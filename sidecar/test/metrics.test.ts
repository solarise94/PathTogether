/**
 * Phase 2b tests: structured metrics sink (§12).
 *
 * Verifies the metrics record carries the §12 fields, marks provider-dependent
 * fields as "unknown" when no usage is returned, and never includes image
 * content or API keys.
 */
import { describe, expect, it } from "vitest";

import { buildRequestMetrics, defaultMetricsSink, type RequestMetrics } from "../src/metrics.js";

describe("buildRequestMetrics (§12)", () => {
	it("fills provider-dependent fields with 'unknown' when usage is absent", () => {
		const m = buildRequestMetrics({
			session_id: "sess1",
			checkpoint_generation: 2,
			stable_prefix_hash: "abcdef0123456789",
			prompt_cache_mode: "auto",
			transform_ms: 10,
			region_fetch_ms: 5,
			selected_images: 3,
			materialized_images: 3,
			evicted_image_refs: ["ref1"],
			image_lru_hits: 2,
			image_lru_misses: 1,
			overview_image_bytes_sent: 1024,
			working_set_image_bytes_sent: 2048,
			prepared_request_bytes: 4096,
		});
		expect(m.input_tokens).toBe("unknown");
		expect(m.cached_tokens).toBe("unknown");
		expect(m.cache_write_tokens).toBe("unknown");
	});

	it("uses real usage numbers when the provider returns them", () => {
		const m = buildRequestMetrics({
			session_id: "sess1",
			checkpoint_generation: 2,
			stable_prefix_hash: "abcdef0123456789",
			prompt_cache_mode: "explicit",
			transform_ms: 10,
			region_fetch_ms: 5,
			selected_images: 0,
			materialized_images: 0,
			evicted_image_refs: [],
			image_lru_hits: 0,
			image_lru_misses: 0,
			overview_image_bytes_sent: 0,
			working_set_image_bytes_sent: 0,
			prepared_request_bytes: 0,
			usage: { input: 1000, cacheRead: 800, cacheWrite: 200 },
		});
		expect(m.input_tokens).toBe(1000);
		expect(m.cached_tokens).toBe(800);
		expect(m.cache_write_tokens).toBe(200);
	});

	it("truncates the stable_prefix_hash to a 16-char prefix", () => {
		const long = "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef";
		const m = buildRequestMetrics({
			session_id: "s",
			checkpoint_generation: 1,
			stable_prefix_hash: long,
			prompt_cache_mode: "auto",
			transform_ms: 0,
			region_fetch_ms: 0,
			selected_images: 0,
			materialized_images: 0,
			evicted_image_refs: [],
			image_lru_hits: 0,
			image_lru_misses: 0,
			overview_image_bytes_sent: 0,
			working_set_image_bytes_sent: 0,
			prepared_request_bytes: 0,
		});
		expect(m.stable_prefix_hash_prefix).toBe("0123456789abcdef");
	});

	it("does NOT include image content or API keys in the record", () => {
		const m = buildRequestMetrics({
			session_id: "s",
			checkpoint_generation: 1,
			stable_prefix_hash: "x",
			prompt_cache_mode: "auto",
			transform_ms: 0,
			region_fetch_ms: 0,
			selected_images: 1,
			materialized_images: 1,
			evicted_image_refs: [],
			image_lru_hits: 0,
			image_lru_misses: 0,
			overview_image_bytes_sent: 100,
			working_set_image_bytes_sent: 0,
			prepared_request_bytes: 0,
		});
		const serialized = JSON.stringify(m);
		// No base64 payloads, no API keys.
		expect(serialized).not.toMatch(/api[_-]?key/i);
		expect(serialized).not.toMatch(/data:image/);
		expect(serialized).not.toMatch(/[A-Za-z0-9+/]{100,}/); // no long base64 blobs
	});
});

describe("defaultMetricsSink (§12)", () => {
	it("emits a single JSON line tagged [ai-metrics] without throwing", () => {
		const original = console.info;
		const lines: string[] = [];
		console.info = (s: string) => { lines.push(s); };
		try {
			const m: RequestMetrics = {
				session_id: "s1",
				checkpoint_generation: 1,
				stable_prefix_hash_prefix: "abc",
				prompt_cache_mode: "auto",
				input_tokens: 10,
				cached_tokens: 0,
				cache_write_tokens: 0,
				selected_images: 0,
				materialized_images: 0,
				evicted_image_refs: [],
				image_lru_hits: 0,
				image_lru_misses: 0,
				overview_image_bytes_sent: 0,
				working_set_image_bytes_sent: 0,
				prepared_request_bytes: 0,
				transform_ms: 1,
				region_fetch_ms: 0,
				compaction_reason: null,
				derivative_hash_mismatch: 0,
				checkpoint_rebuild_reason: null,
				visual_budget_overflow_tokens: 0,
			};
			defaultMetricsSink(m);
			expect(lines.length).toBe(1);
			expect(lines[0]).toContain("[ai-metrics]");
			expect(lines[0]).toContain("\"session_id\":\"s1\"");
		} finally {
			console.info = original;
		}
	});
});
