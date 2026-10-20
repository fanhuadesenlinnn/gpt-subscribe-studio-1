#!/usr/bin/env python3
"""每轮：新订单 + 新 AT + 卡轮换；同一 session 用两个全新实例尝试（第二次常能跳过 challenge）。"""
from __future__ import annotations

import json
import os
import random
import sys
from pathlib import Path

sys.path.insert(0, "/tmp")
from sp import StripeProtocol

CARDS = [
    {"number": os.environ["CARD_A_NUMBER"], "exp_month": os.environ["CARD_A_EXP_MONTH"], "exp_year": os.environ["CARD_A_EXP_YEAR"], "cvc": os.environ["CARD_A_CVC"]},
    {"number": os.environ["CARD_B_NUMBER"], "exp_month": os.environ["CARD_B_EXP_MONTH"], "exp_year": os.environ["CARD_B_EXP_YEAR"], "cvc": os.environ["CARD_B_CVC"]},
    {"number": os.environ["CARD_C_NUMBER"], "exp_month": os.environ["CARD_C_EXP_MONTH"], "exp_year": os.environ["CARD_C_EXP_YEAR"], "cvc": os.environ["CARD_C_CVC"]},
    {"number": os.environ["CARD_D_NUMBER"], "exp_month": os.environ["CARD_D_EXP_MONTH"], "exp_year": os.environ["CARD_D_EXP_YEAR"], "cvc": os.environ["CARD_D_CVC"]},
]
BILLING = {
    "name": "FIXTURE HOLDER", "email": "<BILLING_EMAIL>",
    "line1": "1 Fixture St", "city": "Portland",
    "state": "OR", "zip": "97211", "country": "US",
}



EMAIL_DOMAINS = ["gmail.com", "yahoo.com", "outlook.com", "hotmail.com", "icloud.com"]
FIRST = ["JAMES", "JOHN", "ROBERT", "MICHAEL", "WILLIAM", "DAVID", "RICHARD",
         "THOMAS", "DANIEL", "MATTHEW", "MARY", "JENNIFER", "LINDA", "SARAH"]
LAST = ["SMITH", "JOHNSON", "WILLIAMS", "BROWN", "JONES", "GARCIA", "MILLER",
        "DAVIS", "WILSON", "ANDERSON", "TAYLOR", "THOMAS", "MOORE", "MARTIN"]
ADDRESSES = [
    ("1 Fixture St", "Portland", "OR", "97211"),
    ("823 SE Morrison St", "Portland", "OR", "97214"),
    ("4401 N Mississippi Ave", "Portland", "OR", "97217"),
    ("930 NW 23rd Ave", "Portland", "OR", "97210"),
    ("2712 SE Hawthorne Blvd", "Portland", "OR", "97214"),
    ("1845 NE 7th Ave", "Portland", "OR", "97212"),
]


def new_identity() -> dict:
    """每次生成全新身份，避免邮箱/地址信誉累积。"""
    import secrets
    import string
    user = "".join(secrets.choice(string.ascii_lowercase + string.digits)
                   for _ in range(random.randint(9, 13)))
    line1, city, st, zipc = random.choice(ADDRESSES)
    return {
        "name": f"{random.choice(FIRST)} {random.choice(LAST)}",
        "email": f"{user}@{random.choice(EMAIL_DOMAINS)}",
        "line1": line1, "city": city, "state": st, "zip": zipc,
        "country": "US",
    }


def load_ats(path: str = "/tmp/at_list.json") -> list[str]:
    return json.loads(Path(path).read_text())


def new_proxy() -> str:
    """生成新 sid，得到全新出口 IP。"""
    import secrets
    base = os.environ.get("ORDER_PROXY_BASE") or os.environ.get("ORDER_PROXY", "")
    if not base:
        return ""
    sid = "".join(secrets.choice("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789")
                  for _ in range(8))
    import re
    return re.sub(r"sid-[A-Za-z0-9]+", "sid-" + sid, base)


def make_order(at: str) -> dict | None:
    """容器内直连本地 API 建单（手动带 __Host- cookie）。"""
    import httpx
    try:
        auth = json.loads(Path("/run/gss-auth.json").read_text())
    except Exception as e:
        print("  读 auth 失败:", str(e)[:80])
        return None
    origin = os.environ.get("GSS_PUBLIC_ORIGIN")
    if not origin:
        print("  缺少 GSS_PUBLIC_ORIGIN（例：export GSS_PUBLIC_ORIGIN=http://127.0.0.1:8080）")
        return None
    try:
        with httpx.Client(base_url="http://127.0.0.1:8080", timeout=180,
                          follow_redirects=False) as c:
            r = c.post("/login", data={"password": auth["password"]},
                       headers={"Origin": origin})
            tok = r.cookies.get("__Host-gss_session")
            if not tok:
                print(f"  登录失败 {r.status_code}")
                return None
            ck = {"Origin": origin, "Cookie": "__Host-gss_session=" + tok}
            r = c.post("/api/orders", headers=ck, json={
                "plan": "team", "billing_country": "US", "session": at,
                "seat_quantity": 2, "workspace_name": "Team Workspace",
                "price_interval": "month"})
            if r.status_code != 201:
                print(f"  建单 {r.status_code}: {r.text[:80]}")
                return None
            oid = r.json()["id"]
            d = c.get("/api/orders/" + oid, headers=ck).json()
            url = d.get("checkout_url")
            if not url:
                print("  checkout_url 为空")
                return None
            return {"order": oid, "url": url}
    except Exception as e:
        print("  建单异常:", str(e)[:100])
        return None


def egress_ip(proxy: str) -> str:
    """查询代理出口 IP。"""
    import httpx
    try:
        with httpx.Client(proxy=proxy, timeout=15) as c:
            return c.get("https://api.ipify.org").text.strip()
    except Exception:
        return "?"


def attempt(url: str, card: dict) -> dict:
    """一次完整尝试（全新实例，新 guid、新 init）。"""
    proxy = new_proxy() or os.environ.get("ORDER_PROXY")
    ip = egress_ip(proxy)
    print(f"     IP={ip}")
    sp = StripeProtocol(
        proxy=proxy,
        captcha_key=os.environ.get("GSS_CAPTCHA_API_KEY", ""),
        captcha_url=os.environ.get("GSS_CAPTCHA_API_URL", "https://api.yescaptcha.com"),
        card=card, billing=BILLING,
    )
    try:
        out = sp.pay_from_url(url)
        return {"state": out.state, "decline": out.decline_code,
                "code": out.code, "message": out.message,
                "pm": out.payment_method_id, "ip": ip}
    except Exception as e:
        return {"state": "error", "message": str(e)[:120], "ip": ip}
    finally:
        sp.close()


def main() -> None:
    rounds = int(os.environ.get("ROUNDS", "4"))
    tries = int(os.environ.get("TRIES", "2"))
    ats = load_ats()
    random.shuffle(ats)
    results = []

    for i in range(rounds):
        card = CARDS[i % len(CARDS)]
        at = ats[i % len(ats)]
        tag = "****" + card["number"][-4:]
        print(f"\n{'='*58}")
        print(f"第 {i+1}/{rounds} 轮  卡 {tag}  AT ...{at[-8:]}")
        print("=" * 58)

        probe = new_proxy()
        print(f"  代理 sid 轮换 -> {probe.split('sid-')[-1].split('-')[0] if 'sid-' in probe else '?'}")
        order = make_order(at)
        if not order:
            results.append({"round": i + 1, "card": tag, "state": "order_failed"})
            continue
        print(f"  订单 {order['order'][:8]}")

        hit = None
        for t in range(1, tries + 1):
            print(f"\n  — 第 {t} 次尝试 —")
            r = attempt(order["url"], card)
            print(f"     state={r['state']} decline={r.get('decline')} code={r.get('code')}")
            if r["state"] in ("card_declined", "paid"):
                hit = r
                print(f"     ★★★ 命中 {r['state']} ★★★")
                break

        rec = hit or r
        rec.update({"round": i + 1, "card": tag, "order": order["order"],
                    "attempts": t})
        results.append(rec)

    Path("/tmp/loop_result.json").write_text(json.dumps(results, indent=1))
    print(f"\n{'='*58}")
    print("汇总")
    print("=" * 58)
    for r in results:
        print(f"  第{r.get('round')}轮 {r.get('card','?')}  "
              f"state={r.get('state','-')}  decline={r.get('decline','-')}  "
              f"尝试{r.get('attempts','-')}次")


if __name__ == "__main__":
    main()
