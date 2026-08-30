# -*- coding: utf-8 -*-
"""平台功能前置条件判定测试（json/PG 双模式都跑，无需数据库）。

覆盖（docs §4.3）：
  - budget/demo 能力仅 STORAGE_BACKEND=postgres 为 True（dual 不算）；
  - json/dual 调用 PG-only 存储原语 → PgFeatureUnavailable（fail-closed，
    不静默退化内存计数）；
  - PUBLIC_DEMO_ENABLED 解析（默认 0；本任务只解析+暴露，不做启动拒绝）。

后端值用 monkeypatch 模拟 json/dual/postgres 三种形态，双模式运行均覆盖。
"""
import pytest

import platform_features


@pytest.mark.parametrize("backend", ["json", "dual"])
def test_features_unavailable_on_json_and_dual(monkeypatch, backend):
    monkeypatch.setattr(platform_features, "STORAGE_BACKEND", backend)
    assert platform_features.budget_features_available() is False
    assert platform_features.demo_features_available() is False


def test_features_available_on_postgres(monkeypatch):
    monkeypatch.setattr(platform_features, "STORAGE_BACKEND", "postgres")
    assert platform_features.budget_features_available() is True
    assert platform_features.demo_features_available() is True
    assert platform_features.require_pg_backend("ai_budget") is None


@pytest.mark.parametrize("backend", ["json", "dual"])
def test_require_pg_backend_raises_with_code(monkeypatch, backend):
    monkeypatch.setattr(platform_features, "STORAGE_BACKEND", backend)
    with pytest.raises(platform_features.PgFeatureUnavailable) as ei:
        platform_features.require_pg_backend("ai_budget")
    assert ei.value.code == "pg_backend_required"
    assert "ai_budget" in str(ei.value)
    assert backend in str(ei.value)


def test_public_demo_enabled_parsing(monkeypatch):
    monkeypatch.delenv("PUBLIC_DEMO_ENABLED", raising=False)
    assert platform_features.public_demo_enabled() is False  # 默认 0
    for raw in ("1", "true", "YES", " yes "):
        monkeypatch.setenv("PUBLIC_DEMO_ENABLED", raw)
        assert platform_features.public_demo_enabled() is True
    for raw in ("0", "false", "", "garbage", "2", "on"):
        monkeypatch.setenv("PUBLIC_DEMO_ENABLED", raw)
        assert platform_features.public_demo_enabled() is False


@pytest.mark.parametrize("backend", ["json", "dual"])
def test_budget_store_fail_closed_on_json_and_dual(monkeypatch, backend):
    """json/dual 调 ai_budget 原语：抛 PgFeatureUnavailable，不退化内存计数。"""
    import budget_store

    monkeypatch.setattr(platform_features, "STORAGE_BACKEND", backend)
    with pytest.raises(platform_features.PgFeatureUnavailable):
        budget_store.reserve_turn("req_x", "user", "usr_1", "platform")
    with pytest.raises(platform_features.PgFeatureUnavailable):
        budget_store.consume("req_x", "hp_sess")
    with pytest.raises(platform_features.PgFeatureUnavailable):
        budget_store.release("req_x")
    with pytest.raises(platform_features.PgFeatureUnavailable):
        budget_store.reclaim_expired()
    with pytest.raises(platform_features.PgFeatureUnavailable):
        budget_store.usage_report()
    with pytest.raises(platform_features.PgFeatureUnavailable):
        budget_store.reset_period()
    with pytest.raises(platform_features.PgFeatureUnavailable):
        budget_store.update_period_limits({"platform_turn_limit": 10})
    with pytest.raises(platform_features.PgFeatureUnavailable):
        budget_store.get_current_period()


@pytest.mark.parametrize("backend", ["json", "dual"])
def test_demo_store_fail_closed_on_json_and_dual(monkeypatch, backend):
    import demo_store

    monkeypatch.setattr(platform_features, "STORAGE_BACKEND", backend)
    with pytest.raises(platform_features.PgFeatureUnavailable):
        demo_store.create_capability("dmo_1", "hash_1")
    with pytest.raises(platform_features.PgFeatureUnavailable):
        demo_store.get_valid_capability("hash_1")
    with pytest.raises(platform_features.PgFeatureUnavailable):
        demo_store.reserve_run("dmo_1", "req_1", "sld_a", "rev_1")
    with pytest.raises(platform_features.PgFeatureUnavailable):
        demo_store.accept_run("dmr_1", "hp_sess")
    with pytest.raises(platform_features.PgFeatureUnavailable):
        demo_store.release_run("dmr_1")
    with pytest.raises(platform_features.PgFeatureUnavailable):
        demo_store.finish_run("dmr_1")
    with pytest.raises(platform_features.PgFeatureUnavailable):
        demo_store.expire_run("dmr_1")
    with pytest.raises(platform_features.PgFeatureUnavailable):
        demo_store.hit_ip_request_rate("ipp_1")
    with pytest.raises(platform_features.PgFeatureUnavailable):
        demo_store.revoke_by_slide("sld_a")
    with pytest.raises(platform_features.PgFeatureUnavailable):
        demo_store.catalog_add("sld_a")
    with pytest.raises(platform_features.PgFeatureUnavailable):
        demo_store.catalog_remove("sld_a")
    with pytest.raises(platform_features.PgFeatureUnavailable):
        demo_store.catalog_list_ordered()
    with pytest.raises(platform_features.PgFeatureUnavailable):
        demo_store.catalog_set_default("sld_a")


@pytest.mark.parametrize("backend", ["json", "dual"])
def test_auth_limit_store_fail_closed_on_json_and_dual(monkeypatch, backend):
    import auth_limit_store

    monkeypatch.setattr(platform_features, "STORAGE_BACKEND", backend)
    with pytest.raises(platform_features.PgFeatureUnavailable):
        auth_limit_store.record_auth_failure("acct_h", "ip_h")
    with pytest.raises(platform_features.PgFeatureUnavailable):
        auth_limit_store.check_auth_locked("acct_h", "ip_h")
    with pytest.raises(platform_features.PgFeatureUnavailable):
        auth_limit_store.clear_auth_failures("acct_h", "ip_h")
