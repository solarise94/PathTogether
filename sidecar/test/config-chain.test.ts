/**
 * End-to-end config-chain tests (§9.2.1 P1 regression).
 *
 * Green unit tests previously did not cover the full
 *   Flask DEFAULT_CONFIG → _build_sidecar_config JSON → sidecar
 *   validateRunConfig → resolveCompactionSettings / resolveTransformSettings
 * chain, so three P1 bugs slipped through:
 *   - validateRunConfig treated null as 0 and rejected the public default
 *     config (ctx=null / visual budget=null / window_tier=balanced);
 *   - the tier-derived context window only reached buildModel, while
 *     compaction fell back to 272k and the visual reserve fell back to 8000;
 *   - no end-to-end assertion that the tier-derived 400k window and 60k
 *     budget are observed consistently across compaction + transform.
 *
 * The Flask-driven case spawns the repo's Python (`.venv/bin/python`, falling
 * back to `python3`) with an isolated SHARE_DATA_DIR so `_build_sidecar_config()`
 * reflects DEFAULT_CONFIG only. When no Python is available the suite is
 * skipped (vitest `test.skip`).
 */
import { execFileSync } from "node:child_process";
import { existsSync, mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";

import { beforeAll, describe, expect, it } from "vitest";

import { resolveCompactionSettings } from "../src/compaction.js";
import { resolveTransformSettings, WINDOW_TIER_PRESETS } from "../src/transform-context.js";
import { deriveEffectiveRunConfig, validateRunConfig, resolveEffectiveContextWindow, LEGACY_CONTEXT_WINDOW_TOKENS, type RunConfig } from "../src/agent-runner.js";

/** Repo root (parent of sidecar/). The Flask `app` module lives here. */
const REPO_ROOT = resolve(__dirname, "..", "..");
const VENV_PYTHON = join(REPO_ROOT, ".venv", "bin", "python");

/**
 * Locate a Python interpreter that can import the Flask app. Preference:
 *   1. <repoRoot>/.venv/bin/python (project venv)
 *   2. python3 on PATH
 * Returns null when neither is usable so the Flask-driven suite can skip.
 */
function findPython(): string | null {
	if (existsSync(VENV_PYTHON)) return VENV_PYTHON;
	try {
		// `which`-style probe: python3 --version exits 0 when present.
		execFileSync("python3", ["--version"], { stdio: "ignore" });
		return "python3";
	} catch {
		return null;
	}
}

const PYTHON = findPython();

/**
 * Inline Python script: isolates ai_config.json via SHARE_DATA_DIR, imports the
 * Flask app module, and dumps `_build_sidecar_config()` as JSON to stdout.
 *
 * `_build_sidecar_config` → `_load_ai_config` reads `_ai_config_path()`, which
 * is `_data_dir_for_secret() / "ai_config.json"`, and `_data_dir_for_secret`
 * honors the SHARE_DATA_DIR env var. With a fresh temp dir, no ai_config.json
 * exists, so the returned config is DEFAULT_CONFIG (+ base fields).
 */
const BUILD_SIDECAR_CONFIG_SCRIPT = `
import json, os, sys
# Repo root is passed as argv[1] so app is importable regardless of cwd.
sys.path.insert(0, sys.argv[1])
import app
cfg = app._build_sidecar_config()
sys.stdout.write(json.dumps(cfg, ensure_ascii=False))
`;

/** Spawn Python and return the parsed `_build_sidecar_config()` dict. */
function buildFlaskSidecarConfig(): Record<string, unknown> {
	if (!PYTHON) throw new Error("no python available");
	const tmpDir = mkdtempSync(join(tmpdir(), "svs-config-chain-"));
	const stdout = execFileSync(PYTHON, ["-c", BUILD_SIDECAR_CONFIG_SCRIPT, REPO_ROOT], {
		env: { ...process.env, SHARE_DATA_DIR: tmpDir },
		encoding: "utf-8",
		maxBuffer: 1 << 20,
	});
	return JSON.parse(stdout) as Record<string, unknown>;
}

/**
 * The production effective-config derivation (agent-runner.ts `runAgentLoop`
 * builds this ONCE and threads it through every consumer): when a valid
 * window_tier is set and context_window_tokens is not explicitly >0, fill in
 * the tier preset window. Imported from the production module so this test
 * exercises the real derivation instead of mirroring it.
 *
 * `resolveCompactionSettings` itself does NOT derive the tier window (only
 * `resolveTransformSettings` does, for its own budget/edges) — the derivation
 * lives at the run boundary, so the compaction assertions below go through
 * `deriveEffectiveRunConfig` exactly as production does.
 */
function deriveEffectiveConfig(cfg: Record<string, unknown>): Record<string, unknown> {
	return deriveEffectiveRunConfig(cfg as unknown as RunConfig) as unknown as Record<string, unknown>;
}

/**
 * Flask PUT-equivalent: run `_validate_ai_tuning` on `body`, persist, then
 * dump `_build_sidecar_config()`. Used for the reserve/keep=0 chain so the
 * JSON sidecar actually sees is the same shape Flask would inject at run start.
 */
const VALIDATE_AND_BUILD_SCRIPT = `
import json, os, sys
sys.path.insert(0, sys.argv[1])
import app
body = json.loads(os.environ["SVS_BODY"])
validated, err = app._validate_ai_tuning(body, {})
if err:
    sys.stderr.write(err)
    sys.exit(2)
cfg = dict(body)
cfg.update(validated)
app._save_ai_config(cfg)
out = app._build_sidecar_config()
sys.stdout.write(json.dumps(out, ensure_ascii=False))
`;

function flaskValidateAndBuild(body: Record<string, unknown>): Record<string, unknown> {
	if (!PYTHON) throw new Error("no python available");
	const tmpDir = mkdtempSync(join(tmpdir(), "svs-config-chain-"));
	const stdout = execFileSync(PYTHON, ["-c", VALIDATE_AND_BUILD_SCRIPT, REPO_ROOT], {
		env: { ...process.env, SHARE_DATA_DIR: tmpDir, SVS_BODY: JSON.stringify(body) },
		encoding: "utf-8",
		maxBuffer: 1 << 20,
	});
	return JSON.parse(stdout) as Record<string, unknown>;
}

// =========================================================================== //
// Flask-driven end-to-end chain (default config → JSON → sidecar resolution)
// =========================================================================== //
(PYTHON ? describe : describe.skip)("Flask default config → sidecar config chain", () => {
	/**
	 * Build the sidecar config ONCE for the suite. This reflects exactly what
	 * the public demo ships (no ai_config.json → DEFAULT_CONFIG). Re-resolving
	 * per test would re-spawn Python unnecessarily.
	 */
	let flaskConfig: Record<string, unknown>;
	beforeAll(() => {
		flaskConfig = buildFlaskSidecarConfig();
	});

	it("builds a config with the §9.2.1 default tier (balanced) and null ctx/budget", () => {
		expect(flaskConfig.window_tier).toBe("balanced");
		// Flask deliberately emits null so the sidecar derives from the tier.
		expect(flaskConfig.context_window_tokens).toBeNull();
		expect(flaskConfig.visual_context_budget_tokens).toBeNull();
	});

	it("validateRunConfig does not reject the public default config (Bug 1 regression)", () => {
		// This is the exact shape the demo sends; before the fix, Number(null)
		// === 0 caused a 400 "需为正整数".
		expect(() => validateRunConfig(flaskConfig as unknown as RunConfig)).not.toThrow();
	});

	it("resolveCompactionSettings uses the tier-derived 400k window (Bug 2 regression)", () => {
		// In production runAgentLoop builds the effective config (tier window
		// filled in) and passes it to resolveCompactionSettings. Before the fix,
		// resolveCompactionSettings read the raw config and fell back to 272k.
		const effective = deriveEffectiveConfig(flaskConfig);
		const comp = resolveCompactionSettings(effective as never);
		expect(comp.contextWindow).toBe(400_000);
	});

	it("resolveTransformSettings uses the tier-derived 60k budget + balanced image tiers", () => {
		// resolveTransformSettings derives the tier budget/edges internally, so
		// it can be called with the raw Flask config directly.
		const t = resolveTransformSettings(flaskConfig as never);
		expect(t.visualContextBudgetTokens).toBe(60_000);
		expect(t.overviewLongEdge).toBe(1024);
		expect(t.detailImageLongEdge).toBe(1280);
		expect(t.workingImageLongEdge).toBe(768);
	});

	it("compaction contextWindow and transform budget share ONE tier-derived source (consistency)", () => {
		// §9.2.1: the same 400k window feeds both the compaction trigger
		// (contextWindow) and the transform budget (15% of 400k = 60k). Before
		// the fix these diverged (272k vs 8000) because only buildModel saw the
		// tier window.
		const effective = deriveEffectiveConfig(flaskConfig);
		const comp = resolveCompactionSettings(effective as never);
		const t = resolveTransformSettings(flaskConfig as never);
		expect(comp.contextWindow).toBe(WINDOW_TIER_PRESETS.balanced.contextWindowTokens);
		expect(t.visualContextBudgetTokens).toBe(
			Math.ceil(WINDOW_TIER_PRESETS.balanced.contextWindowTokens * WINDOW_TIER_PRESETS.balanced.visualBudgetFraction),
		);
	});

	it("preserves reserve/keep=0 through Flask validate → sidecar validate → compaction", () => {
		// Reviewer P1: context=10000, reserve=0, keep=1 used to pass Flask and
		// validateRunConfig, then numOr rewrote reserve to 16384 at runtime.
		const flaskZero = flaskValidateAndBuild({
			window_tier: null,
			context_window_tokens: 10000,
			reserve_tokens: 0,
			keep_recent_tokens: 1,
		});
		expect(flaskZero.reserve_tokens).toBe(0);
		expect(flaskZero.keep_recent_tokens).toBe(1);
		expect(flaskZero.context_window_tokens).toBe(10000);
		expect(() => validateRunConfig(flaskZero as unknown as RunConfig)).not.toThrow();
		const effective = deriveEffectiveConfig(flaskZero);
		const comp = resolveCompactionSettings(effective as never);
		expect(comp.settings.reserveTokens).toBe(0);
		expect(comp.settings.keepRecentTokens).toBe(1);
		expect(comp.contextWindow).toBe(10000);
		expect(comp.settings.reserveTokens + comp.settings.keepRecentTokens).toBeLessThan(comp.contextWindow);
	});
});

// =========================================================================== //
// Pure-TS tier resolution (no Flask dependency)
// =========================================================================== //
describe("sidecar tier resolution (pure TS)", () => {
	it("saving tier derives 200k window, 20k budget, 768/1024/640 edges", () => {
		const cfg = {
			window_tier: "saving",
			context_window_tokens: null,
			visual_context_budget_tokens: null,
		} as unknown as Record<string, unknown>;
		expect(() => validateRunConfig(cfg as unknown as RunConfig)).not.toThrow();
		// Compaction sees the tier window via the effective config (mirrors
		// runAgentLoop threading effectiveConfig to resolveCompactionSettings).
		const comp = resolveCompactionSettings(deriveEffectiveConfig(cfg) as never);
		expect(comp.contextWindow).toBe(200_000);
		// Transform derives the tier budget/edges internally.
		const t = resolveTransformSettings(cfg as never);
		expect(t.visualContextBudgetTokens).toBe(20_000);
		expect(t.overviewLongEdge).toBe(768);
		expect(t.detailImageLongEdge).toBe(1024);
		expect(t.workingImageLongEdge).toBe(640);
	});

	it("explicit visual_context_budget_tokens overrides the tier-derived budget", () => {
		const cfg = {
			window_tier: "balanced",
			context_window_tokens: null,
			visual_context_budget_tokens: 12345,
		} as unknown as Record<string, unknown>;
		expect(() => validateRunConfig(cfg as unknown as RunConfig)).not.toThrow();
		const t = resolveTransformSettings(cfg as never);
		expect(t.visualContextBudgetTokens).toBe(12345);
		// Explicit budget does NOT change the tier-derived window (effective
		// config still fills the 400k window from the balanced tier).
		const comp = resolveCompactionSettings(deriveEffectiveConfig(cfg) as never);
		expect(comp.contextWindow).toBe(400_000);
	});

	it("manual mode (no ctx, no tier) derives the legacy 272k window", () => {
		const cfg = {
			window_tier: null,
			context_window_tokens: null,
		} as unknown as Record<string, unknown>;
		expect(resolveEffectiveContextWindow(cfg as unknown as RunConfig)).toBe(LEGACY_CONTEXT_WINDOW_TOKENS);
		expect(deriveEffectiveConfig(cfg).context_window_tokens).toBe(LEGACY_CONTEXT_WINDOW_TOKENS);
		expect(() =>
			validateRunConfig({
				...cfg,
				reserve_tokens: 300000,
				keep_recent_tokens: 20000,
			} as unknown as RunConfig),
		).toThrow(/context_window_tokens/);
		const comp = resolveCompactionSettings(deriveEffectiveConfig(cfg) as never);
		expect(comp.contextWindow).toBe(LEGACY_CONTEXT_WINDOW_TOKENS);
	});
});
