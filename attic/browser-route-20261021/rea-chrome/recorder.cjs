#!/usr/bin/env node
/**
 * 逆向记录器：连上已开的调试 Chrome，全程记录并落盘。
 *
 * 反调试对策：
 *   · 用 Page.addScriptToEvaluateOnNewDocument 在每个文档最早期注入，
 *     早于 hCaptcha/Stripe 的任何脚本，保证 hook 不被覆盖
 *   · 保留原始函数引用，hook 后仍调用原实现，不破坏流程
 *   · debugger 语句屏蔽：拦截 Function 构造与 eval，把 debugger 剔除
 *   · console/methods 只旁路记录，不改返回值
 *
 * 记录内容：
 *   · 所有 stripe/hcaptcha 请求 + 响应体
 *   · hsw / checksiteconfig / getcaptcha 的入参与返回值
 *   · hCaptcha 内部 manifest / motionData / pem
 *   · 任何 confirm / verify_challenge 的请求体与响应
 *
 * 用法：
 *   node tools/rea-chrome/recorder.cjs start [--filter=stripe|hcaptcha]
 *   node tools/rea-chrome/recorder.cjs stop
 *   node tools/rea-chrome/recorder.cjs dump   # 打印已记录摘要
 */
'use strict';

const fs = require('node:fs');
const path = require('node:path');

const PORT = Number(process.env.CDP_PORT || 9222);
const BASE = `http://127.0.0.1:${PORT}`;
const OUT_DIR = process.env.REC_OUT || path.join(process.cwd(), 'runtime', 'rea-capture');
const OUT = path.join(OUT_DIR, 'capture.jsonl');
const STATE = path.join(OUT_DIR, 'state.json');

function arg(name, dflt = '') {
  const hit = process.argv.find((a) => a.startsWith(`--${name}=`));
  return hit ? hit.slice(name.length + 3) : dflt;
}
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function pickPage() {
  const all = await (await fetch(`${BASE}/json/list`)).json();
  const pages = all.filter((t) => t.type === 'page');
  const want = arg('target');
  if (want) return pages.find((t) => (t.url || '').includes(want)) || pages[0];
  // 优先 <YOUR_ORIGIN> / pay.openai.com
  return pages.find((t) => /pay\.(<PAY_HOST>\.shop|openai\.com)/.test(t.url || '')) || pages[0];
}

class CDP {
  constructor(url) { this.url = url; this.id = 0; this.pending = new Map(); this.handlers = new Map(); }
  connect() {
    return new Promise((resolve, reject) => {
      this.ws = new WebSocket(this.url);
      this.ws.addEventListener('open', () => resolve(this));
      this.ws.addEventListener('error', () => reject(new Error('ws error')));
      this.ws.addEventListener('message', (ev) => {
        let m; try { m = JSON.parse(ev.data); } catch (_) { return; }
        if (m.id !== undefined) {
          const p = this.pending.get(m.id);
          if (p) { this.pending.delete(m.id); m.error ? p.reject(new Error(JSON.stringify(m.error))) : p.resolve(m.result); }
          return;
        }
        for (const h of this.handlers.get(m.method) || []) { try { h(m.params); } catch (_) {} }
      });
    });
  }
  send(method, params = {}) {
    const id = ++this.id;
    return new Promise((resolve, reject) => {
      this.pending.set(id, { resolve, reject });
      this.ws.send(JSON.stringify({ id, method, params }));
      setTimeout(() => { if (this.pending.delete(id)) reject(new Error(`timeout ${method}`)); }, 30000);
    });
  }
  on(m, h) { if (!this.handlers.has(m)) this.handlers.set(m, []); this.handlers.get(m).push(h); }
  close() { try { this.ws.close(); } catch (_) {} }
}

// ── 注入到每个新文档的 hook 脚本（越早越好）────────────────
const HOOK = String.raw`
(() => {
  if (window.__rea_hooked) return;
  window.__rea_hooked = true;
  const LOG = [];
  window.__rea_log = LOG;
  const push = (kind, data) => {
    try { LOG.push({ t: Date.now(), kind, data }); } catch (_) {}
    // 同步给 CDP 侧
    try { window.__rea_cdp && window.__rea_cdp(JSON.stringify({ kind, data: String(data).slice(0, 200000) })); } catch (_) {}
  };
  const safeStr = (v) => {
    try {
      if (typeof v === 'string') return v;
      if (v instanceof Uint8Array) return 'Uint8Array[' + v.length + '] ' + Array.from(v.slice(0,32)).join(',');
      const s = JSON.stringify(v);
      return (s || '').slice(0, 200000);
    } catch (_) { return String(v).slice(0, 2000); }
  };

  // 1) 屏蔽 debugger：包住 Function 构造与 eval
  try {
    const origFunction = window.Function;
    const stripDebugger = (src) => String(src).replace(/\bdebugger\s*;?/g, '');
    window.Function = function (...args) {
      if (args.length) args[args.length - 1] = stripDebugger(args[args.length - 1]);
      return origFunction.apply(this, args);
    };
    window.Function.prototype = origFunction.prototype;
    const origEval = window.eval;
    window.eval = function (src) { return origEval.call(this, stripDebugger(src)); };
    push('hook', 'debugger-stripped');
  } catch (e) { push('hook-error', 'debugger: ' + e); }

  // 2) 记录 fetch
  try {
    const origFetch = window.fetch;
    window.fetch = function (input, init) {
      const url = typeof input === 'string' ? input : (input && input.url) || '';
      const method = (init && init.method) || (input && input.method) || 'GET';
      const body = init && init.body;
      const bodyStr = (typeof body === 'string') ? body : (body ? safeStr(body) : '');
      if (/stripe|hcaptcha/i.test(url)) push('fetch-req', { url, method, body: bodyStr.slice(0, 100000) });
      const p = origFetch.apply(this, arguments);
      if (/stripe|hcaptcha/i.test(url)) {
        p.then((res) => {
          const clone = res.clone();
          clone.text().then((txt) => push('fetch-res', { url, status: res.status, body: txt.slice(0, 200000) })).catch(() => {});
        }).catch(() => {});
      }
      return p;
    };
  } catch (e) { push('hook-error', 'fetch: ' + e); }

  // 3) 记录 XHR
  try {
    const XO = window.XMLHttpRequest.prototype.open;
    const XS = window.XMLHttpRequest.prototype.send;
    window.XMLHttpRequest.prototype.open = function (m, u) {
      this.__rea = { m, u };
      return XO.apply(this, arguments);
    };
    window.XMLHttpRequest.prototype.send = function (body) {
      const info = this.__rea || {};
      if (/stripe|hcaptcha/i.test(info.u || '')) {
        push('xhr-req', { url: info.u, method: info.m, body: body ? safeStr(body).slice(0, 100000) : '' });
        this.addEventListener('load', () => {
          push('xhr-res', { url: info.u, status: this.status, body: String(this.responseText || '').slice(0, 200000) });
        });
      }
      return XS.apply(this, arguments);
    };
  } catch (e) { push('hook-error', 'xhr: ' + e); }

  // 4) 记录 postMessage（hCaptcha iframe 通信）
  try {
    const origPM = window.postMessage;
    window.postMessage = function (msg, origin, ...rest) {
      try {
        const s = typeof msg === 'string' ? msg : safeStr(msg);
        if (/hcaptcha|manifest|motion|token|execute|getcaptcha/i.test(s)) {
          push('postMessage', { origin, msg: s.slice(0, 20000) });
        }
      } catch (_) {}
      return origPM.apply(this, arguments);
    };
  } catch (e) { push('hook-error', 'postMessage: ' + e); }

  // 5) hcaptcha API 包装（每次出现都重新包）
  let wrappedIds = new WeakSet();
  const wrapHcaptcha = () => {
    const hc = window.hcaptcha;
    if (!hc || wrappedIds.has(hc)) return;
    wrappedIds.add(hc);
    for (const name of ['execute', 'render', 'getRespKey', 'reset']) {
      const orig = hc[name];
      if (typeof orig !== 'function') continue;
      hc[name] = function (...args) {
        push('hcaptcha-' + name, 'args=' + safeStr(args).slice(0, 5000));
        try {
          const out = orig.apply(this, args);
          if (out && typeof out.then === 'function') {
            out.then((v) => push('hcaptcha-' + name + '-ok', safeStr(v).slice(0, 30000)),
                     (e) => push('hcaptcha-' + name + '-err', String(e).slice(0, 2000)));
          } else {
            push('hcaptcha-' + name + '-sync', safeStr(out).slice(0, 5000));
          }
          return out;
        } catch (e) { push('hcaptcha-' + name + '-throw', String(e).slice(0, 2000)); throw e; }
      };
    }
    push('hook', 'hcaptcha wrapped');
  };
  setInterval(wrapHcaptcha, 300);
  setTimeout(wrapHcaptcha, 50);

  push('hook', 'installed on ' + location.href);
})();
`;

const INJECT_ID = 'rea-hook-v1';

async function start() {
  fs.mkdirSync(OUT_DIR, { recursive: true });
  const page = await pickPage();
  if (!page) { console.error('没有可调试页面'); process.exit(1); }
  console.log('目标:', page.url.slice(0, 100));

  const cdp = await new CDP(page.webSocketDebuggerUrl).connect();
  await cdp.send('Page.enable');
  await cdp.send('Runtime.enable');
  await cdp.send('Network.enable', { maxTotalBufferSize: 500 * 1024 * 1024 });

  // 注入 hook（对之后所有新文档生效）
  const { identifier } = await cdp.send('Page.addScriptToEvaluateOnNewDocument', {
    source: HOOK, runImmediately: true,
  });
  console.log('hook 注入 id:', identifier);

  // 当前页面也补一次
  await cdp.send('Runtime.evaluate', { expression: HOOK, includeCommandLineAPI: true });

  // CDP 侧收 hook 日志
  await cdp.send('Runtime.addBinding', { name: '__rea_cdp' });
  cdp.on('Runtime.bindingCalled', (p) => {
    if (p.name !== '__rea_cdp') return;
    fs.appendFileSync(OUT, p.payload + '\n');
    try {
      const j = JSON.parse(p.payload);
      const k = j.kind || '';
      if (/hcaptcha|getcaptcha|checksiteconfig|manifest|verify/.test(k) || /hcaptcha/.test(String(j.data).slice(0, 200))) {
        console.log('[HOOK]', k, String(j.data).slice(0, 180).replace(/\n/g, ' '));
      }
    } catch (_) {}
  });

  // 网络记录
  const reqs = new Map();
  const filter = arg('filter');
  cdp.on('Network.requestWillBeSent', (p) => {
    const u = p.request?.url || '';
    reqs.set(p.requestId, { url: u, method: p.request?.method, postData: p.request?.postData, t: Date.now() });
    if (/stripe|hcaptcha/i.test(u) && (!filter || u.includes(filter))) {
      console.log('[REQ]', p.request?.method, u.slice(0, 140));
    }
  });
  cdp.on('Network.responseReceived', async (p) => {
    const u = p.response?.url || '';
    if (!/stripe|hcaptcha/i.test(u)) return;
    if (filter && !u.includes(filter)) return;
    const rec = reqs.get(p.requestId) || {};
    let body = '';
    try {
      const b = await cdp.send('Network.getResponseBody', { requestId: p.requestId });
      body = b.base64Encoded ? Buffer.from(b.body, 'base64').toString('utf8') : b.body;
    } catch (_) {}
    fs.appendFileSync(OUT, JSON.stringify({
      t: Date.now(), kind: 'net', url: u, status: p.response?.status,
      method: rec.method, reqBody: (rec.postData || '').slice(0, 100000),
      resBody: body.slice(0, 200000),
    }) + '\n');
    console.log('[RES]', p.response?.status, u.slice(0, 120), `(${body.length}B)`);
  });

  fs.writeFileSync(STATE, JSON.stringify({ pageUrl: page.url, out: OUT, startedAt: Date.now() }));
  console.log('\n记录中 →', OUT);
  console.log('现在去操作页面，我这边实时记录。Ctrl+C 停止。\n');

  // 保持连接
  await new Promise(() => {});
}

function dump() {
  if (!fs.existsSync(OUT)) { console.log('还没有记录'); return; }
  const lines = fs.readFileSync(OUT, 'utf8').trim().split('\n').filter(Boolean);
  const kinds = {};
  for (const ln of lines) {
    try { const j = JSON.parse(ln); kinds[j.kind] = (kinds[j.kind] || 0) + 1; } catch (_) {}
  }
  console.log('总条数:', lines.length);
  console.log('分类:', JSON.stringify(kinds, null, 2));
}

async function main() {
  const cmd = process.argv[2] || 'start';
  if (cmd === 'start') return start();
  if (cmd === 'dump') return dump();
  if (cmd === 'stop') { try { fs.unlinkSync(STATE); } catch (_) {} console.log('已停止标记'); return; }
  console.error('用法: recorder.cjs start|dump|stop');
}
main().catch((e) => { console.error('ERR', String(e).slice(0, 400)); process.exit(1); });
