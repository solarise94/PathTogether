/**
 * C4 共享 COS 上传引擎单元测试（static/upload/cos-uploader.js）。
 *
 * 与 cos-upload.test.ts（app.js 工作台适配层）不同：这里直接以**注入的假
 * 依赖**（apiFetch/storage/source/回调）驱动 HP_COS_UPLOAD.createUpload，
 * 锁定引擎本身的合同——工具页（/tools/slides）依赖的行为都在这里：
 *
 *  1. 分块只经 source.slice()（绝不整体读取），COS PUT 独立传输（无 CSRF、
 *     credentials:omit、mode:cors）；
 *  2. storage.save 在任何分块 PUT 之前落任务记录（断网/刷新后可续传）；
 *  3. retryCompleteOnNetworkError=true：完成响应丢失 → 重发 complete，
 *     409 ingestion_state_conflict 视为已完成过 → 轮询收口；
 *  4. 显式续传 resumeJobId + readConfirmed：只签/只传未确认分块；
 *     useResumeEndpoint：服务端已离开 uploading → 先 POST /resume；
 *  5. 401 中途失败：reject（引擎无 location 访问——绝不自动跳转登录）；
 *  6. cancel：清本地记录 + POST /cancel + done resolve {cancelled:true}；
 *  7. terminal：storage.complete(terminal) + reject {terminal}。
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
};

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

/** 分块假源：只允许 slice 访问（整体读取会被计数并失败测试） */
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
				return { _range: [start, end] };
			},
		},
	};
}

function fakeStorage() {
	const saves: StorageRec[] = [];
	const completes: Array<[string, Record<string, unknown>]> = [];
	const removes: string[] = [];
	let confirmed: number[] = [];
	return {
		saves, completes, removes,
		setConfirmed: (c: number[]) => { confirmed = c; },
		adapter: {
			save(rec: StorageRec) { saves.push(rec); },
			complete(id: string, outcome: Record<string, unknown>) {
				completes.push([id, outcome]);
			},
			remove(id: string) { removes.push(id); },
			findResumable: () => null,
			readConfirmed: () => confirmed,
		},
	};
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
		if (url.startsWith("https://")) return Promise.resolve(resp({}));
		return Promise.resolve(resp({}));
	}) as unknown as typeof fetch;
}

afterEach(() => {
	vi.useRealTimers();
	vi.unstubAllGlobals();
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
		const calls = (fetchImpl as unknown as vi.Mock).mock.calls as unknown as [string, RequestInit?][];
		const puts = calls.filter(([u]) => u.startsWith("https://"));
		expect(puts).toHaveLength(4);
		puts.forEach(([, init]) => {
			expect(init && init.credentials).toBe("omit");
			expect(init && init.mode).toBe("cors");
			expect(!(init && init.headers)).toBe(true);
		});
		// 每片恰好 slice 一次，且长度受计划约束
		expect(src.slices).toHaveLength(4);
		expect(src.slices.map((s) => s.end - s.start).sort()).toEqual([8, 8, 8, 8]);
		// 持久化先于第一个 PUT（断网/刷新后可续传的前提）
		const firstPutIdx = calls.findIndex(([u]) => u.startsWith("https://"));
		// storage.save 是同步调用（引擎内联），首个 PUT 前必有 saves 记录
		expect(st.saves.length).toBeGreaterThanOrEqual(1);
		expect(st.saves[0].job_id).toBe("inj_x");
		void firstPutIdx;
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
		const src = fakeSource(32);
		const st = fakeStorage();
		st.setConfirmed([1, 2]);
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
		expect(src.slices).toHaveLength(2);
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

	it("cancel：本地记录清理 + POST /cancel；done resolve {cancelled:true}", async () => {
		const parts = [{ part_number: 1, length: 8 }, { part_number: 2, length: 8 }];
		let cancelled = 0;
		const fetchImpl = vi.fn((url: string, init?: RequestInit) => {
			const method = String((init && init.method) || "GET");
			if (url === "/api/ingestions" && method === "POST") {
				return Promise.resolve(resp({ job_id: "inj_n", stage: "uploading" }, 202));
			}
			if (url === "/api/ingestions/inj_n" && method === "GET") {
				return new Promise((resolve) => setTimeout(() => resolve(resp({ stage: "uploading", parts }) as unknown as Response), 50));
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
			if (url.startsWith("https://")) {
				return new Promise((resolve) => setTimeout(() => resolve(resp({}) as unknown as Response), 80));
			}
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
		await flush(4);
		up.cancel();
		const r = await up.done;
		expect(r && r.cancelled).toBe(true);
		expect(cancelled).toBe(1);
		expect(st.removes).toContain("inj_n");
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
		expect(calls().filter((u) => u.startsWith("https://"))).toHaveLength(2);
	});

	it("取消之后才完成的 PUT 不再写回续传记录", async () => {
		const parts = [{ part_number: 1, length: 8 }];
		let releasePut: () => void = () => {};
		const base = happyFetch(parts);
		const fetchImpl = vi.fn((url: string, init?: RequestInit) => {
			if (url.startsWith("https://")) {
				// 模拟不响应 abort 的在途 PUT：取消后仍然成功返回
				return new Promise<Response>((r) => { releasePut = () => r(resp({})); });
			}
			return (base as unknown as (u: string, i?: RequestInit) => Promise<Response>)(url, init);
		}) as unknown as typeof fetch;
		const eng = loadEngine(fetchImpl);
		const st = fakeStorage();
		const up = eng.createUpload({
			source: fakeSource(8).view,
			apiFetch: (u, o) => (fetchImpl as unknown as (u2: string, o2?: RequestInit) => Promise<Response>)(u, o),
			config: eng.resolveConfig(CFG), storage: st.adapter,
		});
		for (let i = 0; i < 50 && !(fetchImpl as unknown as vi.Mock).mock.calls
			.some((c: unknown[]) => String(c[0]).startsWith("https://")); i++) await tick();
		const savesBefore = st.saves.length;
		up.cancel();
		expect(st.removes).toEqual(["inj_x"]);
		releasePut();
		const r = await up.done;
		expect(r && r.cancelled).toBe(true);
		await flush();
		expect(st.saves.length).toBe(savesBefore);
	});
});
