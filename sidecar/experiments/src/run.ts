/**
 * Phase 4 A/B framework (Wave 2: execution runner) — experiment runner library.
 *
 * Drives a ResolvedArm × Task matrix through the REAL AgentRunner (pi agent-loop
 * + tools + Phase 2b assembler), playing back each task's `model_script` via a
 * fake streamFn (scripted mode), collecting per-request RequestMetrics + a
 * flattened rubric transcript, and writing the JSONL + rubric + report outputs.
 *
 * Dependency injection: the Flask environment (FlaskClient + pinned manifest +
 * teardown) is provided by {@link RunnerDeps.acquireEnv}, so the vitest suite can
 * substitute the in-memory FlaskClient mock (helpers.ts) WITHOUT spawning real
 * Flask. The real CLI (run-ab.ts) wires acquireEnv to the spawn+pin pipeline.
 *
 * Wiring point for arm overrides: {@link buildRunConfig} spreads the arm's
 * resolvedOverrides (snake_case) straight into the RunConfig. Because
 * resolveTransformSettings(config) reads those same snake_case fields directly
 * off the config object, an arm's overview_enabled / overview_long_edge /
 * context_window_tokens etc. reach the assembler + engine with NO extra adapter.
 *
 * NOTE: experiment data plane only — NOT built into the shipped sidecar bundle.
 */
import { promises as fs } from "node:fs";
import { tmpdir } from "node:os";
import { basename, join } from "node:path";
import { spawn } from "node:child_process";
import { mkdtemp } from "node:fs/promises";

import type { AgentMessage } from "@earendil-works/pi-agent-core";
import type { AssistantMessage } from "@earendil-works/pi-ai";

import { SessionStore } from "../../src/session-store.js";
import { SessionEventBus } from "../../src/events.js";
import { AgentRunner, type RunConfig } from "../../src/agent-runner.js";
import { clearRegionLru } from "../../src/transform-context.js";
import type { FlaskClient } from "../../src/flask-client.js";
import type { PersistedAgentMessage } from "../../src/session-store.js";
import type { RequestMetrics } from "../../src/metrics.js";

import { checkRubric, type RubricTranscriptEntry } from "./rubric.js";
import { loadArmDir, buildStepMatrix, DEFAULT_CONTEXT_WINDOW_TOKENS, type Arm, type ArmStep, type ResolvedArm } from "./arms.js";
import { loadTaskset, type Task, type Taskset } from "./taskset.js";
import { aggregateReport, renderReport, type ReportRow } from "./report.js";
import { assertDataCollectionAllowed, DataCollectionGateError, type DataCollectionMode } from "./gate.js";
import { makeFakeStreamFn } from "./fake-stream.js";
import type { Manifest, ManifestFixture } from "./manifest.js";

// =========================================================================== //
// Public types
// =========================================================================== //

/** Base engine config for scripted mode (the streamFn is faked, so these are unused). */
export const RUNNER_BASE_CONFIG = {
	base_url: "http://127.0.0.1:0/v1",
	api_key: "scripted-no-key",
	model: "scripted-model",
	max_tokens: 2048,
	api_protocol: "openai" as const,
};

export interface RunOptions {
	mode: DataCollectionMode;
	step: ArmStep;
	/** Step 2 only: the Step-1 arm id whose image overrides are copied in. */
	imageArmId?: string;
	/** Path to the taskset JSON. */
	tasksetPath: string;
	/** Path to the arms directory. */
	armsDir: string;
	/** Output directory (created if missing). */
	outDir: string;
	/** Run id (also the output dir basename). */
	runId: string;
	/** Repo root (for generate.py / app.py paths). */
	repoRoot: string;
	/** Fixtures dir (slides/ lives under here). */
	fixturesDir: string;
	/** Keep Flask alive after the run (debug). */
	keepFlask?: boolean;
}

/**
 * The Flask environment for one run. Injected by {@link RunnerDeps.acquireEnv}
 * so tests substitute the in-memory mock without spawning real Flask.
 */
export interface FixtureEnv {
	/** The FlaskClient the AgentRunner uses (real HTTP or in-memory mock). */
	flask: FlaskClient;
	/** The pinned manifest (fixture ids, fingerprints, ground-truth regions). */
	manifest: Manifest;
	/** Stop the Flask process (no-op in tests). */
	teardown: () => Promise<void>;
}

export interface RunnerDeps {
	/**
	 * Acquire the Flask + manifest environment for the run. Called once, after
	 * the gate + arg validation pass. Tests inject a stub that returns the
	 * in-memory FlaskClient mock + a fixed manifest + a no-op teardown.
	 */
	acquireEnv: (opts: RunOptions) => Promise<FixtureEnv>;
}

/** Per-(task, arm) rubric outcome tagged with its coordinates. */
export interface RubricOutcomeRow {
	task_id: string;
	arm_id: string;
	step: ArmStep;
	outcome: ReturnType<typeof checkRubric>;
}

/** Per-(task, arm) flattened transcript (kept for debugging / re-rubric). */
export interface TranscriptRow {
	task_id: string;
	arm_id: string;
	step: ArmStep;
	entries: RubricTranscriptEntry[];
}

export interface RunResult {
	runId: string;
	outDir: string;
	step: ArmStep;
	mode: DataCollectionMode;
	armIds: string[];
	rows: ReportRow[];
	rubricOutcomes: RubricOutcomeRow[];
	transcripts: TranscriptRow[];
	manifest: Manifest;
	reportMarkdown: string;
}

// =========================================================================== //
// Errors
// =========================================================================== //

export class RunnerArgumentError extends Error {
	constructor(message: string) {
		super(message);
		this.name = "RunnerArgumentError";
	}
}

// =========================================================================== //
// RunConfig wiring (the exact point arm overrides reach transform settings)
// =========================================================================== //

/**
 * Build a RunConfig for one resolved arm by spreading the arm's resolved
 * snake_case overrides onto the scripted base config. Because
 * `resolveTransformSettings(config)` (agent-runner.ts runAgentLoop) reads these
 * same snake_case fields directly off the config object, the overrides reach the
 * Phase 2b assembler + compaction engine with no intermediate adapter:
 *   - context_window_tokens → engine config (buildModel) + compaction;
 *   - overview_enabled / overview_long_edge / visual_* / image_* → transform settings;
 *   - prompt_cache_mode is set from the arm (separate field, not an override).
 */
export function buildRunConfig(arm: ResolvedArm): RunConfig {
	const config: RunConfig = {
		...RUNNER_BASE_CONFIG,
		context_window_tokens: DEFAULT_CONTEXT_WINDOW_TOKENS,
		// Spread the arm's resolved overrides (snake_case) straight in. These are
		// a subset of RunConfig keys (validated by arms.ts KNOWN_OVERRIDE_KEYS).
		...(arm.resolvedOverrides as unknown as Record<string, unknown>),
		prompt_cache_mode: arm.prompt_cache_mode,
	} as RunConfig;
	// context_window_tokens may have been overridden above; ensure it is set.
	if (!config.context_window_tokens) {
		config.context_window_tokens = DEFAULT_CONTEXT_WINDOW_TOKENS;
	}
	return config;
}

// =========================================================================== //
// Scripted-mode compaction fake
// =========================================================================== //

/**
 * A minimal fake pi Models for compaction in scripted mode. Compaction's
 * summarizer normally hits the real provider; in scripted mode there is none, so
 * we return a canned summary (or fail fast) so the run never hangs. Compaction
 * failure is already non-fatal in the agent-runner (runCompaction → null).
 */
export function fakeCompactionModels(summary: string): { completeSimple: (m: unknown, c: unknown, o?: unknown) => Promise<AssistantMessage> } {
	return {
		completeSimple: async () =>
			({
				role: "assistant",
				content: [{ type: "text", text: summary }],
				api: "openai-completions",
				provider: "cpa-gateway",
				model: "scripted-model",
				usage: { input: 100, output: 50, cacheRead: 0, cacheWrite: 0, totalTokens: 150, cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, total: 0 } },
				stopReason: "stop",
				timestamp: Date.now(),
			}) as AssistantMessage,
	};
}

// =========================================================================== //
// Transcript flattening (session messages → RubricTranscriptEntry[])
// =========================================================================== //

/**
 * Flatten a session's canonical messages into the rubric transcript shape.
 *   - assistant → { role, text, toolCalls };
 *   - toolResult → { role, text, toolCallId, bbox } where bbox is the snapshot
 *     viewport from `details.src` (the user's liveImageMeta refactor stores src
 *     in toolResult.details for snapshots);
 *   - user/system → { role, text }.
 */
export function flattenTranscript(messages: PersistedAgentMessage[]): RubricTranscriptEntry[] {
	const out: RubricTranscriptEntry[] = [];
	for (const m of messages) {
		const role = (m as { role?: string }).role;
		const content = (m as { content?: unknown }).content;
		if (role === "assistant") {
			const blocks = Array.isArray(content) ? content : [];
			const text = blocks
				.filter((b): b is { type: "text"; text: string } => !!b && typeof b === "object" && (b as { type?: string }).type === "text")
				.map((b) => b.text)
				.join("");
			const toolCalls = blocks
				.filter((b) => !!b && typeof b === "object" && (b as { type?: string }).type === "toolCall")
				.map((b) => {
					const tc = b as { id: string; name: string; arguments: Record<string, unknown> };
					return { id: tc.id, name: tc.name, arguments: tc.arguments || {} };
				});
			out.push({ role: "assistant", text, toolCalls: toolCalls.length ? toolCalls : undefined });
		} else if (role === "toolResult") {
			const toolCallId = (m as { toolCallId?: string }).toolCallId;
			const details = (m as {
				details?: { src?: { x: number; y: number; w: number; h: number }; width?: number; height?: number };
			}).details;
			const src = details?.src;
			const bbox = src && src.w > 0 && src.h > 0 ? { x: src.x, y: src.y, w: src.w, h: src.h } : undefined;
			const text = textOfContent(content);
			out.push({ role: "toolResult", text, toolCallId, bbox });
		} else {
			out.push({ role: (role === "user" ? "user" : "system") as RubricTranscriptEntry["role"], text: textOfContent(content) });
		}
	}
	return out;
}

function textOfContent(content: unknown): string {
	if (typeof content === "string") return content;
	if (!Array.isArray(content)) return "";
	return content
		.map((b) => {
			if (typeof b === "string") return b;
			if (b && typeof b === "object" && (b as { type?: string }).type === "text") return String((b as { text?: string }).text ?? "");
			return "";
		})
		.join("");
}

// =========================================================================== //
// Settle wait
// =========================================================================== //

/** Poll a session until it reaches a terminal status (not running/idle). */
export async function waitForSettle(store: SessionStore, sessionId: string, timeoutMs = 30_000): Promise<string> {
	const deadline = Date.now() + timeoutMs;
	while (Date.now() < deadline) {
		const d = await store.readSession(sessionId);
		if (d && d.status !== "running" && d.status !== "idle") return d.status;
		await new Promise((r) => setTimeout(r, 20));
	}
	throw new Error(`session ${sessionId} did not settle within ${timeoutMs}ms`);
}

// =========================================================================== //
// Per-cell execution
// =========================================================================== //

interface CellResult {
	rows: ReportRow[];
	rubric: RubricOutcomeRow;
	transcript: TranscriptRow;
}

/**
 * Run one (arm, task) cell: a FRESH SessionStore (temp dir) + SessionEventBus +
 * AgentRunner sharing the run-wide FlaskClient. Drives the task's user_turns
 * through the runner's public runMain/continueMain API, collecting per-request
 * RequestMetrics and a flattened rubric transcript.
 */
async function runCell(args: {
	arm: ResolvedArm;
	task: Task;
	fixture: ManifestFixture;
	env: FixtureEnv;
}): Promise<CellResult> {
	const { arm, task, fixture, env } = args;
	const slide = fixture.file;
	const config = buildRunConfig(arm);

	// A/B fairness: the derivative LRU (transform-context.ts) is MODULE-LEVEL, so
	// without a reset, earlier cells warm the cache for later cells (later arms
	// would show inflated image_lru_hits). Clear it per cell so every (arm, task)
	// starts cold. NOTE: Flask-side caches (slide handles etc.) persist across
	// cells — acceptable in scripted mode (JPEG output is deterministic), but a
	// documented caveat for real-model data collection (README).
	clearRegionLru();

	// Fresh per-cell session store in a private temp dir.
	const cellDir = await mkdtemp(join(tmpdir(), `svs-ab-${arm.arm_id}-${task.id}-`));
	const store = new SessionStore({ sessionsDir: cellDir });
	const bus = new SessionEventBus(store);

	// Fake streamFn playing the task's model_script, keyed by assistant count.
	const { fn: streamFn } = makeFakeStreamFn(task.model_script);

	// Collecting metrics sink + per-request wall timestamps.
	const collected: Array<{ metrics: RequestMetrics; at: number }> = [];
	const cellStart = Date.now();
	let prevFire = cellStart;
	const metricsSink = (metrics: RequestMetrics): void => {
		const at = Date.now();
		collected.push({ metrics, at });
		void prevFire; // prevFire used below in row construction
		prevFire = at;
	};

	const runner = new AgentRunner(store, bus, env.flask, {
		streamFn: streamFn as never,
		metricsSink,
		compactionModels: fakeCompactionModels("[scripted compaction summary]") as never,
	});

	// Drive user_turns[0] via runMain (fresh main session).
	const turn0 = task.user_turns[0] ?? "";
	const { sessionId } = await runner.runMain({ slide, config, task: turn0, fresh: true });
	await waitForSettle(store, sessionId);

	// Drive subsequent user_turns by appending the user message then continuing.
	for (let i = 1; i < task.user_turns.length; i++) {
		const q = task.user_turns[i]!;
		await store.withLock(sessionId, async (d) => {
			if (!d) return null;
			const msg: PersistedAgentMessage = {
				role: "user",
				content: q,
				display_text: q,
				timestamp: Date.now(),
			} as PersistedAgentMessage;
			(d as { messages: PersistedAgentMessage[] }).messages.push(msg);
			d.updated_at = Math.floor(Date.now() / 1000);
			await store.writeSession(sessionId, d);
			return d;
		});
		await runner.continueMain({ slide, config });
		await waitForSettle(store, sessionId);
	}

	// Build ReportRows: each collected RequestMetrics → ReportRow with wall_ms
	// (delta from the previous fire; meaningless in scripted mode — documented).
	let prev = cellStart;
	const rows: ReportRow[] = collected.map(({ metrics, at }) => {
		const wall_ms = at - prev;
		prev = at;
		return {
			...metrics,
			task_id: task.id,
			arm_id: arm.arm_id,
			step: arm.step,
			wall_ms,
		};
	});

	// Flatten the settled transcript + run the rubric.
	const settled = await store.readSession(sessionId);
	const messages = (settled?.messages || []) as PersistedAgentMessage[];
	const transcript = flattenTranscript(messages);
	const regions: Record<string, { x: number; y: number; w: number; h: number }> = {};
	for (const r of fixture.regions) regions[r.label] = { x: r.x, y: r.y, w: r.w, h: r.h };
	const outcome = checkRubric(task.rubric, transcript, regions);

	// Best-effort cleanup of the per-cell temp dir (the loop may still be
	// settling background writes; ignore ENOENT races).
	await fs.rm(cellDir, { recursive: true, force: true }).catch(() => undefined);

	return {
		rows,
		rubric: { task_id: task.id, arm_id: arm.arm_id, step: arm.step, outcome },
		transcript: { task_id: task.id, arm_id: arm.arm_id, step: arm.step, entries: transcript },
	};
}

// =========================================================================== //
// Fixture pipeline (generate slides if missing)
// =========================================================================== //

/**
 * Ensure the slides directory is non-empty, spawning the repo-root
 * `generate.py` (no --pin) when it is. Idempotent: existing slides are reused.
 */
export async function ensureSlides(repoRoot: string, fixturesDir: string, slidesDir: string): Promise<void> {
	let existing: string[] = [];
	try {
		existing = (await fs.readdir(slidesDir)).filter((f) => f.endsWith(".tiff") || f.endsWith(".tif"));
	} catch {
		// dir may not exist yet
	}
	if (existing.length > 0) return;
	await fs.mkdir(slidesDir, { recursive: true });
	const gen = join(fixturesDir, "generate.py");
	const py = join(repoRoot, ".venv", "bin", "python");
	await new Promise<void>((resolve, reject) => {
		const child = spawn(py, [gen, "--out-dir", slidesDir], { cwd: repoRoot, stdio: ["ignore", "pipe", "pipe"] });
		let stderr = "";
		child.stderr?.on("data", (c: Buffer) => { stderr += c.toString("utf8"); });
		child.on("error", reject);
		child.on("exit", (code) => {
			if (code === 0) resolve();
			else reject(new Error(`generate.py exited ${code}\n${stderr.slice(-2000)}`));
		});
	});
}

/**
 * Run `generate.py --pin` against a live Flask to produce a pinned manifest.
 * `token` is the AI_INTERNAL_TOKEN the Flask was spawned with — it is passed
 * EXPLICITLY via the child's env (the Flask spawn generates a fresh random
 * token per run, so relying on the parent process env would 401).
 * Returns the written manifest path.
 */
export async function pinManifest(args: {
	repoRoot: string;
	fixturesDir: string;
	slidesDir: string;
	flaskUrl: string;
	manifestPath: string;
	/** The AI_INTERNAL_TOKEN the target Flask was spawned with. */
	token: string;
}): Promise<void> {
	const { repoRoot, fixturesDir, slidesDir, flaskUrl, manifestPath, token } = args;
	const gen = join(fixturesDir, "generate.py");
	const py = join(repoRoot, ".venv", "bin", "python");
	await new Promise<void>((resolve, reject) => {
		const child = spawn(py, [gen, "--pin", "--flask-url", flaskUrl, "--out-dir", slidesDir, "--manifest", manifestPath], {
			cwd: repoRoot,
			env: { ...process.env, AI_INTERNAL_TOKEN: token },
			stdio: ["ignore", "pipe", "pipe"],
		});
		let stderr = "";
		child.stderr?.on("data", (c: Buffer) => { stderr += c.toString("utf8"); });
		child.stdout?.on("data", (c: Buffer) => { stderr += c.toString("utf8"); });
		child.on("error", reject);
		child.on("exit", (code) => {
			if (code === 0) resolve();
			else reject(new Error(`generate.py --pin exited ${code}\n${stderr.slice(-3000)}`));
		});
	});
}

// =========================================================================== //
// Output writers
// =========================================================================== //

async function writeOutputs(opts: RunOptions, env: FixtureEnv, result: RunResult): Promise<void> {
	await fs.mkdir(opts.outDir, { recursive: true });

	// metrics.jsonl — one ReportRow per line.
	await fs.writeFile(join(opts.outDir, "metrics.jsonl"), result.rows.map((r) => JSON.stringify(r)).join("\n") + (result.rows.length ? "\n" : ""), "utf8");

	// rubric.json — per (task, arm) outcome incl. per-assertion detail.
	const rubricJson = result.rubricOutcomes.map((r) => ({
		task_id: r.task_id,
		arm_id: r.arm_id,
		step: r.step,
		overall: r.outcome.overall,
		assertions: r.outcome.results,
	}));
	await fs.writeFile(join(opts.outDir, "rubric.json"), JSON.stringify(rubricJson, null, 2), "utf8");

	// transcripts.json — flattened transcripts (debugging / re-rubric).
	await fs.writeFile(join(opts.outDir, "transcripts.json"), JSON.stringify(result.transcripts, null, 2), "utf8");

	// report.md — deterministic markdown (NO timestamps in the body).
	await fs.writeFile(join(opts.outDir, "report.md"), result.reportMarkdown, "utf8");

	// run.json — run metadata (timestamps allowed here).
	const manifestSha = env.manifest.fixtures.map((f) => f.sha256).sort().join("|");
	const runMeta = {
		run_id: result.runId,
		step: result.step,
		mode: result.mode,
		arm_ids: result.armIds,
		taskset_id: (result as { tasksetId?: string }).tasksetId,
		taskset_schema_version: (result as { tasksetSchema?: number }).tasksetSchema,
		manifest_sha: manifestSha,
		image_arm_id: opts.imageArmId ?? null,
		keep_flask: !!opts.keepFlask,
		generated_at_utc: new Date().toISOString(),
	};
	await fs.writeFile(join(opts.outDir, "run.json"), JSON.stringify(runMeta, null, 2), "utf8");
}

// =========================================================================== //
// Main entry
// =========================================================================== //

/**
 * Run one Phase 4 A/B experiment matrix.
 *
 * Order: gate + arg validation FIRST → acquire env → cross-check fixtures →
 * for each (arm, task) run a fresh cell (arm-major, tasks in taskset order) →
 * aggregate + write outputs → teardown.
 */
export async function runExperiment(opts: RunOptions, deps: RunnerDeps): Promise<RunResult> {
	// 1. Gate FIRST (gate.ts). Scripted always passes; real-model requires
	// PHASE4_CPA_VERIFIED=1.
	assertDataCollectionAllowed(opts.mode);
	// Belt-and-suspenders: real-model is not implemented in Wave 2 (provider
	// wiring lands with CPA verification). The gate already blocks everyone
	// without PHASE4_CPA_VERIFIED=1; this guards the day the gate is lifted.
	if (opts.mode === "real-model") {
		throw new Error(
			"real-model 模式在 Wave 2 尚未实现——真实 provider 接线随 CPA 验证落地。" +
				"当前仅支持 scripted 模式（makeFakeStreamFn 回放 model_script）。",
		);
	}

	// 2. Arg validation: step 2 requires --image-arm.
	if (opts.step === 2 && !opts.imageArmId) {
		throw new RunnerArgumentError("Step 2 需要传 --image-arm 指向 Step 1 的胜出 arm id（用于解析 image_strategy 占位符）");
	}

	// 3. Load taskset + arms.
	const taskset: Taskset = loadTaskset(opts.tasksetPath);
	const arms: Arm[] = loadArmDir(opts.armsDir, opts.step);

	// Resolve the image-arm for step 2 (if given).
	let imageArm: Arm | undefined;
	if (opts.imageArmId) {
		imageArm = arms.find((a) => a.arm_id === opts.imageArmId) ?? loadArmDir(opts.armsDir).find((a) => a.arm_id === opts.imageArmId);
		if (!imageArm) throw new RunnerArgumentError(`--image-arm "${opts.imageArmId}" 未在 arms 目录中找到`);
		if (imageArm.step !== 1) throw new RunnerArgumentError(`--image-arm "${opts.imageArmId}" 必须是 Step 1 arm（got step=${imageArm.step}）`);
	}

	// 4. Build the matrix.
	const matrix = buildStepMatrix(arms, opts.step, imageArm ? { imageArm } : {});

	// 5. Acquire the Flask environment (injectable).
	const env = await deps.acquireEnv(opts);

	// 6. Cross-check taskset fixture_ids against the manifest.
	const fixtureById = new Map<string, ManifestFixture>();
	for (const f of env.manifest.fixtures) fixtureById.set(f.fixture_id, f);
	for (const task of taskset.tasks) {
		if (!fixtureById.has(task.fixture_id)) {
			throw new RunnerArgumentError(`task "${task.id}" references fixture_id "${task.fixture_id}" not present in the pinned manifest`);
		}
	}

	// 7. Run every (arm, task) cell: arm-major, tasks in taskset order.
	const allRows: ReportRow[] = [];
	const rubricOutcomes: RubricOutcomeRow[] = [];
	const transcripts: TranscriptRow[] = [];
	for (const arm of matrix.arms) {
		for (const task of taskset.tasks) {
			const fixture = fixtureById.get(task.fixture_id)!;
			// eslint-disable-next-line no-console
			console.log(`[run] arm=${arm.arm_id} task=${task.id} fixture=${fixture.fixture_id}`);
			const cell = await runCell({ arm, task, fixture, env });
			allRows.push(...cell.rows);
			rubricOutcomes.push(cell.rubric);
			transcripts.push(cell.transcript);
		}
	}

	// 8. Aggregate + render.
	const data = aggregateReport(allRows, rubricOutcomes.map((r) => ({ task_id: r.task_id, arm_id: r.arm_id, step: r.step, outcome: r.outcome })));
	const reportMarkdown = renderReport(data);

	const result: RunResult = {
		runId: opts.runId,
		outDir: opts.outDir,
		step: opts.step,
		mode: opts.mode,
		armIds: matrix.arms.map((a) => a.arm_id),
		rows: allRows,
		rubricOutcomes,
		transcripts,
		manifest: env.manifest,
		reportMarkdown,
	};
	(result as RunResult & { tasksetId?: string; tasksetSchema?: number }).tasksetId = taskset.taskset_id;
	(result as RunResult & { tasksetSchema?: number }).tasksetSchema = taskset.schema_version;

	// 9. Write outputs.
	await writeOutputs(opts, env, result);

	// 10. Teardown (unless --keep-flask).
	if (!opts.keepFlask) {
		await env.teardown().catch(() => undefined);
	}

	return result;
}

/** Re-exports for the CLI + tests. */
export { DataCollectionGateError, basename };
export type { AgentMessage };
