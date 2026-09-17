# Suite release — 2026-09-17

Released the remaining `wip/ser8-dev` work and the user-feedback fixes to the
existing production services on SSH alias `homepc`. Both repositories were pushed
to their GitHub `wip/ser8-dev` branches; no merge to `master` was performed.

## Deployed versions

| Component | Source revision | Production image / bundle |
| --- | --- | --- |
| HistoPilot service | `ecb8d47d22fac308d9533964be4c38ea30498411` | `localhost/histopilot-demo:suite-20260917` |
| HistoPilot browser plugin | same revision | `releases/histopilot-0.3.2` |
| PathTogether | `e53bcc87281f64bb7526c21c9247be50f55b1964` | `localhost/pathtogether-demo:suite-20260917-final` |
| Admin plugin | unchanged deployed bundle, now committed | `releases/pathtogether-admin-0.4.7` |

Image IDs:

- HistoPilot: `f4463e810c9df833185e6dce8e259820ebf05a0be679a13364eeaed41ede2382`
- PathTogether: `2abd410d008fbf0ab187cf3dc814222f668864a2675905ca30a51a41d2ff3d3d`

HistoPilot plugin manifest SHA-256:
`5b2b8c142d1be47f4f21ef9b1ec4f574569f55e0d227688ad929c803cb0d1f9f`.

The runtime dependencies matched the previous production images, so the release
reused those dependency layers and replaced application code with the committed
runtime sources/build output. This avoids changing dependencies as part of the
release. PathTogether's old application directory was removed in the build,
including the deleted standalone login template. Runtime file checks covered
223 PathTogether files and 33 HistoPilot files. The final review-link fix changed
only `app.py`; its deployed SHA-256 is
`4c424e969623e74cf5c45145632c5cad0f660302854a35fd79d8aa1a32989923`.

## Changes and deployment actions

- User-feedback fixes: summary delivery, stop/resume pairing, viewport context,
  evidence-aware answers, and sample-tool opt-out.
- Test application submission/review, activation and notification support.
- Homepage login dialog and SVG demonstration, including mobile and language
  behavior.
- Baidu import fixes and their regression tests. Production Baidu adapter/store
  hashes already matched the local fixes before this release.
- Updated three browser-test login helpers to use form fields rather than
  removed standalone-login element IDs.
- Production smoke checks found that existing notification emails pointed to
  `/admin/test-applications`, which had no route. Added an authenticated,
  owner-only redirect to `/admin#test-applications`, with regression coverage.
- Re-registered `sample-tma-score` through the platform installer so the existing
  production installation actually receives `agent_exposed=false`; verified the
  saved capability flag. No installation secrets were rotated.
- The admin 0.4.7 bundle was already identical to the committed bundle and was
  retained. HistoPilot switched atomically from the retained 0.3.1 bundle to 0.3.2.
- Container environment, commands, entrypoints, host network, data mounts and
  resource settings were compared before cutover. The plugin mount is now
  read-only, as required by the existing release runbook.
- Checked that no analysis, import or conversion job was running before cutover.
  Started the new sidecar before the new platform. Existing configuration and
  session volumes were retained.

## Validation

- HistoPilot TypeScript build and all 1,423 non-contract tests passed.
- PathTogether: 247 affected Python tests and 35 feedback/platform tests passed.
- After the review-link fix, all 23 test-application API/UI tests passed.
- All 444 PathTogether JavaScript tests passed.
- Three homepage browser tests passed locally and again against
  `https://histopilot.com`: complete SVG flow, annotation/language behavior,
  and mobile/reduced-motion layout.
- Five isolated browser tests passed for anonymous/user/owner admin access,
  iframe styling and user listing.
- Production read checks passed for the owner admin workspace, application
  list API, admin bundle HTML and review-link redirect. Admin trust/pin
  validation and sample-tool opt-out passed.
- Both public `/healthz` endpoints returned 200; platform reported PostgreSQL
  healthy and sidecar reachable. Public homepage/login returned 200, anonymous
  `/admin` redirected to login, and HistoPilot browser assets returned 200.
- Database migration `0054_test_applications.sql` was already applied in
  production. No new migration or account/credit adjustment was required.

No paid model generation or real Baidu transfer/download was triggered as part
of deployment verification. Production owner checks used an in-process session
for read-only requests; no user password was changed.

## Backup and rollback

Remote release files: `~/releases/suite-20260917/`.
Restricted backup directory: `~/svs-viewer-demo-data/backups/suite-20260917/`.

Backups contain a PostgreSQL custom-format dump, plugin/session archive,
encrypted HistoPilot configuration and restricted container configuration
snapshots. The database dump was restored into a temporary database and queried
successfully; the temporary database was then removed. Encrypted configuration
was decrypted and its archive listing checked. Credentials and database/session
contents are not stored in Git.

Retained stopped rollback containers:

- `histopilot-demo-pre-suite-20260917`
- `pathtogether-demo-pre-suite-20260917`
- `pathtogether-demo-pre-review-link` (intermediate release, before link fix)

To restore the pre-release application containers and HistoPilot plugin:

```sh
ssh homepc 'python3 ~/releases/suite-20260917/deploy.py rollback'
```

This retains failed/new containers for inspection and rechecks both local health
endpoints. It does not restore the database or erase new user data. The previous
sample capability metadata is retained in the shared-data volume as
`suite-20260917-sample-capabilities-before.json` for targeted restoration if needed.
