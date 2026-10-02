#!/usr/bin/env node
// C3 真实样本走查：KFB-1（私有样本目录按序第一个 *.kfb；证据只用别名与哈希，
// 文件名/字节绝不入仓）。经工具页完整流程 → 保存（OPFS 替身）→
// 页内流式 sha256 == 标定值，并与本机原生 CLI 输出互验。新任务默认 bf-ome；
// classic 标定（C1/C2）另以原生 CLI 复核，证明 classic 写出未变。
//
//   node tests/browser/slide_tools_c3/run_real_sample.js --samples <样本目录>
'use strict';
const path = require('path');
const fs = require('fs');
const L = require('./lib.js');

const PORT = Number(L.arg('port', '8943'));
const SAMPLES = L.arg('samples', '/home/solarise/ZCodeProject/histopilot-suite/切片文件夹');
const ALIAS = 'KFB-1';
const EXPECTED_SHA = '374c70c8e9d778de0f5d21425077da42d11e65fdcf38f921f098dcd84a91cfde';
const CLASSIC_SHA = '385a59c6c69478c26fcac9f4232d065137864f5be47aa398f6222c6e818657fd';

async function main() {
  fs.mkdirSync(L.GATE, { recursive: true });
  // 别名 KFB-1 = 样本目录排序后的第一个 *.kfb（与 C1/C2 口径一致）
  const kfb = fs.readdirSync(SAMPLES).filter((n) => n.toLowerCase().endsWith('.kfb'))
    .sort()[0];
  if (!kfb) throw new Error('no .kfb sample found');
  const sample = path.join(SAMPLES, kfb);
  const stat = fs.statSync(sample);

  const server = await L.startServer(PORT);
  process.on('exit', () => { try { server.kill('SIGKILL'); } catch { /* */ } });
  const { context, page } = await L.launch('real-kfb1', [L.savePickerStub()]);
  try {
    await L.openTools(page, PORT);
    await page.setInputFiles('#file-input', sample);
    await page.waitForSelector('#probe-section:not([hidden])', { timeout: 120000 });
    await page.click('#convert-btn');
    await page.waitForSelector('#result-section:not([hidden])', { timeout: 300000 });
    await page.click('#save-btn');
    await page.waitForFunction(() => /已保存|Saved/.test(document.getElementById('save-status').textContent),
      null, { timeout: 120000 });
    const saved = await L.opfsSha256(page);
    const resultSha = (await page.textContent('#result-sha')).trim();

    // 原生 CLI 互验（同一文件）
    const nativeOut = path.join(L.GATE, 'fixtures', 'kfb1-native.ome.tif');
    L.nativeConvert(sample, nativeOut);
    const nativeSha = await L.sha256File(nativeOut);
    const classicOut = path.join(L.GATE, 'fixtures', 'kfb1-native-classic.tif');
    L.nativeConvert(sample, classicOut, ['--profile', 'bf-classic']);
    const classicSha = await L.sha256File(classicOut);
    const suggestedName = await page.evaluate(() => window.__pickerSuggested);

    const ok = saved.sha256 === EXPECTED_SHA && saved.sha256 === nativeSha
      && resultSha === EXPECTED_SHA && saved.size === fs.statSync(nativeOut).size
      && classicSha === CLASSIC_SHA && /\.ome\.tif$/.test(suggestedName || '');
    L.writeJson('real-sample/result.json', {
      ok, alias: ALIAS, sourceBytes: stat.size,
      savedSha256: saved.sha256, resultSha256: resultSha,
      nativeSha256: nativeSha, expectedSha256: EXPECTED_SHA,
      classicNativeSha256: classicSha, classicExpectedSha256: CLASSIC_SHA,
      savedBytes: saved.size, suggestedOmeTif: /\.ome\.tif$/.test(suggestedName || ''),
    });
    console.log(`real sample ${ALIAS}: saved=${saved.sha256} native=${nativeSha} expected=${EXPECTED_SHA} classic=${classicSha}`);
    console.log(ok ? 'REAL SAMPLE PASS' : 'REAL SAMPLE FAIL');
    if (!ok) process.exitCode = 1;
  } finally {
    await context.close();
    server.kill('SIGTERM');
  }
}

main().catch((e) => { console.error(e); process.exit(1); });
