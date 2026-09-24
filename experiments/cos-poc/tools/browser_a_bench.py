#!/usr/bin/env python3
"""Phase 0 browser→COS A-path benchmark; localhost-only control API, no persistent URLs."""
import argparse
import hashlib
import json
import secrets
import subprocess
import sys
import threading
import time
import urllib.error
import xml.etree.ElementTree as ET
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parent))
import poc_config
from cos_xml_signing import signed_request
from presign_parts import abort_multipart, build_part_url, compute_plan, initiate_multipart

ROOT = Path(__file__).resolve().parents[1]
HTML = r'''<!doctype html><meta charset="utf-8"><title>A browser benchmark</title><pre id="status">starting</pre><script>
(async()=>{
 let result={ok:false};const metrics=[];const started=performance.now();
 try{
  const cfg=await (await fetch('/config')).json();
  for(let i=0;i<cfg.parts.length;i++){
   const part=cfg.parts[i];
   const data=await (await fetch('/data/'+i)).arrayBuffer();
   if(data.byteLength!==part.length)throw Error('local data length mismatch');
   const auth=await (await fetch('/sign/'+i,{headers:{'X-PoC-Token':cfg.token}})).json();
   const t0=performance.now();
   const resp=await fetch(auth.url,{method:'PUT',body:data,credentials:'omit',mode:'cors'});
   const seconds=(performance.now()-t0)/1000;
   if(!resp.ok)throw Error('COS PUT status '+resp.status);
   const etag=resp.headers.get('ETag');if(!etag)throw Error('ETag not exposed');
   metrics.push({number:i+1,bytes:part.length,seconds,etag});
   document.getElementById('status').textContent='uploaded '+(i+1)+'/'+cfg.parts.length;
  }
  const cr=await fetch('/complete',{method:'POST',headers:{'Content-Type':'application/json','X-PoC-Token':cfg.token},body:JSON.stringify({parts:metrics.map(x=>({number:x.number,etag:x.etag}))})});
  const completed=await cr.json();if(!cr.ok||!completed.ok)throw Error('complete failed '+(completed.error||cr.status));
  result={ok:true,parts:metrics.length,bytes:cfg.size,put_seconds:metrics.reduce((n,x)=>n+x.seconds,0),wall_seconds:(performance.now()-started)/1000,version_present:completed.version_present};
 }catch(e){result={ok:false,error:String(e).slice(0,160),parts:metrics.length,bytes:metrics.reduce((n,x)=>n+x.bytes,0)}}
 window.__done=result;document.getElementById('status').textContent=JSON.stringify(result);
})();
</script>'''

class Handler(BaseHTTPRequestHandler):
    server_version='PocABrowserBench/1'
    def log_message(self,*args): pass
    @property
    def state(self):return self.server.state
    def reply(self,status,body,content_type='application/json'):
        if isinstance(body,(dict,list)):body=json.dumps(body).encode()
        elif isinstance(body,str):body=body.encode()
        self.send_response(status);self.send_header('Content-Type',content_type);self.send_header('Content-Length',str(len(body)))
        self.send_header('Cache-Control','no-store');self.send_header('Content-Security-Policy',"default-src 'none'; script-src 'unsafe-inline'; connect-src 'self' https://"+self.state.cfg.cos_host+"; style-src 'none'")
        self.end_headers();self.wfile.write(body)
    def authorized(self):return self.headers.get('X-PoC-Token')==self.state.token and self.headers.get('Origin','http://127.0.0.1:8765')=='http://127.0.0.1:8765'
    def do_GET(self):
        path=urlsplit(self.path).path
        if path=='/':return self.reply(200,HTML,'text/html; charset=utf-8')
        if path=='/config':return self.reply(200,{'parts':[{'length':x['length']} for x in self.state.plan],'size':self.state.size,'token':self.state.token})
        if path.startswith('/data/'):
            try:i=int(path.rsplit('/',1)[1]);part=self.state.plan[i]
            except (ValueError,IndexError):return self.reply(404,{'error':'part_not_found'})
            with self.state.path.open('rb') as f:f.seek(part['offset']);data=f.read(part['length'])
            return self.reply(200,data,'application/octet-stream')
        if path.startswith('/sign/'):
            if not self.authorized():return self.reply(403,{'error':'forbidden'})
            try:i=int(path.rsplit('/',1)[1]);part=self.state.plan[i]
            except (ValueError,IndexError):return self.reply(404,{'error':'part_not_found'})
            url,_=build_part_url(self.state.cfg,self.state.key,self.state.upload_id,part['part_number'],part['length'],True,900)
            return self.reply(200,{'url':url})
        return self.reply(404,{'error':'not_found'})
    def do_POST(self):
        if self.path!='/complete' or not self.authorized():return self.reply(403,{'error':'forbidden'})
        try:
            n=int(self.headers.get('Content-Length','0'))
            if n<=0 or n>200000:raise ValueError('invalid request size')
            reported=json.loads(self.rfile.read(n))['parts']
            plan=self.state.plan
            if [p['number'] for p in reported]!=[p['part_number'] for p in plan]:raise ValueError('part numbers differ')
            listed=self.state.call('GET',query={'uploadId':self.state.upload_id})
            root=ET.fromstring(listed.read())
            remote=[]
            for x in root.iter():
                if x.tag.rsplit('}',1)[-1]=='Part':
                    remote.append((int(next(e.text for e in x if e.tag.rsplit('}',1)[-1]=='PartNumber')),next(e.text for e in x if e.tag.rsplit('}',1)[-1]=='ETag'),int(next(e.text for e in x if e.tag.rsplit('}',1)[-1]=='Size'))))
            if remote!=[(x['part_number'],r['etag'],x['length']) for x,r in zip(plan,reported)]:raise ValueError('ListParts mismatch')
            body=ET.Element('CompleteMultipartUpload')
            for r in reported:
                p=ET.SubElement(body,'Part');ET.SubElement(p,'PartNumber').text=str(r['number']);ET.SubElement(p,'ETag').text=r['etag']
            with self.state.call('POST',query={'uploadId':self.state.upload_id},body=ET.tostring(body,encoding='utf-8'),headers={'content-type':'application/xml'}) as resp:
                raw=resp.read();version=resp.headers.get('x-cos-version-id')
                if resp.status!=200 or b'<Error>' in raw or not version:raise ValueError('complete invalid')
            self.state.version=version;self.state.upload_id=None
            return self.reply(200,{'ok':True,'version_present':True})
        except Exception as e:return self.reply(502,{'ok':False,'error':type(e).__name__})

class State:
    def __init__(self,cfg,path):
        self.cfg=cfg;self.path=path;self.size=path.stat().st_size;self.plan=compute_plan(self.size,32_000_000)
        self.key=poc_config.random_key(cfg.prefix,label='browser-a');self.token=secrets.token_urlsafe(24)
        self.upload_id=initiate_multipart(cfg,self.key);self.version=None
    def call(self,method,query=None,body=None,headers=None):
        return signed_request(scheme='https',host=self.cfg.cos_host,method=method,key=self.key,query=query,body=body,headers=headers,secret_id=self.cfg.secret_id,secret_key=self.cfg.secret_key,expires_in=900,timeout=300)
    def cleanup(self):
        if self.upload_id:
            try:print('abort',abort_multipart(self.cfg,self.key,self.upload_id),flush=True)
            except Exception as e:print('ABORT_FAILED',type(e).__name__,flush=True)
        if self.version:
            try:
                with self.call('DELETE',query={'versionId':self.version}) as resp:resp.read();print('delete_exact_version',resp.status,flush=True)
            except Exception as e:print('DELETE_FAILED',type(e).__name__,flush=True)

def main():
    p=argparse.ArgumentParser();p.add_argument('--file',type=Path,required=True);p.add_argument('--port',type=int,default=8765);p.add_argument('--browser',choices=['chromium','firefox'],default='chromium');a=p.parse_args()
    cfg=poc_config.load_config(require=True);state=State(cfg,a.file)
    server=ThreadingHTTPServer(('127.0.0.1',a.port),Handler);server.state=state
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    try:
        node=ROOT/'tools'/'browser_a_bench.js'
        proc=subprocess.run(['node',str(node),f'http://127.0.0.1:{a.port}/',a.browser],capture_output=True,text=True,timeout=900)
        print('browser',a.browser,'exit',proc.returncode,'result',proc.stdout.strip()[:500],flush=True)
        if proc.stderr:print('browser_stderr',proc.stderr.strip()[:400],flush=True)
        return proc.returncode
    finally:server.shutdown();state.cleanup()
if __name__=='__main__':sys.exit(main())
