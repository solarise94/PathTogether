# -*- coding: utf-8 -*-
"""真实 gunicorn（正式 gunicorn.conf.py）下的并发回归：客户端超时后，排队中的请求不得开始推理。

只替换 predictor：首个请求慢推理占锁，其余请求以短客户端超时并发打进来。
gunicorn 未安装时跳过。
"""
import base64
import concurrent.futures
import io
import json
import os
import socket
import subprocess
import sys
import textwrap
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest
from PIL import Image

pytest.importorskip("gunicorn")

SERVICE = Path(__file__).resolve().parents[1]
FIRST_INFERENCE_S = 3.0
CLIENT_TIMEOUT_S = 1.5
WAITING_CLIENTS = 8


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_no_inference_starts_after_client_timeout(tmp_path):
    events = tmp_path / "events.jsonl"
    events.write_text("")
    (tmp_path / "slow_app.py").write_text(textwrap.dedent("""
        import json, os, time
        import numpy as np
        from flask import request
        import app as service
        def predict(img, box):
            rid = request.headers.get("X-Probe-ID")
            with open(os.environ["PROBE_EVENTS"], "a") as f:
                f.write(json.dumps({"id": rid, "started": time.time()}) + "\\n")
            time.sleep(%r if rid == "first" else 0.02)
            return np.zeros(img.shape[:2], dtype=bool), 0.0
        service._MODEL.inject(predict)
        app = service.app
    """ % FIRST_INFERENCE_S))
    port = _free_port()
    url = "http://127.0.0.1:%d" % port
    env = dict(os.environ, CONTOUR_REFINE_EAGER_LOAD="0", PROBE_EVENTS=str(events),
               PYTHONPATH=os.pathsep.join([str(tmp_path), str(SERVICE)]))
    env.pop("CONTOUR_REFINE_TOKEN", None)
    log = open(tmp_path / "gunicorn.log", "w")
    proc = subprocess.Popen(
        [sys.executable, "-m", "gunicorn", "--config", str(SERVICE / "gunicorn.conf.py"),
         "--bind", "127.0.0.1:%d" % port, "slow_app:app"],
        cwd=tmp_path, env=env, stdout=log, stderr=log)
    try:
        deadline = time.time() + 20
        while True:
            try:
                with urllib.request.urlopen(url + "/healthz", timeout=0.2) as r:
                    if r.status == 200:
                        break
            except Exception:  # noqa: BLE001
                if time.time() > deadline:
                    raise RuntimeError((tmp_path / "gunicorn.log").read_text())
                time.sleep(0.05)

        buf = io.BytesIO()
        Image.new("RGB", (64, 64), "white").save(buf, format="PNG")
        body = json.dumps({"image_png_b64": base64.b64encode(buf.getvalue()).decode(),
                           "polygon": [[10, 10], [50, 10], [50, 50], [10, 50]]}).encode()

        def send(rid, timeout):
            req = urllib.request.Request(url + "/v1/refine", data=body, headers={
                "Content-Type": "application/json", "X-Probe-ID": rid})
            sent = time.time()
            try:
                with urllib.request.urlopen(req, timeout=timeout) as r:
                    return rid, r.status, sent
            except urllib.error.HTTPError as e:
                return rid, e.code, sent
            except (TimeoutError, urllib.error.URLError):
                return rid, "client_timeout", sent

        with concurrent.futures.ThreadPoolExecutor(max_workers=WAITING_CLIENTS + 1) as pool:
            first = pool.submit(send, "first", FIRST_INFERENCE_S + 5)
            while not events.read_text():
                time.sleep(0.01)
            waiting = [pool.submit(send, str(i), CLIENT_TIMEOUT_S) for i in range(WAITING_CLIENTS)]
            results = [f.result() for f in waiting]
            assert first.result()[1] == 200
        time.sleep(0.5)

        starts = {e["id"]: e["started"] for e in map(json.loads, events.read_text().splitlines())}
        late = [(rid, status, starts[rid] - sent) for rid, status, sent in results
                if rid in starts and starts[rid] - sent > CLIENT_TIMEOUT_S]
        assert not late, ("requests began inference after their client gave up", late)
        assert all(status == 503 for _, status, _ in results), results
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
        log.close()
