/**
 * 主页升级（H1-media）：工作台预览截图 + 主页前后对比。
 *
 * 前置：boot_preview_app.py 已在 --port 起好（读到 PREVIEW_READY）。
 * 产出（写入 docs/review-evidence/homepage-upgrade/artifacts/）：
 *   - workbench.png        工作台真实界面（登录 → 打开示例切片 → 打开标注
 *     面板并画一个真实标注；视口 1280×800）
 *   - entry-before.png     升级前主页（可选：HEAD~ 基线，由调用方决定）
 *   - entry-after.png      升级后主页（未登录，1440×900）
 *
 * 用法：node docs/review-evidence/homepage-upgrade/shot_workbench.mjs \
 *         [--port 8917]
 * 仅使用本地测试环境与合成示例切片；不触碰任何真实账号/数据。
 */
import { chromium } from '@playwright/test';
import { readFileSync, writeFileSync, mkdirSync } from 'node:fs';
import { resolve, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';

const here = dirname(fileURLToPath(import.meta.url));
const args = process.argv.slice(2);
const portFlag = args.indexOf('--port');
const port = portFlag >= 0 ? Number(args[portFlag + 1]) : 8917;
const creds = JSON.parse(readFileSync(
  resolve(process.env.TMPDIR || '/tmp', `pt-preview-creds-${port}.json`), 'utf8'));
const outDir = resolve(here, 'artifacts');
mkdirSync(outDir, { recursive: true });

const browser = await chromium.launch();

// ---------- 1) 工作台真实界面（1440×900；>=1440 工具栏全展开，标注组不折叠） ----------
const page = await browser.newPage({ viewport: { width: 1440, height: 900 }, locale: 'zh-CN' });
await page.goto(creds.baseUrl + '/login');
await page.fill('form[action="/login"] input[name="username"]', creds.login);
await page.fill('form[action="/login"] input[name="password"]', creds.password);
await Promise.all([
  page.waitForURL(u => !u.pathname.includes('/login')),
  page.click('form[action="/login"] button[type="submit"]'),
]);
// 展开侧栏（默认可能收起）并打开第一张示例切片
await page.click('#menu-btn');
await page.waitForTimeout(600);
await page.waitForSelector('#unfiled-list .slide-row', { state: 'visible' });
await page.click('#unfiled-list .slide-row');
await page.waitForSelector('#viewer .openseadragon-canvas canvas', { timeout: 30000 });
// 等首屏瓦片渲染稳定（真实 openslide 切片）
await page.waitForTimeout(2500);
// 打开标注面板：清理此前试跑留下的未命名标注（默认标签「管理员」有误导性），
// 再以「观察标记」为名画一个真实箭头标注。选择器来自 _app_shell.html/app.js：
// #anno-btn 标记面板开关（切片已有标注才启用）、.ai-del 面板行删除、
// #anno-more-btn 标注选项 popover 内 #anno-label-input 名称、#anno-arrow-btn
// 箭头工具（切片打开即启用），绘制走 #anno-canvas 覆盖层的 pointer 事件。
const step = async (name, fn) => { try { await fn(); console.log('STEP_OK', name); }
  catch (e) { console.log('STEP_FAIL', name, e.message.split('\n')[0]); throw e; } };
const annoBtnEnabled = await page.locator('#anno-btn').evaluate(el => !el.disabled);
if (annoBtnEnabled) {
  await step('panel-open', () => page.click('#anno-btn'));
  await page.waitForTimeout(500);
  for (let i = 0; i < 10; i++) {
    const del = page.locator('#anno-panel-list .ai-del').first();
    if (!(await del.count())) break;
    await del.click();
    await page.waitForTimeout(450);
  }
  await step('panel-close', () => page.click('#anno-panel-close'));
}
await step('anno-pop', () => page.click('#anno-more-btn'));
await step('label-fill', () => page.fill('#anno-label-input', '观察标记'));
await step('anno-pop-close', () => page.keyboard.press('Escape'));
await step('arrow-tool', () => page.click('#anno-arrow-btn'));
const canvas = page.locator('#viewer .openseadragon-canvas canvas').first();
const box = await canvas.boundingBox();
if (!box) throw new Error('viewer canvas not found');
await page.mouse.move(box.x + box.width * 0.40, box.y + box.height * 0.34);
await page.mouse.down();
await page.mouse.move(box.x + box.width * 0.66, box.y + box.height * 0.56, { steps: 10 });
await page.mouse.up();
// 等待 POST /api/annotation 保存 + 标注列表刷新（保存成功后自动回到平移工具）
await page.waitForTimeout(1500);
await step('panel-final', () => page.click('#anno-btn'));
// 「显示全部标记」👁：打开 showAnno（新画标注已被选中聚焦，首击清除聚焦并
// 保持全部显示；此前打开过切片时该状态默认为 false，画布层不画标注）
await step('anno-all-toggle', () => page.click('#anno-all-toggle'));
// 截图前在页内验证：标注画布确有笔迹（箭头 + 名称标签像素）
const painted = await page.evaluate(() => {
  const c = document.getElementById('anno-canvas');
  if (!c) return -1;
  const d = c.getContext('2d').getImageData(0, 0, c.width, c.height).data;
  let non = 0;
  for (let i = 3; i < d.length; i += 4) if (d[i] > 0) non++;
  return non;
});
console.log('ANNO_PAINTED_PIXELS', painted);
if (painted <= 0) throw new Error('annotation canvas empty after toggle');
await page.waitForTimeout(700);
await page.screenshot({ path: resolve(outDir, 'workbench.png') });
await page.close();

// ---------- 2) 主页（升级后，未登录，1440×900；中/英各一张） ----------
const entry = await browser.newPage({ viewport: { width: 1440, height: 900 }, locale: 'zh-CN' });
await entry.goto(creds.baseUrl + '/');
await entry.waitForLoadState('networkidle');
await entry.screenshot({ path: resolve(outDir, 'entry-after.png') });
await entry.click('.lang-toggle');
await entry.waitForTimeout(400);
await entry.screenshot({ path: resolve(outDir, 'entry-after-en.png') });
await entry.close();

await browser.close();
console.log('SHOTS_DONE ' + outDir);
