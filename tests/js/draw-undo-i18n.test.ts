/**
 * 工单 D（§5）聚焦源码契约：i18n 键（中英成对）+ 裁剪/标注双保存路径隔离 +
 * 状态机形态。jsdom 不可用（无 DOM 环境），按本仓库既有模式锁生产源码契约。
 */
import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";

const here = dirname(fileURLToPath(import.meta.url));
const appSrc = readFileSync(resolve(here, "../../static/app.js"), "utf8");
const i18nSrc = readFileSync(resolve(here, "../../static/i18n.js"), "utf8");

function dictBlock(lang: string): string {
  const re = new RegExp(`${lang}: \\{`);
  const m = re.exec(i18nSrc);
  expect(m, `${lang} 字典必须存在`).toBeTruthy();
  const start = m!.index;
  // 字典块到下一个顶层语言键（zh: { ... },\n\n    en: {）为止
  const next = i18nSrc.slice(start + 10).search(/\n\s{4}(zh|en): \{/);
  return next >= 0 ? i18nSrc.slice(start, start + 10 + next) : i18nSrc.slice(start);
}

describe("工单 D：i18n 键中英成对", () => {
  const keys = [
    "draw.cancelled",
    "draw.unsaved.tip",
    "draw.unsaved.retry",
    "undo.done",
    "undo.fail",
    "undo.conflict",
    "undo.pending",
    "redo.done",
    "redo.fail",
  ];

  it.each(keys)("%s 在 zh/en 两侧都存在", (key) => {
    expect(dictBlock("zh")).toMatch(new RegExp(`"${key}":`));
    expect(dictBlock("en")).toMatch(new RegExp(`"${key}":`));
  });

  it("app.js 引用的工单 D 文案键都已定义", () => {
    for (const key of keys) {
      expect(appSrc).toMatch(new RegExp(`t\\("${key.replace(/\./g, "\\.")}"`));
    }
  });
});

describe("工单 D：裁剪导出与标注保存隔离", () => {
  it("saveCrop（保存图片）不产生标注：无 POST /api/annotation", () => {
    const m = /function saveCrop\(\)/.exec(appSrc);
    expect(m).toBeTruthy();
    const start = m!.index;
    const end = appSrc.indexOf("\n  function ", start + 10);
    const fn = appSrc.slice(start, end > 0 ? end : undefined);
    expect(fn).not.toMatch(/\/api\/annotation/);
    expect(fn).not.toMatch(/submitCurrentDraft|submitAnnotationDraft|saveAnno/);
  });

  it("saveAnno（保存标记）走统一提交（幂等 + 草稿保留）", () => {
    const m = /function saveAnno\(\)/.exec(appSrc);
    expect(m).toBeTruthy();
    const start = m!.index;
    const end = appSrc.indexOf("\n  function ", start + 10);
    const fn = appSrc.slice(start, end > 0 ? end : undefined);
    expect(fn).toMatch(/submitCurrentDraft\(\)/);
    // 不再自带第二条 POST 路径
    expect(fn).not.toMatch(/apiFetch\("\/api\/annotation"/);
  });
});

describe("工单 D：状态机与幂等键", () => {
  it("绘制会话相位变量存在（idle/drawing/saving）", () => {
    expect(appSrc).toMatch(/drawPhase = "idle"/);
    expect(appSrc).toMatch(/setDrawPhase\("drawing"\)/);
    expect(appSrc).toMatch(/setDrawPhase\("saving"\)/);
    expect(appSrc).toMatch(/setDrawPhase\("idle"\)/);
  });

  it("重试沿用同一 client_action_id（重试/双击不重复建标注）", () => {
    const fn = appSrc.slice(
      appSrc.indexOf("function submitAnnotationDraft"),
      appSrc.indexOf("function enterDrawMode"),
    );
    expect(fn).toMatch(/retryDraft && retryDraft\.clientActionId/);
    expect(fn).toMatch(/retryDraft = \{/);
    expect(fn).toMatch(/clientActionId: actionId/);
  });

  it("删除成功后把 tombstone revision 写回 entry 供 restore", () => {
    const fn = appSrc.slice(
      appSrc.indexOf("function undoCreateEntry"),
      appSrc.indexOf("function cleanPatchGeom"),
    );
    expect(fn).toMatch(/entry\.revision = res\.revision/);
  });

  it("重做创建走 restore 而不是复用 client_action_id 再 INSERT", () => {
    const fn = appSrc.slice(
      appSrc.indexOf("function performRedo"),
      appSrc.indexOf("function canUndo"),
    );
    expect(fn).toMatch(/\/restore/);
    expect(fn).not.toMatch(/client_action_id: entry\.clientActionId/);
  });

  it("创建成功后回到移动工具（exitRoi/exitDrawMode）并按 annotation_id 选中", () => {
    const fn = appSrc.slice(
      appSrc.indexOf("function submitAnnotationDraft"),
      appSrc.indexOf("function enterDrawMode"),
    );
    expect(fn).toMatch(/exitRoi\(\)/);
    expect(fn).toMatch(/exitDrawMode\(\)/);
    expect(fn).toMatch(/selectCreatedAnnotation\(j && j\.annotation_id\)/);
    const sel = appSrc.slice(
      appSrc.indexOf("function selectCreatedAnnotation"),
      appSrc.indexOf("function findItemByAnnotationId"),
    );
    expect(sel).toMatch(/findItemByAnnotationId/);
  });

  it("撤销编辑提交前清洗 side_px（v2 w/h 契约）", () => {
    const fn = appSrc.slice(
      appSrc.indexOf("function cleanPatchGeom"),
      appSrc.indexOf("function undoEditEntry"),
    );
    expect(fn).toMatch(/side_px/);
  });
});
