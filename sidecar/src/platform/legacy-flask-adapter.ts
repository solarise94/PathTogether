/**
 * Platform Plugin Contract v0.1 — legacy Flask adapter (design doc §6.1/§9.2).
 *
 * Wraps the existing {@link FlaskClient} (loopback `/internal/ai/*` secured by
 * `X-AI-Internal-Token`) so the HistoPilot core can talk to a stable
 * {@link PlatformClient} surface instead. This adapter is the ONLY place that:
 *   - knows about `/internal/ai/*` endpoints and the `FlaskClient` engine;
 *   - decodes the legacy `image_base64` JSON transport into contract
 *     {@link RegionResult.bytes};
 *   - translates Flask's snake_case wire into the camelCase contract (§7.0).
 *
 * Zero new wire behavior (Stage 1 §18.4): same endpoints, same payloads, same
 * token. The {@link FlaskClient} class remains unchanged and remains the
 * adapter's engine; only this adapter touches it.
 */
import { createHash } from "node:crypto";

import { FlaskHttpError, type FlaskClient } from "../flask-client.js";
import {
	CAPABILITY_NOT_SUPPORTED,
	ContractError,
	legacyFilename,
	type AnnotationResult,
	type AnnotationTombstone,
	type ChangePage,
	type CreateAnnotationRequest,
	type DeleteAnnotationRequest,
	type EventStreamRequest,
	type PlatformClient,
	type PlatformEvent,
	type PluginAuditEvent,
	type RegionEncoder,
	type RegionRequest,
	type RegionResult,
	type SlideDescriptor,
	type SlideRef,
	type UpdateAnnotationRequest,
} from "./contract.js";

// =========================================================================== //
// HTTP status → contract error mapping (§7.7)
// =========================================================================== //

interface MappedCode {
	code: ContractError["code"];
	retryable: boolean;
}

/**
 * Map an HTTP status to a stable {@link ContractError} code + retryable flag
 * (§7.7 base mapping). `401 token_expired` is the only 4xx flagged retryable
 * (renewable); `429`/`5xx` are retryable per the table.
 */
function mapStatus(status: number): MappedCode {
	if (status === 400) return { code: "invalid_request", retryable: false };
	if (status === 401) return { code: "token_expired", retryable: true };
	if (status === 403) return { code: "permission_denied", retryable: false };
	if (status === 404) return { code: "slide_not_found", retryable: false };
	if (status === 409) return { code: "slide_revision_conflict", retryable: false };
	if (status === 410) return { code: "cursor_expired", retryable: false };
	if (status === 429) return { code: "rate_limited", retryable: true };
	if (status >= 500) return { code: "service_unavailable", retryable: true };
	return { code: "unknown_error", retryable: false };
}

/**
 * Re-throw a {@link FlaskHttpError} as a {@link ContractError}, preserving the
 * HTTP status and any parseable body. Non-Flask errors pass through unchanged
 * (e.g. AbortError — the contract caller still sees an abort).
 */
function mapFlaskError(e: unknown): never {
	if (e instanceof FlaskHttpError) {
		const { code, retryable } = mapStatus(e.status);
		// Flask error bodies are not the §7.7 envelope (legacy endpoint), so best-
		// effort message extraction; the stable `code` is what callers branch on.
		const bodyMsg = typeof (e.body as { error?: unknown } | undefined)?.error === "object"
			? String((e.body as { error: { message?: unknown } }).error.message ?? "")
			: typeof e.body === "string" && e.body ? e.body : "";
		throw new ContractError({
			code,
			message: bodyMsg || e.message || `flask ${e.status}`,
			retryable,
			httpStatus: e.status,
			details: typeof e.body === "object" && e.body ? (e.body as Record<string, unknown>) : undefined,
		});
	}
	throw e;
}

/** Normalize Flask's snake_case encoder descriptor to the camelCase contract. */
function normalizeEncoder(enc: { id?: string; version?: string; resize?: string; overlay_version?: string; jpeg_quality?: number } | undefined): RegionEncoder | undefined {
	if (!enc) return undefined;
	return {
		id: String(enc.id ?? ""),
		version: String(enc.version ?? ""),
		resize: String(enc.resize ?? ""),
		overlayVersion: String(enc.overlay_version ?? ""),
		jpegQuality: Number(enc.jpeg_quality ?? 0),
	};
}

// =========================================================================== //
// LegacyFlaskPlatformAdapter
// =========================================================================== //

/**
 * Adapter options (mirror {@link FlaskClient} construction). The concrete
 * engine is supplied explicitly so production wires `createFlaskClient()` and
 * tests can pass a stub.
 */
export interface LegacyFlaskPlatformAdapterOptions {
	/** The FlaskClient engine to wrap. */
	flask: FlaskClient;
}

/**
 * {@link PlatformClient} implementation backed by the current Flask internal
 * endpoints. Performs base64→bytes decode and snake_case→camelCase
 * normalization; emits {@link ContractError} for every non-2xx Flask response.
 *
 * This class exists only for the migration period (§9.1 `legacy/flask-adapter/`);
 * it is replaced by `PathTogatherHttpClient` once `/api/plugin/v1` is live.
 */
export class LegacyFlaskPlatformAdapter implements PlatformClient {
	private readonly flask: FlaskClient;

	constructor(opts: LegacyFlaskPlatformAdapterOptions) {
		this.flask = opts.flask;
	}

	async slideInfo(ref: SlideRef): Promise<SlideDescriptor> {
		const filename = legacyFilename(ref);
		try {
			const r = await this.flask.slideInfo(filename);
			return {
				width: r.width,
				height: r.height,
				levelDownsamples: [...(r.level_downsamples || [1.0])],
				mpp: r.mpp == null ? null : r.mpp,
				// §4.4: the legacy opaque `mtime:size` fingerprint is surfaced as the
				// (legacy) asset revision. It is NOT an `ar_sha256_*` value.
				assetRevision: r.fingerprint || "",
			};
		} catch (e) {
			throw mapFlaskError(e);
		}
	}

	async region(request: RegionRequest, signal?: AbortSignal): Promise<RegionResult> {
		const filename = legacyFilename(request.slide);
		const { x, y, w, h } = request.bbox;
		try {
			const r = await this.flask.region({
				slide: filename,
				x,
				y,
				w,
				h,
				out_w: request.outW,
				out_h: request.outH,
				max_long_edge: request.maxLongEdge,
				jpeg_quality: request.quality,
				expected_fingerprint: request.expectedAssetRevision,
				// Strip signal from the contract surface; FlaskClient.region merges
				// it with its internal timeout controller.
				signal,
			});
			// base64 → bytes (the ONLY base64 decode in the codebase).
			const bytes = Buffer.from(r.image_base64 || "", "base64");
			return {
				bytes,
				mimeType: "image/jpeg",
				width: r.width,
				height: r.height,
				src: r.src,
				magnification: r.magnification,
				contentSha256: createHash("sha256").update(bytes).digest("hex"),
				// The legacy region response does not carry a per-call asset revision.
				assetRevision: undefined,
				encoder: normalizeEncoder(r.encoder),
			};
		} catch (e) {
			throw mapFlaskError(e);
		}
	}

	async spots(ref: SlideRef, afterCursor: number): Promise<ChangePage> {
		const filename = legacyFilename(ref);
		try {
			const r = await this.flask.spots(filename, afterCursor);
			return {
				changes: (r.changes || []) as ChangePage["changes"],
				currentSeq: r.current_seq,
			};
		} catch (e) {
			throw mapFlaskError(e);
		}
	}

	async annotate(request: CreateAnnotationRequest): Promise<AnnotationResult> {
		const filename = legacyFilename(request.slide);
		try {
			return await this.flask.annotate({
				slide: filename,
				label: request.label,
				x: request.x,
				y: request.y,
				side_px: request.sidePx,
				note: request.note,
				effect_key: request.effectKey,
				session_id: request.sessionId,
			});
		} catch (e) {
			throw mapFlaskError(e);
		}
	}

	// ------------------------------------------------------------------------- //
	// Declared capabilities NOT exercised in Stage 1 (§7.2). The legacy Flask
	// backend has no endpoints for these, so they fail fast with a stable code
	// instead of a 404 or silent no-op.
	// ------------------------------------------------------------------------- //

	async updateAnnotation(_request: UpdateAnnotationRequest): Promise<AnnotationResult> {
		throw new ContractError({
			code: CAPABILITY_NOT_SUPPORTED,
			message: "updateAnnotation is not available via the legacy Flask adapter",
			retryable: false,
			httpStatus: 0,
		});
	}

	async deleteAnnotation(_request: DeleteAnnotationRequest): Promise<AnnotationTombstone> {
		throw new ContractError({
			code: CAPABILITY_NOT_SUPPORTED,
			message: "deleteAnnotation is not available via the legacy Flask adapter",
			retryable: false,
			httpStatus: 0,
		});
	}

	openEventStream(_request: EventStreamRequest, _signal?: AbortSignal): AsyncIterable<PlatformEvent> {
		throw new ContractError({
			code: CAPABILITY_NOT_SUPPORTED,
			message: "openEventStream is not available via the legacy Flask adapter",
			retryable: false,
			httpStatus: 0,
		});
	}

	async appendAuditEvent(_event: PluginAuditEvent): Promise<void> {
		throw new ContractError({
			code: CAPABILITY_NOT_SUPPORTED,
			message: "appendAuditEvent is not available via the legacy Flask adapter",
			retryable: false,
			httpStatus: 0,
		});
	}
}
