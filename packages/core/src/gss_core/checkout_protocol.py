"""Single-checkout HTTP payment contract observed in the 2026-10-29 capture.

No saved-card preflight, historical credentials, browser impersonation metadata,
challenge solver, or automatic financial replay is used by this flow.
"""
from __future__ import annotations

from decimal import Decimal
from typing import Any
from urllib.parse import urlencode, urlsplit

from gss_contracts.errors import DomainError, ErrorCode
from .chatgpt import APP_BASE, STRIPE_BASE, STRIPE_API_VERSION, ChatGPTClient, payment_intent_from_confirm, redact_sensitive
from .saved_cards import restore_account
from .saved_card_protocol import _factor, _id, _minor, _setup_rejection, _stripe_error, outcome
from .plus_har_protocol import har_engine_enabled, run_har_context


def _json(response) -> dict:
    payload = response.json()
    if not isinstance(payload, dict):
        raise DomainError(ErrorCode.CHECKOUT_UPSTREAM_ERROR, "上游响应格式未核实，请查询原订单")
    return payload


def _request_error(payload: dict, status: int, stage: str) -> dict | None:
    if 400 <= status < 500 and status not in {408, 409, 429}:
        result = _setup_rejection(payload, status)
        result["stage"] = stage
        if result["state"] == "integration_error":
            result["message"] = "Stripe 拒绝了当前商户的服务端卡片采集接口，尚未提交结账确认。"
        return result
    return None


def _page_url(context: dict) -> str:
    url = str(context.get("checkout_url") or "")
    if urlsplit(url).scheme != "https" or urlsplit(url).hostname != "chatgpt.com":
        raise DomainError(ErrorCode.CHECKOUT_UPSTREAM_ERROR, "结账链接不属于当前商户")
    return url


def _read_checkout(client: ChatGPTClient, context: dict) -> dict:
    route = f"/backend-api/payments/checkout/{context['processor']}/{context['checkout_id']}"
    response = client.http.get(APP_BASE + route, headers=client._route_headers(route, page_url=_page_url(context)), timeout=client.timeout)
    payload = _json(response)
    if int(response.status_code) >= 400:
        client._raise(ErrorCode.CHECKOUT_UPSTREAM_ERROR, response, "读取原结账状态")
    if payload.get("checkout_session_id") != context["checkout_id"]:
        raise DomainError(ErrorCode.CHECKOUT_UPSTREAM_ERROR, "返回的结账会话与订单不匹配")
    if payload.get("publishable_key") and payload["publishable_key"] != context["publishable_key"]:
        raise DomainError(ErrorCode.CHECKOUT_UPSTREAM_ERROR, "原结账 Stripe 商户公钥发生变化")
    if payload.get("account_id") and payload["account_id"] != context["account_id"]:
        raise DomainError(ErrorCode.CHECKOUT_UPSTREAM_ERROR, "返回的结账账号与订单不匹配")
    secret = str(payload.get("customer_session_client_secret") or "")
    if secret:
        previous = context.get("customer_session_client_secret")
        if previous and previous != secret:
            raise DomainError(ErrorCode.CHECKOUT_UPSTREAM_ERROR, "结账 Customer 会话发生变化，请核对原订单")
        context["customer_session_client_secret"] = secret
    return payload


def _checkout_quote(session: dict, expected_currency: str) -> tuple[Decimal, str]:
    state = session.get("checkout_state") if isinstance(session.get("checkout_state"), dict) else {}
    details = session.get("billing_details") if isinstance(session.get("billing_details"), dict) else {}
    currencies = [str(value).upper() for value in (session.get("currency"), state.get("currency"), details.get("currency")) if value]
    if not currencies or any(value != expected_currency.upper() for value in currencies):
        raise DomainError(ErrorCode.CHECKOUT_UPSTREAM_ERROR, "结账报价币种与订单不一致或尚未核实")
    total = state.get("total")
    if not isinstance(total, dict) or not isinstance(total.get("total"), dict):
        raise DomainError(ErrorCode.CHECKOUT_UPSTREAM_ERROR, "结账缺少最终应付总额")
    minor = total["total"].get("minorUnitsAmount")
    if not isinstance(minor, int) or isinstance(minor, bool) or minor < 0:
        raise DomainError(ErrorCode.CHECKOUT_UPSTREAM_ERROR, "结账总额未核实，尚未提交付款")
    return Decimal(minor) / _factor(expected_currency), expected_currency.upper()


def _sync_quote(client: ChatGPTClient, context: dict, billing: dict) -> None:
    from .chatgpt import _taxes_body
    route = "/backend-api/payments/checkout/taxes"
    response = client.http.post(APP_BASE + route,
        json=_taxes_body(checkout_id=context["checkout_id"], billing=billing, processor=context["processor"], currency=context["currency"]),
        headers=client._route_headers(route, page_url=_page_url(context)), timeout=client.timeout)
    payload = _json(response)
    if int(response.status_code) >= 400:
        client._raise(ErrorCode.CHECKOUT_UPSTREAM_ERROR, response, "读取账单税费")
    session = payload.get("checkout_session")
    if not isinstance(session, dict) or session.get("checkout_session_id") != context["checkout_id"]:
        raise DomainError(ErrorCode.CHECKOUT_UPSTREAM_ERROR, "税费响应缺少匹配的结账会话")
    if session.get("publishable_key") and session["publishable_key"] != context["publishable_key"]:
        raise DomainError(ErrorCode.CHECKOUT_UPSTREAM_ERROR, "税费响应的商户公钥不匹配")
    amount, currency = _checkout_quote(session, context["currency"])
    context["amount"] = amount
    context["quoted"] = True
    route = "/backend-api/payments/checkout/update_tax_id"
    response = client.http.post(APP_BASE + route, json={"checkout_session_id": context["checkout_id"], "tax_id_info": None},
        headers=client._route_headers(route, page_url=_page_url(context)), timeout=client.timeout)
    if int(response.status_code) >= 400:
        client._raise(ErrorCode.CHECKOUT_UPSTREAM_ERROR, response, "更新税费信息")


def _read_elements(client: ChatGPTClient, context: dict) -> None:
    params = {"key": context["publishable_key"], "_stripe_version": STRIPE_API_VERSION,
              "type": "deferred_intent", "deferred_intent[mode]": "subscription",
              "deferred_intent[amount]": str(_minor(context["amount"], context["currency"])),
              "deferred_intent[currency]": context["currency"].lower(),
              "deferred_intent[setup_future_usage]": "off_session",
              "deferred_intent[payment_method_types][0]": "card",
              "deferred_intent[payment_method_types][1]": "link"}
    if context.get("customer_session_client_secret"):
        params["customer_session_client_secret"] = context["customer_session_client_secret"]
    response = client.stripe_http.get(STRIPE_BASE + "/v1/elements/sessions", params=params, timeout=client.timeout)
    payload = _json(response)
    if int(response.status_code) >= 400:
        client._raise(ErrorCode.CHECKOUT_UPSTREAM_ERROR, response, "读取当前结账支付上下文")
    customer = _id(payload.get("customer"))
    if customer.startswith("cus_"):
        if context.get("customer_id") and context["customer_id"] != customer:
            raise DomainError(ErrorCode.CHECKOUT_UPSTREAM_ERROR, "Elements 返回的付款 Customer 发生变化")
        context["customer_id"] = customer
    # Stripe response bodies were absent from the HAR: do not infer session IDs or flags.
    context["elements_read"] = True


def _create_token(client: ChatGPTClient, context: dict, card: dict, billing: dict) -> dict | None:
    context["token_started"] = True
    form: dict[str, Any] = {"key": context["publishable_key"], "_stripe_version": STRIPE_API_VERSION,
        "payment_method_data[type]": "card", "payment_method_data[allow_redisplay]": "always",
        "setup_future_usage": "off_session", "set_as_default_payment_method": "false",
        "client_context[mode]": "subscription", "client_context[currency]": context["currency"].lower(),
        "client_context[payment_method_types][0]": "card", "client_context[payment_method_types][1]": "link"}
    if context.get("customer_id"):
        form["client_context[customer]"] = context["customer_id"]
    for key in ("number", "cvc", "exp_month", "exp_year"):
        form[f"payment_method_data[card][{key}]"] = str(card.get(key) or "")
    for key in ("name", "email"):
        form[f"payment_method_data[billing_details][{key}]"] = billing.get(key, "")
    for key in ("line1", "city", "state", "postal_code", "country"):
        form[f"payment_method_data[billing_details][address][{key}]"] = billing.get(key, "")
    # No captured Radar/captcha values or invented Stripe.js identity are sent.
    response = client.stripe_http.post(STRIPE_BASE + "/v1/confirmation_tokens", data=urlencode(form), timeout=client.timeout)
    payload = _json(response)
    status = int(response.status_code)
    if status >= 400:
        terminal = _request_error(payload, status, "setup")
        if terminal:
            context["terminal_result"] = terminal
            return terminal
        return outcome("unknown", "setup", "支付方式准备的结果待核实，本次未重复创建")
    token = str(payload.get("id") or "")
    if not token.startswith("ctoken_"):
        return outcome("unknown", "setup", "未读到有效确认令牌，请查询原订单")
    context["confirmation_token"] = token
    preview = payload.get("payment_method_preview")
    if isinstance(preview, dict) and str(preview.get("id") or "").startswith("pm_"):
        context["payment_method_id"] = preview["id"]
    return None


def _return_url(context: dict, confirmed: dict) -> str:
    value = str(confirmed.get("confirm_return_url") or context.get("confirm_return_url") or "")
    parts = urlsplit(value)
    if parts.scheme != "https" or parts.hostname != "chatgpt.com" or parts.username or parts.password:
        raise DomainError(ErrorCode.CHECKOUT_UPSTREAM_ERROR, "未取得可信的支付返回地址，尚未提交 Stripe 确认")
    return value


def _app_confirm(client: ChatGPTClient, context: dict) -> dict | None:
    context["app_confirmation_started"] = True
    route = "/backend-api/payments/checkout/confirm"
    response = client.http.post(APP_BASE + route, json={"checkout_session_id": context["checkout_id"],
        "confirm_token": context["confirmation_token"], "save_payment_method_for_reuse": True,
        "selected_payment_method_type": "card"},
        headers=client._route_headers(route, page_url=_page_url(context)), timeout=client.timeout)
    payload = _json(response)
    status = int(response.status_code)
    if status >= 400:
        terminal = _request_error(payload, status, "payment")
        if terminal:
            context["terminal_result"] = terminal
            return terminal
        return outcome("unknown", "payment", "结账确认的结果待核实，请查询原订单")
    if str(payload.get("status") or "").lower() in {"blocked", "failed", "error"}:
        result = outcome("request_rejected", "payment", redact_sensitive(str(payload.get("message") or "商户未接受本次结账确认"))[:240])
        context["terminal_result"] = result
        return result
    intent_id, secret, _ = payment_intent_from_confirm(payload)
    if not intent_id.startswith(("pi_", "seti_")) or not secret.startswith(intent_id + "_secret_"):
        return outcome("unknown", "payment", "结账确认未返回可核对的 Intent，未重复提交付款")
    context.update(intent_id=intent_id, intent_secret=secret, intent_return_url=_return_url(context, payload))
    return None


def _active_plus(payload: dict) -> bool:
    entitlement = payload.get("entitlement") if isinstance(payload.get("entitlement"), dict) else {}
    plan = str(payload.get("plan_type") or entitlement.get("subscription_plan") or "").lower()
    active = payload.get("is_active") is True or entitlement.get("has_active_subscription") is True
    if payload.get("is_active") is False or entitlement.get("has_active_subscription") is False:
        active = False
    return plan in {"plus", "chatgptplusplan"} and active and payload.get("is_delinquent") is not True and entitlement.get("is_delinquent") is not True


def _paid_checkout(client: ChatGPTClient, context: dict, *, read_only: bool) -> dict | None:
    page = _read_checkout(client, context)
    if page.get("status") != "complete" or page.get("payment_status") not in {"paid", "no_payment_required"}:
        return None
    if page.get("payment_status") == "no_payment_required" and Decimal(str(context["amount"])) != 0:
        return outcome("unknown", "payment", "非零结账未返回已付款状态")
    amount, currency = _checkout_quote(page, context["currency"])
    if amount is None or _minor(amount, currency) != _minor(context["amount"], context["currency"]) or currency.upper() != context["currency"]:
        return outcome("unknown", "payment", "已完成结账的金额或币种与已确认报价不一致，请核对原结果")
    page_intent, _, _ = payment_intent_from_confirm(page)
    if context.get("intent_id") and page_intent and page_intent != context["intent_id"]:
        return outcome("unknown", "payment", "已完成结账引用了不同的付款 Intent")
    if not context.get("ack_started"):
        if read_only:
            return outcome("pending", "checkout", "付款已确认，请显式继续完成订阅登记；不会重新扣款", intent_status="succeeded")
        context["ack_started"] = True
        route = "/backend-api/payments/checkout_session"
        try:
            response = client.http.post(APP_BASE + route, json={"processor_entity": context["processor"],
                "stripe_checkout_session_id": context["checkout_id"]},
                headers=client._route_headers(route, page_url=_page_url(context)), timeout=client.timeout)
            acknowledgment = _json(response)
            context["ack_complete"] = int(response.status_code) < 400 and acknowledgment.get("success") is True
        except Exception:
            context["ack_complete"] = False
    subscription = client.subscription_payload()
    if _active_plus(subscription):
        # An active subscription plus the exact paid checkout reconciles a lost acknowledgment response.
        return outcome("paid", "payment", "原结账已完成，Plus 订阅已生效", intent_status="succeeded")
    return outcome("pending" if context.get("ack_complete") else "unknown", "payment",
                   "付款已确认，等待 Plus 订阅生效" if context.get("ack_complete") else "付款已确认，订阅登记结果待核实；本次未重复登记或扣款",
                   intent_status="succeeded")


def _intent(client: ChatGPTClient, context: dict, *, read_only: bool) -> dict:
    completed = _paid_checkout(client, context, read_only=read_only)
    if completed:
        return completed
    intent_id = context.get("intent_id")
    if not intent_id or not context.get("intent_secret"):
        return outcome("unknown", "payment", "原确认结果尚未核实，本次未重新提交付款")
    intent = client.retrieve_stripe_intent(intent_id=intent_id, client_secret=context["intent_secret"], publishable_key=context["publishable_key"])
    if not intent or intent.get("id") != intent_id:
        return outcome("unknown", "payment", "未读到原付款 Intent，请查询协议状态")
    status = str(intent.get("status") or "")
    customer = _id(intent.get("customer"))
    if context.get("customer_id") and customer and context["customer_id"] != customer:
        return outcome("unknown", "payment", "付款 Customer 与原结账不匹配")
    if customer:
        context["customer_id"] = customer
    pm = _id(intent.get("payment_method"))
    if context.get("payment_method_id") and pm and pm != context["payment_method_id"]:
        return outcome("unknown", "payment", "付款支付方式与原确认令牌不匹配")
    if pm:
        context["payment_method_id"] = pm
    if intent_id.startswith("pi_"):
        raw_amount = intent.get("amount")
        if not isinstance(raw_amount, int) or isinstance(raw_amount, bool) or str(intent.get("currency") or "").upper() != context["currency"]:
            return outcome("unknown", "payment", "付款 Intent 的金额或币种未核实")
        if raw_amount != _minor(context["amount"], context["currency"]):
            return outcome("unknown", "payment", "付款金额与已确认报价不一致，尚未重新确认")
    if status == "requires_confirmation" and not context.get("stripe_confirmation_started"):
        if read_only:
            return outcome("pending", "checkout", "付款 Intent 已准备好，请确认当前金额后继续", intent_status=status)
        context["stripe_confirmation_started"] = True
        try:
            response = client.stripe_http.post(STRIPE_BASE + f"/v1/payment_intents/{intent_id}/confirm" if intent_id.startswith("pi_")
                else STRIPE_BASE + f"/v1/setup_intents/{intent_id}/confirm",
                data=urlencode({"key": context["publishable_key"], "_stripe_version": STRIPE_API_VERSION,
                    "client_secret": context["intent_secret"], "confirmation_token": context["confirmation_token"],
                    "return_url": context["intent_return_url"]}), timeout=client.timeout)
            # Retrieve the intent and checkout after any response; no success is inferred from HTTP 200.
            if int(response.status_code) >= 400:
                payload = _json(response)
                context["stripe_last_response_error"] = _stripe_error(payload)
        except Exception:
            pass
        return _intent(client, context, read_only=read_only)
    if status in {"requires_payment_method", "canceled"}:
        error = _stripe_error(intent)
        return outcome("card_declined", "payment", error.pop("message", "Stripe 未完成本次付款"), intent_status=status, **error)
    if status == "requires_action":
        action = intent.get("next_action") if isinstance(intent.get("next_action"), dict) else {}
        return outcome("challenge_required", "payment", "Stripe 要求继续验证，本次未自动重新确认", intent_status=status,
                       next_action={"type": str(action.get("type") or "requires_action")})
    if status in {"succeeded", "processing", "requires_capture"}:
        return outcome("pending", "payment", "付款处理中，等待原结账和订阅状态确认", intent_status=status)
    return outcome("unknown", "payment", "原付款结果待核实，请查询协议状态", intent_status=status or None)


def run_checkout_protocol(client: ChatGPTClient, context: dict, spec: dict, *, card: dict | None = None,
                          billing: dict | None = None, read_only: bool = False) -> dict:
    checkout_id = str(context.get("checkout_id") or spec.get("checkout_id") or "")
    if har_engine_enabled() and not checkout_id.startswith("cs_"):
        # 2026-10-29 HAR 逐请求复刻（含指纹与遥测）；GSS_PLUS_ENGINE=legacy 回退旧链路。
        # cs_ 形态的 custom checkout 需要先 finalize，仍走旧链路。
        return run_har_context(client, context, spec, card=card, billing=billing, read_only=read_only)
    stage = "checkout"
    terminal = context.get("terminal_result")
    if terminal:
        return dict(terminal)
    try:
        restore_account(client, str(context.get("account_id") or spec.get("account_id") or ""))
        context["account_id"] = client.account_id
        if not context.get("checkout_id"):
            context.update(checkout_id=spec["checkout_id"], checkout_url=spec["checkout_url"],
                publishable_key=spec["publishable_key"], currency=str(spec["currency"]).upper(),
                amount=Decimal(str(spec["amount"])), processor=spec["processor"],
                customer_session_client_secret=spec.get("customer_session_client_secret") or "",
                confirm_return_url=spec.get("confirm_return_url") or "")
        if not str(context["checkout_id"]).startswith("oaics_"):
            return outcome("integration_error", "checkout", "当前 HAR 直付契约仅支持 oaics_ 自定义结账", code="checkout_kind_unsupported")
        if context.get("app_confirmation_started"):
            return _intent(client, context, read_only=read_only)
        original = _read_checkout(client, context)
        if original.get("status") == "complete":
            return _paid_checkout(client, context, read_only=read_only) or outcome("unknown", "payment", "原结账已关闭，付款状态待核实")
        if original.get("status") != "open" or original.get("payment_status") != "unpaid":
            return outcome("request_rejected", "checkout", "原结账不处于可付款状态，请核对原订单")
        if context.get("token_started") and not context.get("confirmation_token"):
            return outcome("unknown", "setup", "原卡片准备结果待核实，本次未重复提交")
        if read_only:
            return outcome("pending", "checkout", "原结账尚未提交付款，状态查询不会准备卡片或确认支付")
        billing = billing or context.get("protocol_billing") or {}
        _sync_quote(client, context, billing)
        if _minor(context["amount"], context["currency"]) != _minor(spec["expected_amount"], spec["expected_currency"]) or context["currency"] != str(spec["expected_currency"]).upper():
            return outcome("pending", "checkout", "账单税费已更新，请确认当前应付金额后再提交付款")
        if not context.get("confirmation_token"):
            stage = "setup"
            _read_elements(client, context)
            stopped = _create_token(client, context, card or {}, billing)
            if stopped:
                return stopped
        stage = "payment"
        stopped = _app_confirm(client, context)
        return stopped or _intent(client, context, read_only=False)
    except DomainError as exc:
        return outcome("unknown", stage, redact_sensitive(exc.message)[:240], code=str(exc.code))
    except Exception:
        return outcome("unknown", stage, "上游连接中断，请查询原订单；本次未自动重试金融确认")
