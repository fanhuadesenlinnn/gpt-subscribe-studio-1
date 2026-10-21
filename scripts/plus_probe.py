"""阶段 1 探测：Plus（custom checkout）能不能在服务端造出 pm_。

流程：
  1. 登录本服务，建一张 Plus 订单（standard 模式，不用 promo）
  2. 取 checkout_url（oaics_）与 publishable_key
  3. 用这个 pk 向 Stripe /v1/payment_methods 提交卡号，试两种 attribution 组合
  4. 打印状态码与错误

不产生扣款：只创建 PaymentMethod。
"""
import json
import re
import secrets
import sys
import uuid
from pathlib import Path
from urllib.parse import urlencode

import httpx

sys.path.insert(0, "/opt/gss/app/packages/core/src")
from gss_core.proxies import align_proxy_region, configured_proxy

API = "http://127.0.0.1:18120"
ORIGIN = "https://<PAY_HOST>"
STRIPE = "https://api.stripe.com"
UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/155.0.0.0 Safari/537.36")
RUNTIME_HASH = "5412f474d5"
V = "2020-08-27;custom_checkout_beta=v1"

BILLING = {
    "name": "FIXTURE HOLDER", "email": "", "line1": "1 Fixture St",
    "city": "Portland", "state": "OR", "postal_code": "97211", "country": "US",
}


def load_env(path: str) -> dict:
    out = {}
    for line in Path(path).read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def mask(value: str, keep: int = 10) -> str:
    return value[:keep] + "…" if value else "(空)"


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
    if not card["number"]:
        print("cards.env 缺 CARD_A_NUMBER")
        sys.exit(1)

    at = ats[0] if isinstance(ats[0], str) else ats[0].get("access_token", "")

    with httpx.Client(base_url=API, timeout=300, follow_redirects=False) as c:
        r = c.post("/login", data={"password": auth["password"]}, headers={"Origin": ORIGIN})
        ck = {"Origin": ORIGIN,
              "Cookie": "__Host-gss_session=" + (r.cookies.get("__Host-gss_session") or "")}

        print("建 Plus 订单（standard 模式）…")
        r = c.post("/api/orders", headers=ck, json={
            "plan": "plus",
            "billing_country": "US",
            "session": at,
            "mode": {"account_source": "customer_session",
                     "card_source": "customer",
                     "billing_mode": "standard",
                     "risk_acknowledged": False},
        })
        print("  建单:", r.status_code)
        if r.status_code != 201:
            print("  ", r.text[:300])
            sys.exit(1)
        oid = r.json()["id"]

        d = c.get("/api/orders/" + oid, headers=ck).json()
        url = d.get("checkout_url") or ""
        pk = d.get("publishable_key") or ""
        BILLING["email"] = d.get("session_email") or "billing@example.com"
        print("  checkout_url:", mask(url, 46))
        print("  publishable_key:", mask(pk, 12))
        print("  amount/currency:", d.get("amount"), d.get("currency"))
        print("  checkout_kind:", d.get("checkout_kind"))
        m = re.search(r"(oaics_[A-Za-z0-9]+)", url)
        print("  会话 id:", mask(m.group(1), 16) if m else "(没取到 oaics_)")
        if not pk:
            print("  ⚠️ 没有 publishable_key，后面没法造 pm_")
            sys.exit(1)

    proxy = align_proxy_region(configured_proxy("checkout"), "US")
    proxy = re.sub(r"sid-[A-Za-z0-9]+", "sid-" + secrets.token_hex(4), proxy or "")
    print("\n出口:", (proxy.split("@")[-1] if proxy else "(直连)"))

    guid, muid, sid = str(uuid.uuid4()), str(uuid.uuid4()), str(uuid.uuid4())
    base = {
        "billing_details[name]": BILLING["name"],
        "billing_details[email]": BILLING["email"],
        "billing_details[address][country]": BILLING["country"],
        "billing_details[address][line1]": BILLING["line1"],
        "billing_details[address][city]": BILLING["city"],
        "billing_details[address][postal_code]": BILLING["postal_code"],
        "billing_details[address][state]": BILLING["state"],
        "type": "card",
        "card[number]": card["number"],
        "card[cvc]": card["cvc"],
        "card[exp_year]": card["exp_year"],
        "card[exp_month]": card["exp_month"],
        "payment_user_agent": f"stripe.js/{RUNTIME_HASH}; stripe-js-v3/{RUNTIME_HASH}; payment-element",
        "guid": guid, "muid": muid, "sid": sid,
        "key": pk,
        "_stripe_version": V,
    }

    variants = {
        "A hosted 风格": {
            "client_attribution_metadata[client_session_id]": str(uuid.uuid4()),
            "client_attribution_metadata[checkout_session_id]": m.group(1) if m else "",
            "client_attribution_metadata[merchant_integration_source]": "checkout",
            "client_attribution_metadata[merchant_integration_version]": "hosted_checkout",
            "client_attribution_metadata[payment_method_selection_flow]": "merchant_specified",
        },
        "B elements 风格": {
            "client_attribution_metadata[client_session_id]": str(uuid.uuid4()),
            "client_attribution_metadata[merchant_integration_source]": "elements",
            "client_attribution_metadata[merchant_integration_version]": "2021",
            "client_attribution_metadata[payment_method_selection_flow]": "automatic",
        },
        "C 无 attribution": {},
    }

    headers = {
        "User-Agent": UA,
        "Content-Type": "application/x-www-form-urlencoded",
        "Origin": "https://js.stripe.com",
        "Referer": "https://js.stripe.com/",
    }

    print("\n=== 造 pm_ 探测 ===")
    with httpx.Client(proxy=proxy or None, timeout=90,
                      headers=headers, verify=False) as sc:
        for name, extra in variants.items():
            body = {**base, **extra}
            try:
                r = sc.post(f"{STRIPE}/v1/payment_methods", content=urlencode(body))
                ok = r.status_code == 200
                payload = r.json() if r.content else {}
                if ok:
                    pm = payload.get("id", "")
                    c = payload.get("card") or {}
                    print(f"  [{name}] {r.status_code} ✅ {mask(pm, 14)} "
                          f"{c.get('display_brand')} ****{c.get('last4')}")
                else:
                    err = (payload.get("error") or {})
                    print(f"  [{name}] {r.status_code} ❌ "
                          f"{err.get('type')} / {err.get('code')} / {str(err.get('message'))[:110]}")
            except Exception as e:
                print(f"  [{name}] 异常 {str(e)[:110]}")


if __name__ == "__main__":
    main()
