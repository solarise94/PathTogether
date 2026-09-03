# -*- coding: utf-8 -*-
"""平台功能前置条件判定（Demo / AI 预算数据层，docs §4.3）。

PostgreSQL 为唯一后端；本模块不再做 json/dual fail-closed 判定，
``require_pg_backend`` / ``*_features_available`` 只是常真占位，供调用方沿用。
``public_demo_enabled`` 仍只解析 env，启动拒绝在 app.py。

本模块只做判定与 env 解析，不接 Flask 路由；``PUBLIC_DEMO_ENABLED=1`` 的
「启动期拒绝」由路由层任务实现（本层仅暴露解析结果）。
"""

import os

# 与 app.py _env_truthy 相同的真值集合（"1"/"true"/"yes"）
_TRUTHY = ("1", "true", "yes")

# 唯一后端：postgres
STORAGE_BACKEND = "postgres"


def _truthy(value) -> bool:
    """env 值 → 布尔（与 app.py _env_truthy 口径一致；REGISTRATION_OPEN env
    已随 R3 Wave2-Compat 删除，本 helper 仅服务于 PUBLIC_DEMO_ENABLED）。"""
    return (value or "").strip().lower() in _TRUTHY


def current_backend() -> str:
    """当前存储后端（恒为 postgres）。"""
    return STORAGE_BACKEND


def budget_features_available() -> bool:
    """AI 预算（原子预占 / 周期 / 用量）是否可用：postgres 唯一后端，恒 True。"""
    return True


def demo_features_available() -> bool:
    """匿名 Demo（capability / 目录 / 一次性 run）是否可用：恒 True。"""
    return True


def billing_features_available() -> bool:
    """金额计费（价格表 / 用量计价 / 账本 / 余额快照）是否可用：恒 True。"""
    return True


def usage_ingest_available() -> bool:
    """插件用量投递（POST /api/plugin/v1/usage-events）是否可用：恒 True。"""
    return True


def public_demo_enabled() -> bool:
    """解析 PUBLIC_DEMO_ENABLED（默认 0）。仅解析+暴露；启动拒绝由路由层做。"""
    return _truthy(os.environ.get("PUBLIC_DEMO_ENABLED"))


def require_pg_backend(feature: str) -> None:
    """postgres 唯一后端，无 fail-closed 分支；no-op。保留签名便于调用方沿用。"""
    return None