# -*- coding: utf-8 -*-
"""PG 双跑测试基建（Stage 3b-2）+ 测试自举入口（test-review P3-16）。

**无条件部分（json 模式也执行）**：顶部 ``import _bootstrap``——pytest 先加载
conftest 再加载任何测试模块，保证 session 级 ``SHARE_DATA_DIR`` / ``UPLOAD_DIR``
env 与 openslide stub **早于第一个 ``import app``** 生效。此前约 38 个测试模块
在各自 import 期抢写这些 env，只有第一个模块生效、其余靠 per-test ``_isolate``
补偿；现在目录选择收敛到 ``tests/_bootstrap.py`` 唯一一份（脚本直跑路径同样
import 它）。per-test 隔离见 ``_pt_helpers.isolate_app``。

仅当环境变量 ``RUN_PG_TESTS=1`` 时启用 PG 部分：
  - 在 conftest **import 期**（pytest 先加载 conftest 再加载各测试模块，故早于任何
    ``import share_store`` / ``import app``）起一个 pgserver session 实例，并设
    ``DATABASE_URL`` + ``STORAGE_BACKEND=postgres``——保证 app.py / share_store /
    user_store 在 import 期即选中 PostgreSQL 后端；
  - autouse fixture 在每用例前 ``TRUNCATE ... RESTART IDENTITY CASCADE`` 全部业务
    表，保证用例隔离；
  - ``pg_uri`` fixture 暴露连接串（测试自建连接用）。

json 默认路径零影响：``RUN_PG_TESTS`` 未设时 PG 部分什么都不做（不起 PG、不设
env、不注册 truncate fixture）。
"""
import _bootstrap  # noqa: F401  # 须最先：session 目录 + openslide stub

import os

_RUN_PG = (os.environ.get("RUN_PG_TESTS") or "").strip() in ("1", "true", "True")

if _RUN_PG:
    import psycopg
    import pytest

    import pg_store

    # ----------------------------------------------------------------------- #
    # import 期：起 pgserver + 设 env + 应用 schema（先于任何测试模块 import app）
    # ----------------------------------------------------------------------- #
    _pgserver = pytest.importorskip("pgserver")
    _PG_DATA_DIR = None

    def _start_pg_server():
        import tempfile
        global _PG_DATA_DIR
        _PG_DATA_DIR = tempfile.mkdtemp(prefix="svs-pg-conftest-")
        _srv = _pgserver.get_server(_PG_DATA_DIR)
        _uri = _srv.get_uri()
        os.environ["DATABASE_URL"] = _uri
        os.environ["STORAGE_BACKEND"] = "postgres"
        _conn = psycopg.connect(_uri)
        try:
            pg_store.ensure_schema(_conn)
        finally:
            _conn.close()
        return _srv

    _SERVER = _start_pg_server()

    def _session_cleanup():
        try:
            _SERVER.cleanup()
        except Exception:
            pass

    import atexit

    atexit.register(_session_cleanup)

    _BUSINESS_TABLES = (
        "rois", "change_log", "grants", "shares",
        "project_slides", "projects", "slide_assets", "slides", "users",
        "audit_events", "plugin_installations", "run_grants",
        # comments 0003 起入库，但此前未进 truncate 清单——跨用例残留会让
        # list_changes（含评论）读到上次用例的数据（Stage 4-1a 测试暴露）
        "comments",
        # 0006 起：Demo / AI 预算 / 登录锁定数据层（RESTART IDENTITY CASCADE
        # 会重置 ai_budget_periods 的 serial，保证 period id 从 1 起可预测）
        "platform_settings", "auth_rate_limits",
        "ai_budget_usage", "ai_budget_reservations", "ai_budget_periods",
        "demo_sessions", "demo_catalog",
        # 0012 起：邀请注册（registration_invites；users 的 ai_access 列随
        # users 清空重置）
        "registration_invites",
        # 0017 起：Upload V2 分片任务（无 users 外键——owner_user_id 允许空，
        # 不随 users CASCADE 清空，必须显式列出，否则跨用例残留串数据）
        "upload_tasks",
        # 0018 起：金额计费（admin-billing §6）。billing_price_books 的迁移
        # 种子会随 TRUNCATE 清掉——需要种子的用例用 tests/_billing_helpers
        # .seed_price_books() 幂等重放 migrations/0018_billing.sql（迁移文件
        # 是种子唯一权威来源）。
        "billing_ledger_entries", "ai_usage_events", "billing_accounts",
        "billing_rates", "billing_price_books", "provider_balance_snapshots",
        # 0020 起：billing holds（admin-billing §12.3，PR7 影子预授权）。
        # account_id 引用 billing_accounts，CASCADE 已覆盖，显式列出防残留
        "billing_holds",
        # 0019 起：来源归因（admin-billing §11）。user_acquisition 引用
        # users/registration_invites/acquisition_visits，acquisition_visits 引用
        # acquisition_campaigns——显式列出保证跨用例无残留
        "user_acquisition", "acquisition_visits", "acquisition_campaigns",
    )

    @pytest.fixture(scope="session")
    def pg_uri():
        """session 级 PG 连接串（供测试直接建连接）。"""
        return _SERVER.get_uri()

    @pytest.fixture(autouse=True)
    def _truncate_pg_before_each():
        """每用例前清空业务表（RESTART IDENTITY CASCADE），保证隔离。"""
        conn = psycopg.connect(_SERVER.get_uri(), autocommit=True)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "TRUNCATE %s RESTART IDENTITY CASCADE"
                    % ", ".join(_BUSINESS_TABLES)
                )
        finally:
            conn.close()
        yield

    # 供测试用：当前是否为 PG 后端（json-only 测试据此 skipif）
    BACKEND = "postgres"
else:
    BACKEND = "json"
