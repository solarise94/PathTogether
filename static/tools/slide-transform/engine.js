// engine.js — C2 browser runner shared library (ES module, no deps).
// Loaded by the page façade (runner.js) and the compute worker (worker.js).
//
// Everything here is deliberately free of DOM/Worker APIs so both contexts
// share one implementation of: resource profiles, typed errors, safe-offset
// guards, the checksummed journal codec, slot-pair records, the state
// machine and the disk-precheck arithmetic.

// ---------------------------------------------------------------- errors --

export const ERROR_CODES = {
  RESOURCE_PROFILE_INSUFFICIENT: 'resource_profile_insufficient',
  DISK_PRECHECK_FAILED: 'disk_precheck_failed',
  QUOTA_EXCEEDED: 'quota_exceeded_recoverable',
  IO_RECOVERABLE: 'io_recoverable',
  OUTPUT_CAP: 'conversion_output_too_large',
  RESUME_REFUSED: 'resume_refused',
  JOB_LOCKED: 'job_locked_other_tab',
  JOB_DIR_MISSING: 'job_dir_missing',
  NOT_READY: 'not_ready_not_exportable',
  SOURCE_CHANGED: 'source_changed_refuse_resume',
  UNSUPPORTED_INPUT: 'unsupported_input',
  CANCELLED: 'cancelled',
};

export function stError(code, message, extra = {}) {
  return { error: { code, message, ...extra } };
}
export function isStError(v) {
  return !!v && typeof v === 'object' && v.error && typeof v.error.code === 'string';
}
export function errCode(v) {
  return isStError(v) ? v.error.code : null;
}
export function errText(e) {
  if (isStError(e)) return `${e.error.code}: ${e.error.message}`;
  return String((e && e.message) || e);
}

// ------------------------------------------------------- safe integers --

export function assertSafeOffset(n, what = 'offset') {
  if (!Number.isSafeInteger(n) || n < 0) {
    throw new RangeError(`${what} not a safe unsigned integer: ${n}`);
  }
  return n;
}
export function isSafeOffset(n) {
  return Number.isSafeInteger(n) && n >= 0;
}

// ------------------------------------------------------------ profiles --

// Engine-managed totals (plan §4): wasm heap + codec scratch + caches +
// index pages + JS in-flight. The conversion engine is single-worker and
// synchronous, so the dominant terms are the wasm heap allowance and the
// journal/staging buffers; the budget is an envelope the engine asserts
// against, never a promise about browser RSS (measured separately).
export const PROFILES = {
  // stageBytes: the single reused buffer of the source-staging copy.
  saver: {
    id: 'saver', label: '节省',
    budgetBytes: 192 * 2 ** 20,
    wasmHeapCapBytes: 160 * 2 ** 20,
    stageBytes: 4 * 2 ** 20,
    journalIntervalBytes: 16 * 2 ** 20,
    journalMaxAgeMs: 4000,
    computeWorkers: 1,
  },
  balanced: {
    id: 'balanced', label: '均衡',
    budgetBytes: 384 * 2 ** 20,
    wasmHeapCapBytes: 288 * 2 ** 20,
    stageBytes: 4 * 2 ** 20,
    journalIntervalBytes: 32 * 2 ** 20,
    journalMaxAgeMs: 4000,
    computeWorkers: 1,
  },
  faster: {
    id: 'faster', label: '较快',
    budgetBytes: 768 * 2 ** 20,
    wasmHeapCapBytes: 576 * 2 ** 20,
    stageBytes: 4 * 2 ** 20,
    journalIntervalBytes: 64 * 2 ** 20,
    journalMaxAgeMs: 4000,
    computeWorkers: 1,
  },
};

/// Smallest working set the engine can run at all: 1 MiB host chunk +
/// codec canvas/coefficients + spilled index pages + journal buffers.
export const MIN_WORKING_SET_BYTES = 12 * 2 ** 20;

export function getProfile(id) {
  const p = PROFILES[id];
  if (!p) throw stError(ERROR_CODES.RESOURCE_PROFILE_INSUFFICIENT,
    `未知资源档位: ${id}`);
  return p;
}

/// Allocation-credit gate: refuse before allocating when the minimum
/// working set cannot fit the profile budget (never probe RAM by
/// allocating — the decision is arithmetic only).
export function assertProfileFeasible(profile) {
  if (profile.budgetBytes < MIN_WORKING_SET_BYTES) {
    throw stError(ERROR_CODES.RESOURCE_PROFILE_INSUFFICIENT,
      `档位 ${profile.label} 预算 ${profile.budgetBytes} B ` +
      `< 最小工作集 ${MIN_WORKING_SET_BYTES} B`);
  }
  const fixed = (profile.stageBytes || 0) + (profile.wasmHeapCapBytes || 0) + 4 * 2 ** 20;
  if (fixed > profile.budgetBytes) {
    throw stError(ERROR_CODES.RESOURCE_PROFILE_INSUFFICIENT,
      `档位 ${profile.label} 的固定缓冲（复制缓冲+wasm 堆上限）超出总预算`);
  }
  return true;
}

/// Default suggestion: deviceMemory is approximate and absent on many
/// browsers — unknown defaults to the saver profile (plan §4).
export function defaultProfileId(deviceMemory, hardwareConcurrency) {
  if (typeof deviceMemory !== 'number' || !(deviceMemory > 0)) return 'saver';
  if (deviceMemory <= 4) return 'saver';
  if (deviceMemory <= 8) return 'balanced';
  return 'faster';
}

// ---------------------------------------------------------- state machine --

export const STATES = [
  'selected', 'probing', 'planned', 'running', 'paused',
  'finalizing', 'validating', 'ready', 'exported',
  'failed', 'cancelled', 'cleanup_pending',
];

const TRANSITIONS = {
  selected: ['probing', 'cancelled'],
  probing: ['planned', 'failed', 'cancelled'],
  planned: ['running', 'cancelled'],
  running: ['paused', 'finalizing', 'failed', 'cancelled', 'cleanup_pending'],
  paused: ['running', 'cancelled', 'cleanup_pending'],
  finalizing: ['validating', 'failed', 'cancelled', 'cleanup_pending'],
  validating: ['ready', 'failed', 'cancelled', 'cleanup_pending'],
  ready: ['exported', 'exported' /* re-export allowed */],
  exported: ['exported'],
  failed: ['running' /* explicit retry after fix */],
  cancelled: [],
  cleanup_pending: ['cancelled'],
};

export function canTransition(from, to) {
  return !!TRANSITIONS[from] && TRANSITIONS[from].includes(to);
}

// ------------------------------------------------------------- checksum --

// Two decorrelated 32-bit FNV-1a lanes with Math.imul (fast per-byte JS;
// good enough for journal chunk checks — the strong whole-file hash is
// sha256 computed in the wasm core).
export function fnv2x32(u8) {
  let a = 0x811c9dc5 | 0;
  let b = 0x61b1 | 0; // second lane offset basis
  for (let i = 0; i < u8.length; i++) {
    const x = u8[i];
    a = Math.imul(a ^ x, 0x01000193);
    b = Math.imul(b ^ ((x + 0x9d) & 0xff), 0x85ebca6b);
  }
  return (a >>> 0).toString(16).padStart(8, '0') +
    (b >>> 0).toString(16).padStart(8, '0');
}

export function recordChecksum(line) {
  const m = line.match(/^(.*),"rc":"[0-9a-f]*"\}$/);
  const body = m ? `${m[1]}}` : line;
  return fnv2x32(new TextEncoder().encode(body));
}

// -------------------------------------------------------------- journal --

// Append-only JSON-lines journal. Records:
//   {"t":"gen","gen":G,"identity":{...},"core":...,"plan":...,"policy":...,
//    "profile":...,"cap":N|null,"resume":<state|null>}
//   {"t":"c","gen":G,"seq":n,"st":{"level":L,"channel":C,"cell":X,"out":N,
//                                  "ifds":[...]},
//    "chunks":[[len,"fnv2x32"],...],"rc":"..."}
// Every record ends with \n and carries an "rc" checksum; a torn/corrupt
// tail (crash mid-append) is discarded — only whole valid records count.
// A "gen" header supersedes everything before it (generation bump).
export function encodeJournalRecord(obj) {
  const body = JSON.stringify(obj);
  const rc = fnv2x32(new TextEncoder().encode(body));
  return `${body.slice(0, -1)},"rc":"${rc}"}\n`;
}

export function decodeJournal(text) {
  const records = [];
  let torn = false;
  let pos = 0;
  while (pos < text.length) {
    const nl = text.indexOf('\n', pos);
    if (nl === -1) { torn = text.slice(pos).trim().length > 0; break; }
    const line = text.slice(pos, nl);
    pos = nl + 1;
    if (!line.trim()) continue;
    let obj;
    try { obj = JSON.parse(line); } catch { torn = true; break; }
    if (typeof obj.rc !== 'string' || recordChecksum(line) !== obj.rc) {
      torn = true;
      break;
    }
    records.push(obj);
  }
  return { records, torn };
}

/// Reduce decoded records to the effective state: the LAST valid `gen`
/// header plus the commits after it (earlier generations superseded).
export function journalState(records) {
  let gen = null;
  let lastCommit = null;
  let seq = 0;
  for (const r of records) {
    if (r.t === 'gen') {
      gen = r;
      lastCommit = null;
      seq = 0;
    } else if (r.t === 'c' && gen && r.gen === gen.gen) {
      if (typeof r.seq === 'number' && r.seq === seq + 1) {
        seq = r.seq;
        lastCommit = r;
      } else {
        break; // gap: stop trusting the tail
      }
    }
  }
  return { gen, lastCommit, seq };
}

// -------------------------------------------------------- slot records --

// Dual-slot small-record store (job.json): alternating files with a gen
// counter + checksum; a torn write leaves the other slot authoritative.
export async function writeSlotRecord(dirHandle, base, obj) {
  const prev = await readSlotRecord(dirHandle, base);
  const gen = (prev && typeof prev.gen === 'number' ? prev.gen : 0) + 1;
  const slot = gen % 2 === 0 ? `${base}.b.json` : `${base}.a.json`;
  const body = JSON.stringify({ ...obj, gen });
  const text = `${body}\n${JSON.stringify({ rc: fnv2x32(new TextEncoder().encode(body)) })}\n`;
  const fh = await dirHandle.getFileHandle(slot, { create: true });
  const w = await fh.createWritable();
  await w.write(new TextEncoder().encode(text));
  await w.close();
  return gen;
}

export async function readSlotRecord(dirHandle, base) {
  const out = [];
  for (const slot of [`${base}.a.json`, `${base}.b.json`]) {
    try {
      const fh = await dirHandle.getFileHandle(slot);
      const file = await fh.getFile();
      if (file.size > 1 << 20) continue; // defensive cap for small records
      const text = await file.slice(0, 1 << 20).arrayBuffer();
      const str = new TextDecoder().decode(text);
      const nl = str.indexOf('\n');
      if (nl === -1) continue;
      let body, meta;
      try { body = JSON.parse(str.slice(0, nl)); meta = JSON.parse(str.slice(nl + 1).trim()); }
      catch { continue; }
      if (!meta || typeof meta.rc !== 'string') continue;
      if (fnv2x32(new TextEncoder().encode(str.slice(0, nl))) !== meta.rc) continue;
      out.push(body);
    } catch { /* absent slot */ }
  }
  if (!out.length) return null;
  out.sort((a, b) => (b.gen || 0) - (a.gen || 0));
  return out[0];
}

// ------------------------------------------------------------ OPFS util --

/// Retry helper for the reload race: a dying worker's sync handle can keep
/// the file locked for a moment → NotReadableError (C0 ADR §7). Backoff.
export async function withRetry(fn, { attempts = 20, delayMs = 400, name = 'opfs' } = {}) {
  let lastErr = null;
  for (let i = 0; i < attempts; i++) {
    try {
      return await fn();
    } catch (e) {
      lastErr = e;
      const n = String((e && e.name) || e);
      const retryable = /NotReadable|NoModification|InvalidState|UnknownError/i.test(n) ||
        /another one open|Access Handles cannot|quota/i.test(String((e && e.message) || ''));
      if (!retryable) throw e;
      await new Promise((r) => setTimeout(r, delayMs * (1 + Math.min(i, 5))));
    }
  }
  throw lastErr;
}

export async function removeEntryRecursive(dirHandle, name) {
  try {
    await dirHandle.removeEntry(name, { recursive: true });
    return true;
  } catch (e) {
    const n = String((e && e.name) || e);
    if (/NotFound/i.test(n)) return true; // already gone = success
    throw e;
  }
}

// ------------------------------------------------------- disk precheck --

/// estimate: {output_upper_bound_bytes, cells_total, tiles_present} from the
/// core probe; scratch/index ≈ 44 B/cell + 12 B/tile ×2 (index + offcnt) +
/// journal allowance; sourceBytes = the staged source copy when it is not
/// yet written; export peak = a second copy of the upper bound when
/// exporting to another OPFS file (showSaveFilePicker writes to user disk
/// and does not consume OPFS quota).
export function diskNeedBytes(estimate, { exporting = false, sourceBytes = 0 } = {}) {
  const e = estimate || {};
  const ub = Number(e.output_upper_bound_bytes || 0);
  const cells = Number(e.cells_total || 0);
  const tiles = Number(e.tiles_present || 0);
  const scratch = cells * 44 + tiles * 12 * 2 + 8 * 2 ** 20;
  const journal = 32 * 2 ** 20;
  const exportPeak = exporting ? ub : 0;
  const source = Number(sourceBytes || 0);
  return { source, output: ub, scratch, journal, exportPeak,
    total: source + ub + scratch + journal + exportPeak };
}

/// Chromium reports quota as usage + min(real availability, 10 GiB) (C0 ADR
/// §8: 0→10, 9→19, 12→22 GiB), so a reported headroom at that cap is only a
/// lower bound. Below the cap the number reflects a real limit and a
/// shortfall is refused; at the cap a larger need is `uncertain` — the
/// caller must get explicit user confirmation, and per-write quota errors
/// stay recoverable either way.
export const REPORTED_HEADROOM_CAP = 10 * 2 ** 30;

export function checkDiskBudget(estimate, storageEstimate, opts = {}) {
  const need = diskNeedBytes(estimate, opts);
  if (!storageEstimate || typeof storageEstimate.quota !== 'number') {
    return { ok: false, uncertain: true, need, available: null };
  }
  const available = Math.max(0, storageEstimate.quota - (storageEstimate.usage || 0));
  if (available >= need.total) return { ok: true, uncertain: false, need, available };
  const capped = available >= REPORTED_HEADROOM_CAP - 2 ** 20;
  return { ok: false, uncertain: capped, need, available };
}

/// Supported container signatures (the core decides the variant; this only
/// avoids staging a multi-GiB copy of a file the core can never read).
export const SUPPORTED_MAGICS = [
  [0xF1, 0x01, 0xEE, 0xEE, 0x4B, 0x46, 0x42, 0x00], // KFB
  [0xF1, 0x01, 0xEE, 0xEE, 0x4B, 0x46, 0x42, 0x46], // KFBF
];

export function magicSupported(head) {
  return SUPPORTED_MAGICS.some((m) => m.every((b, i) => head[i] === b));
}

// ---------------------------------------------------------------- misc --

export function nowIso() {
  return new Date().toISOString();
}

export function newJobId() {
  const c = globalThis.crypto;
  const r = c && c.getRandomValues ? c.getRandomValues(new Uint8Array(8)) : null;
  const hex = r ? [...r].map((b) => b.toString(16).padStart(2, '0')).join('') : String(Date.now());
  return `job-${hex}`;
}

export const JOB_ROOT = 'slide-jobs';
export const PENDING_CLEANUP = 'pending-cleanup.json';
export const JOURNAL_FILE = 'journal.jsonl';
export const OUTPUT_NAME = 'output.tif';
export const SOURCE_NAME = 'source.bin';
export const STAGE_CHUNK = 4 * 2 ** 20;
export const JOURNAL_ROTATE_BYTES = 8 * 2 ** 20;
