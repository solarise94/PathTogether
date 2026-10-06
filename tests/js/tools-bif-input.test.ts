/**
 * Ventana BIF 输入侧约定（engine.js + slide-sniff.js）：
 *
 *  - BigTIFF + IFD 0 XMLPacket（700）携带 iScan 厂商块（描述是
 *    "Label Image"、无 Make）→ BIF 适配器（ventana-bif-jpeg；重叠瓦片拼
 *    接 / EncodeInfo 终审由 wasm 核心复制后给）；
 *  - 经典 TIFF 容器不是 BIF 输入（ventana tif 变体，类型化拒绝）；
 *  - 无 iScan 块的 BigTIFF 不进 BIF 适配器（回落到既有分派：tiled JPEG
 *    的结构门槛 / 通用 TIFF）；
 *  - `outputFileName` 剥掉 .bif；适配器常量与核心一致（v1 / l0-box2 /
 *    stitch 拼接指纹）；转换器产物词表含 ventana-bif-jpeg；
 *  - slide-sniff：.bif 从「暂时直传」改为「需要转换」——头解析分派
 *    （IFD0 XMLPacket 带 iScan + BigTIFF + JPEG 压缩 → convert；
 *    JPEG2000/经典 TIFF/无 iScan → temporary），IFD0 位于头窗口之外时
 *    按头字段指向的首 IFD 偏移补读。
 */
import { describe, expect, it } from "vitest";
import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
// eslint-disable-next-line
import * as E from "../../static/tools/slide-transform/engine.js";

// ---- minimal in-memory BigTIFF builder with an IFD-0 XMLPacket ---------- //

function buildBif({
	little = true,
	bigtiff = true,
	iscan = true,
	desc = "Label Image",
	compression = 7,
	tiled = true,
	ifdFar = false,
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
	const descBytes = [...new TextEncoder().encode(desc), 0];
	const xmp = iscan
		? `<iScan Magnification="40" ScanRes="0.2325" Z-layers="1">\n<AOI0/>\n</iScan>\n`
		: `<?xml version="1.0"?><NotVentana/>\n`;
	const xmpBytes = [...new TextEncoder().encode(xmp)];
	const hdrLen = bigtiff ? 16 : 8;
	// BIF 的真实布局：label tile 载荷在前，IFD0 在后面（OS-2.bif ≈ 0.5 MB
	// 处）；测试用 256 KiB 的带内偏移即可触发「按头指向补读」路径
	const labelAt = hdrLen;
	const labelLen = ifdFar ? 300 * 1024 : 64;
	const descAt = labelAt + labelLen;
	const xmpAt = descAt + descBytes.length;
	const ifdAt = xmpAt + xmpBytes.length;
	const entries: number[][] = [];
	const push = (tag: number, typ: number, count: number, val: number[]) =>
		entries.push([...u16(tag), ...u16(typ), ...(bigtiff ? [...u64(count)] : [...u32(count)]), ...val]);
	const inline = (bytes: number[]) => {
		const v = bigtiff ? 8 : 4;
		return [...bytes, ...new Array(v - bytes.length).fill(0)].slice(0, v);
	};
	push(256, 4, 1, inline(u32(96)));
	push(257, 4, 1, inline(u32(64)));
	push(259, 3, 1, inline(u16(compression)));
	push(262, 3, 1, inline(u16(6)));
	push(270, 2, descBytes.length, bigtiff ? u64(descAt) : u32(descAt));
	push(277, 3, 1, inline(u16(3)));
	push(284, 3, 1, inline(u16(1)));
	if (tiled) {
		push(322, 3, 1, inline(u16(96)));
		push(323, 3, 1, inline(u16(64)));
	}
	push(324, 16, 1, bigtiff ? u64(labelAt) : u32(labelAt));
	push(325, 16, 1, bigtiff ? u64(labelLen) : u32(labelLen));
	push(700, 1, xmpBytes.length, bigtiff ? u64(xmpAt) : u32(xmpAt));
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
	// label payload (opaque bytes) then desc + xmp
	for (let i = 0; i < labelLen; i++) bytes.push(0xab);
	bytes.push(...descBytes, ...xmpBytes);
	if (bigtiff) bytes.push(...u64(entries.length));
	else bytes.push(...u16(entries.length));
	for (const e of entries) bytes.push(...e, ...new Array(esize - e.length).fill(0));
	if (bigtiff) bytes.push(...u64(0));
	else bytes.push(...u32(0));
	return new Uint8Array(bytes);
}

const asFile = (u8: Uint8Array, name = "x.bif") => new File([u8], name);

describe("BIF sniff（有界 staging 前探测）", () => {
	it("BigTIFF + IFD0 XMLPacket iScan → supported，适配器 ventana-bif-jpeg", async () => {
		for (const opts of [{}, { little: false }, { ifdFar: true }]) {
			const cap = await E.sniffTiffSlideCapability(asFile(buildBif(opts)));
			expect(cap, JSON.stringify(opts)).toMatchObject({
				supported: true,
				modality: "brightfield",
				format: "ventana-bif-jpeg",
				adapter: "ventana-bif-jpeg",
				bigtiff: true,
			});
		}
	});

	it("经典 TIFF + iScan → 类型化拒绝（BIF 恒为 BigTIFF）", async () => {
		const cap = await E.sniffTiffSlideCapability(asFile(buildBif({ bigtiff: false })));
		expect(cap.supported).toBe(false);
		if (!cap.supported) expect(cap.reason).toContain("BigTIFF");
	});

	it("XMLPacket 无 iScan → 不进 BIF 适配器（回落通用 tiled-JPEG 分派）", async () => {
		const cap = await E.sniffTiffSlideCapability(asFile(buildBif({ iscan: false })));
		expect(cap.supported).toBe(true);
		expect(cap.adapter).toBe(E.GTIFF_SOURCE_ADAPTER);
	});

	it("JPEG 2000 压缩的 BIF（IFD0 33003）→ 复制前类型化拒绝", async () => {
		const cap = await E.sniffTiffSlideCapability(asFile(buildBif({ compression: 33003 })));
		expect(cap.supported).toBe(false);
		if (!cap.supported) expect(cap.reason).toContain("JPEG 2000");
	});
});

describe("BIF 命名与常量", () => {
	it("outputFileName 剥掉 .bif 并按 profile 命名", () => {
		const ome = { outputProfile: E.OUTPUT_PROFILES.BF_OME };
		const classic = { outputProfile: E.OUTPUT_PROFILES.BF_CLASSIC };
		expect(E.outputFileName("OS-2.bif", ome)).toBe("OS-2.ome.tif");
		expect(E.outputFileName("OS-2.bif", classic)).toBe("OS-2.tif");
	});

	it("适配器常量与核心一致（v1 / l0-box2 / stitch 拼接指纹）", () => {
		expect(E.BIF_SOURCE_ADAPTER).toBe("ventana-bif-jpeg");
		expect(E.BIF_ADAPTER_VERSION).toBe("1");
		expect(E.BIF_PYRAMID_METHOD).toBe("l0-box2");
		expect(E.BIF_PRESERVE_COMPOSE_FINGERPRINT).toBe("bif-mosaic-compose:q96:y422:hstd:v1");
		// 审查回归：BIF 转换器自己的产物必须进「转换器输出」词表——否则
		// 其 classic BigTIFF 会被当普通输入做第二次有损重编码
		expect(E.CONVERTER_SOURCE_FORMATS).toContain("ventana-bif-jpeg");
	});

	it("扩展名只是提示：.bif 不在 bundle 提示表（单文件输入）", () => {
		expect(E.inputExtensionHint("scan.bif")).toBeNull();
	});
});

// ---- slide-sniff：.bif 从「暂时直传」改为「需要转换」 -------------------- //

const here = dirname(fileURLToPath(import.meta.url));
const sniffSrc = readFileSync(resolve(here, "../../static/upload/slide-sniff.js"), "utf8");
const w: { HP_SLIDE_SNIFF?: any } = {};
new Function("window", sniffSrc)(w);
const S = w.HP_SLIDE_SNIFF!;

describe("slide-sniff：BIF 分流", () => {
	it(".bif → 头解析分派（不再是暂时直传）", () => {
		const r = S.classifyExt("scan.bif");
		expect(r.cls).toBeUndefined();
		expect(r.route).toBe("tiff");
		expect(r.bif).toBe(true);
	});

	it("BigTIFF + iScan XMLPacket + JPEG → convert", async () => {
		const f = asFile(buildBif({}), "scan.bif");
		const r = await S.classifyFile(f);
		expect(r.cls).toBe(S.CLS.CONVERT);
		expect(r.bundle).toBe(false);
	});

	it("JPEG2000 压缩 → temporary（legacy-direct 声明）", async () => {
		const f = asFile(buildBif({ compression: 33005 }), "scan.bif");
		const r = await S.classifyFile(f);
		expect(r.cls).toBe(S.CLS.TEMPORARY);
	});

	it("经典 TIFF + iScan → temporary（ventana tif 变体）", async () => {
		const f = asFile(buildBif({ bigtiff: false }), "scan.bif");
		const r = await S.classifyFile(f);
		expect(r.cls).toBe(S.CLS.TEMPORARY);
	});

	it("无 iScan 块 → temporary（非 Ventana 容器，服务端终审）", async () => {
		const f = asFile(buildBif({ iscan: false }), "scan.bif");
		const r = await S.classifyFile(f);
		expect(r.cls).toBe(S.CLS.TEMPORARY);
	});

	it("IFD0 在头窗口之外（label 载荷在前）→ 按头指向的首 IFD 偏移补读", async () => {
		const f = asFile(buildBif({ ifdFar: true }), "scan.bif");
		const r = await S.classifyFile(f);
		expect(r.cls).toBe(S.CLS.CONVERT);
	});
});
