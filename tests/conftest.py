# -*- coding: utf-8 -*-
"""PG 测试基建（R3 Wave 3 单轨） + 测试自举入口（test-review P3-16）。

**默认（也是唯一）后端是 PostgreSQL**；json 双跑已退役。顶部
``import _bootstrap``——pytest 先加载 conftest 再加载任何测试模块，保证 session
级 ``SHARE_DATA_DIR`` / ``UPLOAD_DIR`` env 与 openslide stub **早于第一个
``import app``** 生效。此前约 38 个测试模块在各自 import 期抢写这些 env，只有
第一个模块生效、其余靠 per-test ``_isolate`` 补偿；现在目录选择收敛到
``tests/_bootstrap.py`` 唯一一份（脚本直跑路径同样 import 它）。per-test 隔离见
``_pt_helpers.isolate_app``。

无条件启用 PG：
  - 在 conftest **import 期**（pytest 先加载 conftest 再加载各测试模块，故早于任何
``import share_store`` / ``import app``）起一个 pgserver session 实例，并设
``DATABASE_URL`` + ``STORAGE_BACKEND=postgres``——保证 app.py / share_store /
user_store 在 import 期即选中 PostgreSQL 后端；
  - autouse fixture 在每用例前 ``TRUNCATE ... RESTART IDENTITY CASCADE`` 全部业务
表，保证用例隔离；
  - ``pg_uri`` fixture 暴露连接串（测试自建连接用）。

不再要求 ``RUN_PG_TESTS``：未设时仍按 postgres 起内嵌 PG。若 ``RUN_PG_TESTS`` 仍
被设置（旧的 json 双跑残留），仅作无害的忽略，不影响 PG 启动。
"""
import _bootstrap  # noqa: F401  # 须最先：session 目录 + openslide stub

import os

import psycopg
import pytest

import pg_store
import pgserver

# ----------------------------------------------------------------------- #
# import 期：起 pgserver + 设 env + 应用 schema（先于任何测试模块 import app）
# ----------------------------------------------------------------------- #
_PG_DATA_DIR = None

def _start_pg_server():
    import tempfile
    global _PG_DATA_DIR
    _PG_DATA_DIR = tempfile.mkdtemp(prefix="svs-pg-conftest-")
    _srv = pgserver.get_server(_PG_DATA_DIR)
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
    # 0027 起：金额时代 run→主体权威绑定（无 FK，显式列出防跨用例残留）
    "ai_run_bindings",
    "demo_sessions", "demo_catalog",
    # 0026 起：Demo run 流水 + IP 短窗口请求速率桶（批次 E）。demo_runs
    # 无 FK（capability 行不删除），必须显式列出防跨用例残留
    "demo_runs", "demo_ip_request_rate",
    # 0012 起：邀请注册（registration_invites；users 的 ai_access 列随
    # users 清空重置）
    "registration_invites",
    # 0017 起：Upload V2 分片任务（无 users 外键——owner_user_id 允许空，
    # 不随 users CASCADE 清空，必须显式列出，否则跨用例残留串数据）
    "upload_tasks",
    # 0018 起：金额计费（admin-billing §6）。billing_price_books 的迁移
    # 种子会随 TRUNCATE 清掉——需要种子的用例用 tests/_billing_helpers
    # .seed_price_books() 幂等重放 migrations/0018_billing.sql +
    # 0022_billing_price_unit_fix.sql（迁移文件是种子与批次 A 单位修复
    # 的唯一权威来源）。
    "billing_ledger_entries", "ai_usage_events", "billing_accounts",
    "billing_rates", "billing_price_books", "provider_balance_snapshots",
    # 0020 起：billing holds（admin-billing §12.3，PR7 影子预授权）。
    # account_id 引用 billing_accounts，CASCADE 已覆盖，显式列出防残留
    "billing_holds",
    # 0023 起：金额 policy/window（批次 B shadow 数据层）。0023 的迁移
    # 种子（三条默认策略 + spend_enforcement_mode）会随 TRUNCATE 清掉
    # ——需要种子的用例用 _billing_helpers.seed_spend_policies() 幂等
    # 重放 migrations/0023_spend_policies_windows.sql（迁移文件是种子
    # 的唯一权威来源，与 seed_price_books 同约定）
    "ai_spend_windows", "ai_spend_policies",
    # 0029 起：Batch B user 一次性总额度 + 金额拒绝事件。ai_spend_total_
    # defaults 无迁移种子（面值由 cutover 写入）；user_spend_target/
    # ai_dispatch_maintenance 两个 platform_settings 键随 platform_settings
    # 清掉——需要它们的用例用 _billing_helpers.seed_spend_settings()
    # 幂等重放 migrations/0029_user_total_allowances_and_denials.sql
    "ai_spend_total_allowances", "ai_spend_denial_events",
    "ai_spend_total_defaults",
    # 0019 起：来源归因（admin-billing §11）。user_acquisition 引用
    # users/registration_invites/acquisition_visits，acquisition_visits 引用
    # acquisition_campaigns——显式列出保证跨用例无残留
    "user_acquisition", "acquisition_visits", "acquisition_campaigns",
    # 0030 起：Batch D2 站点匿名访问事件（无用户外键、无 IP/UA/query/
    # token/资源 ID 列；worker 批量写跨用例必残留，显式列出清空）
    "site_visit_events",
)

@pytest.fixture(scope="session")
def pg_uri():
    """session 级 PG 连接串（供测试直接建连接）。"""
    return _SERVER.get_uri()

@pytest.fixture(autouse=True)
def _truncate_pg_before_each():
    """每用例前清空业务表（RESTART IDENTITY CASCADE），保证用例隔离。

    R3 Wave1-Money 单轨：TRUNCATE 后恢复一条基线种子——
    ai_spend_total_defaults 全局默认行（20 CNY = 0023 种子 user_default
    策略面值，与 0032 迁移物化同值）。单轨后 role=user 建号（user_store
    .create_user / create_user_with_total_allowance / redeem_invite）无
    显式面值时**必须**解析到默认行，缺行会 total_default_missing 拒绝建号
    ——不恢复该行会让大量与 target 无关的既有用例在建号处失败。
    需要构造「缺默认」场景的用例自行 DELETE 该行（已有先例：维护闸
    缺键用例）。幂等（ON CONFLICT DO NOTHING，不覆盖用例自定义面值）。"""
    conn = psycopg.connect(_SERVER.get_uri(), autocommit=True)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "TRUNCATE %s RESTART IDENTITY CASCADE"
                % ", ".join(_BUSINESS_TABLES)
            )
            cur.execute(
                "INSERT INTO ai_spend_total_defaults "
                "(singleton, default_limit_nano_cny, version, updated_by) "
                "VALUES ('global', %s, 1, 'conftest-baseline') "
                "ON CONFLICT (singleton) DO NOTHING",
                (20 * 10 ** 9,))
    finally:
        conn.close()
    yield

BACKEND = "postgres"
