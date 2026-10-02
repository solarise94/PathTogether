# Brightfield pyramidal OME-BigTIFF (`bf-ome`) — implementation & acceptance report

Date: 2026-10-02 · Branch `bf-ome-tiff` (local commits, not pushed, not deployed)
Host: Linux x86_64 (Ubuntu 26.04.1), 18 GiB RAM.

Privacy: real samples are referred to only by alias and sha256. **KFB-1** = first
`*.kfb` of the private sample folder (C1/C2 alias); **KFB-Q** = the brightfield
sample of the 2026-10-02 QuPath investigation; **KFBF-A..D** = fluorescence
samples (C1 aliases). No sample bytes, names, scanner identifiers or screenshots
are committed; raw evidence stays under the git-ignored-by-convention `.gate-tmp/`.

## 0. Verdict

| Gate | Result | Evidence (§) |
|---|---|---|
| A. QuPath **default** opening, Linux x86_64, 0.6.0-rc5 / 0.6.0 / 0.7.0 | **PASS** | §4.1 — Bio-Formats chosen by default, all resolutions, regions at every level pixel-identical to classic, project save/reopen |
| A. Bio-Formats grouping checked explicitly | **PASS** | §4.1 — `OMETiffReader`, 1 series, N resolutions, 1 channel × 3 samples |
| A. QuPath on macOS ARM64 | **EXTERNAL — not run** | §9 |
| B. Payload / pixel / metadata preservation | **PASS** | §5 |
| B. Browser output byte-equal to native | **PASS** | §6 (synthetic, KFB-1, 4.4 GiB, 9.8 GiB) |
| C. Browser flows (local convert/export, refresh/resume, cancel, interrupted finalize, one-click convert-and-upload, >4 GiB, low memory, oversize save) | **PASS** (cgroup-simulated memory, see caveats) | §6 |
| Platform reader + real ingestion + viewer + project reopen | **PASS** | §7 |
| D. Rust / Python / JS / browser regression | **PASS** except 3 pre-existing, unrelated pytest failures | §8 |
| OpenSlide-based tools on `bf-ome` | **Compatibility change** — 1 level only; classic output selectable on the tool page | §2, §3 |
| QuPath acceptance helpers fail on failed checks (round 2) | **PASS** | §4.1 strict gate |

Unit tests alone were not used as acceptance; every A/B/C item above was
exercised on real or full-size outputs.

## 1. Output layout (`bf-ome`, format id `ome-bigtiff-subifd-rgb-jpeg-pyramid`)

```
0      BigTIFF header (16 B)
16     tile payload stream — byte-identical to the classic profile, same offsets
       (level 0 tiles, then level 1, …; JPEG payloads copied from the KFB,
        only edge tiles re-encoded exactly as classic does)
...    IFDs, written at finish():
       main chain: exactly ONE IFD  (level 0, full resolution)
         NewSubfileType 0 · ImageDescription = OME-XML 2016-06
         SubIFDs (330, LONG8) → level 1..N
       SubIFDs: NewSubfileType 1 (reduced), no 270/330, next = 0
```

Tags on every level: 256/257 size, 258 = [8,8,8], 259 = 7 (JPEG), 262 = 6
(YCbCr — the payloads are YCbCr JPEG and are **not** relabelled as RGB),
277 = 3, 284 = 1 (chunky/interleaved), 296 = 3 + 282/283 (px/cm, same rounding
as classic → byte-equal), 322/323 = 256, 324/325 LONG8, 530 YCbCrSubSampling
taken from the source JPEG (e.g. [2,1] for 4:2:2).

OME-XML: one `Image` with `Pixels DimensionOrder="XYCZT" Type="uint8"
SizeC="3" Interleaved="true"` and **one** `Channel SamplesPerPixel="3"`
(one RGB image, not three fluorescence channels), `PhysicalSizeX/Y` in µm,
`TiffData IFD="0" PlaneCount="1"`; `Instrument/Objective NominalMagnification`
only when the source reports one; a `MapAnnotation` (namespace
`pathtogether.slide-transform/provenance`) with converter, converter version,
output profile, source format, pyramid levels, tile payload policy, pixel
policy, and the scanner id **only if the source contains one**. Nothing is
invented; no source file name is written.

## 2. Profiles and migration

| Profile id | Format id | File name | Where it is the default |
|---|---|---|---|
| `bf-ome` | `ome-bigtiff-subifd-rgb-jpeg-pyramid` | `<base>.ome.tif` | **new** brightfield jobs in the browser tool (page choice 「OME-TIFF（推荐，QuPath / Bio-Formats）」) |
| `bf-classic` | `classic-bigtiff-jpeg-pyramid` | `<base>.tif` | page choice 「经典金字塔 TIFF（OpenSlide 工具）」; CLI `--profile auto` for KFB; every legacy brightfield job |
| `fl-ome` | `ome-bigtiff-subifd-multichannel-jpeg-passthrough` | `<base>.ome.tif` | fluorescence (unchanged) |

Page choice (section 「7 · 输出格式」, brightfield only; fluorescence has no
choice and stays `fl-ome`):

* The selection is passed when the job is prepared; changing it afterwards
  saves it to the prepared record (`runner.setPreparedOutputProfile`, accepted
  only while the job is `prepared`, and only for a profile that fits the
  modality).
* Starting a job passes the on-screen selection only when the visible section
  belongs to that job; after a reload, or for another job in the list, the
  record's profile is used, so a saved classic choice is never replaced by the
  default radio.
* Once a job starts, the section is locked and shows the job's format; resume
  always uses the record. Job-list rows show the format.

Persistence and resume rules (runner + worker + wasm):

* The profile is fixed at **prepare** (`record.outputProfile`) and written into
  every journal generation (`gen.outputProfile`) and every wasm checkpoint
  (`{"profile": …}`).
* A record **without** `outputProfile` (created before this change) is a
  legacy job: brightfield → `bf-classic`, fluorescence → `fl-ome`. It resumes
  and exports exactly as before (`.tif`, classic bytes).
* Resume is refused (`resume_refused`, kind `output-profile`) when an explicit
  profile differs from the record, when the journal generation disagrees with
  the record, or when the last checkpoint's profile disagrees. A partially
  written classic output is therefore never continued as OME (and vice versa);
  the wasm resume path performs the same check independently.
* `CORE_VERSION` is unchanged (0.1.0), so existing jobs keep passing the core
  version gate and stay resumable.
* The page shows the format of the result (`#result-format`), and the
  suggested name and save-dialog filter follow the job's profile
  (`OME-TIFF` / `.ome.tif` vs `TIFF` / `.tif`); uploads use the same name.
* CLI: `--profile auto|bf-classic|bf-ome|fl-ome`; `auto` keeps classic for KFB
  because the Baidu import plugin worker consumes classic output. Output JSON
  gains `output_profile`; `validate` gains `main_ifds`/`sub_ifds`.

## 3. Compatibility changes

1. New default for new browser brightfield jobs: `.ome.tif`, OME-TIFF.
2. **OpenSlide 4.0.1 opens `bf-ome` as `generic-tiff` with 1 level** (it does
   not read SubIFD pyramids): KFB-Q 1 level vs 9 for classic; 9.8 GiB synthetic
   1 level. Pixels are correct but there is no pyramid for OpenSlide-based tools
   (and for QuPath only if a user forces the OpenSlide reader). Users of such
   tools should choose 「经典金字塔 TIFF（OpenSlide 工具）」 on the tool page (`bf-classic`).
   QuPath's default path is unaffected (§4).
3. The platform reader now treats YCbCr-JPEG OME tiles as native RGB
   (previously such a file would be classified as 3-channel "multichannel").
   The platform opens `bf-ome` with `TiffFileSlide` (OME sniff), not OpenSlide.
4. `viewable_formats` / `SLIDE_TOOLS_VIEWABLE_OUTPUT_FORMATS` include the new id.
5. Validator: `ifd_count` = main + SubIFDs for all profiles; the worker passes
   the converter's `ifd_count` for all modalities.
6. OME writer fix (`ext_bytes`): short descriptions/SubIFD arrays (≤ 8 B) were
   counted as external bytes, mis-laying IFDs on some small fluorescence sizes
   (e.g. 300×200, 400×300 — previously rejected by the validator). Those now
   convert; every size that converted before is byte-identical (real KFBF-A..D
   unchanged, §8).

## 4. A — Interoperability

### 4.1 QuPath default reader (Linux x86_64)

Method: QuPath CLI `script` with `scripts/qupath-probe/default-probe.groovy`, which asks the
`ImageServerProvider` for the **default** server (no reader forced, no
preference changed), lists builder support, reads regions (center, corner,
far edge, tissue) at **every** level, saves a project, reopens it, and
separately queries Bio-Formats (`ImageReader`) for grouping. Regions are
compared with a tifffile decode of the classic output.

| QuPath | Bio-Formats | Input | Default server (support) | Resolutions | RGB / type | mpp (µm) | Mag | Regions | Project reopen |
|---|---|---|---|---|---|---|---|---|---|
| 0.6.0-rc5 | 8.1.1 | KFB-Q bf-ome | BioFormats (5.0 vs OpenSlide 2.5) | 9 | yes / UINT8 | 0.484105 | 20 | 36, max diff 0 | BioFormats, 9 |
| 0.6.0 | 8.2.0 | KFB-Q bf-ome | BioFormats (5.0 vs 2.5) | 9 | yes / UINT8 | 0.484105 | 20 | 36, max diff 0 | BioFormats, 9 |
| 0.7.0 | 8.4.0 | KFB-Q bf-ome | BioFormats (5.0 vs 2.5) | 9 | yes / UINT8 | 0.484105 | 20 | 36, max diff 0 | BioFormats, 9 |
| 0.6.0-rc5 | 8.1.1 | 9.8 GiB synthetic | BioFormats (5.0 vs 2.5) | 10 | yes / UINT8 | 0.484105 | 20 | 40, max diff 0 | BioFormats, 10 |
| 0.7.0 | 8.4.0 | 9.8 GiB synthetic | BioFormats (5.0 vs 2.5) | 10 | yes / UINT8 | 0.484105 | 20 | 40, max diff 0 | BioFormats, 10 |

Baseline (2026-10-02 QuPath report): the classic output opens with 1 resolution
under the same default reader.

**Strict gate (round 2).** The first version of the harness could report
success for a failed check (review finding: `run_probe.sh` returned 0 after a
failed launch; `compare_regions.py` returned 0 with max diff 150 and cropped
mismatched shapes). Now:

* `run_probe.sh` returns QuPath's/timeout's status (127 launcher missing, 124
  timeout, any QuPath failure as-is) and, after a zero exit, 2 unless
  `check_probe.py` accepts the evidence: resolutions = levels, four region
  PNGs per level with exactly the box size, boxes inside the level, Bio-Formats
  grouping and project reopen present with the same resolution count; optional
  `--expect-resolutions/--expect-server-substring/--expect-rgb`.
* `compare_regions.py` exits nonzero on any pixel difference (default
  tolerance 0), missing PNG, shape ≠ box, box outside the reference level (no
  cropping), a level with no compared region, reference level count ≠ probe
  resolutions, fewer regions than `--expect-regions`, or fewer textured
  regions than `--min-textured`.
* `tests/test_qupath_probe_scripts.py` (31 tests, no QuPath needed) covers a
  missing launcher, a launcher exiting 3, a launcher that writes nothing or
  incomplete evidence, one-pixel-off, all-pixels-wrong, wrong shape,
  out-of-bounds box, missing PNG, uncovered level, level-count mismatch and
  too few textured regions. The old scripts returned 0 for the launch, silent
  and all-wrong cases; the reviewer's own all-wrong fixture now fails
  (`max_abs_diff 150 > 0`, exit 1), a real QuPath launch on a corrupt file
  fails with exit 1.
* Re-check with the strict gate: the reviewer's file (sha `14b71b61…`, same
  bytes as KFB-Q bf-ome) on QuPath 0.6.0-rc5 and 0.7.0 — `run_probe.sh … 0.0603
  0.9716 --expect-resolutions 9 --expect-server-substring BioFormats
  --expect-rgb` exit 0; `compare_regions.py --expect-regions 36 --min-textured
  12` exit 0, 36 regions, 16 textured, max diff 0. All earlier evidence
  (KFB-Q on rc5/0.6.0/0.7.0, 9.8 GiB on rc5/0.7.0) passes the strict checks
  unchanged.

Bio-Formats grouping (all rows): `loci.formats.in.OMETiffReader`, 1 series,
resolution count = levels above, `isRGB` true, effective SizeC 1, one channel
with SamplesPerPixel 3, PhysicalSize in µm, objective NominalMagnification 20.
Channel names in QuPath are Red/Green/Blue (one RGB image).

OME-XML schema: both OME-XML documents validate against the released OME
2016-06 `ome.xsd` (JDK validator) and Bio-Formats `XMLTools.validateXML`
("No validation errors found").

### 4.2 Not run here

macOS ARM64 QuPath (and Windows) — **external check**, see §9.

## 5. B — Preservation (KFB-Q real sample; synthetic 9.8 GiB)

| Check | KFB-Q | 9.8 GiB synthetic |
|---|---|---|
| Tiles byte-equal to classic | 31 577 / 31 577 | offsets+bytecounts equal on all 10 levels |
| Tiles verbatim in the source KFB (= `tiles_raw_copied`) | 30 957; 620 edge tiles re-encoded (same as classic) | — |
| Photometric / compression / spp / subsampling / resolution tags | 6 / 7 / 3 / [2,1] / equal to classic on every level | same |
| Decoded regions vs classic (tissue, background, right/bottom edges, corner; every level) | 45 regions, all equal | 20 regions on levels 0,1,5,9, all max diff 0 |
| Dimensions | 34043×45101 equal | 81000² … 158², equal |
| Calibration | OME PhysicalSize = classic mpp 0.48410487174987793 (both axes) | equal |
| Objective | 20 = 20 | 20 |
| Colors | Pillow vs tifffile decode of raw tiles diff 0; tissue mean RGB ≈ (193,132,198), background ≈ (245,…) | — |
| OME channel model | 1 channel, Interleaved=true | same |

Strict-lossless policy still refuses inputs that need edge re-encoding (Rust
test + C3 scenario g).

## 6. C — Browser (Chromium 151, Playwright 1.62.1)

| Flow | Result |
|---|---|
| Tool page local conversion + export (C3 a) | saved sha = native bf-ome `6e8744f9…`; suggested `bf-580x300.ome.tif`; OME-TIFF filter; format row shown |
| Real sample KFB-1 through the tool page | saved = native bf-ome `374c70c8…` (220 055 361 B); classic native still `385a59c6…` |
| Refresh/resume during conversion, crash points incl. IFD back-patch (finalize) and validation, cancel, legacy jobs (C2 matrix) | 29/29, incl. new `classic-profile-resume-matches`, `output-profile-change-refused`, `legacy-record-resumes-classic` |
| Refresh-resume / cancel 250 ms / offline / strict-lossless refusal (C3) | 18/18 |
| Direct upload from the tool page (C4) | 15/15; bf upload name `bf-580x300.ome.tif` |
| One-click convert-and-upload incl. workbench handoff, project association, reopen (R1) | 31/31 (`a-oneclick` uploads `bf-580x300.ome.tif`, uploaded sha = product sha) |
| Full >4 GiB conversion in the page (fresh profile, uncertain-disk dialog) | 9.79 GiB KFB → saved 10 509 701 545 B, sha = native `c0cef739…`; suggested `bf-10g.ome.tif`; stage+probe 48.7 s, convert 67.6 s, total 284 s |
| Oversized output remains savable locally (C4 f-oversize, R1 c1-oversize) | upload disabled with reason, save works, sha = native |

### 6.1 Low-memory measurement (same method as C2 §5: `run_mem_cgroup.sh`, cgroup simulation, MemoryMax as listed, no swap; baseline = blank harness page; increment = job RSS peak − baseline mean)

| Run | Limit | Profile | Input | Increment | wasm heap peak | Temp disk peak | Throughput | oom / oom_kill | sha = native |
|---|---|---|---:|---:|---:|---:|---:|---|---|
| cg4g-1g a | 4G | saver | 0.98 GiB | 114 MiB | 2.2 MiB | 2.0 GiB | 135.5 MiB/s | 0/0 | yes `bb1d3793` |
| cg4g-1g b | 4G | saver | 0.98 GiB | 103 MiB | 2.2 MiB | 2.0 GiB | 438.5 MiB/s | 0/0 | yes |
| cg4g-4g | 4G | saver | 4.43 GiB | 178 MiB | 2.2 MiB | 8.9 GiB | 254.4 MiB/s | 0/0 | yes `5c71229e` |
| cg4g-10g | 4G | saver | 9.79 GiB | 157 MiB | 3.2 MiB | 19.6 GiB | 222.2 MiB/s | 0/0 | yes `c0cef739` |
| cg8g-10g | 8G | balanced | 9.79 GiB | 130 MiB | 3.2 MiB | 19.6 GiB | 82.9 MiB/s | 0/0 | yes `c0cef739` |

Measured values only. Comparable to the classic C2 numbers (101–114 / 176 /
166–176 / 156 MiB). Throughput is **not** comparable across rows: these runs
overlapped with another test process on the host (the platform-chain test
development), which explains the 135.5 and 82.9 MiB/s outliers. `memory.current`
of the cgroup reaches the limit because it includes page cache from OPFS
writes; the `max` event counter (reclaim at the limit) is non-zero on the 4G
4.4/9.8 GiB runs, `oom`/`oom_kill` stay 0. The 4 GB / 8 GB *real device*
measurement remains external (as in C2).

Native CLI reference for the same 9.8 GiB input: 59.6 s, max RSS 4.8 MB.

## 7. Platform reader, ingestion and viewing

* `tests/test_bf_ome_platform_chain.py` (7 tests, new): synthetic KFB →
  CLI `bf-ome` → `POST /api/ingestions` as `*.ome.tif` → multipart PUT of the
  real bytes (fake COS) → `upload-complete` → real worker stages
  preparing…validating (real `slide_io.open_slide`, **no reader patch**) →
  ready/completed → cleanup. Then `/info` (dimensions, `mpp_source=metadata`,
  mpp/objective = source probe, `image_mode=native_rgb`, no channels), `/dzi`,
  tiles at the highest level (interior + edge) and two reduced levels compared
  with the classic decode, a run with the multichannel flag on, project
  creation + association + listing, reopen with a fresh session (same tile
  bytes/ETag), and a negative check that the slide is not exposed as three
  channels. The reader-level comparison is exact (0); endpoint tiles are
  re-encoded JPEG q82 4:2:0 and are compared with luma/mean/block tolerances
  plus a channel-swap correlation check. 4 of 7 fail on the previous reader
  (`image_mode` "multichannel", `is_native_rgb` False).
* `tests/test_slide_tools_upload_capability.py`: reader proof for the new
  format (TiffFileSlide, native RGB, read_region), proof set == viewable set.
* Browser viewing of a published bf-ome slide in the workbench: §7.1.

### 7.1 Workbench viewer in Chromium

`node tests/browser/slide_tools_c4/run_bfome_viewer.js --input <kfb1 bf-ome> --reference <kfb1 classic>`
(real Flask app + Postgres; `server.py --seed-bfome` puts the real bf-ome bytes
into a ready id-bundle slide; the reader is not patched). Real sample KFB-1:

* Login → project with the slide → open from the workbench sidebar → viewer.
* OpenSeadragon tile requests: 94/94 `200 image/jpeg` across 8 DeepZoom levels
  (9–16, 16 = full resolution; home → mouse wheel → 1:1).
* `/info` from the page: `image_mode=native_rgb`, 0 channels, mpp 0.4841 from
  metadata, max level 16; UI shows the RGB badge, no channel panel, a
  magnification zoom badge.
* Rendered canvas: tissue-coloured, non-blank (low zoom mean RGB
  [231,197,225], full resolution [233,137,199]); screenshots checked by eye
  (H&E pink/purple, nuclei visible) and kept private.
* Served tiles vs classic decode, 11 tiles on levels 8/9/12/15/16: worst
  per-channel mean diff 0.46, worst pixel MAD 4.36 (q82 4:2:0 re-encode);
  channel-permutation check rejects every swap (identity ≤ 0.59 vs ≥ 2.14).
* Reopen: page reload and a fresh browser context + re-login → project → slide
  → tiles 200, same info.

The synthetic default (no `--input`) passes as well.

Upload paths into the platform: direct upload (C4 a-bf) and one-click
convert-and-upload incl. project association and reopening from the project
(R1 a-oneclick, i-workbench-handoff, i4, l1) run in the browser against fake
COS/ingestion responses; the server-side ingestion of real bf-ome bytes is
covered by the platform-chain pytest above.

## 8. D — Regression

| Suite | Command | Result |
|---|---|---|
| Rust workspace | `cargo test --release --workspace --features slide-transform-core/fixtures` | 76 passed, 0 failed (incl. `bf_ome` 8, `resume` 10 with bf-ome resume, wasm 2) |
| Core parity (real samples vs Python oracle) | `pytest tests/test_slide_transform_core.py` | 16 passed, 1 skipped |
| Browser real-sample parity | `node tests/browser/slide_tools_c2/run_parity.js --samples <dir> --fl --fl-all` | KFB-1 `374c70c8…`; KFBF-A `6f8a1e3b…`, B `40cb12fa…`, C `5d6b1b21…` (= C2-pinned values, fluorescence unchanged), D `872fe3b6…` — all browser = native |
| Full pytest (serial, nothing else running) | `pytest tests -q --deselect tests/test_ai_budget_wiring.py::test_ui_budget_card_and_max_steps_sync_present` | 2886 passed, 10 skipped, **3 failed** — the known stale `tests/test_admin_preview.py` cases (`/api/upload` removed by the COS-only upload work; file untouched by this branch, fails identically on its own) |
| vitest | `npx vitest run --dir tests/js` | 41 files, 637 passed (round 2; was 630) |
| C2 smoke / faults | `run_smoke.js`, `run_faults.js` | PASS / 30/30 (round 2 adds `prepared-output-profile-set-rules`) |
| C3 e2e | `node tests/browser/slide_tools_c3/run_e2e.js` | 24/24 (round 2 adds o classic choice, p classic reload+resume, q list start, r fluorescence no choice, s classic kept across reload, t record wins after reload; s shown to fail without the reload guard) |
| C4 e2e | `node tests/browser/slide_tools_c4/run_e2e.js` | 16/16 (adds n classic upload: `bf-580x300.tif`, bytes = native classic) |
| R1 e2e | `node tests/browser/slide_tools_r1/run_e2e.js` | 31/31 (round 2 rerun; one e1 failure in the first rerun was a harness race reading `.job-row` before it rendered — 3/3 alone, fixed by waiting for the row, full rerun 31/31) |
| QuPath helper tests | `pytest tests/test_qupath_probe_scripts.py` | 31 passed |
| Classic byte compatibility | native classic on 9.8 GiB / KFB-1 | `2084a333…` / `385a59c6…` = pre-change values |

Regression tests shown to fail on old code: Rust `ext_bytes` layout test
(temporary revert), `test_ycbcr_jpeg_ome_is_native_rgb_not_three_channels`,
the platform-chain tests above, and the C2 profile scenarios.

## 9. Remaining external checks (not passes)

1. QuPath on **macOS ARM64** (0.6.0-rc5 and 0.7.0), on the installation where
   the problem was first seen, with the reviewer's file `user-bfome.ome.tif`
   (sha256 `14b71b615c6ad7fc6553d3e5cc7b3e9e72f9bd79111803527cd1c41550e56021`).
   Without changing preferences: drag the file into a new project → Image tab
   shows server Bio-Formats, 9 pyramid levels (Image › Server / pyramid
   info), pixel size ≈ 0.4841 µm, magnification 20; zoom from overview to
   full resolution on tissue, colors normal H&E; save, close and reopen the
   project and repeat. If QuPath is scriptable there, the same
   `scripts/qupath-probe/run_probe.sh <QuPath.app/Contents> …` gate can run
   (launcher path differs on macOS). Not runnable on this Linux host.
2. QuPath on Windows (same steps).
3. Real OS save dialog: the tests stub `showSaveFilePicker`; the filter text
   "OME-TIFF (.ome.tif)" and the suggested name must be checked by hand in
   Chrome/Edge on Windows/macOS.
4. Real 4 GB / 8 GB devices (memory numbers above are cgroup simulations).
5. Production deployment and production upload/viewing (out of scope here).

## 10. Commits (branch `bf-ome-tiff`, on top of `c914f23`)

| Commit | Content |
|---|---|
| `7f1eade` | core: `bf-ome` writer/profile, OME-XML, SubIFD validator, checkpoint profile, `ext_bytes` fix, Rust tests |
| `bd1b2c7` | platform reader: YCbCr-JPEG OME = native RGB; viewable format |
| `0528d57` | browser runner/page: default profile, persistence, legacy jobs, refusals, names/filters, wasm rebuild, JS/browser tests |
| `85df877` | platform-chain pytest; real-sample and large-page gates expect bf-ome |
| `6a1e696` | browser viewer gate (`run_bfome_viewer.js`, `server.py --seed-bfome`) |
| `0591075` | report, c1/c4 doc updates, `scripts/qupath-probe/` |
| `46eaad6` | strict QuPath gate: exit status, evidence checker, strict comparator, 31 negative/positive tests |
| `9909779` | tool page output-format choice (OME-TIFF / classic), saved to the job record, locked at start; browser + C2 + vitest coverage |
| (this) | report round 2; R1 harness waits for the job row |

## 11. Commands (worktree root; `TMPDIR=$PWD/.gate-tmp COLUMNS=200`)

```
bash scripts/build_slide_transform.sh
slide-transform convert <in.kfb> <out.ome.tif> --profile bf-ome --overwrite
slide-transform validate <out.ome.tif>
bash scripts/qupath-probe/run_probe.sh <QuPath root> <out-dir> <image.ome.tif> [fx fy] --expect-resolutions N --expect-server-substring BioFormats --expect-rgb   # nonzero = fail
.venv/bin/python scripts/qupath-probe/compare_regions.py <out-dir>/probe.json <out-dir>/regions <classic.tif> <out-dir>/region-compare.json --expect-regions 4N --min-textured K   # nonzero = fail
<QuPath root>/bin/QuPath script scripts/qupath-probe/xsd-validate.groovy -a <ome 2016-06 ome.xsd> -a <ome.xml>
node tests/browser/slide_tools_c3/run_large_page.js --input <bf-10g.kfb> --native-sha <sha>
bash tests/browser/slide_tools_c2/run_mem_cgroup.sh <label> <4G|8G> <1g|4g|10g> <saver|balanced>
node tests/browser/slide_tools_c3/run_real_sample.js --samples <dir>
.venv/bin/python -m pytest tests/test_bf_ome_platform_chain.py -q
```

Versions: rustc/cargo 1.98.1, wasm-bindgen 0.2.129, Node 22.23.2, Playwright
1.62.1 (Chromium 151.0.7922.34), vitest 3.2.7, Python 3.14.4, tifffile
2024.5.22, openslide-python 1.4.6 / libopenslide 4.0.1, Pillow 12.3.0, numpy
2.5.3, QuPath 0.6.0-rc5 / 0.6.0 / 0.7.0 (Linux builds).
