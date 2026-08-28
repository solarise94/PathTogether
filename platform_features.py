# -*- coding: utf-8 -*-
"""平台功能前置条件判定（Demo / AI 预算数据层，docs §4.3）。

匿名 Demo capability、跨 worker 预算、登录锁定与 reservation 回收都依赖 PG
一致事务。第一阶段仅在 ``STORAGE_BACKEND=postgres`` 时开放：

- ``json``：本地单机开发形态，无公网 Demo / 多用户预算 / 跨 worker 防爆破保证；
- ``dual``：expand 形态，PG 只是 json 权威的影子副本，**不是**预算权威来源，
  同样不算满足前置条件（设计明确 dual 不算）；
- 因此本模块对 json/dual 一律 fail-closed：``require_pg_backend`` 抛
  ``PgFeatureUnavailable``，调用方不得静默退化到进程内计数。

本模块只做判定与 env 解析，不接 Flask 路由；``PUBLIC_DEMO_ENABLED=1`` 但后端
不是 postgres 的「启动期拒绝」由路由层任务实现（本层仅暴露解析结果）。
"""

import os

# 与 app.py _env_truthy 相同的真值集合（"1"/"true"/"yes"）
_TRUTHY = ("1", "true", "yes")

# import 期一次性读取（与 share_store / user_store dispatcher 同语义）
STORAGE_BACKEND = (os.environ.get("STORAGE_BACKEND") or "json").strip()


def _truthy(value) -> bool:
    """env 值 → 布尔（与 app.py REGISTRATION_OPEN / _env_truthy 口径一致）。"""
    return (value or "").strip().lower() in _TRUTHY


class PgFeatureUnavailable(RuntimeError):
    """json/dual 后端调用 PG-only 能力：fail-closed 拒绝。

    携带 ``code``（"pg_backend_required"），供路由层映射稳定错误码；
    绝不能被捕获后退化成内存计数（那会静默失去跨 worker 一致性保证）。
    """

    code = "pg_backend_required"

    def __init__(self, message=None):
        super().__init__(message or (
            "该能力要求 STORAGE_BACKEND=postgres（当前 %r）；"
            "json/dual 后端 fail-closed，不提供跨 worker 一致保证"
            % STORAGE_BACKEND))


def current_backend() -> str:
    """当前存储后端（json|postgres|dual）。测试可 monkeypatch STORAGE_BACKEND。"""
    return STORAGE_BACKEND


def budget_features_available() -> bool:
    """AI 预算（原子预占 / 周期 / 用量）是否可用：仅 postgres 为 True。"""
    return STORAGE_BACKEND == "postgres"


def demo_features_available() -> bool:
    """匿名 Demo（capability / 目录 / 一次性 run）是否可用：仅 postgres 为 True。"""
    return STORAGE_BACKEND == "postgres"


def billing_features_available() -> bool:
    """金额计费（价格表 / 用量计价 / 账本 / 余额快照）是否可用：仅 postgres。

    admin-billing 方案 §6.1：billing 能力只在 STORAGE_BACKEND=postgres 开放；
    json/dual 稳定 pg_backend_required，不得降级到进程内余额（金额账本无
    跨 worker 一致保证时不可信）。
    """
    return STORAGE_BACKEND == "postgres"


def usage_ingest_available() -> bool:
    """插件用量投递（POST /api/plugin/v1/usage-events）是否可用：仅 postgres。

    与 billing_features_available 同口径（ingest 即 billing 写路径的入口）；
    单独暴露便于路由层与未来 admin API 分别引用。
    """
    return STORAGE_BACKEND == "postgres"


def public_demo_enabled() -> bool:
    """解析 PUBLIC_DEMO_ENABLED（默认 0）。仅解析+暴露；启动拒绝由路由层做。"""
    return _truthy(os.environ.get("PUBLIC_DEMO_ENABLED"))


def require_pg_backend(feature: str) -> None:
    """json/dual 后端调用 PG-only 存储原语前的 fail-closed 守卫。

    ``feature`` 为能力名（如 "ai_budget" / "demo_sessions" / "auth_rate_limits"），
    出现在错误信息里帮助定位调用方。
    """
    if STORAGE_BACKEND != "postgres":
        raise PgFeatureUnavailable(
            "%s 要求 STORAGE_BACKEND=postgres（当前 %r），"
            "json/dual 后端 fail-closed" % (feature, STORAGE_BACKEND))
