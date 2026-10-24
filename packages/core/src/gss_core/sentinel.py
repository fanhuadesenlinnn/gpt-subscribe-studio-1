"""签发 ChatGPT OpenAI-Sentinel 令牌。用本仓库的 Node 桥，不依赖其它本地项目。"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict

_ASSETS = Path(__file__).with_name("sentinel_assets")
_BRIDGE = _ASSETS / "sentinel_bridge.js"
_BRIDGE_VERSION = "20260810913b"


def extract_attestation(html: str) -> str:
    import re

    text = str(html or "")
    patterns = (
        r'"webDeploymentAttestation"\s*:\s*"([^"]+)"',
        r'[^A-Za-z0-9]webDeploymentAttestation[=:]\s*["\']?([A-Za-z0-9._-]+)',
    )
    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            value = match.group(1).strip()
            if value and "." in value:
                return value
    return ""


def extract_build(html: str) -> tuple[str, str, str]:
    import re

    text = str(html or "")
    prod = re.search(r"prod-[0-9a-f]{40}", text)
    version_match = re.search(r'data-build="([^"]+)"', text)
    seq_match = re.search(r'data-seq="([^"]+)"', text) or re.search(
        r'"buildNumber"\s*:\s*"?(\d+)"?', text
    )
    session_match = re.search(r'"sessionId"\s*:\s*"([0-9a-fA-F-]{36})"', text)
    return (
        prod.group(0) if prod else (version_match.group(1) if version_match else ""),
        seq_match.group(1) if seq_match else "",
        session_match.group(1) if session_match else "",
    )


def header_value(token: Dict[str, Any]) -> str:
    if not isinstance(token, dict) or not token.get("p") or not token.get("c"):
        return ""
    payload = {key: token[key] for key in token if not str(key).startswith("_")}
    try:
        return json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
    except (TypeError, ValueError):
        return ""


def so_header_value(token: Dict[str, Any]) -> str:
    so_token = token.get("_so") if isinstance(token, dict) else None
    if not isinstance(so_token, dict) or not so_token.get("so"):
        return ""
    try:
        return json.dumps(so_token, separators=(",", ":"), ensure_ascii=False)
    except (TypeError, ValueError):
        return ""


def mint(
    *,
    flow: str,
    device_id: str,
    user_agent: str,
    proxy: str = "",
    page_url: str = "https://chatgpt.com/",
    language: str = "en-US",
    timezone: str = "America/New_York",
    cookie_header: str = "",
    timeout_s: float = 90.0,
    platform: str = "Linux x86_64",
    platform_label: str = "Linux",
    screen_w: int = 1920,
    screen_h: int = 1080,
    avail_h: int = 1032,
    cores: int = 8,
) -> Dict[str, Any]:
    """返回哨兵 JSON；失败返回空 dict，由调用方决定是否中止。"""

    if not _BRIDGE.is_file():
        return {}
    payload = json.dumps(
        {
            "ua": user_agent,
            "cores": cores,
            "deviceId": device_id,
            "flow": flow,
            "proxy": proxy or "",
            "version": _BRIDGE_VERSION,
            "pageUrl": page_url,
            "language": language,
            "timezone": timezone,
            "platform": platform,
            "platformLabel": platform_label,
            "screenW": screen_w,
            "screenH": screen_h,
            "availH": avail_h,
            "maxTouchPoints": 0,
            "cookieHeader": cookie_header,
            "sentinelOrigin": "https://chatgpt.com",
        },
        separators=(",", ":"),
    ).encode("utf-8")
    env = os.environ.copy()
    env.setdefault("PYTHON_BIN", sys.executable)
    env.setdefault("SENTINEL_PYTHON", sys.executable)
    try:
        process = subprocess.run(
            [os.environ.get("SENTINEL_NODE") or "node", str(_BRIDGE)],
            input=payload,
            capture_output=True,
            timeout=timeout_s,
            cwd=str(_ASSETS),
            env=env,
            check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return {}
    output = (process.stdout or b"").decode("utf-8", "replace").strip()
    if not output:
        return {}
    try:
        result = json.loads(output)
    except json.JSONDecodeError:
        return {}
    if result.get("error") or not result.get("main"):
        return {}
    try:
        token = json.loads(str(result.get("main") or ""))
    except json.JSONDecodeError:
        return {}
    if not isinstance(token, dict):
        return {}
    so_raw = str(result.get("so") or "")
    if so_raw:
        try:
            token["_so"] = json.loads(so_raw)
        except json.JSONDecodeError:
            pass
    return token


__all__ = ["extract_attestation", "extract_build", "header_value", "so_header_value", "mint"]
