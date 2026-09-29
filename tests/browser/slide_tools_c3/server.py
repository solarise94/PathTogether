# -*- coding: utf-8 -*-
"""C3 工具页 Playwright 驱动的被测应用：真实 Flask app（不是 C2 的静态 harness）。

沿用 tests/e2e/e2e_server.py 的模式（内嵌 PostgreSQL + 临时数据目录 +
``import app`` 后 ``app.AUTH_ENABLED = True``），但只保留工具页验收需要的部分：

- 不做 owner/user 种子与凭据文件（本页无登录面；AUTH_ENABLED=True 用于证明
  /tools/slides 无需登录可达，方式与 pytest 的 test_phase1_auth_ui 相同——
  import 后改模块属性，request 期生效）；
- 不起 share_server（工具页零 /api/ 请求，网络捕获断言会证明这一点）；
- postmaster 为守护进程：写 marker 供父进程在 SIGKILL 后收割（同 e2e）。

运行：python3 tests/browser/slide_tools_c3/server.py --port 8943
"""
import argparse
import atexit
import os
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "tests"))
sys.path.insert(0, str(REPO_ROOT / "tests" / "e2e"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import pg_reap  # noqa: E402


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8943)
    args = parser.parse_args()

    tmp = tempfile.mkdtemp(prefix="pt-c3-tools-")
    os.environ["SHARE_DATA_DIR"] = os.path.join(tmp, "share-data")
    os.environ["UPLOAD_DIR"] = os.path.join(tmp, "uploads")
    os.makedirs(os.environ["SHARE_DATA_DIR"], exist_ok=True)
    os.makedirs(os.environ["UPLOAD_DIR"], exist_ok=True)
    os.environ.setdefault("UPLOAD_RESERVED_FREE_BYTES", str(16 * 1024 * 1024))
    os.environ.setdefault("AI_SIDECAR_URL", "http://127.0.0.1:8055")
    os.environ.setdefault("PUBLIC_BASE_URL", "http://127.0.0.1:%d" % args.port)

    import pgserver
    import psycopg
    import pg_store
    pgdata = os.path.join(tmp, "pgdata")
    srv = pgserver.get_server(pgdata)
    os.environ["DATABASE_URL"] = srv.get_uri()
    os.environ["STORAGE_BACKEND"] = "postgres"

    marker = pg_reap.marker_path_for(args.port)
    pg_reap.write_marker(marker, pgdata=pgdata, tmp=tmp)
    cleanup = pg_reap.make_cleanup(pgdata, tmp, marker, lambda: srv)
    atexit.register(cleanup)
    pg_reap.install_signal_handlers()

    conn = psycopg.connect(os.environ["DATABASE_URL"])
    try:
        pg_store.ensure_schema(conn)
    finally:
        conn.close()

    import _bootstrap  # noqa: F401  # openslide stub + 数据目录（幂等）
    import app as app_mod

    # 认证开启（生产形态）；/tools/slides 由 _require_auth 白名单放行
    app_mod.AUTH_ENABLED = True

    app_mod.app.run(host="127.0.0.1", port=args.port, threaded=True)


if __name__ == "__main__":
    main()
