/**
 * 「保存到我的项目」评审修复回归（2026-10-23，mac 支付评审 sw5H5x）：
 * /api/projects 失败时，#save-project-error 必须可见。
 *
 * 来源：/mnt/ssd2/tests/review-mac-pay-20261023-sw5H5x/repro-save-dialog.cjs。
 * 按项目约定保留评审脚手架原文（对话框标记切片、setters/render 切片与
 * page.evaluate 体逐字保留），仅把「锁定 BUG 行为」的断言反转为安全行为：
 * 原脚本断言错误元素不可见（isVisible() === false，错误文本被藏进隐藏的
 * #save-project-fields）→ 现断言错误元素可见（isVisible() === true，文本
 * 匹配保留）。被测文件（相对仓库根读取）：templates/tools_slides.html、
 * static/tools/tools-slides.js。
 *
 * 脚手架改动（除以下各项外与评审脚本逐字一致）：
 *   1. 用 Playwright 的 page 夹具替代手动 chromium.launch；
 *   2. 源文件相对仓库根读取（原脚本用评审机绝对路径）；
 *   3. 截图写 testInfo.outputPath(...)（原脚本写评审目录）；
 *   4. 断言反转：isVisible 断言 false → true。
 */
import { expect, test } from "@playwright/test";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";

// 本仓库 package.json 无 "type": "module"：Playwright 将 .ts 规格转译为
// CJS（与 save-to-project.spec.ts 的 __dirname 同口径），不可用 import.meta。
const root = resolve(__dirname, "..", "..");

test("保存到我的项目：/api/projects 失败时错误行可见（不再藏进隐藏字段容器）",
  async ({ page }, testInfo) => {
    const src = readFileSync(
      resolve(root, "static/tools/tools-slides.js"), "utf8");
    const html = readFileSync(
      resolve(root, "templates/tools_slides.html"), "utf8");
    const dialog = html.slice(
      html.indexOf('  <dialog id="save-project-dialog"'),
      html.lastIndexOf("</dialog>") + 9);
    const setters = src.slice(
      src.indexOf("function setSaveProjectMsg("),
      src.indexOf("async function openSaveProjectDialog("));
    const render = src.slice(
      src.indexOf("async function renderSaveProjectDestinations("),
      src.indexOf("function onSaveProjectDestChange("));
    await page.setContent(dialog);
    await page.evaluate(async ({ setters, render }) => {
      const els = {
        saveProjectStatus: document.getElementById('save-project-status'),
        saveProjectError: document.getElementById('save-project-error'),
      };
      const t = (key) => key;
      const fetchProjects = async () => {throw new Error('HTTP 503');};
      const run = new Function('els','t','fetchProjects', setters + render + '\nreturn renderSaveProjectDestinations();');
      document.getElementById('save-project-dialog').showModal();
      await run(els, t, fetchProjects);
    }, { setters, render });
    const error = page.locator("#save-project-error");
    expect(await error.textContent())
      .toMatch(/tools\.saveproj\.projects\.failed/);
    // 反转的断言：错误行必须真的可见（修复前它嵌在 hidden 的
    // #save-project-fields 里，文本在 DOM 上但用户看不见）
    expect(await error.isVisible()).toBe(true);
    await page.screenshot({
      path: testInfo.outputPath("save-project-error-visible.png"),
    });
  });
