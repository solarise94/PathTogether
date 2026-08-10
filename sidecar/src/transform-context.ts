/**
 * AI reading assistant sidecar — pi transformContext hook (Step 4).
 *
 * Replaces the Python "materialize image_ref at request time + cap recent
 * images" logic (ai_session.py:845 `_materialize_image_ref` /
 * `materialize_request_messages`, plus the implicit cap on carried snapshots).
 *
 * pi calls {@link AgentOptions.transformContext} on every LLM request, AFTER
 * the agent state is built but BEFORE {@link AgentOptions.convertToLlm}
 * (agent-loop.ts:288-292). Our hook does two jobs there:
 *
 *   1. **Pre-evict**: decide which image positions to KEEP (first overview +
 *      last N non-overview). Evicted `image_ref`s become placeholder text
 *      without calling flask.region.
 *   2. **Materialize (rehydrate)**: turn KEEP `image_ref` blocks into real pi
 *      `image` blocks via flask.region (concurrency capped). Slide
 *      fingerprint mismatch or fetch failure → degrade to
 *      `"该图因切片变更不可用。"`.
 *
 * Contract: the hook MUST NOT throw or reject. On any error we return a safe
 * fallback that replaces every `image_ref` with degrade text (never leave
 * `image_ref` in the output). Pure read-only transform: never mutate
 * `agent.state.messages`.
 *
 * **All `image_ref` blocks are guaranteed removed from the output** — pi's
 * `defaultConvertToLlm` only filters by role, it does not rewrite content, so a
 * leftover `image_ref` would reach the LLM and break serialization.
 *
 * Phase 1 upgrades (see docs/ai-context-cache-visual-workspace-upgrade.md):
 *   - AbortSignal passthrough end-to-end (§13): hook → mapPool → materializeRef
 *     → flask.region, with in-flight request coalescing per cache key (§12.1).
 *   - Aspect-preserving adaptive sizing (§6.1/§6.2): target_long_edge chosen by
 *     bbox coverage tier; server keeps aspect ratio via max_long_edge.
 *   - Byte-budget LRU + deterministic derivative spec (§6.3/§6.4): the cache
 *     key is the full derivative spec and eviction is by total bytes.
 */
import type { AgentMessage } from "@earendil-works/pi-agent-core";
import type { ImageContent } from "@earendil-works/pi-ai";

import type { FlaskClient } from "./flask-client.js";
import { FlaskHttpError } from "./flask-client.js";
import type { SlideInfo } from "./tools.js";
import { isImageContent, isImageRefContent, stripContextMeta, type ImageRefContent, type PersistedAgentMessage } from "./session-store.js";
import { estimateImageRefTokens, enforceVisualTokenBudget, resetVisualBudgetOverflow } from "./compaction.js";

const PLACEHOLDER_TEXT = "（历史快照已省略，可用 goto+snapshot 重新查看）";
const DEGRADE_TEXT = "该图因切片变更不可用。";
/** Exposed for the assembler so it can fall back to the same degraded copy. */
export { PLACEHOLDER_TEXT as LEGACY_PLACEHOLDER_TEXT, DEGRADE_TEXT };
/** Default visual working set size when the new config field is absent. */
const DEFAULT_VISUAL_WORKING_SET_MAX = 4;
/** Default per-derivative target long edges (§6.1). */
const DEFAULT_OVERVIEW_LONG_EDGE = 1024;
const DEFAULT_WORKING_IMAGE_LONG_EDGE = 768;
const DEFAULT_DETAIL_IMAGE_LONG_EDGE = 1280;
/** Default derivative encoding spec (§6.3, coupled to app.py constants). */
const DEFAULT_JPEG_QUALITY = 85;
const DEFAULT_OVERLAY_VERSION = "v1";
const ENCODER_ID = "pillow";
const RESIZE_ALGORITHM = "LANCZOS";
/** Default region materialize concurrency (replaced by config when present). */
const DEFAULT_REGION_CONCURRENCY = 3;
/** Default LRU byte budget (64 MB) and TTL (30 min) — §6.4. */
const DEFAULT_LRU_MAX_MB = 64;
const DEFAULT_LRU_TTL_MS = 1800_000;
const MB_BYTES = 1024 * 1024;
/** Default per-request visual token hard budget (§9.1/§11). */
const DEFAULT_VISUAL_CONTEXT_BUDGET_TOKENS = 8000;

// =========================================================================== //
// Public config
// =========================================================================== //

/**
 * Tuning knobs for {@link makeTransformContext}. Carries the Phase 1 image
 * pipeline config fields (§11); legacy {@link TransformContextConfig.keep_recent_images}
 * is accepted and mapped to {@link TransformContextConfig.visual_working_set_max}.
 */
export interface TransformContextConfig {
	/** Legacy: max materialized image blocks retained per request. */
	keep_recent_images?: number;
	/** Max materialized non-overview images retained per request (§11). */
	visual_working_set_max?: number;
	/** Per-request visual token hard budget (§9.1, informational in Phase 1). */
	visual_context_budget_tokens?: number;
	/** Overview image longest edge px (§6.1). */
	overview_long_edge?: number;
	/** Working / recent image longest edge px (§6.1). */
	working_image_long_edge?: number;
	/** Current high-power detail image longest edge px (§6.1). */
	detail_image_long_edge?: number;
	/** Deterministic JPEG quality (§6.3). */
	image_jpeg_quality?: number;
	/** Coord-tick overlay version (§6.3). */
	image_overlay_version?: string;
	/** Region materialize concurrency cap (replaces hardcoded 3). */
	region_materialize_concurrency?: number;
	/** Derivative LRU total byte budget in MB (§6.4). */
	image_derivative_cache_max_mb?: number;
	/** Derivative LRU TTL in seconds (§6.4). */
	image_derivative_cache_ttl?: number;
}

/** Resolved settings (all fields populated with defaults). */
export interface TransformContextSettings {
	keepRecentImages: number;
	overviewLongEdge: number;
	workingImageLongEdge: number;
	detailImageLongEdge: number;
	jpegQuality: number;
	overlayVersion: string;
	regionConcurrency: number;
	lruMaxBytes: number;
	lruTtlMs: number;
	/**
	 * Per-request visual token HARD budget (§9.1). The assembler and the Phase 1
	 * selectors evict the oldest ordinary kept images until the estimated
	 * selected visual tokens <= this value. Default 8000 (§11). P2-3.
	 */
	visualContextBudgetTokens: number;
}

function numOr(v: unknown, def: number): number {
	const n = Number(v);
	return Number.isFinite(n) && n > 0 ? n : def;
}

export function resolveTransformSettings(cfg: TransformContextConfig): TransformContextSettings {
	// visual_working_set_max takes precedence; fall back to legacy
	// keep_recent_images; both absent → default.
	const vwsmRaw = Number(cfg.visual_working_set_max);
	const kriRaw = Number(cfg.keep_recent_images);
	let keepRecentImages: number;
	if (Number.isFinite(vwsmRaw) && vwsmRaw >= 0) {
		// New field wins; an explicit 0 means "keep zero non-overview images".
		keepRecentImages = Math.floor(vwsmRaw);
	} else if (Number.isFinite(kriRaw) && kriRaw > 0) {
		keepRecentImages = Math.floor(kriRaw);
	} else {
		keepRecentImages = DEFAULT_VISUAL_WORKING_SET_MAX;
	}
	const lruMaxMb = numOr(cfg.image_derivative_cache_max_mb, DEFAULT_LRU_MAX_MB);
	const lruTtlS = numOr(cfg.image_derivative_cache_ttl, DEFAULT_LRU_TTL_MS / 1000);
	return {
		keepRecentImages,
		overviewLongEdge: numOr(cfg.overview_long_edge, DEFAULT_OVERVIEW_LONG_EDGE),
		workingImageLongEdge: numOr(cfg.working_image_long_edge, DEFAULT_WORKING_IMAGE_LONG_EDGE),
		detailImageLongEdge: numOr(cfg.detail_image_long_edge, DEFAULT_DETAIL_IMAGE_LONG_EDGE),
		jpegQuality: numOr(cfg.image_jpeg_quality, DEFAULT_JPEG_QUALITY),
		overlayVersion: typeof cfg.image_overlay_version === "string" && cfg.image_overlay_version.trim()
			? cfg.image_overlay_version.trim()
			: DEFAULT_OVERLAY_VERSION,
		regionConcurrency: numOr(cfg.region_materialize_concurrency, DEFAULT_REGION_CONCURRENCY),
		lruMaxBytes: Math.floor(lruMaxMb * MB_BYTES),
		lruTtlMs: Math.floor(lruTtlS * 1000),
		// P2-3 (§9.1): visual token hard budget. visual_context_budget_tokens <= 0
		// disables the budget (treated as "no limit"); a positive value enforces
		// the HARD cap in both selectors.
		visualContextBudgetTokens: (() => {
			const v = Number(cfg.visual_context_budget_tokens);
			return Number.isFinite(v) && v > 0 ? v : DEFAULT_VISUAL_CONTEXT_BUDGET_TOKENS;
		})(),
	};
}

// =========================================================================== //
// Region LRU (module-level, byte-budget)
// =========================================================================== //

type RegionLruEntry = {
	data: string;
	mime: string;
	cachedAt: number;
	slide: string;
	fingerprint: string;
	/** Decoded byte length of the base64 payload (for byte-budget eviction). */
	bytes: number;
};

/**
 * Derivative spec used both as the LRU/in-flight key and for recording. Built
 * from the config snapshot so it is computable BEFORE the region request (needed
 * for in-flight coalescing, §12.1).
 */
export interface DerivativeSpec {
	slide: string;
	fingerprint: string;
	x: number;
	y: number;
	w: number;
	h: number;
	targetLongEdge: number;
	jpegQuality: number;
	overlayVersion: string;
	resizeAlgorithm: string;
	encoderId: string;
}

/** Successful region fetches keyed by the full derivative spec (§6.3). */
const regionLru = new Map<string, RegionLruEntry>();
/** Current byte-budget ceiling; updated by resolveTransformSettings per run. */
let regionLruMaxBytes = DEFAULT_LRU_MAX_MB * MB_BYTES;
/** Current TTL; updated by resolveTransformSettings per run. */
let regionLruTtlMs = DEFAULT_LRU_TTL_MS;

/**
 * LRU hit/miss counters for §12 metrics. Reset by {@link resetLruCounters}.
 * Phase 2b: the assembler / metrics sink read these after each request.
 */
let lruHitCount = 0;
let lruMissCount = 0;

/** Reset the LRU hit/miss counters (call at the start of a metrics window). */
export function resetLruCounters(): void {
	lruHitCount = 0;
	lruMissCount = 0;
	// Phase 3.1: the budget-overflow counter (compaction.ts) is per-request too.
	resetVisualBudgetOverflow();
}

/** Current LRU hit count since the last {@link resetLruCounters} (§12). */
export function lruHitCount_value(): number {
	return lruHitCount;
}

/** Current LRU miss count since the last {@link resetLruCounters} (§12). */
export function lruMissCount_value(): number {
	return lruMissCount;
}

/** Test helper: reconfigure the LRU budget/TTL (mirrors run-time resolution). */
export function configureRegionLru(maxBytes: number, ttlMs: number): void {
	regionLruMaxBytes = Math.max(1, Math.floor(maxBytes));
	regionLruTtlMs = Math.max(1, Math.floor(ttlMs));
}

function normalizeSlideKey(slide: string): string {
	return String(slide || "").trim();
}

function derivativeKey(spec: DerivativeSpec): string {
	return [
		normalizeSlideKey(spec.slide),
		spec.fingerprint,
		`${spec.x},${spec.y},${spec.w},${spec.h}`,
		`le${spec.targetLongEdge}`,
		`q${spec.jpegQuality}`,
		`ov${spec.overlayVersion}`,
		spec.resizeAlgorithm,
		`${spec.encoderId}`,
	].join("|");
}

function regionLruGet(key: string, expectedFp: string, countMiss = true): { data: string; mime: string } | undefined {
	const hit = regionLru.get(key);
	if (!hit) {
		if (countMiss) lruMissCount += 1;
		return undefined;
	}
	if (hit.fingerprint !== expectedFp || Date.now() - hit.cachedAt > regionLruTtlMs) {
		regionLru.delete(key);
		if (countMiss) lruMissCount += 1;
		return undefined;
	}
	// Refresh recency (Map iteration order = insertion order).
	regionLru.delete(key);
	regionLru.set(key, hit);
	lruHitCount += 1;
	return { data: hit.data, mime: hit.mime };
}

/**
 * Peek the LRU without touching recency or counters. Used by the assembler's
 * overview content-hash verification (§10/§13): we need to know whether the
 * derivative is cached so we can decide to repair or rebuild, without consuming
 * a "hit" in the metrics.
 */
function regionLruPeek(key: string, expectedFp: string): { data: string; mime: string } | undefined {
	const hit = regionLru.get(key);
	if (!hit) return undefined;
	if (hit.fingerprint !== expectedFp || Date.now() - hit.cachedAt > regionLruTtlMs) {
		return undefined;
	}
	return { data: hit.data, mime: hit.mime };
}

/** Test/diagnostic helper: drop one derivative cache entry by key. */
function regionLruDelete(key: string): void {
	regionLru.delete(key);
}

function decodedBase64Bytes(b64: string): number {
	// base64 decoded length = floor(len * 3/4) minus padding chars.
	const len = b64.length;
	if (len === 0) return 0;
	const padding = b64.endsWith("==") ? 2 : b64.endsWith("=") ? 1 : 0;
	return Math.max(0, Math.floor(len * 3 / 4) - padding);
}

function regionLruSet(
	key: string,
	entry: { data: string; mime: string; slide: string; fingerprint: string },
): void {
	const bytes = decodedBase64Bytes(entry.data);
	if (bytes <= 0) return; // never cache empty payloads
	if (regionLru.has(key)) regionLru.delete(key);
	regionLru.set(key, { ...entry, cachedAt: Date.now(), bytes });
	evictLru();
}

/** Evict oldest entries until the total cached bytes fit the budget. */
function evictLru(): void {
	let total = 0;
	for (const e of regionLru.values()) total += e.bytes;
	while (total > regionLruMaxBytes && regionLru.size > 0) {
		const oldest = regionLru.keys().next().value;
		if (oldest === undefined) break;
		const e = regionLru.get(oldest);
		regionLru.delete(oldest);
		if (e) total -= e.bytes;
	}
}

/** Test helper: total bytes currently cached. */
export function regionLruBytes(): number {
	let total = 0;
	for (const e of regionLru.values()) total += e.bytes;
	return total;
}

/** Test helper: number of entries currently cached. */
export function regionLruSize(): number {
	return regionLru.size;
}

/** Test helper: clear the materialized-region LRU. */
export function clearRegionLru(): void {
	regionLru.clear();
}

/**
 * Drop LRU entries for one slide (or all when `slide` is omitted). Used when
 * Flask reports fingerprint mismatch / slideInfo refreshes to a new fingerprint.
 */
export function invalidateRegionLru(slide?: string): void {
	if (!slide) {
		regionLru.clear();
		return;
	}
	const prefix = `${normalizeSlideKey(slide)}|`;
	for (const key of [...regionLru.keys()]) {
		if (key.startsWith(prefix)) regionLru.delete(key);
	}
}

// =========================================================================== //
// In-flight coalescing (§12.1)
// =========================================================================== //

/**
 * One in-flight derivative request. Subscribers each hold their own AbortSignal;
 * the underlying fetch is only aborted when there are NO subscribers left (§12.1
 * /§16.2).
 *
 * Each subscriber gets its OWN promise (§16.2: "用户取消后不再等待无关历史图片
 * 请求完成"). The per-subscriber promise is wired to the shared underlying fetch
 * via `entry.promise.then(resolve, reject)`; if the subscriber's own signal
 * aborts, its promise rejects IMMEDIATELY with an AbortError and detaches from
 * the entry — without waiting for the shared fetch to settle. The shared fetch
 * itself is only aborted when the last subscriber leaves, so a cancelling
 * subscriber never aborts another subscriber's in-flight image.
 */
type Subscriber = {
	signal: AbortSignal;
	/**
	 * Resolve/reject the per-subscriber promise. Tied to {@link InFlightEntry.promise}
	 * via `.then`; detached on settle so we never double-settle.
	 */
	resolve: (v: { data: string; mime: string; encoder?: unknown }) => void;
	reject: (e: unknown) => void;
	/** Whether this subscriber has already settled (avoid double settle). */
	settled: boolean;
	/** Per-subscriber abort listener; detached on removal. */
	detach: () => void;
};

type InFlightEntry = {
	promise: Promise<{ data: string; mime: string; encoder?: unknown }>;
	subscribers: Set<Subscriber>;
	/** AbortController driving the underlying flask fetch. */
	fetchController: AbortController;
};

const inFlight = new Map<string, InFlightEntry>();

/**
 * Detach a subscriber's abort listener and drop it from the entry. When no
 * subscribers remain, abort the underlying fetch so we do not keep doing work
 * nobody wants (§12.1: "只有同一 in-flight 派生请求已无订阅者时才中止底层
 * region fetch").
 */
function detachSubscriberKey(entry: InFlightEntry, sub: Subscriber): void {
	sub.detach();
	entry.subscribers.delete(sub);
	if (entry.subscribers.size === 0 && !entry.fetchController.signal.aborted) {
		entry.fetchController.abort();
	}
}

/**
 * Build a fresh per-subscriber promise that resolves with the shared fetch's
 * result, or rejects immediately when THIS subscriber's signal fires. The
 * promise is settled exactly once; the listener is detached on settle to avoid
 * leaks (§16.2).
 */
function addSubscriber(entry: InFlightEntry, signal: AbortSignal): {
	promise: Promise<{ data: string; mime: string; encoder?: unknown }>;
	sub: Subscriber;
} {
	// Per-subscriber promise; settled either by the shared fetch (resolve/reject
	// forwarded) or by this subscriber's own abort (immediate reject).
	const sub: Subscriber = { signal, resolve: () => undefined, reject: () => undefined, settled: false, detach: () => undefined };
	const promise = new Promise<{ data: string; mime: string; encoder?: unknown }>((resolve, reject) => {
		sub.resolve = resolve;
		sub.reject = reject;
	});
	// Forward the shared fetch's outcome to this subscriber exactly once.
	const onSettle = (ok: boolean): (v: unknown, e?: unknown) => void => {
		return ok
			? (v: unknown): void => {
					if (sub.settled) return;
					sub.settled = true;
					sub.detach();
					entry.subscribers.delete(sub);
					sub.resolve(v as { data: string; mime: string; encoder?: unknown });
				}
			: (_v: unknown, e: unknown): void => {
					if (sub.settled) return;
					sub.settled = true;
					sub.detach();
					entry.subscribers.delete(sub);
					sub.reject(e);
				};
	};
	const onFulfilled = onSettle(true);
	const onRejected = (e: unknown): void => onSettle(false)(undefined, e);
	entry.promise.then(onFulfilled, onRejected);
	// Abort listener: reject this subscriber immediately and detach (the shared
	// fetch continues for the remaining subscribers; the underlying fetch only
	// aborts when detachSubscriberKey drops the last one).
	const onAbort = (): void => {
		if (sub.settled) return;
		sub.settled = true;
		// Detach + drop BEFORE rejecting so the bookkeeping is consistent even
		// if the reject path throws synchronously into the caller.
		detachSubscriberKey(entry, sub);
		const err = typeof DOMException !== "undefined"
			? new DOMException("The operation was aborted.", "AbortError")
			: new Error("aborted");
		sub.reject(err);
	};
	sub.detach = (): void => signal.removeEventListener("abort", onAbort);
	entry.subscribers.add(sub);
	if (signal.aborted) {
		onAbort();
	} else {
		signal.addEventListener("abort", onAbort, { once: true });
	}
	return { promise, sub };
}

/**
 * Subscribe to (or start) an in-flight derivative fetch for `key`. Same-key
 * callers share ONE region call; each subscriber receives its OWN promise that
 * rejects immediately when ITS signal aborts (§16.2). The underlying fetch is
 * only aborted when the last subscriber leaves (§12.1).
 */
function subscribeDerivative(
	key: string,
	start: (fetchSignal: AbortSignal) => Promise<{ data: string; mime: string; encoder?: unknown }>,
	signal?: AbortSignal,
): Promise<{ data: string; mime: string; encoder?: unknown }> {
	const existing = inFlight.get(key);
	const subSig = signal ?? new AbortController().signal;
	if (existing) {
		const { promise } = addSubscriber(existing, subSig);
		return promise;
	}
	const fetchController = new AbortController();
	const subscribers = new Set<Subscriber>();
	const entry: InFlightEntry = { promise: undefined as never, subscribers, fetchController };
	const promise = start(fetchController.signal).finally(() => {
		// Always clean the entry on settle (success/failure/abort). Detach any
		// stragglers so their abort listeners don't leak (a long-lived session
		// signal would otherwise retain this entry's closure).
		const e = inFlight.get(key);
		if (e === entry) inFlight.delete(key);
		for (const sub of entry.subscribers) sub.detach();
		entry.subscribers.clear();
	});
	entry.promise = promise;
	inFlight.set(key, entry);
	const { promise: subPromise } = addSubscriber(entry, subSig);
	return subPromise;
}

// =========================================================================== //
// Overview identification
// =========================================================================== //

/**
 * Whether an image_ref is the protected first overview.
 *
 * Authoritative: `ref_id === "ref_${firstSnapshotToolCallId}"`.
 * Coverage fallback (>90% of slide width): only the FIRST such image in
 * message order when `firstSnapshotToolCallId` is null.
 */
function isOverviewImageRef(
	ref: ImageRefContent,
	slideInfo: SlideInfo,
	firstSnapshotToolCallId: string | null,
	coverageOverviewClaimed: boolean,
): { overview: boolean; claimCoverage: boolean } {
	if (firstSnapshotToolCallId) {
		if (ref.ref_id === `ref_${firstSnapshotToolCallId}`) {
			return { overview: true, claimCoverage: false };
		}
		return { overview: false, claimCoverage: false };
	}
	const src = ref.src;
	if (src && typeof src.w === "number" && slideInfo.width > 0) {
		const cov = (src.w / slideInfo.width) * 100;
		if (cov > 90 && !coverageOverviewClaimed) {
			return { overview: true, claimCoverage: true };
		}
	}
	return { overview: false, claimCoverage: false };
}

/**
 * Live `ImageContent` inside a toolResult is overview iff the parent message's
 * toolCallId matches firstSnapshotToolCallId.
 */
function isOverviewLiveImage(
	msg: AgentMessage,
	firstSnapshotToolCallId: string | null,
): boolean {
	if (!firstSnapshotToolCallId) return false;
	const role = (msg as { role?: string }).role;
	if (role !== "toolResult") return false;
	const toolCallId = (msg as { toolCallId?: string }).toolCallId;
	return toolCallId === firstSnapshotToolCallId;
}

/**
 * Whether an image position corresponds to the current pending snapshot (highest
 * eviction priority, §15.1). A pending snapshot's image_ref carries
 * `ref_id === ref_${pendingSnapshotId}`. We pass the pending id in when known.
 */
function isPendingImageRef(ref: ImageRefContent, pendingSnapshotId: string | null): boolean {
	if (!pendingSnapshotId) return false;
	return ref.ref_id === `ref_${pendingSnapshotId}`;
}

// =========================================================================== //
// Safe fallback (no image_ref left)
// =========================================================================== //

/** Replace every image_ref with degrade text; leave other content alone. */
function stripImageRefsToDegrade(messages: AgentMessage[]): AgentMessage[] {
	return messages.map((m) => {
		const role = (m as { role?: string }).role;
		if (role !== "user" && role !== "assistant" && role !== "toolResult") {
			return m;
		}
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
// Concurrency pool (signal-aware)
// =========================================================================== //

/**
 * Run async tasks with a fixed concurrency limit; preserve result order.
 *
 * Signal-aware (§13): once the signal aborts, no NEW tasks are started; in-
 * flight tasks depend on their own fetch abort (passed via `fn`). Aborted slots
 * reject so the caller can short-circuit.
 */
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
// makeTransformContext
// =========================================================================== //

/**
 * Build a pi transformContext hook bound to one session's flask client, slide
 * info, and tuning settings.
 *
 * @param firstSnapshotToolCallIdRef a mutable ref (object) so the runner can
 *   record the first snapshot's toolCallId as the session progresses; the hook
 *   reads it live each call. null until the first snapshot lands.
 * @param pendingSnapshotIdRef a mutable ref carrying the current pending
 *   snapshot id, so the pending image is prioritized over ordinary recent
 *   images (§15.1). null when no snapshot is pending.
 */
export function makeTransformContext(args: {
	flask: FlaskClient;
	slide: string;
	slideInfo: SlideInfo;
	settings: TransformContextSettings;
	firstSnapshotToolCallIdRef: { value: string | null };
	pendingSnapshotIdRef?: { value: string | null };
}): (messages: AgentMessage[], signal?: AbortSignal) => Promise<AgentMessage[]> {
	const { flask, slide, slideInfo, settings, firstSnapshotToolCallIdRef, pendingSnapshotIdRef } = args;
	// Apply the resolved LRU budget/TTL to the module-level cache for this run.
	configureRegionLru(settings.lruMaxBytes, settings.lruTtlMs);

	return async (messages, signal): Promise<AgentMessage[]> => {
		try {
			const out = await transformOnce(
				messages,
				flask,
				slide,
				slideInfo,
				settings,
				firstSnapshotToolCallIdRef,
				pendingSnapshotIdRef ?? { value: null },
				signal,
			);
			// Provider boundary (§10): strip sidecar-only _context_meta so the
			// model payload never carries session_message_seq.
			return stripContextMeta(out as PersistedAgentMessage[]) as AgentMessage[];
		} catch {
			// Never leave image_ref in the output; never throw. Also strip
			// _context_meta on the fallback path (§10 Provider boundary).
			return stripContextMeta(stripImageRefsToDegrade(messages) as PersistedAgentMessage[]) as AgentMessage[];
		}
	};
}

type ImgPos = {
	msgIdx: number;
	blkIdx: number;
	overview: boolean;
	pending: boolean;
	kind: "ref" | "image";
	ref?: ImageRefContent;
};

/**
 * Pre-evict then materialize. Pure: returns a new array, leaves inputs untouched.
 *
 *   1. Scan messages; collect image_ref + already-materialized image positions.
 *   2. Mark overview (identity / first coverage fallback), pending snapshot,
 *      and choose KEEP set (overview + pending + last N ordinary non-overview).
 *   3. Materialize only KEEP image_refs (concurrency cap). Evicted refs →
 *      placeholder text without flask.region.
 */
async function transformOnce(
	messages: AgentMessage[],
	flask: FlaskClient,
	slide: string,
	slideInfo: SlideInfo,
	settings: TransformContextSettings,
	firstSnapshotToolCallIdRef: { value: string | null },
	pendingSnapshotIdRef: { value: string | null },
	signal?: AbortSignal,
): Promise<AgentMessage[]> {
	const firstId = firstSnapshotToolCallIdRef.value;
	const pendingId = pendingSnapshotIdRef.value;
	const positions: ImgPos[] = [];
	let coverageOverviewClaimed = false;

	// Phase 1: scan.
	for (let msgIdx = 0; msgIdx < messages.length; msgIdx++) {
		const m = messages[msgIdx]!;
		const role = (m as { role?: string }).role;
		if (role !== "user" && role !== "assistant" && role !== "toolResult") continue;
		const content = (m as { content?: unknown }).content;
		if (!Array.isArray(content)) continue;
		for (let blkIdx = 0; blkIdx < content.length; blkIdx++) {
			const part = content[blkIdx];
			if (part && isImageRefContent(part)) {
				const { overview, claimCoverage } = isOverviewImageRef(part, slideInfo, firstId, coverageOverviewClaimed);
				if (claimCoverage) coverageOverviewClaimed = true;
				positions.push({
					msgIdx,
					blkIdx,
					overview,
					pending: isPendingImageRef(part, pendingId),
					kind: "ref",
					ref: part,
				});
			} else if (isImageContent(part)) {
				positions.push({
					msgIdx,
					blkIdx,
					overview: isOverviewLiveImage(m, firstId),
					pending: false,
					kind: "image",
				});
			}
		}
	}

	// Phase 2: decide KEEP.
	//   - overview always kept;
	//   - pending snapshot always kept (priority above ordinary recent, §15.1);
	//   - ordinary non-overview: keep the last N by message order.
	const keepKeys = new Set<string>();
	const ordinary: ImgPos[] = [];
	for (const p of positions) {
		if (p.overview || p.pending) {
			keepKeys.add(posKey(p));
		} else {
			ordinary.push(p);
		}
	}
	ordinary.sort((a, b) => rank(a) - rank(b)); // oldest first
	const keepFrom = Math.max(0, ordinary.length - settings.keepRecentImages);
	for (const p of ordinary.slice(keepFrom)) {
		keepKeys.add(posKey(p));
	}

	// Phase 2b (§9.1 / P2-3): enforce the visual token budget as a HARD cap.
	// Overview and pending positions are non-evictable; the oldest ordinary
	// kept images are evicted until the estimate fits the budget. Phase 1 path
	// has no stable-region overview (the overview is one of the kept positions),
	// so the overview cost is counted directly from the kept overview position.
	enforceTransformOnceBudget(keepKeys, positions, ordinary, keepFrom, settings);

	// Phase 3: materialize KEEP refs only (concurrency cap, signal-aware).
	// The newest kept *ordinary* (non-overview, non-pending) ref gets the detail
	// tier (§6.1 current high-power evidence image); pending snapshots also get
	// the detail tier (they are the "current" image under review).
	const keptOrdinary = positions
		.filter((p) => !p.overview && !p.pending && p.kind === "ref" && keepKeys.has(posKey(p)))
		.sort((a, b) => rank(a) - rank(b));
	const newestOrdinaryKey = keptOrdinary.length ? posKey(keptOrdinary[keptOrdinary.length - 1]!) : null;
	const toMaterialize = positions.filter((p) => p.kind === "ref" && p.ref && keepKeys.has(posKey(p)));
	const materialized = new Map<string, ImageContent | { type: "text"; text: string }>();
	await mapPool(
		toMaterialize,
		settings.regionConcurrency,
		async (p) => {
			const isDetail = p.pending || posKey(p) === newestOrdinaryKey;
			const block = await materializeRef(p.ref!, flask, slide, slideInfo, settings, p.overview, isDetail, signal);
			materialized.set(posKey(p), block);
			return block;
		},
		signal,
	);

	// Phase 4: rebuild messages.
	return messages.map((m, msgIdx) => {
		const role = (m as { role?: string }).role;
		if (role !== "user" && role !== "assistant" && role !== "toolResult") {
			return m;
		}
		const content = (m as { content?: unknown }).content;
		if (typeof content === "string" || !Array.isArray(content)) {
			return m;
		}
		let touched = false;
		const newContent = content.map((part, blkIdx): unknown => {
			const key = `${msgIdx}:${blkIdx}`;
			if (part && isImageRefContent(part)) {
				touched = true;
				if (!keepKeys.has(key)) {
					return { type: "text", text: PLACEHOLDER_TEXT };
				}
				return materialized.get(key) ?? { type: "text", text: DEGRADE_TEXT };
			}
			if (isImageContent(part)) {
				if (!keepKeys.has(key)) {
					touched = true;
					return { type: "text", text: PLACEHOLDER_TEXT };
				}
				return part;
			}
			return part;
		});
		return touched ? ({ ...(m as object), content: newContent } as AgentMessage) : m;
	});
}

function posKey(p: { msgIdx: number; blkIdx: number }): string {
	return `${p.msgIdx}:${p.blkIdx}`;
}

function rank(p: { msgIdx: number; blkIdx: number }): number {
	return p.msgIdx * 1_000_000 + p.blkIdx;
}

/**
 * Phase 2b (§9.1 / P2-3) budget enforcement for the Phase 1 {@link transformOnce}
 * selector. Thin adapter over the SHARED {@link enforceVisualTokenBudget}
 * (compaction.ts) — Phase 3.1 review: the two selectors had diverged, and both
 * charged the newest ordinary image at the working tier even though it is
 * materialized at the detail tier (1280 vs 768 px, ~1398 tokens short for a
 * square). The newest kept ordinary position is therefore charged at the
 * detail tier here.
 *
 * Overview and pending positions are NEVER evicted (§9.1). Mutates `keepKeys`
 * in place (removed keys are evicted).
 */
function enforceTransformOnceBudget(
	keepKeys: Set<string>,
	positions: ImgPos[],
	ordinary: ImgPos[],
	keepFrom: number,
	settings: TransformContextSettings,
): void {
	const budget = settings.visualContextBudgetTokens;
	if (!(Number.isFinite(budget) && budget > 0)) return;
	// Sum non-evictable (overview + pending) cost first.
	let baseline = 0;
	for (const p of positions) {
		const key = posKey(p);
		if (!keepKeys.has(key)) continue;
		if (p.overview) {
			baseline += estimateImageRefTokens({ w: settings.overviewLongEdge, h: settings.overviewLongEdge }, settings.overviewLongEdge);
		} else if (p.pending) {
			baseline += estimateImageRefTokens({ w: settings.detailImageLongEdge, h: settings.detailImageLongEdge }, settings.detailImageLongEdge);
		}
	}
	const keptOrdinary = ordinary.slice(keepFrom); // oldest → newest
	const newestKey = keptOrdinary.length ? posKey(keptOrdinary[keptOrdinary.length - 1]!) : null;
	// Cost each kept ordinary image at its FINAL materialization tier: the
	// newest ordinary is materialized at the detail tier (chooseTargetLongEdge),
	// so it must be charged at detail — not working (Phase 3.1 P1).
	const costOf = (p: ImgPos): number => {
		const isDetail = p.pending || posKey(p) === newestKey;
		const target = isDetail ? settings.detailImageLongEdge : settings.workingImageLongEdge;
		if (p.kind === "ref" && p.ref) {
			const src = p.ref.src || { x: 0, y: 0, w: 0, h: 0 };
			return estimateImageRefTokens({ w: src.w, h: src.h }, target);
		}
		return estimateImageRefTokens({ w: target, h: target }, target);
	};
	const sel = enforceVisualTokenBudget({
		budgetTokens: budget,
		baselineTokens: baseline,
		ordinary: keptOrdinary.map((p) => ({ key: posKey(p), tokens: costOf(p) })),
	});
	for (const key of sel.evictedKeys) keepKeys.delete(key);
}

/**
 * Choose the target longest edge for a derivative by coverage tier (§6.2).
 *
 * Simplified Phase 1 rule (per task spec):
 *   - overview image → overview_long_edge;
 *   - pending snapshot / most recent kept ordinary image → detail_image_long_edge;
 *   - other recent images → working_image_long_edge.
 *
 * `isMostRecent` marks the newest kept ordinary position so it gets the detail
 * tier (mirrors "current high-power evidence image").
 */
function chooseTargetLongEdge(
	settings: TransformContextSettings,
	overview: boolean,
	isDetail: boolean,
): number {
	if (overview) return settings.overviewLongEdge;
	if (isDetail) return settings.detailImageLongEdge;
	return settings.workingImageLongEdge;
}

/**
 * Materialize one KEEP image_ref. Fingerprint mismatch / empty / failure →
 * degrade text. Uses the module LRU + in-flight coalescing; never throws.
 *
 * The AbortSignal is threaded into flask.region; in-flight same-spec requests
 * are coalesced so a single subscriber's abort never aborts another's fetch.
 */
async function materializeRef(
	ref: ImageRefContent,
	flask: FlaskClient,
	slide: string,
	slideInfo: SlideInfo,
	settings: TransformContextSettings,
	overview: boolean,
	isDetail: boolean,
	signal?: AbortSignal,
): Promise<ImageContent | { type: "text"; text: string }> {
	const fp = ref.slide_fingerprint || "";
	if (fp && fp !== (slideInfo.fingerprint || "")) {
		return { type: "text", text: DEGRADE_TEXT };
	}

	const src = ref.src || { x: 0, y: 0, w: 0, h: 0 };
	const x = src.x;
	const y = src.y;
	const w = Math.max(1, src.w);
	const h = Math.max(1, src.h);
	const effectiveFp = slideInfo.fingerprint || fp;
	const targetLongEdge = chooseTargetLongEdge(settings, overview, isDetail);

	const spec: DerivativeSpec = {
		slide,
		fingerprint: effectiveFp,
		x,
		y,
		w,
		h,
		targetLongEdge,
		jpegQuality: settings.jpegQuality,
		overlayVersion: settings.overlayVersion,
		resizeAlgorithm: RESIZE_ALGORITHM,
		encoderId: ENCODER_ID,
	};
	const cacheKey = derivativeKey(spec);

	// LRU hit short-circuits (still respect an already-aborted signal).
	if (signal?.aborted) return { type: "text", text: DEGRADE_TEXT };
	const cached = regionLruGet(cacheKey, effectiveFp);
	if (cached) {
		return { type: "image", data: cached.data, mimeType: cached.mime };
	}

	try {
		const result = await subscribeDerivative(
			cacheKey,
			async (fetchSignal) => {
				const r = await flask.region({
					slide,
					x,
					y,
					w,
					h,
					max_long_edge: targetLongEdge,
					jpeg_quality: settings.jpegQuality,
					expected_fingerprint: fp || undefined,
					signal: fetchSignal,
				});
				return { data: r.image_base64, mime: r.mime, encoder: r.encoder };
			},
			signal,
		);
		const b64 = result.data || "";
		if (!b64) {
			return { type: "text", text: DEGRADE_TEXT };
		}
		const mime = result.mime || "image/jpeg";
		regionLruSet(cacheKey, { data: b64, mime, slide: normalizeSlideKey(slide), fingerprint: effectiveFp });
		return { type: "image", data: b64, mimeType: mime };
	} catch (e) {
		if (e instanceof FlaskHttpError && e.status === 409) {
			invalidateRegionLru(slide);
		}
		// AbortError (user-cancel / last-subscriber-left) → degrade text, not a throw.
		return { type: "text", text: DEGRADE_TEXT };
	}
}

// =========================================================================== //
// Test-visible helpers
// =========================================================================== //

/** Expose the encoder constants for the checkpoint spec (§6.3, Phase 2b). */
export const TRANSFORM_ENCODER_ID = ENCODER_ID;
export const TRANSFORM_RESIZE_ALGORITHM = RESIZE_ALGORITHM;

/**
 * Drop a single derivative cache entry that matches the given spec, when
 * present. Used by the assembler's overview hash-repair flow (§10/§13): on a
 * content_sha256 mismatch we delete the suspect cached entry before rebuilding
 * it with the checkpoint's encoding parameters.
 */
export function dropDerivative(spec: DerivativeSpec): void {
	regionLruDelete(derivativeKey(spec));
}

/**
 * Peek (without counters or recency refresh) whether a derivative is cached and
 * return its base64 + mime. Used by the assembler's overview verification.
 */
export function peekDerivative(spec: DerivativeSpec): { data: string; mime: string } | undefined {
	return regionLruPeek(derivativeKey(spec), spec.fingerprint);
}

/**
 * Insert (or overwrite) a derivative cache entry directly. Used by the
 * assembler after rebuilding an overview whose hash matched on the second
 * materialization (§10: "重建匹配→继续"), so subsequent requests hit the LRU.
 */
export function putDerivative(
	spec: DerivativeSpec,
	entry: { data: string; mime: string },
): void {
	regionLruSet(derivativeKey(spec), {
		data: entry.data,
		mime: entry.mime,
		slide: normalizeSlideKey(spec.slide),
		fingerprint: spec.fingerprint,
	});
}

/**
 * Materialize ONE derivative from a known src bbox + slide fingerprint, using
 * the derivative LRU + in-flight coalescing (§6.3/§7.1). Used by:
 *   - the overview back-fill (§10) to materialize the stable overview once and
 *     compute its content_sha256;
 *   - the assembler's overview hash-repair rebuild (§10/§13).
 *
 * Unlike {@link materializeRef}, this function THROWS on failure (the overview
 * path must be able to detect a permanent failure and retire the checkpoint,
 * §3.2). Transient errors are still swallowed into DEGRADE_TEXT only for the
 * non-overview materializeRef path.
 *
 * @returns the base64 + mime + encoder info (when Flask returns it).
 */
export async function materializeDerivativeRaw(args: {
	flask: FlaskClient;
	slide: string;
	slideInfo: SlideInfo;
	spec: DerivativeSpec;
	/** Pass the ref's slide_fingerprint so Flask can 409 on a mismatch. */
	expectedFingerprint?: string;
	signal?: AbortSignal;
}): Promise<{ data: string; mime: string; encoder?: { id: string; version: string; resize: string; overlay_version: string; jpeg_quality: number } }> {
	const { flask, slide, slideInfo, spec, signal } = args;
	const cacheKey = derivativeKey(spec);
	const fp = spec.fingerprint || "";

	if (signal?.aborted) {
		throw new Error("materializeDerivativeRaw aborted");
	}
	// Note: we do NOT consume a hit/miss counter here; the overview path uses
	// peekDerivative for its own verification and only counts real region calls.
	const cached = regionLruGet(cacheKey, fp, false);
	if (cached) {
		return { data: cached.data, mime: cached.mime };
	}

	const result = await subscribeDerivative(
		cacheKey,
		async (fetchSignal) => {
			const r = await flask.region({
				slide,
				x: spec.x,
				y: spec.y,
				w: spec.w,
				h: spec.h,
				max_long_edge: spec.targetLongEdge,
				jpeg_quality: spec.jpegQuality,
				expected_fingerprint: args.expectedFingerprint || fp || undefined,
				signal: fetchSignal,
			});
			return { data: r.image_base64, mime: r.mime, encoder: r.encoder };
		},
		signal,
	);
	const b64 = result.data || "";
	if (!b64) {
		throw new Error("materializeDerivativeRaw: empty payload");
	}
	const mime = result.mime || "image/jpeg";
	regionLruSet(cacheKey, { data: b64, mime, slide: normalizeSlideKey(slide), fingerprint: fp });
	const encoder = result.encoder as { id: string; version: string; resize: string; overlay_version: string; jpeg_quality: number } | undefined;
	return { data: b64, mime, encoder };
}

/**
 * Build a {@link DerivativeSpec} for the stable overview (§6.1: longest edge
 * = overview_long_edge). Exported so the assembler and the checkpoint
 * back-fill build the SAME spec / cache key from the same inputs.
 */
export function overviewDerivativeSpec(args: {
	slide: string;
	fingerprint: string;
	src: { x: number; y: number; w: number; h: number };
	targetLongEdge: number;
	jpegQuality: number;
	overlayVersion: string;
}): DerivativeSpec {
	return {
		slide: args.slide,
		fingerprint: args.fingerprint,
		x: args.src.x,
		y: args.src.y,
		w: Math.max(1, args.src.w),
		h: Math.max(1, args.src.h),
		targetLongEdge: args.targetLongEdge,
		jpegQuality: args.jpegQuality,
		overlayVersion: args.overlayVersion,
		resizeAlgorithm: RESIZE_ALGORITHM,
		encoderId: ENCODER_ID,
	};
}

/**
 * Count image blocks (materialized images, not refs) in a message array. Used
 * by tests to assert the eviction outcome.
 */
export function countImageBlocks(messages: AgentMessage[] | PersistedAgentMessage[]): number {
	let n = 0;
	for (const m of messages) {
		const content = (m as { content?: unknown }).content;
		if (!Array.isArray(content)) continue;
		for (const part of content) {
			if (isImageContent(part)) n += 1;
		}
	}
	return n;
}

/**
 * Assert no `image_ref` blocks remain (transform output invariant). Returns
 * true when clean. Used by tests.
 */
export function hasNoImageRefBlocks(messages: AgentMessage[] | PersistedAgentMessage[]): boolean {
	for (const m of messages) {
		const content = (m as { content?: unknown }).content;
		if (!Array.isArray(content)) continue;
		for (const part of content) {
			if (isImageRefContent(part)) return false;
		}
	}
	return true;
}

// =========================================================================== //
// Rich-text history (§7.2, Phase 2b)
// =========================================================================== //

/**
 * One observation relevant to a given image ref (§7.2). Built by the assembler
 * from `SessionData.observations` and matched to a ref by bbox overlap or an
 * explicit snapshot-id link.
 */
export interface RichHistoryObservation {
	summary: string;
}

/**
 * Build the §7.2 rich-text history block for an evicted image_ref. Used by the
 * assembler when a ref is dropped from the visual working set:
 *
 *   历史快照 ref={ref_id}：
 *   - level-0 bbox=(x,y,w,h)
 *   - 放大倍率={magnification}
 *   - 观察摘要={summary 或“尚无结构化观察”}
 *   - 如需复核，可 goto bbox 中心后重新 snapshot
 *
 * Per §7.2: "已经产生 observation 的图片必须携带 observation 文本；未产生
 * observation 的图片不得伪造结论。" When `observations` is empty / no matching
 * observation is found, the summary line is "尚无结构化观察" — NEVER fabricated.
 */
export function buildRichHistoryText(
	ref: ImageRefContent,
	observations: RichHistoryObservation[] = [],
): string {
	const s = ref.src || { x: 0, y: 0, w: 0, h: 0 };
	// §7.2: 观察摘要 is the OBSERVATION summary, not the image_ref's own
	// summary caption. When no observation exists, the line MUST say
	// "尚无结构化观察" — never fabricate from the ref's caption.
	const summaryText = observations.length > 0
		? observations.map((o) => o.summary).filter(Boolean).join("；") || "尚无结构化观察"
		: "尚无结构化观察";
	const lines = [
		`历史快照 ref=${ref.ref_id}：`,
		`- level-0 bbox=(${s.x},${s.y},${s.w},${s.h})`,
		`- 放大倍率=${ref.magnification || "未知"}`,
		`- 观察摘要=${summaryText}`,
		`- 如需复核，可 goto bbox 中心后重新 snapshot`,
	];
	return lines.join("\n");
}

/**
 * Build the §7.2 rich-text block for an evicted image_ref, choosing between the
 * rich form (with observations) and a short placeholder when no observations
 * apply. The assembler passes the full observations list; this helper filters
 * by bbox overlap (§7.2: "ref 的 bbox 与 observation bbox 匹配或 observation
 * 携带 snapshot 关联时挂上").
 *
 * @param ref the evicted image_ref
 * @param observations raw observations to filter (each may carry bbox + note)
 * @param snapshotIdToObservations optional precomputed index of snapshot_id →
 *   observations, when the observation carries an explicit snapshot link
 *   (Phase 2b falls back to bbox overlap when this is absent).
 */
export function richHistoryForRef(
	ref: ImageRefContent,
	observations: { bbox?: { x?: number; y?: number; w?: number; h?: number }; note?: string; [k: string]: unknown }[] = [],
	snapshotIdToObservations?: Map<string, RichHistoryObservation[]>,
): string {
	const refId = ref.ref_id || "";
	// Explicit snapshot-id link wins (§7.2: "observation 携带 snapshot 关联").
	const linked = snapshotIdToObservations?.get(refId);
	if (linked && linked.length > 0) {
		return buildRichHistoryText(ref, linked);
	}
	// Fallback: bbox overlap.
	const rs = ref.src;
	const matched: RichHistoryObservation[] = [];
	if (rs && (rs.w > 0 || rs.h > 0)) {
		for (const o of observations) {
			const b = o.bbox;
			if (!b) continue;
			if (bboxOverlap(rs, b)) {
				matched.push({ summary: o.note || "" });
			}
		}
	}
	return buildRichHistoryText(ref, matched);
}

/** Whether two bboxes overlap (closed-interval). */
function bboxOverlap(
	a: { x: number; y: number; w: number; h: number },
	b: { x?: number; y?: number; w?: number; h?: number },
): boolean {
	const bx = Number(b.x ?? NaN);
	const by = Number(b.y ?? NaN);
	const bw = Number(b.w ?? NaN);
	const bh = Number(b.h ?? NaN);
	if (![bx, by, bw, bh].every(Number.isFinite)) return false;
	const noOverlap = a.x + a.w <= bx || bx + bw <= a.x || a.y + a.h <= by || by + bh <= a.y;
	return !noOverlap;
}
