"""百度链路 review 回归：同名文件和枚举领取令牌（真实 PG）。"""
import pytest

import baidu_import_store as store
from baidu_adapter import AdapterError, FakeBaiduAdapter
from _baidu_helpers import (expire_enumeration_lease, install_fake,
                            make_ready_enumeration)

OWNER = "baidu-review-owner"


def _sql(query, args=()):
    conn = store._connect()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(query, args)
                return cur.fetchall() if cur.description else None
    finally:
        conn.close()


@pytest.mark.parametrize("second_size", [4, 8])
def test_duplicate_names_rejected_before_quota_or_transfer(monkeypatch, second_size):
    fake, enum_id, cands = make_ready_enumeration(
        monkeypatch, owner=OWNER, entries=[
            {"path": "/A/sample.svs", "size": 4},
            {"path": "/B/sample.svs", "size": second_size},
        ])
    ids = [cands[p]["id"] for p in ("A/sample.svs", "B/sample.svs")]
    def quota(*args):
        pytest.fail("同名拒绝必须发生在配额预占之前")
    with pytest.raises(store.ValidationError) as exc:
        store.create_import(OWNER, enum_id, ids, quota_hook=quota)
    assert exc.value.code == "duplicate_filename"
    assert "分批导入" in str(exc.value)
    assert _sql("SELECT id FROM baidu_import_batches") == []
    assert fake.counters()["transfer"] == 0
    # 分批导入仍可接受；同一 candidate 重复提交仍按既有合同去重。
    for cid in ids:
        assert store.create_import(OWNER, enum_id, [cid, cid])["state"] == "queued"


def test_legacy_duplicate_batch_stops_before_external_actions(monkeypatch, tmp_path):
    fake, enum_id, cands = make_ready_enumeration(
        monkeypatch, owner=OWNER, entries=[
            {"path": "/A/sample.svs", "size": 4},
            {"path": "/B/other.svs", "size": 4},
        ])
    batch = store.create_import(OWNER, enum_id, [
        cands["A/sample.svs"]["id"], cands["B/other.svs"]["id"]])
    # 模拟旧版本已经接受的冲突批次。
    _sql("UPDATE baidu_import_items SET name='sample.svs' WHERE batch_id=%s",
         (batch["id"],))
    before = fake.counters()
    view = store.run_batch(batch["id"], fake, staging_root=tmp_path)
    assert view["state"] == "failed"
    assert [r["error_code"] for r in _sql(
        "SELECT error_code FROM baidu_import_items WHERE batch_id=%s",
        (batch["id"],))] == ["duplicate_filename", "duplicate_filename"]
    for action in ("transfer", "download", "delete"):
        assert fake.counters()[action] == before[action]


@pytest.mark.parametrize("old_failure", [False, True])
@pytest.mark.parametrize("new_owner", ["baidu-worker", "another-worker"])
def test_stale_enumerator_cannot_overwrite_new_result(monkeypatch, old_failure, new_owner):
    fake = install_fake(monkeypatch, entries=[{"path": "/old.svs", "size": 4}])
    enum_id = store.create_enumeration(OWNER, "https://pan.baidu.com/s/1Review")['id']
    old = store.claim_enumeration("baidu-worker")
    original = fake.list_share_page

    def take_over(*args):
        # 旧 worker 阻塞在 CLI 期间被新 worker 领取并完成。
        expire_enumeration_lease(enum_id)
        new = store.claim_enumeration(new_owner)
        assert new["lease_token"] != old["lease_token"]
        assert store._progress_enumeration(
            enum_id, old["lease_owner"], 999, old["lease_token"]) is False
        assert store.heartbeat_enumeration(
            enum_id, old["lease_owner"], old["lease_token"]) is False
        winner = FakeBaiduAdapter(entries=[{"path": "/new.svs", "size": 8}])
        assert store.run_one_enumeration(new, winner)["state"] == "ready"
        if old_failure:
            raise AdapterError("connector_timeout", "injected")
        return original(*args)

    monkeypatch.setattr(fake, "list_share_page", take_over)
    result = store.run_one_enumeration(old, fake)
    assert result["state"] == "ready"
    view = store.get_enumeration(enum_id, OWNER)
    assert view["scanned_count"] == view["candidate_count"] == 1
    assert view["error_code"] is None
    assert [c["relative_path"] for c in store.list_candidates(
        enum_id, OWNER)["items"]] == ["new.svs"]


def test_candidates_and_terminal_state_commit_atomically(monkeypatch):
    fake = install_fake(monkeypatch, entries=[{"path": "/a.svs", "size": 4}])
    enum_id = store.create_enumeration(OWNER, "https://pan.baidu.com/s/1Atomic")['id']
    claim = store.claim_enumeration()
    original = store._insert_candidates

    def crash_after_insert(*args):
        original(*args)
        raise RuntimeError("injected before terminal update")

    monkeypatch.setattr(store, "_insert_candidates", crash_after_insert)
    with pytest.raises(RuntimeError, match="injected"):
        store.run_one_enumeration(claim, fake)
    assert _sql("SELECT id FROM baidu_candidates WHERE enumeration_id=%s",
                (enum_id,)) == []
    assert store.get_enumeration(enum_id, OWNER)["state"] == "enumerating"
    monkeypatch.setattr(store, "_insert_candidates", original)
    expire_enumeration_lease(enum_id)
    assert store.run_one_enumeration(store.claim_enumeration(), fake)["state"] == "ready"


def test_copy_reconciliation_error_reaches_terminal_state(monkeypatch, tmp_path):
    fake, enum_id, cands = make_ready_enumeration(
        monkeypatch, owner=OWNER, entries=[{"path": "/a.svs", "size": 4}])
    batch = store.create_import(OWNER, enum_id, [cands['a.svs']['id']])
    def fail(*args):
        raise AdapterError('connector_failed', 'injected list error')
    monkeypatch.setattr(fake, 'list_batch_copies', fail)
    view = store.run_batch(batch['id'], fake, staging_root=tmp_path)
    assert view['state'] == 'failed'
    assert view['items'][0]['error_code'] == 'connector_failed'
    assert fake.counters()['transfer'] == 0
