# -*- coding: utf-8 -*-
"""用户存储层 —— PostgreSQL 后端实现（Stage 3b-2）。

逐函数对照 `user_store_json` 语义移植（21 个公共名全实现）。调用方仍经
`user_store` dispatcher 访问（`STORAGE_BACKEND=postgres` 时 re-export 本模块），
app.py / share_server.py / tests 一行不改。

与 JSON 实现的差异（允许的实现差，在 docstring 声明）：
  - 数据落 PostgreSQL `users` 表，不再是 users.json 文件；
  - email 大小写不敏感唯一由 `lower(email)` 唯一索引保证（等价 json 写入侧小写
    规范化 + 冲突即 ValueError）；
  - `created_at` 在库里是 TIMESTAMPTZ，读出统一转 epoch 浮点，保持与 json 版本
    dict 形状（浮点时间戳）完全一致；
  - 密码一律 werkzeug pbkdf2 哈希落库（与 json 一致，绝不存明文）；
  - ai_config 落 users.ai_config JSONB（api_key 已是 app.py 加密形态，原样存储）。

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


def _normalize_email(email: str) -> str:
    """email 唯一键规范化：小写 + 去首尾空白（与 json 一致）。"""
    return str(email or "").strip().lower()


def _public(user: dict) -> dict:
    """导出副本（不含 password_hash），与 json `_to_public` 对齐。"""
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
# §3.7 新增列：受邀用户默认 FALSE，存量默认 TRUE，见 0012 迁移）。
_SEL_HASH = (
    "user_id, email, display_name, password_hash, role, "
    "extract(epoch from created_at)::float8 AS created_at, disabled, ai_config, "
    "ai_access"
)
_SEL_PUBLIC = (
    "user_id, email, display_name, role, "
    "extract(epoch from created_at)::float8 AS created_at, disabled, ai_config, "
    "ai_access"
)


def create_user(email, password, role=ROLE_USER, display_name=None,
                _enforce_min_length=True):
    """创建用户。返回新用户 dict（不含 hash）；email 冲突抛 ValueError。"""
    if role not in VALID_ROLES:
        raise ValueError("非法角色")
    norm_email = _normalize_email(email)
    if not norm_email:
        raise ValueError("邮箱/用户名不能为空")
    if not isinstance(password, str) or not password:
        raise ValueError("密码不能为空")
    if _enforce_min_length and isinstance(password, str) and len(password) < 8:
        raise ValueError("密码长度至少 8 位")
    name = str(display_name or "").strip() or norm_email
    uid = _user_id()
    now = time.time()

    conn = _connect()
    try:
        try:
            with pg_store.transaction(conn) as c:
                with c.cursor() as cur:
                    sql = (
                        "INSERT INTO users "
                        "(user_id, email, display_name, password_hash, role, "
                        " created_at, disabled) "
                        "VALUES (%s,%s,%s,%s,%s, to_timestamp(%s), FALSE) "
                        "RETURNING " + _SEL_PUBLIC
                    )
                    cur.execute(
                        sql,
                        (uid, norm_email, name, generate_password_hash(password),
                         role, now),
                    )
                    row = cur.fetchone()
        except psycopg.errors.UniqueViolation:
            raise ValueError("该邮箱/用户名已存在")
        return _public(row) if row else None
    finally:
        conn.close()


def get_user(user_id):
    """按 user_id 取用户 dict（含 hash）；不存在返回 None。"""
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


def get_user_by_email(email):
    """按 email（小写规范化）取用户 dict（含 hash）；不存在返回 None。"""
    key = _normalize_email(email)
    if not key:
        return None
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                cur.execute(
                    "SELECT " + _SEL_HASH + " FROM users WHERE lower(email)=lower(%s)",
                    (key,),
                )
                row = cur.fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def get_user_by_display_name(display_name):
    """按 display_name 精确匹配取用户 dict（含 hash）；不存在返回 None。"""
    key = str(display_name or "").strip()
    if not key:
        return None
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                cur.execute(
                    "SELECT " + _SEL_HASH + " FROM users WHERE display_name=%s",
                    (key,),
                )
                row = cur.fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def verify_user(email_or_name, password):
    """校验登录：按 email 或 display_name 查找用户并核对密码。

    返回用户 dict（含 hash）；查无此人 / 被禁用 / 密码错误返回 None。
    """
    if not isinstance(password, str) or not password:
        return None
    user = get_user_by_email(email_or_name)
    if user is None:
        user = get_user_by_display_name(email_or_name)
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
    """设置用户禁用状态（True=禁用）。返回更新后的用户 dict；不存在返回 None。"""
    flag = bool(flag)
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                cur.execute(
                    "UPDATE users SET disabled=%s WHERE user_id=%s "
                    "RETURNING " + _SEL_PUBLIC,
                    (flag, user_id),
                )
                row = cur.fetchone()
        return _public(row) if row else None
    finally:
        conn.close()


def set_user_password(user_id, new_password, _enforce_min_length=True):
    """重置用户密码。返回更新后的用户 dict；不存在返回 None。"""
    if not isinstance(new_password, str) or not new_password:
        raise ValueError("密码不能为空")
    if _enforce_min_length and len(new_password) < 8:
        raise ValueError("密码长度至少 8 位")
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                cur.execute(
                    "UPDATE users SET password_hash=%s WHERE user_id=%s "
                    "RETURNING " + _SEL_PUBLIC,
                    (generate_password_hash(new_password), user_id),
                )
                row = cur.fetchone()
        return _public(row) if row else None
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


def first_owner():
    """返回第一个 owner 用户 dict（含 hash）；无则 None。"""
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                cur.execute(
                    "SELECT " + _SEL_HASH +
                    " FROM users WHERE role='owner' "
                    "ORDER BY extract(epoch from created_at) ASC, user_id LIMIT 1"
                )
                row = cur.fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


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


def ensure_owner(email, password):
    """owner 引导与迁移：按 ADMIN_USERNAME/ADMIN_PASSWORD 维护 owner 账户。

    语义与 json 完全一致：无 owner → 创建；已有 owner 且 ADMIN_PASSWORD 不匹配 →
    重置该 owner 密码。返回 owner 用户 dict（含 hash）。
    """
    norm_email = _normalize_email(email)
    name = norm_email or "owner"
    owner = first_owner()
    if owner is None:
        return create_user(name, password, role=ROLE_OWNER, display_name=name,
                           _enforce_min_length=False)
    try:
        match = check_password_hash(owner.get("password_hash") or "", password)
    except Exception:
        match = False
    if not match:
        set_user_password(owner["user_id"], password, _enforce_min_length=False)
        owner = get_user(owner["user_id"])
    return owner


# --------------------------------------------------------------------------- #
# dual 后端 result-replay 镜像（Stage 3b-2）
#
# json 为权威、pg 为影子副本。create_user/ensure_owner 在 json 侧内部生成 user_id，
# 同参调 pg 会让 pg 生成不同 user_id → 两库发散。这里提供 _mirror_user：接收 json
# 返回的权威用户 dict，按其中 user_id 原样 upsert 进 pg（身份逐项一致）。
# 走 force_user_id 方案而非直接给 pg create_user 重放，是因为 email 冲突/密码长度
# 校验在 json 侧已完成，pg 只需按权威结果落影子行。
# --------------------------------------------------------------------------- #
def _mirror_user(ret, *a, **k):
    """把 json create_user/ensure_owner 返回的权威用户 dict upsert 进 pg（按 user_id）。

    json 公开返回值经 `_to_public` 去掉 password_hash，不能直接拿来写 pg。
    这里按 user_id 再读 json 权威记录（含 hash），保证 dual→postgres 切换后仍能登录。
    已存在的影子行也 upsert（含把旧 bug 写入的空 password_hash 回填为 json 权威 hash）。
    ai_config 原样保留（api_key 已是 app.py 加密形态）；created_at 转浮点写回。
    """
    u = ret if isinstance(ret, dict) else None
    if not u or not u.get("user_id"):
        return
    uid = u["user_id"]
    # 公开返回值不含 hash；json 权威行才有。
    import user_store_json as _json
    authoritative = _json.get_user(uid)
    if authoritative:
        u = authoritative
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                cur.execute(
                    "INSERT INTO users "
                    "(user_id, email, display_name, password_hash, role, "
                    " created_at, disabled, ai_config) "
                    "VALUES (%s,%s,%s,%s,%s, to_timestamp(%s), %s, %s) "
                    "ON CONFLICT (user_id) DO UPDATE SET "
                    " email=EXCLUDED.email, display_name=EXCLUDED.display_name, "
                    " password_hash=CASE WHEN EXCLUDED.password_hash <> '' "
                    "  THEN EXCLUDED.password_hash ELSE users.password_hash END, "
                    " role=EXCLUDED.role, created_at=EXCLUDED.created_at, "
                    " disabled=EXCLUDED.disabled, ai_config=EXCLUDED.ai_config",
                    (uid, u.get("email"), u.get("display_name", ""),
                     u.get("password_hash") or "",
                     u.get("role", ROLE_USER), u.get("created_at"),
                     bool(u.get("disabled", False)),
                     psycopg.types.json.Jsonb(u.get("ai_config") or {})),
                )
    finally:
        conn.close()


def repair_empty_password_hashes_from_json():
    """把 json 权威 password_hash 回填到 pg 中空 hash 的影子行。

    旧 dual `_mirror_user` 曾把 create_user 的公开返回值（无 hash）写成空字符串；
    那些用户启动时未必再走 create_user/ensure_owner，所以需要一次批量回填。
    json 文件不存在或无法读取时返回 0（postgres 单后端、测试隔离目录皆安全）。
    """
    import user_store_json as _json
    path = getattr(_json, "USER_FILE", None)
    if path is None:
        return 0
    try:
        if not path.exists():
            return 0
    except OSError:
        return 0
    repaired = 0
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                for pub in _json.list_users():
                    uid = pub.get("user_id") if isinstance(pub, dict) else None
                    if not uid:
                        continue
                    full = _json.get_user(uid)
                    h = (full or {}).get("password_hash") or ""
                    if not h:
                        continue
                    cur.execute(
                        "UPDATE users SET password_hash=%s "
                        "WHERE user_id=%s AND (password_hash IS NULL OR password_hash='')",
                        (h, uid),
                    )
                    repaired += cur.rowcount or 0
    finally:
        conn.close()
    return repaired
