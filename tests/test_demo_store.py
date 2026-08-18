# -*- coding: utf-8 -*-
"""demo_sessions + demo_catalog 数据层原语测试（docs §5.1-§5.3/§9.3）。

仅 RUN_PG_TESTS=1 时真跑（conftest 起真实 PG + 每用例 TRUNCATE）。

覆盖：
  - capability 生命周期：创建 / token_hash 查询 / 过期即 None / 重复冲突；
  - reserve_run CAS：并发同 capability 只有一个成功；冲突返回 None；
  - consume / release 状态机与幂等；consumed 不可 release；释放后可再预占；
  - attempt 单调递增；在途同 ID 重放不升版本；CAS 同时校验 request_id；
  - reclaim_expired_runs 惰性回收；
  - count_ip_runs：仅 reserved/consumed 计入；缺 hash 归 unknown 桶；
  - revoke_by_slide：capability 立即失效 + 未完成 run 标记终止；
  - demo_catalog 增删查排 + add 校验 slide 存在 + remove 联动撤销。
"""
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest

# 缺基建依赖时整模块 skip（不 fail）
pytest.importorskip("pgserver")
pytest.importorskip("psycopg")

import demo_store  # noqa: E402
from pg_compat import BACKEND  # noqa: E402

pytestmark = pytest.mark.skipif(
    BACKEND != "postgres",
    reason="Demo 数据层原语需 PG（RUN_PG_TESTS=1）",
)


@pytest.fixture
def pg_conn(pg_uri):
    """直连 pg（dict_row）：造 slides 行 / 拨回过期时间。"""
    import psycopg
    c = psycopg.connect(pg_uri)
    c.row_factory = psycopg.rows.dict_row
    yield c
    c.close()


def _slide(pg_conn, name=None):
    """插入一个 slides 行，返回稳定 slide_id（catalog_add 的存在性前提）。"""
    sid = "sld_" + uuid.uuid4().hex[:10]
    with pg_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO slides (slide_id, legacy_filename) VALUES (%s,%s)",
            (sid, name or (sid + ".svs")))
    pg_conn.commit()
    return sid


# --------------------------------------------------------------------------- #
# capability 生命周期
# --------------------------------------------------------------------------- #
def test_capability_create_and_lookup():
    cap = demo_store.create_capability(
        "dmo_1", "hash_1", ip_prefix_hash="ip_h")
    assert cap["id"] == "dmo_1"
    assert cap["run_state"] == "available"
    assert cap["ip_prefix_hash"] == "ip_h"
    assert cap["expires_at"] > cap["created_at"]
    got = demo_store.get_valid_capability("hash_1")
    assert got["id"] == "dmo_1"
    assert demo_store.get_valid_capability("hash_other") is None
    # token_hash 唯一：重复签发冲突
    with pytest.raises(ValueError):
        demo_store.create_capability("dmo_2", "hash_1")
    # id 冲突同理
    with pytest.raises(ValueError):
        demo_store.create_capability("dmo_1", "hash_2")
    # 入参校验
    with pytest.raises(ValueError):
        demo_store.create_capability("", "hash_3")
    with pytest.raises(ValueError):
        demo_store.create_capability("dmo_3", "")


def test_expired_capability_is_none(pg_conn):
    demo_store.create_capability("dmo_1", "hash_1")
    with pg_conn.cursor() as cur:
        cur.execute(
            "UPDATE demo_sessions SET expires_at = now() - interval '1 minute' "
            "WHERE id='dmo_1'")
    pg_conn.commit()
    assert demo_store.get_valid_capability("hash_1") is None


# --------------------------------------------------------------------------- #
# run 预占 / 消费 / 释放 / 回收
# --------------------------------------------------------------------------- #
def test_reserve_run_cas_conflict_returns_none():
    demo_store.create_capability("dmo_1", "hash_1")
    r = demo_store.reserve_run("dmo_1", "req_1", "sld_a", "rev_1")
    assert r["run_state"] == "reserved"
    assert r["request_id"] == "req_1"
    assert r["slide_id"] == "sld_a"
    assert r["asset_revision"] == "rev_1"
    assert r["reservation_expires_at"] > r["reserved_at"]
    # 已 reserved 且不同 request_id：CAS 不满足 → None
    assert demo_store.reserve_run("dmo_1", "req_2", "sld_a", "rev_1") is None
    # 同 request_id 在途重放：不升 attempt，标记 replayed
    replay = demo_store.reserve_run("dmo_1", "req_1", "sld_a", "rev_1")
    assert replay is not None
    assert replay["run_state"] == "reserved"
    assert replay["request_id"] == "req_1"
    assert replay["attempt"] == 1
    assert replay["rollback_epoch"] == 1
    assert replay.get("replayed") is True
    # 不存在的 capability → None
    assert demo_store.reserve_run("dmo_x", "req_3", "sld_a", "rev_1") is None


def test_reserve_run_from_consumed_when_allowed():
    demo_store.create_capability("dmo_1", "hash_1")
    demo_store.reserve_run("dmo_1", "req_1", "sld_a", "rev_1")
    demo_store.consume_run("dmo_1", "hp_sess_1")
    assert demo_store.reserve_run("dmo_1", "req_2", "sld_a", "rev_1") is None
    again = demo_store.reserve_run(
        "dmo_1", "req_2", "sld_a", "rev_1",
        from_states=(demo_store.RUN_STATE_AVAILABLE, demo_store.RUN_STATE_CONSUMED))
    assert again is not None
    assert again["run_state"] == "reserved"
    assert again["request_id"] == "req_2"
    assert again["attempt"] == 2
    assert again["histopilot_session_id"] is None


def test_concurrent_reserve_run_single_winner():
    """并发 reserve 同一 capability：CAS 保证只有一个成功（双击/多标签页）。"""
    demo_store.create_capability("dmo_1", "hash_1")

    def worker(i):
        return demo_store.reserve_run("dmo_1", "req_%d" % i, "sld_a", "rev_1")

    with ThreadPoolExecutor(max_workers=8) as ex:
        results = list(ex.map(worker, range(16)))
    wins = [r for r in results if r is not None]
    assert len(wins) == 1
    assert wins[0]["run_state"] == "reserved"
    # 输了的请求全部 None
    assert all(r is None for r in results if r is not wins[0])


def test_consume_run_and_release_run_state_machine():
    demo_store.create_capability("dmo_1", "hash_1")
    demo_store.reserve_run("dmo_1", "req_1", "sld_a", "rev_1")
    out = demo_store.consume_run("dmo_1", "hp_sess_1")
    assert out["run_state"] == "consumed"
    assert out["histopilot_session_id"] == "hp_sess_1"
    assert out["consumed_at"] is not None
    # 幂等：已 consumed 直接返回
    assert demo_store.consume_run(
        "dmo_1", "hp_sess_1")["run_state"] == "consumed"
    # consumed 不可 release（防误退款）
    with pytest.raises(ValueError):
        demo_store.release_run("dmo_1")
    # 不存在 → None；available 状态不能直接 consume
    assert demo_store.consume_run("dmo_none", "hp") is None
    demo_store.create_capability("dmo_2", "hash_2")
    with pytest.raises(ValueError):
        demo_store.consume_run("dmo_2", "hp")


def test_release_run_stale_attempt_keeps_newer_try():
    """确认失败后重新预占才换代；旧 attempt/request_id 不得释放新 run。"""
    demo_store.create_capability("dmo_1", "hash_1")
    first = demo_store.reserve_run("dmo_1", "req_1", "sld_a", "rev_1")
    assert first["attempt"] == 1
    demo_store.release_run("dmo_1", expected_attempt=1, expected_request_id="req_1")
    second = demo_store.reserve_run("dmo_1", "req_2", "sld_a", "rev_1")
    assert second["attempt"] == 2
    assert second["request_id"] == "req_2"
    with pytest.raises(demo_store.RunAttemptConflict):
        demo_store.release_run("dmo_1", expected_attempt=1, expected_request_id="req_1")
    with pytest.raises(demo_store.RunAttemptConflict):
        demo_store.consume_run("dmo_1", "hp_stale", expected_attempt=2,
                               expected_request_id="req_1")
    assert demo_store.get_session("dmo_1")["run_state"] == "reserved"
    out = demo_store.consume_run("dmo_1", "hp_sess", expected_attempt=2,
                                 expected_request_id="req_2")
    assert out["run_state"] == "consumed"


def test_consumed_rerun_attempt_is_monotonic_against_aba():
    """从 consumed 再预占不得把 attempt 重置为 1，旧对账不得动新 run。"""
    demo_store.create_capability("dmo_1", "hash_1")
    demo_store.reserve_run("dmo_1", "req_1", "sld_a", "rev_1")
    demo_store.consume_run("dmo_1", "hp_1", expected_attempt=1,
                           expected_request_id="req_1")
    nxt = demo_store.reserve_run(
        "dmo_1", "req_2", "sld_a", "rev_1",
        from_states=(demo_store.RUN_STATE_AVAILABLE, demo_store.RUN_STATE_CONSUMED))
    assert nxt["attempt"] == 2
    assert nxt["request_id"] == "req_2"
    with pytest.raises(demo_store.RunAttemptConflict):
        demo_store.release_run("dmo_1", expected_attempt=1, expected_request_id="req_1")
    with pytest.raises(demo_store.RunAttemptConflict):
        demo_store.consume_run("dmo_1", "hp_stale", expected_attempt=1,
                               expected_request_id="req_1")
    out = demo_store.consume_run("dmo_1", "hp_2", expected_attempt=2,
                                 expected_request_id="req_2")
    assert out["run_state"] == "consumed"


def test_reserved_replay_keeps_attempt_so_original_consume_succeeds():
    demo_store.create_capability("dmo_1", "hash_1")
    first = demo_store.reserve_run("dmo_1", "req_1", "sld_a", "rev_1")
    replay = demo_store.reserve_run("dmo_1", "req_1", "sld_a", "rev_1")
    assert first["attempt"] == replay["attempt"] == 1
    assert replay.get("replayed") is True
    assert (first.get("rollback_epoch") or 0) == 0
    assert replay["rollback_epoch"] == 1
    out = demo_store.consume_run("dmo_1", "hp_sess", expected_attempt=1,
                                 expected_request_id="req_1")
    assert out["run_state"] == "consumed"


def test_reserved_replay_invalidates_original_demo_rollback():
    """A reserve → B replay → A release 不得把 Demo run 放回 available。"""
    demo_store.create_capability("dmo_1", "hash_1")
    original = demo_store.reserve_run("dmo_1", "req_1", "sld_a", "rev_1")
    replay = demo_store.reserve_run("dmo_1", "req_1", "sld_a", "rev_1")
    assert original.get("replayed") is not True
    assert replay.get("replayed") is True
    assert original["attempt"] == replay["attempt"] == 1
    assert (original.get("rollback_epoch") or 0) == 0
    assert replay["rollback_epoch"] == 1
    with pytest.raises(demo_store.RunAttemptConflict):
        demo_store.release_run(
            "dmo_1", expected_attempt=original["attempt"],
            expected_request_id="req_1",
            expected_rollback_epoch=original.get("rollback_epoch") or 0)
    assert demo_store.get_session("dmo_1")["run_state"] == "reserved"
    out = demo_store.consume_run("dmo_1", "hp_sess", expected_attempt=1,
                                 expected_request_id="req_1")
    assert out["run_state"] == "consumed"


def test_release_run_idempotent_and_allows_retry():
    demo_store.create_capability("dmo_1", "hash_1")
    assert demo_store.release_run("dmo_1")["run_state"] == "available"  # 幂等
    demo_store.reserve_run("dmo_1", "req_1", "sld_a", "rev_1")
    out = demo_store.release_run("dmo_1")
    assert out["run_state"] == "available"
    assert out["reserved_at"] is None and out["request_id"] is None
    # 释放后浏览器可重试（HistoPilot 接受前失败的语义）
    r = demo_store.reserve_run("dmo_1", "req_2", "sld_a", "rev_1")
    assert r is not None and r["request_id"] == "req_2"
    assert r["attempt"] == 2
    assert demo_store.release_run("dmo_none") is None


def test_count_ip_runs_only_reserved_or_consumed():
    demo_store.create_capability("dmo_a", "hash_a", ip_prefix_hash="ipp_1")
    demo_store.create_capability("dmo_b", "hash_b", ip_prefix_hash="ipp_1")
    demo_store.create_capability("dmo_c", "hash_c", ip_prefix_hash="ipp_1")
    demo_store.create_capability("dmo_other", "hash_o", ip_prefix_hash="ipp_2")
    assert demo_store.count_ip_runs("ipp_1")["count"] == 0
    demo_store.reserve_run("dmo_a", "req_a", "sld_a", "rev_1")
    demo_store.reserve_run("dmo_b", "req_b", "sld_a", "rev_1")
    demo_store.consume_run("dmo_b", "hp_b")
    demo_store.reserve_run("dmo_c", "req_c", "sld_a", "rev_1")
    demo_store.release_run("dmo_c")  # available 不计入
    demo_store.reserve_run("dmo_other", "req_o", "sld_a", "rev_1")
    usage = demo_store.count_ip_runs("ipp_1")
    assert usage["count"] == 2
    assert usage["retry_after_seconds"] >= 1
    assert demo_store.count_ip_runs("ipp_2")["count"] == 1
    assert demo_store.count_ip_runs("ipp_missing")["count"] == 0


def test_reset_demo_runs_reopens_browser_and_ip_gates():
    """owner 一键重置：consumed 退回 available，IP 桶清零，同一 cookie 可再预占。"""
    demo_store.create_capability("dmo_1", "hash_1", ip_prefix_hash="ipp_1")
    demo_store.create_capability("dmo_idle", "hash_idle")
    demo_store.reserve_run("dmo_1", "req_1", "sld_a", "rev_1", ip_prefix_hash="ipp_1")
    demo_store.consume_run("dmo_1", "hp_1")
    assert demo_store.count_ip_runs("ipp_1")["count"] == 1
    assert demo_store.count_run_states()["consumed"] == 1
    assert demo_store.count_run_states()["available"] == 1
    ids = demo_store.reset_demo_runs()
    assert ids == ["dmo_1"]
    row = demo_store.get_session("dmo_1")
    assert row["run_state"] == "available"
    assert row["request_id"] is None
    assert demo_store.count_ip_runs("ipp_1")["count"] == 0
    assert demo_store.count_run_states()["consumed"] == 0
    nxt = demo_store.reserve_run("dmo_1", "req_2", "sld_a", "rev_1")
    assert nxt is not None and nxt["request_id"] == "req_2"


def test_count_ip_runs_empty_hash_shares_unknown_bucket():
    demo_store.create_capability("dmo_u", "hash_u", ip_prefix_hash="unknown")
    demo_store.reserve_run("dmo_u", "req_u", "sld_a", "rev_1")
    assert demo_store.count_ip_runs("")["count"] == 1
    assert demo_store.count_ip_runs(None)["count"] == 1


def test_reclaim_expired_runs():
    demo_store.create_capability("dmo_1", "hash_1")
    demo_store.reserve_run("dmo_1", "req_1", "sld_a", "rev_1", ttl_seconds=60)
    assert demo_store.reclaim_expired_runs(time.time()) == []  # 未过期
    reclaimed = demo_store.reclaim_expired_runs(time.time() + 120)
    assert [r["id"] for r in reclaimed] == ["dmo_1"]
    got = demo_store.get_valid_capability("hash_1")
    assert got["run_state"] == "available"  # 回收后可再次预占
    assert demo_store.reserve_run(
        "dmo_1", "req_3", "sld_a", "rev_1") is not None


def test_revoke_by_slide_expires_capability_and_terminates_runs():
    demo_store.create_capability("dmo_1", "hash_1")
    demo_store.reserve_run("dmo_1", "req_1", "sld_x", "rev_1")  # 绑定 slide
    demo_store.create_capability("dmo_2", "hash_2")  # 未绑 slide（slide_id NULL）
    res = demo_store.revoke_by_slide("sld_x")
    assert res["expired_capabilities"] == 1
    assert res["terminated_runs"] == ["dmo_1"]
    # capability 立即失效
    assert demo_store.get_valid_capability("hash_1") is None
    assert demo_store.get_valid_capability("hash_2") is not None  # 不受影响
    # 终止的 run 被回收转回 available，但 capability 已失效不能再预占
    reclaimed = demo_store.reclaim_expired_runs(time.time() + 1)
    assert [r["id"] for r in reclaimed] == ["dmo_1"]
    assert demo_store.reserve_run("dmo_1", "req_9", "sld_x", "rev_1") is None


# --------------------------------------------------------------------------- #
# demo_catalog
# --------------------------------------------------------------------------- #
def test_catalog_add_requires_slide_exists(pg_conn):
    with pytest.raises(ValueError):
        demo_store.catalog_add("sld_nope")


def test_catalog_add_list_ordered_set_default(pg_conn):
    s1 = _slide(pg_conn, "one.svs")
    s2 = _slide(pg_conn, "two.svs")
    e1 = demo_store.catalog_add(
        s1, display_name="一切片", description="教学用", sort_order=2,
        added_by="usr_owner")
    e2 = demo_store.catalog_add(s2, display_name="二切片", sort_order=1)
    assert e1["slide_id"] == s1 and e1["added_by"] == "usr_owner"
    assert demo_store.catalog_list_ordered() == [e2, e1]  # 按 sort_order
    # UPSERT：改展示名不新增条目
    e1b = demo_store.catalog_add(s1, display_name="新名", sort_order=2)
    ordered = demo_store.catalog_list_ordered()
    assert len(ordered) == 2
    assert ordered[1]["display_name"] == "新名"
    # 唯一默认切片
    assert demo_store.catalog_set_default(s1)["is_default"] is True
    assert demo_store.catalog_set_default(s2)["is_default"] is True
    flags = {r["slide_id"]: r["is_default"] for r in
             demo_store.catalog_list_ordered()}
    assert flags == {s1: False, s2: True}
    with pytest.raises(ValueError):
        demo_store.catalog_set_default("sld_nope")


def test_catalog_remove_revokes_capabilities(pg_conn):
    s1 = _slide(pg_conn, "cat.svs")
    demo_store.catalog_add(s1)
    demo_store.create_capability("dmo_1", "hash_1")
    demo_store.reserve_run("dmo_1", "req_1", s1, "rev_1")
    ret = demo_store.catalog_remove(s1)
    assert ret is not None and ret["entry"]["slide_id"] == s1
    # remove 联动 revoke_by_slide：capability 立即失效、run 终止
    assert ret["revoke"]["expired_capabilities"] == 1
    assert ret["revoke"]["terminated_runs"] == ["dmo_1"]
    assert demo_store.get_valid_capability("hash_1") is None
    assert demo_store.catalog_list_ordered() == []
    assert demo_store.catalog_remove(s1) is None  # 再删 → None
