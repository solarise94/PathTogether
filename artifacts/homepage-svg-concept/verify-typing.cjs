const {chromium}=require('@playwright/test');
const assert=require('node:assert/strict');
(async()=>{
 const browser=await chromium.launch();
 try {
  const p=await browser.newPage({viewport:{width:1440,height:1100}});
  await p.clock.install();await p.goto('http://127.0.0.1:8921');
  const read=()=>p.locator('#note-title').textContent();
  await p.clock.runFor(8100);assert.equal(await p.locator('#tissue').getAttribute('data-scene'),'3');
  const first=await read();assert(first.length>0&&first.length<18);
  await p.clock.runFor(1600);assert((await p.locator('#note-body').textContent()).length>0);
  await p.locator('#toggle').click();const paused=await p.locator('#note-body').textContent();
  await p.clock.runFor(1000);assert.equal(await p.locator('#note-body').textContent(),paused);
  await p.locator('#replay').click();
  for(let i=0;i<3;i++)await p.locator('#next').click();
  assert.equal(await p.locator('#tissue').getAttribute('data-scene'),'3');
  assert.equal(await p.locator('#note-body').textContent(),'');
  await p.clock.runFor(800);const partial=await read();assert(partial.length>0&&partial.length<18);
  await p.clock.runFor(1600);assert((await p.locator('#note-body').textContent()).length>0);
  await p.clock.runFor(5000);assert.equal(await p.locator('#tissue').getAttribute('data-scene'),'3');
  assert.equal(await p.locator('#toggle').textContent(),'继续');
  for(let i=0;i<4;i++)await p.locator('#next').click();
  assert(await p.locator('#review-a').isVisible());assert(await p.locator('#review-b').isVisible());
  console.log('PASS: automatic title/body typing; pause; manual entry starts empty, types, stays on scene; overview retains both notes');
 }finally{await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
