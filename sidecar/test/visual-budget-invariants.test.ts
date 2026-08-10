/**
 * Phase 3.1 visual-budget calibration invariants.
 *
 * Three invariant classes added from the external review of the §9.1 visual
 * token budget rewrite:
 *
 *   1. The newest ordinary image is charged at the DETAIL tier (1280px), not the
 *      working tier (768px). Charging it at working under-counts ~1398 tokens for
 *      a square image, which changes the eviction outcome. This invariant is
 *      asserted through the REAL Phase 1 selector (transformOnce), not just the
 *      shared helper.
 *   2. The estimator mirrors the server's `_aspect_fit_size`: a small bbox is
 *      UPSCALED so its longest edge equals the target, so a 200×200 ref costs the
 *      same as a full-edge square (parity with app.py).
 *   3. The shared `enforceVisualTokenBudget` either keeps `selectedTokens <=
 *      budget` OR records the excess as `overflowTokens` (never silently drops
 *      below the force-keep-newest floor).
 */
import { describe, expect, it, beforeEach } from "vitest";

import type { AgentMessage } from "@earendil-works/pi-agent-core";

import {
	makeTransformContext,
	resolveTransformSettings,
	clearRegionLru,
} from "../src/transform-context.js";
import {
	estimateImageRefTokens,
	enforceVisualTokenBudget,
	visualBudgetOverflowTokensValue,
	resetVisualBudgetOverflow,
	PIXELS_PER_VISUAL_TOKEN,
} from "../src/compaction.js";
import type { FlaskClient, RegionResult } from "../src/flask-client.js";
import type { SlideInfo } from "../src/tools.js";
import type { ImageRefContent } from "../src/session-store.js";

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

function toolResultMsg(toolCallId: string, blocks: unknown[], ts = Date.now()): AgentMessage {
	return {
		role: "toolResult",
		toolCallId,
		content: blocks as never,
		timestamp: ts,
	} as unknown as AgentMessage;
}

/** Minimal flask mock whose region() returns a deterministic base64 per call. */
function makeFlask(): Pick<FlaskClient, "region"> {
	const region = async (args: Record<string, unknown> & { signal?: AbortSignal }): Promise<RegionResult> => {
		if (args.signal?.aborted) {
			const err = new Error("aborted");
			err.name = "AbortError";
			throw err;
		}
		const le = (args.max_long_edge as number | undefined) ?? 768;
		return {
			image_base64: "QUFBQQ==", // "AAAA"
			mime: "image/jpeg",
			width: le,
			height: le,
			src: { x: args.x as number, y: args.y as number, w: args.w as number, h: args.h as number },
			magnification: null,
			encoder: { id: "pillow", version: "1", resize: "LANCZOS", overlay_version: "v1", jpeg_quality: 85 },
		};
	};
	return { region };
}

beforeEach(() => {
	clearRegionLru();
	resetVisualBudgetOverflow();
});

// =========================================================================== //
// Invariant 1: newest ordinary charged at the DETAIL tier (real selector)
// =========================================================================== //
describe("Phase 3.1 invariant — newest ordinary charged at the detail tier", () => {
	it("detail-tier charging of the newest ordinary changes the eviction outcome", async () => {
		// Two ordinary square refs (s1 oldest, s2 newest). The newest is charged at
		// the detail tier (1280 → ceil(1280*1280/750)=2185); the older one at the
		// working tier (768 → ceil(768*768/750)=787).
		//
		// Budget 2500 DISCRIMINATES the two charging schemes:
		//   - WORKING-only charging (the OLD bug): 787 + 787 = 1574 ≤ 2500 → BOTH
		//     kept. The selector would never evict here.
		//   - DETAIL-tier charging (corrected): walking newest→oldest keeps s2
		//     (force-keep, 2185); s1 would push it to 2185 + 787 = 2972 > 2500, so
		//     s1 is EVICTED. Only the newest survives.
		//
		// Asserting only the newest survives proves the detail tier is in effect.
		const settings = resolveTransformSettings({
			visual_working_set_max: 4,
			visual_context_budget_tokens: 2500,
		});
		expect(settings.detailImageLongEdge).toBe(1280);
		expect(settings.workingImageLongEdge).toBe(768);
		const transform = makeTransformContext({
			flask: makeFlask() as unknown as FlaskClient,
			slide: SLIDE,
			slideInfo: SLIDE_INFO,
			settings,
			firstSnapshotToolCallIdRef: { value: null },
			pendingSnapshotIdRef: { value: null },
		});
		const msgs: AgentMessage[] = [
			toolResultMsg("s1", [imgRef("ref_s1", { x: 1, y: 1, w: 10, h: 10 })], 1), // oldest
			toolResultMsg("s2", [imgRef("ref_s2", { x: 2, y: 2, w: 10, h: 10 })], 2), // newest
		];
		const out = await transform(msgs);
		const blockType = (i: number): string => {
			const content = (out[i] as { content?: unknown[] }).content as { type: string }[];
			return content[0]!.type;
		};
		// Only the newest survives; the oldest is evicted (text). This is the
		// detail-tier-charging outcome — working-only charging would keep BOTH.
		expect(blockType(0)).toBe("text"); // oldest evicted
		expect(blockType(1)).toBe("image"); // newest kept
	});

	it("discriminating token math: detail vs working charging at budget 2500", () => {
		// Directly document the math that the selector relies on. A square ref at
		// the working tier costs 787; at the detail tier it costs 2185.
		const square = { w: 200, h: 200 };
		const workingCost = estimateImageRefTokens(square, 768);
		const detailCost = estimateImageRefTokens(square, 1280);
		expect(workingCost).toBe(Math.ceil((768 * 768) / PIXELS_PER_VISUAL_TOKEN)); // 787
		expect(detailCost).toBe(Math.ceil((1280 * 1280) / PIXELS_PER_VISUAL_TOKEN)); // 2185
		// Budget 2500:
		//   working-only (both at 787): 1574 ≤ 2500 → both fit.
		//   detail-tier newest (2185) + working oldest (787): 2972 > 2500 → oldest evicted.
		expect(workingCost * 2).toBeLessThanOrEqual(2500);
		expect(detailCost + workingCost).toBeGreaterThan(2500);
		expect(detailCost).toBeLessThanOrEqual(2500); // newest alone fits (force-keep)
	});
});

// =========================================================================== //
// Invariant 2: small-bbox upscale parity with app.py _aspect_fit_size
// =========================================================================== //
describe("Phase 3.1 invariant — small-bbox upscale parity (mirrors _aspect_fit_size)", () => {
	it("a 200x200 square ref is upscaled to the target long edge at every tier", () => {
		// app.py _aspect_fit_size scales the longest edge to the target INCLUDING
		// upscaling small bboxes, then clamps each dimension to [1,4096]. A
		// 200x200 square therefore outputs a full-edge square: 768x768 @768,
		// 1280x1280 @1280.
		expect(estimateImageRefTokens({ w: 200, h: 200 }, 768)).toBe(Math.ceil((768 * 768) / PIXELS_PER_VISUAL_TOKEN)); // 787
		expect(estimateImageRefTokens({ w: 200, h: 200 }, 1280)).toBe(Math.ceil((1280 * 1280) / PIXELS_PER_VISUAL_TOKEN)); // 2185
	});

	it("a 10x10 tiny ref is upscaled identically to a full-edge square", () => {
		// Upscale-aware: a tiny bbox is blown up to the target, so it costs the
		// same as a max-edge square. (The OLD estimator clamped to the source size
		// and reported ~11 tokens for 10x10 @768 — under-counting ~776 tokens.)
		expect(estimateImageRefTokens({ w: 10, h: 10 }, 768)).toBe(Math.ceil((768 * 768) / PIXELS_PER_VISUAL_TOKEN)); // 787
		expect(estimateImageRefTokens({ w: 10, h: 10 }, 1280)).toBe(Math.ceil((1280 * 1280) / PIXELS_PER_VISUAL_TOKEN)); // 2185
	});

	it("a non-square ref keeps aspect ratio under upscale (100x200 @768 → 384x768)", () => {
		// longest = 200; scale = 768/200 = 3.84; out = round(100*3.84)=384 by
		// round(200*3.84)=768 → ceil(384*768/750)=394 tokens.
		const ow = 384;
		const oh = 768;
		expect(estimateImageRefTokens({ w: 100, h: 200 }, 768)).toBe(Math.ceil((ow * oh) / PIXELS_PER_VISUAL_TOKEN)); // 394
	});

	it("degenerate / non-positive inputs return 0 tokens", () => {
		expect(estimateImageRefTokens({ w: 0, h: 0 }, 768)).toBe(0);
		expect(estimateImageRefTokens({ w: 10, h: 10 }, 0)).toBe(0);
		expect(estimateImageRefTokens({ w: -1, h: 10 }, 768)).toBe(0);
	});
});

// =========================================================================== //
// Invariant 3: selectedTokens <= budget OR explicit overflow
// =========================================================================== //
describe("Phase 3.1 invariant — final selected <= budget OR explicit overflow", () => {
	it("(a) everything fits → selectedTokens <= budget, overflowTokens = 0", () => {
		const sel = enforceVisualTokenBudget({
			budgetTokens: 10000,
			baselineTokens: 1000,
			ordinary: [
				{ key: "a", tokens: 2000 },
				{ key: "b", tokens: 2000 },
				{ key: "c", tokens: 2000 },
			],
		});
		expect(sel.evictedKeys.size).toBe(0);
		expect(sel.selectedTokens).toBeLessThanOrEqual(10000);
		expect(sel.overflowTokens).toBe(0);
		expect(visualBudgetOverflowTokensValue()).toBe(0);
	});

	it("(b) only the newest ordinary fits → evict older, selectedTokens <= budget, overflow 0", () => {
		// Baseline 1000 + ordinary newest 2000 = 3000 fits a 3000 budget; the
		// next older (5000) would overflow → evicted.
		const sel = enforceVisualTokenBudget({
			budgetTokens: 3000,
			baselineTokens: 1000,
			ordinary: [
				{ key: "old", tokens: 5000 },
				{ key: "new", tokens: 2000 },
			],
		});
		expect(sel.evictedKeys.has("old")).toBe(true);
		expect(sel.evictedKeys.has("new")).toBe(false);
		expect(sel.selectedTokens).toBeLessThanOrEqual(3000);
		expect(sel.overflowTokens).toBe(0);
		expect(visualBudgetOverflowTokensValue()).toBe(0);
	});

	it("(c) even the newest ordinary does not fit → force-keep newest, record overflow", () => {
		// Budget 1000, baseline 0, single ordinary of 5000 → cannot fit even one.
		// The newest is STILL force-kept (current-evidence floor); the excess is
		// reported as overflow AND reflected in the process counter.
		resetVisualBudgetOverflow();
		const sel = enforceVisualTokenBudget({
			budgetTokens: 1000,
			baselineTokens: 0,
			ordinary: [{ key: "only", tokens: 5000 }],
		});
		expect(sel.evictedKeys.size).toBe(0); // newest force-kept, not evicted
		expect(sel.selectedTokens).toBe(5000);
		const excess = 5000 - 1000;
		expect(sel.overflowTokens).toBe(excess);
		expect(sel.overflowTokens).toBeGreaterThan(0);
		expect(visualBudgetOverflowTokensValue()).toBe(excess);
	});

	it("(c-variant) multiple ordinary, none fit → force-keep NEWEST, evict the rest, overflow = excess", () => {
		// Budget 1000; the newest ordinary alone (5000) already overflows, so the
		// older ones are evicted and the newest is force-kept.
		resetVisualBudgetOverflow();
		const sel = enforceVisualTokenBudget({
			budgetTokens: 1000,
			baselineTokens: 0,
			ordinary: [
				{ key: "old", tokens: 4000 },
				{ key: "mid", tokens: 4500 },
				{ key: "new", tokens: 5000 },
			],
		});
		// Newest kept; older two evicted.
		expect(sel.evictedKeys.has("old")).toBe(true);
		expect(sel.evictedKeys.has("mid")).toBe(true);
		expect(sel.evictedKeys.has("new")).toBe(false);
		// Overflow is the excess of the force-kept newest over the budget.
		expect(sel.overflowTokens).toBe(5000 - 1000);
		expect(sel.selectedTokens).toBe(5000);
		expect(visualBudgetOverflowTokensValue()).toBe(5000 - 1000);
	});

	it("(d) baseline alone over budget → protected images force-kept, overflow recorded", () => {
		// Protected images (baseline) alone exceed the budget. Protected images are
		// NEVER evicted (§9.1), so the baseline excess is reported as overflow.
		// With NO ordinary images, nothing else can be kept — overflow = baseline - budget.
		resetVisualBudgetOverflow();
		const sel = enforceVisualTokenBudget({
			budgetTokens: 1000,
			baselineTokens: 9000, // alone over budget
			ordinary: [],
		});
		expect(sel.evictedKeys.size).toBe(0);
		expect(sel.selectedTokens).toBe(9000); // baseline survives
		expect(sel.overflowTokens).toBe(9000 - 1000);
		expect(visualBudgetOverflowTokensValue()).toBe(9000 - 1000);
	});

	it("(d-variant) baseline over budget + ordinary → oldest ordinary evicted, newest force-kept, all excess overflow", () => {
		// When the baseline ALREADY overflows but ordinary images are present, the
		// force-keep-newest floor still applies: the newest ordinary is kept
		// (current-evidence floor is absolute, §9.1) and the older ordinary images
		// are evicted. The ENTIRE running total (baseline + newest) over budget is
		// recorded as overflow — protected images never reduce the floor.
		resetVisualBudgetOverflow();
		const sel = enforceVisualTokenBudget({
			budgetTokens: 1000,
			baselineTokens: 9000, // already over budget
			ordinary: [
				{ key: "old", tokens: 100 },
				{ key: "new", tokens: 200 },
			],
		});
		// Oldest ordinary evicted; newest force-kept (absolute floor).
		expect(sel.evictedKeys.has("old")).toBe(true);
		expect(sel.evictedKeys.has("new")).toBe(false);
		// selectedTokens = baseline + force-kept newest.
		expect(sel.selectedTokens).toBe(9000 + 200);
		// overflow = selectedTokens - budget.
		expect(sel.overflowTokens).toBe(9000 + 200 - 1000);
		expect(visualBudgetOverflowTokensValue()).toBe(9000 + 200 - 1000);
	});

	it("resetVisualBudgetOverflow clears the process counter", () => {
		resetVisualBudgetOverflow();
		enforceVisualTokenBudget({
			budgetTokens: 1,
			baselineTokens: 0,
			ordinary: [{ key: "x", tokens: 9999 }],
		});
		expect(visualBudgetOverflowTokensValue()).toBe(9999 - 1);
		resetVisualBudgetOverflow();
		expect(visualBudgetOverflowTokensValue()).toBe(0);
	});
});
