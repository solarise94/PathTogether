# -*- coding: utf-8 -*-
"""P0-C AI 完整性测试（安全复核方案 §3.8 / §3.10 / §6.3）。

覆盖：
  1. /api/ai/cancel 权限：user 只能按 session_id 取消自己的 run；slide 分支
     仅 owner / 内部兼容；A、B 协作同一切片时 B 不能取消 A 的 session；
  2. run grant 生命周期：
     - verify/annotate 校验 installation / slide / session（原子绑定）/ 创建者
       （账号存在、未禁用、仍有 annotate 权限）任一不符 → 拒绝；
     - bind 端点：CAS 绑定、幂等、绑定冲突 409、非法 session 400；
     - 机器端撤销只按 Bearer installation 匹配（不再混用 current_identity）；
     - 人类撤销走 Cookie+CSRF 的 /api/ai/run-grants/<id>（owner 或创建者）；
     - 默认 TTL 从 2h 降至 30min；
     - 主动撤销路径：run 被拒、run 结束（上游 SSE 正常关流）、cancel、session
       归档、协作 share 撤销、项目归档；用户禁用由写前复查兜底拒绝。

json / pg 双后端通用（RUN_PG_TESTS=1 时 conftest 已切 postgres 并逐用例
TRUNCATE）。运行：cd 项目根 && python3 -m pytest tests/test_ai_integrity.py -q
"""
import json
import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TMP = tempfile.mkdtemp(prefix="svs-ai-integrity-")
DATA_DIR = os.path.join(TMP, "share-data")
UPLOAD_DIR = os.path.join(TMP, "uploads")
os.environ["SHARE_DATA_DIR"] = DATA_DIR
os.environ["UPLOAD_DIR"] = UPLOAD_DIR
os.makedirs(DATA_DIR, exist_ok=True)
os.makedirs(UPLOAD_DIR, exist_ok=True)
os.environ["AI_SIDECAR_URL"] = "http://127.0.0.1:8055"
os.environ["ADMIN_PASSWORD"] = ""

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

import pytest  # noqa: E402

import app as app_mod  # noqa: E402
import share_store  # noqa: E402
import user_store  # noqa: E402
from _pt_helpers import csrf_client, install_json_login_limits  # noqa: E402

app_mod.UPLOAD_DIR = Path(UPLOAD_DIR)
app_mod.UPLOAD_DIR.mkdir(parents=True, exist_ok=True)


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    """每用例独立存储目录 + 假 sidecar requests + AI 配置复位。"""
    monkeypatch.setenv("SHARE_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(share_store, "SHARE_DATA_DIR", tmp_path)
    monkeypatch.setattr(share_store, "SHARE_FILE", tmp_path / "shares.json")
    monkeypatch.setattr(user_store, "SHARE_DATA_DIR", tmp_path)
    monkeypatch.setattr(user_store, "USER_FILE", tmp_path / "users.json")
    monkeypatch.setattr(app_mod, "UPLOAD_DIR", Path(UPLOAD_DIR))
    monkeypatch.setattr(app_mod, "AUTH_ENABLED", False)
    share_store.set_owner_user_id("")
    install_json_login_limits(monkeypatch)
    for child in Path(UPLOAD_DIR).iterdir():
        if child.is_file():
            child.unlink()
    yield


# --------------------------------------------------------------------------- #
# 基建：fake sidecar / 用户 / 切片 / grant
# --------------------------------------------------------------------------- #
class FakeResponse:
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
            self.content = content if isinstance(content, bytes) else content.encode()
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

    def get_json(self, silent=False):
        try:
            return json.loads(self.content.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return None

    def json(self):
        """requests 兼容：r.json()（_ai_session_owner 等平台路径使用）。"""
        data = self.get_json(silent=True)
        if data is None:
            raise ValueError("No JSON object could be decoded")
        return data


class FakeRequests:
    ConnectionError = __import__("requests").ConnectionError
    Timeout = __import__("requests").Timeout

    def __init__(self):
        self._routes = {}
        self.calls = []

    def register(self, method, path, handler):
        self._routes[(method.upper(), path)] = handler

    def register_json(self, method, path, status=200, body=None, headers=None):
        payload = json.dumps(body if body is not None else {"ok": True}).encode()
        self._routes[(method.upper(), path)] = (
            lambda b, q, h, k: FakeResponse(status, payload, headers=headers))

    def _dispatch(self, method, url, **kwargs):
        base = app_mod.AI_SIDECAR_URL
        path = url[len(base):] if url.startswith(base) else url
        self.calls.append({
            "method": method, "path": path, "body": kwargs.get("json"),
            "query": kwargs.get("params"), "headers": kwargs.get("headers") or {},
        })
        handler = self._routes.get((method, path))
        if handler is None:
            return FakeResponse(404, json.dumps({"error": "no route"}).encode(),
                                headers={"Content-Type": "application/json"})
        return handler(kwargs.get("json"), kwargs.get("params"),
                       kwargs.get("headers") or {}, kwargs)

    def get(self, url, **kwargs):
        return self._dispatch("GET", url, **kwargs)

    def post(self, url, **kwargs):
        return self._dispatch("POST", url, **kwargs)


@pytest.fixture()
def fake_sidecar(monkeypatch):
    fake = FakeRequests()
    monkeypatch.setattr(app_mod, "requests", fake)
    return fake


def _client(auth_enabled=False):
    app_mod.app.config["TESTING"] = True
    app_mod.AUTH_ENABLED = auth_enabled
    return csrf_client(app_mod.app.test_client())


def _login(client, user):
    """以给定 user_store 用户行建立登录 session（回查通过 _require_auth）。"""
    with client.session_transaction() as s:
        s["auth_user"] = user.get("email") or user.get("user_id")
        s["user_id"] = user["user_id"]
        s["role"] = user.get("role") or "user"
        s["auth_version"] = user.get("auth_version", 1)
    return client


def _touch(name="demo.svs"):
    p = Path(UPLOAD_DIR) / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"svs-stub")
    return name


def _setup_users():
    owner = user_store.create_user("owner@x.com", "ownerpass123456", role="owner")
    usera = user_store.create_user("a@x.com", "userApass123456", role="user")
    userb = user_store.create_user("b@x.com", "userBpass123456", role="user")
    share_store.set_owner_user_id(owner["user_id"])
    return owner, usera, userb


def _bootstrap_plugin():
    inst = app_mod._bootstrap_plugin_installations()
    assert inst is not None
    app_mod._HISTOPILOT_INSTALLATION = inst
    return inst


def _token_for(inst):
    """读当前数据目录的插件凭证文件换 access token（与 sidecar 4-1b 同源）。"""
    secret_file = Path(os.environ.get("SHARE_DATA_DIR")) / "plugin-secret-histopilot.txt"
    raw = secret_file.read_text(encoding="utf-8").strip()
    try:
        obj = json.loads(raw)
        secret = str(obj.get("secret") or "")
    except (ValueError, TypeError):
        secret = raw
    assert secret, "插件 secret 文件缺失：%s" % secret_file
    r = _client().post("/api/plugin/v1/auth/token",
                       json={"installation_id": inst["installation_id"],
                             "secret": secret})
    assert r.status_code == 200, r.get_json()
    return r.get_json()["access_token"]


def _bearer(token):
    return {"Authorization": "Bearer " + token}


def _make_grant(slide, installation_id, created_by_user_id=None,
                session_id="", ttl_seconds=None):
    return share_store.create_run_grant(
        installation_id=installation_id, slide=slide,
        session_id=session_id, created_by_user_id=created_by_user_id,
        ttl_seconds=ttl_seconds)


def _setup_ai_config():
    app_mod._save_ai_config({
        "base_url": "http://llm.example/v1",
        "api_key": "sk-integrity-test-123456",
        "model": "gpt-integrity",
        "api_protocol": "openai",
    })


# =========================================================================== #
# 1. /api/ai/cancel 权限（§3.8 / §6.3）
# =========================================================================== #
def test_user_cancel_own_and_foreign_session(fake_sidecar):
    """A、B 都可协作同一 slide；B 不能取消 A 的 session，A 可取消自己的。"""
    owner, usera, userb = _setup_users()
    slide = _touch("coop.svs")
    share_store.set_slide_meta(slide, owner_user_id=usera["user_id"])
    # A 建分享（annotate），B 认领 → B 具备该切片 annotate 权限。
    share = share_store.create_share([slide], 24, permissions=["view", "annotate"],
                                     creator_user_id=usera["user_id"])
    share_store.claim_share(share["token"], userb["user_id"])

    fake = fake_sidecar
    # sidecar：/session/sess-a 的 owner = A；cancel 透传。
    fake.register_json("GET", "/session/sess-a", body={"session": {"owner": usera["user_id"]}})
    fake.register_json("POST", "/cancel", body={"ok": True, "session_id": "sess-a"})

    cb = _login(_client(auth_enabled=True), userb)
    # B 取消 A 的 session → 403（不泄露存在性；sidecar 只收到归属查询，
    # 没有任何 /cancel 转发）。
    r = cb.post("/api/ai/cancel", json={"session_id": "sess-a"})
    assert r.status_code == 403, r.get_json()
    assert not any(c["method"] == "POST" and c["path"] == "/cancel"
                   for c in fake.calls)

    # A 取消自己的 session → 200，转发 body 透传。
    ca = _login(_client(auth_enabled=True), usera)
    r = ca.post("/api/ai/cancel", json={"session_id": "sess-a"})
    assert r.status_code == 200, r.get_json()
    cancel_call = [c for c in fake.calls if c["path"] == "/cancel"][-1]
    assert cancel_call["body"]["session_id"] == "sess-a"

    # owner 可取消任意 session。
    co = _login(_client(auth_enabled=True), owner)
    r = co.post("/api/ai/cancel", json={"session_id": "sess-a"})
    assert r.status_code == 200


def test_cancel_slide_branch_owner_only(fake_sidecar):
    """slide 分支（取消该切片 main）只保留给 owner；user 传 slide → 403。"""
    owner, usera, userb = _setup_users()
    slide = _touch("coop2.svs")
    share_store.set_slide_meta(slide, owner_user_id=usera["user_id"])
    share = share_store.create_share([slide], 24, permissions=["view", "annotate"],
                                     creator_user_id=usera["user_id"])
    share_store.claim_share(share["token"], userb["user_id"])

    fake = fake_sidecar
    fake.register_json("POST", "/cancel", body={"ok": True, "session_id": "m1"})

    # B 能查看/协作该切片，但不能按 slide 取消。
    cb = _login(_client(auth_enabled=True), userb)
    assert cb.post("/api/ai/cancel", json={"slide": slide}).status_code == 403
    # owner 可以。
    co = _login(_client(auth_enabled=True), owner)
    assert co.post("/api/ai/cancel", json={"slide": slide}).status_code == 200
    # AUTH_ENABLED=False（归一 owner，内部兼容）也可以。
    ci = _client(auth_enabled=False)
    assert ci.post("/api/ai/cancel", json={"slide": slide}).status_code == 200


def test_cancel_revokes_session_bound_grants(fake_sidecar):
    """取消成功后按 sidecar 回显 session_id 撤销绑定 grant（§3.10）。"""
    inst = _bootstrap_plugin()
    slide = _touch("cancel-grant.svs")
    grant = _make_grant(slide, inst["installation_id"], session_id="sess-cx")
    assert share_store.get_run_grant(grant["grant_id"])["revoked"] is False

    fake = fake_sidecar
    fake.register_json("POST", "/cancel",
                       body={"ok": True, "session_id": "sess-cx"})
    r = _client().post("/api/ai/cancel", json={"session_id": "sess-cx"})
    assert r.status_code == 200
    assert share_store.get_run_grant(grant["grant_id"])["revoked"] is True


# =========================================================================== #
# 2. run grant 校验矩阵（§6.3：installation/slide/session/creator/权限）
# =========================================================================== #
def _annotate(client, token, slide, grant_id, session_id="sess-x", **kw):
    return client.post(
        "/api/plugin/v1/slides/%s/annotations" % slide,
        headers={**_bearer(token), "X-Run-Grant": grant_id},
        json={"label": "AI", "x": 1, "y": 1, "side_px": 10,
              "session_id": session_id, **kw})


def _verify(client, token, grant_id, slide):
    return client.post("/api/plugin/v1/run-grants/verify",
                       headers=_bearer(token),
                       json={"grant_id": grant_id, "slide": slide})


def test_grant_bind_endpoint_cas_and_idempotent():
    inst = _bootstrap_plugin()
    token = _token_for(inst)
    slide = _touch("bind.svs")
    client = _client()
    grant = _make_grant(slide, inst["installation_id"])

    # 未绑定 grant 的 annotate → grant_unbound。
    r = _annotate(client, token, slide, grant["grant_id"], session_id="sess-1")
    assert r.status_code == 403
    assert r.get_json()["error"]["code"] == "run_grant_invalid"
    assert "grant_unbound" in r.get_json()["error"]["message"]

    # bind：成功 + 幂等。
    r = client.post("/api/plugin/v1/run-grants/bind", headers=_bearer(token),
                    json={"grant_id": grant["grant_id"], "session_id": "sess-1"})
    assert r.status_code == 200 and r.get_json()["ok"] is True
    r = client.post("/api/plugin/v1/run-grants/bind", headers=_bearer(token),
                    json={"grant_id": grant["grant_id"], "session_id": "sess-1"})
    assert r.status_code == 200

    # 绑定后 annotate 成功；换 session → session_mismatch。
    r = _annotate(client, token, slide, grant["grant_id"], session_id="sess-1")
    assert r.status_code == 200, r.get_json()
    r = _annotate(client, token, slide, grant["grant_id"], session_id="sess-2")
    assert r.status_code == 403
    assert "session_mismatch" in r.get_json()["error"]["message"]

    # 绑定到其它 session → 409 conflict。
    r = client.post("/api/plugin/v1/run-grants/bind", headers=_bearer(token),
                    json={"grant_id": grant["grant_id"], "session_id": "sess-2"})
    assert r.status_code == 409

    # 非法 session_id 格式 → 400。
    r = client.post("/api/plugin/v1/run-grants/bind", headers=_bearer(token),
                    json={"grant_id": grant["grant_id"],
                          "session_id": "bad session!"})
    assert r.status_code == 400
    # 不存在的 grant → 404。
    r = client.post("/api/plugin/v1/run-grants/bind", headers=_bearer(token),
                    json={"grant_id": "rgr_none", "session_id": "sess-1"})
    assert r.status_code == 404
    # 已撤销 → 403 run_grant_invalid。
    share_store.revoke_run_grant(grant["grant_id"])
    grant2 = _make_grant(slide, inst["installation_id"])
    share_store.revoke_run_grant(grant2["grant_id"])
    r = client.post("/api/plugin/v1/run-grants/bind", headers=_bearer(token),
                    json={"grant_id": grant2["grant_id"], "session_id": "s1"})
    assert r.status_code == 403


def test_grant_installation_mismatch_rejected():
    inst = _bootstrap_plugin()
    other = share_store.create_plugin_installation("other-plugin")
    slide = _touch("inst.svs")
    grant = _make_grant(slide, other["installation_id"])
    token = _token_for(inst)  # histopilot 的 token
    client = _client()
    r = _verify(client, token, grant["grant_id"], slide)
    assert r.get_json() == {"valid": False, "reason": "installation_mismatch"}
    r = _annotate(client, token, slide, grant["grant_id"])
    assert r.status_code == 403


def test_grant_creator_rechecks():
    """创建者被禁用/失去 annotate 权限/被删除 → grant 立即失效（§3.10）。"""
    owner, usera, userb = _setup_users()
    inst = _bootstrap_plugin()
    token = _token_for(inst)
    client = _client()
    slide = _touch("creator.svs")
    share_store.set_slide_meta(slide, owner_user_id=usera["user_id"])
    share = share_store.create_share([slide], 24, permissions=["view", "annotate"],
                                     creator_user_id=usera["user_id"])
    share_store.claim_share(share["token"], userb["user_id"])
    grant = _make_grant(slide, inst["installation_id"],
                        created_by_user_id=userb["user_id"])
    share_store.bind_run_grant_session(grant["grant_id"], "sess-b1")

    # 协作有效时 annotate 通过。
    r = _annotate(client, token, slide, grant["grant_id"], session_id="sess-b1")
    assert r.status_code == 200, r.get_json()

    # 用户禁用 → 写前复查拒绝（verify 与 annotate 都失效）。
    user_store.set_user_disabled(userb["user_id"], True)
    r = _verify(client, token, grant["grant_id"], slide)
    assert r.get_json()["reason"] == "creator_not_allowed"
    r = _annotate(client, token, slide, grant["grant_id"], session_id="sess-b1")
    assert r.status_code == 403
    user_store.set_user_disabled(userb["user_id"], False)

    # 协作 share 撤销 → B 失去 annotate；且撤销动作主动 revoke 该 grant。
    grant2 = _make_grant(slide, inst["installation_id"],
                         created_by_user_id=userb["user_id"])
    share_store.bind_run_grant_session(grant2["grant_id"], "sess-b2")
    assert _verify(client, token, grant2["grant_id"], slide).get_json()["valid"] is True
    co = _login(_client(auth_enabled=True), usera)
    assert co.post("/api/share/revoke", json={"token": share["token"]}).status_code == 200
    # B 的 grant 已被主动撤销；切片 owner（A）的 grant 不受影响。
    assert share_store.get_run_grant(grant2["grant_id"])["revoked"] is True
    grant_a = _make_grant(slide, inst["installation_id"],
                          created_by_user_id=usera["user_id"])
    assert _verify(client, token, grant_a["grant_id"], slide).get_json()["valid"] is True

    # 创建者账号被删除 → 失效。
    user_store.set_user_disabled(userb["user_id"], True)  # 禁用等价拦截写
    r = _verify(client, token, grant["grant_id"], slide)
    assert r.get_json()["valid"] is False

    # 无主 grant（AUTH_ENABLED=False 归一 owner）保持可用（归档另测）。
    grant_o = _make_grant(slide, inst["installation_id"])
    assert _verify(client, token, grant_o["grant_id"], slide).get_json()["valid"] is True


def test_grant_archived_slide_rejected_and_revoked():
    owner, _, _ = _setup_users()
    inst = _bootstrap_plugin()
    token = _token_for(inst)
    slide = _touch("arch.svs")
    proj = share_store.create_project(name="P", note="", slides=[slide],
                                      owner_user_id=owner["user_id"])
    grant = _make_grant(slide, inst["installation_id"])
    client = _client()
    assert _verify(client, token, grant["grant_id"], slide).get_json()["valid"] is True
    co = _login(_client(auth_enabled=True), owner)
    assert co.post("/api/project/%s/archive" % proj["pid"]).status_code == 200
    # 归档 → 既有 grant 被主动撤销；写前复查同样拒绝。
    assert share_store.get_run_grant(grant["grant_id"])["revoked"] is True
    assert _verify(client, token, grant["grant_id"], slide).get_json()["valid"] is False


# =========================================================================== #
# 3. 机器端 / 人类端撤销通道分离（§3.10）
# =========================================================================== #
def test_human_revoke_endpoint_owner_or_creator_csrf():
    owner, _, userc = _setup_users()
    inst = _bootstrap_plugin()
    slide = _touch("human-revoke.svs")
    grant = _make_grant(slide, inst["installation_id"],
                        created_by_user_id=userc["user_id"])

    # 其它 user → 403。
    client = _login(_client(auth_enabled=True), owner)  # 先拿 owner client 备用
    other = user_store.create_user("d@x.com", "userDpass123456", role="user")
    cd = _login(_client(auth_enabled=True), other)
    r = cd.delete("/api/ai/run-grants/%s" % grant["grant_id"])
    assert r.status_code == 403
    assert share_store.get_run_grant(grant["grant_id"])["revoked"] is False

    # 创建者本人 → ok。
    cc = _login(_client(auth_enabled=True), userc)
    assert cc.delete("/api/ai/run-grants/%s" % grant["grant_id"]).status_code == 200
    assert share_store.get_run_grant(grant["grant_id"])["revoked"] is True

    # owner 可撤销任意（新 grant）。
    grant2 = _make_grant(slide, inst["installation_id"],
                         created_by_user_id=userc["user_id"])
    assert client.delete("/api/ai/run-grants/%s" % grant2["grant_id"]).status_code == 200

    # CSRF：裸 client（无 X-CSRF-Token）→ 400（Cookie 通道写必须带 CSRF）。
    grant3 = _make_grant(slide, inst["installation_id"])
    app_mod.app.config["TESTING"] = True
    raw = app_mod.app.test_client()
    with raw.session_transaction() as s:
        s["auth_user"] = owner.get("email")
        s["user_id"] = owner["user_id"]
        s["role"] = "owner"
        s["auth_version"] = owner.get("auth_version", 1)
    r = raw.delete("/api/ai/run-grants/%s" % grant3["grant_id"])
    assert r.status_code == 400, r.get_json()
    assert share_store.get_run_grant(grant3["grant_id"])["revoked"] is False


def test_machine_revoke_ignores_cookie_identity():
    """机器端撤销只看 Bearer installation 匹配（§3.10 身份混用修复）。"""
    _setup_users()
    inst = _bootstrap_plugin()
    slide = _touch("machine-revoke.svs")
    grant = _make_grant(slide, inst["installation_id"])
    token = _token_for(inst)
    # 无任何 Cookie 的纯机器请求可撤销自己 installation 的 grant。
    raw = app_mod.app.test_client()
    r = raw.delete("/api/plugin/v1/run-grants/%s" % grant["grant_id"],
                   headers=_bearer(token))
    assert r.status_code == 200
    assert share_store.get_run_grant(grant["grant_id"])["revoked"] is True


# =========================================================================== #
# 4. run 生命周期主动撤销 + TTL（§3.10）
# =========================================================================== #
def test_run_rejected_revokes_issued_grant(fake_sidecar):
    inst = _bootstrap_plugin()
    _setup_ai_config()
    slide = _touch("reject.svs")
    fake = fake_sidecar
    fake.register_json("POST", "/run", status=409, body={"error": "会话正在运行中"})
    r = _client().post("/api/ai/run", json={"slide": slide})
    assert r.status_code == 409
    grants = share_store.list_run_grants(slide=slide, include_revoked=True)
    # sidecar 拒绝 → 本轮签发的 grant 立即撤销（不留到 TTL）。
    assert grants and all(g["revoked"] for g in grants), grants


def test_run_finished_revokes_bound_grant(fake_sidecar):
    """上游 SSE 正常关流（run 落定）→ 撤销绑定到该 session 的 grant。"""
    inst = _bootstrap_plugin()
    _setup_ai_config()
    slide = _touch("finish.svs")
    fake = fake_sidecar

    def run_handler(body, query, headers, kwargs):
        # 模拟 sidecar：接受 run 后把 grant 绑定到 session（HistoPilot 行为）。
        rg = (body.get("config") or {}).get("run_grant") or {}
        if rg.get("grant_id"):
            share_store.bind_run_grant_session(rg["grant_id"], "sess-run-9")
        return FakeResponse(200, sse_frames=[
            b"id: 1\nevent: slide_opened\ndata: {}\n\n",
            b"id: 2\nevent: session_ended\ndata: {}\n\n",
        ], headers={"X-AI-Session-ID": "sess-run-9"})

    fake.register("POST", "/run", run_handler)
    resp = _client().post("/api/ai/run", json={"slide": slide})
    assert resp.status_code == 200
    _ = resp.data  # 完整消费响应 → 上游流正常结束
    grants = share_store.list_run_grants(slide=slide, include_revoked=True)
    assert grants, "应已签发 grant"
    assert all(g["revoked"] for g in grants), grants
    # revoke 的 grant 在 verify 上也失效。
    token = _token_for(inst)
    r = _client().post("/api/plugin/v1/run-grants/verify",
                       headers=_bearer(token),
                       json={"grant_id": grants[0]["grant_id"], "slide": slide})
    assert r.get_json()["valid"] is False


def test_grant_ttl_default_reduced(fake_sidecar):
    """默认 TTL 从 2h 降到 30min（接近单次 run 上限；主动撤销为主路径）。"""
    inst = _bootstrap_plugin()
    _setup_ai_config()
    slide = _touch("ttl.svs")
    assert app_mod._RUN_GRANT_TTL_SECONDS <= 1800
    fake = fake_sidecar
    fake.register("POST", "/run", lambda b, q, h, k: FakeResponse(
        200, sse_frames=[b"id: 1\nevent: slide_opened\ndata: {}\n\n"],
        headers={"X-AI-Session-ID": "sess-ttl"}))
    resp = _client().post("/api/ai/run", json={"slide": slide})
    assert resp.status_code == 200
    _ = resp.data
    grants = share_store.list_run_grants(slide=slide, include_revoked=True)
    assert grants
    ttl = grants[0]["expires_at"] - grants[0]["created_at"]
    assert ttl <= 1800 + 1, ttl


def test_session_archive_revokes_grants(fake_sidecar):
    inst = _bootstrap_plugin()
    slide = _touch("arch-session.svs")
    grant = _make_grant(slide, inst["installation_id"], session_id="sess-arch")
    fake = fake_sidecar
    fake.register_json("POST", "/session/sess-arch/archive",
                       body={"ok": True, "archived": True})
    fake.register_json("POST", "/session/sess-arch/unarchive",
                       body={"ok": True, "archived": False})
    client = _client()
    assert client.post("/api/ai/session/sess-arch/archive", json={}).status_code == 200
    assert share_store.get_run_grant(grant["grant_id"])["revoked"] is True
