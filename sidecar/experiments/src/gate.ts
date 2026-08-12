/**
 * Phase 4 A/B framework (Wave 1: data plane) — data-collection gate.
 *
 * The gate is the single enforcement point for real-model data collection: the
 * Wave 2 execution runner MUST call {@link assertDataCollectionAllowed} before
 * issuing any real-model request. It requires `PHASE4_CPA_VERIFIED=1`.
 *
 * ## Gate policy (updated 2026-08-12 — LIFTED for the openai-protocol path)
 *
 * Cache observability on the real CPA gateway (http://198.51.100.10:46450) has
 * now been VERIFIED for the **openai-protocol** path:
 *   - model `gpt-5.6-luna`, non-stream probe with `prompt_cache_key` set,
 *     returned `cached_tokens=3438` on repeat requests (≈99.9% prefix hit).
 *
 * So for openai-protocol real-model runs (the default for cache experiments),
 * `PHASE4_CPA_VERIFIED=1` is now the EXPECTED setting and the gate's purpose
 * (verify cache observability before formal data collection) is satisfied.
 *
 * ## Gemmini-protocol caveat (CPA antigravity bug #592, v7.2.129)
 *
 * The gemini path (`gemini-3.6-flash-high`) does NOT report cache hits —
 * `cachedContentTokenCount` stays 0 on repeated identical requests — so
 * real-model cache conclusions ONLY apply to openai-protocol arms. Do NOT draw
 * cache-hit conclusions from gemini-protocol real-model runs. The gate still
 * requires the env var for gemini runs too (it does not branch by protocol);
 * the gemini caveat is a data-interpretation warning, not a code gate.
 *
 * ## Code behavior (UNCHANGED)
 *
 * The check code below is intentionally left as-is: real-model still requires
 * `env[PHASE4_CPA_VERIFIED] === "1"`. This comment documents the LIFTED
 * policy (the env var is now expected to be set for openai-protocol runs), not
 * a removal of the check. Leaving the env requirement in place keeps an
 * explicit opt-in footgun guard for any operator who copies the command without
 * understanding the cost.
 *
 * 依据：docs/ai-context-cache-visual-workspace-upgrade.md §14 Phase 3（显式 Prompt
 * Cache 验证 `prompt_cache_key`/breakpoint/usage 透传）。
 *
 * NOTE: experiment data plane only — NOT built into the shipped sidecar bundle.
 */

/** Mode of a run. `scripted` uses the fake streamFn (mechanism validation); `real-model` hits the real CPA gateway. */
export type DataCollectionMode = "scripted" | "real-model";

/**
 * Env var that, when set to "1", lifts the real-model gate. As of 2026-08-12 the
 * gate policy is LIFTED for the openai-protocol path (gpt-5.6-luna
 * cached_tokens verified); this env var is the EXPECTED setting for openai-
 * protocol real-model runs. See the file header for the gemini caveat (#592).
 */
export const PHASE4_CPA_VERIFIED_ENV = "PHASE4_CPA_VERIFIED";

export class DataCollectionGateError extends Error {
	constructor(message: string) {
		super(message);
		this.name = "DataCollectionGateError";
	}
}

/**
 * Assert that the requested data-collection mode is allowed under the Phase 4
 * data-collection policy.
 *
 * - `scripted`: always allowed (uses the fake streamFn; no real cache claims).
 * - `real-model`: allowed ONLY when `env[PHASE4_CPA_VERIFIED] === "1"`. As of
 *   2026-08-12 the openai-protocol path is verified (gpt-5.6-luna cached_tokens
 *   probe), so this env var is the EXPECTED setting for openai-protocol runs.
 *   The gemini path remains cache-unobservable (#592). Otherwise throws
 *   {@link DataCollectionGateError} with a CPA-UNVERIFIED message citing
 *   §14 Phase 3.
 *
 * @param mode the collection mode being attempted
 * @param env the process env (defaults to process.env)
 */
export function assertDataCollectionAllowed(mode: DataCollectionMode, env: NodeJS.ProcessEnv = process.env): void {
	if (mode === "scripted") return;
	if (mode === "real-model") {
		if (env[PHASE4_CPA_VERIFIED_ENV] !== "1") {
			throw new DataCollectionGateError(
				`CPA-UNVERIFIED：real-model 采数被 Phase 4 NO-GO 门禁拦截。` +
					`prompt_cache_key 透传尚未在真实 CPA 网关上验证，缓存命中率数字不作为正式结论。` +
					`依据 docs/ai-context-cache-visual-workspace-upgrade.md §14 Phase 3。` +
					`验证通过后设置 ${PHASE4_CPA_VERIFIED_ENV}=1 解除。`,
			);
		}
		return;
	}
	// Exhaustive guard: a new mode must be handled explicitly.
	throw new DataCollectionGateError(`未知采数模式 "${String(mode)}"；仅支持 "scripted" 或 "real-model"。`);
}

/** Non-throwing check (for runners that want a boolean). */
export function isDataCollectionAllowed(mode: DataCollectionMode, env: NodeJS.ProcessEnv = process.env): boolean {
	try {
		assertDataCollectionAllowed(mode, env);
		return true;
	} catch {
		return false;
	}
}
