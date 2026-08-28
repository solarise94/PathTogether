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
 *   - 宿主全部 result/error 回包带当前 load 的 nonce（§8.3 P2 对称认证）；
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

	it("wires a backend and param schema for every mapped method (PR5 drift guard)", () => {
		const { AdminBridgeHost } = loadModule();
		const table = AdminBridgeHost.METHOD_PERMISSIONS;
		const schemas = (
			AdminBridgeHost as unknown as {
				METHOD_PARAM_SCHEMAS: Record<string, unknown>;
			}
		).METHOD_PARAM_SCHEMAS;
		for (const method of Object.keys(table)) {
			// 防御性兜底（not_implemented）只对「漏接线」生效；PR5 后全表必须
			// 同时具备 backend 实现与参数 schema——这里通过 schema 存在性 +
			// dispatch 全链路用例锁定（backend 存在性由下方逐方法代理用例覆盖）
			expect(schemas[method], method + " 缺参数 schema").toBeTruthy();
			const schema = schemas[method] as { additionalProperties?: boolean };
			expect(schema.additionalProperties, method).toBe(false);
		}
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
		// 22 个 §8.4 表方法 + PR3b 扩展的 providerBalance.refresh（与 get 同级
		// admin:billing:read：只抓取供应商自身余额，不触碰用户数据）+ PR5
		// 修订补的 4 个 UI parity 方法（users.startPreview / plugins.*）
		expect(Object.keys(table)).toHaveLength(27);
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
		expect(table["admin.billing.providerBalance.refresh"]).toBe("admin:billing:read");
		expect(table["admin.acquisition.summary"]).toBe("admin:acquisition:read");
		expect(table["admin.acquisition.list"]).toBe("admin:acquisition:read");
		expect(table["admin.audit.list"]).toBe("admin:audit:read");
		// PR5 修订（UI parity）：身份预览（写 owner session，归 users:write）+
		// 插件管理（独立 plugins:read/write，不复用 users/billing）
		expect(table["admin.users.startPreview"]).toBe("admin:users:write");
		expect(table["admin.plugins.list"]).toBe("admin:plugins:read");
		expect(table["admin.plugins.setEnabled"]).toBe("admin:plugins:write");
		expect(table["admin.plugins.rotateSecret"]).toBe("admin:plugins:write");
	});

	it("declares param schemas for every PR3b read method (whitelist + types)", () => {
		const { AdminBridgeHost } = loadModule();
		const schemas = (
			AdminBridgeHost as unknown as {
				METHOD_PARAM_SCHEMAS: Record<string, { properties: Record<string, unknown> }>;
			}
		).METHOD_PARAM_SCHEMAS;
		for (const method of [
			"admin.auth.get",
			"admin.overview.get",
			"admin.users.list",
			"admin.billing.account.get",
			"admin.billing.usage.list",
			"admin.billing.ledger.list",
			"admin.billing.providerBalance.get",
			"admin.billing.providerBalance.refresh",
			"admin.audit.list",
			"admin.turnBudgets.get",
		]) {
			expect(schemas[method], method).toBeTruthy();
			// 附加属性一律拒绝（iframe 不能借桥传任意字段）
			expect((schemas[method] as { additionalProperties?: boolean }).additionalProperties).toBe(false);
		}
	});
});

describe("AdminBridge host — PR3b read backend proxy (§9 Admin API v1)", () => {
	const ALL_READ = [
		"admin:overview:read", "admin:users:read", "admin:turn-budgets:read",
		"admin:billing:read", "admin:audit:read",
	];

	function makeReadHost(fetchJson: (url: string, o?: unknown) => Promise<unknown>) {
		return makeHost({ permissions: ALL_READ, fetchJson });
	}

	it("admin.overview.get proxies GET /api/admin/v1/overview verbatim", async () => {
		const overview = {
			users: { total: 2, active: 2, disabled: 0, ai_access: 1 },
			billing: { available: true, charge_nano_cny: 42 },
			turn_budget: { available: true, platform: { total: 3, limit: 30 } },
		};
		const { handle, posted, contentWindow } = makeReadHost(async (url) => {
			expect(url).toBe("/api/admin/v1/overview");
			return { status: 200, ok: true, body: overview };
		});
		handle._handleIframeLoad();
		handle._handleWindowMessage({
			source: contentWindow, data: requestEnv(nonce0(posted), "r1", "admin.overview.get"),
		});
		await ticks();
		const rs = responses(posted, "r1");
		expect(rs[0].env.ok).toBe(true);
		expect(rs[0].env.result).toEqual(overview);
	});

	it("admin.users.list maps cursor/limit/filters into the query string", async () => {
		const { handle, posted, contentWindow } = makeReadHost(async (url) => {
			expect(url).toBe(
				"/api/admin/v1/users?cursor=abc&limit=25&q=alice&enabled=true&ai_access=false",
			);
			return { status: 200, ok: true, body: { items: [], next_cursor: null } };
		});
		handle._handleIframeLoad();
		handle._handleWindowMessage({
			source: contentWindow,
			data: requestEnv(nonce0(posted), "r1", "admin.users.list", {
				cursor: "abc", limit: 25, q: "alice", enabled: true, ai_access: false,
			}),
		});
		await ticks();
		expect(responses(posted, "r1")[0].env.ok).toBe(true);
	});

	it("admin.billing.usage.list only whitelists cursor/limit/model/user_id/status", async () => {
		const { handle, posted, contentWindow } = makeReadHost(async (url) => {
			expect(url).toBe("/api/admin/v1/billing/usage-events?limit=10&status=unpriced");
			return { status: 200, ok: true, body: { items: [], next_cursor: null } };
		});
		handle._handleIframeLoad();
		handle._handleWindowMessage({
			source: contentWindow,
			data: requestEnv(nonce0(posted), "r1", "admin.billing.usage.list", {
				limit: 10, status: "unpriced", model: "", user_id: null,
			}),
		});
		await ticks();
		expect(responses(posted, "r1")[0].env.ok).toBe(true);
	});

	it("admin.billing.account.get URL-encodes user_id", async () => {
		const { handle, posted, contentWindow } = makeReadHost(async (url) => {
			expect(url).toBe("/api/admin/v1/billing/accounts/usr_abc%2Fdef");
			return { status: 200, ok: true, body: { account: null, balance_nano: null } };
		});
		handle._handleIframeLoad();
		handle._handleWindowMessage({
			source: contentWindow,
			data: requestEnv(nonce0(posted), "r1", "admin.billing.account.get", {
				user_id: "usr_abc/def",
			}),
		});
		await ticks();
		expect(responses(posted, "r1")[0].env.ok).toBe(true);
	});

	it("admin.billing.providerBalance.refresh issues POST and maps server error codes", async () => {
		const calls: Array<{ url: string; method?: string }> = [];
		const { handle, posted, contentWindow } = makeReadHost(async (url, o) => {
			calls.push({ url, method: (o as { method?: string } | undefined)?.method });
			if (calls.length === 1) {
				return { status: 429, ok: false, body: { error: { code: "refresh_throttled", message: "slow down" } } };
			}
			return {
				status: 200, ok: true,
				body: { ok: true, snapshot: { total_balance_nano: 1 }, age_seconds: 0 },
			};
		});
		handle._handleIframeLoad();
		handle._handleWindowMessage({
			source: contentWindow,
			data: requestEnv(nonce0(posted), "r1", "admin.billing.providerBalance.refresh"),
		});
		await ticks();
		let rs = responses(posted, "r1");
		expect(rs[0].env.ok).toBe(false);
		expect((rs[0].env.error as { code: string }).code).toBe("refresh_throttled");

		handle._handleWindowMessage({
			source: contentWindow,
			data: requestEnv(nonce0(posted), "r2", "admin.billing.providerBalance.refresh"),
		});
		await ticks();
		rs = responses(posted, "r2");
		expect(rs[0].env.ok).toBe(true);
		expect(calls).toHaveLength(2);
		expect(calls[0]).toEqual({ url: "/api/admin/v1/billing/provider-balance/refresh", method: "POST" });
	});

	it("maps 503 pg_backend_required envelopes to the bridge error code", async () => {
		const { handle, posted, contentWindow } = makeReadHost(async () => ({
			status: 503, ok: false,
			body: { error: { code: "pg_backend_required", message: "fail-closed" } },
		}));
		handle._handleIframeLoad();
		handle._handleWindowMessage({
			source: contentWindow, data: requestEnv(nonce0(posted), "r1", "admin.turnBudgets.get"),
		});
		await ticks();
		const rs = responses(posted, "r1");
		expect(rs[0].env.ok).toBe(false);
		expect((rs[0].env.error as { code: string }).code).toBe("pg_backend_required");
	});

	it("rejects schema-invalid params on the new methods (type / enum / extra field)", async () => {
		const { handle, posted, contentWindow } = makeReadHost(async () => ({
			status: 200, ok: true, body: {},
		}));
		handle._handleIframeLoad();
		const nonce = nonce0(posted);
		const bad: Array<[string, unknown]> = [
			["admin.users.list", { limit: "25" }],                    // 类型：integer
			["admin.users.list", { limit: 0 }],                       // 范围：min 1
			["admin.users.list", { q: 123 }],                         // 类型：string
			["admin.billing.usage.list", { status: "free" }],         // 枚举
			["admin.billing.usage.list", { csrfToken: "leak" }],      // 未声明字段
			["admin.billing.account.get", { user_id: 123 }],         // 类型：string
			["admin.audit.list", { action: true }],                   // 类型
			["admin.overview.get", { anything: 1 }],                   // 空白名单
		];
		for (let i = 0; i < bad.length; i++) {
			handle._handleWindowMessage({
				source: contentWindow,
				data: requestEnv(nonce, "bad-" + i, bad[i][0], bad[i][1]),
			});
		}
		await ticks();
		for (let i = 0; i < bad.length; i++) {
			const rs = responses(posted, "bad-" + i);
			expect(rs, bad[i][0] + " #" + i).toHaveLength(1);
			expect(rs[0].env.ok).toBe(false);
			expect((rs[0].env.error as { code: string }).code).toBe("invalid_params");
		}
		// 后端一次都没被打到（参数门在 backend 之前）
		expect(handle.stats().handled).toBe(0);
	});

	it("caches the owner recheck for 5s (one /api/auth/info per burst)", async () => {
		const { AdminBridgeHost, crypto } = loadModule();
		const posted: Posted[] = [];
		const contentWindow = {
			postMessage: (env: Record<string, unknown>, targetOrigin: string) =>
				posted.push({ env, targetOrigin }),
		};
		const authCalls: string[] = [];
		const fetchJson = async (url: string) => {
			if (url === "/api/auth/info") {
				authCalls.push(url);
				return {
					status: 200, ok: true,
					body: { actor: { role: "owner", username: "owner@x.com" } },
				};
			}
			return { status: 200, ok: true, body: {} };
		};
		const handle = AdminBridgeHost.create({
			iframe: { contentWindow, addEventListener() {}, getAttribute: () => "x", setAttribute() {} },
			permissions: ALL_READ,
			crypto,
			fetchJson, // 不传 ensureOwner：走默认 makeOwnerGuard（带 TTL 缓存）
			timeoutMs: 5000,
		});
		handle._handleIframeLoad();
		const nonce = initNonce(posted);
		for (let i = 1; i <= 3; i++) {
			handle._handleWindowMessage({
				source: contentWindow, data: requestEnv(nonce, "c" + i, "admin.overview.get"),
			});
		}
		await ticks(6);
		// 三条消息共用一次 owner 回查（5s TTL 内）
		expect(authCalls).toHaveLength(1);
		expect(responses(posted, "c1")[0].env.ok).toBe(true);
		expect(responses(posted, "c3")[0].env.ok).toBe(true);
	});
});

describe("AdminBridge host — PR4 acquisition read backend proxy (§9 Admin API v1)", () => {
	it("admin.acquisition.summary proxies GET /api/admin/v1/acquisition/summary", async () => {
		const summary = {
			registration_mode: "invite_only",
			items: [{ source_code: "mywebpage", campaign_id: null, visits: 3,
				registrations: 1, first_ai_count: 1 }],
			totals: { visits: 3, registrations: 1, first_ai_count: 1 },
		};
		const { handle, posted, contentWindow } = makeHost({
			permissions: ["admin:acquisition:read"],
			fetchJson: async (url) => {
				expect(url).toBe("/api/admin/v1/acquisition/summary");
				return { status: 200, ok: true, body: summary };
			},
		});
		handle._handleIframeLoad();
		handle._handleWindowMessage({
			source: contentWindow,
			data: requestEnv(nonce0(posted), "r1", "admin.acquisition.summary"),
		});
		await ticks();
		const rs = responses(posted, "r1");
		expect(rs[0].env.ok).toBe(true);
		expect(rs[0].env.result).toEqual(summary);
	});

	it("admin.acquisition.list maps cursor/limit into the users query string", async () => {
		const { handle, posted, contentWindow } = makeHost({
			permissions: ["admin:acquisition:read"],
			fetchJson: async (url) => {
				expect(url).toBe("/api/admin/v1/acquisition/users?cursor=k1&limit=25");
				return { status: 200, ok: true, body: { items: [], next_cursor: null } };
			},
		});
		handle._handleIframeLoad();
		handle._handleWindowMessage({
			source: contentWindow,
			data: requestEnv(nonce0(posted), "r1", "admin.acquisition.list", {
				cursor: "k1", limit: 25,
			}),
		});
		await ticks();
		expect(responses(posted, "r1")[0].env.ok).toBe(true);
	});

	it("rejects undeclared params on acquisition methods (schema gate)", async () => {
		const { handle, posted, contentWindow } = makeHost({
			permissions: ["admin:acquisition:read"],
			fetchJson: async () => ({ status: 200, ok: true, body: {} }),
		});
		handle._handleIframeLoad();
		const nonce = nonce0(posted);
		handle._handleWindowMessage({
			source: contentWindow,
			data: requestEnv(nonce, "b1", "admin.acquisition.list", { q: "evil" }),
		});
		handle._handleWindowMessage({
			source: contentWindow,
			data: requestEnv(nonce, "b2", "admin.acquisition.summary", { user_id: "x" }),
		});
		await ticks();
		expect((responses(posted, "b1")[0].env.error as { code: string }).code).toBe("invalid_params");
		expect((responses(posted, "b2")[0].env.error as { code: string }).code).toBe("invalid_params");
		expect(handle.stats().handled).toBe(0);
	});
});

describe("AdminBridge host — PR5 write methods (§9 Admin API v1 writes)", () => {
	const ALL_WRITE = [
		"admin:users:write", "admin:invites:read", "admin:invites:write",
		"admin:turn-budgets:write", "admin:billing:write",
	];

	function makeWriteHost(
		fetchJson: (url: string, o?: unknown) => Promise<unknown>,
		permissions = ALL_WRITE,
	) {
		return makeHost({ permissions, fetchJson });
	}

	it("admin.users.create proxies POST /api/admin/v1/users with whitelisted body", async () => {
		const calls: Array<{ url: string; method?: string; body?: unknown }> = [];
		const { handle, posted, contentWindow } = makeWriteHost(async (url, o) => {
			calls.push({
				url, method: (o as { method?: string } | undefined)?.method,
				body: JSON.parse(String((o as { body?: string }).body)),
			});
			return { status: 200, ok: true, body: { user: { user_id: "u1" } } };
		});
		handle._handleIframeLoad();
		handle._handleWindowMessage({
			source: contentWindow,
			data: requestEnv(nonce0(posted), "r1", "admin.users.create", {
				login_id: "a@x.com", password: "longpass-12345", display_name: "A",
			}),
		});
		await ticks();
		expect(responses(posted, "r1")[0].env.ok).toBe(true);
		expect(calls).toEqual([{
			url: "/api/admin/v1/users", method: "POST",
			body: { login_id: "a@x.com", password: "longpass-12345", display_name: "A" },
		}]);
	});

	it("admin.users.setEnabled maps enabled flag onto enable/disable path", async () => {
		const calls: Array<{ url: string; method?: string }> = [];
		const { handle, posted, contentWindow } = makeWriteHost(async (url, o) => {
			calls.push({ url, method: (o as { method?: string } | undefined)?.method });
			return { status: 200, ok: true, body: { user: {} } };
		});
		handle._handleIframeLoad();
		const nonce = nonce0(posted);
		handle._handleWindowMessage({
			source: contentWindow,
			data: requestEnv(nonce, "r1", "admin.users.setEnabled", { user_id: "usr_1", enabled: false }),
		});
		handle._handleWindowMessage({
			source: contentWindow,
			data: requestEnv(nonce, "r2", "admin.users.setEnabled", { user_id: "usr_1", enabled: true }),
		});
		await ticks();
		expect(calls[0]).toEqual({ url: "/api/admin/v1/users/usr_1/disable", method: "POST" });
		expect(calls[1]).toEqual({ url: "/api/admin/v1/users/usr_1/enable", method: "POST" });
	});

	it("rejects path params containing / before any backend call (invalid_params)", async () => {
		const calls: string[] = [];
		const { handle, posted, contentWindow } = makeWriteHost(async (url) => {
			calls.push(url);
			return { status: 200, ok: true, body: {} };
		});
		handle._handleIframeLoad();
		handle._handleWindowMessage({
			source: contentWindow,
			data: requestEnv(nonce0(posted), "r1", "admin.users.setEnabled", {
				user_id: "usr/../../api/admin/users", enabled: true,
			}),
		});
		await ticks();
		const rs = responses(posted, "r1");
		expect(rs[0].env.ok).toBe(false);
		expect((rs[0].env.error as { code: string }).code).toBe("invalid_params");
		expect(calls).toEqual([]); // 后端一次都没被打到
	});

	it("admin.users.setAiAccess / resetPassword hit their v1 endpoints", async () => {
		const calls: Array<{ url: string; method?: string; body?: unknown }> = [];
		const { handle, posted, contentWindow } = makeWriteHost(async (url, o) => {
			calls.push({
				url, method: (o as { method?: string } | undefined)?.method,
				body: JSON.parse(String((o as { body?: string }).body)),
			});
			return { status: 200, ok: true, body: { user: {} } };
		});
		handle._handleIframeLoad();
		const nonce = nonce0(posted);
		handle._handleWindowMessage({
			source: contentWindow,
			data: requestEnv(nonce, "r1", "admin.users.setAiAccess", { user_id: "usr_2", enabled: true }),
		});
		handle._handleWindowMessage({
			source: contentWindow,
			data: requestEnv(nonce, "r2", "admin.users.resetPassword", { user_id: "usr_2", password: "longpass-12345" }),
		});
		await ticks();
		expect(calls[0]).toEqual({
			url: "/api/admin/v1/users/usr_2/ai-access", method: "POST", body: { enabled: true },
		});
		expect(calls[1]).toEqual({
			url: "/api/admin/v1/users/usr_2/password-reset", method: "POST",
			body: { password: "longpass-12345" },
		});
	});

	it("admin.invites.list/create/revoke map to the v1 invite endpoints", async () => {
		const calls: Array<{ url: string; method?: string; body?: unknown }> = [];
		const { handle, posted, contentWindow } = makeWriteHost(async (url, o) => {
			calls.push({
				url, method: (o as { method?: string } | undefined)?.method,
				body: (o as { body?: string } | undefined)?.body
					? JSON.parse(String((o as { body?: string }).body)) : undefined,
			});
			if (url.startsWith("/api/admin/v1/invites") && !calls[0].method) {
				return { status: 200, ok: true, body: { invites: [], next_cursor: null } };
			}
			return { status: 200, ok: true, body: { invite: {} } };
		});
		handle._handleIframeLoad();
		const nonce = nonce0(posted);
		handle._handleWindowMessage({
			source: contentWindow,
			data: requestEnv(nonce, "r1", "admin.invites.list", { limit: 25, cursor: "k9" }),
		});
		handle._handleWindowMessage({
			source: contentWindow,
			data: requestEnv(nonce, "r2", "admin.invites.create", {
				ttl_hours: 48, ai_access: true, source_code: "mywebpage",
			}),
		});
		handle._handleWindowMessage({
			source: contentWindow,
			data: requestEnv(nonce, "r3", "admin.invites.revoke", { invite_id: "inv_9" }),
		});
		await ticks();
		expect(calls[0]).toEqual({ url: "/api/admin/v1/invites?cursor=k9&limit=25" });
		expect(calls[1]).toEqual({
			url: "/api/admin/v1/invites", method: "POST",
			body: {
				login_id: undefined, ttl_hours: 48, ai_access: true, cohort: undefined,
				note: undefined, source_code: "mywebpage", campaign_id: undefined,
			},
		});
		expect(calls[2]).toEqual({
			url: "/api/admin/v1/invites/inv_9/revoke", method: "POST", body: {},
		});
	});

	it("admin.turnBudgets.update PUTs whitelisted fields; newPeriod forces confirm=true", async () => {
		const calls: Array<{ url: string; method?: string; body?: unknown }> = [];
		const { handle, posted, contentWindow } = makeWriteHost(async (url, o) => {
			calls.push({
				url, method: (o as { method?: string } | undefined)?.method,
				body: JSON.parse(String((o as { body?: string }).body)),
			});
			return { status: 200, ok: true, body: { period_id: 2 } };
		});
		handle._handleIframeLoad();
		const nonce = nonce0(posted);
		handle._handleWindowMessage({
			source: contentWindow,
			data: requestEnv(nonce, "r1", "admin.turnBudgets.update", {
				platform_turn_limit: 500, demo_enabled: false,
			}),
		});
		handle._handleWindowMessage({
			source: contentWindow,
			data: requestEnv(nonce, "r2", "admin.turnBudgets.newPeriod", {}),
		});
		await ticks();
		expect(calls[0]).toEqual({
			url: "/api/admin/v1/turn-budgets", method: "PUT",
			body: { platform_turn_limit: 500, demo_enabled: false },
		});
		// confirm 由桥层固定补 true（二次确认在插件 UI 层做，§3.3）
		expect(calls[1]).toEqual({
			url: "/api/admin/v1/turn-budgets/new-period", method: "POST",
			body: { confirm: true, limits: undefined },
		});
	});

	it("admin.billing.account.updateCaps PUTs caps with version; adjust POSTs adjustment", async () => {
		const calls: Array<{ url: string; method?: string; body?: unknown }> = [];
		const { handle, posted, contentWindow } = makeWriteHost(async (url, o) => {
			calls.push({
				url, method: (o as { method?: string } | undefined)?.method,
				body: JSON.parse(String((o as { body?: string }).body)),
			});
			return { status: 200, ok: true, body: { ok: true } };
		});
		handle._handleIframeLoad();
		const nonce = nonce0(posted);
		// §5 v0.3（P2）：金额 wire 为十进制字符串（桥层 pattern 限定 1..19 位）
		handle._handleWindowMessage({
			source: contentWindow,
			data: requestEnv(nonce, "r1", "admin.billing.account.updateCaps", {
				user_id: "usr_3", soft_cap_nano_cny: null, hard_cap_nano_cny: "200", version: 4,
			}),
		});
		handle._handleWindowMessage({
			source: contentWindow,
			data: requestEnv(nonce, "r2", "admin.billing.adjust", {
				user_id: "usr_3", kind: "grant", amount_nano_cny: "1000",
				reason: "welcome", idempotency_key: "adj_x1",
			}),
		});
		await ticks();
		expect(calls[0]).toEqual({
			url: "/api/admin/v1/billing/accounts/usr_3/caps", method: "PUT",
			body: { soft_cap_nano_cny: null, hard_cap_nano_cny: "200", version: 4 },
		});
		expect(calls[1]).toEqual({
			url: "/api/admin/v1/billing/adjustments", method: "POST",
			body: {
				user_id: "usr_3", kind: "grant", amount_nano_cny: "1000",
				reason: "welcome", idempotency_key: "adj_x1",
			},
		});
	});

	it("rejects schema-invalid write params (required / types / enums / nonZero)", async () => {
		const { handle, posted, contentWindow } = makeWriteHost(async () => ({
			status: 200, ok: true, body: {},
		}));
		handle._handleIframeLoad();
		const nonce = nonce0(posted);
		const bad: Array<[string, unknown]> = [
			["admin.users.create", { login_id: "a@x.com" }],                     // 缺 password
			["admin.users.create", { login_id: "", password: "longpass-12345" }], // minLength
			["admin.users.setEnabled", { user_id: "u1" }],                        // 缺 enabled
			["admin.users.setEnabled", { user_id: "u1", enabled: "yes" }],       // boolean
			["admin.users.resetPassword", { user_id: "u1", password: "" }],      // minLength
			["admin.invites.create", { ttl_hours: 9999 }],                        // max 720
			["admin.invites.create", { evil: 1 }],                                // 未声明字段
			["admin.invites.revoke", {}],                                         // 缺 invite_id
			["admin.turnBudgets.update", { platform_turn_limit: 1.5 }],          // integer
			["admin.turnBudgets.update", { platform_turn_limit: 0 }],            // min 1
			["admin.turnBudgets.newPeriod", { limits: [] }],                      // object
			["admin.billing.account.updateCaps", { user_id: "u1", version: 1 }], // 缺 caps 键
			// §5 v0.3（P2）：金额只接受十进制字符串——JSON number / 负 cap /
			// 小数 / 超 19 位 / 零值形态（nonZero）一律桥层拒绝
			["admin.billing.account.updateCaps", {
				user_id: "u1", soft_cap_nano_cny: 10, hard_cap_nano_cny: "20", version: 1,
			}],                                                                    // number 型金额
			["admin.billing.account.updateCaps", {
				user_id: "u1", soft_cap_nano_cny: "-5", hard_cap_nano_cny: "20", version: 1,
			}],                                                                    // pattern 非负
			["admin.billing.account.updateCaps", {
				user_id: "u1", soft_cap_nano_cny: "1.5", hard_cap_nano_cny: "20", version: 1,
			}],                                                                    // pattern 整数
			["admin.billing.account.updateCaps", {
				user_id: "u1", soft_cap_nano_cny: "12345678901234567890",
				hard_cap_nano_cny: "20", version: 1,
			}],                                                                    // 超 19 位
			["admin.billing.account.updateCaps", {
				user_id: "u1", soft_cap_nano_cny: "1", hard_cap_nano_cny: null, version: 0,
			}],                                                                    // version min 1
			["admin.billing.adjust", { user_id: "u1", kind: "usage_debit",
				amount_nano_cny: "-5", reason: "r" }],                              // 枚举
			["admin.billing.adjust", { user_id: "u1", kind: "grant",
				amount_nano_cny: 0, reason: "r" }],                                // number 型金额
			["admin.billing.adjust", { user_id: "u1", kind: "grant",
				amount_nano_cny: "0", reason: "r" }],                               // nonZero（零值字符串）
			["admin.billing.adjust", { user_id: "u1", kind: "grant",
				amount_nano_cny: "-0", reason: "r" }],                              // nonZero（负零形态）
			["admin.billing.adjust", { user_id: "u1", kind: "grant",
				amount_nano_cny: "1.5", reason: "r" }],                             // pattern 整数
			["admin.billing.adjust", { user_id: "u1", kind: "grant",
				amount_nano_cny: "9" + "0".repeat(19), reason: "r" }],              // 超 19 位
			["admin.billing.adjust", { user_id: "u1", kind: "grant",
				amount_nano_cny: "5", reason: "" }],                                // reason minLength
			// §6.5 PR5 修订：幂等键必须由调用方生成——缺失/null/空串在桥层即拒
			//（纯空白的 trim 语义归服务端，见 test_admin_billing_writes.py）
			["admin.billing.adjust", { user_id: "u1", kind: "grant",
				amount_nano_cny: 5, reason: "r" }],                               // 缺 idempotency_key
			["admin.billing.adjust", { user_id: "u1", kind: "grant",
				amount_nano_cny: 5, reason: "r", idempotency_key: null }],       // 不再 nullable
			["admin.billing.adjust", { user_id: "u1", kind: "grant",
				amount_nano_cny: 5, reason: "r", idempotency_key: "" }],        // minLength 1
			["admin.invites.list", { q: "evil" }],                                // 未声明字段
		];
		for (let i = 0; i < bad.length; i++) {
			handle._handleWindowMessage({
				source: contentWindow,
				data: requestEnv(nonce, "w-" + i, bad[i][0], bad[i][1]),
			});
		}
		await ticks();
		for (let i = 0; i < bad.length; i++) {
			const rs = responses(posted, "w-" + i);
			expect(rs, bad[i][0] + " #" + i).toHaveLength(1);
			expect(rs[0].env.ok).toBe(false);
			expect((rs[0].env.error as { code: string }).code).toBe("invalid_params");
		}
		// 参数门在 backend 之前：一次都没代理
		expect(handle.stats().handled).toBe(0);
	});

	it("write methods require their admin:*:write permission", async () => {
		const { handle, posted, contentWindow } = makeHost({
			permissions: ["admin:users:read", "admin:billing:read"], // 无 write 权限
			fetchJson: async () => ({ status: 200, ok: true, body: {} }),
		});
		handle._handleIframeLoad();
		const nonce = nonce0(posted);
		handle._handleWindowMessage({
			source: contentWindow,
			data: requestEnv(nonce, "r1", "admin.users.create",
				{ login_id: "a@x.com", password: "longpass-12345" }),
		});
		handle._handleWindowMessage({
			source: contentWindow,
			data: requestEnv(nonce, "r2", "admin.billing.adjust",
				{ user_id: "u1", kind: "grant", amount_nano_cny: "1", reason: "r",
					idempotency_key: "adj_retry_same_key" }),
		});
		await ticks();
		expect((responses(posted, "r1")[0].env.error as { code: string }).code)
			.toBe("permission_denied");
		expect((responses(posted, "r2")[0].env.error as { code: string }).code)
			.toBe("permission_denied");
		expect(handle.stats().handled).toBe(0);
	});

	it("maps 409 version_conflict envelopes from caps updates", async () => {
		const { handle, posted, contentWindow } = makeWriteHost(async () => ({
			status: 409, ok: false,
			body: { error: { code: "version_conflict", message: "stale" } },
		}));
		handle._handleIframeLoad();
		handle._handleWindowMessage({
			source: contentWindow,
			data: requestEnv(nonce0(posted), "r1", "admin.billing.account.updateCaps", {
				user_id: "u1", soft_cap_nano_cny: "1", hard_cap_nano_cny: "2", version: 1,
			}),
		});
		await ticks();
		const rs = responses(posted, "r1");
		expect(rs[0].env.ok).toBe(false);
		expect((rs[0].env.error as { code: string }).code).toBe("version_conflict");
	});

	it("host responses carry the current-load nonce (P2 symmetric auth)", async () => {
		// §8.3 P2 修订：宿主全部 result/error 回包都带当前 load 的 nonce——
		// 插件侧据此（+ event.source）拒绝其他 frame/窗口伪造的响应。
		const { handle, posted, contentWindow } = makeHost({});
		handle._handleIframeLoad();
		const nonce = initNonce(posted);
		handle._handleWindowMessage({
			source: contentWindow, data: requestEnv(nonce, "r1", "admin.auth.get"),
		});
		await ticks();
		const rs = responses(posted, "r1");
		expect(rs).toHaveLength(1);
		expect(rs[0].env.nonce).toBe(nonce);
		// 错误回包同样带 nonce（invalid_params 分支）
		handle._handleWindowMessage({
			source: contentWindow,
			data: requestEnv(nonce, "r2", "admin.auth.get", { evil: 1 }),
		});
		await ticks();
		const rs2 = responses(posted, "r2");
		expect(rs2).toHaveLength(1);
		expect(rs2[0].env.nonce).toBe(nonce);
		// reload 后新 load 的响应对应新 nonce（旧 load 在途请求收到旧 nonce）
		handle._handleWindowMessage({
			source: contentWindow,
			data: requestEnv(nonce, "r3", "admin.auth.get", {}),
		});
		handle._handleIframeLoad(); // r3 在途 → 作废回包（旧 nonce）
		await ticks();
		const rs3 = responses(posted, "r3");
		expect(rs3).toHaveLength(1);
		expect(rs3[0].env.nonce).toBe(nonce);
		const nonce2 = posted
			.filter((p) => p.env.kind === "init")
			.map((p) => p.env.nonce as string)[1];
		expect(nonce2).not.toBe(nonce);
	});
});

describe("AdminBridge host — PR5 UI parity methods (preview + plugins, §10.2)", () => {
	const PARITY_PERMS = [
		"admin:users:write", "admin:plugins:read", "admin:plugins:write",
	];

	it("admin.users.startPreview POSTs /api/admin/preview/start with user_id", async () => {
		const calls: Array<{ url: string; method?: string; body?: unknown }> = [];
		const { handle, posted, contentWindow } = makeHost({
			permissions: PARITY_PERMS,
			fetchJson: async (url, o) => {
				calls.push({
					url, method: (o as { method?: string } | undefined)?.method,
					body: JSON.parse(String((o as { body?: string }).body)),
				});
				return {
					status: 200, ok: true,
					body: { ok: true, preview: { subject_user_id: "usr_9", expires_at: 123 } },
				};
			},
		});
		handle._handleIframeLoad();
		handle._handleWindowMessage({
			source: contentWindow,
			data: requestEnv(nonce0(posted), "r1", "admin.users.startPreview",
				{ user_id: "usr_9" }),
		});
		await ticks();
		const rs = responses(posted, "r1");
		expect(rs[0].env.ok).toBe(true);
		expect(calls).toEqual([{
			url: "/api/admin/preview/start", method: "POST", body: { user_id: "usr_9" },
		}]);
	});

	it("admin.plugins.list GETs /api/admin/plugins verbatim", async () => {
		const list = {
			installations: [{
				installation_id: "inst_1", plugin_id: "sample-tma-score",
				enabled: true, health: "reachable", capabilities: [],
			}],
		};
		const { handle, posted, contentWindow } = makeHost({
			permissions: PARITY_PERMS,
			fetchJson: async (url) => {
				expect(url).toBe("/api/admin/plugins");
				return { status: 200, ok: true, body: list };
			},
		});
		handle._handleIframeLoad();
		handle._handleWindowMessage({
			source: contentWindow, data: requestEnv(nonce0(posted), "r1", "admin.plugins.list"),
		});
		await ticks();
		const rs = responses(posted, "r1");
		expect(rs[0].env.ok).toBe(true);
		expect(rs[0].env.result).toEqual(list);
	});

	it("admin.plugins.setEnabled maps enabled onto enable/disable path", async () => {
		const calls: Array<{ url: string; method?: string }> = [];
		const { handle, posted, contentWindow } = makeHost({
			permissions: PARITY_PERMS,
			fetchJson: async (url, o) => {
				calls.push({ url, method: (o as { method?: string } | undefined)?.method });
				return { status: 200, ok: true, body: { installation_id: "inst_1" } };
			},
		});
		handle._handleIframeLoad();
		const nonce = nonce0(posted);
		handle._handleWindowMessage({
			source: contentWindow,
			data: requestEnv(nonce, "r1", "admin.plugins.setEnabled",
				{ installation_id: "inst_1", enabled: false }),
		});
		handle._handleWindowMessage({
			source: contentWindow,
			data: requestEnv(nonce, "r2", "admin.plugins.setEnabled",
				{ installation_id: "inst_1", enabled: true }),
		});
		await ticks();
		expect(calls[0]).toEqual({ url: "/api/admin/plugins/inst_1/disable", method: "POST" });
		expect(calls[1]).toEqual({ url: "/api/admin/plugins/inst_1/enable", method: "POST" });
	});

	it("admin.plugins.rotateSecret POSTs and passes the secret through once", async () => {
		const calls: Array<{ url: string; method?: string; body?: unknown }> = [];
		const { handle, posted, contentWindow } = makeHost({
			permissions: PARITY_PERMS,
			fetchJson: async (url, o) => {
				calls.push({
					url, method: (o as { method?: string } | undefined)?.method,
					body: (o as { body?: string } | undefined)?.body
						? JSON.parse(String((o as { body?: string }).body)) : undefined,
				});
				return {
					status: 200, ok: true,
					// 新明文 secret 仅本次响应出现（一次性透传给插件 UI 展示）
					body: { installation_id: "inst_2", secret: "new-secret-once" },
				};
			},
		});
		handle._handleIframeLoad();
		handle._handleWindowMessage({
			source: contentWindow,
			data: requestEnv(nonce0(posted), "r1", "admin.plugins.rotateSecret",
				{ installation_id: "inst_2" }),
		});
		await ticks();
		const rs = responses(posted, "r1");
		expect(rs[0].env.ok).toBe(true);
		expect(rs[0].env.result).toEqual({
			installation_id: "inst_2", secret: "new-secret-once",
		});
		expect(calls).toEqual([{
			url: "/api/admin/plugins/inst_2/rotate-secret", method: "POST", body: {},
		}]);
	});

	it("plugin path params with / are rejected before any backend call; write perms enforced", async () => {
		const calls: string[] = [];
		const { handle, posted, contentWindow } = makeHost({
			permissions: PARITY_PERMS,
			fetchJson: async (url) => {
				calls.push(url);
				return { status: 200, ok: true, body: {} };
			},
		});
		handle._handleIframeLoad();
		const nonce = nonce0(posted);
		// 路径参数含 "/" → invalid_params（pathId 防护，后端不可达）
		handle._handleWindowMessage({
			source: contentWindow,
			data: requestEnv(nonce, "r1", "admin.plugins.setEnabled",
				{ installation_id: "inst/../../api/admin/users", enabled: true }),
		});
		// schema 门：缺 enabled / 缺 installation_id / 多余字段
		handle._handleWindowMessage({
			source: contentWindow,
			data: requestEnv(nonce, "r2", "admin.plugins.setEnabled",
				{ installation_id: "inst_1" }),
		});
		handle._handleWindowMessage({
			source: contentWindow,
			data: requestEnv(nonce, "r3", "admin.plugins.rotateSecret", {}),
		});
		handle._handleWindowMessage({
			source: contentWindow,
			data: requestEnv(nonce, "r4", "admin.users.startPreview",
				{ user_id: "u1", extra: 1 }),
		});
		await ticks();
		for (const rid of ["r1", "r2", "r3", "r4"]) {
			const rs = responses(posted, rid);
			expect(rs, rid).toHaveLength(1);
			expect((rs[0].env.error as { code: string }).code).toBe("invalid_params");
		}
		expect(calls).toEqual([]);
		// 权限门：未申请 admin:plugins:* → permission_denied（schema 已过）
		const { handle: h2, posted: p2, contentWindow: cw2 } = makeHost({
			permissions: ["admin:users:write"], // 无 plugins 权限
			fetchJson: async () => ({ status: 200, ok: true, body: {} }),
		});
		h2._handleIframeLoad();
		h2._handleWindowMessage({
			source: cw2,
			data: requestEnv(nonce0(p2), "p1", "admin.plugins.list"),
		});
		await ticks();
		expect((responses(p2, "p1")[0].env.error as { code: string }).code)
			.toBe("permission_denied");
	});
});

function nonce0(posted: Posted[]): string {
	return initNonce(posted);
}
