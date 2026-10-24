const {chromium} = require('playwright');
const fs = require('node:fs');
const RUN = process.env.REVIEW_RUN;
const BASE = process.env.REVIEW_BASE_URL;

(async () => {
  const browser = await chromium.launch({headless: true});
  try {
    const context = await browser.newContext({viewport: {width:1280,height:900}, locale:'zh-CN'});
    await context.addInitScript(() => localStorage.setItem('hp_lang','zh'));
    const page = await context.newPage();
    await page.route('**/support/pay/*/qr', r => r.fulfill({status:200,contentType:'text/html',body:'<!doctype html><body></body>'}));
    await page.goto(BASE + '/support');
    await page.locator('input[name="amount"][value="6.00"]').check({force:true});
    let response = page.waitForResponse(r => r.url().endsWith('/support/orders') && r.request().method()==='POST');
    await page.click('#pay-pick-submit');
    const original = await (await response).json();
    await page.waitForFunction(() => !document.getElementById('pay-modal-expired').hidden);
    response = page.waitForResponse(r => r.url().endsWith('/regenerate'));
    await page.click('#pay-modal-regenerate');
    const replacement = await (await response).json();
    await page.waitForFunction(ref => document.querySelector('#pay-modal-frame-host iframe').getAttribute('src').includes(ref), replacement.out_trade_no);
    await page.click('#pay-modal-close');
    await page.click('#pay-pick-submit');
    await page.waitForFunction(() => !document.getElementById('pay-modal').hidden);
    const reopenedFrame = await page.locator('#pay-modal-frame-host iframe').getAttribute('src');
    let extra = null;
    if (reopenedFrame.includes(original.out_trade_no)) {
      await page.waitForFunction(() => !document.getElementById('pay-modal-expired').hidden);
      response = page.waitForResponse(r => r.url().endsWith('/regenerate'));
      await page.click('#pay-modal-regenerate');
      extra = await (await response).json();
    }
    const result = {original:original.out_trade_no,replacement:replacement.out_trade_no,reopenedFrame,
      extra:extra && extra.out_trade_no,hiddenKey:await page.locator('[name="idempotency_key"]').inputValue()};
    fs.writeFileSync(RUN + '/real-browser-result.json',JSON.stringify(result,null,2));
    console.log(JSON.stringify(result));
  } finally {
    await browser.close();
  }
})().catch(e => {console.error(e.stack);process.exitCode=1;});
