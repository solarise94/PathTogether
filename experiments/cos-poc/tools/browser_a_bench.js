const { chromium, firefox } = require('../../../node_modules/playwright');
const [url, which] = process.argv.slice(2);
(async()=>{
  const browser = await (which==='firefox'?firefox:chromium).launch({headless:true, executablePath:which==='firefox'?'/usr/bin/firefox':'/usr/bin/google-chrome', args:which==='firefox'?[]:['--no-sandbox','--disable-dev-shm-usage']});
  const page = await browser.newPage();
  try {
    await page.goto(url,{waitUntil:'domcontentloaded'});
    await page.waitForFunction(()=>window.__done!==undefined,null,{timeout:600000});
    const result=await page.evaluate(()=>window.__done);
    console.log(JSON.stringify(result));
    if(!result.ok)process.exitCode=1;
  } finally {await browser.close()}
})().catch(e=>{console.error(e.name, e.message.slice(0,200));process.exitCode=2});
