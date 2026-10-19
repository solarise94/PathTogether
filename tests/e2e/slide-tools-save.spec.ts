import { test, expect, type Page } from '@playwright/test';

test.use({ locale: 'zh-CN', viewport: { width: 1440, height: 950 } });

// Synthetic uncompressed RGB BMP; input stays in the browser. No private sample
// files or upload tasks. The picker substitute uses a real OPFS writable handle.
function bmp() {
  const width = 96, height = 64, size = 54 + width * height * 3;
  const data = Buffer.alloc(size);
  data.write('BM'); data.writeUInt32LE(size, 2); data.writeUInt32LE(54, 10);
  data.writeUInt32LE(40, 14); data.writeInt32LE(width, 18); data.writeInt32LE(height, 22);
  data.writeUInt16LE(1, 26); data.writeUInt16LE(24, 28);
  for (let i = 54; i < size; i++) data[i] = i % 251;
  return { name: 'save-regression.bmp', mimeType: 'image/bmp', buffer: data };
}

async function picker(page: Page, supported = true) {
  await page.addInitScript(({ supported }) => {
    const w = window as any;
    w.__saveMode = 'success';
    Object.defineProperty(window, 'showSaveFilePicker', {
      configurable: true,
      value: supported ? async () => {
        if (w.__saveMode === 'cancel') throw new DOMException('Cancelled', 'AbortError');
        if (w.__saveMode === 'error') throw new DOMException('Disk permission denied', 'NotAllowedError');
        const root = await navigator.storage.getDirectory();
        return root.getFileHandle('__save_regression.tif', { create: true });
      } : undefined,
    });
  }, { supported });
}

async function open(page: Page) {
  await page.goto('/tools/slides');
  await page.waitForFunction(() => (window as any).__stToolsReady);
}

async function convert(page: Page) {
  await page.setInputFiles('#file-input', bmp());
  await expect(page.locator('#summary-section')).toBeVisible();
  await page.locator('#convert-btn').click();
  await expect(page.locator('#result-section')).toBeVisible();
}

test('转换保存：取消、失败的反馈保持可见，语言切换和重试成功，文件只在本地', async ({ page }) => {
  await picker(page);
  const api: string[] = [], errors: string[] = [];
  page.on('request', r => { if (new URL(r.url()).pathname.startsWith('/api/')) api.push(r.url()); });
  page.on('pageerror', e => errors.push(e.message));
  await open(page);
  await expect(page.locator('#save-capability-note')).toBeHidden();
  await convert(page);
  await expect(page.locator('#result-grid')).toContainText('瓦片重编码摘要');
  await expect(page.locator('#result-grid')).not.toContainText('MRXS');
  await page.evaluate(() => { (window as any).__saveMode = 'cancel'; });
  await page.locator('#save-btn').click();
  await expect(page.locator('#save-status')).toContainText('已取消保存');
  await expect(page.locator('#page-error')).toBeHidden();
  await page.locator('.lang-toggle').click();
  await expect(page.locator('#save-status')).toContainText('Save cancelled');
  await page.evaluate(() => { (window as any).__saveMode = 'error'; });
  await page.locator('#save-btn').click();
  await expect(page.locator('#save-status')).toContainText('Save failed');
  await page.evaluate(() => { (window as any).__saveMode = 'success'; });
  await page.locator('#save-btn').click();
  await expect(page.locator('#save-status')).toContainText('Saved');
  await expect(page.locator('#page-error')).toBeHidden();
  const hash = await page.evaluate(async () => {
    const root = await navigator.storage.getDirectory();
    const file = await (await root.getFileHandle('__save_regression.tif')).getFile();
    return Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256', await file.arrayBuffer())),
      n => n.toString(16).padStart(2, '0')).join('');
  });
  await expect(page.locator('#result-sha')).toHaveText(hash);
  expect(api).toEqual([]); expect(errors).toEqual([]);
});

test('刷新后从任务记录保存：显示本任务结果，取消与失败可重试', async ({ page }) => {
  await picker(page); await open(page); await convert(page);
  await page.reload();
  await page.waitForFunction(() => (window as any).__stToolsReady);
  const save = page.locator('.job-row [data-action="export"]').first();
  await page.evaluate(() => { (window as any).__saveMode = 'cancel'; });
  await save.click();
  await expect(page.locator('#result-section')).toBeVisible();
  await expect(page.locator('#save-status')).toContainText('已取消保存');
  await page.evaluate(() => { (window as any).__saveMode = 'error'; });
  await save.click();
  await expect(page.locator('#save-status')).toContainText('保存失败');
  await page.evaluate(() => { (window as any).__saveMode = 'success'; });
  await save.click();
  await expect(page.locator('#save-status')).toContainText('已保存');
});

test('缺少保存 API：选文件前显示工作台指引，禁用新转换，手机不溢出', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 900 });
  await picker(page, false); await open(page);

  // 2026-10-20 转换器本地化：无保存 API = 没有产出物出口——选文件前就地
  // 给出工作台指引（含普通「打开工作台」链接，不带文件/意图），不再承诺
  // 「转换后登录上传」
  const note = page.locator('#save-capability-note');
  await expect(note).toBeVisible();
  await expect(note).toContainText('无法将转换结果保存到电脑');
  await expect(note).toContainText('在工作台选择原文件');
  await expect(note).toContainText('工作台会自动完成转换');
  await expect(note.locator('a[href="/app"]', { hasText: '打开工作台' })).toBeVisible();
  await expect(note).not.toContainText('登录上传');
  // 上传入口已不存在于本页
  await expect(page.locator('#upload-btn')).toHaveCount(0);
  await expect(page.locator('#convert-upload-btn')).toHaveCount(0);

  // 新转换入口全部禁用（按钮 + 拖放区），绕过入口塞文件也不会开始转换
  await expect(page.locator('#pick-file-btn')).toBeDisabled();
  await expect(page.locator('#pick-folder-btn')).toBeDisabled();
  await expect(page.locator('#drop-zone')).toHaveAttribute('aria-disabled', 'true');
  await page.setInputFiles('#file-input', bmp());
  await expect(page.locator('#summary-section')).toBeHidden();
  await expect(page.locator('#input-message')).toContainText('无法将转换结果保存到电脑');

  // 既有任务的查看/删除不受影响：任务记录卡照常存在（无任务时空态文案）
  await expect(page.locator('#jobs-details')).toContainText('任务记录');

  // 手机宽度无横向溢出
  const layout = await page.evaluate(() => ({ width: innerWidth, scroll: document.documentElement.scrollWidth }));
  expect(layout.scroll).toBeLessThanOrEqual(layout.width + 1);
});

test('不支持的格式：短说明与可用转换指南入口，不展示过时的格式集合', async ({ page }) => {
  await picker(page); await open(page);
  await page.setInputFiles('#file-input', { name: 'bad.png', mimeType: 'image/png', buffer: Buffer.from('unsupported') });
  await expect(page.locator('#page-error')).toContainText('格式或编码');
  await expect(page.locator('#page-error')).not.toContainText('支持 KFB / KFBF / Aperio');
  await expect(page.locator('a[href="/guide/conversion"]')).toBeVisible();
});
