/**
 * Shared fixtures for the Step 3 agent-runner / server tests.
 *
 * Provides:
 *   - a deterministic, scriptable fake streamFn that yields pi
 *     AssistantMessageEvent sequences (so we exercise the real pi Agent +
 *     agent-loop, not a mock of them);
 *   - an in-memory FlaskClient mock (regions, annotations, spots, slide_info);
 *   - a harness builder that wires a SessionStore + SessionEventBus +
 *     AgentRunner with the fake streamFn, and collects emitted events in order.
 */
import { promises as fs } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { createHash } from "node:crypto";

import { createAssistantMessageEventStream, type AssistantMessage, type AssistantMessageEvent, type AssistantMessageEventStream } from "@earendil-works/pi-ai";

import { SessionStore } from "../src/session-store.js";
import { SessionEventBus } from "../src/events.js";
import { AgentRunner } from "../src/agent-runner.js";
import type { CreateAnnotationRequest, PlatformClient, RegionResult, RegionRequest, RoiDict, SlideDescriptor } from "../src/platform/contract.js";
import type { FlaskClient } from "../src/flask-client.js";

// ------------------------------------------------------------------------- //
// Slide fixture: a small pretend pyramid. mpp 0.5 → level-0 20x.
// ------------------------------------------------------------------------- //

export const SLIDE = "test.svs";
export const DOWNSAMPLES = [1, 2, 4, 8];
export const MPP = 0.5;
export const SLIDE_W = 10000;
export const SLIDE_H = 8000;
export const FINGERPRINT = "fp-test:abcd";

export const SLIDE_INFO = {
	width: SLIDE_W,
	height: SLIDE_H,
	levelDownsamples: DOWNSAMPLES,
	mpp: MPP,
	fingerprint: FINGERPRINT,
};

export const BASE_CONFIG = {
	base_url: "http://127.0.0.1:0/v1",
	api_key: "test-key",
	model: "test-model",
	max_tokens: 256,
	context_window_tokens: 8192,
	api_protocol: "openai" as const,
};

// ------------------------------------------------------------------------- //
// Fake streamFn: scriptable assistant turns.
// ------------------------------------------------------------------------- //

/** One scripted assistant turn (what the fake model "returns" for turn N). */
export interface ScriptedTurn {
	/** Text blocks to emit as text_delta then a concatenated text block. */
	text?: string;
	/** Tool calls to emit (toolcall_end). */
	toolCalls?: Array<{ id: string; name: string; arguments: Record<string, unknown> }>;
	/** Stop reason. Defaults to "toolUse" if toolCalls present else "stop". */
	stopReason?: "stop" | "length" | "toolUse";
}

/**
 * Build a fake streamFn that plays back a script of turns in order, one per
 * model call. The script is keyed by the number of assistant messages already
 * in the context (turn counter), so re-entrant calls advance through it.
 *
 * The fake emits the full AssistantMessageEvent protocol (start → text/toolcall
 * deltas → done/error) so the real pi agent-loop processes it correctly.
 */
export function makeFakeStreamFn(script: ScriptedTurn[], opts: { injectError?: { atTurn: number; message: string; transient?: boolean } } = {}): {
	fn: (model: unknown, context: unknown, options?: unknown) => AssistantMessageEventStream;
	calls: number;
} {
	let calls = 0;
	// Track whether the injected error has already fired. The retry wrapper
	// retries the SAME logical call with the SAME context (so assistantCount is
	// unchanged across attempts); without this guard the error would re-inject
	// on every retry and the run could never recover, defeating the test's
	// intent.
	let errorInjected = false;
	const fn = function (_model: unknown, context: unknown): AssistantMessageEventStream {
		const turnIndex = calls;
		calls += 1;
		const stream = createAssistantMessageEventStream();
		void (async () => {
			// Determine turn index from the context's assistant-message count
			// (more robust than the call counter when retries happen).
			const ctx = context as { messages?: Array<{ role?: string; content?: unknown[] }> };
			const assistantCount = (ctx.messages || []).filter((m) => m.role === "assistant").length;

			// Error injection takes precedence over the script. Fires at most once.
			if (!errorInjected && opts.injectError && assistantCount === opts.injectError.atTurn) {
				errorInjected = true;
				const errMsg = opts.injectError.message;
				const errorAssistant = makeAssistant([], "error", errMsg);
				stream.push({ type: "error", reason: "error", error: errorAssistant });
				stream.end(errorAssistant);
				return;
			}

			const turn = script[assistantCount] || script[turnIndex] || { text: "(script exhausted)", stopReason: "stop" as const };
			const content: AssistantMessage["content"] = [];
			// start
			const partial = makeAssistant([], "pending");
			stream.push({ type: "start", partial });
			// text
			if (turn.text) {
				const textStart = makeAssistant(content.slice(), "pending");
				stream.push({ type: "text_start", contentIndex: 0, partial: textStart });
				stream.push({ type: "text_delta", contentIndex: 0, delta: turn.text, partial: textStart });
				content.push({ type: "text", text: turn.text });
				stream.push({ type: "text_end", contentIndex: 0, content: turn.text, partial: makeAssistant(content.slice(), "pending") });
			}
			// tool calls
			for (let i = 0; i < (turn.toolCalls || []).length; i++) {
				const tc = turn.toolCalls![i]!;
				const idx = content.length;
				stream.push({ type: "toolcall_start", contentIndex: idx, partial: makeAssistant(content.slice(), "pending") });
				stream.push({ type: "toolcall_end", contentIndex: idx, toolCall: { type: "toolCall", id: tc.id, name: tc.name, arguments: tc.arguments }, partial: makeAssistant(content.slice(), "pending") });
				content.push({ type: "toolCall", id: tc.id, name: tc.name, arguments: tc.arguments });
			}
			const stopReason = turn.stopReason ?? (turn.toolCalls && turn.toolCalls.length > 0 ? "toolUse" : "stop");
			const finalMsg = makeAssistant(content, stopReason);
			stream.push({ type: "done", reason: stopReason as "stop" | "length" | "toolUse", message: finalMsg });
			stream.end(finalMsg);
		})();
		return stream;
	};
	return { fn, get calls() { return calls; } };
}

function makeAssistant(content: AssistantMessage["content"], stopReason: AssistantMessage["stopReason"], errorMessage?: string): AssistantMessage {
	return {
		role: "assistant",
		content,
		api: "openai-completions",
		provider: "cpa-gateway",
		model: "test-model",
		usage: { input: 10, output: 5, cacheRead: 0, cacheWrite: 0, totalTokens: 15, cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, total: 0 } },
		stopReason,
		errorMessage,
		timestamp: Date.now(),
	} as AssistantMessage;
}

// ------------------------------------------------------------------------- //
// In-memory FlaskClient mock
// ------------------------------------------------------------------------- //

export interface MockFlaskState {
	regionCalls: Array<{ x: number; y: number; w: number; h: number; outW?: number; outH?: number; maxLongEdge?: number; expectedAssetRevision?: string }>;
	annotateCalls: Array<{ label: string; x: number; y: number; sidePx: number; note?: string; effectKey?: string; sessionId?: string }>;
	/** Spot change log; the test mutates this to simulate annotation edits. */
	spotChanges: Array<Record<string, unknown> & { annotation_id: string; deleted?: boolean; change_seq: number }>;
	currentSeq: number;
	/** Slide files that exist (for _validate_ai_slide parity in tests). */
	annotateResult: RoiDict;
	regionResult?: Partial<RegionResult>;
	/** Per-call error injection for region/annotate. */
	regionError?: Error;
}

export function makeMockFlask(initialSpots: Array<Record<string, unknown> & { annotation_id: string; deleted?: boolean; change_seq: number }> = []): MockFlaskState & Pick<PlatformClient, "region" | "annotate" | "spots" | "slideInfo"> {
	// The returned object proxies mutable fields through getters/setters so
	// test code mutating `mock.currentSeq` / `mock.spotChanges` is visible to
	// the closure-based method implementations.
	const state: MockFlaskState = {
		regionCalls: [],
		annotateCalls: [],
		spotChanges: [...initialSpots],
		currentSeq: initialSpots.reduce((m, s) => Math.max(m, s.change_seq || 0), 0),
		annotateResult: {
			annotation_id: "ann-uuid-1",
			index: 0,
			token: "admin",
			slide: SLIDE,
			label: "",
			note: "",
			type: "rect",
			x: 0,
			y: 0,
			side_px: 0,
			size_mm: 0,
			shared: false,
			source: "ai",
			created_by_session_id: "",
			change_seq: 1,
			revision: 1,
		},
	};
	const obj = {
		async region(request: RegionRequest) {
			state.regionCalls.push({
				x: request.bbox.x,
				y: request.bbox.y,
				w: request.bbox.w,
				h: request.bbox.h,
				outW: request.outW,
				outH: request.outH,
				maxLongEdge: request.maxLongEdge,
				expectedAssetRevision: request.expectedAssetRevision,
			});
			if (state.regionError) throw state.regionError;
			const ov = state.regionResult;
			// Aspect-preserving width/height echo when maxLongEdge is set and no
			// explicit override (mirrors the real Flask server behavior).
			let ow = ov?.width ?? request.outW ?? 1024;
			let oh = ov?.height ?? request.outH ?? 1024;
			if (request.maxLongEdge && !ov?.width) {
				const longest = Math.max(request.bbox.w, request.bbox.h);
				const scale = request.maxLongEdge / longest;
				ow = Math.max(1, Math.round(request.bbox.w * scale));
				oh = Math.max(1, Math.round(request.bbox.h * scale));
			}
			const bytes: Uint8Array = ov?.bytes ?? Buffer.from("AAAA", "base64");
			const r: RegionResult = {
				bytes,
				mimeType: ov?.mimeType ?? "image/jpeg",
				width: ow,
				height: oh,
				src: ov?.src ?? { x: request.bbox.x, y: request.bbox.y, w: request.bbox.w, h: request.bbox.h },
				magnification: ov?.magnification ?? 20,
				contentSha256: ov?.contentSha256 ?? createHash("sha256").update(Buffer.from(bytes)).digest("hex"),
				assetRevision: ov?.assetRevision,
				encoder: ov?.encoder,
			};
			return r;
		},
		async annotate(args: CreateAnnotationRequest) {
			state.annotateCalls.push({ label: args.label, x: args.x, y: args.y, sidePx: args.sidePx, note: args.note, effectKey: args.effectKey, sessionId: args.sessionId });
			state.currentSeq += 1;
			const roi: RoiDict = {
				...state.annotateResult,
				label: args.label,
				x: args.x,
				y: args.y,
				side_px: args.sidePx,
				note: args.note ?? "",
				change_seq: state.currentSeq,
			};
			// Also record into the spot change log so subsequent runs see it.
			state.spotChanges.push({ ...roi, annotation_id: roi.annotation_id, deleted: false, change_seq: state.currentSeq });
			return roi;
		},
		async spots(_ref: unknown, afterSeq: number) {
			const changes = state.spotChanges.filter((s) => (s.change_seq || 0) > afterSeq);
			return { changes, currentSeq: state.currentSeq };
		},
		async slideInfo(_ref: unknown): Promise<SlideDescriptor> {
			return { width: SLIDE_W, height: SLIDE_H, levelDownsamples: [...DOWNSAMPLES], mpp: MPP, assetRevision: FINGERPRINT };
		},
	};
	// Proxy mutable fields through getters/setters so test code mutating them
	// (e.g. `mock.currentSeq += 1`) is reflected in the closure's `state`.
	Object.defineProperties(obj, {
		regionCalls: { get: () => state.regionCalls, set: (v) => { state.regionCalls = v; }, enumerable: true, configurable: true },
		annotateCalls: { get: () => state.annotateCalls, set: (v) => { state.annotateCalls = v; }, enumerable: true, configurable: true },
		spotChanges: { get: () => state.spotChanges, set: (v) => { state.spotChanges = v; }, enumerable: true, configurable: true },
		currentSeq: { get: () => state.currentSeq, set: (v) => { state.currentSeq = v; }, enumerable: true, configurable: true },
		annotateResult: { get: () => state.annotateResult, set: (v) => { state.annotateResult = v; }, enumerable: true, configurable: true },
		regionResult: { get: () => state.regionResult, set: (v) => { state.regionResult = v; }, enumerable: true, configurable: true },
		regionError: { get: () => state.regionError, set: (v) => { state.regionError = v; }, enumerable: true, configurable: true },
	});
	return obj as unknown as MockFlaskState & Pick<PlatformClient, "region" | "annotate" | "spots" | "slideInfo">;
}

/**
 * A mock of the LEGACY {@link FlaskClient} engine (Flask `/internal/ai/*` wire
 * shape: `image_base64`, `current_seq`, `level_downsamples`, `fingerprint`).
 *
 * Use this for code paths that wrap the engine in {@link LegacyFlaskPlatformAdapter}
 * (the experiment runner in experiments/src/run.ts), where the mock stands in for
 * the real Flask process. For code paths that take a {@link PlatformClient}
 * directly (AgentRunner / tools / assembler tests), use {@link makeMockFlask}
 * instead — it returns the decoded contract shape.
 */
export function makeLegacyFlaskMock(): Pick<FlaskClient, "region" | "annotate" | "spots" | "slideInfo"> {
	const region = async (args: { x: number; y: number; w: number; h: number; out_w?: number; out_h?: number; max_long_edge?: number }) => ({
		image_base64: "QUFBQQ==", // "AAAA"
		mime: "image/jpeg",
		width: args.out_w ?? 1024,
		height: args.out_h ?? 1024,
		src: { x: args.x, y: args.y, w: args.w, h: args.h },
		magnification: 20,
		encoder: { id: "pillow", version: "test", resize: "LANCZOS", overlay_version: "v1", jpeg_quality: 85 },
	});
	const annotate = async () => ({
		annotation_id: "ann-uuid-1", index: 0, token: "admin", slide: SLIDE, label: "", note: "", type: "rect",
		x: 0, y: 0, side_px: 0, size_mm: 0, shared: false, source: "ai", created_by_session_id: "", change_seq: 1, revision: 1,
	});
	const spots = async (_slide: string, _afterSeq: number) => ({ changes: [], current_seq: 0 });
	const slideInfo = async (_slide: string) => ({ width: SLIDE_W, height: SLIDE_H, level_downsamples: [...DOWNSAMPLES], mpp: MPP, fingerprint: FINGERPRINT });
	return { region, annotate, spots, slideInfo } as unknown as Pick<FlaskClient, "region" | "annotate" | "spots" | "slideInfo">;
}

// ------------------------------------------------------------------------- //
// Harness: store + bus + runner + collected events
// ------------------------------------------------------------------------- //

export interface Harness {
	store: SessionStore;
	bus: SessionEventBus;
	runner: AgentRunner;
	mock: ReturnType<typeof makeMockFlask>;
	/** Collect a live event log for the session under test (set via watch()). */
	events: Array<{ seq: number; type: string; payload: Record<string, unknown> }>;
	dir: string;
	watch: (sessionId: string) => void;
}

let rootTmp = "";

export async function newHarness(
	fakeStreamFn: (model: unknown, context: unknown, options?: unknown) => AssistantMessageEventStream,
	overrides: { compactionModels?: { completeSimple: (model: unknown, context: unknown, options?: unknown) => Promise<unknown> } } = {},
): Promise<Harness> {
	if (!rootTmp) {
		rootTmp = await fs.mkdtemp(join(tmpdir(), "svs-step3-"));
	}
	const dir = join(rootTmp, `d${Math.random().toString(36).slice(2)}`);
	await fs.mkdir(dir, { recursive: true });
	const store = new SessionStore({ sessionsDir: dir });
	const bus = new SessionEventBus(store);
	const mock = makeMockFlask();
	const runner = new AgentRunner(store, bus, mock as unknown as PlatformClient, {
		streamFn: fakeStreamFn as never,
		...(overrides.compactionModels ? { compactionModels: overrides.compactionModels } : {}),
	});
	const events: Array<{ seq: number; type: string; payload: Record<string, unknown> }> = [];
	const watch = (sessionId: string) => {
		bus.subscribe(sessionId, (seq, type, payload) => {
			events.push({ seq, type, payload });
		});
	};
	return { store, bus, runner, mock, events, dir, watch };
}

export async function cleanupRootTmp(): Promise<void> {
	// Intentionally a no-op: each test creates its own subdirectory under the
	// shared root temp dir, and runs are async-fire (the loop may still be
	// settling when the test function returns). Removing the tree here would
	// race those background writes and surface as spurious ENOENT failures.
	// The OS reclaims /tmp on reboot; vitest runs are short-lived.
}

/** Wait until a session reaches a terminal status (not running/idle). */
export async function waitForSettle(store: SessionStore, sessionId: string, timeoutMs = 5000): Promise<string> {
	const deadline = Date.now() + timeoutMs;
	while (Date.now() < deadline) {
		const d = await store.readSession(sessionId);
		if (d && d.status !== "running" && d.status !== "idle") return d.status;
		await new Promise((r) => setTimeout(r, 20));
	}
	throw new Error(`session ${sessionId} did not settle within ${timeoutMs}ms`);
}

/** Re-exported for type-only convenience in tests. */
export type { AssistantMessageEvent };
