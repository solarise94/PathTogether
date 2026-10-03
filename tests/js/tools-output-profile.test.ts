/**
 * 明场 RGB OME-BigTIFF 输出 profile（bf-ome）的页面侧约定：
 *
 *  - 新建明场任务默认 bf-ome，荧光 fl-ome；没有 outputProfile 字段的旧任务
 *    记录一律按旧布局（明场 classic）对待——部分写出的经典产物绝不会被当成
 *    OME 续写；
 *  - 文件名按核心 result.format（其次 outputProfile）决定：两种 OME 都是
 *    `.ome.tif`，经典金字塔是 `.tif`；保存对话框过滤器与之匹配；
 *  - 上传（真实 tools-slides-upload.js + 真实共享引擎 cos-uploader.js）：
 *    bf-ome 产物以 `.ome.tif` 名创建 ingestion；新格式在 viewable_formats
 *    里才放行，不在则禁用上传（本地保存不受影响）。
 */
import { afterEach, describe, expect, it, vi } from "vitest";
import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
// eslint-disable-next-line
import * as E from "../../static/tools/slide-transform/engine.js";
// eslint-disable-next-line
import { createUploadController } from "../../static/tools/tools-slides-upload.js";

const FMT_CLASSIC = "classic-bigtiff-jpeg-pyramid";
const FMT_BF_OME = "ome-bigtiff-subifd-rgb-jpeg-pyramid";
const FMT_FL = "ome-bigtiff-subifd-multichannel-jpeg-passthrough";

describe("output profile helpers (engine.js)", () => {
	it("new jobs: brightfield → bf-ome, fluorescence → fl-ome", () => {
		expect(E.defaultOutputProfile("brightfield")).toBe("bf-ome");
		expect(E.defaultOutputProfile("fluorescence")).toBe("fl-ome");
	});

	it("records without outputProfile predate profiles → their original layout", () => {
		expect(E.recordOutputProfile({ modality: "brightfield", state: "paused" })).toBe("bf-classic");
		expect(E.recordOutputProfile({ modality: "fluorescence" })).toBe("fl-ome");
		expect(E.recordOutputProfile({ modality: "brightfield", outputProfile: "bf-ome" })).toBe("bf-ome");
		expect(E.recordOutputProfile({ modality: "brightfield", outputProfile: "bf-classic" })).toBe("bf-classic");
	});

	it("profile/modality fit", () => {
		expect(E.profileFitsModality("bf-ome", "brightfield")).toBe(true);
		expect(E.profileFitsModality("bf-classic", "brightfield")).toBe(true);
		expect(E.profileFitsModality("fl-ome", "brightfield")).toBe(false);
		expect(E.profileFitsModality("bf-ome", "fluorescence")).toBe(false);
		expect(E.profileFitsModality("fl-ome", "fluorescence")).toBe(true);
	});

	it("file names follow the core format, then the recorded profile", () => {
		expect(E.outputFileName("a.kfb", { result: { format: FMT_BF_OME } })).toBe("a.ome.tif");
		expect(E.outputFileName("a.kfb", { result: { format: FMT_CLASSIC } })).toBe("a.tif");
		expect(E.outputFileName("b.KFBF", { result: { format: FMT_FL } })).toBe("b.ome.tif");
		// a legacy ready job: the format decides even if a stale field says otherwise
		expect(E.outputFileName("a.kfb",
			{ outputProfile: "bf-ome", result: { format: FMT_CLASSIC } })).toBe("a.tif");
		// no result yet: recorded profile; no profile at all: legacy default
		expect(E.outputFileName("a.kfb", { modality: "brightfield", outputProfile: "bf-ome" })).toBe("a.ome.tif");
		expect(E.outputFileName("a.kfb", { modality: "brightfield" })).toBe("a.tif");
		expect(E.outputFileName("", { result: { format: FMT_BF_OME } })).toBe("slide.ome.tif");
	});

	it("save dialog filter matches the suggested name", () => {
		const ome = E.saveFileTypes({ result: { format: FMT_BF_OME } });
		expect(ome[0].description).toBe("OME-TIFF");
		expect(ome[0].accept["image/tiff"]).toContain(".tif");
		const classic = E.saveFileTypes({ result: { format: FMT_CLASSIC } });
		expect(classic[0].description).toBe("TIFF");
		// every suggested name ends with an accepted extension
		for (const fmt of [FMT_CLASSIC, FMT_BF_OME, FMT_FL]) {
			const job = { result: { format: fmt } };
			const name = E.outputFileName("x.kfb", job);
			const exts = E.saveFileTypes(job)[0].accept["image/tiff"];
			expect(exts.some((x: string) => name.endsWith(x))).toBe(true);
		}
	});

	it("magicModality: KFB → brightfield, KFBF → fluorescence, else null", () => {
		const kfb = [0xf1, 0x01, 0xee, 0xee, 0x4b, 0x46, 0x42, 0x00];
		const kfbf = [0xf1, 0x01, 0xee, 0xee, 0x4b, 0x46, 0x42, 0x46];
		expect(E.magicModality(kfb)).toBe("brightfield");
		expect(E.magicModality(kfbf)).toBe("fluorescence");
		// short/garbage heads never claim a modality (page then passes no profile)
		expect(E.magicModality([0xf1, 0x01])).toBeNull();
		expect(E.magicModality([0, 0, 0, 0, 0, 0, 0, 0])).toBeNull();
		// the choice the page offers only fits the sniffed brightfield input:
		// for fluorescence neither option fits, so the page must pass nothing
		expect(E.profileFitsModality("bf-ome", "brightfield")).toBe(true);
		expect(E.profileFitsModality("bf-classic", "brightfield")).toBe(true);
		expect(E.profileFitsModality("bf-ome", "fluorescence")).toBe(false);
		expect(E.profileFitsModality("bf-classic", "fluorescence")).toBe(false);
	});
});

// ---- upload controller harness (same shape as tools-upload-limit.test.ts) --

const here = dirname(fileURLToPath(import.meta.url));
const cosEngineSrc = readFileSync(
	resolve(here, "../../static/upload/cos-uploader.js"), "utf8");

function mkEl(id?: string) {
	return {
		id: id || "", disabled: false, hidden: false, textContent: "", title: "",
		className: "",
		appendChild(c: unknown) { return c; },
		insertBefore(c: unknown) { return c; },
		addEventListener() {}, removeEventListener() {}, click() {},
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

function resp(body: unknown, status = 200) {
	return {
		ok: status >= 200 && status < 300, status,
		json: () => Promise.resolve(body),
		headers: { get: () => null },
	} as unknown as Response;
}

type FetchLog = { method: string; url: string; body?: unknown };

async function setup(job: Record<string, unknown>, viewable: string[]) {
	const doc = mkDoc();
	const store = new Map<string, string>();
	const messages: string[] = [];
	const fetchLog: FetchLog[] = [];
	const fetchImpl = vi.fn((url: string, opts?: RequestInit) => {
		const method = String((opts && opts.method) || "GET");
		let body: unknown = undefined;
		try { body = opts && opts.body ? JSON.parse(String(opts.body)) : undefined; } catch { /* */ }
		fetchLog.push({ method, url: String(url), body });
		if (String(url) === "/api/tools/slides/upload-capability") {
			return Promise.resolve(resp({
				cos_upload: {
					available: true, manual_only: true, max_size_bytes: 900000000,
					part_bytes: 8, url_ttl_seconds: 600, max_concurrent_parts: 2,
					sign_batch_max_parts: 4, policy_version: "v1-manual", formats: ["tif", "tiff"],
				},
				viewable_formats: viewable, account: "u1", account_label: "u1@x",
			}));
		}
		if (method === "POST" && String(url) === "/api/ingestions") {
			return Promise.resolve(resp({ job_id: "inj_t1", state: "preparing", stage: "uploading" }, 202));
		}
		if (String(url).startsWith("/api/ingestions/")) {
			return Promise.resolve(resp({ state: "failed", stage: "terminal", fail_code: "demo" }));
		}
		return Promise.resolve(resp({}));
	}) as unknown as typeof fetch;
	const w: Record<string, unknown> = { HP_COS_UPLOAD: null };
	new Function("window", "document", "fetch", "location", cosEngineSrc)(
		w, doc, fetchImpl, { href: "http://local/tools/slides", origin: "http://local" });
	vi.stubGlobal("window", w);
	vi.stubGlobal("document", doc);
	vi.stubGlobal("fetch", fetchImpl);
	vi.stubGlobal("localStorage", {
		getItem: (k: string) => (store.has(k) ? store.get(k)! : null),
		setItem: (k: string, v: string) => { store.set(k, String(v)); },
		removeItem: (k: string) => { store.delete(k); },
		clear() { store.clear(); },
	});
	vi.stubGlobal("navigator", {
		locks: { request: (_n: string, _o: unknown, fn: (l: unknown) => Promise<unknown>) =>
			Promise.resolve(fn({ name: "lock" })) },
	});
	const artifact = { size: 5000, slice: (s: number, e: number) => ({ s, e }) };
	const runner = {
		async getJob(id: string) { return { id, state: "ready", ...job }; },
		async setJobUpload() { return undefined; },
		async setJobIntent() { return undefined; },
		async artifactView() { return artifact; },
	};
	const ctl = createUploadController({
		runner: runner as never,
		t: (k: string) => { messages.push(k); return k; },
		onJobsRefresh: null,
		onPublished: null,
	}) as unknown as {
		startOrContinue: (id: string) => Promise<unknown>;
		setResultJob: (id: string) => Promise<unknown>;
		isDisabled: (id: string) => boolean;
	};
	return { ctl, doc, fetchLog, messages };
}

afterEach(() => {
	vi.unstubAllGlobals();
});

function createdName(fetchLog: FetchLog[]) {
	const create = fetchLog.find((c) => c.method === "POST" && c.url === "/api/ingestions");
	expect(create).toBeTruthy();
	const body = create!.body as Record<string, unknown>;
	return String(body.filename || body.name || "");
}

describe("upload of bf-ome artifacts", () => {
	it("bf-ome artifact uploads as <base>.ome.tif", async () => {
		const { ctl, fetchLog } = await setup({
			modality: "brightfield", outputProfile: "bf-ome",
			source: { name: "case-x.kfb", size: 1 },
			result: { outputBytes: 5000, format: FMT_BF_OME },
		}, [FMT_CLASSIC, FMT_BF_OME, FMT_FL]);
		await ctl.setResultJob("job-1");
		await ctl.startOrContinue("job-1");
		expect(createdName(fetchLog)).toBe("case-x.ome.tif");
	});

	it("legacy classic artifact keeps <base>.tif", async () => {
		const { ctl, fetchLog } = await setup({
			modality: "brightfield",
			source: { name: "case-y.kfb", size: 1 },
			result: { outputBytes: 5000, format: FMT_CLASSIC },
		}, [FMT_CLASSIC, FMT_BF_OME, FMT_FL]);
		await ctl.setResultJob("job-2");
		await ctl.startOrContinue("job-2");
		expect(createdName(fetchLog)).toBe("case-y.tif");
	});

	it("a server that cannot view bf-ome disables upload (no ingestion), save stays", async () => {
		const { ctl, doc, fetchLog, messages } = await setup({
			modality: "brightfield", outputProfile: "bf-ome",
			source: { name: "case-z.kfb", size: 1 },
			result: { outputBytes: 5000, format: FMT_BF_OME },
		}, [FMT_CLASSIC, FMT_FL]);
		await ctl.setResultJob("job-3");
		await ctl.startOrContinue("job-3");
		expect(ctl.isDisabled("job-3")).toBe(true);
		expect(messages).toContain("tools.upload.format.unsupported");
		expect(doc.getElementById("save-btn").disabled).toBe(false);
		expect(fetchLog.filter((c) => c.url.indexOf("/api/ingestions") >= 0)).toHaveLength(0);
	});
});

// ---- page wiring (source-level; tools-slides.js is a live-DOM module) ----

const pageSrc = readFileSync(resolve(here, "../../static/tools/tools-slides.js"), "utf8");
const runnerSrc = readFileSync(resolve(here, "../../static/tools/slide-transform/runner.js"), "utf8");
const shellSrc = readFileSync(resolve(here, "../../templates/tools_slides.html"), "utf8");
const i18nSrc = readFileSync(resolve(here, "../../static/i18n.js"), "utf8");

describe("tool page output-format choice (wiring)", () => {
	it("template offers the brightfield choice, bf-ome default-checked", () => {
		expect(shellSrc).toContain('id="format-section"');
		expect(shellSrc).toContain('id="format-ome" name="outputFormat" value="bf-ome" checked');
		expect(shellSrc).toContain('id="format-classic" name="outputFormat" value="bf-classic"');
		expect(shellSrc).toContain('data-i18n="tools.format.ome"');
		expect(shellSrc).toContain('data-i18n="tools.format.classic"');
	});

	it("page passes the selection at prepare and at both start paths", () => {
		// prepare: only a sniffed brightfield input carries the UI selection
		// (fluorescence → undefined → runner defaults to fl-ome)
		expect(pageSrc)
			.toContain("sniffedModality === 'brightfield' ? selectedOutputProfile() : undefined");
		// fresh start (「仅转换」and R1「转换并上传到工作台」share driveConversion)
		expect(pageSrc).toMatch(/startJob\(page\.file, \{[\s\S]*?outputProfile,/);
		// job-list start passes the UI selection only when the visible format
		// section belongs to THIS job — after a reload (page.prep null, section
		// hidden) nothing is passed and the runner uses record.outputProfile,
		// so the template-default radio can never override a saved choice
		// (U2: thisPrep is exactly that guard, shared by format and quality)
		expect(pageSrc).toMatch(/startJob\(null, \{[\s\S]*?outputProfile: startOutputProfile,/);
		expect(pageSrc).toContain("const thisPrep = page.prep && page.prep.jobId === job.id;");
		expect(pageSrc).toContain("(action === 'start' && thisPrep && !els.formatSection.hidden)");
		// resume passes no settings at all → the record's profile is kept
		expect(pageSrc).toContain("await page.runner.resumeJob(job.id)");
		expect(pageSrc).not.toMatch(/resumeJob\(job\.id, \{[\s\S]*?outputProfile/);
	});

	it("a radio change after prepare is persisted into the prepared record", () => {
		expect(pageSrc).toContain("setPreparedOutputProfile(page.prep.jobId, ev.target.value)");
		// the handler is armed on the radio group and skips locked/hidden state
		expect(pageSrc).toMatch(/input\[name="outputFormat"\][\s\S]*?onOutputFormatChange/);
		expect(pageSrc).toMatch(/onOutputFormatChange\(ev\)[\s\S]*?page\.outputLockedProfile \|\| els\.formatSection\.hidden/);
		// lock shows the job's actual format: explicit choice, else the record
		expect(pageSrc).toContain("startOutputProfile || job.outputProfile");
	});

	it("runner setPreparedOutputProfile: prepared-only and modality-checked", () => {
		expect(runnerSrc).toContain("async setPreparedOutputProfile(jobId, profile)");
		// only a prepared record accepts the change (typed refusal otherwise)…
		expect(runnerSrc).toMatch(/setPreparedOutputProfile\(jobId, profile\)[\s\S]*?rec\.state !== 'prepared'/);
		// …and the value must fit the probed modality
		expect(runnerSrc).toMatch(/setPreparedOutputProfile\(jobId, profile\)[\s\S]*?checkedOutputProfile\(profile, rec\.modality\)/);
	});

	it("a started job locks the choice and shows its actual format", () => {
		expect(pageSrc)
			.toContain("lockOutputProfile(outputProfile || E.defaultOutputProfile(probeDoc().modality))");
		expect(pageSrc).toContain("els.formatFieldset.disabled = !!locked;");
		expect(pageSrc).toContain("t(`tools.result.format.${locked}`)");
		// the job list row names the job's output format
		expect(pageSrc).toContain("t(`tools.jobs.format.${job.outputProfile}`)");
	});

	it("i18n carries the choice labels in zh and en (output target by software, U2)", () => {
		expect(i18nSrc).toContain('"tools.format.ome": "适合 QuPath（OME-TIFF，默认）"');
		expect(i18nSrc).toContain('"tools.format.classic": "兼容 OpenSlide 工具（经典 TIFF）"');
		expect(i18nSrc).toContain('"tools.format.ome": "For QuPath (OME-TIFF, default)"');
		expect(i18nSrc).toContain('"tools.format.classic": "OpenSlide-compatible tools (classic TIFF)"');
	});
});
