# -*- coding: utf-8 -*-
"""demo_sessions + demo_runs + demo_catalog + IP 请求速率数据层测试。

批次 E（docs ai-money-budget-bugfix-and-simplification-plan.md §4/§9.5）。

仅 RUN_PG_TESTS=1 时真跑（conftest 起真实 PG + 每用例 TRUNCATE）。

覆盖：
  - capability 生命周期：创建 / token_hash 查询 / 过期即 None / 重复冲突；
  - reserve_run：同 capability 顺序多次（终态后可再开）；同 capability 并发
    第二个 run 被 DB 部分唯一索引拒绝；同 request_id 在途重放不升 attempt；
    released 后同 ID 重试 attempt+1；finished/expired 终态同 ID 拒绝；
  - capability 过期不能新开 run（DemoCapabilityExpired）；
  - accept / release / finish / expire 状态机与幂等；accepted 不可 release
    （防误退款）；reserved TTL 与 accepted 重连窗口；
  - list_active_expired / latest_run_for_capability / get_run_for_session /
    count_run_states / reset_demo_runs（在途 → expired）；
  - demo_ip_request_rate：固定窗口计数、窗口滚动重置、超限 retry_after、
    缺 hash 归 unknown 桶；
  - revoke_by_slide：终止该切片在途 run（capability 多切片复用，不整体失效）；
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


def _expire_capability(pg_conn, cap_id):
    with pg_conn.cursor() as cur:
        cur.execute("UPDATE demo_sessions SET expires_at="
                    " now() - interval '1 minute' WHERE id=%s", (cap_id,))
    pg_conn.commit()


# --------------------------------------------------------------------------- #
# capability 生命周期
# --------------------------------------------------------------------------- #
def test_capability_create_and_lookup():
    cap = demo_store.create_capability(
        "dmo_1", "hash_1", ip_prefix_hash="ip_h")
    assert cap["id"] == "dmo_1"
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
    _expire_capability(pg_conn, "dmo_1")
    assert demo_store.get_valid_capability("hash_1") is None


# --------------------------------------------------------------------------- #
# reserve / accept / finish / release / expire 状态机
# --------------------------------------------------------------------------- #
def test_reserve_run_basic_fields_and_missing_capability():
    demo_store.create_capability("dmo_1", "hash_1")
    r = demo_store.reserve_run("dmo_1", "req_1", "sld_a", "rev_1",
                               ip_prefix_hash="ipp_1")
    assert r["state"] == "reserved"
    assert r["capability_id"] == "dmo_1"
    assert r["request_id"] == "req_1"
    assert r["slide_id"] == "sld_a"
    assert r["asset_revision"] == "rev_1"
    assert r["attempt"] == 1
    assert r["expires_at"] > r["created_at"]
    assert demo_store.reserve_run("dmo_x", "req_9", "sld_a", "rev_1") is None


def test_sequential_runs_after_terminal_states():
    """同 capability 顺序多次 run：终态（finished/released/expired）后可再开。"""
    demo_store.create_capability("dmo_1", "hash_1")
    r1 = demo_store.reserve_run("dmo_1", "req_1", "sld_a", "rev_1")
    # 在途时第二个 run 被拒（单 active 并发闸）
    with pytest.raises(demo_store.DemoRunActiveConflict):
        demo_store.reserve_run("dmo_1", "req_2", "sld_a", "rev_1")
    # accepted 后仍被拒
    demo_store.accept_run(r1["demo_run_id"], "hp_1")
    with pytest.raises(demo_store.DemoRunActiveConflict):
        demo_store.reserve_run("dmo_1", "req_2", "sld_a", "rev_1")
    # 流正常结束 → finished 终态 → 可再开（无限顺序体验）
    demo_store.finish_run(r1["demo_run_id"])
    r2 = demo_store.reserve_run("dmo_1", "req_2", "sld_b", "rev_2")
    assert r2["state"] == "reserved"
    assert r2["request_id"] == "req_2"
    assert r2["slide_id"] == "sld_b"
    # released 路径：接受前失败释放后也可再开
    demo_store.release_run(r2["demo_run_id"])
    r3 = demo_store.reserve_run("dmo_1", "req_3", "sld_a", "rev_1")
    assert r3["state"] == "reserved"
    # expired 路径：accepted 到期终态后可再开
    demo_store.accept_run(r3["demo_run_id"], "hp_3")
    demo_store.expire_run(r3["demo_run_id"])
    r4 = demo_store.reserve_run("dmo_1", "req_4", "sld_a", "rev_1")
    assert r4["state"] == "reserved"
    # 流水保留（append-only）：四条 run 行
    counts = demo_store.count_run_states()
    assert counts["total"] == 4
    assert counts["active"] == 1


def test_concurrent_reserve_single_winner():
    """并发 reserve 同一 capability：capability 行锁 + 部分唯一索引只留一个。"""
    demo_store.create_capability("dmo_1", "hash_1")

    def worker(i):
        try:
            return demo_store.reserve_run("dmo_1", "req_%d" % i, "sld_a", "rev_1")
        except (demo_store.DemoRunActiveConflict, demo_store.DemoRunFinalConflict):
            return None

    with ThreadPoolExecutor(max_workers=8) as ex:
        results = list(ex.map(worker, range(16)))
    wins = [r for r in results if r is not None]
    assert len(wins) == 1
    assert wins[0]["state"] == "reserved"
    assert all(r is None for r in results if r is not wins[0])


def test_db_partial_unique_index_blocks_second_active_run(pg_conn):
    """绕过应用层（直插行）时，部分唯一索引仍拒绝第二个 active run。"""
    import psycopg
    demo_store.create_capability("dmo_1", "hash_1")
    demo_store.reserve_run("dmo_1", "req_1", "sld_a", "rev_1")
    with pytest.raises(psycopg.errors.UniqueViolation):
        with pg_conn.cursor() as cur:
            cur.execute(
                "INSERT INTO demo_runs (demo_run_id, capability_id, "
                "request_id, state, expires_at) "
                "VALUES ('dmr_evil', 'dmo_1', 'req_evil', 'reserved', "
                " now() + interval '10 minutes')")
        pg_conn.commit()
    pg_conn.rollback()
    # 终态行不受该索引约束（同 capability 可有多条历史流水）
    with pg_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO demo_runs (demo_run_id, capability_id, "
            "request_id, state, expires_at) "
            "VALUES ('dmr_hist', 'dmo_1', 'req_hist', 'finished', "
            " now() + interval '10 minutes')")
    pg_conn.commit()


def test_same_request_id_replay_and_released_retry():
    demo_store.create_capability("dmo_1", "hash_1")
    first = demo_store.reserve_run("dmo_1", "req_1", "sld_a", "rev_1")
    # 在途同 ID 重放：不升 attempt，标记 replayed，rollback_epoch+1
    replay = demo_store.reserve_run("dmo_1", "req_1", "sld_a", "rev_1")
    assert replay is not None
    assert replay.get("replayed") is True
    assert replay["attempt"] == first["attempt"] == 1
    assert (first.get("rollback_epoch") or 0) == 0
    assert replay["rollback_epoch"] == 1
    # 重放令原请求的 release CAS 失效（防 ABA：旧回滚不得动新执行）
    with pytest.raises(demo_store.RunAttemptConflict):
        demo_store.release_run(
            first["demo_run_id"], expected_attempt=1,
            expected_request_id="req_1", expected_rollback_epoch=0)
    # 确认式释放（不校验 epoch）→ released 终态
    demo_store.release_run(first["demo_run_id"], expected_request_id="req_1")
    # released 后同 ID 重试：attempt+1、epoch 归零（网络重试属新执行尝试）
    again = demo_store.reserve_run("dmo_1", "req_1", "sld_b", "rev_2")
    assert again["state"] == "reserved"
    assert again["attempt"] == 2
    assert again["rollback_epoch"] == 0
    assert again["slide_id"] == "sld_b"
    assert again["histopilot_session_id"] is None


def test_terminal_request_id_cannot_be_reopened():
    demo_store.create_capability("dmo_1", "hash_1")
    r = demo_store.reserve_run("dmo_1", "req_1", "sld_a", "rev_1")
    demo_store.accept_run(r["demo_run_id"], "hp_1")
    demo_store.finish_run(r["demo_run_id"])
    with pytest.raises(demo_store.DemoRunFinalConflict) as ei:
        demo_store.reserve_run("dmo_1", "req_1", "sld_a", "rev_1")
    assert ei.value.code == "demo_run_request_final"
    # 新 request_id 可开
    assert demo_store.reserve_run(
        "dmo_1", "req_2", "sld_a", "rev_1")["state"] == "reserved"


def test_capability_expired_cannot_start_run(pg_conn):
    """capability 过期不能新开 run；既有终态流水保留。"""
    demo_store.create_capability("dmo_1", "hash_1")
    r1 = demo_store.reserve_run("dmo_1", "req_1", "sld_a", "rev_1")
    demo_store.accept_run(r1["demo_run_id"], "hp_1")
    demo_store.finish_run(r1["demo_run_id"])
    _expire_capability(pg_conn, "dmo_1")
    with pytest.raises(demo_store.DemoCapabilityExpired):
        demo_store.reserve_run("dmo_1", "req_2", "sld_a", "rev_1")
    # 既有终态流水保留
    assert demo_store.get_run_by_request("dmo_1", "req_1")["state"] == "finished"
    assert demo_store.count_run_states()["total"] == 1


def test_accept_run_state_machine():
    demo_store.create_capability("dmo_1", "hash_1")
    r = demo_store.reserve_run("dmo_1", "req_1", "sld_a", "rev_1")
    rid = r["demo_run_id"]
    out = demo_store.accept_run(rid, "hp_sess_1")
    assert out["state"] == "accepted"
    assert out["histopilot_session_id"] == "hp_sess_1"
    assert out["accepted_at"] is not None
    assert out["expires_at"] > out["accepted_at"]  # 重连窗口
    # 幂等：已 accepted 且 session 一致
    assert demo_store.accept_run(rid, "hp_sess_1")["state"] == "accepted"
    # session 不一致 → 冲突
    with pytest.raises(demo_store.RunAttemptConflict):
        demo_store.accept_run(rid, "hp_sess_other")
    # accepted 不可 release（防误退款）
    with pytest.raises(ValueError):
        demo_store.release_run(rid)
    assert demo_store.accept_run("dmr_none", "hp") is None


def test_accept_run_rejects_terminal_and_cas(pg_conn):
    demo_store.create_capability("dmo_1", "hash_1")
    r = demo_store.reserve_run("dmo_1", "req_1", "sld_a", "rev_1")
    rid = r["demo_run_id"]
    # released 终态不能 accept
    demo_store.release_run(rid)
    with pytest.raises(ValueError):
        demo_store.accept_run(rid, "hp_x")
    # 重新预占（同 ID）后 CAS 校验
    r2 = demo_store.reserve_run("dmo_1", "req_1", "sld_a", "rev_1")
    assert r2["attempt"] == 2
    with pytest.raises(demo_store.RunAttemptConflict):
        demo_store.accept_run(rid, "hp_x", expected_attempt=1)
    with pytest.raises(demo_store.RunAttemptConflict):
        demo_store.accept_run(rid, "hp_x", expected_request_id="req_other")
    out = demo_store.accept_run(rid, "hp_x", expected_attempt=2,
                                expected_request_id="req_1")
    assert out["state"] == "accepted"


def test_finish_and_expire_runs():
    demo_store.create_capability("dmo_1", "hash_1")
    r = demo_store.reserve_run("dmo_1", "req_1", "sld_a", "rev_1")
    rid = r["demo_run_id"]
    # reserved 上 finish 是防御性 no-op（不改状态，交由对账定局）
    assert demo_store.finish_run(rid)["state"] == "reserved"
    demo_store.accept_run(rid, "hp_1")
    out = demo_store.finish_run(rid)
    assert out["state"] == "finished"
    assert out["finished_at"] is not None
    # 幂等
    assert demo_store.finish_run(rid)["state"] == "finished"
    # expire：active → expired；终态幂等
    r2 = demo_store.reserve_run("dmo_1", "req_2", "sld_a", "rev_1")
    assert demo_store.expire_run(r2["demo_run_id"])["state"] == "expired"
    assert demo_store.expire_run(r2["demo_run_id"])["state"] == "expired"
    assert demo_store.expire_run("dmr_none") is None
    assert demo_store.finish_run("dmr_none") is None


def test_release_run_idempotent_allows_retry():
    demo_store.create_capability("dmo_1", "hash_1")
    r = demo_store.reserve_run("dmo_1", "req_1", "sld_a", "rev_1")
    out = demo_store.release_run(r["demo_run_id"])
    assert out["state"] == "released"
    # 幂等
    assert demo_store.release_run(r["demo_run_id"])["state"] == "released"
    assert demo_store.release_run("dmr_none") is None
    # 释放后浏览器可重试（新 request_id 或同 ID attempt+1）
    r2 = demo_store.reserve_run("dmo_1", "req_2", "sld_a", "rev_1")
    assert r2["state"] == "reserved"


def test_lazy_expire_stale_active_at_reserve(pg_conn):
    """reserve 时惰性终态：过期的 active run 不再阻塞 capability。"""
    demo_store.create_capability("dmo_1", "hash_1")
    r1 = demo_store.reserve_run("dmo_1", "req_1", "sld_a", "rev_1")
    demo_store.accept_run(r1["demo_run_id"], "hp_1")
    # 拨回过期时间（模拟 accepted 重连窗口到期 / reserved TTL 到期）
    with pg_conn.cursor() as cur:
        cur.execute("UPDATE demo_runs SET expires_at="
                    " now() - interval '1 hour' WHERE capability_id='dmo_1'")
    pg_conn.commit()
    r2 = demo_store.reserve_run("dmo_1", "req_2", "sld_a", "rev_1")
    assert r2["state"] == "reserved"
    assert demo_store.get_run_by_request("dmo_1", "req_1")["state"] == "expired"


def test_list_active_expired_and_extend(pg_conn):
    demo_store.create_capability("dmo_1", "hash_1")
    r1 = demo_store.reserve_run("dmo_1", "req_1", "sld_a", "rev_1",
                                ttl_seconds=60)
    assert demo_store.list_active_expired(time.time()) == []
    stale = demo_store.list_active_expired(time.time() + 120)
    assert [x["demo_run_id"] for x in stale] == [r1["demo_run_id"]]
    # 顺延（对账 HistoPilot 不可达不释放、顺延）
    demo_store.extend_run_reservation(r1["demo_run_id"], 600)
    assert demo_store.list_active_expired(time.time() + 120) == []
    assert demo_store.list_active_expired(time.time() + 700) != []
    # accepted 过期也进入对账清单（转 expired 解锁 capability）
    demo_store.accept_run(r1["demo_run_id"], "hp_1")
    with pg_conn.cursor() as cur:
        cur.execute("UPDATE demo_runs SET expires_at="
                    " now() - interval '1 hour' WHERE capability_id='dmo_1'")
    pg_conn.commit()
    expired = demo_store.list_active_expired(time.time())
    assert [x["state"] for x in expired] == ["accepted"]


def test_latest_run_and_session_binding():
    demo_store.create_capability("dmo_1", "hash_1")
    assert demo_store.latest_run_for_capability("dmo_1") is None
    r1 = demo_store.reserve_run("dmo_1", "req_1", "sld_a", "rev_1")
    demo_store.accept_run(r1["demo_run_id"], "hp_1")
    demo_store.finish_run(r1["demo_run_id"])
    # get_run_for_session 只认 accepted/finished
    got = demo_store.get_run_for_session("dmo_1", "hp_1")
    assert got["demo_run_id"] == r1["demo_run_id"]
    assert demo_store.get_run_for_session("dmo_1", "hp_other") is None
    # released/expired 不可读
    r2 = demo_store.reserve_run("dmo_1", "req_2", "sld_a", "rev_1")
    demo_store.release_run(r2["demo_run_id"])
    assert demo_store.get_run_for_session("dmo_1", None) is None
    # 最新 run 是 released（按钮态：可再跑）
    latest = demo_store.latest_run_for_capability("dmo_1")
    assert latest["state"] == "released"
    # 第二次 run 绑定不同 HP session，互不串读（顺序多次）
    r3 = demo_store.reserve_run("dmo_1", "req_3", "sld_b", "rev_2")
    demo_store.accept_run(r3["demo_run_id"], "hp_3")
    assert demo_store.get_run_for_session("dmo_1", "hp_1")["state"] == "finished"
    assert demo_store.get_run_for_session("dmo_1", "hp_3")["state"] == "accepted"


def test_count_run_states_shape():
    demo_store.create_capability("dmo_1", "hash_1")
    assert demo_store.count_run_states() == {
        "reserved": 0, "accepted": 0, "finished": 0, "released": 0,
        "expired": 0, "active": 0, "total": 0}
    r1 = demo_store.reserve_run("dmo_1", "req_1", "sld_a", "rev_1")
    demo_store.accept_run(r1["demo_run_id"], "hp_1")
    demo_store.finish_run(r1["demo_run_id"])
    demo_store.reserve_run("dmo_1", "req_2", "sld_a", "rev_1")
    counts = demo_store.count_run_states()
    assert counts == {"reserved": 1, "accepted": 0, "finished": 1,
                      "released": 0, "expired": 0, "active": 1, "total": 2}


def test_reset_demo_runs_expires_active_only():
    """owner 一键重置：在途 run → expired 终态（capability 立即可再开）。"""
    demo_store.create_capability("dmo_1", "hash_1")
    r1 = demo_store.reserve_run("dmo_1", "req_1", "sld_a", "rev_1")
    demo_store.accept_run(r1["demo_run_id"], "hp_1")
    demo_store.finish_run(r1["demo_run_id"])
    r2 = demo_store.reserve_run("dmo_1", "req_2", "sld_a", "rev_1")
    assert demo_store.count_run_states()["active"] == 1
    ids = demo_store.reset_demo_runs()
    assert ids == [r2["demo_run_id"]]
    assert demo_store.count_run_states()["active"] == 0
    assert demo_store.get_run_by_request("dmo_1", "req_2")["state"] == "expired"
    # 终态流水不动（finished 保留）
    assert demo_store.get_run_by_request("dmo_1", "req_1")["state"] == "finished"
    # 同 capability 可立即再开
    assert demo_store.reserve_run(
        "dmo_1", "req_3", "sld_a", "rev_1")["state"] == "reserved"


# --------------------------------------------------------------------------- #
# demo_ip_request_rate：短窗口请求速率（防刷/防 DoS）
# --------------------------------------------------------------------------- #
def test_ip_request_rate_fixed_window():
    base = time.time()
    # 窗口内计数递增；超限拒绝并给出 retry_after
    for i in range(1, 4):
        usage = demo_store.hit_ip_request_rate("ipp_1", limit=3, now=base)
        assert usage["allowed"] is True
        assert usage["count"] == i
    denied = demo_store.hit_ip_request_rate("ipp_1", limit=3, now=base)
    assert denied["allowed"] is False
    assert denied["count"] == 4
    assert denied["retry_after_seconds"] >= 1
    # 窗口滚动：越过窗口长度后整桶重置
    rolled = demo_store.hit_ip_request_rate(
        "ipp_1", limit=3, now=base + 61)
    assert rolled["allowed"] is True
    assert rolled["count"] == 1
    # 不同前缀独立计数
    other = demo_store.hit_ip_request_rate("ipp_2", limit=3, now=base)
    assert other["allowed"] is True and other["count"] == 1


def test_ip_request_rate_empty_hash_shares_unknown_bucket():
    base = time.time()
    demo_store.hit_ip_request_rate("unknown", limit=5, now=base)
    usage = demo_store.hit_ip_request_rate("", limit=5, now=base)
    assert usage["count"] == 2
    assert demo_store.hit_ip_request_rate(None, limit=5, now=base)["count"] == 3


# --------------------------------------------------------------------------- #
# revoke_by_slide：终止在途 run（capability 不整体失效）
# --------------------------------------------------------------------------- #
def test_revoke_by_slide_terminates_active_runs_only():
    demo_store.create_capability("dmo_1", "hash_1")
    r1 = demo_store.reserve_run("dmo_1", "req_1", "sld_x", "rev_1")
    demo_store.accept_run(r1["demo_run_id"], "hp_1")
    # 同 capability 在另一切片的在途 run 不受影响
    demo_store.create_capability("dmo_2", "hash_2")
    r2 = demo_store.reserve_run("dmo_2", "req_2", "sld_y", "rev_1")
    res = demo_store.revoke_by_slide("sld_x")
    assert res["expired_capabilities"] == 0
    assert [t["demo_run_id"] for t in res["terminated_runs"]] == \
        [r1["demo_run_id"]]
    assert res["terminated_runs"][0]["request_id"] == "req_1"
    assert demo_store.get_run_by_request("dmo_1", "req_1")["state"] == "expired"
    # capability 仍有效（多切片复用，不整体失效）
    assert demo_store.get_valid_capability("hash_1") is not None
    assert demo_store.get_valid_capability("hash_2") is not None
    # 其它切片在途 run 不受影响
    assert demo_store.get_run_by_request("dmo_2", "req_2")["state"] == "reserved"
    # sld_x 终止后 capability 可在新切片再开
    r3 = demo_store.reserve_run("dmo_1", "req_3", "sld_z", "rev_1")
    assert r3["state"] == "reserved"


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


def test_catalog_remove_revokes_active_runs(pg_conn):
    s1 = _slide(pg_conn, "cat.svs")
    demo_store.catalog_add(s1)
    demo_store.create_capability("dmo_1", "hash_1")
    r = demo_store.reserve_run("dmo_1", "req_1", s1, "rev_1")
    ret = demo_store.catalog_remove(s1)
    assert ret is not None and ret["entry"]["slide_id"] == s1
    # remove 联动 revoke_by_slide：该切片在途 run 被终止
    assert ret["revoke"]["terminated_runs"][0]["demo_run_id"] == \
        r["demo_run_id"]
    assert demo_store.get_run_by_request("dmo_1", "req_1")["state"] == "expired"
    assert demo_store.catalog_list_ordered() == []
    assert demo_store.catalog_remove(s1) is None  # 再删 → None
