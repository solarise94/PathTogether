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
import secrets
import time

import psycopg
from werkzeug.security import generate_password_hash

import pg_store
import platform_features
import user_store

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

REGISTRATION_MODES = ("closed", "invite_only", "public")


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
    """
    uid = _new_user_id()
    name = str(display_name or "").strip() or login_id_normalized
    cur.execute(
        "INSERT INTO users "
        "(user_id, login_id, display_name, password_hash, role, created_at, "
        " disabled, ai_config, ai_access) "
        "VALUES (%s,%s,%s,%s,'user', now(), FALSE, '{}'::jsonb, %s) "
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
    "ai_access, cohort, note"
)


def _invite_out(row) -> dict:
    out = dict(row)
    out["ai_access"] = bool(out.get("ai_access"))
    return out


def create_invite(created_by_user_id, login_id=None,
                  ttl_seconds=DEFAULT_INVITE_TTL_SECONDS,
                  ai_access=False, cohort="", note=""):
    """创建一次性邀请码。返回 dict：含**明文 token**（唯一出现处）与行信息。

    - token：``secrets.token_urlsafe(32)``；库内只存 invite_token_hash(token)；
    - login_id 给出时按 normalize_login_id 绑定——语义为「允许兑换的登录账号
      login_id」（docs §8.2；原批次 B 参数名 email，批次 C 收口改名）；
      None = 不绑定（owner 明确选择的高风险选项，UI 需标注）；
    - ai_access/cohort/note 为邀请模板：兑换时决定新用户平台 AI 权限与分组。
    """
    platform_features.require_pg_backend("registration_invites")
    if not isinstance(created_by_user_id, str) or not created_by_user_id:
        raise ValueError("created_by_user_id 不能为空")
    bound = normalize_login_id(login_id) if login_id else ""
    if login_id and not bound:
        raise ValueError("绑定登录账号不能为空白")
    try:
        ttl = int(ttl_seconds)
    except (TypeError, ValueError):
        raise ValueError("ttl_seconds 需为整数")
    if ttl <= 0:
        raise ValueError("ttl_seconds 需为正整数")
    cohort = str(cohort or "").strip()[:64]
    note = str(note or "").strip()[:200]

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
                        " max_uses, use_count, ai_access, cohort, note) "
                        "VALUES (%s,%s,%s,%s, now(), "
                        " now() + (%s * interval '1 second'), 1, 0, %s, %s, %s) "
                        "RETURNING " + _INVITE_SEL,
                        (invite_id, token_hash, bound or None,
                         created_by_user_id, ttl, bool(ai_access), cohort,
                         note),
                    )
                    row = cur.fetchone()
                    _insert_audit(
                        cur, "registration.invite_create", created_by_user_id,
                        "registration_invite", invite_id,
                        {"email_bound": bool(bound), "ai_access":
                         bool(ai_access), "cohort": cohort,
                         "ttl_seconds": ttl})
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
    platform_features.require_pg_backend("registration_invites")
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
    platform_features.require_pg_backend("registration_invites")
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
    platform_features.require_pg_backend("registration_invites")
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

    失败一律抛 ``InviteRedeemError``（对外统一 code
    ``invite_invalid_or_unavailable``）：
      - 无效/随机 token、过期、撤销、已消费（细分 reason：not_found / expired /
        revoked / consumed）；
      - 绑定登录账号不匹配（email_mismatch——稳定 reason 标识符，批次 C 维持
        不变；常数时间比较规范化值）；
      - users 登录账号已存在（email_taken；此时 invite 未消费——检查先于
        UPDATE）；
    成功返回 ``{"user": <新用户公共 dict>, "invite_id": ..., "login_id": ...}``
    （login_id 键即规范化登录账号；批次 C 起不再返回 "email" 键）。
    成功审计在同一事务内（registration.redeem，actor=被创建 user_id）；失败审计
    在独立 best-effort 事务（主事务已随异常回滚），detail 只含 invite_id/status。
    """
    platform_features.require_pg_backend("registration_invites")
    tok = (token or "").strip()
    norm_login = normalize_login_id(login_id)
    if not tok or not isinstance(password, str) or not norm_login \
            or len(password) < MIN_PASSWORD_LENGTH \
            or len(password) > MAX_PASSWORD_LENGTH:
        # 输入形状问题也按统一错误处理（路由层已做过表单校验，这里是防御层）
        raise InviteRedeemError("bad_input")
    token_hash = invite_token_hash(tok)
    fail_invite_id = None

    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                cur.execute(
                    "SELECT invite_id, token_hash, login_id_normalized, "
                    "extract(epoch from expires_at)::float8 AS expires_at, "
                    "max_uses, use_count, consumed_at, revoked_at, ai_access, "
                    "cohort FROM registration_invites WHERE token_hash=%s "
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
                    # 常数时间比较（规范化后等长补齐，长度差不泄露信息）
                    a = norm_login.encode("utf-8")
                    b = str(bound).encode("utf-8")
                    n = max(len(a), len(b), 1)
                    if not hmac.compare_digest(a + b"\0" * (n - len(a)),
                                               b + b"\0" * (n - len(b))):
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
                _audit_redeem(cur, invite_id, "success",
                              created_user_id=user["user_id"])
        return {"user": user, "invite_id": invite_id, "login_id": norm_login}
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
