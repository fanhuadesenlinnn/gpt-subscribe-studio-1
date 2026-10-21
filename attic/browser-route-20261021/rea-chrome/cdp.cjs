#!/usr/bin/env node
/**
 * 极简 CDP 客户端（只用 Node 内置 WebSocket，不装任何依赖）。
 *
 * 用法：
 *   node tools/rea-chrome/cdp.cjs list
 *   node tools/rea-chrome/cdp.cjs eval  "<js 表达式>" [--target=URL子串] [--frame=URL子串]
 *   node tools/rea-chrome/cdp.cjs frames  [--target=URL子串]
 *   node tools/rea-chrome/cdp.cjs net on|off
 *   node tools/rea-chrome/cdp.cjs har  <输出路径> <秒数> [--target=URL子串]
 *   node tools/rea-chrome/cdp.cjs goto <url> [--target=URL子串]
 *   node tools/rea-chrome/cdp.cjs click "<selector>" [--target=URL子串]
 *   node tools/rea-chrome/cdp.cjs type  "<selector>" "<text>" [--target=URL子串]
 *   node tools/rea-chrome/cdp.cjs watch [--target=URL子串]      # 实时打印所有网络请求
 */
'use strict';

const PORT = Number(process.env.CDP_PORT || 9222);
const BASE = `http://127.0.0.1:${PORT}`;

function arg(name, fallback = '') {
  const hit = process.argv.find((a) => a.startsWith(`--${name}=`));
  return hit ? hit.slice(name.length + 3) : fallback;
}

async function listTargets() {
  const r = await fetch(`${BASE}/json/list`);
  return r.json();
}

async function pickTarget(match) {
  const all = await listTargets();
  const pages = all.filter((t) => t.type === 'page');
  if (!match) return pages[0];
  return pages.find((t) => (t.url || '').includes(match) || (t.title || '').includes(match));
}

class CDP {
  constructor(wsUrl) {
    this.wsUrl = wsUrl;
    this.id = 0;
    this.pending = new Map();
    this.handlers = new Map();
  }

  connect() {
    return new Promise((resolve, reject) => {
      this.ws = new WebSocket(this.wsUrl);
      this.ws.addEventListener('open', () => resolve(this));
      this.ws.addEventListener('error', (e) => reject(new Error('ws error')));
      this.ws.addEventListener('message', (ev) => {
        let msg;
        try { msg = JSON.parse(ev.data); } catch (_) { return; }
        if (msg.id !== undefined) {
          const p = this.pending.get(msg.id);
          if (p) {
            this.pending.delete(msg.id);
            if (msg.error) p.reject(new Error(JSON.stringify(msg.error)));
            else p.resolve(msg.result);
          }
          return;
        }
        const hs = this.handlers.get(msg.method) || [];
        for (const h of hs) { try { h(msg.params); } catch (_) {} }
      });
    });
  }

  send(method, params = {}) {
    const id = ++this.id;
    return new Promise((resolve, reject) => {
      this.pending.set(id, { resolve, reject });
      this.ws.send(JSON.stringify({ id, method, params }));
      setTimeout(() => {
        if (this.pending.has(id)) {
          this.pending.delete(id);
          reject(new Error(`timeout: ${method}`));
        }
      }, Number(process.env.CDP_TIMEOUT || 60000));
    });
  }

  on(method, handler) {
    if (!this.handlers.has(method)) this.handlers.set(method, []);
    this.handlers.get(method).push(handler);
  }

  close() { try { this.ws.close(); } catch (_) {} }
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function main() {
  const cmd = process.argv[2] || 'list';

  if (cmd === 'list') {
    const all = await listTargets();
    for (const t of all) {
      console.log(`${t.type.padEnd(16)} ${(t.title || '').slice(0, 40).padEnd(42)} ${(t.url || '').slice(0, 90)}`);
    }
    return;
  }

  const match = arg('target');
  const target = await pickTarget(match);
  if (!target) { console.error('找不到目标；先跑 cdp.cjs list'); process.exit(1); }

  const cdp = await new CDP(target.webSocketDebuggerUrl).connect();
  try {
    if (cmd === 'eval') {
      const expr = process.argv[3];
      const frameMatch = arg('frame');
      if (frameMatch) {
        await cdp.send('Page.enable');
        const tree = await cdp.send('Page.getFrameTree');
        const walk = (n, out = []) => {
          if ((n.frame?.url || '').includes(frameMatch)) out.push(n.frame);
          for (const c of n.childFrames || []) walk(c, out);
          return out;
        };
        const frames = walk(tree.frameTree);
        if (!frames.length) { console.error('没有匹配的 frame'); process.exit(2); }
        const res = await cdp.send('Runtime.evaluate', {
          expression: expr, contextId: undefined, returnByValue: true, awaitPromise: true,
          // frame 内的执行：用 isolated world 的 context
        }).catch(() => null);
        // 简化：先尝试主 frame，失败提示
        console.error('注：frame 内 eval 需要 executionContextId，已回退到主 frame');
      }
      const res = await cdp.send('Runtime.evaluate', {
        expression: expr, returnByValue: true, awaitPromise: true, includeCommandLineAPI: true,
      });
      if (res.exceptionDetails) {
        console.error('异常:', res.exceptionDetails.text,
          res.exceptionDetails.exception?.description?.slice(0, 400) || '');
        process.exit(3);
      }
      console.log(JSON.stringify(res.result?.value ?? res.result, null, 2));
      return;
    }

    if (cmd === 'frames') {
      await cdp.send('Page.enable');
      const tree = await cdp.send('Page.getFrameTree');
      const walk = (n, depth = 0) => {
        const f = n.frame || {};
        console.log(`${'  '.repeat(depth)}${f.id}  ${(f.url || '').slice(0, 110)}`);
        for (const c of n.childFrames || []) walk(c, depth + 1);
      };
      walk(tree.frameTree);
      return;
    }

    if (cmd === 'goto') {
      await cdp.send('Page.enable');
      await cdp.send('Page.navigate', { url: process.argv[3] });
      await sleep(Number(arg('wait', '2500')));
      const r = await cdp.send('Runtime.evaluate', { expression: 'location.href', returnByValue: true });
      console.log('now:', r.result?.value);
      return;
    }

    if (cmd === 'click') {
      const sel = process.argv[3];
      const res = await cdp.send('Runtime.evaluate', {
        expression: `(()=>{const e=document.querySelector(${JSON.stringify(sel)});
          if(!e)return 'not found'; e.scrollIntoView({block:'center'}); e.click(); return 'clicked '+(e.innerText||'').slice(0,40);})()`,
        returnByValue: true,
      });
      console.log(res.result?.value);
      return;
    }

    if (cmd === 'type') {
      const sel = process.argv[3];
      const text = process.argv[4] || '';
      const res = await cdp.send('Runtime.evaluate', {
        expression: `(()=>{const e=document.querySelector(${JSON.stringify(sel)});
          if(!e)return 'not found';
          const proto = e.tagName==='TEXTAREA'?window.HTMLTextAreaElement.prototype:window.HTMLInputElement.prototype;
          const setter = Object.getOwnPropertyDescriptor(proto,'value').set;
          setter.call(e, ${JSON.stringify(text)});
          e.dispatchEvent(new Event('input',{bubbles:true}));
          e.dispatchEvent(new Event('change',{bubbles:true}));
          return 'typed '+e.value.length;})()`,
        returnByValue: true,
      });
      console.log(res.result?.value);
      return;
    }

    if (cmd === 'net' || cmd === 'watch') {
      await cdp.send('Network.enable', { maxTotalBufferSize: 200 * 1024 * 1024 });
      const filter = arg('filter', '');
      cdp.on('Network.requestWillBeSent', (p) => {
        const u = p.request?.url || '';
        if (filter && !u.includes(filter)) return;
        console.log('→', p.request?.method, u.slice(0, 150));
      });
      cdp.on('Network.responseReceived', (p) => {
        const u = p.response?.url || '';
        if (filter && !u.includes(filter)) return;
        console.log('←', p.response?.status, u.slice(0, 150));
      });
      cdp.on('Network.loadingFailed', (p) => {
        console.log('✗', p.errorText, (p.requestId || '').slice(0, 12));
      });
      console.log('监听中…（Ctrl+C 结束）');
      await new Promise(() => {});
      return;
    }

    if (cmd === 'har') {
      const outPath = process.argv[3] || '/tmp/capture.har';
      const seconds = Number(process.argv[4] || 30);
      const filter = arg('filter', '');
      await cdp.send('Network.enable', { maxTotalBufferSize: 500 * 1024 * 1024 });
      const entries = new Map();
      cdp.on('Network.requestWillBeSent', (p) => {
        entries.set(p.requestId, {
          startedDateTime: new Date().toISOString(),
          request: { method: p.request?.method, url: p.request?.url,
                     headers: p.request?.headers, postData: p.request?.postData },
          response: {}, timings: {}, _t: Date.now(),
        });
      });
      cdp.on('Network.responseReceived', (p) => {
        const e = entries.get(p.requestId);
        if (!e) return;
        e.response = { status: p.response?.status, headers: p.response?.headers,
                       mimeType: p.response?.mimeType };
        e._ms = Date.now() - e._t;
      });
      console.log(`抓 ${seconds}s…`);
      await sleep(seconds * 1000);
      const list = [];
      for (const [rid, e] of entries) {
        const url = e.request?.url || '';
        if (filter && !url.includes(filter)) continue;
        let body = '';
        try {
          const b = await cdp.send('Network.getResponseBody', { requestId: rid });
          body = b.base64Encoded ? Buffer.from(b.body, 'base64').toString('utf8') : b.body;
        } catch (_) {}
        list.push({ ...e, responseBody: body.slice(0, 200000) });
      }
      require('node:fs').writeFileSync(outPath, JSON.stringify({ entries: list }, null, 1));
      console.log(`写入 ${outPath}（${list.length} 条）`);
      return;
    }

    console.error('未知命令:', cmd);
  } finally {
    cdp.close();
  }
}

main().catch((e) => { console.error('ERR', String(e).slice(0, 400)); process.exit(1); });
