"""Extract the Stripe JS telemetry event stream from a HAR into a template catalog.

The 2026-10-29 Plus capture contains 88 `r.stripe.com/b` posts carrying 291
events.  This script keeps the exact event order and parameter shape, but
replaces every captured device / session / credential value with a `{{token}}`
placeholder so the runtime can re-materialise the stream on a freshly
randomised device, so the captured one is never replayed.

Usage:
    python scripts/har_extract_stripe_events.py /path/to/1029pluschatgpt.com.har \
        packages/core/src/gss_core/stripe_events.json
"""
from __future__ import annotations

import hashlib
import json
import re
import sys
import uuid
from collections import Counter
from pathlib import Path
from urllib.parse import parse_qsl

UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)
HEX32_RE = re.compile(r"^[0-9a-f]{32}$", re.I)

# key -> placeholder.  Keys absent here keep their captured value unless the
# value itself looks like a per-device identifier (UUID / 32-hex).
KEY_TOKENS = {
    "created": "{{created}}",
    "event_id": "{{event_id}}",
    "stripe_js_id": "{{stripe_js_id}}",
    "key": "{{key}}",
    "public_key": "{{key}}",
    "publishable_key": "{{key}}",
    "elements_session_id": "{{elements_session_id}}",
    "elements_session_config_id": "{{elements_session_config_id}}",
    "stripe_obj_id": "{{sobj_id}}",
    "customer_id": "{{customer_id}}",
    "customer": "{{customer_id}}",
    "email": "{{email}}",
    "email_address": "{{email}}",
    "passive_captcha_token": "{{hcaptcha_token}}",
    "os": "{{os}}",
    "browserFamily": "{{browser}}",
    "version": "{{version}}",
    "browser_timezone": "{{timezone}}",
    "frame_width": "{{frame_w}}",
    "frame_height": "{{frame_h}}",
    "element_id": "{{element_id}}",
    "element_ids": "{{element_ids}}",
    "options-groupId": "{{elements_group_id}}",
    "elements_group_id": "{{elements_group_id}}",
    "controller_load_time": "{{controller_load_time}}",
    "deploy_status_time_to_fetch_ms": "{{deploy_ms}}",
    "connection_rtt": "{{rtt}}",
    "connection_downlink": "{{downlink}}",
    "timeline": "{{timeline}}",
    "paymentUserAgent": "{{payment_user_agent}}",
    "ip": "{{ip}}",
    "currency": "{{currency}}",
    "amount": "{{amount}}",
    "country": "{{country}}",
}

# Values that must never survive extraction even under an unknown key.
SENSITIVE_KEY_RE = re.compile(
    r"(email|secret|token|cookie|card|iban|phone|address|postal|user_agent|userAgent|"
    r"billing_name|cardholder|full_name|first_name|last_name|account_holder)",
    re.I,
)


def _scrub(value, key: str = ""):
    if isinstance(value, dict):
        return {k: _scrub(v, k) for k, v in value.items()}
    if isinstance(value, list):
        return [_scrub(v, key) for v in value]
    token = KEY_TOKENS.get(key)
    if token:
        return token
    if not isinstance(value, str):
        return value
    if SENSITIVE_KEY_RE.search(key or ""):
        return "{{scrubbed}}"
    if UUID_RE.match(value):
        return "{{uuid}}"
    if HEX32_RE.match(value):
        return "{{hex32}}"
    if value.startswith(("pi_", "seti_", "pm_", "ctoken_", "cus_", "cuss_secret_", "oaics_", "cs_live_")):
        return "{{stripe_id}}"
    if value.startswith("P1_"):
        return "{{hcaptcha_token}}"
    return value


def main(har_path: str, out_path: str) -> None:
    raw = Path(har_path).read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    har = json.loads(raw)
    entries = har["log"]["entries"]

    sequence = []
    defaults = None
    for index, entry in enumerate(entries, 1):
        request = entry.get("request") or {}
        if "r.stripe.com" not in (request.get("url") or ""):
            continue
        body = ((request.get("postData") or {}).get("text")) or ""
        fields = dict(parse_qsl(body, keep_blank_values=True))
        events = json.loads(fields.get("events") or "[]")
        num_requests = int(fields.get("num_requests") or 0)
        for event in events:
            template = _scrub(event)
            if defaults is None:
                defaults = dict(template)
            diff = {k: v for k, v in template.items()
                    if defaults.get(k) != v and k != "event_name"}
            sequence.append({"name": template.get("event_name"), "post": index,
                             "num_requests": num_requests, "params": diff})

    names = Counter(item["name"] for item in sequence)
    catalog = {
        "source": {
            "har_sha256": digest,
            "har_entries": len(entries),
            "stripe_event_posts": sum(
                1 for e in entries if "r.stripe.com" in ((e.get("request") or {}).get("url") or "")
            ),
            "events": len(sequence),
            "first_post": sequence[0]["post"] if sequence else 0,
            "last_post": sequence[-1]["post"] if sequence else 0,
            "unique_events": len(names),
        },
        "defaults": defaults or {},
        "sequence": sequence,
    }
    Path(out_path).write_text(json.dumps(catalog, separators=(",", ":"), ensure_ascii=False) + "\n")
    print(f"wrote {out_path}: {len(sequence)} events, {len(names)} unique, "
          f"{Path(out_path).stat().st_size} bytes")


if __name__ == "__main__":
    if len(sys.argv) != 3:
        raise SystemExit(__doc__)
    main(sys.argv[1], sys.argv[2])
