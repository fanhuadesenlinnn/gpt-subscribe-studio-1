// 只做一件事：让真实页面走到 confirm，把 confirm 请求体里的凭据整包交出来。
// 输出 /tmp/env_out.json
const puppeteer = require('puppeteer-core');
const fs = require('node:fs');
const CFG = JSON.parse(fs.readFileSync('/tmp/env_cfg.json', 'utf8'));
const sleep = ms => new Promise(r => setTimeout(r, ms));

(async () => {
  let browser = null;
  const hard = setTimeout(async () => {
    console.log('[hard-timeout]');
    if (browser) { try { await browser.close(); } catch (e) {} }
    process.exit(3);
  }, 330000);

  try {
    const pu = CFG.proxy ? new URL(CFG.proxy) : null;
    const args = ['--no-sandbox', '--disable-setuid-sandbox', '--disable-dev-shm-usage',
      '--disable-gpu', '--disable-blink-features=AutomationControlled',
      '--lang=en-US', '--no-first-run', '--no-default-browser-check'];
    if (pu) args.push(`--proxy-server=${pu.protocol}//${pu.host}`);

    browser = await puppeteer.launch({
      executablePath: '/usr/bin/google-chrome',
      headless: 'new', args,
      ignoreDefaultArgs: ['--enable-automation'],
      protocolTimeout: 180000,
    });
    const page = await browser.newPage();
    await page.setUserAgent('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/155.0.0.0 Safari/537.36');
    if (pu && pu.username) {
      await page.authenticate({ username: decodeURIComponent(pu.username),
                                password: decodeURIComponent(pu.password) });
    }

    let confirmBody = null;
    page.on('request', r => {
      if (/\/payment_pages\/[^/]+\/confirm/.test(r.url())) {
        const d = r.postData() || '';
        if (d.includes('passive_captcha_token')) { confirmBody = d; console.log('[confirm] captured', d.length); }
      }
    });

    console.log('[goto]', String(CFG.url).slice(0, 60));
    await page.goto(CFG.url, { waitUntil: 'domcontentloaded', timeout: 70000 }).catch(e => console.log('[goto-err]', e.message.slice(0, 60)));
    await sleep(8000);

    // email
    await page.evaluate((em) => {
      const el = document.querySelector('input[name="email"]');
      if (!el) return;
      Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set.call(el, em);
      el.dispatchEvent(new Event('input', { bubbles: true }));
    }, CFG.email).catch(() => {});
    await sleep(1500);

    // 选 Card
    const radio = await page.evaluate(() => {
      for (const l of document.querySelectorAll('label,div,span')) {
        if ((l.innerText || '').trim() === 'Card') {
          const r = l.getBoundingClientRect();
          if (r.width > 0 && r.height > 0) return { x: Math.round(r.x + 18), y: Math.round(r.y + r.height / 2) };
        }
      }
      return null;
    }).catch(() => null);
    if (radio) { await page.mouse.click(radio.x, radio.y); console.log('[card] clicked'); }
    await sleep(3200);

    // 逐字段填（等出现 + JS focus + 真键盘）
    const fill = async (name, value) => {
      let ok = false;
      for (let t = 0; t < 20 && !ok; t++) {
        ok = await page.evaluate((n) => {
          const el = document.querySelector(`[name="${n}"]`);
          if (!el || el.offsetParent === null) return false;
          el.scrollIntoView({ block: 'center' });
          el.focus();
          if (el.select) el.select();
          return document.activeElement === el;
        }, name).catch(() => false);
        if (!ok) await sleep(400);
      }
      if (!ok) { console.log('[skip]', name); return false; }
      await sleep(150);
      await page.keyboard.down('Control'); await page.keyboard.press('KeyA'); await page.keyboard.up('Control');
      await page.keyboard.press('Backspace');
      await page.keyboard.type(String(value), { delay: 45 });
      const got = await page.evaluate((n) => {
        const el = document.querySelector(`[name="${n}"]`);
        return el ? (el.value || '') : null;
      }, name).catch(() => null);
      console.log('[fill]', name, '=', got ? got.slice(0, 22) : '(空)');
      return !!got;
    };

    // 有效期必须逐位确认，Stripe 会自动插入 " / "
    const fillExpiry = async (value) => {
      let ok = false;
      for (let t = 0; t < 20 && !ok; t++) {
        ok = await page.evaluate(() => {
          const el = document.querySelector('[name="cardExpiry"]');
          if (!el || el.offsetParent === null) return false;
          el.scrollIntoView({ block: 'center' });
          el.focus();
          if (el.select) el.select();
          return document.activeElement === el;
        }).catch(() => false);
        if (!ok) await sleep(400);
      }
      if (!ok) { console.log('[skip] cardExpiry'); return; }
      await sleep(200);
      // 清空
      await page.keyboard.down('Control'); await page.keyboard.press('KeyA');
      await page.keyboard.up('Control'); await page.keyboard.press('Backspace');
      await sleep(120);
      // 逐位输入，每位后校验长度
      for (const ch of String(value)) {
        await page.keyboard.type(ch, { delay: 90 });
        await sleep(130);
      }
      await sleep(300);
      let got = await page.evaluate(() =>
        (document.querySelector('[name="cardExpiry"]') || {}).value || '').catch(() => '');
      console.log('[fill] cardExpiry =', got);
      // 如果年只剩 1 位，补最后一位
      const digits = got.replace(/\D/g, '');
      const want = String(value).replace(/\D/g, '');
      if (digits.length < want.length) {
        const missing = want.slice(digits.length);
        console.log('[fix] 补输入', missing);
        for (const ch of missing) { await page.keyboard.type(ch, { delay: 110 }); await sleep(150); }
        got = await page.evaluate(() =>
          (document.querySelector('[name="cardExpiry"]') || {}).value || '').catch(() => '');
        console.log('[fill] cardExpiry(修正后) =', got);
      }
    };

    await fill('cardNumber', CFG.card.number);
    await fillExpiry(CFG.card.exp);
    await fill('cardCvc', CFG.card.cvc);
    await fill('billingName', CFG.name);
    await fill('billingAddressLine1', CFG.line1);
    await fill('billingLocality', CFG.city);
    await fill('billingPostalCode', CFG.zip);

    // 州
    await page.evaluate((st) => {
      const s = document.querySelector('select[name="billingAddressState"],select[name="billingState"]');
      if (!s) return;
      const o = Array.from(s.options).find(o => o.value === st || new RegExp(st, 'i').test(o.text));
      if (o) { s.value = o.value; s.dispatchEvent(new Event('change', { bubbles: true })); }
    }, CFG.state).catch(() => {});
    await sleep(1200);

    // 勾选条款
    await page.evaluate(() => {
      document.querySelectorAll('input[type=checkbox]').forEach(b => { if (!b.checked) b.click(); });
    }).catch(() => {});
    await sleep(700);

    // 点 Subscribe
    const btn = await page.evaluate(() => {
      const b = Array.from(document.querySelectorAll('button'))
        .find(x => /subscribe/i.test(x.innerText || '') && !x.disabled);
      if (!b) return null;
      const r = b.getBoundingClientRect();
      return { x: Math.round(r.x + r.width / 2), y: Math.round(r.y + r.height / 2) };
    }).catch(() => null);
    // 提交前校验关键字段
    const check = await page.evaluate(() => {
      const g = n => (document.querySelector(`[name="${n}"]`) || {}).value || '';
      return { card: g('cardNumber').replace(/\D/g, ''), exp: g('cardExpiry').replace(/\D/g, ''),
               cvc: g('cardCvc'), zip: g('billingPostalCode'), city: g('billingLocality') };
    }).catch(() => ({}));
    console.log('[check]', JSON.stringify(check));
    const valid = check.card && check.card.length >= 15 && check.exp && check.exp.length === 4
                  && check.cvc && check.cvc.length >= 3 && check.zip;
    if (!valid) console.log('[abort] 字段不完整，不提交');
    else if (btn) await page.mouse.click(btn.x, btn.y);
    else console.log('[submit] button not found');

    // 点击后持续监控：页面文案变化 / 错误提示 / confirm
    let lastText = '';
    for (let i = 0; i < 80; i++) {
      await sleep(1000);
      if (confirmBody) break;
      if (i % 5 === 0) {
        const info = await page.evaluate(() => {
          const t = (document.body.innerText || '');
          const btn = Array.from(document.querySelectorAll('button'))
            .find(x => /subscribe|processing|pay/i.test(x.innerText || ''));
          const err = t.match(/(declined|insufficient[^\n]{0,50}|failed[^\n]{0,50}|invalid[^\n]{0,50}|incorrect[^\n]{0,50}|could not[^\n]{0,50}|required[^\n]{0,50})/i);
          return {
            btn: btn ? btn.innerText.trim().slice(0, 20) : null,
            disabled: btn ? btn.disabled : null,
            err: err ? err[0] : null,
            tail: t.slice(-160).replace(/\s+/g, ' '),
          };
        }).catch(() => ({}));
        console.log('[watch]', i + 's', JSON.stringify(info));
        if (info.err) { console.log('[page-error]', info.err); }
      }
    }

    const out = { ok: false };
    if (confirmBody) {
      const q = new URLSearchParams(confirmBody);
      const g = k => q.get(k) || '';
      Object.assign(out, {
        ok: true,
        token: g('passive_captcha_token'),
        ekey: g('passive_captcha_ekey'),
        guid: g('guid'), muid: g('muid'), sid: g('sid'),
        pm: g('payment_method'),
        key: g('key'),
        configId: g('client_attribution_metadata[checkout_config_id]'),
        sessionId: g('client_attribution_metadata[checkout_session_id]'),
        clientSessionId: g('client_attribution_metadata[client_session_id]'),
        rvTimestamp: g('rv_timestamp'),
        expectedAmount: g('expected_amount'),
      });
    }
    fs.writeFileSync('/tmp/env_out.json', JSON.stringify(out, null, 2));
    console.log('[done] ok =', out.ok, 'token =', (out.token || '').length);
  } finally {
    clearTimeout(hard);
    if (browser) { try { await browser.close(); } catch (e) {} }
    process.exit(0);
  }
})().catch(e => { console.error('FATAL', e.message.slice(0, 200)); process.exit(1); });
