"""Plus / Go / Pro 家族的纯协议支付（custom checkout，会话 id 为 oaics_）。

与 Team 的 hosted 链路（``StripeProtocol``）区别：

    Team (hosted, cs_)          Plus (custom, oaics_)
    ─────────────────────       ──────────────────────────────
    payment_pages/{cs}/confirm  ChatGPT /backend-api/payments/checkout/confirm
    直接拿 PaymentIntent         先 confirm 再 finalize 出 PaymentIntent
    不需要 ctoken_               需要 confirmation_token（由 pm_ 换取）

整条链都在服务端完成，不需要浏览器：

    ① mint_sentinel           挂 openai-sentinel-token / -so-token
    ② POST /v1/payment_methods      卡号 → pm_
    ③ POST /v1/confirmation_tokens  pm_  → ctoken_
    ④ POST /backend-api/payments/checkout/confirm
    ⑤ finalize_stripe_custom_checkout → PaymentIntent
    ⑥ confirm_stripe_intent           → 终态

卡号只在内存里用一次，不落库、不打日志。
"""

from __future__ import annotations

import re
import uuid
from typing import Any, Dict, Optional
from urllib.parse import urlencode

import httpx

from gss_contracts.errors import DomainError, ErrorCode
from gss_core.proxies import rotate_proxy_sid
from gss_core.chatgpt import (
    STRIPE_API_VERSION,
    STRIPE_BASE,
    ChatGPTClient,
    decline_message,
    checkout_confirm_blocked,
    checkout_confirm_message,
    nested_intent_status,
    payment_intent_from_confirm,
    public_checkout_id,
    redact_sensitive,
    stripe_verify_url,
    _processor_from_checkout_url,
    submission_state,
)
from gss_core.stripe_protocol import (
    RUNTIME_HASH,
    STRIPE_API,
    STRIPE_HEADERS,
    V_HOSTED,
    PaymentOutcome,
    StripeProtocol,
    log,
)

_SESSION = re.compile(r"(oaics_[A-Za-z0-9]+|cs_(?:live|test)_[A-Za-z0-9]+)")


class PlusProtocol:
    """custom checkout 的协议支付。"""

    def __init__(
        self,
        *,
        session: str,
        checkout_url: str,
        publishable_key: str,
        card: Optional[Dict[str, str]] = None,
        billing: Optional[Dict[str, str]] = None,
        proxy: Optional[str] = None,
        currency: str = "USD",
        timeout: float = 120.0,
        profile: Optional[Dict[str, Any]] = None,
        customer_session_client_secret: str = "",
    ) -> None:
        self.session = session or ""
        self.checkout_url = checkout_url or ""
        self.publishable_key = publishable_key or ""
        self.card = card or {}
        self.billing = billing or {}
        self.proxy = proxy or None
        # 指纹必须与建单、求解器完全一致
        self.profile = dict(profile or {})
        if self.profile:
            self.ua = self.profile["ua"].format(build=self.profile["build"])
            self.platform = self.profile.get("platform", "Linux x86_64")
            self.screen = tuple(self.profile.get("screen", (1920, 1080, 1053)))
        else:
            self.ua = STRIPE_HEADERS.get("User-Agent", "")
            self.platform = "Linux x86_64"
            self.screen = (1920, 1080, 1053)
        self.headers = {**STRIPE_HEADERS, "User-Agent": self.ua}
        import os as _os
        self.solver_url = _os.environ.get("GSS_LLM_SOLVER_URL", "").rstrip("/")
        if ("host.docker.internal" in self.solver_url
                and not _os.path.exists("/.dockerenv")):
            # 仅宿主机脚本需要换（容器内 host.docker.internal 才是对的；
            # 换了会指向容器自己，导致 Connection refused）
            self.solver_url = self.solver_url.replace(
                "host.docker.internal", "127.0.0.1")
        self._hcap_sitekey = "24ed0064-62cf-4d42-9960-5dd1a41d4e29"
        self._hcap_token = ""
        # 真实 site_key / rqdata 由 elements/sessions 的 passive_captcha 下发
        self._hcap_rqdata = ""
        self._t0 = 0.0
        self.cuss_secret = customer_session_client_secret or ""
        self._elements_session_id = ""
        import os as _os3
        self.three_ds = _os3.environ.get("GSS_REQUEST_3DS", "any")
        self.currency = (currency or "USD").upper()
        self.timeout = timeout
        self.payment_method_id = ""
        # 浏览器 Stripe 输入框已换好的 pm_：给定时跳过服务端提交卡号。
        # 卡数据经过 Stripe iframe，与真实用户路径一致。
        self.external_pm = ""

        m = _SESSION.search(self.checkout_url)
        self.checkout_id = public_checkout_id(m.group(1) if m else "")
        # custom checkout 有两种会话 id：
        #   oaics_   ChatGPT 内部结账会话
        #   cs_live_ Stripe Custom Checkout 会话
        self.is_stripe_custom = self.checkout_id.startswith("cs_")

        self.client = ChatGPTClient.from_raw_session(self.session, proxy=self.proxy)
        self.client.timeout = timeout
        self._http: Optional[httpx.Client] = None

    # ── 资源 ──────────────────────────────────────────────
    def close(self) -> None:
        try:
            self.client.close()
        except Exception:
            pass
        if self._http is not None:
            try:
                self._http.close()
            except Exception:
                pass
            self._http = None

    def _stripe(self) -> httpx.Client:
        if self._http is None:
            self._http = httpx.Client(
                proxy=self.proxy,
                timeout=self.timeout,
                verify=False,
                headers={
                    **self.headers,
                    "Origin": "https://js.stripe.com",
                    "Referer": "https://js.stripe.com/",
                },
            )
        return self._http

    def rotate_proxy_sid(self) -> None:
        """换出口 SID（地区不变）。代理抽到坏出口时用。"""
        if not self.proxy:
            return
        try:
            self.proxy = rotate_proxy_sid(self.proxy)
        except Exception:
            return
        log(f"   轮换出口 SID → {self.proxy.split('@')[-1]}")
        self.close()
        self.client = ChatGPTClient.from_raw_session(self.session, proxy=self.proxy)
        self.client.timeout = self.timeout

    # ── ② pm_ ────────────────────────────────────────────
    def create_payment_method(self) -> str:
        log("② payment_methods（服务端提交卡号）")
        card = self.card
        b = self.billing
        body: Dict[str, str] = {
            "billing_details[name]": b.get("name", ""),
            "billing_details[email]": (b.get("email") or "").strip(),
            "billing_details[address][country]": (b.get("country") or "US").upper(),
            "billing_details[address][line1]": b.get("line1", ""),
            "billing_details[address][city]": b.get("city", ""),
            "billing_details[address][postal_code]": (b.get("postal_code") or b.get("zip") or ""),
            "billing_details[address][state]": b.get("state", ""),
            "type": "card",
            # 订阅需要保存卡片复用；不声明时上游会拒绝复用
            "allow_redisplay": "always",
            "card[number]": card.get("number", ""),
            "card[cvc]": card.get("cvc", ""),
            "card[exp_year]": card.get("exp_year", ""),
            "card[exp_month]": card.get("exp_month", ""),
            # custom checkout 走 Elements，attribution 按 elements 填
            "payment_user_agent": (
                f"stripe.js/{RUNTIME_HASH}; stripe-js-v3/{RUNTIME_HASH}; payment-element"
            ),
            "client_attribution_metadata[client_session_id]": str(uuid.uuid4()),
            "client_attribution_metadata[merchant_integration_source]": "elements",
            "client_attribution_metadata[merchant_integration_version]": "2021",
            "client_attribution_metadata[payment_method_selection_flow]": "automatic",
            "guid": str(uuid.uuid4()),
            "muid": str(uuid.uuid4()),
            "sid": str(uuid.uuid4()),
            "key": self.publishable_key,
            "_stripe_version": V_HOSTED,
        }
        r = self._stripe().post(f"{STRIPE_API}/v1/payment_methods", content=urlencode(body))
        payload: Dict[str, Any] = {}
        try:
            payload = r.json() or {}
        except Exception:
            payload = {}
        if r.status_code != 200:
            err = payload.get("error") or {}
            raise DomainError(
                ErrorCode.CHECKOUT_UPSTREAM_ERROR,
                f"payment_methods {r.status_code}: "
                f"{err.get('code') or err.get('message') or r.text[:160]}",
            )
        pm = str(payload.get("id") or "")
        if not pm.startswith("pm_"):
            raise DomainError(ErrorCode.CHECKOUT_UPSTREAM_ERROR, "payment_methods 没返回 pm_")
        c = payload.get("card") or {}
        log(f"   {pm[:14]}… ({c.get('display_brand')} ****{c.get('last4')})")
        self.payment_method_id = pm
        return pm

    # ── ⑤ Stripe 侧确认（保留原始响应，拒卡原因不丢）──────
    def confirm_intent_raw(
        self, *, intent_id: str, client_secret: str, ctoken: str, return_url: str,
        request_three_d_secure: str = "",
    ) -> tuple[int, Dict[str, Any]]:
        kind = "payment_intents" if intent_id.startswith("pi_") else "setup_intents"
        body: Dict[str, str] = {
            "key": self.publishable_key,
            "client_secret": client_secret,
            "_stripe_version": STRIPE_API_VERSION,
            "client_attribution_metadata[merchant_integration_source]": "l1",
        }
        if ctoken:
            body["confirmation_token"] = ctoken
        if return_url:
            body["return_url"] = return_url
        r = self._stripe().post(
            f"{STRIPE_BASE}/v1/{kind}/{intent_id}/confirm", content=urlencode(body)
        )
        try:
            payload: Dict[str, Any] = r.json() or {}
        except Exception:
            payload = {}
        return r.status_code, payload

    def _confirm(self, ctoken: str, save_choice: Optional[bool]) -> Dict[str, Any]:
        return self.client.confirm_checkout(
            checkout_session_id=self.checkout_id,
            confirm_token=ctoken,
            checkout_url=self.checkout_url,
            billing=self.billing,
            currency=self.currency,
            publishable_key=self.publishable_key,
            save_choice=save_choice,
        )

    # ── 暖场（对齐真实浏览器的会话行为，降低 Radar 风险分）──────
    def _warm_elements_session(self) -> str:
        """GET /v1/elements/sessions —— Elements 会话初始化。"""
        if not self.cuss_secret:
            return ""
        try:
            params = {
                "client_betas[0]": "elements_link_signup_beta_1",
                "customer_session_client_secret": self.cuss_secret,
                "deferred_intent[mode]": "subscription",
                "deferred_intent[currency]": (self.currency or "USD").lower(),
                "deferred_intent[setup_future_usage]": "off_session",
                "deferred_intent[payment_method_types][0]": "card",
                "deferred_intent[payment_method_types][1]": "link",
                "currency": (self.currency or "USD").lower(),
                "key": self.publishable_key,
                "_stripe_version": "2025-03-31.basil",
                "elements_init_source": "stripe.elements",
                "referrer_host": "chatgpt.com",
                "stripe_js_id": str(uuid.uuid4()),
                "locale": (self.profile or {}).get("locale", "en-US"),
                "browser_timezone": (self.profile or {}).get("timezone", "America/New_York"),
                "type": "deferred_intent",
            }
            r = self.client.stripe_http.get(
                "https://api.stripe.com/v1/elements/sessions", params=params,
                timeout=self.timeout)
            j = r.json() if r.content else {}
            sid = str(j.get("session_id") or "")
            self._elements_session_id = sid
            # passive_captcha 是 Stripe 下发的 hCaptcha 配置（前端同源使用）
            cap = j.get("passive_captcha") or {}
            sk = str(cap.get("site_key") or "")
            rq = str(cap.get("rqdata") or "")
            if sk:
                self._hcap_sitekey = sk
                self._hcap_rqdata = rq
                log(f"   暖场 passive_captcha site_key={sk[:8]}… rqdata="
                    f"{(rq[:12] + '…') if rq else '(空)'}")
            log(f"   暖场 elements/sessions → {sid[:28] or '(空)'}")
            return sid
        except Exception as exc:  # noqa: BLE001
            log(f"   暖场 elements 失败: {str(exc)[:70]}")
            return ""

    def _warm_consumers_lookup(self) -> None:
        """POST /v1/consumers/sessions/lookup —— 消费者会话查找。"""
        try:
            body = {
                "request_surface": "web_payment_element",
                "email_address": str((self.billing or {}).get("email") or ""),
                "email_source": "default_value",
                "session_id": self._elements_session_id or str(uuid.uuid4()),
                "key": self.publishable_key,
            }
            self.client.stripe_http.post(
                "https://api.stripe.com/v1/consumers/sessions/lookup",
                data=urlencode(body), timeout=self.timeout)
            log("   暖场 consumers/sessions/lookup")
        except Exception as exc:  # noqa: BLE001
            log(f"   暖场 consumers 失败: {str(exc)[:70]}")

    def _warm_card_metadata(self) -> None:
        """GET /edge-internal/card-metadata —— 卡 BIN 识别。"""
        num = str((self.card or {}).get("number") or "").replace(" ", "")
        if len(num) < 6:
            return
        try:
            self.client.stripe_http.get(
                "https://api.stripe.com/edge-internal/card-metadata",
                params={"bin_prefix": num[:6], "key": self.publishable_key,
                        "_stripe_version": "2025-03-31.basil"},
                timeout=self.timeout)
            log(f"   暖场 card-metadata bin={num[:6]}")
        except Exception as exc:  # noqa: BLE001
            log(f"   暖场 card-metadata 失败: {str(exc)[:70]}")

    def _warm_taxes(self) -> None:
        """POST /backend-api/payments/checkout/taxes —— 算税。"""
        b = self.billing or {}
        try:
            payload = {
                "checkout_session_id": self.checkout_id,
                "processor_entity": "openai_llc",
                "billing_country": str(b.get("country") or "US").upper(),
                "billing_name": str(b.get("name") or ""),
                "billing_address": {
                    "line1": str(b.get("line1") or ""),
                    "line2": "",
                    "city": str(b.get("city") or ""),
                    "country": str(b.get("country") or "US").upper(),
                    "postal_code": str(b.get("postal_code") or b.get("zip") or ""),
                    "state": str(b.get("state") or ""),
                },
                "currency": (self.currency or "USD").lower(),
                "checkout_email": str(b.get("email") or ""),
            }
            self.client.http.post(
                "https://chatgpt.com/backend-api/payments/checkout/taxes",
                json=payload,
                headers=self.client._route_headers(
                    "/backend-api/payments/checkout/taxes",
                    page_url=self.checkout_url),
                timeout=self.timeout)
            log("   暖场 checkout/taxes")
        except Exception as exc:  # noqa: BLE001
            log(f"   暖场 taxes 失败: {str(exc)[:70]}")

    def _warm_tax_id(self) -> None:
        """POST /backend-api/payments/checkout/update_tax_id。"""
        try:
            self.client.http.post(
                "https://chatgpt.com/backend-api/payments/checkout/update_tax_id",
                json={"checkout_session_id": self.checkout_id, "tax_id_info": None},
                headers=self.client._route_headers(
                    "/backend-api/payments/checkout/update_tax_id",
                    page_url=self.checkout_url),
                timeout=self.timeout)
            log("   暖场 update_tax_id")
        except Exception as exc:  # noqa: BLE001
            log(f"   暖场 tax_id 失败: {str(exc)[:70]}")

    def _warmup(self) -> None:
        """按 HAR 顺序跑完暖场，任何一步失败都不阻断。"""
        log("①b 暖场（对齐真实浏览器会话行为）")
        self._warm_elements_session()
        self._warm_consumers_lookup()
        self._warm_card_metadata()
        self._warm_taxes()
        self._warm_tax_id()

    def _hcaptcha_token(self) -> str:
        """向求解器要一个 passive hCaptcha token（对齐 HAR 的 radar_options）。"""
        if not self.solver_url:
            return ""
        payload = {
            "sitekey": self._hcap_sitekey,
            "rqdata": self._hcap_rqdata,
            "host": "b.stripecdn.com",
            "proxy": self.proxy or "",
            "ua": self.ua,
            "platform": self.platform,
            "platform_label": (self.profile or {}).get("label", "Linux"),
            "screen_w": self.screen[0],
            "screen_h": self.screen[1],
            "avail_h": self.screen[2],
            "cores": 8,
            "locale": (self.profile or {}).get("locale", "en-US"),
            "timezone": (self.profile or {}).get("timezone", "America/New_York"),
        }
        try:
            r = httpx.post(f"{self.solver_url}/solve", json=payload,
                           timeout=httpx.Timeout(180.0, connect=15.0))
            j = r.json() if r.content else {}
            tok = str(j.get("token") or "")
            if tok:
                log(f"   hcaptcha token 已获取 len={len(tok)} sitekey={self._hcap_sitekey[:8]}")
                return tok
            log(f"   求解器未出 token: {str(j)[:120]}")
        except Exception as exc:  # noqa: BLE001
            log(f"   求解器异常: {str(exc)[:100]}")
        return ""

    def _confirm_with_ctoken(self, pm: str) -> tuple[Dict[str, Any], str]:
        """ctoken 的 setup_future_usage 与 confirm 的保存选择必须一致。

        覆盖上游两种措辞：
            Card save choice does not match confirmation token
            new_card_save_choice_required
        """
        combos: list[tuple[Optional[str], Optional[bool]]] = [
            ("off_session", True),    # 必须匹配 PI 上的值，否则 400
            (None, None),
        ]
        last: Optional[DomainError] = None
        _tok_cached = self._hcaptcha_token() if not pm else ""
        if not pm and not _tok_cached:
            # 没有 token 就不提交：带上无效/缺失 token 的请求会被 ChatGPT 侧
            # 直接判 blocked，给出误导性的「拒绝确认」，掩盖真实原因。
            raise DomainError(
                ErrorCode.CHECKOUT_UPSTREAM_ERROR,
                "未能获取 hCaptcha 令牌，已终止（未提交任何支付请求）。"
                "请检查求解器服务是否可用。",
            )
        for setup, save in combos:
            _tok = _tok_cached
            ctoken = self.client.create_confirmation_token(
                publishable_key=self.publishable_key,
                payment_method=pm,
                setup_future_usage=setup,
                card=(None if pm else self.card),
                billing=(None if pm else self.billing),
                radar_token=_tok,
                time_on_page_ms=int((__import__("time").time() - (self._t0 or 0)) * 1000),
                currency=self.currency,
            )
            log(f"   ctoken {ctoken[:14]}… (setup_future_usage={setup}, save={save})")
            try:
                return self._confirm(ctoken, save), ctoken
            except DomainError as exc:
                if "save choice" not in str(exc).lower():
                    raise
                last = exc
                log("   保存选择不匹配，换下一组")
        raise last if last else DomainError(ErrorCode.PAYMENT_DECLINED, "保存选择重试失败")

    # ── 主流程 ────────────────────────────────────────────
    def pay(self) -> PaymentOutcome:
        if not self.checkout_id.startswith(("oaics_", "cs_live_", "cs_test_")):
            raise DomainError(ErrorCode.PAYMENT_DECLINED, f"不是 custom checkout 会话：{self.checkout_url[:80]}")
        if not self.publishable_key.startswith(("pk_live_", "pk_test_")):
            raise DomainError(ErrorCode.CHECKOUT_UPSTREAM_ERROR, "缺少可用的 publishable_key")

        out = PaymentOutcome()

        # ① Sentinel（失败不阻断，先跑通链路）
        try:
            log("① 铸造 Sentinel 令牌")
            self.client.mint_sentinel(flow="chatgpt_checkout", page_url=self.checkout_url)
            log("   已挂 openai-sentinel-token")
        except Exception as exc:  # noqa: BLE001
            log(f"   Sentinel 未拿到，继续：{str(exc)[:100]}")

        import time as _time
        self._t0 = _time.time()

        # ①b 暖场：让 Radar 看到真实会话该有的请求序列
        try:
            self._warmup()
        except Exception as exc:  # noqa: BLE001
            log(f"   暖场整体失败，继续：{str(exc)[:70]}")

        # ② 对齐 HAR：卡号直接交给 confirmation_tokens，不先造 pm_
        pm = ""
        if self.external_pm:
            pm = self.external_pm
            self.payment_method_id = pm
            log(f"② 复用浏览器产出的 {pm[:14]}…")
        else:
            log("② 跳过 payment_methods，卡号直传 confirmation_tokens（对齐 HAR）")

        # ③ ctoken_ + ④ confirm
        log("③ confirmation_tokens + ④ checkout/confirm")
        confirmed, ctoken = self._confirm_with_ctoken(pm)

        # classify 期望 HTTP 包装 {status, body}；这里只借它抽错误字段，
        # 状态由本方法自己判（hosted 流程的状态机不适用于 custom）。
        if self.is_stripe_custom:
            log("④b finalize custom checkout → PaymentIntent")
            try:
                confirmed = self.client.finalize_stripe_custom_checkout(
                    checkout_session_id=self.checkout_id,
                    publishable_key=self.publishable_key,
                    checkout_url=self.checkout_url,
                    processor_entity=_processor_from_checkout_url(self.checkout_url),
                    confirmed=confirmed,
                )
            except Exception as exc:  # noqa: BLE001
                log(f"   finalize 失败：{str(exc)[:110]}")

        _pi = confirmed.get("payment_intent")
        _pi = _pi if isinstance(_pi, dict) else {}
        log(f"   confirm: status={confirmed.get('status')} type={confirmed.get('type')} "
            f"pi={str(_pi.get('id') or '')[:12]} pi_status={_pi.get('status')}")

        scanned = StripeProtocol.classify({"status": 200, "body": confirmed}, None)
        out.code = scanned.code
        out.decline_code = scanned.decline_code
        out.message = scanned.message
        out.intent_status = scanned.intent_status
        out.charge_id = scanned.charge_id
        out.payment_method_id = scanned.payment_method_id or pm
        out.raw = {"confirm": confirmed}

        # 被风控/资格拦截
        if checkout_confirm_blocked(confirmed):
            import json as _json
            log(f"   原始响应：{_json.dumps(confirmed, ensure_ascii=False)[:900]}")
            out.state = "card_declined"
            out.message = checkout_confirm_message(confirmed) or "ChatGPT 侧拒绝确认"
            log(f"   被拦截：{out.message[:100]}")
            return out

        state = submission_state(confirmed)
        if state in {"failed", "declined", "canceled", "cancelled"}:
            out.state = "card_declined"
            out.message = out.message or f"ChatGPT 未完成确认: {state}"
            log(f"   确认失败：{state}")
            return out

        intent_id, intent_secret, return_url = payment_intent_from_confirm(confirmed)
        pi_status = nested_intent_status(confirmed)
        nested = confirmed.get("payment_intent")
        nested = nested if isinstance(nested, dict) else {}

        # 卡被拒
        if pi_status in {"requires_payment_method", "canceled", "cancelled"} or nested.get(
            "last_payment_error"
        ):
            out.state = "card_declined"
            log(f"   拒卡 status={pi_status} decline={out.decline_code}")
            return out

        # 已经成功
        if pi_status == "succeeded":
            out.state = "paid"
            return out

        # 3DS：纯协议无法完成
        if pi_status == "requires_action":
            if intent_id and intent_secret:
                try:
                    live = self.client.retrieve_stripe_intent(
                        intent_id=intent_id,
                        client_secret=intent_secret,
                        publishable_key=self.publishable_key,
                    )
                    live_status = str(live.get("status") or "")
                    out.intent_status = live_status or pi_status
                    if live_status == "succeeded":
                        out.state = "paid"
                        return out
                    if live_status == "requires_payment_method":
                        out.state = "card_declined"
                        return out
                except Exception as exc:  # noqa: BLE001
                    log(f"   读取 intent 失败：{str(exc)[:80]}")
            out.state = "challenge_required"
            na = nested.get("next_action") or {}
            sdk = (na.get("use_stripe_sdk") or {}) if isinstance(na, dict) else {}
            kind = str(sdk.get("type") or na.get("type") or "")
            out.raw["next_action"] = na
            if kind == "intent_confirmation_challenge":
                out.message = "被 hCaptcha 人机验证拦截（intent_confirmation_challenge）"
                log("   命中 hCaptcha 挑战，纯协议放弃本单")
            else:
                out.message = f"需要 3DS 验证（{kind or 'requires_action'}），纯协议无法完成"
                log(f"   需要 3DS（{kind or '未知类型'}），纯协议放弃本单")
            return out

        # 还差一次 Stripe 侧确认
        if intent_id and intent_secret:
            if not return_url:
                return_url = stripe_verify_url(self.checkout_id)
            log("⑤ Stripe payment_intents/confirm")
            # Stripe 侧要求 ctoken 带 setup_future_usage（订阅要保存卡片），
            # 而 ChatGPT 侧的 confirm 用的是另一个 token，两者可以不同。
            intent_ctoken = ctoken
            # 只有走 pm_ 路径时才需要另建 ctoken；卡号直传路径下 ctoken 已经
            # 带好卡数据与 radar token，直接用，不必也不能再建（无 pm_ 会 400）。
            if pm:
                try:
                    intent_ctoken = self.client.create_confirmation_token(
                        publishable_key=self.publishable_key,
                        payment_method=pm,
                        setup_future_usage="off_session",
                    )
                    log(f"   intent ctoken {intent_ctoken[:14]}… (off_session)")
                except Exception as exc:  # noqa: BLE001
                    log(f"   intent ctoken 创建失败，复用原 token：{str(exc)[:70]}")
            http_status, final = self.confirm_intent_raw(
                intent_id=intent_id,
                client_secret=intent_secret,
                ctoken=intent_ctoken,
                return_url=return_url,
            )
            if http_status >= 400:
                err = final.get("error") if isinstance(final.get("error"), dict) else {}
                code = str(err.get("code") or "")
                decline = str(err.get("decline_code") or "")
                msg = str(err.get("message") or f"Stripe HTTP {http_status}")
                out.code = code or out.code
                out.decline_code = decline or out.decline_code
                # 402 的错误体里没有 PaymentIntent 状态；
                # 不能沿用第 ④ 步 ChatGPT 的 submission status（那是 "success"，
                # 只表示「确认请求被接受」，不是扣款结果）。
                out.intent_status = str(final.get("status") or "") or None
                out.raw["final"] = final
                if http_status == 402 or code == "card_declined" or decline:
                    out.state = "card_declined"
                    out.message = decline_message(redact_sensitive(decline or code or msg))
                    log(f"   拒卡 {code}/{decline}：{msg[:90]}")
                else:
                    out.state = "unknown"
                    out.message = redact_sensitive(msg)[:200]
                    log(f"   Stripe {http_status}：{msg[:90]}")
                return out

            import json as _json
            log(f"   ⑤ 原始响应 HTTP {http_status}: {_json.dumps(final, ensure_ascii=False)[:600]}")
            final_scanned = StripeProtocol.classify({"status": http_status, "body": final}, None)
            out.code = final_scanned.code or out.code
            out.decline_code = final_scanned.decline_code or out.decline_code
            out.message = final_scanned.message or out.message
            out.intent_status = final_scanned.intent_status or out.intent_status
            out.charge_id = final_scanned.charge_id or out.charge_id
            out.raw["final"] = final

            final_status = str(final.get("status") or "")
            if final_status == "succeeded":
                out.state = "paid"
            elif final_status in {"requires_payment_method", "canceled", "cancelled"}:
                out.state = "card_declined"
            elif final_status == "requires_action":
                out.state = "challenge_required"
                out.message = out.message or "需要 3DS 验证，纯协议无法完成"
            elif final_status in {"processing", "requires_capture"}:
                out.state = "pending"
            else:
                out.state = "unknown"
            return out

        # ChatGPT 侧说成功但没给 intent
        if str(confirmed.get("status") or "") in {"succeeded", "success", "complete", "completed"}:
            out.state = "paid"
            return out

        out.state = "unknown"
        out.message = out.message or f"未预期的确认结果：{state or pi_status or '空'}"
        return out

    # ── 便捷入口 ──────────────────────────────────────────
    def pay_from_url(self, retries: int = 3) -> PaymentOutcome:
        """遇到代理故障（SSL/EOF/连接错误）时轮换 SID 重试。"""
        last_exc: Optional[Exception] = None
        for attempt in range(retries):
            try:
                return self.pay()
            except DomainError:
                raise
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
                msg = str(exc)
                if not any(k in msg for k in ("SSL", "EOF", "ConnectError", "TLS")):
                    raise
                if attempt == retries - 1:
                    raise
                log(f"   代理故障（第 {attempt + 1}/{retries} 次），轮换重试")
                self.rotate_proxy_sid()
        raise last_exc if last_exc else DomainError(
            ErrorCode.CHECKOUT_UPSTREAM_ERROR, "协议支付重试失败"
        )
