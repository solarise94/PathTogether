/**
 * F6 Hamamatsu NDPI 输入侧约定（engine.js + slide-sniff.js）：
 *
 *  - Make（271）标识 Hamamatsu + 整层单条带 + 基线 JPEG + 3 samples +
 *    photo 2/6 的经典 TIFF → NDPI 适配器（hamamatsu-ndpi-jpeg；
 *    restart marker / 层级分类 / >4GiB 扩展由 wasm 核心复制后终审）；
 *  - 分块存储不是 NDPI 布局；JPEG 2000 / 非 JPEG 编码 / 多通道 / 平面
 *    存储 / 非 {2,6} photometric 在结构门槛被拒（复制前类型化拒绝）；
 *  - BigTIFF 容器不是 NDPI 输入（NDPI 恒为经典 TIFF 42）；
 *  - 非 Hamamatsu 的 Make 不进 NDPI 适配器（无描述 + tiled 的结构门槛
 *    未过 → 通用 TIFF 适配器的条带变体拒绝路径）；
 *  - `outputFileName` 剥掉 .ndpi；适配器常量与核心一致（v1 / l0-box2）；
 *  - slide-sniff：.ndpi 从「暂时直传」改为「需要转换」——Make + 条带 +
 *    JPEG 明场 → convert；JP2K 变体 → temporary（legacy-direct）。
 */
import { describe, expect, it } from "vitest";
import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
// eslint-disable-next-line
import * as E from "../../static/tools/slide-transform/engine.js";

// ---- minimal in-memory classic-TIFF builder with Make + strip tags ------ //

function buildNdpi({
	little = true,
	bigtiff = false,
	tiled = false,
	compression = 7,
	samples = 3,
	planar = 1,
	photo = 6,
	make = "Hamamatsu",
	strips = true,
} = {}) {
	const le = little;
	const u16 = (v: number) => {
		const b = [(v >> 8) & 0xff, v & 0xff];
		return le ? [b[1], b[0]] : b;
	};
	const u32 = (v: number) => {
		const b = [(v >>> 24) & 0xff, (v >>> 16) & 0xff, (v >>> 8) & 0xff, v & 0xff];
		return le ? [b[3], b[2], b[1], b[0]] : b;
	};
	const u64 = (v: number) => {
		const lo = v >>> 0;
		const hi = Math.floor(v / 2 ** 32) >>> 0;
		return le ? [...u32(lo), ...u32(hi)] : [...u32(hi), ...u32(lo)];
	};
	const makeBytes = [...new TextEncoder().encode(make), 0];
	const hdrLen = bigtiff ? 16 : 8;
	const makeAt = hdrLen;
	const ifdAt = makeAt + makeBytes.length;
	const entries: number[][] = [];
	const push = (tag: number, typ: number, count: number, val: number[]) =>
		entries.push([...u16(tag), ...u16(typ), ...(bigtiff ? [...u64(count)] : [...u32(count)]), ...val]);
	const inline = (bytes: number[]) => {
		const v = bigtiff ? 8 : 4;
		return [...bytes, ...new Array(v - bytes.length).fill(0)].slice(0, v);
	};
	push(256, 4, 1, inline(u32(512)));
	push(257, 4, 1, inline(u32(384)));
	push(259, 3, 1, inline(u16(compression)));
	push(262, 3, 1, inline(u16(photo)));
	push(271, 2, makeBytes.length, bigtiff ? u64(makeAt) : u32(makeAt));
	if (strips) {
		push(273, 4, 1, inline(u32(8))); // StripOffsets
	}
	push(277, 3, 1, inline(u16(samples)));
	push(278, 4, 1, inline(u32(384))); // RowsPerStrip = height
	if (strips) {
		push(279, 4, 1, inline(u32(4096))); // StripByteCounts
	}
	push(284, 3, 1, inline(u16(planar)));
	if (tiled) {
		push(322, 3, 1, inline(u16(256)));
		push(323, 3, 1, inline(u16(256)));
	}
	entries.sort((a, b) => (a[0] < b[0] ? -1 : 1));
	const esize = bigtiff ? 20 : 12;
	const bytes: number[] = [];
	if (bigtiff) {
		bytes.push(...(le ? [0x49, 0x49] : [0x4d, 0x4d]), ...u16(43), ...u16(8), ...u16(0));
		bytes.push(...u64(ifdAt));
	} else {
		bytes.push(...(le ? [0x49, 0x49] : [0x4d, 0x4d]), ...u16(42));
		bytes.push(...u32(ifdAt));
	}
	while (bytes.length < hdrLen) bytes.push(0);
	bytes.push(...makeBytes, ...new Array(ifdAt - hdrLen - makeBytes.length).fill(0));
	if (bigtiff) bytes.push(...u64(entries.length));
	else bytes.push(...u16(entries.length));
	for (const e of entries) bytes.push(...e, ...new Array(esize - e.length).fill(0));
	if (bigtiff) bytes.push(...u64(0));
	else bytes.push(...u32(0));
	return new Uint8Array(bytes);
}

const asFile = (u8: Uint8Array, name = "x.ndpi") => new File([u8], name);

describe("NDPI sniff（有界 staging 前探测）", () => {
	it("Hamamatsu Make + 整层单条带 + JPEG 明场 → supported，适配器 hamamatsu-ndpi-jpeg", async () => {
		for (const opts of [{}, { little: false }]) {
			const cap = await E.sniffTiffSlideCapability(asFile(buildNdpi(opts)));
			expect(cap, JSON.stringify(opts)).toMatchObject({
				supported: true,
				modality: "brightfield",
				format: "hamamatsu-ndpi-jpeg",
				adapter: "hamamatsu-ndpi-jpeg",
			});
		}
	});

	it("BigTIFF 容器 → NDPI 是经典 TIFF，类型化拒绝", async () => {
		const cap = await E.sniffTiffSlideCapability(asFile(buildNdpi({ bigtiff: true })));
		expect(cap.supported).toBe(false);
		expect(cap.reason).toContain("BigTIFF");
	});

	it("分块存储不是 NDPI 布局 → 拒绝", async () => {
		const cap = await E.sniffTiffSlideCapability(asFile(buildNdpi({ tiled: true })));
		expect(cap.supported).toBe(false);
		expect(cap.reason).toContain("NDPI 布局");
	});

	it("JPEG 2000 / 非 JPEG 编码 → 拒绝", async () => {
		for (const compression of [33003, 33005, 5, 8]) {
			const cap = await E.sniffTiffSlideCapability(
				asFile(buildNdpi({ compression }), `x-${compression}.ndpi`));
			expect(cap.supported, String(compression)).toBe(false);
			expect(cap.reason, String(compression)).toContain(
				compression === 33003 || compression === 33005 ? "JPEG 2000" : "基线 JPEG");
		}
	});

	it("多通道 / 平面存储 / photometric 不在 {2,6} → 结构拒绝", async () => {
		const spp = await E.sniffTiffSlideCapability(asFile(buildNdpi({ samples: 1 })));
		expect(spp.supported).toBe(false);
		expect(spp.reason).toContain("SamplesPerPixel=1");
		const planar = await E.sniffTiffSlideCapability(asFile(buildNdpi({ planar: 2 })));
		expect(planar.supported).toBe(false);
		expect(planar.reason).toContain("平面存储");
		const photo = await E.sniffTiffSlideCapability(asFile(buildNdpi({ photo: 5 })));
		expect(photo.supported).toBe(false);
		expect(photo.reason).toContain("PhotometricInterpretation=5");
	});

	it("非 Hamamatsu 的 Make 不进 NDPI 适配器（条带 + 无 tile → 通用适配器拒绝）", async () => {
		const cap = await E.sniffTiffSlideCapability(
			asFile(buildNdpi({ make: "OtherScanner" })));
		expect(cap.supported).toBe(false);
		expect(cap.reason).toContain("tiled");
	});
});

describe("NDPI 命名与常量", () => {
	it("outputFileName 剥掉 .ndpi 并按 profile 命名", () => {
		const ome = { outputProfile: E.OUTPUT_PROFILES.BF_OME };
		const classic = { outputProfile: E.OUTPUT_PROFILES.BF_CLASSIC };
		expect(E.outputFileName("CMU-1.ndpi", ome)).toBe("CMU-1.ome.tif");
		expect(E.outputFileName("CMU-1.ndpi", classic)).toBe("CMU-1.tif");
	});

	it("适配器常量与核心一致（v1 / l0-box2）", () => {
		expect(E.NDPI_SOURCE_ADAPTER).toBe("hamamatsu-ndpi-jpeg");
		expect(E.NDPI_ADAPTER_VERSION).toBe("1");
		expect(E.NDPI_PYRAMID_METHOD).toBe("l0-box2");
	});
});

// ---- slide-sniff：.ndpi 从「暂时直传」改为「需要转换」 -------------------- //

const here = dirname(fileURLToPath(import.meta.url));
const sniffSrc = readFileSync(resolve(here, "../../static/upload/slide-sniff.js"), "utf8");
const w: { HP_SLIDE_SNIFF?: {
	CLS: Record<string, string>;
	DIRECT_CLASS: Record<string, string>;
	classifyExt: (n: string) => { cls?: string; route?: string; ext?: string };
	classifyTiffHead: (
		head: Uint8Array, more: { bytes: Uint8Array; baseOffset: number } | null,
		ext: string) => { cls: string; directClass: string | null; compression: number };
} } = {};
new Function("window", sniffSrc)(w);
const S = w.HP_SLIDE_SNIFF!;

describe("slide-sniff：NDPI 分流（F6）", () => {
	it("扩展名快路径：.ndpi → route=tiff（头解析分派）", () => {
		expect(S.classifyExt("scan.ndpi").route).toBe("tiff");
		expect(S.classifyExt("scan.ndpi").cls).toBeUndefined();
	});

	it("Hamamatsu + 条带 + JPEG 明场 → convert", () => {
		const head = buildNdpi({});
		const r = S.classifyTiffHead(head, null, ".ndpi");
		expect(r.cls).toBe(S.CLS.CONVERT);
		expect(r.directClass).toBeNull();
	});

	it("JPEG 2000 变体 → temporary（暂时直传，legacy-direct）", () => {
		const head = buildNdpi({ compression: 33005 });
		const r = S.classifyTiffHead(head, null, ".ndpi");
		expect(r.cls).toBe(S.CLS.TEMPORARY);
		expect(r.directClass).toBe(S.DIRECT_CLASS.LEGACY);
	});

	it("多通道变体 → temporary", () => {
		const head = buildNdpi({ samples: 1 });
		const r = S.classifyTiffHead(head, null, ".ndpi");
		expect(r.cls).toBe(S.CLS.TEMPORARY);
	});
});
