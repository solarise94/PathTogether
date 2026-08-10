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
