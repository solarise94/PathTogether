// runner.js — C2 page façade over the compute worker (engine.js + worker.js).
// Replaces the C1 placeholder runner: production API for the future /tools
// page (C3) and for the C2 test harness. ES module, no dependencies.
//
// Public API (the only surface the /tools page may use):
//
//   const runner = await SlideToolsRunner.create();
//   // sniff 8-byte header → copy File into OPFS → full probe on the copy →
//   // disk gate → job recorded as `prepared`
//   const prep = await runner.probe(file, { confirmUncertainDisk, channelJson });
//        // → { jobId, probe, identity: { size, sha256 } }
//   // channelJson (≤1 MiB string) is saved on the prepared record so a
//   // start after a page refresh keeps it; replace it before starting with
//   await runner.setPreparedChannelJson(prep.jobId, channelJson | null);
//   // the FIRST argument is still the File; passing a `prepared` jobId
//   // reuses its copy (no second copy), otherwise probe() runs first.
//   // channelJson: undefined = the saved one, null = none, string = this one
//   const { jobId, done } = await runner.startJob(file, {
//     jobId: prep.jobId, profileId, policy, outputCapBytes, channelJson,
//     confirmUncertainDisk });
//   // resume never needs the File; omitted settings default to the saved
//   // ones, explicitly different ones are refused (`resume_refused`)
//   const { done } = await runner.resumeJob(jobId);
//   const jobs = await runner.listJobs();   // [JobSummary], see _summary()
//   const job  = await runner.getJob(jobId); // JobSummary | null
//   await runner.cancelJob();                // the running job of this tab
//   await runner.discardJob(jobId, { abandonUpload }); // delete copy + artifact
//   await runner.exportJob(jobId, () => savePicker.createWritable());
//   // C4 upload hookup: merge a patch into record.upload (serialized per tab,
//   // same slot record discipline as every other record change). The page owns
//   // the shape; recommended: {ingestionId, filename, size, state,
//   // confirmedParts, slideId, updatedAt, error}. discardJob refuses with a
//   // typed `upload_active` while a tab holds E.uploadLockName(jobId), and for
//   // a leftover non-terminal record unless { abandonUpload: true }.
//   await runner.setJobUpload(jobId, patch);
//
// Disk gate: `disk_precheck_failed` with `uncertain: true` means the
// browser's quota report is capped (usage + 10 GiB) and cannot prove the
// space either way — ask the user, then retry with confirmUncertainDisk.
//
// State machine (plan §5):
//   selected → probing → planned → running ↔ paused → finalizing →
//   validating → ready → exported, plus failed / cancelled /
//   cleanup_pending. Only `ready` results are exportable.
//
// Source staging (plan §5 decision 2026-09-29): before probing, the File is
// streamed into the job's OPFS `source.bin`; everything after reads that
// copy. Resume therefore needs no re-selected file, only a verified copy.

import * as E from './engine.js';

const LOCK_HEAVY = 'slide-transform:heavy';

/// Settings a resumed run must match (see _startOrResume refusals).
function savedSettings(rec) {
  return {
    profileId: rec.profile || undefined,
    policy: rec.policy || 'allow-edge',
    outputCapBytes: rec.cap || undefined,
    channelJson: rec.channelJson || undefined,
  };
}
const EXPORT_CHUNK = 4 * 2 ** 20;
const CHANNEL_JSON_MAX = 2 ** 20;

function checkedChannelJson(v) {
  if (v === undefined || v === null || v === '') return null;
  if (typeof v !== 'string' || v.length > CHANNEL_JSON_MAX) {
    throw E.stError(E.ERROR_CODES.IO_RECOVERABLE, 'channel.json 须为不超过 1 MiB 的文本');
  }
  return v;
}

function channelJsonHash(v) {
  return v ? E.fnv2x32(new TextEncoder().encode(v)) : null;
}

export class SlideToolsRunner {
  static async create(opts = {}) {
    const r = new SlideToolsRunner(opts);
    await r._init();
    return r;
  }

  constructor(opts = {}) {
    this.opts = opts;
    this.worker = null;
    this.workerReady = null;
    this.jobId = null;
    this._state = null;
    this._cbs = { state: [], progress: [], fault: [], phase: [], done: [], workerExit: [] };
    this._lock = null;
    this._cancelled = false;
    this.testMode = !!opts.testMode;
  }

  on(kind, cb) { this._cbs[kind].push(cb); return this; }
  _emit(kind, payload) { for (const cb of this._cbs[kind]) cb(payload); }
  get state() { return this._state; }

  async _init() {
    await this._processPendingCleanups();
    await this._sweepIncompleteStaging();
    if (this.opts.spawnWorker !== false) {
      await this._spawnWorker(); // coreVersion known before any job
    }
  }

  // ------------------------------------------------------------ worker --

  _spawnWorker() {
    if (this.worker) return this.workerReady;
    const url = new URL('./worker.js', import.meta.url);
    const w = new Worker(url, { type: 'module' });
    this.worker = w;
    this.workerReady = new Promise((resolve, reject) => {
      const t = setTimeout(() => reject(new Error('worker init timeout')), 20000);
      w.addEventListener('message', (ev) => {
        const m = ev.data;
        if (m.type === 'ready') {
          clearTimeout(t);
          this.coreVersion = m.coreVersion;
          resolve(m);
        }
      });
      w.addEventListener('error', (e) => {
        this._rejectPending(`worker error: ${e.message}`);
        this._emit('workerExit', { reason: 'error', message: e.message });
        this.worker = null;
        this.workerReady = null;
      });
      w.addEventListener('messageerror', () => this._emit('workerExit', { reason: 'messageerror' }));
    });
    w.addEventListener('message', (ev) => this._onWorkerMessage(ev.data));
    w.postMessage({ type: 'init', testMode: this.testMode });
    return this.workerReady;
  }

  async _request(type, payload, timeoutMs = 15 * 60 * 1000) {
    await this._spawnWorker();
    const id = `r${Math.random().toString(36).slice(2)}`;
    return new Promise((resolve, reject) => {
      const t = setTimeout(() => {
        this._pending.delete(id);
        reject(new Error(`${type} timeout`));
      }, timeoutMs);
      this._pending = this._pending || new Map();
      this._pending.set(id, { resolve, reject, t });
      this.worker.postMessage({ id, type, ...payload });
    });
  }

  /// A terminated worker never answers: fail every in-flight request now.
  _rejectPending(reason) {
    if (!this._pending) return;
    for (const [id, p] of this._pending) {
      clearTimeout(p.t);
      p.reject(Object.assign(new Error(reason),
        E.stError(E.ERROR_CODES.IO_RECOVERABLE, reason)));
      this._pending.delete(id);
    }
  }

  _onWorkerMessage(m) {
    if (m.type === 'reply') {
      const p = this._pending && this._pending.get(m.id);
      if (p) {
        clearTimeout(p.t);
        this._pending.delete(m.id);
        m.ok ? p.resolve(m.result) : p.reject(Object.assign(
          new Error(`worker error: ${JSON.stringify(m.result).slice(0, 300)}`), m.result));
      }
      return;
    }
    if (m.type === 'progress') { this._emit('progress', m.progress); return; }
    if (m.type === 'phase') { this._emit('phase', m); return; }
    if (m.type === 'dbg') { this._emit('phase', { phase: 'dbg', ...m.counters }); return; }
    if (m.type === 'state') {
      if (m.state === 'running') this._setState('running', { source: 'worker' });
      if (m.state === 'finalizing') this._setState('finalizing');
      if (m.state === 'validating') this._setState('validating');
      return;
    }
    if (m.type === 'fault-reached') { this._emit('fault', m); return; }
    if (m.type === 'cancelled') {
      this._onCancelled(m);
      return;
    }
    if (m.type === 'done') {
      this._onDone(m);
      return;
    }
    if (m.type === 'log') { /* verbose; harness may observe */ }
  }

  // ---------------------------------------------------------- OPFS I/O --

  async _jobsDir(create = false) {
    const root = await navigator.storage.getDirectory();
    return root.getDirectoryHandle(E.JOB_ROOT, { create });
  }

  async _jobDir(jobId, create = false) {
    const jobs = await this._jobsDir(true);
    return E.withRetry(() => jobs.getDirectoryHandle(jobId, { create }), { name: 'job dir' });
  }

  async _readJobRecord(jobId) {
    const dir = await this._jobDir(jobId);
    return E.readSlotRecord(dir, 'job');
  }

  /// Record writes of this tab run one at a time: two interleaved
  /// read-merge-writes would both take the same gen and one patch would be lost.
  _serialRecord(fn) {
    const run = (this._recordChain || Promise.resolve()).then(fn, fn);
    this._recordChain = run.catch(() => {});
    return run;
  }

  /// Slot records replace wholesale — patches must read-merge-write.
  async _updateJobRecord(jobId, patch) {
    return this._serialRecord(async () => {
      const prev = (await this._readJobRecord(jobId)) || {};
      const next = { ...prev, ...patch, updatedAt: E.nowIso() };
      for (const k of Object.keys(next)) {
        if (next[k] === undefined) delete next[k];
      }
      await this._writeJobRecordNow(jobId, next);
      return next;
    });
  }

  async _writeJobRecord(jobId, obj) {
    return this._serialRecord(() => this._writeJobRecordNow(jobId, obj));
  }

  async _writeJobRecordNow(jobId, obj) {
    const dir = await this._jobDir(jobId, true);
    return E.writeSlotRecord(dir, 'job', { v: 1, id: jobId, ...obj });
  }

  async _readJournal(jobId) {
    const dir = await this._jobDir(jobId);
    const fh = await E.withRetry(() => dir.getFileHandle(E.JOURNAL_FILE));
    const file = await fh.getFile();
    if (file.size > 64 * 2 ** 20) {
      throw E.stError(E.ERROR_CODES.IO_RECOVERABLE, 'journal 异常增大');
    }
    const text = new TextDecoder().decode(await file.slice(0, file.size).arrayBuffer());
    const { records, torn } = E.decodeJournal(text);
    return { records, torn, state: E.journalState(records) };
  }

  // ----------------------------------------------------- pending cleanups --

  async _readRegistry() {
    try {
      const jobs = await this._jobsDir();
      const fh = await jobs.getFileHandle(E.PENDING_CLEANUP);
      const f = await fh.getFile();
      const text = new TextDecoder().decode(await f.slice(0, 1 << 20).arrayBuffer());
      return JSON.parse(text);
    } catch { return { jobs: [] }; }
  }

  async _writeRegistry(reg) {
    const jobs = await this._jobsDir(true);
    const fh = await jobs.getFileHandle(E.PENDING_CLEANUP, { create: true });
    const w = await fh.createWritable();
    await w.write(new TextEncoder().encode(JSON.stringify(reg)));
    await w.close();
  }

  async _addPendingCleanup(jobId, lastError) {
    const reg = await this._readRegistry();
    if (!reg.jobs.some((j) => j.id === jobId)) {
      reg.jobs.push({ id: jobId, attempts: 0, lastError, at: E.nowIso() });
      await this._writeRegistry(reg);
    }
  }

  async _processPendingCleanups() {
    const reg = await this._readRegistry();
    const remain = [];
    for (const j of reg.jobs) {
      try {
        const jobs = await this._jobsDir();
        await E.removeEntryRecursive(jobs, j.id);
      } catch (e) {
        remain.push({ ...j, attempts: (j.attempts || 0) + 1, lastError: E.errText(e) });
      }
    }
    if (remain.length !== reg.jobs.length || remain.length === 0) {
      await this._writeRegistry({ jobs: remain });
    }
    return { processed: reg.jobs.length - remain.length, remaining: remain.length };
  }

  /// A crash during staging leaves a job dir whose record is missing or still
  /// `staging`; it can never be resumed. Only swept while this tab can take
  /// the heavy lock (no other tab is staging right now).
  async _sweepIncompleteStaging() {
    if (!(await this._acquireHeavyLock())) return { swept: 0, skipped: 'locked' };
    let swept = 0;
    try {
      let jobs;
      try { jobs = await this._jobsDir(); } catch { return { swept: 0 }; }
      const victims = [];
      for await (const [name, handle] of jobs.entries()) {
        if (handle.kind !== 'directory' || name.startsWith('.')) continue;
        let rec = null;
        try { rec = await E.readSlotRecord(handle, 'job'); } catch { rec = null; }
        // C4: a job with an upload record is never sweepable — the record is the
        // only durable link to a server-side ingestion still referencing these
        // bytes (upload only starts from ready, so this is defensive).
        if (rec && rec.upload) continue;
        if (!rec || rec.state === 'staging') victims.push(name);
      }
      for (const name of victims) {
        try { await E.removeEntryRecursive(jobs, name); swept += 1; } catch (e) {
          await this._addPendingCleanup(name, E.errText(e));
        }
      }
    } finally {
      this._releaseHeavyLock();
    }
    return { swept };
  }

  // ------------------------------------------------------------ locking --

  async _acquireHeavyLock() {
    if (this._lock) return true;
    return new Promise((resolve) => {
      navigator.locks.request(LOCK_HEAVY, { ifAvailable: true }, (lock) => {
        if (!lock) {
          resolve(false);
          return;
        }
        this._lock = lock;
        resolve(true);
        // hold the lock until the job reaches a terminal state
        return new Promise((release) => { this._lockRelease = release; });
      }).catch(() => resolve(false));
    });
  }

  _releaseHeavyLock() {
    if (this._lockRelease) { this._lockRelease(); this._lockRelease = null; }
    this._lock = null;
  }

  // ------------------------------------------------------------- states --

  _setState(to, extra = {}) {
    if (this._state === to) return;
    if (this._state && !E.canTransition(this._state, to)) {
      // observed transitions used by the runner are all legal; a skip means
      // an event arrived out of order — record it loudly instead of hiding
      this._emit('state', { from: this._state, to, ...extra, skippedCheck: true });
    }
    this._state = to;
    this._emit('state', { from: null, to, ...extra });
  }

  // -------------------------------------------------------------- probe --

  /// Stage + probe (the plan's 选择输入 → 探测 step). Leaves a `prepared` job
  /// that startJob({jobId}) converts, or discardJob() removes.
  async probe(file, opts = {}) {
    if (!(await this._acquireHeavyLock())) {
      throw E.stError(E.ERROR_CODES.JOB_LOCKED, '另一个标签页正在执行重转换任务（Web Lock）');
    }
    try {
      this._state = null;
      this._setState('selected');
      const prep = await this._prepare(file, opts);
      this._setState('planned', { probe: prep.probe });
      return prep;
    } finally {
      this._releaseHeavyLock();
    }
  }

  /// Replace the companion saved on a job that has not started yet.
  async setPreparedChannelJson(jobId, channelJson) {
    const cj = checkedChannelJson(channelJson);
    await this._serialRecord(async () => {
      const rec = await this._readJobRecord(jobId);
      if (!rec || rec.state !== 'prepared') {
        throw E.stError(E.ERROR_CODES.RESUME_REFUSED, '任务已开始，不能再更换 channel.json',
          { kind: 'channel-json' });
      }
      await this._writeJobRecordNow(jobId, {
        ...rec, channelJson: cj, channelJsonHash: channelJsonHash(cj), updatedAt: E.nowIso(),
      });
    });
  }

  /// C4 upload record: merge `patch` into record.upload (read-merge-write on
  /// the serialized record chain — the page must not write the slot itself).
  /// Only ready/exported jobs accept an upload record: the artifact this
  /// record refers to must exist.
  async setJobUpload(jobId, patch) {
    if (!patch || typeof patch !== 'object') {
      throw E.stError(E.ERROR_CODES.IO_RECOVERABLE, 'upload 记录须为对象');
    }
    await this._serialRecord(async () => {
      const rec = await this._readJobRecord(jobId);
      if (!rec || (rec.state !== 'ready' && rec.state !== 'exported')) {
        throw E.stError(E.ERROR_CODES.NOT_READY,
          `任务状态 ${rec ? rec.state : 'missing'} 不可挂上传记录（仅 ready/exported）`);
      }
      const prev = rec.upload || {};
      await this._writeJobRecordNow(jobId, {
        ...rec,
        upload: { ...prev, ...patch, updatedAt: E.nowIso() },
      });
    });
  }

  /// An upload in any tab holds E.uploadLockName(jobId) and reads the OPFS
  /// artifact as its only source, so discard is refused while that lock is
  /// held. A non-terminal upload record without a lock holder is a leftover
  /// of a closed tab: it is refused too (`lockHeld: false`) unless the
  /// caller passes { abandonUpload: true } after asking the user.
  async discardJob(jobId, opts = {}) {
    return navigator.locks.request(E.uploadLockName(jobId), { ifAvailable: true },
      async (lock) => {
        const rec = await this._readJobRecord(jobId).catch(() => null);
        const up = rec && rec.upload;
        const pending = !!(up && E.UPLOAD_ACTIVE_STATES.includes(up.state));
        if (!lock || (pending && !opts.abandonUpload)) {
          throw E.stError(E.ERROR_CODES.UPLOAD_ACTIVE,
            `上传进行中（${up ? up.state : 'uploading'}），删除会丢失本地产物；请先取消或等待收口`,
            { uploadState: up ? up.state : null, lockHeld: !lock });
        }
        await this._discardNow(jobId);
      });
  }

  async _discardNow(jobId) {
    await this._request('release-source', {});
    const jobs = await this._jobsDir();
    try {
      await E.withRetry(() => E.removeEntryRecursive(jobs, jobId), { attempts: 12, delayMs: 500 });
    } catch (e) {
      await this._addPendingCleanup(jobId, E.errText(e));
    }
  }

  _diskGate(chk, opts, stage) {
    if (chk.ok) return;
    const mib = (n) => Math.round(n / 2 ** 20);
    if (chk.uncertain && opts.confirmUncertainDisk) return;
    throw E.stError(E.ERROR_CODES.DISK_PRECHECK_FAILED,
      chk.uncertain
        ? `浏览器只报告了 ~${mib(chk.available || 0)} MiB 可用额度（报告上限），` +
          `本任务预计需要 ~${mib(chk.need.total)} MiB；需用户确认后继续（${stage}）`
        : `空间不足：需要 ~${mib(chk.need.total)} MiB，可用 ~${mib(chk.available)} MiB（${stage}）`,
      { need: chk.need, available: chk.available, uncertain: !!chk.uncertain, stage });
  }

  /// sniff → pre-stage disk gate → stage (copy + sha256) → probe the copy →
  /// estimate-based disk gate → `prepared` record.
  async _prepare(file, opts) {
    const head = new Uint8Array(await file.slice(0, 8).arrayBuffer());
    if (!E.magicSupported(head)) {
      throw E.stError(E.ERROR_CODES.UNSUPPORTED_INPUT, '不是本工具支持的 KFB/KFBF 文件（文件头不符）');
    }
    this._setState('probing');
    const channelJson = checkedChannelJson(opts.channelJson);
    const jobId = opts.jobId || E.newJobId();
    this.jobId = jobId;
    // before staging: the copy itself plus an output of about the same size
    this._diskGate(E.checkDiskBudget({ output_upper_bound_bytes: file.size },
      await navigator.storage.estimate(), { sourceBytes: file.size }), opts, 'pre-stage');
    await this._writeJobRecord(jobId, { state: 'staging', createdAt: E.nowIso(), updatedAt: E.nowIso() });
    let staged;
    let probeResult;
    try {
      staged = await this._request('stage-source',
        { jobId, file, faults: this.testMode ? (opts.faults || null) : null }, 60 * 60 * 1000);
      probeResult = await this._request('probe', { jobId }, 60 * 60 * 1000);
      if (probeResult.error) throw probeResult;
      const estimate = probeResult.document.estimate || probeResult.estimate;
      this._diskGate(E.checkDiskBudget(estimate, await navigator.storage.estimate()), opts, 'post-probe');
    } catch (e) {
      await this._discardNow(jobId).catch(() => { /* pending-cleanup recorded */ });
      throw e;
    }
    const doc = probeResult.document;
    await this._writeJobRecord(jobId, {
      state: 'prepared',
      // NOTE: the plain file name stays local (OPFS job record); reports
      // and logs must use aliases.
      identity: { name: file.name, size: staged.size, lastModified: file.lastModified, sha256: staged.sha256 },
      core: this.coreVersion,
      estimate: doc.estimate || probeResult.estimate,
      modality: doc.modality,
      channelJson,
      channelJsonHash: channelJsonHash(channelJson),
      createdAt: E.nowIso(),
      updatedAt: E.nowIso(),
    });
    return { jobId, probe: probeResult, identity: { size: staged.size, sha256: staged.sha256 } };
  }

  // ----------------------------------------------------------- job start --

  async startJob(file, opts = {}) {
    if (!(await this._acquireHeavyLock())) {
      throw E.stError(E.ERROR_CODES.JOB_LOCKED,
        '另一个标签页正在执行重转换任务（Web Lock）');
    }
    try {
      return await this._startOrResume(file, opts, null);
    } catch (e) {
      this._releaseHeavyLock();
      throw e;
    }
  }

  async resumeJob(jobId, _file = null, opts = {}) {
    if (!(await this._acquireHeavyLock())) {
      throw E.stError(E.ERROR_CODES.JOB_LOCKED,
        '另一个标签页正在执行重转换任务（Web Lock）');
    }
    try {
      let saved = {};
      try {
        const rec = await this._readJobRecord(jobId);
        if (rec) saved = savedSettings(rec);
      } catch { /* missing dir is reported by _startOrResume */ }
      const explicit = Object.fromEntries(
        Object.entries(opts).filter(([, v]) => v !== undefined));
      return await this._startOrResume(null, { ...saved, ...explicit }, jobId);
    } catch (e) {
      this._releaseHeavyLock();
      throw e;
    }
  }

  async _startOrResume(file, opts, resumeJobId) {
    this._state = 'selected'; // a new job lifecycle begins
    this._cancelled = false;
    const profile = E.getProfile(opts.profileId ||
      E.defaultProfileId(navigator.deviceMemory, navigator.hardwareConcurrency));
    E.assertProfileFeasible(profile);
    const policy = opts.policy || 'allow-edge';

    // ---- fresh: reuse a prepared job or stage+probe now
    let jobId = resumeJobId || opts.jobId || null;
    let record = null;
    if (jobId) {
      try {
        record = await this._readJobRecord(jobId);
      } catch (e) {
        throw E.stError(E.ERROR_CODES.JOB_DIR_MISSING,
          `任务目录缺失或不可读（可能被浏览器驱逐/删除）：${E.errText(e)}`);
      }
      if (!record) {
        throw E.stError(E.ERROR_CODES.JOB_DIR_MISSING, '任务记录缺失（目录被删除/驱逐）');
      }
    }
    // a staged copy whose run never journaled (prepared, or crashed before
    // the worker opened its journal) can start fresh from the copy; once a
    // journal exists only resumeJob may continue it
    let reusable = !!(record && record.identity &&
      ['prepared', 'planned', 'paused'].includes(record.state));
    if (!resumeJobId && reusable && record.state !== 'prepared') {
      const dir = await this._jobDir(jobId);
      const journaled = await dir.getFileHandle(E.JOURNAL_FILE).then(() => true, () => false);
      if (journaled) {
        throw E.stError(E.ERROR_CODES.RESUME_REFUSED, '该任务已有进度记录，请使用续跑', { kind: 'use-resume' });
      }
    }
    if (!resumeJobId && !reusable) {
      if (!file) throw E.stError(E.ERROR_CODES.IO_RECOVERABLE, '缺少输入文件');
      await this._prepare(file, { ...opts, jobId: jobId || undefined });
      jobId = this.jobId;
      record = await this._readJobRecord(jobId);
    }
    this.jobId = jobId;
    if (!resumeJobId) {
      opts = {
        ...opts,
        channelJson: opts.channelJson === undefined
          ? (record.channelJson || null) : checkedChannelJson(opts.channelJson),
      };
      this._setState('probing');
    }

    // ---- resume validation (typed refusals, never blind-continue)
    let resume = null;
    let nextGen = 1;
    if (resumeJobId) {
      this._setState(record.state === 'paused' ? 'paused' : 'selected');
      const refuse = (reason, extra = {}) => {
        throw E.stError(E.ERROR_CODES.RESUME_REFUSED, reason, extra);
      };
      if (record.state === 'staging' || !record.identity) {
        refuse('源副本未完整复制（复制阶段中断），请重新开始', { kind: 'staging-incomplete' });
      }
      if (record.core !== this.coreVersion) {
        refuse(`核心版本不符：任务 ${record.core}，当前 ${this.coreVersion}`,
          { kind: 'core-version' });
      }
      if (opts.forceProfile !== true && record.profile !== profile.id) {
        refuse(`资源档位已改变：任务 ${record.profile}，请求 ${profile.id}`,
          { kind: 'profile' });
      }
      if ((record.policy || 'allow-edge') !== policy) {
        refuse(`像素策略已改变：任务 ${record.policy}，请求 ${policy}`,
          { kind: 'policy' });
      }
      const capChanged = (record.cap || null) !== (opts.outputCapBytes || null);
      if (capChanged) refuse('输出上限设置已改变', { kind: 'cap' });
      if ((record.channelJsonHash || null) !== channelJsonHash(opts.channelJson)) {
        refuse('伴随 channel.json 设置已改变', { kind: 'channel-json' });
      }
      // the staged copy must still be exactly the bytes hashed at staging
      const id = record.identity;
      const v = await this._request('verify-source', { jobId, size: id.size }, 60 * 60 * 1000);
      if (v.size !== id.size || v.sha256 !== id.sha256) {
        throw E.stError(E.ERROR_CODES.SOURCE_CHANGED,
          `源副本与记录不符（长度 ${v.size}/${id.size}${v.sha256 ? '，哈希不同' : ''}），拒绝续跑`);
      }
      // journal state
      const j = await this._readJournal(jobId);
      const st = j.state;
      if (!st.gen) {
        throw E.stError(E.ERROR_CODES.JOB_DIR_MISSING, 'journal 无有效代次记录');
      }
      nextGen = st.gen.gen + 1;
      if (st.lastCommit) {
        resume = { st: st.lastCommit.st };
      }
      // committed outputs must exist and be at least as long as committed
      const dir = await this._jobDir(jobId);
      const outFh = await dir.getFileHandle(E.OUTPUT_NAME);
      const outFile = await outFh.getFile();
      if (resume && outFile.size < resume.st.out) {
        throw E.stError(E.ERROR_CODES.IO_RECOVERABLE,
          `输出文件 ${outFile.size} < journal 已提交 ${resume.st.out}（数据丢失）`);
      }
    }

    // geometry for scratch pre-opening comes from probing the staged copy
    const probeResult = await this._request('probe', { jobId }, 60 * 60 * 1000);
    if (probeResult.error) throw probeResult;
    const doc = probeResult.document;
    const modality = doc.modality; // brightfield | fluorescence
    const levels = (doc.levels || []).length;
    const channels = modality === 'fluorescence' ? (doc.channels || []).length : 1;
    const estimate = doc.estimate || probeResult.estimate;
    const identity = record.identity;

    await this._updateJobRecord(jobId, {
      state: resume ? 'paused' : 'planned',
      identity,
      core: this.coreVersion,
      plan: 1,
      policy,
      profile: profile.id,
      channelJsonHash: channelJsonHash(opts.channelJson),
      // kept locally so resume can re-supply the identical companion
      channelJson: opts.channelJson || null,
      cap: opts.outputCapBytes || null,
      estimate,
      modality,
    });

    this._setState('planned');
    if (this.testMode && opts.faults) this._faults = opts.faults;
    const runOpts = {
      jobId,
      opts: {
        policy, channelJson: opts.channelJson || '',
        profileId: profile.id, outputCapBytes: opts.outputCapBytes || null,
        resume, nextGen, identity,
        coreVersion: this.coreVersion,
        modality,
        scratchLevels: levels,
        scratchIfdCount: levels * channels,
      },
      faults: this.testMode ? (opts.faults || null) : null,
    };

    // ---- running → finalizing → validating happen in the worker
    await this._spawnWorker();
    const doneP = new Promise((resolve) => { this._doneResolve = resolve; });
    this.worker.postMessage({ type: 'start', ...runOpts });
    return { jobId, done: doneP };
  }

  /// `done` settles only after the job record is updated and the heavy lock
  /// released, so a caller that lists jobs right after sees the final state.
  _onDone(m) {
    const resolve = this._doneResolve;
    this._doneResolve = null;
    const finish = () => {
      this._emit('done', m);
      this._releaseHeavyLock();
      if (resolve) resolve(m);
    };
    if (m.ok) {
      this._updateJobRecord(this.jobId, {
        state: 'ready',
        result: m.result,
        validation: m.validation,
        convertMs: m.convertMs,
        wasmHeapPeakBytes: m.wasmHeapPeak,
        journalBytes: m.journalBytes,
      }).then(() => {
        this._setState('ready', { sha256: m.validation && m.validation.sha256 });
      }).catch(() => { /* record stays as it was; result still returned */ })
        .finally(finish);
    } else {
      const isCancel = m.error && m.error.code === E.ERROR_CODES.CANCELLED;
      const rec = isCancel ? Promise.resolve() : this._updateJobRecord(this.jobId, {
        state: 'failed',
        error: m.error || (m.result && m.result.error) || null,
      }).catch(() => { /* keep going */ });
      if (!isCancel) this._setState('failed', { error: m.error });
      rec.finally(finish);
    }
  }

  _onCancelled(m) {
    if (this._doneResolve) {
      const r = this._doneResolve;
      this._doneResolve = null;
      r({ type: 'cancelled', ...m });
    }
    if (m.cleanupError) {
      this._addPendingCleanup(m.jobId || this.jobId, m.cleanupError)
        .then(() => this._updateJobRecord((m.jobId || this.jobId), {
          state: 'cleanup_pending',
        }))
        .catch(() => { /* */ })
        .finally(() => {
          this._setState('cleanup_pending', { cleanupError: m.cleanupError });
          this._setState('cancelled');
          this._emit('done', { type: 'cancelled', ...m });
          this._releaseHeavyLock();
        });
    } else {
      this._setState('cancelled');
      this._emit('done', { type: 'cancelled', ...m });
      this._releaseHeavyLock();
    }
    this._cancelled = false;
  }

  // -------------------------------------------------------------- cancel --

  /// Cancel: the compute worker executes the wasm core synchronously, so a
  /// queued 'cancel' message cannot be delivered mid-conversion — the
  /// sanctioned mechanism (plan §10.2) is terminating the isolated worker.
  /// The journal guarantees no inconsistent state survives; cancellation
  /// deletes this job's OPFS dir (pending-cleanup record if that fails).
  async cancelJob(feedbackTs = Date.now()) {
    this._cancelled = true;
    this._setState('cancelled', { requestedAt: feedbackTs, immediate: true });
    // resolve/abort any in-flight job promise and release the lock
    const crashed = { ok: false, phase: 'cancelled', cancelled: true };
    if (this._doneResolve) {
      const r = this._doneResolve;
      this._doneResolve = null;
      r(crashed);
    }
    this._emit('done', crashed);
    this._releaseHeavyLock();
    if (this.worker) {
      const w = this.worker;
      this.worker = null;
      this.workerReady = null;
      w.terminate();
      this._rejectPending('cancelled');
      this._emit('workerExit', { reason: 'cancelled' });
    }
    const jobId = this.jobId;
    if (!jobId) return { cancelled: true, cleanup: 'no-job' };
    try {
      if (this.testMode && this._faults && this._faults.failCleanup) {
        throw new Error('injected cleanup failure (test)');
      }
      const jobs = await this._jobsDir();
      await E.withRetry(() => E.removeEntryRecursive(jobs, jobId),
        { attempts: 12, delayMs: 500, name: 'cancel delete' });
      return { cancelled: true, cleanup: 'page-after-terminate' };
    } catch (e) {
      await this._addPendingCleanup(jobId, E.errText(e));
      return { cancelled: true, cleanup: 'pending-record' };
    }
  }

  /// Test/harness hook: hard-kill the compute worker (simulates crash).
  /// Performs the crash recovery the page would do on reload: unblocks any
  /// pending job promise and releases the heavy lock. The persisted job
  /// record keeps its last durable state (resume decides from the journal).
  async terminateWorkerForTest() {
    const crashed = { ok: false, phase: 'worker-exit', crashed: true };
    if (this._doneResolve) {
      const r = this._doneResolve;
      this._doneResolve = null;
      r(crashed);
    }
    this._emit('done', crashed);
    this._releaseHeavyLock();
    if (this.worker) {
      const w = this.worker;
      this.worker = null;
      this.workerReady = null;
      w.terminate();
      this._rejectPending('worker terminated');
      this._emit('workerExit', { reason: 'terminated' });
    }
  }

  // -------------------------------------------------------------- export --

  /// Stream OPFS artifact → writable (FileSystemWritableFileStream from
  /// showSaveFilePicker in the product, or an OPFS test target). One reused
  /// bounded buffer; a failed export NEVER deletes the OPFS artifact.
  async exportJob(jobId, getWritable) {
    const record = await this._readJobRecord(jobId);
    if (!record || (record.state !== 'ready' && record.state !== 'exported')) {
      throw E.stError(E.ERROR_CODES.NOT_READY,
        `任务状态 ${record ? record.state : 'missing'} 不可导出（仅 ready）`);
    }
    const dir = await this._jobDir(jobId);
    const fh = await dir.getFileHandle(E.OUTPUT_NAME);
    const file = await fh.getFile();
    const writable = await getWritable();
    // BYOB reader refilling ONE buffer; each chunk is fully written before
    // the buffer is handed back (per-chunk arrayBuffer() allocations were
    // measured to accumulate — report §4.3)
    let pos = 0;
    try {
      const reader = file.stream().getReader({ mode: 'byob' });
      let buf = new ArrayBuffer(EXPORT_CHUNK);
      for (;;) {
        const { value, done } = await reader.read(new Uint8Array(buf));
        if (done) break;
        await writable.write({ type: 'write', position: pos, data: value });
        pos += value.length;
        buf = value.buffer;
      }
      if (pos !== file.size) throw new Error(`导出字节 ${pos} ≠ 产物 ${file.size}`);
      await writable.close();
    } catch (e) {
      try { await writable.abort(); } catch { /* */ }
      // artifact untouched; state stays ready
      throw E.stError(E.ERROR_CODES.IO_RECOVERABLE,
        `导出失败（本地产物保留）：${E.errText(e)}`);
    }
    await this._updateJobRecord(jobId, { state: 'exported' });
    this._setState('exported');
    return { exportedBytes: pos, size: file.size };
  }

  // -------------------------------------------------------------- listing --

  /// JobSummary: {id, state, nextAction, active, createdAt, updatedAt,
  ///   source: {name, size, sha256} | null, modality, estimate,
  ///   settings: {profileId, policy, outputCapBytes, channelJson} | null,
  ///   hasChannelJson, committedBytes,
  ///   result: {outputBytes, sha256, channels: [{name, display_window, …}]} | null,
  ///   upload: {ingestionId, filename, size, state, confirmedParts, slideId,
  ///            updatedAt, error} | null (C4; page-managed shape),
  ///   error}
  /// nextAction: 'start' (prepared) | 'resume' (interrupted run) |
  ///   'export' (ready/exported) | 'wait' (running in this tab) | 'discard'.
  async _summary(id, dirHandle, rec) {
    const active = this.jobId === id && !!this._lockRelease &&
      ['probing', 'planned', 'running', 'paused', 'finalizing', 'validating'].includes(this._state);
    let committedBytes = 0;
    let hasJournal = false;
    try {
      const fh = await dirHandle.getFileHandle(E.JOURNAL_FILE);
      const f = await fh.getFile();
      hasJournal = f.size > 0;
      if (hasJournal && f.size <= 64 * 2 ** 20) {
        const { records } = E.decodeJournal(new TextDecoder().decode(
          await f.slice(0, f.size).arrayBuffer()));
        const st = E.journalState(records);
        committedBytes = st.lastCommit ? st.lastCommit.st.out : 0;
      }
    } catch { /* no journal yet */ }
    const state = rec ? rec.state : 'staging';
    let nextAction = 'discard';
    if (active) nextAction = 'wait';
    else if (state === 'prepared') nextAction = 'start';
    else if ((state === 'planned' || state === 'paused') && hasJournal) nextAction = 'resume';
    else if (state === 'planned' || state === 'paused') nextAction = 'start';
    else if (state === 'ready' || state === 'exported') nextAction = 'export';
    const id0 = rec && rec.identity;
    return {
      id, state, nextAction, active,
      createdAt: rec ? rec.createdAt : null,
      updatedAt: rec ? rec.updatedAt : null,
      source: id0 ? { name: id0.name, size: id0.size, sha256: id0.sha256 } : null,
      modality: rec ? rec.modality || null : null,
      estimate: rec ? rec.estimate || null : null,
      settings: rec && rec.profile ? savedSettings(rec) : null,
      hasChannelJson: !!(rec && rec.channelJson),
      committedBytes,
      result: rec && rec.result
        ? {
          outputBytes: rec.result.output_bytes,
          sha256: rec.validation && rec.validation.sha256,
          format: rec.result.format || null,
          channels: rec.result.channels || [],
        }
        : null,
      upload: rec && rec.upload ? { ...rec.upload } : null,
      error: rec ? rec.error || null : null,
    };
  }

  async listJobs() {
    let jobs;
    try { jobs = await this._jobsDir(); } catch { return []; }
    const out = [];
    for await (const [name, handle] of jobs.entries()) {
      if (handle.kind !== 'directory' || name.startsWith('.')) continue;
      let rec = null;
      try { rec = await E.readSlotRecord(handle, 'job'); } catch { rec = null; }
      try { out.push(await this._summary(name, handle, rec)); } catch { /* raced delete */ }
    }
    out.sort((a, b) => String(b.updatedAt || '').localeCompare(String(a.updatedAt || '')));
    return out;
  }

  async getJob(jobId) {
    let dir;
    try { dir = await (await this._jobsDir()).getDirectoryHandle(jobId); } catch { return null; }
    let rec = null;
    try { rec = await E.readSlotRecord(dir, 'job'); } catch { rec = null; }
    return this._summary(jobId, dir, rec);
  }

  /// Read back the artifact sha256 through the worker (wasm sha2 over a
  /// sync handle) — used by tests to compare against the native CLI.
  async hashArtifact(jobId) {
    return this._request('hash-opfs-file', { jobId, name: E.OUTPUT_NAME }, 60 * 60 * 1000);
  }

  /// C4: the finished artifact as a File view for the shared COS uploader
  /// (ready/exported only). The caller reads it strictly via slice() per
  /// part — never whole-file materialization (plan §3/§5).
  async artifactView(jobId) {
    const record = await this._readJobRecord(jobId);
    if (!record || (record.state !== 'ready' && record.state !== 'exported')) {
      throw E.stError(E.ERROR_CODES.NOT_READY,
        `任务状态 ${record ? record.state : 'missing'} 无产物可读（仅 ready）`);
    }
    const dir = await this._jobDir(jobId);
    const fh = await dir.getFileHandle(E.OUTPUT_NAME);
    return fh.getFile();
  }
}
