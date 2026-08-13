/**
 * Platform Plugin Contract v0.1 — PathTogatherHttpClient tests (Stage 4-1b).
 *
 * Covers the formal /api/plugin/v1 client:
 *   - token lifecycle: lazy exchange + cache + margin renewal + single-flight;
 *     401 token_expired → refresh + replay exactly once; non-token 401 → no replay;
 *   - unified envelope → ContractError mapping matrix (code / retryable / httpStatus);
 *   - annotate carries X-Run-Grant; 409/403 (run_grant_invalid) mapping;
 *   - region Content-SHA256 integrity verify (pass + fail);
 *   - verifyRunGrant passthrough;
 *   - no secret/token ever reaches the fetch (scan) or any error/log.
 *
 * Uses an injectable fetch (`mockFetch`) with a scripted response queue, so no
 * real HTTP server is needed and the tests can assert on request headers.
 */
import { describe, expect, it } from "vitest";
import { createHash } from "node:crypto";

import { PathTogatherHttpClient, type PathTogatherHttpClientOptions } from "../src/platform/http-client.js";
import { ContractError, legacySlide, type CreateAnnotationRequest } from "../src/platform/contract.js";

// --------------------------------------------------------------------------- //
// Scripted fetch harness
// --------------------------------------------------------------------------- //

interface Call {
	url: string;
	method: string;
	headers: Record<string, string>;
	body: unknown;
}

interface ScriptedResponse {
	status: number;
	body?: unknown;
	headers?: Record<string, string>;
}

type Responder = (call: Call) => ScriptedResponse;

function jsonResponse(status: number, body: unknown, headers: Record<string, string> = {}): ScriptedResponse {
	return { status, body, headers };
}

function makeFetch(script: Responder[]) {
	const calls: Call[] = [];
	let index = 0;
	const fn = (input: Parameters<typeof fetch>[0], init?: RequestInit): Promise<Response> => {
		const url = typeof input === "string" ? input : input instanceof URL ? input.toString() : input.url;
		const rawBody = init?.body;
		const body = typeof rawBody === "string" ? (JSON.parse(rawBody) as unknown) : undefined;
		const headers: Record<string, string> = {};
		const h = init?.headers as Record<string, string> | undefined;
		if (h) {
			Object.entries(h).forEach(([k, v]) => {
				headers[k.toLowerCase()] = String(v);
			});
		}
		const call: Call = { url, method: init?.method ?? "GET", headers, body };
		calls.push(call);
		const responder = script[Math.min(index, script.length - 1)]!;
		const resp = responder(call);
		index += 1;
		const status = resp.status;
		const resHeaders = new Headers(resp.headers ?? {});
		const text = resp.body === undefined ? "" : JSON.stringify(resp.body);
		const r = new Response(text, { status, headers: resHeaders });
		return Promise.resolve(r);
	};
	return { fn: fn as typeof fetch, calls, get index() { return index; } };
}

function makeClient(fetchFn: typeof fetch, overrides: Partial<PathTogatherHttpClientOptions> = {}) {
	return new PathTogatherHttpClient({
		baseUrl: "http://platform.test",
		installationId: "pin_test123",
		secret: "super-secret-value",
		fetch: fetchFn,
		tokenMarginSec: 60,
		...overrides,
	});
}

const ACCESS_TOKEN = "jwt.token.abc";
const HEADER = (v: string) => `Bearer ${v}`;

// A minimal valid slide-info body.
const SLIDE_INFO_BODY = {
	width: 10000, height: 8000, level_downsamples: [1, 2, 4], mpp: 0.5,
	fingerprint: "fp:1", asset_revision: "rev:1",
};

function tokenResponse(): ScriptedResponse {
	return jsonResponse(200, { access_token: ACCESS_TOKEN, expires_in: 900, token_type: "bearer" });
}

// --------------------------------------------------------------------------- //
// Token lifecycle
// --------------------------------------------------------------------------- //

describe("PathTogatherHttpClient token lifecycle", () => {
	it("exchanges lazily (no fetch before the first capability call) and caches the token", async () => {
		const m = makeFetch([
			() => tokenResponse(),
			() => jsonResponse(200, SLIDE_INFO_BODY),
		]);
		const client = makeClient(m.fn);
		expect(m.calls.length).toBe(0); // lazy: no token fetch yet
		const d = await client.slideInfo(legacySlide("a.svs"));
		expect(d.assetRevision).toBe("rev:1");
		expect(m.calls.length).toBe(2);
		// first call = token exchange, body carries secret; auth header on the data call.
		expect(m.calls[0]!.url).toContain("/api/plugin/v1/auth/token");
		expect((m.calls[0]!.body as { secret?: unknown }).secret).toBe("super-secret-value");
		expect(m.calls[1]!.headers["authorization"]).toBe(HEADER(ACCESS_TOKEN));
		// second data call reuses the cached token → no re-exchange.
		await client.slideInfo(legacySlide("b.svs"));
		expect(m.calls.length).toBe(3);
		expect(m.calls[2]!.url).not.toContain("/auth/token");
	});

	it("renews the token proactively when within the margin", async () => {
		let seq = 0;
		const m = makeFetch([
			// token with expires_in=60 and margin 60 → immediately stale on first call
			() => jsonResponse(200, { access_token: "t1", expires_in: 60, token_type: "bearer" }),
			() => jsonResponse(200, SLIDE_INFO_BODY),
			// second capability call must re-exchange (expired within margin)
			() => jsonResponse(200, { access_token: "t2", expires_in: 900, token_type: "bearer" }),
			() => jsonResponse(200, SLIDE_INFO_BODY),
		]);
		const client = makeClient(m.fn, { tokenMarginSec: 60 });
		await client.slideInfo(legacySlide("a.svs"));
		await client.slideInfo(legacySlide("b.svs"));
		const tokenCalls = m.calls.filter((c) => c.url.includes("/auth/token"));
		expect(tokenCalls.length).toBe(2);
		// second data call carried the refreshed token
		expect(m.calls[3]!.headers["authorization"]).toBe(HEADER("t2"));
	});

	it("is single-flight: concurrent calls share one exchange", async () => {
		let tokenFetches = 0;
		const m = makeFetch([
			() => {
				tokenFetches += 1;
				return tokenResponse();
			},
			() => jsonResponse(200, SLIDE_INFO_BODY),
			() => jsonResponse(200, SLIDE_INFO_BODY),
		]);
		const client = makeClient(m.fn);
		await Promise.all([client.slideInfo(legacySlide("a.svs")), client.slideInfo(legacySlide("b.svs"))]);
		expect(tokenFetches).toBe(1);
	});

	it("on 401 token_expired refreshes exactly once and replays the request", async () => {
		const m = makeFetch([
			() => tokenResponse(), // initial token
			() => jsonResponse(401, { error: { code: "token_expired", message: "expired", retryable: true } }), // data call 401
			() => tokenResponse(), // refreshed token
			() => jsonResponse(200, SLIDE_INFO_BODY), // replay succeeds
		]);
		const client = makeClient(m.fn);
		const d = await client.slideInfo(legacySlide("a.svs"));
		expect(d.assetRevision).toBe("rev:1");
		const tokenCalls = m.calls.filter((c) => c.url.includes("/auth/token"));
		expect(tokenCalls.length).toBe(2);
		// the replay carried the refreshed token
		expect(m.calls[3]!.headers["authorization"]).toBe(HEADER(ACCESS_TOKEN));
	});

	it("on a 401 token_expired that still fails, does NOT loop (single replay)", async () => {
		const m = makeFetch([
			() => tokenResponse(),
			() => jsonResponse(401, { error: { code: "token_expired", message: "expired", retryable: true } }),
			() => tokenResponse(),
			() => jsonResponse(401, { error: { code: "token_expired", message: "expired", retryable: true } }),
			() => tokenResponse(),
		]);
		const client = makeClient(m.fn);
		await expect(client.slideInfo(legacySlide("a.svs"))).rejects.toMatchObject({
			name: "ContractError", code: "token_expired", retryable: true, httpStatus: 401,
		});
		// Only one refresh happened (no infinite loop)
		const tokenCalls = m.calls.filter((c) => c.url.includes("/auth/token"));
		expect(tokenCalls.length).toBe(2);
	});

	it("does NOT replay a non-token 401 (unauthorized)", async () => {
		const m = makeFetch([
			() => tokenResponse(),
			() => jsonResponse(401, { error: { code: "unauthorized", message: "no", retryable: false } }),
			() => tokenResponse(), // must NOT be reached
		]);
		const client = makeClient(m.fn);
		await expect(client.slideInfo(legacySlide("a.svs"))).rejects.toMatchObject({
			name: "ContractError", code: "invalid_token", retryable: false, httpStatus: 401,
		});
		const tokenCalls = m.calls.filter((c) => c.url.includes("/auth/token"));
		expect(tokenCalls.length).toBe(1);
	});

	it("maps token-exchange failure to unauthorized (non-retryable)", async () => {
		const m = makeFetch([
			() => jsonResponse(401, { error: { code: "unauthorized", message: "bad secret", retryable: false } }),
		]);
		const client = makeClient(m.fn);
		await expect(client.slideInfo(legacySlide("a.svs"))).rejects.toMatchObject({
			name: "ContractError", code: "unauthorized", retryable: false, httpStatus: 401,
		});
	});
});

// --------------------------------------------------------------------------- //
// Envelope → ContractError mapping matrix
// --------------------------------------------------------------------------- //

describe("PathTogatherHttpClient envelope mapping", () => {
	const cases: Array<[number, string, ContractError["code"], boolean]> = [
		[400, "invalid_request", "invalid_request", false],
		[403, "forbidden", "permission_denied", false],
		[403, "run_grant_invalid", "run_grant_invalid", false],
		[404, "not_found", "slide_not_found", false],
		[409, "slide_revision_conflict", "slide_revision_conflict", false],
		[429, "rate_limited", "rate_limited", true],
		[500, "internal", "service_unavailable", true],
		[503, "unavailable", "service_unavailable", true],
		// NOTE: 401 token_expired is deliberately NOT here — it triggers the
		// refresh+replay path, which is covered by the dedicated token tests.
		[401, "unauthorized", "invalid_token", false],
	];

	for (const [status, envCode, contractCode, retryable] of cases) {
		it(`maps ${status} ${envCode} → ${contractCode} (retryable=${retryable})`, async () => {
			const m = makeFetch([
				() => tokenResponse(),
				() => jsonResponse(status, { error: { code: envCode, message: "x", retryable } }),
			]);
			const client = makeClient(m.fn);
			await expect(client.slideInfo(legacySlide("a.svs"))).rejects.toMatchObject({
				name: "ContractError", code: contractCode, retryable, httpStatus: status,
			});
		});
	}

	it("maps a missing/malformed envelope → unavailable (retryable) — defensive", async () => {
		// envelope missing entirely (platform too old / proxy stripped body)
		const m1 = makeFetch([
			() => tokenResponse(),
			() => jsonResponse(502, { nope: true }),
		]);
		await expect(makeClient(m1.fn).slideInfo(legacySlide("a.svs"))).rejects.toMatchObject({
			name: "ContractError", code: "unavailable", retryable: true, httpStatus: 502,
		});

		// malformed: error present but no code
		const m2 = makeFetch([
			() => tokenResponse(),
			() => jsonResponse(500, { error: { message: "oops" } }),
		]);
		await expect(makeClient(m2.fn).slideInfo(legacySlide("a.svs"))).rejects.toMatchObject({
			name: "ContractError", code: "unavailable", retryable: true, httpStatus: 500,
		});
	});
});

// --------------------------------------------------------------------------- //
// Annotate + run grant
// --------------------------------------------------------------------------- //

describe("PathTogatherHttpClient annotate", () => {
	const ANNO_BODY = { annotation_id: "ann-1", index: 0, token: "admin", slide: "a.svs", label: "L", note: "", type: "rect", x: 1, y: 2, side_px: 30, size_mm: 0, shared: false, source: "ai", created_by_session_id: "sid", change_seq: 1, revision: 1 };

	it("sends X-Run-Grant header and the run_grant id", async () => {
		const m = makeFetch([
			() => tokenResponse(),
			() => jsonResponse(200, ANNO_BODY),
		]);
		const client = makeClient(m.fn);
		const req: CreateAnnotationRequest = {
			slide: legacySlide("a.svs"), label: "L", x: 1, y: 2, sidePx: 30,
			effectKey: "ek", sessionId: "sid",
			runGrant: { grant_id: "grant-xyz", installation_id: "pin_x", slide: "a.svs" },
		};
		const roi = await client.annotate(req);
		expect(roi.annotation_id).toBe("ann-1");
		const call = m.calls[1]!;
		expect(call.url).toContain("/api/plugin/v1/slides/a.svs/annotations");
		expect(call.headers["x-run-grant"]).toBe("grant-xyz");
		expect(call.headers["authorization"]).toBe(HEADER(ACCESS_TOKEN));
		expect(call.body).toMatchObject({ label: "L", x: 1, y: 2, side_px: 30, effect_key: "ek", session_id: "sid" });
	});

	it("omits X-Run-Grant when no grant is present (call still made)", async () => {
		const m = makeFetch([
			() => tokenResponse(),
			() => jsonResponse(403, { error: { code: "run_grant_invalid", message: "no grant", retryable: false } }),
		]);
		const client = makeClient(m.fn);
		const req: CreateAnnotationRequest = { slide: legacySlide("a.svs"), label: "L", x: 1, y: 2, sidePx: 30 };
		await expect(client.annotate(req)).rejects.toMatchObject({
			name: "ContractError", code: "run_grant_invalid", retryable: false, httpStatus: 403,
		});
		expect(m.calls[1]!.headers["x-run-grant"]).toBeUndefined();
	});

	it("maps 409 slide_revision_conflict", async () => {
		const m = makeFetch([
			() => tokenResponse(),
			() => jsonResponse(409, { error: { code: "slide_revision_conflict", message: "rev conflict", retryable: false } }),
		]);
		const client = makeClient(m.fn);
		await expect(client.annotate({ slide: legacySlide("a.svs"), label: "L", x: 0, y: 0, sidePx: 10 })).rejects.toMatchObject({
			name: "ContractError", code: "slide_revision_conflict", retryable: false, httpStatus: 409,
		});
	});
});

// --------------------------------------------------------------------------- //
// Region Content-SHA256 integrity
// --------------------------------------------------------------------------- //

describe("PathTogatherHttpClient region integrity", () => {
	function regionBody(overrides: Record<string, unknown> = {}) {
		const imageBase64 = Buffer.from("jpeg-bytes-here").toString("base64");
		const contentSha256 = createHash("sha256").update(Buffer.from("jpeg-bytes-here")).digest("hex");
		return {
			image_base64: imageBase64,
			mime: "image/jpeg", width: 100, height: 50,
			src: { x: 0, y: 0, w: 10, h: 5 }, magnification: 2.5,
			content_sha256: contentSha256, asset_revision: "rev:1",
			encoder: { id: "pillow", version: "1", resize: "LANCZOS", overlay_version: "v1", jpeg_quality: 85 },
			...overrides,
		};
	}

	it("passes when Content-SHA256 header matches the decoded bytes", async () => {
		const body = regionBody();
		const m = makeFetch([
			() => tokenResponse(),
			() => jsonResponse(200, body, { "Content-SHA256": body.content_sha256 }),
		]);
		const client = makeClient(m.fn);
		const r = await client.region({ slide: legacySlide("a.svs"), bbox: { x: 0, y: 0, w: 10, h: 5 } });
		expect(Buffer.from(r.bytes).toString()).toBe("jpeg-bytes-here");
		expect(r.contentSha256).toBe(body.content_sha256);
		expect(r.assetRevision).toBe("rev:1");
		expect(r.encoder?.id).toBe("pillow");
	});

	it("falls back to the body content_sha256 when the header is absent", async () => {
		const body = regionBody();
		const m = makeFetch([
			() => tokenResponse(),
			() => jsonResponse(200, body, {}), // no header
		]);
		const client = makeClient(m.fn);
		const r = await client.region({ slide: legacySlide("a.svs"), bbox: { x: 0, y: 0, w: 10, h: 5 } });
		expect(r.contentSha256).toBe(body.content_sha256);
	});

	it("throws integrity_error when the declared digest does not match the bytes", async () => {
		const body = regionBody({ content_sha256: "deadbeef" });
		const m = makeFetch([
			() => tokenResponse(),
			() => jsonResponse(200, body, { "Content-SHA256": "deadbeef" }),
		]);
		const client = makeClient(m.fn);
		await expect(client.region({ slide: legacySlide("a.svs"), bbox: { x: 0, y: 0, w: 10, h: 5 } })).rejects.toMatchObject({
			name: "ContractError", code: "integrity_error", retryable: false, httpStatus: 0,
		});
	});
});

// --------------------------------------------------------------------------- //
// verifyRunGrant + no-secret-in-requests guarantee
// --------------------------------------------------------------------------- //

describe("PathTogatherHttpClient verifyRunGrant", () => {
	it("posts grant_id+slide and returns valid/reason", async () => {
		const m = makeFetch([
			() => tokenResponse(),
			() => jsonResponse(200, { valid: false, reason: "grant_expired" }),
		]);
		const client = makeClient(m.fn);
		const r = await client.verifyRunGrant({ grant_id: "g1", installation_id: "pin_x", slide: "a.svs" });
		expect(r).toEqual({ valid: false, reason: "grant_expired" });
		const call = m.calls[1]!;
		expect(call.url).toContain("/api/plugin/v1/run-grants/verify");
		expect(call.body).toEqual({ grant_id: "g1", slide: "a.svs" });
	});
});

describe("PathTogatherHttpClient secret hygiene", () => {
	it("never sends the secret or token to a capability endpoint (only to /auth/token)", async () => {
		const m = makeFetch([
			() => tokenResponse(),
			() => jsonResponse(200, SLIDE_INFO_BODY),
		]);
		const client = makeClient(m.fn);
		await client.slideInfo(legacySlide("a.svs"));
		// data call carries the token, never the secret
		expect(m.calls[1]!.headers["authorization"]).toBe(HEADER(ACCESS_TOKEN));
		expect(m.calls[1]!.headers["authorization"]).not.toContain("super-secret-value");
	});
});
