/**
 * slide.opened：多通道「本地持久化配色重开」丢事件修复 + name|revision 去重。
 *
 * 生产 bug（IAB 实测 3/3 丢失）：多通道切片的本地持久化选择 ≠ 服务端默认时，
 * channel-controls 的 handleInfo 先用默认 token 调 opts.open(plan)（第一次
 * viewer.open），随后【同步】执行 applySelection()：viewer.close()（吃掉在途
 * open 事件）+ onReopening()（channelReopening 置位）→ normalizeRenderContext
 * 异步返回后第二次 viewer.open（持久化配色生效的那次）。该次 "open" 事件只会
 * 从 onViewerOpen 的 channelReopening 轻量路径到达，旧代码在此直接 return、
 * 不发 slide.opened → 插件停留旧切片，会话恢复/切片替换保护全部失效。
 * 注意：若两次 open 事件都到达，旧代码并不丢（第一次消耗标志位、第二次走
 * 完整路径照发）——真正丢事件的是「close 吃掉第一次 open」这一生产时序，
 * 本 harness 的 fakeViewer 按真实 OSD 行为建模（open 异步入队、close 取消在途）。
 *
 * 修复语义：slide.opened =「插件应按此切片重置/恢复状态」，正常路径与轻量
 * 路径都经 emitSlideOpened() 按 name|revision 键去重：
 *   - 纯换配色重开（同切片同 revision）→ 键相同跳过（不重置插件 AI 会话）；
 *   - 上述生产时序 → 无论哪次 open 事件到达，插件尚未收到当前切片+revision
 *     就补发（恰好一次）；
 *   - 同名文件替换（revision 变化）或切换切片 → 键变化必然重发。
 *
 * harness（复用 slide-opened-revision.test.ts 的真实 app.js 模式，扩展）：
 *   - 加载真实 static/channel-controls.js（多通道流程走生产代码，非 stub 复刻）；
 *   - fakeViewer.open 只入队（模拟 OSD tile source 异步就绪），viewer.close()
 *     丢弃在途 open 事件，测试经 deliverOpens() 控制事件到达时机；
 *   - 包装 HP_Channels.createChannelController 捕获 app.js 传入的 opts（含
 *     onReopening 引用）与控制器实例（驱动真实 setChannelColor 换配色重开）。
 */
import { afterEach, describe, expect, it, vi } from "vitest";
import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const appSrc = readFileSync(resolve(here, "../../static/app.js"), "utf8");
const channelsSrc = readFileSync(resolve(here, "../../static/channel-controls.js"), "utf8");

const SLIDE_A = "slide-a.ndpi";
const SLIDE_B = "slide-b.ndpi";
const REV1 = "1757000000000000000:42";
const REV2 = "1757000001000000000:43";

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
	appendChild: (c: unknown) => unknown;
	insertBefore: (c: unknown, ref: unknown) => unknown;
	removeChild: (c: FakeEl) => unknown;
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
		// channel-controls renderPanel 重渲前 removePanel 清空宿主子节点
		removeChild: (c) => {
			const i = children.indexOf(c);
			if (i >= 0) children.splice(i, 1);
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

interface StorageLike {
	getItem: (k: string) => string | null;
	setItem: (k: string, v: string) => void;
	removeItem: (k: string) => void;
}

function fakeStorage(): StorageLike {
	const m = new Map<string, string>();
	return {
		getItem: (k) => (m.has(k) ? (m.get(k) as string) : null),
		setItem: (k, v) => void m.set(k, String(v)),
		removeItem: (k) => void m.delete(k),
	};
}

interface EmittedEvent {
	type: string;
	payload: Record<string, unknown>;
}

// ---------- info fixtures ----------
// RGB/legacy 切片（无通道字段 → handleInfo 返回 legacy，app.js 走原 DZI 路径）
function plainInfo(revision: string, name = SLIDE_A): Record<string, unknown> {
	return {
		name,
		slide_id: name,
		width: 1000,
		height: 800,
		mpp_x: 0.5,
		mpp_y: 0.5,
		mpp_source: "native",
		asset_revision: revision,
	};
}

// 多通道切片（4 通道；服务端默认 context 启用 [0,1,2,3]）
function multichannelInfo(revision: string, name = SLIDE_A): Record<string, unknown> {
	return Object.assign(plainInfo(revision, name), {
		image_mode: "multichannel",
		server_capability: { render_context_endpoint: true },
		channels: [0, 1, 2, 3].map((i) => ({
			index: i,
			name: "C" + (i + 1),
			color_source: "ome",
			color: ["#00FFFF", "#FF00FF", "#FFD166", "#00E676"][i],
			alpha: 1,
			intensity: { status: "ok", black: 0, white: 255 },
			default_active: true,
		})),
		deepzoom: { width: 1000, height: 800, tile_size: 512, overlap: 1, min_level: 0, max_level: 8 },
		default_render_context: {
			active_channels: [{ index: 0 }, { index: 1 }, { index: 2 }, { index: 3 }],
		},
		default_render_token: "tok-default",
		warnings: [],
		plane: { t: 1, z: 1 },
	});
}

// channel-controls storageKey 口径：scope=currentUserId 为 null（harness 的
// /api/auth/info 返回 {}）→ "official:local"
function persistedSelectionKey(slideId: string, revision: string): string {
	return "pt.rc.v1|official:local|" + slideId + "|" + revision;
}

// 本地持久化配色：[0,2] 通道 + 自定义色（≠ 默认 [0,1,2,3]，触发 applySelection 重开）
function persistedSelection(): string {
	return JSON.stringify({
		v: 1,
		selection: [0, 2],
		overrides: { "0": { color: "#FF0000", alpha: 1 } },
	});
}

interface BootResult {
	emitted: EmittedEvent[];
	openArgs: unknown[];
	closeCount: () => number;
	pendingOpens: () => number;
	deliverOpens: (times?: number) => void;
	channelOpts: () => { onReopening?: () => void } | null;
	channelCtrl: () => { setChannelColor: (index: number, color: string) => boolean } | null;
	findSlideRow: (name?: string) => FakeEl;
}

function bootApp(opts: {
	infoByName: Record<string, Record<string, unknown>>;
	slides?: Array<Record<string, unknown>>;
	storage?: StorageLike;
}): BootResult {
	const infoByName = opts.infoByName;
	const els: Record<string, FakeEl> = {};
	const created: FakeEl[] = [];
	const docListeners: Record<string, Array<() => void>> = {};
	const rafCbs: Array<() => void> = [];
	const emitted: EmittedEvent[] = [];

	// ---- fakeViewer：open 事件入队（OSD tile source 异步就绪），close 取消在途 ----
	const handlers: Record<string, Array<(e?: unknown) => void>> = {};
	let pending = 0;
	let closes = 0;
	const openArgs: unknown[] = [];
	const viewer: Record<string, unknown> = {
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
		removeHandler(type: string, fn: (e?: unknown) => void) {
			handlers[type] = (handlers[type] || []).filter((h) => h !== fn);
		},
		// channel-controls applySelection 用 addOnceHandler 在重开后恢复 viewport
		addOnceHandler(type: string, fn: (e?: unknown) => void) {
			const wrap = (e?: unknown) => {
				(viewer as { removeHandler: (t: string, f: (e?: unknown) => void) => void })
					.removeHandler(type, wrap);
				fn(e);
			};
			(handlers[type] ||= []).push(wrap);
		},
		// 生产里 viewer.open 在 tile source 就绪后才触发 "open"（异步）→ 只入队
		open(src: unknown) {
			openArgs.push(src);
			pending += 1;
		},
		// 生产里 viewer.close() 取消在途 open（其 "open" 事件不再触发）并触发 "close"
		close() {
			closes += 1;
			pending = 0;
			(handlers["close"] || []).slice().forEach((fn) => fn({}));
		},
		setMouseNavEnabled() {},
	};
	// 送达在途 "open" 事件（默认全部；times 限制送达个数）
	const deliverOpens = (times = Infinity) => {
		let n = Math.min(times, pending);
		while (n-- > 0) {
			pending -= 1;
			(handlers["open"] || []).slice().forEach((fn) => fn({}));
		}
	};

	const fetchImpl = vi.fn((url: string) => {
		const u = String(url);
		const m = u.match(/\/api\/slide\/([^/]+)\/info/);
		if (m) {
			const name = decodeURIComponent(m[1]);
			return jsonResponse(infoByName[name] || { error: "not_found" });
		}
		if (u.includes("/api/annotations?slide=")) return jsonResponse({ annotations: [] });
		if (u.includes("/api/annotations")) return jsonResponse({ by_slide: {} });
		if (u.includes("/api/slides")) {
			return jsonResponse(opts.slides || [
				{ name: SLIDE_A, width: 1000, height: 800, mpp_x: 0.5, mpp_source: "native" },
			]);
		}
		if (u.includes("/api/projects")) {
			return jsonResponse([{
				pid: "p1",
				name: "P1",
				slides: Object.keys(infoByName),
				slide_count: Object.keys(infoByName).length,
				roi_count: 0,
			}]);
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

	const storage = opts.storage || fakeStorage();

	// HostBridgeHost stub：emit 捕获 Host→Plugin 事件（slide.opened 断言数据源）
	const w: Record<string, unknown> = {
		HP_I18N: { t: (k: string) => k, getLang: () => "zh" },
		HP_ViewerCore: { create: () => viewer },
		// channel-controls adapter：多通道流程只用到 normalizeRenderContext
		HP_API: {
			normalizeRenderContext: (_id: string, _body: unknown) =>
				jsonResponse({
					render_context: { fingerprint: "fp-user" },
					render_context_fingerprint: "fp-user",
					render_token: "tok-user",
				}),
		},
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
		localStorage: storage,
		document: doc,
	};

	(globalThis as { document: unknown }).document = doc;
	(globalThis as { window: unknown }).window = w;
	(globalThis as { fetch: typeof fetch }).fetch = fetchImpl;
	// app.js / channel-controls.js 用裸标识符引用浏览器全局（=window 属性）
	(globalThis as { HP_ViewerCore?: unknown }).HP_ViewerCore = { create: () => viewer };
	(globalThis as { HP_I18N?: unknown }).HP_I18N = w.HP_I18N;

	// 真实 channel-controls.js（挂到 window.HP_Channels），再包装捕获 app.js
	// 传入的 opts（含 onReopening）与控制器实例（驱动真实换配色流程）
	// eslint-disable-next-line @typescript-eslint/no-explicit-any
	new Function("window", channelsSrc)(w);
	// eslint-disable-next-line @typescript-eslint/no-explicit-any
	const realCreate = (w.HP_Channels as any).createChannelController as
		(o: Record<string, unknown>) => { setChannelColor: (i: number, c: string) => boolean };
	let channelOpts: { onReopening?: () => void } | null = null;
	let channelCtrl: { setChannelColor: (i: number, c: string) => boolean } | null = null;
	// eslint-disable-next-line @typescript-eslint/no-explicit-any
	(w.HP_Channels as any).createChannelController = function (o: Record<string, unknown>) {
		channelOpts = o;
		channelCtrl = realCreate(o);
		return channelCtrl;
	};
	(globalThis as { HP_Channels?: unknown }).HP_Channels = w.HP_Channels;

	// eslint-disable-next-line @typescript-eslint/no-explicit-any
	new Function("window", "document", "fetch", "location", appSrc)(w, doc, fetchImpl, (w as any).location);

	// DOMContentLoaded → init()；刷新挂起的 rAF
	(docListeners["DOMContentLoaded"] || []).forEach((cb) => cb());
	while (rafCbs.length) (rafCbs.shift() as () => void)();

	const findSlideRow = (name?: string) => {
		const row = created.find((e) =>
			e.classList.contains("slide-row") && (!name || e.dataset.name === name));
		if (!row) throw new Error("harness: 未渲染出 .slide-row（生产路径 loadAll → renderProjects → renderSlideRow）");
		return row;
	};
	return {
		emitted,
		openArgs,
		closeCount: () => closes,
		pendingOpens: () => pending,
		deliverOpens,
		// 控制器在首次 openSlide 时才创建（app.js 惰性 createChannelController），
		// 必须经 getter 取点击后的捕获值，不能在 boot 时快照
		channelOpts: () => channelOpts,
		channelCtrl: () => channelCtrl,
		findSlideRow,
	};
}

async function settle(times = 20) {
	for (let i = 0; i < times; i++) await Promise.resolve();
}

function slideOpenedEvents(emitted: EmittedEvent[]): Array<{ slide: Record<string, unknown> }> {
	return emitted
		.filter((e) => e.type === "slide.opened")
		.map((e) => e.payload as unknown as { slide: Record<string, unknown> });
}

const EXPECTED_SLIDE = {
	name: SLIDE_A,
	width: 1000,
	height: 800,
	mppX: 0.5,
	mppY: 0.5,
};

describe("slide.opened：多通道持久化配色重开不丢事件 + name|revision 去重", () => {
	afterEach(() => {
		vi.restoreAllMocks();
		const g = globalThis as Record<string, unknown>;
		delete g.window;
		delete g.document;
		delete g.fetch;
		delete g.HP_ViewerCore;
		delete g.HP_Channels;
		delete g.HP_I18N;
	});

	it("回归：正常打开切片 → 恰好一次 slide.opened（含 revision）", async () => {
		const app = bootApp({ infoByName: { [SLIDE_A]: plainInfo(REV1) } });
		await settle();
		app.findSlideRow().dispatch("click");
		await settle();

		// 生产路径：openSlide → legacy viewer.open（在途）→ OSD "open" 事件到达
		expect(app.openArgs).toEqual(["/api/slide/" + SLIDE_A + ".dzi"]);
		app.deliverOpens();

		const opened = slideOpenedEvents(app.emitted);
		expect(opened.length, "正常打开必须恰好发出一次 slide.opened").toBe(1);
		expect(opened[0]).toEqual({ slide: Object.assign({}, EXPECTED_SLIDE, { revision: REV1 }) });
	});

	it("bug 复现：多通道 + 本地持久化配色，首开默认 token 的 open 事件被 viewer.close 吃掉 → 持久化配色重开补发恰好一次", async () => {
		const storage = fakeStorage();
		storage.setItem(persistedSelectionKey(SLIDE_A, REV1), persistedSelection());
		const app = bootApp({ infoByName: { [SLIDE_A]: multichannelInfo(REV1) }, storage });
		await settle();
		app.findSlideRow().dispatch("click");
		await settle();

		// 生产时序已发生：handleInfo 用默认 token 打开（第 1 次 open，事件在途）
		// → applySelection 同步 viewer.close()（吃掉在途 open 事件）+ onReopening
		// （channelReopening 置位）→ 持久化配色的第 2 次 open 入队
		expect(app.channelOpts() && typeof app.channelOpts()!.onReopening).toBe("function");
		expect(app.openArgs.length, "默认 token + 持久化配色共两次 open").toBe(2);
		expect(app.closeCount(), "applySelection 同步 close 一次").toBe(1);
		expect(app.pendingOpens(), "第 1 次 open 事件被 close 吃掉，只剩第 2 次在途").toBe(1);

		app.deliverOpens();  // 第 2 次 open 的 "open" 事件到达（只会走轻量路径）

		const opened = slideOpenedEvents(app.emitted);
		expect(opened.length, "轻量路径必须补发 slide.opened（生产 3/3 丢失的 bug）").toBe(1);
		expect(opened[0]).toEqual({ slide: Object.assign({}, EXPECTED_SLIDE, { revision: REV1 }) });
	});

	it("去重（换配色不重置会话）：补发一次后，setChannelColor 换配色重开（同 name+revision）不再重发", async () => {
		const storage = fakeStorage();
		storage.setItem(persistedSelectionKey(SLIDE_A, REV1), persistedSelection());
		const app = bootApp({ infoByName: { [SLIDE_A]: multichannelInfo(REV1) }, storage });
		await settle();
		app.findSlideRow().dispatch("click");
		await settle();
		app.deliverOpens();
		expect(slideOpenedEvents(app.emitted).length).toBe(1);

		// 用户在通道面板改色 → 真实控制器 applySelection 重开（第 3 次 open）
		expect(app.channelCtrl(), "harness 应捕获到通道控制器实例").toBeTruthy();
		expect(app.channelCtrl()!.setChannelColor(0, "#00FF00")).toBe(true);
		await settle();
		expect(app.openArgs.length).toBe(3);
		expect(app.closeCount()).toBe(2);

		app.deliverOpens();  // 换配色重开的 "open" 事件（轻量路径）

		const opened = slideOpenedEvents(app.emitted);
		expect(opened.length, "纯换配色重开（同 name+revision）不得重发，否则插件 AI 会话被重置").toBe(1);
	});

	it("去重：同 name+revision 的连续两次 open（模拟换配色重开键口径）→ 只发一次", async () => {
		const app = bootApp({ infoByName: { [SLIDE_A]: plainInfo(REV1) } });
		await settle();
		app.findSlideRow().dispatch("click");
		await settle();
		app.deliverOpens();

		app.findSlideRow().dispatch("click");  // 同一切片再次 open（同 revision）
		await settle();
		app.deliverOpens();

		const opened = slideOpenedEvents(app.emitted);
		expect(opened.length, "同 name+revision 的两次 open 只发一次").toBe(1);
		expect(opened[0]).toEqual({ slide: Object.assign({}, EXPECTED_SLIDE, { revision: REV1 }) });
	});

	it("同名替换（重开路径）：revision 变化 + 本地持久化配色 → 补发且载荷 revision 为新值", async () => {
		const storage = fakeStorage();
		storage.setItem(persistedSelectionKey(SLIDE_A, REV2), persistedSelection());
		const app = bootApp({ infoByName: { [SLIDE_A]: multichannelInfo(REV2) }, storage });
		await settle();
		app.findSlideRow().dispatch("click");
		await settle();
		app.deliverOpens();

		const opened = slideOpenedEvents(app.emitted);
		expect(opened.length, "文件替换后（新 revision）的重开必须送达").toBe(1);
		expect(opened[0]).toEqual({ slide: Object.assign({}, EXPECTED_SLIDE, { revision: REV2 }) });
	});

	it("同名替换（正常路径）：同 name 但 revision 变化的再次 open → 重发且载荷 revision 为新值（去重不误伤）", async () => {
		const info = plainInfo(REV1);
		const app = bootApp({ infoByName: { [SLIDE_A]: info } });
		await settle();
		app.findSlideRow().dispatch("click");
		await settle();
		app.deliverOpens();

		info.asset_revision = REV2;  // 同名文件被替换（mtime_ns:size 变化）
		app.findSlideRow().dispatch("click");
		await settle();
		app.deliverOpens();

		const opened = slideOpenedEvents(app.emitted);
		expect(opened.length, "revision 变化必须重发（键不同），不得被去重吞掉").toBe(2);
		expect(opened[0].slide.revision).toBe(REV1);
		expect(opened[1].slide.revision).toBe(REV2);
	});

	it("切换切片再切回：A→B→A 每次切片身份变化都送达（去重只看上一条键）", async () => {
		const app = bootApp({
			infoByName: {
				[SLIDE_A]: plainInfo(REV1),
				[SLIDE_B]: plainInfo(REV1, SLIDE_B),
			},
			slides: [
				{ name: SLIDE_A, width: 1000, height: 800, mpp_x: 0.5, mpp_source: "native" },
				{ name: SLIDE_B, width: 2000, height: 1600, mpp_x: 0.25, mpp_source: "native" },
			],
		});
		await settle();
		app.findSlideRow(SLIDE_A).dispatch("click");
		await settle();
		app.deliverOpens();
		app.findSlideRow(SLIDE_B).dispatch("click");
		await settle();
		app.deliverOpens();
		app.findSlideRow(SLIDE_A).dispatch("click");  // 切回 A：插件已见过 B，必须重发 A
		await settle();
		app.deliverOpens();

		const opened = slideOpenedEvents(app.emitted);
		expect(opened.map((e) => e.slide.name)).toEqual([SLIDE_A, SLIDE_B, SLIDE_A]);
	});
});
