/**
 * Phase 4 A/B framework (Wave 2) — execution runner mechanics tests.
 *
 * Tests the runner WITHOUT spawning real Flask: the Flask spawn + client factory
 * is dependency-injected (RunnerDeps.acquireEnv) so we substitute the in-memory
 * FlaskClient mock from helpers.ts. Covers:
 *   (a) scripted 1-task/1-arm run produces metrics.jsonl + rubric.json + report.md;
 *   (b) transcript flattening attaches snapshot bbox from toolResult.details.src;
 *   (c) real-model mode without PHASE4_CPA_VERIFIED throws the gate error before any work;
 *   (d) step 2 without --image-arm throws;
 *   (e) arm overrides reach the transform settings (buildRunConfig wiring + a
 *       scripted run with overview_enabled=false completes with zero overview bytes).
 */
import { promises as fs } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { mkdtemp } from "node:fs/promises";

import { afterEach, beforeEach, describe, expect, it } from "vitest";

import type { Taskset, Task, RubricAssertion } from "../experiments/src/taskset.js";
import type { Arm } from "../experiments/src/arms.js";
import type { Manifest } from "../experiments/src/manifest.js";
import type { FlaskClient } from "../src/flask-client.js";
import { makeLegacyFlaskMock, FINGERPRINT, SLIDE_W, SLIDE_H, DOWNSAMPLES, MPP } from "./helpers.js";
import {
	runExperiment,
	buildRunConfig,
	flattenTranscript,
	RunnerArgumentError,
	CpaApiKeyMissingError,
	type FixtureEnv,
	type RunOptions,
	type RunnerDeps,
} from "../experiments/src/run.js";
import { makeFakeStreamFn } from "../experiments/src/fake-stream.js";
import { DataCollectionGateError } from "../experiments/src/gate.js";

// ------------------------------------------------------------------------- //
// Shared stub env: in-memory FlaskClient mock + a one-fixture manifest.
// ------------------------------------------------------------------------- //

const FIXTURE_ID = "fx-test";
const SLIDE_FILE = "test.svs";

function stubManifest(): Manifest {
	return {
		manifest_version: 1,
		generated_at: "1970-01-01T00:00:00Z",
		fixtures: [
			{
				fixture_id: FIXTURE_ID,
				file: SLIDE_FILE,
				size_bytes: 1234,
				sha256: "a".repeat(64),
				fingerprint: FINGERPRINT,
				width: SLIDE_W,
				height: SLIDE_H,
				level_downsamples: [...DOWNSAMPLES],
				mpp: MPP,
				regions: [{ label: "region_A", x: 100, y: 100, w: 200, h: 200, density: "medium" }],
				tags: ["test"],
			},
		],
	};
}

/** Build an acquireEnv that returns the in-memory mock (no real Flask spawn). */
function mockAcquireEnv(): (opts: RunOptions) => Promise<FixtureEnv> {
	return async () => ({
		flask: makeLegacyFlaskMock() as unknown as FlaskClient,
		manifest: stubManifest(),
		teardown: async () => {
			// no-op: in-memory mock, no process to stop
		},
	});
}

// ------------------------------------------------------------------------- //
// Minimal taskset / arm builders (written to temp files for the runner).
// ------------------------------------------------------------------------- //

function finishOnlyTask(id = "t-finish"): Task {
	const rubric: RubricAssertion[] = [
		{ id: "seq", type: "tool_call_sequence", sequence: ["finish"] },
	];
	return {
		id,
		category: "无值得标注区域的完整扫读",
		fixture_id: FIXTURE_ID,
		user_turns: ["请扫读并结束。"],
		model_script: [
			{
				text: "完成扫读。",
				toolCalls: [{ id: "tc-f", name: "finish", arguments: { summary: "done" } }],
				stopReason: "toolUse",
			},
		],
		rubric,
	};
}

function snapshotTask(id = "t-snap"): Task {
	const rubric: RubricAssertion[] = [
		{ id: "minsnap", type: "min_snapshot_count", min: 1 },
		{ id: "revisit", type: "bbox_revisit", region_labels: ["region_A"], tools: ["goto", "snapshot"], min_hits: 1 },
	];
	return {
		id,
		category: "细胞级形态需要重新抓取高倍图",
		fixture_id: FIXTURE_ID,
		user_turns: ["请 goto 到 (150,150) 后抓一张高倍快照并结束。"],
		model_script: [
			{
				text: "导航到目标区域。",
				toolCalls: [{ id: "tc-goto", name: "goto", arguments: { x: 150, y: 150, level: 0, reason: "high-power" } }],
				stopReason: "toolUse",
			},
			{
				toolCalls: [{ id: "tc-snap", name: "snapshot", arguments: {} }],
				stopReason: "toolUse",
			},
			{
				toolCalls: [{ id: "tc-csr", name: "complete_snapshot_review", arguments: { disposition: "no_annotation", summary: "snap", no_annotation_reason: "test" } }],
				stopReason: "toolUse",
			},
			{
				toolCalls: [{ id: "tc-f", name: "finish", arguments: { summary: "done" } }],
				stopReason: "toolUse",
			},
		],
		rubric,
	};
}

function step1Arm(armId: string, overrides: Record<string, unknown> = {}): Arm {
	return {
		arm_id: armId,
		step: 1,
		overrides: { overview_enabled: true, context_window_tokens: 272000, ...overrides },
		prompt_cache_mode: "off",
	};
}

async function writeTaskset(dir: string, tasks: Task[]): Promise<string> {
	const taskset: Taskset = {
		taskset_id: "test-taskset",
		schema_version: 1,
		manifest_version: 1,
		tasks,
	};
	const p = join(dir, "taskset.json");
	await fs.writeFile(p, JSON.stringify(taskset), "utf8");
	return p;
}

async function writeArm(dir: string, arm: Arm): Promise<string> {
	const p = join(dir, `${arm.arm_id}.json`);
	await fs.writeFile(p, JSON.stringify(arm), "utf8");
	return p;
}

// ------------------------------------------------------------------------- //
// Test harness
// ------------------------------------------------------------------------- //

interface Setup {
	outDir: string;
	workDir: string;
	tasksetPath: string;
	armsDir: string;
}

async function setup(tasks: Task[], arms: Arm[]): Promise<Setup> {
	const workDir = await mkdtemp(join(tmpdir(), "svs-ab-test-"));
	const armsDir = join(workDir, "arms");
	const outDir = join(workDir, "out");
	await fs.mkdir(armsDir, { recursive: true });
	const tasksetPath = await writeTaskset(workDir, tasks);
	for (const a of arms) await writeArm(armsDir, a);
	return { outDir, workDir, tasksetPath, armsDir };
}

function baseOpts(setup: Setup, overrides: Partial<RunOptions> = {}): RunOptions {
	return {
		mode: "scripted",
		step: 1,
		tasksetPath: setup.tasksetPath,
		armsDir: setup.armsDir,
		outDir: setup.outDir,
		runId: "test-run",
		repoRoot: setup.workDir,
		fixturesDir: setup.workDir,
		...overrides,
	};
}

beforeEach(() => {
	// Ensure no stale gate / CPA env leaks between tests.
	delete process.env.PHASE4_CPA_VERIFIED;
	delete process.env.CPA_API_KEY;
	delete process.env.CPA_BASE_URL;
	delete process.env.CPA_MODEL;
	delete process.env.CPA_API_PROTOCOL;
});

afterEach(() => {
	delete process.env.PHASE4_CPA_VERIFIED;
	delete process.env.CPA_API_KEY;
	delete process.env.CPA_BASE_URL;
	delete process.env.CPA_MODEL;
	delete process.env.CPA_API_PROTOCOL;
});

// =========================================================================== //
// Tests
// =========================================================================== //

describe("experiments runner — scripted run produces outputs", () => {
	it("writes metrics.jsonl, rubric.json, report.md, run.json for a 1-task/1-arm matrix", async () => {
		const setup_ = await setup([finishOnlyTask()], [step1Arm("s1-a")]);
		const result = await runExperiment(baseOpts(setup_), { acquireEnv: mockAcquireEnv() });

		expect(result.rows.length).toBeGreaterThanOrEqual(1);
		expect(result.armIds).toEqual(["s1-a"]);

		const files = await fs.readdir(setup_.outDir);
		expect(files.sort()).toEqual(["metrics.jsonl", "report.md", "rubric.json", "run.json", "transcripts.json"]);

		// metrics.jsonl: one JSON row, carries task/arm/step.
		const metricsText = await fs.readFile(join(setup_.outDir, "metrics.jsonl"), "utf8");
		const metricsRows = metricsText.trim().split("\n").map((l) => JSON.parse(l) as { task_id: string; arm_id: string; step: number; prompt_cache_mode: string });
		expect(metricsRows.length).toBe(result.rows.length);
		for (const r of metricsRows) {
			expect(r.task_id).toBe("t-finish");
			expect(r.arm_id).toBe("s1-a");
			expect(r.step).toBe(1);
		}

		// rubric.json: one outcome, PASS (the finish sequence matches).
		const rubricJson = JSON.parse(await fs.readFile(join(setup_.outDir, "rubric.json"), "utf8")) as Array<{ overall: string; task_id: string }>;
		expect(rubricJson.length).toBe(1);
		expect(rubricJson[0]!.overall).toBe("PASS");

		// report.md: deterministic, contains the arm row.
		const report = await fs.readFile(join(setup_.outDir, "report.md"), "utf8");
		expect(report).toContain("Phase 4 A/B 报告");
		expect(report).toContain("s1-a");
		// No timestamp inside the report body.
		expect(report).not.toContain("UTC");
		expect(report).not.toMatch(/\d{4}-\d{2}-\d{2}T/);

		// run.json: carries metadata + timestamp.
		const runMeta = JSON.parse(await fs.readFile(join(setup_.outDir, "run.json"), "utf8")) as { run_id: string; step: number; generated_at_utc: string };
		expect(runMeta.run_id).toBe("test-run");
		expect(runMeta.step).toBe(1);
		expect(runMeta.generated_at_utc).toMatch(/^\d{4}-\d{2}-\d{2}T/);
	}, 60_000);

	it("runs a snapshot task and the rubric reflects the snapshot call", async () => {
		const setup_ = await setup([snapshotTask()], [step1Arm("s1-snap")]);
		const result = await runExperiment(baseOpts(setup_), { acquireEnv: mockAcquireEnv() });

		// The snapshot task produced metrics rows.
		expect(result.rows.length).toBeGreaterThanOrEqual(1);
		const snapOutcome = result.rubricOutcomes.find((r) => r.task_id === "t-snap")!;
		expect(snapOutcome).toBeDefined();
		// min_snapshot_count min=1 → the snapshot assertion passes (the scripted
		// snapshot tool call is in the transcript).
		const minSnap = snapOutcome.outcome.results.find((x) => x.assertionId === "minsnap")!;
		expect(minSnap.pass).toBe(true);
	}, 60_000);
});

describe("experiments runner — transcript flattening", () => {
	it("attaches snapshot viewport bbox from toolResult.details.src", () => {
		// Build a minimal toolResult message carrying details.src (the user's
		// liveImageMeta refactor stores src in toolResult.details for snapshots).
		const messages = [
			{ role: "user", content: "q", timestamp: 1 },
			{
				role: "toolResult",
				toolCallId: "tc-snap",
				content: [{ type: "image", data: "QUFBQQ==", mimeType: "image/jpeg" }],
				timestamp: 2,
				details: { src: { x: 100, y: 200, w: 300, h: 400 }, width: 1024, height: 1024 },
			},
		] as never;
		const entries = flattenTranscript(messages);
		const tr = entries.find((e) => e.role === "toolResult")!;
		expect(tr).toBeDefined();
		expect(tr.toolCallId).toBe("tc-snap");
		expect(tr.bbox).toEqual({ x: 100, y: 200, w: 300, h: 400 });
	});

	it("omits bbox when details.src is absent or zero-size", () => {
		const messages = [
			{ role: "toolResult", toolCallId: "tc-1", content: [{ type: "image", data: "AA==", mimeType: "image/jpeg" }], timestamp: 1, details: {} },
		] as never;
		const entries = flattenTranscript(messages);
		expect(entries[0]!.bbox).toBeUndefined();
	});
});

describe("experiments runner — gate + argument validation", () => {
	it("real-model mode without PHASE4_CPA_VERIFIED throws the gate error before any work", async () => {
		const setup_ = await setup([finishOnlyTask()], [step1Arm("s1-a")]);
		// acquireEnv must NOT be called (gate fires first). Use a spy that fails
		// if reached.
		let acquireCalled = false;
		await expect(
			runExperiment(baseOpts(setup_, { mode: "real-model" }), {
				acquireEnv: async () => {
					acquireCalled = true;
					return mockAcquireEnv()({} as never);
				},
			}),
		).rejects.toBeInstanceOf(DataCollectionGateError);
		expect(acquireCalled).toBe(false);

		// With the gate env set BUT no CPA_API_KEY, real-model now fails loudly
		// on the missing key (also before acquireEnv) — no embedded fallback key.
		process.env.PHASE4_CPA_VERIFIED = "1";
		let acquireCalled2 = false;
		await expect(
			runExperiment(baseOpts(setup_, { mode: "real-model" }), {
				acquireEnv: async () => {
					acquireCalled2 = true;
					return mockAcquireEnv()({} as never);
				},
			}),
		).rejects.toBeInstanceOf(CpaApiKeyMissingError);
		expect(acquireCalled2).toBe(false);
	});

	it("step 2 without --image-arm throws RunnerArgumentError", async () => {
		const setup_ = await setup([finishOnlyTask()], [step1Arm("s1-a")]);
		await expect(
			runExperiment(baseOpts(setup_, { step: 2 }), { acquireEnv: mockAcquireEnv() }),
		).rejects.toBeInstanceOf(RunnerArgumentError);
	});

	it("an unknown --image-arm throws RunnerArgumentError", async () => {
		const setup_ = await setup([finishOnlyTask()], [step1Arm("s1-a")]);
		await expect(
			runExperiment(baseOpts(setup_, { step: 1, imageArmId: "does-not-exist" }), { acquireEnv: mockAcquireEnv() }),
		).rejects.toBeInstanceOf(RunnerArgumentError);
	});
});

describe("experiments runner — arm overrides reach transform settings", () => {
	it("buildRunConfig spreads arm resolvedOverrides into the RunConfig (exact wiring point)", () => {
		const arm = {
			arm_id: "s1-x",
			step: 1 as const,
			overrides: {},
			resolvedOverrides: {
				overview_enabled: false,
				overview_long_edge: 768,
				visual_working_set_max: 2,
				visual_context_budget_tokens: 4000,
				context_window_tokens: 272000,
			},
			prompt_cache_mode: "explicit" as const,
		};
		const cfg = buildRunConfig(arm);
		// snake_case fields reach the config object that resolveTransformSettings
		// reads directly off (agent-runner.ts runAgentLoop).
		expect(cfg.overview_enabled).toBe(false);
		expect(cfg.overview_long_edge).toBe(768);
		expect(cfg.visual_working_set_max).toBe(2);
		expect(cfg.visual_context_budget_tokens).toBe(4000);
		expect(cfg.context_window_tokens).toBe(272000);
		expect(cfg.prompt_cache_mode).toBe("explicit");
		// Base engine fields are present.
		expect(cfg.api_protocol).toBe("openai");
	});

	it("overview_enabled=false produces zero overview_image_bytes_sent across all rows", async () => {
		const setup_ = await setup([snapshotTask()], [step1Arm("s1-no-ov", { overview_enabled: false })]);
		const result = await runExperiment(baseOpts(setup_), { acquireEnv: mockAcquireEnv() });
		expect(result.rows.length).toBeGreaterThanOrEqual(1);
		for (const r of result.rows) {
			expect(r.overview_image_bytes_sent).toBe(0);
		}
	}, 60_000);
});

// =========================================================================== //
// Real-model mode (Wave 2: real provider data collection)
// =========================================================================== //

/**
 * A stubbed "real" streamFn injected via RunnerDeps.realStreamFnFactory. In
 * production this seam is unset and the AgentRunner uses its built-in default
 * provider; here we substitute a fake that emits a finish tool call so the
 * cell completes without hitting the real CPA gateway.
 */
function stubRealStreamDeps(scriptTask: Task): Pick<RunnerDeps, "realStreamFnFactory"> {
	// The factory returns a fresh streamFn per cell; we feed the task's own
	// model_script as a stand-in for the real model's turns.
	return {
		realStreamFnFactory: () => {
			const { fn } = makeFakeStreamFn(scriptTask.model_script);
			return fn as never;
		},
	};
}

describe("experiments runner — real-model mode", () => {
	it("drives real user_turns through the injected real stream + records cpa_model/base_url (no key) in run.json", async () => {
		process.env.PHASE4_CPA_VERIFIED = "1";
		process.env.CPA_API_KEY = "sk-test-key-1234567890";
		const setup_ = await setup([finishOnlyTask()], [step1Arm("s1-real")]);
		// Fix the arm to use explicit cache mode (the cache experiment default).
		const deps: RunnerDeps = {
			acquireEnv: mockAcquireEnv(),
			...stubRealStreamDeps(finishOnlyTask()),
		};
		const result = await runExperiment(baseOpts(setup_, { mode: "real-model" }), deps);
		expect(result.mode).toBe("real-model");
		expect(result.rows.length).toBeGreaterThanOrEqual(1);
		expect(result.cpaModel).toBe("gpt-5.6-luna");
		expect(result.cpaBaseUrl).toBe("http://localhost:46450/v1");
		expect(result.cellErrors).toEqual([]);

		// run.json carries cpa_model + cpa_base_url but NEVER the api key.
		const runMeta = JSON.parse(await fs.readFile(join(setup_.outDir, "run.json"), "utf8")) as Record<string, unknown>;
		expect(runMeta.cpa_model).toBe("gpt-5.6-luna");
		expect(runMeta.cpa_base_url).toBe("http://localhost:46450/v1");
		expect(runMeta.cell_errors).toEqual([]);
		const runJsonText = await fs.readFile(join(setup_.outDir, "run.json"), "utf8");
		expect(runJsonText).not.toContain("sk-test-key");
	}, 60_000);

	it("missing CPA_API_KEY throws CpaApiKeyMissingError before acquireEnv (no embedded fallback key)", async () => {
		process.env.PHASE4_CPA_VERIFIED = "1";
		delete process.env.CPA_API_KEY;
		const setup_ = await setup([finishOnlyTask()], [step1Arm("s1-a")]);
		let acquireCalled = false;
		await expect(
			runExperiment(baseOpts(setup_, { mode: "real-model" }), {
				acquireEnv: async () => {
					acquireCalled = true;
					return mockAcquireEnv()({} as never);
				},
			}),
		).rejects.toBeInstanceOf(CpaApiKeyMissingError);
		expect(acquireCalled).toBe(false);
	});

	it("env overrides for CPA_BASE_URL / CPA_MODEL propagate into the run config", async () => {
		process.env.PHASE4_CPA_VERIFIED = "1";
		process.env.CPA_API_KEY = "sk-override-key-xxxxxxxx";
		process.env.CPA_BASE_URL = "http://override.example/v1";
		process.env.CPA_MODEL = "override-model-name";
		const setup_ = await setup([finishOnlyTask()], [step1Arm("s1-real")]);
		const deps: RunnerDeps = {
			acquireEnv: mockAcquireEnv(),
			...stubRealStreamDeps(finishOnlyTask()),
		};
		const result = await runExperiment(baseOpts(setup_, { mode: "real-model" }), deps);
		expect(result.cpaModel).toBe("override-model-name");
		expect(result.cpaBaseUrl).toBe("http://override.example/v1");
		const runMeta = JSON.parse(await fs.readFile(join(setup_.outDir, "run.json"), "utf8")) as Record<string, unknown>;
		expect(runMeta.cpa_model).toBe("override-model-name");
		expect(runMeta.cpa_base_url).toBe("http://override.example/v1");
	}, 60_000);
});

describe("experiments runner — dry-run", () => {
	it("prints the resolved matrix + redacted config without executing or acquiring the env", async () => {
		process.env.PHASE4_CPA_VERIFIED = "1";
		process.env.CPA_API_KEY = "sk-secret-key-abcdef";
		const setup_ = await setup([finishOnlyTask()], [step1Arm("s1-dry")]);

		let acquireCalled = false;
		const logSpy: string[] = [];
		const origLog = console.log;
		console.log = (...a: unknown[]) => { logSpy.push(a.map(String).join(" ")); };
		try {
			const result = await runExperiment(baseOpts(setup_, { mode: "real-model", dryRun: true }), {
				acquireEnv: async () => {
					acquireCalled = true;
					return mockAcquireEnv()({} as never);
				},
			});
			expect(acquireCalled).toBe(false);
			expect(result.rows).toEqual([]);
			expect(result.rubricOutcomes).toEqual([]);
			expect(result.cpaModel).toBe("gpt-5.6-luna");
		} finally {
			console.log = origLog;
		}

		// The dry-run banner was printed and the key is REDACTED (never full).
		const combined = logSpy.join("\n");
		expect(combined).toContain("dry-run");
		expect(combined).toContain("gpt-5.6-luna");
		// The full secret must NEVER appear; redactKey keeps first 3 + last 2 chars
		// plus the length, so "secret" (the middle) must be absent.
		expect(combined).not.toContain("sk-secret-key-abcdef");
		expect(combined).not.toContain("secret");
		expect(combined).toContain("(20 chars)");

		// No output files were written (dry-run returns before writeOutputs).
		const files = await fs.readdir(setup_.outDir).catch(() => [] as string[]);
		expect(files).toEqual([]);
	}, 30_000);

	it("dry-run in scripted mode prints the matrix without CPA config", async () => {
		const setup_ = await setup([finishOnlyTask()], [step1Arm("s1-dry-scripted")]);
		const logSpy: string[] = [];
		const origLog = console.log;
		console.log = (...a: unknown[]) => { logSpy.push(a.map(String).join(" ")); };
		try {
			await runExperiment(baseOpts(setup_, { dryRun: true }), { acquireEnv: mockAcquireEnv() });
		} finally {
			console.log = origLog;
		}
		const combined = logSpy.join("\n");
		expect(combined).toContain("dry-run");
		expect(combined).toContain("scripted");
		expect(combined).not.toContain("cpa_api_key");
	}, 30_000);
});

describe("experiments runner — cost guard (max-cells)", () => {
	it("--max-cells caps the executed cells and skips the rest", async () => {
		const setup_ = await setup(
			[finishOnlyTask("t-a"), finishOnlyTask("t-b")],
			[step1Arm("s1-a"), step1Arm("s1-b")],
		);
		const result = await runExperiment(baseOpts(setup_, { maxCells: 1 }), { acquireEnv: mockAcquireEnv() });
		// 2 arms x 2 tasks = 4 cells; capped to 1 → only 1 cell's rows.
		// Each finishOnlyTask cell produces >= 1 row from a single model call.
		expect(result.rubricOutcomes.length).toBe(1);
	}, 60_000);
});
