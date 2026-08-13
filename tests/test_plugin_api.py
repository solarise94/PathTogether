# -*- coding: utf-8 -*-
"""Stage 4-1a：插件安装凭证 + scoped JWT + /api/plugin/v1（平台侧）测试。

覆盖：
  - 引导：histopilot 安装幂等创建、secret 文件 0600、已存在不重建不覆盖、
    env PLUGIN_HISTOPILOT_SECRET 优先；
  - token 端点：secret 换 JWT 成功 / 错 secret 401 / 缺参 400 / rotate 后旧
    secret 立即失效；
  - scoped JWT 守卫：无 token / 坏 token / 错签名 / 过期（401 token_expired,
    retryable=true）/ scope 不足 403 / disable 后旧 token 立即 401；
  - plugin v1 各端点 happy path（slide info / region+Content-SHA256 / changes /
    annotate+X-Run-Grant）与统一错误信封形状；
  - run grant：起跑自动发放落库 + config 注入、verify 端点、撤销后 annotate
    403、slide 不匹配 403、过期 403。

json / pg 双后端通用（RUN_PG_TESTS=1 时 conftest 已切 postgres 并逐用例
TRUNCATE）。运行：cd 项目根 && python3 -m pytest tests/test_plugin_api.py -q
"""
import base64
import hashlib
import json
import os
import stat
import sys
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TMP = tempfile.mkdtemp(prefix="svs-plugin-")
os.environ["SHARE_DATA_DIR"] = os.path.join(TMP, "share-data")
os.makedirs(os.environ["SHARE_DATA_DIR"], exist_ok=True)
os.environ["UPLOAD_DIR"] = os.path.join(TMP, "uploads")
os.makedirs(os.environ["UPLOAD_DIR"], exist_ok=True)
os.environ["AI_SIDECAR_URL"] = "http://127.0.0.1:8055"

# openslide 未安装时 stub（本测试不需要真 OpenSlide）
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

import app as app_mod  # noqa: E402
import share_store  # noqa: E402

app_mod.UPLOAD_DIR = Path(os.environ["UPLOAD_DIR"])
app_mod.UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

import pytest  # noqa: E402


# --------------------------------------------------------------------------- #
# 隔离 + 引导
# --------------------------------------------------------------------------- #
@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    """每用例独立存储 + 数据目录（json/pg 双后端通用）。"""
    monkeypatch.setenv("SHARE_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(app_mod, "AUTH_ENABLED", False)
    # json 后端：SHARE_FILE 指到本用例目录（dispatcher setattr 自动镜像进实现）
    monkeypatch.setattr(share_store, "SHARE_DATA_DIR", tmp_path)
    monkeypatch.setattr(share_store, "SHARE_FILE", tmp_path / "shares.json")
    yield


def _bootstrap():
    """引导 histopilot 安装并注入 app 模块级引用（返回安装 dict）。"""
    inst = app_mod._bootstrap_plugin_installations()
    assert inst is not None, "引导应成功"
    app_mod._HISTOPILOT_INSTALLATION = inst
    return inst


def _secret_file():
    return Path(os.environ["SHARE_DATA_DIR"]) / "plugin-secret-histopilot.txt"


def _file_secret():
    raw = _secret_file().read_text(encoding="utf-8").strip()
    # 4-1b 起凭证文件为 JSON {installation_id, secret}；旧格式整行即明文 secret。
    try:
        obj = json.loads(raw)
        if isinstance(obj, dict):
            return str(obj.get("secret") or "")
    except (ValueError, TypeError):
        pass
    return raw


def _client():
    app_mod.app.config["TESTING"] = True
    return app_mod.app.test_client()


def _owner_session(client, user_id="usr_owner_test"):
    """注入 owner session（AUTH_ENABLED=False 时 _require_owner 仍看 session role）。"""
    with client.session_transaction() as s:
        s["role"] = "owner"
        s["user_id"] = user_id
        s["auth_user"] = "owner-test"


def _bearer(token):
    return {"Authorization": "Bearer " + token}


def _assert_envelope(r, status, code, retryable=None):
    """统一错误信封形状断言：{error:{code,message,retryable}}，无其它顶层键。"""
    assert r.status_code == status, "got %s body=%r" % (r.status_code, r.get_json())
    body = r.get_json() or {}
    assert set(body.keys()) == {"error"}, "顶层键应为 error only: %r" % body
    err = body["error"]
    assert set(err.keys()) >= {"code", "message", "retryable"}
    assert err["code"] == code, "code=%r full=%r" % (err.get("code"), err)
    assert isinstance(err["message"], str) and err["message"]
    assert isinstance(err["retryable"], bool)
    if retryable is not None:
        assert err["retryable"] is retryable


def _exchange(client, installation_id, secret, expect=200):
    """secret → access_token；expect 非 200 时返回响应本身。"""
    r = client.post("/api/plugin/v1/auth/token",
                    json={"installation_id": installation_id, "secret": secret})
    assert r.status_code == expect, "got %s body=%r" % (r.status_code, r.get_json())
    if expect == 200:
        body = r.get_json()
        assert body["token_type"] == "bearer"
        assert body["expires_in"] == 900
        return body["access_token"]
    return r


def _touch_slide(name="demo.svs"):
    path = app_mod.UPLOAD_DIR / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"svs-stub")
    return name


@contextmanager
def _borrow_pair_ctx(pair):
    yield pair


_FAKE_ENTRY = {"pool": None, "sem": None}


def _slide_read_mocks(osr=None, mpp=0.5):
    """slide 读路径 mock 栈（dimensions + mpp）。用法：with _slide_read_mocks(): ..."""
    pair = {"osr": osr or mock.Mock(dimensions=(1000, 2000))}
    return mock.patch.object(app_mod, "_get_slide", return_value=_FAKE_ENTRY), \
        mock.patch.object(app_mod.slide_cache, "borrow_pair",
                          side_effect=lambda _e: _borrow_pair_ctx(pair)), \
        mock.patch.object(app_mod, "_read_metadata", return_value={"mpp_x": mpp})


# --------------------------------------------------------------------------- #
# 1. 安装引导
# --------------------------------------------------------------------------- #
def test_bootstrap_creates_installation_and_secret_file(tmp_path):
    inst = _bootstrap()
    assert inst["installation_id"].startswith("pin_")
    assert inst["plugin_id"] == "histopilot"
    assert inst["enabled"] is True
    assert "secret" not in inst and "secret_hash" not in inst
    # 明文 secret 落盘 0600
    f = _secret_file()
    assert f.is_file()
    mode = stat.S_IMODE(f.stat().st_mode)
    assert mode == 0o600, "secret 文件应 0600，got %o" % mode
    # 文件明文能换 token（文件即 sidecar 4-1b 读取的凭证）
    token = _exchange(_client(), inst["installation_id"], _file_secret())
    assert token.count(".") == 2


def test_bootstrap_idempotent_keeps_secret():
    inst1 = _bootstrap()
    f = _secret_file()
    content1 = _file_secret()
    mtime1 = f.stat().st_mtime_ns
    time.sleep(0.01)
    inst2 = _bootstrap()
    assert inst2["installation_id"] == inst1["installation_id"]
    assert _file_secret() == content1, "已存在的 secret 文件不得重建/覆盖"
    assert f.stat().st_mtime_ns == mtime1, "secret 文件不应被重写"
    assert len(share_store.list_plugin_installations()) == 1


def test_bootstrap_env_secret_wins_over_file(tmp_path):
    env_secret = "env-secret-abcdef123456"
    os.environ["PLUGIN_HISTOPILOT_SECRET"] = env_secret
    try:
        inst = _bootstrap()
        assert not _secret_file().exists(), "env 显式配置时不写 secret 文件"
        # env secret 可换 token
        _exchange(_client(), inst["installation_id"], env_secret)
        # 文件 secret（不存在）不能误配
        r = _client().post("/api/plugin/v1/auth/token",
                           json={"installation_id": inst["installation_id"],
                                 "secret": "wrong"})
        _assert_envelope(r, 401, "unauthorized")
    finally:
        del os.environ["PLUGIN_HISTOPILOT_SECRET"]


def test_bootstrap_adopts_existing_secret_file(tmp_path):
    # 先有文件、无安装行：引导采用文件内容，不重建不覆盖
    _secret_file().parent.mkdir(parents=True, exist_ok=True)
    _secret_file().write_text("pre-existing-secret-xyz", encoding="utf-8")
    mtime1 = _secret_file().stat().st_mtime_ns
    inst = _bootstrap()
    assert _file_secret() == "pre-existing-secret-xyz"
    assert _secret_file().stat().st_mtime_ns == mtime1
    _exchange(_client(), inst["installation_id"], "pre-existing-secret-xyz")


# --------------------------------------------------------------------------- #
# 2. token 端点 + rotate
# --------------------------------------------------------------------------- #
def test_token_exchange_success_and_wrong_secret():
    inst = _bootstrap()
    secret = _file_secret()
    token = _exchange(_client(), inst["installation_id"], secret)
    # payload 解码：iss/aud/sub/plugin_id/scope/jti
    payload_b64 = token.split(".")[1]
    pad = "=" * (-len(payload_b64) % 4)
    payload = __import__("json").loads(base64.urlsafe_b64decode(payload_b64 + pad))
    assert payload["iss"] == "pathtogether"
    assert payload["aud"] == "plugin"
    assert payload["sub"] == inst["installation_id"]
    assert payload["plugin_id"] == "histopilot"
    assert "slide:read" in payload["scope"].split()
    assert "annotation:write" in payload["scope"].split()
    assert payload["exp"] > payload["iat"] and payload["jti"]
    # 错 secret → 401 unauthorized 信封
    r = _exchange(_client(), inst["installation_id"], "wrong-secret", expect=401)
    _assert_envelope(r, 401, "unauthorized", retryable=False)


def test_token_exchange_missing_fields():
    _bootstrap()
    r = _client().post("/api/plugin/v1/auth/token", json={"installation_id": "pin_x"})
    _assert_envelope(r, 400, "invalid_request")


def test_rotate_secret_invalidates_old_immediately():
    inst = _bootstrap()
    client = _client()
    _owner_session(client)
    old_secret = _file_secret()
    _exchange(client, inst["installation_id"], old_secret)  # 旧可用
    r = client.post("/api/admin/plugins/%s/rotate-secret" % inst["installation_id"])
    assert r.status_code == 200
    new_secret = r.get_json()["secret"]
    assert new_secret and new_secret != old_secret
    # 旧 secret 立即失效
    _exchange(client, inst["installation_id"], old_secret, expect=401)
    # 新 secret 可用
    _exchange(client, inst["installation_id"], new_secret)
    # rotate 不改 secret 文件（文件只属引导；轮换后靠 API 分发）
    assert _file_secret() == old_secret


def test_admin_plugins_list_and_toggle():
    inst = _bootstrap()
    client = _client()
    _owner_session(client)
    r = client.get("/api/admin/plugins")
    assert r.status_code == 200
    items = r.get_json()["installations"]
    assert len(items) == 1
    assert items[0]["installation_id"] == inst["installation_id"]
    # health 为 sidecar 可达性快照（reachable/unreachable），不再是占位 unknown
    assert items[0]["health"] in ("reachable", "unreachable")
    # disable → token 立即失效；enable → 恢复
    secret = _file_secret()
    token = _exchange(client, inst["installation_id"], secret)
    assert client.post("/api/admin/plugins/%s/disable" % inst["installation_id"]).status_code == 200
    r = client.get("/api/plugin/v1/slides/%s" % _touch_slide(), headers=_bearer(token))
    _assert_envelope(r, 401, "unauthorized")
    # disabled 状态下列表可见
    items = client.get("/api/admin/plugins").get_json()["installations"]
    assert items[0]["enabled"] is False and items[0]["disabled_at"]
    assert client.post("/api/admin/plugins/%s/enable" % inst["installation_id"]).status_code == 200
    m1, m2, m3 = _slide_read_mocks()
    with m1, m2, m3:
        r = client.get("/api/plugin/v1/slides/%s" % _touch_slide(), headers=_bearer(token))
        assert r.status_code == 200


def test_admin_plugins_health_probe_mock():
    """平台 /api/admin/plugins 顺带探 sidecar /healthz（mock requests）。"""
    inst = _bootstrap()
    client = _client()
    _owner_session(client)
    # 用 FakeRequests 替换 app.requests 捕获 /healthz 调用；默认 404 → unreachable
    fake = _install_fake_requests()
    fake.register("GET", "/healthz",
                  lambda b, q, h, k: _FakeResponse(200, b'{"ok":true}',
                                                   headers={"Content-Type": "application/json"}))
    items = client.get("/api/admin/plugins").get_json()["installations"]
    assert items[0]["health"] == "reachable"
    # 把 /healthz 关掉 → unreachable
    fake2 = _install_fake_requests()
    fake2.set_unreachable()
    items = client.get("/api/admin/plugins").get_json()["installations"]
    assert items[0]["health"] == "unreachable"


def test_admin_plugins_requires_owner():
    _bootstrap()
    client = _client()
    with client.session_transaction() as s:
        s["role"] = "user"
    r = client.get("/api/admin/plugins")
    assert r.status_code == 403
    r = client.post("/api/admin/plugins/pin_x/rotate-secret")
    assert r.status_code == 403


# --------------------------------------------------------------------------- #
# 3. scoped JWT 守卫
# --------------------------------------------------------------------------- #
def _token_for(inst):
    return _exchange(_client(), inst["installation_id"], _file_secret())


def test_plugin_endpoints_require_bearer():
    _bootstrap()
    slide = _touch_slide()
    client = _client()
    r = client.get("/api/plugin/v1/slides/%s" % slide)
    _assert_envelope(r, 401, "unauthorized")
    r = client.get("/api/plugin/v1/slides/%s" % slide, headers=_bearer("not.a.jwt"))
    _assert_envelope(r, 401, "unauthorized")
    # 错误签名（用另一把 key 签）→ 401
    forged = app_mod._plugin_jwt_encode(
        {"iss": "pathtogether", "aud": "plugin", "sub": "pin_x"},
        key=hashlib.sha256(b"other-key").digest())
    r = client.get("/api/plugin/v1/slides/%s" % slide, headers=_bearer(forged))
    _assert_envelope(r, 401, "unauthorized")


def test_expired_token_401_token_expired_retryable():
    inst = _bootstrap()
    slide = _touch_slide()
    # 伪造短 exp（签名有效、exp 已过）
    token = app_mod._plugin_jwt_encode(
        {"iss": "pathtogether", "aud": "plugin", "sub": inst["installation_id"],
         "plugin_id": "histopilot", "scope": app_mod._PLUGIN_JWT_SCOPES},
        ttl=-60)
    r = _client().get("/api/plugin/v1/slides/%s" % slide, headers=_bearer(token))
    _assert_envelope(r, 401, "token_expired", retryable=True)


def test_scope_insufficient_403():
    inst = _bootstrap()
    slide = _touch_slide()
    token = app_mod._plugin_jwt_encode(
        {"iss": "pathtogether", "aud": "plugin", "sub": inst["installation_id"],
         "plugin_id": "histopilot", "scope": "slide:read"})  # 缺 region:read
    r = _client().post("/api/plugin/v1/slides/%s/regions" % slide,
                       headers=_bearer(token),
                       json={"x": 0, "y": 0, "w": 10, "h": 10})
    _assert_envelope(r, 403, "forbidden", retryable=False)


def test_disabled_installation_invalidates_token_immediately():
    inst = _bootstrap()
    client = _client()
    _owner_session(client)
    slide = _touch_slide()
    token = _token_for(inst)
    client.post("/api/admin/plugins/%s/disable" % inst["installation_id"])
    r = client.get("/api/plugin/v1/slides/%s" % slide, headers=_bearer(token))
    _assert_envelope(r, 401, "unauthorized")
    # secret 本身也对（disabled 时换 token 被拒）
    _exchange(client, inst["installation_id"], _file_secret(), expect=401)


# --------------------------------------------------------------------------- #
# 4. plugin v1 数据端点 happy path + 信封
# --------------------------------------------------------------------------- #
def test_slide_info_happy_path():
    inst = _bootstrap()
    slide = _touch_slide()
    m1, m2, m3 = _slide_read_mocks()
    with m1, m2, m3:
        r = _client().get("/api/plugin/v1/slides/%s" % slide,
                          headers=_bearer(_token_for(inst)))
    assert r.status_code == 200
    body = r.get_json()
    assert body["width"] == 1000 and body["height"] == 2000
    assert body["mpp"] == 0.5
    assert isinstance(body["level_downsamples"], list)
    assert body["fingerprint"] and body["asset_revision"]


def test_slide_info_not_found_envelope():
    inst = _bootstrap()
    r = _client().get("/api/plugin/v1/slides/missing.svs",
                      headers=_bearer(_token_for(inst)))
    _assert_envelope(r, 404, "not_found")
    r = _client().get("/api/plugin/v1/slides/..%2Fetc",
                      headers=_bearer(_token_for(inst)))
    assert r.status_code in (400, 404)


def test_region_happy_path_content_sha256():
    inst = _bootstrap()
    slide = _touch_slide()
    jpeg_bytes = b"\xff\xd8fake-jpeg-bytes\xff\xd9"
    fake_region = {
        "image_base64": base64.b64encode(jpeg_bytes).decode("ascii"),
        "mime": "image/jpeg", "width": 64, "height": 64,
        "src": {"x": 0, "y": 0, "w": 100, "h": 100}, "magnification": 20,
    }
    expected_sha = hashlib.sha256(jpeg_bytes).hexdigest()
    m1, m2, m3 = _slide_read_mocks()
    with m1, m2, m3, \
            mock.patch.object(app_mod, "_read_region_b64", return_value=fake_region):
        r = _client().post(
            "/api/plugin/v1/slides/%s/regions" % slide,
            headers=_bearer(_token_for(inst)),
            json={"bbox": {"x": 0, "y": 0, "w": 100, "h": 100},
                  "max_long_edge": 1568, "jpeg_quality": 85})
    assert r.status_code == 200, r.get_json()
    body = r.get_json()
    assert body["image_base64"] == fake_region["image_base64"]
    assert body["content_sha256"] == expected_sha
    assert r.headers.get("Content-SHA256") == expected_sha
    assert body["encoder"]["jpeg_quality"] == 85
    # 解码后字节 hash 与头一致（4-1b 校验语义）
    assert hashlib.sha256(base64.b64decode(body["image_base64"])).hexdigest() == expected_sha


def test_region_fingerprint_mismatch_409_envelope():
    inst = _bootstrap()
    slide = _touch_slide()
    r = _client().post(
        "/api/plugin/v1/slides/%s/regions" % slide,
        headers=_bearer(_token_for(inst)),
        json={"x": 0, "y": 0, "w": 10, "h": 10, "expected_fingerprint": "mtime:2"})
    _assert_envelope(r, 409, "slide_revision_conflict", retryable=False)
    err = r.get_json()["error"]
    assert err.get("details", {}).get("expected") == "mtime:2"


def test_region_bad_params_400():
    inst = _bootstrap()
    slide = _touch_slide()
    r = _client().post("/api/plugin/v1/slides/%s/regions" % slide,
                       headers=_bearer(_token_for(inst)),
                       json={"x": 0, "y": 0, "w": -1, "h": 10})
    _assert_envelope(r, 400, "invalid_request")


def test_changes_happy_path():
    inst = _bootstrap()
    slide = _touch_slide()
    # 直接经 store 落一条 AI 标注（changes 源头）
    share_store.add_roi(share_store.ADMIN_TOKEN, slide, "seed", type="rect",
                        x=1, y=1, side_px=10, source="ai")
    r = _client().get("/api/plugin/v1/slides/%s/changes" % slide,
                      headers=_bearer(_token_for(inst)))
    assert r.status_code == 200
    body = r.get_json()
    assert body["current_seq"] >= 1
    assert len(body["changes"]) >= 1
    # after_seq 过滤
    r2 = _client().get("/api/plugin/v1/slides/%s/changes?after=%d"
                       % (slide, body["current_seq"]),
                       headers=_bearer(_token_for(inst)))
    assert r2.get_json()["changes"] == []


# --------------------------------------------------------------------------- #
# 5. run grant 生命周期
# --------------------------------------------------------------------------- #
def _grant_headers(inst, grant_id):
    return {"X-Run-Grant": grant_id}


def _make_grant(slide, **kw):
    inst = app_mod._HISTOPILOT_INSTALLATION
    return share_store.create_run_grant(
        installation_id=inst["installation_id"], slide=slide,
        created_by_user_id="usr_creator_1", **kw)


def test_run_start_issues_grant_into_sidecar_config():
    inst = _bootstrap()
    fake = _install_fake_requests()
    _setup_ai_config()
    slide = _touch_slide()

    def handler(body, query, headers, kwargs):
        return _FakeResponse(200, sse_frames=[b"id: 1\nevent: slide_opened\ndata: {}\n\n"])

    fake.register("POST", "/run", handler)
    r = _client().post("/api/ai/run", json={"slide": slide})
    assert r.status_code == 200
    body = fake.calls[-1]["body"]
    rg = body["config"]["run_grant"]
    assert rg["grant_id"].startswith("rgr_")
    assert rg["slide"] == slide
    assert rg["installation_id"] == inst["installation_id"]
    assert rg["expires_at"] > time.time()
    # 落库核对
    grant = share_store.get_run_grant(rg["grant_id"])
    assert grant is not None
    assert grant["slide"] == slide
    assert grant["installation_id"] == inst["installation_id"]


def test_run_grant_verify_endpoint():
    _bootstrap()
    slide = _touch_slide()
    grant = _make_grant(slide)
    token = _token_for(app_mod._HISTOPILOT_INSTALLATION)
    client = _client()
    r = client.post("/api/plugin/v1/run-grants/verify",
                    headers=_bearer(token),
                    json={"grant_id": grant["grant_id"], "slide": slide})
    assert r.status_code == 200
    assert r.get_json() == {"valid": True, "reason": ""}
    # slide 不匹配
    other = _touch_slide("other.svs")
    r = client.post("/api/plugin/v1/run-grants/verify",
                    headers=_bearer(token),
                    json={"grant_id": grant["grant_id"], "slide": other})
    assert r.get_json()["valid"] is False
    assert r.get_json()["reason"] == "slide_mismatch"
    # grant_id 必填
    r = client.post("/api/plugin/v1/run-grants/verify",
                    headers=_bearer(token), json={"slide": slide})
    _assert_envelope(r, 400, "invalid_request")


def test_annotate_requires_valid_run_grant():
    inst = _bootstrap()
    slide = _touch_slide()
    token = _token_for(inst)
    client = _client()
    body = {"label": "AI 病灶", "x": 10, "y": 10, "side_px": 100}
    # 1) 缺 X-Run-Grant
    r = client.post("/api/plugin/v1/slides/%s/annotations" % slide,
                    headers=_bearer(token), json=body)
    _assert_envelope(r, 403, "run_grant_invalid")
    # 2) grant 不存在
    r = client.post("/api/plugin/v1/slides/%s/annotations" % slide,
                    headers={**_bearer(token), **_grant_headers(inst, "rgr_none")},
                    json=body)
    _assert_envelope(r, 403, "run_grant_invalid")
    # 3) slide 不匹配
    grant = _make_grant(slide)
    other = _touch_slide("other.svs")
    r = client.post("/api/plugin/v1/slides/%s/annotations" % other,
                    headers={**_bearer(token), **_grant_headers(inst, grant["grant_id"])},
                    json=body)
    _assert_envelope(r, 403, "run_grant_invalid")


def test_annotate_happy_path_provenance_from_grant():
    inst = _bootstrap()
    slide = _touch_slide()
    token = _token_for(inst)
    grant = _make_grant(slide)
    r = _client().post(
        "/api/plugin/v1/slides/%s/annotations" % slide,
        headers={**_bearer(token), "X-Run-Grant": grant["grant_id"]},
        json={"label": "AI 病灶", "x": 10, "y": 10, "side_px": 100,
              "note": "n", "effect_key": "ek-1", "session_id": "sess_a",
              "run_id": "run_9", "model": "gpt-x",
              "created_by_user_id": "usr_spoofed"})
    assert r.status_code == 200, r.get_json()
    roi = r.get_json()
    assert roi["annotation_id"] and roi["source"] == "ai"
    prov = roi["provenance"]
    # created_by_user_id 从 grant 来，不信任请求体
    assert prov["created_by_user_id"] == "usr_creator_1"
    assert prov["plugin_id"] == "histopilot"
    assert prov["run_id"] == "run_9"
    assert prov["session_id"] == "sess_a"
    assert prov["model"] == "gpt-x"
    assert prov["idempotency_key"] == "ek-1"
    assert prov["slide_asset_revision"]
    # effect_key 幂等：同 key 重试返回同一 annotation_id
    r2 = _client().post(
        "/api/plugin/v1/slides/%s/annotations" % slide,
        headers={**_bearer(token), "X-Run-Grant": grant["grant_id"]},
        json={"label": "AI 病灶", "x": 10, "y": 10, "side_px": 100,
              "effect_key": "ek-1"})
    assert r2.get_json()["annotation_id"] == roi["annotation_id"]


def test_annotate_revoked_and_expired_grant():
    inst = _bootstrap()
    slide = _touch_slide()
    token = _token_for(inst)
    client = _client()
    body = {"label": "AI", "x": 1, "y": 1, "side_px": 10}
    # 撤销（DELETE 端点，owner 归一放行）→ annotate 403
    grant = _make_grant(slide)
    r = client.delete("/api/plugin/v1/run-grants/%s" % grant["grant_id"],
                      headers=_bearer(token))
    assert r.status_code == 200 and r.get_json()["revoked"] is True
    r = client.post("/api/plugin/v1/slides/%s/annotations" % slide,
                    headers={**_bearer(token), "X-Run-Grant": grant["grant_id"]},
                    json=body)
    _assert_envelope(r, 403, "run_grant_invalid")
    # verify 报 revoked
    r = client.post("/api/plugin/v1/run-grants/verify",
                    headers=_bearer(token),
                    json={"grant_id": grant["grant_id"], "slide": slide})
    assert r.get_json() == {"valid": False, "reason": "grant_revoked"}
    # 过期（ttl=1s 后失效）
    exp_grant = _make_grant(slide, ttl_seconds=1)
    time.sleep(1.2)
    r = client.post("/api/plugin/v1/run-grants/verify",
                    headers=_bearer(token),
                    json={"grant_id": exp_grant["grant_id"], "slide": slide})
    assert r.get_json()["reason"] == "grant_expired"
    r = client.post("/api/plugin/v1/slides/%s/annotations" % slide,
                    headers={**_bearer(token), "X-Run-Grant": exp_grant["grant_id"]},
                    json=body)
    _assert_envelope(r, 403, "run_grant_invalid")
    # DELETE 不存在的 grant → 404 信封
    r = client.delete("/api/plugin/v1/run-grants/rgr_none", headers=_bearer(token))
    _assert_envelope(r, 404, "not_found")


def test_run_grant_revoke_permission():
    inst = _bootstrap()
    slide = _touch_slide()
    grant = _make_grant(slide)  # created_by = usr_creator_1
    token = _token_for(inst)
    client = _client()
    # 非创建者、非 owner → 403
    with client.session_transaction() as s:
        s["role"] = "user"
        s["user_id"] = "usr_someone_else"
    r = client.delete("/api/plugin/v1/run-grants/%s" % grant["grant_id"],
                      headers=_bearer(token))
    _assert_envelope(r, 403, "forbidden")
    # 创建者本人 → 可撤销
    with client.session_transaction() as s:
        s["role"] = "user"
        s["user_id"] = "usr_creator_1"
    r = client.delete("/api/plugin/v1/run-grants/%s" % grant["grant_id"],
                      headers=_bearer(token))
    assert r.status_code == 200
    assert share_store.get_run_grant(grant["grant_id"])["revoked"] is True


# --------------------------------------------------------------------------- #
# Fake sidecar（仅 run 起跑发放测试用，方案同 test_ai_proxy）
# --------------------------------------------------------------------------- #
class _FakeResponse:
    def __init__(self, status_code=200, content=b"", headers=None,
                 sse_frames=None, ctype=None):
        self.status_code = status_code
        if sse_frames is not None:
            self._sse_frames = list(sse_frames)
            self.content = b"".join(sse_frames)
            self.headers = {"Content-Type": ctype or "text/event-stream"}
            if headers:
                self.headers.update(headers)
        else:
            self._sse_frames = None
            self.content = content
            self.headers = dict(headers or {})
            self.headers.setdefault("Content-Type", ctype or "application/json")

    def iter_content(self, chunk_size=4096):
        if self._sse_frames is None:
            yield self.content
            return
        for frame in self._sse_frames:
            yield frame

    def close(self):
        pass


class _FakeRequests:
    ConnectionError = __import__("requests").ConnectionError
    Timeout = __import__("requests").Timeout

    def __init__(self):
        self._routes = {}
        self.calls = []
        self._next_error = None

    def set_unreachable(self):
        self._next_error = True

    def register(self, method, path, handler):
        self._routes[(method.upper(), path)] = handler

    def _dispatch(self, method, url, **kwargs):
        base = app_mod.AI_SIDECAR_URL
        path = url[len(base):] if url.startswith(base) else url
        self.calls.append({"method": method, "path": path,
                           "body": kwargs.get("json"), "query": kwargs.get("params")})
        if self._next_error:
            raise _FakeRequests.ConnectionError("sidecar down (test)")
        handler = self._routes.get((method, path))
        if handler is None:
            return _FakeResponse(404, b'{"error":"no route"}')
        return handler(kwargs.get("json"), kwargs.get("params"),
                       kwargs.get("headers") or {}, kwargs)

    def get(self, url, **kwargs):
        return self._dispatch("GET", url, **kwargs)

    def post(self, url, **kwargs):
        return self._dispatch("POST", url, **kwargs)


def _install_fake_requests():
    fake = _FakeRequests()
    app_mod.requests = fake
    return fake


def _setup_ai_config(plain_key="sk-plugin-test-123456"):
    app_mod._save_ai_config({
        "base_url": "http://llm.example/v1",
        "api_key": plain_key,
        "model": "gpt-test",
        "api_protocol": "openai",
    })
    return plain_key


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))


def test_healthz_no_auth_required():
    """/healthz 健康检查必须免鉴权（demo 实测被 _require_auth 302 到 /login，
    探活全挂——Stage 4-3 review 修复回归）。"""
    c = _client()
    r = c.get("/healthz")
    assert r.status_code == 200
    body = r.get_json()
    assert body["ok"] is True
    assert "sidecar" in body  # reachable/unreachable 字段在
