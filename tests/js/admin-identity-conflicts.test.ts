/**
 * R6 退役回归（service-review-fix-plan-20260919.md §8，2026-09-19）：
 * 「身份冲突」页与「新建用户」表单整体退役。
 *
 * 原 review-2026-09-08 P2-2 交付的身份冲突清单/孤儿处置（桥方法
 * admin.users.identityConflicts / admin.users.discardPending）与手动建号
 * （admin.users.create）已全部下线。本文件锁定退役不可逆：
 *   - 宿主桥（static/admin-host.js）：三个方法无权限映射、无参数 schema、
 *     无后端映射——dispatch 门稳定 unknown_method，绝不发出对
 *     /api/admin/v1/users（POST）/ identity-conflicts / discard-pending
 *     的任何 HTTP 调用（服务端旧 REST 入口亦已 410 endpoint_retired）；
 *   - 插件 UI（plugins/pathtogether-admin/ui/）：无 identity 导航/页面/
 *     深链、无「新建用户」表单与提交处理器、main.js 无
 *     submitCreateUser/loadIdentityConflicts/discardPendingIdentity 等残留；
 *   - R7 交付保持不变：测试申请页（含 activated_by_invite 终态）不受影响。
 */
import { describe, expect, it } from "vitest";
import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const hostSrc = readFileSync(resolve(here, "../../static/admin-host.js"), "utf8");
const pluginSrc = readFileSync(
	resolve(here, "../../plugins/pathtogether-admin/ui/main.js"), "utf8");
const htmlSrc = readFileSync(
	resolve(here, "../../plugins/pathtogether-admin/ui/index.html"), "utf8");

const RETIRED_METHODS = [
	"admin.users.create",
	"admin.users.identityConflicts",
	"admin.users.discardPending",
];

// --------------------------------------------------------------------------- //
// 宿主桥侧（模式同 admin-bridge.test.ts）
// --------------------------------------------------------------------------- //
interface Posted {
	env: Record<string, unknown>;
	targetOrigin: string;
}

function loadHostModule() {
	let entropyCalls = 0;
	const crypto = {
		getRandomValues(buf: Uint8Array) {
			entropyCalls += 1;
			for (let i = 0; i < buf.length; i++) {
				buf[i] = (i * 7 + 3 + entropyCalls * 13) % 256;
			}
			return buf;
		},
	};
	const w: Record<string, unknown> = {
		crypto,
		fetch: async () => {
			throw new Error("raw window.fetch must not be used; go through fetchJson");
		},
		console,
		setTimeout,
		clearTimeout,
		document: {
			readyState: "complete",
			getElementById: () => null, // auto-boot no-op
			addEventListener() {},
		},
	};
	new Function("window", hostSrc)(w);
	return {
		AdminBridgeHost: w.AdminBridgeHost as {
			METHOD_PERMISSIONS: Record<string, string>;
			METHOD_PARAM_SCHEMAS: Record<string, unknown>;
			create: (opts: Record<string, unknown>) => HostHandle;
		},
	};
}

interface HostHandle {
	_handleIframeLoad: () => void;
	_handleWindowMessage: (event: { source: unknown; data: unknown }) => void;
	stats: () => { denied: number; handled: number };
}

interface Call {
	url: string;
	opts: { method?: string; body?: string; headers?: Record<string, string> };
}

function makeHost(opts: {
	permissions?: string[];
	respond?: (call: Call) => { status: number; ok: boolean; body: unknown };
}) {
	const { AdminBridgeHost } = loadHostModule();
	const posted: Posted[] = [];
	const contentWindow = {
		postMessage: (env: Record<string, unknown>, targetOrigin: string) =>
			posted.push({ env, targetOrigin }),
	};
	const iframe = {
		contentWindow,
		addEventListener() {},
		getAttribute: () => "/admin/plugin-assets/pathtogether-admin/ui/index.html",
		setAttribute() {},
	};
	const calls: Call[] = [];
	const fetchJson = async (url: string, o?: Call["opts"]) => {
		const call: Call = { url, opts: o || {} };
		calls.push(call);
		if (opts.respond) return opts.respond(call);
		return { status: 200, ok: true, body: {} };
	};
	const handle = AdminBridgeHost.create({
		iframe,
		permissions: opts.permissions || [
			"admin:overview:read", "admin:users:read", "admin:users:write",
		],
		crypto: { getRandomValues: (b: Uint8Array) => cryptoFill(b) },
		fetchJson,
		ensureOwner: async () => true,
		timeoutMs: 5000,
	});
	return { handle, posted, contentWindow, calls, AdminBridgeHost };
}

function cryptoFill(buf: Uint8Array) {
	for (let i = 0; i < buf.length; i++) buf[i] = (i * 11 + 5) % 256;
	return buf;
}

const tick = () => new Promise((r) => setTimeout(r, 0));
const ticks = async (n = 4) => {
	for (let i = 0; i < n; i++) await tick();
};

function initNonce(posted: Posted[]): string {
	const init = posted.find((p) => p.env.kind === "init");
	expect(init, "init envelope posted").toBeTruthy();
	return (init!.env.nonce as string) || "";
}

function requestEnv(nonce: string, requestId: string, method: string, payload: unknown = {}) {
	return {
		kind: "request", bridge: "admin", protocolVersion: "1.0.0",
		nonce, requestId, method, payload,
	};
}

function responses(posted: Posted[], requestId?: string) {
	return posted.filter(
		(p) =>
			p.env.kind === "response" &&
			(requestId === undefined || p.env.requestId === requestId),
	);
}

describe("宿主桥 — R6 退役方法不可调用（unknown_method + 后端零调用）", () => {
	it("三个退役方法无权限映射、无参数 schema（源码级）", () => {
		const { AdminBridgeHost } = loadHostModule();
		const table = AdminBridgeHost.METHOD_PERMISSIONS;
		const schemas = AdminBridgeHost.METHOD_PARAM_SCHEMAS as Record<string, unknown>;
		for (const method of RETIRED_METHODS) {
			expect(table[method], method).toBeUndefined();
			expect(schemas[method], method).toBeUndefined();
		}
		// 健在的用户管理方法不受影响（用户列表/启停/AI 权限/密码重置）
		expect(table["admin.users.list"]).toBe("admin:users:read");
		expect(table["admin.users.setEnabled"]).toBe("admin:users:write");
		expect(table["admin.users.setAiAccess"]).toBe("admin:users:write");
		expect(table["admin.users.resetPassword"]).toBe("admin:users:write");
	});

	it.each(RETIRED_METHODS.map((m) => [m]))(
		"%s 请求稳定 unknown_method，绝不代理到旧 REST 入口",
		async (method) => {
			const { handle, posted, contentWindow, calls } = makeHost({
				respond: () => ({ status: 200, ok: true, body: {} }),
			});
			handle._handleIframeLoad();
			handle._handleWindowMessage({
				source: contentWindow,
				data: requestEnv(initNonce(posted), "r1", method,
					method === "admin.users.discardPending"
						? { user_id: "usr_AbCdEfGh" }
						: { login_id: "a@x.com", password: "longpass-12345" }),
			});
			await ticks();
			const rs = responses(posted, "r1");
			expect(rs).toHaveLength(1);
			expect(rs[0].env.ok).toBe(false);
			expect((rs[0].env.error as { code: string }).code).toBe("unknown_method");
			// 后端零调用：identity-conflicts / discard-pending / POST users
			// 三条旧路径都不允许出现在宿主 fetch 里
			expect(calls).toHaveLength(0);
			expect(calls.map((c) => c.url)).not.toContain(
				"/api/admin/v1/users/identity-conflicts");
		},
	);

	it("宿主源码不再含旧 REST 入口接线（POST users / identity-conflicts / discard-pending）", () => {
		// 只查代码接线形态（字符串字面量调用），注释中的退役说明不受限
		expect(hostSrc).not.toContain('jsonWrite("/api/admin/v1/users"');
		expect(hostSrc).not.toContain('identity-conflicts")');
		expect(hostSrc).not.toContain('/discard-pending"');
	});
});

// --------------------------------------------------------------------------- //
// 插件 UI 侧（源码级退役断言：无导航/页面/表单/处理器残留）
// --------------------------------------------------------------------------- //
describe("插件 UI — R6 退役残留清零", () => {
	it("index.html 无 identity 导航/页面骨架/确认条，且无「新建用户」表单", () => {
		expect(htmlSrc).not.toContain('data-page="identity"');
		expect(htmlSrc).not.toContain('id="adm-page-identity"');
		expect(htmlSrc).not.toContain('id="adm-identity-kpis"');
		expect(htmlSrc).not.toContain('id="adm-identity-table"');
		expect(htmlSrc).not.toContain('id="adm-identity-tbody"');
		expect(htmlSrc).not.toContain('id="adm-identity-confirm"');
		expect(htmlSrc).not.toContain('id="adm-identity-refresh-btn"');
		expect(htmlSrc).not.toContain('id="adm-identity-status"');
		// 创建表单整体移除（表单/输入/提交按钮/状态行）
		expect(htmlSrc).not.toContain('id="adm-users-create-box"');
		expect(htmlSrc).not.toContain('id="adm-users-create-form"');
		expect(htmlSrc).not.toContain('id="adm-users-create-btn"');
		expect(htmlSrc).not.toContain('id="adm-users-create-status"');
		expect(htmlSrc).not.toContain('id="adm-users-new-login"');
		expect(htmlSrc).not.toContain('id="adm-users-new-display"');
		expect(htmlSrc).not.toContain('id="adm-users-new-password"');
		expect(htmlSrc).not.toContain('id="adm-users-new-limit"');
		// 导航仅剩 R6 后的 10 页（无「身份冲突」入口）
		expect(htmlSrc).toContain('data-page="users"');
		expect(htmlSrc).toContain('data-page="slides"');
		expect(htmlSrc).not.toContain("身份冲突清单");
	});

	it("main.js 无 submitCreateUser / loadIdentityConflicts / discardPending 残留", () => {
		// 函数声明 / 处理器绑定 / 桥请求一律不得存在（注释中的退役说明除外）
		expect(pluginSrc).not.toMatch(
			/function (submitCreateUser|loadIdentityConflicts|renderIdentityConflicts|renderIdentityRow|discardPendingIdentity)\b/);
		expect(pluginSrc).not.toMatch(/var IDENTITY_CONFLICT_LABELS\b/);
		// 桥方法名不再作为请求发出
		expect(pluginSrc).not.toContain('request("admin.users.create"');
		expect(pluginSrc).not.toContain('request("admin.users.identityConflicts"');
		expect(pluginSrc).not.toContain('request("admin.users.discardPending"');
		// 深链白名单与页注册不含 identity
		expect(pluginSrc).not.toMatch(/var pages = \[[^\]]*"identity"/);
		expect(pluginSrc).not.toMatch(/identity: \$\("adm-page-identity"\)/);
		expect(pluginSrc).not.toMatch(/identity: "身份冲突"/);
		expect(pluginSrc).not.toMatch(/name === "identity"/);
		// 事件监听不再绑定退役控件
		expect(pluginSrc).not.toContain('onClick("adm-users-create-btn"');
		expect(pluginSrc).not.toContain('onClick("adm-identity-refresh-btn"');
	});

	it("R7 交付保持：测试申请页与 activated_by_invite 终态不受 R6 影响", () => {
		expect(htmlSrc).toContain('data-page="test-applications"');
		expect(htmlSrc).toContain("已通过邀请码激活");
		expect(pluginSrc).toContain('"test-applications"');
	});
});
