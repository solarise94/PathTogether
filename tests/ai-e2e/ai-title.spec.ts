import { test, expect, type Page } from '@playwright/test';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { tmpdir } from 'node:os';
const port = Number(process.env.E2E_PORT || 8907);
const creds = JSON.parse(readFileSync(process.env.E2E_CREDS_FILE || join(tmpdir(), `pt-e2e-creds-${port}.json`), 'utf8'));
const writes: string[] = [];
async function boot(page: Page, owner = false) {
  writes.length = 0;
  page.setDefaultTimeout(10000);
  await page.addInitScript(() => localStorage.setItem('hp_lang', 'zh'));
  await page.route('**/api/ai/**', async route => {
    const path = new URL(route.request().url()).pathname;
    if (route.request().method() !== 'GET') writes.push(path);
    let data: object = {};
    if (path === '/api/ai/config') data = { using: 'platform', platform_configured: true, base_url: 'https://example.test', api_key_set: true, max_steps: 30, model: 'test' };
    if (path === '/api/ai/sessions') data = { conversations: Array.from({ length: 12 }, (_, i) => ({ id: `session-${i}`, title: i === 0 ? '观察组织结构' : `切片讨论 ${i}`, status: 'finished', active: i === 0, updated_at: 1791500000 - i * 3600, branches: i === 0 ? [{ id: 'branch-1', title: '局部复核', status: 'finished' }] : [] })) };
    if (path.startsWith('/api/ai/session/')) data = { session: { id: path.split('/')[4], status: 'finished', allow_ai_drawing: false }, transcript: [{ role: 'user', content: '请帮我梳理这张切片的观察顺序。' }, { role: 'assistant', content: '可以从低倍观察组织结构，再放大查看感兴趣区域。' }] };
    await route.fulfill({ json: data });
  });
  await page.goto('/login');
  await page.fill('[name=username]', owner ? creds.ownerLogin : creds.userLogin);
  await page.fill('[name=password]', owner ? creds.ownerPassword : creds.userPassword);
  await Promise.all([page.waitForURL('**/app'), page.click('form[action="/login"] button[type=submit]')]);
  if (owner) {
    const token = (await page.context().cookies()).find(c => c.name === 'csrf_token')!.value;
    const grant = await page.request.post(`/api/admin/v1/slides/${creds.rasterSlides.workbench.slide_id}/temporary-view`, { headers: { 'X-CSRF-Token': token } });
    expect(grant.ok()).toBeTruthy();
  }
  await page.goto(`/app?slide=${creds.rasterSlides.workbench.slide_id}`);
  await page.waitForFunction(() => !!(window as any).HistoPilot?.s?.slide);
  if (await page.locator('#ai-btn').isVisible()) await page.locator('#ai-btn').click();
  else { await page.locator('#tbb-more-btn').click(); await page.locator('#tbb-more-ai').click(); }
  await expect(page.locator('#ai-panel')).toBeVisible();
  await expect(page.locator('#ai-panel')).toHaveClass(/ai-title-layout/);
  await expect(page.locator('#ai-attach-view-btn')).toHaveCount(0);
}

test('real plugin: search, branch/history switching, new draft preservation and folded settings', async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 900 });
  await boot(page);
  await expect(page.locator('#ai-panel-title')).toHaveText('观察组织结构');
  await expect(page.locator('#ai-config-wrap')).toBeHidden();
  await expect(page.locator('#ai-session-select')).toBeHidden();
  await page.locator('#ai-conversation-toggle').click();
  await expect(page.locator('.ai-session-row')).toHaveCount(8);
  await page.locator('#ai-conversation-all').click();
  await expect(page.locator('.ai-session-row')).toHaveCount(13);
  await expect(page.locator('#ai-conversation-all')).toBeHidden();
  await page.keyboard.press('ArrowUp');
  await expect(page.locator('.ai-session-row').last()).toBeFocused();
  await page.locator('#ai-conversation-search').fill('局部复核');
  await page.locator('.ai-session-row').click();
  await expect(page.locator('#ai-panel-title')).toHaveText('局部复核');
  await expect.poll(() => page.evaluate(() => (window as any).HistoPilot.s.activeAiSession.kind)).toBe('branch');
  await page.locator('#ai-new-conversation').click();
  await expect(page.locator('#ai-panel-title')).toHaveText('新对话');
  await page.locator('#ai-task').fill('保留我的未发送草稿');
  await page.locator('#ai-options-toggle').click();
  await expect(page.locator('.hp-drawing-toggle')).toBeVisible();
  await page.locator('.hp-drawing-toggle').click();
  await expect(page.locator('#hp-ai-drawing-toggle')).toBeChecked();
  await expect(page.locator('#ai-service-settings')).not.toHaveAttribute('open', '');
  await page.locator('#ai-service-settings > summary').click();
  await expect(page.locator('#ai-max-steps')).toBeVisible();
  await expect(page.locator('#ai-max-steps')).toHaveAttribute('readonly', '');
  await expect(page.locator('#ai-api-key')).toBeHidden();
  await page.keyboard.press('Escape');
  await expect(page.locator('#ai-options')).toBeHidden();
  await page.locator('#ai-conversation-toggle').click();
  await page.locator('#ai-conversation-search').fill('切片讨论 11');
  await page.keyboard.press('ArrowDown'); await page.keyboard.press('Enter');
  await expect(page.locator('#ai-panel-title')).toHaveText('切片讨论 11');
  await page.locator('#ai-new-conversation').click();
  await expect(page.locator('#ai-task')).toHaveValue('保留我的未发送草稿');
  await page.locator('#ai-options-toggle').click();
  await expect(page.locator('#hp-ai-drawing-toggle')).toBeChecked();
  expect(writes).toEqual([]); // Switching/preparing a draft must never start a model.
});

test('real plugin: title buttons do not drag, grip and resize work, keyboard dismisses history', async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 900 }); await boot(page);
  const panel = page.locator('#ai-panel'); const before = (await panel.boundingBox())!;
  await page.locator('#ai-conversation-toggle').click();
  await page.keyboard.press('Escape');
  await expect(page.locator('#ai-conversation-toggle')).toBeFocused();
  expect((await panel.boundingBox())!.x).toBe(before.x);
  const grip = (await page.locator('.ai-drag-grip').boundingBox())!;
  await page.mouse.move(grip.x + 8, grip.y + 8); await page.mouse.down();
  await page.mouse.move(grip.x - 112, grip.y + 8); await page.mouse.up();
  expect((await panel.boundingBox())!.x).toBeLessThan(before.x - 80);
  const moved = (await panel.boundingBox())!;
  const handle = (await page.locator('#ai-panel-resize').boundingBox())!;
  await page.mouse.move(handle.x + 4, handle.y + 4); await page.mouse.down();
  await page.mouse.move(handle.x + 64, handle.y - 76); await page.mouse.up();
  const resized = (await panel.boundingBox())!;
  expect(resized.width).toBeGreaterThan(moved.width + 40);
  expect(resized.height).toBeLessThan(moved.height - 50);
  await page.reload(); await page.locator('#ai-btn').click();
  await expect(panel).toBeVisible();
  expect(Math.abs((await panel.boundingBox())!.width - resized.width)).toBeLessThan(2);
  if ((await page.locator('#menu-btn').getAttribute('aria-expanded')) !== 'true') await page.locator('#menu-btn').click();
  await expect.poll(async () => {
    const p = (await panel.boundingBox())!, f = (await page.locator('#viewer-wrap').boundingBox())!;
    return p.x + p.width - f.x - f.width;
  }).toBeLessThanOrEqual(1);

});

test('real plugin: mobile-first to desktop enables drag; short viewport keeps panel inside', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 }); await boot(page);
  await page.locator('#ai-options-toggle').click();
  const mobile = (await page.locator('#ai-options').boundingBox())!;
  expect(mobile.x).toBeGreaterThanOrEqual(0); expect(mobile.x + mobile.width).toBeLessThanOrEqual(390);
  await page.keyboard.press('Escape');
  await page.setViewportSize({ width: 1200, height: 800 });
  const panel = page.locator('#ai-panel'); const before = (await panel.boundingBox())!;
  const grip = (await page.locator('.ai-drag-grip').boundingBox())!;
  await page.mouse.move(grip.x + 8, grip.y + 8); await page.mouse.down();
  await page.mouse.move(grip.x - 100, grip.y + 8); await page.mouse.up();
  expect((await panel.boundingBox())!.x).toBeLessThan(before.x - 70);
  await page.setViewportSize({ width: 1200, height: 260 });
  const frame = (await page.locator('#viewer-wrap').boundingBox())!;
  const after = (await panel.boundingBox())!;
  expect(after.y + after.height).toBeLessThanOrEqual(frame.y + frame.height + 1);
});

test('real plugin: owner service controls remain reachable behind options', async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 900 }); await boot(page, true);
  await expect(page.locator('#ai-config-wrap')).toBeHidden();
  await page.locator('#ai-options-toggle').click();
  await page.locator('#ai-service-settings > summary').click();
  await page.locator('#ai-reconfig-btn').click();
  await expect(page.locator('#ai-config-save')).toBeVisible();
  await expect(page.locator('#ai-api-key')).toBeVisible();
});


test('real plugin: missing configuration has a visible settings entry', async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 900 }); await boot(page);
  await page.route('**/api/ai/config', r => r.fulfill({ json: { using: 'platform', platform_configured: false, max_steps: 30 } }));
  await page.reload();
  await page.waitForFunction(() => !!(window as any).HistoPilot?.s?.slide);
  await page.locator('#ai-btn').click();
  await expect(page.locator('#ai-setup-notice')).toBeVisible();
  await page.locator('#ai-setup-notice').click();
  await expect(page.locator('#ai-max-steps')).toBeVisible();
  expect(writes).toEqual([]);
});
