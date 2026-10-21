"""Plus 纯协议全链路验证：建单 → pm_ → ctoken_ → confirm → intent → 判定。

只用标准模式（billing_mode=standard），不碰 promo。
目标：拿到明确结果（拒卡 / 成功 / 需 3DS）。
"""
import json
import re
import secrets
import sys
from pathlib import Path

import httpx

from tax_free_billing import random_tax_free_billing

sys.path.insert(0, "/opt/gss/app/packages/core/src")
from gss_core.plus_protocol import PlusProtocol
from gss_core.proxies import align_proxy_region, configured_proxy

API = "http://127.0.0.1:18120"
ORIGIN = "https://<PAY_HOST>"


def load_env(path: str) -> dict:
    out = {}
    for line in Path(path).read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def main() -> None:
    auth = json.loads(Path("/opt/gss/config/container-auth.json").read_text())
    env = load_env("/opt/gss/app/cards.env")
    ats = json.load(open("/opt/gss/data/at_list.json"))

    card = {
        "number": env.get("CARD_A_NUMBER", ""),
        "exp_month": env.get("CARD_A_EXP_MONTH", ""),
        "exp_year": env.get("CARD_A_EXP_YEAR", ""),
        "cvc": env.get("CARD_A_CVC", ""),
    }
    at = ats[0] if isinstance(ats[0], str) else ats[0].get("access_token", "")

    with httpx.Client(base_url=API, timeout=300, follow_redirects=False) as c:
        r = c.post("/login", data={"password": auth["password"]}, headers={"Origin": ORIGIN})
        ck = {"Origin": ORIGIN,
              "Cookie": "__Host-gss_session=" + (r.cookies.get("__Host-gss_session") or "")}
        r = c.post("/api/orders", headers=ck, json={
            "plan": "plus",
            "billing_country": "US",
            "session": at,
            "mode": {"account_source": "customer_session",
                     "card_source": "customer",
                     "billing_mode": "standard",
                     "risk_acknowledged": False},
        })
        if r.status_code != 201:
            print("建单失败", r.status_code, r.text[:200])
            sys.exit(1)
        oid = r.json()["id"]
        d = c.get("/api/orders/" + oid, headers=ck).json()

    url = d.get("checkout_url") or ""
    pk = d.get("publishable_key") or ""
    currency = d.get("currency") or "USD"
    print("订单:", oid[:12])
    print("会话:", (re.search(r"oaics_[A-Za-z0-9]+", url) or [""])[0][:20] or "(无)")
    print("币种:", currency, " 金额:", d.get("amount"))

    # 与前端一致：五个免税州随机生成地址
    billing = random_tax_free_billing(
        email=d.get("session_email") or "billing@example.com",
        name="FIXTURE HOLDER",
    )

    proxy = align_proxy_region(configured_proxy("checkout"), "US")
    if proxy:
        proxy = re.sub(r"sid-[A-Za-z0-9]+", "sid-" + secrets.token_hex(4), proxy)
    print("出口:", (proxy.split("@")[-1] if proxy else "(直连)"))
    print()

    pp = PlusProtocol(
        session=at, checkout_url=url, publishable_key=pk,
        card=card, billing=billing, proxy=proxy, currency=currency,
    )
    try:
        out = pp.pay()
    finally:
        pp.close()

    print()
    print("=" * 60)
    print("state        :", out.state)
    print("code         :", out.code)
    print("decline_code :", out.decline_code)
    print("message      :", (out.message or "")[:200])
    print("intent_status:", out.intent_status)
    print("payment_meth :", (out.payment_method_id or "")[:16])


if __name__ == "__main__":
    main()
