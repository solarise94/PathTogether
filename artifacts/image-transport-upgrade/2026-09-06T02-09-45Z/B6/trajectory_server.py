# -*- coding: utf-8 -*-
"""浏览器轨迹基准的被测服务器（可复现；不进产品代码）。

嵌入 PG（与 e2e_server 同 pgserver 路径）+ AUTH 关闭（空用户库 → 单租户
免认证，owner 全量可见 UPLOAD_DIR 文件）。用法：
  python trajectory_server.py --port 8931 --repo /path/to/checkout \
      --upload-dir /path/to/uploads --pgdata /tmp/pg-trajectory
在 checkout 内运行（sys.path 指向该 checkout 的 app.py）。
"""
import argparse
import os
import sys
from pathlib import Path


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8931)
    ap.add_argument("--repo", required=True)
    ap.add_argument("--upload-dir", required=True)
    ap.add_argument("--pgdata", required=True)
    args = ap.parse_args()

    repo = Path(args.repo).resolve()
    sys.path.insert(0, str(repo))
    sys.path.insert(0, str(repo / "tests"))

    os.environ["UPLOAD_DIR"] = str(Path(args.upload_dir).resolve())
    os.environ["SHARE_DATA_DIR"] = str(Path(args.upload_dir).parent / "share-data")
    Path(os.environ["SHARE_DATA_DIR"]).mkdir(parents=True, exist_ok=True)
    os.environ["STORAGE_BACKEND"] = "postgres"

    import pgserver
    srv = pgserver.get_server(args.pgdata)
    os.environ["DATABASE_URL"] = srv.get_uri()

    import app as app_mod  # noqa: E402  （env 就绪后再 import）
    assert app_mod.AUTH_ENABLED is False, "用户库必须为空（免认证单租户）"
    app_mod.app.run(host="127.0.0.1", port=args.port, threaded=True)


if __name__ == "__main__":
    main()
