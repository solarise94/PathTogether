"""R1 跟进：用户删除的切片不再出现在项目的活动视图里（列表/计数/选择一致），
历史成员行与切片墓碑保留，权限不放宽。

生产复现（dogfood 2026-10-02）：同一项目里两张同原始文件名的切片，经项目行
按钮删除其一（DELETE /api/slides/<id> 成功）后刷新，被删的那张仍在项目列表
与计数里，显示「读取失败/未找到」，点开 403「无权访问」；另一张正常。

运行：python3 -m pytest tests/test_project_deleted_slides_pg.py -q
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import _bootstrap  # noqa: E402,F401  # session 目录+openslide stub（conftest 先行）
UPLOAD_DIR = _bootstrap.UPLOAD_DIR
import psycopg  # noqa: E402
import pytest  # noqa: E402

import app as app_mod  # noqa: E402
import upload_guard  # noqa: E402
import user_store  # noqa: E402
from _pt_helpers import (clear_upload_dir, csrf_client, isolate_app,  # noqa: E402
                         publish_test_slide)
from _tiff_fixtures import make_tiff_bytes  # noqa: E402

PG_URI = os.environ["DATABASE_URL"]
TIFF = make_tiff_bytes(64, 96)
SAME_NAME = "same-name.tif"


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    isolate_app(monkeypatch, tmp_path, UPLOAD_DIR)
    monkeypatch.setattr(upload_guard, "UPLOAD_RESERVED_FREE_BYTES", 0)
    clear_upload_dir(UPLOAD_DIR)
    yield


def _client():
    app_mod.app.config["TESTING"] = True
    app_mod.AUTH_ENABLED = True
    return csrf_client(app_mod.app.test_client())


def _user_session(client, login):
    u = user_store.create_user(login, "pass1234pass1234", role="user")
    with client.session_transaction() as sess:
        sess["auth_user"] = True
        sess["user_id"] = u["user_id"]
        sess["role"] = "user"
        sess["auth_version"] = (user_store.get_user(u["user_id"]) or {}).get(
            "auth_version", 1)
    return u["user_id"]


def _rows(sql, params=()):
    with psycopg.connect(PG_URI, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            return cur.fetchall()


def _setup():
    c = _client()
    uid = _user_session(c, "proj-del@x.com")
    upload_guard.get_quota_row(uid)
    keep = publish_test_slide(SAME_NAME, TIFF, owner_user_id=uid, upload_dir=UPLOAD_DIR)
    gone = publish_test_slide(SAME_NAME, TIFF, owner_user_id=uid, upload_dir=UPLOAD_DIR)
    assert keep != gone
    r = c.post("/api/project/create", json={"name": "P", "slide_ids": [keep, gone]})
    assert r.status_code == 200, r.get_data(as_text=True)
    pid = r.get_json()["pid"]
    return c, uid, pid, keep, gone


def _listed(c, pid):
    return next(p for p in c.get("/api/projects").get_json() if p["pid"] == pid)


def test_deleted_slide_leaves_active_project_view_but_history_stays():
    c, _uid, pid, keep, gone = _setup()
    before = _listed(c, pid)
    assert before["slide_count"] == 2
    assert sorted(before["slide_ids"]) == sorted([keep, gone])

    assert c.delete("/api/slides/%s" % gone).status_code == 200

    # 列表、计数、行身份一致：被删的那张不在活动视图里
    after = _listed(c, pid)
    assert after["slide_count"] == 1
    assert after["slide_ids"] == [keep]
    assert [r["slide_id"] for r in after["slide_refs"]] == [keep]
    assert len(after["slides"]) == 1
    detail = c.get("/api/project/%s" % pid).get_json()
    assert detail["project"]["slide_ids"] == [keep]
    assert [sa["slide_id"] for sa in detail["slide_annotations"]] == [keep]

    # 另一张同名切片照常可读
    assert c.get("/api/slides/%s/info" % keep).status_code == 200
    # 被删的那张不被复活：仍不可读、墓碑保留
    assert c.get("/api/slides/%s/info" % gone).status_code in (403, 404)
    state = _rows("SELECT asset_state FROM slides WHERE slide_id=%s", (gone,))
    assert state == [("deleted",)]
    # 历史成员行保留（不物理抹掉项目关系）
    members = _rows("SELECT slide_id FROM project_slides WHERE project_id=%s "
                    "ORDER BY position", (pid,))
    assert sorted(m[0] for m in members) == sorted([keep, gone])


def test_full_list_update_keeps_hidden_history_rows():
    """PATCH 整表替换只作用于活动视图：客户端看不到的已删除成员行不被抹掉。"""
    c, _uid, pid, keep, gone = _setup()
    assert c.delete("/api/slides/%s" % gone).status_code == 200
    r = c.patch("/api/project/%s" % pid, json={"name": "P2", "slide_ids": [keep]})
    assert r.status_code == 200, r.get_data(as_text=True)
    assert r.get_json()["slide_ids"] == [keep]
    members = _rows("SELECT slide_id FROM project_slides WHERE project_id=%s "
                    "ORDER BY position", (pid,))
    assert [m[0] for m in members] == [keep, gone]
    assert _listed(c, pid)["slide_count"] == 1


def test_failed_asset_is_not_disguised_as_deleted():
    """只有用户删除（deleting/deleted）被隐藏；failed 等不可读资产照常列出。"""
    c, _uid, pid, keep, gone = _setup()
    with psycopg.connect(PG_URI, autocommit=True) as conn:
        conn.execute("UPDATE slides SET asset_state='failed' WHERE slide_id=%s", (gone,))
    after = _listed(c, pid)
    assert after["slide_count"] == 2
    assert sorted(after["slide_ids"]) == sorted([keep, gone])


def test_other_user_gains_nothing():
    c, _uid, pid, keep, gone = _setup()
    assert c.delete("/api/slides/%s" % gone).status_code == 200
    other = _client()
    _user_session(other, "proj-del-other@x.com")
    assert other.get("/api/project/%s" % pid).status_code == 403
    assert all(p["pid"] != pid for p in other.get("/api/projects").get_json())
    for sid in (keep, gone):
        assert other.get("/api/slides/%s/info" % sid).status_code in (403, 404)
