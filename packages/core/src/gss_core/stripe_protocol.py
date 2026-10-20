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
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/146.0.0.0 Safari/537.36"
)
RUNTIME_HASH = "5412f474d5"
PUBLISHABLE_KEY = os.environ.get(
    "STRIPE_PUBLISHABLE_KEY",
    "pk_live_51HOrSwC6h1nxGoI3lTAgRjYVrz4dU3fVOabyCcKR3pbEJguCVAlqCxdxCUvoRh1XWwRacViovU3kLKvpkjh7IqkW00iXQsjo3n",
)
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
    ) -> None:
        self.proxy = proxy or None
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
        self._solver_session = ""
        self._solver_ua = ""
        self._email_cache = ""
        # 由浏览器 Stripe 输入框产出的 pm_：给定时跳过服务端 tokenize。
        # 卡数据经过 Stripe iframe（真实用户路径），hCaptcha/Radar 判定更宽松。
        self.external_pm = ""
        self._solver_url = os.environ.get("HCAPTCHA_SOLVER_URL", "").rstrip("/")

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
                    impersonate="chrome124",
                    proxies=proxies,
                    timeout=self.timeout,
                    headers={"Accept": "application/json"},
                )
                log("   使用 curl_cffi（Chrome TLS 指纹）")
            else:
                self._client = httpx.Client(
                    proxy=self.proxy, timeout=self.timeout,
                    headers={"User-Agent": UA, "Accept": "application/json"},
                )
                log("   使用 httpx（默认 TLS 指纹）")
        return self._client

    def _captcha_http(self) -> httpx.Client:
        # 打码服务直连，不经过订单代理
        if self._captcha_client is None:
            self._captcha_client = httpx.Client(
                timeout=60, headers={"User-Agent": UA, "Content-Type": "application/json"}
            )
        return self._captcha_client

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
            "User-Agent": UA,
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
        # 真实服务器环境是 Linux，浏览器 UA 声称 Windows 会造成矛盾
        # 这里与 UA 保持一致，用 Windows 平台串
        platform_str = "Win32"
        locale = random.choice(["en-US", "en-US", "en-US", "en-GB"])
        sw, sh, dpr = random.choice(
            [(1920, 1080, 1), (1536, 864, 1.25), (2560, 1440, 1), (1440, 900, 1)]
        )
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
                    "l": {"v": UA, "t": 0},
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
            "User-Agent": UA,
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
             "User-Agent": UA},
        )
        ok = r.status_code == 200
        log(f"   allowed_origins → {r.status_code}")
        return r.json() if ok else {}

    def init_checkout(self, cs_id: str) -> tuple[dict[str, Any], CheckoutContext]:
        log("② init")
        js_id = str(uuid.uuid4())
        locale, tz = self._align_locale(self.proxy)
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
                newest = (r.json() or {}).get("config_id")
                if newest:
                    ctx.config_id = newest
                    log(f"   config_id 更新为 {ctx.config_id[:12]}")
            except Exception:
                pass
        else:
            log(f"   {r.text[:100]}")


    def _solver_open_session(self, timeout: int = 180) -> str:
        """开一个 solver 浏览器会话，供 passive 与 challenge 共用。

        为什么要共用：Stripe 的 verify_challenge 校验 token 与发起支付的环境
        同源（同一出口 IP、同一浏览器指纹）。passive 与 challenge 各开一个
        会话就等于两个不同出口，challenge 必被拒
        （payment_intent_authentication_failure）。
        """
        if self._solver_session or not self._solver_url:
            return self._solver_session
        try:
            r = httpx.post(
                f"{self._solver_url}/session",
                json={"proxy": self.proxy or "", "ua": UA},
                timeout=timeout,
            )
            if r.status_code == 200:
                j = r.json()
                self._solver_session = str(j.get("session_id") or "")
                self._solver_ua = str(j.get("ua") or "")
                log(f"   solver 会话 {self._solver_session[:8]} "
                    f"proxy={'有' if self.proxy else '无'}")
                return self._solver_session
            log(f"   solver 开会话 HTTP {r.status_code}: {r.text[:100]}")
        except Exception as e:
            log(f"   solver 开会话失败: {str(e)[:80]}")
        return ""

    def _solver_close_session(self) -> None:
        if not self._solver_session or not self._solver_url:
            return
        sid = self._solver_session
        self._solver_session = ""
        try:
            httpx.delete(f"{self._solver_url}/session/{sid}", timeout=30)
        except Exception:
            pass

    def _solve_via_browser(self, site_key: str, rqdata: str,
                           timeout: int = 180) -> dict[str, str] | None:
        """通过 hCaptcha solver 服务产出真实 token。

        两种接入方式，按优先级：
          ① HTTP 服务（推荐）：POST {solver_url}/solve（带会话）
          ② 共享卷（兜底）：写 cfg 文件 → 轮询读 token 文件

        为什么需要浏览器：所有 stripe 域的 CSP 都缺 'unsafe-eval'，
        hCaptcha 的 wasm 只能在 newassets.hcaptcha.com 的 iframe 内运行。
        详见 tools/hcaptcha-solver/README.md
        """
        if not site_key:
            return None

        solver_url = os.environ.get("HCAPTCHA_SOLVER_URL", "").rstrip("/")
        if solver_url:
            return self._solve_via_http(solver_url, site_key, rqdata, timeout)
        return self._solve_via_volume(site_key, rqdata, timeout)

    def _solve_via_http(self, solver_url: str, site_key: str, rqdata: str,
                        timeout: int) -> dict[str, str] | None:
        """调用 solver 的 HTTP 接口（复用会话）。"""
        payload: dict[str, Any] = {
            "sitekey": site_key, "rqdata": rqdata, "host": "b.stripecdn.com",
        }
        if self._solver_session:
            payload["session_id"] = self._solver_session
        elif self.proxy:
            payload["proxy"] = self.proxy
            payload["ua"] = UA
        try:
            r = httpx.post(f"{solver_url}/solve", json=payload, timeout=timeout + 30)
            if r.status_code == 200:
                j = r.json()
                tok = j.get("token") or ""
                if tok.startswith("P1_") and len(tok) > 200:
                    if not self._solver_session and j.get("session_id"):
                        self._solver_session = str(j["session_id"])
                    log(f"   solver token {len(tok)} chars ekey={len(j.get('ekey') or '')} "
                        f"({j.get('elapsed_ms', '?')}ms)")
                    return {"token": tok, "ekey": str(j.get("ekey") or "")}
                log(f"   solver 返回异常: {str(j.get('error'))[:80]}")
            else:
                log(f"   solver HTTP {r.status_code}: {r.text[:100]}")
        except Exception as e:
            log(f"   solver 连接失败: {str(e)[:80]}")
        return None

    def _solve_via_volume(self, site_key: str, rqdata: str,
                          timeout: int) -> dict[str, str] | None:
        """兜底：通过共享卷与宿主机 watcher 协作。"""
        data_dir = Path(os.environ.get("HCAP_DATA", "/data"))
        cfg_path = data_dir / "hcap_cfg.json"
        tok_path = data_dir / "hcap_token.txt"
        try:
            if tok_path.exists():
                tok_path.unlink()
        except Exception:
            pass
        try:
            cfg_path.write_text(json.dumps({"site_key": site_key, "rqdata": rqdata}))
        except Exception as e:
            log(f"   写 cfg 失败: {str(e)[:60]}")
            return None

        deadline = time.time() + timeout
        while time.time() < deadline:
            time.sleep(2)
            if tok_path.exists():
                try:
                    t = tok_path.read_text().strip()
                except Exception:
                    continue
                if t.startswith("P1_") and len(t) > 200:
                    log(f"   卷 token {len(t)} chars")
                    return {"token": t, "ekey": ""}
        log("   卷 token 超时")
        return None

    # ── ⑤/⑦b hCaptcha ──────────────────────────────────────
    def _solve_captcha(
        self, site_key: str, rqdata: str, use_proxy: bool = False
    ) -> dict[str, str] | None:
        if not self.captcha_key or not site_key:
            return None
        website = (
            "https://b.stripecdn.com/stripethirdparty-srv/assets/v35.5/HCaptchaInvisible.html"
        )
        task: dict[str, Any] = {
            "websiteURL": website,
            "websiteKey": site_key,
            "isEnterprise": True,
            "userAgent": UA,
        }
        if use_proxy and self.proxy:
            u = httpx.URL(self.proxy)
            task.update({
                "type": "HCaptchaTask",
                "proxyType": u.scheme or "http",
                "proxyAddress": u.host,
                "proxyPort": u.port or 8080,
                "proxyLogin": u.username or "",
                "proxyPassword": u.password or "",
            })
        else:
            task["type"] = "HCaptchaTaskProxyless"
        if rqdata:
            task["enterprisePayload"] = {"rqdata": rqdata}

        cc = self._captcha_http()
        try:
            r = cc.post(f"{self.captcha_url}/createTask",
                        json={"clientKey": self.captcha_key, "task": task})
            j = r.json()
            if j.get("errorId"):
                log(f"   createTask 失败: {j.get('errorCode')}")
                return None
            task_id = j.get("taskId")
            if not task_id:
                return None
        except Exception as e:
            log(f"   createTask 异常: {str(e)[:80]}")
            return None

        for _ in range(60):
            time.sleep(3)
            try:
                r = cc.post(f"{self.captcha_url}/getTaskResult",
                            json={"clientKey": self.captcha_key, "taskId": task_id})
                j = r.json()
            except Exception:
                continue
            if j.get("status") == "ready":
                sol = j.get("solution") or {}
                tok = sol.get("gRecaptchaResponse") or sol.get("token") or ""
                ekey = sol.get("eKey") or sol.get("respKey") or ""
                return {"token": tok, "ekey": ekey}
            code = j.get("errorCode")
            if code and code != "ERROR_TASK_NOT_READY":
                log(f"   任务错误: {code}")
                return None
        return None

    # ── ⑥ payment_methods ──────────────────────────────────
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
    def verify_challenge(
        self, path: str, token: str, client_secret: str, ekey: str = ""
    ) -> dict[str, Any]:
        log("⑦c verify_challenge")
        data = {
            "client_secret": client_secret,
            "challenge_response_token": token,
            "captcha_vendor_name": "hcaptcha",
            "key": PUBLISHABLE_KEY,
            "_stripe_version": V_FULL,
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
        fp = self.register_fingerprint()

        # solver 会话必须在任何 hCaptcha 请求之前建立：
        # passive 与 challenge 的 token 要同浏览器、同出口 IP，
        # 否则 verify_challenge 返回 payment_intent_authentication_failure。
        self._solver_open_session()

        # 精简时间线（与成功案例一致）
        self._sleep(random.uniform(2.0, 4.0))
        self.fetch_allowed_origins()

        self._sleep(random.uniform(1.0, 2.0))
        _, ctx = self.init_checkout(cs_id)

        # init 后停留（成功案例是 13.5 秒）
        dwell = random.uniform(10.0, 16.0)
        log(f"   停留 {dwell:.1f}s（模拟用户阅读）")
        time.sleep(dwell)

        locale, _tz = self._align_locale(self.proxy)
        self.elements_session(cs_id, ctx, locale)

        # 只提交一次 tax_region
        self._sleep(random.uniform(0.5, 1.2))
        self.update_address(cs_id, ctx)

        # 解 passive captcha
        log("⑤ 解 passive captcha")
        passive = self._solve_via_browser(ctx.site_key, ctx.rqdata)
        if not passive:
            passive = self._solve_captcha(ctx.site_key, ctx.rqdata, use_proxy=False)
        if passive:
            log(f"   passive {len(passive['token'])} chars")
        else:
            log("   passive 未解出，继续")

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
            log(f"⑦b 解 challenge（第 {round_no + 1} 次）site_key={str(sj.get('site_key'))[:8]}")
            ch = self._solve_via_browser(
                str(sj.get("site_key") or ""), str(sj.get("rqdata") or "")
            )
            if not ch:
                ch = self._solve_captcha(
                    str(sj.get("site_key") or ""), str(sj.get("rqdata") or ""),
                    use_proxy=True,
                )
            if not (ch and sj.get("verification_url")):
                log("   challenge 未解出")
                break
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

        self._solver_close_session()
        outcome = self.classify(confirm_res, None)
        if outcome.state not in ("paid", "card_declined", "captcha_rejected"):
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
