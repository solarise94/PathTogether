#!/usr/bin/env node
// F2 real-browser end-to-end on the /tools/slides page (Chromium via
// Playwright): select the public CC0 Aperio JPEG 2000 sample, verify the
// page-level JPEG 2000 vocabulary (re-encode notice + variant scope),
// convert through the real worker, save to the OPFS stub, and verify the
// saved artifact against the native CLI byte-for-byte.
//
//   node tests/browser/slide_tools_c3/run_jp2k_e2e.js \
//     --input <CMU-1-JP2K-33005.svs> [--port 8945]
'use strict';
const path = require('path');
const fs = require('fs');
const L = require('./lib.js');

const PORT = Number(L.arg('port', '8945'));
const INPUT = L.arg('input', '/home/solarise/ZCodeProject/histopilot-suite/.testdata/openslide/CMU-1-JP2K-33005.svs');
const EXPECTED_OME_SHA = '4a29c2c92304c95dc0ed6f7ca861c1a2642e0b98dd45d6fb0aa99e9935671824';

async function main() {
  fs.mkdirSync(L.GATE, { recursive: true });
  const server = await L.startServer(PORT);
  process.on('exit', () => { try { server.kill('SIGKILL'); } catch { /* */ } });
  const { context, page } = await L.launch('jp2k-e2e', [L.savePickerStub(), L.downloadGuard()]);
  const results = { input: path.basename(INPUT), checks: {} };
  try {
    await L.openTools(page, PORT);
    await page.setInputFiles('#file-input', INPUT);
    await page.waitForSelector('#summary-section:not([hidden])', { timeout: 300000 });
    // JP2K vocabulary: the re-encode notice is visible with the agreed wording
    await page.waitForFunction(() => {
      const el = document.getElementById('quality-jp2k-note');
      return el && !el.hidden && el.textContent.length > 0;
    }, null, { timeout: 30000 });
    results.checks.jp2kNotice = await page.evaluate(() => ({
      visible: !document.getElementById('quality-jp2k-note').hidden,
      text: document.getElementById('quality-jp2k-note').textContent.slice(0, 80),
    }));
    if (!results.checks.jp2kNotice.text.includes('JPEG2000 源，输出需要重新编码')) {
      throw new Error('JP2K notice wording wrong: ' + JSON.stringify(results.checks.jp2kNotice));
    }
    // F2 review: the OPTION DESCRIPTIONS themselves must switch to the
    // re-encode semantics for JP2K sources (no as-is copying in either mode)
    results.checks.qualityOptionDesc = await page.evaluate(() => ({
      preserve: document.getElementById('quality-preserve-desc').textContent,
      compact: document.getElementById('quality-compact-desc').textContent,
    }));
    if (!/质量 96|quality 96/.test(results.checks.qualityOptionDesc.preserve)
        || !results.checks.qualityOptionDesc.preserve.includes('4:2:2')) {
      throw new Error('preserve option desc not JP2K: '
        + results.checks.qualityOptionDesc.preserve);
    }
    if (!/质量 80|quality 80/.test(results.checks.qualityOptionDesc.compact)
        || !results.checks.qualityOptionDesc.compact.includes('4:2:0')) {
      throw new Error('compact option desc not JP2K: '
        + results.checks.qualityOptionDesc.compact);
    }
    // probe document identifies the codec (technical summary row)
    const sourceCodecRow = await page.evaluate(() => {
      const g = document.getElementById('probe-grid');
      return g ? g.textContent : '';
    });
    results.checks.probeRowHasJp2k = sourceCodecRow.includes('JPEG 2000');
    await L.shot(page, 'jp2k-01-probe');
    // close-up of the quality options with the JP2K descriptions
    await page.locator('#quality-fieldset').screenshot({
      path: path.join(L.SCREENS, 'jp2k-01b-quality-options.png'),
    });
    // locale note: assertions follow the pinned zh-CN test locale

    // strict-lossless: selecting the policy must show the JP2K-specific
    // refusal warning BEFORE conversion (and the conversion would refuse)
    await L.openMoreOptions(page);
    await page.check('#policy-strict');
    await page.waitForFunction(() => {
      const el = document.getElementById('policy-strict-warn');
      return el && !el.hidden && el.textContent.includes('JPEG2000');
    }, null, { timeout: 10000 });
    results.checks.strictWarn = await page.evaluate(
      () => document.getElementById('policy-strict-warn').textContent.slice(0, 60));
    await L.shot(page, 'jp2k-02-strict-warn');
    await page.check('#policy-allow-edge'); // back to default

    // convert (default bf-ome) through the real worker
    await page.click('#convert-btn');
    await page.waitForSelector('#result-section:not([hidden])', { timeout: 1800000 });
    const resultSha = (await page.textContent('#result-sha')).trim();
    results.checks.resultSha = resultSha;
    await L.shot(page, 'jp2k-03-result');

    // save via the picker stub and hash the OPFS artifact
    await page.click('#save-btn');
    await page.waitForFunction(
      () => /已保存|Saved/.test(document.getElementById('save-status').textContent),
      null, { timeout: 120000 });
    const saved = await L.opfsSha256(page);
    results.checks.saved = saved;
    results.checks.shaMatch = saved.sha256 === EXPECTED_OME_SHA && resultSha === EXPECTED_OME_SHA;
    await L.shot(page, 'jp2k-04-saved');
    results.ok = results.checks.shaMatch;
    L.writeJson('jp2k-e2e/result.json', results);
    console.log('e2e:', JSON.stringify(results.checks, null, 1));
    console.log(results.ok ? 'JP2K E2E PASS' : 'JP2K E2E FAIL');
    if (!results.ok) process.exitCode = 1;
  } finally {
    await context.close();
    server.kill('SIGTERM');
  }
}

main().catch((e) => { console.error(e); process.exit(1); });
