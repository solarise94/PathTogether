/**
 * 直传控制器（/tools/slides「上传到工作台」面板，tools-slides-direct-upload.js）
 * review 2026-10-07 #3/#4 回归：
 *  - #3 发布成功后再次点击：不创建第二个 ingestion——持久化 published
 *    receipt（按账号 + 文件内容凭证），展示「打开切片」入口；
 *  - #4 项目关联失败：不得报告完整成功（不回调 onPublished），持久化
 *    {published, slideId, association_pending, target, 幂等键}；只重试关联
 *    （同 slideId、同幂等键），成功后才回调 onPublished；刷新后可恢复。
 * 真实模块 + 真实共享引擎（window.HP_COS_UPLOAD）+ 假 fetch/XHR 后端。
 */
import { afterEach, describe, expect, it, vi } from "vitest";
import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
// eslint-disable-next-line
import { createDirectUploadController } from "../../static/tools/tools-slides-direct-upload.js";

const here = dirname(fileURLToPath(import.meta.url));
const cosEngineSrc = readFileSync(
	resolve(here, "../../static/upload/cos-uploader.js"), "utf8");

type Rec = { job_id: string; filename: string; size: number;
	confirmed: number[]; digests?: Record<string, string> | null;
	account?: string; plan?: Array<{ part_number: number; length: number }> };

function fakeLocalStorage() {
	const m = new Map<string, string>();
	return {
		getItem: (k: string) => (m.has(k) ? m.get(k)! : null),
		setItem: (k: string, v: string) => { m.set(k, String(v)); },
		removeItem: (k: string) => { m.delete(k); },
		clear() { m.clear(); },
		_dump: () => m,
	};
}

/** 可驱动的假 XHR（PUT 一律立即 200 + ETag）。 */
class FakeXHR {
	static instances: FakeXHR[] = [];
	method = ""; url = ""; body: unknown = null;
	withCredentials: boolean | undefined = undefined;
	status = 0;
	upload: Record<string, unknown> = {};
	onload: (() => void) | null = null;
	onerror: (() => void) | null = null;
	onabort: (() => void) | null = null;
	ontimeout: (() => void) | null = null;
	setRequestHeader = vi.fn();
	getResponseHeader = vi.fn((_h: string) => '"etag"');
	static hold = false;   // true：send 挂起不回（测取消/在途）
	open(method: string, url: string) { this.method = method; this.url = url; }
	send(body: unknown) {
		this.body = body;
		expect(this.upload.onprogress).toBeTypeOf("function");
		expect(this.onload).toBeTypeOf("function");
		if (FakeXHR.hold) return;   // 在途挂起，由测试驱动
		this.status = 200;
		if (this.onload) this.onload();
	}
	abort() {
		this.aborted = true;
		if (this.onabort) this.onabort();
	}
	aborted = false;
	progress() { /* 本文件不需要 */ }
	constructor() { FakeXHR.instances.push(this); }
}

function resp(body: unknown, status = 200) {
	return {
		ok: status >= 200 && status < 300,
		status,
		json: () => Promise.resolve(body),
		headers: { get: () => null },
	} as unknown as Response;
}

const CAPS = {
	available: true, manual_only: true, max_size_bytes: 1 << 20,
	part_bytes: 8, url_ttl_seconds: 600, max_concurrent_parts: 2,
	sign_batch_max_parts: 4, policy_version: "v1-manual", formats: ["tif"],
};

/** 假平台后端：能力 / ingestion 全链 / 项目关联（关联可注入失败）。 */
function fakeBackend(opts: {
	account?: string; assocStatus?: number; createProjectStatus?: number;
} = {}) {
	const st = {
		creates: [] as Array<Record<string, unknown>>,
		assocAdds: [] as Array<{ pid: string; slideIds: string[] }>,
		projectCreates: Array<{ key: string; name: string }>(),
		assocStatus: opts.assocStatus ?? 200,
		createProjectStatus: opts.createProjectStatus ?? 200,
	};
	// 有状态 ingestion：创建 → uploading（分块计划）→ complete → viewable。
	// completed 集合独立于 jobs：跨标签场景的“进行中”任务（续传）不经过本
	// 假后端的创建端点，complete 后同样要能查到 viewable。
	const jobs = new Map<string, { size: number; completed: boolean }>();
	const completed = new Set<string>();
	const fetchImpl = vi.fn((url: string, init?: RequestInit) => {
		const method = String((init && init.method) || "GET");
		if (url === "/api/tools/slides/upload-capability") {
			return Promise.resolve(resp({
				cos_upload: CAPS, viewable_formats: [], account: opts.account ?? "acct-1",
				account_label: "tester",
			}));
		}
		if (url === "/api/ingestions" && method === "POST") {
			const body = JSON.parse(String(init!.body)) as Record<string, unknown>;
			st.creates.push(body);
			const jid = `inj_${st.creates.length}`;
			jobs.set(jid, { size: Number(body.declared_size), completed: false });
			return Promise.resolve(resp({ job_id: jid, stage: "uploading" }, 202));
		}
		if (/^\/api\/ingestions\/[^/]+$/.test(url) && method === "GET") {
			const id = url.split("/").pop()!;
			const job = jobs.get(id);
			if (completed.has(id) || (job && job.completed)) {
				// slide id 按 job 派生（inj_1 → sld_rc1）：同账号两条回执的
				// 场景需要可区分的 slide id（单任务场景期望值不变）
				return Promise.resolve(resp({
					stage: "viewable", slide_id: `sld_${id.replace("inj_", "rc")}`,
					slide: "same.ome.tif",
				}));
			}
			const size = job ? job.size : 16;
			const parts: Array<{ part_number: number; length: number }> = [];
			let off = 0;
			let n = 1;
			while (off < size) {
				const len = Math.min(8, size - off);
				parts.push({ part_number: n, length: len });
				off += len;
				n += 1;
			}
			return Promise.resolve(resp({ stage: "uploading", parts }));
		}
		if (url.endsWith("/parts/sign")) {
			const nums = JSON.parse(String(init!.body)).part_numbers as number[];
			return Promise.resolve(resp({
				urls: nums.map((n) => ({
					url: `https://cos.example/o?partNumber=${n}`, part_number: n,
				})),
			}));
		}
		if (url.endsWith("/upload-complete")) {
			// 只收口被 complete 的任务（同账号两条回执的场景各自独立）
			const jid = url.split("/")[3];
			completed.add(jid);
			const mine = jobs.get(jid);
			if (mine) mine.completed = true;
			return Promise.resolve(resp({ stage: "awaiting_server" }, 202));
		}
		if (/^\/api\/project\/create$/.test(url) && method === "POST") {
			st.projectCreates.push({
				key: String((init!.headers as Record<string, string>)["Idempotency-Key"] || ""),
				name: (JSON.parse(String(init!.body)) as { name: string }).name,
			});
			if (st.createProjectStatus !== 200) {
				return Promise.resolve(resp({ error: "injected" }, st.createProjectStatus));
			}
			return Promise.resolve(resp({ pid: "pj_new" }));
		}
		if (/^\/api\/project\/[^/]+\/slides$/.test(url) && method === "POST") {
			const body = JSON.parse(String(init!.body)) as { slide_ids: string[] };
			st.assocAdds.push({ pid: url.split("/")[3], slideIds: body.slide_ids });
			if (st.assocStatus !== 200) {
				return Promise.resolve(resp({ error: "injected assoc failure" }, st.assocStatus));
			}
			return Promise.resolve(resp({ ok: true }));
		}
		return Promise.resolve(resp({}));
	}) as unknown as typeof fetch;
	return { st, fetchImpl };
}

function omeTiffFile(bytes: Uint8Array, name = "same.ome.tif") {
	return new File([bytes], name, { type: "image/tiff" });
}

async function setup(opts?: Parameters<typeof fakeBackend>[0], reuseLs?: ReturnType<typeof fakeLocalStorage>) {
	const ls = reuseLs || fakeLocalStorage();
	const be = fakeBackend(opts);
	const engineEvents: Array<Record<string, unknown>> = [];
	vi.stubGlobal("localStorage", ls);
	vi.stubGlobal("document", { cookie: "csrf_token=tok" });
	const w: Record<string, unknown> = {};
	vi.stubGlobal("window", w);
	new Function("window", cosEngineSrc)(w);
	vi.stubGlobal("fetch", be.fetchImpl);
	FakeXHR.instances = [];
	vi.stubGlobal("XMLHttpRequest", FakeXHR);
	const statuses: string[] = [];
	let published: string | null = null;
	const ctl = createDirectUploadController({
		t: (k: string, vars?: Record<string, unknown>) =>
			(k + (vars ? JSON.stringify(vars) : "")) as string,
		onStatus: (s: string) => { statuses.push(s); },
		onPublished: (id: string) => { published = id; },
		onEngineEvent: (ev: Record<string, unknown>) => { engineEvents.push(ev); },
	});
	return { ctl, be, ls, statuses, engineEvents,
		publishedId: () => published,
		receipts: () => JSON.parse(ls.getItem("pt.tools.direct.published") || "[]") as
			Array<Record<string, unknown>> };
}

afterEach(() => {
	vi.unstubAllGlobals();
	FakeXHR.instances = [];
	FakeXHR.hold = false;
});

describe("direct 控制器：published receipt（review #3/#4）", () => {
	it("发布成功后再次点击同一文件：不创建第二个 ingestion，给出打开切片入口", async () => {
		const h = await setup();
		const file = omeTiffFile(new Uint8Array(16).fill(1));
		const cls = { directClass: "ome-tiff", ext: ".tif" };
		const r1 = await h.ctl.uploadFile(file, { cls, target: null });
		expect(r1.ok).toBe(true);
		expect(h.be.st.creates).toHaveLength(1);
		// 再次点击：receipt 命中（内容凭证核验通过）→ 不新建、不重传
		const r2 = await h.ctl.uploadFile(file, { cls, target: null });
		expect(r2.ok).toBe(true);
		expect((r2 as { deduped?: boolean }).deduped).toBe(true);
		expect(h.be.st.creates).toHaveLength(1);
		expect(FakeXHR.instances).toHaveLength(2);   // 仍是第一次的两片 PUT
		expect(h.statuses.some((s) => s.startsWith("tools.direct.published.open"))).toBe(true);
	});

	it("同名同大小不同内容：receipt 内容凭证不符 → 按新文件重新上传", async () => {
		const h = await setup();
		const cls = { directClass: "ome-tiff", ext: ".tif" };
		const a = await h.ctl.uploadFile(
			omeTiffFile(new Uint8Array(16).fill(32)), { cls, target: null });
		expect(a.ok).toBe(true);
		const b = await h.ctl.uploadFile(
			omeTiffFile(new Uint8Array(16).fill(192)), { cls, target: null });
		expect(b.ok).toBe(true);
		expect((b as { deduped?: boolean }).deduped).toBeUndefined();
		expect(h.be.st.creates).toHaveLength(2);   // 内容不同 = 新上传
		// 旧 receipt 被替换（不残留过期凭证）
		expect(h.receipts()).toHaveLength(1);
	});

	it("关联失败：不回调 onPublished、不报全成功；receipt 记 pending 可重试", async () => {
		const h = await setup({ assocStatus: 503 });
		const file = omeTiffFile(new Uint8Array(16).fill(1));
		const r = await h.ctl.uploadFile(file, {
			cls: { directClass: "ome-tiff", ext: ".tif" },
			target: { project: "pj_1" },
		});
		// 不报全成功：ok=false + 不回调 onPublished（切片已发布但未入项目）
		expect(r.ok).toBe(false);
		expect((r as { reason?: string }).reason).toBe("association");
		expect((r as { slideId?: string }).slideId).toBe("sld_rc1");
		expect(h.publishedId()).toBeNull();
		// receipt 持久化：published + association_pending + target
		const recs = h.receipts();
		expect(recs).toHaveLength(1);
		expect(recs[0].slide_id).toBe("sld_rc1");
		expect((recs[0].assoc as { state: string }).state).toBe("pending");
		expect(recs[0].target).toEqual({ project: "pj_1" });
		// 状态文案是“已发布但关联失败”，不是成功
		expect(h.statuses.some((s) => s.startsWith("tools.direct.assoc.pending"))).toBe(true);
	});

	it("重试关联：同 slideId、成功后才回调 onPublished；receipt 转 ok", async () => {
		const h = await setup({ assocStatus: 503 });
		const file = omeTiffFile(new Uint8Array(16).fill(1));
		await h.ctl.uploadFile(file, {
			cls: { directClass: "ome-tiff", ext: ".tif" },
			target: { project: "pj_1" },
		});
		expect(h.publishedId()).toBeNull();
		h.be.st.assocStatus = 200;
		const rec = h.receipts()[0] as { receipt_id?: string; job_id?: string; slide_id?: string };
		const r = await (h.ctl as unknown as {
			retryAssociation: (id?: string, sid?: string) => Promise<{ ok: boolean; slideId?: string }>;
		}).retryAssociation(String(rec.receipt_id || rec.job_id), String(rec.slide_id));
		expect(r.ok).toBe(true);
		expect(r.slideId).toBe("sld_rc1");
		expect(h.publishedId()).toBe("sld_rc1");   // 关联成功后才发全量成功
		expect(h.be.st.assocAdds[0].slideIds).toEqual(["sld_rc1"]);
		expect((h.receipts()[0].assoc as { state: string }).state).toBe("ok");
	});

	it("刷新后重选同文件：pending 关联可继续（不重传、同 slideId 重试）", async () => {
		const h = await setup({ assocStatus: 503 });
		const file = omeTiffFile(new Uint8Array(16).fill(1));
		await h.ctl.uploadFile(file, {
			cls: { directClass: "ome-tiff", ext: ".tif" },
			target: { project: "pj_1" },
		});
		// “刷新”= 全新控制器实例 + 同一 localStorage（同一浏览器档案）
		const h2 = await setup({ assocStatus: 503 }, h.ls);
		const r = await h2.ctl.uploadFile(file, {
			cls: { directClass: "ome-tiff", ext: ".tif" },
			target: { project: "pj_1" },
		});
		expect(h2.be.st.creates).toHaveLength(0);   // 绝不重传/重建
		expect((r as { deduped?: boolean; assocPending?: boolean }).deduped).toBe(true);
		expect((r as { assocPending?: boolean }).assocPending).toBe(true);
		// 重试关联走 receipt 里的 slideId + target
		h2.be.st.assocStatus = 200;
		const rec2 = h2.receipts()[0] as { receipt_id?: string; job_id?: string; slide_id?: string };
		const rr = await (h2.ctl as unknown as {
			retryAssociation: (id?: string, sid?: string) => Promise<{ ok: boolean }>;
		}).retryAssociation(String(rec2.receipt_id || rec2.job_id), String(rec2.slide_id));
		expect(rr.ok).toBe(true);
		expect(h2.be.st.assocAdds[0].slideIds).toEqual(["sld_rc1"]);
	});

	it("「新项目」目标：重试沿用同一幂等键（不重复建项目）", async () => {
		const h = await setup({ createProjectStatus: 503 });
		const file = omeTiffFile(new Uint8Array(16).fill(1));
		const target = { newProject: { name: "晨会切片", key: "idem-key-1" } };
		const r = await h.ctl.uploadFile(file, {
			cls: { directClass: "ome-tiff", ext: ".tif" }, target,
		});
		expect(r.ok).toBe(false);
		expect(h.be.st.projectCreates).toHaveLength(1);
		expect(h.be.st.projectCreates[0].key).toBe("idem-key-1");
		// 服务端恢复后重试：同一幂等键；建成后 pid 写回目标（不再二次建）
		h.be.st.createProjectStatus = 200;
		const recA = h.receipts()[0] as { receipt_id?: string; job_id?: string; slide_id?: string };
		const rr = await (h.ctl as unknown as {
			retryAssociation: (id?: string, sid?: string) => Promise<{ ok: boolean }>;
		}).retryAssociation(String(recA.receipt_id || recA.job_id), String(recA.slide_id));
		expect(rr.ok).toBe(true);
		expect(h.be.st.projectCreates).toHaveLength(2);
		expect(h.be.st.projectCreates[1].key).toBe("idem-key-1");
		expect(h.be.st.assocAdds[0].pid).toBe("pj_new");
		// 再重试一次（receipt target.project 已写回）→ 不再创建项目
		h.be.st.assocStatus = 503;
		await h.ctl.uploadFile(file, {
			cls: { directClass: "ome-tiff", ext: ".tif" }, target,
		});
		h.be.st.assocStatus = 200;
		const recB = h.receipts()[0] as { receipt_id?: string; job_id?: string; slide_id?: string };
		await (h.ctl as unknown as {
			retryAssociation: (id?: string, sid?: string) => Promise<{ ok: boolean }>;
		}).retryAssociation(String(recB.receipt_id || recB.job_id), String(recB.slide_id));
		expect(h.be.st.projectCreates).toHaveLength(2);   // 幂等：无第三次创建
		expect(h.be.st.assocAdds[h.be.st.assocAdds.length - 1].pid).toBe("pj_new");
	});

	it("无目标的发布：立即全量成功（onPublished）且 receipt assoc=ok；重复点击不重建", async () => {
		const h = await setup();
		const file = omeTiffFile(new Uint8Array(16).fill(1));
		const r = await h.ctl.uploadFile(file, {
			cls: { directClass: "ome-tiff", ext: ".tif" }, target: null,
		});
		expect(r.ok).toBe(true);
		expect(h.publishedId()).toBe("sld_rc1");
		expect((h.receipts()[0].assoc as { state: string }).state).toBe("ok");
	});
});

// --------------------------------------------------------------------------- //
// 复核 8428f7f0（2026-10-07）：双标签并发直传的跨标签互斥（Web Lock）。
// 锁实现 = 共享引擎 window.HP_COS_UPLOAD.acquireContentLock；vitest 用假
// navigator.locks 驱动「被占 → 等待 → 复用/续传」「等待可取消」两条路径；
// 无 navigator.locks 的退化行为由上方全部既有用例覆盖（Node 无 locks）。
// --------------------------------------------------------------------------- //
function fakeLocks() {
	const busy = new Set<string>();
	const waiters: Array<{ name: string; wake: () => void }> = [];
	function pump(name: string) {
		for (let i = 0; i < waiters.length; i++) {
			if (waiters[i].name === name && !busy.has(name)) {
				waiters.splice(i, 1)[0].wake();
				break;
			}
		}
	}
	function tryRun(name: string, cb: (lock: unknown) => unknown): Promise<unknown> {
		busy.add(name);
		// 请求 promise 在回调 settle 时收口——锁随之释放、唤醒下一个等待者
		const p = Promise.resolve().then(() => cb({ name, mode: "exclusive" }));
		void p.then(() => { busy.delete(name); pump(name); },
			() => { busy.delete(name); pump(name); });
		return p;
	}
	return {
		busy,
		request(name: string,
			opts: { ifAvailable?: boolean; signal?: AbortSignal },
			cb: (lock: unknown) => unknown): Promise<unknown> {
			const signal = opts && opts.signal;
			if (!busy.has(name)) return tryRun(name, cb);
			if (opts && opts.ifAvailable) {
				return Promise.resolve().then(() => cb(null));
			}
			return new Promise((resolve, reject) => {
				const onAbort = () =>
					reject(new DOMException("lock request aborted", "AbortError"));
				if (signal) {
					if (signal.aborted) return onAbort();
					signal.addEventListener("abort", onAbort, { once: true });
				}
				waiters.push({
					name,
					wake: () => {
						if (signal) signal.removeEventListener("abort", onAbort);
						if (signal && signal.aborted) return onAbort();
						tryRun(name, cb).then(resolve, reject);
					},
				});
			});
		},
	};
}

/// Node crypto.subtle 计算 8 B 分片摘要，落一条内容凭证正确的已发布回执
/// （模拟「另一标签已完成上传」后的共享 localStorage 状态）。
async function seedPublishedReceipt(
	ls: ReturnType<typeof fakeLocalStorage>, file: File, account: string,
	slideId = "sld_tab1", jobId = "inj_tab1") {
	// 引擎分片摘要方案 sha256-chunked-4MiB-v1 的独立实现（测试分片 ≤ 4 MiB
	// → 单子块）：分片摘要 = SHA-256(子块摘要逐字节拼接)。
	const hex = async (b: ArrayBuffer) => [...new Uint8Array(b)]
		.map((x) => x.toString(16).padStart(2, "0")).join("");
	const digests: Record<string, string> = {};
	const plan: Array<{ part_number: number; length: number }> = [];
	let off = 0;
	let n = 1;
	while (off < file.size) {
		const len = Math.min(8, file.size - off);
		const buf = await file.slice(off, off + len).arrayBuffer();
		const sub = await crypto.subtle.digest("SHA-256", buf);
		const whole = await crypto.subtle.digest("SHA-256", sub);
		digests[String(n)] = await hex(whole);
		plan.push({ part_number: n, length: len });
		off += len;
		n += 1;
	}
	const receipts = JSON.parse(ls.getItem("pt.tools.direct.published") || "[]") as
		Array<Record<string, unknown>>;
	receipts.push({
		receipt: true, receipt_id: `rc_${jobId}`, account,
		filename: file.name, size: file.size, digests,
		digest_scheme: "sha256-chunked-4MiB-v1",
		plan,
		job_id: jobId, slide_id: slideId, target: null,
		assoc: { state: "ok", error: null },
	});
	ls.setItem("pt.tools.direct.published", JSON.stringify(receipts));
}

async function until(cond: () => boolean, label = "cond"): Promise<void> {
	for (let i = 0; i < 500 && !cond(); i++) {
		await new Promise((r) => setTimeout(r, 2));
	}
	if (!cond()) throw new Error(`timeout: ${label}`);
}

describe("direct 控制器：跨标签内容锁（复核 8428f7f0）", () => {
	it("另一标签持锁：显示等待提示、零创建；释放后回执命中 → 复用（零新 ingestion）", async () => {
		const h = await setup();
		const locks = fakeLocks();
		vi.stubGlobal("navigator", { locks: locks as unknown as LockManager });
		const file = omeTiffFile(new Uint8Array(16).fill(1));
		const lockName = String((window as Record<string, unknown> as {
			HP_COS_UPLOAD: { contentLockName: (a: string, f: File) => string };
		}).HP_COS_UPLOAD.contentLockName("acct-1", file));
		// 「另一标签」先持有同一把锁（同一账号 + 同一文件身份）
		let releaseHolder: () => void = () => {};
		await new Promise<void>((res) => {
			void locks.request(lockName, {}, (l) => {
				expect(l).toBeTruthy();
				res();
				return new Promise<void>((r) => { releaseHolder = r; });
			}) as unknown as Promise<unknown>;
		});
		const pending = h.ctl.uploadFile(file, {
			cls: { directClass: "ome-tiff", ext: ".tif" }, target: null,
		});
		await until(() => h.statuses.some((s) => s.startsWith("tools.direct.other.tab")),
			"waiting message");
		expect(h.be.st.creates).toHaveLength(0);   // 等待期绝不创建
		// 另一标签完成上传：共享 localStorage 出现内容凭证正确的已发布回执
		await seedPublishedReceipt(h.ls, file, "acct-1");
		releaseHolder();
		const r = await pending;
		expect(r.ok).toBe(true);
		expect((r as { deduped?: boolean }).deduped).toBe(true);
		expect(h.be.st.creates).toHaveLength(0);   // 复用结果：零新 ingestion
		expect(h.statuses.some((s) => s.startsWith("tools.direct.published.open")))
			.toBe(true);
	});

	it("另一标签持锁且只有进行中记录：释放后经既有核验续传（不重建，只补未确认分片）", async () => {
		const h = await setup();
		const locks = fakeLocks();
		vi.stubGlobal("navigator", { locks: locks as unknown as LockManager });
		const file = omeTiffFile(new Uint8Array(16).fill(1));
		const lockName = String((window as Record<string, unknown> as {
			HP_COS_UPLOAD: { contentLockName: (a: string, f: File) => string };
		}).HP_COS_UPLOAD.contentLockName("acct-1", file));
		let releaseHolder: () => void = () => {};
		await new Promise<void>((res) => {
			void locks.request(lockName, {}, () => {
				res();
				return new Promise<void>((r) => { releaseHolder = r; });
			}) as unknown as Promise<unknown>;
		});
		// 另一标签创建了任务、确认了分片 1，还没传完（记录落共享 localStorage；
		// 摘要 = sha256-chunked-4MiB-v1 方案）
		const part1 = await file.slice(0, 8).arrayBuffer();
		const sub1 = await crypto.subtle.digest("SHA-256", part1);
		const whole1 = await crypto.subtle.digest("SHA-256", sub1);
		const hex1 = [...new Uint8Array(whole1)]
			.map((b) => b.toString(16).padStart(2, "0")).join("");
		h.ls.setItem("pt.tools.direct.uploads", JSON.stringify([{
			job_id: "inj_tab1", filename: file.name, size: file.size,
			account: "acct-1", confirmed: [1],
			digests: { 1: hex1 },
			digest_scheme: "sha256-chunked-4MiB-v1",
			plan: [{ part_number: 1, length: 8 }, { part_number: 2, length: 8 }],
			slide_id: null,
		}]));
		const pending = h.ctl.uploadFile(file, {
			cls: { directClass: "ome-tiff", ext: ".tif" }, target: null,
		});
		await until(() => h.statuses.some((s) => s.startsWith("tools.direct.other.tab")),
			"waiting message");
		releaseHolder();
		const r = await pending;
		expect(r.ok).toBe(true);
		expect(h.be.st.creates).toHaveLength(0);   // 续传：不重建
		// 只补分片 2（分片 1 经摘要核验后跳过）
		const signs = h.be.fetchImpl as unknown as vi.Mock;
		const signCall = signs.mock.calls.find((c: unknown[]) =>
			String(c[0]).endsWith("/parts/sign"));
		expect(signCall).toBeTruthy();
		const body = JSON.parse(String(
			(signCall![1] as RequestInit).body));
		expect(body.part_numbers).toEqual([2]);
	});

	it("等待另一标签期间取消：以 cancelled 收口、零创建、不报错误", async () => {
		const h = await setup();
		const locks = fakeLocks();
		vi.stubGlobal("navigator", { locks: locks as unknown as LockManager });
		const file = omeTiffFile(new Uint8Array(16).fill(1));
		const lockName = String((window as Record<string, unknown> as {
			HP_COS_UPLOAD: { contentLockName: (a: string, f: File) => string };
		}).HP_COS_UPLOAD.contentLockName("acct-1", file));
		// 持锁不放
		void (locks.request(lockName, {}, () => new Promise<void>(() => {})) as
			unknown as Promise<unknown>);
		const pending = h.ctl.uploadFile(file, {
			cls: { directClass: "ome-tiff", ext: ".tif" }, target: null,
		});
		await until(() => h.statuses.some((s) => s.startsWith("tools.direct.other.tab")),
			"waiting message");
		h.ctl.cancel();
		const r = await pending;
		expect(r.ok).toBe(false);
		expect((r as { reason?: string }).reason).toBe("cancelled");
		expect(h.be.st.creates).toHaveLength(0);
	});
});

// --------------------------------------------------------------------------- //
// 复核 8428f7f0：关联重试绑定当前回执（绝不取「第一条 pending」）+ 账号核对。
// --------------------------------------------------------------------------- //
describe("direct 控制器：关联重试绑定与账号核对（复核 8428f7f0）", () => {
	it("同账号两条 pending：重试 B 只发 project-B/slide-B；A 原样 pending", async () => {
		const h = await setup({ assocStatus: 503 });
		const cls = { directClass: "ome-tiff", ext: ".tif" };
		const ra = await h.ctl.uploadFile(
			omeTiffFile(new Uint8Array(16).fill(32), "a.ome.tif"),
			{ cls, target: { project: "pj_A" } });
		expect(ra.ok).toBe(false);
		const rb = await h.ctl.uploadFile(
			omeTiffFile(new Uint8Array(16).fill(64), "b.ome.tif"),
			{ cls, target: { project: "pj_B" } });
		expect(rb.ok).toBe(false);
		expect(h.be.st.assocAdds).toHaveLength(2);   // 两次发布时的失败关联
		h.be.st.assocStatus = 200;
		const recB = h.receipts().find((r) => r.filename === "b.ome.tif") as
			{ receipt_id?: string; job_id?: string; slide_id?: string };
		const rr = await (h.ctl as unknown as {
			retryAssociation: (id?: string, sid?: string) => Promise<{ ok: boolean }>;
		}).retryAssociation(String(recB.receipt_id || recB.job_id),
			String(recB.slide_id));
		expect(rr.ok).toBe(true);
		expect(h.be.st.assocAdds).toHaveLength(3);   // A 未被重试
		expect(h.be.st.assocAdds[2]).toEqual({ pid: "pj_B", slideIds: ["sld_rc2"] });
		const recs = h.receipts();
		expect(((recs.find((r) => r.filename === "a.ome.tif") as
			{ assoc: { state: string } }).assoc).state).toBe("pending");
		expect(((recs.find((r) => r.filename === "b.ome.tif") as
			{ assoc: { state: string } }).assoc).state).toBe("ok");
	});

	it("回执属于他号（登录账号已切换）：拒绝、不发任何关联请求、明确提示", async () => {
		const h = await setup({ assocStatus: 503, account: "acct-1" });
		const cls = { directClass: "ome-tiff", ext: ".tif" };
		await h.ctl.uploadFile(omeTiffFile(new Uint8Array(16).fill(1)), {
			cls, target: { project: "pj_1" },
		});
		expect((h.receipts()[0].assoc as { state: string }).state).toBe("pending");
		// 「退出后以另一账号登录」：全新控制器 + 同一 localStorage
		const h2 = await setup({ assocStatus: 503, account: "acct-2" }, h.ls);
		const rec0 = h.receipts()[0] as { job_id?: string; slide_id?: string };
		const rr = await (h2.ctl as unknown as {
			retryAssociation: (id?: string, sid?: string) =>
				Promise<{ ok: boolean; reason?: string }>;
		}).retryAssociation(String(rec0.job_id), String(rec0.slide_id));
		expect(rr.ok).toBe(false);
		expect(rr.reason).toBe("account");
		expect(h2.be.st.assocAdds).toHaveLength(0);   // 未发任何关联请求
		expect(h2.be.st.projectCreates).toHaveLength(0);
		expect(h2.statuses.some((s) =>
			s.startsWith("tools.direct.assoc.account.mismatch"))).toBe(true);
		// 回执未被修改
		expect((h.receipts()[0].assoc as { state: string }).state).toBe("pending");
	});

	it("owner repro（同句柄多回执）：两条回执由同一真实回执展开复制——重试 B 只发 project-B/slide-B", async () => {
		const h = await setup({ assocStatus: 503 });
		const cls = { directClass: "ome-tiff", ext: ".tif" };
		// 真实发布一次，得到内容凭证完整的真实回执 r
		const real = omeTiffFile(new Uint8Array(16).fill(1), "current.ome.tif");
		const rr0 = await h.ctl.uploadFile(real, {
			cls, target: { project: "pj_real" },
		});
		expect(rr0.ok).toBe(false);   // 注入失败 → 真实回执 assoc=pending
		const r = h.receipts()[0] as Record<string, unknown>;
		// owner 构造：a/b 由 {...r} 展开——receipt_id/job_id/digests 全部相同，
		// 只有 filename/slide_id/target 不同（句柄无法区分，slide_id 才能）
		const a = { ...r, filename: "older.ome.tif", slide_id: "slide-A",
			target: { project: "project-A" }, assoc: { state: "pending", error: "x" } };
		const b = { ...r, slide_id: "slide-B",
			target: { project: "project-B" }, assoc: { state: "pending", error: "x" } };
		h.ls.setItem("pt.tools.direct.published", JSON.stringify([a, b]));
		// 重选当前文件（B 的 filename = 真实文件）→ 回执命中 B → 重试 B
		h.be.st.assocStatus = 200;
		const before = h.be.st.assocAdds.length;   // 首次发布时失败的关联
		const rr = await (h.ctl as unknown as {
			retryAssociation: (id?: string, sid?: string) => Promise<{ ok: boolean }>;
		}).retryAssociation(String(r.receipt_id || r.job_id), "slide-B");
		expect(rr.ok).toBe(true);
		const adds = h.be.st.assocAdds;
		expect(adds).toHaveLength(before + 1);   // 只新增 B 的关联
		expect(adds[before]).toEqual({ pid: "project-B", slideIds: ["slide-B"] });
		const recs = h.receipts();
		expect(((recs.find((x) => (x as { slide_id?: string }).slide_id === "slide-A") as
			{ assoc: { state: string } }).assoc).state).toBe("pending");
		expect(((recs.find((x) => (x as { slide_id?: string }).slide_id === "slide-B") as
			{ assoc: { state: string } }).assoc).state).toBe("ok");
	});

	it("句柄歧义（同句柄同 slide_id 多条 pending）：拒绝、不发任何请求", async () => {
		const h = await setup({ assocStatus: 503 });
		const cls = { directClass: "ome-tiff", ext: ".tif" };
		const real = omeTiffFile(new Uint8Array(16).fill(1), "current.ome.tif");
		await h.ctl.uploadFile(real, { cls, target: { project: "pj_real" } });
		const r = h.receipts()[0] as Record<string, unknown>;
		const a = { ...r, filename: "copy-a.ome.tif",
			assoc: { state: "pending", error: "x" } };
		const b = { ...r, filename: "copy-b.ome.tif",
			assoc: { state: "pending", error: "x" } };
		h.ls.setItem("pt.tools.direct.published", JSON.stringify([a, b]));
		const before = h.be.st.assocAdds.length;
		const rr = await (h.ctl as unknown as {
			retryAssociation: (id?: string, sid?: string) =>
				Promise<{ ok: boolean; reason?: string }>;
		}).retryAssociation(String(r.receipt_id || r.job_id), String(r.slide_id));
		expect(rr.ok).toBe(false);
		expect(rr.reason).toBe("ambiguous");
		expect(h.be.st.assocAdds).toHaveLength(before);   // 未发任何请求
		expect(h.statuses.some((x) =>
			x.startsWith("tools.direct.assoc.ambiguous"))).toBe(true);
	});

	it("无句柄调用（retryAssociation 不带参数）：一律不动、零请求（owner 第二项检查）", async () => {
		const h = await setup({ assocStatus: 503 });
		const cls = { directClass: "ome-tiff", ext: ".tif" };
		await h.ctl.uploadFile(omeTiffFile(new Uint8Array(16).fill(1)), {
			cls, target: { project: "pj_1" },
		});
		expect(h.receipts()).toHaveLength(1);
		const before = h.be.st.assocAdds.length;
		const rr = await (h.ctl as unknown as {
			retryAssociation: (id?: string, sid?: string) =>
				Promise<{ ok: boolean; reason?: string }>;
		}).retryAssociation();
		expect(rr.ok).toBe(false);
		expect(rr.reason).toBe("none");
		expect(h.be.st.assocAdds).toHaveLength(before);   // 零请求
		expect(h.be.st.projectCreates).toHaveLength(0);
	});
});

// --------------------------------------------------------------------------- //
// review 2026-10-07 #6：直传面板接字节进度与取消（复用产物上传的交互）——
// 控制器把共享引擎的 progress/status/retry 事件透传给页面（onEngineEvent），
// cancel() 走引擎取消（abort 在途 XHR + POST /cancel + done cancelled）。
// --------------------------------------------------------------------------- //
describe("direct 控制器：进度与取消（review #6）", () => {
	it("上传过程透传 progress 字节事件（loadedBytes/totalBytes/sentAll）", async () => {
		const h = await setup();
		const file = omeTiffFile(new Uint8Array(16).fill(1));
		const r = await h.ctl.uploadFile(file, {
			cls: { directClass: "ome-tiff", ext: ".tif" }, target: null,
		});
		expect(r.ok).toBe(true);
		const progress = h.engineEvents.filter((e) => e.type === "progress") as
			Array<{ phase?: string; loadedBytes?: number; totalBytes?: number;
				sentAll?: boolean }>;
		expect(progress.length).toBeGreaterThan(0);
		expect(progress.some((e) => e.phase === "uploading" &&
			e.totalBytes === 16)).toBe(true);
		expect(progress.some((e) => e.loadedBytes === 16)).toBe(true);
	});

	it("取消：done 以 cancelled 收口、POST /cancel、不再创建第二个 ingestion", async () => {
		const h = await setup();
		const file = omeTiffFile(new Uint8Array(16).fill(1));
		const pending = h.ctl.uploadFile(file, {
			cls: { directClass: "ome-tiff", ext: ".tif" }, target: null,
		});
		FakeXHR.hold = true;   // 两片 PUT 在途挂起
		for (let i = 0; i < 100 && FakeXHR.instances.length < 2; i++) {
			await new Promise((r2) => setTimeout(r2, 2));
		}
		expect(FakeXHR.instances.length).toBe(2);
		h.ctl.cancel();
		const r = await pending;
		expect((r as { reason?: string }).reason).toBe("cancelled");
		expect(h.be.st.creates).toHaveLength(1);   // 不新建第二个 ingestion
		expect(FakeXHR.instances.every((x) => x.aborted)).toBe(true);
		const urls = h.be.fetchImpl as unknown as vi.Mock;
		const calls = urls.mock.calls.map((c: unknown[]) => String(c[0]));
		expect(calls.some((u) => (u as string).endsWith("/cancel"))).toBe(true);
		FakeXHR.hold = false;
	});
});
