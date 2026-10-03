# R1 rc6 production deployment and checks — 2026-10-03

Deployed at the owner's request from local tag `r1-rc6`, commit `8127ecb`.
Production image: `localhost/pathtogether-demo:suite-20261003`.
The previous rc5 container is stopped and retained as
`pathtogether-demo-pre-suite-20261003`. No branch or tag was pushed.

## Release procedure

- Built on homepc from `git archive r1-rc6`; image revision label is `8127ecb`.
- Cloned rc5 environment, mounts, command, network, restart policy and limits;
  deployment helper verified shape and environment equality. No plugin switch.
- Four conversion/Baidu switches remain `0`. COS capacity is 10,000,000,000 B,
  safety margin 500,000,000 B, maximum upload 9,500,000,000 B.
- No schema migration, configuration change or sidecar/plugin update. Schema
  still has 77 applied migrations. C7 is not included.
- Saved a private 3,260,985-byte database dump; restored it successfully into a
  separate temporary database and read its schema and baseline quota. Dropped
  that temporary database after the checks. Production was not restored.
- Private acceptance container on port 18090 passed conversion/upload/viewing
  checks and was stopped. In-flight upload, ingestion, deletion, conversion,
  Baidu, producer and staging-slide counts were zero before stopping rc5 and
  again after stopping it. Then swapped containers and passed the health gate.
- Public health checks on pt.solarise94.fun and histopilot.cn passed with the
  sidecar reachable. Public manifest, WASM, worker, runner and engine bytes
  match the candidate. TLS verification was enabled.

## Browser and real-service checks

Chromium automation used an ordinary existing test account, generated synthetic
files, real production permission checks, real COS transfers and real platform
readers. Private acceptance requests were forwarded to port 18090 while retaining
the public HTTPS origin for CSP/CORS. Post-cutover checks used the public HTTPS
site directly, including worker/static requests. No upload, project, reader or
COS responses were mocked.

38 conversion/release assertions passed. These are assertions in the private
acceptance harness, not a repeat of the full regression suites documented in
the candidate handoff.

| Check | Observed result |
|---|---|
| Workbench KFB handoff → popup → default OME → upload → target project | Passed privately and on public production; actual project membership and published receipt checked |
| OME bytes | Browser result and streamed export match native; 9,537,416 B, SHA-256 `a16faa8a24c2700a4643e18af4705c68c5f13b456c8019ff16b2cbc7758d7b3e` |
| OME viewing | `native_rgb`, zero logical channels; 15 tile requests per opening, levels 9–12 including full resolution; project reload/reopen works |
| Served colors | Three platform tiles compared with native classic reference; worst channel mean difference 0.039, worst pixel MAD 23.742 within the synthetic-noise tolerance 30 |
| Classic choice | Browser and exported bytes match native classic; upload, project association, RGB viewing/full-resolution tiles and reopen pass |
| Fluorescence | No format choice; three-channel OME output/export match native; upload, project association, multichannel viewing/full-resolution tiles and reopen pass |
| Genuine rc5 interrupted task → rc6 | Killed the compute worker after a real journal checkpoint, reloaded, then resumed after cutover; result remains classic and matches uninterrupted native output, 640,968,341 B, SHA-256 `b1326c19617f5f2a2c143225a21fb141bc2f21e021b8b690b0a72cc6fa2db494` |
| Over-limit admission | Declared 9,500,000,001 B returns 413 `upload_too_large` |
| Loaded tool page offline | Conversion and streamed export match native, with zero attempted network requests |

The save picker was an automated stand-in writing chunks to a local filesystem
file. This verifies the page's export bytes and suggested `.ome.tif` name/type;
it does **not** verify the real OS save dialog. The 9.8 GiB full-page run was not
repeated against production; its candidate acceptance evidence remains separate.
Neither real low-memory devices nor macOS/Windows QuPath were run by this agent.
The owner's earlier Mac opening/metadata screenshot remains valid evidence;
Mac full-resolution inspection and project reopening still need owner confirmation.

Harness setup issues were corrected before recording passes: the project-detail
response wraps `project`; the writable interface receives positioned write
commands; sidebar visibility must be rechecked after application boot. No
application-code changes were made to address those harness issues.

## Cleanup

- Deleted only the five test-created slides and three test-created projects,
  including the first acceptance upload created before a harness assertion failed.
- Original visible slide and project ID sets match the saved baseline.
- Deleted all local test jobs. The interrupted rc5 job was also removed after
  its successful resume.
- Five test ingestions are `completed`, remote `cleanup_status=cleaned`, and
  `local_cleanup_status=cleaned`; all five slide assets are deleted tombstones.
- Test-account used storage restored from the snapshot baseline to 6,359,395 B;
  reserved storage is zero. No ledger rows were edited manually.
- COS pool reserved and observed remote bytes are zero, reconciliation `ok`.
- Raw identifiers, credentials, browser profiles, screenshots, logs and backup
  stay in private operator evidence; none are committed here.

## Finding outside the OME change

**AI session listing for newly uploaded slides returns 403.** During actual
project viewing, the installed UI sends `/api/ai/sessions?slide=<slide_id>`.
The same ordinary user receives slide info/tiles successfully, but session listing
returns `{"error":"无权访问"}`. This was observed for OME, classic and fluorescence
test uploads. AI execution was not started or billed during these checks.

The session-list route is unchanged from rc5; the only `app.py` change in rc6 is
adding the new viewable converter format. Source inspection shows this route
still calls the legacy `can_view_slide(slide)` path instead of the ID-aware
resolver. Treat that as the preliminary diagnosis, not as a tested correction.
The issue is outstanding; conversion/upload/viewing passes do not claim AI
session listing passes. A follow-up should cover owned ID-bundle assets, legacy
assets, explicit grants and unauthorized users without relaxing isolation.

## Rollback limitation

The old container is retained; no database rollback is needed for this release.
However, rc5 reads new brightfield OME files as multichannel rather than native
RGB. Its resume parser also ignores the new output profile. A rollback is
therefore not transparent to new OME results or unfinished rc6 browser jobs.

During a rollback, preserve OPFS data and do not continue rc6-created OME jobs
under rc5; resume them once rc6 is restored. A full rc6 → rc5 → rc6 browser-job
round trip was not exercised. The shared tile/scratch layout alone does not
prove the profile records remain coherent across that round trip.
