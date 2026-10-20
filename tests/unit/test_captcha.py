"""Stripe intent_confirmation_challenge solving, offline."""
from types import SimpleNamespace

import pytest

from gss_contracts.errors import DomainError
from gss_core.captcha import challenge_from_intent, solve_hcaptcha, verify_challenge, _proxy_fields

CHALLENGE = {
    "site_key": "site_fixture",
    "rqdata": "rq_fixture",
    "verification_url": "/v1/payment_intents/pi_fixture/verify_challenge",
}
INTENT = {
    "id": "pi_fixture",
    "status": "requires_action",
    "next_action": {"type": "use_stripe_sdk", "use_stripe_sdk": {
        "type": "intent_confirmation_challenge",
        "stripe_js": {"site_key": "site_fixture", "rqdata": "rq_fixture",
                      "verification_url": "/v1/payment_intents/pi_fixture/verify_challenge"}}},
}


class HTTP:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def post(self, url, json=None, data=None, headers=None, timeout=None, **kw):
        self.calls.append({"url": url, "json": json, "data": data, "headers": headers or {}})
        payload = self.responses.pop(0) if self.responses else {"errorId": 0, "status": "processing"}
        status = payload.pop("_status", 200)
        return SimpleNamespace(status_code=status, json=lambda: payload)


def test_challenge_extraction_reads_stripe_js_block():
    assert challenge_from_intent(INTENT) == CHALLENGE


def test_challenge_extraction_ignores_other_next_actions():
    other = {"next_action": {"type": "use_stripe_sdk", "use_stripe_sdk": {"type": "three_d_secure_redirect"}}}
    assert challenge_from_intent(other) is None
    assert challenge_from_intent({}) is None


PROXY = "http://user:pass@proxy.invalid:3128"


def test_solve_sends_proxied_enterprise_task_with_rqdata_then_returns_token():
    http = HTTP([
        {"errorId": 0, "taskId": "task_fixture"},
        {"errorId": 0, "status": "processing"},
        {"errorId": 0, "status": "ready", "solution": {"gRecaptchaResponse": "P1_token"}},
    ])
    token = solve_hcaptcha(http, CHALLENGE, api_key="key_fixture", proxy=PROXY, poll_interval=0)
    assert token == "P1_token"
    task = http.calls[0]["json"]["task"]
    # Enterprise binds the token to the solving context, so the task must use the order proxy.
    assert task["type"] == "HCaptchaTask"
    assert task["isEnterprise"] is True
    assert task["websiteKey"] == "site_fixture"
    assert task["rqdata"] == "rq_fixture"
    assert task["proxyAddress"] == "proxy.invalid" and task["proxyPort"] == 3128
    assert task["proxyLogin"] == "user" and task["proxyPassword"] == "pass"
    assert task["websiteURL"].startswith("https://b.stripecdn.com/")
    # UA must be pinned so solve and verify share the same context.
    assert task["userAgent"].startswith("Mozilla/5.0")


def test_solve_refuses_to_run_without_order_proxy():
    with pytest.raises(DomainError, match="未取到代理"):
        solve_hcaptcha(HTTP([]), CHALLENGE, api_key="key_fixture", proxy="", poll_interval=0)


def test_proxy_fields_parses_socks_and_bare_host():
    assert _proxy_fields("socks5://h:1")["proxyType"] == "socks5"
    assert _proxy_fields("") == {}
    assert _proxy_fields("host:8080")["proxyPort"] == 8080


def test_solve_omits_rqdata_when_absent():
    http = HTTP([{"errorId": 0, "taskId": "t"}, {"errorId": 0, "status": "ready",
                                                  "solution": {"gRecaptchaResponse": "tok"}}])
    solve_hcaptcha(http, {"site_key": "s"}, api_key="k", proxy=PROXY, poll_interval=0)
    assert "rqdata" not in http.calls[0]["json"]["task"]


def test_solve_requires_api_key():
    with pytest.raises(DomainError):
        solve_hcaptcha(HTTP([]), CHALLENGE, api_key="", proxy=PROXY)
    with pytest.raises(DomainError):
        solve_hcaptcha(HTTP([]), {}, api_key="k", proxy=PROXY)


def test_solve_reports_provider_error():
    http = HTTP([{"errorId": 1, "errorDescription": "ERROR_KEY_DENIED"}])
    with pytest.raises(DomainError, match="任务创建失败"):
        solve_hcaptcha(http, CHALLENGE, api_key="k", proxy=PROXY, poll_interval=0)


def test_solve_raises_when_no_token_returned():
    http = HTTP([{"errorId": 0, "taskId": "t"}, {"errorId": 0, "status": "ready", "solution": {}}])
    with pytest.raises(DomainError):
        solve_hcaptcha(http, CHALLENGE, api_key="k", proxy=PROXY, poll_interval=0)


def test_verify_posts_token_with_client_secret():
    http = HTTP([{"status": "succeeded"}])
    result = verify_challenge(http, intent_id="pi_fixture", client_secret="pi_fixture_secret_x",
                              token="P1_token", publishable_key="pk_live_x",
                              api_version="2026-03-25.dahlia")
    assert result["status"] == "succeeded"
    call = http.calls[0]
    assert call["url"].endswith("/v1/payment_intents/pi_fixture/verify_challenge")
    # Stripe.js posts verify_challenge, so the Origin is js.stripe.com here.
    assert call["headers"]["Origin"] == "https://js.stripe.com"
    body = call["data"]
    assert "challenge_response_token=P1_token" in body
    assert "captcha_vendor_name=hcaptcha" in body
    assert "_stripe_version=2026-03-25.dahlia" in body


def test_verify_includes_ekey_when_present():
    http = HTTP([{"status": "succeeded"}])
    verify_challenge(http, intent_id="pi_fixture", client_secret="pi_fixture_secret_x",
                     token="P1_token", publishable_key="pk_live_x",
                     api_version="2025-03-31.basil", ekey="ek_live_fixture")
    body = http.calls[0]["data"]
    # hCaptcha hands back an ekey; Stripe.js forwards it as captcha_response_key.
    assert "captcha_response_key=ek_live_fixture" in body


def test_verify_omits_ekey_when_empty():
    http = HTTP([{"status": "succeeded"}])
    verify_challenge(http, intent_id="pi_fixture", client_secret="pi_fixture_secret_x",
                     token="P1_token", publishable_key="pk_live_x",
                     api_version="2025-03-31.basil")
    assert "captcha_response_key" not in http.calls[0]["data"]


def test_verify_surfaces_stripe_error():
    http = HTTP([{"_status": 400, "error": {"type": "invalid_request_error", "message": "No valid challenge"}}])
    with pytest.raises(DomainError, match="Stripe 验证未通过"):
        verify_challenge(http, intent_id="pi_x", client_secret="pi_x_secret_y", token="t",
                         publishable_key="pk_x", api_version="v")


def test_verify_requires_intent_credentials():
    with pytest.raises(DomainError):
        verify_challenge(HTTP([]), intent_id="cs_x", client_secret="pi_x_secret_y", token="t",
                         publishable_key="pk_x", api_version="v")
