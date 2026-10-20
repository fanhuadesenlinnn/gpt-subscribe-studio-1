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
// Chrome 路径：优先环境变量，其次按平台推断
const CHROME = process.env.CHROME_PATH || (() => {
  const cands = [
    '/usr/bin/google-chrome',
    '/usr/bin/chromium',
    '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
    '/Applications/Chromium.app/Contents/MacOS/Chromium',
  ];
  for (const c of cands) {
    try { if (require('node:fs').existsSync(c)) return c; } catch (_) {}
  }
  return '/usr/bin/google-chrome';
})();
const DEFAULT_SITEKEY = process.env.HCAPTCHA_SITEKEY || '24ed0064-62cf-4d42-9960-5dd1a41d4e29';
const DEFAULT_HOST = process.env.HCAPTCHA_HOST || 'b.stripecdn.com';
const TOKEN_TIMEOUT_MS = Number(process.env.TOKEN_TIMEOUT_MS || 90000);
const SESSION_TTL_MS = Number(process.env.SESSION_TTL_MS || 600000);
const MAX_SESSIONS = Number(process.env.MAX_SESSIONS || 8);
const DEFAULT_UA =
  process.env.HCAPTCHA_UA ||
  'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 ' +
  '(KHTML, like Gecko) Chrome/155.0.0.0 Safari/537.36';

// 页面自己加载 hcaptcha.com/1/api.js，并暴露一个 __reaSolve 供 CDP 调用。
// 这是"直接求解"的关键：hcaptcha.execute 必须由加载了 api.js 的页面发起，
// 我们不在 Node 里拼 getcaptcha 的 wire（那一定 415，密文绑定 wasm 会话）。
const buildPageHtml = () => `<!doctype html><html><head><meta charset="utf-8">
<title>HCaptchaInvisible</title>
<script src="https://hcaptcha.com/1/api.js?onload=captchaLoad&render=explicit" async defer></script>
</head><body><div id="root"></div>
<script>
// 复刻真实 HCaptchaInvisible.html 的行为（来自 HCaptchaInvisible.<hash>.bundle.js）：
//   1) 用 window.captchaLoad 等 api.js 就绪
//   2) render 必须带 error-callback
//   3) execute 前把 stripe_* 行为信号注入 window（hCaptcha 的 identity 校验读这个）
var HOST_FOR_RENDER = '__MERCHANT_HOST__';
window.__reaReady = false;
window.__reaLib = new Promise(function (resolve) {
  if (window.hcaptcha) { window.__reaReady = true; return resolve(window.hcaptcha); }
  window.captchaLoad = function () { window.__reaReady = true; resolve(window.hcaptcha); };
});

// 与真实 bundle 相同的 key 清洗规则
function reaSanitize(v) { return String(v).replace(/\\W/g, '_'); }

// Stripe 通过 postMessage 送来的行为数据 -> window.stripe_<k>_<v>
window.__reaFraudSignals = [];
function reaInjectSignals(data) {
  if (!data || typeof data !== 'object') return;
  window.__reaFraudSignals.forEach(function (k) { try { delete window[k]; } catch (e) {} });
  window.__reaFraudSignals = [];
  Object.keys(data).forEach(function (k) {
    var name = ('stripe_' + reaSanitize(k) + '_' + reaSanitize(data[k])).substring(0, 10000);
    window[name] = null;          // 真实实现也是先置 null，值本身在 key 里编码
    window.__reaFraudSignals.push(name);
  });
}

// 监听父页面（Stripe）发来的消息，和真实实现一致
window.addEventListener('message', function (ev) {
  var m = ev.data;
  if (!m || typeof m !== 'object') return;
  if (m.tag === 'SEND_FRAUD_SIGNALS_HCAPTCHA_INVISIBLE' || (m.message && m.message.data)) {
    reaInjectSignals((m.message && m.message.data) || m.data);
  }
});

// ── 页面内自动答题器 ────────────────────────────────────────
// 必须在 execute 的生命周期内运行：hCaptcha 一旦被我们的超时切断，
// challenge 页面立即销毁，之后任何点击都无效。
// 这里用定时器盯着 challenge iframe，题目一出现就地分析并点击。
window.__reaAuto = {
  active: false,
  attempts: 0,
  log: [],
  timer: null,
  lastSig: '',

  start: function (opts) {
    if (this.active) return;
    this.active = true;
    this.attempts = 0;
    this.log = [];
    var self = this;
    var cfg = opts || {};
    this.timer = setInterval(function () { self.tick(cfg); }, Number(cfg.intervalMs || 400));
  },

  stop: function () {
    this.active = false;
    if (this.timer) { clearInterval(this.timer); this.timer = null; }
  },

  note: function (msg) {
    this.log.push(Date.now() + ' ' + msg);
    if (this.log.length > 60) this.log.shift();
  },

  // 找到 challenge iframe 的 document（同源才能读；跨域拿不到）
  challengeDoc: function () {
    var fs = document.querySelectorAll('iframe');
    for (var i = 0; i < fs.length; i++) {
      var src = fs[i].src || '';
      if (src.indexOf('frame=challenge') === -1) continue;
      try {
        var d = fs[i].contentDocument;
        if (d) return { doc: d, el: fs[i] };
      } catch (e) {}
    }
    return null;
  },

  tick: function (cfg) {
    if (!this.active || this.attempts >= (cfg.maxAttempts || 3)) return;
    var got = this.challengeDoc();
    if (!got) return;
    var doc = got.doc;

    // 题干文字
    var text = (doc.body && doc.body.innerText || '').replace(/\s+/g, ' ').slice(0, 200);
    if (!text) return;
    var sig = text.slice(0, 60) + '|' + doc.querySelectorAll('canvas').length;
    if (sig === this.lastSig) return;   // 同一道题不重复处理
    this.lastSig = sig;

    // 9 宫格题：能直接按格子点击（判据是物体类别，需要外部知识，这里先跳过）
    var cells = doc.querySelectorAll('[aria-label^="Challenge Image"]');
    if (cells.length >= 4) {
      this.note('9grid:' + cells.length + ' -> skip');
      var skip = doc.querySelector('[aria-label^="Skip Challenge"]');
      if (skip) { try { skip.click(); } catch (e) {} }
      return;
    }

    // 线条题：canvas 像素分析
    var cv = doc.querySelector('canvas');
    if (cv) {
      this.attempts += 1;
      var res = this.solveLines(cv, doc);
      this.note('lines:' + JSON.stringify(res && res.picked ? res.picked.length : 0));
      if (res && res.picked && res.picked.length >= 2) {
        this.clickAt(doc, res.picked);
      }
    }
  },

  // 线条题：高亮低饱和像素 -> 连通域 -> PCA 主轴长度 -> 最短两条
  solveLines: function (cv, doc) {
    try {
      var W = cv.width, H = cv.height;
      var ctx = cv.getContext('2d');
      var img = ctx.getImageData(0, 0, W, H).data;

      // 1) 生成掩码
      var mask = new Uint8Array(W * H);
      var cnt = 0;
      for (var y = 0; y < H; y++) {
        for (var x = 0; x < W; x++) {
          var i = (y * W + x) * 4;
          var r = img[i], g = img[i + 1], b = img[i + 2];
          var mx = Math.max(r, g, b), mn = Math.min(r, g, b);
          var v = mx, s = mx === 0 ? 0 : Math.round((mx - mn) * 255 / mx);
          // 线条：亮（v>150）且低饱和（s<110）
          if (v > 150 && s < 110) { mask[y * W + x] = 1; cnt++; }
        }
      }
      if (cnt < 500) return { err: 'few_pixels', cnt: cnt };

      // 2) 连通域（4 邻域，迭代栈）
      var lab = new Int32Array(W * H);
      var comps = [];
      var cur = 0;
      var stack = new Int32Array(W * H);
      for (var p0 = 0; p0 < W * H; p0++) {
        if (!mask[p0] || lab[p0]) continue;
        cur++;
        var sp = 0; stack[sp++] = p0; lab[p0] = cur;
        var xs = [], ys = [];
        while (sp > 0) {
          var q = stack[--sp];
          var qx = q % W, qy = (q / W) | 0;
          xs.push(qx); ys.push(qy);
          var nb = [q - 1, q + 1, q - W, q + W];
          for (var k = 0; k < 4; k++) {
            var nq = nb[k];
            if (nq < 0 || nq >= W * H) continue;
            if (k < 2 && (Math.abs((nq % W) - qx) !== 1)) continue;
            if (!mask[nq] || lab[nq]) continue;
            lab[nq] = cur; stack[sp++] = nq;
          }
        }
        if (xs.length < 300) continue;
        // PCA
        var mx2 = 0, my2 = 0;
        for (var t = 0; t < xs.length; t++) { mx2 += xs[t]; my2 += ys[t]; }
        mx2 /= xs.length; my2 /= xs.length;
        var sxx = 0, syy = 0, sxy = 0;
        for (var t2 = 0; t2 < xs.length; t2++) {
          var dx = xs[t2] - mx2, dy = ys[t2] - my2;
          sxx += dx * dx; syy += dy * dy; sxy += dx * dy;
        }
        sxx /= xs.length; syy /= xs.length; sxy /= xs.length;
        var tr = sxx + syy, det = sxx * syy - sxy * sxy;
        var disc = Math.sqrt(Math.max(0, tr * tr / 4 - det));
        var l1 = tr / 2 + disc;
        var span = Math.sqrt(Math.max(0, l1)) * 3.46;   // ≈ 6σ
        comps.push({ n: xs.length, span: span, cx: mx2, cy: my2 });
      }
      if (comps.length < 2) return { err: 'few_comps', comps: comps.length };
      comps.sort(function (a, b) { return a.span - b.span; });
      // 取最短两条，坐标换算成 canvas 内的点击点
      var picked = comps.slice(0, 2).map(function (c) {
        return [c.cx / W, c.cy / H];
      });
      return { picked: picked, comps: comps.length,
               spans: comps.slice(0, 6).map(function (c) { return Math.round(c.span); }) };
    } catch (e) {
      return { err: String(e).slice(0, 100) };
    }
  },

  // 在 challenge iframe 内按 canvas 归一化坐标派发点击
  clickAt: function (doc, coords) {
    var cv = doc.querySelector('canvas');
    if (!cv) return;
    var r = cv.getBoundingClientRect();
    for (var i = 0; i < coords.length; i++) {
      var cx = r.left + coords[i][0] * r.width;
      var cy = r.top + coords[i][1] * r.height;
      var tgt = doc.elementFromPoint(cx, cy) || cv;
      ['pointerdown', 'mousedown', 'pointerup', 'mouseup', 'click'].forEach(function (type) {
        var isPointer = type.indexOf('pointer') === 0;
        var Ev = isPointer ? (window.PointerEvent || MouseEvent) : MouseEvent;
        tgt.dispatchEvent(new Ev(type, {
          bubbles: true, cancelable: true, clientX: cx, clientY: cy,
          view: doc.defaultView, button: 0,
        }));
      });
      this.note('click ' + Math.round(cx) + ',' + Math.round(cy));
    }
    // 点击后找提交按钮
    var self = this;
    setTimeout(function () {
      var btns = doc.querySelectorAll('div[role=button],button,div.button');
      for (var b = 0; b < btns.length; b++) {
        var tt = (btns[b].innerText || '').trim().toLowerCase();
        if (/^(verify|submit|next|continue)$/.test(tt)) {
          try { btns[b].click(); self.note('submit'); } catch (e) {}
          break;
        }
      }
    }, 700);
  }
};

window.__reaSolve = function (sitekey, rqdata, timeoutMs, fraudData, opts) {
  var REA_OPTS = opts || {};
  return window.__reaLib.then(function (hc) {
    if (!hc || typeof hc.render !== 'function') {
      return { error: 'no_hcaptcha' };
    }
    if (fraudData) reaInjectSignals(fraudData);
    // 每次求解前清掉上一次的 widget 容器与残留 iframe。
    // 实测：不清会让 hCaptcha 复用被污染的实例，下一轮直接出题或超时。
    try {
      var prevWidgets = window.__reaWidgets || [];
      for (var wi = 0; wi < prevWidgets.length; wi++) {
        try { hc.remove(prevWidgets[wi]); } catch (e) {}
      }
      window.__reaWidgets = [];
      var stray = document.querySelectorAll('iframe[src*="newassets.hcaptcha.com"]');
      for (var si = 0; si < stray.length; si++) {
        try { stray[si].parentNode && stray[si].parentNode.removeChild(stray[si]); } catch (e) {}
      }
      var olds = document.querySelectorAll('[id^="hcaptcha-invisible-"]');
      for (var oi = 0; oi < olds.length; oi++) {
        try { olds[oi].parentNode && olds[oi].parentNode.removeChild(olds[oi]); } catch (e) {}
      }
    } catch (e) {}
    var div = document.createElement('div');
    div.id = 'hcaptcha-invisible-' + sitekey + '-' + Math.random().toString(36).slice(2, 8);
    var root = document.getElementById('root') || document.body;
    root.appendChild(div);
    var rid;
    try {
      rid = hc.render(div, {
        sitekey: sitekey,
        size: 'invisible',
        host: HOST_FOR_RENDER,      // 与真实 Stripe 页一致；缺失会让 sitekey 域名校验不匹配
        'error-callback': function (e) { window.__reaRenderError = String(e).slice(0, 200); },
      });
    } catch (e) {
      return { error: 'render_failed: ' + String(e).slice(0, 160) };
    }
    try {
      window.__reaWidgets = window.__reaWidgets || [];
      window.__reaWidgets.push(rid);
    } catch (e) {}
    return new Promise(function (resolve) {
      // 给 hCaptcha 内部足够时间完成 identity 采样
      setTimeout(function () {
        var opts = { async: true };
        if (rqdata) opts.rqdata = rqdata;
        var t0 = Date.now();
        var done = false;
        // 启动页面内自动答题器：必须与 execute 并行，
        // 因为 challenge 的存活期绑定在 execute 的 promise 上。
        try {
          if (REA_OPTS.noAuto !== true && window.__reaAuto && !window.__reaAuto.active) {
            window.__reaAuto.start({ intervalMs: 350, maxAttempts: 3 });
          }
        } catch (e) {}
        // 超时后把 promise 保留在 window 上，供外部答题后再等结果。
        // 直接 resolve 会让 hCaptcha 的 execute 变成孤儿，页面被回收后 canvas 就没了。
        window.__reaPending = { rid: rid, t0: t0, el: null, token: null, error: null };
        var timer = setTimeout(function () {
          if (!done) {
            done = true;
            var frames = [];
            try {
              var fs = document.querySelectorAll('iframe');
              for (var i = 0; i < fs.length; i++) {
                var m = (fs[i].src || '').match(/frame=([a-z-]+)/);
                frames.push(m ? m[1] : 'main');
              }
            } catch (e) {}
            // 不 resolve 掉 execute，留给 window.__reaWait 继续等
            resolve({ error: 'execute_timeout', pending: true,
                      elapsedMs: Date.now() - t0,
                      iframes: frames, bodyLen: document.body.innerHTML.length });
          }
        }, timeoutMs || 90000);
        hc.execute(rid, opts).then(function (v) {
          var tok = (v && v.response) || v;
          var good = typeof tok === 'string' && tok.length > 100;
          try { if (window.__reaAuto) window.__reaAuto.stop(); } catch (e) {}
          if (window.__reaPending) {
            window.__reaPending.token = good ? tok : null;
            window.__reaPending.ekey = (v && (v.ekey || v.key)) || '';
            window.__reaPending.error = good ? null : 'short_token';
          }
          if (done) return;
          done = true; clearTimeout(timer);
          if (!good) return resolve({ error: 'short_token: ' + String(tok).slice(0, 80) });
          resolve({ token: tok, ekey: (v && (v.ekey || v.key)) || '', elapsedMs: Date.now() - t0 });
        }, function (e) {
          if (window.__reaPending) window.__reaPending.error = String(e).slice(0, 200);
          if (done) return;
          done = true; clearTimeout(timer);
          resolve({ error: 'execute_failed: ' + String(e).slice(0, 200) });
        });
      }, 3000);
    });
  });
};
</script></body></html>`;
// 延迟求值：PAGE_HTML 依赖 MERCHANT_HOST，而后者在下方才定义
// 精简页：与 tools/rea-chrome/ip_ab_test.cjs 的 PAGE 完全一致。
// 实测 AB 页 14/15 成功，而完整页 0/N，用 SIMPLE_PAGE=true 切换对比。
const buildSimplePage = (host) => `<!doctype html><html><head><meta charset="utf-8">
<title>HCaptchaInvisible</title>
<script src="https://hcaptcha.com/1/api.js?onload=captchaLoad&render=explicit" async defer></script>
</head><body><div id="root"></div>
<script>
window.__lib = new Promise(function (resolve) {
  if (window.hcaptcha) return resolve(window.hcaptcha);
  window.captchaLoad = function () { resolve(window.hcaptcha); };
});
window.__reaSolve = function (sk, rq, tmo) {
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

const SIMPLE_PAGE = process.env.SIMPLE_PAGE === 'true';

const PAGE_HTML_GET = () => (SIMPLE_PAGE
  ? buildSimplePage(MERCHANT_HOST)
  : buildPageHtml().replace('__MERCHANT_HOST__', MERCHANT_HOST));

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

// merchant 模式下页面必须是 HCaptchaInvisible.html 这个路径：
// 真实的 Stripe 结账就是把 hCaptcha 挂在这个路径上，sitekey 的白名单校验会看
// location.pathname。用根路径 / 也能加载 api.js，但 host 校验与真实环境不一致。
const MERCHANT_PATH = process.env.MERCHANT_PATH
  || '/stripethirdparty-srv/assets/v35.5/HCaptchaInvisible.html';

function startOriginServer() {
  if (_originSingleton) return Promise.resolve(_originSingleton);
  if (ORIGIN_MODE === 'merchant') {
    return new Promise((resolve, reject) => {
      const tls = ensureCert(MERCHANT_HOST);
      const server = require('node:https').createServer(tls, (req, res) => {
        res.writeHead(200, { 'Content-Type': 'text/html; charset=utf-8' });
        res.end(PAGE_HTML_GET());
      });
      server.on('error', reject);
      server.listen(MERCHANT_PORT, '127.0.0.1', () => {
        _originSingleton = {
          origin: `https://${MERCHANT_HOST}${MERCHANT_PATH}`,
          close: () => new Promise((r) => server.close(() => r())),
        };
        resolve(_originSingleton);
      });
    });
  }
  return new Promise((resolve) => {
    const server = http.createServer((req, res) => {
      res.writeHead(200, { 'Content-Type': 'text/html; charset=utf-8' });
      res.end(PAGE_HTML_GET());
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
    headless: process.env.HEADLESS === 'true' ? 'shell' : false,
    args: [
      '--no-sandbox',
      '--disable-dev-shm-usage',
      // 渲染后端：默认走 Mesa llvmpipe（--use-angle=gl），而不是 SwiftShader。
      // 原因：SwiftShader 是纯软渲染，除了 GL 字符串能被 JS 伪装，
      // readPixels 耗时、帧率、合成延迟这些旁路信号仍是软渲染特征。
      // llvmpipe 也是 CPU 渲染，但走 Mesa 的 GL 栈，时序分布更接近真机。
      // 可用 GPU_BACKEND=swiftshader 回退。
      ...(process.env.GPU_BACKEND === 'swiftshader'
        ? ['--use-gl=angle', '--use-angle=swiftshader', '--enable-unsafe-swiftshader']
        : ['--use-gl=angle', '--use-angle=gl', '--enable-unsafe-swiftshader',
           '--enable-features=Vulkan', '--disable-gpu-sandbox']),
      '--ignore-gpu-blocklist',
      '--disable-blink-features=AutomationControlled',
      // 注意：不要加 --disable-features=IsolateOrigins,site-per-process
      // 它会让所有 iframe 与主页面共享进程，hCaptcha 的 wasm/vm 在共享进程里
      // 行为与真实浏览器（按站点隔离）不同，实测该参数存在时 challenge 必出题。
      ...(process.env.NO_ISOLATE === 'true'
        ? ['--disable-features=IsolateOrigins,site-per-process'] : []),
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

    // 关键：用 CDP 的 Emulation.setUserAgentOverride 带 userAgentMetadata，
    // 让 navigator.userAgentData 由 Chrome 引擎自己生成（含 brand/版本/平台/架构全套），
    // 而不是靠 JS 覆盖。JS 覆盖的 userAgentData 缺少 fullVersionList、
    // getHighEntropyValues 的真实实现，是能被识别的伪造。
    try {
      const cdp = await this.page.createCDPSession();
      await cdp.send('Emulation.setUserAgentOverride', {
        userAgent: this.ua,
        acceptLanguage: 'en-US,en',
        platform: 'Win32',
        userAgentMetadata: {
          brands: [
            { brand: 'Google Chrome', version: '155' },
            { brand: 'Chromium', version: '155' },
            { brand: 'Not=A?Brand', version: '24' },
          ],
          fullVersionList: [
            { brand: 'Google Chrome', version: '155.0.8059.84' },
            { brand: 'Chromium', version: '155.0.8059.84' },
            { brand: 'Not=A?Brand', version: '24.0.0.0' },
          ],
          fullVersion: '155.0.8059.84',
          platform: 'Windows',
          platformVersion: '10.0.0',
          architecture: 'x86',
          model: '',
          mobile: false,
          bitness: '64',
          wow64: false,
        },
      });
      this._cdp = cdp;
      console.log('   CDP UA override（含 userAgentMetadata）已应用');
    } catch (e) {
      console.log('   CDP UA override 失败，回退 setUserAgent: ' + String(e).slice(0, 80));
      await this.page.setUserAgent(this.ua);
    }
    await this.page.setExtraHTTPHeaders({
      'Accept-Language': 'en-US,en;q=0.9',
    });

    // 反自动化痕迹：webdriver 与插件数组
    // 环境一致性伪装。实测服务器 Chrome 有三处自相矛盾：
    //   UA 说 Windows / platform 是 Linux x86_64
    //   GPU 是 SwiftShader 软件渲染
    //   navigator.userAgentData.brands 为空数组
    // hCaptcha 的 identity 校验会看这些。全部对齐到「Windows 10 + 真实 NVIDIA GPU」。
    await this.page.evaluateOnNewDocument(() => {
      const UA_WIN = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 ' +
                     '(KHTML, like Gecko) Chrome/155.0.0.0 Safari/537.36';

      // 1) webdriver 必须是 false（不是 undefined —— 真实浏览器有这个属性且为 false）
      Object.defineProperty(navigator, 'webdriver', { get: () => false, configurable: true });

      // 2) platform 跟 UA 对齐
      Object.defineProperty(navigator, 'platform', { get: () => 'Win32', configurable: true });
      Object.defineProperty(navigator, 'vendor', { get: () => 'Google Inc.', configurable: true });
      Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'], configurable: true });

      // 3) userAgentData 交给 CDP 的 Emulation.setUserAgentOverride 生成（原生值），
      //    JS 层不再伪造，避免与原生实现冲突导致特征不一致。

      // 4) 真实性旁证：plugins / mimeTypes 有非空项
      const fakePlugins = [
        { name: 'PDF Viewer', filename: 'internal-pdf-viewer', description: 'Portable Document Format' },
        { name: 'Chrome PDF Viewer', filename: 'internal-pdf-viewer', description: 'Portable Document Format' },
        { name: 'Chromium PDF Viewer', filename: 'internal-pdf-viewer', description: 'Portable Document Format' },
        { name: 'Microsoft Edge PDF Viewer', filename: 'internal-pdf-viewer', description: 'Portable Document Format' },
        { name: 'WebKit built-in PDF', filename: 'internal-pdf-viewer', description: 'Portable Document Format' },
      ];
      Object.defineProperty(navigator, 'plugins', {
        get: () => {
          // Plugin 实例属性只读，用普通对象模拟（navigator.plugins 只需可枚举 + name/filename/description）
          const arr = fakePlugins.map((p) => ({ ...p }));
          arr.item = (i) => arr[i];
          arr.namedItem = (n) => arr.find((p) => p.name === n) || null;
          return arr;
        },
        configurable: true,
      });
      Object.defineProperty(navigator, 'mimeTypes', {
        get: () => {
          const arr = [
            { type: 'application/pdf', suffixes: 'pdf', description: 'Portable Document Format' },
            { type: 'text/pdf', suffixes: 'pdf', description: 'Portable Document Format' },
          ];
          arr.item = (i) => arr[i];
          arr.namedItem = (n) => arr.find((m) => m.type === n) || null;
          return arr;
        },
        configurable: true,
      });

      // 5) WebGL 真实 GPU 字符串（SwiftShader 是明显特征）
      const patchWebGL = () => {
        const proto = window.WebGLRenderingContext && window.WebGLRenderingContext.prototype;
        if (!proto) return;
        const orig = proto.getParameter;
        proto.getParameter = function (p) {
          if (p === 37445) return 'Google Inc. (NVIDIA)';                 // UNMASKED_VENDOR_WEBGL
          if (p === 37446) return 'ANGLE (NVIDIA, NVIDIA GeForce RTX 3060 Direct3D11 vs_5_0 ps_5_0, D3D11)';
          return orig.call(this, p);
        };
      };
      patchWebGL();

      // 6) chrome 对象（真实 Chrome 有 runtime 等）
      if (!window.chrome) {
        window.chrome = {
          runtime: {}, loadTimes: () => {}, csi: () => {},
          app: { isInstalled: false, InstallState: { DISABLED: 'disabled' }, RunningState: { RUNNING: 'running' } },
        };
      }

      // 7) 硬件指标与 UA 平台一致
      Object.defineProperty(navigator, 'hardwareConcurrency', { get: () => 8, configurable: true });
      Object.defineProperty(navigator, 'deviceMemory', { get: () => 8, configurable: true });
      Object.defineProperty(navigator, 'maxTouchPoints', { get: () => 0, configurable: true });

      // 8) permissions.query 要能正常返回（headless 常见缺口）
      if (navigator.permissions && navigator.permissions.query) {
        const oq = navigator.permissions.query.bind(navigator.permissions);
        navigator.permissions.query = (p) => (p && p.name === 'notifications'
          ? Promise.resolve({ state: Notification.permission, onchange: null })
          : oq(p));
      }
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

    // 页面 HTML 已经自带 hcaptcha.com/1/api.js，等它就绪即可；
    // 不要再插一次 api.js（两个 onload 回调会互相覆盖）。
    const loaded = await this.page.evaluate(() => new Promise((resolve) => {
      if (typeof window.hcaptcha === 'object') return resolve(true);
      let settled = false;
      const done = (v) => { if (!settled) { settled = true; resolve(v); } };
      // 页面自己的 captchaLoad 回调就是权威就绪信号
      const prev = window.captchaLoad;
      window.captchaLoad = function () {
        try { if (typeof prev === 'function') prev.apply(this, arguments); } catch (_) {}
        window.__reaReady = true;
        done(true);
      };
      // 兜底：轮询
      const t0 = Date.now();
      const iv = setInterval(() => {
        if (typeof window.hcaptcha === 'object') { clearInterval(iv); return done(true); }
        if (Date.now() - t0 > 30000) { clearInterval(iv); done(false); }
      }, 300);
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

  /**
   * 给 challenge frame 预注入自动答题器。
   * challenge iframe 跨域，主页面读不到 contentDocument，
   * 只能在 frame 自己的 JS 上下文里跑分析逻辑。
   */
  async installFrameSolver() {
    const script = String.raw`(() => {
      if (window.__frameAuto) return 'already';
      window.__frameAuto = { log: [], done: false, timer: null, count: 0, lastBodySig: '', lastSig: '' };

      function note(m) {
        window.__frameAuto.log.push(Date.now() + ' ' + m);
        if (window.__frameAuto.log.length > 50) window.__frameAuto.log.shift();
      }

      function fire(el, type, x, y) {
        const isPtr = type.indexOf('pointer') === 0;
        const Ev = isPtr ? (window.PointerEvent || MouseEvent) : MouseEvent;
        el.dispatchEvent(new Ev(type, {
          bubbles: true, cancelable: true, clientX: x, clientY: y,
          view: window, button: 0, buttons: type.indexOf('down') >= 0 ? 1 : 0,
        }));
      }

      function clickAt(x, y) {
        const el = document.elementFromPoint(x, y) || document.body;
        ['mousedown', 'mouseup', 'click'].forEach((t) => fire(el, t, x, y));
      }

      function solveLines(cv) {
        const W = cv.width, H = cv.height;
        const ctx = cv.getContext('2d');
        let data;
        try { data = ctx.getImageData(0, 0, W, H).data; }
        catch (e) { return { err: 'tainted' }; }
        const mask = new Uint8Array(W * H);
        let cnt = 0;
        for (let y = 0; y < H; y++) {
          for (let x = 0; x < W; x++) {
            const i = (y * W + x) * 4;
            const r = data[i], g = data[i + 1], b = data[i + 2];
            const mx = Math.max(r, g, b), mn = Math.min(r, g, b);
            const v = mx, s = mx === 0 ? 0 : Math.round((mx - mn) * 255 / mx);
            if (v > 150 && s < 110) { mask[y * W + x] = 1; cnt++; }
          }
        }
        if (cnt < 500) return { err: 'few_px', cnt };
        const lab = new Int32Array(W * H);
        const comps = [];
        let cur = 0;
        const st = new Int32Array(W * H);
        for (let p0 = 0; p0 < W * H; p0++) {
          if (!mask[p0] || lab[p0]) continue;
          cur++;
          let sp = 0; st[sp++] = p0; lab[p0] = cur;
          const xs = [], ys = [];
          while (sp > 0) {
            const q = st[--sp];
            const qx = q % W, qy = (q / W) | 0;
            xs.push(qx); ys.push(qy);
            const nb = [q - 1, q + 1, q - W, q + W];
            for (let k = 0; k < 4; k++) {
              const nq = nb[k];
              if (nq < 0 || nq >= W * H) continue;
              if (k < 2 && Math.abs((nq % W) - qx) !== 1) continue;
              if (!mask[nq] || lab[nq]) continue;
              lab[nq] = cur; st[sp++] = nq;
            }
          }
          // 过滤碎片：线条面积通常 >600，且要有一定跨度
          if (xs.length < 600) continue;
          let mxx = 0, myy = 0;
          for (let t = 0; t < xs.length; t++) { mxx += xs[t]; myy += ys[t]; }
          mxx /= xs.length; myy /= xs.length;
          let sxx = 0, syy = 0, sxy = 0;
          for (let t2 = 0; t2 < xs.length; t2++) {
            const dx = xs[t2] - mxx, dy = ys[t2] - myy;
            sxx += dx * dx; syy += dy * dy; sxy += dx * dy;
          }
          sxx /= xs.length; syy /= xs.length; sxy /= xs.length;
          const tr = sxx + syy, det = sxx * syy - sxy * sxy;
          const disc = Math.sqrt(Math.max(0, tr * tr / 4 - det));
          const l1 = tr / 2 + disc;
          const span = Math.sqrt(Math.max(0, l1)) * 3.46;
          // 真线条跨度通常 > 60px（canvas 内部坐标，DPR2 下即 30 CSS px）
          if (span < 60) continue;
          // 排除接近方形的大块（背景反光）
          const fill = xs.length / Math.max(1, (Math.max(...xs) - Math.min(...xs) + 1) *
                                              (Math.max(...ys) - Math.min(...ys) + 1));
          if (fill > 0.45) continue;
          comps.push({ n: xs.length, span, cx: mxx, cy: myy, fill: Math.round(fill * 100) });
        }
        if (comps.length < 2) return { err: 'few_comps', n: comps.length };
        comps.sort((a, b) => a.span - b.span);
        return { comps: comps.length, spans: comps.slice(0, 6).map((c) => Math.round(c.span)),
                 picked: comps.slice(0, 2).map((c) => [c.cx, c.cy]) };
      }

      function tick() {
        if (window.__frameAuto.done) return;
        if (window.__frameAuto.count > 12) { window.__frameAuto.done = true; return; }
        const bodySig = (document.body.innerText || '').replace(/\s+/g, ' ').slice(0, 60);
        if (bodySig && bodySig === window.__frameAuto.lastBodySig) return;
        const cvs = document.querySelectorAll('canvas');
        const grids = document.querySelectorAll('[aria-label^="Challenge Image"]');
        if (grids.length >= 4) {
          note('grid:' + grids.length);
          const skip = document.querySelector('[aria-label^="Skip Challenge"]');
          if (skip) {
            try { skip.click(); note('skip'); } catch (e) {}
          }
          // skip 之后 hCaptcha 会出新题，不能置 done，等下一轮 tick 重新判断
          window.__frameAuto.lastGridSig = grids.length + ':' + Date.now();
          return;
        }
        if (!cvs.length) return;
        const cv = cvs[0];
        window.__frameAuto.count += 1;
        window.__frameAuto.lastBodySig = bodySig;
        const res = solveLines(cv);
        if (res.err) { note('lines:' + res.err); return; }
        note('spans:' + res.spans.join(','));
        const r = cv.getBoundingClientRect();
        res.picked.forEach(([px, py]) => {
          const x = r.left + (px / cv.width) * r.width;
          const y = r.top + (py / cv.height) * r.height;
          clickAt(x, y);
          note('click:' + Math.round(x) + ',' + Math.round(y));
        });
        // 记下本次答题的题面签名，避免对同一道题重复点击
        window.__frameAuto.lastSig = (document.body.innerText || '').slice(0, 50) + '|' + res.spans.join(',');
        setTimeout(() => {
          const btns = document.querySelectorAll('div[role=button],button,div.button');
          for (const b of btns) {
            const t = (b.innerText || '').trim().toLowerCase();
            if (/^(verify|submit|next|continue)$/.test(t)) {
              try { b.click(); note('submit'); } catch (e) {}
              break;
            }
          }
        }, 800);
      }

      window.__frameAuto.timer = setInterval(tick, 400);
      setTimeout(() => { try { clearInterval(window.__frameAuto.timer); } catch (e) {} }, 45000);
      return 'installed';
    })()`;
    let n = 0;
    for (const f of this.page.frames()) {
      if (!/challenge|newassets/.test(f.url())) continue;
      try { await f.evaluate(script); n += 1; } catch (_) {}
    }
    return n;
  }

  async solve({ sitekey, rqdata, host, timeout, inject = true }) {
    this.usedAt = now();
    this.solves += 1;

    // frame 级答题器必须在最开头启动：challenge iframe 是 __reaSolve
    // 内部 render 之后才创建的，注入器要一直在跑才能赶上。
    // 之前放在 direct 分支之后，永远走不到（direct 会 return）。
    const injector = inject
      ? setInterval(() => {
          this.installFrameSolver().catch(() => {});
        }, 600)
      : null;
    const stopInjector = () => {
      try { if (injector) clearInterval(injector); } catch (_) {}
    };
    setTimeout(stopInjector, 120000);

    // 首选：页面自己的 __reaSolve（HCaptchaInvisible.html 内，加载了 api.js）。
    // 这条路径实测 300-370ms 出 token，不需要图像题。
    try {
      const direct = await this.page.evaluate(
        (cfg) => (typeof window.__reaSolve === 'function'
          ? window.__reaSolve(cfg.sitekey, cfg.rqdata, cfg.timeout, null, cfg.opts)
          : Promise.resolve(null)),
        { sitekey: sitekey || DEFAULT_SITEKEY, rqdata: rqdata || '',
          timeout: timeout || TOKEN_TIMEOUT_MS,
          opts: { noAuto: inject === false } },
      );
      if (direct && direct.token) {
        stopInjector();
        return { token: direct.token, ekey: direct.ekey || '' };
      }
      // 有结果但不是 token：记录原因后立刻返回，不再跑兜底。
      // 兜底会让总耗时翻倍（90+90=180s）并掩盖 __reaSolve 的真实失败原因。
      if (direct && direct.error) {
        const ch = await this.captureChallenge().catch(() => null);
        stopInjector();
        return { error: direct.error, challenge: ch || undefined };
      }
      if (direct && direct.timeout) {
        const ch = await this.captureChallenge().catch(() => null);
        stopInjector();
        return { error: 'execute_timeout', challenge: ch || undefined };
      }
    } catch (_) {}

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
    stopInjector();
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
      // 完整导出 canvas（题目可能是 canvas 渲染的线条/图片）
      const canvases = [...document.querySelectorAll('canvas')].map((c, i) => {
        try {
          return { i, w: c.width, h: c.height, dataUrl: c.toDataURL('image/png') };
        } catch (e) { return { i, err: String(e).slice(0, 60) }; }
      });
      // 9 宫格图可能用 background-image（hcaptcha.com / blob: / data:）
      const bgs = [...document.querySelectorAll('div,span,a,button,[role=button]')]
        .map((d) => {
          const bi = getComputedStyle(d).backgroundImage || '';
          const r = d.getBoundingClientRect();
          const m = bi.match(/url\("?([^")]+)"?\)/);
          return m ? { src: m[1], x: Math.round(r.x), y: Math.round(r.y),
                       w: Math.round(r.width), h: Math.round(r.height) } : null;
        })
        .filter((v) => v && v.src && /hcaptcha|newassets|blob:|data:image/.test(v.src));
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
      return { prompt, header, imgs, bgImages: bgs, canvases, cells, frameUrl: location.href.slice(0, 160) };
    }).catch(() => null);
  }

  /**
   * 按 challenge 图像内的相对坐标点击（0..1 归一化）。
   * 用于任意位置的题目（如「点击两条较短的线」），不依赖 9 宫格布局。
   * coords: [[xN, yN], ...]  xN/yN 是相对于题目 canvas 的归一化坐标。
   */
  async answerChallengeAt(coords) {
    const frameEl = await this.page.$('iframe[src*="frame=challenge"]');
    if (!frameEl) return { error: 'no_challenge_iframe' };
    const box = await frameEl.boundingBox();
    if (!box) return { error: 'no_iframe_box' };
    // challenge 可能有多层嵌套 iframe，逐个试，找到含大 canvas 的那一层
    const cands = this.page.frames().filter((f) => /challenge|newassets/.test(f.url()));
    let fr = null;
    let canvasInfo = null;
    for (const f of cands) {
      try {
        const info = await f.evaluate(() => {
          const all = [...document.querySelectorAll('canvas')].map((c) => {
            const r = c.getBoundingClientRect();
            return { x: r.x, y: r.y, w: r.width, h: r.height, cw: c.width, ch: c.height };
          });
          const big = all.filter((c) => c.w > 80 && c.h > 80)
            .sort((a, b) => (b.w * b.h) - (a.w * a.h));
          return { count: all.length, all: all.slice(0, 6), picked: big[0] || null,
                   href: location.href.slice(0, 100) };
        });
        if (info && info.picked) { fr = f; canvasInfo = info; break; }
        if (!canvasInfo || (info && info.count > (canvasInfo.count || 0))) {
          canvasInfo = info; fr = fr || f;
        }
      } catch (_) {}
    }
    if (!fr) return { error: 'no_challenge_frame' };

    const canvas = canvasInfo && canvasInfo.picked;
    if (!canvas) return { error: 'no_canvas', diag: canvasInfo, framesChecked: cands.length };

    const picked = [];
    for (const [nx, ny] of coords) {
      const cx = canvas.x + nx * canvas.w;
      const cy = canvas.y + ny * canvas.h;
      const ax = box.x + cx;
      const ay = box.y + cy;
      await this.page.mouse.move(ax + (Math.random() - 0.5) * 4, ay + (Math.random() - 0.5) * 4);
      await sleep(80 + Math.random() * 120);
      await this.page.mouse.click(ax, ay, { delay: 40 + Math.random() * 50 });
      picked.push({ nx, ny, ax: Math.round(ax), ay: Math.round(ay) });
      await sleep(400 + Math.random() * 500);
    }
    return { picked, canvas, iframe: { x: Math.round(box.x), y: Math.round(box.y) } };
  }

  /**
   * 按 challenge iframe 内的归一化坐标点击（0..1）。
   * 用于 9 宫格题：格子不是 button 元素、也没有 canvas 时按比例定位。
   */
  async clickInFrame(coords) {
    const frameEl = await this.page.$('iframe[src*="frame=challenge"]');
    if (!frameEl) return { error: 'no_challenge_iframe' };
    const box = await frameEl.boundingBox();
    if (!box) return { error: 'no_iframe_box' };
    const picked = [];
    for (const [nx, ny] of coords) {
      const ax = box.x + nx * box.width;
      const ay = box.y + ny * box.height;
      await this.page.mouse.move(ax + (Math.random() - 0.5) * 3,
                                 ay + (Math.random() - 0.5) * 3);
      await sleep(70 + Math.random() * 90);
      await this.page.mouse.click(ax, ay, { delay: 40 + Math.random() * 40 });
      picked.push({ nx, ny, ax: Math.round(ax), ay: Math.round(ay) });
      await sleep(350 + Math.random() * 400);
    }
    return { picked, iframe: { x: Math.round(box.x), y: Math.round(box.y),
                               w: Math.round(box.width), h: Math.round(box.height) } };
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
      // 只要 9 宫格那种方形格子：尺寸接近正方形、aria 以 Challenge Image 开头
      .filter((c) => c.w >= 80 && c.h >= 80
        && Math.abs(c.w - c.h) < 24
        && /challenge image/i.test(c.aria)));

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

  /** 读取 __reaPending 里保存的结果（超时后 execute 仍可能在跑）。 */
  async readPendingToken() {
    if (!this.page) return null;
    try {
      const out = await this.page.evaluate(() => {
        const p = window.__reaPending;
        if (!p) return null;
        return { token: p.token || null, ekey: p.ekey || '', error: p.error || null };
      });
      if (out && out.token) {
        await this.page.evaluate(() => { window.__reaPending = null; });
        return out;
      }
      return out;
    } catch (_) { return null; }
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

  // 调试用：在会话页面里执行任意 JS（仅本机 127.0.0.1 监听，供逆向 / 环境对比）
  if (req.method === 'POST' && url.pathname === '/eval') {
    const body = await readBody(req);
    if (!body || !body.session_id) return send(400, { error: 'need session_id' });
    const s = sessions.get(body.session_id);
    if (!s) return send(404, { error: 'no_session' });
    try {
      s.busy = true;
      const out = await s.withLock(() => s.page.evaluate(
        (expr) => {
          // eslint-disable-next-line no-new-func
          const fn = new Function('return (' + expr + ')');
          const v = fn();
          return Promise.resolve(v).then((x) => ({ ok: true, value: x }));
        },
        String(body.expr || 'null'),
      ));
      return send(200, out);
    } catch (e) {
      return send(500, { ok: false, error: String(e).slice(0, 400) });
    } finally {
      s.busy = false;
    }
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
          inject: body.inject !== false,
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

  // 一次性完成：出题 -> 导出 canvas -> (可选)按坐标作答 -> 拿 token
  // { session_id, coords?: [[x,y],...], submit?: true, timeout_ms? }
  if (req.method === 'POST' && url.pathname === '/solve-then-answer') {
    const body = await readBody(req);
    const s = sessions.get((body && body.session_id) || '');
    if (!s) return send(404, { error: 'no_session' });
    const coords = Array.isArray(body.coords) ? body.coords : null;
    try {
      s.busy = true;
      const out = await s.withLock(async () => {
        const t0 = now();
        const solved = await s.solve({
          sitekey: body.sitekey || DEFAULT_SITEKEY,
          rqdata: body.rqdata || '',
          host: body.host || DEFAULT_HOST,
          timeout: Number(body.timeout_ms || 45000),
        });
        // 如果直接拿到 token，结束
        if (solved.token) return { phase: 'solved', token: solved.token, tokenLen: solved.token.length };

        // 出题了：立即抓 canvas + 截图（此刻页面还在，慢一步就被回收）
        const ch = solved.challenge || await s.captureChallenge().catch(() => null);
        const result = { phase: 'challenge', prompt: (ch && ch.prompt) || '',
                         canvases: (ch && ch.canvases) || [], cells: (ch && ch.cells) || [],
                         imgs: (ch && ch.imgs) || [], bgImages: (ch && ch.bgImages) || [] };
        // 同时把整个 challenge iframe 截图带回去，供外部视觉判断
        try {
          const el = await s.page.$('iframe[src*="frame=challenge"]');
          if (el) {
            const bx = await el.boundingBox();
            if (bx) {
              const png = await s.page.screenshot({
                clip: { x: Math.max(0, bx.x), y: Math.max(0, bx.y),
                        width: Math.min(bx.width, 2000), height: Math.min(bx.height, 2000) },
                encoding: 'base64',
              });
              result.screenshot = { png, box: { x: Math.round(bx.x), y: Math.round(bx.y),
                                               w: Math.round(bx.width), h: Math.round(bx.height) } };
            }
          }
        } catch (_) {}

        // 没给坐标就返回，等外部再调一次带 coords
        if (!coords || !coords.length) {
          result.needAnswer = true;
          result.elapsedMs = now() - t0;
          return result;
        }

        // 给了坐标：立即点击（此刻 challenge 页面还在）
        const ans = await s.answerChallengeAt(coords);
        result.answer = ans;
        if (ans && ans.error) return result;

        await sleep(400);
        const sub = await s.submitChallenge();
        result.submit = sub;

        // 等 token（优先 pending，其次 render callback）
        const waitUntil = now() + Number(body.wait_ms || 60000);
        while (now() < waitUntil) {
          await sleep(1000);
          const pend = await s.readPendingToken();
          if (pend && pend.token) { result.token = pend.token; result.tokenLen = pend.token.length; break; }
          const got = await s.readToken();
          if (got && got.token) { result.token = got.token; result.tokenLen = got.token.length; break; }
        }
        result.elapsedMs = now() - t0;
        return result;
      });
      return send(200, { ok: true, session_id: s.id, ...out });
    } catch (e) {
      return send(500, { error: String(e).slice(0, 300) });
    } finally {
      s.busy = false;
    }
  }

  // 截图：把 challenge iframe 或整页截下来，返回 base64 PNG
  // { session_id, what?: 'challenge'|'page', clip?: {x,y,width,height} }
  if (req.method === 'POST' && url.pathname === '/shoot') {
    const body = await readBody(req);
    const s = sessions.get((body && body.session_id) || '');
    if (!s) return send(404, { error: 'no_session' });
    try {
      s.busy = true;
      const b64 = await s.withLock(async () => {
        const what = (body && body.what) || 'challenge';
        if (what === 'challenge') {
          const el = await s.page.$('iframe[src*="frame=challenge"]');
          if (!el) return null;
          const box = await el.boundingBox();
          if (!box) return null;
          const buf = await s.page.screenshot({
            clip: { x: Math.max(0, box.x), y: Math.max(0, box.y),
                    width: Math.min(box.width, 2000), height: Math.min(box.height, 2000) },
            encoding: 'base64',
          });
          return { b64: buf, box: { x: Math.round(box.x), y: Math.round(box.y),
                                    w: Math.round(box.width), h: Math.round(box.height) } };
        }
        const buf = await s.page.screenshot({ encoding: 'base64', fullPage: false });
        return { b64: buf, box: null };
      });
      if (!b64) return send(404, { error: 'challenge iframe not found' });
      return send(200, { ok: true, box: b64.box, png: b64.b64 });
    } catch (e) {
      return send(500, { error: String(e).slice(0, 300) });
    } finally {
      s.busy = false;
    }
  }

  // 读取 frame 级自动答题器的日志：{ session_id }
  if (req.method === 'POST' && url.pathname === '/frame-log') {
    const body = await readBody(req);
    const s = sessions.get((body && body.session_id) || '');
    if (!s) return send(404, { error: 'no_session' });
    try {
      s.busy = true;
      const out = await s.withLock(async () => {
        const list = [];
        for (const f of s.page.frames()) {
          if (!/challenge|newassets/.test(f.url())) continue;
          let info = null;
          try {
            info = await f.evaluate(() => ({
              installed: !!window.__frameAuto,
              done: window.__frameAuto ? window.__frameAuto.done : null,
              log: window.__frameAuto ? window.__frameAuto.log.slice(-15) : null,
              canvases: document.querySelectorAll('canvas').length,
              grids: document.querySelectorAll('[aria-label^="Challenge Image"]').length,
              text: (document.body.innerText || '').replace(/\s+/g, ' ').slice(0, 100),
            }));
          } catch (e) { info = { err: String(e).slice(0, 100) }; }
          list.push({ url: f.url().slice(0, 90), info });
        }
        return list;
      });
      return send(200, { frames: out });
    } catch (e) {
      return send(500, { error: String(e).slice(0, 300) });
    } finally {
      s.busy = false;
    }
  }

  // 诊断：列出会话里 puppeteer 能看到的全部 frame 及其挑战内容
  if (req.method === 'POST' && url.pathname === '/frames') {
    const body = await readBody(req);
    const s = sessions.get((body && body.session_id) || '');
    if (!s) return send(404, { error: 'no_session' });
    try {
      s.busy = true;
      const out = await s.withLock(async () => {
        const list = [];
        for (const f of s.page.frames()) {
          let info = null;
          try {
            info = await f.evaluate(() => {
              const cvs = [...document.querySelectorAll('canvas')].map((c) => {
                const r = c.getBoundingClientRect();
                return { x: Math.round(r.x), y: Math.round(r.y),
                         w: Math.round(r.width), h: Math.round(r.height),
                         cw: c.width, ch: c.height };
              });
              const imgs = [...document.querySelectorAll('img')]
                .map((i) => { const r = i.getBoundingClientRect();
                  return { src: (i.src || '').slice(0, 90), x: Math.round(r.x), y: Math.round(r.y),
                           w: Math.round(r.width), h: Math.round(r.height) }; });
              const btns = [...document.querySelectorAll('div[role=button],button,div.button')]
                .map((d) => { const r = d.getBoundingClientRect();
                  return { aria: d.getAttribute('aria-label') || '',
                           txt: (d.innerText || '').slice(0, 30),
                           x: Math.round(r.x), y: Math.round(r.y),
                           w: Math.round(r.width), h: Math.round(r.height) }; })
                .filter((b) => b.w > 10 && b.h > 10);
              return {
                href: location.href.slice(0, 110),
                bodyLen: document.body.innerHTML.length,
                text: (document.body.innerText || '').replace(/\s+/g, ' ').slice(0, 160),
                canvases: cvs, imgs, buttons: btns.slice(0, 12),
              };
            });
          } catch (e) { info = { err: String(e).slice(0, 100) }; }
          list.push({ url: (f.url() || '').slice(0, 110), info });
        }
        return list;
      });
      return send(200, { frames: out });
    } catch (e) {
      return send(500, { error: String(e).slice(0, 300) });
    } finally {
      s.busy = false;
    }
  }

  // 视觉答题：出题 → 裁剪 canvas → 交给外部视觉服务 → 点击 → 等 token
  // { session_id, sitekey, rqdata, host, vision_url, timeout_ms, wait_ms }
  if (req.method === 'POST' && url.pathname === '/vision-answer') {
    const body = await readBody(req);
    const s = sessions.get((body && body.session_id) || '');
    if (!s) return send(404, { error: 'no_session' });
    const visionUrl = (body && body.vision_url) || 'http://127.0.0.1:8300/solve';
    try {
      s.busy = true;
      const out = await s.withLock(async () => {
        const t0 = now();
        // 1) 出题
        const solved = await s.solve({
          sitekey: body.sitekey || DEFAULT_SITEKEY,
          rqdata: body.rqdata || '',
          host: body.host || DEFAULT_HOST,
          timeout: Number(body.timeout_ms || 40000),
          inject: false,
        });
        if (solved.token) return { phase: 'direct', token: solved.token, tokenLen: solved.token.length };
        const ch = solved.challenge || await s.captureChallenge().catch(() => null);
        const prompt = (ch && ch.prompt) || '';
        const cvs = (ch && ch.canvases) || [];
        const cells = (ch && ch.cells) || [];
        // 题干含 line/curve/shorter 才走线条题；其余一律按 9 宫格处理
        // （格子未必是 button 元素，captureChallenge 拿不到，所以不能靠格子数量判断）
        const isLinePrompt = /lines?\b|curve|shorter|longer/i.test(prompt);

        const gridCells = cells.filter((c) => c.w >= 80 && c.h >= 80 &&
          Math.abs(c.w - c.h) < 30 && !/skip|about|refresh|english/i.test(c.aria || ''));

        // ── 9 宫格题：截图后交给视觉服务按索引作答 ──
        if (!isLinePrompt) {
          let shot = null;
          try {
            const el = await s.page.$('iframe[src*="frame=challenge"]');
            if (el) {
              const bx = await el.boundingBox();
              if (bx) {
                shot = await s.page.screenshot({
                  clip: { x: Math.max(0, bx.x), y: Math.max(0, bx.y),
                          width: Math.min(bx.width, 2000), height: Math.min(bx.height, 2000) },
                  encoding: 'base64',
                });
              }
            }
          } catch (_) {}
          if (!shot) return { phase: 'grid', error: 'no_shot', prompt, cells: gridCells };
          let gv = null;
          try {
            const vr = await fetch(visionUrl.replace('/solve', '/solve-grid'), {
              method: 'POST',
              headers: { 'Content-Type': 'application/json' },
              body: JSON.stringify({ png: 'data:image/png;base64,' + shot,
                                     prompt, cells: gridCells }),
            });
            gv = await vr.json();
          } catch (e) {
            console.log('   [grid] vision call failed:', String(e).slice(0, 200));
            return { phase: 'grid_vision_fail', error: String(e).slice(0, 200), prompt,
                     visionUrl: visionUrl.replace('/solve', '/solve-grid'),
                     shotLen: shot.length };
          }
          console.log('   [grid] vision →', JSON.stringify(gv).slice(0, 200));
          const idx = (gv && gv.indices) || [];
          if (!idx.length) return { phase: 'grid_no_picks', prompt, gv };
          // 按 3x3 阅读顺序换算成 iframe 内的归一化坐标
          // 网格区域约占截图下方 80%（题干栏在上方）
          let ans;
          if (gridCells.length >= 4) {
            ans = await s.answerChallenge(idx);
          } else {
            // 无 aria 时按几何布局推算 3x3 格子中心。
            // 系数由实测截图 (520x570) 标定：
            //   列中心 x = 145/260/375  →  0.279 / 0.500 / 0.721
            //   行中心 y = 213/300/388  →  0.374 / 0.526 / 0.681
            const XS = [0.279, 0.500, 0.721];
            const YS = [0.374, 0.526, 0.681];
            const coords = idx.map((k) => [XS[k % 3], YS[Math.floor(k / 3)]]);
            ans = await s.clickInFrame(coords);
          }
          const result = { phase: 'grid_answered', prompt, indices: idx, ans, gv };
          if (ans && ans.error) return result;
          await sleep(400);
          result.submit = await s.submitChallenge();
          // 点击后立刻截图，确认格子被选中（边框变色）
          try {
            const el2 = await s.page.$('iframe[src*="frame=challenge"]');
            if (el2) {
              const b2 = await el2.boundingBox();
              if (b2) {
                result.afterShot = await s.page.screenshot({
                  clip: { x: Math.max(0, b2.x), y: Math.max(0, b2.y),
                          width: Math.min(b2.width, 2000),
                          height: Math.min(b2.height, 2000) },
                  encoding: 'base64' });
                require('node:fs').writeFileSync(
                  '/tmp/after_grid_' + Date.now() + '.png',
                  Buffer.from(result.afterShot, 'base64'));
              }
            }
          } catch (_) {}
          const until = now() + Number(body.wait_ms || 60000);
          while (now() < until) {
            await sleep(1000);
            const pend = await s.readPendingToken();
            if (pend && pend.token) { result.token = pend.token; result.tokenLen = pend.token.length; break; }
            const got = await s.readToken();
            if (got && got.token) { result.token = got.token; result.tokenLen = got.token.length; break; }
          }
          result.elapsedMs = now() - t0;
          return result;
        }

        // ── 线条题：canvas → 视觉服务 ──
        const main = cvs.filter((c) => (c.dataUrl || '').length > 500)
          .sort((a, b) => (b.w * b.h) - (a.w * a.h))[0];
        if (!main) {
          return { phase: 'challenge', error: 'no_canvas', prompt,
                   cells, elapsedMs: now() - t0 };
        }
        // 2) 交给视觉服务
        // 压缩：canvas dataURL 可能 1.4MB，直接用 fetch 会失败。
        // 在页面内用 OffscreenCanvas 缩到宽 600 再传。
        let pngData = main.dataUrl;
        // 缩图必须在 challenge frame 内做（canvas 在那个 frame 里，
        // 主页面拿到 dataUrl 后重新解码会占用大量内存且容易失败）。
        try {
          const shrunk = await s.page.evaluate(async (cfg) => {
            const im = new Image();
            im.crossOrigin = 'anonymous';
            await new Promise((ok, bad) => {
              im.onload = ok; im.onerror = () => bad(new Error('img_load'));
              im.src = cfg.du;
            });
            const maxW = 720;
            const scale = Math.min(1, maxW / im.width);
            const c = document.createElement('canvas');
            c.width = Math.max(1, Math.round(im.width * scale));
            c.height = Math.max(1, Math.round(im.height * scale));
            const g = c.getContext('2d');
            g.drawImage(im, 0, 0, c.width, c.height);
            // 用 JPEG 进一步压缩（题目图不需要无损）
            return c.toDataURL('image/jpeg', 0.82);
          }, { du: main.dataUrl });
          if (shrunk && shrunk.length > 2000) {
            pngData = shrunk;
            console.log('   [line] shrunk', main.dataUrl.length, '→', shrunk.length);
          }
        } catch (e) {
          console.log('   [line] shrink failed:', String(e).slice(0, 120));
        }
        if (pngData.length > 900000) {
          console.log('   [line] payload still too big:', pngData.length);
          return { phase: 'vision_payload_too_big', prompt, size: pngData.length };
        }
        let vis = null;
        try {
          const vr = await fetch(visionUrl, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ png: pngData, prompt,
                                   width: main.w, height: main.h }),
          });
          vis = await vr.json();
        } catch (e) {
          console.log('   [line] vision call failed:', String(e).slice(0, 200));
          return { phase: 'vision_fail', error: String(e).slice(0, 200), prompt,
                   visionUrl, dataLen: (main.dataUrl || '').length };
        }
        console.log('   [line] vision →', JSON.stringify(vis).slice(0, 200));
        const coords = (vis && vis.norm) || [];
        if (coords.length < 2) {
          return { phase: 'vision_no_coords', prompt, vis };
        }
        // 3) 点击
        const ans = await s.answerChallengeAt(coords);
        const result = { phase: 'answered', prompt, coords, ans, vis };
        if (ans && ans.error) return result;
        // 4) 提交并等 token
        await sleep(300);
        result.submit = await s.submitChallenge();
        const until = now() + Number(body.wait_ms || 60000);
        while (now() < until) {
          await sleep(1000);
          const pend = await s.readPendingToken();
          if (pend && pend.token) { result.token = pend.token; result.tokenLen = pend.token.length; break; }
          const got = await s.readToken();
          if (got && got.token) { result.token = got.token; result.tokenLen = got.token.length; break; }
        }
        result.elapsedMs = now() - t0;
        return result;
      });
      return send(200, { ok: true, session_id: s.id, ...out });
    } catch (e) {
      return send(500, { error: String(e).slice(0, 300) });
    } finally {
      s.busy = false;
    }
  }

  // 按归一化坐标作答：{ session_id, coords: [[0.33,0.31],[0.37,0.47]], submit?: true }
  if (req.method === 'POST' && url.pathname === '/answer-at') {
    const body = await readBody(req);
    if (!body) return send(400, { error: 'invalid_json' });
    const s = sessions.get(body.session_id || '');
    if (!s) return send(404, { error: 'no_session' });
    const coords = Array.isArray(body.coords) ? body.coords : [];
    if (!coords.length) return send(400, { error: 'need coords' });
    try {
      s.busy = true;
      const out = await s.withLock(async () => {
        const ans = await s.answerChallengeAt(coords);
        let sub = null;
        if (body.submit !== false) {
          await sleep(400 + Math.random() * 400);
          sub = await s.submitChallenge();
          await sleep(1500);
          // 部分题目点完会自动提交，再兜底点一次
          if (sub && sub.error) {
            await sleep(800);
            sub = await s.submitChallenge();
          }
        }
        let tok = null;
        if (body.wait !== false) {
          const t0 = now();
          while (now() - t0 < Number(body.wait_ms || 60000)) {
            await sleep(1000);
            // 优先读 __reaPending（那是原始 execute 的 promise 结果）
            const pend = await s.readPendingToken();
            if (pend && pend.token) { tok = pend; break; }
            const got = await s.readToken();
            if (got && got.token) { tok = got; break; }
          }
        }
        return { answer: ans, submit: sub, token: tok && tok.token ? tok.token : null,
                 tokenLen: tok && tok.token ? tok.token.length : 0,
                 pendingErr: tok && tok.error ? tok.error : null };
      });
      return send(200, { ok: true, session_id: s.id, ...out });
    } catch (e) {
      return send(500, { error: String(e).slice(0, 300) });
    } finally {
      s.busy = false;
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
