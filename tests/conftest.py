# -*- coding: utf-8 -*-
"""PG 双跑测试基建（Stage 3b-2）。

仅当环境变量 ``RUN_PG_TESTS=1`` 时启用：
  - 在 conftest **import 期**（pytest 先加载 conftest 再加载各测试模块，故早于任何
    ``import share_store`` / ``import app``）起一个 pgserver session 实例，并设
    ``DATABASE_URL`` + ``STORAGE_BACKEND=postgres``——保证 app.py / share_store /
    user_store 在 import 期即选中 PostgreSQL 后端；
  - autouse fixture 在每用例前 ``TRUNCATE ... RESTART IDENTITY CASCADE`` 全部业务
    表，保证用例隔离；
  - ``pg_uri`` fixture 暴露连接串（测试自建连接用）。

json 默认路径零影响：``RUN_PG_TESTS`` 未设时本模块什么都不做（不起 PG、不设 env、
不注册 truncate fixture）。
"""
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
