/**
 * 注册帮助页交互（static/registration-help.js，2026-10-08 设计 §5）。
 * 加载真实源码（最小 fake DOM），锁定：
 *   - mailto 跟随语言：初始 zh → data-mailto-zh；hp-lang-change 后 en →
 *     data-mailto-en；预览 <pre> 从对应 body 参数解码刷新；
 *   - 复制按钮：navigator.clipboard.writeText 优先；成功显示「已复制」并恢复；
 *   - 无剪贴板 API（或拒绝）时回退选中可见邮箱文本 + execCommand('copy')；
 *   - 无 JS 基线：href/preview 由服务端渲染（模板默认，不依赖本脚本存在）。
 */
import { describe, expect, it } from "vitest";
import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const src = readFileSync(
	resolve(here, "../../static/registration-help.js"), "utf8");

/* ---------------- 极简 fake DOM ---------------- */

type Handler = (ev?: unknown) => void;

class FakeEl {
	tag: string;
	id: string;
	attrs: Record<string, string> = {};
	children: FakeEl[] = [];
	parentNode: FakeEl | null = null;
	handlers: Record<string, Handler[]> = {};
	classes = new Set<string>();
	private text = "";
	readonly: string | null = null;

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
		this.children = [];
		this.text = String(v);
	}
	get className(): string {
		return Array.from(this.classes).join(" ");
	}
	set className(v: string) {
		this.classes = new Set(String(v).split(/\s+/).filter(Boolean));
	}
	get classList() {
		const s = this.classes;
		return {
			add: (c: string) => void s.add(c),
			remove: (c: string) => void s.delete(c),
			contains: (c: string) => s.has(c),
		};
	}
	appendChild(child: FakeEl): FakeEl {
		child.parentNode = this;
		this.children.push(child);
		return child;
	}
	getAttribute(name: string): string | null {
		return Object.prototype.hasOwnProperty.call(this.attrs, name)
			? this.attrs[name]
			: null;
	}
	setAttribute(name: string, v: string): void {
		this.attrs[name] = String(v);
	}
	addEventListener(type: string, fn: Handler): void {
		(this.handlers[type] = this.handlers[type] || []).push(fn);
	}
	emit(type: string, ev?: Record<string, unknown>): void {
		(this.handlers[type] || []).forEach((fn) => fn(ev ?? {}));
	}
	querySelector(sel: string): FakeEl | null {
		return this.querySelectorAll(sel)[0] ?? null;
	}
	querySelectorAll(sel: string): FakeEl[] {
		const out: FakeEl[] = [];
		const visit = (el: FakeEl) => {
			el.children.forEach((child) => {
				if (sel.startsWith("#") && child.id === sel.slice(1)) {
					out.push(child);
				}
				visit(child);
			});
		};
		visit(this);
		return out;
	}
}

const ZH_BODY = "你好，我在注册 HistoPilot 时遇到了问题。\n\n注册入口：https://histopilot.cn\n";
const EN_BODY = "Hello, I ran into a problem while registering for HistoPilot.\n\nRegistration entry: https://histopilot.cn\n";

function mailto(body: string): string {
	return "mailto:solarise94@gmail.com?subject=" +
		encodeURIComponent("HistoPilot 注册遇到问题 / Registration help") +
		"&body=" + encodeURIComponent(body);
}

function makePage(langHolder: { value: string }) {
	const timers: Array<{ fn: () => void }> = [];
	const docHandlers: Record<string, Handler[]> = {};
	const body = new FakeEl("body");
	const mail = new FakeEl("a", "reghelp-mail");
	mail.setAttribute("href", mailto(ZH_BODY));
	mail.setAttribute("data-mailto-zh", mailto(ZH_BODY));
	mail.setAttribute("data-mailto-en", mailto(EN_BODY));
	mail.textContent = "给作者发邮件";
	const copy = new FakeEl("button", "reghelp-copy");
	copy.setAttribute("data-author-email", "solarise94@gmail.com");
	copy.textContent = "复制作者邮箱";
	const emailText = new FakeEl("p", "reghelp-email-text");
	emailText.textContent = "solarise94@gmail.com";
	const preview = new FakeEl("pre", "reghelp-template");
	preview.textContent = ZH_BODY; // 服务端默认（入口默认语言）
	[mail, copy, emailText, preview].forEach((el) => body.appendChild(el));

	const clipboardCalls: string[][] = [];
	const docObj = {
		body,
		createElement: (tag: string) => new FakeEl(tag),
		createRange: () => ({
			selectNodeContents: () => {},
		}),
		execCommand: () => true,
		getElementById: (id: string) =>
			([mail, copy, emailText, preview].find((el) => el.id === id)) ?? null,
		addEventListener: (type: string, fn: Handler) => {
			(docHandlers[type] = docHandlers[type] || []).push(fn);
		},
	};
	const winObj = {
		HP_I18N: {
			getLang: () => langHolder.value,
			t: (key: string) => (key === "reghelp.copied" ? "已复制" : key),
		},
		setTimeout: (fn: () => void) => {
			timers.push({ fn });
			return timers.length;
		},
		clearTimeout: () => {},
		addEventListener: () => {},
		navigator: {
			clipboard: {
				writeText: (s: string) => {
					clipboardCalls.push([s]);
					return Promise.resolve();
				},
			},
		},
		getSelection: () => ({
			removeAllRanges: () => {},
			addRange: () => {},
		}),
	};
	new Function("document", "window", "navigator", src)(
		docObj, winObj, winObj.navigator);
	return {
		mail, copy, emailText, preview, clipboardCalls, docHandlers, timers,
		emitDoc(type: string) {
			(docHandlers[type] || []).forEach((fn) => fn({}));
		},
	};
}

describe("registration-help（注册帮助页交互）", () => {
	it("初始 zh：主按钮 href=data-mailto-zh，预览为 zh 正文（含入口域名）", () => {
		const page = makePage({ value: "zh" });
		expect(page.mail.getAttribute("href")).toBe(mailto(ZH_BODY));
		expect(page.preview.textContent).toContain("注册入口：https://histopilot.cn");
	});

	it("hp-lang-change → en：href 切到 data-mailto-en，预览解码为 en 正文", () => {
		// 服务端默认渲染 zh（模板默认）；页面以 en 语言打开 → 初始即为 en 版
		const pageEn = makePage({ value: "en" });
		expect(pageEn.mail.getAttribute("href")).toBe(mailto(EN_BODY));
		expect(pageEn.preview.textContent).toContain(
			"Registration entry: https://histopilot.cn");
		// zh 页面运行中切换语言（i18n.js 广播 hp-lang-change）：href 与预览同步
		const holder = { value: "zh" };
		const page = makePage(holder);
		expect(page.mail.getAttribute("href")).toBe(mailto(ZH_BODY));
		holder.value = "en";
		page.emitDoc("hp-lang-change");
		expect(page.mail.getAttribute("href")).toBe(mailto(EN_BODY));
		expect(page.preview.textContent).toContain(
			"Registration entry: https://histopilot.cn");
	});

	it("复制按钮：clipboard.writeText 写入作者邮箱，按钮临时显示「已复制」后恢复", async () => {
		const page = makePage({ value: "zh" });
		page.copy.emit("click", { currentTarget: page.copy, target: page.copy });
		await Promise.resolve(); // writeText promise → done 微任务
		expect(page.clipboardCalls).toEqual([["solarise94@gmail.com"]]);
		expect(page.copy.textContent).toBe("已复制");
		expect(page.copy.classList.contains("btn-copied")).toBe(true);
		// 2s 后恢复原文案
		const t = page.timers.shift();
		t!.fn();
		expect(page.copy.textContent).toBe("复制作者邮箱");
		expect(page.copy.classList.contains("btn-copied")).toBe(false);
	});

	it("无 clipboard API：回退为选中可见邮箱文本 + execCommand('copy')", () => {
		const timers: Array<{ fn: () => void }> = [];
		const execCalls: string[] = [];
		const body = new FakeEl("body");
		const mail = new FakeEl("a", "reghelp-mail");
		mail.setAttribute("href", mailto(ZH_BODY));
		const copy = new FakeEl("button", "reghelp-copy");
		copy.setAttribute("data-author-email", "solarise94@gmail.com");
		const emailText = new FakeEl("p", "reghelp-email-text");
		emailText.textContent = "solarise94@gmail.com";
		const preview = new FakeEl("pre", "reghelp-template");
		[mail, copy, emailText, preview].forEach((el) => body.appendChild(el));
		let rangeSelected = 0;
		const docObj = {
			body,
			createElement: (tag: string) => new FakeEl(tag),
			createRange: () => ({ selectNodeContents: () => { rangeSelected += 1; } }),
			execCommand: (cmd: string) => {
				execCalls.push(cmd);
				return true;
			},
			getElementById: (id: string) =>
				([mail, copy, emailText, preview].find((el) => el.id === id)) ?? null,
			addEventListener: () => {},
		};
		const winObj = {
			HP_I18N: { getLang: () => "zh", t: (k: string) => k },
			setTimeout: (fn: () => void) => {
				timers.push({ fn });
				return timers.length;
			},
			clearTimeout: () => {},
			addEventListener: () => {},
			navigator: {}, // 无 clipboard API（非安全上下文等）
			getSelection: () => ({ removeAllRanges: () => {}, addRange: () => {} }),
		};
		new Function("document", "window", "navigator", src)(
			docObj, winObj, winObj.navigator);
		copy.emit("click", { currentTarget: copy, target: copy });
		expect(execCalls).toEqual(["copy"]);
		expect(rangeSelected).toBe(1);
	});
});
