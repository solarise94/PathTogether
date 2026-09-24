#!/usr/bin/env python3
"""Phase 0: real browser PUT from each production Origin, bypassing CSP only in isolated browser.

This verifies CORS; it does NOT satisfy production CSP readiness. All parts are aborted.
"""
import json
import subprocess
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parent))
import poc_config
from presign_parts import initiate_multipart,abort_multipart,build_part_url

ORIGINS=('https://pt.solarise94.fun','https://histopilot.com','https://histopilot.cn')

def main():
 cfg=poc_config.load_config(require=True)
 rc=0
 for origin in ORIGINS:
  key=poc_config.random_key(cfg.prefix,label='origin-probe')
  upload_id=initiate_multipart(cfg,key)
  try:
   url,_=build_part_url(cfg,key,upload_id,1,1_000_000,True,300)
   payload=json.dumps({'origin':origin,'url':url,'size':1_000_000})
   node=Path(__file__).with_suffix('.js')
   run=subprocess.run(['node',str(node)],input=payload,text=True,capture_output=True,timeout=60)
   print(run.stdout.strip()[:300] if run.stdout else origin+' browser_failed '+run.stderr.strip()[:120],flush=True)
   if run.returncode:rc=1
  finally:
   try:print(origin,'abort',abort_multipart(cfg,key,upload_id),flush=True)
   except Exception as e:print(origin,'ABORT_FAILED',type(e).__name__,flush=True);rc=1
 return rc
if __name__=='__main__':raise SystemExit(main())
