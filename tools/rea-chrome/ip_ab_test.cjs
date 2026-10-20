#!/usr/bin/env node
/**
 * 出口 IP 对照实验台。
 *
 * 目的：在完全相同的页面、相同的 sitekey、相同的浏览器上，
 *      只改变出口 IP，观察 hCaptcha 是否放行。
 *
 * 做法：
 *   · 本机起 HTTPS 服务，路径 /stripethirdparty-srv/assets/v35.5/HCaptchaInvisible.html
 *   · Chrome 用 --host-resolver-rules 把 b.stripecdn.com 指到本机
 *   · 可选 --proxy-server 挂代理
 *   · 页面里加载真实 hcaptcha api.js，调用 execute
 *
 * 用法：
 *   node tools/rea-chrome/ip_ab_test.cjs --sitekey=c7faac4c-... [--proxy=http://...]
 *   node tools/rea-chrome/ip_ab_test.cjs --sitekey=... --proxy=... --runs=3
 */
'use strict';

const https = require('node:https');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { spawn } = require('node:child_process');

const CHROME = process.env.CHROME_PATH
  || '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome';

const arg = (n, d = '') => {
  const h = process.argv.find((a) => a.startsWith(`--${n}=`));
  return h ? h.slice(n.length + 3) : d;
};
const has = (n) => process.argv.includes(`--${n}`);

const SITEKEY = arg('sitekey', 'c7faac4c-1cd7-4b1b-b2d4-42ba98d09c7a');
const RQDATA = arg('rqdata', '');
const PROXY = arg('proxy', '');
const RUNS = Number(arg('runs', '1'));
const PORT = Number(arg('port', '9443'));
const CDP_PORT = Number(arg('cdp', '9344'));
const TIMEOUT = Number(arg('timeout', '75'));
const HOST = process.env.AB_HOST || 'hcaptcha-test.local';
const M_PATH = '/stripethirdparty-srv/assets/v35.5/HCaptchaInvisible.html';

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

// 与真实 HCaptchaInvisible.html 同构的页面
const PAGE = (host) => `<!doctype html><html><head><meta charset="utf-8">
<title>HCaptchaInvisible</title>
<script src="https://hcaptcha.com/1/api.js?onload=captchaLoad&render=explicit" async defer></script>
</head><body><div id="root"></div>
<script>
window.__lib = new Promise(function (resolve) {
  if (window.hcaptcha) return resolve(window.hcaptcha);
  window.captchaLoad = function () { resolve(window.hcaptcha); };
});
window.__solve = function (sk, rq, tmo) {
  return window.__lib.then(function (hc) {
    var d = document.createElement('div');
    d.id = 'hcaptcha-invisible-' + sk;
    (document.getElementById('root') || document.body).appendChild(d);
    var rid;
    try {
      rid = hc.render(d, {
        sitekey: sk, size: 'invisible', host: ${JSON.stringify(host)},
        'error-callback': function (e) { window.__rcbErr = String(e).slice(0, 200); },
      });
    } catch (e) { return { error: 'render_failed: ' + String(e).slice(0, 160) }; }
    return new Promise(function (resolve) {
      setTimeout(function () {
        var opts = { async: true };
        if (rq) opts.rqdata = rq;
        var t0 = Date.now(), done = false;
        var timer = setTimeout(function () {
          if (!done) { done = true; resolve({ error: 'execute_timeout', ms: Date.now() - t0 }); }
        }, tmo);
        hc.execute(rid, opts).then(function (v) {
          if (done) return; done = true; clearTimeout(timer);
          var tok = (v && v.response) || v;
          if (typeof tok !== 'string' || tok.length < 100)
            return resolve({ error: 'short_token', ms: Date.now() - t0 });
          resolve({ token: tok, len: tok.length, ms: Date.now() - t0 });
        }, function (e) {
          if (done) return; done = true; clearTimeout(timer);
          resolve({ error: 'execute_failed: ' + String(e).slice(0, 200), ms: Date.now() - t0 });
        });
      }, 2500);
    });
  });
};
</script></body></html>`;

function startServer() {
  return new Promise((resolve, reject) => {
    const srv = https.createServer(
      { key: fs.readFileSync('/tmp/localmerchant/key.pem'),
        cert: fs.readFileSync('/tmp/localmerchant/cert.pem') },
      (req, res) => {
        if (!req.url || req.url.startsWith(M_PATH) || req.url === '/') {
          res.writeHead(200, { 'Content-Type': 'text/html; charset=utf-8' });
          return res.end(PAGE(HOST));
        }
        res.writeHead(404); res.end('nf');
      },
    );
    srv.on('error', reject);
    srv.listen(PORT, '127.0.0.1', () => resolve(srv));
  });
}

function splitProxy(p) {
  if (!p) return null;
  let t = p;
  if (!/^[a-z0-9+.-]+:\/\//i.test(t)) t = 'http://' + t;
  const u = new URL(t);
  return {
    server: `${u.protocol}//${u.hostname}:${u.port || 80}`,
    user: decodeURIComponent(u.username || ''),
    pass: decodeURIComponent(u.password || ''),
  };
}

async function main() {
  if (!fs.existsSync('/tmp/localmerchant/cert.pem')) {
    console.error('缺证书：先在 /tmp/localmerchant 生成 cert.pem / key.pem');
    process.exit(1);
  }
  const srv = await startServer();
  console.log('本地页面:', `https://${HOST}${M_PATH}`);

  const profile = path.join(os.tmpdir(), `rea-ab-${Date.now()}`);
  fs.mkdirSync(profile, { recursive: true });
  const px = splitProxy(PROXY);

  const args = [
    `--remote-debugging-port=${CDP_PORT}`,
    `--user-data-dir=${profile}`,
    '--no-first-run', '--no-default-browser-check',
    '--disable-blink-features=AutomationControlled',
    '--ignore-certificate-errors', '--allow-insecure-localhost',
    '--lang=en-US,en',
    `--host-resolver-rules=MAP ${HOST} 127.0.0.1:${PORT}`,
    `--window-size=1440,900`,
    `https://${HOST}${M_PATH}`,
  ];
  if (px) {
    args.push(`--proxy-server=${px.server}`);
    args.push(`--proxy-bypass-list=<-loopback>;${HOST}`);
    console.log('出口代理:', px.server, '| user:', px.user.slice(0, 46));
  } else {
    console.log('出口代理: 无（直连本机网络）');
  }

  const child = spawn(CHROME, args, { stdio: 'ignore', detached: true });
  child.unref();

  // 等 CDP
  let pageWs = null;
  for (let i = 0; i < 40; i += 1) {
    await sleep(500);
    try {
      const list = await (await fetch(`http://127.0.0.1:${CDP_PORT}/json/list`)).json();
      const pg = list.find((t) => t.type === 'page' && (t.url || '').includes(HOST));
      if (pg?.webSocketDebuggerUrl) { pageWs = pg.webSocketDebuggerUrl; break; }
    } catch (_) {}
  }
  if (!pageWs) { console.error('CDP 未就绪'); srv.close(); process.exit(1); }

  const ws = new WebSocket(pageWs);
  let id = 0; const pend = new Map();
  ws.addEventListener('message', (ev) => {
    const m = JSON.parse(ev.data);
    if (m.id && pend.has(m.id)) {
      const p = pend.get(m.id); pend.delete(m.id);
      m.error ? p.rej(new Error(JSON.stringify(m.error))) : p.ok(m.result);
    }
  });
  const send = (a, b = {}) => new Promise((ok, rej) => {
    const i = ++id; pend.set(i, { ok, rej });
    ws.send(JSON.stringify({ id: i, method: a, params: b }));
    setTimeout(() => { if (pend.delete(i)) rej(new Error('timeout ' + a)); }, 120000);
  });
  await new Promise((r) => ws.addEventListener('open', r));
  await send('Page.enable');
  await send('Runtime.enable');
  await send('Network.enable').catch(() => {});
  // 诊断：打印 api.js 相关请求的失败原因
  ws.addEventListener('message', (ev) => {
    const m = JSON.parse(ev.data);
    if (m.method === 'Network.loadingFailed') {
      console.log('  [net fail]', (m.params.errorText || '').slice(0, 60), (m.params.blockedReason || ''));
    }
    if (m.method === 'Network.responseReceived') {
      const u = m.params.response?.url || '';
      if (/hcaptcha\.com/.test(u)) console.log('  [net]', m.params.response.status, u.slice(0, 80));
    }
  });

  // 代理认证：Fetch 只拦 authRequired 阶段，必须同时 continue 所有 requestPaused，
  // 否则主文档也会被拦住，页面脚本不会执行（window.__solve 就没了）。
  if (px?.user) {
    ws.addEventListener('message', async (ev) => {
      const m = JSON.parse(ev.data);
      if (m.method === 'Fetch.authRequired') {
        send('Fetch.continueWithAuth', {
          requestId: m.params.requestId,
          authChallengeResponse: {
            response: 'ProvideCredentials',
            username: px.user, password: px.pass,
          },
        }).catch(() => {});
      } else if (m.method === 'Fetch.requestPaused') {
        // 一律放行；认证由 authRequired 单独应答
        send('Fetch.continueRequest', { requestId: m.params.requestId }).catch(() => {});
      }
    });
    // handleAuthRequests 需要 Request stage 才能拿 407；
    // disable 掉默认 Continue，改为我们自己逐个 continue，
    // 否则 api.js 会被卡住（表现为 hcaptcha 永不就绪）。
    await send('Fetch.enable', {
      patterns: [{ urlPattern: '*', requestStage: 'Request' }],
      handleAuthRequests: true,
    }).catch(() => {});
    await send('Page.reload', { ignoreCache: true });
    await sleep(7000);
  }

  // 等 api.js 就绪
  let ready = false;
  for (let i = 0; i < 30; i += 1) {
    await sleep(700);
    try {
      const r = await send('Runtime.evaluate', {
        expression: 'typeof window.hcaptcha', returnByValue: true });
      if (r.result?.value === 'object') { ready = true; break; }
    } catch (_) {}
  }
  console.log('hcaptcha 就绪:', ready);
  try {
    const diag = await send('Runtime.evaluate', {
      expression: `JSON.stringify({
        href: location.href.slice(0,110),
        hasSolve: typeof window.__solve,
        hasLib: typeof window.__lib,
        scripts: [...document.scripts].map(s => (s.src||'inline').slice(0,60)),
        bodyLen: document.body.innerHTML.length,
      })`, returnByValue: true });
    console.log('页面自检:', diag.result?.value);
  } catch (_) {}
  if (!ready) {
    await send('Runtime.evaluate', { expression: 'JSON.stringify({href:location.href,loadErr:!!window.__rcbErr})', returnByValue: true })
      .then((r) => console.log('页面:', r.result?.value)).catch(() => {});
    ws.close(); srv.close(); process.exit(1);
  }

  // 先查一次 checksiteconfig，记录 pass 与 JWT 特征（定位环境差异用）
  try {
    const cc = await send('Runtime.evaluate', {
      expression: `new Promise(async res => {
        try {
          const sk = ${JSON.stringify(SITEKEY)};
          const r = await fetch('https://api.hcaptcha.com/checksiteconfig?v=86591f996c36877668d253e353e36fac021db33b&host=b.stripecdn.com&sitekey='+sk+'&sc=1&swa=1', {credentials:'include'});
          const j = await r.json();
          const jwt = (j.c||{}).req || '';
          let pl = null;
          try { const q = jwt.split('.')[1]; pl = JSON.parse(atob(q.replace(/-/g,'+').replace(/_/g,'/')+'='.repeat((-q.length)%4))); } catch(e){}
          res(JSON.stringify({http:r.status, pass:j.pass, features:j.features,
            jwtLen:jwt.length, f:pl&&pl.f, s:pl&&pl.s, t:pl&&pl.t, dlen:pl&&String(pl.d||'').length}));
        } catch(e) { res('ERR '+String(e).slice(0,150)); }
      })`, returnByValue: true, awaitPromise: true });
    console.log('checksiteconfig:', cc.result?.value);
  } catch (_) {}

  // 采集环境指纹（与服务器对比）
  try {
    const fp = await send('Runtime.evaluate', {
      expression: `JSON.stringify({
        ua: navigator.userAgent, platform: navigator.platform,
        webdriver: navigator.webdriver, cores: navigator.hardwareConcurrency,
        mem: navigator.deviceMemory, screen: screen.width+'x'+screen.height,
        gpu: (()=>{try{const c=document.createElement('canvas');const g=c.getContext('webgl');
          const d=g.getExtension('WEBGL_debug_renderer_info');
          return g.getParameter(d.UNMASKED_RENDERER_WEBGL);}catch(e){return null}})(),
        brands: navigator.userAgentData ? navigator.userAgentData.brands.map(b=>b.brand) : null,
        tz: Intl.DateTimeFormat().resolvedOptions().timeZone,
      })`, returnByValue: true });
    console.log('环境:', fp.result?.value);
  } catch (_) {}

  const results = [];
  for (let i = 0; i < RUNS; i += 1) {
    const expr = `window.__solve(${JSON.stringify(SITEKEY)}, ${JSON.stringify(RQDATA)}, ${TIMEOUT * 1000})`;
    let out = null;
    try {
      const r = await send('Runtime.evaluate', {
        expression: expr, returnByValue: true, awaitPromise: true });
      out = r?.result?.value ?? { error: 'empty_result', raw: JSON.stringify(r).slice(0, 200) };
    } catch (e) { out = { error: String(e).slice(0, 160) }; }
    results.push(out);
    const dump = JSON.stringify(out);
    console.log(`  #${i + 1}`, (dump || '').slice(0, 190));
    if (i < RUNS - 1) await sleep(1500);
  }

  const ok = results.filter((r) => r && r.token).length;
  console.log(`\n结果: ${ok}/${results.length} 成功`);
  console.log(JSON.stringify({ proxy: px ? px.server : 'direct', sitekey: SITEKEY, ok, runs: results.length }));

  ws.close();
  srv.close();
  if (!has('keep')) {
    try { process.kill(-child.pid); } catch (_) { try { child.kill(); } catch (_) {} }
  } else {
    console.log('（--keep：Chrome 与页面服务保留，供手动检查）');
    await new Promise(() => {});
  }
}

main().catch((e) => { console.error('ERR', String(e).slice(0, 400)); process.exit(1); });
