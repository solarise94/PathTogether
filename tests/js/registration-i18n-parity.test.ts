/**
 * 注册防刷相关页面的 i18n 键位一致性（2026-10-08 设计 §5/§7）。
 * 锁定（不加载 DOM，纯源码检查，风格同 tests/test_phase1_auth_ui.py 的键位守卫）：
 *   - templates/_login_dialog.html、registration_help.html、verify_email.html
 *     中出现的每个 data-i18n 静态键在 static/i18n.js 的 zh 与 en 词典都存在；
 *   - 模板默认（zh）文本与 zh 词典值一致（i18n.js 未加载/缺键时页面显示的
 *     就是模板默认——两者不一致会导致 zh 用户看到 en 文案或旧文案）；
 *   - register-turnstile.js / registration-help.js 里 t("key", fallback) 用到
 *     的动态键同样 zh/en 成对存在；
 *   - reghelp.<reason>.title 覆盖服务端 REGISTRATION_HELP_REASONS 全部 8 个
 *     固定原因词（动态键，无法在模板里静态断言）。
 */
import { describe, expect, it } from "vitest";
import { readFileSync } from "node:fs";
import { dirname, resolve, join } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const root = resolve(here, "../..");

const i18nSrc = readFileSync(join(root, "static/i18n.js"), "utf8");
const dialogSrc = readFileSync(join(root, "templates/_login_dialog.html"), "utf8");
const helpSrc = readFileSync(join(root, "templates/registration_help.html"), "utf8");
const verifySrc = readFileSync(join(root, "templates/verify_email.html"), "utf8");
const loaderSrc = readFileSync(join(root, "static/register-turnstile.js"), "utf8");
const helpJsSrc = readFileSync(join(root, "static/registration-help.js"), "utf8");

function dictBlock(lang: "zh" | "en"): string {
	if (lang === "zh") {
		return i18nSrc.slice(i18nSrc.indexOf("zh: {"), i18nSrc.indexOf("en: {"));
	}
	return i18nSrc.slice(i18nSrc.indexOf("en: {"));
}

function dictEntries(lang: "zh" | "en"): Map<string, string> {
	const out = new Map<string, string>();
	const re = /"([^"\\]+)":\s*"((?:[^"\\]|\\.)*)"/g;
	let m: RegExpExecArray | null;
	while ((m = re.exec(dictBlock(lang))) !== null) {
		if (!out.has(m[1])) out.set(m[1], m[2]);
	}
	return out;
}

/** 模板里的 data-i18n 键 → 模板默认文本（紧跟 > 的首个文本段；动态键含 {{） */
function templateKeys(src: string): Map<string, string> {
	const out = new Map<string, string>();
	const re = /data-i18n="([^"]+)"\s*>([^<]+)</g;
	let m: RegExpExecArray | null;
	while ((m = re.exec(src)) !== null) {
		if (!out.has(m[1])) out.set(m[1], m[2].trim());
	}
	return out;
}

/** JS 里 t("key", …) 引用的键 */
function jsKeys(src: string): string[] {
	const out = new Set<string>();
	const re = /\bt\(\s*'([A-Za-z0-9_.-]+)'/g;
	let m: RegExpExecArray | null;
	while ((m = re.exec(src)) !== null) out.add(m[1]);
	return Array.from(out);
}

const REASONS = [
	"general", "challenge", "cooldown", "limit", "unavailable",
	"link_invalid", "link_expired", "submit_error",
];

describe("注册防刷页面 i18n 键位一致性", () => {
	it("_login_dialog.html：每个 data-i18n 键 zh/en 都存在，且模板默认=zh 词典值", () => {
		const zh = dictEntries("zh");
		const en = dictEntries("en");
		for (const [key, templateDefault] of templateKeys(dialogSrc)) {
			expect(zh.has(key), `i18n.js zh 缺键：${key}`).toBe(true);
			expect(en.has(key), `i18n.js en 缺键：${key}`).toBe(true);
			const zhVal = (zh.get(key) ?? "").replace(/\\"/g, "\"");
			expect(zhVal, `模板默认与 zh 词典不一致：${key}`).toBe(templateDefault);
		}
	});

	it("registration_help.html：data-i18n 键 zh/en 存在、默认=zh 值；8 个原因标题键齐备", () => {
		const zh = dictEntries("zh");
		const en = dictEntries("en");
		const keys = templateKeys(helpSrc);
		expect(keys.has("reghelp.{{ reason }}.title")).toBe(true); // 动态键存在
		for (const [key, templateDefault] of keys) {
			if (key.includes("{{") || templateDefault.includes("{{")) {
				continue; // 动态键（Jinja 渲染）单独断言
			}
			expect(zh.has(key), `i18n.js zh 缺键：${key}`).toBe(true);
			expect(en.has(key), `i18n.js en 缺键：${key}`).toBe(true);
			const zhVal = (zh.get(key) ?? "").replace(/\\"/g, "\"");
			expect(zhVal, `模板默认与 zh 词典不一致：${key}`).toBe(templateDefault);
		}
		for (const reason of REASONS) {
			const key = `reghelp.${reason}.title`;
			expect(zh.has(key), `i18n.js zh 缺原因标题：${key}`).toBe(true);
			expect(en.has(key), `i18n.js en 缺原因标题：${key}`).toBe(true);
		}
	});

	it("verify_email.html 错误态：data-i18n 键 zh/en 都存在，且模板默认=zh 词典值", () => {
		const zh = dictEntries("zh");
		const en = dictEntries("en");
		for (const [key, templateDefault] of templateKeys(verifySrc)) {
			if (!key.startsWith("verify.error.")) continue; // 其余键由既有测试守卫
			expect(zh.has(key), `i18n.js zh 缺键：${key}`).toBe(true);
			expect(en.has(key), `i18n.js en 缺键：${key}`).toBe(true);
			const zhVal = (zh.get(key) ?? "").replace(/\\"/g, "\"");
			expect(zhVal, `模板默认与 zh 词典不一致：${key}`).toBe(templateDefault);
		}
	});

	it("register-turnstile.js / registration-help.js 的 t() 动态键 zh/en 成对存在", () => {
		const zh = dictEntries("zh");
		const en = dictEntries("en");
		for (const src of [loaderSrc, helpJsSrc]) {
			for (const key of jsKeys(src)) {
				expect(zh.has(key), `i18n.js zh 缺 JS 键：${key}`).toBe(true);
				expect(en.has(key), `i18n.js en 缺 JS 键：${key}`).toBe(true);
			}
		}
	});

	it("Turnstile/状态文案绝不出现「机器人/robot」措辞（§7 中性口径）", () => {
		const zh = dictEntries("zh");
		const en = dictEntries("en");
		for (const [lang, dict] of [["zh", zh], ["en", en]] as const) {
			for (const key of Object.keys(Object.fromEntries(dict))) {
				if (!key.startsWith("register.turnstile.") &&
					!key.startsWith("register.state.")) continue;
				const val = (dict.get(key) ?? "").toLowerCase();
				expect(val.includes("机器人") || val.includes("robot"),
					`${lang} ${key} 含「机器人/robot」措辞`).toBe(false);
			}
		}
	});
});
