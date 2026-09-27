#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""上传容量存量核账与修复（0072/0073；R12 §3.4/§3.5/§4 完整合同）。

模型：只补**容量责任**，不恢复执行——异常任务先停止（failed/cancelled +
清理 pending），按冻结证据补记责任；本轮不实现损坏任务自动续传。
正常、绑定一致的活跃任务不改状态；配额豁免身份（owner/本地模式）按身份
合同显式识别（预约为空 ≠ 异常）；普通用户不能靠 rid 为空获得豁免。

工作流：
  1. 在线 dry-run（默认）：只读预审报告，不是可应用计划；
  2. 维护窗口（停写 + 停旧 API/worker/重试器/子进程 + 备份）内
     ``--plan-out plan.json``：带 schema/工具版本、数据根身份、DB 前态与
     文件证据（字节/文件数；硬链接按 inode 去重）的冻结计划。数据库
     口令/邮件配置/COS 密钥/分享 token 不进入证据；
  3. ``--apply --plan plan.json``：全量预检（计划自洽、DB 前态、文件
     证据）——发现一项无法解释先 no-go（退出 3），不边发现边提交；随后
     单事务应用全部动作并写回执（0073 action_key 幂等）；
  4. 应用后重新 collect/verify，输出**应用后**的 over_quota/pending/
     阻断项。

退出码：0=检查完整且无未解释责任（已补记可解释的超额可 0，但列明并注明
「继续禁止新准入」）；2=参数/环境错误（含旧 --reattach）；3=证据缺失/
漂移/mismatch/未处理责任。预约缺失但仍有未补记残留不得返回 0。

边界：核账不删除任何本地/远端数据、不调清理 worker、不改额度
（quota_bytes 不变）、不自动扣第二次 used；consumed 未解释/跨 owner/
holder/purpose 错/共享预算归因不明 → 阻断人工核对。生产执行另行批准。
"""
from __future__ import annotations

import argparse
import datetime as _dt
import hashlib
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import psycopg
import psycopg.rows

import pg_store
import slide_storage
import upload_guard

SCHEMA_VERSION = 1
TOOL_VERSION = "r12.1"

_ACTIVE_UPLOAD_TASK_STATES = ("active", "committing")
_ACTIVE_INGESTION_STATES = ("preparing", "uploading", "completing", "queued",
                            "downloading", "validating")
_PURPOSE = {"upload_task": "upload", "ingestion_job": "ingest_local",
            "baidu_batch": "baidu_import"}
_HOLDER_ID_KEY = {"upload_task": "upload_id", "ingestion_job": "job_id",
                  "baidu_batch": "batch_id"}


class EvidenceError(Exception):
    """证据不完整/漂移——no-go（不能低报为 0 或忽略）。"""


def _connect(database_url=None):
    conn = psycopg.connect(database_url or os.environ.get("DATABASE_URL"))
    conn.row_factory = psycopg.rows.dict_row
    return conn


# --------------------------------------------------------------------------- #
# 文件证据（§4.1：清单化 + 硬链接计量 + 越界拒绝；错误=no-go 不是 0）
# --------------------------------------------------------------------------- #
def scan_task_tree(upload_dir, task_id):
    """任务暂存树计量：(文件数, 总字节)。同 (st_dev, st_ino) 硬链接只计
    一次（已计实占的硬链接源不重复补账）。

    拒绝（EvidenceError）：路径非目录/符号链接（含目录成员）/无法枚举或
    读取。树不存在 = (0, 0)。文件消失/成员变化由冻结证据比对发现。"""
    base = slide_storage.staging_task_dir(str(task_id), root=upload_dir)
    if not os.path.exists(base):
        return 0, 0
    if base.is_symlink() or not base.is_dir():
        raise EvidenceError("任务暂存路径不是目录：%s" % base)
    nfiles, seen = 0, set()
    total = 0
    for root, dirs, names in os.walk(base, followlinks=False):
        dirs[:] = sorted(d for d in dirs
                         if not os.path.islink(os.path.join(root, d)))
        for name in sorted(names):
            p = os.path.join(root, name)
            if os.path.islink(p):
                raise EvidenceError("暂存树含符号链接（拒绝）：%s" % p)
            try:
                st = os.stat(p)
            except OSError as exc:
                raise EvidenceError("暂存成员无法读取：%s（%s）" % (p, exc))
            key = (st.st_dev, st.st_ino)
            if key in seen:
                continue
            seen.add(key)
            total += int(st.st_size)
            nfiles += 1
    return nfiles, total


# --------------------------------------------------------------------------- #
# 扫描集合（§4.1：任务表/预约表/清理表/存储目录双向核对）
# --------------------------------------------------------------------------- #
def collect(cur, upload_dir):
    cur.execute(
        "SELECT t.upload_id, t.owner_user_id, t.state, t.reservation_id AS"
        " rid, r.state AS rstate, r.user_id AS ruser, r.reserved_bytes,"
        " r.holder_kind, r.holder_id FROM upload_tasks t"
        " LEFT JOIN upload_reservations r ON r.reservation_id=t.reservation_id"
        " ORDER BY t.upload_id")
    tasks = [dict(r) for r in cur.fetchall()]

    cur.execute(
        "SELECT j.job_id, j.owner_user_id, j.owner_role, j.state,"
        " j.local_reservation_id AS rid, j.local_cleanup_status,"
        " r.state AS rstate, r.user_id AS ruser, r.reserved_bytes,"
        " r.holder_kind, r.holder_id FROM ingestion_jobs j"
        " LEFT JOIN upload_reservations r ON r.reservation_id="
        " j.local_reservation_id ORDER BY j.job_id")
    jobs = [dict(r) for r in cur.fetchall()]

    cur.execute(
        "SELECT b.id AS batch_id, b.owner_user_id, b.state,"
        " b.quota_reservation_id AS rid, b.total_bytes, r.state AS rstate,"
        " r.user_id AS ruser, r.reserved_bytes, r.holder_kind, r.holder_id"
        " FROM baidu_import_batches b"
        " LEFT JOIN upload_reservations r ON r.reservation_id="
        " b.quota_reservation_id ORDER BY b.id")
    batches = [dict(r) for r in cur.fetchall()]

    cur.execute("SELECT upload_id, reservation_id, attempts FROM "
                "upload_cleanup_pending ORDER BY upload_id")
    pending_rows = [dict(r) for r in cur.fetchall()]
    pending_by_task = {p["upload_id"]: p for p in pending_rows}

    items = []
    for t in tasks:
        if t["state"] in _ACTIVE_UPLOAD_TASK_STATES:
            items.append(("upload_task", t))
        elif t["rid"] or t["upload_id"] in pending_by_task:
            row = dict(t)
            row["pending"] = pending_by_task.get(t["upload_id"])
            items.append(("upload_task_terminal", row))
    for j in jobs:
        duty = (j["owner_role"] == "user" and (j["owner_user_id"] or ""))
        if j["state"] in _ACTIVE_INGESTION_STATES:
            items.append(("ingestion_job" if duty
                          else "ingestion_job_exempt", j))
        elif j["local_cleanup_status"] in ("pending", "failed"):
            items.append(("ingestion_job_cleanup", j))
    for b in batches:
        if b["state"] in ("queued", "running"):
            items.append(("baidu_batch", b))

    referenced = set()
    for t in tasks:
        if t["rid"]:
            referenced.add(t["rid"])
    for j in jobs:
        if j["rid"]:
            referenced.add(j["rid"])
    for b in batches:
        if b["rid"]:
            referenced.add(b["rid"])
    for p in pending_rows:
        if p["reservation_id"]:
            referenced.add(p["reservation_id"])
    cur.execute(
        "SELECT reservation_id, user_id, reserved_bytes, holder_kind,"
        " holder_id FROM upload_reservations WHERE state='reserved'"
        " ORDER BY reservation_id")
    dangling = [dict(r) for r in cur.fetchall()
                if r["reservation_id"] not in referenced]

    known_ids = {t["upload_id"] for t in tasks} | {j["job_id"] for j in jobs}
    unknown_dirs = []
    sroot = os.path.join(str(upload_dir), ".staging")
    if os.path.isdir(sroot):
        for name in sorted(os.listdir(sroot)):
            if name not in known_ids:
                unknown_dirs.append(name)

    cur.execute(
        "SELECT user_id, quota_bytes, used_bytes, reserved_bytes,"
        " used_bytes + reserved_bytes - quota_bytes AS over FROM"
        " upload_user_quotas WHERE used_bytes + reserved_bytes >"
        " quota_bytes ORDER BY user_id")
    over_quota = [dict(r) for r in cur.fetchall()]

    # 逐项字节证据（豁免身份无需——预约为空按身份合同识别，非异常）
    for kind, row in items:
        if kind.endswith("_exempt"):
            continue
        base_kind = _base_kind(kind)
        tid = row[_HOLDER_ID_KEY[base_kind]]
        row["staging_files"], row["staging_bytes"] = scan_task_tree(
            upload_dir, tid)
    return {"items": items, "pending": pending_rows, "dangling": dangling,
            "unknown_dirs": unknown_dirs, "over_quota": over_quota,
            "counts": {"upload_tasks": len(tasks),
                       "ingestion_jobs": len(jobs),
                       "baidu_import_batches": len(batches),
                       "upload_cleanup_pending": len(pending_rows)}}


def _base_kind(kind):
    return kind.replace("_terminal", "").replace("_cleanup", "").replace(
        "_exempt", "")


def classify(kind, row):
    """(kind,row) → ok/bind/missing/released/consumed/mismatch_*。"""
    if kind.endswith("_exempt"):
        return "ok"
    base_kind = _base_kind(kind)
    rid = row.get("rid") or (row.get("pending") or {}).get("reservation_id")
    if not rid:
        return "missing"
    if row.get("rstate") is None:
        return "missing"
    if row["rstate"] != "reserved":
        return row["rstate"]
    holder_id = row[_HOLDER_ID_KEY[base_kind]]
    if (row.get("holder_id") or None) == holder_id and \
            (row.get("holder_kind") or None) == base_kind:
        if row.get("ruser") == row.get("owner_user_id"):
            return "ok"
        return "mismatch_owner"
    if row.get("holder_id"):
        return "mismatch_holder"
    if row.get("ruser") != row.get("owner_user_id"):
        return "mismatch_owner"
    return "bind"


def plan_actions(state, repair_residuals):
    """扫描态 → (actions, blockers)。动作 key 持久唯一、可重放。"""
    actions, blockers = [], []
    for kind, row in state["items"]:
        base_kind = _base_kind(kind)
        verdict = classify(kind, row)
        tid = row[_HOLDER_ID_KEY[base_kind]]
        rid = row.get("rid") or (row.get("pending") or {}
                                 ).get("reservation_id")
        if verdict == "ok":
            continue
        if verdict == "bind":
            if kind == "upload_task_terminal" and \
                    int(row.get("staging_bytes") or 0) > \
                    int(row.get("reserved_bytes") or 0):
                blockers.append({"kind": kind, "id": tid,
                                 "reason": "residue_exceeds_reservation",
                                 "bytes": row.get("staging_bytes"),
                                 "reserved": row.get("reserved_bytes")})
                continue
            actions.append({
                "action_key": "bind:%s:%s:%s" % (base_kind, tid, rid),
                "action": "bind", "kind": base_kind, "id": tid,
                "reservation_id": rid,
                "evidence": {"bytes": row.get("staging_bytes"),
                             "reserved_bytes": row.get("reserved_bytes")}})
            continue
        if verdict in ("missing", "released", "consumed"):
            has_residue = int(row.get("staging_bytes") or 0) > 0
            if kind in ("upload_task", "ingestion_job"):
                actions.append({
                    "action_key": "stop:%s:%s" % (base_kind, tid),
                    "action": "stop", "kind": base_kind, "id": tid,
                    "observed": verdict,
                    "evidence": {"bytes": row.get("staging_bytes")}})
                if verdict == "consumed":
                    blockers.append({"kind": kind, "id": tid,
                                     "reason": "consumed_unexplained",
                                     "reservation_id": rid})
                elif repair_residuals and has_residue:
                    actions.append(_repair_action(base_kind, tid, row))
                elif has_residue:
                    blockers.append({"kind": kind, "id": tid,
                                     "reason": "residual_without_duty",
                                     "bytes": row.get("staging_bytes"),
                                     "need_flag": "--repair-residuals"})
            elif kind in ("upload_task_terminal", "ingestion_job_cleanup"):
                if repair_residuals and has_residue:
                    actions.append(_repair_action(base_kind, tid, row))
                elif has_residue:
                    blockers.append({"kind": kind, "id": tid,
                                     "reason": "residual_without_duty",
                                     "bytes": row.get("staging_bytes"),
                                     "need_flag": "--repair-residuals"})
            elif kind == "baidu_batch":
                blockers.append({"kind": kind, "id": tid,
                                 "reason": "shared_budget_unattributable",
                                 "observed": verdict})
            continue
        blockers.append({"kind": kind, "id": tid, "reason": verdict,
                         "reservation_id": rid})

    for r in state["dangling"]:
        blockers.append({"kind": "reservation", "id": r["reservation_id"],
                         "reason": "dangling_reserved",
                         "user": r["user_id"],
                         "reserved_bytes": int(r["reserved_bytes"]),
                         "holder": [r["holder_kind"], r["holder_id"]]})
    for d in state["unknown_dirs"]:
        blockers.append({"kind": "staging_dir", "id": d,
                         "reason": "unknown_staging_dir"})
    return actions, blockers


def _repair_action(base_kind, tid, row):
    return {"action_key": "repair:%s:%s" % (base_kind, tid),
            "action": "repair", "kind": base_kind, "id": tid,
            "owner": row.get("owner_user_id"),
            "evidence": {"bytes": row.get("staging_bytes"),
                         "files": row.get("staging_files")}}


def _prestate_for(cur, actions):
    """冻结/复核用的 DB 前态：全局计数 + 逐动作目标关键列。"""
    pre = {"counts": {}, "targets": {}}
    for table in ("upload_tasks", "ingestion_jobs", "baidu_import_batches",
                  "upload_cleanup_pending", "upload_reservations"):
        cur.execute("SELECT COUNT(*)::int AS n FROM %s" % table)
        pre["counts"][table] = cur.fetchone()["n"]
    for act in actions:
        if act["kind"] == "upload_task":
            cur.execute("SELECT state, reservation_id FROM upload_tasks "
                        "WHERE upload_id=%s", (act["id"],))
        elif act["kind"] == "ingestion_job":
            cur.execute("SELECT state, local_reservation_id FROM "
                        "ingestion_jobs WHERE job_id=%s", (act["id"],))
        else:
            continue
        row = cur.fetchone()
        pre["targets"][act["action_key"]] = row and dict(row) or None
    return pre


def _applied_action_keys(cur, plan):
    cur.execute("SELECT action_key FROM upload_capacity_repair_receipts "
                "WHERE plan_hash=%s", (plan["plan_hash"],))
    return {r["action_key"] for r in cur.fetchall()}


def _verify_prestate(cur, plan, already_applied=frozenset()):
    """DB 前态复核。已应用动作（回执在）的目标跳过；全局计数按已应用
    动作的推导效应校正（stop:upload_task → pending+1；repair →
    reservations+1）——同计划重跑不因自身已生效而误报漂移。"""
    expect = plan["db_prestate"]
    delta_pending = sum(1 for a in plan["actions"]
                        if a["action_key"] in already_applied
                        and a["action"] == "stop"
                        and a["kind"] == "upload_task")
    delta_reservations = sum(1 for a in plan["actions"]
                             if a["action_key"] in already_applied
                             and a["action"] == "repair")
    adjusted = dict(expect["counts"])
    adjusted["upload_cleanup_pending"] = \
        adjusted.get("upload_cleanup_pending", 0) + delta_pending
    adjusted["upload_reservations"] = \
        adjusted.get("upload_reservations", 0) + delta_reservations
    for table, n in adjusted.items():
        cur.execute("SELECT COUNT(*)::int AS n FROM %s" % table)
        got = cur.fetchone()["n"]
        if got != n:
            raise EvidenceError("表 %s 计数漂移（期望 %d 实际 %d）"
                                % (table, n, got))
    for act in plan["actions"]:
        if act["action_key"] in already_applied:
            continue  # 已应用：终态由回执+跳过语义覆盖
        exp = expect["targets"].get(act["action_key"])
        if act["kind"] == "upload_task":
            cur.execute("SELECT state, reservation_id FROM upload_tasks "
                        "WHERE upload_id=%s", (act["id"],))
        elif act["kind"] == "ingestion_job":
            cur.execute("SELECT state, local_reservation_id FROM "
                        "ingestion_jobs WHERE job_id=%s", (act["id"],))
        else:
            continue
        row = cur.fetchone()
        cur_row = row and dict(row) or None
        if cur_row != exp:
            raise EvidenceError("目标前态漂移：%s（%r → %r）"
                                % (act["action_key"], exp, cur_row))


def _verify_file_evidence(upload_dir, plan):
    for act in plan["actions"]:
        if act["action"] != "repair":
            continue
        nfiles, total = scan_task_tree(upload_dir, act["id"])
        if total != int(act["evidence"]["bytes"]) or \
                nfiles != int(act["evidence"]["files"]):
            raise EvidenceError("文件证据漂移：%s（%d/%d → %d/%d）"
                                % (act["action_key"],
                                   act["evidence"]["bytes"],
                                   act["evidence"]["files"], total, nfiles))


def apply_action(cur, act):
    kind, tid = act["kind"], act["id"]
    if act["action"] == "bind":
        upload_guard.bind_reservation_locked(
            cur, act["reservation_id"], kind, tid, _PURPOSE[kind])
        return {"bound": act["reservation_id"]}
    if act["action"] == "stop":
        if kind == "upload_task":
            cur.execute(
                "UPDATE upload_tasks SET state='failed', updated_at=now()"
                " WHERE upload_id=%s AND state = ANY(%s)",
                (tid, list(_ACTIVE_UPLOAD_TASK_STATES)))
            cur.execute(
                "INSERT INTO upload_cleanup_pending (upload_id,"
                " reservation_id, attempts, last_error)"
                " VALUES (%s,NULL,1,%s) ON CONFLICT (upload_id) DO UPDATE"
                " SET attempts = upload_cleanup_pending.attempts + 1,"
                " last_error = EXCLUDED.last_error, updated_at=now()",
                (tid, "reconcile: reservation %s" % act.get("observed")))
        else:
            cur.execute(
                "UPDATE ingestion_jobs SET state='failed', fail_code="
                "'local_reservation_invalid', terminal_at=now(),"
                " cleanup_status='pending', local_cleanup_status='pending',"
                " updated_at=now() WHERE job_id=%s AND state = ANY(%s)",
                (tid, list(_ACTIVE_INGESTION_STATES)))
        return {"stopped": True}
    if act["action"] == "repair":
        res = upload_guard.record_reconciled_residual_locked(
            cur, act["owner"], int(act["evidence"]["bytes"]),
            holder_kind=kind, holder_id=tid, purpose=_PURPOSE[kind])
        if kind == "upload_task":
            cur.execute("UPDATE upload_tasks SET reservation_id=%s,"
                        " updated_at=now() WHERE upload_id=%s",
                        (res["reservation_id"], tid))
            cur.execute("UPDATE upload_cleanup_pending SET reservation_id=%s,"
                        " updated_at=now() WHERE upload_id=%s",
                        (res["reservation_id"], tid))
        else:
            cur.execute("UPDATE ingestion_jobs SET local_reservation_id=%s,"
                        " updated_at=now() WHERE job_id=%s",
                        (res["reservation_id"], tid))
        return {"repaired": res["reservation_id"],
                "bytes": int(act["evidence"]["bytes"])}
    raise ValueError("未知动作：%r" % act["action"])


def _unexplained_after(state, actions_left, blockers_left):
    """应用后不得为 0 的项：残留无 duty / dangling / 未知目录 / 计划外。"""
    problems = list(blockers_left)
    for a in actions_left:
        problems.append({"kind": a["kind"], "id": a["id"],
                         "reason": "action_still_pending",
                         "action_key": a["action_key"]})
    return problems


def _plan_hash(plan):
    body = {k: v for k, v in plan.items() if k != "plan_hash"}
    return hashlib.sha256(json.dumps(body, sort_keys=True,
                                     ensure_ascii=False).encode()).hexdigest()


def _residual_without_duty(state, actions=()):
    planned_duty = {a["id"] for a in actions if a["action"] == "repair"}
    for kind, row in state["items"]:
        if kind.endswith("_exempt"):
            continue
        base_kind = _base_kind(kind)
        if row.get(_HOLDER_ID_KEY[base_kind]) in planned_duty:
            continue  # 计划内补记责任（--repair-residuals）
        if kind.endswith("_exempt"):
            continue
        if int(row.get("staging_bytes") or 0) > 0 and \
                classify(kind, row) != "ok" and not (
                    row.get("rid")
                    or (row.get("pending") or {}).get("reservation_id")):
            return True
    return False


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--database-url", default=None)
    ap.add_argument("--upload-dir", default=os.environ.get("UPLOAD_DIR"))
    ap.add_argument("--apply", action="store_true",
                    help="应用冻结计划（必须与 --plan 同用）")
    ap.add_argument("--plan", default=None, help="冻结计划文件（应用输入）")
    ap.add_argument("--plan-out", default=None,
                    help="生成冻结计划文件（维护窗口内执行）")
    ap.add_argument("--repair-residuals", action="store_true",
                    help="按冻结证据补记残留责任（维护专用；不恢复执行）")
    ap.add_argument("--reattach", action="store_true", help=argparse.SUPPRESS)
    ap.add_argument("--json-out", default=None)
    args = ap.parse_args(argv)
    if args.reattach:
        print("--reattach 已按 R12 §3.4 拆除（核账只补责任，不恢复执行）；"
              "改用 --repair-residuals", file=sys.stderr)
        return 2
    if args.apply and not args.plan:
        print("--apply 必须与 --plan <冻结计划> 同用（在线 dry-run 不是"
              "可应用计划）", file=sys.stderr)
        return 2
    if args.upload_dir is None:
        print("--upload-dir 或 UPLOAD_DIR 必须提供（暂存字节审计输入）",
              file=sys.stderr)
        return 2
    upload_dir = os.path.abspath(str(args.upload_dir))

    conn = _connect(args.database_url)
    try:
        if args.apply:
            with open(args.plan, "r", encoding="utf-8") as fh:
                plan = json.load(fh)
            if _plan_hash(plan) != plan.get("plan_hash"):
                print("计划自校验失败（plan_hash 不符）", file=sys.stderr)
                return 3
            if plan.get("upload_root") != upload_dir:
                print("计划数据根与当前 --upload-dir 不符（%r != %r）"
                      % (plan.get("upload_root"), upload_dir),
                      file=sys.stderr)
                return 3
            try:
                _verify_file_evidence(upload_dir, plan)
            except EvidenceError as exc:
                print("预检 no-go（文件证据漂移）：%s" % exc,
                      file=sys.stderr)
                return 3
            # DB 前态 + 环境漂移预检（短事务，只读）——全过再应用；
            # 已有回执的同计划动作按「已应用」校正计数（幂等重跑）
            with pg_store.transaction(conn):
                with conn.cursor() as cur:
                    try:
                        already = _applied_action_keys(cur, plan)
                        _verify_prestate(cur, plan, already)
                    except EvidenceError as exc:
                        print("预检 no-go（DB 前态漂移）：%s" % exc,
                              file=sys.stderr)
                        return 3
                    state = collect(cur, upload_dir)
                    actions_now, blockers_now = plan_actions(
                        state, args.repair_residuals)
                    plan_keys = {a["action_key"] for a in plan["actions"]}
                    new_keys = {a["action_key"] for a in actions_now} - \
                        plan_keys - already
                    if new_keys:
                        print("预检 no-go：计划外动作（环境漂移/停写被"
                              "破坏）：%s" % sorted(new_keys),
                              file=sys.stderr)
                        return 3
            # 单事务应用 + 回执（幂等：回执在 → 跳过）
            applied, skipped = [], []
            with pg_store.transaction(conn):
                with conn.cursor() as cur:
                    for act in plan["actions"]:
                        cur.execute(
                            "INSERT INTO upload_capacity_repair_receipts "
                            "(action_key, plan_hash, target_kind, target_id,"
                            " action) VALUES (%s,%s,%s,%s,%s) ON CONFLICT"
                            " (action_key) DO NOTHING RETURNING action_key",
                            (act["action_key"], plan["plan_hash"],
                             act["kind"], act["id"], act["action"]))
                        if cur.fetchone() is None:
                            skipped.append(act["action_key"])
                            continue
                        result = apply_action(cur, act)
                        applied.append({"action_key": act["action_key"],
                                        "result": result})
            # 应用后重新 collect（输出应用后状态，不用应用前快照）
            with pg_store.transaction(conn):
                with conn.cursor() as cur:
                    state = collect(cur, upload_dir)
            actions_left, blockers_left = plan_actions(
                state, args.repair_residuals)
            report = {"mode": "apply", "plan_hash": plan["plan_hash"],
                      "applied": applied, "skipped": skipped,
                      "over_quota": state["over_quota"],
                      "pending_work": {
                          "upload_cleanup_pending": state["pending"],
                          "ingestion_local_cleanup": [
                              dict(r) for k, r in state["items"]
                              if k == "ingestion_job_cleanup"]},
                      "unexplained": _unexplained_after(
                          state, actions_left, blockers_left),
                      "note": ("超额已如实补记；新准入仍被 guard 拒绝"
                               if state["over_quota"] else None)}
            out = json.dumps(report, ensure_ascii=False, indent=2,
                             default=str)
            print(out)
            if args.json_out:
                with open(args.json_out, "w", encoding="utf-8") as fh:
                    fh.write(out)
            return 3 if report["unexplained"] else 0

        # dry-run / 冻结计划生成
        with pg_store.transaction(conn):
            with conn.cursor() as cur:
                state = collect(cur, upload_dir)
                actions, blockers = plan_actions(state,
                                                 args.repair_residuals)
                prestate = _prestate_for(cur, actions)
        report = {"mode": "plan-out" if args.plan_out else "dry-run",
                  "tool_version": TOOL_VERSION,
                  "actions": actions, "blockers": blockers,
                  "over_quota": state["over_quota"],
                  "pending_work": {
                      "upload_cleanup_pending": state["pending"],
                      "ingestion_local_cleanup": [
                          dict(r) for k, r in state["items"]
                          if k == "ingestion_job_cleanup"]},
                  "note": "dry-run 仅预审；应用需冻结计划（--plan-out）"
                          " + --apply --plan"}
        if args.plan_out:
            plan = {"schema_version": SCHEMA_VERSION,
                    "tool_version": TOOL_VERSION,
                    "created_at": _dt.datetime.now(
                        _dt.timezone.utc).isoformat(),
                    "upload_root": upload_dir,
                    "repair_residuals": bool(args.repair_residuals),
                    "db_prestate": prestate,
                    "actions": actions}
            plan["plan_hash"] = _plan_hash(plan)
            with open(args.plan_out, "w", encoding="utf-8") as fh:
                json.dump(plan, fh, ensure_ascii=False, indent=2)
            report["plan_out"] = args.plan_out
            report["plan_hash"] = plan["plan_hash"]
        out = json.dumps(report, ensure_ascii=False, indent=2, default=str)
        print(out)
        if args.json_out:
            with open(args.json_out, "w", encoding="utf-8") as fh:
                fh.write(out)
        if blockers:
            return 3
        if _residual_without_duty(state, actions):
            return 3
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
