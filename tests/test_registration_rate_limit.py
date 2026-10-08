# -*- coding: utf-8 -*-
"""P0-B 注册限流测试（docs §4.5 / §6.1 限流条目）。

覆盖（仅 RUN_PG_TESTS=1 真跑，PG 权威）：
  - 每 IP 前缀 15 分钟 10 次失败 → 锁定（429 + Retry-After）；
  - 每 IP 前缀 24 小时 30 次**尝试**（成功也计）→ 锁定；
  - 每 invite token_hash 15 分钟 5 次失败 → 短时锁定（换 IP 也锁）；
  - owner 创建邀请码每分钟 / 每日上限；
  - 存储不可用 → POST /register 503 fail-closed（不退化进程内计数）；
  - 路由层：invite_only 下连打失败兑换，达到 IP 短窗阈值后 429，统一错误文案。
"""
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import _bootstrap  # noqa: E402,F401  # session 目录+openslide stub（conftest 先行）
DATA_DIR = _bootstrap.SHARE_DATA_DIR
UPLOAD_DIR = _bootstrap.UPLOAD_DIR
import pytest  # noqa: E402

pytest.importorskip("pgserver")
pytest.importorskip("psycopg")

import auth_limit_store  # noqa: E402
import platform_features  # noqa: E402
import registration_store  # noqa: E402
import settings_store  # noqa: E402
import user_store  # noqa: E402
import app as app_mod  # noqa: E402
from pg_compat import BACKEND  # noqa: E402
from _pt_helpers import isolate_app  # noqa: E402
from _pt_helpers import csrf_client  # noqa: E402


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    isolate_app(monkeypatch, tmp_path)
    monkeypatch.delenv("PUBLIC_BASE_URL", raising=False)
    app_mod.app.config["TESTING"] = True
    monkeypatch.setattr(app_mod, "AUTH_ENABLED", True)
    yield


def _ip_hash(ip):
    return app_mod._ip_prefix_hash(ip)


def _mk_owner():
    return user_store.create_user("rl-owner@x.com", "ownerpass123456", role="owner")


def _client():
    return csrf_client(app_mod.app.test_client())


def _enable_public_mode(monkeypatch):
    """public 生效态（2026-10-08 §4 唯一开放模式；含双文稿前置）。"""
    import agreement_store
    monkeypatch.setattr(platform_features, "STORAGE_BACKEND", "postgres")
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://path.example.com")
    monkeypatch.setenv("ADMIN_SESSION_COOKIE_SECURE", "1")
    monkeypatch.setenv("SECRET_KEY", "rl-secret")
    monkeypatch.setenv("REGISTRATION_MAIL_PAYLOAD_KEY", "rl-key")
    monkeypatch.setenv("REGISTRATION_MAIL_SENDER", "agent_mail_cli")
    monkeypatch.setenv("REGISTRATION_AGENT_MAIL_CLI", "/usr/bin/true")
    monkeypatch.setenv("REGISTRATION_ADMIN_EMAIL", "admin@x.com")
    agreement_store.ensure_builtin_documents()
    for dt in ("user_agreement", "research_sharing"):
        doc = [d for d in agreement_store.builtin_documents()
               if d["document_type"] == dt][0]
        agreement_store.publish_document(dt, doc["version"])
    settings_store.set_registration_mode("public", updated_by="t")


# --------------------------------------------------------------------------- #
# 数据层：三桶 + owner 创建频率
# --------------------------------------------------------------------------- #
def test_ip_short_failure_bucket_locks():
    ip = _ip_hash("203.0.113.9")
    for i in range(auth_limit_store.REG_IP_SHORT_FAILURE_LIMIT - 1):
        retry = auth_limit_store.record_registration_failure(ip, None)
        assert retry == 0
    # 达到阈值的那次失败触发锁定
    retry = auth_limit_store.record_registration_failure(ip, None)
    assert retry > 0
    assert auth_limit_store.check_registration_locked(ip, None) > 0
    # 同 /24 其他 IP 共享前缀桶
    assert auth_limit_store.check_registration_locked(
        _ip_hash("203.0.113.200"), None) > 0
    # 不同前缀不受影响
    assert auth_limit_store.check_registration_locked(
        _ip_hash("198.51.100.1"), None) == 0


def test_ip_daily_attempt_bucket_locks():
    """24 小时 30 次**尝试**（成功也计）：第 30 次触发锁定。"""
    ip = _ip_hash("198.51.100.7")
    limit = auth_limit_store.REG_IP_DAILY_ATTEMPT_LIMIT
    for i in range(limit - 1):
        assert auth_limit_store.record_registration_attempt(ip) == 0
    assert auth_limit_store.record_registration_attempt(ip) > 0
    assert auth_limit_store.check_registration_locked(ip, None) > 0


def test_invite_hash_bucket_locks_independent_of_ip():
    """invite 桶（auth_limit_store 存储能力；2026-10-08 §4 起 /register 不
    再传 invite hash，桶语义保留）。"""
    ih = "deadbeef" * 8  # 任意带盐 hash 形态
    # 5 次失败（不同 IP 前缀）也累计到 invite 桶
    for i in range(auth_limit_store.REG_INVITE_FAILURE_LIMIT):
        retry = auth_limit_store.record_registration_failure(
            _ip_hash("192.0.2.%d" % (i + 1)), ih)
    assert retry > 0
    # 换全新 IP：invite 桶仍锁
    assert auth_limit_store.check_registration_locked(
        _ip_hash("203.0.113.1"), ih) > 0
    # 不带 invite 桶（空 hash）不受影响
    assert auth_limit_store.check_registration_locked(
        _ip_hash("203.0.113.1"), None) == 0


def test_owner_invite_creation_rate_limits(monkeypatch):
    monkeypatch.setattr(auth_limit_store, "REG_OWNER_CREATE_PER_MINUTE", 3)
    owner_hash = "ow-hash-1"
    for _ in range(2):
        assert auth_limit_store.record_owner_invite_creation(owner_hash) == 0
    assert auth_limit_store.record_owner_invite_creation(owner_hash) > 0
    assert auth_limit_store.check_owner_invite_creation_locked(owner_hash) > 0
    # 其他 owner 不受影响
    assert auth_limit_store.check_owner_invite_creation_locked(
        "ow-hash-2") == 0


def test_subject_hashes_store_no_plaintext():
    """计数 subject 只存带盐 hash：库内无明文 IP。"""
    ip = "203.0.113.77"
    auth_limit_store.record_registration_failure(_ip_hash(ip), "")
    import psycopg
    import pg_store
    conn = pg_store.connect()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT scope, subject_hash FROM auth_rate_limits "
                        "WHERE scope LIKE 'reg_%'")
            rows = cur.fetchall()
    finally:
        conn.close()
    assert rows
    blob = repr(rows)
    assert "203.0.113" not in blob


# --------------------------------------------------------------------------- #
# 路由层：fail-closed 与 429
# --------------------------------------------------------------------------- #
def test_register_503_when_limit_store_unavailable(monkeypatch):
    """PG 权威限流存储不可用 → POST /register 503（不退化进程内计数；
    2026-10-08 §4 后以 public 生效态触发，限流闸先于表单校验）。"""
    _enable_public_mode(monkeypatch)

    def boom(*a, **k):
        raise RuntimeError("pg down")

    monkeypatch.setattr(auth_limit_store, "check_registration_locked", boom)
    client = _client()
    r = client.post("/register", data={"email": "n@x.com"})
    assert r.status_code == 503
    assert r.get_json()["code"] == "registration_unavailable"
    assert user_store.get_user_by_login_id("n@x.com") is None


def test_register_daily_attempt_429(monkeypatch):
    """24h 30 次尝试桶：成功尝试也计数，达阈值后锁定（public 模式同口径）。"""
    _enable_public_mode(monkeypatch)
    # 预先造 29 次尝试（直接记桶；同 test client IP 127.0.0.1）
    ip_hash = _ip_hash("127.0.0.1")
    limit = auth_limit_store.REG_IP_DAILY_ATTEMPT_LIMIT
    for _ in range(limit - 1):
        auth_limit_store.record_registration_attempt(ip_hash)
    # 第 30 次尝试（POST /register）触发锁定 → 本次响应 429
    client = _client()
    r = client.post("/register", data={"email": "n@x.com"})
    assert r.status_code == 429


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
