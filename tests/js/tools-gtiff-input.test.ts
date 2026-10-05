/**
 * F5 通用瓦片 JPEG TIFF/BigTIFF 输入侧约定（engine.js）：
 *
 *  - 无厂商描述（或外来描述）+ 结构门槛全过（tiled + 基线 JPEG + chunky +
 *    3 samples + photo 2/6）的 TIFF/BigTIFF → 通用瓦片适配器
 *    （generic-tiled-jpeg-tiff，OpenSlide generic-tiff 家族）；
 *  - 条带 / LZW / deflate / JPEG 2000 / 多通道 / 平面存储等变体在结构门槛
 *    被拒（「暂时直传」变体；核心在复制前给同一批类型化拒绝）；
 *  - OME-TIFF 与本工具导出的 BigTIFF（描述 JSON 带来源标记，词表含
 *    generic-tiled-jpeg-tiff 自己）不是转换输入，在 staging 前类型化拒绝；
 *  - `outputFileName` 剥掉 .tif / .tiff。
 */
import { describe, expect, it } from "vitest";
// eslint-disable-next-line
import * as E from "../../static/tools/slide-transform/engine.js";

// ---- minimal in-memory TIFF builder with a custom description（SCN 用例
//      同款；photo/samples/planar/tiled 可调） ---------------------------- //

function buildTiff({
	bigtiff = true,
	little = true,
	tiled = true,
	compression = 7,
	samples = 3,
	planar = 1,
	photo = 6,
	desc = "",
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
	bytes.push(...text, ...new Array(ifdAt - hdrLen - text.length).fill(0));
	if (bigtiff) bytes.push(...u64(entries.length));
	else bytes.push(...u16(entries.length));
	for (const e of entries) bytes.push(...e, ...new Array(esize - e.length).fill(0));
	if (bigtiff) bytes.push(...u64(0));
	else bytes.push(...u32(0));
	return new Uint8Array(bytes);
}

const asFile = (u8: Uint8Array, name = "x.tif") => new File([u8], name);

describe("通用瓦片 JPEG TIFF sniff（有界 staging 前探测）", () => {
	it("无厂商描述 + 结构门槛全过 → supported，适配器 generic-tiled-jpeg-tiff", async () => {
		for (const opts of [{}, { bigtiff: false }, { little: false }, { bigtiff: false, little: false }]) {
			const cap = await E.sniffTiffSlideCapability(asFile(buildTiff(opts)));
			expect(cap, JSON.stringify(opts)).toMatchObject({
				supported: true,
				modality: "brightfield",
				format: "generic-tiled-jpeg-tiff",
				adapter: "generic-tiled-jpeg-tiff",
			});
		}
	});

	it("外来厂商描述（无已知厂商标记）同样路由进通用适配器", async () => {
		const cap = await E.sniffTiffSlideCapability(
			asFile(buildTiff({ desc: "Some Other Scanner v1" })));
		expect(cap).toMatchObject({ supported: true, adapter: "generic-tiled-jpeg-tiff" });
	});

	it("条带存储（无 322/323）→ 结构拒绝（暂时直传变体）", async () => {
		const cap = await E.sniffTiffSlideCapability(asFile(buildTiff({ tiled: false })));
		expect(cap.supported).toBe(false);
		expect(cap.reason).toContain("tiled");
	});

	it("LZW / deflate 编码 → 非 JPEG 拒绝", async () => {
		for (const compression of [5, 8, 32946]) {
			const cap = await E.sniffTiffSlideCapability(asFile(buildTiff({ compression })));
			expect(cap.supported, String(compression)).toBe(false);
			expect(cap.reason, String(compression)).toContain("基线 JPEG");
		}
	});

	it("JPEG 2000 编码 → JPEG 2000 拒绝", async () => {
		for (const compression of [33003, 33005]) {
			const cap = await E.sniffTiffSlideCapability(asFile(buildTiff({ compression })));
			expect(cap.supported, String(compression)).toBe(false);
			expect(cap.reason, String(compression)).toContain("JPEG 2000");
		}
	});

	it("多通道 / 灰度（SamplesPerPixel≠3）与平面存储 → 结构拒绝", async () => {
		const spp = await E.sniffTiffSlideCapability(asFile(buildTiff({ samples: 1 })));
		expect(spp.supported).toBe(false);
		expect(spp.reason).toContain("SamplesPerPixel=1");
		const planar = await E.sniffTiffSlideCapability(asFile(buildTiff({ planar: 2 })));
		expect(planar.supported).toBe(false);
		expect(planar.reason).toContain("平面存储");
	});

	it("photometric 不在 {2,6} → 结构拒绝", async () => {
		const cap = await E.sniffTiffSlideCapability(asFile(buildTiff({ photo: 5 })));
		expect(cap.supported).toBe(false);
		expect(cap.reason).toContain("PhotometricInterpretation=5");
	});
});

describe("转换器输出与 OME-TIFF 不是转换输入", () => {
	it("OME-TIFF → 类型化拒绝", async () => {
		const ome = '<?xml version="1.0" encoding="UTF-8"?><OME xmlns="http://www.openmicroscopy.org/Schemas/OME/2016-06"></OME>';
		const cap = await E.sniffTiffSlideCapability(asFile(buildTiff({ desc: ome, photo: 2 })));
		expect(cap.supported).toBe(false);
		expect(cap.reason).toContain("OME-TIFF 不是转换输入");
	});

	it("转换器导出的 BigTIFF（描述 JSON 带来源标记）→ 类型化拒绝", async () => {
		for (const sf of E.CONVERTER_SOURCE_FORMATS) {
			const desc = `{"adapter": "${sf}", "adapter_version": "1", "mpp_x": 0.5, "mpp_y": 0.5, "objective": null, "source_format": "${sf}"}\u0000`;
			const cap = await E.sniffTiffSlideCapability(asFile(buildTiff({ desc, photo: 2 })));
			expect(cap.supported, sf).toBe(false);
			expect(cap.reason, sf).toContain("不是转换输入");
		}
	});

	it("Aperio 描述仍路由 SVS 适配器（不进通用适配器）", async () => {
		const aperio = "Aperio Image Library v11.2.1 \r\n520x300 (256x256) JPEG/RGB Q=30|AppMag = 20|MPP = 0.4990";
		const cap = await E.sniffTiffSlideCapability(asFile(buildTiff({ desc: aperio, photo: 2 })));
		expect(cap).toMatchObject({ supported: true, adapter: "aperio-svs-jpeg" });
	});
});

describe("通用 TIFF 命名与常量", () => {
	it("outputFileName 剥掉 .tif / .tiff 并按 profile 命名", () => {
		const ome = { outputProfile: E.OUTPUT_PROFILES.BF_OME };
		const classic = { outputProfile: E.OUTPUT_PROFILES.BF_CLASSIC };
		expect(E.outputFileName("CMU-1.tiff", ome)).toBe("CMU-1.ome.tif");
		expect(E.outputFileName("CMU-1.tiff", classic)).toBe("CMU-1.tif");
		expect(E.outputFileName("CMU-1.tif", ome)).toBe("CMU-1.ome.tif");
	});

	it("适配器常量与核心一致（v1 / l0-box2）", () => {
		expect(E.GTIFF_SOURCE_ADAPTER).toBe("generic-tiled-jpeg-tiff");
		expect(E.GTIFF_ADAPTER_VERSION).toBe("1");
		expect(E.GTIFF_PYRAMID_METHOD).toBe("l0-box2");
		// 转换器来源词表包含通用 TIFF 自身（自产 BigTIFF 不是转换输入）
		expect(E.CONVERTER_SOURCE_FORMATS).toContain("generic-tiled-jpeg-tiff");
	});
});
