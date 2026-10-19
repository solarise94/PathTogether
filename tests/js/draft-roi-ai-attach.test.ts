/**
 * 未保存候选框的「✨ 加入 AI 对话」（ROI 附件 2026-10-18）——加载真实
 * static/app.js（harness 同 rect-pointer-session.test.ts：最小 DOM + fetch
 * mock + HostBridgeHost emit 捕获；__PT_TEST_HOOKS 暴露 viewerState），
 * 经生产路径驱动：openSlide → 矩形工具 → 预设放置（就近工具条出现）→ 点击
 * ✨。锁定：
 *   - 布局：draft 工具条 [保存为标记] [✨ 加入 AI 对话] [锁定位置] [⋯]——
 *     保存仍为主动作，加入对话是次动作；
 *   - 点击 ✨（点击时刻冻结）→ HostBridge emit conversation.attachIntent，
 *     载荷 kind=roi：bbox=当前草稿 level-0 外接框、slide_id+slide 身份、
 *     center 由 bbox 推导；**不伪造 annotation_id/revision**，type/geometry/
 *     note/label 全 null（未保存草稿没有标注语义）；
 *   - 冻结语义：加入后再拖动草稿 → 已发出的载荷原样不变（独立快照），
 *     再次点击才带新几何；保存为标记（POST /api/annotation）也不回写
 *     已冻结附件——保存后的标记要加入对话走 marker 入口重新冻结；
 *   - 插件未加载（hpReady=false）→ ✨ 禁用 + 「插件未加载」tooltip，
 *     点击不 emit；加入成功 toast 为 roi 专属文案键。
 */
import { afterEach, describe, expect, it, vi } from "vitest";
import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const appSrc = readFileSync(resolve(here, "../../static/app.js"), "utf8");
const geoSrc = readFileSync(resolve(here, "../../static/anno-geometry.js"), "utf8");
const i18nSrc = readFileSync(resolve(here, "../../static/i18n.js"), "utf8");

const SLIDE = "rect-draft.bmp";
const SLIDE_ID = "sld_rect000001";

// ---------- 元素桩（裁自 rect-pointer-session.test.ts：真实监听表 + 可编程指针捕获） ----------
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
	querySelector: () => null;
	querySelectorAll: () => FakeEl[];
	closest: () => null;
	focus: () => void;
	getBoundingClientRect: () => { width: number; height: number; left: number; top: number };
	setPointerCapture: (pid: number) => void;
	releasePointerCapture: (pid: number) => void;
	hasPointerCapture: (pid: number) => boolean;
	releaseShouldThrow: boolean;
	capturedPointerId: number | null;
	released: number[];
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

// ---------- 文档桩 ----------
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
		},
		addHandler() {},
		open() {},
		updateOverlay(_el: unknown, _rect: unknown, _placement: unknown) {},
		addOverlay(opts: { element: unknown }) {
			overlays.push(opts.element);
		},
		getOverlayById(el: unknown) {
			return overlays.includes(el) ? { element: el } : null;
		},
		removeOverlay(el: unknown) {
			const i = overlays.indexOf(el);
			if (i >= 0) overlays.splice(i, 1);
		},
		close() {},
	};
	return { viewer, container };
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
	};
	doc: FakeDoc;
	viewer: ReturnType<typeof makeViewer>["viewer"];
	posts: Array<Record<string, unknown>>;
	emitted: Array<{ type: string; payload: Record<string, unknown> }>;
	winListeners: Record<string, Array<() => void>>;
	win: Record<string, unknown>;
	rafQueue: Array<() => void>;
}

async function bootApp(opts: { pluginLoaded?: boolean } = {}): Promise<Boot> {
	installOsd();
	const doc = fakeDoc();
	const mv = makeViewer();
	const posts: Array<Record<string, unknown>> = [];
	const emitted: Array<{ type: string; payload: Record<string, unknown> }> = [];
	const winListeners: Record<string, Array<() => void>> = {};
	const rafQueue: Array<() => void> = [];

	const fetchImpl = vi.fn((url: string, req?: { method?: string; body?: string }) => {
		const u = String(url);
		if (u.includes("/api/annotation") && (req?.method || "GET") === "POST") {
			posts.push(JSON.parse(req!.body || "{}") as Record<string, unknown>);
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
				ok: true,
				status: 200,
				clone() { return this; },
				json: () => Promise.resolve([{
					name: SLIDE, slide_id: SLIDE_ID, display_name: "",
					width: 1000, height: 800, mpp_x: 0.5, mpp_y: 0.5, mpp_source: "native",
				}]),
			});
		}
		if (u.includes("/api/projects")) {
			return Promise.resolve({
				ok: true,
				status: 200,
				clone() { return this; },
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
		HistoPilot: opts.pluginLoaded === false ? undefined : {},
		HostBridgeHost: {
			onRequest() {},
			onEvent() {},
			emit(type: string, payload: Record<string, unknown>) {
				emitted.push({ type, payload });
			},
			request() {
				return Promise.reject({ code: "plugin_disabled" });
			},
		},
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
		posts,
		emitted,
		winListeners,
		win: w,
		rafQueue,
	};
}

/** 执行挂起的 rAF 帧回调（布局/重绘同步）。 */
function flushRaf(h: Boot) {
	for (let i = 0; i < 10 && h.rafQueue.length; i++) {
		h.rafQueue.splice(0).forEach((cb) => cb());
	}
}

function roiBoxOf(h: Boot): FakeEl {
	const box = h.doc.created.filter((e) => e.id === "roi-box").pop();
	expect(box, "roi-box 必须已创建").toBeTruthy();
	return box as FakeEl;
}

function toolbarOf(h: Boot): FakeEl {
	const bar = h.doc.created.filter((e) => e.id === "anno-obj-toolbar").pop();
	expect(bar, "就近工具条必须已创建").toBeTruthy();
	return bar as FakeEl;
}

function btnByClass(h: Boot, needle: string): FakeEl {
	const bar = toolbarOf(h);
	const btn = bar.children.filter((b) => String(b.className).includes(needle)).pop();
	expect(btn, `工具条按钮 ${needle} 必须存在`).toBeTruthy();
	return btn as FakeEl;
}

function activateRectTool(h: Boot) {
	h.doc.getElementById("roi-rect-btn").dispatch("click", { type: "click" });
	expect(h.hooks.viewerState.roiMode).toBe("rect");
}

/** 预设尺寸放置：切到 px 单位，100×80，Enter 应用（草稿几何 {450,360,100,80}） */
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

function clickAskai(h: Boot) {
	btnByClass(h, "aot-askai").dispatch("click", { type: "click" });
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

// ---------------------------------------------------------------- 主流程

describe("未保存草稿的「✨ 加入 AI 对话」（ROI 附件 2026-10-18）", () => {
	it("draft 工具条布局：[保存为标记] [✨ 加入 AI 对话] [锁定位置] [⋯]——保存仍居首", async () => {
		const h = await bootApp();
		activateRectTool(h);
		placePresetDraft(h);
		flushRaf(h);
		const bar = toolbarOf(h);
		// 注：不锁 bar.hidden——updateAnnoToolbarPos 依赖真实 canvas 布局
		// （imgToCanvas/DPR），最小 DOM 桩不建模；工具条可见性由 e2e 锁定。
		const classes = bar.children.map((b) => String(b.className));
		expect(classes[0]).toContain("aot-save");
		expect(classes[1]).toContain("aot-askai");
		expect(classes[1]).toContain("aot-askai-draft");
		expect(classes[2]).toContain("aot-lock");
		expect(classes[3]).toContain("aot-more");
		// 次动作 tooltip 专属文案键（区分于标记版的「把此标记…」）
		expect(btnByClass(h, "aot-askai").title).toBe("anno.menu.askai.draft.tip");
	});

	it("点击 ✨ → emit conversation.attachIntent kind=roi：冻结草稿 bbox，不伪造标注字段", async () => {
		const h = await bootApp();
		activateRectTool(h);
		placePresetDraft(h);
		flushRaf(h);
		expect(h.emitted).toEqual([]);

		clickAskai(h);
		expect(h.emitted).toHaveLength(1);
		const ev = h.emitted[0]!;
		expect(ev.type).toBe("conversation.attachIntent");
		const p = ev.payload;
		expect(p.kind).toBe("roi");
		expect(p.bbox).toEqual({ x: 450, y: 360, w: 100, h: 80 });
		expect(p.slide_id).toBe(SLIDE_ID);
		expect(p.slide).toBe(SLIDE);
		expect(p.center).toEqual({ x: 500, y: 400 });   // 由 bbox 推导，非点击点位
		expect(p.click_point).toBeNull();
		// 未保存草稿没有标注语义：绝不伪造 annotation_id/revision
		expect(p.annotation_id).toBeNull();
		expect(p.revision).toBeNull();
		expect(p.type).toBeNull();
		expect(p.geometry).toBeNull();
		expect(p.note).toBeNull();
		expect(p.label).toBeNull();
		expect(typeof p.frozen_at).toBe("number");
		// 加入 ≠ 发送：无任何标注保存请求
		expect(h.posts).toEqual([]);
		// roi 专属成功 toast（文案键直出，i18n 键在 dedicated 用例断言 zh/en）
		const toasts = h.doc.getElementById("toast-container").children;
		expect(String(toasts[toasts.length - 1]!.textContent)).toBe("ctxmenu.attach.roi.added");
	});

	it("冻结语义：加入后再拖动草稿，已发出的附件载荷不变；再次点击才带新几何", async () => {
		const h = await bootApp();
		activateRectTool(h);
		placePresetDraft(h);
		flushRaf(h);

		clickAskai(h);
		const first = h.emitted[0]!.payload;

		// 拖动草稿到新几何（真实指针手势路径）
		const box = roiBoxOf(h);
		box.dispatch("pointerdown", { type: "pointerdown", pointerId: 1, button: 0, buttons: 1, clientX: 500, clientY: 400 });
		box.dispatch("pointermove", { type: "pointermove", pointerId: 1, buttons: 1, clientX: 560, clientY: 430 });
		box.dispatch("pointerup", { type: "pointerup", pointerId: 1, buttons: 0, clientX: 560, clientY: 430 });
		expect(h.hooks.viewerState.roi).toEqual({ x: 510, y: 390, w: 100, h: 80 });

		// 已冻结的附件不随草稿移动（独立快照，没有活引用）
		expect(first.bbox).toEqual({ x: 450, y: 360, w: 100, h: 80 });

		// 再次点击 ✨ → 新快照带新几何；两次互不影响
		clickAskai(h);
		expect(h.emitted).toHaveLength(2);
		expect(h.emitted[1]!.payload.bbox).toEqual({ x: 510, y: 390, w: 100, h: 80 });
		expect(h.emitted[0]!.payload.bbox).toEqual({ x: 450, y: 360, w: 100, h: 80 });
		expect(h.emitted[1]!.payload).not.toBe(first);
	});

	it("保存为标记不回写已冻结附件：POST 照常，roi 载荷仍无 annotation_id", async () => {
		const h = await bootApp();
		activateRectTool(h);
		placePresetDraft(h);
		flushRaf(h);

		clickAskai(h);
		const frozen = h.emitted[0]!.payload;

		// 保存草稿（主动作）→ 恰好一次 POST /api/annotation
		btnByClass(h, "aot-save").dispatch("click", { type: "click" });
		await settle();
		expect(h.posts).toHaveLength(1);
		expect(h.posts[0]!.type).toBe("rect");

		// 已冻结的 roi 载荷不被保存动作回写（仍无标注 id；要加保存后的标记
		// 应走 marker 入口重新冻结）
		expect(frozen.kind).toBe("roi");
		expect(frozen.annotation_id).toBeNull();
		expect(frozen.bbox).toEqual({ x: 450, y: 360, w: 100, h: 80 });
	});

	it("插件未加载（hpReady=false）→ ✨ 禁用 + 明确 tooltip；点击不 emit", async () => {
		const h = await bootApp({ pluginLoaded: false });
		activateRectTool(h);
		placePresetDraft(h);
		flushRaf(h);

		const askai = btnByClass(h, "aot-askai");
		expect(askai.disabled).toBe(true);
		expect(askai.title).toBe("ctxmenu.attach.plugin.missing");
		askai.dispatch("click", { type: "click" });
		expect(h.emitted).toEqual([]);
	});

	it("i18n：ROI 附件相关键 zh+en 就位", () => {
		for (const key of [
			"ctxmenu.attach.roi.added",
			"anno.menu.askai.draft.tip",
			"ai.attach.roi",
			"ai.attach.unknown",
		]) {
			expect(i18nSrc.match(new RegExp('"' + key + '": "')), `zh 缺 ${key}`).toBeTruthy();
			expect(i18nSrc.indexOf('"' + key + '"')).toBeGreaterThan(-1);
		}
		// zh 与 en 两处（zh 段在前，en 段在后；同名键各出现 ≥2 次）
		expect(i18nSrc.split('"ctxmenu.attach.roi.added"').length - 1).toBeGreaterThanOrEqual(2);
		expect(i18nSrc.split('"ai.attach.roi"').length - 1).toBeGreaterThanOrEqual(2);
		expect(i18nSrc.split('"anno.menu.askai.draft.tip"').length - 1).toBeGreaterThanOrEqual(2);
		expect(i18nSrc.split('"ai.attach.unknown"').length - 1).toBeGreaterThanOrEqual(2);
		expect(i18nSrc).toContain("已将未保存区域加入会话草稿");
		expect(i18nSrc).toContain("Unsaved region added to the conversation draft");
		expect(i18nSrc).toContain("未保存区域附件");
		expect(i18nSrc).toContain("Unsaved region attachment");
	});
});
