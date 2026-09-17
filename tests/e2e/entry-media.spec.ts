import { test, expect } from '@playwright/test';

test.use({ viewport: { width: 1440, height: 1000 }, locale: 'zh-CN' });

test('homepage loads no raster images and runs the full SVG flow without changing tissue', async ({ page }) => {
  // 100 轮 clock.runFor + getAttribute 往返，慢环境下会顶到默认 60s 超时
  test.slow();
  const images: string[] = [];
  const errors: string[] = [];
  page.on('request', request => { if (/\.(webp|jpe?g|png)(\?|$)/.test(request.url())) images.push(request.url()); });
  page.on('pageerror', error => errors.push(error.message));
  await page.clock.install();
  await page.goto('/');
  await expect(page.locator('h1')).toHaveText('与 AI 一起，观察病理切片。');
  await expect(page.locator('body')).not.toContainText('不用于临床诊断');
  await expect(page.locator('#hero-tissue [data-glands]')).toBeAttached();
  await page.locator('.tissue-demo').scrollIntoViewIfNeeded();
  await expect(page.locator('#tissue')).toHaveAttribute('data-scene', '0');
  const geometry = await page.locator('#tissue [data-glands]').innerHTML();
  const seen = new Set<string>();
  for (let i = 0; i < 100; i++) {
    await page.clock.runFor(400);
    seen.add((await page.locator('#tissue').getAttribute('data-scene'))!);
  }
  expect(seen.size).toBe(8);
  expect(await page.locator('#tissue [data-glands]').innerHTML()).toBe(geometry);
  expect(images).toEqual([]);
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
