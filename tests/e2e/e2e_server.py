# -*- coding: utf-8 -*-
"""管理工作台 Playwright E2E 的被测应用进程（一次性修复包 F，§10.1）。

由 playwright.config.ts 的 webServer 拉起；职责：
  - 临时数据目录 + 内嵌 PostgreSQL（pgserver，同 conftest 的 RUN_PG_TESTS 路径）；
  - 仓库内 admin bundle + 仓库 source-policy pin（启动引导自动建 installation 行）；
  - 一次性 owner（BOOTSTRAP_OWNER_*）与普通用户——凭据只在进程内存与
    E2E_CREDS_FILE 指定文件中，绝不写 stdout/日志/artifact；
  - 本地 HTTP origin 起 Flask（「公网 HTTPS、内部 HTTP」的 scheme 分离由
    Python CSP 回归测试覆盖；部署后的公网冒烟负责真实 TLS 链）。

运行：python3 tests/e2e/e2e_server.py --port 8907
"""
import argparse
import atexit
import json
import os
import secrets
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "tests"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import pg_reap  # noqa: E402  (path 就绪后再 import，不依赖脚本目录恰为 sys.path[0])


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8907)
    args = parser.parse_args()

    tmp = tempfile.mkdtemp(prefix="pt-e2e-app-")
    os.environ["SHARE_DATA_DIR"] = os.path.join(tmp, "share-data")
    os.environ["UPLOAD_DIR"] = os.path.join(tmp, "uploads")
    os.makedirs(os.environ["SHARE_DATA_DIR"], exist_ok=True)
    os.makedirs(os.environ["UPLOAD_DIR"], exist_ok=True)
    os.environ.setdefault("AI_SIDECAR_URL", "http://127.0.0.1:8055")

    # 内嵌 PostgreSQL（隔离实例，不碰任何本机库）
    import pgserver
    import psycopg
    import pg_store
    pgdata = os.path.join(tmp, "pgdata")
    srv = pgserver.get_server(pgdata)
    os.environ["DATABASE_URL"] = srv.get_uri()
    os.environ["STORAGE_BACKEND"] = "postgres"

    # postmaster 是守护进程（独立进程组），webServer 的 SIGKILL 打不到它；
    # 写 marker 给父进程（playwright.config.ts 的 exit 兜底）收割用，不含凭据。
    marker = pg_reap.marker_path_for(args.port)
    pg_reap.write_marker(marker, pgdata=pgdata, tmp=tmp)
    cleanup = pg_reap.make_cleanup(pgdata, tmp, marker, lambda: srv)
    atexit.register(cleanup)
    # SIGTERM -> 正常退出路径，保证 finally/atexit 里的清理执行
    pg_reap.install_signal_handlers()

    conn = psycopg.connect(os.environ["DATABASE_URL"])
    try:
        pg_store.ensure_schema(conn)
    finally:
        conn.close()

    # 一次性凭据：只进进程内存 + 指定文件（默认系统临时目录内、带端口名）
    owner_pw = secrets.token_urlsafe(24)
    user_pw = secrets.token_urlsafe(24)
    pw_file = Path(tmp) / "bootstrap-owner-pw"
    pw_file.write_text(owner_pw, encoding="utf-8")
    pw_file.chmod(0o600)
    os.environ["BOOTSTRAP_OWNER_LOGIN_ID"] = "e2e-owner@pt.test"
    os.environ["BOOTSTRAP_OWNER_PASSWORD_FILE"] = str(pw_file)
    os.environ["REQUIRE_ADMIN_AUTH"] = "1"
    # 生产判定（TESTING/debug 均关）下 CSP 必须有规范公网 origin；本地 E2E
    # 用 http origin（本地 HTTP 合法，公网强制 https 由 Python CSP 测试覆盖）
    os.environ["PUBLIC_BASE_URL"] = "http://127.0.0.1:%d" % args.port

    # openslide stub + 数据目录幂等初始化（与 pytest 会话同一套引导）
    import _bootstrap  # noqa: F401
    import app as app_mod  # 启动期自动：owner 首建 + admin 插件 installation 引导

    # Batch B review 修复后的形态：注册用户授权面由 user_spend_target 决定。
    # 10d/10e/10f（建号/抽屉 CAS/邀请模板）验收的是 cutover 后的总额度形态，
    # 故种子：target CAS 到 total_allowance + 全局默认总额度 50 CNY
    # （version=1 首写），预建普通用户经建号组合原语自动获得默认额度行。
    import settings_store
    import spend_store
    settings_store.compare_and_set_setting(
        settings_store.USER_SPEND_TARGET_KEY, "window", "total_allowance",
        updated_by="e2e-seed")
    spend_store.set_total_default(50_000_000_000, 1, updated_by="e2e-seed")

    import user_store_pg
    user_store_pg.create_user_with_total_allowance(
        "e2e-user@pt.test", user_pw, display_name="E2E 普通用户")

    creds_path = os.environ.get("E2E_CREDS_FILE") or os.path.join(
        tempfile.gettempdir(), "pt-e2e-creds-%d.json" % args.port)
    Path(creds_path).write_text(json.dumps({
        "baseUrl": "http://127.0.0.1:%d" % args.port,
        "ownerLogin": "e2e-owner@pt.test",
        "ownerPassword": owner_pw,
        "userLogin": "e2e-user@pt.test",
        "userPassword": user_pw,
    }), encoding="utf-8")

    try:
        app_mod.app.run(host="127.0.0.1", port=args.port, threaded=True)
    finally:
        cleanup()
        atexit.unregister(cleanup)


if __name__ == "__main__":
    main()
