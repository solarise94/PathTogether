#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""旧上传链路排空核验工具（检查点 B 部署门禁；R16 修订）。

检查点 A（排空兼容版）已按用户裁决取消：生产在维护窗口内停写、部署 B
（旧 V1/V2 端点已删除）后，由本工具证明旧链路（upload_tasks）责任全部
收口，再开放新上传。旧任务不再有恢复路径——在途旧任务由核账工具
（scripts/reconcile_upload_capacity.py）stop/repair 后经清理收口。

子命令（只读、可重复执行）：

  audit   部署后预审：列出仍在途的旧任务（待收口，不算异常）；发现
          异常时非零退出。
  report  排空核验：旧责任全部收口且无异常才 exit 0；否则 exit 3。

裁决（状态、责任、文件三方关联；「存在任务行」只提供归属线索，不是
残留合法的证据——R16）：

  旧任务在途        upload_tasks active/committing
  持久待清理        upload_cleanup_pending 行
  旧任务未释放容量  holder=upload_task 的 reserved 预约
  转换交接未收口    conversion_jobs 指向在途旧任务
  旧任务暂存残留    任意状态、任意配额身份的旧任务暂存树非空（逐成员
                    证据扫描，与核账工具同一实现；扫描失败=no-go）
  核账阻断/动作     reconcile_upload_capacity 的 collect + plan_actions
                    （配账双向、悬挂预约、ingestion 责任、未知暂存目录）

非上传域暂存（conversion_jobs 的 source 保留、百度导入条目）归各自生命
周期，只列出不裁决。进程内在途与浏览器旧恢复记录无法服务端证明，列为
运维核对项。

用法：
  python3 scripts/upload_drain.py audit  [--upload-dir DIR]
  python3 scripts/upload_drain.py report [--upload-dir DIR] [--json PATH]

退出码：0 = 通过；3 = no-go（report）/ 发现异常（audit）；2 = 用法错误。
"""

from __future__ import annotations

import argparse
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

TOOL_VERSION = "r16.1"

OPS_CHECKLIST = [
    "维护窗口内边缘已停止转发旧上传请求，旧版本进程已全部停止",
    "旧 worker/转换子进程无仍在写 .staging 的进程（ps/日志核对）",
    "所有调用方迁移完成（HistoPilot/脚本无旧通道调用）",
]


def _connect():
    conn = pg_store.connect()
    conn.row_factory = psycopg.rows.dict_row
    return conn


def _q(cur, sql, args=()):
    cur.execute(sql, args)
    return [dict(r) for r in cur.fetchall()]


def _upload_root(arg_dir):
    if arg_dir:
        return Path(arg_dir)
    env = os.environ.get("UPLOAD_DIR")
    return Path(env) if env else Path.home() / "svs-viewer" / "uploads"


def collect(upload_root: Path):
    """只读采集。暂存扫描失败抛 ``recon.EvidenceError``（调用方判 no-go）。"""
    conn = _connect()
    try:
        with conn.cursor() as cur:
            tasks = _q(cur, "SELECT upload_id, state, owner_user_id, "
                            "reservation_id FROM upload_tasks "
                            "ORDER BY upload_id")
            cleanup_backlog = _q(
                cur, "SELECT upload_id, reservation_id, updated_at "
                     "FROM upload_cleanup_pending ORDER BY upload_id")
            reserved_holders = _q(
                cur, "SELECT reservation_id, holder_id, reserved_bytes "
                     "FROM upload_reservations WHERE state='reserved' "
                     "AND holder_kind='upload_task' ORDER BY holder_id")
            conversion_handoff = _q(
                cur, "SELECT c.id, c.state AS cstate, c.upload_id, t.state "
                     "AS task_state FROM conversion_jobs c JOIN upload_tasks t "
                     "ON t.upload_id = c.upload_id WHERE t.state "
                     "IN ('active','committing') ORDER BY c.id")
            foreign_ids = {r["id"] for r in _q(
                cur, "SELECT id FROM conversion_jobs")}
            foreign_ids |= {r["id"] for r in _q(
                cur, "SELECT id FROM baidu_import_items")}
            foreign_ids |= {r["slide_id"] for r in _q(
                cur, "SELECT slide_id FROM slides WHERE slide_id IS NOT NULL")}
            # C5 producer 导入（新通道，非旧链路责任）：audit 清单列出其暂存
            # 树残留证据（逐成员扫描与核账同一实现），不裁决、不进 pending。
            producers = _q(
                cur, "SELECT import_id, state, owner_user_id, "
                     "local_cleanup_status, plugin_cleanup_status "
                     "FROM producer_imports ORDER BY import_id")
            recon_state = recon.collect(cur, upload_root)
    finally:
        conn.close()

    pending_tasks = [t for t in tasks if t["state"] in ("active", "committing")]
    residue = []
    for t in tasks:
        manifest, nfiles, nbytes = recon.scan_task_manifest(
            upload_root, t["upload_id"])
        if manifest:
            residue.append({"upload_id": t["upload_id"], "state": t["state"],
                            "files": nfiles, "bytes": nbytes})
    producer_residue = []
    for p in producers:
        manifest, nfiles, nbytes = recon.scan_task_manifest(
            upload_root, p["import_id"])
        producer_residue.append({
            "import_id": p["import_id"], "state": p["state"],
            "local_cleanup": p["local_cleanup_status"],
            "plugin_cleanup": p["plugin_cleanup_status"],
            "files": nfiles, "bytes": nbytes})
    actions, blockers = recon.plan_actions(recon_state, repair_residuals=False)
    foreign_dirs = [b["id"] for b in blockers
                    if b.get("reason") == "unknown_staging_dir"
                    and b["id"] in foreign_ids]
    blockers = [b for b in blockers
                if not (b.get("reason") == "unknown_staging_dir"
                        and b["id"] in foreign_ids)]
    return {
        "pending_tasks": pending_tasks,
        "cleanup_backlog": cleanup_backlog,
        "reserved_holders": reserved_holders,
        "conversion_handoff": conversion_handoff,
        "old_task_residue": residue,
        "producer_import_residue": producer_residue,
        "reconcile_blockers": blockers,
        "reconcile_actions": [{k: a[k] for k in ("action_key", "action",
                                                  "kind", "id")}
                              for a in actions],
        "foreign_staging": foreign_dirs,
    }


def _anomalies(data):
    """任何时点都算异常（与旧任务是否仍在途无关）。"""
    pending_ids = {t["upload_id"] for t in data["pending_tasks"]}
    out = []
    terminal_residue = [r for r in data["old_task_residue"]
                        if r["upload_id"] not in pending_ids]
    if terminal_residue:
        out.append(("old_task_terminal_residue", terminal_residue))
    if data["reconcile_blockers"]:
        out.append(("reconcile_blockers", data["reconcile_blockers"]))
    if data["reconcile_actions"]:
        out.append(("reconcile_actions", data["reconcile_actions"]))
    return out


def _pending(data):
    """未收口的旧链路责任（排空未完成）。"""
    out = []
    if data["pending_tasks"]:
        out.append(("old_task_pending", data["pending_tasks"]))
    if data["cleanup_backlog"]:
        out.append(("cleanup_backlog", data["cleanup_backlog"]))
    if data["reserved_holders"]:
        out.append(("reserved_not_released", data["reserved_holders"]))
    if data["conversion_handoff"]:
        out.append(("conversion_handoff_open", data["conversion_handoff"]))
    return out


def _print_findings(title, findings):
    if not findings:
        print("%s: 无" % title)
        return
    for kind, rows in findings:
        print("%s: %s ×%d" % (title, kind, len(rows)))
        for row in rows[:20]:
            print("  - %s" % json.dumps(row, ensure_ascii=False, default=str))
        if len(rows) > 20:
            print("  …（其余 %d 项略，--json 输出全量）" % (len(rows) - 20))


def _print_ops(data):
    if data["foreign_staging"]:
        print("\n非上传域暂存（转换/百度/切片生命周期，未裁决）：%d"
              % len(data["foreign_staging"]))
    if data.get("producer_import_residue"):
        print("\nproducer 导入暂存证据（新通道，逐成员扫描，未裁决）：%d"
              % len(data["producer_import_residue"]))
        for row in data["producer_import_residue"][:20]:
            print("  - %s" % json.dumps(row, ensure_ascii=False,
                                       default=str))
        if len(data["producer_import_residue"]) > 20:
            print("  …（其余 %d 项略，--json 输出全量）"
                  % (len(data["producer_import_residue"]) - 20))
    print("\n运维核对项（无法服务端证明）：")
    for item in OPS_CHECKLIST:
        print("  [ ] %s" % item)


def _collect_or_fail(args):
    try:
        return collect(_upload_root(args.upload_dir))
    except recon.EvidenceError as exc:
        print("暂存证据扫描失败（no-go，不把少扫描当没有数据）：%s" % exc)
        return None


def cmd_audit(args):
    print("== 部署后预审（audit，TOOL_VERSION=%s）==" % TOOL_VERSION)
    data = _collect_or_fail(args)
    if data is None:
        return 3
    print("仍在途的旧任务（待核账 stop 与清理收口）：%d"
          % len(data["pending_tasks"]))
    for t in data["pending_tasks"][:20]:
        print("  - %s state=%s" % (t["upload_id"], t["state"]))
    anomalies = _anomalies(data)
    _print_findings("异常", anomalies)
    _print_ops(data)
    if anomalies:
        print("\naudit: 发现异常 → exit 3")
        return 3
    print("\naudit: 通过（在途旧任务仍须收口，见 report）")
    return 0


def cmd_report(args):
    print("== 排空核验（report，TOOL_VERSION=%s）==" % TOOL_VERSION)
    data = _collect_or_fail(args)
    if data is None:
        return 3
    pending = _pending(data)
    anomalies = _anomalies(data)
    _print_findings("未收口", pending)
    _print_findings("异常", anomalies)
    _print_ops(data)
    if args.json:
        Path(args.json).write_text(json.dumps({
            "tool_version": TOOL_VERSION,
            "pending": {k: v for k, v in pending},
            "anomalies": {k: v for k, v in anomalies},
            "foreign_staging": data["foreign_staging"],
            "producer_import_residue": data["producer_import_residue"],
            "ops_checklist": OPS_CHECKLIST,
        }, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        print("报告已写入 %s" % args.json)
    if pending or anomalies:
        print("\nreport: no-go（存在未收口责任或异常；exit 3）")
        return 3
    print("\nreport: go（旧链路责任全部收口）")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("audit", "report"):
        p = sub.add_parser(name)
        p.add_argument("--upload-dir", default=None,
                       help="UPLOAD_DIR 覆盖（缺省 env/默认）")
        p.add_argument("--json", default=None,
                       help="（report）机器可读输出路径")
    args = ap.parse_args(argv)
    if args.cmd == "audit":
        return cmd_audit(args)
    return cmd_report(args)


if __name__ == "__main__":
    sys.exit(main())
