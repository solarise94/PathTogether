/**
 * 矩形手势状态机（§3.2）——加载真实 static/app.js（最小 DOM + fetch mock），
 * 经生产入口驱动：openSlide → 矩形工具 → 预设放置/自由画框 → 指针事件序列。
 *
 * 锁定（handoff 20261018 §3.2/§3.3）：
 *   - 一次手势只归属一个活动 pointerId：其他指针的 move/up/cancel 一律忽略；
 *   - lostpointercapture / pointercancel / window blur = 取消：恢复拖前几何，
 *     绝不触发保存（自由画框捕获丢失也不自动保存）；
 *   - move 发现 buttons===0（释放事件在框外/浏览器外丢失）→ 终止陈旧手势，
 *     不改写几何、不算一次有效提交；
 *   - 正常 pointerup 恰好完成一次：拖框保留几何，自由画框自动保存恰好一次；
 *   - setPointerCapture 失败时 document 兜底监听接管，结束时移除；
 *   - 工具退出 / Escape 撤稿 / 换片清理活动手势（幂等）。
 */
import { afterEach, describe, expect, it, vi } from "vitest";
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const appSrc = readFileSync(resolve(here, "../../static/app.js"), "utf8");
const geoSrc = readFileSync(resolve(here, "../../static/anno-geometry.js"), "utf8");
const require = createRequire(import.meta.url);

const SLIDE = "rect-draft.bmp";
const SLIDE_ID = "sld_rect000001";

// ---------- 指针事件桩（只带状态机读的字段） ----------
interface PEvt {
	type: string;
	pointerId: number;
	button: number;
	buttons: number;
	clientX: number;
	clientY: number;
	shiftKey: boolean;
	target: unknown;
	preventDefaultCount: number;
	preventDefault(): void;
	stopPropagation(): void;
}

function pev(opts: Partial<PEvt> & { target?: unknown }): PEvt {
	const e = {
		type: opts.type || "pointermove",
		pointerId: opts.pointerId ?? 1,
		button: opts.button ?? 0,
		buttons: opts.buttons ?? 1,
		clientX: opts.clientX ?? 0,
		clientY: opts.clientY ?? 0,
		shiftKey: !!opts.shiftKey,
		target: opts.target,
		preventDefaultCount: 0,
		preventDefault() { e.preventDefaultCount++; },
		stopPropagation() {},
	} as PEvt;
	return e;
}

// ---------- 元素桩：真实监听表 + 可编程的指针捕获 ----------
interface FakeEl extends Record<string, unknown> {
	id: string;
	tagName: string;
	hidden: boolean;
	title: string;
	type: string;
	className: string;
	childNodes: FakeEl[];
	style: Record<string, string>;
	dataset: Record<string, string>;
	textContent: string;
	innerHTML: string;
	value: string;
	disabled: boolean;
	offsetWidth: number;
	offsetHeight: number;
	clientWidth: number;
	clientHeight: number;
	children: FakeEl[];
	parentNode: FakeEl | null;
	ownerDocument: FakeDoc | null;
	classList: {
		add: (...names: string[]) => void;
		remove: (...names: string[]) => void;
		contains: (n: string) => boolean;
		toggle: (n: string, force?: boolean) => boolean;
	};
	setAttribute: (k: string, v: string) => void;
	getAttribute: (k: string) => string | null;
	addEventListener: (type: string, cb: (e?: unknown) => void) => void;
	removeEventListener: (type: string, cb: (e?: unknown) => void) => void;
	listenerCount: (type: string) => number;
	dispatch: (type: string, evt?: Record<string, unknown>) => void;
	appendChild: (c: FakeEl) => FakeEl;
	insertBefore: (c: FakeEl) => FakeEl;
	removeChild: (c: FakeEl) => FakeEl;
	remove: () => void;
	querySelector: (sel: string) => FakeEl | null;
	querySelectorAll: () => FakeEl[];
	closest: () => null;
	focus: () => void;
	getBoundingClientRect: () => { width: number; height: number; left: number; top: number };
	setPointerCapture: (pid: number) => void;
	releasePointerCapture: (pid: number) => void;
	hasPointerCapture: (pid: number) => boolean;
	failPointerCapture: () => void;
	capturedPointerId: number | null;
	released: number[];
	releaseShouldThrow: boolean;
}

function fakeEl(id = "", tag = "div", doc: FakeDoc | null = null): FakeEl {
	const listeners: Record<string, Array<(e?: unknown) => void>> = {};
	const children: FakeEl[] = [];
	const classes = new Set<string>();
	const attrs = new Map<string, string>();
	const el: FakeEl = {
		id,
		tagName: tag.toUpperCase(),
		hidden: false,
		title: "",
		type: "",
		childNodes: children,
		style: {},
		dataset: {},
		textContent: "",
		innerHTML: "",
		value: "",
		disabled: false,
		offsetWidth: 60,
		offsetHeight: 28,
		clientWidth: 800,
		clientHeight: 600,
		children,
		parentNode: null,
		ownerDocument: doc,
		capturedPointerId: null,
		released: [],
		releaseShouldThrow: false,
		classList: {
			add: (...names) => names.forEach((n) => classes.add(n)),
			remove: (...names) => names.forEach((n) => classes.delete(n)),
			contains: (n) => classes.has(n),
			toggle: (n, force) => {
				const on = force === undefined ? !classes.has(n) : !!force;
				if (on) classes.add(n);
				else classes.delete(n);
				return on;
			},
		},
		setAttribute: (k, v) => void attrs.set(k, String(v)),
		getAttribute: (k) => (attrs.has(k) ? (attrs.get(k) as string) : null),
		addEventListener: (type, cb) => void (listeners[type] ||= []).push(cb),
		removeEventListener: (type, cb) => {
			const arr = listeners[type] || [];
			const i = arr.indexOf(cb);
			if (i >= 0) arr.splice(i, 1);
		},
		listenerCount: (type) => (listeners[type] || []).length,
		dispatch(type, evt) {
			const e = Object.assign({
				type,
				target: el,
				pointerId: 1,
				button: 0,
				buttons: 1,
				clientX: 0,
				clientY: 0,
				shiftKey: false,
				key: "",
				preventDefault() {},
				stopPropagation() {},
			}, evt || {}, { type, target: el });
			(listeners[type] || []).slice().forEach((cb) => cb(e));
		},
		appendChild(c) {
			if (c && typeof c === "object") {
				if (c.parentNode && c.parentNode !== el) c.parentNode.removeChild(c);
				c.parentNode = el;
				children.push(c);
			}
			return c;
		},
		insertBefore(c) {
			return el.appendChild(c);
		},
		removeChild(c) {
			const i = children.indexOf(c);
			if (i >= 0) children.splice(i, 1);
			c.parentNode = null;
			return c;
		},
		remove() {
			if (el.parentNode) el.parentNode.removeChild(el);
		},
	querySelector: () => null,
	querySelectorAll: () => [],
	closest: () => null,
	focus() {},
	width: 0,
	height: 0,
	getContext: () => ({
		clearRect() {}, save() {}, restore() {}, setTransform() {}, setLineDash() {},
		beginPath() {}, moveTo() {}, lineTo() {}, stroke() {}, fill() {}, arc() {},
		strokeRect() {}, fillRect() {}, fillText() {}, translate() {}, rotate() {},
		scale() {}, closePath() {}, quadraticCurveTo() {},
		measureText: (t: string) => ({ width: String(t).length * 6 }),
		drawImage() {},
		set font(_v: string) {},
		get font() { return ""; },
		set fillStyle(_v: string) {},
		get fillStyle() { return ""; },
		set strokeStyle(_v: string) {},
		get strokeStyle() { return ""; },
		set lineWidth(_v: number) {},
		get lineWidth() { return 1; },
		set globalAlpha(_v: number) {},
		get globalAlpha() { return 1; },
	}),
	getBoundingClientRect: () => ({ width: 800, height: 600, left: 0, top: 0 }),
		setPointerCapture(pid) {
			if (el.releaseShouldThrow) throw new Error("setPointerCapture: NotSupportedError");
			el.capturedPointerId = pid;
		},
		releasePointerCapture(pid) {
			if (el.releaseShouldThrow || el.capturedPointerId !== pid) {
				throw new Error("releasePointerCapture: InvalidPointerId");
			}
			el.capturedPointerId = null;
			el.released.push(pid);
		},
		hasPointerCapture: (pid) => el.capturedPointerId === pid,
	};
	Object.defineProperty(el, "className", {
		get: () => Array.from(classes).join(" "),
		set: (v: string) => {
			classes.clear();
			String(v).split(/\s+/).filter(Boolean).forEach((n) => classes.add(n));
		},
		configurable: true,
	});
	return el;
}

// ---------- 文档桩：元素注册表 + 捕获阶段兜底监听表 ----------
interface FakeDoc {
	readyState: string;
	cookie: string;
	body: FakeEl;
	els: Record<string, FakeEl>;
	created: FakeEl[];
	docListeners: Record<string, Array<(e?: unknown) => void>>;
	getElementById: (id: string) => FakeEl;
	createElement: (tag?: string) => FakeEl;
	addEventListener: (type: string, cb: (e?: unknown) => void) => void;
	removeEventListener: (type: string, cb: (e?: unknown) => void) => void;
	dispatch: (type: string, evt?: Record<string, unknown>) => void;
	listenerCount: (type: string) => number;
	querySelector: () => null;
	querySelectorAll: () => FakeEl[];
}

function fakeDoc(): FakeDoc {
	const doc: FakeDoc = {
		readyState: "loading",
		cookie: "csrf_token=tok",
		body: null as unknown as FakeEl,
		els: {},
		created: [],
		docListeners: {},
		getElementById(id) {
			if (!doc.els[id]) doc.els[id] = fakeEl(id, "div", doc);
			return doc.els[id];
		},
		createElement(tag = "div") {
			const el = fakeEl("", tag, doc);
			doc.created.push(el);
			return el;
		},
		addEventListener(type, cb) {
			(doc.docListeners[type] ||= []).push(cb);
		},
		removeEventListener(type, cb) {
			const arr = doc.docListeners[type] || [];
			const i = arr.indexOf(cb);
			if (i >= 0) arr.splice(i, 1);
		},
	dispatch(type, evt) {
		const e = Object.assign({
			type,
			pointerId: 1,
			button: 0,
			buttons: 1,
			clientX: 0,
			clientY: 0,
			shiftKey: false,
			key: "",
			preventDefault() {},
			stopPropagation() {},
		}, evt || {}, { type });
		(doc.docListeners[type] || []).slice().forEach((cb) => cb(e));
	},
		listenerCount: (type) => (doc.docListeners[type] || []).length,
		querySelector: () => null,
		querySelectorAll: () => [],
	};
	doc.body = fakeEl("body", "body", doc);
	return doc;
}

// ---------- OpenSeadragon 桩 + viewer 桩（1:1 坐标映射） ----------
function installOsd() {
	const g = globalThis as { OpenSeadragon?: unknown };
	g.OpenSeadragon = {
		Point: function (x: number, y: number) {
			this.x = x;
			this.y = y;
		},
		Placement: { TOP_LEFT: "TOP_LEFT" },
		OverlayRotationMode: { BOUNDING_BOX: "BOUNDING_BOX" },
	};
}

function makeViewer() {
	const calls: Array<{ op: string; args: unknown[] }> = [];
	const overlays: unknown[] = [];
	const viewport = {
		viewerElementToImageCoordinates: (p: { x: number; y: number }) => ({ x: p.x, y: p.y }),
		imageToViewportRectangle: (x: number, y: number, w: number, h: number) => ({ x, y, w, h }),
		imageToViewerElementCoordinates: (x: number, y: number) => ({ x, y }),
	};
	const container = fakeEl("viewer-container");
	const viewer = {
		container,
		canvas: fakeEl("viewer-canvas"),
		viewport,
		currentOverlays: overlays,
		mouseNavEnabled: true,
		setMouseNavEnabled(on: boolean) {
			viewer.mouseNavEnabled = on;
			calls.push({ op: "setMouseNavEnabled", args: [on] });
		},
		addHandler() {},
		open() {},
		updateOverlay(el: unknown, rect: unknown, placement: unknown) {
			calls.push({ op: "updateOverlay", args: [el, rect, placement] });
		},
		addOverlay(opts: { element: unknown }) {
			overlays.push(opts.element);
			calls.push({ op: "addOverlay", args: [opts.element] });
		},
		getOverlayById(el: unknown) {
			return overlays.includes(el) ? { element: el } : null;
		},
		removeOverlay(el: unknown) {
			const i = overlays.indexOf(el);
			if (i >= 0) overlays.splice(i, 1);
			calls.push({ op: "removeOverlay", args: [el] });
		},
		close() {},
	};
	return { viewer, calls, container };
}

interface Boot {
	hooks: {
		viewerState: {
			slide: { name: string; width: number; height: number } | null;
			roiMode: string | null;
			roi: { x: number; y: number; w: number; h: number };
			rotation: number;
		};
		openSlide: (name: string) => void;
		saveAnno: () => void;
	};
	doc: FakeDoc;
	viewer: ReturnType<typeof makeViewer>["viewer"];
	calls: ReturnType<typeof makeViewer>["calls"];
	posts: Array<Record<string, unknown>>;
	winListeners: Record<string, Array<() => void>>;
	win: Record<string, unknown>;
	rafQueue: Array<() => void>;
}

async function bootApp(): Promise<Boot> {
	installOsd();
	const doc = fakeDoc();
	const mv = makeViewer();
	const posts: Array<Record<string, unknown>> = [];
	const winListeners: Record<string, Array<() => void>> = {};
	// rAF 只入队不自动执行：帧回调由 flushRaf() 显式驱动，
	// 测试结束后没有残留定时器去碰已拆除的全局。
	const rafQueue: Array<() => void> = [];

	const fetchImpl = vi.fn((url: string, opts?: { method?: string; body?: string }) => {
		const u = String(url);
		if (u.includes("/api/annotation") && (opts?.method || "GET") === "POST") {
			const body = JSON.parse(opts!.body || "{}") as Record<string, unknown>;
			posts.push(body);
			return Promise.resolve({
				ok: true,
				status: 200,
				clone() { return this; },
				json: () => Promise.resolve({ annotation_id: "anno-1", revision: 1 }),
			});
		}
		if (u.includes(`/api/slides/${SLIDE_ID}/info`) ||
			u.includes(`/api/slide/${SLIDE}/info`) ||
			u.includes(`/api/slides/${SLIDE}/info`)) {
			return Promise.resolve({
				ok: true,
				status: 200,
				clone() { return this; },
				json: () => Promise.resolve({
					name: SLIDE, slide_id: SLIDE_ID, display_name: "", original_filename: SLIDE,
					width: 1000, height: 800, mpp_x: 0.5, mpp_y: 0.5, mpp_source: "native",
				}),
			});
		}
		if (u.includes("/api/annotations")) {
			return Promise.resolve({
				ok: true, status: 200, clone() { return this; },
				json: () => Promise.resolve({ annotations: [], by_slide: {} }),
			});
		}
		if (u.includes("/api/slides")) {
			return Promise.resolve({
				ok: true, status: 200, clone() { return this; },
				json: () => Promise.resolve([{
					name: SLIDE, slide_id: SLIDE_ID, display_name: "",
					width: 1000, height: 800, mpp_x: 0.5, mpp_y: 0.5, mpp_source: "native",
				}]),
			});
		}
		if (u.includes("/api/projects")) {
			return Promise.resolve({
				ok: true, status: 200, clone() { return this; },
				json: () => Promise.resolve([{ pid: "p1", name: "P1", slides: [SLIDE], slide_count: 1, roi_count: 0 }]),
			});
		}
		return Promise.resolve({
			ok: true, status: 200, clone() { return this; }, json: () => Promise.resolve({}),
		});
	}) as unknown as typeof fetch;

	const w: Record<string, unknown> = {
		__PT_TEST_HOOKS: true,
		HP_I18N: { t: (k: string) => k, getLang: () => "zh" },
		HP_ViewerCore: { create: () => mv.viewer },
		HP_API: {},
		HP_APP_BOOTSTRAP: { mode: "official", capabilities: { slide_id_api: true } },
		HistoPilot: {},
		HP_PLUGIN_BUNDLES: [],
		fetch: fetchImpl,
		location: { href: "http://local/", pathname: "/" },
		innerWidth: 1280,
		innerHeight: 900,
		matchMedia: () => ({ matches: false, addEventListener() {}, addListener() {} }),
		requestAnimationFrame: (cb: () => void) => {
			rafQueue.push(cb);
			return rafQueue.length;
		},
		addEventListener(type: string, cb: () => void) {
			(winListeners[type] ||= []).push(cb);
		},
		removeEventListener() {},
		localStorage: null,
		setTimeout,
		clearTimeout,
	};
	// anno-geometry.js 以经典 script 语义挂 window.HP_AnnoGeometry
	new Function("window", geoSrc)(w);
	// app.js 以裸全局引用 HP_ViewerCore / OpenSeadragon（同既有 harness 约定）
	(globalThis as { HP_ViewerCore?: unknown }).HP_ViewerCore = { create: () => mv.viewer };

	(globalThis as { document?: unknown }).document = doc;
	(globalThis as { window?: unknown }).window = w;
	(globalThis as { fetch?: unknown }).fetch = fetchImpl;
	(globalThis as { location?: unknown }).location = w.location;

	new Function("window", "document", "fetch", "location", appSrc)(w, doc, fetchImpl, w.location);
	(doc.docListeners["DOMContentLoaded"] || []).forEach((cb) => cb());

	// 打开切片并就绪 viewer（info 回包异步；flush 后 state.slide 就绪）
	(w.HP_PROJECT_UI as Boot["hooks"]).openSlide(SLIDE);
	for (let i = 0; i < 12; i++) await Promise.resolve();
	mv.viewer.viewport = {
		viewerElementToImageCoordinates: (p: { x: number; y: number }) => ({ x: p.x, y: p.y }),
		imageToViewportRectangle: (x: number, y: number, w: number, h: number) => ({ x, y, w, h }),
		imageToViewerElementCoordinates: (x: number, y: number) => ({ x, y }),
	};

	return {
		hooks: w.HP_PROJECT_UI as Boot["hooks"],
		doc,
		viewer: mv.viewer,
		calls: mv.calls,
		posts,
		winListeners,
		win: w,
		rafQueue,
	};
}

/** 执行挂起的 rAF 帧回调（布局/重绘同步）；新回调入队则继续排空。 */
function flushRaf(h: Boot) {
	for (let i = 0; i < 10 && h.rafQueue.length; i++) {
		h.rafQueue.splice(0).forEach((cb) => cb());
	}
}

// ---------- 驱动助手 ----------
/** 触发 window 级监听（onViewerKeydown 注册处），补默认事件方法。 */
function fireWin(h: Boot, type: string, evt: Record<string, unknown>) {
	const e = Object.assign({
		type,
		key: "",
		target: h.doc.body,
		preventDefault() {},
		stopPropagation() {},
	}, evt, { type });
	h.winListeners[type].forEach((cb) => (cb as (x: unknown) => void)(e));
}

function roiBoxOf(h: Boot): FakeEl {
	const box = h.doc.created.filter((e) => e.id === "roi-box").pop();
	expect(box, "roi-box 必须已创建").toBeTruthy();
	return box as FakeEl;
}

function activateRectTool(h: Boot) {
	h.doc.getElementById("roi-rect-btn").dispatch("click", { type: "click" });
	expect(h.hooks.viewerState.roiMode).toBe("rect");
}

/** 预设尺寸放置：切到 px 单位，100×80，Enter 应用（点击画布中心放置用） */
function placePresetDraft(h: Boot) {
	const unitSel = h.doc.getElementById("roi-unit-select");
	unitSel.value = "px";
	unitSel.dispatch("change", { type: "change" });
	const wIn = h.doc.getElementById("roi-w-input");
	const hIn = h.doc.getElementById("roi-h-input");
	wIn.value = "100";
	hIn.value = "80";
	hIn.dispatch("keydown", { type: "keydown", key: "Enter" });
	expect(h.hooks.viewerState.roi).toEqual({ x: 450, y: 360, w: 100, h: 80 });
}

async function settle(times = 12) {
	for (let i = 0; i < times; i++) await Promise.resolve();
}

afterEach(() => {
	vi.restoreAllMocks();
	delete (globalThis as { window?: unknown }).window;
	delete (globalThis as { document?: unknown }).document;
	delete (globalThis as { fetch?: unknown }).fetch;
	delete (globalThis as { location?: unknown }).location;
	delete (globalThis as { HP_ViewerCore?: unknown }).HP_ViewerCore;
	delete (globalThis as { OpenSeadragon?: unknown }).OpenSeadragon;
});

// ---------------------------------------------------------------- 拖框（roi-box）

describe("矩形拖框手势（§3.2：单指针归属 + 丢失捕获取消）", () => {
	it("正常拖动/释放：几何保留、导航恢复；随后悬停（buttons=0）不再改写几何", async () => {
		const h = await bootApp();
		activateRectTool(h);
		placePresetDraft(h);
		const box = roiBoxOf(h);
		const start = { ...h.hooks.viewerState.roi };
		const baseDocMove = h.doc.listenerCount("pointermove");

		box.dispatch("pointerdown", { type: "pointerdown", pointerId: 1, button: 0, buttons: 1, clientX: 500, clientY: 400 });
		expect(h.viewer.mouseNavEnabled).toBe(false);
		// 捕获成功：挂了 lostpointercapture，无 document 兜底（相对基线不增）
		expect(box.hasPointerCapture(1)).toBe(true);
		expect(box.listenerCount("lostpointercapture")).toBe(1);
		expect(h.doc.listenerCount("pointermove")).toBe(baseDocMove);

		box.dispatch("pointermove", { type: "pointermove", pointerId: 1, buttons: 1, clientX: 560, clientY: 430 });
		expect(h.hooks.viewerState.roi).toEqual({ x: 510, y: 390, w: 100, h: 80 });

		box.dispatch("pointerup", { type: "pointerup", pointerId: 1, buttons: 0, clientX: 560, clientY: 430 });
		expect(h.viewer.mouseNavEnabled).toBe(true);
		expect(h.hooks.viewerState.roi).toEqual({ x: 510, y: 390, w: 100, h: 80 });
		expect(h.posts).toEqual([]); // 拖框完成 ≠ 保存
		expect(box.listenerCount("lostpointercapture")).toBe(0);

		// 释放后跨过框和按钮的悬停：几何不动
		box.dispatch("pointermove", { type: "pointermove", pointerId: 1, buttons: 0, clientX: 700, clientY: 500 });
		expect(h.hooks.viewerState.roi).toEqual({ x: 510, y: 390, w: 100, h: 80 });
		expect(start).toBeTruthy();
	});

	it("拖动中丢失捕获（lostpointercapture）：恢复拖前几何；框外 pointerup 与悬停均无效", async () => {
		const h = await bootApp();
		activateRectTool(h);
		placePresetDraft(h);
		const box = roiBoxOf(h);
		const before = { ...h.hooks.viewerState.roi };

		box.dispatch("pointerdown", { type: "pointerdown", pointerId: 1, button: 0, buttons: 1, clientX: 500, clientY: 400 });
		box.dispatch("pointermove", { type: "pointermove", pointerId: 1, buttons: 1, clientX: 600, clientY: 460 });
		expect(h.hooks.viewerState.roi).not.toEqual(before);

		// 真实故障形态：捕获被外部释放，pointerup 落到框外（元素上收不到）
		box.dispatch("lostpointercapture", { type: "lostpointercapture", pointerId: 1, buttons: 1 });
		expect(h.hooks.viewerState.roi).toEqual(before);
		expect(h.viewer.mouseNavEnabled).toBe(true);
		expect(box.listenerCount("lostpointercapture")).toBe(0);

		// pointerup 落到 anno-canvas（元素外）：不得重新进入任何手势路径
		h.doc.getElementById("anno-canvas").dispatch("pointerup", { type: "pointerup", pointerId: 1, buttons: 0, clientX: 20, clientY: 20 });
		// 之后无按键悬停：几何不动
		box.dispatch("pointermove", { type: "pointermove", pointerId: 1, buttons: 0, clientX: 800, clientY: 600 });
		expect(h.hooks.viewerState.roi).toEqual(before);
		expect(h.posts).toEqual([]);
	});

	it("释放事件缺失但下一帧 buttons=0：立即终止陈旧手势并恢复，不提交", async () => {
		const h = await bootApp();
		activateRectTool(h);
		placePresetDraft(h);
		const box = roiBoxOf(h);
		const before = { ...h.hooks.viewerState.roi };

		box.dispatch("pointerdown", { type: "pointerdown", pointerId: 1, button: 0, buttons: 1, clientX: 500, clientY: 400 });
		box.dispatch("pointermove", { type: "pointermove", pointerId: 1, buttons: 1, clientX: 540, clientY: 420 });
		// 释放发生在浏览器外：下一个 move 已无按键
		box.dispatch("pointermove", { type: "pointermove", pointerId: 1, buttons: 0, clientX: 560, clientY: 430 });
		expect(h.hooks.viewerState.roi).toEqual(before);
		expect(h.viewer.mouseNavEnabled).toBe(true);
		// 后续悬停/释放都不再改写
		box.dispatch("pointermove", { type: "pointermove", pointerId: 1, buttons: 0, clientX: 900, clientY: 700 });
		box.dispatch("pointerup", { type: "pointerup", pointerId: 1, buttons: 0, clientX: 900, clientY: 700 });
		expect(h.hooks.viewerState.roi).toEqual(before);
		expect(h.posts).toEqual([]);
	});

	it("其他指针的 move/up 不干扰当前手势；pointercancel 恢复拖前几何", async () => {
		const h = await bootApp();
		activateRectTool(h);
		placePresetDraft(h);
		const box = roiBoxOf(h);
		const before = { ...h.hooks.viewerState.roi };

		box.dispatch("pointerdown", { type: "pointerdown", pointerId: 1, button: 0, buttons: 1, clientX: 500, clientY: 400 });
		// 第二指（pointerId=2）按下/移动：不得夺取手势，也不得新开手势
		box.dispatch("pointerdown", { type: "pointerdown", pointerId: 2, button: 0, buttons: 2, clientX: 300, clientY: 300 });
		box.dispatch("pointermove", { type: "pointermove", pointerId: 2, buttons: 2, clientX: 100, clientY: 100 });
		box.dispatch("pointerup", { type: "pointerup", pointerId: 2, buttons: 0, clientX: 100, clientY: 100 });
		expect(h.hooks.viewerState.roi).toEqual(before); // 只有 pointerId=1 能动
		expect(box.hasPointerCapture(1)).toBe(true);

		box.dispatch("pointermove", { type: "pointermove", pointerId: 1, buttons: 1, clientX: 520, clientY: 410 });
		expect(h.hooks.viewerState.roi).toEqual({ x: 470, y: 370, w: 100, h: 80 });

		box.dispatch("pointercancel", { type: "pointercancel", pointerId: 1, buttons: 1 });
		expect(h.hooks.viewerState.roi).toEqual(before);
		expect(h.viewer.mouseNavEnabled).toBe(true);
		expect(h.posts).toEqual([]);
	});

	it("右键/中键不在框上开启拖拽（事件不消费）", async () => {
		const h = await bootApp();
		activateRectTool(h);
		placePresetDraft(h);
		const box = roiBoxOf(h);
		box.dispatch("pointerdown", { type: "pointerdown", pointerId: 1, button: 2, buttons: 2, clientX: 500, clientY: 400 });
		expect(box.hasPointerCapture(1)).toBe(false);
		box.dispatch("pointermove", { type: "pointermove", pointerId: 1, buttons: 2, clientX: 700, clientY: 500 });
		expect(h.hooks.viewerState.roi).toEqual({ x: 450, y: 360, w: 100, h: 80 });
	});

	it("window blur 收尾：恢复拖前几何、清监听；返回后可重新开新手势", async () => {
		const h = await bootApp();
		activateRectTool(h);
		placePresetDraft(h);
		const box = roiBoxOf(h);
		const before = { ...h.hooks.viewerState.roi };

		box.dispatch("pointerdown", { type: "pointerdown", pointerId: 1, button: 0, buttons: 1, clientX: 500, clientY: 400 });
		box.dispatch("pointermove", { type: "pointermove", pointerId: 1, buttons: 1, clientX: 550, clientY: 420 });
		expect(h.winListeners["blur"]?.length).toBeGreaterThan(0);
		h.winListeners["blur"].forEach((cb) => cb());

		expect(h.hooks.viewerState.roi).toEqual(before);
		expect(box.listenerCount("pointermove")).toBe(0);
		expect(box.listenerCount("pointerup")).toBe(0);
		expect(box.listenerCount("pointercancel")).toBe(0);
		expect(box.listenerCount("lostpointercapture")).toBe(0);

		// 回到窗口后同一指针可以重新拖动
		box.dispatch("pointerdown", { type: "pointerdown", pointerId: 1, button: 0, buttons: 1, clientX: 500, clientY: 400 });
		expect(box.hasPointerCapture(1)).toBe(true);
		box.dispatch("pointermove", { type: "pointermove", pointerId: 1, buttons: 1, clientX: 530, clientY: 415 });
		expect(h.hooks.viewerState.roi).toEqual({ x: 480, y: 375, w: 100, h: 80 });
	});

	it("setPointerCapture 失败：document 兜底监听接管收尾，结束时移除", async () => {
		const h = await bootApp();
		activateRectTool(h);
		placePresetDraft(h);
		const box = roiBoxOf(h);
		const before = { ...h.hooks.viewerState.roi };
		const baseMove = h.doc.listenerCount("pointermove");
		const baseUp = h.doc.listenerCount("pointerup");
		const baseCancel = h.doc.listenerCount("pointercancel");
		box.releaseShouldThrow = true;

		box.dispatch("pointerdown", { type: "pointerdown", pointerId: 7, button: 0, buttons: 1, clientX: 500, clientY: 400 });
		// 兜底监听挂 document 捕获阶段（相对基线 +1）
		expect(h.doc.listenerCount("pointermove")).toBe(baseMove + 1);
		expect(h.doc.listenerCount("pointerup")).toBe(baseUp + 1);
		expect(h.doc.listenerCount("pointercancel")).toBe(baseCancel + 1);
		expect(h.viewer.mouseNavEnabled).toBe(false);

		h.doc.dispatch("pointermove", { type: "pointermove", pointerId: 7, buttons: 1, clientX: 555, clientY: 425 });
		expect(h.hooks.viewerState.roi).toEqual({ x: 505, y: 385, w: 100, h: 80 });

		// 释放事件由兜底收到（元素自身收不到也算完成）
		h.doc.dispatch("pointerup", { type: "pointerup", pointerId: 7, buttons: 0, clientX: 555, clientY: 425 });
		expect(h.hooks.viewerState.roi).toEqual({ x: 505, y: 385, w: 100, h: 80 });
		expect(h.doc.listenerCount("pointermove")).toBe(baseMove);
		expect(h.doc.listenerCount("pointerup")).toBe(baseUp);
		expect(h.doc.listenerCount("pointercancel")).toBe(baseCancel);
		expect(h.viewer.mouseNavEnabled).toBe(true);

		// 兜底路径同样尊重 pointerId（其他指针不终结别人的手势）
		box.dispatch("pointerdown", { type: "pointerdown", pointerId: 9, button: 0, buttons: 1, clientX: 500, clientY: 400 });
		h.doc.dispatch("pointerup", { type: "pointerup", pointerId: 8, buttons: 0, clientX: 500, clientY: 400 });
		expect(h.doc.listenerCount("pointerup")).toBe(baseUp + 1); // 手势仍在
		h.doc.dispatch("pointercancel", { type: "pointercancel", pointerId: 9 });
		// 第二次手势的拖前几何 = 第一次完成后的几何（取消按各自手势起点恢复）
		expect(h.hooks.viewerState.roi).toEqual({ x: 505, y: 385, w: 100, h: 80 });
		expect(h.doc.listenerCount("pointerup")).toBe(baseUp);
	});

	it("拖动中退出工具/撤稿：会话一并清理，残留 move/up 不再生效（幂等）", async () => {
		const h = await bootApp();
		activateRectTool(h);
		placePresetDraft(h);
		const box = roiBoxOf(h);

		box.dispatch("pointerdown", { type: "pointerdown", pointerId: 1, button: 0, buttons: 1, clientX: 500, clientY: 400 });
		box.dispatch("pointermove", { type: "pointermove", pointerId: 1, buttons: 1, clientX: 520, clientY: 410 });

		// Escape：先撤草稿（会话收尾）——onViewerKeydown 注册在 window
		fireWin(h, "keydown", { key: "Escape" });
		expect(h.hooks.viewerState.roiMode).toBe("rect"); // 第一次 Escape 只撤稿
		expect(h.hooks.viewerState.roi).toEqual({ x: 0, y: 0, w: 0, h: 0 });
		expect(box.parentNode).toBeNull();

		// 残留事件：无会话可落
		box.dispatch("pointermove", { type: "pointermove", pointerId: 1, buttons: 1, clientX: 900, clientY: 700 });
		box.dispatch("pointerup", { type: "pointerup", pointerId: 1, buttons: 0, clientX: 900, clientY: 700 });
		expect(h.hooks.viewerState.roi).toEqual({ x: 0, y: 0, w: 0, h: 0 });
		expect(box.listenerCount("pointermove")).toBe(0);

		// 再次 Escape 退出工具（幂等收尾不抛错）
		fireWin(h, "keydown", { key: "Escape" });
		expect(h.hooks.viewerState.roiMode).toBeNull();
	});
});

// ---------------------------------------------------------------- 画框（anno-canvas）

describe("初始自由画框手势（§3.2：捕获丢失不保存，正常完成恰好保存一次）", () => {
	it("正常画框松开：自动保存恰好一次，几何与视觉一致", async () => {
		const h = await bootApp();
		activateRectTool(h);
		const canvas = h.doc.getElementById("anno-canvas");

		canvas.dispatch("pointerdown", { type: "pointerdown", pointerId: 1, button: 0, buttons: 1, clientX: 200, clientY: 200 });
		canvas.dispatch("pointermove", { type: "pointermove", pointerId: 1, buttons: 1, clientX: 300, clientY: 280 });
		canvas.dispatch("pointerup", { type: "pointerup", pointerId: 1, buttons: 0, clientX: 300, clientY: 280 });
		expect(h.hooks.viewerState.roi).toEqual({ x: 200, y: 200, w: 100, h: 80 });
		expect(h.posts.length).toBe(1);
		expect(h.posts[0]).toMatchObject({ type: "rect", x: 200, y: 200, w: 100, h: 80 });

		// 保存完成后的悬停不再提交/改写
		await settle();
		expect(h.posts.length).toBe(1);
	});

	it("画框中丢失捕获：不保存，恢复空选区并移除临时框；旧草稿场景恢复旧几何", async () => {
		const h = await bootApp();
		activateRectTool(h);
		const canvas = h.doc.getElementById("anno-canvas");

		// 空状态画框被打断 → 回到空
		canvas.dispatch("pointerdown", { type: "pointerdown", pointerId: 1, button: 0, buttons: 1, clientX: 200, clientY: 200 });
		canvas.dispatch("pointermove", { type: "pointermove", pointerId: 1, buttons: 1, clientX: 320, clientY: 300 });
		canvas.dispatch("lostpointercapture", { type: "lostpointercapture", pointerId: 1, buttons: 1 });
		expect(h.hooks.viewerState.roi).toEqual({ x: 0, y: 0, w: 0, h: 0 });
		expect(h.doc.created.filter((e) => e.id === "roi-box").every((e) => e.parentNode === null)).toBe(true);
		canvas.dispatch("pointerup", { type: "pointerup", pointerId: 1, buttons: 0, clientX: 10, clientY: 10 });
		expect(h.posts).toEqual([]);

		// 已有草稿（预设放置）上重新画框被打断 → 恢复旧草稿，不丢不存
		placePresetDraft(h);
		const before = { ...h.hooks.viewerState.roi };
		canvas.dispatch("pointerdown", { type: "pointerdown", pointerId: 2, button: 0, buttons: 1, clientX: 100, clientY: 100 });
		canvas.dispatch("pointermove", { type: "pointermove", pointerId: 2, buttons: 1, clientX: 400, clientY: 400 });
		canvas.dispatch("lostpointercapture", { type: "lostpointercapture", pointerId: 2, buttons: 1 });
		expect(h.hooks.viewerState.roi).toEqual(before);
		expect(h.posts).toEqual([]);
	});

	it("画框中 pointercancel / blur：不保存并恢复；随后可立即重新画框", async () => {
		const h = await bootApp();
		activateRectTool(h);
		const canvas = h.doc.getElementById("anno-canvas");

		canvas.dispatch("pointerdown", { type: "pointerdown", pointerId: 1, button: 0, buttons: 1, clientX: 200, clientY: 200 });
		canvas.dispatch("pointermove", { type: "pointermove", pointerId: 1, buttons: 1, clientX: 300, clientY: 280 });
		h.winListeners["blur"].forEach((cb) => cb());
		expect(h.hooks.viewerState.roi).toEqual({ x: 0, y: 0, w: 0, h: 0 });
		expect(h.posts).toEqual([]);

		// 立即重新画框：新手势完整可用
		canvas.dispatch("pointerdown", { type: "pointerdown", pointerId: 1, button: 0, buttons: 1, clientX: 500, clientY: 300 });
		canvas.dispatch("pointermove", { type: "pointermove", pointerId: 1, buttons: 1, clientX: 600, clientY: 380 });
		canvas.dispatch("pointerup", { type: "pointerup", pointerId: 1, buttons: 0, clientX: 600, clientY: 380 });
		expect(h.hooks.viewerState.roi).toEqual({ x: 500, y: 300, w: 100, h: 80 });
		expect(h.posts.length).toBe(1);
	});

	it("画框中收到其他指针事件：忽略；buttons=0 的 move 终止且不保存", async () => {
		const h = await bootApp();
		activateRectTool(h);
		const canvas = h.doc.getElementById("anno-canvas");

		canvas.dispatch("pointerdown", { type: "pointerdown", pointerId: 1, button: 0, buttons: 1, clientX: 200, clientY: 200 });
		canvas.dispatch("pointermove", { type: "pointermove", pointerId: 2, buttons: 1, clientX: 800, clientY: 700 });
		expect(h.hooks.viewerState.roi).toEqual({ x: 0, y: 0, w: 0, h: 0 }); // 别的指针画不了
		canvas.dispatch("pointermove", { type: "pointermove", pointerId: 1, buttons: 1, clientX: 300, clientY: 280 });
		canvas.dispatch("pointermove", { type: "pointermove", pointerId: 1, buttons: 0, clientX: 400, clientY: 400 });
		canvas.dispatch("pointerup", { type: "pointerup", pointerId: 1, buttons: 0, clientX: 400, clientY: 400 });
		expect(h.hooks.viewerState.roi).toEqual({ x: 0, y: 0, w: 0, h: 0 });
		expect(h.posts).toEqual([]);
	});

	it("中键不启动画框；换片清理活动会话（旧事件不落到新切片）", async () => {
		const h = await bootApp();
		activateRectTool(h);
		const canvas = h.doc.getElementById("anno-canvas");

		canvas.dispatch("pointerdown", { type: "pointerdown", pointerId: 1, button: 1, buttons: 4, clientX: 200, clientY: 200 });
		canvas.dispatch("pointermove", { type: "pointermove", pointerId: 1, buttons: 4, clientX: 300, clientY: 280 });
		expect(h.hooks.viewerState.roi).toEqual({ x: 0, y: 0, w: 0, h: 0 });

		// 带活动会话换片：info 回包后的 exitRoi 收尾，旧 pointerup 无处可落
		canvas.dispatch("pointerdown", { type: "pointerdown", pointerId: 1, button: 0, buttons: 1, clientX: 200, clientY: 200 });
		canvas.dispatch("pointermove", { type: "pointermove", pointerId: 1, buttons: 1, clientX: 300, clientY: 280 });
		h.hooks.openSlide(SLIDE);
		await settle();
		expect(h.hooks.viewerState.roiMode).toBeNull();
		canvas.dispatch("pointerup", { type: "pointerup", pointerId: 1, buttons: 0, clientX: 300, clientY: 280 });
		expect(h.posts).toEqual([]);
		expect(h.hooks.viewerState.roi).toEqual({ x: 0, y: 0, w: 0, h: 0 });
	});
});
