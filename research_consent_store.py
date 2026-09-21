# -*- coding: utf-8 -*-
"""研究授权 consent 服务（P0/P2：docs/agent-plan-20260921-registration-consent-
research.md §3.3/§3.4/§3.5/§6.1/§6.2/§6.3）。

本模块是「是否同意研究数据共享」的**唯一服务端权威**：

- 当前状态存 ``user_research_consents``（缺行/异常 = 未授权），每次
  grant/withdraw 在单事务内锁行、epoch+1 并写不可变历史
  ``user_research_consent_history``（含前后 state、新 epoch、操作者、
  文档 hash、幂等键）；必选协议接受凭据追加进 ``user_agreement_acceptances``。
- **管理者不能代用户 grant**：grant/withdraw 的 actor 必须等于本人
  （ActorForbiddenError）。
- grant 必须引用**当前 published** 的 research_sharing 文稿且版本/hash 匹配；
  withdraw 不校验文稿（旧协议下架后也必须可撤回，§3.5）。
- ``expected_epoch`` 提供 CAS：与当前 epoch 不一致 → EpochConflictError
  （多标签页冲突由调用方映射 409）。
- ``idempotency_key`` 请求级幂等：同键重放直接返回当前状态，不重复写历史
  （唯一索引兜底）。
- **撤回原子（§6.3，P2）**：withdraw 的状态迁移、历史与
  ``research_data_deletion_jobs`` 删除任务在**同一事务**内落库——不存在
  「已撤回但没有删除任务」的中间态；事务回滚则三者一起回滚。
- ``create_deletion_job``：账户设置显式申请（reason=user_request）幂等创建
  本人研究副本删除任务（每用户至多一条**未了结**任务——completed 之外均
  算未了结，含待人工处置的终态 failed；部分唯一索引兜底并发）；该任务
  不删除业务切片或临床/科研工作记录（§3.5）。任务由
  ``research_deletion_worker``（独立进程，0064 退避簿记）异步执行：
  pending→running→completed/failed，失败有界重试、安全错误码；达上限的
  终态 failed 停止自动重试但**不解除研究阻断**（待人工处置，见
  :data:`UNRESOLVED_DELETION_JOBS_SQL`）；90 天到期清理同样在该
  worker（§8）。
- ``evaluate_research_access``（§6.1，P2）：研究采集/读取/导出的**统一权威
  判定**——当前真实用户本人、账号 active、非 owner 预览态、非 demo/公开
  分享访客、state=granted、文档版本有效、epoch 一致、数据在本次 grant 之后
  产生、无未了结删除任务（completed 之外一律阻断——含待人工处置的终态
  failed）、研究功能开关开启。任何前端上报、后端轨迹复制、
  研究浏览、导出与后续分析作业都必须调用本判定，不能只看某个 checkbox。
- **旧 test_applications.share_research_data 只是历史证明**：兼容层
  ``legacy_test_application_signal`` 只读旧字段并打 historical_only 标记；
  ``is_granted``/``ingest_allowed``/``evaluate_research_access`` 绝不读取
  旧字段——旧 true 不自动 granted，旧 false/缺行保持拒绝（§1 历史用户决定）。
- **研究采集开关默认关闭**：``RESEARCH_COLLECTION_ENABLED`` 未设置时
  ``collection_enabled()`` 为 False，``ingest_allowed`` 与
  ``evaluate_research_access`` 恒不允许（即使已 grant）；旧按钮/旧字段
  无法重新打开未授权采集。
"""

import os
import secrets

import psycopg

import agreement_store
import pg_store
import share_store_pg
import user_store

#: 研究授权对应的协议文档类型
RESEARCH_DOCUMENT_TYPE = "research_sharing"

#: 必选用户协议文档类型
TERMS_DOCUMENT_TYPE = "user_agreement"

#: consent 状态词表（与 0060 迁移 CHECK 一致）
CONSENT_STATES = ("declined", "granted", "withdrawn", "reconsent_required")

#: 接受凭据来源词表（与 0060 迁移 CHECK 一致）
ACCEPTANCE_SOURCES = ("register", "account_reaccept")

#: 研究副本删除任务原因词表（与 0062 迁移 CHECK 一致）
DELETION_JOB_REASONS = ("withdrawal", "user_request")

#: 删除任务状态词表（与 0062/0064 迁移 CHECK 一致；pending/running = 未终态）
DELETION_JOB_ACTIVE_STATUSES = ("pending", "running")
DELETION_JOB_STATUSES = ("pending", "running", "completed", "failed")

#: 删除任务执行尝试上限（0064；含首试）。达到上限后 failed 为**终态**，
#: worker 不再自动重试（需人工介入；worker 与本模块共用同一常量）。
#: 注意：终态 failed ≠ 解除研究阻断——删除义务要到 completed 才算了结
#: （见 :data:`UNRESOLVED_DELETION_JOBS_SQL`，两者是分开的两件事）。
MAX_DELETION_ATTEMPTS = 5

#: 「未了结（删除义务未了结）」删除任务的 SQL 谓词（§6.1/§6.3-5：删除
#: 义务未了结期间持续阻断研究采集/使用/再授权后的采集恢复）：
#: - pending / running：任务在队列或执行中；
#: - failed（无论退避重试中 attempts<上限，还是 attempts 达上限的终态）：
#:   数据还没删成，删除义务未了结，研究侧持续阻断。终态 failed 只表示
#:   worker 停止自动重试（research_deletion_worker 不再领取），**不**表示
#:   阻断可以解除——需人工按 error_code 处置；等待人工期间不影响用户
#:   正常读片/业务（阻断只作用于研究采集/读取/使用侧）；
#: - completed：在线副本已删，删除义务了结，唯一解除阻断的终态（任务行
#:   仍保留为备份恢复时的重放清单）。
UNRESOLVED_DELETION_JOBS_SQL = "(status IN ('pending','running','failed'))"

#: 研究采集功能开关 env（默认关闭；§1「研究采集独立开关默认关闭」）
COLLECTION_SWITCH_ENV = "RESEARCH_COLLECTION_ENABLED"


class ConsentError(RuntimeError):
    """consent 服务业务异常基类（路由层按 code 映射 4xx）。"""

    code = "consent_error"


class ActorForbiddenError(ConsentError):
    """操作者不是本人：管理者/其他用户不能代用户 grant 或 withdraw。"""

    code = "actor_forbidden"


class EpochConflictError(ConsentError):
    """expected_epoch 与当前 epoch 不一致（多标签页/重试冲突）。"""

    code = "epoch_conflict"


class DocumentVersionRequiredError(ConsentError):
    """grant 缺少协议版本或内容摘要（grant 时版本必填，§3.5）。"""

    code = "document_version_required"


class DeletionJobNotFoundError(ConsentError):
    """管理员处置入口按 job_id 定位删除任务失败（路由映射 404）。"""

    code = "deletion_job_not_found"


class DeletionJobNotTerminalFailedError(ConsentError):
    """任务不是「重试耗尽的终态 failed」，不能经管理员入口复活（409）。

    pending/running 本就会被 worker 领取；退避重试中的 failed
    （attempts < MAX_DELETION_ATTEMPTS）仍会自动重试；completed 是删除义务
    已了结的唯一终态——这些状态一律不接受管理员「重新执行」。
    """

    code = "deletion_job_not_terminal"


def _connect():
    conn = pg_store.connect()
    conn.row_factory = psycopg.rows.dict_row
    return conn


def collection_enabled(environ=None) -> bool:
    """研究采集功能开关：默认关闭（仅显式 1/true/yes 开启）。"""
    env = os.environ if environ is None else environ
    return (env.get(COLLECTION_SWITCH_ENV) or "").strip().lower() in (
        "1", "true", "yes")


def _consent_view(row) -> dict | None:
    if row is None:
        return None
    return {
        "user_id": row["user_id"],
        "state": row["state"],
        "scope_version": row["scope_version"],
        "document_version": row["document_version"],
        "document_sha256": row["document_sha256"],
        "epoch": row["epoch"],
        "granted_at": row["granted_at"],
        "withdrawn_at": row["withdrawn_at"],
        "updated_at": row["updated_at"],
    }


def get_consent(user_id) -> dict | None:
    """当前 consent 行；缺行返回 None（= 未授权）。"""
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT user_id, state, scope_version, document_version, "
                "document_sha256, epoch, granted_at, withdrawn_at, updated_at "
                "FROM user_research_consents WHERE user_id=%s", (user_id,))
            return _consent_view(cur.fetchone())
    finally:
        conn.close()


def is_granted(user_id) -> bool:
    """当前是否有有效研究授权（缺行/异常 = False；不读旧 test_applications）。"""
    try:
        consent = get_consent(user_id)
    except Exception:
        return False  # 缺行/异常 = 未授权（fail-closed）
    return bool(consent and consent["state"] == "granted")


def ingest_allowed(user_id, environ=None) -> bool:
    """研究采集/写入准入：功能开关开启 **且** 当前 state=granted。

    P0 开关默认关闭 → 恒 False；旧 test_applications 选项不参与判定。
    """
    if not collection_enabled(environ):
        return False
    return is_granted(user_id)


# --------------------------------------------------------------------------- #
# 必选协议接受凭据（追加，不覆盖旧版本）
# --------------------------------------------------------------------------- #
def record_terms_acceptance(user_id, document_type, version, content_sha256,
                            source, locale=agreement_store.DEFAULT_LOCALE,
                            conn=None) -> dict:
    """记录一次协议接受（``user_agreement_acceptances`` 追加行）。

    - document_type 必须在词表内；source ∈ register/account_reaccept；
    - 所引用文稿必须是**当前 published** 版本且 hash 匹配，否则
      agreement_store.DocumentNotPublishedError（条款缺失/版本不匹配 →
      后端拒绝，§3.1/P0 验收）；
    - user_id 由服务端会话产生（调用方责任），客户端提交的身份不作权威。
    """
    if document_type not in agreement_store.DOCUMENT_TYPES:
        raise ConsentError("未知协议文档类型：%r" % (document_type,))
    if source not in ACCEPTANCE_SOURCES:
        raise ConsentError("未知接受来源：%r" % (source,))
    doc = agreement_store.require_published_document(
        document_type, version, content_sha256, locale=locale)
    own = conn is None
    c = conn or _connect()
    try:
        with pg_store.transaction(c) as tx:
            with tx.cursor() as cur:
                cur.execute(
                    "INSERT INTO user_agreement_acceptances "
                    "(user_id, document_type, version, content_sha256, source, locale) "
                    "VALUES (%s,%s,%s,%s,%s,%s) RETURNING acceptance_id, accepted_at",
                    (user_id, doc["document_type"], doc["version"],
                     doc["content_sha256"], source, doc["locale"]))
                row = cur.fetchone()
                return {"acceptance_id": row["acceptance_id"],
                        "user_id": user_id,
                        "document_type": doc["document_type"],
                        "version": doc["version"],
                        "content_sha256": doc["content_sha256"],
                        "source": source,
                        "locale": doc["locale"],
                        "accepted_at": row["accepted_at"]}
    finally:
        if own:
            c.close()


def list_acceptances(user_id, document_type=None) -> list:
    """本人接受凭据列表（新→旧；账户设置展示用）。"""
    conn = _connect()
    try:
        with conn.cursor() as cur:
            if document_type is None:
                cur.execute(
                    "SELECT acceptance_id, user_id, document_type, version, "
                    "content_sha256, accepted_at, source, locale "
                    "FROM user_agreement_acceptances WHERE user_id=%s "
                    "ORDER BY acceptance_id DESC", (user_id,))
            else:
                cur.execute(
                    "SELECT acceptance_id, user_id, document_type, version, "
                    "content_sha256, accepted_at, source, locale "
                    "FROM user_agreement_acceptances "
                    "WHERE user_id=%s AND document_type=%s "
                    "ORDER BY acceptance_id DESC", (user_id, document_type))
            return [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# grant / withdraw（单事务：锁行 → CAS → 状态迁移 → 不可变历史）
# --------------------------------------------------------------------------- #
def _check_actor(user_id, actor_user_id) -> str:
    actor = actor_user_id or user_id
    if actor != user_id:
        raise ActorForbiddenError("不能代替其他用户变更研究授权")
    return actor


def _replay(cur, user_id, idempotency_key):
    """幂等重放：同 idempotency_key 已落历史 → 返回当前 consent（不再写）。"""
    if not idempotency_key:
        return None
    cur.execute(
        "SELECT 1 FROM user_research_consent_history "
        "WHERE user_id=%s AND idempotency_key=%s",
        (user_id, idempotency_key))
    if cur.fetchone() is None:
        return None
    cur.execute(
        "SELECT user_id, state, scope_version, document_version, "
        "document_sha256, epoch, granted_at, withdrawn_at, updated_at "
        "FROM user_research_consents WHERE user_id=%s", (user_id,))
    view = _consent_view(cur.fetchone())
    return {"consent": view, "changed": False, "replayed": True}


def _lock_consent(cur, user_id):
    cur.execute(
        "SELECT user_id, state, scope_version, document_version, "
        "document_sha256, epoch, granted_at, withdrawn_at, updated_at "
        "FROM user_research_consents WHERE user_id=%s FOR UPDATE", (user_id,))
    return cur.fetchone()


def grant(user_id, document_version=None, document_sha256=None,
          expected_epoch=None, actor_user_id=None, idempotency_key=None,
          locale=agreement_store.DEFAULT_LOCALE) -> dict:
    """同意研究数据共享：要求当前 published 文稿、版本/hash 匹配、本人操作。

    返回 ``{"consent": <view>, "changed": bool, "replayed": bool}``。
    """
    actor = _check_actor(user_id, actor_user_id)
    if not document_version or not document_sha256:
        raise DocumentVersionRequiredError("同意研究共享必须提供协议版本与内容摘要")
    # 版本/hash 校验在锁外完成（失败不改变任何状态）；文稿可能被并发 retire，
    # 但 published 校验以「当前」为准，锁内不再复核版本（epoch CAS 已覆盖
    # 用户侧并发；文稿 retire 后新 grant 会在下一次调用被拦）。
    doc = agreement_store.require_published_document(
        RESEARCH_DOCUMENT_TYPE, document_version, document_sha256, locale=locale)
    conn = _connect()
    try:
        with pg_store.transaction(conn) as tx:
            with tx.cursor() as cur:
                replayed = _replay(cur, user_id, idempotency_key)
                if replayed is not None:
                    return replayed
                row = _lock_consent(cur, user_id)
                if row is None:
                    if expected_epoch not in (None, 0):
                        raise EpochConflictError(
                            "当前无授权记录，epoch 应从 0 开始")
                    from_state, new_epoch = None, 1
                    cur.execute(
                        "INSERT INTO user_research_consents "
                        "(user_id, state, scope_version, document_version, "
                        " document_sha256, epoch, granted_at, updated_at) "
                        "VALUES (%s,'granted',%s,%s,%s,%s,now(),now())",
                        (user_id, doc["version"], doc["version"],
                         doc["content_sha256"], new_epoch))
                else:
                    if expected_epoch is not None and expected_epoch != row["epoch"]:
                        raise EpochConflictError(
                            "授权状态已变化（当前 epoch=%s），请刷新后重试"
                            % row["epoch"])
                    from_state, new_epoch = row["state"], row["epoch"] + 1
                    cur.execute(
                        "UPDATE user_research_consents "
                        "SET state='granted', scope_version=%s, "
                        "    document_version=%s, document_sha256=%s, epoch=%s, "
                        "    granted_at=now(), updated_at=now() "
                        "WHERE user_id=%s",
                        (doc["version"], doc["version"], doc["content_sha256"],
                         new_epoch, user_id))
                cur.execute(
                    "INSERT INTO user_research_consent_history "
                    "(user_id, from_state, to_state, epoch, actor_user_id, "
                    " document_version, document_sha256, idempotency_key) "
                    "VALUES (%s,%s,'granted',%s,%s,%s,%s,%s)",
                    (user_id, from_state, new_epoch, actor, doc["version"],
                     doc["content_sha256"], idempotency_key or None))
                row = _lock_consent(cur, user_id)
                return {"consent": _consent_view(row), "changed": True,
                        "replayed": False}
    finally:
        conn.close()


def withdraw(user_id, expected_epoch=None, actor_user_id=None,
             idempotency_key=None) -> dict:
    """撤回研究共享：无需审批、无需理由；旧协议下架后也必须可用（不校验文稿）。

    无 consent 行或已 withdrawn → 幂等成功（changed=False）。状态迁移时
    **同一事务**内 epoch+1、写历史并创建 reason='withdrawal' 的研究副本
    删除任务（§6.3 撤回原子：不存在「已撤回但没有删除任务」的中间态；
    任何一步失败整体回滚）。

    返回 ``{"consent": <view>, "changed": bool, "replayed": bool,
    "deletion_job": <view>|None}``。
    """
    actor = _check_actor(user_id, actor_user_id)
    conn = _connect()
    try:
        with pg_store.transaction(conn) as tx:
            with tx.cursor() as cur:
                replayed = _replay(cur, user_id, idempotency_key)
                if replayed is not None:
                    replayed["deletion_job"] = _active_job_tx(cur, user_id)
                    return replayed
                row = _lock_consent(cur, user_id)
                if row is None:
                    return {"consent": None, "changed": False,
                            "replayed": False, "deletion_job": None}
                if expected_epoch is not None and expected_epoch != row["epoch"]:
                    raise EpochConflictError(
                        "授权状态已变化（当前 epoch=%s），请刷新后重试"
                        % row["epoch"])
                if row["state"] == "withdrawn":
                    return {"consent": _consent_view(row), "changed": False,
                            "replayed": False,
                            "deletion_job": _active_job_tx(cur, user_id)}
                from_state, new_epoch = row["state"], row["epoch"] + 1
                cur.execute(
                    "UPDATE user_research_consents "
                    "SET state='withdrawn', epoch=%s, withdrawn_at=now(), "
                    "    updated_at=now() "
                    "WHERE user_id=%s", (new_epoch, user_id))
                cur.execute(
                    "INSERT INTO user_research_consent_history "
                    "(user_id, from_state, to_state, epoch, actor_user_id, "
                    " document_version, document_sha256, idempotency_key) "
                    "VALUES (%s,%s,'withdrawn',%s,%s,%s,%s,%s)",
                    (user_id, from_state, new_epoch, actor,
                     row["document_version"], row["document_sha256"],
                     idempotency_key or None))
                # 撤回原子（§6.3）：同一事务内创建研究副本删除任务
                job = _upsert_deletion_job_tx(
                    cur, user_id, new_epoch, reason="withdrawal")
                row = _lock_consent(cur, user_id)
                return {"consent": _consent_view(row), "changed": True,
                        "replayed": False, "deletion_job": job}
    finally:
        conn.close()


def list_history(user_id) -> list:
    """本人授权历史（新→旧；不可变记录，账户设置/争议处理展示用）。"""
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT history_id, user_id, from_state, to_state, epoch, "
                "actor_user_id, document_version, document_sha256, "
                "idempotency_key, created_at "
                "FROM user_research_consent_history WHERE user_id=%s "
                "ORDER BY history_id DESC", (user_id,))
            return [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# 研究副本删除任务（§3.5/§6.2/§6.3，P2 落任务；P1 修复补执行器
# research_deletion_worker——本节只保留任务生命周期与阻断权威）
# --------------------------------------------------------------------------- #
_JOB_COLUMNS = ("job_id, user_id, consent_epoch, reason, status, "
                "online_cleared, exports_cleared, backups_pending, "
                "error_code, attempts, next_retry_at, "
                "created_at, updated_at, completed_at")


def _job_view(row) -> dict | None:
    if row is None:
        return None
    return dict(row)


def _active_job_tx(cur, user_id) -> dict | None:
    """在既有 cursor 上读当前**未了结**删除任务（供事务内路径复用连接）。

    未了结 = pending/running/failed（含达上限、待人工处置的终态 failed；
    删除义务未了结即阻断，见 :data:`UNRESOLVED_DELETION_JOBS_SQL`）。
    """
    cur.execute(
        "SELECT %s FROM research_data_deletion_jobs "
        "WHERE user_id=%%s AND %s "
        "ORDER BY created_at DESC LIMIT 1"
        % (_JOB_COLUMNS, UNRESOLVED_DELETION_JOBS_SQL), (user_id,))
    return _job_view(cur.fetchone())


def _upsert_deletion_job_tx(cur, user_id, consent_epoch, reason):
    """在**既有事务内**创建/推进研究副本删除任务（撤回原子链的一环）。

    每用户至多一条未了结任务（completed 之外，见
    :data:`UNRESOLVED_DELETION_JOBS_SQL`；0062→0065 部分唯一索引把 failed
    也纳入唯一约束，兜底并发）：已有未了结任务时把其 consent_epoch 推进到
    本次撤回的新 epoch（清理目标以最新撤回为准），不重复建任务；处于
    failed（退避重试中或达上限的终态）的任务同时被**复活**为 pending
    立即可领取（新撤回 = 新删除义务，不该等旧退避时钟；终态 failed 复活后
    attempts 仍为已达上限，worker 再失败一次即回终态——每次新撤回至多换一
    轮尝试，不会无限循环）；无未了结任务则插入 reason='withdrawal' 新任务。

    并发兜底：本函数的 SELECT 与 INSERT 之间存在竞态窗口——另一请求抢先
    提交未了结任务（0065 起含 failed，例如对方任务在窗口内建成并执行失败
    转 failed）。INSERT ... ON CONFLICT DO NOTHING 落空时**不得**返回 None
    （撤回原子：已撤回必须带出删除任务视图），而是重查未了结任务并按上述
    推进/复活语义收口到赢家任务（READ COMMITTED 下 no-op 返回时对方事务
    必然已提交、对下一条语句可见）。
    """
    for _round in range(2):
        cur.execute(
            "SELECT %s FROM research_data_deletion_jobs "
            "WHERE user_id=%%s AND %s "
            "ORDER BY created_at DESC, job_id DESC "
            "FOR UPDATE" % (_JOB_COLUMNS, UNRESOLVED_DELETION_JOBS_SQL),
            (user_id,))
        row = cur.fetchone()
        if row is not None:
            cur.execute(
                "UPDATE research_data_deletion_jobs "
                "SET consent_epoch=%s, "
                "    status=CASE WHEN status='failed' THEN 'pending' "
                "                ELSE status END, "
                "    next_retry_at=CASE WHEN status='failed' THEN now() "
                "                       ELSE next_retry_at END, "
                "    updated_at=now() WHERE job_id=%s",
                (consent_epoch, row["job_id"]))
            cur.execute(
                "SELECT %s FROM research_data_deletion_jobs WHERE job_id=%%s"
                % _JOB_COLUMNS, (row["job_id"],))
            return _job_view(cur.fetchone())
        job_id = "rdj_" + secrets.token_urlsafe(16)
        cur.execute(
            "INSERT INTO research_data_deletion_jobs "
            "(job_id, user_id, consent_epoch, reason) VALUES (%s,%s,%s,%s) "
            "ON CONFLICT DO NOTHING", (job_id, user_id, consent_epoch, reason))
        cur.execute(
            "SELECT %s FROM research_data_deletion_jobs WHERE job_id=%%s"
            % _JOB_COLUMNS, (job_id,))
        row = cur.fetchone()
        if row is not None:
            return _job_view(row)
        # 插入被唯一索引兜底 no-op（并发赢家已提交，含 failed）：回到循环
        # 头重查未了结任务并按撤回语义推进（一轮重试足够）。
    return _active_job_tx(cur, user_id)


def create_deletion_job(user_id, reason="user_request", actor_user_id=None) -> dict:
    """显式申请删除**本人**研究副本（§3.5）：幂等创建可查询状态的删除任务。

    - reason ∈ withdrawal/user_request（路由层只允许 user_request；
      withdrawal 由 ``withdraw`` 事务内自动创建，不在此重复入口）；
    - 幂等：已有未了结任务（含退避重试中与达上限、待人工处置的终态
      failed）→ 原样返回 ``created=False``（终态 failed 不自动重试，
      任务状态即「删除任务待人工处理」的真实展示；处置到 completed 前
      研究侧持续阻断）；
    - 记录创建时的 consent epoch（无 consent 行 = 0：从未授权，无研究副本，
      任务仅作请求凭据）；备份恢复后按本清单执行，不让已撤回数据复活；
    - **不删除业务切片、标注或临床/科研工作记录**（只清理研究副本层；
      执行由 ``research_deletion_worker`` 异步完成，任务状态可经
      GET /api/account/research-data/deletion 查询）。
    """
    _check_actor(user_id, actor_user_id)
    if reason not in DELETION_JOB_REASONS:
        raise ConsentError("未知删除任务原因：%r" % (reason,))
    conn = _connect()
    try:
        with pg_store.transaction(conn) as tx:
            with tx.cursor() as cur:
                cur.execute(
                    "SELECT %s FROM research_data_deletion_jobs "
                    "WHERE user_id=%%s AND %s "
                    "FOR UPDATE" % (_JOB_COLUMNS, UNRESOLVED_DELETION_JOBS_SQL),
                    (user_id,))
                row = cur.fetchone()
                if row is not None:
                    return {"job": _job_view(row), "created": False}
                cur.execute(
                    "SELECT epoch FROM user_research_consents "
                    "WHERE user_id=%s", (user_id,))
                crow = cur.fetchone()
                epoch = crow["epoch"] if crow is not None else 0
                job_id = "rdj_" + secrets.token_urlsafe(16)
                # 并发竞争兜底：另一事务抢先提交了未了结任务（0065 部分唯一
                # 索引把 failed 也算冲突——含本 SELECT 与 INSERT 窗口内建成
                # 即执行失败转 failed 的任务）→ 本插入 no-op，返回对方任务
                # （created=False，幂等语义）
                cur.execute(
                    "INSERT INTO research_data_deletion_jobs "
                    "(job_id, user_id, consent_epoch, reason) "
                    "VALUES (%s,%s,%s,%s) ON CONFLICT DO NOTHING",
                    (job_id, user_id, epoch, reason))
                cur.execute(
                    "SELECT %s FROM research_data_deletion_jobs "
                    "WHERE job_id=%%s" % _JOB_COLUMNS, (job_id,))
                created_row = cur.fetchone()
                if created_row is None:
                    return {"job": _active_job_tx(cur, user_id),
                            "created": False}
                return {"job": _job_view(created_row), "created": True}
    finally:
        conn.close()


def get_active_deletion_job(user_id) -> dict | None:
    """当前未了结删除任务（pending/running/failed——含待人工处置的终态
    failed）；全部 completed（或无任务）时 None。"""
    conn = _connect()
    try:
        with conn.cursor() as cur:
            return _active_job_tx(cur, user_id)
    finally:
        conn.close()


def list_deletion_jobs(user_id) -> list:
    """本人删除任务列表（新→旧；账户设置展示用）。"""
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT %s FROM research_data_deletion_jobs "
                "WHERE user_id=%%s ORDER BY created_at DESC, job_id DESC"
                % _JOB_COLUMNS, (user_id,))
            return [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# 管理员最小处置入口（owner-only；终态 failed 的人工处置，§6.3-5）
# --------------------------------------------------------------------------- #
def list_deletion_jobs_admin(status=None, limit=200) -> list:
    """管理员视图：**全部用户**的研究删除任务（owner admin API 只读出口）。

    - status ∈ :data:`DELETION_JOB_STATUSES` 时按状态过滤（None = 全部）；
      非法状态直接 ConsentError（路由映射 400）；
    - 行含处置诊断字段（status/error_code/attempts/next_retry_at/
      consent_epoch/创建与更新时间）；按 updated_at 新→旧（最近失败的排在
      前面），上限 limit（1..500）；
    - 本函数不脱敏 user_id——出线白名单与身份映射由路由层（admin v1）负责。
    """
    if status is not None and status not in DELETION_JOB_STATUSES:
        raise ConsentError("未知删除任务状态：%r" % (status,))
    cap = max(1, min(int(limit), 500))
    conn = _connect()
    try:
        with conn.cursor() as cur:
            if status is None:
                cur.execute(
                    "SELECT %s FROM research_data_deletion_jobs "
                    "ORDER BY updated_at DESC, job_id DESC LIMIT %s"
                    % (_JOB_COLUMNS, cap))
                return [dict(r) for r in cur.fetchall()]
            cur.execute(
                "SELECT %s FROM research_data_deletion_jobs "
                "WHERE status=%%s ORDER BY updated_at DESC, job_id DESC "
                "LIMIT %d" % (_JOB_COLUMNS, cap), (status,))
            return [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()


def admin_retry_deletion_job(job_id, actor_user_id=None, actor_role=None) -> dict:
    """管理员处置：把**终态 failed**（重试耗尽）的删除任务复活为 pending。

    语义（与路由层 /api/admin/v1/research-deletion-jobs/<job_id>/retry 同源）：

    - 仅接受 ``status='failed'`` 且 ``attempts >= MAX_DELETION_ATTEMPTS`` 的
      **终态** failed（worker 已停止自动重试、待人工按 error_code 处置）；
      pending/running/退避重试中的 failed/completed 一律
      :class:`DeletionJobNotTerminalFailedError`（路由映射 409）——
      非 终态任务不需要也不应经人工复活（避免绕过退避节奏、避免碰已了结
      义务）；
    - 复活 = ``status='pending'``、``attempts`` 重置 0（给一轮全新的
      MAX_DELETION_ATTEMPTS 尝试预算）、``next_retry_at=now()``（立即可
      领取）；``consent_epoch`` **不动**（删除义务的覆盖范围不变，只是
      重新执行）；``error_code`` 保留为上一轮最后错误（诊断线索；worker
      成功时清空、再失败时覆盖）；
    - **红线：本函数（以及任何管理入口）绝不提供把任务直接置 'completed'
      的路径**——completed 只能由 ``research_deletion_worker`` 在实际清理
      成功后落库（``_finalize_success``）。删除义务是否了结以真实清理为准，
      不接受任何「跳过清理直接了结」的人工终态；复活后研究侧仍以
      deletion_pending 阻断，直到 worker 真实清理完成；
    - 审计：**同一事务**内写 audit_events（操作者/动作/任务 id/时间由审计
      行自带；detail 记 previous_attempts / attempts_reset_to / error_code）——
      审计写失败则复活一并回滚（处置必留痕）。

    返回 ``{"job": <view>, "previous_attempts": int}``。
    """
    if not isinstance(job_id, str) or not job_id:
        raise DeletionJobNotFoundError("删除任务不存在")
    conn = _connect()
    try:
        with pg_store.transaction(conn) as tx:
            with tx.cursor() as cur:
                cur.execute(
                    "SELECT %s FROM research_data_deletion_jobs "
                    "WHERE job_id=%%s FOR UPDATE" % _JOB_COLUMNS, (job_id,))
                row = cur.fetchone()
                if row is None:
                    raise DeletionJobNotFoundError("删除任务不存在")
                previous_attempts = int(row["attempts"] or 0)
                if row["status"] != "failed" or \
                        previous_attempts < MAX_DELETION_ATTEMPTS:
                    raise DeletionJobNotTerminalFailedError(
                        "仅重试耗尽的终态 failed 任务可重新执行"
                        "（当前 status=%s attempts=%d）"
                        % (row["status"], previous_attempts))
                cur.execute(
                    "UPDATE research_data_deletion_jobs "
                    "SET status='pending', attempts=0, next_retry_at=now(), "
                    "    updated_at=now() WHERE job_id=%s", (job_id,))
                share_store_pg.record_audit_tx(
                    cur, "research.deletion_job.retry",
                    actor_user_id=actor_user_id, actor_role=actor_role,
                    target_type="research_deletion_job", target_id=job_id,
                    detail={"previous_attempts": previous_attempts,
                            "attempts_reset_to": 0,
                            "error_code": row["error_code"],
                            "consent_epoch": row["consent_epoch"]})
                cur.execute(
                    "SELECT %s FROM research_data_deletion_jobs "
                    "WHERE job_id=%%s" % _JOB_COLUMNS, (job_id,))
                return {"job": _job_view(cur.fetchone()),
                        "previous_attempts": previous_attempts}
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# §6.1 研究采集/使用的统一权威判定（P2）
# --------------------------------------------------------------------------- #
def evaluate_research_access(user_id, *, preview=False, demo_guest=False,
                             data_consent_epoch=None, data_created_at=None,
                             resource_owner_id=None,
                             environ=None) -> dict:
    """研究数据采集/读取/导出的**唯一权威判定**（§6.1）。

    任何前端上报、后端轨迹复制、研究浏览、导出和后续分析作业都必须调用
    本函数（或其上的路由封装），不能只检查浏览器或某个后台列表的 checkbox。
    全部条件**同时**满足才 allowed：

    1. 当前真实用户本人（user_id 非空）且账号存在、未禁用、activation_state
       == active（缺行/查询异常 = 拒绝，fail-closed）；
    2. 非 owner 预览态、非 demo/公开分享访客（调用方传入请求上下文标志；
       预览态的 effective subject 不是用户本人操作）；
    3. 当前 state=granted，且引用的文档版本与**当前 published**
       research_sharing 一致（无 published 或版本过期 = 拒绝——不能以旧
       协议继续采集/使用）；
    4. epoch 一致：携带数据标记 ``data_consent_epoch`` 时必须等于当前
       epoch（旧 grant/撤回前/离线重传的数据一律拒绝）；
    5. 数据在本次 grant 之后产生：携带 ``data_created_at`` 时必须不早于
       当前 granted_at（grant 前的历史数据回填 = 未经授权，拒绝）；
    6. 资源权利：携带 ``resource_owner_id`` 时必须等于本人（第一版只允许
       本人拥有且明确标记可用于本项研究的资源；「可查看他人切片」不足
       以授权研究）；
    7. 无未了结研究副本删除任务（pending/running/failed——撤回/删除链未
       清完不得继续研究使用；终态 failed 只表示 worker 停止自动重试，
       处置到 completed 前持续阻断、等待人工按 error_code 处置，执行详见
       ``research_deletion_worker``）；
    8. 研究采集/使用功能开关开启（P2 默认关闭——即使已 grant 也不允许）。

    返回 ``{"allowed": bool, "reasons": [机器码...], "consent": <view>|None}``；
    reasons 供审计/测试，不出现在给最终用户的报错文案里。
    """
    reasons = []
    if not user_id:
        return {"allowed": False, "reasons": ["no_identity"], "consent": None}
    if preview:
        reasons.append("owner_preview")
    if demo_guest:
        reasons.append("demo_guest")
    # 账号状态（缺行/异常 = 拒绝）
    try:
        user = user_store.get_user(user_id)
    except Exception:
        return {"allowed": False, "reasons": ["account_lookup_failed"],
                "consent": None}
    if user is None or user.get("disabled"):
        reasons.append("account_not_found")
    elif (user.get("activation_state") or "active") != "active":
        reasons.append("account_not_active")
    # consent 当前状态 + 文档版本有效
    consent = get_consent(user_id)
    if consent is None or consent["state"] != "granted":
        reasons.append("not_granted")
    else:
        published = agreement_store.current_published(RESEARCH_DOCUMENT_TYPE)
        if published is None:
            reasons.append("document_not_published")
        elif consent["document_version"] != published["version"]:
            reasons.append("document_version_stale")
        if data_consent_epoch is not None and data_consent_epoch != consent["epoch"]:
            reasons.append("epoch_mismatch")
        if data_created_at is not None:
            granted_at = consent.get("granted_at")
            if granted_at is None or data_created_at < granted_at:
                reasons.append("predates_grant")
    if resource_owner_id is not None and resource_owner_id != user_id:
        reasons.append("resource_not_owned")
    if get_active_deletion_job(user_id) is not None:
        reasons.append("deletion_pending")
    if not collection_enabled(environ):
        reasons.append("collection_disabled")
    return {"allowed": not reasons, "reasons": reasons, "consent": consent}


# --------------------------------------------------------------------------- #
# 旧 test_applications 选项兼容层（§1/§3.5：旧 true 仅保留历史证明）
# --------------------------------------------------------------------------- #
def legacy_test_application_signal(user_id) -> dict | None:
    """读取旧测试申请的分享选项，**只作为历史证明**返回。

    返回值带 ``historical_only=True`` 与 ``historical_consent_version``；
    本函数的结果绝不进入 is_granted/ingest_allowed——旧 true 不自动
    granted，旧 false/缺行保持拒绝。
    """
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT share_research_data, consent_version, consent_updated_at "
                "FROM test_applications WHERE user_id=%s", (user_id,))
            row = cur.fetchone()
            if row is None:
                return None
            return {
                "historical_only": True,
                "share_research_data": bool(row["share_research_data"]),
                "historical_consent_version": row["consent_version"],
                "consent_updated_at": row["consent_updated_at"],
            }
    finally:
        conn.close()


def research_authorization_view(user_id, environ=None) -> dict:
    """授权视图（账户设置/后台只读展示）：当前状态 + 旧选项历史标记。

    ``granted``/``ingest_allowed`` 只由新 consent 行与采集开关决定；
    旧 test_applications 字段单独标记「历史版本，未授权当前研究采集」。
    P2 起附 ``active_deletion_job``（撤回/显式申请产生的未了结删除任务，
    账户设置/后台展示清理状态用——含待人工处置的终态 failed，向用户/管理
    呈现「删除任务待人工处理」的真实状态）。
    """
    consent = get_consent(user_id)
    granted = bool(consent and consent["state"] == "granted")
    enabled = collection_enabled(environ)
    return {
        "user_id": user_id,
        "state": consent["state"] if consent else None,
        "granted": granted,
        "epoch": consent["epoch"] if consent else 0,
        "document_version": consent["document_version"] if consent else None,
        "document_sha256": consent["document_sha256"] if consent else None,
        "granted_at": consent["granted_at"] if consent else None,
        "withdrawn_at": consent["withdrawn_at"] if consent else None,
        "collection_enabled": enabled,
        "ingest_allowed": bool(enabled and granted),
        "legacy_test_application": legacy_test_application_signal(user_id),
        "active_deletion_job": get_active_deletion_job(user_id),
    }


def new_idempotency_key() -> str:
    """生成请求幂等键（调用方/路由层也可自行生成后传入）。"""
    return "rci_" + secrets.token_urlsafe(16)
