#!/usr/bin/env node
/**
 * 开一个可调试的 Chrome，用于逆向 hCaptcha / Stripe。
 *
 * 特性：
 *   · --remote-debugging-port=9222，可用 DevTools 或 CDP 直连
 *   · 独立 user-data-dir（不污染日常 Chrome）
 *   · 证书错误放过（merchant origin 用自签证书）
 *   · 不启用自动化标记，navigator.webdriver 为 undefined
 *   · 预置 b.stripecdn.com → 127.0.0.1:8443 域名映射（solver 的 merchant origin）
 *
 * 用法：
 *   node tools/rea-chrome/open_debug_chrome.cjs
 *   然后浏览器打开 http://127.0.0.1:9222 看 devtools list
 *   或 chrome://inspect 里配置 localhost:9222
 */
'use strict';

const { spawn } = require('node:child_process');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');

const CHROME = process.env.CHROME_PATH
  || '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome';
const PORT = Number(process.env.CDP_PORT || 9222);
const PROFILE = process.env.PROFILE_DIR
  || path.join(os.tmpdir(), 'rea-chrome-profile');
const MERCHANT = process.env.MERCHANT_HOST || 'b.stripecdn.com';
const MERCHANT_PORT = Number(process.env.MERCHANT_PORT || 8443);

fs.mkdirSync(PROFILE, { recursive: true });

const args = [
  `--remote-debugging-port=${PORT}`,
  `--user-data-dir=${PROFILE}`,
  '--no-first-run',
  '--no-default-browser-check',
  '--disable-blink-features=AutomationControlled',
  '--ignore-certificate-errors',
  '--allow-insecure-localhost',
  '--lang=en-US,en',
  // 让页面内的 b.stripecdn.com 落到本机 solver 的 8443
  `--host-resolver-rules=MAP ${MERCHANT} 127.0.0.1:${MERCHANT_PORT}`,
  '--window-size=1440,900',
  process.env.START_URL || 'about:blank',
];

console.log('启动 Chrome');
console.log('  port   :', PORT);
console.log('  profile:', PROFILE);
console.log('  映射   :', `${MERCHANT} → 127.0.0.1:${MERCHANT_PORT}`);
console.log('  devtools: http://127.0.0.1:' + PORT);

const child = spawn(CHROME, args, { stdio: 'ignore', detached: true });
child.unref();

// 等端口起来后打印可调试目标
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
(async () => {
  for (let i = 0; i < 30; i += 1) {
    await sleep(500);
    try {
      const r = await fetch(`http://127.0.0.1:${PORT}/json/version`);
      if (r.ok) {
        const v = await r.json();
        console.log('\n就绪');
        console.log('  browser:', v.Browser);
        console.log('  ws      :', v.webSocketDebuggerUrl);
        const list = await (await fetch(`http://127.0.0.1:${PORT}/json/list`)).json();
        console.log('  targets :');
        for (const t of list) console.log('    -', t.type, t.url.slice(0, 80));
        return;
      }
    } catch (_) { /* 还没起来 */ }
  }
  console.log('\n端口未就绪，检查 Chrome 是否被拦截');
})();
