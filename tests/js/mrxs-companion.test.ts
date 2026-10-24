/**
 * MRXS「先选 .mrxs，再选同名数据文件夹」共享协助逻辑（static/upload/
 * mrxs-companion.js，classic script → new Function 加载）的单元测试。
 *
 * 覆盖：
 *   - stem 抽取与名字匹配：精确 / 大小写 / 中文 NFD vs NFC；
 *   - detectNeedsFolder：单个 .mrxs（+散装 .dat）→ 需要文件夹；带目录结构 /
 *     多个 / 零个 .mrxs / .vms → 不进入；
 *   - planCompanionFolder：(a) 同名文件夹本身 (b) 父文件夹里的同名子目录
 *     (c) 不同名但根上有 Slidedat.ini（需显式确认）(d) 没有 Slidedat.ini
 *     → 拒绝；
 *   - mergeRows：relPath 改写为「{stem}/…」、入口行、排除兄弟子树与散文件、
 *     排除 .mrxs、成员上限；装配结果**真的**满足 engine.sniffMrxBundle /
 *     planMrxBundle（planner 仍是完整性的唯一权威）；
 *   - collectDirectoryHandle：假 FileSystemDirectoryHandle 的 values() 递归
 *     遍历 + 上限；
 *   - pickDirectory：startIn 仅在真有句柄时传；TypeError 回退重试；
 *     AbortError=cancelled（页面据此安静保持原状态）；SecurityError/无 API
 *     → 回退 webkitdirectory input。
 * 页面接线（转换器页源码级 + 工作台编排器行为）见本文件下半部分与
 * tests/js/inline-convert.test.ts 的 companion 分组。
 */
import { describe, expect, it, vi } from "vitest";
import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
// eslint-disable-next-line
import * as E from "../../static/tools/slide-transform/engine.js";

const here = dirname(fileURLToPath(import.meta.url));
const root = resolve(here, "../..");
const src = readFileSync(resolve(root, "static/upload/mrxs-companion.js"), "utf8");
const toolsPageSrc = readFileSync(resolve(root, "static/tools/tools-slides.js"), "utf8");
const toolsShellSrc = readFileSync(resolve(root, "templates/tools_slides.html"), "utf8");
const toolsCssSrc = readFileSync(resolve(root, "static/tools/tools-slides.css"), "utf8");
const i18nSrc = readFileSync(resolve(root, "static/i18n.js"), "utf8");
const appSrc = readFileSync(resolve(root, "static/app.js"), "utf8");
const inlineSrc = readFileSync(
	resolve(root, "static/upload/inline-convert.js"), "utf8");
const indexHtml = readFileSync(resolve(root, "templates/index.html"), "utf8");

type W = Record<string, unknown>;
function load() {
	const w: W = {};
	new Function("window", src)(w);
	return w.HP_MRXS_COMPANION as Record<string, (...a: unknown[]) => unknown> & {
		DEFAULT_MAX_MEMBERS: number;
	};
}
const C = () => load() as unknown as {
	stemOf(name: string): string;
	namesMatch(a: string, b: string): boolean;
	detectNeedsFolder(rows: unknown): { stem: string; entryName: string; entryRow: unknown } | null;
	planCompanionFolder(o: { stem: string; folderName: string; rows: unknown[] }): {
		status: string; prefixSegs?: string[]; prefixSegCount?: number; folderName?: string;
	};
	mergeRows(o: Record<string, unknown>): { ok: boolean; rows?: Array<{ name: string; relPath: string; file: unknown }>; code?: string; cap?: number };
	collectDirectoryHandle(dir: unknown, opts?: { maxMembers?: number }, deps?: { getFile?: (h: unknown) => Promise<unknown> }): Promise<Array<{ name: string; relPath: string; file: unknown }>>;
	collectCompanionFolder(dir: unknown, stem: string, opts?: { maxMembers?: number }, deps?: { getFile?: (h: unknown) => Promise<unknown> }): Promise<Array<{ name: string; relPath: string; file: unknown }>>;
	pickDirectory(opts?: { id?: string; startIn?: unknown }, deps?: { win?: unknown }): Promise<{ ok: boolean; reason?: string; handle?: unknown; name?: string }>;
	supportsDirectoryPicker(win?: unknown): boolean;
	supportsFolderPicker(deps?: { doc?: unknown }): boolean;
	DEFAULT_MAX_MEMBERS: number;
};

/// 测试用「File」替身（slice().arrayBuffer() 与 Blob 同形，planMrxBundle 直用）
function fileOf(name: string, content: string | Uint8Array) {
	const b = typeof content === "string"
		? new TextEncoder().encode(content) : content;
	return {
		name,
		size: b.length,
		slice(start: number, end: number) {
			const s = Math.max(0, start), e = Math.min(end, b.length);
			return { arrayBuffer: async () => b.slice(s, e).buffer };
		},
	};
}
const rowOf = (relPath: string, file: unknown, name?: string) =>
	({ name: name ?? String(relPath).split("/").pop(), relPath, file });

const SLIDEDAT = [
	"[HIERARCHICAL]", "INDEXFILE = Index.dat", "",
	"[DATAFILE]", "FILE_COUNT = 1", "FILE_0 = Data0000.dat", "",
].join("\r\n");

// ---------------------------------------------------------------- stem/match

describe("stemOf / namesMatch（NFC + 大小写折叠）", () => {
	it("抽取词干：精确、大小写、多点文件名", () => {
		expect(C().stemOf("病例A.mrxs")).toBe("病例A");
		expect(C().stemOf("SLIDE.MRXS")).toBe("SLIDE");
		expect(C().stemOf("scan.2024.01.mrxs")).toBe("scan.2024.01");
		expect(C().stemOf("CMU-1.mrxs")).toBe("CMU-1");
	});

	it("中文名精确匹配；可分解字符的 NFD（macOS）与 NFC 等价；大小写折叠", () => {
		expect(C().namesMatch("病例A", "病例A")).toBe(true);
		// 中文没有规范分解（NFC==NFD），混合可分解字符（é）才是 NFD 差异的来源
		const nfd = "切片e\u0301A";            // e + 组合尖音符号
		expect(nfd.normalize("NFC")).toBe("切片éA");
		expect(C().namesMatch(nfd, "切片éA")).toBe(true);
		expect(C().namesMatch("CMU-1", "cmu-1")).toBe(true);
		expect(C().namesMatch("病例A", "病例B")).toBe(false);
	});
});

// ------------------------------------------------------------ detectNeedsFolder

describe("detectNeedsFolder（只有一个散装 .mrxs → 需要同名文件夹）", () => {
	it("单个 .mrxs → needs，词干来自入口名", () => {
		const rows = [rowOf("病例A.mrxs", fileOf("病例A.mrxs", "x"))];
		const need = C().detectNeedsFolder(rows);
		expect(need && need.stem).toBe("病例A");
		expect(need && need.entryName).toBe("病例A.mrxs");
	});

	it(".mrxs + 散装 .dat（无目录结构）→ 仍是 needs", () => {
		const rows = [
			rowOf("CMU-1.mrxs", fileOf("CMU-1.mrxs", "x")),
			rowOf("Data0000.dat", fileOf("Data0000.dat", "x")),
			rowOf("Slidedat.ini", fileOf("Slidedat.ini", "x")),
		];
		expect(C().detectNeedsFolder(rows)?.stem).toBe("CMU-1");
	});

	it("已有目录结构（≥2 层的行）→ 不进入（完整性交给 planner）", () => {
		const rows = [
			rowOf("CMU-1.mrxs", fileOf("CMU-1.mrxs", "x")),
			rowOf("CMU-1/Slidedat.ini", fileOf("Slidedat.ini", "x")),
		];
		expect(C().detectNeedsFolder(rows)).toBeNull();
	});

	it("零个 / 多个 .mrxs、.vms → 不进入", () => {
		expect(C().detectNeedsFolder([rowOf("a.dat", fileOf("a.dat", "x"))])).toBeNull();
		expect(C().detectNeedsFolder([
			rowOf("a.mrxs", fileOf("a.mrxs", "x")),
			rowOf("b.mrxs", fileOf("b.mrxs", "x")),
		])).toBeNull();
		expect(C().detectNeedsFolder([rowOf("slide.vms", fileOf("slide.vms", "x"))])).toBeNull();
	});
});

// -------------------------------------------------------- planCompanionFolder

describe("planCompanionFolder（(a)/(b)/(c)/(d) 四态）", () => {
	const stem = "病例A";

	it("(a) 所选文件夹本身就是同名数据文件夹", () => {
		const rows = [
			rowOf("病例A/Slidedat.ini", fileOf("Slidedat.ini", SLIDEDAT)),
			rowOf("病例A/Index.dat", fileOf("Index.dat", "x")),
		];
		const plan = C().planCompanionFolder({ stem, folderName: "病例A", rows });
		expect(plan.status).toBe("use");
		expect(plan.prefixSegs).toEqual(["病例A"]);
		expect(plan.prefixSegCount).toBe(1);
	});

	it("(b) 选的是父文件夹（含同名子目录；里面还有别的兄弟内容）", () => {
		const rows = [
			rowOf("scan/病例A/Slidedat.ini", fileOf("Slidedat.ini", SLIDEDAT)),
			rowOf("scan/病例A/Data0000.dat", fileOf("Data0000.dat", "x")),
			rowOf("scan/病例B/other.txt", fileOf("other.txt", "x")),
			rowOf("scan/notes.txt", fileOf("notes.txt", "x")),
		];
		const plan = C().planCompanionFolder({ stem, folderName: "scan", rows });
		expect(plan.status).toBe("parent");
		expect(plan.prefixSegs).toEqual(["scan", "病例A"]);
		expect(plan.prefixSegCount).toBe(2);
	});

	it("(b') 目录名大小写/NFD 差异不影响判定", () => {
		const nfd = "病例A".normalize("NFD");
		const rows = [rowOf(`${nfd}/Slidedat.ini`, fileOf("Slidedat.ini", SLIDEDAT))];
		expect(C().planCompanionFolder({ stem, folderName: nfd, rows }).status).toBe("use");
		const rows2 = [rowOf("CMU-1/Slidedat.ini", fileOf("Slidedat.ini", SLIDEDAT))];
		expect(C().planCompanionFolder({ stem: "CMU-1", folderName: "cmu-1", rows: rows2 }).status)
			.toBe("use");
	});

	it("(c) 根上有 Slidedat.ini 但不同名 → mismatch（等用户显式确认）", () => {
		const rows = [rowOf("wrong_name/Slidedat.ini", fileOf("Slidedat.ini", SLIDEDAT))];
		const plan = C().planCompanionFolder({ stem, folderName: "wrong_name", rows });
		expect(plan.status).toBe("mismatch");
		expect(plan.folderName).toBe("wrong_name");
	});

	it("(c') 所选文件夹根上直接是 Slidedat.ini（一层行）→ mismatch", () => {
		const rows = [
			rowOf("some_folder/Slidedat.ini", fileOf("Slidedat.ini", SLIDEDAT)),
		];
		const plan = C().planCompanionFolder({ stem, folderName: "some_folder", rows });
		expect(plan.status).toBe("mismatch");
	});

	it("(d) 没有 Slidedat.ini → reject", () => {
		const rows = [
			rowOf("病例A/Index.dat", fileOf("Index.dat", "x")),
			rowOf("病例A/Data0000.dat", fileOf("Data0000.dat", "x")),
		];
		expect(C().planCompanionFolder({ stem, folderName: "病例A", rows }).status).toBe("reject");
		expect(C().planCompanionFolder({ stem, folderName: "empty", rows: [] }).status).toBe("reject");
	});

	// 复核 2026-10-22：父文件夹里常有多张切片（病例A/、病例B/…）——绝不能
	// 按遍历顺序拿第一个 Slidedat.ini
	it("(b) 其他切片的子目录排在前面：仍选中目标切片的子树", () => {
		const rows = [
			rowOf("Slides/病例B/Slidedat.ini", fileOf("Slidedat.ini", SLIDEDAT)),
			rowOf("Slides/病例B/Data0000.dat", fileOf("Data0000.dat", "x")),
			rowOf("Slides/病例A/Slidedat.ini", fileOf("Slidedat.ini", SLIDEDAT)),
			rowOf("Slides/病例A/Index.dat", fileOf("Index.dat", "x")),
			rowOf("Slides/病例A/Data0000.dat", fileOf("Data0000.dat", "x")),
		];
		const plan = C().planCompanionFolder({ stem: "病例A", folderName: "Slides", rows });
		expect(plan.status).toBe("parent");
		expect(plan.prefixSegs).toEqual(["Slides", "病例A"]);
		const merged = C().mergeRows({
			stem: "病例A", entryName: "病例A.mrxs",
			entryFile: fileOf("病例A.mrxs", "entry"),
			rows, prefixSegs: plan.prefixSegs,
		});
		expect(merged.ok).toBe(true);
		// 只有病例A 的成员进装配；病例B 的文件一概不在
		const rels = (merged.rows || []).map((r) => r.relPath);
		expect(rels).toEqual([
			"病例A.mrxs", "病例A/Slidedat.ini", "病例A/Index.dat", "病例A/Data0000.dat",
		]);
	});

	it("(d) 父文件夹里只有其他切片的数据文件夹 → reject", () => {
		const rows = [
			rowOf("Slides/病例B/Slidedat.ini", fileOf("Slidedat.ini", SLIDEDAT)),
			rowOf("Slides/病例C/Slidedat.ini", fileOf("Slidedat.ini", SLIDEDAT)),
		];
		expect(C().planCompanionFolder({ stem: "病例A", folderName: "Slides", rows }).status)
			.toBe("reject");
	});

	it("多个同名候选：取最浅的", () => {
		const rows = [
			rowOf("backup/2024/病例A/Slidedat.ini", fileOf("Slidedat.ini", SLIDEDAT)),
			rowOf("backup/病例A/Slidedat.ini", fileOf("Slidedat.ini", SLIDEDAT)),
		];
		const plan = C().planCompanionFolder({ stem: "病例A", folderName: "backup", rows });
		expect(plan.status).toBe("parent");
		expect(plan.prefixSegs).toEqual(["backup", "病例A"]);
	});
});

// ------------------------------------------------------------------ mergeRows

describe("mergeRows（relPath 布局与 planner 合同）", () => {
	it("入口行 relPath = 文件名本身；成员行改写为「{stem}/…」；排除兄弟子树与 .mrxs", () => {
		const entry = fileOf("病例A.mrxs", "entry");
		const rows = [
			rowOf("scan/病例A.mrxs", fileOf("病例A.mrxs", "dup")),      // 入口已有 → 排除
			rowOf("scan/病例A/Slidedat.ini", fileOf("Slidedat.ini", SLIDEDAT)),
			rowOf("scan/病例A/Index.dat", fileOf("Index.dat", "idx")),
			rowOf("scan/病例B/other.txt", fileOf("other.txt", "x")),    // 兄弟子树 → 排除
			rowOf("scan/notes.txt", fileOf("notes.txt", "x")),          // 散文件 → 排除
		];
		const merged = C().mergeRows({
			stem: "病例A", entryName: "病例A.mrxs", entryFile: entry,
			rows, prefixSegs: ["scan", "病例A"],
		});
		expect(merged.ok).toBe(true);
		const rels = (merged.rows || []).map((r) => r.relPath);
		expect(rels).toEqual([
			"病例A.mrxs",
			"病例A/Slidedat.ini",
			"病例A/Index.dat",
		]);
		// 大小写不同的文件夹名同样归一到入口词干
		const merged2 = C().mergeRows({
			stem: "CMU-1", entryName: "CMU-1.mrxs", entryFile: entry,
			rows: [rowOf("cmu-1/Data0000.dat", fileOf("Data0000.dat", "x")),
			       rowOf("cmu-1/Slidedat.ini", fileOf("Slidedat.ini", SLIDEDAT))],
			prefixSegs: ["cmu-1"],
		});
		expect((merged2.rows || []).map((r) => r.relPath))
			.toEqual(["CMU-1.mrxs", "CMU-1/Data0000.dat", "CMU-1/Slidedat.ini"]);
	});

	it("成员上限：超过 maxMembers 在进 planner 之前拒绝", () => {
		const entry = fileOf("a.mrxs", "x");
		const rows = [rowOf("a/Slidedat.ini", fileOf("Slidedat.ini", SLIDEDAT))];
		for (let i = 0; i < 5; i++) rows.push(rowOf(`a/Data${i}.dat`, fileOf(`Data${i}.dat`, "x")));
		const merged = C().mergeRows({
			stem: "a", entryName: "a.mrxs", entryFile: entry,
			rows, prefixSegs: ["a"], maxMembers: 3,
		});
		expect(merged.ok).toBe(false);
		expect(merged.code).toBe("too_many_members");
	});

	it("装配结果满足真实 planner：sniffMrxBundle 与 planMrxBundle 都接受", async () => {
		const entry = fileOf("病例A.mrxs", "entry-bytes");
		const rows = [
			rowOf("sel/病例A/Slidedat.ini", fileOf("Slidedat.ini", SLIDEDAT)),
			rowOf("sel/病例A/Index.dat", fileOf("Index.dat", "index-bytes")),
			rowOf("sel/病例A/Data0000.dat", fileOf("Data0000.dat", "pix")),
			rowOf("sel/other.txt", fileOf("other.txt", "noise")),
		];
		const plan = C().planCompanionFolder({ stem: "病例A", folderName: "sel", rows });
		expect(plan.status).toBe("parent");
		const merged = C().mergeRows({
			stem: "病例A", entryName: "病例A.mrxs", entryFile: entry,
			rows, prefixSegs: plan.prefixSegs,
		});
		expect(merged.ok).toBe(true);
		const sniff = E.sniffMrxBundle(merged.rows as never);
		expect(sniff.supported).toBe(true);
		if (sniff.supported) {
			expect(sniff.stem).toBe("病例A");
		}
		const full = await E.planBundle(merged.rows as never);
		expect(full.stem).toBe("病例A");
		expect(full.members.map((m: { name: string }) => m.name).sort()).toEqual([
			"病例A.mrxs", "病例A/Data0000.dat", "病例A/Index.dat", "病例A/Slidedat.ini",
		].sort());
	});
});

// ----------------------------------------------------- collectDirectoryHandle

describe("collectDirectoryHandle（假 FileSystemDirectoryHandle 遍历）", () => {
	function fHandle(name: string, file: unknown) {
		return { kind: "file", name, getFile: async () => file };
	}
	function dHandle(name: string, children: unknown[]) {
		return {
			kind: "directory", name,
			async *values() { for (const c of children) yield c; },
		};
	}

	it("values() 递归遍历，relPath 以所选文件夹名开头（与 webkitRelativePath 同形）", async () => {
		const tree = dHandle("病例A", [
			fHandle("Slidedat.ini", fileOf("Slidedat.ini", SLIDEDAT)),
			dHandle("sub", [fHandle("Data0000.dat", fileOf("Data0000.dat", "x"))]),
			fHandle("Index.dat", fileOf("Index.dat", "x")),
		]);
		const rows = await C().collectDirectoryHandle(tree);
		expect(rows.map((r) => r.relPath)).toEqual([
			"病例A/Slidedat.ini",
			"病例A/sub/Data0000.dat",
			"病例A/Index.dat",
		]);
		expect(rows.every((r) => r.file)).toBe(true);
	});

	it("成员数超上限立即抛 too_many_members", async () => {
		const tree = dHandle("f", [
			fHandle("a", "x"), fHandle("b", "x"), fHandle("c", "x"),
		].map((h, i) => fHandle(`f${i}`, fileOf(`f${i}`, "x"))));
		await expect(C().collectDirectoryHandle(tree, { maxMembers: 2 }))
			.rejects.toMatchObject({ code: "too_many_members" });
	});
});

// ------------------------------------------ collectCompanionFolder（复核 r2） --

describe("collectCompanionFolder（picked 不同名 → 只遍历同名直接子目录）", () => {
	function fHandle(name: string, file: unknown) {
		return { kind: "file", name, getFile: async () => file };
	}
	function dHandle(name: string, children: unknown[]) {
		return {
			kind: "directory", name,
			async *values() { for (const c of children) yield c; },
		};
	}
	const dataFiles = () => [
		fHandle("Slidedat.ini", fileOf("Slidedat.ini", SLIDEDAT)),
		fHandle("Index.dat", fileOf("Index.dat", "x")),
	];

	it("picked 目录名与 stem 不同名：只遍历同名的直接子目录，relPath 保留 picked 前缀", async () => {
		const tree = dHandle("Slides", [
			dHandle("病例B", [...dataFiles(), fHandle("Data0000.dat", fileOf("D0", "x"))]),
			dHandle("病例A", [...dataFiles(), fHandle("Data0000.dat", fileOf("D0", "x"))]),
			fHandle("notes.txt", fileOf("notes.txt", "x")),
		]);
		const rows = await C().collectCompanionFolder(tree, "病例A", { maxMembers: 8192 });
		// 病例B 排在前面也只取病例A；relPath 以 picked 目录名开头 → plan=parent
		expect(rows.map((r) => r.relPath)).toEqual([
			"Slides/病例A/Slidedat.ini",
			"Slides/病例A/Index.dat",
			"Slides/病例A/Data0000.dat",
		]);
		const plan = C().planCompanionFolder({ stem: "病例A", folderName: "Slides", rows });
		expect(plan.status).toBe("parent");
		expect(plan.prefixSegs).toEqual(["Slides", "病例A"]);
	});

	it("picked 目录本身就与 stem 同名：整棵树照常遍历", async () => {
		const tree = dHandle("病例A", [...dataFiles()]);
		const rows = await C().collectCompanionFolder(tree, "病例A", {});
		expect(rows.map((r) => r.relPath)).toEqual([
			"病例A/Slidedat.ini", "病例A/Index.dat",
		]);
	});

	it("成员上限只作用于目标子树：巨大的兄弟目录不被遍历、不触发 too_many_members", async () => {
		const bigSiblings = Array.from({ length: 500 }, (_v, i) =>
			fHandle(`big${i}.dat`, fileOf(`big${i}.dat`, "x")));
		const tree = dHandle("Slides", [
			dHandle("病例B", bigSiblings),          // 会撞上限的兄弟子树
			dHandle("病例A", [...dataFiles()]),      // 目标子树（很小）
		]);
		const rows = await C().collectCompanionFolder(tree, "病例A", { maxMembers: 5 });
		expect(rows).toHaveLength(2);
		expect(rows.every((r) => r.relPath.startsWith("Slides/病例A/"))).toBe(true);
	});

	it("目标子树自身超上限 → too_many_members（装配前拒绝）", async () => {
		const tree = dHandle("病例A", [
			...dataFiles(),
			...Array.from({ length: 10 }, (_v, i) => fHandle(`f${i}`, fileOf(`f${i}`, "x"))),
		]);
		await expect(C().collectCompanionFolder(tree, "病例A", { maxMembers: 3 }))
			.rejects.toMatchObject({ code: "too_many_members" });
	});

	it("找不到同名子目录 → 按整棵树遍历（调用方拿到行后由 plan 判 reject）", async () => {
		const tree = dHandle("Slides", [
			dHandle("病例B", [...dataFiles()]),
			fHandle("readme.txt", fileOf("readme.txt", "x")),
		]);
		const rows = await C().collectCompanionFolder(tree, "病例A", {});
		expect(rows.length).toBeGreaterThanOrEqual(3);
	});
});

describe("supportsFolderPicker（webkitdirectory 回退可用性）", () => {
	it("有 webkitdirectory 属性 → true；没有 → false；createElement 抛错 → false", () => {
		expect(C().supportsFolderPicker({ doc: {
			createElement: () => ({ type: "", }),   // 无 webkitdirectory 属性的对象
		} })).toBe(false);
		// in 检查对任意对象都成立性测试：带属性的对象 → true
		expect(C().supportsFolderPicker({ doc: {
			createElement: () => ({ type: "file", webkitdirectory: false }),
		} })).toBe(true);
		expect(C().supportsFolderPicker({ doc: {
			createElement: () => { throw new Error("no DOM"); },
		} })).toBe(false);
		expect(C().supportsFolderPicker({ doc: null })).toBe(false);
	});
});

// --------------------------------------------------------------- pickDirectory

describe("pickDirectory（startIn 只在真有句柄时传；TypeError 重试；取消/回退）", () => {
	function winWith(impl: (...args: unknown[]) => unknown) {
		return { showDirectoryPicker: vi.fn(impl) } as unknown as {
			showDirectoryPicker: ReturnType<typeof vi.fn>;
		};
	}
	const handle = { kind: "directory", name: "picked" };

	it("startIn 仅在提供了句柄时进入 options（连同 id 与 mode:'read'）", async () => {
		const w1 = winWith(() => handle);
		await C().pickDirectory({ id: "mrxs-companion", startIn: { my: "fh" } }, { win: w1 });
		expect(w1.showDirectoryPicker).toHaveBeenCalledTimes(1);
		expect(w1.showDirectoryPicker.mock.calls[0][0]).toEqual({
			id: "mrxs-companion", mode: "read", startIn: { my: "fh" },
		});
		const w2 = winWith(() => handle);
		await C().pickDirectory({ id: "mrxs-companion" }, { win: w2 });
		expect(w2.showDirectoryPicker.mock.calls[0][0]).toEqual({
			id: "mrxs-companion", mode: "read",
		});
		expect(Object.keys(w2.showDirectoryPicker.mock.calls[0][0] as object))
			.not.toContain("startIn");
	});

	it("成功：返回句柄与名字", async () => {
		const w = winWith(() => handle);
		const res = await C().pickDirectory({ id: "x" }, { win: w });
		expect(res).toMatchObject({ ok: true, handle, name: "picked" });
	});

	it("AbortError → cancelled（调用方安静保持「需要文件夹」状态）", async () => {
		const w = winWith(() => Promise.reject(Object.assign(new Error(), { name: "AbortError" })));
		const res = await C().pickDirectory({ id: "x" }, { win: w });
		expect(res).toEqual({ ok: false, reason: "cancelled" });
		expect(w.showDirectoryPicker).toHaveBeenCalledTimes(1);
	});

	it("带 startIn 抛 TypeError → 不带 startIn 重试一次并成功", async () => {
		const w = winWith((_opts: unknown) => {
			if (w.showDirectoryPicker.mock.calls.length === 1) {
				return Promise.reject(Object.assign(new Error(), { name: "TypeError" }));
			}
			return handle;
		});
		const res = await C().pickDirectory({ id: "x", startIn: { h: 1 } }, { win: w });
		expect(res).toMatchObject({ ok: true, name: "picked" });
		expect(w.showDirectoryPicker).toHaveBeenCalledTimes(2);
		expect(w.showDirectoryPicker.mock.calls[1][0]).toEqual({ id: "x", mode: "read" });
	});

	it("无 startIn 时 SecurityError / 其他错误 → security / error；无 API → unsupported", async () => {
		const wSec = winWith(() => Promise.reject(Object.assign(new Error(), { name: "SecurityError" })));
		expect(await C().pickDirectory({ id: "x" }, { win: wSec }))
			.toMatchObject({ ok: false, reason: "security" });
		const wErr = winWith(() => Promise.reject(new Error("boom")));
		expect(await C().pickDirectory({ id: "x" }, { win: wErr }))
			.toMatchObject({ ok: false, reason: "error" });
		expect(C().supportsDirectoryPicker({})).toBe(false);
		expect(C().supportsDirectoryPicker(undefined)).toBe(false);
	});

	it("AbortError in TypeError retry → cancelled（状态同样保持）", async () => {
		const w = winWith(() => Promise.reject(Object.assign(new Error(), { name: "AbortError" })));
		const res = await C().pickDirectory({ id: "x", startIn: { h: 1 } }, { win: w });
		expect(res).toEqual({ ok: false, reason: "cancelled" });
	});
});

// ------------------------------------------- 页面接线（转换器页源码级约定） --

describe("转换器页接线（/tools/slides）", () => {
	it("模板：协助面板存在——aria-live 状态区、主按钮、取消按钮、提示行；无内联样式", () => {
		expect(toolsShellSrc).toContain('id="companion-section"');
		expect(toolsShellSrc).toMatch(/id="companion-message"[^>]*role="status"[\s\S]*?aria-live="polite"/);
		expect(toolsShellSrc).toContain('id="companion-pick-btn"');
		expect(toolsShellSrc).toContain('id="companion-cancel-btn"');
		expect(toolsShellSrc).toContain('data-i18n="tools.companion.hint"');
		expect(toolsShellSrc).toMatch(/<section id="companion-section"[^>]*hidden>/);
		// CSP：无内联样式/脚本
		expect(toolsShellSrc).not.toMatch(/\sstyle="/);
		expect(toolsShellSrc).not.toMatch(/<script>(?!\s*<)/);
		// 共享模块在本页加载（先于页面模块）
		expect(toolsShellSrc).toMatch(
			/src="\/static\/upload\/mrxs-companion\.js[^"]*"[^>]*><\/script>\s*<script type="module" src="\/static\/tools\/tools-slides\.js/);
		expect(toolsCssSrc).toContain(".companion-section");
		expect(toolsCssSrc).toContain(".companion-actions");
	});

	it("散文件 .mrxs 进入协助状态；文件夹/目录 drop 不进入；缺成员失败后面板以「重新选择」口径重出", () => {
		// prepareBundleSource 的分流：仅散文件输入（非 fromFolder、非协助中）
		expect(toolsPageSrc).toContain("if (!opts.fromFolder && !page.companion) {");
		expect(toolsPageSrc).toContain("C.detectNeedsFolder(rows)");
		// 拖入句柄在 drop 事件内同步捕获（getAsFileSystemHandle）
		expect(toolsPageSrc).toMatch(/handleDropData\(dt\)[\s\S]*?getAsFileSystemHandle/);
		// webkitdirectory 回退分支在协助激活时改道（不经过整包入口）
		expect(toolsPageSrc).toMatch(/if \(page\.companion\) \{[\s\S]*?companionFolderResolved\(bundleRows\(files\), folderName\)/);
		// 缺成员失败：mrxs-bundle + 协助在 → 面板以 retry 口径重出
		//（正文 = tools.companion.needed.retry，按钮 = tools.companion.pick.retry）
		expect(toolsPageSrc).toMatch(/info\.kind === 'mrxs-bundle'[\s\S]*?paintCompanionPanel\(true\)/);
		expect(toolsPageSrc).toMatch(/setCompanionMsg\(retry \? 'tools\.companion\.needed\.retry' : 'tools\.companion\.needed'/);
		expect(toolsPageSrc).toMatch(/setCompanionPick\(retry \? 'tools\.companion\.pick\.retry' : 'tools\.companion\.pick'/);
		// 复核 r3 A：协助装配后的缺成员错误只说缺什么（「重新选择」的引导
		// 由下方面板承担；普通文件夹/拖入路径保持原句不动）
		expect(toolsPageSrc).toMatch(/isMrxs && page\.companion\) \{[\s\S]{0,200}tools\.err\.bundle_incomplete\.mrxs\.retry/);
		// 遍历走 collectCompanionFolder（picked 不同名 → 只遍历同名直接子目录）
		expect(toolsPageSrc).toMatch(/C\.collectCompanionFolder\(handle, page\.companion\.stem,/);
		// 准备成功 / 取消：协助状态收口
		expect(toolsPageSrc).toMatch(/page\.prep = prep;\s*\n\s*page\.companion = null;/);
		// 主按钮 startIn 只在真有句柄时传
		expect(toolsPageSrc).toMatch(/C\.pickDirectory\(\{ id: 'mrxs-companion', startIn \}\)/);
		// 新输入尝试清掉协助状态；resetFlowPanels 只藏面板不清上下文
		expect(toolsPageSrc).toMatch(/beginInputAttempt\(\) \{[\s\S]*?clearCompanion\(\);/);
		expect(toolsPageSrc).toMatch(/hideCompanionPanel\(\);/);
		// 按钮聚焦；语言切换重渲染面板文案（正文/按钮/提示行）
		expect(toolsPageSrc).toContain("els.companionPickBtn.focus()");
		expect(toolsPageSrc).toMatch(/companionMsg\.key[\s\S]*?companionMessage\.textContent = t\(companionMsg\.key/);
		expect(toolsPageSrc).toMatch(/companionHintMsg\.key[\s\S]*?companionHint\.textContent = t\(companionHintMsg\.key/);
		// 提示行两态：可用 → 权限提示；彻底不可用（无 picker 且无
		// webkitdirectory）→ 如实说明并隐藏死按钮
		expect(toolsPageSrc).toMatch(/!pickerSupported && !folderInputSupported/);
		expect(toolsPageSrc).toMatch(/setCompanionHint\('tools\.companion\.unsupported'\)/);
		expect(toolsPageSrc).toMatch(/setCompanionHint\('tools\.companion\.hint'\)/);
		expect(toolsPageSrc).toMatch(/els\.companionPickBtn\.hidden = true/);
		// 忙碌时协助按钮一并禁用
		expect(toolsPageSrc).toMatch(/\[els\.pickFileBtn, els\.pickFolderBtn,\s*\n?\s*els\.companionPickBtn, els\.companionCancelBtn\]/);
	});

	it("i18n：tools.companion.* 与 upload.inline.needFolder/companion.* zh/en 成对", () => {
		for (const key of [
			"tools.companion.title", "tools.companion.needed", "tools.companion.pick",
			"tools.companion.cancel", "tools.companion.hint", "tools.companion.unsupported",
			"tools.companion.needed.retry", "tools.companion.pick.retry",
			"tools.companion.mismatch.title", "tools.companion.mismatch.body",
			"tools.companion.mismatch.use", "tools.companion.mismatch.reselect",
			"tools.companion.not_data_folder", "tools.companion.read_failed",
			"tools.companion.too_many", "tools.err.bundle_incomplete.mrxs.retry",
			"upload.inline.stage.needFolder", "upload.inline.needFolder.mrxs",
			"upload.inline.needFolder.notDataFolder", "upload.inline.needFolder.readFailed",
			"upload.inline.needFolder.tooMany", "upload.inline.needFolder.retry",
			"upload.inline.needFolder.retryMissing", "upload.inline.needFolder.missingMore",
			"upload.inline.needFolder.toast",
			"upload.inline.companion.mismatch", "upload.inline.companion.use",
			"upload.inline.companion.hint", "upload.inline.companion.unsupported",
			"upload.inline.action.chooseFolderNamed", "upload.inline.action.reselectFolder",
		]) {
			const pattern = new RegExp(`"${key}"\\s*:`, "g");
			const hits = i18nSrc.match(pattern) || [];
			expect(hits.length, key).toBe(2);   // zh 表 + en 表各一条
		}
		// app.js 兜底表同样双语成对
		for (const key of [
			"upload.inline.stage.needFolder", "upload.inline.needFolder.mrxs",
			"upload.inline.needFolder.notDataFolder", "upload.inline.needFolder.retry",
			"upload.inline.needFolder.retryMissing", "upload.inline.needFolder.missingMore",
			"upload.inline.needFolder.toast", "upload.inline.action.reselectFolder",
			"upload.inline.companion.mismatch", "upload.inline.companion.hint",
			"upload.inline.companion.unsupported",
			"upload.inline.companion.use", "upload.inline.action.chooseFolderNamed",
		]) {
			const line = appSrc.match(new RegExp(`"${key}": \\{ zh: ?("[^"]*"), en: ?("[^"]*") \\}`));
			expect(line, key).toBeTruthy();
			expect(line?.[2]?.length, key).toBeGreaterThan(2);
		}
	});

	it("主站模板加载共享模块（先于 inline-convert / app.js）", () => {
		const at = (needle: string) => indexHtml.indexOf(needle);
		const companionAt = at("/static/upload/mrxs-companion.js");
		expect(companionAt).toBeGreaterThan(0);
		expect(companionAt).toBeGreaterThan(at("/static/upload/slide-sniff.js"));
		expect(companionAt).toBeLessThan(at("/static/upload/inline-convert.js"));
		expect(companionAt).toBeLessThan(at("/static/app.js"));
	});

	it("主站接线：协助行的选择/回退/确认都在行内（app.js 源码级约定）", () => {
		// 行附注是 aria-live（确认/拒绝的状态变化读屏可闻）
		expect(appSrc).toMatch(/upload-item-status";[\s\S]{0,120}setAttribute\("aria-live", "polite"\)/);
		// drop 事件内同步捕获 FileSystemFileHandle（getAsString 之外唯一的
		// 事件内窗口），并按 File 传给 uploadFile → enqueue → _fileHandle
		expect(appSrc).toMatch(/function handleWorkbenchDrop\(dt\)[\s\S]*?getAsFileSystemHandle/);
		expect(appSrc).toMatch(/uploadFile\(files\[f2\], handle \? \{ fileHandle: handle \} : undefined\)/);
		expect(appSrc).toMatch(/fileHandle: opts\.fileHandle \|\| null/);
		// 协助行的「选择文件夹」：目录选择器优先；无 API/SecurityError → 同
		// 手势回退 webkitdirectory input（pendingCompanion 标记待处理）
		expect(appSrc).toContain("function companionPickForEntry(entry)");
		expect(appSrc).toMatch(/C\.pickDirectory\(\{ id: "mrxs-companion", startIn \}\)/);
		expect(appSrc).toMatch(/reason === "cancelled"\) return;\s*\/\/ 用户取消：安静保持待选态/);
		expect(appSrc).toMatch(/pendingCompanion = \{ id: entry\.id, stem: stem \}/);
		// 遍历走 collectCompanionFolder（picked 不同名 → 只遍历同名直接子目录）
		expect(appSrc).toMatch(/C\.collectCompanionFolder\(res\.handle, stem,/);
		// 遍历失败 → traversalError（行回 need_folder + read 原因）
		expect(appSrc).toContain("{ traversalError: true }");
		// importPickFolder 的协助改道；确认/重选动作经 orch.act
		expect(appSrc).toContain("resolveCompanionFolder(pc.id, rows, first)");
		expect(appSrc).toContain('inlineOrch.act(entry.id, "companionUse")');
		expect(appSrc).toContain('inlineOrch.act(entry.id, "companionReselect")');
		// 行内文案：need_folder / 确认 / 拒绝（读失败、超上限）、装配后缺成员
		// 的重试说明、权限提示与「彻底不可用」各有键
		expect(appSrc).toContain('tt("upload.inline.companion.mismatch"');
		expect(appSrc).toContain('tt("upload.inline.needFolder.notDataFolder"');
		expect(appSrc).toContain('tt("upload.inline.needFolder.readFailed"');
		expect(appSrc).toContain('tt("upload.inline.needFolder.tooMany"');
		expect(appSrc).toContain('tt("upload.inline.needFolder.mrxs"');
		expect(appSrc).toContain('tt("upload.inline.needFolder.retry"');
		expect(appSrc).toContain('tt("upload.inline.needFolder.retryMissing"');
		expect(appSrc).toContain('missingListText(entry.companionMissing)');
		// 复核 r3 C：确认态的「重新选择」回的是文件夹（专用文案键）
		expect(appSrc).toMatch(/kind === "companionReselect"\) \{[\s\S]{0,160}upload\.inline\.action\.reselectFolder/);
		// 复核 r4：装配后缺成员的 toast 用专用短提示（不再说「不支持本机转换」）
		expect(inlineSrc).toMatch(/e\.companionRetry && e\.companion\) \{[\s\S]{0,200}upload\.inline\.needFolder\.toast/);
		expect(appSrc).toContain('tt("upload.inline.companion.hint")');
		expect(appSrc).toContain('tt("upload.inline.companion.unsupported")');
		// 主按钮 is-primary（chooseFolder / companionUse）；彻底不可用时不给
		// 死按钮（folderPickSupported 把关 need_folder 与 convert_failed 重试）
		expect(appSrc).toMatch(/kind === "chooseFolder" \|\| kind === "companionUse"\s*\n\s*\? "upload-item-btn is-primary"/);
		expect(appSrc).toContain("function folderPickSupported()");
		expect(appSrc).toMatch(/if \(folderPickSupported\(\)\) out\.push\("chooseFolder"\);\s*\n\s*out\.push\("reselect", "cancel"\);/);
		expect(appSrc).toMatch(/entry\.companion && entry\.companionRetry[\s\S]{0,200}folderPickSupported\(\)\) out\.push\("chooseFolder"\);/);
	});
});
