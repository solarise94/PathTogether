/**
 * AdminBridge 宿主侧单测（PR3，docs/admin-billing-plugin-implementation-plan.md
 * §8.3/§8.4 / §14.1「AdminBridge」行）。
 *
 * 加载真实 static/admin-host.js（new Function 注入假 window，模式同
 * host-bridge-native.test.ts），锁定：
 *   - iframe load → init 携带 256-bit 一次性 nonce + 协议版本 + 已授予能力
 *     （targetOrigin "*"，安全边界 = 精确 WindowProxy + nonce + 服务端复核）；
 *   - event.source 不符 / nonce 错误 / 协议版本不符 → 静默丢弃（无响应）；
 *   - requestId 重放 → request_id_replayed，后端只执行一次；
 *   - 未知 method → unknown_method；未申请 adminPermission → permission_denied；
 *   - 已登记但未实现（PR3b 待填）→ 稳定 not_implemented；
 *   - 参数 schema（admin.auth.get 拒绝多余字段）；
 *   - reload / 登出（后端 401）→ nonce 与在途请求立即作废；
 *   - method→permission 映射表与 §8.4 一致（防漂移）。
 */
import { describe, expect, it } from "vitest";
import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const src = readFileSync(resolve(here, "../../static/admin-host.js"), "utf8");

interface Posted {
	env: Record<string, unknown>;
	targetOrigin: string;
}

function loadModule() {
	// 计数式确定性熵源：同一进程内每次 getRandomValues 输出不同（nonce 轮换可断言）
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
	new Function("window", src)(w);
	return {
		AdminBridgeHost: w.AdminBridgeHost as {
			PROTOCOL_VERSION: string;
			METHOD_PERMISSIONS: Record<string, string>;
			maskLoginId: (id: string) => string | null;
			create: (opts: Record<string, unknown>) => HostHandle;
		},
		crypto,
	};
}

interface HostHandle {
	_handleIframeLoad: () => void;
	_handleWindowMessage: (event: { source: unknown; data: unknown }) => void;
	invalidate: (reason: string) => void;
	isReady: () => boolean;
	stats: () => { denied: number; handled: number };
}

function makeHost(opts: {
	permissions?: string[];
	fetchJson?: (url: string, o?: unknown) => Promise<unknown>;
	ensureOwner?: () => Promise<boolean>;
	crypto?: unknown;
}) {
	const { AdminBridgeHost, crypto } = loadModule();
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
	const calls: string[] = [];
	const fetchJson =
		opts.fetchJson ||
		(async (url: string) => {
			calls.push(url);
			return {
				status: 200,
				ok: true,
				body: { actor: { role: "owner", username: "owner@x.com" } },
			};
		});
	const handle = AdminBridgeHost.create({
		iframe,
		permissions: opts.permissions || ["admin:overview:read", "admin:users:read"],
		crypto: opts.crypto || crypto,
		fetchJson,
		ensureOwner: opts.ensureOwner || (async () => true),
		timeoutMs: 5000,
	});
	return { handle, posted, contentWindow, calls, AdminBridgeHost };
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
		kind: "request",
		bridge: "admin",
		protocolVersion: "1.0.0",
		nonce,
		requestId,
		method,
		payload,
	};
}

function responses(posted: Posted[], requestId?: string) {
	return posted.filter(
		(p) =>
			p.env.kind === "response" &&
			(requestId === undefined || p.env.requestId === requestId),
	);
}

describe("AdminBridge host — handshake (§8.3)", () => {
	it("iframe load posts init with fresh 256-bit nonce, protocol version, granted permissions", () => {
		const { handle, posted } = makeHost({});
		expect(handle.isReady()).toBe(false);
		handle._handleIframeLoad();
		expect(handle.isReady()).toBe(true);
		expect(posted).toHaveLength(1);
		const { env, targetOrigin } = posted[0];
		expect(env.kind).toBe("init");
		expect(env.bridge).toBe("admin");
		expect(env.protocolVersion).toBe("1.0.0");
		expect(String(env.nonce)).toMatch(/^[0-9a-f]{64}$/); // 256-bit hex
		expect(env.adminPermissions).toEqual(["admin:overview:read", "admin:users:read"]);
		// opaque iframe：只能 "*"（安全边界 = WindowProxy + nonce + 服务端复核）
		expect(targetOrigin).toBe("*");
	});

	it("reload rotates nonce and rejects in-flight requests", async () => {
		const { handle, posted, contentWindow } = makeHost({
			ensureOwner: () => new Promise<boolean>(() => {}), // 永不 resolve → 请求滞留
		});
		handle._handleIframeLoad();
		const nonce1 = initNonce(posted);
		handle._handleWindowMessage({
			source: contentWindow,
			data: requestEnv(nonce1, "r1", "admin.auth.get"),
		});
		await ticks();

		// reload：旧 load 作废（在途请求收到 bridge_invalidated）+ 新 nonce
		handle._handleIframeLoad();
		const invalidation = responses(posted, "r1");
		expect(invalidation).toHaveLength(1);
		expect(invalidation[0].env.ok).toBe(false);
		expect((invalidation[0].env.error as { code: string }).code).toBe("bridge_invalidated");
		const evt = posted.find((p) => p.env.kind === "event");
		expect(evt?.env.type).toBe("bridge_invalidated");
		const nonce2 = posted.filter((p) => p.env.kind === "init").map((p) => p.env.nonce);
		expect(nonce2).toHaveLength(2);
		expect(nonce2[0]).not.toBe(nonce2[1]);
		expect(handle.isReady()).toBe(true);

		// 旧 nonce 在新 load 下立即失效
		handle._handleWindowMessage({
			source: contentWindow,
			data: requestEnv(nonce1, "r2", "admin.auth.get"),
		});
		await ticks();
		expect(responses(posted, "r2")).toHaveLength(0);
		expect(handle.stats().denied).toBeGreaterThanOrEqual(1);
	});
});

describe("AdminBridge host — message gate (§8.3/§8.4)", () => {
	it("rejects messages whose event.source is not the exact iframe WindowProxy", async () => {
		const { handle, posted } = makeHost({});
		handle._handleIframeLoad();
		const nonce = initNonce(posted);
		handle._handleWindowMessage({
			source: { fake: "window" }, // 不是 iframe.contentWindow
			data: requestEnv(nonce, "r1", "admin.auth.get"),
		});
		await ticks();
		expect(posted.filter((p) => p.env.kind === "response")).toHaveLength(0);
		expect(handle.stats().denied).toBe(1);
	});

	it("rejects wrong nonce (silent drop)", async () => {
		const { handle, posted, contentWindow } = makeHost({});
		handle._handleIframeLoad();
		handle._handleWindowMessage({
			source: contentWindow,
			data: requestEnv("0".repeat(64), "r1", "admin.auth.get"),
		});
		await ticks();
		expect(posted.filter((p) => p.env.kind === "response")).toHaveLength(0);
		expect(handle.stats().denied).toBe(1);
	});

	it("rejects protocol major mismatch (silent drop)", async () => {
		const { handle, posted, contentWindow } = makeHost({});
		handle._handleIframeLoad();
		const nonce = initNonce(posted);
		handle._handleWindowMessage({
			source: contentWindow,
			data: { ...requestEnv(nonce, "r1", "admin.auth.get"), protocolVersion: "2.0.0" },
		});
		await ticks();
		expect(posted.filter((p) => p.env.kind === "response")).toHaveLength(0);
		expect(handle.stats().denied).toBe(1);
	});

	it("rejects replayed requestId within the same load", async () => {
		const { handle, posted, contentWindow } = makeHost({});
		handle._handleIframeLoad();
		const nonce = initNonce(posted);
		handle._handleWindowMessage({
			source: contentWindow, data: requestEnv(nonce, "r1", "admin.auth.get"),
		});
		await ticks();
		handle._handleWindowMessage({
			source: contentWindow, data: requestEnv(nonce, "r1", "admin.auth.get"),
		});
		await ticks();
		const rs = responses(posted, "r1");
		expect(rs).toHaveLength(2);
		expect(rs[0].env.ok).toBe(true);
		expect(rs[1].env.ok).toBe(false);
		expect((rs[1].env.error as { code: string }).code).toBe("request_id_replayed");
		// 后端只执行一次（防重放的本质）
		expect(handle.stats().handled).toBe(1);
	});

	it("rejects unknown method with unknown_method", async () => {
		const { handle, posted, contentWindow } = makeHost({});
		handle._handleIframeLoad();
		const nonce = initNonce(posted);
		handle._handleWindowMessage({
			source: contentWindow, data: requestEnv(nonce, "r1", "admin.users.delete"),
		});
		await ticks();
		const rs = responses(posted, "r1");
		expect(rs).toHaveLength(1);
		expect(rs[0].env.ok).toBe(false);
		expect((rs[0].env.error as { code: string }).code).toBe("unknown_method");
	});

	it("rejects methods whose adminPermission is not declared in the manifest", async () => {
		const { handle, posted, contentWindow } = makeHost({
			permissions: ["admin:overview:read"], // 未申请 admin:users:read
		});
		handle._handleIframeLoad();
		const nonce = initNonce(posted);
		handle._handleWindowMessage({
			source: contentWindow, data: requestEnv(nonce, "r1", "admin.users.list"),
		});
		await ticks();
		const rs = responses(posted, "r1");
		expect(rs).toHaveLength(1);
		expect(rs[0].env.ok).toBe(false);
		expect((rs[0].env.error as { code: string }).code).toBe("permission_denied");
	});

	it("returns stable not_implemented for mapped-but-unimplemented methods (PR3b)", async () => {
		const { handle, posted, contentWindow } = makeHost({
			permissions: ["admin:overview:read", "admin:billing:read"],
		});
		handle._handleIframeLoad();
		const nonce = initNonce(posted);
		handle._handleWindowMessage({
			source: contentWindow, data: requestEnv(nonce, "r1", "admin.billing.ledger.list"),
		});
		await ticks();
		const rs = responses(posted, "r1");
		expect(rs).toHaveLength(1);
		expect(rs[0].env.ok).toBe(false);
		expect((rs[0].env.error as { code: string }).code).toBe("not_implemented");
	});

	it("enforces per-method param schema (admin.auth.get rejects extra fields)", async () => {
		const { handle, posted, contentWindow } = makeHost({});
		handle._handleIframeLoad();
		const nonce = initNonce(posted);
		handle._handleWindowMessage({
			source: contentWindow,
			data: requestEnv(nonce, "r1", "admin.auth.get", { csrfToken: "leak" }),
		});
		await ticks();
		const rs = responses(posted, "r1");
		expect(rs).toHaveLength(1);
		expect((rs[0].env.error as { code: string }).code).toBe("invalid_params");
	});

	it("rejects when current actor is no longer owner (per-message recheck)", async () => {
		const { handle, posted, contentWindow } = makeHost({
			ensureOwner: async () => false,
		});
		handle._handleIframeLoad();
		const nonce = initNonce(posted);
		handle._handleWindowMessage({
			source: contentWindow, data: requestEnv(nonce, "r1", "admin.auth.get"),
		});
		await ticks();
		const rs = responses(posted, "r1");
		expect(rs).toHaveLength(1);
		expect((rs[0].env.error as { code: string }).code).toBe("forbidden");
	});
});

describe("AdminBridge host — backend proxy (admin.auth.get only in PR3)", () => {
	it("admin.auth.get returns masked identity without exposing raw fields", async () => {
		const { handle, posted, contentWindow, calls } = makeHost({
			fetchJson: async (url: string) => {
				expect(url).toBe("/api/auth/info");
				return {
					status: 200, ok: true,
					body: {
						auth_enabled: true, username: "owner@x.com", role: "owner",
						user_id: "u1", password_hash: "SECRET",
						actor: { username: "owner@x.com", role: "owner", user_id: "u1" },
						preview: null,
					},
				};
			},
		});
		handle._handleIframeLoad();
		const nonce = initNonce(posted);
		handle._handleWindowMessage({
			source: contentWindow, data: requestEnv(nonce, "r1", "admin.auth.get"),
		});
		await ticks();
		const rs = responses(posted, "r1");
		expect(rs).toHaveLength(1);
		expect(rs[0].env.ok).toBe(true);
		expect(rs[0].env.result).toEqual({
			role: "owner", loginIdMasked: "o***@x.com", previewActive: false,
		});
		// CSRF token / session 内容绝不进 iframe 信封
		expect(JSON.stringify(posted)).not.toContain("SECRET");
		expect(calls).toEqual([]);
	});

	it("backend 401 (logout) immediately invalidates nonce and pending requests", async () => {
		const { handle, posted, contentWindow } = makeHost({
			fetchJson: async () => ({ status: 401, ok: false, body: { error: "auth_required" } }),
		});
		handle._handleIframeLoad();
		const nonce = initNonce(posted);
		handle._handleWindowMessage({
			source: contentWindow, data: requestEnv(nonce, "r1", "admin.auth.get"),
		});
		await ticks();
		// 401 → 立即作废：在途请求被 reject（回 bridge_invalidated 而非业务结果），
		// 另发 bridge_invalidated 事件，nonce 会话终结
		const rs = responses(posted, "r1");
		expect(rs).toHaveLength(1);
		expect(rs[0].env.ok).toBe(false);
		expect((rs[0].env.error as { code: string }).code).toBe("bridge_invalidated");
		expect(JSON.stringify(rs[0].env)).not.toContain("backend_error");
		const evt = posted.find((p) => p.env.kind === "event");
		expect(evt?.env.type).toBe("bridge_invalidated");
		expect(evt?.env.reason).toBe("logout");
		expect(handle.isReady()).toBe(false);
	});

	it("maskLoginId never returns the raw login id", () => {
		const { AdminBridgeHost } = loadModule();
		expect(AdminBridgeHost.maskLoginId("owner@x.com")).toBe("o***@x.com");
		expect(AdminBridgeHost.maskLoginId("admin")).toBe("a***n");
		expect(AdminBridgeHost.maskLoginId("ab")).toBe("a*");
		expect(AdminBridgeHost.maskLoginId("")).toBeNull();
	});
});

describe("AdminBridge host — §8.4 method→permission mapping (drift guard)", () => {
	it("matches the documented table", () => {
		const { AdminBridgeHost } = loadModule();
		const table = AdminBridgeHost.METHOD_PERMISSIONS;
		expect(Object.keys(table)).toHaveLength(22);
		expect(table["admin.auth.get"]).toBe("admin:overview:read");
		expect(table["admin.overview.get"]).toBe("admin:overview:read");
		expect(table["admin.users.list"]).toBe("admin:users:read");
		expect(table["admin.users.create"]).toBe("admin:users:write");
		expect(table["admin.users.setEnabled"]).toBe("admin:users:write");
		expect(table["admin.users.setAiAccess"]).toBe("admin:users:write");
		expect(table["admin.users.resetPassword"]).toBe("admin:users:write");
		expect(table["admin.invites.list"]).toBe("admin:invites:read");
		expect(table["admin.invites.create"]).toBe("admin:invites:write");
		expect(table["admin.invites.revoke"]).toBe("admin:invites:write");
		expect(table["admin.turnBudgets.get"]).toBe("admin:turn-budgets:read");
		expect(table["admin.turnBudgets.update"]).toBe("admin:turn-budgets:write");
		expect(table["admin.turnBudgets.newPeriod"]).toBe("admin:turn-budgets:write");
		expect(table["admin.billing.account.get"]).toBe("admin:billing:read");
		expect(table["admin.billing.account.updateCaps"]).toBe("admin:billing:write");
		expect(table["admin.billing.adjust"]).toBe("admin:billing:write");
		expect(table["admin.billing.usage.list"]).toBe("admin:billing:read");
		expect(table["admin.billing.ledger.list"]).toBe("admin:billing:read");
		expect(table["admin.billing.providerBalance.get"]).toBe("admin:billing:read");
		expect(table["admin.acquisition.summary"]).toBe("admin:acquisition:read");
		expect(table["admin.acquisition.list"]).toBe("admin:acquisition:read");
		expect(table["admin.audit.list"]).toBe("admin:audit:read");
	});
});
