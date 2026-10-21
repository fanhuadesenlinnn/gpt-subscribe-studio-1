// 本地桥接页 + 系统 Chrome 完成 Stripe passive hCaptcha。
// 用法: node solve.cjs --site-key K --rqdata R --merchant-id M [--proxy URL]
const http = require('node:http');
const fs = require('node:fs');
const path = require('node:path');
const { URLSearchParams } = require('node:url');
const puppeteer = require('puppeteer-core');

const ASSET_VERSION = process.env.ASSET_VER || 'v35.5';
const CDN = 'https://b.stripecdn.com/stripethirdparty-srv/assets';

function arg(name, def) {
  const i = process.argv.indexOf('--' + name);
  return i > 0 ? process.argv[i + 1] : def;
}
const SITE_KEY = arg('site-key');
const RQDATA = arg('rqdata', '');
const MERCHANT_ID = arg('merchant-id', '');
const LOCALE = arg('locale', 'en-US');
const PROXY = arg('proxy', process.env.ORDER_PROXY || '');
const TIMEOUT = Number(arg('timeout', '110'));
const OUT = arg('out', '/tmp/hcap_bridge_result.json');
const VISIBLE = process.argv.includes('--visible');

if (!SITE_KEY) { console.error('need --site-key'); process.exit(2); }

function frameUrl(frameId, origin) {
  const page = VISIBLE ? 'HCaptcha.html' : 'HCaptchaInvisible.html';
  return `${CDN}/${ASSET_VERSION}/${page}?id=${frameId}&origin=${encodeURIComponent(origin)}`;
}

function parentHtml(frameId, fUrl) {
  const visiblePayload = {
    sitekey: SITE_KEY, rqdata: RQDATA, merchantId: MERCHANT_ID, locale: LOCALE,
    headerText: 'Verification required', instructionText: 'Complete captcha to continue',
    showCloseButton: false,
  };
  const initPayload = { tag: 'INITIALIZE_HCAPTCHA_INVISIBLE', message: { sitekey: SITE_KEY } };
  const execPayload = { tag: 'EXECUTE_HCAPTCHA_INVISIBLE', message: {
    sitekey: SITE_KEY, rqdata: RQDATA,
    data: { merchant_id: MERCHANT_ID || '', locale: LOCALE || '',
            flow: 'passive_captcha', captcha_vendor: 'hcaptcha' } } };
  const signals = [
    { tag:'SEND_FRAUD_SIGNALS_HCAPTCHA_INVISIBLE', message:{ type:'mouse', eventName:'mousemove', coordinates:{x:168,y:132} } },
    { tag:'SEND_FRAUD_SIGNALS_HCAPTCHA_INVISIBLE', message:{ type:'pointer', eventName:'pointermove', coordinates:{x:214,y:176} } },
    { tag:'SEND_FRAUD_SIGNALS_HCAPTCHA_INVISIBLE', message:{ type:'keyboard', eventName:'keydown' } },
  ];
  return `<!doctype html><html><head><meta charset="utf-8"><title>bridge</title>
<style>body{margin:0;font-family:Arial;background:#f7f7f7}.shell{padding:16px}
#status{font-size:14px;margin-bottom:12px;color:#333}iframe{width:420px;height:720px;border:0;background:#fff}
pre{white-space:pre-wrap;word-break:break-word;font-size:12px}</style></head><body>
<div class="shell"><div id="status">waiting</div>
<iframe id="f" src="${fUrl}"></iframe><pre id="result"></pre></div>
<script>
const frameID=${JSON.stringify(frameId)};
const invisible=${JSON.stringify(!VISIBLE)};
const visiblePayload=${JSON.stringify(visiblePayload)};
const initPayload=${JSON.stringify(initPayload)};
const execPayload=${JSON.stringify(execPayload)};
const signals=${JSON.stringify(signals)};
let inited=false,executed=false;
const $=id=>document.getElementById(id);
const st=t=>{$("status").textContent=t};
function toBridge(p,o){fetch(p,{method:"POST",headers:{"Content-Type":"application/json"},
  body:JSON.stringify(o||{}),keepalive:true}).catch(()=>{})}
function toChild(w,o,p){try{w.postMessage({type:"stripe-third-party-parent-to-child",
  frameID,payload:p},o||"*")}catch(e){}}
function doInit(w,o){if(inited)return;inited=true;st("init");
  toBridge("/event",{type:"init"});toChild(w,o,initPayload)}
function doExec(w,o){if(executed)return;executed=true;st("exec");
  signals.forEach((s,i)=>setTimeout(()=>toChild(w,o,s),50*i));
  setTimeout(()=>{toBridge("/event",{type:"exec"});toChild(w,o,execPayload)},180);
  // 周期性重发 execute，直到拿到响应
  let n=0; const timer=setInterval(()=>{ n++;
    if(window.__r||n>12){clearInterval(timer);return}
    toChild(w,o,execPayload);
    signals.forEach((s,i)=>setTimeout(()=>toChild(w,o,s),40*i));
  },4000);}
window.addEventListener("message",ev=>{const d=ev.data||{};
  toBridge("/event",{type:"raw",origin:ev.origin,tag:d.tag||d.type||""});
  if(d.type==="stripe-third-party-frame-ready"&&d.frameID===frameID){
    st("frame-ready");if(invisible)doInit(ev.source,ev.origin);
    else toChild(ev.source,ev.origin,visiblePayload);return}
  if(d.type!=="stripe-third-party-child-to-parent"||d.frameID!==frameID)return;
  const p=d.payload||{};$("result").textContent=JSON.stringify(p,null,2);
  toBridge("/event",{type:"child",tag:p.tag||p.type||"",v:p.value?JSON.stringify(p.value).slice(0,120):""});
  if(!invisible){if(p.type==="response"){window.__r=p;toBridge("/result",p)}return}
  const tag=p.tag||"",v=p.value||{};
  if(tag==="LOAD_HCAPTCHA_INVISIBLE"){doExec(ev.source,ev.origin);return}
  if(tag==="RESPONSE_HCAPTCHA_INVISIBLE"){st("solved");
    const s={response:v.response||"",ekey:v.key||"",duration:v.duration||0};
    window.__r=s;toBridge("/result",s);return}
  if(tag==="ERROR_HCAPTCHA_INVISIBLE"){st("fail:"+(v.error||"?"));
    toBridge("/error",{error:v.error||"?",raw:p});return}
});
</script></body></html>`;
}

(async () => {
  const state = { events: [], result: null, error: null };
  let resolveDone, rejectDone;
  const done = new Promise((a, b) => { resolveDone = a; rejectDone = b; });

  const server = http.createServer((req, res) => {
    const u = req.url.split('?')[0];
    if (req.method === 'POST') {
      const ch = [];
      req.on('data', c => ch.push(c));
      req.on('end', () => {
        let p = {}; try { p = JSON.parse(Buffer.concat(ch).toString() || '{}'); } catch (e) {}
        if (u === '/event') state.events.push(p);
        else if (u === '/result') { state.result = p; resolveDone(); }
        else if (u === '/error') { state.error = p; resolveDone(); }
        res.writeHead(200, { 'Content-Type': 'application/json' });
        res.end('{"ok":true}');
      });
      return;
    }
    res.writeHead(200, { 'Content-Type': 'text/html; charset=utf-8' });
    res.end(FLASH.html || '');
  });

  let FLASH = { html: '' };
  await new Promise(r => server.listen(0, '127.0.0.1', r));
  const port = server.address().port;
  const origin = `http://127.0.0.1:${port}`;
  const frameId = require('node:crypto').randomUUID();
  const fUrl = frameUrl(frameId, origin);
  FLASH.html = parentHtml(frameId, fUrl);
  console.log('[bridge]', origin + '/index.html');
  console.log('[frame]', fUrl.slice(0, 110));

  const args = ['--no-sandbox','--disable-setuid-sandbox','--disable-dev-shm-usage',
    '--disable-gpu','--disable-blink-features=AutomationControlled','--lang=' + LOCALE,
    '--no-first-run','--no-default-browser-check'];
  if (PROXY) { const pu = new URL(PROXY); args.push(`--proxy-server=${pu.protocol}//${pu.host}`); }

  const browser = await puppeteer.launch({
    executablePath: '/usr/bin/google-chrome',
    headless: 'new',
    args, ignoreDefaultArgs: ['--enable-automation'],
  });
  const page = await browser.newPage();
  await page.setUserAgent('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/155.0.0.0 Safari/537.36');
  if (PROXY) { const pu = new URL(PROXY);
    if (pu.username) await page.authenticate({ username: decodeURIComponent(pu.username),
      password: decodeURIComponent(pu.password) }); }
  page.on('console', m => { const t = m.text();
    if (/error|fail/i.test(t) && t.length > 10) console.log('[console]', t.slice(0, 120)); });

  await page.goto(origin + '/index.html', { waitUntil: 'domcontentloaded', timeout: 60000 });
  const timeoutP = new Promise(r => setTimeout(() => r('timeout'), TIMEOUT * 1000));
  await Promise.race([done, timeoutP]);
  let status = '';
  try { status = await page.evaluate(() => document.getElementById('status').textContent); } catch (e) {}

  const out = { ok: false, status, events: state.events.length };
  if (state.result) Object.assign(out, { ok: true, token: state.result.response || '',
    ekey: state.result.ekey || '', duration: state.result.duration || 0 });
  else if (state.error) out.error = state.error;
  else { out.eventsTail = state.events.slice(-25); out.allEvents = state.events; }

  fs.writeFileSync(OUT, JSON.stringify(out, null, 2));
  console.log(JSON.stringify({ ok: out.ok, status: out.status, events: out.events,
    tokenLen: out.token ? out.token.length : 0, error: out.error }, null, 1));
  await browser.close();
  server.close();
  process.exit(out.ok ? 0 : 1);
})().catch(e => { console.error('FATAL', e.message); process.exit(1); });
