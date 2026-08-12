/**
 * Phase 4 A/B framework (Wave 1: data plane) — taskset loader + validator.
 *
 * Loads and validates a §15.3 quality-regression task set (JSON) against the
 * known category list + manifest fixture ids + a tight rubric-assertion shape.
 * Hand-rolled validation (no ajv runtime dep): produces one clear, located
 * error per problem so a malformed taskset fails loudly before any run.
 *
 * The ScriptedTurn shape mirrors sidecar/test/helpers.ts so Wave 2's runner can
 * feed model_script directly into makeFakeStreamFn for mechanism-validation
 * mode.
 *
 * NOTE: this module is part of the experiment data plane only — it is NOT built
 * into the shipped sidecar bundle (tsconfig.build.json scopes rootDir to src).
 */
import { readFileSync } from "node:fs";

// ------------------------------------------------------------------------- //
// Known categories (§15.3) — the canonical Chinese labels.
// ------------------------------------------------------------------------- //

/** The 7 §15.3 quality-regression task categories, in canonical order. */
export const TASK_CATEGORIES = [
	"全片低倍候选区域定位",
	"从概览逐级goto至高倍",
	"细胞级形态需要重新抓取高倍图",
	"多区域比较",
	"长会话继续和compaction后继续",
	"历史observation坐标回访",
	"无值得标注区域的完整扫读",
] as const;

export type TaskCategory = (typeof TASK_CATEGORIES)[number];

// ------------------------------------------------------------------------- //
// ScriptedTurn (mirrors sidecar/test/helpers.ts ScriptedTurn)
// ------------------------------------------------------------------------- //

export interface ScriptedToolCall {
	id: string;
	name: string;
	arguments: Record<string, unknown>;
}

export interface ScriptedTurn {
	text?: string;
	toolCalls?: ScriptedToolCall[];
	stopReason?: "stop" | "length" | "toolUse";
}

// ------------------------------------------------------------------------- //
// Rubric assertions
// ------------------------------------------------------------------------- //

export type RubricAssertion =
	| { id: string; type: "tool_call_sequence"; sequence: string[] }
	| { id: string; type: "tool_call_forbidden"; tools: string[] }
	| {
			id: string;
			type: "bbox_revisit";
			region_labels: string[];
			tools?: string[];
			min_hits?: number;
	  }
	| { id: string; type: "min_snapshot_count"; min: number }
	| { id: string; type: "max_annotation_count"; max: number }
	| { id: string; type: "manual"; criteria: string };

const RUBRIC_TYPES = new Set<RubricAssertion["type"]>([
	"tool_call_sequence",
	"tool_call_forbidden",
	"bbox_revisit",
	"min_snapshot_count",
	"max_annotation_count",
	"manual",
]);

// ------------------------------------------------------------------------- //
// Taskset types
// ------------------------------------------------------------------------- //

export interface Task {
	id: string;
	category: TaskCategory;
	fixture_id: string;
	user_turns: string[];
	model_script: ScriptedTurn[];
	rubric: RubricAssertion[];
}

export interface Taskset {
	taskset_id: string;
	schema_version: 1;
	manifest_version: 1;
	description?: string;
	tasks: Task[];
}

// ------------------------------------------------------------------------- //
// Validation error
// ------------------------------------------------------------------------- //

/** One located validation problem (task id + json path + message). */
export interface TasksetError {
	task?: string;
	path: string;
	message: string;
}

export class TasksetValidationError extends Error {
	readonly errors: TasksetError[];
	constructor(errors: TasksetError[]) {
		super(`taskset validation failed:\n${errors.map((e) => `  - [${e.path}] ${e.message}`).join("\n")}`);
		this.name = "TasksetValidationError";
		this.errors = errors;
	}
}

// ------------------------------------------------------------------------- //
// Validator
// ------------------------------------------------------------------------- //

function isStr(v: unknown): v is string {
	return typeof v === "string";
}
function isStrArray(v: unknown): v is string[] {
	return Array.isArray(v) && v.every(isStr);
}

/**
 * Validate a parsed taskset object. `knownFixtureIds` is the set of fixture ids
 * declared by the versioned manifest; when provided, fixture_id refs are
 * checked against it. Returns the typed Taskset or throws
 * {@link TasksetValidationError} with every located problem.
 */
export function validateTaskset(obj: unknown, knownFixtureIds?: Iterable<string>): Taskset {
	const errors: TasksetError[] = [];
	const fixtureSet = knownFixtureIds ? new Set(knownFixtureIds) : undefined;

	if (!obj || typeof obj !== "object") {
		throw new TasksetValidationError([{ path: "$", message: "taskset must be an object" }]);
	}
	const root = obj as Record<string, unknown>;

	for (const key of ["taskset_id", "schema_version", "manifest_version", "tasks"]) {
		if (!(key in root)) errors.push({ path: "$", message: `missing required field "${key}"` });
	}
	if (!isStr(root.taskset_id)) errors.push({ path: "$.taskset_id", message: "must be a non-empty string" });
	if (root.schema_version !== 1) errors.push({ path: "$.schema_version", message: 'must be the number 1' });
	if (root.manifest_version !== 1) errors.push({ path: "$.manifest_version", message: 'must be the number 1' });

	const tasks = root.tasks;
	if (!Array.isArray(tasks) || tasks.length === 0) {
		errors.push({ path: "$.tasks", message: "must be a non-empty array" });
		if (errors.length) throw new TasksetValidationError(errors);
	}

	const seenTaskIds = new Set<string>();
	const categorySet = new Set<string>(TASK_CATEGORIES);

	(tasks as unknown[]).forEach((task, i) => {
		const tpath = `$.tasks[${i}]`;
		if (!task || typeof task !== "object") {
			errors.push({ path: tpath, message: "task must be an object" });
			return;
		}
		const t = task as Record<string, unknown>;
		const tid = isStr(t.id) ? t.id : undefined;

		for (const key of ["id", "category", "fixture_id", "user_turns", "model_script", "rubric"]) {
			if (!(key in t)) errors.push({ path: `${tpath}`, message: `missing required field "${key}"` });
		}
		if (!isStr(t.id)) {
			errors.push({ path: `${tpath}.id`, message: "must be a non-empty string" });
		} else if (seenTaskIds.has(t.id)) {
			errors.push({ path: `${tpath}.id`, message: `duplicate task id "${t.id}"` });
		} else {
			seenTaskIds.add(t.id);
		}

		if (!isStr(t.category) || !categorySet.has(t.category)) {
			errors.push({
				path: `${tpath}.category`,
				message: `unknown category; must be one of ${TASK_CATEGORIES.map((c) => `"${c}"`).join(", ")}`,
			});
		}

		if (!isStr(t.fixture_id)) {
			errors.push({ path: `${tpath}.fixture_id`, message: "must be a non-empty string" });
		} else if (fixtureSet && !fixtureSet.has(t.fixture_id)) {
			errors.push({
				path: `${tpath}.fixture_id`,
				message: `references unknown fixture_id "${t.fixture_id}" (not present in the provided manifest fixture set)`,
			});
		}

		if (!isStrArray(t.user_turns) || t.user_turns.length === 0) {
			errors.push({ path: `${tpath}.user_turns`, message: "must be a non-empty array of strings" });
		}

		if (!Array.isArray(t.model_script)) {
			errors.push({ path: `${tpath}.model_script`, message: "must be an array of scripted turns" });
		} else {
			t.model_script.forEach((turn, j) => {
				const errs = validateScriptedTurn(turn);
				for (const m of errs) errors.push({ path: `${tpath}.model_script[${j}]`, message: m, task: tid });
			});
		}

		if (!Array.isArray(t.rubric) || t.rubric.length === 0) {
			errors.push({ path: `${tpath}.rubric`, message: "must be a non-empty array of assertions" });
		} else {
			const seenAssertIds = new Set<string>();
			t.rubric.forEach((a, j) => {
				const errs = validateRubricAssertion(a);
				for (const m of errs) errors.push({ path: `${tpath}.rubric[${j}]`, message: m, task: tid });
				if (a && typeof a === "object" && isStr((a as { id?: unknown }).id)) {
					const aid = (a as { id: string }).id;
					if (seenAssertIds.has(aid)) {
						errors.push({ path: `${tpath}.rubric[${j}].id`, message: `duplicate assertion id "${aid}"`, task: tid });
					} else {
						seenAssertIds.add(aid);
					}
				}
			});
		}
	});

	if (errors.length) throw new TasksetValidationError(errors);
	return obj as Taskset;
}

function validateScriptedTurn(turn: unknown): string[] {
	const errs: string[] = [];
	if (!turn || typeof turn !== "object") return ["must be an object"];
	const t = turn as Record<string, unknown>;
	if ("text" in t && t.text !== undefined && !isStr(t.text)) errs.push("text must be a string");
	if ("stopReason" in t && t.stopReason !== undefined && !["stop", "length", "toolUse"].includes(t.stopReason as string)) {
		errs.push("stopReason must be one of stop|length|toolUse");
	}
	if ("toolCalls" in t && t.toolCalls !== undefined) {
		if (!Array.isArray(t.toolCalls)) {
			errs.push("toolCalls must be an array");
		} else {
			t.toolCalls.forEach((tc, k) => {
				if (!tc || typeof tc !== "object") {
					errs.push(`toolCalls[${k}] must be an object`);
					return;
				}
				const c = tc as Record<string, unknown>;
				if (!isStr(c.id)) errs.push(`toolCalls[${k}].id must be a string`);
				if (!isStr(c.name)) errs.push(`toolCalls[${k}].name must be a string`);
				if (!c.arguments || typeof c.arguments !== "object" || Array.isArray(c.arguments)) {
					errs.push(`toolCalls[${k}].arguments must be an object`);
				}
			});
		}
	}
	return errs;
}

function validateRubricAssertion(a: unknown): string[] {
	const errs: string[] = [];
	if (!a || typeof a !== "object") return ["must be an object"];
	const r = a as Record<string, unknown>;
	if (!isStr(r.id)) return ["id must be a non-empty string"];
	if (!isStr(r.type) || !RUBRIC_TYPES.has(r.type as RubricAssertion["type"])) {
		return [`type must be one of ${[...RUBRIC_TYPES].join("|")}`];
	}
	switch (r.type) {
		case "tool_call_sequence":
			if (!isStrArray(r.sequence) || r.sequence.length === 0) errs.push("sequence must be a non-empty array of tool names");
			break;
		case "tool_call_forbidden":
			if (!isStrArray(r.tools) || r.tools.length === 0) errs.push("tools must be a non-empty array of tool names");
			break;
		case "bbox_revisit": {
			if (!isStrArray(r.region_labels) || r.region_labels.length === 0) {
				errs.push("region_labels must be a non-empty array of manifest region labels");
			}
			if (r.tools !== undefined && !(isStrArray(r.tools) && r.tools.length > 0)) {
				errs.push("tools, when present, must be a non-empty array of tool names");
			}
			if (r.min_hits !== undefined && (typeof r.min_hits !== "number" || !Number.isInteger(r.min_hits) || r.min_hits < 1)) {
				errs.push("min_hits, when present, must be an integer >= 1");
			}
			break;
		}
		case "min_snapshot_count":
			if (typeof r.min !== "number" || !Number.isInteger(r.min) || r.min < 0) errs.push("min must be an integer >= 0");
			break;
		case "max_annotation_count":
			if (typeof r.max !== "number" || !Number.isInteger(r.max) || r.max < 0) errs.push("max must be an integer >= 0");
			break;
		case "manual":
			if (!isStr(r.criteria) || r.criteria.trim().length === 0) errs.push("criteria must be a non-empty string");
			break;
		default:
			break;
	}
	return errs;
}

// ------------------------------------------------------------------------- //
// File loader
// ------------------------------------------------------------------------- //

/**
 * Read + JSON.parse + validate a taskset file. Throws on read/parse/validation
 * failure. `knownFixtureIds` optionally cross-checks fixture_id refs against the
 * pinned manifest.
 */
export function loadTaskset(filePath: string, knownFixtureIds?: Iterable<string>): Taskset {
	const text = readFileSync(filePath, "utf8");
	let obj: unknown;
	try {
		obj = JSON.parse(text);
	} catch (e) {
		throw new Error(`taskset file ${filePath} is not valid JSON: ${(e as Error).message}`);
	}
	return validateTaskset(obj, knownFixtureIds);
}
