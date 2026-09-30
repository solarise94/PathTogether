# -*- coding: utf-8 -*-
"""C4 工具页上传 Playwright 驱动的被测应用：真实 Flask app。

在 C3 server.py（tests/browser/slide_tools_c3/server.py）基础上增加：

- 一次性 owner/user 凭据（仿 tests/e2e/e2e_server.py）：上传能力端点与
  ingestion 需要**登录**会话；凭据只经 --creds 指定的 JSON 文件传递；
- 假 COS 配置（COS_BUCKET/COS_REGION/SECRET_* 全为本地假值，绝不触网：
  ingestion 分块 PUT 由 Playwright page.route 拦截，服务端 presign 只用
  本地签名算法），COS_UPLOAD_CAPABILITY=on + 池容量 > 产品上限（产品上限
  在 import 后压到 900,000,000，使 _cos_upload_capability_payload 可用）；
- 真实响应头由此进程决定：工具页 CSP 的 connect-src 会包含假 COS origin
  （https://<bucket>.cos.<region>.myqcloud.com），页面分块 PUT 必须落在
  该 origin 内（page.route 晚于 CSP 检查，CSP 写错即测试失败）。

运行：python3 tests/browser/slide_tools_c4/server.py --port 8953 --creds <json>

R1 可选项（C4 套件不用）：
- 第二个普通用户 c4-user2（跨账号授权用例：真实 _ingestion_fetch 403）；
- --fake-cos-worker：进程内线程把真实 ingestion 从 preparing 推进到
  uploading（cos_ingest_worker.process_preparing + 本地假 COS 的 Initiate，
  仅此一步——分块 PUT 仍由 page.route 拦截；completing 之后不推进）；
- --seed-ready-slide：为 c4-user 分配一个真实 ready 切片（合成 TIFF 写入
  id_bundle 存储路径 + mark_ready），ID 写入 creds（readySlideId），供真实
  项目关联端点测试。
"""
import argparse
import atexit
import json
import os
import secrets
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "tests"))
sys.path.insert(0, str(REPO_ROOT / "tests" / "e2e"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import pg_reap  # noqa: E402


def _seed_ready_slide(owner_user_id):
    """真实 ready 切片：allocate → 合成 TIFF 落到 id_bundle 路径 → mark_ready。"""
    import numpy as np
    import tifffile
    import slide_storage
    import slide_store
    desc = slide_store.allocate_slide(owner_user_id, "r1-assoc-real.tif", "tif")
    path = str(slide_storage.resolve_descriptor_path(desc))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tifffile.imwrite(path, np.full((256, 256, 3), 200, dtype=np.uint8),
                     tile=(256, 256), photometric="rgb")
    assert slide_store.mark_ready(desc.slide_id,
                                  accounted_bytes=os.path.getsize(path))
    return desc.slide_id


class _FakeCosInitiate:
    """只实现 Initiate（preparing → uploading 所需的唯一远端调用）。"""

    def __init__(self):
        self._n = 0

    def initiate_multipart(self, key):
        self._n += 1
        return "fake-up-%d" % self._n


def _start_fake_cos_preparing_worker():
    import threading
    import time
    import cos_ingest_worker

    fake = _FakeCosInitiate()

    def loop():
        while True:
            try:
                while cos_ingest_worker.process_preparing(cos=fake):
                    pass
            except Exception as exc:  # noqa: BLE001 — 测试服务：记录后继续
                print("fake-cos-worker error: %r" % (exc,), file=sys.stderr)
            time.sleep(0.2)

    threading.Thread(target=loop, name="fake-cos-preparing", daemon=True).start()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8953)
    parser.add_argument("--creds", default="")
    parser.add_argument("--fake-cos-worker", action="store_true")
    parser.add_argument("--seed-ready-slide", action="store_true")
    args = parser.parse_args()

    tmp = tempfile.mkdtemp(prefix="pt-c4-tools-")
    os.environ["SHARE_DATA_DIR"] = os.path.join(tmp, "share-data")
    os.environ["UPLOAD_DIR"] = os.path.join(tmp, "uploads")
    os.makedirs(os.environ["SHARE_DATA_DIR"], exist_ok=True)
    os.makedirs(os.environ["UPLOAD_DIR"], exist_ok=True)
    os.environ.setdefault("UPLOAD_RESERVED_FREE_BYTES", str(16 * 1024 * 1024))
    os.environ.setdefault("AI_SIDECAR_URL", "http://127.0.0.1:8055")
    os.environ["PUBLIC_BASE_URL"] = "http://127.0.0.1:%d" % args.port

    # 假 COS（本地值；分块 PUT 由 page.route 拦截，绝不解析真实 DNS）
    os.environ["COS_BUCKET"] = "c4fake-1250000000"
    os.environ["COS_REGION"] = "ap-fake"
    os.environ["COS_SECRET_ID"] = "AKIDc4fake"
    os.environ["COS_SECRET_KEY"] = "c" * 20
    os.environ["COS_UPLOAD_CAPABILITY"] = "on"
    os.environ["COS_POOL_CAPACITY_BYTES"] = "10000000000"
    os.environ["COS_POOL_SAFETY_BYTES"] = "100000000"

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

    # 一次性凭据（进程内存 + creds 文件；不进日志）
    owner_pw = secrets.token_urlsafe(24)
    user_pw = secrets.token_urlsafe(24)
    pw_file = Path(tmp) / "bootstrap-owner-pw"
    pw_file.write_text(owner_pw, encoding="utf-8")
    pw_file.chmod(0o600)
    os.environ["BOOTSTRAP_OWNER_LOGIN_ID"] = "c4-owner@pt.test"
    os.environ["BOOTSTRAP_OWNER_PASSWORD_FILE"] = str(pw_file)

    import _bootstrap  # noqa: F401  # openslide stub + 数据目录（幂等）
    import app as app_mod  # 启动期自动：owner 首建 + admin 插件 installation 引导

    # 认证开启（生产形态）；/tools/slides 仍由 _require_auth 白名单放行
    app_mod.AUTH_ENABLED = True
    # 产品上限压到池结构性准入（9.9e9）之下，capability 才会 available=true
    app_mod.UPLOAD_PRODUCT_MAX_BYTES = 900_000_000

    import cos_pool_store
    cos_pool_store.ensure_pool_state()

    import user_store_pg
    user_store_pg.create_user_with_total_allowance(
        "c4-user@pt.test", user_pw, display_name="C4 普通用户")
    user2_pw = secrets.token_urlsafe(24)
    user_store_pg.create_user_with_total_allowance(
        "c4-user2@pt.test", user2_pw, display_name="C4 普通用户二")

    ready_slide_id = ""
    if args.seed_ready_slide:
        ready_slide_id = _seed_ready_slide(
            user_store_pg.get_user_by_login_id("c4-user@pt.test")["user_id"])
    if args.fake_cos_worker:
        _start_fake_cos_preparing_worker()

    creds_path = args.creds or os.path.join(
        tempfile.gettempdir(), "pt-c4-creds-%d.json" % args.port)
    Path(creds_path).write_text(json.dumps({
        "baseUrl": "http://127.0.0.1:%d" % args.port,
        "ownerLogin": "c4-owner@pt.test",
        "ownerPassword": owner_pw,
        "userLogin": "c4-user@pt.test",
        "userPassword": user_pw,
        "user2Login": "c4-user2@pt.test",
        "user2Password": user2_pw,
        "readySlideId": ready_slide_id,
        "cosOrigin": "https://c4fake-1250000000.cos.ap-fake.myqcloud.com",
    }), encoding="utf-8")

    app_mod.app.run(host="127.0.0.1", port=args.port, threaded=True)


if __name__ == "__main__":
    main()
