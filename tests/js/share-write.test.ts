/**
 * share.js 写操作最小集（test-review P3-18-2）。
 *
 * static/share.js 是 IIFE，仅在末尾最小导出 window.HP_SHARE（真正发写请求的
 * 函数）。本测试按**实际代码**断言写请求契约：
 *   - 鉴权 = URL 内 share token（capability）：所有请求打到 /s/<token>/api/*；
 *   - 分享页**不携带**主站 X-CSRF-Token 头——share_server 无 CSRF 中间件，
 *     token 即凭据（与主站 /api/* 的 header-only CSRF 是两套契约，勿臆造统一）；
 *   - JSON 写请求带 Content-Type: application/json 与完整 body。
 */
import { afterEach, describe, expect, it, vi } from "vitest";
import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const shareSrc = readFileSync(resolve(here, "../../static/share.js"), "utf8");

const TOKEN = "tok123";
const API = "/s/" + TOKEN;

function fakeEl() {
	return {
		hidden: true,
		textContent: "",
		innerHTML: "",
		value: "",
		disabled: false,
		style: {},
		title: "",
		className: "",
		dataset: {},
		classList: { add() {}, remove() {}, toggle() {}, contains() { return false; } },
		appendChild() {},
		removeChild() {},
		addEventListener() {},
		removeEventListener() {},
		setAttribute() {},
		querySelector() { return fakeEl(); },
		querySelectorAll() { return []; },
	};
}

function loadShare(fetchImpl: typeof fetch) {
	const els: Record<string, ReturnType<typeof fakeEl>> = {};
	const loc = { href: "http://local/s/" + TOKEN, pathname: "/s/" + TOKEN };
	const w: Record<string, unknown> = {
		HP_I18N: { t: (k: string, vars?: { e?: string }) => (vars && vars.e ? `${k}:${vars.e}` : k) },
		__SHARE_TOKEN__: TOKEN,
		fetch: fetchImpl,
		location: loc,
		devicePixelRatio: 1,
		setTimeout: (fn: () => void) => fn(),
	};
	const doc = {
		readyState: "loading", // init 延迟到 DOMContentLoaded（harness 不触发）
		cookie: "",
		body: fakeEl(),
		getElementById(id: string) {
			if (!els[id]) els[id] = fakeEl();
			return els[id];
		},
		createElement() { return fakeEl(); },
		addEventListener() {},
		querySelector() { return fakeEl(); },
		querySelectorAll() { return []; },
	};
	(w as { document: typeof doc }).document = doc;
	(globalThis as { document: typeof doc }).document = doc;
	(globalThis as { window: typeof w }).window = w;
	(globalThis as { fetch: typeof fetch }).fetch = fetchImpl;
	(globalThis as { location: typeof loc }).location = loc;
	new Function("window", "document", "fetch", "location", shareSrc)(w, doc, fetchImpl, loc);
	return {
		share: w.HP_SHARE as {
			state: { slides: unknown[]; slide: unknown; roiMode: number | null; roi: { x: number; y: number; side: number } };
			saveRoi: () => void;
			saveAnnotation: (geom: object) => void;
			deleteRoi: (index: number) => void;
		},
		els,
	};
}

function okJson() {
	return { ok: true, status: 200, json: () => Promise.resolve({}) } as unknown as Response;
}

describe("share.js 写请求契约（token 即凭据，无主站 CSRF 头）", () => {
	afterEach(() => {
		vi.unstubAllGlobals();
	});

	it("saveRoi → POST /s/<token>/api/roi，JSON body 完整", () => {
		const fetchImpl = vi.fn(() => Promise.resolve(okJson())) as unknown as typeof fetch;
		const h = loadShare(fetchImpl);
		h.share.state.slide = { name: "s.svs", width: 100, height: 100, mppX: 0.5, mppSource: "manual" };
		h.share.state.roiMode = 6;
		h.share.state.roi = { x: 10, y: 20, side: 12 };
		h.els["roi-label"].value = "病灶";
		h.els["roi-note"].value = "备注";
		h.share.saveRoi();
		expect(fetchImpl).toHaveBeenCalledTimes(1);
		const [url, opts] = (fetchImpl as unknown as vi.Mock).mock.calls[0] as [string, RequestInit];
		expect(url).toBe(API + "/api/roi");
		expect((opts.method as string).toUpperCase()).toBe("POST");
		expect(opts.headers).toEqual({ "Content-Type": "application/json" });
		expect(opts.headers).not.toHaveProperty("X-CSRF-Token");
		const body = JSON.parse(String(opts.body));
		expect(body).toMatchObject({ slide: "s.svs", type: "rect", label: "病灶", note: "备注" });
	});

	it("saveAnnotation（arrow）→ POST /s/<token>/api/roi，几何透传", () => {
		const fetchImpl = vi.fn(() => Promise.resolve(okJson())) as unknown as typeof fetch;
		const h = loadShare(fetchImpl);
		h.share.state.slide = { name: "s.svs", width: 100, height: 100, mppX: 0.5, mppSource: "manual" };
		h.els["roi-label"].value = "箭头";
		h.share.saveAnnotation({ type: "arrow", x1: 1, y1: 2, x2: 3, y2: 4 });
		const [url, opts] = (fetchImpl as unknown as vi.Mock).mock.calls[0] as [string, RequestInit];
		expect(url).toBe(API + "/api/roi");
		expect((opts.method as string).toUpperCase()).toBe("POST");
		expect(opts.headers).not.toHaveProperty("X-CSRF-Token");
		const body = JSON.parse(String(opts.body));
		expect(body).toMatchObject({ slide: "s.svs", type: "arrow", label: "箭头", x1: 1, y1: 2, x2: 3, y2: 4 });
	});

	it("deleteRoi → DELETE /s/<token>/api/roi/<index>，无 body/无 CSRF 头", () => {
		const fetchImpl = vi.fn(() => Promise.resolve(okJson())) as unknown as typeof fetch;
		const h = loadShare(fetchImpl);
		h.share.deleteRoi(3);
		const [url, opts] = (fetchImpl as unknown as vi.Mock).mock.calls[0] as [string, RequestInit];
		expect(url).toBe(API + "/api/roi/3");
		expect((opts.method as string).toUpperCase()).toBe("DELETE");
		expect(opts.headers).toBeUndefined();
		expect(opts.body).toBeUndefined();
	});

	it("label 为空 → saveRoi 不发写请求（前端必填校验）", () => {
		const fetchImpl = vi.fn(() => Promise.resolve(okJson())) as unknown as typeof fetch;
		const h = loadShare(fetchImpl);
		h.share.state.slide = { name: "s.svs", width: 100, height: 100, mppX: 0.5, mppSource: "manual" };
		h.share.state.roiMode = 6;
		h.els["roi-label"].value = "";
		h.share.saveRoi();
		expect(fetchImpl).not.toHaveBeenCalled();
	});
});

// ========================================================================== //
// G8 倍率徽章：唯一口径 HP_ViewerCore.zoomText，单调且三页面同源
// （review-2026-08-29 §10.4 G8）
// ========================================================================== //
const viewerCoreSrc = readFileSync(resolve(here, "../../static/viewer-core.js"), "utf8");
const appSrc = readFileSync(resolve(here, "../../static/app.js"), "utf8");
const demoSrc = readFileSync(resolve(here, "../../static/demo.js"), "utf8");
const shareHtml = readFileSync(resolve(here, "../../templates/share.html"), "utf8");

function loadViewerCore(overrides: Record<string, unknown> = {}) {
	const w: Record<string, unknown> = { ...overrides };
	new Function("window", viewerCoreSrc)(w);
	return w.HP_ViewerCore as {
		zoomText: (viewer: unknown, mppX: unknown) => string;
		formatMag: (mag: number) => string;
		zoomToNative: (viewer: unknown) => boolean;
	};
}

function fakeViewer(zoom: number) {
	return {
		viewport: {
			getZoom: () => zoom,
			getContainerSize: () => ({ x: 1000 }),
		},
		source: { dimensions: { x: 2000 } },
	};
}

function magValue(text: string): number {
	// "40×" / "1.2k×" / "3.4M×" / "57%" → 数值；"—" 视为 -Infinity；
	// F3「数字放大 / digital」后缀只修饰展示，不参与数值单调比较
	if (text === "—" || text === "") return -Infinity;
	const stripped = String(text).replace(/\s*(数字放大|digital)\s*$/, "");
	const m = stripped.match(/^([\d.]+)(k|M)?[%×]?$/);
	if (!m) throw new Error("unexpected zoomText output: " + text);
	let v = parseFloat(m[1]);
	if (m[2] === "k") v *= 1e3;
	if (m[2] === "M") v *= 1e6;
	return v;
}

describe("G8 倍率徽章：唯一口径 viewer-core.zoomText（三页面同源 + 单调）", () => {
	it("zoom 增大时倍率单调不减（mpp 有效，imageZoom × 10/mpp）", () => {
		const core = loadViewerCore();
		const mpp = 0.5; // 20× 物镜
		let prev = -Infinity;
		for (let i = 0; i <= 40; i++) {
			const zoom = 0.05 * Math.pow(1.25, i); // 视口缩放递增
			const v = magValue(core.zoomText(fakeViewer(zoom), mpp));
			expect(v).toBeGreaterThanOrEqual(prev);
			prev = v;
		}
		// 1× 图像像素/屏幕像素 且 mpp=0.5 → 20×（物镜等效）
		expect(core.zoomText(fakeViewer(2), 0.5)).toBe("20×");
	});

	it("无 mpp 时退回百分比，同样单调", () => {
		const core = loadViewerCore();
		let prev = -Infinity;
		for (let i = 0; i <= 20; i++) {
			const zoom = 0.1 * Math.pow(1.3, i);
			const v = magValue(core.zoomText(fakeViewer(zoom), null));
			expect(v).toBeGreaterThanOrEqual(prev);
			prev = v;
		}
	});

	it("同一输入下 formatMag 是唯一展示格式（app/share/viewer-core 无第二套公式）", () => {
		const core = loadViewerCore();
		// 与 app.js/demo.js 的显示格式同源：由 viewer-core.formatMag 定义
		expect(core.formatMag(20)).toBe("20×");
		expect(core.formatMag(1234)).toMatch(/1k|1234/);
	});

	it("share.js 已删除本地倍率公式：不含 25400 / 本地 formatMag，只调 zoomText", () => {
		expect(shareSrc).not.toContain("25400");
		expect(shareSrc).not.toMatch(/function\s+formatMag\s*\(/);
		expect(shareSrc).not.toMatch(/function\s+updateMag\s*\(/);
		expect(shareSrc).toMatch(/HP_ViewerCore\.zoomText/);
		// 倍率数值换算只允许出现在 viewer-core（app.js 已委托；demo.js 已委托）
		expect(appSrc).toMatch(/HP_ViewerCore\.zoomText/);
		expect(demoSrc).toMatch(/HP_ViewerCore\.zoomText/);
	});

	it("share.html 在 share.js 之前加载带版本的 viewer-core.js", () => {
		const coreIdx = shareHtml.indexOf("/static/viewer-core.js");
		const shareIdx = shareHtml.indexOf("/static/share.js");
		expect(coreIdx).toBeGreaterThan(-1);
		expect(shareIdx).toBeGreaterThan(coreIdx);
		expect(shareHtml).toMatch(/viewer-core\.js\?v=/); // 与主站同 cache-bust 约定
	});

	it("F3 倍率诚实：imageZoom>1.02 追加数字放大后缀（zh/en），1:1 附近不标", () => {
		// fakeViewer(zoom): containerW=1000, imgW=2000 → imageZoom = zoom/2
		const core = loadViewerCore();
		expect(core.zoomText(fakeViewer(2), 0.5)).toBe("20×"); // imageZoom=1：无后缀
		expect(core.zoomText(fakeViewer(2.1), 0.5)).toMatch(/ 数字放大$/); // imageZoom=1.05
		expect(core.zoomText(fakeViewer(2.1), null)).toMatch(/% 数字放大$/);
		const coreEn = loadViewerCore({ HP_I18N: { getLang: () => "en" } });
		expect(coreEn.zoomText(fakeViewer(2.1), 0.5)).toMatch(/ digital$/);
		expect(coreEn.zoomText(fakeViewer(2), 0.5)).not.toContain("digital");
	});

	it("F3 zoomToNative：imageZoom=1（zoom=imgW/containerW），保留中心并 applyConstraints", () => {
		const core = loadViewerCore();
		const zoomCalls: Array<{ zoom: number; center: unknown }> = [];
		let constrained = 0;
		const viewer = {
			viewport: {
				getZoom: () => 8,
				getContainerSize: () => ({ x: 1000 }),
				getCenter: () => ({ x: 0.5, y: 0.5 }),
				zoomTo: (z: number, c: unknown) => {
					zoomCalls.push({ zoom: z, center: c });
				},
				applyConstraints: () => {
					constrained += 1;
				},
			},
			source: { dimensions: { x: 2000 } },
		};
		expect(core.zoomToNative(viewer)).toBe(true);
		expect(zoomCalls.length).toBe(1);
		expect(zoomCalls[0]!.zoom).toBe(2); // 2000/1000 → 1 CSS px / 1 level-0 px
		expect(zoomCalls[0]!.center).toEqual({ x: 0.5, y: 0.5 });
		expect(constrained).toBe(1);
		// 无 viewer/viewport/source：安全返回 false，不抛
		expect(core.zoomToNative(null)).toBe(false);
		expect(core.zoomToNative({ viewport: {} })).toBe(false);
	});
});
