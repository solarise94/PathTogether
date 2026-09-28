#!/usr/bin/env bash
# Build the spike WASM module into ../site/ (wasm-bindgen --target web glue).
# Toolchain: rustc 1.98.1 pinned by rust-toolchain.toml; wasm-bindgen-cli 0.2.129.
set -euo pipefail
cd "$(dirname "$0")/rust"

CARGO=${CARGO:-$HOME/.cargo/bin/cargo}
WB=${WB:-$HOME/.cargo/bin/wasm-bindgen}

"$CARGO" build --release --target wasm32-unknown-unknown
"$WB" --target web --out-dir ../site \
  target/wasm32-unknown-unknown/release/slide_chunk_spike.wasm
ls -la ../site/slide_chunk*
