// Page-side engine: owns the two workers, collects progress/records,
// handles the termination probe (kill workers from the main thread) and the
// post-reload persistence check. No inline script: CSP-clean.

import { fillExpectedOutput } from './common.js';

const state = {
  status: 'idle',
  written: 0,
  total: 0,
  records: [],
  phases: [],
  killed: false,
  killReport: null,
  reloadAt: null,
};
window.__spikeState = state;
const statusEl = document.getElementById('status');
function setStatus(s) {
  state.status = s;
  if (statusEl) statusEl.textContent = s;
}

function bytesEqual(a, b) {
  if (a.length !== b.length) return false;
  for (let i = 0; i < a.length; i++) if (a[i] !== b[i]) return false;
  return true;
}

window.runSpike = function (config) {
  const fileInput = document.getElementById('file');
  const file = fileInput && fileInput.files && fileInput.files[0];
  if (!file) return Promise.reject(new Error('no input file selected'));
  state.inputSize = file.size;
  state.records = [];
  state.killed = false;
  state.killReport = null;
  state.reloadAt = null;

  const io = new Worker('io-worker.js', { type: 'module' });
  const compute = new Worker('compute-worker.js', { type: 'module' });
  const ch = new MessageChannel();

  return new Promise(async (resolve, reject) => {
    let settled = false;
    const estimateBefore = await navigator.storage.estimate();
    let persistGranted;
    try {
      persistGranted = await navigator.storage.persist();
    } catch (e) {
      persistGranted = 'error: ' + String(e);
    }

    compute.addEventListener('error', (e) => {
      // worker script load failure (e.g. worker-src blocked by CSP)
      if (settled) return;
      settled = true;
      reject(new Error('compute worker load error: ' + (e.message || e.filename || 'unknown')));
    });

    compute.onmessage = (e) => {
      const m = e.data;
      if (m.type === 'progress') {
        state.written = m.written;
        state.total = m.total;
        if (m.records) state.records.push(...m.records);
        setStatus(`running ${Math.round(m.written / 1048576)}/${Math.round(m.total / 1048576)} MiB`);
      } else if (m.type === 'phase') {
        state.phases.push(m);
      } else if (m.type === 'killme') {
        state.killed = true;
        terminateAndProbe();
      } else if (m.type === 'reloadme') {
        state.reloadAt = m.written;
        setStatus('reloadme ' + m.written);
      } else if (m.type === 'done') {
        if (settled) return;
        settled = true;
        setStatus('done');
        resolve({ ...m.result, estimateBefore, persistGranted, inputSize: file.size });
      } else if (m.type === 'fatal') {
        if (settled) return;
        settled = true;
        setStatus('fatal');
        reject(new Error('worker fatal: ' + m.error));
      }
    };

    async function terminateAndProbe() {
      // Kill BOTH workers mid-write; then inspect OPFS from the main thread
      // (no sync handles on the main thread: getFile() + slice() only).
      io.terminate();
      compute.terminate();
      try {
        const root = await navigator.storage.getDirectory();
        const fh = await root.getFileHandle('spike-out.bin');
        const f = await fh.getFile();
        const first = state.records[0]; // fixed chunk: t=0, s=0, len=chunkMax
        const probe = { size: f.size, lastKnownWritten: 0, chunk0Ok: null };
        if (state.records.length) {
          const last = state.records[state.records.length - 1];
          probe.lastKnownWritten = last.t + last.len;
        }
        if (first) {
          const got = new Uint8Array(await f.slice(first.t, first.t + first.len).arrayBuffer());
          const exp = fillExpectedOutput(new Uint8Array(first.len), first.s);
          probe.chunk0Ok = bytesEqual(got, exp);
        }
        state.killReport = probe;
        setStatus('killed+probed');
      } catch (e) {
        state.killReport = { error: String(e) };
        setStatus('killed+probe-failed');
      }
    }

    io.postMessage({ type: 'init', file, port: ch.port1 }, [ch.port1]);
    compute.postMessage({ type: 'run', config, port: ch.port2 }, [ch.port2]);
  });
};

// Called by the runner AFTER a page reload: OPFS must still hold the file
// with everything written up to the reload; chunk 0 content is verifiable
// without the input File (deterministic pattern).
//
// Measured behavior: immediately after reload the file can still be locked
// by the dying worker's sync access handle — getFile()/slice() then fails
// with NotReadableError. Retry with backoff and RECORD how long the lock
// lasts; that delay bounds what a resume path must tolerate.
window.checkOpfsPersisted = async function (minBytes) {
  const root = await navigator.storage.getDirectory();
  const t0 = performance.now();
  let lastErr = null;
  for (let attempt = 1; attempt <= 20; attempt++) {
    try {
      const fh = await root.getFileHandle('spike-out.bin');
      const f = await fh.getFile();
      const len = Math.min(4 * 1024 * 1024, f.size);
      const got = new Uint8Array(await f.slice(0, len).arrayBuffer());
      const exp = fillExpectedOutput(new Uint8Array(len), 0);
      return {
        size: f.size,
        minBytes: minBytes || 0,
        sizeAtLeastMin: f.size >= (minBytes || 0),
        chunk0Ok: bytesEqual(got, exp),
        lastModified: f.lastModified,
        readableAfterMs: Math.round(performance.now() - t0),
        attempts: attempt,
        lastErrorBeforeSuccess: lastErr,
      };
    } catch (e) {
      lastErr = String(e);
      await new Promise((r) => setTimeout(r, 500));
    }
  }
  return { error: 'still unreadable after 10s', lastError: lastErr };
};

// Page-level WASM compile probe (the engine design only compiles WASM in
// workers, but record whether the page CSP would also allow it).
window.probePageWasm = async function () {
  try {
    // (module) — the smallest valid wasm binary
    await WebAssembly.instantiate(new Uint8Array([0x00, 0x61, 0x73, 0x6d, 0x01, 0x00, 0x00, 0x00]));
    return { pageWasmCompileOk: true };
  } catch (e) {
    return { pageWasmCompileOk: false, error: String(e) };
  }
};

window.coopCoepInfo = function () {
  return {
    crossOriginIsolated: !!self.crossOriginIsolated,
    userAgent: navigator.userAgent,
    deviceMemory: navigator.deviceMemory,
    hardwareConcurrency: navigator.hardwareConcurrency,
  };
};
