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
 *      `image` blocks via flask.region (concurrency capped at 3). Slide
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
 */
import type { AgentMessage } from "@earendil-works/pi-agent-core";
import type { ImageContent } from "@earendil-works/pi-ai";

import type { FlaskClient } from "./flask-client.js";
import { FlaskHttpError } from "./flask-client.js";
import type { SlideInfo } from "./tools.js";
import { isImageContent, isImageRefContent, type ImageRefContent, type PersistedAgentMessage } from "./session-store.js";

const PLACEHOLDER_TEXT = "（历史快照已省略，可用 goto+snapshot 重新查看）";
const DEGRADE_TEXT = "该图因切片变更不可用。";
const REGION_OUT_W = 1568;
const REGION_OUT_H = 1568;
const REGION_FETCH_CONCURRENCY = 3;
const REGION_LRU_MAX = 32;
/** Soft TTL so a replaced slide cannot forever serve stale pixels via LRU. */
const REGION_LRU_TTL_MS = 30_000;

// =========================================================================== //
// Public config
// =========================================================================== //

/** Tuning knobs for {@link makeTransformContext}. */
export interface TransformContextConfig {
	/** Max materialized image blocks retained per request (default 6). */
	keep_recent_images?: number;
}

/** Resolved settings. */
export interface TransformContextSettings {
	keepRecentImages: number;
}

export function resolveTransformSettings(cfg: TransformContextConfig): TransformContextSettings {
	const raw = Number(cfg.keep_recent_images);
	const n = Number.isFinite(raw) && raw > 0 ? Math.floor(raw) : 6;
	return { keepRecentImages: n };
}

// =========================================================================== //
// Region LRU (module-level)
// =========================================================================== //

type RegionLruEntry = {
	data: string;
	mime: string;
	cachedAt: number;
	slide: string;
	fingerprint: string;
};

/** Successful region fetches keyed by `slide|fingerprint|bbox|outputSize`. */
const regionLru = new Map<string, RegionLruEntry>();

function normalizeSlideKey(slide: string): string {
	return String(slide || "").trim();
}

function regionLruKey(
	slide: string,
	fingerprint: string,
	x: number,
	y: number,
	w: number,
	h: number,
	outW: number,
	outH: number,
): string {
	return `${normalizeSlideKey(slide)}|${fingerprint}|${x},${y},${w},${h}|${outW}x${outH}`;
}

function regionLruGet(key: string, expectedFp: string): { data: string; mime: string } | undefined {
	const hit = regionLru.get(key);
	if (!hit) return undefined;
	if (hit.fingerprint !== expectedFp || Date.now() - hit.cachedAt > REGION_LRU_TTL_MS) {
		regionLru.delete(key);
		return undefined;
	}
	// Refresh recency (Map iteration order = insertion order).
	regionLru.delete(key);
	regionLru.set(key, hit);
	return { data: hit.data, mime: hit.mime };
}

function regionLruSet(
	key: string,
	entry: { data: string; mime: string; slide: string; fingerprint: string },
): void {
	if (regionLru.has(key)) regionLru.delete(key);
	regionLru.set(key, { ...entry, cachedAt: Date.now() });
	while (regionLru.size > REGION_LRU_MAX) {
		const oldest = regionLru.keys().next().value;
		if (oldest === undefined) break;
		regionLru.delete(oldest);
	}
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
// Concurrency pool
// =========================================================================== //

/** Run async tasks with a fixed concurrency limit; preserve result order. */
async function mapPool<T, R>(items: T[], concurrency: number, fn: (item: T, index: number) => Promise<R>): Promise<R[]> {
	const results: R[] = new Array(items.length);
	let next = 0;
	async function worker(): Promise<void> {
		for (;;) {
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
 */
export function makeTransformContext(args: {
	flask: FlaskClient;
	slide: string;
	slideInfo: SlideInfo;
	settings: TransformContextSettings;
	firstSnapshotToolCallIdRef: { value: string | null };
}): (messages: AgentMessage[], _signal?: AbortSignal) => Promise<AgentMessage[]> {
	const { flask, slide, slideInfo, settings, firstSnapshotToolCallIdRef } = args;

	return async (messages): Promise<AgentMessage[]> => {
		try {
			return await transformOnce(messages, flask, slide, slideInfo, settings, firstSnapshotToolCallIdRef);
		} catch {
			// Never leave image_ref in the output; never throw.
			return stripImageRefsToDegrade(messages);
		}
	};
}

type ImgPos = {
	msgIdx: number;
	blkIdx: number;
	overview: boolean;
	kind: "ref" | "image";
	ref?: ImageRefContent;
};

/**
 * Pre-evict then materialize. Pure: returns a new array, leaves inputs untouched.
 *
 *   1. Scan messages; collect image_ref + already-materialized image positions.
 *   2. Mark overview (identity / first coverage fallback) and choose KEEP set
 *      (overview + last N non-overview).
 *   3. Materialize only KEEP image_refs (concurrency 3). Evicted refs →
 *      placeholder text without flask.region.
 */
async function transformOnce(
	messages: AgentMessage[],
	flask: FlaskClient,
	slide: string,
	slideInfo: SlideInfo,
	settings: TransformContextSettings,
	firstSnapshotToolCallIdRef: { value: string | null },
): Promise<AgentMessage[]> {
	const firstId = firstSnapshotToolCallIdRef.value;
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
				positions.push({ msgIdx, blkIdx, overview, kind: "ref", ref: part });
			} else if (isImageContent(part)) {
				positions.push({
					msgIdx,
					blkIdx,
					overview: isOverviewLiveImage(m, firstId),
					kind: "image",
				});
			}
		}
	}

	// Phase 2: decide KEEP (overview always + last N non-overview).
	const keepKeys = new Set<string>();
	for (const p of positions) {
		if (p.overview) keepKeys.add(posKey(p));
	}
	const evictable = positions
		.filter((p) => !p.overview)
		.sort((a, b) => rank(a) - rank(b)); // oldest first
	const keepFrom = Math.max(0, evictable.length - settings.keepRecentImages);
	for (const p of evictable.slice(keepFrom)) {
		keepKeys.add(posKey(p));
	}

	// Phase 3: materialize KEEP refs only (pool of 3).
	const toMaterialize = positions.filter((p) => p.kind === "ref" && p.ref && keepKeys.has(posKey(p)));
	const materialized = new Map<string, ImageContent | { type: "text"; text: string }>();
	await mapPool(toMaterialize, REGION_FETCH_CONCURRENCY, async (p) => {
		const block = await materializeRef(p.ref!, flask, slide, slideInfo);
		materialized.set(posKey(p), block);
		return block;
	});

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
 * Materialize one KEEP image_ref. Fingerprint mismatch / empty / failure →
 * degrade text. Uses the module LRU; never throws.
 */
async function materializeRef(
	ref: ImageRefContent,
	flask: FlaskClient,
	slide: string,
	slideInfo: SlideInfo,
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
	const cacheKey = regionLruKey(slide, effectiveFp, x, y, w, h, REGION_OUT_W, REGION_OUT_H);
	const cached = regionLruGet(cacheKey, effectiveFp);
	if (cached) {
		return { type: "image", data: cached.data, mimeType: cached.mime };
	}

	try {
		const r = await flask.region({
			slide,
			x,
			y,
			w,
			h,
			out_w: REGION_OUT_W,
			out_h: REGION_OUT_H,
			expected_fingerprint: fp || undefined,
		});
		const b64 = r.image_base64 || "";
		if (!b64) {
			return { type: "text", text: DEGRADE_TEXT };
		}
		const mime = r.mime || "image/jpeg";
		regionLruSet(cacheKey, { data: b64, mime, slide: normalizeSlideKey(slide), fingerprint: effectiveFp });
		return { type: "image", data: b64, mimeType: mime };
	} catch (e) {
		if (e instanceof FlaskHttpError && e.status === 409) {
			invalidateRegionLru(slide);
		}
		return { type: "text", text: DEGRADE_TEXT };
	}
}

// =========================================================================== //
// Test-visible helpers
// =========================================================================== //

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
