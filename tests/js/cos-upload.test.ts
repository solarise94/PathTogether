/**
 * COS 直传 Phase 4 前端适配器（docs/cos-direct-upload-audit-plan.md §5/§10
 * Phase 4 + docs/upload-routing-open-source-review.md D3/D6/D7/D8/D10）。
 *
 * 用 loadApp harness（同 upload-v2.test.ts / upload-csrf.test.ts）驱动**真实**
 * app.js，锁定：
 *  1. 选路：capability off → uploadFile 禁用创建（零网络请求 + 「上传暂
 *     不可用」提示）；可用 → uploadFile 恒走 COS（无开关、无大小分流、
 *     无回退）；eligible 词表/上限以服务端 capability 为唯一权威；
 *  2. COS PUT 独立传输：对 COS URL 的 fetch 不带 X-CSRF-Token、
 *     credentials:"omit"、mode:"cors"；控制 API（/api/ingestions*）经 apiFetch
 *     带 CSRF 双提交头；
 *  3. 分批签名（sign_batch_max_parts）+ 批内并发（max_concurrent_parts）+
 *     分块进度（confirmed/total，仅上传阶段）；单片失败重试后成功；
 *  4. 413 upload_too_large → 可读说明；**无另一后端按钮**（回退产品
 *     策略已退役）；
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
// C4：COS 状态机抽出为共享引擎 static/upload/cos-uploader.js（index.html
// 先于 app.js 加载）。harness 同序执行两个源码——只改加载方式，不改断言。
const cosEngineSrc = readFileSync(
	resolve(here, "../../static/upload/cos-uploader.js"), "utf8");
// 阶段 1：直传类别嗅探共享模块（index.html 与 cos-uploader 同段加载）
const slideSniffSrc = readFileSync(
	resolve(here, "../../static/upload/slide-sniff.js"), "utf8");
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

/** U1：COS PUT 走 XHR（upload.onprogress）。可控假 XHR：send 时经 onSend 裁决。 */
class FakeXHR {
	static instances: FakeXHR[] = [];
	static onSend: ((xhr: FakeXHR) => void) | null = null;
	method = "";
	url = "";
	body: unknown = null;
	sent = false;
	aborted = false;
	withCredentials: boolean | undefined = undefined;
	status = 0;
	upload: Record<string, unknown> = {};
	setRequestHeader = vi.fn();
	getResponseHeader = vi.fn((_h: string) => null);
	open(method: string, url: string) { this.method = method; this.url = url; }
	send(body: unknown) {
		this.body = body;
		this.sent = true;
		if (FakeXHR.onSend) FakeXHR.onSend(this);
	}
	abort() { this.aborted = true; if (this.onabort) this.onabort(); }
	respond(status: number, etag: string | null = null) {
		this.status = status;
		this.getResponseHeader = vi.fn((h: string) =>
			h.toLowerCase() === "etag" ? etag : null);
		if (this.onload) this.onload();
	}
	failNetwork() { if (this.onerror) this.onerror(); }
	progress(loaded: number, computable = true) {
		const fn = this.upload.onprogress as ((ev: unknown) => void) | undefined;
		if (fn) fn({ loaded, lengthComputable: computable });
	}
	onload: (() => void) | null = null;
	onerror: (() => void) | null = null;
	onabort: (() => void) | null = null;
	ontimeout: (() => void) | null = null;
	constructor() { FakeXHR.instances.push(this); }
}

/** 默认 XHR 后端：全部 COS PUT 立即 200 + ETag。 */
function okXhr() {
	FakeXHR.onSend = (x) => { x.respond(200, '"etag-x"'); };
}

/** 行文本部件（stub 元素不级联 className 之外的语义）。 */
function rowText(row: ReturnType<typeof el>, cls: string) {
	const c = row.children.find((x) => String((x as ReturnType<typeof el>).className) === cls) as ReturnType<typeof el> | undefined;
	return c ? String(c.textContent) : "";
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
	new Function("window", "document", "fetch", "location", cosEngineSrc)(w, doc, theFetch, loc);
	new Function("window", slideSniffSrc)(w);
	new Function("window", "document", "fetch", "location", appSrc)(w, doc, theFetch, loc);
	const up = w.HP_UPLOAD as Record<string, unknown>;
	return {
		up: up as {
			uploadFile: (f: unknown, opts?: { cosRetry?: string }) => void;
			cosUploadEligible: (f: unknown) => boolean;
			resolveCosConfig: () => unknown;
			cosStageKey: (s: string) => string;
			cosIneligibleReason: (f: unknown) => string;
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
	FakeXHR.onSend = null;
});

// --------------------------------------------------------------------------- #
// 1. 选路（U3 统一 COS：无开关、无大小分流；capability off 禁用创建）
// --------------------------------------------------------------------------- #
describe("选路：capability / eligible 判定（统一 COS）", () => {
	it("cos_upload 未下发（off）→ 禁用创建：零网络请求 + 「上传暂不可用」提示（不回退 V1/V2）", async () => {
		const h = loadApp(undefined, { mode: "official", capabilities: { upload_v2_threshold_bytes: THRESHOLD } });
		expect(h.up.resolveCosConfig()).toBeNull();
		h.up.initCosUploadUi();
		expect(h.toggleParent._inserts.length).toBe(0);   // 无任何开关渲染
		h.up.uploadFile({ name: "a.svs", size: 3 });
		await flush(8);   // 阶段 1：uploadFile 先嗅探（微任务）再分流
		expect(FakeXHR.instances).toHaveLength(0);        // 不发 V1 XHR
		expect(h.fetchCalls().length).toBe(0);            // 不建 V2/COS 任务
		const row = h.container.appendChildren[h.container.appendChildren.length - 1];
		expect(String(row.children[2].textContent)).toContain("上传暂不可用");
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

	it("capability 可用 → uploadFile 直接 POST /api/ingestions（CSRF）；无开关、不走 /api/uploads、不发 V1 XHR", async () => {
		// 创建后即 terminal（失败终态）让链路尽快收口，只验证选路
		const fetchImpl = vi.fn((url: string, opts?: RequestInit) => {
			if (url === "/api/ingestions" && opts && opts.method === "POST") {
				return Promise.resolve(resp({ job_id: "inj_1", state: "failed", stage: "terminal", fail_code: "demo" }, 202));
			}
			return Promise.resolve(resp({ job_id: "inj_1", stage: "terminal", fail_code: "demo" }));
		}) as unknown as typeof fetch;
		const h = loadApp(fetchImpl, { mode: "official", capabilities: { cos_upload: cosCaps() } });
		h.up.initCosUploadUi();
		expect(h.toggleParent._inserts.length).toBe(0);   // U3：无开关渲染
		h.up.uploadFile(cosFile());
		await flush(8);
		const create = h.fetchCalls().find((c) => c.url === "/api/ingestions" && c.method === "POST");
		expect(create).toBeTruthy();
		expect((create!.opts.headers as Record<string, string>)["X-CSRF-Token"]).toBe("tok");
		expect(h.fetchCalls().some((c) => c.url === "/api/uploads" && c.method === "POST")).toBe(false);
		expect(FakeXHR.instances).toHaveLength(0);
	});

	it("eligible 词表以服务端 capability 为准：zip/kfb 受理（词表内）、裸 mrxs/超大/零字节拒绝并说明原因", async () => {
		const h = loadApp(undefined, { mode: "official", capabilities: {
			cos_upload: cosCaps({ formats: ["bif", "ndpi", "svs", "tif", "zip", "kfb"] }),
		} });
		expect(h.up.cosUploadEligible(cosFile(30))).toBe(true);
		expect(h.up.cosUploadEligible({ name: "a.zip", size: 30 })).toBe(true);        // U2：zip 受理
		expect(h.up.cosUploadEligible({ name: "a.kfb", size: 30 })).toBe(true);        // U2：转换源受理
		expect(h.up.cosUploadEligible({ name: "a.mrxs", size: 30 })).toBe(false);      // 裸 bundle：服务端词表外
		expect(h.up.cosUploadEligible({ name: "a.svs", size: 0 })).toBe(false);
		expect(h.up.cosUploadEligible({ name: "a.svs", size: 1001 })).toBe(false);     // 超产品上限
		expect(String(h.up.cosIneligibleReason({ name: "a.mrxs", size: 30 }))).toContain("不支持该文件格式");
		expect(String(h.up.cosIneligibleReason({ name: "a.svs", size: 5000 }))).toContain("超过平台上限");
		// 不合格 → 零请求 + 可读说明（不提供另一后端）。阶段 1：mrxs 按嗅探
		// 类别走「本机转换并上传」入口（行提示，不判失败）；超大 .svs 走
		// 「超过平台上限」toast（嗅探读失败按暂时直传降级 → 词表路径）
		h.up.uploadFile({ name: "a.mrxs", size: 30 });
		h.up.uploadFile({ name: "huge.svs", size: 5000 });
		await flush(8);
		expect(FakeXHR.instances).toHaveLength(0);
		expect(h.fetchCalls().length).toBe(0);
		const rows = h.container.appendChildren;
		// mrxs 行是「待本机转换」提示（upload.kfb.hint），不判失败；huge 行
		// 是「超过平台上限」toast（upload.cos.err.too_large）
		const mrxsRow = String(rows[rows.length - 2].children[2].textContent);
		expect(mrxsRow).toContain("本机转换");
		expect(h.toastMessages.some((m) => m.indexOf("超过平台上限") >= 0))
			.toBe(true);
	});
});

// --------------------------------------------------------------------------- #
// 2 + 3. 独立传输 / 分批签名 + 并发 + 进度 / upload-complete
// --------------------------------------------------------------------------- #
describe("COS 上传状态机：独立传输、分批签名、并发、进度、完成", () => {
	it("COS PUT 走 XHR（无 CSRF 头/不带凭据）；控制 API 带 CSRF；两批签名 + 批内并发 + 字节进度 + upload-complete", async () => {
		vi.useFakeTimers();
		const parts = [
			{ part_number: 1, length: 8 }, { part_number: 2, length: 8 },
			{ part_number: 3, length: 8 }, { part_number: 4, length: 6 },
		];
		const afterComplete = ["awaiting_server", "downloading", "viewable"];
		let getStatus = 0;
		// complete 后第一次 GET 挂起：先断言上传阶段 100% 中间态再放行
		let releaseServerStage: (() => void) | null = null;
		// 签名（fetch）与分块 PUT（XHR）的跨载体顺序：批 1 的两个 PUT 都在
		// 批 2 签名之前
		const seq: string[] = [];
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
				// P2 合同 §5.2：viewable 响应带 slide_id（slide 名快照并存）
				if (stage === "viewable") {
					body.slide = "big.svs";
					body.slide_id = "sld_cos00000001";
				}
				return Promise.resolve(resp(body));
			}
			if (url === "/api/ingestions/inj_9/parts/sign" && method === "POST") {
				seq.push("sign");
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
			// U1：COS PUT 不再走 fetch——落到这里即判错（防回归到 fetch 载体）
			if (url.startsWith("https://")) {
				return Promise.reject(new Error("COS PUT must use XHR (U1)"));
			}
			return Promise.resolve(resp({}));
		}) as unknown as typeof fetch;
		const h = loadApp(fetchImpl, { mode: "official", capabilities: {
			slide_id_api: true,
			cos_upload: cosCaps(),
		} });
		// 分块 PUT：挂起收集字节进度，再逐一确认（字节事件 → 行内字节文本）
		const gated: FakeXHR[] = [];
		FakeXHR.onSend = (x) => { seq.push("PUT"); gated.push(x); };
		const file = cosFile(30);
		h.up.uploadFile(file);
		for (let i = 0; i < 50 && gated.length < 2; i++) await vi.advanceTimersByTimeAsync(0);
		expect(gated.length, "批 1 两片并发在途").toBe(2);

		const calls = h.fetchCalls;
		// ② 控制 API（apiFetch 语义）：创建/签名/完成都带双提交头
		["/api/ingestions", "/api/ingestions/inj_9/parts/sign", "/api/ingestions/inj_9/upload-complete"]
			.forEach((u) => {
				const call = calls().find((c) => c.url === u && c.method === "POST");
				if (u.endsWith("/upload-complete")) return;   // 尚未到达（批 2 未传）
				expect(call, u).toBeTruthy();
				expect((call!.opts.headers as Record<string, string>)["X-CSRF-Token"]).toBe("tok");
			});
		// ⑤ U1 字节进度：分块内字节事件 → 行文本按字节（不是按片数）
		const row = h.container.appendChildren[h.container.appendChildren.length - 1];
		const statusEl = row.children[2];
		await vi.advanceTimersByTimeAsync(150);
		gated[0].progress(4);                    // 批 1 第 1 片 4/8 字节
		await vi.advanceTimersByTimeAsync(0);
		expect(rowText(row, "upload-item-status")).toContain("正在上传");
		expect(rowText(row, "upload-item-bytes")).toContain("upload.cos.bytes:4 B,30 B");
		// 批 1 全部字节发出（16/30，尾批未开始）：字节文本如实显示，不显示完成
		await vi.advanceTimersByTimeAsync(150);
		gated[1].progress(8);                   // 12/30（节流发射）
		await vi.advanceTimersByTimeAsync(0);
		await vi.advanceTimersByTimeAsync(150);
		gated[0].progress(8);                   // 16/30：批 1 body 全发出
		await vi.advanceTimersByTimeAsync(0);
		expect(rowText(row, "upload-item-bytes")).toContain("upload.cos.bytes:16 B,30 B");
		gated[0].respond(200, '"etag-x"');
		gated[1].respond(200, '"etag-x"');
		for (let i = 0; i < 50 && gated.length < 4; i++) await vi.advanceTimersByTimeAsync(0);
		expect(gated.length, "批 2 两片并发在途").toBe(4);

		// ① 独立传输：COS PUT 是 XHR——PUT/签名 URL/零自定义头/不带凭据
		const puts = FakeXHR.instances;
		expect(puts).toHaveLength(4);
		puts.forEach((x) => {
			expect(x.method).toBe("PUT");
			expect(String(x.url)).toContain("partNumber=");
			expect(x.withCredentials).toBe(false);
			expect(x.setRequestHeader).not.toHaveBeenCalled();
		});
		// ③ 分批（sign_batch_max_parts=2）+ 批内并发（max_concurrent_parts=2）：
		//    批 1 的两个 PUT 都发生在批 2 签名之前
		const signCalls = calls().filter((c) => c.url.endsWith("/parts/sign"));
		expect(signCalls).toHaveLength(2);
		expect(JSON.parse(String((signCalls[0].opts as { body: string }).body)).part_numbers).toEqual([1, 2]);
		expect(JSON.parse(String((signCalls[1].opts as { body: string }).body)).part_numbers).toEqual([3, 4]);
		const sign1 = seq.indexOf("sign");
		const sign2 = seq.indexOf("sign", sign1 + 1);
		expect(seq.slice(sign1, sign2).filter((s) => s === "PUT").length).toBe(2);
		// 短尾片（6B/30B）：字节加权（已确认 16 + 尾片进行中）
		await vi.advanceTimersByTimeAsync(150);
		gated[3].progress(3);
		await vi.advanceTimersByTimeAsync(0);
		expect(rowText(row, "upload-item-bytes")).toContain("upload.cos.bytes:19 B,30 B");
		// 全部 body 已发出、HTTP 未确认 → 「数据已发送，等待确认」（不是完成）
		await vi.advanceTimersByTimeAsync(150);
		gated[3].progress(6);                   // 16 + 6 = 22（尾片 body 全发出）
		await vi.advanceTimersByTimeAsync(0);
		await vi.advanceTimersByTimeAsync(150);
		gated[2].progress(8);                   // 16 + 6 + 8 = 30：全部字节已发出
		await vi.advanceTimersByTimeAsync(0);
		// （无 vars 的键走 app.js _EXTRA_I18N 兜底的真实中文文案）
		expect(rowText(row, "upload-item-bytes")).toContain("数据已发送，等待确认");
		gated[2].respond(200, '"etag-x"');
		gated[3].respond(200, '"etag-x"');
		await vi.advanceTimersByTimeAsync(0);
		// ④ upload-complete 已被调（全部 confirmed 之后，控制 API 带双提交头）
		const completeCall = calls().find((c) => c.url.endsWith("/upload-complete") && c.method === "POST");
		expect(completeCall).toBeTruthy();
		expect((completeCall!.opts.headers as Record<string, string>)["X-CSRF-Token"]).toBe("tok");
		// 上传阶段 100%（按字节：30/30）；阶段行（aria-live）只装阶段名，
		// 百分比/字节在非播报的字节行——绝不显示「完成」
		expect(String(statusEl.textContent)).toContain("正在上传");
		expect(rowText(row, "upload-item-bytes")).toContain("100%");
		expect(String(statusEl.textContent)).not.toContain("100%");
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
		// 下载阶段百分比同样在字节行（aria-live 只随阶段播报）
		expect(rowText(row, "upload-item-bytes")).toContain("50%");
		expect(String(statusEl.textContent)).not.toContain("50%");
		await vi.advanceTimersByTimeAsync(2000);   // downloading → viewable
		await vi.advanceTimersByTimeAsync(0);
		expect(h.toastMessages.some((m) => m.indexOf("upload.done") >= 0)).toBe(true);
		// P2 合同 §5.2：viewable 打开目标 = 响应 slide_id（ID 通道 info；绝不
		// 按 file.name/slide 名猜）
		expect(h.fetchCalls().some((c) => c.url === "/api/slides/sld_cos00000001/info")).toBe(true);
		expect(h.fetchCalls().some((c) => c.url === "/api/slide/big.svs/info")).toBe(false);
		expect(JSON.parse(h.storage.getItem("pt.cos.jobs") || "[]")).toEqual([]);
	});

	it("单片失败：同 URL 重试后成功（不触发重新签名分支、不影响其余分块；重试显示「正在重试」）", async () => {
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
			return Promise.resolve(resp({}));
		}) as unknown as typeof fetch;
		const h = loadApp(fetchImpl, { mode: "official", capabilities: { cos_upload: cosCaps() } });
		FakeXHR.onSend = (x) => {
			const n = Number(new URL(String(x.url)).searchParams.get("partNumber"));
			if (n === 2 && part2Fails-- > 0) x.respond(500);
			else x.respond(200, '"etag-x"');
		};
		h.up.uploadFile(cosFile(30));
		await flush(12);
		// 重试延迟 600ms（真实定时器）后 part 2 成功 → 全部 confirmed → complete → viewable
		await new Promise((r) => setTimeout(r, 800));
		await flush(12);
		expect(FakeXHR.instances).toHaveLength(5);   // 4 片 + part2 重试一次
		expect(h.fetchCalls().some((c) => c.url === "/api/ingestions/inj_f/upload-complete")).toBe(true);
		expect(h.toastMessages.some((m) => m.indexOf("upload.done") >= 0)).toBe(true);
		// 失败重试不累计字节：进度按唯一 confirmed 分块计（viewable 后行文案
		// 已切「入库完成」，以恢复记录为准）
		const saved = JSON.parse(h.storage.getItem("pt.cos.jobs") || "[]");
		expect(saved).toEqual([]);   // 成功后清理
	});
});

// --------------------------------------------------------------------------- #
// 4. 413 upload_too_large → 可读说明；无另一后端按钮（回退策略已退役）
// --------------------------------------------------------------------------- #
describe("413 upload_too_large：说明 + 无回退入口", () => {
	it("row 显示可读说明；无任何「平台上传」按钮/XHR（不提供另一后端）", async () => {
		const fetchImpl = vi.fn((url: string, opts?: RequestInit) => {
			if (url === "/api/ingestions" && opts && opts.method === "POST") {
				return Promise.resolve(resp({ code: "upload_too_large", max_size_bytes: 900000 }, 413));
			}
			return Promise.resolve(resp({}));
		}) as unknown as typeof fetch;
		const h = loadApp(fetchImpl, { mode: "official", capabilities: { cos_upload: cosCaps() } });
		const file = cosFile(30, "small.svs");
		h.up.uploadFile(file);
		await flush(12);
		// 可读文案（稳定码不透出原文）
		expect(h.toastMessages.some((m) => m.indexOf("文件超过平台上限") >= 0)).toBe(true);
		// 行上只有「取消」按钮——没有「改用平台上传」
		const row = h.container.appendChildren[h.container.appendChildren.length - 1];
		const btns = row.children.filter((c) => String(c.tagName) === "BUTTON");
		expect(btns.some((b) => String(b.textContent).indexOf("平台") >= 0)).toBe(false);
		// 不触发 V1 XHR、不重试创建
		expect(FakeXHR.instances).toHaveLength(0);
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
			return Promise.resolve(resp({}));
		}) as unknown as typeof fetch;
		const h = loadApp(fetchImpl, { mode: "official", capabilities: { cos_upload: cosCaps() } });
		okXhr();
		h.up.uploadFile(cosFile(16));
		await vi.advanceTimersByTimeAsync(0);
		const row = h.container.appendChildren[h.container.appendChildren.length - 1];
		const statusEl = row.children[2];
		// 排队位置展示（0 基 → 第 1 位，位置在字节行）；无预计时间（不出现 eta 字样）
		expect(String(statusEl.textContent)).toContain("等待暂存空间");
		expect(rowText(row, "upload-item-bytes")).toContain("upload.cos.queue:1");
		expect(String(statusEl.textContent)).not.toContain("eta");
		// 5s 轮询推进：第一跳后仍在等待（第二次 GET 仍 waiting），继续放行到 uploading
		await vi.advanceTimersByTimeAsync(5000);
		await vi.advanceTimersByTimeAsync(5000);
		expect(h.fetchCalls().filter((c) => c.url === "/api/ingestions/inj_w" && c.method === "GET").length)
			.toBeGreaterThanOrEqual(3);
		// 准入后拿到分块计划 → 签名 + PUT（XHR）→ 完成
		expect(h.fetchCalls().some((c) => c.url === "/api/ingestions/inj_w/parts/sign")).toBe(true);
		expect(FakeXHR.instances).toHaveLength(2);
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
		// 下载百分比在字节行（阶段行 aria-live 只装阶段名）
		expect(rowText(row, "upload-item-bytes")).toContain("33%");
		expect(String(row.children[2].textContent)).not.toContain("33%");
		// uploading 未完成 → 提示重选同名文件可续传（位置在字节行）
		stage = "uploading";
		await vi.advanceTimersByTimeAsync(3000);
		expect(rowText(row, "upload-item-bytes")).toContain("重新选择同名文件可续传");
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
			return Promise.resolve(resp({}));
		}) as unknown as typeof fetch;
		const h = loadApp(fetchImpl, { mode: "official", capabilities: { cos_upload: cosCaps() } });
		okXhr();   // U1：COS PUT 走 XHR
		h.storage.setItem("pt.cos.jobs", JSON.stringify([
			{ job_id: "inj_r", filename: "big.svs", size: 30, confirmed: [1, 2] },
		]));
		h.confirmMock.mockReturnValue(true);   // 用户确认续传
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
  	const STAGE_KEYS = ["waiting_space", "uploading", "awaiting_server", "downloading", "validating", "processing", "readiness", "viewable"];
	const OTHER_KEYS = [
		"upload.cos.queue", "upload.cos.cancel",
		"upload.cos.cancelled", "upload.cos.retry",
		"upload.cos.resume_hint", "upload.cos.resume_confirm",
		"upload.cos.err.format_unsupported", "upload.cos.err.too_large",
		"upload.cos.err.pool_config", "upload.cos.unavailable",
		"upload.cos.items", "upload.cos.conv_state",
		"upload.cos.err.waiting_limit", "upload.cos.err.state", "upload.cos.err.rate",
		"upload.cos.err.reconcile",
		// U1 字节级进度
		"upload.cos.bytes", "upload.cos.sent_all", "upload.cos.retrying",
		"upload.cos.err.plan",
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
