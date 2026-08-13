/**
 * Platform Plugin Contract v0.1 — Stage 1 type surface (design doc §6.1/§7).
 *
 * This module is the stable boundary between HistoPilot (the AI reading plugin)
 * and the platform that owns the slide/annotation data. Starting at Stage 1 the
 * HistoPilot core (agent, tools, request assembler, transform context) depends
 * ONLY on {@link PlatformClient} and the types in this file — never on Flask
 * private names, the `/internal/ai/*` endpoints or the legacy base64 image
 * transport.
 *
 * Stage 1 scope notes (deliberate, documented deviations from the §6.1 sketch):
 *   - The four capabilities actually exercised by the current sidecar keep
 *     consumer-compatible method names (`slideInfo` / `region` / `spots` /
 *     `annotate`). The four not-yet-used capabilities (updateAnnotation /
 *     deleteAnnotation / openEventStream / appendAuditEvent) are declared on the
 *     interface per §7.2 ("PlatformClient 必须覆盖本表全部能力…不能从类型中
 *     消失") and the legacy adapter throws {@link CAPABILITY_NOT_SUPPORTED}.
 *     The doc's `getSlide`/`readRegion`/… names are the *target* names for the
 *     future PathTogatherHttpClient; renaming every call site + mock now would
 *     risk the Stage 1 "all existing tests green" gate without buying any
 *     decoupling (the current names carry no Flask semantics).
 *   - {@link RegionResult} is binary-native (`bytes: Uint8Array`) from Stage 1.
 *     The legacy adapter is the only place that performs base64→bytes.
 *   - {@link SlideDescriptor#assetRevision} carries the legacy opaque
 *     `mtime:size` fingerprint value (§4.4); the internal `SlideInfo` type still
 *     calls it `fingerprint` because that name is baked into the session/
 *     checkpoint file format (§11.2: do not change persisted formats).
 *   - {@link SpotChange} / {@link AnnotationResult} keep the legacy ROI dict
 *     field shape (snake_case columns like `side_px`, `annotation_id`). They
 *     mirror the platform's persisted domain object (§7.0: DB columns are
 *     snake_case); renaming them is an annotation-schema migration, not a
 *     transport decoupling, and is therefore out of scope for Stage 1.
 *
 * All public fields below are camelCase per §7.0 ("TypeScript 公共接口使用
 * camelCase"). The legacy adapter is the only place allowed to translate
 * between this camelCase contract and Flask's snake_case wire.
 */

// =========================================================================== //
// Slide identity (§4.4)
// =========================================================================== //

/**
 * Legacy slide reference: a bare filename. Stage 1–2 use this form. It MUST NOT
 * be masqueraded as a stable `slide_id` — a filename can be renamed or replaced
 * without changing identity, so it is kept as an explicit discriminated union
 * member rather than a bare `string` (§4.4: "禁止把裸文件名伪装成稳定 ID").
 */
export interface LegacySlideRef {
	readonly kind: "legacy-filename";
	readonly filename: string;
}

/**
 * Target stable slide identity, allocated by the platform as `sld_<uuidv7>`
 * (§4.4). Stage 3b establishes the `legacy filename → slide_id` mapping; until
 * then no request in the sidecar carries this form.
 */
export interface SlideIdRef {
	readonly kind: "slide-id";
	readonly slideId: string;
}

/** A slide reference as accepted by every slide-scoped capability. */
export type SlideRef = LegacySlideRef | SlideIdRef;

/** Construct a {@link LegacySlideRef} from a filename (the only form in Stage 1). */
export function legacySlide(filename: string): LegacySlideRef {
	return { kind: "legacy-filename", filename };
}

/** Extract the bare filename from a legacy ref, or throw for the stable-id form. */
export function legacyFilename(ref: SlideRef): string {
	if (ref.kind === "legacy-filename") return ref.filename;
	// The legacy Flask adapter only resolves filenames; a stable slide_id cannot
	// be turned into a filename without the Stage 3b mapping service.
	throw new ContractError({
		code: "slide_not_found",
		message: "stable slide_id cannot be resolved by the legacy adapter",
		retryable: false,
		httpStatus: 404,
	});
}

// =========================================================================== //
// Geometry / region (§7.3)
// =========================================================================== //

/** A level-0 bounding box (the coordinate space HistoPilot reasons in). */
export interface Bbox {
	x: number;
	y: number;
	w: number;
	h: number;
}

/**
 * Request to read a derived region image (§7.3). Carries the slide identity plus
 * the output spec. The legacy adapter translates this to the Flask
 * `/internal/ai/region` body (snake_case + `expected_fingerprint`).
 *
 * Either `outW`/`outH` (explicit, non-aspect-preserving when both set) or
 * `maxLongEdge` (aspect-preserving; preferred, §6.1) may be used. The platform
 * returns the real output dimensions on {@link RegionResult}.
 */
export interface RegionRequest {
	slide: SlideRef;
	bbox: Bbox;
	/** Explicit output width (does not preserve aspect when both outW/outH set). */
	outW?: number;
	/** Explicit output height. */
	outH?: number;
	/** Aspect-preserving longest-edge target; overrides outW/outH when set. */
	maxLongEdge?: number;
	/** JPEG quality (server default 85). */
	quality?: number;
	/**
	 * Overlay enum (§7.3). v0.1 allows `"none"` (default) and
	 * `"coordinate-ticks-v1"` (HistoPilot snapshot). Unknown values yield
	 * `400 invalid_overlay`.
	 */
	overlay?: string;
	/**
	 * The asset revision the caller built its derivative/cache against; the
	 * platform returns `409 slide_revision_conflict` on mismatch. In Stage 1 this
	 * carries the legacy opaque `mtime:size` fingerprint value (§4.4).
	 */
	expectedAssetRevision?: string;
}

/**
 * Derivative encoder descriptor (§6.3), returned so HistoPilot can record/validate
 * the deterministic derivative spec. camelCase per §7.0.
 */
export interface RegionEncoder {
	id: string;
	version: string;
	resize: string;
	overlayVersion: string;
	jpegQuality: number;
}

/**
 * Binary-native region result (§6.1/§7.3). From Stage 1 this is a `Uint8Array`
 * payload — the legacy base64 transport (`image_base64`) is decoded inside the
 * adapter and never reaches the agent/tools/assembler layers.
 *
 * `contentSha256` is computed by the adapter from the decoded JPEG bytes (§6.1).
 * `assetRevision` is the slide revision the region was read against; the legacy
 * Flask region response does not carry it per-call, so it is left `undefined`
 * until the Stage 3b content-addressed asset revision exists.
 */
export interface RegionResult {
	/** Raw JPEG bytes (already decoded from any wire transport). */
	bytes: Uint8Array;
	mimeType: "image/jpeg";
	width: number;
	height: number;
	src: Bbox;
	magnification: number | null;
	/** SHA-256 of {@link bytes}, hex-encoded. Computed by the adapter. */
	contentSha256: string;
	/** Slide asset revision the region was read against (opaque). */
	assetRevision: string | undefined;
	encoder: RegionEncoder | undefined;
}

/**
 * Slide metadata descriptor (§7.2 GET /slides/{id}). {@link assetRevision}
 * carries the legacy opaque fingerprint in Stage 1.
 */
export interface SlideDescriptor {
	width: number;
	height: number;
	levelDownsamples: number[];
	mpp: number | null;
	assetRevision: string;
}

// =========================================================================== //
// Annotations / change stream (§7.2)
// =========================================================================== //

/**
 * Legacy ROI/annotation domain object, mirroring the platform's persisted
 * annotation row (snake_case columns). Stage 1 passes this shape through
 * unchanged (§11.2: do not alter annotation semantics); full camelCase
 * normalization is deferred to the annotation-schema migration.
 */
export interface RoiDict {
	annotation_id: string;
	index: number;
	token: string;
	slide: string;
	label: string;
	note: string;
	type: string;
	x: number;
	y: number;
	side_px: number;
	size_mm: number;
	shared: boolean;
	source: string;
	created_by_session_id: string;
	change_seq: number;
	revision: number;
	[k: string]: unknown;
}

/** One change-log entry (may carry a tombstone marker). */
export type SpotChange = RoiDict & { deleted?: boolean };

/**
 * Incremental change page (§7.2 GET .../annotations?after=, §8.3). `currentSeq`
 * is the resume cursor. The legacy adapter surfaces Flask's `current_seq`.
 */
export interface ChangePage {
	changes: SpotChange[];
	currentSeq: number;
}

/**
 * Run-grant reference (§7.6). Issued by the platform at run start and injected
 * into the sidecar run config as `config.run_grant` by the Flask proxy. The v1
 * annotate capability requires the grant id (as `X-Run-Grant`); the stable-id
 * and installation identity fields are carried for self-verification/logging.
 */
export interface RunGrantRef {
	/** The grant id the platform issued at run start. */
	grant_id: string;
	/** The owning plugin installation id. */
	installation_id: string;
	/** The slide this grant is scoped to. */
	slide: string;
	/** Grant expiry (unix seconds, platform's `expires_at`). */
	expires_at?: number | string;
}

/**
 * Idempotent annotation create request (§6.4). `effectKey` is the idempotency
 * key (recommended `${session_id}:${tool_call_id}:${effect_seq}`).
 */
export interface CreateAnnotationRequest {
	slide: SlideRef;
	label: string;
	x: number;
	y: number;
	sidePx: number;
	note?: string;
	effectKey?: string;
	sessionId?: string;
	/**
	 * Run-grant for the v1 annotate capability. The formal channel REQUIRES it
	 * (else 403 `run_grant_invalid`); the legacy adapter ignores it (uses the
	 * shared `X-AI-Internal-Token`). Absent on internal/legacy runs.
	 */
	runGrant?: RunGrantRef;
}

/** Result of an annotation write — the legacy ROI dict shape. */
export type AnnotationResult = RoiDict;

/** Revision-CAS update (declared, not yet exercised in Stage 1). */
export interface UpdateAnnotationRequest {
	annotationId: string;
	revision: number;
	patch: Record<string, unknown>;
}

/** Revision-CAS tombstone delete (declared, not yet exercised in Stage 1). */
export interface DeleteAnnotationRequest {
	annotationId: string;
	revision: number;
}

/** Same-row tombstone result (§5.3). */
export interface AnnotationTombstone {
	annotationId: string;
	slideId: string;
	revision: number;
	deletedAt: string;
}

// =========================================================================== //
// Event stream (§7.4) + audit (§7.2)
// =========================================================================== //

/** Request to open the platform event stream (declared, not yet exercised). */
export interface EventStreamRequest {
	slide: SlideRef;
	/** Resume cursor; omit for a full snapshot. */
	afterCursor?: string;
}

/** A single platform event (§7.4). Unknown `type`s must be ignored, not fatal. */
export interface PlatformEvent {
	cursor: string;
	occurredAt: string;
	resourceRevision?: string;
	type: string;
	payload: Record<string, unknown>;
}

/** Plugin audit event (§7.2 POST /audit/plugin-events, declared, not yet exercised). */
export interface PluginAuditEvent {
	pluginId: string;
	pluginVersion: string;
	runId?: string;
	sessionId?: string;
	userId?: string;
	action: string;
	details?: Record<string, unknown>;
}

// =========================================================================== //
// Unified error envelope (§7.7)
// =========================================================================== //

/** Stable machine-readable error code (§7.7). Program branches key off this. */
export type ContractErrorCode =
	| "invalid_request"
	| "invalid_overlay"
	| "token_expired"
	| "invalid_token"
	| "permission_denied"
	| "plugin_disabled"
	| "slide_not_found"
	| "annotation_not_found"
	| "slide_revision_conflict"
	| "revision_conflict"
	| "idempotency_key_reused"
	| "cursor_expired"
	| "rate_limited"
	| "region_budget_exceeded"
	| "service_unavailable"
	| "region_failed"
	| "capability_not_supported"
	| "unknown_error"
	// Stage 4-1b: codes surfaced by the formal /api/plugin/v1 channel (§7.7). They
	// are deliberately added to the stable vocabulary (not folded into a nearby
	// legacy code) so program branches can tell them apart:
	//   - "unauthorized": the installation secret was rejected at token exchange
	//     (wrong/rotated secret or installation disabled). Non-retryable by itself
	//     — the operator must fix the credential file/env.
	//   - "run_grant_invalid": an annotation write was refused because the
	//     X-Run-Grant is missing/expired/revoked/mismatched (403). Non-retryable.
	//   - "unavailable": a non-2xx v1 response whose error envelope is missing or
	//     malformed (platform too old, or a proxy swallowed the JSON body). Treated
	//     as retryable because it is defensive against version-skew, not an
	//     authoritative rejection.
	//   - "integrity_error": a region payload's Content-SHA256 did not match the
	//     declared or locally-computed digest (corrupt/forged bytes). Non-retryable.
	| "unauthorized"
	| "run_grant_invalid"
	| "unavailable"
	| "integrity_error";

/** Error thrown by every {@link PlatformClient} capability on failure (§7.7). */
export class ContractError extends Error {
	readonly code: ContractErrorCode;
	readonly retryable: boolean;
	/** Mapped HTTP status (§7.7 table); `0` when not HTTP-derived. */
	readonly httpStatus: number;
	readonly requestId?: string;
	readonly details?: Record<string, unknown>;

	constructor(opts: {
		code: ContractErrorCode;
		message: string;
		retryable: boolean;
		httpStatus: number;
		requestId?: string;
		details?: Record<string, unknown>;
	}) {
		super(opts.message);
		this.name = "ContractError";
		this.code = opts.code;
		this.retryable = opts.retryable;
		this.httpStatus = opts.httpStatus;
		this.requestId = opts.requestId;
		this.details = opts.details;
	}
}

/** Code returned when a capability is declared but not implemented in Stage 1. */
export const CAPABILITY_NOT_SUPPORTED = "capability_not_supported" as const;

// =========================================================================== //
// PlatformClient (§6.1 / §7.2)
// =========================================================================== //

/**
 * The stable capability surface HistoPilot depends on. Covers every row of the
 * §7.2 capability table; capabilities not yet exercised throw
 * {@link CAPABILITY_NOT_SUPPORTED} from the legacy adapter rather than
 * disappearing from the type.
 *
 * Method names kept consumer-compatible for the four exercised capabilities
 * (see file header); the §6.1 target names are noted for each.
 */
export interface PlatformClient {
	/** GET /slides/{id} (§7.2; doc name `getSlide`). */
	slideInfo(ref: SlideRef): Promise<SlideDescriptor>;
	/** POST .../regions (§7.3; doc name `readRegion`). */
	region(request: RegionRequest, signal?: AbortSignal): Promise<RegionResult>;
	/** GET .../annotations?after= (§7.2/§8.3; doc name `listAnnotationChanges`). */
	spots(ref: SlideRef, afterCursor: number): Promise<ChangePage>;
	/** POST .../annotations (§6.4; doc name `createAnnotation`). */
	annotate(request: CreateAnnotationRequest): Promise<AnnotationResult>;

	// --- Declared capabilities, not yet exercised in Stage 1 (§7.2) --- //
	/**
	 * Best-effort run-grant self-check (§7.6). Implemented by the v1 client; the
	 * legacy adapter does not implement it (undefined) because it has no grant
	 * model. The AgentRunner calls it before a run when `config.run_grant` is
	 * present, logging-only on failure so platform/plugin version skew cannot
	 * block a run.
	 */
	verifyRunGrant?(grant: RunGrantRef): Promise<{ valid: boolean; reason: string }>;
	updateAnnotation(request: UpdateAnnotationRequest): Promise<AnnotationResult>;
	deleteAnnotation(request: DeleteAnnotationRequest): Promise<AnnotationTombstone>;
	openEventStream(request: EventStreamRequest, signal?: AbortSignal): AsyncIterable<PlatformEvent>;
	appendAuditEvent(event: PluginAuditEvent): Promise<void>;
}

// =========================================================================== //
// pi integration helper
// =========================================================================== //

/**
 * Encode contract {@link RegionResult.bytes} as a base64 string for providers/pi
 * (whose `ImageContent.data` is a base64 string). This is the pi serialization
 * boundary — it is NOT the legacy `image_base64` wire transport (that decoding
 * lives in the legacy adapter). Centralized here so call sites do not each reach
 * for `Buffer`.
 */
export function bytesToBase64(bytes: Uint8Array): string {
	// Buffer.from(Uint8Array) copies the bytes; toString("base64") standard base64.
	return Buffer.from(bytes.buffer, bytes.byteOffset, bytes.byteLength).toString("base64");
}
