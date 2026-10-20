"""在本地桥接页里完成 Stripe passive hCaptcha，取回 token。

用法：
    python solve.py --site-key KEY --rqdata RQ --merchant-id MID [--proxy URL]
输出：JSON 到 stdout 和 /tmp/hcap_bridge_result.json
"""
from __future__ import annotations
import argparse
import json
import os
import socketserver
import http.server
import tempfile
import threading
import time
import uuid
from pathlib import Path

from bridge_page import build_frame_url, build_parent_html


class State:
    def __init__(self):
        self.events = []
        self.result = None
        self.error = None
        self.ready = threading.Event()
        self.failed = threading.Event()


def make_handler(tmpdir: str, st: State):
    class H(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *a, **kw):
            super().__init__(*a, directory=tmpdir, **kw)
        def log_message(self, *a): return
        def _json(self, code, obj):
            b = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(b)))
            self.end_headers()
            self.wfile.write(b)
        def do_POST(self):
            try:
                n = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                n = 0
            raw = self.rfile.read(n) if n else b"{}"
            try: payload = json.loads(raw.decode() or "{}")
            except Exception: payload = {}
            if self.path == "/event":
                st.events.append(payload); self._json(200, {"ok": True}); return
            if self.path == "/result":
                st.result = payload; st.ready.set(); self._json(200, {"ok": True}); return
            if self.path == "/error":
                st.error = payload; st.failed.set(); self._json(200, {"ok": True}); return
            self._json(404, {"error": "nf"})
    return H


class Threaded(socketserver.ThreadingMixIn, socketserver.TCPServer):
    allow_reuse_address = True
    daemon_threads = True


def run(site_key: str, rqdata: str, merchant_id: str, locale: str,
        proxy: str | None, timeout: float, invisible: bool) -> dict:
    st = State()
    with tempfile.TemporaryDirectory(prefix="hcap-bridge-") as tmp:
        httpd = Threaded(("127.0.0.1", 0), make_handler(tmp, st))
        port = httpd.server_address[1]
        origin = f"http://127.0.0.1:{port}"
        fid = str(uuid.uuid4())
        frame_url = build_frame_url(fid, origin, invisible=invisible)
        html = build_parent_html(frame_id=fid, frame_url=frame_url, site_key=site_key,
                                 rqdata=rqdata, merchant_id=merchant_id,
                                 locale=locale, invisible=invisible)
        Path(tmp, "index.html").write_text(html, encoding="utf-8")
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        bridge_url = f"{origin}/index.html"
        print(f"[bridge] {bridge_url}", flush=True)
        print(f"[frame ] {frame_url[:110]}", flush=True)

        from playwright.sync_api import sync_playwright
        with sync_playwright() as pw:
            launch = dict(headless=True,
                          args=["--no-sandbox", "--disable-dev-shm-usage",
                                "--disable-gpu", "--disable-blink-features=AutomationControlled"])
            if proxy:
                launch["proxy"] = {"server": proxy, "bypass": "127.0.0.1,localhost"}
            try:
                browser = pw.chromium.launch(**launch)
            except Exception as e:
                print("[warn] chromium 启动失败，用 firefox:", str(e)[:80], flush=True)
                browser = pw.firefox.launch(headless=True)
            page = browser.new_page(viewport={"width": 900, "height": 900})
            page.goto(bridge_url, wait_until="domcontentloaded", timeout=60000)
            deadline = time.time() + timeout
            while time.time() < deadline:
                if st.ready.is_set() or st.failed.is_set():
                    break
                page.wait_for_timeout(500)
            try:
                status = page.locator("#status").inner_text()
            except Exception:
                status = ""
            browser.close()
        httpd.shutdown()

        out = {"ok": False, "status": status, "events": len(st.events)}
        if st.result:
            out.update({"ok": True, "token": st.result.get("response", ""),
                        "ekey": st.result.get("ekey", ""),
                        "duration": st.result.get("duration", 0)})
        elif st.error:
            out["error"] = st.error
        else:
            out["events_tail"] = st.events[-6:]
        return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--site-key", required=True)
    ap.add_argument("--rqdata", default="")
    ap.add_argument("--merchant-id", default="")
    ap.add_argument("--locale", default="en-US")
    ap.add_argument("--proxy", default=os.environ.get("ORDER_PROXY"))
    ap.add_argument("--timeout", type=float, default=60)
    ap.add_argument("--visible", action="store_true")
    ap.add_argument("--out", default="/tmp/hcap_bridge_result.json")
    a = ap.parse_args()
    r = run(a.site_key, a.rqdata, a.merchant_id, a.locale, a.proxy,
            a.timeout, not a.visible)
    Path(a.out).write_text(json.dumps(r, ensure_ascii=False, indent=2))
    print(json.dumps(r, ensure_ascii=False, indent=2)[:800])
    print("saved ->", a.out)


if __name__ == "__main__":
    main()
