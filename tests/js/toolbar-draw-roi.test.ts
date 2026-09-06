/**
 * 工具栏状态机源码契约（review-2026-09-06 P1-7 / P2-3）：
 * 箭头/描图不得改写 showAnno；exitRoi 必须收起 roi-settings。
 * app.js / share.js 为页面 IIFE，这里锁定生产源码契约，避免再引入隐式可见性。
 */
import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";

const here = dirname(fileURLToPath(import.meta.url));
const appSrc = readFileSync(resolve(here, "../../static/app.js"), "utf8");
const shareSrc = readFileSync(resolve(here, "../../static/share.js"), "utf8");

function extractFn(src: string, name: string): string {
	const re = new RegExp(`function ${name}\\([^)]*\\) \\{`);
	const m = re.exec(src);
	expect(m, `${name} 必须存在`).toBeTruthy();
	const start = m!.index;
	let i = src.indexOf("{", start);
	let depth = 0;
	for (; i < src.length; i++) {
		if (src[i] === "{") depth++;
		else if (src[i] === "}") {
			depth--;
			if (depth === 0) return src.slice(start, i + 1);
		}
	}
	throw new Error(`未能截取 ${name}`);
}

describe("矩形 / 绘制工具状态机契约", () => {
	it("exitRoi 关闭 roi-settings 并复位 aria-expanded", () => {
		const fn = extractFn(appSrc, "exitRoi");
		expect(fn).toMatch(/els\.roiSettings\.hidden = true/);
		expect(fn).toMatch(/aria-expanded["'], ["']false["']/);
	});

	it("enterDrawMode（主查看器）不写 showAnno", () => {
		const fn = extractFn(appSrc, "enterDrawMode");
		expect(fn).not.toMatch(/showAnno\s*=/);
		expect(fn).toMatch(/exitRoi\(\)/);
	});

	it("enterDrawMode（分享页）不写 showAnno", () => {
		const fn = extractFn(shareSrc, "enterDrawMode");
		expect(fn).not.toMatch(/showAnno\s*=/);
	});

	it("toggleAnnoAll 仍是 showAnno 的合法写入点", () => {
		const fn = extractFn(appSrc, "toggleAnnoAll");
		expect(fn).toMatch(/state\.showAnno\s*=/);
	});
});
