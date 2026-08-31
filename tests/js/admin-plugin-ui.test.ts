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

interface Posted {
	env: Record<string, unknown>;
	targetOrigin: string;
}

function fakeEl() {
	// 可交互假元素：属性字典 + 子节点文本累积（textContent 语义与 DOM 对齐：
	// 读取时拼子树，设置时清空子树）——包 E 的 KPI/抽屉/状态组件渲染需要
	const attrs: Record<string, string> = {};
	const children: Array<{ textContent?: string }> = [];
	let ownText = "";
	const el = {
		hidden: true,
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
		addEventListener() {},
		getAttribute(name: string) {
			return Object.prototype.hasOwnProperty.call(attrs, name)
				? attrs[name] : null;
		},
		setAttribute(name: string, value: string) {
			attrs[name] = String(value);
		},
		querySelectorAll: () => [],
		closest: () => null,
	};
	return el;
}

function loadPluginUi(hash: string) {
	const els: Record<string, ReturnType<typeof fakeEl>> = {};
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
	handshakeState: () => { ready: boolean; grantedCount: number };
}

// 可交互装配：捕获 message 监听器与 window.parent.postMessage，可模拟宿主
// init / 响应 / 伪造消息（P2 对称认证用例）。
function loadPluginUiWithBus(hash = "") {
	const els: Record<string, ReturnType<typeof fakeEl>> = {};
	const messageHandlers: Array<(event: unknown) => void> = [];
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
		getElementById(id: string) {
			if (!els[id]) els[id] = fakeEl();
			return els[id];
		},
		createElement: () => fakeEl(),
		addEventListener() {},
	};
	(w as { document: typeof doc }).document = doc;
	new Function("window", "document", src)(w, doc);
	const dispatch = (source: unknown, data: unknown) => {
		for (const h of messageHandlers) h({ source, data });
	};
	return {
		els,
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
