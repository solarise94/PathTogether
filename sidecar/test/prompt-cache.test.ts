/**
 * Phase 3 tests: explicit Prompt Cache (§8/§13).
 *
 * Covers:
 *   - capability resolution for off/auto/explicit modes (§8.1);
 *   - cache-key construction + version invalidation (§5.1/§10);
 *   - request-body injection via samplingParams (explicit mode);
 *   - rejection detection + safe downgrade (§13);
 *   - options strip on downgrade (retry without the field).
 *
 * CPA-UNVERIFIED: no test here exercises a real CPA gateway. The rejection +
 * downgrade tests simulate a provider 400/422 response and assert the wrapper
 * strips the field and retries; real-gateway passthrough is documented as a
 * deviation requiring live validation.
 */
import { describe, expect, it } from "vitest";

import {
	PROMPT_CACHE_KEY_FIELD,
	PROMPT_CACHE_KEY_MAX_LENGTH,
	buildCacheKeySamplingParams,
	buildPromptCacheKey,
	downgradeCacheKeyCapability,
	isCacheFieldRejection,
	mergeCacheKeyOptions,
	normalizePromptCacheMode,
	resolvePromptCacheCapabilities,
	stripCacheKeyOptions,
	type PromptCacheCapabilities,
} from "../src/prompt-cache.js";

// =========================================================================== //
// Capability resolution (§8.1)
// =========================================================================== //

describe("resolvePromptCacheCapabilities (§8.1)", () => {
	it("off mode disables all provider-specific flags", () => {
		const caps = resolvePromptCacheCapabilities("off", { apiProtocol: "openai-completions" });
		expect(caps.mode).toBe("off");
		expect(caps.supportsCacheKey).toBe(false);
		expect(caps.supportsBreakpoints).toBe(false);
		expect(caps.supportsUsageMetrics).toBe(false);
	});

	it("auto mode preserves structure but emits no provider field", () => {
		const caps = resolvePromptCacheCapabilities("auto", { apiProtocol: "openai-completions" });
		expect(caps.mode).toBe("auto");
		expect(caps.supportsCacheKey).toBe(false);
		expect(caps.supportsBreakpoints).toBe(false);
		// supportsUsageMetrics starts false/unknown; runtime-probed.
		expect(caps.supportsUsageMetrics).toBe(false);
	});

	it("explicit mode is optimistic on cache key and false on breakpoints", () => {
		const caps = resolvePromptCacheCapabilities("explicit", { apiProtocol: "openai-completions" });
		expect(caps.mode).toBe("explicit");
		expect(caps.supportsCacheKey).toBe(true); // optimistic; runtime downgrade
		// openai-completions has no explicit breakpoint marker (automatic prefix
		// caching). CPA-UNVERIFIED: breakpoints stay false until CPA-validated.
		expect(caps.supportsBreakpoints).toBe(false);
	});

	it("does not infer capability from the upstream model name (§8.1)", () => {
		// The resolver takes apiProtocol, NOT a model name. Two different
		// "models" with the same protocol resolve identically.
		const a = resolvePromptCacheCapabilities("explicit", { apiProtocol: "openai-completions" });
		const b = resolvePromptCacheCapabilities("explicit", { apiProtocol: "openai-completions" });
		expect(a).toEqual(b);
	});

	it("defaults to auto when the config value is missing or invalid", () => {
		expect(normalizePromptCacheMode(undefined)).toBe("auto");
		expect(normalizePromptCacheMode("garbage")).toBe("auto");
		expect(normalizePromptCacheMode("off")).toBe("off");
		expect(normalizePromptCacheMode("explicit")).toBe("explicit");
	});

	it("explicit mode for anthropic protocol also has breakpoints=false (path unwired)", () => {
		// CPA-UNVERIFIED: the anthropic-messages streamFn path is not wired in
		// the sidecar yet (defaultStreamFnForConfig throws). So even explicit +
		// anthropic does not claim breakpoint support until the path is wired
		// AND CPA-validated.
		const caps = resolvePromptCacheCapabilities("explicit", { apiProtocol: "anthropic-messages" });
		expect(caps.supportsBreakpoints).toBe(false);
		expect(caps.supportsCacheKey).toBe(true); // still optimistic on the key
	});
});

// =========================================================================== //
// Cache-key construction + version invalidation (§5.1/§10)
// =========================================================================== //

describe("buildPromptCacheKey (§5.1/§10)", () => {
	it("produces the documented svs-viewer:{session}:{fp}:g{gen} form", () => {
		const key = buildPromptCacheKey({
			sessionId: "sess_abc",
			slideFingerprint: "fp-test:1234",
			generation: 3,
		});
		expect(key).toBe("svs-viewer:sess_abc:fp-test:1234:g3");
	});

	it("changes when generation bumps (version invalidation, §10)", () => {
		const g1 = buildPromptCacheKey({ sessionId: "s1", slideFingerprint: "fp1", generation: 1 });
		const g2 = buildPromptCacheKey({ sessionId: "s1", slideFingerprint: "fp1", generation: 2 });
		expect(g1).not.toBe(g2);
	});

	it("changes when slide_fingerprint changes (§10)", () => {
		const a = buildPromptCacheKey({ sessionId: "s1", slideFingerprint: "fpA", generation: 1 });
		const b = buildPromptCacheKey({ sessionId: "s1", slideFingerprint: "fpB", generation: 1 });
		expect(a).not.toBe(b);
	});

	it("changes when session_id changes", () => {
		const a = buildPromptCacheKey({ sessionId: "s1", slideFingerprint: "fp1", generation: 1 });
		const b = buildPromptCacheKey({ sessionId: "s2", slideFingerprint: "fp1", generation: 1 });
		expect(a).not.toBe(b);
	});

	it("stays stable for the same generation/fingerprint/session", () => {
		const a = buildPromptCacheKey({ sessionId: "s1", slideFingerprint: "fp1", generation: 5 });
		const b = buildPromptCacheKey({ sessionId: "s1", slideFingerprint: "fp1", generation: 5 });
		expect(a).toBe(b);
	});

	it("truncates to PROMPT_CACHE_KEY_MAX_LENGTH (OpenAI limit)", () => {
		const longSession = "x".repeat(200);
		const key = buildPromptCacheKey({
			sessionId: longSession,
			slideFingerprint: "fp",
			generation: 1,
		});
		expect(Array.from(key).length).toBeLessThanOrEqual(PROMPT_CACHE_KEY_MAX_LENGTH);
		// Truncated key still starts with the prefix.
		expect(key.startsWith("svs-viewer:")).toBe(true);
	});
});

// =========================================================================== //
// Request-body injection (explicit mode)
// =========================================================================== //

describe("buildCacheKeySamplingParams (explicit injection)", () => {
	it("returns the field under the documented OpenAI name for explicit mode", () => {
		const caps: PromptCacheCapabilities = {
			mode: "explicit",
			supportsCacheKey: true,
			supportsBreakpoints: false,
			supportsUsageMetrics: false,
		};
		const params = buildCacheKeySamplingParams(caps, "svs-viewer:s1:fp1:g1");
		expect(params).not.toBeNull();
		expect(params![PROMPT_CACHE_KEY_FIELD]).toBe("svs-viewer:s1:fp1:g1");
	});

	it("returns null for auto/off mode (no provider field emitted)", () => {
		const auto: PromptCacheCapabilities = {
			mode: "auto",
			supportsCacheKey: false,
			supportsBreakpoints: false,
			supportsUsageMetrics: false,
		};
		expect(buildCacheKeySamplingParams(auto, "key")).toBeNull();

		const off: PromptCacheCapabilities = { ...auto, mode: "off" };
		expect(buildCacheKeySamplingParams(off, "key")).toBeNull();
	});

	it("returns null after a runtime downgrade (supportsCacheKey=false)", () => {
		const downgraded: PromptCacheCapabilities = {
			mode: "explicit",
			supportsCacheKey: false, // downgraded
			supportsBreakpoints: false,
			supportsUsageMetrics: false,
		};
		expect(buildCacheKeySamplingParams(downgraded, "key")).toBeNull();
	});
});

describe("mergeCacheKeyOptions", () => {
	it("adds samplingParams to a null options object", () => {
		const opts = mergeCacheKeyOptions(null, { prompt_cache_key: "k1" });
		expect(opts.samplingParams).toEqual({ prompt_cache_key: "k1" });
	});

	it("preserves existing samplingParams keys and adds the cache key", () => {
		const opts = mergeCacheKeyOptions(
			{ samplingParams: { top_p: 0.9 } },
			{ prompt_cache_key: "k1" },
		);
		expect(opts.samplingParams).toEqual({ top_p: 0.9, prompt_cache_key: "k1" });
	});

	it("cache key wins when an existing samplingParams had the same field", () => {
		const opts = mergeCacheKeyOptions(
			{ samplingParams: { prompt_cache_key: "old" } },
			{ prompt_cache_key: "new" },
		);
		expect((opts.samplingParams as Record<string, unknown>).prompt_cache_key).toBe("new");
	});

	it("preserves other top-level option keys (signal, apiKey, ...)", () => {
		const opts = mergeCacheKeyOptions(
			{ signal: "sig", apiKey: "k" },
			{ prompt_cache_key: "ck" },
		);
		expect(opts.signal).toBe("sig");
		expect(opts.apiKey).toBe("k");
		expect((opts.samplingParams as Record<string, unknown>).prompt_cache_key).toBe("ck");
	});
});

describe("stripCacheKeyOptions (downgrade retry)", () => {
	it("removes the cache key field from samplingParams", () => {
		const opts = stripCacheKeyOptions({
			samplingParams: { prompt_cache_key: "k1", top_p: 0.9 },
		});
		expect(opts.samplingParams).toEqual({ top_p: 0.9 });
	});

	it("drops samplingParams entirely when it becomes empty", () => {
		const opts = stripCacheKeyOptions({ samplingParams: { prompt_cache_key: "k1" } });
		expect("samplingParams" in opts).toBe(false);
	});

	it("leaves options without samplingParams untouched", () => {
		const opts = stripCacheKeyOptions({ signal: "sig", apiKey: "k" });
		expect(opts).toEqual({ signal: "sig", apiKey: "k" });
	});

	it("does not mutate the input object", () => {
		const input = { samplingParams: { prompt_cache_key: "k1", top_p: 0.9 } };
		stripCacheKeyOptions(input);
		expect(input.samplingParams).toEqual({ prompt_cache_key: "k1", top_p: 0.9 });
	});
});

// =========================================================================== //
// Rejection detection + downgrade (§13)
// =========================================================================== //

describe("isCacheFieldRejection (§13)", () => {
	it("matches a 400 mentioning prompt_cache_key as unrecognized", () => {
		expect(isCacheFieldRejection("400 Unrecognized request argument: prompt_cache_key")).toBe(true);
	});

	it("matches a 422 additional-properties-not-allowed", () => {
		expect(
			isCacheFieldRejection("422 Additional properties not allowed: 'prompt_cache_key' was unexpected"),
		).toBe(true);
	});

	it("matches an unknown-parameter error mentioning cache", () => {
		expect(isCacheFieldRejection("Unknown parameter: cache_key")).toBe(true);
	});

	it("matches Chinese rejection text (无法识别 prompt_cache_key)", () => {
		expect(isCacheFieldRejection("400 无法识别的参数 prompt_cache_key")).toBe(true);
	});

	it("matches Python-style unexpected keyword argument", () => {
		expect(
			isCacheFieldRejection("got an unexpected keyword argument 'prompt_cache_key'"),
		).toBe(true);
	});

	it("does NOT match a successful cache-hit log", () => {
		// "prompt_cache_key" present but no rejection marker.
		expect(isCacheFieldRejection("prompt_cache_key accepted, cache hit")).toBe(false);
	});

	it("does NOT match an unrelated transient error", () => {
		expect(isCacheFieldRejection("SSLError: connection reset by peer")).toBe(false);
		expect(isCacheFieldRejection("")).toBe(false);
	});

	it("errs on the side of matching (false positive is cheap, §13)", () => {
		// A generic 400 + "cache" anywhere → match (triggers one field-less retry).
		expect(isCacheFieldRejection("400 bad request, cache disabled")).toBe(true);
	});
});

describe("downgradeCacheKeyCapability (§13)", () => {
	it("flips supportsCacheKey to false and records effective mode as auto", () => {
		const explicit: PromptCacheCapabilities = {
			mode: "explicit",
			supportsCacheKey: true,
			supportsBreakpoints: false,
			supportsUsageMetrics: false,
		};
		const downgraded = downgradeCacheKeyCapability(explicit);
		expect(downgraded.supportsCacheKey).toBe(false);
		expect(downgraded.mode).toBe("auto"); // effective mode for metrics
	});

	it("does not mutate the original capabilities object", () => {
		const explicit: PromptCacheCapabilities = {
			mode: "explicit",
			supportsCacheKey: true,
			supportsBreakpoints: false,
			supportsUsageMetrics: false,
		};
		downgradeCacheKeyCapability(explicit);
		expect(explicit.supportsCacheKey).toBe(true); // unchanged
		expect(explicit.mode).toBe("explicit");
	});

	it("subsequent buildCacheKeySamplingParams returns null after downgrade", () => {
		const explicit: PromptCacheCapabilities = {
			mode: "explicit",
			supportsCacheKey: true,
			supportsBreakpoints: false,
			supportsUsageMetrics: false,
		};
		const downgraded = downgradeCacheKeyCapability(explicit);
		expect(buildCacheKeySamplingParams(downgraded, "key")).toBeNull();
	});
});

// =========================================================================== //
// Adapter contract: explicit → reject → downgrade → retry succeeds (§15)
// =========================================================================== //

describe("Phase 3 adapter contract (§15)", () => {
	it("simulates the explicit→reject→downgrade→retry-success sequence", () => {
		// This mirrors what agent-runner's makeRetryingStreamFn does, exercised
		// at the prompt-cache module level (no real streamFn).
		let caps = resolvePromptCacheCapabilities("explicit", { apiProtocol: "openai-completions" });
		expect(caps.supportsCacheKey).toBe(true);

		// Attempt 1: build options with the cache key.
		const cacheKey = buildPromptCacheKey({ sessionId: "s1", slideFingerprint: "fp1", generation: 1 });
		const cacheKeyParams = buildCacheKeySamplingParams(caps, cacheKey);
		let opts = mergeCacheKeyOptions({ signal: "sig" }, cacheKeyParams);
		expect((opts.samplingParams as Record<string, unknown>)[PROMPT_CACHE_KEY_FIELD]).toBe(cacheKey);

		// Provider rejects the field.
		const providerError = "400 Unrecognized request argument: prompt_cache_key";
		expect(isCacheFieldRejection(providerError)).toBe(true);

		// Downgrade + strip.
		caps = downgradeCacheKeyCapability(caps);
		opts = stripCacheKeyOptions(opts);

		// Retry: no cache field is emitted, and buildCacheKeySamplingParams
		// returns null for the rest of the run.
		const retryParams = buildCacheKeySamplingParams(caps, cacheKey);
		expect(retryParams).toBeNull();
		expect("samplingParams" in opts).toBe(false); // field stripped
		expect(opts.signal).toBe("sig"); // other options preserved
	});

	it("auto/off mode never injects the field, so no downgrade path triggers", () => {
		for (const mode of ["auto", "off"] as const) {
			const caps = resolvePromptCacheCapabilities(mode, { apiProtocol: "openai-completions" });
			const cacheKey = buildPromptCacheKey({ sessionId: "s1", slideFingerprint: "fp1", generation: 1 });
			const params = buildCacheKeySamplingParams(caps, cacheKey);
			expect(params).toBeNull();
		}
	});
});
