"""Stripe.js / ChatGPT telemetry replayed on a randomised device.

The 2026-10-29 capture shows four telemetry surfaces on the payment path:

    m.stripe.com/6      fingerprint registration + mouse-timing beacons
    r.stripe.com/b      291 batched Elements/SDK events over 88 POSTs
    /ces/v1/rgstr       ChatGPT (statsig) client register, gzip JSON body
    /backend-api/sentinel/ping   sentinel keep-alive after each /sentinel/req

This module emits all four with the capture's exact structure, header set,
ordering and batching.  Device-, session- and account-specific values come from
:class:`gss_core.har_device.DeviceProfile` (fresh per payment attempt) in
of the captured machine.
"""
from __future__ import annotations

import base64
import gzip
import json
import random
import time
import urllib.parse
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from .har_device import DeviceProfile

_CATALOG_PATH = Path(__file__).with_name("stripe_events.json")
_CATALOG: Optional[Dict[str, Any]] = None

M_STRIPE = "https://m.stripe.com"
R_STRIPE = "https://r.stripe.com"
CES_REGISTER = "https://chatgpt.com/ces/v1/rgstr"
SENTINEL_PING = "https://chatgpt.com/backend-api/sentinel/ping"

# The statsig key the ChatGPT web client uses.  It is a public client key that
# ships in the page bundle, not a secret.
STATSIG_CLIENT_KEY = "client-sYWqzCYMRkUg4DqqiZcR5DGTNl2iD7zNJY0HoeDLzxR"
STATSIG_SDK_VERSION = "3.35.1"
STATSIG_SDK_TYPE = "javascript-client-react"

# HAR entry numbers of the r.stripe.com/b POSTs; the flow flushes "everything
# up to this point in the capture" after the matching business step.
PHASE_ELEMENTS = 98
PHASE_CAPTCHA = 160
PHASE_CARD_ENTRY = 215
PHASE_TAXES = 266
PHASE_TAX_ID = 293
PHASE_TOKEN = 329
PHASE_CONFIRM = 343


def _catalog() -> Dict[str, Any]:
    global _CATALOG
    if _CATALOG is None:
        _CATALOG = json.loads(_CATALOG_PATH.read_text())
    return _CATALOG


def _b64_url(data: Dict[str, Any]) -> str:
    raw = json.dumps(data, separators=(",", ":"), ensure_ascii=False)
    return base64.b64encode(urllib.parse.quote(raw, safe="").encode()).decode()


def _seg(n: int = 32) -> str:
    import secrets

    return base64.urlsafe_b64encode(secrets.token_bytes(n)).rstrip(b"=").decode()


@dataclass
class TelemetryContext:
    """Values the event catalog needs from the live checkout."""

    device: DeviceProfile
    key: str
    currency: str
    amount: int
    account_id: str = ""
    device_id: str = ""
    email: str = ""
    country: str = ""
    customer_id: str = ""
    hcaptcha_token: str = ""
    page_url: str = "https://chatgpt.com/"
    started_at: float = field(default_factory=time.time)


class StripeTelemetry:
    """Fingerprint beacons + the batched r.stripe.com/b event stream."""

    def __init__(self, http: Any, ctx: TelemetryContext) -> None:
        self.http = http
        self.ctx = ctx
        self.device = ctx.device
        self._cursor = 0
        self._tick = 0.0
        self.registered = False
        self.guid = ctx.device.guid
        self.muid = ctx.device.muid
        self.sid = ctx.device.sid
        self.sent_posts = 0
        self.sent_events = 0
        self._frame_cached = ""
        # Hard wall: a residential proxy can spend minutes on 88 posts, so the
        # payment must not wait for the tail of the event stream.
        self.deadline = 0.0

    # ── m.stripe.com/6 ─────────────────────────────────────────────
    def _frame_base(self) -> str:
        return self.device.frame_base

    def _frame_url(self) -> str:
        """One iframe URL per attempt, reused by every beacon (capture: 186 == 184.b)."""
        if self._frame_cached:
            return self._frame_cached
        base = self._frame_base()
        first = self.device.frame_token or _seg(20)
        second, q1, q2, v1, v2 = _seg(18), _seg(16), _seg(14), _seg(20), _seg(16)
        self._frame_cached = f"{base}/{first}/{second}?{q1}={v1}&{q2}={v2}"
        return self._frame_cached

    def _fingerprint_body(self, *, with_signals: bool, with_ids: bool) -> Dict[str, Any]:
        d = self.device
        signals = {
            "a": {"v": "true", "t": 0},
            "b": {"v": "false", "t": 0},
            "c": {"v": d.languages, "t": 0.1},
            "d": {"v": d.platform, "t": 0},
            "e": {"v": d.plugins, "t": 0},
            "f": {"v": d.frame_size, "t": 0},
            "g": {"v": str(d.cores), "t": 0},
            "h": {"v": "false", "t": 0},
            "i": {"v": "sessionStorage-enabled, localStorage-enabled", "t": round(random.uniform(0.4, 2.0), 1)},
            "j": {"v": d.canvas, "t": round(random.uniform(2.0, 12.0), 1)},
            "k": {"v": "", "t": 0},
            "l": {"v": d.ua, "t": 0},
            "m": {"v": "", "t": 0},
            "n": {"v": "false", "t": round(random.uniform(1.5, 4.0), 1)},
            "o": {"v": d.audio, "t": round(random.uniform(1.0, 3.0), 1)},
        }
        return {
            "v2": 2 if with_signals else 1,
            "id": self.device.fingerprint_id,
            "t": round(random.uniform(3.0, 15.0), 1),
            "tag": "$npm_package_version",
            "src": "js",
            "a": signals if with_signals else None,
            "b": {
                "a": self._frame_base() + "/",
                "b": self._frame_url(),
                "c": self.device.frame_token or _seg(24),
                "d": self.muid if with_ids else self.muid,
                "e": self.sid if with_ids else self.sid,
                "f": False,
                "g": True,
                "h": True,
                "i": ["location"],
                "j": [],
                "n": 9.800000011920929,
                "u": "chatgpt.com",
                "v": "chatgpt.com",
                "w": f"{int(time.time() * 1000)}:{_sha256_hex()}",
            },
            "h": _hex(10),
        }

    def _mouse_body(self, source: str, data: List[int]) -> Dict[str, Any]:
        return {
            "muid": self.muid,
            "sid": self.sid,
            "url": self._frame_url(),
            "source": source,
            "data": data,
        }

    def _post_fingerprint(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        headers = self.device.fingerprint_headers()
        headers["cookie"] = f"m={self.guid}"
        try:
            response = self.http.post(f"{M_STRIPE}/6", content=_b64_url(payload),
                                      headers=headers, timeout=15.0)
            if int(getattr(response, "status_code", 0) or 0) == 200 and getattr(response, "content", b""):
                try:
                    body = response.json()
                except Exception:  # noqa: BLE001
                    body = {}
                for name in ("guid", "muid", "sid"):
                    value = str(body.get(name) or "")
                    if value:
                        setattr(self, name, value)
        except Exception:  # noqa: BLE001 - telemetry must never block payment
            pass
        return payload

    def register(self) -> None:
        """The four beacons the capture sends while Elements boots."""
        self._post_fingerprint(self._fingerprint_body(with_signals=False, with_ids=True))
        self._post_fingerprint(self._fingerprint_body(with_signals=True, with_ids=True))
        self._post_fingerprint(self._mouse_body("mouse-timings-10-v2", []))
        self._post_fingerprint(self._mouse_body(
            "mouse-timings-10",
            [random.randint(20, 900) for _ in range(10)],
        ))
        self._adopt_server_ids()
        self.registered = True

    def _adopt_server_ids(self) -> None:
        """m.stripe.com returns its own guid/muid/sid; the confirmation token
        must carry those exact values, not the locally generated ones."""
        self.device.guid = self.guid
        self.device.muid = self.muid
        self.device.sid = self.sid

    def beat(self, times: int = 2) -> None:
        """Post-payment beacons (capture: entries 351 and 372)."""
        for _ in range(max(1, times)):
            self._post_fingerprint(self._fingerprint_body(with_signals=False, with_ids=True))
        self._adopt_server_ids()

    # ── js.stripe.com / m.stripe.network asset loads ───────────────
    ASSETS = (
        ("https://js.stripe.com/basil/stripe.js", "https://chatgpt.com/"),
        ("https://js.stripe.com/v3/controller-with-preconnect-9a777715ca4969506d9d7a6674dae5b6.html",
         "https://chatgpt.com/"),
        ("https://js.stripe.com/v3/fingerprinted/js/shared-690527f8d3c420a8fcc6f1886cb6306e.js",
         "https://js.stripe.com/v3/controller-with-preconnect-9a777715ca4969506d9d7a6674dae5b6.html"),
        ("https://js.stripe.com/v3/hcaptcha-invisible-958211fd0bbfc01588e6b70f4697cd72.html",
         "https://js.stripe.com/"),
        ("https://m.stripe.network/inner.html", "https://js.stripe.com/"),
        ("https://m.stripe.network/out-4.5.45.js", "https://m.stripe.network/inner.html"),
    )

    def warm_assets(self) -> None:
        """The SDK/iframe resources the capture loads before fingerprinting."""
        for url, referer in self.ASSETS:
            headers = (self.device.fingerprint_headers() if "m.stripe.network" in url
                       else {**self.device.stripe_headers(), "accept": "*/*"})
            headers["referer"] = referer
            try:
                self.http.get(url, headers=headers, timeout=10.0)
            except Exception:  # noqa: BLE001
                pass

    # ── r.stripe.com/b ─────────────────────────────────────────────
    def _timeline(self) -> str:
        d = self.device
        outer = []
        t = 0.0
        for _ in range(6):
            t += random.uniform(80, 900)
            outer.append({"k": "longtask", "t": round(t, 1), "d": round(random.uniform(40, 160), 1),
                          "n": "self", "i": "[redacted]"})
        payload = {
            d.stripe_js_id: {
                d.sobj_id: {
                    "__null__": {
                        "__null__": {
                            "outer": outer,
                            "inner": [],
                        }
                    }
                }
            }
        }
        return json.dumps(payload, separators=(",", ":"))

    def _tokens(self) -> Dict[str, str]:
        d = self.device
        c = self.ctx
        self._tick += random.uniform(0.05, 0.6)
        return {
            "{{created}}": str(int((c.started_at + self._tick) * 1000)),
            "{{event_id}}": str(uuid.uuid4()),
            "{{uuid}}": str(uuid.uuid4()),
            "{{hex32}}": _hex(16),
            "{{stripe_id}}": f"pi_{_alnum(24)}",
            "{{key}}": c.key,
            "{{stripe_js_id}}": d.stripe_js_id,
            "{{elements_session_id}}": d.elements_session_id,
            "{{elements_session_config_id}}": d.elements_session_config_id,
            "{{sobj_id}}": d.sobj_id,
            "{{elements_group_id}}": d.elements_group_id,
            "{{element_id}}": d.element_ids["payment"],
            "{{element_ids}}": json.dumps(list(d.element_ids.values()), separators=(",", ":")),
            "{{timeline}}": self._timeline(),
            "{{payment_user_agent}}": d.payment_user_agent(),
            "{{customer_id}}": c.customer_id,
            "{{email}}": c.email,
            "{{hcaptcha_token}}": c.hcaptcha_token,
            "{{os}}": d.os_label,
            "{{browser}}": "Chrome",
            "{{version}}": d._js_hash(),
            "{{timezone}}": d.timezone,
            "{{frame_w}}": str(d.screen_w),
            "{{frame_h}}": str(d.avail_h),
            "{{rtt}}": str(d.rtt),
            "{{downlink}}": str(d.downlink),
            "{{currency}}": c.currency.lower(),
            "{{amount}}": str(c.amount),
            "{{country}}": c.country,
            "{{ip}}": "",
            "{{controller_load_time}}": str(int((c.started_at - random.uniform(0.2, 2.0)) * 1000)),
            "{{deploy_ms}}": str(random.randint(40, 260)),
        }

    def _materialise(self, value: Any, tokens: Dict[str, str]) -> Any:
        if isinstance(value, dict):
            out = {}
            for key, item in value.items():
                filled = self._materialise(item, tokens)
                if filled is _DROP:
                    continue
                out[key] = filled
            return out
        if isinstance(value, list):
            return [self._materialise(item, tokens) for item in value]
        if isinstance(value, str):
            if value == "{{scrubbed}}":
                return _DROP
            if value == "{{created}}":
                return int(tokens["{{created}}"])
            for token, replacement in tokens.items():
                if token in value:
                    value = value.replace(token, replacement)
            return value
        return value

    def _next_post_group(self, limit: int) -> Optional[List[Dict[str, Any]]]:
        catalog = _catalog()
        sequence = catalog["sequence"]
        defaults = catalog["defaults"]
        if self._cursor >= len(sequence):
            return None
        post = sequence[self._cursor]["post"]
        if post > limit:
            return None
        events = []
        while self._cursor < len(sequence) and sequence[self._cursor]["post"] == post:
            item = sequence[self._cursor]
            event = dict(defaults)
            event.update(item["params"])
            event["event_name"] = item["name"]
            events.append(event)
            self._cursor += 1
        return events

    def emit_through(self, limit: int) -> int:
        """Flush every captured event whose POST happened at or before `limit`."""
        sent = 0
        failures = 0
        while True:
            if self.deadline and time.time() > self.deadline:
                # Out of budget: drop the remaining events, keep the payment path.
                self._cursor = len(_catalog()["sequence"])
                return sent
            if failures >= 3:
                # A dead r.stripe.com must not add 88 timeouts to the payment.
                self._cursor = len(_catalog()["sequence"])
                return sent
            events = self._next_post_group(limit)
            if not events:
                return sent
            tokens = self._tokens()
            materialised = [self._materialise(event, tokens) for event in events]
            body = urllib.parse.urlencode({
                "client_id": "stripe-js",
                "num_requests": str(len(materialised)),
                "events": json.dumps(materialised, separators=(",", ":")),
            })
            try:
                self.http.post(f"{R_STRIPE}/b", content=body,
                               headers=self.device.stripe_headers(), timeout=10.0)
                failures = 0
            except Exception:  # noqa: BLE001
                failures += 1
            self.sent_posts += 1
            self.sent_events += len(materialised)
            sent += len(materialised)

    def flush(self) -> int:
        return self.emit_through(10 ** 9)

    # ── ChatGPT statsig register ───────────────────────────────────
    def ces_register(self, http: Any = None, auth_headers: Optional[Dict[str, str]] = None) -> None:
        c = self.ctx
        now_ms = int(time.time() * 1000)
        session_id = str(uuid.uuid4())
        user_id = f"user-{_alnum(24)}"
        device_id = c.device_id or c.device.guid
        account_id = c.account_id or device_id
        payload = {
            "events": [{
                "eventName": "__protobuf_structured_event__",
                "metadata": {
                    "eventId": str(uuid.uuid4()),
                    "eventCreatedAt": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()) + f".{now_ms % 1000:03d}Z",
                    "eventType": "web",
                    "deviceParams": {},
                    "eventParams": {
                        "@type": ("openai.buf.dev/openai/protobuf-analytics-events/"
                                  "protobuf_analytics_events.v1.CodexAppWindowViewportResized"),
                        "width": c.device.screen_w,
                        "height": c.device.avail_h,
                        "codexMetadata": {
                            "authMethod": "chatgpt",
                            "codexWindowType": "browser",
                            "codexBuildFlavor": "prod",
                        },
                    },
                },
                "user": {
                    "userID": user_id,
                    "customIDs": {
                        "ads_segment_id": str(random.randint(1000, 9999)),
                        "WebAnonymousCookieID": device_id,
                        "account_id": account_id,
                        "workspace_id": account_id,
                        "stableID": device_id,
                        "DeviceId": device_id,
                        "source_surface_stable_id": device_id,
                    },
                    "email": c.email,
                    "userAgent": c.device.ua,
                    "country": c.country,
                    "locale": c.device.locale,
                    "appVersion": f"26.{random.randint(1000, 1100)}.{random.randint(90000, 99999)}",
                    "custom": {
                        "ads_segment_id": random.randint(1000, 9999),
                        "client_type": "web",
                        "is_fedramp_compliant_workspace": False,
                        "account_id": account_id,
                        "is_paid": False,
                        "is_openai_internal": False,
                        "plan_type": "free",
                        "brand_name": "codex",
                        "desktop_app_beta_enabled": False,
                        "data_residency": "no_constraint",
                        "auth_status": "logged_in",
                        "auth_method": "chatgpt",
                        "systemName": "web",
                        "compute_residency": "no_constraint",
                        "codex_window_type": "browser",
                        "codex_build_flavor": "prod",
                        "is_test_user": False,
                    },
                    "statsigEnvironment": {"tier": "production"},
                },
                "time": now_ms,
                "statsigMetadata": {"currentPage": c.page_url},
            }],
            "statsigMetadata": {
                "sdkVersion": STATSIG_SDK_VERSION,
                "sdkType": STATSIG_SDK_TYPE,
                "appIdentifier": "codex-browser",
                "locale": c.device.locale,
                "systemName": "web",
                "stableID": device_id,
                "sessionID": session_id,
                "fallbackUrl": None,
            },
        }
        query = urllib.parse.urlencode({
            "k": STATSIG_CLIENT_KEY,
            "st": STATSIG_SDK_TYPE,
            "sv": STATSIG_SDK_VERSION,
            "t": str(now_ms),
            "sid": session_id,
            "ec": "1",
            "gz": "1",
        })
        headers = {
            **c.device.web_headers(referer="https://chatgpt.com/"),
            "content-type": "application/json",
            "statsig-event-count": "1",
            "statsig-retry-count": "0",
            "statsig-sdk-type": STATSIG_SDK_TYPE,
            "statsig-sdk-version": STATSIG_SDK_VERSION,
        }
        if auth_headers:
            headers.update(auth_headers)
        target = http if http is not None else self.http
        try:
            target.post(f"{CES_REGISTER}?{query}",
                        content=gzip.compress(json.dumps(payload).encode()),
                        headers=headers, timeout=15.0)
        except Exception:  # noqa: BLE001
            pass


class _Drop:
    pass


_DROP = _Drop()


def _hex(n: int) -> str:
    import secrets

    return secrets.token_hex(n)


def _alnum(n: int) -> str:
    import secrets
    import string

    return "".join(secrets.choice(string.ascii_letters + string.digits) for _ in range(n))


def _sha256_hex() -> str:
    import hashlib
    import os

    return hashlib.sha256(os.urandom(32)).hexdigest()


def sentinel_ping(http: Any, device: DeviceProfile, *, timeout: float = 10.0) -> None:
    """The keep-alive the sentinel iframe fires after each /sentinel/req."""
    headers = {
        **device.web_headers(referer="https://chatgpt.com/"),
        "cache-control": "no-cache",
        "pragma": "no-cache",
    }
    try:
        http.post(SENTINEL_PING, content=b"", headers=headers, timeout=timeout)
    except Exception:  # noqa: BLE001
        pass


def catalog_info() -> Dict[str, Any]:
    return dict(_catalog()["source"])


__all__ = [
    "StripeTelemetry",
    "TelemetryContext",
    "sentinel_ping",
    "catalog_info",
    "PHASE_ELEMENTS",
    "PHASE_CAPTCHA",
    "PHASE_CARD_ENTRY",
    "PHASE_TAXES",
    "PHASE_TAX_ID",
    "PHASE_TOKEN",
    "PHASE_CONFIRM",
]
