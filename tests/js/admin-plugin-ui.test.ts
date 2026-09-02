/**
 * pathtogether-admin 插件 UI 最小装配测试（PR5 修订 + v0.3 P2 修订 +
 * 2026-09-03 wave 2 收敛，review-2026-09-02-upload-user-limits-admin-ui-cleanup.md
 * §4 / Batch C5-6 / D1 / D2-3 / §4.7 金额精度）。
 *
 * 插件页运行在 /admin 宿主页的 opaque iframe 内，无 jsdom 环境；本文件沿用
 * admin-preview.test.ts 的「new Function + 假 window/document」模式，锁定：
 *   - main.js 在缺省 DOM 下加载不抛错（所有页面/按钮绑定均为可选探测）；
 *   - 导出 PathTogetherAdminClient（request/showPage/handshakeState +
 *     金额换算 cnyToNano/nanoToCnyString/formatCny2/fmtNano/fmtCny，仅测试用）；
 *   - §8.3 P2（对称认证）：onMessage 一律先验 event.source === window.parent；
 *   - §5 v0.3 P2（金额十进制字符串）：cnyToNano 字符串进字符串出；
 *   - §4.7（wave 2）：formatCny2 两位小数、半分进位（half away from zero），
 *     全程 BigInt，不经 JS Number/toFixed；17.8064508→17.81、17.804→17.80、
 *     -1.235→-1.24；耗尽/超额状态按原始 nano 判断；
 *   - §4.3（wave 2）：spend.total / spend.window 互斥形态，两者同时出现 =
 *     显式契约错误；user 抽屉唯一金额动作 = 设置总额度/恢复默认（CAS）；
 *   - §4.4/§4.6：邀请页无任何来源/归因内容；费用页 = KPI + [仅异常]告警 +
 *     Demo 卡 + 三页内标签（只有当前标签发请求，迟到旧响应按代际丢弃）；
 *   - D2-3：siteStats 桥不可达时概览站点访问卡整卡隐藏。
 */
import { describe, expect, it } from "vitest";
import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const src = readFileSync(
	resolve(here, "../../plugins/pathtogether-admin/ui/main.js"), "utf8");
// wave 2：HTML/CSS 源码级断言（结构/label/折叠/按钮语义/退役入口不复活）
const htmlSrc = readFileSync(
	resolve(here, "../../plugins/pathtogether-admin/ui/index.html"), "utf8");
const cssSrc = readFileSync(
	resolve(here, "../../plugins/pathtogether-admin/ui/style.css"), "utf8");

interface Posted {
	env: Record<string, unknown>;
	targetOrigin: string;
}

interface FakeListener {
	(ev?: unknown): void;
}

function fakeEl(tag?: string) {
	// 可交互假元素：属性字典 + 子节点文本累积（textContent 语义与 DOM 对齐）。
	const attrs: Record<string, string> = {};
	const children: Array<{ textContent?: string }> = [];
	const listeners: Record<string, FakeListener[]> = {};
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
		addEventListener(type: string, fn: FakeListener) {
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

function loadPluginUi(hash: string) {
	const els: Record<string, FakeEl> = {};
	const location = { hash };
	const w: Record<string, unknown> = {
		location,
		parent: { postMessage() {} },
		addEventListener() {},
		setTimeout,
		clearTimeout,
	};
	const doc = {
		getElementById(id: string) {
			if (!els[id]) els[id] = fakeEl();
			return els[id];
		},
		createElement: () => fakeEl(),
		createTextNode: (text: string) => ({ textContent: text }),
		addEventListener() {},
	};
	(w as { document: typeof doc }).document = doc;
	new Function("window", "document", src)(w, doc);
	return {
		els,
		client: w.PathTogetherAdminClient as PluginClient | undefined,
	};
}

interface PluginClient {
	request: (method: string, payload?: unknown) => Promise<unknown>;
	showPage: (page: string) => void;
	cnyToNano: (text: unknown) => string | null;
	nanoToCnyString: (n: unknown) => string;
	formatCny2: (v: unknown) => string | null;
	fmtNano: (v: unknown) => string;
	fmtCny: (v: unknown) => string;
	fmtTs: (epoch: unknown) => string;
	handshakeState: () => { ready: boolean; grantedCount: number };
}

// 可交互装配：捕获 message 监听器与 window.parent.postMessage，可模拟宿主
// init / 响应 / 伪造消息（P2 对称认证用例）。录制 document 级监听器（抽屉
// Esc/Tab 焦点管理）与 createElement 产物（按钮语义类、raw values 等）。
function loadPluginUiWithBus(hash = "") {
	const els: Record<string, FakeEl> = {};
	const messageHandlers: Array<(event: unknown) => void> = [];
	const docHandlers: Record<string, FakeListener[]> = {};
	const created: FakeEl[] = [];
	const parentPosted: Posted[] = [];
	const parent = {
		postMessage(env: Record<string, unknown>, targetOrigin: string) {
			parentPosted.push({ env, targetOrigin });
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
		addEventListener(type: string, fn: FakeListener) {
			(docHandlers[type] ||= []).push(fn);
		},
	};
	(w as { document: typeof doc }).document = doc;
	new Function("window", "document", src)(w, doc);
	const dispatch = (source: unknown, data: unknown) => {
		for (const h of messageHandlers) h({ source, data });
	};
	const fireDocument = (type: string, ev?: unknown) => {
		for (const fn of docHandlers[type] || []) fn(ev);
	};
	return {
		els,
		doc,
		created,
		fireDocument,
		parent,
		parentPosted,
		dispatch,
		client: w.PathTogetherAdminClient as PluginClient | undefined,
	};
}

const tick = () => new Promise((r) => setTimeout(r, 0));
const ticks = async (n = 3) => {
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

describe("pathtogether-admin plugin UI bootstrap (PR5)", () => {
	it("loads without throwing and exports the bridge client", () => {
		const { client } = loadPluginUi("");
		expect(client).toBeTruthy();
		expect(typeof client!.request).toBe("function");
		expect(typeof client!.showPage).toBe("function");
		expect(typeof client!.handshakeState).toBe("function");
		expect(typeof client!.formatCny2).toBe("function");
		// 未握手：请求应拒绝（not_ready），showPage 切页不抛错（含 plugins/settings）
		expect(() => client!.showPage("plugins")).not.toThrow();
		expect(() => client!.showPage("overview")).not.toThrow();
		expect(() => client!.showPage("settings")).not.toThrow();
	});

	it("initial page whitelist includes the plugins page; unknown falls back", () => {
		// plugins 在白名单内：hash 透传后不再回概览（宿主深链 #plugins）
		const a = loadPluginUi("#plugins");
		expect(a.client).toBeTruthy();
		// 未知 slug：装配仍成功并回 overview（白名单校验在模块内部完成）
		const b = loadPluginUi("#no-such-page");
		expect(b.client).toBeTruthy();
	});

	it("wave 2：邀请/费用页在白名单内（slug 不变，标题收敛）；设置页元素已绑定", () => {
		expect(loadPluginUi("#invites").client).toBeTruthy();
		expect(loadPluginUi("#billing").client).toBeTruthy();
		const a = loadPluginUi("#settings");
		expect(a.client).toBeTruthy();
		expect(a.els["adm-page-settings"]).toBeTruthy();
		expect(a.els["adm-regmode-save-btn"]).toBeTruthy();
		expect(a.els["adm-spend-save-btn"]).toBeTruthy();
		expect(a.els["adm-rt-save-btn"]).toBeTruthy();
		expect(a.els["adm-win-demo-adjust-btn"]).toBeTruthy();
		expect(a.els["adm-win-owner-adjust-btn"]).toBeTruthy();
		// wave 2：注册模式写控件只在设置页；邀请页只有只读摘要 + 跳转
		expect(htmlSrc).toContain('id="adm-regmode-select"');
		expect(htmlSrc).toContain('id="adm-invite-goto-settings-btn"');
	});
});

describe("pathtogether-admin plugin UI — response source/nonce auth (§8.3 P2)", () => {
	const NONCE = "a".repeat(64);

	function boot({ client, dispatch, parent }: ReturnType<typeof loadPluginUiWithBus>) {
		dispatch(parent, {
			kind: "init", bridge: "admin", protocolVersion: "1.0.0",
			nonce: NONCE, adminPermissions: [],
		});
		expect(client!.handshakeState().ready).toBe(true);
	}

	it("drops responses whose event.source is not window.parent", async () => {
		const bus = loadPluginUiWithBus();
		boot(bus);
		let settled = false;
		const p = bus.client!.request("admin.overview.get", {}).then(
			(v) => { settled = true; return v; },
			() => { settled = true; });
		await ticks();
		const env = bus.parentPosted[bus.parentPosted.length - 1].env;
		expect(env.nonce).toBe(NONCE);
		// 伪造来源（其他 frame/窗口）的响应：即使 nonce/requestId 全对也丢弃
		bus.dispatch({ fake: "window" }, {
			kind: "response", bridge: "admin", nonce: NONCE,
			requestId: env.requestId, ok: true, result: { forged: true },
		});
		await ticks();
		expect(settled).toBe(false);
		void p;
	});

	it("drops responses whose nonce does not match the init session nonce", async () => {
		const bus = loadPluginUiWithBus();
		boot(bus);
		let settled = false;
		bus.client!.request("admin.overview.get", {}).then(
			(v) => { settled = true; return v; },
			() => { settled = true; });
		await ticks();
		const env = bus.parentPosted[bus.parentPosted.length - 1].env;
		bus.dispatch(bus.parent, {
			kind: "response", bridge: "admin", nonce: "b".repeat(64),
			requestId: env.requestId, ok: true, result: { forged: true },
		});
		bus.dispatch(bus.parent, {
			kind: "response", bridge: "admin",
			requestId: env.requestId, ok: true, result: { forged: true },
		});
		await ticks();
		expect(settled).toBe(false);
	});

	it("resolves the promise for a correct source + nonce + requestId response", async () => {
		const bus = loadPluginUiWithBus();
		boot(bus);
		const p = bus.client!.request("admin.overview.get", {});
		await ticks();
		const env = bus.parentPosted[bus.parentPosted.length - 1].env;
		bus.dispatch(bus.parent, {
			kind: "response", bridge: "admin", nonce: NONCE,
			requestId: env.requestId, ok: true, result: { users: { total: 1 } },
		});
		await expect(p).resolves.toEqual({ users: { total: 1 } });
	});
});

describe("pathtogether-admin plugin UI — 金额精度（§4.7 wave 2 formatCny2）", () => {
	it("cnyToNano returns a decimal string; >19 nano digits or bad shapes reject", () => {
		const { client } = loadPluginUi("");
		expect(client!.cnyToNano("12.5")).toBe("12500000000");
		expect(client!.cnyToNano("0.000000001")).toBe("1");
		expect(client!.cnyToNano("-0.5")).toBe("-500000000");
		expect(client!.cnyToNano("0")).toBe("0");
		expect(client!.cnyToNano("0012")).toBe("12000000000"); // 前导零归一
		expect(client!.cnyToNano("10000000000")).toBeNull();
		expect(client!.cnyToNano("1.0000000001")).toBeNull();
		expect(client!.cnyToNano("abc")).toBeNull();
		expect(client!.cnyToNano("")).toBeNull();
	});

	it("nanoToCnyString converts precisely via BigInt（仅技术详情/输入回显用）", () => {
		const { client } = loadPluginUi("");
		expect(client!.nanoToCnyString("12345678901")).toBe("12.345678901");
		expect(client!.nanoToCnyString("-500000000")).toBe("-0.5");
		expect(client!.nanoToCnyString("0")).toBe("0");
		expect(client!.nanoToCnyString("1000000000")).toBe("1");
		expect(client!.nanoToCnyString(null)).toBe("");
		// 2^53 之外仍精确（Number 路径会失真）
		expect(client!.nanoToCnyString("9007199254740993")).toBe("9007199.254740993");
	});

	it("formatCny2：两位小数、半分进位（away from zero）、全程 BigInt", () => {
		const { client } = loadPluginUi("");
		// §7.2 验收锚点（nano 入参）
		expect(client!.formatCny2("17806450800")).toBe("17.81"); // 17.8064508 → 17.81
		expect(client!.formatCny2("17804000000")).toBe("17.80"); // 17.804 → 17.80
		expect(client!.formatCny2("-1235000000")).toBe("-1.24"); // -1.235 → -1.24
		// 半分进位边界
		expect(client!.formatCny2("5000000")).toBe("0.01");   // 0.005 → 0.01
		expect(client!.formatCny2("4999999")).toBe("0.00");   // 0.00499999 → 0.00
		expect(client!.formatCny2("-4999999")).toBe("0.00");  // 半分进位方向对称
		expect(client!.formatCny2("15000000")).toBe("0.02");  // 0.015 → 0.02
		// 常规值恰好两位小数
		expect(client!.formatCny2("12500000000")).toBe("12.50");
		expect(client!.formatCny2("0")).toBe("0.00");
		expect(client!.formatCny2("1000000000")).toBe("1.00");
		// 大值不经 Number（>2^53 nano 仍精确）
		expect(client!.formatCny2("9007199254740993")).toBe("9007199.25");
		// 空/非法：null（调用方回显原值或「—」，绝不伪造 0）
		expect(client!.formatCny2(null)).toBeNull();
		expect(client!.formatCny2("")).toBeNull();
		expect(client!.formatCny2("not-a-number")).toBeNull();
	});

	it("fmtCny：所有面向人 CNY 恰好两位小数；非法值显式回显原值", () => {
		const { client } = loadPluginUi("");
		expect(client!.fmtCny("12500000000")).toBe("12.50 CNY");
		expect(client!.fmtCny("-500000000")).toBe("-0.50 CNY");
		expect(client!.fmtCny("0")).toBe("0.00 CNY");
		expect(client!.fmtCny(null)).toBe("—");
		expect(client!.fmtCny("9007199254740993")).toBe("9007199.25 CNY");
		expect(client!.fmtCny("not-a-number")).toBe("not-a-number");
		// 0 < remaining < 0.005：显示 0.00，但状态判断按原始 nano（见 remainingInfo 用例）
		expect(client!.fmtCny("4000000")).toBe("0.00 CNY");
	});

	it("fmtNano renders exact CNY + raw nano（技术详情专用，不经 Number）", () => {
		const { client } = loadPluginUi("");
		expect(client!.fmtNano("12500000000")).toBe("12.5 CNY（12500000000 nano）");
		expect(client!.fmtNano("-500000000")).toBe("-0.5 CNY（-500000000 nano）");
		expect(client!.fmtNano(null)).toBe("—");
		expect(client!.fmtNano("9007199254740993"))
			.toBe("9007199.254740993 CNY（9007199254740993 nano）");
	});
});

// --------------------------------------------------------------------------- //
// §8.2：未握手时切页必须等待而非报错。
// --------------------------------------------------------------------------- //
describe("pathtogether-admin plugin UI — not-ready pages wait instead of erroring (§8.2)", () => {
	it("switching pages before handshake shows a waiting state, not the global error card", async () => {
		const bus = loadPluginUiWithBus();
		expect(bus.client!.handshakeState().ready).toBe(false);
		bus.client!.showPage("overview");
		await ticks(4);
		expect(bus.els["adm-error-card"].hidden).toBe(true);
		expect(bus.parentPosted.filter((p) => p.env.kind === "request")).toHaveLength(0);
	});
});

// --------------------------------------------------------------------------- //
// wave 2（§4.4）：邀请页 = 注册模式只读摘要 + 邀请列表；不再有任何
// 来源/归因内容（漏斗/用户来源/first·last touch/首次 AI）。
// --------------------------------------------------------------------------- //
describe("wave 2 — 邀请页解耦归因（§4.4）", () => {
	const NONCE = "c".repeat(64);

	function bootAndWait(bus: ReturnType<typeof loadPluginUiWithBus>) {
		bus.dispatch(bus.parent, {
			kind: "init", bridge: "admin", protocolVersion: "1.0.0",
			nonce: NONCE, adminPermissions: ["admin:settings:read", "admin:invites:read"],
		});
	}

	/** 回复邀请页首屏的两路请求（settings.get / invites.list）。 */
	function replyAll(
		bus: ReturnType<typeof loadPluginUiWithBus>,
		reply: (method: string) => { ok: boolean; result?: unknown; error?: unknown } | null,
	) {
		for (const posted of bus.parentPosted) {
			if (posted.env.kind !== "request") continue;
			const r = reply(String(posted.env.method));
			if (!r) continue;
			bus.dispatch(bus.parent, {
				kind: "response", bridge: "admin", nonce: NONCE,
				requestId: posted.env.requestId, ok: r.ok,
				result: r.result, error: r.error,
			});
		}
	}

	it("页面终态：两路都成功即 ready（不再有「加载中…」滞留），零邀请给表内提示", async () => {
		const bus = loadPluginUiWithBus();
		bootAndWait(bus);
		bus.client!.showPage("invites");
		await ticks(4);
		replyAll(bus, (method) => {
			if (method === "admin.settings.get") {
				return { ok: true, result: { registration: { mode: "closed" } } };
			}
			return { ok: true, result: { invites: [], next_cursor: null } };
		});
		await ticks(6);
		const st = bus.els["adm-state-invites"];
		expect(st.getAttribute("data-page-state")).toBe("ready");
		expect(st.textContent).toContain("已更新");
		// 注册模式只读摘要来自 settings.get
		expect(bus.els["adm-invite-mode"].textContent).toContain("closed");
		// 归因文案绝不存在（任何元素都不出现旧空态文案）
		const all = Object.values(bus.els).map((el) => el.textContent).join("\n");
		expect(all).not.toContain("暂无来源归因数据");
		expect(all).not.toContain("历史用户尚未回填");
	});

	it("有邀请数据 → ready 且列表渲染（绑定账号/AI/初始总额度/备注/状态列）", async () => {
		const bus = loadPluginUiWithBus();
		bootAndWait(bus);
		bus.client!.showPage("invites");
		await ticks(4);
		replyAll(bus, (method) => {
			if (method === "admin.settings.get") {
				return { ok: true, result: { registration: { mode: "invite_only" } } };
			}
			return {
				ok: true,
				result: {
					invites: [{
						invite_id: "iv1", login_id_masked: "a***1@x.com",
						ai_access: true, total_limit_nano_cny: "2500000000",
						note: "9 月批次", expires_at: Date.now() / 1000 + 3600,
					}],
					next_cursor: null,
				},
			};
		});
		await ticks(6);
		const st = bus.els["adm-state-invites"];
		expect(st.getAttribute("data-page-state")).toBe("ready");
		const tbody = bus.els["adm-invites-tbody"].textContent;
		expect(tbody).toContain("iv1");
		expect(tbody).toContain("2.50 CNY");
		expect(tbody).toContain("9 月批次");
		// 旧 cohort / source·campaign 列不再存在（HTML 结构断言见下方源码用例）
	});

	it("两路全失败 → 终态 error + 重试，绝不渲染成空态", async () => {
		const bus = loadPluginUiWithBus();
		bootAndWait(bus);
		bus.client!.showPage("invites");
		await ticks(4);
		replyAll(bus, () => ({
			ok: false, error: { code: "permission_denied", message: "manifest 未申请" },
		}));
		await ticks(6);
		const st = bus.els["adm-state-invites"];
		expect(st.getAttribute("data-page-state")).toBe("error");
		expect(st.textContent).toContain("permission_denied");
	});

	it("部分失败（settings 挂了）→ ready + partial 提示", async () => {
		const bus = loadPluginUiWithBus();
		bootAndWait(bus);
		bus.client!.showPage("invites");
		await ticks(4);
		replyAll(bus, (method) => {
			if (method === "admin.settings.get") {
				return { ok: false, error: { code: "bridge_timeout", message: "" } };
			}
			return { ok: true, result: { invites: [{ invite_id: "iv1" }], next_cursor: null } };
		});
		await ticks(6);
		const st = bus.els["adm-state-invites"];
		expect(st.getAttribute("data-page-state")).toBe("ready");
		expect(st.textContent).toContain("部分数据加载失败");
	});

	it("创建邀请只发新契约字段（ttl_seconds/total_limit_nano_cny），无归因字段", async () => {
		const bus = loadPluginUiWithBus();
		bootAndWait(bus);
		bus.doc.getElementById("adm-invite-ttl")!.value = "168";
		bus.doc.getElementById("adm-invite-ai")!.checked = true;
		bus.doc.getElementById("adm-invite-limit")!.value = "2.5";
		bus.doc.getElementById("adm-invite-note")!.value = "9 月批次";
		bus.els["adm-invite-create-btn"]._fire("click", {});
		await ticks(2);
		const req = bus.parentPosted
			.filter((p) => p.env.kind === "request" && p.env.method === "admin.invites.create")
			.at(-1);
		expect(req).toBeTruthy();
		expect(req!.env.payload).toEqual({
			ttl_seconds: 604800, ai_access: true,
			total_limit_nano_cny: "2500000000", note: "9 月批次",
		});
		replyMethod(bus, NONCE, "admin.invites.create", {
			ok: true, result: { invite: { invite_id: "iv9", token: "pt-inv-secret" } },
		});
		await ticks(4);
		// 一次性明文码展示
		expect(bus.els["adm-invite-token-box"].hidden).toBe(false);
		expect(bus.els["adm-invite-token"].textContent).toBe("pt-inv-secret");
	});

	it("HTML 源码：邀请页不再包含 source/campaign/cohort/漏斗/用户来源/first·last touch", () => {
		const invStart = htmlSrc.indexOf('id="adm-page-invites"');
		const invEnd = htmlSrc.indexOf('id="adm-page-settings"');
		const invitesPage = htmlSrc.slice(invStart, invEnd);
		for (const banned of [
			"adm-invite-cohort", "adm-invite-source", "adm-invite-campaign",
			"adm-acq-funnel", "adm-acq-users", "adm-acq-more-btn",
			"来源漏斗", "first touch", "last touch", "首次 AI", "campaign_id",
			"source_code", "cohort",
		]) {
			expect(invitesPage, banned).not.toContain(banned);
		}
	});
});

// --------------------------------------------------------------------------- //
// wave 2（§4.2）：概览 = 精简 KPI + 供应商余额/调用缓存 + 条件告警 +
// 站点访问卡（降级隐藏）。
// --------------------------------------------------------------------------- //
describe("wave 2 — 概览页收敛 + 站点访问卡（§4.2 / D2-3）", () => {
	const NONCE = "d2".repeat(32);

	function boot(bus: ReturnType<typeof loadPluginUiWithBus>) {
		bus.dispatch(bus.parent, {
			kind: "init", bridge: "admin", protocolVersion: "1.0.0",
			nonce: NONCE, adminPermissions: ["admin:overview:read"],
		});
	}

	it("KPI 只保留用户/AI/调用/缓存/User 累计已用/unpriced；turn 卡与徽标不再存在", async () => {
		const bus = loadPluginUiWithBus();
		boot(bus);
		bus.client!.showPage("overview");
		await ticks(4);
		replyMethod(bus, NONCE, "admin.siteStats.get", {
			ok: false, error: { code: "unknown_method", message: "未知或未登记的桥方法" },
		});
		replyMethod(bus, NONCE, "admin.overview.get", {
			ok: true,
			result: {
				users: { total: 9, active: 8, disabled: 1, ai_access: 5 },
				billing: {
					available: true, model_calls_period: 42, model_calls_today: 3,
					cache_hit_ratio: 0.5, cache_hit_input_tokens: 100,
					cache_miss_input_tokens: 100, charge_nano_cny: "12500000000",
					unpriced_count: 0,
					provider_balance_snapshot: { total_balance_nano: "86130000000" },
					provider_balance_age_seconds: 30,
				},
			},
		});
		await ticks(6);
		const texts = Object.values(bus.els).map((el) => el.textContent).join("\n");
		expect(texts).toContain("用户总数");
		expect(texts).toContain("AI access 用户");
		expect(texts).toContain("模型调用（本周期）");
		expect(texts).toContain("缓存命中率");
		expect(texts).toContain("User 累计已用");
		expect(texts).toContain("12.50 CNY");
		expect(texts).toContain("86.13 CNY");
		// turn 冻结历史 UI 整体退役（HTML + 渲染两端）
		expect(htmlSrc).not.toContain('id="adm-ov-turn-box"');
		expect(htmlSrc).not.toContain("已退役 · 冻结历史");
		expect(htmlSrc).not.toContain('id="adm-turn-legacy-card"');
		expect(texts).not.toContain("对话额度");
		// unpriced=0：无告警卡（正常状态空告警卡不渲染）
		expect(bus.els["adm-ov-alert-card"].hidden).toBe(true);
		// D2 未发布：siteStats unknown_method → 站点访问卡整卡隐藏
		expect(bus.els["adm-site-card"].hidden).toBe(true);
		const st = bus.els["adm-state-overview"];
		expect(st.getAttribute("data-page-state")).toBe("ready");
	});

	it("告警条条件：unpriced>0 / 余额快照缺失 / reconcile drift 才出现", async () => {
		const bus = loadPluginUiWithBus();
		boot(bus);
		bus.client!.showPage("overview");
		await ticks(4);
		replyMethod(bus, NONCE, "admin.siteStats.get", {
			ok: false, error: { code: "unknown_method", message: "" },
		});
		replyMethod(bus, NONCE, "admin.overview.get", {
			ok: true,
			result: {
				users: { total: 1, active: 1, disabled: 0, ai_access: 1 },
				billing: {
					available: true, unpriced_count: 3, charge_nano_cny: "1000000000",
					reconcile_drift: true, provider_balance_snapshot: null,
				},
			},
		});
		await ticks(6);
		expect(bus.els["adm-ov-alert-card"].hidden).toBe(false);
		const alerts = bus.els["adm-ov-alerts"].textContent;
		expect(alerts).toContain("3 条 unpriced");
		expect(alerts).toContain("reconcile");
		expect(alerts).toContain("暂无快照");
	});

	it("站点访问卡：成功响应渲染 KPI/趋势/Top/最近，geo 未配置时国家块隐藏", async () => {
		const bus = loadPluginUiWithBus();
		boot(bus);
		bus.client!.showPage("overview");
		await ticks(4);
		replyMethod(bus, NONCE, "admin.overview.get", {
			ok: true, result: { users: { total: 1 }, billing: { available: false } },
		});
		replyMethod(bus, NONCE, "admin.siteStats.get", {
			ok: true,
			result: {
				generated_at: 1700000000,
				today: { visits: 12, unique_visitors: 7, bots: 1 },
				d7: { visits: 80, unique_visitors: 41, bots: 4 },
				d30: { visits: 300, unique_visitors: 120, bots: 15 },
				daily: [{ date: "2026-09-01", visits: 10, unique_visitors: 6, bots: 1 }],
				top_referrers: [{ domain: "example.com", visits: 20 }],
				top_pages: [{ page_key: "home", visits: 90 }],
				top_countries: [{ country_code: "unknown", visits: 10 }],
				visitor_kinds: { anonymous_human: 200, signed_in_human: 85, suspected_bot: 15 },
				recent: [{
					occurred_at: 1700000000, page_key: "home",
					referrer_domain: null, country_code: "unknown",
					visitor_kind: "suspected_bot", bot_name: "Googlebot",
				}],
				geo_configured: false,
			},
		});
		await ticks(6);
		expect(bus.els["adm-site-card"].hidden).toBe(false);
		// 假 DOM 中站点卡的子块是独立元素：逐块断言
		const kpis = bus.els["adm-site-kpis"].textContent;
		// KPI：匿名访客日去重次数不得命名为「独立用户数」（帮助文案须明确否定）
		expect(kpis).toContain("匿名访客日去重次数（30 天累计）");
		expect(kpis).toContain("不是独立用户数");
		expect(kpis).toContain("疑似爬虫");
		expect(bus.els["adm-site-referrers-tbody"].textContent).toContain("example.com");
		expect(bus.els["adm-site-pages-tbody"].textContent).toContain("home");
		expect(bus.els["adm-site-recent-tbody"].textContent).toContain("Googlebot");
		expect(bus.els["adm-site-kinds"].textContent).toContain("疑似爬虫");
		// geo_configured=false：国家块隐藏
		expect(bus.els["adm-site-countries-block"].hidden).toBe(true);
		// 站点卡禁止出现用户/邀请/注册/转化内容
		const siteAll = ["adm-site-kpis", "adm-site-daily-tbody", "adm-site-kinds",
			"adm-site-recent-tbody", "adm-site-empty"]
			.map((id) => bus.els[id].textContent).join("\n");
		expect(siteAll).not.toContain("转化");
		expect(siteAll).not.toContain("注册用户");
	});

	it("siteStats permission_denied / not_implemented / backend_error 同样整卡隐藏", async () => {
		for (const code of ["permission_denied", "not_implemented", "backend_error"]) {
			const bus = loadPluginUiWithBus();
			boot(bus);
			bus.client!.showPage("overview");
			await ticks(4);
			replyMethod(bus, NONCE, "admin.overview.get", {
				ok: true, result: { users: { total: 1 } },
			});
			replyMethod(bus, NONCE, "admin.siteStats.get", {
				ok: false, error: { code, message: "" },
			});
			await ticks(6);
			expect(bus.els["adm-site-card"].hidden, code).toBe(true);
			// 降级不报全局错误（低频站长卡不打扰概览）
			expect(bus.els["adm-error-card"].hidden).toBe(true);
		}
	});
});

// --------------------------------------------------------------------------- //
// 包 E 锁定（§9）：用户表精简列 + 详情抽屉、页级四态组件。
// --------------------------------------------------------------------------- //
describe("pathtogether-admin plugin UI — workbench KPI + drawer (§9, 包 E)", () => {
	const NONCE = "d".repeat(64);

	function bootWithOverview(bus: ReturnType<typeof loadPluginUiWithBus>) {
		bus.dispatch(bus.parent, {
			kind: "init", bridge: "admin", protocolVersion: "1.0.0",
			nonce: NONCE, adminPermissions: ["admin:overview:read", "admin:users:read"],
		});
	}

	it("users table keeps high-frequency columns only; details open the drawer", async () => {
		const bus = loadPluginUiWithBus();
		bootWithOverview(bus);
		bus.client!.showPage("users");
		await ticks(4);
		const req = bus.parentPosted
			.filter((p) => p.env.kind === "request" && p.env.method === "admin.users.list")
			.at(-1);
		expect(req).toBeTruthy();
		bus.dispatch(bus.parent, {
			kind: "response", bridge: "admin", nonce: NONCE,
			requestId: req!.env.requestId, ok: true,
			result: {
				items: [{
					user_id: "u1", display_name: "张三", login_id_masked: "z***@x.com",
					role: "user", enabled: true, ai_access: true,
					created_at: 1700000000, registration_method: "invite",
					spend: {
						total: {
							allowance_id: "alw_1", total_limit_nano_cny: "20000000000",
							spent_nano_cny: "3420000000", reserved_nano_cny: "500000000",
							remaining_nano: "16080000000", overage_nano: "0",
							source: "invite", version: 2, cutover_at: 1700000000,
							opening_spent_nano_cny: "3420000000",
						},
					},
					last_ai_call_at: 1700000100,
				}],
				next_cursor: null,
			},
		});
		await ticks(4);
		const tbody = bus.els["adm-users-tbody"].textContent;
		// 表内只剩显示名/角色/状态/额度剩余/操作
		expect(tbody).toContain("张三");
		expect(tbody).toContain("user");
		expect(tbody).toContain("启用");
		expect(tbody).toContain("剩余 16.08 CNY");
		// 低频字段不进表格行（掩码登录账号/allowance 详情只在抽屉里出现）
		expect(tbody).not.toContain("z***@x.com");
		expect(tbody).not.toContain("alw_1");
		expect(bus.els["adm-user-drawer"]).toBeTruthy();
	});

	it("empty users page renders an explained empty state with page-state attribute", async () => {
		const bus = loadPluginUiWithBus();
		bootWithOverview(bus);
		bus.client!.showPage("users");
		await ticks(4);
		const req = bus.parentPosted
			.filter((p) => p.env.kind === "request" && p.env.method === "admin.users.list")
			.at(-1);
		bus.dispatch(bus.parent, {
			kind: "response", bridge: "admin", nonce: NONCE,
			requestId: req!.env.requestId, ok: true,
			result: { items: [], next_cursor: null },
		});
		await ticks(4);
		const st = bus.els["adm-state-users"];
		expect(st.getAttribute("data-page-state")).toBe("empty");
		expect(st.textContent).toContain("暂无用户");
	});
});

// --------------------------------------------------------------------------- //
// UI 升级批次 A 锁定（金额主视图 CNY-only、持久 label、抽屉焦点管理、
// 危险按钮语义、390px 列适配、紧凑握手）——按 wave 2 契约重写。
// --------------------------------------------------------------------------- //

describe("UI 批次A 锁定（wave 2 重写版）", () => {
	const NONCE = "a9".repeat(32);

	function boot(bus: ReturnType<typeof loadPluginUiWithBus>) {
		bus.dispatch(bus.parent, {
			kind: "init", bridge: "admin", protocolVersion: "1.0.0",
			nonce: NONCE, adminPermissions: ["admin:overview:read", "admin:users:read"],
		});
		expect(bus.client!.handshakeState().ready).toBe(true);
	}

	// §4.2 金额显示：主视图两位小数 CNY-only，raw nano 在 adm-raw-values 展开区
	it("批次A-1: 概览主视图两位小数 CNY、无 nano 长串且 raw 可展开", async () => {
		const bus = loadPluginUiWithBus();
		boot(bus);
		bus.client!.showPage("overview");
		await ticks(4);
		replyMethod(bus, NONCE, "admin.siteStats.get", {
			ok: false, error: { code: "unknown_method", message: "" },
		});
		replyMethod(bus, NONCE, "admin.overview.get", {
			ok: true,
			result: {
				users: { total: 2, active: 2, disabled: 0, ai_access: 1 },
				billing: {
					available: true, model_calls_period: 3, model_calls_today: 1,
					cache_hit_ratio: 0.5, cache_hit_input_tokens: 10,
					cache_miss_input_tokens: 10, charge_nano_cny: "12500000000",
					unpriced_count: 0,
					provider_balance_snapshot: { total_balance_nano: "86130000000" },
				},
			},
		});
		await ticks(6);
		const rawBoxes = bus.created.filter((el) =>
			String(el.className).includes("adm-raw-values"));
		expect(rawBoxes.length).toBeGreaterThan(0);
		expect(rawBoxes.map((el) => el.textContent).join("\n")).toContain("12500000000");
		// 主视图不得拼接 nano 长串（§4.2）：剔除 raw 展开区后应无 nano 字样
		let texts = Object.values(bus.els).map((el) => el.textContent).join("\n");
		for (const box of rawBoxes) {
			texts = texts.split(box.textContent).join("");
		}
		expect(texts).toContain("12.50 CNY");
		expect(texts).toContain("86.13 CNY");
		expect(texts).not.toContain("nano");
		expect(texts).not.toContain("12500000000");
	});

	// §4.3 wave 2：额度列按形态渲染（total 短文案四态 + 0<remaining<0.005
	// 显示 0.00 但状态按原始 nano）
	it("批次A-2: 用户行额度剩余四态正确；0<remaining<0.005 显示 0.00 不判耗尽", async () => {
		const bus = loadPluginUiWithBus();
		boot(bus);
		bus.client!.showPage("users");
		await ticks(4);
		replyMethod(bus, NONCE, "admin.users.list", {
			ok: true,
			result: {
				items: [
					{
						user_id: "u1", display_name: "正常户", role: "user", enabled: true,
						ai_access: true,
						spend: { total: {
							allowance_id: "a1", total_limit_nano_cny: "20000000000",
							spent_nano_cny: "3420000000", reserved_nano_cny: "500000000",
							remaining_nano: "16080000000", overage_nano: "0",
							source: "invite", version: 1, cutover_at: 1700000000,
						} },
					},
					{
						user_id: "u2", display_name: "零额度", role: "user", enabled: true,
						ai_access: true,
						spend: { total: {
							allowance_id: "a2", total_limit_nano_cny: "0",
							spent_nano_cny: "0", reserved_nano_cny: "0",
							remaining_nano: "0", overage_nano: "0",
							source: "default", version: 1, cutover_at: 1700000000,
						} },
					},
					{
						user_id: "u3", display_name: "超支柱", role: "user", enabled: true,
						ai_access: true,
						spend: { total: {
							allowance_id: "a3", total_limit_nano_cny: "20000000000",
							spent_nano_cny: "22500000000", reserved_nano_cny: "0",
							remaining_nano: "0", overage_nano: "2500000000",
							source: "admin", version: 3, cutover_at: 1700000000,
						} },
					},
					{
						// 0 < remaining < 0.005 CNY：显示「剩余 0.00 CNY」而非「已用尽」
						user_id: "u4", display_name: "零头户", role: "user", enabled: true,
						ai_access: true,
						spend: { total: {
							allowance_id: "a4", total_limit_nano_cny: "10000000000",
							spent_nano_cny: "9996000000", reserved_nano_cny: "0",
							remaining_nano: "4000000", overage_nano: "0",
							source: "default", version: 1, cutover_at: 1700000000,
						} },
					},
					{
						user_id: "u5", display_name: "owner 月窗", role: "owner", enabled: true,
						ai_access: true,
						spend: { window: {
							window_id: "w5", window_start: 1700000000, window_end: 1702588800,
							limit_nano_snapshot: "1000000000000", spent_nano_cny: "0",
							reserved_nano_cny: "0", remaining_nano: "1000000000000",
							version: 2,
						} },
					},
					{
						user_id: "u6", display_name: "缺剩余", role: "user", enabled: true,
						ai_access: true,
						spend: { total: {
							allowance_id: "a6", total_limit_nano_cny: "20000000000",
							spent_nano_cny: "1000000000", reserved_nano_cny: "0",
							remaining_nano: null, overage_nano: null,
							source: "default", version: 1, cutover_at: 1700000000,
						} },
					},
					{
						user_id: "u7", display_name: "错误形态", role: "user", enabled: true,
						ai_access: true, spend: { error: "pg_backend_required" },
					},
				],
				next_cursor: null,
			},
		});
		await ticks(4);
		const tbody = bus.els["adm-users-tbody"].textContent;
		expect(tbody).toContain("剩余 16.08 CNY");
		expect(tbody).toContain("已用尽");
		expect(tbody).toContain("超支 2.50 CNY");
		// 显示 0.00 但不是「已用尽」（原始 nano 判状态）
		expect(tbody).toContain("剩余 0.00 CNY");
		expect(tbody).toContain("不可用（remaining 缺失）");
		expect(tbody).toContain("pg_backend_required");
		// owner 行用 window 形态的剩余，绝不伪造 total
		expect(tbody).toContain("剩余 1000.00 CNY");
		// 每行 5 个单元格（显示名/角色/状态/额度剩余/操作）
		const rowCount = (tbody.match(/详情/g) || []).length;
		expect(rowCount).toBe(7);
		// 状态格内仍有移动端 AI 堆叠补行
		const stacks = bus.created.filter((el) =>
			String(el.className).includes("adm-stack-mobile"));
		expect(stacks.length).toBeGreaterThanOrEqual(1);
	});

	// §4.3 wave 2：互斥形态契约——total 与 window 同时出现必须显式报错
	it("批次A-2b: spend.total 与 spend.window 同时出现 = 契约错误（不任选其一）", async () => {
		const bus = loadPluginUiWithBus();
		boot(bus);
		bus.client!.showPage("users");
		await ticks(4);
		replyMethod(bus, NONCE, "admin.users.list", {
			ok: true,
			result: {
				items: [{
					user_id: "u1", display_name: "双形态", role: "user", enabled: true,
					ai_access: true,
					spend: {
						total: {
							allowance_id: "a1", total_limit_nano_cny: "10000000000",
							spent_nano_cny: "0", reserved_nano_cny: "0",
							remaining_nano: "10000000000", overage_nano: "0",
							source: "default", version: 1, cutover_at: 1700000000,
						},
						window: {
							window_id: "w1", window_start: 1700000000,
							window_end: 1702588800, limit_nano_snapshot: "20000000000",
							spent_nano_cny: "0", reserved_nano_cny: "0",
							remaining_nano: "20000000000", version: 5,
						},
					},
				}],
				next_cursor: null,
			},
		});
		await ticks(4);
		const tbody = bus.els["adm-users-tbody"].textContent;
		expect(tbody).toContain("契约错误");
		// 打开抽屉：同样显式报错，且不提供任何金额动作（无编辑器输入）
		const detailBtn = bus.created.find((el) => el.textContent === "详情" &&
			el._listeners && el._listeners.click);
		detailBtn!._fire("click", {});
		expect(bus.els["adm-user-drawer"].hidden).toBe(false);
		const body = bus.els["adm-drawer-body"].textContent;
		expect(body).toContain("契约错误");
		// 绝不任选其一渲染：两种形态的数字都不出现
		expect(body).not.toContain("10.00 CNY");
		expect(body).not.toContain("20.00 CNY");
		const editorInput = bus.created.find((el) => el.id === "adm-total-limit-input");
		expect(editorInput).toBeUndefined();
	});

	// §4.4 折叠创建表单：高级折叠项改名「单独总额度」
	it("批次A-3: 创建用户/邀请默认折叠为入口，token 区在展开区内", () => {
		const usersBox = htmlSrc.indexOf('id="adm-users-create-box"');
		expect(usersBox).toBeGreaterThan(-1);
		const usersTag = htmlSrc.slice(htmlSrc.lastIndexOf("<details", usersBox),
			htmlSrc.indexOf(">", usersBox) + 1);
		expect(usersTag).not.toMatch(/\sopen[\s>]/); // 默认折叠
		const usersEnd = htmlSrc.indexOf("<section", usersBox);
		const usersBlock = htmlSrc.slice(usersBox, usersEnd);
		expect(usersBlock).toContain("新建用户");
		expect(usersBlock).toContain('id="adm-users-create-form"');
		expect(usersBlock).toContain('id="adm-users-create-btn"');
		expect(usersBlock).toContain("高级：单独总额度");
		const invBox = htmlSrc.indexOf('id="adm-invite-create-box"');
		expect(invBox).toBeGreaterThan(-1);
		const invTag = htmlSrc.slice(htmlSrc.lastIndexOf("<details", invBox),
			htmlSrc.indexOf(">", invBox) + 1);
		expect(invTag).not.toMatch(/\sopen[\s>]/);
		const invEnd = htmlSrc.indexOf("<section", invBox);
		const invBlock = htmlSrc.slice(invBox, invEnd);
		expect(invBlock).toContain("新建邀请");
		expect(invBlock).toContain('id="adm-invite-create-form"');
		expect(invBlock).toContain('id="adm-invite-create-btn"');
		expect(invBlock).toContain('id="adm-invite-token-box"');
		expect(invBlock).toContain("高级：单独总额度");
	});

	// §4.1 持久 label
	it("批次A-4: 每个关键 input/select 都有真实 <label for>（全量扫描）", () => {
		const ids = [
			// 创建用户
			"adm-users-new-login", "adm-users-new-display", "adm-users-new-password",
			"adm-users-new-limit",
			// 用户筛选
			"adm-users-q", "adm-users-enabled", "adm-users-ai",
			// 创建邀请（wave 2：login/ttl/limit/note；cohort/source/campaign 已删）
			"adm-invite-login", "adm-invite-ttl", "adm-invite-limit", "adm-invite-note",
			// 设置：注册模式 / 三键额度策略 / enforcement / 运行时 / Demo+Owner 立即调整
			"adm-regmode-select", "adm-spend-user-total", "adm-spend-demo-week",
			"adm-spend-owner-month", "adm-spend-mode",
			"adm-rt-psteps", "adm-rt-demosteps", "adm-rt-concurrency",
			"adm-win-demo-limit", "adm-win-owner-limit",
			// 费用页：Demo 统计窗口 / 用量筛选 / 审计筛选
			"adm-demo-window", "adm-usage-model", "adm-usage-user",
			"adm-usage-status", "adm-audit-action",
		];
		const missing = ids.filter((id) =>
			!new RegExp(`<label[^>]*for=["']${id}["']`).test(htmlSrc));
		expect(missing).toEqual([]);
		// 全量扫描：index.html 里每个非复选框 input/select 都必须有 <label for>
		const controlIds = Array.from(htmlSrc.matchAll(/<(?:input|select)\b[^>]*>/g))
			.map((m) => m[0])
			.filter((tag) => !/type=["']checkbox["']/.test(tag))
			.map((tag) => tag.match(/id=["']([^"']+)["']/)?.[1])
			.filter((v): v is string => !!v);
		expect(controlIds.length).toBeGreaterThanOrEqual(ids.length);
		const labeled = new Set(
			Array.from(htmlSrc.matchAll(/<label[^>]*for=["']([^"']+)["']/g))
				.map((m) => m[1]));
		expect(controlIds.filter((id) => !labeled.has(id))).toEqual([]);
		expect(htmlSrc).toMatch(/class=["'][^"']*adm-field-label[^"']*["'][^>]*for=["']adm-spend-user-total["']/);
		expect(cssSrc).toMatch(/\.adm-field-label\s*{[^}]*font-size:\s*1[2-9]px/);
	});

	// §4.10 抽屉语义与焦点管理（user=total 形态 + 总额度编辑器 label）
	it("批次A-5: 抽屉 actions 用 div；打开聚焦关闭钮、Tab 圈定、Esc 关闭并恢复焦点", async () => {
		const bus = loadPluginUiWithBus();
		boot(bus);
		bus.client!.showPage("users");
		await ticks(4);
		replyMethod(bus, NONCE, "admin.users.list", {
			ok: true,
			result: {
				items: [{
					user_id: "u1", display_name: "张三", login_id_masked: "z***@x.com",
					role: "user", enabled: true, ai_access: true,
					spend: { total: {
						allowance_id: "a1", total_limit_nano_cny: "20000000000",
						spent_nano_cny: "3420000000", reserved_nano_cny: "0",
						remaining_nano: "16580000000", overage_nano: "0",
						source: "invite", version: 3, cutover_at: 1700000000,
					} },
				}],
				next_cursor: null,
			},
		});
		await ticks(4);
		const detailBtn = bus.created.find((el) => el.textContent === "详情" &&
			el._listeners && el._listeners.click);
		expect(detailBtn).toBeTruthy();
		detailBtn!._fire("click", {});
		expect(bus.els["adm-user-drawer"].hidden).toBe(false);
		expect(bus.els["adm-drawer-close"]._focusCalls).toBeGreaterThanOrEqual(1);
		// renderUserActions 返回真实 div（不再返回 td）
		const actionsWrap = bus.created.find((el) =>
			el.className === "adm-actions" && el.tagName === "DIV");
		expect(actionsWrap).toBeTruthy();
		expect(bus.created.some((el) => el.tagName === "TD" &&
			el.className === "adm-actions")).toBe(false);
		// 总额度编辑器复用统一 field/label：存在 label[for] 与对应 id 的输入
		const labeled = bus.created.filter((el) => el.htmlFor);
		expect(labeled.length).toBeGreaterThanOrEqual(1);
		// Tab 圈定：末尾焦点 + Tab → 回到第一个可聚焦元素
		const drawerEl = bus.els["adm-user-drawer"];
		const f1 = fakeEl("button");
		const f2 = fakeEl("button");
		const f3 = fakeEl("button");
		f1.hidden = false;
		f2.hidden = false;
		f3.hidden = false;
		drawerEl.querySelectorAll = () => [f1, f2, f3] as unknown as ReturnType<typeof fakeEl.querySelectorAll>;
		bus.doc.activeElement = f3;
		bus.fireDocument("keydown", { key: "Tab", shiftKey: false, preventDefault() {} });
		expect(f1._focusCalls).toBeGreaterThanOrEqual(1);
		// Esc 关闭并恢复触发按钮焦点
		bus.fireDocument("keydown", { key: "Escape" });
		expect(bus.els["adm-user-drawer"].hidden).toBe(true);
		expect(detailBtn!._focusCalls).toBeGreaterThanOrEqual(1);
	});

	// P0-3 回归：抽屉危险操作确认挂抽屉内 #adm-drawer-confirm
	it("批次A-5b: 抽屉危险操作确认走 #adm-drawer-confirm；关闭抽屉时清空", async () => {
		const bus = loadPluginUiWithBus();
		boot(bus);
		bus.client!.showPage("users");
		await ticks(4);
		replyMethod(bus, NONCE, "admin.users.list", {
			ok: true,
			result: {
				items: [{
					user_id: "u1", display_name: "张三", login_id_masked: "z***@x.com",
					role: "user", enabled: true, ai_access: true,
				}],
				next_cursor: null,
			},
		});
		await ticks(4);
		const detailBtn = bus.created.find((el) => el.textContent === "详情" &&
			el._listeners && el._listeners.click);
		expect(detailBtn).toBeTruthy();
		detailBtn!._fire("click", {});
		expect(bus.els["adm-user-drawer"].hidden).toBe(false);
		const disableBtn = bus.created.find((el) => el.textContent === "禁用" &&
			el._listeners && el._listeners.click);
		expect(disableBtn).toBeTruthy();
		disableBtn!._fire("click", {});
		const box = bus.els["adm-drawer-confirm"];
		expect(box.hidden).toBe(false);
		expect(box.textContent).toContain("确认禁用用户 张三");
		const okBtn = bus.created.filter((el) => el.textContent === "确认执行").at(-1);
		expect(okBtn).toBeTruthy();
		expect(okBtn!.className).toBe("adm-btn-danger");
		expect(okBtn!._focusCalls).toBeGreaterThanOrEqual(1);
		okBtn!._fire("click", {});
		await ticks(2);
		const req = bus.parentPosted.filter((p) => p.env.kind === "request").at(-1);
		expect(req?.env.method).toBe("admin.users.setEnabled");
		bus.fireDocument("keydown", { key: "Escape" });
		expect(bus.els["adm-user-drawer"].hidden).toBe(true);
		expect(box.hidden).toBe(true);
		expect(box.textContent).toBe("");
		detailBtn!._fire("click", {});
		const previewBtn = bus.created.find((el) => el.textContent === "身份预览" &&
			el._listeners && el._listeners.click);
		previewBtn!._fire("click", {});
		expect(bus.els["adm-drawer-confirm"].hidden).toBe(false);
		expect(bus.els["adm-drawer-confirm"].textContent).toContain("身份进入只读预览");
	});

	// P1：重置密码输入必须有真实 <label for>
	it("批次A-5c: 重置密码输入有 label[for] + .adm-field 组件；确认重置为实心 danger", async () => {
		const bus = loadPluginUiWithBus();
		boot(bus);
		bus.client!.showPage("users");
		await ticks(4);
		replyMethod(bus, NONCE, "admin.users.list", {
			ok: true,
			result: {
				items: [{
					user_id: "u1", display_name: "张三", login_id_masked: "z***@x.com",
					role: "user", enabled: true, ai_access: true,
				}],
				next_cursor: null,
			},
		});
		await ticks(4);
		const detailBtn = bus.created.find((el) => el.textContent === "详情" &&
			el._listeners && el._listeners.click);
		detailBtn!._fire("click", {});
		const resetBtn = bus.created.find((el) => el.textContent === "重置密码" &&
			el._listeners && el._listeners.click);
		expect(resetBtn).toBeTruthy();
		resetBtn!._fire("click", {});
		const input = bus.created.find((el) => el.tagName === "INPUT" &&
			el.id === "adm-reset-password-input");
		expect(input).toBeTruthy();
		expect(input!.minLength).toBe(15);
		expect(String(input!.autocomplete)).toBe("new-password");
		const label = bus.created.find((el) =>
			el.htmlFor === "adm-reset-password-input");
		expect(label).toBeTruthy();
		expect(String(label!.className)).toContain("adm-field-label");
		expect(label!.textContent).toContain("张三");
		expect(label!.textContent).toContain("15 位");
		expect(String(input!.placeholder)).not.toContain("密码");
		const okBtn = bus.created.filter((el) => el.textContent === "确认重置").at(-1);
		expect(okBtn!.className).toBe("adm-btn-danger");
	});

	// §4.6 wave 2：费用页结构 = KPI → [仅异常]告警条 → Demo 卡 → 三页内标签；
	// 人工调整 / caps / 历史影子 / turn legacy 全部不复活
	it("批次A-6: 费用页重排（KPI/告警条/Demo 卡/三标签）；误导入口全部删除", () => {
		const billingStart = htmlSrc.indexOf('id="adm-page-billing"');
		const billingEnd = htmlSrc.indexOf('id="adm-page-plugins"');
		const billing = htmlSrc.slice(billingStart, billingEnd);
		// 结构顺序：KPI → 告警条（hidden）→ Demo 卡 → 明细标签卡
		const kpiIdx = billing.indexOf('id="adm-bill-kpis"');
		const alertIdx = billing.indexOf('id="adm-bill-alert"');
		const demoIdx = billing.indexOf('id="adm-demo-card"');
		const detailIdx = billing.indexOf('id="adm-detail-card"');
		expect(kpiIdx).toBeGreaterThan(-1);
		expect(alertIdx).toBeGreaterThan(kpiIdx);
		expect(demoIdx).toBeGreaterThan(alertIdx);
		expect(detailIdx).toBeGreaterThan(demoIdx);
		// 三个页内标签 + 单一内容区
		expect(billing).toContain('id="adm-tab-usage"');
		expect(billing).toContain('id="adm-tab-ledger"');
		expect(billing).toContain('id="adm-tab-unpriced"');
		expect(billing.match(/role="tabpanel"/g)?.length).toBe(1);
		expect(billing).toContain('aria-selected="true"');
		// 误导入口（及对应 JS 挂点）不复活
		for (const banned of [
			"adm-adjust-card", "adm-acct-user", "adm-caps-form", "adm-caps-soft",
			"adm-caps-hard", "adm-adjust-btn", "adm-legacy-card", "adm-turn-legacy-card",
			"adm-billing-acct-box", "adm-billing-usage-box", "adm-billing-ledger-box",
			"人工调整", "caps", "历史影子", "赠送",
		]) {
			expect(billing, banned).not.toContain(banned);
		}
		// 中文业务名为标题：模型调用/账务流水/计费异常
		expect(billing).toContain("模型调用");
		expect(billing).toContain("账务流水");
		expect(billing).toContain("计费异常");
		// usage 默认列（时间/用户/模型/状态/输入/输出 token/用户费用）
		const usageStart = billing.indexOf('id="adm-usage-section"');
		const usageEnd = billing.indexOf('id="adm-ledger-section"');
		const usage = billing.slice(usageStart, usageEnd);
		expect(usage).toContain("<th>时间</th>");
		expect(usage).toContain("<th>用户</th>");
		expect(usage).toContain("<th>模型</th>");
		expect(usage).toContain("<th>状态</th>");
		expect(usage).toContain("<th>输入 tokens</th>");
		expect(usage).toContain("<th>输出 tokens</th>");
		expect(usage).toContain("<th>用户费用</th>");
		expect(usage).not.toContain("provider 成本</th>");
		expect(usage).not.toContain("<th>event</th>");
		// ledger 默认列（时间/用户/类型/金额 CNY/原因）
		const ledgerStart = billing.indexOf('id="adm-ledger-section"');
		const ledgerEnd = billing.indexOf('id="adm-unpriced-section"');
		const ledger = billing.slice(ledgerStart, ledgerEnd);
		expect(ledger).toContain("<th>金额（CNY）</th>");
		expect(ledger).toContain("<th>原因</th>");
		expect(ledger).not.toContain("<th>账户</th>");
		expect(ledger).not.toContain("金额（nano）");
		// unpriced 空态中性文案，无红框卡片
		expect(billing).toContain("当前没有未计价事件");
		// 概览不再有 turn 卡
		const ovStart = htmlSrc.indexOf('id="adm-page-overview"');
		const ovEnd = htmlSrc.indexOf('id="adm-page-users"');
		const ov = htmlSrc.slice(ovStart, ovEnd);
		expect(ov).not.toContain("adm-ov-turn");
		// tab 样式存在（aria-selected 高亮 + 可见焦点）
		expect(cssSrc).toMatch(/\.adm-tab\[aria-selected="true"\]/);
		expect(cssSrc).toMatch(/\.adm-tab:focus-visible/);
	});

	// §4.5 危险按钮语义
	it("批次A-7: 危险/普通按钮语义类正确（rotate=outline、调整=实心 danger）", () => {
		expect(cssSrc).toMatch(/\.adm-btn-primary\s*{/);
		expect(cssSrc).toMatch(/\.adm-btn-secondary\s*{/);
		expect(cssSrc).toMatch(/\.adm-btn-danger\s*{/);
		expect(cssSrc).toMatch(/\.adm-btn-danger-outline\s*{/);
		expect(cssSrc).toMatch(/\.adm-btn[^{]*:focus-visible/);
		// 设置页 Demo/Owner 立即调整均为实心 danger
		expect(htmlSrc).toMatch(/id="adm-win-demo-adjust-btn"[^>]*class=["'][^"']*adm-btn-danger/);
		expect(htmlSrc).toMatch(/id="adm-win-owner-adjust-btn"[^>]*class=["'][^"']*adm-btn-danger/);
		// 轮换凭证：次要危险 → danger-outline
		expect(src).toMatch(/actionBtn\("轮换凭证"[\s\S]{0,600}?"danger-outline"/);
		// 供应商余额刷新是普通次要按钮
		expect(htmlSrc).toMatch(/id="adm-balance-refresh-btn"[^>]*class=["'][^"']*adm-btn-secondary/);
	});

	// §5.3/§4.8 390px 列适配（CSS 断言）
	it("批次A-8: 次要列可隐藏、5 列表头（窄屏 4 列）、移动堆叠补行、日期不 break-all", () => {
		const usersPage = htmlSrc.slice(htmlSrc.indexOf('id="adm-page-users"'),
			htmlSrc.indexOf('id="adm-page-invites"'));
		expect(usersPage).toMatch(/<th[^>]*adm-col-secondary[^>]*>角色</);
		expect(usersPage).not.toMatch(/<th[^>]*>登录账号</);
		expect(usersPage).not.toMatch(/<th[^>]*>最近 AI 调用</);
		expect(usersPage).not.toMatch(/<th[^>]*adm-col-desktop/);
		expect(usersPage).toContain("<th>显示名</th>");
		expect(usersPage).toContain("<th>状态</th>");
		// wave 2：列名从「本月剩余」改为「额度剩余」（user 总额度/owner 月窗共用）
		expect(usersPage).toContain("<th>额度剩余</th>");
		expect(usersPage).not.toContain("<th>本月剩余</th>");
		expect(usersPage).toContain("<th>操作</th>");
		expect(cssSrc).toMatch(/@media \(max-width:\s*767px\)[\s\S]*\.adm-col-secondary\s*{[^}]*display:\s*none/);
		expect(cssSrc).toMatch(/@media \(max-width:\s*767px\)[\s\S]*\.adm-table--users \.adm-stack-mobile\s*{[^}]*display:\s*block/);
		expect(cssSrc).toMatch(/\.adm-stack-mobile\s*{[^}]*display:\s*none/);
		expect(cssSrc).toMatch(/\.adm-cell-time\s*{[^}]*word-break:\s*normal/);
		expect(cssSrc).toMatch(/\.adm-drawer-tech\s*{/);
		expect(cssSrc).toMatch(/\.adm-win-adjust\s*{/);
	});

	// P0-1 回归：390px 导航完整标签
	it("批次A-13: 移动端导航 ::before 按同特异性逐页复位", () => {
		const mobileBlock = cssSrc.slice(cssSrc.indexOf("@media (max-width: 767px)"));
		expect(mobileBlock).toContain("adm-nav-btn { font-size: 14px");
		for (const p of ["overview", "users", "invites", "settings",
			"billing", "plugins", "audit"]) {
			expect(mobileBlock,
				`mobile ::before reset for ${p}`).toMatch(
				new RegExp(`\\.adm-nav-btn\\[data-page="${p}"\\]::before[\\s\\S]{0,600}?content:\\s*none`));
		}
	});

	// §4.9 紧凑握手
	it("批次A-9: 健康握手缩为绿点+已连接+详情；作废时恢复完整文字", async () => {
		const bus = loadPluginUiWithBus();
		boot(bus);
		const hs = bus.els["adm-handshake-status"];
		expect(hs.textContent).toContain("已连接");
		expect(hs.textContent).toContain("protocolVersion=1.0.0");
		expect(bus.created.some((el) =>
			String(el.className).includes("adm-status-dot--ok"))).toBe(true);
		bus.dispatch(bus.parent, {
			kind: "event", bridge: "admin", type: "bridge_invalidated",
			reason: "reload", message: "宿主已作废桥接会话",
		});
		expect(hs.textContent).toContain("作废");
		expect(hs.textContent).not.toContain("已连接");
	});

	// §4.9 当前身份一行化 + §4.8 上海时间
	it("批次A-10: 概览身份收成一行；绝对时间为上海 GMT+8", async () => {
		expect(htmlSrc).not.toContain('id="adm-actor-card"');
		expect(htmlSrc).toContain('id="adm-actor-line"');
		const bus = loadPluginUiWithBus();
		boot(bus);
		bus.client!.showPage("overview");
		await ticks(4);
		replyMethod(bus, NONCE, "admin.auth.get", {
			ok: true,
			result: { role: "owner", loginIdMasked: "o***r@x.com", previewActive: false },
		});
		replyMethod(bus, NONCE, "admin.overview.get", {
			ok: true, result: { users: { total: 1, active: 1, disabled: 0, ai_access: 0 } },
		});
		replyMethod(bus, NONCE, "admin.siteStats.get", {
			ok: false, error: { code: "unknown_method", message: "" },
		});
		await ticks(6);
		const line = bus.els["adm-actor-line"].textContent;
		expect(line).toContain("owner");
		expect(line).toContain("o***r@x.com");
		expect(bus.client!.fmtTs(1700000000)).toBe("2023-11-15 06:13:20 GMT+8");
		expect(bus.client!.fmtTs("Tue, 01 Sep 2026 08:36:01 GMT"))
			.toBe("2026-09-01 16:36:01 GMT+8");
	});

	// §4.8 audit 摘要 + 原始详情不丢数据
	it("批次A-11: audit 已知 action 出人类摘要；原始 JSON 保留在折叠区", async () => {
		const bus = loadPluginUiWithBus();
		bus.dispatch(bus.parent, {
			kind: "init", bridge: "admin", protocolVersion: "1.0.0",
			nonce: NONCE, adminPermissions: ["admin:audit:read"],
		});
		bus.client!.showPage("audit");
		await ticks(4);
		replyMethod(bus, NONCE, "admin.audit.list", {
			ok: true,
			result: {
				items: [
					{ ts: 1700000000, actor_role: "owner", actor_user_id: "u0",
					  action: "spend.total_limit.set", target_type: "user",
					  target_id: "u1", detail: { from_limit_nano_cny: "20000000000",
					  	to_limit_nano_cny: "25000000000" } },
					{ ts: 1700000001, actor_role: "owner", actor_user_id: "u0",
					  action: "exotic.future_action", target_type: "x", target_id: "y",
					  detail: { unknown_field: "v1", another: 2 } },
				],
				next_cursor: null,
			},
		});
		await ticks(4);
		const tbody = bus.els["adm-audit-tbody"].textContent;
		expect(tbody).toContain("2023-11-15 06:13:20 GMT+8");
		expect(tbody).toContain("原始详情");
		expect(tbody).toContain("unknown_field");
		expect(tbody).toContain("v1");
		expect(tbody).toContain("原额度（nano）：20000000000；新额度（nano）：25000000000");
	});

	// §5.5 设置页窗口摘要卡：只回答额度/剩余；不拉 users.list
	it("批次A-12: 设置页当前窗口摘要卡只有 Demo/Owner 两张（两位小数）；不发 users.list", async () => {
		const bus = loadPluginUiWithBus();
		bus.dispatch(bus.parent, {
			kind: "init", bridge: "admin", protocolVersion: "1.0.0",
			nonce: NONCE, adminPermissions: ["admin:settings:read", "admin:users:read"],
		});
		bus.client!.showPage("settings");
		await ticks(4);
		replyMethod(bus, NONCE, "admin.settings.get", {
			ok: true,
			result: {
				registration: { mode: "closed", stored_mode: "closed",
					precondition_failures: [], supported_modes: ["closed", "invite_only"] },
				spend: {
					available: true, enforcement_mode: "shadow",
					user_default_total_limit_nano_cny: "20000000000",
					demo_weekly_limit_nano_cny: "50000000000",
					owner_monthly_limit_nano_cny: "1000000000000",
					policies: {},
					current_windows: {
						demo: { window_id: "w1", window_start: 1700000000,
							window_end: 1702588800, limit_nano_snapshot: "50000000000",
							spent_nano_cny: "21300000000", reserved_nano_cny: "2500000000",
							remaining_nano: "26200000000", version: 3 },
					},
				},
				runtime: { available: true, limits: { demo_enabled: true } },
			},
		});
		await ticks(6);
		const requested = bus.parentPosted
			.filter((p) => p.env.kind === "request")
			.map((p) => String(p.env.method));
		expect(requested).not.toContain("admin.users.list");
		const cards = bus.created.filter((el) =>
			String(el.className).includes("adm-summary-card"));
		expect(cards.length).toBe(2);
		const cardText = cards.map((el) => el.textContent).join("\n");
		expect(cardText).toContain("额度");
		expect(cardText).toContain("50.00 CNY");
		expect(cardText).toContain("剩余");
		expect(cardText).toContain("26.20 CNY");
		expect(cardText).not.toContain("已消费");
		expect(cardText).not.toContain("预占");
		expect(cardText).not.toContain("v3");
	});

	// §4.5 wave 2：运行时安全参数改名 + 自带 API 步数字段移除
	it("批次A-14: 「注册用户单任务安全上限」文案与 500 上限；ownsteps 字段不出现", () => {
		const rtStart = htmlSrc.indexOf('id="adm-rt-psteps"');
		expect(rtStart).toBeGreaterThan(-1);
		const rtBlockStart = htmlSrc.lastIndexOf("<section", rtStart);
		const rtBlockEnd = htmlSrc.indexOf("</section>", rtStart);
		const rtBlock = htmlSrc.slice(rtBlockStart, rtBlockEnd);
		expect(rtBlock).toContain("注册用户单任务安全上限");
		expect(rtBlock).toContain("默认/最高 500");
		expect(rtBlock).toContain("超过 100 记为异常长任务");
		expect(rtBlock).toContain("达到 500 暂停");
		expect(rtBlock).toContain("消费额度由总金额控制");
		// psteps 输入上限 500（HTML + JS 双闸）
		expect(rtBlock).toMatch(/id="adm-rt-psteps"[^>]*max="500"/);
		// 自带 API 步数上限从 UI 移除（后端字段兼容保留）
		expect(htmlSrc).not.toContain('id="adm-rt-ownsteps"');
		expect(src).not.toContain('"adm-rt-ownsteps"');
		expect(src).not.toMatch(/own_task_max_steps_limit[^\n]*\n[^\n]*adm-rt/);
	});
});

// --------------------------------------------------------------------------- //
// wave 2 抽屉金额动作（§4.3）：user=总额度（设置/恢复默认，CAS，绝不重置
// 已用）；owner / 切换前 user=既有 currentWindow.adjust；互斥不串。
// --------------------------------------------------------------------------- //
describe("wave 2 — 抽屉总额度动作（§4.3 / Batch B）", () => {
	const NONCE = "m5".repeat(32);

	function boot(bus: ReturnType<typeof loadPluginUiWithBus>) {
		bus.dispatch(bus.parent, {
			kind: "init", bridge: "admin", protocolVersion: "1.0.0",
			nonce: NONCE, adminPermissions: ["admin:users:read", "admin:users:write",
				"admin:settings:read"],
		});
		expect(bus.client!.handshakeState().ready).toBe(true);
	}

	const TOTAL_USER = {
		user_id: "u1", display_name: "张三", login_id_masked: "z***@x.com",
		role: "user", enabled: true, ai_access: true,
		spend: {
			total: {
				allowance_id: "alw_1", total_limit_nano_cny: "20000000000",
				spent_nano_cny: "3420000000", reserved_nano_cny: "500000000",
				remaining_nano: "16080000000", overage_nano: "0",
				source: "invite", version: 3, cutover_at: 1700000000,
				opening_spent_nano_cny: "3000000000",
			},
		},
	};

	const WINDOW_OWNER = {
		user_id: "u2", display_name: "李 owner", login_id_masked: "l***@x.com",
		role: "owner", enabled: true, ai_access: true,
		spend: {
			window: {
				window_id: "w1", window_start: 1700000000, window_end: 1702588800,
				limit_nano_snapshot: "1000000000000", spent_nano_cny: "1000000000",
				reserved_nano_cny: "0", remaining_nano: "999000000000",
				version: 6,
			},
		},
	};

	async function openDrawer(bus: ReturnType<typeof loadPluginUiWithBus>, user: unknown) {
		bus.client!.showPage("users");
		await ticks(4);
		replyMethod(bus, NONCE, "admin.users.list", {
			ok: true, result: { items: [user], next_cursor: null },
		});
		await ticks(4);
		const detailBtn = bus.created.find((el) => el.textContent === "详情" &&
			el._listeners && el._listeners.click);
		expect(detailBtn).toBeTruthy();
		detailBtn!._fire("click", {});
		expect(bus.els["adm-user-drawer"].hidden).toBe(false);
	}

	function drawerStatus(bus: ReturnType<typeof loadPluginUiWithBus>) {
		return bus.created.filter((el) =>
			String(el.className).includes("adm-status"))
			.map((el) => el.textContent).join("\n");
	}

	it("M-1: user（total 形态）主视图五要素；余额/caps 永不出现；技术细节含 allowance/raw", async () => {
		const bus = loadPluginUiWithBus();
		boot(bus);
		await openDrawer(bus, TOTAL_USER);
		const body = bus.els["adm-drawer-body"].textContent;
		// 主视图：总额度/累计已用/预占/可用金额/额度来源（两位小数）
		expect(body).toContain("总额度");
		expect(body).toContain("20.00 CNY");
		expect(body).toContain("累计已用");
		expect(body).toContain("3.42 CNY");
		expect(body).toContain("预占");
		expect(body).toContain("0.50 CNY");
		expect(body).toContain("可用金额");
		expect(body).toContain("剩余 16.08 CNY");
		expect(body).toContain("额度来源");
		expect(body).toContain("邀请初始额度");
		// 金额余额 / soft·hard caps / billing account 心智一律删除
		expect(body).not.toContain("金额余额");
		expect(body).not.toContain("soft");
		expect(body).not.toContain("hard");
		expect(body).not.toContain("caps");
		expect(body).not.toContain("balance");
		// 技术细节：allowance id/version、cutover、原始 nano
		const techIdx = body.indexOf("技术细节");
		expect(techIdx).toBeGreaterThan(-1);
		const tech = body.slice(techIdx);
		expect(tech).toContain("alw_1");
		expect(tech).toContain("2023-11-15 06:13:20 GMT+8");
		expect(tech).toContain("20000000000");
		expect(tech).toContain("opening_spent_nano_cny");
	}, 10000);

	it("M-2: owner（window 形态）显示月窗口四要素与窗口调整编辑器；不出现 total 动作", async () => {
		const bus = loadPluginUiWithBus();
		boot(bus);
		await openDrawer(bus, WINDOW_OWNER);
		const body = bus.els["adm-drawer-body"].textContent;
		expect(body).toContain("本月额度");
		expect(body).toContain("1000.00 CNY");
		expect(body).toContain("本月已用");
		expect(body).toContain("本月预占");
		expect(body).toContain("本月剩余");
		expect(body).toContain("剩余 999.00 CNY");
		// owner 不出现 total 动作与 total 词汇
		expect(body).not.toContain("设置总额度");
		expect(body).not.toContain("恢复默认");
		expect(body).not.toContain("allowance");
		// 窗口调整编辑器存在（currentWindow.adjust 的抽屉入口）
		const input = bus.created.find((el) => el.id === "adm-window-adjust-input");
		expect(input).toBeTruthy();
	}, 10000);

	it("M-3: 设置总额度走确认条 + 单次 CAS 桥调用（expected_version）；文案明示不重置已用", async () => {
		const bus = loadPluginUiWithBus();
		boot(bus);
		await openDrawer(bus, TOTAL_USER);
		const input = bus.created.find((el) => el.id === "adm-total-limit-input");
		expect(input).toBeTruthy();
		const label = bus.created.find((el) => el.htmlFor === "adm-total-limit-input");
		expect(String(label!.textContent)).toContain("总额度");
		input!.value = "2.5";
		const saveBtn = bus.created.find((el) => el.textContent === "设置总额度" &&
			el._listeners && el._listeners.click);
		expect(saveBtn).toBeTruthy();
		saveBtn!._fire("click", {});
		// 页内确认条（非 window.confirm）：明示绝对上限、不重置已用
		const box = bus.els["adm-drawer-confirm"];
		expect(box.hidden).toBe(false);
		expect(box.textContent).toContain("2.50 CNY（2500000000 nano）");
		expect(box.textContent).toContain("不清零、不重置");
		const okBtn = bus.created.filter((el) => el.textContent === "确认执行").at(-1);
		okBtn!._fire("click", {});
		await ticks(2);
		// 单次桥调用：userTotalLimit.set（PUT total-limit，CAS version=3）
		const req = bus.parentPosted
			.filter((p) => p.env.kind === "request" &&
				p.env.method === "admin.spend.userTotalLimit.set")
			.at(-1);
		expect(req).toBeTruthy();
		expect(req!.env.payload).toEqual({
			user_id: "u1", total_limit_nano_cny: "2500000000", expected_version: 3,
		});
		// 旧两步流（setSpendOverride + currentWindow.adjust）不得再发
		expect(bus.parentPosted.some((p) => p.env.kind === "request" &&
			p.env.method === "admin.users.setSpendOverride")).toBe(false);
		expect(bus.parentPosted.some((p) => p.env.kind === "request" &&
			p.env.method === "admin.spend.currentWindow.adjust")).toBe(false);
		replyMethod(bus, NONCE, "admin.spend.userTotalLimit.set", { ok: true, result: {} });
		await ticks(4);
		const status = drawerStatus(bus);
		expect(status).toContain("已设置总额度 2.50 CNY");
		expect(status).toContain("不重置已用金额");
		expect(box.hidden).toBe(true);
	}, 10000);

	it("M-4: 409 version_conflict → 如实提示并刷新，不假装成功", async () => {
		const bus = loadPluginUiWithBus();
		boot(bus);
		await openDrawer(bus, TOTAL_USER);
		const input = bus.created.find((el) => el.id === "adm-total-limit-input");
		input!.value = "2.5";
		const saveBtn = bus.created.find((el) => el.textContent === "设置总额度" &&
			el._listeners && el._listeners.click);
		saveBtn!._fire("click", {});
		const okBtn = bus.created.filter((el) => el.textContent === "确认执行").at(-1);
		okBtn!._fire("click", {});
		await ticks(2);
		replyMethod(bus, NONCE, "admin.spend.userTotalLimit.set", {
			ok: false, error: { code: "version_conflict", message: "stale" },
		});
		await ticks(4);
		const status = drawerStatus(bus);
		expect(status).toContain("409 version_conflict");
		expect(status).not.toContain("已设置总额度");
	}, 10000);

	it("M-5: 恢复默认读 spend 新键默认值 → restoreDefault CAS；已用保留语义可见", async () => {
		const bus = loadPluginUiWithBus();
		boot(bus);
		await openDrawer(bus, TOTAL_USER);
		const restoreBtn = bus.created.find((el) => el.textContent === "恢复默认" &&
			el._listeners && el._listeners.click);
		expect(restoreBtn).toBeTruthy();
		restoreBtn!._fire("click", {});
		await ticks(4);
		// 先读 settings 的 user_default_total_limit_nano_cny（新键）
		replyMethod(bus, NONCE, "admin.settings.get", {
			ok: true,
			result: { spend: { available: true,
				user_default_total_limit_nano_cny: "20000000000" } },
		});
		await ticks(4);
		expect(bus.els["adm-drawer-confirm"].textContent).toContain("恢复为全局默认 20.00 CNY");
		expect(bus.els["adm-drawer-confirm"].textContent).toContain("已用金额保留");
		const okBtn = bus.created.filter((el) => el.textContent === "确认执行").at(-1);
		okBtn!._fire("click", {});
		await ticks(2);
		const req = bus.parentPosted
			.filter((p) => p.env.kind === "request" &&
				p.env.method === "admin.spend.userTotalLimit.restoreDefault")
			.at(-1);
		expect(req!.env.payload).toEqual({ user_id: "u1", expected_version: 3 });
		replyMethod(bus, NONCE, "admin.spend.userTotalLimit.restoreDefault", {
			ok: true, result: {},
		});
		await ticks(4);
		expect(drawerStatus(bus)).toContain("已恢复默认总额度 20.00 CNY");
	}, 10000);

	it("M-6: settings 无新键默认值 → 不发 restoreDefault，明确告知未做修改", async () => {
		const bus = loadPluginUiWithBus();
		boot(bus);
		await openDrawer(bus, TOTAL_USER);
		const restoreBtn = bus.created.find((el) => el.textContent === "恢复默认" &&
			el._listeners && el._listeners.click);
		restoreBtn!._fire("click", {});
		await ticks(4);
		replyMethod(bus, NONCE, "admin.settings.get", {
			ok: true, result: { spend: { available: true } },
		});
		await ticks(4);
		const status = drawerStatus(bus);
		expect(status).toContain("未能读取全局默认总额度");
		expect(status).toContain("未做任何修改");
		const restoreReqs = bus.parentPosted
			.filter((p) => p.env.kind === "request" &&
				p.env.method === "admin.spend.userTotalLimit.restoreDefault");
		expect(restoreReqs).toHaveLength(0);
	}, 10000);

	it("M-6b: 建号带初始总额度发 total_limit_nano_cny（不再发 monthly 字段）", async () => {
		const bus = loadPluginUiWithBus();
		boot(bus);
		bus.doc.getElementById("adm-users-new-login")!.value = "new@pt.test";
		bus.doc.getElementById("adm-users-new-password")!.value = "longpass-123456789";
		bus.doc.getElementById("adm-users-new-limit")!.value = "3.5";
		bus.els["adm-users-create-btn"]._fire("click", {});
		await ticks(2);
		const req = bus.parentPosted
			.filter((p) => p.env.kind === "request" && p.env.method === "admin.users.create")
			.at(-1);
		expect(req!.env.payload).toEqual({
			login_id: "new@pt.test", password: "longpass-123456789",
			total_limit_nano_cny: "3500000000",
		});
	});
});

// --------------------------------------------------------------------------- //
// wave 2 费用页：Demo 消耗卡 + 三页内标签按需加载 + 迟到响应丢弃。
// --------------------------------------------------------------------------- //
describe("wave 2 — 费用页（Demo 统计 + 页内标签 §4.6）", () => {
	const NONCE = "m7".repeat(32);

	function boot(bus: ReturnType<typeof loadPluginUiWithBus>, perms: string[]) {
		bus.dispatch(bus.parent, {
			kind: "init", bridge: "admin", protocolVersion: "1.0.0",
			nonce: NONCE, adminPermissions: perms,
		});
	}

	const SETTINGS = {
		registration: { mode: "closed", stored_mode: "closed",
			precondition_failures: [], supported_modes: ["closed"] },
		spend: {
			available: true, enforcement_mode: "shadow", policies: {},
			user_default_total_limit_nano_cny: "20000000000",
			demo_weekly_limit_nano_cny: "50000000000",
			owner_monthly_limit_nano_cny: "1000000000000",
			current_windows: {
				demo: { window_id: "wd1", window_start: 1700000000,
					window_end: 1702588800, limit_nano_snapshot: "50000000000",
					spent_nano_cny: "0", reserved_nano_cny: "0",
					remaining_nano: "50000000000", version: 2 },
				owner: { window_id: "wo1", window_start: 1700000000,
					window_end: 1702588800, limit_nano_snapshot: "1000000000000",
					spent_nano_cny: "0", reserved_nano_cny: "0",
					remaining_nano: "1000000000000", version: 6 },
			},
		},
		runtime: { available: true, limits: {} },
	};

	const DEMO_STATS = {
		window: "current", window_id: "spw_d1", window_version: 2,
		virtual: false, window_start: 1700000000, window_end: 1700604800,
		policy_id: "spp_demo", policy_version: 4,
		limit_nano_cny: "50000000000", spent_nano_cny: "21300000000",
		reserved_nano_cny: "2500000000", remaining_nano_cny: "26200000000",
		overage_nano_cny: "0", priced_calls: 9, unpriced_calls: 0,
		charge_nano_cny: "21000000000", provider_cost_nano_cny: "19000000000",
		cache_hit_tokens: 1000, cache_miss_tokens: 500, output_tokens: 800,
		reasoning_tokens: 0,
		holds: { authorized: 1, open: 1, settled: 6, released: 1, expired: 0 },
		denials: [{ reason: "insufficient_remaining", count: 2 }],
		denials_total: 2, db_unavailable_denials_included: false,
	};

	it("M-7: Demo 立即调整当前周期：固定主体 + CAS 载荷；确认条含影响与新剩余", async () => {
		const bus = loadPluginUiWithBus();
		boot(bus, ["admin:settings:read", "admin:settings:write"]);
		bus.client!.showPage("settings");
		await ticks(4);
		replyMethod(bus, NONCE, "admin.settings.get", { ok: true, result: SETTINGS });
		await ticks(4);
		bus.doc.getElementById("adm-win-demo-limit")!.value = "52";
		bus.els["adm-win-demo-adjust-btn"]._fire("click", {});
		const box = bus.els["adm-win-demo-confirm"];
		expect(box.hidden).toBe(false);
		expect(box.textContent).toContain("Demo（全站共享周窗口）");
		expect(box.textContent).toContain("已消费 0.00 CNY / 预占 0.00 CNY 不回退");
		expect(box.textContent).toContain("新剩余 52.00 CNY");
		const okBtn = bus.created.filter((el) => el.textContent === "确认执行").at(-1);
		okBtn!._fire("click", {});
		await ticks(2);
		const req = bus.parentPosted
			.filter((p) => p.env.kind === "request" && p.env.method === "admin.spend.currentWindow.adjust")
			.at(-1);
		expect(req!.env.payload).toEqual({
			window_id: "wd1", limit_nano_snapshot: "52000000000", version: 2,
		});
		replyMethod(bus, NONCE, "admin.spend.currentWindow.adjust", {
			ok: true, result: { window: { version: 3 } },
		});
		await ticks(4);
		expect(bus.els["adm-win-demo-status"].textContent).toContain("已调整");
	}, 10000);

	it("M-8: Owner 立即调整走同一桥方法（window_id=wo1）", async () => {
		const bus = loadPluginUiWithBus();
		boot(bus, ["admin:settings:read", "admin:settings:write"]);
		bus.client!.showPage("settings");
		await ticks(4);
		replyMethod(bus, NONCE, "admin.settings.get", { ok: true, result: SETTINGS });
		await ticks(4);
		bus.doc.getElementById("adm-win-owner-limit")!.value = "1200";
		bus.els["adm-win-owner-adjust-btn"]._fire("click", {});
		const okBtn = bus.created.filter((el) => el.textContent === "确认执行").at(-1);
		okBtn!._fire("click", {});
		await ticks(2);
		const req = bus.parentPosted
			.filter((p) => p.env.kind === "request" && p.env.method === "admin.spend.currentWindow.adjust")
			.at(-1);
		expect(req!.env.payload).toEqual({
			window_id: "wo1", limit_nano_snapshot: "1200000000000", version: 6,
		});
	}, 10000);

	it("M-9: 进入费用页首屏 = overview+余额+Demo 统计 + 默认 usage 标签；其余标签激活才发请求", async () => {
		const bus = loadPluginUiWithBus();
		boot(bus, ["admin:overview:read", "admin:billing:read"]);
		bus.client!.showPage("billing");
		await ticks(4);
		const methods = () => bus.parentPosted
			.filter((p) => p.env.kind === "request")
			.map((p) => String(p.env.method));
		expect(methods()).toContain("admin.overview.get");
		expect(methods()).toContain("admin.billing.providerBalance.get");
		expect(methods()).toContain("admin.spend.demoStats.get");
		// 默认标签 = 模型调用（唯一当前标签发请求）
		expect(methods().filter((m) => m === "admin.billing.usage.list").length).toBe(1);
		expect(methods()).not.toContain("admin.billing.ledger.list");
		replyMethod(bus, NONCE, "admin.overview.get", {
			ok: true, result: { users: { total: 2 }, billing: {
				available: true, charge_nano_cny: "12500000000", unpriced_count: 0,
			} },
		});
		replyMethod(bus, NONCE, "admin.billing.providerBalance.get", {
			ok: true, result: { provider: "deepseek", snapshot: null },
		});
		replyMethod(bus, NONCE, "admin.spend.demoStats.get", {
			ok: true, result: DEMO_STATS,
		});
		replyMethod(bus, NONCE, "admin.billing.usage.list", {
			ok: true, result: { items: [], next_cursor: null },
		});
		await ticks(6);
		expect(bus.els["adm-state-billing"].getAttribute("data-page-state")).toBe("ready");
		// Demo 卡渲染（两位小数）+ hold/拒绝聚合
		const demo = bus.els["adm-demo-info"].textContent;
		expect(demo).toContain("21.30 CNY");
		expect(demo).toContain("26.20 CNY");
		expect(demo).toContain("insufficient_remaining");
		// KPI 行含四卡
		const kpis = bus.els["adm-bill-kpis"].textContent;
		expect(kpis).toContain("供应商余额");
		expect(kpis).toContain("User 累计已用");
		expect(kpis).toContain("Demo 本周已用");
		expect(kpis).toContain("未计价");
		// 切到账务流水 → 才发 ledger 请求
		bus.els["adm-tab-ledger"]._fire("click", {});
		await ticks(2);
		expect(methods()).toContain("admin.billing.ledger.list");
		replyMethod(bus, NONCE, "admin.billing.ledger.list", {
			ok: true, result: { items: [], next_cursor: null },
		});
		await ticks(4);
		// 切到计费异常 → 才发 unpriced 过滤请求
		bus.els["adm-tab-unpriced"]._fire("click", {});
		await ticks(2);
		const unpricedReq = bus.parentPosted
			.filter((p) => p.env.kind === "request" && p.env.method === "admin.billing.usage.list")
			.at(-1);
		expect((unpricedReq!.env.payload as Record<string, unknown>).status).toBe("unpriced");
		// aria-selected 状态（单选）
		expect(bus.els["adm-tab-unpriced"].getAttribute("aria-selected")).toBe("true");
		expect(bus.els["adm-tab-ledger"].getAttribute("aria-selected")).toBe("false");
		expect(bus.els["adm-tab-usage"].getAttribute("aria-selected")).toBe("false");
	}, 10000);

	it("M-9b: 切换标签后旧标签的迟到响应被丢弃，不覆盖当前视图", async () => {
		const bus = loadPluginUiWithBus();
		boot(bus, ["admin:billing:read", "admin:overview:read"]);
		bus.client!.showPage("billing");
		await ticks(4);
		// 捕获 usage 标签的 requestId（默认标签），但不立即回复
		const usageReq = bus.parentPosted
			.filter((p) => p.env.kind === "request" && p.env.method === "admin.billing.usage.list")
			.at(-1);
		expect(usageReq).toBeTruthy();
		// 切到账务流水（usage 请求仍在途）
		bus.els["adm-tab-ledger"]._fire("click", {});
		await ticks(2);
		// 迟到的 usage 响应（含大量行）到达——必须被代际丢弃
		bus.dispatch(bus.parent, {
			kind: "response", bridge: "admin", nonce: NONCE,
			requestId: usageReq!.env.requestId, ok: true,
			result: { items: [{ event_id: "stale-1", occurred_at: 1700000000,
				model: "m", status: "priced", charge_nano_cny: "1",
				cache_hit_input_tokens: 0, cache_miss_input_tokens: 0,
				output_tokens: 0 }], next_cursor: null },
		});
		await ticks(4);
		// usage 表仍为空（迟到响应没有写回）
		expect(bus.els["adm-usage-tbody"].textContent).not.toContain("stale-1");
		// 当前视图是 ledger 标签
		expect(bus.els["adm-ledger-section"].hidden).toBe(false);
		expect(bus.els["adm-usage-section"].hidden).toBe(true);
	}, 10000);

	it("M-10: unpriced=0 无红框告警；>0 时告警条出现且可跳转计费异常标签", async () => {
		const bus = loadPluginUiWithBus();
		boot(bus, ["admin:overview:read", "admin:billing:read"]);
		// unpriced=0：告警条保持隐藏
		bus.client!.showPage("billing");
		await ticks(4);
		replyMethod(bus, NONCE, "admin.overview.get", {
			ok: true, result: { users: { total: 1 }, billing: {
				available: true, charge_nano_cny: "0", unpriced_count: 0 } },
		});
		replyMethod(bus, NONCE, "admin.billing.providerBalance.get", {
			ok: true, result: { provider: "deepseek", snapshot: null },
		});
		replyMethod(bus, NONCE, "admin.spend.demoStats.get", {
			ok: false, error: { code: "not_implemented", message: "" },
		});
		replyMethod(bus, NONCE, "admin.billing.usage.list", {
			ok: true, result: { items: [], next_cursor: null },
		});
		await ticks(6);
		expect(bus.els["adm-bill-alert"].hidden).toBe(true);
		// Demo 统计不可用：卡内中性空态，不是异常色
		expect(bus.els["adm-demo-empty"].hidden).toBe(false);
		expect(bus.els["adm-demo-card"].className).not.toContain("adm-card--anomaly");
		// unpriced>0：告警条出现 + 跳转按钮
		bus.client!.showPage("billing");
		await ticks(4);
		replyMethod(bus, NONCE, "admin.overview.get", {
			ok: true, result: { users: { total: 1 }, billing: {
				available: true, charge_nano_cny: "0", unpriced_count: 4 } },
		});
		replyMethod(bus, NONCE, "admin.billing.providerBalance.get", {
			ok: true, result: { provider: "deepseek", snapshot: null },
		});
		replyMethod(bus, NONCE, "admin.spend.demoStats.get", {
			ok: true, result: { ...DEMO_STATS, unpriced_calls: 4 },
		});
		replyMethod(bus, NONCE, "admin.billing.usage.list", {
			ok: true, result: { items: [], next_cursor: null },
		});
		await ticks(6);
		expect(bus.els["adm-bill-alert"].hidden).toBe(false);
		expect(bus.els["adm-bill-alert-list"].textContent).toContain("4 条未计价事件");
		expect(bus.els["adm-bill-alert-goto"].hidden).toBe(false);
		// 跳转：切到计费异常标签并发起 unpriced 过滤请求
		bus.els["adm-bill-alert-goto"]._fire("click", {});
		await ticks(2);
		expect(bus.els["adm-tab-unpriced"].getAttribute("aria-selected")).toBe("true");
		const unpricedReq = bus.parentPosted
			.filter((p) => p.env.kind === "request" && p.env.method === "admin.billing.usage.list")
			.at(-1);
		expect((unpricedReq!.env.payload as Record<string, unknown>).status).toBe("unpriced");
		// Demo 卡 unpriced>0 → 异常色
		expect(bus.els["adm-demo-card"].className).toContain("adm-card--anomaly");
	}, 10000);

	it("M-11: 费用明细键盘可达（方向键切换标签）", async () => {
		const bus = loadPluginUiWithBus();
		boot(bus, ["admin:billing:read", "admin:overview:read"]);
		bus.client!.showPage("billing");
		await ticks(4);
		// ArrowRight：usage → ledger
		bus.els["adm-tab-usage"]._fire("keydown", { key: "ArrowRight", preventDefault() {} });
		expect(bus.els["adm-tab-ledger"].getAttribute("aria-selected")).toBe("true");
		// Home：回到 usage
		bus.els["adm-tab-ledger"]._fire("keydown", { key: "Home", preventDefault() {} });
		expect(bus.els["adm-tab-usage"].getAttribute("aria-selected")).toBe("true");
		// End：跳到 unpriced
		bus.els["adm-tab-usage"]._fire("keydown", { key: "End", preventDefault() {} });
		expect(bus.els["adm-tab-unpriced"].getAttribute("aria-selected")).toBe("true");
	});
});
