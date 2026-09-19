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

schema 前置（fail-closed，2026-09-19 修复）：本工具**绝不隐式迁移**——
dry-run 与 --apply 都不调用 ``pg_store.ensure_schema`` 或任何等价物；缺少
必要 schema（users/test_applications/audit_events 表或所需列缺失，apply 另
要求 test_applications.status 约束已支持 activated_by_invite）时立即报错
退出（非零码），提示先由部署流程应用迁移。dry-run 连接额外设为会话级只读
（``SET default_transaction_read_only = on``），任何写都被数据库拒绝——
「dry-run 不写库」由数据库层保证，不靠脚本自觉。

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

#: dry-run / apply 共同的必需表与列（脚本 SQL 实际用到的最小集）。
_REQUIRED_COLUMNS = {
    "users": ("user_id", "login_id", "email_normalized",
              "activation_state", "activation_source"),
    "test_applications": ("user_id", "status"),
    "audit_events": ("event_id", "actor_user_id", "actor_role", "action",
                     "target_type", "target_id", "slide", "detail"),
}


class SchemaMissingError(RuntimeError):
    """数据库 schema 不满足本工具前置条件（缺表/缺列/缺 apply 所需约束）。"""


def _connect(readonly=False):
    """建连接并设 dict_row（**绝不隐式迁移**：不调用 ensure_schema）。

    readonly=True（dry-run）：会话级 ``default_transaction_read_only = on``
    ——本连接上后续所有事务均为 READ ONLY，任何写（含意外混入的 DDL/DML）
    都在数据库层被拒绝，dry-run「不写库」不靠脚本自觉。
    """
    conn = pg_store.connect()
    conn.row_factory = psycopg.rows.dict_row
    if readonly:
        with conn.cursor() as cur:
            cur.execute("SET default_transaction_read_only = on")
        conn.commit()
    return conn


def _require_schema(cur):
    """显式校验必需表/列已存在（fail-closed：缺 schema 报错退出，提示先应用
    迁移；绝不静默补建、绝不顺手执行迁移）。"""
    cur.execute(
        "SELECT table_name, column_name FROM information_schema.columns "
        "WHERE table_schema = current_schema()")
    have = {}
    for row in cur.fetchall():
        have.setdefault(row["table_name"], set()).add(row["column_name"])
    problems = []
    for table, columns in _REQUIRED_COLUMNS.items():
        cols = have.get(table)
        if cols is None:
            problems.append("表 %s 不存在" % table)
            continue
        missing_cols = [c for c in columns if c not in cols]
        if missing_cols:
            problems.append("表 %s 缺列：%s" % (table, ", ".join(missing_cols)))
    if problems:
        raise SchemaMissingError("；".join(problems))


def _require_invite_terminal_status(cur):
    """apply 前置：test_applications.status 约束必须已支持 activated_by_invite
    （迁移 0055）；否则 UPDATE 会撞 CHECK 约束——显式报错优于中途回滚。"""
    cur.execute(
        "SELECT count(*)::int AS n FROM pg_constraint "
        "WHERE conrelid = 'test_applications'::regclass AND contype = 'c' "
        "AND pg_get_constraintdef(oid) LIKE '%activated_by_invite%'")
    if (cur.fetchone() or {}).get("n", 0) < 1:
        raise SchemaMissingError(
            "test_applications.status 约束不支持 activated_by_invite"
            "（未应用激活终态迁移）")


def _schema_fail(exc):
    """缺 schema 的统一出口：中文错误（stderr）+ 非零码，绝不静默修复。"""
    sys.stderr.write(
        "错误：数据库 schema 不满足本工具前置条件：%s\n"
        "请先应用数据库迁移（部署迁移流程 / pg_store.ensure_schema）后再"
        "运行本工具；本工具绝不隐式执行迁移。\n" % exc)
    return 2


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
    # dry-run 连接只读（数据库层拒绝任何写）；apply 连接读写但开工前同样
    # 显式校验 schema。两条路径都不做隐式迁移。
    conn = _connect(readonly=not args.apply)
    try:
        try:
            with conn.cursor() as cur:
                _require_schema(cur)
                if args.apply:
                    _require_invite_terminal_status(cur)
            conn.rollback()  # 校验事务收口（只读 SELECT，不留事务残留）
        except SchemaMissingError as exc:
            conn.rollback()
            return _schema_fail(exc)

        if not args.apply:
            # 只读连接 + 只读事务：_report 只发 SELECT，任何写都会被数据库
            # 拒绝（cannot execute ... in a read-only transaction）。
            with conn.cursor() as cur:
                cur.execute("SET TRANSACTION READ ONLY")
                _report(cur, apply_mode=False)
            conn.rollback()
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
