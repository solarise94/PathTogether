/**
 * uploadFile / Upload V2 分片上传器（上传修复 U3 / test-review P3-18）。
 *
 * 用 loadApp harness（同 upload-csrf.test.ts）驱动**真实** app.js
 * （U3 统一 COS 后：V2 适配器仅排空过渡保留，检查点 B 删除）：
 *  - V2 适配器直驱：POST /api/uploads 创建任务（apiFetch → fetch，请求头带
 *    X-CSRF-Token），逐片 PUT /api/uploads/<id>/chunk（裸 XHR，同样带头 +
 *    offset/sha256 query），最后 POST commit；
 *  - 分片串行推进以服务端 confirmed_offset 为准；offset_mismatch 409 对齐重传；
 *  - 刷新恢复（经 uploadFile 的排空过渡 handoff）：localStorage v3 记录
 *    (name,size)→upload_id，续传从 confirmed_offset 起，不新建任务；
 *  - 无旧任务且 COS capability 未下发 → uploadFile 零网络请求（不回退）；
 *  - 多文件各自独立进度行（修共用进度条 bug）。
 */
import { afterEach, describe, expect, it, vi } from "vitest";
import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const appSrc = readFileSync(resolve(here, "../../static/app.js"), "utf8");

// 上传修复 A1：路由阈值唯一权威是服务端 UPLOAD_V2_THRESHOLD_BYTES（16MiB），
// 经 HP_APP_BOOTSTRAP.capabilities 下发；前端解析失败回落 16MiB。
const THRESHOLD = 16 * 1024 * 1024;

/** 可计数的元素 stub（appendChild/removeChild 记录 children，可断言多进度行） */
function el() {
	const children: unknown[] = [];
	return {
		hidden: true,
		textContent: "",
		innerHTML: "",
		value: "",
		disabled: false,
		style: {},
		className: "",
		classList: { add() {}, remove() {}, contains() { return false; } },
		children,
		appendChildren: children,
		appendChild(c: unknown) { children.push(c); },
		removeChild(c: unknown) {
			const i = children.indexOf(c);
			if (i >= 0) children.splice(i, 1);
		},
		addEventListener() {},
		parentNode: null,
	};
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
	simulateError() {
		this.listeners["error"] && this.listeners["error"]();
	}
}

/** localStorage stub（全局；app.js 直接引用 localStorage；含 key/length 枚举——
 * P2 续传键 v3 的账户域扫描与 v2 一次性迁移依赖） */
function fakeLocalStorage() {
	const map = new Map<string, string>();
	return {
		getItem: (k: string) => (map.has(k) ? map.get(k)! : null),
		setItem: (k: string, v: string) => { map.set(k, String(v)); },
		removeItem: (k: string) => { map.delete(k); },
		clear: () => map.clear(),
		get length() { return map.size; },
		key: (i: number) => Array.from(map.keys())[i] ?? null,
		_dump: map,
	};
}

type FetchCall = { url: string; opts: RequestInit & { headers?: Record<string, string> } };

function loadApp(fetchImpl?: typeof fetch, bootstrap?: unknown) {
	const storage = fakeLocalStorage();
	const els: Record<string, ReturnType<typeof el | typeof toastContainer>> = {};
	const toast = toastContainer();
	els["toast-container"] = toast as never;
	const container = el();
	els["upload-progress-list"] = container as never;
	const loc = { href: "http://local/", pathname: "/" };
	const theFetch = fetchImpl || (vi.fn(() => Promise.resolve({
		ok: true, status: 200, clone() { return this; },
		json: () => Promise.resolve({}),
	})) as unknown as typeof fetch);
	const w: Record<string, unknown> = {
		HP_I18N: {
			t: (k: string, vars?: { e?: string }) => (vars && vars.e ? `${k}:${vars.e}` : k),
			getLang: () => "zh",
		},
		fetch: theFetch,
		location: loc,
		OpenSeadragon: undefined,
	};
	if (bootstrap !== undefined) w.HP_APP_BOOTSTRAP = bootstrap;
	const doc = {
		readyState: "loading",
		cookie: "csrf_token=tok",
		getElementById(id: string) {
			if (!els[id]) els[id] = el();
			return els[id];
		},
		createElement() { return el(); },
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
	return {
		uploadFile: (w.HP_UPLOAD as { uploadFile: (f: unknown) => void }).uploadFile,
		uploadFileV2: (w.HP_UPLOAD as {
			uploadFileV2: (f: unknown, row: unknown, resumeOnly?: boolean) => unknown;
		}).uploadFileV2,
		fetchCalls: () => ((theFetch as unknown as { mock?: { calls: FetchCall[][] } }).mock
			? ((theFetch as unknown as vi.Mock).mock.calls as unknown as FetchCall[][]).map(
				([url, opts]) => ({ url, opts: opts || {} }))
			: []),
		container,
		toastMessages: toast.messages,
		storage,
	};
}

const tick = () => new Promise((r) => setTimeout(r, 0));
async function flush(n = 8) {
	for (let i = 0; i < n; i++) await tick();
}

function bigFile(size = THRESHOLD + 16) {
	return {
		name: "big.svs",
		size,
		lastModified: 42,
		slice(s: number, e: number) {
			const len = Math.max(0, Math.min(e, size) - s);
			return { arrayBuffer: async () => new ArrayBuffer(len) };
		},
	};
}

const stubRow = () => ({ setStage() {}, finish() {}, markError() {} });

afterEach(() => {
	vi.unstubAllGlobals();
	FakeXHR.instances = [];
});

describe("大文件走 Upload V2（/api/uploads + 分片 PUT + commit）", () => {
	it("创建请求带 X-CSRF-Token；分片 PUT 带头并按服务端 confirmed_offset 串行推进", async () => {
		const responses = new Map<string, () => Promise<Response>>([
			["POST /api/uploads", () => Promise.resolve({
				ok: true, status: 200, clone() { return this; },
				json: () => Promise.resolve({
					upload_id: "up-1", chunk_size: 8, confirmed_offset: 0,
					state: "active",
				}),
			} as unknown as Response)],
			["POST /api/uploads/up-1/commit", () => Promise.resolve({
				ok: true, status: 200, clone() { return this; },
				json: () => Promise.resolve({
					state: "committed", upload_id: "up-1",
					// P2 合同 §5.2：commit 完成响应携带 slide_id（打开目标不再按名猜）
					slide_id: "sld_up1completed",
				}),
			} as unknown as Response)],
		]);
		const fetchImpl = vi.fn((url: string, opts?: RequestInit) => {
			const key = `${(opts && opts.method) || "GET"} ${url}`;
			const handler = responses.get(key);
			if (handler) return handler();
			return Promise.resolve({
				ok: true, status: 200, clone() { return this; },
				json: () => Promise.resolve({}),
			} as unknown as Response);
		}) as unknown as typeof fetch;
		const h = loadApp(fetchImpl, {
			mode: "official",
			capabilities: { slide_id_api: true },
		});
		const file = bigFile(THRESHOLD + 16); // chunk_size=8 → 2 片 + 尾片（由响应推进）
		// U3：V2 适配器直驱（新任务不再路由到 V2——排空过渡期验证适配器契约）
		h.uploadFileV2(file, stubRow());
		await flush();

		// ① 创建任务：POST /api/uploads，CSRF 头由 apiFetch 注入
		const create = h.fetchCalls().find((c) => c.url === "/api/uploads");
		expect(create).toBeTruthy();
		expect(create!.opts.method).toBe("POST");
		expect((create!.opts.headers as Record<string, string>)["X-CSRF-Token"]).toBe("tok");
		// localStorage 已记录续传条目（P2 合同 §5.3：账户域键 pt.upload.v3::
		// <account>:<upload_id>，值含 filename/size 供辨认 + slide_id 字段位）
		const saved = JSON.parse(h.storage.getItem("pt.upload.v3::local:up-1") || "null");
		expect(saved && saved.upload_id).toBe("up-1");
		expect(saved.filename).toBe("big.svs");
		expect(saved.declared_size).toBe(file.size);
		expect("slide_id" in saved).toBe(true);

		// ② 分片 PUT：裸 XHR 带头；offset/sha256 在 query；严格串行
		expect(FakeXHR.instances.length).toBeGreaterThanOrEqual(1);
		const put1 = FakeXHR.instances[0];
		expect(put1.open).toHaveBeenCalledWith(
			"PUT", expect.stringMatching(/^\/api\/uploads\/up-1\/chunk\?offset=0&sha256=[0-9a-f]{64}$/));
		expect(put1.setRequestHeader).toHaveBeenCalledWith("X-CSRF-Token", "tok");
		// 服务端确认 8 字节 → 下一片 offset=8
		put1.simulateLoad(200, JSON.stringify({
			upload_id: "up-1", state: "active", chunk_size: 8, confirmed_offset: 8,
		}));
		await flush();
		expect(FakeXHR.instances.length).toBe(2);
		const put2 = FakeXHR.instances[1];
		expect(put2.open).toHaveBeenCalledWith(
			"PUT", expect.stringMatching(/^\/api\/uploads\/up-1\/chunk\?offset=8&sha256=[0-9a-f]{64}$/));
		// 第二片直接确认到文件末尾 → 进入 commit
		put2.simulateLoad(200, JSON.stringify({
			upload_id: "up-1", state: "active", chunk_size: 8,
			confirmed_offset: THRESHOLD + 16,
		}));
		await flush();

		// ③ commit：POST /api/uploads/up-1/commit（apiFetch 带头）
		const commit = h.fetchCalls().find((c) => c.url === "/api/uploads/up-1/commit");
		expect(commit).toBeTruthy();
		expect((commit!.opts.headers as Record<string, string>)["X-CSRF-Token"]).toBe("tok");
		await flush();
		// 成功：toast + 打开目标 = 响应 slide_id（P2 合同 §5.2：ID 通道
		// /api/slides/<slide_id>/info，绝不按 file.name/canonical_name 猜）
		expect(h.toastMessages.some((m) => m.indexOf("upload.done") >= 0)).toBe(true);
		expect(h.fetchCalls().some((c) => c.url === "/api/slides/sld_up1completed/info")).toBe(true);
		expect(h.fetchCalls().some((c) => c.url === "/api/slide/big.svs/info")).toBe(false);
		// localStorage 清续传条目
		expect(h.storage.getItem("pt.upload.v3::local:up-1")).toBeNull();
	});

	it("offset_mismatch 409 → 按服务端 confirmed_offset 对齐重传", async () => {
		const fetchImpl = vi.fn(() => Promise.resolve({
			ok: true, status: 200, clone() { return this; },
			json: () => Promise.resolve({
				upload_id: "up-2", chunk_size: 8, confirmed_offset: 0, state: "active",
			}),
		} as unknown as Response)) as unknown as typeof fetch;
		const h = loadApp(fetchImpl);
		h.uploadFileV2(bigFile(THRESHOLD + 16), stubRow());
		await flush();
		const put1 = FakeXHR.instances[0];
		// 服务端声称已确认 4 字节（客户端发的 offset=0 被拒）→ 对齐后重发 offset=4
		put1.simulateLoad(409, JSON.stringify({
			error: "offset 超前于服务端确认点（严格串行）",
			code: "offset_mismatch", confirmed_offset: 4,
		}));
		await flush();
		expect(FakeXHR.instances.length).toBe(2);
		expect(FakeXHR.instances[1].open).toHaveBeenCalledWith(
			"PUT", expect.stringMatching(/^\/api\/uploads\/up-2\/chunk\?offset=4&sha256=/));
	});

	it("刷新恢复：localStorage 有未完成任务 → GET 状态后从 confirmed_offset 续传", async () => {
		let statusQueried = false;
			const fetchImpl = vi.fn((url: string, opts?: RequestInit) => {
				if (url === "/api/uploads/up-9" && (!opts || !opts.method || opts.method === "GET")) {
				statusQueried = true;
				return Promise.resolve({
					ok: true, status: 200, clone() { return this; },
					json: () => Promise.resolve({
						upload_id: "up-9", state: "active", chunk_size: 8,
						confirmed_offset: 24,
					}),
				} as unknown as Response);
			}
			return Promise.resolve({
				ok: true, status: 200, clone() { return this; },
				json: () => Promise.resolve({}),
			} as unknown as Response);
		}) as unknown as typeof fetch;
		const h = loadApp(fetchImpl);
		const file = bigFile(THRESHOLD + 32);
		// P2 合同 §5.3：v2 键（无账户域）只做一次性只读迁移——能映射 upload_id
		// 的迁入 v3（账户域 local），迁完删旧键
		const v2Key = "pt.upload.v2::big.svs:" + file.size + ":42";
		h.storage.setItem(v2Key,
			JSON.stringify({ upload_id: "up-9", declared_size: file.size, chunk_size: 8 }));
		h.uploadFile(file);
		await flush();
		expect(statusQueried).toBe(true);
		// 迁移收口：v2 键删除、v3 键就位（账户域 local:up-9）
		expect(h.storage.getItem(v2Key)).toBeNull();
		expect(h.storage.getItem("pt.upload.v3::local:up-9")).toBeTruthy();
		// 不发 POST /api/uploads（复用任务）；第一片 offset = 恢复的 24
		expect(h.fetchCalls().some((c) => c.url === "/api/uploads" && c.opts.method === "POST")).toBe(false);
			expect(FakeXHR.instances[0].open).toHaveBeenCalledWith(
				"PUT", expect.stringMatching(/^\/api\/uploads\/up-9\/chunk\?offset=24&sha256=/));
		});

		it("刷新恢复：committed 任务重放 commit，不新建上传、不 PUT 分片", async () => {
			const fetchImpl = vi.fn((url: string, opts?: RequestInit) => {
				const method = (opts && opts.method) || "GET";
				if (url === "/api/uploads/up-c" && (!opts || !opts.method || method === "GET")) {
					return Promise.resolve({
						ok: true, status: 200, clone() { return this; },
						json: () => Promise.resolve({
							upload_id: "up-c", state: "committed", chunk_size: 8,
							confirmed_offset: THRESHOLD + 16,
						}),
					} as unknown as Response);
				}
				if (url === "/api/uploads/up-c/commit" && method === "POST") {
					return Promise.resolve({
						ok: true, status: 202, clone() { return this; },
						json: () => Promise.resolve({
							upload_id: "up-c", state: "queued",
							status: "conversion_pending",
							conversion_job_id: "cvj-1",
							canonical_name: "big.tif",
						}),
					} as unknown as Response);
				}
				if (String(url).startsWith("/api/conversions/")) {
					return Promise.resolve({
						ok: true, status: 200, clone() { return this; },
						json: () => Promise.resolve({
							conversion_job_id: "cvj-1", state: "queued",
							canonical_name: "big.tif",
						}),
					} as unknown as Response);
				}
				return Promise.resolve({
					ok: true, status: 200, clone() { return this; },
					json: () => Promise.resolve({}),
				} as unknown as Response);
			}) as unknown as typeof fetch;
			const h = loadApp(fetchImpl);
			const file = bigFile(THRESHOLD + 16);
			// v2 键经一次性迁移进 v3（同上：账户域 local:up-c）
			const v2Key = "pt.upload.v2::big.svs:" + file.size + ":42";
			h.storage.setItem(v2Key,
				JSON.stringify({ upload_id: "up-c", declared_size: file.size, chunk_size: 8 }));
			h.uploadFile(file);
			await flush();
			expect(h.fetchCalls().some((c) => c.url === "/api/uploads" && c.opts.method === "POST")).toBe(false);
			expect(FakeXHR.instances.length).toBe(0);
			const commit = h.fetchCalls().find((c) => c.url === "/api/uploads/up-c/commit");
			expect(commit).toBeTruthy();
			expect(commit!.opts.method).toBe("POST");
		});
	});

describe("无旧任务 + COS capability 未下发 → uploadFile 零网络请求（U3 不回退）", () => {
	it("不发 V1 XHR、不建 V2/COS 任务（禁用创建提示）", async () => {
		const h = loadApp();
		h.uploadFile({ name: "small.svs", size: 3 });
		await flush();
		expect(FakeXHR.instances).toHaveLength(0);
		expect(h.fetchCalls().length).toBe(0);
	});
});

describe("多文件独立进度行（修共用进度条 bug）", () => {
	it("两个并发上传各占一行（容器 children=2）", async () => {
		const h = loadApp();
		h.uploadFile({ name: "a.svs", size: 3 });
		h.uploadFile({ name: "b.svs", size: 5 });
		await flush();
		expect(h.container.appendChildren.length).toBe(2);
	});
});
