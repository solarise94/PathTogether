# -*- coding: utf-8 -*-
"""ai_budget 数据层原子原语测试（docs §4.2/§5.3/§9.4）。

仅 RUN_PG_TESTS=1 时真跑（conftest 已起 pgserver、设 DATABASE_URL +
STORAGE_BACKEND=postgres，并每用例 TRUNCATE 0006 新表 + RESTART IDENTITY，
period id 每用例从 1 起）。缺 pgserver/psycopg 时整模块 skip。

覆盖：
  - 周期默认值、幂等取行（值以行为准）、conn 版 get_or_create；
  - reserve 基本流 + request_id 幂等重放不重复扣；
  - 并发 N 线程 reserve 同一 user（限额 10）→ 成功数恰为 10；
  - demo 每日子额度（缺省 50，滚动 24h）与平台总量 30 的组合超限场景；
  - own 凭据不扣平台总量但落可观测用量；
  - consume / release 的状态机与幂等、consumed 拒绝释放；
  - reclaim_expired 只按时间回收并回退 usage；
  - update_period_limits 只改行不清用量、调低后新请求立即被拒；
  - reset_period 关旧开新、旧周期行与 usage 保留、新周期用量归零；
  - usage_report 形状（总量 / 构成 / 每 user 明细）。
"""
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest

# 缺基建依赖时整模块 skip（不 fail）
pytest.importorskip("pgserver")
pytest.importorskip("psycopg")

import budget_store  # noqa: E402
import pg_store  # noqa: E402
from pg_compat import BACKEND  # noqa: E402

pytestmark = pytest.mark.skipif(
    BACKEND != "postgres",
    reason="ai_budget 原语需 PG（RUN_PG_TESTS=1）",
)


def _req():
    return "req_" + uuid.uuid4().hex


@pytest.fixture
def pg_conn(pg_uri):
    """直连 pg（dict_row），核对保留的旧周期行/用量。"""
    import psycopg
    c = psycopg.connect(pg_uri)
    c.row_factory = psycopg.rows.dict_row
    yield c
    c.close()


# --------------------------------------------------------------------------- #
# 周期
# --------------------------------------------------------------------------- #
def test_period_defaults_and_row_is_authoritative():
    p1 = budget_store.get_current_period()
    assert p1["platform_turn_limit"] == 30
    assert p1["demo_turn_limit"] == 50  # 0014 起每日（滚动 24h）口径
    assert p1["user_turn_limit"] == 3  # P0-B §3.7：单 user 初始 3
    assert p1["owner_reserved_turn_limit"] == 10  # owner 保留池
    assert p1["user_pool_turn_limit"] == 15       # user 共享池
    assert p1["platform_task_max_steps"] == 20
    assert p1["own_task_max_steps_limit"] == 500
    assert p1["demo_task_max_steps"] == 20
    assert p1["demo_enabled"] is False
    assert p1["demo_per_browser_limit"] == 1
    assert p1["demo_max_concurrency"] == 2
    assert p1["closed_at"] is None
    assert p1["started_at"] is not None
    # 值以行为准：再取不新建
    p2 = budget_store.get_current_period()
    assert p2["id"] == p1["id"]


def test_get_or_create_current_period_with_conn(pg_conn):
    """任务签名形态：调用方事务内传 conn 使用。"""
    with pg_store.transaction(pg_conn) as c:
        p = budget_store.get_or_create_current_period(c, created_by="usr_owner")
    assert p["created_by"] == "usr_owner"
    assert p["id"] == budget_store.get_current_period()["id"]


# --------------------------------------------------------------------------- #
# reserve / 幂等重放
# --------------------------------------------------------------------------- #
def test_reserve_creates_reservation_and_usage():
    rid = _req()
    period = budget_store.get_current_period()
    r = budget_store.reserve_turn(rid, "user", "usr_a", "platform")
    assert r["state"] == "reserved"
    assert r["request_id"] == rid
    assert r["period_id"] == period["id"]
    assert r["subject_type"] == "user" and r["subject_id"] == "usr_a"
    assert r["credential_source"] == "platform"
    assert r["reservation_expires_at"] > r["reserved_at"]
    assert budget_store.get_reservation(rid)["state"] == "reserved"
    report = budget_store.usage_report()
    assert report["platform"]["reserved"] == 1
    assert report["per_user"][0]["subject_id"] == "usr_a"


def test_reserve_replay_same_request_id_no_double_charge():
    rid = _req()
    r1 = budget_store.reserve_turn(rid, "user", "usr_a", "platform")
    r2 = budget_store.reserve_turn(rid, "user", "usr_a", "platform")
    assert r2["request_id"] == rid
    assert r2["state"] == r1["state"] == "reserved"
    assert r1["attempt"] == 1
    assert r2["attempt"] == 1
    assert (r1.get("rollback_epoch") or 0) == 0
    assert r2["rollback_epoch"] == 1
    assert r2.get("replayed") is True
    assert budget_store.usage_report()["platform"]["total"] == 1
    # 原执行仍可用 attempt=1 consume，重放不得换代
    out = budget_store.consume(rid, "hp_sess", expected_attempt=1)
    assert out["state"] == "consumed"


def test_reserved_replay_invalidates_original_rollback_cas():
    """A reserve → B replay → A release 不得把仍在执行的预占退款。"""
    rid = _req()
    original = budget_store.reserve_turn(rid, "user", "usr_replay", "platform")
    replay = budget_store.reserve_turn(rid, "user", "usr_replay", "platform")
    assert original.get("replayed") is not True
    assert replay.get("replayed") is True
    assert original["attempt"] == replay["attempt"] == 1
    assert (original.get("rollback_epoch") or 0) == 0
    assert replay["rollback_epoch"] == 1
    with pytest.raises(budget_store.ReservationAttemptConflict):
        budget_store.release(
            rid, expected_attempt=original["attempt"],
            expected_rollback_epoch=original.get("rollback_epoch") or 0)
    got = budget_store.get_reservation(rid)
    assert got["state"] == "reserved"
    out = budget_store.consume(rid, "hp_sess", expected_attempt=replay["attempt"])
    assert out["state"] == "consumed"


def test_reconcile_release_without_epoch_still_works_after_replay():
    """确认 missing 的对账不传 rollback_epoch，仍可释放已重放过的 reserved。"""
    rid = _req()
    budget_store.reserve_turn(rid, "user", "usr_recon", "platform")
    budget_store.reserve_turn(rid, "user", "usr_recon", "platform")
    out = budget_store.release(rid, expected_attempt=1)
    assert out["state"] == "released"


def test_stale_attempt_release_does_not_refund_newer_try():
    """确认 missing/abandoned 并退款后重新预占才换代；旧 attempt 不得退新尝试。"""
    rid = _req()
    first = budget_store.reserve_turn(rid, "user", "usr_cas", "platform")
    assert first["attempt"] == 1
    budget_store.release(rid, expected_attempt=1)
    second = budget_store.reserve_turn(rid, "user", "usr_cas", "platform")
    assert second["attempt"] == 2
    assert not second.get("replayed")
    with pytest.raises(budget_store.ReservationAttemptConflict):
        budget_store.release(rid, expected_attempt=1)
    got = budget_store.get_reservation(rid)
    assert got["state"] == "reserved"
    assert got["attempt"] == 2
    out = budget_store.consume(rid, "hp_sess", expected_attempt=2)
    assert out["state"] == "consumed"
    # 已 consumed：幂等返回，不再因旧 attempt 改状态
    again = budget_store.consume(rid, "hp_sess", expected_attempt=1)
    assert again["state"] == "consumed"


def test_concurrent_reserve_same_request_id_is_idempotent():
    """同 request_id 并发预占必须全部成功且只扣 1 次，不能撞主键变 500。"""
    rid = _req()
    n = 8
    barrier = threading.Barrier(n)

    def worker(_i):
        barrier.wait()
        return budget_store.reserve_turn(rid, "user", "usr_idem", "platform")

    with ThreadPoolExecutor(max_workers=n) as pool:
        rows = list(pool.map(worker, range(n)))
    assert all(r["request_id"] == rid and r["state"] == "reserved" for r in rows)
    assert budget_store.usage_report()["platform"]["total"] == 1


def test_reserve_validates_subject_and_source():
    with pytest.raises(ValueError):
        budget_store.reserve_turn(_req(), "guest", "g1", "platform")
    with pytest.raises(ValueError):
        budget_store.reserve_turn(_req(), "user", "usr_a", "leaked")
    with pytest.raises(ValueError):
        budget_store.reserve_turn("", "user", "usr_a", "platform")


def test_reserve_rejects_cross_subject_request_id_reuse():
    """reserved/consumed 也必须校验主体：禁止匿名换 capability 复用同一 request_id。"""
    rid = _req()
    first = budget_store.reserve_turn(rid, "demo", "dmo_cap_a", "platform")
    assert first["state"] == "reserved"
    with pytest.raises(budget_store.RequestIdSubjectConflict) as ei:
        budget_store.reserve_turn(rid, "demo", "dmo_cap_b", "platform")
    assert ei.value.code == "request_id_subject_conflict"
    # 原预占未被改写
    got = budget_store.get_reservation(rid)
    assert got["subject_id"] == "dmo_cap_a"
    assert got["state"] == "reserved"
    budget_store.consume(rid, "sess_a")
    with pytest.raises(budget_store.RequestIdSubjectConflict):
        budget_store.reserve_turn(rid, "demo", "dmo_cap_b", "platform")
    assert budget_store.get_reservation(rid)["state"] == "consumed"


def test_demo_per_browser_limit_and_concurrency_enforced():
    budget_store.update_period_limits({
        "demo_per_browser_limit": 1, "demo_max_concurrency": 1,
        "demo_turn_limit": 5})
    budget_store.reserve_turn(_req(), "demo", "dmo_same", "platform")
    with pytest.raises(budget_store.DemoPerBrowserExhausted) as ei:
        budget_store.reserve_turn(_req(), "demo", "dmo_same", "platform")
    assert ei.value.code == "demo_run_already_used"
    with pytest.raises(budget_store.DemoConcurrencyExceeded) as ei2:
        budget_store.reserve_turn(_req(), "demo", "dmo_other", "platform")
    assert ei2.value.code == "demo_concurrency_exceeded"


# --------------------------------------------------------------------------- #
# 额度判定：user / demo / platform
# --------------------------------------------------------------------------- #
def test_user_limit_enforced_sequentially():
    budget_store.update_period_limits({"user_turn_limit": 10})  # P0-B 默认改 3，显式回 10 保持本用例语义
    for _ in range(10):
        budget_store.reserve_turn(_req(), "user", "usr_b", "platform")
    with pytest.raises(budget_store.UserBudgetExhausted) as ei:
        budget_store.reserve_turn(_req(), "user", "usr_b", "platform")
    assert ei.value.code == "user_budget_exhausted"
    assert ei.value.context["limit"] == 10
    assert ei.value.context["used"] == 10
    # 其他 user 不受影响
    assert budget_store.reserve_turn(
        _req(), "user", "usr_other", "platform")["state"] == "reserved"


def test_concurrent_reserve_same_user_exact_limit():
    """N 线程同时 reserve 同一 user（限额显式 10）→ 成功数恰为 10，无超扣。"""
    budget_store.update_period_limits({"user_turn_limit": 10})
    n = 30
    barrier = threading.Barrier(n)

    def worker(_i):
        barrier.wait()  # 尽量同时起跑，放大竞态窗口
        try:
            budget_store.reserve_turn(_req(), "user", "usr_c", "platform")
            return "ok"
        except budget_store.BudgetError as exc:
            return exc.code
        except Exception as exc:  # 意外异常单独标记，便于定位
            return "unexpected:%s" % type(exc).__name__

    with ThreadPoolExecutor(max_workers=n) as ex:
        results = list(ex.map(worker, range(n)))
    assert results.count("ok") == 10
    assert all(r in ("ok", "user_budget_exhausted") for r in results), results
    report = budget_store.usage_report()
    assert report["platform"]["total"] == 10  # 无超扣、无丢失
    assert report["per_user"][0]["subject_id"] == "usr_c"
    assert report["per_user"][0]["total"] == 10


def test_demo_sublimit_and_platform_combo():
    """demo 每日子额度（显式 5，滚动 24h）、总额 30：demo 第 6 次被拒，
    注册用户仍可用剩余总额。"""
    budget_store.update_period_limits({
        "demo_turn_limit": 5, "demo_max_concurrency": 10})
    for i in range(5):
        assert budget_store.reserve_turn(
            _req(), "demo", "dmo_%d" % i, "platform")["state"] == "reserved"
    with pytest.raises(budget_store.DemoBudgetExhausted) as ei:
        budget_store.reserve_turn(_req(), "demo", "dmo_5", "platform")
    assert ei.value.code == "demo_budget_exhausted"
    assert ei.value.context["limit"] == 5
    # demo 每日子额度耗尽不影响注册用户使用剩余平台总额度
    assert budget_store.reserve_turn(
        _req(), "user", "usr_x", "platform")["state"] == "reserved"
    report = budget_store.usage_report()
    assert report["demo"]["total"] == 5  # 窗口口径（5 次均在 24h 内）
    assert report["demo"]["limit"] == 5
    assert report["platform"]["total"] == 6
    assert report["by_subject_type"]["demo"]["total"] == 5
    assert report["by_subject_type"]["user"]["total"] == 1


def test_platform_total_exhausted():
    budget_store.update_period_limits(
        {"platform_turn_limit": 3, "user_turn_limit": 2, "demo_turn_limit": 2})
    budget_store.reserve_turn(_req(), "user", "u1", "platform")  # 1
    budget_store.reserve_turn(_req(), "user", "u2", "platform")  # 2
    budget_store.reserve_turn(_req(), "demo", "d1", "platform")  # 3
    # 第 4 次：user 子额度 / demo 子额度均未满，但平台总量已满
    with pytest.raises(budget_store.PlatformBudgetExhausted) as ei:
        budget_store.reserve_turn(_req(), "user", "u3", "platform")
    assert ei.value.code == "platform_ai_budget_exhausted"
    assert ei.value.context == {"limit": 3, "used": 3}
    with pytest.raises(budget_store.PlatformBudgetExhausted):
        budget_store.reserve_turn(_req(), "demo", "d2", "platform")


def test_own_credential_bypasses_platform_quota_but_recorded():
    budget_store.update_period_limits({"platform_turn_limit": 1})
    budget_store.reserve_turn(_req(), "user", "u1", "platform")
    with pytest.raises(budget_store.PlatformBudgetExhausted):
        budget_store.reserve_turn(_req(), "user", "u1", "platform")
    # own 凭据：平台总额已满仍可预占（不扣平台总量）
    r = budget_store.reserve_turn(_req(), "user", "u1", "own")
    assert r["state"] == "reserved" and r["credential_source"] == "own"
    report = budget_store.usage_report()
    assert report["platform"]["total"] == 1
    assert report["own"]["total"] == 1  # 可观测量仍落库
    # per_user 只统计 platform 凭据：u1 的 own 预占不计入（platform 那 1 次仍在）
    per_user = {r["subject_id"]: r for r in report["per_user"]}
    assert set(per_user) == {"u1"}
    assert per_user["u1"]["reserved"] == 1 and per_user["u1"]["accepted"] == 0


# --------------------------------------------------------------------------- #
# consume / release / reclaim
# --------------------------------------------------------------------------- #
def test_consume_moves_usage_and_is_idempotent():
    rid = _req()
    budget_store.reserve_turn(rid, "user", "usr_a", "platform")
    out = budget_store.consume(rid, "hp_sess_1")
    assert out["state"] == "consumed"
    assert out["histopilot_session_id"] == "hp_sess_1"
    # 幂等：已 consumed 直接返回
    again = budget_store.consume(rid, "hp_sess_1")
    assert again["state"] == "consumed"
    report = budget_store.usage_report()
    assert report["platform"]["accepted"] == 1
    assert report["platform"]["reserved"] == 0
    assert report["platform"]["total"] == 1  # 消费后仍计入总量
    # 未知 request_id → None
    assert budget_store.consume("req_unknown", "hp") is None


def test_release_refunds_usage_and_rejects_consumed():
    rid = _req()
    budget_store.reserve_turn(rid, "demo", "dmo_1", "platform")
    out = budget_store.release(rid)
    assert out["state"] == "released"
    assert budget_store.release(rid)["state"] == "released"  # 幂等
    assert budget_store.usage_report()["platform"]["total"] == 0  # 回退
    # released 不能消费
    with pytest.raises(ValueError):
        budget_store.consume(rid, "hp_sess")
    # consumed 拒绝释放（防误退款）
    rid2 = _req()
    budget_store.reserve_turn(rid2, "demo", "dmo_2", "platform")
    budget_store.consume(rid2, "hp_sess_2")
    with pytest.raises(ValueError):
        budget_store.release(rid2)
    assert budget_store.usage_report()["platform"]["total"] == 1


def test_reclaim_expired_only_by_time():
    rid = _req()
    budget_store.reserve_turn(rid, "demo", "dmo_1", "platform", ttl_seconds=60)
    # 未过期：不回收
    assert budget_store.reclaim_expired() == []
    # 时间前进 120 秒后回收（本函数只按时间回收，对账顺延语义在上层）
    reclaimed = budget_store.reclaim_expired(time.time() + 120)
    assert [r["request_id"] for r in reclaimed] == [rid]
    assert reclaimed[0]["state"] == "released"
    assert budget_store.usage_report()["platform"]["total"] == 0  # usage 回退
    with pytest.raises(ValueError):
        budget_store.consume(rid, "hp_sess")  # released 不可消费


# --------------------------------------------------------------------------- #
# 限制更新 / 周期重置
# --------------------------------------------------------------------------- #
def test_update_period_limits_keeps_usage():
    budget_store.reserve_turn(_req(), "user", "u1", "platform")
    p = budget_store.update_period_limits({"platform_turn_limit": 2})
    assert p["platform_turn_limit"] == 2
    assert p["user_turn_limit"] == 3  # 未给的列不动（P0-B 默认 3）
    assert budget_store.usage_report()["platform"]["total"] == 1  # 不清用量
    # 调低到小于已用量：现有运行不取消，但新请求立即被拒（docs §4.2）
    budget_store.update_period_limits({"platform_turn_limit": 1})
    with pytest.raises(budget_store.PlatformBudgetExhausted):
        budget_store.reserve_turn(_req(), "user", "u2", "platform")


def test_update_period_limits_validates_fields():
    with pytest.raises(ValueError):
        budget_store.update_period_limits({"no_such_limit": 1})
    with pytest.raises(ValueError):
        budget_store.update_period_limits({"platform_turn_limit": -1})
    with pytest.raises(ValueError):
        budget_store.update_period_limits({})


def test_reset_period_closes_old_keeps_history_zeroes_usage(pg_conn):
    p1 = budget_store.get_current_period()
    budget_store.reserve_turn(_req(), "user", "u1", "platform")
    budget_store.reserve_turn(_req(), "demo", "d1", "platform")
    p2 = budget_store.reset_period(
        {"platform_turn_limit": 50}, created_by="usr_owner")
    assert p2["id"] > p1["id"]
    assert p2["platform_turn_limit"] == 50
    assert p2["user_turn_limit"] == 3  # 未给的沿用旧周期值（P0-B 默认 3）
    assert p2["closed_at"] is None
    # 新周期用量归零、报表切到新周期
    report = budget_store.usage_report()
    assert report["period"]["id"] == p2["id"]
    assert report["platform"]["total"] == 0
    assert report["platform"]["limit"] == 50
    # 旧周期行与 usage 保留（用于排查，不物理删除）
    with pg_conn.cursor() as cur:
        cur.execute(
            "SELECT extract(epoch from closed_at)::float8 AS closed_at "
            "FROM ai_budget_periods WHERE id=%s", (p1["id"],))
        assert cur.fetchone()["closed_at"] is not None
        cur.execute(
            "SELECT COALESCE(SUM(accepted_turns + reserved_turns),0)::int "
            "AS used FROM ai_budget_usage WHERE period_id=%s", (p1["id"],))
        assert cur.fetchone()["used"] == 2
    # 新周期内额度恢复可用
    assert budget_store.reserve_turn(
        _req(), "user", "u1", "platform")["state"] == "reserved"


# --------------------------------------------------------------------------- #
# usage_report 形状
# --------------------------------------------------------------------------- #
def test_usage_report_shape():
    r1 = budget_store.reserve_turn(_req(), "user", "usr_a", "platform")
    budget_store.reserve_turn(_req(), "demo", "dmo_1", "platform")
    budget_store.reserve_turn(_req(), "user", "usr_b", "own")
    budget_store.consume(r1["request_id"], "hp_sess_1")
    report = budget_store.usage_report()
    assert report["platform"]["accepted"] == 1
    assert report["platform"]["reserved"] == 1
    assert report["platform"]["total"] == 2
    assert report["platform"]["limit"] == 30
    assert report["demo"]["total"] == 1
    assert report["demo"]["limit"] == 50  # 0014 起每日（滚动 24h）口径
    assert report["by_subject_type"]["user"]["total"] == 1
    assert report["by_subject_type"]["demo"]["total"] == 1
    assert "owner" not in report["by_subject_type"]  # 无 owner 用量不出场
    per_user = {r["subject_id"]: r for r in report["per_user"]}
    assert per_user["usr_a"]["accepted"] == 1
    assert per_user["usr_a"]["reserved"] == 0
    assert per_user["usr_a"]["limit"] == 3  # P0-B 单 user 初始 3
    assert "usr_b" not in per_user  # own 凭据不计入 per_user
    assert report["own"]["total"] == 1
    # P0-B 池区段（docs §3.7）
    assert report["user_pool"]["total"] == 1
    assert report["user_pool"]["limit"] == 15
    assert report["owner"]["reserved_limit"] == 10
