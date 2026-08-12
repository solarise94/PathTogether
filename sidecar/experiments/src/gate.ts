/**
 * Phase 4 A/B framework (Wave 1: data plane) — NO-GO gate.
 *
 * Real-model data collection stays gated until `prompt_cache_key` passthrough is
 * verified against the real CPA gateway. This gate is the single enforcement
 * point: the Wave 2 execution runner MUST call {@link assertDataCollectionAllowed}
 * before issuing any real-model request.
 *
 * 依据：docs/ai-context-cache-visual-workspace-upgrade.md §14 Phase 3（显式 Prompt
 * Cache 验证 `prompt_cache_key`/breakpoint/usage 透传）。
 *
 * NOTE: experiment data plane only — NOT built into the shipped sidecar bundle.
 */

/** Mode of a run. `scripted` uses the fake streamFn (mechanism validation); `real-model` hits the real CPA gateway. */
export type DataCollectionMode = "scripted" | "real-model";

/** Env var that, when set to "1", lifts the real-model NO-GO gate. */
export const PHASE4_CPA_VERIFIED_ENV = "PHASE4_CPA_VERIFIED";

export class DataCollectionGateError extends Error {
	constructor(message: string) {
		super(message);
		this.name = "DataCollectionGateError";
	}
}

/**
 * Assert that the requested data-collection mode is allowed under the Phase 4
 * NO-GO policy.
 *
 * - `scripted`: always allowed (uses the fake streamFn; no real cache claims).
 * - `real-model`: allowed ONLY when `env[PHASE4_CPA_VERIFIED] === "1"`.
 *   Otherwise throws {@link DataCollectionGateError} with a CPA-UNVERIFIED
 *   message citing §14 Phase 3.
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
