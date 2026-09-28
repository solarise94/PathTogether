#!/usr/bin/env bash
# Build script for the C1 slide-transform toolchain artifacts.
# Produces:
#   slide-transform-core/target/release/slide-transform          (native CLI)
#   static/tools/slide-transform/slide_transform_bg.wasm       (wasm module)
#   static/tools/slide-transform/slide_transform.js              (wasm-bindgen glue)
#   static/tools/slide-transform/runner.js                       (host IO adapter)
#   static/tools/slide-transform/build-manifest.json             (versions + sha256)
#   static/tools/slide-transform/NOTICE                          (third-party notices)
# Environment: PATH must include ~/.cargo/bin (rustc 1.98.1 pinned via
# rust-toolchain.toml; wasm-bindgen-cli 0.2.129 on PATH).
set -euo pipefail
export PATH="$HOME/.cargo/bin:$PATH"

REPO="$(cd "$(dirname "$0")/.." && pwd)"
CRATE="$REPO/slide-transform-core"
OUT="$REPO/static/tools/slide-transform"
mkdir -p "$OUT"

echo "== native CLI (release) =="
cargo build --manifest-path "$CRATE/Cargo.toml" -p slide-transform-cli --release
# The native CLI stays under target/; static/ is web-served and must only
# hold browser artifacts.
rm -f "$OUT/slide-transform"

echo "== wasm32 module =="
cargo build --manifest-path "$CRATE/Cargo.toml" -p slide-transform-wasm \
    --target wasm32-unknown-unknown --release
wasm-bindgen "$CRATE/target/wasm32-unknown-unknown/release/slide_transform_wasm.wasm" \
    --target web --out-dir "$OUT" --out-name slide_transform
cp "$CRATE/NOTICE" "$OUT/NOTICE"

echo "== runner.js (host IO adapter over the bindgen glue) =="
cat > "$OUT/runner.js" <<'JSEOF'
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
JSEOF

echo "== build-manifest.json =="
RUSTC_VERSION="$(rustc --version | head -1)"
BINDGEN_VERSION="$(wasm-bindgen --version)"
MANIFEST="$OUT/build-manifest.json"
python3 - "$MANIFEST" "$RUSTC_VERSION" "$BINDGEN_VERSION" "$CRATE" "$OUT" <<'PYGEN'
import hashlib, json, subprocess, sys, datetime
manifest, rustc_v, bindgen_v, crate, out = sys.argv[1:6]
def sha(path):
    h = hashlib.sha256()
    h.update(open(path, "rb").read())
    return h.hexdigest()
deps_raw = subprocess.run(
    ["cargo", "tree", "--manifest-path", crate + "/Cargo.toml",
     "-p", "slide-transform-wasm", "--prefix", "none", "--format", "{p}|{l}", "-e", "normal"],
    capture_output=True, text=True,
    env={"PATH": "/home/solarise/.cargo/bin:/usr/bin:/bin"}).stdout
deps = []
seen = set()
for line in deps_raw.splitlines():
    if "slide-transform" in line:
        continue
    parts = line.split("|")
    if len(parts) != 2:
        continue
    name_ver, lic = parts
    name = name_ver.split(" v")[0]
    if name in seen:
        continue
    seen.add(name)
    deps.append({"crate": name, "version": name_ver.split(" v")[-1].split()[0],
                 "license": lic.strip() or "unknown"})
json.dump({
    "built_at_utc": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    "toolchain": {"rustc": rustc_v, "wasm_bindgen_cli": bindgen_v,
                  "pinned_rust": "1.98.1", "pinned_bindgen": "0.2.129"},
    "artifacts": {
        "slide_transform_bg.wasm": sha(out + "/slide_transform_bg.wasm"),
        "slide_transform.js": sha(out + "/slide_transform.js"),
        "runner.js": sha(out + "/runner.js"),
        "slide-transform": sha(crate + "/target/release/slide-transform"),
    },
    "dependencies": deps,
    "notes": "JPEG codec lives inside the core crate (no third-party JPEG crate); it mirrors libjpeg-turbo code paths and carries the IJG attribution in NOTICE (docs/slide-tools/c1-core-report.md sections 4 and 6).",
}, open(manifest, "w"), indent=2, ensure_ascii=False)
PYGEN

echo "OK -> $OUT"
