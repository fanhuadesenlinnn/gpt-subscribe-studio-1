"""Legacy core regression; the current HTTP contract is test_checkout_protocol_api."""
from __future__ import annotations

import json
from decimal import Decimal
from types import SimpleNamespace
from urllib.parse import parse_qs

import pytest

from gss_core.chatgpt import APP_BASE, STRIPE_BASE
from gss_core.saved_card_protocol import run_saved_card_protocol


@pytest.fixture
def legacy():
    state = {"calls": [], "setup_status": "requires_payment_method", "after_setup": "succeeded",
             "visible": True, "pi_status": "requires_confirmation", "after_payment": "succeeded"}
    context = {"protocol_flow": True}
    spec = {"account_id": "account_fixture", "country": "US", "promo": False,
            "expected_amount": "20.00", "expected_currency": "USD"}
    card = {"number": "4242424242424242", "cvc": "123", "exp_month": "12", "exp_year": "2040"}
    billing = {"name": "Fixture", "email": "fixture@example.test", "country": "US", "state": "OR",
               "city": "Portland", "line1": "Fixture Street", "postal_code": "97211"}
    class Client:
        account_id = "account_fixture"
        timeout = 1
        def __init__(self): self.http = self; self.stripe_http = self
        def refresh_session(self): return False
        def hydrate(self): pass
        def _publishable_key(self): return "pk_test_fixture"
        def _route_headers(self,route,**kwargs): return {}
        def prepare_setup_intent(self):
            state["calls"].append("prepare_setup")
            if state.get("prepare_lost"): raise TimeoutError("fixture preparation lost")
            return "seti_fixture", "seti_fixture_secret_fixture"
        def retrieve_stripe_intent(self,**kwargs):
            state["calls"].append("get_"+kwargs["intent_id"])
            if kwargs["intent_id"] == "seti_fixture":
                return {"id":"seti_fixture","status":state["setup_status"],"customer":"cus_fixture","payment_method":"pm_fixture"}
            return {"id":"pi_fixture","status":state["pi_status"],"customer":"cus_fixture","payment_method":"pm_fixture",
                    "amount":2000,"currency":"usd","last_payment_error":{"code":"card_declined","decline_code":"insufficient_funds"}}
        def get(self,url,**kwargs):
            assert url==APP_BASE+"/backend-api/payments/payment_methods"
            state["calls"].append("list_methods")
            data=[{"id":"pm_fixture","customer":"cus_fixture","type":"card","card":{"brand":"visa","last4":"4242"}}] if state["visible"] else []
            return SimpleNamespace(status_code=200,json=lambda:{"data":data})
        def post(self,url,**kwargs):
            assert url==STRIPE_BASE+"/v1/setup_intents/seti_fixture/confirm"
            form={k:v[0] for k,v in parse_qs(kwargs["data"]).items()}
            assert form["payment_method_data[card][number]"]==card["number"]
            state["calls"].append("confirm_setup")
            if state.get("setup_error"):
                return SimpleNamespace(status_code=state.get("setup_http",400),json=lambda:{"error":state["setup_error"]})
            state["setup_status"]=state["after_setup"]
            if state.get("setup_lost"):raise TimeoutError("fixture setup response lost")
            return SimpleNamespace(status_code=200,json=lambda:{"id":"seti_fixture","status":state["setup_status"],"payment_method":"pm_fixture"})
        def create_checkout(self,**kwargs):
            state["calls"].append("create_checkout")
            return SimpleNamespace(account_id="account_fixture",publishable_key="pk_test_fixture",checkout_session_id="oaics_fixture",
                checkout_url=APP_BASE+"/checkout/openai_llc/oaics_fixture",amount=Decimal("20.00"),currency="USD")
        def _sync_billing_tax(self,**kwargs):return state.get("tax_minor",2000)
        def create_confirmation_token(self,**kwargs):
            state["calls"].append("ctoken")
            return "ctoken_fixture"
        def confirm_checkout(self,**kwargs):
            state["calls"].append("app_confirm")
            return {"status":"success","client_secret":"pi_fixture_secret_fixture"}
        def confirm_stripe_intent(self,**kwargs):
            state["calls"].append("pi_confirm")
            state["pi_status"]=state["after_payment"]
            if state.get("payment_lost"):raise TimeoutError("fixture PI response lost")
        def subscription_payload(self):return {"plan_type":"plus","status":"active"}
    client=Client()
    def run(read_only=False):return run_saved_card_protocol(client,context,spec,card=card,billing=billing,read_only=read_only)
    return SimpleNamespace(state=state,context=context,spec=spec,card=card,billing=billing,run=run)


def test_legacy_core_preserves_inline_setup_contract(legacy):
    assert legacy.run()["state"]=="paid"
    assert legacy.context["protocol_setup_method"]=="inline_payment_method_data"
    assert legacy.state["calls"].count("confirm_setup")==1


@pytest.mark.parametrize("status,expected",[("requires_action","challenge_required"),("processing","pending"),("canceled","card_declined")])
def test_legacy_setup_pause_never_creates_checkout(legacy,status,expected):
    legacy.state["after_setup"]=status
    assert legacy.run()["state"]==expected
    assert legacy.run(True)["state"]==expected
    assert "create_checkout" not in legacy.state["calls"]
    assert legacy.state["calls"].count("confirm_setup")==1


def test_legacy_saved_card_waits_for_account_sync(legacy):
    legacy.state["visible"]=False
    result=legacy.run()
    assert (result["state"],result["stage"])==("pending","sync")
    assert "create_checkout" not in legacy.state["calls"]


@pytest.mark.parametrize("settled",[False,True])
def test_legacy_setup_connection_loss_does_not_replay(legacy,settled):
    legacy.state.update(setup_lost=True,after_setup="succeeded" if settled else "requires_payment_method")
    assert legacy.run()["state"]==("paid" if settled else "unknown")
    legacy.run(True);legacy.run()
    assert legacy.state["calls"].count("confirm_setup")==1


def test_legacy_unknown_setup_preparation_is_not_replayed(legacy):
    legacy.state["prepare_lost"]=True
    assert legacy.run()["state"]=="unknown"
    assert legacy.run(True)["state"]==legacy.run()["state"]=="unknown"
    assert legacy.state["calls"].count("prepare_setup")==1


@pytest.mark.parametrize("http,expected",[(400,"request_rejected"),(402,"card_declined"),(500,"unknown")])
def test_legacy_explicit_setup_error_stays_distinct_from_unknown(legacy,http,expected):
    legacy.state.update(setup_http=http,setup_error={"message":"Fixture rejection","code":"fixture_error"})
    assert legacy.run()["state"]==expected
    assert legacy.run(True)["state"]==legacy.run()["state"]==expected
    assert legacy.state["calls"].count("confirm_setup")==1
    assert "app_confirm" not in legacy.state["calls"]


def test_legacy_integration_rejection_is_retained(legacy):
    legacy.state["setup_error"]={"message":"This integration surface is unsupported for publishable key tokenization"}
    result=legacy.run()
    assert result["state"]=="integration_error"
    assert legacy.run(True)==legacy.run()==result
    assert "app_confirm" not in legacy.state["calls"]


def test_legacy_quote_change_stops_before_payment(legacy):
    legacy.state["tax_minor"]=2150
    result=legacy.run()
    assert (result["state"],result["stage"])==("pending","checkout")
    assert "app_confirm" not in legacy.state["calls"]
    assert legacy.context["protocol_checkout"].amount==Decimal("21.5")


@pytest.mark.parametrize("status,expected",[("requires_action","challenge_required"),("processing","pending"),("requires_payment_method","card_declined")])
def test_legacy_payment_status_and_no_replay(legacy,status,expected):
    legacy.state["after_payment"]=status
    assert legacy.run()["state"]==expected
    legacy.run(True);legacy.run()
    assert legacy.state["calls"].count("pi_confirm")==legacy.state["calls"].count("app_confirm")==1


def test_legacy_context_has_no_raw_card_fields(legacy):
    legacy.state["after_setup"]="processing"
    legacy.run()
    text=json.dumps(legacy.context,default=str)
    assert legacy.card["number"] not in text
    assert '"cvc"' not in text
    assert '"number"' not in text
