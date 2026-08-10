/**
 * Phase 2b tests: overview derivative back-fill + hash verification (§3.2/§6.3/§10/§13).
 *
 * Verifies:
 *   - selectOverviewRef prefers identity then first >90% coverage;
 *   - buildOverviewDerivative computes content_sha256 over decoded bytes;
 *   - computeDerivativeContentHash matches for identical bytes, differs otherwise;
 *   - buildStablePrefixObject produces a stable canonical form.
 */
import { describe, expect, it } from "vitest";

import {
	buildOverviewDerivative,
	buildStablePrefixObject,
	canonicalSerialize,
	computeDerivativeContentHash,
	selectOverviewRef,
	stablePrefixHash,
	REQUEST_SCHEMA_VERSION,
	type ContextCheckpoint,
} from "../src/checkpoint.js";

describe("selectOverviewRef (§7.3)", () => {
	it("prefers the recorded identity (first snapshot toolCallId)", () => {
		const messages = [
			{
				role: "toolResult",
				content: [{ type: "image_ref", ref_id: "ref_snap1", slide_fingerprint: "fp", src: { x: 0, y: 0, w: 9000, h: 8000 }, magnification: "1x", summary: "" }],
			},
			{
				role: "toolResult",
				content: [{ type: "image_ref", ref_id: "ref_other", slide_fingerprint: "fp", src: { x: 0, y: 0, w: 9500, h: 8000 }, magnification: "1x", summary: "" }],
			},
		];
		const out = selectOverviewRef({ messages, firstSnapshotToolCallId: "snap1", slideWidth: 10000 });
		expect(out?.ref_id).toBe("ref_snap1");
	});

	it("falls back to the first >90% coverage image_ref when no identity", () => {
		const messages = [
			{
				role: "toolResult",
				content: [{ type: "image_ref", ref_id: "ref_small", slide_fingerprint: "fp", src: { x: 0, y: 0, w: 500, h: 500 }, magnification: "20x", summary: "" }],
			},
			{
				role: "toolResult",
				content: [{ type: "image_ref", ref_id: "ref_big", slide_fingerprint: "fp", src: { x: 0, y: 0, w: 9500, h: 8000 }, magnification: "1x", summary: "" }],
			},
		];
		const out = selectOverviewRef({ messages, firstSnapshotToolCallId: null, slideWidth: 10000 });
		expect(out?.ref_id).toBe("ref_big");
	});

	it("returns null when no image_ref covers >90% and no identity", () => {
		const messages = [
			{
				role: "toolResult",
				content: [{ type: "image_ref", ref_id: "ref_small", slide_fingerprint: "fp", src: { x: 0, y: 0, w: 500, h: 500 }, magnification: "20x", summary: "" }],
			},
		];
		const out = selectOverviewRef({ messages, firstSnapshotToolCallId: null, slideWidth: 10000 });
		expect(out).toBeNull();
	});
});

describe("buildOverviewDerivative + content_sha256 (§6.3)", () => {
	it("computes content_sha256 over decoded JPEG bytes (not the base64 string)", () => {
		const r = buildOverviewDerivative({
			ref_id: "ref1",
			jpegBase64: "AAAA", // decodes to 3 bytes 0x00 0x00 0x00
			target_long_edge: 1024,
			jpeg_quality: 85,
			overlay_version: "v1",
			resize_algorithm: "LANCZOS",
			encoder_id: "pillow",
			encoder_version: "v1",
			mime_type: "image/jpeg",
		});
		expect(r.content_sha256).toMatch(/^[0-9a-f]{64}$/);
		// Same bytes → same hash.
		expect(computeDerivativeContentHash("AAAA")).toBe(r.content_sha256);
	});

	it("produces different hashes for different bytes", () => {
		const a = computeDerivativeContentHash("AAAA");
		const b = computeDerivativeContentHash("AAAB");
		expect(a).not.toBe(b);
	});

	it("records the full encoding spec in overview_derivative", () => {
		const r = buildOverviewDerivative({
			ref_id: "ref1",
			jpegBase64: "AAAA",
			target_long_edge: 1024,
			jpeg_quality: 85,
			overlay_version: "v1",
			resize_algorithm: "LANCZOS",
			encoder_id: "pillow",
			encoder_version: "v1",
			mime_type: "image/jpeg",
		});
		expect(r.overview_derivative).toMatchObject({
			ref_id: "ref1",
			target_long_edge: 1024,
			jpeg_quality: 85,
			overlay_version: "v1",
			resize_algorithm: "LANCZOS",
			encoder_id: "pillow",
			encoder_version: "v1",
			mime_type: "image/jpeg",
			content_sha256: r.content_sha256,
		});
	});

	it("base64 padding does not change the hash (decoded bytes are identical)", () => {
		// "AAAA" (4 chars, no padding) and "AAAA=" (5 chars, 1 padding) both
		// represent the same 3 bytes when decoded — wait, actually they differ.
		// "AAAA" → 3 bytes (0x00 0x00 0x00). "AAAA=" → floor(5*3/4)-1 = 2 bytes.
		// So we verify the more meaningful case: the same bytes encoded with
		// different padding representations still hash identically.
		const buf = Buffer.from([0, 0, 0, 16]);
		const b64a = buf.toString("base64"); // "AAAAEA==" (with padding)
		const b64b = buf.toString("base64").replace(/=+$/, ""); // "AAAAEA" (no padding)
		expect(computeDerivativeContentHash(b64a)).toBe(computeDerivativeContentHash(b64b));
	});
});

describe("buildStablePrefixObject (§10)", () => {
	it("produces a byte-stable canonical form for the same inputs", () => {
		const args = {
			systemPrompt: "prompt",
			system_prompt_version: "spv",
			tool_schema_hash: "tsh",
			request_schema_version: REQUEST_SCHEMA_VERSION,
			slide_fingerprint: "fp",
			summary: "summary",
			annotation_index: "idx",
			overview_derivative: null,
		};
		const a = stablePrefixHash(buildStablePrefixObject(args));
		const b = stablePrefixHash(buildStablePrefixObject({ ...args }));
		expect(a).toBe(b);
	});

	it("changes when the overview derivative changes", () => {
		const base = {
			systemPrompt: "p",
			system_prompt_version: "spv",
			tool_schema_hash: "tsh",
			request_schema_version: REQUEST_SCHEMA_VERSION,
			slide_fingerprint: "fp",
			summary: "s",
			annotation_index: "",
		};
		const od: NonNullable<ContextCheckpoint["overview_derivative"]> = {
			ref_id: "r",
			target_long_edge: 1024,
			jpeg_quality: 85,
			overlay_version: "v1",
			resize_algorithm: "LANCZOS",
			encoder_id: "pillow",
			encoder_version: "v1",
			mime_type: "image/jpeg",
			content_sha256: "abc",
		};
		const a = stablePrefixHash(buildStablePrefixObject({ ...base, overview_derivative: null }));
		const b = stablePrefixHash(buildStablePrefixObject({ ...base, overview_derivative: od }));
		expect(a).not.toBe(b);
	});

	it("changes when the summary changes", () => {
		const base = {
			systemPrompt: "p",
			system_prompt_version: "spv",
			tool_schema_hash: "tsh",
			request_schema_version: REQUEST_SCHEMA_VERSION,
			slide_fingerprint: "fp",
			annotation_index: "",
			overview_derivative: null as ContextCheckpoint["overview_derivative"],
		};
		const a = stablePrefixHash(buildStablePrefixObject({ ...base, summary: "v1" }));
		const b = stablePrefixHash(buildStablePrefixObject({ ...base, summary: "v2" }));
		expect(a).not.toBe(b);
	});
});

describe("canonicalSerialize integration with stablePrefixHash", () => {
	it("the same stable-prefix object serializes identically regardless of key insertion order", () => {
		const a = canonicalSerialize({ summary: "s", overview_derivative: null, annotation_index: "" });
		const b = canonicalSerialize({ annotation_index: "", overview_derivative: null, summary: "s" });
		expect(a).toBe(b);
	});
});
