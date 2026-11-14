/**
 * 0094 匿名本地转换完成计数：转换器页接线（源码级）。
 *
 * tools-slides.js 是依赖真实 DOM/OPFS 的 ES module，页面既有测试都采用
 * 「读源码锁定接线与文案」的做法；真实交互由 Playwright 复核截图覆盖。
 *
 * 覆盖：
 *   - 页脚统计行位置/默认 hidden + zh/en 文案与占位符；
 *   - 成功转换后的 fire-and-forget beacon（两条成功路径各一次，
 *     source='converter'，keepalive，无 Cookie，失败吞掉、成功后刷新汇总）；
 *   - 加载时读 summary、失败/0 保持隐藏、语言切换重渲染；
 *   - 静态资源 cache-buster 与 i18n 文案同步。
 */
import { describe, expect, it } from "vitest";
import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const root = resolve(here, "../..");
const read = (rel: string) => readFileSync(resolve(root, rel), "utf8");
const pageSrc = read("static/tools/tools-slides.js");
const shellSrc = read("templates/tools_slides.html");
const cssSrc = read("static/tools/tools-slides.css");
const i18nSrc = read("static/i18n.js");
const inlineSrc = read("static/upload/inline-convert.js");
const appSrc = read("static/app.js");
const indexSrc = read("templates/index.html");

function dictBlock(lang: "zh" | "en"): string {
	if (lang === "zh") {
		return i18nSrc.slice(i18nSrc.indexOf("zh: {"), i18nSrc.indexOf("en: {"));
	}
	return i18nSrc.slice(i18nSrc.indexOf("en: {"), i18nSrc.indexOf("\n  };", i18nSrc.indexOf("en: {")));
}
const dicts = { zh: dictBlock("zh"), en: dictBlock("en") } as const;

function val(lang: "zh" | "en", key: string): string | null {
	const m = dicts[lang].match(
		new RegExp(`"${key.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")}"\\s*:\\s*"((?:[^"\\\\]|\\\\.)*)"`));
	return m ? m[1] : null;
}

describe("converter page summary line", () => {
	it("footer holds one hidden line between the support block and footer note", () => {
		expect(shellSrc).toMatch(
			/<p id="conversion-count" class="footer-stat" hidden><\/p>[\s\S]*?<p data-i18n="tools.footer">/);
		// 位置：支持区之后（页脚内、页脚说明之前）
		const support = shellSrc.indexOf('class="footer-support"');
		const line = shellSrc.indexOf('id="conversion-count"');
		const footer = shellSrc.indexOf('data-i18n="tools.footer"');
		expect(support).toBeGreaterThan(0);
		expect(line).toBeGreaterThan(support);
		expect(footer).toBeGreaterThan(line);
		expect(cssSrc).toContain(".footer-stat");
	});

	it("zh/en copy uses the requested wording and placeholders", () => {
		expect(val("zh", "tools.stats.completed"))
			.toBe("自 {since} 起，已在浏览器本地完成 {count} 次转换");
		expect(val("en", "tools.stats.completed"))
			.toBe("{count} conversions completed locally in browsers since {since}");
	});

	it("summary fetch is same-origin, no-cookie, and hides on failure/zero", () => {
		expect(pageSrc).toContain(
			"fetch('/api/tools/conversions/summary'");
		expect(pageSrc).toMatch(
			/fetch\('\/api\/tools\/conversions\/summary'[\s\S]*?credentials: 'omit'/);
		const loader = pageSrc.slice(pageSrc.indexOf("async function loadConversionSummary"));
		expect(loader).toContain("if (!resp || !resp.ok) return;");
		expect(loader).toMatch(/total <= 0/);
		expect(loader).toMatch(/\^\\d\{4\}-\\d\{2\}-\\d\{2\}\$/);
		// 失败只保持隐藏：不写错误文案、不抛给页面
		expect(loader).toMatch(/catch \(e\) \{ \/\* 汇总不可用：保持隐藏 \*\/ \}/);
	});

	it("renders with thousands separators and re-renders on language switch", () => {
		const render = pageSrc.slice(pageSrc.indexOf("function renderConversionCount"));
		expect(render).toContain("toLocaleString(lang)");
		expect(render).toContain("el.hidden = false;");
		expect(pageSrc).toMatch(
			/function rerenderForLang\(\)[\s\S]*?renderConversionCount\(\);/);
		expect(pageSrc).toContain("void loadConversionSummary();");
	});

	it("static assets are cache-busted for this change", () => {
		expect(shellSrc).toContain("/static/tools/tools-slides.js?v=20261114a");
		expect(shellSrc).toContain("/static/tools/tools-slides.css?v=20261110a");
		expect(shellSrc).toContain("/static/i18n.js?v=20261110b");
	});
});

describe("converter beacon after a successful conversion", () => {
	it("posts source='converter' with keepalive and no credentials", () => {
		expect(pageSrc).toContain("fetch('/api/tools/conversions'");
		const fn = pageSrc.slice(pageSrc.indexOf("function reportCompletedConversion"));
		expect(fn).toContain("method: 'POST'");
		expect(fn).toContain("keepalive: true");
		expect(fn).toContain("credentials: 'omit'");
		expect(fn).toContain("source: 'converter'");
		expect(fn).toContain("if (resp && resp.ok) void loadConversionSummary();");
		expect(fn).toMatch(/pending\.then\([\s\S]*?\}, \(\) => \{\}\)/);
	});

	it("fires once on each successful completion path (driveConversion + job list)", () => {
		// 定义 1 次 + 两条 done 路径各 1 次
		const calls = pageSrc.match(/reportCompletedConversion\(/g) || [];
		expect(calls.length).toBe(3);
		expect(pageSrc).toMatch(
			/setRunJob\(jobName, 'done'\);[\s\S]{0,300}?reportCompletedConversion\(conversionInputFormat\(/);
		expect(pageSrc).toMatch(
			/reportCompletedConversion\(conversionInputFormat\(\s*job\.source && job\.source\.name, !!thisPrep\)\)/);
		// 失败路径没有紧随其后的计数调用
		expect(pageSrc).not.toMatch(
			/setRunJob\(jobName, 'failed'\)[\s\S]{0,500}reportCompletedConversion/);
	});

	it("beacon carries only the normalized format token (no file name/size)", () => {
		const body = pageSrc.slice(
			pageSrc.indexOf("body: JSON.stringify({ source: 'converter'"),
			pageSrc.indexOf("body: JSON.stringify({ source: 'converter'") + 220);
		expect(body).toContain("format: String(format || '')");
		expect(body).not.toContain("name");
		expect(body).not.toContain("size");
	});
});

describe("workbench inline conversion beacon", () => {
	it("inline-convert.js owns the source='workbench' post and exports it", () => {
		expect(inlineSrc).toContain('fetch("/api/tools/conversions"');
		const fn = inlineSrc.slice(inlineSrc.indexOf("function reportConversion"));
		expect(fn).toContain('method: "POST"');
		expect(fn).toContain("keepalive: true");
		expect(fn).toContain('credentials: "omit"');
		expect(fn).toContain('source: "workbench"');
		expect(fn).toContain("format: String(format || \"\")");
		expect(fn).toContain("pending.catch(");
		expect(inlineSrc).toContain("reportConversion: reportConversion,");
	});

	it("counts once on the success path only (conversionCounted guard)", () => {
		const onConverted = inlineSrc.slice(
			inlineSrc.indexOf("function onConverted"),
			inlineSrc.indexOf("function failConvert"));
		expect(onConverted).toContain("if (countConversion && !e.conversionCounted)");
		expect(onConverted).toContain("countConversion(conversionFormatOf(e))");
		const fail = inlineSrc.slice(
			inlineSrc.indexOf("function failConvert"),
			inlineSrc.indexOf("function releaseConvertSlot"));
		expect(fail).not.toContain("countConversion(");
	});

	it("app.js wires the callback and index.html cache-busts the changed assets", () => {
		expect(appSrc).toContain(
			"reportConversion: window.HP_INLINE_CONVERT.reportConversion,");
		expect(indexSrc).toContain("/static/upload/inline-convert.js?v=20261110a");
		expect(indexSrc).toContain("/static/app.js?v=20261110b");
	});
});
