/**
 * viewer.getSkillTarget 桥方法测试（E3 描绘技能：发送时冻结的技能目标，
 * viewer-drawing-skill-contract §2.1 target 字段的宿主侧取值）。
 *
 * 加载真实 static/app.js（最小 DOM + fetch mock + HostBridgeHost stub，
 * harness 模式同 viewer-get-viewport.test.ts）与真实 static/plugin-permissions.js，
 * 经生产路径驱动：init（registerHostBridgeHandlers 注册桥处理器）→ 切片行
 * click → openSlide（state.slide 就绪）→ 直接调用捕获的桥处理器。锁定：
 *   - 权限映射：viewer.getSkillTarget 归 slide:metadata:read 档（只读几何/
 *     身份读取，无导航无写操作，与 selection.getBbox 同档）；
 *   - 无切片 → {available:false, reason:"no_slide"}（不抛错；插件据此在
 *     @ 候选显示不可用说明，不发写意图）；
 *   - 临时查看切片（只读视图）→ {available:false, reason:"readonly"}；
 *   - viewer 就绪 → kind="viewport" + level-0 bbox + slide_id/asset_revision；
 *   - viewer 未就绪 → {available:false, reason:"viewer_not_ready"}（宽容缺省，
 *     不抛错——技能目标缺失由插件降级，不发导航类错误语义）；
 *   - 优先级（选中标注 > 选框 ROI > 视野）与 annotation 元数据透传：源级断言
 *     （currentSkillTarget 单一实现；ROI/标注选中态需真实指针/面板交互驱动，
 *      既有用例同以源断言覆盖该层——见 toolbar-draw-roi.test.ts 惯例）。
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

const SLIDE = "skill-target.ndpi";
const SLIDE_ID = "sld_st0000000001";
const SLIDE_REVISION = "1700000000:4096";
const TMP_SLIDE = "e2e-temp-view.tif";
const TMP_SLIDE_ID = "sld_st0000000002";

// ---------- 最小假元素（裁自 viewer-get-viewport.test.ts harness） ----------
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
		querySelectorAll: () => [] as FakeEl[],
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
		viewport: null as unknown,
		addHandler(type: string, fn: (e?: unknown) => void) {
			(openHandlers[type] ||= []).push(fn);
		},
		open(_url: string) {
			(openHandlers["open"] || []).forEach((fn) => fn({}));
		},
		setMouseNavEnabled() {},
		close() {},
	};

	const fetchImpl = vi.fn((url: string) => {
		const u = String(url);
		if (u.includes(`/api/slides/${SLIDE_ID}/info`)) {
			return jsonResponse({
				name: SLIDE, slide_id: SLIDE_ID, display_name: "", original_filename: SLIDE,
				width: 1000, height: 800, mpp_x: 0.5, mpp_y: 0.5, mpp_source: "native",
				asset_revision: SLIDE_REVISION,
			});
		}
		if (u.includes(`/api/slides/${TMP_SLIDE_ID}/info`)) {
			return jsonResponse({
				name: TMP_SLIDE, slide_id: TMP_SLIDE_ID, display_name: "", original_filename: TMP_SLIDE,
				width: 640, height: 480, mpp_x: 0.5, mpp_y: 0.5, mpp_source: "native",
				asset_revision: "1700000100:2048",
				// 临时查看切片：真实后端把到期时间放在列表项/info（epoch 秒）
				temporary_view_expires_at: 4102444800000,
			});
		}
		if (u.includes("/api/annotations?slide_id=")) return jsonResponse({ annotations: [] });
		if (u.includes("/api/annotations")) return jsonResponse({ by_slide: {} });
		if (u.includes("/api/slides")) {
			return jsonResponse([
				{ name: SLIDE, slide_id: SLIDE_ID, display_name: "", width: 1000, height: 800, mpp_x: 0.5, mpp_source: "native" },
				{ name: TMP_SLIDE, slide_id: TMP_SLIDE_ID, display_name: "", width: 640, height: 480, mpp_x: 0.5, mpp_source: "native", temporary_view_expires_at: 4102444800 },
			]);
		}
		if (u.includes("/api/projects")) {
			return jsonResponse([{ pid: "p1", name: "P1", slides: [SLIDE, TMP_SLIDE], slide_count: 2, roi_count: 0 }]);
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
		HP_APP_BOOTSTRAP: { mode: "official", capabilities: { slide_id_api: true } },
		HistoPilot: {},
		PluginPermissions,
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

/** 按 dataset.slideId 找切片卡（.fb-hit；先逐个进入有父节点的文件夹卡）。 */
function findSlideRowById(app: BootResult, slideId: string): FakeEl {
	const pick = () => app.created.filter((e) => e.classList.contains("fb-hit") && e.parentNode && e.dataset.slideId === slideId).pop();
	let row = pick();
	if (!row) {
		// 临时查看切片归入虚拟 __temp__ 文件夹，普通切片在项目文件夹——逐个进入。
		const folders = app.created.filter((e) => e.classList.contains("fb-folder") && e.parentNode);
		for (const folder of folders) {
			if (row) break;
			folder.dispatch("click");
			row = pick();
		}
	}
	if (!row) throw new Error(`harness: 未找到 slideId=${slideId} 的切片卡`);
	return row;
}

/** 与 manifest.schema.json 一致的最小权限声明。 */
const FULL_PERMS = ["slide:metadata:read", "annotation:read", "annotation:write", "viewer:navigate"];

afterEach(() => {
	vi.restoreAllMocks();
	delete (globalThis as { window?: unknown }).window;
	delete (globalThis as { document?: unknown }).document;
	delete (globalThis as { fetch?: unknown }).fetch;
	delete (globalThis as { HP_ViewerCore?: unknown }).HP_ViewerCore;
});

describe("viewer.getSkillTarget 桥方法（真实 app.js）", () => {
	it("权限映射：viewer.getSkillTarget 归 slide:metadata:read 档（只读几何/身份，与 selection.getBbox 同）", () => {
		expect(PluginPermissions.METHOD_PERMISSIONS["viewer.getSkillTarget"]).toBe("slide:metadata:read");
	});

	it("无切片 → {available:false, reason:'no_slide'}（不抛错；不发写意图）", async () => {
		const app = bootApp(FULL_PERMS);
		await settle();
		const handler = app.bridgeHandlers["viewer.getSkillTarget"];
		expect(handler).toBeTruthy();
		expect(handler({}, { pluginInstallationId: "histopilot" })).toEqual({ available: false, reason: "no_slide" });
	});

	it("临时查看切片（只读视图）→ {available:false, reason:'readonly'}", async () => {
		const app = bootApp(FULL_PERMS);
		await settle();
		findSlideRowById(app, TMP_SLIDE_ID).dispatch("click");
		await settle();
		expect(app.emitted.some((e) => e.type === "slide.opened")).toBe(true);
		expect(app.bridgeHandlers["viewer.getSkillTarget"]({}, { pluginInstallationId: "histopilot" }))
			.toEqual({ available: false, reason: "readonly" });
	});

	it("viewer 就绪 → kind=viewport + level-0 bbox + slide_id/asset_revision", async () => {
		const app = bootApp(FULL_PERMS);
		await settle();
		findSlideRowById(app, SLIDE_ID).dispatch("click");
		await settle();
		// viewer 就绪：视野 (10,20,400,300) viewport 坐标 → 图像像素 (100,160,400,480)
		// → 钳到切片边界 [0,0,1000,800] → 取整。
		app.fakeViewer.viewport = makeMockViewport(
			{ x: 0.01, y: 0.02, width: 0.4, height: 0.3 },
			{ x: 100, y: 160, width: 400, height: 480 },
		);
		expect(app.bridgeHandlers["viewer.getSkillTarget"]({}, { pluginInstallationId: "histopilot" })).toEqual({
			available: true,
			kind: "viewport",
			bbox: { x: 100, y: 160, w: 400, h: 480 },
			slide_id: SLIDE_ID,
			asset_revision: SLIDE_REVISION,
			annotation_id: null,
			annotation_revision: null,
		});
	});

	it("viewer 未就绪 → {available:false, reason:'viewer_not_ready'}（宽容缺省，不抛错）", async () => {
		const app = bootApp(FULL_PERMS);
		await settle();
		findSlideRowById(app, SLIDE_ID).dispatch("click");
		await settle();
		expect(app.fakeViewer.viewport).toBeNull();
		expect(app.bridgeHandlers["viewer.getSkillTarget"]({}, { pluginInstallationId: "histopilot" }))
			.toEqual({ available: false, reason: "viewer_not_ready" });
	});

	it("优先级与 annotation 元数据：currentSkillTarget 单一实现按「选中标注 > 选框 ROI > 视野」解析", () => {
		// ROI/标注选中态需真实指针/面板交互驱动（既有惯例以源断言覆盖该层，
		// 见 toolbar-draw-roi.test.ts）。锁定优先级顺序与 annotation 必填字段：
		const fnStart = appSrc.indexOf("function currentSkillTarget()");
		expect(fnStart).toBeGreaterThan(-1);
		const fnSrc = appSrc.slice(fnStart, appSrc.indexOf("\n  }\n", fnStart));
		const annoIdx = fnSrc.indexOf('kind: "annotation"');
		const roiIdx = fnSrc.indexOf('kind: "roi"');
		const vpIdx = fnSrc.indexOf('kind: "viewport"');
		expect(annoIdx).toBeGreaterThan(-1);
		expect(roiIdx).toBeGreaterThan(annoIdx);
		expect(vpIdx).toBeGreaterThan(roiIdx);
		// annotation 目标必须带稳定 id 与 revision（引用不授予编辑权限，结果恒新建 pending）。
		expect(fnSrc).toContain("annotation_id: String(editItem.annotation_id)");
		expect(fnSrc).toContain("annotation_revision:");
		// 无切片/临时查看的 fail-closed 语义在前。
		expect(fnSrc.indexOf('"no_slide"')).toBeLessThan(annoIdx);
		expect(fnSrc.indexOf('"readonly"')).toBeLessThan(annoIdx);
	});

	it("桥注册：viewer.getSkillTarget 在 HP bridge 请求处理处（gate 包裹）", async () => {
		const app = bootApp(FULL_PERMS);
		await settle();
		expect(typeof app.bridgeHandlers["viewer.getSkillTarget"]).toBe("function");
		// 未声明 slide:metadata:read 的插件身份 → 权限门拒绝（fail-closed）。
		let err: { code?: string } | null = null;
		try {
			app.bridgeHandlers["viewer.getSkillTarget"]({}, { pluginInstallationId: "sample-annotator" });
		} catch (e) { err = e as { code?: string }; }
		expect(err && err.code).toBe("permission_denied");
	});
});
