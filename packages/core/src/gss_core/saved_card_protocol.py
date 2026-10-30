"""Account-scoped HTTP card setup and payment, with explicit mutation boundaries."""
from __future__ import annotations

from decimal import Decimal
from typing import Any
from urllib.parse import urlencode

from gss_contracts.errors import DomainError, ErrorCode
from .chatgpt import (
    STRIPE_BASE, ChatGPTClient, CheckoutAmountChanged, checkout_confirm_blocked, checkout_confirm_message,
    payment_intent_from_confirm, redact_sensitive, stripe_verify_url, _processor_from_checkout_url,
)
from .saved_cards import inspect_account_setup, prepare_account_setup, restore_account
from .money import _ZERO_DECIMAL


def _factor(currency: str) -> Decimal:
    return Decimal(1 if currency.upper() in _ZERO_DECIMAL else 100)


def _minor(amount, currency: str) -> int:
    value = Decimal(str(amount)) * _factor(currency)
    if not value.is_finite() or value < 0 or value != value.to_integral_value():
        raise DomainError(ErrorCode.CHECKOUT_UPSTREAM_ERROR, "订单金额格式未核实")
    return int(value)


def _id(value) -> str:
    return str(value.get("id") or "") if isinstance(value, dict) else str(value or "")


def outcome(state: str, stage: str, message: str, **fields) -> dict[str, Any]:
    return {"state": state, "stage": stage, "message": message, **fields}


def _stripe_error(payload: dict) -> dict[str, str]:
    error = payload.get("error") or payload.get("last_payment_error") or payload.get("last_setup_error") or {}
    if not isinstance(error, dict):
        return {}
    return {name: redact_sensitive(str(error.get(name) or ""))[:240] for name in ("code", "decline_code", "message") if error.get(name)}


def _setup_rejection(payload: dict, status: int) -> dict:
    error = _stripe_error(payload)
    raw_error = payload.get("error") if isinstance(payload.get("error"), dict) else {}
    message = redact_sensitive(str(raw_error.get("message") or error.get("message") or "Stripe 未接受保存支付方式请求"))[:2000]
    unsupported = "integration surface is unsupported for publishable key tokenization" in message.lower()
    if unsupported:
        result = outcome("integration_error", "setup",
            "Stripe 未启用当前商户公钥的服务端卡号采集接口。尚未完成绑卡或提交订阅付款。",
            code="stripe_publishable_key_tokenization_unsupported",
            remediation="接口开关由当前公钥所属的 Stripe 商户账号控制；修改本地账单、请求头或重复提交不会启用该接口。",
            documentation_url="https://support.stripe.com/questions/card-tokenization-restrictions-using-publishable-keys")
    else:
        result = outcome("card_declined" if status == 402 else "request_rejected", "setup", message,
                         code=error.get("code") or "stripe_setup_rejected")
    result.update(upstream_message=message, upstream_status=status)
    if error.get("decline_code"):
        result["decline_code"] = error["decline_code"]
    if raw_error.get("code"):
        result["upstream_code"] = redact_sensitive(str(raw_error["code"]))[:120]
    return result


def _confirm_setup_inline(client: ChatGPTClient, context: dict, card: dict, billing: dict) -> None:
    # Persist the write boundary before sending: an ambiguous result is GET-only.
    context["protocol_setup_method"] = "inline_payment_method_data"
    context["protocol_setup_confirmation_started"] = True
    data = {"key": context["publishable_key"], "client_secret": context["client_secret"],
            "payment_method_data[type]": "card", "payment_method_data[allow_redisplay]": "always"}
    for key in ("number", "cvc", "exp_month", "exp_year"):
        data[f"payment_method_data[card][{key}]"] = str(card.get(key) or "")
    for key in ("name", "email"):
        data[f"payment_method_data[billing_details][{key}]"] = billing.get(key, "")
    for key in ("line1", "city", "state", "postal_code", "country"):
        data[f"payment_method_data[billing_details][address][{key}]"] = billing.get(key, "")
    response = client.stripe_http.post(
        f"{STRIPE_BASE}/v1/setup_intents/{context['setup_intent_id']}/confirm",
        data=urlencode(data), timeout=client.timeout,
    )
    status = int(response.status_code)
    payload = response.json()
    if not isinstance(payload, dict):
        raise DomainError(ErrorCode.CHECKOUT_UPSTREAM_ERROR, "Stripe 保存支付方式响应格式未核实，请查询原结果")
    if status >= 400:
        if 400 <= status < 500 and status not in {408, 409, 429}:
            context["protocol_setup_result"] = _setup_rejection(payload, status)
        # 5xx, rate limits, and request conflicts have no trusted terminal result.
        return
    if payload.get("id") == context["setup_intent_id"]:
        pm = _id(payload.get("payment_method"))
        if pm.startswith("pm_"):
            context["protocol_payment_method_id"] = pm
    # Confirmation itself is not proof of saving: follow with Stripe GET and account list.


def _setup(client: ChatGPTClient, context: dict, card: dict | None, billing: dict, read_only: bool) -> dict | None:
    checked = inspect_account_setup(client, context)
    state = checked["state"]
    if state in {"saved", "checkout_ready"}:
        expected_pm = context.get("protocol_payment_method_id")
        if expected_pm and checked.get("payment_method_id") != expected_pm:
            raise DomainError(ErrorCode.CHECKOUT_UPSTREAM_ERROR, "保存结果与本订单提交的支付方式不匹配")
        return None
    if state == "sync_pending":
        return outcome("pending", "sync", checked["message"], intent_status="succeeded")
    if state == "requires_action":
        return outcome("challenge_required", "setup", "保存支付方式需要上游验证，尚未提交订阅付款", intent_status=state)
    if state == "processing":
        return outcome("pending", "setup", "支付方式验证正在处理，请查询协议状态", intent_status=state)
    if state == "canceled":
        return outcome("card_declined", "setup", "本次支付方式验证已取消，尚未提交订阅付款", intent_status=state)
    if context.get("protocol_setup_confirmation_started"):
        setup_error = checked.get("setup_error")
        if state == "requires_payment_method" and setup_error:
            result = outcome("card_declined" if setup_error.get("decline_code") or setup_error.get("code") == "card_declined" else "request_rejected",
                             "setup", setup_error.get("message") or "Stripe 未接受本次保存支付方式请求",
                             code=setup_error.get("code") or "stripe_setup_rejected", intent_status=state)
            if setup_error.get("decline_code"):
                result["decline_code"] = setup_error["decline_code"]
            context["protocol_setup_result"] = result
            return result
        if context.get("protocol_setup_error"):
            return outcome("card_declined" if context.get("protocol_setup_error_code") == str(ErrorCode.PAYMENT_DECLINED) else "unknown", "setup", context["protocol_setup_error"], intent_status=state)
        return outcome("unknown", "setup", "原绑卡确认结果尚未核实，本次未重复提交", intent_status=state)
    pm = context.get("protocol_payment_method_id")
    if not pm and context.get("protocol_tokenization_started"):
        return outcome("unknown", "setup", "原支付方式创建结果待核实，本次未重复提交")
    if read_only:
        return outcome("pending", "setup", "支付方式尚未保存；状态查询不会重新提交绑卡", intent_status=state)
    try:
        if pm:
            # Resume an older order that already owns a verified PaymentMethod.
            context["protocol_setup_confirmation_started"] = True
            client.confirm_setup_intent(setup_id=context["setup_intent_id"], client_secret=context["client_secret"],
                                        payment_method=pm, publishable_key=context["publishable_key"])
        else:
            _confirm_setup_inline(client, context, card or {}, billing)
    except DomainError as exc:
        context["protocol_setup_error"] = redact_sensitive(exc.message)[:240]
        context["protocol_setup_error_code"] = str(exc.code)
    except Exception:
        # Reconcile an ambiguous response by GET, never by a second confirmation.
        pass
    if context.get("protocol_setup_result"):
        return dict(context["protocol_setup_result"])
    return _setup(client, context, card, billing, True)


def _refresh(client: ChatGPTClient, context: dict, spec: dict, billing: dict):
    checkout = client.create_checkout(
        plan="plus", country=spec["country"], promo=spec["promo"],
        promo_code=spec.get("promo_code", ""), promo_campaign_id=spec.get("promo_campaign_id", ""),
    )
    if checkout.account_id != context["account_id"] or checkout.publishable_key != context["publishable_key"]:
        raise DomainError(ErrorCode.CHECKOUT_UPSTREAM_ERROR, "新结账的账号或 Stripe 公钥与绑卡上下文不匹配")
    if not checkout.checkout_session_id or not checkout.checkout_url or checkout.amount is None:
        raise DomainError(ErrorCode.CHECKOUT_UPSTREAM_ERROR, "新结账会话或报价未完整读取，保留原链接，请重新确认")
    minor = client._sync_billing_tax(session_id=checkout.checkout_session_id, page_url=checkout.checkout_url,
                                     billing=billing, processor=_processor_from_checkout_url(checkout.checkout_url),
                                     currency=checkout.currency, strict=True)
    if not isinstance(minor, int) or isinstance(minor, bool) or minor < 0:
        raise DomainError(ErrorCode.CHECKOUT_UPSTREAM_ERROR, "未读到真实账单的最终税费报价，保留原链接")
    checkout.amount = Decimal(minor) / _factor(checkout.currency)
    context["protocol_checkout"] = checkout
    context["refreshed_checkout_id"] = checkout.checkout_session_id
    context["state"] = "checkout_ready"
    return checkout


def _intent_result(client: ChatGPTClient, context: dict, *, read_only: bool) -> dict:
    payment = context["protocol_payment"]
    intent_id, secret = payment.get("intent_id"), payment.get("client_secret")
    if not intent_id or not secret:
        return outcome("unknown", "payment", "原结账确认的结果待核实，本次未重复提交付款")
    intent = client.retrieve_stripe_intent(intent_id=intent_id, client_secret=secret, publishable_key=context["publishable_key"])
    if not isinstance(intent, dict) or intent.get("id") != intent_id:
        return outcome("unknown", "payment", "未读到可确认的 Stripe 状态，请查询协议状态")
    status = str(intent.get("status") or "")
    if _id(intent.get("customer")) != context.get("customer_id"):
        return outcome("unknown", "payment", "支付 Intent 的 Customer 与已保存卡片不匹配")
    pm = _id(intent.get("payment_method"))
    if (pm and pm != context["payment_method_id"]) or (status in {"succeeded", "requires_action", "processing"} and pm != context["payment_method_id"]):
        return outcome("unknown", "payment", "支付 Intent 的支付方式与本订单不匹配")
    checkout = context["protocol_checkout"]
    if intent_id.startswith("pi_"):
        actual = intent.get("amount")
        if not isinstance(actual, int) or isinstance(actual, bool) or actual < 0 or str(intent.get("currency") or "").upper() != checkout.currency.upper():
            return outcome("unknown", "payment", "支付 Intent 的金额或币种未核实")
        if actual != _minor(checkout.amount, checkout.currency):
            if status == "requires_confirmation" and not payment.get("stripe_confirmation_started"):
                checkout.amount = Decimal(actual) / _factor(checkout.currency)
                return outcome("pending", "checkout", "Stripe 最终金额已更新，请确认新报价后再提交付款")
            return outcome("unknown", "payment", "支付记录金额与已确认报价不一致，请先核对原结果")
    if status == "requires_confirmation" and read_only and not payment.get("stripe_confirmation_started"):
        return outcome("pending", "checkout", "支付 Intent 尚待确认，请核对当前金额后显式提交付款", intent_status=status)
    if status == "requires_confirmation" and not read_only and not payment.get("stripe_confirmation_started"):
        payment["stripe_confirmation_started"] = True
        try:
            client.confirm_stripe_intent(
                intent_id=intent_id, client_secret=secret, publishable_key=context["publishable_key"],
                payment_method=context["payment_method_id"], return_url=stripe_verify_url(context["refreshed_checkout_id"]),
            )
        except Exception:
            pass
        return _intent_result(client, context, read_only=True)
    details = _stripe_error(intent)
    if status == "succeeded":
        if intent_id.startswith("seti_"):
            checkout = context["protocol_checkout"]
            if Decimal(str(checkout.amount)) != 0:
                return outcome("pending", "payment", "SetupIntent 已完成，尚未获得订阅付款成功结果", intent_status=status)
            subscription = client.subscription_payload()
            plan = str(subscription.get("plan_type") or (subscription.get("subscription") or {}).get("plan") or "").lower()
            sub = subscription.get("subscription") if isinstance(subscription.get("subscription"), dict) else {}
            active_status = str(sub.get("status") or subscription.get("status") or "").lower()
            if (plan not in {"plus", "chatgptplusplan"} or active_status not in {"active", "trialing"}
                    or subscription.get("active") is False or sub.get("active") is False):
                return outcome("pending", "payment", "零金额结账已确认，等待 Plus 订阅生效", intent_status=status)
        return outcome("paid", "payment", "Stripe 已确认结账完成", intent_status=status,
                       charge_id=intent.get("latest_charge") if isinstance(intent.get("latest_charge"), str) else None)
    if status in {"requires_payment_method", "canceled"}:
        return outcome("card_declined", "payment", details.pop("message", "Stripe 未完成本次付款"), intent_status=status, **details)
    if status == "requires_action":
        action = intent.get("next_action") if isinstance(intent.get("next_action"), dict) else {}
        return outcome("challenge_required", "payment", "Stripe 要求继续验证，本次未重复提交付款", intent_status=status,
                       next_action={"type": str(action.get("type") or "requires_action")})
    if status in {"processing", "requires_capture"}:
        return outcome("pending", "payment", "Stripe 正在处理，请查询协议状态", intent_status=status)
    return outcome("unknown", "payment", "Stripe 尚未返回已完成状态，请查询协议状态", intent_status=status or None)


def _payment(client: ChatGPTClient, context: dict, billing: dict, *, read_only: bool) -> dict:
    payment = context.get("protocol_payment")
    if payment:
        return _intent_result(client, context, read_only=read_only)
    if read_only:
        return outcome("pending", "payment", "卡片和结账已准备好，尚未提交付款")
    checkout = context["protocol_checkout"]
    payment = {"preparing": True}
    claimed = context.setdefault("protocol_payment", payment)
    if claimed is not payment:
        return _intent_result(client, context, read_only=True)
    try:
        token = client.create_confirmation_token(publishable_key=context["publishable_key"], payment_method=context["payment_method_id"], setup_future_usage="off_session")
        payment.update(preparing=False, app_confirmation_started=True)
        confirmed = client.confirm_checkout(
            checkout_session_id=checkout.checkout_session_id, confirm_token=token, checkout_url=checkout.checkout_url,
            billing=billing, currency=checkout.currency, publishable_key=context["publishable_key"], save_choice=True,
            expected_amount_minor=_minor(checkout.amount, checkout.currency),
        )
        if checkout.checkout_session_id.startswith("cs_"):
            confirmed = client.finalize_stripe_custom_checkout(
                checkout_session_id=checkout.checkout_session_id, publishable_key=context["publishable_key"], checkout_url=checkout.checkout_url,
                processor_entity=_processor_from_checkout_url(checkout.checkout_url), confirmed=confirmed,
            )
        if checkout_confirm_blocked(confirmed):
            result = outcome("card_declined", "payment", checkout_confirm_message(confirmed) or "结账确认未获批准")
            payment["terminal_result"] = result
            return result
        intent_id, secret, _ = payment_intent_from_confirm(confirmed)
        payment.update(intent_id=intent_id, client_secret=secret)
    except CheckoutAmountChanged as exc:
        if exc.currency.upper() != checkout.currency.upper():
            return outcome("unknown", "payment", "最终报价币种变化，尚未提交确认")
        checkout.amount = Decimal(exc.amount_minor) / _factor(checkout.currency)
        if context.get("protocol_payment") is payment:
            context.pop("protocol_payment")
        return outcome("pending", "checkout", "最终税费报价已更新，请确认新金额后再提交付款")
    except DomainError as exc:
        return outcome("unknown", "payment", redact_sensitive(exc.message)[:240])
    except Exception:
        return outcome("unknown", "payment", "结账确认连接中断，结果待核实；请查询协议状态")
    return _intent_result(client, context, read_only=False)


def run_saved_card_protocol(client: ChatGPTClient, context: dict, spec: dict, *, card: dict | None = None,
                            billing: dict | None = None, read_only: bool = False) -> dict:
    stage = "setup"
    terminal_setup = context.get("protocol_setup_result") or context.get("protocol_tokenization_result")
    if terminal_setup:
        return dict(terminal_setup)
    try:
        restore_account(client, str(context.get("account_id") or spec.get("account_id") or ""))
        if not context.get("setup_intent_id"):
            if context.get("prepare_started"):
                return outcome("unknown", stage, "原保存会话的创建结果待核实，本次未重复请求")
            if read_only:
                return outcome("pending", stage, "协议流程尚未取得保存卡片会话")
            prepare_account_setup(client, context, str(spec.get("account_id") or ""))
        billing = billing or context.get("protocol_billing") or {}
        stopped = _setup(client, context, card, billing, read_only)
        if stopped:
            return stopped
        stage = "checkout"
        checkout = context.get("protocol_checkout")
        if not checkout:
            if read_only:
                return outcome("pending", stage, "支付方式已保存，尚未重新提链")
            checkout = _refresh(client, context, spec, billing)
        if read_only and not context.get("protocol_payment"):
            return outcome("pending", stage, "新结账已准备好，尚未提交付款")
        if not read_only and (Decimal(str(checkout.amount)) != Decimal(str(spec["expected_amount"]))
                              or str(checkout.currency).upper() != str(spec["expected_currency"]).upper()):
            return outcome("pending", stage, "新报价与提交前不同，请确认更新后的金额再提交付款")
        stage = "payment"
        terminal = (context.get("protocol_payment") or {}).get("terminal_result")
        return terminal or _payment(client, context, billing, read_only=read_only)
    except DomainError as exc:
        state = "card_declined" if exc.code is ErrorCode.PAYMENT_DECLINED else "unknown"
        return outcome(state, stage, redact_sensitive(exc.message)[:240], code=str(exc.code))
    except Exception:
        return outcome("unknown", stage, "上游请求中断，请查询协议状态；本次未自动重试确认")
