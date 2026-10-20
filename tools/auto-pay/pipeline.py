#!/usr/bin/env python3
"""ChatGPT 订阅自动开通流水线（纯协议 + 浏览器仅取 token）。

架构（参考 DanOps-1/Gpt-Agreement-Payment）：
  ① 建订单（本地 API）           → cs_live URL
  ② init  （纯 HTTP）            → total/return_url/config_id
  ③ 开浏览器，只做被动 captcha   → passive_captcha_token / guid / muid / sid
  ④ payment_methods（纯 HTTP）   → pm_xxx
  ⑤ confirm（纯 HTTP）           → 走到拒卡 / 3DS / 成功
  ⑥ poll（纯 HTTP）              → 最终状态

用法:
  python pipeline.py --session <AT> --card <CARD_NUMBER_PLACEHOLDER>|02|2029|135
  python pipeline.py --checkout-url <url> --card ...
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
import uuid
from pathlib import Path
from urllib.parse import urlencode

import httpx

# ── 常量 ────────────────────────────────────────────────────────────
API_HOST = os.environ.get("GSS_API")
if not API_HOST:
    raise SystemExit("缺少环境变量 GSS_API（例：export GSS_API=http://127.0.0.1:8080）")
STRIPE_API = "https://api.stripe.com"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/155.0.0.0 Safari/537.36")
V_BASE = "2020-08-27;custom_checkout_beta=v1"
V_ELEM = "2026-03-25.dahlia"

STATE_FILE = Path("/tmp/auto_pay_state.json")


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


def new_id() -> str:
    return uuid.uuid4().hex + uuid.uuid4().hex[:6]


# ── ① 建订单 ────────────────────────────────────────────────────────
def create_order(session_at: str, country: str = "US") -> dict:
    log("① 建订单")
    pw = json.loads(Path("/run/gss-auth.json").read_text())["password"]
    c = httpx.Client(base_url=API_HOST, timeout=180, follow_redirects=False)
    c.post("/login", data={"password": pw}, headers={"Origin": API_HOST})
    r = c.post("/api/orders", headers={"Origin": API_HOST}, json={
        "plan": "team", "billing_country": country, "session": session_at,
        "seat_quantity": 2, "workspace_name": "Team Workspace",
        "price_interval": "month"})
    if r.status_code != 201:
        raise RuntimeError(f"建单失败 {r.status_code}: {r.text[:200]}")
    oid = r.json()["id"]
    d = c.get(f"/api/orders/{oid}", headers={"Origin": API_HOST}).json()
    url = d.get("checkout_url")
    log(f"   订单 {oid[:8]}  {str(url)[:60]}")
    return {"order_id": oid, "checkout_url": url}


# ── ② init（纯 HTTP） ───────────────────────────────────────────────
def init_checkout(cs_id: str, pk: str, proxy: str | None) -> tuple[dict, dict]:
    log("② init（纯 HTTP）")
    js_id = str(uuid.uuid4())
    ctx = {"stripe_js_id": js_id}
    data = {
        "browser_locale": "en-US",
        "browser_timezone": "America/Los_Angeles",
        "elements_session_client[elements_init_source]": "custom_checkout",
        "elements_session_client[referrer_host]": "chatgpt.com",
        "elements_session_client[stripe_js_id]": js_id,
        "elements_session_client[locale]": "en-US",
        "elements_session_client[is_aggregation_expected]": "false",
        "key": pk,
        "_stripe_version": V_BASE,
    }
    hdr = {
        "User-Agent": UA,
        "Content-Type": "application/x-www-form-urlencoded",
        "Origin": "https://pay.openai.com",
        "Referer": "https://pay.openai.com/",
    }
    with httpx.Client(proxy=proxy, timeout=60, headers=hdr) as c:
        r = c.post(f"{STRIPE_API}/v1/payment_pages/{cs_id}/init", content=urlencode(data))
    if r.status_code != 200:
        raise RuntimeError(f"init 失败 {r.status_code}: {r.text[:200]}")
    j = r.json()
    due = (j.get("total_summary") or {}).get("due")
    ctx.update({
        "config_id": j.get("config_id", ""),
        "init_checksum": j.get("init_checksum", ""),
        "return_url": j.get("return_url") or j.get("url") or "",
        "stripe_hosted_url": j.get("stripe_hosted_url") or "",
        "locale": j.get("locale") or "en",
        "currency": (j.get("currency") or "usd").lower(),
        "amount": due,
    })
    log(f"   amount={due} {ctx['currency']} config={str(ctx['config_id'])[:12]}")
    return j, ctx


# ── ③ 浏览器只取 token ─────────────────────────────────────────────
def collect_browser_env(checkout_url: str, proxy: str | None) -> dict:
    log("③ 浏览器取 token（唯一用到浏览器的一步）")
    cfg = {"url": checkout_url, "proxy": proxy or "",
           "email": "<BILLING_EMAIL>",
           "card": {"number": os.environ["CARD_A_NUMBER"], "exp": os.environ["CARD_A_EXP"], "cvc": os.environ["CARD_A_CVC"]},
           "name": "Fixture Holder", "line1": "1 Fixture St",
           "city": "Portland", "state": "OR", "zip": "97211"}
    Path("/tmp/env_cfg.json").write_text(json.dumps(cfg))
    r = subprocess.run(
        ["node", str(Path(__file__).parent / "collect_env3.cjs")],
        capture_output=True, text=True, timeout=360,
        env={**os.environ, "DISPLAY": ":99", "ORDER_PROXY": proxy or ""})
    sys.stderr.write(r.stderr[-1500:] if r.stderr else "")
    out = json.loads(Path("/tmp/env_out.json").read_text())
    if not out.get("ok"):
        raise RuntimeError(f"浏览器未拿到 token: {out.get('status') or out}")
    log(f"   token={len(out.get('token') or '')} guid={str(out.get('guid'))[:12]}")
    return out


# ── ④ payment_methods（纯 HTTP） ───────────────────────────────────
def create_payment_method(pk: str, card: dict, billing: dict,
                          guid: str, muid: str, sid: str,
                          config_id: str, proxy: str | None) -> str:
    log("④ payment_methods（纯 HTTP）")
    exp = card["exp"]                      # MMYY 或 MM/YY
    digits = re.sub(r"\D", "", exp)
    mm, yy = digits[:2], digits[2:]
    year = yy if len(yy) == 4 else "20" + yy
    data = {
        "type": "card",
        "card[number]": card["number"],
        "card[exp_month]": mm,
        "card[exp_year]": year,
        "card[cvc]": card["cvc"],
        "billing_details[name]": billing["name"],
        "billing_details[address][line1]": billing["line1"],
        "billing_details[address][city]": billing["city"],
        "billing_details[address][state]": billing["state"],
        "billing_details[address][postal_code]": billing["zip"],
        "billing_details[address][country]": "US",
        "guid": guid, "muid": muid, "sid": sid,
        "key": pk,
        "_stripe_version": V_BASE,
        "payment_user_agent": "stripe.js/2bae21f786; stripe-js-v3/2bae21f786; checkout",
        "client_attribution_metadata[client_session_id]": str(uuid.uuid4()),
        "client_attribution_metadata[checkout_config_id]": config_id,
        "client_attribution_metadata[merchant_integration_source]": "checkout",
        "client_attribution_metadata[merchant_integration_version]": "hosted_checkout",
        "client_attribution_metadata[payment_method_selection_flow]": "merchant_specified",
    }
    hdr = {"User-Agent": UA, "Content-Type": "application/x-www-form-urlencoded",
           "Origin": "https://pay.openai.com", "Referer": "https://pay.openai.com/"}
    with httpx.Client(proxy=proxy, timeout=60, headers=hdr) as c:
        r = c.post(f"{STRIPE_API}/v1/payment_methods", content=urlencode(data))
    if r.status_code != 200:
        raise RuntimeError(f"pm 失败 {r.status_code}: {r.text[:300]}")
    pm = r.json()["id"]
    log(f"   {pm}")
    return pm


# ── ⑤ confirm（纯 HTTP） ───────────────────────────────────────────
def confirm(cs_id: str, pk: str, pm: str, token: str, env: dict,
            ctx: dict, proxy: str | None) -> dict:
    log("⑤ confirm（纯 HTTP）")
    data = {
        "eid": "NA",
        "payment_method": pm,
        "expected_amount": str(ctx.get("amount") or "0"),
        "consent[terms_of_service]": "accepted",
        "save_payment_method": "true",
        "expected_payment_method_type": "card",
        "_stripe_version": V_BASE,
        "guid": env.get("guid", ""),
        "muid": env.get("muid", ""),
        "sid": env.get("sid", ""),
        "key": pk,
        "passive_captcha_token": token,
        "rv_timestamp": env.get("rvTimestamp", ""),
        "client_attribution_metadata[client_session_id]": str(uuid.uuid4()),
        "client_attribution_metadata[checkout_session_id]": cs_id,
        "client_attribution_metadata[merchant_integration_source]": "checkout",
        "client_attribution_metadata[merchant_integration_version]": "hosted_checkout",
        "client_attribution_metadata[payment_method_selection_flow]": "merchant_specified",
        "client_attribution_metadata[checkout_config_id]": ctx.get("config_id", ""),
        "link_brand": "link",
    }
    if env.get("ekey"):
        data["passive_captcha_ekey"] = env["ekey"]
    hdr = {"User-Agent": UA, "Content-Type": "application/x-www-form-urlencoded",
           "Origin": "https://pay.openai.com", "Referer": "https://pay.openai.com/"}
    with httpx.Client(proxy=proxy, timeout=180, headers=hdr) as c:
        r = c.post(f"{STRIPE_API}/v1/payment_pages/{cs_id}/confirm", content=urlencode(data))
    j = {}
    try: j = r.json()
    except Exception: j = {"raw": r.text[:400]}
    log(f"   HTTP {r.status_code}")
    return {"status_code": r.status_code, "body": j}


# ── ⑥ poll（纯 HTTP） ──────────────────────────────────────────────
def poll(cs_id: str, pk: str, proxy: str | None, tries: int = 6) -> dict:
    log("⑥ poll（纯 HTTP）")
    hdr = {"User-Agent": UA, "Content-Type": "application/x-www-form-urlencoded",
           "Origin": "https://pay.openai.com", "Referer": "https://pay.openai.com/"}
    last = {}
    with httpx.Client(proxy=proxy, timeout=60, headers=hdr) as c:
        for i in range(tries):
            r = c.post(f"{STRIPE_API}/v1/payment_pages/{cs_id}/poll",
                       content=urlencode({"key": pk, "_stripe_version": V_BASE}))
            try: last = r.json()
            except Exception: last = {"raw": r.text[:300]}
            st = (last.get("payment_intent") or {}).get("status") or last.get("status")
            log(f"   [{i+1}] HTTP {r.status_code} status={st}")
            if st in ("succeeded", "requires_capture", "canceled"):
                break
            if (last.get("payment_intent") or {}).get("last_payment_error"):
                break
            time.sleep(3)
    return last


# ── 结果判定 ────────────────────────────────────────────────────────
def classify(res: dict) -> dict:
    """从 confirm/poll 结果里提取最终判定与 decline code。"""
    txt = json.dumps(res)[:8000]
    out = {"state": "unknown", "decline_code": None, "error_code": None,
           "message": None, "intent_status": None}
    for holder in ("payment_intent", "setup_intent"):
        obj = res.get(holder) if isinstance(res, dict) else None
        if isinstance(obj, dict):
            out["intent_status"] = obj.get("status")
            err = obj.get("last_payment_error") or obj.get("last_setup_error") or {}
            if err:
                out["error_code"] = err.get("code")
                out["decline_code"] = err.get("decline_code")
                out["message"] = (err.get("message") or "")[:200]
    err = res.get("error") if isinstance(res, dict) else None
    if isinstance(err, dict):
        out["error_code"] = out["error_code"] or err.get("code")
        out["decline_code"] = out["decline_code"] or err.get("decline_code")
        out["message"] = out["message"] or (err.get("message") or "")[:200]

    st = (out["intent_status"] or "").lower()
    if st in ("succeeded", "requires_capture"):
        out["state"] = "paid"
    elif out["decline_code"] or out["error_code"]:
        if "captcha" in (out["message"] or "").lower():
            out["state"] = "captcha_rejected"
        elif st == "requires_payment_method":
            out["state"] = "card_declined"
        else:
            out["state"] = "error"
    elif st == "requires_action":
        out["state"] = "3ds_required"
    return out


def run(checkout_url: str | None, session_at: str | None, card: dict,
        billing: dict, proxy: str | None) -> dict:
    result = {}
    # ① 订单
    if checkout_url:
        m = re.search(r"(cs_live_[A-Za-z0-9]+)", checkout_url)
        if not m: raise RuntimeError("URL 里没有 cs_live_")
        cs_id = m.group(1)
        result["checkout_url"] = checkout_url
    else:
        o = create_order(session_at)
        checkout_url, cs_id = o["checkout_url"], re.search(
            r"(cs_live_[A-Za-z0-9]+)", o["checkout_url"]).group(1)
        result["order_id"] = o["order_id"]
        result["checkout_url"] = checkout_url
    result["cs_id"] = cs_id

    # pk（publishable key）
    pk = "${STRIPE_PUBLISHABLE_KEY}"

    # ② init
    init_resp, ctx = init_checkout(cs_id, pk, proxy)
    result["init"] = {"amount": ctx.get("amount"), "currency": ctx.get("currency"),
                      "return_url": ctx.get("return_url")}

    # ③ 浏览器 token
    env = collect_browser_env(checkout_url, proxy)
    result["env"] = {k: env.get(k) for k in
                     ("guid", "muid", "sid", "pm", "configId", "rvTimestamp")}
    result["token_len"] = len(env.get("token") or "")

    # ④ pm
    pm = create_payment_method(pk, card, billing,
                               env.get("guid", ""), env.get("muid", ""),
                               env.get("sid", ""), ctx.get("config_id", ""), proxy)
    result["pm"] = pm

    # ⑤ confirm
    cf = confirm(cs_id, pk, pm, env.get("token", ""), env, ctx, proxy)
    result["confirm"] = cf
    cls = classify(cf["body"])
    result["verdict"] = cls
    log(f"   判定: {cls['state']}  decline={cls['decline_code']}  msg={(cls['message'] or '')[:60]}")

    # ⑥ poll（仅在没拿到终态时）
    if cls["state"] in ("unknown", "3ds_required"):
        p = poll(cs_id, pk, proxy)
        result["poll"] = p
        cls2 = classify(p)
        if cls2["state"] != "unknown":
            result["verdict"] = cls2
            log(f"   poll 判定: {cls2['state']} decline={cls2['decline_code']}")

    return result


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--checkout-url")
    ap.add_argument("--session")
    ap.add_argument("--card", default="<CARD_NUMBER_PLACEHOLDER>|02|2029|135",
                    help="number|MM|YYYY|CVC")
    ap.add_argument("--name", default="Fixture Holder")
    ap.add_argument("--line1", default="1 Fixture St")
    ap.add_argument("--city", default="Portland")
    ap.add_argument("--state", default="OR")
    ap.add_argument("--zip", default="97211")
    ap.add_argument("--email", default="<BILLING_EMAIL>")
    ap.add_argument("--proxy", default=os.environ.get("ORDER_PROXY"))
    ap.add_argument("--out", default="/tmp/auto_pay_result.json")
    a = ap.parse_args()

    n, mm, yy, cvc = a.card.split("|")
    card = {"number": n, "exp": f"{mm}{yy}", "cvc": cvc}
    billing = {"name": a.name, "line1": a.line1, "city": a.city,
               "state": a.state, "zip": a.zip}
    try:
        res = run(a.checkout_url, a.session, card, billing, a.proxy)
    except Exception as e:
        res = {"error": str(e)[:400]}
        log("失败:", res["error"])
    Path(a.out).write_text(json.dumps(res, ensure_ascii=False, indent=2, default=str))
    v = res.get("verdict") or {}
    print(json.dumps({"state": v.get("state"), "decline_code": v.get("decline_code"),
                      "message": v.get("message"), "token_len": res.get("token_len"),
                      "error": res.get("error")}, ensure_ascii=False, indent=1))
    print("saved ->", a.out)


if __name__ == "__main__":
    main()
