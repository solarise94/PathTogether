# Suite release — 2026-09-20

Released the viewer/demo collaboration batch (plan
`docs/viewer-demo-collaboration-review-plan-20260919.md`, six review rounds
closed) to the existing production services on SSH alias `homepc`. Both
repositories were pushed to their GitHub `wip/ser8-dev` branches; no merge to
`master` was performed.

## Deployed versions

| Component | Source revision | Production image / bundle |
| --- | --- | --- |
| HistoPilot service | `26fd2a9` | `localhost/histopilot-demo:suite-20260920` |
| HistoPilot browser plugin | same revision | `releases/histopilot-0.3.4` |
| PathTogether | `1dae167` | `localhost/pathtogether-demo:suite-20260920` |
| Admin plugin | unchanged | `releases/pathtogether-admin-0.4.10` |

Image IDs:

- HistoPilot: `a1c67e34f6277bbc9cd80beaa46ae7a43f5a775ffadd877664c2bbed538b73c3`
- PathTogether: `7d630e530f1db30d3b87a81b43151099bb57f72c1b7ea472fdaeffd6a18478b7`

HistoPilot plugin manifest SHA-256:
`e8cf1553e2823506d57a27a81bee2225afb0092450dd65bc79d6587ccc7f1d76`.

Remote sources: `~/pathtogether-demo` fast-forwarded to `1dae167`;
HistoPilot exported via `git archive` into
`~/releases/suite-20260920/build/histopilot-src`. Dependency layers were
reused from previous production images. Cutover used the staged-container
deploy script with exact configuration comparison (env, mounts, ulimits,
network, restart policy, resources) before switching.

## Changes released

- Annotation data isolation: subject-scoped visibility
  (local_owner/user/visitor/ai/guest), fail-closed unbound AI subject,
  public projections without share bearer tokens.
- Explicit grants (`annotation_grants`) for user / share_token grantees plus
  a grant/revoke audit event stream (`annotation_access_events`, revoke
  carries `reset_required`). 🌐 toggle grants/revokes every active share
  that includes the slide; share creation auto-grants the owner's own
  annotations.
- Annotation writes addressed by `annotation_id`; DELETE returns the
  authoritative tombstone revision; undo deletes and redo restores from
  tombstones with operation-revision CAS.
- Frozen attachment pipeline: gateway marker-visibility + revision-CAS
  validation, wire attachments parsed strictly server-side, persisted into
  the request ledger with keyed `attachments_dispatched` dedupe across all
  recovery paths; render context fingerprints derived platform-side with
  byte-exact Python parity (including exact binary64 midpoint rounding).
- Viewer right-click menu: add current viewport / annotation to the AI
  session (frozen payloads; honest error when the plugin is absent).
- Demo: bilingual catalog columns (additive; falls back to the default
  language), single-run step default 20 → 100. Entry page What's New feed.

## Validation

- HistoPilot: full suite 1,482 tests green plus 19 cross-repo contract
  tests (real PathTogether + real PostgreSQL). `tsconfig.build.json`
  clean; default `tsc --noEmit` error set byte-identical to clean HEAD
  (pre-existing DOM-lib test-file errors).
- PathTogether: 2,295 Python tests passed; remaining 2 failures
  (`test_e2e_pg_reap`) reproduce identically on clean HEAD (environment
  process-reaping, also `_ci_skip`-marked). All 502 JS tests passed.
- Migration drill: production schema-only dump restored into a local
  PostgreSQL, 55 recorded migrations injected, `ensure_schema` applied
  exactly 0056–0059, idempotent re-run clean, new tables/indexes/columns
  verified.
- Browser acceptance against a real local instance (real openslide, real
  tiles, two accounts): rect drag created and persisted an annotation
  (private by default); Ctrl+Z deleted it (annotation_id-addressed);
  Ctrl+Y restored it from tombstone (revision advanced to 3); right-click
  menu offered viewport/annotation attach items; share creation emitted an
  auto-grant access event; 🌐 grant/revoke produced grant and revoke events
  (revoke carried reset semantics); an anonymous visitor on the share link
  saw the granted annotation as a read-only projection; a second logged-in
  account saw the public slide but none of the owner's annotations.
- Post-cutover: both `/healthz` endpoints green (platform reported
  PostgreSQL healthy and sidecar reachable), `schema_migrations` = 59 with
  0056–0059 on top, `annotation_grants` / `annotation_access_events` /
  `ai_session_principals` present, `ai_safety.demo_task_max_steps` = 100,
  121 live ROIs retained. Public checks: homepage 200 with the What's New
  feed, `/demo` 200, anonymous `/admin` redirected to login, plugin assets
  remain login-gated.

No paid model generation was triggered as part of deployment verification.

## Backup and rollback

Remote release files: `~/releases/suite-20260920/`.

Pre-cutover PostgreSQL custom-format dump:
`~/svs-viewer-demo-data/backups/suite-20260920/svs_demo.dump` (mode 600).
The dump was restored into a temporary database, queried (55 migrations),
and the temporary database removed.

Retained stopped rollback containers:

- `histopilot-demo-pre-suite-20260920`
- `pathtogether-demo-pre-suite-20260920`

To restore the pre-release application containers and HistoPilot plugin:

```sh
ssh homepc 'python3 ~/releases/suite-20260920/deploy.py rollback'
```

This retains failed/new containers for inspection and rechecks both local
health endpoints. It does not restore the database or erase new user data.
