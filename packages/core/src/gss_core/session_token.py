"""把用户粘贴的 session 解析成 access token、session cookie、设备 id。只在内存中使用。"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Dict


@dataclass
class SessionBundle:
    access_token: str
    session_token: str = ""
    device_id: str = ""
    oai_session_id: str = ""
    cookies: Dict[str, str] = field(default_factory=dict)


def parse_session(raw: str) -> str:
    return parse_session_bundle(raw).access_token


def parse_session_bundle(raw: str) -> SessionBundle:
    text = (raw or "").strip()
    if not text:
        raise ValueError("session 为空")
    if text.lower().startswith("bearer "):
        text = text.split(None, 1)[1].strip()
    
    # 处理 __Secure-next-auth.session-token=xxx 格式
    if text.startswith("__Secure-next-auth.session-token="):
        text = text.split("=", 1)[1].strip()
    
    if text.startswith("{") or text.startswith("["):
        try:
            data: Any = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ValueError("session JSON 无法解析") from exc
        return _from_mapping(data)
    if ";" in text and "=" in text and not text.lstrip().startswith("eyJ"):
        cookies = _parse_cookie_header(text)
        if "__Secure-next-auth.session-token" in cookies or "oai-did" in cookies:
            raise ValueError("请粘贴包含 accessToken 的整段 session JSON，仅 Cookie 不够")
    
    # 识别 JWE（5段）vs JWS（3段）
    # JWE (session token): eyJ...dir...  (5段，用于 next-auth)
    # JWS (access token): eyJ...alg... (3段，用于 API 授权)
    if text.startswith("eyJ") and text.count(".") >= 2:
        parts = text.split(".")
        if len(parts) == 5:
            # 这是 JWE，当作 session_token 而非 access_token
            return SessionBundle(access_token="", session_token=text)
    
    return SessionBundle(access_token=text)


def _from_mapping(data: Any) -> SessionBundle:
    if isinstance(data, list) and data:
        return _from_mapping(data[0])
    if not isinstance(data, dict):
        return SessionBundle(access_token="")
    nested = data.get("session")
    if isinstance(nested, dict):
        inner = _from_mapping(nested)
    else:
        inner = SessionBundle(access_token="")
    token = _first_string(data, ("accessToken", "access_token", "token", "at")) or inner.access_token
    if not token and isinstance(data.get("user"), dict):
        token = _first_string(data["user"], ("accessToken", "access_token", "token"))
    cookies = dict(inner.cookies)
    cookies.update(_cookies_of(data))
    session_token = (
        _first_string(data, ("sessionToken", "session_token", "__Secure-next-auth.session-token"))
        or cookies.get("__Secure-next-auth.session-token")
        or inner.session_token
    )
    device_id = (
        _first_string(data, ("oai-did", "oai_did", "deviceId", "device_id"))
        or cookies.get("oai-did")
        or inner.device_id
    )
    statsig = data.get("statsigContext")
    session_id = (
        _first_string(data, ("sessionId", "session_id", "oai-session-id", "oai_session_id"))
        or inner.oai_session_id
    )
    if isinstance(statsig, dict):
        if not device_id:
            device_id = _first_string(statsig, ("deviceId", "device_id"))
        if not session_id:
            session_id = _first_string(statsig, ("sessionId", "session_id"))
    if not token and isinstance(nested, str) and len(nested) > 20:
        token = nested.strip()
    if not token:
        raise ValueError("JSON 中没有 accessToken")
    if session_token:
        cookies.setdefault("__Secure-next-auth.session-token", session_token)
    if device_id:
        cookies.setdefault("oai-did", device_id)
    return SessionBundle(
        access_token=token,
        session_token=session_token,
        device_id=device_id,
        oai_session_id=session_id,
        cookies=cookies,
    )


def _first_string(data: Dict[str, Any], keys: tuple[str, ...]) -> str:
    for key in keys:
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _cookies_of(data: Dict[str, Any]) -> Dict[str, str]:
    cookies: Dict[str, str] = {}
    raw_cookies = data.get("cookies")
    if isinstance(raw_cookies, list):
        for item in raw_cookies:
            if isinstance(item, dict) and item.get("name") and item.get("value") is not None:
                cookies[str(item["name"])] = str(item["value"])
    elif isinstance(raw_cookies, dict):
        cookies.update({str(k): str(v) for k, v in raw_cookies.items() if v is not None})
    elif isinstance(raw_cookies, str):
        cookies.update(_parse_cookie_header(raw_cookies))
    for key in ("cookie", "cookie_header", "Cookie"):
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            cookies.update(_parse_cookie_header(value))
    return cookies


def _parse_cookie_header(text: str) -> Dict[str, str]:
    cookies: Dict[str, str] = {}
    for part in str(text or "").split(";"):
        part = part.strip()
        if not part or "=" not in part:
            continue
        name, value = part.split("=", 1)
        name = name.strip()
        if name:
            cookies[name] = value.strip()
    return cookies


__all__ = ["SessionBundle", "parse_session", "parse_session_bundle"]

def email_from_access_token(access_token: str) -> str:
    """从 ChatGPT accessToken（JWT）里取账户邮箱。

    载荷里邮箱出现的位置不止一处，按以下顺序尝试：
      · "email"
      · "https://api.openai.com/profile" → "email"   ← 实测位置
      · "https://api.openai.com/auth" → "email"
      · "user" → "email"
    解析失败一律返回空串，由调用方决定兜底。
    """
    import base64
    import json

    token = (access_token or "").strip()
    parts = token.split(".")
    if len(parts) < 2:
        return ""
    payload = parts[1].replace("-", "+").replace("_", "/")
    payload += "=" * (-len(payload) % 4)
    try:
        data = json.loads(base64.b64decode(payload).decode("utf-8", "replace"))
    except Exception:
        return ""
    if not isinstance(data, dict):
        return ""

    def _ok(value: Any) -> str:
        text = str(value or "").strip()
        return text if "@" in text and "." in text.split("@")[-1] else ""

    direct = _ok(data.get("email"))
    if direct:
        return direct
    # 实测 ChatGPT accessToken 的邮箱在这里
    profile = data.get("https://api.openai.com/profile")
    if isinstance(profile, dict):
        found = _ok(profile.get("email"))
        if found:
            return found
    auth = data.get("https://api.openai.com/auth")
    if isinstance(auth, dict):
        found = _ok(auth.get("email"))
        if found:
            return found
    user = data.get("user")
    if isinstance(user, dict):
        found = _ok(user.get("email"))
        if found:
            return found
    return ""
