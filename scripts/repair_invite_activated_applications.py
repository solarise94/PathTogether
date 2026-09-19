#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""R7 历史修复：邀请码激活后仍滞留 pending 的测试申请收口（dry-run/apply）。

背景（docs/service-review-fix-plan-20260919.md §3）：修复前的
``registration_store.activate_registered_user`` 不更新 test_applications，
留下「users.active + activation_source='invite' + application 仍 pending」
的滞留行——管理员再审批会报「账号已激活或不可用」。应用层修复只覆盖
新激活；本工具显式收口历史滞留行。

范围（fail-closed，绝不凭猜测改写）：

  - 修复目标（唯一会改写的集合）：``users.activation_state='active'`` 且
    ``users.activation_source='invite'`` 且 ``test_applications.status=
    'pending'`` → ``status='activated_by_invite'``。reviewed_by/reviewed_at
    保持 NULL（邀请码激活不是人工审批，不伪造审核人）；激活时间以
    users.activation_updated_at 为准，不在本工具中另记。
  - 其他 active+pending（activation_source='admin' 等）：**只报告，不改写**
    ——那可能是另一类数据问题，须人工核对后单独处置。
  - 不补发额度、不补发/重发任何邮件、不改邀请码、不改 users、不改
    reviewed_by/reviewed_at。

用法（连接走 pg_store，DATABASE_URL 或 PGHOST/... 与应用同源）::

    python3 scripts/repair_invite_activated_applications.py            # dry-run
    python3 scripts/repair_invite_activated_applications.py --apply    # 执行

幂等：逐行 CAS（``WHERE status='pending'``）+ 收口后目标集合恒空，
重复 ``--apply`` 第二遍 0 行改动、不写审计。apply 单事务、锁序与
activate/review 同款（provisioning advisory → user → application），
与在线激活/审批并发无死锁；只为实际改写的行写真实修复审计
（action=``test_application.repair_invite_activated``，actor_user_id=NULL
=系统修复，detail 记 repair 批次说明，不含任何敏感信息）。
"""

import argparse
import sys
import time
from pathlib import Path

# 把仓库根加入 sys.path 以便 import 应用存储模块（脚本可从任意 cwd 运行）。
_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import psycopg  # noqa: E402  （sys.path 已含仓库根）

import pg_store  # noqa: E402
import registration_store  # noqa: E402  （复用 _insert_audit / mask_login_id）
import spend_store  # noqa: E402  （复用 provisioning advisory 锁，锁序一致）

AUDIT_ACTION = "test_application.repair_invite_activated"


def _connect():
    """建连接并设 dict_row（先跑 ensure_schema，再切 dict_row）。"""
    conn = pg_store.connect()
    pg_store.ensure_schema(conn)
    conn.row_factory = psycopg.rows.dict_row
    return conn


def _scan(cur):
    """扫描全部 active+pending 行，按激活来源分成（修复目标, 仅报告）两组。

    只读 SELECT，不做任何改写；输出只含 user_id 与掩码登录账号。
    """
    cur.execute(
        "SELECT t.user_id, u.login_id, u.email_normalized, "
        "u.activation_source "
        "FROM test_applications t JOIN users u ON u.user_id = t.user_id "
        "WHERE u.activation_state = 'active' AND t.status = 'pending' "
        "ORDER BY t.user_id")
    invite_rows, other_rows = [], []
    for row in cur.fetchall():
        if (row["activation_source"] or "") == "invite":
            invite_rows.append(row)
        else:
            other_rows.append(row)
    return invite_rows, other_rows


def _mask(row):
    return registration_store.mask_login_id(
        row["email_normalized"] or row["login_id"] or "")


def _report(cur, apply_mode):
    invite_rows, other_rows = _scan(cur)
    sys.stdout.write("修复目标（active + activation_source=invite + "
                     "application pending）：%d 行\n" % len(invite_rows))
    for row in invite_rows:
        sys.stdout.write("  - user_id=%s login_id=%s（将收口为 "
                         "activated_by_invite）\n" % (row["user_id"], _mask(row)))
    if other_rows:
        sys.stdout.write(
            "注意：另有 %d 行 active+pending 但激活来源非 invite（不动，"
            "仅报告，请人工核对）\n" % len(other_rows))
        for row in other_rows:
            sys.stdout.write("  - user_id=%s login_id=%s（activation_source=%s）\n"
                             % (row["user_id"], _mask(row),
                                row["activation_source"] or "NULL"))
    if not apply_mode:
        sys.stdout.write(
            "确认后执行：python3 scripts/repair_invite_activated_applications.py"
            " --apply\n")
    return invite_rows


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="repair_invite_activated_applications",
        description="把「邀请码激活后仍滞留 pending」的测试申请收口为 "
                    "activated_by_invite（R7 历史修复；默认 dry-run，"
                    "--apply 才写；不补发额度/邮件）")
    parser.add_argument(
        "--apply", action="store_true",
        help="执行写入（缺省 dry-run：只输出待修复计数，不写库）")
    args = parser.parse_args(argv)

    mode = "apply" if args.apply else "dry-run"
    sys.stdout.write("模式：%s\n" % mode)
    conn = _connect()
    try:
        if not args.apply:
            with conn.cursor() as cur:
                _report(cur, apply_mode=False)
            return 0

        # apply：单事务；锁序与 activate/review 一致（provisioning advisory
        # → user 行 → application 行），并发在线激活/审批无死锁。
        started = time.time()
        with pg_store.transaction(conn) as txn:
            with txn.cursor() as cur:
                spend_store.acquire_user_provisioning_lock_tx(cur)
                invite_rows, other_rows = _scan(cur)
                repaired = 0
                for row in invite_rows:
                    # CAS：只命中仍为 pending 的行（扫描与写入同事务且已持
                    # 行锁，此处守卫是幂等重放的最后防线）。
                    cur.execute(
                        "UPDATE test_applications "
                        "SET status='activated_by_invite' "
                        "WHERE user_id=%s AND status='pending'",
                        (row["user_id"],))
                    if (cur.rowcount or 0) != 1:
                        continue
                    repaired += 1
                    # 真实修复审计（同事务；actor=NULL=系统修复；detail 不含
                    # 敏感信息，不含 reviewed_by——收口不是人工审批）
                    registration_store._insert_audit(
                        cur, AUDIT_ACTION, None, "user", row["user_id"],
                        {"repair": "invite_activated_pending_application",
                         "final_status": "activated_by_invite",
                         "reviewed_by": None})
        sys.stdout.write(
            "完成：收口 %d 行（耗时 %.2fs；执行时间 %s）\n"
            % (repaired, time.time() - started,
               time.strftime("%Y-%m-%dT%H:%M:%S%z")))
        if other_rows:
            sys.stdout.write(
                "另有 %d 行 active+pending（来源非 invite）未改动，见 dry-run "
                "口径，请人工核对。\n" % len(other_rows))
        sys.stdout.write("重复执行幂等：再次 --apply 应为 0 行。\n")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
