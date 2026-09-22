# Suite release — 2026-09-22

Hotfix release for the public-registration dialog incident: the homepage and
`/login` register pane rendered the fail-closed copy（「注册暂不可用 / 协议文稿
发布中，公开注册暂未开放」）even though public registration was live.

## Root cause

`_entry_signed_in_context()` (homepage `/` and `/login`) passed
`registration_mode` but never injected `register_terms` / `register_research`.
Only the `/register` deep link (`_register_landing_page`) injected them. The
entry page dialog switches login → register in place via entry-auth.js with no
server round-trip, so the template's public branch
(`{% if register_terms and register_research %}`) fell through to the
fail-closed `else` for the primary user entry path. The 2026-09-21 deployment
verification covered only the `/register` deep link and missed this path.

## Deployed versions

| Component | Source revision | Production image / bundle |
| --- | --- | --- |
| PathTogether | `701d74f` | `localhost/pathtogether-demo:suite-20260922` |
| HistoPilot service | unchanged (`26fd2a9`) | `localhost/histopilot-demo:suite-20260922` (re-tag of `a1c67e34`) |
| HistoPilot browser plugin | unchanged | `releases/histopilot-0.3.4` |
| Admin plugin | unchanged | `releases/pathtogether-admin-0.4.12` |

PathTogether image ID:
`5da35ba1a2cf99d4b8d1f96a1bfa28c070446e5b993e5dd047e169ee237b75e2`.

No database migration (schema stays at 0065). Remote source:
`~/pathtogether-demo` fast-forwarded `4323436..701d74f`. Cutover used the same
staged-container deploy script with exact configuration comparison.

## Fix

- `app.py::_entry_signed_in_context` now merges
  `_public_register_agreements_context()` whenever the effective dialog mode is
  `public`, covering `/`, `/login` and `/register` from one place.
- Regression test `test_homepage_dialog_public_branch_has_agreements`
  (tests/test_public_registration.py): `GET /` and `GET /login` in effective
  public mode must render both checkboxes + agreement version fields and none
  of the fail-closed copy.

## Validation

- PathTogether: `test_public_registration.py` 36 passed (incl. the new
  regression test); entry/auth/invite/application suites 156 passed.
- Post-cutover: both `/healthz` green (platform reports sidecar reachable);
  `schema_migrations` = 65; `/api/registration/public-status` reports
  `mode=public, open=true, remaining=5`.
- Three entries render the public form: `/`, `/login`, `/register` all contain
  `terms_accepted` + `research_opt_in` (neither pre-checked) with
  `terms_version=2026-09-21-v4`, and none contain the fail-closed copy.
- `/legal/user-agreement/2026-09-21-v4`, `/legal/research-sharing/2026-09-21-v4`
  and `/legal/model-providers/2026-09-21-v4` all return 200.
- Browser end-to-end on the exact incident path (anonymous, production):
  homepage → 登录 → 没有账号？注册 via entry-auth.js in-place switch shows the
  register pane with both un-pre-checked checkboxes, quota copy and working
  agreement links (screenshot archived in session artifacts).

No paid model generation was triggered as part of deployment verification.

## Backup and rollback

Remote release files: `~/releases/suite-20260922/`.

Pre-cutover PostgreSQL custom-format dump:
`~/svs-viewer-demo-data/backups/suite-20260922/svs_demo.dump` (mode 600,
TOC-verified, 506 entries).

Retained stopped rollback containers:

- `histopilot-demo-pre-suite-20260922`
- `pathtogether-demo-pre-suite-20260922`

```sh
ssh homepc 'python3 ~/releases/suite-20260922/deploy.py rollback'
```

Rollback restores the pre-release containers and plugin symlink; it does not
touch the database (no migration in this release) or runtime settings.

## Known follow-up (not in this release)

The register dialog subtitle「验证邮箱并提交申请，管理员审核通过后即可使用。」
is the R2 invite-mode copy and is inaccurate in public mode (email verify → set
password → immediate account, no admin review). Candidate for the next
evening deploy: make the subtitle mode-aware.
