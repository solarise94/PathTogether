# -*- coding: utf-8 -*-
"""邀请注册存储原语（registration_invites，P0-B docs §4.2/§4.3/§4.4）。

账户系统批次 B（docs/account-system-simplification-fix-plan.md §8.2）：邀请
绑定字段的语义为**「允许兑换的登录账号 login_id」**，不是已验证邮箱。
兑换匹配的是用户自选的登录账号，display_name 不参与唯一性或邀请匹配。

账户系统批次 C（docs §4.2 物理收口）：函数签名与参数名随物理列一并收口为
login_id 口径——``create_invite``/``redeem_invite`` 参数 ``login_id``、
``normalize_login_id``/``mask_login_id``、SQL 列 ``login_id_normalized``
（0016 改名）、redeem_invite 返回 dict 的 "login_id" 键（原 "email" 键删除）。
内部细分 reason（email_mismatch/email_taken）为稳定标识符，维持不变。

安全不变量：

- 邀请码明文 ``secrets.token_urlsafe(32)``（≥32 字节 CSPRNG），只在
  ``create_invite`` 的返回值里出现一次（owner 经可信通道线下交付）；数据库只存
  带域分离盐的 HMAC-SHA-256（``token_hash`` UNIQUE），绝不存明文；
- ``redeem_invite`` 在**单个 PostgreSQL 事务**内完成：按 token_hash
  ``SELECT ... FOR UPDATE`` 锁行 → 检查撤销/过期/消费 → 绑定登录账号常数时间
  比较 → users 登录账号唯一检查 → 生成 user_id + Werkzeug 密码 hash 插入
  role=user（不走 user_store.create_user——它自开连接，会产生跨事务窗口）→
  消费 invite → 写注册审计 → commit。并发兑换同一邀请码只有一个成功；
- 对外（路由/调用方）所有兑换失败统一 ``InviteRedeemError``，公开 code 固定
  ``invite_invalid_or_unavailable``（不泄露不存在/已撤销/已消费/账号不匹配的
  细分状态）；细分 reason 只进 owner 审计 detail 与日志，且**绝不包含 token**；
- 审计只记 invite_id、actor、状态、被创建 user_id；不记 token、密码、完整 IP、
  明文登录账号（owner 列表也只显示掩码）；
- json/dual 后端 fail-closed（platform_features.require_pg_backend）。

Werkzeug 密码哈希沿用默认算法（当前 scrypt:32768:8:1），旧 hash 验证兼容由
``user_store.verify_user`` 的 check_password_hash 天然保留；本模块只做创建。
"""

import hashlib
import hmac
import logging
import os
import secrets
import time
from datetime import datetime, timezone
from urllib.parse import urlparse

import psycopg
from werkzeug.security import generate_password_hash

import pg_store
# P1-2（review）：生效注册模式的共享判定依赖 settings_store（存储值读取）；
# settings_store 只依赖 pg_store/platform_features，无导入环。
import settings_store
import user_store
# Batch B（docs review-2026-09-02-upload-user-limits-admin-ui-cleanup.md
# §Batch B 数据模型 6）：来源归因与注册解耦——兑换事务**不再**调用
# acquisition_store（user_acquisition 写路径冻结，站点统计故障绝不能阻断
# 注册）；历史查询函数保留。旧 pt_acq cookie 不再被本模块读取。
# R3 Wave1-Money 单轨：邀请模板金额只读 total_limit_nano_cny；R3 Wave2-
# Compat 收口：旧 monthly_limit_nano_cny 形参/列（0032 已把面值回填进
# total 列，0033 物理删列）、redeem_invite 的 acq 形参与「接受但忽略」
# 兼容响应键（acquisition/spend_override_policy）一并删除；
# 兑换事务**恒**为新用户建一次性总额度（spend_store
# .create_user_total_allowance_tx，source="invite"）——模板带面值按面值建行，
# 无面值解析全局默认（只查 ai_spend_total_defaults），皆缺 fail-closed 拒绝
# 兑换；不再有 window/override 过渡形态。spend_store 不回依赖本模块，无
# 循环导入。
import spend_store

_log = logging.getLogger("svs.registration")

#: 邀请码有效期默认 7 天（docs §4.2）
DEFAULT_INVITE_TTL_SECONDS = 7 * 86400
#: 邀请码明文字节数（docs §4.2：至少 32 字节 CSPRNG，URL-safe 展示）
INVITE_TOKEN_BYTES = 32
#: 服务端密码最小长度——统一引用 user_store 常量（账户系统批次 A docs §3.3），
#: 保留 MIN_PASSWORD_LENGTH 名字作兼容别名（app.py 注册表单校验在用）。
MIN_PASSWORD_LENGTH = user_store.PASSWORD_MIN_LENGTH
#: 服务端密码最大长度（同上统一来源；兑换防御层补齐上限校验）
MAX_PASSWORD_LENGTH = user_store.PASSWORD_MAX_LENGTH

#: I 线（设计文档第 8 节）新增模式；settings_store.REGISTRATION_MODES 为权威
#: 词表（本常量为本模块内防御性副本，保持同序语义）
REGISTRATION_MODES = ("closed", "invite_only",
                      "email_verify_invite_activation", "public")

#: I 线邮件线的目标模式常量（激活/重发/验证请求写前置与 worker drain 前置
#: 共用此名，避免调用方散落字符串字面量）
MODE_EMAIL_VERIFY_INVITE_ACTIVATION = "email_verify_invite_activation"

#: P1-2（review）：需要 fail-closed 前置闸的开放注册形态（public 原样透传给
#: 路由层统一 503；判定实现见 registration_mode_precondition_failures）
REGISTRATION_GATED_MODES = ("invite_only", "email_verify_invite_activation")


class RegistrationStoreError(RuntimeError):
    """registration_store 业务异常基类。"""

    code = "registration_error"


class InviteRedeemError(RegistrationStoreError):
    """兑换失败：对外统一 code，不泄露细分状态（细分 reason 仅进审计/日志）。"""

    code = "invite_invalid_or_unavailable"

    def __init__(self, reason, message=None):
        self.reason = str(reason)
        super().__init__(
            message or "邀请码无效或不可用，请联系管理员")
        # 防御：异常文本绝不携带 token（调用方可能把 str(exc) 回显/落日志）
        if len(self.reason) > 64:
            self.reason = self.reason[:64]


class InviteNotFoundError(RegistrationStoreError):
    """invite_id 不存在（owner 管理 API 用；非匿名通道，可 404）。"""

    code = "invite_not_found"


def _connect():
    """建连接并设 dict_row（本模块所有查询按列名访问）。"""
    conn = pg_store.connect()
    conn.row_factory = psycopg.rows.dict_row
    return conn


# --------------------------------------------------------------------------- #
# P1-2（review）：生效注册模式共享判定（app 层与 registration_mail_worker
# 共用同一实现；worker 绝不 import Flask app）
# --------------------------------------------------------------------------- #
def _env_truthy(env, name) -> bool:
    """env 布尔解析（与 app 层 _env_truthy 同口径：1/true/yes）。"""
    return (env.get(name) or "").strip().lower() in ("1", "true", "yes")


def registration_mode_precondition_failures(environ=None, mode=None) -> list:
    """开放注册形态生效的前置条件（docs §3.2 末段 + I 线模式前置；纯函数）。

    invite_only 与 email_verify_invite_activation 共同要求：
      1. ``PUBLIC_BASE_URL`` 配置为 https://（公网入口 TLS 已终止）；
      2. ``ADMIN_SESSION_COOKIE_SECURE`` 启用（session cookie 带 Secure）；
    email_verify_invite_activation 额外要求（I 线模式前置）：
      3. 邮件发送通道已配置（registration_mail_worker.sender_configured，
         ``fake`` 不计入生产通道）；
      4. 邮件载荷加密密钥可用（含明文 token 的冻结正文必须加密落库）；
      5. 验证 token 哈希盐非默认（REGISTRATION_VERIFY_HASH_SALT /
         AUTH_SUBJECT_HASH_SALT / SECRET_KEY 至少其一已配置）。
    任一不满足即应降级 closed（见 :func:`resolve_effective_registration_mode`）。
    app 层的 ``_registration_precondition_failures`` / ``_effective_registration
    _mode`` 与 worker drain 前置共用本实现，不再复制判定逻辑。
    """
    env = os.environ if environ is None else environ
    failures = []
    base_url = (env.get("PUBLIC_BASE_URL") or "").strip()
    if not base_url or urlparse(base_url).scheme != "https":
        failures.append("PUBLIC_BASE_URL 未配置为 https:// 入口")
    if not _env_truthy(env, "ADMIN_SESSION_COOKIE_SECURE"):
        failures.append("ADMIN_SESSION_COOKIE_SECURE 未启用（Secure Cookie）")
    if mode == MODE_EMAIL_VERIFY_INVITE_ACTIVATION:
        import registration_mail_worker as mail_worker
        if not mail_worker.sender_configured(env, production=True):
            failures.append("邮件发送通道未配置（REGISTRATION_MAIL_SENDER；"
                            "fake 不计入生产通道）")
        if not mail_worker.payload_key_available():
            failures.append("邮件载荷加密密钥不可用（"
                            "REGISTRATION_MAIL_PAYLOAD_KEY / SECRET_KEY）")
        if not ((env.get("REGISTRATION_VERIFY_HASH_SALT") or "").strip()
                or (env.get("AUTH_SUBJECT_HASH_SALT") or "").strip()
                or (env.get("SECRET_KEY") or "").strip()):
            failures.append("验证 token 哈希盐未配置（"
                            "REGISTRATION_VERIFY_HASH_SALT / SECRET_KEY）")
    return failures


def resolve_effective_registration_mode(environ=None):
    """生效注册模式（共享权威实现）：存储值 × 前置条件闸，返回 ``(mode,
    failures)``。

    - 存储读取失败按 closed（fail-closed，本地告警）；非开放形态（closed/
      public）原样透传且 failures 为空；
    - 开放形态前置不满足 → 降级 closed，failures 携带原因（是否告警由调用方
      决定：app 层每进程告警一次，worker 逐轮 info）；
    - ``public`` 不做前置判定，原样透传给路由层统一 503。
    """
    try:
        mode = settings_store.get_registration_mode()
    except Exception:
        _log.warning("读取 registration_mode 失败，按 closed 处理",
                     exc_info=True)
        mode = "closed"
    if mode not in REGISTRATION_GATED_MODES:
        return mode, []
    failures = registration_mode_precondition_failures(environ, mode=mode)
    if failures:
        return "closed", failures
    return mode, []


# --------------------------------------------------------------------------- #
# token 哈希（域分离盐；盐来源与 auth_limit_store 口径一致，可 env 覆盖）
# --------------------------------------------------------------------------- #
def _invite_hash_salt() -> str:
    """邀请码哈希盐：REGISTRATION_INVITE_HASH_SALT → AUTH_SUBJECT_HASH_SALT →
    SECRET_KEY → 固定域常量（token 本身高熵，盐主要用于域分离）。"""
    import os
    for name in ("REGISTRATION_INVITE_HASH_SALT", "AUTH_SUBJECT_HASH_SALT",
                 "SECRET_KEY"):
        v = (os.environ.get(name) or "").strip()
        if v:
            return v
    return "pt-registration-invite-v1"


def invite_token_hash(token: str) -> str:
    """邀请码明文 → 带域分离盐的 HMAC-SHA-256（限流桶与库内存储共用）。"""
    msg = (token or "").strip()
    return hmac.new(
        ("reginvite:" + _invite_hash_salt()).encode("utf-8"),
        msg.encode("utf-8"), hashlib.sha256).hexdigest()


def normalize_login_id(login_id) -> str:
    """登录账号规范化：strip + lower（与 user_store 写入侧一致）。

    规范化口径即 login_id 的唯一键规范化（docs §3.1/§8.2；原批次 B 名
    normalize_email，批次 C 随物理列改名）。
    """
    return str(login_id or "").strip().lower()


def mask_login_id(login_id) -> str:
    """owner 列表展示用登录账号掩码：保留首字符与域名（无 @ 则保留首字符）。"""
    s = str(login_id or "").strip()
    if not s:
        return ""
    if "@" in s:
        local, _, domain = s.partition("@")
        head = local[:1] if local else ""
        masked_local = (head + "***") if len(local) > 1 else "***"
        return masked_local + "@" + domain
    return (s[:1] + "***") if len(s) > 1 else "***"


def _consttime_eq(a, b) -> bool:
    """常数时间字符串比较（P0-1）：两侧规范化值等长补齐后 compare_digest，
    长度差不泄露信息。redeem_invite 与 activate_registered_user 的绑定校验
    共用同一实现。"""
    x = str(a or "").encode("utf-8")
    y = str(b or "").encode("utf-8")
    n = max(len(x), len(y), 1)
    return hmac.compare_digest(x + b"\0" * (n - len(x)),
                               y + b"\0" * (n - len(y)))


# --------------------------------------------------------------------------- #
# 内部用户创建原语（可接收 cursor，供同事务插入；docs §4.3）
# --------------------------------------------------------------------------- #
def _new_user_id() -> str:
    return "usr_" + secrets.token_urlsafe(8)


def _insert_user_locked(cur, login_id_normalized, password, display_name,
                        ai_access=False):
    """在**调用方事务的 cursor** 内插入 role=user 用户行，返回公共 dict。

    login_id_normalized 为规范化登录账号（docs §8.2）。与
    user_store_pg.create_user 的差异：不开连接、不独立提交（供 redeem_invite
    同事务使用）；users.lower(login_id) 唯一索引冲突时抛 psycopg
    UniqueViolation（由调用方在同一事务内翻译为统一错误并回滚）。返回 dict
    只带 "login_id" 键（批次 C 起无 "email" 别名，docs §4.2）。
    0037：invite_only 直接兑换的账号即 active（activation_source='invite'）；
    新形态 pending_activation 建号走 _insert_pending_user_tx，不经本函数。
    """
    uid = _new_user_id()
    name = str(display_name or "").strip() or login_id_normalized
    cur.execute(
        "INSERT INTO users "
        "(user_id, login_id, display_name, password_hash, role, created_at, "
        " disabled, ai_config, ai_access, "
        " activation_state, activation_source, activation_updated_at) "
        "VALUES (%s,%s,%s,%s,'user', now(), FALSE, '{}'::jsonb, %s, "
        " 'active', 'invite', now()) "
        "RETURNING user_id, login_id, display_name, role, "
        "extract(epoch from created_at)::float8 AS created_at, disabled, "
        "ai_config, ai_access",
        (uid, login_id_normalized, name, generate_password_hash(password),
         bool(ai_access)),
    )
    row = cur.fetchone()
    out = dict(row)
    out["ai_access"] = bool(out.get("ai_access"))
    return out


def _insert_audit(cur, action, actor_user_id, target_type, target_id, detail):
    """事务内写注册审计（detail 绝不含 token/密码/完整 IP）。"""
    cur.execute(
        "INSERT INTO audit_events "
        "(event_id, ts, actor_user_id, actor_role, action, target_type, "
        " target_id, slide, detail) "
        "VALUES (%s, now(), %s, %s, %s, %s, %s, NULL, %s)",
        ("aud_" + secrets.token_hex(16), actor_user_id or None, "",
         str(action), target_type or None, target_id or None,
         psycopg.types.json.Jsonb(detail if isinstance(detail, dict) else {})),
    )


# --------------------------------------------------------------------------- #
# 邀请创建 / 查询 / 撤销（owner 管理 API 数据源）
# --------------------------------------------------------------------------- #
_INVITE_SEL = (
    "invite_id, login_id_normalized, created_by_user_id, "
    "extract(epoch from created_at)::float8 AS created_at, "
    "extract(epoch from expires_at)::float8 AS expires_at, max_uses, use_count, "
    "extract(epoch from consumed_at)::float8 AS consumed_at, "
    "consumed_by_user_id, extract(epoch from revoked_at)::float8 AS revoked_at, "
    "ai_access, cohort, note, source_code, campaign_id, "
    "total_limit_nano_cny"
)
#: Batch B 起新邀请不再写入的退役列（列保留读取历史行；值为迁移前遗留。
#: 旧 monthly_limit_nano_cny 列已随 R3 Wave2-Compat 的 0033 迁移物理删除）
_RETIRED_INVITE_FIELDS = ("cohort", "source_code", "campaign_id")


def _invite_out(row) -> dict:
    out = dict(row)
    out["ai_access"] = bool(out.get("ai_access"))
    if out.get("total_limit_nano_cny") is not None:
        out["total_limit_nano_cny"] = int(out["total_limit_nano_cny"])
    return out


def _validate_total_limit(total_limit_nano_cny):
    """邀请模板总额度校验：None（兑换时按 ai_spend_total_defaults 默认建行，
    见 redeem_invite）或非负整数 nano。"""
    if total_limit_nano_cny is None:
        return None
    if isinstance(total_limit_nano_cny, bool) \
            or not isinstance(total_limit_nano_cny, int) \
            or total_limit_nano_cny < 0:
        raise ValueError(
            "total_limit_nano_cny 需为非负整数（nano-CNY）或 null")
    return int(total_limit_nano_cny)


def create_invite(created_by_user_id, login_id=None,
                  ttl_seconds=DEFAULT_INVITE_TTL_SECONDS,
                  ai_access=False, cohort="", note="",
                  source_code="", campaign_id=None,
                  total_limit_nano_cny=None):
    """创建一次性邀请码。返回 dict：含**明文 token**（唯一出现处）与行信息。

    Batch B（§4.4/§Batch B 数据模型 6）：邀请只负责注册——绑定登录账号、
    有效期、AI access、初始 user 总额度、备注、状态；**不再携带来源**：

    - ``source_code``/``campaign_id``/``cohort`` 参数**兼容保留但忽略并
      弃用**（app.py wave 2 才改调用方；本波保持旧签名可调用）：不做 slug/
      campaign 存在性校验，也**不写入 DB**（新邀请行三列为空/NULL）；
    - ``total_limit_nano_cny``：初始一次性总额度（nano-CNY 整数，wire 层
      十进制字符串，路由层换算）；None = 兑换时按 ai_spend_total_defaults
      解析全局默认（皆缺 fail-closed 拒绝兑换），语义见 redeem_invite。
      旧 ``monthly_limit_nano_cny`` 形参已随 R3 Wave2-Compat 删除（路由层
      对 body 带该键一律 400 retired_spend_field）。

    - token：``secrets.token_urlsafe(32)``；库内只存 invite_token_hash(token)；
    - login_id 给出时按 normalize_login_id 绑定——语义为「允许兑换的登录账号
      login_id」（docs §8.2）；None = 不绑定（owner 明确选择的高风险选项）；
    - ai_access/note 为邀请模板：兑换时决定新用户平台 AI 权限。
    """
    if not isinstance(created_by_user_id, str) or not created_by_user_id:
        raise ValueError("created_by_user_id 不能为空")
    bound = normalize_login_id(login_id) if login_id else ""
    if login_id and not bound:
        raise ValueError("绑定登录账号不能为空")
    try:
        ttl = int(ttl_seconds)
    except (TypeError, ValueError):
        raise ValueError("ttl_seconds 需为整数")
    if ttl <= 0:
        raise ValueError("ttl_seconds 需为正整数")
    note = str(note or "").strip()[:200]
    effective_total = _validate_total_limit(total_limit_nano_cny)

    for _ in range(5):  # token_hash 撞唯一键概率可忽略，重试兜底
        token = secrets.token_urlsafe(INVITE_TOKEN_BYTES)
        token_hash = invite_token_hash(token)
        invite_id = "inv_" + secrets.token_urlsafe(8)
        conn = _connect()
        try:
            with pg_store.transaction(conn) as c:
                with c.cursor() as cur:
                    cur.execute(
                        "INSERT INTO registration_invites "
                        "(invite_id, token_hash, login_id_normalized, "
                        " created_by_user_id, created_at, expires_at, "
                        " max_uses, use_count, ai_access, note, "
                        " total_limit_nano_cny) "
                        "VALUES (%s,%s,%s,%s, now(), "
                        " now() + (%s * interval '1 second'), 1, 0, %s, "
                        " %s, %s) "
                        "RETURNING " + _INVITE_SEL,
                        (invite_id, token_hash, bound or None,
                         created_by_user_id, ttl, bool(ai_access),
                         note, effective_total),
                    )
                    row = cur.fetchone()
                    _insert_audit(
                        cur, "registration.invite_create", created_by_user_id,
                        "registration_invite", invite_id,
                        {"email_bound": bool(bound), "ai_access":
                         bool(ai_access),
                         "ttl_seconds": ttl,
                         "total_limit_nano_cny": effective_total})
        except psycopg.errors.UniqueViolation:
            continue  # finally 先关连接再重试
        finally:
            conn.close()
        out = _invite_out(row)
        out["token"] = token  # 明文仅此一次返回（路由层 no-store）
        return out
    raise RegistrationStoreError("邀请码生成冲突，请重试")


def list_invites(limit=200):
    """列出邀请（**不含 token_hash**；最新在前）。邮箱掩码由路由层做。"""
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                cur.execute(
                    "SELECT " + _INVITE_SEL +
                    " FROM registration_invites ORDER BY created_at DESC, "
                    "invite_id LIMIT %s", (max(1, min(int(limit), 1000)),))
                rows = cur.fetchall()
        return [_invite_out(r) for r in rows]
    finally:
        conn.close()


def get_invite(invite_id):
    """按 invite_id 取行（不含 token_hash）；不存在返回 None。"""
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                cur.execute(
                    "SELECT " + _INVITE_SEL +
                    " FROM registration_invites WHERE invite_id=%s",
                    (invite_id,))
                row = cur.fetchone()
        return _invite_out(row) if row is not None else None
    finally:
        conn.close()


def revoke_invite(invite_id, revoked_by_user_id):
    """撤销未消费的邀请（幂等：已撤销原样返回；已消费拒绝撤销）。"""
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                cur.execute(
                    "SELECT " + _INVITE_SEL +
                    " FROM registration_invites WHERE invite_id=%s FOR UPDATE",
                    (invite_id,))
                row = cur.fetchone()
                if row is None:
                    raise InviteNotFoundError(invite_id)
                if row["consumed_at"] is not None:
                    raise RegistrationStoreError("邀请码已被使用，不能撤销")
                if row["revoked_at"] is None:
                    cur.execute(
                        "UPDATE registration_invites SET revoked_at=now() "
                        "WHERE invite_id=%s RETURNING " + _INVITE_SEL,
                        (invite_id,))
                    row = cur.fetchone()
                    _insert_audit(
                        cur, "registration.invite_revoke", revoked_by_user_id,
                        "registration_invite", invite_id, {})
        return _invite_out(row)
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# 原子兑换（docs §4.3）
# --------------------------------------------------------------------------- #
class _RedeemFail(Exception):
    """内部控制流：携带细分 reason，最终统一翻译为 InviteRedeemError。"""

    def __init__(self, reason):
        self.reason = reason
        super().__init__(reason)


def redeem_invite(token, login_id, password, display_name=None):
    """兑换邀请码并在**同一事务**内创建 role=user 账号。

    ``login_id`` 参数为兑换人自选的**登录账号**（docs §8.2；原批次 B 参数名
    email，批次 C 收口改名）。

    Batch B（§4.4/§Batch B 数据模型 6）+ R3 Wave1-Money 单轨 + Wave2-Compat：

    - 归因与注册彻底解耦：本事务不读取 pt_acq cookie 上下文、不调用
      acquisition_store、**不写 user_acquisition**（写路径冻结）——站点统计
      故障绝不能阻断注册。原 ``acq``「接受但忽略」形参与恒 None 的兼容响应
      键（acquisition/spend_override_policy）已随 R3 Wave2-Compat 物理删除；
    - 额度面**恒**为一次性总额度（与建号同契约，原 ``user_spend_target``
      分叉已拆除）：模板金额非 NULL（``total_limit_nano_cny`` 列；0032 已把
      旧 monthly 面值回填，0033 物理删列）按面值建行，无面值则
      解析全局默认（**只查** ai_spend_total_defaults，default_version=默认
      版本），皆缺 → ValueError ``total_default_missing``（注册端点统一兜
      底，兑换整体回滚）——绝不建出无额度行的用户；写入失败同样整体回滚
      （invite 不消费、用户不创建）；
    - 新兑换 audit（registration.redeem）不含 source/campaign/cohort/acq
      字段（历史 audit 不可变，照旧保留）。

    失败一律抛 ``InviteRedeemError``（对外统一 code
    ``invite_invalid_or_unavailable``）：
      - 无效/随机 token、过期、撤销、已消费（细分 reason：not_found / expired /
        revoked / consumed）；
      - 绑定登录账号不匹配（email_mismatch——稳定 reason 标识符，批次 C 维持
        不变；常数时间比较规范化值）；
      - users 登录账号已存在（email_taken；此时 invite 未消费——检查先于
        UPDATE）；
    成功返回 ``{"user": <新用户公共 dict>, "invite_id": ..., "login_id": ...,
    "total_allowance": <总额度行 dict>}``（login_id 键即规范化登录账号）。
    成功审计在同一事务内（registration.redeem，actor=被创建 user_id）；失败审计
    在独立 best-effort 事务（主事务已随异常回滚），detail 只含 invite_id/status。

    review R2-F2（与 cutover 串行化 + 维护闸）：事务内 cursor 就绪后、读
    invite 行之前，与建号同款三段式——先查 cutover 维护闸
    （spend_store.is_dispatch_maintenance_tx：缺键按开闸、读取异常 fail-closed）→
    取用户开通 advisory 锁（spend_store.acquire_user_provisioning_lock_tx，
    与 cutover 脚本会话级同键锁互斥串行）→ 复查维护闸（等锁期间闸可能
    开启）。维护中抛 spend_store.ProvisioningMaintenanceError
    （code=ai_dispatch_maintenance）：本异常**不**译成 InviteRedeemError，
    原样上抛——注册端点的 generic Exception→503「注册暂不可用」兜底对其
    语义正确（邀请行未被读取、FOR UPDATE 未加锁、invite 必未消费）。
    """
    tok = (token or "").strip()
    norm_login = normalize_login_id(login_id)
    if not tok or not isinstance(password, str) or not norm_login \
            or not password.strip() \
            or len(password) < MIN_PASSWORD_LENGTH \
            or len(password) > MAX_PASSWORD_LENGTH:
        # 输入形状问题也按统一错误处理（路由层已做过表单校验，这里是防御层；
        # 全空白拒绝与 user_store/_validate_password 及 useradmin CLI 对齐）
        raise InviteRedeemError("bad_input")
    token_hash = invite_token_hash(tok)
    fail_invite_id = None

    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                # review R2-F2 三段式（顺序固定，先于读 invite 行/FOR UPDATE）：
                # 闸检查 → 开通锁 → 复查闸，与 create_user_with_total_allowance
                # 同序，保证建号/兑换两入口与 cutover 三方互斥串行
                if spend_store.is_dispatch_maintenance_tx(cur):
                    raise spend_store.ProvisioningMaintenanceError(
                        "系统维护中（cutover），暂停止注册；请稍后重试")
                spend_store.acquire_user_provisioning_lock_tx(cur)
                if spend_store.is_dispatch_maintenance_tx(cur):
                    raise spend_store.ProvisioningMaintenanceError(
                        "系统维护中（cutover），暂停止注册；请稍后重试")
                cur.execute(
                    "SELECT invite_id, token_hash, login_id_normalized, "
                    "extract(epoch from expires_at)::float8 AS expires_at, "
                    "max_uses, use_count, consumed_at, revoked_at, ai_access, "
                    "cohort, source_code, campaign_id, "
                    "total_limit_nano_cny "
                    "FROM registration_invites WHERE token_hash=%s "
                    "FOR UPDATE",
                    (token_hash,))
                row = cur.fetchone()
                now = time.time()
                if row is None:
                    raise _RedeemFail("not_found")
                invite_id = fail_invite_id = row["invite_id"]
                if row["revoked_at"] is not None:
                    raise _RedeemFail("revoked")
                if row["expires_at"] is not None and row["expires_at"] <= now:
                    raise _RedeemFail("expired")
                if row["consumed_at"] is not None or \
                        int(row["use_count"] or 0) >= int(row["max_uses"] or 1):
                    raise _RedeemFail("consumed")
                bound = row["login_id_normalized"]
                if bound:
                    # 常数时间比较（规范化值等长补齐，长度差不泄露信息）
                    if not _consttime_eq(norm_login, str(bound)):
                        raise _RedeemFail("email_mismatch")
                # users 登录账号唯一检查（在消费 invite 之前；冲突则整体回滚不消费）
                cur.execute(
                    "SELECT 1 FROM users WHERE lower(login_id)=lower(%s) LIMIT 1",
                    (norm_login,))
                if cur.fetchone() is not None:
                    raise _RedeemFail("email_taken")
                try:
                    user = _insert_user_locked(
                        cur, norm_login, password, display_name,
                        ai_access=bool(row["ai_access"]))
                except psycopg.errors.UniqueViolation:
                    raise _RedeemFail("email_taken")
                cur.execute(
                    "UPDATE registration_invites SET use_count=use_count+1, "
                    "consumed_at=now(), consumed_by_user_id=%s "
                    "WHERE invite_id=%s "
                    "AND consumed_at IS NULL AND revoked_at IS NULL",
                    (user["user_id"], invite_id))
                if (cur.rowcount or 0) != 1:
                    # FOR UPDATE 下不可达；防御性回滚（CAS 失败=状态已变）
                    raise _RedeemFail("consumed")
                # 额度面恒为一次性总额度（与建号同契约，单轨）：模板带
                # total_limit_nano_cny 面值直接建行（0032 已回填旧 monthly
                # 面值并随 0033 物理删列），无面值解析全局默认（只查
                # defaults 表），皆缺 → fail-closed 拒绝兑换（绝不建出无
                # 额度行的用户）
                invite_limit = row["total_limit_nano_cny"]
                allowance = None
                try:
                    if invite_limit is not None:
                        limit, dver = int(invite_limit), None
                    else:
                        limit, _src, dver = spend_store._resolve_total_default_tx(
                            cur, datetime.now(timezone.utc))
                        if limit is None:
                            raise ValueError(
                                "total_default_missing: 无可用默认总额度"
                                "（ai_spend_total_defaults 缺行）；"
                                "请先配置邀请面值或设置默认")
                    allowance = spend_store.create_user_total_allowance_tx(
                        cur, user["user_id"], limit, source="invite",
                        default_version=dver,
                        updated_by="invite:" + invite_id)
                except Exception:
                    _log.warning(
                        "邀请模板额度写入失败（整体回滚，不建号不消费邀请）",
                        exc_info=True)
                    raise
                _audit_redeem(cur, invite_id, "success",
                              created_user_id=user["user_id"])
        return {"user": user, "invite_id": invite_id,
                "login_id": norm_login,
                "total_allowance": allowance}
    except _RedeemFail as exc:
        _audit_redeem_best_effort(fail_invite_id, exc.reason)
        raise InviteRedeemError(exc.reason)
    except psycopg.errors.UniqueViolation:
        # users.lower(login_id) 唯一索引冲突（检查与插入之间的并发窗口）
        _audit_redeem_best_effort(fail_invite_id, "email_taken")
        raise InviteRedeemError("email_taken")
    finally:
        conn.close()


def _audit_redeem(cur, invite_id, status, created_user_id=None):
    """兑换成功审计（同事务）：只记 invite_id / actor（被创建 user_id）/ 状态。

    绝不记 token、密码、完整 IP、明文邮箱（docs §4.2/§4.4）。
    """
    _insert_audit(
        cur, "registration.redeem", created_user_id, "registration_invite",
        invite_id, {"status": str(status)})


def _audit_redeem_best_effort(invite_id, status):
    """兑换失败审计（独立小事务，主事务已回滚）；写失败只记日志不抛。"""
    try:
        conn = _connect()
        try:
            with pg_store.transaction(conn) as c:
                with c.cursor() as cur:
                    _insert_audit(
                        cur, "registration.redeem_attempt", None,
                        "registration_invite", invite_id,
                        {"status": str(status or "unknown")})
        finally:
            conn.close()
    except Exception:
        _log.warning("registration.redeem_attempt 审计写入失败（status=%s）",
                     status, exc_info=True)


# =========================================================================== #
# I+J：邮箱验证 + 邀请码激活（设计文档第 8 节 + review J / P2-4）
#
# 状态机：email_pending（无 users 行，物化为未消费 registration_mail_jobs）
#   → pending_activation（POST /api/registration/verify 原子建号）
#   → active（activate_registered_user 单事务）。disabled 与状态机正交。
#
# 安全不变量（与 redeem_invite 同款纪律）：
#   - 验证 token：32 字节 CSPRNG（token_urlsafe）、30 分钟、一次性、只存
#     域分离 HMAC（registration_mail_jobs.token_hash）；含明文 token 的冻结
#     正文经 registration_mail_worker.encrypt_payload 加密后落库；
#   - 验证邮箱**不授予** workspace / AI / 额度：verify 建号 ai_access=FALSE、
#     不建 allowance、activation_state=pending_activation；只有邀请码激活
#     事务才建一次性总额度；
#   - J：新用户 login_id = 规范化邮箱（唯一用户名）；与存量 login_id 冲突时
#     进「待补绑」——新行用合成不可投递 login_id，email 身份列照常落库
#     （底层关联仍 user_id，切片/标注/会话/账单零丢失），owner 可后续补绑；
#   - 邮箱身份唯一由 users_email_identity_key（0037 部分唯一索引，
#     pending_activation+active 两态）兜底；同邮箱并发 verify 只有一行成功；
#   - 激活事务锁序沿用 provisioning 闸三段式（闸检查 → advisory 锁 → 复查）
#     → 锁 user 行 → 锁 invite 行 → CAS 消费；**绝不**调用 redeem_invite
#     （它插入第二个用户）；
#   - already_active 再提交另一码：不消费、不充值（先查状态后读 invite）。
# =========================================================================== #
import re as _re

#: 验证 token 明文字节数（与邀请码同级 ≥32 字节 CSPRNG）
VERIFY_TOKEN_BYTES = 32
#: 验证 token 有效期（30 分钟，设计文档第 8 节）
VERIFY_TOKEN_TTL_SECONDS = 30 * 60

#: 配额（同邮箱维度，权威数据源 = registration_mail_jobs 行数）：
#: 60s 冷却 / 每小时 3 / 每天 5；应用全局日预算 40。
VERIFY_COOLDOWN_SECONDS = 60
VERIFY_HOURLY_LIMIT = 3
VERIFY_DAILY_LIMIT = 5
VERIFY_APP_DAILY_BUDGET = 40

#: 邮件用途（0037 CHECK 约束同词表）
MAIL_PURPOSE_EMAIL_VERIFY = "email_verify"

#: 宽松但可用的邮箱形态校验（注册端入口形状校验；唯一性以规范化值为准）
_EMAIL_RE = _re.compile(
    r"^[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@"
    r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
    r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)+$")


class EmailVerifyError(RegistrationStoreError):
    """邮箱验证失败。``code`` 稳定：invalid_or_expired / email_taken /
    bad_input / rate_limited。（对外文案统一由路由层决定。）"""

    def __init__(self, code, message=None):
        self.code = str(code)
        super().__init__(message or self.code)


class ActivationError(RegistrationStoreError):
    """已注册用户激活失败。``code`` 稳定：user_missing / user_disabled /
    not_pending / already_active（already_active 不消费不充值）。"""

    code = "activation_failed"

    def __init__(self, code, message=None):
        self.code = str(code)
        super().__init__(message or self.code)


def normalize_email(email) -> str:
    """邮箱规范化（J 唯一用户名口径）：strip + lower。与 login_id 同口径。"""
    return str(email or "").strip().lower()


def validate_email(email) -> str:
    """注册入口邮箱校验：规范化后必须命中形态、长度 6..254、local ≤64。
    返回规范化值；违规抛 EmailVerifyError('bad_input')。"""
    norm = normalize_email(email)
    if not norm or len(norm) > 254 or "@" not in norm:
        raise EmailVerifyError("bad_input")
    local = norm.split("@", 1)[0]
    if not local or len(local) > 64:
        raise EmailVerifyError("bad_input")
    if not _EMAIL_RE.match(norm):
        raise EmailVerifyError("bad_input")
    return norm


def _verify_hash_salt() -> str:
    """验证 token 哈希盐（域分离；口径同 invite_token_hash 的盐链）。"""
    import os
    for name in ("REGISTRATION_VERIFY_HASH_SALT", "AUTH_SUBJECT_HASH_SALT",
                 "SECRET_KEY"):
        v = (os.environ.get(name) or "").strip()
        if v:
            return v
    return "pt-registration-verify-v1"


def verify_token_hash(token: str) -> str:
    """验证 token 明文 → 域分离 HMAC-SHA-256（registration_mail_jobs.token_hash）。"""
    msg = (token or "").strip()
    return hmac.new(
        ("regverify:" + _verify_hash_salt()).encode("utf-8"),
        msg.encode("utf-8"), hashlib.sha256).hexdigest()


def _new_verify_token() -> str:
    return secrets.token_urlsafe(VERIFY_TOKEN_BYTES)


# --------------------------------------------------------------------------- #
# 入队（start / resend 同事务入队 + 配额）
# --------------------------------------------------------------------------- #
def _verify_quota_counts_tx(cur, email_norm):
    """同事务读取该邮箱配额占用：(cooldown, hourly, daily, app_daily)。"""
    cur.execute(
        "SELECT "
        " count(*) FILTER (WHERE created_at > now() - interval '"
        + str(VERIFY_COOLDOWN_SECONDS) + " seconds') AS cooldown, "
        " count(*) FILTER (WHERE created_at > now() - interval '1 hour') "
        "   AS hourly, "
        " count(*) FILTER (WHERE created_at > now() - interval '24 hours') "
        "   AS daily "
        "FROM registration_mail_jobs WHERE email_normalized=%s",
        (email_norm,))
    row = cur.fetchone()
    cur.execute(
        "SELECT count(*) AS app_daily FROM registration_mail_jobs "
        "WHERE created_at > now() - interval '24 hours'")
    return (int(row["cooldown"]), int(row["hourly"]), int(row["daily"]),
            int(cur.fetchone()["app_daily"]))


def enqueue_email_verification(email, base_url=None,
                               ttl_seconds=VERIFY_TOKEN_TTL_SECONDS):
    """请求邮箱验证（start/resend 共用）：配额 → 作废旧 token → 入队。

    单个 PostgreSQL 事务内完成：
      1. 配额检查（权威数据源 = registration_mail_jobs 行数；超限抛
         EmailVerifyError('rate_limited')，路由层对外与成功**同一文案**）；
      2. 该邮箱未消费旧作业全部作废（status='superseded'）——同邮箱任意
         时刻至多一个可用 token（一次性语义的一部分）；
      3. INSERT 新作业（token_hash + 加密冻结正文；明文 token 绝不落库）。

    冻结正文由 registration_mail_worker.build_verify_email_body 构造（含
    ``<base_url>/verify-email?token=<明文>`` 链接）并经 encrypt_payload 加密。

    返回 ``{"job_id", "email", "token", "expires_at"}``——``token`` 明文
    **只在返回值出现一次**（经邮件外发；绝不进日志/审计/URL 以外存储）。
    """
    import registration_mail_worker as mail_worker
    email_norm = validate_email(email)
    try:
        ttl = int(ttl_seconds)
    except (TypeError, ValueError):
        raise ValueError("ttl_seconds 需为整数")
    if ttl <= 0 or ttl > 24 * 3600:
        raise ValueError("ttl_seconds 需在 (0, 86400] 内")
    token = _new_verify_token()
    subject, body = mail_worker.build_verify_email_body(
        email_norm, token, base_url)
    payload_enc = mail_worker.encrypt_payload(
        {"subject": subject, "body": body, "purpose": MAIL_PURPOSE_EMAIL_VERIFY,
         "email": email_norm})
    token_hash = verify_token_hash(token)
    job_id = "rmj_" + secrets.token_urlsafe(8)
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                cooldown, hourly, daily, app_daily = \
                    _verify_quota_counts_tx(cur, email_norm)
                if cooldown > 0 or hourly >= VERIFY_HOURLY_LIMIT \
                        or daily >= VERIFY_DAILY_LIMIT \
                        or app_daily >= VERIFY_APP_DAILY_BUDGET:
                    raise EmailVerifyError("rate_limited")
                # 作废同邮箱全部未消费旧 token（一次性 + 单活）：
                # P1-1 起 uncertain（发送结果不确定）同样持有可用链接，一并
                # 作废，维持「同邮箱任意时刻至多一个可用 token」
                cur.execute(
                    "UPDATE registration_mail_jobs SET status='superseded' "
                    "WHERE email_normalized=%s AND purpose=%s "
                    "AND consumed_at IS NULL "
                    "AND status IN ('queued','sent','uncertain')",
                    (email_norm, MAIL_PURPOSE_EMAIL_VERIFY))
                cur.execute(
                    "INSERT INTO registration_mail_jobs "
                    "(job_id, purpose, email_normalized, token_hash, "
                    " payload_enc, status, expires_at) "
                    "VALUES (%s,%s,%s,%s,%s,'queued', "
                    " now() + (%s * interval '1 second')) "
                    "RETURNING extract(epoch from expires_at)::float8 "
                    "AS expires_at",
                    (job_id, MAIL_PURPOSE_EMAIL_VERIFY, email_norm,
                     token_hash, payload_enc, ttl))
                expires_at = float(cur.fetchone()["expires_at"])
    except psycopg.errors.UniqueViolation:
        # token_hash 撞唯一键概率可忽略；防御性统一失败
        raise EmailVerifyError("bad_input")
    finally:
        conn.close()
    return {"job_id": job_id, "email": email_norm, "token": token,
            "expires_at": expires_at}


def check_verify_token(token):
    """**只读**解析验证 token（GET /verify-email 用，绝不消费）。

    返回 ``{"state": "valid"|"expired"|"consumed"|"unknown",
    "email_masked": str|None}``——email 掩码展示（mask_login_id），页面不
    全量回显。未知/非法 token 与过期统一可区分（持链接者本地状态展示），
    但都不产生任何写副作用。
    """
    tok = (token or "").strip()
    if not tok:
        return {"state": "unknown", "email_masked": None}
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                cur.execute(
                    "SELECT email_normalized, status, consumed_at, "
                    "extract(epoch from expires_at)::float8 AS expires_at "
                    "FROM registration_mail_jobs "
                    "WHERE token_hash=%s AND purpose=%s",
                    (verify_token_hash(tok), MAIL_PURPOSE_EMAIL_VERIFY))
                row = cur.fetchone()
    finally:
        conn.close()
    if row is None:
        return {"state": "unknown", "email_masked": None}
    masked = mask_login_id(row["email_normalized"])
    if row["consumed_at"] is not None or row["status"] == "consumed":
        return {"state": "consumed", "email_masked": masked}
    # P1-1：uncertain（发送结果不确定=用户可能已收到邮件）在有效期内与
    # queued/sent 同样按 valid 处理，防止「收到的链接被判无效」
    if row["status"] not in ("queued", "sent", "uncertain"):
        return {"state": "unknown", "email_masked": masked}
    if row["expires_at"] is not None and row["expires_at"] <= time.time():
        return {"state": "expired", "email_masked": masked}
    return {"state": "valid", "email_masked": masked}


# --------------------------------------------------------------------------- #
# 邮箱确认 → 原子创建 pending_activation 用户（密码在邮箱确认之后设置）
# --------------------------------------------------------------------------- #
def _pending_bind_login_id() -> str:
    """login_id 冲突时的合成「待补绑」登录名：不可投递、唯一、可识别。"""
    return "pending-" + secrets.token_hex(8) + "@bind.invalid"


def _insert_pending_user_tx(cur, email_norm, email_raw, password):
    """同事务插入 pending_activation 用户行（J：login_id=规范化邮箱）。

    - login_id = 规范化邮箱；users_login_id_ci_key 冲突 → 合成待补绑
      login_id 重试一次（**不**失败注册、**不**合并账号；email 身份唯一由
      users_email_identity_key 保证，底层业务关联仍是 user_id）。冲突分类
      在 SAVEPOINT 内完成（UniqueViolation 会中止事务，必须先
      ROLLBACK TO SAVEPOINT 才能继续同事务插入）；
    - users_email_identity_key 冲突 → EmailVerifyError('email_taken')
      （pending_activation/active 两态内该邮箱已占用）；
    - ai_access=FALSE、无 allowance：验证邮箱不授予任何权限（设计文档第 8 节）。
    返回用户公共 dict。
    """
    uid = _new_user_id()
    # display_name 默认同邮箱（J：展示主列是邮箱；显示名不冒充身份）
    for attempt, login in ((1, email_norm),
                           (2, _pending_bind_login_id())):
        cur.execute("SAVEPOINT insert_pending_try")
        try:
            cur.execute(
                "INSERT INTO users "
                "(user_id, login_id, display_name, password_hash, role, "
                " created_at, disabled, ai_config, ai_access, "
                " activation_state, activation_source, activation_updated_at,"
                " email, email_normalized, email_verified_at) "
                "VALUES (%s,%s,%s,%s,'user', now(), FALSE, '{}'::jsonb, "
                " FALSE, 'pending_activation', 'invite_activation', now(), "
                " %s, %s, now()) "
                "RETURNING user_id, login_id, display_name, role, "
                "extract(epoch from created_at)::float8 AS created_at, "
                "disabled, ai_config, ai_access, auth_version, "
                "activation_state, activation_source, "
                "extract(epoch from activation_updated_at)::float8 AS "
                "activation_updated_at, email, email_normalized, "
                "extract(epoch from email_verified_at)::float8 AS "
                "email_verified_at",
                (uid, login, email_norm,
                 generate_password_hash(password), email_raw, email_norm))
            row = dict(cur.fetchone())
            cur.execute("RELEASE SAVEPOINT insert_pending_try")
            return row
        except psycopg.errors.UniqueViolation as exc:
            cur.execute("ROLLBACK TO SAVEPOINT insert_pending_try")
            name = getattr(getattr(exc, "diag", None),
                           "constraint_name", "") or ""
            text = str(exc)
            if "users_email_identity_key" in name or \
                    "users_email_identity_key" in text:
                raise EmailVerifyError("email_taken") from exc
            if attempt == 1 and ("users_login_id_ci_key" in name
                                 or "users_login_id_ci_key" in text
                                 or "login_id" in text):
                # 存量同 login_id 账号：待补绑，不合并（review J 红线）
                _log.warning(
                    "verify 建号 login_id 冲突（email=%s 掩码待审）：进待"
                    "补绑", mask_login_id(email_norm))
                continue
            raise
    raise EmailVerifyError("bad_input")


def verify_email_create_user(token, password, display_name=None):
    """消费验证 token 并**原子创建** pending_activation 用户（同一事务）。

    - token 一次性：``SELECT ... FOR UPDATE`` 后置 consumed_at；无效/过期/
      已消费/已作废 → EmailVerifyError('invalid_or_expired')（统一，不泄露
      细分）；
    - 密码在**邮箱确认之后**由此调用设置（token 持有者本人在验证页设置）；
      策略与兑换同款（15..200、非全空白）；
    - J：login_id = 规范化邮箱（冲突进待补绑，见 _insert_pending_user_tx）；
    - 成功审计 ``registration.email_verified``（同事务；detail 无 token/
      密码/IP，email 只存掩码）；
    - 返回 ``{"user": ..., "login_id": ..., "email": ..., "pending_bind":
      bool}``。
    """
    tok = (token or "").strip()
    if not tok:
        raise EmailVerifyError("invalid_or_expired")
    if not isinstance(password, str) or not password.strip() \
            or len(password) < MIN_PASSWORD_LENGTH \
            or len(password) > MAX_PASSWORD_LENGTH:
        raise EmailVerifyError("bad_input")
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                cur.execute(
                    "SELECT job_id, email_normalized, status, consumed_at, "
                    "extract(epoch from expires_at)::float8 AS expires_at "
                    "FROM registration_mail_jobs "
                    "WHERE token_hash=%s AND purpose=%s FOR UPDATE",
                    (verify_token_hash(tok), MAIL_PURPOSE_EMAIL_VERIFY))
                job = cur.fetchone()
                # P1-1：token 消费接受 queued/sent/uncertain（uncertain=发送
                # 结果不确定，用户手里的链接可能真实有效，一律放行——防「收到
                # 的链接被判无效」；failed=确定未发出，仍拒绝）
                if job is None or job["consumed_at"] is not None \
                        or job["status"] not in ("queued", "sent",
                                                 "uncertain"):
                    raise EmailVerifyError("invalid_or_expired")
                if job["expires_at"] is not None \
                        and job["expires_at"] <= time.time():
                    raise EmailVerifyError("invalid_or_expired")
                email_norm = normalize_email(job["email_normalized"])
                # 同邮箱已有待激活/已激活账号 → 拒（唯一索引兜底；检查先于
                # 消费，token 保留给真正未注册的后来者——与 redeem 同策略）
                cur.execute(
                    "SELECT 1 FROM users WHERE lower(email_normalized)=%s "
                    "AND activation_state IN ('pending_activation','active') "
                    "LIMIT 1", (email_norm,))
                if cur.fetchone() is not None:
                    raise EmailVerifyError("email_taken")
                user = _insert_pending_user_tx(
                    cur, email_norm, email_norm,
                    password)  # email 列存规范化值（J：唯一用户名口径）
                pending_bind = normalize_email(user["login_id"]) != email_norm
                cur.execute(
                    "UPDATE registration_mail_jobs SET status='consumed', "
                    "consumed_at=now() WHERE job_id=%s AND "
                    "consumed_at IS NULL", (job["job_id"],))
                if (cur.rowcount or 0) != 1:
                    raise EmailVerifyError("invalid_or_expired")
                _insert_audit(
                    cur, "registration.email_verified", user["user_id"],
                    "user", user["user_id"],
                    {"email_masked": mask_login_id(email_norm),
                     "pending_bind": bool(pending_bind)})
    except psycopg.errors.UniqueViolation as exc:
        # 检查与插入之间的并发窗口（users_email_identity_key 兜底）
        name = getattr(getattr(exc, "diag", None), "constraint_name", "") or ""
        if "users_email_identity_key" in name or \
                "users_email_identity_key" in str(exc):
            raise EmailVerifyError("email_taken") from exc
        raise EmailVerifyError("invalid_or_expired") from exc
    finally:
        conn.close()
    return {"user": user, "login_id": user["login_id"], "email": email_norm,
            "pending_bind": bool(pending_bind)}


# --------------------------------------------------------------------------- #
# 已注册用户激活（单事务；绝不走 redeem_invite——那会插入第二个用户）
# --------------------------------------------------------------------------- #
def activate_registered_user(user_id, invite_token):
    """pending_activation 用户凭邀请码激活（**单个 PostgreSQL 事务**）。

    锁序（与 redeem_invite / create_user_with_total_allowance 同款三段式，
    保证建号/兑换/激活三方与 cutover 互斥串行、无死锁）：
      闸检查 → acquire_user_provisioning_lock_tx → 复查闸
      → SELECT users FOR UPDATE（锁 user 行、复查状态机）
      → SELECT registration_invites FOR UPDATE（锁 invite 行）
      → 绑定校验（P0-1：invite.login_id_normalized 非空时，与 pending 用户
        的**权威邮箱身份** users.email_normalized 做规范化 + 常数时间比较，
        缺省回退 login_id 的规范化形——绑定给 A 的邀请码绝不能激活 B）
      → CAS 消费邀请码（use_count+1 / consumed_at / consumed_by_user_id，
        WHERE consumed_at IS NULL AND revoked_at IS NULL——同码两人并发只有
        一人成功）
      → users.activation_state='active'、activation_source='invite'、
        ai_access=邀请模板值、auth_version+1（权限面变化推进凭据版本）
      → spend_store.create_user_total_allowance_tx（按邀请面值建一次性总额
        度；无面值解析全局默认，皆缺 fail-closed 整体回滚）
      → 审计 registration.activate（同事务）。

    失败语义：
      - 用户缺失/禁用/状态非 pending_activation → ActivationError
        （already_active 时**邀请码未读未消费**：先查状态后读 invite）；
      - 邀请码无效/过期/撤销/已消费/**绑定邮箱不匹配（bound_mismatch）** →
        InviteRedeemError（对外统一 ``invite_invalid_or_unavailable``，不泄露
        绑定差异；整体回滚：邀请码不消费、用户状态不变、不建额度；真实原因
        只进 best-effort 审计与日志；消费 CAS 未命中同样整体回滚）；
      - 维护闸开启 → spend_store.ProvisioningMaintenanceError 原样上抛。

    成功返回 ``{"user", "invite_id", "total_allowance"}``。
    """
    tok = (invite_token or "").strip()
    if not tok or not isinstance(user_id, str) or not user_id.strip():
        raise ActivationError("user_missing")
    token_hash = invite_token_hash(tok)
    fail_invite_id = None
    fail_reason = "unknown"
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                if spend_store.is_dispatch_maintenance_tx(cur):
                    raise spend_store.ProvisioningMaintenanceError(
                        "系统维护中（cutover），暂停激活；请稍后重试")
                spend_store.acquire_user_provisioning_lock_tx(cur)
                if spend_store.is_dispatch_maintenance_tx(cur):
                    raise spend_store.ProvisioningMaintenanceError(
                        "系统维护中（cutover），暂停激活；请稍后重试")
                # 1) 锁 user 行，先查状态机（already_active 不读 invite、
                #    不消费、不充值——review J/P2-4 红线）
                cur.execute(
                    "SELECT user_id, login_id, email_normalized, disabled, "
                    "activation_state, auth_version FROM users "
                    "WHERE user_id=%s FOR UPDATE", (user_id,))
                user = cur.fetchone()
                if user is None:
                    raise ActivationError("user_missing")
                if user["disabled"]:
                    raise ActivationError("user_disabled")
                if user["activation_state"] == "active":
                    raise ActivationError("already_active")
                if user["activation_state"] != "pending_activation":
                    raise ActivationError("not_pending")
                # 2) 锁 invite 行 + 状态检查（时间列统一 epoch float，与
                #    redeem_invite 同口径；P0-1：补读绑定列 login_id_normalized）
                cur.execute(
                    "SELECT invite_id, login_id_normalized, "
                    "extract(epoch from expires_at)::float8 AS expires_at, "
                    "max_uses, use_count, "
                    "extract(epoch from consumed_at)::float8 AS consumed_at, "
                    "extract(epoch from revoked_at)::float8 AS revoked_at, "
                    "ai_access, total_limit_nano_cny "
                    "FROM registration_invites "
                    "WHERE token_hash=%s FOR UPDATE", (token_hash,))
                invite = cur.fetchone()
                if invite is None:
                    fail_reason = "not_found"
                    raise InviteRedeemError("not_found")
                invite_id = fail_invite_id = invite["invite_id"]
                fail_reason = "status_changed"
                now = time.time()
                if invite["revoked_at"] is not None:
                    fail_reason = "revoked"
                    raise InviteRedeemError("revoked")
                if invite["expires_at"] is not None \
                        and invite["expires_at"] <= now:
                    fail_reason = "expired"
                    raise InviteRedeemError("expired")
                if invite["consumed_at"] is not None or \
                        int(invite["use_count"] or 0) >= \
                        int(invite["max_uses"] or 1):
                    fail_reason = "consumed"
                    raise InviteRedeemError("consumed")
                # 2.5) 绑定校验（P0-1，先于 CAS 消费）：invite 绑定了
                #    login_id_normalized（非空）时，与 pending 用户的权威
                #    邮箱身份做规范化 + 常数时间比较——email_normalized 缺省
                #    （理论上 pending 行必有，防御回退）时比较 login_id 的
                #    规范化形。不匹配 → 抛 InviteRedeemError 整体回滚：邀请码
                #    不消费、用户状态不变、不建额度；对外统一 403
                #    invite_invalid_or_unavailable（反枚举，不泄露绑定差异），
                #    真实原因 bound_mismatch 只进 best-effort 审计。
                bound = invite["login_id_normalized"]
                if bound:
                    user_email = normalize_email(
                        user["email_normalized"] or "") \
                        or normalize_email(user["login_id"])
                    if not _consttime_eq(user_email, str(bound)):
                        fail_reason = "bound_mismatch"
                        _log.warning(
                            "激活拒绝：邀请码绑定身份与用户不匹配"
                            "（invite=%s，细节仅审计不外泄）", invite_id)
                        raise InviteRedeemError("bound_mismatch")
                # 3) CAS 消费（同码并发只有一人命中）
                cur.execute(
                    "UPDATE registration_invites SET use_count=use_count+1, "
                    "consumed_at=now(), consumed_by_user_id=%s "
                    "WHERE invite_id=%s AND consumed_at IS NULL "
                    "AND revoked_at IS NULL",
                    (user_id, invite_id))
                if (cur.rowcount or 0) != 1:
                    fail_reason = "consumed"
                    raise InviteRedeemError("consumed")
                # 4) 状态机推进 active + 邀请模板 AI 权限 + 凭据版本推进
                cur.execute(
                    "UPDATE users SET activation_state='active', "
                    "activation_source='invite', activation_updated_at=now(),"
                    " ai_access=%s, auth_version=auth_version+1 "
                    "WHERE user_id=%s RETURNING user_id, login_id, "
                    "display_name, role, extract(epoch from created_at)::float8"
                    " AS created_at, disabled, ai_config, ai_access, "
                    "auth_version, activation_state, activation_source, "
                    "email, email_normalized",
                    (bool(invite["ai_access"]), user_id))
                updated = dict(cur.fetchone())
                # 5) 按邀请面值建一次性总额度（单轨；无面值解析全局默认；
                #    皆缺 fail-closed 整体回滚——invite 不消费、状态不推进）
                invite_limit = invite["total_limit_nano_cny"]
                try:
                    if invite_limit is not None:
                        limit, dver = int(invite_limit), None
                    else:
                        limit, _src, dver = \
                            spend_store._resolve_total_default_tx(
                                cur, datetime.now(timezone.utc))
                        if limit is None:
                            raise ValueError(
                                "total_default_missing: 无可用默认总额度"
                                "（ai_spend_total_defaults 缺行）")
                    allowance = spend_store.create_user_total_allowance_tx(
                        cur, user_id, limit, source="invite",
                        default_version=dver,
                        updated_by="invite:" + invite_id)
                except Exception:
                    _log.warning("激活建额度失败（整体回滚：invite 不消费、"
                                 "状态不推进）", exc_info=True)
                    raise
                _insert_audit(
                    cur, "registration.activate", user_id,
                    "registration_invite", invite_id,
                    {"status": "success",
                     "user_id": user_id,
                     "email_masked": mask_login_id(
                         user["email_normalized"] or "")})
        return {"user": updated, "invite_id": invite_id,
                "total_allowance": allowance}
    except InviteRedeemError as exc:
        # 失败审计（独立 best-effort 小事务；主事务已回滚，invite 必未消费）
        fail_reason = exc.reason if exc.reason != "bad_input" else fail_reason
        _audit_redeem_best_effort(fail_invite_id, "activate:" + fail_reason)
        raise
    finally:
        conn.close()
