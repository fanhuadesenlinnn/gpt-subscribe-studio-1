"""真实 Stripe 结账页全流程：打开 → 填卡 → 提交 → 挑战 → 服务商判题 → 读结果。"""
import asyncio
import base64
import json
import os
import random
import re
import secrets
import sys
from pathlib import Path
from urllib.parse import unquote, urlsplit

import httpx

sys.path.insert(0, "/opt/gss/app/packages/core/src")
from gss_core.proxies import align_proxy_region, configured_proxy

API = "http://127.0.0.1:18120"
CAPTURED = []      # 抓到的 hCaptcha token / verify_challenge 请求体
API_REQS = []      # 所有 api.stripe.com POST 路径
RESP_CAPTURED = [] # 支付相关响应
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



def make_grid(png: str, w: int, h: int) -> str:
    """用 challenger 的 create_coordinate_grid 生成带坐标轴的网格图。"""
    try:
        import cv2
        from hcaptcha_challenger.helper import create_coordinate_grid
        arr = create_coordinate_grid(png, (0, 0, float(w), float(h)))
        out = "/tmp/co_grid.png"
        cv2.imwrite(out, arr)
        return out
    except Exception as exc:
        print("   网格生成失败:", str(exc)[:80], flush=True)
        return ""


async def reason_drag(canvas_png: str, grid_png: str, prompt: str):
    """调 challenger 的 SpatialPathReasoner 解拖拽题。"""
    from hcaptcha_challenger.tools.spatial.path import SpatialPathReasoner
    r = SpatialPathReasoner(gemini_api_key="x")
    out = await r(challenge_screenshot=canvas_png, grid_divisions=grid_png,
                  auxiliary_information=prompt)
    return out




async def challenge_open(page):
    """挑战是否还开着。注意：弹窗和题目都在 iframe 里，必须遍历 frame。"""
    keys = ("one more step", "select the checkbox", "i am human",
            "verify you are human")
    for fr in page.frames:
        try:
            t = (await fr.evaluate(
                "() => document.body ? document.body.innerText : ''") or "").lower()
        except Exception:
            continue
        if any(k in t for k in keys):
            return True
        # 判据：题面元素可见（最可靠，只有真出题时才有），或 canvas 尺寸够大。
        # 不能用 div[role=button] 计数 —— Skip/Refresh/菜单也是这类元素且常驻可见。
        try:
            _pe = fr.locator('.prompt-text, div[class*="prompt"]')
            if await _pe.count() > 0 and await _pe.first.is_visible():
                return True
        except Exception:
            pass
        try:
            _cv = await fr.query_selector("canvas")
            if _cv is not None:
                _cb = await _cv.bounding_box()
                if _cb and _cb["width"] > 200 and _cb["height"] > 150:
                    return True
        except Exception:
            pass
    return False


async def page_state(page):
    """返回页面推进状态：processing / done / error / none。"""
    try:
        body = await page.inner_text("body")
    except Exception:
        return "none"
    b = (body or "").lower()
    # 挑战窗还开着 → 一定还没通过（"Processing" 只是按钮文案，不能当成功信号）
    if "one more step" in b or "select the checkbox" in b or "i am human" in b:
        return "none"
    for k in ("thank you", "success", "you're all set", "subscription confirmed"):
        if k in b:
            return "done"
    for k in ("declined", "your card was declined", "card was declined",
              "could not be charged", "payment failed"):
        if k in b:
            return "declined"
    for k in ("verifying payment", "authenticating"):
        if k in b:
            return "processing"
    for k in ("something went wrong", "try again"):
        if k in b:
            return "error"
    return "none"


async def solve_hcaptcha(page, tag=""):
    """canvas 版：元素截图（不受 CORS 影响）→ 打码服务 → 点击 → Verify。"""
    clicked_once = False
    submitted_any = False
    for rnd in range(14):
        hit = False
        cands = [f for f in page.frames if "hcaptcha" in (f.url or "")]
        if not cands:
            # Skip 后 hCaptcha 会卸载重建 iframe，这个窗口里帧列表为空
            await page.wait_for_timeout(3000)
            continue
        for fr in cands:
            # ── DOM 九宫格题（无 canvas，9 个 role=button 图块）──
            try:
                tiles_loc = fr.locator('div[role="button"]')
                n_tiles = await tiles_loc.count()
            except Exception:
                n_tiles = 0
            if n_tiles >= 9:
                print(f"   {tag}检测到 DOM 九宫格（{n_tiles} 块）", flush=True)
                prompt = ""
                try:
                    prompt = await fr.evaluate(
                        """() => { const e = document.querySelector('.prompt-text')
                             || document.querySelector('div[class*=prompt]');
                             return e ? e.innerText : ''; }""")
                except Exception:
                    pass
                prompt = " ".join(str(prompt).split())[:150]
                tiles = []
                for _i in range(9):
                    _fp = Path(f"/tmp/grid_{_i}.png")
                    try:
                        await tiles_loc.nth(_i).screenshot(path=str(_fp), timeout=6000)
                        tiles.append(_fp)
                    except Exception:
                        tiles.append(None)

                # 整张九宫格一张图（给 LLM Classifier 用）
                _grid_png = ""
                try:
                    _fe2 = await fr.frame_element()
                    _bb2 = await _fe2.bounding_box()
                    if _bb2:
                        _grid_png = "/tmp/grid_full.png"
                        await page.screenshot(path=_grid_png, clip=_bb2)
                except Exception as _e:
                    print(f"   {tag}整图截图失败:", str(_e)[:50], flush=True)
                if any(tiles):
                    task = {"type": "HCaptchaClassification",
                            "queries": [base64.b64encode(Path(t).read_bytes()).decode()
                                        for t in tiles if t]}
                    if prompt:
                        task["question"] = prompt
                    try:
                        async with httpx.AsyncClient(timeout=90) as _c:
                            _r = await _c.post("https://api.yescaptcha.com/createTask",
                                               json={"clientKey": CAPTCHA_KEY, "task": task})
                            _j = _r.json() or {}
                    except Exception as _e:
                        _j = {}
                        print(f"   {tag}九宫格判题异常:", str(_e)[:60], flush=True)
                    _sol = _j.get("solution") or {}
                    idxs = [i for i, v in enumerate(_sol.get("objects") or []) if v]

                    # 优先用 LLM Classifier（服务商判错时靠它兜底）
                    if _grid_png:
                        try:
                            from hcaptcha_challenger.tools.image_classifier import (
                                Classifier as _Clf)
                            _clf = _Clf(gemini_api_key="x")
                            _ans = await _clf(challenge_screenshot=Path(_grid_png))
                            _d2 = _ans.model_dump()
                            print(f"   {tag}LLM 原始返回: {json.dumps(_d2, ensure_ascii=False)[:200]}",
                                  flush=True)
                            _coords = (_d2.get("coordinates")
                                       or _d2.get("selected_coordinates")
                                       or _d2.get("cells") or [])
                            _li = []
                            for _c in _coords:
                                try:
                                    if isinstance(_c, (list, tuple)) and len(_c) >= 2:
                                        _li.append(int(_c[0]) * 3 + int(_c[1]))
                                    elif isinstance(_c, dict):
                                        _li.append(int(_c.get("row", 0)) * 3
                                                   + int(_c.get("col", 0)))
                                except Exception:
                                    pass
                            if _li:
                                print(f"   {tag}改用 LLM 结果 {_li}（服务商给的是 {idxs}）",
                                      flush=True)
                                idxs = _li
                        except Exception as _e3:
                            print(f"   {tag}LLM 判题失败:", str(_e3)[:70], flush=True)
                    print(f"   {tag}九宫格题面={prompt[:45]} 选={idxs}", flush=True)
                    boxes = await fr.evaluate(
                        """() => [...document.querySelectorAll('div[role="button"]')]
                             .map(d => { const r = d.getBoundingClientRect();
                               return {x: r.x + r.width/2, y: r.y + r.height/2}; })""")
                    boxes = sorted(boxes, key=lambda b: (round(b["y"] / 20), b["x"]))[:9]
                    # getBoundingClientRect 是 iframe 内坐标，点击要用页面坐标 → 加 iframe 偏移
                    _ox = _oy = 0
                    try:
                        _fe = await fr.frame_element()
                        _fbb = await _fe.bounding_box()
                        if _fbb:
                            _ox, _oy = _fbb["x"], _fbb["y"]
                    except Exception:
                        pass
                    print(f"   {tag}iframe 偏移 ({round(_ox)},{round(_oy)})", flush=True)
                    for _ix in idxs:
                        if 0 <= _ix < len(boxes):
                            await page.mouse.click(_ox + boxes[_ix]["x"], _oy + boxes[_ix]["y"])
                            await page.wait_for_timeout(420)
                    await page.wait_for_timeout(800)
                    for _lab in ("Verify", "Next", "Submit"):
                        try:
                            _bt = fr.locator(f"div.button-submit:has-text('{_lab}')")
                            if await _bt.count() > 0:
                                await _bt.first.click(timeout=6000)
                                print(f"   {tag}九宫格提交 {_lab}", flush=True)
                                break
                        except Exception:
                            pass
                    # 提交后立刻记录状态：看是出了第二页、提示答错、还是已在加载
                    for _w2 in range(10):
                        await page.wait_for_timeout(3000)
                        if any("confirm" in r for r in API_REQS) or CAPTURED:
                            print(f"   {tag}★ 提交后已触发 confirm/token", flush=True)
                            break
                    try:
                        await page.screenshot(path=f"/tmp/grid_after_{rnd}.png")
                        _bt2 = [f.url[:45] for f in page.frames if "hcaptcha" in (f.url or "")]
                        print(f"   {tag}提交后 hcaptcha 帧={len(_bt2)} "
                              f"截图 /tmp/grid_after_{rnd}.png", flush=True)
                    except Exception:
                        pass
                    hit = True
                    submitted_any = True
                    break

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
                st2 = await page_state(page)
                if st2 != "none":
                    print(f"   {tag}★ 页面已推进: {st2}", flush=True)
                    return True
                # 后续轮次：轮询等新题目图渲染（每页都要等，约 60 秒）
                for _w in range(30):
                    try:
                        el2 = await fr.query_selector("canvas")
                        if el2 is not None:
                            bb2 = await el2.bounding_box()
                            if bb2 and bb2["width"] > 120 and bb2["height"] > 120:
                                fp2 = Path("/tmp/co_cv.png")
                                await el2.screenshot(path=str(fp2))
                                cv = {"W": max(1, round(bb2["width"])),
                                      "H": max(1, round(bb2["height"])),
                                      "rect": {"x": bb2["x"], "y": bb2["y"],
                                               "w": bb2["width"], "h": bb2["height"]},
                                      "path": str(fp2)}
                                break
                    except Exception:
                        pass
                    await page.wait_for_timeout(2000)
                    if _w and _w % 10 == 0:
                        print(f"   {tag}等第二页… {_w*2}s", flush=True)
                # 轮询失败不再 continue —— 落到下面的点击分支，允许重置后再点方框
            if cv is None:
                if clicked_once or submitted_any:
                    # 弹窗还在就继续等；弹窗消失即视为已通过
                    if not await challenge_open(page):
                        print(f"   {tag}★ 弹窗已消失 → 视为通过", flush=True)
                        return True
                    await page.wait_for_timeout(3000)
                    continue
                clicked_once = True
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
                    # 首选：在 hcaptcha-inner 全屏 frame 内用元素定位点方框
                    inner = [f for f in page.frames if "hcaptcha-inner" in (f.url or "")]
                    for ifr in inner:
                        for sel0 in ('#checkbox', '[role="checkbox"]',
                                     'div.checkbox', '.checkbox'):
                            try:
                                cb0 = ifr.locator(sel0)
                                if await cb0.count() > 0:
                                    await cb0.first.click(timeout=6000)
                                    clicked = True
                                    print(f"   {tag}已点 inner 内的 {sel0}", flush=True)
                                    break
                            except Exception:
                                pass
                        if clicked:
                            break
                    try:
                        # 从 checkbox iframe 的真实位置算中心点（硬编码坐标会因页面滚动而落空）
                        tx, ty = 513, 437
                        if not clicked:
                            print(f"   {tag}=== page.frames ({len(page.frames)}) ===", flush=True)
                            for i3, fr3 in enumerate(page.frames):
                                print(f"      [{i3}] {(fr3.url or 'about:blank')[:95]}", flush=True)
                        for el2 in await page.query_selector_all("iframe"):
                            src2 = (await el2.get_attribute("src")) or ""
                            if "frame=checkbox" in src2:
                                bb2 = await el2.bounding_box()
                                if bb2:
                                    tx = bb2["x"] + bb2["width"] / 2
                                    ty = bb2["y"] + bb2["height"] / 2
                                    print(f"   {tag}checkbox 位置 ({round(tx)},{round(ty)})", flush=True)
                                break
                        await page.mouse.move(400, 300)
                        await page.wait_for_timeout(120)
                        for k in range(1, 9) if not clicked else range(0):
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
                print(f"   {tag}点 I am human -> {clicked}，等待 60 秒", flush=True)

                # 静默通过检测：点完没出题、弹窗也消失 → 已经过了，不需要重试
                for _q in range(6):
                    await page.wait_for_timeout(2000)
                    if not await challenge_open(page):
                        st_q = await page_state(page)
                        print(f"   {tag}★★★ 无题 = 已通过（弹窗已消失）state={st_q}", flush=True)
                        # 等 confirm 发出；若只创建了 pm 没有 confirm，再点一次提交
                        for _tk in range(8):
                            await page.wait_for_timeout(2000)
                            if any("confirm" in r for r in API_REQS):
                                print(f"   {tag}★ confirm 已发出", flush=True)
                                break
                        if not any("confirm" in r for r in API_REQS):
                            print(f"   {tag}无 confirm，再点一次提交", flush=True)
                            for _sel in ("button[type=submit]",
                                         "button:has-text('Subscribe')",
                                         "button:has-text('Pay')"):
                                try:
                                    _b = page.locator(_sel)
                                    if await _b.count() > 0:
                                        await _b.first.click(timeout=8000)
                                        print(f"   {tag}已再次点击 {_sel}", flush=True)
                                        break
                                except Exception:
                                    pass
                            for _tk2 in range(12):
                                await page.wait_for_timeout(2500)
                                if any("confirm" in r for r in API_REQS):
                                    print(f"   {tag}★ confirm 已发出（二次点击后）", flush=True)
                                    break
                                if CAPTURED:
                                    break
                        try:
                            await page.screenshot(path=f"/tmp/passed_{rnd}.png")
                            print(f"   {tag}已存截图 /tmp/passed_{rnd}.png", flush=True)
                        except Exception:
                            pass
                        return True
                # hCaptcha 出题/静默通过都需要较长时间，分 12 段轮询
                for _w in range(12):
                    await page.wait_for_timeout(5000)
                    st = await page_state(page)
                    if st != "none":
                        print(f"   {tag}★ 页面已推进: {st}", flush=True)
                        return True
                    try:
                        elw = await fr.query_selector("canvas")
                        if elw is not None:
                            bbw = await elw.bounding_box()
                            if bbw and bbw["width"] > 120:
                                print(f"   {tag}canvas 已出现（等待 {(_w+1)*5}s）", flush=True)
                                break
                    except Exception:
                        pass
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
            if not pts and "drag" in prompt.lower():
                g = make_grid(cv["path"], cv["W"], cv["H"])
                if g:
                    try:
                        outd = await reason_drag(cv["path"], g, prompt)
                        d = outd.model_dump()
                        print(f"   {tag}拖拽路径: {json.dumps(d, ensure_ascii=False)[:220]}", flush=True)
                        rectd = cv["rect"]
                        for path in (d.get("paths") or [])[:2]:
                            sp = path.get("start_point") or {}
                            ep = path.get("end_point") or {}
                            sx = rectd["x"] + float(sp.get("x", 0)) / cv["W"] * rectd["w"]
                            sy = rectd["y"] + float(sp.get("y", 0)) / cv["H"] * rectd["h"]
                            ex = rectd["x"] + float(ep.get("x", 0)) / cv["W"] * rectd["w"]
                            ey = rectd["y"] + float(ep.get("y", 0)) / cv["H"] * rectd["h"]
                            await page.mouse.move(sx, sy)
                            await page.wait_for_timeout(200)
                            await page.mouse.down()
                            await page.wait_for_timeout(150)
                            for k2 in range(1, 13):
                                await page.mouse.move(sx + (ex - sx) * k2 / 12,
                                                      sy + (ey - sy) * k2 / 12)
                                await page.wait_for_timeout(35)
                            await page.wait_for_timeout(150)
                            await page.mouse.up()
                            print(f"   {tag}已拖拽 ({round(sx)},{round(sy)}) -> ({round(ex)},{round(ey)})", flush=True)
                            await page.wait_for_timeout(3000)
                        pts = [1]
                    except Exception as exc:
                        print(f"   {tag}拖拽求解失败:", str(exc)[:90], flush=True)

            if not pts:
                # 拖拽题/无法判题的题型 → Skip 换一道
                skipped = False
                for sel in ('div.refresh.button', 'div.refresh',
                            '[class*="refresh"]',
                            'div.button-submit:has-text("Skip")',
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

            # 调试：把服务商给的点画到 canvas 上，便于人工核对
            try:
                from PIL import Image as _I, ImageDraw as _D
                _im = _I.open(cv["path"]).convert("RGB")
                _d = _D.Draw(_im)
                for _bx, _by in pts[:6]:
                    _x = float(_bx) / cv["W"] * _im.width
                    _y = float(_by) / cv["H"] * _im.height
                    _d.ellipse([_x-12, _y-12, _x+12, _y+12], outline=(255,0,0), width=3)
                    _d.line([_x-16,_y,_x+16,_y], fill=(255,0,0), width=2)
                    _d.line([_x,_y-16,_x,_y+16], fill=(255,0,0), width=2)
                _im.save(f"/tmp/verify_pts_{rnd}.jpg", quality=92)
                print(f"   {tag}已存核对图 /tmp/verify_pts_{rnd}.jpg", flush=True)
            except Exception as _e:
                print(f"   {tag}核对图失败:", str(_e)[:50], flush=True)

            for bx, by in pts[:6]:
                px = rect["x"] + (bx / cv["W"]) * rect["w"]
                py = rect["y"] + (by / cv["H"]) * rect["h"]
                try:
                    await page.mouse.click(px, py)
                except Exception:
                    pass
                await page.wait_for_timeout(430)
            await page.wait_for_timeout(700)
            try:
                allb = await fr.evaluate(
                    """() => [...document.querySelectorAll('.button-submit')]
                         .map(e => (e.innerText||'').trim().slice(0,24))""")
                print(f"   {tag}可用按钮: {allb}", flush=True)
            except Exception:
                pass
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
                        submitted_any = True
                        break
                except Exception:
                    pass
            if hit:
                # 已提交一页，检查是否还有下一页；有就继续，没有才返回
                await page.wait_for_timeout(5000)
                st3 = await page_state(page)
                if st3 != "none":
                    print(f"   {tag}★ 页面已推进: {st3}", flush=True)
                    return True
                try:
                    await page.screenshot(path=f"/tmp/after_submit_{rnd}.png")
                    bnow = (await page.inner_text("body"))[:300].replace("\n", " ")
                    print(f"   {tag}提交后页面: {bnow[:180]}", flush=True)
                    hcnow = [f.url[:50] for f in page.frames if "hcaptcha" in (f.url or "")]
                    print(f"   {tag}提交后 hcaptcha 帧: {len(hcnow)}", flush=True)
                except Exception:
                    pass
                still = [f for f in page.frames if "hcaptcha" in (f.url or "")]
                if still:
                    hit = False
                    print(f"   {tag}提交后仍有挑战帧，继续下一页", flush=True)
                    continue
                return True
        await page.wait_for_timeout(3000)
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
                        el0 = loc.first
                        # 不先 click（会触发 Google 地址联想下拉，遮挡后续字段）
                        await page.wait_for_timeout(random.randint(60, 200))
                        await el0.fill(str(val), timeout=6000)
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
    # ── 兜底：重新收集帧，再补一次 city / postal ──
    try:
        frames = await all_frames(page)
        for _lab, _sels, _val in (
            ("city", ['input[placeholder="City"]', 'input[name="city"]',
                      'input[autocomplete="address-level2"]'], "Portland"),
            ("postal", ['input[placeholder="ZIP"]', 'input[name="postal"]',
                        'input[autocomplete="postal-code"]'], "97211"),
        ):
            if _lab in filled:
                continue
            _ok = await try_fill(_sels, _val, _lab)
            print(f"   兜底填 {_lab}: {_ok}", flush=True)
    except Exception as _e:
        print("   兜底填写异常:", str(_e)[:60], flush=True)


    return filled


async def main():
    auth = json.loads(Path("/opt/gss/config/container-auth.json").read_text())
    env = load_env("/opt/gss/app/cards.env")
    ats = json.load(open("/opt/gss/data/at_list.json"))
    _k = os.environ.get("GSS_CARD", "A").upper()
    card = {"number": env[f"CARD_{_k}_NUMBER"], "exp_month": env[f"CARD_{_k}_EXP_MONTH"],
            "exp_year": env[f"CARD_{_k}_EXP_YEAR"], "cvc": env[f"CARD_{_k}_CVC"]}
    print(f"使用卡: CARD_{_k} ****{card['number'][-4:]}", flush=True)

    d = None
    random_start = random.randrange(max(1, len(ats)))
    for i in range(random_start, min(random_start + 6, len(ats))):
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

    from patchright.async_api import async_playwright
    async with async_playwright() as p:
        # 持久化 profile：累积 cookie / localStorage / 历史，降低 hCaptcha 风险分
        PROFILE = os.environ.get("GSS_BROWSER_PROFILE", "/opt/gss/browser-profile")
        os.makedirs(PROFILE, exist_ok=True)
        ctx = await p.chromium.launch_persistent_context(
            user_data_dir=PROFILE,
            executable_path=CHROME, headless=False,
            proxy=proxy_cfg(proxy),
            viewport={"width": 1280, "height": 980},
            locale="en-US", timezone_id="America/New_York",
            args=["--no-sandbox", "--disable-dev-shm-usage",
                  "--window-size=1280,980",
                  "--disable-blink-features=AutomationControlled"],
        )
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()

        # 抓 token：hCaptcha 企业版 token 以 P1_ 开头，Stripe 提交 verify_challenge 时在请求体里
        def _on_req(req):
            try:
                pd = req.post_data
            except Exception:
                pd = None
            u = req.url or ""
            if pd and ("challenge_response_token" in pd or "P1_" in pd):
                CAPTURED.append({"url": u[:120], "body": pd})
                tok = ""
                for part in pd.replace("&", "\n").split("\n"):
                    if part.startswith("challenge_response_token="):
                        tok = part.split("=", 1)[1]
                if tok:
                    try:
                        Path("/tmp/token_captured.txt").write_text(tok)
                    except Exception:
                        pass
                    print(f"   ★★★ 抓到 hCaptcha token！长度 {len(tok)}", flush=True)
                else:
                    print(f"   ★ 抓到含 token 的请求: {u[:80]}", flush=True)
                    # 直接打印原文，不猜字段名
                    print(f"   BODY: {pd[:700]}", flush=True)
                    try:
                        Path("/tmp/token_body.txt").write_text(pd)
                    except Exception:
                        pass
            elif "api.stripe.com/v1/" in u and req.method == "POST":
                key = u.split("api.stripe.com")[-1][:70]
                API_REQS.append(key)
                if pd:
                    hit = ""
                    for pat in ("challenge_response_token=", "payment_method=",
                                "confirmation_token="):
                        if pat in pd:
                            hit = pat
                            break
                    print(f"   → POST {key} [{hit or '-'}] len={len(pd)}", flush=True)
            elif "verify_challenge" in u:
                CAPTURED.append({"url": u[:120], "body": pd or ""})
                print(f"   ★ verify_challenge 请求已发出", flush=True)

        page.on("request", _on_req)

        async def _on_resp(resp):
            u = resp.url or ""
            if "hcaptcha.com" in u and ("checkcaptcha" in u or "getcaptcha" in u):
                try:
                    _tb = await resp.text()
                except Exception:
                    _tb = ""
                print(f"   >> HCAPTCHA {resp.status} {u.split('hcaptcha.com')[-1][:40]} :: {_tb[:200]}", flush=True)
                return
            if "api.stripe.com/v1/" not in u:
                return
            if not any(k in u for k in ("payment_pages/", "payment_intents/")):
                return
            try:
                body = await resp.text()
            except Exception:
                body = ""
            RESP_CAPTURED.append({"url": u[:110], "status": resp.status, "body": body})
            print(f"   << RESP {resp.status} {u[-50:]} :: {body[:260]}", flush=True)
            try:
                Path("/tmp/confirm_resp.json").write_text(body)
            except Exception:
                pass

        page.on("response", lambda r: asyncio.create_task(_on_resp(r)))
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
            if did:
                print(f"[{rnd}] 挑战已通过，等待支付结果…", flush=True)
                for _r in range(10):
                    await page.wait_for_timeout(3000)
                    st_r = await page_state(page)
                    if st_r in ("done", "declined", "error"):
                        print(f"[{rnd}] ★ 最终状态: {st_r}", flush=True)
                        break
                try:
                    await page.screenshot(path="/tmp/final_result.png")
                except Exception:
                    pass
                break
            break

        await page.screenshot(path="/tmp/co_after_submit.png")
        await page.wait_for_timeout(9000)
        try:
            print("最终正文:", (await page.inner_text("body"))[:400].replace("\n", " "), flush=True)
        except Exception:
            pass
        # —— requires_action 后 3DS 可能自动完成，再等 120 秒看是否落成拒卡 ——
        if any("requires_action" in (r.get("body") or "") for r in RESP_CAPTURED):
            print("\n  检测到 requires_action，等待 3DS 自动完成（最多 120s）…", flush=True)
            _seen = len(RESP_CAPTURED)
            for _w3 in range(24):
                await page.wait_for_timeout(5000)
                if len(RESP_CAPTURED) > _seen:
                    _seen = len(RESP_CAPTURED)
                    print(f"  新响应，共 {_seen} 条", flush=True)
                _all = " ".join((r.get("body") or "") for r in RESP_CAPTURED)
                if any(k in _all for k in ("decline_code", "card_declined",
                                           "requires_payment_method", "do_not_honor")):
                    print("  ★ 3DS 结束，已出现拒卡信号", flush=True)
                    break
                try:
                    await page.screenshot(path="/tmp/after3ds.png")
                except Exception:
                    pass

        # —— 支付判定 ——
        _verdict, _blame = "UNKNOWN", ""
        for _r in RESP_CAPTURED:
            _b = (_r.get("body") or "")
            for _k in ("card_declined", "do_not_honor", "generic_decline",
                       "insufficient_funds", "incorrect_number", "declined"):
                if _k in _b:
                    _verdict, _blame = "DECLINED", _k
                    break
            if _verdict == "DECLINED":
                break
            if "requires_action" in _b:
                _verdict = "REQUIRES_ACTION"
        print(f"\n=== 支付判定: {_verdict} {_blame} ===", flush=True)
        for _r in RESP_CAPTURED[-4:]:
            print(f"   [{_r['status']}] {_r['url'][-60:]}", flush=True)

        # —— token 汇总 ——
        if CAPTURED:
            print("\n=== ★ 捕获到 token / verify_challenge ===", flush=True)
            for c in CAPTURED[:3]:
                print(f"   {c['url'][:90]}", flush=True)
                print(f"   body: {c['body'][:200]}", flush=True)
        else:
            print("\n=== 未捕获到 token ===", flush=True)
        try:
            await page.screenshot(path="/tmp/co_final.png")
        except Exception:
            pass
        await ctx.close()


if __name__ == "__main__":
    asyncio.run(main())
