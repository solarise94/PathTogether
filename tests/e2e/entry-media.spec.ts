import { test, expect } from '@playwright/test';

test.use({ viewport: { width: 1440, height: 900 } });

test('homepage defers the stack until visible and reuses images across a full loop', async ({ page }) => {
  const media: string[] = [];
  page.on('request', request => {
    if (request.url().includes('/entry-media/')) media.push(request.url());
  });
  await page.clock.install();
  await page.goto('/');
  await expect(page.locator('.hero-slide')).toBeVisible();
  expect(media).toHaveLength(1);
  expect(media[0]).toMatch(/tcga-session-low-\d+\.webp$/);
  await expect(page.locator('[data-hp-zoom-in]')).toBeDisabled();
  await page.locator('.slide-stage').scrollIntoViewIfNeeded();
  await expect(page.locator('[data-hp-stage]')).toHaveAttribute('data-phase', '0');

  // Each real magnification must appear, including the midpoint replacements.
  const layers = new Set<string>();
  for (let i = 0; i < 100; i++) {
    await page.clock.runFor(320);
    const frame = await page.locator('.shot-img').evaluateAll(images => {
      const image = images.find(el => (el as HTMLElement).style.opacity === '1') as HTMLImageElement;
      return { key: image?.dataset.key, ready: image?.complete && image?.naturalWidth > 0 };
    });
    expect(frame.ready).toBeTruthy();
    if (frame.key) layers.add(frame.key);
  }
  for (const region of ['left', 'upper', 'right']) {
    for (const mag of [2, 10, 20, 40]) expect(layers.has(`tcga-${region}-${mag}.jpg`)).toBeTruthy();
  }
  expect(media).toHaveLength(14);
  expect(new Set(media).size).toBe(14);
});

test('compact viewport selects 640px and mobile review captions remain inside the slide', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.clock.install();
  await page.goto('/');
  await page.locator('.slide-stage').scrollIntoViewIfNeeded();
  await expect(page.locator('[data-hp-stage]')).toHaveAttribute('data-phase', '0');
  await expect(page.locator('.shot-img[style*="opacity: 1"]')).toHaveAttribute('src', /-640\.webp$/);
  await page.locator('[data-hp-zoom-in]').click();
  await page.locator('[data-hp-zoom-in]').click();
  await page.locator('[data-hp-zoom-in]').click();
  const stage = await page.locator('.slide-stage').boundingBox();
  const label = await page.locator('.anno-b .anno-caption').boundingBox();
  expect(label!.x).toBeGreaterThanOrEqual(stage!.x);
  expect(label!.x + label!.width).toBeLessThanOrEqual(stage!.x + stage!.width);
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBeTruthy();
});

test('reduced motion shows the final still and manual navigation remains available', async ({ page }) => {
  await page.emulateMedia({ reducedMotion: 'reduce' });
  await page.goto('/');
  await page.locator('.slide-stage').scrollIntoViewIfNeeded();
  await expect(page.locator('[data-hp-stage]')).toHaveAttribute('data-phase', '4');
  await expect(page.locator('.anno-a')).toHaveClass(/is-on/);
  await expect(page.locator('.anno-b')).toHaveClass(/is-on/);
  await page.locator('[data-hp-zoom-out]').click();
  await expect(page.locator('[data-hp-stage]')).toHaveAttribute('data-phase', '3');
  await expect(page.locator('.anno-a')).not.toHaveClass(/is-on/);
  await expect(page.locator('.shot-img[style*="opacity: 1"]')).toHaveAttribute('src', /right-40-1024\.webp$/);
});

test('failed media keeps navigation disabled instead of showing an empty observation', async ({ page }) => {
  await page.route('**/tcga-left-10-*.webp', route => route.abort());
  await page.goto('/');
  await page.locator('.slide-stage').scrollIntoViewIfNeeded();
  await expect(page.locator('[data-hp-status]')).toHaveAttribute('data-i18n', 'entry.principle.load.error');
  await expect(page.locator('[data-hp-zoom-in]')).toBeDisabled();
  await expect(page.locator('[data-hp-zoom-out]')).toBeDisabled();
});
