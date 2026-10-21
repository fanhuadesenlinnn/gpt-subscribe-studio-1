#!/usr/bin/env node
/**
 * 全量记录器 v2：把 OOPIF（跨进程 iframe）里的网络与 console 也一起记录。
 *
 * v1 的缺陷：只在主 target 上 Network.enable，js.stripe.com /
 * b.stripecdn.com / newassets.hcaptcha.com 全是独立进程 iframe，
 * 它们的 checksiteconfig / getcaptcha / verify_challenge 全都没抓到。
 *
 * v2 做法：
 *   · Target.setAutoAttach(flatten) 递归下钻，每层都 Network.enable + Runtime.enable
 *   · 每个 session 独立收 Network.responseReceived，用 Network.getResponseBody 取体
 *   · 在每层注入 hook（debugger 剔除 + fetch/XHR 记录 + hcaptcha 包装）
 *   · 抓 verify_challenge / confirm 的完整请求体
 *
 * 用法：
 *   node tools/rea-chrome/recorder2.cjs            # 跑起来，然后你去操作
 *   node tools/rea-chrome/recorder2.cjs --summary  # 看已记录的分类统计
 */
'use strict';

const fs = require('node:fs');
const path = require('node:path');

const PORT = Number(process.env.CDP_PORT || 9222);
const OUT_DIR = process.env.REC_OUT || path.join(process.cwd(), 'runtime', 'rea-capture');
const OUT = path.join(OUT_DIR, 'capture2.jsonl');
const HITS = path.join(OUT_DIR, 'hits.jsonl');

const WANT = /hcaptcha|stripe|stripecdn|pay\.openai/i;
const KEY = /checksiteconfig|getcaptcha|verify_challenge|payment_pages|payment_methods|verify|confirm/i;

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const arg = (n, d = '') => {
  const h = process.argv.find((a) => a.startsWith(`--${n}=`));
  return h ? h.slice(n.length + 3) : d;
};

function log(...a) {
  const line = a.join(' ');
  console.log(line);
  try { fs.appendFileSync(path.join(OUT_DIR, 'recorder2.log'), line + '\n'); } catch (_) {}
}

function save(obj) {
  try { fs.appendFileSync(OUT, JSON.stringify(obj) + '\n'); } catch (_) {}
  const u = String(obj.url || '');
  if (KEY.test(u)) {
    try { fs.appendFileSync(HITS, JSON.stringify(obj) + '\n'); } catch (_) {}
    log('  ★', obj.status || '', u.slice(0, 120));
  }
}

const HOOK = String.raw`
(() => {
  if (window.__rea2) return;
  window.__rea2 = 1;
  window.__rea_log2 = [];
  const push = (k, d) => {
    try { window.__rea_log2.push({t: Date.now(), k, d: String(d).slice(0, 50000)}); } catch(_) {}
    try { window.__rea_cdp2 && window.__rea_cdp2(k + '|' + String(d).slice(0, 20000)); } catch(_) {}
  };
  // debugger 剔除
  try {
    const OF = window.Function;
    const strip = (s) => String(s).replace(/\bdebugger\s*;?/g, '');
    window.Function = function (...a) { if (a.length) a[a.length-1] = strip(a[a.length-1]); return OF.apply(this, a); };
    window.Function.prototype = OF.prototype;
    const OE = window.eval;
    window.eval = function (s) { return OE.call(this, strip(s)); };
  } catch(e){}
  // fetch
  try {
    const of = window.fetch;
    window.fetch = function (input, init) {
      const url = typeof input === 'string' ? input : (input && input.url) || '';
      const body = init && init.body;
      if (/hcaptcha|stripe/i.test(url)) push('fetch-req', url + ' :: ' + (typeof body === 'string' ? body.slice(0,3000) : ''));
      const p = of.apply(this, arguments);
      if (/hcaptcha|stripe/i.test(url)) {
        p.then(r => { const c = r.clone(); c.text().then(t => push('fetch-res', url + ' :: ' + t.slice(0,20000))).catch(()=>{}); }).catch(()=>{});
      }
      return p;
    };
  } catch(e){}
  // XHR
  try {
    const XO = XMLHttpRequest.prototype.open, XS = XMLHttpRequest.prototype.send;
    XMLHttpRequest.prototype.open = function (m, u) { this.__r2 = {m, u}; return XO.apply(this, arguments); };
    XMLHttpRequest.prototype.send = function (b) {
      const i = this.__r2 || {};
      if (/hcaptcha|stripe/i.test(i.u || '')) {
        push('xhr-req', i.u + ' :: ' + (b ? String(b).slice(0,3000) : ''));
        this.addEventListener('load', () => push('xhr-res', i.u + ' :: ' + String(this.responseText||'').slice(0,20000)));
      }
      return XS.apply(this, arguments);
    };
  } catch(e){}
  // postMessage
  try {
    const op = window.postMessage;
    window.postMessage = function (msg, ...rest) {
      try {
        const s = typeof msg === 'string' ? msg : JSON.stringify(msg);
        if (/hcaptcha|manifest|token|execute|getcaptcha|challenge|verify/i.test(s)) push('pm', s.slice(0,20000));
      } catch(_){}
      return op.apply(this, arguments);
    };
  } catch(e){}
  // hcaptcha 包装
  let wrapped = new WeakSet();
  const wrap = () => {
    const hc = window.hcaptcha;
    if (!hc || wrapped.has(hc)) return;
    wrapped.add(hc);
    for (const n of ['execute','render','getRespKey','reset','setData','getPassiveToken']) {
      const o = hc[n]; if (typeof o !== 'function') continue;
      hc[n] = function (...a) {
        push('hc-'+n+'-call', JSON.stringify(a).slice(0,2000));
        try {
          const r = o.apply(this, a);
          if (r && typeof r.then === 'function') {
            r.then(v => push('hc-'+n+'-ok', JSON.stringify(v).slice(0,30000)),
                   e => push('hc-'+n+'-err', String(e).slice(0,2000)));
          } else push('hc-'+n+'-sync', JSON.stringify(r).slice(0,5000));
          return r;
        } catch(e){ push('hc-'+n+'-throw', String(e).slice(0,2000)); throw e; }
      };
    }
  };
  setInterval(wrap, 250); setTimeout(wrap, 30);
  // 兜底：任何拿到的 P1_ token 都存下来
  setInterval(() => {
    try {
      for (const k of Object.keys(window)) {
        const v = window[k];
        if (typeof v === 'string' && v.startsWith('P1_') && v.length > 200) push('TOKEN', k + '=' + v);
      }
    } catch(_){}
  }, 500);
  push('hook', 'installed ' + location.href.slice(0,120));
})();
`;

(async () => {
  fs.mkdirSync(OUT_DIR, { recursive: true });
  const cmd = process.argv[2] || 'run';
  if (cmd === '--summary' || cmd === 'summary') {
    if (!fs.existsSync(OUT)) { console.log('还没记录'); return; }
    const lines = fs.readFileSync(OUT, 'utf8').trim().split('\n').filter(Boolean);
    const byUrl = {};
    let hookKinds = {};
    for (const ln of lines) {
      try {
        const j = JSON.parse(ln);
        if (j.type === 'hook') { hookKinds[j.kind] = (hookKinds[j.kind] || 0) + 1; continue; }
        if (j.url) {
          const k = String(j.url).split('?')[0].slice(0, 90);
          byUrl[k] = (byUrl[k] || 0) + 1;
        }
      } catch (_) {}
    }
    console.log('网络条数:', Object.values(byUrl).reduce((a, b) => a + b, 0));
    console.log('\n=== 关键端点 ===');
    for (const [u, n] of Object.entries(byUrl).filter(([u]) => KEY.test(u))) console.log(n, u);
    console.log('\n=== hook 事件 ===');
    console.log(JSON.stringify(hookKinds, null, 1));
    return;
  }

  const list = await (await fetch(`http://127.0.0.1:${PORT}/json/list`)).json();
  const want = arg('target', 'pay.openai.com');
  const t = list.find((x) => x.type === 'page' && (x.url || '').includes(want)) || list.find((x) => x.type === 'page');
  if (!t) { console.error('没有可调试页面'); process.exit(1); }
  log('目标:', (t.url || '').slice(0, 110));

  const ws = new WebSocket(t.webSocketDebuggerUrl);
  let id = 0;
  const pend = new Map();
  const sessions = new Map();

  ws.addEventListener('message', async (ev) => {
    let m; try { m = JSON.parse(ev.data); } catch (_) { return; }
    if (m.id !== undefined && pend.has(m.id)) {
      const p = pend.get(m.id); pend.delete(m.id);
      m.error ? p.rej(new Error(JSON.stringify(m.error))) : p.ok(m.result);
      return;
    }
    if (m.method === 'Target.attachedToTarget') {
      const s = m.params.sessionId;
      const info = m.params.targetInfo || {};
      sessions.set(s, info);
      if (WANT.test(info.url || '') || info.type === 'iframe') {
        log('attach:', (info.url || '').slice(0, 100));
      }
      // 每个 session 都开 auto-backup（递归下钻）
      try {
        await send('Target.setAutoAttach', { autoAttach: true, waitForDebuggerOnStart: false, flatten: true }, s);
        await send('Runtime.enable', {}, s);
        await send('Network.enable', { maxTotalBufferSize: 200 * 1024 * 1024 }, s);
        await send('Runtime.addBinding', { name: '__rea_cdp2' }, s);
        await send('Page.addScriptToEvaluateOnNewDocument', { source: HOOK, runImmediately: true }, s).catch(() => {});
        await send('Runtime.evaluate', { expression: HOOK }, s).catch(() => {});
      } catch (_) {}
      return;
    }
    if (m.method === 'Runtime.bindingCalled' && m.params?.name === '__rea_cdp2') {
      const [kind, ...rest] = String(m.params.payload || '').split('|');
      const data = rest.join('|');
      save({ type: 'hook', kind, data, sid: (m.sessionId || '').slice(0, 8) });
      return;
    }
    const sid = (m.sessionId || '').slice(0, 8);
    if (m.method === 'Network.responseReceived') {
      const p = m.params;
      const u = p.response?.url || '';
      if (!WANT.test(u)) return;
      let body = '';
      try {
        const b = await send('Network.getResponseBody', { requestId: p.requestId }, m.sessionId);
        body = b.base64Encoded ? Buffer.from(b.body, 'base64').toString('utf8') : b.body;
      } catch (_) {}
      save({ type: 'net', sid, url: u, status: p.response?.status,
             mime: p.response?.mimeType, resBody: body.slice(0, 300000) });
      return;
    }
    if (m.method === 'Network.requestWillBeSent') {
      const p = m.params;
      const u = p.request?.url || '';
      if (!WANT.test(u)) return;
      save({ type: 'req', sid, url: u, method: p.request?.method,
             reqBody: (p.request?.postData || '').slice(0, 300000) });
    }
  });

  const send = (method, params = {}, sessionId) => new Promise((ok, rej) => {
    const i = ++id; pend.set(i, { ok, rej });
    const msg = { id: i, method, params };
    if (sessionId) msg.sessionId = sessionId;
    ws.send(JSON.stringify(msg));
    setTimeout(() => { if (pend.delete(i)) rej(new Error('timeout ' + method)); }, 30000);
  });

  await new Promise((r) => ws.addEventListener('open', r));
  await send('Page.enable');
  await send('Runtime.enable');
  await send('Network.enable', { maxTotalBufferSize: 200 * 1024 * 1024 });
  await send('Runtime.addBinding', { name: '__rea_cdp2' });
  await send('Page.addScriptToEvaluateOnNewDocument', { source: HOOK, runImmediately: true });
  await send('Runtime.evaluate', { expression: HOOK });
  await send('Target.setAutoAttach', { autoAttach: true, waitForDebuggerOnStart: false, flatten: true });

  log('记录中 →', OUT);
  log('关键端点单独存 →', HITS);
  log('现在去操作页面；OOPIF 也会被抓。');

  // 保持常驻
  await new Promise(() => {});
})().catch((e) => { console.error('ERR', String(e).slice(0, 400)); process.exit(1); });
