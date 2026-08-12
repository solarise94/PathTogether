/**
 * AI reading assistant sidecar — pi Agent runner (Step 3, core).
 *
 * Wraps a pi {@link Agent} to drive a reading session and translate pi
 * lifecycle events into the SSE event vocabulary the frontend expects
 * (ai_agent.py:490 `run_agent` + app.py:1941 `_start_main_worker` /
 * 2012 `_start_fork_worker`).
 *
 * Run-level responsibilities (each aligned to the Python original):
 *   - **acquire** the session (409 on conflict) and emit the setup event
 *     (slide_opened / session_resumed / fork_created / fork_resumed);
 *   - drive the pi agent loop with the domain tools (tools.ts) and a
 *     transient-error-retrying streamFn wrapper;
 *   - map pi events to SSE events (agent_thinking / text_delta /
 *     agent_finished / agent_paused / agent_error / agent_retrying);
 *   - enforce max_steps (shouldStopAfterTurn) and the pending-snapshot
 *     plain-text guard (getFollowUpMessages);
 *   - persist transcript + agent_state + usage and transition status
 *     (finished/error/paused), which the SSE layer observes to emit
 *     session_ended.
 *
 * The runner is **async-fire**: run/continue/ask return `{sessionId}` as soon
 * as the session is acquired and the setup event emitted; the agent loop runs
 * in the background and reports completion via the event bus (the SSE stream
 * tails the bus and the session status).
 */
import { Agent, type AgentEvent, type AgentMessage } from "@earendil-works/pi-agent-core";
import type {
	AssistantMessage,
	AssistantMessageEvent,
	AssistantMessageEventStream,
	Message,
	Usage,
} from "@earendil-works/pi-ai";
import { createAssistantMessageEventStream } from "@earendil-works/pi-ai";

import { SYSTEM_PROMPT, DEFAULT_TASK, FORK_LITE_SYSTEM_PROMPT, makeMainMessages, makeForkMessages, type SpotDict } from "./prompts.js";
import { buildModel, type AiEngineConfig } from "./pi-model.js";
import {
	AgentState,
	createTools,
	type SlideInfo,
	type ToolContext,
} from "./tools.js";
import {
	SessionConflict,
	appendMessages,
	collectImageMeta,
	dehydrateMessages,
	isImageContent,
	isImageRefContent,
	replaceMessagesPreservingSeq,
	type PersistedAgentMessage,
	type SessionData,
	type SessionStore,
} from "./session-store.js";
// Re-export so server.ts can catch it uniformly alongside RootAnnotationGone.
export { SessionConflict };
import { ContractError, bytesToBase64, legacySlide, type PlatformClient, type RegionResult, type RoiDict } from "./platform/contract.js";
import { SessionEventBus } from "./events.js";
import {
	buildSpotIndexMessage,
	checkShouldCompact,
	persistCompaction,
	prevCompactionInputs,
	resolveCompactionSettings,
	runCompaction,
	type ResolvedCompactionSettings,
} from "./compaction.js";
import {
	invalidateRegionLru,
	makeTransformContext,
	resolveTransformSettings,
} from "./transform-context.js";
import {
	buildPostCompactionCheckpoint,
	estimateSelectedVisualTokens,
	DEFAULT_VISUAL_CONTEXT_BUDGET_TOKENS,
} from "./compaction.js";
import {
	REQUEST_SCHEMA_VERSION,
	buildOverviewDerivative,
	buildStablePrefixObject,
	checkpointStale,
	computeSystemPromptVersion,
	computeToolSchemaHash,
	ensureCheckpoint,
	selectOverviewRef,
	stablePrefixHash,
	type CheckpointEnv,
	type ContextCheckpoint,
} from "./checkpoint.js";
import {
	materializeDerivativeRaw,
	overviewDerivativeSpec,
	TRANSFORM_ENCODER_ID,
	TRANSFORM_RESIZE_ALGORITHM,
} from "./transform-context.js";
import {
	makeRequestAssembler,
	type AssemblerMetrics,
	type AssemblerSessionSnapshot,
} from "./request-assembler.js";
import {
	buildPreparedRequest,
	StableContextUnavailableError,
	type PreparedRequest,
} from "./prepared-request.js";
import {
	buildRequestMetrics,
	defaultMetricsSink,
	type MetricsSink,
} from "./metrics.js";
import {
	buildCacheKeySamplingParams,
	buildPromptCacheKey,
	downgradeCacheKeyCapability,
	isCacheFieldRejection,
	mergeCacheKeyOptions,
	resolvePromptCacheCapabilities,
	stripCacheKeyOptions,
	type PromptCacheCapabilities,
} from "./prompt-cache.js";

// =========================================================================== //
// Public config / option types
// =========================================================================== //

/**
 * Per-run engine config + tuning knobs, injected by the caller (Flask proxy)
 * in the request body. The sidecar never reads ai_config.json itself.
 */
export interface RunConfig extends AiEngineConfig {
	/** Per-run step cap (ai_agent.py:504 default 50). */
	max_steps?: number;
	/** Active fork limit before oldest non-running fork is archived (app.py:1917). */
	fork_active_limit?: number;
	/** Max materialized images retained per request by transformContext (default 6). */
	keep_recent_images?: number;
	/** Tokens reserved for summary prompt + output in compaction (default 16384). */
	reserve_tokens?: number;
	/** Approximate recent-context tokens kept after compaction (default 20000). */
	keep_recent_tokens?: number;
	/** Legacy field (ai_session.py safety_margin); accepted but unused. */
	safety_margin?: number;
	/** Phase 1 config fields (§11). */
	visual_working_set_max?: number;
	visual_context_budget_tokens?: number;
	overview_long_edge?: number;
	working_image_long_edge?: number;
	detail_image_long_edge?: number;
	image_jpeg_quality?: number;
	image_overlay_version?: string;
	region_materialize_concurrency?: number;
	image_derivative_cache_max_mb?: number;
	image_derivative_cache_ttl?: number;
	prompt_cache_mode?: string;
	/**
	 * Phase 4 §17 risk 2 product switch (default true): when false, the Phase 2b
	 * assembler omits the stable overview image. Whitelisted by app.py
	 * `_validate_ai_tuning` and resolved by `resolveTransformSettings`.
	 */
	overview_enabled?: boolean;
}

/** Common run arguments. `config` is required. */
export interface RunArgs {
	slide: string;
	config: RunConfig;
}

/**
 * Run-boundary validation error (§9.2/§11). Thrown by {@link validateRunConfig};
 * server.ts entry handlers map it to HTTP 400 (distinct from 500 internal
 * errors and 409 session conflicts).
 */
export class ConfigError extends Error {
	constructor(message: string) {
		super(message);
		this.name = "ConfigError";
	}
}

/**
 * Run-boundary validation (§9.2/§11). Mirrors the Flask `_validate_ai_tuning`
 * checks so a config that bypassed the Flask API (e.g. hand-edited
 * ai_config.json) cannot start a run with contradictory parameters.
 *
 * Throws {@link ConfigError} (Chinese message) on the first violation. Called
 * by server.ts entry handlers before any run is started.
 */
export function validateRunConfig(config: RunConfig): void {
	const num = (v: unknown): number => {
		const n = Number(v);
		return Number.isFinite(n) ? n : NaN;
	};
	// reserve_tokens + keep_recent_tokens < context_window_tokens (§9.2)
	const reserve = num(config.reserve_tokens);
	const keep = num(config.keep_recent_tokens);
	const ctx = num(config.context_window_tokens);
	if (Number.isFinite(reserve) && Number.isFinite(keep) && Number.isFinite(ctx)) {
		if (reserve + keep >= ctx) {
			throw new ConfigError(
				`reserve_tokens + keep_recent_tokens（${reserve + keep}）必须小于 context_window_tokens（${ctx}）`,
			);
		}
	}
	// Positive-int range checks for the Phase 1 fields (§11). Only validate when
	// present; defaults are applied by resolveTransformSettings/resolveCompactionSettings.
	const positiveIntFields: Array<keyof RunConfig> = [
		"visual_working_set_max",
		"visual_context_budget_tokens",
		"overview_long_edge",
		"working_image_long_edge",
		"detail_image_long_edge",
		"image_jpeg_quality",
		"region_materialize_concurrency",
		"image_derivative_cache_max_mb",
		"image_derivative_cache_ttl",
	];
	for (const field of positiveIntFields) {
		const v = num((config as unknown as Record<string, unknown>)[field as string]);
		if (!Number.isNaN(v) && (v <= 0 || !Number.isInteger(v))) {
			throw new ConfigError(`${String(field)} 需为正整数（> 0）`);
		}
	}
	// Long-edge upper bound (§6.1, ≤ 4096).
	for (const field of ["overview_long_edge", "working_image_long_edge", "detail_image_long_edge"] as const) {
		const v = num((config as unknown as Record<string, unknown>)[field]);
		if (!Number.isNaN(v) && v > 4096) {
			throw new ConfigError(`${field} 不可超过 4096（最长边上限）`);
		}
	}
	// prompt_cache_mode enum (§8.1).
	const pcm = config.prompt_cache_mode;
	if (pcm !== undefined && pcm !== "off" && pcm !== "auto" && pcm !== "explicit") {
		throw new ConfigError("prompt_cache_mode 仅支持 off/auto/explicit");
	}
}

/** Inject a test streamFn into the runner (mock model). */
export interface AgentRunnerOverrides {
	/** Override the streamFn used by the pi Agent (tests pass a fake). */
	streamFn?: (model: unknown, context: unknown, options?: unknown) => AssistantMessageEventStream;
	/**
	 * Override the Models used by compaction's summarizer (tests pass a fake
	 * completeSimple). When unset, compaction uses the model's real registered
	 * catalog (buildModel). Production never sets this.
	 */
	compactionModels?: { completeSimple: (model: unknown, context: unknown, options?: unknown) => Promise<unknown> };
	/**
	 * Override the §12 metrics sink (tests capture into an array). When unset,
	 * the runner uses {@link defaultMetricsSink} (JSON line on console.info).
	 */
	metricsSink?: MetricsSink;
}

// =========================================================================== //
// Errors
// =========================================================================== //

/** Root annotation no longer exists (app.py:1705 returns 410). */
export class RootAnnotationGone extends Error {
	constructor(message = "该标注已删除") {
		super(message);
		this.name = "RootAnnotationGone";
	}
}

// =========================================================================== //
// AgentRunner
// =========================================================================== //

/**
 * One per sidecar process. Owns the {@link SessionStore}, the live
 * {@link SessionEventBus}, and the {@link PlatformClient} (the platform
 * capability surface; the concrete legacy adapter is wired in index.ts). Run
 * methods are async-fire: they acquire + emit setup + kick off the loop, then
 * return.
 */
export class AgentRunner {
	readonly store: SessionStore;
	readonly bus: SessionEventBus;
	readonly flask: PlatformClient;
	private readonly overrides: AgentRunnerOverrides;
	/** §12 metrics sink (defaults to console.info JSON line). */
	private readonly metricsSink: MetricsSink;
	/** Active agent per session, for cancel(). */
	private readonly activeAgents = new Map<string, Agent>();

	constructor(store: SessionStore, bus: SessionEventBus, flask: PlatformClient, overrides: AgentRunnerOverrides = {}) {
		this.store = store;
		this.bus = bus;
		this.flask = flask;
		this.overrides = overrides;
		this.metricsSink = overrides.metricsSink ?? defaultMetricsSink;
	}

	// ----------------------------------------------------------------------- //
	// run (fresh / reuse main) — app.py:1636 api_ai_run + 1941 _start_main_worker
	// ----------------------------------------------------------------------- //
	/**
	 * Start (or resume) the main session for a slide.
	 *
	 * - `fresh=true`: archive any existing main, create a new session, emit
	 *   `slide_opened` with the overview viewport, then run the loop from the
	 *   initial user task message.
	 * - `fresh=false` with an existing main: resume (continue) it.
	 * - `fresh=false` with no main: behave as fresh.
	 *
	 * Returns `{sessionId}` immediately; the loop runs in the background.
	 * Throws {@link SessionConflict} (409) if the main is already running.
	 */
	async runMain(args: RunArgs & { task?: string; fresh?: boolean }): Promise<{ sessionId: string }> {
		const { slide, config } = args;
		const fresh = args.fresh ?? false;

		// Resolve which session to run.
		let sessionId: string;
		let isContinue: boolean;
		if (fresh) {
			// Archive the old main slot (app.py:1655 fresh path).
			await this.archiveMainSlot(slide);
			const data = await this.store.acquire({ slide, kind: "main" });
			sessionId = data.id;
			isContinue = false;
		} else {
			const idx = await this.store.listBySlide(slide);
			const existing = idx.main;
			if (existing) {
				const data = await this.store.acquire({ sessionId: existing, slide, kind: "main" });
				sessionId = data.id;
				isContinue = true;
			} else {
				const data = await this.store.acquire({ slide, kind: "main" });
				sessionId = data.id;
				isContinue = false;
			}
		}

		// Kick off the loop without awaiting completion.
		void this.driveMain(sessionId, slide, config, args.task ?? "", isContinue).catch(async (e) => {
			await this.handleFatal(sessionId, e);
		});
		return { sessionId };
	}

	/** continue = runMain with fresh=false (app.py:1668). */
	async continueMain(args: RunArgs): Promise<{ sessionId: string }> {
		const idx = await this.store.listBySlide(args.slide);
		const sid = idx.main;
		if (!sid) {
			throw new SessionConflict("没有可继续的主会话");
		}
		return this.runMain({ ...args, fresh: false });
	}

	// ----------------------------------------------------------------------- //
	// ask (fork create/resume) — app.py:1687 api_ai_ask + 2012 _start_fork_worker
	// ----------------------------------------------------------------------- //
	/**
	 * Start or resume a **lite** fork for an annotation (批注小框纯解读对话).
	 *
	 * A fork (kind="fork") registers NO tools: the model answers purely from
	 * the spot card + attached image and the conversation. A plain-text turn
	 * ends the回合 naturally (agent_finished). Legacy forks with historical
	 * tool calls in their transcript are preserved on resume; only new tool
	 * availability is removed.
	 *
	 * - Root annotation gone → throws {@link RootAnnotationGone} (→ 410).
	 * - Existing fork for this annotation → resume it (append the question,
	 *   emit `fork_resumed`).
	 * - Otherwise: enforce the fork-active limit (archive oldest non-running),
	 *   create a new fork, emit `fork_created`, then run the loop.
	 *
	 * Returns `{sessionId}` immediately.
	 */
	async askFork(args: RunArgs & { annotationId: string; question?: string }): Promise<{ sessionId: string; streamFromSeq: number }> {
		const { slide, config, annotationId } = args;

		// Locate the root annotation via the spot change log (tombstone-aware).
		const roi = await this.findSpot(slide, annotationId);
		if (!roi || roi.deleted) {
			throw new RootAnnotationGone();
		}

		const idx = await this.store.listBySlide(slide);
		const existing = idx.forks[annotationId];

		if (existing) {
			// Resume: acquire, append the question, emit fork_resumed, run.
			const data = await this.store.acquire({ sessionId: existing, slide, kind: "fork", annotationId });
			// SSE 起点水位：续聊时流只带本轮新事件（fork_resumed 起），
			// 不从 0 重放历史——前端小框对话是增量渲染（不会清空重排），
			// 重放会把旧工具轨迹重复渲染一遍。（create 路径从 0 起流。）
			const streamFromSeq = data.last_event_seq || 0;
			const qText = args.question || "请谈谈这个区域";
			// Append the user question to the transcript (app.py:1720). Goes
			// through the seq-allocating append path (§10).
			const updated = await this.store.withLock(data.id, async (d) => {
				if (!d) return null;
				const msg: PersistedAgentMessage = {
					role: "user",
					content: qText,
					display_text: qText,
					timestamp: Date.now(),
				} as PersistedAgentMessage;
				appendMessages(d, [msg]);
				d.updated_at = Math.floor(Date.now() / 1000);
				await this.store.writeSession(data.id, d);
				return d;
			});
			void updated;
			await this.bus.emit(data.id, "fork_resumed", { session_id: data.id, annotation_id: annotationId });
			void this.driveFork(data.id, slide, config, annotationId).catch(async (e) => {
				await this.handleFatal(data.id, e);
			});
			return { sessionId: data.id, streamFromSeq };
		}

		// New fork: enforce the active limit (app.py:1726).
		const limit = Math.max(0, Math.floor(config.fork_active_limit ?? 20));
		await this.enforceForkLimit(slide, limit);

		const title = "批注@" + (roi.label || "");
		const data = await this.store.acquire({ slide, kind: "fork", annotationId, title });
		// seed spot_cursor (app.py:1739).
		await this.store.withLock(data.id, async (d) => {
			if (!d) return null;
			const spots = await this.flask.spots(legacySlide(slide), 0).catch(() => ({ changes: [], currentSeq: 0 }));
			d.spot_cursor = spots.currentSeq || 0;
			d.updated_at = Math.floor(Date.now() / 1000);
			await this.store.writeSession(data.id, d);
			return d;
		});

		await this.bus.emit(data.id, "fork_created", { annotation_id: annotationId, title });
		void this.driveFork(data.id, slide, config, annotationId, roi, args.question).catch(async (e) => {
			await this.handleFatal(data.id, e);
		});
		return { sessionId: data.id, streamFromSeq: 0 };
	}

	// ----------------------------------------------------------------------- //
	// branch (true fork: full session from an annotation) — POST /branch
	// ----------------------------------------------------------------------- //
	/**
	 * Start or resume a **branch** for an annotation (真 fork：从标注起步的完整会话).
	 *
	 * A branch (kind="branch") is a full session seeded from a spot card: it has
	 * the SAME toolset as a main session (incl. create_annotation), so the model
	 * can navigate / snapshot / annotate starting from the spot. The initial
	 * message is identical to a fork (spot card + bbox-expanded 15% image).
	 *
	 * - Root annotation gone → throws {@link RootAnnotationGone} (→ 410).
	 * - Existing branch for this annotation → resume it (append the question,
	 *   emit `branch_resumed`).
	 * - Otherwise: enforce the branch-active limit (reuses fork_active_limit but
	 *   counts only kind="branch"; archives the oldest non-running branch),
	 *   create a new branch, emit `branch_created`, then run the loop.
	 *
	 * Returns `{sessionId}` immediately.
	 */
	async askBranch(args: RunArgs & { annotationId: string; question?: string }): Promise<{ sessionId: string; streamFromSeq: number }> {
		const { slide, config, annotationId } = args;

		// Locate the root annotation via the spot change log (tombstone-aware).
		const roi = await this.findSpot(slide, annotationId);
		if (!roi || roi.deleted) {
			throw new RootAnnotationGone();
		}

		const existing = await this.store.findBranch(slide, annotationId);

		if (existing) {
			// Resume: acquire, append the question, emit branch_resumed, run.
			const data = await this.store.acquire({ sessionId: existing, slide, kind: "branch", annotationId });
			// SSE 起点水位（同 fork 续聊）：只流本轮新事件，不重放历史。
			const streamFromSeq = data.last_event_seq || 0;
			const qText = args.question || "请谈谈这个区域";
			// Append the user question to the transcript (mirrors fork resume).
			// Goes through the seq-allocating append path (§10).
			const updated = await this.store.withLock(data.id, async (d) => {
				if (!d) return null;
				const msg: PersistedAgentMessage = {
					role: "user",
					content: qText,
					display_text: qText,
					timestamp: Date.now(),
				} as PersistedAgentMessage;
				appendMessages(d, [msg]);
				d.updated_at = Math.floor(Date.now() / 1000);
				await this.store.writeSession(data.id, d);
				return d;
			});
			void updated;
			await this.bus.emit(data.id, "branch_resumed", { session_id: data.id, annotation_id: annotationId });
			void this.driveBranch(data.id, slide, config, annotationId).catch(async (e) => {
				await this.handleFatal(data.id, e);
			});
			return { sessionId: data.id, streamFromSeq };
		}

		// New branch: enforce the active limit (reuses fork_active_limit but
		// counts only kind="branch").
		const limit = Math.max(0, Math.floor(config.fork_active_limit ?? 20));
		await this.enforceBranchLimit(slide, limit);

		const title = "批注深读@" + (roi.label || "");
		const data = await this.store.acquire({ slide, kind: "branch", annotationId, title });
		// seed spot_cursor (same as fork).
		await this.store.withLock(data.id, async (d) => {
			if (!d) return null;
			const spots = await this.flask.spots(legacySlide(slide), 0).catch(() => ({ changes: [], currentSeq: 0 }));
			d.spot_cursor = spots.currentSeq || 0;
			d.updated_at = Math.floor(Date.now() / 1000);
			await this.store.writeSession(data.id, d);
			return d;
		});

		await this.bus.emit(data.id, "branch_created", { annotation_id: annotationId, title });
		void this.driveBranch(data.id, slide, config, annotationId, roi, args.question).catch(async (e) => {
			await this.handleFatal(data.id, e);
		});
		return { sessionId: data.id, streamFromSeq: 0 };
	}

	// ----------------------------------------------------------------------- //
	// cancel — app.py:1750 api_ai_cancel
	// ----------------------------------------------------------------------- //
	/**
	 * Cancel a running session. Aborts the pi Agent (which signals the streamFn
	 * → "aborted" stop reason) and transitions status to paused once the run
	 * settles. Accepts a sessionId or a slide (resolves the slide's main).
	 */
	async cancel(args: { sessionId?: string; slide?: string }): Promise<{ ok: true }> {
		let sessionId = args.sessionId;
		if (!sessionId) {
			if (!args.slide) throw new SessionConflict("会话不存在");
			const idx = await this.store.listBySlide(args.slide);
			sessionId = idx.main || undefined;
			if (!sessionId) throw new SessionConflict("会话不存在");
		}
		const data = await this.store.readSession(sessionId);
		if (!data) throw new SessionConflict("会话不存在");

		const agent = this.activeAgents.get(sessionId);
		if (agent) {
			agent.abort();
			// The loop's settle path transitions status. If there is no active
			// agent (e.g. crash residue), flip to paused directly.
		} else if (data.status === "running") {
			await this.store.setStatus(sessionId, "paused");
		}
		return { ok: true };
	}

	// =========================================================================== //
	// Main loop driver
	// =========================================================================== //
	/**
	 * Drive a main session: emit setup event, build initial context, run the
	 * agent, then settle status. Mirrors app.py:1957 `worker`.
	 */
	private async driveMain(sessionId: string, slide: string, config: RunConfig, task: string, resumed: boolean): Promise<void> {
		const slideInfo = await this.fetchSlideInfo(slide);
		const data = await this.store.readSession(sessionId);
		if (!data) return;

		let initialMessages: PersistedAgentMessage[];

		if (!resumed) {
			// Fresh: build the user task message, persist it, emit slide_opened
			// with the overview viewport (app.py:1960-1980).
			const vp = 1024;
			const lvl = AgentState.pickOverviewLevel(slideInfo.width, slideInfo.height, slideInfo.levelDownsamples, vp);
			const st = new AgentState(slideInfo.width / 2.0, slideInfo.height / 2.0, vp, lvl, slideInfo.mpp);
			const userMsg = makeMainMessages({ slideName: slide, task, info: slideInfo }) as unknown as PersistedAgentMessage;
			// Inject spot changes since cursor 0 (app.py:1969). injectSpotChanges
			// appends + persists the spot messages itself; we prepend the task
			// user message and persist the full initial transcript.
			const spotMsgs = await this.injectSpotChanges(sessionId, slide);
			initialMessages = [userMsg, ...spotMsgs];

			// Persist the initial transcript + agent_state.
			await this.store.withLock(sessionId, async (d) => {
				if (!d) return null;
				// injectSpotChanges already appended spotMsgs to d.messages;
				// rebuild as [userMsg, ...spotMsgs] (drop any prior residue).
				// Preserved seqs (spotMsgs already stamped) are kept; userMsg gets
				// a fresh seq via the replace-with-preserve path (§10).
				replaceMessagesPreservingSeq(d, [...initialMessages]);
				d.agent_state = st.toDict();
				d.updated_at = Math.floor(Date.now() / 1000);
				await this.store.writeSession(sessionId, d);
				return d;
			});

			const bbox = st.viewportBbox(slideInfo.levelDownsamples, { width: slideInfo.width, height: slideInfo.height });
			await this.bus.emit(sessionId, "slide_opened", {
				slide,
				width: slideInfo.width,
				height: slideInfo.height,
				overview_level: lvl,
				level_count: slideInfo.levelDownsamples.length,
				mpp: slideInfo.mpp,
				viewport: bbox,
				session_id: sessionId,
			});
		} else {
			// Continue: refresh system prompt (no-op: pi keeps it on state),
			// inject spot changes (appends + persists internally), emit
			// session_resumed (app.py:1981-1994).
			await this.injectSpotChanges(sessionId, slide);
			const after = await this.store.readSession(sessionId);
			initialMessages = (after?.messages || []) as PersistedAgentMessage[];
			await this.bus.emit(sessionId, "session_resumed", {
				session_id: sessionId,
				status: after?.status ?? "running",
			});
		}

		await this.runAgentLoop(sessionId, slide, config, slideInfo, initialMessages, resumed);
	}

	/**
	 * Drive a fork session: emit fork_resumed (already emitted by askFork for
	 * new forks via fork_created), build/continue the context, run the loop.
	 * Mirrors app.py:2017 `worker`.
	 */
	private async driveFork(
		sessionId: string,
		slide: string,
		config: RunConfig,
		annotationId: string,
		roi?: RoiDict,
		question?: string,
	): Promise<void> {
		const slideInfo = await this.fetchSlideInfo(slide);
		const data = await this.store.readSession(sessionId);
		if (!data) return;

		let initialMessages: PersistedAgentMessage[];

		if (data.messages.length === 0) {
			// Brand-new fork: build the spot card + image (app.py:1731-1741).
			if (!roi) {
				roi = (await this.findSpot(slide, annotationId)) || undefined;
			}
			const spot: SpotDict = roi || { annotation_id: annotationId };
			const { imageRef, imageB64 } = await this.forkSpotImageRef(slide, slideInfo, spot);
			const userMsg = makeForkMessages({
				slideName: slide,
				info: slideInfo,
				spot,
				question: question || "",
				imageRef,
				imageB64,
			}) as unknown as PersistedAgentMessage;
			initialMessages = [userMsg];
			await this.store.withLock(sessionId, async (d) => {
				if (!d) return null;
				// Fresh fork: seed the only message with a fresh seq (§10).
				replaceMessagesPreservingSeq(d, [...initialMessages]);
				d.updated_at = Math.floor(Date.now() / 1000);
				await this.store.writeSession(sessionId, d);
				return d;
			});
		} else {
			// Resumed fork: inject spot changes (app.py:2021). injectSpotChanges
			// appends + persists internally; re-read the session for the full
			// transcript (avoid double-appending the spot messages).
			await this.injectSpotChanges(sessionId, slide);
			const after = await this.store.readSession(sessionId);
			initialMessages = (after?.messages || []) as PersistedAgentMessage[];
			// fork_resumed was already emitted by askFork; nothing to do here.
		}

		await this.runAgentLoop(sessionId, slide, config, slideInfo, initialMessages, false, {
			kind: "fork",
			systemPrompt: FORK_LITE_SYSTEM_PROMPT,
		});
	}

	// ----------------------------------------------------------------------- //
	// branch (true fork: full session from an annotation) — POST /branch
	// ----------------------------------------------------------------------- //
	/**
	 * Drive a branch session: same initial context shape as a fork (spot card +
	 * bbox-expanded image) but with the FULL toolset (incl. create_annotation),
	 * so the model can navigate / snapshot / annotate starting from the spot.
	 * Mirrors driveFork but passes kind="branch" (full tools + SYSTEM_PROMPT).
	 */
	private async driveBranch(
		sessionId: string,
		slide: string,
		config: RunConfig,
		annotationId: string,
		roi?: RoiDict,
		question?: string,
	): Promise<void> {
		const slideInfo = await this.fetchSlideInfo(slide);
		const data = await this.store.readSession(sessionId);
		if (!data) return;

		let initialMessages: PersistedAgentMessage[];

		if (data.messages.length === 0) {
			// Brand-new branch: build the spot card + image (same shape as fork).
			if (!roi) {
				roi = (await this.findSpot(slide, annotationId)) || undefined;
			}
			const spot: SpotDict = roi || { annotation_id: annotationId };
			const { imageRef, imageB64 } = await this.forkSpotImageRef(slide, slideInfo, spot);
			const userMsg = makeForkMessages({
				slideName: slide,
				info: slideInfo,
				spot,
				question: question || "",
				imageRef,
				imageB64,
			}) as unknown as PersistedAgentMessage;
			initialMessages = [userMsg];
			await this.store.withLock(sessionId, async (d) => {
				if (!d) return null;
				// Fresh branch: seed the only message with a fresh seq (§10).
				replaceMessagesPreservingSeq(d, [...initialMessages]);
				d.updated_at = Math.floor(Date.now() / 1000);
				await this.store.writeSession(sessionId, d);
				return d;
			});
		} else {
			// Resumed branch: inject spot changes (same as fork/main resume).
			await this.injectSpotChanges(sessionId, slide);
			const after = await this.store.readSession(sessionId);
			initialMessages = (after?.messages || []) as PersistedAgentMessage[];
			// branch_resumed was already emitted by askBranch; nothing to do here.
		}

		// Full toolset + full SYSTEM_PROMPT (branch == main toolset, seeded from spot).
		await this.runAgentLoop(sessionId, slide, config, slideInfo, initialMessages, false, {
			kind: "branch",
			systemPrompt: SYSTEM_PROMPT,
		});
	}
	/**
	 * Build a pi Agent, wire event mapping + run-level guards, and run to
	 * completion. Settles status (finished/error/paused) at the end so the SSE
	 * layer emits session_ended.
	 */
	private async runAgentLoop(
		sessionId: string,
		slide: string,
		config: RunConfig,
		slideInfo: SlideInfo,
		initialMessages: PersistedAgentMessage[],
		_continued: boolean,
		loopOptions: { systemPrompt?: string; kind?: "main" | "fork" | "branch" } = {},
	): Promise<void> {
		const { models, model } = buildModel(config);
		const maxSteps = Math.max(1, Math.floor(config.max_steps ?? 50));

		// Resolve the effective kind + system prompt for this run. Defaults
		// mirror the legacy behavior (main / branch = full SYSTEM_PROMPT + full
		// tools; fork = lite prompt + no tools). The caller (askFork) overrides
		// for lite forks; main/branch drive this from the session's persisted kind.
		const sessionForKind = await this.store.readSession(sessionId);
		const kind: "main" | "fork" | "branch" =
			loopOptions.kind ?? (sessionForKind?.kind === "fork" ? "fork" : sessionForKind?.kind === "branch" ? "branch" : "main");
		const systemPrompt = loopOptions.systemPrompt ?? (kind === "fork" ? FORK_LITE_SYSTEM_PROMPT : SYSTEM_PROMPT);

		// Compaction + transform settings, resolved once per run.
		const compactionSettings = resolveCompactionSettings(config);
		const transformSettings = resolveTransformSettings(config);

		// Session-level mutable: the first snapshot's toolCallId, used by
		// transformContext to protect the whole-slide overview from eviction.
		// Set when the first snapshot_captured event fires for this run.
		const firstSnapshotToolCallIdRef = { value: <string | null>null };
		// Session-level mutable: the current pending snapshot id, so transformContext
		// can prioritize the pending snapshot over ordinary recent images (§15.1).
		// Updated on snapshot_captured (set) and snapshot_reviewed (cleared).
		const pendingSnapshotIdRef = { value: <string | null>null };

		// Tools + tool context (tools.ts). emit routes domain events to the bus.
		// fork (lite) registers NO tools — the model does pure text Q&A. main
		// and branch get the full toolset (createTools returns [] for fork as a
		// defensive fallback, but we skip building the tool context entirely
		// for forks so no domain events can fire).
		const toolCtx: ToolContext = {
			sessionStore: this.store,
			sessionId,
			kind,
			slide,
			slideInfo,
			flask: this.flask,
			emit: (type, payload) => {
				// Fire-and-forget; emit is async but tools need not await each.
				// Track the first snapshot so transformContext can protect it.
				if (type === "snapshot_captured") {
					const sid = (payload as { snapshot_id?: string } | undefined)?.snapshot_id;
					if (sid) {
						if (firstSnapshotToolCallIdRef.value === null) firstSnapshotToolCallIdRef.value = sid;
						pendingSnapshotIdRef.value = sid;
					}
				} else if (type === "snapshot_reviewed") {
					pendingSnapshotIdRef.value = null;
				}
				void this.bus.emit(sessionId, type, payload);
			},
			onFingerprintMismatch: () => {
				this.invalidateSlideCaches(slide);
			},
			cfg: {
				...(config as unknown as Record<string, unknown>),
				// Snapshot output is capped at the resolved detail tier so live
				// images cannot exceed the budget estimator's detail square.
				detail_image_long_edge: transformSettings.detailImageLongEdge,
			},
		};
		const tools = kind === "fork" ? [] : createTools(toolCtx);

		// Phase 2b: capture the run config so helpers can resolve settings.
		this.activeRunConfig = config;

		// Phase 2b: build the checkpoint env + ensure a checkpoint exists (with
		// overview back-fill). Runs once at the start of the loop. Best-effort:
		// failures (e.g. concurrent run) are logged and the loop continues; the
		// assembler falls back to the Phase 1 path when no checkpoint exists.
		const checkpointEnv = this.buildCheckpointEnv(systemPrompt, tools, slideInfo);
		try {
			await this.ensureCheckpointRun({
				sessionId,
				slide,
				slideInfo,
				systemPrompt,
				tools,
				firstSnapshotToolCallIdRef,
			});
		} catch (e) {
			console.warn(`[checkpoint] ensureCheckpointRun failed for ${sessionId}: ${(e as Error)?.message || e}`);
		}

		// Session snapshot getter for the assembler (§7.2: reads once per request
		// OUTSIDE the session lock). The assembler calls this at the start of
		// each transform.
		const getSessionSnapshot = async (): Promise<AssemblerSessionSnapshot> => {
			const d = await this.store.readSession(sessionId);
			return {
				checkpoint: (d?.context_checkpoint as ContextCheckpoint | undefined) ?? null,
				observations: d?.observations || [],
				pendingSnapshotId: pendingSnapshotIdRef.value,
				messages: (d?.messages || []) as PersistedAgentMessage[],
			};
		};

		// Overview src resolver: looks up the ref's bbox in the canonical
		// messages so the assembler can materialize the stable overview.
		const overviewSrcResolver = (refId: string): { x: number; y: number; w: number; h: number } | null => {
			// Read from the latest snapshot (best-effort; the ref should be stable
			// across the generation). We walk the live snapshot synchronously is
			// not possible (async), so we cache the last-read messages in a ref.
			const msgs = lastMessagesRef.value;
			for (const m of msgs) {
				const content = (m as { content?: unknown }).content;
				if (!Array.isArray(content)) continue;
				for (const part of content) {
					if (part && typeof part === "object" && (part as { type?: string }).type === "image_ref" && (part as { ref_id?: string }).ref_id === refId) {
						const s = (part as { src?: { x?: number; y?: number; w?: number; h?: number } }).src || {};
						return { x: Number(s.x ?? 0), y: Number(s.y ?? 0), w: Number(s.w ?? 0), h: Number(s.h ?? 0) };
					}
				}
			}
			return null;
		};
		const lastMessagesRef = { value: <PersistedAgentMessage[]>[] };
		// Wrap getSessionSnapshot to refresh lastMessagesRef on each read.
		const getSessionSnapshotWithCache = async (): Promise<AssemblerSessionSnapshot> => {
			const snap = await getSessionSnapshot();
			lastMessagesRef.value = snap.messages;
			return snap;
		};

		// Phase 2b assembler (replaces the Phase 1 transformContext hook). The
		// hook signature is identical so pi's agent loop is unaware of the
		// change. Internally falls back to the Phase 1 path when the session
		// has no checkpoint yet — always go through the assembler so metrics
		// (incl. request-local visual_budget_overflow_tokens) are captured.
		const lastAssemblerMetrics = { value: null as AssemblerMetrics | null };
		// Kept only as the StableContextUnavailable safe-fallback (Phase 1
		// contract: the transform hook MUST NOT throw to pi).
		const phase1Transform = makeTransformContext({
			flask: this.flask,
			slide,
			slideInfo,
			settings: transformSettings,
			firstSnapshotToolCallIdRef,
			pendingSnapshotIdRef,
		});
		const assemblerTransform = makeRequestAssembler({
			flask: this.flask,
			slide,
			slideInfo,
			settings: transformSettings,
			systemPrompt,
			toolSchemaHash: checkpointEnv.tool_schema_hash,
			firstSnapshotToolCallIdRef,
			checkpointEnv,
			getSessionSnapshot: getSessionSnapshotWithCache,
			overviewSrcResolver,
			metricsSink: (m) => {
				lastAssemblerMetrics.value = m;
			},
		});

		// Shared flag for §3.2/§13: the assembler cannot throw out of the
		// transformContext hook (Phase 1 contract: MUST NOT throw). When it
		// encounters StableContextUnavailableError, it captures a safe fallback
		// and sets this flag; the streamFn wrapper detects it and applies the
		// shared retry budget (§3.2: "与瞬时错误共用最多 3 次总重试预算").
		const stableContextError = { value: null as StableContextUnavailableError | null };
		// P1-1: capture the most recent transformContext INPUT messages so the
		// streamFn retry wrapper can RE-RUN the assembler on the same input when
		// StableContextUnavailable fires. pi does NOT re-call transformContext
		// inside the wrapper's retry loop (the wrapper short-circuits pi's
		// transform→stream sequence), so without this the retry would re-send the
		// already-degraded fallback instead of re-attempting the stable overview.
		const lastTransformInput = { value: <AgentMessage[]>[] };

		const transformContext = async (messages: AgentMessage[], signal?: AbortSignal): Promise<AgentMessage[]> => {
			// Clear request-local metrics before every assembly so a fallback
			// path (assembler catch / stable-context retry exhausted) cannot
			// inherit the previous request's overflow into PreparedRequest.
			lastAssemblerMetrics.value = null;
			// Record the input so the retry wrapper can re-attempt assembly on
			// StableContextUnavailable (P1-1).
			lastTransformInput.value = messages;
			try {
				return await assemblerTransform(messages, signal);
			} catch (e) {
				if (e instanceof StableContextUnavailableError) {
					// Record for the streamFn wrapper; return a safe fallback
					// so pi does not see a throw. The wrapper retries.
					stableContextError.value = e;
					return phase1Transform(messages, signal);
				}
				throw e;
			}
		};

		/**
		 * Run a compaction pass against the agent's current messages, apply the
		 * result in place (compactionSummary + retained tail + spot-index),
		 * emit session_compacted, and persist. Used by both the turn_end
		 * threshold path and the context_length_exceeded fallback. Returns the
		 * new message list on success, or null if compaction was a no-op or
		 * failed (the fallback treats null as "give up").
		 */
		const runCompactionPass = async (reason?: string): Promise<AgentMessage[] | null> => {
			const data = await this.store.readSession(sessionId);
			if (!data) return null;
			const prev = prevCompactionInputs(data);
			const msgs = agent.state.messages.slice();
			const outcome = await runCompaction({
				messages: msgs,
				settings: compactionSettings,
				models: (this.overrides.compactionModels as never) ?? models,
				model,
				prevSummary: prev.summary,
				prevTokensBefore: prev.tokensBefore,
			});
			if (!outcome) return null;

			// Append a spot-index user message after the summary + retained tail
			// (ai_session.py:954 _inject_spot_index), updating spot_cursor.
			let finalMessages = outcome.messages.slice();
			const spot = await buildSpotIndexMessage(this.flask, slide);
			if (spot) {
				finalMessages = [...finalMessages, spot.message];
			}
			// Apply to the agent's message state (replace in place).
			(agent.state as { messages: unknown[] }).messages = finalMessages;
			// Persist + emit session_compacted.
			await persistCompaction(this.store, sessionId, outcome, finalMessages, reason);
			await this.store.withLock(sessionId, async (d) => {
				if (!d) return null;
				if (spot) d.spot_cursor = spot.newCursor;
				d.updated_at = Math.floor(Date.now() / 1000);
				await this.store.writeSession(sessionId, d);
				return d;
			});

			// Phase 2b (§5.3): rebuild the checkpoint after a successful
			// compaction. summary = compaction outcome summary, through_seq =
			// highest seq on the post-compaction list, generation+1, overview
			// carried over. CAS-committed atomically; concurrent bump / disk
			// error leaves the prior generation intact (§5.3).
			try {
				const post = await this.store.readSession(sessionId);
				const prevCp = (post?.context_checkpoint as ContextCheckpoint | undefined) ?? null;
				if (prevCp) {
					const candidate = buildPostCompactionCheckpoint({
						prev: prevCp,
						outcome,
						postMessages: (post?.messages || []) as PersistedAgentMessage[],
						observations: post?.observations || [],
						systemPrompt,
					});
					if (candidate) {
						await this.store.commitCheckpoint(
							sessionId,
							prevCp.generation,
							prevCp.slide_fingerprint,
							(d) => {
								d.context_checkpoint = candidate;
							},
						);
					}
				}
			} catch (e) {
				console.warn(`[checkpoint] post-compaction rebuild failed for ${sessionId}: ${(e as Error)?.message || e}`);
			}

			await this.bus.emit(sessionId, "session_compacted", {
				tokens_before: outcome.tokensBefore,
				tokens_after: outcome.tokensAfter,
				...(reason ? { reason } : {}),
			});
			return finalMessages;
		};

		// Phase 3 (§8.1): resolve prompt-cache capabilities ONCE per run. The
		// capabilities object is mutated in place by the retry wrapper when a
		// provider rejects a cache field (§13 downgrade), so subsequent requests
		// in the same run stop emitting the rejected field. Resolution does NOT
		// infer capability from the upstream model name (§8.1); explicit mode is
		// optimistic and runtime-validated.
		const promptCacheCapabilities: PromptCacheCapabilities = resolvePromptCacheCapabilities(
			config.prompt_cache_mode,
			{ apiProtocol: config.api_protocol ?? "openai" },
		);

		// StreamFn with transient-error retry + context_length_exceeded fallback
		// (ai_agent.py:582-608). The fallback force-compacts then retries once.
		// transformContext is passed in so the in-wrapper retry (which bypasses
		// pi's per-request transform) still strips image_ref blocks. The fatal
		// "second context-exceeded" case is detected in the message_end handler
		// (pi surfaces the streamFn's terminal error there), not here.
		//
		// Phase 2b: the wrapper also builds a {@link PreparedRequest} per logical
		// call (reused on transient retry, released on force-compaction), and
		// handles {@link StableContextUnavailableError} via the shared retry
		// budget (§3.2/§13).
		//
		// Phase 3 (§13): the wrapper also injects the explicit prompt_cache_key
		// via options.samplingParams, detects provider rejection of the cache
		// field, and retries once WITHOUT the field (downgrade). This downgrade
		// retry does NOT consume the transient 3-attempt budget.
		const stepRef = { current: -1 };
		const streamFn = this.makeRetryingStreamFn(
			sessionId,
			config,
			stepRef,
			runCompactionPass,
			transformContext,
			systemPrompt,
			tools,
			stableContextError,
			lastTransformInput,
			promptCacheCapabilities,
			lastAssemblerMetrics,
		);

		// Run-state machine for event mapping.
		const runState: RunState = {
			turnCount: 0,
			finished: false,
			paused: false,
			errored: false,
			lastAssistant: null,
			hitMaxSteps: false,
			abortRequested: false,
		};

		// max_steps: when reached, emit agent_paused (ai_agent.py:696-698) and
		// stop. The flag ensures agent_end below does not also emit
		// agent_finished.
		const emitMaxStepsPause = async (): Promise<void> => {
			runState.hitMaxSteps = true;
			runState.paused = true;
			await this.bus.emit(sessionId, "agent_paused", {
				summary: "已达步数上限",
				can_continue: true,
			});
		};

		const agent = new Agent({
			streamFn: streamFn as Agent["streamFunction"],
			transformContext,
			getApiKey: () => config.api_key,
			initialState: {
				model: model as never,
				systemPrompt,
				tools,
				messages: initialMessages as never[],
			},
			// shouldStopAfterTurn: enforce max_steps (ai_agent.py:696-698).
			shouldStopAfterTurn: async () => {
				if (runState.turnCount >= maxSteps) {
					await emitMaxStepsPause();
					return true;
				}
				return false;
			},
		});

		this.activeAgents.set(sessionId, agent);

		// Pending-snapshot plain-text guard (ai_agent.py:650-655):
		// when a plain-text turn ends with a pending snapshot, push a nudge
		// onto the agent's followUp queue. pi's loop drains follow-ups after
		// the agent would otherwise stop (agent-loop.js:162-168), continuing
		// the loop with the nudge as a new user turn — exactly mirroring
		// Python's `append user msg + continue`.

		/**
		 * Threshold compaction check (ai_session.py:908 maybe_compact). Called at
		 * turn_end: estimate context tokens off the agent's current messages
		 * (pi's usage+trailing estimator, fixing the old Python one-turn lag) and
		 * compact when over `context_window - reserve_tokens`.
		 *
		 * Only fires when the turn did not already settle into a terminal/paused
		 * state (no point compacting a run that's about to stop).
		 */
		const maybeCompact = async (): Promise<void> => {
			if (runState.finished || runState.paused || runState.errored || runState.hitMaxSteps) return;
			// §9.1: include the estimated selected visual tokens in the trigger
			// estimate. We do not have the exact post-selection image set at this
			// point (selection happens inside transformContext), so we apply the
			// conservative reserve = visual_context_budget_tokens (§9.1 rule 3)
			// when image_refs are present in the messages. The working-set cap
			// (visual_working_set_max) is enforced separately by the image selector.
			const msgs = agent.state.messages.slice();
			// §9.1: reserve the visual budget when the request carries ANY image
			// payload — both dehydrated image_ref blocks AND live `image` blocks
			// (a snapshot taken mid-run is a live image until settle dehydrates
			// it). P2-3: the original check only saw image_ref, so a run that
			// only carried live images would under-reserve and miss compaction.
			const hasImageRefs = msgs.some((m) => {
				const c = (m as { content?: unknown }).content;
				if (!Array.isArray(c)) return false;
				return c.some((p) => p && (isImageRefContent(p) || isImageContent(p)));
			});
			const visualBudget = Number(config.visual_context_budget_tokens);
			const visualReserve = hasImageRefs
				? (Number.isFinite(visualBudget) && visualBudget > 0 ? visualBudget : DEFAULT_VISUAL_CONTEXT_BUDGET_TOKENS)
				: 0;
			const check = checkShouldCompact(msgs, compactionSettings, { visualContextBudgetReserve: visualReserve });
			if (!check.should) return;
			// Compact failure is non-fatal: log + continue with the un-compacted
			// context (no session_compacted event emitted).
			try {
				await runCompactionPass();
			} catch (e) {
				console.warn(`[compaction] threshold compact failed for ${sessionId}: ${(e as Error)?.message || e}`);
			}
		};

		const unsubscribe = agent.subscribe(async (event: AgentEvent) => {
			await this.handleAgentEvent(sessionId, event, runState, stepRef, agent, maybeCompact);
		});

		try {
			// Fresh run: prompt with the initial user message (already on state
			// via initialState.messages, so use continue() to avoid re-adding).
			// pi requires prompt() to add a new message; since we seeded
			// initialState.messages, we use continue() for the first turn too.
			await agent.continue();
			await agent.waitForIdle();
		} catch (e) {
			runState.errored = true;
			await this.bus.emit(sessionId, "agent_error", {
				error: `读片助手异常：${(e as Error)?.message || String(e)}`,
			});
		} finally {
			unsubscribe();
			this.activeAgents.delete(sessionId);
		}

		// Persist the final transcript + settle status.
		await this.settleRun(sessionId, runState, agent);
	}

	// =========================================================================== //
	// Agent event → SSE event mapping
	// =========================================================================== //
	/**
	 * Subscribe callback: map one pi AgentEvent to SSE events + run-state
	 * updates. Async to coexist with the agent's await-settling contract.
	 */
	private async handleAgentEvent(
		sessionId: string,
		event: AgentEvent,
		runState: RunState,
		stepRef: { current: number },
		agent: Agent,
		maybeCompact: () => Promise<void>,
	): Promise<void> {
		switch (event.type) {
			case "turn_start": {
				runState.turnCount += 1;
				stepRef.current = runState.turnCount - 1; // 0-based like Python
				await this.bus.emit(sessionId, "agent_thinking", { step: stepRef.current });
				break;
			}
			case "message_update": {
				if (event.assistantMessageEvent.type === "text_delta") {
					await this.bus.emit(sessionId, "text_delta", { text: event.assistantMessageEvent.delta });
				}
				break;
			}
			case "message_end": {
				if (event.message.role === "assistant") {
					const msg = event.message as AssistantMessage;
					runState.lastAssistant = msg;
					// Fatal post-compact context-exceeded (ai_agent.py:594-596):
					// if the streamFn already force-compacted once (recorded in
					// compaction_entries) and the model STILL returns a context-
					// length error, the run cannot recover → agent_error. Detected
					// here (not in the streamFn wrapper) because pi surfaces the
					// streamFn's terminal error as an assistant message_end whose
					// stopReason is "error".
					if (msg.stopReason === "error" && isContextExceeded(msg.errorMessage || "")) {
						const data = await this.store.readSession(sessionId);
						const alreadyCompacted = (data?.compaction_entries || []).length > 0;
						if (alreadyCompacted && !runState.errored) {
							runState.errored = true;
							await this.bus.emit(sessionId, "agent_error", {
								error: `调用模型失败：${msg.errorMessage || "context_length_exceeded"}`,
								step: stepRef.current,
							});
						}
					}
					// Generic terminal model error (non-context-exceeded). The
					// retry streamFn already exhausted its transient/cache-field/
					// force-compact recovery budget, so a stopReason "error"
					// reaching here is terminal. Without this, agent_end would
					// MASK it as agent_finished: a dropped finish_reason / a 4xx
					// the gateway kept returning / a parsing failure would settle
					// the session as "finished" with no error event and no usage
					// — exactly the Phase 4 symptom that made this path look like
					// a successful (but tool-truncated) run. Surface it as
					// agent_error so the run settles "error" and the failure is
					// observable. (Length/max_tokens is handled separately below.)
					if (
						msg.stopReason === "error" &&
						!isContextExceeded(msg.errorMessage || "") &&
						!runState.errored &&
						!runState.finished &&
						!runState.paused
					) {
						runState.errored = true;
						await this.bus.emit(sessionId, "agent_error", {
							error: `调用模型失败：${msg.errorMessage || "未知错误"}`,
							step: stepRef.current,
						});
					}
					// length → paused (ai_agent.py:637-646). pi already fails the
					// (possibly truncated) tool calls; we just pause.
					if (msg.stopReason === "length") {
						const tip =
							"模型输出被截断（达到 max_tokens）" +
							(msg.content.some((c) => c.type === "toolCall") ? "，工具调用可能不完整" : "") +
							"，可继续生成或提高 max_tokens";
						await this.bus.emit(sessionId, "agent_paused", {
							summary: tip,
							can_continue: true,
							reason: "max_tokens",
						});
						runState.paused = true;
					}
					// Record usage (ai_agent.py:619-623).
					await this.recordUsage(sessionId, msg.usage);
				}
				break;
			}
			case "turn_end": {
				// Plain-text end with a pending snapshot → enqueue the nudge
				// (ai_agent.py:650-655). pi drains the followUp queue after the
				// agent would otherwise stop, continuing the loop with the
				// nudge — exactly mirroring Python's `append user msg + continue`.
				const msg = event.message as AssistantMessage;
				const hasToolCalls = msg.content.some((c) => c.type === "toolCall");
				if (!hasToolCalls && !runState.paused && !runState.finished && !runState.hitMaxSteps) {
					const pending = await this.isSnapshotPending(sessionId);
					if (pending) {
						const nudge: PersistedAgentMessage = {
							role: "user",
							content:
								"当前还有未消化的快照，请先调用 complete_snapshot_review 关闭后再继续。",
							timestamp: Date.now(),
						} as PersistedAgentMessage;
						agent.followUp(nudge as never);
					}
				}
				// finish tool: the tool sets terminate:true → loop exits. We
				// detect it here so agent_end does not also emit agent_finished.
				if (hasToolCalls) {
					for (const tc of msg.content) {
						if (tc.type === "toolCall" && tc.name === "finish") {
							runState.finished = true;
							const summary = (tc.arguments as { summary?: string })?.summary || "(无总结)";
							await this.bus.emit(sessionId, "agent_finished", { summary });
							break;
						}
					}
				}
				// Threshold compaction (ai_session.py:908 maybe_compact). Runs
				// after the turn fully settles (usage recorded, finish detected).
				// No-op when the turn ended the run or compaction isn't needed.
				await maybeCompact();
				break;
			}
			case "agent_end": {
				// User abort → paused (ai_agent.py:471-477 _pause_cancelled).
				// Detected: the last assistant message has stopReason "aborted".
				if (
					!runState.finished &&
					!runState.paused &&
					!runState.errored &&
					runState.lastAssistant?.stopReason === "aborted"
				) {
					await this.bus.emit(sessionId, "agent_paused", {
						summary: "已停止",
						can_continue: true,
					});
					runState.paused = true;
					break;
				}
				// Plain-text stop → agent_finished (ai_agent.py:656-657).
				// max_steps pauses and length pauses were already emitted.
				if (!runState.finished && !runState.paused && !runState.errored && !runState.hitMaxSteps) {
					const text = runState.lastAssistant
						? runState.lastAssistant.content
								.filter((c): c is { type: "text"; text: string } => c.type === "text")
								.map((c) => c.text)
								.join("")
						: "";
					await this.bus.emit(sessionId, "agent_finished", { summary: text || "(无总结)" });
					runState.finished = true;
				}
				break;
			}
		}
	}

	// =========================================================================== //
	// Settle: persist transcript + transition status
	// =========================================================================== //
	private async settleRun(sessionId: string, runState: RunState, agent: Agent): Promise<void> {
		// Persist the agent's transcript, dehydrating image blocks.
		const msgs = agent.state.messages as unknown as PersistedAgentMessage[];
		const imageMeta = collectImageMeta(msgs);
		const dehydrated = dehydrateMessages(msgs, imageMeta);

		let nextStatus: "finished" | "error" | "paused";
		if (runState.errored) {
			nextStatus = "error";
		} else if (runState.paused) {
			nextStatus = "paused";
		} else if (runState.finished) {
			nextStatus = "finished";
		} else {
			// Defensive fallback: loop exited without an explicit terminal
			// event. Pause so the user can continue.
			nextStatus = "paused";
		}

		await this.store.withLock(sessionId, async (d) => {
			if (!d) return null;
			// Replace the full transcript. Live agent messages carry no
			// _context_meta, so they get fresh monotonic seqs via the
			// replace-with-preserve path (§10): monotonic + never reused.
			replaceMessagesPreservingSeq(d, dehydrated);
			d.updated_at = Math.floor(Date.now() / 1000);
			await this.store.writeSession(sessionId, d);
			return d;
		});
		await this.store.setStatus(sessionId, nextStatus);
	}

	/** Record last_usage on the session for Step 4 compaction triggers. */
	private async recordUsage(sessionId: string, usage: Usage | undefined): Promise<void> {
		if (!usage) return;
		await this.store.withLock(sessionId, async (d) => {
			if (!d) return null;
			(d as SessionData & { last_usage?: Usage }).last_usage = usage;
			await this.store.writeSession(sessionId, d);
			return d;
		});
	}

	/** True if the session currently has a pending_snapshot_review. */
	private async isSnapshotPending(sessionId: string): Promise<boolean> {
		const d = await this.store.readSession(sessionId);
		return !!d?.pending_snapshot_review;
	}

	// =========================================================================== //
	// Retrying streamFn wrapper (ai_agent.py:597-608)
	// =========================================================================== //
	/**
	 * Wrap a real streamFn so:
	 *   - transient errors (SSL/timeout/429/5xx) retry up to 3 times with
	 *     2/4/8s backoff, emitting `agent_retrying` each attempt;
	 *   - context-window errors (ai_agent.py:582-596) trigger a one-shot
	 *     force-compact (skipping the threshold check) then retry the call once
	 *     with the re-materialized messages; a second failure is terminal.
	 *
	 * The wrapper consumes each underlying stream to completion; on a retryable
	 * error it starts a fresh stream. The wrapper itself returns a single
	 * combined AssistantMessageEventStream. Events from the failed first stream
	 * are forwarded (so the UI sees the attempt), then superseded by the retry.
	 */
	private makeRetryingStreamFn(
		sessionId: string,
		config: RunConfig,
		stepRef: { current: number },
		forceCompact: (reason?: string) => Promise<AgentMessage[] | null>,
		transformContext: (messages: AgentMessage[], signal?: AbortSignal) => Promise<AgentMessage[]>,
		systemPrompt: string,
		tools: unknown[],
		stableContextError: { value: StableContextUnavailableError | null },
		lastTransformInput: { value: AgentMessage[] },
		promptCacheCapabilities: PromptCacheCapabilities,
		lastAssemblerMetrics: { value: AssemblerMetrics | null },
	): (model: unknown, context: unknown, options?: unknown) => AssistantMessageEventStream {
		const realStreamFn = (this.overrides.streamFn ??
			// Default: bind the openai-completions streamSimple for the built
			// model. Imported lazily so tests that pass a fake streamFn never
			// touch the real provider module.
			this.defaultStreamFnForConfig(config)) as (
				model: unknown,
				context: unknown,
				options?: unknown,
			) => AssistantMessageEventStream | Promise<AssistantMessageEventStream>;

		const self = this;
		return function (model, context, options) {
			const out = createAssistantMessageEventStream();
			void (async () => {
				const maxTransient = 3;
				let compacted = false; // one-shot context-exceeded guard
				let currentContext = context;
				// Phase 2b: the PreparedRequest for THIS logical call. Built on
				// the first attempt from the (already-transformed) context;
				// reused on transient retry (§8.2); released + rebuilt on
				// force-compaction (§8.2: "force-compaction 换代后释放旧对象").
				let prepared: PreparedRequest | null = null;
				let logicalCallId: string | null = null;
				// Phase 3 (§13): one-shot cache-field downgrade guard. When the
				// provider rejects the prompt_cache_key field, we strip it and
				// retry once WITHOUT consuming the transient budget. This flag
				// prevents a second downgrade retry in the same logical call.
				let cacheDowngraded = false;
				// Phase 3 (§8.1): the EFFECTIVE options for the current attempt.
				// We layer the cache-key samplingParams fragment onto pi's
				// supplied options for explicit mode, and strip it on downgrade.
				let currentOptions = options as Record<string, unknown> | null;

				/**
				 * Build (or rebuild) the PreparedRequest from the current
				 * context. Called once per logical call, and once after a
				 * force-compaction that changed the context.
				 */
				const buildPrepared = async (): Promise<void> => {
					const ctxObj = currentContext as { messages?: AgentMessage[]; systemPrompt?: string; tools?: unknown[] };
					const messages = (ctxObj?.messages || []) as AgentMessage[];
					const snap = await self.store.readSession(sessionId);
					const cpGen = snap?.context_checkpoint?.generation ?? 0;
					const spHash = snap?.context_checkpoint?.stable_prefix_hash ?? "";
					prepared = buildPreparedRequest({
						logicalCallId: logicalCallId || `call-${sessionId}-${Date.now()}`,
						checkpointGeneration: cpGen,
						stablePrefixHash: spHash,
						systemPrompt: ctxObj?.systemPrompt ?? systemPrompt,
						tools: ctxObj?.tools ?? tools,
						messages,
						// Request-local overflow from the most recent assembly
						// (never a process-global counter — concurrent sessions
						// must not clobber each other).
						visualBudgetOverflowTokens: lastAssemblerMetrics.value?.visual_budget_overflow_tokens ?? 0,
					});
					logicalCallId = prepared.logicalCallId;
				};

				/**
				 * Phase 3 (§8.1): resolve the cache-key options for the current
				 * attempt. This is SEPARATE from {@link buildPrepared} so that a
				 * hashing failure in PreparedRequest construction does NOT
				 * suppress cache-key injection. In explicit mode we inject
				 * `prompt_cache_key` via samplingParams; after a runtime
				 * downgrade (§13) the field is dropped.
				 *
				 * Reads the checkpoint generation + slide fingerprint from the
				 * session store (same snapshot buildPrepared uses). Best-effort:
				 * a store read failure leaves currentOptions as the raw options.
				 */
				const applyCacheKeyOptions = async (): Promise<void> => {
						if (promptCacheCapabilities.mode !== "explicit") return;
						// gemini protocol: explicit cache keying is not applicable —
						// Gemini-side caching is IMPLICIT prefix caching observed via
						// usageMetadata.cachedContentTokenCount (pi maps it to
						// usage.cacheRead). Sending prompt_cache_key would just be an
						// unknown body field; skip injection. (Phase 4 probe:
						// CPA-forwarded gemini-3.6-flash-high reported
						// cachedContentTokenCount=0 on repeated identical requests —
						// CPA-UNVERIFIED for cache hits, the metric stays observable.)
						if (config.api_protocol === "gemini") return;
					let cpGen = 0;
					let slideFp = "";
					try {
						const snap = await self.store.readSession(sessionId);
						cpGen = snap?.context_checkpoint?.generation ?? 0;
						slideFp = snap?.context_checkpoint?.slide_fingerprint ?? "";
					} catch {
						// store read failure → no cache key (auto-equivalent).
						return;
					}
					const cacheKey = buildPromptCacheKey({
						sessionId,
						slideFingerprint: slideFp,
						generation: cpGen,
					});
					const cacheKeyParams = buildCacheKeySamplingParams(promptCacheCapabilities, cacheKey);
					if (cacheKeyParams) {
						currentOptions = mergeCacheKeyOptions(options as Record<string, unknown> | null, cacheKeyParams);
					}
				};

				for (let attempt = 0; ; attempt++) {
					// Phase 2b: check the stable-context flag BEFORE attempting
					// the stream. The transformContext hook sets it when the
					// assembler raised StableContextUnavailableError; it shares
					// the same 3-attempt budget as transient errors (§3.2/§13).
					if (stableContextError.value && attempt < maxTransient) {
						const sce = stableContextError.value;
						stableContextError.value = null; // consume
						const delay = 2 ** (attempt + 1); // 2/4/8s
						await self.bus.emit(sessionId, "agent_retrying", {
							step: stepRef.current,
							attempt: attempt + 1,
							max: maxTransient,
							delay,
							reason: `stable_context_unavailable ${attempt + 1}/${maxTransient} (${sce.reason})`,
						});
						await sleep(delay * 1000);
						// P1-1: the wrapper short-circuits pi's transform→stream
						// sequence, so pi does NOT re-call transformContext here.
						// Re-run the assembler ourselves on the SAME input it was
						// last given so the retry actually re-attempts the stable
						// overview (instead of re-sending the degraded fallback).
						// On success: replace currentContext.messages + drop the
						// stale PreparedRequest so it is rebuilt from the fresh
						// context. If the hook sets stableContextError again, the
						// next loop iteration re-enters this branch (up to budget).
						// A non-StableContext error is treated as terminal.
						const inputMsgs = lastTransformInput.value;
						if (inputMsgs.length > 0) {
							try {
								const transformed = await transformContext(inputMsgs);
								// If the re-transform cleared the flag, apply the new
								// context; otherwise leave currentContext as-is and
								// let the loop retry (the degraded fallback stays in
								// place until the budget is exhausted or the overview
								// materializes).
								if (!stableContextError.value) {
									currentContext = { ...(currentContext as object), messages: transformed };
									prepared = null; // rebuild PreparedRequest from the fresh context
								}
							} catch (reErr) {
								// Non-StableContext error during re-transform → terminal.
								const errMsg = `stable_context re-transform failed: ${(reErr as Error)?.message || String(reErr)}`;
								const err = makeErrorAssistant(errMsg);
								out.push({ type: "error", reason: "error", error: err });
								out.end(err);
								return;
							}
						}
						continue; // re-attempt the stream with the re-assembled context
					}
					if (stableContextError.value && attempt >= maxTransient) {
						// Budget exhausted → terminal error (§3.2). Generation is
						// NOT bumped here.
						const sce = stableContextError.value;
						stableContextError.value = null;
						const err = makeErrorAssistant(`stable_context_unavailable: ${sce.reason}`);
						out.push({ type: "error", reason: "error", error: err });
						out.end(err);
						return;
					}

					// Phase 2b: build the PreparedRequest on the first attempt
					// of this logical call. It is reused on transient retry.
					if (!prepared) {
						try {
							await buildPrepared();
						} catch {
							// Hashing failures are non-fatal; we proceed without
							// a PreparedRequest (the request still goes out).
							prepared = null;
						}
					}
					// Phase 3 (§8.1): inject the cache-key options for this
					// attempt. Independent of buildPrepared so a hashing failure
					// does not suppress cache-key injection. On the first
					// attempt of explicit mode this adds prompt_cache_key; after
					// a downgrade the capabilities flag is false so it is a no-op.
					try {
						await applyCacheKeyOptions();
					} catch {
						// best-effort; proceed with whatever options we have
					}
					let stream: AssistantMessageEventStream;
					try {
						// Await: the default streamFn is async (dynamic import of
						// the ESM-only provider module), so this may be a Promise.
						// Phase 3: currentOptions carries the injected cache key
						// (samplingParams) in explicit mode; it is rebuilt on
						// downgrade to strip the field.
						//
						// P2-5 (§8.2): when a PreparedRequest exists, send ITS
						// already-canonicalized messages (a normalized copy with
						// _context_meta stripped once at first-send time) so that
						// transient retries hand the provider the SAME payload
						// object — the "normalize once, reuse on retry" contract.
						// force-compaction / stable-context re-assembly paths set
						// prepared=null, so a freshly-built prepared always
						// reflects the current context.
						const preparedNow = prepared as PreparedRequest | null;
						const sendContext = preparedNow
							? { ...(currentContext as object), messages: preparedNow.context.messages }
							: currentContext;
						stream = await realStreamFn(model, sendContext, currentOptions);
					} catch (e) {
						// streamFn contract says it must not throw, but be defensive.
						out.push({
							type: "error",
							reason: "error",
							error: makeErrorAssistant(String((e as Error)?.message || e)),
						});
						out.end(makeErrorAssistant(String((e as Error)?.message || e)));
						return;
					}
					let finalMessage: AssistantMessage | null = null;
					let eventType: "done" | "error" | null = null;
					let terminalEvent: AssistantMessageEvent | null = null;
					try {
						for await (const ev of stream) {
							if (ev.type === "done") {
								finalMessage = ev.message;
								eventType = "done";
								terminalEvent = ev; // hold back; decide below
							} else if (ev.type === "error") {
								finalMessage = ev.error;
								eventType = "error";
								terminalEvent = ev; // hold back; decide below
							} else {
								// Forward non-terminal events (text_delta, etc.) live so
								// streaming stays responsive.
								out.push(ev);
							}
							// On done/error we stop forwarding further events from this
							// underlying stream; the for-await will end naturally.
						}
					} catch (e) {
						finalMessage = makeErrorAssistant(String((e as Error)?.message || e));
						eventType = "error";
						terminalEvent = { type: "error", reason: "error", error: finalMessage };
					}

					if (eventType === "done") {
						// Phase 2b (§12): emit structured metrics for this request.
						// Best-effort; never fails the request on a metrics error.
						try {
							self.emitRequestMetrics(sessionId, config, prepared, finalMessage, stableContextError.value, promptCacheCapabilities, lastAssemblerMetrics.value);
						} catch {
							// ignore metrics failures
						}
						// Forward the held-back done event, then end.
						if (terminalEvent) out.push(terminalEvent);
						out.end(finalMessage!);
						return;
					}
					if (eventType === "error" && finalMessage) {
						const errMsg = finalMessage.errorMessage || "";
						// Helper to forward the held-back terminal error event then end.
						const forwardTerminalError = (): void => {
							out.push(terminalEvent as AssistantMessageEvent);
							out.end(finalMessage);
						};
						// Context-window exceeded: force-compact once, rebuild the
						// context from the compacted messages, retry once. A second
						// failure (or compact failure) is terminal.
						if (isContextExceeded(errMsg) && !compacted) {
							compacted = true;
							let newMessages: AgentMessage[] | null = null;
							try {
								newMessages = await forceCompact("context_length_exceeded");
							} catch (e) {
								console.warn(`[compaction] force-compact threw for ${sessionId}: ${(e as Error)?.message || e}`);
							}
							if (newMessages) {
								// Re-materialize the context: forceCompact rewrote
								// agent.state.messages in place AND returned the new
								// list. The retry bypasses pi's per-request
								// transformContext, so we run it inline here to keep
								// the image_ref-elimination contract (any image_ref
								// in the compacted tail must become a real image or
								// a text fallback before the LLM sees it).
								const transformed = await transformContext(newMessages).catch(() => newMessages);
								currentContext = { ...(currentContext as object), messages: transformed };
								// Phase 2b (§8.2): force-compaction bumped the
								// checkpoint generation → release the old
								// PreparedRequest and build exactly one new one on
								// the next iteration. logicalCallId is advanced so
								// the new object gets a fresh id.
								prepared = null;
								logicalCallId = null;
								attempt = -1; // next iteration → attempt 0 again
								continue;
							}
							// compact failed → forward the error; the message_end
							// handler treats a context-exceeded error after a
							// compaction as terminal agent_error.
							forwardTerminalError();
							return;
						}
						if (isContextExceeded(errMsg)) {
							// Already compacted once and still over → forward; the
							// message_end handler emits the terminal agent_error
							// (ai_agent.py:594-596).
							forwardTerminalError();
							return;
						}
						// Phase 3 (§13): Provider rejected the cache field. Strip
						// the field and retry once WITHOUT consuming the transient
						// budget. This is a capability downgrade, not a transient
						// error. Only fires when we actually sent the field (explicit
						// mode, not yet downgraded). CPA-UNVERIFIED: the rejection
						// matcher errs on the side of matching (false positive → one
						// extra field-less retry that still succeeds; false negative
						// → terminal error leaked to user).
						if (!cacheDowngraded && isCacheFieldRejection(errMsg)) {
							cacheDowngraded = true;
							// Mutate the run-level capabilities so subsequent
							// requests in this run stop sending the field.
							const downgraded = downgradeCacheKeyCapability(promptCacheCapabilities);
							(promptCacheCapabilities as PromptCacheCapabilities).supportsCacheKey = downgraded.supportsCacheKey;
							(promptCacheCapabilities as PromptCacheCapabilities).mode = downgraded.mode;
							console.warn(
								`[prompt-cache] capability_downgrade for ${sessionId}: provider rejected cache field — ${errMsg.slice(0, 200)}`,
							);
							// Strip the field from the current options and retry
							// immediately (no backoff). The for-loop increment
							// would consume one transient-budget slot, so cancel
							// it: the downgrade retry must NOT eat the §13
							// 3-attempt transient budget.
							currentOptions = stripCacheKeyOptions(currentOptions);
							attempt -= 1;
							continue;
						}
						if (isTransientError(errMsg) && attempt < maxTransient) {
							const delay = 2 ** (attempt + 1); // 2/4/8s
							await self.bus.emit(sessionId, "agent_retrying", {
								step: stepRef.current,
								attempt: attempt + 1,
								max: maxTransient,
								delay,
								reason: `reconnection ${attempt + 1}/${maxTransient} (${delay}s)`,
							});
							await sleep(delay * 1000);
							continue; // retry the model call
						}
						forwardTerminalError();
						return;
					}
					// Stream ended without a terminal event (shouldn't happen).
					out.push({ type: "error", reason: "error", error: makeErrorAssistant("Stream ended without a terminal event") });
					out.end(makeErrorAssistant("Stream ended without a terminal event"));
					return;
				}
			})().catch(() => {
				// Last-resort: ensure the output stream terminates.
				out.end(makeErrorAssistant("retry wrapper failed"));
			});
			return out;
		};
	}

	/** Lazy-import the protocol-matched streamSimple bound to the config. */
	private defaultStreamFnForConfig(_config: RunConfig): (model: unknown, context: unknown, options?: unknown) => Promise<AssistantMessageEventStream> {
		// Dynamic import keeps the provider module out of the test graph when a
		// fake streamFn is supplied. The returned fn dispatches by model.api.
		// StreamFn allows returning a Promise (pi agent-loop awaits it).
		return async (model, context, options) => {
			const m = model as { api?: string };
			if (m?.api === "anthropic-messages") {
				throw new Error("anthropic protocol not yet wired in sidecar streamFn");
			}
			if (m?.api === "google-generative-ai") {
				// Phase 4: gemini protocol via pi-ai's google-generative-ai provider
				// (@google/genai SDK; targets the CPA gateway's gemini-compatible
				// /v1beta endpoint — baseUrl already includes the version path).
				const mod = await this.loadGoogleStream();
				return mod.streamSimple(model as never, context as never, options as never);
			}
			const mod = await this.loadOpenAiStream();
			return mod.streamSimple(model as never, context as never, options as never);
		};
	}

	private googleStreamCache: Promise<{ streamSimple: typeof import("@earendil-works/pi-ai/api/google-generative-ai").streamSimple }> | null = null;
	private loadGoogleStream(): Promise<{ streamSimple: typeof import("@earendil-works/pi-ai/api/google-generative-ai").streamSimple }> {
		if (!this.googleStreamCache) {
			// Same ESM-only subpath constraint as the openai loader below.
			this.googleStreamCache = import("@earendil-works/pi-ai/api/google-generative-ai") as Promise<{
				streamSimple: typeof import("@earendil-works/pi-ai/api/google-generative-ai").streamSimple;
			}>;
		}
		return this.googleStreamCache;
	}

	private openAiStreamCache: Promise<{ streamSimple: typeof import("@earendil-works/pi-ai/api/openai-completions").streamSimple }> | null = null;
	private loadOpenAiStream(): Promise<{ streamSimple: typeof import("@earendil-works/pi-ai/api/openai-completions").streamSimple }> {
		if (!this.openAiStreamCache) {
			// The published pi-ai package marks ./api/* exports as ESM-only
			// ("import" condition, no "require"), so createRequire() fails at
			// runtime with "subpath not defined by exports". Use dynamic import.
			this.openAiStreamCache = import("@earendil-works/pi-ai/api/openai-completions") as Promise<{
				streamSimple: typeof import("@earendil-works/pi-ai/api/openai-completions").streamSimple;
			}>;
		}
		return this.openAiStreamCache!;
	}

	// =========================================================================== //
	// Fatal error handler (uncaught exception in the driver)
	// =========================================================================== //
	private async handleFatal(sessionId: string, e: unknown): Promise<void> {
		const msg = (e as Error)?.message || String(e);
		try {
			await this.bus.emit(sessionId, "agent_error", { error: `读片助手异常：${msg}` });
		} catch {
			// ignore
		}
		await this.store.setStatus(sessionId, "error");
	}

	// =========================================================================== //
	// Spot injection (ai_session.py:985-1024 inject_spot_changes)
	// =========================================================================== //
	/**
	 * Append user messages for spot changes since spot_cursor, updating the
	 * cursor. Returns the appended messages (for fresh-run initial assembly).
	 *
	 * Text format is byte-for-byte aligned with ai_session.py:999-1016.
	 */
	async injectSpotChanges(sessionId: string, slide: string): Promise<PersistedAgentMessage[]> {
		const data = await this.store.readSession(sessionId);
		if (!data) return [];
		const cursor = Math.floor(data.spot_cursor || 0);
		let result: { changes: Record<string, unknown>[]; currentSeq: number };
		try {
			result = await this.flask.spots(legacySlide(slide), cursor);
		} catch {
			return [];
		}
		const changes = result.changes || [];
		if (!changes.length) return [];

		const msgs: PersistedAgentMessage[] = [];
		for (const r of changes) {
			const annotationId = String(r.annotation_id || "");
			if (r.deleted) {
				msgs.push({
					role: "user",
					content: `spot_deleted：标注 (${annotationId}) 已被删除。`,
					spot_deleted: annotationId,
					timestamp: Date.now(),
				} as PersistedAgentMessage);
			} else {
				const s = Math.trunc(Number(r.side_px) || 0);
				const x0 = Number(r.x) || 0;
				const y0 = Number(r.y) || 0;
				const note = String(r.note || "");
				msgs.push({
					role: "user",
					content:
						`spot_updated：已有标注线索（待复核，非诊断事实）——` +
						`位置 level-0 左上角 (${fmt0(x0)},${fmt0(y0)})，边长 ${s}px` +
						`（中心 (${fmt0(x0 + s / 2.0)},${fmt0(y0 + s / 2.0)})；goto 看这里请把视野中心对准中心坐标），` +
						`原标注文案：「${note}」。` +
						`请独立观察后决定采纳、修正或忽略。`,
					spot_updated: annotationId,
					timestamp: Date.now(),
				} as PersistedAgentMessage);
			}
		}

		await this.store.withLock(sessionId, async (d) => {
			if (!d) return null;
			// Append with seq allocation (§10).
			appendMessages(d, msgs);
			d.spot_cursor = result.currentSeq || cursor;
			d.updated_at = Math.floor(Date.now() / 1000);
			await this.store.writeSession(sessionId, d);
			return d;
		});
		return msgs;
	}

	// =========================================================================== //
	// Phase 2b: checkpoint ensure + overview back-fill + assembler wiring
	// =========================================================================== //

	/**
	 * Build a {@link CheckpointEnv} for the current run (§10). The version fields
	 * are derived from the system prompt + tool schema; the slide fingerprint
	 * comes from the (cached) slide info. Used by both {@link ensureCheckpointRun}
	 * and the assembler's staleness check.
	 */
	private buildCheckpointEnv(systemPrompt: string, tools: unknown[], slideInfo: SlideInfo): CheckpointEnv {
		const settings = resolveTransformSettings(this.activeRunConfig ?? ({} as RunConfig));
		return {
			system_prompt_version: computeSystemPromptVersion(systemPrompt),
			tool_schema_hash: computeToolSchemaHash(tools as Parameters<typeof computeToolSchemaHash>[0]),
			request_schema_version: REQUEST_SCHEMA_VERSION,
			slide_fingerprint: slideInfo.fingerprint || "",
			overview_target_long_edge: settings.overviewLongEdge,
			overview_jpeg_quality: settings.jpegQuality,
			overview_overlay_version: settings.overlayVersion,
			overview_resize_algorithm: TRANSFORM_RESIZE_ALGORITHM,
			overview_encoder_id: TRANSFORM_ENCODER_ID,
		};
	}

	/**
	 * Lazily ensure a checkpoint exists for the session, and when the existing
	 * generation-1 checkpoint has no overview derivative, attempt to back-fill
	 * it (§3.2/§7.3): select the overview ref, materialize it once, compute the
	 * content_sha256, and CAS-commit a generation-2 checkpoint with the full
	 * overview_derivative.
	 *
	 * Materialization failure → keep the no-overview generation intact (§3.2:
	 * "物化失败/读取失败不得提交 checkpoint"). This is an explicit degraded
	 * path; the assembler still serves requests without an overview image.
	 *
	 * Stale check (§10): when version fields changed, rebuild a fresh generation
	 * via {@link ensureCheckpoint} (no-overview) and let a later request
	 * re-back-fill.
	 */
	private async ensureCheckpointRun(args: {
		sessionId: string;
		slide: string;
		slideInfo: SlideInfo;
		systemPrompt: string;
		tools: unknown[];
		firstSnapshotToolCallIdRef: { value: string | null };
	}): Promise<void> {
		const { sessionId, slide, slideInfo, systemPrompt, tools } = args;
		const env = this.buildCheckpointEnv(systemPrompt, tools, slideInfo);
		const settings = resolveTransformSettings(this.activeRunConfig ?? ({} as RunConfig));

		// Read once outside the lock (§5.3: candidate computation runs outside).
		const data = await this.store.readSession(sessionId);
		if (!data) return;

		// ensureCheckpoint mutates data in memory; we then commit via CAS.
		const cp = ensureCheckpoint(data, {
			system_prompt_version: env.system_prompt_version,
			tool_schema_hash: env.tool_schema_hash,
			slide_fingerprint: env.slide_fingerprint,
		});

		// Stale check (§10): version fields changed → rebuild from scratch. We
		// drop the stale checkpoint and re-ensure, then CAS-commit. This bumps
		// the generation.
		const staleReason = checkpointStale(cp, env);
		if (staleReason) {
			// Force a fresh no-overview checkpoint. ensureCheckpoint returns the
			// existing one when present, so we null it out first.
			const freshData = { ...data, context_checkpoint: undefined } as SessionData;
			const freshCp = ensureCheckpoint(freshData, {
				system_prompt_version: env.system_prompt_version,
				tool_schema_hash: env.tool_schema_hash,
				slide_fingerprint: env.slide_fingerprint,
			});
			// P1-2 (§8/§10): keep the generation MONOTONIC — bump from the prior
			// generation instead of resetting to 1 (ensureCheckpoint's default).
			// Resetting to 1 would let a higher-generation checkpoint be replaced
			// by a g1 candidate and could re-use a stale prompt-cache key.
			freshCp.generation = cp.generation + 1;
			// freshCp.slide_fingerprint is already env.slide_fingerprint (passed
			// above); the stable_prefix_hash was computed by ensureCheckpoint
			// from that fingerprint, so it stays consistent.
			// P1-2 CAS semantics: the CAS asserts "the on-disk state matches
			// what I read BEFORE the rebuild" — i.e. the OLD generation + OLD
			// fingerprint (cp.*). Passing the new fingerprint would ALWAYS fail
			// when the slide changed (stored still has the old one). expectedGen
			// = cp.generation so a concurrent bump is detected.
			const staleRes = await this.store.commitCheckpoint(
				sessionId,
				cp.generation,
				cp.slide_fingerprint,
				(d) => {
					d.context_checkpoint = freshCp;
				},
			);
			if (!staleRes.ok) {
				// CAS rejected (concurrent bump or the on-disk state already
				// changed) → do NOT backfill against an un-committed in-memory
				// candidate. Another op owns the checkpoint now (§5.3).
				return;
			}
			// P1-2: backfill against the COMMITTED checkpoint (re-read; do not
			// trust the in-memory freshCp, which may now be stale if another op
			// touched the session — though commitCheckpoint held the lock, a
			// re-read is the safe canonical form).
			const committed = await this.store.readSession(sessionId);
			const committedCp = committed?.context_checkpoint ?? freshCp;
			await this.backfillOverview({ sessionId, slide, slideInfo, cp: committedCp, env, settings, systemPrompt, tools, firstSnapshotToolCallIdRef: args.firstSnapshotToolCallIdRef });
			return;
		}

		// Commit the g1 candidate if it is new (ensureCheckpoint may have created
		// it in memory but it is not yet on disk). We detect "new" by reading
		// the on-disk generation; when they differ, CAS-commit.
		const onDisk = await this.store.readSession(sessionId);
		const onDiskGen = onDisk?.context_checkpoint?.generation;
		if (onDiskGen !== cp.generation) {
			const res = await this.store.commitCheckpoint(
				sessionId,
				onDiskGen,
				env.slide_fingerprint,
				(d) => {
					d.context_checkpoint = cp;
				},
			);
			if (!res.ok) {
				// Concurrent bump → another op established the checkpoint; we
				// accept whatever is now on disk and proceed.
				return;
			}
		}

		// Overview back-fill (§3.2): g1 with no overview → try to build g2.
		if (!cp.overview_derivative) {
			await this.backfillOverview({ sessionId, slide, slideInfo, cp, env, settings, systemPrompt, tools, firstSnapshotToolCallIdRef: args.firstSnapshotToolCallIdRef });
		}
	}

	/**
	 * Attempt to back-fill the stable overview derivative (§3.2/§7.3). On
	 * success, CAS-commit a generation+1 checkpoint carrying the full
	 * overview_derivative (ref_id, encoding spec, content_sha256). On failure,
	 * leave the existing no-overview generation intact.
	 */
	private async backfillOverview(args: {
		sessionId: string;
		slide: string;
		slideInfo: SlideInfo;
		cp: ContextCheckpoint;
		env: CheckpointEnv;
		settings: ReturnType<typeof resolveTransformSettings>;
		systemPrompt: string;
		tools: unknown[];
		firstSnapshotToolCallIdRef: { value: string | null };
	}): Promise<void> {
		const { sessionId, slide, slideInfo, cp, env, settings } = args;
		// Select the overview ref (§7.3): identity → first >90% coverage.
		const ref = selectOverviewRef({
			messages: (await this.store.readSession(sessionId))?.messages || [],
			firstSnapshotToolCallId: args.firstSnapshotToolCallIdRef.value,
			slideWidth: slideInfo.width,
		});
		if (!ref) return; // no overview candidate → stay no-overview

		const spec = overviewDerivativeSpec({
			slide,
			fingerprint: slideInfo.fingerprint || "",
			src: ref.src,
			targetLongEdge: settings.overviewLongEdge,
			jpegQuality: settings.jpegQuality,
			overlayVersion: settings.overlayVersion,
		});

		let result;
		try {
			result = await materializeDerivativeRaw({
				flask: this.flask,
				slide,
				slideInfo,
				spec,
				expectedFingerprint: slideInfo.fingerprint || undefined,
			});
		} catch {
			// Materialization failed → keep the no-overview generation (§3.2).
			return;
		}
		const encoderVersion = result.encoder?.version || "unknown";
		const od = buildOverviewDerivative({
			ref_id: ref.ref_id,
			jpegBase64: result.data,
			target_long_edge: settings.overviewLongEdge,
			jpeg_quality: settings.jpegQuality,
			overlay_version: settings.overlayVersion,
			resize_algorithm: TRANSFORM_RESIZE_ALGORITHM,
			encoder_id: TRANSFORM_ENCODER_ID,
			encoder_version: encoderVersion,
			mime_type: result.mime,
		});

		// Build the g+1 candidate with the overview.
		const stablePrefixObj = buildStablePrefixObject({
			systemPrompt: args.systemPrompt,
			system_prompt_version: env.system_prompt_version,
			tool_schema_hash: env.tool_schema_hash,
			request_schema_version: REQUEST_SCHEMA_VERSION,
			slide_fingerprint: env.slide_fingerprint,
			summary: cp.summary,
			annotation_index: cp.annotation_index,
			overview_derivative: od.overview_derivative,
		});
		const nextCp: ContextCheckpoint = {
			...cp,
			generation: cp.generation + 1,
			created_at: Date.now(),
			overview_derivative: od.overview_derivative,
			stable_prefix_hash: stablePrefixHash(stablePrefixObj),
		};

		// CAS-commit: expected = current generation, fingerprint must match.
		await this.store.commitCheckpoint(
			sessionId,
			cp.generation,
			env.slide_fingerprint,
			(d) => {
				d.context_checkpoint = nextCp;
			},
		);
		// Commit failure (concurrent bump / disk error) → old generation intact,
		// no-overview. Acceptable per §3.2.
	}

	/**
	 * The RunConfig for the currently-active run, captured so helper methods
	 * (buildCheckpointEnv) can resolve transform settings without threading it
	 * through every call. Set at the start of {@link runAgentLoop}.
	 */
	private activeRunConfig: RunConfig | null = null;

	/**
	 * Emit one §12 metrics record for a completed model request. Called from
	 * {@link makeRetryingStreamFn} on the "done" event. Gathers assembler-side
	 * fields (from the PreparedRequest + LRU counters) and provider-side fields
	 * (from the assistant message usage). NO image content or API key.
	 *
	 * Phase 3 (§12): `promptCacheCapabilities` carries the EFFECTIVE mode after
	 * any runtime downgrade (§13). The metrics record the mode that was actually
	 * in effect for this request, not the configured value — so a run that
	 * downgraded from explicit→auto on a provider rejection reports "auto".
	 */
	private emitRequestMetrics(
		sessionId: string,
		config: RunConfig,
		prepared: PreparedRequest | null,
		finalMessage: AssistantMessage | null,
		stableError: StableContextUnavailableError | null,
		promptCacheCapabilities: PromptCacheCapabilities,
		assemblerMetrics: AssemblerMetrics | null,
	): void {
		const usage = finalMessage?.usage as { input?: number; cacheRead?: number; cacheWrite?: number } | undefined;
		// Phase 3: record the EFFECTIVE mode (post-downgrade), not the config.
		const promptCacheMode = promptCacheCapabilities.mode;
		// PreparedRequest byte estimate (retained on the prepared object itself).
		const preparedBytes = prepared?.estimatedBytes ?? 0;
		const preparedImages = prepared?.imageContentHashes.length ?? 0;
		// Phase 4: merge the assembler's per-assembly metrics (image byte split,
		// eviction list, LRU counters, transform/fetch latency). The assembler
		// value is request-local (cleared at the start of every assembly), so it
		// always describes THIS request's most recent assembly — never a stale
		// or cross-session value. Falls back to the PreparedRequest counts when
		// the assembler path did not run (e.g. stable-context safe fallback).
		const asm = assemblerMetrics;
		const metrics = buildRequestMetrics({
			session_id: sessionId,
			checkpoint_generation: prepared?.checkpointGeneration ?? 0,
			stable_prefix_hash: prepared?.stablePrefixHash ?? "",
			prompt_cache_mode: promptCacheMode,
			transform_ms: asm?.transform_ms ?? 0,
			region_fetch_ms: asm?.region_fetch_ms ?? 0,
			selected_images: asm?.selected_images ?? preparedImages,
			materialized_images: asm?.materialized_images ?? preparedImages,
			evicted_image_refs: asm?.evicted_image_refs ?? [],
			image_lru_hits: asm?.image_lru_hits ?? 0,
			image_lru_misses: asm?.image_lru_misses ?? 0,
			overview_image_bytes_sent: asm?.overview_image_bytes_sent ?? 0,
			working_set_image_bytes_sent: asm?.working_set_image_bytes_sent ?? 0,
			prepared_request_bytes: preparedBytes,
			compaction_reason: null,
			checkpoint_rebuild_reason: stableError ? "stable_context_unavailable" : null,
			visual_budget_overflow_tokens: prepared?.visualBudgetOverflowTokens ?? 0,
			usage,
		});
		this.metricsSink(metrics);
	}

	// =========================================================================== //
	// Helpers: slide info, spot lookup, fork image, archive, fork limit
	// =========================================================================== //

	/** Cached slide info fetcher (TTL 60s). */
	private slideInfoCache = new Map<string, { info: SlideInfo; fetchedAt: number }>();
	private static readonly SLIDE_INFO_TTL_MS = 60_000;

	/** Drop cached slide geometry/fingerprint and region LRU for a slide. */
	invalidateSlideCaches(slide: string): void {
		this.slideInfoCache.delete(slide);
		invalidateRegionLru(slide);
	}

	private async fetchSlideInfo(slide: string): Promise<SlideInfo> {
		const cached = this.slideInfoCache.get(slide);
		if (cached && Date.now() - cached.fetchedAt < AgentRunner.SLIDE_INFO_TTL_MS) {
			return cached.info;
		}
		const r = await this.flask.slideInfo(legacySlide(slide));
		const info: SlideInfo = {
			width: r.width,
			height: r.height,
			levelDownsamples: [...(r.levelDownsamples || [1.0])],
			mpp: r.mpp == null ? null : r.mpp,
			fingerprint: r.assetRevision || "",
		};
		const prev = this.slideInfoCache.get(slide);
		if (prev && prev.info.fingerprint && prev.info.fingerprint !== info.fingerprint) {
			invalidateRegionLru(slide);
		}
		this.slideInfoCache.set(slide, { info, fetchedAt: Date.now() });
		return info;
	}

	/**
	 * Find a spot by annotation_id from the full change log (tombstone-aware).
	 * Returns the latest record for the id (deleted or not) or null.
	 * Equivalent to app.py:1704 share_store.get_roi_by_annotation_id.
	 */
	private async findSpot(slide: string, annotationId: string): Promise<(RoiDict & { deleted?: boolean }) | null> {
		let result;
		try {
			result = await this.flask.spots(legacySlide(slide), 0);
		} catch {
			return null;
		}
		// The change log may carry multiple revisions; take the latest for id.
		let latest: (RoiDict & { deleted?: boolean }) | null = null;
		for (const c of result.changes || []) {
			if (String(c.annotation_id || "") === annotationId) {
				latest = c as RoiDict & { deleted?: boolean };
			}
		}
		return latest;
	}

	/**
	 * Build the fork's attached image_ref + inline base64 (app.py:1883
	 * _fork_spot_image_ref). bbox expanded 15%, output 1024-1568px.
	 */
	private async forkSpotImageRef(
		slide: string,
		info: SlideInfo,
		spot: SpotDict,
	): Promise<{ imageRef: import("./session-store.js").ImageRefContent | null; imageB64: string | null }> {
		const x = Math.trunc(Number(spot.x) || 0);
		const y = Math.trunc(Number(spot.y) || 0);
		const side = Math.trunc(Number(spot.side_px) || 0);
		if (side <= 0) return { imageRef: null, imageB64: null };
		const pad = Math.round(side * 0.15);
		const width = info.width;
		const height = info.height;
		const ex = Math.max(0, x - pad);
		const ey = Math.max(0, y - pad);
		const ew = Math.min(side + pad * 2, Math.max(1, width - ex));
		const eh = Math.min(side + pad * 2, Math.max(1, height - ey));
		const src = { x: ex, y: ey, w: ew, h: eh };

		let b64 = "";
		let mag: string | null = null;
		try {
			const r: RegionResult = await this.flask.region({
				slide: legacySlide(slide),
				bbox: { x: ex, y: ey, w: ew, h: eh },
				// Aspect-preserving longest edge (§6.1): fork/branch seed image is a
				// small detail crop; use 1280 (detail tier) instead of fixed 1568².
				maxLongEdge: 1280,
				expectedAssetRevision: info.fingerprint || undefined,
			});
			b64 = bytesToBase64(r.bytes);
			mag = (r.magnification == null ? null : String(r.magnification)) || null;
		} catch (e) {
			if (e instanceof ContractError && e.code === "slide_revision_conflict") {
				this.invalidateSlideCaches(slide);
			}
			b64 = "";
		}

		const imageRef = {
			type: "image_ref" as const,
			ref_id: `ref_fork_${String(spot.annotation_id || "").slice(0, 12)}`,
			slide_fingerprint: info.fingerprint || "",
			src,
			magnification: mag ?? "",
			summary: "该 spot 当前快照（bbox 外扩 15%）",
		};
		return { imageRef, imageB64: b64 || null };
	}

	/** Archive the current main session for a slide (fresh path). */
	private async archiveMainSlot(slide: string): Promise<void> {
		const idx = await this.store.listBySlide(slide);
		const mainId = idx.main;
		if (!mainId) return;
		const d = await this.store.readSession(mainId);
		if (!d) return;
		if (d.archived) return;
		await this.store.withLock(mainId, async (data) => {
			if (!data) return null;
			data.archived = true;
			data.updated_at = Math.floor(Date.now() / 1000);
			await this.store.writeSession(mainId, data);
			return data;
		});
		// Remove the main slot from the index (app.py fresh semantics).
		await this.store.unregister(slide, mainId, "main");
	}

	/**
	 * Archive the oldest non-running forks until under the active limit
	 * (app.py:1917 _enforce_fork_limit). Running forks are never archived.
	 */
	private async enforceForkLimit(slide: string, limit: number): Promise<void> {
		if (limit <= 0) return;
		const idx = await this.store.listBySlide(slide);
		const forks: SessionData[] = [];
		for (const sid of Object.values(idx.forks)) {
			const d = await this.store.readSession(sid);
			if (d) forks.push(d);
		}
		const running = forks.filter((d) => d.status === "running");
		const idle = forks
			.filter((d) => d.status !== "running" && !d.archived)
			.sort((a, b) => (a.updated_at || 0) - (b.updated_at || 0));
		const allowed = Math.max(0, limit - running.length);
		const toArchive = idle.slice(Math.max(0, allowed - idle.length));
		for (const d of toArchive) {
			await this.store.withLock(d.id, async (data) => {
				if (!data) return null;
				data.archived = true;
				data.updated_at = Math.floor(Date.now() / 1000);
				await this.store.writeSession(d.id, data);
				return data;
			});
		}
	}

	/**
	 * Archive the oldest non-running branches until under the active limit.
	 * Reuses `fork_active_limit` as the cap but counts ONLY kind="branch"
	 * sessions (forks and branches are rate-limited independently). Running
	 * branches are never archived.
	 */
	private async enforceBranchLimit(slide: string, limit: number): Promise<void> {
		if (limit <= 0) return;
		const idx = await this.store.listBySlide(slide);
		const branches: SessionData[] = [];
		for (const sid of Object.values(idx.branches)) {
			const d = await this.store.readSession(sid);
			if (d) branches.push(d);
		}
		const running = branches.filter((d) => d.status === "running");
		const idle = branches
			.filter((d) => d.status !== "running" && !d.archived)
			.sort((a, b) => (a.updated_at || 0) - (b.updated_at || 0));
		const allowed = Math.max(0, limit - running.length);
		const toArchive = idle.slice(Math.max(0, allowed - idle.length));
		for (const d of toArchive) {
			await this.store.withLock(d.id, async (data) => {
				if (!data) return null;
				data.archived = true;
				data.updated_at = Math.floor(Date.now() / 1000);
				await this.store.writeSession(d.id, data);
				return data;
			});
		}
	}
}

// =========================================================================== //
// Run-state machine
// =========================================================================== //

interface RunState {
	turnCount: number;
	finished: boolean;
	paused: boolean;
	errored: boolean;
	lastAssistant: AssistantMessage | null;
	/** True when the loop exited because max_steps was reached. */
	hitMaxSteps: boolean;
	abortRequested: boolean;
}

// =========================================================================== //
// Transient / context-exceeded error classification (ai_agent.py:422-468)
// =========================================================================== //

/** ai_agent.py:422 _is_context_exceeded. */
function isContextExceeded(msg: string): boolean {
	const lower = (msg || "").toLowerCase();
	const kws = ["context_length", "maximum context", "too many tokens", "context window"];
	for (const kw of kws) {
		if (lower.includes(kw)) return true;
	}
	return lower.includes("context_length_exceeded");
}

/** ai_agent.py:446 _is_transient_error (message-substring half). */
function isTransientError(msg: string): boolean {
	const lower = (msg || "").toLowerCase();
	const kws = [
		"sslerror",
		"unexpected_eof",
		"eof while",
		"connection reset",
		"connection aborted",
		"broken pipe",
		"timed out",
		"max retries",
	];
	for (const kw of kws) {
		if (lower.includes(kw)) return true;
	}
	// HTTP status code hints in error text (429/5xx).
	if (/\b(408|409|425|429|500|502|503|504)\b/.test(lower)) return true;
	// Gateway/proxy hiccup: a non-2xx with an EMPTY body. pi's error-body.js
	// formats these as `"<code> status code (no body)"`. A real upstream
	// validation error (context too long, bad image, rejected field) always
	// carries a body, so the no-body form means the proxy dropped/rejected the
	// request without explanation — flaky, so retry. (Phase 4: the CPA gateway
	// intermittently returned "400 status code (no body)" for valid image-
	// bearing requests; the identical payload succeeded on retry.)
	if (/\b\d{3} status code \(no body\)/.test(lower)) return true;
	return false;
}

// =========================================================================== //
// Small helpers
// =========================================================================== //

function fmt0(v: number): string {
	return String(Math.round(v));
}

function sleep(ms: number): Promise<void> {
	return new Promise((r) => setTimeout(r, ms));
}

/** Build a minimal error AssistantMessage to terminate a stream. */
function makeErrorAssistant(message: string): AssistantMessage {
	return {
		role: "assistant",
		content: [{ type: "text", text: "" }],
		api: "openai-completions",
		provider: "cpa-gateway",
		model: "unknown",
		usage: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, totalTokens: 0, cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, total: 0 } },
		stopReason: "error",
		errorMessage: message,
		timestamp: Date.now(),
	} as AssistantMessage;
}

/** Avoid an unused-import warning while keeping Message available for typing. */
export type { Message };
