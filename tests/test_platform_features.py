# -*- coding: utf-8 -*-
"""平台功能前置条件测试（PostgreSQL 唯一后端，无需数据库）。

覆盖：
  - budget/demo/billing/usage 能力恒 True（postgres 唯一后端，不再有
    json/dual fail-closed 分支）；
  - require_pg_backend 为 no-op（不抛）；
  - current_backend()=="postgres"；
  - PUBLIC_DEMO_ENABLED 解析（默认 0；本任务只解析+暴露，不做启动拒绝）。
"""
import platform_features


def test_features_always_available():
    assert platform_features.budget_features_available() is True
    assert platform_features.demo_features_available() is True
    assert platform_features.billing_features_available() is True
    assert platform_features.usage_ingest_available() is True


def test_require_pg_backend_is_noop():
    assert platform_features.require_pg_backend("ai_budget") is None
    assert platform_features.require_pg_backend("demo_sessions") is None


def test_current_backend_is_postgres():
    assert platform_features.current_backend() == "postgres"
    assert platform_features.STORAGE_BACKEND == "postgres"


def test_public_demo_enabled_parsing(monkeypatch):
    monkeypatch.delenv("PUBLIC_DEMO_ENABLED", raising=False)
    assert platform_features.public_demo_enabled() is False  # 默认 0
    for raw in ("1", "true", "YES", " yes "):
        monkeypatch.setenv("PUBLIC_DEMO_ENABLED", raw)
        assert platform_features.public_demo_enabled() is True
    for raw in ("0", "false", "", "garbage", "2", "on"):
        monkeypatch.setenv("PUBLIC_DEMO_ENABLED", raw)
        assert platform_features.public_demo_enabled() is False