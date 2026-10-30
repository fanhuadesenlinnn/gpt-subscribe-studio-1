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
        # 支持裁剪：默认保留中间 3x3 网格区域（去掉题干栏与底部按钮）
        # 实测：传整图效果最好。裁剪会丢失上下文，
        # 导致模型看不清例题缩略图（在题干栏右上角）。
        crop = body.get("grid_crop") or [0.0, 0.0, 1.0, 1.0]
        cw, ch = im.size
        im2 = im.crop((int(crop[0] * cw), int(crop[1] * ch),
                       int(crop[2] * cw), int(crop[3] * ch)))
        buf = io.BytesIO()
        im2.save(buf, format="PNG")
        raw = buf.getvalue()
        W, H = im2.size
    except Exception as e:
        return JSONResponse({"error": "bad_png: " + str(e)[:80]}, status_code=400)

    # 两步法：先让模型逐格描述，再据此判断。
    # 单步直接问「哪几格符合」时，模型数格子会出错（实测 picks 偏了 4 格）。
    # 走 /solve-grid 时 body 里带 grid_crop，用于裁掉题干栏与底部按钮。

    # Step 1: 逐格描述
    p1 = '''Image size: {W} x {H} pixels.
This is a 3x3 grid of 9 picture tiles, numbered in reading order:
  row1: 1 2 3
  row2: 4 5 6
  row3: 7 8 9

List what each of the 9 tiles depicts. Be concrete and short
(e.g. sailboat, bicycle, car, tree).

JSON ONLY: {{"tiles":["obj1","obj2","obj3","obj4","obj5","obj6","obj7","obj8","obj9"]}}'''
    d1 = ask(raw, p1.format(W=W, H=H))
    tiles = (d1 or {}).get("tiles") or []
    if len(tiles) < 9:
        return JSONResponse({"error": "describe_failed", "raw": d1,
                             "size": [W, H],
                             "asked": p1.format(W=W, H=H)[:300]}, status_code=502)

    # Step 2: 基于描述做判断（不再看图，避免数错格子）
    listing = "\n".join(f"  tile {i+1} = {t}" for i, t in enumerate(tiles))
    # 先单独裁出右上角缩略图，单独问一次（模型有时会忽略缩略图）
    example_desc = ""
    try:
        tw, th = int(W * 0.16), int(H * 0.16)
        thumb = im.crop((W - tw - int(W * 0.02), int(H * 0.01),
                         W - int(W * 0.02), int(H * 0.01) + th))
        tb = io.BytesIO()
        thumb.save(tb, format="PNG")
        t1 = ask(tb.getvalue(),
                 'What single object does this small picture show? '
                 'Answer as JSON: {"object":"<one or two words>"}')
        if t1 and t1.get("object"):
            example_desc = str(t1["object"])[:60]
    except Exception:
        pass

    p2 = f'''Look carefully at the TOP-RIGHT CORNER of this image. There is a small
square reference thumbnail (the "example" picture) inside the blue prompt bar,
to the right of the instruction text. Study what that thumbnail depicts.
{("Hint: the thumbnail appears to show: " + example_desc) if example_desc else ""}

User instruction: "{prompt_text}"

The 3x3 grid below the prompt bar has these 9 tiles (1-based, reading order):
{listing}

Step 1: state what the reference thumbnail depicts.
Step 2: state the category / where it travels.
Step 3: pick EVERY tile that belongs to the same category as the reference.

JSON ONLY: {{"example":"<what the thumbnail shows>","category":"<where/what>","picks":[<1-based tile numbers>],"reason":"..."}}'''
    d2 = ask(raw, p2)
    if not d2:
        return JSONResponse({"error": "judge_failed"}, status_code=502)

    picks = d2.get("picks") or []
    idx = []
    for p in picks:
        try:
            v = int(p) - 1
            if 0 <= v < 9:
                idx.append(v)
        except Exception:
            pass
    if not idx:
        return JSONResponse({"error": "no_picks", "raw": d2}, status_code=502)
    return JSONResponse({"indices": idx, "tiles": tiles, "raw": d2, "size": [W, H]})


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




async def solve_shapes_request(request):
    """形状匹配题（Click the THREE matching shapes / 参照物同形）。
    两步法：先让模型给每个拼块定位+形状描述，再挑与参照物同构的点。"""
    body = await request.json()
    du = body.get("png") or ""
    prompt_text = body.get("prompt") or "Click the THREE matching shapes"
    if "," in du:
        du = du.split(",", 1)[1]
    try:
        raw = base64.b64decode(du)
        im = Image.open(io.BytesIO(raw))
        W, H = im.size
    except Exception as e:
        return JSONResponse({"error": "bad_png: " + str(e)[:80]}, status_code=400)

    # 期望点数：从题干里抓 TWO/THREE/FOUR
    import re as _re
    m = _re.search(r"\b(TWO|THREE|FOUR|2|3|4)\b", prompt_text.upper())
    want = {"TWO": 2, "2": 2, "THREE": 3, "3": 3, "FOUR": 4, "4": 4}.get(m.group(1) if m else "", 3)

    p1 = f"""Image size: {W} x {H} pixels.
User instruction: "{prompt_text}"

The image contains several hex-tile clusters (puzzle pieces) on a colored background.
Step 1: locate EVERY cluster and describe its exact shape (how the hex tiles
are arranged: e.g. "C-shape with 5 tiles", "straight bar of 4", "2x3 block with one corner").
Step 2: list each cluster center as pixel coordinates [x,y].

JSON ONLY: {{"clusters":[{{"pt":[x,y],"shape":"...","tiles":N}}, ...]}}"""
    d1 = ask(raw, p1)
    clusters = (d1 or {}).get("clusters") or []
    if not clusters:
        return JSONResponse({"error": "describe_failed", "raw": d1}, status_code=502)

    listing = "\n".join(
        f"  cluster {i+1}: center {c.get('pt')}, shape: {c.get('shape')}, tiles: {c.get('tiles')}"
        for i, c in enumerate(clusters))
    p2 = f"""User instruction: "{prompt_text}"

There are {len(clusters)} hex-tile clusters:
{listing}

Pick the {want} clusters that are IDENTICAL IN SHAPE to each other
(same tile arrangement, mirrored copies count as same shape).
If the instruction mentions matching a reference/example, match that instead.
Return exactly {want} cluster centers, in the order they appear in the list.

JSON ONLY: {{"picks":[cluster_index_1_based, ...], "click":[[x,y], ...{want} items...]}}"""
    d2 = ask(raw, p2)
    if not d2:
        return JSONResponse({"error": "judge_failed"}, status_code=502)
    click = d2.get("click") or []
    click = [c for c in click if isinstance(c, (list, tuple)) and len(c) == 2]
    if len(click) < want:
        return JSONResponse({"error": "no_points", "raw": d2, "clusters": clusters}, status_code=502)
    return JSONResponse({"click": click[:want], "picks": d2.get("picks"),
                         "clusters": clusters, "size": [W, H]})


app = Starlette(routes=[
    Route("/solve", solve_endpoint, methods=["POST"]),
    Route("/solve-grid", solve_grid_request, methods=["POST"]),
    Route("/solve-shapes", solve_shapes_request, methods=["POST"]),
    Route("/health", health, methods=["GET"]),
])

if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=int(os.environ.get("VPORT", "8300")),
                log_level="warning")
