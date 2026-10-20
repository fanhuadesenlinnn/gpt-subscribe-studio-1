#!/usr/bin/env node
/**
 * hCaptcha token 求解器（HTTP 服务，带会话池）
 *
 * 为什么需要它：
 *   所有 stripe 域（pay.openai.com / js.stripe.com / b.stripecdn.com /
 *   m.stripe.network）的 CSP 都缺 'unsafe-eval'，hCaptcha 的 wasm
 *   无法在这些域实例化，会抛 EvalError。
 *   只有 newassets.hcaptcha.com 的 CSP 含 'unsafe-eval'。
 *
 *   所以必须用一个中性 origin（http://127.0.0.1:<port>）加载
 *   hcaptcha.com/1/api.js —— 它自己创建 newassets iframe，
 *   wasm 在 iframe 内正常运行。
 *
 * 为什么要会话池：
 *   Stripe 的 confirm 可能连发两次验证（passive + intent_confirmation_challenge）。
 *   两次 token 必须来自同一浏览器实例、同一出口 IP，否则 verify_challenge
 *   返回 payment_intent_authentication_failure。
 *
 * 用法：
 *   node server.js                      # 默认 127.0.0.1:8199
 *   PORT=8199 node server.js
 *
 * 接口：
 *   POST /session        { proxy?, ua? }         → { session_id, ua, proxy_ip }
 *   POST /solve          { sitekey, rqdata, host, session_id?, proxy?, ua? }
 *                                               → { token, ekey, elapsed_ms, session_id }
 *   DELETE /session/{id}                         → { ok: true }
 *   GET  /health                                 → { ok, browser, sessions }
 *   GET  /sessions                               → { sessions: [...] }
 */

'use strict';

const http = require('node:http');
const crypto = require('node:crypto');
const puppeteer = require(process.env.PUPPETEER_PATH || 'puppeteer-core');

const PORT = Number(process.env.PORT || 8199);
const HOST = process.env.HOST || '127.0.0.1';
const CHROME = process.env.CHROME_PATH || '/usr/bin/google-chrome';
const DEFAULT_SITEKEY = process.env.HCAPTCHA_SITEKEY || '24ed0064-62cf-4d42-9960-5dd1a41d4e29';
const DEFAULT_HOST = process.env.HCAPTCHA_HOST || 'b.stripecdn.com';
const TOKEN_TIMEOUT_MS = Number(process.env.TOKEN_TIMEOUT_MS || 90000);
const SESSION_TTL_MS = Number(process.env.SESSION_TTL_MS || 600000);
const MAX_SESSIONS = Number(process.env.MAX_SESSIONS || 8);
const DEFAULT_UA =
  process.env.HCAPTCHA_UA ||
  'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 ' +
  '(KHTML, like Gecko) Chrome/155.0.0.0 Safari/537.36';

const PAGE_HTML = '<!doctype html><html><head><meta charset="utf-8"></head>' +
  '<body><div id="hc"></div></body></html>';

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const now = () => Date.now();

// ── origin ─────────────────────────────────────────────────
// 两种模式：
//   loopback（默认）  http://127.0.0.1:<port>
//                     hCaptcha 白名单允许，但 challenge iframe 的 host 会变成
//                     127.0.0.1，Stripe 的 c7faac4c 会按「域不匹配」出图像题。
//   merchant          https://b.stripecdn.com
//                     用自签证书 + --host-resolver-rules 把域名解析到本机。
//                     与真实浏览器同源，challenge 才按正常评分放行。
//                     由 ORIGIN_MODE=merchant 开启。
const ORIGIN_MODE = process.env.ORIGIN_MODE || 'loopback';
const MERCHANT_HOST = process.env.MERCHANT_HOST || 'b.stripecdn.com';
const MERCHANT_PORT = Number(process.env.MERCHANT_PORT || 8443);
const CERT_DIR = process.env.CERT_DIR || '/tmp/hcs';

function ensureCert(host) {
  const fs = require('node:fs');
  const keyPath = `${CERT_DIR}/key.pem`;
  const certPath = `${CERT_DIR}/cert.pem`;
  if (fs.existsSync(keyPath) && fs.existsSync(certPath)) {
    return { key: fs.readFileSync(keyPath), cert: fs.readFileSync(certPath) };
  }
  fs.mkdirSync(CERT_DIR, { recursive: true });
  require('node:child_process').execSync(
    `openssl req -x509 -newkey rsa:2048 -keyout ${keyPath} -out ${certPath} ` +
    `-days 365 -nodes -subj "/CN=${host}" -addext "subjectAltName=DNS:${host}" 2>/dev/null`
  );
  return { key: fs.readFileSync(keyPath), cert: fs.readFileSync(certPath) };
}

// merchant 模式端口固定，多个会话共用同一个 origin 服务；loopback 模式端口任意。
let _originSingleton = null;

function startOriginServer() {
  if (_originSingleton) return Promise.resolve(_originSingleton);
  if (ORIGIN_MODE === 'merchant') {
    return new Promise((resolve, reject) => {
      const tls = ensureCert(MERCHANT_HOST);
      const server = require('node:https').createServer(tls, (req, res) => {
        res.writeHead(200, { 'Content-Type': 'text/html; charset=utf-8' });
        res.end(PAGE_HTML);
      });
      server.on('error', reject);
      server.listen(MERCHANT_PORT, '127.0.0.1', () => {
        _originSingleton = {
          origin: `https://${MERCHANT_HOST}`,
          close: () => new Promise((r) => server.close(() => r())),
        };
        resolve(_originSingleton);
      });
    });
  }
  return new Promise((resolve) => {
    const server = http.createServer((req, res) => {
      res.writeHead(200, { 'Content-Type': 'text/html; charset=utf-8' });
      res.end(PAGE_HTML);
    });
    server.listen(0, '127.0.0.1', () => {
      const { port } = server.address();
      resolve({
        origin: `http://127.0.0.1:${port}`,
        close: () => new Promise((r) => server.close(() => r())),
      });
    });
  });
}

// ── Chrome ─────────────────────────────────────────────────
// Chrome 的 --proxy-server 不接受 URL 里的账号密码，
// 带 foo:bar@host:port 会直接抛 net::ERR_NO_SUPPORTED_PROXIES。
// 账号密码必须交给 page.authenticate 应答 407。
function splitProxy(proxy) {
  const out = { server: '', username: '', password: '' };
  if (!proxy) return out;
  let text = String(proxy).trim();
  if (!/^[a-z0-9+.-]+:\/\//i.test(text)) text = 'http://' + text;
  let u;
  try {
    u = new URL(text);
  } catch (_) {
    return out;
  }
  out.server = `${u.protocol}//${u.hostname}${u.port ? ':' + u.port : ''}`;
  out.username = decodeURIComponent(u.username || '');
  out.password = decodeURIComponent(u.password || '');
  return out;
}

async function launchBrowser(proxy) {
  const p = splitProxy(proxy);
  const merchant = ORIGIN_MODE === 'merchant';
  const extra = merchant
    ? [
        '--ignore-certificate-errors',
        '--allow-insecure-localhost',
        `--host-resolver-rules=MAP ${MERCHANT_HOST} 127.0.0.1:${MERCHANT_PORT}`,
        `--proxy-bypass-list=<-loopback>;${MERCHANT_HOST}`,
      ]
    : ['--ignore-certificate-errors', '--allow-insecure-localhost'];
  return puppeteer.launch({
    executablePath: CHROME,
    headless: false,
    args: [
      '--no-sandbox',
      '--disable-dev-shm-usage',
      '--use-gl=angle',
      '--use-angle=swiftshader',
      '--enable-unsafe-swiftshader',
      '--ignore-gpu-blocklist',
      '--disable-blink-features=AutomationControlled',
      '--disable-features=IsolateOrigins,site-per-process',
      '--lang=en-US,en',
      ...extra,
      ...(p.server ? [`--proxy-server=${p.server}`] : []),
    ],
    ignoreDefaultArgs: ['--enable-automation'],
    defaultViewport: { width: 1440, height: 900 },
    protocolTimeout: 240000,
  });
}

// ── 会话 ───────────────────────────────────────────────────
class Session {
  constructor({ proxy, ua }) {
    this.id = crypto.randomUUID();
    this.proxy = proxy || '';
    this.ua = ua || DEFAULT_UA;
    this.browser = null;
    this.page = null;
    this.origin = null;
    this.lock = Promise.resolve();
    this.busy = false;
    this.createdAt = now();
    this.usedAt = now();
    this.solves = 0;
    this.failures = 0;
    this.proxyIp = '';
  }

  async open() {
    const originSrv = await startOriginServer();
    this._originSrv = originSrv;
    this._ownsOrigin = ORIGIN_MODE !== 'merchant';
    this.origin = originSrv.origin;
    this.browser = await launchBrowser(this.proxy);
    this.page = await this.browser.newPage();

    const pp = splitProxy(this.proxy);
    if (pp.username) {
      await this.page.authenticate({
        username: pp.username,
        password: pp.password,
      });
    }

    await this.page.setUserAgent(this.ua);
    await this.page.setExtraHTTPHeaders({
      'Accept-Language': 'en-US,en;q=0.9',
    });

    // 反自动化痕迹：webdriver 与插件数组
    await this.page.evaluateOnNewDocument(() => {
      Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
      Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'] });
      Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });
      window.chrome = window.chrome || { runtime: {} };
    });

    await this.page.goto(`${this.origin}/`, { waitUntil: 'domcontentloaded', timeout: 30000 });

    // 预热：先通一次 hcaptcha.com，让代理链路建立
    try {
      await this.page.evaluate(async () => {
        await fetch('https://hcaptcha.com/1/api.js', { method: 'HEAD', mode: 'no-cors' })
          .catch(() => {});
      });
      await sleep(800);
    } catch (_) {}

    const loaded = await this.page.evaluate(() => new Promise((resolve) => {
      const s = document.createElement('script');
      s.src = 'https://hcaptcha.com/1/api.js?onload=__hc_cb&render=explicit';
      s.async = true;
      s.defer = true;
      window.__hc_cb = () => { window.__hc_loaded = true; resolve(true); };
      s.onerror = () => resolve(false);
      document.head.appendChild(s);
      setTimeout(() => resolve(typeof window.hcaptcha === 'object'), 25000);
    }));
    if (!loaded) throw new Error('api.js_load_failed');

    // api.js 就绪 ≠ hCaptcha 内部就绪。少了这段等待，
    // widget 在未初始化状态下 render，passkey 会变短，被判可疑。
    await sleep(2500);

    // 行为数据预热：hsw 会把 mousemove/pointermove 写进 manifest。
    // 完全没有鼠标事件的会话 = 零行为数据，challenge 会直接出图像题。
    await this.humanWarmup();
    return this;
  }

  /** 生成贝塞尔曲线鼠标轨迹，填充 hsw 的行为数据窗口。 */
  async humanWarmup(moves = 4) {
    if (!this.page) return;
    const curve = (from, to, n) => {
      const c1 = {
        x: from.x + (to.x - from.x) * 0.3 + (Math.random() - 0.5) * 160,
        y: from.y + (to.y - from.y) * 0.3 + (Math.random() - 0.5) * 160,
      };
      const c2 = {
        x: from.x + (to.x - from.x) * 0.7 + (Math.random() - 0.5) * 160,
        y: from.y + (to.y - from.y) * 0.7 + (Math.random() - 0.5) * 160,
      };
      const pts = [];
      for (let i = 0; i <= n; i += 1) {
        const t = i / n;
        const a = Math.pow(1 - t, 3);
        const b = 3 * Math.pow(1 - t, 2) * t;
        const c = 3 * (1 - t) * t * t;
        const d = t * t * t;
        pts.push({ x: a * from.x + b * c1.x + c * c2.x + d * to.x,
                   y: a * from.y + b * c1.y + c * c2.y + d * to.y });
      }
      return pts;
    };

    let cur = { x: 180 + Math.random() * 240, y: 160 + Math.random() * 240 };
    try {
      await this.page.mouse.move(cur.x, cur.y);
      for (let m = 0; m < moves; m += 1) {
        const to = { x: 90 + Math.random() * 1100, y: 90 + Math.random() * 620 };
        const pts = curve(cur, to, 18 + Math.floor(Math.random() * 16));
        for (const p of pts) {
          await this.page.mouse.move(p.x, p.y);
          await sleep(8 + Math.random() * 20);
        }
        cur = to;
        if (Math.random() < 0.3) {
          await this.page.mouse.wheel({ deltaY: Math.random() < 0.5 ? -60 : 60 });
          await sleep(110);
        }
        await sleep(140 + Math.random() * 380);
      }
    } catch (_) {}
  }

  /** 串行化：同一会话同时只跑一次求解。 */
  withLock(fn) {
    const run = this.lock.then(fn, fn);
    this.lock = run.catch(() => {});
    return run;
  }

  async solve({ sitekey, rqdata, host, timeout }) {
    this.usedAt = now();
    this.solves += 1;
    const result = await this.page.evaluate(async (cfg) => {
      if (typeof window.hcaptcha !== 'object') return { error: 'no_hcaptcha' };

      const div = document.createElement('div');
      div.id = 'hc-' + Math.random().toString(36).slice(2);
      document.body.appendChild(div);

      let widgetId;
      try {
        widgetId = window.hcaptcha.render(div, {
          sitekey: cfg.sitekey,
          size: 'invisible',
          host: cfg.host,
          callback: (tok) => { window.__hc_token = tok || ''; },
        });
      } catch (e) {
        return { error: 'render_failed: ' + String(e).slice(0, 160) };
      }

      // render 之后等 iframe 里 wasm 初始化
      await new Promise((r) => setTimeout(r, 3500));

      const execOpts = { async: true };
      if (cfg.rqdata) execOpts.rqdata = cfg.rqdata;

      try {
        const p = window.hcaptcha.execute(widgetId, execOpts);
        const timeout = new Promise((r) => setTimeout(() => r(null), cfg.timeout));
        const res = await Promise.race([p, timeout]);
        if (!res) return { error: 'execute_timeout', challenge: cfg.captureChallenge };
        const tok = (res && res.response) || res;
        if (typeof tok !== 'string' || tok.length < 100) {
          return { error: 'short_token: ' + String(tok).slice(0, 80) };
        }
        return {
          token: tok,
          ekey: (res && (res.ekey || res.key)) || '',
        };
      } catch (e) {
        return { error: 'execute_failed: ' + String(e).slice(0, 160) };
      }
    }, {
      sitekey: sitekey || DEFAULT_SITEKEY,
      rqdata: rqdata || '',
      host: host || DEFAULT_HOST,
      timeout: timeout || TOKEN_TIMEOUT_MS,
      captureChallenge: false,
    });
    // 超时多半是弹了图像题：把题目与九宫格抓回来，交给上层作答
    if (result.error === 'execute_timeout') {
      try {
        const ch = await this.captureChallenge();
        if (ch) result.challenge = ch;
      } catch (_) {}
    }
    if (result.error) this.failures += 1;
    return result;
  }

  /** 抓取当前 challenge iframe 的题目文字、九宫格图 URL 与格子索引。 */
  async captureChallenge() {
    const fr = this.page.frames().find((f) => /challenge/.test(f.url()));
    if (!fr) return null;
    return fr.evaluate(() => {
      const txt = (document.body.innerText || '').trim();
      const header = txt.split('\n').filter(Boolean).slice(0, 2).join(' / ');
      // 9 宫格既可能是 <img>，也可能是 canvas / background-image
      const imgs = [...document.querySelectorAll('img')]
        .map((i) => i.src)
        .filter((s) => /imgs\d*\.hcaptcha\.com/.test(s));
      const canvases = [...document.querySelectorAll('canvas')].map((c) => c.toDataURL().slice(0, 64) + '...');
      const bgs = [...document.querySelectorAll('div')]
        .map((d) => getComputedStyle(d).backgroundImage)
        .filter((v) => v && v.includes('hcaptcha.com'))
        .map((v) => v.replace(/^url\("?|"?\)$/g, ''));
      const prompt = [...document.querySelectorAll('div,span,h2,p')]
        .map((d) => (d.textContent || '').trim())
        .find((t) => /select|choose|click|pick/i.test(t) && t.length < 200) || '';
      // 可选格子的可点击区域（9 宫格通常是 role=button 或带 .button 的 div）
      const cells = [...document.querySelectorAll('div[role=button],button,div.button')].map((d, i) => {
        const r = d.getBoundingClientRect();
        return {
          i,
          aria: d.getAttribute('aria-label') || '',
          x: Math.round(r.x + r.width / 2),
          y: Math.round(r.y + r.height / 2),
          w: Math.round(r.width),
          h: Math.round(r.height),
        };
      }).filter((c) => c.w > 20 && c.h > 20);
      return { prompt, header, imgs: [...imgs, ...bgs], canvases: canvases.length, cells, frameUrl: location.href.slice(0, 160) };
    }).catch(() => null);
  }

  /** 按格子索引点击（0 基），点完提交。 */
  async answerChallenge(indices) {
    const frameEl = await this.page.$('iframe[src*="frame=challenge"]');
    if (!frameEl) return { error: 'no_challenge_iframe' };
    const box = await frameEl.boundingBox();
    if (!box) return { error: 'no_iframe_box' };
    const fr = this.page.frames().find((f) => /challenge/.test(f.url()));
    if (!fr) return { error: 'no_challenge_frame' };

    const cells = await fr.evaluate(() => [...document.querySelectorAll('div[role=button],button,div.button')]
      .map((d, i) => {
        const r = d.getBoundingClientRect();
        return {
          i,
          aria: d.getAttribute('aria-label') || '',
          x: r.x + r.width / 2,
          y: r.y + r.height / 2,
          w: Math.round(r.width),
          h: Math.round(r.height),
        };
      })
      // 只要 9 宫格那种方形格子，排除 Skip/EN/刷新等按钮
      .filter((c) => c.w >= 80 && c.h >= 80 && Math.abs(c.w - c.h) < 24));

    const picked = [];
    for (const idx of indices) {
      const c = cells[idx];
      if (!c) continue;
      const ax = box.x + c.x;
      const ay = box.y + c.y;
      await this.page.mouse.move(ax + (Math.random() - 0.5) * 8, ay + (Math.random() - 0.5) * 8);
      await sleep(60 + Math.random() * 120);
      await this.page.mouse.click(ax, ay, { delay: 40 + Math.random() * 60 });
      picked.push({ idx, aria: c.aria });
      await sleep(300 + Math.random() * 400);
    }
    return { picked, cells: cells.length };
  }

  /** 点 Verify / Submit 之类的提交按钮。 */
  async submitChallenge() {
    const frameEl = await this.page.$('iframe[src*="frame=challenge"]');
    if (!frameEl) return { error: 'no_challenge_iframe' };
    const box = await frameEl.boundingBox();
    const fr = this.page.frames().find((f) => /challenge/.test(f.url()));
    if (!fr || !box) return { error: 'no_frame' };
    const btn = await fr.evaluate(() => {
      const cands = [...document.querySelectorAll('div[role=button],button,div.button')];
      for (const d of cands) {
        const t = (d.innerText || '').trim().toLowerCase();
        const aria = (d.getAttribute('aria-label') || '').toLowerCase();
        if (/^(verify|submit|next|continue|check)$/.test(t) || /verify|submit/.test(aria)) {
          const r = d.getBoundingClientRect();
          return { x: r.x + r.width / 2, y: r.y + r.height / 2, text: t || aria };
        }
      }
      return null;
    });
    if (!btn) return { error: 'no_submit_button' };
    await this.page.mouse.click(box.x + btn.x, box.y + btn.y, { delay: 50 });
    return { clicked: btn.text };
  }

  /** 读取最近一次成功的 token（供图像题提交后取回）。 */
  async readToken() {
    if (!this.page) return null;
    try {
      const tok = await this.page.evaluate(() => {
        const t = window.__hc_token || '';
        window.__hc_token = '';
        return t;
      });
      if (typeof tok === 'string' && tok.length > 200) {
        return { token: tok, ekey: '' };
      }
    } catch (_) {}
    return null;
  }

  /** 求解前的鼠标活动：在 widget 可见区域附近制造移动轨迹。 */
  async humanBeforeExecute(enabled) {
    if (!enabled || !this.page) return;
    try {
      let x = 300 + Math.random() * 500;
      let y = 260 + Math.random() * 300;
      await this.page.mouse.move(x, y);
      const steps = 22 + Math.floor(Math.random() * 20);
      for (let i = 0; i < steps; i += 1) {
        x += (Math.random() - 0.5) * 46;
        y += (Math.random() - 0.5) * 34;
        await this.page.mouse.move(x, y);
        await sleep(9 + Math.random() * 24);
      }
    } catch (_) {}
  }

  async close() {
    try { if (this.browser) await this.browser.close(); } catch (_) {}
    // merchant 模式的 origin 服务是进程级单例，由 shutdown 统一关闭；
    // loopback 模式每个会话独占一个随机端口，必须自己收。
    if (this._ownsOrigin) {
      try { if (this._originSrv) await this._originSrv.close(); } catch (_) {}
    }
  }
}

const sessions = new Map();

async function openSession({ proxy, ua }) {
  if (sessions.size >= MAX_SESSIONS) {
    // 淘汰最久未用的
    let oldest = null;
    for (const s of sessions.values()) {
      if (!oldest || s.usedAt < oldest.usedAt) oldest = s;
    }
    if (oldest && !oldest.busy) await dropSession(oldest.id);
  }
  const s = new Session({ proxy, ua });
  await s.open();
  sessions.set(s.id, s);
  return s;
}

async function dropSession(id) {
  const s = sessions.get(id);
  if (!s) return false;
  sessions.delete(id);
  await s.close();
  return true;
}

async function getOrOpen({ session_id, proxy, ua }) {
  if (session_id) {
    const s = sessions.get(session_id);
    if (s) return { session: s, owned: false };
  }
  const s = await openSession({ proxy, ua });
  return { session: s, owned: true };
}

// ── 后台清理 ────────────────────────────────────────────────
setInterval(() => {
  const t = now();
  for (const s of sessions.values()) {
    if (!s.busy && t - s.usedAt > SESSION_TTL_MS) {
      dropSession(s.id).catch(() => {});
    }
  }
}, 60000).unref();

// ── HTTP 服务 ──────────────────────────────────────────────
function readBody(req) {
  return new Promise((resolve) => {
    const chunks = [];
    req.on('data', (c) => chunks.push(c));
    req.on('end', () => {
      try {
        resolve(JSON.parse(Buffer.concat(chunks).toString() || '{}'));
      } catch (_) {
        resolve(null);
      }
    });
  });
}

const server = http.createServer(async (req, res) => {
  const send = (code, obj) => {
    res.writeHead(code, {
      'Content-Type': 'application/json; charset=utf-8',
      'Access-Control-Allow-Origin': '*',
    });
    res.end(JSON.stringify(obj));
  };

  const url = new URL(req.url, 'http://127.0.0.1');

  if (req.method === 'GET' && url.pathname === '/health') {
    let ver = 'unknown';
    try {
      const b = await launchBrowser();
      ver = await b.version();
      await b.close();
    } catch (e) {
      return send(503, { ok: false, error: String(e).slice(0, 160) });
    }
    return send(200, { ok: true, browser: ver, sessions: sessions.size });
  }

  if (req.method === 'GET' && url.pathname === '/sessions') {
    const list = [];
    for (const s of sessions.values()) {
      list.push({
        session_id: s.id, proxy: s.proxy ? 'set' : '', busy: s.busy,
        solves: s.solves, failures: s.failures, age_ms: now() - s.createdAt,
      });
    }
    return send(200, { sessions: list });
  }

  if (req.method === 'POST' && url.pathname === '/session') {
    const body = await readBody(req);
    if (!body) return send(400, { error: 'invalid_json' });
    try {
      const s = await openSession({ proxy: body.proxy, ua: body.ua });
      return send(200, { session_id: s.id, ua: s.ua, proxy: body.proxy || '' });
    } catch (e) {
      return send(500, { error: String(e).slice(0, 200) });
    }
  }

  if (req.method === 'DELETE' && url.pathname.startsWith('/session/')) {
    const id = url.pathname.slice('/session/'.length);
    const ok = await dropSession(id);
    return send(ok ? 200 : 404, { ok });
  }

  if (req.method === 'POST' && url.pathname === '/solve') {
    const body = await readBody(req);
    if (!body) return send(400, { error: 'invalid_json' });

    const t0 = now();
    let session = null;
    let owned = false;
    try {
      const got = await getOrOpen(body);
      session = got.session;
      owned = got.owned;
      session.busy = true;

      const out = await session.withLock(async () => {
        await session.humanBeforeExecute(body.human !== false);
        return session.solve({
          sitekey: body.sitekey || DEFAULT_SITEKEY,
          rqdata: body.rqdata || '',
          host: body.host || DEFAULT_HOST,
          timeout: Number(body.timeout || TOKEN_TIMEOUT_MS),
        });
      });

      if (out.token) {
        return send(200, {
          token: out.token,
          ekey: out.ekey || '',
          elapsed_ms: now() - t0,
          session_id: session.id,
          reused: !owned,
        });
      }
      // 出图像题：题目与九宫格一起回，上层可作答后回调 /challenge
      return send(502, {
        error: out.error || 'unknown',
        session_id: session.id,
        challenge: out.challenge || null,
      });
    } catch (e) {
      return send(500, { error: String(e).slice(0, 240) });
    } finally {
      if (owned && session) await dropSession(session.id);
      else if (session) session.busy = false;
    }
  }

  // 作答图像题：{ session_id, indices: [0,4], submit?: true, sitekey, rqdata, host }
  if (req.method === 'POST' && url.pathname === '/challenge') {
    const body = await readBody(req);
    if (!body) return send(400, { error: 'invalid_json' });
    const s = sessions.get(body.session_id || '');
    if (!s) return send(404, { error: 'no_session' });
    try {
      s.busy = true;
      const out = await s.withLock(async () => {
        const ans = await s.answerChallenge(Array.isArray(body.indices) ? body.indices : []);
        let sub = null;
        if (body.submit !== false) {
          await sleep(400 + Math.random() * 500);
          sub = await s.submitChallenge();
        }
        let tok = null;
        if (body.wait !== false) {
          // 提交后 hCaptcha 会自己走完，等 token
          const t0 = now();
          while (now() - t0 < Number(body.wait_ms || 45000)) {
            await sleep(1500);
            const got = await s.readToken();
            if (got && got.token) { tok = got; break; }
          }
        }
        return { answer: ans, submit: sub, token: tok };
      });
      return send(200, { ok: true, session_id: s.id, ...out });
    } catch (e) {
      return send(500, { error: String(e).slice(0, 240) });
    } finally {
      s.busy = false;
    }
  }

  send(404, { error: 'not_found' });
});

server.listen(PORT, HOST, () => {
  console.log(`hCaptcha solver listening on http://${HOST}:${PORT}`);
  console.log(`  chrome:   ${CHROME}`);
  console.log(`  sitekey:  ${DEFAULT_SITEKEY}`);
  console.log(`  host:     ${DEFAULT_HOST}`);
  console.log(`  sessions: max ${MAX_SESSIONS}, ttl ${SESSION_TTL_MS}ms`);
});

async function shutdown() {
  for (const id of [...sessions.keys()]) await dropSession(id);
  server.close(() => process.exit(0));
}
process.on('SIGTERM', shutdown);
process.on('SIGINT', shutdown);
