#!/usr/bin/env python3
"""Phase 0: stage one random file through bound A parts, time homepc COS GET, clean exact version.

Signed GET URLs cross SSH stdin only. No credentials or URLs are printed or persisted.
"""
import argparse
import hashlib
import json
import shlex
import subprocess
import sys
import time
import urllib.error
import xml.etree.ElementTree as ET
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import poc_config
from cos_xml_signing import presign_url, signed_request
from presign_parts import initiate_multipart, abort_multipart, build_part_url, compute_plan, put_presigned

REMOTE = r'''
import hashlib,json,sys,time,urllib.request
p=json.loads(sys.stdin.readline())
h=hashlib.sha256(); n=0; start=time.monotonic()
try:
 with urllib.request.urlopen(p['url'],timeout=600) as response:
  status=response.status
  while True:
   data=response.read(1048576)
   if not data:break
   h.update(data);n+=len(data)
 elapsed=time.monotonic()-start
 print(json.dumps({'status':status,'bytes':n,'seconds':round(elapsed,3),'mbps':round(n*8/elapsed/1e6,3),'sha_match':h.hexdigest()==p['sha'],'size_match':n==p['size']}))
except Exception as e:
 print(json.dumps({'error':type(e).__name__,'bytes':n,'seconds':round(time.monotonic()-start,3)}))
 sys.exit(1)
'''

def call(cfg, method, key, query=None, body=None, headers=None):
    return signed_request(scheme='https',host=cfg.cos_host,method=method,key=key,query=query,body=body,headers=headers,secret_id=cfg.secret_id,secret_key=cfg.secret_key,expires_in=900,timeout=300)

def run(path, expected_sha, repeats, stage_only=False):
    cfg=poc_config.load_config(require=True)
    key=poc_config.random_key(cfg.prefix,label='bench-a')
    upload_id=None;version=None
    try:
        size=path.stat().st_size
        upload_id=initiate_multipart(cfg,key)
        parts=[]
        stage_started=time.monotonic()
        put_seconds=0.0
        with path.open('rb') as f:
            for part in compute_plan(size,32_000_000):
                data=f.read(part['length'])
                url,headers=build_part_url(cfg,key,upload_id,part['part_number'],part['length'],True,900)
                # urllib's response is needed for ETag. Keep all signature material in memory.
                import urllib.request
                req=urllib.request.Request(url,data=data,method='PUT')
                put_started=time.monotonic()
                with urllib.request.urlopen(req,timeout=300) as resp:
                    put_seconds+=time.monotonic()-put_started
                    etag=resp.headers.get('ETag')
                    if resp.status!=200 or not etag:raise RuntimeError('UploadPart status/ETag invalid')
                parts.append((part['part_number'],etag))
        root=ET.Element('CompleteMultipartUpload')
        for number,etag in parts:
            p=ET.SubElement(root,'Part');ET.SubElement(p,'PartNumber').text=str(number);ET.SubElement(p,'ETag').text=etag
        body=ET.tostring(root,encoding='utf-8')
        with call(cfg,'POST',key,query={'uploadId':upload_id},body=body,headers={'content-type':'application/xml'}) as resp:
            result=resp.read();version=resp.headers.get('x-cos-version-id')
            if resp.status!=200 or b'<Error>' in result or not version:raise RuntimeError('Complete response/version invalid')
        upload_id=None
        stage_seconds=time.monotonic()-stage_started
        print('staged',path.name,'bytes',size,'parts',len(parts),'version_present',bool(version),'stage_seconds',round(stage_seconds,3),'stage_mbps',round(size*8/stage_seconds/1e6,3),'put_seconds',round(put_seconds,3),'put_mbps',round(size*8/put_seconds/1e6,3),flush=True)
        if stage_only:return
        url,_=presign_url(scheme='https',host=cfg.cos_host,method='GET',key=key,query={'versionId':version},secret_id=cfg.secret_id,secret_key=cfg.secret_key,expires_in=900)
        for attempt in range(1,repeats+1):
            payload=json.dumps({'url':url,'sha':expected_sha,'size':size})+'\n'
            proc=subprocess.run(['ssh','-o','BatchMode=yes','-o','ConnectTimeout=8','homepc','python3 -c '+shlex.quote(REMOTE)],input=payload,text=True,capture_output=True,timeout=660)
            if proc.returncode!=0:raise RuntimeError('homepc download failed: '+(proc.stdout or proc.stderr)[:160])
            result=json.loads(proc.stdout.strip())
            print('homepc',path.name,'attempt',attempt,json.dumps(result,sort_keys=True),flush=True)
            if not (result.get('status')==200 and result.get('sha_match') and result.get('size_match')):raise RuntimeError('download integrity failed')
    finally:
        if upload_id:
            try:print('abort',abort_multipart(cfg,key,upload_id),flush=True)
            except Exception as e:print('ABORT_FAILED',type(e).__name__,flush=True)
        if version:
            try:
                with call(cfg,'DELETE',key,query={'versionId':version}) as resp:
                    resp.read();print('delete_exact_version',resp.status,flush=True)
            except Exception as e:print('DELETE_FAILED',type(e).__name__,flush=True)

if __name__=='__main__':
    a=argparse.ArgumentParser();a.add_argument('--manifest',required=True);a.add_argument('--size',type=int,required=True);a.add_argument('--repeats',type=int,default=1);a.add_argument('--stage-only',action='store_true');args=a.parse_args()
    m=Path(args.manifest);entries=json.loads(m.read_text())['files'];entry=next(x for x in entries if x['size_bytes']==args.size)
    run(m.parent/entry['filename'],entry['sha256'],args.repeats,args.stage_only)
