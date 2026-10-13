import {test,expect} from '@playwright/test';
test.use({viewport:{width:1440,height:900},locale:'zh-CN'});

test('collaboration walkthrough plays, pauses, navigates, localizes and loads one image',async({page})=>{
 await page.clock.install();
 const images:string[]=[],errors:string[]=[];
 page.on('request',r=>{if(r.url().includes('/entry-media/tcga-left-20-640.webp'))images.push(r.url());});
 page.on('pageerror',e=>errors.push(e.message));
 await page.goto('/');
 const demo=page.locator('[data-collab-demo]');
 await expect(page.locator('#collaboration h2')).toHaveText('和你的同伴一起查阅切片、标注并讨论');
 await demo.scrollIntoViewIfNeeded();await page.clock.runFor(100);
 await expect(demo).toHaveAttribute('data-step','0');
 await page.clock.runFor(6000);await expect(demo).toHaveAttribute('data-step','1');
 await page.locator('[data-collab-toggle]').click();
 await page.clock.runFor(20000);await expect(demo).toHaveAttribute('data-step','1');
 await page.locator('[data-collab-step="2"]').click();
 await expect(demo).toHaveAttribute('data-step','2');
 await expect(page.locator('[data-collab-status]')).toContainText('补充标注和备注');
 await page.locator('#account-btn').click(); await page.locator('.lang-toggle').click(); await page.locator('#account-btn').click();
 await expect(page.locator('#collaboration h2')).toContainText('with your peers');
 await expect(page.locator('[data-collab-toggle]')).toHaveText('Play');
 await demo.scrollIntoViewIfNeeded();
 await page.locator('[data-collab-replay]').click();
 await expect(demo).toHaveAttribute('data-step','0');
 await page.clock.runFor(100);
 await page.locator('.hero').scrollIntoViewIfNeeded();await page.clock.runFor(100);
 const step=await demo.getAttribute('data-step');
 await page.clock.runFor(20000);await expect(demo).toHaveAttribute('data-step',step!);
 expect(images).toHaveLength(1);expect(errors).toEqual([]);
});

for(const width of [320,390])test(`collaboration reduced motion and controls fit ${width}px`,async({page})=>{
 await page.setViewportSize({width,height:844});await page.emulateMedia({reducedMotion:'reduce'});
 await page.goto('/');const demo=page.locator('[data-collab-demo]');await demo.scrollIntoViewIfNeeded();
 await expect(demo).toHaveAttribute('data-step','2');await expect(demo).toHaveAttribute('data-paused','true');
 await expect(page.locator('[data-collab-toggle]')).toHaveText('播放');
 await expect(page.locator('.collab-note-b')).toHaveCSS('opacity','1');
 expect(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth)).toBe(true);
 for(const button of await page.locator('.collab-controls button').all()){
  const box=await button.boundingBox();expect(box!.height).toBeGreaterThanOrEqual(44);expect(box!.x+box!.width).toBeLessThanOrEqual(width);
 }
 await page.locator('[data-collab-step="0"]').click();await expect(demo).toHaveAttribute('data-step','0');
});

for (const width of [320, 390, 768, 1100, 1440]) {
 test(`collaboration keeps geometry and cards intact at ${width}px`, async ({page}) => {
  await page.setViewportSize({width, height: 1000});
  await page.emulateMedia({reducedMotion: 'reduce'});
  await page.goto('/');
  const demo = page.locator('[data-collab-demo]');
  await demo.scrollIntoViewIfNeeded();
  for (const english of [false, true]) {
   if (english) { await page.locator('#account-btn').click(); await page.locator('.lang-toggle').click(); await page.locator('#account-btn').click(); }
   const heights: number[] = [];
   for (const step of [0, 1, 2]) {
    await page.locator(`[data-collab-step="${step}"]`).click();
    const geometry = await demo.evaluate(el => {
     const svg = el.querySelector<SVGSVGElement>('.collab-marks')!;
     const matrix = svg.getScreenCTM()!;
     const box = (selector: string) => el.querySelector(selector)!.getBoundingClientRect().toJSON();
     return {sx: Math.hypot(matrix.a, matrix.b), sy: Math.hypot(matrix.c, matrix.d),
      canvas: box('.collab-canvas'), card: box('.collab-share-card'), window: box('.collab-window')};
    });
    // Uniform scale keeps tissue coordinates, label glyphs and rectangle corners intact.
    expect(geometry.sx).toBeCloseTo(geometry.sy, 5);
    heights.push(geometry.window.height);
    if (step === 0) {
     expect(geometry.card.x).toBeGreaterThanOrEqual(geometry.canvas.x);
     expect(geometry.card.y).toBeGreaterThanOrEqual(geometry.canvas.y);
     expect(geometry.card.right).toBeLessThanOrEqual(geometry.canvas.right);
     expect(geometry.card.bottom).toBeLessThanOrEqual(geometry.canvas.bottom);
    }
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
   }
   expect(new Set(heights).size).toBe(1);
  }
 });
}

for (const width of [320, 1440]) {
 test(`homepage routes are explicit links and language remains independent at ${width}px`, async ({page}) => {
  await page.setViewportSize({width, height: 900});
  await page.goto('/');
  const china = page.locator('[data-route="cn"]');
  const international = page.locator('[data-route="com"]');
  await expect(china).toHaveText('中国线路');
  await expect(china).toHaveAttribute('href', 'https://histopilot.cn/');
  await expect(international).toHaveText('国际线路');
  await expect(international).toHaveAttribute('href', 'https://histopilot.com/');
  for (const link of [china, international]) {
   const box = await link.boundingBox();
   expect(box!.height).toBeGreaterThanOrEqual(44);
   expect(box!.x + box!.width).toBeLessThanOrEqual(width);
  }
  await page.locator('#account-btn').click();
  await page.locator('.lang-toggle').click();
  await expect(china).toHaveText('China route');
  await expect(international).toHaveText('International route');
  await expect(china).toHaveAttribute('href', 'https://histopilot.cn/');
  expect(new URL(page.url()).hostname).toBe('127.0.0.1');
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
 });
}
