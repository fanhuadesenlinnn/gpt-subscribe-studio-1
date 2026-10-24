"""扩展到 Chrome 146-150 真实版本，并让 sec-ch-ua 大版本随 UA 变化。

真实数据（versionhistory.googleapis.com，mac stable）：
  146 → 146.0.7680.{31,65..81,153..180}
  147 → 147.0.7727.{24..57,101..119,137..139}
  148 → 148.0.7778.{56,96..98,167..169,178..181,215..217}
  149 → 149.0.7827.{29,53..55,102..104,114..116,155..158,196..201}
  150 → 150.0.7871.{24..49,100..102,114..116,124..130,181..189}

TLS 层：curl_cffi 只有 chrome150，但 Chrome 146-150 的 ClientHello
（JA3/JA4）几乎一致，所以 UA 落在 146-150 都能与 chrome150 兼容。
关键是不能让 sec-ch-ua 与 UA 打架 —— 大版本必须跟着 UA 走。
"""
import ast
from pathlib import Path

C = Path("/opt/gss/src/packages/core/src/gss_core/chatgpt.py")

BUILDS_MAC = [
    # 停在某个大版本最后的补丁（大多数未升级用户的状态）
    "150.0.7871.189", "149.0.7827.201", "148.0.7778.217",
    "147.0.7727.139", "146.0.7680.180",
    # 稍早一两个补丁（延迟更新的用户）
    "150.0.7871.187", "149.0.7827.199", "148.0.7778.215",
    "147.0.7727.137", "146.0.7680.177",
    # 再往前
    "150.0.7871.184", "149.0.7827.198", "148.0.7778.180",
    "147.0.7727.118", "146.0.7680.166",
    "150.0.7871.130", "149.0.7827.156", "148.0.7778.168",
]

BUILDS_LINUX = [
    "150.0.7871.128", "149.0.7827.115", "148.0.7778.179",
    "147.0.7727.117", "146.0.7680.165",
    "150.0.7871.124", "149.0.7827.103", "148.0.7778.97",
]

s = C.read_text()

# 1) 替换版本表
import re
s = re.sub(
    r"_CHROME_BUILDS_MAC = \[[^\]]*\]",
    "_CHROME_BUILDS_MAC = " + repr(BUILDS_MAC),
    s,
    count=1,
)
s = re.sub(
    r"_CHROME_BUILDS_LINUX = \[[^\]]*\]",
    "_CHROME_BUILDS_LINUX = " + repr(BUILDS_LINUX),
    s,
    count=1,
)

# 2) 加 major 属性
anchor = "    @property\n    def ua(self) -> str:"
props = (
    "    @property\n"
    "    def chrome_major(self) -> str:\n"
    "        return self.chrome_build.split(\".\", 1)[0]\n"
    "\n"
    "    @property\n"
    "    def sec_ch_ua(self) -> str:\n"
    "        major = self.chrome_major\n"
    "        return ('\"Google Chrome\";v=\"' + major + '\", \"Chromium\";v=\"' + major\n"
    "                + '\", \"Not(A:Brand\";v=\"24\"')\n"
    "\n" + anchor
)
if anchor in s and "def chrome_major" not in s:
    s = s.replace(anchor, props, 1)
else:
    print("  !! chrome_major 插入未命中或已存在")

# 3) sec-ch-ua 改为动态
s = s.replace('            "sec-ch-ua": _SEC_CH_UA,', '            "sec-ch-ua": self.sec_ch_ua,')

C.write_text(s)
ast.parse(s)

import subprocess
print(subprocess.run(
    ["grep", "-n", "_CHROME_BUILDS_MAC = \\|chrome_major\\|sec_ch_ua\\|self.sec_ch_ua",
     str(C)], capture_output=True, text=True).stdout[:900])
print("ok")
