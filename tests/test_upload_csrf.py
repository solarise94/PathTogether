# -*- coding: utf-8 -*-
"""上传 CSRF 双层回归——pytest 侧（上传修复 U1 §1.4 / §6；test-review P0-4）。

背景：真实前端 ``uploadFile()`` 曾用裸 XHR 漏传 ``X-CSRF-Token``，服务端
``_csrf_submitted_token`` 又先读 ``request.form``（解析整个 multipart body），
导致 1GB 上传传完才被 400 拒绝。现有 ``CsrfClient`` 包装自动补头，掩盖了该
缺口——本文件用**裸 Flask test client**（不包 CsrfClient）直接验证契约：

  - 有 session、无 token 的 multipart POST /api/upload → 400 csrf_required，
    且在上传 handler 消费 body 之前被拒（reservation 未被调用）；
  - AUTH_ENABLED=True（登录 session）与 False（免认证归一 owner）双覆盖；
  - /api/* 只认 X-CSRF-Token 头：把 token 放进 multipart 表单域不再被接受；
  - 带正确 header 通过 CSRF 层（进入 handler 的业务校验）；
  - HTML 表单路径（/login）仍接受表单域 csrf_token（原生 form 无法带 header）。

vitest 侧（前端真的带头）见 tests/js/upload-csrf.test.ts。
"""
import io
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TMP = tempfile.mkdtemp(prefix="svs-upcsrf-")
DATA_DIR = os.path.join(TMP, "share-data")
UPLOAD_DIR = os.path.join(TMP, "uploads")
os.environ["SHARE_DATA_DIR"] = DATA_DIR
os.makedirs(DATA_DIR, exist_ok=True)
os.environ["UPLOAD_DIR"] = UPLOAD_DIR
os.makedirs(UPLOAD_DIR, exist_ok=True)
os.environ["ADMIN_PASSWORD"] = ""

# openslide 未安装时 stub（本测试不触真实切片解析）
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

import share_store  # noqa: E402
import user_store  # noqa: E402
import app as app_mod  # noqa: E402
from _pt_helpers import install_json_login_limits  # noqa: E402


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    """每用例：临时目录夺回 + 清空用户存储 + 归属注入清空。"""
    data_dir = Path(DATA_DIR)
    monkeypatch.setenv("SHARE_DATA_DIR", DATA_DIR)
    monkeypatch.setattr(user_store, "SHARE_DATA_DIR", data_dir)
    monkeypatch.setattr(user_store, "USER_FILE", data_dir / "users.json")
    monkeypatch.setattr(share_store, "SHARE_DATA_DIR", data_dir)
    monkeypatch.setattr(share_store, "SHARE_FILE", data_dir / "shares.json")
    share_store.set_owner_user_id("")
    install_json_login_limits(monkeypatch)
    for name in ("users.json", "users.json.bak", "shares.json", "shares.json.bak"):
        p = data_dir / name
        if p.exists():
            p.unlink()
    yield


def _bare_client(auth=True):
    """裸 Flask test client：**不**包 CsrfClient（CSRF 拒绝语义的核心前提）。"""
    app_mod.app.config["TESTING"] = True
    app_mod.AUTH_ENABLED = auth
    return app_mod.app.test_client()


def _token_from(client):
    c = client.get_cookie("csrf_token", domain="localhost", path="/")
    return c.value if c is not None else None


def _multipart(token=None, with_file=True):
    """构造 multipart body：可选表单域 token / 可选 file 字段。"""
    data = {}
    if token is not None:
        data["csrf_token"] = token
    if with_file:
        data["file"] = (io.BytesIO(b"fake-slide-bytes"), "a.svs")
    return data


def _post_upload(client, data):
    return client.post("/api/upload", data=data,
                       content_type="multipart/form-data")


# =========================================================================== #
# 1. 无 token 的 multipart POST /api/upload → 400 csrf_required（body 消费前）
# =========================================================================== #
def test_upload_no_token_rejected_auth_enabled(monkeypatch):
    """AUTH_ENABLED=True：登录 session 下无 token → 400，且 handler 未运行。"""
    owner = user_store.create_user("owner@x.com", "ownerpass123456", role="owner")
    share_store.set_owner_user_id(owner["user_id"])
    client = _bare_client(auth=True)
    with client.session_transaction() as s:
        s.update({"auth_user": "o", "user_id": owner["user_id"], "role": "owner",
                  "auth_version": owner.get("auth_version", 1)})
    client.get("/")  # 安全方法绑定 CSRF token 到 session（镜像 cookie）
    assert _token_from(client), "GET / 后应下发 csrf_token cookie"

    # 哨兵：上传 handler 的配额预占入口若被调到，说明 body 已被消费
    calls = []

    def _sentinel(*a, **kw):
        calls.append(1)
        return None
    monkeypatch.setattr(app_mod, "_upload_acquire_reservation", _sentinel)

    r = _post_upload(client, _multipart())
    assert r.status_code == 400, r.get_data(as_text=True)
    assert r.get_json()["error"] == "csrf_required"
    assert not calls, "CSRF 拒绝必须发生在上传 handler 消费 body 之前"


def test_upload_no_token_rejected_auth_disabled():
    """AUTH_ENABLED=False：免认证归一 owner 下同样走 CSRF 钩子 → 400。"""
    client = _bare_client(auth=False)
    client.get("/")
    assert _token_from(client), "免认证模式 GET / 同样应下发 csrf_token cookie"
    r = _post_upload(client, _multipart())
    assert r.status_code == 400, r.get_data(as_text=True)
    assert r.get_json()["error"] == "csrf_required"


# =========================================================================== #
# 2. /api/* header-only 契约：表单域 token 不再被接受
# =========================================================================== #
def test_upload_form_field_token_not_accepted_on_api():
    """multipart 表单域 csrf_token（值正确）但无 header → 仍 400。

    U1 契约：/api/* 只认 X-CSRF-Token 头。若这里回退 request.form，无 token 的
    大文件 multipart 会先整体解析 body 才被拒绝。
    """
    client = _bare_client(auth=False)
    client.get("/")
    tok = _token_from(client)
    assert tok
    r = _post_upload(client, _multipart(token=tok))
    assert r.status_code == 400, r.get_data(as_text=True)
    assert r.get_json()["error"] == "csrf_required"


def test_upload_header_token_passes_csrf_layer():
    """裸 client 手工带 X-CSRF-Token → 通过 CSRF 层（进入 handler 业务校验）。"""
    client = _bare_client(auth=False)
    client.get("/")
    tok = _token_from(client)
    assert tok
    # 无 file 字段：CSRF 通过后 handler 返回业务 400「缺少 file 字段」
    r = client.post("/api/upload", data={},
                    content_type="multipart/form-data",
                    headers={"X-CSRF-Token": tok})
    assert r.status_code == 400, r.get_data(as_text=True)
    assert r.get_json()["error"] == "缺少 file 字段"


# =========================================================================== #
# 3. HTML 表单路径保留表单域回退（/login 原生 form 无法带自定义 header）
# =========================================================================== #
def test_login_form_field_csrf_still_accepted():
    """POST /login 只带表单域 csrf_token（无 header）→ 通过 CSRF（错误密码 401）。"""
    user_store.create_user("o@x.com", "ownerpass123456", role="owner")
    client = _bare_client(auth=True)
    client.get("/login")
    tok = _token_from(client)
    assert tok
    r = client.post("/login", data={
        "csrf_token": tok, "username": "o@x.com", "password": "wrong-password-1"})
    # CSRF 通过、凭据错误 → 401 invalid（而非 400 csrf）
    assert r.status_code == 401, r.get_data(as_text=True)
