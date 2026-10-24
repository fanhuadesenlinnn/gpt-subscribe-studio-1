"""把 chatgpt.py / stripe_protocol.py 的浏览器版本三层对齐到 chrome150。

三层必须一致，否则 TLS 指纹与 UA/client hints 对不上会被判定异常：
  1) TLS/JA3 指纹   curl_cffi impersonate
  2) User-Agent
  3) sec-ch-ua* client hints
"""
from pathlib import Path

VER = "150"
FULL = "150.0.0.0"
UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36"
)
SEC_CH = '"Google Chrome";v="150", "Chromium";v="150", "Not(A:Brand";v="24"'

C = Path("/opt/gss/src/packages/core/src/gss_core/chatgpt.py")
S = Path("/opt/gss/src/packages/core/src/gss_core/stripe_protocol.py")


def patch(path: Path, pairs):
    text = path.read_text()
    for old, new in pairs:
        if old not in text:
            print(f"  !! 未命中: {old[:60]}")
            continue
        text = text.replace(old, new)
    path.write_text(text)
    print(f"  patched {path.name}")


print("== chatgpt.py ==")
patch(C, [
    ('_IMPERSONATE = "chrome136"', '_IMPERSONATE = "chrome150"'),
    (
        '_CHROME = (\n'
        '    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "\n'
        '    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/155.0.8059.85 Safari/537.36"\n'
        ')',
        '_CHROME = (\n    "' + UA + '"\n)',
    ),
    (
        "_SEC_CH_UA = '\"Google Chrome\";v=\"155\", \"Chromium\";v=\"155\", \"Not(A:Brand\";v=\"24\"'",
        "_SEC_CH_UA = '" + SEC_CH + "'",
    ),
    ('"sec-ch-ua-full-version": \'"155.0.8059.85"\'',
     '"sec-ch-ua-full-version": \'"150.0.0.0"\''),
    (
        '"sec-ch-ua-full-version-list": (\n'
        "                '\"Google Chrome\";v=\"155.0.8059.85\", \"Chromium\";v=\"155.0.8059.85\", '\n"
        "                '\"Not(A:Brand\";v=\"24.0.0.0\"'),",
        '"sec-ch-ua-full-version-list": (\n'
        "                '\"Google Chrome\";v=\"150.0.0.0\", \"Chromium\";v=\"150.0.0.0\", '\n"
        "                '\"Not(A:Brand\";v=\"24.0.0.0\"'),",
    ),
    ('session = CurlSession(impersonate="chrome136", timeout=timeout)',
     'session = CurlSession(impersonate="chrome150", timeout=timeout)'),
])

print("== stripe_protocol.py ==")
patch(S, [
    ('impersonate="chrome124"', 'impersonate="chrome150"'),
])

for p in (C, S):
    import ast
    ast.parse(p.read_text())
    print(f"  syntax ok: {p.name}")
