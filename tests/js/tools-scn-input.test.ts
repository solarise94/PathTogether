/**
 * F4 SCN 输入侧约定（engine.js）：
 *
 *  - TIFF/BigTIFF 容器按 IFD0 描述做**厂商分派**：Aperio → aperio-svs-jpeg，
 *    Leica SCN XML → leica-scn-jpeg；OME-TIFF 与本工具导出的 BigTIFF
 *    （描述 JSON 带来源标记）不是转换输入，在 staging 前给出类型化拒绝；
 *  - `sniffTiffSlideCapability` 对 SCN 的结构门槛与 SVS 相同（tiled + 基线
 *    JPEG + chunky + 3 samples），荧光变体（illuminationSource=fluorescence）
 *    在复制前拒绝；主 image 的多 collection 选择由 wasm 核心完成；
 *  - `outputFileName` 剥掉 .scn 扩展名。
 */
import { describe, expect, it } from "vitest";
// eslint-disable-next-line
import * as E from "../../static/tools/slide-transform/engine.js";

// ---- minimal in-memory BigTIFF builder with a custom description ---------- //

const SCN_XML = (illuminationSource: string) =>
	`<?xml version="1.0"?>` +
	`<scn xmlns="http://www.leica-microsystems.com/scn/2010/10/01">` +
	`<collection name="c" uuid="u"><barcode>QQ==</barcode>` +
	`<image name="label" uuid="u1"><pixels sizeX="128" sizeY="96">` +
	`<dimension sizeX="128" sizeY="96" r="0" ifd="0" /></pixels>` +
	`<view sizeX="64000" sizeY="48000" />` +
	`<scanSettings><illuminationSettings><illuminationSource>${illuminationSource}` +
	`</illuminationSource></illuminationSettings></scanSettings></image>` +
	`<image name="main" uuid="u2"><pixels sizeX="520" sizeY="300">` +
	`<dimension sizeX="520" sizeY="300" r="0" ifd="1" />` +
	`<dimension sizeX="260" sizeY="150" r="1" ifd="2" /></pixels>` +
	`<view sizeX="260000" sizeY="150000" />` +
	`<scanSettings><objectiveSettings><objective>20</objective></objectiveSettings>` +
	`<illuminationSettings><illuminationSource>${illuminationSource}` +
	`</illuminationSource></illuminationSettings></scanSettings></image>` +
	`</collection></scn>`;

function buildTiff({
	bigtiff = true,
	little = true,
	tiled = true,
	compression = 7,
	samples = 3,
	planar = 1,
	photo = 6,
	desc = SCN_XML("brightfield"),
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
		const b = [...u32(hi), ...u32(lo)];
		return le ? [...b.slice(4), ...b.slice(0, 4)] : b;
	};
	const text = new TextEncoder().encode(desc);
	const hdrLen = bigtiff ? 16 : 8;
	const ifdAt = hdrLen + text.length + 16;
	const entries: number[][] = [];
	const push = (tag: number, typ: number, count: number, val: number[]) =>
		entries.push([...u16(tag), ...u16(typ), ...(bigtiff ? [...u64(count)] : [...u32(count)]), ...val]);
	const inline = (bytes: number[]) => {
		const v = bigtiff ? 8 : 4;
		return [...bytes, ...new Array(v - bytes.length).fill(0)].slice(0, v);
	};
	push(256, 4, 1, inline(u32(520)));
	push(257, 4, 1, inline(u32(300)));
	push(259, 3, 1, inline(u16(compression)));
	push(262, 3, 1, inline(u16(photo)));
	if (text.length) push(270, 2, text.length, bigtiff ? u64(hdrLen) : u32(hdrLen));
	push(277, 3, 1, inline(u16(samples)));
	push(284, 3, 1, inline(u16(planar)));
	if (tiled) {
		push(322, 3, 1, inline(u16(512)));
		push(323, 3, 1, inline(u16(512)));
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
	bytes.push(...text, ...new Array(ifdAt - hdrLen - text.length).fill(0));
	if (bigtiff) bytes.push(...u64(entries.length));
	else bytes.push(...u16(entries.length));
	for (const e of entries) bytes.push(...e, ...new Array(esize - e.length).fill(0));
	if (bigtiff) bytes.push(...u64(0));
	else bytes.push(...u32(0));
	return new Uint8Array(bytes);
}

const asFile = (u8: Uint8Array, name = "x.scn") => new File([u8], name);

describe("SCN sniff（有界 staging 前探测）", () => {
	it("明场 JPEG 编码 SCN → supported，适配器 leica-scn-jpeg", async () => {
		for (const opts of [{}, { bigtiff: false }, { little: false }, { bigtiff: false, little: false }]) {
			const cap = await E.sniffTiffSlideCapability(asFile(buildTiff(opts)));
			expect(cap, JSON.stringify(opts)).toMatchObject({
				supported: true,
				modality: "brightfield",
				format: "leica-scn-jpeg",
				adapter: "leica-scn-jpeg",
			});
		}
	});

	it("荧光 SCN → 复制前拒绝（变体拒绝，不是结构错误）", async () => {
		const cap = await E.sniffTiffSlideCapability(asFile(buildTiff({ desc: SCN_XML("fluorescence") })));
		expect(cap.supported).toBe(false);
		expect(cap.reason).toContain("荧光");
	});

	it("非 JPEG 编码 SCN → 压缩拒绝", async () => {
		const cap = await E.sniffTiffSlideCapability(asFile(buildTiff({ compression: 8 })));
		expect(cap.supported).toBe(false);
		expect(cap.reason).toContain("基线 JPEG");
	});

	it("JPEG 2000 编码 SCN → JPEG 2000 拒绝", async () => {
		const cap = await E.sniffTiffSlideCapability(asFile(buildTiff({ compression: 33005 })));
		expect(cap.supported).toBe(false);
		expect(cap.reason).toContain("JPEG 2000");
	});

	it("非 tiled 的 SCN 布局 → 结构拒绝", async () => {
		const cap = await E.sniffTiffSlideCapability(asFile(buildTiff({ tiled: false })));
		expect(cap.supported).toBe(false);
		expect(cap.reason).toContain("tiled");
	});
});

describe("TIFF 厂商分派（OME-TIFF / 转换器 BigTIFF 不是转换输入）", () => {
	it("OME-TIFF → 类型化拒绝，不再报「未标识 Aperio」", async () => {
		const ome = '<?xml version="1.0" encoding="UTF-8"?><OME xmlns="http://www.openmicroscopy.org/Schemas/OME/2016-06"></OME>';
		const cap = await E.sniffTiffSlideCapability(asFile(buildTiff({ desc: ome, photo: 2 })));
		expect(cap.supported).toBe(false);
		expect(cap.reason).toContain("OME-TIFF");
		expect(cap.reason).not.toContain("Aperio");
	});

	it("转换器导出的 BigTIFF（描述 JSON 带来源标记）→ 类型化拒绝", async () => {
		for (const sf of E.CONVERTER_SOURCE_FORMATS) {
			const desc = `{"adapter": "${sf}", "adapter_version": "1", "mpp_x": 0.5, "mpp_y": 0.5, "objective": 20.0, "source_format": "${sf}"}\u0000`;
			const cap = await E.sniffTiffSlideCapability(asFile(buildTiff({ desc, photo: 2 })));
			expect(cap.supported, sf).toBe(false);
			expect(cap.reason, sf).toContain("不是转换输入");
		}
	});

	it("未知厂商维持「不猜」拒绝（提到 Aperio / Leica SCN）", async () => {
		const cap = await E.sniffTiffSlideCapability(
			asFile(buildTiff({ desc: "Some Other Scanner v1", photo: 2 })));
		expect(cap.supported).toBe(false);
		expect(cap.reason).toContain("Aperio");
		expect(cap.reason).toContain("Leica SCN");
	});

	it("SVS 路由行为不变：Aperio 描述仍给 aperio-svs-jpeg", async () => {
		const aperio = "Aperio Image Library v11.2.1 \r\n520x300 (128x128) JPEG/RGB Q=30|AppMag = 20|MPP = 0.4990";
		const cap = await E.sniffTiffSlideCapability(asFile(buildTiff({ desc: aperio, photo: 2 }), "y.svs"));
		expect(cap).toMatchObject({ supported: true, adapter: "aperio-svs-jpeg" });
	});
});

describe("SCN 命名与输出", () => {
	it("outputFileName 剥掉 .scn 并按 profile 命名", () => {
		const job = { outputProfile: E.OUTPUT_PROFILES.BF_OME };
		expect(E.outputFileName("Leica-1.scn", job)).toBe("Leica-1.ome.tif");
		const classic = { outputProfile: E.OUTPUT_PROFILES.BF_CLASSIC };
		expect(E.outputFileName("slide.scn", classic)).toBe("slide.tif");
	});

	it("SCN 适配器版本常量与核心一致（v1）", () => {
		expect(E.SCN_SOURCE_ADAPTER).toBe("leica-scn-jpeg");
		expect(E.SCN_ADAPTER_VERSION).toBe("1");
	});
});
