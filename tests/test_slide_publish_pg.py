# -*- coding: utf-8 -*-
"""slide ID 化重构 P3：统一本地发布门禁测试（U5 检查点 B 后保留面）。

合同：docs/slide-id-refactor-p3-contract-20260925.md §6（计划 §8 矩阵的 P3
部分）。旧 V1/V2 上传端点已随 COS 上传统一删除：发布链路（任务状态机/
commit 三段式/取消/配额预占）断言归 COS 链路测试
（tests/test_cos_ingestion_kinds.py / test_ingestion_api.py /
test_cos_ingest_worker.py）；本文件保留**资产侧**行为——同名独立 ID/归属、
删除减账与授权不继承、显示名/revision 语义、ID 端点读路径。资产夹具走
_pt_helpers.publish_test_slide（服务级发布，无 HTTP）。

仍覆盖：

  1. 同名（两账户/同账户）→ 不同 slide_id/不同 objects 目录、各自归属；
  2. 删除 → tombstone + 减账幂等；同名重传 → 新 ID；旧分享/授权/标注/AI
     run grant/Demo 不指向新资产；
  3. annotations_by_slide 对同名 id_bundle 资产按 ID 分组不串；
  4. 显示名修改不移动文件、不动 revision；
  5. 旧端点对 id_bundle 资产按名找不到（403 预期）；列表/info/dzi/tile/
     crop/region 经 ID 端点全通；
  6. 机器通道无行兼容分支已删：无行文件经 internal/plugin 解析一律拒。

运行：cd 项目根 && python3 -m pytest tests/test_slide_publish_pg.py -q
"""
import hashlib
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import _bootstrap  # noqa: E402,F401  # session 目录+openslide stub（conftest 先行）
DATA_DIR = _bootstrap.SHARE_DATA_DIR
UPLOAD_DIR = _bootstrap.UPLOAD_DIR
import psycopg  # noqa: E402
import pytest  # noqa: E402

import app as app_mod  # noqa: E402
import share_store  # noqa: E402
import slide_store  # noqa: E402
import slide_storage  # noqa: E402
import upload_guard  # noqa: E402
import upload_task_store  # noqa: E402
import user_store  # noqa: E402
from _pt_helpers import (clear_upload_dir, csrf_client, isolate_app,  # noqa: E402
                         publish_test_slide)
from _tiff_fixtures import make_tiff_bytes  # noqa: E402

PG_URI = os.environ["DATABASE_URL"]


# --------------------------------------------------------------------------- #
# 基建
# --------------------------------------------------------------------------- #
@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    """每用例：独立存储 + 防护参数复位 + 清空 uploads + 恢复超时复位。"""
    isolate_app(monkeypatch, tmp_path, UPLOAD_DIR)
    monkeypatch.setattr(upload_guard, "UPLOAD_RESERVED_FREE_BYTES", 0)
    monkeypatch.setattr(upload_task_store, "UPLOAD_TASK_TTL_SECONDS", 24 * 3600)
    monkeypatch.setattr(upload_task_store, "UPLOAD_COMMIT_TIMEOUT_SECONDS", 600)
    monkeypatch.setattr(upload_task_store, "UPLOAD_CHUNK_MAX_BYTES",
                        64 * 1024 * 1024)
    monkeypatch.setitem(app_mod.app.config, "MAX_CONTENT_LENGTH", None)
    clear_upload_dir(UPLOAD_DIR)
    yield


def _client(auth=True):
    app_mod.app.config["TESTING"] = True
    app_mod.AUTH_ENABLED = auth
    return csrf_client(app_mod.app.test_client())


def _user_session(client, role="user", login="u@x.com"):
    u = user_store.create_user(login, "pass1234pass1234", role=role)
    with client.session_transaction() as sess:
        sess["auth_user"] = True
        sess["user_id"] = u["user_id"]
        sess["role"] = role
        sess["auth_version"] = (user_store.get_user(u["user_id"]) or {}).get(
            "auth_version", 1)
    return u["user_id"]


def _one(sql, params=()):
    with psycopg.connect(PG_URI, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            row = cur.fetchone()
            return row[0] if row else None


def _quota(uid):
    return upload_guard.get_quota_row(uid)


def _quota_bytes(uid, n):
    with psycopg.connect(PG_URI, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute("INSERT INTO upload_user_quotas (user_id, quota_bytes) "
                        "VALUES (%s, %s) ON CONFLICT (user_id) DO UPDATE SET "
                        "quota_bytes=%s", (uid, n, n))


def _settle_used(uid, n):
    """模拟上传链路的配额结算前置态（publish_test_slide 离线通道不结算
    配额——删除减账断言需要 used_bytes 已按 accounted_bytes 入账）。"""
    with psycopg.connect(PG_URI, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE upload_user_quotas SET used_bytes=%s "
                        "WHERE user_id=%s", (n, uid))


TIFF = make_tiff_bytes(64, 96)
TIFF_SHA = hashlib.sha256(TIFF).hexdigest()


def _bundle_dir(slide_id):
    return Path(UPLOAD_DIR) / "objects" / slide_id


def _desc(slide_id):
    return slide_store.resolve_slide_id(slide_id)


# --------------------------------------------------------------------------- #
# §6-1 同名并发（两账户同名同字节）→ 不同 ID/目录/归属
# --------------------------------------------------------------------------- #
def test_v2_same_name_two_accounts_distinct_ids(tmp_path):
    ca = _client()
    cb = _client()
    uid_a = _user_session(ca, login="a@x.com")
    uid_b = _user_session(cb, login="b@x.com")
    sid_a = publish_test_slide("same.tif", TIFF, owner_user_id=uid_a,
                               upload_dir=UPLOAD_DIR)
    sid_b = publish_test_slide("same.tif", TIFF, owner_user_id=uid_b,
                               upload_dir=UPLOAD_DIR)
    assert sid_a != sid_b
    da, db = _desc(sid_a), _desc(sid_b)
    assert da.storage_layout == "id_bundle" and db.storage_layout == "id_bundle"
    assert da.owner_user_id == uid_a and db.owner_user_id == uid_b
    assert da.legacy_filename is None and db.legacy_filename is None
    assert da.asset_state == db.asset_state == "ready"
    # 不同 objects 目录 + 入口文件就位（no-clobber 由 ID 唯一性兜底）
    assert _bundle_dir(sid_a).is_dir() and _bundle_dir(sid_b).is_dir()
    assert (_bundle_dir(sid_a) / "data.tif").is_file()
    assert (_bundle_dir(sid_b) / "data.tif").is_file()
    # 同账号同名并发同样不冲突（allocate_slide 不查原名）
    sid_a2 = publish_test_slide("same.tif", TIFF, owner_user_id=uid_a,
                                upload_dir=UPLOAD_DIR)
    assert sid_a2 not in (sid_a, sid_b)


# U5（检查点 B）：旧上传端点删除，本场景已由 COS 统一链路覆盖（tests/test_cos_ingestion_kinds.py / test_ingestion_api.py / test_cos_ingest_worker.py）。
# （原 §6-1 test_v2_same_name_no_conflict_409_gone：create 端点同名不再 409
#   的响应断言——端点行为；同名独立 ID 的资产侧语义由上面的用例覆盖。）


# --------------------------------------------------------------------------- #
# §6-2 重复 commit / 响应丢失重试 → 同 ID 一次结算
# --------------------------------------------------------------------------- #
# U5（检查点 B）：旧上传端点删除，本场景已由 COS 统一链路覆盖（tests/test_cos_ingestion_kinds.py / test_ingestion_api.py / test_cos_ingest_worker.py）。
# （原 §6-2 test_v2_repeat_commit_same_id_single_settlement：commit 三段式
#   幂等与配额恰一次结算——纯 V2 commit 端点行为。）


# --------------------------------------------------------------------------- #
# §6-3 三处崩溃点（阶段屏障注入）→ 恢复收口一次、未 ready 不可读
# --------------------------------------------------------------------------- #
# U5（检查点 B）：旧上传端点删除，本场景已由 COS 统一链路覆盖（tests/test_cos_ingestion_kinds.py / test_ingestion_api.py / test_cos_ingest_worker.py）。
# （原 §6-3 三个 test_crash_* 用例：intent 前/intent 后发布前/发布后 DB 前
#   的 commit 崩溃窗口与恢复——纯 V2 commit 端点行为。）


# --------------------------------------------------------------------------- #
# §6-4 取消/删除与发布并发 → 状态机裁定明确胜者
# --------------------------------------------------------------------------- #
# U5（检查点 B）：旧上传端点删除，本场景已由 COS 统一链路覆盖（tests/test_cos_ingestion_kinds.py / test_ingestion_api.py / test_cos_ingest_worker.py）。
# （原 §6-4 test_cancel_wins_before_commit / test_cancel_rejected_during_
#   committing：DELETE /api/uploads/<id> 取消端点语义。删除与发布的胜者
#   （删除方向）由下方 test_delete_after_publish_refunds_and_tombstones 覆盖。）


def test_delete_after_publish_refunds_and_tombstones(tmp_path):
    """删除与发布的胜者：ready 后删除 → deleting 立即拒读 → 结算减账。"""
    c = _client()
    uid = _user_session(c, login="d3@x.com")
    sid = publish_test_slide("del.tif", TIFF, owner_user_id=uid,
                             upload_dir=UPLOAD_DIR)
    # 配额结算前置态（上传链路入账语义归 COS 侧；此处直铺删除结算起点）
    _quota_bytes(uid, 10 * 1024 * 1024)
    _settle_used(uid, len(TIFF))
    assert c.get("/api/slides/%s/info" % sid).status_code == 200
    rd = c.delete("/api/slides/%s" % sid)
    assert rd.status_code == 200 and rd.get_json()["state"] == "deleted"
    assert not _bundle_dir(sid).exists()
    d = _desc(sid)
    assert d.asset_state == "deleted"     # tombstone 行保留
    assert c.get("/api/slides/%s/info" % sid).status_code in (403, 404)
    assert _quota(uid)["used_bytes"] == 0  # accounted_bytes 幂等减


# --------------------------------------------------------------------------- #
# §6-5 满配额 / 预约过期 → 不漏账不重复收费
# --------------------------------------------------------------------------- #
# U5（检查点 B）：旧上传端点删除，本场景已由 COS 统一链路覆盖（tests/test_cos_ingestion_kinds.py / test_ingestion_api.py / test_cos_ingest_worker.py）。
# （原 §6-5 test_quota_exceeded_no_leak：create 端点 413 配额预占拒绝——
#   端点行为。原 §6-5 test_reservation_lease_expired_settles_once_no_double_
#   charge：预占租约过期后的 commit 结算——纯 V2 commit 端点行为。）


def test_delete_then_reupload_new_id_no_inheritance(tmp_path):
    ca = _client()
    cb = _client()
    uid_a = _user_session(ca, login="f1@x.com")
    uid_b = _user_session(cb, login="f2@x.com")

    sid_a = publish_test_slide("inherit.tif", TIFF, owner_user_id=uid_a,
                               upload_dir=UPLOAD_DIR)
    # 建立全部引用面：view grant / share+claim / 标注 / run grant / Demo
    share_store.grant_slide_view(uid_b, "inherit.tif", slide_id=sid_a)
    share = share_store.create_share(
        ["inherit.tif"], 24, creator_user_id=uid_a, slide_ids=[sid_a])
    token = share["token"]
    share_store.claim_share(token, uid_b)
    # 工作台标注（token=admin：个人标注对本人可见——0056 语义）
    share_store.add_roi("admin", "inherit.tif", "L", x=1.0, y=2.0,
                        w=4.0, h=4.0, size_mm=0.5, slide_id=sid_a,
                        owner_user_id=uid_a)
    import demo_store
    demo_store.catalog_add(sid_a, display_name="demo-a")
    share_store.create_run_grant("inst-1", "inherit.tif",
                                 created_by_user_id=uid_a, slide_id=sid_a)

    rd = ca.delete("/api/slides/%s" % sid_a)
    assert rd.status_code == 200

    # 授权联动清理：view grants / share_slides / Demo / run grants
    assert _one("SELECT count(*) FROM slide_view_grants WHERE slide_id=%s",
                (sid_a,)) == 0
    assert _one("SELECT count(*) FROM share_slides WHERE slide_id=%s",
                (sid_a,)) == 0
    assert _one("SELECT count(*) FROM demo_catalog WHERE slide_id=%s",
                (sid_a,)) == 0
    assert _one("SELECT count(*) FROM run_grants WHERE slide_id=%s AND "
                "NOT revoked", (sid_a,)) == 0

    # 同名重传 → 新 ID
    sid_b = publish_test_slide("inherit.tif", TIFF, owner_user_id=uid_a,
                               upload_dir=UPLOAD_DIR)
    assert sid_b != sid_a
    # 旧授权/分享/能力不指向 B
    assert cb.get("/api/slides/%s/info" % sid_b).status_code == 403
    assert _one("SELECT count(*) FROM share_slides WHERE slide_id=%s",
                (sid_b,)) == 0
    assert _one("SELECT count(*) FROM slide_view_grants WHERE slide_id=%s",
                (sid_b,)) == 0
    assert _one("SELECT count(*) FROM demo_catalog WHERE slide_id=%s",
                (sid_b,)) == 0
    # 标注不继承：B 的标注为空；A 的标注仍按 A 的 ID 分组（证据保留）
    by_slide = share_store.annotations_by_slide()
    assert by_slide.get(sid_b) in (None, [])
    groups_a = by_slide.get(sid_a) or []
    assert sum(g["count"] for g in groups_a) == 1
    # 单切片端点按 ID 取组（同名不串）；A 已删除 → 不可读
    rr = ca.get("/api/slides/%s/annotations" % sid_b)
    assert rr.status_code == 200
    assert rr.get_json()["annotations"] == []
    assert ca.get("/api/slides/%s/annotations" % sid_a).status_code in (403, 404)


def test_annotations_same_name_id_bundle_not_crossed(tmp_path):
    """§6-11：两份同名 id_bundle 资产，标注按 ID 分组互不串。"""
    ca = _client()
    uid = _user_session(ca, login="g1@x.com")
    sid_a = publish_test_slide("twin.tif", TIFF, owner_user_id=uid,
                               upload_dir=UPLOAD_DIR)
    sid_b = publish_test_slide("twin.tif", TIFF, owner_user_id=uid,
                               upload_dir=UPLOAD_DIR)
    # 工作台标注（token=admin）分别落在两份同名资产上
    share_store.add_roi("admin", "twin.tif", "A", x=1.0, y=1.0,
                        w=2.0, h=2.0, size_mm=0.1, slide_id=sid_a,
                        owner_user_id=uid)
    share_store.add_roi("admin", "twin.tif", "B", x=2.0, y=2.0,
                        w=2.0, h=2.0, size_mm=0.2, slide_id=sid_b,
                        owner_user_id=uid)
    by_slide = share_store.annotations_by_slide()
    assert by_slide.get(sid_a) and by_slide.get(sid_b)
    labels_a = {g["label"] for g in by_slide[sid_a]}
    labels_b = {g["label"] for g in by_slide[sid_b]}
    assert labels_a == {"A"} and labels_b == {"B"}
    # 端点侧同样隔离
    ra = ca.get("/api/annotations?slide_id=%s" % sid_a).get_json()
    rb = ca.get("/api/annotations?slide_id=%s" % sid_b).get_json()
    assert {g["label"] for g in ra["annotations"]} == {"A"}
    assert {g["label"] for g in rb["annotations"]} == {"B"}


# --------------------------------------------------------------------------- #
# §6-7 删除减账幂等：重复 DELETE 不重复减
# --------------------------------------------------------------------------- #
def test_delete_refund_idempotent(tmp_path):
    c = _client()
    uid = _user_session(c, login="h1@x.com")
    _quota_bytes(uid, 10 * 1024 * 1024)
    sid = publish_test_slide("idem.tif", TIFF, owner_user_id=uid,
                             upload_dir=UPLOAD_DIR)
    _settle_used(uid, len(TIFF))
    assert _quota(uid)["used_bytes"] == len(TIFF)
    r1 = c.delete("/api/slides/%s" % sid)
    assert r1.status_code == 200
    assert _quota(uid)["used_bytes"] == 0
    # 重复 DELETE：幂等 200，不再减（GREATEST 兜底之外由 CAS 保证只减一次）
    r2 = c.delete("/api/slides/%s" % sid)
    assert r2.status_code == 200
    r3 = c.delete("/api/slides/%s" % sid)
    assert r3.status_code == 200
    assert _quota(uid)["used_bytes"] == 0
    # worker 重试语义：deleting 中断后重入（状态直改模拟清理中断）
    sid2 = publish_test_slide("idem2.tif", TIFF, owner_user_id=uid,
                              upload_dir=UPLOAD_DIR)
    _settle_used(uid, len(TIFF))
    with psycopg.connect(PG_URI, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE slides SET asset_state='deleting' "
                        "WHERE slide_id=%s", (sid2,))
    slide_storage.remove_bundle(sid2, root=UPLOAD_DIR)
    r4 = c.delete("/api/slides/%s" % sid2)
    assert r4.status_code == 200
    assert _quota(uid)["used_bytes"] == 0


# --------------------------------------------------------------------------- #
# §6-8 显示名修改不动文件与 revision
# --------------------------------------------------------------------------- #
def test_display_name_edit_no_file_no_revision_change(tmp_path):
    c = _client()
    uid = _user_session(c, login="i1@x.com")
    sid = publish_test_slide("rename.tif", TIFF, owner_user_id=uid,
                             upload_dir=UPLOAD_DIR)
    d0 = _desc(sid)
    path0 = slide_storage.resolve_descriptor_path(d0, root=UPLOAD_DIR)
    stat0 = path0.stat()
    rev0 = _desc(sid).revision
    rp = c.patch("/api/slides/%s" % sid, json={"display_name": "新名字"})
    assert rp.status_code == 200
    d1 = _desc(sid)
    assert d1.display_name == "新名字"
    assert d1.storage_relpath == d0.storage_relpath
    assert path0.stat().st_mtime_ns == stat0.st_mtime_ns  # 文件未动
    assert d1.revision == rev0                            # revision 不动
    assert _one("SELECT count(*) FROM slide_assets WHERE slide_id=%s",
                (sid,)) == 1


# --------------------------------------------------------------------------- #
# §6-9 旧端点按名 404/403（预期）；ID 端点全通；name=None 出列
# --------------------------------------------------------------------------- #
def test_id_endpoints_full_read_path_and_legacy_name_404(tmp_path):
    c = _client()
    uid = _user_session(c, login="j1@x.com")
    sid = publish_test_slide("read.tif", TIFF, owner_user_id=uid,
                             upload_dir=UPLOAD_DIR)
    # 列表出列：name=None + slide_id/original_filename/display_name/format_ext
    items = c.get("/api/slides").get_json()
    mine = [it for it in items if it.get("slide_id") == sid]
    assert len(mine) == 1
    assert mine[0]["name"] is None            # id_bundle：name=None（合同 §4）
    assert mine[0]["original_filename"] == "read.tif"
    assert mine[0]["display_name"] == "read.tif"
    assert mine[0]["format_ext"] == "tif"
    assert "storage_relpath" not in mine[0]   # 路径绝不序列化（R-20）
    # 旧按名端点找不到（新资产无 legacy_filename；403=不泄露存在性）
    assert c.get("/api/slide/read.tif/info").status_code == 403
    assert c.get("/api/slide/read.tif.dzi").status_code == 403
    # ID 端点全通
    assert c.get("/api/slides/%s/info" % sid).status_code == 200
    assert c.get("/api/slides/%s/dzi" % sid).status_code == 200
    assert c.get("/api/slides/%s/tiles/0/0_0.jpeg" % sid).status_code == 200
    assert c.get("/api/slides/%s/crop?x=0&y=0&size=8" % sid).status_code == 200
    assert c.get("/api/slides/%s/region?x=0&y=0&w=8&h=8" % sid).status_code == 200
    assert c.get("/api/slides/%s/thumbnail" % sid).status_code == 200
    # revision 来自 slide_assets（sha256 前缀），不是 mtime:size
    d = _desc(sid)
    assert d.revision == "sha256:%s" % TIFF_SHA[:16]


# --------------------------------------------------------------------------- #
# §6-10 机器通道无行兼容分支已删
# --------------------------------------------------------------------------- #
def test_machine_channel_no_row_rejected(tmp_path):
    """目录上手工放的文件（无 slides 行）：internal/plugin 解析一律拒。"""
    c = _client()
    app_mod.AUTH_ENABLED = False   # 本地免认证单租户态（P1-B2 偏差 #3 收口）
    ghost = Path(UPLOAD_DIR) / "ghost.tif"
    ghost.write_bytes(TIFF)
    with app_mod.app.test_request_context():
        # 机器通道：无行 → 404（不再文件存在即可读）
        _safe, gate, _sid, err = app_mod._internal_slide_target(None, "ghost.tif")
        assert gate is None and err is not None and err[1] == 404
        err_plugin = app_mod._plugin_resolve_slide("ghost.tif")[2]
        assert err_plugin is not None and err_plugin.status_code == 404
        # session 通道（本地免认证 owner 无 uid）：无行 → 拒（403 语义 None）
        assert app_mod._authorize_legacy_read("ghost.tif") is None
    ghost.unlink()


# --------------------------------------------------------------------------- #
# V1 原生单文件新管线（同矩阵抽验：同名并发/幂等/删除减账）
# --------------------------------------------------------------------------- #
# U5（检查点 B）：旧上传端点删除，本场景已由 COS 统一链路覆盖（tests/test_cos_ingestion_kinds.py / test_ingestion_api.py / test_cos_ingest_worker.py）。
# （原 test_v1_native_upload_publishes_id_bundle：V1 单请求端点的发布/配额/
#   同名并发行为。原 test_v1_native_invalid_content_fails_clean：V1 端点对
#   非法内容的 400 拒绝与受理前零残留——端点拒绝场景随端点删除。）
