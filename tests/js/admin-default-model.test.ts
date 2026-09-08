/**
 * 平台默认模型（0.4.2）——宿主桥 + 插件 UI 两层锁定。
 *
 * 后端契约（已上线，勿改）：GET /api/admin/v1/settings/model（owner-only）→
 * {model, provider_kind, image_transport, options:[{model, label,
 * files_supported, expires_at?}]}；PUT 同路径 body {model} →
 * {ok, model, image_transport, transport_adjusted}（切到限时模型且原
 * transport=deepseek_files 时服务端自动落回 inline 并置
 * transport_adjusted=true；400 invalid_request = model 不在允许集合）。
 *
 * 本文件锁定两层（模式同 admin-identity-conflicts.test.ts）：
 *  - 宿主桥（static/admin-host.js）：METHOD_PERMISSIONS 注册
 *    admin.settings.model（admin:settings:read）与
 *    admin.settings.model.update（admin:settings:write）；参数 schema
 *    （读零参数、写仅必填 model 字符串）；GET/PUT 的 URL/方法与 body 形状
 *    （body 原样 {model}）；后端错误信封原样透传；
 *  - 插件 UI（plugins/pathtogether-admin/ui/main.js）：设置页「平台默认
 *    模型」卡渲染（options 动态填充且 label 含限时到期标注、select 值=
 *    options 里的当前项、kv 摘要：当前模型/provider/图片传输/传输联动）；
 *    模型拉取失败卡片独立降级（显示不可用，不阻塞设置页其它卡）；保存流
 *    （update 载荷精确 {model}、transport_adjusted=true 成功文案含自动落回
 *    inline 联动说明、400 invalid_request 错误文案）；限时模型保存前页内
 *    确认条明示到期（取消不发任何请求）。
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
// 宿主桥侧（模式同 admin-bridge.test.ts / admin-identity-conflicts.test.ts）
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
			"admin:settings:read", "admin:settings:write",
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

const MODEL_SETTINGS = {
	model: "deepseek-v4-flash-vision-exp",
	provider_kind: "deepseek_official",
	image_transport: "deepseek_files",
	options: [
		{
			model: "deepseek-v4-flash-vision-exp",
			label: "DeepSeek V4 Flash Vision（正式）",
			files_supported: true,
		},
		{
			model: "deepseek-v4.1-flash-expires-on-0910",
			label: "DeepSeek V4.1 Flash（限时）",
			files_supported: false,
			expires_at: "2026-09-10",
		},
	],
};

describe("宿主桥 — 平台默认模型方法（0.4.2）", () => {
	it("METHOD_PERMISSIONS 注册两方法（读 settings:read / 写 settings:write）+ schema 同步", () => {
		const { AdminBridgeHost } = loadHostModule();
		expect(AdminBridgeHost.METHOD_PERMISSIONS["admin.settings.model"])
			.toBe("admin:settings:read");
		expect(AdminBridgeHost.METHOD_PERMISSIONS["admin.settings.model.update"])
			.toBe("admin:settings:write");
		const schemas = AdminBridgeHost.METHOD_PARAM_SCHEMAS as Record<
			string, { properties: Record<string, unknown>; required?: string[]; additionalProperties?: boolean }
		>;
		const read = schemas["admin.settings.model"];
		expect(read.additionalProperties).toBe(false);
		expect(Object.keys(read.properties)).toEqual([]);
		const write = schemas["admin.settings.model.update"];
		expect(write.additionalProperties).toBe(false);
		expect(write.required).toEqual(["model"]);
		expect(Object.keys(write.properties)).toEqual(["model"]);
	});

	it("settings.model → GET /api/admin/v1/settings/model，result 原样透传（含 options.expires_at）", async () => {
		const { handle, posted, contentWindow, calls } = makeHost({
			respond: () => ({ status: 200, ok: true, body: MODEL_SETTINGS }),
		});
		handle._handleIframeLoad();
		handle._handleWindowMessage({
			source: contentWindow,
			data: requestEnv(initNonce(posted), "r1", "admin.settings.model"),
		});
		await ticks();
		expect(calls).toHaveLength(1);
		expect(calls[0].url).toBe("/api/admin/v1/settings/model");
		const rs = responses(posted, "r1");
		expect(rs).toHaveLength(1);
		expect(rs[0].env.ok).toBe(true);
		expect(rs[0].env.result).toEqual(MODEL_SETTINGS);
	});

	it("settings.model.update → PUT body 原样 {model}；transport_adjusted 响应原样透传", async () => {
		const { handle, posted, contentWindow, calls } = makeHost({
			respond: () => ({
				status: 200, ok: true,
				body: {
					ok: true, model: "deepseek-v4.1-flash-expires-on-0910",
					image_transport: "inline", transport_adjusted: true,
				},
			}),
		});
		handle._handleIframeLoad();
		handle._handleWindowMessage({
			source: contentWindow,
			data: requestEnv(initNonce(posted), "r1", "admin.settings.model.update",
				{ model: "deepseek-v4.1-flash-expires-on-0910" }),
		});
		await ticks();
		expect(calls).toHaveLength(1);
		expect(calls[0].url).toBe("/api/admin/v1/settings/model");
		expect(calls[0].opts.method).toBe("PUT");
		expect(JSON.parse(String(calls[0].opts.body)))
			.toEqual({ model: "deepseek-v4.1-flash-expires-on-0910" });
		const rs = responses(posted, "r1");
		expect(rs).toHaveLength(1);
		expect(rs[0].env.ok).toBe(true);
		expect(rs[0].env.result).toEqual({
			ok: true, model: "deepseek-v4.1-flash-expires-on-0910",
			image_transport: "inline", transport_adjusted: true,
		});
	});

	it("schema 门：读拒绝任意参数；写缺/空/非字符串 model 与附加字段即拒（后端零调用）", async () => {
		const { handle, posted, contentWindow, calls } = makeHost({});
		handle._handleIframeLoad();
		const nonce = initNonce(posted);
		handle._handleWindowMessage({
			source: contentWindow,
			data: requestEnv(nonce, "r1", "admin.settings.model", { q: "x" }),
		});
		handle._handleWindowMessage({
			source: contentWindow,
			data: requestEnv(nonce, "r2", "admin.settings.model.update", {}),
		});
		handle._handleWindowMessage({
			source: contentWindow,
			data: requestEnv(nonce, "r3", "admin.settings.model.update", { model: "" }),
		});
		handle._handleWindowMessage({
			source: contentWindow,
			data: requestEnv(nonce, "r4", "admin.settings.model.update", { model: 42 }),
		});
		handle._handleWindowMessage({
			source: contentWindow,
			data: requestEnv(nonce, "r5", "admin.settings.model.update",
				{ model: "m1", transport: "inline" }),
		});
		await ticks();
		for (const rid of ["r1", "r2", "r3", "r4", "r5"]) {
			const rs = responses(posted, rid);
			expect(rs, rid).toHaveLength(1);
			expect(rs[0].env.ok).toBe(false);
			expect((rs[0].env.error as { code: string }).code).toBe("invalid_params");
		}
		// 全部在桥层参数门被拒：后端零调用
		expect(handle.stats().denied).toBe(5);
		expect(handle.stats().handled).toBe(0);
		expect(calls).toHaveLength(0);
	});

	it("权限门：manifest 未申请 admin:settings:read|write → permission_denied", async () => {
		const { handle, posted, contentWindow } = makeHost({
			permissions: ["admin:overview:read"],
		});
		handle._handleIframeLoad();
		const nonce = initNonce(posted);
		handle._handleWindowMessage({
			source: contentWindow,
			data: requestEnv(nonce, "r1", "admin.settings.model"),
		});
		handle._handleWindowMessage({
			source: contentWindow,
			data: requestEnv(nonce, "r2", "admin.settings.model.update", { model: "m1" }),
		});
		await ticks();
		for (const rid of ["r1", "r2"]) {
			const rs = responses(posted, rid);
			expect(rs, rid).toHaveLength(1);
			expect((rs[0].env.error as { code: string }).code).toBe("permission_denied");
		}
	});

	it("后端 400 invalid_request（model 不在允许集合）信封原样透传", async () => {
		const { handle, posted, contentWindow } = makeHost({
			respond: () => ({
				status: 400, ok: false,
				body: { error: { code: "invalid_request", message: "model 不在允许集合" } },
			}),
		});
		handle._handleIframeLoad();
		handle._handleWindowMessage({
			source: contentWindow,
			data: requestEnv(initNonce(posted), "r1", "admin.settings.model.update",
				{ model: "not-in-allowlist" }),
		});
		await ticks();
		const rs = responses(posted, "r1");
		expect(rs).toHaveLength(1);
		expect(rs[0].env.ok).toBe(false);
		expect(rs[0].env.error).toEqual({
			code: "invalid_request", message: "model 不在允许集合",
		});
	});
});

// --------------------------------------------------------------------------- //
// 插件 UI 侧（模式同 admin-identity-conflicts.test.ts 的假 DOM 装配）
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
		disabled: false,
		_focusCalls: 0,
		_listeners: listeners,
		focus() {
			el._focusCalls += 1;
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

const NONCE = "dd".repeat(32);

function bootSettings(bus: ReturnType<typeof loadPluginUiWithBus>) {
	bus.dispatch(bus.parent, {
		kind: "init", bridge: "admin", protocolVersion: "1.0.0",
		nonce: NONCE,
		adminPermissions: ["admin:settings:read", "admin:settings:write"],
	});
	expect(bus.client.handshakeState().ready).toBe(true);
	bus.client.showPage("settings");
	return ticksUi(4);
}

const SETTINGS_OK = {
	ok: true as const,
	result: {
		registration: { mode: "closed", stored_mode: "closed",
			precondition_failures: [], supported_modes: ["closed"] },
		spend: { available: true, enforcement_mode: "shadow", policies: {} },
		runtime: { available: true, limits: {} },
	},
};

async function renderModelCard(bus: ReturnType<typeof loadPluginUiWithBus>) {
	await bootSettings(bus);
	replyMethod(bus, NONCE, "admin.settings.get", SETTINGS_OK);
	replyMethod(bus, NONCE, "admin.settings.model", {
		ok: true, result: MODEL_SETTINGS,
	});
	await ticksUi(6);
}

describe("插件 UI — 平台默认模型卡渲染（0.4.2）", () => {
	it("options 动态填充（label 含限时到期）、select 值=options 里的当前项、kv 摘要齐全", async () => {
		const bus = loadPluginUiWithBus();
		await renderModelCard(bus);
		// 选项 = 服务端允许集合（label + 到期标注）
		const options = bus.created.filter((el) => el.tagName === "OPTION");
		expect(options).toHaveLength(2);
		expect(options[0].value).toBe("deepseek-v4-flash-vision-exp");
		expect(options[0].textContent).toContain("DeepSeek V4 Flash Vision（正式）");
		expect(options[1].value).toBe("deepseek-v4.1-flash-expires-on-0910");
		expect(options[1].textContent).toContain("（2026-09-10 到期）");
		// select 值 = options 里的当前项（非限时正式模型）
		expect(bus.els["adm-model-select"].value).toBe("deepseek-v4-flash-vision-exp");
		// kv：当前模型 / provider / 图片传输 / 传输联动提示
		const info = bus.els["adm-model-info"].textContent;
		expect(info).toContain("deepseek-v4-flash-vision-exp");
		expect(info).toContain("deepseek_official");
		expect(info).toContain("deepseek_files");
		expect(info).toContain("切到限时模型将自动落回 inline");
		// 保存按钮可用
		expect(bus.els["adm-model-save-btn"].disabled).toBe(false);
		// 页级状态 ready（模型卡不阻塞其它卡）
		expect(bus.els["adm-state-settings"].getAttribute("data-page-state")).toBe("ready");
	});

	it("模型拉取失败：卡片显示不可用且保存禁用，页级 ready 不阻塞、不弹全局错误", async () => {
		const bus = loadPluginUiWithBus();
		await bootSettings(bus);
		replyMethod(bus, NONCE, "admin.settings.get", SETTINGS_OK);
		replyMethod(bus, NONCE, "admin.settings.model", {
			ok: false,
			error: { code: "permission_denied", message: "manifest 未申请 admin:settings:read" },
		});
		await ticksUi(6);
		const info = bus.els["adm-model-info"].textContent;
		expect(info).toContain("不可用");
		expect(info).toContain("permission_denied");
		expect(bus.els["adm-model-save-btn"].disabled).toBe(true);
		// 其它卡照常（页级 ready），全局错误卡不弹（独立降级）
		expect(bus.els["adm-state-settings"].getAttribute("data-page-state")).toBe("ready");
		expect(bus.els["adm-error-card"].hidden).toBe(true);
	});

	it("HTML 结构：模型卡在注册模式卡后、消费额度策略卡前，四件套 + label 齐备", () => {
		expect(htmlSrc).toContain('id="adm-model-card"');
		expect(htmlSrc).toContain("<h2>平台默认模型</h2>");
		expect(htmlSrc).toContain('id="adm-model-select"');
		expect(htmlSrc).toContain('id="adm-model-save-btn"');
		expect(htmlSrc).toContain('id="adm-model-status"');
		expect(htmlSrc).toContain('id="adm-model-info"');
		expect(htmlSrc).toMatch(/<label[^>]*for=["']adm-model-select["']/);
		const cardAt = htmlSrc.indexOf('id="adm-model-card"');
		const regAt = htmlSrc.indexOf('id="adm-regmode-info"');
		const spendAt = htmlSrc.indexOf("消费额度策略");
		expect(cardAt).toBeGreaterThan(regAt);
		expect(cardAt).toBeLessThan(spendAt);
		// 说明文案：限时到期提示 + 即时生效语义
		const blockEnd = htmlSrc.indexOf("</section>",
			htmlSrc.indexOf('id="adm-model-info"'));
		const block = htmlSrc.slice(cardAt, blockEnd);
		expect(block).toContain("到期");
		expect(block).toContain("之后的 AI 会话");
		// main.js 接线：读/写两方法 + 保存按钮
		expect(pluginSrc).toContain('request("admin.settings.model", {})');
		expect(pluginSrc).toContain('request("admin.settings.model.update", { model: model })');
		expect(pluginSrc).toContain('onClick("adm-model-save-btn", saveDefaultModel)');
	});
});

describe("插件 UI — 默认模型保存流（0.4.2）", () => {
	it("非限时模型直接保存：update 载荷精确 {model}，无确认条；成功后刷新（重拉 model）", async () => {
		const bus = loadPluginUiWithBus();
		await renderModelCard(bus);
		bus.els["adm-model-select"].value = "deepseek-v4-flash-vision-exp";
		bus.els["adm-model-save-btn"]._fire("click", {});
		await ticksUi(2);
		// 非限时模型不出现确认条（askConfirm 未被触达 → 元素从未查询），
		// 直接发请求
		expect(bus.els["adm-model-confirm"]).toBeFalsy();
		expect(bus.created.some((el) => el.textContent === "确认执行")).toBe(false);
		const req = bus.parentPosted
			.filter((p) => p.env.kind === "request" &&
				p.env.method === "admin.settings.model.update").at(-1);
		expect(req).toBeTruthy();
		expect(req!.env.payload).toEqual({ model: "deepseek-v4-flash-vision-exp" });
		replyMethod(bus, NONCE, "admin.settings.model.update", {
			ok: true,
			result: {
				ok: true, model: "deepseek-v4-flash-vision-exp",
				image_transport: "deepseek_files", transport_adjusted: false,
			},
		});
		await ticksUi(6);
		const status = bus.els["adm-model-status"].textContent;
		expect(status).toContain("deepseek-v4-flash-vision-exp");
		expect(status).not.toContain("自动落回 inline");
		// 成功后整页刷新：settings.get 与 model 都重拉
		const modelReqs = bus.parentPosted.filter((p) =>
			p.env.kind === "request" && p.env.method === "admin.settings.model");
		expect(modelReqs.length).toBeGreaterThanOrEqual(2);
		const settingsReqs = bus.parentPosted.filter((p) =>
			p.env.kind === "request" && p.env.method === "admin.settings.get");
		expect(settingsReqs.length).toBeGreaterThanOrEqual(2);
	});

	it("限时模型保存前出现页内确认条（该模型 2026-09-10 到期，到期后需切回）；取消不发请求", async () => {
		const bus = loadPluginUiWithBus();
		await renderModelCard(bus);
		bus.els["adm-model-select"].value = "deepseek-v4.1-flash-expires-on-0910";
		bus.els["adm-model-save-btn"]._fire("click", {});
		const box = bus.els["adm-model-confirm"];
		expect(box.hidden).toBe(false);
		expect(box.textContent).toContain("该模型 2026-09-10 到期");
		expect(box.textContent).toContain("到期后需切回");
		// 取消：确认条复位，且没有任何 update 请求
		const cancelBtn = bus.created.filter((el) => el.textContent === "取消").at(-1);
		cancelBtn!._fire("click", {});
		expect(box.hidden).toBe(true);
		expect(box.textContent).toBe("");
		expect(bus.parentPosted.filter((p) =>
			p.env.kind === "request" && p.env.method === "admin.settings.model.update"))
			.toHaveLength(0);
	});

	it("确认后发 update；transport_adjusted=true 成功文案含自动落回 inline 联动说明", async () => {
		const bus = loadPluginUiWithBus();
		await renderModelCard(bus);
		bus.els["adm-model-select"].value = "deepseek-v4.1-flash-expires-on-0910";
		bus.els["adm-model-save-btn"]._fire("click", {});
		const okBtn = bus.created.filter((el) => el.textContent === "确认执行").at(-1);
		okBtn!._fire("click", {});
		await ticksUi(2);
		const req = bus.parentPosted
			.filter((p) => p.env.kind === "request" &&
				p.env.method === "admin.settings.model.update").at(-1);
		expect(req!.env.payload).toEqual({
			model: "deepseek-v4.1-flash-expires-on-0910",
		});
		replyMethod(bus, NONCE, "admin.settings.model.update", {
			ok: true,
			result: {
				ok: true, model: "deepseek-v4.1-flash-expires-on-0910",
				image_transport: "inline", transport_adjusted: true,
			},
		});
		await ticksUi(6);
		const status = bus.els["adm-model-status"].textContent;
		expect(status).toContain("deepseek-v4.1-flash-expires-on-0910");
		expect(status).toContain("自动落回 inline");
	});

	it("400 invalid_request：errText 原样展示，不伪装成功、不刷新", async () => {
		const bus = loadPluginUiWithBus();
		await renderModelCard(bus);
		bus.els["adm-model-select"].value = "deepseek-v4-flash-vision-exp";
		bus.els["adm-model-save-btn"]._fire("click", {});
		await ticksUi(2);
		replyMethod(bus, NONCE, "admin.settings.model.update", {
			ok: false,
			error: { code: "invalid_request", message: "model 不在允许集合" },
		});
		await ticksUi(6);
		const status = bus.els["adm-model-status"].textContent;
		expect(status).toContain("invalid_request");
		expect(status).toContain("model 不在允许集合");
		expect(status).not.toContain("已切换");
		expect(bus.els["adm-error-card"].hidden).toBe(false);
		// 失败不刷新（仍只有首屏那一次 model 请求）
		const modelReqs = bus.parentPosted.filter((p) =>
			p.env.kind === "request" && p.env.method === "admin.settings.model");
		expect(modelReqs).toHaveLength(1);
	});
});
