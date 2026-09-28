#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""旧上传链路排空工具（U4 检查点 A；docs/cos-only-upload-agent-plan-20260928.md §5/U4）。

子命令（同一工具承载切换三步，都是幂等、可重复执行）：

  audit   切换前只读预审：列出将被冻结的 V2 任务与当前未收口责任；
          发现账本漂移/无法解释暂存等**异常**时非零退出（pending 项不算
          异常——切换前存在在途任务是正常状态）。
  freeze  切换时点拍照：active/committing 的 upload_tasks 入
          upload_drain_freeze（幂等，不覆盖既有行；frozen_at 即可信持久
          切换边界）。
  report  排空核验（§5 排空证明）：旧责任全部收口才 exit 0；任何未收口
          pending 或异常 → exit 3（no-go，不猜、不静默放行）。

核验维度（§5）：
  A 旧任务在途（active/committing，含未裁决 commit intent）
  B 持久待清理行（upload_cleanup_pending）
  C 旧任务未释放 reserved（holder=upload_task 的容量责任）
  D 无法解释的 .staging/ 目录（不属于任何已知任务键）
  E 未完成转换责任转交（conversion_jobs 指向在途旧任务）
  F 配账双向一致（upload_user_quotas.reserved ⇋ Σ reserved 预约）
  G drain 模式下清单外出现 active/committing（切换后才可能——异常）

进程内在途（旧请求/worker/子进程仍在写）与浏览器旧恢复记录无法在服务端
证明——部署窗口操作项，见方案 §9 部署顺序；report 输出中列为运维核对项。

用法（独立副本演练同款命令）：
  python3 scripts/upload_drain.py audit [--upload-dir DIR]
  python3 scripts/upload_drain.py freeze
  python3 scripts/upload_drain.py report [--upload-dir DIR] [--json PATH]

退出码：0 = 通过（audit：无异常 / report：go）；3 = report no-go 或
audit 发现异常；2 = 用法/参数错误。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

import psycopg.rows  # noqa: E402

import pg_store  # noqa: E402
import upload_task_store  # noqa: E402

TOOL_VERSION = "u4.1"

#: 运维核对项（无法在服务端证明；部署窗口操作清单）
OPS_CHECKLIST = [
    "旧 V1 在途请求已在停写窗口内结束（边缘停止转发后自然收口）",
    "旧 worker/转换子进程无仍在写 .staging 的进程（ps/日志核对）",
    "浏览器旧恢复记录（localStorage）能识别终态或失效（抽查核对）",
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


# --------------------------------------------------------------------------- #
# 采集（audit / report 共用；全部只读）
# --------------------------------------------------------------------------- #
def collect(upload_root: Path):
    conn = _connect()
    try:
        with conn.cursor() as cur:
            pending_tasks = _q(
                cur,
                "SELECT upload_id, state, owner_user_id, commit_intent_json, "
                "reservation_id, quota_mode FROM upload_tasks "
                "WHERE state IN ('active','committing') ORDER BY upload_id")
            for t in pending_tasks:
                t["commit_intent_open"] = bool(t.get("commit_intent_json"))
            cleanup_backlog = _q(
                cur,
                "SELECT cp.upload_id, cp.reservation_id, cp.updated_at "
                "FROM upload_cleanup_pending cp ORDER BY cp.upload_id")
            reserved_holders = _q(
                cur,
                "SELECT r.reservation_id, r.holder_id, r.reserved_bytes "
                "FROM upload_reservations r WHERE r.state='reserved' "
                "AND r.holder_kind='upload_task' ORDER BY r.holder_id")
            conversion_handoff = _q(
                cur,
                "SELECT c.id, c.state AS cstate, c.upload_id, t.state "
                "AS task_state FROM conversion_jobs c JOIN upload_tasks t "
                "ON t.upload_id = c.upload_id WHERE t.state "
                "IN ('active','committing') ORDER BY c.id")
            frozen = _q(cur, "SELECT upload_id, state_at_freeze, frozen_at "
                             "FROM upload_drain_freeze ORDER BY upload_id")
            unfrozen_active = [t for t in pending_tasks
                               if t["upload_id"] not in
                               {f["upload_id"] for f in frozen}]
            # F：配账双向（R14 同款口径：FULL JOIN 差额）
            ledger = _q(
                cur,
                "SELECT COALESCE(q.user_id, s.user_id) AS user_id, "
                "COALESCE(q.reserved_bytes, 0) AS quota_reserved, "
                "COALESCE(s.reserved_sum, 0) AS ledger_sum "
                "FROM upload_user_quotas q FULL JOIN ("
                "  SELECT user_id, SUM(reserved_bytes)::bigint AS reserved_sum "
                "  FROM upload_reservations WHERE state='reserved' "
                "  GROUP BY user_id) s ON s.user_id = q.user_id "
                "WHERE COALESCE(q.reserved_bytes, 0) <> "
                "COALESCE(s.reserved_sum, 0)")
            # D：无法解释暂存（已知任务键 = 三任务表 + baidu 条目 + slides）
            known = {t["upload_id"] for t in _q(
                cur, "SELECT upload_id FROM upload_tasks")}
            known |= {j["job_id"] for j in _q(
                cur, "SELECT job_id FROM ingestion_jobs")}
            known |= {c["id"] for c in _q(
                cur, "SELECT id FROM conversion_jobs")}
            known |= {b["id"] for b in _q(
                cur, "SELECT id FROM baidu_import_items")}
            known |= {s_["slide_id"] for s_ in _q(
                cur, "SELECT slide_id FROM slides "
                     "WHERE slide_id IS NOT NULL")}
    finally:
        conn.close()
    unexplained = []
    staging = upload_root / ".staging"
    if staging.is_dir():
        for child in staging.iterdir():
            if child.name not in known:
                unexplained.append(child.name)
    return {
        "pending_tasks": pending_tasks,
        "cleanup_backlog": cleanup_backlog,
        "reserved_holders": reserved_holders,
        "conversion_handoff": conversion_handoff,
        "unfrozen_active": unfrozen_active,
        "ledger_drift": ledger,
        "unexplained_staging": unexplained,
        "frozen": frozen,
    }


def _anomalies(data):
    """任何模式下都算异常（切换前也不应存在）。"""
    out = []
    if data["ledger_drift"]:
        out.append(("quota_ledger", data["ledger_drift"]))
    if data["unexplained_staging"]:
        out.append(("staging_unexplained", data["unexplained_staging"]))
    return out


def _pending(data):
    """report 语义下的未收口责任（排空未完成）。"""
    out = []
    if data["pending_tasks"]:
        out.append(("old_task_pending", data["pending_tasks"]))
    if data["cleanup_backlog"]:
        out.append(("cleanup_backlog", data["cleanup_backlog"]))
    if data["reserved_holders"]:
        out.append(("reserved_not_released", data["reserved_holders"]))
    if data["conversion_handoff"]:
        out.append(("conversion_handoff_open", data["conversion_handoff"]))
    if data["unfrozen_active"]:
        out.append(("drain_unfrozen_active", data["unfrozen_active"]))
    return out


def _print_findings(title, findings):
    if not findings:
        print("%s: 无" % title)
        return
    for kind, rows in findings:
        print("%s: %s ×%d" % (title, kind, len(rows)))
        for row in rows[:20]:
            print("  - %s" % json.dumps(row, ensure_ascii=False,
                                        default=str))
        if len(rows) > 20:
            print("  …（其余 %d 项略，--json 输出全量）" % (len(rows) - 20))


# --------------------------------------------------------------------------- #
# 子命令
# --------------------------------------------------------------------------- #
def cmd_audit(args):
    data = collect(_upload_root(args.upload_dir))
    print("== 切换前只读预审（audit，TOOL_VERSION=%s）==" % TOOL_VERSION)
    print("将被 freeze 拍照的在途 V2 任务：%d"
          % len(data["pending_tasks"]))
    for t in data["pending_tasks"][:20]:
        print("  - %s state=%s intent_open=%s" %
              (t["upload_id"], t["state"], t["commit_intent_open"]))
    print("既有冻结清单行：%d" % len(data["frozen"]))
    _print_findings("异常", _anomalies(data))
    print("\n运维核对项（无法服务端证明）：")
    for item in OPS_CHECKLIST:
        print("  [ ] %s" % item)
    if _anomalies(data):
        print("\naudit: 发现异常（非在途责任）→ exit 3")
        return 3
    print("\naudit: 通过（在途责任将随切换/排空收口）")
    return 0


def cmd_freeze(args):
    frozen, already = upload_task_store.freeze_drain_list()
    print("freeze 完成：新冻结 %d 行；清单既有 %d 行（幂等，不覆盖）"
          % (frozen, already))
    print("frozen_at 即可信持久切换边界（检查点 A 构建以 PT_UPLOAD_LEGACY_MODE="
          "drain 进入排空版；检查点 B 构建已删除旧端点，freeze 仅作审计边界）")
    return 0


def cmd_report(args):
    data = collect(_upload_root(args.upload_dir))
    pending = _pending(data)
    anomalies = _anomalies(data)
    print("== 排空核验（report，TOOL_VERSION=%s）==" % TOOL_VERSION)
    _print_findings("未收口", pending)
    _print_findings("异常", anomalies)
    print("\n运维核对项（无法服务端证明）：")
    for item in OPS_CHECKLIST:
        print("  [ ] %s" % item)
    if args.json:
        Path(args.json).write_text(json.dumps({
            "tool_version": TOOL_VERSION,
            "pending": {k: v for k, v in pending},
            "anomalies": {k: v for k, v in anomalies},
            "frozen_count": len(data["frozen"]),
            "ops_checklist": OPS_CHECKLIST,
        }, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        print("报告已写入 %s" % args.json)
    if pending or anomalies:
        print("\nreport: no-go（存在未收口责任或异常；exit 3）")
        return 3
    print("\nreport: go（旧链路责任全部收口，可进入检查点 B 部署评估）")
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
    sub.add_parser("freeze")
    args = ap.parse_args(argv)
    if args.cmd == "audit":
        return cmd_audit(args)
    if args.cmd == "freeze":
        return cmd_freeze(args)
    return cmd_report(args)


if __name__ == "__main__":
    sys.exit(main())
