"""LLM 打码求解服务：POST /solve {sitekey, rqdata, proxy, host} → {token}。

只负责「同会话浏览器里把挑战答出来」这一件事，不碰订单与协议链。
协议侧（StripeProtocol）命中 challenge 后调它拿 token，再自己 verify_challenge。

运行环境要求：Xvfb（DISPLAY=:99）、系统 Chrome、gemini-solver venv。
"""
from __future__ import annotations

import asyncio
import base64
import json
import os
import httpx
import re
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import unquote, urlsplit

from PIL import Image

PORT = int(os.environ.get("GSS_LLM_SOLVER_PORT", "8197"))
BIND = os.environ.get("GSS_LLM_SOLVER_BIND", "0.0.0.0")
CHROME = os.environ.get("GSS_CHROME_PATH", "/usr/bin/google-chrome")
LLM_BASE = os.environ.get("GSS_LLM_BASE_URL", "")
LLM_KEY = os.environ.get("GSS_LLM_KEY", "")
LLM_MODEL = os.environ.get("GSS_LLM_MODEL", "gemini-3.8-flash-high")
MAX_ROUNDS = int(os.environ.get("GSS_LLM_MAX_ROUNDS", "3"))
# 只答九宫格（准确率最高），点击/拖拽题直接 Skip 换题
GRID_ONLY = (os.environ.get("GSS_LLM_GRID_ONLY", "0") == "1")
MAX_PAGES = int(os.environ.get("GSS_LLM_MAX_PAGES", "2"))
# 打码服务（YesCaptcha 判题）优先，LLM 兜底
CAPTCHA_KEY = os.environ.get("GSS_CAPTCHA_API_KEY", "")
CAPTCHA_URL = os.environ.get("GSS_CAPTCHA_API_URL", "https://api.yescaptcha.com").rstrip("/")

LOCK = threading.Lock()

PAGE = """<!doctype html><html><head><meta charset="utf-8"></head>
<body style="margin:0;padding:20px;background:#fff"><div id="hc"></div></body></html>"""


def log(msg: str) -> None:
    print(time.strftime("%H:%M:%S"), msg, flush=True)


def proxy_cfg(url: str) -> Optional[Dict[str, str]]:
    if not url:
        return None
    u = urlsplit(url)
    if not u.hostname:
        return None
    cfg = {"server": f"{u.scheme or 'http'}://{u.hostname}:{u.port or 3010}"}
    if u.username:
        cfg["username"] = unquote(u.username)
        cfg["password"] = unquote(u.password or "")
    return cfg


def grid_overlay(img_path: Path, cols: int = 10, rows: int = 7):
    """在图上叠加带标签网格（列 A-J、行 1-7），让模型报格子编号。"""
    from PIL import ImageDraw
    im = Image.open(img_path).convert("RGB")
    W, H = im.size
    d = ImageDraw.Draw(im)
    cw, ch = W / cols, H / rows
    letters = "ABCDEFGHIJKLMNOP"
    for c in range(cols):
        x = c * cw
        d.line([(x, 0), (x, H)], fill=(255, 40, 40), width=1)
        if c < len(letters):
            d.text((x + 2, 1), letters[c], fill=(255, 40, 40))
    for r in range(rows):
        y = r * ch
        d.line([(0, y), (W, y)], fill=(255, 40, 40), width=1)
        d.text((1, y + 1), str(r + 1), fill=(255, 40, 40))
    out = img_path.with_name(img_path.stem + "_grid.jpg")
    im.save(out, quality=93)
    return out, cw, ch, letters


async def classify_with_service(images_b64: List[str], question: str) -> Dict[str, Any]:
    """调 YesCaptcha HCaptchaClassification。返回 {indices:[int]} 或 {boxes:[[x,y]]} 或 {error}。"""
    if not CAPTCHA_KEY:
        return {"error": "no_captcha_key"}
    task: Dict[str, Any] = {"type": "HCaptchaClassification", "queries": images_b64}
    if question:
        task["question"] = question
    try:
        async with httpx.AsyncClient(timeout=90) as c:
            r = await c.post(f"{CAPTCHA_URL}/createTask",
                             json={"clientKey": CAPTCHA_KEY, "task": task})
            j = r.json() or {}
    except Exception as exc:  # noqa: BLE001
        return {"error": f"service:{str(exc)[:70]}"}
    if j.get("errorId"):
        return {"error": f"{j.get('errorCode')}:{str(j.get('errorDescription'))[:60]}"}
    sol = j.get("solution") or {}
    out: Dict[str, Any] = {}
    if isinstance(sol.get("objects"), list):
        out["indices"] = [i for i, v in enumerate(sol["objects"]) if v]
    elif isinstance(sol.get("top_k"), list):
        out["indices"] = [int(x) for x in sol["top_k"]]
    if isinstance(sol.get("box"), list) and sol["box"]:
        nums = []
        for v in sol["box"]:
            try:
                nums.append(float(v))
            except Exception:
                pass
        out["boxes"] = [[nums[i], nums[i + 1]] for i in range(0, len(nums) - 1, 2)]
    if "indices" not in out and "boxes" not in out:
        out["error"] = "empty_solution"
    return out


async def solve_in_browser(sitekey: str, rqdata: str, proxy: str, host: str) -> Dict[str, Any]:
    from playwright.async_api import async_playwright
    from hcaptcha_challenger.tools.internal.providers.anthropic_compat import (
        AnthropicCompatProvider,
    )
    from pydantic import BaseModel

    class GridAnswer(BaseModel):
        indices: List[int]
        reason: str = ""

    class PointAnswer(BaseModel):
        cells: List[str] = []
        reason: str = ""

    llm = AnthropicCompatProvider(api_key=LLM_KEY, model=LLM_MODEL, base_url=LLM_BASE)
    outdir = Path("/tmp/hc_svc")
    outdir.mkdir(parents=True, exist_ok=True)
    token = ""
    info: Dict[str, Any] = {"rounds": []}

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            executable_path=CHROME, headless=False,
            args=["--no-sandbox", "--disable-dev-shm-usage", "--window-size=1280,900"],
        )
        ctx = await browser.new_context(proxy=proxy_cfg(proxy),
                                        viewport={"width": 1280, "height": 900})
        page = await ctx.new_page()

        ok = False
        for attempt in range(3):
            try:
                await page.goto("https://example.com", wait_until="domcontentloaded",
                                timeout=45000)
                await page.set_content(PAGE, wait_until="load")
                ok = True
                break
            except Exception as exc:  # noqa: BLE001
                log(f"   页面加载失败({attempt+1}/3): {str(exc)[:70]}")
                await page.wait_for_timeout(2500)
        if not ok:
            await browser.close()
            return {"error": "page_load_failed"}

        await page.add_script_tag(
            url="https://js.hcaptcha.com/1/api.js?render=explicit&enterprise=1")
        ready = False
        for _ in range(120):
            try:
                if await page.evaluate("typeof window.hcaptcha !== 'undefined'"):
                    ready = True
                    break
            except Exception:
                pass
            await page.wait_for_timeout(200)
        if not ready:
            await browser.close()
            return {"error": "api_js_not_loaded"}

        await page.evaluate(
            """([sk, rq, hst]) => {
                 window.__token = '';
                 return hcaptcha.render('hc', {
                   sitekey: sk, size: 'normal', host: hst || 'b.stripecdn.com',
                   rqdata: rq || undefined,
                   callback: (t) => { window.__token = t || ''; },
                 });
               }""", [sitekey, rqdata, host])
        await page.wait_for_timeout(2500)

        async def find_frame():
            """按 URL 认挑战框（不要求有九宫格：点击题没有格子）。"""
            best = None
            for fr in page.frames:
                if "frame=challenge" not in (fr.url or ""):
                    continue
                try:
                    if await fr.locator("div.challenge-view").count() > 0:
                        return fr
                    if await fr.locator("canvas").count() > 0:
                        best = fr
                except Exception:
                    pass
            return best

        async def cell_count(fr) -> int:
            try:
                return await fr.locator(
                    'div[role=button][aria-label*="Challenge Image"]').count()
            except Exception:
                return 0

        async def frame_box(fr):
            """challenge iframe 在页面坐标系里的位置。"""
            try:
                for el in await page.query_selector_all('iframe[src*="frame=challenge"]'):
                    b = await el.bounding_box()
                    if b and b["width"] >= 50 and b["height"] >= 50:
                        return b
            except Exception:
                pass
            return None

        async def read_canvas(fr):
            return await fr.evaluate(
                """() => {
                     const cs = [...document.querySelectorAll('canvas')]
                       .map(c => ({ c: c, r: c.getBoundingClientRect() }))
                       .filter(o => o.r.width > 120 && o.r.height > 120)
                       .sort((a, b) => (b.r.width*b.r.height) - (a.r.width*a.r.height));
                     if (!cs.length) return null;
                     const c = cs[0].c, r = cs[0].r;
                     const W = c.width, H = c.height;
                     let d;
                     try { d = c.getContext('2d').getImageData(0,0,W,H).data; } catch(e) { return null; }
                     const br = (i) => (d[i]+d[i+1]+d[i+2])/3;
                     let minX=W,minY=H,maxX=-1,maxY=-1;
                     for (let y=0;y<H;y+=2) for (let x=0;x<W;x+=2) {
                       if (br((y*W+x)*4) > 26) {
                         if (x<minX) minX=x; if (x>maxX) maxX=x;
                         if (y<minY) minY=y; if (y>maxY) maxY=y;
                       }
                     }
                     const rect = {x:r.x, y:r.y, w:r.width, h:r.height};
                     if (maxX < 0) return {W,H,rect,bbox:null,jpg:null};
                     const w = maxX-minX+1, h = maxY-minY+1;
                     const t = document.createElement('canvas');
                     t.width = w; t.height = h;
                     t.getContext('2d').drawImage(c, minX, minY, w, h, 0, 0, w, h);
                     return {W,H,rect,bbox:{minX,minY,w,h},jpg:t.toDataURL('image/jpeg',0.92)};
                   }""")

        async def click_submit(fr) -> str:
            """点 Next / Verify / Submit，多种选择器兜底。"""
            for label in ("Next", "Verify", "Submit", "Continue"):
                cands = [
                    f"xpath=//*[normalize-space(text())='{label}']",
                    f"xpath=//*[contains(@aria-label,'{label}')]",
                    f"button:has-text('{label}')",
                ]
                for sel in cands:
                    try:
                        loc = fr.locator(sel)
                        if await loc.count() > 0:
                            await loc.first.click(timeout=5000)
                            return label
                    except Exception:
                        continue
            return ""

        async def click_skip(fr) -> bool:
            for label in ("Skip", "skip"):
                try:
                    b = fr.locator(f"//div[normalize-space(.)='{label}']")
                    if await b.count() > 0:
                        await b.first.click(timeout=4000)
                        return True
                except Exception:
                    pass
            return False

        async def answer_one_page(fr) -> dict:
            """答当前这一页（九宫格或点击题）。返回诊断信息。"""
            prompt = await fr.evaluate(
                """() => {
                     const el = document.querySelector('.prompt-text')
                       || document.querySelector('div[class*=prompt]');
                     return el ? el.innerText : document.body.innerText.slice(0, 200);
                   }""")
            prompt = " ".join(str(prompt).split())[:200]
            n = await cell_count(fr)
            res: Dict[str, Any] = {"prompt": prompt[:80], "cells": n}

            if n > 0:
                # ── 九宫格 ──
                cells = fr.locator('div[role=button][aria-label*="Challenge Image"]')
                tiles = []
                for i in range(min(n, 9)):
                    fp = outdir / f"t{i}.png"
                    try:
                        await cells.nth(i).screenshot(path=str(fp), timeout=6000)
                        tiles.append(fp)
                    except Exception:
                        tiles.append(None)
                if not any(tiles):
                    res["error"] = "no_tiles"
                    return res
                real = [t for t in tiles if t]
                svc = await classify_with_service(
                    [base64.b64encode(Path(t).read_bytes()).decode() for t in real], prompt)
                if svc.get("indices"):
                    log(f"   打码服务判题：{svc['indices']}")
                    res.update({"kind": "grid", "indices": svc["indices"],
                                "by": "service", "reason": ""})
                    boxes0 = await fr.evaluate(
                        """() => [...document.querySelectorAll('div[role=button][aria-label*="Challenge Image"]')]
                             .map((d, i) => { const r = d.getBoundingClientRect();
                               return { i, x: r.x + r.width/2, y: r.y + r.height/2 }; })""")
                    boxes0 = sorted(boxes0, key=lambda b: (round(b["y"] / 20), b["x"]))
                    fb0 = await frame_box(fr) or {"x": 0, "y": 0}
                    for idx in svc["indices"]:
                        if 0 <= idx < len(boxes0):
                            await page.mouse.click(fb0["x"] + boxes0[idx]["x"],
                                                   fb0["y"] + boxes0[idx]["y"])
                            await page.wait_for_timeout(420)
                    await page.wait_for_timeout(800)
                    res["submit"] = await click_submit(fr)
                    return res
                if svc.get("error") and svc["error"] != "empty_solution":
                    log(f"   服务商不可用（{svc['error']}），改用 LLM")
                try:
                    ans = await llm.generate_with_images(
                        images=real,
                        response_schema=GridAnswer,
                        user_prompt=(f"题目：{prompt}\n下面是九宫格 9 张图，索引 0-8 按从左到右、"
                                     "从上到下排列。\n"
                                     "**必须列出全部符合条件的格子**，这类题通常有 2-4 个答案，"
                                     "漏选任何一个都算错。先逐格判断，再汇总。"),
                        description="你是精确的图像选择题作答助手。只输出 JSON。",
                    )
                    # 查漏：再问一遍是否还有遗漏
                    chk = await llm.generate_with_images(
                        images=real,
                        response_schema=GridAnswer,
                        user_prompt=(f"题目：{prompt}\n下面是九宫格 9 张图（索引 0-8）。\n"
                                     f"上一次的判断结果只选了 {ans.indices}。\n"
                                     "请重新逐格核对：还有哪些格子也符合题意但被漏掉了？\n"
                                     "只回答**漏掉的**索引；若没有遗漏，返回空数组。"),
                        description="你是严格的复核员，专门找出被遗漏的选项。只输出 JSON。",
                    )
                    merged = list(dict.fromkeys(list(ans.indices) + list(chk.indices)))
                    if chk.indices:
                        log(f"   查漏补上 {chk.indices} → {merged}")
                    ans.indices = merged
                except Exception as exc:  # noqa: BLE001
                    res["error"] = f"llm:{str(exc)[:70]}"
                    return res
                boxes = await fr.evaluate(
                    """() => [...document.querySelectorAll('div[role=button][aria-label*="Challenge Image"]')]
                         .map((d, i) => { const r = d.getBoundingClientRect();
                           return { i, x: r.x + r.width/2, y: r.y + r.height/2 }; })""")
                boxes = sorted(boxes, key=lambda b: (round(b["y"] / 20), b["x"]))
                fb = await frame_box(fr) or {"x": 0, "y": 0}
                for idx in ans.indices:
                    if 0 <= idx < len(boxes):
                        await page.mouse.click(fb["x"] + boxes[idx]["x"],
                                               fb["y"] + boxes[idx]["y"])
                        await page.wait_for_timeout(420)
                await page.wait_for_timeout(800)
                res.update({"kind": "grid", "indices": ans.indices,
                            "reason": ans.reason[:60]})
                res["submit"] = await click_submit(fr)
                return res

            # ── 点击题 ──
            if GRID_ONLY:
                res["error"] = "skip_non_grid"
                res["skip"] = True
                return res
            cv = await read_canvas(fr)
            if not cv or not cv.get("jpg"):
                res["error"] = "no_canvas"
                return res
            img = outdir / f"pt_{abs(hash(prompt)) % 100000}.jpg"
            img.write_bytes(base64.b64decode(str(cv["jpg"]).split(",")[-1]))
            bb = cv.get("bbox") or {"minX": 0, "minY": 0, "w": cv["W"], "h": cv["H"]}

            # 打码服务优先：它返回的 box 是上传图的像素坐标，实测对点击题很准
            svc = await classify_with_service(
                [base64.b64encode(img.read_bytes()).decode()], prompt)
            log(f"   服务商返回: {list(svc.keys())} "
                f"boxes={len(svc.get('boxes') or [])} err={svc.get('error')}")
            if svc.get("boxes"):
                rect0 = cv["rect"]
                fb0 = await frame_box(fr) or {"x": 0, "y": 0}
                for bx, by in svc["boxes"][:6]:
                    px_in = max(0.0, min(float(bx), bb["w"] - 1))
                    py_in = max(0.0, min(float(by), bb["h"] - 1))
                    cx0 = (bb["minX"] + px_in) / cv["W"]
                    cy0 = (bb["minY"] + py_in) / cv["H"]
                    await page.mouse.click(fb0["x"] + rect0["x"] + cx0 * rect0["w"],
                                           fb0["y"] + rect0["y"] + cy0 * rect0["h"])
                    await page.wait_for_timeout(430)
                await page.wait_for_timeout(800)
                log(f"   打码服务给 {len(svc['boxes'][:6])} 个点")
                try:
                    await page.screenshot(path=f"/tmp/hc_svc_clicked.png")
                except Exception:
                    pass
                res.update({"kind": "point", "by": "service",
                            "boxes": svc["boxes"][:6]})
                res["submit"] = await click_submit(fr)
                return res
            if svc.get("error") and svc["error"] != "empty_solution":
                log(f"   服务商点击题不可用（{svc['error']}），改用 LLM")
            gimg, cw, ch, letters = grid_overlay(img, 10, 7)
            try:
                pans = await llm.generate_with_images(
                    images=[gimg],
                    response_schema=PointAnswer,
                    user_prompt=(f"题目：{prompt}\n"
                                 "图中已叠加红色网格：列用字母 A-J 标注，行用数字 1-7 标注。\n"
                                 "请找出所有符合题意的目标，回答它们所在的**格子编号**，"
                                 '格式如 "C4"、"D5"。只回答格子编号，不要坐标。'),
                    description="你是精确的图像定位助手。利用网格标签仔细核对目标所在格子。"
                                "只输出 JSON。",
                )
            except Exception as exc:  # noqa: BLE001
                res["error"] = f"llm:{str(exc)[:70]}"
                return res
            rect = cv["rect"]
            fb = await frame_box(fr) or {"x": 0, "y": 0}
            px_list = []
            for lab in pans.cells[:6]:
                lab = str(lab).strip().upper()
                m = re.match(r"^([A-Z])\s*-?\s*(\d{1,2})$", lab)
                if not m or m.group(1) not in letters:
                    continue
                ci = letters.index(m.group(1))
                ri = int(m.group(2)) - 1
                if ri < 0 or ri > 6:
                    continue
                px_list.append(((ci + 0.5) * cw, (ri + 0.5) * ch))
            for px_in, py_in in px_list:
                px_in = max(0.0, min(px_in, bb["w"] - 1))
                py_in = max(0.0, min(py_in, bb["h"] - 1))
                cx = (bb["minX"] + px_in) / cv["W"]
                cy = (bb["minY"] + py_in) / cv["H"]
                await page.mouse.click(fb["x"] + rect["x"] + cx * rect["w"],
                                       fb["y"] + rect["y"] + cy * rect["h"])
                await page.wait_for_timeout(450)
            await page.wait_for_timeout(800)
            res.update({"kind": "point", "cells": pans.cells[:6],
                        "reason": pans.reason[:60]})
            res["submit"] = await click_submit(fr)
            return res

        for rnd in range(1, MAX_ROUNDS + 1):
            if not await find_frame():
                try:
                    await page.frame_locator(
                        'iframe[src*="frame=checkbox"]').locator("#checkbox").click(timeout=10000)
                    log(f"   [轮次{rnd}] 点了 checkbox")
                except Exception as exc:  # noqa: BLE001
                    log(f"   [轮次{rnd}] checkbox: {str(exc)[:60]}")
                await page.wait_for_timeout(4500)
            token = await page.evaluate("window.__token || ''")
            if token:
                break

            # 同一挑战最多答 3 页，答完不立刻 Skip
            for pno in range(1, MAX_PAGES + 1):
                fr = await find_frame()
                if not fr:
                    break
                try:
                    res = await answer_one_page(fr)
                except Exception as exc:  # noqa: BLE001
                    res = {"error": f"exc:{str(exc)[:80]}"}
                res.update({"round": rnd, "page": pno})
                info["rounds"].append(res)
                log(f"   [轮次{rnd}.{pno}] {res.get('kind', res.get('error'))} "
                    f"submit={res.get('submit', '')} idx={res.get('indices')} "
                    f"cells={res.get('cells')}")
                await page.wait_for_timeout(4200)
                token = await page.evaluate("window.__token || ''")
                if token:
                    break
                if res.get("skip"):
                    break
                if not await find_frame():
                    break
            if token:
                break

            fr2 = await find_frame()
            if fr2:
                await click_skip(fr2)
                await page.wait_for_timeout(1800)

        try:
            await page.screenshot(path="/tmp/hc_svc_last.png")
        except Exception:
            pass
        await browser.close()

    if token:
        info["token"] = token
    else:
        info["error"] = "no_token"
    return info


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # noqa: D401
        log(f"{self.address_string()} {fmt % args}")

    def _send(self, code: int, payload: Dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/health":
            return self._send(200, {"ok": True, "llm": bool(LLM_BASE and LLM_KEY),
                                    "model": LLM_MODEL})
        self._send(404, {"error": "not_found"})

    def _allowed(self) -> bool:
        """只接受内网来源（容器经 docker bridge 访问）。"""
        ip = str(self.client_address[0] or "")
        return ip.startswith(("127.", "10.", "172.", "192.168.", "::1"))

    def do_POST(self):
        if not self._allowed():
            return self._send(403, {"error": "forbidden"})
        if self.path != "/solve":
            return self._send(404, {"error": "not_found"})
        try:
            n = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(n) or b"{}")
        except Exception:
            return self._send(400, {"error": "bad_json"})
        sitekey = str(body.get("sitekey") or "")
        rqdata = str(body.get("rqdata") or "")
        proxy = str(body.get("proxy") or "")
        host = str(body.get("host") or "b.stripecdn.com")
        if not sitekey:
            return self._send(400, {"error": "need_sitekey"})
        if not (LLM_BASE and LLM_KEY):
            return self._send(503, {"error": "llm_not_configured"})
        if not LOCK.acquire(timeout=240):
            return self._send(429, {"error": "busy"})
        try:
            out = asyncio.run(solve_in_browser(sitekey, rqdata, proxy, host))
        except Exception as exc:  # noqa: BLE001
            out = {"error": f"exception:{str(exc)[:160]}"}
        finally:
            LOCK.release()
        return self._send(200, out)


def main() -> None:
    log(f"LLM solver listening on {BIND}:{PORT}（仅内网来源）")
    log(f"  model: {LLM_MODEL}  base: {LLM_BASE[:40] or '(未配置)'}")
    ThreadingHTTPServer((BIND, PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
