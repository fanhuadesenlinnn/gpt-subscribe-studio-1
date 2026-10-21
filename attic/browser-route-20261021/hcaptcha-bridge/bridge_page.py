"""生成 Stripe hCaptcha 桥接页。

核心机制（关键）：
  Stripe 的 HCaptchaInvisible.html 接受 ?origin= 参数。
  把 origin 声明成本地服务地址，iframe 就认可这个父窗口，
  从而可以正常完成 postMessage 握手并产出 passive token。

不需要 GPU，不需要完整结账页。
"""
from __future__ import annotations
import json
import urllib.parse
import uuid

ASSET_VERSION = "v32.5"          # 与 Stripe 当前 checkout 对齐
CDN = "https://b.stripecdn.com/stripethirdparty-srv/assets"


def build_frame_url(frame_id: str, origin: str, invisible: bool = True) -> str:
    page = "HCaptchaInvisible.html" if invisible else "HCaptcha.html"
    return (
        f"{CDN}/{ASSET_VERSION}/{page}"
        f"?id={frame_id}&origin={urllib.parse.quote(origin, safe='')}"
    )


def build_parent_html(*, frame_id: str, frame_url: str, site_key: str,
                      rqdata: str, merchant_id: str, locale: str = "en-US",
                      invisible: bool = True) -> str:
    visible_payload = {
        "sitekey": site_key, "rqdata": rqdata,
        "merchantId": merchant_id, "locale": locale,
        "headerText": "Verification required",
        "instructionText": "Complete captcha to continue",
        "showCloseButton": False,
    }
    init_payload = {"tag": "INITIALIZE_HCAPTCHA_INVISIBLE",
                    "message": {"sitekey": site_key}}
    exec_payload = {"tag": "EXECUTE_HCAPTCHA_INVISIBLE",
                    "message": {"sitekey": site_key, "rqdata": rqdata,
                                "data": {"merchant_id": merchant_id or "",
                                         "locale": locale or "",
                                         "flow": "passive_captcha",
                                         "captcha_vendor": "hcaptcha"}}}
    signals = [
        {"tag": "SEND_FRAUD_SIGNALS_HCAPTCHA_INVISIBLE",
         "message": {"type": "mouse", "eventName": "mousemove",
                     "coordinates": {"x": 168, "y": 132}}},
        {"tag": "SEND_FRAUD_SIGNALS_HCAPTCHA_INVISIBLE",
         "message": {"type": "pointer", "eventName": "pointermove",
                     "coordinates": {"x": 214, "y": 176}}},
        {"tag": "SEND_FRAUD_SIGNALS_HCAPTCHA_INVISIBLE",
         "message": {"type": "keyboard", "eventName": "keydown"}},
    ]
    return f"""<!doctype html>
<html><head><meta charset="utf-8"><title>Stripe hCaptcha Bridge</title>
<style>body{{margin:0;font-family:Arial;background:#f7f7f7}}
.shell{{padding:16px}}#status{{font-size:14px;margin-bottom:12px;color:#333}}
iframe{{width:420px;height:720px;border:0;background:#fff}}
pre{{white-space:pre-wrap;word-break:break-word;font-size:12px}}</style></head>
<body><div class="shell">
<div id="status">Waiting for Stripe captcha frame…</div>
<iframe id="f" src="{frame_url}"></iframe>
<pre id="result"></pre></div>
<script>
const frameID = {json.dumps(frame_id)};
const invisibleMode = {json.dumps(bool(invisible))};
const childPayload = {json.dumps(visible_payload)};
const initPayload = {json.dumps(init_payload)};
const execPayload = {json.dumps(exec_payload)};
const signals = {json.dumps(signals)};
let inited = false, executed = false;
const $ = id => document.getElementById(id);
const setStatus = t => {{ $("status").textContent = t; }};
function toBridge(path, payload) {{
  fetch(path, {{method:"POST",headers:{{"Content-Type":"application/json"}},
    body: JSON.stringify(payload||{{}}), keepalive:true}}).catch(()=>{{}});
}}
function toChild(win, origin, payload) {{
  try {{ win.postMessage({{type:"stripe-third-party-parent-to-child", frameID,
    payload}}, origin || "*"); }} catch(e) {{}}
}}
function doInit(win, origin) {{
  if (inited) return; inited = true;
  setStatus("Initializing invisible hCaptcha…");
  toBridge("/event", {{type:"invisible_init"}});
  toChild(win, origin, initPayload);
}}
function doExec(win, origin) {{
  if (executed) return; executed = true;
  setStatus("Executing invisible hCaptcha…");
  signals.forEach((s,i) => setTimeout(()=>toChild(win, origin, s), 50*i));
  setTimeout(()=>{{ toBridge("/event", {{type:"invisible_execute"}});
    toChild(win, origin, execPayload); }}, 180);
}}
window.addEventListener("message", ev => {{
  const d = ev.data || {{}};
  toBridge("/event", {{type:"raw", origin: ev.origin, data: d}});
  if (d.type === "stripe-third-party-frame-ready" && d.frameID === frameID) {{
    setStatus("Frame ready.");
    if (invisibleMode) doInit(ev.source, ev.origin);
    else toChild(ev.source, ev.origin, childPayload);
    return;
  }}
  if (d.type !== "stripe-third-party-child-to-parent" || d.frameID !== frameID) return;
  const p = d.payload || {{}};
  $("result").textContent = JSON.stringify(p, null, 2);
  toBridge("/event", {{type:"child_payload", payload:p}});
  if (!invisibleMode) {{
    if (p.type === "response") {{ window.__result = p; toBridge("/result", p); }}
    return;
  }}
  const tag = p.tag || "", v = p.value || {{}};
  if (tag === "LOAD_HCAPTCHA_INVISIBLE") {{ doExec(ev.source, ev.origin); return; }}
  if (tag === "RESPONSE_HCAPTCHA_INVISIBLE") {{
    const solved = {{response: v.response||"", ekey: v.key||"",
      duration: v.duration||0, raw: p}};
    setStatus("Solved.");
    window.__result = solved;
    toBridge("/result", solved);
    return;
  }}
  if (tag === "ERROR_HCAPTCHA_INVISIBLE") {{
    setStatus("Failed: " + (v.error||"unknown"));
    toBridge("/error", {{error: v.error||"unknown", raw:p}});
    return;
  }}
}});
</script></body></html>"""
