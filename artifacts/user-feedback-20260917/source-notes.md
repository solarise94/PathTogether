# Evidence and QA notes

## Scope

U1 is the requested account. Exact production `public.users` login lookup resolved to `usr_UpOLsStCJVs`, in a read-only transaction. No credentials or AI configuration were selected. `homepc` was reachable using the lowercase SSH alias. The session source is `svs-viewer-demo-data/sidecar-sessions`. Recursive owner matching found seven session JSON files, all in the root directory. This is the retained population, not proof that no historical record was deleted. Extract time is in evidence.json; the report uses Asia/Shanghai.

Only visible text, explicit tool arguments, selected events and session metadata were retained. Reasoning blocks are excluded. Institution, source filenames and case identifiers were replaced. Model medical statements are historical output, not verified findings. No full customer email appears in the report payload.

## Session map

| Alias | Session |
|---|---|
| S1 | sess_5598315ba8a342a6 |
| S2 | sess_0f8be31538304613 |
| S3 | sess_b166e60a8df74759 |
| S4 | sess_f00ef8e2eabf455f |
| S5 | sess_76f3db1d8a3b4dc9 |
| S6 | sess_796f9d20c9e141b2 |
| S7 | sess_39b4b47ce50245d8 |

Screenshot identities: codex-clipboard-42660e46-5dba-42fd-8366-e584fa271ed7.png; codex-clipboard-634a604d-898b-475a-82f6-354be0214633.png; codex-clipboard-e03e746f-a048-446d-9b1e-0ef968bad9f9.png; codex-clipboard-86ffa765-f385-422b-803e-6ad917af7709.png. They were supplied by the user; exact session correspondence is not asserted.

## Code and reproduction

HistoPilot local HEAD: 107fb3e. PathTogether local HEAD: e97ec4f with pre-existing changes. No business code was changed. Deployed frontend: plugins/releases/histopilot-0.3.1. Deployed and local SHA-256 agree:

- main.js: 5b824b3b326cbc0b1b446c25cca1f548d9fceb6a23933cfe1edd02f26b030bdd
- renderer.js: d50d307f93524edc1d4288965be32e72e97887d6618b8f19df6ef430585c97e4

This establishes frontend equality, not equality of every backend file.

summary-repro.test.ts borrows the sse-reconnect.test.ts harness and loads actual frontend code. Copy it temporarily into HistoPilot/test/user-feedback-repro.test.ts, then run from HistoPilot:

    ./node_modules/.bin/vitest run --project unit test/user-feedback-repro.test.ts

Actual result: one file, two tests passed; 267 ms total. Assertions confirm current defects, so passing is a successful reproduction, not a repair. Cases: a non-summary text bubble suppresses finish.summary; transcript replay ignores a finish-only summary. This is minimal DOM verification, not browser E2E. The temporary test file was removed; the evidence copy remains here.

## Calculations

build-report.py reproduces metrics.json and artifact.json from evidence.json and the Markdown document. Exclude platform spot_updated messages from human-message counts. Categories are mutually exclusive: 7 + 1 + 1 + 1 + 1 = 11. Eight nonempty completion events correspond to eight finish calls. Six final finished states plus one error state = seven sessions; event counts do not equal session/run counts.

ROI (31438,35499,3592,3111), snapshot 1 (31152,34952,4096,4096): intersection / snapshot = 0.6660647392. Snapshot 2 (34288,35688,1024,1024): intersection / snapshot = 0.724609375. Outside fractions round to 33.4% and 27.5%. These measure geometry, not attention or clinical error. Both navigation centers are inside the ROI.

No clinical accuracy, global user incidence, reading exposure, screenshot-to-event identity or continuous active time is inferred. No uplift forecast is based on this account.

## Report contract

Audience: product stakeholders. Delivery: portable HTML document; Markdown is its editable source. Ordered roles: title, Executive Summary, behavioral evidence, both issue analyses, recommendations, priorities/acceptance, questions/limitations. One native bar chart compares five human-message intent categories. Y is count from zero; no time trend or causal comparison. The adjacent paragraph explains the denominator and implication. Timeline and acceptance tables support exact lookup. The analytics portable renderer builds the HTML and records verification in the delivery receipt.

## Final packaging QA

The Python count was independently reconciled with intent-counts.sql executed against SQLite JSON1 over the same extract (session_extract.payload). This query materially produces the chart rows, and is embedded in chart provenance.

The stock renderer overflowed because its shared analytics-top-bar uses 100vw with negative margins, extending by half the visible system scrollbar width. Browser inspection isolated this element. package-report.mjs applies a document-local containment override while using the canonical builder, chart extraction and verifier; the plugin files and report data remain unchanged. Final receipt: validation passed, package passed, browser verification passed at 1440 and 390 pixels, source dialog interaction passed. See delivery-receipt.json.

Code references: main.js and renderer.js refer to HistoPilot/integrations/pathtogether/ui; server.ts, prompts.ts, tools.ts, agent-runner.ts and transcript.ts refer to HistoPilot/src. PathTogether/static/app.js:7094 provides the temporary ROI-before-selected-rectangle precedence.
