# -*- coding: utf-8 -*-
"""用户存储层 dispatcher（Stage 3b-1：PostgreSQL 基建 + dispatcher 拆分）。

形态（见 docs/pathtogather-histopilot-platform-plugin-upgrade.md §Stage 3b 与
决策 #8；账户系统批次 C 收口见 docs/account-system-simplification-fix-plan.md
§9.3）：

- JSON 文件实现（users.json）位于 ``user_store_json.py``（含 _ 前缀私有函数、
  fcntl 文件锁、SHARE_DATA_DIR 逻辑），本文件不持有任何业务实现；
- 按 ``STORAGE_BACKEND`` 环境变量分发：
    * ``json`` （默认）→ 显式 re-export ``user_store_json`` 的全部公共名；
    * ``postgres`` → re-export ``user_store_pg`` 的全部公共名；
    * ``dual`` → **user 侧 dual 已在批次 C 删除**：降级安装 json 后端并打一条
      deprecation warning（``STORAGE_BACKEND`` env 与 share_store 共享，share
      侧 dual 迁移机器保留，不能让本模块 import 直接炸）。
- 非法后端值在 import 期即抛 ValueError。

**验收红线：零行为变化**。所有调用方一律 ``import user_store`` 后以
``user_store.X`` 访问，本 dispatcher 对调用方完全透明。

路径常量实时镜像（与 share_store.py 同理）：测试会 monkeypatch 本模块的
``SHARE_DATA_DIR`` / ``USER_FILE``，而 JSON 实现函数体以裸全局读取
``USER_FILE``（其 ``__globals__`` 指向 ``user_store_json``）。本 dispatcher 安装
自定义模块类，把这两个路径常量的外部写入实时镜像回 ``user_store_json.__dict__``。
模块自身初始化用字典写入（``globals()[name] = ...``），不走 ``__setattr__``。

contract 状态：PostgreSQL 为生产唯一用户存储；json 仅服务本地免认证开发与
隔离单元测试（批次 C 已删 user 侧 dual 镜像与启动修复 shim）。
"""

import os as _os
import sys as _sys
import types as _types

# --------------------------------------------------------------------------- #
# 后端选择（与 share_store 同语义）
# --------------------------------------------------------------------------- #
_BACKEND_ENV = "STORAGE_BACKEND"
# user 侧合法后端（账户系统批次 C 删除 dual）。"dual" 不在其中但仍被接受：
# env 与 share_store 共享且 share 侧 dual（Stage 3b 迁移机器）保留，遇 dual
# 时降级 json + deprecation warning（见下方安装分支），不让 import 直接炸。
_VALID_BACKENDS = ("json", "postgres")

#: 当前生效的存储后端（json|postgres|dual）。默认 json，等价拆分前行为。
#: "dual" 表示 env 请求了 dual：user 侧实际安装 json 后端（已删除 dual）。
STORAGE_BACKEND = (_os.environ.get(_BACKEND_ENV) or "json").strip()
if STORAGE_BACKEND not in _VALID_BACKENDS and STORAGE_BACKEND != "dual":
    raise ValueError(
        "STORAGE_BACKEND 仅支持 %s（当前值为 %r）"
        % ("/".join(_VALID_BACKENDS), STORAGE_BACKEND)
    )

#: JSON 实现的公共 API（与 user_store_json 的公共名必须一致；由
#: tests/test_backend_dispatch.py 守卫防漏 export）。显式枚举，不用 ``import *``。
#:
#: 账户系统批次 A（docs/account-system-simplification-fix-plan.md §5.3）：
#: 删除 ensure_owner / first_owner，新增 owner 原语与统一密码策略常量；
#: PASSWORD_MIN_LENGTH / PASSWORD_MAX_LENGTH / OwnerInvariantError 由本表
#: re-export，调用方一律 ``import user_store`` 后访问。
#:
#: 账户系统批次 B（docs §6.1）：删除 get_user_by_display_name——其唯一调用方
#: 是 verify_user 的 display_name 登录 fallback（已随本批次移除），全仓无
#: 展示用途调用方。
#:
#: 账户系统批次 C（docs §4.2）：get_user_by_email → get_user_by_login_id
#: （随物理列 users.login_id 改名）。
_JSON_PUBLIC_NAMES = (
    # —— 常量 ——
    "SHARE_DATA_DIR",
    "USER_FILE",
    "ROLE_OWNER",
    "ROLE_USER",
    "ROLE_GUEST",
    "ROLE_SDK",
    "VALID_ROLES",
    "UserStoreCorrupt",
    "OwnerInvariantError",
    # 统一密码策略（docs §3.3；两实现一致）
    "PASSWORD_MIN_LENGTH",
    "PASSWORD_MAX_LENGTH",
    # user ai_config.max_steps 默认值（docs §9.2，PT-3；两实现一致）
    "DEFAULT_USER_MAX_STEPS",
    # —— 函数 ——
    "create_user",
    "get_user",
    "get_user_by_login_id",
    "verify_user",
    "list_users",
    "set_user_disabled",
    "set_user_password",
    "get_user_ai_config",
    "set_user_ai_config",
    "count_owners",
    "has_enabled_users",
    # owner 解析与引导原语（docs §5.3）
    "list_enabled_owners",
    "list_owners",
    "create_bootstrap_owner",
    "resolve_primary_owner",
)

#: 需要实时镜像到 JSON 实现的路径配置名（函数体裸全局读取它们）。
_MIRROR_NAMES = ("SHARE_DATA_DIR", "USER_FILE")


def _install_json_backend():
    """json 后端：显式 re-export user_store_json 的全部公共名到本模块。"""
    import user_store_json as _json

    missing = [n for n in _JSON_PUBLIC_NAMES if not hasattr(_json, n)]
    if missing:
        raise RuntimeError(
            "user_store_json 缺少公共名 %s，dispatcher 与实现不一致" % missing
        )
    _g = globals()
    for _name in _JSON_PUBLIC_NAMES:
        _g[_name] = getattr(_json, _name)


def _install_pg_backend():
    """postgres 后端：显式 re-export user_store_pg 的全部公共名到本模块。"""
    import user_store_pg as _pg

    missing = [n for n in _JSON_PUBLIC_NAMES if not hasattr(_pg, n)]
    if missing:
        raise RuntimeError(
            "user_store_pg 缺少公共名 %s，dispatcher 与实现不一致" % missing
        )
    _g = globals()
    for _name in _JSON_PUBLIC_NAMES:
        _g[_name] = getattr(_pg, _name)


if STORAGE_BACKEND == "json":
    _install_json_backend()
elif STORAGE_BACKEND == "postgres":
    _install_pg_backend()
else:
    # STORAGE_BACKEND=dual：user 侧 dual 后端已随账户系统批次 C 删除
    # （docs §9.3：_install_dual_backend / _make_dual_replay / _make_dual_same /
    # _DUAL_MIRRORS 及 user_store_pg._mirror_user 一并移除）。但 STORAGE_BACKEND
    # env 与 share_store 共享、share 侧 dual（Stage 3b 迁移机器）保留，这里
    # 降级安装 json 后端并打一条 deprecation warning，保证 share 侧 dual 部署
    # 与重载 dispatcher 的测试（tests/test_dual_backend.py）不会 import 即炸。
    import logging as _logging

    _logging.getLogger("svs.user_store").warning(
        "STORAGE_BACKEND=dual：user 侧 dual 后端已删除（账户系统批次 C，"
        "docs §9.3），user_store 降级为 json 后端（本地开发/测试形态，不是"
        "可登录生产形态）；share 侧 dual 机制保留。")
    _install_json_backend()


# --------------------------------------------------------------------------- #
# 自定义模块类：路径常量镜像 + 未接线公共名访问抛 RuntimeError
# --------------------------------------------------------------------------- #
class _UserStoreModule(_types.ModuleType):
    """分发模块类（详见模块 docstring）。

    json：从 json impl re-export；postgres：从 user_store_pg re-export；
    dual：user 侧已删除，降级安装 json 后端（share 侧 dual 保留）。
    """

    def __setattr__(self, name, value):
        super().__setattr__(name, value)
        if name in _MIRROR_NAMES:
            _json = _sys.modules.get("user_store_json")
            if _json is not None:
                _json.__dict__[name] = value

    def __delattr__(self, name):
        super().__delattr__(name)
        if name in _MIRROR_NAMES:
            _json = _sys.modules.get("user_store_json")
            if _json is not None and name in _json.__dict__:
                del _json.__dict__[name]

    def __getattr__(self, name):
        if name in _JSON_PUBLIC_NAMES:
            raise RuntimeError(
                "存储后端 %r 未正确接线公共名 %r"
                % (STORAGE_BACKEND, name)
            )
        raise AttributeError(
            "module %r has no attribute %r" % (__name__, name)
        )


_sys.modules[__name__].__class__ = _UserStoreModule
