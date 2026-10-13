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

test('缺少保存 API：选文件前显示警告，保留转换上传路径，手机不溢出', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 900 });
  await picker(page, false); await open(page);
  await expect(page.locator('#save-capability-note')).toBeVisible();
  await expect(page.locator('#save-capability-note')).toContainText('无法将转换结果保存到电脑');
  await expect(page.locator('#save-capability-note')).toContainText('登录上传');
  const layout = await page.evaluate(() => ({ width: innerWidth, scroll: document.documentElement.scrollWidth }));
  expect(layout.scroll).toBeLessThanOrEqual(layout.width + 1);
  await convert(page);
  await expect(page.locator('#save-btn')).toBeDisabled();
  await expect(page.locator('#upload-btn')).toBeEnabled();
});

test('不支持的格式：短说明与可用转换指南入口，不展示过时的格式集合', async ({ page }) => {
  await picker(page); await open(page);
  await page.setInputFiles('#file-input', { name: 'bad.png', mimeType: 'image/png', buffer: Buffer.from('unsupported') });
  await expect(page.locator('#page-error')).toContainText('格式或编码');
  await expect(page.locator('#page-error')).not.toContainText('支持 KFB / KFBF / Aperio');
  await expect(page.locator('a[href="/guide/conversion"]')).toBeVisible();
});
