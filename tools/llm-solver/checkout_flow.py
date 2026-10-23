"""真实 Stripe 结账页全流程：打开 → 填卡 → 提交 → 挑战 → 服务商判题 → 读结果。"""
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

API = "http://127.0.0.1:18120"
ORIGIN = os.environ.get("GSS_PUBLIC_ORIGIN", "https://<PAY_HOST>")
CHROME = "/usr/bin/google-chrome"
CAPTCHA_KEY = os.environ.get("GSS_CAPTCHA_API_KEY", "")


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
    c = {"server": f"{u.scheme or 'http'}://{u.hostname}:{u.port or 3010}"}
    if u.username:
        c["username"] = unquote(u.username)
        c["password"] = unquote(u.password or "")
    return c


async def solve_hcaptcha(page, tag=""):
    """canvas 版：元素截图（不受 CORS 影响）→ 打码服务 → 点击 → Verify。"""
    for rnd in range(8):
        hit = False
        for fr in [f for f in page.frames if "hcaptcha" in (f.url or "")]:
            cv = None
            try:
                # 优先 canvas；没有则退到挑战区里的 <img>
                el = await fr.query_selector("canvas")
                if el is None:
                    imgs = await fr.query_selector_all("img")
                    best = None
                    for im in imgs:
                        bb0 = await im.bounding_box()
                        if bb0 and bb0["width"] > 120 and bb0["height"] > 80:
                            if best is None or bb0["width"] * bb0["height"] > best[1]:
                                best = (im, bb0["width"] * bb0["height"])
                    if best is not None:
                        el = best[0]
                if el is not None:
                    bb = await el.bounding_box()
                    if bb and bb["width"] > 120 and bb["height"] > 120:
                        fp = Path("/tmp/co_cv.png")
                        await el.screenshot(path=str(fp))
                        cv = {"W": cv_w(bb), "H": cv_h(bb),
                              "rect": {"x": bb["x"], "y": bb["y"],
                                       "w": bb["width"], "h": bb["height"]},
                              "path": str(fp)}
            except Exception:
                cv = None

            if cv is None and rnd > 0:
                # 后续轮次：只等待题目图渲染，不重复点方框
                await page.wait_for_timeout(4000)
                continue
            if cv is None:
                clicked = False
                for sel in ('#checkbox', '[role="checkbox"]', 'div.checkbox'):
                    try:
                        cb = fr.locator(sel)
                        if await cb.count() > 0:
                            await cb.first.click(timeout=6000)
                            clicked = True
                            break
                    except Exception:
                        pass
                if not clicked:
                    # 真实指针序列：逐步移动 + 按下/抬起（合成 click 容易被判可疑）
                    try:
                        tx, ty = 513, 437
                        await page.mouse.move(400, 300)
                        await page.wait_for_timeout(120)
                        for k in range(1, 9):
                            await page.mouse.move(400 + (tx - 400) * k / 8,
                                                  300 + (ty - 300) * k / 8)
                            await page.wait_for_timeout(35)
                        await page.wait_for_timeout(150)
                        await page.mouse.down()
                        await page.wait_for_timeout(90)
                        await page.mouse.up()
                        clicked = True
                    except Exception:
                        pass
                print(f"   {tag}点 I am human -> {clicked}", flush=True)
                await page.wait_for_timeout(9000)
                continue

            print(f"   {tag}canvas {cv['W']}x{cv['H']} 已截图", flush=True)
            prompt = ""
            try:
                prompt = await fr.evaluate(
                    """() => { const e = document.querySelector('.prompt-text')
                         || document.querySelector('div[class*=prompt]');
                         return e ? e.innerText : ''; }""")
            except Exception:
                pass
            prompt = " ".join(str(prompt).split())[:150]

            task = {"type": "HCaptchaClassification",
                    "queries": [base64.b64encode(Path(cv["path"]).read_bytes()).decode()]}
            if prompt:
                task["question"] = prompt
            try:
                async with httpx.AsyncClient(timeout=90) as c:
                    r = await c.post("https://api.yescaptcha.com/createTask",
                                     json={"clientKey": CAPTCHA_KEY, "task": task})
                    j = r.json() or {}
            except Exception as exc:
                print(f"   {tag}服务商异常:", str(exc)[:60], flush=True)
                continue
            sol = j.get("solution") or {}
            box = []
            for v in (sol.get("box") or []):
                try:
                    box.append(float(v))
                except Exception:
                    pass
            objs = sol.get("objects")
            print(f"   {tag}题面={prompt[:40]} box点数={len(box)//2} objs={objs}", flush=True)

            rect = cv["rect"]
            pts = []
            if len(box) >= 2:
                for i in range(0, len(box) - 1, 2):
                    pts.append((box[i], box[i + 1]))
            elif objs:
                for i, v in enumerate(objs):
                    if v:
                        pts.append(((i % 3 + 0.5) * cv["W"] / 3,
                                    (i // 3 + 0.5) * cv["H"] / 3))
            if not pts:
                # 拖拽题/无法判题的题型 → Skip 换一道
                skipped = False
                for sel in ('div.button-submit:has-text("Skip")',
                            'div.button-submit:has-text("Next")',
                            'text=/Skip Challenge/',
                            'xpath=//div[contains(@class,"button-submit")]'):
                    try:
                        bt = fr.locator(sel)
                        if await bt.count() > 0:
                            txt = (await bt.first.inner_text(timeout=2000)) or ""
                            if "Skip" in txt or "Next" in txt:
                                await bt.first.click(timeout=5000)
                                skipped = True
                                print(f"   {tag}已点 [{txt[:20]}] 换题", flush=True)
                                break
                    except Exception:
                        pass
                await page.wait_for_timeout(5000)
                if skipped:
                    continue

            for bx, by in pts[:6]:
                px = rect["x"] + (bx / cv["W"]) * rect["w"]
                py = rect["y"] + (by / cv["H"]) * rect["h"]
                try:
                    await page.mouse.click(px, py)
                except Exception:
                    pass
                await page.wait_for_timeout(430)
            await page.wait_for_timeout(700)
            for sel in ('div.button-submit:has-text("Verify")',
                        'div.button-submit:has-text("Next")',
                        'div.button-submit:has-text("Submit")'):
                try:
                    bt = fr.locator(sel)
                    if await bt.count() > 0:
                        txt2 = (await bt.first.inner_text(timeout=2000)) or ""
                        await bt.first.click(timeout=5000)
                        print(f"   {tag}提交 [{txt2[:24]}]", flush=True)
                        hit = True
                        break
                except Exception:
                    pass
            if hit:
                break
        await page.wait_for_timeout(5000)
        if hit:
            return True
    return False


def cv_w(bb):
    return max(1, round(bb["width"]))


def cv_h(bb):
    return max(1, round(bb["height"]))


async def all_frames(page):
    """递归收集所有 frame（含嵌套）。"""
    out, seen = [], set()
    def walk(fr, depth=0):
        if id(fr) in seen or depth > 4:
            return
        seen.add(id(fr))
        out.append(fr)
        try:
            for ch in fr.child_frames:
                walk(ch, depth + 1)
        except Exception:
            pass
    walk(page.main_frame)
    return out


async def fill_stripe_fields(page, card, email="buyer@example.com"):
    """按清单顺序填：email → ToS → phone → 卡（递归 frames）。"""
    # 等页面加载稳定：frame 数达到 12+ 再开始填，避免字段还没渲染
    for _w in range(30):
        frames = await all_frames(page)
        if len(frames) >= 12:
            break
        await page.wait_for_timeout(1000)
    print("填表前 frame 数:", len(frames), flush=True)
    filled = []
    frames = await all_frames(page)
    print("frame 数(递归):", len(frames), flush=True)

    async def try_fill(sels, val, label):
        for sel in sels:
            for fr in frames:
                try:
                    loc = fr.locator(sel)
                    if await loc.count() > 0:
                        await loc.first.fill(val, timeout=4000)
                        filled.append(label)
                        return True
                except Exception:
                    pass
        return False

    # 1) email（必填）
    await try_fill(['#email', 'input[name="email"]', 'input[type="email"]'], email, "email")
    # 2) phone（可选）
    await try_fill(['#phoneNumber', 'input[name="phoneNumber"]'], "5035551234", "phone")
    # 3) ToS 勾选
    for fr in frames:
        try:
            ck = fr.locator('#termsOfServiceConsentCheckbox, input[type="checkbox"]')
            if await ck.count() > 0:
                await ck.first.check(timeout=4000)
                filled.append("tos")
                break
        except Exception:
            pass
    # 4) 选中 Card 单选（支付方式未选时卡字段不会渲染）
    picked = False
    try:
        rd = page.get_by_role("radio", name="Card")
        if await rd.count() > 0:
            await rd.first.check(force=True, timeout=5000)
            picked = True
            print("   已选中 Card (role=radio)", flush=True)
    except Exception as exc:
        print("   role=radio 失败:", str(exc)[:60], flush=True)
    if not picked:
        # 圆圈在 "Card" 文字左侧约 42px；用文字 bbox 反推，失败则用截图坐标兜底
        clicked = False
        for fr in frames:
            try:
                loc = fr.locator('text=Card').first
                bb = await loc.bounding_box()
                if bb and bb["width"] < 200:
                    await page.mouse.click(bb["x"] - 42, bb["y"] + bb["height"] / 2)
                    clicked = True
                    print("   点击圆圈(相对文字):", round(bb["x"] - 42), round(bb["y"]), flush=True)
                    break
            except Exception:
                pass
        if not clicked:
            try:
                await page.mouse.click(736, 234)
                clicked = True
                print("   点击圆圈(截图坐标 736,234)", flush=True)
            except Exception:
                pass
        picked = clicked
    await page.wait_for_timeout(4000)
    frames = await all_frames(page)
    print("点开后 frame 数:", len(frames), flush=True)

    # 5) 卡
    exp = f'{card["exp_month"].zfill(2)}/{card["exp_year"][-2:]}'
    for sels, val, lab in (
        (['input[name="number"]', 'input[autocomplete="cc-number"]'], card["number"], "number"),
        (['input[name="expiry"]', 'input[autocomplete="cc-exp"]'], exp, "expiry"),
        (['input[name="cvc"]', 'input[autocomplete="cc-csc"]'], card["cvc"], "cvc"),
        (['input[name="name"]', 'input[autocomplete="cc-name"]'], "FIXTURE HOLDER", "name"),
        (['input[name="address1"]', 'input[autocomplete="address-line1"]',
          'input[placeholder*="Address" i]'], "1 Fixture St", "address1"),
        (['input[name="city"]', 'input[autocomplete="address-level2"]',
          'input[placeholder="City"]', 'input[placeholder*="City" i]'], "Portland", "city"),
        (['input[name="postal"]', 'input[autocomplete="postal-code"]',
          'input[placeholder="ZIP"]', 'input[placeholder*="ZIP" i]',
          'input[placeholder*="Postal" i]'], "97211", "postal"),
    ):
        ok = await try_fill(sels, val, lab)
        if lab == "city":
            for fr in frames:
                try:
                    has_card = await fr.locator('input[name="number"], input[autocomplete="cc-number"]').count()
                except Exception:
                    has_card = 0
                if has_card == 0:
                    continue
                try:
                    n_sel = await fr.locator("select").count()
                except Exception:
                    n_sel = 0
                best, best_n = -1, 0
                for i in range(n_sel):
                    try:
                        cnt = await fr.locator("select").nth(i).locator("option").count()
                    except Exception:
                        continue
                    if cnt > best_n:
                        best, best_n = i, cnt
                if best >= 0 and best_n > 100:
                    try:
                        await fr.locator("select").nth(best).select_option("US", timeout=4000)
                        print("   国家已选 US（select#" + str(best) + "，" + str(best_n) + " 选项）", flush=True)
                        await page.wait_for_timeout(2000)
                    except Exception as exc:
                        print("   国家选择失败:", str(exc)[:60], flush=True)
                for i in range(n_sel):
                    try:
                        el = fr.locator("select").nth(i)
                        opts = await el.locator("option").all_inner_texts()
                        if any((o or "").strip() == "Oregon" for o in opts):
                            await el.select_option(label="Oregon", timeout=3000)
                            print("   州已选 Oregon", flush=True)
                            break
                    except Exception:
                        pass
                break
            frames = await all_frames(page)
            ok = await try_fill(sels, val, lab)
    return filled


async def main():
    auth = json.loads(Path("/opt/gss/config/container-auth.json").read_text())
    env = load_env("/opt/gss/app/cards.env")
    ats = json.load(open("/opt/gss/data/at_list.json"))
    card = {"number": env["CARD_A_NUMBER"], "exp_month": env["CARD_A_EXP_MONTH"],
            "exp_year": env["CARD_A_EXP_YEAR"], "cvc": env["CARD_A_CVC"]}

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
        print("建单失败", flush=True)
        return
    url = d.get("checkout_url") or ""
    print("结账页:", url[:70], flush=True)

    proxy = align_proxy_region(configured_proxy("checkout"), "US")
    proxy = re.sub(r"sid-[A-Za-z0-9]+", "sid-" + secrets.token_hex(4), proxy or "") or None

    from playwright.async_api import async_playwright
    async with async_playwright() as p:
        b = await p.chromium.launch(executable_path=CHROME, headless=False,
                                    args=["--no-sandbox", "--disable-dev-shm-usage",
                                          "--window-size=1280,980"])
        ctx = await b.new_context(proxy=proxy_cfg(proxy), viewport={"width": 1280, "height": 980})
        page = await ctx.new_page()
        ok = False
        for att in range(4):
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=45000)
                ok = True
                break
            except Exception as e:
                print(f"导航重试{att+1}: {str(e)[:60]}", flush=True)
                await page.wait_for_timeout(3000)
        print("导航成功:", ok, flush=True)
        await page.wait_for_timeout(9000)

        filled = await fill_stripe_fields(page, card, d.get("session_email") or "buyer@example.com")
        print("已填字段:", filled, flush=True)
        await page.wait_for_timeout(1500)

        # 尝试点提交按钮
        clicked = ""
        for sel in ("button[type=submit]", "text=Subscribe", "text=Pay", "text=Start trial"):
            try:
                loc = page.locator(sel)
                if await loc.count() > 0:
                    await loc.first.click(timeout=8000)
                    clicked = sel
                    break
            except Exception:
                pass
        print("点击提交:", clicked, flush=True)

        for _w in range(20):
            if any("hcaptcha" in (f.url or "") for f in page.frames):
                print("挑战帧已出现", flush=True)
                break
            await page.wait_for_timeout(1500)

        for rnd in range(3):
            await page.wait_for_timeout(6000)
            did = await solve_hcaptcha(page, tag=f"[{rnd}]")
            print(f"[{rnd}] 挑战处理: {did}", flush=True)
            if not did:
                break

        await page.screenshot(path="/tmp/co_after_submit.png")
        await page.wait_for_timeout(9000)
        try:
            print("最终正文:", (await page.inner_text("body"))[:400].replace("\n", " "), flush=True)
        except Exception:
            pass
        await page.screenshot(path="/tmp/co_final.png")
        await b.close()


if __name__ == "__main__":
    asyncio.run(main())
