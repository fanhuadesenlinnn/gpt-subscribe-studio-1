"""hCaptcha solving for Stripe's intent_confirmation_challenge.

Stripe attaches the challenge to the PaymentIntent:

    next_action.use_stripe_sdk = {
        "type": "intent_confirmation_challenge",
        "stripe_js": {"site_key": ..., "rqdata": ..., "verification_url": "/v1/payment_intents/{pi}/verify_challenge"},
    }

Solving wraps a hosted hCaptcha provider (YesCaptcha-compatible API). The token is
then posted to verification_url together with the PaymentIntent client secret.
No card data or session credentials pass through the solver.
"""
from __future__ import annotations

import logging
import os
import time
from typing import Any, Dict, Optional

from gss_contracts.errors import DomainError, ErrorCode
from gss_core.redaction import redact_sensitive

logger = logging.getLogger("gss.captcha")

DEFAULT_API_URL = "https://api.yescaptcha.com"
#: Must equal the UA used when posting verify_challenge, or hCaptcha rejects the token.
DEFAULT_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/155.0.0.0 Safari/537.36"
)
# The challenge iframe Stripe serves for this site_key. hCaptcha binds the issued
# token to this page and host, so it must match what the checkout actually loads
# (HAR: checksiteconfig?host=b.stripecdn.com&sitekey=c7faac4c-..., asset HCaptcha.html).
STRIPE_HCAPTCHA_PAGE = "https://b.stripecdn.com/stripethirdparty-srv/assets/v35.5/HCaptcha.html"
_CHALLENGE_TYPE = "intent_confirmation_challenge"


def challenge_from_intent(intent: Dict[str, Any]) -> Optional[Dict[str, str]]:
    """Extract site_key / rqdata / verification_url from a PaymentIntent next_action."""
    if not isinstance(intent, dict):
        return None
    next_action = intent.get("next_action")
    next_action = next_action if isinstance(next_action, dict) else {}
    sdk = next_action.get("use_stripe_sdk")
    sdk = sdk if isinstance(sdk, dict) else {}
    if str(sdk.get("type") or "") != _CHALLENGE_TYPE:
        return None
    stripe_js = sdk.get("stripe_js")
    stripe_js = stripe_js if isinstance(stripe_js, dict) else {}
    site_key = str(stripe_js.get("site_key") or "")
    if not site_key:
        return None
    return {
        "site_key": site_key,
        "rqdata": str(stripe_js.get("rqdata") or ""),
        "verification_url": str(stripe_js.get("verification_url") or ""),
    }


def _post_json(session, url: str, payload: Dict[str, Any], timeout: float) -> Dict[str, Any]:
    response = session.post(url, json=payload, timeout=timeout)
    status = int(getattr(response, "status_code", 0) or 0)
    try:
        body = response.json()
    except Exception:
        body = {}
    if status >= 400 or not isinstance(body, dict):
        raise DomainError(ErrorCode.PAYMENT_UNKNOWN_STATE,
                          f"验证码服务返回异常（{status}），请稍后重试")
    return body


def _proxy_fields(proxy: str) -> Dict[str, Any]:
    """Split an http://user:pass@host:port URL into solver task fields."""
    from urllib.parse import urlsplit
    text = str(proxy or "").strip()
    if not text:
        return {}
    if "://" not in text:
        text = "http://" + text
    parsed = urlsplit(text)
    if not parsed.hostname or not parsed.port:
        return {}
    scheme = (parsed.scheme or "http").lower()
    fields: Dict[str, Any] = {
        "proxyType": "socks5" if scheme.startswith("socks") else "http",
        "proxyAddress": parsed.hostname,
        "proxyPort": int(parsed.port),
    }
    if parsed.username:
        fields["proxyLogin"] = parsed.username
    if parsed.password:
        fields["proxyPassword"] = parsed.password
    return fields


def solve_hcaptcha(
    session,
    challenge: Dict[str, str],
    *,
    api_key: str,
    api_url: str = DEFAULT_API_URL,
    proxy: str = "",
    user_agent: str = "",
    timeout: float = 180.0,
    poll_interval: float = 3.0,
) -> str:
    """Return a challenge_response_token for Stripe's verify_challenge."""
    if not api_key:
        raise DomainError(ErrorCode.PAYMENT_UNKNOWN_STATE, "未配置验证码服务密钥，无法完成支付验证")
    site_key = str(challenge.get("site_key") or "")
    if not site_key:
        raise DomainError(ErrorCode.PAYMENT_UNKNOWN_STATE, "Stripe 验证缺少 site_key")

    fields = _proxy_fields(proxy)
    if not fields:
        raise DomainError(ErrorCode.PAYMENT_UNKNOWN_STATE, "支付验证需要与订单一致的出口，但未取到代理")
    # hCaptcha Enterprise binds the token to the solving context, so the solve must
    # leave through the same proxy exit as the checkout, not the server's own IP.
    task: Dict[str, Any] = {
        "type": "HCaptchaTask",
        "websiteURL": STRIPE_HCAPTCHA_PAGE,
        "websiteKey": site_key,
        "isEnterprise": True,
        **fields,
    }
    # Enterprise hCaptcha binds the token to the rqdata blob and the solving UA.
    if challenge.get("rqdata"):
        task["rqdata"] = challenge["rqdata"]
    task["userAgent"] = user_agent or DEFAULT_UA

    created = _post_json(session, f"{api_url}/createTask",
                         {"clientKey": api_key, "task": task}, timeout=30)
    if int(created.get("errorId") or 0) != 0:
        raise DomainError(ErrorCode.PAYMENT_UNKNOWN_STATE,
                          f"验证码任务创建失败：{redact_sensitive(str(created.get('errorDescription') or ''))[:120]}")
    task_id = created.get("taskId")
    if task_id is None:
        raise DomainError(ErrorCode.PAYMENT_UNKNOWN_STATE, "验证码任务未返回 taskId")
    logger.info("captcha_task_created enterprise=True proxied=True rqdata=%s", bool(challenge.get("rqdata")))

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        time.sleep(poll_interval)
        result = _post_json(session, f"{api_url}/getTaskResult",
                            {"clientKey": api_key, "taskId": task_id}, timeout=30)
        if int(result.get("errorId") or 0) != 0:
            code = str(result.get("errorCode") or "")
            if code == "ERROR_TASK_TIMEOUT":
                raise DomainError(ErrorCode.PAYMENT_UNKNOWN_STATE, "验证码任务超时，请重试支付验证")
            continue
        if str(result.get("status")) != "ready":
            continue
        solution = result.get("solution") if isinstance(result.get("solution"), dict) else {}
        token = (solution.get("gRecaptchaResponse") or solution.get("token")
                 or solution.get("respKey") or "")
        token = str(token)
        if not token:
            raise DomainError(ErrorCode.PAYMENT_UNKNOWN_STATE, "验证码服务未返回有效令牌")
        logger.info("captcha_solved token_len=%s", len(token))
        return token
    raise DomainError(ErrorCode.PAYMENT_UNKNOWN_STATE, "验证码求解超时，请重试支付验证")


def verify_challenge(
    session,
    *,
    intent_id: str,
    client_secret: str,
    token: str,
    publishable_key: str,
    api_version: str,
    ekey: str = "",
) -> Dict[str, Any]:
    """Post the solved token to Stripe's verify_challenge endpoint.

    ekey 是 hCaptcha 随 token 一起下发的密钥，Stripe.js 的
    VERIFY_CAPTCHA_CHALLENGE 会把它作为 captcha_response_key 一并回传
    （见 js.stripe.com/v3/fingerprinted/js/hcaptcha-inner-*.js）。
    """
    if not intent_id.startswith("pi_") or not client_secret.startswith("pi_"):
        raise DomainError(ErrorCode.PAYMENT_UNKNOWN_STATE, "缺少支付意图凭据，无法完成验证")
    body = {
        "client_secret": client_secret,
        "challenge_response_token": token,
        "captcha_vendor_name": "hcaptcha",
        "key": publishable_key,
        "_stripe_version": api_version,
    }
    # Stripe 的 verify_challenge 不接受 captcha_response_key：
    #   实测返回 400 parameter_unknown: Received unknown parameter.
    # 逐字段免费探测过 guid/muid/sid/eid/challenge_response_key 等 10 个候选，
    # 全部 parameter_unknown —— 请求体只需要 challenge_response_token。
    if ekey and os.environ.get("GSS_SEND_CAPTCHA_KEY") == "1":
        body["captcha_response_key"] = ekey
    # HAR: verify_challenge is posted by Stripe.js, so its Origin is js.stripe.com —
    # unlike init/confirm/poll which post from pay.openai.com.
    headers = {"Origin": "https://js.stripe.com", "Referer": "https://js.stripe.com/",
               "Content-Type": "application/x-www-form-urlencoded",
               "User-Agent": DEFAULT_UA}
    from urllib.parse import urlencode
    response = session.post(f"https://api.stripe.com/v1/payment_intents/{intent_id}/verify_challenge",
                            data=urlencode(body), headers=headers, timeout=60)
    status = int(getattr(response, "status_code", 0) or 0)
    try:
        payload = response.json()
    except Exception:
        payload = {}
    if status >= 400:
        error = payload.get("error") if isinstance(payload, dict) else None
        error = error if isinstance(error, dict) else {}
        logger.warning("verify_challenge_failed http=%s type=%s code=%s msg=%s", status,
                       error.get("type"), error.get("code"), str(error.get("message") or "")[:200])
        raise DomainError(ErrorCode.PAYMENT_UNKNOWN_STATE,
                          "Stripe 验证未通过：" + redact_sensitive(str(error.get("message") or f"HTTP {status}"))[:200])
    logger.info("verify_challenge_ok status=%s", payload.get("status") if isinstance(payload, dict) else "")
    return payload if isinstance(payload, dict) else {}
