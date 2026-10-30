"""Device randomisation for the HAR-faithful Plus flow."""
from __future__ import annotations

import json
import re

from gss_core.har_device import (
    DeviceProfile,
    device_for_account,
    g_state_cookie,
    oai_telemetry_header,
)

_UA_BUILD = re.compile(r"Chrome/(\d+\.\d+\.\d+\.\d+)")


def test_device_is_coherent_and_random():
    a = DeviceProfile.random(country="JP")
    b = DeviceProfile.random(country="JP")

    assert a.guid != b.guid
    assert a.muid != b.muid
    assert a.sid != b.sid
    assert a.fingerprint_id != b.fingerprint_id
    assert a.stripe_js_id != b.stripe_js_id

    # UA build, major and Client Hints must agree
    build = _UA_BUILD.search(a.ua).group(1)
    assert build == a.chrome_build
    assert a.sec_ch_ua_full_version == build
    assert f'"Chromium";v="{build}"' in a.sec_ch_ua_full_version_list
    assert f'"Google Chrome";v="{a.chrome_major}"' in a.sec_ch_ua

    # platform hints follow the chosen OS
    if a.os_key == "macos":
        assert "Macintosh" in a.ua
        assert a.ch_platform == '"macOS"'
        assert a.platform == "MacIntel"
    elif a.os_key == "windows":
        assert "Windows NT" in a.ua
        assert a.ch_platform == '"Windows"'
    else:
        assert "X11; Linux" in a.ua
        assert a.ch_platform == '"Linux"'

    # screen geometry
    assert a.avail_h < a.screen_h
    assert a.frame_size == f"{a.screen_w}w_{a.screen_h}h_{a.screen_h - a.avail_h}d_{int(a.dpr)}r"
    assert len(a.canvas) == 55 and set(a.canvas) <= {"0", "1"}


def test_stripe_ids_match_capture_shape():
    d = DeviceProfile.random(country="US")
    # 2026-10-29 capture: UUID body + 6 hex chars = 42 chars
    for value in (d.guid, d.muid, d.sid):
        assert re.fullmatch(r"[0-9a-f-]{36}[0-9a-f]{6}", value)
        assert len(value) == 42
    assert re.fullmatch(r"[0-9a-f]{32}", d.fingerprint_id)
    assert d.elements_session_id.startswith("elements_session_")
    assert d.sobj_id.startswith("sobj-")
    assert d.elements_group_id.startswith("elements-")
    assert d.payment_user_agent().endswith("payment-element; deferred-intent")


def test_headers_match_capture_surface():
    d = DeviceProfile.random(country="JP")
    web = d.web_headers(referer="https://chatgpt.com/")
    for name in ("originator", "x-openai-checkout-source", "x-openai-codex-window-type",
                 "x-openai-web-frontend", "sec-ch-ua-full-version-list", "sec-ch-ua-model",
                 "user-agent", "priority"):
        assert name in web
    assert web["originator"] == "Codex Browser"
    assert web["x-openai-checkout-source"] == "chatgpt_web_merged"
    assert web["sec-ch-ua-model"] == '""'

    stripe = d.stripe_headers()
    assert stripe["origin"] == "https://js.stripe.com"
    assert stripe["sec-fetch-site"] == "same-site"

    fp = d.fingerprint_headers()
    assert fp["origin"] == "https://m.stripe.network"
    assert fp["cookie"] == f"m={d.guid}"


def test_profile_roundtrip_and_cookie():
    d = DeviceProfile.random(country="JP", seed="roundtrip")
    restored = DeviceProfile.from_dict(json.loads(json.dumps(d.to_dict())))
    assert restored == d

    state = json.loads(g_state_cookie(d))
    assert set(state) == {"i_l", "i_ll", "i_b", "i_e", "i_et"}
    assert state["i_e"]["enable_itp_optimization"] == 24


def test_oai_telemetry_header_shapes():
    assert oai_telemetry_header() == "[1,null]"
    value = oai_telemetry_header(6055)
    assert value == "[1,6.1,20,473,3,2,0,6]"


def test_device_persists_per_account(tmp_path, monkeypatch):
    monkeypatch.setenv("GSS_DEVICE_STORE", str(tmp_path / "devices.json"))
    first = device_for_account("acct_a", "US")
    again = device_for_account("acct_a", "US")
    other = device_for_account("acct_b", "US")

    assert first.guid == again.guid
    assert first.muid == again.muid
    assert first.sid == again.sid
    assert first.stripe_js_id != again.stripe_js_id  # per page load
    assert first.guid != other.guid
    assert (tmp_path / "devices.json").exists()


def test_device_store_refuses_cross_account_reuse(tmp_path, monkeypatch):
    path = tmp_path / "devices.json"
    monkeypatch.setenv("GSS_DEVICE_STORE", str(path))
    stolen = device_for_account("acct_a", "US").to_dict()
    stolen["owner"] = "acct_a"
    path.write_text(json.dumps({"acct_b": stolen}))

    fresh = device_for_account("acct_b", "US")
    assert fresh.guid != stolen["guid"]
