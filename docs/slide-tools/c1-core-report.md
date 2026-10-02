# C1 — Shared Transform Core Report

Stage C1 of `docs/browser-slide-tools-and-baidu-plugin-agent-plan-20260929.md`,
built on the C0 spike and `docs/review-evidence/slide-tools/C0/acceptance.md`
hard requirements. Code:

- `slide-transform-core/` — Rust workspace (crates `core`, `cli`, `wasm`)
- `scripts/build_slide_transform.sh` — toolchain build
- `static/tools/slide-transform/` — wasm + JS glue + build-manifest
- `tests/test_slide_transform_core.py` — differential harness vs the Python oracle
- `scripts/c1_biginput.py` — >4 GiB proof driver
- Evidence: `.gate-tmp/slide-tools-c1/` (parity runs, stats, showinf logs, RERUN.md)

## 1. Executive summary

The shared transform core reaches **byte-level parity with the Python oracle**
for brightfield (whole-file sha256 equal, including re-encoded edge tiles)
and **full-tile payload + structural parity** for fluorescence (the OME-XML
differs only in the C1-required move of ExposureTime to `<Plane>`). The edge
quality gate passes with margin 0 because the hand-written JPEG codec is
bit-exact with Pillow/libjpeg-turbo in both directions (960/960 encode and
960/960 decode byte-equal in the parity matrix), which removes the C0 spike's
jpeg-encoder quality gap entirely. The codec itself is IJG-derived and ships
with an IJG attribution NOTICE (§6, corrected at review).

| Hard requirement | Status |
|---|---|
| Edge PSNR ≥ oracle−0.5 dB per tile, global max diff ≤ oracle | **PASS with margin 0** (edge tiles byte-equal; KFB-1: 627 edge tiles, maxdiff 42 = oracle 42) |
| jpeg-encoder IJG license review | **DONE** — dependency removed for parity reasons; own codec is IJG-derived, NOTICE shipped (§6) |
| ExposureTime on `<Plane>`, unit stays *assumed* ms | **DONE** — Channel no longer carries exposure; every `<Plane>` has `ExposureTimeUnit="ms"` |
| channel.json optional companion, body wins | **DONE** — display window absorbed into result metadata only; conflicts resolved in favour of the KFBF body |
| Brightfield SubIFD evaluation with evidence | **DONE** — decision: keep classic pyramid (§5) |

## 2. Architecture

- **`kfb`** — brightfield parsing: synthetic `kfb_bf_v1` contract + KF-BIO
  vendor layout (both ports of `kfb/parser.py` / `vendor_kfbio.py`), spilling
  tile records into per-level scratch grids (`paged_index.rs`, 32 B/cell +
  occupancy bitmap ≤ ~1.2 MiB) so memory is O(levels), not O(tiles).
- **`kfbf`** — fluorescence vendor layout (`vendor_kfbf.py` port): tagged
  header, 96 B pointer blocks / 48 B side records per cell, hybrid pyramid
  geometry (L1–L3 floor, L4–L8 = ceil(L0/256)·2^(8−L), L9+ floor), sparse
  cells allowed, L≥1 grid-extent must match the formula exactly. Paged cell
  store 24 B/cell.
- **`jpeg`** — hand-written baseline codec (see §4). `codecs` feature.
- **`bigtiff`** — brightfield classic multi-IFD writer (byte-layout port of
  `converter.py::_BigTiffPyramidWriter`), streaming offset/count arrays from
  scratch (12 B/tile).
- **`ome_writer`** — fluorescence OME-BigTIFF writer with SubIFDs (port of
  `converter_fl.py::_OmeBigTiffWriter`): top-level chain = level-0 channel
  IFDs, each carrying SubIFDs (tag 330 LONG8) to its levels 1..N; IFD order
  level-major; external segments laid out in tag order.
- **`plan` / `report` / `job` / `companion`** — `TransformPlan` (plan_version,
  input identity, output profile, pixel policy, resource limits, core
  version), `TransformResult` (output descriptor, size/hash, per-level
  stats, edge-region list, warnings, validation summary, IFD chain),
  `JobControl` (progress callback per tile-row/level with committed byte
  counts — checkpoint-friendly; cooperative cancel flag checked between
  tiles; wall-clock timeout), channel.json companion parser.
- **`cli`** — `slide-transform probe|convert|gen-kfb|gen-kfbf`, JSON output,
  typed error JSON, `.part` + rename atomic promote, no-clobber, disk-free
  guard via statvfs, associated-image sidecars `<out>.associated/*.jpg`.
- **`wasm`** — `probe()` / `convert(strict, channelJson)` over JS host
  callbacks with chunks bounded at 1 MiB (`stHostRead/Write/Scratch*`,
  `stHostProgress`, `stHostCancelled`). On wasm32 there is no clock
  primitive, so the wall-clock timeout is inert there and the host enforces
  time via the cancel flag (documented in `job.rs`).

## 3. Differential results

### 3.1 Codec parity matrix (foundation of the edge gate)

`.gate-tmp/slide-tools-c1/parity/` (`run_parity.py`, seeds 2024 + 777):
2 samplings × 44 size/quality cells × {encode, decode} vs Pillow 12.3.0
(libjpeg-turbo 3.1.4.1):

```
ENCODE byte-equal: 960/960
DECODE byte-equal: 960/960
```

Covered: gray/444/422/420, qualities 50–100, sizes 1×1…256×256 including
odd/edge sizes and partial MCU widths/heights.

### 3.2 Synthetic fixtures (Python `kfb/fixture*.py` vs CLI)

| fixture | result |
|---|---|
| BF 580×300, 256×256, 613×355, 1024×777, 300×300 | **whole-file sha256 equal** |
| FL 600×400 (sparse cell + cropped bottom row) | full-tile payloads byte-equal per (level,channel); structure equal (BigTIFF, OME, CYX (2,400,600), 3 levels); OME-XML differs only in ExposureTime location |

### 3.3 Real samples (aliases only; sha256 pinned in the harness)

| alias | bytes | full tiles equal | whole-file equal | RSS (KiB) | wall (s) |
|---|---|---|---|---|---|
| KFB-1 (brightfield, 34 013×46 152, 9 levels) | 221 189 354 | 31 650/31 650 + all 627 edge tiles | **yes** | 11 680 | 1.80 |
| KFBF-A (fluorescence, 6 ch) | 946 972 112 | 247 380/247 380 | n/a (ExposureTime move) | 5 664 | 4.79 |
| KFBF-B | 530 063 959 | 186 480/186 480 | n/a | 5 000 | 3.46 |
| KFBF-C | 481 661 346 | 151 008/151 008 | n/a | 4 804 | 2.79 |
| KFBF-D | 806 456 145 | 230 346/230 346 | n/a | 5 324 | 4.42 |

Edge gate on KFB-1 (every edge tile vs the source white-canvas):
`min PSNR margin = +0.5 dB` (byte-equality ⇒ PSNR_mine = PSNR_oracle),
`max|diff| mine 42 = oracle 42`. The gate inequality `psnr_mine ≥
psnr_oracle − 0.5` holds for all 627 tiles; global max equality holds.

### 3.4 Readers

- tifffile 2024.5.22: opens all outputs; BF page/level structure identical
  to oracle; FL `axes=CYX`, `shape=(6,H,W)`, level counts equal.
- openslide 1.4.6 (the platform's first reader for `.tif` in
  `slide_io.py`): opens KFB-1 output with **9 levels, correct dimensions,
  MPP 0.48410487 — identical to the oracle output**.
- Bio-Formats (showinf, JDK 21): KFB-1 output opens (Series count 1,
  34013×46152, RGB); KFBF outputs open (17 series, OME pyramid resolved).
  Logs in `.gate-tmp/slide-tools-c1/evidence/showinf-*.txt`.

### 3.5 >4 GiB proof (both writers, 64-bit offsets)

`C1_BIG=1 pytest tests/test_slide_transform_core.py::test_over_4gib_both_writers`
(109 s): synthetic BF 100 000×100 000 and FL 74 000×74 000×2ch inputs,
converted and reopened with tifffile; both outputs contain `TileOffset`s
beyond 2³²−1. Peak RSS: BF 17 468 KiB, FL 46 268 KiB; wall 69 s / 39 s.

### 3.6 wasm

Node 22 smoke test (evidence `wasm-smoke.mjs`, `wasm-parity.mjs`): probe +
convert work for both modalities through the JS host callbacks; **wasm
output is byte-identical to the native CLI** for the same fixture
(sha256-equal). The output byte stream therefore inherits all the parity
results above.

## 4. The JPEG codec (why hand-written)

C0's acceptance required fixing the edge-tile quality gap. Measured reality:
*any* re-encoder that is not bit-identical to Pillow risks violating
"global max diff ≤ oracle's" on adversarial tiles, because the oracle itself
re-encodes with Pillow. The robust fix is byte equality, so the codec
mirrors libjpeg-turbo's default paths exactly:

- decode: baseline SOF0/SOF1, islow IDCT (verbatim port of `jidctint.c`,
  including the 1024-entry post-IDCT range-limit table), h2v1/h2v2 *fancy*
  upsampling with the box fallback at downsampled width ≤ 2 (verbatim
  `jdsample.c` semantics), fixed-point YCbCr→RGB with jdcolor's table
  constants, FF00 unstuffing, restart intervals, zero-fill past markers.
- encode: jccolor RGB→YCbCr tables (incl. the ONE_HALF−1 chroma rounding
  quirk), jcsample downsampling with alternating bias (0,1 / 1,2 per
  column) and `expand_bottom_edge` semantics (the last *downsampled* row is
  replicated), islow FDCT (verbatim `jfdctint.c`), jcdctmgr quantization
  (divisors = quantval≪3), standard Annex-K Huffman tables, jccoefct
  dummy-block rule (blocks fully past the right/bottom edge encode as
  zero-AC + DC-of-left-neighbour), exact marker layout (SOI, JFIF 1.01
  APP0, one 8-bit DQT per table in zigzag order, SOF0, DHT in
  DC/AC-per-component order, SOS).
- quant-table convention: encoder inputs and `qtables_pillow_style()` are
  **natural (row-major) order**, exactly like Pillow's `im.quantization` /
  `save(qtables=…)`; DQT segments are zigzag-ordered on the wire.

Dependencies: **none** (the codec needs no crates). The workspace's only
runtime dependencies are `sha2` (RustCrypto, MIT OR Apache-2.0) in the CLI
and `wasm-bindgen =0.2.129` (MIT OR Apache-2.0) in the wasm crate — see the
license section.

## 5. Brightfield SubIFD evaluation — decision: keep classic

Evidence:

1. Platform reader path (`slide_io.py`): `.tif` opens via **openslide
   first**; openslide 1.4.6 resolves our classic 9-IFD chain as a full
   9-level pyramid with correct MPP (§3.4). The tifffile fallback
   (`TiffFileSlide`) also opens it. Nothing in the platform reads SubIFDs
   for brightfield.
2. Bio-Formats opens the classic output without errors (series resolved,
   dimensions correct). It does not group the classic chain into
   "resolutions", but the platform does not use Bio-Formats.
3. The hard byte-parity gate is defined against the oracle's classic
   writer; emitting SubIFDs for brightfield would *break whole-file sha256
   equality* by construction.

Decision: brightfield stays `ClassicJpegBigTiff`. If a future Bio-Formats
pipeline needs resolution grouping, a SubIFD profile can be added behind
`OutputProfile` without touching the classic path (open item, §9).

> **Superseded 2026-10-02** — see `bf-ome-acceptance-report.md`. Point 2
> turned out to matter to users: QuPath picks Bio-Formats by default and
> exposes the classic chain as a single resolution. A SubIFD profile
> `OmeBigTiffRgbSubifd` (`bf-ome`, format
> `ome-bigtiff-subifd-rgb-jpeg-pyramid`) was added exactly as anticipated
> above and is now the default for *new* browser brightfield jobs. The
> classic writer is untouched (classic sha256 unchanged on every fixture);
> the byte-parity gate for `bf-ome` is browser == native, and its tile
> payload stream is byte-identical to classic at the same offsets. The CLI
> `--profile auto` still produces classic for KFB.

## 6. License review (hard requirement)

**jpeg-encoder 0.6** (the C0 spike's encoder) is licensed
"(MIT OR Apache-2.0) AND IJG" because its huffman/JPEG implementation is
derived from the Independent JPEG Group's code. For a self-hosted WASM
binary served on a web page the obligations would be: keep the IJG
disclaimer in the distribution (the `.wasm` and its source repo), and
attribute the IJG derivation wherever the license text is shipped; the IJG
clause is a notice/attribution condition (not copyleft), so self-hosting is
permitted, but the AND-combination makes automated license scanning noisy
and the attribution is easy to lose in a minified artifact.

**Resolution:** the dependency was removed and the codec written inside
the core crate (§4). The real driver is byte parity with Pillow on
re-encoded edge tiles (jpeg-encoder is not bit-identical to libjpeg-turbo,
which is what left the C0 quality gap) — *not* license avoidance.

> **Correction (C1 review, 2026-09-29).** An earlier draft of this section
> claimed the libjpeg-turbo sources are under an "MIT-style IJG-free
> license" and that no code was copied. Both are wrong: the files the codec
> mirrors (`jidctint.c`, `jfdctint.c`, `jdsample.c`, `jdcolor.c`,
> `jccolor.c`, `jcsample.c`, `jcdctmgr.c`) are IJG-licensed, and §4
> describes a line-level port of their fixed-point arithmetic. The codec is
> therefore treated as IJG-derived: `slide-transform-core/NOTICE` carries
> the IJG acknowledgement and the build copies it next to the wasm
> (`static/tools/slide-transform/NOTICE`). The IJG License is permissive
> (attribution + disclaimer, no copyleft), so self-hosting remains fine; the
> obligation is simply the same one jpeg-encoder would have carried.

The standard quantization/Huffman tables are normative T.81 content,
verified byte-for-byte against Pillow's own output. Remaining
dependencies:

| crate | version | license |
|---|---|---|
| sha2 (CLI + synth-gen only) | 0.10 | MIT OR Apache-2.0 |
| wasm-bindgen (wasm only) | =0.2.129 | MIT OR Apache-2.0 |
| (transitive) cfg-if, once_cell, proc-macro2, quote, unicode-ident, bumpalo, … | — | MIT OR Apache-2.0 / (MIT OR Apache-2.0) AND Unicode-3.0 |

All permissive. The shipped wasm contains IJG-derived code (the codec) and
ships with its NOTICE.

## 7. API summary

```rust
// plan
TransformPlan::brightfield(identity) / ::fluorescence(identity)
    .with_policy(PixelPolicy::AllowEdgeReencode | StrictLossless)
    .with_limits(ResourceLimits { timeout_seconds, max_output_bytes, min_free_bytes });

// convert (brightfield)
convert_bf::convert_kfb_to_bigtiff(&src /*ByteSource*/, &mut sink /*RandomAccessSink*/,
                                   &mut scratch /*ScratchFactory*/, &plan, &job /*JobControl*/)
    -> TransformResult
// convert (fluorescence; companion optional)
convert_fl::convert_kfbf_to_ome(..., companion: Option<&Companion>) -> TransformResult

// job
JobControl::new(&progress).with_timeout(secs);  // progress: per tile-row/level,
                                                // committed bytes; cancel flag; timeout
```

`TransformResult`: format, output_bytes/sha256, width/height, per-level
`LevelStats` (raw_copied / reencoded / filled_black / channel), the
`edge_regions` list (level, channel, source vs canvas size, qtable reuse),
warnings (`edge_reencode_fallback_q95`, `sparse_fill_black`,
`exposure_unit_assumed_ms`), channel summaries incl. absorbed display
window, `ValidationReport`, IFD chain, associated images, elapsed seconds.

Strict-lossless: `StrictLossless` rejects any input requiring an edge
re-encode **before the first output byte** (`pixel_policy_violation`).
Checkpoint-friendliness: every progress event carries committed output
bytes and the writers' offset/count streams live in scratch, so a C2
checkpoint can resume from `(level, channel, cell)` + committed length.

## 8. Malformed-input corpus (52 Rust tests)

15 lib tests + 37 malformed tests, all green (`cargo test -p
slide-transform-core --features fixtures`). Corpus: KFB — truncation, bad
magic/version/flags, huge counts, header_bytes range, index beyond EOF,
u64::MAX payload offsets, misalignment, reserved fields, duplicate cells,
corrupt edge JPEG (decode failure), missing level, vendor-layout index
truncation/bad record magic; KFBF — bad magic/version/format-version,
huge tile count, index/pointer-block/side-record OOB, record magic,
reserved/sentinel fields, scale not objective/2^L, misalignment, jpeg dims,
len_ch0/side mismatch, duplicate cell, extent mismatch, plus a well-formed
round-trip smoke and the strict-policy rejection. Debug builds turn
unchecked arithmetic into panics, so passing proves checked overflow on
hostile paths.

## 9. Open items / deviations

1. **ExposureTime location (intended)**: FL whole-file sha256 equality with
   the oracle is impossible by requirement; parity is at full-tile payload +
   structure + OME-semantics level.
2. **OME-XML float formatting**: exposure/objective use `.17g`, mpp uses
   `repr()`-style shortest round-trip (unit-tested for the common shapes);
   exotic exponents are formatted Python-style but were not exhaustively
   diffed against CPython (not on any gate).
3. **wasm timeout guard**: inert on wasm32 (no clock); the host enforces
   wall time via cancel. Native keeps the timeout.
4. **SubIFD brightfield profile**: not implemented (decision §5); the
   `OutputProfile` enum leaves room.
5. **Full checkpoint/resume**: C2 scope; the C1 API already emits committed
   byte counts and keeps resumable writer state in scratch.
6. **KFBF OME channel exposure comparison**: the oracle puts exposures on
   `<Channel>`; we put them on `<Plane>`; the harness compares values, not
   location (location is the C1 requirement itself).
