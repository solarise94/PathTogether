# -*- coding: utf-8 -*-
"""R8 复核（2026-09-27）复现处置——入仓为回归。

审查记录：/tmp/slide-id-review-r8/REVIEW.md。两项问题：
  1. P1 待清理登记与过期回收的并发漏账（SUM 与 UPDATE 之间登记提交，
     减账用了漂移的先前聚合）→ 修复＝统一配额行锁 + UPDATE RETURNING
     实际转换减账 + 回收先赢时登记侧重激活。锁协议使原同步注入不可交错
     （审查记录已预告：注入须改为验证两种串行胜者，断言语义不变）；
  2. P2 多分享未变被误报漂移（冻结侧摘要序 vs 重读侧原文序）→ 修复＝
     两侧同键规范化排序；本测试为审查方原样（断言不动）。
"""
import hashlib
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import _bootstrap  # noqa: E402,F401

import psycopg  # noqa: E402
import pytest  # noqa: E402

import upload_guard  # noqa: E402
import upload_task_store  # noqa: E402
import user_store  # noqa: E402
from test_slide_migration_tools import (  # noqa: E402
    _PLAN_ENV, AUDIT, conn, full_apply, planner, verifier, world)

PG_URI = os.environ["DATABASE_URL"]


# --------------------------------------------------------------------------- #
# P1（0072 生命周期改写，plan §D 注记——被替代的状态机合同：R8 时代的
# 「回收先赢 → 登记侧重激活补账」已拆除。任务持有的容量**绑定后从不被
# TTL 回收**，两种串行胜者收敛到同一终态：原 rid 保持 reserved /
# 账本 150 / pending 在——「责任从未释放」取代「漏账后补账」。）
# --------------------------------------------------------------------------- #
def _scenario(winner):
    uid = user_store.create_user(
        "race-r8-%s@example.com" % winner, "pass1234pass1234",
        role="user")["user_id"]
    task_id = "upt-r8-%s" % winner
    rid = upload_guard.reserve_upload(
        uid, 100, holder_kind="upload_task", holder_id=task_id,
        purpose="upload")["reservation_id"]
    with psycopg.connect(PG_URI, autocommit=True) as db:
        db.execute("UPDATE upload_reservations SET expires_at="
                   "now()-interval '1 second' WHERE reservation_id=%s",
                   (rid,))
    if winner == "pending":
        # 登记先（只落 pending 行，零财务副作用）→ 随后准入：绑定预约
        # 不参加回收（账本不减）。
        upload_task_store.record_cleanup_pending(task_id, rid, error="IO")
        upload_guard.reserve_upload(uid, 50)
    else:
        # 准入先：绑定预约不被回收（账本 100+50=150，不再先减后补）→
        # 登记随后只落 pending 行。
        upload_guard.reserve_upload(uid, 50)
        assert upload_guard.get_quota_row(uid)["reserved_bytes"] == 150
        upload_task_store.record_cleanup_pending(task_id, rid, error="IO")
    with psycopg.connect(PG_URI) as db:
        state = db.execute(
            "SELECT state FROM upload_reservations "
            "WHERE reservation_id=%s", (rid,)).fetchone()[0]
    assert state == "reserved"
    assert upload_guard.get_quota_row(uid)["reserved_bytes"] == 150
    assert upload_task_store.get_cleanup_pending(task_id) is not None


def test_pending_registration_wins_serialization():
    """串行胜者①：登记先——绑定责任不被回收，账本 100+50。"""
    _scenario("pending")


def test_reclaim_wins_then_registration_reactivates():
    """串行胜者②：准入先——同一终态（绑定责任从未离开账本，无需重激活）。"""
    _scenario("admission")


# --------------------------------------------------------------------------- #
# P2（审查方原样，断言不动）
# --------------------------------------------------------------------------- #
def test_multiple_unchanged_shares_do_not_report_drift(world):
    sid = 'sld_drill_svs01'
    tokens = ['review-share-0', 'review-share-1', 'review-share-2']
    assert sorted(tokens) != sorted(tokens, key=lambda t: hashlib.sha256(t.encode()).hexdigest())
    with world['conn'].cursor() as cur:
        for token in tokens:
            cur.execute("INSERT INTO shares(token,slides) VALUES (%s,'[\"specimen.svs\"]'::jsonb)", (token,))
            cur.execute('INSERT INTO share_slides(token,slide_id) VALUES (%s,%s)', (token, sid))
            cur.execute("INSERT INTO grants(id,token,user_id,active) VALUES (%s,%s,'usr_bob',true)", ('g-' + token, token))
    world['conn'].commit()
    assert AUDIT.main(['--database-url', world['uri'], '--upload-dir', str(world['up']),
                       '--out-dir', str(world['audit_out']), '--mode', 'frozen']) == 0
    assert planner.main(['--inventory', str(world['audit_out'] / 'inventory.jsonl'),
        '--issues', str(world['audit_out'] / 'issues.jsonl'), '--env', _PLAN_ENV, '--out', str(world['plan'])]) == 0
    world['digest'] = hashlib.sha256(world['plan'].read_bytes()).hexdigest()
    full_apply(world)
    result = verifier.run_verify(upload_dir=str(world['up']), out_dir=str(world['up'].parent / 'verified'),
        plan_path=str(world['plan']), journal_path=str(world['journal']), database_url=world['uri'])
    assert result['go_no_go'] == 'go', result['authorization_diff']
