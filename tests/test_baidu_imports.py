# -*- coding: utf-8 -*-
"""W5 B03/B04/B05/B11：百度导入 PG 状态机（fake 适配器注入，真实 PG）。

B03：嵌套目录递归枚举只在分享内、稳定去重；分页重复游标/超限明确
     失败；枚举阶段 transfer/download/delete 调用恒 0。
B04：混合格式可选性（KFBF 可选、目录/channel.json/MRXS 不可选）；
     空选 400、过期 409、越权 404。
B05：创建导入并发幂等与配额不足（零外部副作用）。
B11：列表详情与秘密隔离（无提取码/令牌/内部路径出线）。
"""
import json
import threading

import pytest

import baidu_import_http as http
import baidu_import_store as store
import upload_guard
from _baidu_helpers import (STANDARD_ENTRIES, expire_batch_lease,
                            install_fake, make_ready_enumeration,
                            set_expires_at, set_max_entries)

OWNER = "u-baidu-1"
OTHER = "u-baidu-2"
IDENT = {"role": "user", "user_id": OWNER}


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("BAIDU_SHARE_SECRET_KEY",
                       "test-baidu-share-secret-key-2026-09-14")


def _make_ready(monkeypatch, entries=None, extraction_code=None):
    return make_ready_enumeration(
        monkeypatch, owner=OWNER, entries=entries or STANDARD_ENTRIES,
        extraction_code=extraction_code)


# --------------------------------------------------------------------------- #
# B03：枚举（递归 / 副作用 / 重复游标 / 超限）
# --------------------------------------------------------------------------- #

def test_b03_nested_dir_enumeration_zero_side_effects(monkeypatch):
    fake, enum_id, by_path = _make_ready(monkeypatch)
    view = store.get_enumeration(enum_id, OWNER)
    assert view["state"] == "ready" and view["complete"] is True
    # 嵌套目录展开（相对路径都在本分享内）
    assert "A1/sample.svs" in by_path
    assert "A1/panel_kfbf/channel.json" in by_path
    assert "B1/deep/Slidedat.ini" in by_path
    # 目录本身也保留为候选（不可选 + 原因）
    assert by_path["A1"]["selectable"] is False
    assert by_path["A1"]["reason_code"] == "not_a_slide"
    # 枚举全阶段 transfer/download/delete 调用恒 0（fake 计数器 +
    # 枚举行审计列双断言）
    counters = fake.counters()
    assert counters["transfer"] == 0 and counters["download"] == 0 \
        and counters["delete"] == 0
    assert counters["list"] > 0
    import psycopg
    import os
    conn = psycopg.connect(os.environ["DATABASE_URL"])
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT transfer_calls, download_calls, delete_calls "
                "FROM baidu_enumerations WHERE id=%s", (enum_id,))
            assert cur.fetchone() == (0, 0, 0)
    finally:
        conn.close()


def test_b03_duplicate_cursor_fails_explicitly(monkeypatch):
    # 分页注入重复游标：必须显式失败（cursor_loop），不能当空分享/ready
    fake = install_fake(monkeypatch, entries=STANDARD_ENTRIES, page_size=3)
    fake.duplicate_cursor_from_page = 1
    monkeypatch.setattr(store, "PAGE_SIZE", 3)
    out = store.create_enumeration(
        OWNER, "https://pan.baidu.com/s/1TestShareId99")
    result = store.run_enumeration(out["id"], fake)
    assert result["state"] == "failed"
    assert result["error_code"] == "cursor_loop"
    view = store.get_enumeration(out["id"], OWNER)
    assert view["state"] == "failed" and view["complete"] is False
    # 失败枚举不能导入（409）
    cands = store.list_candidates(out["id"], OWNER)
    assert cands["items"] == []
    with pytest.raises(store.ConflictError):
        store.create_import(OWNER, out["id"], ["whatever"])


def test_b03_over_max_entries_incomplete_import_rejected(monkeypatch):
    fake, enum_id, by_path = _make_ready(monkeypatch)
    # 事后收紧 max_entries 模拟超限枚举
    set_max_entries(enum_id, 3)
    out2 = store.create_enumeration(
        OWNER, "https://pan.baidu.com/s/1TestShareId22")
    set_max_entries(out2["id"], 3)
    result = store.run_enumeration(out2["id"], fake)
    assert result["state"] == "failed"
    assert result["error_code"] == "incomplete_limit"
    view = store.get_enumeration(out2["id"], OWNER)
    assert view["complete"] is False
    assert view["incomplete_reason"] == "max_entries"
    # 超限（incomplete）枚举禁止导入
    body, status = http.create_import(
        IDENT, {"enumeration_id": out2["id"],
                "candidate_ids": ["bcand_x"]})
    assert status == 409 and body["code"] == "enumeration_not_ready"
    # 原枚举不受影响
    assert store.get_enumeration(enum_id, OWNER)["complete"] is True


# --------------------------------------------------------------------------- #
# B04：混合格式候选 / 有效期 / 权限
# --------------------------------------------------------------------------- #

def test_b04_mixed_format_selectability(monkeypatch):
    _, enum_id, by_path = _make_ready(monkeypatch)
    # KFBF：当前产品已支持 → convert-required 可选
    kfbf = by_path["A1/panel.kfbf"]
    assert kfbf["selectable"] is True
    assert kfbf["capability"] == "convert-required"
    assert kfbf["format"] == ".kfbf"
    # KFB 同为 convert-required 可选；native 单文件可选
    assert by_path["B1/big.kfb"]["selectable"] is True
    assert by_path["A1/sample.svs"]["capability"] == "native-single-file"
    # OME 最长后缀匹配
    assert by_path["A1/scan.ome.tif"]["format"] == ".ome.tif"
    assert by_path["A1/scan2.ome.tiff"]["format"] == ".ome.tiff"
    # channel.json / 未知格式不可选
    cj = by_path["A1/panel_kfbf/channel.json"]
    assert cj["selectable"] is False and cj["reason_code"] == "not_a_slide"
    assert by_path["B1/notes.txt"]["reason_code"] == "unsupported_format"
    # MRXS：无伴随目录 → bundle_incomplete；有伴随目录但百度侧无法保证
    # 完整目录包 → baidu_bundle_unsupported（均不可选）
    assert by_path["A1/scan.mrxs"]["reason_code"] == "bundle_incomplete"
    deep = by_path["B1/deep.mrxs"]
    assert deep["reason_code"] == "baidu_bundle_unsupported"
    assert deep["selectable"] is False
    # size_bytes 十进制字符串
    assert by_path["A1/sample.svs"]["size_bytes"] == "1000"
    assert isinstance(by_path["A1/sample.svs"]["size_bytes"], str)


def test_b04_empty_selection_400(monkeypatch):
    _, enum_id, _ = _make_ready(monkeypatch)
    body, status = http.create_import(
        IDENT, {"enumeration_id": enum_id, "candidate_ids": []})
    assert status == 400 and body["code"] == "empty_selection"
    with pytest.raises(store.ValidationError) as ei:
        store.create_import(OWNER, enum_id, [])
    assert ei.value.code == "empty_selection"


def test_b04_expired_enumeration_409(monkeypatch):
    _, enum_id, by_path = _make_ready(monkeypatch)
    set_expires_at(enum_id, "- 1 hour")
    # 惰性过期：读取即 expired
    view = store.get_enumeration(enum_id, OWNER)
    assert view["state"] == "expired"
    with pytest.raises(store.ConflictError) as ei:
        store.create_import(OWNER, enum_id, [by_path["A1/sample.svs"]["id"]])
    assert ei.value.code == "enumeration_expired"
    body, status = http.create_import(
        IDENT, {"enumeration_id": enum_id,
                "candidate_ids": [by_path["A1/sample.svs"]["id"]]})
    assert status == 409 and body["code"] == "enumeration_expired"


def test_b04_other_owner_404(monkeypatch):
    _, enum_id, _ = _make_ready(monkeypatch)
    with pytest.raises(store.NotFoundError):
        store.get_enumeration(enum_id, OTHER)
    body, status = http.get_enumeration(
        {"role": "user", "user_id": OTHER}, enum_id)
    assert status == 404
    body2, status2 = http.list_candidates(
        {"role": "user", "user_id": OTHER}, enum_id)
    assert status2 == 404


def test_b04_extraction_code_roundtrip(monkeypatch):
    # 带提取码分享：枚举正常（密文落库，错误提取码在适配器层失败）
    fake, enum_id, _ = _make_ready(monkeypatch,
                                   extraction_code="ab12")
    assert store.get_enumeration(enum_id, OWNER)["state"] == "ready"
    # 行内只有密文，无明文提取码/URL
    import psycopg
    import os
    conn = psycopg.connect(os.environ["DATABASE_URL"])
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT share_url_enc, extraction_enc "
                "FROM baidu_enumerations WHERE id=%s", (enum_id,))
            row = cur.fetchone()
    finally:
        conn.close()
    assert "1TestShareId99" not in row[0]
    assert "ab12" not in (row[1] or "")


# --------------------------------------------------------------------------- #
# B05：创建导入的幂等与配额
# --------------------------------------------------------------------------- #

def test_b05_same_key_same_digest_same_batch(monkeypatch):
    _, enum_id, by_path = _make_ready(monkeypatch)
    ids = [by_path["A1/sample.svs"]["id"], by_path["A1/panel.kfbf"]["id"]]
    b1 = store.create_import(OWNER, enum_id, ids, idempotency_key="idem-1")
    b2 = store.create_import(OWNER, enum_id, list(reversed(ids)),
                             idempotency_key="idem-1")  # 顺序无关同 digest
    assert b1["id"] == b2["id"] and b2["state"] == "queued"
    import psycopg
    import os
    conn = psycopg.connect(os.environ["DATABASE_URL"])
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) FROM baidu_import_batches "
                "WHERE owner_user_id=%s AND idempotency_key='idem-1'",
                (OWNER,))
            n_batches = cur.fetchone()[0]
            cur.execute(
                "SELECT COUNT(*) FROM baidu_import_items WHERE batch_id=%s",
                (b1["id"],))
            n_items = cur.fetchone()[0]
    finally:
        conn.close()
    assert (n_batches, n_items) == (1, 2)


def test_b05_same_key_different_selection_409(monkeypatch):
    _, enum_id, by_path = _make_ready(monkeypatch)
    a = by_path["A1/sample.svs"]["id"]
    b = by_path["A1/panel.kfbf"]["id"]
    store.create_import(OWNER, enum_id, [a], idempotency_key="idem-2")
    with pytest.raises(store.ConflictError) as ei:
        store.create_import(OWNER, enum_id, [a, b], idempotency_key="idem-2")
    assert ei.value.code == "idempotency_conflict"


def test_b05_concurrent_same_key_single_batch(monkeypatch):
    _, enum_id, by_path = _make_ready(monkeypatch)
    ids = [by_path["A1/sample.svs"]["id"], by_path["B1/big.kfb"]["id"]]
    barrier = threading.Barrier(2, timeout=10)
    results, errors = [], []

    def worker():
        barrier.wait()
        try:
            results.append(store.create_import(
                OWNER, enum_id, ids, idempotency_key="idem-race"))
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    t1 = threading.Thread(target=worker)
    t2 = threading.Thread(target=worker)
    t1.start(); t2.start(); t1.join(); t2.join()
    # 并发同键同载荷：要么双双成功同批，要么一个成功一个 409 冲突——
    # 但必须恰好一个批次
    ids_out = {r["id"] for r in results}
    assert len(ids_out) == 1, (results, errors)
    for exc in errors:
        assert isinstance(exc, store.ConflictError) or \
            isinstance(exc, store.BaiduImportError)
    import psycopg
    import os
    conn = psycopg.connect(os.environ["DATABASE_URL"])
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) FROM baidu_import_batches "
                "WHERE owner_user_id=%s AND idempotency_key='idem-race'",
                (OWNER,))
            assert cur.fetchone()[0] == 1
            cur.execute(
                "SELECT COUNT(*) FROM baidu_import_items WHERE batch_id=%s",
                (next(iter(ids_out)),))
            assert cur.fetchone()[0] == 2
    finally:
        conn.close()


def test_b05_quota_insufficient_zero_side_effects(monkeypatch):
    fake, enum_id, by_path = _make_ready(monkeypatch)
    ids = [by_path["A1/sample.svs"]["id"]]  # 1000 bytes
    calls = []

    def hook(user_id, nbytes):
        calls.append((user_id, nbytes))
        raise upload_guard.QuotaExceeded("配额不足")

    with pytest.raises(store.QuotaError):
        store.create_import(OWNER, enum_id, ids, idempotency_key="q1",
                            quota_hook=hook)
    assert calls == [(OWNER, 1000)]
    # 零外部副作用：无批次行、fake 无转存/下载/删除
    import psycopg
    import os
    conn = psycopg.connect(os.environ["DATABASE_URL"])
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM baidu_import_batches")
            assert cur.fetchone()[0] == 0
    finally:
        conn.close()
    c = fake.counters()
    assert (c["transfer"], c["download"], c["delete"]) == (0, 0, 0)


def test_b05_quota_insufficient_http_429(monkeypatch):
    # 真实 upload_guard 预占路径：upload_user_quotas 有 users 外键，
    # 用真实建号用户走完整 http 装配
    import user_store
    u = user_store.create_user("baidu-quota@x.com", "pass1234pass1234")
    quota_ident = {"role": "user", "user_id": u["user_id"]}
    fake = install_fake(monkeypatch)
    out = store.create_enumeration(
        u["user_id"], "https://pan.baidu.com/s/1TestShareId99")
    store.run_enumeration(out["id"], fake)
    cands = store.list_candidates(out["id"], u["user_id"], limit=100)
    svs = [c for c in cands["items"] if c["name"] == "sample.svs"][0]
    monkeypatch.setattr(upload_guard, "UPLOAD_USER_QUOTA_BYTES", 100)
    body, status = http.create_import(
        quota_ident, {"enumeration_id": out["id"], "candidate_ids": [svs["id"]]},
        idempotency_key="q2")
    assert status == 429 and body["code"] == "quota_exceeded"
    c = fake.counters()
    assert (c["transfer"], c["download"], c["delete"]) == (0, 0, 0)


def test_b05_quota_reservation_recorded(monkeypatch):
    _, enum_id, by_path = _make_ready(monkeypatch)
    rid_holder = {}

    def hook(user_id, nbytes):
        rid_holder["rid"] = "upr_test_%d" % nbytes
        return rid_holder["rid"]

    batch = store.create_import(
        OWNER, enum_id, [by_path["A1/sample.svs"]["id"],
                         by_path["B1/big.kfb"]["id"]],
        idempotency_key="q3", quota_hook=hook)
    import psycopg
    import os
    conn = psycopg.connect(os.environ["DATABASE_URL"])
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT quota_reservation_id, total_bytes "
                "FROM baidu_import_batches WHERE id=%s", (batch["id"],))
            rid, total = cur.fetchone()
    finally:
        conn.close()
    assert rid == rid_holder["rid"]
    assert int(total) == 1700
    assert batch["total_bytes"] == "1700"


# --------------------------------------------------------------------------- #
# B11：列表详情与秘密隔离
# --------------------------------------------------------------------------- #

def test_b11_no_secrets_in_public_views(monkeypatch, tmp_path):
    fake, enum_id, by_path = _make_ready(
        monkeypatch, extraction_code="ab12")
    ids = [by_path["A1/sample.svs"]["id"], by_path["A1/panel.kfbf"]["id"]]
    batch = store.create_import(OWNER, enum_id, ids, idempotency_key="s1")
    # 推进到终态（含 staging 路径/staging 文件生成）
    view = store.run_batch(batch["id"], fake, staging_root=tmp_path)
    blob = json.dumps(view, ensure_ascii=False, default=str)
    listing = json.dumps(store.list_imports(OWNER), ensure_ascii=False,
                         default=str)
    detail = json.dumps(store.get_import(batch["id"], OWNER),
                        ensure_ascii=False, default=str)
    for text in (blob, listing, detail):
        assert "ab12" not in text           # 提取码
        assert "1TestShareId99" not in text  # 分享 URL/ID
        assert "staging_path" not in text    # 内部暂存路径不出现
        assert "ingest_token" not in text
        assert "share_url" not in text
    # 条目级也没有内部字段
    for item in view["items"]:
        allowed = {
            "id", "fs_id", "name", "relative_path", "stage",
            "error_code", "cleanup_state", "source_size", "attempt",
            "conversion_job_id", "slide_name", "project_associate_state",
        }
        assert set(item) <= allowed
        assert "staging_path" not in item
        assert "ingest_token" not in item


def test_b11_owner_isolation_lists(monkeypatch):
    fake, enum_id, by_path = _make_ready(monkeypatch)
    store.create_import(OWNER, enum_id,
                        [by_path["A1/sample.svs"]["id"]],
                        idempotency_key="o1")
    mine = store.list_imports(OWNER)
    theirs = store.list_imports(OTHER)
    assert len(mine["items"]) == 1
    assert theirs["items"] == []
    batch_id = mine["items"][0]["id"]
    assert store.get_import(batch_id, OWNER)["id"] == batch_id
    with pytest.raises(store.NotFoundError):
        store.get_import(batch_id, OTHER)
    with pytest.raises(store.NotFoundError):
        store.request_cancel(batch_id, OTHER)
    body, status = http.get_import({"role": "user", "user_id": OTHER},
                                   batch_id)
    assert status == 404


def test_b11_pagination_cursor(monkeypatch):
    _, enum_id, by_path = _make_ready(monkeypatch)
    page1 = store.list_candidates(enum_id, OWNER, limit=4)
    assert len(page1["items"]) == 4 and page1["next_cursor"]
    page2 = store.list_candidates(enum_id, OWNER, cursor=page1["next_cursor"],
                                  limit=4)
    paths1 = {c["relative_path"] for c in page1["items"]}
    paths2 = {c["relative_path"] for c in page2["items"]}
    assert not (paths1 & paths2)
    with pytest.raises(store.ValidationError):
        store.list_candidates(enum_id, OWNER, cursor="garbage!!")
