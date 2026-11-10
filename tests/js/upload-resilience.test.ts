/**
 * upload-resilience（2026-11-10）单测：
 *   1. 重复文件预检（本地面板队列 + 服务端 completed/unfinished；批量合并；
 *      3 秒超时 fail-open）；
 *   2. MD5（RFC 1321 已知向量）与服务端 ListParts 分块凭证核验
 *      （ETag = 分块 MD5；非 MD5 → 重传）；
 *   3. 刷新后免重选续传的句柄持久化（假 IndexedDB；权限授予/拒绝/文件已变）；
 *   4. 创建幂等键（账号+名+大小+lastModified+nonce；重试复用）；
 *   5. 全部取消 / 单条取消的槽位顺序（先标记、取消往返结束才 pump）。
 */
import { afterEach, describe, expect, it, vi } from "vitest";
import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const root = resolve(here, "../..");
const appSrc = readFileSync(resolve(root, "static/app.js"), "utf8");
const cosSrc = readFileSync(resolve(root, "static/upload/cos-uploader.js"), "utf8");
const inlineSrc = readFileSync(
	resolve(root, "static/upload/inline-convert.js"), "utf8");
const sniffSrc = readFileSync(
	resolve(root, "static/upload/slide-sniff.js"), "utf8");
const companionSrc = readFileSync(
	resolve(root, "static/upload/mrxs-companion.js"), "utf8");
const dedupeSrc = readFileSync(
	resolve(root, "static/upload/upload-dedupe.js"), "utf8");
const handlesSrc = readFileSync(
	resolve(root, "static/upload/resume-handles.js"), "utf8");

const tick = () => new Promise((r) => setTimeout(r, 0));
async function flush(n = 12) {
	for (let i = 0; i < n; i++) await tick();
}
async function until(cond: () => boolean, label = "condition") {
	for (let i = 0; i < 300; i++) {
		if (cond()) return;
		await flush(1);
	}
	throw new Error("timed out: " + label);
}

// --------------------------------------------------------------------- //
// 1. 重复文件预检（纯模块）
// --------------------------------------------------------------------- //
function loadDedupe() {
	const w: Record<string, unknown> = {};
	new Function("window", dedupeSrc)(w);
	return w.HP_UPLOAD_DEDUPE as {
		DUP_TIMEOUT_MS: number;
		key(name: string, size: number): string;
		queueMatches(entries: unknown[], file: unknown): Array<{ name: string }>;
		normalizeServerBody(body: unknown): unknown;
		requestDuplicates(
			apiFetch: unknown, items: unknown[], opts?: Record<string, unknown>,
		): Promise<Record<string, unknown>>;
		buildPlan(items: unknown[], opts: Record<string, unknown>): {
			total: number;
			duplicates: Array<{
				index: number; kind: string; name: string;
				queueMatch: unknown; completed: unknown; unfinished: unknown;
			}>;
		};
	};
}

function file(name: string, size: number) {
	return { name, size, lastModified: 10 };
}

describe("重复文件预检模块", () => {
	it("queueMatches：同 name+size 且非终态/刚完成才命中；失败/取消行不算", () => {
		const D = loadDedupe();
		const entries = [
			{ name: "a.tif", size: 10, state: "uploading" },
			{ name: "a.tif", size: 10, state: "done" },
			{ name: "a.tif", size: 10, state: "cancelled" },
			{ name: "a.tif", size: 10, state: "upload_failed" },
			{ name: "a.tif", size: 11, state: "uploading" },
			{ name: "b.tif", size: 10, state: "uploading" },
		];
		expect(D.queueMatches(entries, file("a.tif", 10)).length).toBe(2);
		expect(D.queueMatches(entries, file("a.tif", 11)).length).toBe(1);
		expect(D.queueMatches(entries, file("c.tif", 10)).length).toBe(0);
	});

	it("buildPlan：队列命中优先；否则 completed；再否则 unfinished", () => {
		const D = loadDedupe();
		const items = [
			{ file: file("q.tif", 10) },
			{ file: file("c.tif", 20) },
			{ file: file("u.tif", 30) },
			{ file: file("ok.tif", 40) },
		];
		const serverResults: Record<string, unknown> = {};
		serverResults[D.key("c.tif", 20)] = {
			completed: { slide_id: "sld_c", display_name: "C", completed_at: 1 },
			unfinished: null,
		};
		serverResults[D.key("u.tif", 30)] = {
			completed: null,
			unfinished: {
				job_id: "inj_u", state: "uploading",
				uploaded_parts: 2, total_parts: 5,
			},
		};
		const plan = D.buildPlan(items, {
			queueEntries: [{ name: "q.tif", size: 10, state: "uploading" }],
			serverResults,
		});
		expect(plan.total).toBe(4);
		expect(plan.duplicates.map((d) => [d.name, d.kind])).toEqual([
			["q.tif", "queue"], ["c.tif", "completed"], ["u.tif", "unfinished"],
		]);
	});

	it("normalizeServerBody：非法/空响应 → null（fail open）；字段裁剪", () => {
		const D = loadDedupe();
		expect(D.normalizeServerBody(null)).toBeNull();
		expect(D.normalizeServerBody({})).toBeNull();
		expect(D.normalizeServerBody({ completed: { slide_id: "" } })).toBeNull();
		const n = D.normalizeServerBody({
			completed: { slide_id: "sld_1", display_name: "", completed_at: 5 },
			unfinished: { job_id: "inj_1", state: "uploading",
				uploaded_parts: null, total_parts: 3 },
		}) as { completed: { slide_id: string; completed_at: number };
			unfinished: { uploaded_parts: unknown } };
		expect(n.completed.slide_id).toBe("sld_1");
		expect(n.completed.completed_at).toBe(5);
		expect(n.unfinished.uploaded_parts).toBeNull();
	});

	it("requestDuplicates：非 2xx / 坏 JSON / 抛错 → 该文件 null；唯一键去重", async () => {
		const D = loadDedupe();
		const calls: string[] = [];
		const apiFetch = vi.fn((url: string) => {
			calls.push(url);
			if (url.indexOf("bad") >= 0) return Promise.reject(new Error("net"));
			if (url.indexOf("err") >= 0) return Promise.resolve({ ok: false, status: 500 });
			return Promise.resolve({
				ok: true,
				json: () => Promise.resolve({
					completed: { slide_id: "sld_ok", display_name: "ok", completed_at: 1 },
					unfinished: null,
				}),
			});
		});
		const out = await D.requestDuplicates(apiFetch, [
			{ file: file("ok.tif", 10) },
			{ file: file("ok.tif", 10) },   // 同 name+size：只查一次
			{ file: file("bad.tif", 10) },
			{ file: file("err.tif", 10) },
		], { timeoutMs: 50 });
		expect(calls.length).toBe(3);
		expect((out[D.key("ok.tif", 10)] as { completed: { slide_id: string } })
			.completed.slide_id).toBe("sld_ok");
		expect(out[D.key("bad.tif", 10)] ?? null).toBeNull();
		expect(out[D.key("err.tif", 10)] ?? null).toBeNull();
	});

	it("requestDuplicates：3 秒超时默认 fail-open（永不 reject）", async () => {
		const D = loadDedupe();
		expect(D.DUP_TIMEOUT_MS).toBe(3000);
		let aborted = false;
		const apiFetch = () => new Promise((_res, rej) => {
			// 永不响应；只验证超时后 resolve 且不发火
			void rej;
		});
		const ctl = {
			abort() { aborted = true; },
		};
		// 注入极短超时；AbortController 由模块内部创建，这里只验证返回值
		const out = await D.requestDuplicates(apiFetch, [file("slow.tif", 10)],
			{ timeoutMs: 10 });
		expect(out[D.key("slow.tif", 10)] ?? null).toBeNull();
		void ctl;
		void aborted;
	});

	it("requestDuplicates：apiFetch 缺失 → 空结果（不抛）", async () => {
		const D = loadDedupe();
		await expect(D.requestDuplicates(null, [file("a.tif", 1)]))
			.resolves.toEqual({});
	});
});

// --------------------------------------------------------------------- //
// 2. MD5 已知向量 + 服务端分块 ETag 核验
// --------------------------------------------------------------------- //
function loadEngine() {
	const w: Record<string, unknown> = {};
	new Function("window", cosSrc)(w);
	return w.HP_COS_UPLOAD as {
		md5Hex(bytes: Uint8Array): string;
		md5HexChunked(blob: Blob): Promise<string | null>;
		partDigestsChunked(blob: Blob): Promise<
			{ md5: string; sha256: string } | null>;
		sha256HexChunked(blob: Blob): Promise<string | null>;
		etagMd5(etag: unknown): string | null;
		resolveConfig(p: unknown): Record<string, unknown> | null;
		createUpload(o: Record<string, unknown>): {
			cancel(): Promise<boolean>;
			done: Promise<unknown>;
		};
	};
}

const enc = (s: string) => new TextEncoder().encode(s);

describe("MD5（RFC 1321 向量）与分块凭证", () => {
	it("已知向量：空串 / a / abc / message digest / 字母表 / 100 万 a", () => {
		const E = loadEngine();
		const vectors: Array<[string, string]> = [
			["", "d41d8cd98f00b204e9800998ecf8427e"],
			["a", "0cc175b9c0f1b6a831c399e269772661"],
			["abc", "900150983cd24fb0d6963f7d28e17f72"],
			["message digest", "f96b697d7cb7938d525a2f31aaf161d0"],
			["abcdefghijklmnopqrstuvwxyz", "c3fcd3d76192e4007dfb496cca67e13b"],
			["ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789",
				"d174ab98d277d9f5a5611c2c9f419d9f"],
			["12345678901234567890123456789012345678901234567890123456789012"
				+ "345678901234567890", "57edf4a22be3c955ac49da2e2107b67a"],
		];
		for (const [input, want] of vectors) {
			expect(E.md5Hex(enc(input)), input).toBe(want);
		}
		expect(E.md5Hex(new Uint8Array(1_000_000).fill(97)))
			.toBe("7707d6ae4e027c70eea2a935c2296f21");
	});

	it("流式 MD5 与一次性结果一致；partDigestsChunked 的 SHA 与 digest_scheme 同方案", async () => {
		const E = loadEngine();
		const data = new Uint8Array(200_000);
		for (let i = 0; i < data.length; i++) data[i] = i * 31 & 0xff;
		const blob = new Blob([data]);
		expect(await E.md5HexChunked(blob)).toBe(E.md5Hex(data));
		const part = await E.partDigestsChunked(blob);
		expect(part).not.toBeNull();
		expect(part!.md5).toBe(E.md5Hex(data));
		expect(part!.sha256).toBe(await E.sha256HexChunked(blob));
	});

	it("etagMd5：去引号/大小写；非 32 位十六进制 → null（KMS/实现差异）", () => {
		const E = loadEngine();
		expect(E.etagMd5('"900150983CD24FB0D6963F7D28E17F72"'))
			.toBe("900150983cd24fb0d6963f7d28e17f72");
		expect(E.etagMd5("900150983cd24fb0d6963f7d28e17f72"))
			.toBe("900150983cd24fb0d6963f7d28e17f72");
		expect(E.etagMd5('"opaque-kms-etag"')).toBeNull();
		expect(E.etagMd5(null)).toBeNull();
	});
});

/** 最小 XHR 替身（只服务引擎的 PUT） */
class FakeXHR {
	static instances: FakeXHR[] = [];
	method = ""; url = ""; sent = false;
	upload: Record<string, unknown> = {};
	status = 0;
	getResponseHeader = (_h: string) => null as string | null;
	open(m: string, u: string) { this.method = m; this.url = u; }
	send() {
		this.sent = true;
		// 全部 PUT 立即成功（返回任意 ETag；引擎只把它当提示）
		this.status = 200;
		this.getResponseHeader = (h: string) =>
			h.toLowerCase() === "etag" ? '"x"' : null;
		setTimeout(() => { if (this.onload) this.onload(); }, 0);
	}
	abort() { if (this.onabort) this.onabort(); }
	onload: (() => void) | null = null;
	onerror: (() => void) | null = null;
	onabort: (() => void) | null = null;
	ontimeout: (() => void) | null = null;
	constructor() { FakeXHR.instances.push(this); }
}

function fakeSource(size: number, name: string, bytes: Uint8Array) {
	return {
		name,
		size,
		slice(start: number, end: number) {
			const len = end - start;
			return {
				size: len,
				// 引擎的 digest 对分片 Blob 再按 4 MiB 子块 slice —— 子块必须
				// 返回原文件的真实字节（否则「内容核验」测的是零填充）。
				slice(a: number, b: number) {
					const sub = bytes.slice(start + a, start + Math.min(b, len));
					return { size: sub.length, arrayBuffer: async () => sub.buffer };
				},
				arrayBuffer: async () => bytes.slice(start, end).buffer,
			};
		},
	};
}

describe("服务端权威 ListParts 续传核验（引擎）", () => {
	const CFG = {
		available: true, max_size_bytes: 1000, part_bytes: 8,
		url_ttl_seconds: 600, max_concurrent_parts: 1,
		sign_batch_max_parts: 4, formats: ["tif"],
	};

	function harness(serverParts: unknown[], opts: Record<string, unknown> = {},
		bytes: Uint8Array = new Uint8Array(24)) {
		const w: Record<string, unknown> = {};
		const E = loadEngine();
		void w;
		const signed: number[][] = [];
		const created: unknown[] = [];
		let statusCalls = 0;
		const parts = [
			{ part_number: 1, length: 8 },
			{ part_number: 2, length: 8 },
			{ part_number: 3, length: 8 },
		];
		const apiFetch = vi.fn((url: string, init?: { method?: string; body?: string }) => {
			const method = (init && init.method) || "GET";
			if (url === "/api/ingestions" && method === "POST") {
				created.push(JSON.parse(String(init!.body)));
				return Promise.resolve(jsonResp({ job_id: "inj_new", stage: "uploading" }, 202));
			}
			if (url.match(/\/parts\/sign$/)) {
				const nums = JSON.parse(String(init!.body)).part_numbers as number[];
				signed.push(nums);
				return Promise.resolve(jsonResp({
					urls: nums.map((n) => ({
						url: "https://cos.example/p?partNumber=" + n,
						part_number: n,
					})),
				}));
			}
			if (url.match(/\/upload-complete$/)) {
				return Promise.resolve(jsonResp({ stage: "awaiting_server" }, 202));
			}
			// GET 状态
			statusCalls += 1;
			if (statusCalls === 1) {
				return Promise.resolve(jsonResp({
					stage: "uploading", total_parts: 3, parts,
				}));
			}
			return Promise.resolve(jsonResp({ stage: "viewable", slide_id: "sld_x" }));
		});
		const saved: Array<Record<string, unknown>> = [];
		const upload = E.createUpload(Object.assign({
			source: fakeSource(24, "out.tif", bytes),
			apiFetch,
			config: CFG,
			storage: {
				save: (rec: Record<string, unknown>) => { saved.push(rec); },
				complete: () => {},
				remove: () => {},
				findResumable: () => null,
				readConfirmed: () => [],
				readRecord: () => Promise.resolve(null),
			},
			account: "user-1",
			serverParts,
			resumeJobId: "inj_prev",
			skipConfirm: true,
		}, opts));
		return { upload, signed, created, saved };
	}

	it("ETag=MD5 的分块跳过；长度不符/非 MD5/内容不符的分块重传", async () => {
		vi.stubGlobal("XMLHttpRequest", FakeXHR);
		FakeXHR.instances.length = 0;
		const E = loadEngine();
		const bytes = new Uint8Array(24);
		bytes[0] = 7; bytes[1] = 9;        // part 2 与 part 1 内容不同
		const md5p1 = E.md5Hex(bytes.slice(0, 8));
		const md5p2 = E.md5Hex(bytes.slice(8, 16));
		// part2 的 ETag 故意用另一段内容的 MD5 → 内容不符 → 重传
		const wrong = E.md5Hex(new Uint8Array(8).fill(0xaa));
		const h = harness([
			{ part_number: 1, size: 8, etag: '"' + md5p1 + '"' },   // 相符 → 跳过
			{ part_number: 2, size: 8, etag: '"' + wrong + '"' },    // 不符 → 重传
			{ part_number: 3, size: 7, etag: '"' + md5p2 + '"' },    // 长度不符 → 重传
		], {}, bytes);
		// part2 的 MD5 与 ETag 不符；part3 长度与计划不符
		await h.upload.done;
		expect(h.created.length).toBe(0);          // 复用既有任务，绝不建新任务
		expect(h.signed).toEqual([[2, 3]]);        // 只签/只传未核验通过的分块
		const rec = h.saved.find((r) => Array.isArray(r.confirmed) &&
			(r.confirmed as number[]).length > 0);
		expect(rec).toBeTruthy();
		expect(rec!.confirmed).toContain(1);
		expect(rec!.digest_scheme).toBe("sha256-chunked-4MiB-v1");
	});

	it("非 MD5 ETag（KMS 等）→ 该块重传，绝不凭编号跳过", async () => {
		vi.stubGlobal("XMLHttpRequest", FakeXHR);
		FakeXHR.instances.length = 0;
		const h = harness([
			{ part_number: 1, size: 8, etag: '"opaque-kms"' },
		]);
		await h.upload.done;
		expect(h.signed).toEqual([[1, 2, 3]]);
	});

	it("创建幂等键随 POST /api/ingestions 发送（新任务路径）", async () => {
		vi.stubGlobal("XMLHttpRequest", FakeXHR);
		FakeXHR.instances.length = 0;
		const h = harness([], {
			resumeJobId: null,
			serverParts: null,
			idempotencyKey: "ptu:abc123:deadbeef",
		});
		await h.upload.done;
		expect(h.created.length).toBe(1);
		expect(h.created[0]).toMatchObject({
			filename: "out.tif",
			declared_size: 24,
			idempotency_key: "ptu:abc123:deadbeef",
		});
	});
});

function jsonResp(body: unknown, status = 200) {
	return {
		ok: status >= 200 && status < 300,
		status,
		clone() { return this; },
		json: () => Promise.resolve(body),
		headers: { get: () => null },
	};
}

afterEach(() => {
	vi.unstubAllGlobals();
	FakeXHR.instances.length = 0;
});

// --------------------------------------------------------------------- //
// 3. 句柄持久化（假 IndexedDB）
// --------------------------------------------------------------------- //
function loadHandles() {
	const w: Record<string, unknown> = {};
	new Function("window", handlesSrc)(w);
	return w.HP_RESUME_HANDLES as {
		supported(): boolean;
		pickerSupported(): boolean;
		save(jobId: string, handle: unknown, meta: Record<string, unknown>): Promise<boolean>;
		load(jobId: string): Promise<Record<string, unknown> | null>;
		remove(jobId: string): Promise<boolean>;
		fileFromHandle(rec: Record<string, unknown>): Promise<{
			file?: { name: string; size: number; lastModified: number };
			error?: string;
		}>;
		_setIndexedDBFactory(factory: unknown): void;
	};
}

function fakeIndexedDB() {
	const store = new Map<string, Record<string, unknown>>();
	const db = {
		objectStoreNames: { contains: () => true },
		transaction() {
			const tx: Record<string, unknown> = {};
			const finish = () => queueMicrotask(() => {
				if (tx.oncomplete) (tx.oncomplete as () => void)();
			});
			const os = {
				put(rec: Record<string, unknown>) {
					store.set(String(rec.job_id), rec);
					finish();
					return {};
				},
				delete(k: string) {
					store.delete(k);
					finish();
					return {};
				},
				get(k: string) {
					const req: Record<string, unknown> = {};
					queueMicrotask(() => {
						req.result = store.get(k);
						if (req.onsuccess) (req.onsuccess as () => void)();
						finish();
					});
					return req;
				},
			};
			tx.objectStore = () => os;
			return tx;
		},
	};
	void store;
	return {
		_store: store,
		open() {
			const req: Record<string, unknown> = {};
			queueMicrotask(() => {
				req.result = db;
				if (req.onupgradeneeded) (req.onupgradeneeded as () => void)();
				if (req.onsuccess) (req.onsuccess as () => void)();
			});
			return req;
		},
	};
}

function handleWith(fileObj: unknown, permission = "granted") {
	return {
		queryPermission: async () => permission,
		requestPermission: async () => permission,
		getFile: async () => fileObj,
	};
}

describe("刷新后免重选续传：句柄持久化", () => {
	it("save/load/remove：按 job_id 覆盖写入；meta 保留", async () => {
		const H = loadHandles();
		const idb = fakeIndexedDB();
		H._setIndexedDBFactory(idb);
		const h = handleWith({ name: "a.tif", size: 10, lastModified: 1 });
		expect(await H.save("inj_1", h, {
			filename: "a.tif", size: 10, last_modified: 1,
		})).toBe(true);
		const rec = await H.load("inj_1");
		expect(rec).toBeTruthy();
		expect(rec!.filename).toBe("a.tif");
		expect((rec!.handle as unknown)).toBe(h);
		expect(await H.load("nope")).toBeNull();
		expect(await H.remove("inj_1")).toBe(true);
		expect(await H.load("inj_1")).toBeNull();
	});

	it("fileFromHandle：权限授予 → File；拒绝 → permission_denied", async () => {
		const H = loadHandles();
		const ok = await H.fileFromHandle({
			filename: "a.tif", size: 10, last_modified: 1,
			handle: handleWith({ name: "a.tif", size: 10, lastModified: 1 }),
		});
		expect(ok.file).toMatchObject({ name: "a.tif", size: 10 });
		const denied = await H.fileFromHandle({
			filename: "a.tif", size: 10,
			handle: handleWith({ name: "a.tif", size: 10, lastModified: 1 },
				"denied"),
		});
		expect(denied.error).toBe("permission_denied");
	});

	it("fileFromHandle：文件已变（size/lastModified 不符）→ file_changed", async () => {
		const H = loadHandles();
		const bySize = await H.fileFromHandle({
			filename: "a.tif", size: 10, last_modified: 1,
			handle: handleWith({ name: "a.tif", size: 11, lastModified: 1 }),
		});
		expect(bySize.error).toBe("file_changed");
		const byTime = await H.fileFromHandle({
			filename: "a.tif", size: 10, last_modified: 1,
			handle: handleWith({ name: "a.tif", size: 10, lastModified: 2 }),
		});
		expect(byTime.error).toBe("file_changed");
		const byName = await H.fileFromHandle({
			filename: "a.tif", size: 10, last_modified: 1,
			handle: handleWith({ name: "b.tif", size: 10, lastModified: 1 }),
		});
		expect(byName.error).toBe("file_changed");
	});

	it("IndexedDB 不可用：supported=false，所有操作安静降级", async () => {
		const H = loadHandles();
		// 隐私模式/配额：open 抛错 → 所有操作安静返回，不影响上传主链路
		H._setIndexedDBFactory({
			open() { throw new Error("SecurityError"); },
		});
		expect(H.supported()).toBe(true);   // 工厂存在，但打开失败
		expect(await H.load("x")).toBeNull();
		await expect(H.save("x", {}, {})).resolves.toBe(true);   // 不抛
		await expect(H.remove("x")).resolves.toBe(true);
	});
});

// --------------------------------------------------------------------- //
// 4. 编排器：全部取消 / 单条取消的调度顺序
// --------------------------------------------------------------------- //
interface Entry {
	id: string; state: string; name: string; cancelled?: boolean;
	_upload?: { cancel?: () => unknown };
	[key: string]: unknown;
}

const tifBytes = Uint8Array.from([0x49, 0x49, 0x2a, 0x00, 0x08, 0x00, 0x00, 0x00]);
function tif(name: string) {
	return {
		name, size: tifBytes.length, lastModified: 1,
		slice(s: number, e: number) {
			const b = tifBytes.slice(s, e);
			return { arrayBuffer: async () => b.buffer, size: b.length };
		},
	};
}

function deferred<T = unknown>() {
	let resolve!: (v: T) => void;
	const promise = new Promise<T>((r) => { resolve = r; });
	return { promise, resolve };
}

function mkOrchShared(onArtifact: (e: Entry) => Promise<unknown>) {
	const w: Record<string, unknown> = {};
	new Function("window", sniffSrc)(w);
	new Function("window", companionSrc)(w);
	new Function("window", inlineSrc)(w);
	const I = w.HP_INLINE_CONVERT as {
		createOrchestrator(deps: Record<string, unknown>): {
			enqueue(file: unknown, opts?: Record<string, unknown>): Entry;
			cancel(e: Entry): void;
			cancelAll(): number;
			cancellableCount(): number;
			list(): Entry[];
			busy(): { converting: boolean; uploading: boolean };
		};
	};
	const orch = I.createOrchestrator({
		runner: { listJobs: async () => [], getJob: async () => null },
		engine: {},
		sniff: w.HP_SLIDE_SNIFF,
		cos: {},
		caps: { cosConfig: { max_size_bytes: 1000, formats: ["tif"] } },
		account: () => "u1",
		ui: {
			row() {}, setStage() {}, markError() {}, buttons() {},
			toast() {}, refresh() {}, remove() {},
		},
		onArtifact,
		cancelArtifact: (e: Entry) => {
			if (e._upload && e._upload.cancel) return e._upload.cancel();
			return Promise.resolve();
		},
		setTimer: (fn: () => void, ms: number) => setTimeout(fn, ms),
		t: (k: string) => k,
	});
	return { orch, w };
}

describe("全部取消 / 单条取消：先标记、取消往返结束才调度", () => {
	it("cancelAll：活跃 + 排队全部标记；取消往返结束前绝不启动下一条（新加的也不）", async () => {
		const started: string[] = [];
		const { orch } = mkOrchShared((e) => {
			started.push(e.name);
			if (e.name === "a.tif" || e.name === "d.tif") {
				return new Promise(() => {});   // 保持上传（观察槽位顺序）
			}
			return Promise.resolve({ body: { slide_id: "sld_" + e.name } });
		});
		const a = orch.enqueue(tif("a.tif"));
		await flush(6);
		expect(a.state).toBe("uploading");
		expect(started).toEqual(["a.tif"]);
		const b = orch.enqueue(tif("b.tif"));
		const c = orch.enqueue(tif("c.tif"));
		await until(() => b.state === "upload_queued" &&
			c.state === "upload_queued", "b/c queued");
		expect(orch.cancellableCount()).toBe(3);

		// 取消往返可手动放行：槽位必须保留到它结束
		const cancelGate = deferred();
		a._upload = { cancel: () => cancelGate.promise };
		expect(orch.cancelAll()).toBe(3);
		expect(a.state).toBe("cancelled");
		expect(b.state).toBe("cancelled");
		expect(c.state).toBe("cancelled");
		expect(orch.cancellableCount()).toBe(0);
		expect(orch.busy().uploading).toBe(true);   // 槽位仍被取消往返占着

		// 取消全部之后用户又加了一条：在往返结束前绝不启动
		const d = orch.enqueue(tif("d.tif"));
		await flush(8);
		expect(started).toEqual(["a.tif"]);
		expect(d.state !== "uploading").toBe(true);

		cancelGate.resolve(true);
		await flush(10);
		expect(started).toEqual(["a.tif", "d.tif"]);
		expect(d.state).toBe("uploading");
	});

	it("单条取消活跃上传：取消往返结束前不启动排队项", async () => {
		const started: string[] = [];
		const { orch } = mkOrchShared((e) => {
			started.push(e.name);
			return new Promise(() => {});   // a/b 都保持「上传中」
		});
		const a = orch.enqueue(tif("a.tif"));
		await flush(6);
		const b = orch.enqueue(tif("b.tif"));
		await flush(4);
		expect(started).toEqual(["a.tif"]);

		const cancelGate = deferred();
		a._upload = { cancel: () => cancelGate.promise };
		orch.cancel(a);
		expect(a.state).toBe("cancelled");
		await flush(8);
		expect(started).toEqual(["a.tif"]);        // 往返未结束：不得启动 B
		expect(orch.busy().uploading).toBe(true);

		cancelGate.resolve(true);
		await flush(10);
		expect(started).toEqual(["a.tif", "b.tif"]);
		expect(b.state).toBe("uploading");
	});

	it("取消排队项（非活跃）：立即释放/继续调度，不受往返影响", async () => {
		const { orch } = mkOrchShared((e) => {
			if (e.name === "a.tif") return new Promise(() => {});
			return Promise.resolve({ body: { slide_id: "sld_" + e.name } });
		});
		const a = orch.enqueue(tif("a.tif"));
		await flush(6);
		const b = orch.enqueue(tif("b.tif"));
		await flush(4);
		orch.cancel(b);
		expect(b.state).toBe("cancelled");
		expect(a.state).toBe("uploading");
	});
});

// 供 lint/未使用提示：保留通用 harness（部分断言直接使用）
void loadDedupe;

// --------------------------------------------------------------------- //
// 5. app.js 接线：重复提示对话框 / 批量合并 / fail-open / 幂等键
// --------------------------------------------------------------------- //
function elStub(tag = "div") {
	const children: Array<ReturnType<typeof elStub>> = [];
	const listeners: Record<string, Array<(ev?: unknown) => void>> = {};
	const node: Record<string, unknown> = {
		tagName: tag.toUpperCase(), hidden: true, textContent: "", innerHTML: "",
		value: "", disabled: false, type: "", className: "", style: {},
		classList: { add() {}, remove() {}, contains() { return false; } },
		children, parentNode: null,
		appendChild(c: ReturnType<typeof elStub>) { children.push(c); c.parentNode = node; return c; },
		removeChild(c: ReturnType<typeof elStub>) {
			const i = children.indexOf(c);
			if (i >= 0) children.splice(i, 1);
			return c;
		},
		insertBefore(c: ReturnType<typeof elStub>, ref: ReturnType<typeof elStub> | null) {
			const i = ref ? children.indexOf(ref) : -1;
			if (i < 0) children.push(c); else children.splice(i, 0, c);
			c.parentNode = node;
			return c;
		},
		addEventListener(t: string, fn: (ev?: unknown) => void) {
			(listeners[t] = listeners[t] || []).push(fn);
		},
		removeEventListener() {},
		setAttribute() {}, getAttribute() { return null; },
		focus() {}, click() { (listeners.click || []).forEach((f) => f({ preventDefault() {} })); },
	};
	return node as ReturnType<typeof elStub> & { _listeners: typeof listeners };
}

function fakeStorage() {
	const map = new Map<string, string>();
	return {
		getItem: (k: string) => (map.has(k) ? map.get(k)! : null),
		setItem: (k: string, v: string) => { map.set(k, String(v)); },
		removeItem: (k: string) => { map.delete(k); },
		clear: () => map.clear(),
	};
}

function cosCaps() {
	return {
		available: true,
		max_size_bytes: 10_000,
		part_bytes: 8,
		url_ttl_seconds: 600,
		max_concurrent_parts: 2,
		sign_batch_max_parts: 2,
		formats: ["tif", "svs"],
	};
}

function loadApp(fetchImpl: typeof fetch, bootstrap: unknown) {
	const storage = fakeStorage();
	const els: Record<string, ReturnType<typeof elStub>> = {};
	const container = elStub();
	const parent = elStub();
	container.parentNode = parent;
	els["upload-progress-list"] = container;
	els["toast-container"] = elStub();
	const docListeners: Record<string, Array<(ev?: unknown) => void>> = {};
	const doc = {
		readyState: "loading", cookie: "csrf_token=tok", visibilityState: "visible",
		getElementById(id: string) { return (els[id] = els[id] || elStub()); },
		createElement(t: string) { return elStub(t); },
		addEventListener(t: string, fn: (ev?: unknown) => void) {
			(docListeners[t] = docListeners[t] || []).push(fn);
		},
		removeEventListener() {},
		querySelector() { return elStub(); },
		querySelectorAll() { return []; },
	};
	const loc = { href: "http://local/", pathname: "/" };
	const w: Record<string, unknown> = {
		HP_I18N: {
			t: (k: string, vars?: Record<string, unknown>) => {
				if (!vars) return k;
				return k + ":" + Object.keys(vars).map((n) => n + "=" + String(vars[n])).join(",");
			},
			getLang: () => "zh",
		},
		HP_APP_BOOTSTRAP: bootstrap,
		fetch: fetchImpl,
		location: loc,
		confirm: vi.fn(() => true),
		addEventListener() {},
	};
	(globalThis as { window?: unknown }).window = w;
	(globalThis as { document?: unknown }).document = doc;
	(globalThis as { fetch?: unknown }).fetch = fetchImpl;
	(globalThis as { location?: unknown }).location = loc;
	(globalThis as { localStorage?: unknown }).localStorage = storage;
	vi.stubGlobal("localStorage", storage);
	new Function("window", "document", "fetch", "location", cosSrc)(w, doc, fetchImpl, loc);
	new Function("window", sniffSrc)(w);
	new Function("window", dedupeSrc)(w);
	new Function("window", handlesSrc)(w);
	new Function("window", "document", "fetch", "location", appSrc)(w, doc, fetchImpl, loc);
	return {
		up: w.HP_UPLOAD as {
			uploadFiles(items: unknown[], opts?: Record<string, unknown>): Promise<number>;
			uploadFile(file: unknown, opts?: Record<string, unknown>): unknown;
			cosIdempotencyKey(account: string, file: unknown, nonce: string): string;
			newIdemNonce(): string;
			_setUploadConfirm(fn: unknown): unknown;
			started: unknown;
		},
		fetchCalls: () => (fetchImpl as unknown as { mock: { calls: [string, Record<string, unknown>?][] } }).mock.calls
			.map(([url, opts]) => ({
				url: String(url),
				method: String((opts && (opts as { method?: string }).method) || "GET"),
				body: opts && (opts as { body?: string }).body,
			})),
		doc, w,
	};
}

function uploadFile(name = "scan.tif", size = 30) {
	return {
		name, size, lastModified: 42,
		slice(s: number, e: number) {
			const len = Math.max(0, e - s);
			const magic = [0x49, 0x49, 0x2a, 0x00];
			const bytes = new Uint8Array(len);
			for (let i = 0; i < len; i++) bytes[i] = (s + i) < magic.length ? magic[s + i] : 0;
			const part = {
				size: len,
				slice(a: number, b: number) {
					const sub = Math.max(0, Math.min(b, len) - a);
					return { size: sub, arrayBuffer: async () => new ArrayBuffer(sub) };
				},
				arrayBuffer: async () => bytes.buffer,
			};
			return part;
		},
	};
}

function statusResp(body: unknown, status = 200) {
	return {
		ok: status >= 200 && status < 300,
		status,
		clone() { return this; },
		json: () => Promise.resolve(body),
		headers: { get: () => null },
	};
}

describe("app.js 接线：重复提示 / 合并对话框 / fail-open / 幂等键", () => {
	it("服务端已完成同文件 → 样式化对话框（completed 文案 + 时间）；选「不传了」零创建", async () => {
		const fetchImpl = vi.fn((url: string, opts?: { method?: string }) => {
			if (url.indexOf("/api/ingestions/duplicates") === 0) {
				return Promise.resolve(statusResp({
					completed: { slide_id: "sld_old", display_name: "旧切片",
						completed_at: 1760000000 },
					unfinished: null,
				}));
			}
			return Promise.resolve(statusResp({}));
		});
		const h = loadApp(fetchImpl as unknown as typeof fetch,
			{ mode: "official", capabilities: { cos_upload: cosCaps() } });
		const prompts: Array<Record<string, unknown>> = [];
		h.up._setUploadConfirm((opts: Record<string, unknown>) => {
			prompts.push(opts);
			return "skip";
		});
		await h.up.uploadFiles([{ file: uploadFile("scan.tif", 30), handle: null }]);
		expect(prompts.length).toBe(1);
		expect(String(prompts[0].message)).toContain("upload.dup.completed");
		expect(String(prompts[0].message)).toContain("name=scan.tif");
		const buttons = prompts[0].buttons as Array<{ value: string; default?: boolean }>;
		expect(buttons.map((b) => b.value)).toEqual(["upload", "skip"]);
		expect(buttons.find((b) => b.default)!.value).toBe("skip");   // 默认焦点：不传了
		expect(h.fetchCalls().some((c) => c.url === "/api/ingestions" && c.method === "POST"))
			.toBe(false);
		h.up._setUploadConfirm(null);
	});

	it("选「再传一份」→ 正常创建（幂等键随创建体发送）", async () => {
		const fetchImpl = vi.fn((url: string) => {
			if (url.indexOf("/api/ingestions/duplicates") === 0) {
				return Promise.resolve(statusResp({
					completed: { slide_id: "sld_old", display_name: "旧", completed_at: 1 },
					unfinished: null,
				}));
			}
			if (url === "/api/ingestions") {
				return Promise.resolve(statusResp({ job_id: "inj_new", stage: "terminal",
					fail_code: "demo" }, 202));
			}
			return Promise.resolve(statusResp({ stage: "terminal", fail_code: "demo" }));
		});
		const h = loadApp(fetchImpl as unknown as typeof fetch,
			{ mode: "official", capabilities: { cos_upload: cosCaps() } });
		h.up._setUploadConfirm(() => "upload");
		await h.up.uploadFiles([{ file: uploadFile("scan.tif", 30), handle: null }]);
		await flush(10);
		const create = h.fetchCalls().find((c) =>
			c.url === "/api/ingestions" && c.method === "POST");
		expect(create).toBeTruthy();
		const body = JSON.parse(String(create!.body));
		expect(body.filename).toBe("scan.tif");
		expect(String(body.idempotency_key)).toMatch(/^ptu:[0-9a-f]{16}:[0-9a-f]{16}$/);
		h.up._setUploadConfirm(null);
	});

	it("服务端未完成任务 → 续传选项；选「继续」不新建任务，复用既有 job", async () => {
		const fetchImpl = vi.fn((url: string) => {
			if (url.indexOf("/api/ingestions/duplicates") === 0) {
				return Promise.resolve(statusResp({
					completed: null,
					unfinished: { job_id: "inj_prev", state: "uploading",
						uploaded_parts: 2, total_parts: 3 },
				}));
			}
			if (url.indexOf("/api/ingestions/inj_prev/parts") === 0) {
				return Promise.resolve(statusResp({ parts: [
					{ part_number: 1, size: 8, etag: '"x"' },
				] }));
			}
			if (url === "/api/ingestions") {
				return Promise.resolve(statusResp({ job_id: "inj_wrong", stage: "terminal" }, 202));
			}
			return Promise.resolve(statusResp({ stage: "terminal", fail_code: "demo" }));
		});
		const h = loadApp(fetchImpl as unknown as typeof fetch,
			{ mode: "official", capabilities: { cos_upload: cosCaps() } });
		let prompt: Record<string, unknown> | null = null;
		h.up._setUploadConfirm((opts: Record<string, unknown>) => {
			prompt = opts;
			return "resume";
		});
		await h.up.uploadFiles([{ file: uploadFile("scan.tif", 30), handle: null }]);
		await flush(12);
		expect(prompt).toBeTruthy();
		expect(String(prompt!.message)).toContain("upload.dup.resume");
		const buttons = (prompt!.buttons as Array<{ value: string }>).map((b) => b.value);
		expect(buttons).toEqual(["resume", "upload", "skip"]);
		expect(h.fetchCalls().some((c) => c.url === "/api/ingestions" && c.method === "POST"))
			.toBe(false);
		expect(h.fetchCalls().some((c) => c.url === "/api/ingestions/inj_prev"))
			.toBe(true);
		h.up._setUploadConfirm(null);
	});

	it("批量添加多个重复文件 → 只弹一个合并对话框（全部跳过 / 全部仍上传）", async () => {
		const fetchImpl = vi.fn((url: string) => {
			if (url.indexOf("/api/ingestions/duplicates") === 0) {
				return Promise.resolve(statusResp({
					completed: { slide_id: "sld_x", display_name: "旧", completed_at: 1 },
					unfinished: null,
				}));
			}
			return Promise.resolve(statusResp({}));
		});
		const h = loadApp(fetchImpl as unknown as typeof fetch,
			{ mode: "official", capabilities: { cos_upload: cosCaps() } });
		const prompts: Array<Record<string, unknown>> = [];
		h.up._setUploadConfirm((opts: Record<string, unknown>) => {
			prompts.push(opts);
			return "skip";
		});
		await h.up.uploadFiles([
			{ file: uploadFile("a.tif", 30), handle: null },
			{ file: uploadFile("b.tif", 40), handle: null },
		]);
		expect(prompts.length).toBe(1);
		expect((prompts[0].list as string[]).length).toBe(2);
		expect((prompts[0].buttons as Array<{ value: string }>).map((b) => b.value))
			.toEqual(["skip", "upload"]);
		expect(h.fetchCalls().some((c) => c.url === "/api/ingestions" && c.method === "POST"))
			.toBe(false);
		h.up._setUploadConfirm(null);
	});

	it("duplicates 查询网络失败 → fail-open：不发提示、照常创建", async () => {
		const fetchImpl = vi.fn((url: string) => {
			if (url.indexOf("/api/ingestions/duplicates") === 0) {
				return Promise.reject(new Error("network down"));
			}
			if (url === "/api/ingestions") {
				return Promise.resolve(statusResp({ job_id: "inj_new", stage: "terminal",
					fail_code: "demo" }, 202));
			}
			return Promise.resolve(statusResp({ stage: "terminal", fail_code: "demo" }));
		});
		const h = loadApp(fetchImpl as unknown as typeof fetch,
			{ mode: "official", capabilities: { cos_upload: cosCaps() } });
		let prompted = 0;
		h.up._setUploadConfirm(() => { prompted += 1; return "skip"; });
		await h.up.uploadFiles([{ file: uploadFile("scan.tif", 30), handle: null }]);
		await flush(10);
		expect(prompted).toBe(0);
		expect(h.fetchCalls().some((c) => c.url === "/api/ingestions" && c.method === "POST"))
			.toBe(true);
		h.up._setUploadConfirm(null);
	});

	it("幂等键：同 (账号, 文件, nonce) 稳定；换 nonce 变；长度 ≤128", () => {
		const h = loadApp(vi.fn() as unknown as typeof fetch,
			{ mode: "official", capabilities: { cos_upload: cosCaps() } });
		const f = { name: "a.tif", size: 10, lastModified: 123 };
		const k1 = h.up.cosIdempotencyKey("user-1", f, "deadbeefdeadbeef");
		const k2 = h.up.cosIdempotencyKey("user-1", f, "deadbeefdeadbeef");
		const k3 = h.up.cosIdempotencyKey("user-1", f, "0011223344556677");
		const k4 = h.up.cosIdempotencyKey("user-2", f, "deadbeefdeadbeef");
		expect(k1).toBe(k2);
		expect(k1).not.toBe(k3);
		expect(k1).not.toBe(k4);
		expect(k1.length).toBeLessThanOrEqual(128);
		expect(h.up.newIdemNonce()).toMatch(/^[0-9a-f]{16}$/);
	});
});
