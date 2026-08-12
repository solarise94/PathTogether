/**
 * Phase 4 A/B framework (Wave 2: execution runner) — experiment runner library.
 *
 * Drives a ResolvedArm × Task matrix through the REAL AgentRunner (pi agent-loop
 * + tools + Phase 2b assembler). Two modes:
 *
 *   - **scripted** (default): plays back each task's `model_script` via a fake
 *     streamFn (fake-stream.ts). Mechanism validation only; wall_ms / cached
 *     tokens are NOT meaningful.
 *   - **real-model**: drives the taskset's REAL `user_turns` through the runner's
 *     REAL provider streamFn (the AgentRunner default — see "Real streamFn"
 *     below). `model_script` is IGNORED in real-model mode. The CPA gateway
 *     config (base_url / api_key / model) is resolved from env (see
 *     {@link resolveRealModelConfig}). Cache-hit numbers are only meaningful for
 *     openai-protocol arms (gemini path is cache-unobservable, #592).
 *
 * Both modes collect per-request RequestMetrics + a flattened rubric transcript,
 * and write the JSONL + rubric + report outputs.
 *
 * Dependency injection: the Flask environment (FlaskClient + pinned manifest +
 * teardown) is provided by {@link RunnerDeps.acquireEnv}, so the vitest suite can
 * substitute the in-memory FlaskClient mock (helpers.ts) WITHOUT spawning real
 * Flask. The real CLI (run-ab.ts) wires acquireEnv to the spawn+pin pipeline.
 * For real-model testability, {@link RunnerDeps.realStreamFnFactory} lets tests
 * inject a stubbed "real" stream WITHOUT touching agent-runner.ts (production
 * leaves it undefined so the AgentRunner uses its built-in default provider).
 *
 * ## Real streamFn (the exact mechanism)
 *
 * The sanctioned way to obtain the real streamFn is to OMIT the `streamFn`
 * override when constructing `AgentRunner`. The runner's retry wrapper
 * (`agent-runner.ts makeRetryingStreamFn`) falls back to its PRIVATE
 * `defaultStreamFnForConfig(config)` — which dynamically imports the real
 * pi-ai provider module (openai-completions / google-generative-ai) and returns
 * its `streamSimple`, dispatched by `model.api`. So in real-model mode we simply
 * pass NO `streamFn` override (and NO `compactionModels` override, so compaction
 * uses the real model). The seam {@link RunnerDeps.realStreamFnFactory} exists
 * purely so tests can substitute a fake "real" stream.
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
import type { AssistantMessage, AssistantMessageEventStream } from "@earendil-works/pi-ai";

import { SessionStore } from "../../src/session-store.js";
import { SessionEventBus } from "../../src/events.js";
import { AgentRunner, type RunConfig } from "../../src/agent-runner.js";
import { clearRegionLru } from "../../src/transform-context.js";
import type { FlaskClient } from "../../src/flask-client.js";
import { LegacyFlaskPlatformAdapter } from "../../src/platform/legacy-flask-adapter.js";
import type { PersistedAgentMessage } from "../../src/session-store.js";
import type { RequestMetrics } from "../../src/metrics.js";

import { checkRubric, type RubricTranscriptEntry } from "./rubric.js";
import { loadArmDir, buildStepMatrix, DEFAULT_CONTEXT_WINDOW_TOKENS, type Arm, type ArmStep, type ResolvedArm, type ExperimentMatrix } from "./arms.js";
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

// =========================================================================== //
// Real-model (CPA gateway) config
// =========================================================================== //

/** Default CPA gateway base url (openai-compatible /v1 endpoint). */
export const CPA_DEFAULT_BASE_URL = "http://198.51.100.10:46450/v1";
/**
 * Default model for cache experiments. The openai-protocol path on this gateway
 * reports cache hits (gpt-5.6-luna cached_tokens verified 2026-08-12). The
 * gemini path does NOT (CPA antigravity bug #592).
 */
export const CPA_DEFAULT_MODEL = "gpt-5.6-luna";
/** Default api protocol for real-model runs. */
export const CPA_DEFAULT_API_PROTOCOL = "openai" as const;

/** Env var holding the CPA gateway api key (REQUIRED for real-model mode). */
export const CPA_API_KEY_ENV = "CPA_API_KEY";
/** Env var overriding the CPA gateway base url (optional). */
export const CPA_BASE_URL_ENV = "CPA_BASE_URL";
/** Env var overriding the CPA model (optional). */
export const CPA_MODEL_ENV = "CPA_MODEL";
/** Env var overriding the CPA api protocol (optional). */
export const CPA_API_PROTOCOL_ENV = "CPA_API_PROTOCOL";

/**
 * Resolved CPA gateway config for a real-model run. The api_key is held here at
 * runtime (passed into the RunConfig → AgentRunner) but is NEVER serialized to
 * run.json (only base_url + model are recorded). All four fields are overridable
 * via env ({@link resolveRealModelConfig}).
 */
export interface RealModelConfig {
	base_url: string;
	api_key: string;
	model: string;
	api_protocol: "openai" | "anthropic" | "gemini";
}

/** Error thrown when real-model mode is requested without the CPA api key. */
export class CpaApiKeyMissingError extends Error {
	constructor(message: string) {
		super(message);
		this.name = "CpaApiKeyMissingError";
	}
}

/**
 * Resolve the CPA gateway config for a real-model run from the process env.
 *
 * - `CPA_API_KEY` is REQUIRED; a missing/empty key throws
 *   {@link CpaApiKeyMissingError} loudly (NO embedded fallback key — operators
 *   must export the key explicitly so it never leaks into committed source or
 *   logs).
 * - `CPA_BASE_URL` defaults to {@link CPA_DEFAULT_BASE_URL}.
 * - `CPA_MODEL` defaults to {@link CPA_DEFAULT_MODEL}.
 * - `CPA_API_PROTOCOL` defaults to {@link CPA_DEFAULT_API_PROTOCOL}.
 *
 * @param env the process env (defaults to process.env)
 */
export function resolveRealModelConfig(env: NodeJS.ProcessEnv = process.env): RealModelConfig {
	const api_key = env[CPA_API_KEY_ENV];
	if (!api_key || api_key.trim().length === 0) {
		throw new CpaApiKeyMissingError(
			`real-model 模式需要环境变量 ${CPA_API_KEY_ENV}（CPA 网关 api key）。` +
				`为避免密钥泄漏到提交的源码/日志，runner 不内嵌任何回退 key。` +
				`请 export ${CPA_API_KEY_ENV}=sk-... 后重试。` +
				`base_url/model 可选经 ${CPA_BASE_URL_ENV}/${CPA_MODEL_ENV} 覆盖。`,
		);
	}
	const protoRaw = (env[CPA_API_PROTOCOL_ENV] ?? CPA_DEFAULT_API_PROTOCOL) as string;
	if (protoRaw !== "openai" && protoRaw !== "anthropic" && protoRaw !== "gemini") {
		throw new CpaApiKeyMissingError(
			`${CPA_API_PROTOCOL_ENV}="${protoRaw}" 非法；仅支持 openai|anthropic|gemini。`,
		);
	}
	return {
		base_url: env[CPA_BASE_URL_ENV] ?? CPA_DEFAULT_BASE_URL,
		api_key,
		model: env[CPA_MODEL_ENV] ?? CPA_DEFAULT_MODEL,
		api_protocol: protoRaw,
	};
}

/** Redact a key for display: keep the first 3 + last 2 chars, mask the middle. */
export function redactKey(key: string): string {
	if (key.length <= 6) return "***";
	return `${key.slice(0, 3)}…${key.slice(-2)}(${key.length} chars)`;
}

/** Default per-turn settle timeout (ms) — scripted mode (fake stream is fast). */
export const RUNNER_SETTLE_TIMEOUT_MS_SCRIPTED = 30_000;
/** Default per-turn settle timeout (ms) — real-model mode (real turns are slow). */
export const RUNNER_SETTLE_TIMEOUT_MS_REAL = 300_000;

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
	/**
	 * Dry-run: resolve + print the matrix + config (key redacted) and return
	 * WITHOUT acquiring the env or executing any cell. Useful for pre-run review.
	 */
	dryRun?: boolean;
	/**
	 * Cost guard: cap the total number of (arm, task) cells executed. Default:
	 * the full matrix. Cells beyond the cap are skipped (not errored).
	 */
	maxCells?: number;
	/**
	 * Per-turn settle timeout in ms (waitForSettle). Defaults:
	 * scripted {@link RUNNER_SETTLE_TIMEOUT_MS_SCRIPTED}, real-model
	 * {@link RUNNER_SETTLE_TIMEOUT_MS_REAL}. Real-model turns are much slower.
	 */
	settleTimeoutMs?: number;
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

/**
 * A streamFn compatible with {@link AgentRunnerOverrides.streamFn}. Tests inject
 * a fake "real" stream via {@link RunnerDeps.realStreamFnFactory}; production
 * leaves it undefined so the AgentRunner uses its built-in default provider.
 */
export type StreamFn = (model: unknown, context: unknown, options?: unknown) => AssistantMessageEventStream;

export interface RunnerDeps {
	/**
	 * Acquire the Flask + manifest environment for the run. Called once, after
	 * the gate + arg validation pass. Tests inject a stub that returns the
	 * in-memory FlaskClient mock + a fixed manifest + a no-op teardown.
	 */
	acquireEnv: (opts: RunOptions) => Promise<FixtureEnv>;
	/**
	 * TEST SEAM for real-model mode. When set, returns a streamFn that is passed
	 * as the AgentRunner's `streamFn` override (a stubbed "real" stream). When
	 * unset/returns undefined, real-model mode constructs the AgentRunner WITHOUT
	 * a `streamFn` override, so the runner falls back to its built-in
	 * `defaultStreamFnForConfig` — the REAL pi-ai provider. Production never sets
	 * this. This seam exists so the vitest suite can exercise the real-model code
	 * path WITHOUT hitting the real CPA gateway.
	 */
	realStreamFnFactory?: (config: RunConfig) => StreamFn | undefined;
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

/** One cell that errored or timed out (resilience: recorded, not fatal). */
export interface CellError {
	task_id: string;
	arm_id: string;
	step: ArmStep;
	error: string;
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
	/** Cells that errored/timed out (real-model resilience). Empty for clean runs. */
	cellErrors: CellError[];
	manifest: Manifest;
	reportMarkdown: string;
	/** Real-model CPA model (null for scripted). */
	cpaModel: string | null;
	/** Real-model CPA base url, NO key (null for scripted). */
	cpaBaseUrl: string | null;
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
 * snake_case overrides onto the base config. Because
 * `resolveTransformSettings(config)` (agent-runner.ts runAgentLoop) reads these
 * same snake_case fields directly off the config object, the overrides reach the
 * Phase 2b assembler + compaction engine with no intermediate adapter:
 *   - context_window_tokens → engine config (buildModel) + compaction;
 *   - overview_enabled / overview_long_edge / visual_* / image_* → transform settings;
 *   - prompt_cache_mode is set from the arm (separate field, not an override).
 *
 * In real-model mode, pass {@link realModelConfig} so the base_url / api_key /
 * model / api_protocol are the REAL CPA gateway values (buildModel reads them to
 * construct the provider). The arm overrides never touch those four fields
 * (KNOWN_OVERRIDE_KEYS excludes them), so the CPA values are preserved.
 */
export function buildRunConfig(arm: ResolvedArm, realModelConfig?: RealModelConfig): RunConfig {
	const base = realModelConfig
		? {
				...RUNNER_BASE_CONFIG,
				base_url: realModelConfig.base_url,
				api_key: realModelConfig.api_key,
				model: realModelConfig.model,
				api_protocol: realModelConfig.api_protocol,
			}
		: RUNNER_BASE_CONFIG;
	const config: RunConfig = {
		...base,
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
 *
 * Mode branching:
 *   - scripted: fake streamFn playing task.model_script + fake compactionModels.
 *   - real-model: REAL user_turns through the runner's REAL provider streamFn
 *     (injected via realStreamFnFactory for tests; omitted in production so the
 *     AgentRunner uses its default provider) + REAL compaction (no
 *     compactionModels override). model_script is IGNORED in real-model mode.
 */
async function runCell(args: {
	arm: ResolvedArm;
	task: Task;
	fixture: ManifestFixture;
	env: FixtureEnv;
	mode: DataCollectionMode;
	realModelConfig?: RealModelConfig;
	realStreamFnFactory?: RunnerDeps["realStreamFnFactory"];
	settleTimeoutMs: number;
}): Promise<CellResult> {
	const { arm, task, fixture, env, mode, realModelConfig, realStreamFnFactory, settleTimeoutMs } = args;
	const slide = fixture.file;
	const isReal = mode === "real-model";
	// Real-model: pass the CPA config so buildModel wires the real provider.
	const config = buildRunConfig(arm, isReal ? realModelConfig : undefined);

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

	// Collecting metrics sink + per-request wall timestamps.
	const collected: Array<{ metrics: RequestMetrics; at: number }> = [];
	const cellStart = Date.now();
	const metricsSink = (metrics: RequestMetrics): void => {
		collected.push({ metrics, at: Date.now() });
	};

	// Build the AgentRunner overrides per mode.
	const runnerOverrides: Record<string, unknown> = { metricsSink };
	if (isReal) {
		// Real-model: the streamFn is the runner's REAL provider default — we
		// obtain it by OMITTING the streamFn override (the retry wrapper falls
		// back to defaultStreamFnForConfig). The test seam injects a stub.
		const injected = realStreamFnFactory?.(config);
		if (injected) runnerOverrides.streamFn = injected as never;
		// NOTE: no compactionModels override → compaction uses the REAL model.
	} else {
		// Scripted: fake streamFn playing task.model_script + fake compaction.
		const { fn: streamFn } = makeFakeStreamFn(task.model_script);
		runnerOverrides.streamFn = streamFn as never;
		runnerOverrides.compactionModels = fakeCompactionModels("[scripted compaction summary]") as never;
	}

	const runner = new AgentRunner(store, bus, new LegacyFlaskPlatformAdapter({ flask: env.flask }), runnerOverrides as never);

	// Drive user_turns[0] via runMain (fresh main session).
	const turn0 = task.user_turns[0] ?? "";
	const { sessionId } = await runner.runMain({ slide, config, task: turn0, fresh: true });
	await waitForSettle(store, sessionId, settleTimeoutMs);

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
		await waitForSettle(store, sessionId, settleTimeoutMs);
	}

	// Build ReportRows: each collected RequestMetrics → ReportRow with wall_ms
	// (delta from the previous fire; meaningless in scripted mode — documented).
	// Real-model rows carry the CPA api protocol so report.ts can gate the
	// NO-GO banner by protocol (openai verified; gemini #592; scripted omitted).
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
			...(realModelConfig ? { cpa_api_protocol: realModelConfig.api_protocol } : {}),
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

/**
 * Build a synthetic FAIL rubric outcome for a cell that errored or timed out
 * (real-model resilience). Every machine assertion is marked fail with the error
 * detail; the overall is FAIL. The cell contributes NO metrics rows.
 */
function errorRubricOutcome(task: Task, arm: ResolvedArm, error: unknown): RubricOutcomeRow {
	const detail = `cell error: ${(error as Error)?.message || String(error)}`;
	const results = task.rubric.map((a) => ({
		assertionId: a.id,
		type: a.type,
		pass: false,
		detail,
	}));
	return {
		task_id: task.id,
		arm_id: arm.arm_id,
		step: arm.step,
		outcome: { results, overall: "FAIL" as const },
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
		// Real-model CPA provenance (NO api_key ever serialized — only model +
		// base_url so a run.json is safe to share). null for scripted mode.
		cpa_model: result.cpaModel,
		cpa_base_url: result.cpaBaseUrl,
		// Per-cell errors (real-model resilience). Empty for a clean run.
		cell_errors: result.cellErrors,
		generated_at_utc: new Date().toISOString(),
	};
	await fs.writeFile(join(opts.outDir, "run.json"), JSON.stringify(runMeta, null, 2), "utf8");
}

// =========================================================================== //
// Dry-run (resolved matrix + config preview, no execution)
// =========================================================================== //

/**
 * Format the resolved matrix + config for `--dry-run` review. The api key is
 * REDACTED (never printed in full). Deterministic given the inputs.
 */
export function formatDryRun(args: {
	opts: RunOptions;
	matrix: ExperimentMatrix;
	taskset: Taskset;
	realModelConfig?: RealModelConfig;
}): string {
	const { opts, matrix, taskset, realModelConfig } = args;
	const lines: string[] = [];
	lines.push("# Phase 4 A/B dry-run（不执行，仅打印解析结果）");
	lines.push("");
	lines.push(`- mode: ${opts.mode}`);
	lines.push(`- step: ${opts.step}`);
	lines.push(`- run_id: ${opts.runId}`);
	lines.push(`- out_dir: ${opts.outDir}`);
	lines.push(`- taskset: ${taskset.taskset_id} (${taskset.tasks.length} tasks)`);
	lines.push(`- arms: ${matrix.arms.length}`);
	lines.push(`- max_cells: ${opts.maxCells ?? "(full matrix)"}`);
	const fullCells = matrix.arms.length * taskset.tasks.length;
	const effectiveCells = opts.maxCells != null ? Math.min(opts.maxCells, fullCells) : fullCells;
	lines.push(`- effective cells: ${effectiveCells}/${fullCells}`);
	lines.push(`- settle_timeout_ms: ${opts.settleTimeoutMs ?? (opts.mode === "real-model" ? RUNNER_SETTLE_TIMEOUT_MS_REAL : RUNNER_SETTLE_TIMEOUT_MS_SCRIPTED)}`);
	if (opts.imageArmId) lines.push(`- image_arm: ${opts.imageArmId}`);
	if (opts.mode === "real-model" && realModelConfig) {
		lines.push("");
		lines.push("## real-model CPA config（key 已脱敏）");
		lines.push(`- cpa_base_url: ${realModelConfig.base_url}`);
		lines.push(`- cpa_model: ${realModelConfig.model}`);
		lines.push(`- cpa_api_protocol: ${realModelConfig.api_protocol}`);
		lines.push(`- cpa_api_key: ${redactKey(realModelConfig.api_key)}`);
		if (realModelConfig.api_protocol === "gemini") {
			lines.push("");
			lines.push("> ⚠ gemini 协议路径在 CPA 网关上不报告缓存命中（#592）；缓存列不可作为结论。");
		}
	}
	lines.push("");
	lines.push("## resolved arms");
	for (const arm of matrix.arms) {
		lines.push(`- ${arm.arm_id} (step ${arm.step}, prompt_cache_mode=${arm.prompt_cache_mode})`);
		const ov = Object.keys(arm.resolvedOverrides).length
			? Object.entries(arm.resolvedOverrides).map(([k, v]) => `${k}=${JSON.stringify(v)}`).join(", ")
			: "(no overrides)";
		lines.push(`    overrides: ${ov}`);
	}
	lines.push("");
	lines.push("## tasks");
	for (const t of taskset.tasks) {
		lines.push(`- ${t.id} [${t.category}] fixture=${t.fixture_id} user_turns=${t.user_turns.length}`);
	}
	lines.push("");
	return lines.join("\n");
}

// =========================================================================== //
// Main entry
// =========================================================================== //

/**
 * Run one Phase 4 A/B experiment matrix.
 *
 * Order: gate + arg validation FIRST → (dry-run returns here) → resolve CPA
 * config (real-model) → acquire env → cross-check fixtures → for each (arm,
 * task) run a fresh cell (arm-major, tasks in taskset order; real-model cells
 * are resilient — an error/timeout is recorded, not fatal) → aggregate + write
 * outputs → teardown.
 */
export async function runExperiment(opts: RunOptions, deps: RunnerDeps): Promise<RunResult> {
	// 1. Gate FIRST (gate.ts). Scripted always passes; real-model requires
	// PHASE4_CPA_VERIFIED=1 (gate policy lifted for openai-protocol 2026-08-12).
	assertDataCollectionAllowed(opts.mode);

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

	// 5. Resolve real-model CPA config (real-model only). The api key is REQUIRED;
	// a missing key throws loudly BEFORE any env acquisition / cost is incurred.
	const isReal = opts.mode === "real-model";
	const realModelConfig = isReal ? resolveRealModelConfig() : undefined;

	// 6. Dry-run: print the resolved matrix + config (key redacted) and return
	// WITHOUT acquiring the env or executing any cell.
	if (opts.dryRun) {
		const text = formatDryRun({ opts, matrix, taskset, realModelConfig });
		// eslint-disable-next-line no-console
		console.log(text);
		return {
			runId: opts.runId,
			outDir: opts.outDir,
			step: opts.step,
			mode: opts.mode,
			armIds: matrix.arms.map((a) => a.arm_id),
			rows: [],
			rubricOutcomes: [],
			transcripts: [],
			cellErrors: [],
			manifest: { manifest_version: 1, generated_at: "", fixtures: [] },
			reportMarkdown: text,
			cpaModel: realModelConfig?.model ?? null,
			cpaBaseUrl: realModelConfig?.base_url ?? null,
		};
	}

	// 7. Acquire the Flask environment (injectable).
	const env = await deps.acquireEnv(opts);

	// 8. Cross-check taskset fixture_ids against the manifest.
	const fixtureById = new Map<string, ManifestFixture>();
	for (const f of env.manifest.fixtures) fixtureById.set(f.fixture_id, f);
	for (const task of taskset.tasks) {
		if (!fixtureById.has(task.fixture_id)) {
			throw new RunnerArgumentError(`task "${task.id}" references fixture_id "${task.fixture_id}" not present in the pinned manifest`);
		}
	}

	// 9. Per-turn settle timeout (real-model turns are much slower).
	const settleTimeoutMs = opts.settleTimeoutMs ?? (isReal ? RUNNER_SETTLE_TIMEOUT_MS_REAL : RUNNER_SETTLE_TIMEOUT_MS_SCRIPTED);

	// 10. Run every (arm, task) cell: arm-major, tasks in taskset order. Real-model
	// cells are resilient: an error/timeout is recorded (rubric FAIL + error detail
	// in run.json) WITHOUT killing the whole matrix. --max-cells caps the count.
	const allRows: ReportRow[] = [];
	const rubricOutcomes: RubricOutcomeRow[] = [];
	const transcripts: TranscriptRow[] = [];
	const cellErrors: CellError[] = [];
	const maxCells = opts.maxCells ?? Number.POSITIVE_INFINITY;
	let cellsRun = 0;
	for (const arm of matrix.arms) {
		for (const task of taskset.tasks) {
			if (cellsRun >= maxCells) {
				// eslint-disable-next-line no-console
				console.log(`[run] SKIP arm=${arm.arm_id} task=${task.id} (--max-cells ${opts.maxCells} reached)`);
				continue;
			}
			cellsRun += 1;
			const fixture = fixtureById.get(task.fixture_id)!;
			// eslint-disable-next-line no-console
			console.log(`[run] arm=${arm.arm_id} task=${task.id} fixture=${fixture.fixture_id}`);
			try {
				const cell = await runCell({ arm, task, fixture, env, mode: opts.mode, realModelConfig, realStreamFnFactory: deps.realStreamFnFactory, settleTimeoutMs });
				allRows.push(...cell.rows);
				rubricOutcomes.push(cell.rubric);
				transcripts.push(cell.transcript);
			} catch (e) {
				// Resilience: record the cell error + a synthetic FAIL rubric, continue.
				const msg = (e as Error)?.message || String(e);
				// eslint-disable-next-line no-console
				console.error(`[run] ERROR arm=${arm.arm_id} task=${task.id}: ${msg}`);
				cellErrors.push({ task_id: task.id, arm_id: arm.arm_id, step: arm.step, error: msg });
				rubricOutcomes.push(errorRubricOutcome(task, arm, e));
				transcripts.push({ task_id: task.id, arm_id: arm.arm_id, step: arm.step, entries: [] });
			}
		}
	}

	// 11. Aggregate + render.
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
		cellErrors,
		manifest: env.manifest,
		reportMarkdown,
		cpaModel: realModelConfig?.model ?? null,
		cpaBaseUrl: realModelConfig?.base_url ?? null,
	};
	(result as RunResult & { tasksetId?: string; tasksetSchema?: number }).tasksetId = taskset.taskset_id;
	(result as RunResult & { tasksetSchema?: number }).tasksetSchema = taskset.schema_version;

	// 12. Write outputs.
	await writeOutputs(opts, env, result);

	// 13. Teardown (unless --keep-flask).
	if (!opts.keepFlask) {
		await env.teardown().catch(() => undefined);
	}

	return result;
}

/** Re-exports for the CLI + tests (symbols imported into this module). */
export { DataCollectionGateError, basename };
export type { AgentMessage };
