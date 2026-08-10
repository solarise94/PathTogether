/**
 * Phase 2b tests: compaction visual budget (§9.1) + post-compaction checkpoint
 * rebuild (§5.3) + tokens_after re-estimate (§9.3).
 */
import { describe, expect, it } from "vitest";

import {
	buildPostCompactionCheckpoint,
	estimateImageRefTokens,
	estimateSelectedVisualTokens,
	PIXELS_PER_VISUAL_TOKEN,
	DEFAULT_VISUAL_CONTEXT_BUDGET_TOKENS,
	type CompactionOutcome,
} from "../src/compaction.js";
import { REQUEST_SCHEMA_VERSION, type ContextCheckpoint } from "../src/checkpoint.js";
import type { PersistedAgentMessage } from "../src/session-store.js";

// =========================================================================== //
// Visual token estimation (§9.1)
// =========================================================================== //

describe("estimateImageRefTokens (§9.1)", () => {
	it("estimates tokens from pixel dimensions at the target long edge", () => {
		// A 2000×2000 ref at target_long_edge=1024:
		//   longest=2000, scale=1024/2000=0.512
		//   le=1024, se=2000*0.512=1024
		//   pixels=1024*1024=1048576
		//   tokens=ceil(1048576/750)=1399
		const t = estimateImageRefTokens({ w: 2000, h: 2000 }, 1024);
		expect(t).toBeGreaterThan(0);
		expect(t).toBe(Math.ceil((1024 * 1024) / PIXELS_PER_VISUAL_TOKEN));
	});

	it("returns 0 for zero-area refs", () => {
		expect(estimateImageRefTokens({ w: 0, h: 0 }, 1024)).toBe(0);
	});

	it("preserves aspect ratio (a wide ref at long edge 1024)", () => {
		// 4000×1000 ref at target_long_edge=1024:
		//   longest=4000, scale=0.256
		//   le=1024, se=1000*0.256=256
		//   pixels=1024*256=262144
		const t = estimateImageRefTokens({ w: 4000, h: 1000 }, 1024);
		expect(t).toBe(Math.ceil((1024 * 256) / PIXELS_PER_VISUAL_TOKEN));
	});
});

describe("estimateSelectedVisualTokens (§9.1)", () => {
	it("sums per-image estimates and caps at visual_context_budget_tokens", () => {
		const total = estimateSelectedVisualTokens({
			selectedRefs: [
				{ src: { w: 2000, h: 2000 }, target_long_edge: 1024 },
				{ src: { w: 1000, h: 1000 }, target_long_edge: 768 },
			],
			overviewPresent: false,
			overviewLongEdge: 1024,
			visualContextBudgetTokens: 100000, // high cap
		});
		expect(total).toBeGreaterThan(0);
	});

	it("respects the visual_context_budget_tokens cap", () => {
		const total = estimateSelectedVisualTokens({
			selectedRefs: [
				{ src: { w: 4000, h: 4000 }, target_long_edge: 1568 },
				{ src: { w: 4000, h: 4000 }, target_long_edge: 1568 },
			],
			overviewPresent: false,
			overviewLongEdge: 1024,
			visualContextBudgetTokens: 500, // low cap
		});
		expect(total).toBe(500);
	});

	it("reserves the full budget when estimateUnavailable is true (§9.1 rule 3)", () => {
		const total = estimateSelectedVisualTokens({
			selectedRefs: [],
			overviewPresent: false,
			overviewLongEdge: 1024,
			estimateUnavailable: true,
			visualContextBudgetTokens: DEFAULT_VISUAL_CONTEXT_BUDGET_TOKENS,
		});
		expect(total).toBe(DEFAULT_VISUAL_CONTEXT_BUDGET_TOKENS);
	});

	it("includes the overview when present", () => {
		const without = estimateSelectedVisualTokens({
			selectedRefs: [],
			overviewPresent: false,
			overviewLongEdge: 1024,
			visualContextBudgetTokens: 100000,
		});
		const withOverview = estimateSelectedVisualTokens({
			selectedRefs: [],
			overviewPresent: true,
			overviewLongEdge: 1024,
			overviewPixels: { w: 10000, h: 8000 },
			visualContextBudgetTokens: 100000,
		});
		expect(withOverview).toBeGreaterThan(without);
	});
});

// =========================================================================== //
// buildPostCompactionCheckpoint (§5.3)
// =========================================================================== //

describe("buildPostCompactionCheckpoint (§5.3)", () => {
	function makePrev(gen: number, overview: ContextCheckpoint["overview_derivative"] = null): ContextCheckpoint {
		return {
			version: 1,
			generation: gen,
			created_at: 0,
			slide_fingerprint: "fp",
			through_message_seq: 5,
			summary: "old summary",
			annotation_index: "",
			overview_derivative: overview,
			system_prompt_version: "spv",
			tool_schema_hash: "tsh",
			request_schema_version: REQUEST_SCHEMA_VERSION,
			stable_prefix_hash: "old-hash",
		};
	}

	it("bumps the generation by 1", () => {
		const prev = makePrev(3);
		const outcome: CompactionOutcome = {
			messages: [],
			tokensBefore: 1000,
			tokensAfter: 500,
			summary: "new summary",
			retainedTail: [],
		};
		const next = buildPostCompactionCheckpoint({ prev, outcome, postMessages: [], observations: [], systemPrompt: "p" });
		expect(next?.generation).toBe(4);
	});

	it("uses the compaction outcome summary as the new stable summary", () => {
		const prev = makePrev(1);
		const outcome: CompactionOutcome = {
			messages: [],
			tokensBefore: 1000,
			tokensAfter: 500,
			summary: "compacted history",
			retainedTail: [],
		};
		const next = buildPostCompactionCheckpoint({ prev, outcome, postMessages: [], observations: [], systemPrompt: "p" });
		expect(next?.summary).toBe("compacted history");
	});

	it("carries the overview_derivative over unchanged (slide did not change)", () => {
		const od: NonNullable<ContextCheckpoint["overview_derivative"]> = {
			ref_id: "ref_ov",
			target_long_edge: 1024,
			jpeg_quality: 85,
			overlay_version: "v1",
			resize_algorithm: "LANCZOS",
			encoder_id: "pillow",
			encoder_version: "v1",
			mime_type: "image/jpeg",
			content_sha256: "abc",
		};
		const prev = makePrev(1, od);
		const outcome: CompactionOutcome = { messages: [], tokensBefore: 0, tokensAfter: 0, summary: "s", retainedTail: [] };
		const next = buildPostCompactionCheckpoint({ prev, outcome, postMessages: [], observations: [], systemPrompt: "p" });
		expect(next?.overview_derivative).toEqual(od);
	});

	it("recomputes stable_prefix_hash over the new summary", () => {
		const prev = makePrev(1);
		const outcome1: CompactionOutcome = { messages: [], tokensBefore: 0, tokensAfter: 0, summary: "summary A", retainedTail: [] };
		const outcome2: CompactionOutcome = { messages: [], tokensBefore: 0, tokensAfter: 0, summary: "summary B", retainedTail: [] };
		const next1 = buildPostCompactionCheckpoint({ prev, outcome: outcome1, postMessages: [], observations: [], systemPrompt: "p" });
		const next2 = buildPostCompactionCheckpoint({ prev, outcome: outcome2, postMessages: [], observations: [], systemPrompt: "p" });
		expect(next1?.stable_prefix_hash).not.toBe(next2?.stable_prefix_hash);
	});

	it("sets through_message_seq to the highest seq on the post-compaction list", () => {
		const prev = makePrev(1);
		const outcome: CompactionOutcome = { messages: [], tokensBefore: 0, tokensAfter: 0, summary: "s", retainedTail: [] };
		const postMessages: PersistedAgentMessage[] = [
			{ role: "user", content: "a", timestamp: 1, _context_meta: { session_message_seq: 10 } } as PersistedAgentMessage,
			{ role: "user", content: "b", timestamp: 2, _context_meta: { session_message_seq: 15 } } as PersistedAgentMessage,
		];
		const next = buildPostCompactionCheckpoint({ prev, outcome, postMessages, observations: [], systemPrompt: "p" });
		expect(next?.through_message_seq).toBe(15);
	});

	it("returns null when prev is null (no checkpoint to rebuild)", () => {
		const outcome: CompactionOutcome = { messages: [], tokensBefore: 0, tokensAfter: 0, summary: "s", retainedTail: [] };
		const next = buildPostCompactionCheckpoint({ prev: null, outcome, postMessages: [], observations: [], systemPrompt: "p" });
		expect(next).toBeNull();
	});
});
