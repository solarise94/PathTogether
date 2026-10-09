# Admin / Viewer deployment

Deploy the `admin-viewer` branch from the current production lineage. This release
requires migrations through `0081_user_feedback.sql` and admin plugin **0.4.17**.
Set `APP_REVISION` to the full deployed Git revision for feedback diagnostics. Switch the
plugin bundle while the platform is stopped, then restart with the matching source
policy pin. Migration 0080 immediately ends old permanent admin view grants.

The platform entrypoint serves `share_server:combined_app`: `/s/*` uses the share
application; all other paths use the authenticated platform. A deployment running
only `app:app` cannot serve public share links, even if share API tests pass.

Set `SHARE_BASE_URL=https://histopilot.cn` in the production container environment.
The default `http://localhost:38000` is for local development and produces unusable
public links. Existing stored shares retain their tokens; the share list rebuilds
their URLs using the configured base.

Before switching, restore a read-only production database snapshot to an isolated
database, replace writable mounts, disable background workers, and check the
candidate. Preserve the old container, private environment backup, database dump
and plugin bundle for rollback. Never run candidate acceptance workers against
production. Migration 0080 is not fully compatible with the old permanent-grant
writer (`expires_at` is required); rolling back that feature requires a deliberate
database compatibility plan, not silently restoring an older live database.

After switching, check both public domains:

- `/healthz` reports platform and sidecar readiness.
- `/s/healthz` reports share storage readiness.
- A newly created share URL uses public HTTPS and renders in a fresh anonymous
  browser, including tiles, annotation save/reload, and permission denial for
  unrelated slide IDs.
- Admin plugin handshake, ordinary-user access denial, one-hour temporary view,
  explicit end, server expiry, and uploader access preservation.
- Actual uploads, thumbnails, nested folders, search/deep-link location and
  bounded sidebar pagination.
- The real AI plugin's `/api/ai/sessions?slide=sld_…` request succeeds for the
  uploader, remains scoped to that user's conversations, and denies unrelated
  users, expired admin views and deleted assets.

Use dedicated Dogfood accounts and synthetic images. Revoke shares, delete test
assets/folders, and disable those accounts when finished. Do not reset the real
owner's credentials or disable production CAPTCHA/email verification for tests.
