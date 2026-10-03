# F2 feasibility: Aperio SVS JPEG 2000 decoding in the browser (WASM)

Date: 2026-10-03 (study), report. Scratch work: `<scratch>/f2-j2k/`.
Scope per implementation plan §5.3: decoder licence & packaging, WASM build, YCbCr/MCT, real sampling, per-tile decode memory and speed. Read-only review of the PathTogether worktrees; all experiments ran in the scratch dir.

## 1. Verdict

**GO, with conditions** (§8). Decoding Aperio JP2K SVS tiles in the existing
wasm32-unknown-unknown + wasm-bindgen pipeline is feasible today with a pure-Rust
decoder, well inside the memory budget and plausibly inside the speed budget:

- **Decoder:** `hayro-jpeg2000` 0.4.0 (Apache-2.0 OR MIT, pure Rust, zero
  mandatory dependencies with `default-features = false`). Builds for
  wasm32-unknown-unknown with no patching; wasm output is byte-identical to the
  native build; corrupt inputs fail with a clean error, not a trap.
- **Memory:** peak allocation per 256×256 / 240×240 tile ≈ **1.8–1.9 MiB**;
  whole-module linear memory ≈ **3.0 MiB**. The smallest profile
  (`saver`, `wasmHeapCapBytes` 160 MiB, `MIN_WORKING_SET_BYTES` 12 MiB) is not
  challenged.
- **Speed (node 22 / V8, desktop):** 1.8–4.5 ms per level-0 tile
  (222–562 tiles/s). Whole-slide pure-decode compute ≈ 14 s for
  JP2K-33003-1.svs (63.8 MB) and ≈ 70 s for CMU-1-JP2K-33005.svs (132.6 MB)
  in a single worker — the same order as the existing JPEG SVS conversions.
- **Colour:** 33003 (YCbCr 4:2:2, MCT=0) needs a reader-side YCbCr→RGB step;
  33005 (MCT=1, RCT) is decoded to RGB by the codestream inverse MCT. Both were
  implemented and validated against OpenSlide 4.0.1.
- **Output:** a JPEG-2000 source can never be tile-copied into the current
  JPEG-in-TIFF targets; every F2 conversion is a new lossy JPEG encode on top of
  the source's own lossy JP2K (Q=70 / KDU Q=30). This must be labelled, and the
  strict-lossless policy must reject JP2K sources. A lossless target codec would
  need new writer/reader contracts (out of scope here, not built).

## 2. Environment and samples

- rustc/cargo 1.98.1 (`~/.cargo/bin`), `wasm32-unknown-unknown` installed,
  wasm-bindgen 0.2.129 (crate + CLI), node v22.23.2, 16-core Linux dev box.
  Reference decode: `PathTogether/.venv` python 3.14.4 with tifffile
  2024.5.22, imagecodecs 2026.8.16 (OpenJPEG), openslide 1.4.6 (OpenSlide C
  4.0.1 per task context).
- Samples (re-downloaded; the earlier `download.log` had only the CMU JPEG
  slides): `JP2K-33003-1.svs` sha256 `6205ccf7…6fd` ✓ matches
  `Aperio-index.yaml`; `CMU-1-JP2K-33005.svs` sha256 `9a1923cd…420` ✓;
  `JP2K-33003-2.svs` (heart tissue) was still downloading at report time, so
  its tiles were split out of the partial file at SOC boundaries (its IFD sits
  at the end of the file) — codestream shape, decode and timings all checked
  (see §6.4), checksum verification pending completion.
  Note a licence correction vs the task text: per `Aperio-index.yaml` the
  JP2K-33003-* slides are **"distributable", not CC0**; the CC0 one is
  CMU-1-JP2K-33005. In-repo test fixtures must therefore come from the 33005
  sample; the 33003 slides stay as local-only validation data.

## 3. What the Aperio codestreams actually contain

Parsed with a small Python marker walker (`extract_tiles.py`, meta in
`meta.json`) from real tiles of both slides; confirmed against the QCD subband
count (16 = 3×5+1 ↔ 5 decomposition levels):

| | JP2K-33003-1 (`J2K/YUV16 Q=70`) | CMU-1-JP2K-33005 (`J2K/KDU Q=30`) |
|---|---|---|
| TIFF compression tag | 33003 | 33005 |
| tile | 256×256, level 0 grid 69×61 (levels ÷1, ÷4, ÷8) | 240×240, level 0 grid 138×192 (levels ÷1, ÷4, ÷16) |
| each TIFF tile | complete independent raw J2K codestream (`FF4F FF51…`) | same |
| SIZ | Csiz=3, unsigned 8-bit; XRsiz/YRsiz = (1,1),(2,1),(2,1) → **4:2:2 horizontal chroma subsampling** | (1,1),(1,1),(1,1) no subsampling |
| COD | LRCP, 1 layer, **MCT=0**, 5 levels, 64×64 code-blocks, max precincts, no SOP/EPH, 5/3 reversible | LRCP, 1 layer, **MCT=1 (RCT)**, 5 levels, 64×64 code-blocks, 5/3 |
| QCD | style 2 scalar-expounded → **lossy** | style 2 scalar-expounded → **lossy** |
| reader colour duty | decoder gives Y/Cb/Cr planes (chroma already replicated to full width by hayro); reader performs YCbCr→RGB | inverse MCT inside codestream; components are RGB already |

So "33003 = YCbCr, 33005 = RGB" from the task is confirmed at codestream
level, with the nuance that 33003 has no ICT in the codestream (MCT=0) — the
colour transform is entirely the reader's job, exactly as OpenSlide does it
(`openslide-decode-jp2k.c`: per-space `unpack_argb` + fixed-point tables from
`make-tables.c`, full-range JPEG coefficients 1.402 / 0.34414 / 0.71414 /
1.772, chroma indexed `x/2`).

## 4. Decoder survey (crates.io, 2026-10-03)

| crate | version (date) | licence | pure Rust | wasm32-unknown-unknown | verdict |
|---|---|---|---|---|---|
| **hayro-jpeg2000** | 0.4.0 (2026-06-14), MSRV 1.92 | Apache-2.0 OR MIT | yes; with `default-features=false` **zero mandatory deps** (`cargo tree`: only itself) | **builds unmodified**; `no_std`+allocator core, unsafe forbidden crate-wide unless the optional `simd`/`fearless_simd` feature is on | **selected**. Active (hayro PDF renderer project), ~3.1M downloads. Raw J2C + JP2, validated upstream against the OpenJPEG test suite + 20k PDFs |
| jpeg2k (backend `openjp2`) | 0.10.1 (2025-07-29) | MIT/Apache-2.0; `openjp2` 0.6.1 (2025-07-17) is BSD-2-Clause | yes (openjp2 = full OpenJPEG port) | **fails out of the box**: openjp2 declares `extern "C"` malloc/calloc/realloc/free and its own cdylib target; after vendoring + replacing the libc layer with a prefix-header allocator it links, but **traps (OOB) during codec teardown** — the port stores a `Vec`-backed pointer in `decoded_data` and frees it with a stale layout; silent UB natively, hard trap on wasm32 | fallback only. Natively it is **bit-exact vs OpenSlide** (see §6), valuable as a cross-check/reference implementation in the native CLI; not browser-viable short of real upstream work |
| openjpeg-sys | 1.0.12 (2025-04-16) | BSD-2-Clause | no (C OpenJPEG via `cc`) | no — C cannot be built into the wasm-bindgen/wasm32-unknown-unknown pipeline without an emscripten side-toolchain and a custom link story; would be the only C in an otherwise-Rust artifact | out (build complexity vs policy-neutral licence) |
| justjp2 | 0.1.1 (2026-03-20) | MIT OR Apache-2.0 | yes | not tried | very new (~1.5k downloads); has encoder+decoder; **not evaluated** |
| oxigeo-jpeg2000 | 0.2.4 (2026-08-18) | Apache-2.0 | yes | not tried | geospatial driver, MSRV 1.95; **not evaluated** |

Licence fit (c1-core-report §6: permissive only): hayro-jpeg2000
Apache-2.0 OR MIT is equivalent to the existing sha2/wasm-bindgen entries —
no IJG-style notice beyond standard attribution, no copyleft. The two C-backed
options are BSD-2-Clause (also permissive) but fail on packaging, not licence.

## 5. Prototype

Scratch workspace `proto/` (codec + cli + wasm crates, release profile
`opt-level = 3` mirroring `slide-transform-core`):

- `codec`: `decode_tile(bytes, convert_ycbcr)` → interleaved RGB8 using
  hayro-jpeg2000 (`Image::new` + `decode`), plus an OpenSlide-equivalent
  YCbCr→RGB converter (f64-computed tables, same rounding biases as
  `make-tables.c`). hayro replicates 4:2:2 chroma into full-resolution planes
  with the same `x/2` semantics OpenSlide uses (verified in its decode loop).
  Peak-allocation tracking allocator for memory numbers.
- `cli` (native) and `wasm` (cdylib, plain C-ABI exports) driven from node
  (`wasm-run/run.mjs`). The production build would use the existing
  wasm-bindgen bindings; the plain harness is equivalent for speed/memory and
  was chosen to avoid touching the read-only worktree.
- Tiles: 13 extracted via tifffile `dataoffsets/databytecounts`
  (L0 interior, L0 edge, L1, L2 of both slides).
- References per tile: OpenSlide 4.0.1 `read_region` (ground truth, valid
  sub-rectangle only) and imagecodecs/OpenJPEG decode of the same bytes.

Exact commands (from `.scratch/f2-j2k`):

```text
export PATH="$HOME/.cargo/bin:$PATH"
python extract_tiles.py …/JP2K-33003-1.svs …/CMU-1-JP2K-33005.svs   # tiles/ + meta.json
python reference_decode.py                                           # refs/*.osr.npy + summary
cargo build --release            -p f2-cli                                    # native
cargo build --release --target wasm32-unknown-unknown -p f2-wasm              # wasm
node wasm-run/run.mjs tiles/tile_JP2K-33003-1_L0_30_30.bin 20 1 out-wasm/….rgba
python compare.py                # native vs openslide; VS_REF=1 vs imagecodecs
```

## 6. Results

### 6.1 Correctness (vs OpenSlide 4.0.1 read_region, level 0 interior tiles)

| tile type | hayro native/wasm | openjp2 native (cross-check) |
|---|---|---|
| 33005 (240²) | max 2, mean 0.08, p99.9 = 1 | **max 0 — bit-exact** |
| 33003 (256²) | max 25, mean 3.0, p99.9 = 15 | **max 0 — bit-exact** (with our OpenSlide-table conversion) |

- openjp2 being bit-exact confirms the colour semantics (tables, rounding,
  4:2:2 replication) are implemented correctly; the residual hayro deltas are
  float-vs-fixed-point rounding on the *lossy* quantized coefficients, not a
  colour bug. No channel bias (signed per-channel means ≈ 0).
- For calibration, OpenJPEG's own `sycc` conversion (imagecodecs) differs from
  OpenSlide's tables by mean 0.4–3.3 on the same 33003 tiles — hayro sits
  inside the spread of "correct" implementations.
- L1/L2 comparisons against `read_region` carry a ±1-px sampling artifact
  (level downsamples are 4.0002 / 8.0009, non-integer); decoder-vs-decoder at
  all levels: 33005 max 0, 33003 max 1 (conversion variant). Edge tiles: the
  codestream contains encoder-padded pixels beyond the level bounds; OpenSlide
  crops — comparisons must crop to the valid sub-rectangle (done).
- Negative controls: decoding 33005 as YCbCr (or 33003 as RGB) gives mean
  48 / 68 — the classification is load-bearing and testable.
- wasm output is **byte-identical** to native output for the same feature set
  (`cmp` clean), which preserves the project's browser/native parity
  acceptance. Caveat: the native build with hayro's `simd` feature differs
  from the scalar build by 3/196,608 bytes (±1) on the 33003 tile — the
  feature set must be pinned per release. On wasm the scalar, simd-feature
  (scalar fallback) and `RUSTFLAGS="-C target-feature=+simd128"` builds all
  produced identical bytes for the tested tiles; simd128 gains 13–19%.

### 6.2 Speed (median ms/tile; n=20 after warmup)

| | native (scalar) | wasm (V8) | wasm + simd128 |
|---|---|---|---|
| 33003 L0 tiles | 1.34–2.44 (one 3.71 outlier tile) | 1.78–3.01 (outlier 4.51) | 2.69 (outlier tile) |
| 33005 L0 tiles | 1.19–1.67 | 2.12–2.70 | 2.17 |
| peak alloc / tile | 1.8–2.0 MiB | 1.8–1.9 MiB | same |
| module linear memory | — | ≈ 3.0 MiB | same |

Whole-slide pure-decode estimates (tile counts from the TIFF structure,
single worker, wasm medians): JP2K-33003-1 = 4,569 tiles ≈ **12–16 s**;
CMU-1-JP2K-33005 = 28,284 tiles ≈ **65–75 s**. Not measured in a real browser
(see §7).

### 6.3 Code size

| artifact | raw | gzip -9 |
|---|---|---|
| harness stub (no decoder) | 25,032 B | 9,384 B |
| + hayro-jpeg2000 (scalar) | 289,087 B | 85,413 B |
| + hayro (LTO, 1 CGU, panic=abort) | 278,312 B | 81,488 B |
| current production `slide_transform_bg.wasm` (for scale) | 395,349 B | 150,033 B |

Adding JP2K decode roughly **+260 KB raw / +80 KB gzip** to the shipped wasm
(no wasm-opt/binaryen available on this box to verify further shrinking).
Build-manifest and CDN notes must be updated; no licence/NOTICE addition
beyond the dependency listing (Apache-2.0 OR MIT).

### 6.4 Third slide: JP2K-33003-2.svs (heart tissue, same 33003 variant)

Tiles split from the partial download (`tiles/tile_JP2K-33003-2_*.bin`):
codestream shape identical to 33003-1 (256², comps (1,1)/(2,1)/(2,1), MCT=0,
5 levels, 1 layer). hayro native 1.78–1.90 ms/tile, wasm 2.26–2.44 ms/tile,
peak 1.8 MiB, wasm output byte-identical to native; RGB channel means
(242/243/241) plausible for brightfield; agreement vs OpenJPEG's own sycc
conversion mean 1.3–1.6 / max 17–19 — same level as 33003-1. No OpenSlide
`read_region` reference for this slide yet (file incomplete).

## 7. What was NOT tested

- No real browser or Web Worker run (node/V8 desktop only; Safari/Firefox and
  low-end devices unmeasured).
- No wasm-bindgen integration (plain C-ABI harness; pure computation, no
  reason to expect binding friction, but unverified).
- No fuzzing of hayro-jpeg2000; only two crude probes (2 KB truncated tile,
  random garbage → clean error returns, no trap). The F1 malformed-input
  corpus must be run against it before release.
- Only two slides validated end-to-end against OpenSlide (33003-1 aorta, 33005
  CMU-1 export); 33003-2 checked at codestream/decode level only (partial
  download; no `read_region` reference).
- HTJ2K (Part 15), >8-bit, JP2-boxed Aperio variants, non-Aperio JP2K SVS
  producers, unusual tile sizes (memory scales with tile area — a pathological
  4096² single-tile SVS would need ~order 100 MiB and must be rejected at
  parse), and 16-bit photometry are untested.
- justjp2 and oxigeo-jpeg2000 not evaluated.
- No output-size/quality measurements of the re-encoded JPEG (that is U3
  parameter work, out of F2 scope).

## 8. Recommendation and conditions

**Go** for F2 on the hayro-jpeg2000 path, gated on:

1. **Sequencing:** land F1's bounded SVS/TIFF adapter first; F2 adds
   JP2K decode + classification on top and must not enter F1's pass list
   (plan §5.3 already requires this).
2. **Decoder contract:** pin `hayro-jpeg2000 = "=0.4.x"` with
   `default-features = false` (zero unsafe, zero transitive deps); fix the
   feature set per release so browser/native byte parity holds (measured:
   `simd` feature changes output; `+simd128` target feature does not and is a
   safe ~15% win if wanted).
3. **Colour:** classify per tile from the codestream header (SIZ XRsiz/YRsiz +
   COD MCT) rather than trusting only the TIFF compression tag; apply the
   OpenSlide-table YCbCr→RGB for the 33003 shape. Keep the openjp2 numbers of
   this study as the reference implementation note (native-only).
4. **Output honesty:** every F2 tile is decoded and re-encoded JPEG — a new
   lossy generation on a lossy source. Label it in UI/report, count
   re-encoded tiles, and make `strict-lossless` reject JP2K sources before the
   first output byte. Do not advertise "lossless transcode"; a lossless target
   codec would be a separate writer/reader profile decision, not part of F2.
5. **Acceptance:** browser/native byte parity for the same decoder version
   (shown achievable), pixel comparison vs OpenSlide with an explicit
   tolerance (recommend: mean ≤ 5, max ≤ 40 on L0 interior tiles; 33005-class
   inputs expected near-exact), corrupt/truncated tile fixtures, and the F1
   malicious-input corpus against the new decode path.
6. **Fixtures:** CC0 CMU-1-JP2K-33005 only in-repo; JP2K-33003-* stays
   local-only ("distributable", not CC0).
7. **Risk watch:** hayro is young (0.x). Record its version in the journal
   fingerprint like the other codec versions; upgrading it is a
   re-acceptance event because output bytes can change.

Estimated production effort after F1: **~1.5–2.5 engineer-weeks**
(dep vendoring + pinning, header classifier ~100 LOC, YCbCr converter ~80 LOC
already written here, convert_bf wiring + validation warnings, wasm build
manifest update + artifact-size review, fixture and parity tests, UI wording
for the lossy re-encode).

## 9. Reproduction pointers

- Extraction/marker dump: `extract_tiles.py` → `tiles/`, `meta.json`
- References: `reference_decode.py` → `refs/*.osr.npy`, `refs/summary.json`
- Prototype: `proto/` (codec/cli/wasm), benchmarks above
- Comparisons: `compare.py` → `out/compare_native.json`,
  `out-ojp2/compare_native_ojp2.json`
- wasm harness: `wasm-run/run.mjs` (+ `run-simd.mjs`, `run-lto.mjs`,
  `run-ojp2.mjs`, stub/ojp2 wasm variants)
