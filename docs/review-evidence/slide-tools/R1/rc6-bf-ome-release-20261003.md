# R1 r1-rc6: brightfield OME-BigTIFF output — release candidate (2026-10-03)

Baseline: production runs r1-rc5 (`1e5c27d`, image `suite-20261002-2`); `release/r1` was at
`c914f23` (rc5 plus docs). This candidate merges branch `bf-ome-tiff` into `release/r1`.
It contains no C7 backend retirement (C7 lives only on `slide-id-refactor`) and no other
features. Implementation and acceptance details: `docs/slide-tools/bf-ome-acceptance-report.md`.

**Candidate commit:** `8127ecb` (local annotated tag `r1-rc6`) on `release/r1`:
* merge commit `02e7624`;
* manifest refresh `7365b2c`;
* a test-only fix `8127ecb`.

Commits after the tag change only `docs/`. Nothing is pushed or deployed.

## What changes for users

* The tool page converts new brightfield KFB jobs to a pyramidal **OME-TIFF** by default
  (`<name>.ome.tif`). QuPath opens it with its default reader (Bio-Formats) and shows all
  pyramid levels; the classic output showed one. A new section, 「7 · 输出格式」, offers
  「经典金字塔 TIFF（OpenSlide 工具）」 (`<name>.tif`, unchanged classic bytes) for
  OpenSlide-based software. OpenSlide reads the OME output as a single level only.
* The choice is stored on the job and locked once conversion starts. Jobs created before
  this release resume and export as classic `.tif`. Fluorescence output is unchanged.
* The platform reads YCbCr-JPEG OME tiles as native RGB. `.ome.tif` brightfield results can
  be uploaded from the tool page and viewed in the workbench.

## Deployment-relevant facts

| Item | Status |
|---|---|
| Database migrations | **None** (`migrations/` unchanged since rc5) |
| Environment / feature flags | **None** added or changed; clone the running container's env verbatim |
| Deployment tooling (`deploy/`, `Containerfile`, `docker_entry.sh`, requirements) | Unchanged |
| Admin plugin | Unchanged (0.4.14); **no plugin link switch** |
| Baidu import plugin | Unchanged; its worker calls the CLI without `--profile`, which still produces classic |
| HistoPilot sidecar | Unchanged (`suite-20261002`, 0.3.5) |
| Shipped converter | `static/tools/slide-transform/slide_transform_bg.wasm` sha256 `6830819c…`, glue `slide_transform.js` `e0905c0c…`. Rebuilt from the integrated source with `scripts/build_slide_transform.sh` (rustc 1.98.1, wasm-bindgen 0.2.129): wasm, glue, both `.d.ts`, NOTICE and the native CLI were byte-identical; only the manifest's handwritten-file hashes were stale and were refreshed (`7365b2c`) |
| Browser caching | Production serves `/static` with `Cache-Control: no-cache` (checked on `engine.js` and `tools-slides.js`), so the unversioned ES-module imports revalidate; `tools-slides.js` and `i18n.js` also carry `?v=20261003` |

## Deployment steps (same pattern as `suite-20261002-2`; PT only)

1. Locally: `git archive --format=tar.gz -o pt-r1-rc6.tar.gz r1-rc6`; copy it to
   `homepc:~/releases/suite-<date>/`, extract into `src/`, write `SOURCE_COMMIT_PT`
   (= `8127ecb`).
2. Build on homepc: `podman build --network host -v ~/.config/pip/pip.conf:/etc/pip.conf:ro,Z
   --label org.opencontainers.image.revision=8127ecb -t localhost/pathtogether-demo:suite-<date> src`.
3. `deploy.py`: copy `~/releases/suite-20261002-2/deploy.py`, set `ROOT`/`TAG` to the new
   release, and **remove the admin-plugin switch** from `cutover_pt` (no `switch_plugin()`
   call; plugin unchanged). Keep `prepare`, `quiesce-check` and `cutover-pt` otherwise as is.
4. `deploy.py prepare` → staged container plus acceptance container on private port 18090.
   On 18090: `/healthz` 200, `/tools/slides` 200, page shows 「7 · 输出格式」 after choosing
   a KFB, `/static/tools/slide-transform/build-manifest.json` matches the candidate.
   Stop the acceptance container.
5. **Before cutover**, for the existing-job check, leave one brightfield conversion paused
   in a browser on rc5 (see checklist item 4).
6. `deploy.py quiesce-check quiesce.sql` must report all zeros; then stop the old platform
   container and run `deploy.py cutover-pt`; health gate (`/healthz`, sidecar reachable);
   public `/healthz` 200.

## Rollback

* Stop `pathtogether-demo`, rename it to `pathtogether-demo-failed-suite-<date>`, rename
  `pathtogether-demo-pre-suite-<date>` back to `pathtogether-demo`, start it, and run the
  health gate. No database, plugin or configuration rollback is needed.
* **Data written under rc6 that rc5 handles differently:**
  * bf-ome slides published while rc6 was live: rc5's reader classifies YCbCr-JPEG OME as a
    three-channel image. With `PATHTOGETHER_MULTICHANNEL_ENABLED=1` in production they
    would not display as normal RGB slides until rc6 is redeployed. Files and records stay
    intact. Keep production test uploads minimal and delete them after acceptance.
  * Unfinished bf-ome browser jobs: rc5's resume parser ignores the profile and resumes as
    classic. The tile stream is identical and IFDs are written at the end, so the result is
    a valid classic file. Finished-but-unsaved bf-ome results would be offered with a
    `.tif` name. Not exercised end to end; low impact.

## Automated gates on the integrated tree (serial, this host)

All gates ran on `7365b2c`. `8127ecb` changes one browser test scenario, and C3 was
re-run on it. Sanitised outputs are in `results/*rc6*.txt`.

| Gate | Result |
|---|---|
| Rust workspace (`cargo test --release --workspace --features slide-transform-core/fixtures`) | 76 passed, 0 failed |
| Baidu import plugin pytest | 83 passed |
| vitest (`tests/js`) | 41 files, 637 passed |
| Full pytest (`tests/`, nothing deselected) | 2917 passed, 10 skipped, **4 failed** — all four fail identically on the untouched release tip `c914f23` (see below) |
| QuPath strict default-reader gate, verified real-sample bf-ome (`14b71b61…`), 0.6.0-rc5 and 0.7.0 | `run_probe.sh` exit 0 (Bio-Formats, 9 levels, RGB, project reopen 9); `compare_regions.py` exit 0: 36 regions, 16 textured, max diff 0 |
| C2 smoke / fault matrix | PASS / 30/30 |
| C3 tool page (default locale; English host `LANG=en_US.UTF-8`; page forced to `en-US`) | 24/24 in each (forced-English run after the `8127ecb` fix) |
| C4 upload | 16/16 |
| R1 convert-and-upload, workbench handoff and projects | 31/31 |
| bf-ome workbench viewer gate (synthetic; real sample KFB-1) | PASS / PASS (tiles to full resolution, `native_rgb`, colours vs classic, reopen) |
| Real sample KFB-1 through the tool page | saved = native bf-ome `374c70c8…`; native classic still `385a59c6…` |
| Real-sample parity (KFB-1, KFBF-A..D) | browser = native for all five; KFBF hashes equal the C2-pinned values |
| 9.8 GiB input through the tool page | saved 10 509 701 545 B = native `c0cef739…`; suggested `bf-10g.ome.tif` |

**Failures present before this merge.** Each was rerun on `c914f23` (the `release/r1` tip
before the merge, no local changes) and fails the same way there:
* `tests/test_admin_preview.py`: 3 tests fail (`POST /api/upload -> 404`;
  `assert set() == {'owner.svs'}` / `{'o.svs'}`). `app.py` no longer has a `POST /api/upload`
  route (COS-only upload). These tests are recorded as failing in the rc2–rc5 evidence.
* `tests/test_ai_budget_wiring.py::test_ui_budget_card_and_max_steps_sync_present`:
  `assert '0.4.14' == '0.4.12'`. The test pins the admin plugin version and was not updated
  when rc5 shipped admin 0.4.14. Earlier gates deselected it.

**Gates not repeated for this candidate.** The cgroup memory series was not rerun; its
figures are in the acceptance report §6.1. It measured the same `worker.js` and the same
WASM bytes: the manifest hashes of `worker.js`, the WASM and the glue are unchanged since
those runs. `runner.js` and `engine.js` changed afterwards, but only to plumb the output-format choice through the job record.

## Manual checks (owner)

Not run by the agent; none are marked passed here.

1. **KFB → browser OME → real OS save dialog → QuPath.** Use a real KFB in Chrome or Edge:
   * Confirm 「7 · 输出格式」 defaults to OME-TIFF; convert; click save.
   * The native dialog proposes `<name>.ome.tif` with an OME-TIFF file type; save it.
   * Open it in QuPath with default settings (macOS ARM64; also Windows if available).
   * Expect: Bio-Formats server, uint8 RGB, the full pyramid, pixel size and
     magnification as in the source, normal H&E colour at full resolution.
   * Save, close and reopen the project; check the same values again.
2. **Upload → project → view → refresh/reopen.** Use 「转换并上传」 or the result's upload
   button with a small KFB:
   * Expect an upload name of `<name>.ome.tif`; it publishes.
   * Add it to a project from the workbench handoff; open it in the viewer.
   * Expect RGB badge, no channel panel, scale bar/magnification, normal colours when
     zoomed to full resolution.
   * Refresh the page and reopen project → slide; then delete the test slide and project.
3. **Classic TIFF.** Choose 「经典金字塔 TIFF（OpenSlide 工具）」 before converting:
   * Expect a `.tif` suggestion and a TIFF filter, and the job row shows the classic format.
   * Expect the file to open in an OpenSlide tool with all levels.
   * Change the choice on a prepared job, refresh, and start it from the job list. It must
     keep the saved choice.
4. **Existing-job resume.** Before cutover, start a brightfield conversion on rc5 and close
   the tab mid-conversion. After cutover, reopen `/tools/slides`:
   * The job row shows the classic format; resume completes.
   * The saved file is `.tif` and opens like the earlier classic outputs; it must not be OME.
5. **Fluorescence.** Convert a KFBF:
   * Expect no output-format section, `<name>.ome.tif`, and the multichannel OME-TIFF
     result row.
   * Expect it to save and upload and to view with its channels as before.
6. **Oversized result stays locally saveable.** Produce a brightfield result larger than the
   current COS relay limit (9 500 000 000 B; e.g. a synthetic 81000×81000 KFB,
   `slide-transform gen-kfb big.kfb --width 81000 --height 81000` → ~10.5 GB output; needs
   ~20 GiB free browser storage):
   * Expect upload disabled with the size reason.
   * Expect 「保存到磁盘」 to complete and the saved file size to equal the result.

Still pending regardless of the above: QuPath on Windows; real 4 GB / 8 GB devices. These
stay **pending** unless the owner supplies results.
