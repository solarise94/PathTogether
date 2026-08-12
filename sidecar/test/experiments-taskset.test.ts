/**
 * Phase 4 taskset loader/validator tests (Wave 1).
 *
 * Covers: valid committed taskset loads + validates against the manifest
 * fixture set; unknown category / bad fixture ref / malformed rubric assertion
 * each rejected with a useful located message.
 */
import { join } from "node:path";
import { describe, expect, it } from "vitest";

import {
	loadTaskset,
	TASK_CATEGORIES,
	TasksetValidationError,
	validateTaskset,
} from "../experiments/src/taskset.js";

const TASKSET_PATH = join(__dirname, "..", "experiments", "tasksets", "reading-v1.json");
const MANIFEST_FIXTURES = ["synth-dense", "synth-heterogeneous", "synth-sparse"];

function validTask(): Record<string, unknown> {
	return {
		id: "t-valid",
		category: "全片低倍候选区域定位",
		fixture_id: "synth-dense",
		user_turns: ["请定位候选区域。"],
		model_script: [
			{
				text: "去看一眼。",
				toolCalls: [{ id: "tc1", name: "goto", arguments: { x: 1, y: 2, level: 1 } }],
				stopReason: "toolUse",
			},
			{ text: "完成。", stopReason: "stop" },
		],
		rubric: [
			{ id: "a1", type: "tool_call_sequence", sequence: ["goto"] },
			{ id: "a2", type: "min_snapshot_count", min: 0 },
			{ id: "a3", type: "manual", criteria: "人工复核" },
		],
	};
}

function validTaskset(): Record<string, unknown> {
	return {
		taskset_id: "ts-test",
		schema_version: 1,
		manifest_version: 1,
		description: "test",
		tasks: [validTask()],
	};
}

describe("committed reading-v1 taskset", () => {
	it("loads and validates against the manifest fixture set", () => {
		const ts = loadTaskset(TASKSET_PATH, MANIFEST_FIXTURES);
		expect(ts.taskset_id).toBe("reading-v1");
		// All 7 §15.3 categories covered.
		const cats = new Set(ts.tasks.map((t) => t.category));
		for (const c of TASK_CATEGORIES) expect(cats.has(c)).toBe(true);
		// fixture refs all known.
		for (const t of ts.tasks) expect(MANIFEST_FIXTURES).toContain(t.fixture_id);
		// every task has at least one rubric assertion + a model_script.
		for (const t of ts.tasks) {
			expect(t.rubric.length).toBeGreaterThan(0);
			expect(t.model_script.length).toBeGreaterThan(0);
		}
	});

	it("has unique task ids", () => {
		const ts = loadTaskset(TASKSET_PATH, MANIFEST_FIXTURES);
		const ids = ts.tasks.map((t) => t.id);
		expect(new Set(ids).size).toBe(ids.length);
	});
});

describe("validateTaskset rejection cases", () => {
	it("rejects an unknown category with the canonical list in the message", () => {
		const ts = validTaskset();
		(ts.tasks as Array<Record<string, unknown>>)[0]!.category = "not a real category";
		expect(() => validateTaskset(ts)).toThrow(TasksetValidationError);
		try {
			validateTaskset(ts);
		} catch (e) {
			const msg = (e as Error).message;
			expect(msg).toContain("unknown category");
			expect(msg).toContain("全片低倍候选区域定位");
		}
	});

	it("rejects a bad fixture_id ref when the manifest fixture set is provided", () => {
		const ts = validTaskset();
		(ts.tasks as Array<Record<string, unknown>>)[0]!.fixture_id = "no-such-fixture";
		expect(() => validateTaskset(ts, MANIFEST_FIXTURES)).toThrow(/references unknown fixture_id "no-such-fixture"/);
	});

	it("rejects a malformed rubric assertion (bbox_revisit without region_labels)", () => {
		const ts = validTaskset();
		(ts.tasks as Array<Record<string, unknown>>)[0]!.rubric = [
			{ id: "bad", type: "bbox_revisit" },
		];
		expect(() => validateTaskset(ts)).toThrow(/region_labels must be a non-empty array/);
	});

	it("rejects a malformed rubric assertion (min_snapshot_count.min not a number)", () => {
		const ts = validTaskset();
		(ts.tasks as Array<Record<string, unknown>>)[0]!.rubric = [
			{ id: "bad", type: "min_snapshot_count", min: "two" },
		];
		expect(() => validateTaskset(ts)).toThrow(/min must be an integer >= 0/);
	});

	it("rejects duplicate task ids", () => {
		const ts = validTaskset();
		(ts.tasks as unknown[]).push(validTask()); // same id "t-valid"
		expect(() => validateTaskset(ts)).toThrow(/duplicate task id "t-valid"/);
	});

	it("rejects a scripted turn with a bad stopReason", () => {
		const ts = validTaskset();
		((ts.tasks as Array<Record<string, unknown>>)[0]!.model_script as unknown[])[0] = {
			text: "x",
			stopReason: "bogus",
		};
		expect(() => validateTaskset(ts)).toThrow(/stopReason must be one of/);
	});

	it("rejects a toolCall with non-object arguments", () => {
		const ts = validTaskset();
		((ts.tasks as Array<Record<string, unknown>>)[0]!.model_script as unknown[])[0] = {
			toolCalls: [{ id: "tc1", name: "goto", arguments: "not-an-object" }],
		};
		expect(() => validateTaskset(ts)).toThrow(/arguments must be an object/);
	});

	it("rejects a duplicate assertion id within one task", () => {
		const ts = validTaskset();
		(ts.tasks as Array<Record<string, unknown>>)[0]!.rubric = [
			{ id: "dup", type: "min_snapshot_count", min: 0 },
			{ id: "dup", type: "max_annotation_count", max: 1 },
		];
		expect(() => validateTaskset(ts)).toThrow(/duplicate assertion id "dup"/);
	});
});
