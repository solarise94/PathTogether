/**
 * F8 普通图片（BMP/JPEG）输入侧约定（engine.js + slide-sniff.js）：
 *
 *  - BMP（BM）与基线 JPEG（FF D8 FF）按魔数进 raster 适配器
 *    （plain-image-bmp-jpeg；变体终审由 wasm 核心复制后给出）；
 *  - 复制前嗅探（sniffRasterCapability）拒绝：RLE / BITFIELDS /
 *    JPEG/PNG-in-BMP、调色板与 16 位位深、未知 DIB 头、渐进 / 算术 /
 *    灰度 JPEG、像素上限超限——不进 OPFS；
 *  - 24/32 位未压缩 BMP（含 top-down、OS/2 core header）与三分量基线
 *    JPEG → supported；
 *  - `outputFileName` 剥掉 .bmp/.jpg/.jpeg；适配器常量与核心一致
 *    （v1 / l0-box2 / raster-compose 指纹 / 像素上限）；
 *  - 转换器产物（BigTIFF 描述 JSON 带 plain-image-bmp-jpeg 来源标记）
 *    不是转换输入（converter-bigtiff 直传）；
 *  - slide-sniff：.bmp/.jpg/.jpeg 从「暂时直传」改为「需要转换」——
 *    头解析分派（classifyRasterHead），变体 → temporary。
 */
import { describe, expect, it } from "vitest";
import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
// eslint-disable-next-line
import * as E from "../../static/tools/slide-transform/engine.js";

const here = dirname(fileURLToPath(import.meta.url));
const sniffSrc = readFileSync(
	resolve(here, "../../static/upload/slide-sniff.js"), "utf8");

// ---- helpers ------------------------------------------------------------ //

function fileOf(bytes: Uint8Array, name = "in.bin"): File {
	return new File([bytes.slice().buffer as ArrayBuffer], name, {
		type: "application/octet-stream",
	});
}

function bmpBytes({
	dib = 40,
	bpp = 24,
	compression = 0,
	width = 512,
	height = 320,
	topDown = false,
} = {}): Uint8Array {
	const head = new Uint8Array(14 + (dib === 12 ? 12 : 40));
	const dv = new DataView(head.buffer);
	head[0] = 0x42;
	head[1] = 0x4d;
	dv.setUint32(10, 14 + (dib === 12 ? 12 : 40), true); // data offset
	dv.setUint32(14, dib, true);
	if (dib === 12) {
		dv.setUint16(18, Math.min(width, 65535), true);
		dv.setUint16(20, height, true);
		dv.setUint16(22, 1, true);
		dv.setUint16(24, bpp, true);
	} else {
		dv.setInt32(18, width, true);
		dv.setInt32(22, topDown ? -height : height, true);
		dv.setUint16(26, 1, true);
		dv.setUint16(28, bpp, true);
		dv.setUint32(30, compression, true);
		dv.setUint32(34, 0, true);
	}
	return head;
}

/** 基线 JPEG 头桩：JFIF APP0 + DQT + SOF0/SOF2 + DHT + SOS（熵数据不 Needed）。 */
function jpegBytes({ progressive = false, ncomp = 3, width = 64, height = 48 } = {}): Uint8Array {
	const out: number[] = [0xff, 0xd8];
	// APP0 JFIF
	out.push(0xff, 0xe0, 0, 16, 0x4a, 0x46, 0x49, 0x46, 0, 1, 1, 0, 0, 1, 0, 1, 0, 0);
	// DQT (dummy all-1 table, id 0)
	out.push(0xff, 0xdb, 0, 67, 0, ...new Array(64).fill(1));
	// SOF
	const sofLen = ncomp === 3 ? 17 : 11;
	out.push(0xff, progressive ? 0xc2 : 0xc0, (sofLen >> 8) & 0xff, sofLen & 0xff, 8,
		(height >> 8) & 0xff, height & 0xff, (width >> 8) & 0xff, width & 0xff, ncomp);
	if (ncomp === 3) {
		out.push(1, 0x22, 0, 2, 0x11, 1, 3, 0x11, 1);
	} else {
		out.push(1, 0x11, 0);
	}
	// DHT (dummy: one count table)
	out.push(0xff, 0xc4, 0, 31, 0, ...new Array(16).fill(0), ...new Array(12).fill(0));
	// SOS
	out.push(0xff, 0xda, 0, 4, 1, 0);
	// a few entropy bytes + EOI
	out.push(0x12, 0x34, 0xff, 0xd9);
	return new Uint8Array(out);
}

// ---- engine sniff（有界 staging 前探测） -------------------------------- //

describe("raster sniff（有界 staging 前探测）", () => {
	it("未压缩 24/32 位 BMP → supported（适配器 plain-image-bmp-jpeg）", async () => {
		for (const bpp of [24, 32]) {
			const cap = await E.sniffRasterCapability(fileOf(bmpBytes({ bpp })));
			expect(cap.supported, `bpp=${bpp}`).toBe(true);
			expect(cap.modality).toBe("brightfield");
			expect(cap.format).toBe("plain-image-bmp-jpeg");
			expect(cap.adapter).toBe("plain-image-bmp-jpeg");
		}
	});

	it("BMP top-down 与 OS/2 core header → supported", async () => {
		for (const opts of [{ topDown: true }, { dib: 12 }, { dib: 124 }]) {
			const cap = await E.sniffRasterCapability(fileOf(bmpBytes(opts)));
			expect(cap.supported, JSON.stringify(opts)).toBe(true);
		}
	});

	it("RLE / BITFIELDS / PNG-in-BMP → 类型化拒绝（复制前）", async () => {
		for (const [compression, frag] of [
			[1, "RLE"],
			[3, "BITFIELDS"],
			[5, "PNG"],
		] as const) {
			const cap = await E.sniffRasterCapability(fileOf(bmpBytes({ compression })));
			expect(cap.supported, String(compression)).toBe(false);
			expect(String(cap.reason)).toContain(frag);
			expect(String(cap.reason)).toContain("不在支持集");
		}
	});

	it("调色板 / 16 位位深与未知 DIB 头 → 拒绝", async () => {
		for (const bpp of [1, 4, 8, 16]) {
			const cap = await E.sniffRasterCapability(fileOf(bmpBytes({ bpp })));
			expect(cap.supported, `bpp=${bpp}`).toBe(false);
			expect(String(cap.reason)).toContain("位深");
		}
		const weird = await E.sniffRasterCapability(fileOf(bmpBytes({ dib: 999 })));
		expect(weird.supported).toBe(false);
		expect(String(weird.reason)).toContain("未知 DIB 头");
	});

	it("像素上限超限 → 复制前拒绝", async () => {
		const cap = await E.sniffRasterCapability(
			fileOf(bmpBytes({ width: 100000, height: 100000 })));
		expect(cap.supported).toBe(false);
		expect(String(cap.reason)).toContain("上限");
	});

	it("三分量基线 JPEG → supported；渐进 / 灰度 → 拒绝", async () => {
		const ok = await E.sniffRasterCapability(fileOf(jpegBytes()));
		expect(ok.supported).toBe(true);
		expect(ok.adapter).toBe("plain-image-bmp-jpeg");
		const prog = await E.sniffRasterCapability(fileOf(jpegBytes({ progressive: true })));
		expect(prog.supported).toBe(false);
		expect(String(prog.reason)).toContain("渐进");
		const gray = await E.sniffRasterCapability(fileOf(jpegBytes({ ncomp: 1 })));
		expect(gray.supported).toBe(false);
		expect(String(gray.reason)).toContain("灰度");
	});

	it("魔数分派：isRasterHeader / magicModality / magicSupported", () => {
		expect(E.isRasterHeader(new Uint8Array([0x42, 0x4d, 0, 0]))).toBe(true);
		expect(E.isRasterHeader(new Uint8Array([0xff, 0xd8, 0xff, 0xe0]))).toBe(true);
		expect(E.isRasterHeader(new Uint8Array([0x49, 0x49, 42, 0]))).toBe(false);
		expect(E.magicModality(new Uint8Array([0x42, 0x4d, 0, 0]))).toBe("brightfield");
		expect(E.magicModality(new Uint8Array([0xff, 0xd8, 0xff, 0xe0]))).toBe("brightfield");
		expect(E.magicSupported(new Uint8Array([0x42, 0x4d, 0, 0, 0, 0, 0, 0]))).toBe(true);
		expect(E.magicSupported(new Uint8Array([0xff, 0xd8, 0xff, 0, 0, 0, 0, 0]))).toBe(true);
	});
});

// ---- 命名与常量 ---------------------------------------------------------- //

describe("raster 命名与常量", () => {
	it("outputFileName 剥掉 .bmp/.jpg/.jpeg 并按 profile 命名", () => {
		const ome = { outputProfile: E.OUTPUT_PROFILES.BF_OME } as never;
		const classic = { outputProfile: E.OUTPUT_PROFILES.BF_CLASSIC } as never;
		for (const n of ["a.bmp", "b.jpg", "c.jpeg", "d.BMP"]) {
			expect(E.outputFileName(n, ome)).toBe(`${n.replace(/\.[^.]+$/, "")}.ome.tif`);
			expect(E.outputFileName(n, classic)).toBe(`${n.replace(/\.[^.]+$/, "")}.tif`);
		}
	});

	it("适配器常量与核心一致（v1 / l0-box2 / compose 指纹 / 像素上限）", () => {
		expect(E.RASTER_SOURCE_ADAPTER).toBe("plain-image-bmp-jpeg");
		expect(E.RASTER_ADAPTER_VERSION).toBe("1");
		expect(E.RASTER_PYRAMID_METHOD).toBe("l0-box2");
		expect(E.RASTER_PRESERVE_COMPOSE_FINGERPRINT).toBe("raster-compose:q96:y422:hstd:v1");
		expect(E.RASTER_MAX_SIDE).toBe(1_000_000);
		expect(E.RASTER_MAX_PIXELS).toBe(2 ** 32);
	});

	it("转换器产物词表含 plain-image-bmp-jpeg（输出 BigTIFF 不再是转换输入）", () => {
		expect(E.CONVERTER_SOURCE_FORMATS).toContain("plain-image-bmp-jpeg");
	});
});

// ---- slide-sniff 分流 ---------------------------------------------------- //

describe("slide-sniff：普通图片分流（F8）", () => {
	const w: { HP_SLIDE_SNIFF?: {
		CLS: Record<string, string>;
		DIRECT_CLASS: Record<string, string>;
		classifyExt: (n: string) => { cls?: string; route?: string; ext?: string; bundle?: boolean };
		classifyRasterHead: (b: Uint8Array) => { cls: string; directClass: string | null };
		CONVERTER_SOURCE_FORMATS: Record<string, number>;
	} } = {};
	new Function("window", sniffSrc)(w);
	const S = w.HP_SLIDE_SNIFF!;

	it("扩展名快路径：.bmp/.jpg/.jpeg → route=raster（头解析分派）", () => {
		for (const n of ["a.bmp", "b.JPG", "c.jpeg"]) {
			const r = S.classifyExt(n);
			expect(r.cls, n).toBeUndefined();
			expect(r.route, n).toBe("raster");
		}
	});

	it("classifyRasterHead：未压缩 24/32 位 BMP 与三分量基线 JPEG → convert", () => {
		expect(S.classifyRasterHead(bmpBytes({ bpp: 24 })).cls).toBe("convert");
		expect(S.classifyRasterHead(bmpBytes({ bpp: 32 })).cls).toBe("convert");
		expect(S.classifyRasterHead(bmpBytes({ dib: 12, bpp: 24 })).cls).toBe("convert");
		// 变体：RLE / 位域 / 调色板位深 / 未知 DIB → temporary（legacy-direct）
		expect(S.classifyRasterHead(bmpBytes({ compression: 1 })).cls).toBe("temporary");
		expect(S.classifyRasterHead(bmpBytes({ bpp: 4 })).cls).toBe("temporary");
		expect(S.classifyRasterHead(bmpBytes({ dib: 999 })).cls).toBe("temporary");
		// JPEG：三分量基线 → convert；灰度/渐进 → temporary
		expect(S.classifyRasterHead(jpegBytes()).cls).toBe("convert");
		expect(S.classifyRasterHead(jpegBytes({ ncomp: 1 })).cls).toBe("temporary");
		expect(S.classifyRasterHead(jpegBytes({ progressive: true })).cls).toBe("temporary");
		// 魔数不符 → temporary
		expect(S.classifyRasterHead(new Uint8Array([0x49, 0x49, 42, 0])).cls).toBe("temporary");
	});

	it("转换器产物词表与 engine/服务端同源（plain-image-bmp-jpeg）", () => {
		expect(S.CONVERTER_SOURCE_FORMATS["plain-image-bmp-jpeg"]).toBe(1);
	});
});
