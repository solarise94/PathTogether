/**
 * 登录/注册统一弹窗（R2 2026-09-19）：加载真实 static/entry-auth.js（最小 DOM），
 * 锁定：
 *   - 拦截 /register 链接原地切换视图：一次只显示一个 pane，aria-labelledby
 *     跟随当前视图，焦点移入新视图首个输入框（键盘可切换）；
 *   - showModal 升级 + body 滚动锁 + 关闭（背景点击/cancel）还原焦点；
 *   - 服务端 register_open 直开时保持注册视图；
 *   - 表单双击防护：提交中重复 submit 被 preventDefault；
 *   - 限流倒计时：归零前禁用所属表单提交按钮，归零恢复。
 */
import { describe, expect, it, vi } from "vitest";
import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const authSrc = readFileSync(resolve(here, "../../static/entry-auth.js"), "utf8");

interface FakeEl {
	id: string;
	hidden: boolean;
	open: boolean;
	disabled: boolean;
	textContent: string;
	dataset: Record<string, string>;
	classList: { toggle(cls: string, on?: boolean): void };
	addEventListener(type: string, fn: (ev?: unknown) => void): void;
	_clickHandlers: Record<string, Array<(ev?: unknown) => void>>;
	click(): void;
	focus(): void;
	closest(): FakeEl | null;
	getAttribute(name: string): string | null;
	setAttribute(name: string, value: string): void;
	removeAttribute(name: string): void;
	showModal(): void;
	close(): void;
	querySelector(sel: string): FakeEl | null;
	hasAttribute?(name: string): boolean;
}

function fakeEl(id: string, attrs: Partial<FakeEl> = {}): FakeEl {
	const handlers: Record<string, Array<(ev?: unknown) => void>> = {};
	const el: FakeEl = {
		id,
		hidden: false,
		open: false,
		disabled: false,
		textContent: "",
		dataset: {},
		classList: { toggle: () => {} },
		addEventListener(type: string, fn: (ev?: unknown) => void) {
			(handlers[type] = handlers[type] || []).push(fn);
		},
		_clickHandlers: handlers,
		click() {
			(handlers["click"] || []).forEach((fn) => fn({ preventDefault() {} }));
		},
		focus: () => {},
		closest: () => null,
		getAttribute: () => null,
		setAttribute: () => {},
		removeAttribute: () => {},
		showModal: () => {},
		close: () => {},
		querySelector: () => null,
		...attrs,
	} as FakeEl;
	// 允许测试覆写方法后仍保留默认键
	return Object.assign(el, attrs);
}

function makeDoc(opts: {
	dialogOpen?: boolean;
	retrySeconds?: string;
	againNav?: boolean;
} = {}) {
	const focusLog: string[] = [];
	const dialogAttrs: Record<string, string> = {
		"aria-labelledby": "login-dialog-title",
		"data-retry-seconds": opts.retrySeconds ?? "",
	};

	function pane(id: string, firstInputId: string, h2Id: string) {
		const input = fakeEl(firstInputId, { focus: () => focusLog.push(firstInputId) });
		const h2 = fakeEl(h2Id);
		h2.id = h2Id;
		const paneEl = fakeEl(id, {
			querySelector: (sel: string) => {
				if (sel === "h2") return h2;
				if (sel === 'input:not([type="hidden"])') return input;
				return null;
			},
		});
		paneEl.hidden = id === "register-view";
		return paneEl;
	}

	const loginView = pane("login-view", "login-dialog-username", "login-dialog-title");
	const registerView = pane("register-view", "register-dialog-email", "register-dialog-title");

	const closeBtn = fakeEl("close-btn");
	const loginForm = fakeEl("login-dialog-form");
	const registerForm = fakeEl("register-dialog-form");

	const dialog = fakeEl("login-dialog", {
		open: opts.dialogOpen ?? false,
		showModal() {
			dialog.open = true;
		},
		close() {
			dialog.open = false;
		},
		removeAttribute(name: string) {
			delete dialogAttrs[name];
			if (name === "open") dialog.open = false;
		},
		setAttribute(name: string, value: string) {
			dialogAttrs[name] = value;
			if (name === "open") dialog.open = true;
		},
		getAttribute(name: string) {
			return dialogAttrs[name] ?? null;
		},
		querySelector(sel: string) {
			if (sel === "h2") return loginView.querySelector("h2");
			if (sel === 'input:not([type="hidden"])') return loginView.querySelector("input");
			return null;
		},
	});
	(dialog as unknown as { querySelectorAll(sel: string): unknown[] }).querySelectorAll =
		(sel: string) => {
			if (sel === "[data-login-close]") return [closeBtn];
			if (sel === "form") return [loginForm, registerForm];
			return [];
		};
	dialog.addEventListener("close", () => {});
	dialog.addEventListener("click", (ev) => {
		if ((ev as { target?: unknown })?.target === dialog) dialog.close();
	});

	const loginLinks = [fakeEl("topbar-login")];
	const registerLinks = [fakeEl("dialog-register-link")];
	// 发送成功视图的「重新填写邮箱」链接：带 data-auth-nav（真实导航 opt-out），
	// click 记录是否被 preventDefault（允许导航 = 不被拦截）。
	let navPrevented = 0;
	let againLink: FakeEl | null = null;
	if (opts.againNav) {
		const link = fakeEl("register-again-link", {
			hasAttribute: (name: string) => name === "data-auth-nav",
		});
		link.click = () => {
			(link._clickHandlers["click"] || []).forEach((fn) =>
				fn({ preventDefault() { navPrevented += 1; } }));
		};
		againLink = link;
		registerLinks.push(link);
	}
	// 限流倒计时 DOM 与真实模板同构：span 在 .login-dialog-error 区块内
	// （表单之外），closest('.auth-view') 命中所属视图 pane，再由 pane 取
	// 'form button[type="submit"]'——此前 mock 让 closest('form') 直接返回
	// 表单，掩盖了真实模板中 span 不在表单内导致的提交按钮永不禁用缺陷。
	const countdownSubmit = fakeEl("register-submit-btn");
	const countdownPane = fakeEl("register-view", {
		querySelector: (sel: string) =>
			sel === 'form button[type="submit"]' ? countdownSubmit : null,
	});
	const countdown = fakeEl("register-dialog-countdown", {
		closest: (sel: string) => (sel === ".auth-view" ? countdownPane : null),
		getAttribute: (name: string) =>
			name === "data-retry-seconds" ? (opts.retrySeconds ?? "0") : null,
	});

	const bodyClasses: string[] = [];
	const doc = {
		getElementById(id: string) {
			if (id === "login-dialog") return dialog;
			if (id === "login-view") return loginView;
			if (id === "register-view") return registerView;
			return null;
		},
		querySelectorAll(sel: string) {
			if (sel.startsWith('a[href="/login"]')) return loginLinks;
			if (sel.startsWith('a[href="/register"]')) return registerLinks;
			if (sel === "[data-retry-seconds]") {
				return opts.retrySeconds != null ? [countdown] : [];
			}
			return [];
		},
		body: { classList: { toggle: (cls: string, on?: boolean) => {
			if (on) bodyClasses.push(cls);
		} } },
		activeElement: null as unknown,
	};
	return {
		doc, dialog, loginView, registerView, loginLinks, registerLinks,
		loginForm, registerForm, focusLog, bodyClasses, countdownSubmit,
		dialogAttrs, againLink, navPrevented: () => navPrevented,
	};
}

function load(doc: unknown) {
	(vi as unknown as { stubGlobal(name: string, v: unknown): void }).stubGlobal(
		"window", { setTimeout: () => 0 });
	// node 环境无 HTMLElement：注入最小构造（源码仅做 instanceof 判断）
	class HTMLElement {}
	new Function("document", "window", "HTMLElement", authSrc)(
		doc, window, HTMLElement);
}

describe("entry-auth（登录/注册统一弹窗）", () => {
	it("点击 /register 链接原地打开弹窗并切到注册视图（一次只显示一个 pane）", () => {
		const ctx = makeDoc();
		load(ctx.doc);
		ctx.registerLinks[0].click();
		expect(ctx.dialog.open).toBe(true);
		expect(ctx.loginView.hidden).toBe(true);
		expect(ctx.registerView.hidden).toBe(false);
		// aria-labelledby 跟随当前视图标题
		expect(ctx.dialogAttrs["aria-labelledby"]).toBe("register-dialog-title");
		// 焦点移入注册视图首个输入框
		expect(ctx.focusLog).toContain("register-dialog-email");
	});

	it("关闭（cancel）还原焦点并解除滚动锁；再点 /login 切回登录视图", () => {
		const ctx = makeDoc();
		(ctx.doc as { activeElement: unknown }).activeElement = fakeEl("opener", {
			focus: () => ctx.focusLog.push("opener"),
		});
		load(ctx.doc);
		ctx.registerLinks[0].click();
		expect(ctx.bodyClasses).toContain("login-dialog-open");
		// ESC → cancel 事件
		(ctx.dialog._clickHandlers["cancel"] || []).forEach((fn) =>
			fn({ preventDefault() {} }));
		expect(ctx.dialog.open).toBe(false);
		expect(ctx.focusLog).toContain("opener");
		// /login 链接：重新打开 + 登录视图
		ctx.loginLinks[0].click();
		expect(ctx.dialog.open).toBe(true);
		expect(ctx.loginView.hidden).toBe(false);
		expect(ctx.registerView.hidden).toBe(true);
		expect(ctx.dialogAttrs["aria-labelledby"]).toBe("login-dialog-title");
		expect(ctx.focusLog).toContain("login-dialog-username");
	});

	it("服务端 register_open 直开（open 属性 + 注册视图可见）升级为模态且视图保持", () => {
		const ctx = makeDoc({ dialogOpen: true });
		ctx.registerView.hidden = false;
		ctx.loginView.hidden = true;
		load(ctx.doc);
		// open 属性被摘除后经 showModal 升级为模态
		expect(ctx.dialog.open).toBe(true);
		expect(ctx.loginView.hidden).toBe(true);
		expect(ctx.registerView.hidden).toBe(false);
	});

	it("表单双击防护：提交中重复 submit 被 preventDefault 且按钮禁用", () => {
		const ctx = makeDoc();
		let prevented = 0;
		const makeEv = () => ({ preventDefault: () => { prevented += 1; } });
		load(ctx.doc);
		const handlers = (ctx.loginForm._clickHandlers["submit"] || []);
		expect(handlers.length).toBe(1);
		const ev1 = makeEv();
		handlers[0](ev1);
		expect((ev1 as { defaultPrevented?: boolean }).defaultPrevented).toBeFalsy();
		expect(ctx.loginForm.dataset.submitting).toBe("1");
		const before = prevented;
		handlers[0](makeEv());
		expect(prevented).toBe(before + 1);
	});

	it("限流倒计时：归零前禁用提交按钮（归零立即恢复）", () => {
		const ctx = makeDoc({ retrySeconds: "0" });
		load(ctx.doc);
		expect(ctx.countdownSubmit.disabled).toBe(false);
		const ctx2 = makeDoc({ retrySeconds: "30" });
		load(ctx2.doc);
		expect(ctx2.countdownSubmit.disabled).toBe(true);
	});

	it("发送成功态的「重新填写邮箱」（data-auth-nav）不被拦截（允许真实导航）；data-auth-switch 链接仍原地拦截", () => {
		const ctx = makeDoc({ dialogOpen: true, againNav: true });
		// 服务端 done 态直出：注册视图可见（发送成功视图）、登录视图收起
		ctx.registerView.hidden = false;
		ctx.loginView.hidden = true;
		load(ctx.doc);
		expect(ctx.dialog.open).toBe(true);
		// 重新填写邮箱：真实导航——不注册任何 click 拦截器、不 preventDefault
		expect(ctx.againLink!._clickHandlers["click"]).toBeFalsy();
		ctx.againLink!.click();
		expect(ctx.navPrevented()).toBe(0);
		// 弹窗与视图原样：切换/刷新完全交给浏览器整页导航（深链接渲染干净表单）
		expect(ctx.dialog.open).toBe(true);
		expect(ctx.registerView.hidden).toBe(false);
		expect(ctx.loginView.hidden).toBe(true);
		// 对照：同视图内 data-auth-switch「已有账号？登录」仍被拦截原地切换
		ctx.loginLinks[0].click();
		expect(ctx.dialog.open).toBe(true);
		expect(ctx.loginView.hidden).toBe(false);
		expect(ctx.registerView.hidden).toBe(true);
		expect(ctx.dialogAttrs["aria-labelledby"]).toBe("login-dialog-title");
	});
});
