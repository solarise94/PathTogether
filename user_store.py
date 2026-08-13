# -*- coding: utf-8 -*-
"""用户存储层 dispatcher（Stage 3b-1：PostgreSQL 基建 + dispatcher 拆分）。

过渡形态（见 docs/pathtogather-histopilot-platform-plugin-upgrade.md §Stage 3b 与
决策 #8）：

- 原有 JSON 文件实现（users.json：四级身份基础）被**原样**搬到
  `user_store_json.py`（含 _ 前缀私有函数、fcntl 文件锁、SHARE_DATA_DIR 逻辑），
  本文件不再持有任何业务实现。
- 按 `STORAGE_BACKEND` 环境变量分发：
    * ``json``     （默认）→ 显式 re-export `user_store_json` 的全部公共名；
    * ``postgres`` / ``dual`` → 暂不接入，访问任一公共名抛 RuntimeError
      （Stage 3b-2 才接 PostgreSQL 实现；本节点只交付分发骨架 + 基建）。
- 非法后端值在 import 期即抛 ValueError。

**验收红线：零行为变化**。所有调用方一律 ``import user_store`` 后以
``user_store.X`` 访问，本 dispatcher 对调用方完全透明。

路径常量实时镜像（与 share_store.py 同理）：测试会 monkeypatch 本模块的
``SHARE_DATA_DIR`` / ``USER_FILE``，而 JSON 实现函数体以裸全局读取
``USER_FILE``（其 ``__globals__`` 指向 ``user_store_json``）。本 dispatcher 安装
自定义模块类，把这两个路径常量的外部写入实时镜像回 ``user_store_json.__dict__``。
模块自身初始化用字典写入（``globals()[name] = ...``），不走 ``__setattr__``。

contract 计划：Stage 3b contract 阶段删除 JSON 写路径，PostgreSQL 成为唯一存储。
"""

import os as _os
import sys as _sys
import types as _types

# --------------------------------------------------------------------------- #
# 后端选择（与 share_store 同语义）
# --------------------------------------------------------------------------- #
_BACKEND_ENV = "STORAGE_BACKEND"
_VALID_BACKENDS = ("json", "postgres", "dual")

#: 当前生效的存储后端（json|postgres|dual）。默认 json，等价拆分前行为。
STORAGE_BACKEND = (_os.environ.get(_BACKEND_ENV) or "json").strip()
if STORAGE_BACKEND not in _VALID_BACKENDS:
    raise ValueError(
        "STORAGE_BACKEND 仅支持 %s，当前值为 %r"
        % ("/".join(_VALID_BACKENDS), STORAGE_BACKEND)
    )

#: JSON 实现的公共 API（与 user_store_json 的公共名必须一致；由
#: tests/test_backend_dispatch.py 守卫防漏 export）。显式枚举，不用 ``import *``。
_JSON_PUBLIC_NAMES = (
    # —— 常量 ——
    "SHARE_DATA_DIR",
    "USER_FILE",
    "ROLE_OWNER",
    "ROLE_USER",
    "ROLE_GUEST",
    "ROLE_SDK",
    "VALID_ROLES",
    # —— 函数 ——
    "create_user",
    "get_user",
    "get_user_by_email",
    "get_user_by_display_name",
    "verify_user",
    "list_users",
    "set_user_disabled",
    "set_user_password",
    "get_user_ai_config",
    "set_user_ai_config",
    "count_owners",
    "first_owner",
    "has_enabled_users",
    "ensure_owner",
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


if STORAGE_BACKEND == "json":
    _install_json_backend()


# --------------------------------------------------------------------------- #
# 自定义模块类：路径常量镜像 + postgres/dual 公共名访问抛 RuntimeError
# --------------------------------------------------------------------------- #
class _UserStoreModule(_types.ModuleType):
    """过渡期分发模块类（详见模块 docstring）。"""

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
                "存储后端 %r 尚未接入：postgres/dual 后端将在 Stage 3b-2 实现。"
                % STORAGE_BACKEND
            )
        raise AttributeError(
            "module %r has no attribute %r" % (__name__, name)
        )


_sys.modules[__name__].__class__ = _UserStoreModule
