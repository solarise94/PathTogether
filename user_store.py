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
    "UserStoreCorrupt",
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


def _make_dual_replay(name, json_fn, mirror):
    """dual 后端写包装（result-replay）：json 为权威，ret=json_fn(...) → mirror(ret,
    原参) 把权威 dict 按其中身份值（user_id）镜像进 pg。json 抛错则不写 pg。"""
    import functools
    import logging

    _log = logging.getLogger("svs.dual.user")

    @functools.wraps(json_fn)
    def _wrapped(*args, **kwargs):
        ret = json_fn(*args, **kwargs)
        try:
            mirror(ret, *args, **kwargs)
        except Exception:
            _log.exception("dual 后端 pg 镜像写失败: %s", name)
        return ret

    return _wrapped


def _make_dual_same(name, json_fn, pg_fn):
    """dual 后端写包装（同参重放）：无内部生成身份的写，直接同参调 pg（异常记 log）。"""
    import functools
    import logging

    _log = logging.getLogger("svs.dual.user")

    @functools.wraps(json_fn)
    def _wrapped(*args, **kwargs):
        ret = json_fn(*args, **kwargs)
        try:
            pg_fn(*args, **kwargs)
        except Exception:
            _log.exception("dual 后端 pg 镜像写失败: %s", name)
        return ret

    return _wrapped


# result-replay 镜像：json 内部生成 user_id 的写（create_user/ensure_owner），用 json
# 返回的权威 dict 按 user_id 原样 upsert 进 pg（身份一致）。其余写按 user_id 定位，
# user_id 来自调用方入参/已存在用户，同参重放即一致。
_DUAL_MIRRORS = {
    "create_user": "_mirror_user",
    "ensure_owner": "_mirror_user",
}

# 同参重放：无内部生成身份，直接同参调 pg（set_user_* 均按调用方入参 user_id 定位，
# 与 json 幂等——json 返回 None 时 pg 也不存在，镜像 no-op 等价）。
_DUAL_SAME_ARGS = {
    "set_user_disabled", "set_user_password", "set_user_ai_config",
}


def _install_dual_backend():
    """dual 后端（expand 形态）：写 json + result-replay/best-effort 写 pg、读 json。

    读路径切换留 Stage 3b-3。常量/读函数 re-export 自 json impl。
    """
    import user_store_json as _json
    import user_store_pg as _pg

    for n in _JSON_PUBLIC_NAMES:
        if not hasattr(_json, n):
            raise RuntimeError(
                "user_store_json 缺少公共名 %s，dispatcher 与实现不一致" % n)
    for n in _JSON_PUBLIC_NAMES:
        if not hasattr(_pg, n):
            raise RuntimeError(
                "user_store_pg 缺少公共名 %s，dispatcher 与实现不一致" % n)
    _g = globals()
    for _name in _JSON_PUBLIC_NAMES:
        if _name in _DUAL_MIRRORS:
            _g[_name] = _make_dual_replay(
                _name, getattr(_json, _name), getattr(_pg, _DUAL_MIRRORS[_name]))
        elif _name in _DUAL_SAME_ARGS:
            _g[_name] = _make_dual_same(
                _name, getattr(_json, _name), getattr(_pg, _name))
        else:
            _g[_name] = getattr(_json, _name)


if STORAGE_BACKEND == "json":
    _install_json_backend()
elif STORAGE_BACKEND == "postgres":
    _install_pg_backend()
elif STORAGE_BACKEND == "dual":
    _install_dual_backend()


# --------------------------------------------------------------------------- #
# 自定义模块类：路径常量镜像 + postgres/dual 公共名访问抛 RuntimeError
# --------------------------------------------------------------------------- #
class _UserStoreModule(_types.ModuleType):
    """过渡期分发模块类（详见模块 docstring）。

    json：从 json impl re-export；postgres：从 user_store_pg re-export；
    dual（expand 形态）：写 json + best-effort 写 pg、读 json。
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
                "存储后端 %r 未正确接线公共名 %r（postgres/dual 已接入）"
                % (STORAGE_BACKEND, name)
            )
        raise AttributeError(
            "module %r has no attribute %r" % (__name__, name)
        )


_sys.modules[__name__].__class__ = _UserStoreModule
