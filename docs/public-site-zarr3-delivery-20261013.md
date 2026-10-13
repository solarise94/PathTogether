# Public guides and TIFF/Zarr delivery

Implementation merged in PathTogether main aa144cd0 (PR #11). Production candidate is built from PR source 008af88f; its tracked tree matches merged main.

Changes:
- Release branch synchronized with seven dependency/CI commits on main.
- Registration anti-enumeration test uses a scoped fixed clock; response and enqueue assertions are preserved.
- tifffile 2026.9.20 and Zarr >=3.4.1,<4 upgraded together. Reader and QuPath probe create TIFF stores read-only. imagecodecs minimum follows the upstream codec extra.
- Homepage conversion workflow, bilingual copy, public feature/conversion/collaboration guides, HTML map, XML sitemap, Markdown and LLM indexes.
- Robots defaults to excluding unpublished resources. Published pages/assets are allowed; account and resource access controls remain enforced.
- AI crawler classification updated. Cacheable discovery does not create or mirror session cookies.
- Six-worker platform JS default; HistoPilot six-worker config and memory review committed separately (527ed60, documentation followup 6de82c5).

Validation:
- Targeted reader/render/registration/migration/probe: 251 passed.
- Discovery/CSRF contract: 125 passed.
- JS: 1115 passed, 2 skipped.
- Local full browser: 117 passed.
- Public browser check: 1440px/390px, no horizontal overflow or JS errors; metadata endpoints return correct MIME types without cookies.
- Local full Python: 3059 passed, 113 skipped, one source-inspection failure from source editing during the run; unchanged case passes with frozen source. Final PR CI includes frozen Python 3.12 full suite and coverage, all four jobs passed (38280922201).

Build and isolation:
- Remote cold dependency downloads repeatedly broke. Build uses verified production image 9cfa7211 as dependency baseline and offline CPython 3.12 wheels; pip check passes.
- New image 43642a95; 351 runtime files verified against source, 98 static asset hashes checked.
- Candidate tested against private production database snapshot with writable mounts isolated and outbound jobs disabled. Migration state in production remains unchanged.
- Synthetic registered ID-only OME slide opens with TiffFileSlide; info and thumbnail return 200. The private slide ID does not appear in discovery responses.
- Merged main aa144cd0 passed all four required jobs (run 38283622276): Python 3058 passed / 115 skipped, coverage 82%; browser 117 passed.
- Production now runs image 43642a95, tag site-canonical-20261013, runtime source 008af88f. Both public domains pass static integrity/CSP/health checks.
- Live dogfood found PUBLIC_BASE_URL still pointed to the old pt.solarise94.fun. Canonical public origin is now https://histopilot.cn; PUBLIC_ORIGINS preserves all three previously permitted origins. The CSP allow sets were verified unchanged against an isolated production snapshot before the configuration cutover.
- Both histopilot.cn and histopilot.com return 200 for 11 public endpoints. All 10 sitemap pages and 12 LLM index links resolve. Private /api/slides remains 401. Discovery responses have no session cookies.
- Old containers and private database backups retained for rollback. No new migration was introduced; production migration state remained unchanged.
- Independent #1/#6 bumps are closed, superseded by merged #11. Release branch synchronized to main.

Remote evidence: ~/releases/site-zarr3-20261013/ (configuration and database backups remain private).

Local evidence: public-site-20261013/{main-ci-ok.json,live-check.json,live-browser-check.json,main-python.log}. Private remote release/config records: ~/releases/site-zarr3-20261013/ and ~/releases/site-canonical-20261013/.
