# U3 — `compact-jpeg-v1` 「更小文件（有损）」: feasibility, parameter comparison and acceptance

Date: 2026-10-03 · Branch `core-codec` (uncommitted working tree; **not** committed, not deployed).
Spec: `tool-ux-and-format-expansion-implementation-plan-20261003.md` §4 (U3) / §6; review F4.
Background: `c1-core-report.md` §4 (the JPEG codec), `bf-ome-acceptance-report.md` §2 (the
persistence + resume-refusal pattern mirrored here for the encoding field).

Privacy: real samples appear only by alias and sha256 — **KFB-1** = first `*.kfb` of the
private sample folder (C1/C2 alias, 221 189 354 B, brightfield). No sample bytes, names,
scanner identifiers or pixels are committed; raw evidence stays in the untracked
`.gate-tmp/` working area.

## 0. Verdict

| Gate | Result |
|---|---|
| Feasibility: in-core JPEG codec as a general encoder | **YES** — quality-scaled IJG tables, 4:2:0/4:2:2/4:4:4, YCbCr consistent with the decoder, standard Annex-K Huffman; native == WASM **byte-identical** (§1) |
| Parameter comparison Q80/Q85/Q90 (× 4:2:0/4:2:2/4:4:4) on KFB-1 + synthetic + textures | **DONE** (§2) |
| Locked parameters | **q80 · 4:2:0 · standard Annex-K Huffman**, fingerprint `cj1:q80:420:hstd:v1` (§2.3) |
| `compact-jpeg-v1` implementation (brightfield only, both layouts, resume, refusals) | **PASS** (§3) |
| Preserve-mode outputs byte-identical to pre-U3 | **PASS** — KFB-1 bf-ome `374c70c8…`, bf-classic `385a59c6…`, C2 smoke `6e8744f9…` (§4) |
| Browser compact == native compact (byte parity) | **PASS** — smoke `7e2f4f82…`, KFB-1 `86131cab…`, 1 GiB fixture `4f536879…` (§4) |
| Bounded memory (native + browser cgroup) | **PASS** — native 11.8 MB RSS on KFB-1; browser increment ≈ 85 MB, oom 0 (§5) |
| Regression suites (Rust / vitest / pytest / C2) | **PASS** — counts in §6 |
| Human blind quality review of compact crops | **PENDING (owner task)** — §8 |
| Tool-page quality radio wiring (UI files) | **PENDING (another agent)** — engine/runner/worker API is ready; UI files untouched by this phase |

Unit tests alone were not used as acceptance; §2–§5 numbers come from real and full-size runs.

## 1. Feasibility — the in-core codec as a general encoder

The C1 codec (§4 of `c1-core-report.md`) already contains a full baseline JPEG **encoder**
(`jpeg/encoder.rs`: jccolor RGB→YCbCr, jcsample h2v1/h2v2 downsampling, islow FDCT,
jcdctmgr quantization, Annex-K Huffman, exact Pillow/libjpeg marker layout) that was
proven byte-identical to libjpeg-turbo on the C1 parity matrix (960/960 encodes, qualities
50–100, 444/422/420, sizes 1×1…256×256). U3 needs it as a *general* encoder:

1. **Quality-scaled tables** — `EncoderCfg::with_quality(quality, sampling)` applies the IJG
   quality scaling (`jpeg/tables.rs::quality_scaling`, same formula as libjpeg) to the
   standard Annex-K luma/chroma tables. Any q in 1..=100, any sampling.
2. **Subsampling** — `Sampling::S444 | S422 | S420` (h1v1 / h2v1 / h2v2) with jcsample
   alternating-bias downsampling and `expand_bottom_edge` semantics; the decode side
   already fancy-upsamples h2v1/h2v2 (jdsample port), so decode(YCbCr) and
   decode(encode(RGB)) use one consistent colour pipeline (fixed-point jdcolor/jccolor
   constants).
3. **Huffman** — standard Annex-K tables only (the mode does not do optimized Huffman;
   this is exactly what libjpeg/Pillow emit with default tables and keeps the encoder
   bit-comparable with the reference implementation).
4. **Determinism native vs WASM** — no floating-point in the sample path beyond the fixed
   libjpeg integer constants; proven empirically byte-identical three times (§4).
5. **Speed / scratch (native, release, KFB-1 level-0, 400 full tiles,
   `examples/compact_bench.rs` with a counting global allocator):**

   | cfg | decode ms/tile | encode ms/tile | out B/tile |
   |---|---:|---:|---:|
   | q80 4:2:0 | 0.628 | 0.671 | 4 853 |
   | q85 4:2:0 | 0.611 | 0.692 | 5 242 |
   | q90 4:2:0 | 0.612 | 0.758 | 6 912 |
   | q85 4:2:2 | 0.611 | 0.814 | 5 652 |
   | q85 4:4:4 | 0.608 | 1.039 | 6 604 |

   Per-tile scratch peak of one decode→canvas→encode cycle: **769 KiB** (77 allocations;
   decoder planes + 192 KiB RGB canvas + encoder state). Bounded per tile by construction —
   the converter holds exactly one tile at a time.

WASM speed is measured end-to-end instead of per-tile (§5): 1 GiB fixture, native compact
52.6 s vs browser convert 59.5 s ⇒ **WASM ≈ 1.13× slower**, with byte-identical output.

## 2. Parameter comparison and the lock

Method (`examples/u3_compare.rs`, not shipped): every candidate re-encodes each sampled
tile; metrics compare the re-encoded decode against the **source decode** — i.e. against
exactly what `preserve` hands to a viewer. Luma = BT.601 integer weights. Tissue =
non-background pixels (max channel < 235 or chroma spread > 24). All runs bounded:
≤ 600 level-0 tiles of KFB-1, ≤ 128 tiles per synthetic fixture; never a full level-0
array in any tool.

### 2.1 KFB-1 (real clinical sample, 600 level-0 tiles; source ≈ q90-class 4:2:2 scanner JPEG)

| cfg | B/tile | size vs source | PSNR luma | PSNR r/g/b | MAD luma | max rgb | tissue % | PSNR luma (tissue) | MAD luma (tissue) |
|---|---:|---:|---:|---|---:|---:|---:|---:|---:|
| q80 4:2:0 | 5 108 | **90.2 %** | 69.5 dB | 45.6 / 49.9 / 43.0 | 0.004 | 61 | 73 | 72.1 dB | 0.003 |
| q80 4:2:2 | 5 665 | 100.1 % | 71.3 | 68.2 / 69.9 / 68.5 | 0.003 | 16 | 73 | 72.8 | 0.002 |
| q85 4:2:0 | 5 519 | 97.5 % | 52.4 | 45.7 / 48.5 / 43.4 | 0.342 | 48 | 73 | 52.5 | 0.341 |
| q85 4:2:2 | 5 965 | 105.4 % | 52.5 | 49.9 / 51.0 / 48.7 | 0.342 | 19 | 73 | 52.5 | 0.340 |
| q90 4:2:0 | 7 310 | 129.1 % | 57.9 | 47.5 / 50.9 / 45.0 | 0.105 | 43 | 73 | 58.0 | 0.103 |
| q90 4:2:2 | 7 801 | 137.8 % | 57.9 | 54.9 / 56.1 / 53.5 | 0.104 | 22 | 73 | 58.0 | 0.103 |
| q85 4:4:4 | 6 959 | 122.9 % | 52.5 | 49.5 / 50.8 / 48.0 | 0.342 | 25 | 73 | 52.5 | 0.340 |

### 2.2 Synthetic textures and fixtures (adversarial coverage)

* Low-contrast texture (Δ≈6 blobs on flat ground; source q90 4:2:2): q80 4:2:0 → 68.0 %
  size, PSNR 48.1 dB, MAD 0.72, max 6.
* High-saturation texture (full-chroma H&E-like hues + grain): q80 4:2:0 → 73.1 % size,
  PSNR 30.9 dB, MAD 5.8, max 58 (the worst measured case; q85 → 89.2 % / 36.3 dB;
  q90 4:2:0 → 97.8 % / 71.5 dB).
* `gen-kfb` noise fixtures (uniform noise = the adversarial floor, no spatial redundancy),
  sizes 613×355 (odd) and 1024×777, source samplings 4:2:2 / 4:2:0 / 4:4:4, q90 source:
  q80 4:2:0 luma PSNR 30.2–31.7 dB on every variant; output 54.5–69.5 % of a 4:2:2 source,
  69.1–69.5 % of a 4:2:0 source, 36.7–37.2 % of a 4:4:4 source. For a 4:4:4 source the
  newly introduced chroma decimation is large on noise (per-channel PSNR 12–15 dB,
  MAD ≤ 29) — inherent to 4:2:0, and on real/smooth content it is two orders smaller
  (KFB-1 tissue best-channel MAD ≤ 0.5).
* Full table (all seven candidates × six fixtures): untracked evidence
  `.gate-tmp/u3/compare-kfb1.txt`, `compare-synth.txt`.

Note: luma PSNR is **non-monotonic** in quality on real tiles (69.5 dB at q80 vs 52.4 dB
at q85) because re-quantization error depends on the grid interaction between the
scanner's own quantization tables and the candidate table — not a measurement error. The
lock decision therefore rests on the measured size/fidelity pairs, not on monotonicity
assumptions.

### 2.2a Reviewer spot check — colour on dense tissue

The tissue columns in §2.1 are **luma only**; luma is nearly untouched because the change is
in chroma (4:2:0). Independent reviewer check on one dense H&E region of KFB-1 (512×512 at
level 0, picked as the most saturated block of a level-4 overview; 75 % tissue pixels),
OpenSlide reading the bf-classic preserve vs compact outputs: per-channel mean abs diff
**R 3.07 / G 2.20 / B 6.21**, p99 23, max 68. Side by side at 1:1 the two crops were not
distinguishable by the reviewer; this is one region and does not replace the owner's blind
review (§8). Chroma error on stained tissue is therefore materially larger than the luma
figures suggest, and the size saving on this real sample is modest (≈ 10 %): the mode is
offered as an explicit lossy choice, not as a recommendation for colour-quantitative use.

### 2.3 Locked parameters and justification

```text
compact-jpeg-v1 := quality 80 · subsampling 4:2:0 · standard Annex-K Huffman
fingerprint     := cj1:q80:420:hstd:v1
```

1. **It is the only candidate that does the mode's job on the real sample**: 90.2 % of the
   source payload per tile and 90.1 % whole-file (§4); q85 is ≥ 97.5 % (no meaningful
   gain), q90 makes the file *larger* than the source.
2. **Structure is preserved**: luma is essentially untouched (69.5 dB PSNR, MAD 0.004;
   tissue 72.1 dB / MAD 0.003). What changes is chroma resolution (4:2:0), which is where
   the size reduction comes from.
3. **The worst adversarial case is bounded and accepted** as the explicit price of a mode
   the UI labels 「更小文件（有损）」: saturated-noise texture at 30.9 dB / MAD 5.8. Smooth
   real content measures far higher; the human blind review remains a pending owner task
   before any default recommendation beyond "available choice" (spec §4 gate).
4. Same generation semantics for every tile and level: reduced levels are re-encoded from
   **their own source tiles** at the same parameters (no cascaded re-encode of level 0).

Changing any parameter changes the fingerprint and must bump the version suffix; resumed
checkpoints of the old fingerprint are refused (§3).

## 3. Implementation contract

### 3.1 Core (`slide-transform-core`)

* `plan.rs`: `EncodingProfile { PreserveSource, CompactJpegV1 }` (wire ids
  `preserve-source-v1` / `compact-jpeg-v1`), **independent of `OutputProfile`** — the four
  brightfield combinations bf-ome/bf-classic × preserve/compact all exist. Default
  (and every pre-U3 constructed plan) = `PreserveSource`. Locked constants
  `COMPACT_JPEG_V1_{QUALITY,SAMPLING,HUFFMAN,FINGERPRINT}` + `compact_jpeg_v1_encoder_cfg()`.
* `convert_bf.rs`: compact decodes **every** tile of every level, pastes it on the white
  256×256 canvas and re-encodes with the locked config at **full source resolution and
  coordinates** (no crop, no downscale); reduced levels are re-encoded from their own
  source tiles; edge/padded tiles are handled exactly as today's white-canvas path, with
  the compact parameters. Level sampling of the source is not consulted (the output
  sampling is the locked one). Tags match the encoded data: 259 = 7 (JPEG), 262 = 6
  (YCbCr), 530 = [2,2] — asserted per level in `tests/compact_encoding.rs`.
* `convert_fl.rs`: fluorescence **refuses** compact before any output byte
  (`UnsupportedKfbVariant`, zero-byte output, tested).
* `PixelPolicy::StrictLossless` + compact is a contradiction → typed `pixel_policy_violation`
  refusal before any output byte (core + CLI + wasm, tested).
* `report.rs`: `TransformResult.lossy_reencode: Option<LossyReencode>` — present only for
  compact runs, carries profile id, fingerprint, quality, sampling, huffman,
  `tiles_reencoded` (= tiles_total) and `tiles_padded` (= the white-padding subset, listed
  as edge regions). `None` ⇒ preserve semantics; a preserve output never claims or gains a
  lossy summary. OME provenance gains `encoding_profile` + `encoding_params_fingerprint`
  keys **only** for compact runs and describes the payload as `lossy` — preserve OME-XML
  keeps its exact pre-U3 bytes; "lossless" is never claimed.
* `estimate.rs`: probe/estimate gains `compact_upper_bound_bytes` (payload × 1.5 slack —
  re-encoded size is pixel-bound, not byte-bound; the runtime output cap and the
  recoverable quota error remain the hard guards for pathological sources).

### 3.2 CLI

`--encoding preserve|compact` (default `preserve`), independent of `--profile`. Output JSON
gains `encoding`, `lossy_reencode`, `lossy_reencode_params`; probe JSON gains
`compact_upper_bound_bytes`. Fluorescence/strict refusals are typed before any write.

### 3.3 Browser (engine/runner/worker/wasm)

* `engine.js`: `ENCODING_PROFILES`, `COMPACT_JPEG_V1_FINGERPRINT` (mirror of the Rust
  constant, vitest-checked), `defaultEncodingProfile()` = preserve for every modality,
  `recordEncodingProfile()` (missing field = preserve — pre-U3 records never become
  compact), `encodingFitsModality()` (compact = brightfield-only),
  `diskNeedBytes(..., { encoding })` picks the compact bound.
* `runner.js`: `encodingProfile` persisted on the job record at prepare (disk gate uses the
  chosen encoding's bound), `setPreparedEncodingProfile()` mirrors the output-profile rules
  (prepared-only → later `resume_refused`, kind `encoding-profile`; modality-checked →
  `unsupported_input`, kind `encoding-profile`), start/resume ladder identical to the
  output profile's (resumes use the record), resume refusals when the record, the journal
  generation, or the last committed checkpoint state disagree — always `resume_refused`,
  kind `encoding-profile`, never mixing two quality modes in one half-written output.
* `worker.js`: journals `encodingProfile` in every generation and passes it to the wasm;
  calls the new `convertProfileEncoded` / `convertResumeProfileEncoded` exports.
* `wasm/lib.rs`: checkpoint states carry `"encoding"` next to `"profile"`; legacy states
  without the field mean preserve; `resolve_encoding` rejects unknown ids and
  fluorescence-compact before conversion; the resume path refuses any combination mismatch
  (including compact-request-vs-legacy-state) independently of the runner.

## 4. Byte-parity proofs (pinned values)

| Artifact | sha256 (first 8) | bytes | Check |
|---|---|---:|---|
| KFB-1 bf-ome **preserve** (native CLI) | `374c70c8` | 220 055 361 | **= pre-U3 value** |
| KFB-1 bf-classic **preserve** (native CLI) | `385a59c6` | 220 055 311 | **= pre-U3 value** |
| C2 smoke synthetic bf-ome preserve (browser) | `6e8744f9` | 288 881 | **= pre-U3 value**, browser == native |
| C2 smoke synthetic bf-ome **compact** (browser) | `7e2f4f82` | 163 058 | browser == native compact |
| KFB-1 bf-ome **compact** (browser, `run_parity.js --compact`) | `86131cab` | 198 374 506 | browser == native compact |
| KFB-1 bf-ome compact (native CLI) | `86131cab` | 198 374 506 | 90.1 % of preserve |
| KFB-1 bf-classic compact (native CLI) | `037c552d` | 198 374 288 | payload stream identical to the bf-ome compact run (classic == ome layout invariance, as in C1) |
| 1 GiB fixture bf-ome compact (browser, cgroup run) | `4f536879` | 564 661 196 | == native compact (52.6 s, 4.4 MB RSS) |

Preserve-mode outputs are bit-for-bit what the pre-U3 code produced — the encoding field is
strictly additive (OMP provenance keys appear only for compact; the preserve OME-XML and
both writers' byte streams are untouched).

## 5. Memory and time

Whole-slide KFB-1 (32 277 tiles incl. 627 padded), native CLI, `systemd-run MemoryMax=4G`:

| run | wall | max RSS | output |
|---|---:|---:|---|
| bf-ome preserve | 1.7 s | 11.4 MB | 220 055 361 B |
| bf-ome compact | 42.7 s | 11.8 MB | 198 374 506 B |
| bf-classic preserve | 1.7 s | 11.6 MB | 220 055 311 B |
| bf-classic compact | 42.8 s | 11.6 MB | 198 374 288 B |

Compact costs ≈ 25× the wall time of preserve (decode+encode ≈ 1.3 ms/tile incl. I/O) with
**no meaningful memory increase** — the per-tile scratch (§1) keeps RSS flat.

Browser low-memory measurement (`tests/browser/slide_tools_c2/run_mem_cgroup.sh
cg4g-1g-u3compact 4G 1g saver compact`, cgroup simulation, MemoryMax 4G, SwapMax 0;
evidence `.gate-tmp/slide-tools-c2/browser/mem/cg4g-1g-u3compact.json`):

| value | measured |
|---|---|
| input / output | 0.98 GiB / 564 661 196 B (53.8 %) |
| sha == native compact | yes (`4f536879`) |
| browser convert | 59.5 s (native 52.6 s ⇒ WASM ≈ 1.13×) |
| job RSS increment over blank-page baseline | ≈ 85 MB (548.9 − 459.8 MB peak/mean) |
| wasm heap peak | 2.2 MiB |
| temp disk peak | ≈ 1.5 GiB (≤ the compact estimate's bound) |
| cgroup events | oom 0 / oom_kill 0 / max 0 |

Same bounded-memory class as the preserve runs in `bf-ome-acceptance-report.md` §6.1.
Real 4 GB / 8 GB devices remain an external check (unchanged from C2).

## 6. Test results

| Suite | Command | Result |
|---|---|---|
| Rust workspace | `cargo test --release --workspace --features slide-transform-core/fixtures` | **89 passed, 0 failed** — core lib 17, bf_ome 8, **compact_encoding 9 (new)**, jpeg_mutation 2, malformed 37, **resume 12 (10 + 2 new)**, wasm lib 4 (2 + 2 new) |
| New Rust coverage | `tests/compact_encoding.rs` | fingerprint==locked params; contract over 3 source samplings × 4 geometries (incl. odd 613×355) × both layouts (tags 259/262/530, counts, geometry == preserve, validator accepts, both layouts share the payload byte stream, compact ≠ preserve); white padding floor; OME provenance (compact-only keys, `lossy`, never "lossless"); fluorescence refusal (0 bytes); strict-lossless refusal (0 bytes); default = preserve; smaller on default fixture; smooth-content fidelity ≥ 38 dB / MAD ≤ 2 |
| Python oracle harness | `.venv/bin/python -m pytest tests/test_slide_transform_core.py -k compact -q` | **1 passed** (17 deselected; whole-file oracle NOT run — reviewer's job) — report contract + `validate` + `slide_io.open_slide` opens compact bf-ome as native RGB with correct colours/MPP/geometry and compact < preserve |
| Platform capability | `.venv/bin/python -m pytest tests/test_slide_tools_upload_capability.py -q` | **13 passed** |
| vitest | `npx vitest run --dir tests/js` | **42 files, 649 passed** (was 637 + 12 new in `tests/js/tools-encoding-profile.test.ts`) |
| C2 smoke | `run_smoke.js` then `run_smoke.js --encoding compact` (ports 8981/8982) | **PASS / PASS** (compact asserts `result.encoding`, `lossy_reencode`, params) |
| C2 faults | `run_faults.js --port 8983` | **34/34** (30 existing + `compact-encoding-resume-matches`, `encoding-change-refused`, `legacy-record-resumes-preserve`, `prepared-encoding-set-rules`) |
| C2 real-sample parity | `run_parity.js --samples <dir> --compact --port 8984` | **PASS** — KFB-1 browser `86131cab…` == native |
| WASM rebuild | `bash scripts/build_slide_transform.sh` | artifacts byte-identical to the previous build (wasm `c0920da4…`, manifest refreshed) |

Regressions shown to fail on old behaviour: the Rust U3 tests do not compile against the
pre-U3 core (no `EncodingProfile`); the pytest compact case fails on the pre-U3 CLI
(`--encoding` rejected); the C2 `encoding-change-refused` / `legacy-record-resumes-preserve`
scenarios fail on the pre-U3 runner (no encoding refusal — the resume would proceed and
mix modes); the vitest fingerprint/disk-precheck cases fail without the new engine fields.

## 7. Files changed (uncommitted, branch `core-codec`)

Core: `slide-transform-core/crates/core/src/{plan,convert_bf,convert_fl,estimate,report,lib}.rs`,
`crates/core/tests/{compact_encoding,resume}.rs` (new compact tests),
`crates/core/examples/{compact_bench,u3_compare}.rs` (new, not shipped),
`crates/cli/src/main.rs`, `crates/wasm/src/lib.rs`.
Browser: `static/tools/slide-transform/{engine,runner,worker}.js` + rebuilt
`slide_transform*.js/.wasm/.d.ts` + `build-manifest.json`.
Tests/harness: `tests/browser/slide_tools_c2/{harness,run_faults,run_memory,run_parity,
run_smoke}.js`, `run_mem_cgroup.sh`, `tests/test_slide_transform_core.py` (compact case
only), `tests/js/tools-encoding-profile.test.ts` (new).
This report. Tool-page UI files (`templates/tools_slides.html`, `static/tools/tools-slides*`,
`static/upload/*`, `static/app.js`) deliberately untouched (other agent).

## 8. Pending items

1. **Human blind review** of compact-vs-preserve crops on real tissue (spec §4 acceptance
   line) — owner task; until then compact is an explicit choice, not a recommendation, and
   quantitative-use suitability is undecided (no threshold review performed).
2. **Tool-page quality radio** — the reviewer wires `templates/tools_slides.html` /
   `tools-slides*.js` to `runner.setPreparedEncodingProfile` + the `encodingProfile` start
   option; the runner/engine/worker contract is complete and vitest/C2-covered.
3. QuPath/OpenSlide/platform-viewer interop runs used **preserve** outputs (unchanged);
   compact interop through the real ingestion→viewer chain is covered only at the reader
   level (`slide_io.open_slide`, pytest) — a full platform-chain run on a compact artifact
   is available as follow-up if desired.
4. Real 4 GB / 8 GB devices, real OS save dialog, production deployment — unchanged
   external checks from the bf-ome/C2 reports.
5. A pathological low-quality source can exceed the compact estimate's 1.5× slack; the
   runtime output cap and the recoverable quota error remain the hard guards (documented
   in `estimate.rs`).

## 9. Commands (worktree root; `TMPDIR=$PWD/.gate-tmp COLUMNS=200`)

```
cargo test --release --workspace --features slide-transform-core/fixtures   # manifest slide-transform-core/
bash scripts/build_slide_transform.sh
slide-transform convert <in.kfb> <out.ome.tif> --profile bf-ome --encoding compact
slide-transform probe <in.kfb>                       # compact_upper_bound_bytes
npx vitest run --dir tests/js
.venv/bin/python -m pytest tests/test_slide_transform_core.py -k compact -q
.venv/bin/python -m pytest tests/test_slide_tools_upload_capability.py -q
node tests/browser/slide_tools_c2/run_smoke.js --port 8982 --encoding compact
node tests/browser/slide_tools_c2/run_faults.js --port 8983
node tests/browser/slide_tools_c2/run_parity.js --samples <切片文件夹> --compact --port 8984
bash tests/browser/slide_tools_c2/run_mem_cgroup.sh <label> 4G 1g saver compact
```

Versions: rustc/cargo 1.98.1, wasm-bindgen 0.2.129, Node 22, Playwright Chromium 151,
Python 3.14 (.venv), vitest 3.2.7. Evidence: `.gate-tmp/u3/` (comparison tables,
conversion JSON/time, fixture conversions), `.gate-tmp/slide-tools-c2/browser/mem/`
(cgroup run), `smoke/`, `faults/`, `parity-compact/` logs.
