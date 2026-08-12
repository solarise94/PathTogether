/**
 * AI reading assistant sidecar — request-context assembler (Phase 2b, §3.4/§5/§7.2).
 *
 * Assembles the actual model request from the canonical transcript by combining
 * a stable cache region (§5.1) with a temporary working region (§5.2):
 *
 *   [checkpoint summary + annotation_index (stable text block)]
 *   [stable overview image (materialized when overview_derivative != null, §3.2)]
 *   --- (logical cache breakpoint; Phase 3 materializes provider fields) ---
 *   [recent messages after through_message_seq, via the Phase 1 image pipeline]
 *
 * The assembler:
 *   - reads session data (checkpoint, observations, pending review) through a
 *     deps-supplied `getSessionSnapshot` getter that runs ONCE per request
 *     OUTSIDE the session lock (§7.2: "不得在 hook 内逐次读 session 文件");
 *   - reuses the Phase 1 image materialization pipeline (transform-context.ts):
 *     selection → KEEP → materialize → rebuild, with byte-budget LRU,
 *     in-flight coalescing, AbortSignal, and aspect-preserving sizing;
 *   - upgrades evicted image_ref blocks from the Phase 1 PLACEHOLDER_TEXT to
 *     the §7.2 rich-text form (`ref_id`, `bbox`, magnification, observation
 *     summary or "尚无结构化观察", and a re-visit hint), never fabricating
 *     observations (§7.2);
 *   - groups tool call/result so no orphan toolResult leads the request (§5.2):
 *     when slicing from `through_message_seq`, we advance past any leading
 *     toolResult that has no preceding toolCall in the working region;
 *   - verifies the stable overview's content_sha256 against the materialized
 *     bytes and applies the §10/§13 repair flow (clear → rebuild → bump
 *     generation once), raising {@link StableContextUnavailableError} when the
 *     budget is exhausted.
 *
 * The OLD {@link makeTransformContext} hook remains as a compatibility entry
 * (Phase 1 behavior) for sessions that lack a checkpoint; this assembler is the
 * Phase 2b replacement that wraps and enriches it.
 */
import type { AgentMessage } from "@earendil-works/pi-agent-core";
import type { ImageContent } from "@earendil-works/pi-ai";

import type { PlatformClient } from "./platform/contract.js";
import {
	buildOverviewDerivative,
	buildStablePrefixObject,
	canonicalSerialize,
	checkpointStale,
	computeDerivativeContentHash,
	selectOverviewRef,
	stablePrefixHash,
	type CheckpointEnv,
	type ContextCheckpoint,
} from "./checkpoint.js";
import {
	isImageContent,
	isImageRefContent,
	stripContextMeta,
	type ImageRefContent,
	type Observation,
	type PersistedAgentMessage,
	type PersistedMessageMeta,
	type SessionData,
} from "./session-store.js";
import type { SlideInfo } from "./tools.js";
import {
	DEGRADE_TEXT,
	LEGACY_PLACEHOLDER_TEXT,
	chooseTargetLongEdge,
	dropDerivative,
	materializeDerivativeRaw,
	overviewDerivativeSpec,
	peekDerivative,
	putDerivative,
	resetLruCounters,
	richHistoryForRef,
	lruHitCount_value,
	lruMissCount_value,
	type TransformContextSettings,
} from "./transform-context.js";
import { estimateImageRefTokens, estimateImagePixelsTokens, enforceVisualTokenBudget } from "./compaction.js";
import { StableContextUnavailableError } from "./prepared-request.js";

// =========================================================================== //
// Types
// =========================================================================== //

/**
 * Read-only session snapshot the assembler needs per request (§7.2). The runner
 * reads this ONCE outside the session lock and hands it to the assembler via
 * {@link RequestAssemblerDeps.getSessionSnapshot}.
 */
export interface AssemblerSessionSnapshot {
	/** The committed checkpoint (may be null on a brand-new session). */
	checkpoint: ContextCheckpoint | null;
	/** Observations for the §7.2 rich-text history. */
	observations: Observation[];
	/** Current pending snapshot id (highest-priority keep, §15.1). */
	pendingSnapshotId: string | null;
	/** The full canonical message list (the assembler slices it). */
	messages: PersistedAgentMessage[];
}

/**
 * Dependencies the assembler needs to assemble a request. Provided by the
 * runner once per run; the per-request session snapshot comes through the
 * getter so the assembler never touches the session file directly (§7.2).
 */
export interface RequestAssemblerDeps {
	flask: PlatformClient;
	slide: string;
	slideInfo: SlideInfo;
	settings: TransformContextSettings;
	/** System prompt text (included in the stable prefix canonical form). */
	systemPrompt: string;
	/** Tool schema hash (for the stable prefix + staleness check). */
	toolSchemaHash: string;
	/** The first snapshot's toolCallId (for overview identity, §7.3). */
	firstSnapshotToolCallIdRef: { value: string | null };
	/** Checkpoint environment (version fields for staleness, §10). */
	checkpointEnv: CheckpointEnv;
	/** Read a consistent session snapshot once per request (OUTSIDE the lock). */
	getSessionSnapshot: () => Promise<AssemblerSessionSnapshot>;
	/**
	 * Resolve the overview ref's src bbox for a given ref_id, from the snapshot.
	 * Used by the stable-overview materializer (§3.2) — the checkpoint stores
	 * ref_id but not src, so the assembler looks it up in the canonical messages.
	 * Returns null when the ref is no longer in the canonical transcript (§13:
	 * "稳定概览永久失效").
	 */
	overviewSrcResolver?: (refId: string) => { x: number; y: number; w: number; h: number } | null;
	/** Optional sink for §12 structured metrics. */
	metricsSink?: (metrics: AssemblerMetrics) => void;
}

/**
 * Per-request structured metrics emitted by the assembler (§12). The metrics
 * sink receives this after each request. NO image content or API key.
 */
export interface AssemblerMetrics {
	session_id_placeholder?: never; // runner fills session_id at the boundary
	checkpoint_generation: number;
	stable_prefix_hash_prefix: string;
	selected_images: number;
	materialized_images: number;
	evicted_image_refs: string[];
	image_lru_hits: number;
	image_lru_misses: number;
	overview_image_bytes_sent: number;
	working_set_image_bytes_sent: number;
	transform_ms: number;
	region_fetch_ms: number;
	derivative_hash_mismatch: number;
	checkpoint_rebuild_reason: string | null;
	overview_status: "stable" | "repaired" | "rebuild-mismatch" | "no-overview";
	/**
	 * Visual-budget overflow tokens (§12, Phase 3.1): > 0 when protected-priority
	 * semantics (overview/pending never evicted, newest ordinary force-kept)
	 * forced this request over {@link TransformContextSettings.visualContextBudgetTokens}.
	 * Captured from the selection result for THIS assembly (request-local).
	 */
	visual_budget_overflow_tokens: number;
}

// =========================================================================== //
// makeRequestAssembler
// =========================================================================== //

/**
 * Build a request assembler bound to one session's flask/slide/settings/checkpoint
 * env. The returned function has the SAME signature as the Phase 1
 * {@link makeTransformContext} hook — `(messages, signal?) => Promise<AgentMessage[]>` —
 * so it can be a drop-in replacement for pi's `transformContext`.
 *
 * The assembler:
 *   1. fetches the session snapshot once (deps.getSessionSnapshot, outside lock);
 *   2. if no checkpoint exists, falls back to the Phase 1 transform path
 *      (assembler = enhanced transform-context; §7.2: "旧 transform hook 只保留
 *      为兼容入口");
 *   3. otherwise assembles [stable region] + [overview image] + [recent messages
 *      with image pipeline], verifying the overview hash per §10/§13.
 *
 * Contract: like the Phase 1 hook, the assembler MUST NOT leave `image_ref` in
 * the output. On a top-level error it returns a safe fallback that replaces
 * every image_ref with degrade text. The one exception is
 * {@link StableContextUnavailableError}, which propagates so the retry layer can
 * handle it (§3.2/§13).
 */
export function makeRequestAssembler(deps: RequestAssemblerDeps): (messages: AgentMessage[], signal?: AbortSignal) => Promise<AgentMessage[]> {
	return async (messages, signal) => {
		const tStart = Date.now();
		let regionFetchMs = 0;
		// Reset LRU counters at the start of each request so metrics reflect
		// this request only (§12).
		resetLruCounters();

		try {
			const snap = await deps.getSessionSnapshot();
			const cp = snap.checkpoint;

			// No checkpoint yet → Phase 1 fallback (no stable region). The
			// runner's ensureCheckpoint is responsible for creating one; if we
			// get here we behave exactly like the old transformContext.
			if (!cp) {
				const { out, evictedRefIds, overflowTokens } = await assembleWithoutCheckpoint(messages, deps, snap, signal, (ms) => {
					regionFetchMs += ms;
				});
				emitMetrics(deps, snap, cp, {
					checkpoint_generation: 0,
					stable_prefix_hash_prefix: "",
					selected_images: 0,
					materialized_images: 0,
					// P2-6: explicit eviction list from the selector.
					evicted_image_refs: evictedRefIds,
					image_lru_hits: lruHitCount_value(),
					image_lru_misses: lruMissCount_value(),
					overview_image_bytes_sent: 0,
					working_set_image_bytes_sent: 0,
					transform_ms: Date.now() - tStart,
					region_fetch_ms: regionFetchMs,
					derivative_hash_mismatch: 0,
					checkpoint_rebuild_reason: null,
					overview_status: "no-overview",
					visual_budget_overflow_tokens: overflowTokens,
				});
				return out;
			}

			// Checkpoint stale check (§10): if version fields changed, the
			// caller (runner) is responsible for rebuilding; we still assemble
			// with the current checkpoint but flag the reason in metrics. The
			// runner's ensureCheckpoint path handles the actual rebuild.
			const staleReason = checkpointStale(cp, deps.checkpointEnv);

			// Verify + materialize the stable overview (§10/§13).
			//
			// Phase 4 §17 risk 2 product switch: when the resolved settings carry
			// overviewEnabled=false, the stable region is assembled WITHOUT the
			// overview image. We short-circuit to a no-overview result so NO region
			// fetch happens for the overview (overview_image_bytes_sent stays 0) and
			// overview_status is reported as "no-overview". The stable TEXT block
			// (summary + annotation index) is still emitted intact below. The arm
			// identity already records the effective flag, so no new metric field.
			const overviewResult = deps.settings.overviewEnabled
				? await materializeStableOverview(cp, deps, signal)
				: { image: null, overviewRefId: null, imageBytes: 0, hashMismatchCount: 0, regionFetchMs: 0, status: "no-overview" as const };
			regionFetchMs += overviewResult.regionFetchMs;

			// A checkpoint "covers" a message when its session_message_seq <=
			// through_message_seq (§5.2). But a TRIVIAL checkpoint (no summary,
			// no overview, no annotation index — the lazy g1 form for a session
			// with no compaction history) carries no long-term memory, so it
			// MUST NOT swallow the active conversation. We detect this and treat
			// it as covering NOTHING: the whole message list stays in the recent
			// region. This preserves the Phase 1 behavior for fresh sessions
			// while still allowing a real summary-bearing checkpoint to slice.
			const hasStableContent = !!(cp.summary || cp.annotation_index || cp.overview_derivative);
			const effectiveThroughSeq = hasStableContent ? cp.through_message_seq : 0;

			// Build the stable text block (§5.1): checkpoint summary + annotation
			// index. The overview image (when present) is attached as a separate
			// message before the breakpoint. Trivial checkpoints produce NO
			// stable block (the conversation is entirely in the recent region).
			const stableMessages = hasStableContent ? buildStableRegionMessages(cp, overviewResult) : [];

			// Slice recent messages from through_message_seq (§5.2). The slice
			// operates on the `messages` argument (pi's live agent.state.messages,
			// which is the canonical transcript in-memory), NOT on the snapshot's
			// messages (which may be stale on disk mid-run). The snapshot only
			// supplies the checkpoint boundary + observations + pending id.
			const recentRaw = sliceRecentMessages(messages as PersistedAgentMessage[], effectiveThroughSeq);
			// Apply the Phase 1 image pipeline to the recent slice (selection +
			// materialization + eviction). Evicted refs in the recent slice get
			// the §7.2 rich-text form via the per-request observations index.
			const observationsIndex = buildSnapshotObservationIndex(snap.observations, recentRaw);
			const recentSlice = await transformRecentSlice({
				messages: recentRaw,
				deps,
				overviewRefId: overviewResult.overviewRefId,
				pendingSnapshotId: snap.pendingSnapshotId,
				observations: snap.observations,
				observationsIndex,
				signal,
				onRegionFetchMs: (ms) => {
					regionFetchMs += ms;
				},
			});
			const recentTransformed = recentSlice.messages;

			// Final assembly: stable + breakpoint marker + recent. The breakpoint
			// is a logical boundary for Phase 2b (Phase 3 materializes the
			// provider cache-control field). We insert a sentinel user message
			// carrying the breakpoint marker; the provider adapter ignores it
			// until Phase 3.
			const combined = [...stableMessages, ...recentTransformed] as AgentMessage[];

			// Strip sidecar metadata at the Provider boundary (§10).
			const clean = stripContextMeta(combined as PersistedAgentMessage[]) as AgentMessage[];

			// Collect metrics.
			const overviewBytes = overviewResult.imageBytes;
			const workingBytes = countImageBytes(recentTransformed);
			// P2-6: use the explicit eviction list from the selector (recency +
			// budget + overview-covered), NOT a before/after diff.
			const evictedRefs = recentSlice.evictedRefIds;
			const selectedImages = countImageBlocks(recentTransformed);
			const materializedImages = selectedImages; // all selected images are materialized

			emitMetrics(deps, snap, cp, {
				checkpoint_generation: cp.generation,
				stable_prefix_hash_prefix: cp.stable_prefix_hash.slice(0, 16),
				selected_images: selectedImages,
				materialized_images: materializedImages,
				evicted_image_refs: evictedRefs,
				image_lru_hits: lruHitCount_value(),
				image_lru_misses: lruMissCount_value(),
				overview_image_bytes_sent: overviewBytes,
				working_set_image_bytes_sent: workingBytes,
				transform_ms: Date.now() - tStart,
				region_fetch_ms: regionFetchMs,
				derivative_hash_mismatch: overviewResult.hashMismatchCount,
				checkpoint_rebuild_reason: staleReason,
				overview_status: overviewResult.status,
				visual_budget_overflow_tokens: recentSlice.overflowTokens,
			});

			return clean;
		} catch (e) {
			// StableContextUnavailableError propagates (§3.2/§13) so the retry
			// layer can apply the shared budget.
			if (e instanceof StableContextUnavailableError) throw e;
			// Any other top-level error: strip image_refs and return a safe
			// fallback (never leak image_ref to the provider). Mirrors the
			// Phase 1 contract.
			return stripContextMeta(stripImageRefsToDegrade(messages) as PersistedAgentMessage[]) as AgentMessage[];
		}
	};
}

// =========================================================================== //
// Stable overview materialization + hash verification (§3.2/§6.3/§10/§13)
// =========================================================================== //

type OverviewMaterializationResult = {
	/** The materialized overview image block, or null when the checkpoint has no overview. */
	image: ImageContent | null;
	/** The ref_id the overview was built from (for the assembler to exclude from working set). */
	overviewRefId: string | null;
	/** Decoded byte length of the overview JPEG (for metrics). */
	imageBytes: number;
	/** Number of content-hash mismatches encountered during verification. */
	hashMismatchCount: number;
	/** Region fetch time spent materializing/rebuilding (ms). */
	regionFetchMs: number;
	/** Outcome classification for metrics. */
	status: "stable" | "repaired" | "rebuild-mismatch" | "no-overview";
};

/**
 * Materialize and verify the stable overview (§3.2/§10/§13).
 *
 *   - No overview_derivative → return null (degraded generation; stable region
 *     carries no image). This is an allowed state per §3.2.
 *   - Peek the derivative LRU. If present, verify content_sha256:
 *       1. match → use (status "stable");
 *       2. mismatch → drop the cache entry, rebuild once with the checkpoint's
 *          encoding parameters; if the rebuild matches → use + record "repaired";
 *       3. still mismatch → raise StableContextUnavailableError (§13: "禁止循环
 *          重建"). The runner's retry layer applies the shared budget; if it
 *          also fails the runner retires the checkpoint to a no-overview
 *          generation (handled in the rebuild path, not here).
 *   - Not cached → materialize once via {@link materializeDerivativeRaw}; verify
 *     content_sha256; mismatch → same drop+rebuild+raise flow.
 *
 * Fingerprint mismatch (the slide changed under us) is detected by the stale
 * check at the runner level; here we only handle content-hash drift.
 */
async function materializeStableOverview(
	cp: ContextCheckpoint,
	deps: RequestAssemblerDeps,
	signal?: AbortSignal,
): Promise<OverviewMaterializationResult> {
	const od = cp.overview_derivative;
	if (!od) {
		return { image: null, overviewRefId: null, imageBytes: 0, hashMismatchCount: 0, regionFetchMs: 0, status: "no-overview" };
	}

	const spec = overviewDerivativeSpec({
		slide: deps.slide,
		fingerprint: cp.slide_fingerprint,
		// The overview src must be reconstructed from the recorded ref. We
		// resolve the ref from the canonical messages via the snapshot; but
		// this function does not have the snapshot. The caller (makeRequestAssembler)
		// resolves the src separately and we accept it via the checkpoint's
		// overview_derivative... but the checkpoint only stores ref_id, not src.
		// Workaround: the runner back-fills the overview src into a side channel.
		// For Phase 2b we resolve it from the deps' firstSnapshot ref via the
		// deps getSessionSnapshot — but that creates a cycle. Instead, the runner
		// guarantees that when a checkpoint HAS an overview_derivative, the
		// matching ref is still in the canonical messages, so the assembler's
		// caller (below) looks up the src and passes it through a closure.
		// We use a sentinel here and require the caller to have pre-resolved.
		// See the `overviewSrcResolver` below.
		src: { x: 0, y: 0, w: 0, h: 0 },
		targetLongEdge: od.target_long_edge,
		jpegQuality: od.jpeg_quality,
		overlayVersion: od.overlay_version,
	});

	// The overview src is resolved by the caller via the snapshot (the
	// checkpoint stores ref_id but not src). We use the deps' resolver.
	const src = deps.overviewSrcResolver
		? deps.overviewSrcResolver(od.ref_id)
		: null;
	if (!src) {
		// Cannot locate the overview ref in the canonical messages → the
		// overview is permanently unavailable. Per §13 we retire the
		// checkpoint to a no-overview generation (the runner handles this on
		// the StableContextUnavailableError path).
		throw new StableContextUnavailableError(`overview ref ${od.ref_id} not found in canonical messages`);
	}
	const fullSpec = { ...spec, x: src.x, y: src.y, w: Math.max(1, src.w), h: Math.max(1, src.h) };

	let hashMismatchCount = 0;
	const tFetchStart = Date.now();

	// Step 1: peek LRU (no counter mutation).
	const cached = peekDerivative(fullSpec);
	let b64: string | null = null;
	let mime: string = od.mime_type;

	if (cached) {
		if (computeDerivativeContentHash(cached.data) === od.content_sha256) {
			b64 = cached.data;
			mime = cached.mime;
		} else {
			// Step 2: mismatch → drop + rebuild once (§10/§13).
			hashMismatchCount += 1;
			dropDerivative(fullSpec);
			b64 = await rebuildAndVerify(fullSpec, od, deps, signal);
			if (b64 === null) {
				// Rebuild also mismatched → encoder drift → raise. The runner
				// retires the checkpoint to a no-overview generation on this
				// signal (handled once per logical call).
				throw new StableContextUnavailableError(`overview content_sha256 mismatch after rebuild (ref ${od.ref_id})`);
			}
		}
	} else {
		// Not cached → materialize once + verify.
		try {
			const r = await materializeDerivativeRaw({
				flask: deps.flask,
				slide: deps.slide,
				slideInfo: deps.slideInfo,
				spec: fullSpec,
				expectedFingerprint: cp.slide_fingerprint,
				signal,
			});
			b64 = r.data;
			mime = r.mime;
		} catch {
			// Transient materialization failure → raise so the retry layer
			// applies the shared budget (§3.2: "本次请求返回可重试的
			// stable_context_unavailable").
			throw new StableContextUnavailableError(`overview materialization failed (ref ${od.ref_id})`);
		}
		if (computeDerivativeContentHash(b64) !== od.content_sha256) {
			// Step 2: mismatch → drop + rebuild once.
			hashMismatchCount += 1;
			dropDerivative(fullSpec);
			b64 = await rebuildAndVerify(fullSpec, od, deps, signal);
			if (b64 === null) {
				throw new StableContextUnavailableError(`overview content_sha256 mismatch after initial build (ref ${od.ref_id})`);
			}
		}
	}

	const regionFetchMs = Date.now() - tFetchStart;
	const imageBytes = decodedBase64Bytes(b64);
	const status: OverviewMaterializationResult["status"] = hashMismatchCount > 0 ? "repaired" : "stable";
	return {
		image: { type: "image", data: b64, mimeType: mime },
		overviewRefId: od.ref_id,
		imageBytes,
		hashMismatchCount,
		regionFetchMs,
		status,
	};
}

/**
 * Rebuild the overview derivative once with the checkpoint's encoding
 * parameters and verify the content hash. Returns the base64 on match, or null
 * on mismatch (caller raises StableContextUnavailableError).
 *
 * Per §10/§13: "重建后仍不匹配 → 判定为编码器环境/确定性契约漂移".
 */
async function rebuildAndVerify(
	spec: ReturnType<typeof overviewDerivativeSpec>,
	od: NonNullable<ContextCheckpoint["overview_derivative"]>,
	deps: RequestAssemblerDeps,
	signal?: AbortSignal,
): Promise<string | null> {
	let rebuilt: string;
	try {
		// Do NOT pass expectedFingerprint here: materializeDerivativeRaw falls
		// back to spec.fingerprint, which the caller built from
		// cp.slide_fingerprint. (Passing od.ref_id would make Flask 409 —
		// a ref id is not the slide fingerprint.)
		const r = await materializeDerivativeRaw({
			flask: deps.flask,
			slide: deps.slide,
			slideInfo: deps.slideInfo,
			spec,
			signal,
		});
		rebuilt = r.data;
	} catch {
		return null;
	}
	if (computeDerivativeContentHash(rebuilt) === od.content_sha256) {
		// Repair succeeded → cache and continue (§10: "记录一次 derivative repair").
		putDerivative(spec, { data: rebuilt, mime: od.mime_type });
		return rebuilt;
	}
	return null;
}

// =========================================================================== //
// Stable region message construction (§5.1)
// =========================================================================== //

/**
 * Build the stable text block(s) for the request (§5.1):
 *   - one user message with the checkpoint summary + annotation index;
 *   - when an overview image is present, a second user message with a short
 *     label + the image block.
 *
 * The system prompt and tool definitions are NOT included here — pi carries
 * them as part of the LlmContext, and the stable_prefix_hash covers them via
 * the version hashes. The overview image is placed in the stable region so it
 * is byte-stable across requests in the same generation (§8.3).
 */
function buildStableRegionMessages(
	cp: ContextCheckpoint,
	overview: OverviewMaterializationResult,
): AgentMessage[] {
	const out: AgentMessage[] = [];
	const lines: string[] = [];
	if (cp.summary) {
		lines.push(`【会话长期记忆】\n${cp.summary}`);
	}
	if (cp.annotation_index) {
		lines.push(`【已确认观察索引】\n${cp.annotation_index}`);
	}
	if (lines.length > 0) {
		out.push({
			role: "user",
			content: [{ type: "text", text: lines.join("\n\n") }],
			timestamp: 0, // stable: no timestamp drift (§5.1)
		} as AgentMessage);
	}
	if (overview.image) {
		out.push({
			role: "user",
			content: [
				{ type: "text", text: "【稳定全片概览】（用于导航与定位，细节请用 goto+snapshot 重新抓取）" },
				overview.image,
			],
			timestamp: 0, // stable
		} as AgentMessage);
	}
	return out;
}

// =========================================================================== //
// Recent message slicing + Phase 1 image pipeline (§5.2/§7.2)
// =========================================================================== //

/**
 * Slice the canonical messages from `through_message_seq` onward (§5.2). The
 * cut starts at the FIRST message whose session_message_seq is strictly greater
 * than `throughSeq`.
 *
 * §5.2 / §3.4 tool-call grouping: "assembler 从 through 边界切消息时必须从
 * 非 toolResult 消息开始切，避免孤立 toolResult 开头". So if the slice would
 * start with a toolResult whose matching toolCall is NOT in the slice, we drop
 * leading toolResult messages until we reach a user/assistant/compactionSummary
 * message. (pi requires every toolResult to have a preceding toolCall.)
 */
export function sliceRecentMessages(messages: PersistedAgentMessage[], throughSeq: number): PersistedAgentMessage[] {
	// Find the first index whose seq > throughSeq.
	let startIdx = 0;
	let cut = false;
	for (let i = 0; i < messages.length; i++) {
		const seq = messageSeq(messages[i]);
		if (typeof seq === "number" && seq <= throughSeq) {
			startIdx = i + 1;
			cut = true;
		} else {
			startIdx = i;
			break;
		}
	}
	// Defensive: when no seqs are assigned at all (shouldn't happen after Phase
	// 2a migration), startIdx stays 0 and we take everything.
	const sliced = messages.slice(startIdx);
	// Drop leading orphan toolResults ONLY when we actually cut at a checkpoint
	// boundary (§5.2: the rule exists to avoid a toolResult whose toolCall was
	// on the far side of the cut). When throughSeq=0 / no cut happened, the
	// whole message list is intact and we must NOT drop a leading toolResult
	// (it pairs with a toolCall earlier in the canonical transcript, or it is
	// the seed of a fork/branch).
	if (!cut) return sliced;
	return dropLeadingOrphanToolResults(sliced);
}

/**
 * Drop leading toolResult messages that have no matching toolCall earlier in
 * the slice. We walk forward, accumulating seen toolCall ids; any toolResult
 * whose toolCallId is not in the seen set AND appears before any toolCall is
 * considered an orphan and dropped (§5.2: "不能把孤立的 toolResult 放进请求").
 *
 * Once we have seen ANY non-toolResult message (user/assistant/compaction),
 * subsequent toolResults are kept (they pair with assistant toolCalls in the
 * slice). The drop is only for the OPENING of the slice.
 */
function dropLeadingOrphanToolResults(messages: PersistedAgentMessage[]): PersistedAgentMessage[] {
	let i = 0;
	while (i < messages.length) {
		const m = messages[i]!;
		const role = (m as { role?: string }).role;
		if (role === "toolResult") {
			// Is its toolCallId present as a toolCall in messages[0..i-1]? Since
			// we are still in the "leading run", messages[0..i-1] are all
			// toolResults too → no toolCall can precede. So it's an orphan.
			i += 1;
			continue;
		}
		break;
	}
	return messages.slice(i);
}

function messageSeq(m: PersistedAgentMessage | undefined): number | undefined {
	if (!m) return undefined;
	const meta = (m as PersistedAgentMessage & { _context_meta?: PersistedMessageMeta })._context_meta;
	return meta?.session_message_seq;
}

/**
 * Apply the Phase 1 image pipeline to the recent slice: select KEEP images,
 * materialize them, and convert evicted refs to the §7.2 rich-text form using
 * the per-request observations index.
 *
 * This is a focused reimplementation of transformOnce's KEEP logic, specialized
 * for the assembler:
 *   - the overview image is already in the stable region, so any overview ref
 *     in the recent slice is converted to text (it's covered by the stable
 *     overview);
 *   - pending snapshot is always kept;
 *   - the last N non-overview refs are kept (N = settings.keepRecentImages);
 *   - the §9.1 visual token budget is enforced as a HARD cap: after the recency
 *     KEEP pass, ordinary kept images are reduced to a contiguous newest suffix
 *     until the estimated selected visual tokens <= visualContextBudgetTokens.
 *     Overview and pending positions are NOT budget-evictable (§9.1);
 *   - evicted refs use {@link richHistoryForRef} with the observations index.
 *
 * Returns both the rebuilt messages AND the explicit list of evicted ref_ids
 * (P2-6: the KEEP stage knows exactly which refs were dropped; the caller uses
 * this list for the §12 `evicted_image_refs` metric instead of diffing).
 */
async function transformRecentSlice(args: {
	messages: PersistedAgentMessage[];
	deps: RequestAssemblerDeps;
	overviewRefId: string | null;
	pendingSnapshotId: string | null;
	observations: Observation[];
	observationsIndex: Map<string, { summary: string }[]>;
	signal?: AbortSignal;
	onRegionFetchMs: (ms: number) => void;
}): Promise<{ messages: AgentMessage[]; evictedRefIds: string[]; overflowTokens: number }> {
	const { messages, deps, overviewRefId, pendingSnapshotId, observations, observationsIndex, signal } = args;
	const settings = deps.settings;
	const slideInfo = deps.slideInfo;

	// Phase 1: scan image positions — BOTH dehydrated image_ref blocks AND live
	// (not yet persisted) image blocks. Live images appear in toolResult content
	// mid-run (dehydration only happens at settle); without handling them, every
	// snapshot taken during a run would be re-sent on every request — a
	// regression vs the Phase 1 pipeline, which evicted old live images too.
	type Pos = {
		msgIdx: number;
		blkIdx: number;
		overview: boolean;
		pending: boolean;
		kind: "ref" | "image";
		ref?: ImageRefContent;
		liveSrc?: { x: number; y: number; w: number; h: number };
		livePixels?: { w: number; h: number };
		liveFingerprint?: string;
	};
	const positions: Pos[] = [];
	const firstId = deps.firstSnapshotToolCallIdRef.value;
	for (let msgIdx = 0; msgIdx < messages.length; msgIdx++) {
		const m = messages[msgIdx]!;
		const role = (m as { role?: string }).role;
		if (role !== "user" && role !== "assistant" && role !== "toolResult") continue;
		const toolCallId = (m as { toolCallId?: string }).toolCallId;
		const content = (m as { content?: unknown }).content;
		if (!Array.isArray(content)) continue;
		for (let blkIdx = 0; blkIdx < content.length; blkIdx++) {
			const part = content[blkIdx];
			if (part && isImageRefContent(part)) {
				const isOverview = overviewRefId !== null && part.ref_id === overviewRefId;
				const isPending = pendingSnapshotId !== null && part.ref_id === `ref_${pendingSnapshotId}`;
				positions.push({ msgIdx, blkIdx, overview: isOverview, pending: isPending, kind: "ref", ref: part });
			} else if (isImageContent(part)) {
				// Live image: overview/pending identity comes from the parent
				// toolResult's toolCallId (mirrors Phase 1 isOverviewLiveImage).
				const isOverviewLive = role === "toolResult" && firstId !== null && toolCallId === firstId;
				const isPendingLive = role === "toolResult" && pendingSnapshotId !== null && toolCallId === pendingSnapshotId;
				const liveMeta = liveImageMetaFromToolResult(m);
				positions.push({
					msgIdx,
					blkIdx,
					overview: isOverviewLive,
					pending: isPendingLive,
					kind: "image",
					liveSrc: liveMeta?.src,
					livePixels: liveMeta?.pixels,
					liveFingerprint: liveMeta?.fingerprint,
				});
			}
		}
	}

	// Phase 2: KEEP set. Overview is covered by the stable region (when the
	// checkpoint has one) → text-ify in the recent slice; a live overview image
	// with NO stable overview yet is kept (Phase 1 semantics). Pending always
	// kept. Last N ordinary kept.
	const keepKeys = new Set<string>();
	const ordinary: Pos[] = [];
	for (const p of positions) {
			const key = `${p.msgIdx}:${p.blkIdx}`;
		if (p.overview) {
			if (p.kind === "image" && overviewRefId === null) {
				// No stable overview in the checkpoint → keep the live one.
				keepKeys.add(key);
			}
			// Otherwise: covered by the stable region → evict to text below.
			continue;
		}
		if (p.pending) {
			keepKeys.add(key);
		} else {
			ordinary.push(p);
		}
	}
	// Keep last N by message order (oldest evicted first).
	ordinary.sort((a, b) => a.msgIdx - b.msgIdx || a.blkIdx - b.blkIdx);
	const keepFrom = Math.max(0, ordinary.length - settings.keepRecentImages);
	for (const p of ordinary.slice(keepFrom)) {
		keepKeys.add(`${p.msgIdx}:${p.blkIdx}`);
	}

	// Phase 2b (§9.1 / P2-3): enforce the visual token budget as a HARD cap.
	// Ordinary recent images are reduced to a contiguous newest suffix until the
	// estimate fits. Overview and pending positions are NEVER budget-evicted.
	const budgetSel = enforceVisualBudget(keepKeys, positions, ordinary, keepFrom, settings, overviewRefId);
	const budgetEvictedKeys = budgetSel.evictedKeys;

	// Phase 3: materialize KEEP refs + normalize oversized KEEP live images.
	// Detail tier = newest kept ordinary + pending.
	const keptOrdinary = ordinary
		.slice(keepFrom)
		.filter((p) => !budgetEvictedKeys.has(`${p.msgIdx}:${p.blkIdx}`));
	const newestKeptOrdinaryKey = keptOrdinary.length > 0
		? `${keptOrdinary[keptOrdinary.length - 1]!.msgIdx}:${keptOrdinary[keptOrdinary.length - 1]!.blkIdx}`
		: null;
	const toMaterialize = positions.filter((p) => keepKeys.has(`${p.msgIdx}:${p.blkIdx}`) && (p.kind === "ref" || p.kind === "image"));
	const materialized = new Map<string, ImageContent | { type: "text"; text: string }>();
	const tFetchStart = Date.now();
	await mapPool(toMaterialize, settings.regionConcurrency, async (p) => {
		const key = `${p.msgIdx}:${p.blkIdx}`;
		const isDetail = p.pending || key === newestKeptOrdinaryKey;
		if (p.kind === "ref" && p.ref) {
			const block = await materializeRefRich(p.ref, deps.flask, deps.slide, slideInfo, settings, isDetail, signal);
			materialized.set(key, block);
			return;
		}
		// Overview / detail / working tiers must match budget charging below
		// (kept live overview without a stable region uses overviewLongEdge).
		const targetLongEdge = chooseTargetLongEdge(settings, p.overview, isDetail);
		const normalized = await normalizeLiveImageBlock(p, deps.flask, deps.slide, slideInfo, settings, targetLongEdge, signal);
		if (normalized) materialized.set(key, normalized);
	}, signal);
	args.onRegionFetchMs(Date.now() - tFetchStart);

	// Phase 4: rebuild messages, converting evicted refs to §7.2 rich text.
	// Collect the explicit eviction list (P2-6): every image_ref whose position
	// is NOT in keepKeys (recency + budget + overview-covered). This is the
	// authoritative metric input — the caller must NOT diff before/after.
	const evictedRefIds: string[] = [];
	const rebuilt = messages.map((m, msgIdx) => {
		const role = (m as { role?: string }).role;
		if (role !== "user" && role !== "assistant" && role !== "toolResult") return m as AgentMessage;
		const content = (m as { content?: unknown }).content;
		if (typeof content === "string" || !Array.isArray(content)) return m as AgentMessage;
		let touched = false;
		const newContent = content.map((part, blkIdx): unknown => {
			const key = `${msgIdx}:${blkIdx}`;
			if (part && isImageRefContent(part)) {
				touched = true;
				if (!keepKeys.has(key)) {
					// Evicted → §7.2 rich text. Overview refs in the recent slice
					// also land here (they are covered by the stable overview).
					const ref = part as ImageRefContent;
					if (ref.ref_id) evictedRefIds.push(ref.ref_id);
					return { type: "text", text: richHistoryForRef(ref, observations, observationsIndex) };
				}
				return materialized.get(key) ?? { type: "text", text: DEGRADE_TEXT };
			}
			if (isImageContent(part)) {
				// Live image: keep only when selected; evicted older live images
				// become placeholder text (Phase 1 parity — they can be re-
				// captured with goto+snapshot). Evicted overview live images
				// (covered by the stable region) also land here.
				if (!keepKeys.has(key)) {
					touched = true;
					return { type: "text", text: LEGACY_PLACEHOLDER_TEXT };
				}
				const normalized = materialized.get(key);
				if (normalized) {
					touched = true;
					return normalized;
				}
				return part;
			}
			return part;
		});
		return touched ? ({ ...(m as object), content: newContent } as AgentMessage) : (m as AgentMessage);
	});
	return { messages: rebuilt, evictedRefIds, overflowTokens: budgetSel.overflowTokens };
}

/**
 * Enforce the §9.1 visual token budget via the SHARED
 * {@link enforceVisualTokenBudget} (compaction.ts) — Phase 3.1 review: do not
 * fork the eviction logic, and charge the NEWEST kept ordinary image at the
 * detail tier (it is materialized at `detailImageLongEdge`, not
 * `workingImageLongEdge` — charging it at working under-counts ~1398 tokens
 * for a square image).
 *
 * Returns the set of `${msgIdx}:${blkIdx}` keys that were removed from
 * `keepKeys` (mutated in place) plus the request-local overflowTokens. Overview
 * and pending positions are never evicted (§9.1).
 */
function enforceVisualBudget(
	keepKeys: Set<string>,
	positions: Array<{
		msgIdx: number;
		blkIdx: number;
		overview: boolean;
		pending: boolean;
		kind: "ref" | "image";
		ref?: ImageRefContent;
		liveSrc?: { x: number; y: number; w: number; h: number };
		livePixels?: { w: number; h: number };
	}>,
	ordinary: Array<{
		msgIdx: number;
		blkIdx: number;
		kind: "ref" | "image";
		ref?: ImageRefContent;
		liveSrc?: { x: number; y: number; w: number; h: number };
		livePixels?: { w: number; h: number };
	}>,
	keepFrom: number,
	settings: TransformContextSettings,
	overviewRefId: string | null,
): { evictedKeys: Set<string>; overflowTokens: number } {
	const budget = settings.visualContextBudgetTokens;
	if (!(Number.isFinite(budget) && budget > 0)) return { evictedKeys: new Set(), overflowTokens: 0 };

	// Estimate the non-evictable baseline (overview + pending kept positions).
	//   - stable-region overview (overviewRefId set) → charge overviewLongEdge;
	//   - kept live overview when there is NO stable overview → overview tier;
	//   - pending → detail tier;
	//   - overview+pending (default first unreviewed snapshot) → overview wins,
	//     matching chooseTargetLongEdge / materialization (Phase 1 parity).
	let baseline = 0;
	if (overviewRefId !== null) {
		baseline += estimateImageRefTokens({ w: settings.overviewLongEdge, h: settings.overviewLongEdge }, settings.overviewLongEdge);
	}
	for (const p of positions) {
		const key = `${p.msgIdx}:${p.blkIdx}`;
		if (!keepKeys.has(key)) continue;
		const isLiveOverview = p.overview && overviewRefId === null;
		if (isLiveOverview || p.pending) {
			baseline += estimatePosTokens(p, settings, p.pending, isLiveOverview);
		}
	}
	const keptOrdinary = ordinary.slice(keepFrom); // oldest → newest
	const newestKey = keptOrdinary.length ? `${keptOrdinary[keptOrdinary.length - 1]!.msgIdx}:${keptOrdinary[keptOrdinary.length - 1]!.blkIdx}` : null;
	const sel = enforceVisualTokenBudget({
		budgetTokens: budget,
		baselineTokens: baseline,
		ordinary: keptOrdinary.map((p) => {
			const key = `${p.msgIdx}:${p.blkIdx}`;
			return { key, tokens: estimatePosTokens(p, settings, key === newestKey) };
		}),
	});
	for (const key of sel.evictedKeys) keepKeys.delete(key);
	return { evictedKeys: sel.evictedKeys, overflowTokens: sel.overflowTokens };
}

/**
 * Estimate the visual token cost of one position at its FINAL materialization
 * tier (§9.1).
 *   - overview (kept live overview, no stable region) → overview tier
 *     (wins over pending when both apply — same as chooseTargetLongEdge);
 *   - `isDetail` (pending snapshot OR the newest kept ordinary) → detail tier;
 *   - otherwise → working tier;
 *   - kept image_ref / live with src → bbox at that tier (rematerialize path);
 *   - live with only pixels within tier → as-sent pixels;
 *   - live oversized without src → 0 (normalize will text-degrade; do NOT bill
 *     a rematerialized size we cannot produce);
 *   - live with neither src nor pixels → 0 (normalize text-degrades unknown
 *     size; never bill a target-tier square while sending arbitrary bytes).
 */
function estimatePosTokens(
	p: {
		kind: "ref" | "image";
		pending?: boolean;
		ref?: ImageRefContent;
		liveSrc?: { x: number; y: number; w: number; h: number };
		livePixels?: { w: number; h: number };
	},
	settings: TransformContextSettings,
	isDetail: boolean,
	isOverview = false,
): number {
	const targetLongEdge = chooseTargetLongEdge(settings, isOverview, isDetail);
	if (p.kind === "ref" && p.ref) {
		const src = p.ref.src || { x: 0, y: 0, w: 0, h: 0 };
		return estimateImageRefTokens({ w: src.w, h: src.h }, targetLongEdge);
	}
	const hasSrc = !!(p.liveSrc && p.liveSrc.w > 0 && p.liveSrc.h > 0);
	if (hasSrc) {
		return estimateImageRefTokens({ w: p.liveSrc!.w, h: p.liveSrc!.h }, targetLongEdge);
	}
	if (p.livePixels) {
		const le = Math.max(p.livePixels.w, p.livePixels.h);
		if (le <= targetLongEdge) return estimateImagePixelsTokens(p.livePixels.w, p.livePixels.h);
		// Oversized, no rematerialize src → text degrade (0 visual tokens).
		return 0;
	}
	// No size metadata at all → text degrade (0 visual tokens).
	return 0;
}

function liveImageMetaFromToolResult(msg: unknown): {
	src?: { x: number; y: number; w: number; h: number };
	pixels?: { w: number; h: number };
	fingerprint?: string;
} | null {
	const details = (msg as {
		details?: {
			src?: { x: number; y: number; w: number; h: number };
			width?: number;
			height?: number;
			slide_fingerprint?: string;
		};
	}).details;
	if (!details) return null;
	const src = details.src && details.src.w > 0 && details.src.h > 0 ? details.src : undefined;
	const w = Number(details.width);
	const h = Number(details.height);
	const pixels = Number.isFinite(w) && Number.isFinite(h) && w > 0 && h > 0 ? { w: Math.floor(w), h: Math.floor(h) } : undefined;
	const fingerprint = typeof details.slide_fingerprint === "string" ? details.slide_fingerprint : undefined;
	if (!src && !pixels) return null;
	return { src, pixels, fingerprint };
}

/**
 * Normalize an oversized KEEP live image to `targetLongEdge`.
 *
 * Returns rematerialized image bytes, {@link DEGRADE_TEXT} on fingerprint
 * mismatch / rematerialize failure / missing-or-oversized-without-src, or
 * `null` when the original live bytes are already within tier (rebuild keeps
 * them). Never silently retains unknown-size or oversized bytes after the
 * budget assumed a controlled tier.
 */
async function normalizeLiveImageBlock(
	p: {
		liveSrc?: { x: number; y: number; w: number; h: number };
		livePixels?: { w: number; h: number };
		liveFingerprint?: string;
	},
	flask: PlatformClient,
	slide: string,
	slideInfo: SlideInfo,
	settings: TransformContextSettings,
	targetLongEdge: number,
	signal?: AbortSignal,
): Promise<ImageContent | { type: "text"; text: string } | null> {
	const liveFp = p.liveFingerprint || "";
	const slideFp = slideInfo.fingerprint || "";
	if (liveFp && liveFp !== slideFp) {
		return { type: "text", text: DEGRADE_TEXT };
	}
	const pixels = p.livePixels;
	const actualLe = pixels ? Math.max(pixels.w, pixels.h) : Number.POSITIVE_INFINITY;
	const src = p.liveSrc;
	const hasSrc = !!(src && src.w > 0 && src.h > 0);
	if (!hasSrc) {
		// No rematerialize bbox. Keep only when pixels are known AND within
		// tier; unknown size (Infinity) or oversized → text degrade. (Do not
		// use Number.isFinite(actualLe): missing pixels yield Infinity and
		// would otherwise fall through to keeping arbitrary bytes.)
		if (pixels && actualLe <= targetLongEdge) return null;
		return { type: "text", text: DEGRADE_TEXT };
	}
	if (Number.isFinite(actualLe) && actualLe <= targetLongEdge) return null;
	const fp = liveFp || slideFp;
	const spec = overviewDerivativeSpec({
		slide,
		fingerprint: fp,
		src: src!,
		targetLongEdge,
		jpegQuality: settings.jpegQuality,
		overlayVersion: settings.overlayVersion,
	});
	try {
		const r = await materializeDerivativeRaw({
			flask,
			slide,
			slideInfo,
			spec,
			expectedFingerprint: fp || undefined,
			signal,
		});
		return { type: "image", data: r.data, mimeType: r.mime };
	} catch {
		return { type: "text", text: DEGRADE_TEXT };
	}
}

/**
 * Materialize one KEEP image_ref via the derivative LRU + in-flight coalescing.
 * Reuses {@link materializeDerivativeRaw} but falls back to DEGRADE_TEXT on
 * failure (Phase 1 per-image degrade: "仅该图片文本降级，不让整个 transform 失败").
 */
async function materializeRefRich(
	ref: ImageRefContent,
	flask: PlatformClient,
	slide: string,
	slideInfo: SlideInfo,
	settings: TransformContextSettings,
	isDetail: boolean,
	signal?: AbortSignal,
): Promise<ImageContent | { type: "text"; text: string }> {
	const fp = ref.slide_fingerprint || "";
	if (fp && fp !== (slideInfo.fingerprint || "")) {
		return { type: "text", text: DEGRADE_TEXT };
	}
	const src = ref.src || { x: 0, y: 0, w: 0, h: 0 };
	const targetLongEdge = isDetail ? settings.detailImageLongEdge : settings.workingImageLongEdge;
	const spec = overviewDerivativeSpec({
		slide,
		fingerprint: slideInfo.fingerprint || fp,
		src,
		targetLongEdge,
		jpegQuality: settings.jpegQuality,
		overlayVersion: settings.overlayVersion,
	});
	try {
		const r = await materializeDerivativeRaw({ flask, slide, slideInfo, spec, expectedFingerprint: fp || undefined, signal });
		return { type: "image", data: r.data, mimeType: r.mime };
	} catch {
		return { type: "text", text: DEGRADE_TEXT };
	}
}

// =========================================================================== //
// Observations index (§7.2)
// =========================================================================== //

/**
 * Build a `ref_id / snapshot_id → observations[]` index (§7.2). Observations
 * are linked to a ref when:
 *   - the observation carries a `snapshot_id` or `ref_id` field → direct link;
 *   - otherwise the assembler falls back to bbox overlap in {@link richHistoryForRef}.
 */
export function buildSnapshotObservationIndex(
	observations: Observation[],
	_messages: PersistedAgentMessage[],
): Map<string, { summary: string }[]> {
	const idx = new Map<string, { summary: string }[]>();
	for (const o of observations || []) {
		const snapId = (o as { snapshot_id?: string }).snapshot_id;
		const refId = (o as { ref_id?: string }).ref_id;
		const note = o.note || "";
		const entry = { summary: note };
		if (snapId) {
			const key = snapId.startsWith("ref_") ? snapId : `ref_${snapId}`;
			const list = idx.get(key) || [];
			list.push(entry);
			idx.set(key, list);
		}
		if (refId) {
			const list = idx.get(refId) || [];
			list.push(entry);
			idx.set(refId, list);
		}
	}
	return idx;
}

// =========================================================================== //
// No-checkpoint fallback (Phase 1 behavior)
// =========================================================================== //

/**
 * Assemble without a checkpoint: pure Phase 1 transform behavior (no stable
 * region, no overview verification). Used when the session has no checkpoint
 * yet (the runner's ensureCheckpoint will build one on the next run).
 *
 * Returns the rebuilt messages AND the explicit eviction list (P2-6) so the
 * caller can populate the §12 metric without diffing.
 */
async function assembleWithoutCheckpoint(
	messages: AgentMessage[],
	deps: RequestAssemblerDeps,
	snap: AssemblerSessionSnapshot,
	signal: AbortSignal | undefined,
	onRegionFetchMs: (ms: number) => void,
): Promise<{ out: AgentMessage[]; evictedRefIds: string[]; overflowTokens: number }> {
	// Reuse the recent-slice transform with an empty observations index and no
	// overview ref. The "recent slice" here is the full message list (no
	// checkpoint boundary to cut at). Region-fetch time is reported via the
	// onRegionFetchMs callback inside transformRecentSlice (do NOT also add it
	// here — that would double-count).
	const observationsIndex = buildSnapshotObservationIndex(snap.observations, snap.messages);
	const res = await transformRecentSlice({
		messages: messages as PersistedAgentMessage[],
		deps,
		overviewRefId: null,
		pendingSnapshotId: snap.pendingSnapshotId,
		observations: snap.observations,
		observationsIndex,
		signal,
		onRegionFetchMs,
	});
	return {
		out: stripContextMeta(res.messages as PersistedAgentMessage[]) as AgentMessage[],
		evictedRefIds: res.evictedRefIds,
		overflowTokens: res.overflowTokens,
	};
}

// =========================================================================== //
// Metrics helpers (§12)
// =========================================================================== //

function emitMetrics(
	deps: RequestAssemblerDeps,
	_snap: AssemblerSessionSnapshot,
	cp: ContextCheckpoint | null,
	metrics: AssemblerMetrics,
): void {
	if (!deps.metricsSink) return;
	deps.metricsSink({ ...metrics, checkpoint_generation: cp?.generation ?? metrics.checkpoint_generation });
}

function countImageBlocks(messages: AgentMessage[]): number {
	let n = 0;
	for (const m of messages) {
		const content = (m as { content?: unknown }).content;
		if (!Array.isArray(content)) continue;
		for (const part of content) {
			if (part && isImageContent(part)) n += 1;
		}
	}
	return n;
}

function countImageBytes(messages: AgentMessage[]): number {
	let total = 0;
	for (const m of messages) {
		const content = (m as { content?: unknown }).content;
		if (!Array.isArray(content)) continue;
		for (const part of content) {
			if (part && isImageContent(part)) {
				total += decodedBase64Bytes(part.data);
			}
		}
	}
	return total;
}

function collectEvictedRefs(before: PersistedAgentMessage[], after: AgentMessage[]): string[] {
	// P2-6: this heuristic ("all refs in `before` are evicted") is wrong — it
	// ignores `after` entirely and over-reports evictions. The selectors now
	// return the explicit eviction list via {@link transformRecentSlice}, so
	// this helper is DEPRECATED and kept only for backwards-compat callers. It
	// is no longer used on the production path.
	const evicted: string[] = [];
	const beforeRefs = new Set<string>();
	for (const m of before) {
		const content = (m as { content?: unknown }).content;
		if (!Array.isArray(content)) continue;
		for (const part of content) {
			if (part && isImageRefContent(part)) beforeRefs.add(part.ref_id);
		}
	}
	void after;
	for (const ref of beforeRefs) evicted.push(ref);
	return evicted;
}

function decodedBase64Bytes(b64: string): number {
	const len = b64.length;
	if (len === 0) return 0;
	const padding = b64.endsWith("==") ? 2 : b64.endsWith("=") ? 1 : 0;
	return Math.max(0, Math.floor(len * 3 / 4) - padding);
}

/** Replace every image_ref with degrade text; leave other content alone. */
function stripImageRefsToDegrade(messages: AgentMessage[]): AgentMessage[] {
	return messages.map((m) => {
		const role = (m as { role?: string }).role;
		if (role !== "user" && role !== "assistant" && role !== "toolResult") return m;
		const content = (m as { content?: unknown }).content;
		if (!Array.isArray(content)) return m;
		let touched = false;
		const newContent = content.map((part): unknown => {
			if (part && isImageRefContent(part)) {
				touched = true;
				return { type: "text", text: DEGRADE_TEXT };
			}
			return part;
		});
		return touched ? ({ ...(m as object), content: newContent } as AgentMessage) : m;
	});
}

// =========================================================================== //
// Concurrency pool (signal-aware, mirrors transform-context.ts)
// =========================================================================== //

async function mapPool<T, R>(
	items: T[],
	concurrency: number,
	fn: (item: T, index: number) => Promise<R>,
	signal?: AbortSignal,
): Promise<R[]> {
	const results: R[] = new Array(items.length);
	let next = 0;
	async function worker(): Promise<void> {
		for (;;) {
			if (signal?.aborted) return;
			const i = next++;
			if (i >= items.length) return;
			results[i] = await fn(items[i]!, i);
		}
	}
	const n = Math.min(Math.max(1, concurrency), Math.max(1, items.length));
	if (items.length === 0) return results;
	await Promise.all(Array.from({ length: n }, () => worker()));
	return results;
}

// =========================================================================== //
// Re-exports for the runner
// =========================================================================== //

export {
	buildOverviewDerivative,
	buildStablePrefixObject,
	canonicalSerialize,
	selectOverviewRef,
	stablePrefixHash,
	LEGACY_PLACEHOLDER_TEXT,
};
