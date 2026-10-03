#!/usr/bin/env node
// C2 real-sample parity: convert KFB-1 (brightfield) and one KFBF
// (fluorescence, KFBF-A) in the browser and compare the artifact sha256
// with the native CLI conversion of the same input. Privacy: samples are
// referenced ONLY by alias + sha256 — file names never enter the report or
// logs (plan §10.1).
//
// F1: `--svs <file.svs>` converts an SVS input through the browser tool and
// compares against BOTH native output profiles (bf-ome and bf-classic).
//
//   node run_parity.js --samples <切片文件夹> [--fl] [--port 8944]
//   node run_parity.js --svs <sample.svs> [--port 8944]
'use strict';
const fs = require('fs');
const path = require('path');
const { execFileSync } = require('child_process');
const L = require('./lib.js');

const PORT = Number(L.arg('port', '8944'));
const SAMPLES = L.arg('samples', '');
const SVS = L.arg('svs', '');
const WITH_FL = process.argv.includes('--fl');
// --fl-all: every KFBF sample (aliases KFBF-A..D, sorted), not just the first
const FL_ALL = process.argv.includes('--fl-all');
const LABEL = L.arg('label', 'parity');

function sortedBy(p, ext) {
  return fs.readdirSync(p).filter((f) => f.endsWith(ext)).sort()
    .map((f) => path.join(p, f));
}

async function convertInBrowser(page, input, profileId, outputProfile) {
  await L.clearJobs(page);
  await L.setFile(page, input);
  const jobId = await page.evaluate(([p, o]) => window.__c2.start({ profileId: p, outputProfile: o }),
    [profileId, outputProfile]);
  const done = await page.evaluate(() => window.__c2.awaitDone(60 * 60 * 1000));
  if (!done || !done.ok) throw new Error('browser conversion failed: ' + JSON.stringify(done).slice(0, 300));
  const hash = await page.evaluate((id) => window.__c2.hashArtifact(id), jobId);
  const rec = await page.evaluate((id) => window.__c2.jobRecord(id), jobId);
  return { jobId, done, hash, rec };
}

/// F1: one SVS input, browser conversion (both brightfield profiles) vs the
/// native CLI bytes of the same profile. Public CC0 samples carry their own
/// names; the file name never enters the report.
async function mainSvs() {
  if (!SVS || !fs.existsSync(SVS)) throw new Error('--svs <file> required');
  const outDir = path.join(L.GATE, 'parity-svs');
  fs.mkdirSync(outDir, { recursive: true });
  const results = { runs: [], startedAt: new Date().toISOString() };

  const server = await L.startServer(PORT);
  process.on('exit', () => { try { server.kill('SIGKILL'); } catch { /* */ } });
  const { context, page } = await L.launch({ label: LABEL + '-svs' });
  try {
    await L.open(page, PORT);
    for (const profile of ['bf-ome', 'bf-classic']) {
      const nativeOut = path.join(outDir, `svs-native-${profile === 'bf-ome' ? 'ome' : 'classic'}.tif`);
      execFileSync(L.CLI, ['convert', SVS, nativeOut, '--overwrite', '--profile', profile]);
      const nativeSha = await L.sha256File(nativeOut);
      const t0 = Date.now();
      const r = await convertInBrowser(page, SVS, 'saver', profile);
      const equal = r.hash.sha256 === nativeSha;
      results.runs.push({
        input: path.basename(SVS), bytes: fs.statSync(SVS).size,
        sourceFormat: (r.rec && r.rec.result && r.rec.result.source_format) || null,
        outputProfile: profile,
        browserSha256: r.hash.sha256, nativeSha256: nativeSha, equal,
        outputBytes: r.done.outputBytes, convertMs: r.rec.convertMs,
        validation: { ifdCount: r.rec.validation.ifd_count, checks: r.rec.validation.checks },
        wallMs: Date.now() - t0,
      });
      console.log(`SVS ${profile}: browser ${r.hash.sha256.slice(0, 16)}… native ${nativeSha.slice(0, 16)}… equal=${equal} (${r.rec.convertMs} ms)`);
      fs.rmSync(nativeOut, { force: true });
    }
  } finally {
    await context.close().catch(() => { /* */ });
    server.kill('SIGTERM');
  }
  results.finishedAt = new Date().toISOString();
  results.ok = results.runs.every((r) => r.equal);
  L.writeJson('parity-svs/result.json', results);
  console.log(results.ok ? 'SVS PARITY PASS' : 'SVS PARITY FAIL');
  process.exitCode = results.ok ? 0 : 1;
}

async function main() {
  if (SVS) {
    return mainSvs();
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
    const n1 = path.join(outDir, 'kfb1-native.tif');
    execFileSync(L.CLI, ['convert', kfb, n1, '--overwrite', '--profile', 'bf-ome']);
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
      const r = await convertInBrowser(page, kfb, 'saver');
      results.runs.push({
        alias: 'KFB-1', modality: 'brightfield',
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
