/**
 * Platform Plugin Contract v0.1 — LegacyFlaskPlatformAdapter contract tests.
 *
 * Locks the Stage 1 adapter behavior required by §10 Stage 1 acceptance and the
 * §12.1 contract-test list:
 *   - base64 → {@link RegionResult.bytes} decode correctness;
 *   - snake_case wire → camelCase contract field normalization;
 *   - error envelope mapping (401/403/409/429/5xx → code + retryable, §7.7);
 *   - AbortSignal passthrough into the underlying engine;
 *   - 409 slide-revision conflict surfaces as a contract error;
 *   - declared-but-unused capabilities throw `capability_not_supported`.
 *
 * The adapter wraps a stub FlaskClient engine (no HTTP); the stub speaks the
 * legacy `/internal/ai/*` wire shape so these tests exercise the real
 * base64/normalization/error translation.
 */
import { describe, expect, it } from "vitest";
import { createHash } from "node:crypto";

import { FlaskHttpError, type FlaskClient } from "../src/flask-client.js";
import { LegacyFlaskPlatformAdapter } from "../src/platform/legacy-flask-adapter.js";
import {
	CAPABILITY_NOT_SUPPORTED,
	ContractError,
	bytesToBase64,
	legacySlide,
	type PlatformClient,
	type RegionResult,
} from "../src/platform/contract.js";

/** A stub FlaskClient engine that records calls and returns scripted payloads. */
interface StubFlask {
	flask: FlaskClient;
	regionArgs: Array<Record<string, unknown>>;
	annotateArgs: Array<Record<string, unknown>>;
	spotsArgs: Array<{ slide: string; afterSeq: number }>;
	slideInfoArgs: string[];
	setRegionResult: (r: Record<string, unknown>) => void;
	setRegionThrow: (e: unknown) => void;
	setSpotsThrow: (e: unknown) => void;
	setSlideInfoThrow: (e: unknown) => void;
}

function makeStubFlask(opts: {
	region?: Record<string, unknown>;
	spots?: { changes: Record<string, unknown>[]; current_seq: number };
	slideInfo?: Record<string, unknown>;
} = {}): StubFlask {
	const state: {
		region: Record<string, unknown>;
		spots: { changes: Record<string, unknown>[]; current_seq: number };
		slideInfo: Record<string, unknown>;
		regionThrow?: unknown;
		spotsThrow?: unknown;
		slideInfoThrow?: unknown;
	} = {
		region: opts.region ?? {
			image_base64: "QUFBQQ==", // "AAAA"
			mime: "image/jpeg",
			width: 1024,
			height: 768,
			src: { x: 10, y: 20, w: 1000, h: 500 },
			magnification: 2.5,
			encoder: { id: "pillow", version: "9.5.0", resize: "LANCZOS", overlay_version: "coordinate-ticks-v1", jpeg_quality: 85 },
		},
		spots: opts.spots ?? { changes: [{ annotation_id: "a1", change_seq: 3, side_px: 100, x: 1, y: 2 }], current_seq: 3 },
		slideInfo: opts.slideInfo ?? { width: 10000, height: 8000, level_downsamples: [1, 2, 4], mpp: 0.5, fingerprint: "mtime:size" },
	};
	const regionArgs: Array<Record<string, unknown>> = [];
	const annotateArgs: Array<Record<string, unknown>> = [];
	const spotsArgs: Array<{ slide: string; afterSeq: number }> = [];
	const slideInfoArgs: string[] = [];
	const flask = {
		async region(args: Record<string, unknown>) {
			regionArgs.push(args);
			if (state.regionThrow) throw state.regionThrow;
			return { ...state.region };
		},
		async annotate(args: Record<string, unknown>) {
			annotateArgs.push(args);
			return { annotation_id: "ann-1", index: 0, token: "admin", slide: args.slide, label: args.label, note: args.note ?? "", type: "rect", x: args.x, y: args.y, side_px: args.side_px, size_mm: 0, shared: false, source: "ai", created_by_session_id: "", change_seq: 1, revision: 1 };
		},
		async spots(slide: string, afterSeq: number) {
			spotsArgs.push({ slide, afterSeq });
			if (state.spotsThrow) throw state.spotsThrow;
			return { changes: [...state.spots.changes], current_seq: state.spots.current_seq };
		},
		async slideInfo(slide: string) {
			slideInfoArgs.push(slide);
			if (state.slideInfoThrow) throw state.slideInfoThrow;
			return { ...state.slideInfo };
		},
	};
	return {
		flask: flask as unknown as FlaskClient,
		regionArgs,
		annotateArgs,
		spotsArgs,
		slideInfoArgs,
		setRegionResult: (r) => { state.region = r; },
		setRegionThrow: (e) => { state.regionThrow = e; },
		setSpotsThrow: (e) => { state.spotsThrow = e; },
		setSlideInfoThrow: (e) => { state.slideInfoThrow = e; },
	};
}

function makeAdapter(stub: StubFlask): PlatformClient {
	return new LegacyFlaskPlatformAdapter({ flask: stub.flask });
}

describe("LegacyFlaskPlatformAdapter", () => {
	describe("region: base64 → bytes decode + field normalization (§6.1/§7.3)", () => {
		it("decodes image_base64 into RegionResult.bytes (Uint8Array) and round-trips", async () => {
			const stub = makeStubFlask();
			const adapter = makeAdapter(stub);
			const r = await adapter.region({ slide: legacySlide("s.svs"), bbox: { x: 10, y: 20, w: 1000, h: 500 }, maxLongEdge: 1024 });
			expect(Buffer.isBuffer(r.bytes) || r.bytes instanceof Uint8Array).toBe(true);
			// The decoded bytes re-encode to the original base64 (adapter is the
			// sole base64 touch; bytesToBase64 is the pi boundary).
			expect(bytesToBase64(r.bytes)).toBe("QUFBQQ==");
			expect(r.mimeType).toBe("image/jpeg");
		});

		it("normalizes the snake_case encoder descriptor to camelCase", async () => {
			const stub = makeStubFlask();
			const adapter = makeAdapter(stub);
			const r: RegionResult = await adapter.region({ slide: legacySlide("s.svs"), bbox: { x: 0, y: 0, w: 10, h: 10 } });
			expect(r.encoder).toEqual({
				id: "pillow",
				version: "9.5.0",
				resize: "LANCZOS",
				overlayVersion: "coordinate-ticks-v1",
				jpegQuality: 85,
			});
		});

		it("computes contentSha256 from the decoded JPEG bytes (§6.1)", async () => {
			const stub = makeStubFlask({ region: { image_base64: "QkFBQkE=", mime: "image/jpeg", width: 2, height: 2, src: { x: 0, y: 0, w: 1, h: 1 }, magnification: 1 } });
			const adapter = makeAdapter(stub);
			const r = await adapter.region({ slide: legacySlide("s.svs"), bbox: { x: 0, y: 0, w: 1, h: 1 } });
			const expected = createHash("sha256").update(Buffer.from("QkFBQkE=", "base64")).digest("hex");
			expect(r.contentSha256).toBe(expected);
		});

		it("leaves assetRevision undefined (legacy region response carries none)", async () => {
			const stub = makeStubFlask();
			const adapter = makeAdapter(stub);
			const r = await adapter.region({ slide: legacySlide("s.svs"), bbox: { x: 0, y: 0, w: 1, h: 1 } });
			expect(r.assetRevision).toBeUndefined();
		});

		it("echoes width/height/src/magnification unchanged", async () => {
			const stub = makeStubFlask();
			const adapter = makeAdapter(stub);
			const r = await adapter.region({ slide: legacySlide("s.svs"), bbox: { x: 10, y: 20, w: 1000, h: 500 } });
			expect(r.width).toBe(1024);
			expect(r.height).toBe(768);
			expect(r.src).toEqual({ x: 10, y: 20, w: 1000, h: 500 });
			expect(r.magnification).toBe(2.5);
		});

		it("decodes an empty payload to zero-length bytes (not a throw)", async () => {
			const stub = makeStubFlask({ region: { image_base64: "", mime: "image/jpeg", width: 1, height: 1, src: { x: 0, y: 0, w: 1, h: 1 }, magnification: 1 } });
			const adapter = makeAdapter(stub);
			const r = await adapter.region({ slide: legacySlide("s.svs"), bbox: { x: 0, y: 0, w: 1, h: 1 } });
			expect(r.bytes.length).toBe(0);
		});
	});

	describe("slideInfo / spots / annotate normalization (§7.2)", () => {
		it("slideInfo normalizes level_downsamples → levelDownsamples and fingerprint → assetRevision", async () => {
			const stub = makeStubFlask();
			const adapter = makeAdapter(stub);
			const d = await adapter.slideInfo(legacySlide("s.svs"));
			expect(d).toEqual({ width: 10000, height: 8000, levelDownsamples: [1, 2, 4], mpp: 0.5, assetRevision: "mtime:size" });
			expect(stub.slideInfoArgs).toEqual(["s.svs"]);
		});

		it("spots normalizes current_seq → currentSeq and forwards the cursor", async () => {
			const stub = makeStubFlask({ spots: { changes: [{ annotation_id: "a1", change_seq: 5 }], current_seq: 5 } });
			const adapter = makeAdapter(stub);
			const page = await adapter.spots(legacySlide("s.svs"), 2);
			expect(page.currentSeq).toBe(5);
			expect(page.changes).toHaveLength(1);
			expect(stub.spotsArgs).toEqual([{ slide: "s.svs", afterSeq: 2 }]);
		});

		it("annotate maps camelCase request fields to the legacy wire body", async () => {
			const stub = makeStubFlask();
			const adapter = makeAdapter(stub);
			const roi = await adapter.annotate({ slide: legacySlide("s.svs"), label: "L", x: 1, y: 2, sidePx: 30, note: "n", effectKey: "ek", sessionId: "sid" });
			expect(roi.annotation_id).toBe("ann-1");
			expect(stub.annotateArgs[0]).toMatchObject({ slide: "s.svs", label: "L", x: 1, y: 2, side_px: 30, note: "n", effect_key: "ek", session_id: "sid" });
		});

		it("unwraps a legacy-filename SlideRef for every capability", async () => {
			const stub = makeStubFlask();
			const adapter = makeAdapter(stub);
			await adapter.slideInfo(legacySlide("leg.svs"));
			await adapter.spots(legacySlide("leg.svs"), 0);
			await adapter.region({ slide: legacySlide("leg.svs"), bbox: { x: 0, y: 0, w: 1, h: 1 } });
			expect(stub.slideInfoArgs[0]).toBe("leg.svs");
			expect(stub.spotsArgs[0]?.slide).toBe("leg.svs");
			expect(stub.regionArgs[0]?.slide).toBe("leg.svs");
		});
	});

	describe("AbortSignal passthrough (§7.3)", () => {
		it("forwards the AbortSignal into the underlying engine.region call", async () => {
			const stub = makeStubFlask();
			const adapter = makeAdapter(stub);
			const ac = new AbortController();
			await adapter.region({ slide: legacySlide("s.svs"), bbox: { x: 0, y: 0, w: 1, h: 1 } }, ac.signal);
			expect(stub.regionArgs[0]?.signal).toBe(ac.signal);
		});

		it("does not require a signal (undefined is passed through as undefined)", async () => {
			const stub = makeStubFlask();
			const adapter = makeAdapter(stub);
			await adapter.region({ slide: legacySlide("s.svs"), bbox: { x: 0, y: 0, w: 1, h: 1 } });
			expect(stub.regionArgs[0]?.signal).toBeUndefined();
		});
	});

	describe("error envelope mapping (§7.7)", () => {
		it("maps 409 fingerprint mismatch → slide_revision_conflict (not retryable)", async () => {
			const stub = makeStubFlask();
			stub.setRegionThrow(new FlaskHttpError(409, { error: { message: "切片指纹不匹配" } }));
			const adapter = makeAdapter(stub);
			await expect(adapter.region({ slide: legacySlide("s.svs"), bbox: { x: 0, y: 0, w: 1, h: 1 } })).rejects.toMatchObject({
				name: "ContractError",
				code: "slide_revision_conflict",
				retryable: false,
				httpStatus: 409,
			});
		});

		it("maps 401 → token_expired (retryable) and 403 → permission_denied (not retryable)", async () => {
			for (const [status, code, retryable] of [[401, "token_expired", true], [403, "permission_denied", false]] as const) {
				const stub = makeStubFlask();
				stub.setSlideInfoThrow(new FlaskHttpError(status, "nope"));
				const adapter = makeAdapter(stub);
				await expect(adapter.slideInfo(legacySlide("s.svs"))).rejects.toMatchObject({ name: "ContractError", code, retryable, httpStatus: status });
			}
		});

		it("maps 429 → rate_limited (retryable) and 500 → service_unavailable (retryable)", async () => {
			for (const [status, code] of [[429, "rate_limited"], [500, "service_unavailable"], [503, "service_unavailable"]] as const) {
				const stub = makeStubFlask();
				stub.setSpotsThrow(new FlaskHttpError(status, "err"));
				const adapter = makeAdapter(stub);
				await expect(adapter.spots(legacySlide("s.svs"), 0)).rejects.toMatchObject({ name: "ContractError", code, retryable: true, httpStatus: status });
			}
		});

		it("maps 400/404/410 to the documented non-retryable codes", async () => {
			const cases: Array<[number, string]> = [[400, "invalid_request"], [404, "slide_not_found"], [410, "cursor_expired"]];
			for (const [status, code] of cases) {
				const stub = makeStubFlask();
				stub.setSlideInfoThrow(new FlaskHttpError(status, "err"));
				const adapter = makeAdapter(stub);
				await expect(adapter.slideInfo(legacySlide("s.svs"))).rejects.toMatchObject({ name: "ContractError", code, retryable: false, httpStatus: status });
			}
		});

		it("produces a real ContractError instance (instanceof + stable code)", async () => {
			const stub = makeStubFlask();
			stub.setRegionThrow(new FlaskHttpError(409, "x"));
			const adapter = makeAdapter(stub);
			await expect(adapter.region({ slide: legacySlide("s.svs"), bbox: { x: 0, y: 0, w: 1, h: 1 } })).rejects.toBeInstanceOf(ContractError);
		});

		it("passes non-Flask errors through unchanged (e.g. AbortError)", async () => {
			const stub = makeStubFlask();
			const abortErr = new Error("The operation was aborted");
			abortErr.name = "AbortError";
			stub.setRegionThrow(abortErr);
			const adapter = makeAdapter(stub);
			await expect(adapter.region({ slide: legacySlide("s.svs"), bbox: { x: 0, y: 0, w: 1, h: 1 } })).rejects.toBe(abortErr);
		});
	});

	describe("declared-but-unused capabilities (§7.2)", () => {
		it("updateAnnotation / deleteAnnotation / appendAuditEvent throw capability_not_supported", async () => {
			const adapter = makeAdapter(makeStubFlask());
			await expect(adapter.updateAnnotation({ annotationId: "a", revision: 1, patch: {} })).rejects.toMatchObject({ code: CAPABILITY_NOT_SUPPORTED });
			await expect(adapter.deleteAnnotation({ annotationId: "a", revision: 1 })).rejects.toMatchObject({ code: CAPABILITY_NOT_SUPPORTED });
			await expect(adapter.appendAuditEvent({ pluginId: "p", pluginVersion: "1", action: "x" })).rejects.toMatchObject({ code: CAPABILITY_NOT_SUPPORTED });
		});

		it("openEventStream throws capability_not_supported synchronously", () => {
			const adapter = makeAdapter(makeStubFlask());
			expect(() => adapter.openEventStream({ slide: legacySlide("s.svs") })).toThrow(ContractError);
		});
	});

	describe("SlideRef resolution (§4.4)", () => {
		it("legacyFilename resolves a legacy ref and a stable-id ref surfaces as slide_not_found", async () => {
			// Covered indirectly above; here we assert the helper contract directly
			// to prevent regressions in the discriminated-union unwrap.
			const adapter = makeAdapter(makeStubFlask());
			await expect(adapter.slideInfo({ kind: "slide-id", slideId: "sld_abc" })).rejects.toMatchObject({ code: "slide_not_found" });
		});
	});
});
