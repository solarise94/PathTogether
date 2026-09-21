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
  本人研究副本删除任务（每用户至多一条未终态任务，部分唯一索引兜底）；
  该任务不删除业务切片或临床/科研工作记录（§3.5）。
- ``evaluate_research_access``（§6.1，P2）：研究采集/读取/导出的**统一权威
  判定**——当前真实用户本人、账号 active、非 owner 预览态、非 demo/公开
  分享访客、state=granted、文档版本有效、epoch 一致、数据在本次 grant 之后
  产生、无未终态删除任务、研究功能开关开启。任何前端上报、后端轨迹复制、
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

#: 删除任务状态词表（与 0062 迁移 CHECK 一致；pending/running = 未终态）
DELETION_JOB_ACTIVE_STATUSES = ("pending", "running")
DELETION_JOB_STATUSES = ("pending", "running", "completed", "failed")

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
# 研究副本删除任务（§3.5/§6.2/§6.3，P2）
# --------------------------------------------------------------------------- #
_JOB_COLUMNS = ("job_id, user_id, consent_epoch, reason, status, "
                "online_cleared, exports_cleared, backups_pending, "
                "error_code, created_at, updated_at, completed_at")


def _job_view(row) -> dict | None:
    if row is None:
        return None
    return dict(row)


def _active_job_tx(cur, user_id) -> dict | None:
    """在既有 cursor 上读当前未终态删除任务（供事务内路径复用连接）。"""
    cur.execute(
        "SELECT %s FROM research_data_deletion_jobs "
        "WHERE user_id=%%s AND status IN ('pending','running') "
        "ORDER BY created_at DESC LIMIT 1" % _JOB_COLUMNS, (user_id,))
    return _job_view(cur.fetchone())


def _upsert_deletion_job_tx(cur, user_id, consent_epoch, reason):
    """在**既有事务内**创建/推进研究副本删除任务（撤回原子链的一环）。

    每用户至多一条未终态任务（0062 部分唯一索引）：已有未终态任务时把其
    consent_epoch 推进到本次撤回的新 epoch（清理目标以最新撤回为准），
    不重复建任务；无则插入 reason='withdrawal' 新任务。
    """
    cur.execute(
        "SELECT %s FROM research_data_deletion_jobs "
        "WHERE user_id=%%s AND status IN ('pending','running') "
        "FOR UPDATE" % _JOB_COLUMNS, (user_id,))
    row = cur.fetchone()
    if row is not None:
        cur.execute(
            "UPDATE research_data_deletion_jobs "
            "SET consent_epoch=%s, updated_at=now() WHERE job_id=%s",
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
    return _job_view(cur.fetchone())


def create_deletion_job(user_id, reason="user_request", actor_user_id=None) -> dict:
    """显式申请删除**本人**研究副本（§3.5）：幂等创建可查询状态的删除任务。

    - reason ∈ withdrawal/user_request（路由层只允许 user_request；
      withdrawal 由 ``withdraw`` 事务内自动创建，不在此重复入口）；
    - 幂等：已有未终态（pending/running）任务 → 原样返回 ``created=False``；
    - 记录创建时的 consent epoch（无 consent 行 = 0：从未授权，无研究副本，
      任务仅作请求凭据）；备份恢复后按本清单执行，不让已撤回数据复活；
    - **不删除业务切片、标注或临床/科研工作记录**（清理执行属后续阶段；
      P2 只落任务与状态）。
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
                    "WHERE user_id=%%s AND status IN ('pending','running') "
                    "FOR UPDATE" % _JOB_COLUMNS, (user_id,))
                row = cur.fetchone()
                if row is not None:
                    return {"job": _job_view(row), "created": False}
                cur.execute(
                    "SELECT epoch FROM user_research_consents "
                    "WHERE user_id=%s", (user_id,))
                crow = cur.fetchone()
                epoch = crow["epoch"] if crow is not None else 0
                job_id = "rdj_" + secrets.token_urlsafe(16)
                # 并发竞争兜底：另一事务抢先建了 active 任务（部分唯一索引）
                # → 本插入 no-op，返回对方任务（created=False，幂等语义）
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
    """当前未终态（pending/running）删除任务；无则 None。"""
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
    7. 无未终态（pending/running）研究副本删除任务（撤回/删除链未清完
       不得继续研究使用）；
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
    P2 起附 ``active_deletion_job``（撤回/显式申请产生的未终态删除任务，
    账户设置展示清理状态用）。
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
