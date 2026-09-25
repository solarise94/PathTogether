/**
 * slide ID 化前端契约（P2 合同 §5 / 计划 §2.4.1 A4 补充用例）。
 *
 * 加载真实 static/app.js（最小 DOM + fetch mock + HostBridgeHost stub）：
 *  ① 同 display_name 两片并存——列表两行（data-slide-id 区分）、分别打开不串；
 *  ② 改名（PATCH /api/slides/<id> 后重拉列表）ID 不变、行仍指向同片；
 *  ③ V1 上传完成按响应 slide_id 打开（ID 通道 info，绝不按 file.name 猜）；
 *  ④ 续传键 v3 账户域隔离（换 account 不复用会话）；
 *  ⑤ slide_id_api=false（旧后端）回落 name 通道（旧端点 + 载荷无 id）；
 *  ⑥ ?slide=<slide_id> URL 通道加载即打开对应切片。
 */
import { afterEach, describe, expect, it, vi } from "vitest";
import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const appSrc = readFileSync(resolve(here, "../../static/app.js"), "utf8");

const NAME_A = "same-a.svs";
const NAME_B = "same-b.svs";
const ID_A = "sld_same0000001";
const ID_B = "sld_same0000002";
const DISP = "同名切片";

// ---------- 假元素（同 slide-opened-revision.test.ts 口径） ----------
interface FakeEl extends Record<string, unknown> {
	id: string;
	hidden: boolean;
	title: string;
	type: string;
	checked: boolean;
	disabled: boolean;
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
	insertBefore: (c: unknown, ref?: unknown) => unknown;
	removeChild: (c: FakeEl) => unknown;
	remove: () => void;
	querySelector: (sel: string) => FakeEl | null;
	querySelectorAll: (sel: string) => FakeEl[];
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
		disabled: false,
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
		removeEventListener: (type, cb) => {
			listeners[type] = (listeners[type] || []).filter((h) => h !== cb);
		},
		focus() {},
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
		removeChild: (c) => {
			const i = children.indexOf(c);
			if (i >= 0) children.splice(i, 1);
			return c;
		},
		remove() {},
		// 深度按 class 查找（生产代码用 .slide-row / .slide-edit 等选择器）
		querySelector: (sel) => findByClass(el, sel.replace(/^\./, ""))[0] || null,
		querySelectorAll: (sel) => findByClass(el, sel.replace(/^\./, "")),
		closest: () => null,
		getBoundingClientRect: () => ({ width: 800, height: 600, left: 0, top: 0 }),
		getContext: () => ({
			clearRect() {}, save() {}, restore() {}, setTransform() {},
			setLineDash() {}, strokeRect() {}, fillRect() {}, fillText() {},
			measureText: (t: unknown) => ({ width: String(t).length * 6 }),
			drawImage() {},
		}),
	};
	Object.defineProperty(el, "className", {
		get: () => Array.from(classes).join(" "),
		set: (v) => {
			classes.clear();
			String(v).split(/\s+/).filter(Boolean).forEach((n) => classes.add(n));
		},
		configurable: true,
	});
	return el;
}

function findByClass(root: FakeEl, cls: string): FakeEl[] {
	const out: FakeEl[] = [];
	const walk = (node: FakeEl) => {
		for (const c of node.children) {
			if (c.classList.contains(cls)) out.push(c);
			walk(c);
		}
	};
	walk(root);
	return out;
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
	length: number;
	key: (i: number) => string | null;
}

function fakeStorage(): StorageLike {
	const m = new Map<string, string>();
	return {
		getItem: (k) => (m.has(k) ? (m.get(k) as string) : null),
		setItem: (k, v) => void m.set(k, String(v)),
		removeItem: (k) => void m.delete(k),
		get length() { return m.size; },
		key: (i) => Array.from(m.keys())[i] ?? null,
	};
}

// 最小 XHR 桩（③ 的 V1 上传路径；不模拟网络，只记录）
class FakeXHR {
	static instances: FakeXHR[] = [];
	open = vi.fn();
	setRequestHeader = vi.fn();
	send = vi.fn();
	status = 0;
	responseText = "";
	upload = { addEventListener() {} };
	private listeners: Record<string, Array<() => void>> = {};
	constructor() { FakeXHR.instances.push(this); }
	addEventListener(type: string, cb: () => void) {
		(this.listeners[type] ||= []).push(cb);
	}
	simulateLoad(status: number, body: string) {
		this.status = status;
		this.responseText = body;
		(this.listeners["load"] || []).forEach((cb) => cb());
	}
}

interface BootOpts {
	idMode?: boolean;          // 注入 slide_id_api 能力（缺省=false：旧后端回落）
	slides?: Array<Record<string, unknown>>;
	infoBySlideId?: Record<string, Record<string, unknown>>;
	authInfo?: Record<string, unknown> | null;
	storage?: StorageLike;
	search?: string;           // location.search（?slide= 通道）
	fetchImpl?: typeof fetch;  // 自定义 fetch（④ 的 /api/uploads 路由）
}

interface BootResult {
	els: Record<string, FakeEl>;
	created: FakeEl[];
	emitted: Array<{ type: string; payload: Record<string, unknown> }>;
	fetchUrls: () => string[];
	unfiledRows: () => FakeEl[];
	rowBySlideId: (id: string) => FakeEl;
	upload: Record<string, unknown>;
	storage: StorageLike;
}

function bootApp(opts: BootOpts): BootResult {
	const els: Record<string, FakeEl> = {};
	const created: FakeEl[] = [];
	const docListeners: Record<string, Array<() => void>> = {};
	const rafCbs: Array<() => void> = [];
	const emitted: Array<{ type: string; payload: Record<string, unknown> }> = [];
	const urls: string[] = [];

	const slides = opts.slides !== undefined ? opts.slides : [
		{ name: NAME_A, slide_id: ID_A, display_name: DISP, width: 1000, height: 800, mpp_x: 0.5, mpp_source: "native" },
		{ name: NAME_B, slide_id: ID_B, display_name: DISP, width: 2000, height: 1600, mpp_x: 0.25, mpp_source: "native" },
	];
	const infoBySlideId = opts.infoBySlideId || {
		[ID_A]: { name: NAME_A, slide_id: ID_A, display_name: DISP, original_filename: NAME_A, width: 1000, height: 800, mpp_x: 0.5, mpp_y: 0.5, mpp_source: "native", asset_revision: "1757000000000000000:42" },
		[ID_B]: { name: NAME_B, slide_id: ID_B, display_name: DISP, original_filename: NAME_B, width: 2000, height: 1600, mpp_x: 0.25, mpp_y: 0.25, mpp_source: "native", asset_revision: "1757000000000000000:43" },
	};

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
		removeHandler(type: string, fn: (e?: unknown) => void) {
			handlers[type] = (handlers[type] || []).filter((h) => h !== fn);
		},
		open(_src: unknown) {
			// 生产里 open 在 tile source 就绪后异步触发；harness 同步触发
			(handlers["open"] || []).forEach((fn) => fn({}));
		},
		close() {
			(handlers["close"] || []).forEach((fn) => fn({}));
		},
		setMouseNavEnabled() {},
	};

	const fetchImpl = (opts.fetchImpl || ((url: string) => {
		urls.push(String(url));
		const u = String(url);
		// /api/slides/<slide_id>/info（ID 通道；slide_id 为 sld_ 前缀）
		let m = u.match(/^\/api\/slides\/(sld_[^/]+)\/info$/);
		if (m) return jsonResponse(infoBySlideId[decodeURIComponent(m[1])] || { error: "not_found" });
		// 旧端点 /api/slide/<name>/info（name 带扩展名；回落通道）
		m = u.match(/^\/api\/slide\/([^/]+)\/info$/);
		if (m) {
			const n = decodeURIComponent(m[1]);
			const hit = (slides as Array<Record<string, unknown>>).find((s) => s.name === n);
			return jsonResponse(hit ? Object.assign({ asset_revision: "1757000000000000000:42" }, hit) : { error: "not_found" });
		}
		if (u.includes("/api/annotations")) return jsonResponse({ annotations: [], by_slide: {} });
		if (u.includes("/api/auth/info")) {
			return jsonResponse(opts.authInfo === null ? {} : (opts.authInfo || {}));
		}
		if (u.includes("/api/projects")) return jsonResponse([]);
		if (u.includes("/api/slides")) return jsonResponse(slides);
		if (u.includes("/api/share/list")) return jsonResponse({ shares: [] });
		return jsonResponse({});
	})) as unknown as typeof fetch;

	const doc = {
		readyState: "loading",
		cookie: "",
		title: "",
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
		// openSlide 高亮走 document.querySelectorAll(".slide-row")：在已创建
		// 元素里按 class 深度查找（含自身）
		querySelectorAll: (sel: string) => {
			const cls = sel.replace(/^\./, "");
			const out: FakeEl[] = [];
			for (const el of created) {
				if (el.classList.contains(cls)) out.push(el);
				for (const d of findByClass(el, cls)) out.push(d);
			}
			return out;
		},
		body: fakeEl("body"),
	};

	const storage = opts.storage || fakeStorage();
	const loc = { href: "http://local/" + (opts.search || ""), pathname: "/", search: opts.search || "" };

	const w: Record<string, unknown> = {
		HP_I18N: { t: (k: string) => k, getLang: () => "zh" },
		HP_ViewerCore: { create: () => fakeViewer },
		HP_API: {},
		...(opts.idMode ? { HP_APP_BOOTSTRAP: { mode: "official", capabilities: { slide_id_api: true } } } : {}),
		HistoPilot: {},
		HostBridgeHost: {
			onRequest() {},
			onEvent() {},
			emit(type: string, payload: Record<string, unknown>) {
				emitted.push({ type, payload });
			},
			request() { return Promise.reject({ code: "plugin_disabled" }); },
		},
		fetch: fetchImpl,
		location: loc,
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
	(globalThis as { HP_ViewerCore?: unknown }).HP_ViewerCore = { create: () => fakeViewer };
	(globalThis as { XMLHttpRequest?: unknown }).XMLHttpRequest = FakeXHR;
	// app.js 的上传续传键用裸 localStorage 标识符（全局链解析）
	(globalThis as { localStorage?: unknown }).localStorage = storage;

	new Function("window", "document", "fetch", "location", appSrc)(w, doc, fetchImpl, loc);

	(docListeners["DOMContentLoaded"] || []).forEach((cb) => cb());
	while (rafCbs.length) (rafCbs.shift() as () => void)();

	const unfiledRows = () => (els["unfiled-list"] ? els["unfiled-list"].children.filter((c) => c.classList.contains("slide-row")) : []);
	const rowBySlideId = (id: string) => {
		const row = unfiledRows().find((r) => r.dataset.slideId === id);
		if (!row) throw new Error("harness: 未找到 data-slide-id=" + id + " 的行");
		return row;
	};
	return {
		els, created, emitted,
		fetchUrls: () => urls.slice(),
		unfiledRows, rowBySlideId,
		upload: (w.HP_UPLOAD || {}) as Record<string, unknown>,
		storage,
	};
}

async function settle(times = 20) {
	for (let i = 0; i < times; i++) await Promise.resolve();
}

afterEach(() => {
	vi.restoreAllMocks();
	FakeXHR.instances = [];
	const g = globalThis as Record<string, unknown>;
	delete g.window;
	delete g.document;
	delete g.fetch;
	delete g.HP_ViewerCore;
	delete g.XMLHttpRequest;
	delete g.localStorage;
});

describe("slide ID 化前端契约（P2 合同 §5）", () => {
	it("① 同 display_name 两片并存：两行（data-slide-id 区分）、分别打开不串片", async () => {
		const app = bootApp({ idMode: true });
		await settle();
		const rows = app.unfiledRows();
		expect(rows).toHaveLength(2);
		expect(rows.map((r) => r.dataset.slideId).sort()).toEqual([ID_A, ID_B].sort());
		// 主显示 display_name 相同（同名条目用 ID 区分，不改 ID）
		const names = rows.map((r) => (r.querySelector(".slide-name") as FakeEl).innerHTML);
		expect(names[0]).toContain(DISP);
		expect(names[1]).toContain(DISP);

		// 打开 A：info 走 /api/slides/<ID_A>/info；A 行 active
		app.rowBySlideId(ID_A).dispatch("click");
		await settle();
		expect(app.fetchUrls().some((u) => u === "/api/slides/" + ID_A + "/info")).toBe(true);
		expect(app.rowBySlideId(ID_A).classList.contains("active")).toBe(true);
		expect(app.rowBySlideId(ID_B).classList.contains("active")).toBe(false);
		const openedA = app.emitted.filter((e) => e.type === "slide.opened");
		expect(openedA.length).toBe(1);
		expect((openedA[0].payload as { slide: Record<string, unknown> }).slide.id).toBe(ID_A);

		// 打开 B：不串片（info 换 ID_B、事件载荷换 id、active 迁移）
		app.rowBySlideId(ID_B).dispatch("click");
		await settle();
		expect(app.fetchUrls().some((u) => u === "/api/slides/" + ID_B + "/info")).toBe(true);
		expect(app.rowBySlideId(ID_B).classList.contains("active")).toBe(true);
		expect(app.rowBySlideId(ID_A).classList.contains("active")).toBe(false);
		const opened = app.emitted.filter((e) => e.type === "slide.opened");
		expect(opened.map((e) => (e.payload as { slide: Record<string, unknown> }).slide.id))
			.toEqual([ID_A, ID_B]);
	});

	it("② 改名（PATCH 后重拉列表）：ID 不变、行仍指向同片", async () => {
		const slides: Array<Record<string, unknown>> = [
			{ name: NAME_A, slide_id: ID_A, display_name: DISP, width: 1000, height: 800, mpp_x: 0.5, mpp_source: "native" },
		];
		const app = bootApp({ idMode: true, slides });
		await settle();
		const row = app.rowBySlideId(ID_A);
		expect(row.dataset.slideId).toBe(ID_A);
		// 行内编辑：edit 按钮 → 表单（display_name/note）→ 确认
		const editBtn = row.children.find((c) => c.classList.contains("slide-edit"));
		expect(editBtn).toBeTruthy();
		editBtn!.dispatch("click", { stopPropagation() {} });
		await settle();
		const form = row.children.find((c) => c.classList.contains("slide-edit-form"));
		expect(form).toBeTruthy();
		const aInput = form!.children[0] as FakeEl;
		const nInput = form!.children[1] as FakeEl;
		const actions = form!.children[2] as FakeEl;
		const okBtn = actions.children[0] as FakeEl;
		aInput.value = "新名字";
		nInput.value = "备注";
		okBtn.dispatch("click", { stopPropagation() {} });
		await settle(30);
		// ID 通道走 PATCH /api/slides/<slide_id>（display_name/note；旧 meta 端点不动）
		expect(app.fetchUrls().some((u) => u === "/api/slides/" + ID_A)).toBe(true);
		expect(app.fetchUrls().some((u) => u.includes("/api/slide/" + NAME_A + "/meta"))).toBe(false);
		// 模拟服务端已改名后重拉列表（mock slides 是同一数组引用，改后重渲）：
		// 同 slide_id 的行仍在——改名不换 ID、行仍指向同片
		slides[0] = Object.assign({}, slides[0], { display_name: "新名字" });
		const rowsAfter = app.unfiledRows();
		expect(rowsAfter.some((r) => r.dataset.slideId === ID_A)).toBe(true);
	});

	it("③ V1 上传完成按响应 slide_id 打开（ID 通道 info；不按 file.name 猜）", async () => {
		const app = bootApp({ idMode: true });
		await settle();
		const uploadFile = app.upload.uploadFile as (f: unknown) => void;
		uploadFile({ name: "v1.svs", size: 3 });
		await settle();
		expect(FakeXHR.instances).toHaveLength(1);
		FakeXHR.instances[0].simulateLoad(200, JSON.stringify({
			name: "v1.svs", state: "ready", slide_id: "sld_v1done00001",
		}));
		await settle(30);
		// 打开目标 = 响应 slide_id（/api/slides/<id>/info）；不是 file.name
		expect(app.fetchUrls().some((u) => u === "/api/slides/sld_v1done00001/info")).toBe(true);
		expect(app.fetchUrls().some((u) => u === "/api/slide/v1.svs/info")).toBe(false);
		expect(app.fetchUrls().some((u) => u === "/api/slides/v1.svs/info")).toBe(false);
	});

	it("④ 续传键 v3 账户域隔离：换 account 不复用会话", async () => {
		const THRESHOLD = 16 * 1024 * 1024;
		const file = {
			name: "resume.svs",
			size: THRESHOLD,
			lastModified: 1,
			slice() { return { arrayBuffer: async () => new ArrayBuffer(0) }; },
		};
		const auth = (uid: string) => ({
			auth_enabled: true, role: "user", user_id: uid, username: uid + "@x",
		});

		// —— 账户 u1：预置 pt.upload.v3::u1:up-x（filename/size 匹配）→ GET 状态续传
		const st1 = fakeStorage();
		st1.setItem("pt.upload.v3::u1:up-x", JSON.stringify({
			upload_id: "up-x", declared_size: THRESHOLD, chunk_size: 8,
			filename: "resume.svs", slide_id: null,
		}));
		let statusU1 = 0;
		let postedU1 = 0;
		const urlsU1: string[] = [];
		const fetchU1 = ((url: string, opts?: RequestInit) => {
			const u = String(url);
			const method = String((opts && opts.method) || "GET");
			urlsU1.push(method + " " + u);
			if (u === "/api/uploads/up-x" && method === "GET") {
				statusU1++;
				// active 且已确认到末尾 → 直接进 commit（无分片 PUT）
				return jsonResponse({ upload_id: "up-x", state: "active", chunk_size: 8,
					confirmed_offset: THRESHOLD, slide_id: null });
			}
			if (u === "/api/uploads/up-x/commit") {
				return jsonResponse({ state: "ready", upload_id: "up-x", slide_id: "sld_resume0001" });
			}
			if (u === "/api/uploads" && method === "POST") {
				postedU1++;
				return jsonResponse({ upload_id: "up-new", chunk_size: 8, confirmed_offset: THRESHOLD });
			}
			if (u === "/api/uploads/up-new/commit") {
				return jsonResponse({ state: "ready", upload_id: "up-new", slide_id: null });
			}
			if (u.includes("/api/auth/info")) return jsonResponse(auth("u1"));
			return jsonResponse({});
		}) as unknown as typeof fetch;
		const app1 = bootApp({ idMode: true, storage: st1, fetchImpl: fetchU1 });
		const uploadV2 = app1.upload.uploadFileV2 as (f: unknown, r: unknown) => void;
		// 等 initAuth 微任务落地（currentUserId = u1 → uploadAccountScope = "u1"）
		await settle(10);
		uploadV2(file, makeUploadRow());
		await settle(30);
		// u1 域命中：GET /api/uploads/up-x 被查询（复用任务），无新建 POST
		expect(statusU1).toBeGreaterThan(0);
		expect(postedU1).toBe(0);
		// 成功后 u1 域键清理
		expect(st1.getItem("pt.upload.v3::u1:up-x")).toBeNull();

		// —— 账户 u2：同一文件；u1 的 v3 键在 u2 域不可见 → 新建任务
		const st2 = fakeStorage();
		st2.setItem("pt.upload.v3::u1:up-x", JSON.stringify({
			upload_id: "up-x", declared_size: THRESHOLD, chunk_size: 8,
			filename: "resume.svs", slide_id: null,
		}));
		let statusU2 = 0;
		let postedU2 = 0;
		const urlsU2: string[] = [];
		const fetchU2 = ((url: string, opts?: RequestInit) => {
			const u = String(url);
			const method = String((opts && opts.method) || "GET");
			urlsU2.push(method + " " + u);
			if (u === "/api/uploads/up-x" && method === "GET") {
				statusU2++;
				return jsonResponse({ upload_id: "up-x", state: "active", chunk_size: 8,
					confirmed_offset: THRESHOLD, slide_id: null });
			}
			if (u === "/api/uploads" && method === "POST") {
				postedU2++;
				return jsonResponse({ upload_id: "up-y", chunk_size: 8, confirmed_offset: THRESHOLD });
			}
			if (u === "/api/uploads/up-y/commit") {
				// 非确定性失败（500，无稳定码）：任务与 v3 键保留，
				// 便于断言新任务键落在 u2 账户域
				return Promise.resolve({ ok: false, status: 500, clone() { return this; },
					json: () => Promise.resolve({ error: "server error" }) });
			}
			if (u.includes("/api/auth/info")) return jsonResponse(auth("u2"));
			return jsonResponse({});
		}) as unknown as typeof fetch;
		const app2 = bootApp({ idMode: true, storage: st2, fetchImpl: fetchU2 });
		const uploadV2_2 = app2.upload.uploadFileV2 as (f: unknown, r: unknown) => void;
		await settle(10);
		uploadV2_2(file, makeUploadRow());
		await settle(30);
		// u2 域看不到 u1 的记录：不查 /api/uploads/up-x，新建 POST /api/uploads
		expect(statusU2).toBe(0);
		expect(postedU2).toBe(1);
		// 新任务记在 u2 域键下；u1 的旧键原样保留（不被 u2 迁走）
		expect(st2.getItem("pt.upload.v3::u2:up-y")).toBeTruthy();
		expect(st2.getItem("pt.upload.v3::u1:up-x")).toBeTruthy();
	});


	it("⑤ slide_id_api=false（旧后端）→ 回落 name 通道（旧端点 + 载荷无 id）", async () => {
		// 旧后端列表 DTO 无 slide_id/display_name（行键回落名）
		const app = bootApp({ idMode: false, slides: [
			{ name: NAME_A, width: 1000, height: 800, mpp_x: 0.5, mpp_source: "native" },
			{ name: NAME_B, width: 2000, height: 1600, mpp_x: 0.25, mpp_source: "native" },
		] });  // 无 bootstrap：能力缺省 false
		await settle();
		// 列表无 slide_id（旧后端 DTO）→ 行键回落名
		const rows = app.unfiledRows();
		expect(rows.map((r) => r.dataset.slideId)).toEqual([NAME_A, NAME_B]);
		const rowA = rows.find((r) => r.dataset.slideId === NAME_A) as FakeEl;
		rowA.dispatch("click");
		await settle();
		// 读端点走旧 /api/slide/<name>/...；标注查询 ?slide=<名>
		expect(app.fetchUrls().some((u) => u === "/api/slide/" + NAME_A + "/info")).toBe(true);
		expect(app.fetchUrls().some((u) => u === "/api/annotations?slide=" + encodeURIComponent(NAME_A))).toBe(true);
		expect(app.fetchUrls().some((u) => u.includes("/api/slides/"))).toBe(false);
		const opened = app.emitted.filter((e) => e.type === "slide.opened");
		expect(opened).toHaveLength(1);
		const slide = (opened[0].payload as { slide: Record<string, unknown> }).slide;
		expect(slide.id).toBeNull();
		expect(slide.name).toBe(NAME_A);
	});

	it("⑥ ?slide=<slide_id> URL 通道：加载即打开对应切片", async () => {
		const app = bootApp({ idMode: true, search: "?slide=" + ID_B });
		await settle();
		// 未点击任何行：URL 参数驱动 openSlide（ID 通道 info）
		expect(app.fetchUrls().some((u) => u === "/api/slides/" + ID_B + "/info")).toBe(true);
		const opened = app.emitted.filter((e) => e.type === "slide.opened");
		expect(opened).toHaveLength(1);
		expect((opened[0].payload as { slide: Record<string, unknown> }).slide.id).toBe(ID_B);
		// 高亮落在 data-slide-id=ID_B 的行
		expect(app.rowBySlideId(ID_B).classList.contains("active")).toBe(true);
	});
});

/** 最小上传进度行（setStage/finish/markError 桩） */
function makeUploadRow(_created: FakeEl[]): Record<string, unknown> {
	return {
		setStage() {},
		markError() {},
		finish() {},
		_row: fakeEl("upload-item"),
	};
}
