"""让 sentinel 的 platform / platformLabel 跟随 UA，消除硬编码 Win32 的矛盾。

之前：UA 传的是 Linux，但 payload 里 platform 写死 "Win32"、
platformLabel 写死 "Windows" —— 同一次建单里两处说法冲突。

改法：mint() 增加 platform / platform_label / screen 参数，
由 chatgpt.py 根据 UA 推导后传入。
"""
import ast
from pathlib import Path

S = Path("/opt/gss/src/packages/core/src/gss_core/sentinel.py")
C = Path("/opt/gss/src/packages/core/src/gss_core/chatgpt.py")

# ── 1) sentinel.py：参数化 ──────────────────────────────
s = S.read_text()

old_sig = '    cookie_header: str = "",\n    timeout_s: float = 90.0,\n) -> Dict[str, Any]:'
new_sig = (
    '    cookie_header: str = "",\n'
    '    timeout_s: float = 90.0,\n'
    '    platform: str = "Linux x86_64",\n'
    '    platform_label: str = "Linux",\n'
    '    screen_w: int = 1920,\n'
    '    screen_h: int = 1080,\n'
    '    avail_h: int = 1032,\n'
    '    cores: int = 8,\n'
    ') -> Dict[str, Any]:'
)
assert old_sig in s, "sentinel 签名未命中"
s = s.replace(old_sig, new_sig, 1)

old_body = (
    '            "cores": 8,'
)
new_body = '            "cores": cores,'
assert old_body in s, "cores 未命中"
s = s.replace(old_body, new_body, 1)

old_plat = (
    '            "platform": "Win32",\n'
    '            "platformLabel": "Windows",\n'
    '            "screenW": 1920,\n'
    '            "screenH": 1080,\n'
    '            "availH": 1032,'
)
new_plat = (
    '            "platform": platform,\n'
    '            "platformLabel": platform_label,\n'
    '            "screenW": screen_w,\n'
    '            "screenH": screen_h,\n'
    '            "availH": avail_h,'
)
assert old_plat in s, "platform 块未命中"
s = s.replace(old_plat, new_plat, 1)

S.write_text(s)
ast.parse(s)
print("sentinel.py ok")

# ── 2) chatgpt.py：从 UA 推导并传入 ─────────────────────
c = C.read_text()

helper = '''

def _profile_from_ua(ua: str) -> dict:
    """从 UA 推导 platform / 屏幕 / 核心数，保证与 UA 自洽。"""
    low = (ua or "").lower()
    if "windows" in low:
        return {"platform": "Win32", "platform_label": "Windows",
                "screen_w": 1920, "screen_h": 1080, "avail_h": 1032, "cores": 8}
    if "macintosh" in low or "mac os x" in low:
        return {"platform": "MacIntel", "platform_label": "macOS",
                "screen_w": 1512, "screen_h": 982, "avail_h": 938, "cores": 8}
    return {"platform": "Linux x86_64", "platform_label": "Linux",
            "screen_w": 1920, "screen_h": 1080, "avail_h": 1053, "cores": 8}

'''
anchor = "def _ua_mac(build: str) -> str:"
if "_profile_from_ua" not in c:
    c = c.replace(anchor, helper.strip() + "\n\n\n" + anchor, 1)

old_mint = (
    "            cookie_header=self._cookie_header(),\n"
    "            timeout_s=max(30.0, min(self.timeout, 90.0)),\n"
    "        )"
)
new_mint = (
    "            cookie_header=self._cookie_header(),\n"
    "            timeout_s=max(30.0, min(self.timeout, 90.0)),\n"
    "            **_profile_from_ua(self.ua),\n"
    "        )"
)
assert old_mint in c, "mint 调用未命中"
c = c.replace(old_mint, new_mint, 1)

C.write_text(c)
ast.parse(c)
print("chatgpt.py ok")

import subprocess
print(subprocess.run(
    ["grep", "-n", "platform\\|_profile_from_ua", str(S)], capture_output=True, text=True
).stdout[:600])
