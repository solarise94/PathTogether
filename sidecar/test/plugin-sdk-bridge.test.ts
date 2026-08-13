/**
 * plugin-sdk bridge-client + plugin-permissions 单测（Stage 5-2）。
 *
 * 用 createRequire 加载仓库根 UMD 模块（node 下走 module.exports 分支）：
 *   - plugins/sdk/ui/bridge-client.js → { createPluginBridge }
 *   - static/plugin-permissions.js  → { METHOD_PERMISSIONS, checkPermission }
 *
 * bridge 传输在浏览器里是同窗口函数分发（调 window.HostBridgeHost._receiveFromPlugin，
 * host 经 registerPlugin 注册表路由回来）。node 单测构造 global.window + 假
 * HostBridgeHost：自实现 _receiveFromPlugin 配对响应，验证
 * request/response 配对、超时 reject bridge_timeout、negotiate 兼容/不兼容。
 */
import { describe, expect, it, beforeEach } from "vitest";
import { createRequire } from "node:module";
import { fileURLToPath } from "node:url";
import { dirname, resolve } from "node:path";

const here = dirname(fileURLToPath(import.meta.url));
const require = createRequire(import.meta.url);
const PluginSDK = require(resolve(here, "../../plugins/sdk/ui/bridge-client.js")) as {
	createPluginBridge: (opts?: {
		pluginId?: string;
		timeoutMs?: number;
		protocolVersion?: string;
	}) => {
		pluginId: string;
		negotiate: () => Promise<unknown>;
		request: (method: string, payload?: unknown) => Promise<unknown>;
		emit: (type: string, payload?: unknown) => void;
		onRequest: (method: string, fn: (payload: unknown, env?: unknown) => unknown) => void;
		onEvent: (type: string, fn: (payload: unknown) => void) => void;
		_onHostMessage: (env: unknown) => void;
	};
};
const PluginPermissions = require(resolve(here, "../../static/plugin-permissions.js")) as {
	METHOD_PERMISSIONS: Record<string, string>;
	checkPermission: (declaredPerms: string[], method: string) => null | {
		code: string;
		message: string;
		retryable: boolean;
	};
};

// ------------------------------------------------------------------------- //
// 假 HostBridgeHost：node 里模拟浏览器同窗口分发。
//   _receiveFromPlugin(env) 处理插件发来的 request，并回调插件接收函数（注册表）回 response。
// ------------------------------------------------------------------------- //
interface Env {
	kind: string;
	protocolVersion: string;
	pluginInstallationId: string;
	requestId?: string;
	eventId?: string;
	method?: string;
	type?: string;
	payload?: Record<string, unknown>;
	ok?: boolean;
	result?: unknown;
	error?: unknown;
}

interface FakeHost {
	_handlers: Record<string, (payload: unknown, env?: Env) => unknown>;
	_eventHandlers: Record<string, (payload: unknown) => void>;
	_receivers: Record<string, (env: Env) => void>;
	_requestBehavior?: Record<string, () => unknown>;
	events: Array<{ type: string; payload: unknown }>;
	registerPlugin: (pluginId: string, fn: (env: Env) => void) => void;
	_receiveFromPlugin: (env: Env) => void;
}

function makeFakeHost(): FakeHost {
	const h: FakeHost = {
		_handlers: {},
		_eventHandlers: {},
		_receivers: {},
		events: [],
		registerPlugin(pluginId, fn) {
			h._receivers[pluginId] = fn;
		},
		_receiveFromPlugin(env) {
			if (!env) return;
			if (env.kind === "request") {
				const replyTo = env.pluginInstallationId;
				const receiver = h._receivers[replyTo];
				const reply = (ok: boolean, result?: unknown, error?: unknown) => {
					if (receiver) {
						receiver({
							kind: "response",
							protocolVersion: "1.0.0",
							pluginInstallationId: replyTo,
							requestId: env.requestId,
							ok,
							result,
							error,
						} as Env);
					}
				};
				const fail = (e: unknown) => {
					const err = e as { code?: string; message?: string };
					reply(false, undefined, { code: err.code || "host_error", message: err.message || "err" });
				};
				try {
					// 注入行为优先（可配超时不响应 / 不兼容 / 抛错）
					const injected = h._requestBehavior ? h._requestBehavior[env.method!] : undefined;
					if (injected) {
						const r = injected();
						if (r === "NO_RESPONSE") return; // 不响应 → 触发超时
						reply(true, r);
						return;
					}
					const fn = h._handlers[env.method!];
					if (!fn) { reply(false, undefined, { code: "unknown_method" }); return; }
					reply(true, fn(env.payload || {}, env));
				} catch (e) {
					fail(e);
				}
				return;
			}
			if (env.kind === "event") {
				h.events.push({ type: env.type!, payload: env.payload });
				return;
			}
		},
	};
	return h;
}

// ------------------------------------------------------------------------- //
// 桥客户端 request/response 配对 + negotiate
// ------------------------------------------------------------------------- //
describe("PluginSDK bridge request/response", () => {
	let host: FakeHost;

	beforeEach(() => {
		host = makeFakeHost();
		// 浏览器同窗口环境下 window.HostBridgeHost 即假 host；PluginSDK 依赖
		// window 全局（_post / registerPlugin 分支）。
		(globalThis as Record<string, unknown>).window = { HostBridgeHost: host } as unknown as typeof globalThis;
	});

	it("registers its receiver into the host registry", () => {
		PluginSDK.createPluginBridge({ pluginId: "sample-annotator" });
		expect(typeof host._receivers["sample-annotator"]).toBe("function");
	});

	it("pairs request/response by requestId (echo result back)", async () => {
		const bridge = PluginSDK.createPluginBridge({ pluginId: "sample-annotator" });
		host._handlers["slide.getCurrent"] = () => ({ name: "a.svs", width: 100, height: 50, mppX: 0.5, mppY: 0.5 });
		const meta = await bridge.request("slide.getCurrent");
		expect(meta).toEqual({ name: "a.svs", width: 100, height: 50, mppX: 0.5, mppY: 0.5 });
	});

	it("rejects when host returns ok:false (structured error envelope)", async () => {
		const bridge = PluginSDK.createPluginBridge({ pluginId: "sample-annotator" });
		host._requestBehavior = { "annotation.read": () => {
			const e = new Error("boom") as Error & { code: string };
			e.code = "permission_denied";
			throw e;
		} };
		await expect(bridge.request("annotation.read")).rejects.toMatchObject({ code: "permission_denied" });
	});

	it("rejects with bridge_timeout when host does not respond (short injected timeout)", async () => {
		const bridge = PluginSDK.createPluginBridge({ pluginId: "sample-annotator", timeoutMs: 30 });
		host._requestBehavior = { "never.respond": () => "NO_RESPONSE" };
		await expect(bridge.request("never.respond")).rejects.toMatchObject({ code: "bridge_timeout" });
	});

	it("negotiate resolves when compatible", async () => {
		const bridge = PluginSDK.createPluginBridge({ pluginId: "sample-annotator" });
		host._handlers["bridge.negotiate"] = () => ({ ok: true, protocolVersion: "1.0.0" });
		const res = await bridge.negotiate();
		expect(res).toEqual({ ok: true, protocolVersion: "1.0.0" });
	});

	it("negotiate rejects with version_incompatible when host rejects", async () => {
		const bridge = PluginSDK.createPluginBridge({ pluginId: "sample-annotator" });
		host._requestBehavior = { "bridge.negotiate": () => {
			const e = new Error("bridge 协议版本不兼容") as Error & { code: string };
			e.code = "version_incompatible";
			throw e;
		} };
		await expect(bridge.negotiate()).rejects.toMatchObject({ code: "version_incompatible" });
	});

	it("emit is one-way and delivered to host event handler", async () => {
		const bridge = PluginSDK.createPluginBridge({ pluginId: "sample-annotator" });
		bridge.emit("notification.show", { msg: "hi" });
		// 事件同步投递到假 host._receiveFromPlugin（emit 内直接调用），断言已记录
		const found = host.events.find((e) => e.type === "notification.show");
		expect(found).toBeTruthy();
		expect(found && (found.payload as { msg?: string }).msg).toBe("hi");
	});
});

// ------------------------------------------------------------------------- //
// checkPermission 矩阵
// ------------------------------------------------------------------------- //
describe("PluginPermissions.checkPermission", () => {
	it("returns null when declared includes required permission", () => {
		expect(PluginPermissions.checkPermission(["annotation:write"], "annotation.create")).toBeNull();
		expect(PluginPermissions.checkPermission(["slide:metadata:read", "viewer:navigate"], "viewer.navigate")).toBeNull();
	});

	it("returns permission_denied when declared lacks required, message contains method", () => {
		const r = PluginPermissions.checkPermission(["slide:metadata:read"], "annotation.create");
		expect(r).not.toBeNull();
		expect(r!.code).toBe("permission_denied");
		expect(r!.retryable).toBe(false);
		expect(r!.message).toContain("annotation.create");
		expect(r!.message).toContain("annotation:write");
	});

	it("returns null for unmapped methods (not gated)", () => {
		expect(PluginPermissions.checkPermission([], "bridge.negotiate")).toBeNull();
		expect(PluginPermissions.checkPermission([], "some.unknown.method")).toBeNull();
	});

	it("treats undefined/null declared as empty (permission_denied); allow-if-absent logic lives in host gate", () => {
		// checkPermission 本身把非数组当空权限表 → permission_denied；"不在权限表内则放行"
		// 是 app.js 的 gate() 判定（declared 非数组时跳过 checkPermission），不属于本函数。
		expect(PluginPermissions.checkPermission(undefined as unknown as string[], "viewer.navigate")).toMatchObject({
			code: "permission_denied",
		});
	});

	it("maps every gated method to a required permission", () => {
		expect(PluginPermissions.METHOD_PERMISSIONS["slide.getCurrent"]).toBe("slide:metadata:read");
		expect(PluginPermissions.METHOD_PERMISSIONS["selection.getBbox"]).toBe("slide:metadata:read");
		expect(PluginPermissions.METHOD_PERMISSIONS["viewer.navigate"]).toBe("viewer:navigate");
		expect(PluginPermissions.METHOD_PERMISSIONS["viewer.highlight"]).toBe("viewer:navigate");
		expect(PluginPermissions.METHOD_PERMISSIONS["annotation.create"]).toBe("annotation:write");
	});
});
