"""Save a card with user-present Stripe.js, then refresh this account's checkout."""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from fastapi import APIRouter, HTTPException

from gss_contracts import BillingMode, OrderStatus, Plan
from gss_contracts.errors import DomainError, ErrorCode
from gss_core.chatgpt import ChatGPTClient
from gss_core.config import use_fake_adapters
from gss_core.saved_cards import inspect_account_setup, prepare_account_setup, restore_account
from gss_core.vault import vault

from ..store import store
from .events import notify_order_update
from .orders import _error, _from_domain, _team_locks, _to_out

router = APIRouter(prefix="/api/orders", tags=["saved-cards"])
logger = logging.getLogger("gss.saved_cards")


def _load(order_id: str):
    order = store.get_order(order_id)
    if order is None or order.plan is not Plan.PLUS:
        raise HTTPException(404, "Plus 订单不存在")
    if order.status is not OrderStatus.AWAITING_PAYMENT:
        raise HTTPException(409, "支付已提交，请先查询原订单状态")
    secrets = vault.get(order_id)
    if secrets is None:
        raise _error(401, ErrorCode.SESSION_EXPIRED, "订单凭证已过期，请重新创建结账")
    if secrets.saved_card_context and secrets.saved_card_context.get("protocol_flow"):
        raise HTTPException(409, "本订单已进入协议绑卡支付，请使用协议状态查询")
    return order, secrets, store.view(order_id)


def _client(secrets) -> ChatGPTClient:
    client = ChatGPTClient.from_raw_session(secrets.session, proxy=secrets.proxy or None)
    client.timeout = 30.0
    return client


def _prepare_reply(context: dict[str, Any]) -> dict[str, Any]:
    return {key: context[key] for key in ("state", "setup_intent_id", "client_secret", "publishable_key")}


def _inspect(secrets) -> dict[str, Any]:
    client = _client(secrets)
    try:
        return inspect_account_setup(client, secrets.saved_card_context)
    finally:
        client.close()


@router.post("/{order_id}/saved-card/prepare")
async def prepare_saved_card(order_id: str) -> dict:
    async with _team_locks.setdefault(order_id, asyncio.Lock()):
        _, secrets, _ = _load(order_id)
        context = secrets.saved_card_context
        # Only a terminal cancellation verified by the server permits a fresh SetupIntent.
        if context and context.get("state") == "canceled":
            secrets.saved_card_context = None
            secrets.setup_intent_id = ""
            secrets.setup_client_secret = ""
            context = None
        if context and context.get("setup_intent_id") and context.get("client_secret"):
            return _prepare_reply(context)
        if context and context.get("prepare_started"):
            raise HTTPException(409, "绑卡准备请求的结果待确认，请先在账号支付设置中核对；本次未重复创建验证")
        if use_fake_adapters():
            raise HTTPException(409, "离线适配器不提供真实的保存卡片会话")
        context = {"state": "preparing"}
        secrets.saved_card_context = context
        client = _client(secrets)
        try:
            await asyncio.to_thread(prepare_account_setup, client, context, secrets.account_id)
            secrets.account_id = context["account_id"]
            secrets.setup_intent_id = context["setup_intent_id"]
            secrets.setup_client_secret = context["client_secret"]
        except DomainError as exc:
            if not context.get("prepare_started"):
                secrets.saved_card_context = None
            else:
                context["state"] = "prepare_unknown"
            raise _from_domain(exc)
        except Exception:
            if not context.get("prepare_started"):
                secrets.saved_card_context = None
            else:
                context["state"] = "prepare_unknown"
            logger.warning("saved_card_prepare_failed order=%s", order_id)
            raise HTTPException(502, "保存卡片会话准备中断；请先查询状态，本次未提交卡片或付款")
        finally:
            client.close()
        return _prepare_reply(context)


@router.get("/{order_id}/saved-card")
async def saved_card_status(order_id: str) -> dict:
    async with _team_locks.setdefault(order_id, asyncio.Lock()):
        _, secrets, _ = _load(order_id)
        context = secrets.saved_card_context
        if not context:
            return {"state": "not_started"}
        if not context.get("setup_intent_id") or not context.get("client_secret"):
            return {"state": "prepare_unknown", "message": "保存卡片会话的准备结果待确认，本次未重复发送请求"}
        try:
            return await asyncio.to_thread(_inspect, secrets)
        except DomainError as exc:
            raise _from_domain(exc)
        except Exception:
            logger.warning("saved_card_status_failed order=%s", order_id)
            raise HTTPException(502, "读取保存卡片状态中断，请重新查询；本次未提交验证或付款")


@router.post("/{order_id}/saved-card/refresh-checkout")
async def refresh_saved_card_checkout(order_id: str) -> dict:
    async with _team_locks.setdefault(order_id, asyncio.Lock()):
        order, secrets, view = _load(order_id)
        context = secrets.saved_card_context
        if not context or not context.get("setup_intent_id"):
            raise HTTPException(409, "请先保存卡片并查询成功状态")
        try:
            inspected = await asyncio.to_thread(_inspect, secrets)
        except DomainError as exc:
            raise _from_domain(exc)
        except Exception:
            raise HTTPException(502, "读取绑卡结果中断，尚未刷新结账，请重新查询状态")
        if inspected["state"] not in {"saved", "checkout_ready"}:
            raise HTTPException(409, "卡片尚未完成验证或账号同步，请先查询保存状态")
        if context.get("refreshed_checkout_id"):
            if view.checkout_session_id != context["refreshed_checkout_id"]:
                raise HTTPException(409, "结账上下文已发生变化，请重新查询订单")
            return {"order": _to_out(order), "payment_method_id": context["payment_method_id"], "card": context["card"]}
        client = _client(secrets)
        try:
            def _refresh():
                restore_account(client, context["account_id"])
                checkout = client.create_checkout(
                    plan=str(order.plan), country=order.billing_country,
                    promo=order.mode.billing_mode is BillingMode.PROMO,
                    promo_code=view.promo_code or "", promo_campaign_id=view.promo_campaign_id or "",
                )
                if checkout.account_id != context["account_id"]:
                    raise DomainError(ErrorCode.CHECKOUT_UPSTREAM_ERROR, "新结账返回了不同的账号，保留原结账链接")
                if checkout.publishable_key != context["publishable_key"]:
                    raise DomainError(ErrorCode.CHECKOUT_UPSTREAM_ERROR, "新结账与绑卡使用的 Stripe 公钥不同，保留原结账链接")
                if not checkout.checkout_session_id or not checkout.checkout_url:
                    raise DomainError(ErrorCode.CHECKOUT_UPSTREAM_ERROR, "新结账缺少会话或链接，保留原结账链接")
                if checkout.amount is None:
                    raise DomainError(ErrorCode.CHECKOUT_UPSTREAM_ERROR, "新结账金额暂未读取成功，保留原结账链接；可重新提链")
                return checkout
            checkout = await asyncio.to_thread(_refresh)
        except DomainError as exc:
            raise _from_domain(exc)
        except Exception:
            logger.warning("saved_card_checkout_refresh_failed order=%s", order_id)
            raise HTTPException(502, "重新提链中断，卡片保存状态和原链接仍保留；本次未提交付款")
        finally:
            client.close()
        view.checkout_url = checkout.checkout_url
        view.checkout_session_id = checkout.checkout_session_id
        view.publishable_key = checkout.publishable_key
        view.customer_session_client_secret = checkout.customer_session_client_secret or None
        view.currency = checkout.currency
        view.amount = str(checkout.amount) if checkout.amount is not None else None
        view.quote_error = None
        view.checkout_kind = checkout.checkout_kind or "custom"
        view.confirm_return_url = checkout.confirm_return_url or None
        order.amount = checkout.amount
        secrets.checkout_session_id = checkout.checkout_session_id
        secrets.publishable_key = checkout.publishable_key
        context.update(state="checkout_ready", refreshed_checkout_id=checkout.checkout_session_id)
        await notify_order_update(order_id, OrderStatus.AWAITING_PAYMENT, {"payment_method_saved": True, "checkout_refreshed": True})
        logger.info("saved_card_checkout_refreshed order=%s", order_id)
        return {"order": _to_out(order), "payment_method_id": context["payment_method_id"], "card": context["card"]}
