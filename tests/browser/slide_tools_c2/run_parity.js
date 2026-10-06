#!/usr/bin/env node
// C2 real-sample parity: convert KFB-1 (brightfield) and one KFBF
// (fluorescence, KFBF-A) in the browser and compare the artifact sha256
// with the native CLI conversion of the same input. Privacy: samples are
// referenced ONLY by alias + sha256 — file names never enter the report or
// logs (plan §10.1).
//
// F1: `--svs <file.svs>` converts an SVS input through the browser tool and
// compares against BOTH native output profiles (bf-ome and bf-classic).
// --compact also applies to the SVS run (merged U3×F1): the SVS input is
// converted with compact-jpeg-v1 in the browser AND natively — parity is
// browser-compact == native-compact, never compared with preserve bytes.
//
//   node run_parity.js --samples <切片文件夹> [--fl] [--fl-all] [--compact] [--port 8944]
//   node run_parity.js --svs <sample.svs> [--compact] [--port 8944]
//   node run_parity.js --input <file.scn|file.svs|...> [--compact] [--port 8944]
//   node run_parity.js --bundle <bundle-dir> [--compact] [--port 8944]  (= --mrxs)
// --compact (U3): the brightfield sample is converted with compact-jpeg-v1
// in the browser AND natively — parity is browser-compact == native-compact
// (compact bytes are never compared with preserve bytes).
'use strict';
const fs = require('fs');
const path = require('path');
const nativeCache = require('../native_cache.js');
const L = require('./lib.js');

const PORT = Number(L.arg('port', '8944'));
const SAMPLES = L.arg('samples', '');
const SVS = L.arg('svs', '');
// F3: `--mrxs <dir>` / `--bundle <dir>` (alias) — the unpacked bundle
// directory (entry + same-name folder). Converts through the browser bundle
// path and compares with the native CLI for BOTH brightfield profiles.
const MRXS = L.arg('mrxs', '') || L.arg('bundle', '');
// F4: `--input <file>` — ANY single-file converter input (SVS / SCN / KFB);
// the browser artifact must be byte-identical with the native CLI for both
// brightfield profiles (preserve + compact once each via --compact).
const INPUT = L.arg('input', '');
const WITH_FL = process.argv.includes('--fl');
// --fl-all: every KFBF sample (aliases KFBF-A..D, sorted), not just the first
const FL_ALL = process.argv.includes('--fl-all');
// --compact: brightfield converts at compact-jpeg-v1 (U3)
const COMPACT = process.argv.includes('--compact');
const LABEL = L.arg('label', COMPACT ? 'parity-compact' : 'parity');

function sortedBy(p, ext) {
  return fs.readdirSync(p).filter((f) => f.endsWith(ext)).sort()
    .map((f) => path.join(p, f));
}

async function convertInBrowser(page, input, profileId, outputProfile, encoding) {
  await L.clearJobs(page);
  await L.setFile(page, input);
  const jobId = await page.evaluate(
    ({ p, o, e }) => window.__c2.start({ profileId: p, outputProfile: o, encoding: e }),
    { p: profileId, o: outputProfile, e: encoding },
  );
  const done = await page.evaluate(() => window.__c2.awaitDone(60 * 60 * 1000));
  if (!done || !done.ok) throw new Error('browser conversion failed: ' + JSON.stringify(done).slice(0, 300));
  const hash = await page.evaluate((id) => window.__c2.hashArtifact(id), jobId);
  const rec = await page.evaluate((id) => window.__c2.jobRecord(id), jobId);
  return { jobId, done, hash, rec };
}

/// 原生参考（走共享缓存 tests/browser/native_cache.js）：同一
/// (CLI, 输入, 参数) 的原生转换跨场景/跨轮次只真正跑一次，命中以
/// hardlink 交付并直接复用存储时记下的 sha256。
function nativeRef(input, output, args) {
  return nativeCache.nativeConvertCached({ cli: L.CLI, input, output, args }).sha256;
}

/// F3/F7: one bundle (MRXS same-name dir, or a FLAT VMS folder: the .vms
/// entry plus its sibling tile JPEGs), browser conversion (both brightfield
/// profiles) vs the native CLI bytes of the same profile. Members enter the
/// page through the REAL folder-selection input (harness #dir
/// webkitdirectory → File.webkitRelativePath — the engine treats them
/// exactly like a user-picked folder).
async function mainMrsx() {
  if (!MRXS || !fs.existsSync(MRXS)) throw new Error('--mrxs <bundle-dir> required');
  const outDir = path.join(L.GATE, 'parity-mrxs');
  fs.mkdirSync(outDir, { recursive: true });
  const results = { runs: [], startedAt: new Date().toISOString() };

  // collect members by entry kind: MRXS = <dir>/<stem>.mrxs + <dir>/<stem>/*
  // (relPath <stem>/<stem>.mrxs …); VMS = <dir>/<stem>.vms + flat siblings
  // (relPath = the bare file name — members are named relative to the
  // entry's directory)
  const mrxsStems = fs.readdirSync(MRXS).filter((f) => f.toLowerCase().endsWith('.mrxs')).sort();
  const vmsStems = fs.readdirSync(MRXS).filter((f) => f.toLowerCase().endsWith('.vms')).sort();
  let members;
  let entryFile;
  let kind;
  if (vmsStems.length === 1 && mrxsStems.length === 0) {
    kind = 'vms';
    entryFile = vmsStems[0];
    members = fs.readdirSync(MRXS).filter((f) => !f.startsWith('.')).sort()
      .map((f) => ({ name: f, relPath: f, p: path.join(MRXS, f) }));
  } else if (mrxsStems.length === 1 && vmsStems.length === 0) {
    kind = 'mrxs';
    entryFile = mrxsStems[0];
    const stem = mrxsStems[0].replace(/\.mrxs$/i, '');
    members = [{ name: mrxsStems[0], relPath: `${stem}/${mrxsStems[0]}`, p: path.join(MRXS, mrxsStems[0]) }];
    for (const f of fs.readdirSync(path.join(MRXS, stem)).sort()) {
      members.push({ name: f, relPath: `${stem}/${stem}/${f}`, p: path.join(MRXS, stem, f) });
    }
  } else {
    throw new Error(`expected exactly one .mrxs OR one .vms entry in ${MRXS}`);
  }
  const server = await L.startServer(PORT);
  process.on('exit', () => { try { server.kill('SIGKILL'); } catch { /* */ } });
  const { context, page } = await L.launch({ label: 'parity-mrxs' });
  try {
    await L.open(page, PORT);
    // 大包成员经真实「选择文件夹」入口（#dir webkitdirectory input）进入
    // 页面：Playwright setInputFiles 走 DOM.setFileInputFiles 只传路径，
    // 浏览器直接从磁盘读文件——替代此前的 8 MiB base64 切片 CDP evaluate
    // 灌输（真实 VMS 617 MB → ~800 MB CDP 载荷，慢且压渲染进程；gate
    // adapter-20261006-072429 曾因巨型单次 evaluate 炸过 renderer）。
    // 两个 profile 轮共用同一 staged 输入（clearJobs 只清 OPFS）；重选前
    // 清空 value，避免 Chromium 对同一路径跳过 change。
    const pickFolder = async () => {
      await page.evaluate(() => { const el = document.getElementById('dir'); el.value = ''; });
      await page.setInputFiles('#dir', MRXS);
      await page.waitForFunction((n) => document.getElementById('dir').files.length === n,
        members.length, { timeout: 120000 });
    };
    await pickFolder();

    for (const profile of ['bf-ome', 'bf-classic']) {
      const tag = profile === 'bf-ome' ? 'ome' : 'classic';
      const nativeOut = path.join(outDir, `mrxs-native-${tag}.tif`);
      const nativeSha = nativeRef(path.join(MRXS, entryFile), nativeOut,
        ['--profile', profile, ...(COMPACT ? ['--encoding', 'compact'] : [])]);
      // F3 适配器 v2 锚点（自 C3 run_e2e 场景 mx 移入——真实 MRXS 的
      // 浏览器转换已去重到本 parity，锚点跟着原生参考走，零额外成本）：
      // preserve/bf-ome 的原生输出必须仍是 review §4 l0-box2 金字塔的
      // 62da50da…（缩减层像素全部改变；v1 的 42f3c650… 已作废）。
      // The anchor belongs to the CMU-1-Saved-1_16 MRXS sample only; --bundle
      // also carries VMS and other bundles through this function.
      if (!COMPACT && profile === 'bf-ome' && entryFile === 'CMU-1-Saved-1_16.mrxs'
          && !nativeSha.startsWith('62da50da')) {
        throw new Error(`native preserve sha ${nativeSha} != expected 62da50da…`);
      }
      const t0 = Date.now();
      await L.clearJobs(page);
      await pickFolder();
      await page.evaluate(async ({ p, e }) => {
        const prep = await window.__c2.probeBundle({ encoding: e, outputProfile: p });
        await window.__c2.start({ outputProfile: p, encoding: e, preparedJobId: prep.jobId });
      }, { p: profile, e: COMPACT ? 'compact-jpeg-v1' : undefined });
      const done = await page.evaluate(() => window.__c2.awaitDone(60 * 60 * 1000));
      if (!done || !done.ok) throw new Error('browser bundle conversion failed: ' + JSON.stringify(done).slice(0, 300));
      const jobId = await page.evaluate(() => window.__c2.jobId());
      const hash = await page.evaluate((id) => window.__c2.hashArtifact(id), jobId);
      const rec = await page.evaluate((id) => window.__c2.jobRecord(id), jobId);
      const equal = hash.sha256 === nativeSha;
      results.runs.push({
        input: entryFile, bundleKind: kind, bundleMembers: members.length,
        sourceFormat: rec.result && rec.result.source_format,
        outputProfile: profile,
        encoding: COMPACT ? 'compact-jpeg-v1' : 'preserve-source-v1',
        browserSha256: hash.sha256, nativeSha256: nativeSha, equal,
        outputBytes: done.result && done.result.output_bytes,
        convertMs: rec.convertMs,
        composed: rec.result && rec.result.composed,
        validation: rec.validation ? { ifdCount: rec.validation.ifd_count } : null,
        wallMs: Date.now() - t0,
      });
      console.log(`MRXS ${profile}${COMPACT ? ' compact' : ''}: browser ${hash.sha256.slice(0, 16)}… native ${nativeSha.slice(0, 16)}… equal=${equal} (${rec.convertMs} ms)`);
      fs.rmSync(nativeOut, { force: true });
    }
  } finally {
    await context.close().catch(() => { /* */ });
    server.kill('SIGKILL');
  }
  results.finishedAt = new Date().toISOString();
  results.ok = results.runs.every((r) => r.equal);
  L.writeJson('parity-mrxs/result.json', results);
  console.log(results.ok ? 'MRXS PARITY PASS' : 'MRXS PARITY FAIL');
  process.exitCode = results.ok ? 0 : 1;
}

/// F1/F4: one single-file input (SVS/SCN/KFB), browser conversion (both
/// brightfield profiles) vs the native CLI bytes of the same profile. Public
/// CC0 samples carry their own names; the file name never enters the report.
async function mainInput() {
  const input = INPUT || SVS;
  if (!input || !fs.existsSync(input)) throw new Error('--input <file> (or --svs) required');
  const ext = (path.extname(input) || '.bin').toLowerCase().slice(1);
  const outDir = path.join(L.GATE, `parity-${ext}`);
  fs.mkdirSync(outDir, { recursive: true });
  const results = { runs: [], startedAt: new Date().toISOString() };

  const server = await L.startServer(PORT);
  process.on('exit', () => { try { server.kill('SIGKILL'); } catch { /* */ } });
  const { context, page } = await L.launch({ label: LABEL + '-svs' });
  try {
    await L.open(page, PORT);
    for (const profile of ['bf-ome', 'bf-classic']) {
    const tag = `${profile === 'bf-ome' ? 'ome' : 'classic'}${COMPACT ? '-compact' : ''}`;
    const nativeOut = path.join(outDir, `${ext}-native-${tag}.tif`);
    const nativeSha = nativeRef(input, nativeOut,
      ['--profile', profile, ...(COMPACT ? ['--encoding', 'compact'] : [])]);
    const t0 = Date.now();
    const r = await convertInBrowser(page, input, 'saver', profile,
      COMPACT ? 'compact-jpeg-v1' : undefined);
    const equal = r.hash.sha256 === nativeSha;
    results.runs.push({
      input: path.basename(input), bytes: fs.statSync(input).size,
      sourceFormat: (r.rec && r.rec.result && r.rec.result.source_format) || null,
      outputProfile: profile,
      encoding: COMPACT ? 'compact-jpeg-v1' : 'preserve-source-v1',
      browserSha256: r.hash.sha256, nativeSha256: nativeSha, equal,
      outputBytes: r.done.outputBytes, convertMs: r.rec.convertMs,
      validation: { ifdCount: r.rec.validation.ifd_count, checks: r.rec.validation.checks },
      wallMs: Date.now() - t0,
    });
    console.log(`${ext.toUpperCase()} ${profile}${COMPACT ? ' compact' : ''}: browser ${r.hash.sha256.slice(0, 16)}… native ${nativeSha.slice(0, 16)}… equal=${equal} (${r.rec.convertMs} ms)`);
    fs.rmSync(nativeOut, { force: true });
    }
  } finally {
    await context.close().catch(() => { /* */ });
    server.kill('SIGTERM');
  }
  results.finishedAt = new Date().toISOString();
  results.ok = results.runs.every((r) => r.equal);
  L.writeJson(`parity-${ext}/result.json`, results);
  console.log(results.ok ? `${ext.toUpperCase()} PARITY PASS` : `${ext.toUpperCase()} PARITY FAIL`);
  process.exitCode = results.ok ? 0 : 1;
}

async function main() {
  if (MRXS) {
    return mainMrsx();
  }
  if (INPUT || SVS) {
    return mainInput();
  }
  if (!SAMPLES || !fs.existsSync(SAMPLES)) {
    throw new Error('--samples <dir> required (private sample folder)');
  }
  const kfb = sortedBy(SAMPLES, '.kfb')[0];
  if (!kfb) throw new Error('no .kfb sample');
  const kfbfs = sortedBy(path.join(SAMPLES, 'ref'), '.kfbf').slice(0, FL_ALL ? 4 : 1);
  const kfbf = kfbfs[0];
  const outDir = path.join(L.GATE, 'parity');
  fs.mkdirSync(outDir, { recursive: true });

  const results = { alias: {}, runs: [], startedAt: new Date().toISOString() };
  const nativeOf = {};

  // native references (cli, aliased; 走共享原生参考缓存)
  {
    const n1 = path.join(outDir, COMPACT ? 'kfb1-native-compact.tif' : 'kfb1-native.tif');
    nativeOf['KFB-1'] = nativeRef(kfb, n1,
      ['--profile', 'bf-ome', ...(COMPACT ? ['--encoding', 'compact'] : [])]);
    results.alias['KFB-1'] = { bytes: fs.statSync(kfb).size, sha256: await L.sha256File(kfb) };
    for (const [i, f] of (WITH_FL || FL_ALL ? kfbfs : []).entries()) {
      const a = `KFBF-${'ABCD'[i]}`;
      const n2 = path.join(outDir, `kfbf-${'abcd'[i]}-native.tif`);
      nativeOf[a] = nativeRef(f, n2, []);
      results.alias[a] = { bytes: fs.statSync(f).size, sha256: await L.sha256File(f) };
      fs.rmSync(n2, { force: true });
    }
  }

  const server = await L.startServer(PORT);
  process.on('exit', () => { try { server.kill('SIGKILL'); } catch { /* */ } });
  const { context, page } = await L.launch({ label: LABEL });
  try {
    await L.open(page, PORT);

    // ---- KFB-1 brightfield ----
    {
      const t0 = Date.now();
      const r = await convertInBrowser(page, kfb, 'saver', undefined,
        COMPACT ? 'compact-jpeg-v1' : undefined);
      results.runs.push({
        alias: 'KFB-1', modality: 'brightfield', encoding: COMPACT ? 'compact-jpeg-v1' : 'preserve-source-v1',
        browserSha256: r.hash.sha256, nativeSha256: nativeOf['KFB-1'],
        equal: r.hash.sha256 === nativeOf['KFB-1'],
        outputBytes: r.done.outputBytes, convertMs: r.rec.convertMs,
        validation: { ifdCount: r.rec.validation.ifd_count, checks: r.rec.validation.checks },
        wallMs: Date.now() - t0,
      });
      console.log(`KFB-1: browser ${r.hash.sha256.slice(0, 16)}… native ${nativeOf['KFB-1'].slice(0, 16)}… equal=${r.hash.sha256 === nativeOf['KFB-1']}`);
    }
    // ---- export round-trip (OPFS artifact → OPFS copy → hash) ----
    {
      const exp = await page.evaluate(() => window.__c2.exportToOpfs('parity-export.bin'));
      const root = await page.evaluate(async () => {
        const root = await navigator.storage.getDirectory();
        const fh = await root.getFileHandle('parity-export.bin');
        return (await fh.getFile()).size;
      });
      results.export = { ...exp, artifactSize: root };
      console.log('export round-trip bytes:', exp.exportedBytes);
    }
    // ---- KFBF fluorescence ----
    for (const [i, f] of (WITH_FL || FL_ALL ? kfbfs : []).entries()) {
      const a = `KFBF-${'ABCD'[i]}`;
      await L.clearJobs(page);
      const t0 = Date.now();
      const r = await convertInBrowser(page, f, 'saver');
      results.runs.push({
        alias: a, modality: 'fluorescence',
        browserSha256: r.hash.sha256, nativeSha256: nativeOf[a],
        equal: r.hash.sha256 === nativeOf[a],
        outputBytes: r.done.outputBytes, convertMs: r.rec.convertMs,
        validation: { ifdCount: r.rec.validation.ifd_count, checks: r.rec.validation.checks },
        wallMs: Date.now() - t0,
      });
      console.log(`${a}: browser ${r.hash.sha256.slice(0, 16)}… native ${nativeOf[a].slice(0, 16)}… equal=${r.hash.sha256 === nativeOf[a]} convert ${r.rec.convertMs} ms wall ${Date.now() - t0} ms`);
    }
  } finally {
    await context.close().catch(() => { /* */ });
    server.kill('SIGTERM');
  }
  results.finishedAt = new Date().toISOString();
  const ok = results.runs.every((r) => r.equal);
  results.ok = ok;
  L.writeJson('parity/result.json', results);
  console.log(ok ? 'PARITY PASS' : 'PARITY FAIL');
  process.exitCode = ok ? 0 : 1;
}

main().catch((e) => { console.error(e); process.exit(1); });
