#!/usr/bin/env node
// C3 review: a >4.9 GiB input through the real page on a fresh profile —
// the uncertain-disk dialog must appear, confirm → copy → probe → convert →
// save (OPFS picker stub); saved sha256 == native CLI output.
//
//   node tests/browser/slide_tools_c3/run_large_page.js --input <big.kfb> [--native-sha <hex>]
'use strict';
const fs = require('fs');
const L = require('./lib.js');

const PORT = Number(L.arg('port', '8943'));
const INPUT = L.arg('input', '');
const NATIVE_SHA = L.arg('native-sha', '');

async function main() {
  if (!INPUT || !fs.existsSync(INPUT)) throw new Error('--input <big.kfb> required');
  const server = await L.startServer(PORT);
  process.on('exit', () => { try { server.kill('SIGKILL'); } catch { /* */ } });
  const { context, page } = await L.launch('large-page', [L.savePickerStub()]);
  const t0 = Date.now();
  try {
    await L.openTools(page, PORT);
    await page.setInputFiles('#file-input', INPUT);
    await page.waitForSelector('#disk-dialog[open]', { timeout: 60000 });
    const dialogText = (await page.textContent('#disk-dialog')).replace(/\s+/g, ' ').trim();
    await page.click('#disk-confirm-btn');
    await page.waitForSelector('#probe-section:not([hidden])', { timeout: 30 * 60000 });
    const tProbe = Date.now();
    await page.click('#convert-btn');
    await page.waitForSelector('#result-section:not([hidden])', { timeout: 60 * 60000 });
    const tConv = Date.now();
    await page.click('#save-btn');
    await page.waitForFunction(() => /已保存|Saved/.test(document.getElementById('save-status').textContent),
      null, { timeout: 60 * 60000 });
    const saved = await L.opfsSha256(page);
    const resultSha = (await page.textContent('#result-sha')).trim();
    const suggestedName = await page.evaluate(() => window.__pickerSuggested);
    const resultFormat = (await page.textContent('#result-format')).replace(/\s+/g, ' ').trim();
    const ok = resultSha === saved.sha256 && (!NATIVE_SHA || saved.sha256 === NATIVE_SHA);
    const out = {
      ok, inputBytes: fs.statSync(INPUT).size, dialogShown: true, dialogText, suggestedName, resultFormat,
      savedSha256: saved.sha256, savedBytes: saved.size, resultSha256: resultSha, nativeSha256: NATIVE_SHA || null,
      stageProbeMs: tProbe - t0, convertMs: tConv - tProbe, totalMs: Date.now() - t0,
    };
    L.writeJson('large-page/result.json', out);
    console.log(JSON.stringify(out));
    console.log(ok ? 'LARGE PAGE PASS' : 'LARGE PAGE FAIL');
    if (!ok) process.exitCode = 1;
  } finally {
    await context.close();
    server.kill('SIGTERM');
  }
}

main().catch((e) => { console.error(e); process.exit(1); });
