#!/usr/bin/env python3
"""Phase 0 收尾 §9.5-1:真实页面 CSP 正向探针(不绕过 CSP)。

与 probe_origin_put.py 的区别:浏览器 context **不设 bypassCSP**,页面 CSP
原样生效——只有目标 origin 的响应 CSP 确实放行了 COS endpoint,浏览器
PUT 才可能成功。用于:
  - 本地机制验证(demo PT + CSP_EXTRA_CONNECT_SRC);
  - 生产三 origin 门禁验收(部署放行后逐一复测)。
全部 multipart 仅 1 MiB 探针块,结束即 Abort,不落对象。
"""
import json
import subprocess
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
import poc_config
from presign_parts import initiate_multipart, abort_multipart, build_part_url


def main():
    origins = sys.argv[1:] or ["http://127.0.0.1:8765"]
    cfg = poc_config.load_config(require=True)
    rc = 0
    for origin in origins:
        key = poc_config.random_key(cfg.prefix, label='csp-probe')
        upload_id = initiate_multipart(cfg, key)
        try:
            url, _ = build_part_url(cfg, key, upload_id, 1, 1_000_000,
                                    True, 300)
            payload = json.dumps({'origin': origin, 'url': url,
                                  'size': 1_000_000})
            node = Path(__file__).with_suffix('.js')
            run = subprocess.run(['node', str(node)], input=payload, text=True,
                                 capture_output=True, timeout=60)
            print(run.stdout.strip()[:400] if run.stdout
                  else origin + ' browser_failed ' + run.stderr.strip()[:120],
                  flush=True)
            if run.returncode:
                rc = 1
        finally:
            try:
                print(origin, 'abort', abort_multipart(cfg, key, upload_id),
                      flush=True)
            except Exception as e:
                print(origin, 'ABORT_FAILED', type(e).__name__, flush=True)
                rc = 1
    return rc


if __name__ == '__main__':
    raise SystemExit(main())
