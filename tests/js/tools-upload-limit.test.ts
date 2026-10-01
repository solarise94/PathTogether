/**
 * R7：/tools/slides 转换工具页的上传限额语义（真实 tools-slides-upload.js +
 * 真实共享引擎 cos-uploader.js）。
 *
 *  - 限额取自服务端能力端点（/api/tools/slides/upload-capability 的
 *    cos_upload.max_size_bytes，经 resolveConfig 规范化）——页面里没有
 *    硬编码字节常量：同一产物在 max=1000 下被禁、在 max=900000000 下照常
 *    创建 ingestion；
 *  - 产物超限：只禁上传（#upload-btn + 控制器 disabled），文案携带服务端
 *    上限值；本地保存入口（#save-btn）不受影响；零 ingestion 请求。
 *
 * 行为级 e2e 证据：tests/browser/slide_tools_c4/run_e2e.js（f-oversize）。
 */
import { afterEach, describe, expect, it, vi } from "vitest";
import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
// eslint-disable-next-line
import { createUploadController } from "../../static/tools/tools-slides-upload.js";

const here = dirname(fileURLToPath(import.meta.url));
const cosEngineSrc = readFileSync(
	resolve(here, "../../static/upload/cos-uploader.js"), "utf8");

function mkEl(id?: string) {
	return {
		id: id || "",
		disabled: false,
		hidden: false,
		textContent: "",
		title: "",
		className: "",
		appendChild(c: unknown) { return c; },
		insertBefore(c: unknown) { return c; },
		addEventListener() {},
		removeEventListener() {},
		click() {},
	};
}

function mkDoc() {
	const els = new Map<string, ReturnType<typeof mkEl>>();
	return {
		cookie: "csrf_token=tok",
		getElementById(id: string) {
			if (!els.has(id)) els.set(id, mkEl(id));
			return els.get(id)!;
		},
		createElement: () => mkEl(),
		addEventListener() {},
		querySelector: () => mkEl(),
		querySelectorAll: () => [] as unknown[],
		els,
	};
}

function fakeLocalStorage() {
	const m = new Map<string, string>();
	return {
		getItem: (k: string) => (m.has(k) ? m.get(k)! : null),
		setItem: (k: string, v: string) => { m.set(k, String(v)); },
		removeItem: (k: string) => { m.delete(k); },
		clear() { m.clear(); },
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

function cosCaps(maxSize: number) {
	return {
		available: true, manual_only: true,
		max_size_bytes: maxSize, part_bytes: 8, url_ttl_seconds: 600,
		max_concurrent_parts: 2, sign_batch_max_parts: 4, policy_version: "v1-manual",
		formats: ["tif", "tiff"],
	};
}

const VIEWABLE = ["classic-bigtiff-jpeg-pyramid",
	"ome-bigtiff-subifd-multichannel-jpeg-passthrough"];

type FetchLog = { method: string; url: string; body?: unknown };

/** 组装控制器运行环境（真实模块 + 真实共享引擎 + 全 stub DOM）。 */
async function setup(maxSize: number) {
	const doc = mkDoc();
	const storage = fakeLocalStorage();
	const messages: string[] = [];
	const fetchLog: FetchLog[] = [];
	const fetchImpl = vi.fn((url: string, opts?: RequestInit) => {
		const method = String((opts && opts.method) || "GET");
		let body: unknown = undefined;
		try { body = opts && opts.body ? JSON.parse(String(opts.body)) : undefined; } catch { /* */ }
		fetchLog.push({ method, url: String(url), body });
		if (String(url) === "/api/tools/slides/upload-capability") {
			return Promise.resolve(resp({
				cos_upload: cosCaps(maxSize), viewable_formats: VIEWABLE,
				account: "u1", account_label: "u1@x",
			}));
		}
		if (method === "POST" && String(url) === "/api/ingestions") {
			return Promise.resolve(resp(
				{ job_id: "inj_t1", state: "preparing", stage: "uploading" }, 202));
		}
		if (String(url).startsWith("/api/ingestions/")) {
			return Promise.resolve(resp(
				{ state: "failed", stage: "terminal", fail_code: "demo" }));
		}
		return Promise.resolve(resp({}));
	}) as unknown as typeof fetch;
	const w: Record<string, unknown> = { HP_COS_UPLOAD: null };
	// 真实共享引擎（classic script）注入 fake window
	new Function("window", "document", "fetch", "location", cosEngineSrc)(
		w, doc, fetchImpl, { href: "http://local/tools/slides", origin: "http://local" });
	vi.stubGlobal("window", w);
	vi.stubGlobal("document", doc);
	vi.stubGlobal("fetch", fetchImpl);
	vi.stubGlobal("localStorage", storage);
	vi.stubGlobal("navigator", {
		locks: { request: (_n: string, _o: unknown, fn: (l: unknown) => Promise<unknown>) =>
			Promise.resolve(fn({ name: "lock" })) },
	});
	const artifact = { size: 5000, slice: (s: number, e: number) => ({ s, e }) };
	const runner = {
		async getJob(id: string) {
			return {
				id, state: "ready",
				result: { outputBytes: 5000, format: VIEWABLE[0] },
			};
		},
		async setJobUpload() { return undefined; },
		async setJobIntent() { return undefined; },
		async artifactView() { return artifact; },
	};
	const ctl = createUploadController({
		runner: runner as never,
		t: (k: string, vars?: Record<string, unknown>) => {
			const s = vars && Object.keys(vars).length
				? `${k}:${Object.values(vars).join(",")}` : k;
			messages.push(s);
			return s;
		},
		onJobsRefresh: null,
		onPublished: null,
	}) as unknown as {
		startOrContinue: (id: string) => Promise<unknown>;
		setResultJob: (id: string) => Promise<unknown>;
		isDisabled: (id: string) => boolean;
	};
	return { ctl, doc, fetchLog, messages, fetchImpl };
}

afterEach(() => {
	vi.unstubAllGlobals();
});

describe("R7 工具页上传限额（服务端下发，非硬编码）", () => {
	it("产物 5000B > 服务端上限 1000B：上传禁用 + 原因带服务端上限值；本地保存不受影响；零 ingestion 请求", async () => {
		const { ctl, doc, fetchLog, messages } = await setup(1000);
		const saveBtn = doc.getElementById("save-btn");
		await ctl.setResultJob("job-1");
		await ctl.startOrContinue("job-1");
		expect(ctl.isDisabled("job-1")).toBe(true);
		expect(doc.getElementById("upload-btn").disabled).toBe(true);
		// 本地保存入口不被禁用（超限只停上传，产物保留可保存）
		expect(saveBtn.disabled).toBe(false);
		// 文案：tools.upload.too.large + 服务端上限（fmtBytes(1000)=1000 B）
		const tooLarge = messages.find((m) => m.indexOf("tools.upload.too.large") >= 0);
		expect(tooLarge).toBeTruthy();
		expect(tooLarge!.indexOf("1000 B")).toBeGreaterThanOrEqual(0);
		// 只有能力端点被调用——没有任何 ingestion 创建/状态请求
		expect(fetchLog.filter((c) => c.url.indexOf("/api/ingestions") >= 0))
			.toHaveLength(0);
		expect(fetchLog.map((c) => c.url)).toEqual(
			["/api/tools/slides/upload-capability"]);
	});

	it("同一产物、服务端上限 900000000：照常 POST /api/ingestions（declared_size=5000）——上限值完全由服务端决定", async () => {
		const { ctl, fetchLog } = await setup(900000000);
		await ctl.setResultJob("job-1");
		await ctl.startOrContinue("job-1");
		const create = fetchLog.find((c) => c.method === "POST"
			&& c.url === "/api/ingestions");
		expect(create).toBeTruthy();
		expect((create!.body as Record<string, unknown>).declared_size).toBe(5000);
	});
});
