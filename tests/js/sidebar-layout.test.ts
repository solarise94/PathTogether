/**
 * 升级 A：可收起的桌面左侧栏（§4.1/§4.2）。
 *
 * 加载真实 static/app.js（最小 DOM + fetch stub），锁定：
 *   - 偏好键含身份维度（pt.sb.v1|站点:账号）；损坏/缺失偏好回落默认收起；
 *     storage 不可用不阻塞；
 *   - 桌面首次进入默认收起；toggle 切换并持久化；aria-expanded/aria-controls
 *     随状态更新；
 *   - 身份到位（onScopeReady）后按真实身份重读偏好；用户已手动操作不覆盖；
 *   - 手机（≤768px）：抽屉默认关闭，桌面偏好不把抽屉自动打开；closeDrawer
 *     恢复焦点；断点切换清理遮罩并恢复当前设备布局；
 *   - 空态「选择切片」expandAndFocusSearch：展开侧栏 + 聚焦搜索框 + 持久化；
 *   - §4.2 几何链：侧栏布局变化 → onLayoutChange → 下一帧 forceResize +
 *     画布背衬尺寸同步；viewer "resize" 事件 → 先量容器再重绘。
 *
 * 纯逻辑（键构造/偏好解析）与 DOM 决策逻辑（控制器、resize 链）都走生产代码
 * 路径，不 mock 被测逻辑本身；只注入 DOM/存储/媒体查询等边界。
 */
import { afterEach, describe, expect, it, vi } from "vitest";
import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const appSrc = readFileSync(resolve(here, "../../static/app.js"), "utf8");

// ---------- 假元素：记录 class/attr/监听器/焦点 ----------
interface FakeEl extends Record<string, unknown> {
	id: string;
	hidden: boolean;
	title: string;
	style: Record<string, string>;
	dataset: Record<string, string>;
	textContent: string;
	innerHTML: string;
	value: string;
	focusCount: number;
	children: FakeEl[];
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
	focus: () => void;
	contains: (other: FakeEl) => boolean;
	appendChild: (c: unknown) => void;
	querySelector: (sel: string) => FakeEl | null;
	querySelectorAll: (sel: string) => FakeEl[];
	closest: () => null;
	getBoundingClientRect: () => { width: number; height: number };
	getContext: () => Record<string, unknown>;
}

function fakeEl(id = ""): FakeEl {
	const classes = new Set<string>();
	const attrs = new Map<string, string>();
	const listeners: Record<string, Array<(e?: unknown) => void>> = {};
	const children: FakeEl[] = [];
	const el: FakeEl = {
		id,
		hidden: false,
		title: "",
		style: {},
		dataset: {},
		textContent: "",
		innerHTML: "",
		value: "",
		focusCount: 0,
		children,
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
		focus: () => {
			el.focusCount += 1;
		},
		contains: (other) => children.includes(other),
		appendChild: (c) => {
			if (c && typeof c === "object" && "id" in (c as object)) children.push(c as FakeEl);
		},
		querySelector: () => null,
		querySelectorAll: () => [],
		closest: () => null,
		getBoundingClientRect: () => ({ width: 320, height: 600 }),
		getContext: () => ({ setTransform() {}, clearRect() {} }),
	};
	return el;
}

// ---------- Map 后端 storage（可注入抛错行为） ----------
function fakeStorage(initial: Record<string, string> = {}, opts: { throwOnGet?: boolean; throwOnSet?: boolean } = {}) {
	const map = new Map(Object.entries(initial));
	return {
		getItem: (k: string) => {
			if (opts.throwOnGet) throw new Error("storage blocked");
			return map.has(k) ? (map.get(k) as string) : null;
		},
		setItem: (k: string, v: string) => {
			if (opts.throwOnSet) throw new Error("quota exceeded");
			map.set(k, v);
		},
		_map: map,
	};
}

type Deps = {
	sidebar: FakeEl;
	sidebarMask: FakeEl;
	menuBtn: FakeEl;
	mq: { matches: boolean };
	storage: ReturnType<typeof fakeStorage> | null;
	scope: string | (() => string);
	doc: { activeElement: FakeEl | null };
	onLayoutChange: () => void;
	focusSearch: () => void;
} & Record<string, unknown>;

interface Sidebar {
	init: () => void;
	isMobile: () => boolean;
	isDesktopCollapsed: () => boolean;
	isDrawerOpen: () => boolean;
	toggle: () => void;
	closeDrawer: () => void;
	onScopeReady: () => void;
	onBreakpointChange: () => void;
	expandAndFocusSearch: () => void;
	refreshButton: () => void;
}

interface SidebarModule {
	createSidebarController: (deps: Deps) => Sidebar;
	sidebarPrefKey: (scope: string) => string;
	parseSidebarPref: (raw: string | null) => { collapsed: boolean } | null;
	readSidebarPref: (storage: unknown, scope: string) => { collapsed: boolean } | null;
	writeSidebarPref: (storage: unknown, scope: string, collapsed: boolean) => void;
}

function makeDeps(overrides: Partial<Deps> = {}): Deps {
	const sidebar = fakeEl("sidebar");
	const sidebarMask = fakeEl("sidebar-mask");
	const menuBtn = fakeEl("menu-btn");
	sidebar.children.push(menuBtn); // contains() 需要
	return {
		sidebar,
		sidebarMask,
		menuBtn,
		mq: { matches: false },
		storage: fakeStorage(),
		scope: "official:u1",
		doc: { activeElement: null },
		onLayoutChange: () => {},
		focusSearch: () => {},
		...overrides,
	};
}

function makeController(overrides: Partial<Deps> = {}) {
	bootApp(); // 装载生产 app.js，暴露 window.HP_SIDEBAR
	const HP = (globalThis as { window?: { HP_SIDEBAR?: SidebarModule } }).window?.HP_SIDEBAR as SidebarModule;
	const deps = makeDeps(overrides);
	const layoutSpy = vi.fn();
	deps.onLayoutChange = layoutSpy;
	const ctrl = HP.createSidebarController(deps);
	return {
		ctrl,
		deps,
		layoutSpy,
		sidebar: deps.sidebar,
		mask: deps.sidebarMask,
		btn: deps.menuBtn,
		storage: deps.storage as ReturnType<typeof fakeStorage>,
	};
}

// ---------- 启动真实 app.js（与 upload-csrf.test.ts 的 loadApp 同风格） ----------
function bootApp(opts: { mobile?: boolean; storageMap?: Record<string, string>; viewerRect?: { w: number; h: number } } = {}) {
	const els: Record<string, FakeEl> = {};
	const docListeners: Record<string, Array<() => void>> = {};
	const rafCbs: Array<() => void> = [];
	const mqListeners: Array<() => void> = [];
	const mq = { matches: !!opts.mobile };

	// viewer：经 HP_ViewerCore.create 注入（生产 index.html 的加载路径）
	const handlers: Record<string, Array<(e?: unknown) => void>> = {};
	const rect = opts.viewerRect || { w: 800, h: 600 };
	const forceResize = vi.fn();
	const fakeViewer = {
		container: {
			style: {} as Record<string, string>,
			getBoundingClientRect: () => ({ width: rect.w, height: rect.h, left: 0, top: 0 }),
			insertBefore() {},
		},
		canvas: {},
		viewport: null,
		addHandler(type: string, fn: (e?: unknown) => void) {
			(handlers[type] ||= []).push(fn);
		},
		forceResize,
	};

	const fetchImpl = vi.fn(() =>
		Promise.resolve({ ok: true, status: 200, clone() { return this; }, json: () => Promise.resolve([]) }),
	) as unknown as typeof fetch;

	const doc = {
		readyState: "loading",
		cookie: "",
		getElementById(id: string) {
			if (!els[id]) els[id] = fakeEl(id);
			return els[id];
		},
		createElement: () => fakeEl(),
		addEventListener(type: string, cb: () => void) {
			(docListeners[type] ||= []).push(cb);
		},
		querySelector: () => null,
		querySelectorAll: () => [] as FakeEl[],
		body: fakeEl("body"),
	};

	const w: Record<string, unknown> = {
		HP_I18N: { t: (k: string) => k, getLang: () => "zh" },
		HP_ViewerCore: { create: () => fakeViewer },
		HP_API: {},
		fetch: fetchImpl,
		location: { href: "http://local/" },
		matchMedia: (q: string) => {
			if (q !== "(max-width: 768px)") return { matches: false, addEventListener() {}, addListener() {} };
			// matches 用 getter 惰性读取：断点切换测试改 mq.matches 后立即可见
			return {
				get matches() {
					return mq.matches;
				},
				addEventListener(_t: string, cb: () => void) {
					mqListeners.push(cb);
				},
				addListener(cb: () => void) {
					mqListeners.push(cb);
				},
			};
		},
		requestAnimationFrame: (cb: () => void) => {
			rafCbs.push(cb);
			return rafCbs.length;
		},
		addEventListener() {},
		localStorage: opts.storageMap ? fakeStorage(opts.storageMap) : null,
	};
	// 本测试环境的 storage 行为由注入对象表达（浏览器里 safeLocalStorage 探测
	// window.localStorage 可用即采用）。

	(globalThis as { document: unknown }).document = doc;
	(globalThis as { window: unknown }).window = w;
	(globalThis as { fetch: typeof fetch }).fetch = fetchImpl;
	// app.js initViewer 里用裸标识符 HP_ViewerCore（浏览器=window 属性）；
	// harness 里 window 是参数对象，需另挂 globalThis
	(globalThis as { HP_ViewerCore?: unknown }).HP_ViewerCore = { create: () => fakeViewer };

	// eslint-disable-next-line @typescript-eslint/no-explicit-any
	new Function("window", "document", "fetch", "location", appSrc)(w, doc, fetchImpl, (w as { location: unknown }).location);

	// 触发 DOMContentLoaded → init()
	(docListeners["DOMContentLoaded"] || []).forEach((cb) => cb());
	// 刷新挂起的 rAF（init 期间的布局同步）
	const flushRaf = () => {
		while (rafCbs.length) (rafCbs.shift() as () => void)();
	};
	flushRaf();

	const HP = (w as { HP_SIDEBAR?: SidebarModule }).HP_SIDEBAR as SidebarModule;
	return {
		els,
		handlers,
		forceResize,
		rect,
		flushRaf,
		setMobile(mobile: boolean) {
			mq.matches = mobile;
			mqListeners.forEach((cb) => cb());
		},
		HP,
	};
}

describe("HP_SIDEBAR 偏好存取纯逻辑", () => {
	afterEach(() => {
		delete (globalThis as { window?: unknown }).window;
		delete (globalThis as { document?: unknown }).document;
		delete (globalThis as { fetch?: unknown }).fetch;
		delete (globalThis as { HP_ViewerCore?: unknown }).HP_ViewerCore;
	});

	it("偏好键包含站点+账号身份维度（pt.sb.v1|official:<uid>）", () => {
		bootApp();
		const HP = (globalThis as { window?: { HP_SIDEBAR?: SidebarModule } }).window?.HP_SIDEBAR;
		expect(HP).toBeTruthy();
		expect(HP!.sidebarPrefKey("official:u123")).toBe("pt.sb.v1|official:u123");
		expect(HP!.sidebarPrefKey("official:local")).toBe("pt.sb.v1|official:local");
	});

	it("parseSidebarPref：合法结构保留；损坏/结构不符返回 null", () => {
		bootApp();
		const HP = (globalThis as { window?: { HP_SIDEBAR?: SidebarModule } }).window?.HP_SIDEBAR;
		expect(HP!.parseSidebarPref('{"collapsed":false,"t":1}')).toEqual({ collapsed: false });
		expect(HP!.parseSidebarPref('{"collapsed":true}')).toEqual({ collapsed: true });
		expect(HP!.parseSidebarPref("not json")).toBeNull();
		expect(HP!.parseSidebarPref('{"collapsed":"yes"}')).toBeNull();
		expect(HP!.parseSidebarPref("[]")).toBeNull();
		expect(HP!.parseSidebarPref(null)).toBeNull();
	});

	it("readSidebarPref：getItem 抛错/存储缺失不抛出，返回 null（不阻塞页面）", () => {
		bootApp();
		const HP = (globalThis as { window?: { HP_SIDEBAR?: SidebarModule } }).window?.HP_SIDEBAR;
		expect(HP!.readSidebarPref(null, "official:u1")).toBeNull();
		expect(HP!.readSidebarPref(fakeStorage({}, { throwOnGet: true }), "official:u1")).toBeNull();
	});

	it("writeSidebarPref：setItem 抛错不外溢（仅失去持久化）", () => {
		bootApp();
		const HP = (globalThis as { window?: { HP_SIDEBAR?: SidebarModule } }).window?.HP_SIDEBAR;
		expect(() => HP!.writeSidebarPref(fakeStorage({}, { throwOnSet: true }), "official:u1", false)).not.toThrow();
	});
});

describe("桌面侧栏：默认收起 / toggle 持久化 / aria / 断点", () => {
	afterEach(() => {
		delete (globalThis as { window?: unknown }).window;
		delete (globalThis as { document?: unknown }).document;
		delete (globalThis as { fetch?: unknown }).fetch;
		delete (globalThis as { HP_ViewerCore?: unknown }).HP_ViewerCore;
	});

	it("无偏好 init：默认收起，aria-expanded=false、aria-controls=sidebar，按钮文案=展开侧栏", () => {
		const { ctrl, sidebar, btn, layoutSpy } = makeController();
		ctrl.init();
		expect(ctrl.isDesktopCollapsed()).toBe(true);
		expect(sidebar.classList.contains("collapsed")).toBe(true);
		expect(btn.getAttribute("aria-controls")).toBe("sidebar");
		expect(btn.getAttribute("aria-expanded")).toBe("false");
		expect(btn.getAttribute("aria-label")).toBe("tb.sidebar.expand");
		// 每次布局应用都通知 Viewer 同步（§4.2 触发链入口）
		expect(layoutSpy).toHaveBeenCalled();
	});

	it("toggle：收起↔展开切换并写入偏好；aria-expanded 跟随", () => {
		const { ctrl, sidebar, btn, storage } = makeController();
		ctrl.init();
		ctrl.toggle();
		expect(ctrl.isDesktopCollapsed()).toBe(false);
		expect(sidebar.classList.contains("collapsed")).toBe(false);
		expect(btn.getAttribute("aria-expanded")).toBe("true");
		expect(btn.getAttribute("aria-label")).toBe("tb.sidebar.collapse");
		const saved = HP_parsePref(storage, "official:u1");
		expect(saved).toEqual({ collapsed: false });
		ctrl.toggle();
		expect(ctrl.isDesktopCollapsed()).toBe(true);
		expect(HP_parsePref(storage, "official:u1")).toEqual({ collapsed: true });
	});

	function HP_parsePref(storage: ReturnType<typeof fakeStorage>, key: string) {
		const HP = (globalThis as { window?: { HP_SIDEBAR?: SidebarModule } }).window?.HP_SIDEBAR;
		return HP!.parseSidebarPref(storage.getItem("pt.sb.v1|" + key));
	}

	it("已有偏好（展开）init：直接展开，不默认收起", () => {
		const { ctrl, sidebar, storage } = makeController({
			storage: fakeStorage({ "pt.sb.v1|official:u1": JSON.stringify({ collapsed: false }) }),
		});
		ctrl.init();
		expect(ctrl.isDesktopCollapsed()).toBe(false);
		expect(sidebar.classList.contains("collapsed")).toBe(false);
		expect(storage.getItem("pt.sb.v1|official:u1")).toContain('"collapsed":false');
	});

	it("损坏偏好 init：回落默认收起", () => {
		const { ctrl, sidebar } = makeController({
			storage: fakeStorage({ "pt.sb.v1|official:u1": "{{{bad json" }),
		});
		ctrl.init();
		expect(ctrl.isDesktopCollapsed()).toBe(true);
		expect(sidebar.classList.contains("collapsed")).toBe(true);
	});

	it("onScopeReady：身份到位后按真实身份重读偏好；用户已手动操作则不覆盖", () => {
		bootApp(); // 提供 window.HP_SIDEBAR
		const HP = (globalThis as { window?: { HP_SIDEBAR?: SidebarModule } }).window?.HP_SIDEBAR;
		const storage = fakeStorage({
			"pt.sb.v1|official:u8": JSON.stringify({ collapsed: true }),
			"pt.sb.v1|official:u9": JSON.stringify({ collapsed: false }),
		});
		let scope: string = "official:u8";
		const deps = makeDeps({ storage, scope: () => scope });
		const ctrl = HP!.createSidebarController(deps);
		ctrl.init();
		expect(ctrl.isDesktopCollapsed()).toBe(true); // u8 偏好：收起
		// 用户未触摸：切到 u9 → 采用 u9 偏好（展开）
		scope = "official:u9";
		ctrl.onScopeReady();
		expect(ctrl.isDesktopCollapsed()).toBe(false);
		// 用户手动收起（userTouched=true，并写 u9 偏好）
		ctrl.toggle();
		expect(ctrl.isDesktopCollapsed()).toBe(true);
		// 再切回 u9：偏好是展开，但用户操作优先，不被覆盖
		ctrl.onScopeReady();
		expect(ctrl.isDesktopCollapsed()).toBe(true);
	});

	it("断点切换（mq.matches 变更后）：清理手机遮罩/抽屉，恢复对应设备布局", () => {
		const mq = { matches: false };
		const { ctrl, sidebar, mask } = makeController({ mq });
		ctrl.init(); // 桌面收起
		ctrl.toggle(); // 用户展开
		expect(sidebar.classList.contains("collapsed")).toBe(false);
		// 缩到手机断点
		mq.matches = true;
		ctrl.onBreakpointChange();
		expect(sidebar.classList.contains("open")).toBe(false);
		expect(sidebar.classList.contains("collapsed")).toBe(false); // 收起类被清理，宽度交给抽屉规则
		expect(mask.classList.contains("open")).toBe(false);
		// 回到桌面断点：恢复桌面意图态（展开）
		mq.matches = false;
		ctrl.onBreakpointChange();
		expect(sidebar.classList.contains("collapsed")).toBe(false);
		expect(sidebar.classList.contains("open")).toBe(false);
	});
});

describe("手机抽屉：默认关闭 / 桌面偏好不外溢 / 焦点恢复", () => {
	afterEach(() => {
		delete (globalThis as { window?: unknown }).window;
		delete (globalThis as { document?: unknown }).document;
		delete (globalThis as { fetch?: unknown }).fetch;
		delete (globalThis as { HP_ViewerCore?: unknown }).HP_ViewerCore;
	});

	it("手机 init：抽屉关闭、无遮罩；桌面偏好（展开）不把抽屉自动打开", () => {
		const { ctrl, sidebar, mask, btn, storage } = makeController({
			mq: { matches: true },
			storage: fakeStorage({ "pt.sb.v1|official:u1": JSON.stringify({ collapsed: false }) }),
		});
		ctrl.init();
		expect(ctrl.isDrawerOpen()).toBe(false);
		expect(sidebar.classList.contains("open")).toBe(false);
		expect(mask.classList.contains("open")).toBe(false);
		expect(btn.getAttribute("aria-expanded")).toBe("false");
		// 手机上不写偏好
		expect(storage.getItem("pt.sb.v1|official:u1")).toBe(JSON.stringify({ collapsed: false }));
	});

	it("toggle 开抽屉：sidebar+mask 加 open；再 toggle 关闭", () => {
		const { ctrl, sidebar, mask, btn } = makeController({ mq: { matches: true } });
		ctrl.init();
		ctrl.toggle();
		expect(ctrl.isDrawerOpen()).toBe(true);
		expect(sidebar.classList.contains("open")).toBe(true);
		expect(mask.classList.contains("open")).toBe(true);
		expect(btn.getAttribute("aria-expanded")).toBe("true");
		ctrl.toggle();
		expect(ctrl.isDrawerOpen()).toBe(false);
		expect(mask.classList.contains("open")).toBe(false);
		expect(btn.getAttribute("aria-expanded")).toBe("false");
	});

	it("closeDrawer：焦点在抽屉内时关闭后回到 menu-btn（a11y）", () => {
		bootApp();
		const HP = (globalThis as { window?: { HP_SIDEBAR?: SidebarModule } }).window?.HP_SIDEBAR!;
		const searchInDrawer = fakeEl("slide-search");
		const deps = makeDeps({ mq: { matches: true } });
		deps.sidebar.children.push(searchInDrawer);
		const ctrl = HP.createSidebarController(deps);
		ctrl.init();
		ctrl.toggle();
		expect(ctrl.isDrawerOpen()).toBe(true);
		deps.doc.activeElement = searchInDrawer;
		ctrl.closeDrawer();
		expect(ctrl.isDrawerOpen()).toBe(false);
		expect((deps.menuBtn as FakeEl).focusCount).toBe(1);
	});

	it("closeDrawer 未打开时是 no-op（不误触布局同步）", () => {
		const { ctrl, layoutSpy } = makeController({ mq: { matches: true } });
		ctrl.init();
		const calls = layoutSpy.mock.calls.length;
		ctrl.closeDrawer();
		expect(layoutSpy.mock.calls.length).toBe(calls);
	});
});

describe("空态「选择切片」：展开 + 聚焦搜索框", () => {
	afterEach(() => {
		delete (globalThis as { window?: unknown }).window;
		delete (globalThis as { document?: unknown }).document;
		delete (globalThis as { fetch?: unknown }).fetch;
		delete (globalThis as { HP_ViewerCore?: unknown }).HP_ViewerCore;
	});

	it("桌面收起时：展开侧栏、持久化为展开、调用 focusSearch", () => {
		const focusSearch = vi.fn();
		const { ctrl, sidebar, storage } = makeController({ focusSearch: focusSearch as unknown as () => void });
		ctrl.init();
		expect(ctrl.isDesktopCollapsed()).toBe(true);
		ctrl.expandAndFocusSearch();
		expect(sidebar.classList.contains("collapsed")).toBe(false);
		expect(focusSearch).toHaveBeenCalledTimes(1);
		const HP = (globalThis as { window?: { HP_SIDEBAR?: SidebarModule } }).window?.HP_SIDEBAR;
		expect(HP!.parseSidebarPref(storage.getItem("pt.sb.v1|official:u1"))).toEqual({ collapsed: false });
	});

	it("手机时：打开抽屉（不写偏好）并聚焦搜索框", () => {
		const focusSearch = vi.fn();
		const { ctrl, sidebar, storage } = makeController({
			mq: { matches: true },
			focusSearch: focusSearch as unknown as () => void,
		});
		ctrl.init();
		ctrl.expandAndFocusSearch();
		expect(sidebar.classList.contains("open")).toBe(true);
		expect(focusSearch).toHaveBeenCalledTimes(1);
		// 手机上 expand 不写偏好
		expect(storage._map.size).toBe(0);
	});
});

describe("生产装配（真实 app.js init）：默认收起 + resize 触发链", () => {
	afterEach(() => {
		vi.restoreAllMocks();
		delete (globalThis as { window?: unknown }).window;
		delete (globalThis as { document?: unknown }).document;
		delete (globalThis as { fetch?: unknown }).fetch;
		delete (globalThis as { HP_ViewerCore?: unknown }).HP_ViewerCore;
	});

	it("桌面启动：#sidebar 收起、menu-btn aria 就绪；空态可见；rAF 后 forceResize+画布同步", () => {
		const app = bootApp();
		const sidebar = app.els["sidebar"];
		const btn = app.els["menu-btn"];
		expect(sidebar.classList.contains("collapsed")).toBe(true); // 首次进入默认收起
		expect(btn.getAttribute("aria-controls")).toBe("sidebar");
		expect(btn.getAttribute("aria-expanded")).toBe("false");
		expect(app.els["viewer-empty"].hidden).toBe(false); // 无切片空态可见
		// §4.2：布局变化 → onLayoutChange → 下一帧 forceResize + 画布背衬同步
		expect(app.forceResize).toHaveBeenCalled();
		const canvas = app.els["anno-canvas"];
		expect(canvas.style.width).toBe("800px");
		expect(canvas.style.height).toBe("600px");
	});

	it("点击 menu-btn：侧栏展开（collapsed 移除），aria 翻转，并再次触发布局同步", () => {
		const app = bootApp();
		const sidebar = app.els["sidebar"];
		const btn = app.els["menu-btn"];
		btn.dispatch("click");
		expect(sidebar.classList.contains("collapsed")).toBe(false);
		expect(btn.getAttribute("aria-expanded")).toBe("true");
		app.flushRaf(); // 展开后的布局同步（rAF 排队）落地
		expect(app.forceResize.mock.calls.length).toBeGreaterThanOrEqual(2);
	});

	it("OSD 'resize' 事件（容器尺寸变化）→ 画布按新容器尺寸重设后重绘", () => {
		const app = bootApp();
		const canvas = app.els["anno-canvas"];
		expect(canvas.style.width).toBe("800px");
		// 模拟侧栏展开后容器变宽（flex 释放空间给 viewer），OSD 触发 resize 事件
		app.rect.w = 1120;
		(app.handlers["resize"] || []).forEach((fn) => fn({ newContainerSize: { x: 1120, y: 600 } }));
		expect(canvas.style.width).toBe("1120px");
		expect(canvas.style.height).toBe("600px");
	});

	it("断点切到手机：遮罩/抽屉清理，抽屉保持关闭（桌面收起类也清理）", () => {
		const app = bootApp(); // 桌面默认收起
		app.setMobile(true);
		const sidebar = app.els["sidebar"];
		const mask = app.els["sidebar-mask"];
		expect(sidebar.classList.contains("collapsed")).toBe(false);
		expect(sidebar.classList.contains("open")).toBe(false);
		expect(mask.classList.contains("open")).toBe(false);
		expect(app.els["menu-btn"].getAttribute("aria-expanded")).toBe("false");
	});
});
