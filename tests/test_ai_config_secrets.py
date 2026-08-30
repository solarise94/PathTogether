# -*- coding: utf-8 -*-
"""secret/config 降级观测与 fail-fast 测试（review-2026-08-29 §10.4 G8）。

覆盖六处旧「零日志降级」的分类修复：
  1. flask secret key：不存在 → 按设计创建；**空文件 / 读失败 → SystemExit**
     （启动关键 secret，静默轮换会使全体 session/CSRF 瞬间失效）；
  2. AI internal token：同上（轮换 → HistoPilot 旧 token 全部 401）；
  3. ai_secret.key：**已存在**但损坏/为空 → 稳定不可用（None）+ 节流 warning，
     绝不静默重建（重建使全部 enc: 密文永久失效）；不存在 → 创建；
  4. _decrypt_api_key：密钥不可用/解密失败 → ""（稳定不可用）+ 节流 warning，
     不再零日志伪装「AI 未配置」；
  5. _encrypt_api_key：加密失败降级明文时必须告警（类别 + 异常类名）；
  6. _load_ai_config：损坏/不可读/顶层非对象 → {} + 节流 warning；迁移重写
     失败也告警。

日志红线：所有告警只含类别（[secret/config:kind]）与安全路径/异常类名标识，
不得出现密钥、密文、token 内容。同类告警 5 分钟节流（一窗口一条）。

运行：python3 -m pytest tests/test_ai_config_secrets.py -q
"""
import json
import logging
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import _bootstrap  # noqa: E402,F401  # session 目录 + openslide stub（conftest 先行）
os.environ["ADMIN_PASSWORD"] = ""
import pytest  # noqa: E402

import app as app_mod  # noqa: E402


try:
    from cryptography.fernet import Fernet  # noqa: F401
    _HAS_FERNET = True
except Exception:  # pragma: no cover
    _HAS_FERNET = False

fernet_needed = pytest.mark.skipif(
    not _HAS_FERNET, reason="Fernet 链路需 cryptography")


@pytest.fixture(autouse=True)
def _secret_env(tmp_path, monkeypatch):
    """每用例：独立数据目录 + 清空相关 env + 节流状态复位。"""
    monkeypatch.setenv("SHARE_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("SECRET_KEY", raising=False)
    monkeypatch.delenv("HISTOPILOT_INTERNAL_TOKEN", raising=False)
    monkeypatch.delenv("AI_INTERNAL_TOKEN", raising=False)
    monkeypatch.setattr(app_mod, "_secret_warn_last", {})
    return tmp_path


def _records(caplog, kind):
    return [r for r in caplog.records
            if ("[secret/config:%s]" % kind) in r.getMessage()]


def _all_secret_records(caplog):
    return [r for r in caplog.records if "[secret/config:" in r.getMessage()]


# =========================================================================== #
# 1/2. 启动关键 secret：flask secret key / AI internal token
# =========================================================================== #
def test_secret_key_missing_creates_and_is_stable(tmp_path):
    k1 = app_mod._load_or_create_secret_key()
    assert len(k1) == 64 and int(k1, 16) >= 0  # 64 位 hex
    p = tmp_path / "flask_secret.key"
    assert p.is_file()
    assert app_mod._load_or_create_secret_key() == k1  # 重启不轮换
    # 本仓仅支持 POSIX（全仓 fcntl），0600 为硬约定，不做平台兜底
    assert (p.stat().st_mode & 0o777) == 0o600


def test_secret_key_empty_file_exits(tmp_path, caplog):
    (tmp_path / "flask_secret.key").write_text("   \n")
    with caplog.at_level(logging.WARNING):
        with pytest.raises(SystemExit) as ei:
            app_mod._load_or_create_secret_key()
    assert "内容为空" in str(ei.value)
    # fail-fast 不得触碰原文件（空文件不得被轮换覆盖）
    assert (tmp_path / "flask_secret.key").read_text() == "   \n"


def test_secret_key_unreadable_exits(tmp_path, monkeypatch, caplog):
    (tmp_path / "flask_secret.key").write_text("k" * 64)
    real = Path.read_text

    def _deny(self, *a, **kw):
        if self.name == "flask_secret.key":
            raise PermissionError("EACCES（测试注入）")
        return real(self, *a, **kw)

    monkeypatch.setattr(Path, "read_text", _deny)
    with pytest.raises(SystemExit) as ei:
        app_mod._load_or_create_secret_key()
    assert "无法读取" in str(ei.value)
    assert "PermissionError" in str(ei.value)  # 异常类名，不含内容


def test_internal_token_missing_creates(tmp_path):
    t = app_mod._load_or_create_ai_internal_token()
    assert len(t) == 64
    assert (tmp_path / "ai_internal.token").is_file()
    assert app_mod._load_or_create_ai_internal_token() == t


def test_internal_token_empty_file_exits(tmp_path):
    (tmp_path / "ai_internal.token").write_text("")
    with pytest.raises(SystemExit) as ei:
        app_mod._load_or_create_ai_internal_token()
    assert "内容为空" in str(ei.value)


def test_internal_token_unreadable_exits(tmp_path, monkeypatch):
    (tmp_path / "ai_internal.token").write_text("t" * 64)
    real = Path.read_text

    def _deny(self, *a, **kw):
        if self.name == "ai_internal.token":
            raise OSError("EIO（测试注入）")
        return real(self, *a, **kw)

    monkeypatch.setattr(Path, "read_text", _deny)
    with pytest.raises(SystemExit) as ei:
        app_mod._load_or_create_ai_internal_token()
    assert "无法读取" in str(ei.value)


def test_env_secrets_take_precedence(monkeypatch, tmp_path):
    monkeypatch.setenv("SECRET_KEY", "from-env")
    assert app_mod._load_or_create_secret_key() == "from-env"
    monkeypatch.setenv("HISTOPILOT_INTERNAL_TOKEN", "tok-env")
    assert app_mod._load_or_create_ai_internal_token() == "tok-env"
    assert not (tmp_path / "flask_secret.key").exists()


# =========================================================================== #
# 3. ai_secret.key：损坏/为空不静默重建（可降级但必须可观测）
# =========================================================================== #
@fernet_needed
def test_ai_secret_missing_creates(tmp_path):
    f = app_mod._load_or_create_ai_secret()
    assert f is not None
    assert (tmp_path / "ai_secret.key").is_file()


@fernet_needed
def test_ai_secret_corrupt_returns_none_with_warning(tmp_path, caplog):
    (tmp_path / "ai_secret.key").write_bytes(b"not-a-fernet-key")
    with caplog.at_level(logging.WARNING):
        assert app_mod._load_or_create_ai_secret() is None
    recs = _records(caplog, "ai_secret_corrupt")
    assert len(recs) == 1
    # 绝不重建：文件内容原样保留
    assert (tmp_path / "ai_secret.key").read_bytes() == b"not-a-fernet-key"


@fernet_needed
def test_ai_secret_empty_returns_none_with_warning(tmp_path, caplog):
    (tmp_path / "ai_secret.key").write_bytes(b"  \n")
    with caplog.at_level(logging.WARNING):
        assert app_mod._load_or_create_ai_secret() is None
    assert _records(caplog, "ai_secret_empty")


@fernet_needed
def test_ai_secret_unreadable_returns_none_with_warning(tmp_path, monkeypatch, caplog):
    (tmp_path / "ai_secret.key").write_bytes(Fernet.generate_key())
    real = Path.read_bytes

    def _deny(self, *a, **kw):
        if self.name == "ai_secret.key":
            raise PermissionError("EACCES（测试注入）")
        return real(self, *a, **kw)

    monkeypatch.setattr(Path, "read_bytes", _deny)
    with caplog.at_level(logging.WARNING):
        assert app_mod._load_or_create_ai_secret() is None
    assert _records(caplog, "ai_secret_unreadable")


# =========================================================================== #
# 4/5. api_key 加解密：分类降级 + 节流
# =========================================================================== #
@fernet_needed
def test_api_key_roundtrip(tmp_path):
    enc = app_mod._encrypt_api_key("sk-test-plain-key")
    assert enc.startswith("enc:")
    assert app_mod._decrypt_api_key(enc) == "sk-test-plain-key"
    # 明文旧配置原样返回
    assert app_mod._decrypt_api_key("sk-legacy") == "sk-legacy"


@fernet_needed
def test_decrypt_with_corrupt_secret_warns_and_returns_empty(tmp_path, caplog):
    enc = app_mod._encrypt_api_key("sk-secret-material")
    (tmp_path / "ai_secret.key").write_bytes(b"corrupted-key")
    with caplog.at_level(logging.WARNING):
        assert app_mod._decrypt_api_key(enc) == ""
    kinds = {("secret/config:%s]" % k) in " ".join(
        r.getMessage() for r in caplog.records)
        for k in ("ai_secret_corrupt", "api_key_decrypt_unavailable",
                  "api_key_decrypt_failed")}
    assert any(kinds)
    # 日志红线：不得出现密文/明文内容
    logs = " ".join(r.getMessage() for r in caplog.records)
    assert "sk-secret-material" not in logs
    assert enc not in logs


@fernet_needed
def test_decrypt_wrong_key_warns(tmp_path, caplog):
    enc = app_mod._encrypt_api_key("sk-abc")
    (tmp_path / "ai_secret.key").write_bytes(Fernet.generate_key())  # 换钥
    with caplog.at_level(logging.WARNING):
        assert app_mod._decrypt_api_key(enc) == ""
    assert _records(caplog, "api_key_decrypt_failed")


@fernet_needed
def test_encrypt_failure_warns_before_plaintext_fallback(monkeypatch, caplog):
    class _BoomFernet:
        def encrypt(self, data):
            raise RuntimeError("encrypt 崩溃（测试注入）")

    monkeypatch.setattr(app_mod, "_load_or_create_ai_secret",
                        lambda: _BoomFernet())
    with caplog.at_level(logging.WARNING):
        out = app_mod._encrypt_api_key("sk-plain-fallback")
    assert out == "sk-plain-fallback"  # 旧行为保留（generic provider）
    recs = _records(caplog, "api_key_encrypt_failed")
    assert len(recs) == 1
    assert "RuntimeError" in recs[0].getMessage()


def test_throttle_one_record_per_kind_per_window(tmp_path, caplog):
    (tmp_path / "ai_config.json").write_text("{not-json", encoding="utf-8")
    with caplog.at_level(logging.WARNING):
        for _ in range(3):
            assert app_mod._load_ai_config() == {}
    assert len(_records(caplog, "ai_config_corrupt")) == 1  # 节流：只 1 条
    # 窗口重置后再触发 → 再一条
    app_mod._secret_warn_last.clear()
    app_mod._load_ai_config()
    assert len(_records(caplog, "ai_config_corrupt")) == 2


# =========================================================================== #
# 6. _load_ai_config：损坏/形状错误分类告警；正常读取不告警
# =========================================================================== #
def test_ai_config_missing_returns_empty_quietly(tmp_path, caplog):
    with caplog.at_level(logging.WARNING):
        assert app_mod._load_ai_config() == {}
    assert _all_secret_records(caplog) == []


def test_ai_config_unreadable_warns(tmp_path, monkeypatch, caplog):
    p = tmp_path / "ai_config.json"
    p.write_text("{}", encoding="utf-8")
    real = Path.is_file

    def _deny(self, *a, **kw):
        if self.name == "ai_config.json":
            raise PermissionError("EACCES（测试注入）")
        return real(self, *a, **kw)

    monkeypatch.setattr(Path, "is_file", _deny)
    with caplog.at_level(logging.WARNING):
        assert app_mod._load_ai_config() == {}
    assert _records(caplog, "ai_config_unreadable")


@fernet_needed
def test_ai_config_plaintext_migration(tmp_path, caplog):
    (tmp_path / "ai_config.json").write_text(
        json.dumps({"base_url": "http://127.0.0.1:8317/v1",
                    "api_key": "sk-migrate-me"}),
        encoding="utf-8")
    with caplog.at_level(logging.WARNING):
        cfg = app_mod._load_ai_config()
    assert cfg["api_key"] == "sk-migrate-me"
    on_disk = json.loads((tmp_path / "ai_config.json").read_text(encoding="utf-8"))
    assert on_disk["api_key"].startswith("enc:")  # 已加密重写
    logs = " ".join(r.getMessage() for r in caplog.records)
    assert "sk-migrate-me" not in logs  # 日志不含明文 key
    assert _all_secret_records(caplog) == []  # 正常迁移零告警


def test_ai_config_non_dict_top_level_warns(tmp_path, caplog):
    (tmp_path / "ai_config.json").write_text("[]", encoding="utf-8")
    with caplog.at_level(logging.WARNING):
        assert app_mod._load_ai_config() == {}
    assert _records(caplog, "ai_config_shape")


# =========================================================================== #
# 启动 probe 约定：share store 只读 probe 在 import 期已执行（app 可导入即
# 通过）；异常类由 shares fail-closed 批次引入后自动 fail-fast（getattr 兼容）。
# =========================================================================== #
def test_share_store_startup_probe_ran():
    assert app_mod._SHARE_STORE_STARTUP_PROBE is True


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
