/**
 * Wave 3（BMP/JPEG 普通图片兼容）分享页无物理标尺语义：
 *
 *  - mpp_source="missing"（普通图片）：毫米预设标记入口禁用；点击兜底提示按
 *    「普通图片无物理标尺」措辞（不再引导「请先在工具栏设置 mpp」——分享端
 *    手动 mpp 只改前端状态，服务端 _reject_preset_rect_mm 仍按缺可信 MPP 拒绝）；
 *  - mpp 输入区对普通图片隐藏（不暗示本链接已获得持久化校准）；估算 mpp
 *    （"estimated"，扫描切片）保持原入口；
 *  - 查看（缩放百分比）与像素坐标标注（arrow/freehand、rect 编辑的像素几何）
 *    不因缺 mpp 阻塞或变形；
 *  - mpp 显示已由 share-write.test.ts 的 viewer-core.zoomText 用例锁定
 *    （无 mpp → 百分比 + 数字放大后缀），此处不重复。
 */
import { afterEach, describe, expect, it, vi } from "vitest";
import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const shareSrc = readFileSync(resolve(here, "../../static/share.js"), "utf8");

const TOKEN = "tok123";
const API = "/s/" + TOKEN;

// share.js toast()：createElement("div") + textContent + appendChild 到容器
function fakeElWithTextCapture(captured: string[]) {
	const el: Record<string, unknown> = {
		hidden: true,
		innerHTML: "",
		value: "",
		disabled: false,
		style: {},
		title: "",
		className: "",
		dataset: {},
		classList: {
			_add: new Set<string>(),
			add(c: string) { (this._add as Set<string>).add(c); },
			remove(c: string) { (this._add as Set<string>).delete(c); },
			toggle(c: string, force?: boolean) {
				if (force === undefined) {
					if ((this._add as Set<string>).has(c)) (this._add as Set<string>).delete(c);
					else (this._add as Set<string>).add(c);
				} else if (force) (this._add as Set<string>).add(c);
				else (this._add as Set<string>).delete(c);
			},
			contains(c: string) { return (this._add as Set<string>).has(c); },
		},
		appendChild() {},
		removeChild() {},
		addEventListener() {},
		removeEventListener() {},
		setAttribute() {},
		getAttribute: () => null,
		querySelector() { return fakeElWithTextCapture(captured); },
		querySelectorAll() { return []; },
		focus() {},
		parentNode: null,
	};
	Object.defineProperty(el, "textContent", {
		get: () => "",
		set: (v: unknown) => { captured.push(String(v)); },
	});
	return el;
}

function loadShare(fetchImpl: typeof fetch) {
	const els: Record<string, ReturnType<typeof fakeElWithTextCapture>> = {};
	const toasts: string[] = [];
	const loc = { href: "http://local/s/" + TOKEN, pathname: "/s/" + TOKEN };
	const w: Record<string, unknown> = {
		HP_I18N: {
			// 与既有 share-write harness 同形：未知键原样返回 → 落到 share.js
			// 的 _SHARE_I18N 兜底（Wave 3 新键路径）
			t: (k: string, vars?: { e?: string }) => (vars && vars.e ? `${k}:${vars.e}` : k),
			getLang: () => "zh",
		},
		__SHARE_TOKEN__: TOKEN,
		fetch: fetchImpl,
		location: loc,
		devicePixelRatio: 1,
		setTimeout: (fn: () => void) => fn(),
	};
	const doc = {
		readyState: "loading", // init 延迟到 DOMContentLoaded（harness 不触发）
		cookie: "",
		body: fakeElWithTextCapture(toasts),
		getElementById(id: string) {
			if (!els[id]) els[id] = fakeElWithTextCapture(toasts);
			return els[id];
		},
		createElement() { return fakeElWithTextCapture(toasts); },
		addEventListener() {},
		querySelector() { return fakeElWithTextCapture(toasts); },
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
			state: {
				slides: unknown[];
				slide: { name: string; width: number; height: number; mppX: number | null; mppY: number | null; mppSource: string } | null;
				mppX: number | null;
				roiMode: number | null;
				roi: { x: number; y: number; side: number };
				roiSizes: number[];
				noPhysicalScale: boolean;
				showAnno: boolean;
			};
			toggleRoi: (sizeMm: number) => void;
			syncRoiScaleAvailability: () => void;
			slideHasPhysicalScale: () => boolean;
			updateMppSetterVisibility: () => void;
			saveAnnotation: (geom: object) => void;
			commitEdit: (it: Record<string, unknown>, note: string) => void;
			saveRoi: () => void;
		},
		els,
		toasts,
	};
}

function okJson() {
	return { ok: true, status: 200, json: () => Promise.resolve({}) } as unknown as Response;
}

function rasterSlide() {
	return {
		name: "0005.bmp", width: 1920, height: 1080,
		mppX: null, mppY: null, mppSource: "missing",
	};
}

afterEach(() => {
	vi.unstubAllGlobals();
});

describe("Wave 3：分享页普通图片（mpp_source=missing）毫米预设语义", () => {
	it("toggleRoi：普通图片给「无物理标尺」提示，不再引导去设置 mpp，且不进入 ROI", () => {
		const fetchImpl = vi.fn(() => Promise.resolve(okJson())) as unknown as typeof fetch;
		const h = loadShare(fetchImpl);
		h.share.state.slide = rasterSlide();
		h.share.state.mppX = null;
		h.share.toggleRoi(6);
		const joined = h.toasts.join("\n");
		expect(joined).toContain("普通图片无物理标尺");
		expect(joined).not.toContain("roi.need.mpp");   // 不再引导「请先在工具栏设置 mpp」
		expect(joined).not.toContain("缺少 mpp");
		expect(h.share.state.roiMode).toBeNull();        // 不进入 ROI 模式
		expect(fetchImpl).not.toHaveBeenCalled();        // 不发写请求
	});

	it("toggleRoi：非 missing 的缺 mpp 切片维持原「roi.need.mpp」路径", () => {
		const fetchImpl = vi.fn(() => Promise.resolve(okJson())) as unknown as typeof fetch;
		const h = loadShare(fetchImpl);
		h.share.state.slide = { name: "a.svs", width: 100, height: 100, mppX: null, mppY: null, mppSource: "unknown" };
		h.share.state.mppX = null;
		h.share.toggleRoi(6);
		expect(h.toasts.join("\n")).toContain("roi.need.mpp");
		expect(h.share.state.roiMode).toBeNull();
	});

	it("syncRoiScaleAvailability：普通图片禁用 6/6.5mm 按钮（含禁用态样式/提示），滑块段禁用", () => {
		const fetchImpl = vi.fn(() => Promise.resolve(okJson())) as unknown as typeof fetch;
		const h = loadShare(fetchImpl);
		h.share.state.slide = rasterSlide();
		h.share.state.mppX = null;
		const segs = ["6", "6.5"].map((sz) => {
			const seg = fakeElWithTextCapture(h.toasts);
			(seg as { getAttribute: () => string }).getAttribute = () => sz;
			return seg;
		});
		(h.els["roi-box-btn"] as { querySelectorAll: (s: string) => unknown }).querySelectorAll = () => segs;
		h.share.syncRoiScaleAvailability();
		expect(h.share.state.noPhysicalScale).toBe(true);
		expect(h.share.slideHasPhysicalScale()).toBe(false);
		for (const id of ["roi-6", "roi-6-5"]) {
			expect(h.els[id].disabled).toBe(true);
			expect(String(h.els[id].title)).toContain("无物理标尺");
			expect((h.els[id].classList as unknown as { contains(c: string): boolean }).contains("disabled")).toBe(true);
		}
		for (const seg of segs) {
			expect(seg.disabled).toBe(true);
			expect((seg.classList as unknown as { contains(c: string): boolean }).contains("disabled")).toBe(true);
		}
	});

	it("syncRoiScaleAvailability：有物理标尺切片按钮恢复可用（切片切换回来不残留禁用）", () => {
		const fetchImpl = vi.fn(() => Promise.resolve(okJson())) as unknown as typeof fetch;
		const h = loadShare(fetchImpl);
		h.share.state.slide = { name: "a.svs", width: 1000, height: 1000, mppX: 0.25, mppY: 0.25, mppSource: "openslide" };
		h.share.state.mppX = 0.25;
		h.share.state.noPhysicalScale = true; // 模拟上一张是普通图片的残留
		for (const id of ["roi-6", "roi-6-5"]) {
			h.els[id].disabled = true;
			h.els[id].title = "stale";
			(h.els[id].classList as unknown as { add(c: string): void }).add("disabled");
		}
		h.share.syncRoiScaleAvailability();
		expect(h.share.state.noPhysicalScale).toBe(false);
		for (const id of ["roi-6", "roi-6-5"]) {
			expect(h.els[id].disabled).toBe(false);
			expect(h.els[id].title).toBe("");
			expect((h.els[id].classList as unknown as { contains(c: string): boolean }).contains("disabled")).toBe(false);
		}
	});

	it("标尺闸与分享 roi_sizes 白名单汇合：仅允许 6mm 的分享 + 有 mpp → 6.5 仍禁用", () => {
		const fetchImpl = vi.fn(() => Promise.resolve(okJson())) as unknown as typeof fetch;
		const h = loadShare(fetchImpl);
		h.share.state.roiSizes = [6]; // 本次分享只允许 6mm
		h.share.state.slide = { name: "a.svs", width: 1000, height: 1000, mppX: 0.25, mppY: 0.25, mppSource: "openslide" };
		h.share.state.mppX = 0.25;
		h.share.syncRoiScaleAvailability();
		expect(h.share.state.noPhysicalScale).toBe(false);
		expect(h.els["roi-6"].disabled).toBe(false);
		expect(h.els["roi-6-5"].disabled).toBe(true);   // 白名单闸不被标尺恢复覆盖
		expect(h.els["roi-6-5"].title).toBe("share.size.disallowed");
		// 换到普通图片：两闸同时生效，全部禁用
		h.share.state.slide = rasterSlide();
		h.share.state.mppX = null;
		h.share.syncRoiScaleAvailability();
		expect(h.share.state.noPhysicalScale).toBe(true);
		for (const id of ["roi-6", "roi-6-5"]) {
			expect(h.els[id].disabled).toBe(true);
			expect(String(h.els[id].title)).toContain("无物理标尺");
		}
	});

	it("mpp 输入区：普通图片隐藏（不暗示本链接可获得持久化校准）；estimated 保持显示", () => {
		const fetchImpl = vi.fn(() => Promise.resolve(okJson())) as unknown as typeof fetch;
		const h = loadShare(fetchImpl);
		h.share.state.slide = rasterSlide();
		h.share.state.mppX = null;
		h.share.updateMppSetterVisibility();
		expect(h.els["mpp-setter"].style.display).toBe("none");

		h.share.state.slide = { name: "b.svs", width: 100, height: 100, mppX: 0.4, mppY: 0.4, mppSource: "estimated" };
		h.share.state.mppX = 0.4;
		h.share.updateMppSetterVisibility();
		expect(h.els["mpp-setter"].style.display).toBe("flex");
		expect(h.els["mpp-input"].value).toBe(0.4);
	});
});

describe("Wave 3：分享页像素坐标标注不依赖 mpp", () => {
	it("saveAnnotation（arrow，普通图片）：像素几何透传，无 mpp/毫米字段", async () => {
		const fetchImpl = vi.fn(() => Promise.resolve(okJson())) as unknown as typeof fetch;
		const h = loadShare(fetchImpl);
		h.share.state.slide = rasterSlide();
		h.els["roi-label"].value = "亮点";
		h.share.saveAnnotation({ type: "arrow", x1: 1, y1: 2, x2: 300, y2: 400 });
		await new Promise((r) => setTimeout(r, 0));
		const [url, opts] = (fetchImpl as unknown as vi.Mock).mock.calls[0] as [string, RequestInit];
		expect(url).toBe(API + "/api/roi");
		const body = JSON.parse(String(opts.body));
		expect(body).toMatchObject({
			slide: "0005.bmp", type: "arrow", label: "亮点",
			x1: 1, y1: 2, x2: 300, y2: 400,
		});
		expect(Object.keys(body)).not.toContain("size_mm");
		expect(Object.keys(body)).not.toContain("mpp");
	});

	it("commitEdit（rect，普通图片）：像素 w/h 原样提交，缺 mpp 不重算 size_mm", async () => {
		const fetchImpl = vi.fn(() => Promise.resolve(okJson())) as unknown as typeof fetch;
		const h = loadShare(fetchImpl);
		h.share.state.slide = rasterSlide();
		h.share.state.mppX = null;
		h.share.commitEdit({ type: "rect", index: 3, x: 5, y: 6, w: 100, h: 100, revision: 2 }, "备注");
		await new Promise((r) => setTimeout(r, 0));
		const [url, opts] = (fetchImpl as unknown as vi.Mock).mock.calls[0] as [string, RequestInit];
		expect(url).toBe(API + "/api/roi/3");
		expect((opts.method as string).toUpperCase()).toBe("PATCH");
		const body = JSON.parse(String(opts.body));
		expect(body.geom).toEqual({ x: 5, y: 6, w: 100, h: 100 });
		expect(body.geom).not.toHaveProperty("size_mm");  // 缺可信 mpp：不算假毫米
		expect(body.expected_revision).toBe(2);
	});
});
