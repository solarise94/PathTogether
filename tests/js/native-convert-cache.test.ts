/**
 * 原生参考结果缓存（tests/browser/native_cache.js）的失效逻辑单元测试：
 * 键 = sha256(CLI 二进制) + 输入指纹（路径+大小+mtime；目录则全体成员）
 * + 参数。CLI / 输入 / 参数任一变化都必须换键（旧条目不再命中）。
 *
 * 不跑真实 CLI：convert 函数注入假实现（写一份小产物 + 计数），
 * 失效判定 = 「注入的 convert 是否被再次调用」。
 */
import { afterEach, describe, expect, it } from "vitest";
import { createRequire } from "node:module";
import { mkdtempSync, mkdirSync, rmSync, writeFileSync, utimesSync, readFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";

const require = createRequire(import.meta.url);
const nativeCache = require("../browser/native_cache.js");

let cacheDir: string | null = null;
let workDir: string | null = null;

function freshDirs() {
	cacheDir = mkdtempSync(join(tmpdir(), "native-cache-test-"));
	workDir = mkdtempSync(join(tmpdir(), "native-cache-work-"));
	process.env.PT_NATIVE_CACHE = cacheDir;
	return { cacheDir, workDir };
}

afterEach(() => {
	delete process.env.PT_NATIVE_CACHE;
	for (const d of [cacheDir, workDir]) {
		if (d) rmSync(d, { recursive: true, force: true });
	}
	cacheDir = null;
	workDir = null;
});

function fakeCli(dir: string, content = "fake-cli-bytes-v1") {
	const p = join(dir, "slide-transform");
	writeFileSync(p, content);
	return p;
}

function fakeInput(dir: string, content = "synthetic-input-v1") {
	const p = join(dir, "input.kfb");
	writeFileSync(p, content);
	return p;
}

/** 假转换：写一份由（输入路径 + 参数）决定的确定性产物。不读输入内容——
 * 目录输入不可 readFileSync，且失效判定只看「convert 是否被再次调用」。 */
function makeConverter(log: string[]) {
	return (out: string, ctx: { input: string; args: string[] }) => {
		log.push(out);
		writeFileSync(out, `converted:${ctx.input}:${ctx.args.join(",")}`);
	};
}

function runCached(cli: string, input: string, out: string, args: string[], convert: (out: string, ctx: { input: string; args: string[] }) => void) {
	return nativeCache.nativeConvertCached({ cli, input, output: out, args, convert });
}

describe("nativeConvertCached 失效逻辑", () => {
	it("未命中转换一次，同键第二次命中且不再转换", () => {
		const { workDir } = freshDirs();
		const cli = fakeCli(workDir);
		const input = fakeInput(workDir);
		const calls: string[] = [];
		const convert = makeConverter(calls);
		const r1 = runCached(cli, input, join(workDir, "a.tif"), [], convert);
		expect(r1.cached).toBe(false);
		expect(calls).toHaveLength(1);
		// 产物内容原样交给调用方（hardlink/copy）
		expect(readFileSync(join(workDir, "a.tif"), "utf8"))
			.toBe(readFileSync(calls[0], "utf8"));

		const r2 = runCached(cli, input, join(workDir, "b.tif"), [], convert);
		expect(r2.cached).toBe(true);
		expect(calls).toHaveLength(1); // 没有第二次转换
		expect(r2.sha256).toBe(r1.sha256);
		// 命中产物与首产物逐字节一致（同一 sha 已验，内容再直接对照）
		expect(readFileSync(join(workDir, "b.tif"), "utf8"))
			.toBe(readFileSync(calls[0], "utf8"));
	});

	it("输入内容变化（size/mtime 随之变）→ 键失效重转", () => {
		const { workDir } = freshDirs();
		const cli = fakeCli(workDir);
		const input = fakeInput(workDir);
		const calls: string[] = [];
		const convert = makeConverter(calls);
		runCached(cli, input, join(workDir, "a.tif"), [], convert);
		writeFileSync(input, "synthetic-input-v2-longer");
		const r2 = runCached(cli, input, join(workDir, "b.tif"), [], convert);
		expect(r2.cached).toBe(false);
		expect(calls).toHaveLength(2);
	});

	it("同内容同长度但 mtime 变化 → 键失效（mtime 是键的一部分）", () => {
		const { workDir } = freshDirs();
		const cli = fakeCli(workDir);
		const input = fakeInput(workDir);
		const calls: string[] = [];
		const convert = makeConverter(calls);
		runCached(cli, input, join(workDir, "a.tif"), [], convert);
		utimesSync(input, new Date(), new Date(Date.now() + 5000));
		const r2 = runCached(cli, input, join(workDir, "b.tif"), [], convert);
		expect(r2.cached).toBe(false);
		expect(calls).toHaveLength(2);
	});

	it("参数变化（--encoding compact）→ 键失效重转", () => {
		const { workDir } = freshDirs();
		const cli = fakeCli(workDir);
		const input = fakeInput(workDir);
		const calls: string[] = [];
		const convert = makeConverter(calls);
		runCached(cli, input, join(workDir, "a.tif"), [], convert);
		const r2 = runCached(cli, input, join(workDir, "b.tif"), ["--encoding", "compact"], convert);
		expect(r2.cached).toBe(false);
		expect(calls).toHaveLength(2);
	});

	it("CLI 二进制变化 → 键失效重转", () => {
		const { workDir } = freshDirs();
		const cli = fakeCli(workDir);
		const input = fakeInput(workDir);
		const calls: string[] = [];
		const convert = makeConverter(calls);
		runCached(cli, input, join(workDir, "a.tif"), [], convert);
		writeFileSync(cli, "fake-cli-bytes-v2-rebuilt");
		const r2 = runCached(cli, input, join(workDir, "b.tif"), [], convert);
		expect(r2.cached).toBe(false);
		expect(calls).toHaveLength(2);
	});

	it("目录输入：任一成员变化 → 键失效；无变化 → 命中", () => {
		const { workDir } = freshDirs();
		const cli = fakeCli(workDir);
		const bundleDir = join(workDir, "bundle");
		mkdirSync(join(bundleDir, "inner"), { recursive: true });
		writeFileSync(join(bundleDir, "slide.mrxs"), "entry-v1");
		writeFileSync(join(bundleDir, "inner", "Slidedat.ini"), "ini-v1");
		const calls: string[] = [];
		const convert = makeConverter(calls);
		const r1 = runCached(cli, bundleDir, join(workDir, "a.tif"), [], convert);
		expect(r1.cached).toBe(false);
		const r2 = runCached(cli, bundleDir, join(workDir, "b.tif"), [], convert);
		expect(r2.cached).toBe(true);
		expect(calls).toHaveLength(1);
		// 深层成员变化（同长度、内容不同 + mtime 变）→ 失效
		const ini = join(bundleDir, "inner", "Slidedat.ini");
		writeFileSync(ini, "ini-v2");
		utimesSync(ini, new Date(), new Date(Date.now() + 5000));
		const r3 = runCached(cli, bundleDir, join(workDir, "c.tif"), [], convert);
		expect(r3.cached).toBe(false);
		expect(calls).toHaveLength(2);
	});

	it("输出路径不参与键：同输入同参数、不同输出路径仍命中", () => {
		const { workDir } = freshDirs();
		const cli = fakeCli(workDir);
		const input = fakeInput(workDir);
		const calls: string[] = [];
		const convert = makeConverter(calls);
		runCached(cli, input, join(workDir, "somewhere-else", "a.tif"), [], convert);
		const r2 = runCached(cli, input, resolve(join(workDir, "b.tif")), [], convert);
		expect(r2.cached).toBe(true);
		expect(calls).toHaveLength(1);
	});

	it("命中时输出路径已存在（多场景复用同一路径）也能交付", () => {
		const { workDir } = freshDirs();
		const cli = fakeCli(workDir);
		const input = fakeInput(workDir);
		const calls: string[] = [];
		const convert = makeConverter(calls);
		const out = join(workDir, "shared-native.tif");
		const r1 = runCached(cli, input, out, [], convert);
		writeFileSync(out, "stale-bytes-from-a-previous-scenario"); // 模拟旧场景残留
		const r2 = runCached(cli, input, out, [], convert); // 命中 + EEXIST 重链
		expect(r2.cached).toBe(true);
		expect(calls).toHaveLength(1);
		expect(readFileSync(out, "utf8")).toBe(`converted:${input}:`);
		expect(r2.sha256).toBe(r1.sha256);
	});
});
