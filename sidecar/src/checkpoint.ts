/**
 * AI reading assistant sidecar — context checkpoint (Phase 2a).
 *
 * Implements the data structures and persistence primitives for the stable
 * context checkpoint (§3.2/§5.3/§10) without yet wiring the request
 * assembler (that is Phase 2b). This module provides:
 *
 *   - {@link ContextCheckpoint} / {@link VisualWorkingSetEntry} types, added
 *     to {@link SessionData} as optional fields.
 *   - Version constants + derivation helpers ({@link SYSTEM_PROMPT_VERSION},
 *     {@link REQUEST_SCHEMA_VERSION}, {@link computeToolSchemaHash},
 *     {@link computeSystemPromptVersion}) so a checkpoint can be invalidated
 *     when the prompt, tool schema, or request encoding changes (§10).
 *   - Canonical serialization ({@link canonicalSerialize}) and
 *     {@link stablePrefixHash}, shared with the Phase 2b
 *     {@link canonicalPayloadHash} for {@link PreparedRequest}.
 *   - {@link SessionStore.commitCheckpoint}: an atomic compare-and-swap commit
 *     (§5.3) that validates the expected generation + slide fingerprint inside
 *     the per-session lock, runs a mutation, and writes via the existing
 *     tmp+rename mechanism.
 *   - {@link ensureCheckpoint}: lazy generation-1 materialization for legacy
 *     sessions that predate checkpoints (§10).
 *   - {@link checkpointStale}: version-field invalidation check (§10).
 *
 * Design notes:
 *   - Checkpoint candidate computation (summary, overview derivative) may run
 *     OUTSIDE the session lock; only the commit enters the lock and re-reads
 *     the authoritative session snapshot (§5.3).
 *   - The compare-and-swap rejects when another operation bumped the generation
 *     or the slide fingerprint changed; the old checkpoint is left intact.
 *   - This phase does NOT touch the request assembler; transformContext still
 *     uses the Phase 1 transformOnce path.
 */
import { createHash } from "node:crypto";
import type { AgentTool } from "@earendil-works/pi-agent-core";

// Type-only imports to avoid a runtime cycle: session-store.ts imports
// ContextCheckpoint/VisualWorkingSetEntry from here via `import type`, and we
// import SessionData/PersistedAgentMessage from there via `import type`. Both
// directions are erased at compile time.
import type {
	PersistedAgentMessage,
	PersistedMessageMeta,
	SessionData,
} from "./session-store.js";

// =========================================================================== //
// Types (§10)
// =========================================================================== //

/**
 * Stable context checkpoint (§3.2/§10). One checkpoint survives across multiple
 * model requests within a single generation; the generation bumps atomically
 * when the stable region must change.
 *
 * `overview_derivative` may be null in a degraded generation (§3.2: a
 * permanently-unavailable overview forces a new generation with no overview).
 * Phase 2a's {@link ensureCheckpoint} produces generation 1 with
 * `overview_derivative = null` (the ref_id + content_sha256 are materialized
 * and back-filled in Phase 2b); this is an explicit degraded path, noted so
 * Phase 2b can detect and upgrade it once the derivative is built.
 */
export interface ContextCheckpoint {
	version: 1;
	/** Monotonically increasing; bumps on every stable-region change. */
	generation: number;
	/** Unix milliseconds when this generation was committed. */
	created_at: number;
	/** Slide fingerprint the checkpoint was built against. */
	slide_fingerprint: string;
	/**
	 * Largest session_message_seq covered by the checkpoint (§10). NOT the
	 * array index and NOT the SSE event seq.
	 */
	through_message_seq: number;
	/** Text summary of the stable region (goals, confirmed observations). */
	summary: string;
	/** Structured annotation/observation index text for the stable region. */
	annotation_index: string;
	/**
	 * Stable whole-slide overview derivative spec + content hash. null in a
	 * degraded generation (no overview image). Phase 2a may commit generation 1
	 * with null; Phase 2b back-fills after materializing the derivative.
	 */
	overview_derivative: {
		ref_id: string;
		target_long_edge: number;
		jpeg_quality: number;
		overlay_version: string;
		resize_algorithm: string;
		encoder_id: string;
		encoder_version: string;
		mime_type: string;
		content_sha256: string;
	} | null;
	/** Hash of the system prompt the checkpoint was built against (§10). */
	system_prompt_version: string;
	/** Hash of the tool schema the checkpoint was built against (§10). */
	tool_schema_hash: string;
	/** Request encoding/schema revision (bumped on assembler wire-format change). */
	request_schema_version: number;
	/** sha256 of the canonical-serialized stable prefix (§10). */
	stable_prefix_hash: string;
}

/**
 * Visual working set entry (§3.3/§10). Describes one image retained in the
 * temporary working region (after the cache breakpoint). Phase 2a declares the
 * shape; Phase 2b's selector populates it.
 */
export interface VisualWorkingSetEntry {
	ref_id: string;
	tool_call_id: string | null;
	reason: "overview" | "pending" | "recent" | "detail";
	target_long_edge: number;
	last_used_at: number;
}

// =========================================================================== //
// Version constants + derivation (§10)
// =========================================================================== //

/**
 * Request schema version (§10). Bump when the request assembler's wire format
 * (message block structure, tool encoding, breakpoint placement) changes in a
 * way that invalidates cached prefixes. Phase 2a starts at 1.
 */
export const REQUEST_SCHEMA_VERSION = 1;

/**
 * Compute the system prompt version hash (§10). The system prompt is
 * user-facing Chinese copy that is load-bearing for model behavior; any edit
 * changes this hash and invalidates prior checkpoints.
 *
 * Returns a hex sha256 of the UTF-8 prompt string.
 */
export function computeSystemPromptVersion(systemPrompt: string): string {
	return sha256Hex(systemPrompt);
}

/**
 * Compute a stable hash of the tool schema (§10). The tool schema is the set of
 * tool names + their parameter schemas, serialized canonically so that
 * reordering tools or keys does not change the hash. A tool addition, removal,
 * rename, or parameter-type change invalidates prior checkpoints.
 *
 * Accepts the pi {@link AgentTool}[] (the same array {@link createTools}
 * returns). Extracts each tool's name, label, description, and parameters
 * schema object, sorts by name, and canonical-serializes the array.
 */
export function computeToolSchemaHash(tools: AgentTool[]): string {
	const stripped = tools
		.map((t) => ({
			name: t.name,
			label: t.label,
			description: t.description,
			parameters: t.parameters ?? null,
		}))
		.sort((a, b) => (a.name < b.name ? -1 : a.name > b.name ? 1 : 0));
	return sha256Hex(canonicalSerialize(stripped));
}

/**
 * Constants describing the default derivative encoding spec (§6.3). These are
 * recorded inside {@link ContextCheckpoint.overview_derivative} so a checkpoint
 * can be rebuilt deterministically and its content hash re-verified. They must
 * match the encoder actually used by the region/derivative pipeline
 * (transform-context.ts).
 */
export const DEFAULT_DERIVATIVE_SPEC = {
	target_long_edge: 1024,
	jpeg_quality: 85,
	overlay_version: "v1",
	resize_algorithm: "LANCZOS",
	encoder_id: "pillow",
	encoder_version: "unknown-phase2a",
	mime_type: "image/jpeg",
} as const;

// =========================================================================== //
// Canonical serialization (§10)
// =========================================================================== //

/**
 * Canonical serialization for stable-prefix hashing and (Phase 2b)
 * {@link PreparedRequest} payload hashing (§10).
 *
 * Rules (§10):
 *   - UTF-8 encoded.
 *   - Object keys sorted recursively in dictionary order.
 *   - Array element order preserved (NOT sorted).
 *   - `undefined` values are omitted (key dropped), matching JSON.stringify.
 *   - Non-finite numbers (NaN, Infinity, -Infinity) are forbidden and throw —
 *     they would serialize inconsistently across implementations and break the
 *     stability contract.
 *   - Returns a string (canonical JSON).
 *
 * This is a dedicated serializer (not JSON.stringify with a replacer) because
 * the key-ordering and NaN rules must be exact and implementation-independent.
 */
export function canonicalSerialize(value: unknown): string {
	const out = serializeValue(value);
	return out;
}

function serializeValue(value: unknown): string {
	if (value === null) return "null";
	if (value === undefined) return ""; // caller omits; handled by container walkers
	switch (typeof value) {
		case "string":
			return jsonString(value);
		case "boolean":
			return value ? "true" : "false";
		case "number": {
			if (!Number.isFinite(value)) {
				throw new Error(
					`canonicalSerialize: non-finite number (${value}) is forbidden (§10 stability contract)`,
				);
			}
			// Use String() for integer/float fidelity matching JSON.stringify; this
			// produces the canonical numeric form (-0 is serialized as "0" by
			// JSON.stringify, so we match that).
			return Object.is(value, -0) ? "0" : String(value);
		}
		case "object": {
			// toJSON takes priority over plain-object walking so Date (and other
			// built-ins with toJSON) serialize canonically rather than as {}.
			if (typeof (value as { toJSON?: unknown }).toJSON === "function") {
				return serializeValue((value as { toJSON: () => unknown }).toJSON());
			}
			if (Array.isArray(value)) {
				const parts: string[] = [];
				for (const el of value) {
					// undefined array slots become null (JSON.stringify parity).
					parts.push(el === undefined ? "null" : serializeValue(el));
				}
				return "[" + parts.join(",") + "]";
			}
			if (isPlainObject(value)) {
				const keys = Object.keys(value).sort();
				const parts: string[] = [];
				for (const k of keys) {
					const v = (value as Record<string, unknown>)[k];
					if (v === undefined) continue; // omit undefined (§10)
					parts.push(jsonString(k) + ":" + serializeValue(v));
				}
				return "{" + parts.join(",") + "}";
			}
			// Last resort: throw rather than emit non-deterministic output.
			throw new Error(`canonicalSerialize: unsupported value type (${Object.prototype.toString.call(value)})`);
		}
		default:
			// function, symbol, bigint: not supported.
			throw new Error(`canonicalSerialize: unsupported value type (${typeof value})`);
	}
}

function jsonString(s: string): string {
	// JSON.stringify a string (handles escaping). We re-derive rather than call
	// JSON.stringify on the whole object to control key ordering ourselves.
	return JSON.stringify(s);
}

function isPlainObject(v: unknown): v is Record<string, unknown> {
	return typeof v === "object" && v !== null && !Array.isArray(v);
}

/** sha256 of a UTF-8 string, returned as lowercase hex. */
export function sha256Hex(input: string): string {
	return createHash("sha256").update(input, "utf8").digest("hex");
}

/**
 * Compute the stable-prefix hash for a checkpoint (§10). The `stablePrefix`
 * is an opaque value representing exactly what the Provider will receive in
 * the stable region (system prompt + tools + summary + overview + annotation
 * index). The caller assembles it; we canonical-serialize + sha256 it.
 *
 * The same canonical object is used both to compute this hash and (Phase 2b)
 * to build the {@link PreparedRequest} — never hash one form and send another.
 */
export function stablePrefixHash(stablePrefix: unknown): string {
	return sha256Hex(canonicalSerialize(stablePrefix));
}

// =========================================================================== //
// Checkpoint staleness (§10)
// =========================================================================== //

/**
 * Environment against which a checkpoint's version fields are checked for
 * staleness (§10). Any mismatch invalidates the checkpoint and forces a new
 * generation. Provided by the caller (Phase 2b assembler); Phase 2a tests
 * construct it directly.
 */
export interface CheckpointEnv {
	system_prompt_version: string;
	tool_schema_hash: string;
	request_schema_version: number;
	slide_fingerprint: string;
	/** Expected overview derivative encoding spec (§6.3). */
	overview_target_long_edge: number;
	overview_jpeg_quality: number;
	overview_overlay_version: string;
	overview_resize_algorithm: string;
	overview_encoder_id: string;
}

/**
 * Check whether a checkpoint is stale against the current environment (§10).
 * Returns a human-readable reason string when ANY version field, the slide
 * fingerprint, or the overview encoding spec changed; returns null when the
 * checkpoint is still valid.
 *
 * Per §10: "system_prompt_version、tool_schema_hash、request_schema_version、
 * slide fingerprint 或概览编码规格任一变化，都必须使旧 checkpoint/cache key
 * 失效并创建新 generation".
 */
export function checkpointStale(cp: ContextCheckpoint, env: CheckpointEnv): string | null {
	if (cp.system_prompt_version !== env.system_prompt_version) {
		return `system_prompt_version changed (${cp.system_prompt_version} → ${env.system_prompt_version})`;
	}
	if (cp.tool_schema_hash !== env.tool_schema_hash) {
		return `tool_schema_hash changed (${cp.tool_schema_hash} → ${env.tool_schema_hash})`;
	}
	if (cp.request_schema_version !== env.request_schema_version) {
		return `request_schema_version changed (${cp.request_schema_version} → ${env.request_schema_version})`;
	}
	if (cp.slide_fingerprint !== env.slide_fingerprint) {
		return `slide_fingerprint changed (${cp.slide_fingerprint} → ${env.slide_fingerprint})`;
	}
	// Overview encoding spec: only check when the checkpoint HAS an overview
	// (a null-overview generation is not encoding-pinned; Phase 2b may upgrade).
	const od = cp.overview_derivative;
	if (od) {
		if (od.target_long_edge !== env.overview_target_long_edge) {
			return `overview target_long_edge changed (${od.target_long_edge} → ${env.overview_target_long_edge})`;
		}
		if (od.jpeg_quality !== env.overview_jpeg_quality) {
			return `overview jpeg_quality changed (${od.jpeg_quality} → ${env.overview_jpeg_quality})`;
		}
		if (od.overlay_version !== env.overview_overlay_version) {
			return `overview overlay_version changed (${od.overlay_version} → ${env.overview_overlay_version})`;
		}
		if (od.resize_algorithm !== env.overview_resize_algorithm) {
			return `overview resize_algorithm changed (${od.resize_algorithm} → ${env.overview_resize_algorithm})`;
		}
		if (od.encoder_id !== env.overview_encoder_id) {
			return `overview encoder_id changed (${od.encoder_id} → ${env.overview_encoder_id})`;
		}
	}
	return null;
}

// =========================================================================== //
// Lazy generation-1 materialization (§10)
// =========================================================================== //

/**
 * Dependencies {@link ensureCheckpoint} needs to derive a generation-1
 * checkpoint from existing session state. Kept minimal for Phase 2a: the
 * overview ref selection + summary come from compaction history and the
 * canonical message array. Phase 2b will pass richer deps (slide info,
 * observations index builder, derivative materializer).
 */
export interface EnsureCheckpointDeps {
	system_prompt_version: string;
	tool_schema_hash: string;
	slide_fingerprint: string;
}

/**
 * Build (or return) a generation-1 {@link ContextCheckpoint} for a session that
 * lacks one (§10 "读取旧会话：缺少 checkpoint：从现有 compaction summary、
 * observations、spots 和第一张有效 overview ref 惰性生成 generation 1").
 *
 * Phase 2a behavior:
 *   - If the session already has a checkpoint, return it unchanged.
 *   - Otherwise derive a generation-1 checkpoint with:
 *       - summary from the last compaction entry's summary (or empty),
 *       - annotation_index from observations (or empty),
 *       - through_message_seq = max assigned seq (or 0),
 *       - overview_derivative = null (degraded; Phase 2b back-fills ref_id +
 *         content_sha256 after materializing the derivative). This is an
 *         explicit degraded path per §3.2.
 *       - stable_prefix_hash computed over the current stable-region content.
 *
 * This does NOT write to disk; the caller persists via
 * {@link SessionStore.commitCheckpoint} (or writes the session directly). The
 * returned checkpoint is a candidate; committing it atomically is the caller's
 * job. Mutates `data.context_checkpoint` only when a new checkpoint is built.
 */
export function ensureCheckpoint(data: SessionData, deps: EnsureCheckpointDeps): ContextCheckpoint {
	if (data.context_checkpoint) {
		return data.context_checkpoint;
	}

	// through_message_seq: the highest assigned session_message_seq, or 0.
	const msgs = data.messages || [];
	let throughSeq = 0;
	for (const m of msgs) {
		const seq = (m as PersistedAgentMessage & { _context_meta?: PersistedMessageMeta })?._context_meta?.session_message_seq;
		if (typeof seq === "number" && seq > throughSeq) throughSeq = seq;
	}

	// summary: prefer the last compaction entry's summary; fall back to empty.
	const entries = data.compaction_entries || [];
	const lastEntry = entries.length ? entries[entries.length - 1] : null;
	const summary = (lastEntry as { summary?: string } | null)?.summary ?? "";

	// annotation_index: derive from observations (bbox + note). Empty when none.
	const observations = data.observations || [];
	const annotationIndex = buildAnnotationIndex(observations);

	// overview_derivative: null in Phase 2a (degraded generation). Phase 2b
	// materializes the derivative and back-fills ref_id + content_sha256.
	const overviewDerivative = null;

	// stable_prefix_hash: canonical-serialize the stable region we CAN describe
	// (summary + annotation_index + version fields). Phase 2b expands this to
	// include the actual system prompt + tools + overview bytes.
	const stablePrefix = {
		system_prompt_version: deps.system_prompt_version,
		tool_schema_hash: deps.tool_schema_hash,
		request_schema_version: REQUEST_SCHEMA_VERSION,
		slide_fingerprint: deps.slide_fingerprint,
		summary,
		annotation_index: annotationIndex,
		overview_derivative: overviewDerivative,
	};
	const spHash = stablePrefixHash(stablePrefix);

	const cp: ContextCheckpoint = {
		version: 1,
		generation: 1,
		created_at: Date.now(),
		slide_fingerprint: deps.slide_fingerprint,
		through_message_seq: throughSeq,
		summary,
		annotation_index: annotationIndex,
		overview_derivative: overviewDerivative,
		system_prompt_version: deps.system_prompt_version,
		tool_schema_hash: deps.tool_schema_hash,
		request_schema_version: REQUEST_SCHEMA_VERSION,
		stable_prefix_hash: spHash,
	};
	data.context_checkpoint = cp;
	return cp;
}

/**
 * Build a text annotation/observation index from persisted observations
 * (§3.2 "checkpoint 时刻的标注/观察索引"). Phase 2a emits a compact text form;
 * Phase 2b may switch to a richer structured form (the field is free-text).
 */
function buildAnnotationIndex(observations: { bbox?: { x?: number; y?: number; w?: number; h?: number }; note?: string; [k: string]: unknown }[]): string {
	if (observations.length === 0) return "";
	const lines: string[] = [];
	for (let i = 0; i < observations.length; i++) {
		const o = observations[i];
		if (!o) continue;
		const b = o.bbox;
		const coord = b ? `(${fmt(b.x)},${fmt(b.y)},${fmt(b.w)}×${fmt(b.h)})` : "(无坐标)";
		const note = o.note ? `：${o.note}` : "";
		lines.push(`- 观察#${i + 1} ${coord}${note}`);
	}
	return lines.join("\n");
}

function fmt(v: unknown): string {
	const n = Number(v);
	return Number.isFinite(n) ? String(Math.round(n)) : "?";
}

// =========================================================================== //
// Atomic compare-and-swap commit (§5.3)
// =========================================================================== //

/**
 * Result of {@link SessionStore.commitCheckpoint}.
 *   - `ok: true` → the mutation was applied and written atomically; `data` is
 *     the post-commit session snapshot.
 *   - `ok: false` → the CAS validation failed (generation/fingerprint mismatch
 *     or write error); `reason` explains why. The on-disk checkpoint is
 *     untouched on any `ok:false` outcome.
 */
export type CommitCheckpointResult =
	| { ok: true; data: SessionData }
	| { ok: false; reason: string };

/**
 * Expected-generation predicate for {@link SessionStore.commitCheckpoint}.
 *
 * Per §5.3, the CAS validates `d.context_checkpoint?.generation === expected`.
 * When the session has NO checkpoint yet, `expectedGeneration` should be
 * `undefined` and we treat `undefined === undefined` as a valid first-commit.
 * Pass `expectedGeneration: undefined` (or omit) for the first generation;
 * pass a concrete number for subsequent generations.
 */
export function generationMatches(
	current: number | undefined,
	expected: number | undefined,
): boolean {
	// undefined === undefined is the explicit "first commit" semantic (§5.3).
	return current === expected;
}
