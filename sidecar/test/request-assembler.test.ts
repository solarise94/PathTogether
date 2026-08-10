/**
 * Phase 2b tests: request-context assembler (§3.4/§5/§7.2).
 *
 * Verifies:
 *   - the same generation produces a byte-stable stable region across requests;
 *   - toolResult never leads the request when slicing from through_message_seq;
 *   - evicted image_ref blocks become §7.2 rich text with observations;
 *   - observations are never fabricated when none exist;
 *   - the §7.2 rich-text form carries bbox, magnification, summary, re-visit hint;
 *   - StableContextUnavailableError propagates (not swallowed);
 *   - a trivial (no-content) checkpoint does not swallow the active conversation.
 */
import { beforeEach, describe, expect, it } from "vitest";

import type { AgentMessage } from "@earendil-works/pi-agent-core";

import { makeRequestAssembler, sliceRecentMessages, buildSnapshotObservationIndex, type AssemblerSessionSnapshot, type RequestAssemblerDeps } from "../src/request-assembler.js";
import { buildOverviewDerivative, REQUEST_SCHEMA_VERSION, type CheckpointEnv, type ContextCheckpoint } from "../src/checkpoint.js";
import { clearRegionLru, configureRegionLru, resolveTransformSettings } from "../src/transform-context.js";
import type { FlaskClient, RegionResult } from "../src/flask-client.js";
import type { SlideInfo } from "../src/tools.js";
import type { ImageRefContent, Observation, PersistedAgentMessage } from "../src/session-store.js";
import { StableContextUnavailableError } from "../src/prepared-request.js";

const SLIDE = "test.svs";
const FINGERPRINT = "fp-test";
const SLIDE_INFO: SlideInfo = { width: 10000, height: 8000, levelDownsamples: [1, 2, 4, 8], mpp: 0.5, fingerprint: FINGERPRINT };

beforeEach(() => {
	clearRegionLru();
	configureRegionLru(64 * 1024 * 1024, 1800_000);
});

/** Minimal flask mock returning a deterministic base64 keyed by bbox. */
function makeFlask(opts: { b64?: string; encoderVersion?: string } = {}): Pick<FlaskClient, "region"> {
	const region = async (args: { x: number; y: number; w: number; h: number; max_long_edge?: number; signal?: AbortSignal }): Promise<RegionResult> => {
		return {
			image_base64: opts.b64 ?? "QUFBQQ==",
			mime: "image/jpeg",
			width: 1024,
			height: 1024,
			src: { x: args.x, y: args.y, w: args.w, h: args.h },
			magnification: 20,
			encoder: {
				id: "pillow",
				version: opts.encoderVersion ?? "test-v1",
				resize: "LANCZOS",
				overlay_version: "v1",
				jpeg_quality: 85,
			},
		};
	};
	return { region } as Pick<FlaskClient, "region">;
}

function imgRef(refId: string, src: { x: number; y: number; w: number; h: number }, fingerprint = FINGERPRINT): ImageRefContent {
	return { type: "image_ref", ref_id: refId, slide_fingerprint: fingerprint, src, magnification: "20x", summary: "snap" };
}

function userMsg(blocks: unknown[], ts = Date.now()): PersistedAgentMessage {
	return { role: "user", content: blocks as never, timestamp: ts } as PersistedAgentMessage;
}

function assistantMsg(blocks: unknown[], ts = Date.now()): PersistedAgentMessage {
	return { role: "assistant", content: blocks as never, timestamp: ts } as PersistedAgentMessage;
}

function toolResultMsg(toolCallId: string, blocks: unknown[], ts = Date.now()): PersistedAgentMessage {
	return { role: "toolResult", toolCallId, content: blocks as never, timestamp: ts } as unknown as PersistedAgentMessage;
}

function makeTrivialCheckpoint(gen = 1): ContextCheckpoint {
	return {
		version: 1,
		generation: gen,
		created_at: 0,
		slide_fingerprint: FINGERPRINT,
		through_message_seq: 0,
		summary: "",
		annotation_index: "",
		overview_derivative: null,
		system_prompt_version: "spv",
		tool_schema_hash: "tsh",
		request_schema_version: REQUEST_SCHEMA_VERSION,
		stable_prefix_hash: "sph",
	};
}

function makeContentCheckpoint(gen = 2, throughSeq: number): ContextCheckpoint {
	return {
		...makeTrivialCheckpoint(gen),
		summary: "已确认一处可疑灶。",
		annotation_index: "- 观察#1 (100,200,300×400)：紫染密集",
		through_message_seq: throughSeq,
		stable_prefix_hash: "sph-content",
	};
}

function makeCheckpointEnv(): CheckpointEnv {
	return {
		system_prompt_version: "spv",
		tool_schema_hash: "tsh",
		request_schema_version: REQUEST_SCHEMA_VERSION,
		slide_fingerprint: FINGERPRINT,
		overview_target_long_edge: 1024,
		overview_jpeg_quality: 85,
		overview_overlay_version: "v1",
		overview_resize_algorithm: "LANCZOS",
		overview_encoder_id: "pillow",
	};
}

function makeDeps(overrides: Partial<RequestAssemblerDeps> = {}): RequestAssemblerDeps {
	return {
		flask: makeFlask() as FlaskClient,
		slide: SLIDE,
		slideInfo: SLIDE_INFO,
		settings: resolveTransformSettings({}),
		systemPrompt: "system",
		toolSchemaHash: "tsh",
		firstSnapshotToolCallIdRef: { value: null },
		checkpointEnv: makeCheckpointEnv(),
		getSessionSnapshot: async () => ({ checkpoint: null, observations: [], pendingSnapshotId: null, messages: [] }),
		...overrides,
	} as RequestAssemblerDeps;
}

// =========================================================================== //
// sliceRecentMessages (§5.2)
// =========================================================================== //

describe("sliceRecentMessages (§5.2)", () => {
	it("returns messages strictly after throughSeq", () => {
		const msgs: PersistedAgentMessage[] = [
			{ role: "user", content: "a", timestamp: 1, _context_meta: { session_message_seq: 1 } } as PersistedAgentMessage,
			{ role: "user", content: "b", timestamp: 2, _context_meta: { session_message_seq: 2 } } as PersistedAgentMessage,
			{ role: "user", content: "c", timestamp: 3, _context_meta: { session_message_seq: 3 } } as PersistedAgentMessage,
		];
		const out = sliceRecentMessages(msgs, 1);
		expect(out.length).toBe(2);
		expect((out[0] as { content: string }).content).toBe("b");
	});

	it("drops leading orphan toolResult messages (§5.2 no orphan toolResult)", () => {
		const msgs: PersistedAgentMessage[] = [
			{ role: "user", content: "a", timestamp: 1, _context_meta: { session_message_seq: 1 } } as PersistedAgentMessage,
			// assistant toolCall (seq 2) is BEFORE throughSeq=2, so it's cut.
			assistantMsg([{ type: "toolCall", id: "tc1", name: "snap", arguments: {} }], 2) as PersistedAgentMessage,
			// toolResult (seq 3) is AFTER throughSeq=2 but its toolCall was cut → orphan.
			toolResultMsg("tc1", [{ type: "text", text: "r" }], 3) as PersistedAgentMessage,
			{ role: "user", content: "next", timestamp: 4, _context_meta: { session_message_seq: 4 } } as PersistedAgentMessage,
		];
		// Assign seqs manually so the slice logic can see them.
		(msgs[1] as PersistedAgentMessage & { _context_meta?: { session_message_seq: number } })._context_meta = { session_message_seq: 2 };
		(msgs[2] as PersistedAgentMessage & { _context_meta?: { session_message_seq: number } })._context_meta = { session_message_seq: 3 };
		(msgs[3] as PersistedAgentMessage & { _context_meta?: { session_message_seq: number } })._context_meta = { session_message_seq: 4 };
		const out = sliceRecentMessages(msgs, 2);
		// The orphan toolResult is dropped; the user message leads.
		expect(out.length).toBe(1);
		expect((out[0] as { role: string }).role).toBe("user");
		expect((out[0] as { content: string }).content).toBe("next");
	});

	it("keeps a toolResult whose toolCall is also in the slice", () => {
		const msgs: PersistedAgentMessage[] = [
			{ role: "user", content: "a", timestamp: 1, _context_meta: { session_message_seq: 1 } } as PersistedAgentMessage,
			assistantMsg([{ type: "toolCall", id: "tc1", name: "snap", arguments: {} }], 2) as PersistedAgentMessage,
			toolResultMsg("tc1", [{ type: "text", text: "r" }], 3) as PersistedAgentMessage,
		];
		(msgs[1] as PersistedAgentMessage & { _context_meta?: { session_message_seq: number } })._context_meta = { session_message_seq: 2 };
		(msgs[2] as PersistedAgentMessage & { _context_meta?: { session_message_seq: number } })._context_meta = { session_message_seq: 3 };
		const out = sliceRecentMessages(msgs, 1);
		// Both the toolCall and toolResult are kept (toolCall leads, not orphan).
		expect(out.length).toBe(2);
		expect((out[0] as { role: string }).role).toBe("assistant");
		expect((out[1] as { role: string }).role).toBe("toolResult");
	});

	it("returns all messages when throughSeq is 0", () => {
		const msgs: PersistedAgentMessage[] = [
			{ role: "user", content: "a", timestamp: 1, _context_meta: { session_message_seq: 1 } } as PersistedAgentMessage,
		];
		const out = sliceRecentMessages(msgs, 0);
		expect(out.length).toBe(1);
	});
});

// =========================================================================== //
// buildSnapshotObservationIndex (§7.2)
// =========================================================================== //

describe("buildSnapshotObservationIndex (§7.2)", () => {
	it("links observations by snapshot_id (prefixed to ref_)", () => {
		const obs: Observation[] = [
			{ snapshot_id: "snap1", note: "紫染密集" },
			{ ref_id: "ref_explicit", note: "explicit" },
		];
		const idx = buildSnapshotObservationIndex(obs, []);
		expect(idx.get("ref_snap1")?.[0]?.summary).toBe("紫染密集");
		expect(idx.get("ref_explicit")?.[0]?.summary).toBe("explicit");
	});

	it("returns an empty map when no observations carry links", () => {
		const obs: Observation[] = [{ bbox: { x: 1, y: 2 }, note: "no link" }];
		const idx = buildSnapshotObservationIndex(obs, []);
		expect(idx.size).toBe(0);
	});
});

// =========================================================================== //
// makeRequestAssembler — stable region byte stability (§8.3)
// =========================================================================== //

describe("makeRequestAssembler — stable region (§5.1/§8.3)", () => {
	it("produces a byte-identical stable region across two requests in the same generation", async () => {
		const cp = makeContentCheckpoint(2, 0); // throughSeq=0 → all messages are recent
		const deps = makeDeps({
			getSessionSnapshot: async () => ({
				checkpoint: cp,
				observations: [],
				pendingSnapshotId: null,
				messages: [],
			}),
		});
		const assembler = makeRequestAssembler(deps);
		const msgs1: AgentMessage[] = [userMsg(["q1"]) as AgentMessage];
		const msgs2: AgentMessage[] = [userMsg(["q1"]) as AgentMessage, userMsg(["q2"]) as AgentMessage];
		// The stable region is the prefix; the recent tail changes. We extract
		// the stable block (first message) and verify it is identical.
		const out1 = await assembler(msgs1);
		const out2 = await assembler(msgs2);
		// First message = stable block (summary + annotation index).
		const stable1 = JSON.stringify((out1[0] as { content: unknown }).content);
		const stable2 = JSON.stringify((out2[0] as { content: unknown }).content);
		expect(stable1).toBe(stable2);
		// Stable block contains the summary text.
		expect(stable1).toContain("已确认一处可疑灶");
	});

	it("does not prepend a stable block for a trivial (no-content) checkpoint", async () => {
		const cp = makeTrivialCheckpoint(1);
		const deps = makeDeps({
			getSessionSnapshot: async () => ({
				checkpoint: cp,
				observations: [],
				pendingSnapshotId: null,
				messages: [],
			}),
		});
		const assembler = makeRequestAssembler(deps);
		const msgs: AgentMessage[] = [userMsg([["hello"]]) as AgentMessage];
		const out = await assembler(msgs);
		// No stable block prepended; the output length matches the input.
		expect(out.length).toBe(1);
	});

	it("does not include timestamps or random ids in the stable region (§5.1)", async () => {
		const cp = makeContentCheckpoint(2, 0);
		const deps = makeDeps({
			getSessionSnapshot: async () => ({
				checkpoint: cp,
				observations: [],
				pendingSnapshotId: null,
				messages: [],
			}),
		});
		const assembler = makeRequestAssembler(deps);
		const out1 = await assembler([userMsg(["q"]) as AgentMessage]);
		const out2 = await assembler([userMsg(["q"]) as AgentMessage]);
		// The stable block's timestamp is 0 (fixed), so repeated requests produce
		// the same serialization.
		const ser1 = JSON.stringify(out1[0]);
		const ser2 = JSON.stringify(out2[0]);
		expect(ser1).toBe(ser2);
	});
});

// =========================================================================== //
// makeRequestAssembler — §7.2 rich-text history with observations
// =========================================================================== //

describe("makeRequestAssembler — §7.2 rich-text history", () => {
	it("evicts old image_refs and replaces them with §7.2 rich text carrying observation summary", async () => {
		const cp = makeTrivialCheckpoint(1); // no stable content → all messages recent
		const observations: Observation[] = [
			{ snapshot_id: "snap-old", note: "紫染密集区域" },
		];
		// Many refs so the older ones are evicted (keepRecent default 4).
		const refs: ImageRefContent[] = [];
		for (let i = 0; i < 6; i++) {
			refs.push(imgRef(`ref_snap-old-${i}`, { x: i * 100, y: 0, w: 200, h: 200 }));
		}
		const recentMsgs: AgentMessage[] = refs.map((r) => toolResultMsg(`tc-${r.ref_id}`, [r]) as unknown as AgentMessage);
		const deps = makeDeps({
			getSessionSnapshot: async () => ({
				checkpoint: cp,
				observations,
				pendingSnapshotId: null,
				messages: recentMsgs as PersistedAgentMessage[],
			}),
		});
		const assembler = makeRequestAssembler(deps);
		const out = await assembler(recentMsgs);
		// Evicted refs → rich text blocks (NOT the legacy placeholder).
		const textBlocks = out.flatMap((m) => ((m as { content?: unknown[] }).content || []).filter((p) => (p as { type?: string }).type === "text")) as Array<{ text: string }>;
		const richTexts = textBlocks.filter((t) => t.text.includes("历史快照 ref="));
		expect(richTexts.length).toBeGreaterThan(0);
		// Rich text contains bbox + magnification + re-visit hint (§7.2).
		const sample = richTexts[0]!.text;
		expect(sample).toMatch(/level-0 bbox=/);
		expect(sample).toMatch(/放大倍率=/);
		expect(sample).toMatch(/如需复核/);
	});

	it("does NOT fabricate observations when none exist (§7.2)", async () => {
		const cp = makeTrivialCheckpoint(1);
		const ref = imgRef("ref_lone", { x: 0, y: 0, w: 100, h: 100 });
		const recentMsgs: AgentMessage[] = [toolResultMsg("tc-lone", [ref]) as unknown as AgentMessage];
		const deps = makeDeps({
			getSessionSnapshot: async () => ({
				checkpoint: cp,
				observations: [], // no observations
				pendingSnapshotId: null,
				messages: recentMsgs as PersistedAgentMessage[],
			}),
			settings: resolveTransformSettings({ visual_working_set_max: 0 }), // evict everything
		});
		const assembler = makeRequestAssembler(deps);
		const out = await assembler(recentMsgs);
		const textBlocks = out.flatMap((m) => ((m as { content?: unknown[] }).content || []).filter((p) => (p as { type?: string }).type === "text")) as Array<{ text: string }>;
		const rich = textBlocks.find((t) => t.text.includes("历史快照 ref="));
		expect(rich).toBeDefined();
		// Must say "尚无结构化观察", not a fabricated conclusion.
		expect(rich!.text).toContain("尚无结构化观察");
	});

	it("carries the observation note when a linked observation exists", async () => {
		const cp = makeTrivialCheckpoint(1);
		const ref = imgRef("ref_snap1", { x: 0, y: 0, w: 100, h: 100 });
		const recentMsgs: AgentMessage[] = [toolResultMsg("tc-snap1", [ref]) as unknown as AgentMessage];
		const observations: Observation[] = [{ snapshot_id: "snap1", note: "明确的观察结论" }];
		const deps = makeDeps({
			getSessionSnapshot: async () => ({
				checkpoint: cp,
				observations,
				pendingSnapshotId: null,
				messages: recentMsgs as PersistedAgentMessage[],
			}),
			settings: resolveTransformSettings({ visual_working_set_max: 0 }),
		});
		const assembler = makeRequestAssembler(deps);
		const out = await assembler(recentMsgs);
		const textBlocks = out.flatMap((m) => ((m as { content?: unknown[] }).content || []).filter((p) => (p as { type?: string }).type === "text")) as Array<{ text: string }>;
		const rich = textBlocks.find((t) => t.text.includes("历史快照 ref="));
		expect(rich).toBeDefined();
		expect(rich!.text).toContain("明确的观察结论");
	});
});

// =========================================================================== //
// makeRequestAssembler — no image_ref leaks, no orphan toolResult
// =========================================================================== //

describe("makeRequestAssembler — provider-boundary invariants", () => {
	it("never leaves image_ref blocks in the output", async () => {
		const cp = makeTrivialCheckpoint(1);
		const ref = imgRef("ref1", { x: 0, y: 0, w: 100, h: 100 });
		const recentMsgs: AgentMessage[] = [toolResultMsg("tc1", [ref]) as unknown as AgentMessage];
		const deps = makeDeps({
			getSessionSnapshot: async () => ({ checkpoint: cp, observations: [], pendingSnapshotId: null, messages: recentMsgs as PersistedAgentMessage[] }),
		});
		const assembler = makeRequestAssembler(deps);
		const out = await assembler(recentMsgs);
		for (const m of out) {
			const content = (m as { content?: unknown[] }).content;
			if (!Array.isArray(content)) continue;
			for (const part of content) {
				expect((part as { type?: string }).type).not.toBe("image_ref");
			}
		}
	});

	it("does not produce an orphan toolResult at the start of the request", async () => {
		// Build a checkpoint that COVERS the assistant toolCall but NOT the
		// toolResult. Slicing should drop the orphan toolResult.
		const cp = makeContentCheckpoint(2, 1); // throughSeq=1
		const msgs: PersistedAgentMessage[] = [
			{ role: "user", content: "q", timestamp: 1, _context_meta: { session_message_seq: 1 } } as PersistedAgentMessage,
			assistantMsg([{ type: "toolCall", id: "tc1", name: "snap", arguments: {} }], 2) as PersistedAgentMessage,
			toolResultMsg("tc1", [{ type: "text", text: "r" }], 3) as PersistedAgentMessage,
			{ role: "user", content: "follow-up", timestamp: 4 } as PersistedAgentMessage,
		];
		(msgs[1] as PersistedAgentMessage & { _context_meta?: { session_message_seq: number } })._context_meta = { session_message_seq: 2 };
		(msgs[2] as PersistedAgentMessage & { _context_meta?: { session_message_seq: number } })._context_meta = { session_message_seq: 3 };
		(msgs[3] as PersistedAgentMessage & { _context_meta?: { session_message_seq: number } })._context_meta = { session_message_seq: 4 };
		const deps = makeDeps({
			getSessionSnapshot: async () => ({ checkpoint: cp, observations: [], pendingSnapshotId: null, messages: [] }),
		});
		const assembler = makeRequestAssembler(deps);
		const out = await assembler(msgs as AgentMessage[]);
		// The first message must NOT be a toolResult (orphan).
		expect((out[0] as { role?: string }).role).not.toBe("toolResult");
	});
});

// =========================================================================== //
// makeRequestAssembler — StableContextUnavailableError (§3.2/§13)
// =========================================================================== //

describe("makeRequestAssembler — StableContextUnavailableError (§13)", () => {
	it("raises StableContextUnavailable when the overview ref cannot be found", async () => {
		const od = buildOverviewDerivative({
			ref_id: "ref_missing",
			jpegBase64: "AAAA",
			target_long_edge: 1024,
			jpeg_quality: 85,
			overlay_version: "v1",
			resize_algorithm: "LANCZOS",
			encoder_id: "pillow",
			encoder_version: "v1",
			mime_type: "image/jpeg",
		});
		const cp: ContextCheckpoint = {
			...makeContentCheckpoint(2, 0),
			overview_derivative: od.overview_derivative,
		};
		const deps = makeDeps({
			getSessionSnapshot: async () => ({ checkpoint: cp, observations: [], pendingSnapshotId: null, messages: [] }),
			overviewSrcResolver: () => null, // ref not found
		});
		const assembler = makeRequestAssembler(deps);
		await expect(assembler([userMsg(["q"]) as AgentMessage])).rejects.toBeInstanceOf(StableContextUnavailableError);
	});
});

// =========================================================================== //
// makeRequestAssembler — live image eviction (Phase 1 parity regression)
// =========================================================================== //

describe("makeRequestAssembler — live image eviction (Phase 1 parity)", () => {
	const LIVE = { type: "image", data: "QUFBQQ==", mimeType: "image/jpeg" };

	function liveSnapMessages(count: number): PersistedAgentMessage[] {
		const msgs: PersistedAgentMessage[] = [];
		for (let i = 0; i < count; i++) {
			msgs.push(assistantMsg([{ type: "toolCall", id: `tc-${i}`, name: "snapshot", arguments: {} }], i * 2 + 1));
			msgs.push(toolResultMsg(`tc-${i}`, [LIVE], i * 2 + 2));
		}
		return msgs;
	}

	function trivialSnap(overrides: Partial<AssemblerSessionSnapshot> = {}): AssemblerSessionSnapshot {
		return { checkpoint: makeTrivialCheckpoint(), observations: [], pendingSnapshotId: null, messages: [], ...overrides };
	}

	it("evicts older live image blocks beyond the working-set cap", async () => {
		// Default keepRecentImages = 4 (visual_working_set_max). 6 live snapshots
		// → the 2 oldest become placeholder text; no region calls are needed.
		const deps = makeDeps({ getSessionSnapshot: async () => trivialSnap() });
		const assembler = makeRequestAssembler(deps);
		const out = await assembler(liveSnapMessages(6) as AgentMessage[]);
		let images = 0;
		let placeholders = 0;
		for (const m of out) {
			const content = (m as { content?: unknown }).content;
			if (!Array.isArray(content)) continue;
			for (const part of content) {
				const t = (part as { type?: string }).type;
				if (t === "image") images += 1;
				if (t === "text" && String((part as { text?: string }).text).includes("历史快照已省略")) placeholders += 1;
			}
		}
		expect(images).toBe(4);
		expect(placeholders).toBe(2);
	});

	it("keeps the pending live image even when it would be evicted by recency", async () => {
		// Pending = OLDEST snapshot (tc-0); without priority it would be evicted.
		const deps = makeDeps({ getSessionSnapshot: async () => trivialSnap({ pendingSnapshotId: "tc-0" }) });
		const assembler = makeRequestAssembler(deps);
		const out = await assembler(liveSnapMessages(6) as AgentMessage[]);
		// Expect: pending (tc-0) + last 4 ordinary kept = 5 images; 1 placeholder.
		let images = 0;
		let placeholders = 0;
		for (const m of out) {
			const content = (m as { content?: unknown }).content;
			if (!Array.isArray(content)) continue;
			for (const part of content) {
				const t = (part as { type?: string }).type;
				if (t === "image") images += 1;
				if (t === "text" && String((part as { text?: string }).text).includes("历史快照已省略")) placeholders += 1;
			}
		}
		expect(images).toBe(5);
		expect(placeholders).toBe(1);
	});

	it("keeps the live overview image when the checkpoint has no stable overview", async () => {
		// firstSnapshotToolCallIdRef marks tc-0 as the overview; no stable
		// overview in the trivial checkpoint → the live overview must survive.
		const deps = makeDeps({
			firstSnapshotToolCallIdRef: { value: "tc-0" },
			getSessionSnapshot: async () => trivialSnap(),
		});
		const assembler = makeRequestAssembler(deps);
		const out = await assembler(liveSnapMessages(6) as AgentMessage[]);
		const first = out.find((m) => (m as { toolCallId?: string }).toolCallId === "tc-0");
		const content = (first as { content?: unknown[] }).content as { type: string }[];
		expect(content.some((p) => p.type === "image")).toBe(true);
	});
});
