# -*- coding: utf-8 -*-
"""count_cells 方形取景：snapshot_context 网关验证 + context 登记链（wire
合同 v1，2026-10-14）。

覆盖（跨仓合同 §1/§2/§4；HP 侧按同一文本实现）：
  - SDK 校验层：provides[].context 仅接受 current_snapshot（其他值拒绝，
    fail-closed）；缺省不受影响；
  - 登记层：_parse_provides_registry 落盘 context；
  - 注入层：声明 context 的能力在 extra_tools 条目带 "context":
    "current_snapshot"（wire 合同 §2）；未声明的不写键；
  - dispatch 网关（§4）：缺 snapshot_context → 400
    snapshot_context_required；验签（对收到的 payload 字节、域
    hp-snapshot-context-v1 与写入 provenance 域分离）→ 伪造/过期/未来
    exp/跨会话/跨切片/越界 bbox → 403 snapshot_context_invalid（不回显
    签名细节）；revision 不符 → 409 snapshot_stale；通过 → 转发体带
    结构化 context.snapshot（原始签名串不出网关）；未声明 context 的
    能力即使请求带 snapshot_context 也丢弃不转发；
  - 只读：上下文验证路径不签发描绘/写授权（转发头恒无写凭证）。

签名用真实 HMAC（与合同同 wire 格式的小 helper，镜像 HP
signSnapshotContext）。运行：cd 项目根 && python3 -m pytest
tests/test_plugin_snapshot_context.py -q
"""
import base64
import hashlib
import hmac as hmac_mod
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import _bootstrap  # noqa: E402,F402  # session 目录+openslide stub（conftest 先行）
UPLOAD_DIR = _bootstrap.UPLOAD_DIR
import requests as real_requests  # noqa: E402

import app as app_mod  # noqa: E402
import share_store  # noqa: E402
from _pt_helpers import isolate_app, register_slide_row  # noqa: E402
from plugins.sdk import manifest as M  # noqa: E402

app_mod.UPLOAD_DIR = Path(os.environ["UPLOAD_DIR"])
app_mod.UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

import pytest  # noqa: E402

SLIDE = "demo.svs"
PLUGIN_ID = "dev.context.test"
CAP = "count_here"
FULL_NAME = "%s/%s" % (PLUGIN_ID, CAP)
DISPATCH_PATH = "/api/plugin/v1/dispatch/%s/%s" % (PLUGIN_ID, CAP)
CONTEXT_DOMAIN = b"hp-snapshot-context-v1"
ATTEST_DOMAIN = b"hp-snapshot-attest-v1"
REVISION = "sha256:rev-1"
SLIDE_DIMS = (20000, 20000)
SLIDE_ID = "sld_fixture"  # fixture 内按登记行回填（P2 ID 权威）


# --------------------------------------------------------------------------- #
# 合同同款签名 helper（镜像 HP signSnapshotContext 的 wire 形状）
# --------------------------------------------------------------------------- #
def sign_snapshot_context(key, *, sid, slide_id, snap, bbox, rev, fp,
                          exp_delta=60, domain=CONTEXT_DOMAIN):
    """wire 合同 v1 §3.4：``v1.<b64url(payload_json)>.<b64url(hmac)>``，
    MAC 覆盖 ``domain + "\\n" + payload 原始字节``。bbox 为 [x,y,w,h] 整数。"""
    payload = json.dumps(
        {"sid": sid, "slide_id": slide_id, "snap": snap, "bbox": bbox,
         "rev": rev, "fp": fp, "exp": int(time.time() + exp_delta)},
        separators=(",", ":"))
    payload_bytes = payload.encode("utf-8")
    mac = hmac_mod.new(key, domain + b"\n" + payload_bytes,
                       hashlib.sha256).digest()

    def _b64(raw):
        return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")
    return "v1.%s.%s" % (_b64(payload_bytes), _b64(mac))


def _valid_context(**overrides):
    args = dict(sid="sess-1", slide_id=SLIDE_ID, snap="snap-1",
                bbox=[100, 200, 500, 500], rev=REVISION,
                fp="f" * 64, exp_delta=60)
    args.update(overrides)
    exp_delta = args.pop("exp_delta")
    domain = args.pop("domain", CONTEXT_DOMAIN)
    return sign_snapshot_context(app_mod.AI_INTERNAL_TOKEN.encode("utf-8"),
                                 exp_delta=exp_delta, domain=domain, **args)


# --------------------------------------------------------------------------- #
# manifest / 安装 / 隔离
# --------------------------------------------------------------------------- #
def _manifest(provides):
    return {
        "manifestSchemaVersion": "1.0.0",
        "id": PLUGIN_ID,
        "name": "Context Test Plugin",
        "pluginVersion": "0.1.0",
        "pluginContractVersion": "1.0.0",
        "bridgeProtocolVersion": "1.0.0",
        "ui": {"entry": "/plugin/index.html", "slots": ["viewer.right-panel"]},
        "service": {"baseUrl": "http://127.0.0.1:8063", "health": "/healthz"},
        "permissions": ["slide:metadata:read", "slide:region:read"],
        "provides": provides,
    }


def _capability(name, **overrides):
    base = {
        "name": name,
        "version": "1.0.0",
        "description": "只读能力占位描述，用于快照上下文网关测试。",
        "parameters": {"type": "object", "properties": {}, "required": []},
        "accessMode": "read",
        "requiredPermissions": ["slide:metadata:read"],
        "timeout_ms": 15000,
    }
    base.update(overrides)
    return base


class _Resp:
    def __init__(self, status_code, body):
        self.status_code = status_code
        self._body = body

    def json(self):
        return self._body


class _ForwardStub:
    """记录平台 → 插件的转发调用（url/body/headers）。"""

    ConnectionError = real_requests.ConnectionError
    Timeout = real_requests.Timeout

    def __init__(self):
        self.calls = []
        self.handler = None  # fn(url, body, headers, timeout) -> _Resp

    def post(self, url, json=None, headers=None, timeout=None, **kw):
        self.calls.append({"url": url, "json": json, "headers": headers,
                           "timeout": timeout})
        if self.handler is not None:
            return self.handler(url, json, headers, timeout)
        return _Resp(200, {"result": {"ok": True}})


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    """每用例独立存储 + AUTH_ENABLED=False + 全新限流器 + 转发 stub +
    切片尺寸/revision 解析钉桩（dispatch 上下文路径的维度/revision 来源）。"""
    isolate_app(monkeypatch, tmp_path)
    monkeypatch.setattr(app_mod, "AUTH_ENABLED", False)
    monkeypatch.setattr(app_mod, "_DISPATCH_RATE_LIMITER",
                        app_mod._PluginRateLimiter(
                            app_mod._PLUGIN_DISPATCH_RATE_LIMIT_PER_MIN))
    stub = _ForwardStub()
    monkeypatch.setattr(app_mod, "requests", stub)
    # 读隔离（review P0）：owner 主体也需对 slide 有 view（§6.1 映射入口）。
    # 建可读资产行（id_bundle ready + 冻结别名）并回填归属——P2 ID 权威：
    # token claim 与签名上下文都用登记行的稳定 slide_id。
    global SLIDE_ID
    (app_mod.UPLOAD_DIR / SLIDE).parent.mkdir(parents=True, exist_ok=True)
    (app_mod.UPLOAD_DIR / SLIDE).write_bytes(b"svs-stub")
    register_slide_row(SLIDE)
    share_store.set_slide_meta(SLIDE, owner_user_id="owner")
    SLIDE_ID = share_store.get_slide_id(SLIDE)
    assert SLIDE_ID
    monkeypatch.setattr(app_mod, "_dispatch_slide_level0_dims",
                        lambda gate: SLIDE_DIMS)
    monkeypatch.setattr(app_mod, "_slide_revision",
                        lambda desc_or_name, path=None: REVISION)
    yield
    app_mod._plugin_source_policy.cache_clear()


def _install(context_value="current_snapshot", name=CAP):
    cap = _capability(name)
    if context_value is not None:
        cap["context"] = context_value
    caps = app_mod._parse_provides_registry(_manifest([cap]))
    created = share_store.create_plugin_installation(PLUGIN_ID,
                                                     capabilities=caps)
    return created


def _client():
    app_mod.app.config["TESTING"] = True
    return app_mod.app.test_client()


def _tool_token(slide=SLIDE, slide_id=None, capabilities=None,
                session_id="", role="owner", user_id="owner"):
    claims = {
        "sub": user_id, "user_id": user_id, "role": role,
        "session_id": session_id, "slide": slide,
        "slide_id": SLIDE_ID if slide_id is None else slide_id,
        "capabilities": capabilities if capabilities is not None
        else [FULL_NAME],
    }
    return app_mod._agent_tool_token_encode(claims)


def _dispatch(client, token, body=None, session_id="sess-1"):
    return client.post(
        DISPATCH_PATH,
        json=body if body is not None else
        {"slide": SLIDE, "slide_id": SLIDE_ID, "arguments": {},
         "snapshot_context": _valid_context()},
        headers={"Authorization": "Bearer " + token,
                 "X-AI-Session": session_id})


def _err(r):
    body = r.get_json() or {}
    return body.get("error", {})


# --------------------------------------------------------------------------- #
# 1. SDK 校验层 + 登记层 + 注入层（合同 §1/§2）
# --------------------------------------------------------------------------- #
def test_sdk_context_enum():
    assert M.validate_manifest(_manifest([
        _capability("cap_a", context="current_snapshot")])) == []
    assert M.validate_manifest(_manifest([_capability("cap_a")])) == []
    assert M.validate_manifest(
        _manifest([_capability("cap_a", context=None)])) == []
    for bad in ("snapshot", "pending_snapshot", "CURRENT_SNAPSHOT", "", 1,
                True, [], {}):
        errs = M.validate_manifest(_manifest([_capability("cap_a",
                                                          context=bad)]))
        assert errs and any("context" in e for e in errs), \
            "context=%r 必须拒绝（唯一合法值 current_snapshot）" % (bad,)


def test_registry_persists_context():
    caps = app_mod._parse_provides_registry(_manifest([
        _capability("with_ctx", context="current_snapshot"),
        _capability("without_ctx"),
    ]))
    assert [c["context"] for c in caps] == ["current_snapshot", None]


def test_extra_tools_entry_carries_context():
    _install()
    config = {"base_url": "x"}
    assert app_mod._inject_agent_extra_tools({}, SLIDE, config) is True
    entry = [t for t in config["extra_tools"]
             if t["name"].endswith("__" + CAP)][0]
    assert entry["context"] == "current_snapshot"
    # 未声明 context 的能力条目不带该键
    _install(context_value=None, name="plain")
    config2 = {"base_url": "x"}
    assert app_mod._inject_agent_extra_tools({}, SLIDE, config2) is True
    plain = [t for t in config2["extra_tools"]
             if t["name"].endswith("__plain")][0]
    assert "context" not in plain


# --------------------------------------------------------------------------- #
# 2. dispatch 网关（合同 §4）
# --------------------------------------------------------------------------- #
def test_dispatch_context_missing_returns_400():
    _install()
    client = _client()
    r = _dispatch(client, _tool_token(),
                  body={"slide": SLIDE, "slide_id": SLIDE_ID,
                        "arguments": {}})
    assert r.status_code == 400, r.get_json()
    assert _err(r)["code"] == "snapshot_context_required"


def test_dispatch_valid_context_forwarded_as_structured_snapshot():
    created = _install()
    client = _client()
    r = _dispatch(client, _tool_token())
    assert r.status_code == 200, r.get_json()
    app_mod.requests.calls  # 转发 stub 已记录
    (call,) = app_mod.requests.calls
    body = call["json"]
    # 转发体：结构化 context.snapshot（合同 §4），原始签名串不出网关
    snap = body["context"]["snapshot"]
    assert snap["snapshot_id"] == "snap-1"
    assert snap["bbox"] == {"x": 100, "y": 200, "w": 500, "h": 500}
    assert snap["slide_revision"] == REVISION
    assert snap["render_context_fingerprint"] == "f" * 64
    assert "snapshot_context" not in body
    # 只读：转发头无任何描绘/写授权（只有 principal + 委托读凭证按既有
    # 最小披露门槛签发；本能力 requiredPermissions 不含 region:read 时无凭证）
    assert "X-Delegated-Read-Token" not in call["headers"]


def test_dispatch_forged_context_rejected_403():
    _install()
    client = _client()
    # 错误密钥（不是 internal token）签名
    forged = sign_snapshot_context(b"attacker-key", sid="sess-1",
                                   slide_id=SLIDE_ID, snap="snap-1",
                                   bbox=[0, 0, 10, 10], rev=None, fp=None)
    r = _dispatch(client, _tool_token(),
                  body={"slide": SLIDE, "slide_id": SLIDE_ID,
                        "arguments": {}, "snapshot_context": forged})
    assert r.status_code == 403
    err = _err(r)
    assert err["code"] == "snapshot_context_invalid"
    # 不回显签名细节
    assert "hmac" not in err["message"].lower()
    assert app_mod.requests.calls == []


def test_dispatch_wrong_domain_token_rejected_403():
    """域分离：写入 provenance 域（hp-snapshot-attest-v1）签的令牌不能当
    读上下文用，反之亦然。"""
    _install()
    client = _client()
    attest = sign_snapshot_context(app_mod.AI_INTERNAL_TOKEN.encode("utf-8"),
                                   sid="sess-1", slide_id=SLIDE_ID,
                                   snap="snap-1", bbox=[0, 0, 10, 10],
                                   rev=None, fp=None, domain=ATTEST_DOMAIN)
    r = _dispatch(client, _tool_token(),
                  body={"slide": SLIDE, "slide_id": SLIDE_ID,
                        "arguments": {}, "snapshot_context": attest})
    assert r.status_code == 403
    assert _err(r)["code"] == "snapshot_context_invalid"
    assert app_mod.requests.calls == []


def test_dispatch_expired_context_rejected_403():
    _install()
    client = _client()
    r = _dispatch(client, _tool_token(),
                  body={"slide": SLIDE, "slide_id": SLIDE_ID,
                        "arguments": {},
                        "snapshot_context": _valid_context(exp_delta=-1)})
    assert r.status_code == 403
    assert _err(r)["code"] == "snapshot_context_invalid"
    # 超过 TTL+60s 的未来值同样拒绝（伪造/时钟滥用窗口）
    r = _dispatch(client, _tool_token(),
                  body={"slide": SLIDE, "slide_id": SLIDE_ID,
                        "arguments": {},
                        "snapshot_context": _valid_context(exp_delta=600)})
    assert r.status_code == 403
    assert _err(r)["code"] == "snapshot_context_invalid"
    assert app_mod.requests.calls == []


def test_dispatch_cross_session_and_cross_slide_rejected_403():
    _install()
    client = _client()
    r = _dispatch(client, _tool_token(),
                  body={"slide": SLIDE, "slide_id": SLIDE_ID,
                        "arguments": {},
                        "snapshot_context": _valid_context(sid="other-sess")})
    assert r.status_code == 403
    assert _err(r)["code"] == "snapshot_context_invalid"
    r = _dispatch(client, _tool_token(),
                  body={"slide": SLIDE, "slide_id": SLIDE_ID,
                        "arguments": {},
                        "snapshot_context": _valid_context(
                            slide_id="sld_other")})
    assert r.status_code == 403
    assert _err(r)["code"] == "snapshot_context_invalid"
    assert app_mod.requests.calls == []


def test_dispatch_bbox_out_of_slide_bounds_rejected_403():
    _install()
    client = _client()
    for bbox in ([0, 0, 0, 10], [-1, 0, 10, 10], [0, 0, 50000, 10],
                 [19999, 0, 10, 10], [0, 0, 10, 20001], "x"):
        r = _dispatch(client, _tool_token(),
                      body={"slide": SLIDE, "slide_id": SLIDE_ID,
                            "arguments": {},
                            "snapshot_context": _valid_context(bbox=bbox)})
        assert r.status_code == 403, bbox
        assert _err(r)["code"] == "snapshot_context_invalid", bbox
    assert app_mod.requests.calls == []


def test_dispatch_stale_revision_returns_409():
    _install()
    client = _client()
    r = _dispatch(client, _tool_token(),
                  body={"slide": SLIDE, "slide_id": SLIDE_ID,
                        "arguments": {},
                        "snapshot_context": _valid_context(rev="sha256:old")})
    assert r.status_code == 409
    err = _err(r)
    assert err["code"] == "snapshot_stale"
    assert "goto" in err["message"]
    assert app_mod.requests.calls == []


def test_dispatch_non_context_capability_drops_snapshot_context():
    """未声明 context 的能力：请求带 snapshot_context 也丢弃不转发（且不作
    任何验证——不需要快照的能力行为完全不变）。"""
    _install(context_value=None)
    client = _client()
    r = _dispatch(client, _tool_token(),
                  body={"slide": SLIDE, "slide_id": SLIDE_ID,
                        "arguments": {},
                        "snapshot_context": "garbage.v1.x"})
    assert r.status_code == 200
    (call,) = app_mod.requests.calls
    assert "snapshot_context" not in call["json"]
    assert "context" not in call["json"]


def test_dispatch_read_only_no_write_grant():
    """上下文只用于读：验证通过也不签发描绘/写授权（转发头无写凭证；
    插件仍只拿到既有只读链路的委托读凭证）。"""
    _install()
    client = _client()
    r = _dispatch(client, _tool_token())
    assert r.status_code == 200
    (call,) = app_mod.requests.calls
    for header in call["headers"]:
        assert "write" not in header.lower()
        assert "draw" not in header.lower()


def test_dispatch_end_to_end_with_measure_backend(monkeypatch):
    """端到端：真实 pathsegmentor-measure 后端（Flask test client 承接转发）
    ← 带有效签名的 dispatch 请求 → 后端按 context.snapshot 自动取方形并
    返回 counted_region（快照身份透传）。"""
    installation, err = app_mod.install_plugin_bundle("pathsegmentor-measure")
    assert err is None, err
    # 登记行的 count_cells 带 context（manifest v2.0.0 已声明）
    cap = [c for c in installation["capabilities"]
           if c["name"] == "count_cells"][0]
    assert cap["context"] == "current_snapshot"

    # 真后端加载 + 平台回调钉桩（dims/revision 走网关桩的同源值）
    import importlib.util
    monkeypatch.setenv("PT_INSTALLATION_ID", "pi_ctx_e2e")
    monkeypatch.setenv("PT_INSTALLATION_SECRET", "sec_ctx_e2e")
    spec = importlib.util.spec_from_file_location(
        "ctx_e2e_backend",
        Path(__file__).resolve().parent.parent
        / "plugins/pathsegmentor-measure/backend/app.py")
    backend = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(backend)
    monkeypatch.setattr(backend, "_installation_jwt", lambda: "jwt-stub")
    monkeypatch.setattr(
        backend, "_platform_slide_info",
        lambda slide, jwt, slide_id=None, delegated=None:
        {"width": SLIDE_DIMS[0], "height": SLIDE_DIMS[1], "mpp": 0.25,
         "asset_revision": REVISION})

    def _read(slide, args, edge, delegated, jwt, slide_id=None):
        out = max(1, min(int(edge), 4096))
        return b"img", {
            "image_base64": base64.b64encode(b"img").decode("ascii"),
            "mime": "image/jpeg", "width": out, "height": out,
            "src": {"x": args["x"], "y": args["y"],
                    "w": args["w"], "h": args["h"]},
            "magnification": 20.0, "read_level": 0, "upsampled": False,
            "asset_revision": REVISION,
        }
    monkeypatch.setattr(backend, "_platform_read_region", _read)
    monkeypatch.setattr(backend, "_infer_cellpose",
                        lambda *a, **k: {"instance_count": 0,
                                         "areas_px": {},
                                         "instances_top": []})

    forward_handler_calls = []

    def handler(url, body, headers, timeout):
        forward_handler_calls.append(body)
        with backend.app.test_client() as bc:
            resp = bc.post(
                "/capabilities/count_cells",
                json=body,
                headers={"X-Dispatch-Principal":
                         headers["X-Dispatch-Principal"],
                         "X-Delegated-Read-Token":
                         headers.get("X-Delegated-Read-Token", "d")})
        return _Resp(resp.status_code, resp.get_json())

    app_mod.requests.handler = handler
    client = _client()
    snap_bbox = [1000, 2000, 4000, 4000]
    r = client.post(
        "/api/plugin/v1/dispatch/dev.pathsegmentor.measure/count_cells",
        json={"slide": SLIDE, "slide_id": SLIDE_ID,
              "arguments": {"resolution": 512},
              "snapshot_context": _valid_context(
                  snap="snap-e2e", bbox=snap_bbox)},
        headers={"Authorization": "Bearer " + _tool_token(capabilities=[
            "dev.pathsegmentor.measure/count_cells"]),
            "X-AI-Session": "sess-1"})
    assert r.status_code == 200, r.get_json()
    result = r.get_json()["result"]
    cr = result["counted_region"]
    # 后端在快照内自动取方形（512 档真实物理最大），快照身份透传
    assert cr["snapshot_id"] == "snap-e2e"
    assert cr["bbox"]["w"] == cr["bbox"]["h"]
    assert cr["bbox"]["x"] >= snap_bbox[0]
    assert cr["bbox"]["y"] >= snap_bbox[1]
    assert cr["bbox"]["x"] + cr["bbox"]["w"] <= snap_bbox[0] + snap_bbox[2]
    assert cr["bbox"]["y"] + cr["bbox"]["h"] <= snap_bbox[1] + snap_bbox[3]
    assert cr["render_context_fingerprint"] == "f" * 64
    # 推理调用确实发生（走了完整链路）
    assert result["capability"] == "count_cells"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
