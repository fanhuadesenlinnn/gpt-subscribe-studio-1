"""自定义九宫格求解：抓图 → LLM(Anthropic 中转) 判 → 点击 → 提交 → token。"""
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
from pydantic import BaseModel
from playwright.async_api import async_playwright

sys.path.insert(0, "/opt/gss/app/packages/core/src")
from gss_core.proxies import align_proxy_region, configured_proxy
from gss_core.stripe_protocol import PUBLISHABLE_KEY, StripeProtocol, V_HOSTED

from hcaptcha_challenger.tools.internal.providers.anthropic_compat import (
    AnthropicCompatProvider,
)

API = "http://127.0.0.1:18120"
STRIPE = "https://api.stripe.com"
ORIGIN = os.environ.get("GSS_PUBLIC_ORIGIN", "https://<PAY_HOST>")
CHROME = "/usr/bin/google-chrome"
# 中转地址 / key / 模型都从环境变量取，不写死在仓库里
LLM_BASE = os.environ.get("GSS_LLM_BASE_URL", "")
LLM_KEY = os.environ.get("GSS_LLM_KEY", "")
LLM_MODEL = os.environ.get("GSS_LLM_MODEL", "gemini-3.8-flash-high")

PAGE = """<!doctype html><html><head><meta charset="utf-8"></head>
<body style="margin:0;padding:20px;background:#fff">
<div id="hc"></div><pre id="out"></pre></body></html>"""


HARVEST = r"""(() => {
  if (window.__hcHooked) return 'already';
  window.__hcHooked = true; window.__drawn = [];
  const P = CanvasRenderingContext2D.prototype;
  const orig = P.drawImage;
  P.drawImage = function(src) {
    try {
      const ok = (src instanceof HTMLImageElement) || (src instanceof HTMLCanvasElement);
      if (ok && window.__drawn.length < 40) {
        const w = src.naturalWidth || src.width || 0;
        const h = src.naturalHeight || src.height || 0;
        if (w >= 60 && h >= 60) {
          const c = document.createElement('canvas');
          c.width = w; c.height = h;
          c.getContext('2d').drawImage(src, 0, 0);
          window.__drawn.push({ w: w, h: h, url: c.toDataURL('image/jpeg', 0.92) });
        }
      }
    } catch (e) {}
    return orig.apply(this, arguments);
  };
  return 'hooked';
})()"""


class GridAnswer(BaseModel):
    indices: list[int]
    reason: str


def load_env(path: str) -> dict:
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


async def main() -> None:
    auth = json.loads(Path("/opt/gss/config/container-auth.json").read_text())
    env = load_env("/opt/gss/app/cards.env")
    ats = json.load(open("/opt/gss/data/at_list.json"))

    oid = d = None
    for i in range(3):
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
                oid = r.json()["id"]
                d = c.get("/api/orders/" + oid, headers=ck).json()
                break
            await asyncio.sleep(3)
    if not oid:
        print("建单失败")
        return

    billing = {"name": "FIXTURE HOLDER", "email": d.get("session_email") or "b@example.com",
               "line1": "1 Fixture St", "city": "Portland", "state": "OR",
               "postal_code": "97211", "country": "US"}
    card = {"number": env["CARD_A_NUMBER"], "exp_month": env["CARD_A_EXP_MONTH"],
            "exp_year": env["CARD_A_EXP_YEAR"], "cvc": env["CARD_A_CVC"]}
    proxy = align_proxy_region(configured_proxy("checkout"), "US")
    proxy = re.sub(r"sid-[A-Za-z0-9]+", "sid-" + secrets.token_hex(4), proxy or "") or None
    print("订单:", oid[:12])

    sp = StripeProtocol(proxy=proxy, captcha_key="", captcha_url="",
                        card=card, billing=billing)
    try:
        out = sp.pay_from_url(d.get("checkout_url") or "")
    finally:
        sp.close()
    body = ((getattr(out, "raw", {}) or {}).get("confirm") or {}).get("body") or {}
    na = body.get("next_action") or (body.get("payment_intent") or {}).get("next_action") or {}
    sdk = na.get("use_stripe_sdk") or {}
    sj = sdk.get("stripe_js") or {}
    if sdk.get("type") != "intent_confirmation_challenge":
        print("未命中挑战:", out.state)
        return
    pi = body.get("payment_intent") or {}
    sitekey, rqdata = sj.get("site_key"), sj.get("rqdata")
    verify_url = sj.get("verification_url")
    secret = pi.get("client_secret") or sdk.get("client_secret") or ""
    print("挑战下发")

    llm = AnthropicCompatProvider(api_key=LLM_KEY, model=LLM_MODEL, base_url=LLM_BASE)
    token = ""
    tries = 0

    async with async_playwright() as p:
        browser = await p.chromium.launch(executable_path=CHROME, headless=False,
                                          args=["--no-sandbox", "--disable-dev-shm-usage",
                                                "--window-size=1280,900"])
        ctx = await browser.new_context(proxy=proxy_cfg(proxy),
                                        viewport={"width": 1280, "height": 900})
        page = await ctx.new_page()
        await page.add_init_script(HARVEST)
        ok = False
        for attempt in range(4):
            try:
                await page.goto("https://example.com", wait_until="domcontentloaded", timeout=45000)
                await page.set_content(PAGE, wait_until="load")
                ok = True
                break
            except Exception as exc:
                print(f"   页面加载失败({attempt+1}/4): {str(exc)[:70]}")
                await page.wait_for_timeout(3000)
        if not ok:
            print("   连不上，代理不可用")
            return
        for _ in range(120):
            if await page.evaluate("typeof window.hcaptcha !== 'undefined'"):
                break
            await page.wait_for_timeout(200)
        await page.add_script_tag(
            url="https://js.hcaptcha.com/1/api.js?render=explicit&enterprise=1")
        await page.wait_for_timeout(1500)
        await page.evaluate(
            """([sk, rq]) => {
                 window.__token = '';
                 window.__wid = hcaptcha.render('hc', {
                   sitekey: sk, size: 'normal', host: 'b.stripecdn.com',
                   rqdata: rq || undefined,
                   callback: (t) => { window.__token = t || ''; },
                 });
                 return window.__wid;
               }""", [sitekey, rqdata])
        await page.wait_for_timeout(2500)

        async def challenge_frame():
            for fr in page.frames:
                if "frame=challenge" in (fr.url or ""):
                    try:
                        if await fr.locator("div[role=button]").count() > 0:
                            return fr
                    except Exception:
                        pass
            return None

        for tries in range(1, 4):
            print(f"\n── 轮次 {tries} ──")
            if tries == 1:
                # 先点 checkbox
                try:
                    await page.frame_locator(
                        'iframe[src*="frame=checkbox"]').locator("#checkbox").click(timeout=8000)
                    print("   已点 checkbox")
                except Exception as exc:
                    print("   checkbox:", str(exc)[:100])
            await page.wait_for_timeout(3500)

            token = await page.evaluate("window.__token || ''")
            if token:
                print("   直接通过")
                break

            fr = await challenge_frame()
            if not fr:
                print("   无挑战框，刷新重试")
                await page.wait_for_timeout(2000)
                continue

            # 抓题面 + 示例图 + 9 格图
            data = await fr.evaluate(
                """async () => {
                     const prompt = (document.querySelector('.prompt-text') || {}).innerText
                       || (document.querySelector('div[class*=prompt]') || {}).innerText || '';
                     const imgs = [...document.querySelectorAll('img')].map(i => i.src);
                     const anchors = imgs.filter(s => /imgs\\d*\\.hcaptcha\\.com/.test(s));
                     const cells = [...document.querySelectorAll('div[role=button]')]
                       .filter(d => /challenge image/i.test(d.getAttribute('aria-label') || ''));
                     const tiles = [];
                     for (const c of cells) {
                       const im = c.querySelector('img');
                       if (im && im.src) { tiles.push(im.src); continue; }
                       const bg = getComputedStyle(c).backgroundImage || '';
                       const m = bg.match(/url\\("?([^")]+)"?\\)/);
                       tiles.push(m ? m[1] : '');
                     }
                     return { prompt, anchors, tiles, n: cells.length };
                   }""")
            print("   题面:", str(data.get("prompt"))[:90].replace("\n", " "))
            print("   格子:", data.get("n"), " 示例:", len(data.get("anchors") or []))

            # 优先用 drawImage 拦到的原图
            drawn = await fr.evaluate("window.__drawn || []")
            print("   拦到原图:", len(drawn), [(d0.get("w"), d0.get("h")) for d0 in drawn][:6])
            tiles, anchors = [], []
            if drawn:
                from PIL import Image as _I
                files = []
                for i, d0 in enumerate(drawn):
                    fp = Path(f"/tmp/hc_drawn_{i}.jpg")
                    fp.parent.mkdir(exist_ok=True)
                    try:
                        fp.write_bytes(base64.b64decode(str(d0.get("url")).split(",")[-1]))
                        files.append(fp)
                    except Exception:
                        pass
                big = [f for f in files if _I.open(f).size[0] >= 100]
                if len(big) >= 9:
                    tiles = big[:9]
                elif files:
                    im0 = _I.open(files[0]).convert("RGB")
                    W0, H0 = im0.size
                    tw, th = W0 // 3, H0 // 3
                    for r0 in range(3):
                        for c0 in range(3):
                            tp = Path(f"/tmp/hc_tile_{r0*3+c0}.jpg")
                            im0.crop((c0*tw, r0*th, (c0+1)*tw, (r0+1)*th)).save(tp, quality=92)
                            tiles.append(tp)
            if len(tiles) < 9:
                # 回退：元素截图
                cells = fr.locator('div[role=button][aria-label*="Challenge Image"]')
            n = await cells.count()
            tiles = []
            for i in range(min(n, 9)):
                fp = Path(f"/tmp/g_t{i}.png")
                try:
                    await cells.nth(i).screenshot(path=str(fp), timeout=6000)
                    tiles.append(fp)
                except Exception as exc:
                    print("     截图失败", i, str(exc)[:60])
                    tiles.append(None)
            anchors = []
            try:
                aimg = fr.locator("img[src*='imgs'][src*='hcaptcha.com']")
                if await aimg.count() > 0:
                    fp = Path("/tmp/g_anchor.png")
                    await aimg.first.screenshot(path=str(fp), timeout=6000)
                    anchors.append(fp)
            except Exception:
                pass
            print("   抓到格子图:", sum(1 for t in tiles if t), " 示例:", len(anchors))
            if not any(tiles):
                continue

            prompt = str(data.get("prompt") or "").split("Please select")[0].strip()
            ans = await llm.generate_with_images(
                images=[a for a in anchors if a] + [t for t in tiles if t],
                response_schema=GridAnswer,
                user_prompt=(
                    f"题目：{prompt}\n"
                    "第一组图是题目给出的示例（可能没有），后面依次是 9 个格子（索引 0-8，"
                    "按从左到右、从上到下）。请选出所有符合题意的格子索引。"
                ),
                description="你是精确的图像选择题作答助手，只输出 JSON。",
            )
            print("   LLM:", ans.indices, ans.reason[:80])

            # 点击
            for idx in ans.indices:
                try:
                    await fr.locator("div[role=button]").filter(
                        has_text="").nth(idx).click(timeout=4000)
                except Exception:
                    try:
                        cell = fr.locator("div[role=button]").nth(idx)
                        await cell.click(timeout=4000)
                    except Exception as exc:
                        print("     点格子失败", idx, str(exc)[:60])
                await page.wait_for_timeout(500)
            await page.wait_for_timeout(1200)

            # 提交
            for label in ("Next", "Verify", "Submit"):
                try:
                    btn = fr.locator(f"//div[normalize-space(.)='{label}']")
                    if await btn.count() > 0:
                        await btn.first.click(timeout=4000)
                        print("   提交:", label)
                        break
                except Exception:
                    pass
            await page.wait_for_timeout(4000)
            token = await page.evaluate("window.__token || ''")
            if token:
                print("   拿到 token", len(token))
                break
            print("   未通过，继续")

        await page.screenshot(path=f"/tmp/solve_shot_{tries}.png")
        await browser.close()

    if not token:
        print("\n最终未拿到 token")
        return
    Path("/tmp/solve_token.txt").write_text(token)

    print("\nverify_challenge")
    with httpx.Client(timeout=90, proxy=proxy, verify=False) as c:
        r = c.post(f"{STRIPE}{verify_url}", data={
            "client_secret": secret, "challenge_response_token": token,
            "captcha_vendor_name": "hcaptcha", "key": PUBLISHABLE_KEY,
            "_stripe_version": V_HOSTED})
        print(" HTTP", r.status_code)
        try:
            j = r.json()
            e2 = j.get("error") or (j.get("payment_intent") or {}).get("last_payment_error") or {}
            print(" error:", e2.get("code"), str(e2.get("message"))[:120])
            print(" pi:", (j.get("payment_intent") or {}).get("status"))
        except Exception:
            print(" ", r.text[:300])


if __name__ == "__main__":
    asyncio.run(main())
