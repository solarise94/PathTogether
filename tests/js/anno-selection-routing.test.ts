/**
 * §6.1/§6.2 选择语义、视觉与手势路由（R4/R5）：
 *   - 选中 = 单一蓝色轮廓，不再蓝黄双描边 + 常驻黄角点；
 *   - 选中不再隐藏其它标记（「只看这条」收进 ⋯）；
 *   - viewer 容器捕获阶段命中路由：只在确认对象编辑时消费事件，其余交给
 *     OSD（不给透明画布开常驻 pointer-events:auto）；
 *   - 就近工具条四种形态、出视窗翻转、命中容差（桌面≈16px/触摸≈28px）；
 *   - 旋转/镜像下矩形四顶点投影。
 */
import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";

const here = dirname(fileURLToPath(import.meta.url));
const appSrc = readFileSync(resolve(here, "../../static/app.js"), "utf8");
const shareSrc = readFileSync(resolve(here, "../../static/share.js"), "utf8");
const appCss = readFileSync(resolve(here, "../../static/style.css"), "utf8");
const shareCss = readFileSync(resolve(here, "../../static/share.css"), "utf8");

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

describe("§6.1 统一选中视觉（app.js）", () => {
	it("drawAnnoItem 不再用黄色双描边与常驻黄角点", () => {
		const fn = extractFn(appSrc, "drawAnnoItem");
		expect(fn).not.toMatch(/#FFD700/);          // 旧黄主描边
		expect(fn).not.toMatch(/fillStyle = "#FFD700"/);
		expect(fn).not.toMatch(/lineWidth = 6/);     // 旧蓝外框加粗层
		expect(fn).toMatch(/ANNO_SELECTED_STROKE/);  // 单一蓝色轮廓
	});

	it("矩形按投影四边形绘制（rectScreenQuad → AG.rectQuad），旋转/镜像正确", () => {
		const fn = extractFn(appSrc, "drawRectQuad");
		expect(fn).toMatch(/rectScreenQuad/);
		expect(fn).toMatch(/closePath/); // 四边形填充/描边路径
		expect(extractFn(appSrc, "rectScreenQuad")).toMatch(/AG\.rectQuad/);
	});

	it("hitAnno 的矩形命中用投影四边形（pointInQuad），不再屏幕 AABB", () => {
		const fn = extractFn(appSrc, "hitAnno");
		expect(fn).toMatch(/pointInQuad/);
		expect(fn).not.toMatch(/sx >= x - 6/);
	});

	it("手柄命中容差：桌面 8px 半径（≈16px）/ 触摸 14px（≈28px）", () => {
		const fn = extractFn(appSrc, "handleHitTolerance");
		expect(fn).toMatch(/pointer: coarse/);
		expect(fn).toMatch(/14/);
		expect(fn).toMatch(/8/);
	});
});

describe("§6.1 统一选中视觉（share.js）", () => {
	it("drawAnnoItem 去掉黄框/黄角点，改单一蓝轮廓 + 投影四边形", () => {
		const fn = extractFn(shareSrc, "drawAnnoItem");
		expect(fn).not.toMatch(/#FFD700/);
		expect(fn).not.toMatch(/lineWidth = 6/);
		expect(fn).toMatch(/drawRectQuad/);
		expect(extractFn(shareSrc, "rectScreenQuad")).toMatch(/AG\.rectQuad/);
	});

	it("锁定/只读对象不画编辑手柄（drawEditHandles 调用点过 canDragShareItem）", () => {
		expect(shareSrc).toMatch(
			/editItem && state\.editing && canDragShareItem\(editItem\) && state\.showAnno/);
	});
});

describe("§6.1 选中不再隐藏其它标记", () => {
	it("selectEditItem 不再设置 focusAnno（app.js）", () => {
		const fn = extractFn(appSrc, "selectEditItem");
		expect(fn).not.toMatch(/focusAnno\s*=/);
	});

	it("clearEditItem 不再触碰 focusAnno（app.js，恢复显示全部的职责归 ⋯/👁）", () => {
		const fn = extractFn(appSrc, "clearEditItem");
		expect(fn).not.toMatch(/focusAnno\s*=/);
	});

	it("jumpToAnno 定位不再隐藏其它标记（app.js）", () => {
		const fn = extractFn(appSrc, "jumpToAnno");
		expect(fn).not.toMatch(/focusAnno\s*=/);
	});

	it("selectEditItem / jumpToRoi 不再设置 focusAnno（share.js）", () => {
		expect(extractFn(shareSrc, "selectEditItem")).not.toMatch(/focusAnno\s*=/);
		expect(extractFn(shareSrc, "jumpToRoi")).not.toMatch(/focusAnno\s*=/);
	});

	it("「只看此标记」在 ⋯ 菜单（app.js / share.js）", () => {
		expect(appSrc).toMatch(/anno\.solo/);
		expect(shareSrc).toMatch(/anno\.solo/);
		// toggleAnnoAll 仍是 focus 的合法清理点
		expect(extractFn(appSrc, "toggleAnnoAll")).toMatch(/focusAnno/);
	});
});

describe("§6.2 捕获阶段手势路由", () => {
	for (const [label, src] of [["app.js", appSrc], ["share.js", shareSrc]] as const) {
		it(`routeObjectPointerDown：容器捕获绑定 + 消费/放行路由（${label}）`, () => {
			// 绑定在 viewer 容器捕获阶段（capture: true），不是给画布开 pointer-events
			expect(src).toMatch(
				/addEventListener\("pointerdown", routeObjectPointerDown, true\)/);
			expect(src).toMatch(
				/addEventListener\("pointerup", routeObjectPointerUp, true\)/);
			// 透明画布不允诺常驻拦截：anno-canvas 的 pointer-events 只在 drawing 态开启
			const css = label === "app.js" ? appCss : shareCss;
			expect(css).toMatch(/\.anno-canvas\.drawing \{\s*pointer-events: auto/);
		});

		it(`路由优先级：UI 工具条 > Space > 显式绘制 > 已选中编辑 > 选择/平移（${label}）`, () => {
			const fn = extractFn(src, "routeObjectPointerDown");
			const idxUi = fn.indexOf("isViewerSurfaceTarget");
			const idxSpace = fn.indexOf("viewerSpacePan");
			const idxDraw = fn.indexOf("state.drawMode");
			const idxHandle = fn.indexOf("hitHandle");
			const idxHit = fn.indexOf("hitAnno");
			expect(idxUi).toBeGreaterThan(-1);
			expect(idxSpace).toBeGreaterThan(idxUi);
			expect(idxDraw).toBeGreaterThan(idxSpace);
			expect(idxHandle).toBeGreaterThan(idxDraw);
			expect(idxHit).toBeGreaterThan(idxDraw);
		});

		it(`未选中的标记：按下不消费（拖动=平移），松手极小位移才选择（${label}）`, () => {
			const fn = extractFn(src, "routeObjectPointerDown");
			// 命中未选中对象时只记录 click 候选，不 preventDefault
			const tail = fn.slice(fn.indexOf("pendingAnnoSelect ="));
			expect(tail).toMatch(/hit:\s*hit/);
			expect(tail).not.toMatch(/preventDefault/);
			const up = extractFn(src, "routeObjectPointerUp");
			expect(up).toMatch(/SLOP_PX/);
			expect(up).toMatch(/selectEditItem/);
			expect(up).toMatch(/clearEditItem/);
		});

		it(`已选中可编辑对象本体：立即消费并整体移动（${label}）`, () => {
			const fn = extractFn(src, "routeObjectPointerDown");
			const moveBranch = fn.slice(fn.indexOf('startEditDrag(e, hit, "move"') - 260,
				fn.indexOf('startEditDrag(e, hit, "move"'));
			expect(moveBranch).toMatch(/consumePointer\(e\)/);
			expect(moveBranch).toMatch(/canDragItem|canDragShareItem/);
		});

		it(`叠加 UI 层不参与路由（工具条/保存状态优先级最高）（${label}）`, () => {
			expect(extractFn(src, "isViewerSurfaceTarget"))
				.toMatch(/anno-ui-layer/);
			expect(src).toMatch(/className = "anno-ui-layer/);
		});
	}
});

describe("§6.1 就近工具条", () => {
	it("四种形态：临时框保存/锁定、已存标记、AI 待复核接受、锁定解锁（app.js）", () => {
		const fn = extractFn(appSrc, "renderAnnoToolbar");
		expect(fn).toMatch(/anno\.toolbar\.save/);         // 临时框：保存为标记
		expect(fn).toMatch(/anno\.toolbar\.note/);         // 编辑备注
		expect(fn).toMatch(/anno\.review\.accept/);        // AI 待复核：接受
		expect(fn).toMatch(/anno\.lock\.unlock/);          // 锁定对象：解锁位置
		expect(fn).toMatch(/anno\.lock\.lock/);            // 锁定位置
		expect(fn).toMatch(/anno\.lock\.readonly/);        // 只读说明徽章
		expect(fn).toMatch(/anno\.toolbar\.more/);         // ⋯
	});

	it("⋯ 收纳低频动作；锁定对象的删除入口禁用并说明（app.js）", () => {
		const fn = extractFn(appSrc, "openAnnoToolbarMenu");
		expect(fn).toMatch(/anno\.menu\.locate/);
		expect(fn).toMatch(/anno\.solo/);
		expect(fn).toMatch(/anno\.polygon\.editpoints/);
		expect(fn).toMatch(/anno\.review\.reject/);
		expect(fn).toMatch(/delDisabled/);
		expect(fn).toMatch(/anno\.lock\.delete\.denied/);
	});

	it("分享页工具条：可编辑两按钮 + 锁 + ⋯；只读徽章（share.js）", () => {
		const fn = extractFn(shareSrc, "renderAnnoToolbar");
		expect(fn).toMatch(/anno\.toolbar\.save/);   // 临时框形态
		expect(fn).toMatch(/anno\.toolbar\.note/);
		expect(fn).toMatch(/anno\.lock\.unlock/);
		expect(fn).toMatch(/anno\.lock\.readonly/);
	});

	it("工具条出视窗翻转：上方放不下翻到下方/内部（app.js + share.js）", () => {
		for (const src of [appSrc, shareSrc]) {
			const fn = extractFn(src, "updateAnnoToolbarPos");
			expect(fn).toMatch(/bb\.y - th - 8/);
			expect(fn).toMatch(/bb\.y \+ bb\.h \+ 8/);          // 翻到下方
			expect(fn).toMatch(/clamp\(/);                        // 横向/内部钳制
		}
	});

	it("工具条按钮尺寸：桌面 ≥38px、触摸 ≥44px（style.css + share.css）", () => {
		for (const css of [appCss, shareCss]) {
			expect(css).toMatch(/min-height: 38px/);
			expect(css).toMatch(/pointer: coarse/);
			expect(css).toMatch(/min-height: 44px/);
		}
	});
});

describe("§6.2 polygon 编辑轮廓点入口在 ⋯", () => {
	it("app.js：togglePolygonVertexEdit / finishPolygonVertexEdit 存在并被 ⋯ 引用", () => {
		expect(extractFn(appSrc, "togglePolygonVertexEdit")).toMatch(/polygonVertexEdit/);
		expect(extractFn(appSrc, "routeObjectDblClick")).toMatch(/splice/); // 双击删点
	});

	it("双击删点保持 ≥3 个顶点", () => {
		const fn = extractFn(appSrc, "routeObjectDblClick");
		expect(fn).toMatch(/length <= 3/);
	});
});

describe("几何工具 helper 引用（anno-geometry.js）", () => {
	it("两页都挂 window.HP_AnnoGeometry 兜底（脚本缺失时不致崩溃）", () => {
		expect(appSrc).toMatch(/var AG = window\.HP_AnnoGeometry \|\|/);
		expect(shareSrc).toMatch(/var AG = window\.HP_AnnoGeometry \|\|/);
	});
});
