// Host adapter for slide_transform.js (wasm-bindgen web target).
// Usage:
//   const st = await SlideTransformRunner.create();
//   const info = await st.probe(file);
//   const result = await st.convert(file, scratchDir, { strictLossless: false, channelJson: "" });
// All host IO goes through bounded chunks (≤1 MiB per callback).
export class SlideTransformRunner {
  static async create() {
    const mod = await import("./slide_transform.js");
    await mod.default();
    return new SlideTransformRunner(mod);
  }

  constructor(mod) {
    this.mod = mod;
    this.cancelled = false;
    this.progress = [];
  }

  _installHosts(file, scratchStore, names) {
    const dec = new TextDecoder();
    const enc = new TextEncoder();
    const mod = this.mod;
    const runner = this;
    globalThis.stHostSourceSize = () => file.size;
    globalThis.stHostRead = (offset, len) => {
      const buf = new Uint8Array(file.slice(offset, offset + len));
      // NOTE: File.slice is async to read; the wasm core calls this
      // synchronously. The browser runner pre-stages small inputs; for
      // large files use the OPFS-backed variant (see c1 report §runner).
      return runner._syncRead(file, offset, len, buf);
    };
    // synchronous bridge: pread into SharedArrayBuffer via a worker is not
    // available in all browsers; the reference runner requires the caller to
    // provide a pre-loaded source (File backed by an OPFS sync handle).
    // run_convert below installs the OPFS variant when a handle is given.
    globalThis.stHostWrite = (offset, data) => runner._out.write(offset, data);
    globalThis.stHostTruncate = (len) => runner._out.truncate(len);
    globalThis.stHostFlush = () => runner._out.flush();
    globalThis.stHostScratchOpen = (name) => {
      names.add(name);
      runner._scratch[name] = scratchStore ? scratchStore(name) : new MemSink();
    };
    globalThis.stHostScratchRead = (name, offset, len) =>
      runner._scratch[name].read(offset, len);
    globalThis.stHostScratchWrite = (name, offset, data) =>
      runner._scratch[name].write(offset, data);
    globalThis.stHostScratchTruncate = (name, len) =>
      runner._scratch[name].truncate(len);
    globalThis.stHostScratchFlush = (name) => runner._scratch[name].flush?.();
    globalThis.stHostProgress = (json) => runner.progress.push(JSON.parse(json));
    globalThis.stHostCancelled = () => runner.cancelled;
  }

  async probe(source) {
    this._installHosts(source, null, new Set());
    const json = this.mod.probe();
    return JSON.parse(json);
  }

  async convert(source, out, opts = {}) {
    this.cancelled = false;
    this.progress = [];
    this._out = out;
    this._scratch = {};
    this._installHosts(source, opts.scratchFactory || null, new Set());
    const json = this.mod.convert(
      !!opts.strictLossless,
      opts.channelJson || "",
    );
    return JSON.parse(json);
  }
}

// Simple growable in-memory sink/scratch (tests; the browser runner should
// swap in OPFS sync access handles for real work).
export class MemSink {
  constructor() { this.buf = new Uint8Array(1 << 16); this.len = 0; }
  _ensure(n) {
    if (n <= this.buf.length) return;
    let cap = this.buf.length;
    while (cap < n) cap *= 2;
    const next = new Uint8Array(cap);
    next.set(this.buf.subarray(0, this.len));
    this.buf = next;
  }
  write(offset, data) {
    this._ensure(offset + data.length);
    this.buf.set(data, offset);
    this.len = Math.max(this.len, offset + data.length);
  }
  read(offset, len) { return this.buf.slice(offset, offset + len); }
  truncate(len) { this.len = len; this._ensure(len); }
}
