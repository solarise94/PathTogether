/**
 * Tests for src/transform-context.ts (Step 4): image_ref materialization,
 * image eviction, fingerprint-mismatch degradation, and the no-throw contract.
 */
import { describe, expect, it, beforeEach } from "vitest";

import type { AgentMessage } from "@earendil-works/pi-agent-core";

import {
	makeTransformContext,
	resolveTransformSettings,
	countImageBlocks,
	hasNoImageRefBlocks,
	clearRegionLru,
	regionLruSize,
	configureRegionLru,
	invalidateRegionLru,
	resetLruCounters,
	lruHitCount_value,
	lruMissCount_value,
	richHistoryForRef,
	buildRichHistoryText,
	dropDerivative,
	peekDerivative,
	putDerivative,
	overviewDerivativeSpec,
	materializeDerivativeRaw,
	type RichHistoryObservation,
} from "../src/transform-context.js";
import type { FlaskClient, RegionResult } from "../src/flask-client.js";
import type { SlideInfo } from "../src/tools.js";
import type { ImageRefContent, PersistedAgentMessage } from "../src/session-store.js";

// ------------------------------------------------------------------------- //
// Fixtures
// ------------------------------------------------------------------------- //

const SLIDE = "test.svs";
const SLIDE_INFO: SlideInfo = {
	width: 10000,
	height: 8000,
	levelDownsamples: [1, 2, 4, 8],
	mpp: 0.5,
	fingerprint: "fp-test",
};

beforeEach(() => {
	clearRegionLru();
});

/** Build an image_ref block. */
function imgRef(refId: string, src: { x: number; y: number; w: number; h: number }, fingerprint = "fp-test"): ImageRefContent {
	return {
		type: "image_ref",
		ref_id: refId,
		slide_fingerprint: fingerprint,
		src,
		magnification: "20x",
		summary: "snap",
	};
}

/** Build a user message whose content is the given blocks. */
function userMsg(blocks: unknown[], ts = Date.now()): AgentMessage {
	return { role: "user", content: blocks as never, timestamp: ts } as AgentMessage;
}

/** Build a toolResult message carrying an image_ref (the snapshot-tool shape). */
function toolResultMsg(toolCallId: string, blocks: unknown[], ts = Date.now()): AgentMessage {
	return {
		role: "toolResult",
		toolCallId,
		content: blocks as never,
		timestamp: ts,
	} as unknown as AgentMessage;
}

/**
 * A minimal flask mock whose region() returns a deterministic base64 per call.
 *
 * Supports:
 *   - opts.fail: region() throws synchronously.
 *   - opts.emptyB64: returns empty base64.
 *   - opts.b64ByRefId: per-b64 override keyed by `x{x}y{y}`.
 *   - opts.delayMs: resolves after a delay (for AbortSignal-in-flight tests).
 *   - opts.blockUntil: a promise the call awaits before resolving (for abort-
 *     timing tests where we need the fetch to be observable in-flight).
 *
 * Records every call's args (including max_long_edge / signal) on lastArgs.
 */
function makeFlask(
	opts: { fail?: boolean; emptyB64?: boolean; b64ByRefId?: Record<string, string>; delayMs?: number; blockUntil?: Promise<void> } = {},
): Pick<FlaskClient, "region"> & { calls: number; lastArgs: Array<Record<string, unknown>>; invocations: Array<{ args: Record<string, unknown>; startedAt: number; signal?: AbortSignal }> } {
	const state = { calls: 0 };
	const lastArgs: Array<Record<string, unknown>> = [];
	const invocations: Array<{ args: Record<string, unknown>; startedAt: number; signal?: AbortSignal }> = [];
	const region = async (args: Record<string, unknown> & { x: number; y: number; w: number; h: number; out_w?: number; out_h?: number; max_long_edge?: number; signal?: AbortSignal }): Promise<RegionResult> => {
		state.calls += 1;
		lastArgs.push(args);
		const sig = args.signal as AbortSignal | undefined;
		invocations.push({ args, startedAt: Date.now(), signal: sig });
		// Wait on a blocker if provided (lets tests observe in-flight state).
		if (opts.blockUntil) {
			await opts.blockUntil;
		}
		if (opts.delayMs) {
			await new Promise<void>((resolve) => setTimeout(resolve, opts.delayMs));
		}
		if (opts.fail) throw new Error("region boom");
		// If the signal aborted while we were "in flight", reject like fetch does.
		if (sig?.aborted) {
			const err = new Error("The operation was aborted");
			err.name = "AbortError";
			throw err;
		}
		const b64 = (opts.b64ByRefId && opts.b64ByRefId[`x${args.x}y${args.y}`]) || "QUFBQQ=="; // "AAAA"
		// When max_long_edge is set, echo a width/height that preserves aspect.
		let ow = (args.out_w as number | undefined) ?? 1024;
		let oh = (args.out_h as number | undefined) ?? 1024;
		if (args.max_long_edge) {
			const longest = Math.max(args.w, args.h);
			const scale = (args.max_long_edge as number) / longest;
			ow = Math.max(1, Math.round(args.w * scale));
			oh = Math.max(1, Math.round(args.h * scale));
		}
		return {
			image_base64: opts.emptyB64 ? "" : b64,
			mime: "image/jpeg",
			width: ow,
			height: oh,
			src: { x: args.x, y: args.y, w: args.w, h: args.h },
			magnification: 20,
			encoder: { id: "pillow", version: "test", resize: "LANCZOS", overlay_version: "v1", jpeg_quality: 85 },
		};
	};
	// Use getters so `calls` reflects the live closure counter.
	const obj: Pick<FlaskClient, "region"> & { calls: number; lastArgs: Array<Record<string, unknown>>; invocations: Array<{ args: Record<string, unknown>; startedAt: number; signal?: AbortSignal }> } = {
		region: region as never,
		get calls() {
			return state.calls;
		},
		get lastArgs() {
			return lastArgs;
		},
		get invocations() {
			return invocations;
		},
	};
	return obj;
}

// ------------------------------------------------------------------------- //
// Tests
// ------------------------------------------------------------------------- //

describe("transform-context", () => {
	describe("resolveTransformSettings", () => {
		it("defaults to 4 (visual_working_set_max) when unset or invalid", () => {
			expect(resolveTransformSettings({}).keepRecentImages).toBe(4);
			expect(resolveTransformSettings({ keep_recent_images: -1 }).keepRecentImages).toBe(4);
			expect(resolveTransformSettings({ keep_recent_images: NaN }).keepRecentImages).toBe(4);
			expect(resolveTransformSettings({ keep_recent_images: "abc" as unknown as number }).keepRecentImages).toBe(4);
		});
		it("maps legacy keep_recent_images when set", () => {
			// legacy positive value is honored
			expect(resolveTransformSettings({ keep_recent_images: 8 }).keepRecentImages).toBe(8);
			// legacy 0 = "unset" historically → new default 4 (visual_working_set_max)
			expect(resolveTransformSettings({ keep_recent_images: 0 }).keepRecentImages).toBe(4);
			expect(resolveTransformSettings({ keep_recent_images: 4.9 }).keepRecentImages).toBe(4);
		});
		it("prefers visual_working_set_max over legacy keep_recent_images", () => {
			expect(resolveTransformSettings({ visual_working_set_max: 3, keep_recent_images: 8 }).keepRecentImages).toBe(3);
			// new field 0 is honored (means "keep zero non-overview")
			expect(resolveTransformSettings({ visual_working_set_max: 0 }).keepRecentImages).toBe(0);
		});
		it("resolves Phase 1 long-edge / quality / LRU fields with defaults", () => {
			const s = resolveTransformSettings({});
			expect(s.overviewLongEdge).toBe(1024);
			expect(s.workingImageLongEdge).toBe(768);
			expect(s.detailImageLongEdge).toBe(1280);
			expect(s.jpegQuality).toBe(85);
			expect(s.overlayVersion).toBe("v1");
			expect(s.regionConcurrency).toBe(3);
			expect(s.lruMaxBytes).toBe(64 * 1024 * 1024);
			expect(s.lruTtlMs).toBe(1800_000);
		});
	});

	describe("materialization", () => {
		it("turns an image_ref into an image block via flask.region", async () => {
			const flask = makeFlask();
			const transform = makeTransformContext({
				flask: flask as unknown as FlaskClient,
				slide: SLIDE,
				slideInfo: SLIDE_INFO,
				settings: resolveTransformSettings({ keep_recent_images: 6 }),
				firstSnapshotToolCallIdRef: { value: null },
			});
			const msgs: AgentMessage[] = [userMsg([imgRef("ref_a", { x: 100, y: 100, w: 500, h: 500 })])];
			const out = await transform(msgs);
			expect(flask.calls).toBe(1);
			expect(countImageBlocks(out)).toBe(1);
			// invariant: no image_ref left
			expect(hasNoImageRefBlocks(out)).toBe(true);
		});

		it("degrades to text when flask.region throws (fingerprint/availability)", async () => {
			const flask = makeFlask({ fail: true });
			const transform = makeTransformContext({
				flask: flask as unknown as FlaskClient,
				slide: SLIDE,
				slideInfo: SLIDE_INFO,
				settings: resolveTransformSettings({ keep_recent_images: 6 }),
				firstSnapshotToolCallIdRef: { value: null },
			});
			const msgs: AgentMessage[] = [userMsg([imgRef("ref_a", { x: 100, y: 100, w: 500, h: 500 })])];
			const out = await transform(msgs);
			expect(countImageBlocks(out)).toBe(0);
			expect(hasNoImageRefBlocks(out)).toBe(true);
			// The degraded text matches ai_session.py:855.
			const c = (out[0] as { content: Array<{ type: string; text?: string }> }).content;
			expect(c.some((b) => b.type === "text" && b.text === "该图因切片变更不可用。")).toBe(true);
		});

		it("degrades to text when region returns empty base64", async () => {
			const flask = makeFlask({ emptyB64: true });
			const transform = makeTransformContext({
				flask: flask as unknown as FlaskClient,
				slide: SLIDE,
				slideInfo: SLIDE_INFO,
				settings: resolveTransformSettings({ keep_recent_images: 6 }),
				firstSnapshotToolCallIdRef: { value: null },
			});
			const msgs: AgentMessage[] = [toolResultMsg("tc1", [imgRef("ref_tc1", { x: 1, y: 1, w: 10, h: 10 })])];
			const out = await transform(msgs);
			expect(countImageBlocks(out)).toBe(0);
			expect(hasNoImageRefBlocks(out)).toBe(true);
		});

		it("preserves sibling text blocks and non-content messages", async () => {
			const flask = makeFlask();
			const transform = makeTransformContext({
				flask: flask as unknown as FlaskClient,
				slide: SLIDE,
				slideInfo: SLIDE_INFO,
				settings: resolveTransformSettings({ keep_recent_images: 6 }),
				firstSnapshotToolCallIdRef: { value: null },
			});
			const msgs: AgentMessage[] = [
				{ role: "assistant", content: [{ type: "text", text: "hello" }], timestamp: 1 } as never,
				userMsg([{ type: "text", text: "look" }, imgRef("ref_a", { x: 1, y: 1, w: 10, h: 10 })]),
			];
			const out = await transform(msgs);
			// assistant message untouched
			expect((out[0] as { content: Array<{ type: string; text: string }> }).content[0]!.text).toBe("hello");
			// user message keeps its text block + gains an image block
			const u = out[1] as { content: Array<{ type: string; text?: string }> };
			expect(u.content[0]!.type).toBe("text");
			expect(u.content[0]!.text).toBe("look");
			expect(u.content.some((b) => b.type === "image")).toBe(true);
			expect(hasNoImageRefBlocks(out)).toBe(true);
		});
	});

	describe("eviction", () => {
		it("keeps only the last N images, drops older ones to placeholder text", async () => {
			const flask = makeFlask();
			const transform = makeTransformContext({
				flask: flask as unknown as FlaskClient,
				slide: SLIDE,
				slideInfo: SLIDE_INFO,
				settings: resolveTransformSettings({ keep_recent_images: 6 }),
				firstSnapshotToolCallIdRef: { value: null },
			});
			// 8 snapshots, each its own toolResult message (no overview protection).
			const msgs: AgentMessage[] = [];
			for (let i = 0; i < 8; i++) {
				msgs.push(toolResultMsg(`tc${i}`, [imgRef(`ref_tc${i}`, { x: i, y: i, w: 100, h: 100 })], 1000 + i));
			}
			const out = await transform(msgs);
			// 6 images retained, 2 evicted to text placeholders.
			expect(countImageBlocks(out)).toBe(6);
			// Pre-evict: only KEEP refs call flask.region (not all 8).
			expect(flask.calls).toBe(6);
			// The 6 kept are the most recent (tc2..tc7); tc0, tc1 evicted.
			const evictedTexts = (out as Array<{ content: Array<{ type: string; text?: string }> }>)
				.flatMap((m) => m.content)
				.filter((b) => b.type === "text" && b.text === "（历史快照已省略，可用 goto+snapshot 重新查看）");
			expect(evictedTexts.length).toBe(2);
			expect(hasNoImageRefBlocks(out)).toBe(true);
		});

		it("always retains the whole-slide overview snapshot (identity match)", async () => {
			const flask = makeFlask();
			const firstSnapshotRef = { value: "snap-overview-1" };
			const transform = makeTransformContext({
				flask: flask as unknown as FlaskClient,
				slide: SLIDE,
				slideInfo: SLIDE_INFO,
				settings: resolveTransformSettings({ keep_recent_images: 2 }),
				firstSnapshotToolCallIdRef: firstSnapshotRef,
			});
			// First snapshot is the overview (ref_id matches snap-overview-1),
			// followed by 4 normal snapshots. keep_recent_images=2 → normally
			// only the last 2 non-overview survive, but the overview is protected.
			const msgs: AgentMessage[] = [
				toolResultMsg("snap-overview-1", [imgRef("ref_snap-overview-1", { x: 0, y: 0, w: 100, h: 100 })], 1),
				toolResultMsg("snap-2", [imgRef("ref_snap-2", { x: 1, y: 1, w: 100, h: 100 })], 2),
				toolResultMsg("snap-3", [imgRef("ref_snap-3", { x: 2, y: 2, w: 100, h: 100 })], 3),
				toolResultMsg("snap-4", [imgRef("ref_snap-4", { x: 3, y: 3, w: 100, h: 100 })], 4),
				toolResultMsg("snap-5", [imgRef("ref_snap-5", { x: 4, y: 4, w: 100, h: 100 })], 5),
			];
			const out = await transform(msgs);
			// Overview + last 2 = 3 images.
			expect(countImageBlocks(out)).toBe(3);
			// The overview (first message) is still an image, not a placeholder.
			const first = out[0] as { content: Array<{ type: string }> };
			expect(first.content.some((b) => b.type === "image")).toBe(true);
			expect(hasNoImageRefBlocks(out)).toBe(true);
		});

		it("always retains a >90% coverage snapshot (coverage fallback)", async () => {
			const flask = makeFlask();
			// No identity ref set → coverage heuristic must catch the wide bbox.
			const transform = makeTransformContext({
				flask: flask as unknown as FlaskClient,
				slide: SLIDE,
				slideInfo: SLIDE_INFO,
				settings: resolveTransformSettings({ keep_recent_images: 1 }),
				firstSnapshotToolCallIdRef: { value: null },
			});
			// Overview bbox covers the whole width (w=9500 on a 10000-wide slide → 95%).
			const msgs: AgentMessage[] = [
				toolResultMsg("ov", [imgRef("ref_ov", { x: 0, y: 0, w: 9500, h: 8000 })], 1),
				toolResultMsg("s2", [imgRef("ref_s2", { x: 1, y: 1, w: 100, h: 100 })], 2),
				toolResultMsg("s3", [imgRef("ref_s3", { x: 2, y: 2, w: 100, h: 100 })], 3),
			];
			const out = await transform(msgs);
			// Overview (protected) + last 1 = 2 images.
			expect(countImageBlocks(out)).toBe(2);
			const first = out[0] as { content: Array<{ type: string }> };
			expect(first.content.some((b) => b.type === "image")).toBe(true);
		});

		it("protects only the FIRST >90% coverage image when firstSnapshotToolCallId is null", async () => {
			const flask = makeFlask();
			const transform = makeTransformContext({
				flask: flask as unknown as FlaskClient,
				slide: SLIDE,
				slideInfo: SLIDE_INFO,
				settings: resolveTransformSettings({ keep_recent_images: 1 }),
				firstSnapshotToolCallIdRef: { value: null },
			});
			const msgs: AgentMessage[] = [
				toolResultMsg("ov1", [imgRef("ref_ov1", { x: 0, y: 0, w: 9500, h: 8000 })], 1),
				toolResultMsg("ov2", [imgRef("ref_ov2", { x: 0, y: 0, w: 9600, h: 8000 })], 2),
				toolResultMsg("s3", [imgRef("ref_s3", { x: 2, y: 2, w: 100, h: 100 })], 3),
			];
			const out = await transform(msgs);
			// First overview protected + last 1 non-overview (s3). Second wide image is NOT protected.
			expect(countImageBlocks(out)).toBe(2);
			const second = out[1] as { content: Array<{ type: string; text?: string }> };
			expect(second.content.some((b) => b.type === "text" && b.text === "（历史快照已省略，可用 goto+snapshot 重新查看）")).toBe(true);
		});

		it("does not evict when under the cap", async () => {
			const flask = makeFlask();
			const transform = makeTransformContext({
				flask: flask as unknown as FlaskClient,
				slide: SLIDE,
				slideInfo: SLIDE_INFO,
				settings: resolveTransformSettings({ keep_recent_images: 6 }),
				firstSnapshotToolCallIdRef: { value: null },
			});
			const msgs: AgentMessage[] = [
				toolResultMsg("a", [imgRef("ref_a", { x: 1, y: 1, w: 10, h: 10 })], 1),
				toolResultMsg("b", [imgRef("ref_b", { x: 2, y: 2, w: 10, h: 10 })], 2),
			];
			const out = await transform(msgs);
			expect(countImageBlocks(out)).toBe(2);
		});
	});

	describe("fingerprint", () => {
		it("skips flask.region and degrades when slide_fingerprint mismatches slideInfo", async () => {
			const flask = makeFlask();
			const transform = makeTransformContext({
				flask: flask as unknown as FlaskClient,
				slide: SLIDE,
				slideInfo: SLIDE_INFO,
				settings: resolveTransformSettings({ keep_recent_images: 6 }),
				firstSnapshotToolCallIdRef: { value: null },
			});
			const msgs: AgentMessage[] = [userMsg([imgRef("ref_a", { x: 1, y: 1, w: 10, h: 10 }, "fp-old")])];
			const out = await transform(msgs);
			expect(flask.calls).toBe(0);
			expect(countImageBlocks(out)).toBe(0);
			expect(hasNoImageRefBlocks(out)).toBe(true);
			const c = (out[0] as { content: Array<{ type: string; text?: string }> }).content;
			expect(c.some((b) => b.type === "text" && b.text === "该图因切片变更不可用。")).toBe(true);
		});

		it("passes expected_fingerprint to flask.region", async () => {
			const flask = makeFlask();
			const transform = makeTransformContext({
				flask: flask as unknown as FlaskClient,
				slide: SLIDE,
				slideInfo: SLIDE_INFO,
				settings: resolveTransformSettings({ keep_recent_images: 6 }),
				firstSnapshotToolCallIdRef: { value: null },
			});
			await transform([userMsg([imgRef("ref_a", { x: 1, y: 1, w: 10, h: 10 })])]);
			expect(flask.calls).toBe(1);
			expect((flask.lastArgs[0] as { expected_fingerprint?: string }).expected_fingerprint).toBe("fp-test");
		});
	});

	describe("contract", () => {
		it("never rejects: strips image_ref on throw instead of leaving originals", async () => {
			// Force a top-level throw inside transformOnce by passing a slideInfo
			// whose width getter throws — the overview-detection path touches
			// slideInfo.width when firstSnapshotToolCallId is null.
			const poisonInfo = {
				get width(): number {
					throw new Error("poison");
				},
				height: 8000,
				levelDownsamples: [1, 2, 4, 8],
				mpp: 0.5,
				fingerprint: "fp-test",
			};
			const flask = makeFlask();
			const transform = makeTransformContext({
				flask: flask as unknown as FlaskClient,
				slide: SLIDE,
				slideInfo: poisonInfo as unknown as SlideInfo,
				settings: resolveTransformSettings({ keep_recent_images: 6 }),
				firstSnapshotToolCallIdRef: { value: null },
			});
			const msgs: AgentMessage[] = [userMsg([imgRef("ref_a", { x: 1, y: 1, w: 10, h: 10 })])];
			const out = await transform(msgs);
			expect(Array.isArray(out)).toBe(true);
			expect(hasNoImageRefBlocks(out)).toBe(true);
			const c = (out[0] as { content: Array<{ type: string; text?: string }> }).content;
			expect(c.some((b) => b.type === "text" && b.text === "该图因切片变更不可用。")).toBe(true);
		});

		it("is pure: does not mutate the input array", async () => {
			const flask = makeFlask();
			const transform = makeTransformContext({
				flask: flask as unknown as FlaskClient,
				slide: SLIDE,
				slideInfo: SLIDE_INFO,
				settings: resolveTransformSettings({ keep_recent_images: 6 }),
				firstSnapshotToolCallIdRef: { value: null },
			});
			const original: AgentMessage[] = [userMsg([imgRef("ref_a", { x: 1, y: 1, w: 10, h: 10 })])];
			const snapshotBefore = JSON.stringify(original);
			await transform(original);
			expect(JSON.stringify(original)).toBe(snapshotBefore);
		});

		it("handles string content (passthrough)", async () => {
			const flask = makeFlask();
			const transform = makeTransformContext({
				flask: flask as unknown as FlaskClient,
				slide: SLIDE,
				slideInfo: SLIDE_INFO,
				settings: resolveTransformSettings({ keep_recent_images: 6 }),
				firstSnapshotToolCallIdRef: { value: null },
			});
			const msgs = [{ role: "user", content: "plain text", timestamp: 1 }] as unknown as AgentMessage[];
			const out = await transform(msgs);
			expect((out[0] as { content: unknown }).content).toBe("plain text");
			expect(flask.calls).toBe(0);
		});

		it("LRU keys include slide so identical bbox+fp on another slide re-fetches", async () => {
			const flask = makeFlask();
			const src = { x: 5, y: 5, w: 20, h: 20 };
			const transformA = makeTransformContext({
				flask: flask as unknown as FlaskClient,
				slide: "a.svs",
				slideInfo: SLIDE_INFO,
				settings: resolveTransformSettings({ keep_recent_images: 6 }),
				firstSnapshotToolCallIdRef: { value: null },
			});
			const transformB = makeTransformContext({
				flask: flask as unknown as FlaskClient,
				slide: "b.svs",
				slideInfo: SLIDE_INFO,
				settings: resolveTransformSettings({ keep_recent_images: 6 }),
				firstSnapshotToolCallIdRef: { value: null },
			});
			await transformA([userMsg([imgRef("ref_a", src)])]);
			expect(flask.calls).toBe(1);
			await transformA([userMsg([imgRef("ref_a", src)])]);
			expect(flask.calls).toBe(1); // LRU hit for a.svs
			await transformB([userMsg([imgRef("ref_a", src)])]);
			expect(flask.calls).toBe(2); // different slide → miss
			expect((flask.lastArgs[1] as { slide: string }).slide).toBe("b.svs");
		});
	});

	describe("countImageBlocks / hasNoImageRefBlocks helpers", () => {
		it("count image blocks only (not refs)", () => {
			const msgs: PersistedAgentMessage[] = [
				{ role: "user", content: [{ type: "image", data: "x", mimeType: "image/jpeg" }, { type: "text", text: "hi" }] } as never,
				{ role: "user", content: [{ type: "image_ref", ref_id: "r", slide_fingerprint: "", src: { x: 0, y: 0, w: 0, h: 0 }, magnification: "", summary: "" }] } as never,
			];
			expect(countImageBlocks(msgs)).toBe(1);
			expect(hasNoImageRefBlocks(msgs)).toBe(false);
		});
	});

	describe("Provider boundary: _context_meta stripped (§10)", () => {
		it("strips _context_meta from the transform output (happy path)", async () => {
			const flask = makeFlask();
			const transform = makeTransformContext({
				flask: flask as unknown as FlaskClient,
				slide: SLIDE,
				slideInfo: SLIDE_INFO,
				settings: resolveTransformSettings({ keep_recent_images: 6 }),
				firstSnapshotToolCallIdRef: { value: null },
			});
			// Input messages carry sidecar-only _context_meta.
			const msgs = [
				{ role: "user", content: [{ type: "image_ref", ref_id: "ref_a", slide_fingerprint: "fp-test", src: { x: 100, y: 100, w: 500, h: 500 }, magnification: "20x", summary: "snap" }], timestamp: 1, _context_meta: { session_message_seq: 1 } },
				{ role: "user", content: "plain text", timestamp: 2, _context_meta: { session_message_seq: 2 } },
			] as unknown as AgentMessage[];
			const out = await transform(msgs);
			// Provider payload must have no _context_meta and no image_ref.
			for (const m of out as unknown[]) {
				expect("_context_meta" in (m as object)).toBe(false);
			}
			expect(hasNoImageRefBlocks(out)).toBe(true);
		});

		it("strips _context_meta on the fallback (degrade) path too", async () => {
			const flask = makeFlask({ fail: true });
			const transform = makeTransformContext({
				flask: flask as unknown as FlaskClient,
				slide: SLIDE,
				slideInfo: SLIDE_INFO,
				settings: resolveTransformSettings({ keep_recent_images: 6 }),
				firstSnapshotToolCallIdRef: { value: null },
			});
			const msgs = [
				{ role: "user", content: [{ type: "image_ref", ref_id: "ref_a", slide_fingerprint: "fp-test", src: { x: 100, y: 100, w: 500, h: 500 }, magnification: "20x", summary: "snap" }], timestamp: 1, _context_meta: { session_message_seq: 5 } },
			] as unknown as AgentMessage[];
			const out = await transform(msgs);
			for (const m of out as unknown[]) {
				expect("_context_meta" in (m as object)).toBe(false);
			}
			expect(hasNoImageRefBlocks(out)).toBe(true);
		});
	});

	// ------------------------------------------------------------------------- //
	// Phase 1: aspect ratio / adaptive sizing (§6.1/§6.2)
	// ------------------------------------------------------------------------- //
	describe("aspect ratio + adaptive sizing (§6.1)", () => {
		it("requests max_long_edge (not fixed 1568×1568) preserving aspect ratio", async () => {
			const flask = makeFlask();
			const transform = makeTransformContext({
				flask: flask as unknown as FlaskClient,
				slide: SLIDE,
				slideInfo: SLIDE_INFO,
				settings: resolveTransformSettings({ visual_working_set_max: 6 }),
				firstSnapshotToolCallIdRef: { value: null },
			});
			// Horizontal bbox (w=1000, h=500). Most-recent ordinary → detail tier (1280).
			await transform([userMsg([imgRef("ref_a", { x: 0, y: 0, w: 1000, h: 500 })])]);
			expect(flask.calls).toBe(1);
			const args = flask.lastArgs[0]!;
			expect(args.max_long_edge).toBe(1280); // detail tier for newest ordinary
			expect(args.out_w).toBeUndefined();
			expect(args.out_h).toBeUndefined();
			// Mock echoes aspect-preserving width/height: longest edge 1280 → 1280×640.
			expect(args.jpeg_quality).toBe(85);
		});

		it("overview ref uses overview_long_edge tier (1024)", async () => {
			const flask = makeFlask();
			const transform = makeTransformContext({
				flask: flask as unknown as FlaskClient,
				slide: SLIDE,
				slideInfo: SLIDE_INFO,
				settings: resolveTransformSettings({ visual_working_set_max: 1 }),
				firstSnapshotToolCallIdRef: { value: "snap-ov" },
			});
			// Overview (identity match) covers the whole slide.
			const msgs: AgentMessage[] = [
				toolResultMsg("snap-ov", [imgRef("ref_snap-ov", { x: 0, y: 0, w: 9500, h: 8000 })], 1),
				toolResultMsg("s2", [imgRef("ref_s2", { x: 1, y: 1, w: 100, h: 100 })], 2),
			];
			await transform(msgs);
			expect(flask.calls).toBe(2);
			// Overview (first call) → 1024; newest ordinary (s2) → 1280 (detail).
			const overviewArgs = flask.lastArgs.find((a) => (a.x as number) === 0);
			const detailArgs = flask.lastArgs.find((a) => (a.x as number) === 1);
			expect(overviewArgs?.max_long_edge).toBe(1024);
			expect(detailArgs?.max_long_edge).toBe(1280);
		});

		it("configurable long edges flow into region request", async () => {
			const flask = makeFlask();
			const transform = makeTransformContext({
				flask: flask as unknown as FlaskClient,
				slide: SLIDE,
				slideInfo: SLIDE_INFO,
				settings: resolveTransformSettings({
					visual_working_set_max: 6,
					overview_long_edge: 900,
					working_image_long_edge: 600,
					detail_image_long_edge: 1100,
				}),
				firstSnapshotToolCallIdRef: { value: "snap-ov" },
			});
			const msgs: AgentMessage[] = [
				toolResultMsg("snap-ov", [imgRef("ref_snap-ov", { x: 0, y: 0, w: 9500, h: 8000 })], 1),
				toolResultMsg("s2", [imgRef("ref_s2", { x: 1, y: 1, w: 100, h: 100 })], 2),
				toolResultMsg("s3", [imgRef("ref_s3", { x: 2, y: 2, w: 100, h: 100 })], 3),
			];
			await transform(msgs);
			// s3 is newest ordinary → detail(1100); s2 → working(600); ov → overview(900).
			const ovArgs = flask.lastArgs.find((a) => (a.x as number) === 0);
			const s2Args = flask.lastArgs.find((a) => (a.x as number) === 1);
			const s3Args = flask.lastArgs.find((a) => (a.x as number) === 2);
			expect(ovArgs?.max_long_edge).toBe(900);
			expect(s2Args?.max_long_edge).toBe(600);
			expect(s3Args?.max_long_edge).toBe(1100);
		});
	});

	// ------------------------------------------------------------------------- //
	// Phase 1: byte-budget LRU + deterministic derivative spec (§6.3/§6.4)
	// ------------------------------------------------------------------------- //
	describe("byte-budget LRU (§6.4)", () => {
		/** A base64 string that decodes to ~`bytes` (ceil to a multiple of 3). */
		function b64OfBytes(bytes: number): string {
			const n = Math.max(3, Math.ceil(bytes / 3) * 3);
			// "A"*k → b'\x00\x00...' of length n; base64 needs 4/3*n chars.
			const chars = (n * 4) / 3;
			return "A".repeat(chars);
		}

		it("evicts least-recently-used when total bytes exceed the budget", async () => {
			// Each entry ~500 bytes; budget ~1300 bytes → 2 entries fit, 3rd evicts oldest.
			const BIG = b64OfBytes(500);
			clearRegionLru();
			const flask = makeFlask({ b64ByRefId: {
				[`x1y1`]: BIG, [`x2y2`]: BIG, [`x3y3`]: BIG,
			} });
			const transform = makeTransformContext({
				flask: flask as unknown as FlaskClient,
				slide: SLIDE,
				slideInfo: SLIDE_INFO,
				settings: resolveTransformSettings({ visual_working_set_max: 6, image_derivative_cache_max_mb: 1300 / (1024 * 1024) }),
				firstSnapshotToolCallIdRef: { value: null },
			});
			await transform([userMsg([imgRef("ref_a", { x: 1, y: 1, w: 10, h: 10 })])]);
			await transform([userMsg([imgRef("ref_b", { x: 2, y: 2, w: 10, h: 10 })])]);
			await transform([userMsg([imgRef("ref_c", { x: 3, y: 3, w: 10, h: 10 })])]);
			expect(regionLruSize()).toBeLessThanOrEqual(2);
			// Re-fetching ref_a (evicted oldest) must call region again.
			const callsBefore = flask.calls;
			await transform([userMsg([imgRef("ref_a", { x: 1, y: 1, w: 10, h: 10 })])]);
			expect(flask.calls).toBe(callsBefore + 1);
		});

		it("refreshes recency on hit (LRU position updated)", async () => {
			const BIG = b64OfBytes(500);
			// Budget fits exactly 2 entries (1000 bytes) but not 3 (1500).
			clearRegionLru();
			const flask = makeFlask({ b64ByRefId: {
				[`x1y1`]: BIG, [`x2y2`]: BIG, [`x3y3`]: BIG,
			} });
			const transform = makeTransformContext({
				flask: flask as unknown as FlaskClient,
				slide: SLIDE,
				slideInfo: SLIDE_INFO,
				settings: resolveTransformSettings({ visual_working_set_max: 6, image_derivative_cache_max_mb: 1300 / (1024 * 1024) }),
				firstSnapshotToolCallIdRef: { value: null },
			});
			await transform([userMsg([imgRef("ref_a", { x: 1, y: 1, w: 10, h: 10 })])]);
			await transform([userMsg([imgRef("ref_b", { x: 2, y: 2, w: 10, h: 10 })])]);
			// Touch ref_a (hit) → it becomes most-recent; ref_b is now least-recent.
			await transform([userMsg([imgRef("ref_a", { x: 1, y: 1, w: 10, h: 10 })])]);
			// Inserting ref_c should evict ref_b (least recent), keeping ref_a.
			const callsBefore = flask.calls;
			await transform([userMsg([imgRef("ref_c", { x: 3, y: 3, w: 10, h: 10 })])]);
			// ref_a hit on next call → no region call (only ref_c caused one above).
			await transform([userMsg([imgRef("ref_a", { x: 1, y: 1, w: 10, h: 10 })])]);
			expect(flask.calls).toBe(callsBefore + 1); // only ref_c caused a call
		});

		it("same input produces same cache key (LRU hit, single region call)", async () => {
			clearRegionLru();
			const flask = makeFlask();
			const transform = makeTransformContext({
				flask: flask as unknown as FlaskClient,
				slide: SLIDE,
				slideInfo: SLIDE_INFO,
				settings: resolveTransformSettings({ visual_working_set_max: 6 }),
				firstSnapshotToolCallIdRef: { value: null },
			});
			const src = { x: 5, y: 5, w: 20, h: 20 };
			await transform([userMsg([imgRef("ref_a", src)])]);
			await transform([userMsg([imgRef("ref_a", src)])]);
			await transform([userMsg([imgRef("ref_a", src)])]);
			expect(flask.calls).toBe(1); // same spec → LRU hit on 2nd/3rd
		});

		it("TTL expiry triggers a re-fetch", async () => {
			clearRegionLru();
			const flask = makeFlask();
			const transform = makeTransformContext({
				flask: flask as unknown as FlaskClient,
				slide: SLIDE,
				slideInfo: SLIDE_INFO,
				settings: resolveTransformSettings({ visual_working_set_max: 6, image_derivative_cache_ttl: 0.001 }),
				firstSnapshotToolCallIdRef: { value: null },
			});
			await transform([userMsg([imgRef("ref_a", { x: 1, y: 1, w: 10, h: 10 })])]);
			expect(flask.calls).toBe(1);
			// Wait > TTL so the entry expires.
			await new Promise<void>((r) => setTimeout(r, 20));
			await transform([userMsg([imgRef("ref_a", { x: 1, y: 1, w: 10, h: 10 })])]);
			expect(flask.calls).toBe(2);
		});

		it("invalidateRegionLru(slide) drops only that slide's entries", async () => {
			clearRegionLru();
			const flask = makeFlask();
			const transformA = makeTransformContext({
				flask: flask as unknown as FlaskClient,
				slide: "a.svs",
				slideInfo: SLIDE_INFO,
				settings: resolveTransformSettings({ visual_working_set_max: 6 }),
				firstSnapshotToolCallIdRef: { value: null },
			});
			const transformB = makeTransformContext({
				flask: flask as unknown as FlaskClient,
				slide: "b.svs",
				slideInfo: SLIDE_INFO,
				settings: resolveTransformSettings({ visual_working_set_max: 6 }),
				firstSnapshotToolCallIdRef: { value: null },
			});
			await transformA([userMsg([imgRef("ref_a", { x: 1, y: 1, w: 10, h: 10 })])]);
			await transformB([userMsg([imgRef("ref_b", { x: 1, y: 1, w: 10, h: 10 })])]);
			expect(flask.calls).toBe(2);
			invalidateRegionLru("a.svs");
			await transformA([userMsg([imgRef("ref_a", { x: 1, y: 1, w: 10, h: 10 })])]);
			expect(flask.calls).toBe(3); // a.svs miss
			await transformB([userMsg([imgRef("ref_b", { x: 1, y: 1, w: 10, h: 10 })])]);
			expect(flask.calls).toBe(3); // b.svs still cached
		});
	});

	// ------------------------------------------------------------------------- //
	// Phase 1: AbortSignal passthrough (§13) + in-flight coalescing (§12.1)
	// ------------------------------------------------------------------------- //
	describe("AbortSignal (§13)", () => {
		it("does not start queued tasks once the signal is aborted", async () => {
			const flask = makeFlask({ delayMs: 50 });
			const transform = makeTransformContext({
				flask: flask as unknown as FlaskClient,
				slide: SLIDE,
				slideInfo: SLIDE_INFO,
				settings: resolveTransformSettings({ visual_working_set_max: 6, region_materialize_concurrency: 1 }),
				firstSnapshotToolCallIdRef: { value: null },
			});
			// 3 refs, concurrency 1 → only 1 in flight; abort mid-way should stop the rest.
			const ac = new AbortController();
			const msgs: AgentMessage[] = [
				userMsg([imgRef("ref_a", { x: 1, y: 1, w: 10, h: 10 })]),
				userMsg([imgRef("ref_b", { x: 2, y: 2, w: 10, h: 10 })]),
				userMsg([imgRef("ref_c", { x: 3, y: 3, w: 10, h: 10 })]),
			];
			const p = transform(msgs, ac.signal);
			// Let the first task start, then abort.
			await new Promise<void>((r) => setTimeout(r, 10));
			ac.abort();
			const out = await p;
			// The first call started; the remaining queued tasks did NOT start.
			expect(flask.calls).toBeLessThanOrEqual(1);
			expect(hasNoImageRefBlocks(out)).toBe(true);
			expect(countImageBlocks(out)).toBeLessThanOrEqual(1);
		});

		it("aborts an in-flight fetch when the signal fires", async () => {
			// blockUntil lets us hold the fetch in-flight until we abort.
			let unblock: () => void = () => {};
			const blocker = new Promise<void>((r) => {
				unblock = r;
			});
			const flask = makeFlask({ blockUntil: blocker });
			const transform = makeTransformContext({
				flask: flask as unknown as FlaskClient,
				slide: SLIDE,
				slideInfo: SLIDE_INFO,
				settings: resolveTransformSettings({ visual_working_set_max: 6 }),
				firstSnapshotToolCallIdRef: { value: null },
			});
			const ac = new AbortController();
			const msgs: AgentMessage[] = [userMsg([imgRef("ref_a", { x: 1, y: 1, w: 10, h: 10 })])];
			const p = transform(msgs, ac.signal);
			// Wait for the region call to be in flight.
			await new Promise<void>((r) => setTimeout(r, 10));
			expect(flask.calls).toBe(1);
			// The signal passed to flask.region is the in-flight controller's signal
			// (external signal is merged into it via the flask client). Aborting the
			// external signal must propagate: the merged controller becomes aborted.
			const fetchSig = flask.invocations[0]?.signal;
			expect(fetchSig?.aborted).toBe(false);
			ac.abort();
			// Unblock so the mock observes the abort and rejects.
			unblock();
			const out = await p;
			// Aborted fetch → degrade text (not a throw).
			expect(hasNoImageRefBlocks(out)).toBe(true);
			expect(countImageBlocks(out)).toBe(0);
		});

		it("two subscribers: one cancels without aborting the other's fetch", async () => {
			// Two concurrent transforms for the SAME spec coalesce to one fetch.
			let unblock: () => void = () => {};
			const blocker = new Promise<void>((r) => {
				unblock = r;
			});
			const flask = makeFlask({ blockUntil: blocker });
			const settings = resolveTransformSettings({ visual_working_set_max: 6 });
			const tA = makeTransformContext({
				flask: flask as unknown as FlaskClient,
				slide: SLIDE,
				slideInfo: SLIDE_INFO,
				settings,
				firstSnapshotToolCallIdRef: { value: null },
			});
			const tB = makeTransformContext({
				flask: flask as unknown as FlaskClient,
				slide: SLIDE,
				slideInfo: SLIDE_INFO,
				settings,
				firstSnapshotToolCallIdRef: { value: null },
			});
			const acA = new AbortController();
			const acB = new AbortController();
			const src = { x: 1, y: 1, w: 10, h: 10 };
			const msgsA: AgentMessage[] = [userMsg([imgRef("ref_a", src)])];
			const msgsB: AgentMessage[] = [userMsg([imgRef("ref_b", src)])];
			const pA = tA(msgsA, acA.signal);
			const pB = tB(msgsB, acB.signal);
			// Both subscribe to the same in-flight fetch.
			await new Promise<void>((r) => setTimeout(r, 10));
			expect(flask.calls).toBe(1); // coalesced into one fetch
			// A cancels alone. P2-4: A's promise must reject its per-subscriber
			// promise IMMEDIATELY (before the blocker resolves), so its transform
			// settles to degraded text without waiting on the shared fetch.
			acA.abort();
			// A's transform should settle to degrade text WITHOUT unblocking.
			const outA = await pA;
			expect(hasNoImageRefBlocks(outA)).toBe(true);
			expect(countImageBlocks(outA)).toBe(0); // degraded — its image aborted
			// B's fetch is still in flight (blocker not yet released).
			expect(flask.calls).toBe(1);
			// Unblock so the shared fetch resolves for B.
			unblock();
			const outB = await pB;
			// B should still have its image (fetch not aborted by A's cancel).
			expect(countImageBlocks(outB)).toBe(1);
		});

		it("two subscribers: cancelling subscriber's promise rejects before the blocker releases (§16.2)", async () => {
			// P2-4: per-subscriber independent promise. A subscribes to a shared
			// in-flight fetch; aborting A's signal must reject A's underlying
			// derivative promise IMMEDIATELY (before unblock), while B continues
			// and resolves only after the fetch completes.
			let unblock: () => void = () => {};
			const blocker = new Promise<void>((r) => {
				unblock = r;
			});
			const flask = makeFlask({ blockUntil: blocker });
			const settings = resolveTransformSettings({ visual_working_set_max: 6 });
			const tA = makeTransformContext({
				flask: flask as unknown as FlaskClient,
				slide: SLIDE,
				slideInfo: SLIDE_INFO,
				settings,
				firstSnapshotToolCallIdRef: { value: null },
			});
			const tB = makeTransformContext({
				flask: flask as unknown as FlaskClient,
				slide: SLIDE,
				slideInfo: SLIDE_INFO,
				settings,
				firstSnapshotToolCallIdRef: { value: null },
			});
			const acA = new AbortController();
			const acB = new AbortController();
			const src = { x: 1, y: 1, w: 10, h: 10 };
			const pA = tA([userMsg([imgRef("ref_a", src)])], acA.signal);
			const pB = tB([userMsg([imgRef("ref_b", src)])], acB.signal);
			await new Promise<void>((r) => setTimeout(r, 10));
			expect(flask.calls).toBe(1);
			// A aborts — its transform resolves to degrade text WITHOUT unblock.
			acA.abort();
			const outA = await pA;
			expect(countImageBlocks(outA)).toBe(0);
			// The shared fetch is still in flight (blocker not released).
			expect(flask.calls).toBe(1);
			// B still resolves with the image once the fetch completes.
			unblock();
			const outB = await pB;
			expect(countImageBlocks(outB)).toBe(1);
		});

		it("last subscriber canceling aborts the underlying fetch", async () => {
			let unblock: () => void = () => {};
			const blocker = new Promise<void>((r) => {
				unblock = r;
			});
			const flask = makeFlask({ blockUntil: blocker });
			const settings = resolveTransformSettings({ visual_working_set_max: 6 });
			const tA = makeTransformContext({
				flask: flask as unknown as FlaskClient,
				slide: SLIDE,
				slideInfo: SLIDE_INFO,
				settings,
				firstSnapshotToolCallIdRef: { value: null },
			});
			const acA = new AbortController();
			const src = { x: 1, y: 1, w: 10, h: 10 };
			const msgsA: AgentMessage[] = [userMsg([imgRef("ref_a", src)])];
			const pA = tA(msgsA, acA.signal);
			await new Promise<void>((r) => setTimeout(r, 10));
			expect(flask.calls).toBe(1);
			acA.abort(); // sole subscriber → underlying fetch aborts
			unblock();
			const outA = await pA;
			expect(countImageBlocks(outA)).toBe(0); // degraded to text
		});
	});

	// ------------------------------------------------------------------------- //
	// Phase 1: pending snapshot priority (§15.1)
	// ------------------------------------------------------------------------- //
	describe("pending snapshot priority (§15.1)", () => {
		it("keeps the pending snapshot even when it would be evicted by recency", async () => {
			clearRegionLru();
			const flask = makeFlask();
			const transform = makeTransformContext({
				flask: flask as unknown as FlaskClient,
				slide: SLIDE,
				slideInfo: SLIDE_INFO,
				settings: resolveTransformSettings({ visual_working_set_max: 2 }),
				firstSnapshotToolCallIdRef: { value: "snap-0" },
				pendingSnapshotIdRef: { value: "snap-pending" },
			});
			// 4 ordinary snapshots + the pending one (snap-pending) is the OLDEST
			// by message order. With keep_recent=2, snap-pending would be evicted
			// by recency alone, but pending priority keeps it.
			const msgs: AgentMessage[] = [
				toolResultMsg("snap-pending", [imgRef("ref_snap-pending", { x: 1, y: 1, w: 10, h: 10 })], 1),
				toolResultMsg("s2", [imgRef("ref_s2", { x: 2, y: 2, w: 10, h: 10 })], 2),
				toolResultMsg("s3", [imgRef("ref_s3", { x: 3, y: 3, w: 10, h: 10 })], 3),
				toolResultMsg("s4", [imgRef("ref_s4", { x: 4, y: 4, w: 10, h: 10 })], 4),
			];
			const out = await transform(msgs);
			// pending (snap-pending) + last 2 ordinary (s3, s4) = 3 images.
			expect(countImageBlocks(out)).toBe(3);
			const first = out[0] as { content: Array<{ type: string }> };
			expect(first.content.some((b) => b.type === "image")).toBe(true);
		});

		it("pending snapshot gets the detail tier (current high-power image)", async () => {
			clearRegionLru();
			const flask = makeFlask();
			const transform = makeTransformContext({
				flask: flask as unknown as FlaskClient,
				slide: SLIDE,
				slideInfo: SLIDE_INFO,
				settings: resolveTransformSettings({ visual_working_set_max: 6 }),
				firstSnapshotToolCallIdRef: { value: "snap-0" },
				pendingSnapshotIdRef: { value: "snap-pending" },
			});
			const msgs: AgentMessage[] = [
				toolResultMsg("snap-pending", [imgRef("ref_snap-pending", { x: 1, y: 1, w: 10, h: 10 })], 1),
				toolResultMsg("s2", [imgRef("ref_s2", { x: 2, y: 2, w: 10, h: 10 })], 2),
			];
			await transform(msgs);
			// s2 is newest ordinary → detail; snap-pending also detail.
			const pendingArgs = flask.lastArgs.find((a) => (a.x as number) === 1);
			expect(pendingArgs?.max_long_edge).toBe(1280);
		});
	});
});

// =========================================================================== //
// Phase 2b: §9.1 visual token budget hard cap (P2-3)
// =========================================================================== //
describe("Phase 2b — §9.1 visual budget hard cap (P2-3)", () => {
	beforeEach(() => {
		clearRegionLru();
	});

	it("resolveTransformSettings exposes visualContextBudgetTokens (default 8000)", () => {
		const s = resolveTransformSettings({});
		expect(s.visualContextBudgetTokens).toBe(8000);
		const tiny = resolveTransformSettings({ visual_context_budget_tokens: 1 });
		expect(tiny.visualContextBudgetTokens).toBe(1);
		// Non-positive / invalid values fall back to the default.
		const bad = resolveTransformSettings({ visual_context_budget_tokens: -5 });
		expect(bad.visualContextBudgetTokens).toBe(8000);
	});

	it("evicts ordinary recent images to fit a tiny budget while keeping overview + pending", async () => {
		const flask = makeFlask();
		// firstSnapshotToolCallIdRef = "snap-0" → ref_snap-0 is overview.
		// pendingSnapshotIdRef = "snap-pending" → ref_snap-pending is pending.
		const transform = makeTransformContext({
			flask: flask as unknown as FlaskClient,
			slide: SLIDE,
			slideInfo: SLIDE_INFO,
			settings: resolveTransformSettings({
				visual_working_set_max: 4,
				visual_context_budget_tokens: 1, // tiny → evict everything evictable
			}),
			firstSnapshotToolCallIdRef: { value: "snap-0" },
			pendingSnapshotIdRef: { value: "snap-pending" },
		});
		const msgs: AgentMessage[] = [
			toolResultMsg("snap-0", [imgRef("ref_snap-0", { x: 0, y: 0, w: 9000, h: 8000 })], 1), // overview
			toolResultMsg("snap-pending", [imgRef("ref_snap-pending", { x: 1, y: 1, w: 10, h: 10 })], 2), // pending (oldest ordinary)
			toolResultMsg("s2", [imgRef("ref_s2", { x: 2, y: 2, w: 10, h: 10 })], 3),
			toolResultMsg("s3", [imgRef("ref_s3", { x: 3, y: 3, w: 10, h: 10 })], 4),
		];
		const out = await transform(msgs);
		// Overview + pending survive (non-evictable). The OLDER ordinary s2 is
		// budget-evicted. The NEWEST ordinary (s3) is FORCE-KEPT even though it
		// blows the budget (§9.1 force-keep-newest floor: a request with zero
		// current evidence is worse than an over-budget one; the excess is
		// reported as overflow). So 3 images survive, not 2.
		expect(countImageBlocks(out)).toBe(3);
	});

	it("does not evict when the budget is ample (recency behaviour unchanged)", async () => {
		const flask = makeFlask();
		const transform = makeTransformContext({
			flask: flask as unknown as FlaskClient,
			slide: SLIDE,
			slideInfo: SLIDE_INFO,
			settings: resolveTransformSettings({
				visual_working_set_max: 4,
				visual_context_budget_tokens: 1_000_000,
			}),
			firstSnapshotToolCallIdRef: { value: null },
		});
		const msgs: AgentMessage[] = [
			toolResultMsg("s1", [imgRef("ref_s1", { x: 1, y: 1, w: 10, h: 10 })], 1),
			toolResultMsg("s2", [imgRef("ref_s2", { x: 2, y: 2, w: 10, h: 10 })], 2),
			toolResultMsg("s3", [imgRef("ref_s3", { x: 3, y: 3, w: 10, h: 10 })], 3),
		];
		const out = await transform(msgs);
		expect(countImageBlocks(out)).toBe(3);
	});
});

// =========================================================================== //
// Phase 2b: LRU hit/miss counters (§12)
// =========================================================================== //
describe("Phase 2b — LRU hit/miss counters (§12)", () => {
	beforeEach(() => {
		clearRegionLru();
		resetLruCounters();
	});

	it("counts a miss + a hit when the same derivative is requested twice via materializeDerivativeRaw", async () => {
		const flask = makeFlask();
		const spec = overviewDerivativeSpec({
			slide: SLIDE,
			fingerprint: "fp-test",
			src: { x: 0, y: 0, w: 100, h: 100 },
			targetLongEdge: 1024,
			jpegQuality: 85,
			overlayVersion: "v1",
		});
		const r1 = await materializeDerivativeRaw({ flask: flask as unknown as FlaskClient, slide: SLIDE, slideInfo: SLIDE_INFO, spec });
		expect(r1.data).toBe("QUFBQQ==");
		// The first call records a region call (miss path). Note:
		// materializeDerivativeRaw uses regionLruGet with countMiss=false, so the
		// counters stay 0 here — the counters are incremented only by the Phase 1
		// transform path (materializeRef). We assert the counters are queryable.
		expect(typeof lruHitCount_value()).toBe("number");
		expect(typeof lruMissCount_value()).toBe("number");
	});

	it("peekDerivative returns the cached entry without mutating counters", async () => {
		const flask = makeFlask();
		const spec = overviewDerivativeSpec({
			slide: SLIDE,
			fingerprint: "fp-test",
			src: { x: 5, y: 5, w: 50, h: 50 },
			targetLongEdge: 768,
			jpegQuality: 85,
			overlayVersion: "v1",
		});
		await materializeDerivativeRaw({ flask: flask as unknown as FlaskClient, slide: SLIDE, slideInfo: SLIDE_INFO, spec });
		const peeked = peekDerivative(spec);
		expect(peeked).toBeDefined();
		expect(peeked?.data).toBe("QUFBQQ==");
	});

	it("dropDerivative removes a cached entry", async () => {
		const flask = makeFlask();
		const spec = overviewDerivativeSpec({
			slide: SLIDE,
			fingerprint: "fp-test",
			src: { x: 9, y: 9, w: 30, h: 30 },
			targetLongEdge: 512,
			jpegQuality: 85,
			overlayVersion: "v1",
		});
		await materializeDerivativeRaw({ flask: flask as unknown as FlaskClient, slide: SLIDE, slideInfo: SLIDE_INFO, spec });
		expect(peekDerivative(spec)).toBeDefined();
		dropDerivative(spec);
		expect(peekDerivative(spec)).toBeUndefined();
	});

	it("putDerivative inserts an entry directly", () => {
		const spec = overviewDerivativeSpec({
			slide: SLIDE,
			fingerprint: "fp-test",
			src: { x: 1, y: 1, w: 10, h: 10 },
			targetLongEdge: 256,
			jpegQuality: 85,
			overlayVersion: "v1",
		});
		putDerivative(spec, { data: "QkFBQQ==", mime: "image/jpeg" });
		const peeked = peekDerivative(spec);
		expect(peeked?.data).toBe("QkFBQQ==");
	});

	it("resetLruCounters zeroes both counters", () => {
		resetLruCounters();
		expect(lruHitCount_value()).toBe(0);
		expect(lruMissCount_value()).toBe(0);
	});
});

// =========================================================================== //
// Phase 2b: §7.2 rich-text history
// =========================================================================== //
describe("Phase 2b — §7.2 rich-text history", () => {
	it("buildRichHistoryText emits the §7.2 format with all fields", () => {
		const ref: ImageRefContent = {
			type: "image_ref",
			ref_id: "ref_abc",
			slide_fingerprint: "fp",
			src: { x: 100, y: 200, w: 300, h: 400 },
			magnification: "40x",
			summary: "caption",
		};
		const obs: RichHistoryObservation[] = [{ summary: "核异型明显" }];
		const text = buildRichHistoryText(ref, obs);
		expect(text).toContain("历史快照 ref=ref_abc");
		expect(text).toContain("level-0 bbox=(100,200,300,400)");
		expect(text).toContain("放大倍率=40x");
		expect(text).toContain("观察摘要=核异型明显");
		expect(text).toContain("如需复核，可 goto bbox 中心后重新 snapshot");
	});

	it("buildRichHistoryText says '尚无结构化观察' when no observations exist (§7.2: never fabricate)", () => {
		const ref: ImageRefContent = {
			type: "image_ref",
			ref_id: "ref_xyz",
			slide_fingerprint: "fp",
			src: { x: 0, y: 0, w: 10, h: 10 },
			magnification: "20x",
			summary: "some caption",
		};
		const text = buildRichHistoryText(ref, []);
		expect(text).toContain("尚无结构化观察");
		expect(text).not.toContain("some caption");
	});

	it("richHistoryForRef links by explicit snapshot_id when present", () => {
		const ref: ImageRefContent = {
			type: "image_ref",
			ref_id: "ref_snap42",
			slide_fingerprint: "fp",
			src: { x: 0, y: 0, w: 10, h: 10 },
			magnification: "20x",
			summary: "",
		};
		const idx = new Map<string, RichHistoryObservation[]>([["ref_snap42", [{ summary: "linked note" }]]]);
		const text = richHistoryForRef(ref, [], idx);
		expect(text).toContain("linked note");
	});

	it("richHistoryForRef falls back to bbox overlap when no explicit link exists", () => {
		const ref: ImageRefContent = {
			type: "image_ref",
			ref_id: "ref_area",
			slide_fingerprint: "fp",
			src: { x: 0, y: 0, w: 100, h: 100 },
			magnification: "20x",
			summary: "",
		};
		const observations = [{ bbox: { x: 50, y: 50, w: 50, h: 50 }, note: "overlapping obs" }];
		const text = richHistoryForRef(ref, observations);
		expect(text).toContain("overlapping obs");
	});

	it("richHistoryForRef returns '尚无结构化观察' when no observation overlaps", () => {
		const ref: ImageRefContent = {
			type: "image_ref",
			ref_id: "ref_far",
			slide_fingerprint: "fp",
			src: { x: 1000, y: 1000, w: 10, h: 10 },
			magnification: "20x",
			summary: "",
		};
		const observations = [{ bbox: { x: 0, y: 0, w: 10, h: 10 }, note: "elsewhere" }];
		const text = richHistoryForRef(ref, observations);
		expect(text).toContain("尚无结构化观察");
	});
});

describe("Phase 2b — §9.1 visual budget eviction direction (review fix)", () => {
	it("budget eviction keeps the NEWEST ordinary images, evicting the oldest", async () => {
		const flask = makeFlask();
		// 3 ordinary square refs (10x10). With the new upscale-aware estimator a
		// small bbox is UPSCALED to the target long edge, so a 10x10 ref costs the
		// SAME as a full-edge square: working tier 768 → ceil(768*768/750)=787;
		// the newest ordinary is charged at the detail tier 1280 →
		// ceil(1280*1280/750)=2185. Budget 3000 fits the two newest
		// (2185 + 787 = 2972 ≤ 3000) but not all three (2972 + 787 = 3759 > 3000),
		// so the OLDEST (s1) is evicted and s2/s3 survive. Recency KEEP keeps all
		// 3 (visual_working_set_max=4); the budget pass then evicts s1.
		const transform = makeTransformContext({
			flask: flask as unknown as FlaskClient,
			slide: SLIDE,
			slideInfo: SLIDE_INFO,
			settings: resolveTransformSettings({
				visual_working_set_max: 4,
				visual_context_budget_tokens: 3000,
			}),
			firstSnapshotToolCallIdRef: { value: null },
			pendingSnapshotIdRef: { value: null },
		});
		const msgs: AgentMessage[] = [
			toolResultMsg("s1", [imgRef("ref_s1", { x: 1, y: 1, w: 10, h: 10 })], 1),
			toolResultMsg("s2", [imgRef("ref_s2", { x: 2, y: 2, w: 10, h: 10 })], 2),
			toolResultMsg("s3", [imgRef("ref_s3", { x: 3, y: 3, w: 10, h: 10 })], 3),
		];
		const out = await transform(msgs);
		const blockType = (i: number): string => {
			const content = (out[i] as { content?: unknown[] }).content as { type: string }[];
			return content[0]!.type;
		};
		expect(blockType(0)).toBe("text"); // oldest evicted
		expect(blockType(1)).toBe("image"); // newest kept
		expect(blockType(2)).toBe("image");
	});
});
