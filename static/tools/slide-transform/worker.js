// worker.js — C2 dedicated compute worker (ES module worker, no deps).
//
// Hosts the wasm transform core and implements the stHost* IO contract:
//   source       : the user's File is first streamed (BYOB, one reused
//                  buffer) into the job's OPFS `source.bin`; every later
//                  read (hash, probe, convert, validate) is a sync-handle
//                  read straight into wasm memory. Never materialized
//                  whole-file.
//   output       : OPFS FileSystemSyncAccessHandle random writes.
//   scratch      : OPFS sync handles per core-named scratch file
//                  (index spills + per-IFD offset/count streams).
//   journal      : append-only checksummed JSON-lines with the strict
//                  order  data-write → flush → journal-commit → flush.
//
// Offsets crossing the JS bridge are Number safe integers (asserted); the
// core keeps u64 semantics. No whole-file read path exists here: see
// tests/browser/slide_tools_c2/test_no_whole_file.js which greps for them.
//
// Fault-injection hooks (deterministic crash points for the C2 matrix) are
// gated behind init{testMode:true} and inert in production.

import init, {
  probe, convertProfile, convertResumeProfile, finalizeValidate, sha256Source,
  coreVersion, configure, enableCheckpoint,
} from './slide_transform.js';
import * as E from './engine.js';

const MIB = 2 ** 20;

let testMode = false;
let wasm = null; // InitOutput (has .memory)
let outHandle = null; // FileSystemSyncAccessHandle (output.tif)
let outSize = 0;
let jobDir = null; // OPFS dir of the running job
let scratchDir = null;
let scratchHandles = new Map(); // name -> sync handle
let journalHandle = null;
let journalSize = 0;
let journalSeq = 0;
let gen = 0;

let running = null; // mutable run state
let cancelFlag = false;
let pendingHostError = null;

function post(msg) { self.postMessage(msg); }
function log(...a) { post({ type: 'log', text: a.join(' ') }); }

function mem() { return wasm.memory; }

function view(ptr, len) {
  return new Uint8Array(mem().buffer, ptr, len);
}

// ------------------------------------------------------------ fault hooks --

function faultActive(name) {
  return !!(running && running.faults && running.faults[name] !== undefined);
}

function hangUntilDeath(ms = 3000) {
  const t0 = Date.now();
  while (Date.now() - t0 < ms) { /* busy wait for external terminate */ }
}

// ------------------------------------------------------------ sync source --

// The job reads its source from the OPFS copy (`source.bin`) staged by
// 'stage-source': a FileSystemSyncAccessHandle reads straight into wasm
// memory, so the synchronous read bridge allocates nothing per call.
// FileReaderSync/Blob reads were measured to pile up transient
// ArrayBuffers for the whole duration of a synchronous wasm call
// (docs/slide-tools/c2-runner-report.md §4.3).
let srcHandle = null; // FileSystemSyncAccessHandle of source.bin
let srcSize = 0;
let srcJobId = null;

async function openSource(jobId) {
  if (srcHandle && srcJobId === jobId) return srcHandle;
  closeSource();
  const dir = await opfsJobDir(jobId, false);
  const fh = await E.withRetry(() => dir.getFileHandle(E.SOURCE_NAME), { name: 'source copy' });
  srcHandle = await E.withRetry(() => fh.createSyncAccessHandle(), { name: 'source handle' });
  srcSize = srcHandle.getSize();
  srcJobId = jobId;
  return srcHandle;
}

function closeSource() {
  try { if (srcHandle) srcHandle.close(); } catch { /* */ }
  srcHandle = null;
  srcSize = 0;
  srcJobId = null;
}

function syncReadInto(offset, len, ptr) {
  try {
    E.assertSafeOffset(offset, 'read offset');
    if (!srcHandle) return 'source not open';
    if (offset + len > srcSize) {
      return `read [${offset},+${len}) beyond source ${srcSize}`;
    }
    const n = srcHandle.read(view(ptr, len), { at: offset });
    if (n !== len) return `source short read ${n}/${len} @${offset}`;
    return null;
  } catch (e) {
    return E.errText(e);
  }
}

/// Stream the user's File into source.bin: BYOB reader refilling ONE buffer,
/// each chunk written through the sync handle before the buffer is reused.
async function stageSource(jobId, file, faults) {
  closeSource();
  const dir = await opfsJobDir(jobId, true);
  const fh = await E.withRetry(() => dir.getFileHandle(E.SOURCE_NAME, { create: true }));
  const h = await E.withRetry(() => fh.createSyncAccessHandle());
  try {
    h.truncate(0);
    const reader = file.stream().getReader({ mode: 'byob' });
    let buf = new ArrayBuffer(E.STAGE_CHUNK);
    let at = 0;
    let lastPost = 0;
    for (;;) {
      const { value, done } = await reader.read(new Uint8Array(buf));
      if (done) break;
      if (testMode && faults && faults.crashInStage !== undefined && at >= faults.crashInStage) {
        post({ type: 'fault-reached', fault: 'crashInStage', jobId, detail: { at } });
        hangUntilDeath();
        self.close();
      }
      const n = h.write(value, { at });
      if (n !== value.length) throw new Error(`source copy short write ${n}/${value.length}`);
      at += n;
      buf = value.buffer;
      if (at - lastPost >= 256 * MIB) {
        lastPost = at;
        post({ type: 'progress', progress: { unit: 'stage', done: at, total: file.size } });
      }
    }
    if (at !== file.size) throw new Error(`source copy ${at} ≠ file ${file.size}`);
    h.flush();
  } catch (e) {
    try { h.close(); } catch { /* */ }
    const name = String((e && e.name) || '') + String(e);
    if (/QuotaExceeded/i.test(name)) {
      throw E.stError(E.ERROR_CODES.QUOTA_EXCEEDED, `源副本写入配额不足：${E.errText(e)}`);
    }
    throw e;
  }
  h.close();
  await openSource(jobId);
  const r = JSON.parse(sha256Source());
  if (r.error) throw r;
  return { size: srcSize, sha256: r.sha256 };
}

// -------------------------------------------------------------- stHost IO --


function installHosts() {
  globalThis.stHostSourceSize = () => srcSize;
  globalThis.stHostReadInto = (offset, len, ptr) => syncReadInto(offset, len, ptr);
  // legacy C1 host (unused by this core build; kept allocation-bounded)
  globalThis.stHostRead = (offset, len) => {
    const u8 = new Uint8Array(len);
    const n = srcHandle ? srcHandle.read(u8, { at: offset }) : 0;
    return u8.subarray(0, n);
  };

  globalThis.stHostWrite = (offset, data) => {
    if (pendingHostError) {
      const e = pendingHostError;
      pendingHostError = null;
      return e;
    }
    try {
      E.assertSafeOffset(offset, 'write at');
      if (running) {
        running.writes += 1;
        running.bytesWritten += data.length;
        // deterministic mid-payload crash
        if (faultActive('crashAtWrite') && !running.fired.has('crashAtWrite')) {
          if (running.writes >= running.faults.crashAtWrite) {
            running.fired.add('crashAtWrite');
            post({ type: 'fault-reached', fault: 'crashAtWrite', jobId: running.jobId, detail: { write: running.writes, at: offset } });
            hangUntilDeath();
            self.close();
          }
        }
        // injected quota exhaustion (once)
        if (running.faults && running.faults.quotaErrorAtWrite === running.writes) {
          return 'QuotaExceededError: injected (test)';
        }
        // handle loss: close the output handle → subsequent ops throw
        if (running.faults && running.faults.closeOutputAtWrite === running.writes) {
          try { outHandle.close(); } catch { /* */ }
        }
        // finalize-phase crash: many writes with no new checkpoints
        if (running.faults && running.faults.crashInFinalize && running.lastCkpt &&
            !running.fired.has('crashInFinalize') &&
            running.writesSinceCkpt > (running.faults.finalizeWriteThreshold || 64)) {
          running.fired.add('crashInFinalize');
          post({ type: 'fault-reached', fault: 'crashInFinalize', jobId: running.jobId, detail: { writesSinceCkpt: running.writesSinceCkpt } });
          hangUntilDeath();
          self.close();
        }
        if (running.faults && running.faults.writeDelayMs && testMode) {
          const t0 = Date.now();
          while (Date.now() - t0 < running.faults.writeDelayMs) { /* slow IO */ }
        }
        const cap = running.outputCapBytes;
        if (cap && offset + data.length > cap) {
          return `用户输出上限 ${cap} 已超出（写入至 ${offset + data.length}）`;
        }
      }
      const n = outHandle.write(data, { at: offset });
      if (n !== data.length) return `short write ${n}/${data.length} @${offset}`;
      if (running) {
        running.writesSinceCkpt += 1;
        running.chunkLog.push([data.length, E.fnv2x32(data)]);
        if (running.chunkLog.length > 4096) running.chunkLog.splice(0, running.chunkLog.length - 4096);
      }
      return null;
    } catch (e) {
      const name = (e && e.name) || '';
      if (/QuotaExceeded|NS_ERROR_FILE_NO_DEVICE_SPACE/i.test(name + String(e))) {
        return `QuotaExceededError: ${E.errText(e)}`;
      }
      return E.errText(e);
    }
  };
  globalThis.stHostTruncate = (len) => {
    try {
      outHandle.truncate(E.assertSafeOffset(len, 'truncate'));
      outSize = len;
      return null;
    } catch (e) { return E.errText(e); }
  };
  globalThis.stHostFlush = () => {
    try { outHandle.flush(); return null; } catch (e) { return E.errText(e); }
  };

  globalThis.stHostScratchOpen = (name, preserve) => {
    try {
      openScratch(name, !!preserve);
      return null;
    } catch (e) { return E.errText(e); }
  };
  globalThis.stHostScratchRead = (name, offset, len) => {
    const h = scratchHandles.get(name);
    if (!h) throw new Error(`scratch ${name} not open`);
    const u8 = new Uint8Array(len);
    const n = h.read(u8, E.assertSafeOffset(offset, 'scratch read'));
    if (n !== len) throw new Error(`scratch short read ${n}/${len}`);
    return u8;
  };
  globalThis.stHostScratchWrite = (name, offset, data) => {
    try {
      const h = scratchHandles.get(name);
      if (!h) return `scratch ${name} not open`;
      h.write(E.assertSafeOffset(offset, 'scratch write'), data);
      return null;
    } catch (e) { return E.errText(e); }
  };
  globalThis.stHostScratchTruncate = (name, len) => {
    try {
      const h = scratchHandles.get(name);
      if (!h) return `scratch ${name} not open`;
      h.truncate(E.assertSafeOffset(len, 'scratch truncate'));
      return null;
    } catch (e) { return E.errText(e); }
  };
  globalThis.stHostScratchFlush = (name) => {
    try {
      const h = scratchHandles.get(name);
      if (h) h.flush();
      return null;
    } catch (e) { return E.errText(e); }
  };

  globalThis.stHostOutSize = () => outSize;
  globalThis.stHostOutReadInto = (offset, len, ptr) => {
    try {
      if (running && running.faults && running.faults.crashInValidate &&
          !running.fired.has('crashInValidate')) {
        running.outReads = (running.outReads || 0) + 1;
        if (running.outReads >= 3) {
          running.fired.add('crashInValidate');
          post({ type: 'fault-reached', fault: 'crashInValidate', jobId: running.jobId, detail: { reads: running.outReads } });
          hangUntilDeath();
          self.close();
        }
      }
      const u8 = view(ptr, len);
      const n = outHandle.read(u8, { at: E.assertSafeOffset(offset, 'out read') });
      if (n !== len) return `output short read ${n}/${len} @${offset}`;
      return null;
    } catch (e) { return E.errText(e); }
  };

  globalThis.stHostProgress = (jsonText) => {
    if (!running) return;
    const now = Date.now();
    if (now - running.lastProgressTs < 250 && !jsonText.includes('"unit":"level"')) return;
    running.lastProgressTs = now;
    post({ type: 'progress', progress: JSON.parse(jsonText) });
  };

  globalThis.stHostCancelled = () => cancelFlag;

  globalThis.stHostCheckpoint = (jsonText) => {
    if (!running) return;
    const st = JSON.parse(jsonText);
    running.lastCkpt = st;
    running.writesSinceCkpt = 0;
    // wasm heap accounting (profile soft cap → typed recoverable error)
    const heap = mem().buffer.byteLength;
    if (heap > running.profile.wasmHeapCapBytes) {
      pendingHostError = `resource_profile_insufficient: wasm 堆 ${heap} > 档位上限 ` +
        `${running.profile.wasmHeapCapBytes}`;
      return;
    }
    running.wasmHeapPeak = Math.max(running.wasmHeapPeak, heap);
    const now = Date.now();
    const bytesSince = st.out - (running.lastJournaledOut ?? 0);
    if (bytesSince >= running.profile.journalIntervalBytes ||
        now - running.lastJournalTs >= running.profile.journalMaxAgeMs ||
        st.cell === 0) {
      commitCheckpoint(st);
    }
  };
}

// createSyncAccessHandle is async, but the core requests scratch sinks
// synchronously mid-parse/mid-conversion. The names it will ask for are
// determined before the wasm call starts (index spills per level + one
// offcnt stream per IFD — exact for convert, a bounded superset for
// probe); the async setup pre-opens them; this lookup only truncates
// non-preserved opens (parse-phase spills rewrite from offset 0).
// Scratch write-back buffering: the core streams 12–48 B records to the
// paged index / offcnt spills one write_at() at a time (cheap on native
// files, ~0.3–0.4 ms per OPFS sync op — 134k records ≈ 50 s on a 10 GiB
// input). Each scratch handle carries a small contiguous tail buffer that
// coalesces appends; any read/truncate/flush path flushes it first
// (cache-coherent by construction). CAP is fixed per handle — bounded,
// engine-managed, never grows with the input.
const SCRATCH_BUF_CAP = 128 * 1024;

class BufferedScratch {
  constructor(handle) {
    this.h = handle;
    this.buf = new Uint8Array(SCRATCH_BUF_CAP);
    this.start = -1;
    this.len = 0;
  }
  flush() {
    if (this.len > 0) {
      const n = this.h.write(this.buf.subarray(0, this.len), { at: this.start });
      if (n !== this.len) throw new Error(`scratch short buffered write ${n}/${this.len}`);
      this.start = -1;
      this.len = 0;
    }
  }
  write(off, data) {
    if (this.len > 0 && off === this.start + this.len &&
        this.len + data.length <= SCRATCH_BUF_CAP) {
      this.buf.set(data, this.len);
      this.len += data.length;
      return;
    }
    this.flush();
    if (data.length > SCRATCH_BUF_CAP) {
      const n = this.h.write(data, { at: off });
      if (n !== data.length) throw new Error('scratch short write');
      return;
    }
    this.buf.set(data, 0);
    this.start = off;
    this.len = data.length;
  }
  read(out, off) {
    // any read invalidates the buffer (simplest correct coherence)
    this.flush();
    return this.h.read(out, { at: off });
  }
  truncate(len) {
    this.flush();
    this.h.truncate(len);
  }
  close() {
    try { this.flush(); } finally { this.h.close(); }
  }
}

const PROBE_LEVEL_CAP = 40;

function openScratch(name, preserve) {
  let h = scratchHandles.get(name);
  if (h) {
    if (!preserve) h.truncate(0);
    return h;
  }
  const pre = scratchDir && scratchDir.preopened.get(name);
  if (!pre) throw new Error(`scratch ${name} was not pre-opened`);
  if (!preserve) pre.truncate(0);
  const wrapped = new BufferedScratch(pre);
  scratchHandles.set(name, wrapped);
  return wrapped;
}

// -------------------------------------------------------------- journal --

async function prepareJournal(jobDirHandle, startGen) {
  const fh = await E.withRetry(() => jobDirHandle.getFileHandle(E.JOURNAL_FILE, { create: true }));
  journalHandle = await E.withRetry(() => fh.createSyncAccessHandle());
  journalSize = journalHandle.getSize();
  gen = startGen;
  journalSeq = 0;
}

function journalAppend(obj) {
  const text = E.encodeJournalRecord(obj);
  const u8 = new TextEncoder().encode(text);
  journalHandle.write(u8, { at: journalSize });
  journalSize += u8.length;
  journalHandle.flush();
}

function commitCheckpoint(st) {
  // strict order: data already written → flush → journal commit → flush
  outHandle.flush();
  for (const h of scratchHandles.values()) {
    try { h.flush(); } catch { /* scratch flush best-effort in commit path */ }
  }
  if (running && testMode && running.faults &&
      running.faults.crashAfterFlushBeforeJournal && !running.fired.has('crashAfterFlushBeforeJournal')) {
    running.fired.add('crashAfterFlushBeforeJournal');
    post({ type: 'fault-reached', fault: 'crashAfterFlushBeforeJournal',
      jobId: running.jobId, detail: { out: st.out, journaledOut: running.lastJournaledOut } });
    hangUntilDeath();
    self.close();
  }
  journalSeq += 1;
  journalAppend({
    t: 'c', gen, seq: journalSeq, st,
    chunks: running ? running.chunkLog.splice(0, running.chunkLog.length) : [],
  });
  if (running) {
    running.lastJournaledOut = st.out;
    running.lastJournalTs = Date.now();
  }
  if (running && testMode && running.faults &&
      running.faults.crashAfterCheckpoint && !running.fired.has('crashAfterCheckpoint')) {
    running.fired.add('crashAfterCheckpoint');
    post({ type: 'fault-reached', fault: 'crashAfterCheckpoint', jobId: running.jobId, detail: { out: st.out } });
    hangUntilDeath();
    self.close();
  }
}

// ---------------------------------------------------------------- setup --

async function opfsJobDir(jobId, create) {
  const root = await navigator.storage.getDirectory();
  const jobs = await root.getDirectoryHandle(E.JOB_ROOT, { create: true });
  return withRetryDir(jobs, jobId, create);
}
async function withRetryDir(jobs, jobId, create) {
  return E.withRetry(() => jobs.getDirectoryHandle(jobId, { create: !!create }), { name: 'job dir' });
}

/// Pre-open every scratch name the core will request:
///  - index spills: BF `grid-l{level}` / FL `kfbf-cells-l{level}`
///  - offcnt streams: BF `offcnt-l{i}` / FL `ome-offcnt-{i}` (i = IFD order)
/// modality + level count + IFD count come from the probe run in 'start'.
async function openScratchSet(dirHandle, names) {
  const map = new Map();
  for (const n of names) {
    const fh = await E.withRetry(() => dirHandle.getFileHandle(n, { create: true }));
    // a dying worker's handle lock can persist briefly (C0 ADR §7) — retry
    map.set(n, await E.withRetry(() => fh.createSyncAccessHandle()));
  }
  return map;
}

function closeScratchSet() {
  if (scratchDir) {
    for (const h of scratchDir.preopened.values()) {
      try { h.close(); } catch { /* */ }
    }
  }
  scratchDir = null;
  scratchHandles = new Map();
}

/// Exact names for a conversion (geometry known from the probe).
async function prepareScratch(levels, ifdCount, modality) {
  closeScratchSet();
  const dirHandle = await jobDir.getDirectoryHandle('scratch', { create: true });
  const names = [];
  for (let l = 0; l < levels; l++) {
    names.push(modality === 'fluorescence' ? `kfbf-cells-l${l}` : `grid-l${l}`);
  }
  for (let i = 0; i < ifdCount; i++) {
    names.push(modality === 'fluorescence' ? `ome-offcnt-${i}` : `offcnt-l${i}`);
  }
  scratchDir = { preopened: await openScratchSet(dirHandle, names), dir: dirHandle };
  return dirHandle;
}

/// Bounded superset for probe (index spills only; geometry unknown yet).
/// Lives in a throwaway dir under slide-jobs/.tmp and is removed after.
async function prepareProbeScratch() {
  closeScratchSet();
  const root = await navigator.storage.getDirectory();
  const jobs = await root.getDirectoryHandle(E.JOB_ROOT, { create: true });
  const tmp = await jobs.getDirectoryHandle('.tmp', { create: true });
  const id = `probe-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
  const dir = await tmp.getDirectoryHandle(id, { create: true });
  const names = [];
  for (let l = 0; l < PROBE_LEVEL_CAP; l++) {
    names.push(`grid-l${l}`, `kfbf-cells-l${l}`);
  }
  scratchDir = { preopened: await openScratchSet(dir, names), dir, tmpId: id };
  return dir;
}

async function releaseProbeScratch() {
  const tmpId = scratchDir && scratchDir.tmpId;
  closeScratchSet();
  if (tmpId) {
    try {
      const root = await navigator.storage.getDirectory();
      const jobs = await root.getDirectoryHandle(E.JOB_ROOT);
      const tmp = await jobs.getDirectoryHandle('.tmp');
      await E.removeEntryRecursive(tmp, tmpId);
    } catch { /* best effort; .tmp is pruned on the next probe */ }
  }
}

function closeAllHandles() {
  try { if (journalHandle) journalHandle.close(); } catch { /* */ }
  journalHandle = null;
  closeScratchSet();
  try { if (outHandle) outHandle.close(); } catch { /* */ }
  outHandle = null;
  closeSource();
}

// ------------------------------------------------------------------ job --

let tJobStart = 0;

async function runJob(msg) {
  tJobStart = Date.now();
  const { jobId, file, opts, faults } = msg;
  let profile = E.getProfile(opts.profileId);
  E.assertProfileFeasible(profile);
  if (testMode && faults && faults.journalIntervalBytes) {
    // deterministic small-interval journaling for fault injection
    profile = { ...profile, journalIntervalBytes: faults.journalIntervalBytes };
  }
  running = {
    jobId, profile, faults: faults || null, fired: new Set(),
    writes: 0, bytesWritten: 0, writesSinceCkpt: 0,
    chunkLog: [], lastCkpt: null, lastJournaledOut: 0,
    lastJournalTs: Date.now(), lastProgressTs: 0,
    wasmHeapPeak: 0, outputCapBytes: opts.outputCapBytes || null,
    outReads: 0,
  };
  cancelFlag = false;
  pendingHostError = null;
  await openSource(jobId);
  if (opts.identity && srcSize !== opts.identity.size) {
    throw new Error(`源副本长度 ${srcSize} ≠ 记录 ${opts.identity.size}`);
  }

  const jobsRoot = await (await navigator.storage.getDirectory())
    .getDirectoryHandle(E.JOB_ROOT, { create: true });
  const dirHandle = await withRetryDir(jobsRoot, jobId, true);
  jobDir = dirHandle;
  jobDir.__modality = opts.modality;

  // probe-derived geometry decides which scratch names to pre-open
  const levels = opts.scratchLevels | 0;
  const ifdCount = opts.scratchIfdCount | 0;
  await prepareScratch(levels, ifdCount, opts.modality);

  const outFh = await E.withRetry(() => dirHandle.getFileHandle(E.OUTPUT_NAME, { create: true }));
  outHandle = await E.withRetry(() => outFh.createSyncAccessHandle());

  const resume = opts.resume || null;
  post({ type: 'phase', phase: 'job-open', ms: Date.now() - tJobStart });
  await prepareJournal(dirHandle, opts.nextGen || 1);
  journalAppend({
    t: 'gen', gen, identity: opts.identity || null,
    core: opts.coreVersion, plan: 1,
    policy: opts.policy, profile: profile.id,
    outputProfile: opts.outputProfile,
    sourceAdapter: opts.sourceAdapter || null,
    cap: opts.outputCapBytes || null,
    resume: resume ? resume.st : null,
  });

  if (resume) {
    // crash aftermath: truncate output + begun offcnt streams to committed
    outHandle.truncate(E.assertSafeOffset(resume.st.out, 'resume out'));
    const prefix = opts.modality === 'fluorescence' ? 'ome-offcnt-' : 'offcnt-l';
    resume.st.ifds.forEach((tiles, i) => {
      const h = scratchDir.preopened.get(`${prefix}${i}`);
      if (h) h.truncate(tiles * 12);
    });
    // index spills are rewritten by the parse phase (truncate on open)
  } else {
    outHandle.truncate(0);
    for (const h of scratchDir.preopened.values()) h.truncate(0);
  }
  outSize = outHandle.getSize();
  running.lastJournaledOut = resume ? resume.st.out : 0;

  installHosts();
  configure(1);
  enableCheckpoint();

  const strict = opts.policy === 'strict-lossless';
  const channelJson = opts.channelJson || '';
  // the core refuses to continue a checkpoint journalled under another profile
  const outputProfile = opts.outputProfile || '';
  const t0 = Date.now();
  post({ type: 'phase', phase: 'pre-convert', ms: Date.now() - tJobStart });
  post({ type: 'state', state: 'running' });
  let conv;
  try {
    conv = resume
      ? convertResumeProfile(JSON.stringify(resume.st), outputProfile, strict, channelJson)
      : convertProfile(outputProfile, strict, channelJson);
  } catch (e) {
    conv = JSON.stringify(E.stError('io_recoverable', `wasm 异常: ${E.errText(e)}`));
  }
  const convJson = JSON.parse(conv);
  const convertMs = Date.now() - t0;
  post({ type: 'phase', phase: 'convert-done', ms: convertMs,
    outBytes: convJson.output_bytes || 0, err: convJson.error || null });

  if (convJson.error) {
    await failJob(convJson, convertMs);
    return;
  }
  if (cancelFlag) {
    await doCancel(false);
    return;
  }

  // finalize: flush + close, then reopen for validation (read-back)
  post({ type: 'state', state: 'finalizing' });
  outHandle.flush();
  outHandle.close();
  outHandle = await E.withRetry(() => outFh.createSyncAccessHandle());
  outSize = outHandle.getSize();
  if (outSize !== convJson.output_bytes) {
    await failJob(E.stError('io_recoverable',
      `输出长度 ${outSize} ≠ 核心报告 ${convJson.output_bytes}`), convertMs);
    return;
  }

  post({ type: 'state', state: 'validating' });
  post({ type: 'phase', phase: 'validate-start', ms: Date.now() - tJobStart });
  // ifd_count covers the main chain and every SubIFD for all profiles
  const vJson = JSON.parse(finalizeValidate(convJson.ifd_count | 0));
  const wasmHeapPeak = Math.max(running.wasmHeapPeak, mem().buffer.byteLength);

  closeAllHandles();
  post({ type: 'phase', phase: 'validate-done', ms: Date.now() - tJobStart });

  if (vJson.error) {
    post({ type: 'done', ok: false, convertMs, phase: 'validating',
      result: convJson, validation: vJson, error: vJson.error,
      wasmHeapPeak, journalBytes: journalSize });
    running = null;
    return;
  }

  post({ type: 'done', ok: true, convertMs, phase: 'validated',
    result: convJson, validation: vJson,
    resume: !!resume, wasmHeapPeak, journalBytes: journalSize,
    bytesWritten: running.bytesWritten });
  running = null;
}

async function failJob(convJson, convertMs) {
  const code = convJson.error.code;
  const msg = convJson.error.message || '';
  let out = E.stError(
    /QuotaExceeded/i.test(msg) ? E.ERROR_CODES.QUOTA_EXCEEDED : code, msg);
  if (/已取消/.test(msg)) out = E.stError(E.ERROR_CODES.CANCELLED, '已取消（核心在当前有界块边界停止）');
  closeAllHandles();
  if (cancelFlag) {
    await doCancel(false);
    return;
  }
  post({ type: 'done', ok: false, phase: 'convert', result: convJson,
    error: out.error, convertMs });
  running = null;
}

async function doCancel(fromMessage) {
  cancelFlag = true;
  closeAllHandles();
  let cleanupError = null;
  try {
    if (running && running.faults && running.faults.failCleanup && testMode) {
      throw new Error('injected cleanup failure (test)');
    }
    const root = await navigator.storage.getDirectory();
    const jobs = await root.getDirectoryHandle(E.JOB_ROOT);
    await E.removeEntryRecursive(jobs, running ? running.jobId : (currentJobId || ''));
  } catch (e) {
    cleanupError = E.errText(e);
  }
  post({ type: 'cancelled', cleanedUp: !cleanupError, cleanupError,
    jobId: running ? running.jobId : currentJobId });
  running = null;
}

let currentJobId = null;

// ------------------------------------------------------------ dispatcher --

const pending = new Map(); // id -> {resolve, reject}

self.onmessage = async (ev) => {
  const m = ev.data;
  if (m.type === 'init') {
    testMode = !!m.testMode;
    if (!wasm) {
      wasm = await init(new URL('slide_transform_bg.wasm', import.meta.url));
    }
    installHosts(); // source/cancel callbacks are live from the start;
                    // output/scratch callbacks only fire during convert
    configure(1);
    post({ type: 'ready', coreVersion: coreVersion() });
    return;
  }
  if (m.type === 'cancel') {
    cancelFlag = true;
    if (!running) post({ type: 'cancelled', cleanedUp: true, cleanupError: null, jobId: null });
    return;
  }
  if (m.type === 'start') {
    currentJobId = m.jobId;
    try {
      await runJob(m);
    } catch (e) {
      closeAllHandles();
      post({ type: 'done', ok: false, phase: 'setup', error: {
        code: E.ERROR_CODES.IO_RECOVERABLE, message: E.errText(e) } });
      running = null;
    }
    return;
  }
  if (m.type === 'stage-source') {
    try {
      const tS = Date.now();
      configure(1);
      const r = await stageSource(m.jobId, m.file, m.faults || null);
      post({ type: 'phase', phase: 'stage-source', ms: Date.now() - tS, bytes: r.size });
      post({ type: 'reply', id: m.id, ok: true, result: r });
    } catch (e) {
      closeSource();
      post({ type: 'reply', id: m.id, ok: false,
        result: E.isStError(e) ? e : E.stError(E.ERROR_CODES.IO_RECOVERABLE, E.errText(e)) });
    }
    return;
  }
  if (m.type === 'probe') {
    try {
      const tP = Date.now();
      await openSource(m.jobId);
      configure(1);
      await prepareProbeScratch();
      const r = JSON.parse(probe());
      await releaseProbeScratch();
      post({ type: 'phase', phase: 'probe', ms: Date.now() - tP });
      post({ type: 'reply', id: m.id, ok: !r.error, result: r });
    } catch (e) {
      await releaseProbeScratch().catch(() => { /* */ });
      post({ type: 'reply', id: m.id, ok: false, result: E.stError('io_recoverable', E.errText(e)) });
    }
    return;
  }
  if (m.type === 'verify-source') {
    // resume identity: the staged copy must still be the bytes hashed at
    // staging time (OPFS corruption/tampering → refuse, never blind-resume)
    try {
      await openSource(m.jobId);
      configure(1);
      const r = srcSize === m.size ? JSON.parse(sha256Source()) : { size: srcSize };
      post({ type: 'reply', id: m.id, ok: true,
        result: { size: srcSize, sha256: r.sha256 || null } });
    } catch (e) {
      closeSource();
      post({ type: 'reply', id: m.id, ok: false, result: E.stError(E.ERROR_CODES.JOB_DIR_MISSING,
        `源副本缺失或不可读：${E.errText(e)}`) });
    }
    return;
  }
  if (m.type === 'hash-opfs-file') {
    let h = null;
    try {
      closeSource();
      const dir = await opfsJobDir(m.jobId, false);
      const fh = await dir.getFileHandle(m.name);
      h = await E.withRetry(() => fh.createSyncAccessHandle());
      srcHandle = h;
      srcSize = h.getSize();
      configure(1);
      const r = JSON.parse(sha256Source());
      post({ type: 'reply', id: m.id, ok: !r.error, result: r });
    } catch (e) {
      post({ type: 'reply', id: m.id, ok: false, result: E.stError('io_recoverable', E.errText(e)) });
    } finally {
      closeSource();
    }
    return;
  }
  if (m.type === 'release-source') {
    closeSource();
    post({ type: 'reply', id: m.id, ok: true, result: {} });
    return;
  }
};
