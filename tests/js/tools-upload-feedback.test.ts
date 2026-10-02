/**
 * R1 跟进：/tools/slides 上传反馈的可见性与按任务隔离 + 结构化错误文案
 * （真实 tools-slides-upload.js + 真实共享引擎 cos-uploader.js，fake DOM
 * 真正维护子节点，断言的是 #upload-status 里实际渲染出的文本/链接）。
 *
 *  - 从任务列表（含刷新后：结果面板未指向任何任务）发起上传：控制器先
 *    经 onSelectJob 把面板切到该任务，再发任何请求——登录提示/错误/进度
 *    都出现在当前面板里；
 *  - 反馈按任务保存：面板指向别的任务时不画别人的状态，切回时重放；
 *  - 上传进行中：其他行的上传按钮禁用并说明，进行中的行给「查看上传进度」，
 *    取消按钮只在面板指向进行中的任务时出现；
 *  - 服务端结构化错误（409 cos_waiting_limit / 413 upload_too_large /
 *    503 cos_pool_below_product_limit / 未知 500 带对象 error）映射为可读
 *    文案，绝不出现 "[object Object]"。
 *
 * 行为级 e2e：tests/browser/slide_tools_r1/run_e2e.js（m1/m2/m3）。
 */
import { afterEach, describe, expect, it, vi } from "vitest";
import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
// eslint-disable-next-line
import { createUploadController, describeUploadError } from "../../static/tools/tools-slides-upload.js";

const here = dirname(fileURLToPath(import.meta.url));
const cosEngineSrc = readFileSync(
	resolve(here, "../../static/upload/cos-uploader.js"), "utf8");

class FakeEl {
	id = "";
	tagName: string;
	children: FakeEl[] = [];
	parentNode: FakeEl | null = null;
	hidden = false;
	disabled = false;
	title = "";
	className = "";
	href = "";
	type = "";
	returnValue = "";
	dataset: Record<string, string> = {};
	private own = "";
	private listeners: Record<string, Array<() => unknown>> = {};
	constructor(tag = "div", text = "") { this.tagName = tag; this.own = text; }
	get textContent(): string {
		return this.own + this.children.map((c) => c.textContent).join("");
	}
	set textContent(v: string) { this.own = String(v); this.children = []; }
	appendChild(c: FakeEl) { c.parentNode = this; this.children.push(c); return c; }
	insertBefore(c: FakeEl, ref: FakeEl) {
		c.parentNode = this;
		const i = this.children.indexOf(ref);
		if (i < 0) this.children.push(c); else this.children.splice(i, 0, c);
		return c;
	}
	addEventListener(type: string, fn: () => unknown) {
		(this.listeners[type] = this.listeners[type] || []).push(fn);
	}
	removeEventListener() {}
	setAttribute() {}
	click() { for (const f of this.listeners.click || []) f(); }
	find(pred: (e: FakeEl) => boolean): FakeEl | null {
		for (const c of this.children) {
			if (pred(c)) return c;
			const r = c.find(pred);
			if (r) return r;
		}
		return null;
	}
}

function mkDoc() {
	const els = new Map<string, FakeEl>();
	return {
		cookie: "csrf_token=tok",
		getElementById(id: string) {
			if (!els.has(id)) { const e = new FakeEl(); e.id = id; els.set(id, e); }
			return els.get(id)!;
		},
		createElement: (tag: string) => new FakeEl(tag),
		createTextNode: (s: string) => new FakeEl("#text", s),
		addEventListener() {},
		querySelector: () => new FakeEl(),
		querySelectorAll: () => [] as unknown[],
	};
}

function resp(body: unknown, status = 200) {
	return {
		ok: status >= 200 && status < 300,
		status,
		json: () => Promise.resolve(body),
		headers: { get: () => null },
	} as unknown as Response;
}

const VIEWABLE = ["classic-bigtiff-jpeg-pyramid"];
const CAPS = {
	available: true, manual_only: true,
	max_size_bytes: 900000000, part_bytes: 8, url_ttl_seconds: 600,
	max_concurrent_parts: 2, sign_batch_max_parts: 4, policy_version: "v1-manual",
	formats: ["tif", "tiff"],
};

type Opts = {
	capStatus?: number;
	create?: () => Promise<Response>;
};

async function setup(opts: Opts = {}) {
	const doc = mkDoc();
	const calls: string[] = [];
	const fetchImpl = vi.fn((url: string, o?: RequestInit) => {
		const method = String((o && o.method) || "GET");
		calls.push(`${method} ${url}`);
		if (String(url) === "/api/tools/slides/upload-capability") {
			if (opts.capStatus) return Promise.resolve(resp({ error: "auth_required" }, opts.capStatus));
			return Promise.resolve(resp({
				cos_upload: CAPS, viewable_formats: VIEWABLE,
				account: "u1", account_label: "u1@x",
			}));
		}
		if (method === "POST" && String(url) === "/api/ingestions") {
			return opts.create ? opts.create()
				: Promise.resolve(resp({ job_id: "inj_1", state: "preparing", stage: "uploading" }, 202));
		}
		return Promise.resolve(resp({ state: "failed", stage: "terminal", fail_code: "demo" }));
	}) as unknown as typeof fetch;
	const w: Record<string, unknown> = { HP_COS_UPLOAD: null };
	new Function("window", "document", "fetch", "location", cosEngineSrc)(
		w, doc, fetchImpl, { href: "http://local/tools/slides", origin: "http://local" });
	const store = new Map<string, string>();
	vi.stubGlobal("window", w);
	vi.stubGlobal("document", doc);
	vi.stubGlobal("fetch", fetchImpl);
	vi.stubGlobal("localStorage", {
		getItem: (k: string) => (store.has(k) ? store.get(k)! : null),
		setItem: (k: string, v: string) => { store.set(k, v); },
		removeItem: (k: string) => { store.delete(k); },
	});
	vi.stubGlobal("navigator", {
		locks: { request: (_n: string, _o: unknown, fn: (l: unknown) => Promise<unknown>) =>
			Promise.resolve(fn({ name: "lock" })) },
	});
	const jobs: Record<string, Record<string, unknown>> = {};
	for (const id of ["job-a", "job-b"]) {
		jobs[id] = {
			id, state: "ready", source: { name: `${id}.kfb`, size: 10 }, modality: "brightfield",
			result: { outputBytes: 5000, format: VIEWABLE[0], sha256: "x" }, upload: null, intent: null,
		};
	}
	const runner = {
		async getJob(id: string) { return jobs[id] ? { ...jobs[id] } : null; },
		async setJobUpload(id: string, patch: Record<string, unknown>) {
			jobs[id].upload = { ...((jobs[id].upload as object) || {}), ...patch };
		},
		async setJobIntent() { return undefined; },
		async artifactView() { return { size: 5000, slice: (s: number, e: number) => ({ s, e }) }; },
	};
	const selected: string[] = [];
	const status = doc.getElementById("upload-status");
	const section = doc.getElementById("result-section");
	section.hidden = true;  // 刷新后：结果面板隐藏、不指向任何任务
	let ctl: Ctl;
	const t = (k: string, vars?: Record<string, unknown>) => (vars && Object.keys(vars).length
		? `${k}|${Object.entries(vars).map(([a, b]) => `${a}=${b}`).join(",")}` : k);
	const callsAtSelect: number[] = [];
	ctl = createUploadController({
		runner: runner as never, t, onJobsRefresh: null, onPublished: null,
		onSelectJob: async (id: string) => {
			callsAtSelect.push(calls.length);
			selected.push(id);
			section.hidden = false;
			await ctl.setResultJob(id);
		},
	}) as unknown as Ctl;
	/** #upload-status 对用户可见 = 自身与结果面板都未隐藏。 */
	const visibleStatus = () => (section.hidden || status.hidden ? "" : status.textContent);
	return { ctl, doc, calls, selected, callsAtSelect, status, section, visibleStatus, jobs };
}

type Ctl = {
	startOrContinue: (id: string) => Promise<unknown>;
	setResultJob: (id: string) => Promise<unknown>;
	renderRowSegment: (job: unknown, actions: FakeEl, del: FakeEl | null) => void;
	cancel: () => void;
	isDisabled: (id: string) => boolean;
	busyJobId: () => string | null;
};

afterEach(() => { vi.unstubAllGlobals(); });

describe("任务列表发起的上传：反馈可见（刷新后结果面板隐藏）", () => {
	it("未登录：先切面板再请求能力；可见的登录说明 + 可点的登录链接", async () => {
		const { ctl, calls, selected, callsAtSelect, status, visibleStatus } = await setup({ capStatus: 401 });
		await ctl.startOrContinue("job-a");
		expect(selected).toEqual(["job-a"]);
		expect(callsAtSelect[0]).toBe(0);          // 选中发生在任何 /api/ 请求之前
		expect(calls).toEqual(["GET /api/tools/slides/upload-capability"]);
		expect(visibleStatus()).toContain("tools.upload.login.required");
		const link = status.find((e) => e.tagName === "a");
		expect(link && link.href).toBe("/login?next=/tools/slides");
	});

	it("反馈按任务隔离：面板指向别的任务时不显示，切回时重放", async () => {
		const { ctl, status } = await setup({ capStatus: 401 });
		await ctl.startOrContinue("job-a");
		expect(status.textContent).toContain("tools.upload.login.required");
		await ctl.setResultJob("job-b");
		expect(status.textContent).toBe("");
		await ctl.setResultJob("job-a");
		expect(status.textContent).toContain("tools.upload.login.required");
		expect(status.find((e) => e.tagName === "a")).not.toBeNull();
	});

	it("上传进行中：别的行上传禁用并说明；进行中的行给「查看上传进度」；取消只跟随该任务的面板", async () => {
		let release: (r: Response) => void = () => {};
		const gate = new Promise<Response>((r) => { release = r; });
		const { ctl, doc, status, jobs } = await setup({ create: () => gate });
		const run = ctl.startOrContinue("job-a");
		await vi.waitFor(() => expect(ctl.busyJobId()).toBe("job-a"));
		const cancelBtn = doc.getElementById("upload-cancel-btn");
		expect(cancelBtn.hidden).toBe(false);

		const rowB = new FakeEl("article"); const actB = rowB.appendChild(new FakeEl());
		ctl.renderRowSegment(jobs["job-b"], actB, null);
		const upB = actB.find((e) => e.dataset.action === "upload")!;
		expect(upB.disabled).toBe(true);
		expect(upB.title).toBe("tools.upload.busy.other");

		const rowA = new FakeEl("article"); const actA = rowA.appendChild(new FakeEl());
		(jobs["job-a"] as Record<string, unknown>).upload = { ingestionId: "inj_1", state: "uploading" };
		ctl.renderRowSegment(jobs["job-a"], actA, null);
		expect(actA.find((e) => e.dataset.action === "upload-show")).not.toBeNull();

		// 面板切到 B：A 的进度不出现在 B 的面板里
		await ctl.setResultJob("job-b");
		expect(status.textContent).not.toContain("upload.cos.stage");
		expect(status.textContent).not.toContain("tools.upload.working");
		release(resp({ error: "x", code: "cos_waiting_limit" }, 409));
		await run;
		expect(status.textContent).toBe("");                 // A 的失败也不画到 B
		await ctl.setResultJob("job-a");
		expect(status.textContent).toContain("tools.upload.err.waiting_limit");
	});
});

describe("结构化服务端错误 → 可读文案（不出现 [object Object]）", () => {
	const cases: Array<[string, number, Record<string, unknown>, RegExp]> = [
		["账号等待上限 409", 409, { error: "已有等待中的任务", code: "cos_waiting_limit" }, /^tools\.upload\.err\.waiting_limit$/],
		["产品上限 413", 413, { error: "文件超过平台上限", code: "upload_too_large", max_size_bytes: 4096 }, /^tools\.upload\.too\.large\|max=4\.0 KiB$/],
		["池配置 503", 503, { error: "…", code: "cos_pool_below_product_limit", max_size_bytes: 1 }, /^tools\.upload\.err\.pool_config$/],
		["未知 500（error 为对象）", 500, { error: { nested: true } }, /^tools\.upload\.err\.http\|status=500,code=—$/],
	];
	for (const [name, st, body, want] of cases) {
		it(`${name}：面板文案可读`, async () => {
			const { ctl, visibleStatus } = await setup({ create: () => Promise.resolve(resp(body, st)) });
			await ctl.startOrContinue("job-a");
			const txt = visibleStatus();
			expect(txt).not.toContain("[object Object]");
			expect(txt).toMatch(want);
		});
	}

	it("413：只禁上传（本地保存不受影响），刷新后的列表行同样禁用", async () => {
		const { ctl, doc, jobs } = await setup({
			create: () => Promise.resolve(resp({ code: "upload_too_large", max_size_bytes: 4096 }, 413)),
		});
		await ctl.startOrContinue("job-a");
		expect(ctl.isDisabled("job-a")).toBe(true);
		expect(doc.getElementById("upload-btn").disabled).toBe(true);
		expect(doc.getElementById("save-btn").disabled).toBe(false);
		const row = new FakeEl("article"); const act = row.appendChild(new FakeEl());
		ctl.renderRowSegment(jobs["job-a"], act, null);
		expect(act.find((e) => e.dataset.action === "upload")!.disabled).toBe(true);
	});

	it("describeUploadError：非稳定码不外显；Error 保留其 message", () => {
		expect(describeUploadError({ status: 409, data: { code: "<b>x</b>" } }))
			.toEqual({ key: "tools.upload.err.http", vars: { status: 409, code: "—" } });
		expect(describeUploadError(new Error("boom")))
			.toEqual({ key: "tools.upload.failed", vars: { e: "boom" } });
		expect(describeUploadError({ weird: {} })).toEqual({ key: "tools.upload.err.unknown" });
	});
});
