/**
 * AI 面板外框：拖动 / 调整大小（改版第四轮 §1.4）。
 *
 * 纯几何 clampAIPanelBox 与偏好存取（pt.aip.v1|<站点:账号>）直接驱动；
 * createAIPanelController 以假元素注入（与 sidebar-layout.test.ts 同风格），
 * 锁定：
 *   - 钳制：出框收进框内；宽高夹在 [min, frame]；框小于最小值时面板保持
 *     最小尺寸、位置贴 0（绝不出框）；
 *   - 偏好键含身份维度；损坏/缺失回落 null；读写容错（storage 抛错不炸）；
 *   - 拖动：标题栏 pointerdown → pointermove 钳制生效 → pointerup 持久化；
 *     重开按偏好还原；双击标题栏清除偏好并恢复默认（清内联几何）；
 *   - 调整大小：小于 280×240 夹到最小值；
 *   - 窗口缩放：re-clamp 回框内并更新持久化；
 *   - ≤768px：不启用（不读偏好、不绑定拖动）；
 *   - 标题栏按钮（closest 命中 button）不触发拖动。
 */
import { afterEach, describe, expect, it } from "vitest";
import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const appSrc = readFileSync(resolve(here, "../../static/app.js"), "utf8");

interface Box { left: number; top: number; width: number; height: number }

interface PanelModule {
	createAIPanelController(deps: Record<string, unknown>): {
		init(): void;
		onBreakpointChange(): void;
		isMobile(): boolean;
		currentBox(): Box | null;
	};
	aiPanelPrefKey(scope: string): string;
	parseAIPanelPref(raw: string | null): Box | null;
	clampAIPanelBox(box: Box, frame: { width: number; height: number }, min: { width: number; height: number }): Box;
}

interface FakeEl extends Record<string, unknown> {
	style: Record<string, string>;
	classList: { add(...n: string[]): void; remove(...n: string[]): void; contains(n: string): boolean };
	getBoundingClientRect(): { width: number; height: number; left: number; top: number };
	addEventListener(type: string, cb: (e?: unknown) => void): void;
	dispatch(type: string, evt?: unknown): void;
	offsetWidth: number;
	offsetHeight: number;
}

function fakeEl(opts: { rect?: { width: number; height: number }; w?: number; h?: number } = {}): FakeEl {
	const classes = new Set<string>();
	const listeners: Record<string, Array<(e?: unknown) => void>> = {};
	return {
		style: {},
		offsetWidth: opts.w ?? 340,
		offsetHeight: opts.h ?? 500,
		classList: {
			add: (...n) => n.forEach((x) => classes.add(x)),
			remove: (...n) => n.forEach((x) => classes.delete(x)),
			contains: (n) => classes.has(n),
		},
		getBoundingClientRect: () => ({ left: 0, top: 0, width: opts.rect?.width ?? 1000, height: opts.rect?.height ?? 800 }),
		addEventListener: (type, cb) => void (listeners[type] ||= []).push(cb),
		dispatch: (type, evt) => (listeners[type] || []).forEach((cb) => cb(evt)),
	};
}

function fakeStorage(initial: Record<string, string> = {}) {
	const map = new Map(Object.entries(initial));
	return {
		getItem: (k: string) => (map.has(k) ? (map.get(k) as string) : null),
		setItem: (k: string, v: string) => void map.set(k, v),
		removeItem: (k: string) => void map.delete(k),
		_map: map,
	};
}

interface CtrlDeps {
	panel: FakeEl;
	header: FakeEl;
	handle: FakeEl;
	frame: FakeEl;
	mq: { matches: boolean };
	storage: ReturnType<typeof fakeStorage> | null;
	scope: string;
	win: { innerWidth: number; innerHeight: number; addEventListener(t: string, cb: (e?: unknown) => void): void };
	doc: { addEventListener(t: string, cb: (e?: unknown) => void): void };
}

function bootModule(): PanelModule {
	const w: Record<string, unknown> = {
		HP_I18N: { t: (k: string) => k, getLang: () => "zh" },
		HP_ViewerCore: { create: () => ({ container: { style: {}, getBoundingClientRect: () => ({ width: 800, height: 600, left: 0, top: 0 }), insertBefore() {} }, canvas: {}, viewport: null, addHandler() {}, open() {}, close() {}, forceResize() {} }) },
		HP_API: {},
		HP_APP_BOOTSTRAP: { mode: "official", capabilities: {} },
		matchMedia: () => ({ matches: false, addEventListener() {}, addListener() {} }),
		fetch: () => Promise.resolve({ ok: true, status: 200, clone() { return this; }, json: () => Promise.resolve([]) }),
		location: { href: "http://local/", pathname: "/" },
		addEventListener() {},
		localStorage: null,
	};
	const doc = {
		readyState: "loading",
		cookie: "",
		getElementById: () => fakeEl(),
		createElement: () => fakeEl(),
		addEventListener() {},
		querySelector: () => null,
		querySelectorAll: () => [],
		body: fakeEl(),
	};
	(globalThis as { document?: unknown }).document = doc;
	(globalThis as { window?: unknown }).window = w;
	(globalThis as { fetch?: unknown }).fetch = w.fetch;
	(globalThis as { HP_ViewerCore?: unknown }).HP_ViewerCore = w.HP_ViewerCore;
	(globalThis as { HP_I18N?: unknown }).HP_I18N = w.HP_I18N;
	// eslint-disable-next-line @typescript-eslint/no-explicit-any, no-new-func
	new Function("window", "document", "fetch", "location", appSrc)(w, doc, w.fetch, w.location);
	// init() 由 DOMContentLoaded 触发（未派发）：只取模块导出，不启动整页装配
	const HP = (w as { HP_AI_PANEL?: PanelModule }).HP_AI_PANEL;
	if (!HP) throw new Error("HP_AI_PANEL not exported");
	return HP;
}

function makeDeps(overrides: Partial<CtrlDeps> = {}): CtrlDeps {
	return {
		panel: fakeEl(),
		header: fakeEl(),
		handle: fakeEl(),
		frame: fakeEl({ rect: { width: 1000, height: 800 } }),
		mq: { matches: false },
		storage: fakeStorage(),
		scope: "official:u1",
		win: { innerWidth: 1200, innerHeight: 900, addEventListener() {} },
		doc: { addEventListener() {} },
		...overrides,
	};
}

function pointerEvt(o: { pointerId?: number; x?: number; y?: number; target?: unknown; currentTarget?: unknown }) {
	return {
		pointerId: o.pointerId ?? 1,
		clientX: o.x ?? 0,
		clientY: o.y ?? 0,
		button: 0,
		target: o.target ?? { closest: () => null },
		currentTarget: o.currentTarget ?? null,
		cancelable: false,
		preventDefault() {},
	};
}

afterEach(() => {
	delete (globalThis as { window?: unknown }).window;
	delete (globalThis as { document?: unknown }).document;
	delete (globalThis as { fetch?: unknown }).fetch;
	delete (globalThis as { HP_ViewerCore?: unknown }).HP_ViewerCore;
	delete (globalThis as { HP_I18N?: unknown }).HP_I18N;
});

describe("clampAIPanelBox（纯几何）", () => {
	it("框内保持不变；出框收进框内", () => {
		const HP = bootModule();
		const frame = { width: 1000, height: 800 };
		const min = { width: 280, height: 240 };
		const inside = HP.clampAIPanelBox({ left: 100, top: 50, width: 340, height: 500 }, frame, min);
		expect(inside).toEqual({ left: 100, top: 50, width: 340, height: 500 });
		const out = HP.clampAIPanelBox({ left: 900, top: 700, width: 340, height: 500 }, frame, min);
		expect(out.left).toBe(1000 - 340);
		expect(out.top).toBe(800 - 500);
	});

	it("宽高夹在 [min, frame]；负位置夹 0", () => {
		const HP = bootModule();
		const frame = { width: 1000, height: 800 };
		const min = { width: 280, height: 240 };
		expect(HP.clampAIPanelBox({ left: -50, top: -10, width: 200, height: 100 }, frame, min))
			.toEqual({ left: 0, top: 0, width: 280, height: 240 });
		expect(HP.clampAIPanelBox({ left: 0, top: 0, width: 5000, height: 3000 }, frame, min))
			.toEqual({ left: 0, top: 0, width: 1000, height: 800 });
	});

	it("框小于最小值：尺寸缩进视框、位置贴 0", () => {
		const HP = bootModule();
		const out = HP.clampAIPanelBox(
			{ left: 40, top: 40, width: 340, height: 500 },
			{ width: 200, height: 100 },
			{ width: 280, height: 240 });
		expect(out).toEqual({ left: 0, top: 0, width: 200, height: 100 });
	});
});

describe("偏好存取（pt.aip.v1|）", () => {
	it("键含身份维度；合法结构解析、损坏/缺失返回 null", () => {
		const HP = bootModule();
		expect(HP.aiPanelPrefKey("official:u1")).toBe("pt.aip.v1|official:u1");
		expect(HP.parseAIPanelPref(JSON.stringify({ left: 1, top: 2, width: 300, height: 260 })))
			.toEqual({ left: 1, top: 2, width: 300, height: 260 });
		expect(HP.parseAIPanelPref("not json")).toBeNull();
		expect(HP.parseAIPanelPref(JSON.stringify({ left: "x", top: 2, width: 300, height: 260 }))).toBeNull();
		expect(HP.parseAIPanelPref(null)).toBeNull();
	});
});

describe("createAIPanelController（假元素驱动）", () => {
	it("拖动：钳制生效、pointerup 持久化、重开还原、双击恢复默认", () => {
		const HP = bootModule();
		const docListeners: Record<string, Array<(e?: unknown) => void>> = {};
		const storage = fakeStorage();
		const deps = makeDeps({
			storage,
			doc: { addEventListener: (t, cb) => void (docListeners[t] ||= []).push(cb) },
			win: {
				innerWidth: 1200, innerHeight: 900,
				addEventListener: () => {},
			},
		});
		const ctrl = HP.createAIPanelController(deps as unknown as Record<string, unknown>);
		ctrl.init();
		expect(deps.panel.classList.contains("ai-drag-ok")).toBe(true);

		// 默认锚定态起手：面板 340×500，视框 1000×800 → start ≈ (646,14)
		deps.header.dispatch("pointerdown", pointerEvt({ x: 646, y: 14, currentTarget: deps.header }));
		// 大幅左上拖（越框）：位置被钳到 (0,0)
		(docListeners.pointermove || []).forEach((cb) => cb(pointerEvt({ x: 0, y: -1000 })));
		expect(deps.panel.style.left).toBe("0px");
		expect(deps.panel.style.top).toBe("0px");
		expect(deps.panel.style.right).toBe("auto");
		expect(deps.panel.style.width).toBe("340px");
		(docListeners.pointerup || []).forEach((cb) => cb(pointerEvt({ x: 0, y: -1000 })));
		const saved = JSON.parse(storage._map.get("pt.aip.v1|official:u1") || "{}");
		expect(saved).toMatchObject({ left: 0, top: 0, width: 340, height: 500 });

		// 重开（新控制器同存储）：偏好还原
		const deps2 = makeDeps({ storage });
		const ctrl2 = HP.createAIPanelController(deps2 as unknown as Record<string, unknown>);
		ctrl2.init();
		expect(deps2.panel.style.left).toBe("0px");
		expect(deps2.panel.style.top).toBe("0px");

		// 双击标题栏空白：恢复默认（清内联几何）+ 清除偏好
		deps2.header.dispatch("dblclick", { target: { closest: () => null } });
		expect(deps2.panel.style.left).toBe("");
		expect(deps2.panel.style.top).toBe("");
		expect(deps2.panel.style.right).toBe("");
		expect(storage._map.has("pt.aip.v1|official:u1")).toBe(false);
	});

	it("调整大小：小于最小值夹到 280×240；重开与窗口缩放 re-clamp 回框内", () => {
		const HP = bootModule();
		const docListeners: Record<string, Array<(e?: unknown) => void>> = {};
		const winListeners: Record<string, Array<(e?: unknown) => void>> = {};
		const storage = fakeStorage();
		const deps = makeDeps({
			storage,
			doc: { addEventListener: (t, cb) => void (docListeners[t] ||= []).push(cb) },
			win: {
				innerWidth: 1200, innerHeight: 900,
				addEventListener: (t, cb) => void (winListeners[t] ||= []).push(cb),
			},
		});
		const ctrl = HP.createAIPanelController(deps as unknown as Record<string, unknown>);
		ctrl.init();

		// 预置偏好 (500,100,340,500)；手柄向左上拖 400px → 280×240（最小值钳制）
		storage._map.set("pt.aip.v1|official:u1", JSON.stringify({ left: 500, top: 100, width: 340, height: 500 }));
		ctrl.onBreakpointChange();
		expect(deps.panel.style.left).toBe("500px");
		deps.handle.dispatch("pointerdown", pointerEvt({ x: 840, y: 600, currentTarget: deps.handle }));
		(docListeners.pointermove || []).forEach((cb) => cb(pointerEvt({ x: 440, y: 200 })));
		expect(deps.panel.style.width).toBe("280px");
		expect(deps.panel.style.height).toBe("240px");
		(docListeners.pointerup || []).forEach((cb) => cb(pointerEvt({ x: 440, y: 200 })));
		expect(storage._map.get("pt.aip.v1|official:u1")).toContain('"width":280');

		// 窗口缩放：视框变小 → re-clamp 收进框内（最小高度让位于视框，位置贴 0）
		const frameSmall = fakeEl({ rect: { width: 300, height: 200 } });
		const deps3 = makeDeps({
			storage,
			frame: frameSmall,
			doc: { addEventListener: (t, cb) => void (docListeners[t] ||= []).push(cb) },
			win: {
				innerWidth: 1200, innerHeight: 900,
				addEventListener: (t, cb) => void (winListeners[t] ||= []).push(cb),
			},
		});
		const ctrl3 = HP.createAIPanelController(deps3 as unknown as Record<string, unknown>);
		ctrl3.init();
		// 存的是 (500,100,280,240) — 视框 300×200 → 高度缩至 200，位置收进框内
		// （left ≤ 300-280=20，top ≤ max(0, 200-240)=0）
		expect(deps3.panel.style.left).toBe("20px");
		expect(deps3.panel.style.top).toBe("0px");
		expect(deps3.panel.style.width).toBe("280px");
		expect(deps3.panel.style.height).toBe("200px");
		// resize 事件：同样 re-clamp（钳制是幂等的）
		(winListeners.resize || []).forEach((cb) => cb({}));
		expect(deps3.panel.style.left).toBe("20px");
	});

	it("≤768px 不启用：不读偏好、不应用内联几何；标题栏按钮不触发拖动", () => {
		const HP = bootModule();
		const storage = fakeStorage();
		storage._map.set("pt.aip.v1|official:u1", JSON.stringify({ left: 5, top: 5, width: 300, height: 260 }));
		const deps = makeDeps({ storage, mq: { matches: true } });
		const ctrl = HP.createAIPanelController(deps as unknown as Record<string, unknown>);
		ctrl.init();
		expect(ctrl.isMobile()).toBe(true);
		expect(deps.panel.style.left).toBeUndefined(); // 未应用
		// 断点回桌面：重读偏好并应用
		deps.mq.matches = false;
		ctrl.onBreakpointChange();
		expect(deps.panel.style.left).toBe("5px");
	});

	it("mobile-first load installs handlers for a later desktop breakpoint", () => {
		const HP = bootModule();
		const listeners: Record<string, Array<(e?: unknown) => void>> = {};
		const deps = makeDeps({ mq: { matches: true }, doc: { addEventListener: (t, cb) => void (listeners[t] ||= []).push(cb) } });
		const ctrl = HP.createAIPanelController(deps as unknown as Record<string, unknown>);
		ctrl.init(); deps.mq.matches = false; ctrl.onBreakpointChange();
		deps.header.dispatch("pointerdown", pointerEvt({ x: 800, y: 30, currentTarget: deps.header }));
		(listeners.pointermove || []).forEach(cb => cb(pointerEvt({ x: 700, y: 80 })));
		expect(ctrl.currentBox()).not.toBeNull();
	});

	it("short viewports take priority over preferred minimum size", () => {
		const HP = bootModule();
		const result = HP.clampAIPanelBox({ left: 50, top: 40, width: 340, height: 500 }, { width: 230, height: 180 }, { width: 280, height: 240 });
		expect(result).toEqual({ left: 0, top: 0, width: 230, height: 180 });
	});

	it("标题栏按钮（closest 命中 button）pointerdown 不拖动", () => {
		const HP = bootModule();
		const docListeners: Record<string, Array<(e?: unknown) => void>> = {};
		const deps = makeDeps({
			doc: { addEventListener: (t, cb) => void (docListeners[t] ||= []).push(cb) },
		});
		const ctrl = HP.createAIPanelController(deps as unknown as Record<string, unknown>);
		ctrl.init();
		deps.header.dispatch("pointerdown", pointerEvt({
			x: 10, y: 10,
			target: { closest: (sel: string) => (String(sel).includes("button") ? {} : null) },
			currentTarget: deps.header,
		}));
		(docListeners.pointermove || []).forEach((cb) => cb(pointerEvt({ x: -200, y: -200 })));
		expect(deps.panel.style.left).toBeUndefined(); // 未进入拖动
		expect(ctrl.currentBox()).toBeNull();
	});
});
