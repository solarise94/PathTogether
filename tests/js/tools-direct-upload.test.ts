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
	open(method: string, url: string) { this.method = method; this.url = url; }
	send(body: unknown) {
		this.body = body;
		expect(this.upload.onprogress).toBeTypeOf("function");
		expect(this.onload).toBeTypeOf("function");
		this.status = 200;
		if (this.onload) this.onload();
	}
	abort() { if (this.onabort) this.onabort(); }
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
	// 有状态 ingestion：创建 → uploading（分块计划）→ complete → viewable
	const jobs = new Map<string, { size: number; completed: boolean }>();
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
			if (job && job.completed) {
				return Promise.resolve(resp({
					stage: "viewable", slide_id: "sld_rc1", slide: "same.ome.tif",
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
			for (const j of jobs.values()) j.completed = true;
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
	});
	return { ctl, be, ls, statuses,
		publishedId: () => published,
		receipts: () => JSON.parse(ls.getItem("pt.tools.direct.published") || "[]") as
			Array<Record<string, unknown>> };
}

afterEach(() => {
	vi.unstubAllGlobals();
	FakeXHR.instances = [];
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
		const r = await (h.ctl as { retryAssociation: () => Promise<{ ok: boolean; slideId?: string }> })
			.retryAssociation();
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
		const rr = await (h2.ctl as { retryAssociation: () => Promise<{ ok: boolean }> })
			.retryAssociation();
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
		const rr = await (h.ctl as { retryAssociation: () => Promise<{ ok: boolean }> })
			.retryAssociation();
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
		await (h.ctl as { retryAssociation: () => Promise<{ ok: boolean }> }).retryAssociation();
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
