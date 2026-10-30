"""Stripe/ChatGPT telemetry replayed from the 2026-10-29 capture."""
from __future__ import annotations

import base64
import json
import urllib.parse

import httpx

from gss_core.har_device import DeviceProfile
from gss_core.har_telemetry import (
    PHASE_ELEMENTS,
    PHASE_TAX_ID,
    StripeTelemetry,
    TelemetryContext,
    catalog_info,
)

HAR_SHA = "230330059dc8633c73ed695440ceb7019e7ef03be3fa419e36e7005bdb4e9e38"
CAPTURED_STRIPE_JS_ID = "2e066c68-cff7-4d6c-9039-6ef335151673"
CAPTURED_EMAIL = "fixture@example.test"


class Recorder:
    def __init__(self):
        self.requests = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.host == "m.stripe.com":
            return httpx.Response(200, json={"guid": "srv-guid", "muid": "srv-muid", "sid": "srv-sid"})
        return httpx.Response(200, json={})

    def client(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self.handler))


def build(recorder: Recorder, **kwargs):
    device = DeviceProfile.random(country="JP", seed="telemetry")
    ctx = TelemetryContext(
        device=device,
        key="pk_live_fixture",
        currency="JPY",
        amount=1499,
        email="fixture@example.test",
        country="JP",
        customer_id="cus_fixture",
        **kwargs,
    )
    return device, StripeTelemetry(recorder.client(), ctx)


def _decode_fingerprint(request: httpx.Request) -> dict:
    raw = base64.b64decode(request.content.decode()).decode()
    return json.loads(urllib.parse.unquote(raw))


def test_catalog_matches_capture():
    info = catalog_info()
    assert info["har_sha256"] == HAR_SHA
    assert info["events"] == 291
    assert info["unique_events"] == 146
    assert info["stripe_event_posts"] == 88


def test_fingerprint_registration_shape():
    recorder = Recorder()
    device, telemetry = build(recorder)
    original = (device.guid, device.muid, device.sid)
    telemetry.register()

    posts = [r for r in recorder.requests if r.url.host == "m.stripe.com" and r.url.path == "/6"]
    assert len(posts) == 4

    first = _decode_fingerprint(posts[0])
    assert first["v2"] == 1 and first["a"] is None
    assert first["b"]["d"] == original[1] and first["b"]["e"] == original[2]
    assert first["b"]["u"] == "chatgpt.com"
    assert len(first["h"]) == 20

    second = _decode_fingerprint(posts[1])
    assert second["v2"] == 2
    assert second["a"]["d"]["v"] == device.platform
    assert second["a"]["f"]["v"] == device.frame_size
    assert second["a"]["l"]["v"] == device.ua

    mouse = _decode_fingerprint(posts[2])
    assert mouse["source"] == "mouse-timings-10-v2" and mouse["data"] == []
    mouse10 = _decode_fingerprint(posts[3])
    assert mouse10["source"] == "mouse-timings-10" and len(mouse10["data"]) == 10

    # the server-assigned ids win once returned, and the device adopts them
    assert telemetry.guid == "srv-guid"
    assert (device.guid, device.muid, device.sid) == ("srv-guid", "srv-muid", "srv-sid")
    assert posts[0].headers["cookie"] == f"m={original[0]}"


def test_events_are_batched_in_capture_order():
    recorder = Recorder()
    device, telemetry = build(recorder)
    telemetry.emit_through(PHASE_ELEMENTS)

    posts = [r for r in recorder.requests if r.url.host == "r.stripe.com"]
    assert posts, "expected r.stripe.com batches"
    assert all(r.url.path == "/b" for r in posts)

    seen = []
    for request in posts:
        fields = dict(urllib.parse.parse_qsl(request.content.decode()))
        assert fields["client_id"] == "stripe-js"
        events = json.loads(fields["events"])
        assert int(fields["num_requests"]) == len(events)
        seen.extend(events)

    names = [event["event_name"] for event in seen]
    assert names[0] == "elements.controller.load"
    assert names[1] == "elements.cdn.load"
    # every event keeps the capture's common fields
    for event in seen:
        assert event["os"] == device.os_label
        assert event["browserFamily"] == "Chrome"
        assert event["stripe_js_id"] == device.stripe_js_id
        assert isinstance(event["created"], int)


def test_no_captured_device_or_identity_survives():
    recorder = Recorder()
    device, telemetry = build(recorder)
    original = (device.guid, device.muid, device.sid)
    telemetry.register()
    telemetry.emit_through(PHASE_TAX_ID)

    event_blob = "".join(
        urllib.parse.unquote(r.content.decode())
        for r in recorder.requests if r.url.host == "r.stripe.com"
    )
    fingerprint_blob = "".join(
        urllib.parse.unquote(base64.b64decode(r.content.decode()).decode())
        for r in recorder.requests if r.url.host == "m.stripe.com"
    )
    blob = event_blob + fingerprint_blob

    for leaked in (CAPTURED_STRIPE_JS_ID, CAPTURED_EMAIL, "51HOrSwC6h1nxGoI3",
                   "237d4886-0547-4227-89ae-35e884abc758a53e3c"):
        assert leaked not in blob

    # the beacons carry the freshly generated device
    assert device.stripe_js_id in event_blob
    assert device.muid in fingerprint_blob


def test_all_events_materialise_without_placeholders():
    recorder = Recorder()
    _, telemetry = build(recorder)
    telemetry.flush()
    for request in recorder.requests:
        if request.url.host != "r.stripe.com":
            continue
        events = json.loads(dict(urllib.parse.parse_qsl(request.content.decode()))["events"])
        text = json.dumps(events)
        assert "{{" not in text


def test_passive_captcha_token_reaches_events():
    recorder = Recorder()
    _, telemetry = build(recorder)
    telemetry.ctx.hcaptcha_token = "P1_fixture_token"
    telemetry.flush()

    seen = 0
    for request in recorder.requests:
        if request.url.host != "r.stripe.com":
            continue
        events = json.loads(dict(urllib.parse.parse_qsl(request.content.decode()))["events"])
        seen += sum(1 for e in events if e.get("passive_captcha_token") == "P1_fixture_token")
    assert seen > 0
