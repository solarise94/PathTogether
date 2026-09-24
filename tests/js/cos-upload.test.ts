/**
 * COS 直传 Phase 4 前端适配器（docs/cos-direct-upload-audit-plan.md §5/§10
 * Phase 4 + docs/upload-routing-open-source-review.md D3/D6/D7/D8/D10）。
 *
 * 用 loadApp harness（同 upload-v2.test.ts / upload-csrf.test.ts）驱动**真实**
 * app.js，锁定：
 *  1. 选路：capability off → 手动开关不渲染、uploadFile 走旧路径；开 + 勾选 +
 *     eligible（白名单格式/非 ZIP/MRXS/大小在准入内）→ uploadFileCos；
 *  2. COS PUT 独立传输：对 COS URL 的 fetch 不带 X-CSRF-Token、
 *     credentials:"omit"、mode:"cors"；控制 API（/api/ingestions*）经 apiFetch
 *     带 CSRF 双提交头；
 *  3. 分批签名（sign_batch_max_parts）+ 批内并发（max_concurrent_parts）+
 *     分块进度（confirmed/total，仅上传阶段）；单片失败重试后成功；
 *  4. 422 cos_exceeds_admission → 行内说明 + 「改用平台上传」显式按钮触发
 *     平台路径（用户明确选择，非 D8 禁止的自动换路）；
 *  5. waiting_capacity：排队位置展示（无 ETA）、fake timers 推进 5s 轮询、
 *     preparing→uploading 继续；
 *  6. 刷新恢复：localStorage pt.cos.jobs 预置未完任务 → 只读进度行 + 轮询；
 *     终态清理；
 *  7. i18n：upload.cos.* 键 zh/en 均存在，stage → 文案键映射，
 *     app.js _EXTRA_I18N 兜底表同步。
 */
import { afterEach, describe, expect, it, vi } from "vitest";
import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const appSrc = readFileSync(resolve(here, "../../static/app.js"), "utf8");
const i18nSrc = readFileSync(resolve(here, "../../static/i18n.js"), "utf8");

const THRESHOLD = 16 * 1024 * 1024;

/** 可记录 children / 事件监听 / insertBefore 的元素 stub */
function el(tag?: string) {
	const children: ReturnType<typeof el>[] = [];
	const listeners: Record<string, Array<(ev?: unknown) => void>> = {};
	const node: Record<string, unknown> = {
		tagName: (tag || "div").toUpperCase(),
		hidden: true,
		textContent: "",
		innerHTML: "",
		value: "",
		disabled: false,
		checked: false,
		type: "",
		title: "",
		className: "",
		style: {},
		classList: { add() {}, remove() {}, contains() { return false; } },
		children,
		appendChildren: children,
		parentNode: null,
		appendChild(c: ReturnType<typeof el>) { children.push(c); return c; },
		removeChild(c: ReturnType<typeof el>) {
			const i = children.indexOf(c);
			if (i >= 0) children.splice(i, 1);
			return c;
		},
		insertBefore(c: ReturnType<typeof el>, ref: ReturnType<typeof el> | null) {
			const i = ref ? children.indexOf(ref) : -1;
			if (i < 0) children.push(c);
			else children.splice(i, 0, c);
			inserts.push({ node: c, ref });
			return c;
		},
		addEventListener(type: string, fn: (ev?: unknown) => void) {
			(listeners[type] = listeners[type] || []).push(fn);
		},
		removeEventListener() {},
		setAttribute() {},
		getAttribute() { return null; },
		focus() {},
		click() { (listeners["click"] || []).forEach((f) => f({ preventDefault() {} })); },
	};
	const inserts: Array<{ node: unknown; ref: unknown }> = [];
	(node as { _inserts: typeof inserts })._inserts = inserts;
	(node as { _listeners: typeof listeners })._listeners = listeners;
	return node as ReturnType<typeof el> & {
		_inserts: Array<{ node: unknown; ref: unknown }>;
		_listeners: Record<string, Array<(ev?: unknown) => void>>;
	};
}

/** 触发元素上记录的某类事件（点击按钮等） */
function fire(node: ReturnType<typeof el>, type: string, ev?: unknown) {
	(node._listeners[type] || []).forEach((f) => f(ev));
}

function toastContainer() {
	const messages: string[] = [];
	return {
		messages,
		appendChild(child: { textContent?: string }) {
			messages.push(String(child && child.textContent));
		},
		addEventListener() {},
	};
}

class FakeXHR {
	static instances: FakeXHR[] = [];
	open = vi.fn();
	setRequestHeader = vi.fn();
	send = vi.fn();
	status = 0;
	responseText = "";
	private listeners: Record<string, () => void> = {};
	upload = { addEventListener() {} };

	constructor() {
		FakeXHR.instances.push(this);
	}
	addEventListener(type: string, cb: () => void) {
		this.listeners[type] = cb;
	}
	simulateLoad(status: number, body: string) {
		this.status = status;
		this.responseText = body;
		this.listeners["load"] && this.listeners["load"]();
	}
}

function fakeLocalStorage() {
	const map = new Map<string, string>();
	return {
		getItem: (k: string) => (map.has(k) ? map.get(k)! : null),
		setItem: (k: string, v: string) => { map.set(k, String(v)); },
		removeItem: (k: string) => { map.delete(k); },
		clear: () => map.clear(),
		_dump: map,
	};
}

type FetchCall = { method: string; url: string; opts: Record<string, unknown> & { headers?: Record<string, string> } };

/** COS capability payload（照 app.py _cos_upload_capability_payload 形态） */
function cosCaps(overrides?: Record<string, unknown>) {
	return Object.assign({
		available: true,
		manual_only: true,
		formats: ["bif", "ndpi", "svs", "tif"],
		max_size_bytes: 1000,
		part_bytes: 8,
		url_ttl_seconds: 600,
		max_concurrent_parts: 2,
		sign_batch_max_parts: 2,
		policy_version: "v1-manual",
	}, overrides || {});
}

function loadApp(fetchImpl?: typeof fetch, bootstrap?: unknown) {
	const storage = fakeLocalStorage();
	const els: Record<string, ReturnType<typeof el | typeof toastContainer>> = {};
	const toast = toastContainer();
	els["toast-container"] = toast as never;
	const container = el();                    // #upload-progress-list（上传行容器）
	const toggleParent = el();                 // 容器的父节点（开关 insertBefore 目标）
	container.parentNode = toggleParent;
	els["upload-progress-list"] = container as never;
	const loc = { href: "http://local/", pathname: "/" };
	const theFetch = fetchImpl || (vi.fn(() => Promise.resolve({
		ok: true, status: 200, clone() { return this; },
		json: () => Promise.resolve({}),
	})) as unknown as typeof fetch);
	const w: Record<string, unknown> = {
		HP_I18N: {
			t: (k: string, vars?: Record<string, unknown>) =>
				(vars && Object.keys(vars).length) ? `${k}:${Object.values(vars).join(",")}` : k,
			getLang: () => "zh",
		},
		fetch: theFetch,
		location: loc,
		OpenSeadragon: undefined,
		confirm: vi.fn(() => true),
	};
	if (bootstrap !== undefined) w.HP_APP_BOOTSTRAP = bootstrap;
	const doc = {
		readyState: "loading",
		cookie: "csrf_token=tok",
		getElementById(id: string) {
			if (!els[id]) els[id] = el();
			return els[id];
		},
		createElement(tag: string) { return el(tag); },
		addEventListener() {},
		querySelector() { return el(); },
		querySelectorAll() { return []; },
	};
	(w as { document: typeof doc }).document = doc;
	(globalThis as { document: typeof doc }).document = doc;
	(globalThis as { window: typeof w }).window = w;
	(globalThis as { fetch: typeof fetch }).fetch = theFetch;
	(globalThis as { location: typeof loc }).location = loc;
	(globalThis as { localStorage: typeof storage }).localStorage = storage;
	vi.stubGlobal("XMLHttpRequest", FakeXHR);
	vi.stubGlobal("localStorage", storage);
	new Function("window", "document", "fetch", "location", appSrc)(w, doc, theFetch, loc);
	const up = w.HP_UPLOAD as Record<string, unknown>;
	return {
		up: up as {
			uploadFile: (f: unknown, opts?: { platform?: boolean; cosRetry?: string }) => void;
			cosUploadEligible: (f: unknown) => boolean;
			resolveCosConfig: () => unknown;
			cosStageKey: (s: string) => string;
			setCosManual: (on: boolean) => void;
			isCosManual: () => boolean;
			initCosUploadUi: () => void;
			restoreCosJobs: () => void;
		},
		fetchCalls: () => (((theFetch as unknown as { mock?: { calls: unknown[][] } }).mock)
			? ((theFetch as unknown as vi.Mock).mock.calls as unknown as [string, Record<string, unknown>?][]).map(
				([url, opts]) => ({
					method: String((opts && opts.method) || "GET"),
					url: String(url),
					opts: (opts || {}) as FetchCall["opts"],
				}))
			: [] as FetchCall[]),
		container,
		toggleParent,
		toastMessages: toast.messages,
		storage,
		confirmMock: w.confirm as ReturnType<typeof vi.fn>,
	};
}

/** Response 形 stub（含可读 ETag 头） */
function resp(body: unknown, status = 200) {
	return {
		ok: status >= 200 && status < 300,
		status,
		clone() { return this; },
		json: () => Promise.resolve(body),
		headers: { get: (h: string) => (h.toLowerCase() === "etag" ? '"etag-x"' : null) },
	} as unknown as Response;
}

function cosFile(size = 30, name = "big.svs") {
	return {
		name,
		size,
		lastModified: 42,
		slice(s: number, e: number) {
			return { _offset: s, _end: e };
		},
	};
}

const tick = () => new Promise((r) => setTimeout(r, 0));
async function flush(n = 10) {
	for (let i = 0; i < n; i++) await tick();
}

afterEach(() => {
	vi.useRealTimers();
	vi.unstubAllGlobals();
	FakeXHR.instances = [];
});

// --------------------------------------------------------------------------- #
// 1. 选路（manual_only：开关默认关；capability off 无 COS 痕迹）
// --------------------------------------------------------------------------- #
describe("选路：capability / 手动开关 / eligible 判定", () => {
	it("cos_upload 未下发（off）→ 开关不渲染；uploadFile 走旧路径（legacy/V2）", () => {
		const h = loadApp(undefined, { mode: "official", capabilities: { upload_v2_threshold_bytes: THRESHOLD } });
		expect(h.up.resolveCosConfig()).toBeNull();
		h.up.initCosUploadUi();
		expect(h.toggleParent._inserts.length).toBe(0);   // 开关不渲染
		// 小文件 → legacy XHR；即使手动状态被置开也回退平台路径（config 为 null）
		h.up.setCosManual(true);
		h.up.uploadFile({ name: "a.svs", size: 3 });
		expect(FakeXHR.instances).toHaveLength(1);
		expect(FakeXHR.instances[0].open).toHaveBeenCalledWith("POST", "/api/upload");
		expect(h.fetchCalls().some((c) => c.url === "/api/ingestions")).toBe(false);
	});

	it("available:false / 缺字段 / 结构非法 → resolveCosConfig() 一律 null", () => {
		const mk = (caps: unknown) => loadApp(undefined, { mode: "official", capabilities: caps });
		expect(mk({ cos_upload: { available: false, manual_only: true, formats: ["svs"] } }).up.resolveCosConfig()).toBeNull();
		expect(mk({ cos_upload: { available: true } }).up.resolveCosConfig()).toBeNull();
		expect(mk({ cos_upload: cosCaps({ max_size_bytes: "garbage" }) }).up.resolveCosConfig()).toBeNull();
		expect(mk({ cos_upload: cosCaps({ formats: [] }) }).up.resolveCosConfig()).toBeNull();
		expect(mk({ cos_upload: cosCaps({ sign_batch_max_parts: 0 }) }).up.resolveCosConfig()).toBeNull();
		expect(mk({}).up.resolveCosConfig()).toBeNull();
	});

	it("capability 可用 → 渲染手动开关；勾选 + eligible → POST /api/ingestions（CSRF），不走 /api/uploads", async () => {
		// 创建后即 terminal（失败终态）让链路尽快收口，只验证选路
		const fetchImpl = vi.fn((url: string, opts?: RequestInit) => {
			if (url === "/api/ingestions" && opts && opts.method === "POST") {
				return Promise.resolve(resp({ job_id: "inj_1", state: "failed", stage: "terminal", fail_code: "demo" }, 202));
			}
			return Promise.resolve(resp({ job_id: "inj_1", stage: "terminal", fail_code: "demo" }));
		}) as unknown as typeof fetch;
		const h = loadApp(fetchImpl, { mode: "official", capabilities: { cos_upload: cosCaps() } });
		h.up.initCosUploadUi();
		expect(h.toggleParent._inserts.length).toBe(1);   // 开关渲染在进度行容器前
		expect(h.up.isCosManual()).toBe(false);           // 默认关
		h.up.uploadFile(cosFile());
		expect(h.fetchCalls().some((c) => c.url === "/api/ingestions")).toBe(false);  // 未勾选 → 平台
		FakeXHR.instances.length = 0;   // 第一次是平台 legacy 上传，清掉再验 COS 路径
		h.up.setCosManual(true);
		h.up.uploadFile(cosFile());
		await flush(8);
		const create = h.fetchCalls().find((c) => c.url === "/api/ingestions" && c.method === "POST");
		expect(create).toBeTruthy();
		expect((create!.opts.headers as Record<string, string>)["X-CSRF-Token"]).toBe("tok");
		expect(h.fetchCalls().some((c) => c.url === "/api/uploads" && c.method === "POST")).toBe(false);
		expect(FakeXHR.instances).toHaveLength(0);
	});

	it("ZIP/MRXS/超大/零字节 → 不走 COS（D10/D11；即便手动开着）", () => {
		const h = loadApp(undefined, { mode: "official", capabilities: { cos_upload: cosCaps() } });
		h.up.setCosManual(true);
		expect(h.up.cosUploadEligible(cosFile(30))).toBe(true);
		expect(h.up.cosUploadEligible({ name: "a.zip", size: 30 })).toBe(false);
		expect(h.up.cosUploadEligible({ name: "a.mrxs", size: 30 })).toBe(false);
		expect(h.up.cosUploadEligible({ name: "a.kfb", size: 30 })).toBe(false);       // 白名单外
		expect(h.up.cosUploadEligible({ name: "a.svs", size: 0 })).toBe(false);
		expect(h.up.cosUploadEligible({ name: "a.svs", size: 1001 })).toBe(false);     // 超准入上限
		h.up.uploadFile({ name: "a.zip", size: 30 });
		h.up.uploadFile({ name: "a.mrxs", size: 30 });
		h.up.uploadFile({ name: "huge.svs", size: 5000 });
		expect(FakeXHR.instances).toHaveLength(3);       // 全部 legacy（小文件）
		FakeXHR.instances.forEach((x) => expect(x.open).toHaveBeenCalledWith("POST", "/api/upload"));
		expect(h.fetchCalls().some((c) => c.url === "/api/ingestions")).toBe(false);
	});
});

// --------------------------------------------------------------------------- #
// 2 + 3. 独立传输 / 分批签名 + 并发 + 进度 / upload-complete
// --------------------------------------------------------------------------- #
describe("COS 上传状态机：独立传输、分批签名、并发、进度、完成", () => {
	it("COS PUT 无 CSRF 且 credentials:omit；控制 API 带 CSRF；两批签名 + 批内并发 + 100% 后 upload-complete", async () => {
		vi.useFakeTimers();
		const parts = [
			{ part_number: 1, length: 8 }, { part_number: 2, length: 8 },
			{ part_number: 3, length: 8 }, { part_number: 4, length: 6 },
		];
		const afterComplete = ["awaiting_server", "downloading", "viewable"];
		let getStatus = 0;
		// complete 后第一次 GET 挂起：先断言上传阶段 100% 中间态再放行
		let releaseServerStage: (() => void) | null = null;
		const fetchImpl = vi.fn((url: string, opts?: RequestInit) => {
			const method = String((opts && opts.method) || "GET");
			if (url === "/api/ingestions" && method === "POST") {
				return Promise.resolve(resp({ job_id: "inj_9", state: "uploading", stage: "uploading", declared_size: 30 }, 202));
			}
			if (url === "/api/ingestions/inj_9" && method === "GET") {
				if (getStatus++ === 0) {
					return Promise.resolve(resp({ job_id: "inj_9", stage: "uploading", declared_size: 30, total_parts: 4, parts }));
				}
				if (getStatus === 2) {
					return new Promise((resolve) => {
						releaseServerStage = () => resolve(resp({ job_id: "inj_9", stage: "awaiting_server", declared_size: 30 }));
					});
				}
				const idx = Math.min(getStatus - 2, afterComplete.length - 1);
				const stage = afterComplete[idx];
				const body: Record<string, unknown> = { job_id: "inj_9", stage, declared_size: 30 };
				if (stage === "downloading") body.downloaded_bytes = 15;
				if (stage === "viewable") body.slide = "big.svs";
				return Promise.resolve(resp(body));
			}
			if (url === "/api/ingestions/inj_9/parts/sign" && method === "POST") {
				const nums = JSON.parse(String(opts!.body)).part_numbers as number[];
				return Promise.resolve(resp({
					job_id: "inj_9", upload_id: "up-1", transport: "presign_parts",
					urls: nums.map((n) => ({
						url: "https://bucket-appid.cos.ap-shanghai.myqcloud.com/incoming/o?uploadId=up-1&partNumber=" + n,
						part_number: n, content_length: 8, expires_in: 600,
					})),
				}));
			}
			if (url === "/api/ingestions/inj_9/upload-complete" && method === "POST") {
				return Promise.resolve(resp({ job_id: "inj_9", state: "completing", stage: "awaiting_server" }, 202));
			}
			if (url.startsWith("https://")) return Promise.resolve(resp({}));
			return Promise.resolve(resp({}));
		}) as unknown as typeof fetch;
		const h = loadApp(fetchImpl, { mode: "official", capabilities: { cos_upload: cosCaps() } });
		h.up.setCosManual(true);
		const file = cosFile(30);
		h.up.uploadFile(file);
		await vi.advanceTimersByTimeAsync(0);

		const calls = h.fetchCalls;
		// ① 独立传输：COS URL 的 PUT 不带 CSRF / 不带 Cookie / cors 模式
		const puts = calls().filter((c) => c.url.startsWith("https://"));
		expect(puts).toHaveLength(4);
		puts.forEach((c) => {
			expect(c.method).toBe("PUT");
			expect(c.opts.credentials).toBe("omit");
			expect(c.opts.mode).toBe("cors");
			expect(!(c.opts.headers && c.opts.headers["X-CSRF-Token"])).toBe(true);
		});
		// ② 控制 API（apiFetch 语义）：创建/签名/完成都带双提交头
		["/api/ingestions", "/api/ingestions/inj_9/parts/sign", "/api/ingestions/inj_9/upload-complete"]
			.forEach((u) => {
				const call = calls().find((c) => c.url === u && c.method === "POST");
				expect(call, u).toBeTruthy();
				expect((call!.opts.headers as Record<string, string>)["X-CSRF-Token"]).toBe("tok");
			});
		// ③ 分批（sign_batch_max_parts=2）+ 批内并发（max_concurrent_parts=2）：
		//    批 1 的两个 PUT 都发生在批 2 签名之前
		const signCalls = calls().filter((c) => c.url.endsWith("/parts/sign"));
		expect(signCalls).toHaveLength(2);
		expect(JSON.parse(String((signCalls[0].opts as { body: string }).body)).part_numbers).toEqual([1, 2]);
		expect(JSON.parse(String((signCalls[1].opts as { body: string }).body)).part_numbers).toEqual([3, 4]);
		const seq = calls().map((c) => (c.url.startsWith("https://") ? "PUT" : c.url));
		const sign1 = seq.indexOf("/api/ingestions/inj_9/parts/sign");
		const sign2 = seq.indexOf("/api/ingestions/inj_9/parts/sign", sign1 + 1);
		const putCountBetween = seq.slice(sign1, sign2).filter((s) => s === "PUT").length;
		expect(putCountBetween).toBe(2);
		// ④ upload-complete 已被调（全部 confirmed 之后）
		expect(calls().some((c) => c.url === "/api/ingestions/inj_9/upload-complete")).toBe(true);
		// ⑤ 进度：上传阶段百分比（confirmed/total，仅上传阶段）
		const row = h.container.appendChildren[h.container.appendChildren.length - 1];
		const statusEl = row.children[2];
		expect(String(statusEl.textContent)).toContain("正在上传");
		expect(String(statusEl.textContent)).toContain("100%");
		// 恢复记录：confirmed 全量落 localStorage（非秘密：无签名 URL/凭证）
		const saved = JSON.parse(h.storage.getItem("pt.cos.jobs") || "[]");
		expect(saved[0] && saved[0].confirmed).toEqual([1, 2, 3, 4]);
		expect(String(JSON.stringify(saved))).not.toContain("myqcloud");
		// ⑥ 放行服务端阶段轮询：awaiting_server → downloading → viewable
		expect(releaseServerStage).toBeTruthy();
		releaseServerStage!();
		await vi.advanceTimersByTimeAsync(0);
		expect(String(statusEl.textContent)).toContain("等待服务器接收");
		await vi.advanceTimersByTimeAsync(2000);   // awaiting_server → downloading
		expect(String(statusEl.textContent)).toContain("服务器接收中");
		expect(String(statusEl.textContent)).toContain("50%");
		await vi.advanceTimersByTimeAsync(2000);   // downloading → viewable
		await vi.advanceTimersByTimeAsync(0);
		expect(h.toastMessages.some((m) => m.indexOf("upload.done") >= 0)).toBe(true);
		expect(JSON.parse(h.storage.getItem("pt.cos.jobs") || "[]")).toEqual([]);
	});

	it("单片失败：同 URL 重试后成功（不触发重新签名分支、不影响其余分块）", async () => {
		const parts = [
			{ part_number: 1, length: 8 }, { part_number: 2, length: 8 },
			{ part_number: 3, length: 8 }, { part_number: 4, length: 6 },
		];
		let getStatus = 0;
		let part2Fails = 1;   // part 2 首次 PUT 500，重试成功
		const fetchImpl = vi.fn((url: string, opts?: RequestInit) => {
			const method = String((opts && opts.method) || "GET");
			if (url === "/api/ingestions" && method === "POST") {
				return Promise.resolve(resp({ job_id: "inj_f", state: "uploading", stage: "uploading", declared_size: 30 }, 202));
			}
			if (url === "/api/ingestions/inj_f" && method === "GET") {
				if (getStatus++ === 0) return Promise.resolve(resp({ stage: "uploading", total_parts: 4, parts }));
				return Promise.resolve(resp({ stage: "viewable", slide: "big.svs" }));
			}
			if (url === "/api/ingestions/inj_f/parts/sign" && method === "POST") {
				const nums = JSON.parse(String(opts!.body)).part_numbers as number[];
				return Promise.resolve(resp({
					urls: nums.map((n) => ({ url: "https://cos.example/incoming/o?partNumber=" + n, part_number: n })),
				}));
			}
			if (url === "/api/ingestions/inj_f/upload-complete" && method === "POST") {
				return Promise.resolve(resp({ stage: "awaiting_server" }, 202));
			}
			if (url.startsWith("https://")) {
				const n = Number(new URL(url).searchParams.get("partNumber"));
				if (n === 2 && part2Fails-- > 0) return Promise.resolve(resp({}, 500));
				return Promise.resolve(resp({}));
			}
			return Promise.resolve(resp({}));
		}) as unknown as typeof fetch;
		const h = loadApp(fetchImpl, { mode: "official", capabilities: { cos_upload: cosCaps() } });
		h.up.setCosManual(true);
		h.up.uploadFile(cosFile(30));
		await flush(12);
		// 重试延迟 600ms（真实定时器）后 part 2 成功 → 全部 confirmed → complete → viewable
		await new Promise((r) => setTimeout(r, 800));
		await flush(12);
		const puts = h.fetchCalls().filter((c) => c.url.startsWith("https://"));
		expect(puts).toHaveLength(5);   // 4 片 + part2 重试一次
		expect(h.fetchCalls().some((c) => c.url === "/api/ingestions/inj_f/upload-complete")).toBe(true);
		expect(h.toastMessages.some((m) => m.indexOf("upload.done") >= 0)).toBe(true);
		// 失败重试不累计字节：进度按唯一 confirmed 分块计（viewable 后行文案
		// 已切「入库完成」，以恢复记录为准）
		const saved = JSON.parse(h.storage.getItem("pt.cos.jobs") || "[]");
		expect(saved).toEqual([]);   // 成功后清理
	});
});

// --------------------------------------------------------------------------- #
// 4. 422 cos_exceeds_admission → 说明 + 「改用平台上传」显式按钮
// --------------------------------------------------------------------------- #
describe("422 cos_exceeds_admission：显式平台重试（非自动换路）", () => {
	it("row 显示可读说明 + 按钮；点击后走平台路径（legacy /api/upload）", async () => {
		const fetchImpl = vi.fn((url: string, opts?: RequestInit) => {
			if (url === "/api/ingestions" && opts && opts.method === "POST") {
				return Promise.resolve(resp({
					code: "cos_exceeds_admission", max_size_bytes: 900000, fallback_transport: "v2",
				}, 422));
			}
			return Promise.resolve(resp({}));
		}) as unknown as typeof fetch;
		const h = loadApp(fetchImpl, { mode: "official", capabilities: { cos_upload: cosCaps() } });
		h.up.setCosManual(true);
		const file = cosFile(30, "small.svs");
		h.up.uploadFile(file);
		await flush(12);
		// 可读文案（稳定码不透出原文）
		expect(h.toastMessages.some((m) => m.indexOf("文件超过云端直传大小上限") >= 0)).toBe(true);
		// 行上出现「改用平台上传」按钮
		const row = h.container.appendChildren[h.container.appendChildren.length - 1];
		const btns = row.children.filter((c) => String(c.tagName) === "BUTTON");
		const retry = btns.find((b) => b.textContent === "改用平台上传");
		expect(retry).toBeTruthy();
		// 点击 → 全新平台任务（小文件 → legacy XHR，带 CSRF）
		expect(FakeXHR.instances).toHaveLength(0);
		fire(retry!, "click", { preventDefault() {} });
		await flush(4);
		expect(FakeXHR.instances).toHaveLength(1);
		expect(FakeXHR.instances[0].open).toHaveBeenCalledWith("POST", "/api/upload");
		// 没有第二次 COS 创建（不在 COS 路径内自动重试/换路）
		expect(h.fetchCalls().filter((c) => c.url === "/api/ingestions").length).toBe(1);
	});
});

// --------------------------------------------------------------------------- #
// 5. waiting_capacity：排队位置 + 5s 轮询 + 准入后继续
// --------------------------------------------------------------------------- #
describe("waiting_capacity：排队展示与轮询推进", () => {
	it("等待期间显示阶段+排队位置（无 ETA）；5s 轮询 → preparing/uploading 继续 → 完成", async () => {
		vi.useFakeTimers();
		const parts = [{ part_number: 1, length: 8 }, { part_number: 2, length: 8 }];
		let getStatus = 0;
		const fetchImpl = vi.fn((url: string, opts?: RequestInit) => {
			const method = String((opts && opts.method) || "GET");
			if (url === "/api/ingestions" && method === "POST") {
				return Promise.resolve(resp({
					job_id: "inj_w", state: "waiting_capacity", stage: "waiting_space",
					code: "cos_waiting_capacity", queue_position: 0, status_url: "/api/ingestions/inj_w",
				}, 202));
			}
			if (url === "/api/ingestions/inj_w" && method === "GET") {
				if (getStatus === 0) {
					getStatus++;
					return Promise.resolve(resp({ stage: "waiting_space", queue_position: 0 }));
				}
				if (getStatus === 1) {
					getStatus++;
					return Promise.resolve(resp({ stage: "uploading", total_parts: 2, parts }));
				}
				return Promise.resolve(resp({ stage: "viewable", slide: "big.svs" }));
			}
			if (url === "/api/ingestions/inj_w/parts/sign" && method === "POST") {
				return Promise.resolve(resp({
					urls: [{ url: "https://cos.example/incoming/o?partNumber=1", part_number: 1 },
						{ url: "https://cos.example/incoming/o?partNumber=2", part_number: 2 }],
				}));
			}
			if (url === "/api/ingestions/inj_w/upload-complete" && method === "POST") {
				return Promise.resolve(resp({ stage: "awaiting_server" }, 202));
			}
			if (url.startsWith("https://")) return Promise.resolve(resp({}));
			return Promise.resolve(resp({}));
		}) as unknown as typeof fetch;
		const h = loadApp(fetchImpl, { mode: "official", capabilities: { cos_upload: cosCaps() } });
		h.up.setCosManual(true);
		h.up.uploadFile(cosFile(16));
		await vi.advanceTimersByTimeAsync(0);
		const row = h.container.appendChildren[h.container.appendChildren.length - 1];
		const statusEl = row.children[2];
		// 排队位置展示（0 基 → 第 1 位）；无预计时间（不出现 eta 字样）
		expect(String(statusEl.textContent)).toContain("等待暂存空间");
		expect(String(statusEl.textContent)).toContain("upload.cos.queue:1");
		expect(String(statusEl.textContent)).not.toContain("eta");
		// 5s 轮询推进：第一跳后仍在等待（第二次 GET 仍 waiting），继续放行到 uploading
		await vi.advanceTimersByTimeAsync(5000);
		await vi.advanceTimersByTimeAsync(5000);
		expect(h.fetchCalls().filter((c) => c.url === "/api/ingestions/inj_w" && c.method === "GET").length)
			.toBeGreaterThanOrEqual(3);
		// 准入后拿到分块计划 → 签名 + PUT → 完成
		expect(h.fetchCalls().some((c) => c.url === "/api/ingestions/inj_w/parts/sign")).toBe(true);
		expect(h.fetchCalls().filter((c) => c.url.startsWith("https://"))).toHaveLength(2);
		await vi.advanceTimersByTimeAsync(0);
		expect(h.toastMessages.some((m) => m.indexOf("upload.done") >= 0)).toBe(true);
	});
});

// --------------------------------------------------------------------------- #
// 6. 刷新恢复：localStorage pt.cos.jobs
// --------------------------------------------------------------------------- #
describe("刷新恢复：只读进度行与终态清理", () => {
	it("预置未完任务 → 出现只读进度行（阶段+下载进度+续传提示）；终态后清理记录", async () => {
		vi.useFakeTimers();
		let stage = "downloading";
		const fetchImpl = vi.fn((url: string) => {
			if (url === "/api/ingestions/inj_r") {
				if (stage === "downloading") {
					return Promise.resolve(resp({ stage: "downloading", downloaded_bytes: 10, declared_size: 30 }));
				}
				if (stage === "uploading") {
					return Promise.resolve(resp({ stage: "uploading", total_parts: 4, parts: [] }));
				}
				return Promise.resolve(resp({ stage: "terminal", fail_code: "waiting_timeout" }));
			}
			return Promise.resolve(resp({}));
		}) as unknown as typeof fetch;
		const h = loadApp(fetchImpl, { mode: "official", capabilities: { cos_upload: cosCaps() } });
		h.storage.setItem("pt.cos.jobs", JSON.stringify([
			{ job_id: "inj_r", filename: "big.svs", size: 30, confirmed: [1, 2] },
		]));
		h.up.restoreCosJobs();
		await vi.advanceTimersByTimeAsync(0);
		// 只读进度行出现（不重发分块、不创建新任务）
		expect(h.container.appendChildren).toHaveLength(1);
		const row = h.container.appendChildren[0];
		expect(String(row.children[0].textContent)).toContain("big.svs");
		expect(String(row.children[2].textContent)).toContain("服务器接收中");
		expect(String(row.children[2].textContent)).toContain("33%");
		// uploading 未完成 → 提示重选同名文件可续传
		stage = "uploading";
		await vi.advanceTimersByTimeAsync(3000);
		expect(String(row.children[2].textContent)).toContain("重新选择同名文件可续传");
		// 终态 → row 失败 + 本地记录清理
		stage = "terminal";
		await vi.advanceTimersByTimeAsync(3000);
		expect(String(row.children[2].textContent)).toContain("上传失败");
		expect(JSON.parse(h.storage.getItem("pt.cos.jobs") || "[]")).toEqual([]);
	});

	it("重选同名同大小文件 + 存在未完任务 → 询问后续传（跳过已确认分块）", async () => {
		const parts = [
			{ part_number: 1, length: 8 }, { part_number: 2, length: 8 },
			{ part_number: 3, length: 8 }, { part_number: 4, length: 6 },
		];
		let getStatus = 0;
		const signedNums: number[][] = [];
		const fetchImpl = vi.fn((url: string, opts?: RequestInit) => {
			const method = String((opts && opts.method) || "GET");
			if (url === "/api/ingestions" && method === "POST") {
				return Promise.resolve(resp({ job_id: "inj_new", stage: "uploading" }, 202));
			}
			if (url === "/api/ingestions/inj_r" && method === "GET") {
				if (getStatus++ === 0) return Promise.resolve(resp({ stage: "uploading", total_parts: 4, parts }));
				return Promise.resolve(resp({ stage: "viewable", slide: "big.svs" }));
			}
			if (url === "/api/ingestions/inj_r/parts/sign" && method === "POST") {
				const nums = JSON.parse(String(opts!.body)).part_numbers as number[];
				signedNums.push(nums);
				return Promise.resolve(resp({
					urls: nums.map((n) => ({ url: "https://cos.example/incoming/o?partNumber=" + n, part_number: n })),
				}));
			}
			if (url === "/api/ingestions/inj_r/upload-complete" && method === "POST") {
				return Promise.resolve(resp({ stage: "awaiting_server" }, 202));
			}
			if (url.startsWith("https://")) return Promise.resolve(resp({}));
			return Promise.resolve(resp({}));
		}) as unknown as typeof fetch;
		const h = loadApp(fetchImpl, { mode: "official", capabilities: { cos_upload: cosCaps() } });
		h.storage.setItem("pt.cos.jobs", JSON.stringify([
			{ job_id: "inj_r", filename: "big.svs", size: 30, confirmed: [1, 2] },
		]));
		h.confirmMock.mockReturnValue(true);   // 用户确认续传
		h.up.setCosManual(true);
		h.up.uploadFile(cosFile(30, "big.svs"));
		await flush(12);
		// 不创建新任务（无 POST /api/ingestions），只签未确认分块 [3,4]
		expect(h.fetchCalls().some((c) => c.url === "/api/ingestions" && c.method === "POST")).toBe(false);
		expect(h.confirmMock).toHaveBeenCalled();
		expect(signedNums).toEqual([[3, 4]]);
		expect(h.fetchCalls().some((c) => c.url === "/api/ingestions/inj_r/upload-complete")).toBe(true);
		expect(h.toastMessages.some((m) => m.indexOf("upload.done") >= 0)).toBe(true);
	});
});

// --------------------------------------------------------------------------- #
// 7. i18n：键存在性（zh/en）+ stage 映射 + _EXTRA_I18N 兜底同步
// --------------------------------------------------------------------------- #
describe("i18n：upload.cos.* 键（zh/en）与 stage 映射", () => {
	const STAGE_KEYS = ["waiting_space", "uploading", "awaiting_server", "downloading", "validating", "readiness", "viewable"];
	const OTHER_KEYS = [
		"upload.cos.toggle", "upload.cos.toggle.tip", "upload.cos.queue", "upload.cos.cancel",
		"upload.cos.cancelled", "upload.cos.retry", "upload.cos.retry_platform",
		"upload.cos.resume_hint", "upload.cos.resume_confirm",
		"upload.cos.err.exceeds_admission", "upload.cos.err.format_unsupported",
		"upload.cos.err.waiting_limit", "upload.cos.err.state", "upload.cos.err.rate",
		"upload.cos.err.reconcile", "upload.cos.err.unavailable",
	];

	function loadI18n() {
		if (typeof (globalThis as { CustomEvent?: unknown }).CustomEvent === "undefined") {
			(globalThis as { CustomEvent?: unknown }).CustomEvent = class {
				constructor(public type: string, public detail?: unknown) {}
			};
		}
		const storage = fakeLocalStorage();
		const doc = {
			readyState: "loading",
			querySelectorAll: () => [],
			addEventListener() {},
			dispatchEvent() {},
			documentElement: { setAttribute() {}, getAttribute() { return null; } },
		};
		const w: Record<string, unknown> = { localStorage: storage, navigator: { language: "zh-CN" } };
		new Function("window", "document", "localStorage", "navigator", i18nSrc)(w, doc, storage, w.navigator);
		return w.HP_I18N as {
			t: (k: string, vars?: Record<string, unknown>) => string;
			setLang: (l: string) => void;
			getLang: () => string;
		};
	}

	it("真实 i18n.js：zh/en 均能解析全部 upload.cos.* 键（不回落 key 本身）", () => {
		const i18n = loadI18n();
		for (const key of [...STAGE_KEYS.map((s) => "upload.cos.stage." + s), ...OTHER_KEYS]) {
			i18n.setLang("zh");
			const zh = i18n.t(key);
			expect(zh, key + " (zh)").toBeTruthy();
			expect(zh, key + " (zh)").not.toBe(key);
			i18n.setLang("en");
			const en = i18n.t(key);
			expect(en, key + " (en)").toBeTruthy();
			expect(en, key + " (en)").not.toBe(key);
		}
		i18n.setLang("zh");
		expect(i18n.t("upload.cos.queue", { n: 3 })).toBe("第 3 位");
		i18n.setLang("en");
		expect(i18n.t("upload.cos.queue", { n: 3 })).toBe("position 3");
	});

	it("app.js：stage → 文案键映射；_EXTRA_I18N 兜底表同步全部键", () => {
		const h = loadApp();
		for (const s of STAGE_KEYS) {
			expect(h.up.cosStageKey(s)).toBe("upload.cos.stage." + s);
		}
		expect(h.up.cosStageKey("terminal")).toBe("upload.stage.failed");
		expect(h.up.cosStageKey("whatever")).toBe("upload.stage.failed");
		for (const key of [...STAGE_KEYS.map((s) => "upload.cos.stage." + s), ...OTHER_KEYS]) {
			expect(appSrc).toContain(`"${key}"`);
		}
	});
});
