/**
 * 注册弹窗 Turnstile 按需加载器（static/register-turnstile.js，2026-10-08
 * 设计 §5/§7）。加载真实源码（最小 fake DOM），锁定：
 *   - 按需加载：仅注册视图可见时注入 api.js?render=explicit；登录视图不加载；
 *     只注入一次；失败后「重试」重新注入；
 *   - 显式渲染参数（sitekey/action/language/size/theme）+ callback 存 token；
 *   - 提交护栏：无 token → preventDefault + 中性提示（绝不出现「机器人」）；
 *     token 就绪 → 放行；
 *   - 加载失败 → 不可用提示 + 重试按钮 + 求助链接；重试重新注入脚本；
 *   - error-callback 清 token + 中性提示；expired-callback 清 token 并 reset；
 *   - 重新显示（登录↔注册切换）与 bfcache pageshow 都 reset；
 *   - form_locale 初始 + hp-lang-change 同步（仅 zh|en）；
 *   - 重发倒计时：倒计时中禁用 +「X:YY 后可重新发送」且不提前渲染重发
 *     widget（token 5 分钟过期），归零恢复按钮并渲染；
 *   - limit 恢复时间显示为本地时间。
 */
import { describe, expect, it, beforeEach, afterEach } from "vitest";
import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const src = readFileSync(
	resolve(here, "../../static/register-turnstile.js"), "utf8");

/* ---- 可控时钟：Date.now 可跳进（模拟后台标签页节流后回前台） ---- */
const REAL_DATE_NOW = Date.now.bind(Date);
const clock = { now: 1_700_000_000_000 }; // 固定起点（ms）
beforeEach(() => {
	clock.now = 1_700_000_000_000;
	Date.now = () => clock.now;
});
afterEach(() => {
	Date.now = REAL_DATE_NOW;
});
const advanceSeconds = (s: number) => {
	clock.now += s * 1000;
};
/** 与被测源码同口径的期望时间格式（zh/en Intl） */
function expectResumeTime(atSec: number, lang: "zh" | "en"): string {
	const d = new Date(atSec * 1000);
	const locale = lang === "en" ? "en" : "zh-CN";
	const time = new Intl.DateTimeFormat(locale, {
		hour: "2-digit", minute: "2-digit",
	}).format(d);
	const now = new Date();
	const sameDay = d.getFullYear() === now.getFullYear() &&
		d.getMonth() === now.getMonth() && d.getDate() === now.getDate();
	if (sameDay) return time;
	const date = new Intl.DateTimeFormat(locale, {
		month: "short", day: "numeric",
	}).format(d);
	return `${date} ${time}`;
}

/* ---------------- 最小 fake DOM（够 register-turnstile.js 用） ---------------- */

type Handler = (ev?: unknown) => void;

class FakeEl {
	tag: string;
	id: string;
	attrs: Record<string, string> = {};
	children: FakeEl[] = [];
	parentNode: FakeEl | null = null;
	hidden = false;
	open = false;
	disabled = false;
	value = "";
	handlers: Record<string, Handler[]> = {};
	classes = new Set<string>();
	private text = "";
	onload: (() => void) | null = null;
	onerror: (() => void) | null = null;

	constructor(tag: string, id = "") {
		this.tag = tag;
		this.id = id;
		this.attrs.id = id;
	}
	get textContent(): string {
		if (this.children.length > 0) {
			return this.children.map((c) => c.textContent).join("");
		}
		return this.text;
	}
	set textContent(v: string) {
		// 置文本清空子节点（与 DOM 语义一致：按钮文案被倒计时改写等）
		this.children = [];
		this.text = String(v);
	}
	get className(): string {
		return Array.from(this.classes).join(" ");
	}
	set className(v: string) {
		this.classes = new Set(String(v).split(/\s+/).filter(Boolean));
	}
	get firstChild(): FakeEl | null {
		return this.children[0] ?? null;
	}
	get nextSibling(): FakeEl | null {
		if (!this.parentNode) return null;
		const i = this.parentNode.children.indexOf(this);
		return this.parentNode.children[i + 1] ?? null;
	}
	appendChild(child: FakeEl): FakeEl {
		child.parentNode = this;
		this.children.push(child);
		return child;
	}
	insertBefore(child: FakeEl, ref: FakeEl | null): FakeEl {
		child.parentNode = this;
		const i = ref ? this.children.indexOf(ref) : -1;
		if (i < 0) this.children.push(child);
		else this.children.splice(i, 0, child);
		return child;
	}
	removeChild(child: FakeEl): FakeEl {
		const i = this.children.indexOf(child);
		if (i >= 0) this.children.splice(i, 1);
		child.parentNode = null;
		return child;
	}
	getAttribute(name: string): string | null {
		return Object.prototype.hasOwnProperty.call(this.attrs, name)
			? this.attrs[name]
			: null;
	}
	setAttribute(name: string, v: string): void {
		this.attrs[name] = String(v);
		if (name === "value") this.value = String(v);
		if (name === "hidden") this.hidden = true;
	}
	hasAttribute(name: string): boolean {
		return Object.prototype.hasOwnProperty.call(this.attrs, name);
	}
	addEventListener(type: string, fn: Handler): void {
		(this.handlers[type] = this.handlers[type] || []).push(fn);
	}
	emit(type: string, ev?: Record<string, unknown>): void {
		(this.handlers[type] || []).forEach((fn) =>
			fn({ preventDefault() {}, ...ev }));
	}
	get classList() {
		const s = this.classes;
		return {
			add: (c: string) => void s.add(c),
			remove: (c: string) => void s.delete(c),
			contains: (c: string) => s.has(c),
		};
	}
	querySelector(sel: string): FakeEl | null {
		return this.querySelectorAll(sel)[0] ?? null;
	}
	querySelectorAll(sel: string): FakeEl[] {
		const out: FakeEl[] = [];
		const visit = (el: FakeEl) => {
			el.children.forEach((child) => {
				if (matches(child, sel)) out.push(child);
				visit(child);
			});
		};
		visit(this);
		return out;
	}
}

/** 支持 tag / .class / #id / [attr="v"] 及其组合的极简选择器 */
function matches(el: FakeEl, sel: string): boolean {
	const m = sel.match(
		/^([a-zA-Z][\w-]*|\*)?((?:\.[\w-]+|\[[^\]=]+(?:="[^"]*")?\]|#[\w-]+)*)$/);
	if (!m) return false;
	const tag = m[1] || "*";
	if (tag !== "*" && el.tag !== tag) return false;
	const re = /\.([\w-]+)|\[([^\]=]+)(?:="([^"]*)")?\]|#([\w-]+)/g;
	let mm: RegExpExecArray | null;
	while ((mm = re.exec(m[2])) !== null) {
		if (mm[1] !== undefined) {
			if (!el.classes.has(mm[1])) return false;
		} else if (mm[2] !== undefined) {
			if (el.getAttribute(mm[2]) !== (mm[3] ?? "")) return false;
		} else if (mm[4] !== undefined) {
			if (el.id !== mm[4]) return false;
		}
	}
	return true;
}

interface FakeDoc {
	head: FakeEl;
	root: FakeEl;
	byId: Map<string, FakeEl>;
	handlers: Record<string, Handler[]>;
	doc: unknown;
	emit(type: string, ev?: Record<string, unknown>): void;
}

function makeDoc(): FakeDoc {
	const root = new FakeEl("body");
	const head = new FakeEl("head");
	const byId = new Map<string, FakeEl>();
	const handlers: Record<string, Handler[]> = {};
	const docObj = {
		head,
		body: root,
		createElement(tag: string) {
			return new FakeEl(tag);
		},
		getElementById(id: string) {
			return byId.get(id) ?? null;
		},
		querySelector(sel: string) {
			return root.querySelector(sel);
		},
		querySelectorAll(sel: string) {
			return root.querySelectorAll(sel);
		},
		addEventListener(type: string, fn: Handler) {
			(handlers[type] = handlers[type] || []).push(fn);
		},
	};
	return {
		head, root, byId, handlers, doc: docObj,
		emit(type, ev) {
			(handlers[type] || []).forEach((fn) =>
				fn({ preventDefault() {}, ...ev }));
		},
	};
}

/** 可控时钟 + window 事件 */
function makeWindow(lang: string) {
	const timers: Array<{ fn: () => void }> = [];
	const listeners: Record<string, Handler[]> = {};
	const zh: Record<string, string> = {
		"register.turnstile.pending": "请先完成下方的安全验证",
		"register.turnstile.unavailable": "安全验证暂时无法加载，请检查网络后重试",
		"register.turnstile.retry": "重试",
		"register.turnstile.loading": "正在加载安全验证…",
		"register.help.link": "注册遇到问题？给作者发邮件",
		"register.state.resend.wait": "{time} 后可重新发送",
		"register.state.limit.resume": "预计 {time}（本地时间）后可再次自助发送",
	};
	const win = {
		HP_I18N: {
			getLang: () => lang,
			t: (key: string) => zh[key] ?? key,
		},
		setTimeout: (fn: () => void) => {
			timers.push({ fn });
			return timers.length;
		},
		clearTimeout: () => {},
		addEventListener: (type: string, fn: Handler) => {
			(listeners[type] = listeners[type] || []).push(fn);
		},
	};
	return {
		win, timers, listeners,
		runTimers(n: number) {
			for (let i = 0; i < n; i += 1) {
				const t = timers.shift();
				if (!t) return;
				t.fn();
			}
		},
		emitWin(type: string, ev?: Record<string, unknown>) {
			(listeners[type] || []).forEach((fn) => fn({ ...ev }));
		},
	};
}

function mockTurnstile() {
	const calls: Array<[string, ...unknown[]]> = [];
	let nextId = 1;
	return {
		calls,
		render(el: unknown, params: Record<string, unknown>) {
			const id = nextId;
			nextId += 1;
			calls.push(["render", id, el, params]);
			return id;
		},
		reset(id: number) {
			calls.push(["reset", id]);
		},
		remove(id: number) {
			calls.push(["remove", id]);
		},
	};
}

type TurnstileMock = ReturnType<typeof mockTurnstile>;

function load(docObj: unknown, winObj: unknown) {
	// 源码为 IIFE，加载即初始化
	new Function("document", "window", src)(docObj, winObj);
}

// Promise.then（loadApi→render）在微任务中执行；两次 await 保证冲刷
const flush = async () => {
	await Promise.resolve();
	await Promise.resolve();
};

/** 标准注册场景：弹窗直开注册视图 + 表单/容器 + form_locale */
function setupRegister(opts: {
	lang?: string;
	turnstile?: TurnstileMock | null;
	registerHidden?: boolean;
	resendAt?: number;
	resumeAt?: number;
} = {}) {
	const d = makeDoc();
	const w = makeWindow(opts.lang ?? "zh");

	const dialog = new FakeEl("dialog", "login-dialog");
	dialog.open = true;
	const registerView = new FakeEl("div", "register-view");
	registerView.hidden = opts.registerHidden ?? false;
	const loginView = new FakeEl("div", "login-view");
	loginView.hidden = !registerView.hidden;

	const form = new FakeEl("form", "register-dialog-form");
	const submitBtn = new FakeEl("button", "register-submit");
	submitBtn.setAttribute("type", "submit");
	submitBtn.textContent = "发送验证邮件";
	const localeInput = new FakeEl("input", "form-locale-1");
	localeInput.setAttribute("name", "form_locale");
	localeInput.setAttribute("value", "zh");
	form.appendChild(localeInput);
	// 容器：默认在 start 表单内；resendAt 时挂在重发表单上（状态视图替换表单）
	const box = new FakeEl("div", "register-turnstile");
	box.setAttribute("data-sitekey", "1x00000000000000000000AA");
	box.setAttribute("data-action", "registration_start");
	form.appendChild(box);
	form.appendChild(submitBtn);
	registerView.appendChild(form);

	let resendForm: FakeEl | null = null;
	let resendBox: FakeEl | null = null;
	let resendBtn: FakeEl | null = null;
	if (opts.resendAt != null) {
		resendForm = new FakeEl("form", "register-resend-form");
		resendForm.setAttribute("data-resend-at", String(opts.resendAt));
		resendBox = new FakeEl("div", "register-turnstile");
		resendBox.setAttribute("data-sitekey", "1x00000000000000000000AA");
		resendBox.setAttribute("data-action", "registration_resend");
		resendBtn = new FakeEl("button", "register-resend-submit");
		resendBtn.setAttribute("type", "submit");
		resendBtn.textContent = "重新发送验证邮件";
		const locale2 = new FakeEl("input", "form-locale-2");
		locale2.setAttribute("name", "form_locale");
		resendForm.appendChild(locale2);
		resendForm.appendChild(resendBox);
		resendForm.appendChild(resendBtn);
		const span = new FakeEl("span", "register-resend-countdown");
		span.setAttribute("data-resend-at", String(opts.resendAt));
		registerView.appendChild(span);
		registerView.appendChild(resendForm);
		// submitted/cooldown 态：start 表单不在 DOM（被状态视图替换）
		form.removeChild(box);
		form.removeChild(submitBtn);
	}
	if (opts.resumeAt != null) {
		const span = new FakeEl("span", "register-resume-countdown");
		span.setAttribute("data-resume-at", String(opts.resumeAt));
		registerView.appendChild(span);
	}
	d.root.appendChild(dialog);
	d.root.appendChild(loginView);
	d.root.appendChild(registerView);
	// 重建 id 索引（后挂的同名 id 覆盖前者——与 DOM 唯一性一致）
	d.byId.clear();
	const index = (el: FakeEl) => {
		if (el.id) d.byId.set(el.id, el);
		el.children.forEach(index);
	};
	index(d.root);

	const ts = opts.turnstile === null ? null
		: (opts.turnstile ?? mockTurnstile());
	if (ts) Object.assign(w.win, { turnstile: ts });
	load(d.doc, w.win);
	return { d, w, form, box, submitBtn, resendForm, resendBox, resendBtn,
		ts, dialog, registerView, loginView };
}

/** 手动触发脚本加载成功（注入 window.turnstile 后调 onload） */
function succeedScriptLoad(d: FakeDoc, w: ReturnType<typeof makeWindow>,
	ts: TurnstileMock) {
	const script = d.head.children[d.head.children.length - 1];
	Object.assign(w.win, { turnstile: ts });
	script.onload!();
}

describe("register-turnstile（注册弹窗 Turnstile 按需加载器）", () => {
	it("登录视图可见时不加载 api.js；切到注册视图才注入且只注入一次", async () => {
		const ctx = setupRegister({ registerHidden: true, turnstile: null });
		await flush();
		expect(ctx.d.head.children.length).toBe(0);
		// 切到注册视图（entry-auth.js setView：先揭 hidden，再广播 hp-auth-view）
		ctx.registerView.hidden = false;
		ctx.loginView.hidden = true;
		ctx.d.emit("hp-auth-view", { detail: { view: "register", open: true } });
		await flush();
		expect(ctx.d.head.children.length).toBe(1);
		const script = ctx.d.head.children[0];
		expect(script.getAttribute("src")).toBe(
			"https://challenges.cloudflare.com/turnstile/v0/api.js?render=explicit");
		// 再次触发（重复 show）：不重复注入
		ctx.d.emit("hp-auth-view", { detail: { view: "register", open: true } });
		await flush();
		expect(ctx.d.head.children.length).toBe(1);
	});

	it("脚本 onload 后显式渲染：sitekey/action/language/size/theme 取自容器与当前语言", async () => {
		const ctx = setupRegister({ turnstile: null });
		const ts = mockTurnstile();
		succeedScriptLoad(ctx.d, ctx.w, ts);
		await flush();
		const renderCall = ts.calls.find((c) => c[0] === "render")!;
		expect(renderCall).toBeTruthy();
		const [, id, el, params] = renderCall as [string, number, FakeEl,
			Record<string, unknown>];
		expect(el).toBe(ctx.box);
		expect(params.sitekey).toBe("1x00000000000000000000AA");
		expect(params.action).toBe("registration_start");
		expect(params.theme).toBe("auto");
		expect(params.size).toBe("flexible");
		expect(typeof id).toBe("number");
		// zh → language 'zh-cn'
		expect(params.language).toBe("zh-cn");
		// en → language 'en'
		const ctx2 = setupRegister({ lang: "en", turnstile: null });
		const ts2 = mockTurnstile();
		succeedScriptLoad(ctx2.d, ctx2.w, ts2);
		await flush();
		const params2 = ts2.calls.find((c) => c[0] === "render")![3] as
			Record<string, unknown>;
		expect(params2.language).toBe("en");
	});

	it("提交护栏：无 token 时 preventDefault + 中性提示；token 就绪后放行", async () => {
		const ctx = setupRegister({});
		await flush(); // 初始渲染（window.turnstile 已预置 → 无脚本注入）
		expect(ctx.d.head.children.length).toBe(0);
		expect(ctx.ts!.calls.some((c) => c[0] === "render")).toBe(true);
		let prevented = 0;
		ctx.d.emit("submit", {
			target: ctx.form, preventDefault: () => { prevented += 1; },
		});
		expect(prevented).toBe(1);
		// 中性提示（zh 词典）；全文档绝不出现「机器人」
		const notice = ctx.d.root.querySelector(".register-turnstile-notice")!;
		expect(notice).toBeTruthy();
		expect(notice.textContent).toContain("请先完成下方的安全验证");
		expect(ctx.d.root.textContent).not.toContain("机器人");
		// 回调给 token 后放行，且提示撤除
		const renderCall = ctx.ts!.calls.find((c) => c[0] === "render")!;
		const params = renderCall[3] as Record<string, (t?: string) => void>;
		params.callback!("tok-1");
		prevented = 0;
		ctx.d.emit("submit", {
			target: ctx.form, preventDefault: () => { prevented += 1; },
		});
		expect(prevented).toBe(0);
		expect(ctx.d.root.querySelector(".register-turnstile-notice")).toBeNull();
	});

	it("脚本加载失败 → 不可用提示 + 重试按钮 + 求助链接；重试重新注入脚本并渲染", async () => {
		const ctx = setupRegister({ turnstile: null });
		await flush(); // 脚本已注入、尚未 onload
		expect(ctx.d.head.children.length).toBe(1);
		ctx.d.head.children[0].onerror!();
		await flush(); // 拒绝 → .catch → 不可用提示（微任务）
		const notice = ctx.d.root.querySelector(".register-turnstile-notice")!;
		expect(notice.textContent).toContain("安全验证暂时无法加载");
		expect(ctx.d.root.textContent).not.toContain("机器人");
		const retryBtn =
			notice.querySelector("button.register-turnstile-retry")!;
		expect(retryBtn.textContent).toBe("重试");
		const help = notice.querySelector("a")!;
		expect(help.getAttribute("href")).toBe(
			"/registration-help?reason=challenge");
		// 重试：重新注入脚本；这次成功 → 渲染
		retryBtn.emit("click");
		expect(ctx.d.head.children.length).toBe(2);
		const ts = mockTurnstile();
		succeedScriptLoad(ctx.d, ctx.w, ts);
		await flush();
		expect(ts.calls.some((c) => c[0] === "render")).toBe(true);
	});

	it("error-callback 清 token + 中性不可用提示；expired-callback 清 token 并 reset", async () => {
		const ctx = setupRegister({});
		await flush();
		const calls = ctx.ts!.calls;
		const renderCall = calls.find((c) => c[0] === "render")!;
		const params = renderCall[3] as Record<string, (t?: string) => void>;
		params.callback!("tok-1");
		params["error-callback"]!();
		const notice = ctx.d.root.querySelector(".register-turnstile-notice")!;
		expect(notice.textContent).toContain("安全验证暂时无法加载");
		expect(calls.some((c) => c[0] === "reset")).toBe(true);
		// error 后 token 已清：提交再次被拦截
		let prevented = 0;
		ctx.d.emit("submit", {
			target: ctx.form, preventDefault: () => { prevented += 1; },
		});
		expect(prevented).toBe(1);
		// expired-callback：清 token + reset
		params.callback!("tok-2");
		const resetsBefore = calls.filter((c) => c[0] === "reset").length;
		params["expired-callback"]!();
		expect(calls.filter((c) => c[0] === "reset").length).toBe(resetsBefore + 1);
	});

	it("重新显示（登录↔注册切换再回来）与 bfcache pageshow 都会 reset 挑战", async () => {
		const ctx = setupRegister({});
		await flush();
		const id = ctx.ts!.calls.find((c) => c[0] === "render")![1] as number;
		ctx.ts!.calls.length = 0;
		ctx.d.emit("hp-auth-view", { detail: { view: "login", open: true } });
		ctx.d.emit("hp-auth-view", { detail: { view: "register", open: true } });
		await flush();
		expect(ctx.ts!.calls.some((c) => c[0] === "reset" && c[1] === id))
			.toBe(true);
		// bfcache 恢复
		ctx.ts!.calls.length = 0;
		ctx.w.emitWin("pageshow", { persisted: true });
		await flush();
		expect(ctx.ts!.calls.some((c) => c[0] === "reset" && c[1] === id))
			.toBe(true);
	});

	it("form_locale：初始按 UI 语言同步所有隐藏域，hp-lang-change 时再同步（仅 zh|en）", async () => {
		const ctx = setupRegister({ lang: "zh" });
		const input = ctx.form.querySelector('input[name="form_locale"]')!;
		expect(input.value).toBe("zh");
		// 切换语言事件（i18n.js 广播）
		(ctx.w.win.HP_I18N as { getLang: () => string }).getLang = () => "en";
		ctx.d.emit("hp-lang-change", {});
		expect(input.value).toBe("en");
		// 非法语言回落 zh（HP_I18N 契约：只返回 zh|en）
		(ctx.w.win.HP_I18N as { getLang: () => string }).getLang = () => "zh";
		ctx.d.emit("hp-lang-change", {});
		expect(input.value).toBe("zh");
	});

	it("submitted/cooldown 倒计时：禁用 + 「X:YY 后可重新发送」；后台节流（tick 少、时钟跳进）仍按真实时钟恢复", async () => {
		const ts = mockTurnstile();
		const ctx = setupRegister({
			turnstile: ts, resendAt: Math.floor(clock.now / 1000) + 90,
		});
		await flush();
		// 倒计时未结束：widget 未渲染（token 5 分钟过期，等按钮可用再渲染），
		// 容器收起、无占位（不预留高度）
		expect(ts.calls.some((c) => c[0] === "render")).toBe(false);
		expect(ctx.d.root.querySelector(".register-turnstile-placeholder"))
			.toBeNull();
		const btn = ctx.resendBtn!;
		expect(btn.disabled).toBe(true);
		expect(btn.textContent).toContain("1:30");
		expect(btn.textContent).toContain("后可重新发送");
		// cooldown 态的 span 同步显示
		const span = ctx.d.byId.get("register-resend-countdown")!;
		expect(span.textContent).toContain("1:30");
		// 后台 60s：定时器被节流只跑了 1 个 tick——剩余时间按真实时钟重算
		advanceSeconds(60);
		ctx.w.runTimers(1);
		expect(btn.disabled).toBe(true);
		expect(btn.textContent).toContain("0:30");
		// 再过 31s 回前台（visibilitychange）：立即校正 → 恢复按钮 + 渲染重发 widget
		advanceSeconds(31);
		ctx.d.emit("visibilitychange", {});
		expect(btn.disabled).toBe(false);
		expect(btn.textContent).toBe("重新发送验证邮件");
		expect(span.textContent).toBe("");
		await flush();
		const renderCall = ts.calls.find((c) => c[0] === "render")!;
		const params = renderCall[3] as Record<string, unknown>;
		expect(params.action).toBe("registration_resend");
		// 渲染后占位撤除
		expect(ctx.d.root.querySelector(".register-turnstile-placeholder"))
			.toBeNull();
	});

	it("form 容器：渲染前显示中性占位「正在加载安全验证…」，渲染成功后移除", async () => {
		const ctx = setupRegister({ turnstile: null });
		await flush(); // 脚本已注入、尚未 onload → 加载中
		const ph = ctx.d.root.querySelector(".register-turnstile-placeholder");
		expect(ph).toBeTruthy();
		expect(ph!.textContent).toContain("正在加载安全验证");
		expect(ctx.d.root.textContent).not.toContain("机器人");
		// 渲染成功 → 占位移除
		succeedScriptLoad(ctx.d, ctx.w, mockTurnstile());
		await flush();
		expect(ctx.d.root.querySelector(".register-turnstile-placeholder"))
			.toBeNull();
	});

	it("加载失败：占位让位于不可用提示；重试先恢复占位", async () => {
		const ctx = setupRegister({ turnstile: null });
		await flush();
		expect(ctx.d.root.querySelector(".register-turnstile-placeholder"))
			.toBeTruthy();
		ctx.d.head.children[0].onerror!();
		await flush();
		expect(ctx.d.root.querySelector(".register-turnstile-placeholder"))
			.toBeNull(); // 占位撤除
		expect(ctx.d.root.querySelector(".register-turnstile-notice"))
			.toBeTruthy(); // 不可用提示在场
		// 重试：先恢复占位（清除失败态），脚本再失败仍回到提示
		const retryBtn = ctx.d.root
			.querySelector("button.register-turnstile-retry")!;
		retryBtn.emit("click");
		expect(ctx.d.root.querySelector(".register-turnstile-placeholder"))
			.toBeTruthy();
	});

	it("limit 恢复时间：当天只显示 HH:MM；非当天带日期（Intl 跟随 UI 语言）", () => {
		// 固定本地正午，避免跨日边界
		const noon = new Date(REAL_DATE_NOW());
		noon.setHours(12, 0, 0, 0);
		clock.now = noon.getTime();
		const baseSec = Math.floor(clock.now / 1000);
		const ctxSame = setupRegister({ resumeAt: baseSec + 60 });
		const sameText = ctxSame.d.byId.get("register-resume-countdown")!.textContent;
		expect(sameText).toContain(expectResumeTime(baseSec + 60, "zh"));
		const d = new Date((baseSec + 48 * 3600) * 1000);
		const zhShort = new Intl.DateTimeFormat("zh-CN", {
			month: "short", day: "numeric",
		}).format(d);
		expect(sameText).not.toContain(zhShort); // 当天不带日期

		const ctxFar = setupRegister({ resumeAt: baseSec + 48 * 3600 });
		const farText = ctxFar.d.byId.get("register-resume-countdown")!.textContent;
		expect(farText).toContain(zhShort); // 非当天带日期（如 “10月9日”）
		expect(farText).toContain(expectResumeTime(baseSec + 48 * 3600, "zh"));
	});

	it("limit 恢复时间在 hp-lang-change 后按新语言重渲染（en 带英文日期）", () => {
		const baseSec = Math.floor(clock.now / 1000);
		const ctx = setupRegister({ lang: "zh", resumeAt: baseSec + 48 * 3600 });
		const span = ctx.d.byId.get("register-resume-countdown")!;
		expect(span.textContent).toContain(expectResumeTime(baseSec + 48 * 3600, "zh"));
		(ctx.w.win.HP_I18N as { getLang: () => string }).getLang = () => "en";
		ctx.d.emit("hp-lang-change", {});
		expect(span.textContent).toContain(expectResumeTime(baseSec + 48 * 3600, "en"));
	});
});
