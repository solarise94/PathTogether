/**
 * Phase 4 metrics aggregation + report tests (Wave 1).
 *
 * Hand-computes a small 2-arm × 2-task fixture, checks the exact aggregates,
 * byte-identical render determinism for identical input, and the NO-GO banner
 * presence/absence.
 */
import { describe, expect, it } from "vitest";

import { aggregateReport, renderReport, type ReportRow } from "../experiments/src/report.js";
import type { RubricOutcome } from "../experiments/src/rubric.js";

/** Build a minimal RequestMetrics-compatible row. */
function row(args: {
	task_id: string;
	arm_id: string;
	step: 1 | 2;
	wall_ms: number;
	input: number;
	cached: number;
	cacheWrite: number;
	overviewBytes: number;
	workingBytes: number;
	overflow: number;
	cacheMode?: "off" | "auto" | "explicit";
}): ReportRow {
	return {
		task_id: args.task_id,
		arm_id: args.arm_id,
		step: args.step,
		wall_ms: args.wall_ms,
		session_id: "s",
		checkpoint_generation: 1,
		stable_prefix_hash_prefix: "p",
		prompt_cache_mode: args.cacheMode ?? "auto",
		input_tokens: args.input,
		cached_tokens: args.cached,
		cache_write_tokens: args.cacheWrite,
		selected_images: 0,
		materialized_images: 0,
		evicted_image_refs: [],
		image_lru_hits: 0,
		image_lru_misses: 0,
		overview_image_bytes_sent: args.overviewBytes,
		working_set_image_bytes_sent: args.workingBytes,
		prepared_request_bytes: 0,
		transform_ms: 0,
		region_fetch_ms: 0,
		compaction_reason: null,
		derivative_hash_mismatch: 0,
		checkpoint_rebuild_reason: null,
		visual_budget_overflow_tokens: args.overflow,
	};
}

function outcome(overall: RubricOutcome["overall"], fail = 0, manual = 0): RubricOutcome {
	return {
		overall,
		results: [
			{ assertionId: "x", type: "min_snapshot_count", pass: fail === 0, detail: "d" },
			...(manual > 0 ? [{ assertionId: "m", type: "manual" as const, pass: true, detail: "d" }] : []),
		],
	};
}

/** 2 arms (step 1) × 2 tasks × 2 requests each = 8 rows. */
function sampleRows(): ReportRow[] {
	return [
		row({ task_id: "t1", arm_id: "step1-overview-768", step: 1, wall_ms: 100, input: 1000, cached: 800, cacheWrite: 200, overviewBytes: 5000, workingBytes: 1000, overflow: 0 }),
		row({ task_id: "t1", arm_id: "step1-overview-768", step: 1, wall_ms: 200, input: 1200, cached: 1000, cacheWrite: 200, overviewBytes: 5000, workingBytes: 2000, overflow: 10 }),
		row({ task_id: "t2", arm_id: "step1-overview-768", step: 1, wall_ms: 150, input: 1100, cached: 900, cacheWrite: 200, overviewBytes: 5000, workingBytes: 1500, overflow: 0 }),
		row({ task_id: "t2", arm_id: "step1-overview-768", step: 1, wall_ms: 250, input: 1300, cached: 1100, cacheWrite: 200, overviewBytes: 5000, workingBytes: 2500, overflow: 0 }),
		row({ task_id: "t1", arm_id: "step1-overview-1024", step: 1, wall_ms: 110, input: 2000, cached: 1500, cacheWrite: 500, overviewBytes: 9000, workingBytes: 1100, overflow: 5 }),
		row({ task_id: "t1", arm_id: "step1-overview-1024", step: 1, wall_ms: 210, input: 2200, cached: 1700, cacheWrite: 500, overviewBytes: 9000, workingBytes: 2100, overflow: 0 }),
		row({ task_id: "t2", arm_id: "step1-overview-1024", step: 1, wall_ms: 160, input: 2100, cached: 1600, cacheWrite: 500, overviewBytes: 9000, workingBytes: 1600, overflow: 0 }),
		row({ task_id: "t2", arm_id: "step1-overview-1024", step: 1, wall_ms: 260, input: 2300, cached: 1800, cacheWrite: 500, overviewBytes: 9000, workingBytes: 2600, overflow: 0 }),
	];
}

describe("aggregateReport math", () => {
	it("computes exact per-arm totals/means for 2 arms × 2 tasks × 2 requests", () => {
		const data = aggregateReport(sampleRows());
		// 2 arms
		expect(data.armAggregates.length).toBe(2);
		const a768 = data.armAggregates.find((a) => a.arm_id === "step1-overview-768")!;
		const a1024 = data.armAggregates.find((a) => a.arm_id === "step1-overview-1024")!;
		// 768 arm: 4 requests
		expect(a768.request_count).toBe(4);
		// input 1000+1200+1100+1300 = 4600 ; mean 1150
		expect(a768.input_tokens_total).toBe(4600);
		expect(a768.input_tokens_mean).toBe(1150);
		// cached 800+1000+900+1100 = 3800
		expect(a768.cached_tokens_total).toBe(3800);
		// cacheWrite 200*4 = 800
		expect(a768.cache_write_tokens_total).toBe(800);
		// overview bytes 5000*4 = 20000 ; working 1000+2000+1500+2500 = 7000
		expect(a768.overview_image_bytes_total).toBe(20000);
		expect(a768.working_set_image_bytes_total).toBe(7000);
		// overflow 0+10+0+0 = 10
		expect(a768.visual_budget_overflow_tokens_total).toBe(10);
		// wall 100+200+150+250 = 700 ; mean 175
		expect(a768.wall_ms_total).toBe(700);
		expect(a768.wall_ms_mean).toBe(175);

		// 1024 arm: overview 9000*4 = 36000
		expect(a1024.overview_image_bytes_total).toBe(36000);
		expect(a1024.request_count).toBe(4);
		expect(a1024.input_tokens_total).toBe(8600);
		// overflow 5+0+0+0 = 5
		expect(a1024.visual_budget_overflow_tokens_total).toBe(5);
	});

	it("treats 'unknown' tokens as skipped (counted, not summed)", () => {
		const rows = sampleRows();
		// make two cached_tokens unknown in the 768 arm
		(rows[0]!.cached_tokens as unknown) = "unknown";
		(rows[1]!.cached_tokens as unknown) = "unknown";
		const data = aggregateReport(rows);
		const a768 = data.armAggregates.find((a) => a.arm_id === "step1-overview-768")!;
		// only the two numeric cached remain: 900+1100 = 2000
		expect(a768.cached_tokens_total).toBe(2000);
		expect(a768.cached_tokens_unknown_count).toBe(2);
	});

	it("joins rubric outcomes per arm and counts verdicts", () => {
		const data = aggregateReport(sampleRows(), [
			{ task_id: "t1", arm_id: "step1-overview-768", step: 1, outcome: outcome("PASS") },
			{ task_id: "t2", arm_id: "step1-overview-768", step: 1, outcome: outcome("FAIL", 1) },
			{ task_id: "t1", arm_id: "step1-overview-1024", step: 1, outcome: outcome("PENDING", 0, 1) },
			{ task_id: "t2", arm_id: "step1-overview-1024", step: 1, outcome: outcome("PASS") },
		]);
		const a768 = data.armAggregates.find((a) => a.arm_id === "step1-overview-768")!;
		const a1024 = data.armAggregates.find((a) => a.arm_id === "step1-overview-1024")!;
		expect(a768.rubric_total).toBe(2);
		expect(a768.rubric_pass).toBe(1);
		expect(a768.rubric_fail).toBe(1);
		expect(a1024.rubric_total).toBe(2);
		expect(a1024.rubric_pass).toBe(1);
		expect(a1024.rubric_pending).toBe(1);
		// per-task rubric table sorted
		expect(data.taskRubric.length).toBe(4);
		expect(data.taskRubric[0]!.arm_id).toBe("step1-overview-1024");
	});
});

describe("renderReport", () => {
	it("is byte-identical for identical input (deterministic, no timestamps)", () => {
		const data = aggregateReport(sampleRows());
		const r1 = renderReport(data);
		const r2 = renderReport(aggregateReport(sampleRows()));
		expect(r2).toBe(r1);
		// no Date.now() / timestamp leakage: body has no current-year-ish stamp
		// (the only header is the title line).
		expect(r1.startsWith("# Phase 4 A/B 报告\n")).toBe(true);
	});

	it("emits the NO-GO banner for unverified cache data (scripted rows: no protocol)", () => {
		const rows = sampleRows(); // default cacheMode "auto", no cpa_api_protocol
		const data = aggregateReport(rows);
		expect(data.hasUnverifiedCacheData).toBe(true);
		const md = renderReport(data);
		expect(md).toContain("NO-GO（缓存不可验证）");
		expect(md).toContain("prompt_cache_key");
	});

	it("omits the NO-GO banner when every row is prompt_cache_mode 'off'", () => {
		const rows = sampleRows().map((r) => ({ ...r, prompt_cache_mode: "off" as const }));
		const data = aggregateReport(rows);
		expect(data.hasUnverifiedCacheData).toBe(false);
		const md = renderReport(data);
		expect(md).not.toContain("缓存不可验证");
	});

	it("omits the NO-GO banner for VERIFIED openai-protocol real-model rows (explicit cache)", () => {
		// openai path prompt_cache_key passthrough verified 2026-08-12
		// (gpt-5.6-luna cached_tokens observed) → cache columns are a formal
		// conclusion; no banner.
		const rows = sampleRows().map((r) => ({ ...r, prompt_cache_mode: "explicit" as const, cpa_api_protocol: "openai" as const }));
		const data = aggregateReport(rows);
		expect(data.hasUnverifiedCacheData).toBe(false);
		const md = renderReport(data);
		expect(md).not.toContain("缓存不可验证");
	});

	it("emits the NO-GO banner for gemini-protocol rows even in explicit mode (#592 cache-unobservable)", () => {
		const rows = sampleRows().map((r) => ({ ...r, prompt_cache_mode: "explicit" as const, cpa_api_protocol: "gemini" as const }));
		const data = aggregateReport(rows);
		expect(data.hasUnverifiedCacheData).toBe(true);
		const md = renderReport(data);
		expect(md).toContain("NO-GO（缓存不可验证）");
		expect(md).toContain("#592");
	});

	it("reports 概览固定成本 and 临时工作区成本 as separate columns", () => {
		const md = renderReport(aggregateReport(sampleRows()));
		expect(md).toContain("概览字节∑");
		expect(md).toContain("工作区字节∑");
		expect(md).toContain("20,000"); // 768 arm overview total
	});
});
