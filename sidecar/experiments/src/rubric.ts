/**
 * Phase 4 A/B framework (Wave 1: data plane) — rubric checker.
 *
 * Pure function: given a task's machine-checkable rubric assertions and a
 * recorded transcript, produce per-assertion {pass, detail} plus an overall
 * PASS / FAIL / PENDING verdict (FAIL dominates: any failed machine assertion →
 * FAIL; otherwise PENDING iff any `manual` assertion exists).
 *
 * The transcript entry shape is DEFINED here; Wave 2's execution runner is
 * responsible for producing it from the recorded session (it flattens pi
 * AgentMessage[] into role/text/toolCalls/bbox). Rubric assertions reference
 * the same tool names as sidecar/src/tools.ts (goto, snapshot,
 * mark_observation, create_annotation, complete_snapshot_review, finish).
 *
 * NOTE: experiment data plane only — NOT built into the shipped sidecar bundle.
 */
import type { RubricAssertion, ScriptedToolCall } from "./taskset.js";

// ------------------------------------------------------------------------- //
// Transcript shape (Wave 2 runner produces this)
// ------------------------------------------------------------------------- //

export interface Bbox {
	x: number;
	y: number;
	w: number;
	h: number;
}

/**
 * One flattened transcript entry. `bbox` carries a spatial extent when the
 * entry has one (e.g. a snapshot toolResult records the viewport bbox in
 * level-0 coords); `toolCallId` links a toolResult to the assistant toolCall it
 * answers, so bbox_revisit can attach a snapshot's viewport to its call.
 */
export interface RubricTranscriptEntry {
	role: "user" | "assistant" | "toolResult" | "system";
	text: string;
	/** Assistant entries: the tool calls emitted in this turn. */
	toolCalls?: ScriptedToolCall[];
	/** toolResult entries: the toolCall id this result answers. */
	toolCallId?: string;
	/** Spatial extent in level-0 px (snapshot viewport, observation bbox, ...). */
	bbox?: Bbox;
}

// ------------------------------------------------------------------------- //
// Outcome types
// ------------------------------------------------------------------------- //

export interface RubricAssertionResult {
	/** The id of the assertion this result refers to. */
	assertionId: string;
	/** The assertion type (echoed for reporting). */
	type: RubricAssertion["type"];
	/** Machine verdict. Manual assertions are always `true` (not failed) but the overall outcome is PENDING. */
	pass: boolean;
	/** Human-readable detail: what was observed vs expected. */
	detail: string;
}

export interface RubricOutcome {
	results: RubricAssertionResult[];
	/** PASS = all machine assertions pass and no manual assertion; FAIL = a machine assertion failed (dominates PENDING — a hard regression must never hide behind "awaiting human review"); PENDING = no machine failure but a manual assertion is present (review required). */
	overall: "PASS" | "FAIL" | "PENDING";
}

// ------------------------------------------------------------------------- //
// Helpers
// ------------------------------------------------------------------------- ...

/** Spatial probe derived from a tool call: either a real bbox or a point (goto center). */
interface Probe {
	toolName: string;
	bbox: Bbox;
	point: boolean;
}

function toNum(v: unknown): number | undefined {
	const n = Number(v);
	return Number.isFinite(n) ? n : undefined;
}

/** Derive a spatial probe from a tool call's arguments, if possible. */
function probeFromArgs(toolName: string, args: Record<string, unknown>): Probe | undefined {
	const x = toNum(args.x);
	const y = toNum(args.y);
	if (x === undefined || y === undefined) return undefined;
	if (toolName === "goto") {
		// goto args are a CENTER point; treat as a degenerate point bbox.
		return { toolName, bbox: { x, y, w: 0, h: 0 }, point: true };
	}
	const w = toNum(args.w);
	const h = toNum(args.h);
	if (w !== undefined && h !== undefined && w > 0 && h > 0) {
		return { toolName, bbox: { x, y, w, h }, point: false };
	}
	const side = toNum(args.side_px);
	if (side !== undefined && side > 0) {
		// create_annotation: x,y is the top-left, side_px the square edge.
		return { toolName, bbox: { x, y, w: side, h: side }, point: false };
	}
	return undefined;
}

/** Closed-interval bbox intersection. A point probe (w=h=0) intersects when it lies inside `region` (inclusive). */
function intersects(probe: Probe, region: Bbox): boolean {
	const { x, y, w, h } = probe.bbox;
	if (probe.point) {
		return x >= region.x && x <= region.x + region.w && y >= region.y && y <= region.y + region.h;
	}
	const noOverlap = x + w <= region.x || region.x + region.w <= x || y + h <= region.y || region.y + region.h <= y;
	return !noOverlap;
}

/**
 * Build the ordered list of tool-call names + spatial probes from a transcript.
 * snapshot calls pick up their viewport bbox from the matching toolResult.
 */
function collectCalls(transcript: RubricTranscriptEntry[]): {
	names: string[];
	probes: Probe[];
} {
	const names: string[] = [];
	const probes: Probe[] = [];
	// Index toolResult bboxes by toolCallId so snapshot viewports attach to calls.
	const resultBboxById = new Map<string, Bbox>();
	for (const e of transcript) {
		if (e.role === "toolResult" && e.bbox && e.toolCallId) {
			resultBboxById.set(e.toolCallId, e.bbox);
		}
	}
	for (const e of transcript) {
		if (!e.toolCalls) continue;
		for (const tc of e.toolCalls) {
			names.push(tc.name);
			let probe = probeFromArgs(tc.name, tc.arguments || {});
			// snapshot/mark_observation have no explicit bbox in args; attach the
			// viewport/result bbox recorded against this call id.
			if (!probe && tc.id && resultBboxById.has(tc.id)) {
				probe = { toolName: tc.name, bbox: resultBboxById.get(tc.id)!, point: false };
			}
			if (probe) probes.push(probe);
		}
	}
	return { names, probes };
}

/** Ordered-subsequence check: does `names` contain `seq` in order? */
function isSubsequence(names: string[], seq: string[]): boolean {
	let i = 0;
	for (const n of names) {
		if (n === seq[i]) i += 1;
		if (i === seq.length) return true;
	}
	return i === seq.length;
}

// ------------------------------------------------------------------------- //
// Main checker
// ------------------------------------------------------------------------- ...

/**
 * Evaluate a task's rubric against a recorded transcript.
 *
 * @param rubric the task's assertion list (from the taskset)
 * @param transcript the flattened recorded session
 * @param groundTruthRegions optional map of manifest region label → bbox, used
 *   by `bbox_revisit` assertions. Missing labels are reported in the detail.
 */
export function checkRubric(
	rubric: RubricAssertion[],
	transcript: RubricTranscriptEntry[],
	groundTruthRegions: Record<string, Bbox> = {},
): RubricOutcome {
	const { names, probes } = collectCalls(transcript);
	const results: RubricAssertionResult[] = [];
	let hasManual = false;
	let anyFail = false;

	for (const a of rubric) {
		switch (a.type) {
			case "manual": {
				hasManual = true;
				results.push({
					assertionId: a.id,
					type: a.type,
					pass: true,
					detail: `PENDING 人工复核：${a.criteria}`,
				});
				break;
			}
			case "tool_call_sequence": {
				const ok = isSubsequence(names, a.sequence);
				if (!ok) anyFail = true;
				results.push({
					assertionId: a.id,
					type: a.type,
					pass: ok,
					detail: ok
						? `工具调用序列包含有序子序列 [${a.sequence.join(", ")}]`
						: `工具调用序列 [${names.join(", ")}] 未包含有序子序列 [${a.sequence.join(", ")}]`,
				});
				break;
			}
			case "tool_call_forbidden": {
				const found = a.tools.filter((t) => names.includes(t));
				const ok = found.length === 0;
				if (!ok) anyFail = true;
				results.push({
					assertionId: a.id,
					type: a.type,
					pass: ok,
					detail: ok
						? `未出现禁止的工具 [${a.tools.join(", ")}]`
						: `出现了禁止的工具 [${found.join(", ")}]（预期均不出现）`,
				});
				break;
			}
			case "bbox_revisit": {
				const minHits = a.min_hits ?? 1;
				const tools = a.tools ?? ["goto", "snapshot"];
				const missingLabels = a.region_labels.filter((l) => !groundTruthRegions[l]);
				const regions = a.region_labels
					.filter((l) => groundTruthRegions[l])
					.map((l) => ({ label: l, bbox: groundTruthRegions[l]! }));
				const candidateProbes = probes.filter((p) => tools.includes(p.toolName));
				let hits = 0;
				const hitLabels = new Set<string>();
				for (const probe of candidateProbes) {
					for (const r of regions) {
						if (intersects(probe, r.bbox)) {
							hits += 1;
							hitLabels.add(r.label);
						}
					}
				}
				const ok = hits >= minHits && missingLabels.length === 0;
				if (!ok) anyFail = true;
				const parts: string[] = [];
				parts.push(
					ok
						? `命中 ${hits} 次空间回访（≥ ${minHits}）`
						: `仅命中 ${hits} 次空间回访（< ${minHits}）`,
				);
				parts.push(`命中区域 [${[...hitLabels].join(", ") || "无"}]`);
				if (missingLabels.length) parts.push(`manifest 缺失区域标签 [${missingLabels.join(", ")}]`);
				results.push({
					assertionId: a.id,
					type: a.type,
					pass: ok,
					detail: parts.join("；"),
				});
				break;
			}
			case "min_snapshot_count": {
				const count = names.filter((n) => n === "snapshot").length;
				const ok = count >= a.min;
				if (!ok) anyFail = true;
				results.push({
					assertionId: a.id,
					type: a.type,
					pass: ok,
					detail: ok ? `snapshot 调用 ${count} 次（≥ ${a.min}）` : `snapshot 仅调用 ${count} 次（< ${a.min}）`,
				});
				break;
			}
			case "max_annotation_count": {
				const count = names.filter((n) => n === "create_annotation").length;
				const ok = count <= a.max;
				if (!ok) anyFail = true;
				results.push({
					assertionId: a.id,
					type: a.type,
					pass: ok,
					detail: ok ? `create_annotation 调用 ${count} 次（≤ ${a.max}）` : `create_annotation 调用 ${count} 次（> ${a.max}）`,
				});
				break;
			}
			default: {
				// exhaustive
				const _exhaustive: never = a;
				void _exhaustive;
				break;
			}
		}
	}

	let overall: RubricOutcome["overall"];
	// FAIL dominates PENDING: a failed machine assertion is a hard regression
	// signal and must not be masked by the presence of a manual criterion.
	if (anyFail) overall = "FAIL";
	else if (hasManual) overall = "PENDING";
	else overall = "PASS";

	return { results, overall };
}
