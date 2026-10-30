"""Run one HAR-faithful Plus payment attempt from the terminal.

No API server involved: this drives `PlusHarProtocol` directly so the request
sequence, the randomised device and the telemetry beacons can be observed on
their own.

Environment:
    GSS_SESSION            raw session JSON / cookie string (or GSS_SESSION_FILE)
    GSS_CHECKOUT_URL       https://chatgpt.com/checkout/{processor}/{oaics_...}
    GSS_PUBLISHABLE_KEY    pk_live_... (read from the checkout when omitted)
    GSS_CARD               {"number":"...","exp_month":"12","exp_year":"2030","cvc":"123"}
    GSS_BILLING            {"name":"...","email":"...","line1":"...","city":"...",
                            "state":"OR","postal_code":"97211","country":"US"}
    GSS_PROXY              http://user:pass@host:port
    GSS_CURRENCY           JPY (default USD)
    GSS_HAR_TELEMETRY      full (default) | off
    GSS_LLM_SOLVER_URL     passive hCaptcha solver, optional

Example:
    GSS_SESSION_FILE=~/session.txt GSS_CHECKOUT_URL=https://chatgpt.com/checkout/openai_llc/oaics_... \
    GSS_CARD='{"number":"4242424242424242","exp_month":"12","exp_year":"2030","cvc":"123"}' \
    .venv/bin/python scripts/plus_har_run.py
"""
from __future__ import annotations

import json
import logging
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "packages" / "contracts" / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "packages" / "core" / "src"))

from gss_core.plus_har_protocol import PlusHarProtocol  # noqa: E402


def _session() -> str:
    inline = os.environ.get("GSS_SESSION", "").strip()
    if inline:
        return inline
    path = os.environ.get("GSS_SESSION_FILE", "").strip()
    if path:
        return Path(path).expanduser().read_text().strip()
    raise SystemExit("set GSS_SESSION or GSS_SESSION_FILE")


def _json_env(name: str, default: dict) -> dict:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return dict(default)
    return json.loads(raw)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    card = _json_env("GSS_CARD", {})
    billing = _json_env("GSS_BILLING", {
        "name": "Fixture Holder", "email": "", "line1": "", "city": "",
        "state": "", "postal_code": "", "country": "US",
    })
    if not card.get("number"):
        raise SystemExit("set GSS_CARD with number/exp_month/exp_year/cvc")

    checkout_url = os.environ.get("GSS_CHECKOUT_URL", "").strip()
    if not checkout_url:
        raise SystemExit("set GSS_CHECKOUT_URL")

    engine = PlusHarProtocol(
        session=_session(),
        checkout_url=checkout_url,
        publishable_key=os.environ.get("GSS_PUBLISHABLE_KEY", "").strip(),
        card=card,
        billing=billing,
        proxy=os.environ.get("GSS_PROXY", "").strip() or None,
        currency=os.environ.get("GSS_CURRENCY", "USD"),
        country=billing.get("country", "US"),
        email=billing.get("email", ""),
        timeout=float(os.environ.get("GSS_TIMEOUT", "120")),
    )
    print(json.dumps({
        "device": {
            "os": engine.device.os_key,
            "ua": engine.device.ua,
            "screen": engine.device.frame_size,
            "timezone": engine.device.timezone,
            "guid": engine.device.guid,
            "muid": engine.device.muid,
            "sid": engine.device.sid,
            "stripe_js_id": engine.device.stripe_js_id,
            "tls": engine.device.impersonate,
        }
    }, ensure_ascii=False))
    try:
        outcome = engine.pay_from_url()
    finally:
        engine.close()
    print(json.dumps({
        "state": outcome.state,
        "intent_status": outcome.intent_status,
        "code": outcome.code,
        "decline_code": outcome.decline_code,
        "message": outcome.message,
        "payment_method_id": outcome.payment_method_id,
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
