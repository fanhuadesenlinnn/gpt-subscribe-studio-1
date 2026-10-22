"""用 Stripe.js 的 handleNextAction 让 Stripe 自己发起挑战，我们在它的 iframe 里作答。

为什么要这么改：自建页面渲染 hCaptcha 即使 rqdata 一致，hCaptcha 内部仍是另一个
会话实例，Stripe 校验时判无效（十四轮实测）。handleNextAction 渲染的挑战才属于
Stripe 自己的会话。
"""
import asyncio
import base64
import json
import os
import re
import secrets
import sys
from pathlib import Path
from urllib.parse import unquote, urlsplit

import httpx

sys.path.insert(0, "/opt/gss/app/packages/core/src")
from gss_core.proxies import align_proxy_region, configured_proxy
from gss_core.stripe_protocol import PUBLISHABLE_KEY, StripeProtocol

API = "http://127.0.0.1:18120"
ORIGIN = os.environ.get("GSS_PUBLIC_ORIGIN", "https://<PAY_HOST>")
CHROME = "/usr/bin/google-chrome"
CAPTCHA_KEY = os.environ.get("GSS_CAPTCHA_API_KEY", "")

PAGE = """<!doctype html><html><head><meta charset="utf-8">
<script src="https://js.stripe.com/v3/"></script>
</head><body style="margin:0;padding:16px;font:13px monospace">
<pre id="log"></pre><div id="out"></div>
<script>
window.__state = {phase: 'init'};
function P(o){ window.__state = o; document.getElementById('log').textContent = JSON.stringify(o, null, 1); }
window.__run = async function(cs, pk) {
  try {
    const stripe = Stripe(pk);
    P({phase: 'calling'});
    const res = await stripe.handleNextAction({ clientSecret: cs });
    P({phase: 'done', error: res.error ? (res.error.code + ':' + res.error.message) : null,
       pi: res.paymentIntent ? res.paymentIntent.status : null});
  } catch (e) { P({phase: 'throw', msg: String(e).slice(0, 300)}); }
};
</script></body></html>"""


def load_env(path):
    out = {}
    for line in Path(path).read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def proxy_cfg(url):
    if not url:
        return None
    u = urlsplit(url)
    cfg = {"server": f"{u.scheme or 'http'}://{u.hostname}:{u.port or 3010}"}
    if u.username:
        cfg["username"] = unquote(u.username)
        cfg["password"] = unquote(u.password or "")
    return cfg


async def classify(imgs, question):
    if not CAPTCHA_KEY:
        return {}
    task = {"type": "HCaptchaClassification", "queries": imgs}
    if question:
        task["question"] = question
    async with httpx.AsyncClient(timeout=90) as c:
        r = await c.post("https://api.yescaptcha.com/createTask",
                         json={"clientKey": CAPTCHA_KEY, "task": task})
        j = r.json() or {}
    if j.get("errorId"):
        return {"error": j.get("errorCode")}
    return j.get("solution") or {}


async def main():
    auth = json.loads(Path("/opt/gss/config/container-auth.json").read_text())
    env = load_env("/opt/gss/app/cards.env")
    ats = json.load(open("/opt/gss/data/at_list.json"))

    d = None
    for i in range(4):
        at = ats[i] if isinstance(ats[i], str) else ats[i].get("access_token", "")
        with httpx.Client(base_url=API, timeout=300, follow_redirects=False) as c:
            r = c.post("/login", data={"password": auth["password"]}, headers={"Origin": ORIGIN})
            ck = {"Origin": ORIGIN,
                  "Cookie": "__Host-gss_session=" + (r.cookies.get("__Host-gss_session") or "")}
            r = c.post("/api/orders", headers=ck, json={
                "plan": "team", "billing_country": "US", "session": at,
                "seat_quantity": 2, "workspace_name": "Team Workspace",
                "mode": {"account_source": "customer_session", "card_source": "customer",
                         "billing_mode": "standard", "risk_acknowledged": False}})
            if r.status_code == 201:
                d = c.get("/api/orders/" + r.json()["id"], headers=ck).json()
                break
            await asyncio.sleep(4)
    if not d:
        print("建单失败")
        return

    billing = {"name": "FIXTURE HOLDER", "email": d.get("session_email") or "b@example.com",
               "line1": "1 Fixture St", "city": "Portland", "state": "OR",
               "postal_code": "97211", "country": "US"}
    card = {"number": env["CARD_A_NUMBER"], "exp_month": env["CARD_A_EXP_MONTH"],
            "exp_year": env["CARD_A_EXP_YEAR"], "cvc": env["CARD_A_CVC"]}
    proxy = align_proxy_region(configured_proxy("checkout"), "US")
    proxy = re.sub(r"sid-[A-Za-z0-9]+", "sid-" + secrets.token_hex(4), proxy or "") or None

    sp = StripeProtocol(proxy=proxy, captcha_key="", captcha_url="", card=card, billing=billing)
    try:
        out = sp.pay_from_url(d.get("checkout_url") or "")
    finally:
        sp.close()
    body = ((getattr(out, "raw", {}) or {}).get("confirm") or {}).get("body") or {}
    na = body.get("next_action") or (body.get("payment_intent") or {}).get("next_action") or {}
    sdk = na.get("use_stripe_sdk") or {}
    if sdk.get("type") != "intent_confirmation_challenge":
        print("未命中挑战:", out.state)
        return
    cs = (body.get("payment_intent") or {}).get("client_secret") or sdk.get("client_secret") or ""
    print("命中挑战，client_secret:", cs[:12], "len", len(cs), flush=True)

    from playwright.async_api import async_playwright
    async with async_playwright() as p:
        b = await p.chromium.launch(executable_path=CHROME, headless=False,
                                    args=["--no-sandbox", "--disable-dev-shm-usage",
                                          "--window-size=1280,980"])
        ctx = await b.new_context(proxy=proxy_cfg(proxy), viewport={"width": 1280, "height": 980})
        page = await ctx.new_page()

        # 用 route 直接返回主文档，跳过真实导航（真实导航经代理会挂死）
        async def _route(route):
            try:
                req = route.request
                if req.resource_type == "document":
                    await route.fulfill(status=200, content_type="text/html; charset=utf-8",
                                        body=PAGE)
                else:
                    await route.continue_()
            except Exception:
                try:
                    await route.continue_()
                except Exception:
                    pass

        await page.route("**/*", _route)
        print("已装 route，开始导航…", flush=True)
        await page.goto("https://example.com/", wait_until="domcontentloaded", timeout=25000)
        print("主文档已就位", flush=True)
        for _ in range(60):
            if await page.evaluate("typeof window.Stripe === 'function'"):
                break
            await page.wait_for_timeout(500)
        print("Stripe.js:", await page.evaluate("typeof window.Stripe"), flush=True)

        print("调用 handleNextAction…", flush=True)
        await page.evaluate("([cs, pk]) => { window.__run(cs, pk); return 'started'; }",
                            [cs, PUBLISHABLE_KEY])
        print("已调用 handleNextAction", flush=True)
        await page.wait_for_timeout(6000)
        print("状态:", await page.evaluate("JSON.stringify(window.__state)"))

        for rnd in range(1, 5):
            allf = [f.url or "about:blank" for f in page.frames]
            print(f"[{rnd}] 全部 frame({len(allf)}):", [u[:55] for u in allf], flush=True)
            frames = [f for f in page.frames
                      if any(k in (f.url or "") for k in ("hcaptcha", "hooks.stripe"))]
            print(f"[{rnd}] 候选 iframe:", [f.url[:60] for f in frames], flush=True)
            got = False
            for fr in frames:
                try:
                    n = await fr.locator('div[role=button][aria-label*="Challenge Image"]').count()
                except Exception:
                    n = 0
                try:
                    prompt = await fr.evaluate(
                        """() => { const e = document.querySelector('.prompt-text')
                             || document.querySelector('div[class*=prompt]');
                             return e ? e.innerText : ''; }""")
                except Exception:
                    prompt = ""
                prompt = " ".join(str(prompt).split())[:150]
                cells = fr.locator('div[role=button][aria-label*="Challenge Image"]')
                tiles = []
                for i in range(min(n, 9)):
                    fp = Path(f"/tmp/ha_t{i}.png")
                    try:
                        await cells.nth(i).screenshot(path=str(fp), timeout=6000)
                        tiles.append(fp)
                    except Exception:
                        tiles.append(None)
                if any(tiles):
                    sol = await classify(
                        [base64.b64encode(Path(t).read_bytes()).decode() for t in tiles if t],
                        prompt)
                    idx = [i for i, v in enumerate(sol.get("objects") or []) if v]
                    print(f"    题面={prompt[:60]} 服务商={idx}")
                    boxes = await fr.evaluate(
                        """() => [...document.querySelectorAll('div[role=button][aria-label*="Challenge Image"]')]
                             .map(d => { const r = d.getBoundingClientRect();
                               return {x:r.x+r.width/2, y:r.y+r.height/2}; })""")
                    boxes = sorted(boxes, key=lambda q: (round(q["y"] / 20), q["x"]))
                    fb = None
                    for el in await page.query_selector_all("iframe"):
                        bxx = await el.bounding_box()
                        if bxx and bxx["width"] > 200 and bxx["height"] > 200:
                            fb = bxx
                            break
                    ox, oy = (fb["x"], fb["y"]) if fb else (0, 0)
                    for i2 in idx:
                        if 0 <= i2 < len(boxes):
                            await page.mouse.click(ox + boxes[i2]["x"], oy + boxes[i2]["y"])
                            await page.wait_for_timeout(430)
                    for lab in ("Verify", "Next", "Submit"):
                        try:
                            bt = fr.locator(f"xpath=//*[normalize-space(text())='{lab}']")
                            if await bt.count() > 0:
                                await bt.first.click(timeout=5000)
                                print("    提交:", lab)
                                break
                        except Exception:
                            pass
                    got = True
                    break
            await page.wait_for_timeout(5000)
            st = await page.evaluate("JSON.stringify(window.__state)")
            print("    状态:", st)
            if '"done"' in st:
                break
            if not got and rnd > 1:
                break

        await page.screenshot(path="/tmp/ha_final.png")
        await b.close()


if __name__ == "__main__":
    asyncio.run(main())
