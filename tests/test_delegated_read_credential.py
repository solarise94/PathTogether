# -*- coding: utf-8 -*-
"""委托读凭证（delegated read token）安全测试。

docs/measurement-models-homepc-implementation.md §1 阻塞缺口的平台侧闭环：
dispatch 在 agent token / 用户实时权限 / 插件启用全过之后签发短时只读凭证
（X-Delegated-Read-Token 头），插件后端凭它经 by-id region / slide-info
端点读 ROI。覆盖：

  - dispatch 签发：转发头携带、body 形状不变、凭证不入审计与 Agent 结果；
    slide 解析失败 fail-safe 不签发不阻断；
  - 读端点复核（每项读时重查）：happy path / 他人切片 / mint 后撤权 /
    插件停用 / 过期 / revision 变更 / 错切片 / ROI 超上限 / 他安装冒用；
  - 域隔离：plugin JWT / agent-tool-token / 委托凭证三域互不冒用；
  - 写通道：委托凭证不能落标注（annotations 端点只认 run grant）；
  - 既有行为不变：无凭证头时 run grant gate 原样（认证态无 grant 仍 403，
    HP installation + X-Run-Grant 仍放行）。

运行：cd 项目根 && python3 -m pytest tests/test_delegated_read_credential.py -q
"""
import base64
import json
import os
import sys
import threading
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import _bootstrap  # noqa: E402,F401  # session 目录+openslide stub（conftest 先行）
UPLOAD_DIR = _bootstrap.UPLOAD_DIR
import requests as real_requests  # noqa: E402

import app as app_mod  # noqa: E402
import share_store  # noqa: E402
from _pt_helpers import isolate_app, make_snapshot_context, register_slide_row  # noqa: E402

app_mod.UPLOAD_DIR = Path(os.environ["UPLOAD_DIR"])
app_mod.UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

import pytest  # noqa: E402

PLUGIN_DIR_KEY = "pathsegmentor-measure"
PLUGIN_ID = "dev.pathsegmentor.measure"
CAP_NAME = "segment_region"
FULL_NAME = "%s/%s" % (PLUGIN_ID, CAP_NAME)
DISPATCH_PATH = "/api/plugin/v1/dispatch/%s/%s" % (PLUGIN_ID, CAP_NAME)
# 对照插件：manifest/capability 均不声明 slide:region:read
TMA_PLUGIN_KEY = "sample-tma-score"
TMA_PLUGIN_ID = "dev.sample.tma"
TMA_CAP_NAME = "slide_summary"
TMA_FULL_NAME = "%s/%s" % (TMA_PLUGIN_ID, TMA_CAP_NAME)
TMA_DISPATCH_PATH = "/api/plugin/v1/dispatch/%s/%s" % (TMA_PLUGIN_ID,
                                                       TMA_CAP_NAME)
# getattr 兜底：本文件在旧代码（无委托凭证）上也必须可收集——用例失败本身
# 就是「旧代码不签发/不受理凭证」的回归证明，而不是收集期 AttributeError。
HDR = getattr(app_mod, "_DELEGATED_READ_HEADER", "X-Delegated-Read-Token")


# --------------------------------------------------------------------------- #
# 隔离 + 引导
# --------------------------------------------------------------------------- #
@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    isolate_app(monkeypatch, tmp_path)
    monkeypatch.setattr(app_mod, "AUTH_ENABLED", False)
    monkeypatch.setattr(app_mod, "_DISPATCH_RATE_LIMITER",
                        app_mod._PluginRateLimiter(
                            app_mod._PLUGIN_DISPATCH_RATE_LIMIT_PER_MIN))
    monkeypatch.setattr(app_mod, "_PLUGIN_PIXEL_WINDOW",
                        app_mod._SlidingPixelWindow(
                            app_mod._PLUGIN_REGION_PIXEL_BUDGET_PER_MIN))
    monkeypatch.setattr(app_mod, "_PLUGIN_RATE_LIMITER",
                        app_mod._PluginRateLimiter(
                            app_mod._PLUGIN_RATE_LIMIT_PER_MIN))
    monkeypatch.setattr(app_mod, "_PLUGIN_REGION_CONCURRENCY_SEM",
                        threading.BoundedSemaphore(
                            app_mod._PLUGIN_REGION_MAX_CONCURRENT))
    yield


class _ForwardStub:
    """记录 dispatch → 插件后端转发；可编程响应。"""

    ConnectionError = real_requests.ConnectionError
    Timeout = real_requests.Timeout

    def __init__(self):
        self.calls = []
        self.body = {"result": {"ok": True}}
        self.handler = None  # fn(url, json, headers, timeout) -> _Resp

    def post(self, url, json=None, headers=None, timeout=None, **kw):
        self.calls.append({"url": url, "json": json, "headers": headers,
                           "timeout": timeout})
        if self.handler is not None:
            return self.handler(url, json, headers, timeout)
        return _Resp(200, self.body)


class _Resp:
    def __init__(self, status_code, body):
        self.status_code = status_code
        self._body = body

    def json(self):
        return self._body


def _client():
    app_mod.app.config["TESTING"] = True
    return app_mod.app.test_client()


def _install_measure_plugin(monkeypatch):
    """经 owner 安装 API 语义登记插件（走 manifest 校验 + 来源 pin）。"""
    installation, err = app_mod.install_plugin_bundle(PLUGIN_DIR_KEY)
    assert err is None, err
    assert installation is not None
    return installation


def _install_second_plugin(monkeypatch):
    """装第二个插件（另一 installation，冒用测试用）并拿到已知 secret。"""
    installation, err = app_mod.install_plugin_bundle("sample-tma-score")
    assert err is None, err
    rotated = share_store.rotate_installation_secret(
        installation["installation_id"])
    return installation, rotated["secret"]


def _plugin_jwt(installation, secret):
    """安装凭证 → scoped plugin JWT（scope 含 region:read / slide:read）。"""
    r = _client().post("/api/plugin/v1/auth/token",
                       json={"installation_id": installation["installation_id"],
                             "secret": secret})
    assert r.status_code == 200, r.get_json()
    return r.get_json()["access_token"]


def _install_with_known_secret(monkeypatch):
    """登记插件并轮换出已知 secret（读端点调用需要插件侧 JWT）。"""
    installation = _install_measure_plugin(monkeypatch)
    rotated = share_store.rotate_installation_secret(
        installation["installation_id"])
    return installation, rotated["secret"]


def _tool_token(slide="demo.svs", user_id="usr_a", role="user",
                session_id="", capabilities=None):
    claims = {
        "sub": user_id,
        "user_id": user_id,
        "role": role,
        "session_id": session_id,
        "slide": slide,
        "capabilities": capabilities if capabilities is not None else [FULL_NAME],
    }
    return app_mod._agent_tool_token_encode(claims)


def _dispatch(client, stub, token, slide="demo.svs", session_id="sess-1",
              arguments=None):
    return client.post(
        DISPATCH_PATH,
        json={"slide": slide, "arguments": arguments or {}},
        headers={"Authorization": "Bearer " + token,
                 "X-AI-Session": session_id})


def _minted_token(client, stub, token, slide="demo.svs"):
    """跑一次 dispatch 取转发头里的委托凭证（断言恰好一个转发调用）。"""
    r = _dispatch(client, stub, token, slide=slide)
    assert r.status_code == 200, r.get_json()
    assert len(stub.calls) == 1
    return (stub.calls[-1]["headers"] or {}).get(HDR)


_FAKE_ENTRY = {"pool": None, "sem": None}


@contextmanager
def _borrow_pair_ctx(pair):
    yield pair


def _slide_read_mocks(mpp=0.5):
    """region / slide-info 读路径 mock（dimensions + mpp）。"""
    pair = {"osr": mock.Mock(dimensions=(100000, 100000))}
    return (mock.patch.object(app_mod, "_get_slide",
                              return_value=_FAKE_ENTRY),
            mock.patch.object(app_mod.slide_cache, "borrow_pair",
                              side_effect=lambda _e: _borrow_pair_ctx(pair)),
            mock.patch.object(app_mod, "_read_metadata",
                              return_value={"mpp_x": mpp,
                                            "width": 100000,
                                            "height": 100000}))


def _region_payload(w=8, h=8, bbox=(0, 0, 16, 16)):
    return {
        "image_base64": base64.b64encode(b"fake-jpeg-bytes").decode("ascii"),
        "mime": "image/jpeg", "width": w, "height": h,
        "src": {"x": bbox[0], "y": bbox[1], "w": bbox[2], "h": bbox[3]},
        "magnification": 20.0, "read_level": 0, "upsampled": False,
    }


def _touch_slide(name="demo.svs", content=b"svs-stub"):
    path = app_mod.UPLOAD_DIR / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    register_slide_row(name)
    return name


def _register_owned_slide(monkeypatch, name="demo.svs", uid="usr_a",
                          content=b"svs-stub"):
    """登记切片并回填 slides 行归属（dispatch 权限与读端点复核同源）。"""
    _touch_slide(name, content=content)
    share_store.set_slide_meta(name, owner_user_id=uid)
    sid = share_store.get_slide_id(name)
    assert sid
    return sid


def _post_region(client, jwt, sid, *, x=0, y=0, w=16, h=16, delegated=None,
                 extra_headers=None):
    headers = {"Authorization": "Bearer " + jwt}
    if delegated is not None:
        headers[HDR] = delegated
    if extra_headers:
        headers.update(extra_headers)
    return client.post("/api/plugin/v1/slides/by-id/%s/regions" % sid,
                       headers=headers,
                       json={"x": x, "y": y, "w": w, "h": h,
                             "max_long_edge": 8})


def _reencode(claims, monkeypatch=None, ttl=None):
    """按既有 payload 重签委托凭证（改 claims / TTL 的测试辅助）。"""
    body = {k: v for k, v in claims.items()
            if k not in ("iss", "aud", "typ", "exp", "iat")}
    if ttl is not None:
        monkeypatch.setattr(app_mod, "_DELEGATED_READ_TTL_SECONDS", ttl)
    return app_mod._delegated_read_token_encode(body)


def _decode_token(token):
    payload, err = app_mod._delegated_read_token_decode(token)
    assert err is None, err
    return payload


def _assert_envelope(r, status, code):
    body = r.get_json() or {}
    assert r.status_code == status, "got %s body=%r" % (
        r.status_code, r.get_data(as_text=True))
    err = body.get("error") or {}
    assert err.get("code") == code, err
    return err


# --------------------------------------------------------------------------- #
# 1. dispatch 签发
# --------------------------------------------------------------------------- #
def test_dispatch_forwards_delegated_credential(monkeypatch):
    installation, _secret = _install_with_known_secret(monkeypatch)
    _register_owned_slide(monkeypatch)
    stub = _ForwardStub()
    monkeypatch.setattr(app_mod, "requests", stub)
    client = _client()
    token = _minted_token(client, stub, _tool_token())
    assert token, "dispatch 应随转发下发委托凭证头"
    assert len(token.split(".")) == 3
    claims = _decode_token(token)
    assert claims["sub"] == installation["installation_id"]
    assert claims["user_id"] == "usr_a"
    assert claims["role"] == "user"
    assert claims["slide_id"] == share_store.get_slide_id("demo.svs")
    assert claims["capability"] == CAP_NAME
    assert claims["roi_max_pixels"] == app_mod._DELEGATED_READ_ROI_MAX_PIXELS
    assert claims["revision"] == app_mod._slide_revision(
        app_mod.slide_store.resolve_slide_id(claims["slide_id"]))
    assert claims["exp"] - claims["iat"] <= 120  # TTL ≤120s
    assert claims["jti"]
    # body 形状：slide + slide_id（token 无 claim 时空串）+ arguments
    assert set(stub.calls[-1]["json"].keys()) == \
        {"slide", "slide_id", "arguments"}
    assert stub.calls[-1]["json"]["slide_id"] == ""


def test_dispatch_failsafe_without_credential_when_slide_unresolvable(
        monkeypatch):
    _install_with_known_secret(monkeypatch)
    _register_owned_slide(monkeypatch)
    stub = _ForwardStub()
    monkeypatch.setattr(app_mod, "requests", stub)

    orig = share_store.get_slide_id

    def _boom(name):
        raise RuntimeError("db down")
    monkeypatch.setattr(share_store, "get_slide_id", _boom)
    r = _dispatch(_client(), stub, _tool_token())
    assert r.status_code == 200, r.get_json()  # 不阻断转发（fail-safe）
    assert HDR not in (stub.calls[-1]["headers"] or {})  # 但不签发


def test_dispatch_credential_not_in_result_or_audit(monkeypatch):
    _register_owned_slide(monkeypatch, uid="usr_audit")
    _install_with_known_secret(monkeypatch)
    stub = _ForwardStub()
    stub.body = {"result": {"ok": True, "echo_headers": False}}
    monkeypatch.setattr(app_mod, "requests", stub)
    client = _client()
    tok = _minted_token(client, stub, _tool_token(user_id="usr_audit"))
    assert tok
    r = _dispatch(client, stub, _tool_token(user_id="usr_audit"))
    assert r.status_code == 200
    # 凭证不出现在 Agent 可见结果
    assert tok not in r.get_data(as_text=True)
    # 不入审计（detail 里没有 token/jti 之外的新凭证字段）
    events = share_store.list_audit(action="plugin_capability_dispatch")
    assert events
    assert tok not in json.dumps(events, ensure_ascii=False)


# --------------------------------------------------------------------------- #
# 1b. 最小披露：凭证只签给声明 slide:region:read 的能力
# --------------------------------------------------------------------------- #
def test_dispatch_withholds_credential_for_non_region_plugin(monkeypatch):
    """manifest/capability 均无 slide:region:read 的插件（sample-tma-score）
    dispatch 一律不随转发下发委托凭证（旧实现每次 dispatch 都签发）。"""
    installation, err = app_mod.install_plugin_bundle(TMA_PLUGIN_KEY)
    assert err is None, err
    caps = {c["name"]: c for c in installation["capabilities"]}
    assert app_mod._DELEGATED_READ_SCOPE not in \
        (caps[TMA_CAP_NAME].get("required_permissions") or [])
    _register_owned_slide(monkeypatch)
    stub = _ForwardStub()
    monkeypatch.setattr(app_mod, "requests", stub)
    token = _tool_token(capabilities=[TMA_FULL_NAME])
    r = _client().post(
        TMA_DISPATCH_PATH,
        json={"slide": "demo.svs", "arguments": {}},
        headers={"Authorization": "Bearer " + token,
                 "X-AI-Session": "sess-1"})
    assert r.status_code == 200, r.get_json()
    assert len(stub.calls) == 1
    assert HDR not in (stub.calls[-1]["headers"] or {})


def test_dispatch_credential_gated_per_capability_region_scope(monkeypatch):
    """同一 token 调两个能力：segment_region（requiredPermissions 含
    slide:region:read）随转发带凭证；slide_summary（不含）不带——门槛按
    能力级声明判定，不按插件级一揽子签发。"""
    _install_measure_plugin(monkeypatch)
    _install_second_plugin(monkeypatch)
    _register_owned_slide(monkeypatch)
    stub = _ForwardStub()
    monkeypatch.setattr(app_mod, "requests", stub)
    client = _client()
    token = _tool_token(capabilities=[FULL_NAME, TMA_FULL_NAME])

    r = _dispatch(client, stub, token)
    assert r.status_code == 200, r.get_json()
    assert HDR in (stub.calls[-1]["headers"] or {})  # 区域读能力：带凭证

    r2 = client.post(
        TMA_DISPATCH_PATH,
        json={"slide": "demo.svs", "arguments": {}},
        headers={"Authorization": "Bearer " + token,
                 "X-AI-Session": "sess-1"})
    assert r2.status_code == 200, r2.get_json()
    assert HDR not in (stub.calls[-1]["headers"] or {})  # 非区域读能力：不带


# --------------------------------------------------------------------------- #
# 2. 读端点复核
# --------------------------------------------------------------------------- #
def test_region_accepts_delegated_credential_happy_path(monkeypatch):
    installation, secret = _install_with_known_secret(monkeypatch)
    sid = _register_owned_slide(monkeypatch)
    stub = _ForwardStub()
    monkeypatch.setattr(app_mod, "requests", stub)
    delegated = _minted_token(_client(), stub, _tool_token())
    jwt = _plugin_jwt(installation, secret)
    mocks = _slide_read_mocks()
    with mocks[0], mocks[1], mocks[2], \
            mock.patch.object(app_mod, "_read_region_b64",
                              return_value=_region_payload()):
        r = _post_region(_client(), jwt, sid, delegated=delegated)
    assert r.status_code == 200, r.get_data(as_text=True)
    body = r.get_json()
    assert body["src"] == {"x": 0, "y": 0, "w": 16, "h": 16}
    assert body["mime"] == "image/jpeg"
    # 响应不回显凭证
    assert delegated not in r.get_data(as_text=True)


def test_region_by_name_route_accepts_delegated_credential(monkeypatch):
    """后端实际调用的 by-name regions 路由与 by-id 共用同一实现与复核。"""
    installation, secret = _install_with_known_secret(monkeypatch)
    _register_owned_slide(monkeypatch)
    stub = _ForwardStub()
    monkeypatch.setattr(app_mod, "requests", stub)
    delegated = _minted_token(_client(), stub, _tool_token())
    jwt = _plugin_jwt(installation, secret)
    mocks = _slide_read_mocks()
    with mocks[0], mocks[1], mocks[2], \
            mock.patch.object(app_mod, "_read_region_b64",
                              return_value=_region_payload()):
        r = _client().post("/api/plugin/v1/slides/demo.svs/regions",
                           headers={"Authorization": "Bearer " + jwt,
                                    HDR: delegated},
                           json={"x": 0, "y": 0, "w": 16, "h": 16,
                                 "max_long_edge": 8})
    assert r.status_code == 200, r.get_data(as_text=True)


def test_slide_info_by_id_accepts_delegated_credential(monkeypatch):
    installation, secret = _install_with_known_secret(monkeypatch)
    sid = _register_owned_slide(monkeypatch)
    stub = _ForwardStub()
    monkeypatch.setattr(app_mod, "requests", stub)
    delegated = _minted_token(_client(), stub, _tool_token())
    jwt = _plugin_jwt(installation, secret)
    mocks = _slide_read_mocks()
    with mocks[0], mocks[1], mocks[2]:
        r = _client().get("/api/plugin/v1/slides/by-id/%s" % sid,
                          headers={"Authorization": "Bearer " + jwt,
                                   HDR: delegated})
    assert r.status_code == 200, r.get_data(as_text=True)


def test_region_rejects_credential_of_another_slide(monkeypatch):
    installation, secret = _install_with_known_secret(monkeypatch)
    sid_a = _register_owned_slide(monkeypatch, "a.svs", content=b"aaa")
    _register_owned_slide(monkeypatch, "b.svs", uid="usr_a", content=b"bbb")
    stub = _ForwardStub()
    monkeypatch.setattr(app_mod, "requests", stub)
    delegated = _minted_token(_client(), stub,
                              _tool_token(slide="a.svs"), slide="a.svs")
    jwt = _plugin_jwt(installation, secret)
    mocks = _slide_read_mocks()
    with mocks[0], mocks[1], mocks[2], \
            mock.patch.object(app_mod, "_read_region_b64",
                              return_value=_region_payload()):
        r = _post_region(_client(), jwt, sid_a, delegated=delegated)
    assert r.status_code == 200  # 绑定切片本身可用
    sid_b = share_store.get_slide_id("b.svs")
    r = _post_region(_client(), jwt, sid_b, delegated=delegated)
    assert r.status_code == 403, r.get_json()
    assert r.get_json()["error"]["code"] == "forbidden"


def test_region_rejects_after_permission_revoked(monkeypatch):
    installation, secret = _install_with_known_secret(monkeypatch)
    sid = _register_owned_slide(monkeypatch, uid="usr_a")
    stub = _ForwardStub()
    monkeypatch.setattr(app_mod, "requests", stub)
    delegated = _minted_token(_client(), stub, _tool_token())
    jwt = _plugin_jwt(installation, secret)
    # mint 后归属改判给他人（收录关系撤销等价形态）：读时实时复查必须拒
    from _pt_helpers import pg_connection
    conn = pg_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("UPDATE slides SET owner_user_id='usr_other' "
                        "WHERE slide_id=%s", (sid,))
        conn.commit()
    finally:
        conn.close()
    mocks = _slide_read_mocks()
    with mocks[0], mocks[1], mocks[2]:
        r = _post_region(_client(), jwt, sid, delegated=delegated)
    assert r.status_code == 403, r.get_json()
    assert r.get_json()["error"]["code"] == "forbidden"


def test_region_rejects_when_plugin_disabled(monkeypatch):
    installation, secret = _install_with_known_secret(monkeypatch)
    sid = _register_owned_slide(monkeypatch)
    stub = _ForwardStub()
    monkeypatch.setattr(app_mod, "requests", stub)
    delegated = _minted_token(_client(), stub, _tool_token())
    jwt = _plugin_jwt(installation, secret)
    assert share_store.set_installation_enabled(
        installation["installation_id"], False) is not None
    r = _post_region(_client(), jwt, sid, delegated=delegated)
    assert r.status_code == 401, r.get_json()


def test_region_rejects_expired_credential(monkeypatch):
    installation, secret = _install_with_known_secret(monkeypatch)
    sid = _register_owned_slide(monkeypatch)
    stub = _ForwardStub()
    monkeypatch.setattr(app_mod, "requests", stub)
    delegated = _minted_token(_client(), stub, _tool_token())
    claims = _decode_token(delegated)
    expired = _reencode(claims, monkeypatch, ttl=-10)
    jwt = _plugin_jwt(installation, secret)
    r = _post_region(_client(), jwt, sid, delegated=expired)
    assert r.status_code == 401, r.get_json()
    assert r.get_json()["error"]["code"] == "token_expired"


def test_region_rejects_revision_change(monkeypatch):
    installation, secret = _install_with_known_secret(monkeypatch)
    sid = _register_owned_slide(monkeypatch)
    stub = _ForwardStub()
    monkeypatch.setattr(app_mod, "requests", stub)
    delegated = _minted_token(_client(), stub, _tool_token())
    claims = _decode_token(delegated)
    stale = dict(claims)
    stale["revision"] = "sha256:0000000000000000"  # 旧代际
    stale_token = _reencode(stale)
    jwt = _plugin_jwt(installation, secret)
    mocks = _slide_read_mocks()
    with mocks[0], mocks[1], mocks[2]:
        r = _post_region(_client(), jwt, sid, delegated=stale_token)
    assert r.status_code == 409, r.get_json()
    assert r.get_json()["error"]["code"] == "slide_revision_conflict"


def test_region_rejects_roi_over_cap(monkeypatch):
    installation, secret = _install_with_known_secret(monkeypatch)
    sid = _register_owned_slide(monkeypatch)
    stub = _ForwardStub()
    monkeypatch.setattr(app_mod, "requests", stub)
    delegated = _minted_token(_client(), stub, _tool_token())
    claims = _decode_token(delegated)
    small = dict(claims)
    small["roi_max_pixels"] = 100  # 16x16=256 > 100
    small_token = _reencode(small)
    jwt = _plugin_jwt(installation, secret)
    mocks = _slide_read_mocks()
    with mocks[0], mocks[1], mocks[2]:
        r = _post_region(_client(), jwt, sid, delegated=small_token)
    assert r.status_code == 403, r.get_json()
    err = r.get_json()["error"]
    assert err["code"] == "forbidden"
    assert err.get("details", {}).get("reason") == "roi_over_cap"


def test_region_rejects_wrong_installation_presenting_token(monkeypatch):
    installation, secret = _install_with_known_secret(monkeypatch)
    other, other_secret = _install_second_plugin(monkeypatch)
    assert other["installation_id"] != installation["installation_id"]
    sid = _register_owned_slide(monkeypatch)
    stub = _ForwardStub()
    monkeypatch.setattr(app_mod, "requests", stub)
    delegated = _minted_token(_client(), stub, _tool_token())
    # 另一 installation 的合法 plugin JWT 拿着这张凭证来读
    other_jwt = _plugin_jwt(other, other_secret)
    r = _post_region(_client(), other_jwt, sid, delegated=delegated)
    assert r.status_code == 403, r.get_json()
    assert r.get_json()["error"]["code"] == "forbidden"


def test_delegated_token_domain_isolation(monkeypatch):
    """三类同密钥 token 按 aud/typ 互拒：plugin JWT / agent-tool-token 不能
    当委托凭证用；委托凭证也不能调 dispatch。"""
    installation, secret = _install_with_known_secret(monkeypatch)
    sid = _register_owned_slide(monkeypatch)
    stub = _ForwardStub()
    monkeypatch.setattr(app_mod, "requests", stub)
    delegated = _minted_token(_client(), stub, _tool_token())
    jwt = _plugin_jwt(installation, secret)
    agent_token = _tool_token()
    # plugin JWT 冒充委托凭证
    r = _post_region(_client(), jwt, sid, delegated=jwt)
    assert r.status_code == 401
    assert r.get_json()["error"]["code"] == "unauthorized"
    # agent-tool-token 冒充委托凭证
    r = _post_region(_client(), jwt, sid, delegated=agent_token)
    assert r.status_code == 401
    # 委托凭证冒充 agent tool token 调 dispatch → 401（也不落入
    # 「plugin JWT 主体」的 403 分支）
    r = _dispatch(_client(), stub, delegated)
    assert r.status_code == 401


def test_delegated_token_not_usable_for_annotations(monkeypatch):
    """委托凭证不授予写能力：annotations 端点只认 X-Run-Grant run grant。"""
    installation, secret = _install_with_known_secret(monkeypatch)
    sid = _register_owned_slide(monkeypatch)
    stub = _ForwardStub()
    monkeypatch.setattr(app_mod, "requests", stub)
    delegated = _minted_token(_client(), stub, _tool_token())
    jwt = _plugin_jwt(installation, secret)
    r = _client().post("/api/plugin/v1/slides/by-id/%s/annotations" % sid,
                       headers={"Authorization": "Bearer " + jwt,
                                HDR: delegated},
                       json={"label": "x", "x": 0, "y": 0, "side_px": 8,
                             "session_id": "sess-1"})
    assert r.status_code == 403, r.get_json()
    assert r.get_json()["error"]["code"] == "run_grant_invalid"


# --------------------------------------------------------------------------- #
# 3. 既有行为不变（无凭证头 = 旧路径原样）
# --------------------------------------------------------------------------- #
def test_region_run_grant_gate_unchanged_without_credential(monkeypatch):
    """阻塞缺口本身不被本改动放开：认证态下无凭证/无 grant 的普通插件 JWT
    读普通用户切片仍 403（须走委托凭证或既有 grant 通道）。"""
    installation, secret = _install_with_known_secret(monkeypatch)
    sid = _register_owned_slide(monkeypatch)
    monkeypatch.setattr(app_mod, "AUTH_ENABLED", True)
    jwt = _plugin_jwt(installation, secret)
    mocks = _slide_read_mocks()
    with mocks[0], mocks[1], mocks[2]:
        r = _post_region(_client(), jwt, sid, delegated=None)
    assert r.status_code == 403, r.get_json()
    assert r.get_json()["error"]["code"] == "forbidden"


def test_histopilot_region_with_run_grant_still_works(monkeypatch):
    """HistoPilot 既有通道不变：HP installation + X-Run-Grant 头照常读。"""
    import user_store
    inst = app_mod._bootstrap_plugin_installations()
    assert inst is not None
    app_mod._HISTOPILOT_INSTALLATION = inst
    user = user_store.create_user("hp-grant-owner@x.com",
                                  "ownerPass123456", role="user")
    uid = user["user_id"]
    sid = _register_owned_slide(monkeypatch, uid=uid)
    grant = share_store.create_run_grant(
        installation_id=inst["installation_id"], slide="demo.svs",
        session_id="", created_by_user_id=uid, ttl_seconds=600,
        slide_id=sid)
    f = Path(os.environ["SHARE_DATA_DIR"]) / "plugin-secret-histopilot.txt"
    raw = f.read_text(encoding="utf-8").strip()
    try:
        secret = json.loads(raw)["secret"]
    except (ValueError, TypeError):
        secret = raw
    jwt = _plugin_jwt(inst, secret)
    mocks = _slide_read_mocks()
    with mocks[0], mocks[1], mocks[2], \
            mock.patch.object(app_mod, "_read_region_b64",
                              return_value=_region_payload()):
        r = _post_region(_client(), jwt, sid, delegated=None,
                         extra_headers={"X-Run-Grant": grant["grant_id"]})
    assert r.status_code == 200, r.get_data(as_text=True)


# --------------------------------------------------------------------------- #
# 4. id_bundle 行（legacy_filename=NULL，全部新上传的真实形态）
#
# E2E 回归（2026-10-13）：id_bundle 行名通道（get_slide_meta_full
# WHERE legacy_filename=%s）对 original_filename 恒 miss —— 能力不注入、
# dispatch 403、委托凭证不签发。修复：能力注入/dispatch 判权/凭证签发在
# token 携带 slide_id claim 时按 ID 权威解析（_subject_slide_permissions_desc）。
# --------------------------------------------------------------------------- #
def _register_id_bundle_slide(monkeypatch, uid="usr_a", name="26-005817.tif",
                              content=b"idbundle-stub"):
    """登记 id_bundle 切片并去掉夹具回填的冻结别名（复现 E2E 行形态：
    legacy_filename NULL + original_filename=展示名）。"""
    _touch_slide(name, content=content)
    sid = register_slide_row(name)
    share_store.set_slide_meta(name, owner_user_id=uid)
    from _pt_helpers import pg_connection
    conn = pg_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("UPDATE slides SET legacy_filename=NULL "
                        "WHERE slide_id=%s", (sid,))
        conn.commit()
    finally:
        conn.close()
    return sid


def _tool_token_with_sid(slide, slide_id, user_id="usr_a", role="user",
                         capabilities=None):
    """带 slide_id claim 的 tool token（_inject_agent_extra_tools 的产出形态）。"""
    claims = {
        "sub": user_id,
        "user_id": user_id,
        "role": role,
        "session_id": "",
        "slide": slide,
        "slide_id": slide_id,
        "capabilities": capabilities if capabilities is not None else [FULL_NAME],
    }
    return app_mod._agent_tool_token_encode(claims)


def test_id_bundle_slide_capabilities_injected_for_owner(monkeypatch):
    _install_with_known_secret(monkeypatch)
    sid = _register_id_bundle_slide(monkeypatch, uid="usr_a")
    name = "26-005817.tif"
    # owner user：ID 权威判定 → 两个能力都注入（segment_region + count_cells）
    caps = app_mod._list_agent_capabilities(
        {"role": "user", "user_id": "usr_a"}, name, slide_id=sid)
    assert sorted((i["plugin_id"], c["name"]) for i, c in caps) == \
        sorted([(PLUGIN_ID, "segment_region"), (PLUGIN_ID, "count_cells")])
    # 另一用户：同一 id_bundle 切片 → 空（fail-closed，无名回退）
    caps_b = app_mod._list_agent_capabilities(
        {"role": "user", "user_id": "usr_b"}, name, slide_id=sid)
    assert caps_b == []
    # 未知 slide_id → 空集（fail-closed）
    assert app_mod._list_agent_capabilities(
        {"role": "user", "user_id": "usr_a"}, name, slide_id="sld_nope") == []


def test_id_bundle_slide_inject_carries_slide_id_claim(monkeypatch):
    _install_with_known_secret(monkeypatch)
    sid = _register_id_bundle_slide(monkeypatch, uid="usr_a")
    name = "26-005817.tif"
    config = {"max_steps": 20}  # 形如 sidecar config（空 dict 为 falsy 不适用）
    injected = app_mod._inject_agent_extra_tools(
        {"role": "user", "user_id": "usr_a"}, name, config, slide_id=sid)
    assert injected is True
    assert config["extra_tools"], "能力须进入 extra_tools"
    payload, err = app_mod._agent_tool_token_decode(config["tool_token"])
    assert err is None
    assert payload["slide_id"] == sid
    assert payload["slide"] == name  # 名仍随 token（body.slide 绑定不变）
    assert sorted(payload["capabilities"]) == sorted(
        [FULL_NAME, "%s/count_cells" % PLUGIN_ID])


def test_id_bundle_slide_dispatch_succeeds_and_mints_credential(monkeypatch):
    installation, secret = _install_with_known_secret(monkeypatch)
    sid = _register_id_bundle_slide(monkeypatch, uid="usr_a")
    stub = _ForwardStub()
    monkeypatch.setattr(app_mod, "requests", stub)
    token = _tool_token_with_sid("26-005817.tif", sid)
    r = _dispatch(_client(), stub, token, slide="26-005817.tif")
    assert r.status_code == 200, r.get_json()
    delegated = (stub.calls[-1]["headers"] or {}).get(HDR)
    assert delegated, "id_bundle 切片 dispatch 也必须签发委托凭证"
    claims = _decode_token(delegated)
    assert claims["sub"] == installation["installation_id"]
    assert claims["slide_id"] == sid
    assert claims["user_id"] == "usr_a"


def test_id_bundle_slide_dispatch_denies_other_user(monkeypatch):
    _install_with_known_secret(monkeypatch)
    sid = _register_id_bundle_slide(monkeypatch, uid="usr_a")
    stub = _ForwardStub()
    monkeypatch.setattr(app_mod, "requests", stub)
    token = _tool_token_with_sid("26-005817.tif", sid, user_id="usr_b")
    r = _dispatch(_client(), stub, token, slide="26-005817.tif")
    _assert_envelope(r, 403, "permission_denied")
    assert stub.calls == []  # 拒绝前零转发


def test_id_bundle_slide_delegated_read_works(monkeypatch):
    """id_bundle 全链：dispatch 签发 → by-id region 凭证读取 → 200。"""
    installation, secret = _install_with_known_secret(monkeypatch)
    sid = _register_id_bundle_slide(monkeypatch, uid="usr_a")
    stub = _ForwardStub()
    monkeypatch.setattr(app_mod, "requests", stub)
    token = _tool_token_with_sid("26-005817.tif", sid)
    r = _dispatch(_client(), stub, token, slide="26-005817.tif")
    assert r.status_code == 200, r.get_json()
    delegated = (stub.calls[-1]["headers"] or {}).get(HDR)
    assert delegated
    jwt = _plugin_jwt(installation, secret)
    mocks = _slide_read_mocks()
    with mocks[0], mocks[1], mocks[2], \
            mock.patch.object(app_mod, "_read_region_b64",
                              return_value=_region_payload()):
        rr = _post_region(_client(), jwt, sid, delegated=delegated)
    assert rr.status_code == 200, rr.get_data(as_text=True)
    assert rr.get_json()["src"] == {"x": 0, "y": 0, "w": 16, "h": 16}


def test_id_bundle_slide_revoked_owner_denied_at_dispatch(monkeypatch):
    """id_bundle 判权按 slides 行实时复查：mint 后归属改判 → dispatch 拒绝。"""
    _install_with_known_secret(monkeypatch)
    sid = _register_id_bundle_slide(monkeypatch, uid="usr_a")
    from _pt_helpers import pg_connection
    conn = pg_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("UPDATE slides SET owner_user_id='usr_other' "
                        "WHERE slide_id=%s", (sid,))
        conn.commit()
    finally:
        conn.close()
    stub = _ForwardStub()
    monkeypatch.setattr(app_mod, "requests", stub)
    token = _tool_token_with_sid("26-005817.tif", sid)
    r = _dispatch(_client(), stub, token, slide="26-005817.tif")
    _assert_envelope(r, 403, "permission_denied")


def test_legacy_name_slide_still_mints_via_name_path(monkeypatch):
    """legacy 冻结别名行 + 无 slide_id claim 的历史 token：名通道判定不变。"""
    installation, secret = _install_with_known_secret(monkeypatch)
    _register_owned_slide(monkeypatch, "legacy.svs", uid="usr_legacy")
    stub = _ForwardStub()
    monkeypatch.setattr(app_mod, "requests", stub)
    token = _tool_token(slide="legacy.svs", user_id="usr_legacy")
    r = _dispatch(_client(), stub, token, slide="legacy.svs")
    assert r.status_code == 200, r.get_json()
    delegated = (stub.calls[-1]["headers"] or {}).get(HDR)
    assert delegated
    assert _decode_token(delegated)["slide_id"] == \
        share_store.get_slide_id("legacy.svs")


def test_dispatch_plugin_5xx_surfaces_plugin_code_in_details(monkeypatch):
    """插件 5xx 保持 capability_unavailable 契约，细分码经 details 透出。"""
    _install_with_known_secret(monkeypatch)
    _register_owned_slide(monkeypatch)
    stub = _ForwardStub()

    def _handler(*a):
        return _Resp(503, {"error": {"code": "inference_busy",
                                     "message": "GPU busy", "retryable": True}})
    stub.handler = _handler
    monkeypatch.setattr(app_mod, "requests", stub)
    r = _dispatch(_client(), stub, _tool_token())
    assert r.status_code == 503
    err = r.get_json()["error"]
    assert err["code"] == "capability_unavailable"  # 契约码不变
    assert err["retryable"] is True
    assert err["details"]["reason"] == "plugin_5xx"
    assert err["details"]["plugin_code"] == "inference_busy"


# --------------------------------------------------------------------------- #
# 5. P2 双字段转发与插件 4xx 归一（真实 E2E 回归：id_bundle 仅名回调 404 被
#    HP 渲染成「插件已停用」）
# --------------------------------------------------------------------------- #
def test_dispatch_forwards_slide_id_claim_and_rejects_mismatch(monkeypatch):
    """token 的 slide_id claim 进转发 body（权威）；body.slide_id 不符 403。"""
    _install_with_known_secret(monkeypatch)
    sid = _register_owned_slide(monkeypatch)
    stub = _ForwardStub()
    monkeypatch.setattr(app_mod, "requests", stub)
    client = _client()
    token = _tool_token_with_sid("demo.svs", sid)
    r = _dispatch(client, stub, token)
    assert r.status_code == 200, r.get_json()
    assert stub.calls[-1]["json"]["slide_id"] == sid
    # body.slide_id 与 token claim 不一致 → 403（转发前拒绝）
    r = client.post(DISPATCH_PATH,
                    json={"slide": "demo.svs", "slide_id": "sld_other",
                          "arguments": {}},
                    headers={"Authorization": "Bearer " + token,
                             "X-AI-Session": "sess-1"})
    err = _assert_envelope(r, 403, "forbidden")
    assert err.get("details", {}).get("reason") == "slide_id_mismatch"
    assert len(stub.calls) == 1  # 拒绝前只有第一次成功转发


def test_dispatch_plugin_404_maps_retryable_503(monkeypatch):
    """插件回 404（如切片不可读）≠ PT not_found（能力/插件不存在）：归一为
    可重试 503 capability_unavailable——否则 HP 把它渲染成「插件已停用，
    本 run 不再重试」。"""
    _install_with_known_secret(monkeypatch)
    _register_owned_slide(monkeypatch)
    stub = _ForwardStub()
    stub.handler = lambda *a: _Resp(
        404, {"error": {"code": "slide_unavailable",
                        "message": "切片不存在或当前不可读", "retryable": False}})
    monkeypatch.setattr(app_mod, "requests", stub)
    r = _dispatch(_client(), stub, _tool_token())
    assert r.status_code == 503
    err = r.get_json()["error"]
    assert err["code"] == "capability_unavailable"
    assert err["retryable"] is True
    assert err["details"]["plugin_code"] == "slide_unavailable"
    assert err["details"]["plugin_status"] == 404


def test_dispatch_plugin_400_maps_capability_failed(monkeypatch):
    """插件 4xx（参数问题等）：状态保持，code 归一 capability_failed +
    details.plugin_code（不与 PT 保留码冲突）。"""
    _install_with_known_secret(monkeypatch)
    _register_owned_slide(monkeypatch)
    stub = _ForwardStub()
    stub.handler = lambda *a: _Resp(
        400, {"error": {"code": "invalid_request", "message": "参数非法",
                        "retryable": False}})
    monkeypatch.setattr(app_mod, "requests", stub)
    r = _dispatch(_client(), stub, _tool_token())
    assert r.status_code == 400
    err = r.get_json()["error"]
    assert err["code"] == "capability_failed"
    assert err["retryable"] is False
    assert err["details"]["plugin_code"] == "invalid_request"
    assert "参数非法" in err["message"]


def test_id_bundle_slide_denied_when_no_slide_id_claim(monkeypatch):
    """无 slide_id claim 的 token 打 id_bundle 切片：名通道判权恒 miss →
    403（fail-closed，不是放行）。"""
    _install_with_known_secret(monkeypatch)
    _register_id_bundle_slide(monkeypatch, uid="usr_a")
    stub = _ForwardStub()
    monkeypatch.setattr(app_mod, "requests", stub)
    token = _tool_token(slide="26-005817.tif", user_id="usr_a")
    r = _dispatch(_client(), stub, token, slide="26-005817.tif")
    _assert_envelope(r, 403, "permission_denied")


# --------------------------------------------------------------------------- #
# 6. 全链 e2e（dispatch → 真实插件后端 → by-id region → stub 推理）
# --------------------------------------------------------------------------- #
_PT_PLATFORM = "http://127.0.0.1:8000"
_PS_INFER = "http://127.0.0.1:8971"
_CELLPOSE_INFER = "http://127.0.0.1:8972"


class _BackendRouter:
    """插件后端 `requests` 替身：平台地址路由到 PT test client，推理地址
    （PathSegmentor :8971 / Cellpose :8972）按路径返回 stub；其他地址一律
    拒绝（后端不得外联）。"""

    ConnectionError = real_requests.ConnectionError
    Timeout = real_requests.Timeout

    def __init__(self, pt_client, mask_png):
        self.pt = pt_client
        self.mask_png = mask_png
        self.platform_calls = []

    def _route(self, method, url, json=None, headers=None, **_kw):
        if url.startswith(_PT_PLATFORM):
            path = url[len(_PT_PLATFORM):]
            self.platform_calls.append((method, path))
            resp = self.pt.open(path, method=method, json=json,
                                headers=headers)
            try:
                body = resp.get_json()
            except Exception:
                body = {}
            return _Resp(resp.status_code, body)
        if url.startswith(_PS_INFER) and method == "POST":
            return _Resp(200, {"model_id": "psm", "model_sha256": "ab" * 32,
                               "code_rev": "rev-e2e",
                               "prompt_used": "nuclei",
                               "input_width": 8, "input_height": 8,
                               "mask_png_b64": base64.b64encode(
                                   self.mask_png).decode("ascii"),
                               "positive_pixels": 12, "threshold": None,
                               "timings_ms": {"decode": 1, "infer": 2,
                                              "total": 3},
                               "warnings": []})
        if url.startswith(_CELLPOSE_INFER) and method == "POST":
            assert url.endswith("/v1/count"), url
            return _Resp(200, {
                "model_id": "cellpose", "model_name": "nuclei",
                "model_sha256": "cd" * 32, "code_version": "v1.2",
                "input_width": 8, "input_height": 8,
                "instance_count": 2, "diameter_px_used": 14.0,
                "areas_px": {"min": 12.0, "p25": 12.0, "median": 14.0,
                             "p75": 14.0, "max": 14.0},
                "instances_top": [
                    {"bbox": [1, 1, 3, 3], "centroid": [2.5, 2.5],
                     "area_px": 9.0},
                    {"bbox": [4, 4, 2, 2], "centroid": [5.0, 5.0],
                     "area_px": 4.0}],
                "timings_ms": {"decode": 1, "infer": 3, "total": 4},
                "warnings": []})
        raise real_requests.ConnectionError(
            "unexpected backend outbound call: %s %s" % (method, url))

    def post(self, url, json=None, headers=None, timeout=None, **kw):
        return self._route("POST", url, json=json, headers=headers)

    def get(self, url, headers=None, timeout=None, **kw):
        return self._route("GET", url, headers=headers)


class _BackendAdapter:
    """app_mod.requests 替身：把 dispatch 转发路由到插件后端 Flask client。"""

    ConnectionError = real_requests.ConnectionError
    Timeout = real_requests.Timeout

    def __init__(self, backend_mod):
        self.client = backend_mod.app.test_client()
        self.calls = []

    def post(self, url, json=None, headers=None, timeout=None, **kw):
        self.calls.append(url)
        # url = <base_url>/capabilities/<capability_name>——按转发 URL 取路径
        path = "/capabilities/" + url.rsplit("/capabilities/", 1)[-1]
        resp = self.client.post(path, json=json, headers=headers)
        try:
            body = resp.get_json()
        except Exception:
            body = {}
        return _Resp(resp.status_code, body)


def _load_measure_backend(monkeypatch, installation_id, secret, nonce):
    for k, v in (("PT_PLATFORM_URL", _PT_PLATFORM),
                 ("PATHSEGMENTOR_URL", _PS_INFER),
                 ("PT_INSTALLATION_ID", installation_id),
                 ("PT_INSTALLATION_SECRET", secret)):
        monkeypatch.setenv(k, v)
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "psm_backend_e2e_%s" % nonce,
        Path(__file__).resolve().parent.parent / "plugins"
        / "pathsegmentor-measure" / "backend" / "app.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _grid_png(grid):
    """二值 grid → PNG bytes（推理 mask 合同形态）。"""
    import io
    from PIL import Image
    h, w = len(grid), len(grid[0])
    img = Image.new("L", (w, h))
    img.putdata([255 if v else 0 for row in grid for v in row])
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def test_id_bundle_e2e_dispatch_backend_by_id_region(monkeypatch):
    """全链：dispatch（签发凭证+转发 slide_id）→ 插件后端 → 平台 by-id
    region（mocked 读栈）→ stub 推理 → 摘要。8b22db45 上失败：转发无
    slide_id，后端按名回调 → 平台 404。"""
    installation, secret = _install_with_known_secret(monkeypatch)
    sid = _register_id_bundle_slide(monkeypatch, uid="usr_a")
    backend = _load_measure_backend(monkeypatch, installation["installation_id"],
                                    secret, nonce="e2e1")
    blob = [[1 if (1 <= y <= 3 and 1 <= x <= 4) else 0 for x in range(8)]
            for y in range(8)]
    router = _BackendRouter(_client(), _grid_png(blob))
    monkeypatch.setattr(backend, "requests", router)
    adapter = _BackendAdapter(backend)
    monkeypatch.setattr(app_mod, "requests", adapter)
    # 平台 region/slide-info 读栈 mock（真实读路径在单元层已覆盖）
    mocks = _slide_read_mocks()
    with mocks[0], mocks[1], mocks[2], \
            mock.patch.object(app_mod, "_read_region_b64",
                              return_value=_region_payload(w=8, h=8)):
        r = _dispatch(_client(), adapter,
                      _tool_token_with_sid("26-005817.tif", sid),
                      slide="26-005817.tif",
                      arguments={"x": 0, "y": 0, "w": 16, "h": 16,
                                 "object": "nuclei"})
    assert r.status_code == 200, r.get_data(as_text=True)
    result = r.get_json()["result"]
    assert result["plugin"] == "dev.pathsegmentor.measure"
    assert result["slide_id"] == sid
    assert result["model"]["model_id"] == "psm"
    aoc = result["approx_object_count"]
    assert aoc["connected_components"] >= 1
    assert "estimated_count_split" in aoc
    text = r.get_data(as_text=True)
    assert "mask_png_b64" not in text
    assert len(r.get_data()) < 64 * 1024
    # 后端确实走了 by-id 路由（平台侧收到 by-id region 调用）
    assert any(m == "POST" and "/regions" in p and "by-id" in p
               for m, p in router.platform_calls)


def test_dispatch_mints_credential_for_count_cells(monkeypatch):
    """count_cells 声明 slide:region:read → dispatch 按最小披露门槛随转发
    签发委托凭证（ecaf30db gating 对第二个能力同样生效）。

    2026-10-14 方形取景：count_cells 另声明 context=current_snapshot，
    dispatch 先验证签名 snapshot_context（本用例按合同 wire 形态真签），
    通过后才到达凭证签发点——上下文路径不改变委托凭证的最小披露。"""
    _install_with_known_secret(monkeypatch)
    sid = _register_owned_slide(monkeypatch)
    # 网关 bbox 界内检查的切片尺寸钉桩（维度读取与本用例无关）
    monkeypatch.setattr(app_mod, "_dispatch_slide_level0_dims",
                        lambda gate: (4096, 4096))
    stub = _ForwardStub()
    monkeypatch.setattr(app_mod, "requests", stub)
    token = _tool_token(capabilities=["%s/count_cells" % PLUGIN_ID])
    # review 2026-10-14 修复 1：当前 revision 已知时 rev 必须一致（null 会被
    # 409 snapshot_stale 拒绝）——按登记行真实 revision 签名。
    desc = app_mod.slide_store.resolve_slide_id(sid)
    rev = app_mod._slide_revision(desc)
    r = _client().post(
        "/api/plugin/v1/dispatch/%s/count_cells" % PLUGIN_ID,
        json={"slide": "demo.svs",
              "arguments": {},
              "snapshot_context": make_snapshot_context(app_mod.AI_INTERNAL_TOKEN,
                  sid="sess-1", slide_id=sid, snap="snap-1",
                  bbox=[0, 0, 1000, 1000], rev=rev or None)},
        headers={"Authorization": "Bearer " + token,
                 "X-AI-Session": "sess-1"})
    assert r.status_code == 200, r.get_json()
    delegated = (stub.calls[-1]["headers"] or {}).get(HDR)
    assert delegated, "count_cells 也必须随转发拿到委托凭证"
    claims = _decode_token(delegated)
    assert claims["slide_id"] == sid
    assert claims["capability"] == "count_cells"


def test_id_bundle_e2e_count_cells_dispatch_backend_by_id(monkeypatch):
    """count_cells 全链：dispatch → 后端 → by-id region → stub /v1/count。
    8b22db45/2523c64a 上失败：count_cells 未登记（dispatch 404）。"""
    installation, secret = _install_with_known_secret(monkeypatch)
    sid = _register_id_bundle_slide(monkeypatch, uid="usr_a")
    backend = _load_measure_backend(monkeypatch, installation["installation_id"],
                                    secret, nonce="e2e2")
    router = _BackendRouter(_client(), b"")  # count 路径不用 mask
    monkeypatch.setattr(backend, "requests", router)
    adapter = _BackendAdapter(backend)
    monkeypatch.setattr(app_mod, "requests", adapter)
    mocks = _slide_read_mocks()
    # 方形取景（2026-10-14）：快照 16² 中心方形、原生 0.5 渲染（out==src==16）
    # ——平台按合同对界内方形恒渲染恰好请求边（不再模拟 16→8 的越契约渲染）。
    desc = app_mod.slide_store.resolve_slide_id(sid)
    rev = app_mod._slide_revision(desc)
    with mocks[0], mocks[1], mocks[2], \
            mock.patch.object(app_mod, "_read_region_b64",
                              return_value=_region_payload(w=16, h=16)):
        r = _client().post(
            "/api/plugin/v1/dispatch/%s/count_cells" % PLUGIN_ID,
            json={"slide": "26-005817.tif",
                  "slide_id": sid,
                  "arguments": {"resolution": 2048, "diameter_um": 7},
                  "snapshot_context": make_snapshot_context(app_mod.AI_INTERNAL_TOKEN,
                      sid="sess-1", slide_id=sid, snap="snap-e2e",
                      bbox=[0, 0, 16, 16], rev=rev or None)},
            headers={"Authorization": "Bearer " + _tool_token_with_sid(
                        "26-005817.tif", sid,
                        capabilities=["%s/count_cells" % PLUGIN_ID]),
                     "X-AI-Session": "sess-1"})
    assert r.status_code == 200, r.get_data(as_text=True)
    result = r.get_json()["result"]
    assert result["capability"] == "count_cells"
    assert result["instance_count"] == 2
    assert result["slide_id"] == sid
    # counted_region：快照内自动取的方形（16² 短边封顶、中心定位）
    cr = result["counted_region"]
    assert cr["bbox"] == {"x": 0, "y": 0, "w": 16, "h": 16}
    assert cr["snapshot_id"] == "snap-e2e"
    assert cr["reduced"] is False
    # 密度：ROI = (16·0.5/1000)² mm² = 6.4e-5；2 / 6.4e-5 = 31250（不变：
    # 原生像素渲染时 src 16 与原夹具的 level-0 bbox 同一物理）
    assert result["density"]["per_mm2"] == pytest.approx(31250.0)
    # 实例映射回 level-0（scale 1，src (0,0)：原生渲染下 1:1）
    inst0 = result["instances_top"][0]
    assert inst0["bbox_l0"] == [1, 1, 3, 3]
    assert result["model"]["model_id"] == "cellpose"
    text = r.get_data(as_text=True)
    assert len(r.get_data()) < 64 * 1024
    assert any(m == "POST" and "/regions" in p and "by-id" in p
               for m, p in router.platform_calls)


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
