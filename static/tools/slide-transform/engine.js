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
  // C4：上传进行中（分块传输/等待服务端阶段）拒绝删除任务——本地产物在
  // 上传收口前必须可继续读取，删除即丢失唯一副本。
  UPLOAD_ACTIVE: 'upload_active',
};

// C4：upload 记录视为「进行中」的状态（活跃 = 删除被拒；页面据此禁用
// discard 并展示继续上传）。published/failed/cancelled 是收口态。
export const UPLOAD_ACTIVE_STATES = [
  'created', 'waiting', 'uploading', 'awaiting_server', 'downloading',
  'validating', 'processing', 'readiness', 'interrupted',
];

/// Held (Web Lock) by whichever tab is uploading this job's artifact; a
/// discard only proceeds when it can take the same lock.
export function uploadLockName(jobId) {
  return `slide-transform:upload:${jobId}`;
}

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

/// F1: TIFF/BigTIFF container headers (classic II*\0 / MM\0*, BigTIFF
/// II+\0 / MM\0+). The first-IFD offset differs per file, so these are
/// 4-byte prefixes, not full 8-byte magics.
export const TIFF_HEADER_PREFIXES = [
  [0x49, 0x49, 0x2A, 0x00],
  [0x4D, 0x4D, 0x00, 0x2A],
  [0x49, 0x49, 0x2B, 0x00],
  [0x4D, 0x4D, 0x00, 0x2B],
];

export function isTiffHeader(head) {
  if (!head || head.length < 4) return false;
  return TIFF_HEADER_PREFIXES.some((m) => m.every((b, i) => head[i] === b));
}

export function magicSupported(head) {
  if (isTiffHeader(head)) return true; // staged only after sniffTiffSlideCapability
  return SUPPORTED_MAGICS.some((m) => m.every((b, i) => head[i] === b));
}

/// Container magic → modality ('brightfield' KFB | 'fluorescence' KFBF;
/// null = not a supported container). TIFF containers convert through the
/// brightfield SVS adapter (the bounded sniff rejects anything else before
/// staging), so they map to 'brightfield'. The core decides the variant from
/// the same magic, so the page can offer the brightfield output-format choice
/// (or withhold it for fluorescence) before the copy+probe round-trip.
export function magicModality(head) {
  if (SUPPORTED_MAGICS[1].every((b, i) => head[i] === b)) return 'fluorescence';
  if (SUPPORTED_MAGICS[0].every((b, i) => head[i] === b)) return 'brightfield';
  if (isTiffHeader(head)) return 'brightfield';
  return null;
}

// ------------------------------------------------- input capability (F1) --

/// Bounded structural probe of a TIFF container BEFORE staging (input
/// capability helper — deliberately separate from the conversion engine;
/// the authoritative capability report still comes from the wasm core's
/// probe on the staged copy). Reads at most ~78 KiB: header, one IFD entry
/// table (≤512 entries) and one bounded description value. Mirrors the
/// core's accept rule for IFD 0: tiled, baseline JPEG (not JPEG 2000),
/// chunky, 3 samples, Aperio description.
///
///   await sniffTiffSlideCapability(file)
///     → { supported: true, modality: 'brightfield', format: 'aperio-svs-jpeg',
///         adapter: 'aperio-svs-jpeg' }
///     | { supported: false, modality: null, reason: '<typed reason>' }
export const SVS_SOURCE_ADAPTER = 'aperio-svs-jpeg';
const SNIFF_MAX_ENTRIES = 512;
const SNIFF_DESC_MAX = 64 * 2 ** 10;

export async function sniffTiffSlideCapability(file) {
  const bad = (reason) => ({ supported: false, modality: null, reason });
  const readAt = async (off, len) =>
    new Uint8Array(await file.slice(off, off + len).arrayBuffer());
  let head;
  try {
    head = await readAt(0, 16);
  } catch (e) {
    return bad(`无法读取文件头：${errText(e)}`);
  }
  if (head.length < 8) return bad('文件小于 8 字节，不是 TIFF');
  if (!isTiffHeader(head)) return bad('不是 TIFF/BigTIFF 容器');
  const little = head[0] === 0x49 && head[1] === 0x49;
  const u16 = (b, at) => (little ? b[at] | (b[at + 1] << 8) : (b[at] << 8) | b[at + 1]);
  const u32 = (b, at) => little
    ? (b[at] | (b[at + 1] << 8) | (b[at + 2] << 16) | (b[at + 3] << 24)) >>> 0
    : (((b[at] << 24) | (b[at + 1] << 16) | (b[at + 2] << 8) | b[at + 3]) >>> 0);
  const bigtiff = u16(head, 2) === 43;
  if (bigtiff && (u16(head, 4) !== 8 || u16(head, 6) !== 0)) {
    return bad('BigTIFF 头部异常（offset size ≠ 8 或保留位非 0）');
  }
  if (file.size < (bigtiff ? 16 : 8)) return bad('文件头不完整');
  let ifdAt = bigtiff
    ? (little
      ? u32(head, 8) + u32(head, 12) * 2 ** 32
      : u32(head, 8) * 2 ** 32 + u32(head, 12))
    : u32(head, 4);
  if (!isSafeOffset(ifdAt) || ifdAt === 0) return bad('首个 IFD 偏移非法');
  const esize = bigtiff ? 20 : 12;
  try {
    const cb = await readAt(ifdAt, bigtiff ? 8 : 2);
    const n = bigtiff
      ? (little ? u32(cb, 0) + u32(cb, 4) * 2 ** 32 : u32(cb, 0) * 2 ** 32 + u32(cb, 4))
      : u16(cb, 0);
    if (n === 0 || n > SNIFF_MAX_ENTRIES) return bad(`IFD 条目数 ${n} 异常`);
    const tableLen = n * esize + (bigtiff ? 8 : 2) + (bigtiff ? 8 : 4);
    if (ifdAt + tableLen > file.size) return bad('IFD 条目表越界');
    const t = await readAt(ifdAt, tableLen);
    const base = bigtiff ? 8 : 2;
    const entries = {};
    for (let i = 0; i < n; i++) {
      const e = base + i * esize;
      const tag = u16(t, e);
      const typ = u16(t, e + 2);
      const count = bigtiff
        ? (little ? u32(t, e + 4) + u32(t, e + 8) * 2 ** 32 : u32(t, e + 4) * 2 ** 32 + u32(t, e + 8))
        : u32(t, e + 4);
      let val = t.subarray(e + (bigtiff ? 12 : 8), e + esize);
      entries[tag] = { typ, count, val };
    }
    const scalar = (tag) => {
      const en = entries[tag];
      if (!en) return undefined;
      if (en.typ === 3) return u16(en.val, 0);
      if (en.typ === 4) return u32(en.val, 0);
      return undefined;
    };
    const descBytes = async () => {
      const en = entries[270];
      if (!en) return '';
      const len = Math.min(en.count, SNIFF_DESC_MAX);
      const inline = bigtiff ? 8 : 4;
      if (len <= inline) return new TextDecoder().decode(en.val.subarray(0, len));
      const off = bigtiff
        ? (little ? u32(en.val, 0) + u32(en.val, 4) * 2 ** 32 : u32(en.val, 0) * 2 ** 32 + u32(en.val, 4))
        : u32(en.val, 0);
      return new TextDecoder().decode(await readAt(off, len));
    };
    if (!entries[322] || !entries[323]) return bad('主图不是分块（tiled）存储：该 TIFF 变体不在支持集');
    const comp = scalar(259);
    if (comp === 33003 || comp === 33005) {
      return bad('JPEG 2000 压缩不在当前支持集（需要独立解码器）');
    }
    if (comp !== 7) return bad(`压缩编码 ${comp} 不是基线 JPEG，无法按原样搬运`);
    const spp = scalar(277) ?? 1;
    if (spp !== 3) return bad(`SamplesPerPixel=${spp}（荧光/多通道或灰度页组不在明场支持集）`);
    const planar = scalar(284) ?? 1;
    if (planar !== 1) return bad(`PlanarConfiguration=${planar}（平面存储）不在支持集`);
    const photo = scalar(262);
    if (photo !== 2 && photo !== 6) return bad(`PhotometricInterpretation=${photo} 不在支持集（RGB=2 / YCbCr=6）`);
    const desc = await descBytes();
    if (!desc.includes('Aperio')) {
      return bad('TIFF 结构合法但未标识 Aperio：未知厂商变体不猜');
    }
    return {
      supported: true,
      modality: 'brightfield',
      format: SVS_SOURCE_ADAPTER,
      adapter: SVS_SOURCE_ADAPTER,
      bigtiff,
      littleEndian: little,
    };
  } catch (e) {
    return bad(`结构探测失败：${errText(e)}`);
  }
}

// ------------------------------------------------------ output profiles --

/// Output layouts the core can write (`--profile` ids; persisted in job
/// records as `outputProfile` and in every journal generation).
export const OUTPUT_PROFILES = {
  BF_CLASSIC: 'bf-classic', // classic multi-IFD JPEG BigTIFF (.tif)
  BF_OME: 'bf-ome',         // RGB OME-BigTIFF, SubIFD pyramid (.ome.tif)
  FL_OME: 'fl-ome',         // multichannel OME-BigTIFF (.ome.tif)
};

/// Core `result.format` → output profile.
export const FORMAT_PROFILES = {
  'classic-bigtiff-jpeg-pyramid': OUTPUT_PROFILES.BF_CLASSIC,
  'ome-bigtiff-subifd-rgb-jpeg-pyramid': OUTPUT_PROFILES.BF_OME,
  'ome-bigtiff-subifd-multichannel-jpeg-passthrough': OUTPUT_PROFILES.FL_OME,
};

/// Profile for a NEW job of `modality`.
export function defaultOutputProfile(modality) {
  return modality === 'fluorescence' ? OUTPUT_PROFILES.FL_OME : OUTPUT_PROFILES.BF_OME;
}

/// Profile a job record was written with. Records without the field
/// predate output profiles: their (partial) outputs are classic brightfield
/// or fluorescence OME — never reinterpret them as the new default.
export function recordOutputProfile(rec) {
  if (rec && rec.outputProfile) return rec.outputProfile;
  return rec && rec.modality === 'fluorescence'
    ? OUTPUT_PROFILES.FL_OME : OUTPUT_PROFILES.BF_CLASSIC;
}

export function profileFitsModality(profile, modality) {
  if (modality === 'fluorescence') return profile === OUTPUT_PROFILES.FL_OME;
  return profile === OUTPUT_PROFILES.BF_CLASSIC || profile === OUTPUT_PROFILES.BF_OME;
}

/// Output profile of a finished/summarized job: the core-reported format
/// wins; then the recorded profile; then the legacy default for modality.
export function jobOutputProfile(job) {
  const fmt = job && job.result && job.result.format;
  if (fmt && FORMAT_PROFILES[fmt]) return FORMAT_PROFILES[fmt];
  if (job && job.outputProfile) return job.outputProfile;
  return recordOutputProfile(job);
}

export function isOmeProfile(profile) {
  return profile === OUTPUT_PROFILES.BF_OME || profile === OUTPUT_PROFILES.FL_OME;
}

/// Local/uploaded file name for a job's artifact: `.ome.tif` for both OME
/// profiles (Bio-Formats and the platform registry key OME on it), `.tif`
/// for the classic pyramid.
export function outputFileName(sourceName, job) {
  const base = String(sourceName || 'slide').replace(/\.(kfb|kfbf)$/i, '') || 'slide';
  return isOmeProfile(jobOutputProfile(job)) ? `${base}.ome.tif` : `${base}.tif`;
}

/// showSaveFilePicker `types` for a job's artifact (the suggested
/// `.ome.tif` name ends in `.tif`, so it matches the accept list as is).
export function saveFileTypes(job) {
  return [{
    description: isOmeProfile(jobOutputProfile(job)) ? 'OME-TIFF' : 'TIFF',
    accept: { 'image/tiff': ['.tif', '.tiff'] },
  }];
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
