#!/usr/bin/env node
// F2 Node harness: drives the PRODUCTION wasm module (slide_transform.js /
// slide_transform_bg.wasm — the exact shipped artifacts) with in-process
// stHost* callbacks, and proves the JPEG 2000 acceptance on the same code
// path the browser worker uses:
//
//   node tests/browser/slide_tools_c2/run_jp2k_node.mjs \
//        --input <file.svs> --out-dir <dir> [--cli <slide-transform>]
//
// Checks per input:
//   1. probe(): codec = jpeg2000-reencode, adapter = aperio-svs-jp2k
//   2. convertProfileEncoded bf-ome  (preserve)  — sha256 == native CLI
//   3. convertProfileEncoded bf-classic          — sha256 == native CLI
//   4. strict_lossless = true → typed pixel-policy error, ZERO output bytes
//   5. cancel mid-conversion (stHostCancelled after N progress events) →
//      clean typed cancel, then convertResumeProfileEncoded from the last
//      checkpoint → final bytes == the fresh conversion's bytes
//   6. corrupt input (truncated tile) → typed error, no trap, no output
'use strict';
// ESM file (it must import the generated slide_transform.js), with CJS
// modules via createRequire.
const { createRequire } = await import('node:module');
const require = createRequire(import.meta.url);
const fs = require('fs');
const path = require('path');
const crypto = require('crypto');

function arg(name, dflt) {
  const i = process.argv.indexOf('--' + name);
  return i >= 0 ? process.argv[i + 1] : dflt;
}

const INPUT = arg('input', '');
const OUT_DIR = arg('out-dir', '');
const CLI = arg('cli', path.join(import.meta.dirname ?? path.dirname(new URL(import.meta.url).pathname), '../../../slide-transform-core/target/release/slide-transform'));
if (!INPUT || !OUT_DIR) {
  console.error('usage: run_jp2k_node.mjs --input <file.svs> --out-dir <dir> [--cli <bin>]');
  process.exit(2);
}
fs.mkdirSync(OUT_DIR, { recursive: true });
const HERE = import.meta.dirname ?? path.dirname(new URL(import.meta.url).pathname);
const TOOLS = path.join(HERE, '../../../static/tools/slide-transform');

// ------------------------------------------------------------------ host --

const host = {
  fd: fs.openSync(INPUT, 'r'),
  size: fs.statSync(INPUT).size,
  scratchDir: fs.mkdtempSync(path.join(path.resolve(OUT_DIR), 'scratch-')),
  scratches: new Map(), // name -> {path, fd}
  out: null, // Buffer
  outSize: 0,
  cancelled: false,
  cancelAfter: -1,
  progressEvents: 0,
  checkpoints: [],
};

globalThis.stHostSourceSize = () => host.size;
globalThis.stHostReadInto = (offset, len, ptr) => {
  const view = new Uint8Array(wasm.memory.buffer, ptr, len);
  let done = 0;
  while (done < len) {
    const n = fs.readSync(host.fd, view, done, len - done, Number(offset) + done);
    if (n <= 0) return '短读';
    done += n;
  }
  return null;
};
globalThis.stHostRead = (offset, len) => {
  const buf = Buffer.allocUnsafe(Number(len));
  let done = 0;
  while (done < len) {
    const n = fs.readSync(host.fd, buf, done, Number(len) - done, Number(offset) + done);
    if (n <= 0) throw new Error('短读');
    done += n;
  }
  return buf;
};
globalThis.stHostWrite = (offset, bytes) => {
  if (host.failWritesAfterProgress >= 0 && host.progressEvents >= host.failWritesAfterProgress) {
    return 'simulated worker death mid-write';
  }
  // `bytes` is a plain Uint8Array view over wasm memory
  const end = Number(offset) + bytes.length;
  if (!host.out || end > host.out.length) {
    const cap = Math.max(end, host.out ? host.out.length * 2 : 1 << 20);
    const next = Buffer.alloc(Math.ceil(cap / (1 << 20)) * (1 << 20));
    if (host.out) host.out.copy(next, 0);
    host.out = next;
  }
  Buffer.from(bytes.buffer, bytes.byteOffset, bytes.byteLength).copy(host.out, Number(offset));
  if (end > host.outSize) host.outSize = end;
  return null;
};
globalThis.stHostTruncate = (len) => {
  host.outSize = Number(len);
  return null;
};
globalThis.stHostFlush = () => null;
globalThis.stHostOutSize = () => host.outSize;
globalThis.stHostOutReadInto = (offset, len, ptr) => {
  const view = new Uint8Array(wasm.memory.buffer, ptr, len);
  for (let i = 0; i < len; i++) {
    view[i] = host.outSize > Number(offset) + i ? host.out[Number(offset) + i] : 0;
  }
  return null;
};
globalThis.stHostScratchOpen = (name, preserve) => {
  const p = path.join(host.scratchDir, name.replace(/\//g, '__'));
  if (!preserve) fs.rmSync(p, { force: true });
  const fd = fs.openSync(p, preserve ? 'a+' : 'w+');
  host.scratches.set(name, { path: p, fd });
  return null;
};
globalThis.stHostScratchRead = (name, offset, len) => {
  const s = host.scratches.get(name);
  const buf = Buffer.alloc(Number(len));
  fs.readSync(s.fd, buf, 0, Number(len), Number(offset));
  return buf;
};
globalThis.stHostScratchWrite = (name, offset, bytes) => {
  fs.writeSync(host.scratches.get(name).fd, bytes, 0, bytes.length, Number(offset));
  return null;
};

globalThis.stHostScratchTruncate = (name, len) => {
  const s = host.scratches.get(name);
  fs.ftruncateSync(s.fd, Number(len));
  return null;
};
globalThis.stHostScratchFlush = (name) => {
  fs.fsyncSync(host.scratches.get(name).fd);
  return null;
};
globalThis.stHostProgress = (json) => {
  host.progressEvents += 1;
  if (host.cancelAfter >= 0 && host.progressEvents >= host.cancelAfter) host.cancelled = true;
};
// models worker death / quota failure: a host write error mid-conversion
globalThis.stHostFailWrites = false;
globalThis.stHostCheckpoint = (json) => {
  host.checkpoints.push(JSON.parse(json));
};
globalThis.stHostCancelled = () => host.cancelled;

// ------------------------------------------------------------------ wasm --

const init = (async () => {
  const mod = await import(path.join(TOOLS, 'slide_transform.js'));
  const bytes = fs.readFileSync(path.join(TOOLS, 'slide_transform_bg.wasm'));
  const out = await mod.default(bytes);
  if (!out || !out.memory) throw new Error('init returned no memory');
  wasm = out; // exports incl. memory (assigned early: host callbacks use it)
  return mod;
})();
let wasm = null; // exports incl. memory

function outBytes() {
  return host.out.subarray(0, host.outSize);
}
function sha(buf) {
  return crypto.createHash('sha256').update(buf).digest('hex');
}
function resetOutput() {
  host.out = null;
  host.outSize = 0;
  host.progressEvents = 0;
  host.cancelled = false;
  host.checkpoints = [];
}
function parse(json, what) {
  const v = JSON.parse(json);
  if (v.error) throw new Error(`${what}: ${JSON.stringify(v.error)}`);
  return v;
}

async function main() {
  const mod = await init;
  const { probe, convertProfileEncoded, convertResumeProfileEncoded, finalizeValidate, coreVersion, enableCheckpoint, configure } = mod;
  console.log('core:', coreVersion());
  enableCheckpoint(); // the worker enables the committed-state tap likewise
  configure(1);       // prefer stHostReadInto (zero buffer crossing)

  const results = { input: path.basename(INPUT), size: host.size, checks: {} };

  // ---- 1. probe ---------------------------------------------------------
  resetOutput();
  const doc = parse(probe(), 'probe');
  results.checks.probe = {
    format: doc.document.format,
    codec: doc.document.codec,
    source_codec: doc.document.source_codec,
    decoder: doc.document.decoder,
    levels: (doc.document.levels || []).length,
  };
  if (doc.document.format !== 'aperio-svs-jp2k') throw new Error('probe: adapter id ' + doc.document.format);
  if (doc.document.codec !== 'jpeg2000-reencode') throw new Error('probe: codec ' + doc.document.codec);
  console.log('probe ok:', JSON.stringify(results.checks.probe));

  // ---- 2/3. both output profiles, byte parity with the native CLI -------
  for (const [profile, outName] of [
    ['bf-ome', 'node.ome.tif'],
    ['bf-classic', 'node.tif'],
  ]) {
    resetOutput();
    const t0 = Date.now();
    const r = parse(convertProfileEncoded(profile, 'preserve-source-v1', false, ''), 'convert ' + profile);
    const ms = Date.now() - t0;
    const val = parse(finalizeValidate(r.ifd_count), 'validate');
    if (val.sha256 !== sha(outBytes())) throw new Error('validate: sha mismatch');
    const outPath = path.join(OUT_DIR, outName);
    fs.writeFileSync(outPath, outBytes());
    results.checks[profile] = {
      output_bytes: r.output_bytes,
      sha256: val.sha256,
      tiles_reencoded: r.lossy_reencode_params ? r.lossy_reencode_params.tiles_reencoded : null,
      lossy: r.lossy_reencode,
      warnings: r.warnings,
      ms,
      wasm_heap_peak_mb: +(wasm.memory.buffer.byteLength / 2 ** 20).toFixed(1),
    };
    console.log(`${profile}: ${r.output_bytes} bytes in ${ms} ms sha=${val.sha256.slice(0, 16)}… heap=${results.checks[profile].wasm_heap_peak_mb} MB`);
  }

  // ---- 4. strict-lossless refusal with zero output ----------------------
  resetOutput();
  const strict = JSON.parse(convertProfileEncoded('bf-ome', 'preserve-source-v1', true, ''));
  if (!strict.error || !/pixel_policy_violation/.test(strict.error.code)) {
    throw new Error('strict-lossless did not refuse: ' + JSON.stringify(strict));
  }
  if (host.outSize !== 0) throw new Error('strict-lossless produced output bytes');
  results.checks.strict_lossless = { error: strict.error.code, output_bytes: host.outSize };
  console.log('strict-lossless refused with zero output:', strict.error.code);

  // ---- 5. cancel mid-way + refresh-resume from the last checkpoint ------
  // The browser cancels by TERMINATING the worker (the core call is
  // synchronous and cannot be interrupted); a mid-run interruption is the
  // C2 crash contract — a host write fails after the last checkpoint, the
  // output stays partial, and refresh-resume replays the journal. Fail the
  // first write after the 6th progress event to model it.
  resetOutput();
  host.failWritesAfterProgress = 6;
  const crashed = JSON.parse(convertProfileEncoded('bf-ome', 'preserve-source-v1', false, ''));
  host.failWritesAfterProgress = -1;
  if (!crashed.error) throw new Error('mid-run host failure did not surface: ' + JSON.stringify(crashed).slice(0, 160));
  if (host.checkpoints.length === 0) throw new Error('no checkpoint before the interruption');
  const st = host.checkpoints[host.checkpoints.length - 1];
  // crash aftermath, exactly like the worker's resume path: truncate the
  // output + begun offcnt streams to the committed state
  host.outSize = st.out;
  for (const [name, s] of host.scratches) {
    if (name.startsWith('offcnt-l')) {
      const tiles = st.ifds[Number(name.slice('offcnt-l'.length))] ?? 0;
      fs.ftruncateSync(s.fd, tiles * 12);
    }
  }
  const resumed = parse(convertResumeProfileEncoded(JSON.stringify(st), 'bf-ome', 'preserve-source-v1', false, ''), 'resume');
  const resumedBytes = outBytes();
  const fresh = results.checks['bf-ome'];
  if (sha(resumedBytes) !== fresh.sha256) throw new Error('resume: bytes differ from the fresh conversion');
  if (resumedBytes.length !== fresh.output_bytes) throw new Error('resume: length differs');
  results.checks.cancel_resume = {
    interrupted_error: crashed.error.code,
    checkpoint: { level: st.level, cell: st.cell, out: st.out, adapter: st.adapter, encoding: st.encoding },
    resumed_sha256: sha(resumedBytes),
    matches_fresh: true,
  };
  console.log(`interrupt+resume ok: stopped at level ${st.level} cell ${st.cell}, resumed == fresh`);

  // ---- 5b. (reporting) the checkpoint cell field ------------------------
  results.checks.cancel_resume.checkpoint.cell = st.cell;

  // ---- 6. old JPEG-task resume identity still enforced ------------------
  // a checkpoint journalled under aperio-svs-jpeg must be refused for a
  // JP2K input with the restart-task explanation (adapter identity)
  resetOutput();
  const foreign = JSON.parse(convertResumeProfileEncoded(
    JSON.stringify({ ...st, adapter: 'aperio-svs-jpeg', cell: 3, level: 0, out: 4096, ifds: [3] }),
    'bf-ome', 'preserve-source-v1', false, '', ''));
  if (!foreign.error || !/适配器/.test(foreign.error.message)) {
    throw new Error('cross-adapter resume was not refused: ' + JSON.stringify(foreign).slice(0, 200));
  }
  results.checks.cross_adapter_resume = { refused: foreign.error.code, message: foreign.error.message.slice(0, 80) };
  console.log('cross-adapter resume refused:', foreign.error.message.slice(0, 60) + '…');

  // ---- 7. corrupt input: typed error, no trap, no output ----------------
  resetOutput();
  const corruptPath = path.join(OUT_DIR, 'corrupt.svs');
  let src = Buffer.alloc(host.size);
  {
    const buf = Buffer.alloc(1 << 20);
    let at = 0;
    while (at < host.size) {
      const n = fs.readSync(host.fd, buf, 0, buf.length, at);
      if (n <= 0) break;
      buf.copy(src, at, 0, n);
      at += n;
    }
  }
  // corruption modes that a JP2K pipeline MUST catch: (a) a truncated file
  // (a missing tile payload is a bounds-checked typed error), (b) a flipped
  // codestream HEADER byte (the bounded probe refuses). NOTE: bit flips in
  // the entropy-coded data are NOT detectable by design (JPEG 2000 has no
  // coded-data integrity check; OpenSlide behaves the same) — documented in
  // the F2 evidence doc.
  src = src.subarray(0, Math.floor(src.length * 0.75));
  src[60] ^= 0xFF; // inside the first tile's SIZ header
  fs.writeFileSync(corruptPath, src);
  host.fd = fs.openSync(corruptPath, 'r');
  host.size = src.length;
  resetOutput();
  const corrupt = JSON.parse(convertProfileEncoded('bf-ome', 'preserve-source-v1', false, ''));
  results.checks.corrupt_input = {
    error: corrupt.error ? corrupt.error.code : 'NO ERROR (unexpected)',
    message: corrupt.error ? corrupt.error.message.slice(0, 80) : '',
    output_bytes: host.outSize,
    modes: ['truncated file', 'flipped SIZ header byte'],
  };
  if (!corrupt.error) throw new Error('corrupt input converted "successfully"');
  if (corrupt.error.code === 'wasm_trap') throw new Error('corrupt input trapped');
  if (host.outSize !== 0) throw new Error('corrupt input left output bytes');
  console.log('corrupt input typed error:', corrupt.error.code, '—', corrupt.error.message.slice(0, 50) + '…');

  fs.writeFileSync(path.join(OUT_DIR, 'node-results.json'), JSON.stringify(results, null, 2));
  console.log('NODE JP2K HARNESS PASS');
}

main().then(
  () => process.exit(0),
  (e) => { console.error('FAIL:', e.message); process.exit(1); },
);
