"""在 Stripe 真实的 HCaptchaInvisible.html 内解 hCaptcha passive 挑战。

设计要点（对照 HAR 的四层加载链）：
  ① js.stripe.com/v3/hcaptcha-invisible-*.html   ← 外层包装，我们不复刻
  ② b.stripecdn.com/.../HCaptchaInvisible.html   ← 直接加载这一层
  ③ hcaptcha.com/1/api.js                        ← 由 ② 自己加载
  ④ newassets.hcaptcha.com/.../hcaptcha.html     ← 由 ③ 自己加载
  ⑤ /getcaptcha/{sitekey}                        ← 由 ④ 自己发起

我们只补上第 ① 层该发的消息（sitekey + rqdata），其余全部真实。

用法：
    python solve_invisible.py --sitekey 24ed0064-... --rqdata 296...
    python solve_invisible.py --sitekey ... --rqdata ... --proxy http://... --json

作为库：
    from solve_invisible import solve
    token = await solve(sitekey, rqdata, proxy=..., profile=...)
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import uuid
from typing import Any, Dict, Optional

HCAPTCHA_INVISIBLE = (
    "https://b.stripecdn.com/stripethirdparty-srv/assets/v35.5/HCaptchaInvisible.html"
)
STRIPE_ORIGIN = "https://js.stripe.com"

# bundle 里 tag 是变量（不是字符串字面量），运行时逐个试
TAG_CANDIDATES = [0, 1, 2, 3, 4, 5, "render", "renderInvisible", "render_hcaptcha"]

TOKEN_JS = """
() => {
  window.__hc = window.__hc || { token: null, ready: null, replies: [] };
  if (!window.__hc.hooked) {
    window.__hc.hooked = true;
    window.addEventListener("message", (e) => {
      const d = e.data || {};
      if (d.type === "stripe-third-party-frame-ready") window.__hc.ready = d;
      if (d.type && String(d.type).indexOf("stripe-third-party-frame-") === 0
          && d.type !== "stripe-third-party-frame-ready") {
        window.__hc.replies.push(d);
      }
      try {
        const s = JSON.stringify(d);
        const m = s.match(/P1_[A-Za-z0-9_.\\-]{100,}/);
        if (m) window.__hc.token = m[0];
      } catch (_) {}
    });
  }
  return true;
}
"""

DISPATCH_JS = """
(cfg) => {
  window.__hc.replies.length = 0;
  window.dispatchEvent(new MessageEvent("message", {
    origin: "https://js.stripe.com",
    data: {
      type: "stripe-third-party-parent-to-child",
      frameID: cfg.frameID,
      requestID: "req-" + String(cfg.tag),
      payload: { tag: cfg.tag, message: { sitekey: cfg.sk, rqdata: cfg.rq } }
    }
  }));
  return true;
}
"""

STATE_JS = """
() => ({
  token: (window.__hc && window.__hc.token) || null,
  replies: (window.__hc && window.__hc.replies.length) || 0,
  rendered: !!document.querySelector('[id^="hcaptcha-invisible-"]'),
  hasHcaptcha: typeof hcaptcha !== "undefined" && !!hcaptcha.render,
})
"""


async def solve(
    sitekey: str,
    rqdata: str = "",
    *,
    proxy: str = "",
    profile: Optional[Dict[str, Any]] = None,
    host: str = "b.stripecdn.com",
    headless: bool = True,
    timeout_s: float = 90.0,
    chrome: str = "",
) -> Dict[str, Any]:
    """返回 {"token": str} 或 {"error": str}。"""
    from playwright.async_api import async_playwright

    prof = dict(profile or {})
    ua = prof.get("ua", "")
    if prof.get("build") and "{build}" in ua:
        ua = ua.format(build=prof["build"])
    screen = tuple(prof.get("screen", (1920, 1080, 1053)))
    locale = prof.get("locale", "en-US")
    timezone = prof.get("timezone", "America/New_York")
    frame_id = str(uuid.uuid4())

    url = f"{HCAPTCHA_INVISIBLE}?id={frame_id}&origin={STRIPE_ORIGIN.replace(':', '%3A').replace('/', '%2F')}"

    launch_kwargs: Dict[str, Any] = {
        "headless": headless,
        "args": ["--no-sandbox", "--disable-dev-shm-usage"],
    }
    if chrome:
        launch_kwargs["executable_path"] = chrome

    proxy_cfg = None
    if proxy:
        proxy_cfg = {"server": proxy}

    async with async_playwright() as p:
        browser = await p.chromium.launch(**launch_kwargs)
        try:
            ctx = await browser.new_context(
                proxy=proxy_cfg,
                user_agent=ua or None,
                locale=locale,
                timezone_id=timezone,
                viewport={"width": screen[0], "height": screen[1]},
                screen={"width": screen[0], "height": screen[1]},
            )
            page = await ctx.new_page()
            await page.add_init_script(
                "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});"
            )

            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=45000)
            except Exception as exc:  # noqa: BLE001
                return {"error": f"page_load_failed: {str(exc)[:120]}"}

            # 等 bundle 把 hcaptcha 准备好（它自己会加载 api.js）
            ready = False
            for _ in range(150):
                try:
                    if await page.evaluate(
                        "() => typeof hcaptcha !== 'undefined' && !!hcaptcha.render"
                    ):
                        ready = True
                        break
                except Exception:
                    pass
                await page.wait_for_timeout(200)
            if not ready:
                return {"error": "hcaptcha_not_ready"}

            await page.evaluate(TOKEN_JS)
            await page.wait_for_timeout(300)

            ready_msg = await page.evaluate("() => (window.__hc && window.__hc.ready) || null")
            real_id = (ready_msg or {}).get("frameID") or frame_id

            tried = []
            for tag in TAG_CANDIDATES:
                await page.evaluate(
                    DISPATCH_JS,
                    {"frameID": real_id, "tag": tag, "sk": sitekey, "rq": rqdata},
                )
                st = {"rendered": False, "replies": 0, "token": None}
                for _ in range(20):
                    await page.wait_for_timeout(300)
                    st = await page.evaluate(STATE_JS)
                    if st.get("token"):
                        return {"token": st["token"], "tag": tag, "frameID": real_id}
                    if st.get("rendered") or st.get("replies"):
                        break
                tried.append({"tag": tag, "rendered": st.get("rendered"),
                              "replies": st.get("replies")})
                if not (st.get("rendered") or st.get("replies")):
                    continue
                # 命中：继续等 token
                for _ in range(50):
                    await page.wait_for_timeout(300)
                    st = await page.evaluate(STATE_JS)
                    if st.get("token"):
                        return {"token": st["token"], "tag": tag, "frameID": real_id}
                break

            return {"error": "no_token", "tried": tried, "frameID": real_id}
        finally:
            await browser.close()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sitekey", required=True)
    ap.add_argument("--rqdata", default="")
    ap.add_argument("--proxy", default="")
    ap.add_argument("--chrome", default="/usr/bin/google-chrome")
    ap.add_argument("--headful", action="store_true")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()

    out = asyncio.run(
        solve(
            a.sitekey, a.rqdata,
            proxy=a.proxy,
            headless=not a.headful,
            chrome=a.chrome,
        )
    )
    if a.json:
        print(json.dumps(out, ensure_ascii=False)[:800])
    elif out.get("token"):
        t = out["token"]
        print(f"TOKEN ok len={len(t)} tag={out.get('tag')}")
        print(t[:80] + "...")
    else:
        print("FAILED:", json.dumps(out, ensure_ascii=False)[:600])
    sys.exit(0 if out.get("token") else 1)


if __name__ == "__main__":
    main()
