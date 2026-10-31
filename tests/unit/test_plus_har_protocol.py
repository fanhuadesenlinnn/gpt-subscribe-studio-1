"""Request-for-request check of the HAR-faithful Plus flow (offline)."""
from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import parse_qs

import pytest

from types import SimpleNamespace

from gss_core.har_device import DeviceProfile
from gss_core.plus_har_protocol import PlusHarProtocol, run_har_context
from gss_core.stripe_protocol import PaymentOutcome

CHECKOUT_ID = "oaics_fixturecheckout"
FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
PROCESSOR = "openai_llc"


class FakeResponse:
    def __init__(self, payload, status: int = 200):
        self.status_code = status
        self._payload = payload
        self.content = b"{}" if payload is not None else b""
        self.text = json.dumps(payload) if payload is not None else ""

    def json(self):
        return self._payload


class StubClient:
    access_token = "token_fixture"
    account_id = "account_fixture"
    device_id = "did_fixture"
    sentinel_token = "sentinel_fixture"
    sentinel_so = "so_fixture"
    language = "en-US"
    timezone = "Asia/Tokyo"
    proxy = None
    extra_cookies: dict = {}
    session_token = ""

    def refresh_session(self):
        return False

    def hydrate(self):
        return None

    def subscription_payload(self):
        return {"plan_type": "plus", "is_active": True,
                "entitlement": {"has_active_subscription": True}}

    def retrieve_stripe_intent(self, **kwargs):
        return {"id": "pi_fixture", "status": "succeeded"}

    def close(self):
        pass


class FakeApp:
    def __init__(self):
        self.calls = []
        self.page_status = "open"

    def request(self, method, url, **kwargs):
        route = url.split("chatgpt.com", 1)[-1]
        self.calls.append((method, route, kwargs.get("json")))
        if method == "GET" and route.startswith("/backend-api/payments/checkout/"):
            return FakeResponse({
                "checkout_session_id": CHECKOUT_ID,
                "publishable_key": "pk_live_fixture",
                "processor_entity": PROCESSOR,
                "customer_session_client_secret": "cuss_fixture",
                "confirm_return_url": ("https://chatgpt.com/checkout/verify?"
                                       f"stripe_session_id={CHECKOUT_ID}&processor_entity={PROCESSOR}&plan_type=plus"),
                "status": self.page_status,
                "payment_status": "paid" if self.page_status == "complete" else "unpaid",
                "checkout_state": {"currency": "jpy", "total": {"total": {"minorUnitsAmount": 1499}}},
            })
        if route == "/backend-api/payments/checkout/taxes":
            return FakeResponse({"checkout_session": {
                "checkout_session_id": CHECKOUT_ID,
                "publishable_key": "pk_live_fixture",
                "checkout_state": {"currency": "jpy", "total": {"total": {"minorUnitsAmount": 1363}}},
            }})
        if route == "/backend-api/payments/checkout/confirm":
            self.page_status = "complete"
            return FakeResponse({
                "status": "success",
                "type": "payment_intent",
                "client_secret": "pi_fixture_secret_fixture",
                "confirm_return_url": ("https://chatgpt.com/checkout/verify?"
                                       f"stripe_session_id={CHECKOUT_ID}&processor_entity={PROCESSOR}&plan_type=plus"),
            })
        if route == "/backend-api/payments/checkout_session":
            return FakeResponse({"success": True, "created_new_subscription": False})
        return FakeResponse({})


class FakeStripe:
    def __init__(self):
        self.calls = []
        self.forms = {}

    def get(self, url, **kwargs):
        route = url.split("stripe.com", 1)[-1]
        self.calls.append(("GET", route))
        if route.startswith("/v1/elements/sessions"):
            return FakeResponse({
                "session_id": "elements_session_fixture",
                "customer": {"id": "cus_fixture"},
                "passive_captcha": {"site_key": "sitekey_fixture", "rqdata": "rqdata_fixture"},
            })
        return FakeResponse({})

    def post(self, url, **kwargs):
        route = url.split("stripe.com", 1)[-1]
        self.calls.append(("POST", route))
        body = kwargs.get("content") or ""
        self.forms[(route, len(self.calls))] = parse_qs(body if isinstance(body, str) else body.decode())
        if route == "/v1/confirmation_tokens":
            return FakeResponse({"id": "ctoken_fixture", "payment_method_preview": {"id": "pm_fixture"}})
        if "/confirm" in route:
            return FakeResponse({"id": "pi_fixture", "status": "succeeded"})
        return FakeResponse({})


class FakeTelemetry:
    def __init__(self, ctx):
        self.ctx = ctx
        self.calls = []

    def warm_assets(self):
        self.calls.append(("warm_assets",))

    def register(self):
        self.calls.append(("register",))

    def emit_through(self, phase):
        self.calls.append(("emit", phase))

    def beat(self, times=2):
        self.calls.append(("beat", times))

    def ces_register(self):
        self.calls.append(("ces",))


class Harness(PlusHarProtocol):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.app = FakeApp()
        self.stripe = FakeStripe()
        self.sentinel_flows = []
        self.order = []

    def _prepare(self):
        self.client = StubClient()
        self.telemetry = FakeTelemetry(SimpleNamespace(amount=0, customer_id="", hcaptcha_token=""))
        self.order.append(("prepare",))

    def _prelude(self):
        self.telemetry.ces_register()
        self.order.append(("ces",))

    def _sentinel(self, flow):
        self.sentinel_flows.append(flow)
        self.order.append(("sentinel", flow))

    def _radar_token(self):
        return "P1_fixture_token"

    def _app(self, method, route, *, json_body=None, params=None, telemetry=None,
             referer="https://chatgpt.com/"):
        self.order.append(("app", method, route))
        return self.app.request(method, "https://chatgpt.com" + route, json=json_body, params=params)

    def _stripe_session(self):
        return self.stripe

    def _flush(self, phase):
        self.telemetry.emit_through(phase)
        self.order.append(("emit", phase))


def build_harness(**overrides):
    params = dict(
        session="raw-session-fixture",
        checkout_url=f"https://chatgpt.com/checkout/{PROCESSOR}/{CHECKOUT_ID}",
        publishable_key="pk_live_fixture",
        card={"number": "4242424242424242", "cvc": "123", "exp_month": "12", "exp_year": "2040"},
        billing={"name": "Fixture Holder", "email": "fixture@example.test", "line1": "1 Fixture St",
                 "city": "Portland", "state": "OR", "postal_code": "97211", "country": "US"},
        currency="JPY",
        country="US",
        device=DeviceProfile.random(country="US", seed="flow"),
        telemetry="full",
    )
    params.update(overrides)
    return Harness(**params)


def test_flow_order_matches_capture():
    engine = build_harness()
    result = engine.pay()

    assert result.state == "paid"
    assert result.intent_status == "succeeded"

    steps = [(item[0], item[1] if len(item) > 1 else "") for item in engine.order]
    expected = [
        ("prepare", ""),
        ("ces", ""),
        ("sentinel", "chatgpt_checkout"),
        ("app", "GET"),  # read checkout before Elements
    ]
    assert steps[: len(expected)] == expected

    routes = [item[2] for item in engine.order if item[0] == "app"]
    assert routes[0].startswith("/backend-api/payments/checkout/")
    assert "/backend-api/payments/checkout/taxes" in routes
    assert "/backend-api/payments/checkout/update_tax_id" in routes
    assert routes.index("/backend-api/payments/checkout/taxes") < routes.index(
        "/backend-api/payments/checkout/confirm")
    assert "/backend-api/payments/checkout_session" in routes

    assert engine.sentinel_flows == [
        "chatgpt_checkout",          # page boot
        "chatgpt_checkout",          # right after reading the checkout
        "checkout_session_approval",  # before the app confirm
        "chatgpt_checkout",
        "checkout_session_approval",  # before the intent confirm
    ]

    stripe_calls = engine.stripe.calls
    assert stripe_calls[0][0] == "GET" and stripe_calls[0][1].startswith("/v1/elements/sessions")
    lookups = [c for c in stripe_calls if c[1] == "/v1/consumers/sessions/lookup"]
    assert len(lookups) == 3
    assert ("GET", "/edge-internal/card-metadata") in stripe_calls
    assert ("POST", "/v1/confirmation_tokens") in stripe_calls
    assert ("POST", "/v1/payment_intents/pi_fixture/confirm") in stripe_calls

    phases = [item[1] for item in engine.order if item[0] == "emit"]
    assert phases == [98, 160, 215, 266, 293, 329, 343]


def test_confirmation_token_body_matches_capture():
    engine = build_harness()
    engine.pay()

    form = None
    for (route, _), values in engine.stripe.forms.items():
        if route == "/v1/confirmation_tokens":
            form = values
    assert form is not None

    device = engine.device
    assert form["payment_method_data[type]"] == ["card"]
    assert form["payment_method_data[card][number]"] == ["4242424242424242"]
    assert form["payment_method_data[allow_redisplay]"] == ["always"]
    assert form["payment_method_data[guid]"] == [device.guid]
    assert form["payment_method_data[muid]"] == [device.muid]
    assert form["payment_method_data[sid]"] == [device.sid]
    assert form["payment_method_data[radar_options][hcaptcha_token]"] == ["P1_fixture_token"]
    assert form["payment_method_data[pasted_fields]"] == ["number"]
    assert form["setup_future_usage"] == ["off_session"]
    assert form["set_as_default_payment_method"] == ["false"]
    assert form["client_context[customer]"] == ["cus_fixture"]
    assert form["client_attribution_metadata[merchant_integration_source]"] == ["elements"]
    assert form["client_attribution_metadata[payment_intent_creation_flow]"] == ["deferred"]
    assert form["client_attribution_metadata[elements_session_id]"] == ["elements_session_fixture"]
    assert form["_stripe_version"] == ["2025-03-31.basil"]


def test_confirm_and_intent_bodies_match_capture():
    engine = build_harness()
    engine.pay()

    confirm = next(body for method, route, body in engine.app.calls
                   if route == "/backend-api/payments/checkout/confirm")
    assert confirm == {
        "checkout_session_id": CHECKOUT_ID,
        "save_payment_method_for_reuse": True,
        "confirm_token": "ctoken_fixture",
        "selected_payment_method_type": "card",
    }

    intent_form = None
    for (route, _), values in engine.stripe.forms.items():
        if route == "/v1/payment_intents/pi_fixture/confirm":
            intent_form = values
    assert intent_form is not None
    assert intent_form["confirmation_token"] == ["ctoken_fixture"]
    assert intent_form["client_secret"] == ["pi_fixture_secret_fixture"]
    assert intent_form["client_attribution_metadata[merchant_integration_source]"] == ["l1"]
    assert intent_form["client_attribution_metadata[client_session_id]"] == [engine.device.stripe_js_id]
    return_url = intent_form["return_url"][0]
    assert return_url.startswith("https://chatgpt.com/checkout/verify?")
    assert f"stripe_session_id={CHECKOUT_ID}" in return_url
    assert "source=chatgpt_web_merged" in return_url


def test_declined_intent_maps_to_card_declined():
    engine = build_harness()

    original_post = engine.stripe.post

    def decline(url, **kwargs):
        route = url.split("stripe.com", 1)[-1]
        if not route.endswith("/confirm"):
            return original_post(url, **kwargs)
        engine.stripe.calls.append(("POST", route))
        return FakeResponse({"error": {"code": "card_declined", "decline_code": "generic_decline",
                                       "message": "Your card was declined."}}, status=402)

    engine.stripe.post = decline  # type: ignore[assignment]
    result = engine.pay()
    assert result.state == "card_declined"
    assert result.decline_code == "generic_decline"


def test_read_only_never_posts_payment(monkeypatch):
    engine = build_harness(read_only=True)
    result = engine.pay()
    assert result.state == "pending"
    assert not engine.stripe.calls, "status query must not touch Stripe"
    posts = [c for c in engine.app.calls if c[0] == "POST"]
    assert posts == []


def test_run_har_context_persists_device_and_ids(monkeypatch):
    calls = {}

    def fake_pay(self):
        calls["device"] = self.device.to_dict()
        self.ctoken = "ctoken_ctx"
        self.intent_id = "pi_ctx"
        self.intent_secret = "pi_ctx_secret"
        self.amount_minor = 1363
        self.confirmed = {"status": "success"}
        self.stage = "payment"
        return PaymentOutcome(state="paid", intent_status="succeeded")

    monkeypatch.setattr(PlusHarProtocol, "pay", fake_pay)
    context = {}
    spec = {
        "checkout_url": f"https://chatgpt.com/checkout/{PROCESSOR}/{CHECKOUT_ID}",
        "checkout_id": CHECKOUT_ID,
        "publishable_key": "pk_live_fixture",
        "currency": "JPY",
        "country": "US",
        "processor": PROCESSOR,
        "customer_session_client_secret": "cuss_fixture",
    }
    payload = run_har_context(StubClient(), context, spec, card={"number": "4242"},
                              billing={"email": "fixture@example.test", "country": "US"})
    assert payload["state"] == "paid"
    assert payload["stage"] == "payment"
    assert context["har_device"] == calls["device"]
    assert context["confirmation_token"] == "ctoken_ctx"
    assert context["intent_id"] == "pi_ctx"
    assert context["amount_minor"] == 1363
    assert context["app_confirmation_started"] is True


def test_elements_session_parses_live_body():
    """The live elements/sessions body (captured 2026-10-30) nests the cus_ id."""
    engine = build_harness()
    payload = json.loads((FIXTURES / "elements_session_live.json").read_text())

    class LiveStripe:
        def get(self, url, **kwargs):
            return FakeResponse(payload)

    engine._stripe_session = lambda: LiveStripe()  # type: ignore[assignment]
    engine._elements_session()

    assert engine.elements_session_id == payload["session_id"]
    assert engine.device.elements_session_id == payload["session_id"]
    assert engine.customer_id == "cus_fixture"
    assert engine.device.elements_session_config_id == payload["config_id"]
    assert engine.passive_captcha["site_key"] == payload["passive_captcha"]["site_key"]
    assert engine.passive_captcha["rqdata"] == ""


def test_ctoken_card_decline_maps_to_card_declined():
    engine = build_harness()
    engine._prepare()
    engine._radar_token = lambda: ""  # type: ignore[assignment]

    class DecliningStripe:
        def post(self, url, **kwargs):
            return FakeResponse({"error": {
                "code": "card_declined", "decline_code": "live_mode_test_card",
                "message": "Your card was declined. Your request was in live mode, but used a known test card.",
            }}, status=400)

    engine._stripe_session = lambda: DecliningStripe()  # type: ignore[assignment]
    from gss_contracts.errors import DomainError
    with pytest.raises(DomainError) as excinfo:
        engine._confirmation_token()
    assert str(excinfo.value.code) == "payment_declined"


def test_settle_reads_live_decline_body():
    """Live 402 body (2026-10-30) carries the PaymentIntent inside error."""
    engine = build_harness()
    engine.amount_minor = 2918
    payload = json.loads((FIXTURES / "intent_confirm_declined.json").read_text())
    engine.raw["intent"] = payload

    result = engine._settle()
    assert result.state == "card_declined"
    assert result.code == "card_declined"
    assert result.decline_code == "generic_decline"
    assert result.intent_status == "requires_payment_method"
    assert result.charge_id == "ch_fixture"
    assert (result.payment_method_id or "").startswith("pm_")


def test_settle_flags_amount_mismatch():
    engine = build_harness()
    engine.amount_minor = 1363
    payload = json.loads((FIXTURES / "intent_confirm_declined.json").read_text())
    engine.raw["intent"] = payload

    result = engine._settle()
    assert result.state == "unknown"
    assert "不一致" in (result.message or "")


def test_quote_guard_pauses_when_tax_raises_total():
    h = build_harness()
    h.expected_amount_minor = 2178
    h.amount_minor = 2300
    out = h._quote_guard()
    assert out is not None
    assert out.state == "pending"
    assert h.stage == "checkout"
    assert "税费" in (out.message or "")


def test_quote_guard_allows_equal_or_lower_total():
    h = build_harness()
    h.expected_amount_minor = 3000
    h.amount_minor = 2891
    assert h._quote_guard() is None
    h.amount_minor = 3000
    assert h._quote_guard() is None


def test_quote_guard_skipped_without_expected():
    h = build_harness()
    h.expected_amount_minor = 0
    h.amount_minor = 9999
    assert h._quote_guard() is None
