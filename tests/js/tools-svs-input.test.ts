/**
 * F1 SVS 输入侧约定（engine.js）：
 *
 *  - TIFF/BigTIFF 容器头（II*\0 / MM\0* / II+\0 / MM\0+）被 magicSupported
 *    接受、magicModality 判为明场；KFB/KFBF 行为不变；
 *  - `sniffTiffSlideCapability`（staging 前的有界结构探测，≤ ~78 KiB 读取）
 *    只对「tiled + 基线 JPEG（非 JPEG 2000）+ chunky + 3 samples +
 *    Aperio 描述」的主 IFD 放行，并给出 `aperio-svs-jpeg` 适配器 id；
 *    其余 TIFF 给出具体拒绝理由，多 GiB 未支持文件不会被复制进 OPFS。
 */
import { describe, expect, it } from "vitest";
// eslint-disable-next-line
import * as E from "../../static/tools/slide-transform/engine.js";

// ---- minimal in-memory TIFF builder for the sniff ------------------------- //

function buildTiff({
	bigtiff = false,
	little = true,
	tiled = true,
	compression = 7,
	samples = 3,
	planar = 1,
	photo = 2,
	aperio = true,
	extraEntries = [],
} = {}) {
	const le = little;
	const u16 = (v) => {
		const b = [(v >> 8) & 0xff, v & 0xff];
		return le ? [b[1], b[0]] : b;
	};
	const u32 = (v) => {
		const b = [(v >>> 24) & 0xff, (v >>> 16) & 0xff, (v >>> 8) & 0xff, v & 0xff];
		return le ? [b[3], b[2], b[1], b[0]] : b;
	};
	const desc = new TextEncoder().encode(
		aperio
			? "Aperio Image Library v11.2.1 \r\n100x100 [0,0 100x100] (256x256) JPEG/RGB Q=30|AppMag = 20|MPP = 0.4990"
			: "Some Other Scanner v1",
	);
	// layout: header | desc | IFD
	const hdrLen = bigtiff ? 16 : 8;
	const ifdAt = hdrLen + desc.length + 16; // keep even
	const entries: number[][] = [];
	const push = (tag: number, typ: number, count: number, val: number[]) =>
		entries.push([...u16(tag), ...u16(typ), ...(bigtiff ? [...u64(count)] : [...u32(count)]), ...val]);
	const u64 = (v: number) => {
		const lo = v >>> 0;
		const hi = Math.floor(v / 2 ** 32) >>> 0;
		const b = [...u32(hi), ...u32(lo)];
		return le ? [...b.slice(4), ...b.slice(0, 4)] : b;
	};
	const inline = (bytes: number[]) => {
		const v = bigtiff ? 8 : 4;
		return [...bytes, ...new Array(v - bytes.length).fill(0)].slice(0, v);
	};
	push(256, 4, 1, inline(u32(100)));
	push(257, 4, 1, inline(u32(100)));
	push(259, 3, 1, inline(u16(compression)));
	push(262, 3, 1, inline(u16(photo)));
	// description pointer: 8 bytes for BigTIFF, 4 for classic
	push(270, 2, desc.length, bigtiff ? u64(hdrLen) : u32(hdrLen));
	push(277, 3, 1, inline(u16(samples)));
	push(284, 3, 1, inline(u16(planar)));
	if (tiled) {
		push(322, 3, 1, inline(u16(256)));
		push(323, 3, 1, inline(u16(256)));
	}
	for (const e of extraEntries) push(e[0], e[1], e[2], e[3]);
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
	bytes.push(...desc, ...new Array(ifdAt - hdrLen - desc.length).fill(0));
	if (bigtiff) bytes.push(...u64(entries.length));
	else bytes.push(...u16(entries.length));
	for (const e of entries) bytes.push(...e, ...new Array(esize - e.length).fill(0));
	if (bigtiff) bytes.push(...u64(0));
	else bytes.push(...u32(0));
	return new Uint8Array(bytes);
}

const asFile = (u8: Uint8Array) => new File([u8], "x.svs");

describe("TIFF magic / modality (engine.js)", () => {
	it("classic + BigTIFF, both byte orders → supported, brightfield", () => {
		expect(E.isTiffHeader([0x49, 0x49, 0x2a, 0x00, 1, 2, 3, 4])).toBe(true);
		expect(E.isTiffHeader([0x4d, 0x4d, 0x00, 0x2a, 1, 2, 3, 4])).toBe(true);
		expect(E.isTiffHeader([0x49, 0x49, 0x2b, 0x00, 1, 2, 3, 4])).toBe(true);
		expect(E.isTiffHeader([0x4d, 0x4d, 0x00, 0x2b, 1, 2, 3, 4])).toBe(true);
		for (const h of [[0x49, 0x49, 0x2a, 0x00], [0x4d, 0x4d, 0x00, 0x2a], [0x49, 0x49, 0x2b, 0x00]]) {
			expect(E.magicSupported(h)).toBe(true);
			expect(E.magicModality(h)).toBe("brightfield");
		}
		expect(E.isTiffHeader([0x49, 0x49, 0x2d, 0x00])).toBe(false);
	});

	it("KFB/KFBF magic behaviour unchanged", () => {
		const kfb = [0xf1, 0x01, 0xee, 0xee, 0x4b, 0x46, 0x42, 0x00];
		const kfbf = [0xf1, 0x01, 0xee, 0xee, 0x4b, 0x46, 0x42, 0x46];
		expect(E.magicModality(kfb)).toBe("brightfield");
		expect(E.magicModality(kfbf)).toBe("fluorescence");
		expect(E.magicSupported([1, 2, 3, 4, 5, 6, 7, 8])).toBe(false);
	});
});

describe("sniffTiffSlideCapability (bounded pre-stage probe)", () => {
	it("accepts an Aperio JPEG classic TIFF with the SVS adapter id", async () => {
		const cap = await E.sniffTiffSlideCapability(asFile(buildTiff()));
		expect(cap).toMatchObject({
			supported: true,
			modality: "brightfield",
			adapter: "aperio-svs-jpeg",
			format: "aperio-svs-jpeg",
			bigtiff: false,
		});
	});

	it("accepts BigTIFF and big-endian variants", async () => {
		for (const opts of [{ bigtiff: true }, { little: false }, { bigtiff: true, little: false }]) {
			const cap = await E.sniffTiffSlideCapability(asFile(buildTiff(opts)));
			expect(cap.supported, JSON.stringify(opts) + " -> " + JSON.stringify(cap)).toBe(true);
		}
	});

	it("rejects JPEG 2000 with a specific reason", async () => {
		const cap = await E.sniffTiffSlideCapability(asFile(buildTiff({ compression: 33003 })));
		expect(cap.supported).toBe(false);
		expect(cap.reason).toContain("JPEG 2000");
	});

	it("rejects stripped (non-tiled) mains, planar and fluorescence page sets", async () => {
		const stripped = await E.sniffTiffSlideCapability(asFile(buildTiff({ tiled: false })));
		expect(stripped.reason).toContain("tiled");
		const planar = await E.sniffTiffSlideCapability(asFile(buildTiff({ planar: 2 })));
		expect(planar.reason).toContain("PlanarConfiguration");
		const fl = await E.sniffTiffSlideCapability(asFile(buildTiff({ samples: 4 })));
		expect(fl.reason).toContain("SamplesPerPixel");
	});

	it("rejects non-Aperio TIFFs without guessing", async () => {
		const cap = await E.sniffTiffSlideCapability(asFile(buildTiff({ aperio: false })));
		expect(cap.supported).toBe(false);
		expect(cap.reason).toContain("Aperio");
	});

	it("rejects non-TIFF files before any structure read", async () => {
		const cap = await E.sniffTiffSlideCapability(asFile(new Uint8Array([1, 2, 3, 4, 5, 6, 7, 8])));
		expect(cap.supported).toBe(false);
		expect(cap.reason).toContain("TIFF");
	});
});
