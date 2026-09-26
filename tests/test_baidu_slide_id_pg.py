# -*- coding: utf-8 -*-
"""P4-c 百度远程导入 slide ID 链路（合同 §4 / 计划 §8 矩阵）。

覆盖：
1. 同名导入两次（不同批次）→ 不同 slide_id / 不同 objects 目录；
2. 恢复不认领他人文件（盘上恰好同名也不认领——按名对账认领已拆除）；
3. item→slide_id 绑定持久：分配后崩溃 → 恢复复用同一 slide_id（不重分）；
   重放同 token 同 ID；
4. 项目关联按 slide_id（project_slides.slide_id，无名快照）；
5. ready 前不可读（staging 拒读；发布后 owner 可读、他人不可读）。
"""
import os
from pathlib import Path

import psycopg
import pytest

import baidu_import_store as store
import slide_store
from _baidu_helpers import expire_batch_lease, make_ready_enumeration
from _tiff_fixtures import make_tiff_bytes

OWNER = "u-baidu-sid"
OTHER = "u-baidu-sid-other"


class Crash(RuntimeError):
    """模拟进程崩溃（凭证已落库，后续收口未执行）。"""


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    monkeypatch.setenv("BAIDU_SHARE_SECRET_KEY",
                       "test-baidu-share-secret-key-2026-09-14")
    up = tmp_path / "uploads"
    up.mkdir()
    monkeypatch.setenv("UPLOAD_DIR", str(up))
    yield


def _sql(fn):
    conn = psycopg.connect(os.environ["DATABASE_URL"])
    try:
        with conn.cursor() as cur:
            return fn(cur)
    finally:
        conn.close()


def _item(batch_id):
    def q(cur):
        cur.execute(
            "SELECT id, name, stage, error_code, ingest_token, slide_id, "
            "slide_name, project_associate_state FROM baidu_import_items "
            "WHERE batch_id=%s", (batch_id,))
        cols = [d.name for d in cur.description]
        return dict(zip(cols, cur.fetchone()))
    return _sql(q)


def _native_batch(monkeypatch, tmp_path, name, content, *, key,
                  share="https://pan.baidu.com/s/1SidShareBase01",
                  project_id=None):
    entries = [{"path": "/%s" % name, "size": len(content),
                "content": content}]
    fake, enum_id, by_path = make_ready_enumeration(
        monkeypatch, owner=OWNER, entries=entries, share_text=share)
    cand_id = by_path[name]["id"]
    batch = store.create_import(
        OWNER, enum_id, [cand_id], target_project_id=project_id,
        idempotency_key=key)
    return fake, batch, enum_id, cand_id


def _up():
    return Path(os.environ["UPLOAD_DIR"])


# --------------------------------------------------------------------------- #
# 1. 同名导入两次 → 不同 slide_id
# --------------------------------------------------------------------------- #

def test_same_name_two_imports_distinct_slide_ids(monkeypatch, tmp_path):
    tif = make_tiff_bytes()
    fake1, b1, _, _ = _native_batch(monkeypatch, tmp_path, "dup.tif", tif,
                                    key="sid-1")
    fake2, b2, _, _ = _native_batch(
        monkeypatch, tmp_path, "dup.tif", tif, key="sid-2",
        share="https://pan.baidu.com/s/1SidShareBase02")
    v1 = store.run_batch(b1["id"], fake1, staging_root=tmp_path)
    v2 = store.run_batch(b2["id"], fake2, staging_root=tmp_path)
    assert v1["state"] == "succeeded", v1
    assert v2["state"] == "succeeded", v2
    i1, i2 = _item(b1["id"]), _item(b2["id"])
    assert i1["stage"] == "ready" and i2["stage"] == "ready"
    sid1, sid2 = i1["slide_id"], i2["slide_id"]
    assert sid1 and sid2 and sid1 != sid2  # 同名导入是独立资产
    # 各自独立 objects 目录（互不覆盖、互不引用）
    assert (_up() / "objects" / sid1 / "data.tif").is_file()
    assert (_up() / "objects" / sid2 / "data.tif").is_file()
    # token 形态 item:<item_id>（不再承诺 slide:<name>），两条目各异
    assert i1["ingest_token"] == "item:" + i1["id"]
    assert i2["ingest_token"] == "item:" + i2["id"]
    assert i1["ingest_token"] != i2["ingest_token"]
    # slide_name 保留展示快照
    assert i1["slide_name"] == "dup.tif" and i2["slide_name"] == "dup.tif"


# --------------------------------------------------------------------------- #
# 2. 恢复不认领他人文件（盘上恰好同名也不认领）
# --------------------------------------------------------------------------- #

def test_recovery_never_claims_foreign_same_name_file(monkeypatch, tmp_path):
    import baidu_ingest
    import share_store
    tif = make_tiff_bytes()
    victim_bytes = make_tiff_bytes(h=24, w=24)
    # 他人既有的同名 legacy 文件（内容不同）
    (_up() / "grab.tif").write_bytes(victim_bytes)
    share_store.set_slide_meta("grab.tif", owner_user_id=OTHER,
                               requester_role="user")

    def q_victim(cur):
        cur.execute("SELECT slide_id FROM slides WHERE legacy_filename=%s",
                    ("grab.tif",))
        return cur.fetchone()[0]
    victim_sid = _sql(q_victim)
    assert victim_sid

    fake, batch, _, _ = _native_batch(monkeypatch, tmp_path, "grab.tif", tif,
                                      key="sid-grab")
    real_ingest = baidu_ingest.ingest_staging

    def crash_after_publish(**kw):
        real_ingest(**kw)  # 本批资产已发布（objects/<本批 sid>/ + ready）
        raise Crash("token 未落库即进程死亡")

    monkeypatch.setattr(baidu_ingest, "ingest_staging", crash_after_publish)
    with pytest.raises(Crash):
        store.run_batch(batch["id"], fake, staging_root=tmp_path)
    row = _item(batch["id"])
    assert row["stage"] == "ingesting" and not row["ingest_token"]
    my_sid = row["slide_id"]
    assert my_sid and my_sid != victim_sid

    expire_batch_lease(batch["id"])
    view = store.run_batch(batch["id"], fake, staging_root=tmp_path)
    assert view["state"] == "succeeded"
    row = _item(batch["id"])
    # 恢复按 item.slide_id 对账收口：同 ID、token=item 形态
    assert row["slide_id"] == my_sid
    assert row["ingest_token"] == "item:" + row["id"]
    # 他人文件与行原样（不被认领/不改绑/不覆盖）
    assert (_up() / "grab.tif").read_bytes() == victim_bytes

    def q_after(cur):
        cur.execute("SELECT owner_user_id, storage_layout FROM slides "
                    "WHERE slide_id=%s", (victim_sid,))
        return cur.fetchone()
    assert _sql(q_after) == (OTHER, "legacy")
    # 本批资产在独立目录且归属本批 owner
    desc = slide_store.resolve_slide_id(my_sid)
    assert desc.asset_state == "ready"
    assert desc.owner_user_id == OWNER
    assert desc.storage_layout == "id_bundle"


# --------------------------------------------------------------------------- #
# 3/5. item→slide_id 绑定持久 + ready 前不可读
# --------------------------------------------------------------------------- #

def test_allocate_crash_recovery_reuses_slide_id_and_gates_read(
        monkeypatch, tmp_path):
    import baidu_ingest
    tif = make_tiff_bytes()
    fake, batch, enum_id, cand_id = _native_batch(
        monkeypatch, tmp_path, "gate.tif", tif, key="sid-gate")
    real_ingest = baidu_ingest.ingest_staging
    crashed = {"once": False}

    def crash_before_publish(**kw):
        # 预分配已落库（item.slide_id），发布尚未开始 → 崩溃（仅首次）
        crashed["once"] = True
        raise Crash("allocate 后、发布前进程死亡")

    monkeypatch.setattr(baidu_ingest, "ingest_staging", crash_before_publish)
    with pytest.raises(Crash):
        store.run_batch(batch["id"], fake, staging_root=tmp_path)
    monkeypatch.setattr(baidu_ingest, "ingest_staging", real_ingest)
    row = _item(batch["id"])
    assert row["stage"] == "ingesting" and not row["ingest_token"]
    sid = row["slide_id"]
    assert sid
    # ready 前不可读：staging 状态拒绝一切读取（唯一可见性开关）
    assert not slide_store.authorize_read(sid, actor_user_id=OWNER)
    assert not slide_store.authorize_read(sid, actor_user_id=OTHER)
    assert not slide_store.authorize_read(sid, actor_role="owner")
    # 尚未发布：无 objects 目录、不写 UPLOAD_DIR 根
    assert not (_up() / "objects" / sid).exists()

    expire_batch_lease(batch["id"])
    view = store.run_batch(batch["id"], fake, staging_root=tmp_path)
    assert view["state"] == "succeeded"
    row2 = _item(batch["id"])
    # 恢复复用同一 slide_id（预分配持久绑定，绝不重新分配）
    assert row2["slide_id"] == sid
    assert row2["ingest_token"] == "item:" + row2["id"]

    def q_rows(cur):
        cur.execute("SELECT COUNT(*) FROM slides WHERE slide_id=%s", (sid,))
        return int(cur.fetchone()[0])
    assert _sql(q_rows) == 1  # 无重复分配
    # 发布后：owner 可读、他人不可读（可见性开关 + 归属门禁）
    assert slide_store.authorize_read(sid, actor_user_id=OWNER)
    assert not slide_store.authorize_read(sid, actor_user_id=OTHER)
    assert (_up() / "objects" / sid / "data.tif").is_file()

    # 重放（响应丢失：同 idempotency_key 重建导入）→ 原批次原条目——
    # 同 token 同 slide_id，无重复产物、无重复分配
    replay = store.create_import(OWNER, enum_id, [cand_id],
                                 idempotency_key="sid-gate")
    assert replay["id"] == batch["id"]
    row3 = _item(batch["id"])
    assert row3["ingest_token"] == row2["ingest_token"]
    assert row3["slide_id"] == sid
    assert len(list((_up() / "objects").iterdir())) == 1


# --------------------------------------------------------------------------- #
# 4. 项目关联按 slide_id
# --------------------------------------------------------------------------- #

def test_project_association_by_slide_id(monkeypatch, tmp_path):
    import share_store
    tif = make_tiff_bytes()
    proj = share_store.create_project(
        "ID 关联", owner_user_id=OWNER, requester_role="user")
    fake, batch, _, _ = _native_batch(monkeypatch, tmp_path, "proj.tif", tif,
                                      key="sid-proj", project_id=proj["pid"])
    view = store.run_batch(batch["id"], fake, staging_root=tmp_path)
    assert view["state"] == "succeeded", view
    row = _item(batch["id"])
    assert row["project_associate_state"] == "succeeded"
    sid = row["slide_id"]

    def q_assoc(cur):
        cur.execute(
            "SELECT slide, slide_id FROM project_slides "
            "WHERE project_id=%s", (proj["pid"],))
        return cur.fetchall()
    rows = _sql(q_assoc)
    # 关联按 ID（id_bundle 资产无名快照——不因同名关联其它资产）
    assert rows == [("", sid)]
    got = share_store.get_project(proj["pid"])
    assert sid in (got["slide_ids"] or [])
    assert [s for s in got["slides"] if s] == []
