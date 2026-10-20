"""卡支付。浏览器只提交 PaymentMethod 标识，本模块向 Stripe / ChatGPT 确认。"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from gss_contracts import AdapterCapabilities, PaymentRequest, PaymentResult, PaymentStatus
from gss_contracts.errors import DomainError, ErrorCode
from gss_core.billing import address_for
from gss_core.hosted import TeamHostedClient
from gss_core.chatgpt import (
    APP_BASE,
    ChatGPTClient,
    checkout_confirm_blocked,
    checkout_confirm_blocked_advice,
    checkout_confirm_message,
    checkout_requires_approval,
    decline_message,
    nested_intent_status,
    payment_intent_from_confirm,
    public_checkout_id,
    redact_sensitive,
    stripe_verify_url,
    submission_failure_detail,
    submission_state,
    _processor_from_checkout_url,
)
from gss_core.config import load_settings
from gss_core.countries import currency_for, locale_for, timezone_for
from gss_core.proxies import align_proxy_region, configured_proxy, session_proxy


class PaymentAdapterBase:
    def capabilities(self) -> AdapterCapabilities:
        return AdapterCapabilities(
            source="self-contained chatgpt.com + api.stripe.com",
            license="MIT (this repository)",
            supports={
                "providers": ["direct_card"],
                "card_sources": ["customer"],
                "proxy_roles": ["checkout", "approve"],
            },
            failure_modes={
            "payment_declined": "卡片被拒或 ChatGPT 风控 blocked → 换卡 / 整段 session 后重试",
            "requires_action": "3DS → 在浏览器完成验证",
            "payment_unknown_state": "终态不确定 → 先查订阅，避免重复扣款",
            },
        )


logger = logging.getLogger("gss.payment")

_APPROVAL_RETRIES = 1


def _approval_retryable(confirmed: Dict[str, Any]) -> bool:
    """ChatGPT 审批内部错误且没有 PaymentIntent（不可能已扣款）时才允许重试一次。"""
    if submission_state(confirmed) != "failed":
        return False
    if "checkout_approval_internal_error" not in submission_failure_detail(confirmed):
        return False
    intent_id, _, _ = payment_intent_from_confirm(confirmed)
    return not intent_id


@dataclass
class UpstreamPaymentAdapter(PaymentAdapterBase):
    timeout: int = 120

    async def pay(self, req: PaymentRequest) -> PaymentResult:
        if not req.payment_method_token and not req.card_id:
            raise DomainError(
                ErrorCode.PAYMENT_DECLINED,
                "缺少支付方式：需要浏览器端 payment_method_token",
            )
        if not req.session:
            raise DomainError(ErrorCode.INVALID_SESSION, "支付阶段缺少 session")
        checkout_id = req.checkout_session_id or ""
        if not checkout_id:
            raise DomainError(ErrorCode.PAYMENT_DECLINED, "缺少 checkout_session_id")

        # The exit must match the billing country of the payment method. A checkout
        # created for one region but billed to another otherwise looks inconsistent to
        # Stripe and forces an extra verification step.
        billing_country = str((req.billing or {}).get("country") or "").upper()
        target_country = billing_country if str(req.plan or "").lower() == "team" and billing_country else req.country
        proxy = align_proxy_region(
            req.checkout_proxy
            or req.approve_proxy
            or session_proxy("checkout", target_country)
            or configured_proxy("checkout")
            or configured_proxy("approve"),
            target_country,
        )
        client = ChatGPTClient.from_raw_session(req.session, proxy=proxy)
        client.timeout = float(self.timeout)
        client.language = locale_for(target_country)
        client.timezone = timezone_for(target_country)
        if str(req.plan or "").lower() == "team" and public_checkout_id(checkout_id).startswith("cs_"):
            try:
                return await asyncio.to_thread(self._pay_team, client, req)
            finally:
                client.close()
        page_url = req.checkout_url if str(req.checkout_url or "").startswith(APP_BASE) else f"{APP_BASE}/"
        # 支付阶段出口同样已对齐账单国：先刷新 session 再 warmup，
        # 让确认请求的 cookie/凭证与本批次出口同批次。软失败不阻断。
        client.refresh_session()
        client.warmup(page_url=page_url)
        pk = req.publishable_key or ""
        if not pk:
            raise DomainError(
                ErrorCode.PAYMENT_UNKNOWN_STATE,
                "缺少 Stripe 公钥。若是 Team 长链订单，请打开长链手动支付后查询订阅。",
            )

        billing = dict(req.billing or address_for(req.country))
        checkout_id = public_checkout_id(checkout_id)
        stripe_custom = checkout_id.startswith("cs_")
        for attempt in range(_APPROVAL_RETRIES + 1):
            confirm_token = _confirmation_token(client, publishable_key=pk, token=req.payment_method_token or "")
            confirmed = client.confirm_checkout(
                checkout_session_id=checkout_id, confirm_token=confirm_token, checkout_url=page_url,
                billing=billing, currency=currency_for(req.country), publishable_key=pk,
            )
            if stripe_custom:
                confirmed = client.finalize_stripe_custom_checkout(
                    checkout_session_id=checkout_id, publishable_key=pk, checkout_url=page_url,
                    processor_entity=_processor_from_checkout_url(page_url), confirmed=confirmed,
                )
            if attempt < _APPROVAL_RETRIES and _approval_retryable(confirmed):
                logger.warning("checkout_approval_internal_error 无 PaymentIntent，重试 %s/%s", attempt + 1, _APPROVAL_RETRIES)
                continue
            break
        intent_id, intent_secret, return_url = payment_intent_from_confirm(confirmed)
        chatgpt_status = str(confirmed.get("status") or confirmed.get("result") or "")
        kind = str(confirmed.get("type") or "")
        if checkout_confirm_blocked(confirmed):
            extra = checkout_confirm_message(confirmed)
            advice = checkout_confirm_blocked_advice(
                req.country, upstream_reason=extra, upstream_status=chatgpt_status,
            )
            return PaymentResult(
                status=PaymentStatus.DECLINED,
                provider_ref=intent_id or checkout_id,
                message=advice,
            )
        state = submission_state(confirmed)
        if state in {"failed", "declined", "canceled", "cancelled"}:
            extra = checkout_confirm_message(confirmed) or state
            detail = submission_failure_detail(confirmed)
            if detail:
                extra = f"{extra} ({detail})"
            return PaymentResult(
                status=PaymentStatus.DECLINED,
                provider_ref=intent_id or checkout_id,
                message=f"Stripe 未完成确认: {extra}",
            )
        pi_status = nested_intent_status(confirmed)
        nested = confirmed.get("payment_intent")
        nested = nested if isinstance(nested, dict) else {}
        if pi_status in {"requires_payment_method", "canceled", "cancelled"} or nested.get("last_payment_error"):
            return _result_from_intent(nested or {"status": pi_status or "requires_payment_method"}, intent_id)
        if intent_secret.startswith("pi_") or kind == "payment_intent" or intent_id.startswith("pi_"):
            if pi_status == "requires_action" and intent_secret:
                live = client.retrieve_stripe_intent(
                    intent_id=intent_id,
                    client_secret=intent_secret,
                    publishable_key=pk,
                )
                live_status = str(live.get("status") or "")
                if live_status == "succeeded":
                    return _result_from_intent(live, intent_id)
                if live:
                    return _result_from_intent(live, intent_id, intent_secret)
            if pi_status in {"succeeded", "processing", "requires_action"}:
                return _result_from_intent(
                    nested if nested else {"status": pi_status},
                    intent_id,
                    intent_secret,
                )
            if intent_secret:
                if not return_url:
                    return_url = stripe_verify_url(
                        checkout_id,
                        processor=_processor_from_checkout_url(page_url),
                        plan=str(req.plan or ""),
                    )
                final = client.confirm_stripe_intent(
                    intent_id=intent_id,
                    client_secret=intent_secret,
                    publishable_key=pk,
                    confirmation_token=confirm_token,
                    return_url=return_url,
                )
                return _result_from_intent(final, intent_id, intent_secret)
        if chatgpt_status in {"succeeded", "success", "complete", "completed"} or state in {
            "succeeded",
            "complete",
            "completed",
        }:
            return PaymentResult(
                status=PaymentStatus.SUCCEEDED,
                provider_ref=intent_id or checkout_id,
                message="支付成功",
            )
        if stripe_custom and (chatgpt_status in {"open", ""} or state in {"requires_approval", "processing", ""}):
            return PaymentResult(
                status=PaymentStatus.PROCESSING,
                provider_ref=intent_id or checkout_id,
                message="支付已提交，正在等待 Stripe 完成确认。请稍后刷新订单，不要立刻再点确认。",
            )
        return PaymentResult(
            status=PaymentStatus.ERROR,
            provider_ref=intent_id or checkout_id,
            message=f"未知支付状态: {chatgpt_status or state or 'empty'}",
        )


    def _pay_team(self, client: ChatGPTClient, req: PaymentRequest) -> PaymentResult:
        if not req.publishable_key:
            raise DomainError(ErrorCode.PAYMENT_UNKNOWN_STATE, "缺少 Stripe 公钥，请打开官方长链")
        hosted = TeamHostedClient(client)
        context = req.team_context
        if not context:
            raise DomainError(ErrorCode.PAYMENT_UNKNOWN_STATE, "请先初始化 Team Hosted Checkout")
        confirmed = hosted.confirm(context, req.payment_method_token or "",
                                   passive_captcha_token=req.passive_captcha_token or "",
                                   rv_timestamp=req.rv_timestamp or "")
        result = hosted_payment_result(confirmed, context["session_id"], req.checkout_url)
        # Solving takes 30-120s, longer than the gateway allows. Hand the challenge
        # back now and let the order poll endpoint complete it in the background.
        if result.status is PaymentStatus.REQUIRES_ACTION:
            pending = self._challenge_payload(confirmed)
            if pending:
                import dataclasses
                result = dataclasses.replace(result, next_action={**(result.next_action or {}), **pending})
        if result.status is PaymentStatus.PROCESSING:
            polled = hosted.poll(session_id=context["session_id"], key=context["publishable_key"])
            result = hosted_payment_result(polled, context["session_id"], req.checkout_url)
        return result


    @staticmethod
    def _challenge_payload(confirmed: dict) -> dict:
        from gss_core.captcha import challenge_from_intent
        from gss_core.chatgpt import _intent_object
        challenge = challenge_from_intent(_intent_object(confirmed))
        return dict(challenge) if challenge else {}

    def _solve_intent_challenge(self, client: ChatGPTClient, confirmed: dict,
                                context: dict, req: PaymentRequest):
        """Solve Stripe's intent_confirmation_challenge and re-read the intent."""
        from gss_contracts import PaymentResult, PaymentStatus
        from gss_core.captcha import challenge_from_intent, solve_hcaptcha, verify_challenge
        from gss_core.chatgpt import _intent_object, payment_intent_from_confirm
        intent = _intent_object(confirmed)
        challenge = challenge_from_intent(intent)
        if not challenge:
            return None
        settings = load_settings()
        if not settings.captcha_api_key:
            logger.warning("challenge_present captcha_key_missing challenge=stripe")
            return None
        intent_id, client_secret, _ = payment_intent_from_confirm(confirmed)
        if not intent_id or not client_secret:
            return None
        logger.info("challenge_solving site_key_present=%s rqdata_present=%s",
                    bool(challenge.get("site_key")), bool(challenge.get("rqdata")))
        try:
            token = solve_hcaptcha(client.stripe_http, challenge, api_key=settings.captcha_api_key,
                                   api_url=settings.captcha_api_url)
            verified = verify_challenge(client.stripe_http, intent_id=intent_id, client_secret=client_secret,
                                        token=token, publishable_key=req.publishable_key or "",
                                        api_version="2026-03-25.dahlia")
        except DomainError as exc:
            logger.warning("challenge_failed code=%s error=%s", exc.code.value,
                           redact_sensitive(exc.message)[:200])
            return None
        if str(verified.get("status") or "") == "succeeded":
            return PaymentResult(status=PaymentStatus.SUCCEEDED, provider_ref=intent_id, message="支付成功")
        if verified.get("last_payment_error"):
            return _result_from_intent(verified, intent_id, client_secret)
        return PaymentResult(status=PaymentStatus.PROCESSING, provider_ref=intent_id,
                             message="验证通过，等待 Stripe 确认扣款")


def hosted_payment_result(payload: dict, session_id: str, checkout_url: str = ""):
    from gss_contracts import PaymentResult, PaymentStatus
    from gss_core.chatgpt import _intent_object, payment_intent_from_confirm
    object_status = str(payload.get("payment_object_status") or "").lower()
    state = str(payload.get("state") or "").lower()
    status = str(payload.get("status") or "").lower()
    intent = _intent_object(payload)
    intent_id, secret, _ = payment_intent_from_confirm(payload)
    if object_status == "succeeded" or state == "succeeded" or intent.get("status") == "succeeded" or payload.get("payment_status") == "paid":
        return PaymentResult(status=PaymentStatus.SUCCEEDED, provider_ref=intent_id or session_id, message="支付成功")
    if intent.get("status") == "requires_action":
        next_action = intent.get("next_action") or {}
        sdk = next_action.get("use_stripe_sdk") or {}
        kind = str(sdk.get("type") or next_action.get("type") or "")
        result = _result_from_intent(intent, intent_id, secret)
        action = dict(result.next_action or {})
        action["challenge_type"] = kind
        action["checkout_url"] = checkout_url
        # Needed later by the poll endpoint to solve and verify the challenge.
        action["payment_intent_id"] = intent_id
        action["client_secret"] = secret or action.get("client_secret")
        return PaymentResult(status=PaymentStatus.REQUIRES_ACTION, provider_ref=intent_id or session_id,
                             next_action=action, message="需要完成 Stripe 验证" if kind == "intent_confirmation_challenge" else "需要完成支付验证")
    if object_status in {"failed", "canceled", "expired"} or state in {"failed", "canceled", "expired"} or status == "expired":
        return PaymentResult(status=PaymentStatus.DECLINED, provider_ref=intent_id or session_id, message="Hosted Checkout 支付未完成")
    if intent.get("status") in {"requires_payment_method", "canceled"} or intent.get("last_payment_error"):
        return _result_from_intent(intent, intent_id, secret)
    return PaymentResult(status=PaymentStatus.PROCESSING, provider_ref=intent_id or session_id, message="Stripe 正在处理，请等待状态确认")


def _confirmation_token(client: ChatGPTClient, *, publishable_key: str, token: str) -> str:
    text = str(token or "").strip()
    if text.startswith("ctoken_"):
        return text
    if text.startswith("pm_"):
        return client.create_confirmation_token(publishable_key=publishable_key, payment_method=text)
    raise DomainError(ErrorCode.PAYMENT_DECLINED, "支付标识无效")


def _result_from_intent(payload: Dict[str, Any], intent_id: str, client_secret: str = "") -> PaymentResult:
    status = str(payload.get("status") or "")
    if status == "requires_action":
        next_action = payload.get("next_action") if isinstance(payload.get("next_action"), dict) else {}
        url = (next_action or {}).get("redirect_to_url", {}).get("url") or (next_action or {}).get("url")
        secret = str(payload.get("client_secret") or client_secret or "")
        return PaymentResult(
            status=PaymentStatus.REQUIRES_ACTION,
            # 有跳转链接就直接给链接；3DS2（use_stripe_sdk）没有链接，
            # 需要浏览器用 PI 的 client_secret 调 stripe.handleNextAction 弹出验证窗口。
            next_action={
                "type": "3ds_redirect" if url else "use_stripe_sdk",
                "url": url,
                "client_secret": secret or None,
            },
            provider_ref=intent_id,
            message="需要完成 3DS 验证",
        )
    if status == "succeeded":
        return PaymentResult(status=PaymentStatus.SUCCEEDED, provider_ref=intent_id, message="支付成功")
    if status in {"processing", "pending"}:
        return PaymentResult(status=PaymentStatus.PROCESSING, provider_ref=intent_id, message="支付处理中")
    error = payload.get("last_payment_error") if isinstance(payload.get("last_payment_error"), dict) else {}
    if status in {"requires_payment_method", "canceled", "cancelled"} or error:
        detail = str(error.get("decline_code") or error.get("code") or error.get("message") or status)
        return PaymentResult(
            status=PaymentStatus.DECLINED,
            provider_ref=intent_id,
            message=decline_message(detail) if error else f"支付未完成: {status}",
        )
    return PaymentResult(
        status=PaymentStatus.ERROR,
        provider_ref=intent_id,
        message=f"未知支付状态: {status or 'empty'}",
    )


@dataclass
class FakePaymentAdapter(PaymentAdapterBase):
    scripted: List[PaymentStatus] = field(default_factory=lambda: [PaymentStatus.SUCCEEDED])
    calls: List[PaymentRequest] = field(default_factory=list)

    async def pay(self, req: PaymentRequest) -> PaymentResult:
        self.calls.append(req)
        if not self.scripted:
            raise AssertionError("FakePaymentAdapter 脚本已耗尽")
        status = self.scripted.pop(0)
        next_action: Optional[Dict[str, Any]] = None
        if status is PaymentStatus.REQUIRES_ACTION:
            next_action = {"type": "3ds_redirect", "url": "https://3ds.example.invalid/verify"}
        return PaymentResult(
            status=status,
            next_action=next_action,
            provider_ref="fake-payment-ref",
            message=f"scripted:{status}",
        )


__all__ = ["PaymentAdapterBase", "UpstreamPaymentAdapter", "FakePaymentAdapter"]
