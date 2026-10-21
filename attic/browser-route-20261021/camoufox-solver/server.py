"""Camoufox hCaptcha solver —— 接口与 Node 版 server.js 保持一致。

POST /solve  {sitekey, rqdata, host, timeout, proxy?, attempts?, per_try?}
  → {token, ekey, elapsedMs, attempt, log} 或 {error, log, frames}

GET  /health
POST /frames  仅诊断

关键设计
  1. Camoufox 是打过补丁的 Firefox，指纹在 C++ 层伪装。
     实测 mediump 精度 23/127/127（headless Chrome 只有 10/15/15）。
  2. Camoufox 的 page.evaluate 跑在「隔离世界」，读不到主世界的 window。
     因此页面必须自驱动：从 URL query 取参数，结果写进 <pre id="result">，
     Python 侧只轮询 textContent（DOM 跨世界共享）。
  3. c7faac4c 在低评分环境下只回「Please try again」空壳，
     此时必须重新 render + execute 重试（本机 macOS 就是靠重试做到 6/8）。
"""
from __future__ import annotations

import json
import os
import threading
import time
from typing import Any

import uvicorn
from camoufox.sync_api import Camoufox
from starlette.applications import Starlette
from starlette.responses import HTMLResponse, JSONResponse
from starlette.routing import Route

PORT = int(os.environ.get("CFX_PORT", "8198"))
SELF_BASE = f"http://127.0.0.1:{PORT}"
DEFAULT_HOST = os.environ.get("MERCHANT_HOST", "b.stripecdn.com")
DEFAULT_ATTEMPTS = int(os.environ.get("CFX_ATTEMPTS", "5"))
DEFAULT_PER_TRY = int(os.environ.get("CFX_PER_TRY_MS", "15000"))
BROWSER_LOCK = threading.Lock()
BROWSERS: dict[str, Any] = {}
MAX_BROWSERS = 3


PAGE_TEMPLATE = r"""<!doctype html><html><head><meta charset="utf-8">
<title>HCaptchaInvisible</title>
<script src="https://hcaptcha.com/1/api.js?onload=captchaLoad&render=explicit" async defer></script>
</head><body><div id="root"></div><pre id="result"></pre>
<script>
(function () {
  var out = document.getElementById('result');
  function done(o) { out.textContent = JSON.stringify(o); }
  var q = new URLSearchParams(location.search);
  var sk = q.get('sitekey') || '';
  var rq = q.get('rqdata') || '';
  var host = q.get('host') || '__HOST__';
  var perTry = parseInt(q.get('per_try') || '15000', 10);
  var maxTry = parseInt(q.get('attempts') || '5', 10);
  if (!sk) { done({ error: 'no_sitekey' }); return; }

  function sleep(ms) { return new Promise(function (r) { setTimeout(r, ms); }); }

  function waitLib() {
    return new Promise(function (resolve) {
      if (window.hcaptcha) return resolve(window.hcaptcha);
      var i = 0;
      var iv = setInterval(function () {
        if (window.hcaptcha) { clearInterval(iv); resolve(window.hcaptcha); }
        else if (++i > 120) { clearInterval(iv); resolve(null); }
      }, 250);
    });
  }

  // 每次重试前清掉旧 widget 与残留 iframe（累积会导致后续握手失败）
  function cleanup() {
    try {
      document.querySelectorAll('iframe[src*="newassets.hcaptcha.com"]')
        .forEach(function (e) { try { e.remove(); } catch (x) {} });
      document.querySelectorAll('[id^="hcaptcha-invisible-"]')
        .forEach(function (e) { try { e.remove(); } catch (x) {} });
    } catch (e) {}
  }

  function once(hc, n) {
    return new Promise(function (resolve) {
      cleanup();
      var d = document.createElement('div');
      d.id = 'hcaptcha-invisible-' + sk + '-' + n;
      (document.getElementById('root') || document.body).appendChild(d);
      var rid;
      try {
        rid = hc.render(d, {
          sitekey: sk, size: 'invisible', host: host,
          'error-callback': function (e) { window.__rcbErr = String(e).slice(0, 200); },
        });
      } catch (e) {
        return resolve({ error: 'render_failed: ' + String(e).slice(0, 160) });
      }
      setTimeout(function () {
        var opts = { async: true };
        if (rq) opts.rqdata = rq;
        var t0 = Date.now(), fin = false;
        var timer = setTimeout(function () {
          if (!fin) { fin = true; resolve({ error: 'execute_timeout', ms: Date.now() - t0 }); }
        }, perTry);
        hc.execute(rid, opts).then(function (v) {
          if (fin) return; fin = true; clearTimeout(timer);
          var tok = (v && v.response) || v;
          if (typeof tok !== 'string' || tok.length < 100)
            return resolve({ error: 'short_token', ms: Date.now() - t0 });
          resolve({ token: tok, ekey: (v && (v.ekey || v.key)) || '', ms: Date.now() - t0 });
        }, function (e) {
          if (fin) return; fin = true; clearTimeout(timer);
          resolve({ error: 'execute_failed: ' + String(e).slice(0, 160), ms: Date.now() - t0 });
        });
      }, 2000);
    });
  }

  (async function () {
    var hc = await waitLib();
    if (!hc) return done({ error: 'api_js_timeout' });
    var log = [];
    var t0 = Date.now();
    for (var n = 1; n <= maxTry; n++) {
      var r = await once(hc, n);
      log.push({ n: n, e: r.error || null, ms: r.ms || 0 });
      if (r.token) {
        return done({ token: r.token, ekey: r.ekey, ms: Date.now() - t0,
                      attempt: n, log: log });
      }
      await sleep(600);
    }
    done({ error: 'all_attempts_failed', ms: Date.now() - t0, log: log });
  })();
})();
</script></body></html>"""


def build_page(host: str) -> str:
    return PAGE_TEMPLATE.replace("__HOST__", host)


def get_browser(proxy: str):
    key = proxy or "__direct__"
    if key in BROWSERS:
        return BROWSERS[key]
    if len(BROWSERS) >= MAX_BROWSERS:
        old_key = next(iter(BROWSERS))
        try:
            BROWSERS.pop(old_key).__exit__(None, None, None)
        except Exception:
            pass
    kwargs: dict[str, Any] = {
        "headless": True,
        "os": "linux",
        "locale": "en-US",
        "humanize": True,
    }
    if proxy:
        if "@" in proxy:
            auth, hostport = proxy.rsplit("@", 1)
            user, _, pwd = auth.split("//", 1)[-1].partition(":")
            kwargs["proxy"] = {"server": "http://" + hostport,
                               "username": user, "password": pwd}
        else:
            kwargs["proxy"] = {"server": proxy}
    cm = Camoufox(**kwargs)
    cm.__enter__()
    BROWSERS[key] = cm
    return cm


def describe_frames(browser) -> list[dict]:
    out = []
    try:
        for ctx in browser.contexts:
            for pg in ctx.pages:
                for fr in pg.frames:
                    info = {"url": (fr.url or "")[:110]}
                    try:
                        info["text"] = (fr.evaluate(
                            "() => (document.body && document.body.innerText || '')"
                            ".replace(/\\s+/g,' ').slice(0,120)") or "")[:120]
                        info["canvas"] = fr.evaluate(
                            "() => document.querySelectorAll('canvas').length")
                        info["imgs"] = fr.evaluate(
                            "() => document.querySelectorAll('img').length")
                        info["btns"] = fr.evaluate(
                            "() => document.querySelectorAll('div[role=button],button').length")
                    except Exception as e:
                        info["err"] = str(e)[:80]
                    out.append(info)
    except Exception:
        pass
    return out


def run_solve(sitekey: str, rqdata: str, host: str, timeout_ms: int,
              proxy: str, attempts: int, per_try: int) -> dict:
    from urllib.parse import quote
    t0 = time.time()
    cm = get_browser(proxy)
    browser = cm.browser
    ctx = browser.new_context(ignore_https_errors=True)
    page = ctx.new_page()
    try:
        url = (f"{SELF_BASE}/solve-page?sitekey={quote(sitekey)}"
               f"&rqdata={quote(rqdata or '')}&host={quote(host)}"
               f"&attempts={attempts}&per_try={per_try}")
        page.goto(url, wait_until="domcontentloaded", timeout=30000)
        budget = (per_try / 1000.0) * attempts + 45
        deadline = time.time() + budget
        result = None
        while time.time() < deadline:
            txt = page.evaluate(
                "() => { var e = document.getElementById('result');"
                " return e ? e.textContent : ''; }")
            if txt:
                try:
                    result = json.loads(txt)
                except Exception:
                    result = {"error": "bad_result", "raw": txt[:200]}
                break
            page.wait_for_timeout(500)
        elapsed = round(time.time() - t0, 1)
        if not result:
            return {"error": "result_timeout", "elapsedMs": int(elapsed * 1000),
                    "frames": describe_frames(browser)}
        if result.get("token"):
            return {"token": result["token"], "ekey": result.get("ekey") or "",
                    "elapsedMs": int(elapsed * 1000),
                    "solveMs": result.get("ms"), "attempt": result.get("attempt"),
                    "log": result.get("log")}
        return {"error": result.get("error", "unknown"),
                "elapsedMs": int(elapsed * 1000),
                "log": result.get("log"),
                "frames": describe_frames(browser)}
    finally:
        try:
            ctx.close()
        except Exception:
            pass


async def health(_):
    return JSONResponse({"ok": True, "engine": "camoufox",
                         "browsers": list(BROWSERS.keys()),
                         "defaults": {"attempts": DEFAULT_ATTEMPTS,
                                      "per_try_ms": DEFAULT_PER_TRY}})


async def solve_page(_):
    return HTMLResponse(build_page(DEFAULT_HOST))


async def solve(request):
    body = await request.json()
    sitekey = body.get("sitekey") or ""
    if not sitekey:
        return JSONResponse({"error": "need sitekey"}, status_code=400)
    rqdata = body.get("rqdata") or ""
    host = body.get("host") or DEFAULT_HOST
    timeout_ms = int(body.get("timeout") or 40000)
    per_try = int(body.get("per_try") or DEFAULT_PER_TRY)
    attempts = int(body.get("attempts") or DEFAULT_ATTEMPTS)
    proxy = body.get("proxy") or os.environ.get("CFX_PROXY") or ""

    loop = __import__("asyncio").get_running_loop()

    def job():
        with BROWSER_LOCK:
            return run_solve(sitekey, rqdata, host, timeout_ms,
                             proxy, attempts, per_try)

    try:
        out = await loop.run_in_executor(None, job)
    except Exception as e:
        return JSONResponse({"error": f"exception: {str(e)[:200]}"}, status_code=500)
    return JSONResponse(out, status_code=200 if out.get("token") else 502)


async def frames(request):
    body = await request.json()
    proxy = body.get("proxy") or os.environ.get("CFX_PROXY") or ""
    cm = BROWSERS.get(proxy or "__direct__")
    if not cm:
        return JSONResponse({"error": "no_browser"}, status_code=404)
    return JSONResponse({"frames": describe_frames(cm.browser)})


app = Starlette(routes=[
    Route("/health", health, methods=["GET"]),
    Route("/solve-page", solve_page, methods=["GET"]),
    Route("/solve", solve, methods=["POST"]),
    Route("/frames", frames, methods=["POST"]),
])

if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=PORT, log_level="warning")
