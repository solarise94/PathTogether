# -*- coding: utf-8 -*-
"""自助找回密码存储层（忘记密码 → 邮件重置链接 → 设置新密码）。

职责（PG-only，fail-closed）：

**重置邮件入队**（公开、匿名；账号是否存在不泄露）：
- :func:`request_password_reset`：规范化邮箱 → 同事务读取账号与配额 →
  **仅对符合条件账号**签发一次性 token 并复用 registration_mail_jobs
  （purpose='password_reset'，0095 迁移扩词表）入队重置邮件。payload（加密
  冻结正文）绑定 ``user_id`` 与请求时的 ``auth_version``；库内只存 token
  的域分离 HMAC hash，明文 token 只在邮件链接出现；
- 不符合条件（未知/owner/禁用/未激活/无已验证邮箱）与超额限流都返回同一
  形状的结果（``kind`` 只供路由层记日志/排水，**绝不进入响应**）。

**重置链接校验与消费**：
- :func:`check_password_reset_token`：只读解析（GET /reset-password 页面用，
  绝不消费）；除 token 本身外还校验 payload 绑定与账号当前状态（auth_version
  已变化 / 已被禁用 / 已非普通用户 → 一律按 invalid 处理）；
- :func:`consume_password_reset`：单事务消费：token（queued/sent/uncertain
  可消费、一次性、30 分钟）→ 锁定用户行并复核 role=user/active/未禁用/
  auth_version 未变化 → 更新 password_hash 并 ``auth_version+1``（全端
  session 失效）→ job 置 consumed → **同事务**清该账号登录失败桶 → 同事务
  审计 ``user.password_reset_email``（detail 只含掩码邮箱，无 token/密码）。

红线：
  - **owner 账号永不进入本流程**（owner 只能走本人改密或主机侧 break-glass
    CLU）；role 复核在入队与消费两侧都做；
  - token/明文邮箱/密码绝不进日志；日志与审计里邮箱一律
    registration_store.mask_login_id 掩码；
  - 不修改 registration_store / identity_store 的既有语义；只复用其公开函数
    （validate_email / normalize_email / verify_token_hash / mask_login_id）。
    registration_mail_worker 仅新增 ``password_reset_*`` 的完整正文 SMTP
    路由（冻结 template_key 决定通道），既有用途路由不变。
"""
import secrets
import time

import psycopg
from werkzeug.security import generate_password_hash

import auth_limit_store
import pg_store
import registration_mail_worker as mail_worker
import registration_store
import user_store

#: 邮件用途（0095 迁移 purpose CHECK 同词表）
MAIL_PURPOSE_PASSWORD_RESET = "password_reset"

#: token 有效期（与注册验证/改绑同款 30 分钟）
PASSWORD_RESET_TTL_SECONDS = registration_store.VERIFY_TOKEN_TTL_SECONDS

#: 同邮箱冷却/限额（防借「忘记密码」轰炸任意收件箱）。口径与邮箱改绑
#: （identity_store）对齐：60s 冷却 / 时 3 / 日 5；权威数据源是
#: registration_mail_jobs 的作业行数。
PASSWORD_RESET_COOLDOWN_SECONDS = 60
PASSWORD_RESET_HOURLY_LIMIT = 3
PASSWORD_RESET_DAILY_LIMIT = 5

#: 邮件模板键（PurposeRoutingMailSender 按冻结用途路由；当前走 SMTP 完整
#: 正文，避免为一次文案变更新建 SES 审核模板——见 registration_mail_worker）
TEMPLATE_KEY_ZH = "password_reset_zh"
TEMPLATE_KEY_EN = "password_reset_en"


class PasswordResetError(RuntimeError):
    """找回密码失败。``code`` 稳定：bad_input / invalid_or_expired /
    rate_limited / user_missing / user_disabled / user_not_eligible。
    （对外文案统一由路由层决定；公开入口不区分账号是否存在的任何状态。）"""

    def __init__(self, code, message=None):
        self.code = str(code)
        super().__init__(message or self.code)


def _connect():
    conn = pg_store.connect()
    conn.row_factory = psycopg.rows.dict_row
    return conn


# --------------------------------------------------------------------------- #
# 邮件正文（token 只进返回值；调用方加密后才落库）
# --------------------------------------------------------------------------- #
def build_password_reset_body(email, token, base_url, locale="zh"):
    """构造密码重置邮件冻结正文。返回 (subject, body)。

    链接 = ``<base_url>/reset-password?token=<明文 token>``；GET /reset-password
    只展示，消费在 POST /reset-password。语言取入队时冻结的 ``locale``
    （zh|en；与注册/改绑邮件同一入口语言选择口径），不在发送时重建。
    """
    base = str(base_url or "").strip().rstrip("/")
    if not base:
        raise mail_worker.MailSenderUnavailable(
            "PUBLIC_BASE_URL 未配置，无法构造密码重置链接")
    link = base + "/reset-password?token=" + str(token)
    if str(locale or "zh").strip().lower() == "en":
        subject = "PathTogether password reset (valid for 30 minutes)"
        body = (
            "Hello,\n\n"
            "Someone (usually you) requested a password reset for your "
            "PathTogether account (%s).\n"
            "Open the link below within 30 minutes to set a new password:\n\n"
            "%s\n\n"
            "The link can only be used once. After resetting, all signed-in "
            "devices will need to sign in again with the new password.\n\n"
            "If you did not request this, please ignore this email. Your "
            "password will not change.\n"
            % (str(email), link))
        return subject, body
    subject = "PathTogether 密码重置（30 分钟内有效）"
    body = (
        "你好，\n\n"
        "有人（通常是你本人）刚请求重置 PathTogether 账号（%s）的登录密码。\n"
        "请在 30 分钟内打开下面的链接设置新密码：\n\n"
        "%s\n\n"
        "该链接只能使用一次。重置后，所有已登录设备都需要用新密码重新登录。\n\n"
        "如果不是你本人操作，请忽略本邮件，你的密码不会改变。\n"
        % (str(email), link))
    return subject, body


def _quota_counts_tx(cur, email_norm):
    """同事务读取同邮箱重置邮件配额占用：(cooldown, hourly, daily)。

    权威数据源 = registration_mail_jobs 行数（与注册/改绑配额同思路）；
    只统计 purpose='password_reset'，不与其他用途互相占用额度。
    """
    cur.execute(
        "SELECT "
        " count(*) FILTER (WHERE created_at > now() - interval '"
        + str(PASSWORD_RESET_COOLDOWN_SECONDS) + " seconds') AS cooldown, "
        " count(*) FILTER (WHERE created_at > now() - interval '1 hour') "
        "   AS hourly, "
        " count(*) FILTER (WHERE created_at > now() - interval '24 hours') "
        "   AS daily "
        "FROM registration_mail_jobs "
        "WHERE email_normalized=%s AND purpose=%s",
        (email_norm, MAIL_PURPOSE_PASSWORD_RESET))
    row = cur.fetchone()
    return (int(row["cooldown"]), int(row["hourly"]), int(row["daily"]))


def _eligible_user_row(row):
    """账号是否可走「邮件找回密码」：普通用户 + 启用 + 已激活 + 邮箱已验证。

    owner 由调用方在 SQL 中以 role='user' 排除（本函数再兜底一次 role），
    preview 只是 session 态、demo 走独立 capability，均不产生 users 行，故
    无需额外排除。SQL 已保证 email_normalized 命中，这里只判定状态位。
    """
    if row is None:
        return False
    return (
        (row.get("role") or "") == user_store.ROLE_USER
        and not row.get("disabled")
        and (row.get("activation_state") or "active") == "active"
        and row.get("email_verified_at") is not None
    )


def _user_select_tx(cur, where_sql, params, lock=False):
    """按 where 取候选账号（显式列；不读 password_hash）。"""
    cur.execute(
        "SELECT user_id, login_id, email, email_normalized, role, disabled, "
        "activation_state, email_verified_at, auth_version "
        "FROM users WHERE " + where_sql + (" FOR UPDATE" if lock else ""),
        params)
    return cur.fetchone()


def request_password_reset(email, *, base_url, locale="zh",
                           ttl_seconds=PASSWORD_RESET_TTL_SECONDS):
    """发起密码重置（**公开匿名入口**：账号存在性绝不外泄）。

    单个 PostgreSQL 事务：
      1. 规范化 + 形态校验（非法 → PasswordResetError('bad_input')，由路由
         返回表单错误；这不是账号存在性信号）；
      2. ``SELECT ... FOR UPDATE`` 取规范化邮箱命中的账号（同账号并发请求
         串行化，保证「较新的请求作废旧 token」）；
      3. 同事务读取同邮箱配额（60s / 时 3 / 日 5）；超额 →
         ``kind='rate_limited'``（不入队，路由仍返回同一通用文案）；
      4. 账号不符合条件（未知/owner/禁用/未激活/无已验证邮箱）→
         ``kind='ineligible'``（**不写任何行**）；
      5. 符合条件：生成 token、构造冻结正文（zh/en 由 ``locale`` 决定）、
         作废同邮箱未消费旧作业（superseded，含 uncertain 单活语义）、
         INSERT purpose='password_reset' 作业（token_hash=域分离 HMAC，
         payload 绑定 user_id + auth_version 加密落库）。

    返回 ``{"kind": "accepted"|"rate_limited"|"ineligible",
    "email_masked": str}``——kind 只供路由层记日志/排水，**绝不进入响应**。
    明文 token 只在返回值出现一次（经邮件外发；绝不落库/进日志）。
    """
    try:
        email_norm = registration_store.validate_email(email)
    except registration_store.EmailVerifyError:
        raise PasswordResetError("bad_input")
    try:
        ttl = int(ttl_seconds)
    except (TypeError, ValueError):
        raise ValueError("ttl_seconds 需为整数")
    if ttl <= 0 or ttl > 24 * 3600:
        raise ValueError("ttl_seconds 需在 (0, 86400] 内")
    lang = "en" if str(locale or "zh").strip().lower() == "en" else "zh"
    masked = registration_store.mask_login_id(email_norm)

    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                user = _user_select_tx(
                    cur, "lower(email_normalized)=%s", (email_norm,),
                    lock=True)
                cooldown, hourly, daily = _quota_counts_tx(cur, email_norm)
                if not _eligible_user_row(user):
                    return {"kind": "ineligible", "email_masked": masked}
                if cooldown > 0 or hourly >= PASSWORD_RESET_HOURLY_LIMIT \
                        or daily >= PASSWORD_RESET_DAILY_LIMIT:
                    return {"kind": "rate_limited", "email_masked": masked}
                token = secrets.token_urlsafe(
                    registration_store.VERIFY_TOKEN_BYTES)
                subject, body = build_password_reset_body(
                    email_norm, token, base_url, lang)
                link = str(base_url).strip().rstrip("/") \
                    + "/reset-password?token=" + token
                payload_enc = mail_worker.encrypt_payload({
                    "subject": subject, "body": body,
                    "purpose": MAIL_PURPOSE_PASSWORD_RESET,
                    "email": email_norm,
                    "user_id": str(user["user_id"]),
                    # 绑定请求时的凭据版本：任何其它改密路径（本人改密 /
                    # 管理员重置 / break-glass / 邮箱改绑）推进版本后，
                    # 该 token 自动失效，无需额外清理。
                    "auth_version": int(user.get("auth_version") or 0),
                    "locale": lang,
                    "template_key": (TEMPLATE_KEY_EN if lang == "en"
                                     else TEMPLATE_KEY_ZH),
                    "template_data": {"email": email_norm, "link": link},
                })
                token_hash = registration_store.verify_token_hash(token)
                job_id = "rmj_" + secrets.token_urlsafe(8)
                # 作废同邮箱同用途全部未消费旧 token（一次性 + 单活）。
                # uncertain 必须在集合内：发送结果不确定的旧 token 同样可被
                # 消费，遗漏会留下新旧双活 token。
                cur.execute(
                    "UPDATE registration_mail_jobs SET status='superseded' "
                    "WHERE email_normalized=%s AND purpose=%s "
                    "AND consumed_at IS NULL "
                    "AND status IN ('queued','sent','uncertain')",
                    (email_norm, MAIL_PURPOSE_PASSWORD_RESET))
                cur.execute(
                    "INSERT INTO registration_mail_jobs "
                    "(job_id, purpose, email_normalized, token_hash, "
                    " payload_enc, status, expires_at) "
                    "VALUES (%s,%s,%s,%s,%s,'queued', "
                    " now() + (%s * interval '1 second'))",
                    (job_id, MAIL_PURPOSE_PASSWORD_RESET, email_norm,
                     token_hash, payload_enc, ttl))
    finally:
        conn.close()
    return {"kind": "accepted", "email_masked": masked}


def check_password_reset_token(token):
    """**只读**解析密码重置 token（GET /reset-password 用，绝不消费）。

    返回 ``{"state": "valid"|"expired"|"consumed"|"invalid"|"unknown",
    "email_masked": str|None}``：

    - 未命中 token → unknown；已消费 → consumed；已过期 → expired；
    - 作业状态非 queued/sent/uncertain（failed/superseded）→ invalid；
    - 解密失败 / payload 绑定缺失 / 账号已删除、已禁用、已非普通用户、
      未激活、邮箱未验证、auth_version 与 token 绑定不符 → invalid。

    email 只回掩码（mask_login_id）；任何解析异常都不产生写副作用。
    """
    tok = (token or "").strip()
    if not tok:
        return {"state": "unknown", "email_masked": None}
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                cur.execute(
                    "SELECT job_id, email_normalized, payload_enc, status, "
                    "consumed_at, "
                    "extract(epoch from expires_at)::float8 AS expires_at "
                    "FROM registration_mail_jobs "
                    "WHERE token_hash=%s AND purpose=%s",
                    (registration_store.verify_token_hash(tok),
                     MAIL_PURPOSE_PASSWORD_RESET))
                job = cur.fetchone()
                if job is None:
                    return {"state": "unknown", "email_masked": None}
                masked = registration_store.mask_login_id(
                    job["email_normalized"])
                if job["consumed_at"] is not None \
                        or job["status"] == "consumed":
                    return {"state": "consumed", "email_masked": masked}
                if job["status"] not in ("queued", "sent", "uncertain"):
                    return {"state": "invalid", "email_masked": masked}
                if job["expires_at"] is not None \
                        and job["expires_at"] <= time.time():
                    return {"state": "expired", "email_masked": masked}
                try:
                    payload = mail_worker.decrypt_payload(job["payload_enc"])
                except Exception:
                    return {"state": "invalid", "email_masked": masked}
                if str(payload.get("purpose") or "") \
                        != MAIL_PURPOSE_PASSWORD_RESET:
                    return {"state": "invalid", "email_masked": masked}
                user = _user_select_tx(
                    cur, "user_id=%s",
                    (str(payload.get("user_id") or ""),))
                if not _eligible_user_row(user) \
                        or int(payload.get("auth_version") or -1) != int(
                            user.get("auth_version") or 0) \
                        or registration_store.normalize_email(
                            payload.get("email")) \
                        != registration_store.normalize_email(
                            job["email_normalized"]):
                    return {"state": "invalid", "email_masked": masked}
                return {"state": "valid", "email_masked": masked}
    finally:
        conn.close()


def consume_password_reset(token, new_password, *, account_hash_salt="",
                           ip_prefix_hash=""):
    """消费重置 token 并单事务设置新密码（闭环提交步）。

    单个 PostgreSQL 事务：
      1. ``SELECT ... FOR UPDATE`` 取 purpose='password_reset' 作业；未命中/
         已消费/已作废/已过期/状态不可消费 → PasswordResetError
         ('invalid_or_expired')；
      2. 解密 payload；purpose/user_id 绑定校验；
      3. 锁定用户行并复核：存在、role=user（**owner 永不重置**）、未禁用、
         active、邮箱已验证、payload.auth_version == 当前 auth_version
         （任何其它改密路径已推进版本 → token 立即失效）；
      4. UPDATE users：password_hash 与 auth_version+1（全端 session 失效）；
      5. job 置 consumed（CAS：consumed_at IS NULL，二次消费必失败）；
      6. 同事务清该账号登录失败桶（scope=account；传入的 IP 前缀桶一并清，
         与成功登录同口径）；
      7. 同事务审计 ``user.password_reset_email``（actor=本人、target=本人，
         detail 只含掩码邮箱与 sessions_revoked，无 token/密码/hash）。

    新密码策略先于事务用 user_store.validate_password 校验（与本人改密/
    注册同一套 15..200、非全空白规则与文案）；违规抛 ValueError，不消费
    token。成功返回 ``{"user": 公共行快照, "email_masked": str}``；**不写
    session**（不自动登录，由路由层决定后续跳转）。
    """
    # 先验密码策略：非法密码不进入事务、不消耗 token（文案与 store 一致）
    user_store.validate_password(new_password)
    tok = (token or "").strip()
    if not tok:
        raise PasswordResetError("invalid_or_expired")
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                cur.execute(
                    "SELECT job_id, email_normalized, payload_enc, status, "
                    "consumed_at, "
                    "extract(epoch from expires_at)::float8 AS expires_at "
                    "FROM registration_mail_jobs "
                    "WHERE token_hash=%s AND purpose=%s FOR UPDATE",
                    (registration_store.verify_token_hash(tok),
                     MAIL_PURPOSE_PASSWORD_RESET))
                job = cur.fetchone()
                if job is None or job["consumed_at"] is not None \
                        or job["status"] not in ("queued", "sent",
                                                 "uncertain"):
                    raise PasswordResetError("invalid_or_expired")
                if job["expires_at"] is not None \
                        and job["expires_at"] <= time.time():
                    raise PasswordResetError("invalid_or_expired")
                try:
                    payload = mail_worker.decrypt_payload(job["payload_enc"])
                except Exception:
                    # 载荷损坏（密钥轮换等）：绝不能当成有效重置
                    raise PasswordResetError("invalid_or_expired")
                if str(payload.get("purpose") or "") \
                        != MAIL_PURPOSE_PASSWORD_RESET:
                    raise PasswordResetError("invalid_or_expired")
                user = _user_select_tx(
                    cur, "user_id=%s", (str(payload.get("user_id") or ""),),
                    lock=True)
                if user is None:
                    raise PasswordResetError("user_missing")
                if user.get("disabled"):
                    raise PasswordResetError("user_disabled")
                if not _eligible_user_row(user):
                    # owner / pending / 未验证邮箱：token 即使在手也拒绝
                    raise PasswordResetError("user_not_eligible")
                if int(payload.get("auth_version") or -1) != int(
                        user.get("auth_version") or 0):
                    raise PasswordResetError("invalid_or_expired")
                if registration_store.normalize_email(payload.get("email")) \
                        != registration_store.normalize_email(
                            job["email_normalized"]):
                    raise PasswordResetError("invalid_or_expired")
                cur.execute(
                    "UPDATE users SET password_hash=%s, "
                    "auth_version=auth_version+1 "
                    "WHERE user_id=%s "
                    "RETURNING user_id, login_id, role, auth_version",
                    (generate_password_hash(new_password),
                     user["user_id"]))
                updated = cur.fetchone()
                cur.execute(
                    "UPDATE registration_mail_jobs SET status='consumed', "
                    "consumed_at=now() WHERE job_id=%s AND "
                    "consumed_at IS NULL", (job["job_id"],))
                if (cur.rowcount or 0) != 1:
                    raise PasswordResetError("invalid_or_expired")
                # 清登录失败桶（同事务；与成功登录同口径：账号 + 本次来源
                # IP 前缀）。salt 由 app 层传入，保证 subject 与登录锁定
                # 使用同一份带盐 HMAC。
                account_hash = auth_limit_store.account_subject_hash(
                    user.get("login_id") or user.get("email_normalized")
                    or "", account_hash_salt)
                auth_limit_store.clear_auth_failures_tx(
                    cur, account_hash, ip_prefix_hash)
                masked = registration_store.mask_login_id(
                    user.get("email_normalized") or job["email_normalized"])
                import share_store_pg
                share_store_pg.record_audit_tx(
                    cur, "user.password_reset_email",
                    actor_user_id=user["user_id"],
                    actor_role=user.get("role") or user_store.ROLE_USER,
                    target_type="user", target_id=user["user_id"],
                    detail={"email_masked": masked,
                            "sessions_revoked": True})
    finally:
        conn.close()
    return {"user": dict(updated), "email_masked": masked}
