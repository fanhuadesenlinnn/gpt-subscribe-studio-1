"""把 chatgpt.py 的平台从 macOS 改为 Linux，与真实运行环境一致。

原因：我们的浏览器（patchright + /usr/bin/google-chrome 155）跑在 Linux 上，
canvas / WebGL / 字体 / GPU 全是 Linux 的真实值。声称 macOS 拿不出对应的
字体和 GPU，自相矛盾；声称 Linux 则处处可验证。

Linux 侧 Chrome 只发 sec-ch-ua / sec-ch-ua-mobile / sec-ch-ua-platform，
不发 arch / bitness / full-version / platform-version / model。
"""
import ast
import re
from pathlib import Path

C = Path("/opt/gss/src/packages/core/src/gss_core/chatgpt.py")

# 真实 Linux stable 版本（versionhistory.googleapis.com）
BUILDS = [
    "150.0.7871.186", "149.0.7827.200", "148.0.7778.215",
    "147.0.7727.137", "146.0.7680.177",
    "150.0.7871.181", "149.0.7827.196", "148.0.7778.178",
    "147.0.7727.116", "146.0.7680.164",
    "150.0.7871.128", "149.0.7827.155", "148.0.7778.167",
    "147.0.7727.101", "146.0.7680.153",
]

s = C.read_text()

# 1) 版本表
s = re.sub(r"_CHROME_BUILDS_MAC = \[[^\]]*\]", "_CHROME_BUILDS_MAC = " + repr(BUILDS), s, count=1)
s = re.sub(r"_CHROME_BUILDS_LINUX = \[[^\]]*\]", "_CHROME_BUILDS_LINUX = " + repr(BUILDS), s, count=1)

# 2) UA 函数：mac -> linux
s = s.replace(
    'def _ua_mac(build: str) -> str:\n'
    '    return ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "\n'
    '            "AppleWebKit/537.36 (KHTML, like Gecko) "\n'
    '            "Chrome/" + build + " Safari/537.36")',
    'def _ua_mac(build: str) -> str:\n'
    '    """Linux 桌面 Chrome（函数名保留，避免改动调用点）。"""\n'
    '    return ("Mozilla/5.0 (X11; Linux x86_64) "\n'
    '            "AppleWebKit/537.36 (KHTML, like Gecko) "\n'
    '            "Chrome/" + build + " Safari/537.36")',
)

# 3) 平台头：Linux 只保留 platform，删掉 arch/bitness/full-version/platform-version/model
s = re.sub(
    r'\n\s*"sec-ch-ua-arch": \'"arm"\',',
    "",
    s,
)
s = re.sub(
    r'\n\s*"sec-ch-ua-bitness": \'"64"\',',
    "",
    s,
)
s = re.sub(
    r'\n\s*"sec-ch-ua-full-version": [^\n]*\n',
    "\n",
    s,
)
s = re.sub(
    r'\n\s*"sec-ch-ua-full-version-list": \(\n(?:[^\n]*\n){1,3}?[^\n]*\),\n',
    "\n",
    s,
)
s = re.sub(
    r'\n\s*"sec-ch-ua-model": \'""\',',
    "",
    s,
)
s = re.sub(
    r'\n\s*"sec-ch-ua-platform-version": \'"27\.0\.1"\',',
    "",
    s,
)
s = s.replace('"sec-ch-ua-platform": \'"macOS"\'', '"sec-ch-ua-platform": \'"Linux"\'')

C.write_text(s)
ast.parse(s)

import subprocess
out = subprocess.run(
    ["grep", "-n", "sec-ch-ua\\|X11; Linux\\|_CHROME_BUILDS_MAC = ", str(C)],
    capture_output=True, text=True).stdout
print(out[:1200])
print("ok")
