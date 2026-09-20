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

/**
 * 工单 D（§5）绘制交互 + 撤销——源码契约：
 * 自由矩形拖动完成自动保存、Enter 提交、pointercancel 取消、失败保留草稿、
 * 撤销/重做以语义操作为单位且带 expected_revision CAS。
 */
describe("工单 D：绘制交互与撤销契约（app.js）", () => {
  it("自由矩形拖动完成走保存（submitCurrentDraft），不再只点亮按钮", () => {
    const fn = extractFn(appSrc, "onRectCanvasPointerUp");
    expect(fn).toMatch(/submitCurrentDraft\(\)/);
    // 拖动（moved）完成后不得再要求点击「保存标记」：保存调用在 moved 分支
    const movedBranch = fn.slice(fn.indexOf("// 自由矩形拖动完成"));
    expect(movedBranch).toMatch(/submitCurrentDraft/);
  });

  it("onRectKeydown 处理 Enter（提交有效未提交草稿）", () => {
    const fn = extractFn(appSrc, "onRectKeydown");
    expect(fn).toMatch(/["']Enter["']/);
    expect(fn).toMatch(/submitCurrentDraft\(\)/);
    expect(fn).toMatch(/["']Escape["']/);
  });

  it("pointercancel 不得接到完成/保存路径（annoCanvas 与 roiBox）", () => {
    // annoCanvas：取消走 onAnnoPointerCancel，且该函数不得调 finishDraw/saveAnno
    expect(appSrc).toMatch(/addEventListener\("pointercancel", onAnnoPointerCancel\)/);
    expect(appSrc).not.toMatch(/addEventListener\("pointercancel", onAnnoPointerUp\)/);
    const cancelFn = extractFn(appSrc, "onAnnoPointerCancel");
    expect(cancelFn).not.toMatch(/finishDraw/);
    expect(cancelFn).not.toMatch(/saveAnno\b/);
    expect(cancelFn).not.toMatch(/submitCurrentDraft/);
    // roiBox 拖拽：取消恢复几何（startRoi），不按 pointerup 提交
    expect(appSrc).toMatch(
      /roiBox\.addEventListener\("pointercancel", onRoiPointerCancel\)/);
    const roiCancel = extractFn(appSrc, "onRoiPointerCancel");
    expect(roiCancel).toMatch(/startRoi/);
    expect(appSrc).not.toMatch(
      /roiBox\.addEventListener\("pointercancel", onRoiPointerUp\)/);
    // 矩形画布拖出：取消恢复拖前选区
    const rectCancel = extractFn(appSrc, "onRectCanvasPointerCancel");
    expect(rectCancel).toMatch(/startRoi/);
    expect(rectCancel).not.toMatch(/submitCurrentDraft/);
  });

  it("保存失败保留可重试草稿：catch 不退工具、不清几何", () => {
    const fn = extractFn(appSrc, "submitAnnotationDraft");
    const catchIdx = fn.lastIndexOf(".catch(function");
    expect(catchIdx).toBeGreaterThan(0);
    const catchBody = fn.slice(catchIdx);
    expect(catchBody).toMatch(/retryDraft\s*=/);
    expect(catchBody).not.toMatch(/exitDrawMode/);
    expect(catchBody).not.toMatch(/exitRoi/);
    expect(catchBody).toMatch(/setDrawUnsaved\(true\)/);
  });

  it("提交幂等且冻结切片：client_action_id + 保存中防重 + 切片切换丢回包", () => {
    const fn = extractFn(appSrc, "submitAnnotationDraft");
    expect(fn).toMatch(/drawPhase === "saving"/);
    expect(fn).toMatch(/client_action_id/);
    // 切片已切换：不把回包应用到新切片
    expect(fn).toMatch(/state\.slide\.name !== slideName/);
    // 成功后按 annotation_id 选中新标注
    expect(fn).toMatch(/selectCreatedAnnotation/);
  });

  it("拖动阈值用屏幕像素（与缩放倍率解耦），Shift 约束正方形", () => {
    const fn = extractFn(appSrc, "onRectCanvasPointerMove");
    expect(fn).toMatch(/e\.clientX/);
    expect(fn).toMatch(/Math\.hypot/);
    expect(fn).toMatch(/RECT_DRAG_SCREEN_PX/);
    // 不再用图像像素阈值判定
    expect(fn).not.toMatch(/img\.x - rectDrawInfo\.x0/);
    expect(fn).toMatch(/e\.shiftKey/);
  });

  it("撤销/重做助手存在且以语义操作为单位", () => {
    for (const name of ["pushUndoEntry", "performUndo", "performRedo",
                        "undoWhileDrawing", "undoCreateEntry", "undoEditEntry",
                        "onViewerKeydown", "canUndo", "canRedo"]) {
      expect(() => extractFn(appSrc, name)).not.toThrow();
    }
    // 撤销栈限定当前身份/切片
    const scope = extractFn(appSrc, "undoEntryInScope");
    expect(scope).toMatch(/state\.slide\.name/);
    expect(scope).toMatch(/currentUserId/);
    // 创建的逆 = DELETE（带 expected_revision CAS；409 冲突不覆盖）
    const undoCreate = extractFn(appSrc, "undoCreateEntry");
    expect(undoCreate).toMatch(/sendAnnoDelete/);
    expect(undoCreate).not.toMatch(/entry\.index/);
    expect(extractFn(appSrc, "sendAnnoDelete")).toMatch(/annoIdUrl/);
    expect(extractFn(appSrc, "sendAnnoDelete")).toMatch(/expected_revision/);
    expect(undoCreate).toMatch(/undo\.conflict/);
    // 编辑的逆 = PATCH 回上一版
    const undoEdit = extractFn(appSrc, "undoEditEntry");
    expect(undoEdit).toMatch(/sendAnnoPatch/);
    expect(undoEdit).toMatch(/entry\.before\.geom/);
    // 绘制中撤销 = 撤草稿/上一控制点，不是每 pointermove 压栈
    const whileDrawing = extractFn(appSrc, "undoWhileDrawing");
    expect(whileDrawing).toMatch(/points\.pop/);
  });

  it("键盘：Ctrl/Cmd+Z 撤销、Shift+Z/Ctrl+Y 重做；输入控件内不劫持", () => {
    const fn = extractFn(appSrc, "onViewerKeydown");
    expect(fn).toMatch(/isContentEditable/);
    expect(fn).toMatch(/performUndo\(\)/);
    expect(fn).toMatch(/performRedo\(\)/);
    expect(appSrc).toMatch(/window\.addEventListener\("keydown", onViewerKeydown\)/);
    // 保存进行中的撤销意图在成功后补执行
    expect(extractFn(appSrc, "submitAnnotationDraft")).toMatch(/pendingUndoAfterSave/);
  });

  it("箭头首次单击不产生零长度记录（armed 起点保留）", () => {
    const fn = extractFn(appSrc, "onAnnoPointerUp");
    expect(fn).toMatch(/armed && !dragged/);
    expect(fn).not.toMatch(/armed[^;]*finishDraw\s*\(\)\s*;\s*return/);
  });
});

describe("工单 D：分享页契约（share.js）", () => {
  it("pointercancel = 取消恢复，不接 pointerup/finishDraw", () => {
    expect(shareSrc).toMatch(/addEventListener\("pointercancel", onAnnoPointerCancel\)/);
    expect(shareSrc).not.toMatch(/addEventListener\("pointercancel", onAnnoPointerUp\)/);
    const cancelFn = extractFn(shareSrc, "onAnnoPointerCancel");
    expect(cancelFn).not.toMatch(/finishDraw/);
    // roiBox 拖拽取消恢复拖前位置
    expect(shareSrc).toMatch(
      /roiBox\.addEventListener\("pointercancel", onRoiPointerCancel\)/);
    expect(extractFn(shareSrc, "onRoiPointerCancel")).toMatch(/startRoi/);
  });

  it("保存失败保留草稿：catch 恢复预览、不退工具", () => {
    const fn = extractFn(shareSrc, "saveAnnotation");
    const catchIdx = fn.lastIndexOf(".catch(function");
    expect(catchIdx).toBeGreaterThan(0);
    const catchBody = fn.slice(catchIdx);
    expect(catchBody).toMatch(/restorePreviewFromGeom/);
    expect(catchBody).not.toMatch(/exitDrawMode/);
    // 切片切换后的回包不落到新切片
    expect(fn).toMatch(/state\.slide\.name !== slideName/);
  });

  it("只读分享无新增写入口：不引入自动保存矩形/主站标注 API", () => {
    // share.js 只允许既有的 /api/roi 写路径；不新增 /api/annotation 直写
    expect(shareSrc).not.toMatch(/["']\/api\/annotation/);
    expect(shareSrc).not.toMatch(/client_action_id/);
  });
});
