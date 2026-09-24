// Single-use production-origin CORS probe with CSP bypass in isolated Playwright browser.
// Signed URL received through stdin only; never logged or persisted.
const {chromium}=require('../../../node_modules/playwright');
(async()=>{
 const raw=await new Promise((resolve,reject)=>{let s='';process.stdin.setEncoding('utf8');process.stdin.on('data',d=>s+=d);process.stdin.on('end',()=>resolve(s));process.stdin.on('error',reject)});
 const p=JSON.parse(raw);
 const browser=await chromium.launch({headless:true,executablePath:'/usr/bin/google-chrome',args:['--no-sandbox','--disable-dev-shm-usage']});
 try {
  const context=await browser.newContext({bypassCSP:true});const page=await context.newPage();
  const nav=await page.goto(p.origin+'/',{waitUntil:'domcontentloaded',timeout:20000});
  const out=await page.evaluate(async x=>{
   try{const r=await fetch(x.url,{method:'PUT',body:new Uint8Array(x.size),credentials:'omit',mode:'cors'});return {ok:r.ok,status:r.status,etag_visible:!!r.headers.get('ETag')}}
   catch(e){return {ok:false,error:e.name+':'+e.message}}
  },p);
  console.log(JSON.stringify({origin:p.origin,page_status:nav.status(),...out,csp_bypassed:true}));
  if(!out.ok||!out.etag_visible)process.exitCode=1;
 }finally{await browser.close()}
})().catch(e=>{console.error(e.name,e.message.slice(0,150));process.exitCode=2});
