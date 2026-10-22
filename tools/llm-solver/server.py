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
        points: List[List[float]] = []
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

        for rnd in range(1, MAX_ROUNDS + 1):
            try:
                await page.frame_locator(
                    'iframe[src*="frame=checkbox"]').locator("#checkbox").click(timeout=10000)
                log(f"   [轮次{rnd}] 点了 checkbox")
            except Exception as exc:  # noqa: BLE001
                log(f"   [轮次{rnd}] checkbox: {str(exc)[:80]}")
            await page.wait_for_timeout(4500)

            token = await page.evaluate("window.__token || ''")
            if token:
                info["rounds"].append({"round": rnd, "pass": "checkbox"})
                break

            fr = await find_frame()
            if not fr:
                info["rounds"].append({"round": rnd, "error": "no_challenge_frame"})
                continue

            prompt = await fr.evaluate(
                """() => {
                     const el = document.querySelector('.prompt-text')
                       || document.querySelector('div[class*=prompt]');
                     return el ? el.innerText : document.body.innerText.slice(0, 200);
                   }""")
            prompt = " ".join(str(prompt).split())[:200]

            n = await cell_count(fr)
            if n == 0:
                # 点击题：读 canvas → LLM 给坐标 → 点
                cv = await read_canvas(fr)
                if not cv or not cv.get("jpg"):
                    log(f"   [轮次{rnd}] 无 canvas，Skip：{prompt[:40]}")
                    info["rounds"].append({"round": rnd, "error": "no_canvas"})
                    await click_skip(fr)
                    await page.wait_for_timeout(2000)
                    continue
                img = outdir / f"pt_{rnd}.jpg"
                img.write_bytes(base64.b64decode(str(cv["jpg"]).split(",")[-1]))
                bb = cv.get("bbox") or {"minX": 0, "minY": 0,
                                        "w": cv["W"], "h": cv["H"]}
                try:
                    pans = await llm.generate_with_images(
                        images=[img],
                        response_schema=PointAnswer,
                        user_prompt=(f"题目：{prompt}\n"
                                     f"图片宽 {bb['w']} 像素、高 {bb['h']} 像素。\n"
                                     "请给出所有应该点击的位置，用**像素坐标** [x, y]"
                                     f"（x 范围 0-{bb['w']}，y 范围 0-{bb['h']}），"
                                     "点在目标物体的中心。可以有多个。"),
                        description="你是精确的空间坐标作答助手。先仔细看图定位目标，"
                                    "再换算成像素坐标。只输出 JSON。",
                    )
                except Exception as exc:  # noqa: BLE001
                    log(f"   [轮次{rnd}] LLM 失败：{str(exc)[:90]}")
                    info["rounds"].append({"round": rnd, "error": f"llm:{str(exc)[:70]}"})
                    continue
                log(f"   [轮次{rnd}] 点击题 LLM 给 {len(pans.points)} 个点：{pans.reason[:50]}")
                rect = cv["rect"]
                fb = await frame_box(fr) or {"x": 0, "y": 0}
                log(f"   [轮次{rnd}] iframe 偏移 ({round(fb['x'])},{round(fb['y'])})")
                for pt in pans.points[:6]:
                    try:
                        nx, ny = float(pt[0]), float(pt[1])
                    except Exception:
                        continue
                    # LLM 给的是像素坐标（相对裁剪图）
                    px_in = nx if nx > 1.5 else nx * bb["w"]
                    py_in = ny if ny > 1.5 else ny * bb["h"]
                    px_in = max(0.0, min(px_in, bb["w"] - 1))
                    py_in = max(0.0, min(py_in, bb["h"] - 1))
                    cx = (bb["minX"] + px_in) / cv["W"]
                    cy = (bb["minY"] + py_in) / cv["H"]
                    px = fb["x"] + rect["x"] + cx * rect["w"]
                    py = fb["y"] + rect["y"] + cy * rect["h"]
                    try:
                        await page.mouse.click(px, py)
                    except Exception:
                        pass
                    await page.wait_for_timeout(450)
                await page.wait_for_timeout(1000)
                try:
                    await page.screenshot(path=f"/tmp/hc_svc_pt_{rnd}.png")
                except Exception:
                    pass
                submitted2 = await click_submit(fr)
                await page.wait_for_timeout(4200)
                token = await page.evaluate("window.__token || ''")
                info["rounds"].append({"round": rnd, "kind": "point", "prompt": prompt[:70],
                                       "points": pans.points[:6], "submit": submitted2,
                                       "token": len(token)})
                if token:
                    break
                await click_skip(fr)
                await page.wait_for_timeout(2000)
                continue
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
                info["rounds"].append({"round": rnd, "error": "no_tiles", "prompt": prompt})
                continue

            try:
                ans = await llm.generate_with_images(
                    images=[t for t in tiles if t],
                    response_schema=GridAnswer,
                    user_prompt=(f"题目：{prompt}\n下面是九宫格 9 张图，索引 0-8 按从左到右、"
                                 "从上到下排列。请选出所有符合题意的格子索引。"),
                    description="你是精确的图像选择题作答助手，只输出 JSON。",
                )
            except Exception as exc:  # noqa: BLE001
                info["rounds"].append({"round": rnd, "error": f"llm:{str(exc)[:90]}",
                                       "prompt": prompt})
                continue

            boxes = await fr.evaluate(
                """() => [...document.querySelectorAll('div[role=button][aria-label*="Challenge Image"]')]
                     .map((d, i) => { const r = d.getBoundingClientRect();
                       return { i, x: r.x + r.width/2, y: r.y + r.height/2 }; })""")
            boxes = sorted(boxes, key=lambda b: (round(b["y"] / 20), b["x"]))
            for idx in ans.indices:
                if 0 <= idx < len(boxes):
                    b = boxes[idx]
                    try:
                        await page.mouse.click(b["x"], b["y"])
                    except Exception:
                        pass
                    await page.wait_for_timeout(420)
            await page.wait_for_timeout(1000)

            submitted = await click_submit(fr)
            await page.wait_for_timeout(4200)
            token = await page.evaluate("window.__token || ''")
            info["rounds"].append({"round": rnd, "prompt": prompt[:80],
                                   "indices": ans.indices, "submit": submitted,
                                   "token": len(token)})
            if token:
                break

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
