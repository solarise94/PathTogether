/**
 * §5.4 polygon 完整性（R3）：主站 app.js 与分享页 share.js 都必须具备
 * polygon 的画布呈现、命中、定位、编辑、序列化路径——且 polygon 保留原类型
 * （绝不伪装成 freehand 保存）。源码契约风格同 toolbar-draw-roi.test.ts。
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

for (const [label, src] of [["app.js", appSrc], ["share.js", shareSrc]] as const) {
	describe(`§5.4 polygon 完整性（${label}）`, () => {
		it("drawAnnoItem 有独立 polygon 分支（不并入 freehand），走 drawPolygon", () => {
			const fn = extractFn(src, "drawAnnoItem");
			expect(fn).toMatch(/typ === "polygon"/);
			expect(fn).toMatch(/drawPolygon\(it\.points/);
			// polygon 与 freehand 分支互不吞并：polygon 分支不调用 drawFreehand
			const polyBranch = fn.slice(fn.indexOf('typ === "polygon"'),
				fn.indexOf('typ === "freehand"'));
			expect(polyBranch).not.toMatch(/drawFreehand/);
		});

		it("drawPolygon 存在（闭合多边形呈现）", () => {
			const fn = extractFn(src, "drawPolygon");
			expect(fn).toMatch(/closePath/);
			expect(fn).toMatch(/points\.length < 3/);
		});

		it("hitAnno 命中 polygon（内部 + 边线）", () => {
			const fn = extractFn(src, "hitAnno");
			expect(fn).toMatch(/typ === "polygon"/);
			expect(fn).toMatch(/pointInPolygon/);
		});

		it("snapshotGeom/buildEditGeom 序列化 polygon points（保留原类型，不降级 freehand）", () => {
			const snap = extractFn(src, "snapshotGeom");
			expect(snap).toMatch(/typ === "polygon"/);
			const build = extractFn(src, "buildEditGeom");
			expect(build).toMatch(/typ === "polygon"/);
			// 序列化路径没有任何 polygon→freehand 改写
			expect(build).not.toMatch(/["']freehand["']\s*:/);
		});

		it("editHandles 支持 polygon（普通包围框柄 + 编辑轮廓点顶点柄）", () => {
			const fn = extractFn(src, "editHandles");
			expect(fn).toMatch(/typ === "polygon"/);
			expect(fn).toMatch(/polygonVertexEdit/);
			expect(fn).toMatch(/insertAfter/); // 边中点「加顶点」柄
		});

		it("applyEditDrag：polygon 整体移动 / 顶点拖动 / 加顶点 / 包围框缩放", () => {
			const fn = extractFn(src, "applyEditDrag");
			expect(fn).toMatch(/typ === "polygon"/);
			expect(fn).toMatch(/charAt\(0\) === "v"/);  // 拖顶点
			expect(fn).toMatch(/charAt\(0\) === "n"/);  // 加顶点
			expect(fn).toMatch(/"move"/);               // 整体移动
		});

		it("cancelEditDragRestore 恢复 polygon points", () => {
			const fn = extractFn(src, "cancelEditDragRestore");
			expect(fn).toMatch(/typ === "polygon"/);
		});

		it("annoAnchor / 定位包围盒覆盖 polygon", () => {
			expect(extractFn(src, "annoAnchor")).toMatch(/typ === "polygon"|typ === "freehand" \|\| typ === "polygon"/);
		});

		it("完成轮廓编辑校验非自交（polygonSelfIntersects），不静默修正", () => {
			const fn = extractFn(src, "finishPolygonVertexEdit");
			expect(fn).toMatch(/polygonSelfIntersects/);
			expect(fn).toMatch(/selfintersects/);
			expect(fn).toMatch(/minpoints/);
		});
	});
}

describe("§5.4 polygon 定位与面板（app.js）", () => {
	it("jumpToAnno / annoItemBbox 覆盖 polygon", () => {
		expect(extractFn(appSrc, "jumpToAnno")).toMatch(/typ === "polygon"/);
		expect(extractFn(appSrc, "annoItemBbox")).toMatch(/typ === "polygon"/);
	});

	it("标注面板 polygon 图标与点数摘要", () => {
		expect(appSrc).toMatch(/"polygon" \? "⬠"/);
	});

	it("resolveAnnoIndex 兼容 polygon 回退匹配", () => {
		expect(extractFn(appSrc, "resolveAnnoIndex")).toMatch(/rr\.type === "polygon"/);
	});
});

describe("§5.4 polygon 定位与面板（share.js）", () => {
	it("jumpToRoi（doJump）/ 面板图标覆盖 polygon", () => {
		expect(extractFn(shareSrc, "doJump")).toMatch(/typ === "polygon"/);
		expect(shareSrc).toMatch(/"polygon" \? "⬠"/);
	});

	it("分享端 polygon 编辑完成 = 显式保存提示（不自动 PATCH，维持分享端契约）", () => {
		const fn = extractFn(shareSrc, "finishPolygonVertexEdit");
		expect(fn).toMatch(/edit\.need\.save/);
		expect(fn).not.toMatch(/apiFetch|fetch\(/);
	});
});

describe("§5.4 共享几何 helper 加载顺序（模板契约）", () => {
	it("index.html 在 app.js 之前加载 anno-geometry.js", () => {
		const tpl = readFileSync(resolve(here, "../../templates/index.html"), "utf8");
		const geo = tpl.indexOf("anno-geometry.js");
		const app = tpl.indexOf("static/app.js");
		expect(geo).toBeGreaterThan(-1);
		expect(app).toBeGreaterThan(geo);
	});

	it("share.html 在 share.js 之前加载 anno-geometry.js", () => {
		const tpl = readFileSync(resolve(here, "../../templates/share.html"), "utf8");
		const geo = tpl.indexOf("anno-geometry.js");
		const share = tpl.indexOf("static/share.js");
		expect(geo).toBeGreaterThan(-1);
		expect(share).toBeGreaterThan(geo);
	});

	it("两个页面文件都改了 cache-busting 版本（模板 v= 参数）", () => {
		expect(readFileSync(resolve(here, "../../templates/index.html"), "utf8"))
			.toMatch(/anno-geometry\.js\?v=/);
		expect(readFileSync(resolve(here, "../../templates/share.html"), "utf8"))
			.toMatch(/anno-geometry\.js\?v=/);
	});
});
