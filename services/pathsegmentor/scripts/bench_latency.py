#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""暖态延迟与并发行为测量（/v1/segment，1024² 输入）。

用法：
  python3 scripts/bench_latency.py [--host 127.0.0.1] [--port 8971] \
      [--requests 12] [--image path/to/1024.png]

输出：逐次耗时、p50/p95、2 路并发时第二路是否 503 busy。
"""
import argparse
import base64
import io
import json
import statistics
import sys
import threading
import time
import urllib.error
import urllib.request

import numpy as np
from PIL import Image


def post_segment(base_url, image_b64, timeout=300):
    body = {"image_b64": image_b64, "object": "nuclei", "region": "liver"}
    request = urllib.request.Request(
        base_url + "/v1/segment",
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST")
    started = time.monotonic()
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
            return response.status, payload, (time.monotonic() - started) * 1000.0
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8")), \
            (time.monotonic() - started) * 1000.0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8971)
    parser.add_argument("--requests", type=int, default=12)
    parser.add_argument("--image", default=None, help="1024² 输入图；缺省合成噪声图")
    args = parser.parse_args()
    base_url = "http://%s:%d" % (args.host, args.port)

    if args.image:
        image = Image.open(args.image).convert("RGB")
    else:
        rng = np.random.RandomState(7)
        image = Image.fromarray(
            rng.randint(0, 256, (1024, 1024, 3), dtype=np.uint8), "RGB")
    buf = io.BytesIO()
    image.save(buf, format="PNG")
    image_b64 = base64.b64encode(buf.getvalue()).decode("ascii")

    wall_ms, server_ms = [], []
    for index in range(args.requests):
        status, payload, elapsed = post_segment(base_url, image_b64)
        if status != 200:
            print("request %d: HTTP %d %s" % (index, status, payload),
                  file=sys.stderr)
            return 1
        wall_ms.append(elapsed)
        server_ms.append(payload["timings_ms"]["infer"])
        print("warm %2d: roundtrip=%.0fms infer=%.0fms"
              % (index, elapsed, payload["timings_ms"]["infer"]))

    print("\nn=%d  p50=%.0fms  p95=%.0fms (roundtrip)  infer p50=%.0fms"
          % (len(wall_ms), statistics.median(wall_ms),
             float(np.percentile(wall_ms, 95)), statistics.median(server_ms)))

    # 并发：两路同时打，应恰有一路 503 busy（单飞设计，不排队）
    release = threading.Event()
    results = {}

    def worker(tag, wait):
        if wait:
            release.wait(timeout=30)
        results[tag] = post_segment(base_url, image_b64)

    first = threading.Thread(target=worker, args=("first", False))
    second = threading.Thread(target=worker, args=("second", True))
    first.start()
    time.sleep(0.8)  # 让第一路进入推理（持锁）
    second.start()
    release.set()
    first.join(timeout=300)
    second.join(timeout=300)
    statuses = {tag: results[tag][0] for tag in results}
    print("concurrency: statuses=%s %s"
          % (statuses, "(one busy as designed)"
             if sorted(statuses.values()) == [200, 503] else "(UNEXPECTED)"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
