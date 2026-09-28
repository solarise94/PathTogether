# -*- coding: utf-8 -*-
"""R7 复核（2026-09-27）复现用例——原样入仓为回归（断言不动）。

审查记录：/tmp/slide-id-review-20260927/REVIEW.md。三个反例覆盖：
  1. 待清理预约不得被 TTL/新准入回收（容量责任持续有效）；
  2. 授权终验须比对分享领取权限（grants.active 翻转须阻断 go）；
  3. clear_cleanup_pending 按列名取值（dict_row 下 row[0] 必 KeyError）。
"""
import os
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
for _p in (str(_REPO / "scripts"), str(_REPO / "tests"), str(_REPO)):
    if _p not in sys.path:
        sys.path.insert(0, _p)


from pathlib import Path
import psycopg
import pytest
import app as app_mod
import upload_guard
import upload_task_store
from test_slide_publish_pg import _isolate, _client, _user_session, _quota, TIFF
from test_slide_migration_tools import conn, world, full_apply, verifier, drill

def verify(w):
    return verifier.run_verify(upload_dir=str(w['up']), out_dir=str(w['up'].parent / 'recheck'),
        plan_path=str(w['plan']), journal_path=str(w['journal']), database_url=w['uri'])

# U5（检查点 B）：旧上传端点删除，本场景已由 COS 统一链路覆盖（tests/test_cos_ingestion_kinds.py / test_ingestion_api.py / test_cos_ingest_worker.py）。
# （原 test_cleanup_hold_survives_reservation_expiry：经 DELETE 503 注入清理
#   失败后断言 pending 预约不被 TTL/新准入回收——入口与断言均属旧取消端点
#   的 cleanup-retry 行为；不变量的存活通道版由 test_capacity_lifecycle_cos.py
#   与 test_reconcile_upload_capacity.py::test_terminal_pending_legacy_stock_
#   bound_not_reclaimed 覆盖。）

def test_clear_cleanup_returns_reservation_and_removes_row():
    upload_task_store.record_cleanup_pending('upt-review', 'rsv-review', error='failure')
    assert upload_task_store.clear_cleanup_pending('upt-review') == 'rsv-review'
    assert upload_task_store.get_cleanup_pending('upt-review') is None

def test_claimed_share_revocation_blocks_go(world):
    full_apply(world)
    assert verify(world)['go_no_go'] == 'go'
    with world['conn'].cursor() as cur:
        cur.execute('UPDATE grants SET active=false WHERE token=%s', (drill.TOK_SHARE,))
        assert cur.rowcount > 0
    world['conn'].commit()
    result = verify(world)
    assert result['go_no_go'] != 'go', result['authorization_diff']


# --------------------------------------------------------------------------- #
# 编排方补充（R7 修复要求：「验证用户重复 DELETE 和管理员清理两条完整
# 收尾路径」）——非审查方复现，断言编排方口径。
# --------------------------------------------------------------------------- #
# U5（检查点 B）：旧上传端点删除，本场景已由 COS 统一链路覆盖（tests/test_cos_ingestion_kinds.py / test_ingestion_api.py / test_cos_ingest_worker.py）。
# （原 test_repeated_delete_completes_release_after_cleanup_recovery：核心
#   即旧 DELETE 取消端点的「清理失败 503 → 恢复后重复 DELETE 收口」重试
#   路径本身；同语义的存活收口原语 confirm_cleanup_and_release 单事务
#   行为由 test_slide_id_review_r12/r13 覆盖。）


def test_admin_cleanup_releases_held_reservation(pg_uri):
    """完整收尾路径②（管理员确认清理）：pending 持有的过期预约不被 TTL/
    新准入回收；管理员 staging-residue 确认清理后释放并消除 pending。

    U5（检查点 B）：入口态改由服务级构造（create_task + 绑定预约 +
    物理残留 + record_cleanup_pending，终态经 cancel_task 落定——与旧
    DELETE 取消路径同状态）；原「清理失败 503」注入随旧上传端点删除，
    以下收尾断言原样保留。"""
    import user_store
    from _pt_helpers import csrf_client
    c = _client()
    uid = _user_session(c, login="admin-path@example.com")
    task = upload_task_store.create_task(uid, "admin-path.tif",
                                         "admin-path.tif", len(TIFF), 32)
    rid = upload_guard.reserve_upload(
        uid, len(TIFF), holder_kind="upload_task",
        holder_id=task["upload_id"], purpose="upload")["reservation_id"]
    staged = Path(app_mod.UPLOAD_DIR) / ".staging" / task["upload_id"] / "transfer"
    staged.mkdir(parents=True)
    (staged / "data").write_bytes(TIFF)
    upload_task_store.cancel_task(task["upload_id"])
    upload_task_store.record_cleanup_pending(task["upload_id"], rid,
                                             error="cleanup blocked")
    held = _quota(uid)["reserved_bytes"]
    pending = upload_task_store.get_cleanup_pending(task["upload_id"])
    assert pending and pending["reservation_id"]

    # 预约过期 + 同用户新准入：待清理责任不被回收（reserved 只增新任务）
    with psycopg.connect(pg_uri, autocommit=True) as db:
        db.execute("UPDATE upload_reservations SET expires_at="
                   "now()-interval '1 second' WHERE reservation_id=%s",
                   (pending["reservation_id"],))
    upload_guard.reserve_upload(uid, len(TIFF))
    assert _quota(uid)["reserved_bytes"] == held + len(TIFF)

    # 管理员确认清理（真实 owner 用户会话直调端点）
    admin_user = user_store.create_user(
        "admin-path-owner@example.com", "ownerpass123456", role="owner")
    admin = csrf_client(app_mod.app.test_client())
    with admin.session_transaction() as s:
        s["auth_user"] = True
        s["user_id"] = admin_user["user_id"]
        s["role"] = "owner"
        s["auth_version"] = admin_user.get("auth_version", 1)
    r = admin.delete("/api/admin/v1/slides/staging-residue",
                     json={"task_id": task["upload_id"]})
    assert r.status_code == 200, r.get_data(as_text=True)
    assert r.get_json()["released_reservation"] is True
    assert not (Path(app_mod.UPLOAD_DIR) / ".staging" /
                task["upload_id"]).exists()
    assert upload_task_store.get_cleanup_pending(task["upload_id"]) is None
    assert _quota(uid)["reserved_bytes"] == len(TIFF)  # 只剩新任务预占
