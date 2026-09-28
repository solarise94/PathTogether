// Compute worker: WASM chunk processing + OPFS random-write sink.
//
// Data path per chunk:
//   io-worker --transfer(ArrayBuffer)--> here
//     -> wasm-bindgen glue copies into WASM linear memory (copy 1)
//     -> Rust transform (rotl3^0x5A) + FNV-1a 64 checksum in WASM heap
//     -> glue copies result into a JS dst buffer (copy 2)
//     -> FileSystemSyncAccessHandle.write(dst, {at: Number(safe int)})
//
// Backpressure: a byte-budgeted in-flight window (cfg.windowBytes) bounds
// outstanding reads. Offsets are Numbers validated as safe integers; the
// deliberate write at 2^32+12345 plus the read-back canary at 12345 proves
// no 32-bit truncation anywhere in the path.

import init, { ChunkProcessor } from './slide_chunk_spike.js';
import {
  fillExpectedOutput,
  fnv1a64,
  makeRng,
  assertSafeOffset,
} from './common.js';

const OUT_FILE = 'spike-out.bin';
const EXPORT_FILE = 'spike-out-export.bin';
const HIGH_MARK_OFF = 2 ** 32 + 12345; // deliberate > 2^32 offset
const HIGH_MARK_LEN = 4096;
const BACKPATCH_OFF = 4096; // deliberate low-offset rewrite after the pass
const BACKPATCH_LEN = 4096;
const SPECIAL_INPUT_CHUNK = 7; // reads input at 2^32 + 2048

function highMarker(u8) {
  for (let i = 0; i < u8.length; i++) u8[i] = 0xb0 | (i & 0x0f);
  return u8;
}
function backpatchMarker(u8) {
  for (let i = 0; i < u8.length; i++) u8[i] = 0x60 | (i & 0x0f);
  return u8;
}
function bytesEqual(a, b) {
  if (a.length !== b.length) return false;
  for (let i = 0; i < a.length; i++) if (a[i] !== b[i]) return false;
  return true;
}

function buildPlan(cfg) {
  const rng = makeRng(cfg.seed);
  const chunks = [];
  let t = 0;
  let j = 0;
  while (t < cfg.outSize) {
    const len = Math.min(
      j === 0
        ? cfg.chunkMax // fixed first chunk so post-crash checks can recompute
        : cfg.chunkMin + Math.floor((rng() / 4294967296) * (cfg.chunkMax - cfg.chunkMin + 1)),
      cfg.outSize - t
    );
    let s;
    if (j === 0) {
      s = 0;
    } else if (j === SPECIAL_INPUT_CHUNK && cfg.inSize > 2 ** 32 + 2048 + len) {
      s = 2 ** 32 + 2048; // deliberate input read beyond 2^32
    } else {
      // half the reads biased into the top third of the file so a good
      // fraction lands beyond 4 GiB
      const span = cfg.inSize - len;
      s = j % 2 === 0 ? Math.floor((rng() / 4294967296) * span)
        : span - 1 - Math.floor((rng() / 4294967296) * (span / 3));
    }
    assertSafeOffset(s, `chunk ${j} source offset`);
    assertSafeOffset(t, `chunk ${j} target offset`);
    chunks.push({ j, s, t, len });
    t += len;
    j += 1;
  }
  return chunks;
}

async function run(cfg, port, post) {
  const t0 = performance.now();
  await init(); // fetch + instantiate slide_chunk_spike_bg.wasm (no threads)
  const tWasm = performance.now();
  post({ type: 'phase', phase: 'wasm-ready', ms: Math.round(tWasm - t0) });

  const proc = new ChunkProcessor(cfg.chunkMax);
  const wasmHeapBytes = proc.heap_bytes();

  const root = await navigator.storage.getDirectory();
  for (const name of [OUT_FILE, EXPORT_FILE]) {
    try { await root.removeEntry(name); } catch (e) { /* absent */ }
  }
  const fh = await root.getFileHandle(OUT_FILE, { create: true });
  const h = await fh.createSyncAccessHandle();

  const chunks = buildPlan(cfg);
  const nChunks = chunks.length;
  const records = new Array(nChunks);
  let inflightBytes = 0;
  const inflight = new Map();
  let next = 0;
  let written = 0;
  let wroteHigh = false;
  let wroteBack = false;
  let killFired = false;
  let reloadFired = false;
  let tWriteStart = performance.now();
  let lastProgressBatch = [];

  function pump() {
    while (next < nChunks &&
           (inflight.size === 0 || inflightBytes + chunks[next].len <= cfg.windowBytes)) {
      const c = chunks[next];
      next += 1;
      inflightBytes += c.len;
      inflight.set(c.j, c);
      port.postMessage({ type: 'read', id: c.j, offset: c.s, length: c.len });
    }
  }

  const writeDone = new Promise((resolve, reject) => {
    port.onmessage = (ev) => {
      const m = ev.data;
      if (m.type === 'chunk-error') {
        reject(new Error('io-worker read failed: ' + m.error));
        return;
      }
      if (m.type !== 'chunk') return;
      const c = inflight.get(m.id);
      inflight.delete(m.id);
      inflightBytes -= c.len;
      const src = new Uint8Array(m.buffer); // transferred, we own it now
      const dst = new Uint8Array(c.len);
      const hash = proc.process_into(src, dst); // BigInt (FNV-1a 64 of output)
      const at = assertSafeOffset(c.t, 'write at');
      h.write(dst, { at }); // random-offset sink write
      records[c.j] = { s: c.s, t: c.t, len: c.len, hash: hash.toString() };
      written += c.len;
      lastProgressBatch.push(records[c.j]);
      if (written % (64 * 1024 * 1024) < c.len || next === nChunks) {
        post({ type: 'progress', written, total: cfg.outSize, records: lastProgressBatch });
        lastProgressBatch = [];
      }
      if (!killFired && cfg.killAtBytes && written >= cfg.killAtBytes) {
        killFired = true;
        post({ type: 'killme', written });
      }
      if (!reloadFired && cfg.reloadAtBytes && written >= cfg.reloadAtBytes) {
        reloadFired = true;
        post({ type: 'reloadme', written });
      }
      if (inflight.size === 0 && next >= nChunks) resolve();
      else pump();
    };
    pump();
  });

  await writeDone;
  const wroteAll = written;
  const writeMs = performance.now() - tWriteStart;

  // Marker writes AFTER the sequential pass: a write at a high offset
  // (> 2^32) and a "backpatch" at a low offset, as a real TIFF writer would
  // do when finalizing the IFD at the start of the file.
  const hm = highMarker(new Uint8Array(HIGH_MARK_LEN));
  h.write(hm, { at: assertSafeOffset(HIGH_MARK_OFF, 'high marker') });
  wroteHigh = true;
  const bm = backpatchMarker(new Uint8Array(BACKPATCH_LEN));
  h.write(bm, { at: assertSafeOffset(BACKPATCH_OFF, 'backpatch') });
  wroteBack = true;
  h.flush();
  h.close();
  const tFlushed = performance.now();
  post({ type: 'phase', phase: 'flushed-closed', ms: Math.round(tFlushed - tWriteStart - writeMs) });

  if (cfg.stopAfterWrite) {
    // termination/reload phases: the page will inspect the file after the
    // workers are killed / the page reloads; do not verify from here.
    post({
      type: 'done',
      result: {
        ok: true,
        stopped: true,
        written: wroteAll,
        nChunks,
        wasmHeapBytes,
        writeMs,
      },
    });
    return;
  }

  // ---- reopen + verify ----
  const tVerifyStart = performance.now();
  const h2 = await fh.createSyncAccessHandle();
  const size = h2.getSize(); // Number (safe integer)
  // For small smoke runs (< 4 GiB) the high marker at 2^32+12345 legitimately
  // extends the sparse file past EOF; the expected size accounts for it.
  const finalSize = cfg.outSize >= HIGH_MARK_OFF + HIGH_MARK_LEN
    ? cfg.outSize
    : HIGH_MARK_OFF + HIGH_MARK_LEN;
  if (size !== finalSize) throw new Error(`size after reopen ${size} != ${finalSize}`);

  const inMarkerRange = (t, len) =>
    (t < HIGH_MARK_OFF + HIGH_MARK_LEN && t + len > HIGH_MARK_OFF) ||
    (t < BACKPATCH_OFF + BACKPATCH_LEN && t + len > BACKPATCH_OFF);

  // deterministic sample selection: edges, spread, max-s and max-t chunks
  const sampleIdx = new Set([0, SPECIAL_INPUT_CHUNK, nChunks - 1]);
  let maxS = 1, maxT = 1;
  for (let i = 1; i < nChunks; i++) {
    if (chunks[i].s > chunks[maxS].s) maxS = i;
    if (chunks[i].t > chunks[maxT].t) maxT = i;
  }
  sampleIdx.add(maxS); sampleIdx.add(maxT);
  const step = Math.max(1, Math.floor(nChunks / 20));
  for (let i = 0; i < nChunks; i += step) sampleIdx.add(i);

  const verify = { samples: 0, byteCompareOk: 0, fnvCrossOk: 0, failures: [] };
  const expected = new Uint8Array(cfg.chunkMax);
  const readback = new Uint8Array(cfg.chunkMax);
  for (const i of sampleIdx) {
    const c = chunks[i];
    if (inMarkerRange(c.t, c.len)) continue;
    const n = h2.read(readback.subarray(0, c.len), { at: assertSafeOffset(c.t, 'verify read') });
    if (n !== c.len) { verify.failures.push({ i, what: 'short read', n }); continue; }
    fillExpectedOutput(expected.subarray(0, c.len), c.s);
    if (!bytesEqual(readback.subarray(0, c.len), expected.subarray(0, c.len))) {
      verify.failures.push({ i, what: 'byte mismatch', t: c.t, s: c.s, len: c.len });
      continue;
    }
    verify.byteCompareOk += 1;
    // one full-chunk BigInt FNV cross-check WASM hash vs independent JS hash
    if (!verify.fnvCheckedChunk) {
      verify.fnvCheckedChunk = i;
      const jsHash = fnv1a64(readback.subarray(0, c.len));
      if (jsHash.toString() === records[i].hash) verify.fnvCrossOk += 1;
      else verify.failures.push({ i, what: 'fnv mismatch', wasm: records[i].hash, js: jsHash.toString() });
    }
    verify.samples += 1;
  }

  // markers
  const mark = new Uint8Array(HIGH_MARK_LEN);
  h2.read(mark, { at: assertSafeOffset(HIGH_MARK_OFF, 'high marker read') });
  const highOk = bytesEqual(mark, highMarker(new Uint8Array(HIGH_MARK_LEN)));
  h2.read(mark.subarray(0, BACKPATCH_LEN), { at: BACKPATCH_OFF });
  const backOk = bytesEqual(mark.subarray(0, BACKPATCH_LEN), backpatchMarker(new Uint8Array(BACKPATCH_LEN)));

  // truncation canary: if any path had truncated 2^32+12345 to 32 bits, the
  // high marker would now sit at absolute offset 12345.
  const canary = new Uint8Array(16);
  h2.read(canary, { at: 12345 });
  const truncationHappened = bytesEqual(canary, highMarker(new Uint8Array(16)));

  const inputBeyond4G = chunks.filter((c) => c.s > 2 ** 32).length;
  const outputBeyond4G = chunks.filter((c) => c.t + c.len > 2 ** 32).length;
  const verifyMs = performance.now() - tVerifyStart;
  h2.close();

  // ---- export proxy: stream OPFS -> FileSystemWritableFileStream ----
  let exportResult = null;
  if (cfg.runExport) {
    const tExp = performance.now();
    const h3 = await fh.createSyncAccessHandle();
    const dstFh = await root.getFileHandle(EXPORT_FILE, { create: true });
    const w = await dstFh.createWritable(); // streaming writable (no size hint)
    const CHUNK = 4 * 1024 * 1024;
    const buf = new Uint8Array(CHUNK); // one bounded reusable buffer
    let pos = 0;
    const srcSize = h3.getSize();
    while (pos < srcSize) {
      const n = h3.read(buf, { at: assertSafeOffset(pos, 'export read') });
      if (n <= 0) break;
      await w.write({ type: 'write', position: pos, data: buf.subarray(0, n) });
      pos += n;
    }
    await w.close();
    h3.close();
    const dstFile = await dstFh.getFile();
    // sampled correctness of the exported copy (first + >4GiB region)
    const dBuf = new Uint8Array(4096);
    const dFile = await fh.getFile();
    const spot1 = await dFile.slice(0, 4096).arrayBuffer();
    const off2 = srcSize - 8192;
    const spot2 = await dFile.slice(off2, off2 + 4096).arrayBuffer();
    const e1 = await dstFile.slice(0, 4096).arrayBuffer();
    const e2 = await dstFile.slice(off2, off2 + 4096).arrayBuffer();
    exportResult = {
      ms: Math.round(performance.now() - tExp),
      srcSize,
      dstSize: dstFile.size,
      sizeOk: dstFile.size === srcSize,
      spotStartOk: bytesEqual(new Uint8Array(e1), new Uint8Array(spot1)),
      spotEndOk: bytesEqual(new Uint8Array(e2), new Uint8Array(spot2)),
      maxBufferBytes: CHUNK,
    };
  }

  const estimateAfter = await navigator.storage.estimate();

  post({
    type: 'done',
    result: {
      ok: verify.failures.length === 0 && highOk && backOk && !truncationHappened,
      written: wroteAll,
      nChunks,
      wasmHeapBytes,
      windowBytes: cfg.windowBytes,
      writeMs: Math.round(writeMs),
      writeMiBs: +(((written / 1048576) / (writeMs / 1000)).toFixed(1)),
      verifyMs: Math.round(verifyMs),
      verify,
      highMarkerOk: highOk,
      backpatchOk: backOk,
      truncationCanaryClean: !truncationHappened,
      inputReadsBeyond4GiB: inputBeyond4G,
      outputChunksBeyond4GiB: outputBeyond4G,
      highMarkOff: HIGH_MARK_OFF,
      outSize: size,
      export: exportResult,
      estimateAfter,
    },
  });
}

self.onmessage = async (e) => {
  const m = e.data;
  if (m.type !== 'run') return;
  const post = (msg) => self.postMessage(msg);
  try {
    await run(m.config, m.port, post);
  } catch (err) {
    post({ type: 'fatal', error: String((err && err.stack) || err) });
  }
};
