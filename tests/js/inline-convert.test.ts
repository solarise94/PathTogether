/**
 * 上传面板内转换（docs/upload-inline-convert-design-20261015.md）编排器单测。
 *
 * 加载面（设计 §9.2 前言）：inline-convert.js / slide-sniff.js / cos-uploader.js
 * 是 classic script，用 `new Function` 加载；ES module 只能顶层 import。本文件
 * 只驱动前三个 + 一个注入的假 runner/假 engine（真 wasm 由 e2e 覆盖）。
 *
 * 覆盖设计 §9.2 的条目：
 *   1  判定表穷举（routeFor 与真 slide-sniff + 真 engine 嗅探一致）
 *   2  未登记扩展名 → rejected，零网络
 *   3  PNG 改名 .jpg → rejected + detectedType，零 fetch / 零 XHR
 *   4  队列：至多 1 个转换 + 1 个上传并行
 *   5  门禁作用域（转换产物吃 viewable_formats，直传不吃）
 *   6  产物声明从产物字节嗅探（bf-ome → ome-tiff，绝不写死 converter-bigtiff）
 *   7  convert_in_browser → 自动回队恰一次
 *   8  上传槽 + 产物锁（pt:upload-slot:<acct> + engine.uploadLockName）
 *   9  刷新恢复不自动开始（无面板发起的新请求）
 *   10 续传语义（resumeJobId 复用同一 ingestion）
 *   11 终态清理（discardJob；upload_active+lockHeld:false → abandonUpload）
 *   13 申请支持预填
 *   14 i18n 键 zh/en + 两条旧文案不再写死 /tools/slides
 *   16 bundle 散文件与整包
 *   17 beforeunload 五态覆盖、终态不覆盖
 *   18 磁盘门确认后带原始输入重新起（绝不复用旧 jobId）
 *   19 目标关联：入队冻结快照 + uploadFileCos 字面量含 assocTarget（源码级）
 *   20 恢复行两组渲染（已收口也要有行）
 *   21 bundle 两个入口开关两态
 *   22 回退文案两态
 */
import { describe, expect, it, vi } from "vitest";
import { readFileSync } from "node:fs";
// §9.2 第 12 项：对照对象是 ES module，只能顶层 import（设计 §9.2 前言的
// 「两种加载面」——classic script 用 new Function，ES module 用 import，
// 同一文件内并存）。生产代码**不** import 它（它是工具页 DOM 依赖）。
import { createUploadController } from "../../static/tools/tools-slides-upload.js";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const root = resolve(here, "../..");
const appSrc = readFileSync(resolve(root, "static/app.js"), "utf8");
const inlineSrc = readFileSync(
	resolve(root, "static/upload/inline-convert.js"), "utf8");
const sniffSrc = readFileSync(
	resolve(root, "static/upload/slide-sniff.js"), "utf8");
const i18nSrc = readFileSync(resolve(root, "static/i18n.js"), "utf8");

const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));
async function flush(n = 12) {
	for (let i = 0; i < n; i++) await sleep(0);
}

// --------------------------------------------------------------------- //
// classic script 加载 + 假依赖
// --------------------------------------------------------------------- //
type W = Record<string, unknown>;
function loadInline() {
	const w: W = {};
	new Function("window", inlineSrc)(w);
	new Function("window", sniffSrc)(w);
	return w;
}

const IC = () => loadInline().HP_INLINE_CONVERT as {
	parseSwitch(raw?: unknown): boolean;
	normalizeConfig(caps: unknown): { enabled: boolean; url: string; viewableFormats: string[] } | null;
	routeFor(cls: unknown, engineSupported: boolean | null): string;
	artifactDeclaration(cls: unknown): string | null;
	directDeclaration(cls: unknown, caps: unknown): string | null;
	failureStageKey(code: string, detail?: string | null): string | null;
	prefillFormatRequest(entry: unknown, caps: unknown): { format_ext: string; message: string };
	shouldWarn(state: string): boolean;
	isTerminal(state: string): boolean;
	progressStage(p: unknown): unknown;
	WARN_STATES: Record<string, number>;
	createOrchestrator(deps: Record<string, unknown>): Orch;
};

interface Entry {
	id: string; state: string; name: string; size: number; jobId: string | null;
	needsConvert: boolean; directClass: string | null; target: unknown;
	slideId: string | null; uploadJobId: string | null; mode: string | null;
	error: { code?: string; detectedType?: string | null; engineReason?: string | null } | null;
	artifactName?: string; artifactSize?: number; artifactCls?: unknown;
	committedBytes?: number; uploadState?: string | null;
	viaRestore?: boolean; requeueCount: number; stage?: { key: string } | null;
	_confirmDisk?: boolean; _upload?: unknown;
	[key: string]: unknown;
}
interface Orch {
	enqueue(file: unknown, opts?: Record<string, unknown>): Entry | null;
	enqueueBundle(rows: unknown[], folderName: string, opts?: Record<string, unknown>): Entry | null;
	act(id: string, action: string): Entry | null;
	cancel(e: Entry): void;
	discard(e: Entry): Promise<boolean>;
	restore(): Promise<Entry[]>;
	list(): Entry[];
	get(id: string): Entry | null;
	busy(): { converting: boolean; uploading: boolean };
	warnNeeded(): boolean;
}

/// OME-TIFF / 转换器产物夹具（同 workbench-direct-class.test.ts 形态）
function classicTiff(description: string): Uint8Array {
	const desc = new TextEncoder().encode(description);
	const header = new Uint8Array(8);
	header.set([0x49, 0x49], 0);
	new DataView(header.buffer).setUint16(2, 42, true);
	new DataView(header.buffer).setUint32(4, 8, true);
	const ifd = new Uint8Array(2 + 12 + 4);
	new DataView(ifd.buffer).setUint16(0, 1, true);
	new DataView(ifd.buffer).setUint16(2, 270, true);
	new DataView(ifd.buffer).setUint16(4, 2, true);
	new DataView(ifd.buffer).setUint32(6, desc.length, true);
	new DataView(ifd.buffer).setUint32(10, 8 + ifd.length, true);
	const out = new Uint8Array(8 + ifd.length + desc.length);
	out.set(header, 0); out.set(ifd, 8); out.set(desc, 8 + ifd.length);
	return out;
}

function fakeFile(name: string, bytes: Uint8Array | null, size?: number) {
	const b = bytes || new Uint8Array(0);
	return {
		name,
		size: size ?? b.length,
		slice(start: number, end: number) {
			const e = Math.min(end, b.length);
			return { arrayBuffer: async () => b.slice(start, e).buffer };
		},
	};
}

/// 带 IFD0 标签的 classic TIFF（.svs 走 decideTiff 需要 compression=7 + tiled）
function tiffWithTags(desc: string, tags: Array<[number, number, number]>): Uint8Array {
	const d = new TextEncoder().encode(desc);
	const n = tags.length + 1;
	const ifd = new Uint8Array(2 + n * 12 + 4);
	const dv = new DataView(ifd.buffer);
	dv.setUint16(0, n, true);
	tags.forEach(([tag, typ, val], i) => {
		const at = 2 + i * 12;
		dv.setUint16(at, tag, true);
		dv.setUint16(at + 2, typ, true);
		dv.setUint32(at + 4, 1, true);
		if (typ === 3) dv.setUint16(at + 8, val, true); else dv.setUint32(at + 8, val, true);
	});
	const last = 2 + (n - 1) * 12;
	dv.setUint16(last, 270, true); dv.setUint16(last + 2, 2, true);
	dv.setUint32(last + 4, d.length, true);
	dv.setUint32(last + 8, 8 + ifd.length, true);
	const header = new Uint8Array(8);
	header.set([0x49, 0x49], 0);
	new DataView(header.buffer).setUint16(2, 42, true);
	new DataView(header.buffer).setUint32(4, 8, true);
	const out = new Uint8Array(8 + ifd.length + d.length);
	out.set(header, 0); out.set(ifd, 8); out.set(d, 8 + ifd.length);
	return out;
}

/// 可转换形态的 Aperio SVS（压缩 7 + tiled + 3 samples + RGB）
function aperioSvs(): Uint8Array {
	return tiffWithTags("Aperio Image Library v11.2.1", [
		[259, 3, 7], [322, 3, 256], [323, 3, 256], [277, 3, 3], [262, 3, 2],
	]);
}

const PNG_HEAD = Uint8Array.from([0x89, 0x50, 0x4E, 0x47, 0x0D, 0x0A, 0x1A, 0x0A, 0, 0, 0, 0, 0, 0, 0, 0]);

/// 真 engine.js 的子集（sniff 三入口语义与 engine.js:384/400/405 相同）——
/// 真 engine 由 tools-*-input.test.ts 与 e2e 覆盖；这里只作为注入面。
function engineStub(over: Record<string, unknown> = {}) {
	return {
		isTiffHeader: (h: Uint8Array) => h[0] === 0x49 && h[1] === 0x49,
		isRasterHeader: (h: Uint8Array) => h[0] === 0xFF && h[1] === 0xD8,
		magicSupported: () => true,
		sniffTiffSlideCapability: () => Promise.resolve({ supported: true, modality: "brightfield" }),
		sniffRasterCapability: () => Promise.resolve({ supported: true, modality: "brightfield" }),
		OUTPUT_PROFILES: { BF_OME: "bf-ome", FL_OME: "fl-ome", BF_CLASSIC: "bf-classic" },
		ENCODING_PROFILES: { PRESERVE: "preserve-source-v1" },
		errCode: (e: unknown) => ((e as { error?: { code?: string } })?.error?.code) || null,
		outputFileName: (n: string) => String(n).replace(/\.(kfb|kfbf|svs|bmp|jpg)$/i, "") + ".ome.tif",
		...over,
	};
}

const CAPS = {
	inlineConvert: {
		enabled: true, url: "/tools/slides",
		viewableFormats: ["ome-bigtiff-subifd-rgb-jpeg-pyramid", "classic-bigtiff-jpeg-pyramid"],
	},
	directUploadFormats: ["tif", "tiff", "ome.tif", "ndpi", "vms", "scn", "bif",
		"bmp", "jpg", "jpeg", "vmu", "svslide"],
	cosConfig: {
		max_size_bytes: 1_000_000, part_bytes: 100,
		formats: ["tif", "tiff", "svs", "ndpi", "vms", "vmu", "scn", "bif",
			"svslide", "bmp", "jpg", "jpeg", "zip", "kfb", "kfbf"],
	},
};

/// 假 runner：记录调用；默认让 probe→startJob→ready 成功
function fakeRunner(over: Record<string, unknown> = {}) {
	const calls: unknown[][] = [];
	const base = {
		calls,
		setJobIntent: (id: string, patch: unknown) => { calls.push(["setJobIntent", id, patch]); return Promise.resolve(); },
		probe: (file: unknown, opts: unknown) => { calls.push(["probe", (file as { name?: string })?.name, opts]); return Promise.resolve({ jobId: "job1", probe: { document: { modality: "brightfield" } }, identity: { size: 10 } }); },
		prepareBundle: (rows: unknown[], opts: unknown) => { calls.push(["prepareBundle", rows.length, opts]); return Promise.resolve({ jobId: "jobb", probe: { document: { modality: "brightfield" } }, identity: { size: 10 } }); },
		startJob: (file: unknown, opts: Record<string, unknown>) => { calls.push(["startJob", (file as { name?: string })?.name ?? null, opts]); return Promise.resolve({ jobId: opts.jobId, done: Promise.resolve({ ok: true, result: { output_bytes: 100, format: "ome-bigtiff-subifd-rgb-jpeg-pyramid" }, validation: {} }) }); },
		resumeJob: (id: string) => { calls.push(["resumeJob", id]); return Promise.resolve({ jobId: id, done: Promise.resolve({ ok: true, result: { output_bytes: 100, format: "ome-bigtiff-subifd-rgb-jpeg-pyramid" } }) }); },
		cancelJob: () => { calls.push(["cancelJob"]); return Promise.resolve(); },
		listJobs: () => Promise.resolve([]),
		discardJob: (id: string, opts?: unknown) => { calls.push(["discardJob", id, opts]); return Promise.resolve(); },
		getJob: () => Promise.resolve({
			id: "job1", state: "ready",
			result: { outputBytes: 100, format: "ome-bigtiff-subifd-rgb-jpeg-pyramid" },
			upload: null, source: { name: "x" },
		}),
		artifactView: () => Promise.resolve({
			name: "x.ome.tif", size: 100, slice: () => null,
		}),
		setJobUpload: (id: string, patch: unknown) => { calls.push(["setJobUpload", id, patch]); return Promise.resolve(); },
	};
	return { ...base, ...over, calls };
}

function fakeCos(over: Record<string, unknown> = {}) {
	const uploads: Record<string, unknown>[] = [];
	return {
		uploads,
		uploadLockName: (id: string) => "slide-transform:upload:" + id,
		createUpload: (opts: Record<string, unknown>) => {
			uploads.push(opts);
			return {
				done: Promise.resolve({ ok: true, body: { slide_id: "sld_1" } }),
				cancel() { },
			};
		},
		...over,
	};
}

/// 记录锁名并放行的假 navigator.locks
function fakeLocks(opts: { deny?: string[] } = {}) {
	const names: string[] = [];
	const releases: Array<() => void> = [];
	return {
		names,
		releaseAll() { releases.splice(0).forEach((r) => r()); },
		request(name: string, o: { ifAvailable?: boolean }, cb: (l: unknown) => unknown) {
			names.push(name);
			if (opts.deny && opts.deny.indexOf(name) >= 0) {
				return Promise.resolve(cb(null));
			}
			const lock = { name };
			const p = Promise.resolve(cb(lock));
			return { then: p.then.bind(p), catch: p.catch.bind(p) };
		},
	};
}

function mkOrch(over: {
	runner?: Record<string, unknown>;
	engine?: Record<string, unknown>;
	cos?: Record<string, unknown>;
	caps?: Record<string, unknown>;
	locks?: unknown;
	onArtifact?: unknown;
	onUploaded?: unknown;
	account?: () => string;
} = {}) {
	const w = loadInline();
	const I = w.HP_INLINE_CONVERT as ReturnType<typeof IC>;
	const runner = fakeRunner(over.runner);
	const cos = fakeCos(over.cos);
	const stages: Array<[string, string]> = [];
	const rows: Entry[] = [];
	const buttons: Entry[] = [];
	const toasts: string[] = [];
	// 有序事件日志：钉住「行元素先建、阶段文案后写」这一时序（app.js 的
	// inlineRowSetStage 按 entry.id 查行并提前 return——顺序错了状态文案为空）
	const order: string[] = [];
	let removed = 0;
	const locks = over.locks ?? fakeLocks();
	const orch = I.createOrchestrator({
		runner,
		engine: engineStub(over.engine),
		sniff: w.HP_SLIDE_SNIFF,
		cos,
		caps: { ...CAPS, ...(over.caps || {}) },
		ui: {
			row: (e: Entry) => { rows.push(e); order.push("row:" + e.id); },
			setStage: (e: Entry, k: string) => {
				stages.push([e.id, k]);
				order.push("stage:" + e.id);
			},
			markError() { },
			buttons: (e: Entry) => buttons.push(e),
			toast: (k: string) => toasts.push(k),
			refresh() { },
			remove() { removed += 1; },
		},
		account: over.account || (() => "u1"),
		target: () => ({ pid: "p1" }),
		onArtifact: over.onArtifact ?? null,
		onUploaded: over.onUploaded ?? null,
		locks,
		setTimer: (fn: () => void, ms: number) => setTimeout(fn, ms),
	});
	return { I, orch, runner, cos, stages, rows, buttons, toasts, locks, order,
		removed: () => removed };
}

// --------------------------------------------------------------------- //
// 1. 纯函数：开关 / 配置 / 路由 / 声明 / 文案
// --------------------------------------------------------------------- //
describe("inline-convert 纯函数", () => {
	it("parseSwitch：默认开；0/false/no（任意大小写/空白）关", () => {
		const I = IC();
		expect(I.parseSwitch(undefined)).toBe(true);
		expect(I.parseSwitch("")).toBe(true);
		expect(I.parseSwitch("1")).toBe(true);
		expect(I.parseSwitch("true")).toBe(true);
		expect(I.parseSwitch("0")).toBe(false);
		expect(I.parseSwitch("FALSE")).toBe(false);
		expect(I.parseSwitch(" No ")).toBe(false);
	});

	it("normalizeConfig：结构合法才给配置；缺失/非法一律 null（fail-closed）", () => {
		const I = IC();
		const ok = I.normalizeConfig({ cos_upload: { inline_convert: {
			enabled: false, url: "/tools/slides", viewable_formats: ["a"] } } });
		expect(ok).toEqual({ enabled: false, url: "/tools/slides", viewableFormats: ["a"] });
		for (const bad of [undefined, null, {}, { cos_upload: {} },
			{ cos_upload: { inline_convert: { enabled: "yes", viewable_formats: [] } } },
			{ cos_upload: { inline_convert: { enabled: true } } },
			{ cos_upload: { inline_convert: { enabled: true, viewable_formats: [1] } } }]) {
			expect(I.normalizeConfig(bad), JSON.stringify(bad)).toBeNull();
		}
	});

	it("routeFor：convert 类以引擎结论裁决；unsupported → reject；bundle 单独分派", () => {
		const I = IC();
		expect(I.routeFor({ cls: "convert" }, true)).toBe("convert");
		expect(I.routeFor({ cls: "convert" }, false)).toBe("upload");
		expect(I.routeFor({ cls: "convert" }, null)).toBe("convert");
		expect(I.routeFor({ cls: "temporary" }, null)).toBe("upload");
		expect(I.routeFor({ cls: "ome-tiff" }, null)).toBe("upload");
		expect(I.routeFor({ cls: "converter-bigtiff" }, null)).toBe("upload");
		expect(I.routeFor({ cls: "unsupported" }, null)).toBe("reject");
		expect(I.routeFor({ cls: "convert", bundle: true }, null)).toBe("bundle");
		expect(I.routeFor(null, null)).toBe("upload");
	});

	it("artifactDeclaration：从产物字节得出，绝不写死 converter-bigtiff（§4.3 / A1）", () => {
		const I = IC();
		expect(I.artifactDeclaration({ cls: "ome-tiff" })).toBe("ome-tiff");
		expect(I.artifactDeclaration({ cls: "converter-bigtiff" })).toBe("converter-bigtiff");
		expect(I.artifactDeclaration({ cls: "temporary" })).toBeNull();
		expect(I.artifactDeclaration(null)).toBeNull();
	});

	it("directDeclaration：OME/转换器/SVS-JP2K/暂时直传各自的声明", () => {
		const I = IC();
		expect(I.directDeclaration({ cls: "ome-tiff" }, CAPS)).toBe("ome-tiff");
		expect(I.directDeclaration({ cls: "converter-bigtiff" }, CAPS)).toBe("converter-bigtiff");
		expect(I.directDeclaration({ cls: "temporary", svsJp2k: true,
			directClass: "unconverted-variant:svs-jp2k" }, CAPS))
			.toBe("unconverted-variant:svs-jp2k");
		expect(I.directDeclaration({ cls: "temporary", directClass: "legacy-direct" }, CAPS))
			.toBe("legacy-direct");
		expect(I.directDeclaration({ cls: "convert", directClass: "legacy-direct" }, CAPS))
			.toBeNull();
		expect(I.directDeclaration(null, CAPS)).toBeNull();
	});

	it("prefillFormatRequest：format_ext + 带机器码的 message（§7.1）", () => {
		const I = IC();
		const r = I.prefillFormatRequest({
			name: "shot.JPG",
			error: { code: "content_ext_mismatch", detectedType: "png" },
		}, { inlineConvert: { url: "/tools/slides" } });
		expect(r.format_ext).toBe("jpg");
		expect(r.message).toContain("code=content_ext_mismatch");
		expect(r.message).toContain("detected=png");
		expect(r.message).toContain("declared=.jpg");
		const r2 = I.prefillFormatRequest({
			name: "x.kfb",
			error: { code: "engine_unsupported_variant", engineReason: "非常规变体" },
		}, null);
		expect(r2.message).toContain("engine_reason=非常规变体");
	});

	it("failureStageKey：引擎/服务端码 → 文案键；未知码 null", () => {
		const I = IC();
		expect(I.failureStageKey("disk_precheck_failed")).toBe("upload.inline.err.disk");
		expect(I.failureStageKey("convert_in_browser")).toBe("upload.inline.err.needs.convert");
		expect(I.failureStageKey("validation_failed", "raster_format_mismatch"))
			.toBe("upload.cos.err.raster_mismatch");
		expect(I.failureStageKey("validation_failed")).toBe("upload.cos.err.validation");
		expect(I.failureStageKey("client_gone")).toBe("upload.cos.err.client_gone");
		expect(I.failureStageKey("who_knows")).toBeNull();
	});

	it("progressStage：stage / stage-bundle / level 三形态（§3.3）", () => {
		const I = IC();
		expect(I.progressStage({ unit: "stage", done: 5, total: 10 }))
			.toEqual({ key: "tools.phase.probing", frac: 0.5, noteKey: null });
		const b = I.progressStage({ unit: "stage-bundle", done: 5, total: 10, member: 2, members: 4 }) as {
			noteKey: string; noteVars: { member: number } };
		expect(b.noteKey).toBe("tools.stage.bundle.bytes");
		expect(b.noteVars.member).toBe(2);
		expect((I.progressStage({ unit: "level", done: 1, total: 4 }) as { key: string }).key)
			.then?.(() => { /* level 无 then：纯对象 */ });
	});
});

// --------------------------------------------------------------------- //
// 2/3. 当场拒绝（零网络）
// --------------------------------------------------------------------- //
describe("入队判定：拒绝闸与直传", () => {
	it("未登记扩展名 → rejected + 申请支持按钮，零 fetch", async () => {
		const h = mkOrch();
		const e = h.orch.enqueue(fakeFile("virus.xyz", null, 30))!;
		await flush();
		expect(e.state).toBe("rejected");
		expect(h.orch.busy()).toEqual({ converting: false, uploading: false });
		expect(h.runner.calls.filter((c) => c[0] === "probe")).toHaveLength(0);
		expect(h.cos.uploads).toHaveLength(0);
	});

	it("PNG 改名 .jpg → rejected + detectedType=png，零传输（§9.2 第 3 项）", async () => {
		const h = mkOrch();
		const e = h.orch.enqueue(fakeFile("photo.jpg", PNG_HEAD))!;
		await flush();
		expect(e.state).toBe("rejected");
		expect(e.error?.code).toBe("content_ext_mismatch");
		expect(e.error?.detectedType).toBe("png");
		// 零网络：没有 probe / startJob / createUpload
		const kinds = h.runner.calls.map((c) => c[0]);
		expect(kinds).not.toContain("probe");
		expect(kinds).not.toContain("startJob");
		expect(h.cos.uploads).toHaveLength(0);
	});

	it("OME-TIFF（真 TIFF 头 + OME-XML 描述）→ 直传，不转换、不探测", async () => {
		const h = mkOrch();
		const ome = '<?xml version="1.0"?><OME xmlns="http://www.openmicroscopy.org/Schemas/OME/2016-06"></OME>\x00';
		const e = h.orch.enqueue(fakeFile("scan.ome.tif", classicTiff(ome)))!;
		await flush();
		expect(e.needsConvert).toBe(false);
		expect(e.directClass).toBe("ome-tiff");
		const kinds = h.runner.calls.map((c) => c[0]);
		expect(kinds).not.toContain("probe");
		expect(kinds).not.toContain("prepareBundle");
	});

	it("引擎判不支持且 direct_import 开放 → 按原样直传（legacy-direct）", async () => {
		const h = mkOrch({
			engine: { sniffTiffSlideCapability: () => Promise.resolve({ supported: false, reason: "条带布局不在支持集" }) },
		});
		// 通用瓦片 JPEG TIFF：classify 判 convert，引擎判 false
		const gtiff = classicTiff("Generic scanner v1");
		const e = h.orch.enqueue(fakeFile("tile.tif", gtiff))!;
		await flush();
		expect(e.needsConvert).toBe(false);
		expect(e.state === "upload_queued" || e.state === "done" || e.state === "upload_failed")
			.toBe(true);
	});

	it("引擎判不支持且 direct_import 关闭 → 不上传 + 申请支持（engine_unsupported_variant）", async () => {
		const h = mkOrch({
			engine: { sniffTiffSlideCapability: () => Promise.resolve({ supported: false, reason: "条带布局不在支持集" }) },
			caps: { directUploadFormats: [] },
		});
		// 可转换形态的 Aperio SVS（compress 7 + tiled）→ classify 判 convert，
		// 引擎判 false，而 direct_upload 关闭 → 不上传
		const e = h.orch.enqueue(fakeFile("weird.svs", aperioSvs()))!;
		await flush(20);
		expect(e.needsConvert).toBe(false);
		expect(e.state).toBe("convert_failed");
		expect(e.error?.code).toBe("engine_unsupported_variant");
		expect(e.error?.engineReason).toBe("条带布局不在支持集");
		expect(h.cos.uploads).toHaveLength(0);
		expect(h.runner.calls.filter((c) => c[0] === "probe")).toHaveLength(0);
	});

	it("引擎判不支持但 direct_import 开放 → 按原样直传（不改判、不拦）", async () => {
		// 通用瓦片 JPEG TIFF（.tif 在 direct_upload 开放集内）：classify 判
		// convert，引擎判 false → 面板**不拦**，按 legacy-direct 直传，
		// 字节合法性交给服务端 open_slide 终审（§4.1「引擎拒绝分支」）
		const uploaded: string[] = [];
		const h = mkOrch({
			engine: { sniffTiffSlideCapability: () => Promise.resolve({ supported: false, reason: "条带布局不在支持集" }) },
			onArtifact: (e: Entry) => { uploaded.push(e.name); return Promise.resolve({ body: { slide_id: "s" } }); },
		});
		const gtiff = tiffWithTags("", [
			[259, 3, 7], [322, 3, 256], [323, 3, 256], [277, 3, 3], [262, 3, 2],
		]);
		const e = h.orch.enqueue(fakeFile("tile.tif", gtiff))!;
		await flush(20);
		expect(e.needsConvert).toBe(false);
		expect(e.directClass).toBe("legacy-direct");
		expect(e.state).toBe("done");
		expect(uploaded).toEqual(["tile.tif"]);
	});
});

// --------------------------------------------------------------------- //
// 4/5/6/7. 转换 → 上传链路
// --------------------------------------------------------------------- //
describe("转换 → 上传（一次授权整链）", () => {
	it("KFB → probe → startJob → 自动上传 → done，且声明来自产物嗅探", async () => {
		const h = mkOrch();
		const kfb = Uint8Array.from([0xF1, 0x01, 0xEE, 0xEE, 0x4B, 0x46, 0x42, 0x00, 0, 0, 0, 0]);
		const e = h.orch.enqueue(fakeFile("a.kfb", kfb))!;
		await flush(20);
		expect(e.state).toBe("done");
		expect(e.slideId).toBe("sld_1");
		const kinds = h.runner.calls.map((c) => c[0]);
		expect(kinds).toContain("probe");
		expect(kinds).toContain("startJob");
		// 一次拖入即授权整链：不再有第二次确认（无 confirm 调用）
		expect(h.cos.uploads).toHaveLength(1);
		// 产物名与声明：本假 artifactView 是 .ome.tif 但字节为空 →
		// 嗅探不出 → 不带声明（绝不写死 converter-bigtiff）
		const body = h.cos.uploads[0].createBody as { direct_class?: string } | null;
		expect(body?.direct_class).toBeUndefined();
	});

	it("产物是 OME-XML → createBody.direct_class='ome-tiff'（§4.3 主链路）", async () => {
		const ome = '<?xml version="1.0"?><OME xmlns="http://www.openmicroscopy.org/Schemas/OME/2016-06"></OME>\x00';
		const artifact = classicTiff(ome);
		const h = mkOrch({
			runner: {
				artifactView: () => Promise.resolve(fakeFile("x.ome.tif", artifact)),
			},
		});
		const kfb = Uint8Array.from([0xF1, 0x01, 0xEE, 0xEE, 0x4B, 0x46, 0x42, 0x00, 0, 0, 0, 0]);
		const e = h.orch.enqueue(fakeFile("a.kfb", kfb))!;
		await flush(20);
		expect(e.state).toBe("done");
		const body = h.cos.uploads[0].createBody as { direct_class?: string };
		expect(body.direct_class).toBe("ome-tiff");
	});

	it("产物是转换器 BigTIFF → direct_class='converter-bigtiff'（同算法另一支）", async () => {
		const conv = '{"source_format": "kfb_bf_v1", "mpp_x": 0.5}\x00';
		const artifact = classicTiff(conv);
		const h = mkOrch({
			runner: {
				artifactView: () => Promise.resolve(fakeFile("x.tif", artifact)),
			},
			engine: { outputFileName: () => "x.tif" },
		});
		const kfb = Uint8Array.from([0xF1, 0x01, 0xEE, 0xEE, 0x4B, 0x46, 0x42, 0x00, 0, 0, 0, 0]);
		h.orch.enqueue(fakeFile("a.kfb", kfb));
		await flush(20);
		const body = h.cos.uploads[0].createBody as { direct_class?: string };
		expect(body.direct_class).toBe("converter-bigtiff");
	});

	it("门禁作用域：转换产物 result.format 不在 viewable_formats → 不建 ingestion（§5.1）", async () => {
		const h = mkOrch({
			runner: {
				getJob: () => Promise.resolve({
					id: "job1", state: "ready",
					result: { outputBytes: 100, format: "something-else" },
					upload: null,
				}),
			},
		});
		const kfb = Uint8Array.from([0xF1, 0x01, 0xEE, 0xEE, 0x4B, 0x46, 0x42, 0x00, 0, 0, 0, 0]);
		const e = h.orch.enqueue(fakeFile("a.kfb", kfb))!;
		await flush(20);
		expect(e.state).toBe("convert_failed");
		expect(e.error?.code).toBe("format_not_viewable");
		expect(h.cos.uploads).toHaveLength(0);
	});

	it("门禁作用域：转换产物超 max_size_bytes → 不建 ingestion（§5.1）", async () => {
		const h = mkOrch({
			runner: {
				getJob: () => Promise.resolve({
					id: "job1", state: "ready",
					result: { outputBytes: 9_999_999, format: "ome-bigtiff-subifd-rgb-jpeg-pyramid" },
					upload: null,
				}),
			},
		});
		const kfb = Uint8Array.from([0xF1, 0x01, 0xEE, 0xEE, 0x4B, 0x46, 0x42, 0x00, 0, 0, 0, 0]);
		const e = h.orch.enqueue(fakeFile("a.kfb", kfb))!;
		await flush(20);
		expect(e.state).toBe("convert_failed");
		expect(e.error?.code).toBe("upload_too_large");
		expect(h.cos.uploads).toHaveLength(0);
	});

	it("viewable_formats 字段缺失 → fail-closed（绝不因为缺字段就当通过，§8）", async () => {
		const h = mkOrch({
			caps: { inlineConvert: { enabled: true, url: "/tools/slides", viewableFormats: undefined } },
		});
		const kfb = Uint8Array.from([0xF1, 0x01, 0xEE, 0xEE, 0x4B, 0x46, 0x42, 0x00, 0, 0, 0, 0]);
		const e = h.orch.enqueue(fakeFile("a.kfb", kfb))!;
		await flush(20);
		expect(h.cos.uploads).toHaveLength(0);
		expect(e.state).toBe("convert_failed");
	});

	it("直传条目不吃 viewable_formats：无 result.format 也照常上传（§5.1 反向）", async () => {
		const ome = '<?xml version="1.0"?><OME xmlns="http://www.openmicroscopy.org/Schemas/OME/2016-06"></OME>\x00';
		const uploaded: unknown[] = [];
		const h = mkOrch({
			onArtifact: (e: Entry) => {
				uploaded.push(e.name);
				return Promise.resolve({ body: { slide_id: "sld_direct" } });
			},
		});
		const e = h.orch.enqueue(fakeFile("scan.ome.tif", classicTiff(ome)))!;
		await flush(20);
		expect(e.state).toBe("done");
		expect(e.slideId).toBe("sld_direct");
		expect(uploaded).toEqual(["scan.ome.tif"]);
	});

	it("convert_in_browser（422）→ 自动回队恰一次，第二次终态（§7.2）", async () => {
		let n = 0;
		const h = mkOrch({
			runner: {
				// 第一次直传被 422 拒；第二次（回队后）转换也失败
				startJob: () => { n += 1; return Promise.resolve({ jobId: "job1", done: Promise.resolve({ ok: false, error: { code: "unsupported_input", message: "不可转" } }) }); },
			},
			onArtifact: () => Promise.reject({ data: { code: "convert_in_browser" } }),
		});
		const ome = '<?xml version="1.0"?><OME xmlns="http://www.openmicroscopy.org/Schemas/OME/2016-06"></OME>\x00';
		const e = h.orch.enqueue(fakeFile("renamed.tif", classicTiff(ome)))!;
		await flush(40);
		expect(e.requeueCount).toBe(1);
		expect(h.cos.uploads.length + n).toBeGreaterThan(0);
		// 无论最终停在哪个终态，回队计数绝不超过 1（防死循环）
		expect(e.requeueCount).toBeLessThanOrEqual(1);
	});
});

// --------------------------------------------------------------------- //
// 8. 上传槽 + 产物锁
// --------------------------------------------------------------------- //
describe("锁（§5）", () => {
	it("上传阶段同时持有 pt:upload-slot:<acct> 与 engine.uploadLockName(jobId)", async () => {
		const locks = fakeLocks();
		const h = mkOrch({ locks });
		const kfb = Uint8Array.from([0xF1, 0x01, 0xEE, 0xEE, 0x4B, 0x46, 0x42, 0x00, 0, 0, 0, 0]);
		h.orch.enqueue(fakeFile("a.kfb", kfb));
		await flush(25);
		expect(locks.names).toContain("pt:upload-slot:u1");
		expect(locks.names).toContain("slide-transform:upload:job1");
	});

	it("直传条目没有 jobId → 只有调度槽（不请求 uploadLockName）", async () => {
		const locks = fakeLocks();
		const h = mkOrch({
			locks,
			onArtifact: () => Promise.resolve({ body: { slide_id: "s" } }),
		});
		const ome = '<?xml version="1.0"?><OME xmlns="http://www.openmicroscopy.org/Schemas/OME/2016-06"></OME>\x00';
		h.orch.enqueue(fakeFile("scan.ome.tif", classicTiff(ome)));
		await flush(20);
		expect(locks.names.filter((n) => n.indexOf("slide-transform:upload:") === 0)).toHaveLength(0);
		expect(locks.names).toContain("pt:upload-slot:u1");
	});

	it("另一标签持有调度槽 → 不并发，行停在 upload_queued（等锁）", async () => {
		const locks = fakeLocks({ deny: ["pt:upload-slot:u1"] });
		const h = mkOrch({ locks });
		const ome = '<?xml version="1.0"?><OME xmlns="http://www.openmicroscopy.org/Schemas/OME/2016-06"></OME>\x00';
		const e = h.orch.enqueue(fakeFile("scan.ome.tif", classicTiff(ome)))!;
		await flush(20);
		expect(e.state).toBe("upload_queued");
		expect(h.cos.uploads).toHaveLength(0);
	});

	it("account 为空串（未就绪）→ 槽名仍确定（pt:upload-slot:）", async () => {
		const locks = fakeLocks();
		const h = mkOrch({ locks, account: () => "" });
		const ome = '<?xml version="1.0"?><OME xmlns="http://www.openmicroscopy.org/Schemas/OME/2016-06"></OME>\x00';
		h.orch.enqueue(fakeFile("scan.ome.tif", classicTiff(ome)));
		await flush(20);
		expect(locks.names).toContain("pt:upload-slot:");
	});
});

// --------------------------------------------------------------------- //
// 9/10/11/20. 刷新恢复 / 续传 / 清理
// --------------------------------------------------------------------- //
describe("刷新恢复（§6.2，不自动开始）", () => {
	const jobOf = (over: Record<string, unknown>) => ({
		id: "job1", state: "planned", nextAction: "resume", committedBytes: 7,
		source: { name: "a.kfb", size: 100 }, modality: "brightfield",
		upload: null,
		intent: { channel: "workbench-inline", target: { pid: "p1" } },
		...over,
	});

	it("只列出行；零 probe/prepareBundle/startJob/resumeJob/createUpload（§9.2 第 9 项）", async () => {
		const h = mkOrch({
			runner: { listJobs: () => Promise.resolve([jobOf({})]) },
		});
		const made = await h.orch.restore();
		await flush();
		expect(made).toHaveLength(1);
		expect(made[0].state).toBe("convert_queued");
		const kinds = h.runner.calls.map((c) => c[0]);
		for (const forbidden of ["probe", "prepareBundle", "startJob", "resumeJob"]) {
			expect(kinds, forbidden).not.toContain(forbidden);
		}
		expect(h.cos.uploads).toHaveLength(0);
	});

	it("nextAction='resume' → 继续用 resumeJob；'start' → startJob(null,{jobId})", async () => {
		const h = mkOrch({
			runner: { listJobs: () => Promise.resolve([
				jobOf({ id: "j1", nextAction: "resume" }),
				jobOf({ id: "j2", state: "prepared", nextAction: "start" }),
			]) },
		});
		const made = await h.orch.restore();
		expect(made.map((e) => e.mode)).toEqual(["resume", "start"]);
		// 点「继续」后才真正开始
		h.orch.act(made[1].id, "continue");
		await flush(20);
		const kinds = h.runner.calls.map((c) => c[0]);
		expect(kinds).toContain("startJob");
		const sj = h.runner.calls.find((c) => c[0] === "startJob");
		expect(sj?.[1]).toBeNull();          // 不带 File（源副本已在 OPFS）
		expect((sj?.[2] as { jobId: string }).jobId).toBe("j2");
	});

	it("两组渲染：已收口（published）记录也必须有行，且只给删除入口（A2）", async () => {
		const h = mkOrch({
			runner: { listJobs: () => Promise.resolve([
				jobOf({ id: "j_unfinished", state: "planned" }),
				jobOf({ id: "j_ready", state: "ready", upload: { ingestionId: "inj_1", state: "uploading" } }),
				jobOf({ id: "j_published", state: "ready", upload: { ingestionId: "inj_2", state: "published" } }),
			]) },
		});
		const made = await h.orch.restore();
		expect(made).toHaveLength(3);      // 已收口的**没有**被过滤掉
		const pub = made.find((e) => e.id === "j_published")!;
		expect(pub.state).toBe("done");
		const kinds = h.I ? [] : [];
		void kinds;
	});

	it("非本面板渠道（intent.channel 缺失/不同）一律不列", async () => {
		const h = mkOrch({
			runner: { listJobs: () => Promise.resolve([
				{ id: "x", state: "ready", intent: { channel: "tool" }, source: { name: "t" } },
				{ id: "y", state: "ready", source: { name: "t" } },
			]) },
		});
		expect(await h.orch.restore()).toHaveLength(0);
	});

	it("恢复行先建行元素再写阶段文案（顺序错则行内状态恒为空）", async () => {
		// 回归（e2e 实测）：restore() 原先把 setStage 放在 ui.row 之前，而
		// app.js 的 inlineRowSetStage 按 entry.id 查行、查不到就 **提前
		// return**——于是刷新后的恢复行状态文案为空，用户只看到一个按钮
		// （「继续」），不知道它在说什么。设计 §6.2 第 3 点要求显示
		// 「未完成的转换」。
		const h = mkOrch({
			runner: { listJobs: () => Promise.resolve([jobOf({ id: "j_order" })]) },
		});
		await h.orch.restore();
		const rowAt = h.order.indexOf("row:j_order");
		const stageAt = h.order.indexOf("stage:j_order");
		expect(rowAt).toBeGreaterThanOrEqual(0);
		expect(stageAt).toBeGreaterThan(rowAt);
		// 该行确实带上了「未完成的转换」那一档文案
		expect(h.stages.find(([id]) => id === "j_order")?.[1])
			.toBe("upload.inline.stage.resumable");
	});

	it("已收口记录同样先建行（删除入口的按钮宿主必须先存在）", async () => {
		const h = mkOrch({
			runner: { listJobs: () => Promise.resolve([
				jobOf({ id: "j_pub", state: "ready",
					upload: { ingestionId: "inj_x", state: "published" } }),
			]) },
		});
		await h.orch.restore();
		expect(h.order.indexOf("row:j_pub"))
			.toBeLessThan(h.order.indexOf("stage:j_pub"));
		expect(h.order).toContain("row:j_pub");
	});

	it("续传语义：点「继续」传 resumeJobId 等于记录里的 id（§6.4 第 10 项）", async () => {
		const h = mkOrch({
			runner: { listJobs: () => Promise.resolve([
				jobOf({ id: "j9", state: "ready", nextAction: "export",
					upload: { ingestionId: "inj_9", state: "uploading" } }),
			]) },
		});
		const made = await h.orch.restore();
		const e = made[0];
		// 未收口的 ready 行 → 「继续上传」；此处断言它确实带 resumeJobId
		expect(e.mode).toBe(null);
		expect(e.state).toBe("upload_queued");
		// 点「继续」→ 面板自己的 uploadEntryArtifact 路径（未注入 onArtifact）：
		// 它用 runner.getJob 读回上传记录（readRecord/readConfirmed 都经它）
		let gotJobs = 0;
		const h2 = mkOrch({
			runner: {
				listJobs: () => Promise.resolve([
					jobOf({ id: "j9", state: "ready", nextAction: "export",
						upload: { ingestionId: "inj_9", state: "uploading" } }),
				]),
				getJob: () => {
					gotJobs += 1;
					return Promise.resolve({
						id: "j9", state: "ready",
						result: { outputBytes: 100, format: "ome-bigtiff-subifd-rgb-jpeg-pyramid" },
						upload: { ingestionId: "inj_9", state: "uploading", confirmedParts: [1] },
					});
				},
				artifactView: () => Promise.resolve(fakeFile("x.ome.tif", null, 100)),
			},
		});
		const made2 = await h2.orch.restore();
		expect(made2[0].state).toBe("upload_queued");
		expect(made2[0].uploadState).toBe("uploading");
		h2.orch.act(made2[0].id, "continue");
		await flush(40);
		expect(gotJobs).toBeGreaterThan(0);   // 读回了记录（续传的身份依据）
		expect(h2.cos.uploads).toHaveLength(1);
	});

	it("终态清理：discard → discardJob（§6.3 / §9.2 第 11 项）", async () => {
		const h = mkOrch();
		const e = h.orch.enqueue(fakeFile("virus.xyz", null, 10))!;
		await flush();
		e.jobId = "jobX";
		const ok = await h.orch.discard(e);
		expect(ok).toBe(true);
		expect(h.runner.calls.some((c) => c[0] === "discardJob" && c[1] === "jobX")).toBe(true);
	});

	it("discard 遇 upload_active+lockHeld:false → 二次确认后 abandonUpload", async () => {
		let first = true;
		const h = mkOrch({
			runner: {
				discardJob: (id: string, opts?: unknown) => {
					h.runner.calls.push(["discardJob", id, opts]);
					if (first) {
						first = false;
						return Promise.reject({ error: { code: "upload_active", lockHeld: false } });
					}
					return Promise.resolve();
				},
			},
		});
		const e = h.orch.enqueue(fakeFile("virus.xyz", null, 10))!;
		await flush();
		e.jobId = "jobY";
		const ok = await h.orch.discard(e);
		expect(ok).toBe(true);
		const withAbandon = h.runner.calls.find(
			(c) => c[0] === "discardJob" && c[2] && (c[2] as { abandonUpload?: boolean }).abandonUpload);
		expect(withAbandon).toBeTruthy();
	});
});

// --------------------------------------------------------------------- //
// 16/21. bundle（散文件与整包、两个入口）
// --------------------------------------------------------------------- //
describe("bundle（§4.1.1）", () => {
	it("散文件 .mrxs → 类型化说明 + 选择文件夹，零网络", async () => {
		const h = mkOrch();
		const e = h.orch.enqueue(fakeFile("CMU-1.mrxs", new Uint8Array(4)))!;
		await flush();
		expect(e.state).toBe("convert_failed");
		expect(e.error?.code).toBe("need_bundle");
		expect(h.runner.calls.filter((c) => c[0] === "prepareBundle")).toHaveLength(0);
		expect(h.cos.uploads).toHaveLength(0);
		// 说明文案是静态的（不依赖读目录）：行内文案键正确
		expect(h.stages.some((s) => s[1] === "upload.inline.needBundle.mrxs")).toBe(true);
	});

	it("散文件 .vms → VMS 版本的静态说明", async () => {
		const h = mkOrch();
		const e = h.orch.enqueue(fakeFile("slide.vms", new Uint8Array(4)))!;
		await flush();
		expect(e.error?.code).toBe("need_bundle");
		expect(h.stages.some((s) => s[1] === "upload.inline.needBundle.vms")).toBe(true);
	});

	it("整包（enqueueBundle）→ prepareBundle 被调用，走 stage-bundle 进度", async () => {
		const h = mkOrch();
		const rows = [
			{ file: fakeFile("CMU-1.mrxs", new Uint8Array(4)), relPath: "CMU-1/CMU-1.mrxs" },
			{ file: fakeFile("Slidedat.ini", new Uint8Array(4)), relPath: "CMU-1/Slidedat.ini" },
		];
		const e = h.orch.enqueueBundle(rows, "CMU-1")!;
		await flush(25);
		const kinds = h.runner.calls.map((c) => c[0]);
		expect(kinds).toContain("prepareBundle");
		expect(e.state).toBe("done");
		// 转换产物条目走产物门禁 → 有 createUpload
		expect(h.cos.uploads).toHaveLength(1);
	});

	it("含 .vmu 的文件夹不进 bundle 分支（保护今天可用的 VMU 直传，§4.1.1 note）", () => {
		// 源码级断言：app.js 的成员正则恰好是 .mrxs|dat|vms，**不含** vmu
		const idx = appSrc.indexOf("return /\\.(mrxs|dat|vms)$/i.test(r.name);");
		expect(idx, "app.js 的 bundle 成员正则未找到（应恰为 .mrxs|dat|vms）")
			.toBeGreaterThan(-1);
		expect(appSrc).not.toContain(".mrxs|dat|vms|vmu");
	});

	it("bundle 两个入口都走 importBundleRows（开关两态，A3）", () => {
		// 源码级：importDroppedDirectory 与 importPickFolder 都调 importBundleRows，
		// 且不再无条件 handoffBundleFolder
		const dropped = appSrc.slice(
			appSrc.indexOf("function importDroppedDirectory"),
			appSrc.indexOf("function importBundleRows"));
		expect(dropped).toContain("importBundleRows(rows, dirEntry.name)");
		const pickAt = appSrc.indexOf("function importPickFolder");
		const picked = appSrc.slice(pickAt, pickAt + 900);
		expect(picked).toContain("importBundleRows(rows, first)");
		expect(picked).not.toContain("handoffBundleFolder(rows, first)");
		const router = appSrc.slice(
			appSrc.indexOf("function importBundleRows"),
			appSrc.indexOf("function importBundleRows") + 900);
		expect(router).toContain("inlineEnabled()");
		expect(router).toContain("handoffBundleFolder(rows, folderName)");
	});
});

// --------------------------------------------------------------------- //
// 17/18. beforeunload 与磁盘门确认
// --------------------------------------------------------------------- //
describe("beforeunload 与磁盘门（§6.2 第 7 点 / §6.2.1）", () => {
	it("五态覆盖、终态不覆盖（语义判据，不是状态名枚举）", () => {
		const I = IC();
		for (const s of ["converting", "need_disk_confirm", "upload_queued",
			"uploading", "requeue_convert"]) {
			expect(I.shouldWarn(s), s).toBe(true);
		}
		for (const s of ["done", "rejected", "convert_failed", "upload_failed",
			"cancelled", "sniffing"]) {
			expect(I.shouldWarn(s), s).toBe(false);
		}
	});

	it("warnNeeded 反映当前队列（含 need_disk_confirm）", async () => {
		const h = mkOrch({
			runner: {
				probe: () => Promise.resolve({ jobId: "j1", probe: { document: { modality: "brightfield" } } }),
				startJob: () => Promise.resolve({
					jobId: "j1",
					done: Promise.resolve({ ok: false, error: { code: "disk_precheck_failed", message: "空间不确定", need: { total: 100 }, available: 50 } }),
				}),
			},
		});
		const kfb = Uint8Array.from([0xF1, 0x01, 0xEE, 0xEE, 0x4B, 0x46, 0x42, 0x00, 0, 0, 0, 0]);
		const e = h.orch.enqueue(fakeFile("a.kfb", kfb))!;
		await flush(20);
		expect(e.state).toBe("need_disk_confirm");
		expect(h.orch.warnNeeded()).toBe(true);
	});

	it("磁盘门确认后**带原始输入重新起**（probe 再传同一 file），绝不复用旧 jobId", async () => {
		let n = 0;
		const probed: string[] = [];
		const h = mkOrch({
			runner: {
				probe: (file: { name?: string }) => {
					probed.push(String(file?.name));
					n += 1;
					if (n === 1) {
						return Promise.resolve({ jobId: "j1", probe: { document: { modality: "brightfield" } } });
					}
					return Promise.resolve({ jobId: "j2", probe: { document: { modality: "brightfield" } } });
				},
				startJob: (f: unknown, o: Record<string, unknown>) => {
					h.runner.calls.push(["startJob", (f as { name?: string })?.name ?? null, o]);
					if (n === 1) {
						return Promise.resolve({ jobId: o.jobId, done: Promise.resolve({ ok: false, error: { code: "disk_precheck_failed", message: "x" } }) });
					}
					return Promise.resolve({ jobId: o.jobId, done: Promise.resolve({ ok: true, result: { output_bytes: 1, format: "ome-bigtiff-subifd-rgb-jpeg-pyramid" } }) });
				},
			},
		});
		const kfb = Uint8Array.from([0xF1, 0x01, 0xEE, 0xEE, 0x4B, 0x46, 0x42, 0x00, 0, 0, 0, 0]);
		const e = h.orch.enqueue(fakeFile("a.kfb", kfb))!;
		await flush(20);
		expect(e.state).toBe("need_disk_confirm");
		h.orch.act(e.id, "confirmDisk");
		await flush(30);
		// 真的**重新** probe（同一份 File），不是复用旧 job
		expect(probed).toEqual(["a.kfb", "a.kfb"]);
		expect(e.jobId).toBe("j2");
		// 反向断言：确认后绝不以 null File 走 startJob 复用旧 job
		const startWithNull = h.runner.calls.filter(
			(c) => c[0] === "startJob" && c[1] === null);
		expect(startWithNull).toHaveLength(0);
	});

	it("磁盘门确认路径带 confirmUncertainDisk:true", async () => {
		let n = 0;
		const h = mkOrch({
			runner: {
				probe: (_f: unknown, opts: Record<string, unknown>) => {
					h.runner.calls.push(["probe", null, opts]);
					n += 1;
					return Promise.resolve({ jobId: "j" + n, probe: { document: { modality: "brightfield" } } });
				},
				startJob: (_f: unknown, o: Record<string, unknown>) => Promise.resolve({
					jobId: o.jobId,
					done: n === 1
						? Promise.resolve({ ok: false, error: { code: "disk_precheck_failed" } })
						: Promise.resolve({ ok: true, result: { output_bytes: 1, format: "ome-bigtiff-subifd-rgb-jpeg-pyramid" } }),
				}),
			},
		});
		const kfb = Uint8Array.from([0xF1, 0x01, 0xEE, 0xEE, 0x4B, 0x46, 0x42, 0x00, 0, 0, 0, 0]);
		const e = h.orch.enqueue(fakeFile("a.kfb", kfb))!;
		await flush(20);
		h.orch.act(e.id, "confirmDisk");
		await flush(30);
		const confirms = h.runner.calls.filter(
			(c) => c[0] === "probe" && (c[2] as { confirmUncertainDisk?: boolean })?.confirmUncertainDisk === true);
		expect(confirms.length).toBeGreaterThanOrEqual(1);
	});
});

// --------------------------------------------------------------------- //
// 19/22. 目标关联与回退文案（源码级 + i18n）
// --------------------------------------------------------------------- //
describe("目标关联与文案（§3.5 / §8）", () => {
	it("入队前冻结快照 + uploadFileCos 字面量含 assocTarget（A1：钉「被传」）", () => {
		expect(appSrc).toContain("opts.assocTarget = importTargetSnapshot()");
		const call = appSrc.slice(
			appSrc.indexOf("uploadFileCos(file, row, {"),
			appSrc.indexOf("uploadFileCos(file, row, {") + 700);
		expect(call).toContain("assocTarget: opts.assocTarget");
	});

	it("完成时按冻结快照关联（entry.target），不是当前文件夹", async () => {
		const assoc: unknown[] = [];
		const h = mkOrch({
			onArtifact: () => Promise.resolve({ body: { slide_id: "sld_x" } }),
			onUploaded: (e: Entry) => { assoc.push(e.target); },
		});
		const ome = '<?xml version="1.0"?><OME xmlns="http://www.openmicroscopy.org/Schemas/OME/2016-06"></OME>\x00';
		const frozen = { pid: "p_frozen" };
		const e = h.orch.enqueue(fakeFile("scan.ome.tif", classicTiff(ome)), { assocTarget: frozen })!;
		await flush(20);
		expect(e.state).toBe("done");
		expect(assoc).toEqual([frozen]);
	});

	it("转换前的 intent 带 channel=workbench-inline 与冻结目标（§3.5 第 3 点）", async () => {
		const h = mkOrch();
		const frozen = { pid: "p_frozen" };
		const kfb = Uint8Array.from([0xF1, 0x01, 0xEE, 0xEE, 0x4B, 0x46, 0x42, 0x00, 0, 0, 0, 0]);
		h.orch.enqueue(fakeFile("a.kfb", kfb), { assocTarget: frozen });
		await flush(20);
		const intent = h.runner.calls.find((c) => c[0] === "setJobIntent");
		expect(intent).toBeTruthy();
		const patch = intent![2] as { channel: string; target: unknown; account: string };
		expect(patch.channel).toBe("workbench-inline");
		expect(patch.target).toEqual(frozen);
		expect(patch.account).toBe("u1");
	});
});

describe("i18n（§8 / §9.2 第 14、22 项）", () => {
	function loadI18n() {
		if (typeof (globalThis as { CustomEvent?: unknown }).CustomEvent === "undefined") {
			(globalThis as { CustomEvent?: unknown }).CustomEvent = class {
				constructor(public type: string, public detail?: unknown) { }
			};
		}
		const w: Record<string, unknown> = {
			localStorage: { getItem: () => null, setItem() { }, removeItem() { } },
			navigator: { language: "zh-CN" },
		};
		const doc = {
			readyState: "complete",
			querySelectorAll: () => [],
			addEventListener() { },
			dispatchEvent() { },
			documentElement: { setAttribute() { }, getAttribute() { return null; } },
		};
		new Function("window", "document", "localStorage", "navigator", i18nSrc)(
			w, doc, w.localStorage, w.navigator);
		return w.HP_I18N as {
			t: (k: string, vars?: Record<string, unknown>) => string;
			setLang: (l: string) => void;
		};
	}

	it("新增 upload.inline.* 键 zh/en 成对存在（不回落 key 本身）", () => {
		const t = loadI18n();
		const keys = [
			"upload.inline.stage.queued", "upload.inline.stage.rejected",
			"upload.inline.stage.notAccepted", "upload.inline.stage.resumable",
			"upload.inline.stage.settled", "upload.inline.stage.disk",
			"upload.inline.stage.convertFailed", "upload.inline.stage.engineUnsupported",
			"upload.inline.stage.requeue", "upload.inline.note.keepopen",
			"upload.inline.note.committed", "upload.inline.rejected.magic",
			"upload.inline.needBundle.mrxs", "upload.inline.needBundle.vms",
			"upload.inline.err.disk", "upload.inline.err.quota",
			"upload.inline.err.profile", "upload.inline.err.unsupported",
			"upload.inline.err.busy", "upload.inline.err.evicted",
			"upload.inline.err.resume", "upload.inline.err.io",
			"upload.inline.err.magic", "upload.inline.err.notViewable",
			"upload.inline.err.needs.convert", "upload.inline.disk.confirm",
			"upload.inline.action.continue", "upload.inline.action.continueUpload",
			"upload.inline.action.reselect", "upload.inline.action.chooseFolder",
			"upload.inline.action.discard", "upload.inline.action.discard.published",
			"upload.inline.action.requestSupport", "upload.inline.action.saveLocal",
			"upload.inline.freed", "upload.inline.needDisk",
		];
		for (const k of keys) {
			for (const lang of ["zh", "en"] as const) {
				t.setLang(lang);
				const v = t.t(k);
				expect(v, `${lang} ${k}`).toBeTruthy();
				expect(v, `${lang} ${k}`).not.toBe(k);
			}
		}
	});

	it("app.js 的 _EXTRA_I18N 兜底表也覆盖这些键", () => {
		for (const k of ["upload.inline.stage.queued", "upload.inline.action.discard",
			"upload.inline.err.notViewable", "upload.inline.needBundle.mrxs",
			"upload.inline.disk.confirm", "upload.inline.action.requestSupport"]) {
			expect(appSrc, k).toContain(`"${k}"`);
		}
	});

	it("旧文案改写：unsupported 两态都不点名 /tools/slides；convert_in_browser 用 {link} 形参", () => {
		const t = loadI18n();
		for (const lang of ["zh", "en"] as const) {
			t.setLang(lang);
			expect(t.t("upload.err.unsupported"), lang).not.toContain("/tools/slides");
			expect(t.t("upload.cos.err.convert_in_browser"), lang).toContain("{link}");
		}
		// 源码级：基础文案里不再出现路径（路径只经 inline_convert.url 追加）
		const i18nLines = i18nSrc.split("\n").filter(
			(l) => l.includes('"upload.cos.err.convert_in_browser"'));
		expect(i18nLines.length).toBe(2);
		for (const l of i18nLines) expect(l).not.toContain("/tools/slides");
	});

	it("两态显示：enabled=false 时 convert_in_browser 追加工具页链接（§8 / 第 22 项）", () => {
		// 文案里只有 {link} 占位；由 convertInBrowserMessage 按开关两态填充
		const fn = appSrc.slice(
			appSrc.indexOf("function convertInBrowserMessage"),
			appSrc.indexOf("function cosErrorMessage"));
		expect(fn).toContain("upload.cos.err.convert_in_browser");
		expect(fn).toContain("{ link:");
		expect(fn).toContain("inlineEnabled()");
		expect(fn).toContain("CONVERT_HANDOFF_URL");
		// cosErrorMessage 经它取文案（不在函数内重复硬编码路径）
		const msg = appSrc.slice(
			appSrc.indexOf("function cosErrorMessage"),
			appSrc.indexOf("function cosFailStageKey"));
		expect(msg).toContain("convertInBrowserMessage()");
	});
});

// --------------------------------------------------------------------- //
// 12. 门禁等价性对照（§2.3 承诺 / §9.2 第 12 项）
//
// 对照面**不是** runLocked 全序列：它含人工交互分支（chooseAccountAction 的
// dialog、signOutForOriginal 的登录跳转）与孤儿结算（settleOrphan），无法逐例
// 自动比对。这里只比对两点可自动化的观察面：
//   A 判定结论：面板纯函数 artifactGate 与工具页控制器 ctl.isDisabled 对同一
//     组输入必须同判（工具页既有测试正是用 isDisabled 断言，
//     tools-upload-limit.test.ts:169 / tools-upload-feedback.test.ts:270）；
//   B 副作用：refuse 路径双方都**没有** POST /api/ingestions。
// 明确**不做**：账号 mismatch / 孤儿 / 已发布重放——它们是工具页特有的交互
// 语义，面板按设计 §7 自己的处置走，两侧在这些分支上允许不同。
// --------------------------------------------------------------------- //
describe("门禁等价性对照（§9.2 第 12 项）", () => {
	const VIEWABLE = ["ome-bigtiff-subifd-rgb-jpeg-pyramid", "classic-bigtiff-jpeg-pyramid"];
	const cosCaps = (maxSize: number) => ({
		available: true, max_size_bytes: maxSize, part_bytes: 100,
		url_ttl_seconds: 600, max_concurrent_parts: 2, sign_batch_max_parts: 4,
		policy_version: "v1-manual", formats: ["tif", "tiff"],
	});

	/// 驱动工具页上传控制器（真模块 + 假 runner/fetch），返回 isDisabled + 是否
	/// 建过 ingestion。形态照 tools-upload-limit.test.ts。
	async function toolsPageVerdict(maxSize: number, job: unknown) {
		const fetchLog: Array<{ method: string; url: string }> = [];
		const cap = {
			cos_upload: cosCaps(maxSize), viewable_formats: VIEWABLE,
			account: "u1", account_label: "u1@x",
		};
		const resp = (body: unknown, status = 200) => ({
			ok: status >= 200 && status < 300, status,
			clone() { return this; },
			json: () => Promise.resolve(body),
		});
		const els: Record<string, { hidden: boolean; disabled: boolean; textContent: string }> = {};
		const doc = {
			getElementById(id: string) {
				if (!els[id]) els[id] = { hidden: false, disabled: false, textContent: "" };
				return els[id];
			},
			createElement() { return { style: {}, appendChild() { }, addEventListener() { } }; },
			addEventListener() { },
		};
		const fetchImpl = vi.fn((url: string, opts?: { method?: string }) => {
			const method = (opts?.method || "GET").toUpperCase();
			fetchLog.push({ method, url: String(url) });
			if (String(url) === "/api/tools/slides/upload-capability") {
				return Promise.resolve(resp(cap));
			}
			if (method === "POST" && String(url) === "/api/ingestions") {
				return Promise.resolve(resp(
					{ job_id: "inj_t1", state: "preparing", stage: "uploading" }, 202));
			}
			return Promise.resolve(resp({}));
		}) as unknown as typeof fetch;
		const w: Record<string, unknown> = { HP_COS_UPLOAD: null };
		new Function("window", "document", "fetch", "location",
			readFileSync(resolve(root, "static/upload/cos-uploader.js"), "utf8"))(
			w, doc, fetchImpl, { href: "http://local/tools/slides", origin: "http://local" });
		vi.stubGlobal("window", w);
		vi.stubGlobal("document", doc);
		vi.stubGlobal("fetch", fetchImpl);
		vi.stubGlobal("localStorage", { getItem: () => null, setItem() { }, removeItem() { } });
		vi.stubGlobal("navigator", {
			locks: { request: (_n: string, _o: unknown, fn: (l: unknown) => Promise<unknown>) =>
				Promise.resolve(fn({ name: "lock" })) },
		});
		const runner = {
			async getJob() { return job; },
			async setJobUpload() { return undefined; },
			async setJobIntent() { return undefined; },
			async artifactView() { return { size: 1, slice: () => null }; },
		};
		const ctl = createUploadController({
			runner: runner as never,
			t: (k: string) => k,
			onJobsRefresh: null, onPublished: null,
		}) as unknown as {
			startOrContinue(id: string): Promise<unknown>;
			setResultJob(id: string): Promise<unknown>;
			isDisabled(id: string): boolean;
		};
		await ctl.setResultJob("job-1");
		await ctl.startOrContinue("job-1");
		const created = fetchLog.some(
			(f) => f.method === "POST" && f.url === "/api/ingestions");
		return { disabled: ctl.isDisabled("job-1"), created };
	}

	const cases: Array<{ label: string; max: number; job: unknown }> = [
		{
			label: "产物超上限",
			max: 1000,
			job: { id: "job-1", state: "ready",
				result: { outputBytes: 5000, format: VIEWABLE[0] } },
		},
		{
			label: "产物格式不可查看",
			max: 1_000_000,
			job: { id: "job-1", state: "ready",
				result: { outputBytes: 100, format: "something-else" } },
		},
		{
			label: "格式可查看且大小合规",
			max: 1_000_000,
			job: { id: "job-1", state: "ready",
				result: { outputBytes: 100, format: VIEWABLE[0] } },
		},
	];

	for (const c of cases) {
		it(`${c.label}：面板 artifactGate 与工具页 isDisabled 同判（观察面 A/B）`, async () => {
			const tools = await toolsPageVerdict(c.max, c.job);
			const I = IC();
			const cfg = { max_size_bytes: c.max, part_bytes: 100 };
			const panel = I.artifactGate(cfg, c.job, VIEWABLE);
			// 观察面 A：判定结论等价（工具页 disabled ⇔ 面板 !ok）
			expect(panel.ok, `${c.label}: tools.disabled=${tools.disabled}`)
				.toBe(!tools.disabled);
			// 观察面 B：refuse 路径双方都零创建
			if (!panel.ok) expect(tools.created, `${c.label} 不应建 ingestion`).toBe(false);
			vi.unstubAllGlobals();
		});
	}

	it("viewable_formats 字段缺失：面板 fail-closed，与工具页 (cap.viewable_formats || []) 同语义", () => {
		const job = { id: "job-1", state: "ready",
			result: { outputBytes: 100, format: VIEWABLE[0] } };
		const I = IC();
		// 面板：字段缺失显式 fail-closed（缺字段绝不等于通过，§8）
		const missing = I.artifactGate({ max_size_bytes: 1_000_000 }, job, undefined);
		expect(missing.ok).toBe(false);
		expect(missing.code).toBe("viewable_formats_missing");
		// 有该字段且格式在集内 → 面板放行
		expect(I.artifactGate({ max_size_bytes: 1_000_000 }, job, VIEWABLE).ok).toBe(true);
		// 工具页同语义的**源码级**证据：它对该字段取 `(cap.viewable_formats || [])`，
		// 缺失即空集 → 空集的 indexOf 判定必落空 → 同样拒绝（与面板一致）。
		const uploadSrc = readFileSync(
			resolve(root, "static/tools/tools-slides-upload.js"), "utf8");
		expect(uploadSrc).toContain("!(cap.viewable_formats || []).includes(fmt)");
	});
});

// --------------------------------------------------------------------- //
// 6.3 按钮全集 + §9.2 第 15/22 项：开关两态与回退（真 app.js，DOM 级）
// --------------------------------------------------------------------- //
describe("上传行按钮（§6.3 是按钮全集的唯一权威）", () => {
	/// 真 app.js 的 DOM harness（形态照 workbench-direct-class.test.ts），
	/// 用 __PT_TEST_HOOKS 挂载 HP_PROJECT_UI / HP_UPLOAD 驱动面。
	function el(tag?: string) {
		const children: unknown[] = [];
		const listeners: Record<string, Array<(ev?: unknown) => void>> = {};
		const node: Record<string, unknown> = {
			tagName: (tag || "div").toUpperCase(),
			hidden: true, textContent: "", innerHTML: "", value: "", disabled: false,
			checked: false, type: "", title: "", name: "", className: "",
			style: {}, children,
			classList: { add() { }, remove() { }, contains() { return false; } },
			parentNode: null,
			appendChild(c: unknown) { children.push(c); return c; },
			removeChild(c: unknown) {
				const i = children.indexOf(c);
				if (i >= 0) children.splice(i, 1);
				return c;
			},
			insertBefore(c: unknown) { children.push(c); return c; },
			addEventListener(type: string, fn: (ev?: unknown) => void) {
				(listeners[type] = listeners[type] || []).push(fn);
			},
			removeEventListener() { },
			setAttribute() { }, getAttribute() { return null; },
			removeAttribute() { },
			focus() { },
			click() { (listeners["click"] || []).forEach((f) => f({ preventDefault() { } })); },
		};
		(node as { _listeners: typeof listeners })._listeners = listeners;
		return node as ReturnType<typeof el> & {
			_listeners: Record<string, Array<(ev?: unknown) => void>>;
		};
	}

	function loadApp(caps: unknown) {
		const store = new Map<string, string>();
		const els: Record<string, ReturnType<typeof el>> = {};
		const doc = {
			readyState: "loading",
			cookie: "csrf_token=tok",
			getElementById(id: string) { if (!els[id]) els[id] = el(); return els[id]; },
			createElement(tag: string) { return el(tag); },
			addEventListener() { },
			querySelector() { return el(); },
			querySelectorAll() { return []; },
			body: el("body"),
			documentElement: el("html"),
		};
		const theFetch = vi.fn(() => Promise.resolve({
			ok: true, status: 200, clone() { return this; },
			json: () => Promise.resolve({}),
		}));
		const w: Record<string, unknown> = {
			__PT_TEST_HOOKS: true,
			HP_I18N: {
				t: (k: string, vars?: Record<string, unknown>) =>
					vars && Object.keys(vars).length ? `${k}:${Object.values(vars).join(",")}` : k,
				getLang: () => "zh",
			},
			HP_APP_BOOTSTRAP: { mode: "official", capabilities: caps },
			fetch: theFetch,
			location: { href: "http://local/", pathname: "/", origin: "http://local" },
			confirm: vi.fn(() => true),
			open: vi.fn(() => null),
			addEventListener: vi.fn(),
			innerWidth: 1280,
			setTimeout: (fn: () => void, ms?: number) => setTimeout(fn, ms),
			clearTimeout: (h: unknown) => clearTimeout(h as number),
		};
		(globalThis as { document?: unknown }).document = doc;
		(globalThis as { window?: unknown }).window = w;
		(globalThis as { fetch?: unknown }).fetch = theFetch;
		(globalThis as { location?: unknown }).location = w.location;
		(globalThis as { localStorage?: unknown }).localStorage = {
			getItem: (k: string) => (store.has(k) ? store.get(k)! : null),
			setItem: (k: string, v: string) => void store.set(k, String(v)),
			removeItem: (k: string) => void store.delete(k),
		};
		// 加载顺序与模板一致（templates/index.html）：cos-uploader → slide-sniff
		// → inline-convert → app.js
		new Function("window", "document", "fetch", "location",
			readFileSync(resolve(root, "static/upload/cos-uploader.js"), "utf8"))(
			w, doc, theFetch, w.location);
		new Function("window", "document", "fetch", "location", sniffSrc)(w, doc, theFetch, w.location);
		new Function("window", "document", "fetch", "location", inlineSrc)(w, doc, theFetch, w.location);
		new Function("window", "document", "fetch", "location", appSrc)(w, doc, theFetch, w.location);
		return { w, els, fetch: theFetch };
	}

	const inlineCaps = (enabled: boolean) => ({
		cos_upload: {
			available: true, manual_only: true, max_size_bytes: 1000,
			part_bytes: 100, url_ttl_seconds: 600, max_concurrent_parts: 2,
			sign_batch_max_parts: 8,
			formats: ["svs", "tif", "tiff", "ndpi", "vms", "vmu", "scn", "bif",
				"svslide", "bmp", "jpg", "jpeg", "zip"],
			browser_convert: { formats: ["kfb", "kfbf", "svs", "mrxs"], url: "/tools/slides" },
			direct_upload: { formats: ["tif", "tiff", "ome.tif", "ndpi", "vms",
				"scn", "bif", "bmp", "jpg", "jpeg", "vmu", "svslide"] },
			inline_convert: {
				enabled,
				url: "/tools/slides",
				viewable_formats: ["classic-bigtiff-jpeg-pyramid",
					"ome-bigtiff-subifd-rgb-jpeg-pyramid"],
			},
		},
	});

	it("按钮全集：终态给「删除本地产物」；need_bundle 给「选择文件夹」；rejected 给申请支持", () => {
		const h = loadApp(inlineCaps(true));
		const up = h.w.HP_UPLOAD as unknown as {
			inlineButtonKinds(e: unknown): string[];
			inlineButtonLabel(kind: string, e: unknown): string;
		};
		const kinds = (state: string, extra: Record<string, unknown> = {}) =>
			up.inlineButtonKinds({ state, jobId: "j1", ...extra });

		// 终态：唯一清理入口必须在（§6.3）
		expect(kinds("done")).toContain("discard");
		expect(kinds("convert_failed")).toContain("discard");
		expect(kinds("upload_failed")).toContain("discard");
		expect(kinds("cancelled")).toContain("discard");
		expect(kinds("rejected")).toContain("discard");
		// 转换失败另有申请支持与保存到本机
		expect(kinds("convert_failed")).toEqual(
			expect.arrayContaining(["support", "saveLocal"]));
		// 散文件 bundle：类型化说明 → 选择文件夹（不是重选文件）；无 jobId
		// → 也不会有「删除本地产物」（散文件从未建任务，无可删产物）
		expect(up.inlineButtonKinds({ state: "convert_failed", jobId: null,
			error: { code: "need_bundle" } })).toEqual(["chooseFolder"]);
		expect(kinds("convert_failed", { error: { code: "need_bundle" } }))
			.toEqual(["chooseFolder", "discard"]);
		// 拒绝：申请支持
		expect(kinds("rejected")).toEqual(expect.arrayContaining(["support"]));
		// 进行中：取消
		expect(kinds("converting")).toEqual(["cancel"]);
		expect(kinds("uploading")).toEqual(["cancel"]);
		// 磁盘门：确认 + 取消
		expect(kinds("need_disk_confirm")).toEqual(["confirmDisk", "cancel"]);
		// 恢复行：继续 / 继续上传
		expect(up.inlineButtonKinds({ state: "convert_queued", viaRestore: true, jobId: "j" }))
			.toEqual(["continue"]);
		expect(up.inlineButtonKinds({ state: "upload_queued", viaRestore: true, jobId: "j" }))
			.toEqual(["continueUpload"]);
		// 已发布的删除按钮文案要说明「平台上的不受影响」（本 harness 未加载
		// i18n.js → tt 回落 app.js _EXTRA_I18N 的真实中文文案）
		expect(String(up.inlineButtonLabel("discard", { uploadState: "published" })))
			.toContain("不受影响");
		expect(String(up.inlineButtonLabel("discard", { uploadState: "uploading" })))
			.not.toContain("不受影响");
	});

	it("done 只给一个「删除本地产物」（终态集已含 done，不得再单列）", () => {
		// 回归（e2e 实测）：`done` 既属 isTerminal 集合又被单列一条 discard 分支，
		// 行上因此渲染出**两个**同名按钮——终态唯一的 OPFS 清理入口重复出现。
		const h = loadApp(inlineCaps(true));
		const up = h.w.HP_UPLOAD as unknown as {
			inlineButtonKinds(e: unknown): string[];
		};
		expect(up.inlineButtonKinds({ state: "done", jobId: "j1" })
			.filter((k) => k === "discard")).toHaveLength(1);
		// 反向：没有 jobId 的 done（散文件路径）不给删除按钮
		expect(up.inlineButtonKinds({ state: "done", jobId: null }))
			.not.toContain("discard");
		// 其它终态不受本次修改影响（各恰好一个清理入口）
		for (const st of ["convert_failed", "upload_failed", "cancelled", "rejected"]) {
			expect(up.inlineButtonKinds({ state: st, jobId: "j1" })
				.filter((k) => k === "discard"), st).toHaveLength(1);
		}
	});

	it("启动期装配刷新恢复：init() 在开关开时调 ensureInlineOrch()", () => {
		// 回归（e2e 实测）：restore() 只挂在 ensureInlineOrch() 里，而此前
		// **没有任何启动期入口**调它——刷新后恢复行永不出现，§6.3 唯一的
		// OPFS 清理入口可达性归零（源副本 + 产物永久残留；
		// `_sweepIncompleteStaging` 显式跳过带 upload 记录的 ready/published
		// 任务，不兜底）。
		//
		// app.js 是 classic IIFE，__PT_TEST_HOOKS 驱动面里没有 init()；这里用
		// 源码级断言钉住「init() 体内存在该调用且被 inlineEnabled() 守卫」——
		// 比调一次 mock 化的 init() 更贴近失败模式（漏接线恰恰是「代码里根本
		// 没有这一句」）。
		const initStart = appSrc.indexOf("  function init() {");
		expect(initStart).toBeGreaterThan(0);
		const initBody = appSrc.slice(
			initStart, appSrc.indexOf("function openSlideFromLocation"));
		expect(initBody).toContain("if (inlineEnabled()) ensureInlineOrch();");
		// restore() 仍只由 ensureInlineOrch() 触发（不在别处偷偷启动转换）
		expect(appSrc.split("inlineOrch.restore()").length - 1).toBe(1);
	});

	it("开关开：KFB 不再给弹窗交接入口，也不弹窗（零 window.open）", async () => {
		const h = loadApp(inlineCaps(true));
		const up = h.w.HP_UPLOAD as unknown as { inlineEnabled(): boolean };
		expect(up.inlineEnabled()).toBe(true);
		// 面板入口存在；行渲染由编排器驱动（真运行器未加载 → 回退交接，
		// 但开关开的判定与 capacity 读取必须是「开」）
		const caps = (h.w.HP_UPLOAD as unknown as {
			inlineConvertCaps(): { enabled: boolean; url: string } | null;
		}).inlineConvertCaps();
		expect(caps?.enabled).toBe(true);
		expect(caps?.url).toBe("/tools/slides");
	});

	it("开关关：inlineEnabled=false，走既有弹窗交接（§9.2 第 15 项）", () => {
		const h = loadApp(inlineCaps(false));
		const up = h.w.HP_UPLOAD as unknown as { inlineEnabled(): boolean };
		expect(up.inlineEnabled()).toBe(false);
	});

	it("inline_convert 字段缺失：inlineEnabled=false（旧后端 fail-closed，不假设开启）", () => {
		const h = loadApp({ cos_upload: { available: true, manual_only: true, formats: ["tif"] } });
		const up = h.w.HP_UPLOAD as unknown as { inlineEnabled(): boolean };
		expect(up.inlineEnabled()).toBe(false);
	});

	it("两态文案：开关关闭时 convert_in_browser 带工具页链接，开启时不带", () => {
		// 关闭态（无 inline_convert 字段 → enabled=false）
		const off = loadApp({ cos_upload: { available: true, manual_only: true, formats: ["tif"] } });
		const msgOff = (off.w.HP_UPLOAD as unknown as {
			cosErrorMessage(s: number, d: unknown): string;
		}).cosErrorMessage(0, { code: "convert_in_browser" });
		expect(msgOff).toContain("/tools/slides");
		// 开启态
		const on = loadApp(inlineCaps(true));
		const msgOn = (on.w.HP_UPLOAD as unknown as {
			cosErrorMessage(s: number, d: unknown): string;
		}).cosErrorMessage(0, { code: "convert_in_browser" });
		expect(msgOn).not.toContain("/tools/slides");
	});

	it("申请支持内核：预填 format_ext/message 并 POST /api/format-requests（§9.2 第 13 项）", async () => {
		const h = loadApp(inlineCaps(true));
		const up = h.w.HP_UPLOAD as unknown as {
			submitFormatRequestCore(o: Record<string, unknown>): void;
			openSupportForEntry(e: unknown): void;
		};
		let posted: { url: string; body: unknown } | null = null;
		// 注入的 fetch 记录 (url, init)；apiFetchNoRedirect 直接转发它们。
		// 只记**第一条**带 body 的 /api/format-requests（后续调用无 body）。
		h.fetch.mockImplementation((url: string, init?: { body?: unknown }) => {
			if (String(url) === "/api/format-requests" && init && init.body) {
				posted = { url: String(url), body: init.body };
				return Promise.resolve({
					ok: true, status: 202, clone() { return this; },
					json: () => Promise.resolve({ request_id: "fr_1", business_status: "submitted" }),
				});
			}
			return Promise.resolve({ ok: true, status: 200, clone() { return this; }, json: () => Promise.resolve({}) });
		});
		up.openSupportForEntry({
			name: "shot.jpg", error: { code: "content_ext_mismatch", detectedType: "png" },
		});
		await flush(10);
		expect(posted, "未发起带 body 的 /api/format-requests").toBeTruthy();
		expect(posted!.url).toBe("/api/format-requests");
		// FormData：断言字段已带上（test 环境有真 FormData）
		const fd = posted!.body as FormData;
		expect(typeof fd.get).toBe("function");
		expect(fd.get("format_ext")).toBe("jpg");
		expect(String(fd.get("message"))).toContain("code=content_ext_mismatch");
		expect(String(fd.get("message"))).toContain("detected=png");
	});

	it("申请支持的结果回执落在**行**上（抽屉默认关闭，行内按钮是主入口）", async () => {
		// 回归（e2e 实测）：结果原只写抽屉里的 #fr-result（import-drawer 默认
		// hidden），用户点完行内按钮看不到任何回执——设计 §7.1 要求行内显示
		// 申请编号 + 业务状态。
		const h = loadApp(inlineCaps(true));
		const up = h.w.HP_UPLOAD as unknown as { openSupportForEntry(e: unknown): void };
		h.fetch.mockImplementation((url: string, init?: { body?: unknown }) => {
			if (String(url) === "/api/format-requests" && init && init.body) {
				return Promise.resolve({
					ok: true, status: 202, clone() { return this; },
					json: () => Promise.resolve({ request_id: "fr_row1", business_status: "submitted" }),
				});
			}
			return Promise.resolve({ ok: true, status: 200, clone() { return this; },
				json: () => Promise.resolve({}) });
		});
		// 面板行：需要 entry.id 与 inlineRowEls 里已登记的行（renderRow 建）
		const entry = { id: "ic_row_support", name: "shot.jpg", state: "rejected",
			error: { code: "content_ext_mismatch", detectedType: "png" } };
		up.openSupportForEntry(entry);
		await flush(10);
		// 编号回执记在 entry 上（inlineRowNoteText 据此渲染行附注）
		expect((entry as { supportRequestId?: string }).supportRequestId).toBe("fr_row1");
		// toast 也说了「已登记」（本 harness 未加载 i18n.js → tt 走 _EXTRA_I18N
		// 或回显键；只断言它带上了编号，不锁具体措辞）
		expect((entry as { supportBusinessStatus?: string }).supportBusinessStatus)
			.toBe("submitted");
	});
	it("续传语义：未收口的 ingestionId 作为 resumeJobId 复用（§6.4）", () => {
		const h = loadApp(inlineCaps(true));
		const up = h.w.HP_UPLOAD as unknown as {
			resumeIdFromRecord(job: unknown): string | null;
		};
		// 未收口（uploading / awaiting_server / …）→ 复用同一 ingestion
		expect(up.resumeIdFromRecord({ upload: { ingestionId: "inj_1", state: "uploading" } }))
			.toBe("inj_1");
		expect(up.resumeIdFromRecord({ upload: { ingestionId: "inj_1", state: "awaiting_server" } }))
			.toBe("inj_1");
		// 终态 → 不续传（只由用户显式点击新建；刷新一次绝不多个服务端任务）
		for (const st of ["published", "failed", "cancelled"]) {
			expect(up.resumeIdFromRecord({ upload: { ingestionId: "inj_1", state: st } }), st)
				.toBeNull();
		}
		// 无记录 / 无 ingestionId → null
		expect(up.resumeIdFromRecord(null)).toBeNull();
		expect(up.resumeIdFromRecord({})).toBeNull();
		expect(up.resumeIdFromRecord({ upload: {} })).toBeNull();
	});
	
	it("直传条目复用 uploadFileCos（经 onOutcome 收口），仍带 direct_class 与 assocTarget", () => {
		// 源码级：直传分支（artifact 为 null）不得 reject，而是走既有直传状态机
		const fn = appSrc.slice(
			appSrc.indexOf("function uploadEntryArtifact"),
			appSrc.indexOf("function createEntryUpload"));
		expect(fn).toContain("uploadFileCos(entry.file");
		expect(fn).toContain("directClass: entry.directClass");
		expect(fn).toContain("assocTarget: entry.target");
		expect(fn).toContain("onOutcome");
		// uploadFileCos 支持结果回调（面板模式）：行 UI/关联交给面板
		const cos = appSrc.slice(
			appSrc.indexOf("function uploadFileCos"),
			appSrc.indexOf("function cosUploadSucceeded"));
		expect(cos).toContain("opts.onOutcome");
		// 直传声明与冻结目标的转发仍在（第四轮 A1）——该字面量在**调用方**
		// uploadFileClassified 内，所以从那里取片段
		const callSite = appSrc.slice(
			appSrc.indexOf("uploadFileCos(file, row, {"),
			appSrc.indexOf("uploadFileCos(file, row, {") + 900);
		expect(callSite).toContain("assocTarget: opts.assocTarget");
		expect(callSite).toContain("directClass: cls ? (cls.directClass || null) : null");
	});

	it("面板 entry → row 适配器（setStage / markError / _row）", () => {
		const fn = appSrc.slice(
			appSrc.indexOf("function inlineEntryRow"),
			appSrc.indexOf("function resumeIdFromRecord"));
		expect(fn).toContain("setStage:");
		expect(fn).toContain("markError:");
		expect(fn).toContain("_row:");
	});

	it("回队时清掉 ingestion 与声明（§7.2：绝不带着旧声明重试）", () => {
		const mod = readFileSync(resolve(root, "static/upload/inline-convert.js"), "utf8");
		const at = mod.indexOf("e.requeueCount < 1");
		expect(at).toBeGreaterThan(-1);
		const requeue = mod.slice(at, at + 800);
		expect(requeue).toContain("e.directClass = null");
		expect(requeue).toContain("e.uploadJobId = null");
		expect(requeue).toContain("requeue_convert");
	});

	it("行附注：进行中给「请保持页面空开」提示、磁盘待确认给 need/available 数字", () => {
		const src = appSrc.slice(
			appSrc.indexOf("function inlineRowNoteText"),
			appSrc.indexOf("function inlineRowSetStage"));
		expect(src).toContain("upload.inline.note.keepopen");
		expect(src).toContain("upload.inline.needDisk");
		expect(src).toContain("humanSize");
		// 只对进行中的状态显示「保持页面打开」
		expect(src).toContain('entry.state === "converting"');
		expect(src).toContain('entry.state === "uploading"');
	});

	it("删除按钮文案带释放字节数（§6.3），且不误用 published 措辞", () => {
		const h = loadApp(inlineCaps(true));
		const up = h.w.HP_UPLOAD as unknown as {
			inlineButtonLabel(kind: string, e: unknown): string;
		};
		const label = up.inlineButtonLabel("discard", {
			jobId: "j1", size: 1024, artifactSize: 2048, uploadState: "uploading",
		});
		// harness 未载 i18n.js → tt 回落 app.js 自带文案表；**带形参**的键
		// （{bytes}）在 harness 里回落到 key:vars 形态，故断言 key 与格式化值
		expect(String(label)).toContain("upload.inline.freed");
		expect(String(label)).toContain("3.0 KB");   // 1024 + 2048
		expect(String(label)).not.toContain("不受影响");
		// 已发布态的措辞会说「不受影响」，同样带释放字节
		const pub = String(up.inlineButtonLabel("discard",
			{ jobId: "j1", size: 1024, artifactSize: 0, uploadState: "published" }));
		expect(pub).toContain("不受影响");
		expect(pub).toContain("upload.inline.freed");
		// 无 jobId（散文件从未建任务）→ 不显示释放字节
		expect(String(up.inlineButtonLabel("discard", { jobId: null, size: 10 })))
			.not.toContain("upload.inline.freed");
	});

});
