/**
 * upload-liveness（0084）前端回归：
 *  1. 上传前置魔数校验（slide-sniff.verifyUploadHeader 经引擎
 *     sniffGuard）：PNG 改名 .jpg、JPEG 改名 .tif 等在**任何网络请求之前**
 *     被拒绝（typed {magicMismatch}，零 fetch/零 XHR）；真实 JPEG/TIFF
 *     正常放行走完整上传链路；
 *  2. headerMismatch/detectBytesKind 的类型识别矩阵（png/webp/gif/heic/
 *     tiff/bmp/unknown；不校验的扩展名恒放行）；
 *  3. 分块上传期间的客户端活性心跳：在途分块/批间空隙按至多 60s 一次
 *     GET 任务状态（服务端 client_seen_at 触点），节流窗内不发；
 *  4. i18n：client_gone / client_idle_timeout / validation(+fail_detail)
 *     / magic 新键 zh/en 词典成对存在（zh/en 必须给用户可读文案）。
 */
import { afterEach, describe, expect, it, vi } from "vitest";
import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const engineSrc = readFileSync(
	resolve(here, "../../static/upload/cos-uploader.js"), "utf8");
const sniffSrc = readFileSync(
	resolve(here, "../../static/upload/slide-sniff.js"), "utf8");
const i18nSrc = readFileSync(resolve(here, "../../static/i18n.js"), "utf8");

// ----------------------------------------------------------------------- //
// 假 XHR（COS PUT 载体；U1 合同：监听先于 send 注册）
// ----------------------------------------------------------------------- //
class FakeXHR {
	static instances: FakeXHR[] = [];
	static onSend: ((xhr: FakeXHR, url: string) => void) | null = null;
	method = "";
	url = "";
	body: unknown = null;
	status = 0;
	upload: Record<string, unknown> = {};
	setRequestHeader = vi.fn();
	getResponseHeader = vi.fn((_h: string) => null);
	open(method: string, url: string) { this.method = method; this.url = url; }
	send(body: unknown) {
		this.body = body;
		expect(this.upload.onprogress, "upload.onprogress before send")
			.toBeTypeOf("function");
		expect(this.onload, "onload before send").toBeTypeOf("function");
		FakeXHR.onSend?.(this, this.url);
	}
	respond(status: number, etag: string | null = null) {
		this.status = status;
		this.getResponseHeader = vi.fn((h: string) =>
			h.toLowerCase() === "etag" ? etag : null);
		this.onload?.();
	}
	onload: (() => void) | null = null;
	onerror: (() => void) | null = null;
	onabort: (() => void) | null = null;
	ontimeout: (() => void) | null = null;
	constructor() { FakeXHR.instances.push(this); }
}

function resp(body: unknown, status = 200) {
	return {
		ok: status >= 200 && status < 300,
		status,
		clone() { return this; },
		json: () => Promise.resolve(body),
		headers: { get: () => null },
	} as unknown as Response;
}

/** 带真实首部字节的假源：slice(0,16) 返回魔数，其余补零。 */
function bytesSource(name: string, head: number[], size = 32) {
	const slices: Array<{ start: number; end: number }> = [];
	const headBytes = Uint8Array.from(head);
	return {
		slices,
		view: {
			name,
			size,
			slice(start: number, end: number) {
				slices.push({ start, end });
				const len = end - start;
				const blob = {
					size: len,
					slice(a: number, b: number) {
						const sub = Math.min(b, len) - a;
						return {
							size: sub,
							arrayBuffer: async () => new ArrayBuffer(sub),
						};
					},
					arrayBuffer: async () => {
						const out = new ArrayBuffer(len);
						const u8 = new Uint8Array(out);
						for (let i = 0; i < len; i++) {
							const idx = start + i;
							u8[i] = idx < headBytes.length ? headBytes[idx] : 0;
						}
						return out;
					},
				};
				return blob;
			},
		},
	};
}

const JPEG_HEAD = [0xFF, 0xD8, 0xFF, 0xE0, 0x00, 0x10];
const PNG_HEAD = [0x89, 0x50, 0x4E, 0x47, 0x0D, 0x0A, 0x1A, 0x0A];
const TIFF_HEAD = [0x49, 0x49, 0x2A, 0x00, 0x08, 0x00, 0x00, 0x00];

/** 状态机 happy path：uploading(4 片) → sign → PUT → complete → viewable。
 *  GET 有态：首轮 uploading+parts，upload-complete 之后一律 viewable。 */
function happyFetch() {
	let completes = 0;
	return vi.fn((url: string, init?: RequestInit) => {
		const method = String((init && init.method) || "GET");
		if (url === "/api/ingestions" && method === "POST") {
			return Promise.resolve(resp({ job_id: "inj_x", stage: "uploading" }, 202));
		}
		if (url === "/api/ingestions/inj_x" && method === "GET") {
			if (completes === 0) {
				return Promise.resolve(resp({
					stage: "uploading",
					parts: [1, 2, 3, 4].map((n) => ({ part_number: n, length: 8 })),
				}));
			}
			return Promise.resolve(resp({ stage: "viewable", slide_id: "sld_1" }));
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
			completes++;
			return Promise.resolve(resp({ stage: "awaiting_server" }, 202));
		}
		if (url.startsWith("https://")) {
			return Promise.reject(new Error("COS PUT must use XHR (U1)"));
		}
		return Promise.resolve(resp({ stage: "viewable", slide_id: "sld_1" }));
	}) as unknown as typeof fetch;
}

function loadEngine(fetchImpl: typeof fetch) {
	const w: Record<string, unknown> = {
		fetch: fetchImpl,
		location: { href: "http://local/", pathname: "/tools/slides" },
	};
	(globalThis as { window?: unknown }).window = w;
	new Function("window", sniffSrc)(w);
	vi.stubGlobal("XMLHttpRequest", FakeXHR);
	new Function("window", "fetch", "location", engineSrc)(w, w.fetch, w.location);
	return {
		w,
		sniff: w.HP_SLIDE_SNIFF as {
			VERIFY_HEAD_BYTES: number;
			detectBytesKind: (u8: Uint8Array) => string | null;
			expectedKindForExt: (ext: string) => string | null;
			headerMismatch: (name: string, u8: Uint8Array) =>
				{ ext: string; expected: string; detected: string } | null;
			verifyUploadHeader: (name: string, src: unknown) =>
				Promise<{ ext: string; expected: string; detected: string } | null>;
		},
		engine: w.HP_COS_UPLOAD as {
			createUpload: (o: Record<string, unknown>) => {
				cancel: () => void;
				done: Promise<Record<string, unknown>>;
			};
		},
	};
}

const CFG = {
	available: true,
	max_size_bytes: 1000,
	part_bytes: 8,
	url_ttl_seconds: 600,
	max_concurrent_parts: 2,
	sign_batch_max_parts: 4,
	formats: ["tif", "jpg"],
};

function fakeStorage() {
	const completes: Array<[string, Record<string, unknown>]> = [];
	return {
		completes,
		adapter: {
			save() {},
			complete(id: string, outcome: Record<string, unknown>) {
				completes.push([id, outcome]);
			},
			remove() {},
			findResumable: () => null,
			readConfirmed: () => [] as number[],
			readRecord: () => Promise.resolve(null),
		},
	};
}

const REAL_TIMEOUT = setTimeout;
async function advYield(ms: number) {
	await vi.advanceTimersByTimeAsync(ms);
	await new Promise((r) => REAL_TIMEOUT(r, 0));
}

afterEach(() => {
	vi.useRealTimers();
	vi.unstubAllGlobals();
	FakeXHR.instances = [];
	FakeXHR.onSend = null;
});

// ----------------------------------------------------------------------- //
// 1) headerMismatch / detectBytesKind：类型识别矩阵
// ----------------------------------------------------------------------- //
describe("HP_SLIDE_SNIFF.headerMismatch（上传前置魔数校验）", () => {
	it("PNG/WebP/GIF/HEIC 改名 .jpg → 指出检测到的类型", async () => {
		const { sniff } = loadEngine(happyFetch());
		const webp = [0x52, 0x49, 0x46, 0x46, 0, 0, 0, 0,
			0x57, 0x45, 0x42, 0x50];
		const heic = [0x00, 0x00, 0x00, 0x18, 0x66, 0x74, 0x79, 0x70,
			0x68, 0x65, 0x69, 0x63];
		expect(sniff.headerMismatch("a.jpg", Uint8Array.from(PNG_HEAD)))
			.toEqual({ ext: ".jpg", expected: "jpeg", detected: "png" });
		expect(sniff.headerMismatch("a.jpeg", Uint8Array.from(webp)))
			.toEqual({ ext: ".jpeg", expected: "jpeg", detected: "webp" });
		expect(sniff.headerMismatch("a.jpg", Uint8Array.from(heic)))
			.toEqual({ ext: ".jpg", expected: "jpeg", detected: "heic" });
		expect(sniff.headerMismatch(
			"a.bmp", Uint8Array.from([0x47, 0x49, 0x46, 0x38, 0x39, 0x61])))
			.toEqual({ ext: ".bmp", expected: "bmp", detected: "gif" });
		// 认不出的内容：detected=unknown（仍然拒绝——魔数必不符）
		expect(sniff.headerMismatch("a.jpg", Uint8Array.from([1, 2, 3, 4])))
			.toEqual({ ext: ".jpg", expected: "jpeg", detected: "unknown" });
	});

	it("非 TIFF 内容改名 .tif → 指出检测到的类型；真实 TIFF/BigTIFF 放行", async () => {
		const { sniff } = loadEngine(happyFetch());
		// 真 BigTIFF：魔数 43（小端 II+\0 / 大端 MM\0+），不是经典 42。
		// 回归（复核修复）：本机转换器产物恒为 43（bigtiff.rs 写 43u16），
		// 旧夹具写成 MM\0*（经典 42）从未构造 43 —— 于是「BigTIFF 放行」
		// 的用例在 detectBytesKind 漏认 43 时仍是绿的，缺陷被掩盖。
		const bigtiffLE = [0x49, 0x49, 0x2B, 0x00, 8, 0, 0, 0, 0, 0, 0, 0];
		const bigtiffBE = [0x4D, 0x4D, 0x00, 0x2B, 0, 8, 0, 0, 0, 0, 0, 0];
		expect(sniff.headerMismatch("a.tif", Uint8Array.from(JPEG_HEAD)))
			.toEqual({ ext: ".tif", expected: "tiff", detected: "jpeg" });
		// 复合后缀按最后一段（.ome.tif → extOf=".tif"，同为 TIFF 族）
		expect(sniff.headerMismatch("b.ome.tif", Uint8Array.from(PNG_HEAD)))
			.toEqual({ ext: ".tif", expected: "tiff", detected: "png" });
		expect(sniff.headerMismatch("a.svs", Uint8Array.from(PNG_HEAD)))
			.toEqual({ ext: ".svs", expected: "tiff", detected: "png" });
		expect(sniff.headerMismatch("a.tif", Uint8Array.from(TIFF_HEAD)))
			.toBeNull();
		// 经典大端 42（原夹具的真实身份，保留为普通 TIFF 放行样本）
		expect(sniff.headerMismatch(
			"a.ome.tiff", Uint8Array.from([0x4D, 0x4D, 0x00, 0x2A, 0, 0, 0, 8])))
			.toBeNull();
		// BigTIFF 43 两种字节序都必须放行（转换器产出 .tif/.ome.tif 的形态）
		expect(sniff.detectBytesKind(Uint8Array.from(bigtiffLE))).toBe("tiff");
		expect(sniff.detectBytesKind(Uint8Array.from(bigtiffBE))).toBe("tiff");
		for (const name of ["a.tif", "a.tiff", "a.ome.tif", "b.ome.tiff",
			"a.bif", "a.scn", "a.ndpi", "a.svs"]) {
			expect(sniff.headerMismatch(name, Uint8Array.from(bigtiffLE)))
				.toBeNull();
			expect(sniff.headerMismatch(name, Uint8Array.from(bigtiffBE)))
				.toBeNull();
		}
	});

	it("真实 JPEG/BMP 放行；不校验的扩展名恒放行；短/读不到 → null", async () => {
		const { sniff } = loadEngine(happyFetch());
		const bmp = [0x42, 0x4D, 0x36, 0x84, 0x03, 0x00];
		expect(sniff.headerMismatch("a.jpg", Uint8Array.from(JPEG_HEAD)))
			.toBeNull();
		expect(sniff.headerMismatch("a.bmp", Uint8Array.from(bmp))).toBeNull();
		expect(sniff.headerMismatch("a.zip", Uint8Array.from(PNG_HEAD)))
			.toBeNull();
		expect(sniff.headerMismatch("noext", Uint8Array.from(PNG_HEAD)))
			.toBeNull();
		// 证据不足（< 4 字节）：不当拒绝依据（服务端终审）
		expect(sniff.headerMismatch("a.jpg", new Uint8Array(0))).toBeNull();
		expect(sniff.headerMismatch("a.jpg", Uint8Array.from([0x89, 0x50])))
			.toBeNull();
		expect(sniff.detectBytesKind(new Uint8Array([0x89, 0x50]))).toBeNull();
	});
});

// ----------------------------------------------------------------------- //
// 2) 引擎 sniffGuard：创建任务之前拒绝（零网络请求）/ 真实文件放行
// ----------------------------------------------------------------------- //
describe("createUpload sniffGuard（upload-liveness fail-fast）", () => {
	it("PNG 改名 .jpg：任何网络请求之前拒绝 {magicMismatch}，零 fetch/零 XHR", async () => {
		const fetchImpl = happyFetch();
		const { engine } = loadEngine(fetchImpl);
		const src = bytesSource("wechat-export.jpg", PNG_HEAD);
		const storage = fakeStorage();
		const upload = engine.createUpload({
			source: src.view, apiFetch: fetchImpl, config: CFG,
			storage: storage.adapter, onEvent: () => {},
		});
		await expect(upload.done).rejects.toMatchObject({
			magicMismatch: { ext: ".jpg", expected: "jpeg", detected: "png" },
		});
		expect((fetchImpl as unknown as vi.Mock).mock.calls.length).toBe(0);
		expect(FakeXHR.instances.length).toBe(0);
		expect(src.slices.length).toBeGreaterThan(0);   // 只读了首部窗口
		expect(src.slices[0]).toEqual({ start: 0, end: 16 });
	});

	it("JPEG 改名 .tif：零网络请求拒绝 {magicMismatch detected:jpeg}", async () => {
		const fetchImpl = happyFetch();
		const { engine } = loadEngine(fetchImpl);
		const src = bytesSource("renamed.tif", JPEG_HEAD);
		const upload = engine.createUpload({
			source: src.view, apiFetch: fetchImpl, config: CFG,
			storage: fakeStorage().adapter, onEvent: () => {},
		});
		await expect(upload.done).rejects.toMatchObject({
			magicMismatch: { ext: ".tif", expected: "tiff", detected: "jpeg" },
		});
		expect((fetchImpl as unknown as vi.Mock).mock.calls.length).toBe(0);
		expect(FakeXHR.instances.length).toBe(0);
	});

	it("真实 JPEG 走完整链路（创建→签名→PUT→complete→viewable）", async () => {
		const fetchImpl = happyFetch();
		const { engine } = loadEngine(fetchImpl);
		const src = bytesSource("photo.jpg", JPEG_HEAD);
		const storage = fakeStorage();
		const upload = engine.createUpload({
			source: src.view, apiFetch: fetchImpl, config: CFG,
			storage: storage.adapter, onEvent: () => {},
		});
		FakeXHR.onSend = (xhr) => { xhr.respond(200, '"etag-ok"'); };
		await expect(upload.done).resolves.toMatchObject(
			{ ok: true, body: { slide_id: "sld_1" } });
		expect((fetchImpl as unknown as vi.Mock).mock.calls.filter(
			([u]: [string]) => u === "/api/ingestions").length).toBe(1);
		expect(FakeXHR.instances.length).toBe(4);   // 4 片全部 PUT
		expect(storage.completes).toEqual([["inj_x", { succeeded: true, slide_id: "sld_1" }]]);
	});

	it("真实 TIFF 放行并完成上传", async () => {
		const fetchImpl = happyFetch();
		const { engine } = loadEngine(fetchImpl);
		const src = bytesSource("slide.tif", TIFF_HEAD);
		const upload = engine.createUpload({
			source: src.view, apiFetch: fetchImpl, config: CFG,
			storage: fakeStorage().adapter, onEvent: () => {},
		});
		FakeXHR.onSend = (xhr) => { xhr.respond(200, '"etag-ok"'); };
		await expect(upload.done).resolves.toMatchObject({ ok: true });
		expect(FakeXHR.instances.length).toBe(4);
	});

	// 回归（复核修复，high）：本机转换器「转换后上传」主链路的产物是
	// BigTIFF（魔数 43），旧 detectBytesKind 只认经典 42 → 这些文件在创建
	// ingestion **之前**被 mock 成 {magicMismatch detected:"unknown"} 拒掉，
	// 零网络请求，而服务端 open_slide 本会接受它们。夹具字节头取真实产物：
	// slide-transform gen-kfb + convert --profile bf-classic 产出
	// `49 49 2B 00 08 00 00 00`（bigtiff.rs:110 写 43u16）。
	const BIGTIFF_ARTIFACT_HEAD = [
		0x49, 0x49, 0x2B, 0x00, 0x08, 0x00, 0x00, 0x00,
		0x76, 0xDC, 0x12, 0x00, 0x00, 0x00, 0x00, 0x00];

	it("引擎产出（真实 engine.outputFileName 名）的 BigTIFF 产物走完整上传链路", async () => {
		// 页面生产路径（tools-slides-upload.js:797-803）：产物名经引擎
		// outputFileName 生成，.tif / .ome.tif / .bif / .ndpi 都属 TIFF 族。
		for (const name of ["bf-580x300.tif", "bf-580x300.ome.tif",
			"sample.bif", "sample.ndpi"]) {
			const fetchImpl = happyFetch();   // 每轮独立状态（completes 计数器）
			const { engine } = loadEngine(fetchImpl);
			const src = bytesSource(name, BIGTIFF_ARTIFACT_HEAD, 32);
			FakeXHR.instances = [];
			FakeXHR.onSend = (xhr) => { xhr.respond(200, '"etag-ok"'); };
			const upload = engine.createUpload({
				source: src.view, apiFetch: fetchImpl, config: CFG,
				storage: fakeStorage().adapter, onEvent: () => {},
			});
			await expect(upload.done).resolves.toMatchObject({ ok: true },
				`BigTIFF 产物 ${name} 被误拒（回归：漏认魔数 43）`);
			// 放行 = 真的建了任务并传完全部分块（而非零请求 fail-fast）
			expect((fetchImpl as unknown as vi.Mock).mock.calls.filter(
				([u, i]: [string, RequestInit?]) =>
					u === "/api/ingestions" && (i && i.method) === "POST").length)
				.toBe(1);
			expect(FakeXHR.instances.length).toBe(4);
		}
	});
});

// ----------------------------------------------------------------------- //
// 3) 分块上传期间的活性心跳（≤60s 一次 GET 任务状态）
// ----------------------------------------------------------------------- //
describe("createUpload 心跳（upload-liveness）", () => {
	it("分块在途：进度事件按至多 60s 一次触发状态 GET；节流窗内不发", async () => {
		vi.useFakeTimers();
		const fetchImpl = happyFetch();
		const { engine } = loadEngine(fetchImpl);
		const src = bytesSource("slow.tif", TIFF_HEAD);
		const upload = engine.createUpload({
			source: src.view, apiFetch: fetchImpl, config: CFG,
			storage: fakeStorage().adapter, onEvent: () => {},
			// 悬置在途（心跳节流的可观测前提）：超时上界调大——引擎对 0 值
			// 会回落默认 60s（`||` 兜底），不能传 0
			progressTimeoutMs: 3_600_000, partTimeoutMs: 3_600_000,
		});
		const held: FakeXHR[] = [];
		FakeXHR.onSend = (xhr) => { held.push(xhr); };   // PUT 永不响应
		const statusGets = () => (fetchImpl as unknown as vi.Mock).mock.calls
			.filter(([u, i]: [string, RequestInit?]) =>
				u === "/api/ingestions/inj_x" &&
				String((i && i.method) || "GET") === "GET").length;
		await advYield(0);
		// 上传已开始：drive 的状态查询 + 批边界的首个心跳（在场触点）
		expect(held.length).toBeGreaterThan(0);
		const c0 = statusGets();
		expect(c0).toBeGreaterThanOrEqual(2);
		// 节流窗内的进度事件：不再发心跳
		(held[0].upload.onprogress as (ev: unknown) => void)(
			{ loaded: 4, lengthComputable: true });
		expect(statusGets()).toBe(c0);
		// 61s 后的进度事件：恰好一次心跳
		await advYield(61_000);
		(held[0].upload.onprogress as (ev: unknown) => void)(
			{ loaded: 5, lengthComputable: true });
		expect(statusGets()).toBe(c0 + 1);
		// 30s 后：仍在节流窗内
		await advYield(30_000);
		(held[0].upload.onprogress as (ev: unknown) => void)(
			{ loaded: 6, lengthComputable: true });
		expect(statusGets()).toBe(c0 + 1);
		// 再 31s（距上次心跳 61s）：下一次心跳
		await advYield(31_000);
		(held[0].upload.onprogress as (ev: unknown) => void)(
			{ loaded: 7, lengthComputable: true });
		expect(statusGets()).toBe(c0 + 2);
		upload.cancel();
	});

	// 回归（复核修复，low）：心跳是活性探测，绝不能触发工作台 apiFetch 的
	// 401 → location.href="/login"（那会在分块上传中途导航走、丢掉在传行与
	// 本地产物）。心跳必须走 opts.heartbeatFetch（页面注入的不跳转 fetch）。
	it("心跳走 heartbeatFetch（不跳转 fetch），401 不触发 location.href 导航", async () => {
		vi.useFakeTimers();
		const primary = happyFetch();
		const beat = vi.fn((url: string) => {
			if (url === "/api/ingestions/inj_x") {
				// 401 auth_required：注入的「不跳转」fetch 原样返回，不读 body、
				// 不写 location（工作台 apiFetch 才会跳）。
				return Promise.resolve(resp({ error: "auth_required" }, 401));
			}
			return Promise.resolve(resp({}));
		}) as unknown as typeof fetch;
		const loc = { href: "http://local/app", pathname: "/app" };
		const w: Record<string, unknown> = { fetch: primary, location: loc };
		(globalThis as { window?: unknown }).window = w;
		new Function("window", sniffSrc)(w);
		vi.stubGlobal("XMLHttpRequest", FakeXHR);
		new Function("window", "fetch", "location", engineSrc)(w, primary, loc);
		const engine = w.HP_COS_UPLOAD as {
			createUpload: (o: Record<string, unknown>) => {
				cancel: () => void; done: Promise<Record<string, unknown>>;
			};
		};
		const src = bytesSource("slow.tif", TIFF_HEAD);
		const upload = engine.createUpload({
			source: src.view, apiFetch: primary, heartbeatFetch: beat,
			config: CFG, storage: fakeStorage().adapter, onEvent: () => {},
			progressTimeoutMs: 3_600_000, partTimeoutMs: 3_600_000,
		});
		const held: FakeXHR[] = [];
		FakeXHR.onSend = (xhr) => { held.push(xhr); };   // PUT 永不响应
		await advYield(0);
		expect(beat.mock.calls.length).toBeGreaterThanOrEqual(1);   // 首拍走注入面
		await advYield(61_000);
		(held[0].upload.onprogress as (ev: unknown) => void)(
			{ loaded: 5, lengthComputable: true });
		// 心跳只经 heartbeatFetch；主 apiFetch 收到的心跳 GET 为 0
		const primaryBeatGets = (primary as unknown as vi.Mock).mock.calls
			.filter(([u, i]: [string, RequestInit?]) =>
				u === "/api/ingestions/inj_x" &&
				String((i && i.method) || "GET") === "GET").length;
		// 首轮 drive 的状态查询经 apiFetch（1 次）；心跳不该再叠加
		expect(primaryBeatGets).toBe(1);
		expect(loc.href).toBe("http://local/app");   // 从未被导航
		upload.cancel();
	});

	// 回归（复核修复，low）：页面可见性恢复 → pokeLifecycle() 补一拍在场
	// 证据（心跳驱动源只有 JS 事件循环，挂起期间停摆）。
	it("pokeLifecycle()：节流窗外补一拍心跳；取消后是空操作", async () => {
		vi.useFakeTimers();
		const fetchImpl = happyFetch();
		const { engine } = loadEngine(fetchImpl);
		const src = bytesSource("slow.tif", TIFF_HEAD);
		const upload = engine.createUpload({
			source: src.view, apiFetch: fetchImpl, config: CFG,
			storage: fakeStorage().adapter, onEvent: () => {},
			progressTimeoutMs: 3_600_000, partTimeoutMs: 3_600_000,
		}) as unknown as { pokeLifecycle: () => void; cancel: () => void;
			done: Promise<Record<string, unknown>> };
		const held: FakeXHR[] = [];
		FakeXHR.onSend = (xhr) => { held.push(xhr); };
		const statusGets = () => (fetchImpl as unknown as vi.Mock).mock.calls
			.filter(([u, i]: [string, RequestInit?]) =>
				u === "/api/ingestions/inj_x" &&
				String((i && i.method) || "GET") === "GET").length;
		await advYield(0);
		const c0 = statusGets();
		// 节流窗内：poke 不叠加
		upload.pokeLifecycle();
		expect(statusGets()).toBe(c0);
		// 61s 后：poke 恰好补一拍（无需进度事件）
		await advYield(61_000);
		upload.pokeLifecycle();
		expect(statusGets()).toBe(c0 + 1);
		// 取消后：不再发任何拍
		upload.cancel();
		await advYield(120_000);
		upload.pokeLifecycle();
		expect(statusGets()).toBe(c0 + 1);
	});
});

// ----------------------------------------------------------------------- //
// 4) i18n：新键 zh/en 成对存在且为可读文案
// ----------------------------------------------------------------------- //
describe("i18n：upload-liveness 新键 zh/en 成对存在", () => {
	function dictEntries(lang: "zh" | "en"): Map<string, string> {
		const block = lang === "zh"
			? i18nSrc.slice(i18nSrc.indexOf("zh: {"), i18nSrc.indexOf("en: {"))
			: i18nSrc.slice(i18nSrc.indexOf("en: {"));
		const out = new Map<string, string>();
		const re = /"([^"\\]+)":\s*"((?:[^"\\]|\\.)*)"/g;
		let m: RegExpExecArray | null;
		while ((m = re.exec(block)) !== null) out.set(m[1], m[2]);
		return out;
	}

	const KEYS = [
		"upload.cos.err.client_gone",
		"upload.cos.err.client_idle_timeout",
		"upload.cos.err.validation",
		"upload.cos.err.raster_mismatch",
		"upload.cos.err.magic",
		"tools.upload.failed.client_gone",
		"tools.upload.failed.client_idle",
		"tools.upload.failed.raster_mismatch",
		"tools.upload.failed.magic",
	];

	it("client_gone / client_idle_timeout / fail_detail / magic 在 zh+en 词典都有可读文案", () => {
		const zh = dictEntries("zh");
		const en = dictEntries("en");
		for (const key of KEYS) {
			expect(zh.has(key), `zh 缺键 ${key}`).toBe(true);
			expect(en.has(key), `en 缺键 ${key}`).toBe(true);
			expect(zh.get(key)).not.toMatch(/\{code\}|undefined/);
			expect(en.get(key)).not.toBe(zh.get(key));
		}
		// 终态文案给出「重新上传/重新发起」指引
		expect(zh.get("upload.cos.err.client_gone")).toContain("重新");
		expect(zh.get("upload.cos.err.client_idle_timeout")).toContain("重新");
		expect(en.get("upload.cos.err.client_gone")!.toLowerCase())
			.toContain("again");
		// raster mismatch 文案点名 PNG/WebP 改名 .jpg 场景
		expect(zh.get("upload.cos.err.raster_mismatch")).toContain("PNG");
		expect(en.get("upload.cos.err.raster_mismatch")).toContain("PNG");
	});
});
