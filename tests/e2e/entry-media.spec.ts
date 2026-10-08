import { test, expect } from '@playwright/test';

test.use({ viewport: { width: 1440, height: 1000 }, locale: 'zh-CN' });

test('homepage hero ships one preview raster and runs the full SVG flow without changing tissue', async ({ page }) => {
  const images: string[] = [];
  const errors: string[] = [];
  page.on('request', request => { if (/\.(webp|jpe?g|png)(\?|$)/.test(request.url())) images.push(request.url()); });
  page.on('pageerror', error => errors.push(error.message));
  // Drive real requestAnimationFrame callbacks with a frame clock. Advancing
  // 400 frames exercises every automatic transition without 40s of wall time.
  await page.addInitScript(() => {
    const callbacks = new Map<number, FrameRequestCallback>();
    let id = 0;
    window.requestAnimationFrame = callback => { callbacks.set(++id, callback); return id; };
    window.cancelAnimationFrame = key => { callbacks.delete(key); };
    (window as any).__pendingFrames = () => callbacks.size;
    (window as any).__advanceFrame = (now: number) => {
      const pending = Array.from(callbacks.values());
      callbacks.clear();
      pending.forEach(callback => callback(now));
    };
  });
  await page.goto('/');
  await expect(page.locator('h1')).toHaveText('与 AI 一起，观察病理切片。');
  await expect(page.locator('body')).not.toContainText('不用于临床诊断');
  // 主页升级（H1）：Hero 旧组织 SVG（#hero-tissue）已删，换真实工作台截图；
  // 下方「Agent 怎么读一张切片」独立演示（#tissue）保留。
  await expect(page.locator('#hero-tissue')).toHaveCount(0);
  const preview = page.locator('#workbench-preview-img');
  await expect(preview).toBeVisible();
  await expect(preview).toHaveAttribute('width', '1440');
  await expect(preview).toHaveAttribute('height', '900');
  await page.locator('.tissue-demo').scrollIntoViewIfNeeded();
  await expect(page.locator('#tissue')).toHaveAttribute('data-scene', '0');
  const geometry = await page.locator('#tissue [data-glands]').innerHTML();
  await page.evaluate(() => {
    const tissue = document.querySelector('#tissue')!;
    const seen = new Set([tissue.getAttribute('data-scene')]);
    (window as any).__sceneCoverage = seen;
    new MutationObserver(() => seen.add(tissue.getAttribute('data-scene')))
      .observe(tissue, { attributes: true, attributeFilter: ['data-scene'] });
  });
  await expect.poll(() => page.evaluate(() => (window as any).__pendingFrames())).toBeGreaterThan(0);
  await page.evaluate(async () => {
    for (let frame = 1; frame <= 400; frame++) {
      (window as any).__advanceFrame(frame * 100);
      await Promise.resolve(); // Let the scene observer see each transition.
    }
  });
  expect(await page.evaluate(() => Array.from((window as any).__sceneCoverage).sort()))
    .toEqual(['0', '1', '2', '3', '4', '5', '6', '7']);
  expect(await page.locator('#tissue [data-glands]').innerHTML()).toBe(geometry);
  // 首页栅格预算：仅 Hero 工作台预览一张（WebP；桌面 1440 视口不选 720 档）
  expect(images.filter(u => !u.includes('/static/entry-media/workbench-preview'))).toEqual([]);
  expect(images.some(u => u.endsWith('/static/entry-media/workbench-preview.webp'))).toBe(true);
  expect(errors).toEqual([]);
});

test('manual annotation types progressively, pauses, and updates language in place', async ({ page }) => {
  await page.clock.install();
  await page.goto('/');
  await page.locator('.tissue-demo').scrollIntoViewIfNeeded();
  await expect(page.locator('#tissue')).toHaveAttribute('data-scene', '0');
  for (let i = 0; i < 3; i++) await page.locator('#next').click();
  await expect(page.locator('#note-body')).toBeEmpty();
  await page.clock.runFor(800);
  const partial = await page.locator('#note-title').textContent();
  expect(partial!.length).toBeGreaterThan(0);
  expect(partial!.length).toBeLessThan(18);
  await page.clock.runFor(1800);
  await page.locator('#toggle').click();
  const text = await page.locator('#note-body').textContent();
  await page.clock.runFor(1000);
  await expect(page.locator('#note-body')).toHaveText(text!);
  await page.locator('.lang-toggle').click();
  await expect(page.locator('h1')).toHaveText('Explore pathology slides with AI.');
  await expect(page.locator('#phase')).toHaveText('Record the first observation');
  await expect(page.locator('#note-title')).toContainText('Region A');
  await expect(page.locator('#replay')).toHaveText('Replay ↻');
  for (let i = 0; i < 4; i++) await page.locator('#next').click();
  await expect(page.locator('#review-a')).toContainText('Region A');
  await expect(page.locator('#review-b')).toContainText('Region B');
});

test('mobile reduced motion retains both annotations and keeps labels inside the viewport', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.emulateMedia({ reducedMotion: 'reduce' });
  await page.goto('/');
  await page.locator('.tissue-demo').scrollIntoViewIfNeeded();
  await expect(page.locator('#tissue')).toHaveAttribute('data-scene', '7');
  const canvas = (await page.locator('.tissue-demo .canvas').boundingBox())!;
  const labels = [];
  for (const key of ['a', 'b']) {
    await expect(page.locator(`#tissue [data-mark="${key}"]`)).toHaveAttribute('visibility', 'visible');
    await expect(page.locator('#review-' + key)).toBeVisible();
    const label = (await page.locator('#review-' + key).boundingBox())!;
    expect(label.x).toBeGreaterThanOrEqual(canvas.x);
    expect(label.x + label.width).toBeLessThanOrEqual(canvas.x + canvas.width);
    labels.push(label);
  }
  expect(labels[0].y + labels[0].height).toBeLessThanOrEqual(labels[1].y);
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBeTruthy();
});
