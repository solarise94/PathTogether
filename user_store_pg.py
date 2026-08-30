# -*- coding: utf-8 -*-
"""用户存储层 —— PostgreSQL 后端实现（Stage 3b-2）。

逐函数对照 `user_store_json` 语义移植（公共名全实现）。调用方仍经
`user_store` dispatcher 访问（`STORAGE_BACKEND=postgres` 时 re-export 本模块），
app.py / share_server.py / tests 一行不改。

与 JSON 实现的差异（允许的实现差，在 docstring 声明）：
  - 数据落 PostgreSQL `users` 表，不再是 users.json 文件；
  - login_id 大小写不敏感唯一由 `lower(login_id)` 唯一索引保证（等价 json
    写入侧小写规范化 + 冲突即 ValueError）；
  - `created_at` 在库里是 TIMESTAMPTZ，读出统一转 epoch 浮点，保持与 json 版本
    dict 形状（浮点时间戳）完全一致；
  - 密码一律 werkzeug pbkdf2 哈希落库（与 json 一致，绝不存明文）；
  - ai_config 落 users.ai_config JSONB（api_key 已是 app.py 加密形态，原样存储）。

账户系统批次 A（docs/account-system-simplification-fix-plan.md §5.3）：
  - auth_version（0015 列）随所有读路径带出；密码/disable/enable 写路径同事务
    递增（docs §6.2）；
  - owner 引导改为 list_enabled_owners / list_owners / create_bootstrap_owner /
    resolve_primary_owner，删除 ensure_owner / first_owner 的「env 对账覆盖 +
    最早 owner」语义；
  - 密码统一 15..200 策略（PASSWORD_MIN_LENGTH / PASSWORD_MAX_LENGTH），
    无旁路参数。

账户系统批次 B（docs §6.1）：verify_user 只认规范化 login_id，删除
display_name 登录 fallback；get_user_by_display_name 删除。

账户系统批次 C（docs §4.2 物理收口）：
  - 物理列由 users.email 改名为 users.login_id（0016），函数唯一索引
    users_login_id_ci_key（lower(login_id)）；
  - 删除批次 B 兼容窗口 `_with_login_id`：返回用户 dict 只带 "login_id" 键，
    不再有 "email" 键；
  - get_user_by_email → get_user_by_login_id（公共 API 改名）；
  - 删除 user 侧 dual 镜像原语 _mirror_user 与启动修复
    repair_empty_password_hashes_from_json（修复迁至
    scripts/repair_pg_user_password_hashes.py，docs §9.2/§9.3）。

无文件锁 / SHARE_DATA_DIR 语义：PG 是唯一事实源。SHARE_DATA_DIR / USER_FILE 仍
暴露为 None 占位，仅供 dispatcher 公共名校验（hasattr）与形状兼容。
"""

import secrets
import time

import psycopg
from werkzeug.security import check_password_hash, generate_password_hash

import pg_store


class UserStoreCorrupt(Exception):
    """JSON 用户库损坏（PG 后端不会抛出；dispatcher 公共名对齐）。"""


class OwnerInvariantError(Exception):
    """owner 不变量被破坏（账户系统批次 A，docs §3.2/§5.3）。

    消息携带场景标识便于区分处理：
      - ``no_owner``：库中无任何启用 owner（resolve_primary_owner 拒绝解析）；
      - ``multiple_enabled_owners``：启用的 owner 多于一个（禁止选「第一条」）；
      - ``users_table_not_empty``：create_bootstrap_owner 只允许空库首建。
    """


class PasswordChangeConflict(Exception):
    """本人改密 CAS 冲突（P1 修复：消除「请求外验旧 hash + 无条件覆写」窗口）。

    ``reason`` 携带可区分的失败场景：
      - ``user_missing`` / ``user_disabled``：目标用户不存在或已被并发禁用；
      - ``auth_version_conflict``：库内 auth_version 与调用方 expected 不符
        （管理员重置 / break-glass / 另一端本人改密已先完成，绝不能覆盖）；
      - ``invalid_current_password``：current_password 与库内 hash 不匹配；
      - ``same_as_current``：新密码与当前密码相同。
    """

    def __init__(self, reason, message=None):
        self.reason = reason
        super().__init__(message or reason)


# --------------------------------------------------------------------------- #
# 统一密码策略（批次 A，docs §3.3；对齐 NIST SP 800-63B）：
# 15..200 字符，允许空格与长口令，不要求组合规则。所有密码写入口（create_user /
# set_user_password / create_bootstrap_owner）共用，无旁路参数。
# --------------------------------------------------------------------------- #
#: 密码最小长度（存量短 hash 仍可登录，仅新写入执行本策略）
PASSWORD_MIN_LENGTH = 15
#: 密码最大长度（与 UI 上限一致，防御异常大输入）
PASSWORD_MAX_LENGTH = 200


def _validate_password(password):
    """统一密码策略校验：str、非全空白且 15..200 字符；违规 raise ValueError。

    只做长度/类型/全空白校验（口令中段允许空格，长口令友好），不做组合
    规则；错误消息不含密码本身。所有密码写路径共用，无 _enforce_min_length
    一类旁路。全空白拒绝与 useradmin CLI 对齐（P2：Web 与 CLI 策略一致）。
    """
    if not isinstance(password, str) or not password:
        raise ValueError("密码不能为空")
    if not password.strip():
        raise ValueError("密码不能为全空白字符")
    if len(password) < PASSWORD_MIN_LENGTH or len(password) > PASSWORD_MAX_LENGTH:
        raise ValueError(
            "密码长度须在 %d..%d 字符之间（当前 %d 字符）"
            % (PASSWORD_MIN_LENGTH, PASSWORD_MAX_LENGTH, len(password))
        )
    return password


def _connect():
    """建连接并设 dict_row（本模块所有查询按列名访问，与 psycopg3 默认 tuple 区分）。"""
    conn = pg_store.connect()
    conn.row_factory = psycopg.rows.dict_row
    return conn

# 角色常量（与 user_store_json 一致；guest 仅声明不入表）
ROLE_OWNER = "owner"
ROLE_USER = "user"
ROLE_GUEST = "guest"
ROLE_SDK = "sdk"
VALID_ROLES = (ROLE_OWNER, ROLE_USER)

# 文件路径占位：PG 后端不用文件（dispatcher 公共名校验需要这些名字存在）
# 文件路径占位：PG 后端不用文件。为兼容测试里 `USER_FILE.exists()` 判空调用，指向
# 一个必然不存在的路径（exists()=False；unlink 有 `if exists()` 保护不会触发）。
from pathlib import Path as _Path  # noqa: E402
SHARE_DATA_DIR = None
USER_FILE = _Path("/svs-pg-backend/no-user-file")

# AI 凭据默认（与 json 一致：use_platform 缺省 True；max_steps 默认 20，docs §9.2）
#: user 自带 API 步数默认值（与 budget_store.DEFAULT_PLATFORM_TASK_MAX_STEPS 一致）
DEFAULT_USER_MAX_STEPS = 20
_DEFAULT_USER_AI_CONFIG = {
    "use_platform": True, "base_url": "", "model": "", "api_key": "",
    "max_steps": DEFAULT_USER_MAX_STEPS,
}


def _user_max_steps(raw) -> int:
    """规范化 max_steps：非法/缺失回默认 20（防御读取旧数据）。"""
    try:
        v = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_USER_MAX_STEPS
    return v if v > 0 else DEFAULT_USER_MAX_STEPS


def _user_id() -> str:
    return "usr_" + secrets.token_urlsafe(8)


def _normalize_login_id(login_id: str) -> str:
    """登录账号（login_id）规范化：小写 + 去首尾空白（与 json 一致）。

    物理列 0016 起为 users.login_id；规范化即 login_id 的唯一键规范化。
    """
    return str(login_id or "").strip().lower()


def _public(user: dict) -> dict:
    """导出副本（不含 password_hash），与 json `_to_public` 对齐。

    批次 C 起返回 dict 只带 "login_id"（SQL 列名即 login_id），不再补
    "email" 兼容键（docs §4.2 物理收口）。
    """
    out = dict(user)
    out.pop("password_hash", None)
    return out


def _user_ai_config(user: dict) -> dict:
    """返回用户行内 ai_config（规范化后副本，缺省 use_platform=True）。"""
    raw = user.get("ai_config") or {}
    if not isinstance(raw, dict):
        raw = {}
    out = dict(_DEFAULT_USER_AI_CONFIG)
    out.update({k: raw.get(k) for k in ("base_url", "model", "api_key")})
    out["use_platform"] = bool(raw.get("use_platform", True))
    out["max_steps"] = _user_max_steps(raw.get("max_steps"))
    return out


# 公共 SELECT 列（created_at 转 epoch 浮点与 json 形状对齐；ai_access 为 P0-B
# §3.7 新增列：受邀用户默认 FALSE，存量默认 TRUE，见 0012 迁移；auth_version 为
# 0015 新增的 session 凭据版本列，读路径一律带出供 session 比对）。
_SEL_HASH = (
    "user_id, login_id, display_name, password_hash, role, "
    "extract(epoch from created_at)::float8 AS created_at, disabled, ai_config, "
    "ai_access, auth_version"
)
_SEL_PUBLIC = (
    "user_id, login_id, display_name, role, "
    "extract(epoch from created_at)::float8 AS created_at, disabled, ai_config, "
    "ai_access, auth_version"
)


def create_user(login_id, password, role=ROLE_USER, display_name=None):
    """创建用户。返回新用户 dict（不含 hash）；登录账号冲突抛 ValueError。

    参数 ``login_id`` 为**登录账号**（docs §3.1）：可为用户名或邮箱形式，
    写入前 trim + lower 规范化。返回 dict 只带 "login_id" 键（批次 C 起不再
    携带 deprecated 的 "email" 别名，docs §4.2）。

    密码统一执行 15..200 策略（无旁路参数；批次 A docs §3.3）。
    在 PG 上创建第二个 enabled owner 会被 users_single_enabled_owner_key
    部分唯一索引（0015）拦下，同样抛 ValueError。
    """
    if role not in VALID_ROLES:
        raise ValueError("非法角色")
    norm_login = _normalize_login_id(login_id)
    if not norm_login:
        raise ValueError("登录账号不能为空")
    _validate_password(password)
    name = str(display_name or "").strip() or norm_login
    uid = _user_id()
    now = time.time()

    conn = _connect()
    try:
        try:
            with pg_store.transaction(conn) as c:
                with c.cursor() as cur:
                    row = _insert_user_tx(
                        cur, uid, norm_login, name, password, role, now)
        except psycopg.errors.UniqueViolation as exc:
            raise ValueError(_unique_violation_message(exc)) from exc
        return _public(row) if row else None
    finally:
        conn.close()


def _insert_user_tx(cur, uid, norm_login, name, password, role, now,
                    ai_access=True):
    """在调用方事务的 cursor 内插入用户行，返回公共列 dict（不提交）。

    供 :func:`create_user` 与 :func:`create_user_with_spend_override` 共用
    （后者要求 user 行与 override 策略/audit 同一事务）。ai_access 缺省
    True 与 users.ai_access 列默认一致（保持 create_user 既有行为；邀请
    兑换路径经 registration_store 的显式模板值，不经本函数）。
    """
    cur.execute(
        "INSERT INTO users "
        "(user_id, login_id, display_name, password_hash, "
        " role, created_at, disabled, ai_access) "
        "VALUES (%s,%s,%s,%s,%s, to_timestamp(%s), FALSE, %s) "
        "RETURNING " + _SEL_PUBLIC,
        (uid, norm_login, name, generate_password_hash(password), role,
         now, bool(ai_access)),
    )
    return cur.fetchone()


def create_user_with_spend_override(login_id, password, display_name=None,
                                    ai_access=True,
                                    monthly_limit_nano_cny=None,
                                    actor_user_id=None):
    """**单个 PostgreSQL 事务**内：创建 role=user 用户 + 可选月额度覆盖 + audit。

    批次 D（docs ai-money-budget-bugfix-and-simplification-plan.md §5.1）：
    owner 经 admin v1 建号时可选 ``monthly_limit_nano_cny``（nano-CNY 整数；
    None = 继承全局 user_default，不建 override 行）。user 插入、override
    策略（spend_store.set_user_override_tx）与 ``user.create`` 审计
    （share_store_pg.record_audit_tx，不吞错）全部同一事务，任一失败整体
    回滚（用户不存在半创建状态）。

    本入口只创建普通用户（role=user；owner 建号走主机侧 break-glass，docs
    §3.2 不变量 5）。登录账号冲突抛 ValueError（与 create_user 同文案）。
    返回 ``(user_dict, override_policy_dict_or_None)``。
    """
    norm_login = _normalize_login_id(login_id)
    if not norm_login:
        raise ValueError("登录账号不能为空")
    _validate_password(password)
    name = str(display_name or "").strip() or norm_login
    uid = _user_id()
    now = time.time()

    import share_store_pg
    import spend_store
    conn = _connect()
    try:
        try:
            with pg_store.transaction(conn) as c:
                with c.cursor() as cur:
                    row = _insert_user_tx(
                        cur, uid, norm_login, name, password, ROLE_USER, now,
                        ai_access=ai_access)
                    override = None
                    if monthly_limit_nano_cny is not None:
                        override = spend_store.set_user_override_tx(
                            cur, uid, monthly_limit_nano_cny,
                            updated_by=actor_user_id or "admin",
                            actor_user_id=actor_user_id)
                    # 审计 detail 只含非敏感字段（§9.6：无密码/token/IP）
                    share_store_pg.record_audit_tx(
                        cur, "user.create", actor_user_id=actor_user_id,
                        actor_role="owner", target_type="user",
                        target_id=uid,
                        detail={
                            "role": ROLE_USER,
                            "ai_access": bool(ai_access),
                            "monthly_limit_nano_cny":
                                None if override is None
                                else int(override["limit_nano_cny"]),
                        })
        except psycopg.errors.UniqueViolation as exc:
            raise ValueError(_unique_violation_message(exc)) from exc
        return _public(row), override
    finally:
        conn.close()


def _unique_violation_message(exc):
    """把 users 表 UniqueViolation 映射成可区分的中文 ValueError 消息。"""
    name = getattr(getattr(exc, "diag", None), "constraint_name", "") or ""
    text = str(exc)
    if "users_single_enabled_owner_key" in name or \
            "users_single_enabled_owner_key" in text:
        return "已存在启用的 owner：单 enabled owner 不变量（0015 索引）禁止再建"
    return "该登录账号已存在"


def get_user(user_id):
    """按 user_id 取用户 dict（含 hash）；不存在返回 None。

    批次 C 起返回 dict 只带 "login_id" 键（docs §4.2 物理收口）。
    """
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                cur.execute(
                    "SELECT " + _SEL_HASH + " FROM users WHERE user_id=%s",
                    (user_id,),
                )
                row = cur.fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def get_user_by_login_id(login_id):
    """按登录账号（login_id，物理列 login_id；trim+lower 规范化）取用户 dict
    （含 hash）；不存在返回 None。

    认证路径唯一入口（docs §6.1）：按 lower(login_id) 唯一索引查
    （原批次 B 名 get_user_by_email，批次 C 随物理列改名）。
    """
    key = _normalize_login_id(login_id)
    if not key:
        return None
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                cur.execute(
                    "SELECT " + _SEL_HASH +
                    " FROM users WHERE lower(login_id)=lower(%s)",
                    (key,),
                )
                row = cur.fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def verify_user(login_id, password):
    """校验登录：只认规范化 login_id，按唯一索引查用户并核对密码（docs §6.1）。

    流程：normalize(trim + lower) → 按 lower(login_id) 唯一索引查 → disabled
    检查 → check_password_hash。display_name 不参与登录解析（批次 B 删除
    fallback：展示属性可重复，不得用作身份属性）。

    返回用户 dict（含 hash）；查无此人 / 被禁用 / 密码错误一律返回 None——
    调用方统一「账号或密码错误」文案，不泄露账号存在性。
    """
    if not isinstance(password, str) or not password:
        return None
    user = get_user_by_login_id(login_id)
    if user is None:
        return None
    if user.get("disabled"):
        return None
    try:
        if not check_password_hash(user.get("password_hash") or "", password):
            return None
    except Exception:
        return None
    return user


def list_users():
    """返回全部用户（不含 hash），按 created_at 升序。"""
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                cur.execute(
                    "SELECT " + _SEL_PUBLIC +
                    " FROM users ORDER BY extract(epoch from created_at) ASC, user_id"
                )
                rows = cur.fetchall()
        return [_public(r) for r in rows]
    finally:
        conn.close()


def set_user_disabled(user_id, flag):
    """设置用户禁用状态（True=禁用）。返回更新后的用户 dict；不存在返回 None。

    disable 与 enable 两个方向都在同一 UPDATE 事务内递增 auth_version
    （批次 A docs §6.2：enable 也递增，防止禁用期间未发请求的旧 Cookie
    在重新启用后被激活）。
    """
    flag = bool(flag)
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                cur.execute(
                    "UPDATE users SET disabled=%s, auth_version=auth_version+1 "
                    "WHERE user_id=%s RETURNING " + _SEL_PUBLIC,
                    (flag, user_id),
                )
                row = cur.fetchone()
        return _public(row) if row else None
    finally:
        conn.close()


def set_user_password(user_id, new_password):
    """重置用户密码。返回更新后的用户 dict；不存在返回 None。

    密码统一执行 15..200 策略（无旁路参数）；hash 更新与 auth_version+1
    在同一事务内（批次 A docs §6.2，旧 session 凭据版本随之失效）。

    注意：本函数**不做**当前密码/版本校验，仅供管理员重置与 break-glass
    等权威路径；本人改密一律走 change_own_password（CAS，P1 修复）。
    """
    _validate_password(new_password)
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                cur.execute(
                    "UPDATE users SET password_hash=%s, "
                    "auth_version=auth_version+1 "
                    "WHERE user_id=%s RETURNING " + _SEL_PUBLIC,
                    (generate_password_hash(new_password), user_id),
                )
                row = cur.fetchone()
        return _public(row) if row else None
    finally:
        conn.close()


def change_own_password(user_id, current_password, new_password,
                        expected_auth_version):
    """本人改密 CAS 原语：锁行 + 验当前密码/版本 + 更新 hash 与版本，同一事务。

    「验证 current_password（对请求开始时的旧 hash）」与「更新密码」之间存
    在并发窗口：管理员重置 / break-glass / 另一端改密若先提交，无条件
    UPDATE 会覆盖新密码（P1 TOCTOU）。本原语在单事务内完成：

      1. ``SELECT ... FOR UPDATE`` 锁定目标行（并发写在此串行化）；
      2. 校验用户存在且未禁用；
      3. 校验库内 ``auth_version == expected_auth_version``（调用方传入其
         读到的版本；不符 → 说明行已被其它写路径推进，拒绝覆盖）；
      4. 校验 ``current_password`` 对**当前行内** hash；
      5. 统一密码策略 + 新密码不得与当前密码相同；
      6. 更新 hash 并 ``auth_version+1``。

    成功返回更新后的用户 dict（不含 hash）；失败 raise
    ``PasswordChangeConflict``（reason 见该类 docstring；不写库、不计 audit
    ——失败计数与文案由调用方分层处理）或 ``ValueError``（新密码策略违规，
    文案与 _validate_password 一致）。
    """
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                cur.execute(
                    "SELECT " + _SEL_HASH + " FROM users "
                    "WHERE user_id=%s FOR UPDATE",
                    (user_id,),
                )
                row = cur.fetchone()
                if row is None:
                    raise PasswordChangeConflict("user_missing")
                if row.get("disabled"):
                    raise PasswordChangeConflict("user_disabled")
                if int(row.get("auth_version") or 0) != int(
                        expected_auth_version or 0):
                    raise PasswordChangeConflict(
                        "auth_version_conflict",
                        "auth_version_conflict：库内版本已被其它写路径推进"
                        "（expected=%s, actual=%s），拒绝覆盖"
                        % (expected_auth_version, row.get("auth_version")))
                try:
                    current_ok = check_password_hash(
                        row.get("password_hash") or "", current_password or "")
                except Exception:
                    current_ok = False
                if not current_ok:
                    raise PasswordChangeConflict("invalid_current_password")
                _validate_password(new_password)
                if check_password_hash(row.get("password_hash") or "",
                                       new_password):
                    raise PasswordChangeConflict("same_as_current")
                cur.execute(
                    "UPDATE users SET password_hash=%s, "
                    "auth_version=auth_version+1 "
                    "WHERE user_id=%s RETURNING " + _SEL_PUBLIC,
                    (generate_password_hash(new_password), user_id),
                )
                updated = cur.fetchone()
        return _public(updated) if updated else None
    finally:
        conn.close()


def get_user_ai_config(user_id):
    """按 user_id 取用户 AI 凭据 dict（api_key 为磁盘原样，可能 enc: 密文）。

    不存在用户返回 None；用户未配置过返回规范化默认（use_platform=True 空凭据）。
    """
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                cur.execute(
                    "SELECT " + _SEL_HASH + " FROM users WHERE user_id=%s",
                    (user_id,),
                )
                row = cur.fetchone()
        if row is None:
            return None
        return _user_ai_config(row)
    finally:
        conn.close()


def set_user_ai_config(user_id, cfg):
    """设置用户 AI 凭据（use_platform/base_url/model/api_key/max_steps）。返回更新
    后的公共用户 dict；用户不存在返回 None。"""
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                cur.execute(
                    "SELECT " + _SEL_HASH + " FROM users WHERE user_id=%s",
                    (user_id,),
                )
                row = cur.fetchone()
                if row is None:
                    return None
                merged = _user_ai_config(row)
                if isinstance(cfg, dict):
                    for k in ("use_platform", "base_url", "model", "api_key"):
                        if k in cfg:
                            merged[k] = cfg[k]
                    if "max_steps" in cfg:
                        merged["max_steps"] = _user_max_steps(cfg.get("max_steps"))
                cur.execute(
                    "UPDATE users SET ai_config=%s WHERE user_id=%s "
                    "RETURNING " + _SEL_PUBLIC,
                    (psycopg.types.json.Jsonb(merged), user_id),
                )
                row2 = cur.fetchone()
        return _public(row2) if row2 else None
    finally:
        conn.close()


def set_user_ai_access(user_id, enabled):
    """授予/收回平台 AI 访问（P0-B docs §3.7；PG-only，不经 dispatcher）。

    返回更新后的公共用户 dict；不存在返回 None。受邀用户建号时 ai_access 由
    邀请码模板决定（registration_store，默认 False），owner 用本函数显式授予。
    """
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                cur.execute(
                    "UPDATE users SET ai_access=%s WHERE user_id=%s "
                    "RETURNING " + _SEL_PUBLIC,
                    (bool(enabled), user_id),
                )
                row = cur.fetchone()
        return _public(row) if row else None
    finally:
        conn.close()


def count_owners():
    """返回 role=owner 且未禁用的用户数量。"""
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                cur.execute(
                    "SELECT count(*) FROM users WHERE role='owner' AND NOT disabled"
                )
                return int(cur.fetchone()["count"])
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# owner 解析与引导（账户系统批次 A，docs §3.2/§5.3）
#
# 删除含糊的 ensure_owner()/first_owner()（「最早创建的 owner」+ env 密码对账
# 覆盖），改为显式的三个原语：list_enabled_owners / create_bootstrap_owner /
# resolve_primary_owner。数据库层由 0015 的部分唯一索引
# users_single_enabled_owner_key 兜底「最多一个 enabled owner」。
# --------------------------------------------------------------------------- #
# create_bootstrap_owner 专用 advisory lock key（"SVOW" 的 4 字节整数）。
# 独立于 app.py schema 初始化的 0x53565347（"SVSG"）：两个启动阶段互不串行
# 耦合（docs §5.3 明确要求不复用 schema 锁）。
_BOOTSTRAP_OWNER_LOCK = 0x53564F57


def list_enabled_owners():
    """返回全部启用 owner（role='owner' AND NOT disabled）的 dict 列表。

    按 created_at, user_id 升序；每项含 password_hash 与 auth_version
    （owner 解析需要校验 hash 非空，属内部 API，不经公共用户输出）。
    """
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                cur.execute(
                    "SELECT " + _SEL_HASH +
                    " FROM users WHERE role='owner' AND NOT disabled "
                    "ORDER BY extract(epoch from created_at) ASC, user_id"
                )
                rows = cur.fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def list_owners():
    """返回全部 owner 行（含 disabled），排序与字段同 list_enabled_owners。"""
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                cur.execute(
                    "SELECT " + _SEL_HASH +
                    " FROM users WHERE role='owner' "
                    "ORDER BY extract(epoch from created_at) ASC, user_id"
                )
                rows = cur.fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def create_bootstrap_owner(login_id, password):
    """空库首建 owner（仅引导用）。返回新 owner dict（含 hash 与 auth_version）。

    语义（docs §5.2/§5.3）：
      - 仅当 users 表**完全为空**时创建；任何已存在行 → OwnerInvariantError
        （users_table_not_empty）；
      - 事务内先取专用 advisory lock（_BOOTSTRAP_OWNER_LOCK）串行化 gunicorn
        多 worker 并发首启，锁内复查空表再插入；0015 部分唯一索引作数据库层
        兜底（正常路径不会走到）；
      - login_id 列写规范化值（trim + lower），display_name 同值，
        role='owner'；密码统一执行 15..200 策略；
      - 不对已有 owner 做任何对账/改密（那是已删除的 ensure_owner 语义）。
    """
    norm_login = _normalize_login_id(login_id)
    if not norm_login:
        raise ValueError("登录账号不能为空")
    _validate_password(password)
    uid = _user_id()
    now = time.time()
    conn = _connect()
    try:
        try:
            with pg_store.transaction(conn) as c:
                with c.cursor() as cur:
                    # xact 级锁：commit/rollback 自动释放，与检查+插入同事务
                    cur.execute(
                        "SELECT pg_advisory_xact_lock(%s)",
                        (_BOOTSTRAP_OWNER_LOCK,),
                    )
                    cur.execute("SELECT count(*) AS n FROM users")
                    existing = int(cur.fetchone()["n"])
                    if existing:
                        raise OwnerInvariantError(
                            "users_table_not_empty：users 表已有 %d 行，"
                            "create_bootstrap_owner 仅允许在完全空库时创建"
                            "首个 owner（已有 owner 时任何 bootstrap 环境变量"
                            "不得改动账号）" % existing
                        )
                    cur.execute(
                        "INSERT INTO users "
                        "(user_id, login_id, display_name, password_hash, "
                        " role, created_at, disabled) "
                        "VALUES (%s,%s,%s,%s,%s, to_timestamp(%s), FALSE) "
                        "RETURNING " + _SEL_HASH,
                        (uid, norm_login, norm_login,
                         generate_password_hash(password), ROLE_OWNER, now),
                    )
                    row = cur.fetchone()
        except psycopg.errors.UniqueViolation as exc:
            # 兜底路径：并发建号未被 advisory lock 挡住时由 0015 索引拦下
            raise OwnerInvariantError(
                "multiple_enabled_owners：并发创建 owner 被单 enabled owner "
                "索引（users_single_enabled_owner_key）拒绝"
            ) from exc
        return dict(row)
    finally:
        conn.close()


def resolve_primary_owner():
    """解析唯一的 primary owner（只读，永不写库、永不接受密码参数）。

    恰好 1 个启用 owner → 返回该 dict（含 hash 与 auth_version）；
    0 个 → OwnerInvariantError（no_owner）；>1 个 → OwnerInvariantError
    （multiple_enabled_owners，禁止选「第一条」）。
    """
    owners = list_enabled_owners()
    if not owners:
        raise OwnerInvariantError(
            "no_owner：库中不存在任何启用的 owner（role='owner' 且未禁用），"
            "无法解析 primary owner"
        )
    if len(owners) > 1:
        raise OwnerInvariantError(
            "multiple_enabled_owners：存在 %d 个启用的 owner，违反单 "
            "enabled owner 不变量；须人工审计后只保留一个" % len(owners)
        )
    return owners[0]


def has_enabled_users():
    """是否存在任一 enabled（未禁用）用户（任意角色）。"""
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                cur.execute("SELECT EXISTS(SELECT 1 FROM users WHERE NOT disabled)")
                return bool(cur.fetchone()["exists"])
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# user 侧 dual 后端镜像原语（_mirror_user）与启动修复
# repair_empty_password_hashes_from_json 已随账户系统批次 C 删除
# （docs §9.2/§9.3）：user_store dispatcher 不再安装 dual；历史空 hash
# 修复迁至 scripts/repair_pg_user_password_hashes.py（默认 dry-run 的
# 主机侧一次性命令）。share 侧 dual 机制不受影响（share_store.py）。
# --------------------------------------------------------------------------- #
