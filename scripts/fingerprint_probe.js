#!/usr/bin/env node
/**
 * Capture the host machine's own browser fingerprint (canvas / audio / WebGL /
 * screen / cores), so the protocol device profile matches the box it runs on
 * instead of inventing a macOS one on a Linux host.
 *
 * Usage:
 *   CHROME_PATH=/usr/bin/google-chrome node scripts/fingerprint_probe.js > /opt/gss/data/device.json
 *   GSS_DEVICE_FINGERPRINT=/opt/gss/data/device.json ...
 *
 * The script needs `puppeteer-core`; reuse the solver's node_modules:
 *   PUPPETEER_PATH=/opt/gss/tools/hcaptcha-solver/node_modules/puppeteer-core
 */
'use strict';

const crypto = require('node:crypto');
const puppeteer = require(process.env.PUPPETEER_PATH || 'puppeteer-core');

const CHROME = process.env.CHROME_PATH || (() => {
  for (const candidate of [
    '/usr/bin/google-chrome',
    '/usr/bin/chromium',
    '/usr/bin/chromium-browser',
    '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
  ]) {
    try { if (require('node:fs').existsSync(candidate)) return candidate; } catch (_) {}
  }
  return '/usr/bin/google-chrome';
})();

(async () => {
  const browser = await puppeteer.launch({
    executablePath: CHROME,
    headless: process.env.HEADLESS === 'false' ? false : 'shell',
    args: [
      '--no-sandbox',
      '--disable-dev-shm-usage',
      '--use-gl=angle',
      '--use-angle=gl',
      '--enable-unsafe-swiftshader',
      '--ignore-gpu-blocklist',
      '--window-size=1920,1080',
    ],
    defaultViewport: null,
  });
  const page = await browser.newPage();
  await page.goto('about:blank');

  const raw = await page.evaluate(async () => {
    const canvas = document.createElement('canvas');
    canvas.width = 280;
    canvas.height = 80;
    const ctx = canvas.getContext('2d');
    ctx.textBaseline = 'top';
    ctx.font = '14px "Arial"';
    ctx.fillStyle = '#f60';
    ctx.fillRect(2, 2, 120, 24);
    ctx.fillStyle = '#069';
    ctx.fillText('fingerprint-probe-\u2601', 4, 20);
    ctx.strokeStyle = 'rgba(102,204,0,0.7)';
    ctx.beginPath();
    ctx.arc(180, 40, 30, 0, Math.PI * 2);
    ctx.stroke();

    let audio = '';
    try {
      const OfflineCtx = window.OfflineAudioContext || window.webkitOfflineAudioContext;
      const offline = new OfflineCtx(1, 44100, 44100);
      const osc = offline.createOscillator();
      osc.type = 'triangle';
      osc.frequency.value = 10000;
      const compressor = offline.createDynamicsCompressor();
      compressor.threshold.value = -50;
      compressor.knee.value = 40;
      compressor.ratio.value = 12;
      compressor.attack.value = 0;
      compressor.release.value = 0.25;
      osc.connect(compressor);
      compressor.connect(offline.destination);
      osc.start(0);
      const buffer = await offline.startRendering();
      const data = buffer.getChannelData(0);
      let sum = 0;
      for (let i = 0; i < data.length; i += 1) sum += Math.abs(data[i]);
      audio = sum.toString(16).replace('.', '').slice(0, 32);
    } catch (err) {
      audio = '';
    }

    let webgl = { vendor: '', renderer: '' };
    try {
      const gl = document.createElement('canvas').getContext('webgl');
      const ext = gl && gl.getExtension('WEBGL_debug_renderer_info');
      if (ext) {
        webgl = {
          vendor: String(gl.getParameter(ext.UNMASKED_VENDOR_WEBGL) || ''),
          renderer: String(gl.getParameter(ext.UNMASKED_RENDERER_WEBGL) || ''),
        };
      }
    } catch (err) { /* keep empty */ }

    return {
      canvas: canvas.toDataURL(),
      audio,
      webgl,
      ua: navigator.userAgent,
      platform: navigator.platform,
      languages: navigator.languages,
      cores: navigator.hardwareConcurrency || 0,
      memory: navigator.deviceMemory || 0,
      screen: [screen.width, screen.height, screen.availHeight, window.devicePixelRatio || 1],
      timezone: Intl.DateTimeFormat().resolvedOptions().timeZone || '',
    };
  });

  const digest = crypto.createHash('sha256').update(raw.canvas).digest('hex');
  const out = {
    captured_at: new Date().toISOString(),
    ua: raw.ua,
    platform: raw.platform,
    languages: raw.languages,
    cores: raw.cores,
    memory: raw.memory,
    screen: raw.screen,
    timezone: raw.timezone,
    webgl_vendor: raw.webgl.vendor,
    webgl_renderer: raw.webgl.renderer,
    canvas_seed: digest,
    audio_seed: raw.audio || digest.slice(0, 32),
    canvas_sha256: digest,
  };
  process.stdout.write(JSON.stringify(out, null, 1) + '\n');
  await browser.close();
})().catch((err) => {
  process.stderr.write(String(err && err.stack || err) + '\n');
  process.exit(1);
});
