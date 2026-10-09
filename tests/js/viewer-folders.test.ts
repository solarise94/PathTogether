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
		style: {},
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

function bootApp(seed?: (s: Seedable) => void, opts: { search?: string } = {}) {
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
			style: {},
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
		localStorage: null,
	};
	(globalThis as { document?: unknown }).document = doc;
	(globalThis as { window?: unknown }).window = w;
	(globalThis as { fetch?: unknown }).fetch = fetchImpl;
	(globalThis as { HP_ViewerCore?: unknown }).HP_ViewerCore = w.HP_ViewerCore;
	(globalThis as { HP_I18N?: unknown }).HP_I18N = w.HP_I18N;

	if (seed) seed({ routes, hang, laggy });
	new Function("window", "document", "fetch", "location", appSrc)(w, doc, fetchImpl, w.location);
	(docListeners["DOMContentLoaded"] || []).forEach((cb) => cb());

	const UI = (w as { HP_PROJECT_UI?: { fb: FbUI; viewerState: { slide: Record<string, unknown> | null }; openSlide(ref: string): void } }).HP_PROJECT_UI!;
	return {
		els, calls, routes, hang, laggy, doc, opens, closes: () => closes.n,
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
	it("pointerenter 加 .extracted（滑出预览）但不调用 openSlide/info；pointerleave 收回", async () => {
		const h = bootApp((s) => { seedStd(s); });
		await flush();
		const infoCalls = () => h.calls.filter((c) => /\/api\/slides?\/[^/]+\/info$/.test(c.url)).length;
		expect(infoCalls()).toBe(0);
		const card = fbHits(h)[0];
		card.dispatch("pointerenter", { pointerType: "mouse" });
		expect(card.classList.contains("extracted")).toBe(true);
		expect(infoCalls()).toBe(0);
		expect(h.opens.length).toBe(0);
		// 触屏 pointerenter 也不滑出（触屏点击直接打开）
		const card2 = fbHits(h)[1];
		card2.dispatch("pointerenter", { pointerType: "touch" });
		expect(card2.classList.contains("extracted")).toBe(false);
		card.dispatch("pointerleave");
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
