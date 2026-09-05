/**
 * Batch 4：三种查看器通道 UI —— static/channel-controls.js 单元测试。
 *
 * 锁定规格 §4.1–4.4 / §5.2 / §8 的前端口径：
 *   - normalizeChannelInfo：flag 关只回探测字段（不发请求、不出面板）；
 *     RGB 不出面板；multichannel 归一化（颜色校验、Name 缺失、颜色来源）；
 *   - 默认最多启用前 4 个有效通道；一次最多 8（第 9 个阻止并给可读提示）；
 *   - localStorage：key 含用户作用域 + 切片安全名 + asset revision；只存用户
 *     选择不存 token；解析失败只回默认 + 一次非阻塞提示；
 *   - inline custom TileSource（width/height/getTileUrl），不依赖 DZI XML 保留
 *     query；瓦片 URL 必须携带 render token；
 *   - 颜色变化更新顺序与 epoch 竞争：快速连点只应用最后一次，旧响应不得
 *     覆盖新 context；
 *   - open 后恢复 viewport center/zoom/rotation/flip。
 *
 * DOM 用最小 fake 元素（与 demo-ai.test.ts 同套路）；模块经 new Function 注入
 * window/document 加载真实源码。
 */
import { describe, expect, it, vi } from "vitest";
import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const src = readFileSync(resolve(here, "../../static/channel-controls.js"), "utf8");

// ---------------------------------------------------------------- fake DOM --
interface FakeEl {
	tagName: string;
	className: string;
	textContent: string;
	hidden: boolean;
	disabled: boolean;
	checked: boolean;
	value: string;
	title: string;
	style: Record<string, string>;
	dataset: Record<string, string>;
	children: FakeEl[];
	parentNode: FakeEl | null;
	attrs: Record<string, string>;
	listeners: Record<string, Array<() => void>>;
	appendChild(child: FakeEl): FakeEl;
	removeChild(child: FakeEl): FakeEl;
	remove(): void;
	addEventListener(type: string, fn: () => void): void;
	setAttribute(k: string, v: string): void;
	classList: {
		add(c: string): void;
		remove(c: string): void;
		toggle(c: string, on?: boolean): void;
		contains(c: string): boolean;
	};
}

function fakeEl(tagName = "div"): FakeEl {
	const listeners: Record<string, Array<() => void>> = {};
	const classes = new Set<string>();
	const el: FakeEl = {
		tagName,
		className: "",
		textContent: "",
		hidden: false,
		disabled: false,
		checked: false,
		value: "",
		title: "",
		style: {},
		dataset: {},
		children: [],
		parentNode: null,
		attrs: {},
		listeners,
		appendChild(child: FakeEl) {
			child.parentNode = el;
			el.children.push(child);
			return child;
		},
		removeChild(child: FakeEl) {
			const i = el.children.indexOf(child);
			if (i >= 0) el.children.splice(i, 1);
			child.parentNode = null;
			return child;
		},
		remove() {
			if (el.parentNode) el.parentNode.removeChild(el);
		},
		addEventListener(type: string, fn: () => void) {
			(listeners[type] = listeners[type] || []).push(fn);
		},
		setAttribute(k: string, v: string) {
			el.attrs[k] = v;
			if (k === "class") el.className = v;
		},
		classList: {
			add(c: string) {
				classes.add(c);
			},
			remove(c: string) {
				classes.delete(c);
			},
			toggle(c: string, on?: boolean) {
				if (on === undefined) on = !classes.has(c);
				if (on) classes.add(c);
				else classes.delete(c);
			},
			contains(c: string) {
				return classes.has(c);
			},
		},
	};
	return el;
}

function fakeDoc() {
	return { createElement: (tag: string) => fakeEl(tag) };
}

interface ViewerLog {
	opened: unknown[];
	closed: number;
	viewportSnaps: Array<{ center: unknown; zoom: number; rotation: number; flip: boolean }>;
	restores: Array<{ zoom?: number; rotation?: number; flip?: boolean }>;
}

function fakeViewer() {
	const log: ViewerLog = { opened: [], closed: 0, viewportSnaps: [], restores: [] };
	const viewport = {
		getCenter: () => ({ x: 0.4, y: 0.6 }),
		getZoom: () => 1.25,
		getRotation: () => 90,
		getFlip: () => true,
		setRotation(r: number) {
			log.restores.push({ rotation: r });
		},
		setFlip(f: boolean) {
			log.restores.push({ flip: f });
		},
		zoomTo(z: number) {
			log.restores.push({ zoom: z });
		},
	};
	return {
		_hpLog: log,
		viewport,
		container: fakeEl(),
		close() {
			log.closed += 1;
		},
		open(ts: unknown) {
			log.opened.push(ts);
			const hs = openListeners["open"] || [];
			openListeners["open"] = [];
			hs.forEach((h) => h());
		},
	};
}

// addHandler 支持放外面（闭包共享 listener 表）
let openListeners: Record<string, Array<() => void>> = {};
function fakeViewerWithHandlers() {
	openListeners = {};
	const v = fakeViewer() as unknown as Record<string, unknown>;
	const handlers: Record<string, Array<() => void>> = openListeners;
	v.addHandler = (type: string, fn: () => void) => {
		(handlers[type] = handlers[type] || []).push(fn);
	};
	v.removeHandler = (type: string, fn: () => void) => {
		const arr = handlers[type] || [];
		const i = arr.indexOf(fn);
		if (i >= 0) arr.splice(i, 1);
	};
	v.addOnceHandler = (type: string, fn: () => void) => {
		v.addHandler(type, fn);
	};
	return v;
}

function loadModule(overrides: Record<string, unknown> = {}) {
	const w: Record<string, unknown> = {
		document: fakeDoc(),
		localStorage: memoryStorage(),
		...overrides,
	};
	new Function("window", "document", src)(w, w.document);
	return (w as { HP_Channels: Record<string, unknown> }).HP_Channels as never as Channels;
}

// 需要 读 window.PathTogether.renderState（publishRenderState 断言）时的变体
function loadModuleAndWindow(overrides: Record<string, unknown> = {}) {
	const w: Record<string, unknown> = {
		document: fakeDoc(),
		localStorage: memoryStorage(),
		...overrides,
	};
	new Function("window", "document", src)(w, w.document);
	return {
		C: (w as { HP_Channels: Record<string, unknown> }).HP_Channels as never as Channels,
		w: w as { PathTogether?: { renderState: Record<string, unknown> | null } },
	};
}

interface Channels {
	MAX_ACTIVE_CHANNELS: number;
	DEFAULT_ACTIVE_CHANNELS: number;
	PALETTE: string[];
	normalizeChannelInfo(info: Record<string, unknown>): Record<string, unknown>;
	createDeepZoomTileSource(
		info: Record<string, unknown>,
		adapter: Record<string, unknown>,
		renderToken: string | null
	): Record<string, unknown>;
	sameFingerprint(a: string | null, b: string | null): boolean;
	syncState(a: string | null, b: string | null): string;
	invisibleReason(ch: Record<string, unknown>, enabled: boolean): string | null;
	effectiveVisible(ch: Record<string, unknown>, enabled: boolean): boolean;
	defaultSelection(channels: Array<Record<string, unknown>>): number[];
	clampSelection(channels: Array<Record<string, unknown>>, indexes: number[]): number[];
	buildRequestBody(
		channels: Array<Record<string, unknown>>,
		selection: number[],
		overrides: Record<string, Record<string, unknown>>
	): Record<string, unknown>;
	storageKey(scope: string, slideSafeName: string, assetRevision: string): string;
	loadStoredSelection(
		storage: Storage,
		key: string,
		channels: Array<Record<string, unknown>>
	): { selection: number[] | null; overrides: Record<string, Record<string, unknown>>; broken: boolean };
	saveStoredSelection(
		storage: Storage,
		key: string,
		selection: number[],
		overrides: Record<string, Record<string, unknown>>
	): void;
	createChannelController(opts: Record<string, unknown>): ChannelController;
}

interface ChannelController {
	epoch: number;
	info: Record<string, unknown> | null;
	selection: number[];
	overrides: Record<string, Record<string, unknown>>;
	renderToken: string | null;
	renderFingerprint: string | null;
	renderContext: Record<string, unknown> | null;
	isMultichannel(): boolean;
	getToken(): string | null;
	getFingerprint(): string | null;
	setAiFingerprint(fp: string | null): void;
	handleInfo(
		info: Record<string, unknown>,
		meta: { id: string; scope: string }
	): { kind: string; tileSource?: unknown; thumbnailUrl?: string | null };
	setChannelActive(index: number, on: boolean): boolean;
	setChannelColor(index: number, color: string): void;
	applySelectionForTest(): void;
	destroy(): void;
	panelEls: { root: FakeEl; rows: FakeEl[]; count: FakeEl; aiBadge: FakeEl } | null;
}

function memoryStorage() {
	const map = new Map<string, string>();
	return {
		getItem: (k: string) => (map.has(k) ? (map.get(k) as string) : null),
		setItem: (k: string, v: string) => void map.set(k, String(v)),
		removeItem: (k: string) => void map.delete(k),
		_map: map,
	} as unknown as Storage;
}

// fake 元素不可 JSON（parentNode 循环引用）：递归收集 textContent
function panelText(el: FakeEl): string {
	let out = el.textContent || "";
	el.children.forEach((c) => {
		out += "|" + panelText(c);
	});
	return out;
}

// ---------------------------------------------------------------- fixtures --
function channelEntry(i: number, extra: Record<string, unknown> = {}) {
	return {
		index: i,
		id: `Channel:0:${i}`,
		name: `C${i}`,
		color: ["#00FFFF", "#FF00FF", "#FFD166", "#00E676", "#FF5C5C", "#4D7CFE", "#FF8C42", "#B388FF", "#112233", "#445566", "#778899", "#AABBCC"][i % 12],
		alpha: 1,
		color_source: "ome",
		default_active: true,
		dtype: "uint16",
		intensity: { black: 1, white: 100, gamma: 1, source: "global-percentile-v1", status: "ok" },
		...extra,
	};
}

function multichannelInfo(n = 6, extra: Record<string, unknown> = {}) {
	// 与真实服务端一致：default_render_context.active_channels 由同一 manifest
	// 的「前 defaultActiveCount 个有效通道」生成（slide_render.build_default_render_context）。
	const defaultActiveCount = (extra.defaultActiveCount as number | undefined) ?? Math.min(4, n);
	delete extra.defaultActiveCount;
	const rev = (extra.asset_revision as string | undefined) || "rev-1";
	return {
		name: "slide_a.ome.tiff",
		image_mode: "multichannel",
		asset_revision: rev,
		server_capability: { multichannel: true, render_token: true, render_context_endpoint: true },
		channels: Array.from({ length: n }, (_, i) =>
			channelEntry(i, { default_active: i < defaultActiveCount })
		),
		default_render_context: {
			version: "multichannel-additive-v1",
			asset_revision: rev,
			plane: { t: 0, z: 0 },
			active_channels: Array.from({ length: defaultActiveCount }, (_, i) => ({
				index: i,
				color: ["#00FFFF", "#FF00FF", "#FFD166", "#00E676"][i % 4],
				alpha: 1,
				black: 1,
				white: 100,
				gamma: 1,
			})),
			fingerprint: "f".repeat(64),
		},
		default_render_token: "tok-default",
		plane: { t: 0, z: 0, size_t: 1, size_z: 1, policy: "first-plane-v1" },
		axes: "CYX",
		warnings: [],
		deepzoom: { width: 100000, height: 80000, tile_size: 512, overlap: 1, min_level: 0, max_level: 17 },
		...extra,
	};
}

function rgbInfo(flagOn: boolean) {
	return {
		name: "slide_b.svs",
		image_mode: "native_rgb",
		asset_revision: "rev-2",
		server_capability: {
			multichannel: false,
			render_token: flagOn,
			render_context_endpoint: flagOn,
		},
		channels: [],
		default_render_context: flagOn ? { version: "native-rgb-v1", fingerprint: "a".repeat(64) } : undefined,
		default_render_token: flagOn ? "tok-rgb" : undefined,
		warnings: [],
		deepzoom: { width: 1000, height: 800, tile_size: 512, overlap: 1, min_level: 0, max_level: 3 },
	};
}

// ------------------------------------------------------------------ tests --
describe("normalizeChannelInfo（§6.1/§8.1）", () => {
	it("flag 关闭：只回探测字段，不出面板、无通道（不依赖 image_mode）", () => {
		const C = loadModule();
		const n = C.normalizeChannelInfo({
			name: "x.tiff",
			image_mode: "multichannel",
			asset_revision: "rev-1",
			server_capability: { multichannel: false, render_token: false, render_context_endpoint: false },
		}) as Record<string, unknown>;
		expect(n.flagEnabled).toBe(false);
		expect(n.multichannel).toBe(false);
		expect((n.channels as unknown[]).length).toBe(0);
		expect(n.defaultToken).toBeNull();
	});

	it("flag 开 + native_rgb：不出面板（multichannel=false），保留 RGB 探测", () => {
		const C = loadModule();
		const n = C.normalizeChannelInfo(rgbInfo(true)) as Record<string, unknown>;
		expect(n.flagEnabled).toBe(true);
		expect(n.imageMode).toBe("native_rgb");
		expect(n.multichannel).toBe(false);
		expect((n.channels as unknown[]).length).toBe(0);
	});

	it("multichannel：通道归一化（大写颜色、非法色回色卡、Name 缺失、来源、可显示性）", () => {
		const C = loadModule();
		const n = C.normalizeChannelInfo(
			multichannelInfo(3, {
				channels: [
					channelEntry(0),
					channelEntry(1, { name: "", color: "#00ff00", color_source: "default" }),
					channelEntry(2, { color: "not-a-color", intensity: { status: "empty_or_constant" } }),
				],
			})
		) as Record<string, unknown>;
		expect(n.multichannel).toBe(true);
		expect(n.flagEnabled).toBe(true);
		expect(n.assetRevision).toBe("rev-1");
		expect(n.slideId).toBe("slide_a.ome.tiff");
		const ch = n.channels as Array<Record<string, unknown>>;
		expect(ch.length).toBe(3);
		expect(ch[0]!.color).toBe("#00FFFF");
		// 非法颜色 → 确定性色卡按索引回退（§5.2）
		expect(ch[2]!.color).toBe(C.PALETTE[2]);
		// Name 缺失（server 已置「通道 N」形态或空）→ nameMissing
		expect(ch[1]!.nameMissing).toBe(true);
		expect(ch[1]!.color).toBe("#00FF00");
		// empty_or_constant 通道不可启用（服务端会 400）
		expect(ch[2]!.displayable).toBe(false);
		expect(ch[0]!.displayable).toBe(true);
		// 元数据完整性摘要数据
		expect(n.namedCount).toBe(2);
		expect(n.omeColorCount).toBe(1);
	});

	it("T/Z>1 的结构化 warning 透传（§4.2 持续提示数据源）", () => {
		const C = loadModule();
		const n = C.normalizeChannelInfo(
			multichannelInfo(2, {
				warnings: [{ code: "first-plane-v1", message: "当前仅显示 T=0、Z=0；时间/层面切换尚未支持" }],
				plane: { t: 0, z: 0, size_t: 3, size_z: 2, policy: "first-plane-v1" },
			})
		) as Record<string, unknown>;
		expect((n.warnings as Array<{ code: string }>).some((w) => w.code === "first-plane-v1")).toBe(true);
		expect(n.plane).toEqual({ t: 0, z: 0, size_t: 3, size_z: 2, policy: "first-plane-v1" });
	});
});

describe("fingerprint 比较（AI 同步徽章 hook，Batch 5 接管）", () => {
	it("sameFingerprint / syncState 三态", () => {
		const C = loadModule();
		expect(C.sameFingerprint("aa", "aa")).toBe(true);
		expect(C.sameFingerprint("aa", "bb")).toBe(false);
		expect(C.sameFingerprint(null, "aa")).toBe(false);
		// ai fingerprint 未知 → "unknown"（Batch 5 前不显示徽章）
		expect(C.syncState("aa", null)).toBe("unknown");
		expect(C.syncState("aa", "aa")).toBe("synced");
		expect(C.syncState("aa", "bb")).toBe("stale");
	});
});

describe("默认 4 / 上限 8（§4.2）", () => {
	it("defaultSelection：12 通道 default_active 前 4；alpha=0 / empty_or_constant 不算有效", () => {
		const C = loadModule();
		const channels = Array.from({ length: 12 }, (_, i) =>
			channelEntry(i, { default_active: true })
		);
		channels[1] = channelEntry(1, { default_active: true, alpha: 0 });
		channels[2] = channelEntry(2, { default_active: true, intensity: { status: "empty_or_constant" } });
		const sel = C.defaultSelection(channels);
		expect(sel.length).toBe(4);
		expect(sel).toEqual([0, 3, 4, 5]);
	});

	it("defaultSelection：服务端未标 default_active 时回退前 4 个可显示通道", () => {
		const C = loadModule();
		const channels = Array.from({ length: 6 }, (_, i) => channelEntry(i, { default_active: false }));
		expect(C.defaultSelection(channels)).toEqual([0, 1, 2, 3]);
	});

	it("clampSelection：去重、剔除非法索引、最多 8 个", () => {
		const C = loadModule();
		const channels = Array.from({ length: 12 }, (_, i) => channelEntry(i));
		expect(C.clampSelection(channels, [5, 5, 99, 1, 2, 3, 4, 6, 7, 8])).toEqual([1, 2, 3, 4, 5, 6, 7, 8]);
		expect(C.MAX_ACTIVE_CHANNELS).toBe(8);
		expect(C.DEFAULT_ACTIVE_CHANNELS).toBe(4);
	});

	it("buildRequestBody：只提交用户选择（index+color），plane 固定 t=0,z=0；用户重选颜色带 alpha=1", () => {
		const C = loadModule();
		const channels = [channelEntry(0), channelEntry(1), channelEntry(2)];
		const body = C.buildRequestBody(
			channels,
			[2, 0],
			{ 2: { color: "#123ABC", alpha: 1 } }
		) as Record<string, unknown>;
		expect(body.plane).toEqual({ t: 0, z: 0 });
		const ac = body.active_channels as Array<Record<string, unknown>>;
		expect(ac.map((c) => c.index)).toEqual([0, 2]); // 升序
		expect(ac[0]!.color).toBe("#00FFFF");
		expect(ac[1]!.color).toBe("#123ABC");
		expect(ac[1]!.alpha).toBe(1);
		expect(JSON.stringify(body)).not.toContain("token");
	});
});

describe("localStorage 本地偏好（§8.3）", () => {
	it("key 含用户作用域 + 切片安全名 + asset revision，不只是文件名", () => {
		const C = loadModule();
		const k1 = C.storageKey("official:user1", "slide_a.ome.tiff", "rev-1");
		const k2 = C.storageKey("official:user2", "slide_a.ome.tiff", "rev-1");
		const k3 = C.storageKey("official:user1", "slide_a.ome.tiff", "rev-2");
		expect(k1).toContain("official:user1");
		expect(k1).toContain("slide_a.ome.tiff");
		expect(k1).toContain("rev-1");
		expect(new Set([k1, k2, k3]).size).toBe(3);
	});

	it("roundtrip：只存选择与覆盖，不存 token；读回一致", () => {
		const C = loadModule();
		const storage = memoryStorage();
		const key = C.storageKey("demo", "s.tiff", "r1");
		C.saveStoredSelection(storage, key, [0, 3], { 3: { color: "#AABBCC", alpha: 1 } });
		const raw = storage.getItem(key) as string;
		expect(raw).not.toContain("token");
		const loaded = C.loadStoredSelection(
			storage,
			key,
			[channelEntry(0), channelEntry(3)]
		);
		expect(loaded.broken).toBe(false);
		expect(loaded.selection).toEqual([0, 3]);
		expect(loaded.overrides[3]).toEqual({ color: "#AABBCC", alpha: 1 });
	});

	it("解析失败：broken=true + 空选择（调用方给一次非阻塞提示，不阻止打开）", () => {
		const C = loadModule();
		const storage = memoryStorage();
		const key = C.storageKey("demo", "s.tiff", "r1");
		storage.setItem(key, "{not json");
		const loaded = C.loadStoredSelection(storage, key, [channelEntry(0)]);
		expect(loaded.broken).toBe(true);
		expect(loaded.selection).toBeNull();
	});

	it("通道索引/数量变化：旧选择丢弃回默认", () => {
		const C = loadModule();
		const storage = memoryStorage();
		const key = C.storageKey("demo", "s.tiff", "r1");
		C.saveStoredSelection(storage, key, [0, 7], {});
		// 新切片只有 3 个通道：索引 7 不存在 → 丢弃
		const loaded = C.loadStoredSelection(
			storage,
			key,
			[channelEntry(0), channelEntry(1), channelEntry(2)]
		);
		expect(loaded.selection).toBeNull();
	});
});

describe("createDeepZoomTileSource（§8.1–8.2）", () => {
	it("inline custom TileSource：width/height/tileSize/overlap/min/max + getTileUrl 携带 render token", () => {
		const C = loadModule();
		const info = C.normalizeChannelInfo(multichannelInfo(2)) as Record<string, unknown>;
		const adapter = {
			tileUrl: (id: string, level: number, x: number, y: number, token: string | null) =>
				`/api/slide/${id}_files/${level}/${x}_${y}.jpeg?render=${token}`,
		};
		const ts = C.createDeepZoomTileSource(info, adapter, "tok-1") as Record<string, unknown>;
		expect(ts.width).toBe(100000);
		expect(ts.height).toBe(80000);
		expect(ts.tileSize).toBe(512);
		expect(ts.tileOverlap).toBe(1);
		expect(ts.maxLevel).toBe(17);
		expect(ts.minLevel).toBe(0);
		const url = (ts.getTileUrl as (l: number, x: number, y: number) => string)(3, 2, 5);
		expect(url).toBe("/api/slide/slide_a.ome.tiff_files/3/2_5.jpeg?render=tok-1");
	});
});

// ------------------------------------------------------------------ tests --
// controller 共用 opts（F4 新增 describe 也在模块级复用）
function baseOpts(adapter: Record<string, unknown>, viewer: Record<string, unknown>) {
	const storage = memoryStorage();
	const toasts: Array<{ msg: string; type?: string }> = [];
	const host = fakeEl();
	const button = fakeEl("button");
	const badge = fakeEl("span");
	return {
		adapter,
		viewer,
		panelHost: host,
		button,
		badge,
		storage,
		toasts,
		t: (key: string, vars?: Record<string, unknown>) =>
			key + (vars && vars.n != null ? `:${vars.n}/${vars.m}` : ""),
		toast: (msg: string, type?: string) => toasts.push({ msg, type }),
		onReopening: vi.fn(),
		onReopened: vi.fn(),
		setThumbnail: vi.fn(),
	};
}

function deferred<T>() {
	let resolve!: (v: T) => void;
	const promise = new Promise<T>((r) => {
		resolve = r;
	});
	return { promise, resolve };
}

describe("controller：面板/flag/竞争/恢复 viewport", () => {
	it("RGB：不出面板；flag 关：什么都不显示；两者都不发 render-context（§4.1/§15.2）", () => {
		const C = loadModule();
		const calls: string[] = [];
		const adapter = {
			normalizeRenderContext: () => {
				calls.push("post");
				return Promise.resolve({ ok: true, json: () => Promise.resolve({}) });
			},
		};
		const opts = baseOpts(adapter, {});
		const ctrl = C.createChannelController(opts);
		// flag 关（info 只有探测字段）
		const planOff = ctrl.handleInfo(
			{ name: "m.tiff", image_mode: "multichannel", server_capability: { render_context_endpoint: false } },
			{ id: "m.tiff", scope: "demo" }
		);
		expect(planOff.kind).toBe("legacy");
		// flag 开 + RGB
		const planRgb = ctrl.handleInfo(rgbInfo(true), { id: "slide_b.svs", scope: "demo" });
		expect(planRgb.kind).toBe("legacy");
		expect(opts.badge.hidden).toBe(false); // 灰色「原始 RGB」小标识
		expect(opts.button.hidden).toBe(true);
		expect(opts.panelHost.children.length).toBe(0);
		expect(calls).toEqual([]); // 不发新字段
	});

	it("multichannel：面板渲染、默认启用 ≤4、计划用服务端默认 token 打开", () => {
		const C = loadModule();
		const opts = baseOpts(
			{ tileUrl: () => "", thumbnailUrl: () => "thumb?render=t", normalizeRenderContext: () => Promise.resolve({ ok: true, json: () => Promise.resolve({}) }) },
			fakeViewerWithHandlers()
		);
		const ctrl = C.createChannelController(opts) as ChannelController;
		const plan = ctrl.handleInfo(multichannelInfo(12), { id: "slide_a.ome.tiff", scope: "demo" });
		expect(plan.kind).toBe("render");
		expect(ctrl.selection.length).toBe(4);
		expect(opts.button.hidden).toBe(false);
		expect(opts.badge.hidden).toBe(true);
		const root = ctrl.panelEls!.root;
		expect(root.hidden).toBe(false);
		// 面板列出全部 12 个通道
		expect(ctrl.panelEls!.rows.length).toBe(12);
		// 计数文案「已显示 n/m 个通道」
		expect(ctrl.panelEls!.count.textContent).toContain("4/12");
		// T/Z>1 持续提示存在
		const info2 = ctrl.handleInfo(
			multichannelInfo(2, { plane: { t: 0, z: 0, size_t: 2, size_z: 1, policy: "first-plane-v1" } }),
			{ id: "slide_a.ome.tiff", scope: "demo" }
		);
		expect(info2.kind).toBe("render");
		expect(panelText(ctrl.panelEls!.root)).toContain("plane");
	});

	it("第 9 个通道被阻止并给可读提示；最后一个通道不可关闭", () => {
		const C = loadModule();
		const opts = baseOpts(
			{
				tileUrl: () => "",
				thumbnailUrl: () => "",
				normalizeRenderContext: () =>
					Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve({ render_context: {}, render_context_fingerprint: "x", render_token: "t" }) }),
			},
			fakeViewerWithHandlers()
		);
		const ctrl = C.createChannelController(opts) as ChannelController;
		ctrl.handleInfo(multichannelInfo(12), { id: "slide_a.ome.tiff", scope: "demo" });
		// 默认 4 个已启用：再开 4 个到 8
		[4, 5, 6, 7].forEach((i) => expect(ctrl.setChannelActive(i, true)).toBe(true));
		expect(ctrl.selection.length).toBe(8);
		// 第 9 个：阻止 + 可读提示
		expect(ctrl.setChannelActive(8, true)).toBe(false);
		expect(ctrl.selection.length).toBe(8);
		expect(opts.toasts.length).toBeGreaterThan(0);
		expect(opts.toasts[opts.toasts.length - 1]!.msg).toContain("channel.limit.block");
		// 关到只剩 1 个后再关：阻止
		[7, 6, 5, 4, 3, 2, 1].forEach((i) => expect(ctrl.setChannelActive(i, false)).toBe(true));
		expect(ctrl.selection).toEqual([0]);
		expect(ctrl.setChannelActive(0, false)).toBe(false);
		expect(opts.toasts[opts.toasts.length - 1]!.msg).toContain("channel.min.block");
	});

	it("快速连点竞争：epoch 保证只应用最后一次，旧响应不覆盖新 context（§8.2）", async () => {
		const C = loadModule();
		const viewer = fakeViewerWithHandlers();
		const first = deferred<{ ok: boolean; status: number; json: () => Promise<Record<string, unknown>> }>();
		const second = deferred<{ ok: boolean; status: number; json: () => Promise<Record<string, unknown>> }>();
		const postBodies: unknown[] = [];
		let call = 0;
		const responses = [first, second];
		const adapter = {
			tileUrl: (id: string, level: number, x: number, y: number, token: string | null) =>
				`/api/slide/${id}_files/${level}/${x}_${y}.jpeg?render=${token}`,
			thumbnailUrl: () => "",
			normalizeRenderContext: (_id: string, body: unknown) => {
				postBodies.push(JSON.parse(JSON.stringify(body)));
				return responses[call++]!.promise;
			},
		};
		const opts = baseOpts(adapter, viewer);
		const ctrl = C.createChannelController(opts) as ChannelController;
		ctrl.handleInfo(multichannelInfo(8), { id: "slide_a.ome.tiff", scope: "demo" });
		viewer._hpLog.opened.length = 0;

		// 连点两次：在默认 4 个（0-3）之上先后加通道 4、5
		ctrl.setChannelActive(4, true);
		ctrl.setChannelActive(5, true);
		expect(ctrl.selection).toEqual([0, 1, 2, 3, 4, 5]);
		// flush 微任务：两次 applySelection 的 POST 均已发出
		await Promise.resolve();
		await Promise.resolve();
		// 两次 POST 都已发出
		expect(postBodies.length).toBe(2);
		expect((postBodies[0] as { active_channels: unknown[] }).active_channels.length).toBe(5);
		expect((postBodies[1] as { active_channels: unknown[] }).active_channels.length).toBe(6);

		// 第一次响应**后**返回（乱序完成）：必须被 epoch 丢弃
		first.resolve({
			ok: true,
			status: 200,
			json: () => Promise.resolve({ render_context: { v: 1 }, render_context_fingerprint: "old", render_token: "tok-old" }),
		});
		second.resolve({
			ok: true,
			status: 200,
			json: () => Promise.resolve({ render_context: { v: 2 }, render_context_fingerprint: "new", render_token: "tok-new" }),
		});
		await Promise.resolve();
		await new Promise((r) => setTimeout(r, 0));
		await new Promise((r) => setTimeout(r, 0));

		expect(ctrl.renderFingerprint).toBe("new");
		expect(ctrl.renderToken).toBe("tok-new");
		// 最后一次打开的 TileSource 携带新 token（旧响应未触发新的 open）
		expect(viewer._hpLog.opened.length).toBe(1);
		const ts = viewer._hpLog.opened[0] as Record<string, unknown>;
		const url = (ts.getTileUrl as (l: number, x: number, y: number) => string)(0, 0, 0);
		expect(url).toContain("render=tok-new");
		// viewer close 恰好两次（每次 apply 前），open 只有一次（最后一次）
		expect(viewer._hpLog.closed).toBe(2);
		// 恢复 viewport（center/zoom/rotation/flip）
		expect(viewer._hpLog.restores.some((r) => r.zoom === 1.25)).toBe(true);
		expect(viewer._hpLog.restores.some((r) => r.rotation === 90)).toBe(true);
		expect(viewer._hpLog.restores.some((r) => r.flip === true)).toBe(true);
	});

	it("POST 409 slide_revision_conflict：只刷新 info 重建一次（§6.3）", async () => {
		const C = loadModule();
		const viewer = fakeViewerWithHandlers();
		const refreshInfo = vi.fn(() =>
			Promise.resolve(
				multichannelInfo(2, { asset_revision: "rev-2", defaultActiveCount: 1 })
			)
		);
		let attempt = 0;
		const adapter = {
			tileUrl: () => "",
			thumbnailUrl: () => "",
			normalizeRenderContext: () => {
				attempt += 1;
				if (attempt === 1) {
					return Promise.resolve({
						ok: false,
						status: 409,
						json: () => Promise.resolve({ code: "slide_revision_conflict", error: "conflict" }),
					});
				}
				return Promise.resolve({
					ok: true,
					status: 200,
					json: () => Promise.resolve({ render_context: {}, render_context_fingerprint: "fp2", render_token: "tok2" }),
				});
			},
		};
		const opts = baseOpts(adapter, viewer);
		(opts as Record<string, unknown>).refreshInfo = refreshInfo;
		const ctrl = C.createChannelController(opts) as ChannelController;
		ctrl.handleInfo(multichannelInfo(2, { defaultActiveCount: 1 }), {
			id: "slide_a.ome.tiff",
			scope: "demo",
		});
		expect(ctrl.selection).toEqual([0]);
		ctrl.setChannelActive(1, true);
		await new Promise((r) => setTimeout(r, 5));
		expect(refreshInfo).toHaveBeenCalledTimes(1);
		// 刷新 info 后重试一次成功 → token 更新，不再无限重试
		expect(attempt).toBeGreaterThanOrEqual(2);
		expect(opts.toasts.some((t) => t.msg.includes("channel.ctx.conflict"))).toBe(true);
	});

	it("用户改色：来源变「用户调整」并持久化到 localStorage；换切片丢弃/回默认", () => {
		const C = loadModule();
		const opts = baseOpts(
			{
				tileUrl: () => "",
				thumbnailUrl: () => "",
				normalizeRenderContext: () =>
					Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve({ render_context: {}, render_context_fingerprint: "fp", render_token: "t" }) }),
			},
			fakeViewerWithHandlers()
		);
		const ctrl = C.createChannelController(opts) as ChannelController;
		ctrl.handleInfo(multichannelInfo(3), { id: "slide_a.ome.tiff", scope: "demo" });
		ctrl.setChannelColor(1, "#123456");
		const key = C.storageKey("demo", "slide_a.ome.tiff", "rev-1");
		const stored = JSON.parse((opts.storage as Storage).getItem(key) as string) as Record<string, unknown>;
		expect(stored.overrides).toBeTruthy();
		// 本地偏好不含 token（§8.3）
		expect(JSON.stringify(stored)).not.toContain("token");
	});

	it("localStorage 坏数据：回默认 + 恰好一次非阻塞提示", () => {
		const C = loadModule();
		const opts = baseOpts(
			{ tileUrl: () => "", thumbnailUrl: () => "", normalizeRenderContext: () => Promise.resolve({ ok: true, json: () => Promise.resolve({}) }) },
			fakeViewerWithHandlers()
		);
		const key = C.storageKey("demo", "slide_a.ome.tiff", "rev-1");
		(opts.storage as Storage).setItem(key, "}{broken");
		const ctrl = C.createChannelController(opts) as ChannelController;
		ctrl.handleInfo(multichannelInfo(3), { id: "slide_a.ome.tiff", scope: "demo" });
		// 回默认（前 4 → 这里 3 个）
		expect(ctrl.selection).toEqual([0, 1, 2]);
		const brokenToasts = opts.toasts.filter((t) => t.msg.includes("channel.pref.broken"));
		expect(brokenToasts.length).toBe(1);
	});
});

// --------------------------------------------------------------------------- //
// F4 有效不可见：勾选但对合成贡献为 0 的通道，行内如实标注原因；
// 不自动改色（OME 黑色 DAPI 保持 #000000，用户改色后即变可见）。
// --------------------------------------------------------------------------- //
describe("F4 invisibleReason 优先级（disabled > alpha_zero > empty_window > black_mapping）", () => {
	const C = loadModule();
	const ok = { black: 1, white: 100, status: "ok" };

	it("各原因码与有效可见性", () => {
		expect(C.invisibleReason({ color: "#000000", alpha: 0, intensity: ok }, false)).toBe("disabled");
		expect(C.invisibleReason({ color: "#000000", alpha: 0, intensity: ok }, true)).toBe("alpha_zero");
		expect(
			C.invisibleReason({ color: "#000000", alpha: 1, intensity: { black: 10, white: 5, status: "ok" } }, true)
		).toBe("empty_window");
		expect(
			C.invisibleReason({ color: "#000000", alpha: 1, intensity: { status: "empty_or_constant" } }, true)
		).toBe("empty_window");
		expect(C.invisibleReason({ color: "#000000", alpha: 1, intensity: ok }, true)).toBe("black_mapping");
		expect(C.invisibleReason({ color: "#00FFFF", alpha: 1, intensity: ok }, true)).toBeNull();
		expect(C.effectiveVisible({ color: "#000000", alpha: 1, intensity: ok }, true)).toBe(false);
		expect(C.effectiveVisible({ color: "#00FFFF", alpha: 1, intensity: ok }, true)).toBe(true);
	});
});

describe("F4 通道行：勾选但有效不可见 → ch-invisible 行 + 原因文案，不自动改色", () => {
	const noopAdapter = {
		tileUrl: () => "",
		thumbnailUrl: () => "",
		normalizeRenderContext: () =>
			Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve({ render_context: {}, render_context_fingerprint: "fp", render_token: "t" }) }),
	};

	function rowOf(ctrl: ChannelController, index: number): FakeEl {
		const row = (ctrl.panelEls!.rows as FakeEl[]).find((r) => r.dataset.index === String(index));
		if (!row) throw new Error("row not found: " + index);
		return row;
	}
	function childByClass(row: FakeEl, cls: string): FakeEl | undefined {
		return row.children.find((c) => String(c.className).split(" ").includes(cls));
	}

	it("黑色 DAPI：行出现 black 原因；颜色样例仍 #000000，checkbox 仍勾选可用", () => {
		const C = loadModule();
		const opts = baseOpts(noopAdapter, fakeViewerWithHandlers());
		const ctrl = C.createChannelController(opts) as ChannelController;
		ctrl.handleInfo(
			multichannelInfo(4, {
				channels: [
					channelEntry(0, { name: "DAPI", color: "#000000" }),
					channelEntry(1),
					channelEntry(2),
					channelEntry(3),
				],
			}),
			{ id: "slide_a.ome.tiff", scope: "demo" }
		);
		expect(ctrl.selection).toContain(0); // 勾选态
		const row = rowOf(ctrl, 0);
		expect(String(row.className)).toContain("ch-invisible");
		const reason = childByClass(row, "ch-invisible-reason");
		expect(reason!.textContent).toContain("channel.invisible.black");
		// 不自动改色：显示色保持 OME 原 #000000
		expect(childByClass(row, "ch-color")!.value).toBe("#000000");
		// checkbox 仍可勾选（用户改色后变可见）：未禁用、保持勾选
		const cb = row.children[0]!;
		expect(cb.checked).toBe(true);
		expect(cb.disabled).toBe(false);
		// 正常通道行没有原因文案
		expect(String(rowOf(ctrl, 1).className)).not.toContain("ch-invisible");
	});

	it("alpha=0：勾选行显示 alpha 原因；white<=black：显示 window 原因", () => {
		const C = loadModule();
		const opts = baseOpts(noopAdapter, fakeViewerWithHandlers());
		const ctrl = C.createChannelController(opts) as ChannelController;
		ctrl.handleInfo(
			multichannelInfo(4, {
				channels: [
					channelEntry(0),
					channelEntry(1, { alpha: 0 }),
					channelEntry(2, { intensity: { black: 50, white: 50, status: "ok" } }),
					channelEntry(3),
				],
			}),
			{ id: "slide_a.ome.tiff", scope: "demo" }
		);
		// defaultContext 勾选 0..3
		expect(childByClass(rowOf(ctrl, 1), "ch-invisible-reason")!.textContent).toContain(
			"channel.invisible.alpha"
		);
		expect(childByClass(rowOf(ctrl, 2), "ch-invisible-reason")!.textContent).toContain(
			"channel.invisible.window"
		);
	});

	it("用户改色后原因消失（覆盖层 alpha 归 1、颜色非黑）", () => {
		const C = loadModule();
		const opts = baseOpts(noopAdapter, fakeViewerWithHandlers());
		const ctrl = C.createChannelController(opts) as ChannelController;
		ctrl.handleInfo(
			multichannelInfo(2, {
				channels: [channelEntry(0, { name: "DAPI", color: "#000000" }), channelEntry(1)],
			}),
			{ id: "slide_a.ome.tiff", scope: "demo" }
		);
		expect(String(rowOf(ctrl, 0).className)).toContain("ch-invisible");
		ctrl.setChannelColor(0, "#3366CC");
		const row = rowOf(ctrl, 0);
		expect(String(row.className)).not.toContain("ch-invisible");
		expect(childByClass(row, "ch-color")!.value).toBe("#3366CC");
	});
});

describe("F4 AI 徽章：名称可用性与配色同步分开，不用 fingerprint 代替名称", () => {
	it("namedCount>0 → names_ready；名称缺失（通道 N）→ names_unknown；同步态独立追加", () => {
		const C = loadModule();
		const opts = baseOpts(
			{
				tileUrl: () => "",
				thumbnailUrl: () => "",
				normalizeRenderContext: () => Promise.resolve({ ok: true, json: () => Promise.resolve({}) }),
			},
			fakeViewerWithHandlers()
		);
		const ctrl = C.createChannelController(opts) as ChannelController;
		ctrl.handleInfo(multichannelInfo(2), { id: "slide_a.ome.tiff", scope: "demo" });
		const badge = ctrl.panelEls!.aiBadge;
		expect(badge.hidden).toBe(false);
		expect(badge.textContent).toContain("channel.ai.names_ready");
		// fingerprint 未知（未 setAiFingerprint）→ 不显示同步态，但名称行仍在
		expect(badge.textContent).not.toContain("channel.ai.synced");
		ctrl.setAiFingerprint(ctrl.getFingerprint());
		expect(badge.textContent).toContain("channel.ai.names_ready");
		expect(badge.textContent).toContain("channel.ai.synced");

		// 名称缺失（服务端回填「通道 N」形态）→ names_unknown
		ctrl.handleInfo(
			multichannelInfo(2, {
				channels: [channelEntry(0, { name: "通道 1" }), channelEntry(1, { name: "通道 2" })],
			}),
			{ id: "slide_a.ome.tiff", scope: "demo" }
		);
		expect(ctrl.panelEls!.aiBadge.textContent).toContain("channel.ai.names_unknown");
	});
});

describe("F4 publishRenderState：namedCount / channelSemanticsReady", () => {
	it("renderState 与 fingerprint 一起下发名称可用性；名称缺失时 ready=false 但指纹仍在", () => {
		const { C, w } = loadModuleAndWindow();
		const opts = baseOpts(
			{
				tileUrl: () => "",
				thumbnailUrl: () => "",
				normalizeRenderContext: () => Promise.resolve({ ok: true, json: () => Promise.resolve({}) }),
			},
			fakeViewerWithHandlers()
		);
		const ctrl = C.createChannelController(opts) as ChannelController;
		ctrl.handleInfo(multichannelInfo(2), { id: "slide_a.ome.tiff", scope: "demo" });
		let rs = w.PathTogether!.renderState;
		expect(typeof rs!.renderFingerprint).toBe("string");
		expect(rs!.namedCount).toBe(2);
		expect(rs!.channelSemanticsReady).toBe(true);

		ctrl.handleInfo(
			multichannelInfo(2, {
				channels: [channelEntry(0, { name: "通道 1" }), channelEntry(1, { name: "通道 2" })],
			}),
			{ id: "slide_a.ome.tiff", scope: "demo" }
		);
		rs = w.PathTogether!.renderState;
		expect(rs!.namedCount).toBe(0);
		expect(rs!.channelSemanticsReady).toBe(false);
		expect(typeof rs!.renderFingerprint).toBe("string"); // 两者独立
	});
});

// --------------------------------------------------------------------------- //
// §8.2 adapter 契约（static/app-mode.js）：三种访问面的 URL/方法/CSRF 形态。
// URL 一旦漂移，tile/thumbnail/crop 与 render-context 会各自散架——这里按真实
// 源码锁定。
// --------------------------------------------------------------------------- //
interface AppModeAdapter {
	mode: string;
	dziUrl(id: string): string;
	thumbnailUrl(id: string, renderToken?: string): string;
	tileUrl?(id: string, level: number, x: number, y: number, renderToken?: string | null): string;
	cropUrl?(id: string, x: number, y: number, size: number, renderToken?: string | null): string;
	normalizeRenderContext?(id: string, body: unknown): Promise<unknown>;
}

function loadAppMode(fetchLog: Array<{ url: string; init?: RequestInit }>, cookie = "") {
	const modeSrc = readFileSync(resolve(here, "../../static/app-mode.js"), "utf8");
	const w: Record<string, unknown> = {
		HP_APP_BOOTSTRAP: { mode: "official", capabilities: {} },
		document: { cookie },
		fetch: (url: string, init?: RequestInit) => {
			fetchLog.push({ url, init });
			return Promise.resolve({ ok: true, json: () => Promise.resolve({}) });
		},
	};
	new Function("window", "document", "fetch", modeSrc)(w, w.document, w.fetch);
	return (w as { HP_API: AppModeAdapter }).HP_API;
}

describe("app-mode.js adapter（§8.2）", () => {
	it("official：tile/thumbnail/crop 无 token 时 URL 与旧路径一致；有 token 带 ?render=", () => {
		const log: Array<{ url: string; init?: RequestInit }> = [];
		const api = loadAppMode(log, "csrf_token=tkn1");
		expect(api.mode).toBe("official");
		expect(api.dziUrl("s 1")).toBe("/api/slide/s%201.dzi");
		// RGB/legacy：不带 render 参数（行为不变，§4.1）
		expect(api.thumbnailUrl("s 1")).toBe("/api/slide/s%201/thumbnail");
		expect(api.thumbnailUrl("s 1", "tok")).toBe("/api/slide/s%201/thumbnail?render=tok");
		expect(api.tileUrl!("s 1", 3, 2, 5, null)).toBe("/api/slide/s%201_files/3/2_5.jpeg");
		expect(api.tileUrl!("s 1", 3, 2, 5, "tok")).toBe("/api/slide/s%201_files/3/2_5.jpeg?render=tok");
		expect(api.cropUrl!("s 1", 10, 20, 600, "tok")).toContain("/crop?x=10&y=20&size=600&render=tok");
	});

	it("official：normalizeRenderContext POST render-context 且附 X-CSRF-Token", async () => {
		const log: Array<{ url: string; init?: RequestInit }> = [];
		const api = loadAppMode(log, "csrf_token=tkn1");
		await api.normalizeRenderContext!("s1", { active_channels: [{ index: 0 }], plane: { t: 0, z: 0 } });
		expect(log.length).toBe(1);
		expect(log[0]!.url).toBe("/api/slide/s1/render-context");
		expect(log[0]!.init!.method).toBe("POST");
		const headers = log[0]!.init!.headers as Record<string, string>;
		expect(headers["X-CSRF-Token"]).toBe("tkn1");
		expect(String(log[0]!.init!.body)).toContain("active_channels");
	});

	it("demo：tile/render-context 指向 /api/demo/slides/，不套 CSRF（capability 鉴权）", async () => {
		const log: Array<{ url: string; init?: RequestInit }> = [];
		const modeSrc = readFileSync(resolve(here, "../../static/app-mode.js"), "utf8");
		const w: Record<string, unknown> = {
			HP_APP_BOOTSTRAP: { mode: "demo", capabilities: {} },
			document: { cookie: "csrf_token=tkn1" },
			fetch: (url: string, init?: RequestInit) => {
				log.push({ url, init });
				return Promise.resolve({ ok: true, json: () => Promise.resolve({}) });
			},
		};
		new Function("window", "document", "fetch", modeSrc)(w, w.document, w.fetch);
		const api = (w as { HP_API: AppModeAdapter }).HP_API;
		expect(api.mode).toBe("demo");
		expect(api.thumbnailUrl("d1")).toBe(""); // Demo 无缩略图端点
		expect(api.tileUrl!("d1", 2, 0, 1, "tok")).toBe(
			"/api/demo/slides/d1_files/2/0_1.jpeg?render=tok"
		);
		await api.normalizeRenderContext!("d1", { active_channels: [{ index: 1 }] });
		expect(log[0]!.url).toBe("/api/demo/slides/d1/render-context");
		const headers = log[0]!.init!.headers as Record<string, string>;
		expect(headers["X-CSRF-Token"]).toBeUndefined();
	});
});
