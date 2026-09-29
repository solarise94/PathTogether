#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""转换/百度旧链路排空审计工具（C6：退役前盘点 + 排空/恢复演练门禁）。

退役对象（docs/slide-tools/c6-drain-report.md）：
  - LEGACY 后台切片转换链路（conversion_jobs / conversion_worker——旧
    in-process KFB 转换 worker）；
  - LEGACY 百度分享导入 in-process 执行器（scripts/baidu_import_worker.py
    经 claim_batch/run_claimed_batch 领取批次；插件执行器 worker_id 前缀
    ``plugin:`` 经 plugin_claim_batch 同一领取原语接管，非退役对象）。

本工具**严格只读**（默认形态下只 SELECT / stat / listdir；不写库、不写
文件、不触碰网络）。与 scripts/upload_drain.py（R16 旧上传链路）同一风格：
可重复执行、机器可读（--json）、退出码稳定。

子命令：

  inventory  盘点快照：schema 特性探测（兼容生产 0065 旧库——缺失特性按
             ``not_applicable_schema`` 报告，绝不因缺列/缺表失败）、
             conversion/baidu 在途与终态、文件残留归类、保留源与源字节
             计费（C7 输入）、容量台账、核账（reconcile_upload_capacity
             只读 collect+plan_actions）、运维核对项。
  report     排空裁决：exit 0 = GO；3 = NO-GO（逐项打印 reason code 与
             id）；2 = 用法错误。
  compare    前后快照台账漂移核验（排空演练门禁）：残差（used_bytes −
             在册 id_bundle 产物 accounted_bytes 之和）只应被「快照间新
             结算的源字节」移动；否则 exit 3。

裁决（report 阻断码；每项带 reason code 与 id 清单）：

  conversion_job_open          任意进行中转换任务（held/queued/converting/
                               validating——held 是 COS conversion 形态在父
                               任务行锁内创建的不可领取子任务，同样未收口）
  ingest_intent_unresolved     conversion 形态 ingestion 在途且已持久化
                               commit intent / 关联 conversion_job_id
  intent_unresolved            validating 转换任务携带未结算 commit_intent
                               （发布可能只进行到一半——崩溃恢复栅栏未过）
  baidu_in_process_live_lease  活跃批次被 in-process 执行器持有且租约未过期
                               （退役窗口内该执行器必须停，批次无人接管）
  baidu_handoff_pending        in-process 执行器租约已过期且仍有非终态条目
                               （插件必须经 plugin_claim_batch 接管收口）
  baidu_reservation_terminal_bound  reserved 批次预算绑定在终态批次上
                               （配额收口义务未随终态落地——异常）
  residue_attempt_dir          终态任务的工作代次目录残留
  residue_cancelled_job        cancelled 任务的任务树残留（含 source/）
  residue_flat_source          平铺源只被 cancelled 任务引用（清理义务）
  baidu_staging_residue        终态批次在 STAGING_ROOT 下的本地暂存残留
  baidu_staging_unknown        STAGING_ROOT 下无批次行归属的目录
  unknown_staging_dir          .staging 下不归属任何已知表 id 的目录
  source_missing_open          进行中任务源副本缺失（且无平铺源）
  reconcile_blocker            核账工具阻断项（配账双向/悬挂预约等）
  reconcile_scan_failed        核账证据扫描失败（不把少扫描当没有数据）
  live_lock_holder             --probe-locks 探测到任务存储锁有活跃持有者
                               （排空窗口内不应有 worker 持锁）

非阻断（列出、不裁决）：queued/无人领取与 plugin: 租约的批次（插件工作）；
failed 任务源缺失与产物/项目关联未收口（cleanup/decision，退役决策项）；
保留源与源字节计费（C7 输入，decision required——源字节在上传/批次结算时
已计入 used_bytes，删除产物只按产物 accounted_bytes 退款，源侧字节**从不
退款**，由 C7 裁决）。

compare 不变量（docstring 与 docs/slide-tools/c6-drain-report.md §6）：
产物结算/删除使 used_bytes 与「ready/deleting 的 id_bundle slides
accounted_bytes 之和」等量移动，残差只被源字节结算（COS conversion 形态
consume 的源字节、旧上传任务 finish_commit 结算的源字节、百度批次终态
consume 的源字节合计）移动。因此
  drift(user) = (residual_after − residual_before)
              − Σ(快照间新结算的源字节事件)
新事件按稳定 key（``ing:<job_id>`` / ``upt:<upload_id>`` / ``bib:<batch_id>``
）对账：AFTER 有 BEFORE 无即新结算；同 key 字节变化按差量计。drift != 0
即有未解释的台账漂移 → exit 3。预约行的消失/状态变化单独列出并给出持有者
解释（baidu_batch 终态 consume、ingestion 清理后 release 等）。

用法：
  python3 scripts/conversion_drain.py inventory [--upload-dir DIR]
      [--baidu-staging-dir DIR] [--json PATH] [--probe-locks]
      [--database-url URL]
  python3 scripts/conversion_drain.py report   [同上选项]
  python3 scripts/conversion_drain.py compare  BEFORE.json AFTER.json

退出码：0 = 通过/GO；3 = no-go/发现漂移；2 = 用法错误。
"""

from __future__ import annotations

import argparse
import datetime as _dt
import errno
import fcntl
import json
import os
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
for _p in (_REPO, _REPO / "scripts"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import psycopg.rows  # noqa: E402

import pg_store  # noqa: E402
import reconcile_upload_capacity as recon  # noqa: E402

TOOL_VERSION = "c6.1"

OPS_CHECKLIST = [
    "转换 worker（conversion_worker / docker_entry.sh 转换进程）已全部停止",
    "百度 in-process 执行器已停（BAIDU_IMPORT_WORKER=0 且无 "
    "scripts/baidu_import_worker.py 进程；插件执行器除外）",
    "无 kfb 转换子进程仍在写 UPLOAD_DIR/.staging（ps/日志核对）",
    "排空窗口内边缘无新 conversion 形态 ingestion / 转换任务创建（流量摘除）",
]

_CONVERSION_OPEN_STATES = ("held", "queued", "converting", "validating")
_CONVERSION_TERMINAL_STATES = ("ready", "failed", "cancelled")
_BAIDU_ACTIVE_STATES = ("queued", "running")
_BAIDU_TERMINAL_STATES = ("succeeded", "partial_failed", "failed", "cancelled")
_ITEM_TERMINAL_STAGES = ("ready", "failed", "cancelled")

#: 与 task_storage_lock.KINDS 中转换/百度相关的锁目录（只读探测目标）。
_LOCK_KINDS = ("conversion_job", "baidu_batch", "ingestion_job")


def _connect(database_url=None):
    """只读快照连接：整份盘点在一个 REPEATABLE READ READ ONLY 事务里完成——
    各 section 看到同一时点（窗口外运行时不会前后矛盾），且任何写都会被
    数据库拒绝（只读由库强制，不只靠本工具自律）。"""
    conn = psycopg.connect(database_url or pg_store.get_conninfo())
    conn.isolation_level = psycopg.IsolationLevel.REPEATABLE_READ
    conn.read_only = True
    conn.row_factory = psycopg.rows.dict_row
    return conn


def _q(cur, sql, args=()):
    cur.execute(sql, args)
    return [dict(r) for r in cur.fetchall()]


def _not_applicable(reason):
    return {"not_applicable_schema": True, "reason": reason}


# --------------------------------------------------------------------------- #
# schema 特性探测（生产仍在 0065 旧镜像：缺列/缺表按能力降级，绝不失败）
# --------------------------------------------------------------------------- #
def detect_schema(cur):
    """已应用迁移 + information_schema 列/表 + CHECK 约束 → 特性字典。

    特性以**实际 schema** 为准（schema_migrations 只作展示）；任何缺失都
    是正常形态（旧库），对应 section 输出 not_applicable_schema。"""
    applied = []
    if _table_exists(cur, "schema_migrations"):
        applied = sorted(r["filename"] for r in
                         _q(cur, "SELECT filename FROM schema_migrations"))
    cur.execute(
        "SELECT table_name, column_name FROM information_schema.columns "
        "WHERE table_schema='public'")
    cols = {(r["table_name"], r["column_name"]) for r in cur.fetchall()}
    tables = {t for (t, _c) in cols}

    held_ok = False
    if "conversion_jobs" in tables:
        defs = _q(
            cur,
            "SELECT pg_get_constraintdef(oid) AS def FROM pg_constraint "
            "WHERE conrelid='conversion_jobs'::regclass AND contype='c'")
        held_ok = any("'held'" in (d["def"] or "") for d in defs)

    features = {
        "conversion_jobs": "conversion_jobs" in tables,
        "conversion_job_sources": "conversion_job_sources" in tables,
        "conversion_slide_id": ("conversion_jobs", "slide_id") in cols,
        "conversion_commit_intent":
            ("conversion_jobs", "commit_intent_json") in cols,
        "conversion_held_state": held_ok,
        "project_association":
            ("conversion_jobs", "project_associate_state") in cols,
        "slides_asset_accounting":
            ("slides", "asset_state") in cols
            and ("slides", "accounted_bytes") in cols
            and ("slides", "storage_layout") in cols,
        "reservation_holder_binding":
            ("upload_reservations", "holder_kind") in cols,
        "ingestion_jobs": "ingestion_jobs" in tables,
        "ingestion_kinds": ("ingestion_jobs", "kind") in cols,
        "producer_imports": "producer_imports" in tables,
        "baidu_tables": "baidu_import_batches" in tables,
        "upload_tasks": "upload_tasks" in tables,
        "upload_cleanup_pending": "upload_cleanup_pending" in tables,
        "upload_task_quota_mode": ("upload_tasks", "quota_mode") in cols,
        "upload_task_intent": ("upload_tasks", "commit_intent_json") in cols,
    }
    return {"applied_max": applied[-1] if applied else None,
            "applied_count": len(applied),
            "features": features}


def _table_exists(cur, name):
    cur.execute(
        "SELECT 1 FROM information_schema.tables WHERE table_schema='public' "
        "AND table_name=%s", (name,))
    return cur.fetchone() is not None


def _iso(value):
    return value.isoformat() if hasattr(value, "isoformat") else value


# --------------------------------------------------------------------------- #
# 各 section 采集（全部只读）
# --------------------------------------------------------------------------- #
def _section_conversion(cur, schema, db_now):
    feat = schema["features"]
    if not feat["conversion_jobs"]:
        return _not_applicable("conversion_jobs 表不存在（< 0046）")
    jobs = _q(cur, "SELECT * FROM conversion_jobs ORDER BY created_at, id")
    counts = {}
    for j in jobs:
        counts[j["state"]] = counts.get(j["state"], 0) + 1

    ingest_states = {}
    if feat["ingestion_jobs"]:
        for r in _q(cur, "SELECT job_id, state FROM ingestion_jobs"):
            ingest_states[r["job_id"]] = r["state"]

    open_jobs = []
    for j in jobs:
        if j["state"] not in _CONVERSION_OPEN_STATES:
            continue
        expires = j.get("lease_expires_at")
        open_jobs.append({
            "id": j["id"],
            "owner_user_id": j.get("owner_user_id"),
            "state": j["state"],
            "attempt": int(j.get("attempt") or 0),
            "lease_owner": j.get("lease_owner"),
            "lease_state": (None if expires is None else
                            ("live" if expires > db_now else "expired")),
            "has_intent": bool(j.get("commit_intent_json")),
            "slide_id": j.get("slide_id"),
            "upload_id": j.get("upload_id"),
            "parent_ingestion_state":
                ingest_states.get(j.get("upload_id")),
            "created_at": _iso(j.get("created_at")),
            "age_seconds": ((db_now - j["created_at"]).total_seconds()
                            if j.get("created_at") else None),
        })

    obligations = []
    if feat["project_association"]:
        for j in jobs:
            if j["state"] == "ready" and \
                    j.get("project_associate_state") in ("pending", "failed"):
                obligations.append({
                    "id": j["id"],
                    "project_associate_state": j["project_associate_state"],
                    "target_project_id": j.get("target_project_id"),
                    "slide_id": j.get("slide_id")})
    else:
        obligations = _not_applicable(
            "conversion_jobs.project_associate_state 列不存在（< 0052）")

    return {"counts_by_state": counts, "open_jobs": open_jobs,
            "association_obligations": obligations}


def _section_intents(cur, schema):
    feat = schema["features"]
    if not feat["conversion_commit_intent"]:
        conv = _not_applicable(
            "conversion_jobs.commit_intent_json 列不存在（< 0069）")
    else:
        rows = _q(cur, "SELECT * FROM conversion_jobs "
                      "WHERE commit_intent_json IS NOT NULL "
                      "AND state <> 'ready' ORDER BY id")
        jobs = []
        for r in rows:
            intent = None
            try:
                intent = json.loads(r["commit_intent_json"])
            except (TypeError, ValueError):
                intent = None
            jobs.append({
                "id": r["id"], "state": r["state"],
                "owner_user_id": r.get("owner_user_id"),
                "attempt": int(r.get("attempt") or 0),
                "slide_id": r.get("slide_id"),
                "intent_generation": (intent or {}).get("generation"),
                "intent_sha256": (intent or {}).get("sha256"),
                "intent_accounted_bytes": (intent or {}).get(
                    "accounted_bytes"),
                "intent_readable": intent is not None})
        conv = {"jobs": jobs}

    if not (feat["ingestion_jobs"] and feat["ingestion_kinds"]):
        ing = _not_applicable(
            "ingestion_jobs 表/kind 列不存在（< 0066/0075；生产 0065 旧库）")
    else:
        rows = _q(
            cur,
            "SELECT job_id, state, owner_user_id, kind, conversion_job_id, "
            "(commit_intent_json IS NOT NULL) AS has_intent FROM "
            "ingestion_jobs WHERE kind='conversion' AND state NOT IN "
            "('completed','cancelled','failed','expired') AND "
            "(commit_intent_json IS NOT NULL OR conversion_job_id IS NOT "
            "NULL) ORDER BY job_id")
        ing = {"jobs": rows}
    return {"conversion": conv, "ingestion": ing}


def _section_baidu(cur, schema, db_now):
    feat = schema["features"]
    if not feat["baidu_tables"]:
        return _not_applicable("baidu_import_batches 表不存在（< 0051）")
    batches = _q(cur, "SELECT * FROM baidu_import_batches ORDER BY created_at")
    items_by_batch = {}
    for it in _q(cur, "SELECT id, batch_id, stage, source_size, "
                      "conversion_job_id FROM baidu_import_items"):
        items_by_batch.setdefault(it["batch_id"], []).append(it)
    counts = {}
    for b in batches:
        counts[b["state"]] = counts.get(b["state"], 0) + 1

    active = []
    for b in batches:
        if b["state"] not in _BAIDU_ACTIVE_STATES:
            continue
        owner = b.get("lease_owner") or ""
        expires = b.get("lease_expires_at")
        items = items_by_batch.get(b["id"], [])
        active.append({
            "id": b["id"],
            "state": b["state"],
            "owner_user_id": b.get("owner_user_id"),
            "executor": ("plugin" if owner.startswith("plugin:")
                         else ("in_process" if owner else "unclaimed")),
            "lease_owner": owner or None,
            "lease_state": (None if (expires is None or not owner)
                            else ("live" if expires > db_now
                                  else "expired")),
            "cancel_requested": bool(b.get("cancel_requested")),
            "non_terminal_items": sum(
                1 for i in items if i["stage"] not in _ITEM_TERMINAL_STAGES),
            "total_items": len(items)})

    cleanup_obligations = [
        {"id": b["id"], "state": b["state"],
         "cleanup_state": b["cleanup_state"],
         "quota_reservation_id": b.get("quota_reservation_id")}
        for b in batches
        if b["state"] in _BAIDU_TERMINAL_STATES
        and b.get("cleanup_state") in ("pending", "failed")]

    terminal_ids = {b["id"] for b in batches
                    if b["state"] in _BAIDU_TERMINAL_STATES}
    res_state_by_id = {}
    if feat["reservation_holder_binding"]:
        rows = _q(cur, "SELECT reservation_id, state, holder_kind, holder_id, "
                      "user_id, reserved_bytes, settled_bytes, purpose FROM "
                      "upload_reservations WHERE holder_kind='baidu_batch'")
        grouped = {}
        for r in _q(cur, "SELECT state, COUNT(*)::int AS n, "
                         "COALESCE(SUM(reserved_bytes),0)::bigint AS bytes "
                         "FROM upload_reservations WHERE "
                         "holder_kind='baidu_batch' GROUP BY state "
                         "ORDER BY state"):
            grouped[r["state"]] = {"count": r["n"], "bytes": int(r["bytes"])}
        reservations = {"by_state": grouped}
    else:
        rows = _q(
            cur,
            "SELECT r.reservation_id, r.state, NULL AS holder_kind, "
            "NULL AS holder_id, r.user_id, r.reserved_bytes, "
            "r.settled_bytes, NULL AS purpose FROM upload_reservations r "
            "JOIN baidu_import_batches b ON "
            "b.quota_reservation_id=r.reservation_id")
        grouped = {}
        for r in rows:
            g = grouped.setdefault(r["state"], {"count": 0, "bytes": 0})
            g["count"] += 1
            g["bytes"] += int(r["reserved_bytes"] or 0)
        reservations = {
            "by_state": grouped,
            "note": "无持有者绑定（< 0072）：按批次 quota_reservation_id 连接"}
    anomalies = []
    for r in rows:
        res_state_by_id[r["reservation_id"]] = r
        hid = r.get("holder_id")
        if r["state"] == "reserved" and hid is not None and hid in terminal_ids:
            anomalies.append({
                "reservation_id": r["reservation_id"],
                "batch_id": hid, "state": r["state"],
                "reserved_bytes": int(r["reserved_bytes"] or 0),
                "reason": "reserved_reservation_on_terminal_batch"})
    # 无绑定旧库的等价异常：批次行指向的预约仍 reserved 而批次已终态
    if not feat["reservation_holder_binding"]:
        for b in batches:
            rid = b.get("quota_reservation_id")
            if rid and b["state"] in _BAIDU_TERMINAL_STATES:
                r = res_state_by_id.get(rid)
                if r and r["state"] == "reserved":
                    anomalies.append({
                        "reservation_id": rid, "batch_id": b["id"],
                        "state": r["state"],
                        "reserved_bytes": int(r["reserved_bytes"] or 0),
                        "reason": "reserved_reservation_on_terminal_batch"})
    return {"counts_by_state": counts, "active": active,
            "terminal_cleanup_obligations": cleanup_obligations,
            "reservations": reservations, "anomalies": anomalies}


def _tree_bytes(path):
    """目录内常规文件总字节（不跟随符号链接；符号链接单独计数）。"""
    total = 0
    links = 0
    for root, dirs, names in os.walk(path, followlinks=False):
        for n in names:
            p = Path(root) / n
            if p.is_symlink():
                links += 1
                continue
            try:
                total += p.stat().st_size
            except OSError:
                continue
        dirs[:] = [d for d in dirs if not (Path(root) / d).is_symlink()]
    return total, links


def _flat_source_names(cur, schema):
    """conversion_job_sources.source_name ∪ conversion_jobs.source_name。"""
    names = {}
    if not schema["features"]["conversion_jobs"]:
        return names
    for j in _q(cur, "SELECT id, source_name FROM conversion_jobs"):
        if j.get("source_name"):
            names.setdefault(j["source_name"], set()).add(j["id"])
    if schema["features"]["conversion_job_sources"]:
        for s in _q(cur, "SELECT job_id, source_name FROM "
                        "conversion_job_sources"):
            if s.get("source_name"):
                names.setdefault(s["source_name"], set()).add(s["job_id"])
    return names


def _section_files(cur, schema, upload_dir, baidu_staging_dir, probe_locks):
    feat = schema["features"]
    upload_dir = Path(upload_dir)
    jobs = {}
    if feat["conversion_jobs"]:
        jobs = {j["id"]: j for j in
                _q(cur, "SELECT * FROM conversion_jobs")}
    flat_names = _flat_source_names(cur, schema)

    out = {"upload_dir": str(upload_dir),
           "baidu_staging_dir": str(baidu_staging_dir)}

    # (a) 每个转换任务的 .staging/<cvj_id>/ 树：source/ + 代次目录
    conv_staging = []
    staging_root = upload_dir / ".staging"
    dir_names = sorted(os.listdir(staging_root)) if staging_root.is_dir() \
        else []
    for jid, job in jobs.items():
        task_dir = staging_root / jid
        if not task_dir.is_dir():
            entry = {"id": jid, "state": job["state"], "dir_present": False}
        else:
            src_dir = task_dir / "source"
            source_present = src_dir.is_dir() and any(
                True for _ in src_dir.iterdir())
            source_bytes = _tree_bytes(src_dir)[0] if src_dir.is_dir() else 0
            attempts = []
            stray = []
            for child in sorted(task_dir.iterdir()):
                if child == src_dir:
                    continue
                if child.is_dir():
                    attempts.append({"name": child.name,
                                     "bytes": _tree_bytes(child)[0]})
                else:
                    stray.append({"name": child.name,
                                  "bytes": child.stat().st_size})
            entry = {"id": jid, "state": job["state"], "dir_present": True,
                     "source_present": source_present,
                     "source_bytes": source_bytes,
                     "attempt_dirs": attempts, "stray_files": stray}
        # 平铺源回退（resolve_source 分支 3：百度 convert 路径写平铺源）
        flats = []
        for name, jids in flat_names.items():
            if jid in jids:
                p = upload_dir / name
                flats.append({"name": name, "exists": p.is_file(),
                              "bytes": p.stat().st_size if p.is_file()
                              else None})
        entry["flat_sources"] = flats
        # 产物包（objects/<slide_id>/）：终态失败任务若仍有完整包在盘——典型
        # 是「FS 发布后、结算前崩溃」被重转 fail-closed（资产 failed、包保留
        # 待人工核对）——是需要处置的存储占用，不计 used_bytes
        sid = (job.get("slide_id") or "").strip() \
            if feat["conversion_slide_id"] else ""
        if sid and job.get("state") == "failed":
            bdir = upload_dir / "objects" / sid
            if bdir.is_dir():
                entry["product_bundle_bytes"] = _tree_bytes(bdir)[0]
        entry["has_any_source"] = bool(entry.get("source_present")) or \
            any(f["exists"] for f in flats)
        state = job["state"]
        if state == "cancelled":
            content = bool(entry.get("source_present")) or \
                bool(entry.get("attempt_dirs")) or bool(entry.get("stray_files"))
            entry["classification"] = "residue_cancelled_job" if content \
                else "clean"
        elif state in _CONVERSION_TERMINAL_STATES:
            if entry.get("attempt_dirs") or entry.get("stray_files"):
                entry["classification"] = "residue_attempt_dir"
            elif not entry["has_any_source"]:
                entry["classification"] = \
                    "source_missing_%s" % state  # failed/ready
            else:
                entry["classification"] = "expected_source_retained"
        else:  # open（held/queued/converting/validating）
            if not entry["has_any_source"]:
                entry["classification"] = ("held_handoff_pending"
                                           if state == "held"
                                           else "source_missing_open")
            elif state == "held":
                entry["classification"] = "held_in_handoff"
            else:
                entry["classification"] = "expected_open"
        conv_staging.append(entry)
    out["conversion_staging"] = conv_staging

    # (b) 引用的平铺源存在性/字节/引用任务状态
    flat_sources = []
    for name, jids in sorted(flat_names.items()):
        p = upload_dir / name
        states = sorted({jobs[j]["state"] for j in jids if j in jobs})
        classification = None
        if p.is_file():
            if states and all(s == "cancelled" for s in states):
                classification = "residue_flat_source"
            else:
                classification = "retained_or_inflight_flat_source"
        else:
            classification = "absent"
        flat_sources.append({"name": name, "exists": p.is_file(),
                             "bytes": p.stat().st_size if p.is_file()
                             else None,
                             "referenced_by_states": states,
                             "classification": classification})
    out["flat_sources"] = flat_sources

    # (c) 百度本地暂存 STAGING_ROOT/<batch_id>/
    baidu_staging = []
    batch_states = {}
    if feat["baidu_tables"]:
        batch_states = {b["id"]: b for b in _q(
            cur, "SELECT id, state, cleanup_state FROM "
            "baidu_import_batches")}
    bs_root = Path(baidu_staging_dir)
    out["baidu_staging_dir_present"] = bs_root.is_dir()
    if bs_root.is_dir():
        for name in sorted(os.listdir(bs_root)):
            d = bs_root / name
            if not d.is_dir():
                continue
            nbytes, links = _tree_bytes(d)
            batch = batch_states.get(name)
            if batch is None:
                cls = "baidu_staging_unknown"
            elif batch["state"] in _BAIDU_TERMINAL_STATES:
                cls = "baidu_staging_residue" if nbytes > 0 else "clean"
            else:
                cls = "inflight_work"
            baidu_staging.append({"dir": name, "bytes": nbytes,
                                  "symlinks": links,
                                  "batch_state":
                                  batch["state"] if batch else None,
                                  "classification": cls})
    out["baidu_staging"] = baidu_staging

    # (d) .staging 下不归属任何已知表 id 的目录
    known = set(jobs)
    if feat["upload_tasks"]:
        known |= {r["upload_id"] for r in
                  _q(cur, "SELECT upload_id FROM upload_tasks")}
    if feat["ingestion_jobs"]:
        known |= {r["job_id"] for r in
                  _q(cur, "SELECT job_id FROM ingestion_jobs")}
    if feat["producer_imports"]:
        known |= {r["import_id"] for r in
                  _q(cur, "SELECT import_id FROM producer_imports")}
    if feat["baidu_tables"]:
        known |= {r["id"] for r in
                  _q(cur, "SELECT id FROM baidu_import_items")}
    if feat["slides_asset_accounting"]:
        known |= {r["slide_id"] for r in _q(
            cur, "SELECT slide_id FROM slides WHERE slide_id IS NOT NULL")}
    unknown = [{"dir": n, "classification": "unknown_staging_dir"}
               for n in dir_names if n not in known]
    out["unknown_staging_dirs"] = unknown

    # (e) 任务存储锁（--probe-locks 时只读探测既有锁文件，绝不创建）
    locks = []
    lock_root = upload_dir / ".task-locks"
    if lock_root.is_dir():
        for kind in _LOCK_KINDS:
            kdir = lock_root / kind
            if not kdir.is_dir():
                continue
            for name in sorted(os.listdir(kdir)):
                if not name.endswith(".lock"):
                    continue
                entry = {"kind": kind, "task_id": name[:-len(".lock")]}
                if probe_locks:
                    fd = None
                    try:
                        fd = os.open(str(kdir / name), os.O_RDONLY)
                        try:
                            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                            fcntl.flock(fd, fcntl.LOCK_UN)
                            entry["holder"] = "free"
                        except OSError as exc:
                            if exc.errno in (errno.EACCES, errno.EAGAIN,
                                             errno.EWOULDBLOCK):
                                entry["holder"] = "live_holder"
                            else:
                                entry["holder"] = "probe_error:%s" % exc.errno
                    except OSError as exc:
                        entry["holder"] = "open_error:%s" % exc.errno
                    finally:
                        if fd is not None:
                            os.close(fd)
                locks.append(entry)
    out["locks"] = locks
    out["probe_locks"] = bool(probe_locks)
    return out


def _slide_states(cur, schema):
    """slide_id → (asset_state, accounted_bytes)（0067 前的旧库 → 空）。"""
    if not schema["features"]["slides_asset_accounting"]:
        return {}
    rows = _q(cur, "SELECT slide_id, asset_state, accounted_bytes, "
                  "storage_layout, owner_user_id FROM slides "
                  "WHERE slide_id IS NOT NULL")
    return {r["slide_id"]: r for r in rows}


def _source_charge_state(job, slides):
    """按转换任务/产物资产行判定源计费的存活形态。

    live：任务 ready 且产物资产 ready/deleting（源计费对应仍在册的产物）；
    charged_never_refundable：任务 cancelled/failed，或产物 deleted/failed/
    缺行——源字节已计入 used_bytes 而产物侧只按 accounted_bytes 退款（源侧
    从不退款，app._slide_delete_settle 只退产物字节），该部分是**永不退款**
    的既成计费（C7 裁决输入）。"""
    if not job:
        return "undetermined"
    state = job.get("state")
    sid = (job.get("slide_id") or "").strip()
    srow = slides.get(sid) if sid else None
    if state == "ready":
        if srow and srow.get("asset_state") in ("ready", "deleting"):
            return "live"
        return "charged_never_refundable"
    if state in ("cancelled", "failed"):
        return "charged_never_refundable"
    return "undetermined"


def _section_charges(cur, schema, files_doc):
    """retained_sources（C7 输入①）+ source_charges（C7 输入②）。

    源字节计费的推导（每条都注明口径与下界性）：

    1. ``ing:<job_id>`` —— COS conversion 形态 ingestion（0075 kind 列）：
       预约 local_reservation_id 在 worker_settle_source 消费（consume，
       settle_bytes=源字节）；bytes 取预约 settled_bytes，缺失时回退
       ingestion_jobs.source_size_bytes（declared，**下界**——按实际下载字
       节结算时可能更大）。
    2. ``upt:<upload_id>`` —— 旧上传任务（V1/V2 finish_commit 按源字节结
       算）：bytes 取预约 settled_bytes；预约行缺失/未结算时该源**不计入**
       （下界）。
    3. ``bib:<batch_id>`` —— 百度批次终态一次性 consume（Σ ready 条目
       source_size）：bytes 取预约 settled_bytes，缺失回退 Σ ready 条目
       source_size（下界——仅当条目在途未计）。conversion_item_bytes 给出
       其中指向转换任务（conversion_job_id 非空）的条目源字节——这是「为
       转换工作收取」的部分；native 条目源字节与其产物 accounted_bytes
       等量（台账自平衡），但 compare 的漂移口径仍按整笔 settled_bytes 计
       （消费当时 used_bytes 增加的是整笔）。

    live / charged_never_refundable 的拆分经 _source_charge_state；百度侧
    仅对 conversion_job_id 非空条目可拆（插件上报的 ready 条目无该列值 →
    undetermined，注明下界）。"""
    feat = schema["features"]
    slides = _slide_states(cur, schema)
    jobs = {j["id"]: j for j in
            _q(cur, "SELECT * FROM conversion_jobs")} \
        if feat["conversion_jobs"] else {}
    jobs_by_upload = {}
    for j in jobs.values():
        if j.get("upload_id"):
            jobs_by_upload.setdefault(j["upload_id"], []).append(j)

    events = []          # compare 漂移口径的稳定事件集
    per_user = {}        # user → {live, charged_never_refundable, ...}

    def _bucket(user):
        return per_user.setdefault(
            user or "",
            {"live": 0, "charged_never_refundable": 0, "undetermined": 0,
             "settled_event_bytes": 0, "events": []})

    # 1. COS conversion 形态 ingestion
    if not (feat["ingestion_jobs"] and feat["ingestion_kinds"]):
        ingestion = _not_applicable(
            "ingestion_jobs/kind 不存在（< 0066/0075；生产 0065 旧库）")
    else:
        rows = _q(
            cur,
            "SELECT j.job_id, j.owner_user_id, j.state, "
            "j.conversion_job_id, j.local_reservation_id, "
            "j.source_size_bytes, r.state AS rstate, r.settled_bytes "
            "FROM ingestion_jobs j LEFT JOIN upload_reservations r ON "
            "r.reservation_id=j.local_reservation_id WHERE j.kind="
            "'conversion' AND r.state='consumed' ORDER BY j.job_id")
        ingestion = {"events": []}
        for r in rows:
            linked = None
            cj = (r.get("conversion_job_id") or "").strip()
            if cj and cj in jobs:
                linked = jobs[cj]
            else:
                for cand in jobs_by_upload.get(r["job_id"], []):
                    linked = cand
                    break
            nbytes = int(r["settled_bytes"]
                         if r["settled_bytes"] is not None
                         else (r.get("source_size_bytes") or 0))
            state = _source_charge_state(linked, slides)
            user = (r.get("owner_user_id") or
                    (linked or {}).get("owner_user_id") or "")
            ev = {"key": "ing:%s" % r["job_id"], "kind": "cos_ingestion",
                  "user_id": user, "bytes": nbytes,
                  "charge_state": state,
                  "fallback_declared": r["settled_bytes"] is None}
            events.append(ev)
            ingestion["events"].append(ev)
            b = _bucket(user)
            b["settled_event_bytes"] += nbytes
            b[state] = b.get(state, 0) + nbytes
            b["events"].append(ev["key"])

    # 2. 旧上传任务（V1/V2 finish_commit 源字节结算）
    if not feat["upload_tasks"]:
        upload_task = _not_applicable("upload_tasks 表不存在（< 0017）")
    else:
        rows = _q(
            cur,
            "SELECT t.upload_id, t.owner_user_id, t.state, "
            "t.reservation_id, r.settled_bytes FROM upload_tasks t "
            "JOIN upload_reservations r ON r.reservation_id=t.reservation_id "
            "WHERE r.state='consumed' ORDER BY t.upload_id")
        upload_task = {"events": []}
        for r in rows:
            linked = jobs_by_upload.get(r["upload_id"], [None])[0]
            if linked is None:
                continue  # 非转换源的上传任务（普通切片直传）
            nbytes = int(r["settled_bytes"] or 0)
            state = _source_charge_state(linked, slides)
            user = (r.get("owner_user_id")
                    or linked.get("owner_user_id") or "")
            ev = {"key": "upt:%s" % r["upload_id"], "kind": "upload_task",
                  "user_id": user, "bytes": nbytes, "charge_state": state}
            events.append(ev)
            upload_task["events"].append(ev)
            b = _bucket(user)
            b["settled_event_bytes"] += nbytes
            b[state] = b.get(state, 0) + nbytes
            b["events"].append(ev["key"])

    # 3. 百度批次
    if not feat["baidu_tables"]:
        baidu = _not_applicable("baidu_import_batches 表不存在（< 0051）")
    else:
        baidu = {"events": [], "note": ""}
        items_by_batch = {}
        for it in _q(cur, "SELECT id, batch_id, stage, source_size, "
                          "conversion_job_id FROM baidu_import_items"):
            items_by_batch.setdefault(it["batch_id"], []).append(it)
        rows = _q(
            cur,
            "SELECT b.id, b.owner_user_id, b.state, "
            "b.quota_reservation_id, r.state AS rstate, r.settled_bytes "
            "FROM baidu_import_batches b LEFT JOIN upload_reservations r ON "
            "r.reservation_id=b.quota_reservation_id ORDER BY b.id")
        for r in rows:
            items = items_by_batch.get(r["id"], [])
            ready_items = [i for i in items if i["stage"] == "ready"]
            if r.get("rstate") != "consumed":
                continue  # 未结算（在途/释放）——不构成源计费事件
            fallback = sum(int(i["source_size"] or 0) for i in ready_items)
            nbytes = int(r["settled_bytes"]
                         if r["settled_bytes"] is not None else fallback)
            conv_bytes = sum(
                int(i["source_size"] or 0) for i in ready_items
                if (i.get("conversion_job_id") or "").strip())
            user = r.get("owner_user_id") or ""
            # 拆分只对 conversion_job_id 非空条目可判定（插件上报条目 →
            # undetermined，值保留在 undetermined 桶——**下界性**注明）
            live = never = undet = 0
            for i in ready_items:
                cj = (i.get("conversion_job_id") or "").strip()
                size = int(i["source_size"] or 0)
                if not cj:
                    undet += size
                    continue
                st = _source_charge_state(jobs.get(cj), slides)
                if st == "live":
                    live += size
                elif st == "charged_never_refundable":
                    never += size
                else:
                    undet += size
            ev = {"key": "bib:%s" % r["id"], "kind": "baidu_batch",
                  "user_id": user, "bytes": nbytes,
                  "conversion_item_bytes": conv_bytes,
                  "fallback_ready_sum": r["settled_bytes"] is None}
            events.append(ev)
            baidu["events"].append(ev)
            b = _bucket(user)
            b["settled_event_bytes"] += nbytes
            b["live"] += live
            b["charged_never_refundable"] += never
            b["undetermined"] += undet
            b["events"].append(ev["key"])
        baidu["note"] = (
            "conversion_item_bytes=指向转换任务的 ready 条目源字节（in-process "
            "路径回填 conversion_job_id，可判定 live/never）；native/插件上报条"
            "目无锚点 → undetermined（下界）。compare 漂移事件按整笔 "
            "settled_bytes 计（consume 当时 used_bytes 增加整笔）。")

    # retained_sources：ready 任务仍在盘上的源（staging source/ 或平铺源）
    # ——字节证据来自 files section（只 stat，不读内容）。
    staging_evidence = {e["id"]: e for e in
                        (files_doc or {}).get("conversion_staging", [])}
    retained = []
    retained_per_user = {}
    if feat["conversion_jobs"]:
        for jid, j in jobs.items():
            if j["state"] != "ready":
                continue
            ev = staging_evidence.get(jid) or {}
            sid = (j.get("slide_id") or "").strip()
            srow = slides.get(sid) if sid else None
            src_bytes = None
            src_kind = None
            if ev.get("source_present"):
                src_bytes = int(ev.get("source_bytes") or 0)
                src_kind = "staging_source"
            else:
                for f in ev.get("flat_sources") or []:
                    if f.get("exists"):
                        src_bytes = int(f["bytes"] or 0)
                        src_kind = "flat:%s" % f["name"]
                        break
            if src_bytes is None:
                continue  # 源已不在盘（被清理/丢失）——不构成 C7 盘点项
            user = j.get("owner_user_id") or ""
            retained_per_user[user] = retained_per_user.get(user, 0) + src_bytes
            retained.append({
                "id": jid, "owner_user_id": user,
                "slide_id": sid or None,
                "product_state": (srow or {}).get("asset_state"),
                "source_kind": src_kind, "source_bytes": src_bytes})
    retained_sources = {
        "jobs": retained,
        "per_user_bytes": retained_per_user,
        "note": ("ready 任务的保留源（staging source/ 或平铺源，字节=盘上 "
                 "stat）——C7 决策输入，非排空阻断；产物删除只按产物 "
                 "accounted_bytes 退款，这些源字节在删除后成为永不退款的"
                 "既成计费"),
    }
    return {"retained_sources": retained_sources,
            "source_charges": {
                "per_user": per_user,
                "cos_ingestion": ingestion,
                "upload_task": upload_task,
                "baidu": baidu,
                "events": events}}


def _section_ledger(cur, schema):
    feat = schema["features"]
    per_user = {}
    for r in _q(cur, "SELECT user_id, quota_bytes, used_bytes, "
                     "reserved_bytes FROM upload_user_quotas"):
        per_user[r["user_id"]] = {
            "user_id": r["user_id"], "quota_bytes": int(r["quota_bytes"]),
            "used_bytes": int(r["used_bytes"]),
            "reserved_bytes": int(r["reserved_bytes"]),
            "slide_accounted_live_bytes": 0, "residual": int(r["used_bytes"])}
    slide_ok = feat["slides_asset_accounting"]
    if slide_ok:
        for r in _q(cur, "SELECT owner_user_id, "
                         "COALESCE(SUM(accounted_bytes),0)::bigint AS bytes "
                         "FROM slides WHERE asset_state IN "
                         "('ready','deleting') AND storage_layout="
                         "'id_bundle' AND owner_user_id IS NOT NULL AND "
                         "owner_user_id <> '' GROUP BY owner_user_id"):
            u = per_user.setdefault(
                r["owner_user_id"],
                {"user_id": r["owner_user_id"], "quota_bytes": 0,
                 "used_bytes": 0, "reserved_bytes": 0,
                 "slide_accounted_live_bytes": 0, "residual": 0})
            u["slide_accounted_live_bytes"] = int(r["bytes"])
    for u in per_user.values():
        u["residual"] = u["used_bytes"] - u["slide_accounted_live_bytes"]

    reservations = []
    if feat["reservation_holder_binding"]:
        reservations = _q(
            cur, "SELECT reservation_id, user_id, state, reserved_bytes, "
            "settled_bytes, holder_kind, holder_id, purpose FROM "
            "upload_reservations ORDER BY reservation_id")
        groups = {}
        for r in _q(cur, "SELECT COALESCE(holder_kind,'(unbound)') AS hk, "
                         "COALESCE(purpose,'-') AS purpose, state, "
                         "COUNT(*)::int AS n, COALESCE(SUM(reserved_bytes),0)"
                         "::bigint AS bytes FROM upload_reservations GROUP "
                         "BY 1,2,3 ORDER BY 1,2,3"):
            groups.setdefault(
                "%s/%s" % (r["hk"], r["purpose"]), {})[r["state"]] = {
                "count": r["n"], "bytes": int(r["bytes"])}
        grouped = {"by_holder_purpose_state": groups}
    else:
        reservations = _q(
            cur, "SELECT reservation_id, user_id, state, reserved_bytes, "
            "settled_bytes, NULL AS holder_kind, NULL AS holder_id, NULL AS "
            "purpose FROM upload_reservations ORDER BY reservation_id")
        groups = {}
        for r in reservations:
            g = groups.setdefault(r["state"], {"count": 0, "bytes": 0})
            g["count"] += 1
            g["bytes"] += int(r["reserved_bytes"] or 0)
        grouped = {"by_state": groups,
                   "note": "无持有者绑定（< 0072）"}

    return {"per_user": sorted(per_user.values(),
                               key=lambda u: u["user_id"]),
            "reservations": reservations,
            "reservation_groups": grouped,
            "slides_accounting": (
                "ok" if slide_ok else
                "not_applicable_schema: slides 无 asset_state/"
                "accounted_bytes（< 0067）——residual 退化为 used_bytes"),
            "residual_definition": (
                "residual = used_bytes − Σ(slides.accounted_bytes where "
                "asset_state IN (ready,deleting) and storage_layout="
                "id_bundle)。产物结算/删除等量移动两侧；残差只应被源字节"
                "结算移动（见模块 docstring compare 不变量）。")}


def _section_reconcile(cur, schema, upload_dir):
    feat = schema["features"]
    needed = [("upload_tasks", feat["upload_tasks"]),
              ("upload_cleanup_pending", feat["upload_cleanup_pending"]),
              ("ingestion_jobs", feat["ingestion_jobs"]),
              ("producer_imports", feat["producer_imports"]),
              ("baidu_import_batches", feat["baidu_tables"]),
              ("upload_reservations.holder_kind",
               feat["reservation_holder_binding"]),
              ("upload_tasks.quota_mode", feat["upload_task_quota_mode"]),
              ("upload_tasks.commit_intent_json",
               feat["upload_task_intent"])]
    missing = [name for name, ok in needed if not ok]
    if missing:
        return _not_applicable(
            "核账依赖缺失（生产 0065 旧库属正常）：%s" % ", ".join(missing))
    try:
        state = recon.collect(cur, upload_dir)
        actions, blockers = recon.plan_actions(state, repair_residuals=False)
    except recon.EvidenceError as exc:
        return {"blockers": [{"kind": "scan", "id": str(exc),
                              "reason": "reconcile_scan_failed"}],
                "actions": []}
    # conversion/baidu/slides 的暂存树归本工具裁决：核账的 unknown_staging_dir
    # 对这些 id 的命中不算核账阻断（与 upload_drain 同口径）。
    foreign = set()
    if feat["conversion_jobs"]:
        foreign |= {r["id"] for r in
                    _q(cur, "SELECT id FROM conversion_jobs")}
    if feat["baidu_tables"]:
        foreign |= {r["id"] for r in
                    _q(cur, "SELECT id FROM baidu_import_items")}
    if feat["slides_asset_accounting"]:
        foreign |= {r["slide_id"] for r in _q(
            cur, "SELECT slide_id FROM slides WHERE slide_id IS NOT NULL")}
    filtered = [b for b in blockers
                if not (b.get("reason") == "unknown_staging_dir"
                        and b.get("id") in foreign)]
    return {"blockers": filtered,
            "actions": [{k: a[k] for k in ("action_key", "action", "kind",
                                           "id")} for a in actions]}


def collect(conn, upload_dir, baidu_staging_dir, probe_locks=False):
    with conn.cursor() as cur:
        schema = detect_schema(cur)
        db_now = _q(cur, "SELECT now() AS now")[0]["now"]
        doc = {
            "meta": {
                "tool_version": TOOL_VERSION,
                "generated_at": _dt.datetime.now(
                    _dt.timezone.utc).isoformat(),
                "db_now": _iso(db_now),
                "schema": schema,
                "dirs": {"upload_dir": str(upload_dir),
                         "baidu_staging_dir": str(baidu_staging_dir)},
                "probe_locks": bool(probe_locks),
                "read_only": True,
            },
            "conversion": _section_conversion(cur, schema, db_now),
            "intents": _section_intents(cur, schema),
            "baidu": _section_baidu(cur, schema, db_now),
            "files": _section_files(cur, schema, upload_dir,
                                    baidu_staging_dir, probe_locks),
        }
        charged = _section_charges(cur, schema, doc["files"])
        doc["retained_sources"] = charged["retained_sources"]
        doc["source_charges"] = charged["source_charges"]
        doc["ledger"] = _section_ledger(cur, schema)
        doc["reconcile"] = _section_reconcile(cur, schema, upload_dir)
        doc["ops_checklist"] = OPS_CHECKLIST
    return doc


# --------------------------------------------------------------------------- #
# report 裁决（blockers / 非阻断清单）
# --------------------------------------------------------------------------- #
def derive_blockers(doc):
    """inventory 文档 → 阻断清单（code + ids + 细节）。纯函数（可测试）。"""
    blockers = []

    conv = doc.get("conversion") or {}
    if not conv.get("not_applicable_schema"):
        open_jobs = conv.get("open_jobs") or []
        if open_jobs:
            blockers.append({
                "code": "conversion_job_open",
                "ids": [j["id"] for j in open_jobs],
                "detail": {j["id"]: j["state"] for j in open_jobs}})

    intents = doc.get("intents") or {}
    iconv = intents.get("conversion") or {}
    if not iconv.get("not_applicable_schema"):
        unres = [j for j in (iconv.get("jobs") or [])
                 if j["state"] == "validating"]
        if unres:
            blockers.append({
                "code": "intent_unresolved", "ids": [j["id"] for j in unres],
                "detail": {j["id"]: j.get("intent_generation")
                           for j in unres}})
    iing = intents.get("ingestion") or {}
    if not iing.get("not_applicable_schema") and iing.get("jobs"):
        # 提交临界段（completing/validating）的 intent 未结算才是阻断——
        # 源字节结算发生在 validating 收口事务；ready（已结算、转换交接
        # 完毕）只作为清单信息（转换子任务责任由 conversion_job_open 裁决）。
        critical = [j for j in iing["jobs"]
                    if j["state"] in ("completing", "validating")]
        if critical:
            blockers.append({
                "code": "ingest_intent_unresolved",
                "ids": [j["job_id"] for j in critical],
                "detail": {j["job_id"]: j["state"] for j in critical}})

    baidu = doc.get("baidu") or {}
    if not baidu.get("not_applicable_schema"):
        live = [b["id"] for b in (baidu.get("active") or [])
                if b["executor"] == "in_process"
                and b["lease_state"] == "live"]
        if live:
            blockers.append({"code": "baidu_in_process_live_lease",
                             "ids": live})
        handoff = [b["id"] for b in (baidu.get("active") or [])
                   if b["executor"] == "in_process"
                   and b["lease_state"] == "expired"
                   and b["non_terminal_items"] > 0]
        if handoff:
            blockers.append({"code": "baidu_handoff_pending",
                             "ids": handoff,
                             "detail": {i: "in-process 租约过期且仍有非终态"
                                          "条目——插件必须接管收口"
                                        for i in handoff}})
        anomalies = baidu.get("anomalies") or []
        if anomalies:
            blockers.append({
                "code": "baidu_reservation_terminal_bound",
                "ids": [a["batch_id"] for a in anomalies]})

    files = doc.get("files") or {}
    residue_attempt = []
    residue_cancelled = []
    missing_open = []
    for e in files.get("conversion_staging") or []:
        cls = e.get("classification")
        if cls == "residue_attempt_dir":
            residue_attempt.append(e["id"])
        elif cls == "residue_cancelled_job":
            residue_cancelled.append(e["id"])
        elif cls == "source_missing_open":
            missing_open.append(e["id"])
    if residue_attempt:
        blockers.append({"code": "residue_attempt_dir",
                         "ids": residue_attempt})
    if residue_cancelled:
        blockers.append({"code": "residue_cancelled_job",
                         "ids": residue_cancelled})
    if missing_open:
        blockers.append({"code": "source_missing_open", "ids": missing_open})
    flat_residue = [f["name"] for f in (files.get("flat_sources") or [])
                    if f.get("classification") == "residue_flat_source"]
    if flat_residue:
        blockers.append({"code": "residue_flat_source", "ids": flat_residue})
    bs_residue = [b["dir"] for b in (files.get("baidu_staging") or [])
                  if b.get("classification") == "baidu_staging_residue"]
    if bs_residue:
        blockers.append({"code": "baidu_staging_residue", "ids": bs_residue})
    bs_unknown = [b["dir"] for b in (files.get("baidu_staging") or [])
                  if b.get("classification") == "baidu_staging_unknown"]
    if bs_unknown:
        blockers.append({"code": "baidu_staging_unknown", "ids": bs_unknown})
    unknown = [d["dir"] for d in (files.get("unknown_staging_dirs") or [])]
    if unknown:
        blockers.append({"code": "unknown_staging_dir", "ids": unknown})
    if files.get("probe_locks"):
        live_locks = ["%s/%s" % (l["kind"], l["task_id"])
                      for l in (files.get("locks") or [])
                      if l.get("holder") == "live_holder"]
        if live_locks:
            blockers.append({"code": "live_lock_holder", "ids": live_locks})

    recon_sec = doc.get("reconcile") or {}
    if not recon_sec.get("not_applicable_schema"):
        for b in recon_sec.get("blockers") or []:
            code = "reconcile_scan_failed" \
                if b.get("reason") == "reconcile_scan_failed" \
                else "reconcile_blocker"
            blockers.append({"code": code, "ids": [str(b.get("id"))],
                             "detail": {"reason": b.get("reason"),
                                        "kind": b.get("kind")}})
    return blockers


def derive_nonblocking(doc):
    """非阻断三类：插件工作 / 决策项（cleanup/decision）/ C7 输入。"""
    baidu = doc.get("baidu") or {}
    plugin_work = [b for b in (baidu.get("active") or [])
                   if b["executor"] in ("plugin", "unclaimed")]
    decisions = []
    for e in (doc.get("files") or {}).get("conversion_staging") or []:
        if e.get("product_bundle_bytes") is not None:
            decisions.append({"code": "failed_product_bundle_present",
                              "id": e["id"],
                              "bytes": int(e["product_bundle_bytes"])})
        if e.get("classification") in ("source_missing_failed",
                                       "source_missing_ready"):
            decisions.append({"code": e["classification"],
                              "id": e["id"]})
        elif e.get("state") == "failed" and e.get("has_any_source"):
            # 退役后没有重试入口：失败任务保留的源成为清理（及计费）义务
            decisions.append({"code": "failed_source_retained",
                              "id": e["id"],
                              "bytes": int(e.get("source_bytes") or 0) + sum(
                                  int(f.get("bytes") or 0)
                                  for f in e.get("flat_sources") or []
                                  if f.get("exists"))})
    iconv = (doc.get("intents") or {}).get("conversion") or {}
    if not iconv.get("not_applicable_schema"):
        for j in iconv.get("jobs") or []:
            if j["state"] != "validating":
                decisions.append({"code": "intent_lingering_%s" % j["state"],
                                  "id": j["id"]})
    assoc = (doc.get("conversion") or {}).get("association_obligations")
    if isinstance(assoc, list):
        for a in assoc:
            decisions.append({"code": "association_%s"
                              % a["project_associate_state"], "id": a["id"]})
    for a in baidu.get("terminal_cleanup_obligations") or []:
        decisions.append({"code": "baidu_remote_cleanup_%s"
                          % a["cleanup_state"], "id": a["id"]})
    c7 = {"retained_sources": doc.get("retained_sources") or {},
          "source_charges_per_user":
              (doc.get("source_charges") or {}).get("per_user") or {}}
    files = doc.get("files") or {}
    if files.get("baidu_staging_dir_present") is False and \
            (baidu.get("counts_by_state") or {}):
        decisions.append({
            "code": "baidu_staging_dir_missing",
            "id": files.get("baidu_staging_dir"),
            "detail": "有百度批次但本地暂存根不存在：确认旧执行器的暂存是否在"
                      "容器 /tmp（未挂卷）里——不可见 ≠ 没有残留"})
    return {"plugin_work": plugin_work, "decision_items": decisions,
            "c7_inputs": c7}


def _print_blockers(blockers):
    if not blockers:
        print("阻断（blockers）：无")
        return
    for b in blockers:
        ids = b["ids"]
        print("阻断: %s ×%d" % (b["code"], len(ids)))
        for i in ids[:20]:
            detail = (b.get("detail") or {}).get(i)
            print("  - %s%s" % (i, "（%s）" % detail if detail else ""))
        if len(ids) > 20:
            print("  …（其余 %d 项略，--json 输出全量）" % (len(ids) - 20))


def _print_nonblocking(nonblock):
    pw = nonblock["plugin_work"]
    print("\n插件工作（queued/无人领取/plugin: 租约——非阻断）：")
    if not pw:
        print("  无")
    for b in pw[:20]:
        print("  - %s executor=%s lease=%s 非终态条目=%d"
              % (b["id"], b["executor"], b["lease_state"],
                 b["non_terminal_items"]))
    print("\n决策项（cleanup/decision——退役窗口裁决，非阻断）：")
    if not nonblock["decision_items"]:
        print("  无")
    for d in nonblock["decision_items"][:20]:
        print("  - %s: %s" % (d["code"], d["id"]))
    c7 = nonblock["c7_inputs"]
    print("\nC7 输入（保留源 / 源字节计费——decision required，非阻断）：")
    for user, total in sorted(
            (c7["retained_sources"].get("per_user_bytes") or {}).items()):
        print("  - 保留源 %s: %d 字节" % (user, total))
    for user, agg in sorted(c7["source_charges_per_user"].items()):
        print("  - 源计费 %s: live=%d never_refundable=%d "
              "undetermined=%d（事件数 %d）"
              % (user, agg.get("live", 0),
                 agg.get("charged_never_refundable", 0),
                 agg.get("undetermined", 0), len(agg.get("events") or [])))


def _print_ops():
    print("\n运维核对项（无法服务端证明）：")
    for item in OPS_CHECKLIST:
        print("  [ ] %s" % item)


def _short_summary(doc):
    meta = doc["meta"]
    schema = meta["schema"]
    print("schema: applied_max=%s（%d 个迁移）features: %s"
          % (schema["applied_max"], schema["applied_count"],
             ",".join(sorted(k for k, v in schema["features"].items()
                             if v)) or "无"))
    conv = doc["conversion"]
    if conv.get("not_applicable_schema"):
        print("conversion: %s" % conv["reason"])
    else:
        print("conversion: %s" % json.dumps(conv["counts_by_state"],
                                            ensure_ascii=False))
    baidu = doc["baidu"]
    if baidu.get("not_applicable_schema"):
        print("baidu: %s" % baidu["reason"])
    else:
        print("baidu: %s" % json.dumps(baidu["counts_by_state"],
                                       ensure_ascii=False))


def _dump(doc, path):
    Path(path).write_text(
        json.dumps(doc, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8")


def _open_conn(args):
    return _connect(args.database_url)


def cmd_inventory(args):
    print("== 转换/百度盘点（inventory，TOOL_VERSION=%s）==" % TOOL_VERSION)
    conn = _open_conn(args)
    try:
        doc = collect(conn, Path(args.upload_dir), Path(args.baidu_staging_dir),
                      probe_locks=args.probe_locks)
    finally:
        conn.close()
    _short_summary(doc)
    if args.json:
        _dump(doc, args.json)
        print("盘点 JSON 已写入 %s" % args.json)
    return 0


def cmd_report(args):
    print("== 转换/百度排空核验（report，TOOL_VERSION=%s）==" % TOOL_VERSION)
    conn = _open_conn(args)
    try:
        doc = collect(conn, Path(args.upload_dir), Path(args.baidu_staging_dir),
                      probe_locks=args.probe_locks)
    finally:
        conn.close()
    _short_summary(doc)
    blockers = derive_blockers(doc)
    nonblock = derive_nonblocking(doc)
    _print_blockers(blockers)
    _print_nonblocking(nonblock)
    _print_ops()
    if args.json:
        _dump({"tool_version": TOOL_VERSION, "inventory": doc,
               "blockers": blockers,
               "nonblocking": {
                   "plugin_work": nonblock["plugin_work"],
                   "decision_items": nonblock["decision_items"]}},
              args.json)
        print("报告 JSON 已写入 %s" % args.json)
    if blockers:
        print("\nreport: no-go（存在未收口责任/残留/漂移；exit 3）")
        return 3
    print("\nreport: go（转换/百度旧链路责任全部收口）")
    return 0


# --------------------------------------------------------------------------- #
# compare：前后快照台账漂移核验
# --------------------------------------------------------------------------- #
def _residuals(doc):
    return {u["user_id"]: u for u in doc["ledger"]["per_user"]}


def _charge_events(doc):
    return {e["key"]: e for e in doc["source_charges"]["events"]}


def compare_docs(before, after):
    """两份 inventory → (漂移用户清单, 预约变化清单)。

    不变量见模块 docstring：drift(user) = Δresidual − Σ新结算源字节事件。
    事件按稳定 key 对账：AFTER 新增的 key 计全额；同 key 字节变化计差量。"""
    res_b, res_a = _residuals(before), _residuals(after)
    ev_b, ev_a = _charge_events(before), _charge_events(after)
    drifts = []
    for user in sorted(set(res_b) | set(res_a)):
        rb = int((res_b.get(user) or {}).get("residual") or 0)
        ra = int((res_a.get(user) or {}).get("residual") or 0)
        new_bytes = 0
        new_events = []
        for key, ev in sorted(ev_a.items()):
            if (ev.get("user_id") or "") != user:
                continue
            prev = ev_b.get(key)
            if prev is None:
                new_bytes += int(ev.get("bytes") or 0)
                new_events.append(key)
            else:
                delta = int(ev.get("bytes") or 0) - int(prev.get("bytes")
                                                       or 0)
                if delta:
                    new_bytes += delta
                    new_events.append("%s(%+d)" % (key, delta))
        drift = (ra - rb) - new_bytes
        if drift != 0:
            drifts.append({"user_id": user,
                           "residual_before": rb, "residual_after": ra,
                           "new_source_bytes": new_bytes,
                           "drift": drift,
                           "new_events": new_events})
    rb_res = {r["reservation_id"]: r
              for r in before["ledger"]["reservations"]}
    ra_res = {r["reservation_id"]: r
              for r in after["ledger"]["reservations"]}
    changes = []
    for rid in sorted(set(rb_res) | set(ra_res)):
        b, a = rb_res.get(rid), ra_res.get(rid)
        if b is None:
            changes.append({"reservation_id": rid, "change": "appeared",
                            "state": a["state"],
                            "holder": [a.get("holder_kind"),
                                       a.get("holder_id")]})
        elif a is None:
            changes.append({"reservation_id": rid, "change": "disappeared",
                            "state_before": b["state"],
                            "holder": [b.get("holder_kind"),
                                       b.get("holder_id")]})
        elif b["state"] != a["state"]:
            changes.append({"reservation_id": rid,
                            "change": "%s→%s" % (b["state"], a["state"]),
                            "holder": [a.get("holder_kind"),
                                       a.get("holder_id")]})
    return drifts, changes


def cmd_compare(args):
    print("== 排空演练台账漂移核验（compare，TOOL_VERSION=%s）=="
          % TOOL_VERSION)
    before = json.loads(Path(args.before).read_text(encoding="utf-8"))
    after = json.loads(Path(args.after).read_text(encoding="utf-8"))
    if before.get("inventory"):
        before = before["inventory"]
    if after.get("inventory"):
        after = after["inventory"]
    drifts, changes = compare_docs(before, after)
    if drifts:
        print("台账漂移（drift != 0）×%d：" % len(drifts))
        for d in drifts:
            print("  - %s: residual %d→%d，新结算源字节 %d，漂移 %+d"
                  "（事件：%s）"
                  % (d["user_id"], d["residual_before"],
                     d["residual_after"], d["new_source_bytes"],
                     d["drift"], ",".join(d["new_events"]) or "无"))
    else:
        print("台账漂移：无（残差变化全部由源字节结算解释）")
    if changes:
        print("预约变化（%d 项）：" % len(changes))
        for c in changes[:20]:
            print("  - %s %s holder=%s"
                  % (c["reservation_id"], c["change"], c["holder"]))
        if len(changes) > 20:
            print("  …（其余 %d 项略）" % (len(changes) - 20))
    else:
        print("预约变化：无")
    if drifts:
        print("\ncompare: no-go（存在未解释台账漂移；exit 3）")
        return 3
    print("\ncompare: 通过（零未解释漂移）")
    return 0


def _baidu_staging_root(arg_dir):
    if arg_dir:
        return Path(arg_dir)
    env = os.environ.get("BAIDU_IMPORT_STAGING_DIR")
    if env:
        return Path(env)
    import tempfile
    return Path(tempfile.gettempdir()) / "baidu-import-staging"


def _upload_root(arg_dir):
    if arg_dir:
        return Path(arg_dir)
    env = os.environ.get("UPLOAD_DIR")
    return Path(env) if env else Path.home() / "svs-viewer" / "uploads"


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("inventory", "report"):
        p = sub.add_parser(name)
        p.add_argument("--upload-dir", default=None,
                       help="UPLOAD_DIR 覆盖（缺省 env/默认）")
        p.add_argument("--baidu-staging-dir", default=None,
                       help="百度本地暂存根覆盖（缺省 env/默认）")
        p.add_argument("--database-url", default=None,
                       help="DATABASE_URL 覆盖（缺省 env）")
        p.add_argument("--json", default=None,
                       help="机器可读输出路径（inventory=盘点文档；"
                            "report=裁决文档）")
        p.add_argument("--probe-locks", action="store_true",
                       help="只读探测 .task-locks 既有锁文件的活跃持有者"
                            "（flock LOCK_EX|LOCK_NB 即取即释；不创建文件）")
    cp = sub.add_parser("compare")
    cp.add_argument("before", help="inventory BEFORE JSON")
    cp.add_argument("after", help="inventory AFTER JSON")
    args = ap.parse_args(argv)
    if args.cmd in ("inventory", "report"):
        args.upload_dir = _upload_root(args.upload_dir)
        args.baidu_staging_dir = _baidu_staging_root(args.baidu_staging_dir)
        if not Path(args.upload_dir).is_dir():
            # 目录不存在时按「没有文件」处理会得出假 GO（残留扫描全空）
            print("UPLOAD_DIR 不存在：%s（用 --upload-dir 指向真实数据目录）"
                  % args.upload_dir, file=sys.stderr)
            return 2
        if args.cmd == "inventory":
            return cmd_inventory(args)
        return cmd_report(args)
    return cmd_compare(args)


if __name__ == "__main__":
    sys.exit(main())
