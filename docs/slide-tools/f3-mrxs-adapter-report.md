# F3 — Standard MRXS (3DHISTECH MIRAX) full-bundle input adapter report

Phase F3 of `docs/slide-tools/tool-ux-and-format-expansion-implementation-plan-20261003.md`
(§5.1/§5.4, §6). Branch `mrxs-wt` (from `fmt-integ` @a04a5d2). **Status:
implemented and gated; uncommitted work in the worktree** (no commit per task
rules).

**Size revision (reviewer round 2)**: preserve outputs shrank ~2.5× by (a)
deduplicating fill tiles — one shared JPEG payload per distinct
`IMAGE_FILL_COLOR_BGR`, written once right after the BigTIFF header, with
every sparse tile's TileOffsets/TileByteCounts entry referencing it (valid
TIFF; resume stays byte-identical because the payload placement is
deterministic and a resumed run derives the references instead of
rewriting), and (b) switching the preserve compose encode from the earlier
RGB q95 4:4:4 draft to **YCbCr 4:2:2 q96** (fingerprint
`mirax-preserve-compose:q96:y422:hstd:v1`) — the smallest setting whose L0
tissue per-channel mean stays ≤ ~2 on all three public samples, chosen by
the encode bench in §4.5. CMU-1 preserve: 2.67 GB → **1.075 GB**, 8 m 10 s
→ **2 m 22 s**, RSS 38 → 43 MiB. Details and gates below. Structure source: <https://openslide.org/formats/mirax/> (structure
only; OpenSlide used as the independent ROI oracle, never ported).

Code:

- `slide-transform-core/crates/core/src/bundle.rs` — multi-member
  random-access bundle abstraction (`BundleFs`; native `DirBundle`, test
  `MemBundle`); traversal-safe by construction (members resolved by exact
  flat name, never path-joined)
- `slide-transform-core/crates/core/src/mirax.rs` — bounded Slidedat.ini /
  Index.dat / camera-position parsing, zoom-level geometry, estimate
- `slide-transform-core/crates/core/src/convert_mirax.rs` — mosaic
  composition → both brightfield profiles × both encodings; `compose_region`
  (bounded ROI composer used by the validation gates)
- `slide-transform-core/crates/core/src/inflate.rs` — bounded raw-DEFLATE +
  zlib decompressor (the `StitchingIntensityLayer` position blob), no crates
- `slide-transform-core/crates/core/src/mirax_fixture.rs` +
  `crates/core/tests/mirax.rs` + CLI `gen-mrxs` — synthetic bundles and the
  17-test corpus
- `slide-transform-core/crates/wasm/src/lib.rs` — bundle host callbacks
  (`stHostBundleCount/Name/Size/ReadInto`), `probeBundle` /
  `convertProfileEncodedBundle` / `convertResumeProfileEncodedBundle`
- `static/tools/slide-transform/{engine,runner,worker}.js` — bundle sniff /
  manifest / member-by-member OPFS staging / resume verification (tool-page
  UI files untouched per task rules)
- `tests/browser/slide_tools_c2/run_parity.js --mrxs`, `run_faults.js`
  (+4 rows), `tests/js/tools-mrxs-input.test.ts` (19),
  `tests/test_slide_mirax_viewer.py` (3)
- `slide-transform-core/crates/core/src/jpeg/encoder.rs` — RGB-JPEG mode
  (Adobe APP14 transform 0, ids R/G/B) for the compose re-encode

Evidence: `.gate-tmp/f3/` (ROI ground truth + comparisons), QuPath probes in
`.gate-tmp/f3/qupath*`, C2 outputs in `.gate-tmp/slide-tools-c2/browser/`.

## 0. Verdict

| Requirement (plan §5.4) | Status |
|---|---|
| Multi-member random-access source, no whole-file reads (native dir; wasm host callbacks) | **PASS** — every access is `read_member_at(member, offset, len)`; no-whole-file gate PASS |
| Bounded Slidedat.ini / Index.dat parsing with loop/overflow/bounds checks | **PASS** — 17-test corpus incl. page-chain loop, OOB pointer, image-interval OOB, oversize counts |
| Camera positions: VIMSLIDE buffer, compressed `StitchingIntensityLayer`, synthesised fallback | **PASS** — all three on real samples (CMU-1×2 VIMSLIDE, Mirax2.2-1 zlib+deflate) |
| Zoom-level geometry (dims/downsamples), overlaps, fill colour, sparse/missing tiles | **PASS** — level dims byte-exact vs OpenSlide on all 3 samples (incl. its integer-truncation base-extent quirk); sparse fill counted + warned, never silently white |
| Compose output tiles from source images at true positions | **PASS** — level-0 composition **pixel-exact vs OpenSlide** on all 3 samples (masked mean 0.0, eq 100 %) |
| Bad index / missing image / decode failure are typed errors, never silently white | **PASS** — typed codes with member names in the messages |
| Brightfield only (fluorescence typed refusal); no cropping; MPP/objective from Slidedat else unknown | **PASS** |
| Both output profiles × both encodings; honest preserve semantics + result field | **PASS** — `composed` summary in every result (mode, fingerprint q95 RGB 4:4:4, tiles composed/filled); compact = locked U3 params |
| Levels: source zoom levels where true downsamples | **PASS** — all source levels kept (10 on CMU-1; halving verified from the MPP sequence) |
| Associated label/macro/thumbnail not exported | **PASS** — detected, reported, warning `mirax_associated_not_exported` |
| Adapter id `mirax-bundle` + version in result/provenance/checkpoints; resume refuses on adapter or manifest mismatch | **PASS** — C2 rows `mrxs-adapter-change-refused`, `mrxs-source-digest-changed-refused` |
| CLI `probe|convert <path.mrxs>` | **PASS** (+ `gen-mrxs` fixture writer) |
| Bundle source manifest (normalised paths, sizes, digests, totals, root digest, entry, adapter id/version); traversal/duplicate/case/missing-member rejection before any large copy | **PASS** — vitest 19; missing-member errors list exactly what is needed |
| Member-by-member single-buffer copy into OPFS; per-member progress; incomplete copy never a prepared job; resume re-verifies manifest from OPFS; cleanup deletes members | **PASS** — C2 row `mrxs-interrupted-member-copy-not-prepared`; discard/cancel removes the whole job dir incl. `bundle/` |
| Browser == native parity on CMU-1-Saved-1_16, both profiles | **PASS** — byte-equal (`MRXS PARITY PASS`) |
| Real-sample ROI comparison vs OpenSlide incl. seams/overlap zones | **PASS** — §4.2 (L0 exact; reduced levels bounded, tolerance stated) |
| Mirax2.2-1 | **PASS** — probe + 30-ROI bounded composition checks (compressed positions decoded by the in-core inflate) |
| QuPath strict gate on one bf-ome | **PASS** — 0.6.0-rc5 AND 0.7.0, 6 resolutions, Bio-Formats, RGB; region compare 12/12 maxdiff 0 |
| Platform `slide_io.open_slide` pytest (MRXS_SAMPLE env, repo-relative default, skip when absent) | **PASS** — 3 tests |
| Non-regression pins | **PASS** — KFB-1 ×4, SVS ×2, smoke ×2 unchanged (§4.6) |

## 1. Design

### 1.1 Bundle model and source abstraction

A logical slide = `<stem>.mrxs` + the same-name directory
(`<stem>/Slidedat.ini`, `<stem>/Index.dat`, `<stem>/DataNNNN.dat`). The core
never sees filesystem paths: `BundleFs::members()` lists flat names + sizes
and `read_member_at()` is the only read. `DirBundle` (native) maps
`<stem>.mrxs` + `<stem>/*`; the wasm `HostBundle` reads OPFS members through
four host callbacks (≤1 MiB per call, straight into wasm memory like the
single-file bridge). Traversal is impossible by construction: a Slidedat
`FILE_i = ../evil.dat` resolves to no member → typed missing-member error
(tested). Data-file numbers are resolved to member indices at parse time.

### 1.2 Bounded parsing (`mirax.rs`)

- `Slidedat.ini` ≤ 1 MiB (OpenSlide's cap): INI subset, `HIER_0` must be
  `Slide zoom level`, per-level sections give concat exponent / overlaps /
  MPP / DIGITIZER / IMAGE_FORMAT (JPEG only) / fill colour; the MPP sequence
  must halve (~2×) per level — that is the "true downsample" test for which
  levels are kept.
- `Index.dat` (version `01.02` + SLIDE_ID check, unaligned 32-bit LE): the
  hier root table → per-level record `0, page-ptr` → page chains
  `[len][next][len × (image_index, offset, length, fileno)]`. Page chains
  are walked with a visited-set (loops = typed error), page/pointer/image
  bounds are checked, `fileno < FILE_COUNT`, image intervals bounds-checked
  against the member's size, `x % concat == 0` enforced, caps on pages,
  items/page, images, total placements (2 M) and members (8 192).
- Nonhierarchical records locate the camera-position buffer:
  `VIMSLIDE_POSITION_BUFFER` (plain 9-byte entries: flag∈{0,1}, i32 x, y),
  `StitchingIntensityLayer` (zlib/DEFLATE — decoded by the in-core
  `inflate.rs`, exact-output-size enforced, adler32 verified), or neither →
  nominal positions synthesised from the overlap (OpenSlide's fallback).
  Position values are stored in original camera units and multiplied by the
  level-0 concat factor on read (OpenSlide's rule).
- Level extents reproduce OpenSlide's formula **including its integer
  truncation** (`base += (image_w - overlap) as f64 as i64` per column) —
  without it CMU-1-Saved-1_16 reports 7 439 px instead of 7 436.

### 1.3 Composition

Placements are computed exactly as OpenSlide's grid model: sub-tile walk
(`tiles_per_image²` per image), camera position + intra-photo offset,
`pos0/concat`, activity gating at zoom>0, (0,0)-coordinate skip at zoom 0,
paint order sorted by nominal grid coordinates (stable). Output tiles are
256×256; a per-level CSR bucket index maps placements→tiles; an LRU image
cache (32 MiB budget) decodes each source image once; each output tile is
composed with integer snapping (round-half-away-from-zero of the fractional
position) and re-encoded. Fractional-tail sub-tiles that overrun the source
edge by 1 px paint only the visible part (cairo EXTEND_NONE semantics).

**What `preserve-source-v1` means for MRXS** (stated honestly): an MIRAX
level is a mosaic of overlapping camera images at fractional positions, so
no output tile is ever a byte-copy of a source image. Preserve therefore
means: compose at the true positions (level-0 composition is pixel-exact vs
OpenSlide), then re-encode with the documented high-fidelity setting
**YCbCr 4:2:2, quality 96, standard Annex-K Huffman**, fingerprint
`mirax-preserve-compose:q96:y422:hstd:v1` (chosen by the §4.5 bench; the
(2,1) TIFF layout is the same one the KFB outputs use and Bio-Formats
renders correctly — an early YCbCr-4:4:4 draft rendered as garbage there
and an RGB-q95-4:4:4 draft tripled sparse-slide sizes). Tiles no placement
touches are pure fill colour and are **deduplicated**: one shared payload
per distinct `IMAGE_FILL_COLOR_BGR` (usually one), written right after the
16-byte header; every such tile's record references it (valid TIFF —
TileOffsets entries may repeat; the structural validator accepts it and
every reader below decodes it). Every result carries a
`composed` summary (mode, fingerprint, quality, sampling, huffman,
`tiles_composed`, `tiles_filled`) that the UI can show; the OME-XML
provenance and the classic description JSON carry the same facts.
`compact-jpeg-v1` composes identically and re-encodes with the locked U3
parameters (q80 4:2:0). `StrictLossless` is refused before any output byte
(composition + JPEG re-encode is inherently lossy for this format — no
byte-passthrough path exists, and none is claimed).

Sparse areas (camera positions without images) are filled with the level's
`IMAGE_FILL_COLOR_BGR` — the colour comes from Slidedat, and the count is
reported (`tiles_filled` per level, warning `mirax_sparse_fill`); OpenSlide
renders these transparent, so ROI comparisons are alpha-masked. Every tile
is emitted, so the output is dense.

### 1.4 Browser bundle source

`engine.js`: `sniffMrxBundle` / `planMrxBundle` — everything is decided
BEFORE any large copy: entry presence, stem derivation, name normalisation
(`/`-separated, no `..`/`\`/`:`/empty segments), duplicate and case-conflict
rejection (flat storage collision), member-count caps (8 192 total, 4 096
data files), bounded Slidedat parse (≤1 MiB), and the required-member list
(entry + Slidedat + INDEXFILE + every `FILE_i`). Only `.mrxs` or a single
`.dat` → typed `unsupported_input` listing exactly what a full bundle needs.
`Sha256` (FIPS vectors vitest-checked) digests members while copying;
`bundleRootDigest` = sha256 over `path\0size\0sha256\n` lines (any path,
size or byte change flips it).

`worker.js`: `stage-bundle` copies member-by-member with ONE reused BYOB
buffer (transferred per read) into `bundle/<stem>/…` (OPFS names cannot
contain `/`, so member paths nest), truncating any interrupted remains,
then writes `manifest.json`. `openBundle` re-opens members from the
manifest alone — **resume never needs the original folder handle**;
`verify-bundle` re-hashes every member and refuses on any mismatch
(`source_changed_refuse_resume`). Cleanup deletes the whole job directory
including `bundle/`. `prepareBundle` (runner) mirrors single-file `probe()`:
sniff → disk gate → staged copy (fault-injectable) → wasm `probeBundle` on
the staged members → encoding/output-profile checks → estimate disk gate →
`prepared` record (`bundle: true`, manifest, `sourceAdapter:
mirax-bundle`). A crashed copy leaves `staging` — never a resumable job —
and the next runner sweeps it (C2-proven).

### 1.5 Two latent bugs the gates caught (fixes included)

1. **CLI `statvfs` FFI struct was 8 bytes short** (`f_flag` + spares
   missing): glibc wrote past the struct and smashed the caller's stack.
   Latent for every existing caller (their frames had slack); the MRXS
   path segfaulted. Fixed with the full 96-byte layout + a compile-time
   size assertion.
2. **A one-character typo introduced into the YCbCr conversion**
   (`b_cr[r]` for `b_cr[b]`) would have silently corrupted every re-encoded
   KFB edge tile; the pinned KFB hashes caught it before anything shipped
   (all four pins restored exactly after the fix).

## 2. Support matrix (probe = authoritative; both outputs share one accept rule)

| Input | Verdict | Reason / detail |
|---|---|---|
| Standard MRXS brightfield bundle, JPEG images, VIMSLIDE_POSITION_BUFFER (CURRENT_SLIDE_VERSION 1.9) | **accepted** | CMU-1, CMU-1-Saved-1_16 converted fully; all levels kept |
| Same with `StitchingIntensityLayer` compressed positions (≥2.2) | **accepted** | Mirax2.2-1: in-core zlib/DEFLATE decode, dims exact |
| Bundle without any position record | **accepted** | positions synthesised from the nominal overlap (OpenSlide's fallback) |
| Sparse camera positions / interior holes | **accepted** | fill colour from Slidedat, counted, warned; never silently white |
| Fractional / non-integer placement (odd camera units, 4:4:4 concat at reduced levels) | **accepted** | integer snapping, deterministic; L0 exact, reduced levels bounded (§4.2) |
| Missing entry / Slidedat.ini / Index.dat / any referenced data file | **rejected** | typed `unsupported_input` naming the missing members, before any copy |
| `FILE_i` path traversal (`../evil.dat`) | **rejected** | member names resolve exactly — nothing outside the bundle is reachable |
| Duplicate members / case conflicts / oversize counts | **rejected** | vitest-covered typed reasons |
| Index page-chain loops / OOB page or image pointers / image intervals beyond member size | **rejected** | typed `invalid_tile_index` / `tile_payload_out_of_bounds` with the offending values |
| `IMAGE_FORMAT` PNG / BMP24 | **rejected** | "仅支持 JPEG 数据（需要独立解码器）" |
| `SLIDE_TYPE` ≠ BRIGHTFIELD (fluorescence / multi-channel) | **rejected** | "荧光/多通道 MRXS 不在明场支持集（需要独立通道合同）" |
| HIER_0 not `Slide zoom level` / non-halving MPP sequence / concat or grid anomalies | **rejected** | typed variant errors (OpenSlide makes the same HIER_0 demand) |
| Corrupt JPEG payload / progressive or arithmetic source images | **rejected** | typed `jpeg_decode_failed` / variant, never a white tile |
| `fl-ome` profile or `strict-lossless` policy on MRXS input | **rejected** | modality mismatch / `pixel_policy_violation` before any output byte |

Not verified on real data (no sample): BMP24 is asserted rejected from
Slidedat text only; `Mirax2-Fluorescence-*` refusal path is fixture-tested
(SLIDE_TYPE rewrite), not run on the real fluorescence bundles.

## 3. Commands and versions

Worktree root; `TMPDIR=$PWD/.gate-tmp COLUMNS=200`; heavy runs under
`systemd-run --user --scope -q -p MemoryMax=4G -p MemorySwapMax=0`.
Samples: `$MRXS_SAMPLES` (default `<suite>/.testdata/openslide/mirax/`, outside the repo)
(public CC0 / distributable; never committed).

```text
slide-transform probe|convert <stem>.mrxs …            # bundle routing
slide-transform gen-mrxs <dir> [--images-x N --images-y N --divisions N --levels N --sparse]
cargo test --release --workspace --features slide-transform-core/fixtures   # in slide-transform-core/
bash scripts/build_slide_transform.sh
npx vitest run --dir tests/js
PYTHONPATH=.:tests .venv/bin/python -m pytest tests/test_slide_mirax_viewer.py -q   # MRXS_SAMPLE=<dir>
node tests/browser/slide_tools_c2/run_parity.js --mrxs <bundle-dir> --port 8990
node tests/browser/slide_tools_c2/run_faults.js --port 8991
node tests/browser/slide_tools_c2/test_no_whole_file.js
bash scripts/qupath-probe/run_probe.sh <qupath-root> <out> <bf-ome>.ome.tif 0.3 0.7 \
  --expect-resolutions 6 --expect-server-substring BioFormats --expect-rgb
python scripts/qupath-probe/compare_regions.py <out>/probe.json <out>/regions <classic>.tif <out>/rc.json
# ROI ground truth: .gate-tmp/f3/{gen_rois,export_gt,compare_roi}.py + mrxs_roi example
```

Versions: rustc/cargo 1.98.1, wasm-bindgen 0.2.129, OpenSlide 4.0.1 /
openslide-python 1.4.6 (oracle), tifffile 2024.5.22, imagecodecs 2026.8.16,
QuPath 0.6.0-rc5 + 0.7.0 (Bio-Formats 8.4.0), Playwright Chromium (C2).

## 4. Results

### 4.1 Input samples (sha256 of the `.mrxs` entry)

| Sample | entry sha256 (16) | bundle bytes | levels | position source |
|---|---|---:|---:|---|
| CMU-1-Saved-1_16 (CC0) | `f5c96ff978fef727…` | 5 686 605 | 6 | VIMSLIDE |
| CMU-1 (CC0) | `ecbda43ab9f5ae67…` | 565 102 675 | 10 | VIMSLIDE |
| Mirax2.2-1 (distributable) | `76a1662490e85a64…` | 2 915 564 670 | 10 | StitchingIntensity (zlib) |

### 4.2 Composition fidelity vs OpenSlide (bounded ROIs, alpha-masked)

`compose_region` (the conversion's exact placement/rounding rules) vs
OpenSlide on the original bundle; 18–30 ROIs per sample across all levels,
placed inside the source bounds (seams and overlap zones included; ROIs in
fully-empty regions are reported as such):

| sample | level-0 | reduced levels (worst) |
|---|---|---|
| CMU-1-Saved-1_16 | mean **0.0**, eq 100 % (3/3 ROIs) | mean 8.94, p99 42, max 118 |
| CMU-1 | mean **0.0**, eq 100 % (3/3) | mean 8.18, p99 44, max 112 |
| Mirax2.2-1 | mean **0.0** (8/8 tissue ROIs, thumbnail-selected) | mean 15.60, p99 69, max 159 |

**Tolerance and why**: level 0 is exact (same JPEG bytes through decoders
proved bit-exact, integer placements). Reduced levels place fractional
sub-tiles (camera units ÷ concat, e.g. ÷16 with 7.5-px overlaps at
1/16-scale) where OpenSlide renders through cairo with sub-pixel source
positioning while the adapter snaps to the integer grid; the difference is
bounded by ~half a pixel of texture shift, hence mean ≤ 16 / p99 ≤ 70 /
max ≤ 160 across every measured ROI. Env-gated Rust test
(`MRXS_GT`) re-checks this automatically (worst L0 ≤ 0.6, worst L1+ ≤ 16).

### 4.3 Converted outputs — before/after the size revision

CMU-1-Saved-1_16, all levels, bounded ROIs (same ROIs both rounds):

| output | reader | bytes (before → after) | worst ROI mean | notes |
|---|---|---|---:|---|
| bf-ome preserve | tifffile | 17 708 799 → **6 561 133** (37 %) | 4.66 → 4.67 | composition exact + q96 4:2:2 re-encode |
| bf-classic preserve | OpenSlide | 17 708 095 → **6 560 427** | 4.18 → 4.20 | 6 levels, dims + MPP identical |
| bf-ome compact | tifffile | 6 533 043 → **2 963 580** (45 %) | 5.87 → 5.86 | compact fill tiles dedupe too; U3 params unchanged |

Fill/real tile accounting (Saved-1_16, per output): 2 465 tiles total, of
which **2 167 pure-fill tiles now share ONE payload** (L0: 1 830 total /
1 629 fill / 1 629 deduped). Classic-chain IFD walk: L0 1 830 tile records →
202 distinct payloads. Full CMU-1 bf-ome preserve (native):

| metric | before (RGB q95 4:4:4, no dedupe) | after (y422 q96 + dedupe) |
|---|---:|---:|
| output bytes | 2 668 229 688 | **1 075 354 246** (40 %) |
| wall | 8 m 10 s | **2 m 22 s** |
| max RSS | 38 MiB | 43 MiB |
| tiles composed / filled / deduped | 491 865 / 438 824 / 0 | 491 865 / 438 824 / **438 824** |
| real (tissue) tiles | 53 041 payloads | 53 041 payloads + 1 shared fill payload |

Dedupe validity was re-gated end to end: tifffile opens both layouts with
all levels; OpenSlide opens the classic output with all 6 levels and
identical dimensions; `slide_io.open_slide` opens the bf-ome output
(level count/dims equal, L0 tissue ROI mean 1.77); the structural validator
(wasm finalizeValidate + the Rust `validate_output`) accepts the shared
offsets and counts all 2 465 tile records; resume after interruption is
byte-identical to an uninterrupted run on a sparse fixture with fill tiles
on both sides of the cut (`fill_dedupe_resume_is_byte_identical`, two cut
points). The output no longer exceeds the source payload multiple: the
disk estimate now bounds `payload × 2 + 16 B/tile + overhead` (the measured
inflation is ~1.4×); the runtime output cap remains the hard guard.

### 4.4 QuPath / OpenSlide interop

QuPath 0.6.0-rc5 and 0.7.0 both open the deduped bf-ome via Bio-Formats:
resolutions 6 = levels 6, RGB, UINT8, mpp 3.7172/3.7163, magnification 20,
24 region reads — strict gate exit 0 on both (re-run after the y422 q96 +
dedupe revision). `compare_regions` against the classic output: **12/12
regions max_abs_diff 0** (3 textured tissue boxes per version) — Bio-Formats
follows the shared TileOffsets exactly. History: the first interop attempt
(YCbCr 4:4:4 tiles, photometric 6) rendered every region flat magenta in
Bio-Formats, which forced an RGB draft; YCbCr 4:2:2 — the KFB layout — is
rendered correctly and is what ships.

### 4.5 Preserve-encode bench (reviewer round 2)

Method: 8 tissue L0 ROIs per sample (thumbnail-selected), each composed
once (composition is pixel-exact vs OpenSlide — all 24 ROIs measure mean
0.0, per-channel 0.0), split into 256×256 tiles, encoded with each
candidate, decoded back, per-channel error vs the composed pixels = the
encode generation loss the viewer sees (L0). `mrxs_bench` example;
`B/tile` relative sizes are the decision numbers.

| cfg | Saved-1_16 mean R/G/B | CMU-1 mean R/G/B | Mirax2.2-1 mean R/G/B | B/tile vs current |
|---|---|---|---|---:|
| RGB q95 4:4:4 (draft) | 1.45 / 2.06 / 2.03 | 0.76 / 1.26 / 1.38 | 0.83 / 1.36 / 1.41 | 100 % |
| **YCbCr 4:2:2 q96 (LOCKED)** | **1.89 / 1.53 / 1.80** | **1.24 / 0.97 / 1.86** | **1.28 / 1.05 / 1.90** | **~50 %** |
| YCbCr 4:2:2 q95 | 2.19 / 1.82 / 2.09 | 1.34 / 1.06 / 2.03 | 1.43 / 1.17 / 2.08 | 45 % |
| YCbCr 4:2:2 q92 | 2.97 / 2.63 / 2.85 | 1.54 / 1.25 / 2.35 | 1.73 / 1.44 / 2.49 | 36 % |
| YCbCr 4:2:2 q90 | 3.46 / 3.13 / 3.32 | 1.64 / 1.33 / 2.49 | 1.87 / 1.57 / 2.68 | 33 % |
| YCbCr 4:2:0 q90 | 3.85 / 3.33 / 3.60 | 1.85 / 1.46 / 3.10 | 2.22 / 1.76 / 3.40 | 31 % |
| RGB q92 / q90 | 2.27 / 3.25 / 3.21 ; 2.78 / 4.01 / 3.96 | ≤ 1.9 | ≤ 2.2 | 81 % / 73 % |

Decision: the reviewer criterion is per-channel mean ≤ ~2 on tissue. The
requested q90/q92 candidates measure 2.4–3.5 on the re-saved 1/16 sample
(their B channel exceeds ~2 on ALL three samples) — they do not qualify;
q96 is the smallest measured setting that stays ≤ 2 on every sample and
channel (worst 1.90) at ~50 % of the draft's bytes, and it passes the
QuPath strict gate on both versions. Compact (`cj1:q80:420:hstd:v1`) is
unchanged. Full-file confirmation: Saved bf-ome 6.56 MB, CMU-1 1.075 GB
(§4.3).

### 4.6 Browser

| gate | result |
|---|---|
| MRXS parity (Saved-1_16, both profiles) | **PASS** — browser == native byte-equal (`42f3c650…` ome, `77d3b1b8…` classic, after the size revision) |
| Fault matrix | **39/39** (35 prior + 4 new: converts-matches-native, interrupted-member-copy-not-prepared, source-digest-changed-refused, adapter-change-refused) |
| no-whole-file gate | PASS (bounded slices only; bundle copy is a BYOB single-buffer reader) |
| vitest `tests/js` | **681 passed / 43 files** (19 new in `tools-mrxs-input.test.ts`) |
| Manifest model | per-member sha256 + sizes + root digest; resume re-verifies from OPFS without the original handle (C2-proven) |

### 4.7 Non-regression (pinned hashes, native CLI)

| artifact | pin | now |
|---|---|---|
| KFB-1 bf-ome / bf-classic preserve | `374c70c8…` / `385a59c6…` | equal |
| KFB-1 bf-ome / bf-classic compact | `86131cab…` / `037c552d…` | equal |
| SVS CMU-1 bf-ome / bf-classic | `9d1ac1e809ae0402…` / `00198666461c4d5d…` | equal |
| C2 smoke preserve / compact | `6e8744f9…` / `7e2f4f82…` | equal |
| Rust workspace (`--features …/fixtures`) | 99 → | **145 passed, 0 failed** (19 mirax + 6 inflate new; lib 25 incl. inflate) |
| pytest compact / svs-viewer / mirax-viewer / capability | 1 / 2 / 3 / 13 passed | equal or new (re-run after the revision) |

## 5. Memory

Native CMU-1 full bundle convert under `MemoryMax=4G` (after the size
revision): max RSS **43 MiB**, wall 2 m 22 s (before: 38 MiB / 8 m 10 s —
the y422 encode is also ~3.5× faster than RGB 4:4:4; the extra 5 MiB is the
per-level bucket index for 368 501 L0 tiles). Placements are per-level, the
image cache is 32 MiB-bounded, the canvas is one tile, scratch = the
offset/count streams. Saved-1_16 native convert: 1 s. Browser conversion
runs inside the existing single-worker profile caps; the member copy uses
one 4 MiB buffer. No OOM events in any gated run. Since the independent
review the adapter additionally holds these numbers under the **saver**
budget itself and refuses over-budget metadata pre-allocation — §8.1.

## 6. Not verified / open issues

1. **No real Mirax2-Fluorescence sample was opened** — the fluorescence
   refusal is fixture-tested (SLIDE_TYPE rewrite), not proven against the
   distributable fluorescence bundles.
2. **PNG/BMP24 data** are rejected from the Slidedat declaration; no real
   Mirax2.2-4-BMP/PNG bundle was available to confirm the declaration
   matches the payload on real files.
3. **Reduced-level placement stays integer-snapped**: an exact cairo-style
   sub-pixel renderer could drive the L1+ residual from mean ~8–16 toward
   0, at the cost of a resampling stage in the composition; deliberately
   not done (documented tolerance instead).
4. **Chromium renderer crash observation**: killing the worker with
   `self.close()` while a bundle sync-access handle is open mid-copy takes
   down the whole renderer (measured, reproducible). The interrupted-copy
   fault therefore injects a hard IO error from the middle of the copy loop
   — the same user-visible contract (record stays `staging`, never
   prepared, swept on next start) without the renderer loss. Noted for the
   C0 ADR §7 reload-race follow-up.
5. **Tool-page UI wiring** (folder picker in `templates/tools_slides.html`
   / `tools-slides*.js`) is the reviewer's, per task rules — the runner API
   (`prepareBundle`, bundle `startJob`/`resumeJob`, `sourceAdapter`
   `mirax-bundle`) is complete and C2-covered.
6. ~~Big sparse re-saves inflate under preserve~~ — resolved in the size
   revision: fill tiles are deduplicated to one shared payload and the
   compose encode is y422 q96 (CMU-1: 1.075 GB from a 565 MB bundle,
   measured inflation ~1.4×; estimate bound `payload×2 + 16 B/tile`).
   Remaining note: real-tile payload still dominates for dense slides;
   compact remains the smaller-output choice.

## 7. Files changed (worktree, uncommitted)

Modified: `slide-transform-core/crates/{cli/src/main.rs, core/src/{lib,
report,convert_bf,convert_fl,convert_svs,jpeg/encoder,bigtiff,ome_writer}.
rs, core/examples/parity.rs, wasm/src/lib.rs}` (one struct field init each
in convert_bf/fl/svs — `composed: None`; encoder gains the opt-in RGB mode
— default path byte-identical, proven by the pins; bigtiff/ome_writer gain
`write_payload`/`write_tile_ref` for shared-payload tiles), `static/tools/
slide-transform/{engine,runner,worker}.js` + rebuilt wasm/manifest/
`.d.ts`, `tests/browser/slide_tools_c2/{harness.html,harness.js,
run_faults.js,run_parity.js}`. New: `crates/core/src/{bundle,mirax,
convert_mirax,inflate,mirax_fixture}.rs`, `crates/core/examples/{mrxs_roi,
mrxs_bench}.rs`, `crates/core/tests/mirax.rs`,
`tests/js/tools-mrxs-input.test.ts`, `tests/test_slide_mirax_viewer.py`,
this report.

## 8. Review fixes (§1, §3 — independent review 2026-10-03)

Fixes for `docs/slide-tools/ux-formats-independent-review-20261003.md`
§1 (P1: probe/convert allocations unbounded by the resource profile) and
§3 (P2: the GT pixel gate passed with missing/empty ROI material). §2
(bundle identity on resume) is fixed separately in the `fixbundle-wt`
worktree.

### 8.1 §1 — memory budget model

The adapter's working set is now bounded by the host's actual resource
budget, checked BEFORE each large allocation
(`crates/core/src/budget.rs::MemBudget`):

- **Host budget in**: native CLI `--memory-budget BYTES` on
  `probe`/`convert` (conservative default = the browser `saver` profile's
  **192 MiB**); wasm `probeBundle(budgetBytes)` /
  `convertProfileEncodedBundle(…, budgetBytes)` /
  `convertResumeProfileEncodedBundle(…, budgetBytes)` — `worker.js` passes
  the active profile's `budgetBytes`, and the probe-bundle request resolves
  a profile even at prepare time (`runner.js` sends `profileId`; the worker
  falls back to saver). A 24 MiB reserve (decoder slack, allocator
  overhead, process baseline) is subtracted, so the saver cap is
  176,160,768 B.
- **Charges** (overflow-checked estimates, refused before allocating):
  camera positions — raw buffer 9 B/entry + tuple table 16 B/entry
  (synthesised fallback: 16 B/entry), activity marks 1 B/entry, index
  page-chain visited set 64 B/page, image records 64 B/record; per convert
  level — placement sort copies (order element 56 B + stable-sort temporary
  28 B + output vec 40 B per placement upper bound), CSR `ranges`
  32 B/placement, `counts/starts/fill` 12 B/tile (+4), `items` 4 B × the
  tile-span bound, image cache + **decoded-pixel budget** 6 B/px charged
  before every JPEG decode (trued up to the real size, released on
  evict/drop — a single oversized member is refused pre-decode), tile
  canvas/encode 1.5 MB.
- **Refusal**: new `ErrorCode::ResourceLimitExceeded` with stable code
  **`resource_profile_insufficient`** — deliberately the code the tool page
  already maps (`engine.js ERROR_CODES.RESOURCE_PROFILE_INSUFFICIENT`,
  `tools.err.resource_profile_insufficient`), so browser and CLI surface
  the same typed error; no new i18n key.

**Reviewer negative, reproduced** (50,883 B bundle, INI
IMAGENUMBER 5000×5000, `CameraImageDivisionsPerSide = 1`, no position
buffer → nominal fallback ⇒ 25,000,000 declared positions):

| | old code | new code |
|---|---|---|
| `probe`, no cgroup | exit 0, **peak RSS 394,084 KB** | exit 1, typed `resource_profile_insufficient` |
| `probe`, `MemoryMax=192M` | **exit 137 (SIGKILL/OOM)** | exit 1, typed error (`已计 400000000 B` before any 400 MB allocation) |
| `convert`, `MemoryMax=192M` | OOM (probe runs first) | exit 1, typed error |

Regressions (committed): Rust
`large_grid_nominal_fallback_refused_within_budget` builds the reviewer's
bundle in code (same INI values, tiny members) and asserts the typed error
under the default saver budget AND a metered pre-allocation peak
< 64 MiB (per-thread counting allocator — fails on the old code, whose
probe exited 0 after committing ~400 MB); CLI script
`scripts/test_mrxs_memory_budget.sh` regenerates the bundle via
`gen-mrxs` + the INI rewrite and runs probe AND convert under
`systemd-run --user --scope -q -p MemoryMax=192M -p MemorySwapMax=0`,
asserting exit 1 + the typed JSON code and no kill signal (exit 137 on the
old code). `tests/js/tools-mrxs-memory-budget.test.ts` (4) pins the
worker/runner/`.d.ts` budget wiring.

**Real samples still convert under saver** (native, `MemoryMax=192M`):

| run | peak RSS | wall | output |
|---|---:|---:|---|
| CMU-1 probe | 7.0 MB | 0.01 s | — |
| CMU-1 convert bf-ome | 43.7 MB | 2 m 22 s | **1,075,354,246 B — unchanged** (§4.3) |
| Mirax2.2-1 probe | 11.2 MB | 0.03 s | — |
| Mirax2.2-1 convert bf-ome | 52.3 MB | 9 m 26 s | 5,453,168,505 B |
| CMU-1-Saved-1_16 convert bf-ome / bf-classic | — | 1 s | pins hold `42f3c650…` / `77d3b1b8…` |

No real sample needed a budget raise — the metadata working set of every
public sample is a few MB (declared camera grids of ~20 K positions, not
the synthetic 25 M).

### 8.2 §3 — GT gate fails closed

The checker is factored into `compare_gt_rois` (tests/mirax.rs) with
fail-closed rules; every violation is an error **naming the ROI**:
reference raw must exist and be exactly `w·h·3` bytes (no more silent
`continue`, no zip truncation), a mask sidecar must be exactly
`⌈w·h/8⌉` bytes with **non-trivial valid coverage (≥ 1 %** of the ROI —
an almost-fully-transparent mask verifies nothing), the ROI must be
in-bounds and compose, the output must be exactly `w·h·3` bytes, and at
least one level-0 AND one reduced-level ROI must actually have been
compared.

Committed negatives (reviewer's `missing-gt/rois.json` semantics: one L0 +
one L1 16×16 ROI; the source bundle is generated in code):

| negative | old gate | new gate |
|---|---|---|
| both raw files missing | **PASS** (`worst L0 mean 0.0000, worst L1+ mean 0.0000`) | `gt_gate_fails_when_reference_raw_files_are_missing` — FAILED naming `roi-l0-missing-l0` |
| both raw files 0 bytes | **PASS** | `gt_gate_fails_when_reference_raw_files_are_empty` — FAILED: `参考 raw 长度 0 ≠ w*h*3 = 768` |

Strict gate re-run on freshly regenerated real ROI material (same
`.gate-tmp/f3/{gen_rois,export_gt}.py` scripts, all levels, 256-px ROIs;
ROIs with 0 % opaque coverage — OpenSlide fully-transparent regions — are
dropped at generation time and counted, never silently compared):

| sample | ROIs compared (L0 / L1+) | worst L0 mean | worst L1+ mean | gate |
|---|---|---:|---:|---|
| CMU-1-Saved-1_16 | 3 / 15 (18 exported) | **0.0000** | 8.9373 | PASS |
| CMU-1 | 3 / 27 (30 exported) | **0.0000** | 8.1816 | PASS |
| Mirax2.2-1 | 1 / 18 (19 kept; 11 dropped at 0.0 % opaque) | **0.0000** | 15.5978 | PASS |

The numbers reproduce §4.2 (8.94 / 8.18 / 15.60) under the strict gate.

### 8.3 Files changed for this round

New: `slide-transform-core/crates/core/src/budget.rs` (MemBudget +
element-size constants), `scripts/test_mrxs_memory_budget.sh` (CLI cgroup
negative), `tests/js/tools-mrxs-memory-budget.test.ts` (budget wiring).
Modified: `crates/core/src/{error,lib,plan,mirax,convert_mirax}.rs`
(resource-limit code, budget account, probe/convert charges),
`crates/core/tests/mirax.rs` (metered allocator, large-grid negative,
strict GT checker + 2 negatives), `crates/cli/src/main.rs`
(`--memory-budget` on probe/convert), `crates/wasm/src/lib.rs` (budget
params on the three bundle entry points),
`static/tools/slide-transform/{runner,worker}.js` (budget plumbing only)
+ rebuilt wasm/glue/`.d.ts`/manifest, this report.

Gate re-run after the fixes (serial): Rust workspace
`--features slide-transform-core/fixtures` **148 passed** (mirax 22 =
19 + 3 new); vitest `tests/js` **748 passed / 46 files**; C2 smoke
preserve `6e8744f9…` / compact `7e2f4f82…` (pins), fault matrix **39/39**,
parity MRXS `42f3c650…`/`77d3b1b8…` + SVS Small-Region
`fcb6d171…`/`dcefe860…` + KFB-1 `374c70c8…` (pins), native pin re-check
KFB-1 classic `385a59c6…` / compact `86131cab…`, SVS CMU-1
`9d1ac1e8…`/`00198666…`, `test_no_whole_file.js` PASS, pytest
mirax/svs/upload-capability **18 passed**.
