# -*- coding: utf-8 -*-
"""研究副本删除执行器（docs/agent-plan-20260921-registration-consent-research.md
§6.3/§8；P1 修复：0062 只落任务，本模块是执行器）。

独立进程（与 registration_mail_worker / conversion_worker 同款装配）：

    python research_deletion_worker.py --loop          # 生产（docker_entry.sh）
    python research_deletion_worker.py --once          # 部署钩子/测试单轮

职责（只清**研究副本层**，绝不触碰业务切片/标注/临床记录，§3.5）：

1. ``drain_once``：处理 ``research_data_deletion_jobs`` 待执行任务
   （reason=withdrawal / user_request 均执行），状态机
   pending→running→completed/failed：
   - 领取：``SELECT ... FOR UPDATE SKIP LOCKED``——多 worker 同表轮询互不
     重复（行锁只在单任务处理期间持有）；running 超租约
     （``RESEARCH_DELETION_LEASE_SECONDS``，默认 900s）未更新视为主持
     worker 崩溃，可被回收重领（重启安全）；
   - 执行：单事务删除该 user / ``consent_epoch <= job.consent_epoch`` 的
     研究副本，按 §6.2 数据来源链——research_viewer_events（随会话级联）→
     research_conversation_items → research_viewing_sessions → 无剩余引用
     时删 research_subjects 伪名映射（再授权后产生**新** subject，不复活
     旧副本）。删除幂等：重复执行删除 0 行后正常落 completed；
   - 失败：status=failed + attempts+1 + 安全错误码（只记异常**类名**，不落
     明文堆栈/SQL/身份），指数退避 ``next_retry_at``；
     attempts 达 ``research_consent_store.MAX_DELETION_ATTEMPTS`` 后 failed
     为终态不再自动重试（人工按 error_code 处置；任务行保留为备份恢复
     时的重放清单）。终态 failed 只是**停止重试**，不解除研究侧阻断。
   - 成功：status=completed、completed_at、online_cleared/exports_cleared
     =TRUE（在线副本已删；首版无导出/工作副本通道）。``backups_pending``
     保持 TRUE：仓库无隔离备份子系统可主动清，§8 约束隔离备份最长 30 天
     轮换、恢复时必须先应用本任务清单——任务行本身即恢复重放凭据。
2. ``purge_expired_research_once``：§8「研究动作/对话副本每条最多 90 天，
   到期删除」——按 expires_at 删除 research_viewer_events /
   research_conversation_items / research_viewing_sessions（会话最后删，
   级联残余事件）。纯幂等 DELETE，多实例重叠调度安全。

阻断权威不在这里：删除义务未了结（completed 之外——pending/running、
退避重试中的 failed 与达上限的终态 failed）期间
``research_consent_store.evaluate_research_access`` /
``research_store._assert_ingest_allowed`` 以
``UNRESOLVED_DELETION_JOBS_SQL`` 持续阻断研究采集/使用；**只有 completed
解除阻断**（deletion_pending 不再出现）。终态 failed 期间 worker 不再领取
（停止自动重试），但研究侧仍以 deletion_pending 阻断、等待人工按
error_code 处置——「停止自动重试」与「解除研究限制」是分开的两件事；
等待人工处置不影响用户正常读片/业务（阻断只作用于研究采集/读取侧）。

日志纪律：只记 job_id（随机 token）、行数与错误类别；绝不记 user_id/
subject_id/账号/切片名/异常原文。
"""

import argparse
import logging
import os
import sys
import time

if __package__ in (None, ""):  # 脚本直跑（docker_entry.sh：python3 /app/...）
    _REPO = os.path.dirname(os.path.abspath(__file__))
    if _REPO not in sys.path:
        sys.path.insert(0, _REPO)

import psycopg  # noqa: E402

import pg_store  # noqa: E402
import research_consent_store  # noqa: E402

_log = logging.getLogger("svs.research_deletion")

#: 单轮排水任务数上限（每任务独立事务，避免长事务跨任务）
_DRAIN_BATCH = 50

#: 到期清理单表单轮删除上限（分批避免长锁；下轮继续）
_PURGE_BATCH = 500

#: running 租约（秒）：超时未更新视为主持 worker 崩溃，可回收重领
_LEASE_SECONDS_DEFAULT = 900

#: 失败退避基数（秒）：第 n 次尝试失败后退避 base * 2^(n-1)
_RETRY_BACKOFF_BASE_SECONDS_DEFAULT = 30


def _int_env(name, default, environ=None):
    env = os.environ if environ is None else environ
    try:
        return int(env.get(name) or default)
    except (TypeError, ValueError):
        return default


def _lease_seconds(environ=None):
    return max(1, _int_env("RESEARCH_DELETION_LEASE_SECONDS",
                           _LEASE_SECONDS_DEFAULT, environ))


def _retry_base_seconds(environ=None):
    return max(0, _int_env("RESEARCH_DELETION_RETRY_BASE_SECONDS",
                           _RETRY_BACKOFF_BASE_SECONDS_DEFAULT, environ))


def _connect():
    conn = pg_store.connect()
    conn.row_factory = psycopg.rows.dict_row
    return conn


# --------------------------------------------------------------------------- #
# 领取（FOR UPDATE SKIP LOCKED：多 worker 互不重复；running 超租约回收）
# --------------------------------------------------------------------------- #
def _claim_next_job(conn, lease_seconds) -> dict | None:
    """领取一条待执行任务并置 running（独立事务提交，租约起点=updated_at）。

    可领取范围：

    - pending 且 ``next_retry_at <= now()``（新任务/退避期满的失败任务；
      failed 重试需 ``attempts < MAX``）；
    - running 且 ``updated_at < now() - 租约``（主持 worker 崩溃回收——
      删除幂等，重领重删无害）。

    返回 ``{job_id, user_id, consent_epoch, attempts}`` 或 None；None 时
    本轮无活。
    """
    with pg_store.transaction(conn) as tx:
        with tx.cursor() as cur:
            cur.execute(
                "SELECT job_id, user_id, consent_epoch, attempts "
                "FROM research_data_deletion_jobs "
                "WHERE ((status='pending' AND next_retry_at <= now()) "
                "       OR (status='failed' AND attempts < %s "
                "           AND next_retry_at <= now()) "
                "       OR (status='running' "
                "           AND updated_at < now() - make_interval(secs=>%s))) "
                "ORDER BY created_at, job_id LIMIT 1 FOR UPDATE SKIP LOCKED",
                (research_consent_store.MAX_DELETION_ATTEMPTS, lease_seconds))
            row = cur.fetchone()
            if row is None:
                return None
            cur.execute(
                "UPDATE research_data_deletion_jobs "
                "SET status='running', attempts=attempts+1, updated_at=now() "
                "WHERE job_id=%s", (row["job_id"],))
            return {"job_id": row["job_id"], "user_id": row["user_id"],
                    "consent_epoch": row["consent_epoch"],
                    "attempts": int(row["attempts"] or 0) + 1}


# --------------------------------------------------------------------------- #
# 执行：按 §6.2 数据来源链删除研究副本（单事务、幂等）
# --------------------------------------------------------------------------- #
def _delete_research_copies(conn, user_id, consent_epoch) -> dict:
    """删除该用户 ``consent_epoch <= consent_epoch`` 的全部研究副本。

    谓词按 job.consent_epoch 封顶：withdrawal 任务的 epoch=撤回后新 epoch，
    删除覆盖此前全部授权期副本；user_request 任务的 epoch=申请时当前
    epoch，即「删除本人现有研究副本」。执行期间 ingestion 被
    deletion_pending 阻断，不存在竞态写入更大 epoch 的副本；再授权后的
    新 epoch 副本不在本任务删除范围（§6.3：再同意从此后新数据开始）。

    只 DELETE research_* 表——slides/rois/标注/临床记录一概不触碰（§3.5）。
    返回分类计数（日志/测试用；不含任何身份信息）。
    """
    with pg_store.transaction(conn) as tx:
        with tx.cursor() as cur:
            cur.execute("SELECT subject_id FROM research_subjects "
                        "WHERE user_id=%s", (user_id,))
            subjects = [r["subject_id"] for r in cur.fetchall()]
            counts = {"events": 0, "conversation_items": 0, "sessions": 0,
                      "subjects": 0}
            if not subjects:
                return counts  # 从未有研究副本（如从未授权的 user_request）
            # 1) 事件（显式删而非只靠级联：会话谓词与事件谓词同源，行为可测）
            cur.execute(
                "DELETE FROM research_viewer_events e USING "
                "research_viewing_sessions s "
                "WHERE e.session_id=s.session_id AND s.subject_id = ANY(%s) "
                "AND s.consent_epoch <= %s", (subjects, consent_epoch))
            counts["events"] = cur.rowcount
            # 2) 对话研究副本
            cur.execute(
                "DELETE FROM research_conversation_items "
                "WHERE subject_id = ANY(%s) AND consent_epoch <= %s",
                (subjects, consent_epoch))
            counts["conversation_items"] = cur.rowcount
            # 3) 读片会话（其残余事件随 FK CASCADE）
            cur.execute(
                "DELETE FROM research_viewing_sessions "
                "WHERE subject_id = ANY(%s) AND consent_epoch <= %s",
                (subjects, consent_epoch))
            counts["sessions"] = cur.rowcount
            # 4) 伪名映射：无剩余会话/对话引用才删（再授权会产生新 subject）
            cur.execute(
                "DELETE FROM research_subjects sub WHERE sub.user_id=%s "
                "AND NOT EXISTS (SELECT 1 FROM research_viewing_sessions s "
                "                WHERE s.subject_id=sub.subject_id) "
                "AND NOT EXISTS (SELECT 1 FROM research_conversation_items i "
                "                WHERE i.subject_id=sub.subject_id)",
                (user_id,))
            counts["subjects"] = cur.rowcount
            return counts


def _finalize_success(conn, job_id):
    with pg_store.transaction(conn) as tx:
        with tx.cursor() as cur:
            cur.execute(
                "UPDATE research_data_deletion_jobs "
                "SET status='completed', online_cleared=TRUE, "
                "    exports_cleared=TRUE, error_code=NULL, "
                "    completed_at=now(), updated_at=now() "
                "WHERE job_id=%s", (job_id,))


def _safe_error_code(exc) -> str:
    """安全错误码：只含异常类名（不落消息/堆栈——可能带连接串或身份线索）。"""
    return ("deletion_failed_%s" % type(exc).__name__)[:80]


def _finalize_failure(conn, job_id, attempts, exc, environ=None):
    base = _retry_base_seconds(environ)
    delay = base * (2 ** max(0, attempts - 1))
    with pg_store.transaction(conn) as tx:
        with tx.cursor() as cur:
            cur.execute(
                "UPDATE research_data_deletion_jobs "
                "SET status='failed', error_code=%s, "
                "    next_retry_at=now() + make_interval(secs=>%s), "
                "    updated_at=now() WHERE job_id=%s",
                (_safe_error_code(exc), delay, job_id))


def drain_once(limit=_DRAIN_BATCH, environ=None) -> dict:
    """处理至多 limit 条待执行删除任务；返回分类摘要（不含身份信息）。

    每任务三段独立事务（领取 / 删除 / 终态）：崩溃在任一段都安全——
    领取后崩溃由租约回收；删除后崩溃由幂等重删 + 重落终态兜底。
    """
    summary = {"completed": 0, "failed": 0, "deleted": {}}
    lease = _lease_seconds(environ)
    conn = _connect()
    try:
        for _ in range(max(1, int(limit))):
            job = _claim_next_job(conn, lease)
            if job is None:
                break
            try:
                counts = _delete_research_copies(
                    conn, job["user_id"], job["consent_epoch"])
                _finalize_success(conn, job["job_id"])
                summary["completed"] += 1
                for key, n in counts.items():
                    summary["deleted"][key] = \
                        summary["deleted"].get(key, 0) + n
                _log.info("研究副本删除任务完成（job=%s 删除行数=%s）",
                          job["job_id"], counts)
            except Exception as exc:  # noqa: BLE001 - 错误码只含异常类名
                try:
                    _finalize_failure(conn, job["job_id"], job["attempts"],
                                      exc, environ)
                except Exception:
                    _log.exception("删除任务失败态落库异常（job 状态可能仍"
                                   "为 running，等租约回收重试）")
                summary["failed"] += 1
                terminal = job["attempts"] >= \
                    research_consent_store.MAX_DELETION_ATTEMPTS
                _log.warning(
                    "研究副本删除任务失败（job=%s 第 %d/%d 次尝试，错误码"
                    "=%s%s）", job["job_id"], job["attempts"],
                    research_consent_store.MAX_DELETION_ATTEMPTS,
                    _safe_error_code(exc),
                    "，已达上限不再自动重试" if terminal else "，退避后重试")
    finally:
        conn.close()
    return summary


# --------------------------------------------------------------------------- #
# §8 90 天到期清理（研究动作/会话/对话副本）
# --------------------------------------------------------------------------- #
def purge_expired_research_once(limit=_PURGE_BATCH) -> dict:
    """删除 expires_at 已到期的研究副本（§8：每条最多 90 天，到期删除）。

    纯幂等 DELETE（分表分批），多实例重叠调度安全；会话最后删——级联残余
    事件。返回分类计数。会话过期即整会话删除（含尚未满 90 天的少量事件，
    「最多 90 天」是上限不是下限）。
    """
    counts = {"events": 0, "conversation_items": 0, "sessions": 0}
    conn = _connect()
    try:
        for table, key in (
                ("research_viewer_events", "events"),
                ("research_conversation_items", "conversation_items"),
                ("research_viewing_sessions", "sessions")):
            while True:
                with pg_store.transaction(conn) as tx:
                    with tx.cursor() as cur:
                        cur.execute(
                            "DELETE FROM %s WHERE ctid IN "
                            "(SELECT ctid FROM %s WHERE expires_at <= now() "
                            " LIMIT %s)" % (table, table, int(limit)))
                        n = cur.rowcount
                counts[key] += n
                if n < limit:
                    break
    finally:
        conn.close()
    if any(counts.values()):
        _log.info("研究副本到期清理：%s", counts)
    return counts


def run_once(environ=None) -> dict:
    """单轮完整执行：待执行删除任务 + 90 天到期清理。"""
    out = {"drain": drain_once(environ=environ),
           "purge": purge_expired_research_once()}
    return out


def main(argv=None):
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--loop", action="store_true",
                        help="持续循环（生产 worker）")
    parser.add_argument("--once", action="store_true",
                        help="单轮执行后退出（部署钩子/测试）")
    parser.add_argument("--interval", type=int, default=60,
                        help="循环轮询间隔秒（默认 60）")
    args = parser.parse_args(argv)
    if not (args.loop or args.once):
        parser.error("需要 --loop 或 --once 之一")
    if args.once:
        result = run_once()
        print("drain_completed=%d drain_failed=%d purge=%s"
              % (result["drain"]["completed"], result["drain"]["failed"],
                 result["purge"]))
        return 0
    interval = max(1, int(args.interval))
    while True:
        try:
            run_once()
        except Exception:
            _log.exception("worker 循环异常（继续）")
        time.sleep(interval)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
