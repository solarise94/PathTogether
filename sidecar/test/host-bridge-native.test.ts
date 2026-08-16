/**
 * 真实 static/host-bridge.js 路由器测试（2026-08-16）。
 *
 * 背景：plugin-sdk-bridge.test.ts 用假 host 测 SDK 客户端，真实 host-bridge.js
 * 此前没有任何加载测试——demo 上「插件脚本先于 app.js 加载 → negotiate 立刻发出
 * → reqHandlers 尚未注册 → unknown_method」的时序 bug 因此漏网。本文件加载真源码
 * （构造最小 window 后 new Function 注入执行），锁定：
 *   - bridge.negotiate 由路由器原生应答（无需 app.js 注册，demo 时序修复）；
 *   - 协商不兼容回 version_incompatible；
 *   - 其余未注册 method 仍回 unknown_method（路由器行为不变）；
 *   - BridgeVersion 未加载时同 major 兜底接受（信封层已过 compat）。
 */
import { describe, expect, it } from "vitest";
import { createRequire } from "node:module";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, resolve } from "node:path";

const here = dirname(fileURLToPath(import.meta.url));
const require = createRequire(import.meta.url);
const BridgeVersion = require(resolve(here, "../../static/bridge-version.js"));
const hostBridgeSrc = readFileSync(resolve(here, "../../static/host-bridge.js"), "utf8");

interface Envelope {
	kind: string;
	protocolVersion: string;
	pluginInstallationId: string;
	requestId?: string;
	method?: string;
	payload?: Record<string, unknown>;
	ok?: boolean;
	result?: unknown;
	error?: { code: string; message?: string };
}

function freshHost(withBV = true) {
	const w: Record<string, unknown> = {};
	if (withBV) w.BridgeVersion = BridgeVersion;
	new Function("window", hostBridgeSrc)(w);
	const host = w.HostBridgeHost as Record<string, (...args: unknown[]) => void>;
	return { window: w, host };
}

// 注册一个最小插件接收器，经 postFromPlugin（host 盖章身份）发起请求，收集回信
function pluginMailbox(host: Record<string, (...args: unknown[]) => void>, pluginId: string) {
	const received: Envelope[] = [];
	host.registerPlugin(pluginId, (env: Envelope) => received.push(env));
	return {
		received,
		post(method: string, payload: Record<string, unknown> = {}) {
			host.postFromPlugin(pluginId, {
				kind: "request",
				protocolVersion: "1.0.0",
				requestId: "req_test_1",
				method,
				payload,
			});
		},
	};
}

const tick = () => new Promise((r) => setTimeout(r, 0));

describe("host-bridge router — native bridge.negotiate (load-order fix)", () => {
	it("answers bridge.negotiate without any app-side onRequest registration", async () => {
		const { host } = freshHost();
		const mb = pluginMailbox(host, "sample-annotator");
		mb.post("bridge.negotiate", { protocolVersion: "1.0.0" });
		await tick();
		expect(mb.received).toHaveLength(1);
		const res = mb.received[0];
		expect(res.kind).toBe("response");
		expect(res.ok).toBe(true);
		expect(res.result).toMatchObject({ ok: true, protocolVersion: "1.0.0" });
	});

	it("rejects incompatible remote version with version_incompatible", async () => {
		const { host } = freshHost();
		const mb = pluginMailbox(host, "sample-annotator");
		mb.post("bridge.negotiate", { protocolVersion: "2.0.0" });
		await tick();
		expect(mb.received[0].ok).toBe(false);
		expect(mb.received[0].error?.code).toBe("version_incompatible");
	});

	it("still answers unknown_method for unregistered non-negotiate methods", async () => {
		const { host } = freshHost();
		const mb = pluginMailbox(host, "sample-annotator");
		mb.post("definitely.not.registered");
		await tick();
		expect(mb.received[0].ok).toBe(false);
		expect(mb.received[0].error?.code).toBe("unknown_method");
	});

	it("BV missing: native negotiate falls back to same-major accept (envelope passed compat)", async () => {
		const { host } = freshHost(false);
		const mb = pluginMailbox(host, "sample-annotator");
		mb.post("bridge.negotiate", { protocolVersion: "1.0.0" });
		await tick();
		expect(mb.received[0].ok).toBe(true);
		expect(mb.received[0].result).toMatchObject({ protocolVersion: "1.0.0" });
	});
});
