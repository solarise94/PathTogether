#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""注册 user 月窗口 → 一次性总额度 受控 cutover（Batch B）。

docs review-2026-09-02-upload-user-limits-admin-ui-cleanup.md §Batch B
「迁移与额度语义 1-8」+ §8.2 回滚段。

用法（只接受显式 actor；数据库 DSN 只从环境变量读——DATABASE_URL 或
PGHOST/PGPORT/PGUSER/PGPASSWORD/PGDATABASE，脚本绝不接受 --dsn）::

    export DATABASE_URL="postgresql://..."
    python3 scripts/cutover_user_total_allowances.py --mode preflight --actor usr_owner_x
    python3 scripts/cutover_user_total_allowances.py --mode apply      --actor usr_owner_x
    python3 scripts/cutover_user_total_allowances.py --mode rollback-plan --actor usr_owner_x

三种模式（**永不自动修数据**；任一中止条件命中即整体回滚并保持维护态）：

apply（§Batch B 迁移与额度语义 2-4）：
  1. CAS ``ai_dispatch_maintenance`` false→true（compare_and_set_setting；
     未命中 = 已有 cutover 在跑/状态不对 → 中止）；
  2. user open hold 检查：``SELECT ... FROM billing_holds WHERE
     subject_type='user' AND status='open' FOR UPDATE``——非 0 中止并**保持
     维护态**（禁止在线重绑）；
  3. 同一 SERIALIZABLE 事务内：再确认 open hold=0 → 按 user_id 序锁
     ``users(role='user')`` 与各自当前月窗口（UNIQUE 保证至多一个）→ 逐 user
     建总额度行（limit=current_window.limit_nano_snapshot、opening_spent=
     spent=current_window.spent_nano_cny、reserved=0；已有 allowance 行的
     user（部署后邀请/建号新建）按窗口 spent 回填 opening/spent 并**校验
     限额一致，不一致中止**）→ 以 CAS 把 ``user_spend_target`` "window"→
     "total_allowance" → 提交；因此切换前后每个 user 的 remaining 以
     nano-CNY 完全相同；
  4. 提交后再 CAS 关维护闸（true→false）。

只导入当前硬闸窗口，不倒灌历史影子/已关闭月份；记录 source_window_id/
version/cutover_at/actor；旧窗口**冻结不删**。同时在缺行时把全局默认 X
（当时有效 user_default 策略面值）固化为 ai_spend_total_defaults 行
（已存在则不动——不覆盖 owner 决策）。

中止条件（全批中止，绝不给 0/默认 X 继续）：任一 user 无有效当前窗口 /
多个当前窗口 / window drift 非 0 / unpriced usage 非 0 / open hold 非 0 /
remaining_before != remaining_after。

rollback-plan（§8.2；不是切 flag）：
  1. 保持维护态（CAS false→true；已 true 幂等通过）；
  2. 前置检查：reconcile_total_allowances 无 drift、open total holds=0、
     open 的 user 窗口目标 hold=0（否则回滚后 reserved=0 与 reconcile 的
     expected_reserved 不一致）、每个 priced event 恰一条 usage ledger
     （双扣检测）、hold 单目标；
  3. 同一 SERIALIZABLE 事务内：锁 users(role='user')，为每个 user 生成
     **新的**月窗口（边界由服务端 ``window_bounds("calendar_month",
     rollback_at)`` Asia/Shanghai 计算——rollback_at 恒取服务器当前时刻，
     不接受客户端任意时间；不复用已关闭旧窗）——**月感快照**：
     spent=当月 priced usage、limit=spent+max(0, allowance 原剩余)
     （remaining 跨月守恒；已用尽用户压成零剩余饱和窗；当月 priced
     usage > lifetime spent 即数据异常中止）；随后 CAS
     ``user_spend_target`` "total_allowance"→"window"（allowance 冻结为
     非授权审计投影——行保留，仅随 target 切换退出授权路径，不做
     DELETE/清零）→ 提交；
  4. 维护闸**保持开启**：回滚事务提交后需人工验收（reconcile 窗口无
     drift）再显式 CAS 关闸——本脚本不代关（回滚宁保守）。

发现缺账/重复 ledger/金额不等/双 target 立即中止，禁止自动加减修复。
本脚本自身不新增 usage/ledger 分录。
"""

import argparse
import json
import os
import secrets
import sys
from datetime import datetime, timezone

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_SCRIPT_DIR))

import psycopg  # noqa: E402

import billing_pricing  # noqa: E402
import billing_store  # noqa: E402
import pg_store  # noqa: E402
import settings_store  # noqa: E402
import share_store_pg  # noqa: E402
import spend_store  # noqa: E402


def _die(msg, *, code=1):
    print("[cutover] ABORT: %s" % msg, file=sys.stderr)
    sys.exit(code)


def _note(msg):
    print("[cutover] %s" % msg)


def _connect():
    """按环境变量 DSN 建连接（无 DATABASE_URL/PG* 时 fail-fast）。"""
    try:
        pg_store.get_conninfo()
    except RuntimeError as exc:
        _die(str(exc))
    conn = pg_store.connect()
    conn.row_factory = psycopg.rows.dict_row
    return conn


def _tx(conn):
    return pg_store.transaction(conn)


def _scalar(cur, sql, params=()):
    cur.execute(sql, params)
    row = cur.fetchone()
    return row[next(iter(row.keys()))] if row else None


# --------------------------------------------------------------------------- #
# 检查原语（preflight 与 apply/rollback 共用；只读）
# --------------------------------------------------------------------------- #
def check_user_open_holds(cur):
    """user open holds（含 FOR UPDATE 锁序；apply 内复检用）。"""
    cur.execute(
        "SELECT hold_id, call_id, subject_id, status FROM billing_holds "
        "WHERE subject_type='user' AND status='open' ORDER BY hold_id "
        "FOR UPDATE")
    return [dict(r) for r in cur.fetchall()]


def check_window_drift(cur, window, cutover, at_dt):
    """单窗口对账（与 spend_store.reconcile_spend_windows 同口径）。

    返回 ``(spent_drift, reserved_drift, unpriced_count)``。unpriced usage
    非零本身即中止条件（金额事实不完整时不得切换）。
    """
    start_dt, end_dt = window["_start_dt"], window["_end_dt"]
    effective_from = start_dt
    if cutover is not None:
        effective_from = max(effective_from, cutover)
    if window["subject_type"] == "demo":
        subj_where, subj_params = "subject_type='demo'", []
    else:
        subj_where = "subject_type=%s AND subject_id=%s"
        subj_params = [window["subject_type"], window["subject_id"]]
    cur.execute(
        "SELECT COALESCE(SUM(charge_nano_cny), 0)::bigint AS spent "
        "FROM ai_usage_events WHERE status='priced' AND " + subj_where +
        " AND occurred_at >= %s AND occurred_at < %s",
        subj_params + [effective_from, end_dt])
    expected_spent = int(cur.fetchone()["spent"])
    cur.execute(
        "SELECT count(*)::int AS n FROM ai_usage_events "
        "WHERE status='unpriced' AND " + subj_where +
        " AND occurred_at >= %s AND occurred_at < %s",
        subj_params + [effective_from, end_dt])
    unpriced = int(cur.fetchone()["n"])
    cur.execute(
        "SELECT COALESCE(SUM(estimated_nano_cny), 0)::bigint AS reserved "
        "FROM billing_holds WHERE status='open' AND expires_at >= %s "
        "AND " + subj_where +
        " AND created_at >= %s AND created_at < %s "
        "AND estimated_nano_cny IS NOT NULL",
        [at_dt] + subj_params + [start_dt, end_dt])
    expected_reserved = int(cur.fetchone()["reserved"])
    return (int(window["spent_nano_cny"]) - expected_spent,
            int(window["reserved_nano_cny"]) - expected_reserved,
            unpriced)


def _read_pricing_cutover(cur):
    marker = _scalar(
        cur, "SELECT value FROM platform_settings WHERE key=%s",
        (billing_store.PRICING_V2_CUTOVER_SETTING_KEY,))
    if marker is None:
        return None
    return datetime.fromtimestamp(float(marker), tz=timezone.utc)


def _current_user_windows(cur, at_dt):
    """每个 role=user 的当前月窗口（month bounds 由服务端计算）。

    返回 ``{user_id: window_row_with_dt}``；无窗口/多窗口的 user 记入
    problems（多窗口由 UNIQUE(subject_type,subject_id,window_start,
    window_end) 在同一月内不可能，防御性仍检查）。
    """
    start, end = spend_store.month_window_bounds(at_dt)
    cur.execute(
        "SELECT user_id FROM users WHERE role='user' ORDER BY user_id "
        "FOR UPDATE")
    user_ids = [r["user_id"] for r in cur.fetchall()]
    cur.execute(
        "SELECT window_id, policy_id, policy_version, subject_type, "
        "subject_id, window_start, window_end, limit_nano_snapshot, "
        "spent_nano_cny, reserved_nano_cny, status, version "
        "FROM ai_spend_windows WHERE subject_type='user' "
        "AND window_start=%s AND window_end=%s ORDER BY subject_id "
        "FOR UPDATE",
        (start, end))
    by_user = {}
    problems = []
    for row in cur.fetchall():
        by_user.setdefault(row["subject_id"], []).append(row)
    for uid in user_ids:
        wins = by_user.get(uid) or []
        if len(wins) != 1:
            problems.append({
                "problem": "current_window_missing" if not wins
                else "current_window_multiple",
                "user_id": uid, "count": len(wins)})
            continue
        win = dict(wins[0])
        win["_start_dt"] = wins[0]["window_start"]
        win["_end_dt"] = wins[0]["window_end"]
        win["window_start"] = float(win["window_start"].timestamp())
        win["window_end"] = float(win["window_end"].timestamp())
        by_user[uid] = win
    return user_ids, by_user, problems


def preflight(conn, *, actor, at_dt=None):
    """只读体检：不打维护闸、不改任何行；逐条报告中止条件。"""
    at_dt = at_dt or datetime.now(timezone.utc)
    problems = []
    with _tx(conn) as c:
        with c.cursor() as cur:
            target = spend_store.get_user_spend_target_tx(cur)
            maintenance = _scalar(
                cur, "SELECT value FROM platform_settings WHERE key=%s",
                (settings_store.AI_DISPATCH_MAINTENANCE_KEY,))
            holds = check_user_open_holds(cur)
            if holds:
                problems.append({"problem": "user_open_holds",
                                 "count": len(holds),
                                 "hold_ids": [h["hold_id"] for h in holds]})
            cutover = _read_pricing_cutover(cur)
            user_ids, windows, win_problems = _current_user_windows(
                cur, at_dt)
            problems.extend(win_problems)
            per_user = []
            for uid in user_ids:
                win = windows.get(uid)
                if not isinstance(win, dict):
                    continue
                if win["status"] != "open":
                    problems.append({"problem": "window_not_open",
                                     "user_id": uid,
                                     "window_id": win["window_id"]})
                sd, rd, unpriced = check_window_drift(cur, win, cutover,
                                                      at_dt)
                if sd != 0 or rd != 0:
                    problems.append({
                        "problem": "window_drift", "user_id": uid,
                        "window_id": win["window_id"],
                        "spent_drift_nano": sd, "reserved_drift_nano": rd})
                if unpriced != 0:
                    problems.append({"problem": "unpriced_usage",
                                     "user_id": uid, "count": unpriced})
                # F5b 前置预检：已有总额度行的限额必须与当前窗口快照一致
                # （apply 的 existing-allowance 分支同样校验——那时已进维护
                # 窗，preflight 就该把不一致暴露出来）
                existing = spend_store._fetch_total_allowance_read(cur, uid)
                if existing is not None and \
                        int(existing["limit_nano_cny"]) != \
                        int(win["limit_nano_snapshot"]):
                    problems.append({
                        "problem": "allowance_limit_mismatch",
                        "user_id": uid,
                        "allowance_limit_nano":
                            int(existing["limit_nano_cny"]),
                        "window_limit_nano":
                            int(win["limit_nano_snapshot"]),
                    })
                per_user.append({
                    "user_id": uid,
                    "window_id": win["window_id"],
                    "limit_nano_cny": int(win["limit_nano_snapshot"]),
                    "spent_nano_cny": int(win["spent_nano_cny"]),
                    "reserved_nano_cny": int(win["reserved_nano_cny"]),
                    "remaining_nano": (int(win["limit_nano_snapshot"])
                                       - int(win["spent_nano_cny"])
                                       - int(win["reserved_nano_cny"])),
                    "existing_allowance_limit_nano_cny": (
                        int(existing["limit_nano_cny"])
                        if existing is not None else None),
                })
            default_limit, default_source, default_version = \
                spend_store._resolve_total_default_tx(cur, at_dt)
    report = {
        "mode": "preflight",
        "actor": actor,
        "at": at_dt.isoformat(),
        "user_spend_target": target,
        "ai_dispatch_maintenance": maintenance,
        "users_checked": len(user_ids),
        "pricing_cutover": cutover.isoformat() if cutover else None,
        "total_default_nano_cny": default_limit,
        "total_default_source": default_source,
        "total_default_version": default_version,
        "per_user": per_user,
        "problems": problems,
        "ok": not problems and target == "window",
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if not report["ok"]:
        _die("preflight 未通过（problems=%d，user_spend_target=%r；需为空 problems "
             "且 target='window'）" % (len(problems), target))
    _note("preflight 通过：user_spend_target=%s，%d 个 user 全部就绪"
          % (target, len(user_ids)))
    return report


# --------------------------------------------------------------------------- #
# apply
# --------------------------------------------------------------------------- #
def apply_cutover(conn, *, actor, at_dt=None):
    """受控切换（维护闸 → open hold 检查 → SERIALIZABLE 迁移事务 → 关闸）。"""
    at_dt = at_dt or datetime.now(timezone.utc)
    # 1. 维护闸 CAS false→true
    try:
        settings_store.compare_and_set_setting(
            settings_store.AI_DISPATCH_MAINTENANCE_KEY, False, True,
            updated_by="cutover:%s" % actor)
    except settings_store.SettingsVersionConflictError as exc:
        _die("维护闸 CAS 失败（ai_dispatch_maintenance 当前=%r，需为 false"
             "；已有 cutover 在跑或状态被手工改动）" % (exc.context.get(
                 "current"),))
    _note("维护闸已开启（ai_dispatch_maintenance=true）")
    try:
        # 2. user open hold 检查（非 0 中止并保持维护态，禁止在线重绑）
        with _tx(conn) as c:
            with c.cursor() as cur:
                holds = check_user_open_holds(cur)
        if holds:
            _die("存在 %d 个 user open hold（保持维护态；等待在途结束后"
                 "重跑 apply）：%s" % (
                     len(holds), [h["hold_id"] for h in holds]))
        # 3. SERIALIZABLE 迁移事务
        with _tx(conn) as c:
            with c.cursor() as cur:
                cur.execute("SET TRANSACTION ISOLATION LEVEL SERIALIZABLE")
                holds = check_user_open_holds(cur)
                if holds:
                    _die("SERIALIZABLE 事务内复检仍有 %d 个 user open hold"
                          % len(holds))
                cutover = _read_pricing_cutover(cur)
                user_ids, windows, problems = _current_user_windows(cur, at_dt)
                if problems:
                    _die("当前月窗口检查失败：%s" % problems)
                # 三元组解包（解析出的 version 仅用于报告/日志；allowance 行
                # 的 default_version 仍以「INSERT ON CONFLICT DO NOTHING
                # 固化后读行 version」的物化值为准——物化行才是权威模板）
                default_limit, default_source, default_resolved_version = \
                    spend_store._resolve_total_default_tx(cur, at_dt)
                if default_limit is None:
                    _die("无可用默认总额度（defaults 表缺行且 user_default "
                         "策略未配置）——不得给 0/猜测值继续")
                # 缺行才固化默认 X（cutover 时有效 user_default 面值；
                # 已有行不动——不覆盖 owner 决策）
                cur.execute(
                    "INSERT INTO ai_spend_total_defaults "
                    "(singleton, default_limit_nano_cny, version, updated_by) "
                    "VALUES ('global', %s, 1, %s) ON CONFLICT (singleton) "
                    "DO NOTHING RETURNING version",
                    (default_limit, "cutover:%s" % actor))
                seeded = cur.fetchone()
                default_version = int(
                    seeded["version"] if seeded is not None else _scalar(
                        cur, "SELECT version FROM ai_spend_total_defaults "
                        "WHERE singleton='global'"))
                per_user = []
                for uid in user_ids:
                    win = windows[uid]
                    if win["status"] != "open":
                        _die("user %s 当前窗口非 open（%s）"
                             % (uid, win["window_id"]))
                    sd, rd, unpriced = check_window_drift(cur, win, cutover,
                                                          at_dt)
                    if sd != 0 or rd != 0:
                        _die("user %s 窗口 drift 非 0（spent=%+d, "
                             "reserved=%+d）——先排查后重试" % (uid, sd, rd))
                    if unpriced != 0:
                        _die("user %s 存在 %d 条 unpriced usage（金额事实"
                             "不完整，中止）" % (uid, unpriced))
                    remaining_before = (int(win["limit_nano_snapshot"])
                                        - int(win["spent_nano_cny"])
                                        - int(win["reserved_nano_cny"]))
                    existing = spend_store._fetch_total_allowance_read(
                        cur, uid)
                    if existing is not None:
                        # 部署后邀请/建号新建的 user：按窗口 spent 回填
                        # opening/spent；限额必须与当前窗口一致
                        if int(existing["limit_nano_cny"]) != \
                                int(win["limit_nano_snapshot"]):
                            _die("user %s 已有总额度行限额(%s)与当前窗口"
                                 "快照(%s)不一致——人工裁决后重试"
                                 % (uid, existing["limit_nano_cny"],
                                    win["limit_nano_snapshot"]))
                        cur.execute(
                            "UPDATE ai_spend_total_allowances SET "
                            "opening_spent_nano_cny=%s, spent_nano_cny=%s, "
                            "reserved_nano_cny=0, source='cutover', "
                            "default_version=%s, cutover_at=now(), "
                            "source_window_id=%s, "
                            "source_window_version=%s, updated_at=now(), "
                            "updated_by=%s, version=version+1 "
                            "WHERE allowance_id=%s RETURNING version",
                            (int(win["spent_nano_cny"]),
                             int(win["spent_nano_cny"]),
                             default_version,
                             win["window_id"], int(win["version"]),
                             "cutover:%s" % actor,
                             existing["allowance_id"]))
                        row = cur.fetchone()
                        allowance_id = existing["allowance_id"]
                        new_version = int(row["version"])
                    else:
                        # cutover 建行：opening_spent=spent=窗口 spent、
                        # reserved=0；记录源窗口与切换时刻（审计链）
                        allowance = spend_store.create_user_total_allowance_tx(
                            cur, uid, int(win["limit_nano_snapshot"]),
                            source="cutover",
                            default_version=default_version,
                            opening_spent_nano=int(win["spent_nano_cny"]),
                            cutover_at=at_dt,
                            source_window_id=win["window_id"],
                            source_window_version=int(win["version"]),
                            actor_user_id=actor,
                            updated_by="cutover:%s" % actor)
                        allowance_id = allowance["allowance_id"]
                        new_version = allowance["version"]
                    remaining_after = (int(win["limit_nano_snapshot"])
                                       - int(win["spent_nano_cny"]) - 0)
                    if remaining_before != remaining_after:
                        _die("user %s remaining 不守恒（before=%d, "
                             "after=%d）——全批回滚" % (uid, remaining_before,
                                                     remaining_after))
                    per_user.append({
                        "user_id": uid,
                        "allowance_id": str(allowance_id),
                        "allowance_version": int(new_version),
                        "remaining_before_nano": remaining_before,
                        "remaining_after_nano": remaining_after,
                    })
                # 4. CAS 切 target（同事务内单条 UPDATE 比较写入；未命中
                #    整体回滚——维护态保持；无版本 set_setting 禁用于此）
                cur.execute(
                    "UPDATE platform_settings SET value=%s::jsonb, "
                    "updated_at=now(), updated_by=%s "
                    "WHERE key=%s AND value=%s::jsonb RETURNING key",
                    (psycopg.types.json.Jsonb("total_allowance"),
                     "cutover:%s" % actor,
                     settings_store.USER_SPEND_TARGET_KEY,
                     psycopg.types.json.Jsonb("window")))
                if cur.fetchone() is None:
                    _die("user_spend_target CAS 失败（当前值不是 "
                         '"window"）——全批回滚，维护态保持')
    except SystemExit:
        raise
    except Exception as exc:
        _die("迁移事务失败（已整体回滚，维护态保持）：%s: %s"
             % (type(exc).__name__, exc))
    # 5. 提交后关维护闸
    try:
        settings_store.compare_and_set_setting(
            settings_store.AI_DISPATCH_MAINTENANCE_KEY, True, False,
            updated_by="cutover:%s" % actor)
        maintenance_closed = True
    except settings_store.SettingsVersionConflictError:
        maintenance_closed = False
    report = {
        "mode": "apply",
        "actor": actor,
        "at": at_dt.isoformat(),
        "users_migrated": len(per_user),
        "total_default_resolved_version": default_resolved_version,
        "per_user": per_user,
        "user_spend_target": "total_allowance",
        "maintenance_closed": maintenance_closed,
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if not maintenance_closed:
        _note("警告：维护闸关闭 CAS 未命中——请人工核对后显式关闭"
              "（compare_and_set_setting）")
    _note("apply 完成：%d 个 user 已切换到一次性总额度（remaining 逐 user "
          "以 nano-CNY 守恒）" % len(per_user))
    return report


# --------------------------------------------------------------------------- #
# rollback-plan
# --------------------------------------------------------------------------- #
def _double_spend_checks(cur):
    """双扣检测（§8.2 固定口径；发现问题返回清单，不自动修）。"""
    problems = []
    # ① 每个 priced event_id 恰一条事件行
    cur.execute(
        "SELECT event_id, count(*)::int AS n FROM ai_usage_events "
        "WHERE status='priced' GROUP BY event_id HAVING count(*) <> 1")
    for r in cur.fetchall():
        problems.append({"problem": "priced_event_duplicated",
                         "event_id": r["event_id"], "count": int(r["n"])})
    # ② 每个 priced event_id 仍只有一条 'usage:'||event_id ledger
    cur.execute(
        "SELECT e.event_id, count(l.entry_id)::int AS n "
        "FROM ai_usage_events e LEFT JOIN billing_ledger_entries l "
        "ON l.idempotency_key = 'usage:' || e.event_id "
        "WHERE e.status='priced' GROUP BY e.event_id HAVING count(l.entry_id) <> 1")
    for r in cur.fetchall():
        problems.append({
            "problem": "ledger_missing_or_duplicated",
            "event_id": r["event_id"], "ledger_count": int(r["n"])})
    # ③ hold 单目标（DB CHECK 已兜底，防御性复核）
    cur.execute(
        "SELECT count(*)::int AS n FROM billing_holds "
        "WHERE spend_window_id IS NOT NULL "
        "AND spend_total_allowance_id IS NOT NULL")
    n = int(cur.fetchone()["n"])
    if n:
        problems.append({"problem": "hold_dual_target", "count": n})
    return problems


def rollback_plan(conn, *, actor, at_dt=None):
    """回滚规划（§8.2）：不切 flag——先 reconcile/双扣检测，再建新窗并切回。"""
    at_dt = at_dt or datetime.now(timezone.utc)
    # 1. 保持/进入维护态（读后 CAS；已 true 幂等通过，其余值中止）
    with _tx(conn) as c:
        with c.cursor() as cur:
            current_maintenance = _scalar(
                cur, "SELECT value FROM platform_settings WHERE key=%s",
                (settings_store.AI_DISPATCH_MAINTENANCE_KEY,))
    if current_maintenance is True:
        _note("维护闸已处于开启状态（继续）")
    elif current_maintenance is False or current_maintenance is None:
        try:
            settings_store.compare_and_set_setting(
                settings_store.AI_DISPATCH_MAINTENANCE_KEY, False, True,
                updated_by="rollback:%s" % actor)
            _note("维护闸已开启（保持维护态）")
        except settings_store.SettingsVersionConflictError:
            _die("维护闸 CAS 失败（并发改动；重跑 rollback-plan）")
    else:
        _die("ai_dispatch_maintenance 存量值非法（%r）——人工核查"
             % (current_maintenance,))
    try:
        # 2. 前置检查（只读）
        with _tx(conn) as c:
            with c.cursor() as cur:
                recon = spend_store.reconcile_total_allowances(at=at_dt)
                drift = [i for i in recon["items"] if not i["matches"]]
                if drift:
                    _die("总额度 reconcile 有 %d 条 drift（先排查后重试）："
                         "%s" % (len(drift), drift[:5]))
                cur.execute(
                    "SELECT hold_id FROM billing_holds "
                    "WHERE status='open' AND spend_total_allowance_id "
                    "IS NOT NULL ORDER BY hold_id")
                open_total = [r["hold_id"] for r in cur.fetchall()]
                if open_total:
                    _die("存在 %d 个 open 总额度 hold（回滚前必须为 0）：%s"
                         % (len(open_total), open_total))
                # open 的 user 窗口目标 hold 也必须为 0：回滚后新窗
                # reserved 恒 0，此类行会让 reconcile 的 expected_reserved
                # 非 0（必然 drift）。holds 只经 spend_window_id 关联窗口
                # 表（无窗口主体列），join ai_spend_windows 判定 user 窗口；
                # total 模式下此类行不应存在（user hold 只绑 allowance），
                # 查询结果应为空集——作为防御性中止条件保留。
                cur.execute(
                    "SELECT h.hold_id FROM billing_holds h "
                    "JOIN ai_spend_windows w "
                    "ON w.window_id = h.spend_window_id "
                    "WHERE h.status='open' AND h.spend_window_id IS NOT NULL "
                    "AND w.subject_type='user' ORDER BY h.hold_id")
                open_window_user = [r["hold_id"] for r in cur.fetchall()]
                if open_window_user:
                    _die("存在 %d 个 open 的 user 窗口目标 hold（回滚后新窗 "
                         "reserved=0 将与 reconcile expected_reserved 不一致"
                         "；回滚前必须为 0）：%s"
                         % (len(open_window_user), open_window_user[:5]))
                problems = _double_spend_checks(cur)
                if problems:
                    _die("双扣/缺账检测发现 %d 个异常（禁止自动修）：%s"
                         % (len(problems), problems[:5]))
        # 3. SERIALIZABLE：建新窗 + 冻结 allowance（投影退出授权）+ CAS 切回
        with _tx(conn) as c:
            with c.cursor() as cur:
                cur.execute("SET TRANSACTION ISOLATION LEVEL SERIALIZABLE")
                start, end = spend_store.month_window_bounds(at_dt)
                # 月感快照的 usage 下限与 reconcile 口径对齐：
                # max(窗口起点, pricing cutover)——旧错误价格的影子事件不进
                # 窗口对账（spend_store._reconcile_window_tx 同口径），否则
                # 回滚月与 pricing cutover 同月且窗内有旧价事件时必然 drift
                pricing_cutover = _read_pricing_cutover(cur)
                spend_from = (max(start, pricing_cutover)
                              if pricing_cutover is not None else start)
                cur.execute(
                    "SELECT user_id FROM users WHERE role='user' "
                    "ORDER BY user_id FOR UPDATE")
                user_ids = [r["user_id"] for r in cur.fetchall()]
                per_user = []
                for uid in user_ids:
                    cur.execute(
                        "SELECT " + spend_store._ALLOWANCE_SEL +
                        " FROM ai_spend_total_allowances "
                        "WHERE subject_id=%s FOR UPDATE", (uid,))
                    arow = cur.fetchone()
                    if arow is None:
                        # 无 allowance 行（cutover 前从未来过/异常）：记入
                        # 报告但不为其造窗（窗口路径自身按策略解析）
                        per_user.append({"user_id": uid,
                                         "skipped": "no_allowance"})
                        continue
                    allowance = spend_store._allowance_out(arow)
                    if int(allowance["reserved_nano_cny"]) != 0:
                        _die("user %s allowance reserved 非 0（中止）" % uid)
                    # 新月窗口月感快照（值见下方 F3 公式）：reserved=0；
                    # 边界由服务端 window_bounds("calendar_month",
                    # rollback_at) 计算。同月回滚时与冻结旧窗边界重合
                    # （UNIQUE 拒绝第二行）：此时对既有行做受审计的就地
                    # 快照更新（它从未 closed、也不是复用已关闭旧窗——值
                    # 刷新为月感快照并 version+1 留痕）；跨月回滚则插入
                    # 全新行。两分支共用同一月感公式。
                    policy = spend_store._resolve_policy_tx(
                        cur, "user", uid, at_dt)
                    if policy is None:
                        _die("user %s 回滚时刻无有效 user 策略（新窗 FK "
                             "无法满足；中止）" % uid)
                    cur.execute(
                        "SELECT window_id, status, spent_nano_cny, "
                        "limit_nano_snapshot, version FROM ai_spend_windows "
                        "WHERE subject_type='user' AND subject_id=%s "
                        "AND window_start=%s AND window_end=%s FOR UPDATE",
                        (uid, start, end))
                    existing_win = cur.fetchone()
                    # 月感快照（F3）：新月窗口 spent=当月 priced usage、
                    # limit=spent+max(0, allowance 原剩余)——回滚后
                    # reconcile_spend_windows 只 SUM 窗口界内 priced usage，
                    # 整体照抄 lifetime 值跨月必现 drift。remaining
                    # （target_limit - target_spent = max(0, 原剩余)）跨月
                    # 守恒；已用尽/超支用户压成零剩余饱和窗。同月回滚时当月
                    # usage 即全部 usage，结果与「整体照抄」旧口径一致。
                    cur.execute(
                        "SELECT COALESCE(SUM(charge_nano_cny),0)::bigint AS s "
                        "FROM ai_usage_events WHERE status='priced' AND "
                        "subject_type='user' AND subject_id=%s "
                        "AND occurred_at >= %s AND occurred_at < %s",
                        (uid, spend_from, end))
                    month_spent = int(cur.fetchone()["s"])
                    life_spent = int(allowance["spent_nano_cny"])
                    life_limit = int(allowance["limit_nano_cny"])
                    if life_spent < month_spent:
                        _die("user %s 数据异常：当月 priced usage(%d) > "
                             "allowance lifetime spent(%d)——人工核查"
                             % (uid, month_spent, life_spent))
                    remaining = life_limit - life_spent
                    if remaining > 0:
                        target_limit = life_limit - (life_spent - month_spent)
                        target_spent = month_spent
                    else:
                        target_limit = month_spent
                        target_spent = month_spent
                    if existing_win is not None:
                        if existing_win["status"] != "open":
                            _die("user %s 回滚目标月存在已关闭旧窗（禁止"
                                 "复用；中止）" % uid)
                        cur.execute(
                            "UPDATE ai_spend_windows SET "
                            "policy_id=%s, policy_version=%s, "
                            "limit_nano_snapshot=%s, spent_nano_cny=%s, "
                            "reserved_nano_cny=0, version=version+1, "
                            "updated_at=now() "
                            "WHERE window_id=%s RETURNING window_id",
                            (policy["policy_id"], int(policy["version"]),
                             target_limit, target_spent,
                             existing_win["window_id"]))
                        new_window_id = cur.fetchone()["window_id"]
                        rolled_mode = "in_place_resnapshot"
                    else:
                        cur.execute(
                            "INSERT INTO ai_spend_windows "
                            "(window_id, policy_id, policy_version, "
                            " subject_type, subject_id, window_start, "
                            " window_end, limit_nano_snapshot, "
                            " spent_nano_cny, reserved_nano_cny, status, "
                            " version) "
                            "VALUES (%s,%s,%s,'user',%s,%s,%s,%s,%s,0,"
                            "'open',1) RETURNING window_id",
                            ("spw_" + secrets.token_hex(12),
                             policy["policy_id"], int(policy["version"]),
                             uid, start, end, target_limit, target_spent))
                        new_window_id = cur.fetchone()["window_id"]
                        rolled_mode = "new_window"
                    # 回滚窗口写受审计记录（ai_spend_windows 无 updated_by
                    # 列；来源/操作者经 audit_events 留痕）
                    share_store_pg.record_audit_tx(
                        cur, "spend.rollback_window",
                        actor_user_id=actor, actor_role="owner",
                        target_type="spend_window", target_id=new_window_id,
                        detail={
                            "op": "rollback_window_resnapshot",
                            "user_id": uid,
                            "mode": rolled_mode,
                            "allowance_id": allowance["allowance_id"],
                            "limit_nano_cny": target_limit,
                            "spent_nano_cny": target_spent,
                            "month_spent_nano_cny": month_spent,
                            "lifetime_spent_nano_cny": life_spent,
                        })
                    # 双扣检测口径：新窗初始 spent/limit 必须精确等于月感
                    # 快照目标（读回两列复核；不等即中止，不自动加减修复）
                    cur.execute(
                        "SELECT spent_nano_cny, limit_nano_snapshot "
                        "FROM ai_spend_windows WHERE window_id=%s",
                        (new_window_id,))
                    win_row = cur.fetchone()
                    if (int(win_row["spent_nano_cny"]) != target_spent
                            or int(win_row["limit_nano_snapshot"])
                            != target_limit):
                        _die("user %s 新窗 spent/limit != 月感快照目标"
                             "（spent 目标 %d，limit 目标 %d；中止）"
                             % (uid, target_spent, target_limit))
                    # 冻结 allowance 为审计投影：reserved 归零（前置已保证
                    # open total hold=0）、version+1 记账；不 DELETE、不清
                    # opening/spent（历史事实保留）
                    cur.execute(
                        "UPDATE ai_spend_total_allowances SET "
                        "reserved_nano_cny=0, version=version+1, "
                        "updated_at=now(), updated_by=%s "
                        "WHERE allowance_id=%s",
                        ("rollback:%s" % actor, allowance["allowance_id"]))
                    per_user.append({
                        "user_id": uid,
                        "allowance_id": allowance["allowance_id"],
                        "new_window_id": new_window_id,
                        "rolled_mode": rolled_mode,
                        "new_window_limit_nano_cny": target_limit,
                        "new_window_spent_nano_cny": target_spent,
                        "month_spent_nano_cny": month_spent,
                        "lifetime_spent_nano_cny": life_spent,
                    })
                cur.execute(
                    "UPDATE platform_settings SET value=%s::jsonb, "
                    "updated_at=now(), updated_by=%s "
                    "WHERE key=%s AND value=%s::jsonb RETURNING key",
                    (psycopg.types.json.Jsonb("window"),
                     "rollback:%s" % actor,
                     settings_store.USER_SPEND_TARGET_KEY,
                     psycopg.types.json.Jsonb("total_allowance")))
                if cur.fetchone() is None:
                    _die('user_spend_target CAS 失败（当前值不是 '
                         '"total_allowance"）——整体回滚')
    except SystemExit:
        raise
    except Exception as exc:
        _die("回滚事务失败（已整体回滚，维护态保持）：%s: %s"
             % (type(exc).__name__, exc))
    report = {
        "mode": "rollback-plan",
        "actor": actor,
        "at": at_dt.isoformat(),
        "rollback_at_note": "窗口边界由服务端按 Asia/Shanghai 自然月计算",
        "users": per_user,
        "user_spend_target": "window",
        "maintenance": "kept_open",
        "note": "回滚事务已提交；维护闸保持开启，人工验收（窗口 reconcile "
                "无 drift）后请显式 CAS 关闭 ai_dispatch_maintenance",
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))
    _note("rollback-plan 完成：user_spend_target 已切回 window；维护闸保持"
          "开启（人工验收后关闭）")
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Batch B：注册 user 月窗口 → 一次性总额度受控 cutover")
    parser.add_argument("--mode", required=True,
                        choices=("preflight", "apply", "rollback-plan"))
    parser.add_argument("--actor", required=True,
                        help="执行操作的 owner user_id（写入审计/updated_by）")
    args = parser.parse_args(argv)
    if not (isinstance(args.actor, str) and args.actor.strip()):
        _die("--actor 必须为非空 owner user_id")
    conn = _connect()
    try:
        if args.mode == "preflight":
            preflight(conn, actor=args.actor.strip())
        elif args.mode == "apply":
            apply_cutover(conn, actor=args.actor.strip())
        else:
            rollback_plan(conn, actor=args.actor.strip())
    finally:
        conn.close()


if __name__ == "__main__":
    main()
