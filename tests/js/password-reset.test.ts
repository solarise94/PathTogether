/**
 * 找回密码申请页的语言同步（static/password-reset.js，0095）。
 *
 * 只锁定一件事：表单隐藏域 form_locale 跟随当前界面语言（zh|en），
 * 服务端按该值冻结重置邮件正文（与注册表单同口径）。
 */
import { describe, expect, it } from "vitest";
import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const src = readFileSync(resolve(here, "../../static/password-reset.js"), "utf8");

function makeDoc(lang: "zh" | "en") {
	const inputs = [
		{ name: "form_locale", value: "zh" },
		{ name: "form_locale", value: "zh" },
	];
	const handlers: Record<string, Array<() => void>> = {};
	const doc = {
		readyState: "complete",
		addEventListener(type: string, fn: () => void) {
			(handlers[type] = handlers[type] || []).push(fn);
		},
		querySelectorAll(sel: string) {
			return sel === 'input[name="form_locale"]' ? inputs : [];
		},
	};
	const win = { HP_I18N: { getLang: () => lang } };
	new Function("document", "window", src)(doc, win);
	return {
		inputs,
		emit(type: string) {
			(handlers[type] || []).forEach((fn) => fn());
		},
	};
}

describe("password-reset.js form_locale 同步", () => {
	it("加载时按当前界面语言写入 form_locale", () => {
		const ctx = makeDoc("en");
		expect(ctx.inputs.map((i) => i.value)).toEqual(["en", "en"]);
	});

	it("语言切换事件后重新同步", () => {
		const ctx = makeDoc("zh");
		ctx.inputs.forEach((i) => { i.value = "stale"; });
		ctx.emit("hp-lang-change");
		expect(ctx.inputs.map((i) => i.value)).toEqual(["zh", "zh"]);
	});
});
