# -*- coding: utf-8 -*-
"""platform_settings 存取测试（docs §7.3）。

PostgreSQL 唯一后端（仅 RUN_PG_TESTS=1）：UPSERT 读写、CAS、registration_mode
解析（mode 键缺行 → fail-closed bootstrap closed）。

PG 侧表由 conftest 每用例 TRUNCATE（platform_settings 在清单内）。
"""
import settings_store
from pg_compat import BACKEND


# --------------------------------------------------------------------------- #
# PG（唯一后端）：读写 + CAS + registration_mode fail-closed 解析
# --------------------------------------------------------------------------- #
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
    # 布尔（任意 JSON 标量形态）
    settings_store.set_setting("k_bool", True)
    assert settings_store.get_setting("k_bool") is True


def test_registration_mode_missing_row_bootstraps_closed():
    """R3 Wave2-Compat：mode 键缺行 → fail-closed 降级 closed 并 bootstrap
    回写（旧布尔 registration_open / REGISTRATION_OPEN env 均已删除）。"""
    assert settings_store.get_registration_mode() == "closed"
    assert settings_store.get_setting(settings_store.REGISTRATION_MODE_KEY) \
        == "closed"
    # owner 显式切换后立即权威
    settings_store.set_registration_mode("invite_only", updated_by="usr_owner")
    assert settings_store.get_registration_mode() == "invite_only"
