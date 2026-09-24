// Phase 0 browser→platform/frp benchmark. Password read once from stdin, never logged or stored.
const { chromium } = require('../../../node_modules/playwright');
const fs = require('fs');
const origin = process.argv[2] || 'https://pt.solarise94.fun';
const files = process.argv.slice(3);
const loginId = process.env.COS_POC_V2_LOGIN_ID;
if (!loginId || !files.length) throw Error('COS_POC_V2_LOGIN_ID and test files required');
function passwordFromStdin() { return new Promise((resolve,reject)=>{let s='';process.stdin.setEncoding('utf8');process.stdin.on('data',d=>{s+=d;if(s.includes('\n')){process.stdin.pause();resolve(s.split('\n')[0].replace(/\r$/,''))}});process.stdin.on('end',()=>reject(Error('no password')))}); }
(async()=>{
 let password=await passwordFromStdin();
 const browser=await chromium.launch({headless:true,executablePath:'/usr/bin/google-chrome',args:['--no-sandbox','--disable-dev-shm-usage']});
 const page=await browser.newPage();
 try {
  await page.goto(origin+'/login',{waitUntil:'domcontentloaded',timeout:20000});
  await page.locator('#login-dialog-username').fill(loginId);
  await page.locator('#login-dialog-password').fill(password);password='';
  await Promise.all([page.waitForURL(/\/app(?:\?|$)/,{timeout:20000}),page.locator('#login-dialog-form button[type=submit]').click()]);
  console.log('login_ok',origin);
  await page.evaluate(()=>{const i=document.createElement('input');i.type='file';i.id='poc-v2-file';i.hidden=true;document.body.appendChild(i)});
  for(const path of files){
   await page.locator('#poc-v2-file').setInputFiles(path);
   const name=path.split('/').pop();
   const result=await page.evaluate(async(name)=>{
    const file=document.getElementById('poc-v2-file').files[0];
    if(!file||file.name!==name)throw Error('file unavailable');
    const token=decodeURIComponent((document.cookie.match(/(?:^|;\s*)csrf_token=([^;]+)/)||[])[1]||'');
    if(!token)throw Error('csrf unavailable');
    let id=null,offset=0,chunkSize=0;const parts=[];const started=performance.now();
    try {
     const r=await fetch('/api/uploads',{method:'POST',headers:{'Content-Type':'application/json','X-CSRF-Token':token},body:JSON.stringify({filename:name.replace(/\.bin$/,'.tif'),declared_size:file.size})});
     const body=await r.json();if(!r.ok||!body.upload_id)throw Error('create '+r.status+' '+(body.code||''));
     id=body.upload_id;chunkSize=body.chunk_size;
     while(offset<file.size){
      const buffer=await file.slice(offset,Math.min(file.size,offset+chunkSize)).arrayBuffer();
      const digest=await crypto.subtle.digest('SHA-256',buffer);
      const sha=Array.from(new Uint8Array(digest),x=>x.toString(16).padStart(2,'0')).join('');
      const t0=performance.now();
      const put=await fetch('/api/uploads/'+encodeURIComponent(id)+'/chunk?offset='+offset+'&sha256='+sha,{method:'PUT',headers:{'Content-Type':'application/octet-stream','X-CSRF-Token':token},body:buffer});
      const seconds=(performance.now()-t0)/1000;
      const reply=await put.json();if(!put.ok)throw Error('chunk '+put.status+' '+(reply.code||''));
      parts.push({bytes:buffer.byteLength,seconds});offset+=buffer.byteLength;
     }
     return {ok:true,bytes:file.size,parts:parts.length,chunk_size:chunkSize,put_seconds:parts.reduce((v,p)=>v+p.seconds,0),wall_seconds:(performance.now()-started)/1000};
    }catch(e){return {ok:false,error:String(e).slice(0,160),uploaded_bytes:offset,parts:parts.length}}
    finally{if(id){try{const d=await fetch('/api/uploads/'+encodeURIComponent(id),{method:'DELETE',headers:{'X-CSRF-Token':token}});window.__pocCleanupStatus=d.status}catch(e){window.__pocCleanupStatus='network_error'}}}
   },name);
   const cleanup=await page.evaluate(()=>window.__pocCleanupStatus);
   console.log(JSON.stringify({file:name,...result,cleanup_status:cleanup}));
   if(!result.ok||cleanup!==200){process.exitCode=1;break}
  }
 } finally { await browser.close() }
})().catch(e=>{console.error('benchmark_error',e.name,e.message.slice(0,160));process.exitCode=2});
