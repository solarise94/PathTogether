#!/usr/bin/env node
// C2 smoke: probe + fresh convert + finalize-validate + export under the
// mode-C CSP, compared against the native CLI for a synthetic BF fixture.
// Fast sanity before the big matrices.
//
//   node run_smoke.js [--port 8941] [--encoding compact]
// --encoding compact (U3): the browser converts with compact-jpeg-v1 and is
// compared against the NATIVE COMPACT conversion of the same input (never
// against the preserve bytes).
'use strict';
const path = require('path');
const fs = require('fs');
const { execFileSync } = require('child_process');
const L = require('./lib.js');

const PORT = Number(L.arg('port', '8941'));
const LABEL = L.arg('label', 'smoke');
const ENCODING = L.arg('encoding', 'preserve');

async function main() {
  fs.mkdirSync(L.GATE, { recursive: true });
  const fixtureDir = path.join(L.GATE, 'fixtures');
  fs.mkdirSync(fixtureDir, { recursive: true });
  const kfb = path.join(fixtureDir, 'bf-580x300.kfb');
  if (!fs.existsSync(kfb)) {
    execFileSync(L.CLI, ['gen-kfb', kfb, '--width', '580', '--height', '300'], { stdio: 'inherit' });
  }
  const nativeOut = path.join(fixtureDir, ENCODING === 'compact'
    ? 'bf-native-compact.tif' : 'bf-native.tif');
  execFileSync(L.CLI, ['convert', kfb, nativeOut, '--overwrite', '--profile', 'bf-ome',
    ...(ENCODING === 'compact' ? ['--encoding', 'compact'] : [])]);
  const nativeSha = await L.sha256File(nativeOut);

  const server = await L.startServer(PORT);
  process.on('exit', () => { try { server.kill('SIGKILL'); } catch { /* */ } });
  const { context, page } = await L.launch({ label: LABEL });
  try {
    await L.open(page, PORT);
    const info = await L.ready(page);
    console.log('core version:', info.coreVersion, 'encoding:', ENCODING);
    await page.setInputFiles('#file', kfb);
    const prep = await page.evaluate(() => window.__c2.probe());
    console.log('probe modality:', prep.probe.document.modality,
      'ub:', prep.probe.document.estimate.output_upper_bound_bytes, 'staged job:', prep.jobId);

    // convert the prepared (already staged + probed) job
    const jobId = await page.evaluate(({ id, enc }) =>
      window.__c2.start({ profileId: 'saver', preparedJobId: id,
        encoding: enc === 'compact' ? 'compact-jpeg-v1' : undefined }),
    { id: prep.jobId, enc: ENCODING });
    if (jobId !== prep.jobId) throw new Error('prepared job not reused');
    const done = await page.evaluate(() => window.__c2.awaitDone());
    console.log('done:', JSON.stringify(done).slice(0, 300));
    if (!done.ok) throw new Error('smoke convert failed');
    if (ENCODING === 'compact') {
      const rec = await page.evaluate(() => window.__c2.jobRecord());
      if (rec.result.encoding !== 'compact-jpeg-v1' || rec.result.lossy_reencode !== true) {
        throw new Error('compact result fields missing: ' + JSON.stringify(rec.result).slice(0, 200));
      }
      console.log('compact params:', JSON.stringify(rec.result.lossy_reencode_params));
    }
    const hash = await page.evaluate(() => window.__c2.hashArtifact());
    console.log('browser sha256 :', hash.sha256);
    console.log('native  sha256 :', nativeSha);
    if (hash.sha256 !== nativeSha) throw new Error('BROWSER != NATIVE');

    const exp = await page.evaluate(() => window.__c2.exportToOpfs());
    console.log('export:', JSON.stringify(exp));
    const rec = await page.evaluate(() => window.__c2.jobRecord());
    console.log('final state:', rec.state);
    L.writeJson('smoke/result.json', {
      ok: true, jobId, encoding: ENCODING, browserSha256: hash.sha256, nativeSha256: nativeSha,
      state: rec.state, validation: rec.validation, export: exp,
      coreVersion: info.coreVersion,
    });
    console.log('SMOKE PASS');
  } finally {
    await context.close();
    server.kill('SIGTERM');
  }
}

main().catch((e) => { console.error(e); process.exit(1); });
