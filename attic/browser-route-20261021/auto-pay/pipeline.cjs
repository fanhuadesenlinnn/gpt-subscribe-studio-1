#!/usr/bin/env node
/**
 * ChatGPT 订阅自动开通流水线
 * 架构：协议层全 HTTP，浏览器只负责产 passive_captcha_token
 *
 * node pipeline.cjs --checkout-url <url> [--card num|MM|YYYY|cvc]
 * node pipeline.cjs --session <AT>
 */
const { execFileSync } = require('node:child_process');
const path = require('node:path');
const fs = require('node:fs');
// undici 的 ProxyAgent 对 HTTPS-over-proxy 更可靠（https.request + agent 会 TLS 断连）
const undici = require('undici');

const API_HOST = process.env.GSS_API;
if (!API_HOST) {
  console.error('缺少环境变量 GSS_API（例：export GSS_API=http://127.0.0.1:8080）');
  process.exit(1);
}
const STRIPE = 'https://api.stripe.com';
const UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/155.0.0.0 Safari/537.36';
const V_BASE = '2020-08-27;custom_checkout_beta=v1';
const PK = '${STRIPE_PUBLISHABLE_KEY}';
const sleep = ms => new Promise(r => setTimeout(r, ms));
const log = (...a) => console.log(new Date().toISOString().slice(11, 19), ...a);

function arg(n, d) { const i = process.argv.indexOf('--' + n); return i > 0 ? process.argv[i + 1] : d; }
const PROXY = arg('proxy', process.env.ORDER_PROXY || '');
let agent = undefined;   // 在 main 里异步初始化

let dispatcher = null;
async function initDispatcher() {
  if (PROXY) dispatcher = new undici.ProxyAgent(PROXY);
}

async function req(method, url, body, extraHeaders = {}) {
  const headers = { 'User-Agent': UA, 'Accept': 'application/json', ...extraHeaders };
  const opts = { method, headers };
  if (body) opts.body = body;
  if (dispatcher) opts.dispatcher = dispatcher;
  const r = await undici.request(url, opts);
  const text = await r.body.text();
  let json = null;
  try { json = JSON.parse(text); } catch (e) {}
  return { status: r.statusCode, text, json };
}

const form = obj => Object.entries(obj)
  .filter(([, v]) => v !== undefined && v !== null && v !== '')
  .map(([k, v]) => encodeURIComponent(k) + '=' + encodeURIComponent(String(v)))
  .join('&');

const HDR_STRIPE = {
  'Content-Type': 'application/x-www-form-urlencoded',
  'Origin': 'https://pay.openai.com',
  'Referer': 'https://pay.openai.com/',
};

// ── ① 建订单 ────────────────────────────────────────────────
async function createOrder(sessionAt, country = 'US') {
  log('① 建订单');
  const pw = JSON.parse(fs.readFileSync('/run/gss-auth.json', 'utf8')).password;
  const login = await req('POST', API_HOST + '/login', form({ password: pw }),
    { 'Content-Type': 'application/x-www-form-urlencoded', Origin: API_HOST });
  const cookie = (login.json && '') || '';
  // 用 python 里已有的能力更稳：直接调容器内 API
  const script = `
import json, httpx
from pathlib import Path
a = json.loads(Path("/run/gss-auth.json").read_text()); b = "${API_HOST}"
c = httpx.Client(base_url=b, timeout=180, follow_redirects=False)
c.post("/login", data={"password": a["password"]}, headers={"Origin": b})
r = c.post("/api/orders", headers={"Origin": b}, json={"plan":"team","billing_country":"${country}",
    "session":"${sessionAt}","seat_quantity":2,"workspace_name":"Team Workspace","price_interval":"month"})
if r.status_code != 201:
    print("ERR", r.status_code, r.text[:200]); raise SystemExit(1)
oid = r.json()["id"]
d = c.get(f"/api/orders/{oid}", headers={"Origin": b}).json()
print("OK", json.dumps({"order_id": oid, "checkout_url": d.get("checkout_url")}))
`;
  const out = execFileSync('python', ['-'],
    { input: script, encoding: 'utf8', timeout: 240000 });
  const line = out.split('\n').find(l => l.startsWith('OK '));
  if (!line) throw new Error('建单失败: ' + out.slice(0, 200));
  const o = JSON.parse(line.slice(3));
  log('   订单', o.order_id.slice(0, 8), String(o.checkout_url).slice(0, 55));
  return o;
}

// ── ② init ─────────────────────────────────────────────────
async function initCheckout(csId) {
  log('② init');
  const jsId = crypto.randomUUID();
  const body = form({
    browser_locale: 'en-US',
    browser_timezone: 'America/Los_Angeles',
    'elements_session_client[elements_init_source]': 'custom_checkout',
    'elements_session_client[referrer_host]': 'chatgpt.com',
    'elements_session_client[stripe_js_id]': jsId,
    'elements_session_client[locale]': 'en-US',
    'elements_session_client[is_aggregation_expected]': 'false',
    key: PK,
    _stripe_version: V_BASE,
  });
  const r = await req('POST', `${STRIPE}/v1/payment_pages/${csId}/init`, body, HDR_STRIPE);
  if (r.status !== 200) throw new Error(`init ${r.status}: ${r.text.slice(0, 200)}`);
  const due = (r.json.total_summary || {}).due;
  const ctx = {
    configId: r.json.config_id || '',
    returnUrl: r.json.return_url || r.json.url || '',
    hostedUrl: r.json.stripe_hosted_url || '',
    locale: r.json.locale || 'en',
    currency: (r.json.currency || 'usd').toLowerCase(),
    amount: due != null ? String(due) : '0',
    stripeJsId: jsId,
  };
  log(`   amount=${ctx.amount} ${ctx.currency} config=${ctx.configId.slice(0, 12)}`);
  return { resp: r.json, ctx };
}

// ── ③ 浏览器取 token ────────────────────────────────────────
function collectEnv(checkoutUrl, card, billing) {
  log('③ 浏览器取 token');
  fs.writeFileSync('/tmp/env_cfg.json', JSON.stringify({
    url: checkoutUrl, proxy: PROXY, email: billing.email,
    card: { number: card.number, exp: card.exp, cvc: card.cvc },
    name: billing.name, line1: billing.line1, city: billing.city,
    state: billing.state, zip: billing.zip,
  }));
  try {
    const out = execFileSync('node', [path.join(__dirname, 'collect_env3.cjs')], {
      encoding: 'utf8', timeout: 360000,
      env: { ...process.env, DISPLAY: ':99', ORDER_PROXY: PROXY },
      stdio: ['ignore', 'pipe', 'pipe'],
    });
    process.stderr.write(out.split('\n').filter(l => /\[(fill|submit|confirm|card|done)/.test(l)).join('\n') + '\n');
  } catch (e) {
    if (e.stdout) process.stderr.write(String(e.stdout).slice(-800));
  }
  const env = JSON.parse(fs.readFileSync('/tmp/env_out.json', 'utf8'));
  if (!env.ok) throw new Error('浏览器未拿到 token: ' + JSON.stringify(env).slice(0, 160));
  log(`   token=${(env.token || '').length} guid=${String(env.guid).slice(0, 12)}`);
  return env;
}

// ── ④ payment_methods ──────────────────────────────────────
async function createPM(card, billing, env, configId) {
  log('④ payment_methods');
  const digits = String(card.exp).replace(/\D/g, '');
  const mm = digits.slice(0, 2);
  let yy = digits.slice(2);
  if (yy.length === 2) yy = '20' + yy;
  const body = form({
    type: 'card',
    'card[number]': card.number,
    'card[exp_month]': mm,
    'card[exp_year]': yy,
    'card[cvc]': card.cvc,
    'billing_details[name]': billing.name,
    'billing_details[address][line1]': billing.line1,
    'billing_details[address][city]': billing.city,
    'billing_details[address][state]': billing.state,
    'billing_details[address][postal_code]': billing.zip,
    'billing_details[address][country]': 'US',
    guid: env.guid, muid: env.muid, sid: env.sid, key: PK,
    _stripe_version: V_BASE,
    payment_user_agent: 'stripe.js/2bae21f786; stripe-js-v3/2bae21f786; checkout',
    'client_attribution_metadata[client_session_id]': crypto.randomUUID(),
    'client_attribution_metadata[checkout_config_id]': configId,
    'client_attribution_metadata[merchant_integration_source]': 'checkout',
    'client_attribution_metadata[merchant_integration_version]': 'hosted_checkout',
    'client_attribution_metadata[payment_method_selection_flow]': 'merchant_specified',
  });
  const r = await req('POST', `${STRIPE}/v1/payment_methods`, body, HDR_STRIPE);
  if (r.status !== 200) throw new Error(`pm ${r.status}: ${r.text.slice(0, 300)}`);
  log('   ' + r.json.id);
  return r.json.id;
}

// ── ⑤ confirm ──────────────────────────────────────────────
async function confirm(csId, pm, env, ctx) {
  log('⑤ confirm');
  const body = form({
    eid: 'NA',
    payment_method: pm,
    expected_amount: ctx.amount,
    'consent[terms_of_service]': 'accepted',
    save_payment_method: 'true',
    expected_payment_method_type: 'card',
    _stripe_version: V_BASE,
    guid: env.guid, muid: env.muid, sid: env.sid, key: PK,
    passive_captcha_token: env.token,
    passive_captcha_ekey: env.ekey,
    rv_timestamp: env.rvTimestamp,
    'client_attribution_metadata[client_session_id]': crypto.randomUUID(),
    'client_attribution_metadata[checkout_session_id]': csId,
    'client_attribution_metadata[merchant_integration_source]': 'checkout',
    'client_attribution_metadata[merchant_integration_version]': 'hosted_checkout',
    'client_attribution_metadata[payment_method_selection_flow]': 'merchant_specified',
    'client_attribution_metadata[checkout_config_id]': ctx.configId,
    link_brand: 'link',
  });
  const r = await req('POST', `${STRIPE}/v1/payment_pages/${csId}/confirm`, body, HDR_STRIPE);
  log('   HTTP', r.status, r.text.length, 'chars');
  return { status: r.status, json: r.json, text: r.text };
}

// ── ⑥ poll ─────────────────────────────────────────────────
async function poll(csId, ctx, tries = 6) {
  log('⑥ poll');
  let last = null;
  for (let i = 0; i < tries; i++) {
    const r = await req('POST', `${STRIPE}/v1/payment_pages/${csId}/poll`,
      form({ key: PK, _stripe_version: V_BASE }), HDR_STRIPE);
    last = r.json || { raw: r.text.slice(0, 300) };
    const st = (last.payment_intent || {}).status || last.status;
    log(`   [${i + 1}] HTTP ${r.status} status=${st}`);
    if (['succeeded', 'requires_capture', 'canceled'].includes(st)) break;
    if ((last.payment_intent || {}).last_payment_error) break;
    await sleep(3000);
  }
  return last;
}

// ── 判定 ───────────────────────────────────────────────────
function classify(res) {
  const out = { state: 'unknown', decline_code: null, error_code: null, message: null, intent_status: null };
  const j = res && (res.json || res);
  if (!j) return out;
  for (const h of ['payment_intent', 'setup_intent']) {
    const o = j[h];
    if (o && typeof o === 'object') {
      out.intent_status = o.status;
      const e = o.last_payment_error || o.last_setup_error || {};
      if (e && Object.keys(e).length) {
        out.error_code = e.code || null;
        out.decline_code = e.decline_code || null;
        out.message = (e.message || '').slice(0, 200);
      }
    }
  }
  if (j.error) {
    out.error_code = out.error_code || j.error.code || null;
    out.decline_code = out.decline_code || j.error.decline_code || null;
    out.message = out.message || String(j.error.message || '').slice(0, 200);
  }
  const st = String(out.intent_status || '').toLowerCase();
  if (['succeeded', 'requires_capture'].includes(st)) out.state = 'paid';
  else if (out.decline_code || out.error_code) {
    if (/captcha/i.test(out.message || '')) out.state = 'captcha_rejected';
    else if (st === 'requires_payment_method' || out.decline_code) out.state = 'card_declined';
    else out.state = 'error';
  } else if (st === 'requires_action') out.state = '3ds_required';
  return out;
}

// ── 主流程 ─────────────────────────────────────────────────
(async () => {
  const t0 = Date.now();
  await initDispatcher();
  const cardArg = arg('card', '<CARD_NUMBER_PLACEHOLDER>|02|2029|135').split('|');
  // 统一成 MMYY（4 位）：年取后两位，避免出现 022029 这种 6 位错误
  const mmRaw = String(cardArg[1] || '02').replace(/\D/g, '').padStart(2, '0').slice(-2);
  const yyRaw = String(cardArg[2] || '2029').replace(/\D/g, '');
  const yy2 = yyRaw.length >= 4 ? yyRaw.slice(-2) : yyRaw.padStart(2, '0');
  const card = { number: cardArg[0], exp: mmRaw + yy2, cvc: cardArg[3] || '135' };
  log('卡: ****' + card.number.slice(-4), 'exp=' + card.exp, 'cvc=' + card.cvc);
  const billing = {
    name: arg('name', 'Fixture Holder'), line1: arg('line1', '1 Fixture St'),
    city: arg('city', 'Portland'), state: arg('state', 'OR'),
    zip: arg('zip', '97211'), email: arg('email', '<BILLING_EMAIL>'),
  };
  const result = { card: card.number.slice(-4), proxy: !!PROXY };
  try {
    let checkoutUrl = arg('checkout-url', '');
    if (!checkoutUrl) {
      const o = await createOrder(arg('session', ''));
      checkoutUrl = o.checkout_url; result.order_id = o.order_id;
    }
    const m = checkoutUrl.match(/(cs_live_[A-Za-z0-9]+)/);
    if (!m) throw new Error('URL 无 cs_live_');
    const csId = m[1];
    result.csId = csId; result.checkoutUrl = checkoutUrl;

    const { ctx } = await initCheckout(csId);
    result.init = { amount: ctx.amount, currency: ctx.currency };

    const env = collectEnv(checkoutUrl, card, billing);
    result.env = { guid: env.guid, muid: env.muid, sid: env.sid, configId: env.configId };
    result.tokenLen = (env.token || '').length;

    const pm = await createPM(card, billing, env, ctx.configId);
    result.pm = pm;

    const cf = await confirm(csId, pm, env, ctx);
    result.confirm = { status: cf.status, body: cf.json || cf.text.slice(0, 1200) };
    result.verdict = classify({ json: cf.json });
    log(`   判定 ${result.verdict.state} decline=${result.verdict.decline_code} msg=${String(result.verdict.message || '').slice(0, 60)}`);

    if (['unknown', '3ds_required'].includes(result.verdict.state)) {
      const p = await poll(csId, ctx);
      result.poll = p;
      const c2 = classify({ json: p });
      if (c2.state !== 'unknown') { result.verdict = c2; log(`   poll 判定 ${c2.state} decline=${c2.decline_code}`); }
    }
  } catch (e) {
    result.error = String(e.message).slice(0, 400);
    log('失败:', result.error);
  }
  result.elapsed = ((Date.now() - t0) / 1000).toFixed(1) + 's';
  const out = arg('out', '/tmp/auto_pay_result.json');
  fs.writeFileSync(out, JSON.stringify(result, null, 2));
  const v = result.verdict || {};
  console.log('\n' + JSON.stringify({
    state: v.state, decline_code: v.decline_code, message: v.message,
    tokenLen: result.tokenLen, elapsed: result.elapsed, error: result.error,
  }, null, 1));
  console.log('saved ->', out);
})();
