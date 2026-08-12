/**
 * Phase 4 rubric checker tests (Wave 1).
 *
 * Covers each assertion type with a pass + fail case, and the PENDING verdict
 * when a manual assertion is present.
 */
import { describe, expect, it } from "vitest";

import { checkRubric, type RubricTranscriptEntry } from "../experiments/src/rubric.js";

const REGIONS = {
	high_density_cluster_A: { x: 800, y: 600, w: 600, h: 600 },
	high_density_cluster_B: { x: 2400, y: 1800, w: 500, h: 500 },
};

/** Build a transcript with one assistant turn carrying the given tool calls. */
function transcript(...calls: Array<{ id: string; name: string; arguments: Record<string, unknown> }>): RubricTranscriptEntry[] {
	return [{ role: "assistant", text: "", toolCalls: calls }];
}

function resultById(out: ReturnType<typeof checkRubric>, id: string) {
	return out.results.find((r) => r.assertionId === id)!;
}

describe("rubric: tool_call_sequence", () => {
	it("passes when the ordered subsequence is present", () => {
		const out = checkRubric(
			[{ id: "s", type: "tool_call_sequence", sequence: ["goto", "snapshot"] }],
			transcript(
				{ id: "1", name: "goto", arguments: { x: 1, y: 2, level: 1 } },
				{ id: "2", name: "snapshot", arguments: {} },
			),
		);
		expect(resultById(out, "s").pass).toBe(true);
		expect(out.overall).toBe("PASS");
	});

	it("fails when the order is wrong", () => {
		const out = checkRubric(
			[{ id: "s", type: "tool_call_sequence", sequence: ["goto", "snapshot"] }],
			transcript(
				{ id: "1", name: "snapshot", arguments: {} },
				{ id: "2", name: "goto", arguments: { x: 1, y: 2, level: 1 } },
			),
		);
		expect(resultById(out, "s").pass).toBe(false);
		expect(out.overall).toBe("FAIL");
	});

	it("subsequence allows intervening tools", () => {
		const out = checkRubric(
			[{ id: "s", type: "tool_call_sequence", sequence: ["goto", "snapshot"] }],
			transcript(
				{ id: "1", name: "goto", arguments: { x: 1, y: 2, level: 1 } },
				{ id: "2", name: "mark_observation", arguments: { label: "x" } },
				{ id: "3", name: "snapshot", arguments: {} },
			),
		);
		expect(resultById(out, "s").pass).toBe(true);
	});
});

describe("rubric: tool_call_forbidden", () => {
	it("passes when the forbidden tool is absent", () => {
		const out = checkRubric(
			[{ id: "f", type: "tool_call_forbidden", tools: ["create_annotation"] }],
			transcript({ id: "1", name: "snapshot", arguments: {} }),
		);
		expect(resultById(out, "f").pass).toBe(true);
	});

	it("fails when the forbidden tool appears", () => {
		const out = checkRubric(
			[{ id: "f", type: "tool_call_forbidden", tools: ["create_annotation"] }],
			transcript({ id: "1", name: "create_annotation", arguments: { label: "x", x: 1, y: 2, side_px: 10 } }),
		);
		expect(resultById(out, "f").pass).toBe(false);
		expect(out.overall).toBe("FAIL");
	});
});

describe("rubric: bbox_revisit", () => {
	it("passes when a goto center lands inside the named region", () => {
		// goto center (1100,900) is inside high_density_cluster_A (800..1400, 600..1200).
		const out = checkRubric(
			[{ id: "r", type: "bbox_revisit", region_labels: ["high_density_cluster_A"], tools: ["goto"], min_hits: 1 }],
			transcript({ id: "1", name: "goto", arguments: { x: 1100, y: 900, level: 1 } }),
			REGIONS,
		);
		expect(resultById(out, "r").pass).toBe(true);
	});

	it("fails when the goto center is outside all named regions", () => {
		const out = checkRubric(
			[{ id: "r", type: "bbox_revisit", region_labels: ["high_density_cluster_A"], tools: ["goto"], min_hits: 1 }],
			transcript({ id: "1", name: "goto", arguments: { x: 50, y: 50, level: 1 } }),
			REGIONS,
		);
		expect(resultById(out, "r").pass).toBe(false);
		expect(out.overall).toBe("FAIL");
	});

	it("counts min_hits across multiple regions", () => {
		// one goto in A, one goto in B → 2 hits, min_hits 2 passes.
		const out = checkRubric(
			[{ id: "r", type: "bbox_revisit", region_labels: ["high_density_cluster_A", "high_density_cluster_B"], min_hits: 2 }],
			transcript(
				{ id: "1", name: "goto", arguments: { x: 1100, y: 900, level: 1 } },
				{ id: "2", name: "goto", arguments: { x: 2600, y: 2000, level: 1 } },
			),
			REGIONS,
		);
		expect(resultById(out, "r").pass).toBe(true);
	});

	it("fails when a referenced region label is missing from the manifest map", () => {
		const out = checkRubric(
			[{ id: "r", type: "bbox_revisit", region_labels: ["does_not_exist"], min_hits: 1 }],
			transcript({ id: "1", name: "goto", arguments: { x: 1100, y: 900, level: 1 } }),
			REGIONS,
		);
		expect(resultById(out, "r").pass).toBe(false);
		expect(resultById(out, "r").detail).toContain("缺失区域标签");
	});

	it("attaches a snapshot viewport bbox from the matching toolResult", () => {
		// snapshot has no spatial args; its bbox comes from the toolResult entry.
		const t: RubricTranscriptEntry[] = [
			{ role: "assistant", text: "", toolCalls: [{ id: "snap1", name: "snapshot", arguments: {} }] },
			{ role: "toolResult", text: "", toolCallId: "snap1", bbox: { x: 1000, y: 850, w: 200, h: 200 } },
		];
		const out = checkRubric(
			[{ id: "r", type: "bbox_revisit", region_labels: ["high_density_cluster_A"], tools: ["snapshot"], min_hits: 1 }],
			t,
			REGIONS,
		);
		expect(resultById(out, "r").pass).toBe(true);
	});
});

describe("rubric: numeric bounds", () => {
	it("min_snapshot_count passes/fails on the threshold", () => {
		const pass = checkRubric([{ id: "m", type: "min_snapshot_count", min: 2 }], transcript(
			{ id: "1", name: "snapshot", arguments: {} },
			{ id: "2", name: "snapshot", arguments: {} },
		));
		expect(resultById(pass, "m").pass).toBe(true);
		const fail = checkRubric([{ id: "m", type: "min_snapshot_count", min: 2 }], transcript(
			{ id: "1", name: "snapshot", arguments: {} },
		));
		expect(resultById(fail, "m").pass).toBe(false);
	});

	it("max_annotation_count passes/fails on the threshold", () => {
		const pass = checkRubric([{ id: "x", type: "max_annotation_count", max: 1 }], transcript(
			{ id: "1", name: "create_annotation", arguments: { label: "a", x: 1, y: 2, side_px: 5 } },
		));
		expect(resultById(pass, "x").pass).toBe(true);
		const fail = checkRubric([{ id: "x", type: "max_annotation_count", max: 0 }], transcript(
			{ id: "1", name: "create_annotation", arguments: { label: "a", x: 1, y: 2, side_px: 5 } },
		));
		expect(resultById(fail, "x").pass).toBe(false);
	});
});

describe("rubric: manual + overall verdict", () => {
	it("a manual assertion forces PENDING even when all machine assertions pass", () => {
		const out = checkRubric(
			[
				{ id: "m", type: "min_snapshot_count", min: 1 },
				{ id: "h", type: "manual", criteria: "人工复核细节" },
			],
			transcript({ id: "1", name: "snapshot", arguments: {} }),
		);
		// manual assertion itself is not failed...
		expect(resultById(out, "h").pass).toBe(true);
		expect(resultById(out, "h").detail).toContain("PENDING 人工复核");
		// ...but the overall verdict is PENDING.
		expect(out.overall).toBe("PENDING");
	});

	it("a failing machine assertion + manual → overall is FAIL (hard regression dominates PENDING)", () => {
		// Review decision: FAIL must dominate PENDING — a failed machine
		// assertion is a hard regression signal and must not be masked by a
		// pending human review. The manual criterion is still reported PENDING
		// in the per-assertion results.
		const out = checkRubric(
			[
				{ id: "m", type: "min_snapshot_count", min: 5 }, // fails
				{ id: "h", type: "manual", criteria: "review" },
			],
			transcript({ id: "1", name: "snapshot", arguments: {} }),
		);
		expect(out.overall).toBe("FAIL");
		expect(resultById(out, "m").pass).toBe(false);
		expect(resultById(out, "h").pass).toBe(true); // manual itself stays non-failing
	});
});
