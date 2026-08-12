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
import { clearRegionLru, configureRegionLru, resolveTransformSettings, DEGRADE_TEXT } from "../src/transform-context.js";
import { estimateImageRefTokens, estimateImagePixelsTokens } from "../src/compaction.js";
import type { PlatformClient, RegionRequest, RegionResult } from "../src/platform/contract.js";
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

/** Minimal platform mock returning a deterministic payload keyed by bbox. */
function makeFlask(opts: { b64?: string; encoderVersion?: string } = {}): Pick<PlatformClient, "region"> {
	const region = async (request: RegionRequest): Promise<RegionResult> => {
		return {
			bytes: Buffer.from(opts.b64 ?? "QUFBQQ==", "base64"),
			mimeType: "image/jpeg",
			width: 1024,
			height: 1024,
			src: { x: request.bbox.x, y: request.bbox.y, w: request.bbox.w, h: request.bbox.h },
			magnification: 20,
			contentSha256: "",
			assetRevision: undefined,
			encoder: {
				id: "pillow",
				version: opts.encoderVersion ?? "test-v1",
				resize: "LANCZOS",
				overlayVersion: "v1",
				jpegQuality: 85,
			},
		};
	};
	return { region } as Pick<PlatformClient, "region">;
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
		flask: makeFlask() as unknown as PlatformClient,
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
	/** In-tier live bytes so eviction tests exercise keep/placeholder, not unknown-size degrade. */
	function liveToolResult(toolCallId: string, ts: number): PersistedAgentMessage {
		return {
			role: "toolResult",
			toolCallId,
			content: [{ type: "image", data: "QUFBQQ==", mimeType: "image/jpeg" }],
			timestamp: ts,
			details: {
				src: { x: 0, y: 0, w: 100, h: 100 },
				width: 512,
				height: 512,
				slide_fingerprint: FINGERPRINT,
			},
		} as unknown as PersistedAgentMessage;
	}

	function liveSnapMessages(count: number): PersistedAgentMessage[] {
		const msgs: PersistedAgentMessage[] = [];
		for (let i = 0; i < count; i++) {
			msgs.push(assistantMsg([{ type: "toolCall", id: `tc-${i}`, name: "snapshot", arguments: {} }], i * 2 + 1));
			msgs.push(liveToolResult(`tc-${i}`, i * 2 + 2));
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

// =========================================================================== //
// makeRequestAssembler — §9.1 visual budget (P2-3) + §12 eviction metric (P2-6)
// =========================================================================== //

describe("makeRequestAssembler — visual budget hard cap (§9.1, P2-3)", () => {
	function refSnapMessages(count: number, w = 200, h = 200): PersistedAgentMessage[] {
		const msgs: PersistedAgentMessage[] = [];
		for (let i = 0; i < count; i++) {
			msgs.push(assistantMsg([{ type: "toolCall", id: `tc-${i}`, name: "snapshot", arguments: {} }], i * 2 + 1));
			msgs.push(toolResultMsg(`tc-${i}`, [imgRef(`ref_snap-${i}`, { x: i * 100, y: 0, w, h })], i * 2 + 2));
		}
		return msgs;
	}

	it("evicts ordinary recent images to fit a very small visual budget, keeping only non-evictable (pending) ones", async () => {
		// Trivial checkpoint (no stable overview). Pending = tc-0 (oldest).
		// A tiny budget forces eviction of every ordinary kept ref; pending must
		// survive (§9.1: overview + pending are non-evictable).
		const cp = makeTrivialCheckpoint(1);
		const deps = makeDeps({
			settings: resolveTransformSettings({
				visual_working_set_max: 4,
				// Tiny positive budget: overview (1024²/750≈1398) + pending detail
				// (1280²/750≈2185) already exceeds this, so all ordinary refs are
				// evicted; only pending survives.
				visual_context_budget_tokens: 1,
			}),
			getSessionSnapshot: async () => ({
				checkpoint: cp,
				observations: [],
				pendingSnapshotId: "snap-0",
				messages: [],
			}),
		});
		const assembler = makeRequestAssembler(deps);
		const out = await assembler(refSnapMessages(4) as AgentMessage[]);
		// Count surviving image blocks. Pending (ref_snap-0) is non-evictable and
		// survives. The ordinary OLDER refs (snap-1, snap-2) are budget-evicted.
		// The NEWEST ordinary (snap-3) is FORCE-KEPT even though it blows the
		// budget (§9.1 force-keep-newest floor; excess reported as overflow). So
		// 2 images survive: pending + force-kept newest ordinary.
		let images = 0;
		for (const m of out) {
			const content = (m as { content?: unknown }).content;
			if (!Array.isArray(content)) continue;
			for (const part of content) {
				if ((part as { type?: string }).type === "image") images += 1;
			}
		}
		expect(images).toBe(2);
	});

	it("does not evict when the budget is ample (behaviour unchanged)", async () => {
		const cp = makeTrivialCheckpoint(1);
		const deps = makeDeps({
			settings: resolveTransformSettings({
				visual_working_set_max: 4,
				visual_context_budget_tokens: 1_000_000, // effectively unlimited
			}),
			getSessionSnapshot: async () => ({
				checkpoint: cp,
				observations: [],
				pendingSnapshotId: null,
				messages: [],
			}),
		});
		const assembler = makeRequestAssembler(deps);
		const out = await assembler(refSnapMessages(4) as AgentMessage[]);
		let images = 0;
		for (const m of out) {
			const content = (m as { content?: unknown }).content;
			if (!Array.isArray(content)) continue;
			for (const part of content) {
				if ((part as { type?: string }).type === "image") images += 1;
			}
		}
		// All 4 ordinary refs fit (no pending, no overview).
		expect(images).toBe(4);
	});
});

describe("makeRequestAssembler — evicted_image_refs metric (§12, P2-6)", () => {
	it("records exactly the evicted ref_ids (kept refs are NOT in the metric)", async () => {
		// Trivial checkpoint (no overview). 6 ordinary refs, keepRecent=4 →
		// the 2 oldest are evicted by recency. The metric must list exactly
		// those 2; the 4 kept refs must NOT appear.
		const cp = makeTrivialCheckpoint(1);
		const recentMsgs: PersistedAgentMessage[] = [];
		for (let i = 0; i < 6; i++) {
			recentMsgs.push(assistantMsg([{ type: "toolCall", id: `tc-${i}`, name: "snapshot", arguments: {} }], i * 2 + 1));
			recentMsgs.push(toolResultMsg(`tc-${i}`, [imgRef(`ref_snap-${i}`, { x: i * 100, y: 0, w: 200, h: 200 })], i * 2 + 2));
		}
		const captured: Array<{ evicted_image_refs: string[] }> = [];
		const deps = makeDeps({
			settings: resolveTransformSettings({ visual_working_set_max: 4, visual_context_budget_tokens: 1_000_000 }),
			getSessionSnapshot: async () => ({
				checkpoint: cp,
				observations: [],
				pendingSnapshotId: null,
				messages: recentMsgs,
			}),
			metricsSink: (m) => captured.push({ evicted_image_refs: m.evicted_image_refs }),
			overviewSrcResolver: () => null,
		});
		const assembler = makeRequestAssembler(deps);
		await assembler(recentMsgs as AgentMessage[]);
		expect(captured.length).toBe(1);
		const evicted = captured[0]!.evicted_image_refs;
		// The 2 oldest (snap-0, snap-1) are evicted; the 4 newest are kept.
		expect(evicted).toEqual(expect.arrayContaining(["ref_snap-0", "ref_snap-1"]));
		expect(evicted).toEqual(expect.not.arrayContaining(["ref_snap-2", "ref_snap-3", "ref_snap-4", "ref_snap-5"]));
		expect(evicted.length).toBe(2);
	});

	it("includes budget-evicted refs in the metric (P2-3 + P2-6 interaction)", async () => {
		// keepRecent=4 but a tiny budget evicts the ordinary refs on top of
		// recency. The metric must reflect the budget evictions too.
		const cp = makeTrivialCheckpoint(1);
		const recentMsgs: PersistedAgentMessage[] = [];
		for (let i = 0; i < 4; i++) {
			recentMsgs.push(assistantMsg([{ type: "toolCall", id: `tc-${i}`, name: "snapshot", arguments: {} }], i * 2 + 1));
			recentMsgs.push(toolResultMsg(`tc-${i}`, [imgRef(`ref_snap-${i}`, { x: i * 100, y: 0, w: 200, h: 200 })], i * 2 + 2));
		}
		const captured: Array<{ evicted_image_refs: string[] }> = [];
		const deps = makeDeps({
			settings: resolveTransformSettings({ visual_working_set_max: 4, visual_context_budget_tokens: 1 }),
			getSessionSnapshot: async () => ({
				checkpoint: cp,
				observations: [],
				pendingSnapshotId: null,
				messages: recentMsgs,
			}),
			metricsSink: (m) => captured.push({ evicted_image_refs: m.evicted_image_refs }),
			overviewSrcResolver: () => null,
		});
		const assembler = makeRequestAssembler(deps);
		await assembler(recentMsgs as AgentMessage[]);
		const evicted = captured[0]!.evicted_image_refs;
		// No pending/overview to protect any, but the §9.1 force-keep-newest floor
		// still FORCE-KEEPS the single newest ordinary (ref_snap-3). So the 3 older
		// ordinary refs are budget-evicted and reported; the newest is kept (over
		// budget, counted as overflow).
		expect(evicted.length).toBe(3);
		expect(evicted).toEqual(expect.arrayContaining(["ref_snap-0", "ref_snap-1", "ref_snap-2"]));
		expect(evicted).toEqual(expect.not.arrayContaining(["ref_snap-3"]));
	});
});

describe("makeRequestAssembler — visual budget eviction direction (review fix)", () => {
	it("budget eviction keeps the NEWEST ordinary refs and evicts the oldest", async () => {
		// 4 ordinary square refs (200x200). Upscale-aware estimator: a square at
		// the working tier 768 → ceil(768*768/750)=787; the newest ordinary is
		// charged at the detail tier 1280 → ceil(1280*1280/750)=2185. Budget 3000
		// fits the two newest (2185 + 787 = 2972 ≤ 3000) but not three (2972 + 787
		// = 3759 > 3000). No overview/pending. The budget pass must evict the
		// OLDEST two (ref_snap-0/1) and keep the NEWEST two (ref_snap-2/3).
		const cp = makeTrivialCheckpoint(1);
		const captured: { evicted?: string[] } = {};
		const deps = makeDeps({
			settings: resolveTransformSettings({
				visual_working_set_max: 4,
				visual_context_budget_tokens: 3000,
			}),
			getSessionSnapshot: async () => ({
				checkpoint: cp,
				observations: [],
				pendingSnapshotId: null,
				messages: [],
			}),
			metricsSink: (m) => {
				captured.evicted = m.evicted_image_refs;
			},
		});
		const assembler = makeRequestAssembler(deps);
		const msgs: PersistedAgentMessage[] = [];
		for (let i = 0; i < 4; i++) {
			msgs.push(assistantMsg([{ type: "toolCall", id: `tc-${i}`, name: "snapshot", arguments: {} }], i * 2 + 1));
			msgs.push(toolResultMsg(`tc-${i}`, [imgRef(`ref_snap-${i}`, { x: i * 100, y: 0, w: 200, h: 200 })], i * 2 + 2));
		}
		const out = await assembler(msgs as AgentMessage[]);
		const blockTypeOf = (toolCallId: string): string => {
			const m = out.find((mm) => (mm as { toolCallId?: string }).toolCallId === toolCallId);
			const content = (m as { content?: unknown[] }).content as { type: string }[];
			return content[0]!.type;
		};
		expect(blockTypeOf("tc-0")).toBe("text"); // oldest evicted
		expect(blockTypeOf("tc-1")).toBe("text");
		expect(blockTypeOf("tc-2")).toBe("image"); // newest kept
		expect(blockTypeOf("tc-3")).toBe("image");
		expect(captured.evicted).toEqual(["ref_snap-0", "ref_snap-1"]);
	});
});

// =========================================================================== //
// makeRequestAssembler — live overview budget/tier + rematerialize degrade
// =========================================================================== //

describe("makeRequestAssembler — live overview budget tier + rematerialize degrade", () => {
	function trivialSnap(overrides: Partial<AssemblerSessionSnapshot> = {}): AssemblerSessionSnapshot {
		return { checkpoint: makeTrivialCheckpoint(), observations: [], pendingSnapshotId: null, messages: [], ...overrides };
	}

	function liveToolResult(
		toolCallId: string,
		opts: {
			data?: string;
			src?: { x: number; y: number; w: number; h: number } | null;
			width?: number;
			height?: number;
			fingerprint?: string;
			ts?: number;
			omitDetails?: boolean;
		} = {},
	): PersistedAgentMessage {
		const live = { type: "image", data: opts.data ?? "TElWRQ==", mimeType: "image/jpeg" };
		if (opts.omitDetails) {
			return {
				role: "toolResult",
				toolCallId,
				content: [live],
				timestamp: opts.ts ?? 1,
			} as unknown as PersistedAgentMessage;
		}
		const details: Record<string, unknown> = {
			width: opts.width ?? 4096,
			height: opts.height ?? 4096,
			slide_fingerprint: opts.fingerprint ?? FINGERPRINT,
		};
		if (opts.src !== null) {
			details.src = opts.src ?? { x: 10, y: 10, w: 500, h: 500 };
		}
		return {
			role: "toolResult",
			toolCallId,
			content: [live],
			timestamp: opts.ts ?? 1,
			details,
		} as unknown as PersistedAgentMessage;
	}

	it("rematerializes kept live overview at overviewLongEdge (not working)", async () => {
		const regionCalls: Array<{ maxLongEdge?: number }> = [];
		const flask: Pick<PlatformClient, "region"> = {
			region: async (request) => {
				regionCalls.push({ maxLongEdge: request.maxLongEdge });
				const le = request.maxLongEdge ?? 1024;
				return {
					bytes: Buffer.from("QkFBQkE=", "base64"),
					mimeType: "image/jpeg",
					width: le,
					height: le,
					src: { x: request.bbox.x, y: request.bbox.y, w: request.bbox.w, h: request.bbox.h },
					magnification: null,
					contentSha256: "",
					assetRevision: undefined,
					encoder: { id: "pillow", version: "1", resize: "LANCZOS", overlayVersion: "v1", jpegQuality: 85 },
				};
			},
		};
		const settings = resolveTransformSettings({ visual_working_set_max: 4, visual_context_budget_tokens: 8000 });
		const deps = makeDeps({
			flask: flask as unknown as PlatformClient,
			settings,
			firstSnapshotToolCallIdRef: { value: "tc-ov" },
			getSessionSnapshot: async () => trivialSnap(),
		});
		const assembler = makeRequestAssembler(deps);
		const msgs = [
			assistantMsg([{ type: "toolCall", id: "tc-ov", name: "snapshot", arguments: {} }], 1),
			liveToolResult("tc-ov", { ts: 2 }),
		];
		const out = await assembler(msgs as AgentMessage[]);
		const first = out.find((m) => (m as { toolCallId?: string }).toolCallId === "tc-ov");
		const content = (first as { content?: Array<{ type: string; data?: string }> }).content!;
		expect(content[0]!.type).toBe("image");
		expect(content[0]!.data).toBe("QkFBQkE=");
		expect(regionCalls.some((c) => c.maxLongEdge === settings.overviewLongEdge)).toBe(true);
		expect(regionCalls.some((c) => c.maxLongEdge === settings.workingImageLongEdge)).toBe(false);
	});

	it("charges kept live overview alone into baseline (exact overflow)", async () => {
		// Discriminating case: ONLY the live overview — no ordinary force-keep.
		// If overview were skipped in baseline, overflow would be 0.
		const overviewLe = 1024;
		const budget = 100;
		const overviewTokens = estimateImagePixelsTokens(overviewLe, overviewLe);
		const captured: { overflow?: number } = {};
		const deps = makeDeps({
			settings: resolveTransformSettings({
				visual_working_set_max: 4,
				visual_context_budget_tokens: budget,
				overview_long_edge: overviewLe,
			}),
			firstSnapshotToolCallIdRef: { value: "tc-ov" },
			getSessionSnapshot: async () => trivialSnap(),
			metricsSink: (m) => {
				captured.overflow = m.visual_budget_overflow_tokens;
			},
		});
		const assembler = makeRequestAssembler(deps);
		const msgs = [
			assistantMsg([{ type: "toolCall", id: "tc-ov", name: "snapshot", arguments: {} }], 1),
			liveToolResult("tc-ov", {
				width: overviewLe,
				height: overviewLe,
				src: { x: 0, y: 0, w: overviewLe, h: overviewLe },
				data: "T1ZFUlZFVw==",
				ts: 2,
			}),
		];
		const out = await assembler(msgs as AgentMessage[]);
		const content = (out.find((m) => (m as { toolCallId?: string }).toolCallId === "tc-ov") as {
			content?: Array<{ type: string }>;
		}).content!;
		expect(content[0]!.type).toBe("image");
		expect(captured.overflow).toBe(overviewTokens - budget);
	});

	it("overview+pending coincidence bills and rematerializes at overview tier (detail < overview)", async () => {
		// Default first unreviewed snapshot is BOTH overview and pending.
		// detail < overview must NOT under-bill or rematerialize at detail.
		const overviewLe = 900;
		const detailLe = 500;
		const budget = 200;
		const src = { x: 10, y: 10, w: 2000, h: 2000 };
		const overviewTokens = estimateImageRefTokens(src, overviewLe);
		const detailTokens = estimateImageRefTokens(src, detailLe);
		expect(overviewTokens).toBeGreaterThan(detailTokens);

		const regionCalls: number[] = [];
		const flask: Pick<PlatformClient, "region"> = {
			region: async (request) => {
				regionCalls.push(request.maxLongEdge ?? 0);
				const le = request.maxLongEdge ?? overviewLe;
				return {
					bytes: Buffer.from("QkFBQkE=", "base64"),
					mimeType: "image/jpeg",
					width: le,
					height: le,
					src: { x: request.bbox.x, y: request.bbox.y, w: request.bbox.w, h: request.bbox.h },
					magnification: null,
					contentSha256: "",
					assetRevision: undefined,
					encoder: { id: "pillow", version: "1", resize: "LANCZOS", overlayVersion: "v1", jpegQuality: 85 },
				};
			},
		};
		const captured: { overflow?: number } = {};
		const deps = makeDeps({
			flask: flask as unknown as PlatformClient,
			settings: resolveTransformSettings({
				visual_working_set_max: 4,
				visual_context_budget_tokens: budget,
				overview_long_edge: overviewLe,
				detail_image_long_edge: detailLe,
			}),
			firstSnapshotToolCallIdRef: { value: "tc-0" },
			getSessionSnapshot: async () => trivialSnap({ pendingSnapshotId: "tc-0" }),
			metricsSink: (m) => {
				captured.overflow = m.visual_budget_overflow_tokens;
			},
		});
		const assembler = makeRequestAssembler(deps);
		const msgs = [
			assistantMsg([{ type: "toolCall", id: "tc-0", name: "snapshot", arguments: {} }], 1),
			liveToolResult("tc-0", { src, width: 4096, height: 4096, ts: 2 }),
		];
		const out = await assembler(msgs as AgentMessage[]);
		const content = (out.find((m) => (m as { toolCallId?: string }).toolCallId === "tc-0") as {
			content?: Array<{ type: string; data?: string }>;
		}).content!;
		expect(content[0]!.type).toBe("image");
		expect(content[0]!.data).toBe("QkFBQkE=");
		expect(regionCalls).toEqual([overviewLe]);
		expect(captured.overflow).toBe(overviewTokens - budget);
		// Prove we did NOT bill the (smaller) detail tier.
		expect(captured.overflow).not.toBe(detailTokens - budget);
	});

	it("degrades oversized live image that has pixels but no rematerialize src", async () => {
		const regionCalls: unknown[] = [];
		const flask: Pick<PlatformClient, "region"> = {
			region: async (request) => {
				regionCalls.push(request);
				return {
					bytes: Buffer.from("QkFBQkE=", "base64"),
					mimeType: "image/jpeg",
					width: 100,
					height: 100,
					src: { x: 0, y: 0, w: 1, h: 1 },
					magnification: null,
					contentSha256: "",
					assetRevision: undefined,
					encoder: { id: "pillow", version: "1", resize: "LANCZOS", overlayVersion: "v1", jpegQuality: 85 },
				};
			},
		};
		const deps = makeDeps({
			flask: flask as unknown as PlatformClient,
			getSessionSnapshot: async () => trivialSnap(),
		});
		const assembler = makeRequestAssembler(deps);
		const msgs = [
			assistantMsg([{ type: "toolCall", id: "tc-1", name: "snapshot", arguments: {} }], 1),
			liveToolResult("tc-1", { src: null, width: 4096, height: 4096, data: "T1JJR0lOQUw=", ts: 2 }),
		];
		const out = await assembler(msgs as AgentMessage[]);
		const content = (out.find((m) => (m as { toolCallId?: string }).toolCallId === "tc-1") as {
			content?: Array<{ type: string; text?: string; data?: string }>;
		}).content!;
		expect(content[0]!.type).toBe("text");
		expect(content[0]!.text).toBe(DEGRADE_TEXT);
		expect(regionCalls.length).toBe(0);
	});

	it("degrades live image with no src and no width/height (unknown size)", async () => {
		const captured: { overflow?: number } = {};
		const deps = makeDeps({
			settings: resolveTransformSettings({ visual_context_budget_tokens: 100 }),
			getSessionSnapshot: async () => trivialSnap(),
			metricsSink: (m) => {
				captured.overflow = m.visual_budget_overflow_tokens;
			},
		});
		const assembler = makeRequestAssembler(deps);
		const msgs = [
			assistantMsg([{ type: "toolCall", id: "tc-1", name: "snapshot", arguments: {} }], 1),
			liveToolResult("tc-1", { omitDetails: true, data: "VU5LTk9XTg==", ts: 2 }),
			// Also cover user-role bare images (no toolResult.details path).
			userMsg([{ type: "image", data: "VVNFUg==", mimeType: "image/jpeg" }], 3),
		];
		const out = await assembler(msgs as AgentMessage[]);
		const toolContent = (out.find((m) => (m as { toolCallId?: string }).toolCallId === "tc-1") as {
			content?: Array<{ type: string; text?: string }>;
		}).content!;
		expect(toolContent[0]!.type).toBe("text");
		expect(toolContent[0]!.text).toBe(DEGRADE_TEXT);
		const userContent = (out.find((m) => (m as { role?: string }).role === "user" && Array.isArray((m as { content?: unknown }).content)) as {
			content?: Array<{ type: string; text?: string }>;
		}).content!;
		expect(userContent.some((p) => p.type === "text" && p.text === DEGRADE_TEXT)).toBe(true);
		// Unknown-size images bill 0 → no visual overflow from the target-tier square path.
		expect(captured.overflow ?? 0).toBe(0);
	});

	it("degrades to text when live rematerialize fails (does not keep oversized original)", async () => {
		const flask: Pick<PlatformClient, "region"> = {
			region: async () => {
				throw new Error("region unavailable");
			},
		};
		const deps = makeDeps({
			flask: flask as unknown as PlatformClient,
			firstSnapshotToolCallIdRef: { value: null },
			getSessionSnapshot: async () => trivialSnap(),
		});
		const assembler = makeRequestAssembler(deps);
		const msgs = [
			assistantMsg([{ type: "toolCall", id: "tc-1", name: "snapshot", arguments: {} }], 1),
			liveToolResult("tc-1", { data: "T1JJR0lOQUw=", width: 4096, height: 4096, ts: 2 }),
		];
		const out = await assembler(msgs as AgentMessage[]);
		const content = (out.find((m) => (m as { toolCallId?: string }).toolCallId === "tc-1") as {
			content?: Array<{ type: string; text?: string; data?: string }>;
		}).content!;
		expect(content[0]!.type).toBe("text");
		expect(content[0]!.text).toBe(DEGRADE_TEXT);
		expect(content[0]!.data).toBeUndefined();
	});

	it("degrades to text when live fingerprint mismatches even if pixels fit the tier", async () => {
		const regionCalls: unknown[] = [];
		const flask: Pick<PlatformClient, "region"> = {
			region: async (request) => {
				regionCalls.push(request);
				return {
					bytes: Buffer.from("QkFBQkE=", "base64"),
					mimeType: "image/jpeg",
					width: 100,
					height: 100,
					src: { x: 0, y: 0, w: 1, h: 1 },
					magnification: null,
					contentSha256: "",
					assetRevision: undefined,
					encoder: { id: "pillow", version: "1", resize: "LANCZOS", overlayVersion: "v1", jpegQuality: 85 },
				};
			},
		};
		const deps = makeDeps({
			flask: flask as unknown as PlatformClient,
			getSessionSnapshot: async () => trivialSnap(),
		});
		const assembler = makeRequestAssembler(deps);
		const msgs = [
			assistantMsg([{ type: "toolCall", id: "tc-1", name: "snapshot", arguments: {} }], 1),
			// Within working tier (512 ≤ 768) — previously would keep stale bytes.
			liveToolResult("tc-1", {
				data: "U1RBTEU=",
				width: 512,
				height: 512,
				fingerprint: "fp-stale-generation",
				ts: 2,
			}),
		];
		const out = await assembler(msgs as AgentMessage[]);
		const content = (out.find((m) => (m as { toolCallId?: string }).toolCallId === "tc-1") as {
			content?: Array<{ type: string; text?: string; data?: string }>;
		}).content!;
		expect(content[0]!.type).toBe("text");
		expect(content[0]!.text).toBe(DEGRADE_TEXT);
		expect(regionCalls.length).toBe(0);
	});
});

// =========================================================================== //
// makeRequestAssembler — overview_enabled product switch (Phase 4 §17 risk 2)
// =========================================================================== //

describe("makeRequestAssembler — overview_enabled=false (§17 risk 2)", () => {
	/** Flask mock that records every region call (so we can prove the overview was never fetched). */
	function makeRecordingFlask(): { flask: Pick<PlatformClient, "region">; calls: Array<{ x: number; y: number; w: number; h: number }> } {
		const calls: Array<{ x: number; y: number; w: number; h: number }> = [];
		const region = async (request: RegionRequest): Promise<RegionResult> => {
			calls.push({ x: request.bbox.x, y: request.bbox.y, w: request.bbox.w, h: request.bbox.h });
			return {
				bytes: Buffer.from("QUFBQQ==", "base64"),
				mimeType: "image/jpeg",
				width: 1024,
				height: 1024,
				src: { x: request.bbox.x, y: request.bbox.y, w: request.bbox.w, h: request.bbox.h },
				magnification: 20,
				contentSha256: "",
				assetRevision: undefined,
				encoder: { id: "pillow", version: "test-v1", resize: "LANCZOS", overlayVersion: "v1", jpegQuality: 85 },
			};
		};
		return { flask: { region } as Pick<PlatformClient, "region">, calls };
	}

	function makeOverviewCheckpoint(throughSeq: number): ContextCheckpoint {
		const od = buildOverviewDerivative({
			ref_id: "ref_ov",
			jpegBase64: "QUFBQQ==",
			target_long_edge: 1024,
			jpeg_quality: 85,
			overlay_version: "v1",
			resize_algorithm: "LANCZOS",
			encoder_id: "pillow",
			encoder_version: "test-v1",
			mime_type: "image/jpeg",
		});
		return {
			...makeContentCheckpoint(2, throughSeq),
			overview_derivative: od.overview_derivative,
		};
	}

	it("omits the stable overview image and does not fetch it when overview_enabled=false", async () => {
		const { flask, calls } = makeRecordingFlask();
		const cp = makeOverviewCheckpoint(0);
		const deps = makeDeps({
			flask: flask as unknown as PlatformClient,
			// settings default to overviewEnabled=true; flip it off here.
			settings: { ...resolveTransformSettings({}), overviewEnabled: false },
			getSessionSnapshot: async () => ({
				checkpoint: cp,
				observations: [],
				pendingSnapshotId: null,
				messages: [],
			}),
			overviewSrcResolver: () => ({ x: 0, y: 0, w: 10000, h: 8000 }),
		});
		const assembler = makeRequestAssembler(deps);
		const out = await assembler([userMsg(["请继续"]) as AgentMessage]);

		// Stable TEXT block (summary) is still present.
		const stableText = out.some((m) => {
			const c = (m as { content?: unknown }).content;
			if (!Array.isArray(c)) return false;
			return c.some((p) => (p as { type?: string; text?: string }).type === "text" && String((p as { text?: string }).text).includes("会话长期记忆"));
		});
		expect(stableText).toBe(true);

		// NO overview image block anywhere in the stable region.
		const overviewLabel = out.some((m) => {
			const c = (m as { content?: unknown }).content;
			if (!Array.isArray(c)) return false;
			return c.some((p) => (p as { type?: string; text?: string }).type === "text" && String((p as { text?: string }).text).includes("稳定全片概览"));
		});
		expect(overviewLabel).toBe(false);

		// The overview was never materialized: zero region calls.
		expect(calls.length).toBe(0);
	});

	it("still materializes the stable overview when overview_enabled=true (default)", async () => {
		const { flask, calls } = makeRecordingFlask();
		const cp = makeOverviewCheckpoint(0);
		const deps = makeDeps({
			flask: flask as unknown as PlatformClient,
			settings: resolveTransformSettings({}), // overviewEnabled defaults to true
			getSessionSnapshot: async () => ({
				checkpoint: cp,
				observations: [],
				pendingSnapshotId: null,
				messages: [],
			}),
			overviewSrcResolver: () => ({ x: 0, y: 0, w: 10000, h: 8000 }),
		});
		const assembler = makeRequestAssembler(deps);
		const out = await assembler([userMsg(["请继续"]) as AgentMessage]);

		// Overview image block present.
		const hasOverview = out.some((m) => {
			const c = (m as { content?: unknown }).content;
			if (!Array.isArray(c)) return false;
			return c.some((p) => (p as { type?: string }).type === "image");
		});
		expect(hasOverview).toBe(true);
		expect(calls.length).toBe(1);
	});
});
