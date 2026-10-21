#!/usr/bin/env node
/**
 * 决定性实验：同会话内 checksiteconfig → hsw(JWT) → getcaptcha
 *
 * 假设：d 字段是服务端下发的会话密钥；用同一次 checksiteconfig 的 JWT
 * 生成 proof，服务端应该能解开 → 200。
 *
 * 同时做对照：用真品 wire 尾字节 0x67 / 0x6b 各试一次，
 * 确认尾字节是不是版本标记。
 */
'use strict';

const fs = require('node:fs');
const path = require('node:path');

const PORT = 9222;
const OUT = path.join(process.cwd(), 'runtime', 'rea-capture', 'proof_test.jsonl');

const log = (...a) => { const s = a.join(' '); console.log(s); try { fs.appendFileSync(OUT, JSON.stringify({ t: Date.now(), msg: s }) + '\n'); } catch (_) {} };

async function connect() {
  const list = await (await fetch(`http://127.0.0.1:${PORT}/json/list`)).json();
  const t = list.find((x) => x.type === 'page' && /pay\.openai/.test(x.url || ''));
  if (!t) throw new Error('no page');
  const ws = new WebSocket(t.webSocketDebuggerUrl);
  let id = 0;
  const pend = new Map();
  const all = new Map();
  ws.addEventListener('message', (ev) => {
    const m = JSON.parse(ev.data);
    if (m.id && pend.has(m.id)) {
      const p = pend.get(m.id); pend.delete(m.id);
      m.error ? p.rej(new Error(JSON.stringify(m.error))) : p.ok(m.result);
      return;
    }
    if (m.method === 'Target.attachedToTarget') all.set(m.params.sessionId, m.params.targetInfo || {});
  });
  const send = (method, params = {}, sessionId) => new Promise((ok, rej) => {
    const i = ++id; pend.set(i, { ok, rej });
    const msg = { id: i, method, params };
    if (sessionId) msg.sessionId = sessionId;
    ws.send(JSON.stringify(msg));
    setTimeout(() => { if (pend.delete(i)) rej(new Error('timeout ' + method)); }, 120000);
  });
  await new Promise((r) => ws.addEventListener('open', r));
  await send('Target.setAutoAttach', { autoAttach: true, waitForDebuggerOnStart: false, flatten: true });
  for (let k = 0; k < 3; k += 1) {
    for (const s of [...all.keys()]) {
      try { await send('Target.setAutoAttach', { autoAttach: true, waitForDebuggerOnStart: false, flatten: true }, s); } catch (_) {}
    }
    await new Promise((r) => setTimeout(r, 900));
  }
  const hit = [...all.entries()].find(([, v]) => /newassets\.hcaptcha.*frame=challenge/.test(v.url || ''));
  if (!hit) throw new Error('no challenge frame');
  await send('Runtime.enable', {}, hit[0]);
  return { send, sid: hit[0], url: hit[1].url, ws };
}

const EXPERIMENT = (sk) => `(async () => {
  const out = { sitekey: ${JSON.stringify(sk)}, trials: [] };

  // 1) checksiteconfig 拿同会话 JWT
  const v = '86591f996c36877668d253e353e36fac021db33b';
  const csUrl = 'https://api.hcaptcha.com/checksiteconfig?v=' + v +
    '&host=b.stripecdn.com&sitekey=' + ${JSON.stringify(sk)} + '&sc=1&swa=1';
  const cs = await fetch(csUrl, { credentials: 'include' });
  const cj = await cs.json();
  out.pass = cj.pass;
  const jwt = cj.c && cj.c.req;
  if (!jwt) { out.err = 'no jwt'; return out; }
  out.jwtLen = jwt.length;
  out.jwtPayload = (() => { try { const p = jwt.split('.')[1]; return JSON.parse(atob(p.replace(/-/g,'+').replace(/_/g,'/'))); } catch(e) { return null; } })();

  // 2) 生成 proof
  const t0 = Date.now();
  let proof = null;
  try {
    proof = await Promise.race([
      window.hsw(jwt, { href: 'https://b.stripecdn.com/', ardata: null }),
      new Promise((r) => setTimeout(() => r(null), 40000)),
    ]);
  } catch (e) { out.proofErr = String(e).slice(0, 300); }
  out.proofMs = Date.now() - t0;
  if (!proof) { out.err = 'no proof'; return out; }
  out.proofLen = proof.length;
  const bytes = Uint8Array.from(atob(proof), (c) => c.charCodeAt(0));
  out.proofBytes = bytes.length;

  // 3) 组 wire，尾字节 0x67 / 0x6b 各试
  const enc = new TextEncoder();
  const jsonBytes = enc.encode(JSON.stringify({ type: 'hsw', req: jwt }));
  const build = (tailByte) => {
    const total = 1 + 3 + jsonBytes.length + 3 + bytes.length + 1;
    const buf = new Uint8Array(total);
    let p = 0;
    buf[p++] = 0x92;
    buf[p++] = 0xda; buf[p++] = (jsonBytes.length >> 8) & 255; buf[p++] = jsonBytes.length & 255;
    buf.set(jsonBytes, p); p += jsonBytes.length;
    buf[p++] = 0xc8; buf[p++] = (bytes.length >> 8) & 255; buf[p++] = bytes.length & 255;
    buf.set(bytes, p); p += bytes.length;
    buf[p++] = tailByte;
    return buf.subarray(0, p);
  };

  for (const tail of [0x67, 0x6b]) {
    const wire = build(tail);
    const trial = { tail: '0x' + tail.toString(16), wireLen: wire.length };
    try {
      const r = await fetch('https://api.hcaptcha.com/getcaptcha/' + ${JSON.stringify(sk)}, {
        method: 'POST', body: wire, credentials: 'include',
        headers: { 'Content-Type': 'application/octet-stream' },
      });
      const txt = await r.text();
      trial.status = r.status;
      trial.len = txt.length;
      trial.head = txt.slice(0, 300);
      trial.ok = r.status === 200 && txt.includes('"c"');
    } catch (e) { trial.err = String(e).slice(0, 200); }
    out.trials.push(trial);
    if (trial.ok) break;
  }
  return out;
})()`;

(async () => {
  const { send, sid, url } = await connect();
  const sk = (url.match(/sitekey=([0-9a-f-]+)/) || [])[1];
  log('=== 实验开始 sitekey=' + sk + ' ===');
  const r = await send('Runtime.evaluate', {
    expression: EXPERIMENT(sk), returnByValue: true, awaitPromise: true,
  }, sid);
  if (r.exceptionDetails) {
    log('EXC:', (r.exceptionDetails.exception?.description || '').slice(0, 600));
    return;
  }
  const out = r.result?.value;
  log('pass:', out?.pass, ' jwtLen:', out?.jwtLen);
  log('jwt payload:', JSON.stringify({
    f: out?.jwtPayload?.f, s: out?.jwtPayload?.s, t: out?.jwtPayload?.t,
    dLen: out?.jwtPayload?.d?.length, n: out?.jwtPayload?.n, c: out?.jwtPayload?.c,
  }));
  log('proof:', out?.proofLen, 'chars /', out?.proofBytes, 'bytes,', out?.proofMs, 'ms');
  for (const t of out?.trials || []) {
    log(`  tail=${t.tail} wire=${t.wireLen} → ${t.status} (${t.len}B) ok=${t.ok}`);
    if (t.head) log('    ', t.head.slice(0, 200));
  }
  log('=== 实验结束 ===');
})().catch((e) => { console.log('ERR', String(e).slice(0, 300)); process.exit(1); });
