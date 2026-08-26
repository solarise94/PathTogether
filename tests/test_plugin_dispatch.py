# -*- coding: utf-8 -*-
"""插件能力 dispatch 端点测试（插件能力层 docs §4.2/§9，P1）。

覆盖（json 后端；转发层用 stub 替换 app.requests，与现有测试基建一致）：
  - 鉴权：缺 Bearer 401 / token 过期 401 token_expired / 无效 token 401 /
    **plugin JWT 调 dispatch 明确 403**（P2 才开放插件主体）；
  - claims 绑定：能力不在清单 403 capability_not_granted、Body.slide 与 token
    slide claim 不一致 403、X-AI-Session 缺失 400 / 与 token session 不符 403；
  - 启用检查：插件停用 404、能力不存在 404（不泄露存在性）；
  - 权限：发起用户（claims role/user_id）不满足 requiredPermissions →
    403 permission_denied（§6.1 用户权限映射）；
  - 限流：(token session, capability) 维度 token bucket 超限 429 + Retry-After；
    限流键在签名 claims 内（session 为空回退 jti），轮换 X-AI-Session 头不能
    绕过；同 session 的多个 token 共享同一桶；
  - 转发：X-Dispatch-Principal 头（主体类型/id/session）、body.slide/arguments；
    插件 5xx / 超时 / 不可达 → 503 capability_unavailable（retryable）；
  - 截断：result 超 64KB → truncated: true；
  - 审计：plugin_capability_dispatch 事件落库（主体/session/plugin/capability/
    slide/耗时/结果码）；
  - 示例插件端到端：经真实 sample-tma-score 后端（Flask test client）转发，
    含 X-Dispatch-Principal 校验拒绝路径。

运行：cd 项目根 && python3 -m pytest tests/test_plugin_dispatch.py -q
"""
import importlib.util
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import _bootstrap  # noqa: E402,F401  # session 目录+openslide stub（conftest 先行）
UPLOAD_DIR = _bootstrap.UPLOAD_DIR
import requests as real_requests  # noqa: E402

import app as app_mod  # noqa: E402
import share_store  # noqa: E402
from _pt_helpers import isolate_app  # noqa: E402

app_mod.UPLOAD_DIR = Path(os.environ["UPLOAD_DIR"])
app_mod.UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

import pytest  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
TMA_BACKEND = REPO_ROOT / "plugins" / "sample-tma-score" / "backend" / "app.py"

#: 示例插件 manifest 声明的服务基地址（转发 stub 按此前缀路由）
TMA_BASE_URL = "http://127.0.0.1:8061"
#: 示例插件能力全名
FULL_NAME = "dev.sample.tma/slide_summary"
DISPATCH_PATH = "/api/plugin/v1/dispatch/dev.sample.tma/slide_summary"


# --------------------------------------------------------------------------- #
# 转发层 stub（替换 app.requests；异常用真实 requests 异常类）
# --------------------------------------------------------------------------- #
class _Resp:
    """最小 requests.Response 形状（status_code + json()）。"""

    def __init__(self, status_code, body):
        self.status_code = status_code
        self._body = body

    def json(self):
        if isinstance(self._body, Exception):
            raise self._body
        return self._body


class _ForwardStub:
    """记录平台 → 插件的转发调用；handler 可编程返回/抛错。"""

    ConnectionError = real_requests.ConnectionError
    Timeout = real_requests.Timeout

    def __init__(self):
        self.calls = []
        self.handler = None  # fn(url, body, headers, timeout) -> _Resp

    def post(self, url, json=None, headers=None, timeout=None, **kw):
        self.calls.append({"url": url, "json": json, "headers": headers,
                           "timeout": timeout})
        if self.handler is None:
            return _Resp(200, {"result": {"ok": True, "slide": (json or {}).get("slide")}})
        return self.handler(url, json, headers, timeout)


def _load_sample_backend():
    """importlib 加载示例插件后端（目录名含连字符，不可常规 import）。"""
    spec = importlib.util.spec_from_file_location("sample_tma_backend", TMA_BACKEND)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# --------------------------------------------------------------------------- #
# 隔离 + 引导
# --------------------------------------------------------------------------- #
@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    """每用例独立存储 + AUTH_ENABLED=False + 全新 dispatch 限流器。"""
    isolate_app(monkeypatch, tmp_path)
    monkeypatch.setattr(app_mod, "AUTH_ENABLED", False)
    monkeypatch.setattr(app_mod, "_DISPATCH_RATE_LIMITER",
                        app_mod._PluginRateLimiter(
                            app_mod._PLUGIN_DISPATCH_RATE_LIMIT_PER_MIN))
    monkeypatch.setattr(app_mod, "requests", _ForwardStub())
    yield


def _install_tma():
    """安装示例插件 bundle（解析 provides 并登记能力注册表）。"""
    installation, err = app_mod.install_plugin_bundle("sample-tma-score")
    assert err is None, err
    assert installation is not None
    return installation


def _client():
    app_mod.app.config["TESTING"] = True
    return app_mod.app.test_client()


def _tool_token(slide="demo.svs", capabilities=None, session_id="",
                role="owner", user_id=None, ttl=None):
    claims = {
        "sub": user_id or "owner",
        "user_id": user_id or "",
        "role": role,
        "session_id": session_id,
        "slide": slide,
        "capabilities": capabilities if capabilities is not None else [FULL_NAME],
    }
    return app_mod._agent_tool_token_encode(claims)


def _dispatch(client, token, slide="demo.svs", session_id="sess-1",
              arguments=None):
    return client.post(DISPATCH_PATH,
                       json={"slide": slide, "arguments": arguments or {}},
                       headers={"Authorization": "Bearer " + token,
                                "X-AI-Session": session_id})


def _give_view(uid, slide="demo.svs"):
    """让 user 主体对 slide 有 view 权限（归属其名下，§6.1 映射入口）。"""
    share_store.set_slide_meta(slide, owner_user_id=uid)


def _assert_envelope(r, status, code, retryable=None):
    body = r.get_json() or {}
    assert r.status_code == status, "got %s body=%r" % (r.status_code, r.get_data(as_text=True))
    assert set(body.keys()) == {"error"}, "顶层键应为 error only: %r" % body
    err = body["error"]
    assert err["code"] == code, "code=%r full=%r" % (err.get("code"), err)
    assert isinstance(err["retryable"], bool)
    if retryable is not None:
        assert err["retryable"] is retryable
    return err


# --------------------------------------------------------------------------- #
# 1. 鉴权门槛（①）
# --------------------------------------------------------------------------- #
def test_dispatch_missing_bearer_401():
    _install_tma()
    r = _client().post(DISPATCH_PATH, json={"slide": "demo.svs"})
    _assert_envelope(r, 401, "unauthorized", retryable=False)


def test_dispatch_expired_token_401_token_expired():
    _install_tma()
    token = app_mod._plugin_jwt_encode(
        {"iss": app_mod._PLUGIN_JWT_ISSUER, "aud": app_mod._AGENT_TOOL_AUDIENCE,
         "typ": app_mod._AGENT_TOOL_TYP, "sub": "owner", "slide": "demo.svs",
         "capabilities": [FULL_NAME]}, ttl=-10)
    r = _dispatch(_client(), token)
    _assert_envelope(r, 401, "token_expired", retryable=True)


def test_dispatch_garbage_token_401():
    _install_tma()
    r = _dispatch(_client(), "not.a.jwt")
    _assert_envelope(r, 401, "unauthorized", retryable=False)


def test_dispatch_plugin_jwt_403():
    """plugin JWT（installation 主体）调 dispatch 是 P2 语义，P1 明确 403。"""
    _install_tma()
    plugin_token = app_mod._plugin_jwt_encode(
        {"iss": app_mod._PLUGIN_JWT_ISSUER, "aud": app_mod._PLUGIN_JWT_AUDIENCE,
         "sub": "pin_x", "scope": app_mod._PLUGIN_JWT_SCOPES})
    r = _dispatch(_client(), plugin_token)
    _assert_envelope(r, 403, "forbidden", retryable=False)


def test_dispatch_capability_not_in_grant_list_403():
    _install_tma()
    token = _tool_token(capabilities=["other.plugin/other_cap"])
    r = _dispatch(_client(), token)
    _assert_envelope(r, 403, "capability_not_granted", retryable=False)


def test_dispatch_body_slide_mismatch_403():
    _install_tma()
    token = _tool_token(slide="demo.svs")
    r = _dispatch(_client(), token, slide="other.svs")
    err = _assert_envelope(r, 403, "forbidden", retryable=False)
    assert err.get("details", {}).get("reason") == "slide_mismatch"


def test_dispatch_missing_session_header_400():
    _install_tma()
    client = _client()
    r = client.post(DISPATCH_PATH, json={"slide": "demo.svs"},
                    headers={"Authorization": "Bearer " + _tool_token()})
    _assert_envelope(r, 400, "invalid_request", retryable=False)


def test_dispatch_session_mismatch_403():
    _install_tma()
    token = _tool_token(session_id="sess-token")
    r = _dispatch(_client(), token, session_id="sess-header")
    _assert_envelope(r, 403, "forbidden", retryable=False)


# --------------------------------------------------------------------------- #
# 2. 启用检查（②：不泄露存在性，一律 404）
# --------------------------------------------------------------------------- #
def test_dispatch_unknown_capability_404():
    _install_tma()
    client = _client()
    r = client.post("/api/plugin/v1/dispatch/dev.sample.tma/nope",
                    json={"slide": "demo.svs"},
                    headers={"Authorization": "Bearer " + _tool_token(
                        capabilities=["dev.sample.tma/nope"]),
                        "X-AI-Session": "sess-1"})
    _assert_envelope(r, 404, "not_found", retryable=False)


def test_dispatch_disabled_installation_404():
    installation = _install_tma()
    # 直接停用安装行（API 路径的 CSRF/owner 门不在本用例范围）
    assert share_store.set_installation_enabled(
        installation["installation_id"], False) is not None
    r = _dispatch(_client(), _tool_token())
    _assert_envelope(r, 404, "not_found", retryable=False)


def test_dispatch_disabled_capability_404():
    installation = _install_tma()
    caps = [dict(c) for c in installation["capabilities"]]
    caps[0]["enabled"] = False
    share_store.set_installation_capabilities(installation["installation_id"], caps)
    r = _dispatch(_client(), _tool_token())
    _assert_envelope(r, 404, "not_found", retryable=False)


# --------------------------------------------------------------------------- #
# 3. 权限检查（③：§6.1 用户权限映射）
# --------------------------------------------------------------------------- #
def test_dispatch_permission_denied_403_for_user_without_view():
    _install_tma()
    # user 主体对 demo.svs 无任何归属/公开/认领关系 → view 映射为空集
    token = _tool_token(role="user", user_id="usr_nobody")
    r = _dispatch(_client(), token)
    _assert_envelope(r, 403, "permission_denied", retryable=False)


def test_dispatch_owner_principal_passes_permission():
    installation = _install_tma()
    client = _client()
    stub = app_mod.requests
    r = _dispatch(client, _tool_token())
    assert r.status_code == 200, r.get_json()
    assert r.get_json()["result"] == {"ok": True, "slide": "demo.svs"}
    # 转发地址 = 注册表登记的 base_url + /capabilities/{name}
    assert stub.calls[-1]["url"] == TMA_BASE_URL + "/capabilities/slide_summary"


# --------------------------------------------------------------------------- #
# 4. 限流（④：(token session, capability) 维度，session 空时回退 jti）
# --------------------------------------------------------------------------- #
def test_dispatch_rate_limited_429(monkeypatch):
    _install_tma()
    monkeypatch.setattr(app_mod, "_DISPATCH_RATE_LIMITER",
                        app_mod._PluginRateLimiter(1))
    client = _client()
    token = _tool_token()
    r1 = _dispatch(client, token)
    assert r1.status_code == 200, r1.get_json()
    r2 = _dispatch(client, token)
    err = _assert_envelope(r2, 429, "rate_limited", retryable=True)
    assert "Retry-After" in r2.headers
    assert err["details"]["capability"] == FULL_NAME
    # 限流键在签名 token 内（此处为 jti）：轮换 X-AI-Session 头不能绕过
    r3 = _dispatch(client, token, session_id="sess-rotated-1")
    _assert_envelope(r3, 429, "rate_limited", retryable=True)
    r4 = _dispatch(client, token, session_id="sess-rotated-2")
    _assert_envelope(r4, 429, "rate_limited", retryable=True)


def test_dispatch_rate_limit_keyed_by_token_not_header(monkeypatch):
    """起跑 token（session 空串）按 jti 限流：不同 token 各自一桶。"""
    _install_tma()
    monkeypatch.setattr(app_mod, "_DISPATCH_RATE_LIMITER",
                        app_mod._PluginRateLimiter(1))
    client = _client()
    r1 = _dispatch(client, _tool_token())
    assert r1.status_code == 200, r1.get_json()
    r2 = _dispatch(client, _tool_token())  # 新 token → 新 jti → 新桶
    assert r2.status_code == 200, r2.get_json()


def test_dispatch_rate_limit_shared_across_tokens_of_same_session(monkeypatch):
    """token 绑定 session 时按 session 维度：同 session 的新 token 也限流。"""
    _install_tma()
    monkeypatch.setattr(app_mod, "_DISPATCH_RATE_LIMITER",
                        app_mod._PluginRateLimiter(1))
    client = _client()
    r1 = _dispatch(client, _tool_token(session_id="sess-1"), session_id="sess-1")
    assert r1.status_code == 200, r1.get_json()
    r2 = _dispatch(client, _tool_token(session_id="sess-1"), session_id="sess-1")
    _assert_envelope(r2, 429, "rate_limited", retryable=True)


# --------------------------------------------------------------------------- #
# 5. 转发（⑥）/ 截断（⑦）/ 审计（⑤）
# --------------------------------------------------------------------------- #
def test_dispatch_forwards_principal_and_arguments():
    _install_tma()
    _give_view("usr_1")
    client = _client()
    token = _tool_token(user_id="usr_1", role="user", session_id="sess-9")
    r = _dispatch(client, token, session_id="sess-9",
                  arguments={"include_mpp": False})
    assert r.status_code == 200, r.get_json()
    call = app_mod.requests.calls[-1]
    assert call["json"] == {"slide": "demo.svs", "arguments": {"include_mpp": False}}
    principal = json.loads(call["headers"]["X-Dispatch-Principal"])
    assert principal == {"type": "agent", "user_id": "usr_1", "role": "user",
                         "session_id": "sess-9"}


def test_dispatch_plugin_5xx_maps_capability_unavailable():
    _install_tma()
    app_mod.requests.handler = lambda *a: _Resp(503, {"error": "boom"})
    r = _dispatch(_client(), _tool_token())
    _assert_envelope(r, 503, "capability_unavailable", retryable=True)


def test_dispatch_plugin_timeout_maps_capability_unavailable():
    _install_tma()

    def _timeout(*a):
        raise real_requests.Timeout("plugin too slow")
    app_mod.requests.handler = _timeout
    r = _dispatch(_client(), _tool_token())
    _assert_envelope(r, 503, "capability_unavailable", retryable=True)


def test_dispatch_plugin_unreachable_maps_capability_unavailable():
    _install_tma()

    def _down(*a):
        raise real_requests.ConnectionError("plugin down")
    app_mod.requests.handler = _down
    r = _dispatch(_client(), _tool_token())
    _assert_envelope(r, 503, "capability_unavailable", retryable=True)


def test_dispatch_plugin_4xx_passthrough_envelope():
    _install_tma()
    app_mod.requests.handler = lambda *a: _Resp(
        400, {"error": {"code": "invalid_request", "message": "参数非法",
                        "retryable": False}})
    r = _dispatch(_client(), _tool_token())
    _assert_envelope(r, 400, "invalid_request", retryable=False)


def test_dispatch_plugin_redirect_rejected_503():
    """插件 30x 不跟随：转发显式 allow_redirects=False，3xx → 503。"""
    _install_tma()
    app_mod.requests.handler = lambda *a: _Resp(
        302, {}, )
    client = _client()
    r = _dispatch(client, _tool_token())
    _assert_envelope(r, 503, "capability_unavailable", retryable=True)
    # 只有一次转发调用（没有跟随到 Location 目标）
    assert len(app_mod.requests.calls) == 1
    events = share_store.list_audit(action="plugin_capability_dispatch")
    assert events and events[0]["detail"].get("reason") == "plugin_redirect"


def test_dispatch_non_http_base_url_rejected_503():
    """历史登记行带非 http(s) base_url → 运行时兜底拒绝（不发请求）。"""
    installation = _install_tma()
    caps = [dict(c) for c in installation["capabilities"]]
    caps[0]["base_url"] = "file:///etc/passwd"
    share_store.set_installation_capabilities(installation["installation_id"], caps)
    client = _client()
    r = _dispatch(client, _tool_token())
    err = _assert_envelope(r, 503, "capability_unavailable", retryable=True)
    assert err.get("details", {}).get("reason") == "invalid_base_url"
    assert app_mod.requests.calls == []  # 从未出站


def test_dispatch_result_truncated_over_64kb():
    _install_tma()
    big = "x" * (app_mod._PLUGIN_DISPATCH_RESULT_MAX_BYTES + 4096)
    app_mod.requests.handler = lambda *a: _Resp(200, {"result": big})
    r = _dispatch(_client(), _tool_token())
    assert r.status_code == 200
    body = r.get_json()
    assert body["truncated"] is True
    assert body["original_bytes"] > app_mod._PLUGIN_DISPATCH_RESULT_MAX_BYTES
    assert len(body["result"]) < app_mod._PLUGIN_DISPATCH_RESULT_MAX_BYTES
    # 序列化后不超过 64KB 上限
    encoded = json.dumps(body).encode("utf-8")
    assert len(encoded) <= app_mod._PLUGIN_DISPATCH_RESULT_MAX_BYTES + 2048


def test_dispatch_result_structured_truncation_keeps_json_valid():
    _install_tma()
    big = {"rows": [{"i": i, "pad": "y" * 512} for i in range(1000)]}
    app_mod.requests.handler = lambda *a: _Resp(200, {"result": big})
    r = _dispatch(_client(), _tool_token())
    assert r.status_code == 200
    body = r.get_json()
    assert body["truncated"] is True
    assert isinstance(body["result"], dict) and "preview" in body["result"]


def test_dispatch_audit_event_recorded():
    _install_tma()
    _give_view("usr_a")
    client = _client()
    before = len(share_store.list_audit(action="plugin_capability_dispatch"))
    r = _dispatch(client, _tool_token(user_id="usr_a", role="user"),
                  session_id="sess-aud")
    assert r.status_code == 200
    events = share_store.list_audit(action="plugin_capability_dispatch")
    assert len(events) == before + 1
    ev = events[0]
    assert ev["actor_user_id"] == "usr_a"
    assert ev["target_type"] == "plugin_capability"
    assert ev["target_id"] == FULL_NAME
    assert ev["slide"] == "demo.svs"
    d = ev["detail"]
    assert d["session_id"] == "sess-aud"
    assert d["token_jti"]  # 限流/追踪维度：token jti 落审计
    assert d["plugin_id"] == "dev.sample.tma"
    assert d["capability"] == "slide_summary"
    assert d["status"] == 200 and d["code"] == "ok"
    assert isinstance(d["duration_ms"], int)


def test_dispatch_audit_records_failures_too():
    _install_tma()
    token = _tool_token(role="user", user_id="usr_nobody")
    _dispatch(_client(), token)
    events = share_store.list_audit(action="plugin_capability_dispatch")
    assert events and events[0]["detail"]["code"] == "permission_denied"


# --------------------------------------------------------------------------- #
# 6. 示例插件端到端（真实 sample-tma-score 后端经 Flask test client）
# --------------------------------------------------------------------------- #
class _BackendAdapter:
    """把转发 stub 路由到示例插件后端的 Flask test client。"""

    def __init__(self, backend_mod):
        self.backend = backend_mod.app.test_client()
        self.calls = []

    def handler(self, url, body, headers, timeout):
        path = url[len(TMA_BASE_URL):]
        self.calls.append({"path": path, "body": body, "headers": headers})
        resp = self.backend.post(path, json=body, headers=headers)
        try:
            payload = resp.get_json()
        except Exception:
            payload = {}
        return _Resp(resp.status_code, payload)


def test_dispatch_end_to_end_with_sample_backend():
    _install_tma()
    _give_view("usr_e2e")
    adapter = _BackendAdapter(_load_sample_backend())
    app_mod.requests.handler = adapter.handler
    r = _dispatch(_client(), _tool_token(user_id="usr_e2e", role="user"),
                  arguments={"include_mpp": True})
    assert r.status_code == 200, r.get_json()
    result = r.get_json()["result"]
    assert result["slide"] == "demo.svs"
    assert result["plugin"] == "dev.sample.tma"
    assert result["capability"] == "slide_summary"
    assert result["source"] == "degraded"  # 未配置平台回调环境变量
    assert result["requested_by_session"] == "sess-1"


def test_sample_backend_rejects_missing_principal():
    """示例插件后端自身强制校验 X-Dispatch-Principal（无头 → 401 信封）。"""
    backend = _load_sample_backend().app.test_client()
    resp = backend.post("/capabilities/slide_summary",
                        json={"slide": "demo.svs", "arguments": {}})
    assert resp.status_code == 401
    assert resp.get_json()["error"]["code"] == "unauthorized"
    # 平台附加的合法 principal 放行
    ok = backend.post(
        "/capabilities/slide_summary",
        json={"slide": "demo.svs", "arguments": {}},
        headers={"X-Dispatch-Principal": json.dumps(
            {"type": "agent", "user_id": "u1", "role": "user",
             "session_id": "s1"})})
    assert ok.status_code == 200
    assert ok.get_json()["result"]["slide"] == "demo.svs"


def test_sample_backend_healthz():
    backend = _load_sample_backend().app.test_client()
    resp = backend.get("/healthz")
    assert resp.status_code == 200
    assert resp.get_json()["plugin"] == "dev.sample.tma"


def test_docker_entry_supervises_sample_tma_backend():
    """平台容器 entrypoint 默认托管 sample-tma-score，不必重建后手动 exec。"""
    text = (REPO_ROOT / "docker_entry.sh").read_text(encoding="utf-8")
    assert "sample-tma-score/backend/app.py" in text
    assert "SAMPLE_TMA_BACKEND" in text
    assert "exec gunicorn" in text
    # 崩溃自动拉起（避免一次退出后能力永久不可用）
    assert "restart in 2s" in text
    # =0 可关，默认仍起
    assert "${SAMPLE_TMA_BACKEND:-1}" in text


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
