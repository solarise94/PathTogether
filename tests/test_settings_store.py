# -*- coding: utf-8 -*-
"""platform_settings 存取测试（docs §7.3）。

两段：
  - json/dual 行为（双模式都跑，后端用 monkeypatch 模拟）：不可写、读取
    fallback env、get_registration_open 读 REGISTRATION_OPEN；
  - PG 行为（仅 RUN_PG_TESTS=1）：UPSERT 读写、registration_open 的
    「PG 有值优先 / env 只作 bootstrap 并回写」解析顺序。

PG 侧表由 conftest 每用例 TRUNCATE（platform_settings 在清单内）。
"""
import pytest

import platform_features
import settings_store
from pg_compat import BACKEND

pg_only = pytest.mark.skipif(
    BACKEND != "postgres",
    reason="platform_settings 读写需 PG（RUN_PG_TESTS=1）",
)


# --------------------------------------------------------------------------- #
# json/dual：不可写 + env fallback（上层 fail-closed 判定依据）
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("backend", ["json", "dual"])
def test_settings_not_writable_on_json_and_dual(monkeypatch, backend):
    monkeypatch.setattr(platform_features, "STORAGE_BACKEND", backend)
    assert settings_store.settings_writable() is False


@pytest.mark.parametrize("backend", ["json", "dual"])
def test_set_setting_fail_closed_on_json_and_dual(monkeypatch, backend):
    monkeypatch.setattr(platform_features, "STORAGE_BACKEND", backend)
    with pytest.raises(platform_features.PgFeatureUnavailable):
        settings_store.set_setting("registration_open", True)


def test_get_setting_returns_default_on_json_backend(monkeypatch):
    monkeypatch.setattr(platform_features, "STORAGE_BACKEND", "json")
    assert settings_store.get_setting("registration_open", default="v") == "v"
    assert settings_store.get_setting("registration_open") is None


@pytest.mark.parametrize("backend", ["json", "dual"])
def test_get_registration_open_env_fallback_on_json_and_dual(
        monkeypatch, backend):
    monkeypatch.setattr(platform_features, "STORAGE_BACKEND", backend)
    monkeypatch.delenv("REGISTRATION_OPEN", raising=False)
    assert settings_store.get_registration_open() is False
    monkeypatch.setenv("REGISTRATION_OPEN", "1")
    assert settings_store.get_registration_open() is True


# --------------------------------------------------------------------------- #
# PG：读写 + bootstrap 语义
# --------------------------------------------------------------------------- #
@pg_only
def test_set_and_get_setting_roundtrip():
    assert settings_store.settings_writable() is True
    assert settings_store.set_setting(
        "k1", {"a": 1}, updated_by="usr_owner") == {"a": 1}
    assert settings_store.get_setting("k1") == {"a": 1}
    assert settings_store.get_setting("missing", default=7) == 7
    assert settings_store.get_setting("missing") is None
    # UPSERT 覆盖
    assert settings_store.set_setting("k1", [1, 2]) == [1, 2]
    assert settings_store.get_setting("k1") == [1, 2]
    # 布尔（registration_open 形态）
    settings_store.set_setting(settings_store.REGISTRATION_OPEN_KEY, True)
    assert settings_store.get_setting(settings_store.REGISTRATION_OPEN_KEY) \
        is True


@pg_only
def test_registration_open_env_bootstrap_then_pg_authoritative(monkeypatch):
    """解析顺序：PG 有值 → 用 PG；否则 env 作 bootstrap 默认并回写 PG。"""
    monkeypatch.delenv("REGISTRATION_OPEN", raising=False)
    # PG 无值 → env 默认 False，且首次读取回写 PG
    assert settings_store.get_registration_open() is False
    assert settings_store.get_setting(settings_store.REGISTRATION_OPEN_KEY) \
        is False
    # env 之后翻转不影响：PG 已是权威（env 只作 bootstrap）
    monkeypatch.setenv("REGISTRATION_OPEN", "1")
    assert settings_store.get_registration_open() is False
    # owner 在 PG 上打开 → 立即权威
    settings_store.set_setting(
        settings_store.REGISTRATION_OPEN_KEY, True, updated_by="usr_owner")
    assert settings_store.get_registration_open() is True


@pg_only
def test_registration_open_bootstrap_true_when_env_true(monkeypatch):
    monkeypatch.setenv("REGISTRATION_OPEN", "true")
    assert settings_store.get_registration_open() is True
    # bootstrap 值 True 也回写 PG
    assert settings_store.get_setting(settings_store.REGISTRATION_OPEN_KEY) \
        is True
    # env 撤掉后仍以 PG 为准
    monkeypatch.delenv("REGISTRATION_OPEN", raising=False)
    assert settings_store.get_registration_open() is True
