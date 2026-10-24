"""把浏览器版本落到真实存在的 Chrome 150 build，并按 device_id 稳定选取。

数据来源：versionhistory.googleapis.com 官方接口
  mac   stable 150 → 150.0.7871.{24,46,47,49,100-102,114-116,124-130,181-189}
  linux stable 150 → 同一个 build 7871

原则：
  - 大版本固定 150（与 curl_cffi impersonate=chrome150 对齐）
  - 小版本按 device_id 哈希选择，同一账号永远同一个值，不同账号分散
  - User-Agent / sec-ch-ua-full-version / sec-ch-ua-full-version-list 三条同步
  - sec-ch-ua 只带大版本 v="150"（规范如此）
"""
import ast
from pathlib import Path

C = Path("/opt/gss/src/packages/core/src/gss_core/chatgpt.py")
S = Path("/opt/gss/src/packages/core/src/gss_core/stripe_protocol.py")

BUILDS_MAC = [
    "150.0.7871.189", "150.0.7871.187", "150.0.7871.184",
    "150.0.7871.130", "150.0.7871.127", "150.0.7871.116", "150.0.7871.101",
]
BUILDS_LINUX = [
    "150.0.7871.128", "150.0.7871.124", "150.0.7871.114",
    "150.0.7871.100", "150.0.7871.46",
]

HELPER = '''

# ── 真实 Chrome 150 build（来自 versionhistory.googleapis.com）──────────
_CHROME_BUILDS_MAC = %r
_CHROME_BUILDS_LINUX = %r


def _pick_build(seed: str, builds) -> str:
    """按 device_id 稳定选取，同一账号永远同一个版本。"""
    import hashlib as _hl
    if not seed:
        return builds[0]
    digest = _hl.sha256(str(seed).encode("utf-8")).hexdigest()[:8]
    return builds[int(digest, 16) %% len(builds)]


def _ua_mac(build: str) -> str:
    return ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/" + build + " Safari/537.36")

''' % (BUILDS_MAC, BUILDS_LINUX)


def patch_chatgpt():
    s = C.read_text()

    old_ua = (
        '_CHROME = (\n'
        '    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36'
        ' (KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36"\n'
        ')'
    )
    new_ua = HELPER.strip() + '\n\n\n_CHROME = _ua_mac(_CHROME_BUILDS_MAC[0])'
    if old_ua in s:
        s = s.replace(old_ua, new_ua, 1)
    else:
        print("  !! _CHROME 常量未命中")

    # 实例属性
    anchor = "    def _identity_headers(self) -> Dict[str, str]:"
    props = (
        "    @property\n"
        "    def chrome_build(self) -> str:\n"
        "        return _pick_build(getattr(self, \"device_id\", \"\") or \"\", _CHROME_BUILDS_MAC)\n"
        "\n"
        "    @property\n"
        "    def ua(self) -> str:\n"
        "        return _ua_mac(self.chrome_build)\n"
        "\n"
        + anchor
    )
    if anchor in s:
        s = s.replace(anchor, props, 1)
    else:
        print("  !! _identity_headers 未命中")

    pairs = [
        ('            "User-Agent": _CHROME,', '            "User-Agent": self.ua,'),
        (
            '            "sec-ch-ua-full-version": \'"150.0.0.0"\',',
            '            "sec-ch-ua-full-version": \'"\' + self.chrome_build + \'"\',',
        ),
        (
            '            "sec-ch-ua-full-version-list": (\n'
            "                '\"Google Chrome\";v=\"150.0.0.0\", \"Chromium\";v=\"150.0.0.0\", '\n"
            "                '\"Not(A:Brand\";v=\"24.0.0.0\"'),",
            '            "sec-ch-ua-full-version-list": (\n'
            "                '\"Google Chrome\";v=\"' + self.chrome_build + '\", '\n"
            "                '\"Chromium\";v=\"' + self.chrome_build + '\", '\n"
            "                '\"Not(A:Brand\";v=\"24.0.0.0\"'),",
        ),
        ("            user_agent=_CHROME,", "            user_agent=self.ua,"),
        ('                    "User-Agent": _CHROME,', '                    "User-Agent": self.ua,'),
    ]
    for old, new in pairs:
        if old in s:
            s = s.replace(old, new)
        else:
            print(f"  !! 未命中: {old.strip()[:60]}")

    C.write_text(s)
    ast.parse(s)
    print("  chatgpt.py ok")


def patch_stripe():
    s = S.read_text()
    old = (
        'UA = (\n'
        '    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "\n'
        '    "(KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36"\n'
        ')'
    )
    new = (
        'UA = (\n'
        '    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "\n'
        '    "(KHTML, like Gecko) Chrome/150.0.7871.128 Safari/537.36"\n'
        ')'
    )
    if old in s:
        s = s.replace(old, new, 1)
    else:
        print("  !! stripe UA 未命中")
    S.write_text(s)
    ast.parse(s)
    print("  stripe_protocol.py ok")


print("== patch ==")
patch_chatgpt()
patch_stripe()
print("done")
