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

TMP = tempfile.mkdtemp(prefix="svs-p0b-rl-")
DATA_DIR = os.path.join(TMP, "share-data")
UPLOAD_DIR = os.path.join(TMP, "uploads")
os.environ["SHARE_DATA_DIR"] = DATA_DIR
os.environ["UPLOAD_DIR"] = UPLOAD_DIR
os.makedirs(DATA_DIR, exist_ok=True)
os.makedirs(UPLOAD_DIR, exist_ok=True)
os.environ["ADMIN_PASSWORD"] = ""

try:
    import openslide  # noqa: F401
except ImportError:
    import types as _types
    _os = _types.ModuleType("openslide")
    _os.OpenSlide = object
    sys.modules["openslide"] = _os
    _dz = _types.ModuleType("openslide.deepzoom")
    _dz.DeepZoomGenerator = object
    sys.modules["openslide.deepzoom"] = _dz

import pytest  # noqa: E402

pytest.importorskip("pgserver")
pytest.importorskip("psycopg")

import auth_limit_store  # noqa: E402
import platform_features  # noqa: E402
import registration_store  # noqa: E402
import user_store  # noqa: E402
import app as app_mod  # noqa: E402
from pg_compat import BACKEND  # noqa: E402
from _pt_helpers import csrf_client  # noqa: E402

pytestmark = pytest.mark.skipif(
    BACKEND != "postgres",
    reason="注册限流需 PG 权威计数（RUN_PG_TESTS=1）",
)


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    monkeypatch.delenv("PUBLIC_BASE_URL", raising=False)
    app_mod.app.config["TESTING"] = True
    app_mod.AUTH_ENABLED = True
    yield


def _ip_hash(ip):
    return app_mod._ip_prefix_hash(ip)


def _invite_hash(token):
    return registration_store.invite_token_hash(token)


def _mk_owner():
    return user_store.create_user("rl-owner@x.com", "ownerpass1", role="owner")


def _client():
    return csrf_client(app_mod.app.test_client())


def _enable_invite_mode(monkeypatch):
    monkeypatch.setattr(app_mod.settings_store, "get_registration_mode",
                        lambda: "invite_only")
    monkeypatch.setattr(platform_features, "STORAGE_BACKEND", "postgres")
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://path.example.com")
    monkeypatch.setenv("ADMIN_SESSION_COOKIE_SECURE", "1")


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
    tok = "some-invite-token"
    ih = _invite_hash(tok)
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
    """计数 subject 只存带盐 hash：库内无明文 IP / token。"""
    ip = "203.0.113.77"
    tok = "secret-invite-token"
    auth_limit_store.record_registration_failure(_ip_hash(ip),
                                                 _invite_hash(tok))
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
    assert tok not in blob


# --------------------------------------------------------------------------- #
# 路由层：fail-closed 与 429
# --------------------------------------------------------------------------- #
def test_register_503_when_limit_store_unavailable(monkeypatch):
    """PG 权威限流存储不可用 → POST /register 503（不退化进程内计数）。"""
    _enable_invite_mode(monkeypatch)

    def boom(*a, **k):
        raise RuntimeError("pg down")

    monkeypatch.setattr(auth_limit_store, "check_registration_locked", boom)
    client = _client()
    r = client.post("/register", data={
        "invite_token": "whatever", "email": "n@x.com",
        "password": "longpassword1", "password_confirm": "longpassword1"})
    assert r.status_code == 503
    assert r.get_json()["code"] == "registration_unavailable"
    assert user_store.get_user_by_email("n@x.com") is None


def test_register_ip_short_window_429(monkeypatch):
    """invite_only 下同一 IP 连打 10 次失败兑换（每次不同随机 token，避免
    先触发 invite 桶）→ 第 11 次 429。"""
    _enable_invite_mode(monkeypatch)
    client = _client()
    limit = auth_limit_store.REG_IP_SHORT_FAILURE_LIMIT
    for i in range(limit):
        data = {"invite_token": "no-such-token-%d" % i, "email": "n@x.com",
                "password": "longpassword1",
                "password_confirm": "longpassword1"}
        r = client.post("/register", data=data)
        assert r.status_code == 403, r.status_code
        assert "邀请码无效或当前不可用" in r.get_data(as_text=True)
    r11 = client.post("/register", data={
        "invite_token": "no-such-token-final", "email": "n@x.com",
        "password": "longpassword1", "password_confirm": "longpassword1"})
    assert r11.status_code == 429
    assert int(r11.headers.get("Retry-After") or 0) > 0
    # 锁定期内即使表单形状错误也直接 429（闸在表单校验之前）
    r12 = client.post("/register", data={"invite_token": ""})
    assert r12.status_code == 429


def test_register_invite_hash_lockout_429(monkeypatch):
    """同一无效邀请码 5 次失败（不同 IP）→ invite 桶锁，429。"""
    _enable_invite_mode(monkeypatch)
    client = _client()
    tok = "same-bad-token"
    limit = auth_limit_store.REG_INVITE_FAILURE_LIMIT
    for i in range(limit):
        r = client.post("/register", data={
            "invite_token": tok, "email": "n@x.com",
            "password": "longpassword1", "password_confirm": "longpassword1"},
            environ_overrides={"REMOTE_ADDR": "192.0.2.%d" % (i + 1)})
        assert r.status_code == 403, r.status_code
    # 第 6 次（再换 IP）：invite 桶已锁
    r = client.post("/register", data={
        "invite_token": tok, "email": "n@x.com",
        "password": "longpassword1", "password_confirm": "longpassword1"},
        environ_overrides={"REMOTE_ADDR": "198.51.100.9"})
    assert r.status_code == 429


def test_register_daily_attempt_429(monkeypatch):
    """24h 30 次尝试桶：即使兑换成功也计数，达阈值后锁定。"""
    _enable_invite_mode(monkeypatch)
    owner = _mk_owner()
    # 预先造 29 次尝试（直接记桶；同 test client IP 127.0.0.1）
    ip_hash = _ip_hash("127.0.0.1")
    limit = auth_limit_store.REG_IP_DAILY_ATTEMPT_LIMIT
    for _ in range(limit - 1):
        auth_limit_store.record_registration_attempt(ip_hash)
    # 第 30 次尝试（POST /register）触发锁定 → 本次响应 429
    client = _client()
    r = client.post("/register", data={
        "invite_token": "x", "email": "n@x.com",
        "password": "longpassword1", "password_confirm": "longpassword1"})
    assert r.status_code == 429


def test_owner_invite_create_rate_limited_via_api(monkeypatch):
    _enable_invite_mode(monkeypatch)
    monkeypatch.setattr(auth_limit_store, "REG_OWNER_CREATE_PER_MINUTE", 2)
    owner = _mk_owner()
    client = _client()
    with client.session_transaction() as s:
        s.update({"auth_user": "o", "user_id": owner["user_id"],
                  "role": "owner"})
    for _ in range(2):
        r = client.post("/api/admin/registration-invites", json={})
        assert r.status_code == 200
    r3 = client.post("/api/admin/registration-invites", json={})
    assert r3.status_code == 429
    assert r3.get_json()["code"] == "rate_limited"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
