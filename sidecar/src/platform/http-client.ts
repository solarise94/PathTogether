/**
 * Platform Plugin Contract v0.1 — formal `/api/plugin/v1` HTTP client (Stage
 * 4-1b, design doc §9.1 `pathtogather/http-client/`).
 *
 * {@link PathTogatherHttpClient} is the production {@link PlatformClient}
 * implementation for the formal plugin channel:
 *
 *   - credentials: installation secret + installation_id, resolved once at
 *     construction (see `plugin-credentials.ts` for the file/env resolution);
 *   - auth: lazy, cached exchange of `secret → scoped JWT` via
 *     `POST /api/plugin/v1/auth/token` (HS256, `expires_in: 900`); the token is
 *     refreshed before expiry (with a configurable margin) and once on a
 *     `401 token_expired` mid-request (exactly one replay — no retry loops);
 *   - every capability carries `Authorization: Bearer <jwt>` and unwraps the
 *     unified error envelope `{error:{code,message,retryable}}` (§7.7) into a
 *     {@link ContractError};
 *   - annotate additionally sends `X-Run-Grant` (see {@link RunGrantRef}).
 *
 * Security posture: this class NEVER logs the installation secret or the access
 * token (see {@link PathTogatherHttpClientOptions#secret} note). A code scan
 * that searches for these values in console/warn/error output must find none.
 *
 * The legacy `/internal/ai/*` adapter remains the fallback (no credentials); it
 * is the only place that knows base64, and it is unchanged by this node.
 */
import { createHash } from "node:crypto";

import { withTrustedCallback } from "../ssrf-guard.js";

import {
	CAPABILITY_NOT_SUPPORTED,
	ContractError,
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
	type RunGrantRef,
	type SlideDescriptor,
	type SlideRef,
	type UpdateAnnotationRequest,
} from "./contract.js";

// =========================================================================== //
// Construction
// =========================================================================== //

export interface PathTogatherHttpClientOptions {
	/** Flask base URL (env AI_FLASK_URL); `/api/plugin/v1` is appended. */
	baseUrl: string;
	/** Plugin installation id (env PLUGIN_INSTALLATION_ID or file). */
	installationId: string;
	/**
	 * Installation secret (env PLUGIN_HISTOPILOT_SECRET or file). Exchanged for
	 * a short-lived JWT at request time. MUST NOT be logged anywhere in this
	 * class — no console/logger statement may include `this.secret`.
	 */
	secret: string;
	/**
	 * Injectable fetch for tests (defaults to the global fetch). Must return a
	 * WHATWG `Response`.
	 */
	fetch?: typeof fetch;
	/** Refresh the cached token this many seconds before its nominal expiry. */
	tokenMarginSec?: number;
}

interface TokenState {
	value: string;
	/** Absolute expiry timestamp (ms). */
	expiresAtMs: number;
}

/**
 * The formal v1 PlatformClient. See the module docstring for the auth and
 * envelope contract. Implements the same four exercised capabilities as the
 * legacy adapter (semantics-aligned), plus the optional run-grant self-check.
 */
export class PathTogatherHttpClient implements PlatformClient {
	private readonly baseUrl: string;
	private readonly installationId: string;
	private readonly secret: string;
	private readonly doFetch: typeof fetch;
	private readonly marginMs: number;

	private token: TokenState | null = null;
	/** Single-flight in-progress token refresh (prevents stampede). */
	private tokenPromise: Promise<string> | null = null;

	constructor(opts: PathTogatherHttpClientOptions) {
		this.baseUrl = opts.baseUrl.replace(/\/+$/, "");
		this.installationId = opts.installationId;
		this.secret = opts.secret;
		const rawFetch = opts.fetch ?? fetch;
		this.doFetch = ((input: Parameters<typeof fetch>[0], init?: RequestInit) =>
			withTrustedCallback(() => rawFetch(input, init))) as typeof fetch;
		this.marginMs = (opts.tokenMarginSec ?? 60) * 1000;
	}

	// ========================================================================= //
	// Auth (lazy + cached + margin + single-flight + one-shot 401 replay)
	// ========================================================================= //

	/** Exchange the installation secret for a scoped access JWT (§7.6 step 3/5). */
	private async exchangeToken(): Promise<TokenState> {
		// NOTE: do not log `this.secret` or the returned token anywhere.
		let res: Response;
		try {
			res = await this.doFetch(`${this.baseUrl}/api/plugin/v1/auth/token`, {
				method: "POST",
				headers: { "Content-Type": "application/json" },
				body: JSON.stringify({
					installation_id: this.installationId,
					secret: this.secret,
				}),
			});
		} catch (e) {
			// Network failure reaching the platform — surface as non-retryable so
			// the caller does not hammer an unreachable endpoint with token retries.
			throw new ContractError({
				code: "unavailable",
				message: `插件 token 端点不可达：${(e as Error)?.message || e}`,
				retryable: false,
				httpStatus: 0,
			});
		}
		const body = (await readJson(res)) as Record<string, unknown> | undefined;
		if (!res.ok) {
			// Wrong/rotated secret or disabled installation → the operator must fix
			// the credential file/env; never auto-retry. Surface as `unauthorized`.
			const err = mapEnvelope(res.status, body, res.headers);
			throw new ContractError({
				code: "unauthorized",
				message: err?.message || `插件凭证换取失败（${res.status}）`,
				retryable: false,
				httpStatus: res.status,
				details: err?.details,
			});
		}
		const accessToken = typeof body?.access_token === "string" ? body.access_token : "";
		const expiresIn = Number(body?.expires_in ?? 900);
		if (!accessToken) {
			throw new ContractError({
				code: "unavailable",
				message: "插件 token 响应缺少 access_token",
				retryable: false,
				httpStatus: res.status,
			});
		}
		return { value: accessToken, expiresAtMs: Date.now() + expiresIn * 1000 };
	}

	/** Return a fresh access token, refreshing (single-flight) as needed. */
	private async getToken(): Promise<string> {
		const cached = this.token;
		if (cached && Date.now() < cached.expiresAtMs - this.marginMs) return cached.value;
		if (!this.tokenPromise) {
			this.tokenPromise = this.exchangeToken()
				.then((t) => {
					this.token = t;
					return t.value;
				})
				.finally(() => {
					this.tokenPromise = null;
				});
		}
		return this.tokenPromise;
	}

	/** Force a refresh on the next {@link getToken} (used after 401 token_expired). */
	private invalidateToken(): void {
		this.token = null;
	}

	// ========================================================================= //
	// Unified request (bearer + envelope + one-shot 401 token_expired replay)
	// ========================================================================= //

	private async request(opts: {
		method: string;
		path: string;
		query?: Record<string, string>;
		body?: unknown;
		extraHeaders?: Record<string, string>;
		signal?: AbortSignal;
	}): Promise<{ status: number; body: unknown; headers: Headers }> {
		const res = await this.requestRaw(opts, true);
		const body = await readJson(res);
		return { status: res.status, body, headers: res.headers };
	}

	/**
	 * Core transport: bearer + fetch + error-envelope handling + one-shot 401
	 * token_expired replay. Returns the **unread** `Response` on success so the
	 * caller consumes the body in the right shape (JSON for capabilities, raw
	 * bytes for the binary region transport — Stage 4-2). On non-2xx the body is
	 * always the JSON envelope — even for binary endpoints — and is read + mapped
	 * here (the success body is never touched in that case, so the single-read
	 * contract on `Response` is preserved).
	 */
	private async requestRaw(opts: {
		method: string;
		path: string;
		query?: Record<string, string>;
		body?: unknown;
		extraHeaders?: Record<string, string>;
		signal?: AbortSignal;
	}, allowReplay: boolean): Promise<Response> {
		const token = await this.getToken();
		const url = new URL(`${this.baseUrl}/api/plugin/v1${opts.path}`);
		if (opts.query) {
			for (const [k, v] of Object.entries(opts.query)) url.searchParams.set(k, v);
		}
		let res: Response;
		try {
			res = await this.doFetch(url, {
				method: opts.method,
				headers: {
					"Authorization": `Bearer ${token}`,
					...(opts.body !== undefined ? { "Content-Type": "application/json" } : {}),
					...(opts.extraHeaders ?? {}),
				},
				body: opts.body !== undefined ? JSON.stringify(opts.body) : undefined,
				signal: opts.signal,
			});
		} catch (e) {
			// Network failure → map to retryable service_unavailable so callers can
			// back off; do NOT replay (nothing to do with the token).
			throw new ContractError({
				code: "service_unavailable",
				message: `插件请求失败：${(e as Error)?.message || e}`,
				retryable: true,
				httpStatus: 0,
			});
		}
		if (!res.ok) {
			const body = (await readJson(res)) as Record<string, unknown> | undefined;
			const err = mapEnvelope(res.status, body, res.headers);
			if (allowReplay && err?.code === "token_expired") {
				// The cached token lapsed mid-flight; refresh exactly once and replay.
				// `allowReplay=false` on the retry guarantees a single retry, never a loop.
				this.invalidateToken();
				return this.requestRaw(opts, false);
			}
			throw err;
		}
		return res;
	}

	// ========================================================================= //
	// PlatformClient capabilities
	// ========================================================================= //

	async slideInfo(ref: SlideRef): Promise<SlideDescriptor> {
		const { body } = await this.request({ method: "GET", path: `/slides/${enc(ref)}` });
		const r = body as Record<string, unknown>;
		return {
			width: Number(r.width ?? 0),
			height: Number(r.height ?? 0),
			levelDownsamples: Array.isArray(r.level_downsamples)
				? (r.level_downsamples as number[]).map(Number)
				: [1.0],
			mpp: r.mpp == null ? null : Number(r.mpp),
			// §4.4: the platform surfaces the legacy opaque `mtime:size` as
			// `asset_revision` (fall back to `fingerprint` for older platforms).
			assetRevision: String(r.asset_revision ?? r.fingerprint ?? ""),
		};
	}

	async region(request: RegionRequest, signal?: AbortSignal): Promise<RegionResult> {
		const body: Record<string, unknown> = {
			x: request.bbox.x,
			y: request.bbox.y,
			w: request.bbox.w,
			h: request.bbox.h,
		};
		if (request.outW !== undefined) body.out_w = request.outW;
		if (request.outH !== undefined) body.out_h = request.outH;
		if (request.maxLongEdge !== undefined) body.max_long_edge = request.maxLongEdge;
		if (request.quality !== undefined) body.jpeg_quality = request.quality;
		if (request.expectedAssetRevision) body.expected_fingerprint = request.expectedAssetRevision;
		// Stage 4-2: prefer the binary transport (raw JPEG bytes + metadata
		// headers). The success body shape branches on Content-Type:
		//   - application/octet-stream → bytes path (this node's default);
		//   - application/json → legacy base64 path (older platforms that do not
		//     implement binary negotiation).
		// On error the body is ALWAYS the JSON envelope regardless of transport,
		// and is consumed + mapped inside requestRaw (the success body is left
		// unread there, so the single-read contract on Response holds).
		const res = await this.requestRaw({
			method: "POST",
			path: `/slides/${enc(request.slide)}/regions`,
			body,
			extraHeaders: { Accept: "application/octet-stream" },
			signal,
		}, true);
		const contentType = (res.headers.get("content-type") || "").toLowerCase();
		if (contentType.includes("application/octet-stream")) {
			return this.parseBinaryRegion(res, request);
		}
		const r = (await readJson(res)) as Record<string, unknown>;
		return this.parseJsonRegion(r, request, res.headers);
	}

	/** Binary region transport (Stage 4-2): raw JPEG bytes + metadata from headers. */
	private async parseBinaryRegion(res: Response, request: RegionRequest): Promise<RegionResult> {
		const buf = Buffer.from(await res.arrayBuffer());
		const localSha = createHash("sha256").update(buf).digest("hex");
		// Content-SHA256 header is authoritative for the binary path (there is no
		// body field to fall back to). Mismatch → integrity_error (non-retryable).
		verifyRegionIntegrity(res.headers.get("content-sha256") || "", localSha);
		const bbox = headerJson(res.headers, "x-region-bbox");
		const out = headerJson(res.headers, "x-region-out");
		const magRaw = res.headers.get("x-region-magnification");
		return {
			bytes: buf,
			mimeType: "image/jpeg",
			width: Number(out?.outW ?? 0),
			height: Number(out?.outH ?? 0),
			src: {
				x: Number(bbox?.x ?? request.bbox.x),
				y: Number(bbox?.y ?? request.bbox.y),
				w: Number(bbox?.w ?? request.bbox.w),
				h: Number(bbox?.h ?? request.bbox.h),
			},
			magnification: magRaw == null || magRaw === "null" ? null : Number(magRaw),
			contentSha256: localSha,
			assetRevision: res.headers.get("x-asset-revision") || undefined,
			encoder: normalizeEncoder(headerJson(res.headers, "x-region-encoder")),
		};
	}

	/** Legacy JSON base64 region path (older platforms without binary negotiation). */
	private parseJsonRegion(
		r: Record<string, unknown>,
		request: RegionRequest,
		headers: Headers,
	): RegionResult {
		const b64 = typeof r.image_base64 === "string" ? r.image_base64 : "";
		const bytes = Buffer.from(b64, "base64");
		const localSha = createHash("sha256").update(bytes).digest("hex");
		// Integrity check: the Content-SHA256 header is authoritative; fall back to
		// the body `content_sha256` for older platforms. Mismatch → integrity_error.
		const declaredSha = headers.get("content-sha256")
			|| (typeof r.content_sha256 === "string" ? r.content_sha256 : "");
		verifyRegionIntegrity(declaredSha, localSha);
		const src = (r.src ?? {}) as Record<string, unknown>;
		return {
			bytes,
			mimeType: "image/jpeg",
			width: Number(r.width ?? 0),
			height: Number(r.height ?? 0),
			src: {
				x: Number(src.x ?? request.bbox.x),
				y: Number(src.y ?? request.bbox.y),
				w: Number(src.w ?? request.bbox.w),
				h: Number(src.h ?? request.bbox.h),
			},
			magnification: r.magnification == null ? null : Number(r.magnification),
			contentSha256: localSha,
			assetRevision: typeof r.asset_revision === "string" ? r.asset_revision : undefined,
			encoder: normalizeEncoder(r.encoder as Record<string, unknown> | undefined),
		};
	}

	async spots(ref: SlideRef, afterCursor: number): Promise<ChangePage> {
		const { body } = await this.request({
			method: "GET",
			path: `/slides/${enc(ref)}/changes`,
			query: { after_seq: String(afterCursor) },
		});
		const r = body as Record<string, unknown>;
		return {
			changes: (Array.isArray(r.changes) ? r.changes : []) as ChangePage["changes"],
			currentSeq: Number(r.current_seq ?? 0),
		};
	}

	async annotate(request: CreateAnnotationRequest): Promise<AnnotationResult> {
		const body: Record<string, unknown> = {
			label: request.label,
			x: request.x,
			y: request.y,
			side_px: request.sidePx,
		};
		if (request.note !== undefined) body.note = request.note;
		if (request.effectKey !== undefined) body.effect_key = request.effectKey;
		if (request.sessionId !== undefined) body.session_id = request.sessionId;
		// v1 REQUIRES X-Run-Grant. When absent (internal/legacy run), we still
		// call — the platform returns 403 run_grant_invalid and that error bubbles
		// to the caller (the AgentRunner deliberately does NOT degrade; see §2).
		const runGrant = request.runGrant;
		const { body: rbody } = await this.request({
			method: "POST",
			path: `/slides/${enc(request.slide)}/annotations`,
			body,
			extraHeaders: runGrant ? { "X-Run-Grant": runGrant.grant_id } : {},
		});
		return rbody as AnnotationResult;
	}

	async verifyRunGrant(grant: RunGrantRef): Promise<{ valid: boolean; reason: string }> {
		const { body } = await this.request({
			method: "POST",
			path: "/run-grants/verify",
			body: { grant_id: grant.grant_id, slide: grant.slide },
		});
		const r = body as Record<string, unknown>;
		return { valid: r.valid !== false, reason: typeof r.reason === "string" ? r.reason : "" };
	}

	// ------------------------------------------------------------------------- //
	// Declared capabilities not yet offered by the v1 channel in 4-1b (the
	// platform has no update/delete/stream/audit endpoints yet). Fail fast with a
	// stable code, matching the legacy adapter's behaviour for the same set.
	// ------------------------------------------------------------------------- //

	async updateAnnotation(_request: UpdateAnnotationRequest): Promise<AnnotationResult> {
		throw new ContractError({
			code: CAPABILITY_NOT_SUPPORTED,
			message: "updateAnnotation is not available via the v1 channel in 4-1b",
			retryable: false,
			httpStatus: 0,
		});
	}

	async deleteAnnotation(_request: DeleteAnnotationRequest): Promise<AnnotationTombstone> {
		throw new ContractError({
			code: CAPABILITY_NOT_SUPPORTED,
			message: "deleteAnnotation is not available via the v1 channel in 4-1b",
			retryable: false,
			httpStatus: 0,
		});
	}

	openEventStream(_request: EventStreamRequest, _signal?: AbortSignal): AsyncIterable<PlatformEvent> {
		throw new ContractError({
			code: CAPABILITY_NOT_SUPPORTED,
			message: "openEventStream is not available via the v1 channel in 4-1b",
			retryable: false,
			httpStatus: 0,
		});
	}

	async appendAuditEvent(_event: PluginAuditEvent): Promise<void> {
		throw new ContractError({
			code: CAPABILITY_NOT_SUPPORTED,
			message: "appendAuditEvent is not available via the v1 channel in 4-1b",
			retryable: false,
			httpStatus: 0,
		});
	}
}

// =========================================================================== //
// Helpers
// =========================================================================== //

/** encodeURIComponent'd slide reference for the path segment. */
function enc(ref: SlideRef): string {
	// Legacy filename (Stage 1-2) goes straight into the path; a stable slide_id
	// is already URL-safe but is still encoded for uniformity.
	const seg = ref.kind === "slide-id" ? ref.slideId : ref.filename;
	return encodeURIComponent(seg);
}

/** Best-effort JSON body parse (empty → undefined; malformed → raw string). */
async function readJson(res: Response): Promise<unknown> {
	const text = await res.text();
	if (!text) return undefined;
	try {
		return JSON.parse(text) as unknown;
	} catch {
		return text;
	}
}

interface EnvelopeError {
	code: string;
	message: string;
	retryable: boolean;
	details?: Record<string, unknown>;
}

/**
 * Unwrap a non-2xx v1 response into a {@link ContractError} from the unified
 * envelope `{error:{code,message,retryable}}` (§7.7). When the envelope is
 * missing or malformed (platform too old / proxy stripped the JSON), fall back
 * to `unavailable` (retryable) — a defensive code, not an authoritative error.
 *
 * The HTTP `Retry-After` header (§7.7, sent on 429 rate_limited by the Stage 4-2
 * pixel-budget / concurrency / rate-limit gates) is surfaced into
 * `ContractError.details.retry_after` so callers backing off on retryable errors
 * can read a concrete delay. Envelope `details` take precedence for any key it
 * already sets.
 */
function mapEnvelope(status: number, body: unknown, headers?: Headers): ContractError {
	const error = (body as { error?: unknown } | undefined)?.error;
	const env = (typeof error === "object" && error !== null ? error : {}) as Record<string, unknown>;
	const details = mergeRetryAfter(
		typeof env.details === "object" && env.details ? (env.details as Record<string, unknown>) : undefined,
		headers,
	);
	const hasDetails = details !== undefined;
	if (typeof env.code !== "string" || !env.code) {
		// Missing/畸形 envelope → defensive retryable code (platform version skew).
		return new ContractError({
			code: "unavailable",
			message: typeof error === "string" ? error : `插件端点返回 ${status}`,
			retryable: true,
			httpStatus: status,
			details,
		});
	}
	const retryable =
		typeof env.retryable === "boolean"
			? env.retryable
			: status >= 500 || status === 429; // envelope omits it → derive from status
	return new ContractError({
		code: mapEnvelopeCode(env.code, status),
		message: typeof env.message === "string" ? env.message : `插件端点返回 ${status}`,
		retryable,
		httpStatus: status,
		// Keep details literally `undefined` when empty so existing assertions that
		// do not check details are unaffected.
		details: hasDetails ? details : undefined,
	});
}

/**
 * Merge the platform envelope details with the HTTP `Retry-After` header. The
 * envelope details win for any key already present (e.g. the platform's own
 * `retry_after`); otherwise the header value (numeric seconds when parseable,
 * else the raw string) is added under `retry_after`. Returns `undefined` when
 * neither source contributed anything.
 */
function mergeRetryAfter(
	envDetails: Record<string, unknown> | undefined,
	headers?: Headers,
): Record<string, unknown> | undefined {
	const out: Record<string, unknown> = {};
	if (envDetails) Object.assign(out, envDetails);
	if (headers && out.retry_after === undefined) {
		const retryAfter = headers.get("retry-after");
		if (retryAfter !== null && retryAfter !== "") {
			const n = Number(retryAfter);
			out.retry_after = Number.isFinite(n) ? n : retryAfter;
		}
	}
	return Object.keys(out).length ? out : undefined;
}

/**
 * Verify a region payload's declared Content-SHA256 against the locally-computed
 * digest. Empty `declaredSha` is treated as "no assertion" (older platforms) and
 * passes; any non-empty mismatch → non-retryable `integrity_error`.
 */
function verifyRegionIntegrity(declaredSha: string, localSha: string): void {
	if (declaredSha && declaredSha.toLowerCase() !== localSha) {
		throw new ContractError({
			code: "integrity_error",
			message: "region Content-SHA256 与返回字节不符",
			retryable: false,
			httpStatus: 0,
			details: { expected: declaredSha, actual: localSha },
		});
	}
}

/** Parse a JSON-encoded response header (e.g. X-Region-Bbox) into an object. */
function headerJson(headers: Headers, name: string): Record<string, unknown> | undefined {
	const raw = headers.get(name);
	if (!raw) return undefined;
	try {
		const v = JSON.parse(raw);
		return typeof v === "object" && v !== null ? (v as Record<string, unknown>) : undefined;
	} catch {
		return undefined;
	}
}

/** Map the platform's v1 error code vocab to the stable contract code vocab. */
function mapEnvelopeCode(platformCode: string, status: number): ContractError["code"] {
	switch (platformCode) {
		case "invalid_request": return "invalid_request";
		case "token_expired": return "token_expired";
		// Capability-endpoint 401 unauthorized (missing/invalid/disabled token) →
		// invalid_token. Token-EXCHANGE failures use `unauthorized` (see exchangeToken).
		case "unauthorized": return "invalid_token";
		case "forbidden": return "permission_denied";
		case "run_grant_invalid": return "run_grant_invalid";
		case "not_found": return "slide_not_found";
		case "conflict": return "revision_conflict";
		case "slide_revision_conflict": return "slide_revision_conflict";
		case "rate_limited": return "rate_limited";
		case "internal":
		case "unavailable": return "service_unavailable";
		case "plugin_disabled": return "plugin_disabled";
		default:
			return status >= 500 ? "service_unavailable" : "unknown_error";
	}
}

/** Normalize the platform's snake_case encoder descriptor to the contract shape. */
function normalizeEncoder(enc: Record<string, unknown> | undefined): RegionEncoder | undefined {
	if (!enc) return undefined;
	return {
		id: String(enc.id ?? ""),
		version: String(enc.version ?? ""),
		resize: String(enc.resize ?? ""),
		overlayVersion: String(enc.overlay_version ?? ""),
		jpegQuality: Number(enc.jpeg_quality ?? 0),
	};
}
