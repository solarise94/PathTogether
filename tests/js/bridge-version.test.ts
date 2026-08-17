/**
 * bridge-version 共享模块测试（Stage 5-1）。
 *
 * UMD 模块 static/bridge-version.js：node 下经 module.exports 暴露（浏览器下挂
 * window.BridgeVersion）。host-bridge.js / bridge-client.js 复用其 compat / 常量。
 *
 * 版本语义区分（与 Python plugins/sdk/manifest.py 的 manifest 加载期 N/N-1 协商不同）：
 *   - compat(v, local)：运行时每条消息强制同 major；
 *   - negotiate(remote, {supportedMajors})：握手期协商，remote major ∈ supportedMajors
 *     即兼容；默认 SUPPORTED_MAJORS=[1]（仅当前 major），N/N-1 逻辑经注入参数验证。
 */
import { describe, expect, it } from "vitest";
import { createRequire } from "node:module";
import { fileURLToPath } from "node:url";
import { dirname, resolve } from "node:path";

// UMD 在 node 下走 module.exports 分支；用 createRequire 解析到仓库根 static/。
const here = dirname(fileURLToPath(import.meta.url));
const require = createRequire(import.meta.url);
const BridgeVersion = require(resolve(here, "../../static/bridge-version.js")) as {
	PROTOCOL_VERSION: string;
	SUPPORTED_MAJORS: number[];
	parseMajor: (v: unknown) => number | null;
	compat: (v: unknown, local?: string) => boolean;
	negotiate: (
		remote: unknown,
		opts?: { supportedMajors?: number[] },
	) =>
		| { ok: true; protocolVersion: string }
		| { ok: false; error: { code: string; message: string; retryable: boolean } };
};

describe("BridgeVersion constants", () => {
	it("exposes PROTOCOL_VERSION and SUPPORTED_MAJORS", () => {
		expect(BridgeVersion.PROTOCOL_VERSION).toBe("1.0.0");
		expect(BridgeVersion.SUPPORTED_MAJORS).toEqual([1]);
	});
});

describe("BridgeVersion.parseMajor", () => {
	it("parses major from semver-like strings", () => {
		expect(BridgeVersion.parseMajor("1.0.0")).toBe(1);
		expect(BridgeVersion.parseMajor("1.5.2")).toBe(1);
		expect(BridgeVersion.parseMajor("0.9.1")).toBe(0);
		expect(BridgeVersion.parseMajor("2.0.0")).toBe(2);
		expect(BridgeVersion.parseMajor("10.2.3")).toBe(10);
	});

	it("returns null for malformed / missing input", () => {
		expect(BridgeVersion.parseMajor("")).toBeNull();
		expect(BridgeVersion.parseMajor(null)).toBeNull();
		expect(BridgeVersion.parseMajor(undefined)).toBeNull();
		expect(BridgeVersion.parseMajor("v1.0.0")).toBeNull();
		expect(BridgeVersion.parseMajor("abc")).toBeNull();
	});
});

describe("BridgeVersion.compat (runtime same-major)", () => {
	it("treats same major as compatible (1.0.0 ↔ 1.5.2)", () => {
		expect(BridgeVersion.compat("1.0.0", "1.5.2")).toBe(true);
		expect(BridgeVersion.compat("1.5.2", "1.0.0")).toBe(true);
	});

	it("rejects different major (1.x ↔ 0.9 NOT compatible)", () => {
		expect(BridgeVersion.compat("1.2.0", "0.9.0")).toBe(false);
		expect(BridgeVersion.compat("0.9.0", "1.2.0")).toBe(false);
		expect(BridgeVersion.compat("2.0.0", "1.0.0")).toBe(false);
	});

	it("defaults local to PROTOCOL_VERSION", () => {
		expect(BridgeVersion.compat("1.9.9")).toBe(true);
		expect(BridgeVersion.compat("2.0.0")).toBe(false);
	});

	it("returns false for malformed input (never throws)", () => {
		expect(BridgeVersion.compat("", "1.0.0")).toBe(false);
		expect(BridgeVersion.compat(null)).toBe(false);
		expect(BridgeVersion.compat("v1.0.0")).toBe(false);
	});
});

describe("BridgeVersion.negotiate", () => {
	it("default SUPPORTED_MAJORS=[1]: accepts 1.x, rejects 0.x and 2.x", () => {
		expect(BridgeVersion.SUPPORTED_MAJORS).toEqual([1]);
		const ok = BridgeVersion.negotiate("1.5.2");
		expect(ok.ok).toBe(true);
		if (ok.ok) expect(ok.protocolVersion).toBe(BridgeVersion.PROTOCOL_VERSION);

		expect(BridgeVersion.negotiate("0.9.0").ok).toBe(false);
		expect(BridgeVersion.negotiate("2.0.0").ok).toBe(false);
	});

	it("ok response carries platform protocolVersion", () => {
		const r = BridgeVersion.negotiate("1.0.0");
		expect(r.ok).toBe(true);
		if (r.ok) expect(r.protocolVersion).toBe("1.0.0");
	});

	it("incompatible response is a structured error envelope (never throws)", () => {
		const r = BridgeVersion.negotiate("3.1.0");
		expect(r.ok).toBe(false);
		if (!r.ok) {
			expect(r.error.code).toBe("version_incompatible");
			expect(r.error.retryable).toBe(false);
			expect(r.error.message).toContain("3.1.0");
			expect(r.error.message).toContain("major");
		}
	});

	it("missing remote is incompatible", () => {
		expect(BridgeVersion.negotiate(null).ok).toBe(false);
		expect(BridgeVersion.negotiate(undefined).ok).toBe(false);
		expect(BridgeVersion.negotiate("").ok).toBe(false);
	});

	it("N/N-1 logic via injected supportedMajors=[2,1]: accepts 2.x & 1.x, rejects 3.x & 0.x", () => {
		// 假设平台演进到支持 major {2, 1}（N/N-1）：2.x 与 1.x 兼容
		expect(BridgeVersion.negotiate("2.3.4", { supportedMajors: [2, 1] }).ok).toBe(true);
		expect(BridgeVersion.negotiate("1.9.9", { supportedMajors: [2, 1] }).ok).toBe(true);
		// 未来 major（3）与更旧 major（0）均拒绝
		expect(BridgeVersion.negotiate("3.0.0", { supportedMajors: [2, 1] }).ok).toBe(false);
		expect(BridgeVersion.negotiate("0.5.0", { supportedMajors: [2, 1] }).ok).toBe(false);
	});
});
