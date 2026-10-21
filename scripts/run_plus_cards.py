"""Plus 家族批量跑卡（走 /pay-protocol，custom checkout 链路）。

标准模式，不碰 promo。每张卡：建单 → pm_ → ctoken_ → ChatGPT confirm → intent confirm。
输出：掩码卡号 + 判定 + 拒卡原因。
"""
import json
import sys
import time
from pathlib import Path

import httpx

API = "http://127.0.0.1:18120"
ORIGIN = "https://<PAY_HOST>"
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


def mask(n: str) -> str:
    return n[:6] + "****" + n[-4:] if len(n) >= 10 else n


def main() -> None:
    auth = json.loads(Path("/opt/gss/config/container-auth.json").read_text())
    env = load_env("/opt/gss/app/cards.env")
    ats = json.load(open("/opt/gss/data/at_list.json"))

    cards = []
    for tag in ("A", "B", "C", "D"):
        num = env.get(f"CARD_{tag}_NUMBER", "")
        if num:
            cards.append({
                "tag": tag, "number": num,
                "exp_month": env.get(f"CARD_{tag}_EXP_MONTH", ""),
                "exp_year": env.get(f"CARD_{tag}_EXP_YEAR", ""),
                "cvc": env.get(f"CARD_{tag}_CVC", ""),
            })
    print(f"卡片数: {len(cards)}")
    print()

    results = []
    for idx, card in enumerate(cards):
        at = ats[idx % len(ats)]
        at_s = at if isinstance(at, str) else at.get("access_token", "")
        tag = f"[{card['tag']}] {mask(card['number'])}"
        try:
            with httpx.Client(base_url=API, timeout=400, follow_redirects=False) as c:
                r = c.post("/login", data={"password": auth["password"]},
                           headers={"Origin": ORIGIN})
                ck = {"Origin": ORIGIN,
                      "Cookie": "__Host-gss_session=" + (r.cookies.get("__Host-gss_session") or "")}
                r = c.post("/api/orders", headers=ck, json={
                    "plan": "plus", "billing_country": "US", "session": at_s,
                    "mode": {"account_source": "customer_session",
                             "card_source": "customer",
                             "billing_mode": "standard",
                             "risk_acknowledged": False},
                })
                if r.status_code != 201:
                    print(f"  {tag} 建单 {r.status_code}: {r.text[:90]}")
                    results.append((tag, "建单失败", None))
                    continue
                oid = r.json()["id"]
                d = c.get("/api/orders/" + oid, headers=ck).json()
                billing = dict(BILLING, email=d.get("session_email") or "billing@example.com")

                body = {"card": {k: card[k] for k in
                                 ("number", "exp_month", "exp_year", "cvc")},
                        "billing": billing}
                r = None
                for attempt in range(3):
                    r = c.post(f"/api/orders/{oid}/pay-protocol", headers=ck, json=body)
                    if r.status_code == 200:
                        break
                    # 代理抖动：换一单重试（同一单的会话可能已被上游作废）
                    print(f"  {tag} 第 {attempt + 1} 次 {r.status_code}: {r.text[:110]}")
                    time.sleep(4)
                    r2 = c.post("/api/orders", headers=ck, json={
                        "plan": "plus", "billing_country": "US", "session": at_s,
                        "mode": {"account_source": "customer_session",
                                 "card_source": "customer",
                                 "billing_mode": "standard",
                                 "risk_acknowledged": False}})
                    if r2.status_code != 201:
                        break
                    oid = r2.json()["id"]
                if r.status_code != 200:
                    print(f"  {tag} 支付 {r.status_code}: {r.text[:110]}")
                    results.append((tag, "支付失败", None))
                    continue
                out = r.json()
                state = out.get("state")
                code = out.get("decline_code") or out.get("code") or ""
                msg = (out.get("message") or "")[:60]
                print(f"  {tag} → {state} {code} {msg}")
                results.append((tag, state, code))
        except Exception as e:
            print(f"  {tag} 异常 {str(e)[:90]}")
            results.append((tag, "异常", None))
        time.sleep(3)

    print()
    print("=" * 62)
    declined = [r for r in results if r[1] == "card_declined"]
    print(f"拿到拒卡: {len(declined)} / {len(results)}")
    for tag, state, code in results:
        print(f"  {tag:<22} {state:<18} {code or ''}")


if __name__ == "__main__":
    main()
