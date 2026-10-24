"""给 _solve_challenge 补上指纹参数，保证求解器与协议链同环境。"""
import ast
from pathlib import Path

F = Path("/opt/gss/src/packages/core/src/gss_core/stripe_protocol.py")
s = F.read_text()

old = """            payload = {
                "sitekey": site_key,
                "rqdata": rqdata,
                "proxy": self.proxy or "",
                "host": "b.stripecdn.com",
            }"""
new = """            # 指纹必须与协议链一致：同一出口 + 同一 UA / 平台 / 屏幕
            _ua = UA
            _low = _ua.lower()
            if "windows" in _low:
                _plat, _label, _scr = "Win32", "Windows", (1920, 1080, 1032)
            elif "macintosh" in _low or "mac os x" in _low:
                _plat, _label, _scr = "MacIntel", "macOS", (1512, 982, 938)
            else:
                _plat, _label, _scr = "Linux x86_64", "Linux", (1920, 1080, 1053)
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
                "locale": "en-US",
                "timezone": "America/New_York",
            }"""
assert old in s, "payload 锚点未命中"
F.write_text(s.replace(old, new, 1))
ast.parse(F.read_text())
print("patched stripe_protocol._solve_challenge")
