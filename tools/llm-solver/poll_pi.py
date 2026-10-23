"""轮询 payment_intent 最终状态，直到出现拒卡 / 成功。

第二阶段工具：不依赖浏览器，可以长时间跑，不受浏览器超时限制。

用法：
    python poll_pi.py                          # 读 /tmp/stage1.json
    python poll_pi.py --pi pi_xxx --cs pi_xxx_secret_yyy
    python poll_pi.py --interval 30 --max 120  # 每 30 秒一次，最多 120 次
"""
import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, "/opt/gss/app/packages/core/src")

import httpx
from gss_core.proxies import align_proxy_region, configured_proxy
from gss_core.stripe_protocol import PUBLISHABLE_KEY, V_HOSTED

STRIPE = "https://api.stripe.com"
DONE = ("succeeded", "canceled")
BAD = ("requires_payment_method", "requires_confirmation")


def load_target(args):
    if args.pi and args.cs:
        return args.pi, args.cs
    p = Path(args.json)
    if not p.exists():
        sys.exit("缺少 %s，或用 --pi/--cs 指定" % p)
    d = json.loads(p.read_text())
    if "payment_intent" in d:
        pi = d.get("payment_intent") or {}
    elif "pi" in d:
        pi = d.get("pi") or {}
    else:
        pi = d
    return pi.get("id"), pi.get("client_secret")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pi")
    ap.add_argument("--cs")
    ap.add_argument("--json", default="/tmp/stage1.json")
    ap.add_argument("--interval", type=int, default=15)
    ap.add_argument("--max", type=int, default=40)
    args = ap.parse_args()

    pi_id, cs = load_target(args)
    if not pi_id or not cs:
        sys.exit("没拿到 pi_id / client_secret")

    print("轮询 %s  每 %ds，最多 %d 次" % (pi_id, args.interval, args.max), flush=True)

    proxy = align_proxy_region(configured_proxy("checkout"), "US")
    headers = {"Stripe-Version": V_HOSTED}
    url = STRIPE + "/v1/payment_intents/" + pi_id

    with httpx.Client(timeout=45, proxy=proxy, verify=False, headers=headers) as c:
        for i in range(1, args.max + 1):
            try:
                r = c.get(url, params={
                    "client_secret": cs,
                    "key": PUBLISHABLE_KEY,
                    "_stripe_version": V_HOSTED,
                })
                j = r.json() if r.content else {}
            except Exception as e:
                print("[%d] 请求异常 %s" % (i, str(e)[:70]), flush=True)
                time.sleep(args.interval)
                continue

            if r.status_code >= 400:
                err = j.get("error") or {}
                print("[%d] HTTP %d %s %s" % (
                    i, r.status_code, err.get("code"),
                    str(err.get("message"))[:90]), flush=True)
            else:
                st = j.get("status")
                lpe = j.get("last_payment_error") or {}
                na = j.get("next_action") or {}
                print("[%d] status=%s decline=%s code=%s next_action=%s" % (
                    i, st, lpe.get("decline_code"), lpe.get("code"),
                    na.get("type")), flush=True)

                if lpe:
                    print("      >> %s" % str(lpe.get("message"))[:120], flush=True)
                if na:
                    print("      >> next_action: %s" % (
                        json.dumps(na, ensure_ascii=False)[:220]), flush=True)

                if st in DONE:
                    print("\n=== 最终 %s ===" % st, flush=True)
                    return
                if lpe.get("decline_code") or st in BAD:
                    print("\n=== 拒卡 decline_code=%s ===" % lpe.get("decline_code"),
                          flush=True)
                    return

            time.sleep(args.interval)

    print("\n=== 轮询结束，状态仍未落定 ===", flush=True)


if __name__ == "__main__":
    main()
