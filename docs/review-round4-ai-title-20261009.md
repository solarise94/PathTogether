# Round 4 review and AI conversation UI — 2026-10-09

Reviewed `admin-viewer` at `b4258e3e`, including feedback collection/submission,
admin user counts and research consent, retirement of test applications, slide
preview layering, and AI panel geometry. Implemented the selected title-switch
conversation layout in the platform worktree. No production data was changed.

## Confirmed findings and fixes

| Priority | Finding | Evidence and correction |
| --- | --- | --- |
| P1 | Feedback copied arbitrary console arguments, exception/rejection/network error messages and raw exception source URLs. These can include input, secrets and query strings despite the privacy promise. | Regression populated all four paths with `PRIVATE-*` probes and failed before the fix. Console records now carry only severity; errors carry an allowlisted type and sanitized script path/line/column; network failures carry a fixed category. Console behavior and thrown errors are unchanged. Share claim and legacy annotation URL credentials are masked too. |
| P1 | Server feedback forwarded raw audit `target_id` and `detail`. A normal `share.create` audit uses its bearer token as `target_id`; audit storage does not sanitize arbitrary details. | A real PostgreSQL regression inserted a share audit and observed both token and detail in feedback context before the fix. Feedback now selects only event ID, timestamp, role, action and target type. Original audit rows are unchanged and remain available for investigation by event ID. |
| P2 | A mobile-first page never installed dock pointer listeners, so switching to desktop could not enable dragging. | New unit regression fails before the fix; handlers now install on both layouts and check the breakpoint when used. Real-plugin browser coverage crosses the breakpoint and drags the panel. |
| P2 | Dock minimum size overrode a smaller viewport, extending below the viewer. | A 230 × 180 viewer produced a 280 × 240 panel. Regression fails before the fix. Actual frame size now takes priority over preferred minimum size. Browser coverage checks a short viewport. |
| P2 | Saved panel position/size did not restore after refresh. Dock initialization read the anonymous scope before asynchronous authentication; subsequent writes used the user scope. | Real-plugin browser test resized to 400 px, refreshed and observed 340 px before the fix. Authentication now reloads geometry when the user scope changes; the same browser test passes. |

The reviewed real-user count, balance/research columns, test-application
retirement and slide preview changes did not produce further confirmed findings.
This is a scoped code review with regression checks, not a guarantee that every
possible production behavior was exercised.

## Final interface

- One AI header: current conversation title/dropdown, new conversation, options,
  and close. Desktop buttons are 40 px; mobile buttons are 44 px.
- The title dropdown shows eight entries initially, with search and an explicit
  all-conversations action. Main and branch sessions use the existing plugin
  selector and preserve its loading, selection and stale-response protections.
- New conversation dispatches the existing draft action. It does not start an AI
  run or erase previous conversations. Leaving and returning preserves unsent
  draft text and its drawing preference.
- Drawing, path and latest-view controls move into options. Service settings are
  further collapsed; user settings remain read-only, while owner settings remain
  editable. Missing configuration has a visible settings entry.
- Existing stop/continue, attachments, transcript rendering and folded tool
  details remain under the plugin's state machine. No session rename API was
  added; there is no nonfunctional rename entry.
- Header controls do not drag the panel. Blank header/grip dragging, resizing,
  per-user restoration, mobile behavior and keyboard dismissal are covered.
- Feedback now also explains which account/allowance/audit/task information the
  server adds. The JSON preview remains the browser payload preview.

`static/ai-panel-chrome.js` is a presentation adapter over the real plugin's DOM
controls. It does not replace plugin methods or issue AI requests. HistoPilot
source and plugin version were not changed. Tested against HistoPilot release
source `23e89fa` (`release/hp-0.3.5`).

## Verification

| Gate | Result |
| --- | --- |
| Vitest, full | 1003 passed, 2 existing skips |
| Playwright, existing full suite on a fresh isolated server | 83 passed |
| Real HistoPilot UI integration gate | 5 passed |
| Feedback backend tests | 12 passed |
| Total-allowance test file after fixing the time-dependent assertion | 24 passed |
| Pytest, final full suite (no ignored files) | 2939 passed, 113 existing skips |

The first full pytest run had 2938 passes, 113 skips and one failure in the existing
`test_settle_release_expire_projection_accurate`. It advances time by ten minutes
but asserted that a new hold has the old hold's price. This run crossed Shanghai's
14:00 tariff boundary. The test now injects a fixed Friday 13:55 → 14:05 timeline
and asserts the new reservation using the later tariff. The fixed timeline
reproduced the old assertion failure independently of the launch time, and all
24 tests in the file then passed. Billing production code is unchanged.

The two Vitest skips are existing JS/Python sniffer parity checks whose Python
module cannot import in this environment. Python skips include native converter
and real-sample gates; skipped cases are not counted as passes.

The real-plugin gate is explicit rather than silently substituting panel markup:

```sh
# The directory must contain histopilot/ from the supported plugin release.
PATH="$PWD/.venv/bin:$PATH" \
PLUGIN_BUNDLES_DIR=/absolute/path/to/plugin-bundles \
E2E_PORT=8947 npm run test:e2e:ai
```

This gate uses real platform login, slide rendering, template, installed plugin
JavaScript, host bridge and panel controls. AI config/session/transcript responses
are deterministic browser fixtures. It validates the real UI/state integration,
not paid inference or live provider availability. No actual feedback email was
sent; queue/worker behavior is covered by backend tests.

Screenshots are local review artifacts in `.gate-tmp/ai-title-review/`:
`main.png`, `history.png`, `options.png`. They show the real UI with fixture
conversation content. Regression command logs are under `/tmp/ai-*.log`.

The initial review stopped before deployment. The user subsequently authorized
removing the viewport attachment button and deploying. That release, the production
dogfood findings (including unresolved live AI failures), and cleanup are recorded
in `admin-viewer-implementation-evidence-20261008.md`, section 11. The UI adapter
adds no migration and requires no HistoPilot plugin release.
