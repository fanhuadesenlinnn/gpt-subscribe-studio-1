"""Team Hosted Checkout protocol. No ChatGPT Custom Checkout beta fields."""
from __future__ import annotations

import time
import uuid
import html
import re
import logging
from typing import Any
from urllib.parse import urlencode, urlsplit, urljoin

from gss_contracts.errors import DomainError, ErrorCode

HOSTED_API_VERSION = "2020-08-27;custom_checkout_beta=v1"
HOSTED_ELEMENTS_VERSION = "2026-03-25.dahlia"
logger = logging.getLogger("gss.hosted")


def device_id() -> str:
    return str(uuid.uuid4()) + uuid.uuid4().hex[:6]


class TeamHostedClient:
    def __init__(self, client):
        self.client = client
        self.http = client.stripe_http
        # The checkout session's allowed_origins lists pay.openai.com only.
        self.http.headers.update({"Origin": "https://pay.openai.com", "Referer": "https://pay.openai.com/"})

    @staticmethod
    def _stage(path: str) -> str:
        if path.endswith("/init"):
            return "初始化结账"
        if path.endswith("/confirm"):
            return "确认支付"
        if path.endswith("/poll"):
            return "查询支付状态"
        if path == "elements/sessions":
            return "初始化支付组件"
        return "更新账单与税费"

    def _request(self, method: str, path: str, **kwargs) -> dict:
        stage = self._stage(path)
        submitted = method == "POST" and path.endswith("/confirm")
        # Reads and idempotent preparation may reconnect once, using the SAME proxy and SID.
        # A lost Confirm response is ambiguous and must never be retried automatically.
        # TLS/connect failures mean the request never reached Stripe, so retrying is safe.
        # A Confirm whose response was lost is ambiguous and is never retried.
        attempts = 1 if submitted else 3
        for attempt in range(attempts):
            started = time.monotonic()
            try:
                response = getattr(self.http, method.lower())("https://api.stripe.com/v1/" + path,
                                                              timeout=self.client.timeout, **kwargs)
            except Exception as exc:
                code = getattr(exc, "code", None)
                logger.warning("hosted_transport_failed stage=%s method=%s error=%s code=%s elapsed=%.2f attempt=%s confirm_submitted=%s",
                               stage, method, type(exc).__name__, code, time.monotonic() - started, attempt + 1, submitted)
                if not submitted and attempt + 1 < attempts:
                    time.sleep(0.6 * (attempt + 1))
                    continue
                if submitted:
                    raise DomainError(ErrorCode.PAYMENT_UNKNOWN_STATE,
                        "Stripe 确认支付请求中断，扣款结果待确认，请查询状态，不要重复付款") from exc
                if code == 35 or type(exc).__name__ in {"SSLError", "ConnectionError"}:
                    hint = "订单代理 TLS 握手失败，线路不稳定"
                elif code == 28:
                    hint = "订单代理连接超时"
                else:
                    hint = "订单代理或网络连接中断"
                raise DomainError(ErrorCode.CHECKOUT_UPSTREAM_ERROR,
                    f"Stripe {stage}失败：{hint}；本步骤未提交付款确认，稍后重试本步骤即可") from exc
            logger.info("hosted_response stage=%s method=%s http=%s elapsed=%.2f attempt=%s",
                        stage, method, getattr(response, "status_code", 0),
                        time.monotonic() - started, attempt + 1)
            return self.response(response)
        raise AssertionError("unreachable")

    def post(self, path: str, fields: dict[str, Any]) -> dict:
        # Confirm is a mutation: no implicit retries, proxy changes or direct fallback.
        return self._request("POST", path, data=urlencode(fields))

    def get(self, path: str, params: dict[str, Any]) -> dict:
        return self._request("GET", path, params=params)

    @staticmethod
    def response(response) -> dict:
        from .chatgpt import _json, _raise_stripe
        payload = _json(response)
        status = int(getattr(response, "status_code", 0) or 0)
        if status >= 400:
            error = payload.get("error") if isinstance(payload, dict) else None
            error = error if isinstance(error, dict) else {}
            # Stripe 把真正的失败原因放在 extra_fields.confirm_error_reason，
            # 不看这个字段就只能看到笼统的 "An error has occurred confirming the Checkout Session."
            extra = error.get("extra_fields") if isinstance(error.get("extra_fields"), dict) else {}
            logger.warning(
                "hosted_stripe_error http=%s type=%s code=%s decline=%s param=%s reason=%s msg=%s",
                status, error.get("type"), error.get("code"), error.get("decline_code"),
                error.get("param"), extra.get("confirm_error_reason"),
                str(error.get("message") or "")[:300])
            _raise_stripe(payload, status)
        return payload

    def current_timing(self, session_id: str) -> str:
        """Read the live Hosted page and its declared Stripe init bundle; never replay HAR URLs."""
        from .hosted_timing import rv_timestamp_from_script
        timeout = min(float(self.client.timeout), 15)
        page_url = f"https://pay.openai.com/c/pay/{session_id}"
        try:
            page = self.http.get(page_url, timeout=timeout, allow_redirects=False)
            if int(getattr(page, "status_code", 0)) != 200:
                return ""
            text = str(getattr(page, "text", ""))
            if len(text) > 4_000_000:
                return ""
            value = rv_timestamp_from_script(text)
            if value:
                return value
            urls = re.findall(r'(?:src|href)=["\']([^"\']+)["\']', text)
            for raw in urls:
                url = html.unescape(urljoin(page_url, raw))
                parsed = urlsplit(url)
                if parsed.scheme != "https" or parsed.hostname != "js.stripe.com" or parsed.username or parsed.password:
                    continue
                if not parsed.path.startswith("/v3/") or "checkout-app-init" not in parsed.path or not parsed.path.endswith(".js"):
                    continue
                script = self.http.get(url, timeout=timeout, allow_redirects=False)
                if int(getattr(script, "status_code", 0)) != 200:
                    return ""
                source = str(getattr(script, "text", ""))
                return (rv_timestamp_from_script(source) or "") if len(source) <= 8_000_000 else ""
        except Exception:
            return ""
        return ""

    def quote(self, *, session_id: str, key: str, billing: dict[str, str] | None,
              currency: str, browser_locale: str = "en-US", browser_timezone: str = "UTC") -> dict:
        from .chatgpt import expected_amount_minor
        if not re.fullmatch(r"cs_(?:live|test)_[A-Za-z0-9_]+", session_id) or not key.startswith("pk_"):
            raise DomainError(ErrorCode.PAYMENT_UNKNOWN_STATE, "Hosted Checkout 会话或公钥缺失")
        prefix = f"payment_pages/{session_id}"
        phase_started = time.monotonic()
        init = self.post(prefix + "/init", {"key": key, "eid": "NA", "redirect_type": "url",
                                          "browser_locale": browser_locale, "browser_timezone": browser_timezone})
        logger.info("hosted_phase phase=init currency=%s due=%s elapsed=%.2f",
                    str(init.get("currency") or ""), expected_amount_minor(init),
                    time.monotonic() - phase_started)
        eid = str(init.get("eid") or "NA")
        tax = init
        if billing:
            # Match the Hosted page: select billing country, personal purchase, then full address.
            phase_started = time.monotonic()
            self.post(prefix, {"key": key, "eid": eid, "tax_region[country]": billing["country"]})
            logger.info("hosted_phase phase=tax_country country=%s elapsed=%.2f",
                        billing["country"], time.monotonic() - phase_started)
            phase_started = time.monotonic()
            self.post(prefix, {"key": key, "eid": eid, "tax_id_collection[purchasing_as_business]": "false"})
            logger.info("hosted_phase phase=purchasing_as_business elapsed=%.2f", time.monotonic() - phase_started)
            address = {"key": key, "eid": eid}
            for name in ("country", "state", "postal_code", "line1", "city"):
                value = str(billing.get(name) or "").strip()
                if value:
                    address[f"tax_region[{name}]"] = value
            phase_started = time.monotonic()
            tax = self.post(prefix, address)
            logger.info("hosted_phase phase=tax_address country=%s state=%s elapsed=%.2f due=%s",
                        billing.get("country", ""), billing.get("state", ""),
                        time.monotonic() - phase_started, expected_amount_minor(tax))
        amount = expected_amount_minor(tax)
        if amount is None:
            amount = expected_amount_minor(init)
        if amount is None:
            raise DomainError(ErrorCode.PAYMENT_UNKNOWN_STATE, "Team Hosted Checkout 缺少应付金额")
        code = str(init.get("currency") or currency).lower()
        logger.info("hosted_quote currency=%s amount_minor=%s billing_country=%s eid_present=%s config_present=%s",
                    code, amount, (billing or {}).get("country", "initial"), bool(eid),
                    bool(tax.get("config_id") or init.get("config_id")))
        return {"session_id": session_id, "publishable_key": key, "eid": eid, "amount_minor": amount,
                "currency": code, "billing": dict(billing or {}),
                "checkout_config_id": str(tax.get("config_id") or init.get("config_id") or ""),
                "customer_email": str((billing or {}).get("email") or init.get("customer_email") or ""),
                "site_key": tax.get("site_key") or init.get("site_key"),
                "rqdata": tax.get("rqdata") or init.get("rqdata")}

    def prepare(self, *, session_id: str, key: str, billing: dict[str, str],
                currency: str, browser_locale: str = "en-US", browser_timezone: str = "UTC",
                on_quote=None) -> dict:
        from .chatgpt import expected_amount_minor
        quote = self.quote(session_id=session_id, key=key, billing=billing, currency=currency,
                           browser_locale=browser_locale, browser_timezone=browser_timezone)
        if on_quote:
            on_quote(quote)
        amount, code, eid = quote["amount_minor"], quote["currency"], quote["eid"]
        js_id = str(uuid.uuid4())
        params = {"key": key, "type": "deferred_intent", "checkout_session_id": session_id,
                  "hosted_surface": "checkout", "elements_init_source": "checkout",
                  "referrer_host": "pay.openai.com", "stripe_js_id": js_id,
                  "locale": browser_locale, "browser_timezone": browser_timezone,
                  "deferred_intent[mode]": "subscription", "deferred_intent[amount]": str(amount),
                  "deferred_intent[currency]": code, "deferred_intent[setup_future_usage]": "off_session",
                  "deferred_intent[payment_method_types][0]": "card",
                  "client_betas[0]": "google_pay_beta_1",
                  "client_betas[1]": "disable_deferred_intent_client_validation_beta_1",
                  "client_betas[2]": "blocked_card_brands_beta_2",
                  "_stripe_version": HOSTED_ELEMENTS_VERSION}
        phase_started = time.monotonic()
        elements = self.get("elements/sessions", params)
        logger.info("hosted_phase phase=elements amount_minor=%s currency=%s elapsed=%.2f session_present=%s",
                    amount, code, time.monotonic() - phase_started, bool(elements.get("session_id")))
        # The elements/sessions call refreshes the server-side checkout configuration.
        # HAR re-submits the address afterwards, and the payment method + confirm must use
        # that newest config_id, otherwise Stripe rejects confirm with checkout_confirm_error.
        if billing:
            phase_started = time.monotonic()
            refresh = {"key": key, "eid": eid}
            for name in ("country", "state", "postal_code", "line1", "city"):
                value = str(billing.get(name) or "").strip()
                if value:
                    refresh[f"tax_region[{name}]"] = value
            refreshed = self.post(f"payment_pages/{session_id}", refresh)
            latest_config = str(refreshed.get("config_id") or "")
            if latest_config:
                quote["checkout_config_id"] = latest_config
            refreshed_amount = expected_amount_minor(refreshed)
            if refreshed_amount is not None:
                amount = refreshed_amount
            logger.info("hosted_phase phase=config_refresh config=%s elapsed=%.2f due=%s",
                        bool(latest_config), time.monotonic() - phase_started, refreshed_amount)

        captcha = elements.get("passive_captcha") or {}
        site_key = quote.get("site_key") or captcha.get("site_key")
        rqdata = quote.get("rqdata") or captcha.get("rqdata")
        captcha_settings = {"site_key": site_key, "rqdata": rqdata}
        if captcha.get("token_timeout_seconds"):
            captcha_settings["token_timeout_seconds"] = captcha["token_timeout_seconds"]
        phase_started = time.monotonic()
        timing = self.current_timing(session_id)
        logger.info("hosted_phase phase=timing elapsed=%.2f found=%s", time.monotonic() - phase_started, bool(timing))
        # rv_timestamp is a risk signal, not a required field: a slow proxy that cannot
        # fetch the Stripe bundle must not abort the whole payment preparation. Confirm
        # still validates, so a missing value only lowers the score.
        if not timing:
            logger.warning("hosted_timing_missing session=%s 继续确认，稍后由 Stripe 判定", session_id[-8:])
        from .fingerprint import register_device
        device = register_device(self.http)
        device["client_session_id"] = js_id
        logger.info("hosted_prepare_ready amount_minor=%s currency=%s captcha=%s fingerprint=%s",
                    amount, code, bool(site_key), bool(device.get("guid")))
        return {"context_id": str(uuid.uuid4()), "session_id": session_id, "publishable_key": key,
                "eid": eid, "amount_minor": amount, "currency": code,
                "checkout_config_id": quote["checkout_config_id"],
                "elements_session_id": str(elements.get("session_id") or ""),
                "client_session_id": js_id, "guid": device["guid"],
                "muid": device["muid"], "sid": device["sid"],
                "billing": dict(billing), "prepared_at": time.time(),
                "passive_captcha": captcha_settings,
                "customer_email": quote["customer_email"],
                "rv_timestamp": timing}

    def confirm(self, context: dict, payment_method: str, *, passive_captcha_token: str = "",
                rv_timestamp: str = "") -> dict:
        if not payment_method.startswith("pm_"):
            raise DomainError(ErrorCode.PAYMENT_DECLINED, "Team 需要浏览器返回的 pm_ 标识")
        body = {"key": context["publishable_key"], "_stripe_version": HOSTED_API_VERSION,
                "eid": context["eid"], "payment_method": payment_method,
                "expected_amount": str(context["amount_minor"]),
                "consent[terms_of_service]": "accepted", "expected_payment_method_type": "card",
                "guid": context["guid"], "muid": context["muid"], "sid": context["sid"],
                "client_attribution_metadata[client_session_id]": context["client_session_id"],
                "client_attribution_metadata[checkout_session_id]": context["session_id"],
                # Matches the HAR hosted flow used to create the payment method.
                "client_attribution_metadata[merchant_integration_source]": "checkout",
                "client_attribution_metadata[merchant_integration_version]": "hosted_checkout",
                "client_attribution_metadata[payment_method_selection_flow]": "merchant_specified",
                "client_attribution_metadata[checkout_config_id]": context["checkout_config_id"],
                "link_brand": "link", "passive_captcha_ekey": ""}
        if passive_captcha_token:
            body["passive_captcha_token"] = passive_captcha_token
        timing = str(context.get("rv_timestamp") or rv_timestamp or "")
        if timing:
            body["rv_timestamp"] = timing
        return self.post(f"payment_pages/{context['session_id']}/confirm", body)

    def tokenize(self, *, card: dict, billing: dict[str, str], publishable_key: str,
                 session_id: str, checkout_config_id: str = "",
                 device: dict[str, str] | None = None, captcha_token: str = "") -> str:
        """Create a PaymentMethod for this hosted session; return only the pm_ id."""
        from .tokenize import tokenize_card
        return tokenize_card(self.http, card=card, billing=billing, publishable_key=publishable_key,
                             checkout_session_id=session_id,
                             checkout_config_id=checkout_config_id,
                             device=device, captcha_token=captcha_token)

    def poll(self, *, session_id: str, key: str) -> dict:
        return self.get(f"payment_pages/{session_id}/poll", {"key": key})
