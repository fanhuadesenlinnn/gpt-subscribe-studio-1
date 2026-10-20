#!/usr/bin/env node
/**
 * 直接求解 hCaptcha token。
 *
 * 原理：
 *   Stripe 把 hCaptcha 挂在 b.stripecdn.com/stripethirdparty-srv/assets/v35.5/
 *   HCaptchaInvisible.html 里，那个页面有完整的 window.hcaptcha API
 *   （render / execute / getResponse / getRespKey）。在它内部调用 execute
 *   就是"页面自己求解"，无需人工点选，也不需要图像题。
 *
 * 为什么不是直接调 hsw：
 *   hsw(1,x) 只做加密，key 绑定在 wasm 会话里；hsw(0,x) 解密需要服务端密钥。
 *   自己拼 getcaptcha 的 wire 一定 415（连真品 wire 回放也 415，因为会话不匹配）。
 *   唯一可行的"直接求解"就是让持有 hcaptcha 实例的那个 frame 自己跑 execute。
 *
 * 用法：
 *   node tools/rea-chrome/solve_hcaptcha.cjs                 # 用当前页面的 sitekey
 *   node tools/rea-chrome/solve_hcaptcha.cjs --sitekey=xxx
 *   node tools/rea-chrome/solve_hcaptcha.cjs --rqdata=xxx --json
 *   node tools/rea-chrome/solve_hcaptcha.cjs --loop=5        # 连续求解 5 次
 */
'use strict';

const PORT = Number(process.env.CDP_PORT || 9222);
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

const arg = (n, d = '') => {
  const h = process.argv.find((a) => a.startsWith(`--${n}=`));
  return h ? h.slice(n.length + 3) : d;
};
const has = (n) => process.argv.includes(`--${n}`);

const SITEKEY = arg('sitekey', '');
const RQDATA = arg('rqdata', '');
const LOOP = Number(arg('loop', '1'));
const AS_JSON = has('json');
const TIMEOUT = Number(arg('timeout', '90'));

class CDP {
  constructor(url) { this.url = url; this.id = 0; this.pend = new Map(); this.all = new Map(); this.handlers = []; }
  connect() {
    return new Promise((ok, rej) => {
      this.ws = new WebSocket(this.url);
      this.ws.addEventListener('open', () => ok(this));
      this.ws.addEventListener('error', () => rej(new Error('ws error')));
      this.ws.addEventListener('message', (ev) => {
        let m; try { m = JSON.parse(ev.data); } catch (_) { return; }
        if (m.id !== undefined && this.pend.has(m.id)) {
          const p = this.pend.get(m.id); this.pend.delete(m.id);
          m.error ? p.rej(new Error(JSON.stringify(m.error))) : p.ok(m.result);
          return;
        }
        if (m.method === 'Target.attachedToTarget') {
          this.all.set(m.params.sessionId, m.params.targetInfo || {});
        }
      });
    });
  }
  send(method, params = {}, sessionId) {
    const id = ++this.id;
    return new Promise((ok, rej) => {
      this.pend.set(id, { ok, rej });
      const msg = { id, method, params };
      if (sessionId) msg.sessionId = sessionId;
      this.ws.send(JSON.stringify(msg));
      setTimeout(() => { if (this.pend.delete(id)) rej(new Error('timeout ' + method)); }, 180000);
    });
  }
  close() { try { this.ws.close(); } catch (_) {} }
}

async function attachAll(cdp) {
  await cdp.send('Target.setAutoAttach', { autoAttach: true, waitForDebuggerOnStart: false, flatten: true });
  await sleep(1800);
  for (let round = 0; round < 3; round += 1) {
    for (const s of [...cdp.all.keys()]) {
      try {
        await cdp.send('Target.setAutoAttach', { autoAttach: true, waitForDebuggerOnStart: false, flatten: true }, s);
      } catch (_) {}
    }
    await sleep(900);
  }
}

/** 找出持有 window.hcaptcha 的 frame。 */
async function findHcaptchaFrame(cdp) {
  const cands = [...cdp.all.entries()].filter(([, v]) => /HCaptchaInvisible\.html|hcaptcha-invisible|js\.stripe\.com\/v3\/hcaptcha/.test(v.url || ''));
  for (const [sid, info] of cands) {
    try {
      await cdp.send('Runtime.enable', {}, sid);
      const r = await cdp.send('Runtime.evaluate', {
        expression: 'typeof window.hcaptcha + "|" + (window.hcaptcha ? Object.keys(window.hcaptcha).join(",") : "")',
        returnByValue: true,
      }, sid);
      const val = String(r.result?.value || '');
      if (val.startsWith('object|') && val.includes('execute')) {
        return { sid, url: info.url || '', api: val.split('|')[1] };
      }
    } catch (_) {}
  }
  return null;
}

const SOLVE_JS = (sitekey, rqdata, timeoutMs) => `new Promise(async (res) => {
  const out = { steps: [] };
  try {
    const urlSk = (location.hash.match(/sitekey=([0-9a-f-]+)/) || [])[1] || null;
    const sk = ${JSON.stringify(sitekey)} || urlSk || '24ed0064-62cf-4d42-9960-5dd1a41d4e29';
    out.sitekey = sk;
    let div = document.getElementById('__rea_solve');
    if (!div) { div = document.createElement('div'); div.id = '__rea_solve'; document.body.appendChild(div); }
    const rid = window.hcaptcha.render(div, { sitekey: sk, size: 'invisible', host: 'b.stripecdn.com' });
    out.widgetId = rid;
    await new Promise(x => setTimeout(x, 2000));
    const opts = { async: true };
    const rq = ${JSON.stringify(rqdata)};
    if (rq) opts.rqdata = rq;
    const t0 = Date.now();
    const v = await Promise.race([
      window.hcaptcha.execute(rid, opts),
      new Promise(x => setTimeout(() => x('__TIMEOUT__'), ${timeoutMs})),
    ]);
    out.elapsedMs = Date.now() - t0;
    if (v === '__TIMEOUT__') { out.timeout = true; res(JSON.stringify(out)); return; }
    const token = (v && v.response) || v;
    if (typeof token !== 'string' || token.length < 100) {
      out.error = 'short_token:' + String(token).slice(0, 60);
      res(JSON.stringify(out)); return;
    }
    out.token = token;
    out.tokenLen = token.length;
    out.ekey = (v && (v.ekey || v.key)) || '';
    // 顺便解 payload 给调用方看
    try {
      const body = token.replace(/^P1_/, '');
      const p = body.split('.')[1];
      out.payload = JSON.parse(atob(p.replace(/-/g, '+').replace(/_/g, '/') + '='.repeat((-p.length) % 4)));
      if (out.payload && out.payload.passkey) out.payload.passkey = '(' + String(out.payload.passkey).length + ' chars)';
      if (out.payload && out.payload.cdata) out.payload.cdata = '(' + String(out.payload.cdata).length + ' chars)';
    } catch (_) {}
    res(JSON.stringify(out));
  } catch (e) {
    out.error = String(e).slice(0, 400);
    res(JSON.stringify(out));
  }
})`;

async function solveOnce(cdp, frame) {
  const r = await cdp.send('Runtime.evaluate', {
    expression: SOLVE_JS(SITEKEY, RQDATA, TIMEOUT * 1000),
    returnByValue: true,
    awaitPromise: true,
  }, frame.sid);
  if (r.exceptionDetails) throw new Error(r.exceptionDetails.text);
  return JSON.parse(r.result?.value || '{}');
}

async function main() {
  const list = await (await fetch(`http://127.0.0.1:${PORT}/json/list`)).json();
  const want = arg('target', '');
  const pages = list.filter((t) => t.type === 'page');
  const page = want ? (pages.find((t) => (t.url || '').includes(want)) || pages[0]) : pages[0];
  if (!page) { console.error('没有可调试页面；先跑 open_debug_chrome.cjs'); process.exit(1); }
  if (!AS_JSON) console.error('页面:', (page.url || '').slice(0, 100));

  const cdp = await new CDP(page.webSocketDebuggerUrl).connect();
  try {
    await attachAll(cdp);
    const frame = await findHcaptchaFrame(cdp);
    if (!frame) {
      console.error('没有找到持有 hcaptcha API 的 frame');
      console.error('检查页面是否已加载 Stripe 结账（HCaptchaInvisible.html 才会出现）');
      process.exit(2);
    }
    if (!AS_JSON) {
      console.error('frame:', frame.url.slice(0, 100));
      console.error('api  :', frame.api);
    }

    const results = [];
    for (let i = 0; i < LOOP; i += 1) {
      const out = await solveOnce(cdp, frame);
      results.push(out);
      if (AS_JSON) {
        process.stdout.write(JSON.stringify(out) + '\n');
      } else {
        if (out.token) {
          console.log(`#${i + 1} ✅ token ${out.tokenLen} 字符  ${out.elapsedMs}ms  sitekey=${out.sitekey}`);
          if (out.payload) console.log('    payload:', JSON.stringify(out.payload));
          if (out.ekey) console.log('    ekey:', String(out.ekey).slice(0, 40));
        } else if (out.timeout) {
          console.log(`#${i + 1} ⏱ 超时（${out.elapsedMs}ms）sitekey=${out.sitekey}`);
        } else {
          console.log(`#${i + 1} ✗ ${out.error || 'unknown'}`);
        }
      }
      if (i < LOOP - 1) await sleep(1200);
    }
    const ok = results.filter((r) => r.token).length;
    if (!AS_JSON) console.log(`\n成功 ${ok}/${results.length}`);
    process.exitCode = ok === results.length ? 0 : 1;
  } finally {
    cdp.close();
  }
}

main().catch((e) => { console.error('ERR', String(e).slice(0, 400)); process.exit(1); });
