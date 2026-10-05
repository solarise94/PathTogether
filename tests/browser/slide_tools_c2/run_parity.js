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
const { execFileSync } = require('child_process');
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

/// F3: one MRXS bundle, browser conversion (both brightfield profiles) vs
/// the native CLI bytes of the same profile. The members are read in Node
/// and handed to the page as {name, relPath, bytes} rows (the engine treats
/// them exactly like picker files with webkitRelativePath).
async function mainMrsx() {
  if (!MRXS || !fs.existsSync(MRXS)) throw new Error('--mrxs <bundle-dir> required');
  const outDir = path.join(L.GATE, 'parity-mrxs');
  fs.mkdirSync(outDir, { recursive: true });
  const results = { runs: [], startedAt: new Date().toISOString() };

  // collect members: <dir>/<stem>.mrxs + <dir>/<stem>/*
  const stems = fs.readdirSync(MRXS).filter((f) => f.toLowerCase().endsWith('.mrxs')).sort();
  if (stems.length !== 1) throw new Error(`expected exactly one .mrxs entry in ${MRXS}`);
  const stem = stems[0].replace(/\.mrxs$/i, '');
  const members = [{ name: stems[0], relPath: `${stem}/${stems[0]}` }];
  for (const f of fs.readdirSync(path.join(MRXS, stem)).sort()) {
    members.push({ name: f, relPath: `${stem}/${stem}/${f}` });
  }
  const server = await L.startServer(PORT);
  process.on('exit', () => { try { server.kill('SIGKILL'); } catch { /* */ } });
  const { context, page } = await L.launch({ label: 'parity-mrxs' });
  try {
    await L.open(page, PORT);
    // hand the members to the page as File rows (base64 transport —
    // Playwright serializes evaluate args as JSON, so binary buffers must
    // not cross as typed arrays). Saved-1_16 ≈ 5.5 MB; the page keeps the
    // rows for both profile runs (clearJobs only wipes OPFS).
    await page.evaluate((list) => {
      const rows = [];
      for (const m of list) {
        const bin = atob(m.b64);
        const u8 = new Uint8Array(bin.length);
        for (let i = 0; i < bin.length; i++) u8[i] = bin.charCodeAt(i);
        rows.push({ name: m.name, relPath: m.relPath,
          file: new File([u8], m.name, { type: 'application/octet-stream' }) });
      }
      window.__bundleRows = rows;
    }, await Promise.all(members.map(async (m) => {
      const rel = m.name === stems[0]
        ? path.join(MRXS, m.name)
        : path.join(MRXS, stem, m.name);
      return { name: m.name, relPath: m.relPath,
        b64: (await fs.promises.readFile(rel)).toString('base64') };
    })));

    for (const profile of ['bf-ome', 'bf-classic']) {
      const tag = profile === 'bf-ome' ? 'ome' : 'classic';
      const nativeOut = path.join(outDir, `mrxs-native-${tag}.tif`);
      execFileSync(L.CLI, ['convert', path.join(MRXS, stems[0]), nativeOut,
        '--overwrite', '--profile', profile,
        ...(COMPACT ? ['--encoding', 'compact'] : [])]);
      const nativeSha = await L.sha256File(nativeOut);
      const t0 = Date.now();
      await L.clearJobs(page);
      await page.evaluate(() => window.__c2.pickBundle(window.__bundleRows));
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
        input: stems[0], bundleMembers: members.length,
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
    execFileSync(L.CLI, ['convert', input, nativeOut, '--overwrite', '--profile', profile,
      ...(COMPACT ? ['--encoding', 'compact'] : [])]);
    const nativeSha = await L.sha256File(nativeOut);
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

  // native references (cli), aliased
  {
    const n1 = path.join(outDir, COMPACT ? 'kfb1-native-compact.tif' : 'kfb1-native.tif');
    execFileSync(L.CLI, ['convert', kfb, n1, '--overwrite', '--profile', 'bf-ome',
      ...(COMPACT ? ['--encoding', 'compact'] : [])]);
    nativeOf['KFB-1'] = await L.sha256File(n1);
    results.alias['KFB-1'] = { bytes: fs.statSync(kfb).size, sha256: await L.sha256File(kfb) };
    for (const [i, f] of (WITH_FL || FL_ALL ? kfbfs : []).entries()) {
      const a = `KFBF-${'ABCD'[i]}`;
      const n2 = path.join(outDir, `kfbf-${'abcd'[i]}-native.tif`);
      execFileSync(L.CLI, ['convert', f, n2, '--overwrite']);
      nativeOf[a] = await L.sha256File(n2);
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
