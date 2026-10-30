"""Per-attempt randomised browser/device profile for the HAR-faithful Plus flow.

The 2026-10-29 capture was made on one fixed machine
(macOS / Chrome 156.0.8078.48 / 2560x1326@2x / Asia/Tokyo / en-US).  Replaying
those exact values would make every order share one device, so this module
builds a *fresh but internally consistent* device for each payment attempt:

    - OS + Chrome build + UA + User-Agent Client Hints all agree
    - screen / availHeight / devicePixelRatio / cores / memory agree
    - timezone + locale agree with the billing country (or an explicit override)
    - canvas / audio / plugin / connection signals look like a real browser
    - Stripe ids (guid / muid / sid / fingerprint id / stripe_js_id) are new

Everything that the HAR shows as a per-device value is generated here; every
value that the HAR shows as protocol structure is left to the flow module.
"""
from __future__ import annotations

import hashlib
import json
import os
import random
import secrets
import string
import time
import uuid
import base64

import pathlib

from dataclasses import asdict, dataclass, field
from typing import Any, Dict, Optional

import sys

from .countries import timezone_for

# Chrome releases around the capture (Oct 2026).  Each entry is a real
# versionhistory.googleapis.com build string; the flow only needs a coherent
# (major, full build) pair for UA + Client Hints.
# (UA build, curl_cffi impersonate target).  The TLS/HTTP2 fingerprint must
# match the UA Chrome major, so the two travel together.
_CHROME_BUILDS = [
    ("150.0.7871.189", "chrome150"),
    ("150.0.7871.181", "chrome150"),
    ("146.0.7680.180", "chrome146"),
    ("146.0.7680.177", "chrome146"),
    ("145.0.7632.116", "chrome145"),
    ("142.0.7444.175", "chrome142"),
    ("136.0.7103.114", "chrome136"),
    ("133.0.6943.141", "chrome133a"),
]

_OS_PROFILES: Dict[str, Dict[str, Any]] = {
    "macos": {
        "weight": 5,
        "platform": "MacIntel",
        "ch_platform": '"macOS"',
        "platform_versions": ["26.1.0", "26.0.1", "15.7.1", "15.6.1"],
        "arch": '"arm"',
        "bitness": '"64"',
        "screens": [(2560, 1326, 30, 2.0), (1728, 1117, 25, 2.0),
                    (1512, 982, 25, 2.0), (1440, 900, 25, 2.0),
                    (1920, 1080, 25, 1.0)],
        "cores": [8, 10, 12],
        "memory": [8, 16, 24],
        "ua": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
               "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{build} Safari/537.36"),
        "os_label": "MacOS",
        "tz_default": "Asia/Tokyo",
    },
    "windows": {
        "weight": 3,
        "platform": "Win32",
        "ch_platform": '"Windows"',
        "platform_versions": ["15.0.0", "10.0.0", "19.0.0"],
        "arch": '"x86"',
        "bitness": '"64"',
        "screens": [(1920, 1080, 40, 1.0), (2560, 1440, 40, 1.0),
                    (1536, 864, 40, 1.25), (1366, 768, 40, 1.0),
                    (3840, 2160, 40, 1.5)],
        "cores": [4, 8, 12, 16],
        "memory": [8, 16, 32],
        "ua": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
               "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{build} Safari/537.36"),
        "os_label": "Windows",
        "tz_default": "America/New_York",
    },
    "linux": {
        "weight": 1,
        "platform": "Linux x86_64",
        "ch_platform": '"Linux"',
        "platform_versions": [""],
        "arch": '"x86"',
        "bitness": '"64"',
        "screens": [(1920, 1080, 24, 1.0), (2560, 1440, 24, 1.0),
                    (1680, 1050, 24, 1.0)],
        "cores": [4, 8, 16],
        "memory": [8, 16],
        "ua": ("Mozilla/5.0 (X11; Linux x86_64) "
               "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{build} Safari/537.36"),
        "os_label": "Linux",
        "tz_default": "Etc/UTC",
    },
}

# HAR payload `a.e`: the plugin array Chrome reports on a desktop with the
# built-in PDF viewers.  Kept byte-identical to the capture; it is not a device
# identifier.
_PLUGINS = (
    "PDF Viewer,internal-pdf-viewer,application/pdf,pdf++text/pdf,pdf, "
    "Chrome PDF Viewer,internal-pdf-viewer,application/pdf,pdf++text/pdf,pdf, "
    "Chromium PDF Viewer,internal-pdf-viewer,application/pdf,pdf++text/pdf,pdf, "
    "Microsoft Edge PDF Viewer,internal-pdf-viewer,application/pdf,pdf++text/pdf,pdf, "
    "WebKit built-in PDF,internal-pdf-viewer,application/pdf,pdf++text/pdf,pdf"
)

_ALNUM = string.ascii_letters + string.digits


def _seg_b64(n: int = 32) -> str:
    """URL-safe base64 chunk, the shape the capture's frame host uses."""
    return base64.urlsafe_b64encode(secrets.token_bytes(n)).rstrip(b"=").decode()


def _hex20() -> str:
    return secrets.token_hex(10)


def _hex(n: int) -> str:
    return secrets.token_hex(n)


def _uuid() -> str:
    return str(uuid.uuid4())


def _stripe_id() -> str:
    """HAR shape: UUID body + 6 hex chars (42 chars), e.g.
    ``237d4886-0547-4227-89ae-35e884abc758a53e3c``."""
    return f"{uuid.uuid4()}{_hex(3)}"


def _suffix(n: int = 12) -> str:
    return "".join(secrets.choice(_ALNUM) for _ in range(n))


@dataclass
class DeviceProfile:
    """One coherent browser identity + the Stripe ids bound to it."""

    os_key: str
    platform: str
    os_label: str
    ua: str
    chrome_build: str
    chrome_major: str
    impersonate: str
    ch_platform: str
    ch_platform_version: str
    ch_arch: str
    ch_bitness: str
    screen_w: int
    screen_h: int
    avail_h: int
    dpr: float
    cores: int
    memory: int
    timezone: str
    timezone_offset_min: int
    locale: str
    languages: str
    canvas: str
    audio: str
    plugins: str
    rtt: int
    downlink: int
    effective_type: str
    # Stripe device identity
    guid: str
    muid: str
    sid: str
    fingerprint_id: str
    stripe_js_id: str
    elements_session_id: str
    elements_session_config_id: str
    sobj_id: str
    elements_group_id: str
    element_ids: Dict[str, str] = field(default_factory=dict)
    # The fingerprint iframe lives at one origin for the whole device; every
    # m.stripe.com beacon must reuse it (capture: zLPX3….g2u9-hqZ…).
    frame_host: str = ""
    frame_path: str = ""
    frame_token: str = ""
    created_at: float = field(default_factory=time.time)

    # ── construction ────────────────────────────────────────────────
    @classmethod
    def random(
        cls,
        *,
        country: str = "US",
        seed: Optional[str] = None,
        os_key: Optional[str] = None,
        timezone: Optional[str] = None,
        locale: Optional[str] = None,
    ) -> "DeviceProfile":
        rng = random.Random(seed) if seed is not None else random.SystemRandom()
        # The host has one real fingerprint (TLS, JS engine, fonts, canvas).  A
        # macOS UA on a Linux box is the cross-check hCaptcha/Radar catches, so
        # the OS follows the machine unless an operator pins another one.
        host = _host_fingerprint()
        chosen = os_key or os.environ.get("GSS_DEVICE_OS") or host.get("os") or _detect_os()
        os_key = chosen if chosen in _OS_PROFILES else _detect_os()
        prof = _OS_PROFILES[os_key]

        build, impersonate = rng.choice(_CHROME_BUILDS)
        major = build.split(".", 1)[0]
        sw, sh, chrome_px, dpr = rng.choice(prof["screens"])
        host_screen = host.get("screen") or []
        if len(host_screen) >= 4 and int(host_screen[0]) >= 1024 and int(host_screen[1]) >= 700:
            sw, sh, _avail, dpr = host_screen[:4]
            chrome_px = max(0, int(sh) - int(host_screen[2]))
        tz = timezone or host.get("timezone") or timezone_for(country) or prof["tz_default"]
        # The capture keeps the UI language at en-US even on a JP account;
        # only the timezone follows the billing country.
        loc = locale or "en-US"
        language = loc
        primary = loc.split("-")[0]

        return cls(
            os_key=os_key,
            platform=prof["platform"],
            os_label=prof["os_label"],
            # UA keeps the curl_cffi build (TLS must match); the host file only
            # supplies the machine's stable canvas/audio/cores/screen values.
            ua=prof["ua"].format(build=build),
            chrome_build=build,
            chrome_major=major,
            impersonate=impersonate,
            ch_platform=prof["ch_platform"],
            ch_platform_version=rng.choice(prof["platform_versions"]),
            ch_arch=prof["arch"],
            ch_bitness=prof["bitness"],
            screen_w=sw,
            screen_h=sh,
            avail_h=sh - chrome_px,
            dpr=dpr,
            cores=int(host.get("cores") or rng.choice(prof["cores"])),
            memory=int(host.get("memory") or rng.choice(prof["memory"])),
            timezone=tz,
            timezone_offset_min=_offset_minutes(tz),
            locale=loc,
            languages=f"{language},{primary}",
            canvas=host.get("canvas") or "".join(rng.choice("01") for _ in range(55)),
            audio=host.get("audio") or _hex(16),
            plugins=_PLUGINS,
            rtt=rng.choice([50, 100, 150, 200, 250]),
            downlink=rng.choice([10, 10, 10, 5]),
            effective_type="4g",
            guid=_stripe_id(),
            muid=_stripe_id(),
            sid=_stripe_id(),
            fingerprint_id=_hex(16),
            stripe_js_id=_uuid(),
            elements_session_id=f"elements_session_{_suffix()}",
            elements_session_config_id=_uuid(),
            sobj_id=f"sobj-{_uuid()}",
            elements_group_id=f"elements-{_uuid()}-{rng.randint(1000, 9999)}",
            element_ids={
                "payment": f"payment-{_uuid()}",
                "linkSignup": f"linkSignup-{_uuid()}",
                "expressCheckout": f"expressCheckout-{_uuid()}",
                "address": f"address-{_uuid()}",
            },
            frame_host=f"{_seg_b64(32)}.{_seg_b64(24)}",
            frame_path=f"/{_seg_b64(24)}/{_seg_b64(20)}",
            frame_token=_seg_b64(24),
        )

    def rotate_attempt_ids(self) -> None:
        """New page load, same device: guid/muid/sid stay, Stripe.js ids roll."""
        self.stripe_js_id = _uuid()
        self.elements_session_id = f"elements_session_{_suffix()}"
        self.elements_session_config_id = _uuid()
        self.sobj_id = f"sobj-{_uuid()}"
        self.elements_group_id = f"elements-{_uuid()}-{random.randint(1000, 9999)}"
        self.element_ids = {
            "payment": f"payment-{_uuid()}",
            "linkSignup": f"linkSignup-{_uuid()}",
            "expressCheckout": f"expressCheckout-{_uuid()}",
            "address": f"address-{_uuid()}",
        }
        self.created_at = time.time()

    # ── serialisation (kept in the order context between phases) ────
    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "DeviceProfile":
        known = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        return cls(**{k: v for k, v in (data or {}).items() if k in known})

    # ── derived header values ───────────────────────────────────────
    @property
    def sec_ch_ua(self) -> str:
        m = self.chrome_major
        return f'"Not:A-Brand";v="8", "Chromium";v="{m}", "Google Chrome";v="{m}"'

    @property
    def sec_ch_ua_full_version(self) -> str:
        return self.chrome_build

    @property
    def sec_ch_ua_full_version_list(self) -> str:
        b = self.chrome_build
        return f'"Not:A-Brand";v="8.0.0.0", "Chromium";v="{b}", "Google Chrome";v="{b}"'

    @property
    def frame_base(self) -> str:
        host = self.frame_host or f"{_seg_b64(32)}.{_seg_b64(24)}"
        path = self.frame_path or f"/{_seg_b64(24)}/{_seg_b64(20)}"
        return f"https://{host}{path}"

    @property
    def frame_size(self) -> str:
        # Capture shape: 2560w_1326h_30d_2r -> screen.width, screen.height,
        # (screen.height - availHeight), devicePixelRatio.
        return f"{self.screen_w}w_{self.screen_h}h_{self.chrome_px}d_{int(self.dpr)}r"

    @property
    def chrome_px(self) -> int:
        return self.screen_h - self.avail_h

    def client_hint_headers(self) -> Dict[str, str]:
        """`sec-ch-ua*` exactly as the capture sends them (no mobile hints on
        desktop, empty model string included)."""
        headers = {
            "sec-ch-ua": self.sec_ch_ua,
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": self.ch_platform,
        }
        if self.ch_platform_version:
            headers["sec-ch-ua-platform-version"] = f'"{self.ch_platform_version}"'
        headers["sec-ch-ua-arch"] = self.ch_arch
        headers["sec-ch-ua-bitness"] = self.ch_bitness
        headers["sec-ch-ua-full-version"] = f'"{self.sec_ch_ua_full_version}"'
        headers["sec-ch-ua-full-version-list"] = self.sec_ch_ua_full_version_list
        headers["sec-ch-ua-model"] = '""'
        return headers

    def accept_language(self) -> str:
        """`en-US,en;q=0.9` exactly as the capture sends it."""
        primary = self.locale.split("-")[0]
        value = f"{self.locale},{primary};q=0.9"
        if primary != "en":
            value += ",en;q=0.8"
        return value

    def web_headers(self, *, referer: str) -> Dict[str, str]:
        """Headers the capture sends to chatgpt.com (order irrelevant)."""
        return {
            "accept": "*/*",
            "accept-language": self.accept_language(),
            "origin": "https://chatgpt.com",
            "originator": "Codex Browser",
            "priority": "u=1, i",
            "referer": referer,
            **self.client_hint_headers(),
            "sec-fetch-dest": "empty",
            "sec-fetch-mode": "cors",
            "sec-fetch-site": "same-origin",
            "user-agent": self.ua,
            "x-openai-checkout-source": "chatgpt_web_merged",
            "x-openai-codex-window-type": "browser",
            "x-openai-rate-limit-join-workspace": "1",
            "x-openai-web-frontend": "codex_webview",
        }

    def stripe_headers(self) -> Dict[str, str]:
        """Headers the capture sends from js.stripe.com to api.stripe.com."""
        return {
            "accept": "application/json",
            "accept-language": self.accept_language(),
            "content-type": "application/x-www-form-urlencoded",
            "origin": "https://js.stripe.com",
            "priority": "u=1, i",
            "referer": "https://js.stripe.com/",
            "sec-ch-ua": self.sec_ch_ua,
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": self.ch_platform,
            "sec-fetch-dest": "empty",
            "sec-fetch-mode": "cors",
            "sec-fetch-site": "same-site",
            "sec-fetch-storage-access": "active",
            "user-agent": self.ua,
        }

    def fingerprint_headers(self) -> Dict[str, str]:
        """Headers for m.stripe.com / m.stripe.network."""
        return {
            "accept": "*/*",
            "accept-language": self.accept_language(),
            "content-type": "text/plain;charset=UTF-8",
            "origin": "https://m.stripe.network",
            "priority": "u=1, i",
            "referer": "https://m.stripe.network/",
            "sec-ch-ua": self.sec_ch_ua,
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": self.ch_platform,
            "sec-fetch-dest": "empty",
            "sec-fetch-mode": "cors",
            "sec-fetch-site": "same-site",
            "user-agent": self.ua,
            "cookie": f"m={self.guid}",
        }

    def payment_user_agent(self) -> str:
        # HAR: stripe.js/d2bf13370b; stripe-js-v3/d2bf13370b; payment-element; deferred-intent
        return (f"stripe.js/{self._js_hash()}; stripe-js-v3/{self._js_hash()}; "
                "payment-element; deferred-intent")

    def _js_hash(self) -> str:
        # The capture pins the deployed stripe.js build hash.  Derive a stable
        # 10-hex value per device so two orders never share one build hash.
        return hashlib.sha256(f"stripe-js:{self.fingerprint_id}".encode()).hexdigest()[:10]


def _detect_os() -> str:
    if sys.platform.startswith("linux"):
        return "linux"
    if sys.platform == "darwin":
        return "macos"
    if sys.platform.startswith("win"):
        return "windows"
    return "linux"


_HOST_CACHE: Optional[Dict[str, Any]] = None


def _device_store_path() -> pathlib.Path:
    explicit = os.environ.get("GSS_DEVICE_STORE", "").strip()
    if explicit:
        return pathlib.Path(explicit)
    data_dir = os.environ.get("GSS_DATA_DIR", "").strip() or "data"
    return pathlib.Path(data_dir) / "device_store.json"


def _read_store(path: "pathlib.Path") -> Dict[str, Any]:
    try:
        with path.open(encoding="utf-8") as stream:
            data = json.load(stream)
        return data if isinstance(data, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def _write_store(path: "pathlib.Path", data: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path.with_suffix(".tmp")
    with tmp.open("w", encoding="utf-8") as stream:
        json.dump(data, stream, ensure_ascii=False, indent=1)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(tmp, path)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def device_for_account(account_id: str, country: str = "US") -> "DeviceProfile":
    """One device per account, reused by that account's later orders.

    Persisting per account is the whole point: a returning buyer shows the same
    guid/muid/sid.  A single device shared by every account would tell Stripe
    that one machine pays for dozens of unrelated accounts, so the store keys on
    the account and refuses to hand one device to a different owner.
    """
    key = str(account_id or "").strip()
    if not key:
        return DeviceProfile.random(country=country)
    path = _device_store_path()
    store = _read_store(path)
    entry = store.get(key)
    if isinstance(entry, dict):
        owner = str(entry.pop("owner", key))
        entry.pop("saved_at", None)
        if owner == key:
            try:
                profile = DeviceProfile.from_dict(entry)
                profile.rotate_attempt_ids()
                return profile
            except Exception:  # noqa: BLE001
                pass
    profile = DeviceProfile.random(country=country)
    payload = profile.to_dict()
    payload["owner"] = key
    payload["saved_at"] = int(time.time())
    store[key] = payload
    _write_store(path, store)
    return profile


def forget_device(account_id: str) -> None:
    path = _device_store_path()
    store = _read_store(path)
    if store.pop(str(account_id or ""), None) is not None:
        _write_store(path, store)


def _host_fingerprint() -> Dict[str, Any]:
    """Read the machine's own fingerprint file when the operator captured one.

    `scripts/fingerprint_probe.js` writes it; `GSS_DEVICE_FINGERPRINT` points at
    it.  Without the file the profile falls back to the host OS only.
    """
    global _HOST_CACHE
    if _HOST_CACHE is not None:
        return _HOST_CACHE
    path = os.environ.get("GSS_DEVICE_FINGERPRINT", "").strip()
    data: Dict[str, Any] = {}
    if path:
        try:
            with open(path, encoding="utf-8") as stream:
                raw = json.load(stream)
            if isinstance(raw, dict):
                data = raw
        except Exception:  # noqa: BLE001
            data = {}
    if data:
        canvas_seed = str(data.get("canvas_seed") or "")
        if canvas_seed:
            bits = bin(int(canvas_seed, 16))[2:].rjust(64, "0")[:55]
            data["canvas"] = bits
        audio_seed = str(data.get("audio_seed") or "")
        if audio_seed:
            import hashlib

            data["audio"] = hashlib.sha256(audio_seed.encode()).hexdigest()[:32]
        if data.get("platform"):
            data["os"] = {"MacIntel": "macos", "Win32": "windows"}.get(
                str(data["platform"]), "linux")
    else:
        data = {"os": _detect_os()}
    _HOST_CACHE = data
    return data


def _offset_minutes(tz_name: str) -> int:
    """UTC offset of the emulated timezone, in minutes (0 when unknown)."""
    try:
        from datetime import datetime
        from zoneinfo import ZoneInfo

        offset = datetime.now(ZoneInfo(tz_name)).utcoffset()
        return int(offset.total_seconds() // 60) if offset else 0
    except Exception:  # noqa: BLE001 - unknown zone must not break the flow
        return 0


def g_state_cookie(profile: DeviceProfile) -> str:
    """The `g_state` cookie the ChatGPT web app writes on first paint."""
    now_ms = int(time.time() * 1000)
    payload = {
        "i_l": 0,
        "i_ll": now_ms,
        "i_b": "".join(secrets.choice(_ALNUM) for _ in range(43)),
        "i_e": {"enable_itp_optimization": 24},
        "i_et": now_ms,
    }
    return json.dumps(payload, separators=(",", ":"))


def oai_telemetry_header(elapsed_ms: Optional[float] = None) -> str:
    """`oai-telemetry` as observed: ``[1,null]`` before the form is used and a
    timing vector after the payment attempt."""
    if elapsed_ms is None:
        return "[1,null]"
    seconds = max(0.1, float(elapsed_ms) / 1000.0)
    return f"[1,{seconds:.1f},20,473,3,2,0,{int(seconds)}]"


__all__ = ["DeviceProfile", "g_state_cookie", "oai_telemetry_header"]
