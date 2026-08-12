/**
 * Phase 4 A/B framework (Wave 1: data plane) — metrics aggregation + report.
 *
 * Consumes per-(task, arm, request) metrics rows (JSONL) + rubric outcomes,
 * aggregates per arm (token totals/means, the §16.2 概览固定成本 vs 临时工作区
 * 成本 byte split, visual-budget overflow, request count, wall-clock, rubric
 * pass rate), and renders a deterministic markdown comparison report.
 *
 * NO-GO gate (§14 Phase 3): whenever any row carries `prompt_cache_mode !=
 * "off"`, the report emits a prominent banner stating cache-hit numbers are NOT
 * a formal conclusion until `prompt_cache_key` passthrough is verified against
 * the real CPA gateway. The boolean gate itself lives in gate.ts.
 *
 * Determinism: identical input → byte-identical output. No timestamps anywhere
 * in the body; the runner names the output directory (run id lives there only).
 *
 * NOTE: experiment data plane only — NOT built into the shipped sidecar bundle.
 */
import type { RequestMetrics } from "../../src/metrics.js";
import type { RubricOutcome } from "./rubric.js";

// ------------------------------------------------------------------------- //
// Input row types
// ------------------------------------------------------------------------- //

/**
 * One flat JSONL row: a {@link RequestMetrics} record plus the experiment
 * context the runner attaches (task / arm / step / per-request wall clock).
 * `wall_ms` is the end-to-end request wall time (recorded but only meaningful
 * in real-model mode; scripted mode measures the fake streamFn, not the model).
 */
export interface ReportRow extends RequestMetrics {
	task_id: string;
	arm_id: string;
	step: 1 | 2;
	wall_ms: number;
	/**
	 * CPA api protocol the row was collected under (real-model runs only;
	 * scripted rows omit it). Cache-hit numbers are only a formal conclusion
	 * for openai-protocol rows (prompt_cache_key passthrough verified
	 * 2026-08-12); gemini-protocol rows are cache-unobservable (CPA #592).
	 */
	cpa_api_protocol?: "openai" | "anthropic" | "gemini";
}

/** One (task, arm) rubric outcome, pre-flattened for the per-task table. */
export interface TaskRubricRow {
	task_id: string;
	arm_id: string;
	step: 1 | 2;
	overall: RubricOutcome["overall"];
	pass_count: number;
	fail_count: number;
	pending_count: number;
}

// ------------------------------------------------------------------------- //
// Aggregate types
// ------------------------------------------------------------------------- //

export interface ArmAggregate {
	arm_id: string;
	step: 1 | 2;
	request_count: number;
	input_tokens_total: number;
	input_tokens_mean: number;
	input_tokens_unknown_count: number;
	cached_tokens_total: number;
	cached_tokens_unknown_count: number;
	cache_write_tokens_total: number;
	cache_write_tokens_unknown_count: number;
	/** 概览固定成本 (§16.2): overview image bytes sent, total. */
	overview_image_bytes_total: number;
	/** 临时工作区成本 (§16.2): working-set image bytes sent, total. */
	working_set_image_bytes_total: number;
	visual_budget_overflow_tokens_total: number;
	wall_ms_total: number;
	wall_ms_mean: number;
	/** Rubric counts across tasks for this arm. */
	rubric_total: number;
	rubric_pass: number;
	rubric_fail: number;
	rubric_pending: number;
}

export interface ReportData {
	/** Per-arm aggregates, sorted by (step, arm_id) for determinism. */
	armAggregates: ArmAggregate[];
	/** Per-(task, arm) rubric rows, sorted by (step, arm_id, task_id). */
	taskRubric: TaskRubricRow[];
	/**
	 * True iff any row carries a cache claim that is NOT verified for the
	 * protocol it was collected under → the NO-GO banner shows. Rules:
	 *   - openai-protocol real-model rows: VERIFIED (gpt-5.6-luna
	 *     prompt_cache_key passthrough + cached_tokens observed 2026-08-12) →
	 *     no banner.
	 *   - gemini-protocol rows: CPA antigravity #592 → cache hits unobservable →
	 *     banner.
	 *   - scripted rows (no cpa_api_protocol): mechanism validation only →
	 *     banner (historical behavior preserved).
	 */
	hasUnverifiedCacheData: boolean;
}

// ------------------------------------------------------------------------- //
// Aggregation
// ------------------------------------------------------------------------- ...

/** Sum numeric token values, treating "unknown" as skipped (counted separately). */
function sumTokens(values: Array<number | "unknown">): { total: number; unknown: number; count: number } {
	let total = 0;
	let unknown = 0;
	let count = 0;
	for (const v of values) {
		if (v === "unknown") unknown += 1;
		else {
			total += v;
			count += 1;
		}
	}
	return { total, unknown, count };
}

function mean(total: number, count: number): number {
	return count > 0 ? total / count : 0;
}

/**
 * Aggregate per-(task, arm, request) rows + rubric outcomes into a
 * {@link ReportData}. Deterministic: outputs are sorted, no timestamps.
 */
export function aggregateReport(rows: ReportRow[], rubricOutcomes: Array<{ task_id: string; arm_id: string; step: 1 | 2; outcome: RubricOutcome }> = []): ReportData {
	// Group rows by arm_id.
	const byArm = new Map<string, ReportRow[]>();
	let hasUnverifiedCacheData = false;
	for (const r of rows) {
		if (r.prompt_cache_mode !== "off") {
			// Cache claim is verified ONLY for openai-protocol real-model rows.
			if (r.cpa_api_protocol !== "openai") hasUnverifiedCacheData = true;
		}
		const list = byArm.get(r.arm_id);
		if (list) list.push(r);
		else byArm.set(r.arm_id, [r]);
	}

	// Group rubric outcomes by arm_id for the per-arm rubric counts.
	const rubricByArm = new Map<string, TaskRubricRow[]>();
	for (const { task_id, arm_id, step, outcome } of rubricOutcomes) {
		const pass_count = outcome.results.filter((x) => x.pass).length;
		const fail_count = outcome.results.filter((x) => !x.pass).length;
		const pending_count = outcome.results.filter((x) => x.type === "manual").length;
		const row: TaskRubricRow = { task_id, arm_id, step, overall: outcome.overall, pass_count, fail_count, pending_count };
		const list = rubricByArm.get(arm_id);
		if (list) list.push(row);
		else rubricByArm.set(arm_id, [row]);
	}

	const armAggregates: ArmAggregate[] = [];
	for (const [arm_id, armRows] of byArm) {
		const step = armRows[0]!.step;
		const input = sumTokens(armRows.map((r) => r.input_tokens));
		const cached = sumTokens(armRows.map((r) => r.cached_tokens));
		const cacheWrite = sumTokens(armRows.map((r) => r.cache_write_tokens));
		const overviewBytes = armRows.reduce((s, r) => s + (r.overview_image_bytes_sent || 0), 0);
		const workingBytes = armRows.reduce((s, r) => s + (r.working_set_image_bytes_sent || 0), 0);
		const overflow = armRows.reduce((s, r) => s + (r.visual_budget_overflow_tokens || 0), 0);
		const wallTotal = armRows.reduce((s, r) => s + (r.wall_ms || 0), 0);

		const rubricRows = rubricByArm.get(arm_id) || [];
		const rubric_total = rubricRows.length;
		const rubric_pass = rubricRows.filter((r) => r.overall === "PASS").length;
		const rubric_fail = rubricRows.filter((r) => r.overall === "FAIL").length;
		const rubric_pending = rubricRows.filter((r) => r.overall === "PENDING").length;

		armAggregates.push({
			arm_id,
			step,
			request_count: armRows.length,
			input_tokens_total: input.total,
			input_tokens_mean: mean(input.total, input.count),
			input_tokens_unknown_count: input.unknown,
			cached_tokens_total: cached.total,
			cached_tokens_unknown_count: cached.unknown,
			cache_write_tokens_total: cacheWrite.total,
			cache_write_tokens_unknown_count: cacheWrite.unknown,
			overview_image_bytes_total: overviewBytes,
			working_set_image_bytes_total: workingBytes,
			visual_budget_overflow_tokens_total: overflow,
			wall_ms_total: wallTotal,
			wall_ms_mean: mean(wallTotal, armRows.length),
			rubric_total,
			rubric_pass,
			rubric_fail,
			rubric_pending,
		});
	}

	armAggregates.sort((a, b) => a.step - b.step || a.arm_id.localeCompare(b.arm_id));

	const taskRubric: TaskRubricRow[] = [];
	for (const rows2 of rubricByArm.values()) taskRubric.push(...rows2);
	taskRubric.sort((a, b) => a.step - b.step || a.arm_id.localeCompare(b.arm_id) || a.task_id.localeCompare(b.task_id));

	return { armAggregates, taskRubric, hasUnverifiedCacheData };
}

// ------------------------------------------------------------------------- //
// Rendering (deterministic markdown)
// ------------------------------------------------------------------------- ...

function fmt(n: number): string {
	return Number.isFinite(n) ? n.toLocaleString("en-US") : String(n);
}

/** Render the aggregated report as deterministic markdown. */
export function renderReport(data: ReportData): string {
	const lines: string[] = [];
	lines.push("# Phase 4 A/B 报告");
	lines.push("");

	if (data.hasUnverifiedCacheData) {
		lines.push("> **NO-GO（缓存不可验证）**：本报告包含缓存声明未经验证的数据（gemini 协议路径在 CPA 网关上不报告缓存命中，CPA antigravity #592；scripted 数据为机制验证）。");
		lines.push("> openai 协议路径的 `prompt_cache_key` 透传已在真实 CPA 网关验证（gpt-5.6-luna cached_tokens 观测 2026-08-12），该路径数据不受此横幅限制。");
		lines.push("");
	}

	// Group arm aggregates by step.
	for (const step of [1, 2] as const) {
		const arms = data.armAggregates.filter((a) => a.step === step);
		if (arms.length === 0) continue;
		lines.push(`## Step ${step}：${step === 1 ? "图像策略对比（固定 context window）" : "Context window 对比（固定图像策略）"}`);
		lines.push("");
		const header = [
			"| arm | reqs | input∑ | inputμ | cache_read∑ | cache_write∑ | 概览字节∑ | 工作区字节∑ | overflow∑ | wallμ(ms) | rubric pass |",
			"| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
		];
		lines.push(...header);
		for (const a of arms) {
			const rubric = a.rubric_total > 0 ? `${a.rubric_pass}/${a.rubric_total}` : "-";
			const cacheReadCol = a.cached_tokens_unknown_count > 0 && a.cached_tokens_total === 0 ? "unknown" : fmt(a.cached_tokens_total);
			const cacheWriteCol = a.cache_write_tokens_unknown_count > 0 && a.cache_write_tokens_total === 0 ? "unknown" : fmt(a.cache_write_tokens_total);
			lines.push(
				`| ${a.arm_id} | ${a.request_count} | ${fmt(a.input_tokens_total)} | ${fmt(Math.round(a.input_tokens_mean))} | ${cacheReadCol} | ${cacheWriteCol} | ${fmt(a.overview_image_bytes_total)} | ${fmt(a.working_set_image_bytes_total)} | ${fmt(a.visual_budget_overflow_tokens_total)} | ${fmt(Math.round(a.wall_ms_mean))} | ${rubric} |`,
			);
		}
		if (arms.some((a) => a.input_tokens_unknown_count > 0)) {
			lines.push("");
			lines.push("> 部分行 input_tokens 含 `unknown`（Provider 未返回 usage）；均值仅按已知值计算。");
		}
		lines.push("");
		lines.push("> wallμ(ms) 仅为每轮 wall-clock 均值；在 scripted 模式下度量的是 fake streamFn，**仅在 real-model 模式下才有意义**。");
		lines.push("");
	}

	// Per-task rubric table.
	if (data.taskRubric.length > 0) {
		lines.push("## 每任务 rubric");
		lines.push("");
		lines.push("| step | arm | task | verdict | pass | fail | pending(manual) |");
		lines.push("| ---: | --- | --- | --- | ---: | ---: | ---: |");
		for (const r of data.taskRubric) {
			lines.push(`| ${r.step} | ${r.arm_id} | ${r.task_id} | ${r.overall} | ${r.pass_count} | ${r.fail_count} | ${r.pending_count} |`);
		}
		lines.push("");
		lines.push("> `PENDING` 表示存在待人工复核的 manual 断言（非失败）；`FAIL` 表示至少一条机器断言未通过。");
		lines.push("");
	}

	return lines.join("\n");
}
