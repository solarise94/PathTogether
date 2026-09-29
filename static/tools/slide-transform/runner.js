// runner.js — C2 page façade over the compute worker (engine.js + worker.js).
// Replaces the C1 placeholder runner: production API for the future /tools
// page (C3) and for the C2 test harness. ES module, no dependencies.
//
//   const runner = await SlideToolsRunner.create();
//   const prep   = await runner.probe(file);          // stages + probes
//   const job    = await runner.startJob(file, { jobId: prep.jobId, profileId, policy, ... });
//   const resumed= await runner.resumeJob(jobId);     // reads the staged copy
//   await runner.discardJob(jobId);
//   await runner.cancelJob(jobId);
//   await runner.exportJob(jobId, () => savePicker.createWritable());
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
const EXPORT_CHUNK = 4 * 2 ** 20;

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

  /// Slot records replace wholesale — patches must read-merge-write.
  async _updateJobRecord(jobId, patch) {
    const prev = (await this._readJobRecord(jobId)) || {};
    const next = { ...prev, ...patch, updatedAt: E.nowIso() };
    for (const k of Object.keys(next)) {
      if (next[k] === undefined) delete next[k];
    }
    await this._writeJobRecord(jobId, next);
    return next;
  }

  async _writeJobRecord(jobId, obj) {
    const dir = await this._jobDir(jobId, true);
    const gen = await E.writeSlotRecord(dir, 'job', { v: 1, id: jobId, ...obj });
    return gen;
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

  async discardJob(jobId) {
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
      await this.discardJob(jobId).catch(() => { /* pending-cleanup recorded */ });
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
      return await this._startOrResume(null, opts, jobId);
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
    if (!resumeJobId && (!record || record.state !== 'prepared')) {
      if (!file) throw E.stError(E.ERROR_CODES.IO_RECOVERABLE, '缺少输入文件');
      await this._prepare(file, { ...opts, jobId: jobId || undefined });
      jobId = this.jobId;
      record = await this._readJobRecord(jobId);
    }
    this.jobId = jobId;
    if (!resumeJobId) this._setState('probing');

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
      const cjHash = opts.channelJson ? E.fnv2x32(new TextEncoder().encode(opts.channelJson)) : null;
      if ((record.channelJsonHash || null) !== cjHash) {
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
      channelJsonHash: opts.channelJson
        ? E.fnv2x32(new TextEncoder().encode(opts.channelJson)) : null,
      cap: opts.outputCapBytes || null,
      estimate,
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

  _onDone(m) {
    if (this._doneResolve) {
      this._doneResolve(m);
      this._doneResolve = null;
    }
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
        this._emit('done', m);
        this._releaseHeavyLock();
      }).catch(() => this._releaseHeavyLock());
    } else {
      const isCancel = m.error && m.error.code === E.ERROR_CODES.CANCELLED;
      if (!isCancel) {
        this._updateJobRecord(this.jobId, {
          state: 'failed',
          error: m.error || (m.result && m.result.error) || null,
        }).catch(() => { /* keep going */ });
        this._setState('failed', { error: m.error });
      }
      this._emit('done', m);
      this._releaseHeavyLock();
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

  async listJobs() {
    let jobs;
    try { jobs = await this._jobsDir(); } catch { return []; }
    const out = [];
    for await (const [name, handle] of jobs.entries()) {
      if (handle.kind !== 'directory') continue;
      try {
        const rec = await E.readSlotRecord(handle, 'job');
        if (rec) out.push({ id: name, state: rec.state, updatedAt: rec.updatedAt });
      } catch { /* skip unreadable */ }
    }
    return out;
  }

  /// Read back the artifact sha256 through the worker (wasm sha2 over a
  /// sync handle) — used by tests to compare against the native CLI.
  async hashArtifact(jobId) {
    return this._request('hash-opfs-file', { jobId, name: E.OUTPUT_NAME }, 60 * 60 * 1000);
  }
}
