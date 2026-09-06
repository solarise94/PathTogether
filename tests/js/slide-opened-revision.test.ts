/**
 * slide.opened 携带资产 revision（切片替换保护，宿主侧口径）。
 *
 * 加载真实 static/app.js（最小 DOM + fetch mock + HostBridgeHost stub），
 * 经生产路径驱动：init → loadAll 渲染切片行 → 行 click → openSlide（拉 info）
 * → viewer.open → OSD "open" 事件 → onViewerOpen hpEmit("slide.opened")。锁定：
 *   - info 含 asset_revision（服务端口径 "mtime_ns:size"）→ slide.opened 载荷
 *     slide.revision 与 info.asset_revision 一致（插件快照回看比对
 *     view.slide_revision 的依据）；
 *   - info 缺 asset_revision（render fields 读取失败等边缘路径）→ revision
 *     为 null，事件照发、不抛错；
 *   - asset_revision 为空串 → 归一化为 null（|| 宽容口径）。
 */
import { afterEach, describe, expect, it, vi } from "vitest";
import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const appSrc = readFileSync(resolve(here, "../../static/app.js"), "utf8");

const SLIDE = "slide-a.ndpi";
const REVISION = "1757000000000000000:42";

// ---------- 假元素：记录属性/监听器（含 canvas ctx 记录器） ----------
interface FakeEl extends Record<string, unknown> {
	id: string;
	className: string;
	hidden: boolean;
	title: string;
	type: string;
	checked: boolean;
	style: Record<string, string>;
	dataset: Record<string, string>;
	textContent: string;
	innerHTML: string;
	value: string;
	children: FakeEl[];
	parentNode: FakeEl | null;
	classList: {
		add: (...names: string[]) => void;
		remove: (...names: string[]) => void;
		contains: (n: string) => boolean;
		toggle: (n: string, force?: boolean) => boolean;
	};
	setAttribute: (k: string, v: string) => void;
	getAttribute: (k: string) => string | null;
	addEventListener: (type: string, cb: (e?: unknown) => void) => void;
	dispatch: (type: string, evt?: unknown) => void;
	appendChild: (c: unknown) => void;
	insertBefore: (c: unknown, ref: unknown) => void;
	remove: () => void;
	querySelector: () => null;
	querySelectorAll: () => FakeEl[];
	closest: () => null;
	getBoundingClientRect: () => { width: number; height: number; left: number; top: number };
	getContext: () => Record<string, unknown>;
}

function fakeEl(id = ""): FakeEl {
	const listeners: Record<string, Array<(e?: unknown) => void>> = {};
	const children: FakeEl[] = [];
	// 生产代码用 className="..." 与 classList.add/remove 两种途径写类名；
	// className 需与 classList 同源（app.js renderSlideRow 以 className 建行）
	const classes = new Set<string>();
	const attrs = new Map<string, string>();
	const el: FakeEl = {
		id,
		hidden: false,
		title: "",
		type: "",
		checked: false,
		style: {},
		dataset: {},
		textContent: "",
		innerHTML: "",
		value: "",
		children,
		parentNode: null,
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
		dispatch: (type, evt) => (listeners[type] || []).forEach((cb) => cb(evt)),
		appendChild: (c) => {
			if (c && typeof c === "object" && "dispatch" in (c as object)) {
				(c as FakeEl).parentNode = el;
				children.push(c as FakeEl);
			}
			return c;
		},
		insertBefore: (c) => {
			if (c && typeof c === "object" && "dispatch" in (c as object)) {
				(c as FakeEl).parentNode = el;
				children.push(c as FakeEl);
			}
			return c;
		},
		remove() {},
		querySelector: () => null,
		querySelectorAll: () => [],
		closest: () => null,
		getBoundingClientRect: () => ({ width: 800, height: 600, left: 0, top: 0 }),
		getContext: () => ({
			clearRect() {},
			save() {},
			restore() {},
			setTransform() {},
			setLineDash() {},
			strokeRect() {},
			fillRect() {},
			fillText() {},
			measureText: (t: string) => ({ width: String(t).length * 6 }),
			drawImage() {},
		}),
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

function jsonResponse(body: unknown) {
	return Promise.resolve({
		ok: true,
		status: 200,
		clone() { return this; },
		json: () => Promise.resolve(body),
	});
}

interface EmittedEvent {
	type: string;
	payload: Record<string, unknown>;
}

interface BootResult {
	emitted: EmittedEvent[];
	openUrls: string[];
	created: FakeEl[];
	handlers: Record<string, Array<(e?: unknown) => void>>;
	findSlideRow: () => FakeEl;
}

// info：/api/slide/<name>/info 响应体（asset_revision 可选，模拟边缘路径）
function bootApp(info: Record<string, unknown>): BootResult {
	const els: Record<string, FakeEl> = {};
	const created: FakeEl[] = [];
	const docListeners: Record<string, Array<() => void>> = {};
	const rafCbs: Array<() => void> = [];
	const emitted: EmittedEvent[] = [];
	const openUrls: string[] = [];

	const handlers: Record<string, Array<(e?: unknown) => void>> = {};
	const fakeViewer = {
		container: {
			style: {} as Record<string, string>,
			getBoundingClientRect: () => ({ width: 800, height: 600, left: 0, top: 0 }),
			insertBefore() {},
		},
		canvas: {},
		viewport: null,
		addHandler(type: string, fn: (e?: unknown) => void) {
			(handlers[type] ||= []).push(fn);
		},
		// 生产里 viewer.open 在 tile source 就绪后异步触发 "open" 事件；
		// harness 同步触发即可（onViewerOpen 全程同步到 hpEmit）。
		open(url: string) {
			openUrls.push(url);
			(handlers["open"] || []).forEach((fn) => fn({}));
		},
		setMouseNavEnabled() {},
	};

	const fetchImpl = vi.fn((url: string) => {
		const u = String(url);
		if (u.includes("/api/slide/" + SLIDE + "/info")) return jsonResponse(info);
		if (u.includes("/api/annotations?slide=")) return jsonResponse({ annotations: [] });
		if (u.includes("/api/annotations")) return jsonResponse({ by_slide: {} });
		if (u.includes("/api/slides")) {
			return jsonResponse([{ name: SLIDE, width: 1000, height: 800, mpp_x: 0.5, mpp_source: "native" }]);
		}
		if (u.includes("/api/projects")) {
			return jsonResponse([{ pid: "p1", name: "P1", slides: [SLIDE], slide_count: 1, roi_count: 0 }]);
		}
		if (u.includes("/api/share/list")) return jsonResponse({ shares: [] });
		return jsonResponse({});
	}) as unknown as typeof fetch;

	const doc = {
		readyState: "loading",
		cookie: "",
		getElementById(id: string) {
			if (!els[id]) els[id] = fakeEl(id);
			return els[id];
		},
		createElement: () => {
			const el = fakeEl();
			created.push(el);
			return el;
		},
		addEventListener(type: string, cb: () => void) {
			(docListeners[type] ||= []).push(cb);
		},
		querySelector: () => null,
		querySelectorAll: () => [] as FakeEl[],
		body: fakeEl("body"),
	};

	// HostBridgeHost stub：onRequest/onEvent 容纳 registerHostBridgeHandlers 注册，
	// emit 捕获 Host→Plugin 事件（slide.opened 断言数据源）
	const w: Record<string, unknown> = {
		HP_I18N: { t: (k: string) => k, getLang: () => "zh" },
		HP_ViewerCore: { create: () => fakeViewer },
		HP_API: {},
		HistoPilot: {},
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
		fetch: fetchImpl,
		location: { href: "http://local/", pathname: "/" },
		matchMedia: () => ({ matches: false, addEventListener() {}, addListener() {} }),
		requestAnimationFrame: (cb: () => void) => {
			rafCbs.push(cb);
			return rafCbs.length;
		},
		addEventListener() {},
		localStorage: null,
	};

	(globalThis as { document: unknown }).document = doc;
	(globalThis as { window: unknown }).window = w;
	(globalThis as { fetch: typeof fetch }).fetch = fetchImpl;
	// app.js initViewer 用裸标识符 HP_ViewerCore（浏览器=window 属性）
	(globalThis as { HP_ViewerCore?: unknown }).HP_ViewerCore = { create: () => fakeViewer };

	// eslint-disable-next-line @typescript-eslint/no-explicit-any
	new Function("window", "document", "fetch", "location", appSrc)(w, doc, fetchImpl, (w as { location: unknown }).location);

	// DOMContentLoaded → init()；刷新挂起的 rAF
	(docListeners["DOMContentLoaded"] || []).forEach((cb) => cb());
	while (rafCbs.length) (rafCbs.shift() as () => void)();

	const findSlideRow = () => {
		const row = created.find((e) => e.classList.contains("slide-row"));
		if (!row) throw new Error("harness: 未渲染出 .slide-row（生产路径 loadAll → renderProjects → renderSlideRow）");
		return row;
	};
	return { emitted, openUrls, created, handlers, findSlideRow };
}

async function settle(times = 20) {
	for (let i = 0; i < times; i++) await Promise.resolve();
}

function slideOpened(emitted: EmittedEvent[]): { slide: Record<string, unknown> } {
	const ev = emitted.find((e) => e.type === "slide.opened");
	expect(ev, "slide.opened 事件应已发出").toBeTruthy();
	return ev!.payload as unknown as { slide: Record<string, unknown> };
}

describe("slide.opened 携带资产 revision（切片替换保护）", () => {
	afterEach(() => {
		vi.restoreAllMocks();
		delete (globalThis as { window?: unknown }).window;
		delete (globalThis as { document?: unknown }).document;
		delete (globalThis as { fetch?: unknown }).fetch;
		delete (globalThis as { HP_ViewerCore?: unknown }).HP_ViewerCore;
	});

	it("info 含 asset_revision → slide.opened 载荷 slide.revision 与之一致（含其余既有字段）", async () => {
		const app = bootApp({
			name: SLIDE,
			width: 1000,
			height: 800,
			mpp_x: 0.5,
			mpp_y: 0.5,
			mpp_source: "native",
			asset_revision: REVISION,
		});
		await settle();
		app.findSlideRow().dispatch("click");
		await settle();

		// 生产路径确实走到了 legacy viewer.open（harness 触发 OSD "open" 事件）
		expect(app.openUrls).toEqual(["/api/slide/" + SLIDE + ".dzi"]);
		const opened = app.emitted.filter((e) => e.type === "slide.opened");
		expect(opened.length).toBe(1);
		expect(slideOpened(app.emitted)).toEqual({
			slide: {
				name: SLIDE,
				width: 1000,
				height: 800,
				mppX: 0.5,
				mppY: 0.5,
				revision: REVISION,
			},
		});
	});

	it("info 缺 asset_revision（render fields 读取失败等边缘路径）→ revision=null，事件照发不抛错", async () => {
		const app = bootApp({
			name: SLIDE, width: 1000, height: 800, mpp_x: 0.5, mpp_y: 0.5, mpp_source: "native",
		});
		await settle();
		app.findSlideRow().dispatch("click");
		await settle();

		expect(app.emitted.some((e) => e.type === "slide.opened")).toBe(true);
		expect(slideOpened(app.emitted).slide.revision).toBe(null);
	});

	it("asset_revision 为空串 → 归一化为 null（|| 宽容口径，不下发空 revision）", async () => {
		const app = bootApp({
			name: SLIDE, width: 1000, height: 800, mpp_x: 0.5, mpp_y: 0.5,
			mpp_source: "native", asset_revision: "",
		});
		await settle();
		app.findSlideRow().dispatch("click");
		await settle();

		expect(slideOpened(app.emitted).slide.revision).toBe(null);
	});
});
