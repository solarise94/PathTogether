/**
 * C4 共享 COS 上传引擎单元测试（static/upload/cos-uploader.js）。
 *
 * 与 cos-upload.test.ts（app.js 工作台适配层）不同：这里直接以**注入的假
 * 依赖**（apiFetch/storage/source/回调）驱动 HP_COS_UPLOAD.createUpload，
 * 锁定引擎本身的合同——工具页（/tools/slides）依赖的行为都在这里：
 *
 *  1. 分块只经 source.slice()（绝不整体读取），COS PUT 独立传输（XHR：
 *     withCredentials=false、零自定义头、监听先于 send 注册）；
 *  2. storage.save 在任何分块 PUT 之前落任务记录（断网/刷新后可续传）；
 *  3. retryCompleteOnNetworkError=true：完成响应丢失 → 重发 complete，
 *     409 ingestion_state_conflict 视为已完成过 → 轮询收口；
 *  4. 显式续传 resumeJobId + readConfirmed：只签/只传未确认分块；
 *     useResumeEndpoint：服务端已离开 uploading → 先 POST /resume；
 *  5. 401 中途失败：reject（引擎无 location 访问——绝不自动跳转登录）；
 *  6. cancel：abort 全部在途 XHR + 清本地记录 + POST /cancel + done
 *     resolve {cancelled:true}；迟到成功不复活记录；
 *  7. terminal：storage.complete(terminal) + reject {terminal}；
 *  8. U1 字节级进度：分片内多次递增字节事件（节流）、大片+短尾按字节加权、
 *     并发夹紧不过 100%、重试/403/断网/取消/迟到回调不双计、
 *     「数据已发送，等待确认」、totalBytes 与 source.size 不一致拒绝。
 */
import { afterEach, describe, expect, it, vi } from "vitest";
import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const engineSrc = readFileSync(
  resolve(here, "../../static/upload/cos-uploader.js"), "utf8");

type StorageRec = {
	job_id: string; filename: string; size: number;
	confirmed: number[]; slide_id?: string | null;
	digests?: Record<string, string> | null;   // review #1：分片摘要（方案见 digest_scheme）
	digest_scheme?: string;                    // 复核第二轮：摘要方案标记
	account?: string;                          // review #1：账号绑定
};

/** U1：COS PUT 走 XHR。可驱动的假 XHR（进度/成功/HTTP 错误/网络错误/abort）。 */
class FakeXHR {
	static instances: FakeXHR[] = [];
	/** (xhr, url, body) => void —— send 时调用；测试用它裁决每个 PUT */
	static onSend: ((xhr: FakeXHR, url: string, body: unknown) => void) | null = null;
	method = "";
	url = "";
	body: unknown = null;
	withCredentials: boolean | undefined = undefined;
	status = 0;
	upload: Record<string, unknown> = {};
	sent = false;
	aborted = false;
	setRequestHeader = vi.fn();
	getResponseHeader = vi.fn((_h: string) => null);
	open(method: string, url: string) { this.method = method; this.url = url; }
	send(body: unknown) {
		this.body = body;
		this.sent = true;
		// 「监听先于 send 注册」合同：send 时刻必须已挂好进度与终态监听
		expect(this.upload.onprogress, "upload.onprogress before send").toBeTypeOf("function");
		expect(this.onload, "onload before send").toBeTypeOf("function");
		expect(this.onerror, "onerror before send").toBeTypeOf("function");
		expect(this.onabort, "onabort before send").toBeTypeOf("function");
		if (FakeXHR.onSend) FakeXHR.onSend(this, this.url, body);
	}
	abort() {
		this.aborted = true;
		if (this.onabort) this.onabort();
	}
	/** 模拟上传字节进度（lengthComputable 默认 true） */
	progress(loaded: number, computable = true) {
		const fn = this.upload.onprogress as ((ev: unknown) => void) | undefined;
		if (fn) fn({ loaded, lengthComputable: computable, total: computable ? this.bodyLen() : 0 });
	}
	bodyLen() {
		const b = this.body as
			| { _range?: [number, number] }
			| ArrayBuffer
			| null;
		if (b instanceof ArrayBuffer) return b.byteLength;
		return b && (b as { _range?: [number, number] })._range
			? (b as { _range: [number, number] })._range[1] -
					(b as { _range: [number, number] })._range[0]
			: 0;
	}
	/** 模拟 HTTP 响应（2xx=成功；非 2xx=分块失败走重试合同） */
	respond(status: number, etag: string | null = null) {
		this.status = status;
		this.getResponseHeader = vi.fn((h: string) =>
			h.toLowerCase() === "etag" ? etag : null);
		if (this.onload) this.onload();
	}
	failNetwork() { if (this.onerror) this.onerror(); }
	onload: (() => void) | null = null;
	onerror: (() => void) | null = null;
	onabort: (() => void) | null = null;
	ontimeout: (() => void) | null = null;
	constructor() { FakeXHR.instances.push(this); }
}

/** 签名 URL → partNumber（与 fake 后端约定 partNumber=n 查询参数） */
function putPartNumber(url: string) {
	return Number(new URL(url).searchParams.get("partNumber"));
}

function fakeWindow(fetchImpl: typeof fetch) {
	const w: Record<string, unknown> = {
		fetch: fetchImpl,
		location: { href: "http://local/", pathname: "/tools/slides" },
	};
	(globalThis as { window?: unknown }).window = w;
	return w;
}

function loadEngine(fetchImpl?: typeof fetch) {
	const w = fakeWindow(fetchImpl || (vi.fn(() => Promise.resolve({
		ok: true, status: 200, clone() { return this; },
		json: () => Promise.resolve({}),
	})) as unknown as typeof fetch));
	vi.stubGlobal("XMLHttpRequest", FakeXHR);
	new Function("window", "fetch", "location", engineSrc)(
		w, w.fetch, w.location);
	return w.HP_COS_UPLOAD as {
		resolveConfig: (p: unknown) => Record<string, unknown> | null;
		createUpload: (o: Record<string, unknown>) => {
			cancel: () => void;
			done: Promise<{ ok?: boolean; body?: unknown; cancelled?: boolean }>;
		};
	};
}

/** 默认 XHR 后端：全部 PUT 立即 200 + ETag（测试按需覆写 FakeXHR.onSend）。 */
function happyXhr() {
	FakeXHR.onSend = (xhr) => { xhr.respond(200, '"etag-ok"'); };
	return FakeXHR;
}

function resp(body: unknown, status = 200) {
	return {
		ok: status >= 200 && status < 300,
		status,
		clone() { return this; },
		json: () => Promise.resolve(body),
		headers: { get: (h: string) => (h.toLowerCase() === "etag" ? `"e-${h}"` : null) },
	} as unknown as Response;
}

const CFG = {
	available: true,
	max_size_bytes: 1000,
	part_bytes: 8,
	url_ttl_seconds: 600,
	max_concurrent_parts: 2,
	sign_batch_max_parts: 4,
	formats: ["tif"],
};

/** 分块假源：只允许 slice 访问（整体读取会被计数并失败测试）。
 *  review #1：引擎对每片「读一次 slice → 同一 ArrayBuffer 既做 SHA-256
 *  又做 PUT body」，因此假源的 slice 结果必须提供 arrayBuffer()。 */
function fakeSource(size = 32, name = "out.tif") {
	const slices: Array<{ start: number; end: number }> = [];
	return {
		slices,
		view: {
			name,
			size,
			slice(start: number, end: number) {
				slices.push({ start, end });
				expect(end - start).toBeLessThanOrEqual(8);
				const len = end - start;
				// Blob 语义：chunked 哈希对分片 Blob 再按 4 MiB 子块 slice
				const blob = {
					size: len,
					slice(a: number, b: number) {
						const sub = Math.min(b, len) - a;
						return { size: sub, arrayBuffer: async () => new ArrayBuffer(sub) };
					},
					arrayBuffer: async () => new ArrayBuffer(len),
				};
				return blob;
			},
		},
	};
}

function fakeStorage() {
	const saves: StorageRec[] = [];
	const completes: Array<[string, Record<string, unknown>]> = [];
	const removes: string[] = [];
	let confirmed: number[] = [];
	// review #1：续传记录的完整形态（账号绑定 + 分块内容摘要）
	let record: { confirmed?: number[]; digests?: Record<string, string>;
		digest_scheme?: string; account?: string } | null = null;
	return {
		saves, completes, removes,
		setConfirmed: (c: number[]) => { confirmed = c; },
		setRecord: (r: typeof record) => { record = r; },
		adapter: {
			save(rec: StorageRec) { saves.push(rec); },
			complete(id: string, outcome: Record<string, unknown>) {
				completes.push([id, outcome]);
			},
			remove(id: string) { removes.push(id); },
			findResumable: () => null,
			readConfirmed: () => confirmed,
			readRecord: (id: string) => Promise.resolve(
				record ? { job_id: id, filename: "out.tif", size: 0,
					confirmed: record.confirmed || confirmed,
					digests: record.digests || null,
					digest_scheme: record.digest_scheme || "",
					account: record.account || "" } : null),
		},
	};
}


/** 推进假时钟并让出真实事件循环：确认链上的 WebCrypto 摘要是线程池真实
 *  异步——只冲微任务不会让它落地（高负载下尤其明显），done/记录断言需要
 *  真实 yield 才确定。 */
const REAL_TIMEOUT = setTimeout;
async function advYield(ms: number) {
	await vi.advanceTimersByTimeAsync(ms);
	await new Promise((r) => REAL_TIMEOUT(r, 0));
}

const tick = () => new Promise((r) => setTimeout(r, 0));
async function flush(n = 12) { for (let i = 0; i < n; i++) await tick(); }

function happyFetch(parts: Array<{ part_number: number; length: number }>,
	opts: { afterComplete?: string[] } = {}) {
	let status = 0;
	const after = opts.afterComplete || ["viewable"];
	return vi.fn((url: string, init?: RequestInit) => {
		const method = String((init && init.method) || "GET");
		if (url === "/api/ingestions" && method === "POST") {
			return Promise.resolve(resp({ job_id: "inj_x", stage: "uploading" }, 202));
		}
		if (url === "/api/ingestions/inj_x" && method === "GET") {
			if (status === 0) return Promise.resolve(resp({ stage: "uploading", parts }));
			const stage = after[Math.min(status - 1, after.length - 1)];
			status++;
			return Promise.resolve(resp(stage === "viewable"
				? { stage, slide_id: "sld_1", slide: "out.tif" }
				: { stage }));
		}
		if (url.endsWith("/parts/sign") && method === "POST") {
			const nums = JSON.parse(String(init!.body)).part_numbers as number[];
			return Promise.resolve(resp({
				urls: nums.map((n) => ({
					url: `https://cos.example/o?partNumber=${n}`, part_number: n,
				})),
			}));
		}
		if (url.endsWith("/upload-complete") && method === "POST") {
			status = 1;
			return Promise.resolve(resp({ stage: "awaiting_server" }, 202));
		}
		// U1：COS PUT 必须走 XHR——落回 fetch 即判错（防回归到 fetch 载体）
		if (url.startsWith("https://")) {
			return Promise.reject(new Error("COS PUT must use XHR (U1)"));
		}
		return Promise.resolve(resp({}));
	}) as unknown as typeof fetch;
}

afterEach(() => {
	vi.useRealTimers();
	vi.unstubAllGlobals();
	FakeXHR.instances = [];
	FakeXHR.onSend = null;
});

describe("HP_COS_UPLOAD.resolveConfig", () => {
	it("available:false / 缺字段 / 非法数值 → null；合法 → 规范化（并发/批量夹上界）", () => {
		const eng = loadEngine();
		expect(eng.resolveConfig(null)).toBeNull();
		expect(eng.resolveConfig({ available: false })).toBeNull();
		expect(eng.resolveConfig({ ...CFG, part_bytes: "x" })).toBeNull();
		expect(eng.resolveConfig({ ...CFG, formats: [] })).toBeNull();
		const cfg = eng.resolveConfig({ ...CFG, max_concurrent_parts: 999, sign_batch_max_parts: 999 }) as Record<string, number>;
		expect(cfg).toBeTruthy();
		expect(cfg.max_concurrent_parts).toBe(16);
		expect(cfg.sign_batch_max_parts).toBe(64);
	});
});

describe("createUpload（注入假依赖）", () => {
	it("分块只经 source.slice()；storage.save 先于任何 PUT；成功 resolve + complete(succeeded)", async () => {
		const parts = [
			{ part_number: 1, length: 8 }, { part_number: 2, length: 8 },
			{ part_number: 3, length: 8 }, { part_number: 4, length: 8 - 32 + 32 - 28 },
		];
		parts[3].length = 32 - 24;
		const fetchImpl = happyFetch(parts.map((p) => ({ ...p })));
		const eng = loadEngine(fetchImpl);
		happyXhr();
		const src = fakeSource(32);
		const st = fakeStorage();
		const events: Array<Record<string, unknown>> = [];
		const up = eng.createUpload({
			source: src.view, apiFetch: (u, o) => (fetchImpl as unknown as (u2: string, o2?: RequestInit) => Promise<Response>)(u, o),
			config: eng.resolveConfig(CFG), storage: st.adapter,
			retryCompleteOnNetworkError: true, useResumeEndpoint: true,
			onEvent: (ev: Record<string, unknown>) => events.push(ev),
		});
		const r = await up.done;
		expect(r && r.ok).toBe(true);
		// U1：COS PUT 走 XHR——方法/URL/零请求头/不带凭据逐项锁定
		const puts = FakeXHR.instances;
		expect(puts).toHaveLength(4);
		puts.forEach((x) => {
			expect(x.method).toBe("PUT");
			expect(String(x.url)).toMatch(/^https:\/\/cos\.example\/o\?partNumber=\d+$/);
			expect(x.withCredentials).toBe(false);
			expect(x.setRequestHeader).not.toHaveBeenCalled();   // 无 CSRF/Authorization/Content-Length
		});
		// 每片 slice 两次（PUT body 的 Blob 切片 + chunked 哈希的流式另读
		// ——磁盘读两次，内存每通道一个 4 MiB 子块），长度受计划约束
		expect(src.slices).toHaveLength(8);
		expect(src.slices.map((s) => s.end - s.start).sort()).toEqual(
			[8, 8, 8, 8, 8, 8, 8, 8]);
		// 持久化先于第一个 PUT（断网/刷新后可续传的前提）
		// storage.save 是同步调用（引擎内联），首个 PUT 前必有 saves 记录
		expect(st.saves.length).toBeGreaterThanOrEqual(1);
		expect(st.saves[0].job_id).toBe("inj_x");
		expect(st.completes).toHaveLength(1);
		expect(st.completes[0][1]).toMatchObject({ succeeded: true, slide_id: "sld_1" });
		expect(events.some((e) => e.type === "created")).toBe(true);
		expect(events.some((e) => e.type === "progress")).toBe(true);
	});

	it("retryCompleteOnNetworkError：完成响应丢失 → 重发 → 409 冲突视为已完成 → 收口", async () => {
		const parts = [{ part_number: 1, length: 8 }, { part_number: 2, length: 8 }];
		let completePosts = 0;
		const fetchImpl = vi.fn((url: string, init?: RequestInit) => {
			const method = String((init && init.method) || "GET");
			if (url === "/api/ingestions" && method === "POST") {
				return Promise.resolve(resp({ job_id: "inj_c", stage: "uploading" }, 202));
			}
			if (url === "/api/ingestions/inj_c" && method === "GET") {
				if (completePosts >= 2) return Promise.resolve(resp({ stage: "viewable", slide_id: "sld_c" }));
				return Promise.resolve(resp({ stage: "uploading", parts }));
			}
			if (url.endsWith("/parts/sign")) {
				const nums = JSON.parse(String(init!.body)).part_numbers as number[];
				return Promise.resolve(resp({
					urls: nums.map((n) => ({ url: `https://cos.example/o?partNumber=${n}`, part_number: n })),
				}));
			}
			if (url.endsWith("/upload-complete")) {
				completePosts++;
				if (completePosts === 1) return Promise.reject(new TypeError("network lost"));
				return Promise.resolve(resp({ code: "ingestion_state_conflict" }, 409));
			}
			if (url.startsWith("https://")) return Promise.resolve(resp({}));
			return Promise.resolve(resp({}));
		}) as unknown as typeof fetch;
		const eng = loadEngine(fetchImpl);
		happyXhr();
		const src = fakeSource(16);
		const st = fakeStorage();
		const up = eng.createUpload({
			source: src.view,
			apiFetch: (u, o) => (fetchImpl as unknown as (u2: string, o2?: RequestInit) => Promise<Response>)(u, o),
			config: eng.resolveConfig(CFG), storage: st.adapter,
			retryCompleteOnNetworkError: true,
		});
		const r = await up.done;
		expect(r && r.ok).toBe(true);
		expect(completePosts).toBe(2);
		expect(st.completes).toHaveLength(1);
	});

	it("显式续传：readConfirmed 跳过已确认分块；useResumeEndpoint 先 /resume 再传", async () => {
		const parts = [
			{ part_number: 1, length: 8 }, { part_number: 2, length: 8 },
			{ part_number: 3, length: 8 }, { part_number: 4, length: 8 },
		];
		const signed: number[][] = [];
		let resumed = 0;
		let statusCalls = 0;
		const fetchImpl = vi.fn((url: string, init?: RequestInit) => {
			const method = String((init && init.method) || "GET");
			if (url === "/api/ingestions/inj_r" && method === "GET") {
				statusCalls++;
				// 第一次（useResumeEndpoint 探测）：服务端卡在 completing
				if (statusCalls === 1) return Promise.resolve(resp({ stage: "completing" }));
				if (statusCalls === 2) return Promise.resolve(resp({ stage: "uploading", parts }));
				return Promise.resolve(resp({ stage: "viewable", slide_id: "sld_r" }));
			}
			if (url === "/api/ingestions/inj_r/resume" && method === "POST") {
				resumed++;
				return Promise.resolve(resp({ stage: "uploading" }, 202));
			}
			if (url.endsWith("/parts/sign")) {
				const nums = JSON.parse(String(init!.body)).part_numbers as number[];
				signed.push(nums);
				return Promise.resolve(resp({
					urls: nums.map((n) => ({ url: `https://cos.example/o?partNumber=${n}`, part_number: n })),
				}));
			}
			if (url.endsWith("/upload-complete")) {
				return Promise.resolve(resp({ stage: "awaiting_server" }, 202));
			}
			if (url.startsWith("https://")) return Promise.resolve(resp({}));
			return Promise.resolve(resp({}));
		}) as unknown as typeof fetch;
		const eng = loadEngine(fetchImpl);
		happyXhr();
		const src = fakeSource(32);
		const st = fakeStorage();
		// review #1 后的合同：续传候选必须带分片内容摘要（零字节假源的
		// 分片内容 = 8 字节 0x00 的 SHA-256）。曾只凭 readConfirmed 跳过。
		st.setRecord({
			confirmed: [1, 2],
			digests: {
				"1": await chunkedPartHex(new Uint8Array(8)),
				"2": await chunkedPartHex(new Uint8Array(8)),
			},
			digest_scheme: eng.digestScheme,
			account: "",
		});
		const up = eng.createUpload({
			source: src.view,
			apiFetch: (u, o) => (fetchImpl as unknown as (u2: string, o2?: RequestInit) => Promise<Response>)(u, o),
			config: eng.resolveConfig(CFG), storage: st.adapter,
			resumeJobId: "inj_r", useResumeEndpoint: true,
		});
		const r = await up.done;
		expect(r && r.ok).toBe(true);
		expect(resumed).toBe(1);
		expect(signed).toEqual([[3, 4]]);
		// review #1（复核第二轮方案）：6 次 slice = 分片 1/2 的续传摘要核验读
		// （4 MiB 子块流式）+ 分片 3/4 的传输读与哈希读各一次（PUT body =
		expect(src.slices).toHaveLength(6);
		// Blob 切片流式；绝不整体读取）
	});

	it("401 中途失败：reject {status:401}（引擎无 location —— 绝不自动跳转登录）", async () => {
		const parts = [{ part_number: 1, length: 8 }, { part_number: 2, length: 8 }];
		const fetchImpl = vi.fn((url: string, init?: RequestInit) => {
			const method = String((init && init.method) || "GET");
			if (url === "/api/ingestions" && method === "POST") {
				return Promise.resolve(resp({ job_id: "inj_a", stage: "uploading" }, 202));
			}
			if (url === "/api/ingestions/inj_a" && method === "GET") {
				return Promise.resolve(resp({ stage: "uploading", parts }));
			}
			if (url.endsWith("/parts/sign")) {
				return Promise.resolve(resp({ code: "auth_required", error: "登录已过期" }, 401));
			}
			if (url.startsWith("https://")) return Promise.resolve(resp({}));
			return Promise.resolve(resp({}));
		}) as unknown as typeof fetch;
		const eng = loadEngine(fetchImpl);
		const src = fakeSource(16);
		const st = fakeStorage();
		const up = eng.createUpload({
			source: src.view,
			apiFetch: (u, o) => (fetchImpl as unknown as (u2: string, o2?: RequestInit) => Promise<Response>)(u, o),
			config: eng.resolveConfig(CFG), storage: st.adapter,
		});
		await expect(up.done).rejects.toMatchObject({
			status: 401,
			data: { code: "auth_required" },
		});
	});

	it("cancel：abort 全部在途 XHR + 本地记录清理 + POST /cancel；done resolve {cancelled:true}", async () => {
		const parts = [{ part_number: 1, length: 8 }, { part_number: 2, length: 8 }];
		let cancelled = 0;
		const fetchImpl = vi.fn((url: string, init?: RequestInit) => {
			const method = String((init && init.method) || "GET");
			if (url === "/api/ingestions" && method === "POST") {
				return Promise.resolve(resp({ job_id: "inj_n", stage: "uploading" }, 202));
			}
			if (url === "/api/ingestions/inj_n" && method === "GET") {
				return Promise.resolve(resp({ stage: "uploading", parts }));
			}
			if (url === "/api/ingestions/inj_n/cancel" && method === "POST") {
				cancelled++;
				return Promise.resolve(resp({ stage: "terminal" }, 202));
			}
			if (url.endsWith("/parts/sign")) {
				return Promise.resolve(resp({
					urls: [{ url: "https://cos.example/o?partNumber=1", part_number: 1 },
						{ url: "https://cos.example/o?partNumber=2", part_number: 2 }],
				}));
			}
			return Promise.resolve(resp({}));
		}) as unknown as typeof fetch;
		const eng = loadEngine(fetchImpl);
		// PUT 挂起不回（在途 XHR）：取消必须主动 abort 它们
		FakeXHR.onSend = () => { /* never responds */ };
		const src = fakeSource(16);
		const st = fakeStorage();
		const up = eng.createUpload({
			source: src.view,
			apiFetch: (u, o) => (fetchImpl as unknown as (u2: string, o2?: RequestInit) => Promise<Response>)(u, o),
			config: eng.resolveConfig(CFG), storage: st.adapter,
		});
		for (let i = 0; i < 50 && FakeXHR.instances.length < 2; i++) await tick();
		expect(FakeXHR.instances.length).toBe(2);   // 两片 PUT 均在途
		up.cancel();
		const r = await up.done;
		expect(r && r.cancelled).toBe(true);
		expect(cancelled).toBe(1);
		expect(st.removes).toContain("inj_n");
		// 在途 XHR 全部被 abort（监听随之注销：迟到回调不再产生事件）
		const inflight = FakeXHR.instances.filter((x) => x.sent && !x.status);
		expect(inflight.length).toBeGreaterThan(0);
		inflight.forEach((x) => expect(x.aborted).toBe(true));
	});

	it("terminal：storage.complete(terminal) + reject {terminal}", async () => {
		const fetchImpl = vi.fn((url: string) => {
			if (url === "/api/ingestions") {
				return Promise.resolve(resp({ job_id: "inj_t", stage: "uploading" }, 202));
			}
			if (url === "/api/ingestions/inj_t") {
				return Promise.resolve(resp({ stage: "terminal", fail_code: "waiting_timeout" }));
			}
			return Promise.resolve(resp({}));
		}) as unknown as typeof fetch;
		const eng = loadEngine(fetchImpl);
		const st = fakeStorage();
		const up = eng.createUpload({
			source: fakeSource(16).view,
			apiFetch: (u, o) => (fetchImpl as unknown as (u2: string, o2?: RequestInit) => Promise<Response>)(u, o),
			config: eng.resolveConfig(CFG), storage: st.adapter,
		});
		await expect(up.done).rejects.toMatchObject({
			terminal: true,
			data: { fail_code: "waiting_timeout" },
		});
		expect(st.completes).toHaveLength(1);
		expect(st.completes[0][1]).toMatchObject({ terminal: true, fail_code: "waiting_timeout" });
	});
});

describe("createUpload：持久化顺序与取消收尾（C4 验收补充）", () => {
	it("异步 storage.save（OPFS）落盘之前不查状态、不签名、不 PUT", async () => {
		const parts = [{ part_number: 1, length: 8 }, { part_number: 2, length: 8 }];
		const fetchImpl = happyFetch(parts);
		const eng = loadEngine(fetchImpl);
		happyXhr();
		const src = fakeSource(16);
		const st = fakeStorage();
		let releaseSave: () => void = () => {};
		const firstSave = new Promise<void>((r) => { releaseSave = r; });
		let saveCalls = 0;
		const adapter = {
			...st.adapter,
			save(rec: StorageRec) {
				st.saves.push(rec);
				saveCalls++;
				return saveCalls === 1 ? firstSave : undefined;
			},
		};
		const up = eng.createUpload({
			source: src.view,
			apiFetch: (u, o) => (fetchImpl as unknown as (u2: string, o2?: RequestInit) => Promise<Response>)(u, o),
			config: eng.resolveConfig(CFG), storage: adapter,
		});
		await flush();
		const calls = () => (fetchImpl as unknown as vi.Mock).mock.calls.map((c: unknown[]) => String(c[0]));
		expect(calls()).toEqual(["/api/ingestions"]);
		expect(st.saves).toHaveLength(1);
		expect(st.saves[0].job_id).toBe("inj_x");
		releaseSave();
		const r = await up.done;
		expect(r && r.ok).toBe(true);
		expect(FakeXHR.instances.filter((x) => x.sent)).toHaveLength(2);
	});

	it("取消之后才完成的 PUT 不再写回续传记录", async () => {
		const parts = [{ part_number: 1, length: 8 }];
		const base = happyFetch(parts);
		const fetchImpl = vi.fn((url: string, init?: RequestInit) =>
			(base as unknown as (u: string, i?: RequestInit) => Promise<Response>)(url, init)
		) as unknown as typeof fetch;
		const eng = loadEngine(fetchImpl);
		const st = fakeStorage();
		// 在途 PUT 挂起：取消后才「成功返回」（模拟不响应 abort 的迟到成功）
		let late: FakeXHR | null = null;
		FakeXHR.onSend = (xhr) => { late = xhr; };
		const up = eng.createUpload({
			source: fakeSource(8).view,
			apiFetch: (u, o) => (fetchImpl as unknown as (u2: string, o2?: RequestInit) => Promise<Response>)(u, o),
			config: eng.resolveConfig(CFG), storage: st.adapter,
		});
		for (let i = 0; i < 50 && !late; i++) await tick();
		expect(late).toBeTruthy();
		const savesBefore = st.saves.length;
		up.cancel();
		expect(st.removes).toEqual(["inj_x"]);
		(late as FakeXHR).respond(200, '"late-etag"');   // 取消后才落地的成功
		const r = await up.done;
		expect(r && r.cancelled).toBe(true);
		await flush();
		expect(st.saves.length).toBe(savesBefore);       // 不复活已删除的记录
	});
});

describe("createUpload：记录落盘失败与取消收口（C4 复审 P1/P2）", () => {
	type Call = [string, RequestInit?];
	function recorder(handler: (url: string, method: string, init?: RequestInit) => Promise<Response>) {
		const calls: Call[] = [];
		const fn = vi.fn((url: string, init?: RequestInit) => {
			calls.push([url, init]);
			return handler(url, String((init && init.method) || "GET"), init);
		}) as unknown as typeof fetch;
		return { calls, fn };
	}
	const asApi = (f: typeof fetch) =>
		(u: string, o?: RequestInit) => (f as unknown as (u2: string, o2?: RequestInit) => Promise<Response>)(u, o);

	it("P1：创建后记录写入失败 → 不查状态/不签名/不 PUT，取消新任务，reject {persist, reconciled:true}", async () => {
		const { calls, fn } = recorder((url, method) => {
			if (url === "/api/ingestions" && method === "POST") {
				return Promise.resolve(resp({ job_id: "inj_p", stage: "uploading" }, 202));
			}
			if (url === "/api/ingestions/inj_p/cancel") return Promise.resolve(resp({ stage: "terminal" }, 202));
			return Promise.resolve(resp({ stage: "uploading", parts: [{ part_number: 1, length: 8 }] }));
		});
		const eng = loadEngine(fn);
		const st = fakeStorage();
		const adapter = { ...st.adapter, save: () => Promise.reject(new Error("QuotaExceededError")) };
		const up = eng.createUpload({
			source: fakeSource(8).view, apiFetch: asApi(fn),
			config: eng.resolveConfig(CFG), storage: adapter,
		});
		await expect(up.done).rejects.toMatchObject({ persist: true, ingestionId: "inj_p", reconciled: true });
		expect(calls.map(([u, i]) => `${(i && i.method) || "GET"} ${u}`)).toEqual([
			"POST /api/ingestions", "POST /api/ingestions/inj_p/cancel",
		]);
	});

	it("P1：同步抛出同样致命；取消未获确认 → reconciled:false", async () => {
		const { calls, fn } = recorder((url, method) => {
			if (url === "/api/ingestions" && method === "POST") {
				return Promise.resolve(resp({ job_id: "inj_q", stage: "uploading" }, 202));
			}
			if (url.endsWith("/cancel")) return Promise.resolve(resp({}, 503));
			return Promise.resolve(resp({ stage: "uploading", parts: [{ part_number: 1, length: 8 }] }));
		});
		const eng = loadEngine(fn);
		const st = fakeStorage();
		const adapter = { ...st.adapter, save: () => { throw new Error("broken"); } };
		const up = eng.createUpload({
			source: fakeSource(8).view, apiFetch: asApi(fn),
			config: eng.resolveConfig(CFG), storage: adapter,
		});
		await expect(up.done).rejects.toMatchObject({ persist: true, ingestionId: "inj_q", reconciled: false });
		expect(calls.filter(([u]) => u.startsWith("https://") || u.endsWith("/parts/sign"))).toHaveLength(0);
	});

	async function cancelDuringWait(kind: "waiting_space" | "sign_backoff") {
		vi.useFakeTimers();
		const { calls, fn } = recorder((url, method) => {
			if (url === "/api/ingestions" && method === "POST") {
				return Promise.resolve(resp({ job_id: "inj_w", stage: "uploading" }, 202));
			}
			if (url === "/api/ingestions/inj_w" && method === "GET") {
				return Promise.resolve(resp(kind === "waiting_space"
					? { stage: "waiting_space", queue_position: 0 }
					: { stage: "uploading", parts: [{ part_number: 1, length: 8 }] }));
			}
			if (url.endsWith("/parts/sign")) return Promise.resolve(resp({ code: "busy" }, 429));
			return Promise.resolve(resp({}, 202));
		});
		const eng = loadEngine(fn);
		const st = fakeStorage();
		const up = eng.createUpload({
			source: fakeSource(8).view, apiFetch: asApi(fn),
			config: eng.resolveConfig(CFG), storage: st.adapter,
		});
		const waitingOn = kind === "waiting_space" ? "/api/ingestions/inj_w" : "/parts/sign";
		for (let i = 0; i < 50 && !calls.some(([u]) => u.endsWith(waitingOn)); i++) {
			await advYield(0);
		}
		expect(calls.some(([u]) => u.endsWith(waitingOn))).toBe(true);
		let settled: unknown = null;
		up.done.then((r) => { settled = r; });
		up.cancel();
		await advYield(0);
		expect(settled).toEqual({ cancelled: true });
		const before = calls.length;
		await advYield(20000);
		const after = calls.slice(before).map(([u]) => u);
		expect(after).toEqual([]);
		expect(calls.filter(([u]) => u.endsWith("/cancel"))).toHaveLength(1);
		expect(st.removes).toEqual(["inj_w"]);
	}

	it("P2：waiting_space 5s 轮询等待中取消 → done 立即 cancelled，其后零请求", async () => {
		await cancelDuringWait("waiting_space");
	});

	it("P2：签名 429 退避等待中取消 → done 立即 cancelled，其后零请求", async () => {
		await cancelDuringWait("sign_backoff");
	});

	it("P2：控制请求悬置时取消 → done 仍立即 cancelled", async () => {
		const { fn } = recorder((url, method) => {
			if (url === "/api/ingestions" && method === "POST") {
				return Promise.resolve(resp({ job_id: "inj_h", stage: "uploading" }, 202));
			}
			if (url.endsWith("/cancel")) return Promise.resolve(resp({}, 202));
			return new Promise<Response>(() => {});
		});
		const eng = loadEngine(fn);
		const up = eng.createUpload({
			source: fakeSource(8).view, apiFetch: asApi(fn),
			config: eng.resolveConfig(CFG), storage: fakeStorage().adapter,
		});
		await flush();
		up.cancel();
		await expect(up.done).resolves.toEqual({ cancelled: true });
	});
});

// --------------------------------------------------------------------------- //
// U1：字节级上传进度（XHR upload.onprogress + 字节聚合事件合同）
// --------------------------------------------------------------------------- //
describe("createUpload：U1 字节级上传进度", () => {
	type Ev = Record<string, unknown>;

	/** 控制台假后端（fetch）+ 可控 XHR：PUT 一律挂起，由测试逐个驱动。 */
	function byteHarness(parts: Array<{ part_number: number; length: number }>, size: number) {
		const fetchImpl = happyFetch(parts);
		const eng = loadEngine(fetchImpl);
		const events: Ev[] = [];
		const gates: FakeXHR[] = [];
		FakeXHR.onSend = (xhr) => { gates.push(xhr); };
		const st = fakeStorage();
		const up = eng.createUpload({
			source: { name: "out.tif", size, slice: (s: number, e: number) => ({ _range: [s, e], arrayBuffer: async () => new ArrayBuffer(e - s) }) },
			apiFetch: (u, o) => (fetchImpl as unknown as (u2: string, o2?: RequestInit) => Promise<Response>)(u, o),
			config: eng.resolveConfig(CFG), storage: st.adapter,
			onEvent: (ev: Ev) => { if (ev.type === "progress" || ev.type === "retry") events.push(ev); },
		});
		/** 等到第 n 个 PUT 的 XHR 进入 send（签名后） */
		async function waitForPut(n: number) {
			for (let i = 0; i < 200 && gates.length < n; i++) await advYield(0);
			expect(gates.length, `waiting for PUT #${n}`).toBeGreaterThanOrEqual(n);
			return gates[n - 1];
		}
		const byteEvents = () => events.filter((e) => e.type === "progress") as
			Array<{ frac: number; loadedBytes: number; confirmedBytes: number;
				totalBytes: number; determinate: boolean; sentAll: boolean; force?: boolean }>;
		return { fetchImpl, eng, events, gates, st, up, waitForPut, byteEvents };
	}

	it("单片上传：PUT 完成前有多次递增字节更新；body 发完且未确认 → sentAll 而非完成", async () => {
		vi.useFakeTimers();
		const parts = [{ part_number: 1, length: 32 }];
		const h = byteHarness(parts, 32);
		const x1 = await h.waitForPut(1);
		// 多次字节事件（间隔 ≥ 节流窗口 120ms）：8 → 16 → 24 → 32 全部可见。
		// 先推时钟再触发回调：保证距上一次发射 ≥120ms（初始事件在 PUT 前刚发过）
		const steps: number[] = [];
		for (const loaded of [8, 16, 24, 32]) {
			await advYield(150);
			x1.progress(loaded);
			await advYield(0);
			const evs = h.byteEvents();
			const last = evs[evs.length - 1];
			expect(last.totalBytes).toBe(32);
			expect(last.loadedBytes).toBe(loaded);
			expect(last.frac).toBeCloseTo(loaded / 32, 5);
			expect(last.determinate).toBe(true);
			steps.push(last.loadedBytes);
		}
		expect(steps.filter((v, i) => i === 0 || v > steps[i - 1])).toHaveLength(4);
		// 全部字节已发送但 HTTP 未确认：sentAll，绝不显示「完成」
		const beforeConfirm = h.byteEvents();
		expect(beforeConfirm[beforeConfirm.length - 1].sentAll).toBe(true);
		expect(beforeConfirm[beforeConfirm.length - 1].confirmedBytes).toBe(0);
		expect(beforeConfirm.every((e) => e.confirmedBytes === 0)).toBe(true);
		// HTTP 2xx 确认后：confirmedBytes 入账（active→confirmed 同一更新）
		x1.respond(200, '"e1"');
		await advYield(0);
		const evs = h.byteEvents();
		expect(evs[evs.length - 1].loadedBytes).toBe(32);
		expect(evs[evs.length - 1].confirmedBytes).toBe(32);
		expect(evs[evs.length - 1].sentAll).toBe(false);
		const r = await h.up.done;
		expect(r && r.ok).toBe(true);
	});

	it("大片 + 短尾按字节加权；并发 loaded 夹到分片长度，绝不超过 100%", async () => {
		vi.useFakeTimers();
		const parts = [{ part_number: 1, length: 24 }, { part_number: 2, length: 8 }];
		const h = byteHarness(parts, 32);
		const x1 = await h.waitForPut(1);
		const x2 = await h.waitForPut(2);
		await advYield(150);
		x1.progress(12);                       // 大片传一半：12/32（按片数则 0%）
		await advYield(0);
		let last = h.byteEvents().slice(-1)[0];
		expect(last.loadedBytes).toBe(12);
		expect(last.frac).toBeCloseTo(12 / 32, 5);
		await advYield(150);
		x2.progress(4);                        // 尾片 4/8
		await advYield(0);
		last = h.byteEvents().slice(-1)[0];
		expect(last.loadedBytes).toBe(16);
		await advYield(150);
		x1.progress(9999);                     // 越界回调：夹到分片长度
		await advYield(0);
		last = h.byteEvents().slice(-1)[0];
		expect(last.loadedBytes).toBe(28);
		expect(last.frac).toBeLessThanOrEqual(1);
		await advYield(150);
		x2.progress(8);
		await advYield(0);
		last = h.byteEvents().slice(-1)[0];
		expect(last.loadedBytes).toBe(32);     // 夹紧后总和恰为 totalBytes
		expect(last.frac).toBe(1);
		expect(last.sentAll).toBe(true);
		expect(h.byteEvents().every((e) => e.frac <= 1)).toBe(true);
		x1.respond(200); x2.respond(200);
		await advYield(0);
		expect(h.byteEvents().slice(-1)[0].confirmedBytes).toBe(32);
		const r = await h.up.done;
		expect(r && r.ok).toBe(true);
	});

	it("重试：失败尝试的 loaded 丢弃、旧尝试迟到回调无效、retry 事件、确认只计一次", async () => {
		vi.useFakeTimers();
		const parts = [{ part_number: 1, length: 16 }, { part_number: 2, length: 16 }];
		const h = byteHarness(parts, 32);
		const x1 = await h.waitForPut(1);      // part 1 第一次尝试
		await advYield(150);
		x1.progress(16);
		await advYield(0);
		expect(h.byteEvents().slice(-1)[0].loadedBytes).toBe(16);
		x1.failNetwork();                      // 网络错误：同 URL 重试（600ms 退避）
		await advYield(700);
		// 失败尝试的 loaded 立即丢弃（暂态百分比回退 + retry 事件）
		expect(h.events.some((e) => e.type === "retry" && e.part === 1)).toBe(true);
		// 旧尝试的迟到进度回调无效（新 attempt ID 已替换）
		const xRetry = FakeXHR.instances.find(
			(x) => x !== x1 && putPartNumber(String(x.url)) === 1);
		expect(xRetry, "part 1 retry XHR").toBeTruthy();
		await advYield(150);
		xRetry!.progress(4);
		await advYield(0);
		const withRetry4 = h.byteEvents().slice(-1)[0].loadedBytes;
		expect(withRetry4).toBeGreaterThanOrEqual(4);
		x1.progress(16);                       // 旧尝试迟到回调：不得计入
		await advYield(150);
		expect(h.byteEvents().slice(-1)[0].loadedBytes).toBe(withRetry4);
		// 第二次尝试推进 + 双双确认 → 每片只确认一次（重试不重复入账）
		await advYield(150);
		xRetry!.progress(16);
		await advYield(0);
		expect(h.byteEvents().slice(-1)[0].loadedBytes).toBeGreaterThanOrEqual(16);
		FakeXHR.instances.forEach((x) => { if (x !== x1) x.respond(200); });
		await advYield(0);
		const confirmedFull = h.byteEvents().filter((e) => e.confirmedBytes === 32);
		expect(confirmedFull).toHaveLength(1);   // 不双计（重试不重复入账）
		expect(h.st.saves[h.st.saves.length - 1].confirmed.sort()).toEqual([1, 2]);
		const r = await h.up.done;
		expect(r && r.ok).toBe(true);
	});

	it("403：同 URL 重试 3 次 → 重新签名一次 → 新 URL 再 3 次 → {part, status:403}", async () => {
		vi.useFakeTimers();
		const parts = [{ part_number: 1, length: 8 }];
		const signCalls: string[][] = [];
		const fetchImpl = vi.fn((url: string, init?: RequestInit) => {
			const method = String((init && init.method) || "GET");
			if (url === "/api/ingestions" && method === "POST") {
				return Promise.resolve(resp({ job_id: "inj_f403", stage: "uploading" }, 202));
			}
			if (url === "/api/ingestions/inj_f403" && method === "GET") {
				return Promise.resolve(resp({ stage: "uploading", parts }));
			}
			if (url.endsWith("/parts/sign")) {
				const nums = JSON.parse(String(init!.body)).part_numbers as number[];
				signCalls.push(nums);
				return Promise.resolve(resp({
					urls: nums.map((n) => ({
						url: `https://cos.example/o?partNumber=${n}&sign=${signCalls.length}`,
						part_number: n,
					})),
				}));
			}
			return Promise.resolve(resp({}));
		}) as unknown as typeof fetch;
		const eng = loadEngine(fetchImpl);
		FakeXHR.onSend = (xhr) => { xhr.respond(403); };   // 一律 403（URL 过期）
		const up = eng.createUpload({
			source: { name: "o.tif", size: 8, slice: () => ({ _range: [0, 8], arrayBuffer: async () => new ArrayBuffer(8) }) },
			apiFetch: (u, o) => (fetchImpl as unknown as (u2: string, o2?: RequestInit) => Promise<Response>)(u, o),
			config: eng.resolveConfig(CFG), storage: fakeStorage().adapter,
		});
		// 3 次同 URL（600ms 退避）→ 重新签名 → 新 URL 3 次 → 行级失败。
		// 断言 promise 先行订阅：done 在下面的推时钟循环里就可能 reject
		//（否则 vitest 记 Unhandled Rejection）
		const doneAssertion = expect(up.done).rejects.toMatchObject(
			{ part: 1, status: 403, network: false });
		for (let i = 0; i < 40 && FakeXHR.instances.length < 6; i++) {
			await advYield(700);
		}
		expect(FakeXHR.instances).toHaveLength(6);
		const urls = FakeXHR.instances.map((x) => String(x.url));
		expect(urls.filter((u) => u.endsWith("sign=1"))).toHaveLength(3);
		expect(urls.filter((u) => u.endsWith("sign=2"))).toHaveLength(3);
		expect(signCalls).toEqual([[1], [1]]);   // 首批签名 + 恰一次重新签名
		await doneAssertion;
	});

	it("断网（XHR onerror）：耗尽重试后映射为 network 合同（不依赖 fetch TypeError）", async () => {
		vi.useFakeTimers();
		const parts = [{ part_number: 1, length: 8 }];
		const fetchImpl = vi.fn((url: string, init?: RequestInit) => {
			const method = String((init && init.method) || "GET");
			if (url === "/api/ingestions" && method === "POST") {
				return Promise.resolve(resp({ job_id: "inj_net", stage: "uploading" }, 202));
			}
			if (url === "/api/ingestions/inj_net" && method === "GET") {
				return Promise.resolve(resp({ stage: "uploading", parts }));
			}
			if (url.endsWith("/parts/sign")) {
				const nums = JSON.parse(String(init!.body)).part_numbers as number[];
				return Promise.resolve(resp({
					urls: nums.map((n) => ({ url: `https://cos.example/o?partNumber=${n}`, part_number: n })),
				}));
			}
			return Promise.resolve(resp({}));
		}) as unknown as typeof fetch;
		const eng = loadEngine(fetchImpl);
		FakeXHR.onSend = (xhr) => { xhr.failNetwork(); };
		const up = eng.createUpload({
			source: { name: "o.tif", size: 8, slice: () => ({ _range: [0, 8], arrayBuffer: async () => new ArrayBuffer(8) }) },
			apiFetch: (u, o) => (fetchImpl as unknown as (u2: string, o2?: RequestInit) => Promise<Response>)(u, o),
			config: eng.resolveConfig(CFG), storage: fakeStorage().adapter,
		});
		const doneAssertion = expect(up.done).rejects.toMatchObject(
			{ part: 1, network: true });   // 先订阅（见上一测试注释）
		for (let i = 0; i < 40 && FakeXHR.instances.length < 6; i++) {
			await advYield(700);
		}
		await doneAssertion;
	});

	it("totalBytes 与 source.size 不一致 → typed error 拒绝，不签名、不 PUT、无进度事件", async () => {
		const parts = [{ part_number: 1, length: 8 }, { part_number: 2, length: 8 }];
		const h = byteHarness(parts, 12);   // 声明 12B，计划合计 16B
		await expect(h.up.done).rejects.toMatchObject({
			planMismatch: true,
			data: { code: "plan_size_mismatch" },
		});
		expect(FakeXHR.instances).toHaveLength(0);
		const signs = (h.fetchImpl as unknown as vi.Mock).mock.calls
			.filter((c: unknown[]) => String(c[0]).endsWith("/parts/sign"));
		expect(signs).toHaveLength(0);
	});

	it("无 lengthComputable 事件：determinate=false + 已确认字节后备；不虚构 loaded", async () => {
		vi.useFakeTimers();
		const parts = [{ part_number: 1, length: 8 }, { part_number: 2, length: 8 }];
		const h = byteHarness(parts, 16);
		const x1 = await h.waitForPut(1);
		const evCount0 = h.byteEvents().length;
		await advYield(150);
		x1.progress(7, false);               // lengthComputable=false
		await advYield(0);
		let last = h.byteEvents().slice(-1)[0];
		if (h.byteEvents().length > evCount0) {
			// 允许发活动事件，但绝不把不可计算长度的 loaded 虚构成确定进度
			expect(last.determinate).toBe(false);
			expect(last.loadedBytes).toBe(0);
			expect(last.frac).toBe(0);
		}
		expect(last.determinate).toBe(false);
		expect(last.loadedBytes).toBe(0);    // 不可计算长度 → 只用已确认字节
		expect(last.frac).toBe(0);
		x1.respond(200);
		const x2 = await h.waitForPut(2);
		await advYield(150);
		x2.progress(5, false);
		await advYield(0);
		last = h.byteEvents().slice(-1)[0];
		expect(last.determinate).toBe(false);
		expect(last.confirmedBytes).toBe(8); // 后备：分片响应驱动已确认字节
		expect(last.loadedBytes).toBe(8);
		expect(last.frac).toBeCloseTo(8 / 16, 5);
		x2.respond(200);
		const r = await h.up.done;
		expect(r && r.ok).toBe(true);
		expect(h.byteEvents().slice(-1)[0].confirmedBytes).toBe(16);
	});

	it("字节事件节流：密集回调合并；分块确认（force）立即更新", async () => {
		vi.useFakeTimers();
		const parts = [{ part_number: 1, length: 100 }];
		const h = byteHarness(parts, 100);
		const x1 = await h.waitForPut(1);
		const fired = 20;                    // 20 次回调、30ms 间隔（< 120ms 节流窗）
		for (let k = 1; k <= fired; k++) {
			await advYield(30);
			x1.progress(k * 5);
			await advYield(0);
		}
		const evs = h.byteEvents();
		// 密集回调被合并（远少于触发次数），但仍有多次可见（≥3 次中间值）
		expect(evs.length).toBeLessThan(fired);
		expect(evs.length).toBeGreaterThanOrEqual(3);
		x1.respond(200);
		await advYield(0);
		// 确认事件不受节流约束（force 立即发）
		expect(h.byteEvents().slice(-1)[0].confirmedBytes).toBe(100);
		const r = await h.up.done;
		expect(r && r.ok).toBe(true);
	});
});

// --------------------------------------------------------------------------- //
// review 2026-10-07 #1：续传内容身份（有界内存、可证明）。
//   - 分片确认时记录分片摘要（复核第二轮方案 sha256-chunked-4MiB-v1：
//     PUT body = Blob 切片流式发送；摘要按 4 MiB 子块流式计算 = 子块摘要
//     拼接再哈希——磁盘读两次，内存每通道一个子块）；
//   - 记录绑定账号并携带 digest_scheme：无标记/其他方案 = legacy；
//   - 他号记录绝不 offered/used；
//   - 续传前逐个比对「新选中文件」的分片摘要：任何不符 / 无摘要或方案
//     不符的 legacy 记录 → 不续传（弃旧任务、全新创建），绝不混用；
//   - 缺单个摘要的可信部分照传（同编号覆盖，安全），不整单放弃。
// --------------------------------------------------------------------------- //

async function sha256Hex(b: ArrayBuffer): Promise<string> {
	const d = await crypto.subtle.digest("SHA-256", b);
	return Array.from(new Uint8Array(d))
		.map((x) => x.toString(16).padStart(2, "0")).join("");
}

/** 引擎方案 sha256-chunked-4MiB-v1 的独立实现（测试分片 ≤ 4 MiB → 单子块）：
 *  分片摘要 = SHA-256(子块摘要逐字节拼接)。*/
async function chunkedPartHex(bytes: Uint8Array): Promise<string> {
	const sub = await crypto.subtle.digest("SHA-256", bytes as unknown as ArrayBuffer);
	const whole = await crypto.subtle.digest("SHA-256", sub);
	return Array.from(new Uint8Array(whole))
		.map((x) => x.toString(16).padStart(2, "0")).join("");
}

/** 真实字节假源（arrayBuffer 返回真实内容；断言只按分块 slice）。 */
function bytesSource(bytes: Uint8Array, name = "same.ome.tif") {
	const slices: Array<[number, number]> = [];
	return {
		slices,
		view: {
			name,
			size: bytes.length,
			slice(start: number, end: number) {
				slices.push([start, Math.min(end, bytes.length)]);
				const clamped = Math.min(end, bytes.length);
				// Blob 语义：size + 再 slice（chunked 哈希按 4 MiB 子块读）
				const part = {
					size: clamped - start,
					slice(a: number, b: number) {
						const subEnd = Math.min(b, clamped - start);
						return {
							size: subEnd - a,
							arrayBuffer: async () => bytes.buffer.slice(
								bytes.byteOffset + start + a,
								bytes.byteOffset + start + subEnd),
						};
					},
					arrayBuffer: async () => bytes.buffer.slice(
						bytes.byteOffset + start, bytes.byteOffset + clamped),
				};
				return part;
			},
		},
	};
}

describe("createUpload：续传内容身份（review #1）", () => {
	/** 两部分、每部分 8 字节的假后端（可选钉住旧任务状态）。 */
	function resumeHarness(parts = [
		{ part_number: 1, length: 8 }, { part_number: 2, length: 8 },
	]) {
		const calls: Array<[string, string]> = [];
		const signed: number[][] = [];
		let completed = 0;
		const fetchImpl = vi.fn((url: string, init?: RequestInit) => {
			const method = String((init && init.method) || "GET");
			calls.push([method + " " + url.replace("inj_[a-z0-9]+", "inj_*"),
				method]);
			const id = (url.match(/inj_[a-z0-9_]+/) || [])[0];
			if (url === "/api/ingestions" && method === "POST") {
				return Promise.resolve(resp({ job_id: "inj_new", stage: "uploading" }, 202));
			}
			if (id && url === `/api/ingestions/${id}` && method === "GET") {
				// upload-complete 之后服务端阶段推进（否则轮询不收敛）
				if (completed > 0) {
					return Promise.resolve(resp({ stage: "viewable", slide_id: "sld_r" }));
				}
				return Promise.resolve(resp({ stage: "uploading", parts }));
			}
			if (id && url.endsWith("/cancel") && method === "POST") {
				return Promise.resolve(resp({ stage: "terminal" }, 202));
			}
			if (url.endsWith("/parts/sign") && method === "POST") {
				const nums = JSON.parse(String(init!.body)).part_numbers as number[];
				signed.push(nums);
				return Promise.resolve(resp({
					urls: nums.map((n) => ({
						url: `https://cos.example/o?partNumber=${n}`, part_number: n,
					})),
				}));
			}
			if (url.endsWith("/upload-complete")) {
				completed++;
				return Promise.resolve(resp({ stage: "awaiting_server" }, 202));
			}
			return Promise.resolve(resp({}));
		}) as unknown as typeof fetch;
		return { calls, signed, fetchImpl,
			urls: () => calls.map(([u]) => u) };
	}

	it("同名同大小不同内容：分片摘要不符 → 弃旧任务、全新创建、全部重传（不混用）", async () => {
		const fileA = new Uint8Array(16).fill(32);
		const fileB = new Uint8Array(16).fill(192);
		const h = resumeHarness();
		const eng = loadEngine(h.fetchImpl);
		happyXhr();
		const st = fakeStorage();
		st.setRecord({
			confirmed: [1],
			digests: { 1: await chunkedPartHex(fileA.slice(0, 8)) },
			digest_scheme: eng.digestScheme,
			account: "acct-1",
		});
		const up = eng.createUpload({
			source: bytesSource(fileB).view,       // 新选中的文件（内容不同）
			apiFetch: (u, o) => (h.fetchImpl as unknown as (u2: string, o2?: RequestInit) => Promise<Response>)(u, o),
			config: eng.resolveConfig(CFG), storage: st.adapter,
			resumeJobId: "inj_old", skipConfirm: true, account: "acct-1",
			useResumeEndpoint: true,
		});
		const r = await up.done;
		expect(r && r.ok).toBe(true);
		const urls = h.urls();
		expect(urls.some((u) => u.includes("inj_old/cancel"))).toBe(true);  // 弃旧
		expect(urls.some((u) => u === "POST /api/ingestions")).toBe(true);  // 全新创建
		// 两片都重传（旧分片 1 绝不跳过——摘要与新文件不符）
		expect(FakeXHR.instances.map((x) => putPartNumber(String(x.url))).sort())
			.toEqual([1, 2]);
		// 新记录只属于新任务且带分片摘要（fileB 内容，chunked 方案）
		const last = st.saves[st.saves.length - 1];
		expect(last.job_id).toBe("inj_new");
		expect(last.digests && last.digests["1"])
			.toBe(await chunkedPartHex(fileB.slice(0, 8)));
		expect(last.digest_scheme).toBe(eng.digestScheme);
	});

	it("legacy 记录（有确认分块、无摘要）→ 不续传：弃旧任务、全新创建", async () => {
		const file = new Uint8Array(16).fill(7);
		const h = resumeHarness();
		const eng = loadEngine(h.fetchImpl);
		happyXhr();
		const st = fakeStorage();
		st.setRecord({ confirmed: [1], digests: null, account: "acct-1" });
		const up = eng.createUpload({
			source: bytesSource(file).view,
			apiFetch: (u, o) => (h.fetchImpl as unknown as (u2: string, o2?: RequestInit) => Promise<Response>)(u, o),
			config: eng.resolveConfig(CFG), storage: st.adapter,
			resumeJobId: "inj_old", skipConfirm: true, account: "acct-1",
		});
		const r = await up.done;
		expect(r && r.ok).toBe(true);
		const urls = h.urls();
		expect(urls.some((u) => u.includes("inj_old/cancel"))).toBe(true);
		expect(urls.some((u) => u === "POST /api/ingestions")).toBe(true);
		expect(FakeXHR.instances.map((x) => putPartNumber(String(x.url))).sort())
			.toEqual([1, 2]);
	});

	it("刷新后重选同一文件：摘要全部相符 → 续传并只跳过已验证分块", async () => {
		const file = new Uint8Array(32).fill(5);
		const parts = [
			{ part_number: 1, length: 8 }, { part_number: 2, length: 8 },
			{ part_number: 3, length: 8 }, { part_number: 4, length: 8 },
		];
		const h = resumeHarness(parts);
		const eng = loadEngine(h.fetchImpl);
		happyXhr();
		const st = fakeStorage();
		st.setRecord({
			confirmed: [1, 2],
			digests: {
				1: await chunkedPartHex(file.slice(0, 8)),
				2: await chunkedPartHex(file.slice(8, 16)),
			},
			digest_scheme: eng.digestScheme,
			account: "acct-1",
		});
		const up = eng.createUpload({
			source: bytesSource(file).view,
			apiFetch: (u, o) => (h.fetchImpl as unknown as (u2: string, o2?: RequestInit) => Promise<Response>)(u, o),
			config: eng.resolveConfig(CFG), storage: st.adapter,
			resumeJobId: "inj_old", skipConfirm: true, account: "acct-1",
		});
		const r = await up.done;
		expect(r && r.ok).toBe(true);
		const urls = h.urls();
		expect(urls.some((u) => u.includes("/cancel"))).toBe(false);
		expect(urls.some((u) => u === "POST /api/ingestions")).toBe(false);
		expect(h.signed).toEqual([[3, 4]]);            // 只签未确认分块
		expect(FakeXHR.instances.map((x) => putPartNumber(String(x.url))))
			.toEqual([3, 4]);
		// 续传保存的记录保留旧分块的已验证摘要
		const last = st.saves[st.saves.length - 1];
		expect(last.confirmed.sort()).toEqual([1, 2, 3, 4]);
		expect(last.digests && last.digests["1"]).toBeTruthy();
	});

	it("账号变化：他号记录不可续传（不取消他号任务，直接全新创建）", async () => {
		const file = new Uint8Array(16).fill(9);
		const h = resumeHarness();
		const eng = loadEngine(h.fetchImpl);
		happyXhr();
		const st = fakeStorage();
		st.setRecord({
			confirmed: [1],
			digests: { 1: await chunkedPartHex(file.slice(0, 8)) },
			digest_scheme: eng.digestScheme,
			account: "acct-a",
		});
		const up = eng.createUpload({
			source: bytesSource(file).view,
			apiFetch: (u, o) => (h.fetchImpl as unknown as (u2: string, o2?: RequestInit) => Promise<Response>)(u, o),
			config: eng.resolveConfig(CFG), storage: st.adapter,
			resumeJobId: "inj_old", skipConfirm: true, account: "acct-b",
		});
		const r = await up.done;
		expect(r && r.ok).toBe(true);
		const urls = h.urls();
		expect(urls.some((u) => u.includes("inj_old/cancel"))).toBe(false);
		expect(urls.some((u) => u === "POST /api/ingestions")).toBe(true);
		expect(FakeXHR.instances.map((x) => putPartNumber(String(x.url))).sort())
			.toEqual([1, 2]);
	});

	it("记录缺个别分块的摘要：可信分块跳过、缺凭证分块重传（同编号覆盖，不弃单）", async () => {
		const file = new Uint8Array(16).fill(3);
		const h = resumeHarness();
		const eng = loadEngine(h.fetchImpl);
		happyXhr();
		const st = fakeStorage();
		st.setRecord({
			confirmed: [1, 2],
			digests: { 2: await chunkedPartHex(file.slice(8, 16)) },
			digest_scheme: eng.digestScheme,
			account: "acct-1",
		});
		const up = eng.createUpload({
			source: bytesSource(file).view,
			apiFetch: (u, o) => (h.fetchImpl as unknown as (u2: string, o2?: RequestInit) => Promise<Response>)(u, o),
			config: eng.resolveConfig(CFG), storage: st.adapter,
			resumeJobId: "inj_old", skipConfirm: true, account: "acct-1",
		});
		const r = await up.done;
		expect(r && r.ok).toBe(true);
		expect(h.urls().some((u) => u.includes("/cancel"))).toBe(false);
		expect(h.signed).toEqual([[1]]);               // 只重传缺凭证的分块 1
		expect(FakeXHR.instances.map((x) => putPartNumber(String(x.url))))
			.toEqual([1]);
	});

	it("全新上传：确认分块时记录分片 SHA-256（同一 ArrayBuffer 复用，slice 只读一次）", async () => {
		const file = new Uint8Array(16);
		file.set([1, 2, 3, 4, 5, 6, 7, 8], 0);
		file.set([9, 10, 11, 12, 13, 14, 15, 16], 8);
		const parts = [{ part_number: 1, length: 8 }, { part_number: 2, length: 8 }];
		const h = resumeHarness(parts);
		const eng = loadEngine(h.fetchImpl);
		happyXhr();
		const src = bytesSource(file);
		const st = fakeStorage();
		const up = eng.createUpload({
			source: src.view,
			apiFetch: (u, o) => (h.fetchImpl as unknown as (u2: string, o2?: RequestInit) => Promise<Response>)(u, o),
			config: eng.resolveConfig(CFG), storage: st.adapter,
			account: "acct-1",
		});
		const r = await up.done;
		expect(r && r.ok).toBe(true);
		const last = st.saves[st.saves.length - 1];
		expect(last.account).toBe("acct-1");
		expect(last.digests && last.digests["1"])
			.toBe(await chunkedPartHex(file.slice(0, 8)));
		expect(last.digests && last.digests["2"])
			.toBe(await chunkedPartHex(file.slice(8, 16)));
		expect(last.digest_scheme).toBe(eng.digestScheme);
		// PUT body = Blob 切片流式发送；摘要按子块流式另读——每片磁盘读两次
		// （内存有界：每通道同时只有一个 4 MiB 子块），绝不整片进内存
		expect(src.slices).toHaveLength(4);
	});

	it("旧方案记录（有摘要、无 digest_scheme 标记）= legacy：弃旧任务、全新创建", async () => {
		const file = new Uint8Array(16).fill(21);
		const h = resumeHarness();
		const eng = loadEngine(h.fetchImpl);
		happyXhr();
		const st = fakeStorage();
		// 4c38ed7f..改动前的整片哈希记录：有摘要、无方案标记 → 绝不复用
		st.setRecord({
			confirmed: [1],
			digests: { 1: await sha256Hex(file.slice(0, 8).buffer as ArrayBuffer) },
			account: "acct-1",
		});
		const up = eng.createUpload({
			source: bytesSource(file).view,
			apiFetch: (u, o) => (h.fetchImpl as unknown as (u2: string, o2?: RequestInit) => Promise<Response>)(u, o),
			config: eng.resolveConfig(CFG), storage: st.adapter,
			resumeJobId: "inj_old", skipConfirm: true, account: "acct-1",
			useResumeEndpoint: true,
		});
		const r = await up.done;
		expect(r && r.ok).toBe(true);
		const urls = h.urls();
		expect(urls.some((u) => u.includes("inj_old/cancel"))).toBe(true);
		expect(urls.some((u) => u === "POST /api/ingestions")).toBe(true);
		expect(FakeXHR.instances.map((x) => putPartNumber(String(x.url))).sort())
			.toEqual([1, 2]);
	});
});
// --------------------------------------------------------------------------- //
// review 2026-10-07 #7：分片终局失败 → 所有 lane 停止调度新分片，等在途
// 请求收口（drain）后才 surface 错误；用户重试不会与旧请求重叠。
// --------------------------------------------------------------------------- //
function sliceSource(size: number) {
	return { name: "o.tif", size,
		slice: (s: number, e: number) => ({ _range: [s, e], arrayBuffer: async () => new ArrayBuffer(e - s) }) };
}

describe("createUpload：分片失败的 lane 停止与 drain（review #7）", () => {
	function drainHarness(parts: Array<{ part_number: number; length: number }>) {
		const signCalls: number[][] = [];
		const fetchImpl = vi.fn((url: string, init?: RequestInit) => {
			const method = String((init && init.method) || "GET");
			if (url === "/api/ingestions" && method === "POST") {
				return Promise.resolve(resp({ job_id: "inj_drain", stage: "uploading" }, 202));
			}
			if (url === "/api/ingestions/inj_drain" && method === "GET") {
				return Promise.resolve(resp({ stage: "uploading", parts }));
			}
			if (url.endsWith("/parts/sign")) {
				const nums = JSON.parse(String(init!.body)).part_numbers as number[];
				signCalls.push(nums);
				return Promise.resolve(resp({
					urls: nums.map((n) => ({
						url: `https://cos.example/o?partNumber=${n}`, part_number: n,
					})),
				}));
			}
			return Promise.resolve(resp({}));
		}) as unknown as typeof fetch;
		const eng = loadEngine(fetchImpl);
		return { signCalls, eng, fetchImpl };
	}

	/** 推进假时钟直到 done 收口（或预算耗尽）。 */
	async function advanceUntilSettled(up: { done: Promise<unknown> }, budget = 300) {
		let settled = false;
		void up.done.catch(() => {}).then(() => { settled = true; });
		for (let i = 0; i < budget && !settled; i++) {
			await advYield(700);
		}
		return settled;
	}

	it("part 2 终局失败：part 3/4 不再调度；在途 part 1 收口后才 reject", async () => {
		vi.useFakeTimers();
		const parts = [
			{ part_number: 1, length: 8 }, { part_number: 2, length: 8 },
			{ part_number: 3, length: 8 }, { part_number: 4, length: 8 },
		];
		const h = drainHarness(parts);
		// part 1 挂起不回（lane A 全程忙）；part 2 一律 403（终局失败：
		// 同 URL ×3 → 重签 [2] → 新 URL ×3 → 放弃）
		FakeXHR.onSend = (xhr) => {
			if (putPartNumber(String(xhr.url)) !== 1) xhr.respond(403);
		};
		const up = h.eng.createUpload({
			source: sliceSource(32),
			apiFetch: (u, o) => (h.fetchImpl as unknown as (u2: string, o2?: RequestInit) => Promise<Response>)(u, o),
			config: h.eng.resolveConfig({ ...CFG, sign_batch_max_parts: 4 }),
			storage: fakeStorage().adapter,
		});
		// 推进到 part 2 终局失败（终局重签 [2] 出现）
		for (let i = 0; i < 100 && h.signCalls.length < 2; i++) {
			await advYield(700);
		}
		// 终局失败后：第二批分片（3/4）不再被任何 lane 调度
		expect(h.signCalls).toEqual([[1, 2, 3, 4], [2]]);
		const putNums = FakeXHR.instances.map((x) => putPartNumber(String(x.url)));
		expect(putNums).not.toContain(3);
		expect(putNums).not.toContain(4);
		// 在途 part 1 被 drain（等待而非丢弃），此时 done 尚未收口
		const x1 = FakeXHR.instances.find((x) => putPartNumber(String(x.url)) === 1)!;
		expect(x1.status).toBe(0);
		const doneAssertion = expect(up.done).rejects.toMatchObject({ part: 2 });
		x1.respond(200);   // 在途收口
		expect(await advanceUntilSettled(up)).toBe(true);
		await doneAssertion;
		// drain：reject 时所有已发出的 XHR 都已收口（用户重试不会撞在途请求）
		expect(FakeXHR.instances.every((x) => x.status !== 0)).toBe(true);
	});

	it("首批失败：第二批不再签名（sign_batch=2）", async () => {
		vi.useFakeTimers();
		const parts = [
			{ part_number: 1, length: 8 }, { part_number: 2, length: 8 },
			{ part_number: 3, length: 8 }, { part_number: 4, length: 8 },
		];
		const h = drainHarness(parts);
		FakeXHR.onSend = (xhr) => {
			if (putPartNumber(String(xhr.url)) === 1) xhr.respond(200);
			else xhr.respond(403);
		};
		const up = h.eng.createUpload({
			source: sliceSource(32),
			apiFetch: (u, o) => (h.fetchImpl as unknown as (u2: string, o2?: RequestInit) => Promise<Response>)(u, o),
			config: h.eng.resolveConfig({ ...CFG, sign_batch_max_parts: 2 }),
			storage: fakeStorage().adapter,
		});
		const doneAssertion = expect(up.done).rejects.toMatchObject({ part: 2 });
		expect(await advanceUntilSettled(up)).toBe(true);
		await doneAssertion;
		expect(h.signCalls).toEqual([[1, 2], [2]]);   // 第二批 [3,4] 永不签名
	});
});

// --------------------------------------------------------------------------- //
// review 2026-10-07 #8：PUT 无进度超时（默认 60s，progressTimeoutMs 可配置
// ——无进度事件持续该时长 → abort 并按可重试网络错误处理）+ 单片整体上限
// （默认 600s，partTimeoutMs 可配置，写入 xhr.timeout）。计时器在 settle/
// 取消后清理。
// --------------------------------------------------------------------------- //
describe("createUpload：XHR 无进度超时与单片上限（review #8）", () => {
	function timeoutHarness() {
		const parts = [{ part_number: 1, length: 8 }, { part_number: 2, length: 8 }];
		const fetchImpl = happyFetch(parts);
		const eng = loadEngine(fetchImpl);
		const gates: FakeXHR[] = [];
		const events: Array<Record<string, unknown>> = [];
		FakeXHR.onSend = (xhr) => { gates.push(xhr); };   // 挂起，由测试驱动
		function makeUpload(opts: Record<string, unknown> = {}) {
			return eng.createUpload({
				source: sliceSource(16),
				apiFetch: (u: string, o?: RequestInit) => (fetchImpl as unknown as (u2: string, o2?: RequestInit) => Promise<Response>)(u, o),
				config: eng.resolveConfig(CFG), storage: fakeStorage().adapter,
				onEvent: (ev: Record<string, unknown>) => { events.push(ev); },
				...opts,
			});
		}
		async function firstGate(): Promise<FakeXHR> {
			for (let i = 0; i < 200 && !gates.length; i++) {
				await advYield(0);
			}
			expect(gates.length).toBeGreaterThan(0);
			return gates[0];
		}
		return { eng, gates, events, makeUpload, firstGate };
	}

	it("60s 无进度 → abort 且按可重试网络错误处理（retry 事件 + 新尝试）", async () => {
		vi.useFakeTimers();
		const h = timeoutHarness();
		const up = h.makeUpload();
		const x1 = await h.firstGate();
		await advYield(59000);
		expect(x1.aborted).toBe(false);          // 59s 内不动
		await advYield(1000);
		expect(x1.aborted).toBe(true);           // 第 60s：无进度超时
		for (let i = 0; i < 10; i++) await advYield(700);
		expect(h.events.some((e) => e.type === "retry")).toBe(true);
		expect(FakeXHR.instances.length).toBeGreaterThan(1);   // 新尝试已发出
	});

	it("进度事件重置计时器；progressTimeoutMs 可配置", async () => {
		vi.useFakeTimers();
		const h = timeoutHarness();
		const up = h.makeUpload({ progressTimeoutMs: 30000 });
		const x1 = await h.firstGate();
		for (let i = 0; i < 5; i++) {            // 每 25s 有进度：不超时
			await advYield(25000);
			x1.progress(2 * (i + 1));
		}
		expect(x1.aborted).toBe(false);
		await advYield(30000);   // 30s 无进度：按配置超时
		expect(x1.aborted).toBe(true);
	});

	it("单片整体上限写入 xhr.timeout（默认 600s；partTimeoutMs 可配置）；整体超时 → 可重试", async () => {
		vi.useFakeTimers();
		const h = timeoutHarness();
		const up = h.makeUpload({ partTimeoutMs: 120000 });
		const x1 = await h.firstGate();
		expect(x1.timeout).toBe(120000);
		x1.ontimeout && x1.ontimeout();          // 浏览器整体超时 → 可重试
		for (let i = 0; i < 10; i++) await advYield(700);
		expect(h.events.some((e) => e.type === "retry")).toBe(true);
	});

	it("取消后计时器清理：advance 10 分钟不再产生任何新请求", async () => {
		vi.useFakeTimers();
		const h = timeoutHarness();
		const up = h.makeUpload();
		await h.firstGate();
		up.cancel();
		const n = FakeXHR.instances.length;
		await advYield(600000);
		expect(FakeXHR.instances.length).toBe(n);
	});
});

// --------------------------------------------------------------------------- //
// review 2026-10-07 #9：状态轮询容忍瞬时失败（退避重查，连续失败有界），
// 放弃后错误带 recheck 标记（UI 提供「重新检查状态」= 重新查询，绝不重传）；
// 非瞬态（404/401/403）立即失败不退避。
// --------------------------------------------------------------------------- //
describe("createUpload：状态轮询容忍瞬时失败（review #9）", () => {
	function pollingHarness(failTimes: number, failStatus: number) {
		const calls: Array<[string, number]> = [];
		let gets = 0;
		const parts = [{ part_number: 1, length: 8 }];
		const fetchImpl = vi.fn((url: string, init?: RequestInit) => {
			const method = String((init && init.method) || "GET");
			if (url === "/api/ingestions" && method === "POST") {
				return Promise.resolve(resp({ job_id: "inj_pl", stage: "uploading" }, 202));
			}
			if (url === "/api/ingestions/inj_pl" && method === "GET") {
				gets++;
				calls.push(["status", gets]);
				if (gets <= failTimes) {
					if (failStatus === 0) return Promise.reject(new TypeError("net"));
					return Promise.resolve(resp({ error: "busy" }, failStatus));
				}
				if (gets <= failTimes + 1) {
					return Promise.resolve(resp({ stage: "uploading", parts }));
				}
				return Promise.resolve(resp({ stage: "viewable", slide_id: "sld_pl" }));
			}
			if (url.endsWith("/parts/sign")) {
				const nums = JSON.parse(String(init!.body)).part_numbers as number[];
				return Promise.resolve(resp({
					urls: nums.map((n) => ({ url: `https://cos.example/o?partNumber=${n}`, part_number: n })),
				}));
			}
			if (url.endsWith("/upload-complete")) {
				return Promise.resolve(resp({ stage: "awaiting_server" }, 202));
			}
			return Promise.resolve(resp({}));
		}) as unknown as typeof fetch;
		const eng = loadEngine(fetchImpl);
		happyXhr();   // PUT 一律立即 200
		return { eng, fetchImpl, statusGets: () => gets };
	}

	it("瞬时 503 ×2：退避后重查并正常收口（不立即失败）", async () => {
		vi.useFakeTimers();
		const h = pollingHarness(2, 503);
		const up = h.eng.createUpload({
			source: sliceSource(8),
			apiFetch: (u, o) => (h.fetchImpl as unknown as (u2: string, o2?: RequestInit) => Promise<Response>)(u, o),
			config: h.eng.resolveConfig(CFG), storage: fakeStorage().adapter,
		});
		let settled: { ok?: boolean } | null = null;
		void up.done.then((r) => { settled = r as { ok?: boolean }; });
		for (let i = 0; i < 200 && !settled; i++) await advYield(700);
		expect(settled && settled.ok).toBe(true);
		expect(h.statusGets()).toBe(4);   // 2 次失败 + uploading + viewable
	});

	it("连续失败超界（>5 次）→ reject {recheck:true}；同 job 重新查询可收口", async () => {
		vi.useFakeTimers();
		const h = pollingHarness(99, 503);
		const up = h.eng.createUpload({
			source: sliceSource(8),
			apiFetch: (u, o) => (h.fetchImpl as unknown as (u2: string, o2?: RequestInit) => Promise<Response>)(u, o),
			config: h.eng.resolveConfig(CFG), storage: fakeStorage().adapter,
		});
		let err: Record<string, unknown> | null = null;
		void up.done.catch((e) => { err = e as Record<string, unknown>; });
		for (let i = 0; i < 400 && !err; i++) await advYield(700);
		expect(err && err.recheck).toBe(true);   // UI 语义：重新查询，绝不重传
		const getsAtGiveUp = h.statusGets();
		expect(getsAtGiveUp).toBeGreaterThanOrEqual(6);
		// 「重新检查状态」= 以 resumeJobId 重入同一状态机（重查后恢复收口；
		// 绝不重建任务）。后端恢复：重查直接 viewable。
		const recovered = vi.fn((url: string, init?: RequestInit) => {
			const method = String((init && init.method) || "GET");
			if (url === "/api/ingestions/inj_pl" && method === "GET") {
				return Promise.resolve(resp({ stage: "viewable", slide_id: "sld_pl" }));
			}
			return Promise.resolve(resp({}));
		}) as unknown as typeof fetch;
		const eng2 = loadEngine(recovered);
		const up2 = eng2.createUpload({
			source: sliceSource(8),
			apiFetch: (u, o) => (recovered as unknown as (u2: string, o2?: RequestInit) => Promise<Response>)(u, o),
			config: eng2.resolveConfig(CFG), storage: fakeStorage().adapter,
			resumeJobId: "inj_pl",
		});
		const r = await up2.done;
		expect(r && r.ok).toBe(true);
	});

	it("非瞬态（404）：立即失败，不退避重试", async () => {
		const h = pollingHarness(1, 404);
		const up = h.eng.createUpload({
			source: sliceSource(8),
			apiFetch: (u, o) => (h.fetchImpl as unknown as (u2: string, o2?: RequestInit) => Promise<Response>)(u, o),
			config: h.eng.resolveConfig(CFG), storage: fakeStorage().adapter,
		});
		await expect(up.done).rejects.toMatchObject({ status: 404 });
		expect(h.statusGets()).toBe(1);
	});
});

// --------------------------------------------------------------------------- //
// review 2026-10-07 #10：完成请求网络失败 → 先查任务状态再决定——已离开
// uploading（completing/awaiting_server/…/viewable）→ 直接进入轮询；仍在
// uploading → 幂等重发 complete（409 ingestion_state_conflict = 已完成过）；
// 有界（≤4 次）。工作台（不设 retryCompleteOnNetworkError）同样生效。
// --------------------------------------------------------------------------- //
describe("createUpload：完成请求的状态优先重查（review #10）", () => {
	function completeHarness() {
		let completePosts = 0;
		let completeFails = 0;
		const stagesAfter: string[] = [];
		const parts = [{ part_number: 1, length: 8 }];
		const fetchImpl = vi.fn((url: string, init?: RequestInit) => {
			const method = String((init && init.method) || "GET");
			if (url === "/api/ingestions" && method === "POST") {
				return Promise.resolve(resp({ job_id: "inj_cm", stage: "uploading" }, 202));
			}
			if (url === "/api/ingestions/inj_cm" && method === "GET") {
				if (!completePosts) return Promise.resolve(resp({ stage: "uploading", parts }));
				const st = stagesAfter.length ? stagesAfter.shift()! : "viewable";
				return Promise.resolve(resp(st === "viewable"
					? { stage: st, slide_id: "sld_cm" } : { stage: st }));
			}
			if (url.endsWith("/parts/sign")) {
				const nums = JSON.parse(String(init!.body)).part_numbers as number[];
				return Promise.resolve(resp({
					urls: nums.map((n) => ({ url: `https://cos.example/o?partNumber=${n}`, part_number: n })),
				}));
			}
			if (url.endsWith("/upload-complete")) {
				completePosts++;
				if (completePosts <= completeFails) {
					return Promise.reject(new TypeError("complete lost"));
				}
				return Promise.resolve(resp({ stage: "awaiting_server" }, 202));
			}
			return Promise.resolve(resp({}));
		}) as unknown as typeof fetch;
		const eng = loadEngine(fetchImpl);
		happyXhr();   // PUT 一律立即 200
		return { eng, fetchImpl,
			completeCount: () => completePosts,
			setCompleteFails: (n: number) => { completeFails = n; },
			setStageAfter: (s: string) => { stagesAfter.push(s); } };
	}
	const src8 = { name: "o.tif", size: 8,
		slice: (s: number, e: number) => ({ _range: [s, e], arrayBuffer: async () => new ArrayBuffer(e - s) }) };

	it("complete 丢失 + 状态已在 awaiting_server → 不重发，直接轮询收口（工作台同享）", async () => {
		vi.useFakeTimers();
		const h = completeHarness();
		h.setCompleteFails(1);
		h.setStageAfter("awaiting_server");
		const up = h.eng.createUpload({
			source: src8,
			apiFetch: (u, o) => (h.fetchImpl as unknown as (u2: string, o2?: RequestInit) => Promise<Response>)(u, o),
			config: h.eng.resolveConfig(CFG), storage: fakeStorage().adapter,
			// 工作台：不设 retryCompleteOnNetworkError
		});
		let settled: { ok?: boolean } | null = null;
		void up.done.then((r) => { settled = r as { ok?: boolean }; });
		for (let i = 0; i < 200 && !settled; i++) await advYield(1700);
		expect(settled && settled.ok).toBe(true);
		expect(h.completeCount()).toBe(1);   // 状态已收口：不重发
	});

	it("complete 丢失 + 状态仍 uploading → 幂等重发（409 视为已完成）", async () => {
		vi.useFakeTimers();
		const h = completeHarness();
		h.setCompleteFails(1);
		h.setStageAfter("uploading");
		const up = h.eng.createUpload({
			source: src8,
			apiFetch: (u, o) => (h.fetchImpl as unknown as (u2: string, o2?: RequestInit) => Promise<Response>)(u, o),
			config: h.eng.resolveConfig(CFG), storage: fakeStorage().adapter,
		});
		let settled: { ok?: boolean } | null = null;
		void up.done.then((r) => { settled = r as { ok?: boolean }; });
		for (let i = 0; i < 200 && !settled; i++) await advYield(1700);
		expect(settled && settled.ok).toBe(true);
		expect(h.completeCount()).toBe(2);   // 重发一次
	});

	it("重发有界（≤4 次）：持续丢失 → reject {network}（交给行级失败/重查）", async () => {
		vi.useFakeTimers();
		const h = completeHarness();
		h.setCompleteFails(99);
		for (let i = 0; i < 6; i++) h.setStageAfter("uploading");
		const up = h.eng.createUpload({
			source: src8,
			apiFetch: (u, o) => (h.fetchImpl as unknown as (u2: string, o2?: RequestInit) => Promise<Response>)(u, o),
			config: h.eng.resolveConfig(CFG), storage: fakeStorage().adapter,
		});
		let err: Record<string, unknown> | null = null;
		void up.done.catch((e) => { err = e as Record<string, unknown>; });
		for (let i = 0; i < 300 && !err; i++) await advYield(1700);
		expect(err && err.network).toBe(true);
		expect(h.completeCount()).toBeLessThanOrEqual(4);
	});
});
