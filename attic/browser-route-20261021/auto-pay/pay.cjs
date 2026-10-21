#!/usr/bin/env node
/**
 * Stripe Checkout 自动支付（protocol-first，参考 2025-03-31.basil 协议）
 * 用法: node pay.cjs <cs_live_xxx|url> [--card num|MM|YYYY|cvc] [--token HCaptchaToken]
 */
const fs = require('node:fs');
const crypto = require('node:crypto');
const undici = require('undici');

const STRIPE = 'https://api.stripe.com';
const V_FULL = '2025-03-31.basil; checkout_server_update_beta=v1; checkout_manual_approval_preview=v1';
const V_BASE = '2025-03-31.basil';
const UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36';
const PK = '${STRIPE_PUBLISHABLE_KEY}';
const HASH = '5412f474d5';
const PROXY = process.env.ORDER_PROXY || '';
const sleep = ms => new Promise(r => setTimeout(r, ms));
const log = (...a) => console.log(new Date().toISOString().slice(11, 23), ...a);

function arg(n, d) { const i = process.argv.indexOf('--' + n); return i > 0 ? process.argv[i + 1] : d; }

let dispatcher = null;
const form = o => Object.entries(o)
  .filter(([, v]) => v !== undefined && v !== null && v !== '')
  .map(([k, v]) => encodeURIComponent(k) + '=' + encodeURIComponent(String(v))).join('&');

async function req(method, url, body, headers = {}) {
  const h = { 'User-Agent': UA, 'Accept': 'application/json', ...headers };
  const opts = { method, headers: h };
  if (body) opts.body = body;
  if (dispatcher) opts.dispatcher = dispatcher;
  const r = await undici.request(url, opts);
  const text = await r.body.text();
  let json = null; try { json = JSON.parse(text); } catch (e) {}
  return { status: r.statusCode, text, json };
}

const HDR = {
  'Content-Type': 'application/x-www-form-urlencoded',
  'Origin': 'https://js.stripe.com',
  'Referer': 'https://js.stripe.com/',
};

// ── ① 指纹注册 m.stripe.com/6 ──────────────────────────────
function b64u(n = 32) { return crypto.randomBytes(n).toString('base64url'); }
function genId() { return crypto.randomUUID().replace(/-/g, '') + crypto.randomBytes(3).toString('hex'); }

async function registerFingerprint() {
  log('① m.stripe.com/6 注册指纹');
  let [guid, muid, sid] = [genId(), genId(), genId()];
  const fpId = crypto.randomBytes(16).toString('hex');
  const screens = [[1920,1080,1],[1536,864,1.25],[2560,1440,1],[1440,900,1]];
  const [sw, sh, dpr] = screens[Math.floor(Math.random()*screens.length)];
  const vh = sh - (40 + Math.floor(Math.random()*30));
  const cpu = [4,8,12,16][Math.floor(Math.random()*4)];

  const PLUGINS = 'PDF Viewer,internal-pdf-viewer,application/pdf,pdf++text/pdf,pdf, Chrome PDF Viewer,internal-pdf-viewer,application/pdf,pdf++text/pdf,pdf, Chromium PDF Viewer,internal-pdf-viewer,application/pdf,pdf++text/pdf,pdf, Microsoft Edge PDF Viewer,internal-pdf-viewer,application/pdf,pdf++text/pdf,pdf, WebKit built-in PDF,internal-pdf-viewer,application/pdf,pdf++text/pdf,pdf';

  const enc = o => Buffer.from(encodeURIComponent(JSON.stringify(o))).toString('base64');

  const buildFull = (v2, withIds) => ({
    v2, id: fpId, t: +(Math.random()*117+3).toFixed(1), tag: '$npm_package_version', src: 'js',
    a: {
      a: { v: 'true', t: 0 }, b: { v: 'true', t: 0 },
      c: { v: 'en-US', t: 0 }, d: { v: 'Win32', t: 0 },
      e: { v: PLUGINS, t: +(Math.random()*0.5).toFixed(1) },
      f: { v: `${sw}w_${vh}h_24d_${dpr}r`, t: 0 },
      g: { v: String(cpu), t: 0 }, h: { v: 'false', t: 0 },
      i: { v: 'sessionStorage-enabled, localStorage-enabled', t: +(Math.random()*1.5+0.5).toFixed(1) },
      j: { v: '0100100101111111101111101111111001110010110111110111111', t: +(Math.random()*115+5).toFixed(1) },
      k: { v: '', t: 0 }, l: { v: UA, t: 0 }, m: { v: '', t: 0 },
      n: { v: 'false', t: +(Math.random()*47+3).toFixed(1) },
      o: { v: 'd331ca493eb692cfcd19ae5db713ad4b', t: +(Math.random()*10+20).toFixed(1) },
    },
    b: {
      a: `https://${b64u(8)}.${b64u(8)}.${b64u(8)}/`,
      b: `https://${b64u(8)}.${b64u(8)}/${b64u(8)}/${b64u(8)}/${b64u(8)}`,
      c: b64u(32), d: withIds ? muid : 'NA', e: withIds ? sid : 'NA',
      f: false, g: true, h: true, i: ['location'], j: [],
      n: +(Math.random()*1200+800).toFixed(1), u: 'chatgpt.com', v: 'auth.openai.com',
      w: `${Date.now()}:${crypto.randomBytes(32).toString('hex')}`,
    },
    h: crypto.randomBytes(10).toString('hex'),
  });

  const buildMouse = (source) => ({
    muid, sid,
    url: `https://${b64u(8)}.${b64u(8)}/${b64u(8)}/${b64u(8)}/${b64u(8)}`,
    source, data: Array.from({length:10}, () => 1 + Math.floor(Math.random()*8)),
  });

  const H = { 'User-Agent': UA, 'Content-Type': 'text/plain;charset=UTF-8', 'Accept': '*/*',
              'Origin': 'https://m.stripe.network', 'Referer': 'https://m.stripe.network/' };

  const steps = [
    ['#1 full v2=1', buildFull(1, false), j => { muid = j.muid || muid; guid = j.guid || guid; sid = j.sid || sid; }],
    ['#2 full v2=2', buildFull(2, true),  j => { guid = j.guid || guid; }],
    ['#3 mouse v2',  buildMouse('mouse-timings-10-v2'), null],
    ['#4 mouse',     buildMouse('mouse-timings-10'),    null],
  ];
  for (const [name, payload, apply] of steps) {
    try {
      const r = await req('POST', 'https://m.stripe.com/6', enc(payload), H);
      if (r.status === 200 && r.json && apply) apply(r.json);
      log(`   ${name} → ${r.status}`);
    } catch (e) { log(`   ${name} 失败: ${e.message.slice(0,50)}`); }
  }
  log(`   guid=${guid.slice(0,16)} muid=${muid.slice(0,16)} sid=${sid.slice(0,16)}`);
  return { guid, muid, sid };
}

// ── ② init ────────────────────────────────────────────────
async function initCheckout(csId) {
  log('② init');
  const jsId = crypto.randomUUID();
  const elSessId = 'elements_session_' + crypto.randomBytes(6).toString('base64url').slice(0,11);
  let last = null;
  for (const ver of [V_BASE, V_FULL]) {
    const body = form({
      browser_locale: 'en-US',
      browser_timezone: 'America/Chicago',
      'elements_session_client[elements_init_source]': 'custom_checkout',
      'elements_session_client[referrer_host]': 'chatgpt.com',
      'elements_session_client[stripe_js_id]': jsId,
      'elements_session_client[locale]': 'en-US',
      'elements_session_client[is_aggregation_expected]': 'false',
      key: PK, _stripe_version: ver,
      ...(ver === V_FULL ? {
        'elements_session_client[client_betas][0]': 'custom_checkout_server_updates_1',
        'elements_session_client[client_betas][1]': 'custom_checkout_manual_approval_1',
      } : {}),
    });
    const r = await req('POST', `${STRIPE}/v1/payment_pages/${csId}/init`, body, HDR);
    log(`   ${ver.slice(0,26)} → ${r.status}`);
    if (r.status === 200) { last = r; break; }
    if (r.status === 400 && /beta/i.test(r.text)) continue;
    last = r; break;
  }
  if (!last || last.status !== 200) throw new Error(`init ${last && last.status}: ${(last && last.text || '').slice(0,200)}`);
  const j = last.json;
  const due = (j.total_summary || {}).due;
  const lineTotal = (j.line_items || []).reduce((s, x) => s + (x.amount || 0), 0);
  const ctx = {
    stripeJsId: jsId, elementsSessionId: elSessId,
    initChecksum: j.init_checksum || '', configId: j.config_id || '',
    amount: due != null ? String(due) : String(lineTotal),
    returnUrl: j.url || j.stripe_hosted_url || '',
    mode: j.mode, ver: V_FULL,
  };
  log(`   amount=${ctx.amount} mode=${ctx.mode} config=${ctx.configId.slice(0,12)}`);
  return { resp: j, ctx };
}

// ── ③ elements/sessions ───────────────────────────────────
async function elementsSession(csId, ctx) {
  log('③ elements/sessions');
  const q = new URLSearchParams({
    'client_betas[0]': 'custom_checkout_server_updates_1',
    'client_betas[1]': 'custom_checkout_manual_approval_1',
    'deferred_intent[mode]': 'subscription',
    'deferred_intent[amount]': '0',
    'deferred_intent[currency]': 'usd',
    'deferred_intent[setup_future_usage]': 'off_session',
    'deferred_intent[payment_method_types][0]': 'card',
    currency: 'usd', key: PK, _stripe_version: V_FULL,
    elements_init_source: 'custom_checkout', referrer_host: 'chatgpt.com',
    stripe_js_id: ctx.stripeJsId, locale: 'en', type: 'deferred_intent',
    checkout_session_id: csId,
  });
  const r = await req('GET', `${STRIPE}/v1/elements/sessions?${q}`, null, HDR);
  log(`   → ${r.status}`);
  if (r.status === 200 && r.json) {
    if (r.json.session_id || r.json.id) ctx.elementsSessionId = r.json.session_id || r.json.id;
    if (r.json.config_id) ctx.configId = r.json.config_id;
    log(`   sess=${String(ctx.elementsSessionId).slice(0,20)} config=${ctx.configId.slice(0,12)}`);
  }
  return r;
}

// ── ④ consumers/lookup ────────────────────────────────────
async function lookupConsumer(email, ctx) {
  log('④ consumers/sessions/lookup');
  for (const [surface, source] of [
    ['web_link_authentication_in_payment_element', 'default_value'],
    ['web_elements_controller', 'default_value'],
  ]) {
    const body = form({
      request_surface: surface, email_address: email, email_source: source,
      session_id: crypto.randomUUID(), key: PK, _stripe_version: V_FULL,
      ...(surface === 'web_elements_controller' ? { do_not_log_consumer_funnel_event: 'true' } : {}),
    });
    const r = await req('POST', `${STRIPE}/v1/consumers/sessions/lookup`, body, HDR);
    log(`   ${surface.slice(0,24)} → ${r.status}`);
    await sleep(300 + Math.random()*500);
  }
}

// ── ⑤ 逐字段地址提交（关键） ──────────────────────────────
async function updateAddress(csId, billing, ctx) {
  log('⑤ 逐字段提交地址（6 次）');
  const base = {
    'elements_session_client[client_betas][0]': 'custom_checkout_server_updates_1',
    'elements_session_client[client_betas][1]': 'custom_checkout_manual_approval_1',
    'elements_session_client[elements_init_source]': 'custom_checkout',
    'elements_session_client[referrer_host]': 'chatgpt.com',
    'elements_session_client[session_id]': ctx.elementsSessionId,
    'elements_session_client[stripe_js_id]': ctx.stripeJsId,
    'elements_session_client[locale]': 'en-US',
    'elements_session_client[is_aggregation_expected]': 'false',
    'client_attribution_metadata[merchant_integration_additional_elements][0]': 'payment',
    'client_attribution_metadata[merchant_integration_additional_elements][1]': 'address',
    key: PK, _stripe_version: V_FULL,
  };
  const steps = [
    ['country', { 'tax_region[country]': billing.country || 'US' }],
    ['(focus)', {}],
    ['line1', { 'tax_region[line1]': billing.line1 }],
    ['city', { 'tax_region[city]': billing.city }],
    ['state', { 'tax_region[state]': billing.state }],
    ['postal', { 'tax_region[postal_code]': billing.zip }],
  ];
  const acc = {};
  for (let i = 0; i < steps.length; i++) {
    const [name, add] = steps[i];
    Object.assign(acc, add);
    const r = await req('POST', `${STRIPE}/v1/payment_pages/${csId}`, form({ ...base, ...acc }), HDR);
    log(`   [${i+1}/6] ${name.padEnd(8)} → ${r.status}`);
    if (r.status !== 200) log(`       ${(r.text || '').slice(0,120)}`);
    await sleep(2000 + Math.random()*2500);
  }
}

// ── ⑥ payment_methods ────────────────────────────────────
async function createPM(card, billing, fp, csId, ctx, token) {
  log('⑥ payment_methods');
  const body = form({
    'billing_details[name]': billing.name,
    'billing_details[email]': billing.email,
    'billing_details[address][country]': billing.country || 'US',
    'billing_details[address][line1]': billing.line1,
    'billing_details[address][city]': billing.city,
    'billing_details[address][postal_code]': billing.zip,
    'billing_details[address][state]': billing.state,
    type: 'card',
    'card[number]': card.number,
    'card[cvc]': card.cvc,
    'card[exp_year]': card.expYear,
    'card[exp_month]': card.expMonth,
    allow_redisplay: 'unspecified',
    payment_user_agent: `stripe.js/${HASH}; stripe-js-v3/${HASH}; payment-element; deferred-intent`,
    referrer: 'https://chatgpt.com',
    time_on_page: String(25000 + Math.floor(Math.random()*30000)),
    'client_attribution_metadata[client_session_id]': crypto.randomUUID(),
    'client_attribution_metadata[checkout_session_id]': csId,
    'client_attribution_metadata[merchant_integration_source]': 'elements',
    'client_attribution_metadata[merchant_integration_subtype]': 'payment-element',
    'client_attribution_metadata[merchant_integration_version]': '2021',
    'client_attribution_metadata[payment_intent_creation_flow]': 'deferred',
    'client_attribution_metadata[payment_method_selection_flow]': 'automatic',
    guid: fp.guid, muid: fp.muid, sid: fp.sid,
    key: PK, _stripe_version: V_FULL,
    ...(token ? { 'radar_options[hcaptcha_token]': token } : {}),
  });
  const r = await req('POST', `${STRIPE}/v1/payment_methods`, body, HDR);
  log(`   → ${r.status}`);
  if (r.status !== 200) throw new Error(`pm ${r.status}: ${r.text.slice(0,400)}`);
  const pm = r.json;
  log(`   ${pm.id} (${(pm.card||{}).display_brand || '?'} ****${(pm.card||{}).last4 || '????'})`);
  return pm.id;
}

// ── ⑦ confirm ────────────────────────────────────────────
async function confirm(csId, pmId, fp, ctx, token) {
  log('⑦ confirm');
  const body = form({
    guid: fp.guid, muid: fp.muid, sid: fp.sid,
    payment_method: pmId,
    expected_amount: ctx.amount,
    expected_payment_method_type: 'card',
    'consent[terms_of_service]': 'accepted',
    key: PK, _stripe_version: V_FULL,
    init_checksum: ctx.initChecksum,
    version: HASH,
    return_url: ctx.returnUrl,
    'elements_session_client[elements_init_source]': 'custom_checkout',
    'elements_session_client[referrer_host]': 'chatgpt.com',
    'elements_session_client[stripe_js_id]': ctx.stripeJsId,
    'elements_session_client[locale]': 'en-US',
    'elements_session_client[is_aggregation_expected]': 'false',
    'elements_session_client[session_id]': ctx.elementsSessionId,
    'elements_session_client[client_betas][0]': 'custom_checkout_server_updates_1',
    'elements_session_client[client_betas][1]': 'custom_checkout_manual_approval_1',
    'client_attribution_metadata[client_session_id]': ctx.stripeJsId,
    'client_attribution_metadata[checkout_session_id]': csId,
    'client_attribution_metadata[checkout_config_id]': ctx.configId,
    'client_attribution_metadata[elements_session_id]': ctx.elementsSessionId,
    'client_attribution_metadata[elements_session_config_id]': crypto.randomUUID(),
    'client_attribution_metadata[merchant_integration_source]': 'checkout',
    'client_attribution_metadata[merchant_integration_subtype]': 'payment-element',
    'client_attribution_metadata[merchant_integration_version]': 'custom',
    'client_attribution_metadata[payment_intent_creation_flow]': 'deferred',
    'client_attribution_metadata[payment_method_selection_flow]': 'automatic',
    'client_attribution_metadata[merchant_integration_additional_elements][0]': 'payment',
    'client_attribution_metadata[merchant_integration_additional_elements][1]': 'address',
    ...(token ? { passive_captcha_token: token } : {}),
  });
  const r = await req('POST', `${STRIPE}/v1/payment_pages/${csId}/confirm`, body, HDR);
  log(`   → ${r.status} (${r.text.length} chars)`);
  if (r.status !== 200) log(`   ${(r.text || '').slice(0,400)}`);
  return r;
}

// ── ⑧ poll ───────────────────────────────────────────────
async function poll(csId, tries = 15) {
  log('⑧ poll');
  let last = null;
  for (let i = 0; i < tries; i++) {
    await sleep(2000);
    const q = new URLSearchParams({ key: PK, _stripe_version: V_BASE });
    const r = await req('GET', `${STRIPE}/v1/payment_pages/${csId}/poll?${q}`, null, HDR);
    last = r.json || { raw: r.text.slice(0,300) };
    const st = last.state || last.payment_object_status;
    log(`   [${i+1}/${tries}] ${r.status} state=${st} payment=${last.payment_object_status}`);
    if (['succeeded','failed','expired','canceled'].includes(String(st))) break;
  }
  return last;
}

// ── 判定 ─────────────────────────────────────────────────
function classify(confirmRes, pollRes) {
  const out = { state:'unknown', decline_code:null, error_code:null, message:null, intent_status:null };
  const scan = (o) => {
    if (!o || typeof o !== 'object') return;
    for (const h of ['payment_intent','setup_intent','payment_method_object']) {
      const x = o[h];
      if (x && typeof x === 'object') {
        if (x.status) out.intent_status = x.status;
        const e = x.last_payment_error || x.last_setup_error || {};
        if (e && Object.keys(e).length) {
          out.error_code = e.code || out.error_code;
          out.decline_code = e.decline_code || out.decline_code;
          out.message = (e.message || out.message || '').slice(0,200);
        }
      }
    }
    if (o.error) {
      out.error_code = out.error_code || o.error.code;
      out.decline_code = out.decline_code || o.error.decline_code;
      out.message = out.message || String(o.error.message || '').slice(0,200);
    }
    if (o.state) out.state_hint = o.state;
  };
  scan(confirmRes && confirmRes.json);
  scan(pollRes);
  const st = String(out.intent_status || '').toLowerCase();
  if (['succeeded','requires_capture'].includes(st)) out.state = 'paid';
  else if (out.decline_code || out.error_code) {
    if (/captcha/i.test(out.message || '')) out.state = 'captcha_rejected';
    else if (st === 'requires_payment_method' || out.decline_code) out.state = 'card_declined';
    else out.state = 'error';
  } else if (st === 'requires_action') out.state = '3ds_required';
  else if (out.state_hint === 'failed') out.state = 'card_declined';
  return out;
}

// ── main ─────────────────────────────────────────────────
(async () => {
  const t0 = Date.now();
  if (PROXY) dispatcher = new undici.ProxyAgent(PROXY);

  const target = process.argv[2] || arg('checkout-url', '');
  const m = String(target).match(/(cs_(?:live|test)_[A-Za-z0-9]+)/);
  if (!m) { console.error('用法: node pay.cjs <cs_live_xxx|url>'); process.exit(2); }
  const csId = m[1];

  const ca = arg('card', '<CARD_NUMBER_PLACEHOLDER>|02|2029|135').split('|');
  const card = {
    number: ca[0],
    expMonth: String(ca[1] || '02').replace(/\D/g,'').padStart(2,'0'),
    expYear: String(ca[2] || '2029').replace(/\D/g,''),
    cvc: ca[3] || '135',
  };
  const billing = {
    name: arg('name', 'FIXTURE HOLDER'),
    email: arg('email', '<BILLING_EMAIL>'),
    line1: arg('line1', '1 Fixture St'),
    city: arg('city', 'Portland'), state: arg('state', 'OR'),
    zip: arg('zip', '97211'), country: 'US',
  };
  const manualToken = arg('token', '');
  const outPath = arg('out', '/tmp/pay_result.json');
  const result = { csId, card: card.number.slice(-4) };

  try {
    log(`目标 ${csId.slice(0,20)}...  卡 ****${card.number.slice(-4)} ${card.expMonth}/${card.expYear}`);

    const fp = await registerFingerprint();     result.fp = { guid: fp.guid, muid: fp.muid, sid: fp.sid };
    const { resp: initResp, ctx } = await initCheckout(csId);
    result.init = { amount: ctx.amount, mode: ctx.mode };
    await elementsSession(csId, ctx);
    await lookupConsumer(billing.email, ctx);
    await updateAddress(csId, billing, ctx);

    const pmId = await createPM(card, billing, fp, csId, ctx, manualToken);
    result.pm = pmId;

    const cf = await confirm(csId, pmId, fp, ctx, manualToken);
    result.confirm = { status: cf.status, body: cf.json || cf.text.slice(0, 1500) };

    let pr = null;
    const c1 = classify(cf, null);
    if (!['paid','card_declined'].includes(c1.state)) { pr = await poll(csId); result.poll = pr; }
    result.verdict = classify(cf, pr);
    log(`判定: ${result.verdict.state}  decline=${result.verdict.decline_code}  msg=${String(result.verdict.message||'').slice(0,70)}`);
  } catch (e) {
    result.error = String(e.message).slice(0, 500);
    log('失败:', result.error);
  }
  result.elapsed = ((Date.now()-t0)/1000).toFixed(1) + 's';
  fs.writeFileSync(outPath, JSON.stringify(result, null, 2, ));
  const v = result.verdict || {};
  console.log('\n' + JSON.stringify({ state:v.state, decline_code:v.decline_code, message:v.message,
    elapsed: result.elapsed, error: result.error }, null, 1));
  console.log('saved ->', outPath);
  process.exit(0);
})();
