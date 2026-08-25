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

账户系统批次 A（docs/account-system-simplification-fix-plan.md §5.3/§9.1）：
  - auth_version 过渡兼容：读旧数据缺字段按 1，密码/disable/enable 原子递增，
    与 pg 实现语义一致；
  - owner 引导改为 list_enabled_owners / list_owners / create_bootstrap_owner /
    resolve_primary_owner，删除 ensure_owner / first_owner 的「env 对账覆盖 +
    最早 owner」语义；
  - 密码统一 15..200 策略（PASSWORD_MIN_LENGTH / PASSWORD_MAX_LENGTH），
    无旁路参数。json 后端仅服务本地免认证开发与隔离单元测试。

账户系统批次 B（docs §6.1，登录标识收口）：verify_user 只认规范化
login_id，删除 display_name 登录 fallback；get_user_by_display_name 删除。

账户系统批次 C（docs §4.2 物理收口）：
  - users.json 物理记录键 ``email`` → ``login_id``；写路径只写 ``login_id``
    （不双写），写旧格式记录时顺带完成键收口（写入后该记录只含 login_id）；
  - 读侧一次性兼容旧格式文件：记录只有 ``email`` 键时读为 login_id（口径同
    ``_auth_version`` 的缺省规范化；不写回旧键、不触发迁移写盘）；
  - 返回用户 dict 只带 "login_id" 键，删除批次 B 的 "email" deprecated 别名；
  - get_user_by_email → get_user_by_login_id（公共 API 改名）。
json 后端仅服务本地免认证开发与隔离单元测试，不是可登录生产形态。
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

# --------------------------------------------------------------------------- #
# 统一密码策略（账户系统批次 A，docs §3.3；与 user_store_pg 完全一致）：
# 15..200 字符，允许空格与长口令，不要求组合规则。所有密码写入口
# （create_user / set_user_password / create_bootstrap_owner）共用，无旁路参数。
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


class UserStoreCorrupt(Exception):
    """users.json 已存在但损坏/不可读。调用方必须 fail-closed，不得当成空库。"""


class OwnerInvariantError(Exception):
    """owner 不变量被破坏（账户系统批次 A，docs §3.2/§5.3）。

    消息携带场景标识便于区分处理：
      - ``no_owner``：库中无任何启用 owner（resolve_primary_owner 拒绝解析）；
      - ``multiple_enabled_owners``：启用的 owner 多于一个（禁止选「第一条」）；
      - ``users_table_not_empty``：create_bootstrap_owner 只允许空库首建。

    过渡兼容（docs §9.1）：json 后端仅服务本地免认证开发与隔离单元测试，
    不再是可登录生产形态；本类与 pg 实现语义一致，保证同一 dispatcher 在
    不同后端下行为对齐。
    """


class PasswordChangeConflict(Exception):
    """本人改密 CAS 冲突（P1 修复；与 user_store_pg 语义一致）。

    ``reason`` 携带可区分的失败场景：user_missing / user_disabled /
    auth_version_conflict / invalid_current_password / same_as_current
    （各场景含义见 user_store_pg 同名类 docstring）。
    """

    def __init__(self, reason, message=None):
        self.reason = reason
        super().__init__(message or reason)


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


def _normalize_login_id(login_id: str) -> str:
    """登录账号（login_id）规范化：小写 + 去首尾空白。

    users.json 物理键 0016 批次 C 起为 login_id；规范化即 login_id 的唯一键
    规范化。
    """
    return str(login_id or "").strip().lower()


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


def _login_id_of(user) -> str:
    """读侧旧格式兼容（docs §9，一次性）：记录只有 ``email`` 键时读为
    login_id（口径同 _auth_version 缺省规范化；不写回、不双写）。"""
    if not isinstance(user, dict):
        return ""
    v = user.get("login_id")
    if v is None and "email" in user:
        v = user.get("email")
    return str(v or "")


def _migrate_record_key(user: dict) -> dict:
    """写路径键收口：被写记录的旧 ``email`` 键就地改为 ``login_id``
    （批次 C 物理改名；写入后该记录只含 login_id 键，不双写）。

    仅作用于本次写路径修改的目标记录；其余未触碰记录保持原样（读侧
    _login_id_of 兼容），避免读路径触发全量迁移写盘。
    """
    if "login_id" not in user and "email" in user:
        user["login_id"] = user.pop("email")
    else:
        user.pop("email", None)
    return user


def _to_public(user: dict) -> dict:
    """导出副本（不含 password_hash；login_id 规范化出口，无 email 键）。"""
    out = _with_auth_version(user)
    out.pop("password_hash", None)
    return out


def _auth_version(user: dict) -> int:
    """读侧 auth_version 规范化：旧 json 数据缺字段/非法值按 1（docs §9.1）。"""
    v = user.get("auth_version") if isinstance(user, dict) else None
    try:
        v = int(v)
    except (TypeError, ValueError):
        return 1
    return v if v >= 1 else 1


def _with_auth_version(user):
    """返回带 int auth_version 与规范化 login_id 的副本；None 透传
    （读路径统一出口）。

    批次 C 起出口 dict 只带 "login_id" 键：旧格式记录的 ``email`` 键读为
    login_id 后不再出现在返回 dict 中（docs §4.2 物理收口）。
    """
    if user is None:
        return None
    out = dict(user)
    out.pop("email", None)
    if "login_id" not in out:
        out["login_id"] = _login_id_of(user)
    out["auth_version"] = _auth_version(user)
    return out


# --------------------------------------------------------------------------- #
# 公共 API
# --------------------------------------------------------------------------- #
def create_user(login_id, password, role=ROLE_USER, display_name=None):
    """创建用户。返回新用户 dict（不含 hash）；登录账号冲突抛 ValueError。

    参数 ``login_id`` 为**登录账号**（docs §3.1）：trim + lower 规范化后作为
    唯一键。display_name 缺省用登录账号值。返回 dict 只带 "login_id" 键
    （批次 C 起不再携带 deprecated 的 "email" 别名，docs §4.2）。密码经
    werkzeug generate_password_hash 哈希后存储，统一执行 15..200 策略
    （批次 A docs §3.3，无旁路参数）。新用户 auth_version=1。
    """
    if role not in VALID_ROLES:
        raise ValueError("非法角色")
    norm_login = _normalize_login_id(login_id)
    if not norm_login:
        raise ValueError("登录账号不能为空")
    _validate_password(password)
    name = str(display_name or "").strip() or norm_login
    now = time.time()
    uid = _user_id()

    def _do(f):
        data = _load_locked(f)
        if any(_login_id_of(u) == norm_login for u in data["users"].values()):
            raise ValueError("该登录账号已存在")
        user = {
            "user_id": uid,
            "login_id": norm_login,
            "display_name": name,
            "password_hash": generate_password_hash(password),
            "role": role,
            "created_at": now,
            "disabled": False,
            "auth_version": 1,
        }
        data["users"][uid] = user
        _save_locked(f, data)
        return _to_public(user)

    return _with_lock("r+", _do)


def get_user(user_id):
    """按 user_id 取用户 dict（含 hash 与 auth_version）；不存在返回 None。

    旧 json 数据缺 auth_version 字段时读为 1（docs §9.1 过渡兼容）；
    旧格式记录（只有 email 键）读为 login_id。返回 dict 只带 "login_id" 键。
    """
    def _do(f):
        data = _load_locked(f)
        user = data["users"].get(user_id)
        return _with_auth_version(user) if user else None

    return _with_lock("r+", _do)


def get_user_by_login_id(login_id):
    """按登录账号（login_id，物理 json 键 login_id；trim+lower 规范化）取用户
    dict（含 hash 与 auth_version）；无则 None。

    认证路径唯一入口（docs §6.1；原批次 B 名 get_user_by_email，批次 C 随
    物理键改名）。旧格式记录只有 email 键时按 _login_id_of 兼容匹配。
    """
    key = _normalize_login_id(login_id)
    if not key:
        return None

    def _do(f):
        data = _load_locked(f)
        for uid, u in data["users"].items():
            if _login_id_of(u) == key:
                return _with_auth_version(u)
        return None

    return _with_lock("r+", _do)


def verify_user(login_id, password):
    """校验登录：只认规范化 login_id，核对密码（docs §6.1）。

    流程：normalize(trim + lower) → 按规范化 login_id 唯一键查（等价 PG 侧
    lower(login_id) 唯一索引）→ disabled 检查 → check_password_hash。
    display_name 不参与登录解析（展示属性可重复，不得用作身份属性）。

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
    """返回全部用户（不含 hash，含 auth_version），按 created_at 升序。"""
    def _do(f):
        data = _load_locked(f)
        items = [_with_auth_version(u) for u in data["users"].values()]
        items.sort(key=lambda x: x.get("created_at", 0))
        return [_to_public(x) for x in items]

    return _with_lock("r+", _do)


def set_user_disabled(user_id, flag):
    """设置用户禁用状态（True=禁用）。返回更新后的用户 dict；不存在返回 None。

    disable 与 enable 两个方向都在同一次原子写内递增 auth_version
    （批次 A docs §6.2：enable 也递增，防止禁用期间未发请求的旧 Cookie
    在重新启用后被激活）。旧记录缺 auth_version 按 1 起算（§9.1）。
    """
    flag = bool(flag)

    def _do(f):
        data = _load_locked(f)
        user = data["users"].get(user_id)
        if user is None:
            return None
        _migrate_record_key(user)
        user["disabled"] = flag
        user["auth_version"] = _auth_version(user) + 1
        _save_locked(f, data)
        return _to_public(user)

    return _with_lock("r+", _do)


def set_user_password(user_id, new_password):
    """重置用户密码。返回更新后的用户 dict；不存在返回 None。

    密码统一执行 15..200 策略（无旁路参数）；hash 更新与 auth_version+1
    在同一次原子写内（批次 A docs §6.2，旧 session 凭据版本随之失效）。
    旧记录缺 auth_version 按 1 起算（§9.1）。

    注意：本函数**不做**当前密码/版本校验，仅供管理员重置等权威路径；
    本人改密一律走 change_own_password（CAS，P1 修复）。
    """
    _validate_password(new_password)

    def _do(f):
        data = _load_locked(f)
        user = data["users"].get(user_id)
        if user is None:
            return None
        _migrate_record_key(user)
        user["password_hash"] = generate_password_hash(new_password)
        user["auth_version"] = _auth_version(user) + 1
        _save_locked(f, data)
        return _to_public(user)

    return _with_lock("r+", _do)


def change_own_password(user_id, current_password, new_password,
                        expected_auth_version):
    """本人改密 CAS 原语：验当前密码/版本 + 更新 hash 与版本，同一次原子写。

    与 user_store_pg.change_own_password 语义一致（P1 修复）：在文件排他锁
    内重读行，依次校验存在/未禁用、``auth_version == expected_auth_version``
    （不符 → 已被管理员重置等写路径推进，拒绝覆盖）、current_password 对
    当前 hash、新密码策略与「不得与当前相同」，全部通过才写 hash 并
    auth_version+1。失败 raise PasswordChangeConflict 或 ValueError（策略），
    不落盘。成功返回更新后的用户 dict（不含 hash）。
    """
    def _do(f):
        data = _load_locked(f)
        user = data["users"].get(user_id)
        if user is None:
            raise PasswordChangeConflict("user_missing")
        if user.get("disabled"):
            raise PasswordChangeConflict("user_disabled")
        if _auth_version(user) != int(expected_auth_version or 0):
            raise PasswordChangeConflict(
                "auth_version_conflict",
                "auth_version_conflict：库内版本已被其它写路径推进"
                "（expected=%s, actual=%s），拒绝覆盖"
                % (expected_auth_version, _auth_version(user)))
        try:
            current_ok = check_password_hash(
                user.get("password_hash") or "", current_password or "")
        except Exception:
            current_ok = False
        if not current_ok:
            raise PasswordChangeConflict("invalid_current_password")
        _validate_password(new_password)
        if check_password_hash(user.get("password_hash") or "", new_password):
            raise PasswordChangeConflict("same_as_current")
        _migrate_record_key(user)
        user["password_hash"] = generate_password_hash(new_password)
        user["auth_version"] = _auth_version(user) + 1
        _save_locked(f, data)
        return _to_public(user)

    return _with_lock("r+", _do)


# --------------------------------------------------------------------------- #
# 用户 AI 凭据（Stage 3a 第二节点 2b：AI 凭据规则 §5.1.2）
#
# 每个 user 行可带一个 `ai_config` 子对象：
#   {use_platform: bool, base_url, model, api_key, max_steps}
# use_platform 缺省 True（默认沿用平台官方 API）。api_key 在落盘前由 app.py
# 加密（Fernet，enc: 前缀）——本层只负责存取原样 dict，不感知加密细节（避免
# 与 app.py 循环依赖；加密/解密统一在 app.py 侧完成）。owner 无独立 ai_config
# （owner 读写平台配置，见 app.py _load_ai_config）。
# max_steps（docs §9.2 / §12.3）：自带 API 凭据时每次任务的步数，默认 20，
# 仅在 use_platform=false 时生效（平台 AI 步数由周期 platform_task_max_steps
# 决定）；上限校验在 app.py 权威层（当前周期 own_task_max_steps_limit）。
# --------------------------------------------------------------------------- #
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
    """设置用户 AI 凭据。cfg 应为 dict（use_platform/base_url/model/api_key/max_steps）。

    api_key 假定已由 app.py 加密为磁盘形态；本层原样写入，不重新加密。返回更新后
    的公共用户 dict；用户不存在返回 None。
    """
    def _do(f):
        data = _load_locked(f)
        user = data["users"].get(user_id)
        if user is None:
            return None
        _migrate_record_key(user)
        merged = _user_ai_config(user)
        if isinstance(cfg, dict):
            for k in ("use_platform", "base_url", "model", "api_key"):
                if k in cfg:
                    merged[k] = cfg[k]
            if "max_steps" in cfg:
                merged["max_steps"] = _user_max_steps(cfg.get("max_steps"))
        user["ai_config"] = merged
        # 写路径携带 auth_version（docs §9.1）：旧记录落盘时补齐为 1，不递增
        #（AI 凭据配置不是安全相关变化，不废止 session）
        user.setdefault("auth_version", _auth_version(user))
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


# --------------------------------------------------------------------------- #
# owner 解析与引导（账户系统批次 A，docs §3.2/§5.3）
#
# 删除含糊的 ensure_owner()/first_owner()（「最早创建的 owner」+ env 密码对账
# 覆盖），改为显式原语：list_enabled_owners / list_owners /
# create_bootstrap_owner / resolve_primary_owner。json 后端无部分唯一索引，
# 「最多一个 enabled owner」由 resolve_primary_owner 的计数检查与启动检查
# 双重防御（docs §9.1：json 仅过渡/测试形态，不再是可登录生产形态）。
# --------------------------------------------------------------------------- #
def _sorted_owners(users: dict):
    """按 (created_at, user_id) 升序返回 owner 行（含 disabled）。"""
    owners = [u for u in users.values() if u.get("role") == ROLE_OWNER]
    owners.sort(key=lambda u: (u.get("created_at", 0), u.get("user_id", "")))
    return owners


def list_enabled_owners():
    """返回全部启用 owner（role='owner' 且未 disabled）的 dict 列表。

    按 created_at, user_id 升序；每项含 password_hash 与 auth_version
    （owner 解析需要校验 hash 非空，属内部 API，不经公共用户输出）。
    """
    def _do(f):
        data = _load_locked(f)
        return [
            _with_auth_version(u)
            for u in _sorted_owners(data["users"])
            if not u.get("disabled")
        ]

    return _with_lock("r+", _do)


def list_owners():
    """返回全部 owner 行（含 disabled），排序与字段同 list_enabled_owners。"""
    def _do(f):
        data = _load_locked(f)
        return [_with_auth_version(u) for u in _sorted_owners(data["users"])]

    return _with_lock("r+", _do)


def create_bootstrap_owner(login_id, password):
    """空库首建 owner（仅引导用）。返回新 owner dict（含 hash 与 auth_version）。

    语义与 user_store_pg 完全一致（docs §5.2/§5.3）：
      - 仅当 users 存储完全为空时创建；任何已存在行 → OwnerInvariantError
        （users_table_not_empty）；
      - fcntl 排他锁内复查空表再插入（进程间串行化，等价 pg 侧 advisory lock）；
      - login_id 字段写规范化值（trim + lower），display_name 同值，
        role='owner'；密码统一执行 15..200 策略；
      - 不对已有 owner 做任何对账/改密（那是已删除的 ensure_owner 语义）。
    """
    norm_login = _normalize_login_id(login_id)
    if not norm_login:
        raise ValueError("登录账号不能为空")
    _validate_password(password)
    now = time.time()
    uid = _user_id()

    def _do(f):
        data = _load_locked(f)
        if data["users"]:
            raise OwnerInvariantError(
                "users_table_not_empty：users 存储已有 %d 行，"
                "create_bootstrap_owner 仅允许在完全空库时创建首个 owner"
                "（已有 owner 时任何 bootstrap 环境变量不得改动账号）"
                % len(data["users"])
            )
        user = {
            "user_id": uid,
            "login_id": norm_login,
            "display_name": norm_login,
            "password_hash": generate_password_hash(password),
            "role": ROLE_OWNER,
            "created_at": now,
            "disabled": False,
            "auth_version": 1,
        }
        data["users"][uid] = user
        _save_locked(f, data)
        return _with_auth_version(user)

    return _with_lock("r+", _do)


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
    def _do(f):
        data = _load_locked(f)
        return any(not u.get("disabled") for u in data["users"].values())

    return _with_lock("r+", _do)
