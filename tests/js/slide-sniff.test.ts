/**
 * slide-sniff 直传类别分类单元测试（static/upload/slide-sniff.js）。
 *
 * 先转换后上传阶段 1：分类是**分流提示**（服务端创建闸 + worker 头级核验
 * 才是权威）。这里锁定：
 *   1. 扩展名快路径：KFB/KFBF/MRXS 成员 → convert；NDPI → tiff 路由
 *      （F6 头解析分派）；VMS/VMU/SCN/BIF/SVSlide/BMP/JPEG/zip →
 *      temporary；未登记 → unsupported；
 *   2. TIFF 头解析（手工构造的最小 classic TIFF，≤128KB 头预算）：
 *      - ImageDescription 含 OME-XML → ome-tiff（direct_class=ome-tiff）；
 *      - 描述 JSON 带转换器来源标记（source_format ∈ 转换器词表）→
 *        converter-bigtiff；
 *      - .svs 第 0 层压缩 7（JPEG）→ convert；33003/33005（JPEG2000）→
 *        temporary + direct_class=unconverted-variant:svs-jp2k；
 *      - 普通 TIFF → temporary（legacy-direct）；
 *   3. 只按 slice 分块读头（绝不整体读取）；
 *   4. 命名只是提示：.ome.tif 命名但字节非 OME → temporary（不谎报 ome）。
 */
import { describe, expect, it } from "vitest";
import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const sniffSrc = readFileSync(
	resolve(here, "../../static/upload/slide-sniff.js"), "utf8");

const w: { HP_SLIDE_SNIFF?: {
	CLS: Record<string, string>;
	DIRECT_CLASS: Record<string, string>;
	classifyExt: (n: string) => { cls?: string; route?: string; ext?: string; bundle?: boolean };
	classifyTiffHead: (
		head: Uint8Array, more: { bytes: Uint8Array; baseOffset: number } | null,
		ext: string) => { cls: string; directClass: string; compression: number };
	classifyFile: (f: unknown) => Promise<{
		cls: string; directClass: string | null; ext: string;
		bundle: boolean; svsJp2k: boolean; compression: number;
	}>;
} } = {};
new Function("window", sniffSrc)(w);
const S = w.HP_SLIDE_SNIFF!;
expect(S).toBeTruthy();

/** 最小小端 classic TIFF（tag/typ/value + ASCII 描述；测试夹具）。 */
function classicTiff(
	entries: Array<[number, number, number]> = [],
	description = new Uint8Array(0),
): Uint8Array {
	const all = [...entries];
	if (description.length) all.push([270, 2, 0]);
	all.sort((a, b) => a[0] - b[0]);
	const header = new Uint8Array(8);
	header.set([0x49, 0x49], 0);                 // II
	new DataView(header.buffer).setUint16(2, 42, true);
	new DataView(header.buffer).setUint32(4, 8, true);
	const ifdSize = 2 + 12 * all.length + 4;
	const heapOffset = 8 + ifdSize;
	const heaps: Uint8Array[] = [];
	const ifd = new Uint8Array(ifdSize);
	new DataView(ifd.buffer).setUint16(0, all.length, true);
	let at = 2;
	for (const [tag, typ] of all) {
		const dv = new DataView(ifd.buffer);
		dv.setUint16(at, tag, true);
		dv.setUint16(at + 2, typ, true);
		if (typ === 2 && description.length) {
			dv.setUint32(at + 4, description.length, true);   // count
			if (description.length <= 4) {
				ifd.set(description.subarray(0, 4), at + 8);
			} else {
				dv.setUint32(at + 8, heapOffset +
					heaps.reduce((a, b) => a + b.length, 0), true);
				heaps.push(description);
			}
		} else {
			dv.setUint32(at + 4, 1, true);
			dv.setUint32(at + 8, all.find((e) => e[0] === tag)![2], true);
		}
		at += 12;
	}
	// 下一 IFD = 0（末 4 字节保持 0）
	const total = 8 + ifdSize + heaps.reduce((a, b) => a + b.length, 0);
	const out = new Uint8Array(total);
	out.set(header, 0);
	out.set(ifd, 8);
	let pos = 8 + ifdSize;
	for (const h of heaps) {
		out.set(h, pos);
		pos += h.length;
	}
	return out;
}

function descBytes(text: string): Uint8Array {
	const out = new Uint8Array(text.length);
	for (let i = 0; i < text.length; i++) out[i] = text.charCodeAt(i) & 0xff;
	return out;
}

const CONVERTER_DESC = descBytes(
	JSON.stringify({ source_format: "kfb_bf_v1", mpp_x: 0.5 }) + "\x00");
const OME_DESC = descBytes(
	'<?xml version="1.0"?><OME xmlns="http://www.openmicroscopy.org/' +
	'Schemas/OME/2016-06"></OME>\x00');
const PLAIN_DESC = descBytes("plain vendor description\x00");

/** 假 File：记录每次 slice 的请求区间（断言只读头、不整体读取）。 */
function fakeFile(name: string, bytes: Uint8Array) {
	const slices: Array<[number, number]> = [];
	return {
		name,
		size: bytes.length,
		slices,
		slice(start: number, end: number) {
			slices.push([start, Math.min(end, bytes.length)]);
			const clampedEnd = Math.min(end, bytes.length);
			const buf = bytes.buffer.slice(
				bytes.byteOffset + start, bytes.byteOffset + clampedEnd);
			return { arrayBuffer: async () => buf };
		},
	};
}

describe("slide-sniff：扩展名快路径（不读字节）", () => {
	it("浏览器转换器覆盖的格式 → convert（MRXS/VMS 成员带 bundle 标记）", () => {
		expect(S.classifyExt("a.KFB").cls).toBe("convert");
		expect(S.classifyExt("b.kfbf").cls).toBe("convert");
		expect(S.classifyExt("scan.mrxs").cls).toBe("convert");
		expect(S.classifyExt("scan.mrxs").bundle).toBe(true);
		expect(S.classifyExt("Data0000.dat").cls).toBe("convert");
		// VMS 转换器已覆盖：入口文件按 bundle 分流（整包经文件夹交接）
		expect(S.classifyExt("scan.vms").cls).toBe("convert");
		expect(S.classifyExt("scan.vms").bundle).toBe(true);
	});

	it("暂无浏览器转换器的格式/变体 → temporary（直传声明 legacy-direct）", () => {
		for (const n of ["a.vmu", "a.bif",
			"a.svslide", "a.bmp", "a.jpg", "a.jpeg"]) {
			const r = S.classifyExt(n);
			expect(r.cls, n).toBe("temporary");
			expect(r.directClass, n).toBe("legacy-direct");
		}
		// zip 是运输容器，不是切片直传类别：不带声明（回归——曾对每个 zip
		// 发 legacy-direct，服务端 422 invalid_direct_class）
		const zip = S.classifyExt("a.zip");
		expect(zip.cls).toBe("temporary");
		expect(zip.directClass).toBeUndefined();
	});

	it("未登记扩展名 → unsupported；TIFF 类 → route=tiff（需头解析）", () => {
		expect(S.classifyExt("a.xyz123").cls).toBe("unsupported");
		expect(S.classifyExt("noext").cls).toBe("unsupported");
		expect(S.classifyExt("a.tif").route).toBe("tiff");
		expect(S.classifyExt("a.svs").route).toBe("tiff");
		expect(S.classifyExt("a.ndpi").route).toBe("tiff");
		expect(S.classifyExt("a.ome.tif").route).toBe("tiff");
	});
});

describe("slide-sniff：Leica SCN（F4）", () => {
	const SCN_XML_HEAD =
		'<?xml version="1.0"?><scn xmlns="http://www.leica-microsystems.com/scn/2010/10/01">' +
		"<collection><image><pixels sizeX=\"100\" sizeY=\"100\">" +
		'<dimension sizeX="100" sizeY="100" r="0" ifd="0" /></pixels>' +
		"<view sizeX=\"50000\" sizeY=\"50000\" />" +
		"<scanSettings><illuminationSettings><illuminationSource>ILLUM" +
		"</illuminationSource></illuminationSettings></scanSettings>" +
		"</image></collection></scn>\x00";
	const scnDesc = (illum: string) =>
		descBytes(SCN_XML_HEAD.replace("ILLUM", illum));

	it("JPEG 编码明场 SCN → convert（需要转换）", async () => {
		const bytes = classicTiff([[259, 3, 7], [322, 3, 512], [323, 3, 512]],
			scnDesc("brightfield"));
		const r = await S.classifyFile(fakeFile("a.scn", bytes));
		expect(r.cls).toBe("convert");
		expect(r.ext).toBe(".scn");
	});

	it("荧光 SCN → temporary（暂时直传变体）", async () => {
		const bytes = classicTiff([[259, 3, 7], [322, 3, 512], [323, 3, 512]],
			scnDesc("fluorescence"));
		const r = await S.classifyFile(fakeFile("b.scn", bytes));
		expect(r.cls).toBe("temporary");
		expect(r.directClass).toBe("legacy-direct");
	});

	it("非 JPEG 编码 SCN（压缩 8）→ temporary（暂时直传变体）", async () => {
		const bytes = classicTiff([[259, 3, 8], [322, 3, 512], [323, 3, 512]],
			scnDesc("brightfield"));
		const r = await S.classifyFile(fakeFile("c.scn", bytes));
		expect(r.cls).toBe("temporary");
	});

	it("描述不是 SCN XML 的 .scn → temporary", async () => {
		const bytes = classicTiff([[259, 3, 7]], PLAIN_DESC);
		const r = await S.classifyFile(fakeFile("d.scn", bytes));
		expect(r.cls).toBe("temporary");
	});
});

describe("slide-sniff：TIFF 头解析（手工夹具，≤128KB 预算）", () => {
	it("ImageDescription 含 OME-XML → ome-tiff + direct_class=ome-tiff", async () => {
		const bytes = classicTiff([], OME_DESC);
		const r = await S.classifyFile(fakeFile("a.tif", bytes));
		expect(r.cls).toBe("ome-tiff");
		expect(r.directClass).toBe("ome-tiff");
	});

	it("描述 JSON 带转换器来源标记 → converter-bigtiff（六种来源）", async () => {
		for (const sf of ["kfb_bf_v1", "kfb_kfbio_jpeg", "aperio-svs-jpeg",
			"leica-scn-jpeg", "mirax-bundle", "generic-tiled-jpeg-tiff"]) {
			const bytes = classicTiff([], descBytes(
				JSON.stringify({ source_format: sf }) + "\x00"));
			const r = await S.classifyFile(fakeFile("out.tif", bytes));
			expect(r.cls, sf).toBe("converter-bigtiff");
			expect(r.directClass, sf).toBe("converter-bigtiff");
		}
	});

	it("外部工具写的普通描述（无来源标记）→ temporary", async () => {
		const r = await S.classifyFile(
			fakeFile("ext.tif", classicTiff([], PLAIN_DESC)));
		expect(r.cls).toBe("temporary");
		expect(r.directClass).toBe("legacy-direct");
	});

	it("JPEG 编码 SVS（压缩 7）→ convert（本机转换）", async () => {
		const r = await S.classifyFile(
			fakeFile("aperio.svs", classicTiff([[259, 3, 7]])));
		expect(r.cls).toBe("convert");
	});

	it("JPEG2000 编码 SVS（33003/33005）→ temporary + svs-jp2k 声明例外", async () => {
		for (const comp of [33003, 33005]) {
			const r = await S.classifyFile(
				fakeFile("jp2k.svs", classicTiff([[259, 3, comp]])));
			expect(r.cls, String(comp)).toBe("temporary");
			expect(r.directClass, String(comp))
				.toBe("unconverted-variant:svs-jp2k");
			expect(r.svsJp2k, String(comp)).toBe(true);
		}
	});

	it("普通 TIFF（无描述无压缩声明）→ temporary（legacy-direct）", async () => {
		const r = await S.classifyFile(fakeFile("plain.tif", classicTiff()));
		expect(r.cls).toBe("temporary");
		expect(r.directClass).toBe("legacy-direct");
	});

	it("命名只是提示：.ome.tif 命名但字节非 OME → temporary（不谎报 ome）", async () => {
		const r = await S.classifyFile(
			fakeFile("fake.ome.tif", classicTiff([], PLAIN_DESC)));
		expect(r.cls).toBe("temporary");
		expect(r.directClass).toBe("legacy-direct");
	});

	it("只读文件头（≤128KB×2 的分块），绝不整体读取大文件", async () => {
		const big = new Uint8Array(8 * 1024 * 1024);   // 8 MiB 「金字塔数据」
		big.set(classicTiff(), 0);
		const f = fakeFile("big.tif", big);
		const r = await S.classifyFile(f);
		expect(r.cls).toBe("temporary");
		for (const [, end] of f.slices) {
			expect(end).toBeLessThanOrEqual(128 * 1024 * 2 + 1);
		}
	});

	it("非 TIFF 字节按扩展名降级 temporary（服务端终审）", async () => {
		const junk = new Uint8Array(64);
		junk.set([0x50, 0x4b, 0x03, 0x04]);   // zip 魔数（伪装 .tif）
		const r = await S.classifyFile(fakeFile("junk.tif", junk));
		expect(r.cls).toBe("temporary");
	});
});
