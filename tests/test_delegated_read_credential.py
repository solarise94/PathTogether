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
from _pt_helpers import isolate_app, register_slide_row  # noqa: E402

app_mod.UPLOAD_DIR = Path(os.environ["UPLOAD_DIR"])
app_mod.UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

import pytest  # noqa: E402

PLUGIN_DIR_KEY = "pathsegmentor-measure"
PLUGIN_ID = "dev.pathsegmentor.measure"
CAP_NAME = "segment_region"
FULL_NAME = "%s/%s" % (PLUGIN_ID, CAP_NAME)
DISPATCH_PATH = "/api/plugin/v1/dispatch/%s/%s" % (PLUGIN_ID, CAP_NAME)
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
                              return_value={"mpp_x": mpp}))


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
    # body 形状不变（凭证只走头，不进转发 body）
    assert set(stub.calls[-1]["json"].keys()) == {"slide", "arguments"}


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
    # owner user：ID 权威判定 → 能力注入
    caps = app_mod._list_agent_capabilities(
        {"role": "user", "user_id": "usr_a"}, name, slide_id=sid)
    assert [(i["plugin_id"], c["name"]) for i, c in caps] == \
        [(PLUGIN_ID, CAP_NAME)]
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
    assert payload["capabilities"] == [FULL_NAME]


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


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
