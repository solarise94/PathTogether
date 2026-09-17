const {chromium,expect}=require('@playwright/test');
const assert=require('node:assert/strict');
(async()=>{
 const browser=await chromium.launch();
 try {
  const p=await browser.newPage({viewport:{width:1440,height:1100}});
  await p.clock.install();await p.goto('http://127.0.0.1:8921');
  await p.clock.runFor(8000);
  assert.equal(await p.locator('#tissue').getAttribute('data-scene'),'3');
  const first=await p.locator('#note-body').textContent();
  await p.clock.runFor(500);
  const next=await p.locator('#note-body').textContent();
  assert(first.length>0&&next.length>first.length);
  await p.locator('#toggle').click();
  const paused=await p.locator('#note-body').textContent();
  await p.clock.runFor(700);
  assert.equal(await p.locator('#note-body').textContent(),paused);
  await p.emulateMedia({reducedMotion:'reduce'});await p.clock.runFor(100);
  await expect(p.locator('#tissue')).toHaveAttribute('data-scene','7');
  for(const key of ['a','b']){
   assert(await p.locator('#review-'+key).isVisible());
   assert.equal(await p.locator(`[data-mark="${key}"]`).getAttribute('visibility'),'visible');
  }
  await p.screenshot({path:'artifacts/homepage-svg-concept/review-two-notes.png',fullPage:true});
  await p.setViewportSize({width:390,height:844});await p.clock.runFor(100);
  const box=await p.locator('.canvas').boundingBox();const boxes=[];
  for(const key of ['a','b']){
   const r=await p.locator('#review-'+key).boundingBox();
   assert(r.x>=box.x&&r.x+r.width<=box.x+box.width);
   assert(r.y>=box.y&&r.y+r.height<=box.y+box.height);boxes.push(r);
  }
  assert(boxes[0].y+boxes[0].height<=boxes[1].y);
  await p.screenshot({path:'artifacts/homepage-svg-concept/review-mobile.png',fullPage:true});
  console.log('PASS progressive text, pause, both markers and labels retained, mobile bounds and non-overlap');
 }finally{await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
