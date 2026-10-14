/**
 * Viewer 改版 2026-10-08（admin-viewer-simplified §5）：文件夹/切片浏览器。
 *
 * 加载真实 static/app.js（可路由 fetch harness + __PT_TEST_HOOKS → HP_PROJECT_UI），
 * 锁定：
 *   - 叠页大小纯函数（§5.2）：上限 8、按可用高度缩减、文件夹卡占同一预算、下限 1；
 *   - 根目录结构（§5.1）：顶层文件夹 + 「临时查看」虚拟文件夹（仅有临时切片时）+
 *     未归类（非临时）切片；parent_project_id 缺失视为根（后端合入前兼容）；
 *   - 文件夹导航 + 页码记忆（§5.3）：进入/返回恢复上次所在叠（内存）；
 *   - 悬停只预览（§5.2）：pointerenter 加 .extracted 但绝不触发 openSlide/info；
 *   - 打开竞态（§5.2）：A（慢）→ B（快）只保留 B；A 晚到响应被丢弃；
 *   - 搜索（§5.4）：跨文件夹匹配显示名/别名/原文件名；同名用位置 + slide_id
 *     末 6 位区分；点击结果定位到所在文件夹与所在叠；
 *   - 临时查看到期（§5.5）：过期时间/403 → 清屏（viewer.close）、卡片移除、
 *     提示「临时查看已结束」。
 */
import { afterEach, describe, expect, it, vi } from "vitest";
import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const appSrc = readFileSync(resolve(here, "../../static/app.js"), "utf8");

// ---------- 假元素（className/classList 同源 + children 树跟踪） ----------
interface BootEl extends Record<string, unknown> {
	id: string;
	hidden: boolean;
	disabled: boolean;
	title: string;
	textContent: string;
	innerHTML: string;
	value: string;
	children: BootEl[];
	parentNode?: BootEl;
	dataset: Record<string, string>;
	style: Record<string, string>;
	clientHeight?: number;
	classList: { add(...n: string[]): void; remove(...n: string[]): void; contains(n: string): boolean; toggle(n: string, f?: boolean): boolean };
	setAttribute(k: string, v: string): void;
	getAttribute(k: string): string | null;
	removeAttribute(k: string): void;
	addEventListener(type: string, cb: (e?: unknown) => void): void;
	dispatch(type: string, evt?: unknown): void;
	focus(): void;
	appendChild(c: unknown): void;
	removeChild(c: BootEl): void;
	querySelector(selector: string): BootEl | null;
	cloneNode(deep?: boolean): BootEl;
	getBoundingClientRect(): { left: number; top: number; right: number; bottom: number; width: number; height: number };
}

function bootEl(id = ""): BootEl {
	const classes = new Set<string>();
	const attrs = new Map<string, string>();
	const listeners: Record<string, Array<(e?: unknown) => void>> = {};
	const children: BootEl[] = [];
	const el: BootEl = {
		id,
		get className() {
			return Array.from(classes).join(" ");
		},
		set className(v: string) {
			classes.clear();
			String(v).split(/\s+/).filter(Boolean).forEach((n) => classes.add(n));
		},
		hidden: false,
		disabled: false,
		title: "",
		textContent: "",
		innerHTML: "",
		value: "",
		children,
		dataset: {},
		style: { setProperty(k: string, v: string) { this[k] = v; } } as unknown as Record<string, string>,
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
		removeAttribute: (k) => void attrs.delete(k),
		addEventListener: (type, cb) => void (listeners[type] ||= []).push(cb),
		dispatch: (type, evt) => (listeners[type] || []).forEach((cb) =>
			cb(Object.assign({ stopPropagation() {}, preventDefault() {}, pointerType: "mouse" }, evt))),
		focus: () => {},
		appendChild: (c) => {
			if (c && typeof c === "object" && "dispatch" in (c as object)) {
				const child = c as BootEl;
				child.parentNode = el;
				children.push(child);
			}
		},
		removeChild(c) { const i = children.indexOf(c); if (i >= 0) children.splice(i, 1); c.parentNode = undefined; },
		querySelector(selector) { return children.find(c => c.classList.contains(selector.replace(/^\./, ""))) || null; },
		cloneNode(deep) {
			const copy = bootEl(id); copy.className = el.className;
			if (deep) children.forEach(c => copy.appendChild(c.cloneNode(true)));
			return copy;
		},
		getBoundingClientRect: () => ({ left: 10, top: 20, right: 40, bottom: 48, width: 30, height: 28 }),
		getContext: () => new Proxy({}, { get: (t, k) => (k in t ? (t as Record<string, unknown>)[k] : () => undefined) }),
	};
	// innerHTML 赋值（渲染器清空容器）同步清空假 children 树
	let innerHTML = "";
	Object.defineProperty(el, "innerHTML", {
		get: () => innerHTML,
		set: (v: string) => {
			innerHTML = String(v);
			if (!innerHTML) children.length = 0;
		},
		configurable: true,
	});
	return el;
}

function clickEvt() {
	return { stopPropagation() {}, preventDefault() {}, target: null };
}

/** 假 localStorage（folder-experience §2）：可预置内容、可切换「抛错」降级。 */
interface FakeStorage {
	map: Map<string, string>;
	throws: boolean;
	getItem(k: string): string | null;
	setItem(k: string, v: string): void;
	removeItem(k: string): void;
}

function fakeStorage(seed: Record<string, string> = {}): FakeStorage {
	const map = new Map<string, string>(Object.entries(seed));
	const store: FakeStorage = {
		map,
		throws: false,
		getItem(k: string) {
			if (store.throws) throw new Error("storage blocked");
			return map.has(k) ? (map.get(k) as string) : null;
		},
		setItem(k: string, v: string) {
			if (store.throws) throw new Error("storage blocked");
			map.set(k, String(v));
		},
		removeItem(k: string) {
			map.delete(k);
		},
	};
	return store;
}

/** folder-experience §2 的版本化位置记忆记录（测试预置用）。 */
function posRecord(folder: string | null, pages: Record<string, number>, updatedAt = Date.now()) {
	return JSON.stringify({ v: 1, folder, pages, updatedAt });
}

interface RouteResult { status: number; body: unknown }

interface FbUI {
	state: { folder: string | null; page: number; totalPages: number; pages: Record<string, number> };
	FB_TEMP_KEY: string;
	pageSize(availH: number, folderCount: number): number;
	bandGap?(availH: number, folderCount: number, slideCount: number): number;
	entries(folderKey: string | null): { folders: Array<{ key: string; name: string; count: number; virtual?: boolean }>; slides: Array<Record<string, unknown>> };
	pathOf(key: string | null): string;
	search(q: string): Array<Record<string, unknown>>;
	locationText(s: Record<string, unknown>): string;
	render(): void;
	go(key: string | null, page?: number): void;
	locate(s: Record<string, unknown>): void;
	endTemporaryView(): void;
}

type Seedable = { routes: Map<string, () => RouteResult>; hang: Set<string>; laggy: Map<string, (out: RouteResult) => void> };

function bootApp(seed?: (s: Seedable) => void, opts: { search?: string; storage?: FakeStorage | null } = {}) {
	const els: Record<string, BootEl> = {};
	const docListeners: Record<string, Array<(e?: unknown) => void>> = {};
	const calls: Array<{ url: string; opts?: { method?: string; headers?: Record<string, string>; body?: string } }> = [];
	const routes = new Map<string, () => RouteResult>();
	const hang = new Set<string>();
	// 慢响应（竞态用）：url → 手动 resolve
	const laggy = new Map<string, (out: RouteResult) => void>();

	const fetchImpl = vi.fn((url: string, opts?: Record<string, unknown>) => {
		const u = String(url);
		calls.push({ url: u, opts: opts as never });
		if (hang.has(u)) return new Promise(() => {}) as Promise<Response>;
		if (laggy.has(u)) {
			return new Promise<Response>((resolvePromise) => {
				laggy.set(u, (out) => {
					resolvePromise({
						ok: out.status >= 200 && out.status < 300,
						status: out.status,
						clone() { return this; },
						json: () => Promise.resolve(out.body),
					} as Response);
				});
			}) as Promise<Response>;
		}
		const out = routes.has(u) ? routes.get(u)!() : { status: 200, body: [] as unknown };
		return Promise.resolve({
			ok: out.status >= 200 && out.status < 300,
			status: out.status,
			clone() { return this; },
			json: () => Promise.resolve(out.body),
		}) as Promise<Response>;
	}) as unknown as typeof fetch;

	const doc = {
		readyState: "loading",
		cookie: "csrf_token=tok",
		activeElement: null,
		getElementById(id: string) {
			if (!els[id]) els[id] = bootEl(id);
			return els[id];
		},
		createElement: (tag = "") => bootEl(tag),
		addEventListener(type: string, cb: (e?: unknown) => void) {
			(docListeners[type] ||= []).push(cb);
		},
		removeEventListener() {},
		querySelector: () => null,
		querySelectorAll: () => [] as BootEl[],
		body: bootEl("body"),
		documentElement: { lang: "zh-CN" },
	};
	const opens: Array<string | undefined> = [];
	const closes = { n: 0 };
	const fakeViewer = {
		container: {
			style: { setProperty(k: string, v: string) { this[k] = v; } } as unknown as Record<string, string>,
			getBoundingClientRect: () => ({ width: 800, height: 600, left: 0, top: 0 }),
			insertBefore() {},
		},
		canvas: {},
		viewport: null,
		addHandler() {},
		setMouseNavEnabled() {},
		open(src?: string) { opens.push(src); },
		close() { closes.n += 1; },
		forceResize() {},
	};
	const w: Record<string, unknown> = {
		__PT_TEST_HOOKS: true,
		HP_I18N: {
			t: (k: string, vars?: Record<string, unknown>) =>
				vars && Object.keys(vars).length ? `${k}(${JSON.stringify(vars)})` : k,
			getLang: () => "zh",
			setLang: () => {},
		},
		HP_ViewerCore: { create: () => fakeViewer },
		HP_API: {},
		HP_APP_BOOTSTRAP: { mode: "official", capabilities: { slide_id_api: true } },
		matchMedia: (q: string) => ({
			matches: q.includes("max-width: 768") ? false : true,
			addEventListener() {},
			addListener() {},
		}),
		fetch: fetchImpl,
		location: { href: "http://local/" + (opts.search || ""), pathname: "/", search: opts.search || "" },
		innerWidth: 1920,
		innerHeight: 900,
		requestAnimationFrame: (cb: () => void) => {
			cb();
			return 1;
		},
		addEventListener() {},
		localStorage: opts.storage || null,
	};
	(globalThis as { document?: unknown }).document = doc;
	(globalThis as { window?: unknown }).window = w;
	(globalThis as { fetch?: unknown }).fetch = fetchImpl;
	(globalThis as { HP_ViewerCore?: unknown }).HP_ViewerCore = w.HP_ViewerCore;
	(globalThis as { HP_I18N?: unknown }).HP_I18N = w.HP_I18N;
	// app.js 的 pt.cos.jobs 读写用裸 localStorage（浏览器里 = window.localStorage）；
	// Node 测试环境裸名落在 globalThis——镜像同一份 fake，恢复管线才可驱动
	(globalThis as { localStorage?: unknown }).localStorage = opts.storage || null;

	if (seed) seed({ routes, hang, laggy });
	new Function("window", "document", "fetch", "location", appSrc)(w, doc, fetchImpl, w.location);
	(docListeners["DOMContentLoaded"] || []).forEach((cb) => cb());

	const UI = (w as {
		HP_PROJECT_UI?: {
			fb: FbUI;
			viewerState: { slide: Record<string, unknown> | null };
			openSlide(ref: string): void;
			importDrawer: {
				state: { open: boolean; targetGen: number; targetDirty: boolean };
				targetState: { pid: string; newProjectName: string; createdPid: string; createKey: string | null };
				open(trigger?: BootEl): void;
				close(): void;
				associate(slideId: string | null, slideName: string, snap?: unknown, row?: unknown): Promise<unknown>;
				snapshot(): { pid: string; newProjectName: string; createdPid: string; createKey: string | null };
				defaultTargetPid(): string;
			};
		};
	}).HP_PROJECT_UI!;
	return {
		els, calls, routes, hang, laggy, doc, opens, closes: () => closes.n,
		storage: (opts.storage || null) as FakeStorage | null,
		docDispatch(type: string, evt?: unknown) {
			(docListeners[type] || []).forEach((cb) => cb(evt));
		},
		UI,
	};
}

function teardown() {
	vi.clearAllTimers();
	vi.useRealTimers();
	delete (globalThis as { window?: unknown }).window;
	delete (globalThis as { document?: unknown }).document;
	delete (globalThis as { fetch?: unknown }).fetch;
	delete (globalThis as { location?: unknown }).location;
	delete (globalThis as { HP_ViewerCore?: unknown }).HP_ViewerCore;
	delete (globalThis as { HP_I18N?: unknown }).HP_I18N;
	delete (globalThis as { localStorage?: unknown }).localStorage;
}

async function flush(times = 10) {
	for (let i = 0; i < times; i++) await Promise.resolve();
}

/** 深度拼接假元素树的 textContent（假元素不聚合父子文本）。 */
function deepText(el: BootEl): string {
	return (el.textContent || "") + el.children.map((c) => deepText(c)).join("");
}

function UI_fb(h: ReturnType<typeof bootApp>): FbUI {
	return h.UI.fb;
}

function fbHits(h: ReturnType<typeof bootApp>): BootEl[] {
	return h.els["fb-stack"].children.filter((c) => c.classList.contains("fb-hit"));
}
function fbFolders(h: ReturnType<typeof bootApp>): BootEl[] {
	return h.els["fb-stack"].children.filter((c) => c.classList.contains("fb-folder"));
}

// 根目录 36 张未归类 + 2 个顶层文件夹（教学/研究），可选 1 张临时切片
function seedStd(s: Seedable, opts: { tempAt?: number } = {}) {
	const slides: Array<Record<string, unknown>> = Array.from({ length: 36 }, (_, i) => ({
		name: "示例切片 " + String(i + 1).padStart(2, "0") + ".svs",
		slide_id: "sld_std" + String(i + 1).padStart(2, "0"),
		display_name: "",
		width: 1000, height: 800, mpp_x: 0.5, mpp_source: "native",
	}));
	if (opts.tempAt) slides[9].temporary_view_expires_at = opts.tempAt;
	s.routes.set("/api/slides", () => ({ status: 200, body: slides }));
	s.routes.set("/api/projects", () => ({
		status: 200,
		body: [
			{ pid: "p_t", name: "教学", slides: [], slide_count: 0 },
			{ pid: "p_r", name: "研究", slides: [], slide_count: 0 },
		],
	}));
	return slides;
}
function seedNone(s: Seedable) {
	const slides: Array<Record<string, unknown>> = Array.from({ length: 12 }, (_, i) => ({
		name: "切片" + i + ".svs",
		slide_id: "sld_l" + String(i).padStart(2, "0"),
		display_name: "",
		width: 10, height: 10, mpp_x: 0.5,
	}));
	s.routes.set("/api/slides", () => ({ status: 200, body: slides }));
	s.routes.set("/api/projects", () => ({ status: 200, body: [] }));
	return slides;
}

afterEach(teardown);

describe("叠页大小与露出条带（§5.2 纯函数）", () => {
	it("页容量：上限 8；按最紧露出条带（44px）计保证放得下；文件夹卡占用同一预算；下限 1", () => {
		bootApp();
		const UI = (globalThis as { window?: { HP_PROJECT_UI?: { fb: FbUI } } }).window?.HP_PROJECT_UI!;
		const ps = UI.fb.pageSize;
		expect(ps(10000, 0)).toBe(8);
		// 无文件夹：1 张完整卡 + (n-1)*44 ≤ avail
		expect(ps(44 * 7 + 128, 0)).toBe(8);
		expect(ps(44 * 7 + 128 - 1, 0)).toBe(7);
		expect(ps(128, 0)).toBe(1);
		// 文件夹卡占用同一预算（每张 64+8）
		expect(ps(10000, 2)).toBe(8);
		// 极小可用高度：钳到 1（不出现 0/负数叠）
		expect(ps(0, 5)).toBe(1);
	});

	it("多文件夹时应利用可用空间，而非每页只显示一张卡", async () => {
		const h = bootApp(s => {
			seedNone(s);
			s.routes.set("/api/projects", () => ({ status: 200, body:
				Array.from({ length: 8 }, (_, i) => ({ pid: "p" + i, name: "Folder " + i, slides: [] })) }));
		});
		await flush();
		h.els["fb-stack"].clientHeight = 600;
		h.UI.fb.render();
		// 600px 足以放入多张 64px 文件夹卡；不复述 pageSize 内部公式。
		expect(fbFolders(h).length + fbHits(h).length).toBeGreaterThan(1);
	});

	it("露出条带：短叠铺满可用高度、上限=整卡高、下限=44px；单张不铺开", () => {
		bootApp();
		const UI = (globalThis as { window?: { HP_PROJECT_UI?: { fb: FbUI } } }).window?.HP_PROJECT_UI!;
		const gap = UI.fb.bandGap!;
		expect(gap(600, 0, 1)).toBe(44);          // 单张：无铺开语义，用偏好值
		expect(gap(600, 0, 8)).toBe(67);          // 8 张：472/7=67（贴合 600）
		expect(gap(1000, 0, 2)).toBe(128);        // 2 张大空间：钳到整卡高
		expect(gap(300, 0, 8)).toBe(44);          // 紧：钳到下限
		expect(gap(100, 0, 8)).toBe(44);          // 极小：不出负数/0
		expect(gap(600, 2, 8)).toBe(46);          // 2 张文件夹卡占 144：(600-144-128)/7=46
	});
});

describe("根目录结构与文件夹导航（§5.1/§5.3）", () => {
	it("根目录=顶层文件夹+未归类；临时文件夹仅在有临时切片时出现；缺失 parent_project_id 视为根", async () => {
		const h = bootApp((s) => { seedStd(s, { tempAt: Date.parse("2036-01-01T00:00:00Z") / 1000 }); });
		await flush();
		// 600px 可用高度 → 每叠 8（3 文件夹卡 + 5 切片卡）
		h.els["fb-stack"].clientHeight = 600;
		h.UI.fb.render();
		// 2 个顶层文件夹 + 1 个虚拟临时文件夹 + 35 张未归类（临时那张排除）
		expect(fbFolders(h).map((c) => c.dataset.pid)).toEqual(["p_t", "p_r", "__temp__"]);
		expect(deepText(fbFolders(h)[2])).toContain("fb.temp.folder");
		// 首页 8 项 = 3 张文件夹卡 + 5 张切片卡（39 项 ÷ 8 = 5 页）
		expect(fbHits(h).length).toBe(5);
		expect(UI_fb(h).state.folder).toBeNull();
	});

	it("翻页 + 页码记忆：进入/返回恢复上次所在叠", async () => {
		const projects: Array<Record<string, unknown>> = [
			{ pid: "p_t", name: "教学", slides: [], slide_count: 0 },
			{ pid: "p_r", name: "研究", slides: [], slide_count: 0 },
		];
		const h = bootApp((s) => {
			seedStd(s);
			s.routes.set("/api/projects", () => ({ status: 200, body: projects }));
		});
		await flush();
		h.els["fb-stack"].clientHeight = 600;
		h.UI.fb.render();
		// 39 项 ÷ 8 = 5 页；先翻到第 3 页
		h.els["fb-next-btn"].dispatch("click", clickEvt());
		h.els["fb-next-btn"].dispatch("click", clickEvt());
		expect(UI_fb(h).state.page).toBe(2);
		expect(h.els["fb-page-info"].textContent).toContain('"p":3');
		// 进入文件夹（fbGo 记忆根目录页码）→ 返回 → 页码恢复
		UI_fb(h).go("p_t");
		expect(UI_fb(h).state.folder).toBe("p_t");
		expect(UI_fb(h).state.page).toBe(0);
		h.els["fb-up-btn"].dispatch("click", clickEvt());
		expect(UI_fb(h).state.folder).toBeNull();
		expect(UI_fb(h).state.page).toBe(2);
		// 回到首页后文件夹卡可点击进入
		h.els["fb-prev-btn"].dispatch("click", clickEvt());
		h.els["fb-prev-btn"].dispatch("click", clickEvt());
		fbFolders(h)[0].dispatch("click", clickEvt());
		expect(UI_fb(h).state.folder).toBe("p_t");
		// 子文件夹（parent_project_id 指向 p_t）只出现在 p_t 内
		projects.push({ pid: "p_sub", name: "复核", parent_project_id: "p_t", slides: [], slide_count: 0 });
		UI_fb(h).reload();
		await flush(20);
		const sub = fbFolders(h).find((c) => c.dataset.pid === "p_sub");
		expect(sub).toBeTruthy();
	});

	it("当前文件夹被删除（重载后不在列表）→ 回到根目录", async () => {
		const projects: Array<Record<string, unknown>> = [
			{ pid: "p_t", name: "教学", slides: [], slide_count: 0 },
			{ pid: "p_r", name: "研究", slides: [], slide_count: 0 },
		];
		const h = bootApp((s) => {
			seedStd(s);
			s.routes.set("/api/projects", () => ({ status: 200, body: projects }));
		});
		await flush();
		fbFolders(h)[0].dispatch("click", clickEvt());
		expect(UI_fb(h).state.folder).toBe("p_t");
		// 服务端删除 p_t 后重载（真实重取 /api/projects）
		projects.splice(0, 1);
		UI_fb(h).reload();
		await flush(20);
		expect(UI_fb(h).state.folder).toBeNull();
	});
});

describe("悬停只预览（§5.2）", () => {
	it("悬停预览延迟出现、离开收回，全程不调用 openSlide/info", async () => {
		vi.useFakeTimers();
		const h = bootApp((s) => { seedStd(s); });
		await flush();
		const infoCalls = () => h.calls.filter((c) => /\/api\/slides?\/[^/]+\/info$/.test(c.url)).length;
		expect(infoCalls()).toBe(0);
		const card = fbHits(h)[0];
		card.dispatch("pointerenter", { pointerType: "mouse" });
		await vi.advanceTimersByTimeAsync(80);
		expect(card.classList.contains("extracted")).toBe(true);
		expect(infoCalls()).toBe(0);
		expect(h.opens.length).toBe(0);
		// 触屏 pointerenter 也不滑出（触屏点击直接打开）
		const card2 = fbHits(h)[1];
		card2.dispatch("pointerenter", { pointerType: "touch" });
		expect(card2.classList.contains("extracted")).toBe(false);
		card.dispatch("pointerleave");
		await vi.advanceTimersByTimeAsync(90);
		expect(card.classList.contains("extracted")).toBe(false);
		expect(infoCalls()).toBe(0);
	});
});

describe("打开竞态（§5.2：A→B 快速点击只保留 B）", () => {
	it("A 的 info 慢、B 的快 → viewer 打开 B；A 晚到响应被丢弃（不覆盖 state/不换底图）", async () => {
		let slides: Array<Record<string, unknown>> = [];
		const h = bootApp((s) => { slides = seedStd(s); });
		await flush();
		const a = slides[0];
		const b = slides[1];
		// A：info 挂起（手动放行）
		h.laggy.set("/api/slides/" + a.slide_id + "/info", () => {});
		fbHits(h)[0].dispatch("click", clickEvt());
		await flush(2);
		// B：正常路由
		h.routes.set("/api/slides/" + b.slide_id + "/info", () => ({
			status: 200,
			body: { name: b.name, slide_id: b.slide_id, display_name: "", width: 10, height: 10, mpp_x: 0.5 },
		}));
		fbHits(h)[1].dispatch("click", clickEvt());
		await flush(20);
		expect(h.UI.viewerState.slide && h.UI.viewerState.slide.id).toBe(b.slide_id);
		const bOpens = h.opens.filter((u) => String(u).includes(b.slide_id) || String(u).includes(encodeURIComponent(b.name)));
		expect(bOpens.length).toBeGreaterThan(0);
		const opensBefore = h.opens.length;
		// A 晚到：返回 200 info → 必须整体丢弃
		const releaseA = h.laggy.get("/api/slides/" + a.slide_id + "/info")!;
		releaseA({ status: 200, body: { name: a.name, slide_id: a.slide_id, display_name: "", width: 10, height: 10, mpp_x: 0.5 } });
		await flush(20);
		expect(h.opens.length).toBe(opensBefore);
		expect(h.UI.viewerState.slide && h.UI.viewerState.slide.id).toBe(b.slide_id);
	});
});

describe("搜索（§5.4）", () => {
	function seedSearch(s: Seedable) {
		const slides = [
			{ name: "tumor-a.svs", slide_id: "sld_sa1", display_name: "肿瘤 A", original_filename: "tumor-a.svs", width: 10, height: 10, mpp_x: 0.5 },
			{ name: "tumor-b.svs", slide_id: "sld_sb2", display_name: "肿瘤 A", original_filename: "tumor-b.svs", width: 10, height: 10, mpp_x: 0.5 },
			{ name: "normal.svs", slide_id: "sld_n3", display_name: "", original_filename: "normal.svs", width: 10, height: 10, mpp_x: 0.5 },
		];
		s.routes.set("/api/slides", () => ({ status: 200, body: slides }));
		s.routes.set("/api/projects", () => ({
			status: 200,
			body: [
				{ pid: "p1", name: "教学", slide_ids: ["sld_sa1"], slides: ["tumor-a.svs"], slide_count: 1 },
				{ pid: "p2", name: "研究", parent_project_id: "p1", slide_ids: ["sld_sb2"], slides: ["tumor-b.svs"], slide_count: 1 },
			],
		}));
		return slides;
	}

	it("跨文件夹匹配显示名/别名/原文件名（大小写不敏感）；同名结果可区分（位置 + ID 末 6 位）", async () => {
		const h = bootApp((s) => { seedSearch(s); });
		await flush();
		const fb = h.UI.fb;
		// 按显示名命中 2 张（不同文件夹）；按原文件名命中各自一张
		expect(fb.search("肿瘤 A").length).toBe(2);
		expect(fb.search("TUMOR-B").map((s) => s.slide_id)).toEqual(["sld_sb2"]);
		expect(fb.search("no-such-slide")).toEqual([]);
		// 位置：p1 是顶层文件夹（「教学」），p2 是其子文件夹（「教学 / 研究」）；
		// 未归类 → fb.unfiled 键
		const locs = fb.search("肿瘤 A").map((sl) => fb.locationText(sl));
		expect(locs).toEqual(["教学", "教学 / 研究"]);
		expect(fb.locationText(fb.search("normal")[0])).toContain("fb.unfiled");
	});

	it("混合文件夹与切片时，搜索定位后目标卡可见并能打开", async () => {
		let slides: Array<Record<string, unknown>> = [];
		const h = bootApp((s) => {
			slides = seedNone(s);
			s.routes.set("/api/projects", () => ({ status: 200, body: [
				{ pid: "p_a", name: "Folder A", slides: [] },
				{ pid: "p_b", name: "Folder B", slides: [] },
			] }));
		});
		await flush();
		const target = slides[5];
		h.UI.fb.locate(target);
		expect(h.UI.fb.state.folder).toBeNull();
		const card = fbHits(h).find((c) => c.dataset.slideId === target.slide_id);
		expect(card).toBeTruthy();
		card!.dispatch("click", clickEvt());
		await flush(20);
		expect(h.calls.some((c) => c.url === "/api/slides/" + target.slide_id + "/info")).toBe(true);
	});
});

	/** 到期后让 /api/slides 不再下发某张切片（模拟服务端门禁的列表口径）。 */
	function hideSlideFromList(h: { routes: Map<string, () => RouteResult> }, sid: string) {
		const prev = h.routes.get("/api/slides")!;
		h.routes.set("/api/slides", () => {
			const out = prev();
			const body = (out.body as Array<Record<string, unknown>>).filter((s) => s.slide_id !== sid);
			return { status: 200, body };
		});
	}

describe("临时查看到期（§5.5）", () => {
	function seedTemp(s: Seedable, expiresAt: number) {
		const slides = [
			{ name: "temp.svs", slide_id: "sld_temp", display_name: "", temporary_view_expires_at: expiresAt, width: 10, height: 10, mpp_x: 0.5 },
			{ name: "mine.svs", slide_id: "sld_mine", display_name: "", width: 10, height: 10, mpp_x: 0.5 },
		];
		s.routes.set("/api/slides", () => ({ status: 200, body: slides }));
		s.routes.set("/api/projects", () => ({ status: 200, body: [] }));
		s.routes.set("/api/slides/sld_temp/info", () => ({
			status: 200,
			body: { name: "temp.svs", slide_id: "sld_temp", display_name: "", width: 10, height: 10, mpp_x: 0.5 },
		}));
		return slides;
	}

	it("列表 epoch 秒到期、info 无标记：有效期内可看，到期后无人操作也清屏", async () => {
		vi.useFakeTimers();
		vi.setSystemTime(new Date("2026-10-08T00:00:00Z"));
		const h = bootApp((s) => { seedTemp(s, Date.now() / 1000 + 5); });
		await flush();
		// 临时切片收在「临时查看」虚拟文件夹里 → 先进入
		h.UI.fb.go(h.UI.fb.FB_TEMP_KEY);
		const card = fbHits(h).find((c) => c.dataset.slideId === "sld_temp");
		expect(card).toBeTruthy();
		card!.dispatch("click", clickEvt());
		await flush(20);
		expect(h.UI.viewerState.slide?.id).toBe("sld_temp");
		await vi.advanceTimersByTimeAsync(4999);
		expect(h.UI.viewerState.slide?.id).toBe("sld_temp");
		await vi.advanceTimersByTimeAsync(2);
		expect(h.UI.viewerState.slide).toBeNull();
		expect(h.closes()).toBeGreaterThan(0);
		// 卡片与虚拟文件夹消失（临时切片排除出根目录）
		expect(fbHits(h).some((c) => c.dataset.slideId === "sld_temp")).toBe(false);
		expect(fbFolders(h).some((c) => c.dataset.pid === "__temp__")).toBe(false);
		// 普通切片不受影响
		h.UI.fb.go(null);
		expect(fbHits(h).some((c) => c.dataset.slideId === "sld_mine")).toBe(true);
		const toastTexts = h.els["toast-container"].children.map((c) => c.textContent).join("|");
		expect(toastTexts).toContain("tempview.ended");
		// 画布标签清空
		expect(h.els["canvas-slide-label"].hidden).toBe(true);
		// 回到「未打开切片」基线：空态卡回归、缩放徽章复位（顶栏无残留切片上下文）
		expect(h.els["viewer-empty"].hidden).toBe(false);
		expect(h.els["zoom-badge"].textContent).toBe("—");
		expect(h.els["header-zoom-badge"].textContent).toBe("—");
	});

	it("DEF-3：info 不带标记时回读列表标记 → 到期 403 仍清屏 + 临时提示 + 列表刷新", async () => {
		const h = bootApp((s) => {
			seedTemp(s, Date.parse("2036-01-01T00:00:00Z") / 1000);
			// 复刻真实后端：/info 不携带 temporary_view_expires_at（标记只在列表上）
			s.routes.set("/api/slides/sld_temp/info", () => ({
				status: 200,
				body: { name: "temp.svs", slide_id: "sld_temp", display_name: "", width: 10, height: 10, mpp_x: 0.5 },
			}));
		});
		await flush();
		h.UI.fb.go(h.UI.fb.FB_TEMP_KEY);
		const card = fbHits(h).find((c) => c.dataset.slideId === "sld_temp");
		card!.dispatch("click", clickEvt());
		await flush(20);
		expect(h.UI.viewerState.slide && h.UI.viewerState.slide.id).toBe("sld_temp");
		// 服务端把授权改到过去 + /api/slides 不再下发该切片（门禁权威）
		h.routes.set("/api/slides/sld_temp/info", () => ({ status: 403, body: { error: "forbidden" } }));
		hideSlideFromList(h, "sld_temp");
		const callsBefore = h.calls.length;
		// 列表标记仍在（最近一次 /api/slides 下发过 temporary_view_expires_at）——
		// info 不带标记时回读列表标记 → 按到期清屏（DEF-3 修复路径）
		h.UI.fb.render();
		fbHits(h).find((c) => c.dataset.slideId === "sld_temp")!.dispatch("click", clickEvt());
		await flush(30);
		expect(h.UI.viewerState.slide).toBeNull();
		expect(h.closes()).toBeGreaterThan(0);
		expect(h.els["toast-container"].children.map((c) => c.textContent).join("|")).toContain("tempview.ended");
		// 列表权威刷新（/api/slides、/api/projects 重拉）
		const refetched = h.calls.slice(callsBefore).filter((c) => /\/api\/slides$|\/api\/projects$/.test(c.url));
		expect(refetched.length).toBeGreaterThan(0);
		// 临时文件夹已被列表移除
		expect(fbFolders(h).some((c) => c.dataset.pid === "__temp__")).toBe(false);
		// 403 清屏路径同一基线：空态卡回归、缩放徽章复位
		expect(h.els["viewer-empty"].hidden).toBe(false);
		expect(h.els["zoom-badge"].textContent).toBe("—");
		expect(h.els["header-zoom-badge"].textContent).toBe("—");
	});

	it("403/404（服务端拒绝）→ 同口径清屏；普通切片 404 维持报错不清屏", async () => {
		const h = bootApp((s) => { seedTemp(s, Date.parse("2036-01-01T00:00:00Z") / 1000); }); // 未到期
		await flush();
		h.UI.fb.go(h.UI.fb.FB_TEMP_KEY);
		const card = fbHits(h).find((c) => c.dataset.slideId === "sld_temp");
		card!.dispatch("click", clickEvt());
		await flush(20);
		expect(h.UI.viewerState.slide && h.UI.viewerState.slide.id).toBe("sld_temp");
		// 服务端把授权改到过去 → info 403
		h.routes.set("/api/slides/sld_temp/info", () => ({ status: 403, body: { error: "forbidden" } }));
		fbHits(h).find((c) => c.dataset.slideId === "sld_temp")!.dispatch("click", clickEvt());
		await flush(20);
		expect(h.UI.viewerState.slide).toBeNull();
		expect(h.closes()).toBeGreaterThan(0);
		expect(h.els["toast-container"].children.map((c) => c.textContent).join("|")).toContain("tempview.ended");
		// 普通切片 404：报 open.fail，不清屏
		const openSlideCount = h.closes();
		h.UI.fb.go(null);
		h.routes.set("/api/slides/sld_mine/info", () => ({ status: 404, body: { error: "missing" } }));
		fbHits(h).find((c) => c.dataset.slideId === "sld_mine")!.dispatch("click", clickEvt());
		await flush(20);
		expect(h.closes()).toBe(openSlideCount);
		expect(h.UI.viewerState.slide).toBeNull(); // 本就没有打开切片
	});
});

// Short/landscape sidebar: every page must respect the same visible height.
it("换页后首张切片按完整卡高计入，第二页不超出 200px", async () => {
    const h = bootApp(seedNone);
    await flush();
    h.els["fb-stack"].clientHeight = 200;
    h.UI.fb.go(null, 1);
    const cards = fbHits(h);
    expect(cards.length).toBeGreaterThan(0);
    const bottom = Math.max(...cards.map(card =>
        parseFloat(card.style.top) + parseFloat(card.style.height)));
    expect(bottom).toBeLessThanOrEqual(200);
});

// 深链 /app?slide=<id>：侧栏翻到该切片所在的叠（与搜索定位同一函数）
it("深链打开的切片在侧栏所在叠可见", async () => {
	const h = bootApp(seedNone, { search: "?slide=sld_l11" });
	h.els["fb-stack"].clientHeight = 200;
	await flush(20);
	expect(fbHits(h).some((c) => c.dataset.slideId === "sld_l11")).toBe(true);
});

// ===========================================================================
// folder-experience 2026-10-13（docs/followup/folder-experience-implementation-20261013.md）
// §2 位置记忆（刷新恢复/裁剪/隔离/降级）+ §1 导入默认目标与冻结。
// ===========================================================================

const AUTH_U1 = { auth_enabled: true, user_id: "u1", role: "user", username: "u1@example.com" };

function seedAuth(s: Seedable, info: Record<string, unknown>) {
	s.routes.set("/api/auth/info", () => ({ status: 200, body: info }));
}

/** 12 张未归类 + 「肺组织」(p_t, 4 张) + 「研究」(p_r)；can_write 由服务端逐行下发。 */
function seedFx(s: Seedable, projectsOverride?: Array<Record<string, unknown>>) {
	const slides = Array.from({ length: 12 }, (_, i) => ({
		name: "fx" + String(i + 1).padStart(2, "0") + ".svs",
		slide_id: "sld_fx" + String(i + 1).padStart(2, "0"),
		display_name: "", width: 1000, height: 800, mpp_x: 0.5, mpp_source: "native",
	}));
	const projects: Array<Record<string, unknown>> = projectsOverride || [
		{ pid: "p_t", name: "肺组织", can_write: true,
			slide_ids: slides.slice(0, 4).map((x) => x.slide_id),
			slides: slides.slice(0, 4).map((x) => x.name) },
		{ pid: "p_r", name: "研究", can_write: true, slide_ids: [], slides: [] },
	];
	s.routes.set("/api/slides", () => ({ status: 200, body: slides }));
	s.routes.set("/api/projects", () => ({ status: 200, body: projects }));
	return { slides, projects };
}

describe("位置记忆：刷新恢复与隔离（folder-experience §2）", () => {
	it("案例：进入子目录第 2 页后刷新 → 恢复同一目录和合法页码（先存后恢复全链路）", async () => {
		// 第一次会话：进入 p_t 翻到第 2 页（导航/翻页结束即写记忆）
		const storage1 = fakeStorage();
		const h1 = bootApp((s) => { seedFx(s); seedAuth(s, AUTH_U1); }, { storage: storage1 });
		await flush(20);
		h1.els["fb-stack"].clientHeight = 200;
		h1.UI.fb.go("p_t");
		h1.els["fb-next-btn"].dispatch("click", clickEvt());
		expect(h1.UI.fb.state.page).toBe(1);
		const raw = storage1.map.get("pt.fb.pos.v1:u1");
		expect(raw).toBeTruthy();
		const saved = JSON.parse(raw!);
		expect(saved.v).toBe(1);
		expect(saved.folder).toBe("p_t");
		// 只存 ID/页码/时间：不含任何切片名或文件夹名
		expect(JSON.stringify(saved)).not.toContain("肺组织");
		expect(JSON.stringify(saved)).not.toContain("fx01");
		// 刷新（第二次 boot，同一 storage）：恢复同一目录和页码
		const storage2 = fakeStorage(Object.fromEntries(storage1.map));
		const h2 = bootApp((s) => { seedFx(s); seedAuth(s, AUTH_U1); }, { storage: storage2 });
		h2.els["fb-stack"].clientHeight = 200;   // 恢复渲染前先固定可用高度
		await flush(20);
		expect(h2.UI.fb.state.folder).toBe("p_t");
		expect(h2.UI.fb.state.page).toBe(1);
		expect(h2.els["fb-page-info"].textContent).toContain('"p":2');
		// 卡片确实渲染（非空列表）
		expect(fbHits(h2).length).toBeGreaterThan(0);
	});

	it("案例：记忆页码超出剩余页数（第 3 页只剩两页）→ 钳到最后一页，不出现空白列表", async () => {
		const h = bootApp((s) => { seedFx(s); seedAuth(s, AUTH_U1); }, {
			storage: fakeStorage({ "pt.fb.pos.v1:u1": posRecord("p_t", { "p_t": 2 }) }),
		});
		h.els["fb-stack"].clientHeight = 200;
		await flush(20);
		// p_t 只有 4 张 → 2 页；记忆的第 3 页钳到第 2 页
		expect(h.UI.fb.state.folder).toBe("p_t");
		expect(h.UI.fb.state.page).toBe(1);
		expect(h.els["fb-page-info"].textContent).toContain('"m":2');
		expect(fbHits(h).length).toBeGreaterThan(0);
	});

	it("案例：记忆目录被删除/撤权 → 回根目录，且不发起对该目录的任何内容请求", async () => {
		const h = bootApp((s) => {
			seedFx(s);
			seedAuth(s, AUTH_U1);
		}, {
			storage: fakeStorage({ "pt.fb.pos.v1:u1": posRecord("p_gone", { "p_gone": 1 }) }),
		});
		await flush(20);
		expect(h.UI.fb.state.folder).toBeNull();
		expect(h.UI.fb.state.page).toBe(0);
		// 恢复只与本地授权列表比对：没有任何针对 p_gone 的请求
		expect(h.calls.some((c) => c.url.includes("p_gone"))).toBe(false);
	});

	it("案例：深链 ?slide= 优先于磁盘记忆 → 定位到链接切片及其真实位置", async () => {
		const h = bootApp((s) => {
			seedFx(s);
			seedAuth(s, AUTH_U1);
			// p_t 含 sld_fx01；深链打开它 → 侧栏进 p_t，而不是记忆目录
		}, {
			search: "?slide=sld_fx01",
			storage: fakeStorage({ "pt.fb.pos.v1:u1": posRecord("p_r", { "p_r": 0 }) }),
		});
		h.els["fb-stack"].clientHeight = 200;
		await flush(20);
		expect(h.UI.fb.state.folder).toBe("p_t");
		expect(fbHits(h).some((c) => c.dataset.slideId === "sld_fx01")).toBe(true);
	});

	it("案例：本次会话已导航 → 迟到的身份/记忆恢复不回拉旧目录", async () => {
		let releaseAuth: (out: RouteResult) => void = () => {};
		const h = bootApp((s) => {
			seedFx(s);
			s.routes.set("/api/auth/info", () => ({ status: 200, body: AUTH_U1 }));
			s.laggy.set("/api/auth/info", (out) => releaseAuth(out));
		}, {
			storage: fakeStorage({ "pt.fb.pos.v1:u1": posRecord("p_r", {}) }),
		});
		await flush(20);
		// 身份未到时用户已导航进 p_t（projects 列表正常加载，p_t 可见）
		h.UI.fb.go("p_t");
		expect(h.UI.fb.state.folder).toBe("p_t");
		releaseAuth({ status: 200, body: AUTH_U1 });
		await flush(20);
		// 不被记忆目录 p_r 拉回
		expect(h.UI.fb.state.folder).toBe("p_t");
	});

	it("案例：无可信身份（未登录/免认证）不持久化", async () => {
		const storage = fakeStorage();
		const h = bootApp((s) => {
			seedFx(s);
			seedAuth(s, { auth_enabled: false });
		}, { storage });
		await flush(20);
		h.UI.fb.go("p_t");
		h.els["fb-next-btn"].dispatch("click", clickEvt());
		await flush(4);
		expect([...storage.map.keys()].filter((k) => k.startsWith("pt.fb.")).length).toBe(0);
	});

	it("案例：换账号绝不读取上一账号的记忆；管理员预览按有效身份（被预览者）恢复", async () => {
		const shared = fakeStorage({
			"pt.fb.pos.v1:uA": posRecord("p_t", { "p_t": 1 }),
		});
		// 用户 B 登录（同一浏览器存储）：看不到 A 的目录
		const hB = bootApp((s) => { seedFx(s); seedAuth(s, { ...AUTH_U1, user_id: "uB" }); }, { storage: shared });
		await flush(20);
		expect(hB.UI.fb.state.folder).toBeNull();
		// 用户 A 登录：恢复
		const hA = bootApp((s) => { seedFx(s); seedAuth(s, { ...AUTH_U1, user_id: "uA" }); }, { storage: shared });
		await flush(20);
		expect(hA.UI.fb.state.folder).toBe("p_t");
		// 管理员预览 A（actor=uAdmin，有效身份=uA）：按有效身份恢复 A 的记忆
		const hP = bootApp((s) => {
			seedFx(s);
			seedAuth(s, { ...AUTH_U1, user_id: "uA", actor: { user_id: "uAdmin", role: "owner" }, preview: { subject_user_id: "uA" } });
		}, { storage: shared });
		await flush(20);
		expect(hP.UI.fb.state.folder).toBe("p_t");
	});

	it("案例：「临时查看」集合与 30 天前/坏形状记录永不恢复", async () => {
		const stale = posRecord("p_t", { "p_t": 0 }, Date.now() - 31 * 24 * 3600 * 1000);
		const h = bootApp((s) => { seedFx(s); seedAuth(s, AUTH_U1); }, {
			storage: fakeStorage({
				"pt.fb.pos.v1:u1": posRecord("__temp__", {}),
				"pt.fb.pos.v1:old": stale,
				"pt.fb.pos.v1:bad": "{\"v\":2,\"folder\":\"p_t\"}",
				"pt.fb.pos.v1:junk": "not-json",
			}),
		});
		await flush(20);
		// u1 的记录指向临时查看 → 回根
		expect(h.UI.fb.state.folder).toBeNull();
		// 换个身份读 30 天前的记录 → 过期整体丢弃
		const h2 = bootApp((s) => { seedFx(s); seedAuth(s, { ...AUTH_U1, user_id: "old" }); }, {
			storage: fakeStorage({ "pt.fb.pos.v1:old": stale }),
		});
		await flush(20);
		expect(h2.UI.fb.state.folder).toBeNull();
	});

	it("案例：localStorage 抛错（隐私模式）→ 导航、翻页、恢复尝试全部照常", async () => {
		const storage = fakeStorage();
		storage.throws = true;
		const h = bootApp((s) => { seedFx(s); seedAuth(s, AUTH_U1); }, { storage });
		h.els["fb-stack"].clientHeight = 200;
		await flush(20);
		expect(() => h.UI.fb.go("p_t")).not.toThrow();
		expect(h.UI.fb.state.folder).toBe("p_t");
		expect(() => h.els["fb-next-btn"].dispatch("click", clickEvt())).not.toThrow();
		expect(h.UI.fb.state.page).toBe(1);
	});
});

describe("导入默认目标与冻结（folder-experience §1）", () => {
	function openDrawer(h: ReturnType<typeof bootApp>) {
		h.UI.importDrawer.open();
		return flush(20);
	}

	it("案例：在文件夹内打开抽屉 → 默认当前文件夹（本地与百度下拉一致）", async () => {
		const h = bootApp((s) => { seedFx(s); seedAuth(s, AUTH_U1); });
		await flush(20);
		h.UI.fb.go("p_t");
		await openDrawer(h);
		expect(h.UI.importDrawer.defaultTargetPid()).toBe("p_t");
		expect(h.UI.importDrawer.targetState.pid).toBe("p_t");
		expect(h.els["import-target-select"].value).toBe("p_t");
		expect(h.els["baidu-target-select"].value).toBe("p_t");
		h.UI.importDrawer.close();
	});

	it("案例：根目录与「临时查看」集合 → 默认未归类", async () => {
		const h = bootApp((s) => {
			seedFx(s);
			seedAuth(s, AUTH_U1);
			// 一张临时切片 → 临时查看虚拟文件夹可用
			s.routes.set("/api/slides", () => ({ status: 200, body: [
				{ name: "temp.svs", slide_id: "sld_temp", display_name: "",
					temporary_view_expires_at: Date.parse("2036-01-01T00:00:00Z") / 1000,
					width: 10, height: 10, mpp_x: 0.5 },
			] }));
		});
		await flush(20);
		await openDrawer(h);
		expect(h.UI.importDrawer.targetState.pid).toBe("");
		h.UI.importDrawer.close();
		h.UI.fb.go(h.UI.fb.FB_TEMP_KEY);
		await openDrawer(h);
		expect(h.UI.importDrawer.targetState.pid).toBe("");
		h.UI.importDrawer.close();
	});

	it("案例：默认只认最新授权列表 + 服务端显式 can_write（缺字段/false/不在列表 → 未归类）", async () => {
		// 当前目录不在最新列表（已撤权）→ 未归类
		const h1 = bootApp((s) => {
			seedFx(s);
			seedAuth(s, AUTH_U1);
		});
		await flush(20);
		h1.UI.fb.go("p_t");
		h1.routes.set("/api/projects", () => ({ status: 200, body: [
			{ pid: "p_r", name: "研究", can_write: true, slide_ids: [], slides: [] },
		] }));
		await openDrawer(h1);
		expect(h1.UI.importDrawer.targetState.pid).toBe("");
		h1.UI.importDrawer.close();

		// 列表里有目录但 can_write 字段缺失（旧后端）→ 不默认写入
		const h2 = bootApp((s) => { seedFx(s); seedAuth(s, AUTH_U1); });
		await flush(20);
		h2.UI.fb.go("p_t");
		h2.routes.set("/api/projects", () => ({ status: 200, body: [
			{ pid: "p_t", name: "肺组织", slide_ids: [], slides: [] },
		] }));
		await openDrawer(h2);
		expect(h2.UI.importDrawer.targetState.pid).toBe("");
		h2.UI.importDrawer.close();

		// can_write 显式 false → 不默认写入
		const h3 = bootApp((s) => { seedFx(s, [
			{ pid: "p_t", name: "肺组织", can_write: false, slide_ids: [], slides: [] },
		]); seedAuth(s, AUTH_U1); });
		await flush(20);
		h3.UI.fb.go("p_t");
		await openDrawer(h3);
		expect(h3.UI.importDrawer.targetState.pid).toBe("");
		h3.UI.importDrawer.close();
	});

	it("案例：用户先改目标、projects 响应后才返回 → 不覆盖用户选择", async () => {
		let releaseProjects: (out: RouteResult) => void = () => {};
		const h = bootApp((s) => {
			seedFx(s);
			seedAuth(s, AUTH_U1);
			s.laggy.set("/api/projects", (out) => releaseProjects(out));
		});
		await flush(20);
		h.UI.fb.go("p_t");
		h.UI.importDrawer.open();
		await flush(4);
		// projects 尚未返回，用户已把目标改成 p_r（本地下拉）
		h.els["import-target-select"].value = "p_r";
		h.els["import-target-select"].dispatch("change", {});
		expect(h.UI.importDrawer.state.targetDirty).toBe(true);
		// 迟到响应（含 p_t + can_write）返回：不得覆盖
		releaseProjects({ status: 200, body: [
			{ pid: "p_t", name: "肺组织", can_write: true, slide_ids: [], slides: [] },
			{ pid: "p_r", name: "研究", can_write: true, slide_ids: [], slides: [] },
		] });
		await flush(20);
		expect(h.els["import-target-select"].value).toBe("p_r");
		expect(h.UI.importDrawer.targetState.pid).toBe("p_r");
		h.UI.importDrawer.close();
	});

	it("案例：明确选择未归类 → 不被当前文件夹默认值覆盖", async () => {
		let releaseProjects: (out: RouteResult) => void = () => {};
		const h = bootApp((s) => {
			seedFx(s);
			seedAuth(s, AUTH_U1);
			s.laggy.set("/api/projects", (out) => releaseProjects(out));
		});
		await flush(20);
		h.UI.fb.go("p_t");
		h.UI.importDrawer.open();
		await flush(4);
		h.els["import-target-select"].value = "";
		h.els["import-target-select"].dispatch("change", {});
		releaseProjects({ status: 200, body: [
			{ pid: "p_t", name: "肺组织", can_write: true, slide_ids: [], slides: [] },
		] });
		await flush(20);
		expect(h.els["import-target-select"].value).toBe("");
		expect(h.UI.importDrawer.targetState.pid).toBe("");
		h.UI.importDrawer.close();
	});

	it("案例：重开抽屉 = 新的导入选择会话 → 重新按当前目录默认（不再保留上次手选）", async () => {
		const h = bootApp((s) => { seedFx(s); seedAuth(s, AUTH_U1); });
		await flush(20);
		h.UI.fb.go("p_t");
		await openDrawer(h);
		expect(h.UI.importDrawer.targetState.pid).toBe("p_t");
		h.els["import-target-select"].value = "p_r";
		h.els["import-target-select"].dispatch("change", {});
		h.UI.importDrawer.close();
		const gen1 = h.UI.importDrawer.state.targetGen;
		await openDrawer(h);
		expect(h.UI.importDrawer.state.targetGen).toBe(gen1 + 1);
		expect(h.UI.importDrawer.state.targetDirty).toBe(false);
		expect(h.UI.importDrawer.targetState.pid).toBe("p_t");
		h.UI.importDrawer.close();
	});

	it("案例：上传开始后冻结目标 → 侧栏切换/下拉改动不影响关联归属", async () => {
		const h = bootApp((s) => { seedFx(s); seedAuth(s, AUTH_U1); });
		await flush(20);
		h.UI.fb.go("p_t");
		await openDrawer(h);
		const snap = h.UI.importDrawer.snapshot();
		expect(snap.pid).toBe("p_t");
		// 用户随后改下拉并切走侧栏
		h.els["import-target-select"].value = "p_r";
		h.els["import-target-select"].dispatch("change", {});
		h.UI.importDrawer.close();
		h.UI.fb.go("p_r");
		h.routes.set("/api/project/p_t/slides", () => ({ status: 200, body: { ok: true } }));
		await h.UI.importDrawer.associate("sld_fx_new", "n.svs", snap);
		await flush(20);
		const posts = h.calls.filter((c) => /^\/api\/project\/[^/]+\/slides$/.test(c.url));
		expect(posts.length).toBe(1);
		expect(posts[0].url).toBe("/api/project/p_t/slides");   // 冻结时的目标
		expect(JSON.parse(String(posts[0].opts && posts[0].opts.body))).toEqual({ slide_ids: ["sld_fx_new"] });
		// uploadFile 入口接线：上传开始即快照（行为面由 associate+snapshot 锁定）
		expect(appSrc).toContain("opts.assocTarget = importTargetSnapshot()");
	});

	it("案例：目标关联失败 → 保留切片与重试入口（行内按钮 + 按身份落盘待办），重试沿用原 slide ID/目标", async () => {
		let slidesStatus = 403;
		const storage = fakeStorage();
		const h = bootApp((s) => { seedFx(s); seedAuth(s, AUTH_U1); }, { storage });
		await flush(20);
		h.routes.set("/api/project/p_t/slides", () => ({ status: slidesStatus, body: { error: "denied" } }));
		const row = { _row: bootEl("row") };
		await h.UI.importDrawer.associate("sld_fx07", "fail.svs",
			{ pid: "p_t", newProjectName: "", createdPid: "", createKey: null }, row);
		await flush(10);
		// 失败：toast + 行内重试按钮
		expect(h.els["toast-container"].children.map((c) => c.textContent).join("|"))
			.toContain("imp.assoc.fail2");
		const retryBtn = row._row.children.find((c) => c.textContent === "imp.assoc.retry");
		expect(retryBtn).toBeTruthy();
		// 待办按身份落盘（原 slide ID + 原目标 + 原幂等键）
		const pending = JSON.parse(storage.map.get("pt.fb.assoc.v1:u1")!);
		expect(pending.length).toBe(1);
		expect(pending[0].slideId).toBe("sld_fx07");
		expect(pending[0].target.pid).toBe("p_t");
		// 不自动改成当前目录：期间切到 p_r 再重试 → 仍打原目标
		h.UI.fb.go("p_r");
		slidesStatus = 200;
		retryBtn!.dispatch("click", clickEvt());
		await flush(20);
		const posts = h.calls.filter((c) => /^\/api\/project\/[^/]+\/slides$/.test(c.url));
		expect(posts.length).toBe(2);   // 首次（403）+ 行内重试
		expect(posts[1].url).toBe("/api/project/p_t/slides");
		expect(JSON.parse(String(posts[1].opts && posts[1].opts.body))).toEqual({ slide_ids: ["sld_fx07"] });
		// 成功后待办清除
		expect(storage.map.get("pt.fb.assoc.v1:u1")).toBe("[]");
	});

	it("案例：关联失败后刷新 → 启动自动重试沿用原目标与原幂等键（新项目目标不重复建目录）", async () => {
		const storage1 = fakeStorage();
		const h1 = bootApp((s) => { seedFx(s); seedAuth(s, AUTH_U1); }, { storage: storage1 });
		await flush(20);
		h1.routes.set("/api/project/create", () => ({ status: 500, body: { error: "boom" } }));
		await h1.UI.importDrawer.associate("sld_fx08", "x.svs",
			{ pid: "", newProjectName: "新目录", createdPid: "", createKey: "idem-abc" });
		await flush(10);
		const saved = JSON.parse(storage1.map.get("pt.fb.assoc.v1:u1")!);
		expect(saved[0].target.createKey).toBe("idem-abc");
		expect(saved[0].target.newProjectName).toBe("新目录");
		// 刷新：create 恢复可用 → 启动自动重试原目标
		const h2 = bootApp((s) => {
			seedFx(s);
			seedAuth(s, AUTH_U1);
			s.routes.set("/api/project/create", () => ({ status: 200, body: { pid: "p_new" } }));
			s.routes.set("/api/project/p_new/slides", () => ({ status: 200, body: { ok: true } }));
		}, { storage: fakeStorage(Object.fromEntries(storage1.map)) });
		await flush(30);
		const creates = h2.calls.filter((c) => c.url === "/api/project/create");
		expect(creates.length).toBe(1);
		expect(creates[0].opts && (creates[0].opts.headers as Record<string, string>)["Idempotency-Key"]).toBe("idem-abc");
		const posts = h2.calls.filter((c) => c.url === "/api/project/p_new/slides");
		expect(posts.length).toBe(1);
		expect(JSON.parse(String(posts[0].opts && posts[0].opts.body))).toEqual({ slide_ids: ["sld_fx08"] });
		expect(h2.storage!.map.get("pt.fb.assoc.v1:u1")).toBe("[]");
	});

	it("案例：localStorage 抛错 → 目标关联照常工作（仅失去待办落盘）", async () => {
		const storage = fakeStorage();
		storage.throws = true;
		const h = bootApp((s) => { seedFx(s); seedAuth(s, AUTH_U1); }, { storage });
		await flush(20);
		h.routes.set("/api/project/p_t/slides", () => ({ status: 200, body: { ok: true } }));
		await expect(h.UI.importDrawer.associate("sld_fx09", "ok.svs",
			{ pid: "p_t", newProjectName: "", createdPid: "", createKey: null })).resolves.toBeUndefined();
		await flush(20);
		expect(h.calls.some((c) => c.url === "/api/project/p_t/slides")).toBe(true);
	});

	it("案例：本机转换 handoff 沿用同一默认与冻结规则（convertHandoffTarget）", async () => {
		const h = bootApp((s) => { seedFx(s); seedAuth(s, AUTH_U1); });
		await flush(20);
		h.UI.fb.go("p_t");
		await openDrawer(h);
		const w = (globalThis as { window?: { HP_UPLOAD?: { convertHandoffTarget(): unknown } } }).window;
		expect(w!.HP_UPLOAD!.convertHandoffTarget()).toEqual({ project: "p_t" });
		// 「新项目」目标把名称 + 一次性幂等键交给工具页
		h.els["import-target-select"].value = "new";
		h.els["import-target-select"].dispatch("change", {});
		h.els["import-target-new-name"].value = "转换目录";
		h.els["import-target-new-name"].dispatch("input", {});
		const t2 = w!.HP_UPLOAD!.convertHandoffTarget() as { newProject: { name: string; key: string } };
		expect(t2.newProject.name).toBe("转换目录");
		expect(typeof t2.newProject.key).toBe("string");
		// 再次调用复用同一幂等键（冻结语义）
		expect((w!.HP_UPLOAD!.convertHandoffTarget() as typeof t2).newProject.key).toBe(t2.newProject.key);
		h.UI.importDrawer.close();
	});
});

describe("上传中刷新恢复：完成即按原目标关联（folder-experience §1.3×§5）", () => {
	/** pt.cos.jobs 记录（cosJobsRead 校验形状：job_id/filename/size/confirmed）。 */
	function cosJobRecord(jobId: string, account = "u1") {
		return { job_id: jobId, filename: "restored.tif", size: 1234,
			slide_id: null, account, digests: {}, digest_scheme: "", confirmed: [] };
	}
	function upAssocSeed(jobId: string, target: Record<string, unknown>, at = Date.now()) {
		return JSON.stringify([{ job_id: jobId, target, at }]);
	}
	const UPASSOC_KEY = "pt.fb.upassoc.v1:u1";
	function ingRoute(body: Record<string, unknown>) {
		return () => ({ status: 200, body });
	}
	function slidesPosts(h: ReturnType<typeof bootApp>) {
		return h.calls.filter((c) => /^\/api\/project\/[^/]+\/slides$/.test(c.url));
	}
	function retryButton(h: ReturnType<typeof bootApp>): BootEl | undefined {
		for (const row of h.els["upload-progress-list"].children) {
			const btn = row.children.find((c) => c.textContent === "imp.assoc.retry");
			if (btn) return btn;
		}
		return undefined;
	}

	it("案例：上传中刷新、恢复行完成 → 按开始时冻结的原目标关联恰好一次", async () => {
		const storage = fakeStorage({
			"pt.cos.jobs": JSON.stringify([cosJobRecord("job1")]),
			[UPASSOC_KEY]: upAssocSeed("job1",
				{ pid: "p_t", newProjectName: "", createdPid: "", createKey: null }),
		});
		const h = bootApp((s) => {
			seedFx(s);
			seedAuth(s, AUTH_U1);
			s.routes.set("/api/ingestions/job1", ingRoute(
				{ stage: "viewable", slide_id: "sld_r1", slide: "restored.tif" }));
			s.routes.set("/api/project/p_t/slides", () => ({ status: 200, body: { ok: true } }));
		}, { storage });
		await flush(40);
		const posts = slidesPosts(h);
		expect(posts.length).toBe(1);
		expect(posts[0].url).toBe("/api/project/p_t/slides");
		expect(JSON.parse(String(posts[0].opts && posts[0].opts.body)))
			.toEqual({ slide_ids: ["sld_r1"] });
		// 任务与快照记录消费后清理
		expect(storage.map.get(UPASSOC_KEY)).toBe("[]");
		expect(JSON.parse(storage.map.get("pt.cos.jobs") as string).length).toBe(0);
		// 再次恢复（等价于又一次刷新）：无记录 → 绝不重复关联
		const callsBefore = h.calls.length;
		(globalThis as { window?: { HP_UPLOAD?: { restoreCosJobs(): void } } })
			.window!.HP_UPLOAD!.restoreCosJobs();
		await flush(20);
		expect(slidesPosts(h).length).toBe(1);
		expect(h.calls.length).toBe(callsBefore);
	});

	it("案例：恢复完成时目标目录已被删除 → 明确失败 + 重试入口 + 待办，绝不新建目录", async () => {
		let slidesStatus = 403;
		const storage = fakeStorage({
			"pt.cos.jobs": JSON.stringify([cosJobRecord("job2")]),
			[UPASSOC_KEY]: upAssocSeed("job2",
				{ pid: "p_gone", newProjectName: "", createdPid: "", createKey: null }),
		});
		const h = bootApp((s) => {
			seedFx(s);
			seedAuth(s, AUTH_U1);
			s.routes.set("/api/ingestions/job2", ingRoute(
				{ stage: "viewable", slide_id: "sld_r2", slide: "restored.tif" }));
			s.routes.set("/api/project/p_gone/slides",
				() => ({ status: slidesStatus, body: { error: "denied" } }));
		}, { storage });
		await flush(40);
		// 失败明确可见：toast + 行内重试按钮 + 按身份落盘待办（原目标）
		expect(h.els["toast-container"].children.map((c) => c.textContent).join("|"))
			.toContain("imp.assoc.fail2");
		expect(retryButton(h)).toBeTruthy();
		const pending = JSON.parse(storage.map.get("pt.fb.assoc.v1:u1") as string);
		expect(pending.length).toBe(1);
		expect(pending[0].slideId).toBe("sld_r2");
		expect(pending[0].target.pid).toBe("p_gone");
		// 没有任何「新建目录」的兜底（快照是已有目录，不是 new+createKey）
		expect(h.calls.some((c) => c.url === "/api/project/create")).toBe(false);
		// 目录恢复可写后点行内重试 → 只重试原目标，成功清待办
		slidesStatus = 200;
		retryButton(h)!.dispatch("click", clickEvt());
		await flush(20);
		const posts = slidesPosts(h);
		expect(posts.length).toBe(2);
		expect(posts[1].url).toBe("/api/project/p_gone/slides");
		expect(JSON.parse(String(posts[1].opts && posts[1].opts.body)))
			.toEqual({ slide_ids: ["sld_r2"] });
		expect(storage.map.get("pt.fb.assoc.v1:u1")).toBe("[]");
	});

	it("案例：快照是「新目录」目标 → 恢复完成用原幂等键建目录并关联，不重复建", async () => {
		const storage = fakeStorage({
			"pt.cos.jobs": JSON.stringify([cosJobRecord("job3")]),
			[UPASSOC_KEY]: upAssocSeed("job3",
				{ pid: "", newProjectName: "恢复目录", createdPid: "", createKey: "idem-r1" }),
		});
		const h = bootApp((s) => {
			seedFx(s);
			seedAuth(s, AUTH_U1);
			s.routes.set("/api/ingestions/job3", ingRoute(
				{ stage: "viewable", slide_id: "sld_r3", slide: "restored.tif" }));
			s.routes.set("/api/project/create", () => ({ status: 200, body: { pid: "p_new" } }));
			s.routes.set("/api/project/p_new/slides", () => ({ status: 200, body: { ok: true } }));
		}, { storage });
		await flush(40);
		const creates = h.calls.filter((c) => c.url === "/api/project/create");
		expect(creates.length).toBe(1);
		expect(creates[0].opts && (creates[0].opts.headers as Record<string, string>)["Idempotency-Key"])
			.toBe("idem-r1");
		const posts = slidesPosts(h);
		expect(posts.length).toBe(1);
		expect(posts[0].url).toBe("/api/project/p_new/slides");
		expect(JSON.parse(String(posts[0].opts && posts[0].opts.body)))
			.toEqual({ slide_ids: ["sld_r3"] });
	});

	it("案例：换了账号（其余记录俱在）→ 不读不关联，原快照原样保留", async () => {
		const storage = fakeStorage({
			"pt.cos.jobs": JSON.stringify([cosJobRecord("job1")]),   // 任务属 u1
			[UPASSOC_KEY]: upAssocSeed("job1",
				{ pid: "p_t", newProjectName: "", createdPid: "", createKey: null }),
		});
		const h = bootApp((s) => {
			seedFx(s);
			seedAuth(s, { ...AUTH_U1, user_id: "uB" });
			// 最坏情况：轮询对 uB 也返回 viewable——仍绝不按 A 的快照关联
			s.routes.set("/api/ingestions/job1", ingRoute(
				{ stage: "viewable", slide_id: "sld_r1", slide: "restored.tif" }));
			s.routes.set("/api/project/p_t/slides", () => ({ status: 200, body: { ok: true } }));
		}, { storage });
		await flush(40);
		expect(slidesPosts(h).length).toBe(0);
		expect(JSON.parse(storage.map.get(UPASSOC_KEY) as string).length).toBe(1);
	});

	it("案例：快照缺失或过期 → 不猜，保持未归类（无任何关联请求）", async () => {
		// 缺失
		const h1 = bootApp((s) => {
			seedFx(s);
			seedAuth(s, AUTH_U1);
			s.routes.set("/api/ingestions/job1", ingRoute(
				{ stage: "viewable", slide_id: "sld_r1", slide: "restored.tif" }));
		}, { storage: fakeStorage({ "pt.cos.jobs": JSON.stringify([cosJobRecord("job1")]) }) });
		await flush(40);
		expect(slidesPosts(h1).length).toBe(0);
		// 过期（31 天前）
		const h2 = bootApp((s) => {
			seedFx(s);
			seedAuth(s, AUTH_U1);
			s.routes.set("/api/ingestions/job1", ingRoute(
				{ stage: "viewable", slide_id: "sld_r1", slide: "restored.tif" }));
		}, { storage: fakeStorage({
			"pt.cos.jobs": JSON.stringify([cosJobRecord("job1")]),
			[UPASSOC_KEY]: upAssocSeed("job1",
				{ pid: "p_t", newProjectName: "", createdPid: "", createKey: null },
				Date.now() - 31 * 24 * 3600 * 1000),
		}) });
		await flush(40);
		expect(slidesPosts(h2).length).toBe(0);
	});

	// R5 复核（integration-20261014 §R5）：两种响应顺序都必须恰好关联一次。
	// 本案例（源自评审补丁 folder-regression.patch）：**身份先就绪**——
	// ingestion 故意延迟，/api/auth/info 先完成；旧行为在 viewable 时立即
	// cosJobRemove（内含 fbUpAssocTake 消费快照），身份闸里第二次 Take 落空
	// → 0 次关联、静默未归类。修复后：先取快照再清任务记录，与请求到达
	// 顺序无关。
	it("案例：身份先就绪（auth/info 先于 ingestion 返回）→ 仍按原目标关联恰好一次（R5 回归）", async () => {
		const storage = fakeStorage({
			"pt.cos.jobs": JSON.stringify([cosJobRecord("job_review")]),
			[UPASSOC_KEY]: upAssocSeed("job_review",
				{ pid: "p_t", newProjectName: "", createdPid: "", createKey: null }),
		});
		const h = bootApp((s) => {
			seedFx(s);
			seedAuth(s, AUTH_U1);
			s.laggy.set("/api/ingestions/job_review", () => {});
			s.routes.set("/api/project/p_t/slides", () => ({ status: 200, body: { ok: true } }));
		}, { storage });
		await flush(40); // /api/auth/info 完成而 ingestion 仍挂起
		expect(JSON.parse(storage.map.get(UPASSOC_KEY)!).length).toBe(1);
		h.laggy.get("/api/ingestions/job_review")!({ status: 200, body: { stage: "viewable", slide_id: "sld_review", slide: "restored.tif" } });
		await flush(40);
		const posts = slidesPosts(h);
		expect(posts.length).toBe(1);
		expect(JSON.parse(String(posts[0].opts && posts[0].opts.body)))
			.toEqual({ slide_ids: ["sld_review"] });
		// 快照一次性消费、任务记录清理；再次恢复（再刷新）绝不重复关联
		expect(storage.map.get(UPASSOC_KEY)).toBe("[]");
		expect(JSON.parse(storage.map.get("pt.cos.jobs") as string).length).toBe(0);
		const callsBefore = h.calls.length;
		(globalThis as { window?: { HP_UPLOAD?: { restoreCosJobs(): void } } })
			.window!.HP_UPLOAD!.restoreCosJobs();
		await flush(20);
		expect(h.calls.length).toBe(callsBefore);
	});

	// R5 的另一半：**任务先到**——viewable 在 /api/auth/info 之前返回，
	// 关联必须等身份就绪且仍然恰好一次（旧行为里 cosJobRemove 的 Take 在
	// 无身份域时退化为空操作，靠身份闸里的 Take 成功；修复后语义不变）。
	it("案例：任务先到（ingestion 先于 auth/info 返回）→ 等身份就绪后仍恰好关联一次", async () => {
		const storage = fakeStorage({
			"pt.cos.jobs": JSON.stringify([cosJobRecord("job4")]),
			[UPASSOC_KEY]: upAssocSeed("job4",
				{ pid: "p_t", newProjectName: "", createdPid: "", createKey: null }),
		});
		const h = bootApp((s) => {
			seedFx(s);
			seedAuth(s, AUTH_U1);
			s.laggy.set("/api/auth/info", () => {});
			s.routes.set("/api/ingestions/job4", ingRoute(
				{ stage: "viewable", slide_id: "sld_r4", slide: "restored.tif" }));
			s.routes.set("/api/project/p_t/slides", () => ({ status: 200, body: { ok: true } }));
		}, { storage });
		await flush(40);
		// 身份未就绪：绝不发起关联（快照按身份域存取，无键不读）
		expect(slidesPosts(h).length).toBe(0);
		// 任务记录已随 viewable 清理（§5：已可查看即清理），快照仍在
		expect(JSON.parse(storage.map.get("pt.cos.jobs") as string).length).toBe(0);
		expect(JSON.parse(storage.map.get(UPASSOC_KEY) as string).length).toBe(1);
		h.laggy.get("/api/auth/info")!({ status: 200, body: AUTH_U1 });
		await flush(40);
		const posts = slidesPosts(h);
		expect(posts.length).toBe(1);
		expect(JSON.parse(String(posts[0].opts && posts[0].opts.body)))
			.toEqual({ slide_ids: ["sld_r4"] });
		expect(storage.map.get(UPASSOC_KEY)).toBe("[]");
	});

	// 取消/终态仍清理快照（R5 修复约束：只把恢复成功路径改成先取后清；
	// 终态路径 cosJobRemove 的「记录 + 快照」一并清理保持不变）。
	it("案例：恢复轮询遇终态 → 任务记录与原目标快照一并清理，绝无关联请求", async () => {
		const storage = fakeStorage({
			"pt.cos.jobs": JSON.stringify([cosJobRecord("job9")]),
			[UPASSOC_KEY]: upAssocSeed("job9",
				{ pid: "p_t", newProjectName: "", createdPid: "", createKey: null }),
		});
		const h = bootApp((s) => {
			seedFx(s);
			seedAuth(s, AUTH_U1);
			s.routes.set("/api/ingestions/job9", ingRoute({ stage: "terminal" }));
		}, { storage });
		await flush(40);
		expect(JSON.parse(storage.map.get("pt.cos.jobs") as string).length).toBe(0);
		expect(storage.map.get(UPASSOC_KEY)).toBe("[]");
		expect(slidesPosts(h).length).toBe(0);
	});
});
