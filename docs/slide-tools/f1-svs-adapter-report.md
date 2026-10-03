# F1 — Brightfield SVS (Aperio, JPEG tiles) Input Adapter Report

Phase F1 of `docs/slide-tools/tool-ux-and-format-expansion-implementation-plan-20261003.md`
(§5.1/§5.2). Branch `svs-adapter`, worktree `.gate-tmp/svs-wt`. **Status: implemented
and gated; uncommitted work in the worktree** (no commit per task rules).
Code:

- `slide-transform-core/crates/core/src/tiff_read.rs` — bounded TIFF/BigTIFF reader
- `slide-transform-core/crates/core/src/svs.rs` — Aperio SVS detection/probe adapter
  (`aperio-svs-jpeg`, adapter version `1`)
- `slide-transform-core/crates/core/src/convert_svs.rs` — conversion to both
  brightfield output profiles
- `slide-transform-core/crates/core/src/svs_fixture.rs` — synthetic SVS fixture
  generator (in-code, `fixtures` feature)
- `slide-transform-core/crates/core/tests/svs.rs` — 21 fixture/malformed/resume tests
- `slide-transform-core/crates/cli/src/main.rs` — `probe`/`convert` routing +
  `gen-svs` fixture generator
- `slide-transform-core/crates/wasm/src/lib.rs` — SVS routing, adapter-carrying
  checkpoints, resume refusal
- `static/tools/slide-transform/{engine,runner,worker}.js` — TIFF header detection,
  bounded pre-stage structural sniff, adapter identity in records/journals
- `tests/browser/slide_tools_c2/run_parity.js` — `--svs` browser/native parity mode
- `tests/js/tools-svs-input.test.ts` — engine sniff unit tests (vitest)
- `tests/test_slide_svs_viewer.py` — platform viewer-path pytest (new)

Evidence: `.gate-tmp/f1/` (comparisons, QuPath probes), `.gate-tmp/slide-tools-c2/browser/parity-svs/`.

## 0. Verdict

| Requirement (plan §5.1–5.2) | Status |
|---|---|
| Bounded TIFF/BigTIFF reader (byte order, 32/64-bit offsets, IFD/SubIFD chains, loops, overflow, bounds, tag caps; malformed corpus) | **PASS** — 21-test corpus incl. truncation, pointer loop, OOB tile, huge dims, wrong tag types, count mismatch |
| Aperio detection, true reduced levels, associated excluded, typed rejections incl. JPEG 2000 | **PASS** — probe returns capability report or typed `unsupported_kfb_variant` reason |
| Passthrough only when compatible; shared JPEGTables carried verbatim (written as tag 347 in output IFDs, NOT spliced per tile); true colourspace from JPEG data, verified against independent decoder; source tile size via 322/323; KFB outputs byte-identical | **PASS** — tile payload byte identity 38 805/38 805 (CMU-2), KFB-1 hashes unchanged |
| Levels/downsamples preserved, MPP/AppMag from Aperio description or unknown, ICC → 34675, label/macro not exported | **PASS** |
| Adapter id + version in plan/report/provenance; wasm checkpoints; runner records; resume refuses on adapter mismatch | **PASS** (refusal covered by wasm unit tests + runner logic, not a C2 fault scenario — §8) |
| engine.js TIFF detection; runner stages SVS only after bounded probe | **PASS** — vitest + no-whole-file gate |
| browser == native parity for SVS, both profiles | **PASS** — byte-equal |
| Acceptance: fixtures, real CC0 samples, QuPath strict gate, OpenSlide, platform reader, vitest, KFB gates unchanged | **PASS** (details below) |

## 1. Design

### 1.1 Input adapter (`svs.rs`, `tiff_read.rs`)

The reader never materialises the file: every access is `ByteSource::read_at(offset, len)`
with explicit length. Hard bounds: per-IFD entry count ≤ 512, walked IFDs ≤ 64 with
duplicate-offset detection (pointer loops are a typed error, not a hang), tag values ≤ 4 MiB,
every (count × element size) product checked, tile grids checked against nominal
geometry, every tile interval bounds-checked against the file size. `TileCursor`
streams (offset, count) pairs in chunks of 512 so a wasm host sees few, bounded reads.

Accept rule (IFD 0): tiled (322/323 present), compression 7, chunky (PlanarConfiguration 1),
SamplesPerPixel 3 with BitsPerSample [8,8,8], no ExtraSamples, photometric 2 or 6,
ImageDescription identifying Aperio. Reduced levels are the *tiled JPEG* pages after IFD 0
forming a strictly decreasing 1.5–8× (x and y, ratio difference ≤ 1.5) sequence —
Aperio's own 4×. Stripped pages become associated summaries: `label`/`macro` via their
description line markers or NewSubfileType bit 3, anything else small is the thumbnail.
A second tiled sequence, a tiled page with a non-JPEG codec, a stripped page as large as
the main image, or a fluorescence page set is a typed rejection. **Label/macro/thumbnail
are not exported** — this is a main-image conversion, not a source archive
(warning `aperio_associated_not_exported`).

### 1.2 Shared JPEGTables and colourspace (the two correctness traps)

- **JPEGTables (347): written verbatim into every output IFD**, not spliced per tile.
  Rationale: the tile payloads stay byte-identical (the acceptance proof compares payload
  bytes), the output stays a standards-conformant tiled-JPEG TIFF that every reader
  resolves the same way the source was read, and per-tile splicing would rewrite ~250 KB
  of duplicated headers per level for zero fidelity gain.
- **True colourspace comes from the JPEG data, never from the source tags.**
  Aperio writes `JPEG/RGB` + PhotometricInterpretation 2 while its YCbCrSubSampling tag
  lies (CMU-1 tags (2,2) but every SOF is 4:4:4). The rule (`jpeg::tiff_jpeg_color`,
  mirroring tifffile's `jpeg_decode_colorspace`): JFIF APP0 ⇒ YCbCr; Adobe APP14
  transform 0 ⇒ RGB / else YCbCr; no marker ⇒ the TIFF photometric decides (the Aperio
  `JPEG/RGB` case, component ids 0,1,2). Verified against the independent decoder:
  OpenSlide renders all CMU levels exactly like an RGB-forced decode (max diff 0,
  §4.2), while a naive libjpeg-default YCbCr conversion differs by up to 249 — the
  generic-decoder default is wrong for these streams, the adapter's rule is right.
  The output photometric is the payload truth (2 for these files); nothing is relabelled.
  For YCbCr-payload levels the output carries photometric 6 with the SOF subsampling
  (fixture-tested; no real YCbCr SVS sample was available — §8).

### 1.3 Writer generalisation and tile size

`bigtiff.rs` gained `end_level_ex` / `LevelExtras`, `ome_writer.rs` gained
`begin_rgb_ifd_ex` / `RgbIfdExtras`: source tile shape (322/323 — e.g. 240),
photometric 2/6, optional JPEGTables (347) and ICC (34675), optional calibration
(unknown MPP ⇒ 282/283/296 omitted, never invented). All fields default to the
historical KFB layout; the old signatures remain and the KFB byte stream is unchanged
(one static 270 before 324/325 produces the same bytes as before). KFB-1 hashes are
identical pre/post (§4.5). `StrictLossless` accepts everything: the adapter never
re-encodes; cropped tiles (Aperio keeps full tiles past the edge — those are copied
as-is) and genuinely cropped tiles are passed through verbatim and recorded in
`edge_regions` with warning `svs_cropped_edge_tile_passthrough`.

### 1.4 Provenance, resume, runner

`TransformResult` carries `source_format: "aperio-svs-jpeg"` + `adapter_version: "1"`
(CLI report, wasm result JSON, OME-XML `<M>` provenance, classic-profile description
JSON). Wasm checkpoints embed `"adapter":"aperio-svs-jpeg"`; resume refuses when the
journalled adapter differs from the current input's (mirroring the output-profile
refusal) — pre-adapter KFB/KFBF states carry no field and keep resuming. runner.js
runs `sniffTiffSlideCapability` (≤ ~78 KiB of bounded reads) **before staging**, so an
unsupported multi-GiB TIFF is never copied into OPFS, and records `sourceAdapter` in
the task record; the staged copy's wasm probe must agree on resume.

## 2. Support matrix (probe = authoritative report; both outputs share one accept rule)

| Input | Verdict | Reason / detail |
|---|---|---|
| Classic TIFF or BigTIFF, little/big endian, tiled JPEG, Aperio description, RGB or YCbCr payloads | **accepted** | converted to `bf-ome` / `bf-classic`, pure passthrough |
| Tile shapes 16–8192 (240, 256 fixture-tested) | **accepted** | written via 322/323 as the source's own shape |
| Shared JPEGTables present | **accepted** | tag 347 written verbatim into each output IFD |
| MPP / AppMag in description | **accepted** | PhysicalSize + NominalMagnification (OME) / XResolution (classic); else `unknown`, nothing invented |
| ICC profile on main IFD | **accepted** | carried to tag 34675 (main level only) |
| Associated thumbnail / label / macro | **detected, not exported** | main-image conversion; CLI exports no sidecars for SVS |
| JPEG 2000 (compression 33003 or 33005) | **rejected** | `unsupported_kfb_variant`: "JPEG 2000 压缩（33003）不在 F1 支持集（需要独立解码器，见计划 §5.3）" |
| Non-Aperio TIFF (unknown vendor) | **rejected** | "TIFF 结构合法但 ImageDescription 未标识 Aperio：未知厂商变体不猜" |
| Stripped main image, planar (284=2), SamplesPerPixel ≠ 3 / ExtraSamples (fluorescence page set), second tiled series, unknown downsample sequence, non-JPEG tiled codec, tile without tables or self-contained DQT | **rejected** | specific typed reasons (message text asserts the offending tag) |
| Fluorescence output profile (`fl-ome`) on SVS input | **rejected** | profile/input modality mismatch |
| Malformed containers (truncated header, IFD loop, OOB tile offset/length, huge dims, wrong tag types, 324/325 count mismatch, corrupt payload) | **rejected** | typed error codes (`invalid_kfb_header`, `conversion_validation_failed`, `tile_payload_out_of_bounds`, `unsupported_kfb_variant`, `jpeg_decode_failed`) |

## 3. Commands and versions

Worktree root; `TMPDIR=$PWD/.gate-tmp COLUMNS=200`; heavy runs under
`systemd-run --user --scope -q -p MemoryMax=4G -p MemorySwapMax=0`.

```text
cargo test --release --workspace --features slide-transform-core/fixtures
bash scripts/build_slide_transform.sh
npx vitest run --dir tests/js
PYTHONPATH=.:tests .venv/bin/python -m pytest tests/test_slide_svs_viewer.py -q
slide-transform-core/target/release/slide-transform convert <sample.svs> <out.tif> --overwrite --profile bf-ome|bf-classic
node tests/browser/slide_tools_c2/run_parity.js --svs <sample.svs> --port 8990
node tests/browser/slide_tools_c2/run_smoke.js --samples <切片文件夹> --port 8991
node tests/browser/slide_tools_c2/run_parity.js --samples <切片文件夹> --port 8992
node tests/browser/slide_tools_c2/run_faults.js --samples <切片文件夹> --port 8993
node tests/browser/slide_tools_c2/test_no_whole_file.js
bash scripts/qupath-probe/run_probe.sh <qupath-root> <out-dir> <bf-ome.tif> 0.2 0.7 \
  --expect-resolutions 3 --expect-server-substring BioFormats --expect-rgb
.venv/bin/python scripts/qupath-probe/compare_regions.py <out>/probe.json <out>/regions \
  <bf-classic.tif> <out>/region-compare.json --expect-regions 12 --min-textured 3
.venv/bin/python .gate-tmp/f1/compare_roi.py <sample.svs> <bf-ome.tif> <bf-classic.tif>
```

Versions: rustc 1.98.1 (48a229cea), wasm-bindgen 0.2.129, openslide 4.0.1 /
openslide-python 1.4.6, tifffile 2024.5.22, imagecodecs 2026.8.16, numpy 2.5.3,
QuPath 0.6.0-rc5 + 0.7.0 (Bio-Formats 8.1.1), Chromium via Playwright (C2 harness).

## 4. Results

### 4.1 Input samples (public CC0 OpenSlide set; sha256)

| Sample | sha256 | Note |
|---|---|---|
| CMU-1-Small-Region.svs | `ed92d5a9f2e86df67640d6f92ce3e231419ce127131697fbbce42ad5e002c8a7` | 2220×2967, 1 level, tile 240 |
| CMU-1.svs | `00a3d54482cd707abf254fe69dccc8d06b8ff757a1663f1290c23418c480eb30` | 46000×32914, 3 levels, tile 256, MPP 0.499, AppMag 20 |
| CMU-2.svs | `fb6df83bfd91a252185c9652aebeb00deff84e49329f7ff8f75744f31f475b08` | 78000×30462, 4 levels, tile 256 |
| JP2K-33003-1.svs | `6205ccf75a8fa6c32df7c5c04b7377398971a490fb6b320d50d91f7ba6a0e6fd` | JPEG 2000 — rejected |

CMU-1-JP2K-33005.svs rejected identically (33005). Output sha256 (bf-ome / bf-classic):
CMU-1-Small-Region `fcb6d1719262f2ce…` / `dcefe86063c632a3…`, CMU-1
`9d1ac1e809ae0402…` / `00198666461c4d5d…`, CMU-2 `78fdfa6bca36e58c…` / `df8a8ffc2eb23b7e…`.

### 4.2 Pixel / payload verification (bounded ROI + tile streaming; never a whole level in memory)

- **Tile payload byte identity vs source: 100 %** — CMU-1-Small-Region 130/130,
  CMU-1 24 813/24 813, CMU-2 38 805/38 805 — for **both** output profiles.
- **bf-classic through OpenSlide (openslide↔openslide, same decoder): exact** —
  24 ROIs of ≤512×512 across all levels of all three samples, every `max_abs_diff = 0`;
  levels, dimensions and downsamples identical to the source (4× Aperio steps).
- **bf-ome through the independent decoder** (imagecodecs, i.e. what tifffile uses):
  with the Aperio RGB rule applied, same-decoder source-vs-output diff = **0** on every
  ROI. Cross-decoder (OpenSlide libjpeg-turbo vs imagecodecs libjpeg on byte-identical
  payloads): max diff 0 / 39 / 177 per sample (97–99 % of pixels exactly equal, ~3 %
  differ by 1) — JPEG IDCT implementation variance on heavily quantised (Q=30) tiles,
  not a conversion effect (payloads are byte-identical; stated tolerance: byte identity
  is the exactness criterion, cross-decoder ≤ 200 with mean ≤ 0.02).

- **Reviewer check through the platform viewer path** (`slide_io.open_slide`, which serves
  bf-ome via `TiffFileSlide`/tifffile and bf-classic via OpenSlide), CMU-1, three 384×384
  ROIs per level vs OpenSlide on the source SVS: bf-classic exact on every ROI. bf-ome
  exact at level 0, but on a dense tissue ROI level 1 differs by mean 6.0 / max 57 (7.7 %
  of values equal) and level 2 by mean 0.7 / max 14; background ROIs ≤ 0.03 mean. Reading
  the **source SVS itself** with tifffile gives the identical difference and tifffile
  source == tifffile output exactly, so this is a decoder disagreement on the reduced-level
  payloads (most likely chroma upsampling), not a conversion effect. Consequence: after
  upload, the platform's bf-ome viewer can render SVS-derived reduced levels slightly
  differently from an OpenSlide view of the original; the "~3 % differ by 1" summary above
  holds for background-heavy ROIs only.

### 4.3 Structure and metadata

Independent IFD walks: bf-ome = main IFD + SubIFDs (tag 330) per reduced level
(CMU-1: 2, CMU-2: 3; single-level Small-Region: none, correctly); bf-classic = one IFD
chain with NewSubfileType 0/1. Photometric 2 everywhere (payload truth), JPEGTables on
every level IFD, ICC absent (samples carry none → warning `color_management_not_applied`).
QuPath 0.6.0-rc5 and 0.7.0 both open the CMU-1 bf-ome via
`BioFormatsImageServer`: resolutions=3 (downsamples 1/4/16), RGB, mpp_x=mpp_y=0.499,
magnification 20, UINT8 — strict gate exit 0 on both; region comparison vs the classic
output: 12/12 regions `max_abs_diff 0` (3 textured tissue boxes per version).

### 4.4 Gates

| Gate | Result |
|---|---|
| Rust workspace tests (`--features …/fixtures`) | **99 passed** (17 unit, 8 bf_ome, 2 jpeg_mutation, 37 malformed, 10 resume, 21 svs, 4 wasm, 7 harness) — includes the malformed corpus, both output profiles × classic/BigTIFF × little/big endian × tile 240/256, YCbCr variant, missing MPP, cropped-tail passthrough under both pixel policies, payload byte identity, resume byte-identity |
| vitest (`tests/js`) | **645 passed / 42 files** (incl. 8 new SVS sniff tests) |
| pytest `tests/test_slide_svs_viewer.py` | **2 passed** (viewer-path reading of SVS-derived bf-ome + bf-classic; skips when CLI/sample absent) |
| C2 parity `--svs` (browser vs native, both profiles) | **PASS**, byte-equal (`SVS PARITY PASS`) |
| C2 smoke KFB | **PASS** — browser `6e8744f9…` == native `6e8744f9…` (unchanged) |
| C2 parity KFB-1 bf-ome | **PASS** — browser == native `374c70c8e9d778de…` (unchanged) |
| Native KFB-1 bf-classic | `385a59c6c69478c2…` (unchanged) |
| C2 fault matrix | **30/30 passed** |
| no-whole-file static gate | **PASS** |
| JP2K rejection (33003 + 33005), probe and convert | **rejected, exit 1, typed reason, no output file** |

### 4.5 KFB non-regression proof

The writer generalisation keeps the historical layout for KFB (`end_level` /
`begin_rgb_ifd` unchanged; extras default to the KFB constants). The three pinned KFB-1
hashes above reproduce byte-for-byte after the change.

## 5. Regression status of the new tests

At `HEAD` (7aa4d88) the SVS modules do not exist, so `tests/svs.rs` cannot compile —
the "fails on old code" condition is trivially met for the adapter suite. The writer
generalisation assertions (tile 240 via 322/323, omitted 282/283/296 when MPP unknown,
photometric 2 passthrough, ICC 34675) directly contradict the old hardcoded KFB
constants (256, always-MPP, photometric 6); the byte-identity of the three KFB hashes
proves the old path is simultaneously preserved.

## 6. Privacy

Only public CC0 OpenSlide samples were used for real-data gates, referenced by name +
sha256 above. No clinical sample, label image or scanner identity entered any tracked
file; private KFB samples are referenced by alias only (KFB-1).

## 7. Files changed (worktree, uncommitted)

Modified: `crates/cli/src/main.rs` (SVS routing, probe report, `gen-svs`, SVS sidecar
suppression), `crates/core/src/{bigtiff,ome_writer}.rs` (writer generalisation),
`crates/core/src/{convert_bf,convert_fl,report}.rs` (`source_format`/`adapter_version`),
`crates/core/src/jpeg/{decoder,mod}.rs` (marker probes + `decode_ex(force_rgb)` +
`tiff_jpeg_color`), `crates/core/src/lib.rs` (module wiring), `crates/wasm/src/lib.rs`
(SVS routing, adapter in checkpoints/resume refusal), `static/tools/slide-transform/`
(engine sniff + manifest + rebuilt byte-identical wasm), `tests/browser/slide_tools_c2/run_parity.js`
(`--svs` mode). New: `crates/core/src/{tiff_read,svs,convert_svs,svs_fixture}.rs`,
`crates/core/tests/svs.rs`, `tests/js/tools-svs-input.test.ts`, `tests/test_slide_svs_viewer.py`,
this report. CLI fix this session: SVS conversions no longer emit empty
`<output>.associated/` sidecars (adapter reports associated images with length 0 —
they are detected, not exported).

## 8. What is not verified / open issues

- **No real YCbCr-payload SVS sample** in the CC0 set: the YCbCr accept path (photometric 6,
  SOF-subsampling propagation, default colourspace) is covered only by synthetic fixtures.
- **Cross-decoder decode deltas** (libjpeg-turbo vs imagecodecs) on Q=30 payloads reach
  177 at isolated pixels; exactness rests on byte identity, which is the strongest
  property a passthrough can offer, but a pixel-diff gate at tolerance 0 through two
  different decoders is not achievable.
- **SVS resume refusal is not a C2 fault scenario**: the adapter-mismatch refusal is
  covered by wasm unit tests and runner logic; a browser fault row (journal adapter
  vs staged copy) would strengthen the matrix.
- **SNAP/`gen-svs` big-endian + BigTIFF fixtures** are exercised in-process (Rust), not
  through the browser; the browser path was verified with the real classic little-endian
  sample and synthetic files natively.
- **Associated-image naming**: for unusual Aperio exports, thumbnail classification
  relies on description markers/NewSubfileType; an exotic file could classify a
  thumbnail as `label` — it is still excluded from the output, so the failure mode is
  cosmetic (metadata naming in the report only).
- Tool-page accept list / UI labels for the new input kind are deliberately untouched
  (concurrent worktree); the reviewer wires them separately. Until then the page routes
  TIFF inputs through the same brightfield flow (`magicModality` → `brightfield`).
- Not run: whole `tests/test_slide_transform_core.py` (per task rules; C2 + targeted
  pytest cover the affected paths), QuPath gate on the bf-classic output (OpenSlide
  gate covers classic), F2 (JPEG 2000) by design.
