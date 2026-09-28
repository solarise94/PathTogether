# slide-tools-c0 core-spike (C0 part ②)

Rust spike of the shared slide-transform core: brightfield KFB parsing
(synthetic `kfb_bf_v1` + KF-BIO vendor layout) and a JPEG-passthrough tiled
BigTIFF pyramid writer, with all IO behind traits (`ByteSource` /
`RandomAccessSink` / `ScratchFactory`) so the same code builds natively
(file-backed, `kfb2tiff`) and for wasm32 (browser adapters in C0 part ③).

Behavioral reference / oracle: `PathTogether/kfb/` (parser.py,
vendor_kfbio.py, converter.py, fixture.py). Decisions, measurements and
parity results: `PathTogether/docs/slide-tools/c0-adr-core.md`.

## Layout

```
crates/core        pure format logic (parse, paged index, BigTIFF writer,
                   edge re-encode behind the `edge-reencode` feature)
crates/cli         kfb2tiff binary (convert / probe / gen-synth)
crates/wasm        wasm-bindgen surface; build proof + size measurement
scripts/           differential + validation harnesses (run against the
                   repo's Python venv and the real sample)
rust-toolchain.toml  pins rustc 1.98.1 + wasm32-unknown-unknown
```

Memory bound: in-memory state is O(levels) (≤16 × occupancy bitmap ≈ 1.2 MiB
worst case + one ≤8 MiB tile buffer); tile records and IFD offset/count
arrays spill to scratch storage and stream back in fixed pages. Measured:
RSS 11.6 MB for the 221 MB real sample, 4.0 MB for an 8.9 GB synthetic.

## Build & test

```bash
export PATH=$HOME/.cargo/bin:$PATH
cargo build --release                 # native CLI at target/release/kfb2tiff
cargo test                            # malformed-input suite (20 cases)
cargo build --release --target wasm32-unknown-unknown -p slide-transform-wasm-spike
cargo build --release --target wasm32-unknown-unknown -p slide-transform-wasm-spike --no-default-features
```

## CLI

```
kfb2tiff convert <in.kfb> <out.tif> [--overwrite] [--max-output-bytes N]
kfb2tiff probe   <in.kfb>
kfb2tiff gen-synth <out.kfb> --width N --height N [--quality Q] \
                     [--sampling 420|422|444] [--seed N]
```

`convert` writes `<out>.part`, the associated sidecars next to the output
(`<out>.associated/<name>.jpg`, byte-identical to the oracle's), then renames.
Scratch files (`.kfb2tiff-scratch-*`) are created in the output directory and
removed on completion.

## Differential vs Python oracle (synthetic fixtures)

```bash
bash scripts/run_diff_synth.sh            # 580x300 512x512 767x513 1024x768 300x580
bash scripts/run_diff_synth.sh 2048x1536  # custom sizes
```

Requires `PathTogether/.venv` (tifffile + Pillow + numpy) and runs the
oracle (`kfb/converter.convert_kfb`) on fixtures from `kfb/fixture.py`.
Expected: `structure+full-tile parity OK` per fixture; 512x512 additionally
whole-file sha256 EQUAL.

## Real sample (alias KFB-1) differential + independent readers

```bash
bash scripts/run_diff_kfb1.sh             # oracle vs spike + tifffile diff + sidecars
.venv/bin/python scripts/validate_readers.py <tif...>   # tifffile per-level tile decode
JAVA_HOME=$HOME/.local/opt/jdk-21.0.12.1+1-jre \
PATH=$JAVA_HOME/bin:$PATH ~/.local/opt/bftools/showinf -nopix -no-upgrade \
  -omexml-only <out.tif>
```

The sample path is private (alias map lives only under `.gate-tmp`).

## >4 GiB proof

```bash
G=PathTogether/.gate-tmp/slide-tools-c0/core
./target/release/kfb2tiff gen-synth $G/big/big.kfb --width 69376 --height 69376 --quality 92 --seed 424242
/usr/bin/time -v ./target/release/kfb2tiff convert $G/big/big.kfb $G/big/big.tif --overwrite
# then .venv/bin/python: open with tifffile, decode tiles whose TileOffset >= 2**32
```

Evidence from the C0 run: `big/beyond-4gib-proof.txt` (26,194/73,441 level-0
tiles beyond 4 GiB; IFD chain at 8.29 GiB; decode verified).

## Known divergences from the oracle (see ADR §5)

- Edge tiles: re-encoded with a different encoder (zune-jpeg + jpeg-encoder,
  source quantization tables + level subsampling reused), so bytes differ
  from Pillow; decoded-pixel diffs are measured instead (KFB-1 max 32/255).
- The oracle's internal tifffile re-validation is not replicated in the CLI
  (external harnesses validate instead); manifest JSON sidecar not emitted.
