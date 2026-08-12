/**
 * Phase 2b tests: PreparedRequest (§8.2).
 *
 * Verifies the canonicalPayloadHash + imageContentHashes are stable across
 * retries of the same logical call, and that image_ref blocks never appear in
 * the prepared context.
 */
import { describe, expect, it } from "vitest";

import { buildPreparedRequest, hasNoImageRefs, StableContextUnavailableError, type PreparedRequest } from "../src/prepared-request.js";
import type { PersistedAgentMessage } from "../src/session-store.js";

describe("PreparedRequest (§8.2)", () => {
	it("builds a request with a stable canonicalPayloadHash for the same input", () => {
		const messages: PersistedAgentMessage[] = [
			{ role: "user", content: "hello", timestamp: 1 } as PersistedAgentMessage,
			{
				role: "assistant",
				content: [{ type: "text", text: "hi" }],
				timestamp: 2,
			} as unknown as PersistedAgentMessage,
		];
		const a = buildPreparedRequest({
			logicalCallId: "c1",
			checkpointGeneration: 1,
			stablePrefixHash: "abc",
			systemPrompt: "you are an assistant",
			tools: [{ name: "t" }],
			messages,
		});
		const b = buildPreparedRequest({
			logicalCallId: "c2", // different id, same content
			checkpointGeneration: 1,
			stablePrefixHash: "abc",
			systemPrompt: "you are an assistant",
			tools: [{ name: "t" }],
			messages,
		});
		expect(a.canonicalPayloadHash).toBe(b.canonicalPayloadHash);
		expect(a.imageContentHashes).toEqual(b.imageContentHashes);
		expect(a.canonicalPayloadHash).toMatch(/^[0-9a-f]{64}$/);
	});

	it("changes canonicalPayloadHash when messages change", () => {
		const baseMessages: PersistedAgentMessage[] = [
			{ role: "user", content: "hello", timestamp: 1 } as PersistedAgentMessage,
		];
		const a = buildPreparedRequest({
			logicalCallId: "c1",
			checkpointGeneration: 1,
			stablePrefixHash: "abc",
			messages: baseMessages,
		});
		const b = buildPreparedRequest({
			logicalCallId: "c1",
			checkpointGeneration: 1,
			stablePrefixHash: "abc",
			messages: [...baseMessages, { role: "user", content: "more", timestamp: 2 } as PersistedAgentMessage],
		});
		expect(a.canonicalPayloadHash).not.toBe(b.canonicalPayloadHash);
	});

	it("collects imageContentHashes for materialized image blocks, in order", () => {
		const messages: PersistedAgentMessage[] = [
			{
				role: "user",
				content: [
					{ type: "text", text: "q" },
					{ type: "image", data: "AAAA", mimeType: "image/jpeg" },
					{ type: "image", data: "BBBB", mimeType: "image/jpeg" },
				],
				timestamp: 1,
			} as unknown as PersistedAgentMessage,
		];
		const a = buildPreparedRequest({
			logicalCallId: "c1",
			checkpointGeneration: 1,
			stablePrefixHash: "abc",
			messages,
		});
		expect(a.imageContentHashes.length).toBe(2);
		expect(a.imageContentHashes[0]).not.toBe(a.imageContentHashes[1]);
		expect(a.imageContentHashes[0]).toMatch(/^[0-9a-f]{64}$/);
	});

	it("strips _context_meta from messages in the prepared context", () => {
		const messages: PersistedAgentMessage[] = [
			{
				role: "user",
				content: "hello",
				timestamp: 1,
				_context_meta: { session_message_seq: 42 },
			} as PersistedAgentMessage,
		];
		const a = buildPreparedRequest({
			logicalCallId: "c1",
			checkpointGeneration: 1,
			stablePrefixHash: "abc",
			messages,
		});
		// The prepared context's messages must not carry _context_meta.
		for (const m of a.context.messages) {
			expect("_context_meta" in (m as object)).toBe(false);
		}
	});

	it("hasNoImageRefs returns true when no image_ref blocks remain", () => {
		const req: PreparedRequest = {
			logicalCallId: "c1",
			checkpointGeneration: 1,
			stablePrefixHash: "abc",
			context: {
				messages: [
					{ role: "user", content: [{ type: "text", text: "hi" }], timestamp: 1 } as never,
				],
			},
			imageContentHashes: [],
			canonicalPayloadHash: "x",
			estimatedBytes: 0,
			visualBudgetOverflowTokens: 0,
		};
		expect(hasNoImageRefs(req)).toBe(true);
	});

	it("hasNoImageRefs returns false when an image_ref leaks", () => {
		const req: PreparedRequest = {
			logicalCallId: "c1",
			checkpointGeneration: 1,
			stablePrefixHash: "abc",
			context: {
				messages: [
					{
						role: "user",
						content: [{ type: "image_ref", ref_id: "r1", slide_fingerprint: "fp", src: { x: 0, y: 0, w: 1, h: 1 }, magnification: "20x", summary: "" }],
						timestamp: 1,
					} as never,
				],
			},
			imageContentHashes: [],
			canonicalPayloadHash: "x",
			estimatedBytes: 0,
			visualBudgetOverflowTokens: 0,
		};
		expect(hasNoImageRefs(req)).toBe(false);
	});

	it("StableContextUnavailableError carries a reason and is identifiable", () => {
		const e = new StableContextUnavailableError("overview hash mismatch");
		expect(e).toBeInstanceOf(Error);
		expect(e.name).toBe("StableContextUnavailableError");
		expect(e.reason).toBe("overview hash mismatch");
		expect(e.message).toContain("stable_context_unavailable");
	});

	it("produces a non-zero estimatedBytes for messages with images", () => {
		const messages: PersistedAgentMessage[] = [
			{
				role: "user",
				content: [
					{ type: "image", data: "AAAA", mimeType: "image/jpeg" },
				],
				timestamp: 1,
			} as unknown as PersistedAgentMessage,
		];
		const a = buildPreparedRequest({
			logicalCallId: "c1",
			checkpointGeneration: 1,
			stablePrefixHash: "abc",
			messages,
		});
		expect(a.estimatedBytes).toBeGreaterThan(0);
	});
});

// =========================================================================== //
// Phase 3 adapter contract tests (§15)
// =========================================================================== //

describe("PreparedRequest — Phase 3 adapter contract (§15)", () => {
	/**
	 * §15.2 / §8.2: "transient retry 复用同一个 PreparedRequest，对象内图片 hash
	 * 和 payload bytes 不变". The same transformed context, rebuilt twice (as the
	 * retry wrapper would), must produce the same canonicalPayloadHash.
	 */
	it("canonicalPayloadHash is identical when the same transformed context is rebuilt (retry reuse, §8.2)", () => {
		const messages: PersistedAgentMessage[] = [
			{
				role: "user",
				content: [
					{ type: "text", text: "question" },
					{ type: "image", data: "AAAA", mimeType: "image/jpeg" },
				],
				timestamp: 1,
			} as unknown as PersistedAgentMessage,
			{
				role: "assistant",
				content: [{ type: "text", text: "answer" }],
				timestamp: 2,
			} as unknown as PersistedAgentMessage,
		];
		const common = {
			checkpointGeneration: 1,
			stablePrefixHash: "abc",
			systemPrompt: "you are an assistant",
			tools: [{ name: "t" }],
			messages,
		};
		const first = buildPreparedRequest({ logicalCallId: "call-1", ...common });
		const retry = buildPreparedRequest({ logicalCallId: "call-1-retry", ...common });
		expect(first.canonicalPayloadHash).toBe(retry.canonicalPayloadHash);
		expect(first.imageContentHashes).toEqual(retry.imageContentHashes);
	});

	/**
	 * §10 stability: canonicalSerialize sorts object keys recursively, so the
	 * same semantic content with DIFFERENT key insertion order must hash equal.
	 * This is the adapter-layer version of the checkpoint canonical test.
	 */
	it("canonicalPayloadHash is stable across different key-order in message content (§10)", () => {
		// Two message objects with the same fields but different key insertion
		// order. canonicalSerialize sorts keys, so both must hash identically.
		const msgA = {
			role: "user",
			content: [{ type: "text", text: "hi" }],
			timestamp: 1,
			extra: "z",
		} as unknown as PersistedAgentMessage;
		const msgB = {
			extra: "z",
			timestamp: 1,
			content: [{ text: "hi", type: "text" }],
			role: "user",
		} as unknown as PersistedAgentMessage;
		const a = buildPreparedRequest({
			logicalCallId: "c1",
			checkpointGeneration: 1,
			stablePrefixHash: "x",
			messages: [msgA],
		});
		const b = buildPreparedRequest({
			logicalCallId: "c1",
			checkpointGeneration: 1,
			stablePrefixHash: "x",
			messages: [msgB],
		});
		expect(a.canonicalPayloadHash).toBe(b.canonicalPayloadHash);
	});

	/**
	 * §15.2: "transient retry 复用同一个 PreparedRequest". The wrapper does not
	 * rebuild the object on transient retry — it reuses the SAME reference. So
	 * the context object identity (and thus the payload hash) is stable by
	 * construction. This test documents the contract: building twice with the
	 * same input yields the same hash (the wrapper relies on this when it
	 * DOES rebuild after force-compaction).
	 */
	it("two independent builds of the same context produce the same hash (idempotent)", () => {
		const messages: PersistedAgentMessage[] = [
			{ role: "user", content: "hello", timestamp: 1 } as PersistedAgentMessage,
		];
		const args = {
			checkpointGeneration: 2,
			stablePrefixHash: "h",
			systemPrompt: "p",
			tools: [],
			messages,
		};
		const a = buildPreparedRequest({ logicalCallId: "x", ...args });
		const b = buildPreparedRequest({ logicalCallId: "y", ...args });
		// logicalCallId does NOT affect the payload hash (it is metadata only).
		expect(a.canonicalPayloadHash).toBe(b.canonicalPayloadHash);
	});

	/**
	 * §8.3 expected request sequence: same generation → same stable prefix.
	 * The PreparedRequest.checkpointGeneration field records the generation
	 * the request was assembled against; a generation bump invalidates it.
	 */
	it("checkpointGeneration is recorded and distinguishable across generations", () => {
		const messages: PersistedAgentMessage[] = [
			{ role: "user", content: "hi", timestamp: 1 } as PersistedAgentMessage,
		];
		const g1 = buildPreparedRequest({
			logicalCallId: "c1",
			checkpointGeneration: 1,
			stablePrefixHash: "h1",
			messages,
		});
		const g2 = buildPreparedRequest({
			logicalCallId: "c1",
			checkpointGeneration: 2,
			stablePrefixHash: "h2",
			messages,
		});
		expect(g1.checkpointGeneration).toBe(1);
		expect(g2.checkpointGeneration).toBe(2);
		expect(g1.stablePrefixHash).toBe("h1");
		expect(g2.stablePrefixHash).toBe("h2");
	});

	/**
	 * §15.2: "PreparedRequest 在重试层不会被二次序列化为不同载荷". The
	 * PreparedRequest.context is a plain object; the retry wrapper passes the
	 * SAME reference to the streamFn on retry. This test verifies the context
	 * object built by buildPreparedRequest is stable (its messages array is the
	 * cleaned copy, not a re-walked canonical form that could drift).
	 */
	it("context messages are a stable cleaned copy (image_ref-free, meta-free)", () => {
		const messages: PersistedAgentMessage[] = [
			{
				role: "user",
				content: [{ type: "text", text: "q" }],
				timestamp: 1,
				_context_meta: { session_message_seq: 7 },
			} as PersistedAgentMessage,
		];
		const req = buildPreparedRequest({
			logicalCallId: "c1",
			checkpointGeneration: 1,
			stablePrefixHash: "x",
			messages,
		});
		// No _context_meta on the prepared context's messages.
		for (const m of req.context.messages) {
			expect("_context_meta" in (m as object)).toBe(false);
		}
		// No image_ref blocks.
		expect(hasNoImageRefs(req)).toBe(true);
	});
});
