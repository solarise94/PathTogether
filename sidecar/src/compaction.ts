/**
 * AI reading assistant sidecar — pi compaction hook (Step 4).
 *
 * Wires pi's harness compaction primitives
 * ({@link shouldCompact}/{@link prepareCompaction}/{@link compact}) into the
 * agent-runner. Replaces ai_session.py:908 `maybe_compact` / 916 `force_compact`
 * / 954 `_inject_spot_index`, and the `_compact_now` history-summary mechanic.
 *
 * Design — Entry adapter (selection rationale):
 *
 * pi's compaction operates on a session-branch `Entry[]` tree
 * (harness/session/types.ts). Our sidecar stores a flat `AgentMessage[]` on
 * `agent.state.messages` (no session-manager branch), so we need a thin adapter
 * that:
 *   - presents each message as a `MessageEntry` (parent chain = linear), and
 *   - materializes the most recent previous compaction as a single
 *     `CompactionEntry` (with `summary` + empty `retainedTail`) at the front,
 *     so pi's `prepareCompaction` can pick up `previousSummary` for incremental
 *     updates. The retained tail is left empty because those messages already
 *     live in the flat `messages` list (re-injecting them would double-count).
 *
 * We chose the flat-linear adapter (Option A) over re-running pi's full
 * SessionManager because:
 *   - we have no session-manager / branch store; messages are the source of
 *     truth (Step 1 design);
 *   - pi's `prepareCompaction` only reads `Entry` shape + the previous
 *     `CompactionEntry`; a linear `MessageEntry[]` with one synthesized
 *     `CompactionEntry` satisfies that contract exactly;
 *   - the harness `compact()` returns a `CompactResult` (no firstKeptEntryId /
 *     session-manager coupling), so we rebuild the post-compaction message list
 *     directly from `summary + retainedTail`.
 *
 * Outcome of a successful compact:
 *   - the agent's `messages` become `[compactionSummary, ...retainedTail]`;
 *   - a `session_compacted` event is emitted with `tokens_before`/`tokens_after`;
 *   - a spot-index user message is appended (ai_session.py:954 `_inject_spot_index`)
 *     so the model has the current annotation snapshot;
 *   - the previous compaction's `summary` + `retainedTail` are persisted on the
 *     session's `compaction_entries` log so the next compaction can update the
 *     summary incrementally.
 *
 * Failure handling: a compaction LLM-summary failure never breaks the main
 * loop — we log to console and leave the messages untouched (the agent-runner
 * continues with the un-compacted context). Only the `context_length_exceeded`
 * fallback path (force compact → retry the model call once) treats a second
 * failure as fatal.
 */
import type { AgentMessage } from "@earendil-works/pi-agent-core";
import {
	compact as piCompact,
	DEFAULT_COMPACTION_SETTINGS,
	estimateContextTokens,
	estimateTokens,
	type CompactResult,
	type CompactionPreparation,
	type CompactionSettings,
	prepareCompaction,
	type Entry,
	shouldCompact,
	type MessageEntry,
	type CompactionEntry as PiCompactionEntry,
} from "@earendil-works/pi-agent-core";
import type { Api, Model, Models } from "@earendil-works/pi-ai";
import { createCompactionSummaryMessage } from "@earendil-works/pi-agent-core";

import type { FlaskClient } from "./flask-client.js";
import {
	collectImageMeta,
	dehydrateMessages,
	isImageRefContent,
	replaceMessagesPreservingSeq,
	type PersistedAgentMessage,
	type SessionData,
	type SessionStore,
} from "./session-store.js";
import {
	DEFAULT_DERIVATIVE_SPEC,
	REQUEST_SCHEMA_VERSION,
	buildStablePrefixObject,
	stablePrefixHash,
	type ContextCheckpoint,
} from "./checkpoint.js";

// =========================================================================== //
// Public config
// =========================================================================== //

/** Tuning knobs for compaction. */
export interface CompactionConfig {
	/** Tokens reserved for summary prompt + output (default 16384). */
	reserve_tokens?: number;
	/** Approximate recent-context tokens kept after compaction (default 20000). */
	keep_recent_tokens?: number;
	/** Context window (inherited from engine config; default 272000). */
	context_window_tokens?: number;
}

/** Resolved compaction settings (pi CompactionSettings + context window). */
export interface ResolvedCompactionSettings {
	settings: CompactionSettings;
	contextWindow: number;
}

export function resolveCompactionSettings(cfg: CompactionConfig): ResolvedCompactionSettings {
	const reserve = numOr(cfg.reserve_tokens, DEFAULT_COMPACTION_SETTINGS.reserveTokens);
	const keepRecent = numOr(cfg.keep_recent_tokens, DEFAULT_COMPACTION_SETTINGS.keepRecentTokens);
	return {
		settings: { enabled: true, reserveTokens: reserve, keepRecentTokens: keepRecent },
		contextWindow: numOr(cfg.context_window_tokens, 272000),
	};
}

function numOr(v: unknown, d: number): number {
	const n = Number(v);
	return Number.isFinite(n) && n > 0 ? Math.floor(n) : d;
}

// =========================================================================== //
// Entry adapter: AgentMessage[] (+ prev compaction) → pi Entry[]
// =========================================================================== //

let entryIdCounter = 0;
function nextEntryId(): string {
	entryIdCounter += 1;
	return `svs-compaction-entry-${entryIdCounter}`;
}

/**
 * Build a flat-linear pi `Entry[]` from our message list, optionally prefixed
 * by a synthesized previous-compaction entry.
 *
 * The previous-compaction entry, when supplied, carries the last summary (and
 * `tokensBefore`) so pi's `prepareCompaction` can pass `summary` as
 * `previousSummary` for an incremental update. Its `retainedTail` is always
 * empty: after a compaction, `messages` is already
 * `[compactionSummary, ...retainedTail, ...new]`, so re-injecting the prior
 * retained tail onto the CompactionEntry would double-count those messages
 * when prepareCompaction virtually unrolls them.
 *
 * @param prevSummary   summary text from the prior compaction (undefined if none)
 * @param prevTokensBefore tokensBefore recorded for the prior compaction
 */
export function toEntries(
	messages: AgentMessage[],
	prevSummary?: string,
	prevTokensBefore?: number,
): Entry[] {
	const entries: Entry[] = [];
	let parentId: string | null = null;

	if (prevSummary !== undefined) {
		const ce: PiCompactionEntry = {
			type: "compaction",
			id: nextEntryId(),
			parentId: null,
			seq: 0,
			timestamp: Date.now(),
			summary: prevSummary,
			// Empty on purpose: retained-tail messages are already in `messages`.
			retainedTail: [],
			tokensBefore: prevTokensBefore ?? 0,
		};
		entries.push(ce);
		parentId = ce.id;
	}

	for (const message of messages) {
		// Skip any compactionSummary messages already in the stream: the
		// synthesized CompactionEntry above is the canonical representation of
		// the last compaction. Carrying both would double-count.
		if ((message as { role?: string }).role === "compactionSummary") continue;
		const me: MessageEntry = {
			type: "message",
			id: nextEntryId(),
			parentId,
			seq: 0,
			timestamp: (message as { timestamp?: number }).timestamp ?? Date.now(),
			message,
		};
		entries.push(me);
		parentId = me.id;
	}
	return entries;
}

// =========================================================================== //
// Previous-compaction state on the session
// =========================================================================== //

/**
 * Read the most recent compaction's summary + retained tail from the session
 * log, if any. The session store records each compaction in `compaction_entries`
 * with `summary` + `retained_tail` (dehydrated); we rehydrate the tail here.
 */
function readPrevCompaction(data: SessionData): { summary?: string; retainedTail: AgentMessage[]; tokensBefore?: number } {
	const log = data.compaction_entries || [];
	if (log.length === 0) return { retainedTail: [] };
	const last = log[log.length - 1]!;
	const stored = last as PersistedCompactionEntry;
	const summary = stored.summary;
	const tail = stored.retained_tail ?? [];
	// Tail is persisted dehydrated (image blocks → image_ref); it's fine to pass
	// to pi compaction as-is because the summarizer only reads text (serializeConversation).
	const retainedTail = tail as unknown as AgentMessage[];
	return { summary, retainedTail, tokensBefore: last.tokens_before };
}

/** Internal compaction-log record (extends the public CompactionEntry). */
export interface PersistedCompactionEntry {
	seq: number;
	tokens_before: number;
	tokens_after: number;
	reason?: string;
	ts: number;
	/** Summary text (for incremental updates on the next compaction). */
	summary?: string;
	/** Retained-tail messages kept after this compaction (dehydrated form). */
	retained_tail?: PersistedAgentMessage[];
}

// =========================================================================== //
// shouldCompact: usage+trailing estimate (fixes the old Python one-turn lag)
// =========================================================================== //

/**
 * Visual token estimator (§9.1).
 *
 * Phase 2b has no provider image-token formula, so per §9.1 rule 3 we cannot
 * reliably predict tokens for an arbitrary image set from aggregate usage. We
 * approximate with a coarse pixel-based heuristic:
 *
 *   est_image_tokens = (long_edge * short_edge) / PIXELS_PER_TOKEN
 *
 * where PIXELS_PER_TOKEN is a calibratable constant (commented below). The
 * estimator is deliberately conservative: it overestimates rather than
 * underestimates, so compaction triggers slightly early rather than late. The
 * visual_context_budget_tokens cap (§9.1) is enforced separately by the image
 * selector.
 *
 * When the caller cannot supply per-image dimensions (e.g. the request has not
 * been assembled yet), {@link estimateSelectedVisualTokens} returns the full
 * `visual_context_budget_tokens` as a conservative reserve (§9.1 rule 3).
 */

/**
 * Calibratable constant: pixels per visual token (§9.1). OpenAI's published
 * vision formula is roughly 768px-tile based; we use a conservative 750 px/token
 * so the estimate trends high. This is a CONSTANT — adjust via A/B (Phase 4),
 * not at runtime.
 */
export const PIXELS_PER_VISUAL_TOKEN = 750;

/**
 * Default per-request visual token hard budget (§9.1, suggested 8000). Used as
 * the conservative reserve when per-image dimensions are unavailable.
 */
export const DEFAULT_VISUAL_CONTEXT_BUDGET_TOKENS = 8000;

/**
 * Estimate the visual token cost of a single image_ref by its pixel dimensions
 * (§9.1). Returns 0 for degenerate (zero-area) refs.
 */
export function estimateImageRefTokens(src: { w: number; h: number }, targetLongEdge: number): number {
	const w = src.w;
	const h = src.h;
	if (w <= 0 || h <= 0 || targetLongEdge <= 0) return 0;
	const longest = Math.max(w, h);
	const scale = targetLongEdge / longest;
	const le = Math.min(longest, targetLongEdge);
	const se = Math.min(w, h) * scale;
	return Math.ceil((le * se) / PIXELS_PER_VISUAL_TOKEN);
}

/**
 * Estimate the visual token cost of the selected visual working set (§9.1).
 *
 * The caller passes the candidate selected image refs (after the Phase 1
 * selection step). When no refs are selected, returns 0. When the caller
 * cannot determine the selection, pass `estimateUnavailable: true` to reserve
 * the full `visual_context_budget_tokens` (§9.1 rule 3).
 */
export function estimateSelectedVisualTokens(args: {
	selectedRefs: Array<{ src: { w: number; h: number }; target_long_edge: number }>;
	overviewPresent: boolean;
	overviewLongEdge: number;
	overviewPixels?: { w: number; h: number };
	estimateUnavailable?: boolean;
	visualContextBudgetTokens: number;
}): number {
	if (args.estimateUnavailable) {
		return args.visualContextBudgetTokens;
	}
	let total = 0;
	for (const r of args.selectedRefs) {
		total += estimateImageRefTokens(r.src, r.target_long_edge);
	}
	if (args.overviewPresent && args.overviewPixels) {
		total += estimateImageRefTokens(args.overviewPixels, args.overviewLongEdge);
	}
	return Math.min(total, args.visualContextBudgetTokens);
}

/**
 * Decide whether the current messages exceed the compaction threshold.
 *
 * Uses pi's `estimateContextTokens` (usage + trailing-tail estimate). This
 * fixes the old Python estimator's one-turn lag: Python keyed off
 * `last_usage.prompt_tokens` which reflects the *previous* request's input, so
 * it could not see tokens added by the just-completed turn until the next one
 * ran. pi's estimator adds an `estimateTokens(message)` walk over the messages
 * after the last usage block, so the threshold check is current.
 *
 * Phase 2b (§9.1): `tokens` now includes an estimate of the selected visual
 * working set so the trigger sees image cost. Pass `visualTokens` to add a
 * precomputed estimate; pass `visualContextBudgetReserve` to reserve the full
 * budget when per-image estimation is unavailable (§9.1 rule 3).
 */
export function checkShouldCompact(
	messages: AgentMessage[],
	settings: ResolvedCompactionSettings,
	extra?: { visualTokens?: number; visualContextBudgetReserve?: number },
): { should: boolean; tokens: number } {
	const est = estimateContextTokens(messages);
	const visual = extra?.visualTokens ?? extra?.visualContextBudgetReserve ?? 0;
	const combined = est.tokens + visual;
	const should = shouldCompact(combined, settings.contextWindow, settings.settings);
	return { should, tokens: combined };
}

// =========================================================================== //
// runCompaction: prepare + compact + rebuild messages
// =========================================================================== //

/** Result of a successful compaction. */
export interface CompactionOutcome {
	/** New message list: compactionSummary + retainedTail. */
	messages: AgentMessage[];
	/** Estimated tokens before compaction. */
	tokensBefore: number;
	/** Estimated tokens after compaction (re-estimated on the new list). */
	tokensAfter: number;
	/** Generated summary text. */
	summary: string;
	/** Retained-tail messages (for the next incremental compaction). */
	retainedTail: AgentMessage[];
}

/**
 * Run one compaction pass over the given messages.
 *
 * @returns the outcome, or `null` when compaction was not applicable (no
 *   messages, or the last entry is already a compaction) or failed.
 */
export async function runCompaction(args: {
	messages: AgentMessage[];
	settings: ResolvedCompactionSettings;
	models: Models;
	model: Model<Api>;
	prevSummary?: string;
	prevTokensBefore?: number;
	signal?: AbortSignal;
}): Promise<CompactionOutcome | null> {
	const { messages, settings, models, model } = args;

	const entries = toEntries(messages, args.prevSummary, args.prevTokensBefore);
	const preparationResult = prepareCompaction(entries, settings.settings);
	const preparation: CompactionPreparation | undefined = preparationResult.ok ? preparationResult.value : undefined;
	if (!preparation) return null;

	let result: CompactResult;
	try {
		const r = await piCompact(preparation, models, model, undefined, args.signal);
		if (!r.ok) return null;
		result = r.value;
	} catch {
		return null;
	}

	// Build the post-compaction message list: a compactionSummary message
	// carrying the summary, followed by the retained tail. This guarantees the
	// next LLM request sees the summary (fixes "amnesia after compaction").
	const summaryMsg = createCompactionSummaryMessage(result.summary, result.tokensBefore, Date.now()) as unknown as AgentMessage;
	const newMessages: AgentMessage[] = [summaryMsg, ...result.retainedTail];

	// §9.3: tokens_after must NOT reuse the retained assistant message's stale
	// usage. Re-estimate by walking the NEW message list with estimateTokens
	// (char heuristic) and add the §9.1 visual estimate for the image_refs that
	// survive in the retained tail (treated as the selected working set at the
	// working tier). Marked as estimate in the event by the caller.
	const textAfter = newMessages.reduce((acc, m) => acc + estimateTokens(m), 0);
	const visualAfter = estimateRetainedVisualTokens(newMessages);
	const tokensAfter = textAfter + visualAfter;

	return {
		messages: newMessages,
		tokensBefore: result.tokensBefore,
		tokensAfter,
		summary: result.summary,
		retainedTail: result.retainedTail,
	};
}

/**
 * Estimate the visual token cost of the image_refs surviving in the retained
 * tail (§9.3: "按 §9.1 估算的已选视觉工作集"). Uses the working-image long
 * edge for non-overview refs; this is an upper bound since the assembler may
 * later evict some. Marked as estimate.
 */
function estimateRetainedVisualTokens(messages: AgentMessage[]): number {
	let total = 0;
	for (const m of messages) {
		const content = (m as { content?: unknown }).content;
		if (!Array.isArray(content)) continue;
		for (const part of content) {
			if (part && isImageRefContent(part)) {
				total += estimateImageRefTokens(part.src, DEFAULT_DERIVATIVE_SPEC.target_long_edge);
			}
		}
	}
	return total;
}

// =========================================================================== //
// Spot-index injection (ai_session.py:954 _inject_spot_index)
// =========================================================================== //

/**
 * Build the "current annotation snapshot" user message injected after every
 * compaction (ai_session.py:964-972). Text-only, full visible-spot list,
 * updates spot_cursor. Byte-for-byte format alignment with the Python original.
 *
 * Returns null when there are no visible spots.
 */
export async function buildSpotIndexMessage(
	flask: FlaskClient,
	slide: string,
): Promise<{ message: AgentMessage; newCursor: number } | null> {
	let result;
	try {
		result = await flask.spots(slide, 0);
	} catch {
		return null;
	}
	const visible = (result.changes || []).filter((r) => !r.deleted);
	if (visible.length === 0) return null;

	const lines: string[] = ["当前切片标注库快照（待复核线索，非诊断事实）："];
	for (const r of visible) {
		const s = Math.trunc(Number(r.side_px) || 0);
		const x0 = Number(r.x) || 0;
		const y0 = Number(r.y) || 0;
		lines.push(
			`- 位置 level-0 左上角 (${fmt0(x0)},${fmt0(y0)})，边长 ${s}px` +
				`（中心 (${fmt0(x0 + s / 2.0)},${fmt0(y0 + s / 2.0)})，goto 请对准中心）：${String(r.note || "")}`,
		);
	}
	const message: AgentMessage = {
		role: "user",
		content: [{ type: "text", text: lines.join("\n") }],
		timestamp: Date.now(),
	} as AgentMessage;
	return { message, newCursor: result.current_seq || 0 };
}

function fmt0(v: number): string {
	return String(Math.round(v));
}

// =========================================================================== //
// Persist helpers
// =========================================================================== //

/**
 * Record a compaction on the session log + apply the new messages. Caller
 * passes the rebuilt message list (already including the compactionSummary and
 * any spot-index injection).
 */
export async function persistCompaction(
	store: SessionStore,
	sessionId: string,
	outcome: CompactionOutcome,
	newMessages: AgentMessage[],
	reason?: string,
): Promise<void> {
	await store.withLock(sessionId, async (d) => {
		if (!d) return null;
		// Dehydrate with real ImageMeta from toolResult.details (or existing
		// image_ref), so retained_tail keeps recoverable src/fingerprint.
		const imageMeta = collectImageMeta([
			...(outcome.retainedTail as PersistedAgentMessage[]),
			...(newMessages as PersistedAgentMessage[]),
		]);
		const dehydratedTail = dehydrateMessages(outcome.retainedTail, imageMeta);
		const entry: PersistedCompactionEntry = {
			seq: (d.last_event_seq || 0) + 1,
			tokens_before: outcome.tokensBefore,
			tokens_after: outcome.tokensAfter,
			reason,
			ts: Math.floor(Date.now() / 1000),
			summary: outcome.summary,
			// Persist the retained tail in dehydrated form. The summary message is
			// already at the head of newMessages, so we only need the tail.
			retained_tail: dehydratedTail,
		};
		d.compaction_entries = [...(d.compaction_entries || []), entry as unknown as (typeof d.compaction_entries)[number]];
		// Replace messages preserving seqs (§10): dehydrated retained-tail
		// messages that already carry _context_meta keep their seq (retained tail
		// is NOT renumbered); the new compactionSummary message and any spot-index
		// message get fresh monotonic seqs. dehydrateMessages preserves existing
		// _context_meta via object spread.
		const dehydrated = dehydrateMessages(newMessages, imageMeta);
		replaceMessagesPreservingSeq(d, dehydrated);
		d.updated_at = Math.floor(Date.now() / 1000);
		await store.writeSession(sessionId, d);
		return d;
	});
}

/**
 * Read previous-compaction inputs for the session (summary + retained tail),
 * for the next incremental compaction.
 */
export function prevCompactionInputs(data: SessionData): { summary?: string; retainedTail: AgentMessage[]; tokensBefore?: number } {
	return readPrevCompaction(data);
}

// =========================================================================== //
// Checkpoint rebuild after compaction (Phase 2b, §5.3/§9.3)
// =========================================================================== //

/**
 * Build a candidate post-compaction {@link ContextCheckpoint} (§5.3: "force-
 * compaction / 阈值 compaction 成功后重建 checkpoint"). The candidate is NOT
 * committed; the caller commits via {@link SessionStore.commitCheckpoint} with
 * the expected generation + fingerprint.
 *
 * The candidate:
 *   - generation = prev.generation + 1 (atomic bump);
 *   - summary = compaction outcome summary (replaces the stable-region text);
 *   - annotation_index rebuilt from observations (the snapshot did not change);
 *   - through_message_seq = the highest seq on the post-compaction message list;
 *   - overview_derivative carried over from prev (the slide did not change);
 *   - stable_prefix_hash recomputed over the new stable region (§5.3).
 *
 * Returns null when `prev` is null (no checkpoint to rebuild — the caller
 * should ensureCheckpoint instead). The candidate's overview_derivative is
 * verbatim from prev so the overview bytes stay byte-stable across the
 * generation bump (§8.3: only the summary changes).
 */
export function buildPostCompactionCheckpoint(args: {
	prev: ContextCheckpoint | null;
	outcome: CompactionOutcome;
	postMessages: PersistedAgentMessage[];
	observations: { bbox?: { x?: number; y?: number; w?: number; h?: number }; note?: string; [k: string]: unknown }[];
	systemPrompt: string;
}): ContextCheckpoint | null {
	const { prev, outcome, postMessages, observations, systemPrompt } = args;
	if (!prev) return null;

	// through_message_seq: the highest seq on the post-compaction list.
	let throughSeq = 0;
	for (const m of postMessages) {
		const seq = (m as PersistedAgentMessage & { _context_meta?: { session_message_seq?: number } })._context_meta?.session_message_seq;
		if (typeof seq === "number" && seq > throughSeq) throughSeq = seq;
	}

	// annotation_index rebuilt from observations.
	const annotationIndex = buildAnnotationIndexFromObs(observations);

	const stablePrefixObj = buildStablePrefixObject({
		systemPrompt,
		system_prompt_version: prev.system_prompt_version,
		tool_schema_hash: prev.tool_schema_hash,
		request_schema_version: REQUEST_SCHEMA_VERSION,
		slide_fingerprint: prev.slide_fingerprint,
		summary: outcome.summary,
		annotation_index: annotationIndex,
		overview_derivative: prev.overview_derivative,
	});
	const spHash = stablePrefixHash(stablePrefixObj);

	return {
		version: 1,
		generation: prev.generation + 1,
		created_at: Date.now(),
		slide_fingerprint: prev.slide_fingerprint,
		through_message_seq: throughSeq,
		summary: outcome.summary,
		annotation_index: annotationIndex,
		overview_derivative: prev.overview_derivative,
		system_prompt_version: prev.system_prompt_version,
		tool_schema_hash: prev.tool_schema_hash,
		request_schema_version: REQUEST_SCHEMA_VERSION,
		stable_prefix_hash: spHash,
	};
}

/**
 * Build a text annotation index from observations (mirrors checkpoint.ts
 * buildAnnotationIndex, kept local to avoid a runtime cycle).
 */
function buildAnnotationIndexFromObs(observations: { bbox?: { x?: number; y?: number; w?: number; h?: number }; note?: string; [k: string]: unknown }[]): string {
	if (!observations || observations.length === 0) return "";
	const lines: string[] = [];
	for (let i = 0; i < observations.length; i++) {
		const o = observations[i];
		if (!o) continue;
		const b = o.bbox;
		const coord = b ? `(${fmtCoord(b.x)},${fmtCoord(b.y)},${fmtCoord(b.w)}×${fmtCoord(b.h)})` : "(无坐标)";
		const note = o.note ? `：${o.note}` : "";
		lines.push(`- 观察#${i + 1} ${coord}${note}`);
	}
	return lines.join("\n");
}

function fmtCoord(v: unknown): string {
	const n = Number(v);
	return Number.isFinite(n) ? String(Math.round(n)) : "?";
}
