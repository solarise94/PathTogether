/**
 * pathtogether-admin 插件 UI 最小装配测试（PR5 修订 + v0.3 P2 修订）。
 *
 * 插件页运行在 /admin 宿主页的 opaque iframe 内，无 jsdom 环境；本文件沿用
 * admin-preview.test.ts 的「new Function + 假 window/document」模式，锁定：
 *   - main.js 在缺省 DOM 下加载不抛错（所有新页面/按钮绑定均为可选探测）；
 *   - 导出 PathTogetherAdminClient（request/showPage/handshakeState +
 *     金额换算 cnyToNano/nanoToCnyString/fmtNano，仅测试用）；
 *   - 初始页白名单含 plugins（PR5 新增页），未知 hash 回 overview；
 *   - §8.3 P2（对称认证）：onMessage 一律先验 event.source === window.parent；
 *     init 之后的响应必须带与 init 相同的 nonce 且命中在途 requestId，否则
 *     静默丢弃——其他 frame/窗口无法向插件伪造响应；
 *   - §5 v0.3 P2（金额十进制字符串）：cnyToNano 字符串进字符串出（>19 位
 *     拒绝）；nanoToCnyString/fmtNano 全 BigInt 精确换算，不经 Number。
 */
import { describe, expect, it } from "vitest";
import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const src = readFileSync(
	resolve(here, "../../plugins/pathtogether-admin/ui/main.js"), "utf8");
// 2026-09-01 UI 升级：HTML/CSS 源码级断言（结构/label/折叠/legacy/按钮语义）
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
	// 可交互假元素：属性字典 + 子节点文本累积（textContent 语义与 DOM 对齐：
	// 读取时拼子树，设置时清空子树）——包 E 的 KPI/抽屉/状态组件渲染需要。
	// 2026-09-01 升级补充：tagName/classList/事件录制/focus 录制，用于抽屉
	// 焦点管理、按钮语义类与握手紧凑态的单元断言（均为增量，不影响旧用例）。
	const attrs: Record<string, string> = {};
	const children: Array<{ textContent?: string }> = [];
	const listeners: Record<string, FakeListener[]> = {};
	let ownText = "";
	const el = {
		tagName: String(tag || "div").toUpperCase(),
		hidden: true,
		className: "",
		htmlFor: "",
		// 2026-09-01 P1 补充：动态创建的输入（重置密码）也断言 label[for]/
		// placeholder/minLength——主代码直接赋值这些普通属性，假元素需可写
		id: "",
		placeholder: "",
		autocomplete: "",
		minLength: 0,
		maxLength: 0,
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
	fmtNano: (v: unknown) => string;
	fmtCny: (v: unknown) => string;
	fmtTs: (epoch: unknown) => string;
	handshakeState: () => { ready: boolean; grantedCount: number };
}

// 可交互装配：捕获 message 监听器与 window.parent.postMessage，可模拟宿主
// init / 响应 / 伪造消息（P2 对称认证用例）。2026-09-01 升级补充：录制
// document 级监听器（抽屉 Esc/Tab 焦点管理）与 createElement 产物（按钮
// 语义类、raw values details、usage meter 等 JS 渲染节点断言）。
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

describe("pathtogether-admin plugin UI bootstrap (PR5)", () => {
	it("loads without throwing and exports the bridge client", () => {
		const { client } = loadPluginUi("");
		expect(client).toBeTruthy();
		expect(typeof client!.request).toBe("function");
		expect(typeof client!.showPage).toBe("function");
		expect(typeof client!.handshakeState).toBe("function");
		// 未握手：请求应拒绝（not_ready），showPage 切页不抛错（含 plugins/settings）
		expect(() => client!.showPage("plugins")).not.toThrow();
		expect(() => client!.showPage("overview")).not.toThrow();
		expect(() => client!.showPage("settings")).not.toThrow();
	});

	it("initial page whitelist includes the PR5 plugins page; unknown falls back", () => {
		// plugins 在白名单内：hash 透传后不再回概览（宿主深链 #plugins）
		const a = loadPluginUi("#plugins");
		expect(a.client).toBeTruthy();
		// 未知 slug：装配仍成功并回 overview（白名单校验在模块内部完成）
		const b = loadPluginUi("#no-such-page");
		expect(b.client).toBeTruthy();
	});

	it("批次 D：设置页在白名单内（#settings 深链；设置页元素在装配期被绑定）", () => {
		// settings 在白名单内（宿主深链 /admin#settings）
		const a = loadPluginUi("#settings");
		expect(a.client).toBeTruthy();
		// 设置页 section 装配期注册；bindNav 对设置页按钮/主体选择器做了
		// 可选探测绑定（缺省 DOM 下不抛错即装配完成）
		expect(a.els["adm-page-settings"]).toBeTruthy();
		expect(a.els["adm-regmode-save-btn"]).toBeTruthy();
		expect(a.els["adm-spend-save-btn"]).toBeTruthy();
		expect(a.els["adm-rt-save-btn"]).toBeTruthy();
		expect(a.els["adm-window-adjust-btn"]).toBeTruthy();
		expect(a.els["adm-window-subject"]).toBeTruthy();
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
		// nonce 不符（含旧 load 残留回包）：静默丢弃
		bus.dispatch(bus.parent, {
			kind: "response", bridge: "admin", nonce: "b".repeat(64),
			requestId: env.requestId, ok: true, result: { forged: true },
		});
		// 缺 nonce 字段：同样丢弃
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

describe("pathtogether-admin plugin UI — decimal-string amounts (§5 v0.3 P2)", () => {
	it("cnyToNano returns a decimal string; >19 nano digits or bad shapes reject", () => {
		const { client } = loadPluginUi("");
		// 字符串进、字符串出（不产生 Number 中转）
		expect(client!.cnyToNano("12.5")).toBe("12500000000");
		expect(client!.cnyToNano("0.000000001")).toBe("1");
		expect(client!.cnyToNano("-0.5")).toBe("-500000000");
		expect(client!.cnyToNano("0")).toBe("0");
		expect(client!.cnyToNano("0012")).toBe("12000000000"); // 前导零归一
		// 超过 wire 上限 19 位（10^10 CNY 以上）→ null
		expect(client!.cnyToNano("10000000000")).toBeNull();
		// 形态非法 → null
		expect(client!.cnyToNano("1.0000000001")).toBeNull();
		expect(client!.cnyToNano("abc")).toBeNull();
		expect(client!.cnyToNano("")).toBeNull();
	});

	it("nanoToCnyString converts precisely via BigInt (beyond 2^53 intact)", () => {
		const { client } = loadPluginUi("");
		expect(client!.nanoToCnyString("12345678901")).toBe("12.345678901");
		expect(client!.nanoToCnyString("-500000000")).toBe("-0.5");
		expect(client!.nanoToCnyString("0")).toBe("0");
		expect(client!.nanoToCnyString("1000000000")).toBe("1");
		expect(client!.nanoToCnyString(null)).toBe("");
		// 2^53 之外仍精确（Number 路径会失真）
		expect(client!.nanoToCnyString("9007199254740993")).toBe("9007199.254740993");
	});

	it("fmtNano renders exact CNY + raw nano without Number precision loss", () => {
		const { client } = loadPluginUi("");
		expect(client!.fmtNano("12500000000")).toBe("12.5 CNY（12500000000 nano）");
		expect(client!.fmtNano("-500000000")).toBe("-0.5 CNY（-500000000 nano）");
		expect(client!.fmtNano(null)).toBe("—");
		// >2^53 的 nano 值精确显示（旧实现 Number 除法会失真）
		expect(client!.fmtNano("9007199254740993"))
			.toBe("9007199.254740993 CNY（9007199254740993 nano）");
	});
});

// --------------------------------------------------------------------------- //
// 一次性修复包 A/D 回归（docs/admin-workbench-ci-one-shot-remediation-plan.md
// §8.2/§9.2）——在旧实现上必须失败：
//   - 未握手时切页：页面应呈现「等待桥接」的可见等待态，而不是把 not_ready
//     渲染成全局错误（旧实现 request reject → showError → errorCard 展开）；
//   - 来源零数据：固定空状态文案（历史用户未回填的说明），而不是空白表格
//     （旧实现空 items 渲染空 tbody，无任何解释）。
// --------------------------------------------------------------------------- //
describe("pathtogether-admin plugin UI — not-ready pages wait instead of erroring (§8.2)", () => {
	it("switching pages before handshake shows a waiting state, not the global error card", async () => {
		const bus = loadPluginUiWithBus();
		expect(bus.client!.handshakeState().ready).toBe(false);
		bus.client!.showPage("overview");
		await ticks(4);
		// 未 ready：不得把 not_ready 渲染成全局错误
		expect(bus.els["adm-error-card"].hidden).toBe(true);
		// 且不得发出任何管理 API 请求（骨架等待，§8.2「未 ready 不发请求」）
		expect(bus.parentPosted.filter((p) => p.env.kind === "request")).toHaveLength(0);
	});
});

describe("pathtogether-admin plugin UI — acquisition aggregated page state (§9.2 + 复核 P2)", () => {
	const NONCE = "c".repeat(64);

	function bootAndWait(bus: ReturnType<typeof loadPluginUiWithBus>) {
		bus.dispatch(bus.parent, {
			kind: "init", bridge: "admin", protocolVersion: "1.0.0",
			nonce: NONCE, adminPermissions: ["admin:acquisition:read", "admin:invites:read"],
		});
	}

	/** 回复 invites 页首屏的三组请求（summary / acquisition.list / invites.list）。 */
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

	it("zero data everywhere → terminal empty state, long copy exactly once (page-level)", async () => {
		const bus = loadPluginUiWithBus();
		bootAndWait(bus);
		bus.client!.showPage("invites");
		await ticks(4);
		replyAll(bus, () => ({
			ok: true,
			result: { registration_mode: "closed", items: [], invites: [],
				totals: { visits: 0, registrations: 0, first_ai_count: 0 } },
		}));
		await ticks(6);
		// 终态：empty（不是永久 loading）
		const st = bus.els["adm-state-invites"];
		expect(st.getAttribute("data-page-state")).toBe("empty");
		const pageMsg = st.textContent;
		expect(pageMsg).toContain("暂无来源归因数据");
		expect(pageMsg).toContain("历史用户尚未回填");
		// 完整长文案只在页级状态条出现一次（子区块是简短提示）
		const all = Object.values(bus.els).map((el) => el.textContent).join("\n");
		expect(all.split("历史用户尚未回填").length - 1).toBe(1);
		expect(bus.els["adm-acq-empty"].textContent).toContain("本周期暂无来源访问记录");
	});

	it("any data in one group → ready（页面必须离开 loading）", async () => {
		const bus = loadPluginUiWithBus();
		bootAndWait(bus);
		bus.client!.showPage("invites");
		await ticks(4);
		replyAll(bus, (method) => {
			if (method === "admin.invites.list") {
				return { ok: true, result: { invites: [{ invite_id: "iv1" }], next_cursor: null } };
			}
			return { ok: true, result: { registration_mode: "closed", items: [], totals: {} } };
		});
		await ticks(6);
		expect(bus.els["adm-state-invites"].getAttribute("data-page-state")).toBe("ready");
	});

	it("all requests fail → terminal error with retry, never rendered as empty", async () => {
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
		expect(st.textContent).not.toContain("暂无来源归因数据");
	});

	it("partial failure with data elsewhere → ready with partial note", async () => {
		const bus = loadPluginUiWithBus();
		bootAndWait(bus);
		bus.client!.showPage("invites");
		await ticks(4);
		replyAll(bus, (method) => {
			if (method === "admin.acquisition.summary") {
				return { ok: false, error: { code: "bridge_timeout", message: "" } };
			}
			if (method === "admin.invites.list") {
				return { ok: true, result: { invites: [{ invite_id: "iv1" }], next_cursor: null } };
			}
			return { ok: true, result: { items: [], next_cursor: null } };
		});
		await ticks(6);
		const st = bus.els["adm-state-invites"];
		expect(st.getAttribute("data-page-state")).toBe("ready");
		expect(st.textContent).toContain("部分数据加载失败");
	});
});

// --------------------------------------------------------------------------- //
// 包 E 锁定（§9）：KPI 卡渲染、用户表精简列 + 详情抽屉、页级四态组件。
// 这些是对新实现的契约锁定（区别于上方包 A/D 的先红后绿复现用例）。
// --------------------------------------------------------------------------- //
describe("pathtogether-admin plugin UI — workbench KPI + drawer (§9, 包 E)", () => {
	const NONCE = "d".repeat(64);

	function bootWithOverview(bus: ReturnType<typeof loadPluginUiWithBus>) {
		bus.dispatch(bus.parent, {
			kind: "init", bridge: "admin", protocolVersion: "1.0.0",
			nonce: NONCE, adminPermissions: ["admin:overview:read", "admin:users:read"],
		});
	}

	it("overview renders KPI cards with double-quota semantics", async () => {
		const bus = loadPluginUiWithBus();
		bootWithOverview(bus);
		bus.client!.showPage("overview");
		await ticks(4);
		const req = bus.parentPosted
			.filter((p) => p.env.kind === "request" && p.env.method === "admin.overview.get")
			.at(-1);
		expect(req).toBeTruthy();
		bus.dispatch(bus.parent, {
			kind: "response", bridge: "admin", nonce: NONCE,
			requestId: req!.env.requestId, ok: true,
			result: {
				users: { total: 9, active: 8, disabled: 1, ai_access: 5 },
				billing: {
					available: true, model_calls_period: 42, model_calls_today: 3,
					cache_hit_ratio: 0.5, cache_hit_input_tokens: 100,
					cache_miss_input_tokens: 100, charge_nano_cny: "12500000000",
					unpriced_count: 0,
				},
				turn_budget: {
					available: true, period_id: 1, legacy: true,
					note: "turn 消费闸已于批次 F 退役，以下为冻结历史",
				},
			},
		});
		await ticks(4);
		const texts = Object.values(bus.els).map((el) => el.textContent).join("\n");
		// KPI：用户/活跃、模型调用、缓存命中、模拟扣费、unpriced
		expect(texts).toContain("用户总数");
		expect(texts).toContain("模型调用（本周期）");
		expect(texts).toContain("缓存命中率");
		expect(texts).toContain("PR6 模拟软扣费口径");
		expect(texts).toContain("unpriced 事件（本周期）");
		// 批次 F：overview 的 turn 卡带 legacy 徽标（静态标记，读源断言）
		const { readFileSync } = await import("node:fs");
		const { dirname, resolve } = await import("node:path");
		const { fileURLToPath } = await import("node:url");
		const pluginHtml = readFileSync(
			resolve(dirname(fileURLToPath(import.meta.url)),
				"../../plugins/pathtogether-admin/ui/index.html"), "utf8");
		expect(pluginHtml).toContain('id="adm-ov-turn-legacy"');
		expect(pluginHtml).toContain("已退役 · 冻结历史");
		// 页级状态进入 ready
		const st = bus.els["adm-state-overview"];
		expect(st.getAttribute("data-page-state")).toBe("ready");
	});

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
					// 批次 F：turn_used/turn_limit 字段已随 turn 消费闸退役删除
					billing: { balance_nano: "100", soft_spend_cap_nano: null, hard_spend_cap_nano: null },
					last_ai_call_at: 1700000100,
				}],
				next_cursor: null,
			},
		});
		await ticks(4);
		const texts = Object.values(bus.els).map((el) => el.textContent).join("\n");
		// 高频列在表内
		expect(texts).toContain("张三");
		expect(texts).toContain("z***@x.com");
		// 低频字段不进表格行（余额/caps/注册方式只在抽屉里出现）
		// —— 直接验证抽屉可打开且包含完整字段
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
// 2026-09-01 管理工作台 UI 升级回归（review-2026-09-01-admin-ui.md §4/§6 批次 A）。
// 先于实现编写（改动前必须失败）：金额主视图 CNY-only、用户行用量边界、
// 折叠创建表单、持久 label、抽屉焦点管理、legacy 降级、危险按钮语义、
// 390px 列适配与紧凑握手。断言语义/DOM/终态，截图不作为断言。
// --------------------------------------------------------------------------- //

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

describe("UI 升级 2026-09-01 — 批次A 锁定（review spec §4）", () => {
	const NONCE = "a9".repeat(32);

	function boot(bus: ReturnType<typeof loadPluginUiWithBus>) {
		bus.dispatch(bus.parent, {
			kind: "init", bridge: "admin", protocolVersion: "1.0.0",
			nonce: NONCE, adminPermissions: ["admin:overview:read", "admin:users:read"],
		});
		expect(bus.client!.handshakeState().ready).toBe(true);
	}

	// §4.2 金额显示：主视图 CNY-only，raw nano 在 adm-raw-values 可展开区
	it("批次A-1: fmtCny 仅输出精确 x CNY；概览主视图无 nano 长串且 raw 可展开", async () => {
		const { client } = loadPluginUi("");
		expect(client!.fmtCny("12500000000")).toBe("12.5 CNY");
		expect(client!.fmtCny("-500000000")).toBe("-0.5 CNY");
		expect(client!.fmtCny("0")).toBe("0 CNY");
		expect(client!.fmtCny(null)).toBe("—");
		// >2^53 不经 Number 仍精确
		expect(client!.fmtCny("9007199254740993")).toBe("9007199.254740993 CNY");
		// 非法值：显示原值，不吞错、不伪造 0
		expect(client!.fmtCny("not-a-number")).toBe("not-a-number");

		const bus = loadPluginUiWithBus();
		boot(bus);
		bus.client!.showPage("overview");
		await ticks(4);
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
		await ticks(4);
		const rawBoxes = bus.created.filter((el) =>
			String(el.className).includes("adm-raw-values"));
		expect(rawBoxes.length).toBeGreaterThan(0);
		expect(rawBoxes.map((el) => el.textContent).join("\n")).toContain("12500000000");
		// 主视图不得拼接 nano 长串（§4.2）：剔除 raw 展开区后应无 nano 字样，
		// 且原始十进制串只出现在 raw 展开区内
		let texts = Object.values(bus.els).map((el) => el.textContent).join("\n");
		for (const box of rawBoxes) {
			texts = texts.split(box.textContent).join("");
		}
		expect(texts).toContain("12.5 CNY");
		expect(texts).not.toContain("nano");
		expect(texts).not.toContain("12500000000");
	});

	// §4.3 用户行用量：spent/reserved/remaining、0、overage、不可用
	it("批次A-2: 用户行展示已消费/预占/剩余；额度 0、超支、不可用边界正确", async () => {
		const bus = loadPluginUiWithBus();
		boot(bus);
		bus.client!.showPage("users");
		await ticks(4);
		replyMethod(bus, NONCE, "admin.users.list", {
			ok: true,
			result: {
				items: [
					{
						user_id: "u1", display_name: "正常户", login_id_masked: "a***1@x.com",
						role: "user", enabled: true, ai_access: true,
						spend: { window: {
							window_id: "w1", window_start: 1700000000, window_end: 1702588800,
							limit_nano_snapshot: "20000000000", spent_nano_cny: "3420000000",
							reserved_nano_cny: "500000000", remaining_nano: "16080000000",
							version: 1 } },
					},
					{
						user_id: "u2", display_name: "零额度", login_id_masked: "b***2@x.com",
						role: "user", enabled: true, ai_access: true,
						spend: { window: {
							window_id: "w2", window_start: 1700000000, window_end: 1702588800,
							limit_nano_snapshot: "0", spent_nano_cny: "0",
							reserved_nano_cny: "0", remaining_nano: "0", version: 1 } },
					},
					{
						user_id: "u3", display_name: "超支柱", login_id_masked: "c***3@x.com",
						role: "user", enabled: true, ai_access: true,
						spend: { window: {
							window_id: "w3", window_start: 1700000000, window_end: 1702588800,
							limit_nano_snapshot: "20000000000", spent_nano_cny: "22500000000",
							reserved_nano_cny: "0", remaining_nano: "-2500000000", version: 1 } },
					},
					{
						user_id: "u4", display_name: "无窗口", login_id_masked: "d***4@x.com",
						role: "user", enabled: true, ai_access: true, spend: null,
					},
					{
						user_id: "u5", display_name: "错误窗", login_id_masked: "e***5@x.com",
						role: "user", enabled: true, ai_access: true,
						spend: { error: "pg_backend_required" },
					},
					{
						// P1-6：remaining_nano 缺失 → 「不可用（remaining 缺失）」，
						// 绝不伪造为 0 或空白
						user_id: "u6", display_name: "缺剩余", login_id_masked: "f***6@x.com",
						role: "user", enabled: true, ai_access: true,
						spend: { window: {
							window_id: "w6", window_start: 1700000000, window_end: 1702588800,
							limit_nano_snapshot: "20000000000", spent_nano_cny: "1000000000",
							reserved_nano_cny: "0", remaining_nano: null, version: 1 } },
					},
				],
				next_cursor: null,
			},
		});
		await ticks(4);
		const tbody = bus.els["adm-users-tbody"].textContent;
		expect(tbody).toContain("已消费 3.42 CNY");
		expect(tbody).toContain("预占 0.5 CNY");
		expect(tbody).toContain("剩余 16.08 CNY");
		expect(tbody).toContain("额度为 0");
		expect(tbody).toContain("超支 2.5 CNY");
		// 不可用：非敏感错误摘要；绝不伪造 0 或空白
		expect(tbody).toContain("不可用");
		expect(tbody).toContain("pg_backend_required");
		// remaining_nano = null 的窗口：显式「不可用（remaining 缺失）」
		expect(tbody).toContain("不可用（remaining 缺失）");
		// 移动端堆叠补行（P0-2）：正常户的用量格内堆叠剩余语义、状态格内堆叠 AI
		const stacks = bus.created.filter((el) =>
			String(el.className).includes("adm-stack-mobile"));
		const stackTexts = stacks.map((el) => el.textContent).join("\n");
		expect(stackTexts).toContain("剩余 16.08 CNY");
		expect(stackTexts).toContain("AI");
		expect(stacks.every((el) => String(el.className).includes("adm-stack-mobile"))).toBe(true);
		const rows = tbody.split("无窗口")[1] || "";
		void rows;
		// 堆叠迷你条：spent 主段 + reserved 警示段（宽度桶类，CSP 无内联样式）
		const meters = bus.created.filter((el) =>
			String(el.className).includes("adm-usage-meter"));
		expect(meters.length).toBeGreaterThanOrEqual(2);
		const allSegs = bus.created.filter((el) =>
			String(el.className).includes("adm-usage-meter-spent"));
		expect(allSegs.length).toBeGreaterThanOrEqual(2);
		// 正常户：spent 17.1% → 桶 w15；overage 行：条宽 clamp 到 100%
		expect(String(allSegs[0].className)).toContain("adm-usage-w15");
		const overageSegs = bus.created.filter((el) =>
			String(el.className).includes("adm-usage-w100"));
		expect(overageSegs.length).toBeGreaterThanOrEqual(1);
	});

	// §4.4 折叠创建表单
	it("批次A-3: 创建用户/邀请默认折叠为入口，token 区在展开区内", () => {
		// users 创建卡
		const usersBox = htmlSrc.indexOf('id="adm-users-create-box"');
		expect(usersBox).toBeGreaterThan(-1);
		const usersTag = htmlSrc.slice(htmlSrc.lastIndexOf("<details", usersBox),
			htmlSrc.indexOf(">", usersBox) + 1);
		expect(usersTag).not.toMatch(/\sopen[\s>]/); // 默认折叠
		const usersEnd = htmlSrc.indexOf("</details>", usersBox);
		const usersBlock = htmlSrc.slice(usersBox, usersEnd);
		expect(usersBlock).toContain("新建用户");
		expect(usersBlock).toContain('id="adm-users-create-form"');
		expect(usersBlock).toContain('id="adm-users-create-btn"');
		// invites 创建卡（token 一次性展示区同在 details 内，创建后不被自动折叠）
		const invBox = htmlSrc.indexOf('id="adm-invite-create-box"');
		expect(invBox).toBeGreaterThan(-1);
		const invTag = htmlSrc.slice(htmlSrc.lastIndexOf("<details", invBox),
			htmlSrc.indexOf(">", invBox) + 1);
		expect(invTag).not.toMatch(/\sopen[\s>]/);
		const invEnd = htmlSrc.indexOf("</details>", invBox);
		const invBlock = htmlSrc.slice(invBox, invEnd);
		expect(invBlock).toContain("新建邀请");
		expect(invBlock).toContain('id="adm-invite-create-form"');
		expect(invBlock).toContain('id="adm-invite-create-btn"');
		expect(invBlock).toContain('id="adm-invite-token-box"');
	});

	// §4.1 持久 label
	it("批次A-4: 每个关键 input/select 都有真实 <label for>（全量扫描，不止白名单）", () => {
		const ids = [
			// 创建用户
			"adm-users-new-login", "adm-users-new-display", "adm-users-new-password",
			"adm-users-new-limit",
			// 用户筛选
			"adm-users-q", "adm-users-enabled", "adm-users-ai",
			// 创建邀请
			"adm-invite-login", "adm-invite-ttl", "adm-invite-limit",
			"adm-invite-cohort", "adm-invite-note", "adm-invite-source",
			"adm-invite-campaign",
			// 设置：注册模式 / 金额策略 / enforcement / 运行时 / 调整窗口
			"adm-regmode-select", "adm-spend-demo-week", "adm-spend-user-month",
			"adm-spend-owner-month", "adm-spend-mode",
			"adm-rt-psteps", "adm-rt-demosteps", "adm-rt-ownsteps", "adm-rt-concurrency",
			"adm-window-subject", "adm-window-newlimit",
			// 额度与账单：账户 / caps / 人工调整
			"adm-acct-user", "adm-caps-soft", "adm-caps-hard",
			"adm-adjust-kind", "adm-adjust-amount", "adm-adjust-reason",
			// 用量明细筛选 / 审计筛选
			"adm-usage-model", "adm-usage-user", "adm-usage-status",
			"adm-audit-action",
		];
		const missing = ids.filter((id) =>
			!new RegExp(`<label[^>]*for=["']${id}["']`).test(htmlSrc));
		expect(missing).toEqual([]);
		// 全量扫描（P1：不能只有硬编码白名单）——index.html 里每个非复选框
		// input/select 都必须有 <label for> 指向它
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
		// label 有独立组件类（≥12px 由 CSS 断言锁定）
		expect(htmlSrc).toMatch(/class=["'][^"']*adm-field-label[^"']*["'][^>]*for=["']adm-spend-demo-week["']/);
		expect(cssSrc).toMatch(/\.adm-field-label\s*{[^}]*font-size:\s*1[2-9]px/);
	});

	// §4.10 抽屉语义与焦点管理
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
					spend: { window: {
						window_id: "w1", window_start: 1700000000, window_end: 1702588800,
						limit_nano_snapshot: "20000000000", spent_nano_cny: "3420000000",
						reserved_nano_cny: "0", remaining_nano: "16580000000", version: 1 } },
				}],
				next_cursor: null,
			},
		});
		await ticks(4);
		// 表格行的详情按钮（td 容器）触发抽屉
		const detailBtn = bus.created.find((el) => el.textContent === "详情" &&
			el._listeners && el._listeners.click);
		expect(detailBtn).toBeTruthy();
		detailBtn!._fire("click", {});
		expect(bus.els["adm-user-drawer"].hidden).toBe(false);
		// 焦点进入关闭按钮
		expect(bus.els["adm-drawer-close"]._focusCalls).toBeGreaterThanOrEqual(1);
		// renderUserActions 返回真实 div（不再返回 td）
		const actionsWrap = bus.created.find((el) =>
			el.className === "adm-actions" && el.tagName === "DIV");
		expect(actionsWrap).toBeTruthy();
		expect(bus.created.some((el) => el.tagName === "TD" &&
			el.className === "adm-actions")).toBe(false);
		// 覆盖编辑器复用统一 field/label：存在 label[for] 与对应 id 的输入
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

	// P0-3 回归：抽屉发起的危险操作确认必须挂抽屉内的 #adm-drawer-confirm
	// （旧实现挂页级 #adm-users-confirm——在遮罩之后、不在 Tab 圈定内）
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
		// 页级确认条不再被抽屉操作使用
		const disableBtn = bus.created.find((el) => el.textContent === "禁用" &&
			el._listeners && el._listeners.click);
		expect(disableBtn).toBeTruthy();
		disableBtn!._fire("click", {});
		const box = bus.els["adm-drawer-confirm"];
		expect(box.hidden).toBe(false);
		expect(box.textContent).toContain("确认禁用用户 张三");
		// 确认执行：实心 danger、出现即聚焦（Tab 圈定内的可焦点入口）
		const okBtn = bus.created.filter((el) => el.textContent === "确认执行").at(-1);
		expect(okBtn).toBeTruthy();
		expect(okBtn!.className).toBe("adm-btn-danger");
		expect(okBtn!._focusCalls).toBeGreaterThanOrEqual(1);
		// 确认执行 → setUserEnabled 走桥（行为真正接通，不只是可见）
		okBtn!._fire("click", {});
		await ticks(2);
		const req = bus.parentPosted.filter((p) => p.env.kind === "request").at(-1);
		expect(req?.env.method).toBe("admin.users.setEnabled");
		// Esc 关闭抽屉 → 抽屉确认条清空复位
		bus.fireDocument("keydown", { key: "Escape" });
		expect(bus.els["adm-user-drawer"].hidden).toBe(true);
		expect(box.hidden).toBe(true);
		expect(box.textContent).toBe("");
		// 身份预览同样指向抽屉确认条
		detailBtn!._fire("click", {});
		const previewBtn = bus.created.find((el) => el.textContent === "身份预览" &&
			el._listeners && el._listeners.click);
		previewBtn!._fire("click", {});
		expect(bus.els["adm-drawer-confirm"].hidden).toBe(false);
		expect(bus.els["adm-drawer-confirm"].textContent).toContain("身份进入只读预览");
	});

	// P1：重置密码输入必须有真实 <label for>（动态创建节点也要扫；
	// placeholder 只放格式示例，不承担字段名；minLength 15 保持）
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
		// label 承担字段名（含目标用户），placeholder 只是格式示例
		expect(label!.textContent).toContain("张三");
		expect(label!.textContent).toContain("15 位");
		expect(String(input!.placeholder)).not.toContain("密码");
		// 确认重置是该确认条里的实心 danger 提交点
		const okBtn = bus.created.filter((el) => el.textContent === "确认重置").at(-1);
		expect(okBtn!.className).toBe("adm-btn-danger");
	});

	// §4.6 legacy 降级
	it("批次A-6: 账单页金额账户在前；turn legacy 卡中性、折叠、位于页尾", () => {
		const billingStart = htmlSrc.indexOf('id="adm-page-billing"');
		const billingEnd = htmlSrc.indexOf('id="adm-page-plugins"');
		const billing = htmlSrc.slice(billingStart, billingEnd);
		// 当前金额账户卡在账单页第一个卡片位置（在 legacy 卡之前）
		const acctIdx = billing.indexOf('id="adm-acct-card"');
		const turnIdx = billing.indexOf('id="adm-turn-legacy-card"');
		expect(acctIdx).toBeGreaterThan(-1);
		expect(turnIdx).toBeGreaterThan(acctIdx);
		// turn legacy 卡：details 默认折叠 + 中性 legacy 样式（非 error 红卡）
		const turnTag = billing.slice(billing.lastIndexOf("<details", turnIdx),
			billing.indexOf(">", turnIdx) + 1);
		expect(turnTag).toContain('id="adm-turn-legacy-card"');
		expect(turnTag).toContain("adm-legacy-card");
		expect(turnTag).not.toContain("adm-error-card");
		expect(turnTag).not.toMatch(/\sopen[\s>]/);
		// 文案仍是冻结历史、只读、不参与当前硬额度
		expect(billing).toContain("冻结历史");
		// 概览：turn 卡在金额与调用信息之后并默认折叠
		const ovStart = htmlSrc.indexOf('id="adm-page-overview"');
		const ovEnd = htmlSrc.indexOf('id="adm-page-users"');
		const ov = htmlSrc.slice(ovStart, ovEnd);
		const billingIdx = ov.indexOf('id="adm-ov-billing"');
		const usageIdx = ov.indexOf('id="adm-ov-usage"');
		const ovTurnIdx = ov.indexOf('id="adm-ov-turn-box"');
		expect(ovTurnIdx).toBeGreaterThan(-1);
		expect(ovTurnIdx).toBeGreaterThan(billingIdx);
		expect(ovTurnIdx).toBeGreaterThan(usageIdx);
		const ovTurnTag = ov.slice(ov.lastIndexOf("<details", ovTurnIdx),
			ov.indexOf(">", ovTurnIdx) + 1);
		expect(ovTurnTag).not.toMatch(/\sopen[\s>]/);
		expect(ovTurnTag).toContain("adm-legacy-card");
	});

	// §4.5 危险按钮语义
	it("批次A-7: 危险/普通按钮语义类正确（rotate=outline、每区一个实心 danger）", () => {
		// 四种语义类在 CSS 中定义，且有统一的 :focus-visible / disabled / hover
		expect(cssSrc).toMatch(/\.adm-btn-primary\s*{/);
		expect(cssSrc).toMatch(/\.adm-btn-secondary\s*{/);
		expect(cssSrc).toMatch(/\.adm-btn-danger\s*{/);
		expect(cssSrc).toMatch(/\.adm-btn-danger-outline\s*{/);
		expect(cssSrc).toMatch(/\.adm-btn[^{]*:focus-visible/);
		// 账单页两个既有实心 danger 保留（窗口调整/人工调整提交）
		expect(htmlSrc).toMatch(/id="adm-window-adjust-btn"[^>]*class=["'][^"']*adm-btn-danger/);
		expect(htmlSrc).toMatch(/id="adm-adjust-btn"[^>]*class=["'][^"']*adm-btn-danger/);
		// 轮换凭证：次要危险 → danger-outline（不再中性化）
		expect(src).toMatch(/actionBtn\("轮换凭证"[\s\S]{0,600}?"danger-outline"/);
		// 供应商余额刷新等普通操作不再是品牌蓝实心
		expect(htmlSrc).toMatch(/id="adm-balance-refresh-btn"[^>]*class=["'][^"']*adm-btn-secondary/);
	});

	it("批次A-7b: 人工调整区先查主体后启用；user_id 变更即失效再禁用", async () => {
		const bus = loadPluginUiWithBus();
		bus.dispatch(bus.parent, {
			kind: "init", bridge: "admin", protocolVersion: "1.0.0",
			nonce: NONCE, adminPermissions: ["admin:billing:read", "admin:billing:write"],
		});
		bus.client!.showPage("billing");
		await ticks(4);
		// 未查询：调整区禁用
		expect(bus.els["adm-adjust-btn"].disabled).toBe(true);
		// owner 输入 user_id 并点击「查询账户」
		bus.els["adm-acct-user"].value = "usr_target";
		bus.els["adm-acct-load-btn"]._fire("click", {});
		await ticks(4);
		// 回应账户查询（account:null —— 尚未开户也允许 grant/topup 自动开户）
		replyMethod(bus, NONCE, "admin.billing.account.get", {
			ok: true, result: { account: null, balance_nano: null },
		});
		await ticks(4);
		expect(bus.els["adm-adjust-btn"].disabled).toBe(false);
		// user_id 输入变化：旧查询失效 → 再次禁用
		bus.els["adm-acct-user"].value = "someone-else";
		bus.els["adm-acct-user"]._fire("input", {});
		expect(bus.els["adm-adjust-btn"].disabled).toBe(true);
	});

	// §4.3/§4.8 390px 列适配（CSS 断言；真实视口在批次 E Chromium 验收）
	it("批次A-8: 次要/桌面列可隐藏、关键列保留、移动堆叠补行、日期不 break-all", () => {
		// 用户表次要列（角色/登录账号/最近调用）带隐藏标记
		expect(htmlSrc).toMatch(/<th[^>]*adm-col-secondary[^>]*>登录账号</);
		expect(htmlSrc).toMatch(/<th[^>]*adm-col-secondary[^>]*>角色</);
		expect(htmlSrc).toMatch(/<th[^>]*adm-col-secondary[^>]*>最近 AI 调用</);
		// P0-2：AI access 与剩余为桌面列（≤767px 隐藏，由堆叠补行接管）
		expect(htmlSrc).toMatch(/<th[^>]*adm-col-desktop[^>]*>AI access</);
		expect(htmlSrc).toMatch(/<th[^>]*adm-col-desktop[^>]*>剩余</);
		expect(htmlSrc).toContain("<th>显示名</th>");
		expect(htmlSrc).toContain("<th>本月用量</th>");
		expect(htmlSrc).toContain("<th>状态</th>");
		expect(htmlSrc).toContain("<th>操作</th>");
		// 窄屏媒体查询隐藏次要列与桌面列，显示堆叠补行（4 列 = 显示名/状态+AI/
		// 用量+剩余/操作），不靠横向滚动
		expect(cssSrc).toMatch(/@media \(max-width:\s*767px\)[\s\S]*\.adm-col-secondary\s*{[^}]*display:\s*none/);
		expect(cssSrc).toMatch(/@media \(max-width:\s*767px\)[\s\S]*\.adm-table--users \.adm-col-desktop\s*{[^}]*display:\s*none/);
		expect(cssSrc).toMatch(/@media \(max-width:\s*767px\)[\s\S]*\.adm-table--users \.adm-stack-mobile\s*{[^}]*display:\s*block/);
		// 堆叠补行桌面默认隐藏（桌面显示独立列）
		expect(cssSrc).toMatch(/\.adm-stack-mobile\s*{[^}]*display:\s*none/);
		// 移动端迷你条收窄（~72px），避免挤掉剩余文字
		expect(cssSrc).toMatch(/@media \(max-width:\s*767px\)[\s\S]*\.adm-usage-meter\s*{[^}]*width:\s*7[0-9]px/);
		// 日期单元格不允许 break-all（窗口边界在 → 两侧换行）
		expect(cssSrc).toMatch(/\.adm-cell-time\s*{[^}]*word-break:\s*normal/);
		expect(htmlSrc).toMatch(/id="adm-window-boundary"/);
	});

	// P0-1 回归：390px 导航完整标签——平板图标字符规则（特异性 0,2,1）必须
	// 在移动块内按同特异性逐页复位，否则首字符重复（概概览/户用户）
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
		// 作废：完整文字恢复（无绿点、无「已连接」）
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
		await ticks(4);
		const line = bus.els["adm-actor-line"].textContent;
		expect(line).toContain("owner");
		expect(line).toContain("o***r@x.com");
		// 固定 epoch：UTC 2023-11-14T22:13:20Z → 上海 2023-11-15 06:13:20
		expect(bus.client!.fmtTs(1700000000)).toBe("2023-11-15 06:13:20 GMT+8");
		// Flask JSON 把 datetime 序列化成 HTTP 日期字符串（spend policy
		// effective_from）——同样按上海 GMT+8 显示，不吞错
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
					  action: "spend.window.adjust", target_type: "spend_window",
					  target_id: "w1", detail: { from_cny: "40", to_cny: "50" } },
					{ ts: 1700000001, actor_role: "owner", actor_user_id: "u0",
					  action: "exotic.future_action", target_type: "x", target_id: "y",
					  detail: { unknown_field: "v1", another: 2 } },
				],
				next_cursor: null,
			},
		});
		await ticks(4);
		const tbody = bus.els["adm-audit-tbody"].textContent;
		expect(tbody).toContain("2023-11-15 06:13:20 GMT+8"); // 上海时间主显示
		expect(tbody).toContain("原始详情");
		expect(tbody).toContain("unknown_field"); // 未知字段不丢
		expect(tbody).toContain("v1");
		// 已知 action 出人类可读摘要（键值翻译），裸 JSON 退到「原始详情」折叠区
		expect(tbody).toContain("调整前：40；调整后：50");
	});

	// §4.7 设置页窗口摘要卡
	it("批次A-12: 设置页当前窗口拆成摘要卡（额度/已消费/预占/剩余/版本 + raw）", async () => {
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
					policies: {
						demo_global: { policy_id: "p1", version: 4,
							limit_nano_cny: "50000000000", effective_from: 1700000000 },
					},
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
		replyMethod(bus, NONCE, "admin.users.list", {
			ok: true, result: { items: [], next_cursor: null },
		});
		await ticks(6);
		const cards = bus.created.filter((el) =>
			String(el.className).includes("adm-summary-card"));
		expect(cards.length).toBeGreaterThanOrEqual(1);
		const cardText = cards.map((el) => el.textContent).join("\n");
		expect(cardText).toContain("已消费（spent）21.3 CNY");
		expect(cardText).toContain("预占（reserved）2.5 CNY");
		expect(cardText).toContain("剩余（remaining）26.2 CNY");
		expect(cardText).toContain("v3");
		// 原始 nano 在可展开 raw 区
		const rawText = bus.created.filter((el) =>
			String(el.className).includes("adm-raw-values"))
			.map((el) => el.textContent).join("\n");
		expect(rawText).toContain("21300000000");
	});
});
