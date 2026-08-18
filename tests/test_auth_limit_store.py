# -*- coding: utf-8 -*-
"""auth_rate_limits 跨 worker 登录锁定原语测试（docs §6.3 末段/§9.5）。

仅 RUN_PG_TESTS=1 时真跑（conftest 起真实 PG + 每用例 TRUNCATE）。

覆盖：
  - 单账号多 IP 撞库：IP 桶每条 fresh，账号桶累计到阈值（10）被锁——
    复合键防不住的形态；
  - 单 IP 多账号撞库：IP 桶（5）先达阈值被锁；
  - check_auth_locked 返回权威剩余秒数；
  - 成功登录 clear 只清该账号与来源 IP 前缀，不影响其他主体；
  - 窗口过期重置计数（直接拨回 window_started_at 模拟）；
  - 自定义阈值参数。
"""
import pytest

# 缺基建依赖时整模块 skip（不 fail）
pytest.importorskip("pgserver")
pytest.importorskip("psycopg")

import auth_limit_store  # noqa: E402
from pg_compat import BACKEND  # noqa: E402

pytestmark = pytest.mark.skipif(
    BACKEND != "postgres",
    reason="登录限流原语需 PG（RUN_PG_TESTS=1）",
)


@pytest.fixture
def pg_conn(pg_uri):
    """直连 pg（dict_row），拨回窗口起点模拟窗口过期。"""
    import psycopg
    c = psycopg.connect(pg_uri)
    c.row_factory = psycopg.rows.dict_row
    yield c
    c.close()


def test_account_bucket_locks_across_many_ips():
    """单账号 × 10 个不同 IP：每条 IP 记录都 fresh，只有账号桶累计到 10。"""
    for i in range(9):
        res = auth_limit_store.record_auth_failure("acct_h", "ip_%d" % i)
        assert res["locked"] is False
        assert res["scopes"]["account"]["failed_count"] == i + 1
    res = auth_limit_store.record_auth_failure("acct_h", "ip_9")
    assert res["locked"] is True
    assert res["retry_after_seconds"] > 0
    assert res["retry_after_seconds"] <= auth_limit_store.AUTH_LOCK_SECONDS
    assert res["scopes"]["account"]["locked"] is True
    assert res["scopes"]["ip_prefix"]["locked"] is False  # IP 桶各自 fresh
    # 换一个全新 IP 查询：锁状态仍权威可见（跨 worker/跨来源一致）
    st = auth_limit_store.check_auth_locked("acct_h", "ip_brand_new")
    assert st["locked"] is True
    assert st["retry_after_seconds"] > 0
    assert st["scopes"]["account"]["locked"] is True
    assert st["scopes"]["ip_prefix"]["locked"] is False


def test_ip_bucket_locks_across_many_accounts():
    """单 IP × 5 个不同账号：IP 桶先达阈值（5）被锁，波及后续任何账号。"""
    for i in range(4):
        res = auth_limit_store.record_auth_failure("acct_%d" % i, "ip_h")
        assert res["locked"] is False
    res = auth_limit_store.record_auth_failure("acct_4", "ip_h")
    assert res["locked"] is True
    assert res["scopes"]["ip_prefix"]["failed_count"] == 5
    assert res["scopes"]["account"]["failed_count"] == 1  # 账号桶只 1 次
    # 全新账号从该 IP 登录也看到锁
    st = auth_limit_store.check_auth_locked("acct_new", "ip_h")
    assert st["locked"] is True
    assert st["scopes"]["ip_prefix"]["locked"] is True
    assert st["scopes"]["account"]["locked"] is False


def test_check_auth_locked_zero_when_clean():
    st = auth_limit_store.check_auth_locked("acct_none", "ip_none")
    assert st["locked"] is False
    assert st["retry_after_seconds"] == 0
    assert st["locked_until"] is None


def test_clear_after_success_only_these_two_records():
    for i in range(8):  # 账号 8 次（未达 10）
        auth_limit_store.record_auth_failure("acct_h", "ip_h")
    # 其他主体留一条记录
    auth_limit_store.record_auth_failure("acct_other", "ip_h2")
    assert auth_limit_store.clear_auth_failures("acct_h", "ip_h") == 2
    st = auth_limit_store.check_auth_locked("acct_h", "ip_h")
    assert st["locked"] is False and st["retry_after_seconds"] == 0
    # 其他主体不受影响：其账号计数继续累计
    res = auth_limit_store.record_auth_failure("acct_other", "ip_h2")
    assert res["scopes"]["account"]["failed_count"] == 2


def test_window_expiry_resets_count(pg_conn):
    for i in range(9):  # 账号 9 次（每次换 IP，IP 桶各自 fresh 不干扰）
        auth_limit_store.record_auth_failure("acct_h", "ip_%d" % i)
    # 只把账号桶窗口起点拨回 2 小时前（模拟窗口过期）
    with pg_conn.cursor() as cur:
        cur.execute(
            "UPDATE auth_rate_limits SET window_started_at = "
            "now() - interval '2 hours' WHERE scope='account'")
    pg_conn.commit()
    res = auth_limit_store.record_auth_failure("acct_h", "ip_new")
    assert res["locked"] is False  # 账号计数重置，差 1 次不再因第 10 次锁定
    assert res["scopes"]["account"]["failed_count"] == 1


def test_lock_persists_until_locked_until():
    for i in range(10):
        auth_limit_store.record_auth_failure("acct_h", "ip_%d" % i)
    # 锁定期间再次查询仍锁（权威 locked_until 在未来）
    for _ in range(3):
        st = auth_limit_store.check_auth_locked("acct_h", None)
        assert st["locked"] is True
    assert "ip_prefix" not in st["scopes"]  # 未提供的桶不出现


def test_custom_thresholds_params():
    res = auth_limit_store.record_auth_failure(
        "acct_h", "ip_h", account_limit=2, ip_limit=3)
    assert res["locked"] is False
    res = auth_limit_store.record_auth_failure(
        "acct_h", "ip_h", account_limit=2, ip_limit=3)
    assert res["locked"] is True  # 账号桶 2 次即达自定义阈值
    assert res["scopes"]["ip_prefix"]["failed_count"] == 2  # 未达 3
