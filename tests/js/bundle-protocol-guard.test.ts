/**
 * bundle 协议版本守护（test-review P3-18-3）。
 *
 * CROSS_REPO_CONTRACT: histopilot bridge bundle 发布自兄弟仓 HistoPilot，其
 * manifest（bridgeProtocolVersion）不在本仓——host 侧 static/bridge-version.js
 * 的 SUPPORTED_MAJORS 必须含 1（当前协议 major），否则发布 bundle 会被握手拒收。
 * 跨仓方向的完整协商测试归 HistoPilot 仓；本仓只守护 host 端常量与仓内 manifest
 * 的一致性（HistoPilot 仓改动协议 major 时，这里应当红，提醒同步本仓常量）。
 *
 * 仓内守护：plugins 下各插件目录的 manifest.json（sample 插件）的
 * bridgeProtocolVersion major 必须 ∈ host SUPPORTED_MAJORS——它们随本仓一起
 * 发布、走同一握手。
 */
import { describe, expect, it } from "vitest";
import { createRequire } from "node:module";
import { readdirSync, readFileSync, existsSync, statSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const require = createRequire(import.meta.url);
const BridgeVersion = require(resolve(here, "../../static/bridge-version.js")) as {
	PROTOCOL_VERSION: string;
	SUPPORTED_MAJORS: number[];
	parseMajor: (v: unknown) => number | null;
};

function repoPluginManifests(): string[] {
	const pluginsDir = resolve(here, "../../plugins");
	const out: string[] = [];
	for (const name of readdirSync(pluginsDir)) {
		const mf = resolve(pluginsDir, name, "manifest.json");
		if (existsSync(mf) && statSync(mf).isFile()) out.push(mf);
	}
	return out;
}

describe("bundle 协议版本守护（host SUPPORTED_MAJORS）", () => {
	it("host SUPPORTED_MAJORS 含 1（当前协议 major；HistoPilot bundle 依赖）", () => {
		// CROSS_REPO_CONTRACT: histopilot sidecar/plugin bundle 的
		// bridgeProtocolVersion 由兄弟仓发布（本仓无该 manifest）。host 必须
		// 继续接受 major 1，直到两仓协同升版。
		expect(BridgeVersion.SUPPORTED_MAJORS).toContain(1);
		expect(BridgeVersion.parseMajor(BridgeVersion.PROTOCOL_VERSION)).toBe(1);
	});

	it("仓内 plugin manifest 的 bridgeProtocolVersion major ∈ host SUPPORTED_MAJORS", () => {
		const manifests = repoPluginManifests();
		expect(manifests.length).toBeGreaterThan(0);
		for (const mf of manifests) {
			const m = JSON.parse(readFileSync(mf, "utf8")) as { bridgeProtocolVersion?: string };
			const major = BridgeVersion.parseMajor(m.bridgeProtocolVersion);
			expect(major, `${mf}: bridgeProtocolVersion 不可解析`).not.toBeNull();
			expect(
				BridgeVersion.SUPPORTED_MAJORS,
				`${mf}: bridge major ${major} 不在 host SUPPORTED_MAJORS`,
			).toContain(major as number);
		}
	});
});
