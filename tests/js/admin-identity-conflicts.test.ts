/**
 * 身份冲突清单 + 孤儿处置（review-2026-09-08 P2-2 产品闭环）。
 *
 * 后端契约（已上线，勿改）：GET /api/admin/v1/users/identity-conflicts
 * （owner-only，items + counts）；POST /api/admin/v1/users/<user_id>/
 * discard-pending（物理删除孤儿 pending 行，不可逆；409 not_discardable
 * 含 has_dependents 语义文案）。仅 discardable=true（pending_bind_synthetic）
 * 的行可处置。
 *
 * 本文件锁定两层：
 *   - 宿主桥（static/admin-host.js，模式同 admin-bridge.test.ts）：
 *     METHOD_PERMISSIONS 注册 admin.users.identityConflicts（users:read）/
 *     admin.users.discardPending（users:write）；参数 schema（identityConflicts
 *     零参数、discardPending 仅 user_id）；后端 URL/方法映射；409 错误信封
 *     code+message 原样透传；
 *   - 插件 UI（plugins/pathtogether-admin/ui/main.js，模式同
 *     admin-plugin-ui.test.ts 的假 DOM 装配）：进入页面发清单请求并渲染
 *     计数/表格；仅 discardable 行有「删除孤儿账号」按钮；删除走页内确认条
 *     （明示不可逆物理删除，取消不发请求）；成功后刷新清单；409 文案原样
 *     展示，绝不静默或伪装成功。
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

describe("宿主桥 — 身份冲突清单/孤儿处置方法注册（review P2-2）", () => {
	it("METHOD_PERMISSIONS 注册两个新方法：清单=users:read，处置=users:write；schema 同步", () => {
		const { AdminBridgeHost } = loadHostModule();
		expect(AdminBridgeHost.METHOD_PERMISSIONS["admin.users.identityConflicts"])
			.toBe("admin:users:read");
		expect(AdminBridgeHost.METHOD_PERMISSIONS["admin.users.discardPending"])
			.toBe("admin:users:write");
		const schemas = AdminBridgeHost.METHOD_PARAM_SCHEMAS as Record<
			string, { properties: Record<string, unknown>; required?: string[]; additionalProperties?: boolean }
		>;
		expect(schemas["admin.users.identityConflicts"].additionalProperties).toBe(false);
		const discard = schemas["admin.users.discardPending"];
		expect(discard.additionalProperties).toBe(false);
		expect(discard.required).toEqual(["user_id"]);
		expect(Object.keys(discard.properties)).toEqual(["user_id"]);
	});

	it("identityConflicts → GET /api/admin/v1/users/identity-conflicts，result=响应体", async () => {
		const body = {
			items: [{
				user_id: "u1", login_id: "pending-abc@bind.invalid",
				email_normalized: null, email_verified: false, role: "user",
				disabled: false, activation_state: "pending_activation",
				activation_source: "email_verify", conflicts: ["pending_bind_synthetic"],
				discardable: true,
			}],
			counts: { pending_bind_synthetic: 1, total_conflicting_rows: 1 },
		};
		const { handle, posted, contentWindow, calls } = makeHost({
			respond: () => ({ status: 200, ok: true, body }),
		});
		handle._handleIframeLoad();
		handle._handleWindowMessage({
			source: contentWindow,
			data: requestEnv(initNonce(posted), "r1", "admin.users.identityConflicts"),
		});
		await ticks();
		expect(calls).toHaveLength(1);
		expect(calls[0].url).toBe("/api/admin/v1/users/identity-conflicts");
		const rs = responses(posted, "r1");
		expect(rs).toHaveLength(1);
		expect(rs[0].env.ok).toBe(true);
		expect(rs[0].env.result).toEqual(body);
	});

	it("discardPending → POST /api/admin/v1/users/<uid>/discard-pending（空 JSON 体）", async () => {
		const { handle, posted, contentWindow, calls } = makeHost({
			respond: () => ({
				status: 200, ok: true,
				body: { ok: true, discarded: { user_id: "u1" } },
			}),
		});
		handle._handleIframeLoad();
		handle._handleWindowMessage({
			source: contentWindow,
			data: requestEnv(initNonce(posted), "r1", "admin.users.discardPending",
				{ user_id: "usr_AbCdEfGh" }),
		});
		await ticks();
		expect(calls).toHaveLength(1);
		expect(calls[0].url).toBe("/api/admin/v1/users/usr_AbCdEfGh/discard-pending");
		expect(calls[0].opts.method).toBe("POST");
		expect(calls[0].opts.body).toBe("{}");
		const rs = responses(posted, "r1");
		expect(rs).toHaveLength(1);
		expect(rs[0].env.ok).toBe(true);
		expect(rs[0].env.result).toEqual({ ok: true, discarded: { user_id: "u1" } });
	});

	it("参数 schema 门：identityConflicts 拒绝任意参数；discardPending 缺/多/含路径分隔符 user_id 即拒", async () => {
		const { handle, posted, contentWindow } = makeHost({});
		handle._handleIframeLoad();
		const nonce = initNonce(posted);
		handle._handleWindowMessage({
			source: contentWindow,
			data: requestEnv(nonce, "r1", "admin.users.identityConflicts", { q: "x" }),
		});
		handle._handleWindowMessage({
			source: contentWindow,
			data: requestEnv(nonce, "r2", "admin.users.discardPending", {}),
		});
		handle._handleWindowMessage({
			source: contentWindow,
			data: requestEnv(nonce, "r3", "admin.users.discardPending",
				{ user_id: "u1", force: true }),
		});
		handle._handleWindowMessage({
			source: contentWindow,
			data: requestEnv(nonce, "r4", "admin.users.discardPending",
				{ user_id: "a/b" }),
		});
		await ticks();
		for (const rid of ["r1", "r2", "r3", "r4"]) {
			const rs = responses(posted, rid);
			expect(rs, rid).toHaveLength(1);
			expect(rs[0].env.ok).toBe(false);
			expect((rs[0].env.error as { code: string }).code).toBe("invalid_params");
		}
		// r1/r2/r3 在桥层参数门被拒；r4（含 "/"）过了 schema、由 pathId 在
		// backend 映射内抛 invalid_params——已计入 handled，同样稳定拒绝
		expect(handle.stats().handled).toBe(1);
		expect(handle.stats().denied).toBe(3);
	});

	it("权限门：manifest 未申请 admin:users:read / admin:users:write → permission_denied", async () => {
		const { handle, posted, contentWindow } = makeHost({
			permissions: ["admin:overview:read"],
		});
		handle._handleIframeLoad();
		const nonce = initNonce(posted);
		handle._handleWindowMessage({
			source: contentWindow,
			data: requestEnv(nonce, "r1", "admin.users.identityConflicts"),
		});
		handle._handleWindowMessage({
			source: contentWindow,
			data: requestEnv(nonce, "r2", "admin.users.discardPending", { user_id: "u1" }),
		});
		await ticks();
		for (const rid of ["r1", "r2"]) {
			const rs = responses(posted, rid);
			expect(rs, rid).toHaveLength(1);
			expect((rs[0].env.error as { code: string }).code).toBe("permission_denied");
		}
	});

	it("后端 409 信封原样透传：code=not_discardable，message 保留 has_dependents 语义文案", async () => {
		const serverMessage =
			"该账号存在业务关联行（并非孤儿），已拒绝删除；请先人工核查";
		const { handle, posted, contentWindow } = makeHost({
			respond: () => ({
				status: 409, ok: false,
				body: { error: { code: "not_discardable", message: serverMessage } },
			}),
		});
		handle._handleIframeLoad();
		handle._handleWindowMessage({
			source: contentWindow,
			data: requestEnv(initNonce(posted), "r1", "admin.users.discardPending",
				{ user_id: "u1" }),
		});
		await ticks();
		const rs = responses(posted, "r1");
		expect(rs).toHaveLength(1);
		expect(rs[0].env.ok).toBe(false);
		expect(rs[0].env.error).toEqual({ code: "not_discardable", message: serverMessage });
	});
});

// --------------------------------------------------------------------------- //
// 插件 UI 侧（模式同 admin-plugin-ui.test.ts 的假 DOM 装配）
// --------------------------------------------------------------------------- //
function fakeEl(tag?: string) {
	const attrs: Record<string, string> = {};
	const children: Array<{ textContent?: string }> = [];
	const listeners: Record<string, Array<(ev?: unknown) => void>> = {};
	let ownText = "";
	const el = {
		tagName: String(tag || "div").toUpperCase(),
		hidden: true,
		className: "",
		htmlFor: "",
		id: "",
		placeholder: "",
		autocomplete: "",
		minLength: 0,
		maxLength: 0,
		colSpan: 0,
		open: false,
		_focusCalls: 0,
		_listeners: listeners,
		focus() {
			el._focusCalls += 1;
		},
		get classList() {
			return {
				add: (...names: string[]) => {
					const set = new Set(el.className.split(/\s+/).filter(Boolean));
					for (const n of names) set.add(n);
					el.className = [...set].join(" ");
				},
				remove: (...names: string[]) => {
					const set = new Set(el.className.split(/\s+/).filter(Boolean));
					for (const n of names) set.delete(n);
					el.className = [...set].join(" ");
				},
				contains: (n: string) =>
					el.className.split(/\s+/).filter(Boolean).includes(n),
			};
		},
		get textContent() {
			let out = ownText;
			for (const c of children) out += (c && c.textContent) || "";
			return out;
		},
		set textContent(v: string) {
			ownText = String(v ?? "");
			children.length = 0;
		},
		value: "",
		disabled: false,
		checked: false,
		appendChild(c: { textContent?: string }) {
			children.push(c);
			return c;
		},
		addEventListener(type: string, fn: (ev?: unknown) => void) {
			(listeners[type] ||= []).push(fn);
		},
		_fire(type: string, ev?: unknown) {
			for (const fn of listeners[type] || []) fn(ev);
		},
		getAttribute(name: string) {
			return Object.prototype.hasOwnProperty.call(attrs, name)
				? attrs[name] : null;
		},
		setAttribute(name: string, value: string) {
			attrs[name] = String(value);
		},
		removeAttribute(name: string) {
			delete attrs[name];
		},
		querySelectorAll: () => [] as unknown[],
		contains: (_other: unknown) => false,
		closest: () => null,
	};
	return el;
}

type FakeEl = ReturnType<typeof fakeEl>;

function loadPluginUiWithBus(hash = "") {
	const els: Record<string, FakeEl> = {};
	const messageHandlers: Array<(event: unknown) => void> = [];
	const created: FakeEl[] = [];
	const parentPosted: Array<{ env: Record<string, unknown> }> = [];
	const parent = {
		postMessage(env: Record<string, unknown>, _targetOrigin: string) {
			parentPosted.push({ env });
		},
	};
	const w: Record<string, unknown> = {
		location: { hash },
		parent,
		addEventListener(type: string, handler: (event: unknown) => void) {
			if (type === "message") messageHandlers.push(handler);
		},
		setTimeout,
		clearTimeout,
	};
	const doc = {
		activeElement: null as { focus?: () => void } | null,
		getElementById(id: string) {
			if (!els[id]) els[id] = fakeEl();
			return els[id];
		},
		createElement: (tag?: string) => {
			const el = fakeEl(tag);
			created.push(el);
			return el;
		},
		createTextNode: (text: string) => ({ textContent: text }),
		addEventListener() {},
	};
	(w as { document: typeof doc }).document = doc;
	new Function("window", "document", pluginSrc)(w, doc);
	const dispatch = (source: unknown, data: unknown) => {
		for (const h of messageHandlers) h({ source, data });
	};
	return {
		els,
		created,
		parent,
		parentPosted,
		dispatch,
		client: w.PathTogetherAdminClient as {
			showPage: (page: string) => void;
			handshakeState: () => { ready: boolean };
		},
	};
}

const ticksUi = async (n = 4) => {
	for (let i = 0; i < n; i++) await tick();
};

/** 便捷：回复 bus 中尚未应答的指定 method 请求（一次性快照应答）。 */
function replyMethod(
	bus: ReturnType<typeof loadPluginUiWithBus>,
	nonce: string,
	method: string,
	reply: { ok: boolean; result?: unknown; error?: unknown },
) {
	for (const posted of bus.parentPosted) {
		if (posted.env.kind !== "request") continue;
		if (String(posted.env.method) !== method) continue;
		bus.dispatch(bus.parent, {
			kind: "response", bridge: "admin", nonce,
			requestId: posted.env.requestId,
			ok: reply.ok, result: reply.result, error: reply.error,
		});
	}
}

const NONCE = "ec".repeat(32);

function bootIdentity(bus: ReturnType<typeof loadPluginUiWithBus>) {
	bus.dispatch(bus.parent, {
		kind: "init", bridge: "admin", protocolVersion: "1.0.0",
		nonce: NONCE,
		adminPermissions: ["admin:users:read", "admin:users:write"],
	});
	expect(bus.client.handshakeState().ready).toBe(true);
}

const CONFLICT_REPORT = {
	items: [
		{
			user_id: "u_orphan", login_id: "pending-a1b2c3@bind.invalid",
			display_name: null, email_normalized: null, email_verified: false,
			role: "user", disabled: false, activation_state: "pending_activation",
			activation_source: "email_verify",
			conflicts: ["pending_bind_synthetic"], discardable: true,
		},
		{
			user_id: "u_legacy", login_id: "legacy01",
			display_name: "老账号", email_normalized: "legacy01@x.com",
			email_verified: true, role: "user", disabled: false,
			activation_state: "active", activation_source: null,
			conflicts: ["login_id_not_email", "email_login_mismatch"],
			discardable: false,
		},
		{
			user_id: "u_shared", login_id: "a@x.com",
			display_name: null, email_normalized: "a@x.com", email_verified: false,
			role: "user", disabled: false, activation_state: "email_pending",
			activation_source: "email_verify",
			conflicts: ["email_shared", "email_login_mismatch"],
			discardable: false, email_shared_key: "a@x.com",
		},
	],
	counts: {
		login_id_not_email: 1, email_login_mismatch: 2,
		pending_bind_synthetic: 1, email_shared: 1,
		total_conflicting_rows: 3,
	},
};

describe("插件 UI — 身份冲突页渲染（review P2-2）", () => {
	it("进入页面发 identityConflicts；渲染计数摘要 + 冲突行（徽章/未验证/仅报告）", async () => {
		const bus = loadPluginUiWithBus();
		bootIdentity(bus);
		bus.client.showPage("identity");
		await ticksUi();
		const req = bus.parentPosted
			.filter((p) => p.env.kind === "request" &&
				p.env.method === "admin.users.identityConflicts").at(-1);
		expect(req).toBeTruthy();
		expect(req!.env.payload).toEqual({});
		replyMethod(bus, NONCE, "admin.users.identityConflicts", {
			ok: true, result: CONFLICT_REPORT,
		});
		await ticksUi(4);
		const st = bus.els["adm-state-identity"];
		expect(st.getAttribute("data-page-state")).toBe("ready");
		expect(st.textContent).toContain("已更新");
		// 计数摘要（含可处置孤儿计数与危险色语义）
		const kpis = bus.els["adm-identity-kpis"].textContent;
		expect(kpis).toContain("冲突行总数");
		expect(kpis).toContain("待补绑孤儿（可处置）");
		expect(kpis).toContain("登录名非邮箱");
		expect(kpis).toContain("邮箱与登录名不一致");
		expect(kpis).toContain("邮箱复用");
		// 表格行：user_id / 登录账号 / 邮箱 + 冲突徽章
		const tbody = bus.els["adm-identity-tbody"].textContent;
		expect(tbody).toContain("u_orphan");
		expect(tbody).toContain("pending-a1b2c3@bind.invalid");
		expect(tbody).toContain("u_legacy");
		expect(tbody).toContain("legacy01@x.com");
		expect(tbody).toContain("待补绑孤儿");
		expect(tbody).toContain("登录名非邮箱");
		expect(tbody).toContain("邮箱与登录名不一致");
		expect(tbody).toContain("邮箱复用");
		// email_verified=false 的行有「未验证」徽标
		expect(tbody).toContain("未验证");
		// 非 discardable 行只报告、不提供处置
		expect(tbody).toContain("仅报告");
	});

	it("空清单：中性空态解释为什么为空（不渲染成错误）", async () => {
		const bus = loadPluginUiWithBus();
		bootIdentity(bus);
		bus.client.showPage("identity");
		await ticksUi();
		replyMethod(bus, NONCE, "admin.users.identityConflicts", {
			ok: true,
			result: { items: [], counts: { total_conflicting_rows: 0 } },
		});
		await ticksUi(4);
		const st = bus.els["adm-state-identity"];
		expect(st.getAttribute("data-page-state")).toBe("empty");
		expect(st.textContent).toContain("没有身份冲突行");
		expect(bus.els["adm-error-card"].hidden).toBe(true);
	});

	it("清单失败：error 态 + 重试，绝不渲染成空态", async () => {
		const bus = loadPluginUiWithBus();
		bootIdentity(bus);
		bus.client.showPage("identity");
		await ticksUi();
		replyMethod(bus, NONCE, "admin.users.identityConflicts", {
			ok: false,
			error: { code: "permission_denied", message: "manifest 未申请 admin:users:read" },
		});
		await ticksUi(4);
		const st = bus.els["adm-state-identity"];
		expect(st.getAttribute("data-page-state")).toBe("error");
		expect(st.textContent).toContain("permission_denied");
	});
});

describe("插件 UI — 孤儿处置（仅 discardable 行可删除；确认 + 409 文案）", () => {
	async function renderOneOrphan() {
		const bus = loadPluginUiWithBus();
		bootIdentity(bus);
		bus.client.showPage("identity");
		await ticksUi();
		replyMethod(bus, NONCE, "admin.users.identityConflicts", {
			ok: true, result: CONFLICT_REPORT,
		});
		await ticksUi(4);
		return bus;
	}

	it("仅 discardable=true 的行有「删除孤儿账号」按钮（实心 danger），其余行只报告", async () => {
		const bus = await renderOneOrphan();
		const deleteBtns = bus.created.filter((el) =>
			el.textContent === "删除孤儿账号" && el._listeners &&
			el._listeners.click);
		expect(deleteBtns).toHaveLength(1); // 3 行里只有孤儿行可处置
		expect(deleteBtns[0].className).toBe("adm-btn-danger");
		const reportCells = bus.created.filter((el) =>
			el.tagName === "TD" && el.className === "adm-actions-cell");
		expect(reportCells.length).toBe(3);
	});

	it("删除走页内确认条：明示不可逆物理删除；取消不发任何请求", async () => {
		const bus = await renderOneOrphan();
		const deleteBtn = bus.created.find((el) =>
			el.textContent === "删除孤儿账号" && el._listeners && el._listeners.click);
		deleteBtn!._fire("click", {});
		const box = bus.els["adm-identity-confirm"];
		expect(box.hidden).toBe(false);
		expect(box.textContent).toContain("物理删除");
		expect(box.textContent).toContain("不可逆");
		expect(box.textContent).toContain("pending-a1b2c3@bind.invalid");
		expect(box.textContent).toContain("u_orphan");
		// 确认按钮是实心 danger + 获得焦点（新交互内容可达）
		const okBtn = bus.created.filter((el) => el.textContent === "确认执行").at(-1);
		expect(okBtn!.className).toBe("adm-btn-danger");
		expect(okBtn!._focusCalls).toBeGreaterThanOrEqual(1);
		// 取消：确认条复位，且没有任何 discardPending 请求
		const cancelBtn = bus.created.filter((el) => el.textContent === "取消").at(-1);
		cancelBtn!._fire("click", {});
		expect(box.hidden).toBe(true);
		expect(box.textContent).toBe("");
		expect(bus.parentPosted.filter((p) =>
			p.env.kind === "request" && p.env.method === "admin.users.discardPending"))
			.toHaveLength(0);
	});

	it("确认后发 discardPending（user_id 精确载荷）；成功后提示并刷新清单", async () => {
		const bus = await renderOneOrphan();
		const deleteBtn = bus.created.find((el) =>
			el.textContent === "删除孤儿账号" && el._listeners && el._listeners.click);
		deleteBtn!._fire("click", {});
		bus.created.filter((el) => el.textContent === "确认执行").at(-1)!._fire("click", {});
		await ticksUi(2);
		const req = bus.parentPosted
			.filter((p) => p.env.kind === "request" &&
				p.env.method === "admin.users.discardPending").at(-1);
		expect(req).toBeTruthy();
		expect(req!.env.payload).toEqual({ user_id: "u_orphan" });
		replyMethod(bus, NONCE, "admin.users.discardPending", {
			ok: true, result: { ok: true, discarded: { user_id: "u_orphan" } },
		});
		await ticksUi(4);
		// 成功提示（不可逆语义如实呈现）
		expect(bus.els["adm-identity-status"].textContent).toContain("已物理删除");
		expect(bus.els["adm-identity-status"].textContent).toContain("不可逆");
		// 成功后自动刷新清单（第二条 identityConflicts 请求）
		const listReqs = bus.parentPosted.filter((p) =>
			p.env.kind === "request" && p.env.method === "admin.users.identityConflicts");
		expect(listReqs.length).toBeGreaterThanOrEqual(2);
		replyMethod(bus, NONCE, "admin.users.identityConflicts", {
			ok: true, result: { items: [], counts: { total_conflicting_rows: 0 } },
		});
		await ticksUi(4);
		expect(bus.els["adm-state-identity"].getAttribute("data-page-state")).toBe("empty");
	});

	it("409 not_discardable：后端 message（has_dependents 语义）原样展示，不伪装成功", async () => {
		const bus = await renderOneOrphan();
		const deleteBtn = bus.created.find((el) =>
			el.textContent === "删除孤儿账号" && el._listeners && el._listeners.click);
		deleteBtn!._fire("click", {});
		bus.created.filter((el) => el.textContent === "确认执行").at(-1)!._fire("click", {});
		await ticksUi(2);
		replyMethod(bus, NONCE, "admin.users.discardPending", {
			ok: false,
			error: {
				code: "not_discardable",
				message: "该账号存在业务关联行（并非孤儿），已拒绝删除；请先人工核查",
			},
		});
		await ticksUi(4);
		const status = bus.els["adm-identity-status"].textContent;
		expect(status).toContain("not_discardable");
		expect(status).toContain("该账号存在业务关联行（并非孤儿），已拒绝删除；请先人工核查");
		// 全局错误条同时出现（code + message）
		expect(bus.els["adm-error-card"].hidden).toBe(false);
		expect(bus.els["adm-error-text"].textContent).toContain("not_discardable");
		// 失败后清单不刷新（无第二条清单请求）
		const listReqs = bus.parentPosted.filter((p) =>
			p.env.kind === "request" && p.env.method === "admin.users.identityConflicts");
		expect(listReqs).toHaveLength(1);
	});
});

describe("插件 UI — 身份冲突页结构（HTML 源码断言）", () => {
	it("index.html 具备导航入口、页面骨架、确认条与刷新按钮", () => {
		expect(htmlSrc).toContain('data-page="identity"');
		expect(htmlSrc).toContain('id="adm-page-identity"');
		expect(htmlSrc).toContain('id="adm-identity-kpis"');
		expect(htmlSrc).toContain('id="adm-identity-tbody"');
		expect(htmlSrc).toContain('id="adm-identity-confirm"');
		expect(htmlSrc).toContain('id="adm-identity-refresh-btn"');
		expect(htmlSrc).toContain('id="adm-identity-status"');
		// 页面说明必须明示：只有孤儿行可删除、删除不可逆、其余只报告
		const pageStart = htmlSrc.indexOf('id="adm-page-identity"');
		const pageEnd = htmlSrc.indexOf('id="adm-page-slides"');
		const page = htmlSrc.slice(pageStart, pageEnd);
		expect(page).toContain("pending-*@bind.invalid");
		expect(page).toContain("不可逆");
		expect(page).toContain("绝不自动合并");
	});

	it("main.js 注册 identity 深链与页标题；宿主桥两方法已接线", () => {
		expect(pluginSrc).toContain('identity: "身份冲突"');
		expect(pluginSrc).toContain('identity: $("adm-page-identity")');
		expect(pluginSrc).toMatch(/var pages = \["overview", "users", "identity"/);
		// 渲染只认 discardable 标记；不认 conflicts 数组猜测
		expect(pluginSrc).toMatch(/if \(item\.discardable\) \{/);
	});
});
