#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""注册 user 月窗口 → 一次性总额度 受控迁移工具（Batch B；R3 Wave1-Money
瘦身为纯迁移）。

docs review-2026-09-02-upload-user-limits-admin-ui-cleanup.md §Batch B
「迁移与额度语义 1-8」。

用法（只接受显式 actor；数据库 DSN 只从环境变量读——DATABASE_URL 或
PGHOST/PGPORT/PGUSER/PGPASSWORD/PGDATABASE，脚本绝不接受 --dsn）。

**编排（R3 review 停服口径，与脚本内部维护闸 CAS 不是同一层）**：
本脚本的 ``apply`` **要求** ``ai_dispatch_maintenance`` 当前为 false，并以
CAS false→true 自己开闸、成功后 true→false 自己关闸。因此**不能**按「先手工
开维护闸再跑 preflight/apply」——preflight 会把已开闸判为 ``maintenance_active``。
正确顺序是完整停服（停入口流量 + 停旧 Web）后，在无流量窗口对库跑
preflight → apply；脚本内部短暂开/关维护标记不暴露给用户。详见规格文档
§8.1 与 docs/demo-deployment.md「金额单轨 cutover」。

    export DATABASE_URL="postgresql://..."
    python3 scripts/cutover_user_total_allowances.py --mode preflight --actor usr_owner_x
    python3 scripts/cutover_user_total_allowances.py --mode apply      --actor usr_owner_x

两种模式（**永不自动修数据**；任一中止条件命中即整体回滚并保持维护态）：

apply（§Batch B 迁移与额度语义 2-4；R3 Wave1-Money 起无 target CAS——
代码已单轨，apply 只负责**物化数据**：建齐 allowance 行 + 固化默认 X）：
  1. CAS ``ai_dispatch_maintenance`` false→true（compare_and_set_setting；
     未命中 = 已有 cutover 在跑/状态不对 → 中止）；
  2. 持**会话级用户开通锁**（pg_advisory_lock；键与建号/兑换事务内的
     pg_advisory_xact_lock 同为 spend_store.USER_PROVISIONING_ADVISORY_
     LOCK_KEY，两侧互斥串行；全程持有，finally 显式释放——含 _die 的
     SystemExit 路径）；
  3. user open hold 检查：``SELECT ... FROM billing_holds WHERE
     subject_type='user' AND status='open' FOR UPDATE``——非 0 中止并**保持
     维护态**（禁止在线重绑）；
  4. 同一 SERIALIZABLE 事务内：再确认 open hold=0 → 按 user_id 序锁
     ``users(role='user')`` 与各自当前月窗口（UNIQUE 保证至多一个）→
     无当前窗口的 user 按有效策略**先物化零消费窗口**（复用
     spend_store._get_or_create_window_tx；写审计
     spend.cutover_window_materialize；无有效策略中止）→ 逐 user
     建总额度行（limit=current_window.limit_nano_snapshot、opening_spent=
     spent=current_window.spent_nano_cny、reserved=0；已有 allowance 行的
     user（部署后邀请/建号新建）按窗口 spent 回填 opening/spent 并**校验
     限额一致，不一致中止**）→ 提交；因此迁移前后每个 user 的 remaining
     以 nano-CNY 完全相同（月感迁移公式：额度从当前月窗口快照平移到
     一次性总额度行，remaining 逐 user 守恒）；
  5. 提交后再 CAS 关维护闸（true→false）。

只导入当前硬闸窗口，不倒灌历史影子/已关闭月份；记录 source_window_id/
version/cutover_at/actor；旧窗口**冻结不删**。同时在缺行时把全局默认 X
（当时有效 user_default 策略面值）固化为 ai_spend_total_defaults 行
（已存在则不动——不覆盖 owner 决策；0032 迁移也会物化一次，双保险）。

中止条件（全批中止，绝不给 0/默认 X 继续）：任一 user 多个当前窗口 /
无当前窗口且无有效 user 策略（零消费窗物化不了）/ window drift 非 0 /
unpriced usage 非 0 / open hold 非 0 / remaining_before != remaining_after
（异常清单交人工裁决，禁止自动加减修复）。

竞态说明（review R2-F2）：维护闸之外，建号/邀请兑换与 cutover 迁移在
同一批 ``users`` / ``ai_spend_windows`` 行上交错是真实竞态（「建号读
window → cutover 扫描不到未提交用户 → 建号提交授权面」）。apply 在
维护闸成功后全程持**会话级** ``pg_advisory_lock``（键
spend_store.USER_PROVISIONING_ADVISORY_LOCK_KEY，与建号/兑换事务内的
``pg_advisory_xact_lock`` 同键），持锁期间任何建号/兑换事务在取锁处
排队或超时（ProvisioningMaintenanceError → 503）。锁绑定连接，进程
退出/连接关闭自动释放；正常与异常路径（含 _die 的 SystemExit）一律
finally 显式释放。

回滚（R3 Wave1-Money 契约）：**没有在线 rollback-to-window 路径**——
原 rollback-plan 模式已删除；切换出问题 = 恢复 DB 备份 + 回滚旧镜像
（代码恒 total 单轨，无 flag 可切）。

本脚本自身不新增 usage/ledger 分录。
"""

import argparse
import json
import os
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
# 用户开通锁（review R2-F2；与建号/兑换的 xact 锁同键互斥）
# --------------------------------------------------------------------------- #
def _acquire_user_provisioning_lock(conn):
    """持**会话级**用户开通 advisory 锁（维护闸成功后立刻取，全程持有）。

    与建号/兑换事务内的 ``pg_advisory_xact_lock`` 同键
    （spend_store.USER_PROVISIONING_ADVISORY_LOCK_KEY）：持锁期间任何
    建号/兑换事务都会在取锁处排队或超时（ProvisioningMaintenanceError
    → 503），cutover 的用户/窗口扫描与并发建号由此互斥串行。会话级锁
    绑定连接（非事务），取后立即提交归还干净连接——后续迁移事务的
    ``SET TRANSACTION ISOLATION LEVEL`` 必须是事务首语句。进程退出/
    连接关闭自动释放，正常路径仍须 :func:`_release_user_provisioning_lock`
    显式释放。
    """
    with conn.cursor() as cur:
        cur.execute("SELECT pg_advisory_lock(%s)",
                    (spend_store.USER_PROVISIONING_ADVISORY_LOCK_KEY,))
    conn.commit()


def _release_user_provisioning_lock(conn):
    """显式释放会话级开通锁（异常吞掉记 _note；连接关闭兜底释放）。

    解锁前先显式 ``rollback()``：``pg_store.transaction`` 只回滚
    ``Exception``——``_die`` 抛出的 SystemExit（BaseException）会**跳过**
    其回滚分支，把被中断的迁移事务留在连接上；此处先回滚再解锁，保证
    中止路径绝不把半截迁移提交出去（idle 连接上 rollback 为 no-op）。
    """
    try:
        conn.rollback()
        with conn.cursor() as cur:
            cur.execute("SELECT pg_advisory_unlock(%s)",
                        (spend_store.USER_PROVISIONING_ADVISORY_LOCK_KEY,))
        conn.commit()
    except Exception as exc:  # 释放失败不掩盖主流程结果/异常
        _note("警告：用户开通锁释放失败（连接关闭时将自动释放）：%s: %s"
              % (type(exc).__name__, exc))


# --------------------------------------------------------------------------- #
# 检查原语（preflight 与 apply 共用；只读）
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

    返回 ``(user_ids, windows, problems, materialize)``：windows 仍为
    ``{user_id: window_row_with_dt}``；「无当前窗口」不再视为 fatal
    （review R2-F1：window 模式新注册 user 没跑过 AI、没被管理列表读过
    即无窗口行，属常态）——收集进 materialize，由调用方按有效策略物化
    零消费窗口后再迁移；「多个当前窗口」仍为 fatal problem（数据损坏
    语义不变，UNIQUE(subject_type,subject_id,window_start,window_end)
    在同一月内不可能，防御性仍检查）。
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
    materialize = []
    for row in cur.fetchall():
        by_user.setdefault(row["subject_id"], []).append(row)
    for uid in user_ids:
        wins = by_user.get(uid) or []
        if len(wins) > 1:
            problems.append({
                "problem": "current_window_multiple",
                "user_id": uid, "count": len(wins)})
            continue
        if not wins:
            materialize.append(uid)
            continue
        win = dict(wins[0])
        win["_start_dt"] = wins[0]["window_start"]
        win["_end_dt"] = wins[0]["window_end"]
        win["window_start"] = float(win["window_start"].timestamp())
        win["window_end"] = float(win["window_end"].timestamp())
        by_user[uid] = win
    return user_ids, by_user, problems, materialize


def preflight(conn, *, actor, at_dt=None):
    """只读体检：不打维护闸、不改任何行；逐条报告中止条件。

    完整镜像 apply 的静态前置条件（review R2-F3；R3 Wave1-Money 起单轨，
    原 target_setting_missing / target_not_window 两项检查已随
    ``user_spend_target`` 设定键删除而移除）：维护闸已开、默认总额度
    不可解析、物化名单存在无策略 user——均进 problems（ok=false 硬失败，
    不留假绿）。
    """
    at_dt = at_dt or datetime.now(timezone.utc)
    problems = []
    with _tx(conn) as c:
        with c.cursor() as cur:
            maintenance = _scalar(
                cur, "SELECT value FROM platform_settings WHERE key=%s",
                (settings_store.AI_DISPATCH_MAINTENANCE_KEY,))
            if maintenance is True:
                # apply 的维护闸 CAS false→true 必然失败（已是维护态）
                problems.append({"problem": "maintenance_active",
                                 "current": maintenance})
            elif maintenance is not False:
                # 行缺失/非法值：apply 的闸 CAS（WHERE value=false）无法
                # 命中——提前暴露，不留假绿
                problems.append({"problem": "maintenance_setting_invalid",
                                 "current": maintenance})
            holds = check_user_open_holds(cur)
            if holds:
                problems.append({"problem": "user_open_holds",
                                 "count": len(holds),
                                 "hold_ids": [h["hold_id"] for h in holds]})
            cutover = _read_pricing_cutover(cur)
            user_ids, windows, win_problems, materialize = \
                _current_user_windows(cur, at_dt)
            problems.extend(win_problems)
            per_user = []
            for uid in user_ids:
                win = windows.get(uid)
                if not isinstance(win, dict):
                    # 物化名单（无当前窗口）：apply 会先建零消费窗再迁移；
                    # 这里预检「有没有策略可物化」（无 override 也无
                    # user_default → apply 必然中止，preflight 先暴露）
                    policy = spend_store._resolve_policy_tx(
                        cur, "user", uid, at_dt)
                    if policy is None:
                        problems.append({
                            "problem": "window_materialize_no_policy",
                            "user_id": uid})
                    existing = spend_store._fetch_total_allowance_read(
                        cur, uid)
                    per_user.append({
                        "user_id": uid,
                        "window_id": None,
                        "limit_nano_cny": None,
                        "spent_nano_cny": None,
                        "reserved_nano_cny": None,
                        "remaining_nano": None,
                        "existing_allowance_limit_nano_cny": (
                            int(existing["limit_nano_cny"])
                            if existing is not None else None),
                        "window_materialize_required": True,
                    })
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
                    "window_materialize_required": False,
                })
            default_limit, default_source, default_version = \
                spend_store._resolve_total_default_tx(cur, at_dt)
            if default_limit is None:
                # apply 会 _die「无可用默认总额度」——镜像进 problems
                problems.append({"problem": "total_default_unresolvable"})
    report = {
        "mode": "preflight",
        "actor": actor,
        "at": at_dt.isoformat(),
        "ai_dispatch_maintenance": maintenance,
        "users_checked": len(user_ids),
        "windows_to_materialize": len(materialize),
        "pricing_cutover": cutover.isoformat() if cutover else None,
        "total_default_nano_cny": default_limit,
        "total_default_source": default_source,
        "total_default_version": default_version,
        "per_user": per_user,
        "problems": problems,
        "ok": not problems,
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if not report["ok"]:
        _die("preflight 未通过（problems=%d；需为空 problems）"
             % len(problems))
    _note("preflight 通过：%d 个 user 全部就绪" % len(user_ids))
    return report


# --------------------------------------------------------------------------- #
# apply
# --------------------------------------------------------------------------- #
def apply_cutover(conn, *, actor, at_dt=None):
    """受控切换（维护闸 → 会话级开通锁 → open hold 检查 → SERIALIZABLE
    迁移事务 → 关闸）。"""
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
    # 2. 会话级用户开通锁（与建号/兑换事务互斥，全程持有；finally 显式
    #    释放——_die 抛 SystemExit 亦走 finally，绝不带锁异常退出）
    _acquire_user_provisioning_lock(conn)
    try:
        try:
            # 3. user open hold 检查（非 0 中止并保持维护态，禁止在线重绑）
            with _tx(conn) as c:
                with c.cursor() as cur:
                    holds = check_user_open_holds(cur)
            if holds:
                _die("存在 %d 个 user open hold（保持维护态；等待在途结束后"
                     "重跑 apply）：%s" % (
                         len(holds), [h["hold_id"] for h in holds]))
            # 4. SERIALIZABLE 迁移事务
            with _tx(conn) as c:
                with c.cursor() as cur:
                    cur.execute(
                        "SET TRANSACTION ISOLATION LEVEL SERIALIZABLE")
                    holds = check_user_open_holds(cur)
                    if holds:
                        _die("SERIALIZABLE 事务内复检仍有 %d 个 user open "
                             "hold" % len(holds))
                    cutover = _read_pricing_cutover(cur)
                    user_ids, windows, problems, materialize = \
                        _current_user_windows(cur, at_dt)
                    if problems:
                        _die("当前月窗口检查失败：%s" % problems)
                    # 4a. 物化名单：无当前窗口的 user 先按有效策略建零消费
                    #     窗口（review R2-F1：window 模式新注册 user 无窗口
                    #     行是常态，不再阻断 cutover；物化在迁移同事务内、
                    #     过程写审计，随后走下方既有正常迁移）
                    materialized_ids = []
                    for uid in materialize:
                        try:
                            new_win = spend_store._get_or_create_window_tx(
                                cur, "user", uid, at_dt)
                        except spend_store.SpendPolicyMissingError:
                            _die("user %s 无当前窗口且无有效 user 策略"
                                 "（零消费窗口物化不了；中止）：materialize"
                                 "=%s" % (uid, materialize))
                        win = dict(new_win)
                        win["_start_dt"] = datetime.fromtimestamp(
                            float(new_win["window_start"]), tz=timezone.utc)
                        win["_end_dt"] = datetime.fromtimestamp(
                            float(new_win["window_end"]), tz=timezone.utc)
                        windows[uid] = win
                        materialized_ids.append(uid)
                        # 物化写受审计记录（ai_spend_windows 无 updated_by
                        # 列；来源/操作者经 audit_events 留痕）
                        share_store_pg.record_audit_tx(
                            cur, "spend.cutover_window_materialize",
                            actor_user_id=actor, actor_role="owner",
                            target_type="spend_window",
                            target_id=win["window_id"],
                            detail={
                                "op": "cutover_window_materialize",
                                "user_id": uid,
                                "window_id": win["window_id"],
                                "policy_id": win["policy_id"],
                                "policy_version": int(win["policy_version"]),
                                "limit_nano_snapshot":
                                    int(win["limit_nano_snapshot"]),
                            })
                    # 三元组解包（解析出的 version 仅用于报告/日志；allowance 行
                    # 的 default_version 仍以「INSERT ON CONFLICT DO NOTHING
                    # 固化后读行 version」的物化值为准——物化行才是权威模板）
                    default_limit, default_source, default_resolved_version \
                        = spend_store._resolve_total_default_tx(cur, at_dt)
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
                            allowance = \
                                spend_store.create_user_total_allowance_tx(
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
                            "window_materialized":
                                uid in materialized_ids,
                            "allowance_id": str(allowance_id),
                            "allowance_version": int(new_version),
                            "remaining_before_nano": remaining_before,
                            "remaining_after_nano": remaining_after,
                        })
                    # R3 Wave1-Money：原「CAS 切 user_spend_target」步骤已
                    # 删除——代码恒 total 单轨（0032 删除该设定行），apply
                    # 只负责物化数据；迁移事务到此直接提交
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
            "windows_materialized": materialized_ids,
            "total_default_resolved_version": default_resolved_version,
            "per_user": per_user,
            "maintenance_closed": maintenance_closed,
        }
        print(json.dumps(report, ensure_ascii=False, indent=2))
        if not maintenance_closed:
            _note("警告：维护闸关闭 CAS 未命中——请人工核对后显式关闭"
                  "（compare_and_set_setting）")
        _note("apply 完成：%d 个 user 的总额度行已物化（remaining 逐 user "
              "以 nano-CNY 守恒）" % len(per_user))
        return report
    finally:
        _release_user_provisioning_lock(conn)


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Batch B：注册 user 月窗口 → 一次性总额度受控 cutover")
    parser.add_argument("--mode", required=True,
                        choices=("preflight", "apply"))
    parser.add_argument("--actor", required=True,
                        help="执行操作的 owner user_id（写入审计/updated_by）")
    args = parser.parse_args(argv)
    if not (isinstance(args.actor, str) and args.actor.strip()):
        _die("--actor 必须为非空 owner user_id")
    conn = _connect()
    try:
        if args.mode == "preflight":
            preflight(conn, actor=args.actor.strip())
        else:
            apply_cutover(conn, actor=args.actor.strip())
    finally:
        conn.close()


if __name__ == "__main__":
    main()
