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
