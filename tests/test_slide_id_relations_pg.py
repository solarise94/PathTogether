# -*- coding: utf-8 -*-
"""slide ID 化重构 P2 后端关系测试（活动关系切 slide_id + 读取端点 ID 化）。

对齐 docs/slide-id-refactor-p2-contract-20260925.md §1~§4/§7/§8 的后端完成
标准：
  - 同显示名两片分别标注/分享/入项目互不串（rois/share_slides/project_slides
    双写 slide_id；按 ID 查询）；
  - 改名（PATCH /api/slides/<slide_id> display_name）不动 ID/授权/标注归属；
  - 双字段 slide_id/slide 冲突 → 400 slide_ref_conflict；未知 ID → 404
    slide_not_found；
  - slide_id IS NULL 的历史行（unresolved）不出现在新资产的标注/变更流；
  - 删除后旧 share token / 旧 view grant / 旧 run grant 对新资产（含同显示
    名）全部拒绝；
  - 项目内同名不同 ID 并存；DELETE /api/project/<pid>/slides/<slide_id> 精确
    命中；
  - 双写字段一致性（rois.slide 快照 ↔ slide_id、change_log.slide_id、
    run_grants.slide_id、ai_session_principals.slide_id）；
  - alias 停写（R-02）：alias 列不再被写、出参从 display_name 派生；
  - 机器通道（/internal/ai/*、/api/plugin/v1/ by-id 族）双字段 + 按 ID 校验。

运行：.venv/bin/python -m pytest tests/test_slide_id_relations_pg.py -q
（conftest 起内嵌 PG；每用例前 TRUNCATE 业务表）。
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import psycopg  # noqa: E402
import pytest  # noqa: E402

import _bootstrap  # noqa: E402,F401  # session 目录 + openslide stub（conftest 先行）
DATA_DIR = _bootstrap.SHARE_DATA_DIR
UPLOAD_DIR = _bootstrap.UPLOAD_DIR
import app as app_mod  # noqa: E402
import share_server as share_srv  # noqa: E402
import share_store  # noqa: E402
import slide_store  # noqa: E402
import user_store  # noqa: E402
from _pt_helpers import csrf_client, isolate_app, register_slide_row  # noqa: E402
from _tiff_fixtures import make_tiff_bytes  # noqa: E402

INTERNAL_TOKEN = "test-internal-token-p2"


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    """每用例存储隔离 + 清空上传目录 + 清进程内缓存（键已 ID 化仍防串扰）。"""
    _, up_dir = isolate_app(monkeypatch, DATA_DIR, UPLOAD_DIR,
                            login_limits=True, clear_stores=True)
    for child in up_dir.iterdir():
        if child.is_file():
            child.unlink()
    app_mod.slide_cache._slide_cache.clear()
    app_mod._DEFAULT_FP_CACHE.clear()
    share_srv._tile_cache.clear()
    share_srv._DEFAULT_FP_CACHE.clear()
    monkeypatch.setattr(app_mod, "AI_INTERNAL_TOKEN", INTERNAL_TOKEN)
    yield


# --------------------------------------------------------------------------- #
# 辅助
# --------------------------------------------------------------------------- #
def _client():
    app_mod.app.config["TESTING"] = True
    app_mod.AUTH_ENABLED = True
    return csrf_client(app_mod.app.test_client())


def _share_client():
    share_srv.app.config["TESTING"] = True
    return share_srv.app.test_client()


def _login(client, login_id, password):
    return client.post("/login", data={"username": login_id,
                                       "password": password})


def _setup_users():
    owner = user_store.create_user("owner@x.com", "ownerpass123456",
                                   role="owner")
    usera = user_store.create_user("a@x.com", "userApass123456", role="user")
    userb = user_store.create_user("b@x.com", "userBpass123456", role="user")
    share_store.set_owner_user_id(owner["user_id"])
    return owner, usera, userb


def _touch_tiff(name):
    p = Path(UPLOAD_DIR) / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(make_tiff_bytes())
    return name


def _register(name, owner_user_id):
    """等价上传完成的真实状态：文件在盘 + 可读仓（P6：id_bundle 行——
    register_slide_row 发布 objects/<sid>/ 包，再回填归属）。"""
    register_slide_row(name)
    share_store.set_slide_meta(name, owner_user_id=owner_user_id)
    return share_store.get_slide_id(name)


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


def _annotate(client, slide_id=None, slide=None, label="L", **extra):
    body = {"type": "rect", "label": label, "x": 10, "y": 10,
            "width_px": 40, "height_px": 30, "note": "n"}
    if slide_id is not None:
        body["slide_id"] = slide_id
    if slide is not None:
        body["slide"] = slide
    body.update(extra)
    return client.post("/api/annotation", json=body)


def _internal_get(path):
    return app_mod.app.test_client().get(
        path, headers={"X-AI-Internal-Token": INTERNAL_TOKEN})


def _internal_post(path, body):
    return app_mod.app.test_client().post(
        path, json=body, headers={"X-AI-Internal-Token": INTERNAL_TOKEN})


def _two_same_display(owner):
    """两张同显示名切片（不同 legacy 文件名 → 不同 slide_id）。"""
    n1 = _touch_tiff("p2-one.tif")
    n2 = _touch_tiff("p2-two.tif")
    id1 = _register(n1, owner["user_id"])
    id2 = _register(n2, owner["user_id"])
    c = _client()
    _login(c, "owner@x.com", "ownerpass123456")
    assert c.patch("/api/slides/%s" % id1,
                   json={"display_name": "同名片"}).status_code == 200
    assert c.patch("/api/slides/%s" % id2,
                   json={"display_name": "同名片"}).status_code == 200
    return c, n1, n2, id1, id2


# =========================================================================== #
# 1. 同显示名两片：标注/分享/项目互不串（§8-1）
# =========================================================================== #
def test_same_display_name_annotations_isolated():
    owner, _a, _b = _setup_users()
    c, n1, n2, id1, id2 = _two_same_display(owner)
    assert _annotate(c, slide_id=id1, label="A1").status_code == 200
    assert _annotate(c, slide_id=id2, label="A2").status_code == 200

    r1 = c.get("/api/slides/%s/annotations" % id1).get_json()
    r2 = c.get("/api/slides/%s/annotations" % id2).get_json()
    labels1 = [grp["label"] for grp in r1["annotations"]]
    labels2 = [grp["label"] for grp in r2["annotations"]]
    assert labels1 == ["A1"], labels1
    assert labels2 == ["A2"], labels2
    # items 恒带 slide_id（DTO 新字段，§2.2）
    item = r1["annotations"][0]["items"][0]
    assert item["slide_id"] == id1

    # 变更流按 ID 隔离
    ch1 = c.get("/api/slides/%s/changes?after=0" % id1).get_json()
    ch2 = c.get("/api/slides/%s/changes?after=0" % id2).get_json()
    assert len(ch1["changes"]) == 1 and ch1["changes"][0]["label"] == "A1"
    assert len(ch2["changes"]) == 1 and ch2["changes"][0]["label"] == "A2"


def test_same_display_name_share_isolated():
    owner, _a, userb = _setup_users()
    c, n1, n2, id1, id2 = _two_same_display(owner)
    # 只分享 id1
    r = c.post("/api/share/create", json={
        "slide_ids": [id1], "expires_hours": 24})
    assert r.status_code == 200, r.get_data(as_text=True)
    token = r.get_json()["token"]

    sc = _share_client()
    # id1 经分享可读；id2 不在分享内（同显示名不串）
    assert sc.get("/s/%s/api/slides/%s/dzi" % (token, id1)).status_code == 200
    r2 = sc.get("/s/%s/api/slides/%s/dzi" % (token, id2))
    assert r2.status_code == 403

    # 旧名通道同名不可达（id2 无 id1 的成员关系）
    assert sc.get("/s/%s/api/slide/%s.dzi" % (token, n2)).status_code == 403


def test_project_same_name_different_ids_coexist():
    owner, _a, _b = _setup_users()
    c, n1, n2, id1, id2 = _two_same_display(owner)
    r = c.post("/api/project/create", json={
        "name": "P", "slide_ids": [id1, id2]})
    assert r.status_code == 200, r.get_data(as_text=True)
    pid = r.get_json()["pid"]
    proj = c.get("/api/project/%s" % pid).get_json()["project"]
    assert sorted(proj["slide_ids"]) == sorted([id1, id2])
    assert sorted(proj["slides"]) == sorted([n1, n2])

    # 分别标注后按项目聚合互不串
    _annotate(c, slide_id=id1, label="P1")
    _annotate(c, slide_id=id2, label="P2")
    det = c.get("/api/project/%s" % pid).get_json()
    per = {sa["slide_id"]: [grp["label"] for grp in sa["annotations"]]
           for sa in det["slide_annotations"]}
    assert per[id1] == ["P1"] and per[id2] == ["P2"]

    # DELETE by slide_id 精确命中一个
    r = c.delete("/api/project/%s/slides/%s" % (pid, id1))
    assert r.status_code == 200
    proj2 = r.get_json()
    assert proj2["slide_ids"] == [id2]
    # 未知 ID → 404
    assert c.delete("/api/project/%s/slides/sld_unknown00" % pid).status_code == 404


def test_project_slide_refs_are_row_aligned_with_ids():
    """项目行按 (slide, slide_id) 逐行下发：slide_ids 会剔除无 ID 的行，与
    slides 不能按下标配对；客户端以 slide_refs 的 slide_id 为行身份（id_bundle
    资产 name 为空、原始文件名可重复，按名打开/删除会落到 404/403）。"""
    import pg_store
    owner, _a, _b = _setup_users()
    c, n1, n2, id1, id2 = _two_same_display(owner)
    r = c.post("/api/project/create", json={"name": "P", "slide_ids": [id1, id2]})
    assert r.status_code == 200, r.get_data(as_text=True)
    pid = r.get_json()["pid"]
    conn = pg_store.connect()
    try:
        with conn.cursor() as cur:
            cur.execute("INSERT INTO project_slides (project_id, slide, slide_id, position) "
                        "VALUES (%s, 'legacy-only.svs', NULL, 0)", (pid,))
        conn.commit()
    finally:
        conn.close()
    item = [p for p in c.get("/api/projects").get_json() if p["pid"] == pid][0]
    refs = item["slide_refs"]
    assert len(refs) == len(item["slides"]) == 3
    assert [r["slide"] for r in refs] == item["slides"]
    assert {r["slide_id"] for r in refs} == {id1, id2, None}
    assert sorted(item["slide_ids"]) == sorted([id1, id2])
    for ref in refs:
        if ref["slide_id"]:
            assert c.get("/api/slides/%s/info" % ref["slide_id"]).status_code == 200
    detail = c.get("/api/project/%s" % pid).get_json()["project"]
    assert detail["slide_refs"] == refs


@pytest.mark.parametrize("idem_key", [None, "k-dup-same"])
def test_project_create_by_ids_keeps_same_filename_id_bundle_slides(idem_key):
    """id_bundle 资产无 legacy 名（名快照=原始文件名，可重复）：按 slide_ids
    建项目——无论带不带 Idempotency-Key——两张同原始文件名切片都须以各自
    slide_id 成行（生产 dogfood：按文件名落行 → 并成一行、ID 丢失、读取失败）。"""
    from _pt_helpers import publish_test_slide
    owner, _a, _b = _setup_users()
    id1 = publish_test_slide("dup-same.tif", make_tiff_bytes(),
                             owner_user_id=owner["user_id"])
    id2 = publish_test_slide("dup-same.tif", make_tiff_bytes(),
                             owner_user_id=owner["user_id"])
    assert id1 != id2
    c = _client()
    _login(c, "owner@x.com", "ownerpass123456")
    headers = {"Idempotency-Key": idem_key} if idem_key else {}
    r = c.post("/api/project/create", headers=headers,
               json={"name": "P", "slide_ids": [id1, id2]})
    assert r.status_code == 200, r.get_data(as_text=True)
    pid = r.get_json()["pid"]
    proj = c.get("/api/project/%s" % pid).get_json()["project"]
    assert [ref["slide_id"] for ref in proj["slide_refs"]] == [id1, id2]
    assert proj["slide_ids"] == [id1, id2]


# =========================================================================== #
# 2. 改名不动 ID/授权（§8-1）
# =========================================================================== #
def test_rename_display_name_keeps_id_and_grants():
    owner, _a, userb = _setup_users()
    name = _touch_tiff("rename.tif")
    sid = _register(name, owner["user_id"])
    # userb 获得显式 view grant
    share_store.grant_slide_view(userb["user_id"], name, 30 * 24 * 3600,
                                 granted_by=owner["user_id"],
                                 slide_id=sid)
    cb = _client()
    _login(cb, "b@x.com", "userBpass123456")
    assert cb.get("/api/slides/%s/info" % sid).status_code == 200

    c = _client()
    _login(c, "owner@x.com", "ownerpass123456")
    _annotate(c, slide_id=sid, label="R1")
    r = c.patch("/api/slides/%s" % sid,
                json={"display_name": "新名字", "note": "nn"})
    assert r.status_code == 200, r.get_data(as_text=True)
    body = r.get_json()
    assert body["slide_id"] == sid and body["display_name"] == "新名字"

    # ID 不变；授权不变（grant 仍生效）；标注仍按 ID 归属
    assert cb.get("/api/slides/%s/info" % sid).status_code == 200
    ann = c.get("/api/slides/%s/annotations" % sid).get_json()
    labels = [grp["label"] for grp in ann["annotations"]]
    assert labels == ["R1"]
    # DTO 的 alias 从 display_name 派生（R-02）
    info = c.get("/api/slides/%s/info" % sid).get_json()
    assert info["display_name"] == "新名字" and info["alias"] == "新名字"
    assert info["slide_id"] == sid


# =========================================================================== #
# 3. 双字段冲突 / 未知 ID（§2.1）
# =========================================================================== #
def test_slide_ref_conflict_and_unknown_id():
    owner, _a, _b = _setup_users()
    c, n1, n2, id1, id2 = _two_same_display(owner)
    # 同给且指向不同资产 → 400 slide_ref_conflict
    r = _annotate(c, slide_id=id1, slide=n2)
    assert r.status_code == 400
    assert "slide_ref_conflict" in r.get_data(as_text=True)
    # 未知 ID → 404 slide_not_found
    r = _annotate(c, slide_id="sld_unknown00")
    assert r.status_code == 404
    assert "slide_not_found" in r.get_data(as_text=True)
    # /api/annotations 双字段冲突
    r = c.get("/api/annotations?slide_id=%s&slide=%s" % (id1, n2))
    assert r.status_code == 400
    # 一致的（同资产名+ID）→ 200
    r = c.get("/api/annotations?slide_id=%s&slide=%s" % (id1, n1))
    assert r.status_code == 200
    assert r.get_json()["slide_id"] == id1


# =========================================================================== #
# 4. 历史 NULL-slide_id 行不展示（§1/R-08）
# =========================================================================== #
def test_null_slide_id_history_rows_hidden():
    owner, _a, _b = _setup_users()
    c, n1, _n2, id1, _id2 = _two_same_display(owner)
    _annotate(c, slide_id=id1, label="LIVE")
    # 直接 SQL 造一条 slide_id IS NULL 的历史行（unresolved）
    _sql(
        "INSERT INTO rois (id, token, slide, annotation_id, label, type, geom, "
        " size_mm, shared, note, deleted, created_at, updated_at, data, "
        " visibility_status) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,"
        " to_timestamp(%s), to_timestamp(%s), %s, %s)",
        ("roi_hist_p2_1", share_store.ADMIN_TOKEN, n1, "anno-hist-p2-1",
         "HIST", "rect", psycopg.types.json.Jsonb({"x": 1, "y": 1, "w": 5,
                                                   "h": 5}),
         0.0, False, "", False, 1700000000, 1700000000,
         psycopg.types.json.Jsonb({"token": share_store.ADMIN_TOKEN,
                                   "slide": n1, "label": "HIST",
                                   "annotation_id": "anno-hist-p2-1",
                                   "ts": 1700000000, "revision": 1}),
         "unclaimed"))
    _sql("INSERT INTO change_log (slide, token, annotation_id, op) "
         "VALUES (%s,%s,%s,'add')", (n1, share_store.ADMIN_TOKEN,
                                      "anno-hist-p2-1"))
    ann = c.get("/api/slides/%s/annotations" % id1).get_json()
    labels = [grp["label"] for grp in ann["annotations"]]
    assert labels == ["LIVE"], labels
    ch = c.get("/api/slides/%s/changes?after=0" % id1).get_json()
    labels_ch = [e.get("label") for e in ch["changes"]]
    assert labels_ch == ["LIVE"], labels_ch


# =========================================================================== #
# 5. 删除后旧 share token / view grant / run grant 对新资产拒绝（§8）
# =========================================================================== #
def test_old_credentials_rejected_for_new_asset():
    owner, _a, userb = _setup_users()
    n_old = _touch_tiff("old.tif")
    id_old = _register(n_old, owner["user_id"])
    n_new = _touch_tiff("new.tif")
    id_new = _register(n_new, owner["user_id"])

    c = _client()
    _login(c, "owner@x.com", "ownerpass123456")
    # 旧 share（含 old）、旧 view grant（userb → old）、旧 run grant（old）
    token = c.post("/api/share/create", json={
        "slides": [n_old], "expires_hours": 24}).get_json()["token"]
    share_store.grant_slide_view(userb["user_id"], n_old, 30 * 24 * 3600,
                                  granted_by=owner["user_id"],
                                  slide_id=id_old)
    grant = share_store.create_run_grant("inst-p2", n_old,
                                         created_by_user_id=owner["user_id"])

    cb = _client()
    _login(cb, "b@x.com", "userBpass123456")
    # 授权前先让 userb 对 new 有账号（无 old 授权）：new 不可读
    assert cb.get("/api/slides/%s/info" % id_new).status_code == 403

    # 旧 view grant 不作用于新资产（同显示名也不行）
    c.patch("/api/slides/%s" % id_old, json={"display_name": "DUP"})
    c.patch("/api/slides/%s" % id_new, json={"display_name": "DUP"})
    assert cb.get("/api/slides/%s/info" % id_new).status_code == 403
    assert cb.get("/api/slides/%s/info" % id_old).status_code == 200

    # 旧 share token 对新资产拒绝（share_slides 只含 old 的 ID）
    sc = _share_client()
    assert sc.get("/s/%s/api/slides/%s/dzi" % (token, id_new)).status_code == 403

    # 旧 run grant：对新资产（名或 ID）校验一律失败
    valid, reason = app_mod._verify_run_grant(
        grant["grant_id"], n_new, "inst-p2")
    assert not valid and reason == "slide_mismatch", (valid, reason)
    valid, reason = app_mod._verify_run_grant(
        grant["grant_id"], n_new, "inst-p2", slide_id=id_new)
    assert not valid and reason == "slide_mismatch"
    # 对原资产仍有效（名通道解析回原 ID）
    valid, reason = app_mod._verify_run_grant(
        grant["grant_id"], n_old, "inst-p2")
    assert valid, reason

    # 删除 old：旧 share token/旧授权对已删资产拒绝（状态门禁）
    assert c.delete("/api/slide/%s" % n_old).status_code == 200
    assert sc.get("/s/%s/api/slides/%s/dzi" % (token, id_old)).status_code == 403
    assert cb.get("/api/slides/%s/info" % id_old).status_code == 403


# =========================================================================== #
# 6. 双写字段一致性（§1/§3.1/§3.3）
# =========================================================================== #
def test_dual_write_consistency():
    owner, _a, _b = _setup_users()
    c, n1, _n2, id1, _id2 = _two_same_display(owner)
    r = _annotate(c, slide_id=id1, label="DW")
    assert r.status_code == 200
    aid = r.get_json()["annotation_id"]
    row = _sql("SELECT slide, slide_id FROM rois WHERE annotation_id=%s",
               (aid,), fetch=True)[0]
    assert row[0] == n1 and row[1] == id1
    seqs = _sql("SELECT slide_id FROM change_log WHERE annotation_id=%s",
                (aid,), fetch=True)
    assert seqs and all(s[0] == id1 for s in seqs)

    # 评论双写（名通道也解析出 ID）
    cmt = share_store.add_comment(aid, n1, share_store.ADMIN_TOKEN, "hi",
                                  slide_id=id1)
    crow = _sql("SELECT slide_id FROM comments WHERE comment_id=%s",
                (cmt["comment_id"],), fetch=True)[0]
    assert crow[0] == id1
    cmt2 = share_store.add_comment(aid, n1, share_store.ADMIN_TOKEN, "hi2")
    crow2 = _sql("SELECT slide_id FROM comments WHERE comment_id=%s",
                 (cmt2["comment_id"],), fetch=True)[0]
    assert crow2[0] == id1  # 名入参自动解析

    # run grant / principal 双写
    g = share_store.create_run_grant("inst-dw", n1)
    assert g["slide_id"] == id1
    share_store.upsert_ai_session_principal("sess-dw", owner["user_id"], n1)
    p = share_store.get_ai_session_principal("sess-dw")
    assert p["slide_id"] == id1 and p["slide"] == n1

    # access events 双写（授权变更走 annotation_grants）
    share_store.grant_annotation_to_user(aid, owner["user_id"],
                                         created_by=owner["user_id"])
    ev = _sql("SELECT slide_id FROM annotation_access_events "
              "WHERE annotation_id=%s", (aid,), fetch=True)
    assert ev and all(e[0] == id1 for e in ev)

    # audit 双写（slide.delete 先例已带 ID；这里查 annotation.add 列）
    _sql("DELETE FROM audit_events")
    _annotate(c, slide_id=id1, label="AUD")
    au = _sql("SELECT slide_id FROM audit_events WHERE action='annotation.add'",
              (), fetch=True)
    assert au and all(a[0] == id1 for a in au)


# =========================================================================== #
# 7. alias 停写（§7/R-02）
# =========================================================================== #
def test_alias_stop_write():
    owner, _a, _b = _setup_users()
    name = _touch_tiff("alias.tif")
    sid = _register(name, owner["user_id"])
    # set_slide_meta(alias=...) 只写 display_name，不写 alias 列
    meta = share_store.set_slide_meta(name, alias="显示甲")
    assert meta["alias"] == "显示甲"
    col = _sql("SELECT alias, display_name FROM slides WHERE slide_id=%s",
               (sid,), fetch=True)[0]
    assert col[0] == "" and col[1] == "显示甲", col
    # 读侧出参从 display_name 派生
    assert share_store.get_slide_meta(name)["alias"] == "显示甲"
    assert share_store.get_slide_meta_full(name)["alias"] == "显示甲"

    # meta 端点 alias 入参映射 display_name（列不动）
    c = _client()
    _login(c, "owner@x.com", "ownerpass123456")
    r = c.post("/api/slide/%s/meta" % name, json={"alias": "显示乙"})
    assert r.status_code == 200
    col = _sql("SELECT alias, display_name FROM slides WHERE slide_id=%s",
               (sid,), fetch=True)[0]
    assert col[0] == "" and col[1] == "显示乙", col
    assert r.get_json()["meta"]["alias"] == "显示乙"

    # PATCH 端点：display_name/note/public（列仍不写）
    r = c.patch("/api/slides/%s" % sid,
                json={"display_name": "显示丙", "public": True})
    assert r.status_code == 200
    col = _sql("SELECT alias, display_name, public FROM slides "
               "WHERE slide_id=%s", (sid,), fetch=True)[0]
    assert col[0] == "" and col[1] == "显示丙" and col[2] is True
    # user 不能改他人切片 / 不能设 public
    cb = _client()
    _login(cb, "b@x.com", "userBpass123456")
    assert cb.patch("/api/slides/%s" % sid,
                    json={"display_name": "X"}).status_code == 403
    # 未知 ID
    assert c.patch("/api/slides/sld_unknown00",
                   json={"display_name": "X"}).status_code == 404


# =========================================================================== #
# 8. 读取端点全族 ID 化（§4）
# =========================================================================== #
def test_read_family_id_routes():
    owner, _a, _b = _setup_users()
    c, n1, _n2, id1, _id2 = _two_same_display(owner)
    _annotate(c, slide_id=id1, label="FR")
    urls = [
        "/api/slides/%s" % id1 + "/info",
        "/api/slides/%s/dzi" % id1,
        "/api/slides/%s/tiles/0/0_0.jpeg" % id1,
        "/api/slides/%s/thumbnail" % id1,
        "/api/slides/%s/crop?x=0&y=0&size=8" % id1,
        "/api/slides/%s/region?x=0&y=0&w=8&h=8" % id1,
        "/api/slides/%s/annotations" % id1,
        "/api/slides/%s/changes?after=0" % id1,
    ]
    for u in urls:
        r = c.get(u)
        assert r.status_code == 200, (u, r.status_code)
    # DZI XML 的瓦片 URL 指向 ID 通道 tiles 前缀
    xml = c.get("/api/slides/%s/dzi" % id1).get_data(as_text=True)
    assert 'Url="/api/slides/%s/tiles/"' % id1 in xml
    # 未知 ID 全族 404 slide_not_found
    for u in ["/api/slides/sld_unknown00/info",
              "/api/slides/sld_unknown00/dzi",
              "/api/slides/sld_unknown00/annotations",
              "/api/slides/sld_unknown00/changes?after=0"]:
        r = c.get(u)
        assert r.status_code == 404, u
        assert "slide_not_found" in r.get_data(as_text=True)
    # 未授权（他人）不可读
    cb = _client()
    _login(cb, "b@x.com", "userBpass123456")
    assert cb.get("/api/slides/%s/info" % id1).status_code == 403


# =========================================================================== #
# 9. 机器通道双字段（/internal/ai/*；协调方补充范围）
# =========================================================================== #
def test_internal_ai_dual_field():
    owner, _a, _b = _setup_users()
    c, n1, n2, id1, id2 = _two_same_display(owner)
    # slide_info：slide_id 优先 / 名 alias / 冲突 400 / 未知 404
    r = _internal_get("/internal/ai/slide_info?slide_id=%s" % id1)
    assert r.status_code == 200 and r.get_json()["width"] > 0
    r = _internal_get("/internal/ai/slide_info?slide=%s" % n1)
    assert r.status_code == 200
    r = _internal_get("/internal/ai/slide_info?slide_id=%s&slide=%s"
                      % (id1, n2))
    assert r.status_code == 400
    r = _internal_get("/internal/ai/slide_info?slide_id=sld_unknown00")
    assert r.status_code == 404

    # annotate：slide_id 优先写入（rois.slide_id 双写）；带属主/会话使
    # spots 读取主体（run grant / principal 绑定）可见（0056 fail-closed）
    r = _internal_post("/internal/ai/annotate", {
        "slide_id": id1, "label": "AI-DW", "x": 2, "y": 2,
        "width_px": 20, "height_px": 20, "effect_key": "ek-p2-1",
        "session_id": "sess-p2-internal",
        "created_by_user_id": owner["user_id"]})
    assert r.status_code == 200, r.get_data(as_text=True)
    aid = r.get_json()["annotation_id"]
    row = _sql("SELECT slide, slide_id FROM rois WHERE annotation_id=%s",
               (aid,), fetch=True)[0]
    assert row == (n1, id1)

    # spots：slide_id 查询流只含该资产（X-AI-Session-Owner 绑定读取主体）
    r = app_mod.app.test_client().get(
        "/internal/ai/spots?slide_id=%s&after=0&session_id=sess-p2-internal"
        % id1, headers={"X-AI-Internal-Token": INTERNAL_TOKEN,
                       "X-AI-Session-Owner": owner["user_id"]})
    assert r.status_code == 200
    assert r.get_json()["slide_id"] == id1
    assert len(r.get_json()["changes"]) == 1

    # region：slide_id 通道
    r = _internal_post("/internal/ai/region", {
        "slide_id": id1, "x": 0, "y": 0, "w": 8, "h": 8})
    assert r.status_code == 200 and r.get_json()["image_base64"]


# =========================================================================== #
# 10. 插件桥 by-id 族 + run grant ID 校验（§3.3；协调方补充范围）
# =========================================================================== #
def test_plugin_by_id_and_run_grant_id_verify(monkeypatch):
    owner, _a, _b = _setup_users()
    c, n1, n2, id1, id2 = _two_same_display(owner)
    monkeypatch.setattr(app_mod, "_require_plugin_token",
                        lambda scope=None: ({"sub": "inst-p2"}, None))
    monkeypatch.setattr(app_mod, "share_store", app_mod.share_store)
    monkeypatch.setattr(app_mod, "_audit", lambda *a, **k: None)

    grant = share_store.create_run_grant("inst-p2", n1,
                                         created_by_user_id=owner["user_id"])
    assert grant["slide_id"] == id1
    pc = app_mod.app.test_client()
    hdr = {"X-Run-Grant": grant["grant_id"]}

    # by-id slide_info
    r = pc.get("/api/plugin/v1/slides/by-id/%s" % id1)
    assert r.status_code == 200 and r.get_json()["width"] > 0
    assert pc.get("/api/plugin/v1/slides/by-id/sld_unknown00").status_code == 404

    # by-id regions（grant 命中同资产 ID）
    r = pc.post("/api/plugin/v1/slides/by-id/%s/regions" % id1,
                json={"x": 0, "y": 0, "w": 8, "h": 8}, headers=hdr)
    assert r.status_code == 200, r.get_data(as_text=True)

    # 名通道 + 他人名伪造：grant 属 id1，请求名是 n2 → mismatch（防伪）
    r = pc.post("/api/plugin/v1/slides/%s/regions" % n2,
                json={"x": 0, "y": 0, "w": 8, "h": 8}, headers=hdr)
    assert r.status_code == 403

    # by-id annotate：grant 属 id1，目标 id2 → 403 slide_mismatch
    r = pc.post("/api/plugin/v1/slides/by-id/%s/annotations" % id2,
                json={"label": "AI-X", "x": 2, "y": 2, "width_px": 10,
                      "height_px": 10, "session_id": "sess-p2"},
                headers=hdr)
    assert r.status_code == 403

    # by-id annotate：同资产 → 200 且 rois.slide_id 双写
    share_store.bind_run_grant_session(grant["grant_id"], "sess-p2")
    r = pc.post("/api/plugin/v1/slides/by-id/%s/annotations" % id1,
                json={"label": "AI-OK", "x": 2, "y": 2, "width_px": 10,
                      "height_px": 10, "session_id": "sess-p2"},
                headers=hdr)
    assert r.status_code == 200, r.get_data(as_text=True)
    aid = r.get_json()["annotation_id"]
    row = _sql("SELECT slide_id FROM rois WHERE annotation_id=%s",
               (aid,), fetch=True)[0]
    assert row[0] == id1

    # by-id changes
    r = pc.get("/api/plugin/v1/slides/by-id/%s/changes?after=0" % id1)
    assert r.status_code == 200 and r.get_json()["slide_id"] == id1

    # verify 端点：双字段冲突 400；ID 匹配 200；错配 invalid
    r = pc.post("/api/plugin/v1/run-grants/verify",
                json={"grant_id": grant["grant_id"], "slide": n2,
                      "slide_id": id1})
    assert r.status_code == 400
    r = pc.post("/api/plugin/v1/run-grants/verify",
                json={"grant_id": grant["grant_id"], "slide_id": id1})
    assert r.status_code == 200 and r.get_json()["valid"] is True
    r = pc.post("/api/plugin/v1/run-grants/verify",
                json={"grant_id": grant["grant_id"], "slide_id": id2})
    assert r.status_code == 200 and r.get_json()["valid"] is False


# =========================================================================== #
# 11. research slide_pseudonym 派生源切换（§3.4/R-11）
# =========================================================================== #
def test_research_pseudonym_from_slide_id():
    owner, _a, _b = _setup_users()
    name = _touch_tiff("res.tif")
    sid = _register(name, owner["user_id"])
    import research_store as rs
    conn = rs._connect()
    try:
        with conn.cursor() as cur:
            p_id = rs.slide_pseudonym(name, cur, slide_id=sid)
            p_name = rs.slide_pseudonym(name, cur)
            p_id2 = rs.slide_pseudonym("renamed.tif", cur, slide_id=sid)
    finally:
        conn.close()
    # ID 派生与名派生不同域；同 ID 不同名 → 同伪名（改名不换伪名）
    assert p_id != p_name
    assert p_id == p_id2


# =========================================================================== #
# 12. demo 兜底收口（§3.4：catalog 项必须 resolve_slide_id 命中）
# =========================================================================== #
def test_demo_catalog_requires_resolvable_row():
    owner, _a, _b = _setup_users()
    name = _touch_tiff("demo-p2.tif")
    sid = _register(name, owner["user_id"])
    import demo_store
    entry = demo_store.catalog_add(sid, display_name="D")
    assert entry is not None
    # 行删除（模拟无行资产）→ _demo_catalog_slide fail-closed
    _sql("DELETE FROM slides WHERE slide_id=%s", (sid,))
    e, d = app_mod._demo_catalog_slide(sid)
    assert e is None and d is None


# =========================================================================== #
# 13. P2 前端缺口修复回归（share 列表 slide_id / 分享 ROI 双字段 /
#     render-context by-id / research 双字段 / conversions GET slide_id）
# =========================================================================== #
def _mk_share_with_slide(owner):
    """一主一片一分享的基础夹具：返回 (client, name, slide_id, token)。"""
    c = _client()
    _login(c, "owner@x.com", "ownerpass123456")
    name = _touch_tiff("gap.tif")
    sid = _register(name, owner["user_id"])
    token = c.post("/api/share/create", json={
        "slide_ids": [sid], "expires_hours": 24}).get_json()["token"]
    return c, name, sid, token


def test_share_listing_carries_slide_id():
    """缺口①：GET /s/<token>/api/slides 成员项带 slide_id（前端按 ID 键控）。"""
    owner, _a, _b = _setup_users()
    _c, name, sid, token = _mk_share_with_slide(owner)
    items = _share_client().get("/s/%s/api/slides" % token).get_json()
    assert len(items) == 1
    assert items[0]["slide_id"] == sid
    assert items[0]["name"] == name
    assert "display_name" in items[0]


def test_share_roi_add_dual_field():
    """缺口②：POST /s/<token>/api/roi 接受 slide_id（优先）且冲突 400。"""
    owner, _a, _b = _setup_users()
    _c, name, sid, token = _mk_share_with_slide(owner)
    sc = _share_client()
    # slide_id 通道
    r = sc.post("/s/%s/api/roi" % token, json={
        "slide_id": sid, "label": "L1", "type": "freehand",
        "points": [[1, 1], [2, 2], [3, 1]]})
    assert r.status_code == 200, r.get_data(as_text=True)
    row = _sql("SELECT slide, slide_id FROM rois ORDER BY insert_seq DESC "
               "LIMIT 1", fetch=True)[0]
    assert row[0] == name and row[1] == sid
    # 双字段同资产 → 放行；不同资产 → 400
    _touch_tiff("gap2.tif")
    sid2 = _register("gap2.tif", owner["user_id"])
    r2 = sc.post("/s/%s/api/roi" % token, json={
        "slide_id": sid, "slide": name, "label": "L2", "type": "freehand",
        "points": [[1, 1], [2, 2], [3, 1]]})
    assert r2.status_code == 200
    r3 = sc.post("/s/%s/api/roi" % token, json={
        "slide_id": sid2, "slide": name, "label": "L3", "type": "freehand",
        "points": [[1, 1], [2, 2], [3, 1]]})
    assert r3.status_code == 400
    assert r3.get_json().get("code") == "slide_ref_conflict"


def test_render_context_by_id_routes():
    """缺口④：主站与分享端 render-context by-id 路由（多通道 flag 关时 403）。"""
    owner, _a, _b = _setup_users()
    c, _name, sid, token = _mk_share_with_slide(owner)
    # flag 默认关：两端点都应到达功能闸（403 multichannel_disabled），
    # 而不是 404（路由存在性证据）；未知 ID 主站 404、分享端 403
    r_main = c.post("/api/slides/%s/render-context" % sid, json={})
    assert r_main.status_code in (200, 403)
    assert c.post("/api/slides/sld_nonexistent/render-context",
                  json={}).status_code == 404
    r_share = _share_client().post(
        "/s/%s/api/slides/%s/render-context" % (token, sid), json={})
    assert r_share.status_code in (200, 403)
    assert _share_client().post(
        "/s/%s/api/slides/sld_nonexistent/render-context" % token,
        json={}).status_code == 403


def test_research_viewing_session_dual_field():
    """缺口③：研究读片会话接受 slide_id；冲突 400；伪名从 ID 派生。"""
    owner, _a, _b = _setup_users()
    c = _client()
    _login(c, "owner@x.com", "ownerpass123456")
    name = _touch_tiff("res2.tif")
    sid = _register(name, owner["user_id"])
    import research_store as rs
    rs_store_env = rs  # noqa: F841
    # 研究采集需开关+授权——直接走 store 层验证 slide_id 透传语义已在
    # test_research_pseudonym_from_slide_id 覆盖；此处验证 HTTP 层字段接收
    # 与冲突裁决（开关关闭时 403/503 均可，但不能 400 invalid_request）
    r = c.post("/api/research/viewing-sessions", json={"slide_id": sid})
    assert r.status_code != 400 or "未知字段" not in r.get_data(as_text=True)
    r2 = c.post("/api/research/viewing-sessions",
                json={"slide_id": sid, "slide": "nonexistent.tif"})
    assert r2.status_code != 400 or "未知字段" not in r2.get_data(as_text=True)
    # 明确冲突（名解析到另一资产）→ 400 slide_ref_conflict
    _touch_tiff("res3.tif")
    sid3 = _register("res3.tif", owner["user_id"])
    r3 = c.post("/api/research/viewing-sessions",
                json={"slide_id": sid, "slide": "res3.tif"})
    assert r3.status_code == 400
    assert r3.get_json().get("code") == "slide_ref_conflict"
    assert sid3 != sid


def test_conversions_get_carries_slide_id():
    """缺口⑤（P4-app 断言换新）：GET /api/conversions/<job_id> 响应带
    slide_id——**从任务绑定读**（create_job 即预分配；不按 canonical 名
    resolve——新产物是独立 id_bundle 资产，无 legacy_filename）。"""
    owner, _a, _b = _setup_users()
    c = _client()
    _login(c, "owner@x.com", "ownerpass123456")
    name = _touch_tiff("conv-src.tif")
    _register(name, owner["user_id"])
    import conversion_store
    job = conversion_store.create_job(
        owner_user_id=owner["user_id"], upload_id=None, source_name=name,
        source_sha256="0" * 64, source_format="kfb",
        canonical_name="conv-src.tif.tif")
    assert job["slide_id"]  # create_job 即预分配产物资产
    # 直接置 ready（模拟转换完成的收口态）
    _sql("UPDATE slides SET asset_state='ready', published_at=now(), "
         "accounted_bytes=1 WHERE slide_id=%s", (job["slide_id"],))
    _sql("UPDATE conversion_jobs SET state='ready' WHERE id=%s", (job["id"],))
    r = c.get("/api/conversions/%s" % job["id"])
    assert r.status_code == 200, r.get_data(as_text=True)
    assert r.get_json().get("slide_id") == job["slide_id"]
