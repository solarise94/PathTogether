#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""上传容量存量核账与绑定（0072 生命周期 §6-E）。

模型切换（绑定 + 不回收任务持有容量）后，旧版本产生的存量行需要一次
核账：一致项补绑定；异常项停止执行并审计实际字节；需要继续持有责任的
缺失项可经 ``--reattach`` 建 origin='reconcile' 预约并原子绑定（不改
历史 released 行、不调高用户额度、不计一次用户上传）。

用法（PathTogether 根目录）：

    # 只读报告（默认，不动数据）
    python3 scripts/reconcile_upload_capacity.py \
        --database-url "$DATABASE_URL" --upload-dir "$UPLOAD_DIR"

    # 维护窗口（停写）内应用：绑定一致项；异常项终止进清理编排
    python3 scripts/reconcile_upload_capacity.py --apply ...

    # 异常项需要继续持有责任时：按审计字节补建核账预约并绑定
    python3 scripts/reconcile_upload_capacity.py --apply --reattach ...

约束：幂等（重跑对已处理项 no-op）；逐项结果输出；报告不含任何秘密；
超额（used+reserved>quota）只如实补记与列出，不伪装成新上传获准；
未知路径不自动删除。生产执行需另行批准（与迁移 runbook 同门禁）。
"""
from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import psycopg
import psycopg.rows

import pg_store
import upload_guard

#: 活跃（未结算、可能仍持容量责任）任务状态集
_ACTIVE_UPLOAD_TASK_STATES = ("active", "committing")
_ACTIVE_INGESTION_STATES = ("preparing", "uploading", "completing", "queued",
                            "downloading", "validating")


def _connect(database_url=None):
    conn = psycopg.connect(database_url or os.environ.get("DATABASE_URL"))
    conn.row_factory = psycopg.rows.dict_row
    return conn


def _staging_bytes(upload_dir, task_id):
    """任务暂存树实际字节数（审计输入；树缺失 = 0）。未知路径不删除。"""
    import slide_storage
    try:
        base = slide_storage.staging_task_dir(task_id, root=upload_dir)
    except ValueError:
        return 0
    if not base.is_dir():
        return 0
    total = 0
    for root, _dirs, files in os.walk(base):
        for name in files:
            try:
                total += os.path.getsize(os.path.join(root, name))
            except OSError:
                continue
    return total


def collect(cur, upload_dir):
    """只读扫描：返回 (items, over_quota, pending_work)。

    items：需要裁决的「活跃任务 → 预约」绑定状态记录；pending_work：
    终态任务的清理责任存量（补齐清理工作，不释放）。"""
    items = []
    cur.execute(
        "SELECT t.upload_id, t.owner_user_id, t.state, t.reservation_id AS rid,"
        "       r.state AS rstate, r.user_id AS ruser, r.reserved_bytes,"
        "       r.holder_kind, r.holder_id"
        " FROM upload_tasks t"
        " LEFT JOIN upload_reservations r ON r.reservation_id ="
        "     t.reservation_id"
        " WHERE t.state = ANY(%s)", (list(_ACTIVE_UPLOAD_TASK_STATES),))
    for row in cur.fetchall():
        items.append(("upload_task", dict(row)))

    cur.execute(
        "SELECT j.job_id, j.owner_user_id, j.owner_role, j.state,"
        "       j.local_reservation_id AS rid, j.declared_size,"
        "       r.state AS rstate, r.user_id AS ruser, r.reserved_bytes,"
        "       r.holder_kind, r.holder_id"
        " FROM ingestion_jobs j"
        " LEFT JOIN upload_reservations r ON r.reservation_id ="
        "     j.local_reservation_id"
        " WHERE j.state = ANY(%s) AND j.owner_role='user'"
        "   AND COALESCE(j.owner_user_id,'') <> ''",
        (list(_ACTIVE_INGESTION_STATES),))
    for row in cur.fetchall():
        items.append(("ingestion_job", dict(row)))

    cur.execute(
        "SELECT b.id AS batch_id, b.owner_user_id, b.state,"
        "       b.quota_reservation_id AS rid, b.total_bytes,"
        "       r.state AS rstate, r.user_id AS ruser, r.reserved_bytes,"
        "       r.holder_kind, r.holder_id"
        " FROM baidu_import_batches b"
        " LEFT JOIN upload_reservations r ON r.reservation_id ="
        "     b.quota_reservation_id"
        " WHERE b.state IN ('queued','running')")
    for row in cur.fetchall():
        items.append(("baidu_batch", dict(row)))

    cur.execute(
        "SELECT user_id, quota_bytes, used_bytes, reserved_bytes,"
        "       used_bytes + reserved_bytes - quota_bytes AS over"
        " FROM upload_user_quotas WHERE used_bytes + reserved_bytes >"
        "     quota_bytes")
    over_quota = [dict(r) for r in cur.fetchall()]

    pending = {"upload_cleanup_pending": None, "ingestion_local_cleanup": None}
    cur.execute("SELECT upload_id, reservation_id, attempts FROM "
                "upload_cleanup_pending ORDER BY upload_id")
    pending["upload_cleanup_pending"] = [dict(r) for r in cur.fetchall()]
    cur.execute(
        "SELECT job_id, local_cleanup_status, local_cleanup_attempts FROM "
        "ingestion_jobs WHERE local_cleanup_status IN ('pending','failed') "
        "ORDER BY job_id")
    pending["ingestion_local_cleanup"] = [dict(r) for r in cur.fetchall()]

    for kind, row in items:
        row["staging_bytes"] = _staging_bytes(
            upload_dir, row.get("upload_id") or row.get("job_id")
            or row.get("batch_id"))
    return items, over_quota, pending


#: (kind, id) → 预期 purpose
_PURPOSE = {"upload_task": "upload", "ingestion_job": "ingest_local",
            "baidu_batch": "baidu_import"}
#: (kind, id) 行内键名
_HOLDER_ID_KEY = {"upload_task": "upload_id", "ingestion_job": "job_id",
                  "baidu_batch": "batch_id"}


def classify(kind, row):
    """单条记录的裁决：bind / ok / mismatch_owner / mismatch_holder /
    missing / released / consumed。"""
    if not row.get("rid"):
        return "missing"
    if row.get("rstate") is None:
        return "missing"
    if row["rstate"] != "reserved":
        return row["rstate"]
    holder_id = row[_HOLDER_ID_KEY[kind]]
    if (row.get("holder_id") or None) == holder_id and \
            (row.get("holder_kind") or None) == kind:
        if row.get("ruser") == row.get("owner_user_id"):
            return "ok" if row.get("holder_id") else "bind"
        return "mismatch_owner"
    if row.get("holder_id"):
        return "mismatch_holder"
    if row.get("ruser") != row.get("owner_user_id"):
        return "mismatch_owner"
    return "bind"


def apply_item(cur, kind, row, verdict, reattach):
    """--apply 的单条处理（幂等）。返回动作说明。"""
    holder_id = row[_HOLDER_ID_KEY[kind]]
    purpose = _PURPOSE[kind]
    if verdict == "bind":
        upload_guard.bind_reservation_locked(
            cur, row["rid"], kind, holder_id, purpose)
        return "bound:%s" % holder_id
    if verdict in ("missing", "released", "consumed"):
        # 停止执行 + 审计字节（默认）：任务转失败/清理编排，不动配额。
        if kind == "upload_task":
            cur.execute(
                "UPDATE upload_tasks SET state='failed', updated_at=now()"
                " WHERE upload_id=%s AND state = ANY(%s)",
                (holder_id, list(_ACTIVE_UPLOAD_TASK_STATES)))
            cur.execute(
                "INSERT INTO upload_cleanup_pending (upload_id,"
                " reservation_id, attempts, last_error) VALUES (%s,%s,1,%s)"
                " ON CONFLICT (upload_id) DO UPDATE SET attempts ="
                " upload_cleanup_pending.attempts + 1, last_error="
                " EXCLUDED.last_error, updated_at=now()",
                (holder_id, row.get("rid"),
                 "reconcile: reservation %s" % verdict))
        elif kind == "ingestion_job":
            cur.execute(
                "UPDATE ingestion_jobs SET state='failed', fail_code="
                "'local_reservation_invalid', terminal_at=now(),"
                " cleanup_status='pending', local_cleanup_status='pending',"
                " updated_at=now() WHERE job_id=%s AND state = ANY(%s)",
                (holder_id, list(_ACTIVE_INGESTION_STATES)))
        else:  # baidu_batch：闭班路径自身会撞 ReservationInvalid——只报告
            return "report-only:%s" % verdict
        if reattach and int(row.get("staging_bytes") or 0) > 0 \
                and verdict == "missing":
            # 按审计字节补建核账责任（不改历史行；不计每小时准入数）
            res = upload_guard.reserve_upload_locked(
                cur, row["owner_user_id"], int(row["staging_bytes"]),
                holder_kind=kind, holder_id=holder_id, purpose=purpose,
                origin="reconcile")
            if kind == "upload_task":
                cur.execute("UPDATE upload_tasks SET reservation_id=%s,"
                            " state='active', updated_at=now()"
                            " WHERE upload_id=%s", (res["reservation_id"],
                                                    holder_id))
                cur.execute("DELETE FROM upload_cleanup_pending"
                            " WHERE upload_id=%s", (holder_id,))
            elif kind == "ingestion_job":
                cur.execute("UPDATE ingestion_jobs SET state='uploading',"
                            " local_reservation_id=%s, fail_code=NULL,"
                            " terminal_at=NULL, updated_at=now()"
                            " WHERE job_id=%s", (res["reservation_id"],
                                                  holder_id))
            return "reattached:%d bytes" % int(row["staging_bytes"])
        return "stopped:%s(bytes=%d)" % (verdict,
                                          int(row.get("staging_bytes") or 0))
    if verdict in ("mismatch_owner", "mismatch_holder"):
        # 不变量破坏：不自动修正（不扣第二次、不转移归属）——阻断人工核对
        return "blocked:%s" % verdict
    return "noop"


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--database-url", default=None)
    ap.add_argument("--upload-dir", default=os.environ.get("UPLOAD_DIR"))
    ap.add_argument("--apply", action="store_true",
                    help="维护窗口内应用（默认只读报告）")
    ap.add_argument("--reattach", action="store_true",
                    help="缺失责任的活跃任务按审计字节补建核账预约并绑定"
                         "（仅在 --apply 下生效）")
    ap.add_argument("--json-out", default=None, help="报告落盘路径（可选）")
    args = ap.parse_args(argv)

    if args.upload_dir is None:
        print("--upload-dir 或 UPLOAD_DIR 必须提供（暂存字节审计输入）",
              file=sys.stderr)
        return 2
    conn = _connect(args.database_url)
    report = {"mode": "apply" if args.apply else "dry-run", "items": [],
              "over_quota": [], "pending_work": None}
    try:
        if args.apply:
            with pg_store.transaction(conn):
                with conn.cursor() as cur:
                    items, over, pending = collect(cur, args.upload_dir)
                    for kind, row in items:
                        verdict = classify(kind, row)
                        if verdict == "ok":
                            continue
                        action = apply_item(cur, kind, row, verdict,
                                            args.reattach)
                        report["items"].append(
                            {"kind": kind,
                             "id": row[_HOLDER_ID_KEY[kind]],
                             "verdict": verdict, "action": action})
                    report["over_quota"] = over
                    report["pending_work"] = pending
        else:
            with conn.cursor() as cur:
                items, over, pending = collect(cur, args.upload_dir)
                for kind, row in items:
                    verdict = classify(kind, row)
                    if verdict == "ok":
                        continue
                    report["items"].append({
                        "kind": kind, "id": row[_HOLDER_ID_KEY[kind]],
                        "verdict": verdict,
                        "reservation": row.get("rid"),
                        "staging_bytes": row.get("staging_bytes")})
                report["over_quota"] = over
                report["pending_work"] = pending
    finally:
        conn.close()

    print(json.dumps(report, ensure_ascii=False, indent=2,
                     default=str))
    if args.json_out:
        with open(args.json_out, "w", encoding="utf-8") as fh:
            json.dump(report, fh, ensure_ascii=False, indent=2,
                      default=str)
    # 异常项（mismatch_* / blocked）存在时以非零码提示（dry-run 亦然）
    if any(i["verdict"].startswith("mismatch") for i in report["items"]):
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(main())
