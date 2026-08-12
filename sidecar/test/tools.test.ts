import { afterAll, beforeAll, beforeEach, describe, expect, it } from "vitest";
import { promises as fs } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { SessionStore } from "../src/session-store.js";
import { createTools, AgentState, magnificationGuide, type ToolContext } from "../src/tools.js";
import type { CreateAnnotationRequest, PlatformClient, RegionRequest, RegionResult, RoiDict, SlideDescriptor } from "../src/platform/contract.js";

// --------------------------------------------------------------------------- //
// Test fixtures
// --------------------------------------------------------------------------- //

let rootTmp = "";
beforeAll(async () => {
	rootTmp = await fs.mkdtemp(join(tmpdir(), "svs-tools-"));
});
afterAll(async () => {
	await fs.rm(rootTmp, { recursive: true, force: true });
});

// A pretend pyramid: 4 levels, downsamples 1/2/4/8. mpp 0.5 → level-0 = 20x.
const DOWNSAMPLES = [1, 2, 4, 8];
const MPP = 0.5;
const SLIDE = "test.svs";
const SLIDE_W = 10000;
const SLIDE_H = 8000;
const FINGERPRINT = "fp-1234:9999";

const slideInfo = {
	width: SLIDE_W,
	height: SLIDE_H,
	levelDownsamples: DOWNSAMPLES,
	mpp: MPP,
	fingerprint: FINGERPRINT,
};

/** In-memory PlatformClient mock: records calls, returns canned data. */
interface MockFlask {
	regionCalls: Array<{ x: number; y: number; w: number; h: number; outW?: number; outH?: number; maxLongEdge?: number; expectedAssetRevision?: string }>;
	annotateCalls: Array<{ label: string; x: number; y: number; sidePx: number; note?: string; effectKey?: string; sessionId?: string }>;
	annotateResult: RoiDict;
	regionResult?: Partial<RegionResult>;
	regionError?: Error;
}
function makeMockFlask(): MockFlask & Pick<PlatformClient, "region" | "annotate" | "spots" | "slideInfo"> {
	const state: MockFlask = {
		regionCalls: [],
		annotateCalls: [],
		annotateResult: {
			annotation_id: "ann-uuid-1",
			index: 0,
			token: "admin",
			slide: SLIDE,
			label: "",
			note: "",
			type: "rect",
			x: 0,
			y: 0,
			side_px: 0,
			size_mm: 0,
			shared: false,
			source: "ai",
			created_by_session_id: "",
			change_seq: 1,
			revision: 1,
		},
	};
	// Live region result override: reading/writing regionResult on the
	// returned object must be visible to the closures, so route it through a
	// getter/setter on the shared `state`.
	const obj = {
		...state,
		async region(request: RegionRequest) {
			state.regionCalls.push({
				x: request.bbox.x,
				y: request.bbox.y,
				w: request.bbox.w,
				h: request.bbox.h,
				outW: request.outW,
				outH: request.outH,
				maxLongEdge: request.maxLongEdge,
				expectedAssetRevision: request.expectedAssetRevision,
			});
			if (state.regionError) throw state.regionError;
			// Echo width/height: aspect-preserving when maxLongEdge is set.
			let ow = state.regionResult?.width ?? request.outW ?? 1024;
			let oh = state.regionResult?.height ?? request.outH ?? 1024;
			if (request.maxLongEdge && !state.regionResult?.width) {
				const longest = Math.max(request.bbox.w, request.bbox.h);
				const scale = request.maxLongEdge / longest;
				ow = Math.max(1, Math.round(request.bbox.w * scale));
				oh = Math.max(1, Math.round(request.bbox.h * scale));
			}
			const bytes: Uint8Array = state.regionResult?.bytes ?? Buffer.from("AAAA", "base64");
			const r: RegionResult = {
				bytes,
				mimeType: state.regionResult?.mimeType ?? "image/jpeg",
				width: ow,
				height: oh,
				src: state.regionResult?.src ?? { x: request.bbox.x, y: request.bbox.y, w: request.bbox.w, h: request.bbox.h },
				magnification: state.regionResult?.magnification ?? 20,
				contentSha256: state.regionResult?.contentSha256 ?? "",
				assetRevision: state.regionResult?.assetRevision,
				encoder: state.regionResult?.encoder,
			};
			return r;
		},
		async annotate(args: CreateAnnotationRequest) {
			state.annotateCalls.push({ label: args.label, x: args.x, y: args.y, sidePx: args.sidePx, note: args.note, effectKey: args.effectKey, sessionId: args.sessionId });
			return { ...state.annotateResult, label: args.label, x: args.x, y: args.y, side_px: args.sidePx, note: args.note ?? "" };
		},
		async spots(_ref: unknown, _afterSeq: number) {
			return { changes: [], currentSeq: 1 };
		},
		async slideInfo(_ref: unknown): Promise<SlideDescriptor> {
			return { width: slideInfo.width, height: slideInfo.height, levelDownsamples: [...slideInfo.levelDownsamples], mpp: slideInfo.mpp, assetRevision: slideInfo.fingerprint };
		},
	};
	// Route regionResult access through state so test overrides are visible.
	Object.defineProperty(obj, "regionResult", {
		get() {
			return state.regionResult;
		},
		set(v: Partial<RegionResult> | undefined) {
			state.regionResult = v;
		},
	});
	Object.defineProperty(obj, "regionError", {
		get() {
			return state.regionError;
		},
		set(v: Error | undefined) {
			state.regionError = v;
		},
	});
	return obj as MockFlask & Pick<PlatformClient, "region" | "annotate" | "spots" | "slideInfo">;
}

async function newStore(): Promise<{ store: SessionStore; dir: string }> {
	const dir = join(rootTmp, `d${Math.random().toString(36).slice(2)}`);
	await fs.mkdir(dir, { recursive: true });
	const store = new SessionStore({ sessionsDir: dir });
	return { store, dir };
}

interface Harness {
	ctx: ToolContext;
	store: SessionStore;
	sessionId: string;
	mock: MockFlask;
	events: Array<{ type: string; payload: Record<string, unknown> }>;
}

async function makeHarness(kind: "main" | "fork" = "main"): Promise<Harness> {
	const { store } = await newStore();
	const data = await store.createSession({ slide: SLIDE, kind });
	const events: Array<{ type: string; payload: Record<string, unknown> }> = [];
	const mock = makeMockFlask();
	const ctx: ToolContext = {
		sessionStore: store,
		sessionId: data.id,
		kind,
		slide: SLIDE,
		slideInfo,
		flask: mock as unknown as PlatformClient,
		emit: (type, payload) => {
			events.push({ type, payload });
		},
		cfg: {},
	};
	return { ctx, store, sessionId: data.id, mock, events };
}

/** Find a tool by name in the array returned by createTools. */
function tool(h: Harness, name: string) {
	const tools = createTools(h.ctx);
	const t = tools.find((t) => t.name === name);
	if (!t) throw new Error(`tool ${name} not found`);
	return t;
}

/** Helper: extract the single text string from a tool result (asserts text-only). */
function resultText(r: { content: Array<{ type: string; text?: string }> }): string {
	if (r.content.length === 0) return "";
	const t = r.content[0];
	if (t?.type !== "text" || typeof t.text !== "string") throw new Error("expected text content");
	return t.text;
}

// --------------------------------------------------------------------------- //
// Tests
// --------------------------------------------------------------------------- //

describe("AgentState helpers (port from ai_agent.py)", () => {
	it("viewportBbox scales the covered span by the level's downsample", () => {
		const st = new AgentState(1000, 2000, 1024, 0, MPP);
		// level 0 ds=1 → side = 1024
		expect(st.viewportBbox(DOWNSAMPLES)).toEqual({ x: 488, y: 1488, w: 1024, h: 1024 });
		// level 2 ds=4 → side = 4096
		st.pyramidLevel = 2;
		expect(st.viewportBbox(DOWNSAMPLES)).toEqual({ x: -1048, y: -48, w: 4096, h: 4096 });
	});

	it("viewportBbox clamps the box into bounds when slideDims is given", () => {
		// Center near the top-left on a small slide: without clamping the box
		// origin goes negative and the server /internal/ai/region 400s, making
		// every snapshot fail (Phase 4 gemini smoke: center (1000,800) at level 1
		// on a 4000x3000 slide → x=-24,y=-224 without the clamp).
		const dims = { width: 4000, height: 3000 };
		const st = new AgentState(1000, 800, 1024, 1, MPP);
		// level 1 ds=2 → side 2048; raw x=-24,y=-224 → clamped to x=0,y=0.
		expect(st.viewportBbox(DOWNSAMPLES, dims)).toEqual({ x: 0, y: 0, w: 2048, h: 2048 });
		// Center near the far edge: box shifted back so it ends at the slide edge.
		const st2 = new AgentState(3900, 2900, 1024, 1, MPP);
		expect(st2.viewportBbox(DOWNSAMPLES, dims)).toEqual({ x: 1952, y: 952, w: 2048, h: 2048 });
		// Slide smaller than the covered side on an axis → cover the whole axis.
		const st3 = new AgentState(500, 500, 1024, 2, MPP); // side 4096 > 4000/3000
		expect(st3.viewportBbox(DOWNSAMPLES, dims)).toEqual({ x: 0, y: 0, w: 4096, h: 4096 });
		// Without slideDims the legacy unclamped behaviour is preserved.
		expect(st.viewportBbox(DOWNSAMPLES)).toEqual({ x: -24, y: -224, w: 2048, h: 2048 });
	});

	it("effectiveLevel clamps out-of-range levels", () => {
		const st = new AgentState(0, 0, 1024, 99, MPP);
		expect(st.effectiveLevel(DOWNSAMPLES)).toBe(3);
		st.pyramidLevel = -5;
		expect(st.effectiveLevel(DOWNSAMPLES)).toBe(0);
	});

	it("magnificationLabel uses base=10/mpp and tiers", () => {
		const st = new AgentState(0, 0, 1024, 0, MPP);
		// base = 10/0.5 = 20x, level 0 ds 1 → 20x 中低倍
		expect(st.magnificationLabel(DOWNSAMPLES)).toBe("20x（中低倍，level=0）");
		// level 3 ds 8 → 2.5x 全片概览
		st.pyramidLevel = 3;
		expect(st.magnificationLabel(DOWNSAMPLES)).toBe("3x（全片概览，level=3）");
	});

	it("magnificationLabel falls back to a level label when mpp is unknown", () => {
		const st = new AgentState(0, 0, 1024, 2, null);
		expect(st.magnificationLabel(DOWNSAMPLES)).toBe("level=2（level 越大倍率越低）");
	});

	it("pickOverviewLevel returns the lowest level that 95%-covers the slide", () => {
		// viewport 1024, slide 10000 → need 1024*ds >= 9500 → ds>=9.28 → none of 1/2/4/8 → last level 3
		expect(AgentState.pickOverviewLevel(10000, 8000, DOWNSAMPLES, 1024)).toBe(3);
		// need 3800: level 2 ds 4 → 1024*4=4096 >= 3800 → level 2
		expect(AgentState.pickOverviewLevel(4000, 4000, [1, 2, 4, 8, 16], 1024)).toBe(2);
		// exact: 1024*4=4096 >= 3800 → level 2
		expect(AgentState.pickOverviewLevel(4000, 4000, [1, 2, 4, 8], 1024)).toBe(2);
	});

	it("magnificationGuide emits the per-level table", () => {
		const g = magnificationGuide({ level_downsamples: DOWNSAMPLES, mpp: MPP });
		expect(g).toContain("level 0≈20x");
		expect(g).toContain("约 20x 只算中低倍");
	});

	it("toDict/fromDict round-trips", () => {
		const st = new AgentState(123.4, 567.8, 2048, 2, MPP);
		const d = st.toDict();
		expect(d).toEqual({ center_x: 123.4, center_y: 567.8, pyramid_level: 2, viewport_px: 2048 });
		const back = AgentState.fromDict(d, MPP);
		expect(back.centerX).toBe(123.4);
		expect(back.pyramidLevel).toBe(2);
		expect(back.mpp).toBe(MPP);
	});
});

describe("goto (ai_agent.py L753-814)", () => {
	it("moves and emits tool_started with the right payload", async () => {
		const h = await makeHarness();
		// seed initial state at center (0,0) level 0
		const t = tool(h, "goto");
		const r = await t.execute("tc1", { x: 500, y: 600, level: 1, reason: "看这里" });
		expect(resultText(r)).toBe("已移动到 (500,600)，当前 10x（低倍，level=1）。");
		// base 20 / ds 2 = 10x → 低倍
		expect(h.events).toHaveLength(1);
		expect(h.events[0]?.type).toBe("tool_started");
		const p = h.events[0]?.payload as Record<string, unknown>;
		expect(p).toMatchObject({ tool: "goto", x: 500, y: 600, level: 1, reason: "看这里", requested_level: 1 });
		expect(p.magnification).toBe("10x（低倍，level=1）");
		// state persisted
		const data = await h.store.readSession(h.sessionId);
		expect(data?.agent_state).toEqual({ center_x: 500, center_y: 600, pyramid_level: 1, viewport_px: 1024 });
	});

	it("clamps ±2 levels per goto and tells the model", async () => {
		const h = await makeHarness();
		// start at level 0
		await tool(h, "goto").execute("tc1", { x: 0, y: 0, level: 0 });
		h.events.length = 0;
		// request level 5 → only +2 allowed → clamped to level 2 (within range)
		const r = await tool(h, "goto").execute("tc2", { x: 0, y: 0, level: 5 });
		const txt = resultText(r);
		expect(txt).toContain("已移动到 (0,0)");
		expect(txt).toContain("单次 goto 最多变 2 层（当前 level=0）");
		expect(txt).toContain("请求 level=5 已夹到 2");
		expect(txt).toContain("当前 5x（低倍，level=2）"); // 20/4=5x
	});

	it("clamps both step and range when request is wildly out of range", async () => {
		const h = await makeHarness();
		await tool(h, "goto").execute("tc1", { x: 0, y: 0, level: 0 });
		h.events.length = 0;
		// request level 99 → step-clamp to 2 (still in range here, but test the note path)
		const r = await tool(h, "goto").execute("tc2", { x: 10, y: 10, level: 99 });
		const txt = resultText(r);
		expect(txt).toContain("请求 level=99 已夹到有效层 2");
	});

	it("detects no-op and refuses a duplicate goto", async () => {
		const h = await makeHarness();
		await tool(h, "goto").execute("tc1", { x: 100, y: 100, level: 1 });
		const r = await tool(h, "goto").execute("tc2", { x: 100, y: 100, level: 1 });
		const txt = resultText(r);
		expect(txt).toContain("已在目标位置");
		expect(txt).toContain("不要重复相同的 goto");
		// no tool_started emitted for the no-op
		const started = h.events.filter((e) => e.type === "tool_started");
		expect(started).toHaveLength(1);
	});

	it("blocks goto while a snapshot is pending", async () => {
		const h = await makeHarness();
		await tool(h, "goto").execute("tc1", { x: 0, y: 0, level: 0 });
		await tool(h, "snapshot").execute("snap1", {});
		const r = await tool(h, "goto").execute("tc2", { x: 50, y: 50, level: 1 });
		expect(resultText(r)).toContain("需先消化当前快照");
	});
});

describe("snapshot (ai_agent.py L816-875)", () => {
	it("rejects a second snapshot while one is pending", async () => {
		const h = await makeHarness();
		await tool(h, "goto").execute("tc1", { x: 0, y: 0, level: 0 });
		await tool(h, "snapshot").execute("snap1", {});
		const r = await tool(h, "snapshot").execute("snap2", {});
		expect(resultText(r)).toBe("需先消化当前快照后再抓新快照。");
	});

	it("clamps out_w/out_h to 64..detail_image_long_edge (default 1280)", async () => {
		const h = await makeHarness();
		await tool(h, "goto").execute("tc1", { x: 5000, y: 4000, level: 0 });
		const r = await tool(h, "snapshot").execute("snap1", { out_w: 8, out_h: 99999 });
		expect(r.content).toHaveLength(2);
		expect(h.mock.regionCalls[0]?.outW).toBe(64);
		expect(h.mock.regionCalls[0]?.outH).toBe(1280); // detail-tier cap
		// default when omitted is viewportPx (1024)
		const r2 = await tool(h, "complete_snapshot_review").execute("csr1", { disposition: "no_annotation", no_annotation_reason: "导航确认" });
		expect(resultText(r2)).toContain("已关闭快照");
		const snap2 = await tool(h, "snapshot").execute("snap2", {});
		expect(h.mock.regionCalls[1]?.outW).toBe(1024);
		expect(h.mock.regionCalls[1]?.outH).toBe(1024);
	});

	it("passes expected_fingerprint from slideInfo to flask.region", async () => {
		const h = await makeHarness();
		await tool(h, "goto").execute("tc1", { x: 5000, y: 4000, level: 0 });
		await tool(h, "snapshot").execute("snap1", {});
		expect(h.mock.regionCalls[0]).toMatchObject({ expectedAssetRevision: FINGERPRINT });
	});

	it("uses max_long_edge when provided (aspect-preserving, §6.1)", async () => {
		const h = await makeHarness();
		await tool(h, "goto").execute("tc1", { x: 5000, y: 4000, level: 0 });
		await tool(h, "snapshot").execute("snap1", { max_long_edge: 1024 });
		const call = h.mock.regionCalls[0]!;
		expect(call.maxLongEdge).toBe(1024);
		// out_w/out_h should not be sent when max_long_edge is used.
		expect(call.outW).toBeUndefined();
		expect(call.outH).toBeUndefined();
	});

	it("clamps max_long_edge to the detail-tier cap (default 1280)", async () => {
		const h = await makeHarness();
		await tool(h, "goto").execute("tc1", { x: 5000, y: 4000, level: 0 });
		await tool(h, "snapshot").execute("snap1", { max_long_edge: 99999 });
		expect(h.mock.regionCalls[0]?.maxLongEdge).toBe(1280);
	});

	it("honors cfg.detail_image_long_edge as the snapshot output cap", async () => {
		const h = await makeHarness();
		h.ctx.cfg.detail_image_long_edge = 900;
		await tool(h, "goto").execute("tc1", { x: 5000, y: 4000, level: 0 });
		await tool(h, "snapshot").execute("snap1", { max_long_edge: 4096 });
		expect(h.mock.regionCalls[0]?.maxLongEdge).toBe(900);
	});

	it("on 409 fingerprint mismatch clears caches via onFingerprintMismatch", async () => {
		const h = await makeHarness();
		let cleared = 0;
		h.ctx.onFingerprintMismatch = () => {
			cleared += 1;
		};
		// The platform surfaces a slide-revision conflict as a ContractError
		// (§7.7). The legacy adapter maps Flask's 409 to this code; in tests the
		// mock IS the platform, so it throws the contract error directly.
		const { ContractError } = await import("../src/platform/contract.js");
		h.mock.regionError = new ContractError({
			code: "slide_revision_conflict",
			message: "切片指纹不匹配（文件已变更）",
			retryable: false,
			httpStatus: 409,
		});
		await tool(h, "goto").execute("tc1", { x: 5000, y: 4000, level: 0 });
		const r = await tool(h, "snapshot").execute("snap1", {});
		expect(resultText(r)).toContain("切片文件已变更");
		expect(cleared).toBe(1);
	});

	it("emits snapshot_captured and sets pending_snapshot_review", async () => {
		const h = await makeHarness();
		await tool(h, "goto").execute("tc1", { x: 5000, y: 4000, level: 0 });
		await tool(h, "snapshot").execute("snap1", {});
		const ev = h.events.find((e) => e.type === "snapshot_captured");
		expect(ev).toBeTruthy();
		const p = ev?.payload as Record<string, unknown>;
		expect(p).toHaveProperty("bboxLevel0");
		expect(p).toHaveProperty("magnification");
		expect(p).toHaveProperty("out_w");
		expect(p).toHaveProperty("out_h");
		const data = await h.store.readSession(h.sessionId);
		expect(data?.pending_snapshot_review?.snapshot_id).toBe("snap1");
	});

	it("text includes coverage-tier hint for an overview-level bbox", async () => {
		const h = await makeHarness();
		// viewport 1024 at level 3 (ds 8) → side 8192 ≈ 82% of 10000 → low-power tier
		await tool(h, "goto").execute("tc1", { x: 5000, y: 4000, level: 3 });
		// but region mock echoes the requested w; force a wide bbox to hit overview tier
		h.mock.regionResult = { src: { x: 0, y: 0, w: 9500, h: 8000 } };
		const r = await tool(h, "snapshot").execute("snap1", {});
		const txt = (r.content[0] as { text: string }).text;
		expect(txt).toContain("覆盖全片约 95.0%");
		expect(txt).toContain("全片概览级");
	});
});

describe("mark_observation (ai_agent.py L877-904)", () => {
	it("refuses when no snapshot is pending", async () => {
		const h = await makeHarness();
		const r = await tool(h, "mark_observation").execute("mo1", { label: "L", note: "n" });
		expect(resultText(r)).toBe("当前没有待消化的快照；请先 snapshot。");
	});

	it("writes an observation and emits the observation event", async () => {
		const h = await makeHarness();
		await tool(h, "goto").execute("tc1", { x: 0, y: 0, level: 0 });
		await tool(h, "snapshot").execute("snap1", {});
		const r = await tool(h, "mark_observation").execute("mo1", { label: "可疑灶", note: "紫染密集", x: 10, y: 20, w: 30, h: 40 });
		expect(resultText(r)).toBe("已记录观察：可疑灶");
		const data = await h.store.readSession(h.sessionId);
		expect(data?.observations).toHaveLength(1);
		expect(data?.observations[0]).toMatchObject({ label: "可疑灶", note: "紫染密集", snapshot_id: "snap1" });
		const ev = h.events.find((e) => e.type === "observation");
		expect(ev?.payload).toMatchObject({ label: "可疑灶", note: "紫染密集", no_annotation_reason: "" });
		expect((ev?.payload as { bbox: unknown }).bbox).toEqual({ x: 10, y: 20, w: 30, h: 40 });
	});
});

describe("create_annotation (ai_agent.py L906-934)", () => {
	it("is omitted from the tool set in fork sessions", () => {
		// tools_for_kind("fork") omits the schema entirely (ai_agent.py:309);
		// the in-execute fork guard at L907 is defense-in-depth.
		const tools = createTools({
			sessionStore: {} as SessionStore,
			sessionId: "fork-sess",
			kind: "fork",
			slide: SLIDE,
			slideInfo,
			flask: {} as unknown as PlatformClient,
			emit: () => undefined,
			cfg: {},
		});
		expect(tools.find((t) => t.name === "create_annotation")).toBeUndefined();
	});

	it("the fork guard refuses if invoked directly with kind=fork", async () => {
		// Build a main tool set, capture the create_annotation instance, then
		// flip ctx.kind to fork to exercise the in-execute guard
		// (ai_agent.py:907) directly.
		const h = await makeHarness("main");
		await tool(h, "goto").execute("tc1", { x: 0, y: 0, level: 0 });
		await tool(h, "snapshot").execute("snap1", {});
		const createAnnotation = tool(h, "create_annotation");
		h.ctx.kind = "fork";
		const r = await createAnnotation.execute("ca1", { label: "L", x: 1, y: 2, side_px: 10 });
		expect(resultText(r)).toBe("fork 会话不允许 create_annotation（批注只做问答，不改标注库）。");
	});

	it("refuses when no snapshot is pending", async () => {
		const h = await makeHarness();
		const r = await tool(h, "create_annotation").execute("ca1", { label: "L", x: 1, y: 2, side_px: 10 });
		expect(resultText(r)).toBe("当前没有待消化的快照；请先 snapshot。");
	});

	it("passes a deterministic effect_key to flask.annotate and emits annotation_created", async () => {
		const h = await makeHarness();
		await tool(h, "goto").execute("tc1", { x: 0, y: 0, level: 0 });
		await tool(h, "snapshot").execute("snap1", {});
		const r = await tool(h, "create_annotation").execute("ca1", { label: "肿瘤灶", x: 100, y: 200, side_px: 500, note: "高密度" });
		const txt = resultText(r);
		expect(txt).toContain("已落标注「肿瘤灶」");
		expect(txt).toContain("(100,200)");
		expect(txt).toContain("边长 500 像素");
		expect(txt).toContain("中心 (350,450)");
		// effect_key shape: sessionId:seq:toolCallId
		expect(h.mock.annotateCalls).toHaveLength(1);
		const call = h.mock.annotateCalls[0]!;
		expect(call.effectKey).toBe(`${h.sessionId}:1:ca1`);
		expect(call.sessionId).toBe(h.sessionId);
		expect(call.label).toBe("肿瘤灶");
		// event
		const ev = h.events.find((e) => e.type === "annotation_created");
		expect(ev?.payload).toMatchObject({ label: "肿瘤灶", x: 100, y: 200, side_px: 500, note: "高密度", index: 0, annotation_id: "ann-uuid-1" });
	});

	it("clamps side_px to 1..40000", async () => {
		const h = await makeHarness();
		await tool(h, "goto").execute("tc1", { x: 0, y: 0, level: 0 });
		await tool(h, "snapshot").execute("snap1", {});
		await tool(h, "create_annotation").execute("ca1", { label: "L", x: 1, y: 2, side_px: 999999 });
		expect(h.mock.annotateCalls[0]?.sidePx).toBe(40000);
	});
});

describe("complete_snapshot_review (ai_agent.py L936-959)", () => {
	it("refuses when no snapshot is pending", async () => {
		const h = await makeHarness();
		const r = await tool(h, "complete_snapshot_review").execute("csr1", { disposition: "annotated" });
		expect(resultText(r)).toBe("当前没有待消化的快照；请先 snapshot。");
	});

	it("rejects an invalid disposition", async () => {
		const h = await makeHarness();
		await tool(h, "goto").execute("tc1", { x: 0, y: 0, level: 0 });
		await tool(h, "snapshot").execute("snap1", {});
		const r = await tool(h, "complete_snapshot_review").execute("csr1", { disposition: "maybe" as unknown as "annotated" });
		expect(resultText(r)).toBe("disposition 必须是 annotated 或 no_annotation。");
	});

	it("requires a reason for no_annotation", async () => {
		const h = await makeHarness();
		await tool(h, "goto").execute("tc1", { x: 0, y: 0, level: 0 });
		await tool(h, "snapshot").execute("snap1", {});
		const r = await tool(h, "complete_snapshot_review").execute("csr1", { disposition: "no_annotation" });
		expect(resultText(r)).toContain("disposition=no_annotation 时请给 no_annotation_reason");
	});

	it("closes the snapshot (annotated) and clears pending", async () => {
		const h = await makeHarness();
		await tool(h, "goto").execute("tc1", { x: 0, y: 0, level: 0 });
		await tool(h, "snapshot").execute("snap1", {});
		const r = await tool(h, "complete_snapshot_review").execute("csr1", { disposition: "annotated", summary: "已标注" });
		expect(resultText(r)).toBe("已关闭快照 snap1（annotated）。");
		const data = await h.store.readSession(h.sessionId);
		expect(data?.pending_snapshot_review).toBeNull();
		const ev = h.events.find((e) => e.type === "snapshot_reviewed");
		expect(ev?.payload).toMatchObject({ snapshot_id: "snap1", disposition: "annotated", summary: "已标注" });
	});

	it("falls back to the latest observation note when summary is omitted", async () => {
		const h = await makeHarness();
		await tool(h, "goto").execute("tc1", { x: 0, y: 0, level: 0 });
		await tool(h, "snapshot").execute("snap1", {});
		await tool(h, "mark_observation").execute("mo1", { label: "Obs 标签", note: "观察详情" });
		const r = await tool(h, "complete_snapshot_review").execute("csr1", { disposition: "no_annotation", no_annotation_reason: "无明显异常" });
		expect(resultText(r)).toBe("已关闭快照 snap1（no_annotation）。");
		const ev = h.events.find((e) => e.type === "snapshot_reviewed");
		expect((ev?.payload as { summary: string }).summary).toBe("观察详情");
		expect((ev?.payload as { no_annotation_reason: string }).no_annotation_reason).toBe("无明显异常");
	});
});

describe("finish (ai_agent.py L750-751)", () => {
	it("returns terminate and 已结束", async () => {
		const h = await makeHarness();
		const r = await tool(h, "finish").execute("f1", { summary: "读片完成" });
		expect(resultText(r)).toBe("已结束");
		expect(r.terminate).toBe(true);
		expect((r.details as { summary: string }).summary).toBe("读片完成");
	});
});

describe("tool set composition (three-kind: main / branch / fork)", () => {
	it("main sessions include create_annotation", () => {
		const tools = createTools({
			sessionStore: {} as SessionStore,
			sessionId: "x",
			kind: "main",
			slide: SLIDE,
			slideInfo,
			flask: {} as unknown as PlatformClient,
			emit: () => undefined,
			cfg: {},
		});
		const names = tools.map((t) => t.name);
		expect(names).toEqual([
			"goto",
			"snapshot",
			"mark_observation",
			"create_annotation",
			"complete_snapshot_review",
			"finish",
		]);
	});

	it("branch sessions include the full toolset (same as main, incl create_annotation)", () => {
		const tools = createTools({
			sessionStore: {} as SessionStore,
			sessionId: "x",
			kind: "branch",
			slide: SLIDE,
			slideInfo,
			flask: {} as unknown as PlatformClient,
			emit: () => undefined,
			cfg: {},
		});
		const names = tools.map((t) => t.name);
		expect(names).toEqual([
			"goto",
			"snapshot",
			"mark_observation",
			"create_annotation",
			"complete_snapshot_review",
			"finish",
		]);
	});

	it("fork sessions register NO tools (lite Q&A)", () => {
		const tools = createTools({
			sessionStore: {} as SessionStore,
			sessionId: "x",
			kind: "fork",
			slide: SLIDE,
			slideInfo,
			flask: {} as unknown as PlatformClient,
			emit: () => undefined,
			cfg: {},
		});
		expect(tools).toEqual([]);
	});
});
