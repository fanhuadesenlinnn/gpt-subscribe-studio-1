"""Saved-card setup and checkout refresh contracts; all upstream calls are fake."""
import asyncio
import json
import threading

import httpx
from decimal import Decimal
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from gss_api.main import create_app
from gss_api.store import store
from gss_contracts import OrderStatus, SubscriptionInfo
from gss_core.vault import vault


@pytest.fixture
def flow(monkeypatch):
    monkeypatch.setenv("GSS_USE_FAKE", "1")
    store.clear()
    api = TestClient(create_app())
    created = api.post("/api/orders", json={"plan": "plus", "billing_country": "US", "session": "fixture-session"})
    assert created.status_code == 201, created.text
    oid = created.json()["id"]
    monkeypatch.setenv("GSS_USE_FAKE", "0")
    state = {
        "account": "account_fixture", "key": "pk_test_saved", "setup_status": "requires_payment_method",
        "intent_id": "seti_fixture", "customer": "cus_fixture", "method_customer": "cus_fixture",
        "method_id": "pm_fixture", "visible": True, "closed": 0, "calls": [],
    }

    class Client:
        timeout = 30.0
        def __init__(self):
            self.account_id = state["account"]
            self.access_token = "fixture-original-token"
            self.session_token = "fixture-session-cookie"
            self.device_id = "fixture-device"
            self.oai_session_id = "fixture-session-id"
            self.extra_cookies = {"fixture_cookie": "fixture_value"}
            self.http = self
        def refresh_session(self):
            state["calls"].append("refresh_session")
            if state.get("renew_session"):
                self.access_token = "refreshed-fixture-token"
                return True
            return False
        def hydrate(self):
            pass
        def _publishable_key(self):
            state["calls"].append("bootstrap")
            return "pk_test_saved"
        def prepare_setup_intent(self):
            state["calls"].append("prepare")
            if state.get("prepare_timeout"):
                raise TimeoutError("fixture timeout")
            setup_id = state.get("next_setup_id", "seti_fixture")
            state["expected_setup_id"] = setup_id
            state["intent_id"] = setup_id
            return setup_id, setup_id + "_secret_fixture"
        def retrieve_stripe_intent(self, **kwargs):
            state["calls"].append("retrieve")
            assert kwargs["intent_id"] == state.get("expected_setup_id", "seti_fixture")
            return {"id": state["intent_id"], "status": state["setup_status"], "customer": state["customer"], "payment_method": state["method_id"]}
        def _route_headers(self, route):
            return {}
        def get(self, url, **kwargs):
            state["calls"].append("list")
            assert url.endswith("/backend-api/payments/payment_methods")
            assert kwargs["params"] == {"account_id": "account_fixture"}
            methods = [{"id": "pm_fixture", "type": state.get("method_type", "card"), "customer": state["method_customer"], "card": {"brand": "visa", "last4": "4242"}}] if state["visible"] else []
            return SimpleNamespace(status_code=200, json=lambda: {"data": methods})
        def create_checkout(self, **kwargs):
            state["calls"].append(("checkout", kwargs))
            if state.get("refresh_timeout"):
                raise TimeoutError("fixture timeout")
            if state.get("refresh_entered"):
                state["refresh_entered"].set()
                assert state["refresh_release"].wait(2), "fixture refresh was not released"
            return SimpleNamespace(
                account_id=self.account_id, publishable_key=state["key"], checkout_session_id="oaics_refreshed",
                checkout_url="https://chatgpt.com/checkout/openai_llc/oaics_refreshed", currency="USD",
                customer_session_client_secret="cuss_fixture", amount=state.get("new_amount", Decimal("20.00")), checkout_kind="custom", confirm_return_url="",
            )
        def subscription_payload(self):
            assert self.access_token == "refreshed-fixture-token"
            assert self.account_id == "account_fixture"
            state["calls"].append("real_adapter_subscription_payload")
            return {"plan_type": "plus", "status": "active"}
        def close(self):
            state["closed"] += 1

    monkeypatch.setattr("gss_api.routers.saved_cards._client", lambda secrets: Client())
    def from_raw(raw, proxy=None):
        instance = Client()
        try:
            payload = json.loads(raw)
            instance.access_token = payload["accessToken"]
            if instance.access_token == "refreshed-fixture-token":
                instance.account_id = "account_fixture"
        except (ValueError, KeyError):
            pass
        return instance
    monkeypatch.setattr("gss_api.routers.orders.ChatGPTClient.from_raw_session", from_raw)
    async def verify(**kwargs):
        parsed = json.loads(kwargs["session"])
        assert parsed["accessToken"] == state.get("expected_verification_token", "fixture-original-token")
        state["verified_session"] = parsed
        state["calls"].append("verify_subscription")
        return SubscriptionInfo(active=bool(state.get("verify_active")), channel="fixture")
    monkeypatch.setattr("gss_api.routers.orders.build_pipeline", lambda: SimpleNamespace(verify=verify))
    class NoRealPayment:
        def __init__(self, **kwargs):
            raise AssertionError("offline test attempted to construct a real payment client")
    monkeypatch.setattr("gss_api.routers.orders.PlusProtocol", NoRealPayment)
    root = f"/api/orders/{oid}/saved-card"
    yield api, oid, root, state
    vault.drop(oid)
    store.clear()


def prepare(flow):
    api, _, root, _ = flow
    response = api.post(root + "/prepare")
    assert response.status_code == 200, response.text
    return response.json()


def saved(flow):
    prepare(flow)
    flow[3]["setup_status"] = "succeeded"


def test_initial_status_does_not_create_setup(flow):
    api, _, root, state = flow
    assert api.get(root).json() == {"state": "not_started"}
    assert state["calls"] == []


def test_prepare_is_idempotent_and_uses_account_key(flow):
    first = prepare(flow)
    second = prepare(flow)
    assert first == second
    assert first["publishable_key"] == "pk_test_saved"
    assert flow[3]["calls"].count("prepare") == 1
    assert flow[3]["closed"] == 1


@pytest.mark.parametrize("status", ["requires_payment_method", "requires_action", "requires_confirmation", "processing", "canceled"])
def test_incomplete_setup_never_refreshes_checkout(flow, status):
    prepare(flow)
    api, oid, root, state = flow
    old_url = store.view(oid).checkout_url
    state["setup_status"] = status
    assert api.get(root).json()["state"] == status
    assert api.post(root + "/refresh-checkout").status_code == 409
    assert store.view(oid).checkout_url == old_url
    assert not any(isinstance(call, tuple) for call in state["calls"])
    assert store.get_order(oid).status is OrderStatus.AWAITING_PAYMENT


def test_browser_success_flag_cannot_override_server_state(flow):
    prepare(flow)
    api, _, root, _ = flow
    result = api.post(root + "/refresh-checkout", json={"paid": True, "setup_status": "succeeded", "payment_method_id": "pm_other"})
    assert result.status_code == 409


def test_succeeded_setup_must_be_visible_on_same_account(flow):
    saved(flow)
    api, _, root, state = flow
    state["visible"] = False
    assert api.get(root).json()["state"] == "sync_pending"
    assert api.post(root + "/refresh-checkout").status_code == 409
    state["visible"] = True
    response = api.get(root)
    assert response.json()["state"] == "saved"
    assert response.json()["card"] == {"brand": "visa", "last4": "4242"}
    assert "secret" not in response.text
    assert "cus_fixture" not in response.text


@pytest.mark.parametrize("field,value", [("account", "other_account"), ("customer", ""), ("method_customer", "cus_other"), ("intent_id", "seti_other")])
def test_mismatched_context_never_counts_as_saved(flow, field, value):
    saved(flow)
    api, _, root, state = flow
    state[field] = value
    assert api.get(root).status_code == 400
    assert api.post(root + "/refresh-checkout").status_code == 400
    assert not any(isinstance(call, tuple) for call in state["calls"])


def test_refresh_retains_order_and_is_idempotent_without_payment(flow):
    saved(flow)
    api, oid, root, state = flow
    first = api.post(root + "/refresh-checkout")
    assert first.status_code == 200, first.text
    assert first.json()["order"]["id"] == oid
    assert first.json()["order"]["checkout_url"].endswith("oaics_refreshed")
    assert first.json()["order"]["status"] == "awaiting_payment"
    assert first.json()["payment_method_id"] == "pm_fixture"
    assert api.post(root + "/refresh-checkout").json() == first.json()
    assert api.get(root).json()["state"] == "checkout_ready"
    assert len([call for call in state["calls"] if isinstance(call, tuple)]) == 1
    assert vault.get(oid).session == "fixture-session"


def test_merchant_key_mismatch_keeps_original_checkout(flow):
    saved(flow)
    api, oid, root, state = flow
    old_url = store.view(oid).checkout_url
    state["key"] = "pk_test_other_merchant"
    assert api.post(root + "/refresh-checkout").status_code == 400
    assert store.view(oid).checkout_url == old_url
    assert vault.get(oid).saved_card_context["state"] == "saved"


def test_failed_refresh_preserves_bound_card_and_old_link(flow):
    saved(flow)
    api, oid, root, state = flow
    old_url = store.view(oid).checkout_url
    state["refresh_timeout"] = True
    assert api.post(root + "/refresh-checkout").status_code == 502
    assert store.view(oid).checkout_url == old_url
    assert api.get(root).json()["state"] == "saved"


def test_prepare_timeout_is_not_replayed(flow):
    api, _, root, state = flow
    state["prepare_timeout"] = True
    assert api.post(root + "/prepare").status_code == 502
    assert api.post(root + "/prepare").status_code == 409
    assert api.get(root).json()["state"] == "prepare_unknown"
    assert state["calls"].count("prepare") == 1


def test_payment_submission_waits_for_setup_resolution(flow):
    prepare(flow)
    api, oid, _, _ = flow
    body = {"payment_method_id": "pm_fixture", "billing": {"name": "Fixture", "line1": "Fixture Street", "city": "Fixture City", "postal_code": "00000", "country": "US"}}
    assert api.post(f"/api/orders/{oid}/pay-protocol", json=body).status_code == 409
    assert api.post(f"/api/orders/{oid}/pay", json={"payment_method_token": "pm_fixture"}).status_code == 409


def test_expired_order_cannot_create_setup(flow):
    api, oid, root, state = flow
    vault.drop(oid)
    assert api.post(root + "/prepare").status_code == 401
    assert state["calls"] == []


def test_non_card_method_is_not_exposed_as_saved_card(flow):
    saved(flow)
    api, _, root, state = flow
    state["method_type"] = "us_bank_account"
    assert api.get(root).status_code == 400
    assert api.post(root + "/refresh-checkout").status_code == 400


def test_canceled_setup_can_be_explicitly_prepared_again_after_server_check(flow):
    original = prepare(flow)
    api, _, root, state = flow
    state["setup_status"] = "canceled"
    # The browser alone cannot authorize a new setup.
    assert api.post(root + "/prepare").json()["setup_intent_id"] == original["setup_intent_id"]
    assert api.get(root).json()["state"] == "canceled"
    state["next_setup_id"] = "seti_second_fixture"
    state["setup_status"] = "requires_payment_method"
    new = api.post(root + "/prepare")
    assert new.status_code == 200
    assert new.json()["setup_intent_id"] != original["setup_intent_id"]
    assert state["calls"].count("prepare") == 2


def test_missing_new_quote_does_not_replace_old_checkout_or_cache_failure(flow):
    saved(flow)
    api, oid, root, state = flow
    old_url, old_amount = store.view(oid).checkout_url, store.view(oid).amount
    state["new_amount"] = None
    response = api.post(root + "/refresh-checkout")
    assert response.status_code == 400
    assert store.view(oid).checkout_url == old_url
    assert store.view(oid).amount == old_amount
    assert not vault.get(oid).saved_card_context.get("refreshed_checkout_id")
    state["new_amount"] = Decimal("0.00")
    retried = api.post(root + "/refresh-checkout")
    assert retried.status_code == 200
    assert retried.json()["order"]["amount"] == "0.00"


def test_session_renewal_rechecks_account_for_setup_status_and_refresh(flow, monkeypatch):
    api, _, root, state = flow
    state["account"] = "expired_account_snapshot"
    state["renew_session"] = True
    monkeypatch.setattr("gss_core.saved_cards.jwt_account_id", lambda token: "account_fixture")
    prepare(flow)
    state["setup_status"] = "succeeded"
    assert api.get(root).json()["state"] == "saved"
    assert api.post(root + "/refresh-checkout").status_code == 200
    assert state["calls"].index("refresh_session") < state["calls"].index("bootstrap")
    assert state["calls"].count("refresh_session") >= 3


def test_verify_during_setup_preserves_awaiting_payment(flow):
    prepare(flow)
    api, oid, root, state = flow
    state["setup_status"] = "requires_action"
    assert api.get(root).json()["state"] == "requires_action"
    assert api.post(f"/api/orders/{oid}/verify?paid=true").status_code == 409
    assert store.get_order(oid).status is OrderStatus.AWAITING_PAYMENT
    assert api.get(root).status_code == 200
    assert "verify_subscription" not in state["calls"]


@pytest.mark.parametrize("paid", [False, True])
def test_verify_before_official_payment_never_closes_refreshed_checkout(flow, paid):
    saved(flow)
    api, oid, root, _ = flow
    assert api.post(root + "/refresh-checkout").status_code == 200
    response = api.post(f"/api/orders/{oid}/verify?paid={str(paid).lower()}")
    assert response.status_code == 200
    assert response.json()["status"] == "awaiting_payment"
    assert response.json()["subscription"]["active"] is False
    assert api.get(root).json()["state"] == "checkout_ready"
    assert vault.get(oid) is not None


def test_real_subscription_activation_finishes_and_repeated_verify_is_idempotent(flow):
    saved(flow)
    api, oid, root, state = flow
    assert api.post(root + "/refresh-checkout").status_code == 200
    state["verify_active"] = True
    result = api.post(f"/api/orders/{oid}/verify")
    assert result.status_code == 200, result.text
    assert result.json()["status"] == "delivered"
    assert result.json()["subscription"]["active"] is True
    assert vault.get(oid) is None
    assert api.post(f"/api/orders/{oid}/verify").json()["status"] == "delivered"
    assert state["calls"].count("verify_subscription") == 1


def test_refresh_and_verify_share_the_same_order_lock(flow):
    saved(flow)
    api, oid, root, state = flow
    state["refresh_entered"] = threading.Event()
    state["refresh_release"] = threading.Event()
    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api.app), base_url="http://testserver") as client:
            refresh = asyncio.create_task(client.post(root + "/refresh-checkout"))
            assert await asyncio.to_thread(state["refresh_entered"].wait, 2)
            verify = asyncio.create_task(client.post(f"/api/orders/{oid}/verify"))
            await asyncio.sleep(0)
            assert not verify.done()
            state["refresh_release"].set()
            refreshed, checked = await asyncio.gather(refresh, verify)
            assert refreshed.status_code == checked.status_code == 200
            assert checked.json()["status"] == "awaiting_payment"
    asyncio.run(run())
    assert store.get_order(oid).status is OrderStatus.AWAITING_PAYMENT
    assert vault.get(oid).saved_card_context["state"] == "checkout_ready"


def test_final_verify_uses_renewed_session_and_preserves_account_context(flow, monkeypatch):
    saved(flow)
    api, oid, root, state = flow
    assert api.post(root + "/refresh-checkout").status_code == 200
    state["account"] = "expired_account_snapshot"
    state["renew_session"] = True
    state["expected_verification_token"] = "refreshed-fixture-token"
    state["verify_active"] = True
    monkeypatch.setattr("gss_core.saved_cards.jwt_account_id", lambda token: "account_fixture")
    checked = api.post(f"/api/orders/{oid}/verify")
    assert checked.status_code == 200, checked.text
    assert checked.json()["status"] == "delivered"
    assert state["verified_session"]["accessToken"] == "refreshed-fixture-token"
    assert state["verified_session"]["cookies"] == {"fixture_cookie": "fixture_value"}


def test_final_verify_rejects_changed_account_before_subscription_lookup(flow):
    saved(flow)
    api, oid, root, state = flow
    assert api.post(root + "/refresh-checkout").status_code == 200
    state["account"] = "other_account"
    response = api.post(f"/api/orders/{oid}/verify?paid=true")
    assert response.status_code == 400
    assert "verify_subscription" not in state["calls"]
    assert store.get_order(oid).status is OrderStatus.AWAITING_PAYMENT


def test_final_verify_real_account_adapter_receives_refreshed_credentials(flow, monkeypatch):
    from gss_account.adapter import UpstreamAccountAdapter
    saved(flow)
    api, oid, root, state = flow
    assert api.post(root + "/refresh-checkout").status_code == 200
    state["account"] = "expired_account_snapshot"
    state["renew_session"] = True
    monkeypatch.setattr("gss_core.saved_cards.jwt_account_id", lambda token: "account_fixture")
    async def real_verify(*, session):
        return await UpstreamAccountAdapter().subscription_status(session)
    monkeypatch.setattr("gss_api.routers.orders.build_pipeline", lambda: SimpleNamespace(verify=real_verify))
    response = api.post(f"/api/orders/{oid}/verify")
    assert response.status_code == 200, response.text
    assert response.json()["subscription"]["active"] is True
    assert response.json()["status"] == "delivered"
    assert state["calls"].count("real_adapter_subscription_payload") == 1
