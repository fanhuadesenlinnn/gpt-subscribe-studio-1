"""统一三平台 profile：Windows / macOS / Linux 各含 146-150 真实 build。

按 device_id 哈希选定平台与版本，同一账号永远同一组合，不同账号分散。
UA / client hints / navigator.platform / platformVersion / 屏幕尺寸
全部来自同一份 profile，不会再出现互相矛盾。

真实数据（versionhistory.googleapis.com，stable）：
  win64 146 .178-.180  147 .137-.139  148 .216-.218  149 .199-.201  150 .187-.189
  mac   146 .177-.180  147 .137-.139  148 .215-.217  149 .199-.201  150 .187-.189
  linux 146 .153/.164/.177  147 .101/.116/.137  148 .167/.178/.215
        149 .155/.196/.200   150 .128/.181/.186
"""
import ast
import re
from pathlib import Path

C = Path("/opt/gss/src/packages/core/src/gss_core/chatgpt.py")
s = C.read_text()

BLOCK = '''# ── 真实 Chrome 146-150 profile（versionhistory.googleapis.com）──────────
_PROFILES = {
    "windows": {
        "builds": ["150.0.7871.189", "149.0.7827.201", "148.0.7778.218",
                   "147.0.7727.139", "146.0.7680.180",
                   "150.0.7871.187", "149.0.7827.199", "148.0.7778.216",
                   "147.0.7727.137", "146.0.7680.178"],
        "platform": "Win32",
        "label": "Windows",
        "screen": (1920, 1080, 1032),
        "ua": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
               "AppleWebKit/537.36 (KHTML, like Gecko) "
               "Chrome/{build} Safari/537.36"),
        "hints": {
            "sec-ch-ua-platform": '"Windows"',
            "sec-ch-ua-platform-version": '"10.0.0"',
            "sec-ch-ua-arch": '"x86"',
            "sec-ch-ua-bitness": '"64"',
        },
    },
    "macos": {
        "builds": ["150.0.7871.189", "149.0.7827.201", "148.0.7778.217",
                   "147.0.7727.139", "146.0.7680.180",
                   "150.0.7871.187", "149.0.7827.199", "148.0.7778.215",
                   "147.0.7727.137", "146.0.7680.177"],
        "platform": "MacIntel",
        "label": "macOS",
        "screen": (1512, 982, 938),
        "ua": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
               "AppleWebKit/537.36 (KHTML, like Gecko) "
               "Chrome/{build} Safari/537.36"),
        "hints": {
            "sec-ch-ua-platform": '"macOS"',
            "sec-ch-ua-platform-version": '"14.6.1"',
            "sec-ch-ua-arch": '"arm"',
            "sec-ch-ua-bitness": '"64"',
        },
    },
    "linux": {
        "builds": ["150.0.7871.186", "149.0.7827.200", "148.0.7778.215",
                   "147.0.7727.137", "146.0.7680.177",
                   "150.0.7871.181", "149.0.7827.196", "148.0.7778.178",
                   "147.0.7727.116", "146.0.7680.164"],
        "platform": "Linux x86_64",
        "label": "Linux",
        "screen": (1920, 1080, 1053),
        "ua": ("Mozilla/5.0 (X11; Linux x86_64) "
               "AppleWebKit/537.36 (KHTML, like Gecko) "
               "Chrome/{build} Safari/537.36"),
        "hints": {"sec-ch-ua-platform": '"Linux"'},
    },
}

# 加权：Windows 占真实用户多数
_PROFILE_ORDER = ["windows", "windows", "windows", "macos", "macos", "linux"]


def _hash8(seed: str, salt: str = "") -> int:
    import hashlib as _hl
    return int(_hl.sha256((str(seed) + salt).encode("utf-8")).hexdigest()[:8], 16)


def _pick_profile(device_id: str) -> dict:
    """按 device_id 稳定选定平台与版本，同一账号永远同一组合。"""
    seed = str(device_id or "anon")
    key = _PROFILE_ORDER[_hash8(seed, "os") % len(_PROFILE_ORDER)]
    prof = dict(_PROFILES[key])
    prof["key"] = key
    prof["build"] = prof["builds"][_hash8(seed, "b") % len(prof["builds"])]
    return prof


def _profile_from_ua(ua: str) -> dict:
    """兼容旧调用：从 UA 推导 platform / 屏幕 / 核心数。"""
    low = (ua or "").lower()
    if "windows" in low:
        return {"platform": "Win32", "platform_label": "Windows",
                "screen_w": 1920, "screen_h": 1080, "avail_h": 1032, "cores": 8}
    if "macintosh" in low or "mac os x" in low:
        return {"platform": "MacIntel", "platform_label": "macOS",
                "screen_w": 1512, "screen_h": 982, "avail_h": 938, "cores": 8}
    return {"platform": "Linux x86_64", "platform_label": "Linux",
            "screen_w": 1920, "screen_h": 1080, "avail_h": 1053, "cores": 8}


def _ua_mac(build: str) -> str:
    """兼容旧调用：默认 Linux UA。"""
    return _PROFILES["linux"]["ua"].format(build=build)
'''

# 用新块替换「真实 Chrome 150 build ... _ua_mac 函数」整段
start = s.index("# ── 真实 Chrome 150 build")
end = s.index("def _ua_mac(build: str) -> str:")
end = s.index("\n\n\n", end) + 3
s = s[:start] + BLOCK + "\n\n" + s[end:]

# 属性：改用 profile
old_props = '''    @property
    def chrome_build(self) -> str:
        return _pick_build(getattr(self, "device_id", "") or "", _CHROME_BUILDS_MAC)'''
new_props = '''    @property
    def profile(self) -> dict:
        return _pick_profile(getattr(self, "device_id", "") or "")

    @property
    def chrome_build(self) -> str:
        return self.profile["build"]'''
assert old_props in s, "chrome_build 属性未命中"
s = s.replace(old_props, new_props, 1)

old_ua_prop = '''    @property
    def ua(self) -> str:
        return _ua_mac(self.chrome_build)'''
new_ua_prop = '''    @property
    def ua(self) -> str:
        return self.profile["ua"].format(build=self.chrome_build)

    @property
    def sentinel_profile(self) -> dict:
        p = self.profile
        return {"platform": p["platform"], "platform_label": p["label"],
                "screen_w": p["screen"][0], "screen_h": p["screen"][1],
                "avail_h": p["screen"][2], "cores": 8}'''
assert old_ua_prop in s, "ua 属性未命中"
s = s.replace(old_ua_prop, new_ua_prop, 1)

# client hints：平台相关的整组展开
s = s.replace(
    '            "sec-ch-ua-platform": \'"Linux"\',',
    '            **self.profile["hints"],',
    1,
)

# sentinel 调用改用 sentinel_profile
s = s.replace("            **_profile_from_ua(self.ua),", "            **self.sentinel_profile,", 1)

C.write_text(s)
ast.parse(s)

import subprocess
print(subprocess.run(
    ["grep", "-n", "_PROFILES = \\|def _pick_profile\\|def profile\\|sentinel_profile\\|profile\\[.hints.\\|sec-ch-ua-platform",
     str(C)], capture_output=True, text=True).stdout[:900])
print("ok")
