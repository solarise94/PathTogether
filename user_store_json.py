# -*- coding: utf-8 -*-
"""用户存储层 —— 四级身份的基础（Stage 3a 第一节点：身份基础）。

数据文件为 SHARE_DATA_DIR/users.json（0600，fcntl 文件锁），风格仿 share_store.py。

四级身份（docs §5.1）：
  - owner   ：部署者 / superadmin，管理一切；
  - user    ：平台注册用户（邮箱账户，持久会话 / 标注历史）；
  - guest   ：受邀链接匿名进入，无注册、无 users 行（走 share token，后续阶段）；
  - sdk-user：插件访问身份（非自然人，代理某 user 调用，后续阶段）。

本节点只做身份基础：owner/user 落 users 表；guest / sdk 仅声明 ROLE_GUEST /
ROLE_SDK 常量，不入表。资源级鉴权矩阵是下一个节点的事。

密码一律用 werkzeug 的 generate_password_hash / check_password_hash（默认 pbkdf2）。
全库不得再出现明文密码存储 / 比较。
"""

import json
import os
import secrets
import time
from pathlib import Path

import fcntl

from werkzeug.security import check_password_hash, generate_password_hash

# 数据目录与文件路径（与 share_store 同目录；SHARE_DATA_DIR 由 env 决定）
SHARE_DATA_DIR = Path(
    os.environ.get("SHARE_DATA_DIR") or (Path.home() / "svs-viewer" / "share-data")
)
SHARE_DATA_DIR.mkdir(parents=True, exist_ok=True)
USER_FILE = SHARE_DATA_DIR / "users.json"

# 角色常量
ROLE_OWNER = "owner"
ROLE_USER = "user"
ROLE_GUEST = "guest"  # 仅常量声明：guest 走分享链接匿名，无 users 行
ROLE_SDK = "sdk"      # 仅常量声明：sdk-user 为插件代理身份，后续阶段实现

VALID_ROLES = (ROLE_OWNER, ROLE_USER)


class UserStoreCorrupt(Exception):
    """users.json 已存在但损坏/不可读。调用方必须 fail-closed，不得当成空库。"""


# 空结构骨架
_EMPTY = {
    "users": {},
    "meta": {"schema_version": 1},
}


def _copy_empty():
    """返回一个新的空结构（避免共享引用）。"""
    return {"users": {}, "meta": {"schema_version": 1}}


def _user_id() -> str:
    return "usr_" + secrets.token_urlsafe(8)


def _normalize_email(email: str) -> str:
    """email 唯一键规范化：小写 + 去首尾空白。"""
    return str(email or "").strip().lower()


def _load_locked(f):
    """在已锁定的文件对象上读取并解析 JSON。

    空文件（首次 touch）→ 空库。已有内容但损坏 → 备份后抛 UserStoreCorrupt，
    绝不返回空库（否则鉴权会 fail-open）。
    """
    f.seek(0)
    raw = f.read()
    if not raw:
        return _copy_empty()
    try:
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError("top-level not object")
        data.setdefault("users", {})
        data.setdefault("meta", {"schema_version": 1})
        if not isinstance(data["users"], dict):
            raise ValueError("users not object")
        if not isinstance(data["meta"], dict):
            raise ValueError("meta not object")
        return data
    except (json.JSONDecodeError, ValueError) as e:
        bak = USER_FILE.with_suffix(".json.bak")
        try:
            with open(bak, "w", encoding="utf-8") as bf:
                bf.write(raw)
        except Exception:
            pass
        raise UserStoreCorrupt(
            "users.json 损坏（已备份至 %s）：%s" % (bak.name, e)
        ) from e


def _save_locked(f, data):
    """在已锁定的文件对象上写入 JSON（先截断）。"""
    f.seek(0)
    f.truncate()
    json.dump(data, f, ensure_ascii=False, indent=2)
    f.flush()
    os.fsync(f.fileno())


def _with_lock(mode, fn):
    """以指定模式打开 USER_FILE，加排他锁后执行 fn(file_obj)。返回 fn 返回值。"""
    if not USER_FILE.exists():
        USER_FILE.touch()
    try:
        os.chmod(USER_FILE, 0o600)
    except OSError:
        pass
    with open(USER_FILE, mode, encoding="utf-8") as f:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        try:
            return fn(f)
        finally:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)


def _to_public(user: dict) -> dict:
    """导出副本（不含 password_hash）。"""
    out = dict(user)
    out.pop("password_hash", None)
    return out


# --------------------------------------------------------------------------- #
# 公共 API
# --------------------------------------------------------------------------- #
def create_user(email, password, role=ROLE_USER, display_name=None,
                _enforce_min_length=True):
    """创建用户。返回新用户 dict（不含 hash）；email 冲突抛 ValueError。

    email 会做小写规范化并作为唯一键。display_name 缺省用 email 值。
    密码经 werkzeug generate_password_hash 哈希后存储。
    _enforce_min_length=False 供 owner 引导（ADMIN_PASSWORD env）使用：既有部署
    的 ADMIN_PASSWORD 可能不足 8 位，为保证 demo 兼容不做最小长度强制。
    """
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
    now = time.time()
    uid = _user_id()

    def _do(f):
        data = _load_locked(f)
        if norm_email in data["users"]:
            raise ValueError("该邮箱/用户名已存在")
        user = {
            "user_id": uid,
            "email": norm_email,
            "display_name": name,
            "password_hash": generate_password_hash(password),
            "role": role,
            "created_at": now,
            "disabled": False,
        }
        data["users"][uid] = user
        _save_locked(f, data)
        return _to_public(user)

    return _with_lock("r+", _do)


def get_user(user_id):
    """按 user_id 取用户 dict（含 hash）；不存在返回 None。"""
    def _do(f):
        data = _load_locked(f)
        user = data["users"].get(user_id)
        return dict(user) if user else None

    return _with_lock("r+", _do)


def get_user_by_email(email):
    """按 email（小写规范化）取用户 dict（含 hash）；不存在返回 None。"""
    key = _normalize_email(email)
    if not key:
        return None

    def _do(f):
        data = _load_locked(f)
        for uid, u in data["users"].items():
            if u.get("email") == key:
                return dict(u)
        return None

    return _with_lock("r+", _do)


def get_user_by_display_name(display_name):
    """按 display_name 精确匹配取用户 dict（含 hash）；不存在返回 None。"""
    key = str(display_name or "").strip()
    if not key:
        return None

    def _do(f):
        data = _load_locked(f)
        for uid, u in data["users"].items():
            if u.get("display_name") == key:
                return dict(u)
        return None

    return _with_lock("r+", _do)


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
    def _do(f):
        data = _load_locked(f)
        items = [_to_public(u) for u in data["users"].values()]
        items.sort(key=lambda x: x.get("created_at", 0))
        return items

    return _with_lock("r+", _do)


def set_user_disabled(user_id, flag):
    """设置用户禁用状态（True=禁用）。返回更新后的用户 dict；不存在返回 None。"""
    flag = bool(flag)

    def _do(f):
        data = _load_locked(f)
        user = data["users"].get(user_id)
        if user is None:
            return None
        user["disabled"] = flag
        _save_locked(f, data)
        return _to_public(user)

    return _with_lock("r+", _do)


def set_user_password(user_id, new_password, _enforce_min_length=True):
    """重置用户密码。返回更新后的用户 dict；不存在返回 None。"""
    if not isinstance(new_password, str) or not new_password:
        raise ValueError("密码不能为空")
    if _enforce_min_length and len(new_password) < 8:
        raise ValueError("密码长度至少 8 位")

    def _do(f):
        data = _load_locked(f)
        user = data["users"].get(user_id)
        if user is None:
            return None
        user["password_hash"] = generate_password_hash(new_password)
        _save_locked(f, data)
        return _to_public(user)

    return _with_lock("r+", _do)


# --------------------------------------------------------------------------- #
# 用户 AI 凭据（Stage 3a 第二节点 2b：AI 凭据规则 §5.1.2）
#
# 每个 user 行可带一个 `ai_config` 子对象：
#   {use_platform: bool, base_url, model, api_key}
# use_platform 缺省 True（默认沿用平台官方 API）。api_key 在落盘前由 app.py
# 加密（Fernet，enc: 前缀）——本层只负责存取原样 dict，不感知加密细节（避免
# 与 app.py 循环依赖；加密/解密统一在 app.py 侧完成）。owner 无独立 ai_config
# （owner 读写平台配置，见 app.py _load_ai_config）。
# --------------------------------------------------------------------------- #
_DEFAULT_USER_AI_CONFIG = {"use_platform": True, "base_url": "", "model": "", "api_key": ""}


def _user_ai_config(user: dict) -> dict:
    """返回用户行内 ai_config（规范化后副本，缺省 use_platform=True）。"""
    raw = user.get("ai_config") or {}
    if not isinstance(raw, dict):
        raw = {}
    out = dict(_DEFAULT_USER_AI_CONFIG)
    out.update({k: raw.get(k) for k in ("base_url", "model", "api_key")})
    out["use_platform"] = bool(raw.get("use_platform", True))
    return out


def get_user_ai_config(user_id):
    """按 user_id 取用户 AI 凭据 dict（api_key 为磁盘原样，可能 enc: 密文）。

    不存在用户返回 None；用户未配置过返回规范化默认（use_platform=True 空凭据）。
    """
    def _do(f):
        data = _load_locked(f)
        user = data["users"].get(user_id)
        if user is None:
            return None
        return _user_ai_config(user)

    return _with_lock("r+", _do)


def set_user_ai_config(user_id, cfg):
    """设置用户 AI 凭据。cfg 应为 dict（use_platform/base_url/model/api_key）。

    api_key 假定已由 app.py 加密为磁盘形态；本层原样写入，不重新加密。返回更新后
    的公共用户 dict；用户不存在返回 None。
    """
    def _do(f):
        data = _load_locked(f)
        user = data["users"].get(user_id)
        if user is None:
            return None
        merged = _user_ai_config(user)
        if isinstance(cfg, dict):
            for k in ("use_platform", "base_url", "model", "api_key"):
                if k in cfg:
                    merged[k] = cfg[k]
        user["ai_config"] = merged
        _save_locked(f, data)
        return _to_public(user)

    return _with_lock("r+", _do)


def count_owners():
    """返回 role=owner 且未禁用的用户数量。"""
    def _do(f):
        data = _load_locked(f)
        return sum(
            1 for u in data["users"].values()
            if u.get("role") == ROLE_OWNER and not u.get("disabled")
        )

    return _with_lock("r+", _do)


def first_owner():
    """返回第一个 owner 用户 dict（含 hash）；无则 None。"""
    def _do(f):
        data = _load_locked(f)
        for u in data["users"].values():
            if u.get("role") == ROLE_OWNER:
                return dict(u)
        return None

    return _with_lock("r+", _do)


def has_enabled_users():
    """是否存在任一 enabled（未禁用）用户（任意角色）。"""
    def _do(f):
        data = _load_locked(f)
        return any(not u.get("disabled") for u in data["users"].values())

    return _with_lock("r+", _do)


def ensure_owner(email, password):
    """owner 引导与迁移：按 ADMIN_USERNAME/ADMIN_PASSWORD 维护 owner 账户。

    - 无 owner 角色用户 → 创建 owner（email 用 ADMIN_USERNAME 值，可非邮箱格式，
      display_name=email 同值）；
    - 已存在 owner 且 ADMIN_PASSWORD 与现存 hash 不匹配 → 更新该 owner 的
      password_hash（env 始终可重置 owner 密码，保住「改密码靠 env」运维习惯）。

    返回 owner 用户 dict（含 hash）。
    """
    norm_email = _normalize_email(email)
    name = norm_email or "owner"
    owner = first_owner()
    if owner is None:
        return create_user(name, password, role=ROLE_OWNER, display_name=name,
                           _enforce_min_length=False)
    # 已有 owner：ADMIN_PASSWORD 始终可重置其密码
    try:
        match = check_password_hash(owner.get("password_hash") or "", password)
    except Exception:
        match = False
    if not match:
        set_user_password(owner["user_id"], password, _enforce_min_length=False)
        owner = get_user(owner["user_id"])
    return owner
