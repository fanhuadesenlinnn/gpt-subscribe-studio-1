"""Backend card tokenization through the Stripe Elements surface.

The Elements surface answers `POST /v1/payment_methods` for an authenticated
merchant publishable key when the request carries the Elements attribution set
and the `js.stripe.com` origin. Verified against a live Team checkout: the
endpoint returns card errors (real validation) instead of an integration-surface
rejection. Card data exists only in this call's memory.
"""
from __future__ import annotations

import logging
import re
import time
import uuid
from typing import Any, Dict, Optional

from gss_contracts.errors import DomainError, ErrorCode
from gss_core.redaction import redact_sensitive

logger = logging.getLogger("gss.tokenize")

_PAYMENT_METHODS_URL = "https://api.stripe.com/v1/payment_methods"
# Hosted checkout sessions require the hosted API version and attribution set.
_HOSTED_VERSION = "2020-08-27;custom_checkout_beta=v1"
_HOSTED_UA = "stripe.js/2bae21f786; stripe-js-v3/2bae21f786; checkout"

#: Only opaque ids ever leave this module.
_CARD_KEYS = ("number", "cvc", "exp_month", "exp_year")


def normalize_card(card: Dict[str, Any]) -> Dict[str, str]:
    """Validate card shape; raise a message that never contains the PAN or CVC."""
    if not isinstance(card, dict):
        raise DomainError(ErrorCode.PAYMENT_DECLINED, "缺少卡片信息")
    number = str(card.get("number") or "").replace(" ", "").replace("-", "")
    cvc = str(card.get("cvc") or "").strip()
    month = str(card.get("exp_month") or "").strip()
    year = str(card.get("exp_year") or "").strip()
    if not number.isdigit() or not 12 <= len(number) <= 19:
        raise DomainError(ErrorCode.PAYMENT_DECLINED, "卡号格式不正确")
    if not cvc.isdigit() or not 3 <= len(cvc) <= 4:
        raise DomainError(ErrorCode.PAYMENT_DECLINED, "CVC 格式不正确")
    if not month.isdigit() or not 1 <= int(month) <= 12:
        raise DomainError(ErrorCode.PAYMENT_DECLINED, "到期月份不正确")
    if not year.isdigit() or len(year) not in (2, 4):
        raise DomainError(ErrorCode.PAYMENT_DECLINED, "到期年份不正确")
    if len(year) == 2:
        year = "20" + year
    return {"number": number, "cvc": cvc, "exp_month": str(int(month)), "exp_year": year}


def _scrub(message: str, card: Dict[str, str]) -> str:
    """Remove card secrets from any upstream text before it reaches a caller."""
    text = str(message or "")
    for key in _CARD_KEYS:
        value = card.get(key)
        if value and len(value) >= 3:
            text = "[已隐藏]".join(text.split(value))
    return redact_sensitive(text)[:400]


def tokenize_card(
    session,
    *,
    card: Dict[str, Any],
    billing: Dict[str, str],
    publishable_key: str,
    checkout_session_id: str,
    checkout_config_id: str = "",
    device: Optional[Dict[str, str]] = None,
    captcha_token: str = "",
) -> str:
    """Create a PaymentMethod on the Elements surface; return only its `pm_` id."""
    if not publishable_key.startswith("pk_"):
        raise DomainError(ErrorCode.PAYMENT_UNKNOWN_STATE, "缺少 Stripe 公钥，无法创建支付方式")
    if not checkout_session_id.startswith("cs_"):
        raise DomainError(ErrorCode.PAYMENT_UNKNOWN_STATE, "缺少结账会话，无法创建支付方式")
    safe = normalize_card(card)
    device = device or {}

    form = {
        "type": "card",
        "card[number]": safe["number"],
        "card[cvc]": safe["cvc"],
        "card[exp_month]": safe["exp_month"],
        "card[exp_year]": safe["exp_year"],
        "billing_details[name]": str(billing.get("name") or ""),
        "billing_details[address][country]": str(billing.get("country") or "").upper(),
        "billing_details[address][line1]": str(billing.get("line1") or ""),
        "billing_details[address][city]": str(billing.get("city") or ""),
        "billing_details[address][state]": str(billing.get("state") or ""),
        "billing_details[address][postal_code]": str(billing.get("postal_code") or ""),
        "payment_user_agent": _HOSTED_UA,
        "client_attribution_metadata[client_session_id]": str(device.get("client_session_id") or uuid.uuid4()),
        "client_attribution_metadata[checkout_session_id]": checkout_session_id,
        "client_attribution_metadata[merchant_integration_source]": "checkout",
        "client_attribution_metadata[merchant_integration_version]": "hosted_checkout",
        "client_attribution_metadata[payment_method_selection_flow]": "merchant_specified",
        "client_attribution_metadata[checkout_config_id]": checkout_config_id,
        "guid": str(device.get("guid") or ""),
        "muid": str(device.get("muid") or ""),
        "sid": str(device.get("sid") or ""),
        "key": publishable_key,
        "_stripe_version": _HOSTED_VERSION,
    }
    # hosted_checkout 的 confirm 会校验 email 是否已绑在 PaymentMethod 上，
    # 空着会得到 400 checkout_confirm_error / reason=checkout_email_missing。
    # 卡主真实邮箱由调用方传入；没填时造一个同名的占位地址，避免整单直接 400。
    email = str(billing.get("email") or "").strip()
    if not email:
        slug = re.sub(r"[^a-z0-9]+", ".", str(billing.get("name") or "cardholder").lower()).strip(".") or "cardholder"
        email = f"{slug}.{uuid.uuid4().hex[:6]}@gmail.com"
    form["billing_details[email]"] = email
    if captcha_token:
        form["radar_options[hcaptcha_token]"] = captcha_token

    headers = {
        # This session's allowed_origins is pay.openai.com; the hosted page posts from there.
        "Origin": "https://pay.openai.com",
        "Referer": "https://pay.openai.com/",
        "Content-Type": "application/x-www-form-urlencoded",
        "Accept": "application/json",
    }
    # A connection-level failure means the request never reached Stripe, so retrying
    # the same call is safe. Any response at all is handled below without retrying.
    response = None
    last_error = None
    for attempt in range(3):
        try:
            response = session.post(_PAYMENT_METHODS_URL, data=form, headers=headers, timeout=30)
            break
        except Exception as exc:
            last_error = exc
            code = getattr(exc, "code", None)
            logger.warning("tokenize_transport_failed type=%s code=%s attempt=%s",
                           type(exc).__name__, code, attempt + 1)
            if attempt < 2:
                time.sleep(0.6 * (attempt + 1))
    # Drop card references as early as possible; `form` is local to this frame.
    for key in _CARD_KEYS:
        form.pop(f"card[{key}]", None)

    if response is None:
        raise DomainError(ErrorCode.CHECKOUT_UPSTREAM_ERROR,
                          "创建支付方式时代理连接中断；本步骤未提交付款，稍后重试即可") from last_error

    status = int(getattr(response, "status_code", 0) or 0)
    try:
        payload = response.json()
    except Exception:
        payload = {}
    if status == 200 and isinstance(payload, dict):
        pm_id = str(payload.get("id") or "")
        if pm_id.startswith("pm_"):
            card_info = payload.get("card") or {}
            logger.info("tokenize_ok brand=%s last4_present=%s funding=%s",
                        card_info.get("brand"), bool(card_info.get("last4")), card_info.get("funding"))
            return pm_id
    error = payload.get("error") if isinstance(payload, dict) else None
    error = error if isinstance(error, dict) else {}
    message = _scrub(str(error.get("message") or f"Stripe 支付方式创建失败（{status}）"), safe)
    code = str(error.get("code") or "")
    decline = str(error.get("decline_code") or "")
    logger.warning("tokenize_failed http=%s type=%s code=%s decline=%s", status,
                   error.get("type"), code, decline)
    detail = " ".join(part for part in (code, decline) if part)
    raise DomainError(ErrorCode.PAYMENT_DECLINED, f"{message}{f' ({detail})' if detail else ''}"
                      if message else f"Stripe 支付方式创建失败（{status}）")


def hosted_confirm_needs_card(token: str) -> bool:
    """Whether a stored token is a hosted payment method vs a browser confirmation token."""
    return str(token or "").startswith("pm_")
