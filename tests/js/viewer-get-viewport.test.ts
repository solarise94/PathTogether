/**
 * viewer.getViewport 桥方法测试（P1「普通发送绑定浏览器当前视野」）。
 *
 * 加载真实 static/app.js（最小 DOM + fetch mock + HostBridgeHost stub，
 * harness 模式同 slide-opened-revision.test.ts）与真实 static/plugin-permissions.js，
 * 经生产路径驱动：init（registerHostBridgeHandlers 注册桥处理器）→ 切片行
 * click → openSlide（state.slide 就绪）→ 直接调用捕获的桥处理器。锁定：
 *   - 无切片 → null（宽容缺省，不抛错）；
 *   - viewer 未就绪 → 真实 error code viewer_not_ready（retryable，R1 惯例）；
 *   - viewer 就绪 → 返回 level-0 像素 bbox（viewport 坐标 → 图像像素 →
 *     钳到切片边界 → 取整）；
 *   - 越界视野钳到切片边界 [0,0,width,height]；
 *   - 权限门：未声明 viewer:navigate 的插件身份 → permission_denied；
 *     声明后放行（viewer.getViewport 与 viewer.navigate 同权限档）。
 */
import { afterEach, describe, expect, it, vi } from "vitest";
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const appSrc = readFileSync(resolve(here, "../../static/app.js"), "utf8");
const require = createRequire(import.meta.url);
const PluginPermissions = require(resolve(here, "../../static/plugin-permissions.js")) as {
	METHOD_PERMISSIONS: Record<string, string>;
	gatePermission: (pluginId: string, method: string, table?: Record<string, string[]>) => null | { code: string };
};

const SLIDE = "vp-slide.ndpi";

// ---------- 最小假元素（裁自 slide-opened-revision.test.ts harness） ----------
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
		getAttribute: (k) => (attrs.has(k) ? attrs.get(k) as string : null),
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
			clearRect() {}, save() {}, restore() {}, setTransform() {}, setLineDash() {},
			strokeRect() {}, fillRect() {}, fillText() {},
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

interface ImageRect { x: number; y: number; width: number; height: number }

/** OpenSeadragon viewport 桩：可编程的当前视野（viewport 坐标 / 图像像素）。 */
function makeMockViewport(viewportRect: ImageRect, imageRect: ImageRect) {
	return {
		getBounds: (_immediate?: boolean) => ({ ...viewportRect }),
		viewportToImageRectangle: (_b: unknown) => ({ ...imageRect }),
		imageToViewportRectangle: (x: number, y: number, w: number, h: number) => ({ x, y, width: w, height: h }),
		fitBounds() {},
	};
}

interface BootResult {
	emitted: Array<{ type: string; payload: Record<string, unknown> }>;
	bridgeHandlers: Record<string, (payload: unknown, env?: { pluginInstallationId?: string }) => unknown>;
	created: FakeEl[];
	fakeViewer: { viewport: unknown; open: (url: string) => void };
}

function bootApp(declaredPerms: string[]): BootResult {
	const els: Record<string, FakeEl> = {};
	const created: FakeEl[] = [];
	const docListeners: Record<string, Array<() => void>> = {};
	const rafCbs: Array<() => void> = [];
	const emitted: Array<{ type: string; payload: Record<string, unknown> }> = [];
	const bridgeHandlers: Record<string, (payload: unknown, env?: { pluginInstallationId?: string }) => unknown> = {};
	const openHandlers: Record<string, Array<(e?: unknown) => void>> = {};

	const fakeViewer = {
		container: {
			style: {} as Record<string, string>,
			getBoundingClientRect: () => ({ width: 800, height: 600, left: 0, top: 0 }),
			insertBefore() {},
		},
		canvas: {},
		viewport: null as unknown, // 就绪与否由用例在 open 之后设置
		addHandler(type: string, fn: (e?: unknown) => void) {
			(openHandlers[type] ||= []).push(fn);
		},
		// 生产里 viewer.open 在 tile source 就绪后异步触发 "open" 事件；
		// harness 同步触发（onViewerOpen → slide.opened，state.slide 已就绪）。
		open(_url: string) {
			(openHandlers["open"] || []).forEach((fn) => fn({}));
		},
		setMouseNavEnabled() {},
	};

	const fetchImpl = vi.fn((url: string) => {
		const u = String(url);
		if (u.includes("/api/slide/" + SLIDE + "/info")) {
			return jsonResponse({ name: SLIDE, width: 1000, height: 800, mpp_x: 0.5, mpp_y: 0.5, mpp_source: "native" });
		}
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

	const w: Record<string, unknown> = {
		HP_I18N: { t: (k: string) => k, getLang: () => "zh" },
		HP_ViewerCore: { create: () => fakeViewer },
		HP_API: {},
		HistoPilot: {},
		PluginPermissions,
		// 宿主侧权限表：histopilot 声明的 manifest permissions（用例可编程）。
		SVS_PLUGIN_PERMISSIONS: { histopilot: declaredPerms },
		HostBridgeHost: {
			onRequest(method: string, fn: (payload: unknown, env?: { pluginInstallationId?: string }) => unknown) {
				bridgeHandlers[method] = fn;
			},
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
	(globalThis as { HP_ViewerCore?: unknown }).HP_ViewerCore = { create: () => fakeViewer };

	new Function("window", "document", "fetch", "location", appSrc)(w, doc, fetchImpl, (w as { location: unknown }).location);
	(docListeners["DOMContentLoaded"] || []).forEach((cb) => cb());
	while (rafCbs.length) (rafCbs.shift() as () => void)();

	return { emitted, bridgeHandlers, created, fakeViewer };
}

async function settle(times = 20) {
	for (let i = 0; i < times; i++) await Promise.resolve();
}

function findSlideRow(app: BootResult): FakeEl {
	const row = app.created.find((e) => e.classList.contains("slide-row"));
	if (!row) throw new Error("harness: 未渲染出 .slide-row");
	return row;
}

/** 与 manifest.schema.json 一致的最小权限声明（viewer:navigate 档）。 */
const FULL_PERMS = ["slide:metadata:read", "annotation:read", "annotation:write", "viewer:navigate"];

afterEach(() => {
	vi.restoreAllMocks();
	delete (globalThis as { window?: unknown }).window;
	delete (globalThis as { document?: unknown }).document;
	delete (globalThis as { fetch?: unknown }).fetch;
	delete (globalThis as { HP_ViewerCore?: unknown }).HP_ViewerCore;
});

describe("viewer.getViewport 桥方法（真实 app.js）", () => {
	it("权限映射：viewer.getViewport 归 viewer:navigate 档（与 viewer.navigate 同）", () => {
		expect(PluginPermissions.METHOD_PERMISSIONS["viewer.getViewport"]).toBe("viewer:navigate");
	});

	it("无切片 → 返回 null（宽容缺省）", async () => {
		const app = bootApp(FULL_PERMS);
		await settle();
		const handler = app.bridgeHandlers["viewer.getViewport"];
		expect(handler).toBeTruthy();
		expect(handler({}, { pluginInstallationId: "histopilot" })).toBeNull();
	});

	it("viewer 未就绪 → 抛 viewer_not_ready（retryable:true，R1 惯例）", async () => {
		const app = bootApp(FULL_PERMS);
		await settle();
		findSlideRow(app).dispatch("click");
		await settle();
		expect(app.emitted.some((e) => e.type === "slide.opened")).toBe(true);
		expect(app.fakeViewer.viewport).toBeNull();
		let err: { code?: string; retryable?: boolean } | null = null;
		try {
			app.bridgeHandlers["viewer.getViewport"]({}, { pluginInstallationId: "histopilot" });
		} catch (e) {
			err = e as { code?: string; retryable?: boolean };
		}
		expect(err && err.code).toBe("viewer_not_ready");
		expect(err && err.retryable).toBe(true);
	});

	it("viewer 就绪 → 返回 level-0 像素 bbox（转换 + 取整）", async () => {
		const app = bootApp(FULL_PERMS);
		await settle();
		findSlideRow(app).dispatch("click");
		await settle();
		// viewport 坐标 (0.0106, 0.0255, 0.5005, 0.50025) → 图像像素
		// (10.6, 20.4, 500.5, 400.2)（切片 1000×800）。钳界不触发。
		app.fakeViewer.viewport = makeMockViewport(
			{ x: 0.0106, y: 0.0255, width: 0.5005, height: 0.50025 },
			{ x: 10.6, y: 20.4, width: 500.5, height: 400.2 },
		);
		const bbox = app.bridgeHandlers["viewer.getViewport"]({}, { pluginInstallationId: "histopilot" }) as Record<string, number>;
		// x0=10.6→11；x1=511.1→511；w=511-11=500；y0=20.4→20；y1=420.6→421；h=401。
		expect(bbox).toEqual({ x: 11, y: 20, w: 500, h: 401 });
	});

	it("越界视野钳到切片边界 [0,0,width,height]", async () => {
		const app = bootApp(FULL_PERMS);
		await settle();
		findSlideRow(app).dispatch("click");
		await settle();
		app.fakeViewer.viewport = makeMockViewport(
			{ x: -0.05, y: -0.05, width: 2, height: 2 },
			{ x: -50, y: -40, width: 2000, height: 1600 },
		);
		const bbox = app.bridgeHandlers["viewer.getViewport"]({}, { pluginInstallationId: "histopilot" }) as Record<string, number>;
		expect(bbox).toEqual({ x: 0, y: 0, w: 1000, h: 800 });
	});

	it("权限门：未声明 viewer:navigate → permission_denied；声明后放行", async () => {
		const app = bootApp(["slide:metadata:read"]); // 缺 viewer:navigate
		await settle();
		findSlideRow(app).dispatch("click");
		await settle();
		app.fakeViewer.viewport = makeMockViewport(
			{ x: 0, y: 0, width: 1, height: 1 },
			{ x: 0, y: 0, width: 1000, height: 800 },
		);
		let err: { code?: string } | null = null;
		try {
			app.bridgeHandlers["viewer.getViewport"]({}, { pluginInstallationId: "histopilot" });
		} catch (e) {
			err = e as { code?: string };
		}
		expect(err && err.code).toBe("permission_denied");
	});
});
