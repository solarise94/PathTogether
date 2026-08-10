/**
 * Tests for {@link validateRunConfig} (§9.2/§11 run-boundary validation).
 *
 * The sidecar re-validates config at every run-start entry so a config that
 * bypassed the Flask PUT /api/ai/config validator (e.g. hand-edited
 * ai_config.json) cannot start a run with contradictory parameters.
 */
import { describe, expect, it } from "vitest";

import { validateRunConfig, ConfigError, type RunConfig } from "../src/agent-runner.js";

function baseConfig(over: Partial<RunConfig> = {}): RunConfig {
	return {
		base_url: "http://x/v1",
		api_key: "k",
		model: "m",
		max_tokens: 2048,
		context_window_tokens: 200000,
		reserve_tokens: 8000,
		keep_recent_tokens: 10000,
		...over,
	} as RunConfig;
}

describe("validateRunConfig", () => {
	it("accepts a well-formed config without throwing", () => {
		expect(() => validateRunConfig(baseConfig())).not.toThrow();
	});

	it("rejects reserve_tokens + keep_recent_tokens >= context_window_tokens", () => {
		expect(() =>
			validateRunConfig(baseConfig({ context_window_tokens: 10000, reserve_tokens: 6000, keep_recent_tokens: 5000 })),
		).toThrow(/context_window_tokens/);
		// equality (==) also violates strict <
		expect(() =>
			validateRunConfig(baseConfig({ context_window_tokens: 10000, reserve_tokens: 5000, keep_recent_tokens: 5000 })),
		).toThrow(/context_window_tokens/);
	});

	it("accepts reserve + keep < context_window", () => {
		expect(() =>
			validateRunConfig(baseConfig({ context_window_tokens: 10000, reserve_tokens: 4000, keep_recent_tokens: 5000 })),
		).not.toThrow();
	});

	it("rejects non-positive Phase 1 integer fields", () => {
		for (const field of [
			"visual_working_set_max",
			"visual_context_budget_tokens",
			"overview_long_edge",
			"working_image_long_edge",
			"detail_image_long_edge",
			"image_jpeg_quality",
			"region_materialize_concurrency",
			"image_derivative_cache_max_mb",
			"image_derivative_cache_ttl",
		] as const) {
			expect(() => validateRunConfig(baseConfig({ [field]: 0 } as Partial<RunConfig>))).toThrow(/正整数/);
			expect(() => validateRunConfig(baseConfig({ [field]: -1 } as Partial<RunConfig>))).toThrow(/正整数/);
		}
	});

	it("rejects long_edge fields above 4096", () => {
		expect(() => validateRunConfig(baseConfig({ overview_long_edge: 4097 }))).toThrow(/4096/);
		expect(() => validateRunConfig(baseConfig({ working_image_long_edge: 5000 }))).toThrow(/4096/);
		expect(() => validateRunConfig(baseConfig({ detail_image_long_edge: 4097 }))).toThrow(/4096/);
	});

	it("accepts long_edge fields at the 4096 boundary", () => {
		expect(() => validateRunConfig(baseConfig({ overview_long_edge: 4096 }))).not.toThrow();
	});

	it("rejects invalid prompt_cache_mode", () => {
		expect(() => validateRunConfig(baseConfig({ prompt_cache_mode: "bogus" }))).toThrow(/prompt_cache_mode/);
	});

	it("accepts valid prompt_cache_mode values", () => {
		for (const m of ["off", "auto", "explicit"] as const) {
			expect(() => validateRunConfig(baseConfig({ prompt_cache_mode: m }))).not.toThrow();
		}
	});

	it("does not validate absent fields (defaults applied later)", () => {
		// Omitting all Phase 1 fields is fine — they fall back to defaults.
		expect(() => validateRunConfig(baseConfig())).not.toThrow();
	});
});
