"""视觉求解服务：接收 hCaptcha 题目 PNG + 题干，返回 canvas 归一化坐标。

启动：
  python3 vision_server.py        # 监听 127.0.0.1:8300

POST /solve
  {"png": "data:image/png;base64,...", "prompt": "...", "width": 482, "height": 332}
  → {"norm": [[x,y],[x,y]], "raw": {...}}
"""
import base64
import io
import json
import os
import sys
import time

import httpx
from PIL import Image
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route
import uvicorn

PROV = json.load(open(os.environ.get("PROVIDERS_JSON")
                       or os.path.expanduser("~/.config/magpie/providers.json")))
# 兼容两种格式：完整版（providers[4] 是 grok）与最小版（providers[0]）
G = None
for p_ in PROV.get("providers") or []:
    if p_.get("key") and p_.get("chat") and "grok" in (p_.get("name") or "").lower():
        G = p_
        break
if G is None:
    for p_ in PROV.get("providers") or []:
        if p_.get("key") and p_.get("chat"):
            G = p_
            break
if G is None:
    raise SystemExit("no usable provider in providers.json")
KEY = G["key"]
BASE = G["chat"]
MODEL = os.environ.get("VISION_MODEL", "grok-4.7")


def ask(png_bytes, prompt, tries=3, max_tokens=600):
    b64 = base64.b64encode(png_bytes).decode()
    body = {
        "model": MODEL, "max_tokens": max_tokens, "temperature": 0,
        "messages": [
            {"role": "system",
             "content": "You are a visual analyst for UI testing. Output valid JSON only, brief, no markdown."},
            {"role": "user", "content": [
                {"type": "text", "text": prompt},
                {"type": "image_url",
                 "image_url": {"url": f"data:image/png;base64,{b64}"}},
            ]},
        ],
    }
    for i in range(tries):
        try:
            r = httpx.post(f"{BASE}/chat/completions",
                           headers={"Authorization": f"Bearer {KEY}",
                                    "Content-Type": "application/json"},
                           json=body, timeout=180)
            if r.status_code != 200:
                time.sleep(2)
                continue
            t = r.json()["choices"][0]["message"]["content"].strip()
            if t.startswith("```"):
                t = t.split("```")[1]
                if t.startswith("json"):
                    t = t[4:]
                t = t.strip()
            i0, k0 = t.find("{"), t.rfind("}")
            if i0 >= 0 and k0 > i0:
                t = t[i0:k0 + 1]
            return json.loads(t)
        except Exception:
            time.sleep(2)
    return None


def build_prompt(W, H, prompt_text, n_lines=5):
    return f'''Image size: {W} x {H} pixels.
Instruction shown to the user: "{prompt_text}"

The image contains exactly {n_lines} semi-transparent curved strokes drawn over
a photographic background.

For each stroke, estimate its total arc length in pixels, then select the two
with the smallest length. For each selected stroke, give one pixel coordinate
lying exactly on that stroke.

JSON ONLY:
{{"ranked":[{{"id":1,"len":50,"pt":[x,y]}}, ...{n_lines} items shortest first...],
 "click":[[x,y],[x,y]]}}'''


async def solve_grid_request(request):
    """9 宫格题：接收整张 challenge 截图 + 格子坐标，返回要点击的格子索引。"""
    body = await request.json()
    du = body.get("png") or ""
    prompt_text = body.get("prompt") or ""
    cells = body.get("cells") or []
    if "," in du:
        du = du.split(",", 1)[1]
    try:
        raw = base64.b64decode(du)
        im = Image.open(io.BytesIO(raw))
        W, H = im.size
    except Exception as e:
        return JSONResponse({"error": "bad_png: " + str(e)[:80]}, status_code=400)

    # 不依赖 cells：直接把整张截图交给模型，让它按 3x3 阅读顺序编号
    prompt = f'''Screenshot of a UI panel. Instruction text shown at top: "{prompt_text}"

A small reference thumbnail appears in the prompt bar at the very top.
Below it is a 3x3 grid of 9 picture tiles numbered in reading order:
  row1: 1 2 3
  row2: 4 5 6
  row3: 7 8 9

Identify what the reference thumbnail depicts, then select all tiles belonging
to the same category.

JSON ONLY: {{"example":"...","picks":[5],"reason":"..."}}'''

    out = ask(raw, prompt)
    if not out:
        return JSONResponse({"error": "vision_failed"}, status_code=502)
    picks = out.get("picks") or []
    idx = []
    for p in picks:
        try:
            v = int(p) - 1
            if 0 <= v < 9:
                idx.append(v)
        except Exception:
            pass
    if not idx:
        return JSONResponse({"error": "no_picks", "raw": out}, status_code=502)
    return JSONResponse({"indices": idx, "raw": out, "size": [W, H]})


async def solve_endpoint(request):
    body = await request.json()
    du = body.get("png") or ""
    prompt_text = body.get("prompt") or "Please click on the TWO lines that are shorter"
    if "," in du:
        du = du.split(",", 1)[1]
    try:
        raw = base64.b64decode(du)
        im = Image.open(io.BytesIO(raw))
        W, H = im.size
    except Exception as e:
        return JSONResponse({"error": "bad_png: " + str(e)[:80]}, status_code=400)

    out = ask(raw, build_prompt(W, H, prompt_text))
    if not out:
        return JSONResponse({"error": "vision_failed"}, status_code=502)

    click = out.get("click") or []
    if len(click) < 2:
        click = [r["pt"] for r in (out.get("ranked") or [])[:2] if "pt" in r]
    if len(click) < 2:
        return JSONResponse({"error": "no_points", "raw": out}, status_code=502)

    norm = [[round(p[0] / W, 4), round(p[1] / H, 4)] for p in click[:2]]
    return JSONResponse({"norm": norm, "raw": out, "size": [W, H]})


async def health(request):
    return JSONResponse({"ok": True, "model": MODEL})


app = Starlette(routes=[
    Route("/solve", solve_endpoint, methods=["POST"]),
    Route("/solve-grid", solve_grid_request, methods=["POST"]),
    Route("/health", health, methods=["GET"]),
])

if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=int(os.environ.get("VPORT", "8300")),
                log_level="warning")
