# -*- coding: utf-8 -*-
"""切片分享 —— 存储层 dispatcher（Stage 3b-1：PostgreSQL 基建 + dispatcher 拆分）。

过渡形态（见 docs/pathtogather-histopilot-platform-plugin-upgrade.md §Stage 3b 与
决策 #8）：

- 原有 JSON 文件实现（shares.json：shares/grants/rois/change log/slide_meta/
  projects）被**原样**搬到 `share_store_json.py`（含 _ 前缀私有函数、fcntl 文件锁、
  SHARE_DATA_DIR 逻辑），本文件不再持有任何业务实现。
- 本文件按 `STORAGE_BACKEND` 环境变量把调用方分发到具体后端：
    * ``json``     （默认）→ 显式 re-export `share_store_json` 的全部公共名；
    * ``postgres`` / ``dual`` → 暂不接入，访问任一公共名抛 RuntimeError
      （Stage 3b-2 才接 PostgreSQL 实现；本节点只交付分发骨架 + 基建）。
- 非法后端值在 import 期即抛 ValueError。

**验收红线：零行为变化**。所有调用方（app.py / share_server.py / tests）一律
``import share_store`` 后以 ``share_store.X`` 访问，本 dispatcher 对调用方完全
透明，一行都不需要改。

路径常量实时镜像
----------------
测试为隔离数据目录，会 monkeypatch 本模块的 ``SHARE_DATA_DIR`` / ``SHARE_FILE``
（见 tests/test_user_store.py 等）。但 JSON 实现的函数体以**裸全局**读取
``SHARE_FILE``（其 ``__globals__`` 指向 ``share_store_json``），直接 re-export
并不能让「patch ``share_store.SHARE_FILE``」对 JSON 实现生效。

为此本 dispatcher 安装自定义模块类：对 ``SHARE_DATA_DIR`` / ``SHARE_FILE`` 的
**外部**写入（monkeypatch.setattr / ``share_store.X = ...``）实时镜像回
``share_store_json.__dict__``，使二者保持一致。模块自身初始化用的是字典写入
（``globals()[name] = ...``），不走 ``__setattr__``，因此不会触发镜像、无递归。

contract 计划
-------------
双后端并存仅是迁移期过渡形态。Stage 3b contract 阶段将删除 JSON 写路径，
PostgreSQL 成为唯一存储（无 SQLite 双轨，决策 #8）。
"""

import os as _os
import sys as _sys
import types as _types

# --------------------------------------------------------------------------- #
# 后端选择（import 期一次性读取 env）
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

#: JSON 实现的公共 API（与 share_store_json 的公共名必须一致；由
#: tests/test_backend_dispatch.py 守卫防漏 export）。显式枚举，不使用 ``import *``
#: 的隐式形式。
_JSON_PUBLIC_NAMES = (
    # —— 常量 ——
    "SHARE_DATA_DIR",
    "SHARE_FILE",
    "ROI_TYPES",
    "ALLOWED_ROI_SIZES",
    "DEFAULT_ROI_SIZES",
    "ADMIN_TOKEN",
    "PERMISSION_VIEW",
    "PERMISSION_ANNOTATE",
    "PERMISSION_DOWNLOAD",
    "DEFAULT_PERMISSIONS",
    # —— 函数 ——
    "set_owner_user_id",
    "create_share",
    "get_share",
    "list_shares",
    "revoke_share",
    "claim_share",
    "claimed_active_slides_for_user",
    "list_grants_for_user",
    "add_roi",
    "update_roi",
    "list_rois",
    "get_roi",
    "get_roi_by_annotation_id",
    "delete_roi",
    "delete_roi_by_annotation_id",
    "list_changes",
    "current_change_seq",
    "set_roi_shared",
    "roi_count_by_token",
    "list_shared_rois_for_slides",
    "set_slide_meta",
    "get_slide_meta",
    "get_slide_meta_full",
    "get_all_slide_meta_full",
    "get_all_slide_meta",
    "create_project",
    "list_projects",
    "get_project",
    "update_project",
    "add_slides_to_project",
    "remove_slide_from_project",
    "delete_project",
    "annotations_by_slide",
    "annotations_by_project",
)

#: 需要实时镜像到 JSON 实现的路径配置名（函数体裸全局读取它们）。
_MIRROR_NAMES = ("SHARE_DATA_DIR", "SHARE_FILE")


def _install_json_backend():
    """json 后端：显式 re-export share_store_json 的全部公共名到本模块。"""
    import share_store_json as _json

    missing = [n for n in _JSON_PUBLIC_NAMES if not hasattr(_json, n)]
    if missing:
        raise RuntimeError(
            "share_store_json 缺少公共名 %s，dispatcher 与实现不一致" % missing
        )
    # 用字典写入而非 setattr：避免触发自定义 __setattr__ 的镜像（初始化阶段
    # JSON 实现已自带正确的 SHARE_FILE，无需镜像）。
    _g = globals()
    for _name in _JSON_PUBLIC_NAMES:
        _g[_name] = getattr(_json, _name)


if STORAGE_BACKEND == "json":
    _install_json_backend()


# --------------------------------------------------------------------------- #
# 自定义模块类：路径常量镜像 + postgres/dual 公共名访问抛 RuntimeError
# --------------------------------------------------------------------------- #
class _ShareStoreModule(_types.ModuleType):
    """过渡期分发模块类（详见模块 docstring）。"""

    def __setattr__(self, name, value):
        super().__setattr__(name, value)
        # 外部写入路径配置 → 镜像回 JSON 实现，使其函数体裸全局立即生效
        if name in _MIRROR_NAMES:
            _json = _sys.modules.get("share_store_json")
            if _json is not None:
                _json.__dict__[name] = value

    def __delattr__(self, name):
        super().__delattr__(name)
        if name in _MIRROR_NAMES:
            _json = _sys.modules.get("share_store_json")
            if _json is not None and name in _json.__dict__:
                del _json.__dict__[name]

    def __getattr__(self, name):
        # 仅当 name 不在本模块 __dict__ 时触发（PEP 562 语义）。
        if name in _JSON_PUBLIC_NAMES:
            raise RuntimeError(
                "存储后端 %r 尚未接入：postgres/dual 后端将在 Stage 3b-2 实现。"
                % STORAGE_BACKEND
            )
        raise AttributeError(
            "module %r has no attribute %r" % (__name__, name)
        )


# 把已初始化完毕的本模块切换到自定义类（此后外部 setattr/getattr 走上面的逻辑）。
_sys.modules[__name__].__class__ = _ShareStoreModule
