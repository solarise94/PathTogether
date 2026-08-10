/**
 * AI reading assistant sidecar — PreparedRequest (Phase 2b, §8.2).
 *
 * Promotes the implicit `currentContext` reuse in agent-runner's retry wrapper
 * into a verifiable object so that:
 *
 *   - ordinary transient retries (2/4/8s backoff, up to 3 attempts) reuse the
 *     SAME prepared request — the same canonical payload hash, the same image
 *     content hashes — rather than relying on the region LRU happening to stay
 *     warm;
 *   - force-compaction (which bumps the checkpoint generation) releases the old
 *     PreparedRequest and assembles exactly one new object before retrying;
 *   - StableContextUnavailableError (§3.2/§13: overview bytes missing or hash
 *     drift after one rebuild) is surfaced as a retryable error that shares the
 *     same retry budget as transient errors, instead of starting an independent
 *     recursive retry loop.
 *
 * A PreparedRequest lives only for the duration of one logical model call:
 * success, non-retryable error, cancellation, or generation bump all release it.
 * It is NOT written to {@link SessionData}, does not enter the canonical
 * transcript, and at most one is held per active session. Image content reuses
 * the immutable bytes already cached in the derivative LRU; we do not copy them.
 *
 * Phase 2b computes {@link PreparedRequest.canonicalPayloadHash} over the
 * sidecar-side canonical object (messages + system + tools). Phase 3 will add
 * payload-level contract tests once the provider adapter exposes a canonical
 * post-conversion form; see {@link buildPreparedRequest} for the deviation note.
 */
import { createHash } from "node:crypto";
import type { AgentMessage } from "@earendil-works/pi-agent-core";

import { canonicalSerialize, sha256Hex } from "./checkpoint.js";
import { isImageContent, isImageRefContent, stripContextMeta, type PersistedAgentMessage } from "./session-store.js";

// =========================================================================== //
// Errors
// =========================================================================== //

/**
 * Raised when the stable region cannot be assembled for the current generation
 * (§3.2/§13): the overview derivative could not be materialized, its
 * content_sha256 did not match even after one rebuild, or the slide fingerprint
 * changed and the old checkpoint had to be retired.
 *
 * This is a RETRYABLE error: agent-runner's retry layer treats it exactly like
 * a transient network error — it shares the same 3-attempt budget and 2/4/8s
 * backoff and does NOT start its own recursive retry loop. When the budget is
 * exhausted the logical call ends with this error and the checkpoint generation
 * is NOT bumped (§3.2: "耗尽后结束本次调用并报告错误").
 */
export class StableContextUnavailableError extends Error {
	readonly reason: string;
	constructor(reason: string) {
		super(`stable_context_unavailable: ${reason}`);
		this.name = "StableContextUnavailableError";
		this.reason = reason;
	}
}

// =========================================================================== //
// PreparedRequest (§8.2)
// =========================================================================== //

/**
 * The opaque "context" object we hand to the provider streamFn. Phase 2b keeps
 * this loose-typed (`unknown` at the boundary) because pi's `LlmContext` shape
 * is owned by the pi/provider layer; the assembler builds a plain object with
 * `{systemPrompt?, tools?, messages}` and the provider adapter serializes it.
 */
export type LlmContext = {
	systemPrompt?: string;
	tools?: unknown[];
	messages: AgentMessage[];
	[k: string]: unknown;
};

/**
 * One fully-assembled, ready-to-send model request bound to a checkpoint
 * generation (§8.2). All transient retries of the same logical call reuse this
 * object; force-compaction releases it and builds a new one.
 *
 *   - `logicalCallId`: a caller-supplied id unique per logical model call (used
 *     only for metrics/logging; not persisted).
 *   - `checkpointGeneration`: the generation this request was assembled against.
 *     A bump invalidates the object.
 *   - `stablePrefixHash`: the canonical sha256 of the stable region (system +
 *     tools + summary + overview + annotation index). Must be byte-identical
 *     across all requests in the same generation (§8.3).
 *   - `context`: the actual payload handed to the provider streamFn.
 *   - `imageContentHashes`: sha256 of each materialized image's base64 bytes,
 *     in request order. Stable across retries of the same object.
 *   - `canonicalPayloadHash`: sha256 of the canonical-serialized sidecar object
 *     ({system, tools, messages}). Stable across retries; Phase 3 will add the
 *     post-provider-conversion payload hash.
 *   - `estimatedBytes`: rough byte estimate for memory accounting (§8.2).
 */
export interface PreparedRequest {
	logicalCallId: string;
	checkpointGeneration: number;
	stablePrefixHash: string;
	context: LlmContext;
	imageContentHashes: string[];
	canonicalPayloadHash: string;
	estimatedBytes: number;
}

// =========================================================================== //
// Builders
// =========================================================================== //

/**
 * Assemble a {@link PreparedRequest} from the already-transformed message list
 * and the stable-prefix hash computed by the assembler.
 *
 * Steps (§8.2):
 *   1. strip `_context_meta` from every message (Provider boundary, §10);
 *   2. collect sha256 of each materialized image's base64 bytes, in order;
 *   3. canonical-serialize `{systemPrompt, tools, messages}` and sha256 it →
 *      `canonicalPayloadHash`;
 *   4. estimate byte size (images + canonical payload length).
 *
 * Deviation note (Phase 3): `canonicalPayloadHash` is computed over the
 * sidecar-side canonical object, not the post-provider-conversion wire payload.
 * The openai-completions streamSimple serializes inside pi; we cannot hash that
 * form without an adapter hook. Phase 3's adapter contract tests will cover the
 * payload-level hash once the adapter exposes a canonical post-conversion form.
 */
export function buildPreparedRequest(args: {
	logicalCallId: string;
	checkpointGeneration: number;
	stablePrefixHash: string;
	systemPrompt?: string;
	tools?: unknown[];
	messages: PersistedAgentMessage[] | AgentMessage[];
}): PreparedRequest {
	const cleanMessages = stripContextMeta(args.messages as PersistedAgentMessage[]) as AgentMessage[];
	const imageHashes = collectImageContentHashes(cleanMessages);

	// Canonical payload: {system, tools, messages}. Keys sorted recursively by
	// canonicalSerialize; undefined fields omitted.
	const payloadObj = {
		systemPrompt: args.systemPrompt,
		tools: args.tools,
		messages: cleanMessages,
	};
	const canonical = canonicalSerialize(payloadObj);
	const canonicalPayloadHash = sha256Hex(canonical);

	const estimatedBytes = Buffer.byteLength(canonical, "utf8") + imageHashes.reduce((acc, _h, i) => acc + estimateImageBytes(cleanMessages, i), 0);

	return {
		logicalCallId: args.logicalCallId,
		checkpointGeneration: args.checkpointGeneration,
		stablePrefixHash: args.stablePrefixHash,
		context: {
			...(args.systemPrompt !== undefined ? { systemPrompt: args.systemPrompt } : {}),
			...(args.tools !== undefined ? { tools: args.tools } : {}),
			messages: cleanMessages,
		},
		imageContentHashes: imageHashes,
		canonicalPayloadHash,
		estimatedBytes,
	};
}

/**
 * Collect sha256 of each materialized image block's base64 bytes, in the order
 * they appear across the message stream. Used as the per-image identity for
 * cache-hit verification (§8.2: "imageContentHashes 不变").
 *
 * Only real `image` blocks contribute (not `image_ref`, which must never reach
 * the provider). We hash the decoded raw bytes, not the base64 string, so a
 * change in base64 line wrapping or padding does not affect the hash.
 */
export function collectImageContentHashes(messages: AgentMessage[]): string[] {
	const hashes: string[] = [];
	for (const m of messages) {
		const content = (m as { content?: unknown }).content;
		if (!Array.isArray(content)) continue;
		for (const part of content) {
			if (part && isImageContent(part)) {
				const raw = base64ToBytes(part.data);
				hashes.push(sha256HexOfBytes(raw));
			}
		}
	}
	return hashes;
}

/**
 * Quick assertion helper for tests / metrics: returns true when the request
 * payload contains zero `image_ref` blocks (Provider boundary invariant, §16.1).
 */
export function hasNoImageRefs(req: PreparedRequest): boolean {
	for (const m of req.context.messages) {
		const content = (m as { content?: unknown }).content;
		if (!Array.isArray(content)) continue;
		for (const part of content) {
			if (part && isImageRefContent(part)) return false;
		}
	}
	return true;
}

// ------------------------------------------------------------------------- //
// Internal helpers
// ------------------------------------------------------------------------- //

function base64ToBytes(b64: string): Buffer {
	// Buffer.from handles base64 decoding including padding. Empty string → 0
	// bytes (we still hash it so an empty image is distinguishable from absent).
	try {
		return Buffer.from(b64 || "", "base64");
	} catch {
		return Buffer.alloc(0);
	}
}

function sha256HexOfBytes(buf: Buffer): string {
	return createHash("sha256").update(buf).digest("hex");
}

/**
 * Rough byte estimate of the i-th image block in the message stream. Used only
 * for the `estimatedBytes` accounting field (§8.2: "记录对象估算字节数"); not
 * load-bearing for correctness.
 *
 * `messages` is walked again to find the i-th image because imageContentHashes
 * already established the canonical order.
 */
function estimateImageBytes(messages: AgentMessage[], imageIndex: number): number {
	let seen = -1;
	for (const m of messages) {
		const content = (m as { content?: unknown }).content;
		if (!Array.isArray(content)) continue;
		for (const part of content) {
			if (part && isImageContent(part)) {
				seen += 1;
				if (seen === imageIndex) {
					// Decoded byte length of the base64 payload.
					const len = (part.data || "").length;
					if (len === 0) return 0;
					const padding = part.data.endsWith("==") ? 2 : part.data.endsWith("=") ? 1 : 0;
					return Math.max(0, Math.floor(len * 3 / 4) - padding);
				}
			}
		}
	}
	return 0;
}
