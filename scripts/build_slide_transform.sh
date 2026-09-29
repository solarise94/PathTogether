#!/usr/bin/env bash
# Build script for the C1 slide-transform toolchain artifacts.
# Produces:
#   slide-transform-core/target/release/slide-transform          (native CLI)
#   static/tools/slide-transform/slide_transform_bg.wasm       (wasm module)
#   static/tools/slide-transform/slide_transform.js              (wasm-bindgen glue)
# runner.js / engine.js / worker.js are HANDWRITTEN sources in
# static/tools/slide-transform/ (C2 browser runner) — this script never
# overwrites them, it only records their hashes in the manifest.
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
        "engine.js": sha(out + "/engine.js"),
        "worker.js": sha(out + "/worker.js"),
        "slide-transform": sha(crate + "/target/release/slide-transform"),
    },
    "dependencies": deps,
    "notes": "JPEG codec lives inside the core crate (no third-party JPEG crate); it mirrors libjpeg-turbo code paths and carries the IJG attribution in NOTICE (docs/slide-tools/c1-core-report.md sections 4 and 6).",
}, open(manifest, "w"), indent=2, ensure_ascii=False)
PYGEN

echo "OK -> $OUT"
