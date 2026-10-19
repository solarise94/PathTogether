# -*- coding: utf-8 -*-
"""hotfix 2026-10-19 回归：rois.slide_id 离散列 × JSON data 分裂态的读口径。

生产缺陷（reviewer 实证，read-only repro：run-review-dogfood-QAdF4a/
repro-column-json-split.py）：存量行「``rois.slide_id`` 列已有 ID、JSON
data 缺 ``slide_id`` 键」。annotations_by_slide 曾按 JSON 分组——同片行被
拆进 ID 组（JSON 有键的新行）与名组（JSON 缺键的旧行），app.py
/api/annotations 的「ID 组 or 名组」二选一取组导致：新增一条标注隐藏全部
旧标注，删除它旧标注又回来（生产 slide sld_iu8fv7Y8sQm2 11 行全中）。

修复（只读侧，无数据改写/迁移）：
  - annotations_by_slide 分组键列优先：``row["slide_id"] or data.slide_id
    or data.slide``；items.slide_id 缺键时以列回填；
  - list_rois / get_roi / get_roi_by_annotation_id 同口径回填（调用方按
    JSON slide_id 过滤/归属比较不再把分裂态行当 unresolved）；
  - app.py /api/share/create 的标注授权收集改 ID 组 ∪ 名组双取。

本文件移植 reviewer repro（期望语义不变：before/with_new/after_delete_new
的 label 集、列有 ID 而 JSON 缺键的行数镜像），并覆盖其余修复路径。

运行：.venv/bin/python -m pytest tests/test_anno_slide_column_split.py -q
（conftest 起内嵌 PG；每用例前 TRUNCATE 业务表）。
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import psycopg  # noqa: E402
import pytest  # noqa: E402

import _bootstrap  # noqa: E402,F401  # session 目录 + openslide stub
DATA_DIR = _bootstrap.SHARE_DATA_DIR
UPLOAD_DIR = _bootstrap.UPLOAD_DIR
import app as app_mod  # noqa: E402
import share_store  # noqa: E402
import user_store  # noqa: E402
from _pt_helpers import csrf_client, isolate_app, register_slide_row  # noqa: E402

NAME = "split-legacy.svs"
LOGIN = "split-owner@x.com"
PASSWORD = "splitpass123456"


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    isolate_app(monkeypatch, DATA_DIR, UPLOAD_DIR, clear_stores=True)
    yield


# --------------------------------------------------------------------------- #
# 辅助
# --------------------------------------------------------------------------- #
def _sql(query, params=(), fetch=False):
    conn = psycopg.connect(os.environ["DATABASE_URL"])
    try:
        with conn.cursor() as cur:
            cur.execute(query, params)
            out = cur.fetchall() if fetch else None
        conn.commit()
        return out
    finally:
        conn.close()


def _setup_slide_with_split_row(label="old-label"):
    """owner + 资产 + 「列有 ID、JSON 缺键」的存量行（生产分裂态）。"""
    u = user_store.create_user(LOGIN, PASSWORD, role="owner")
    share_store.set_owner_user_id(u["user_id"])
    (Path(UPLOAD_DIR) / NAME).write_bytes(b"split-fixture")
    sid = register_slide_row(NAME, upload_dir=UPLOAD_DIR)
    share_store.set_slide_meta(NAME, owner_user_id=u["user_id"])
    old = share_store.add_roi(share_store.ADMIN_TOKEN, NAME, label,
                              type="arrow", x1=1, y1=1, x2=10, y2=10,
                              slide_id=sid, source="human",
                              owner_user_id=u["user_id"])
    # 与生产一致的分裂态：typed 列有 ID，JSON 文档缺键
    _sql("UPDATE rois SET data=data-'slide_id' WHERE annotation_id=%s",
         (old["annotation_id"],))
    return u, sid, old


def _add(user, sid, label, x1, y1=None):
    y1 = x1 if y1 is None else y1
    return share_store.add_roi(share_store.ADMIN_TOKEN, NAME, label,
                               type="arrow", x1=x1, y1=y1, x2=x1 + 9,
                               y2=y1 + 9, slide_id=sid, source="human",
                               owner_user_id=user["user_id"])


def _owner_client():
    app_mod.app.config["TESTING"] = True
    app_mod.AUTH_ENABLED = True
    client = csrf_client(app_mod.app.test_client())
    login = client.post("/login", data={"username": LOGIN,
                                        "password": PASSWORD})
    assert login.status_code in (200, 302)
    return client


# =========================================================================== #
# 1. 移植 reviewer repro（/api/annotations?slide_id= 期望语义不变）
# =========================================================================== #
def test_api_annotations_column_json_split_repro():
    u, sid, old = _setup_slide_with_split_row()
    client = _owner_client()

    def read():
        r = client.get("/api/annotations", query_string={"slide_id": sid})
        j = r.get_json()
        return {"status": r.status_code,
                "labels": sorted(g["label"] for g in j.get("annotations", []))}

    before = read()
    new = _add(u, sid, "new-label", 20)
    with_new = read()
    mirrors = _sql(
        "SELECT count(*), count(*) FILTER(WHERE slide_id IS NOT NULL), "
        "count(*) FILTER(WHERE data->>'slide_id' IS NULL) FROM rois "
        "WHERE NOT deleted", fetch=True)[0]
    _sql("UPDATE rois SET deleted=true, "
         "data=jsonb_set(data,'{deleted}','true'::jsonb) "
         "WHERE annotation_id=%s", (new["annotation_id"],))
    after_delete = read()

    # reviewer repro 原期望（语义不变）
    assert before == {"status": 200, "labels": ["old-label"]}
    assert after_delete == {"status": 200, "labels": ["old-label"]}
    assert with_new == {"status": 200,
                        "labels": ["new-label", "old-label"]}, \
        "Adding a new marker must not hide an existing readable marker"
    # 分裂态镜像：2 行全在列；JSON 恰缺旧行的键
    assert tuple(mirrors) == (2, 2, 1)

    # 修复附加口径：分裂态行的 items.slide_id 以列回填（与分组键一致）
    r = client.get("/api/annotations", query_string={"slide_id": sid})
    grps = r.get_json()["annotations"]
    assert sorted(g["label"] for g in grps) == ["old-label"]
    items = [it for g in grps for it in g.get("items", [])]
    assert all(it["slide_id"] == sid for it in items)


# =========================================================================== #
# 2. store 层分组：annotations_by_slide / annotations_by_project
# =========================================================================== #
def test_annotations_by_slide_groups_split_row_by_column():
    u, sid, _old = _setup_slide_with_split_row()
    _add(u, sid, "new-label", 20)
    by = share_store.annotations_by_slide(subject=None)
    # 列优先分组：同片两行并进 ID 组，不再拆名组
    assert set(by.keys()) == {sid}
    assert sorted(grp["label"] for grp in by[sid]) == \
        ["new-label", "old-label"]
    old_grp = next(grp for grp in by[sid] if grp["label"] == "old-label")
    old_item = old_grp["items"][0]
    assert old_item["slide_id"] == sid  # 列回填
    assert old_item["slide"] == NAME    # 名快照保留


def test_annotations_by_slide_null_column_row_falls_back_to_name():
    u, sid, old = _setup_slide_with_split_row()
    # 真 NULL 列历史行 → 名组回退（legacy 口径不变）
    _sql("UPDATE rois SET slide_id=NULL WHERE annotation_id=%s",
         (old["annotation_id"],))
    by = share_store.annotations_by_slide(subject=None)
    assert set(by.keys()) == {NAME}
    item = by[NAME][0]["items"][0]
    assert item["slide_id"] is None
    assert item["slide"] == NAME


def test_annotations_by_project_includes_split_rows_under_id():
    u, sid, _old = _setup_slide_with_split_row()
    _add(u, sid, "new-label", 20)
    proj = share_store.create_project("P", slides=[NAME],
                                      owner_user_id=u["user_id"])
    by = share_store.annotations_by_project(proj["pid"])
    assert set(by.keys()) == {sid}
    assert sorted(grp["label"] for grp in by[sid]) == \
        ["new-label", "old-label"]


# =========================================================================== #
# 3. list_rois / get_roi family：slide_id 列回填
# =========================================================================== #
def test_list_rois_fills_slide_id_from_column():
    u, sid, old = _setup_slide_with_split_row()
    tok_rows = share_store.list_rois(share_store.ADMIN_TOKEN)
    assert [r["slide_id"] for r in tok_rows if
            r["annotation_id"] == old["annotation_id"]] == [sid]
    all_rows = share_store.list_rois()
    hit = next(r for r in all_rows
               if r["annotation_id"] == old["annotation_id"])
    assert hit["slide_id"] == sid


def test_get_roi_family_fills_slide_id_from_column():
    u, sid, old = _setup_slide_with_split_row()
    idx = next(i for i, r in enumerate(share_store.list_rois(
        share_store.ADMIN_TOKEN))
        if r["annotation_id"] == old["annotation_id"])
    got = share_store.get_roi(share_store.ADMIN_TOKEN, idx)
    assert got["slide_id"] == sid
    by_id = share_store.get_roi_by_annotation_id(old["annotation_id"])
    assert by_id["slide_id"] == sid
    assert by_id["slide"] == NAME


# =========================================================================== #
# 4. /api/share/rois：按 slide_id 过滤不再丢分裂态行
# =========================================================================== #
def test_api_share_rois_slide_id_filter_includes_split_rows():
    u, sid, _old = _setup_slide_with_split_row()
    _add(u, sid, "new-label", 20)
    client = _owner_client()
    r = client.get("/api/share/rois", query_string={"slide_id": sid})
    assert r.status_code == 200
    labels = sorted(x["label"] for x in r.get_json())
    assert labels == ["new-label", "old-label"]
    # 缺省分支（双键过滤）：分裂态行照旧按名可见，不因修复重复/丢失
    r2 = client.get("/api/share/rois")
    labels2 = sorted(x["label"] for x in r2.get_json())
    assert labels2 == ["new-label", "old-label"]


# =========================================================================== #
# 5. /api/share/create：授权收集 ID 组 ∪ 名组（分裂态行不漏授）
# =========================================================================== #
def test_share_create_grants_split_rows():
    u, sid, old = _setup_slide_with_split_row()
    _add(u, sid, "new-label", 20)
    client = _owner_client()
    r = client.post("/api/share/create", json={
        "slides": [NAME], "expires_hours": 24})
    assert r.status_code == 200, r.get_data(as_text=True)
    assert r.get_json()["annotations_granted"] == 2
    token = r.get_json()["token"]
    # 分享页可见集合带两条（授权行的实证）
    import share_server as share_srv
    sc = share_srv.app.test_client()
    rois = sc.get("/s/%s/api/rois" % token).get_json()
    assert sorted(x["label"] for x in rois) == ["new-label", "old-label"]


# =========================================================================== #
# 6. 项目端点：ID 组取数（分裂态行不丢；计数不重复）
# =========================================================================== #
def test_project_endpoints_include_split_rows():
    u, sid, _old = _setup_slide_with_split_row()
    _add(u, sid, "new-label", 20)
    client = _owner_client()
    r = client.post("/api/project/create",
                    json={"name": "P", "slide_ids": [sid]})
    assert r.status_code == 200, r.get_data(as_text=True)
    pid = r.get_json()["pid"]
    # 列表 roi_count：ID 组 + 名组双键汇总，分裂态迁移后不重复计数
    item = [p for p in client.get("/api/projects").get_json()
            if p["pid"] == pid][0]
    assert item["roi_count"] == 2
    # 详情按 ID 取组：两组标注齐全
    detail = client.get("/api/project/%s" % pid).get_json()
    groups = detail["slide_annotations"][0]["annotations"]
    assert sorted(grp["label"] for grp in groups) == \
        ["new-label", "old-label"]
    items = [it for grp in groups for it in grp.get("items", [])]
    assert all(it["slide_id"] == sid for it in items)


# =========================================================================== #
# 7. ID 原生标注端点：/api/slides/<slide_id>/annotations
# =========================================================================== #
def test_slide_annotations_by_id_endpoint_includes_split_rows():
    u, sid, _old = _setup_slide_with_split_row()
    _add(u, sid, "new-label", 20)
    client = _owner_client()
    r = client.get("/api/slides/%s/annotations" % sid)
    assert r.status_code == 200
    j = r.get_json()
    assert sorted(grp["label"] for grp in j["annotations"]) == \
        ["new-label", "old-label"]
    items = [it for grp in j["annotations"] for it in grp.get("items", [])]
    assert all(it["slide_id"] == sid for it in items)
