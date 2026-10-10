"""Real Gunicorn 23 + production config; only predictor replaced with a slow stub."""
import base64
import concurrent.futures
import io
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
import urllib.request
import urllib.error
from PIL import Image

HERE = Path(__file__).parent
SERVICE = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else Path.cwd() / 'services/contour-refine'
EVENTS = HERE / 'events.jsonl'
EVENTS.write_text('')
(HERE / 'slow_app.py').write_text('''
import json, os, time
import numpy as np
from flask import request
import app as service
def predict(img, box):
    rid = request.headers.get('X-Probe-ID')
    with open(os.environ['PROBE_EVENTS'], 'a') as f:
        f.write(json.dumps({'id':rid,'started':time.monotonic()}) + '\\n')
    time.sleep(12 if rid == 'first' else 0.02)
    return np.zeros(img.shape[:2], dtype=bool), 0.0
service._MODEL.inject(predict)
app = service.app
''')
with socket.socket() as s:
    s.bind(('127.0.0.1', 0))
    port = s.getsockname()[1]
url = f'http://127.0.0.1:{port}'
env = dict(os.environ, CONTOUR_REFINE_EAGER_LOAD='0', PROBE_EVENTS=str(EVENTS),
           PYTHONPATH=os.pathsep.join([str(HERE/'deps'), str(HERE), str(SERVICE)]))
env.pop('CONTOUR_REFINE_TOKEN', None)
log = open(HERE/'gunicorn.log', 'w')
p = subprocess.Popen([sys.executable, '-m', 'gunicorn', '--config', str(SERVICE/'gunicorn.conf.py'),
                      '--bind', f'127.0.0.1:{port}', 'slow_app:app'], cwd=HERE, env=env,
                     stdout=log, stderr=log)
try:
    for _ in range(100):
        try:
            with urllib.request.urlopen(url+'/healthz', timeout=.2) as r:
                if r.status == 200: break
        except Exception: time.sleep(.05)
    else: raise RuntimeError('server failed to become ready')
    img = Image.new('RGB', (64,64), 'white')
    buf=io.BytesIO(); img.save(buf,format='PNG')
    body=json.dumps({'image_png_b64':base64.b64encode(buf.getvalue()).decode(),
                     'polygon':[[10,10],[50,10],[50,50],[10,50]]}).encode()
    t0=time.monotonic()
    def send(rid):
        start=time.monotonic()
        req=urllib.request.Request(url+'/v1/refine', data=body,
            headers={'Content-Type':'application/json','X-Probe-ID':rid})
        try:
            with urllib.request.urlopen(req,timeout=8) as r: status=r.status
        except urllib.error.HTTPError as e: status=e.code
        except TimeoutError: status='client_timeout'
        return {'id':rid,'status':status,'elapsed':round(time.monotonic()-start,3)}
    with concurrent.futures.ThreadPoolExecutor(max_workers=12) as pool:
        first=pool.submit(send,'first')
        while not EVENTS.read_text(): time.sleep(.01)
        futures=[first]+[pool.submit(send,str(i)) for i in range(11)]
        results=[f.result() for f in futures]
    time.sleep(max(0,14-(time.monotonic()-t0)))
    starts=[json.loads(s) for s in EVENTS.read_text().splitlines()]
    for e in starts: e['started']=round(e['started']-t0,3)
    timed_out={r['id'] for r in results if r['status']=='client_timeout'}
    late=[e for e in starts if e['id'] in timed_out and e['started']>8.1]
    print(json.dumps({'gunicorn':'23.0.0','threads':2,'lock_wait_seconds':2,
                      'client_timeout_seconds':8,'clients':results,
                      'inference_starts':starts,'late_inference':late},indent=2))
    assert not late, f'{len(late)} abandoned requests began inference after the client timeout'
finally:
    p.terminate()
    try: p.wait(timeout=5)
    except subprocess.TimeoutExpired: p.kill(); p.wait()
    log.close()
