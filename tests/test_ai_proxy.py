# -*- coding: utf-8 -*-
"""AI sidecar 代理测试（pi 迁移 Step 5）。

覆盖 Flask /api/ai/* → sidecar 的代理转发：
  - run/continue/ask/cancel/sessions/session/archive/stream 路径与 body
  - body 注入 config（api_key 是解密后的明文）
  - 响应 body / 状态码透传（含 409/404/410）
  - SSE 字节透传（假 sidecar 返回若干 SSE 帧，断言客户端收到完全一致字节）
  - X-AI-Session-ID 头透传
  - Last-Event-ID / after_seq 透传
  - sidecar 宕机 → 503 {error:"ai sidecar 不可用"}
  - 鉴权 401 仍然生效

方案：用内存中的 FakeRequests 替换 app.requests，无需起真 server。
运行：cd 项目根 && python3 tests/test_ai_proxy.py
"""
import json
import logging
import os
import re
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TMP = tempfile.mkdtemp(prefix="svs-proxy-")
os.environ["SHARE_DATA_DIR"] = os.path.join(TMP, "share-data")
os.makedirs(os.environ["SHARE_DATA_DIR"], exist_ok=True)
# 固定 sidecar 地址（测试用假 requests，地址不会真的被访问）
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
from _pt_helpers import csrf_client  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent

PASS = 0
FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  ok  %s" % name)
        return
    FAIL += 1
    print("FAIL  %s  %s" % (name, detail))
    # pytest 收集运行时必须失败：计数式断言只看 PASS/FAIL 汇总，pytest 下
    # 每个 test_* 都会「绿」，失败的检查会静默漏网（PYTEST_CURRENT_TEST 由
    # pytest 在用例执行期间设置；脚本直跑模式保持原汇总行为）。
    if "PYTEST_CURRENT_TEST" in os.environ:
        raise AssertionError("FAIL %s %s" % (name, detail))


# =========================================================================== #
# Fake requests layer
# =========================================================================== #
class FakeResponse:
    """模拟 requests.Response：普通 + SSE 两种形态。"""

    def __init__(self, status_code=200, content=b"", headers=None,
                 sse_frames=None, ctype=None):
        self.status_code = status_code
        if sse_frames is not None:
            # SSE：content 是帧字节序列拼接；iter_content 逐帧吐
            self._sse_frames = list(sse_frames)
            data = b"".join(sse_frames)
            self.content = data
            self.headers = {"Content-Type": ctype or "text/event-stream"}
            # X-AI-Session-ID 由调用方在 headers 里给
            if headers:
                self.headers.update(headers)
        else:
            self._sse_frames = None
            self.content = content if isinstance(content, bytes) else content.encode("utf-8")
            self.headers = dict(headers or {})
            if "Content-Type" not in self.headers:
                self.headers["Content-Type"] = ctype or "application/json"
        self._closed = False

    def iter_content(self, chunk_size=4096):
        if self._sse_frames is None:
            # 普通 body：一次吐完
            yield self.content
            return
        # SSE：逐帧吐（不分块，保持帧边界便于断言）
        for frame in self._sse_frames:
            yield frame

    def close(self):
        self._closed = True


class FakeRequests:
    """替换 app.requests。按 (method, path) 注册响应工厂；记录所有调用。"""

    def __init__(self):
        self._routes = {}  # (method, path) -> handler(body, query, headers, kwargs)
        self.calls = []    # 记录：{method, path, body, query, headers}
        self._next_error = None  # ConnectionError/Timeout 触发器

    ConnectionError = __import__("requests").ConnectionError
    Timeout = __import__("requests").Timeout

    def register(self, method, path, handler):
        self._routes[(method.upper(), path)] = handler

    def set_unreachable(self):
        self._next_error = True

    def clear_unreachable(self):
        self._next_error = None
        self.calls.clear()

    def _dispatch(self, method, url, **kwargs):
        # url 形如 http://127.0.0.1:8055/run
        base = app_mod.AI_SIDECAR_URL
        path = url[len(base):] if url.startswith(base) else url
        body = kwargs.get("json")
        params = kwargs.get("params")
        headers = kwargs.get("headers") or {}
        self.calls.append({
            "method": method, "path": path, "body": body,
            "query": params, "headers": headers,
        })
        if self._next_error:
            raise FakeRequests.ConnectionError("sidecar down (test)")
        handler = self._routes.get((method, path))
        if handler is None:
            return FakeResponse(404, json.dumps({"error": "no route"}).encode(),
                                headers={"Content-Type": "application/json"})
        return handler(body, params, headers, kwargs)

    def get(self, url, **kwargs):
        return self._dispatch("GET", url, **kwargs)

    def post(self, url, **kwargs):
        return self._dispatch("POST", url, **kwargs)


def install_fake_requests():
    fake = FakeRequests()
    app_mod.requests = fake
    return fake


def make_client():
    app_mod.app.config["TESTING"] = True
    # 认证默认关闭（多数测试需要放行 /api/）
    app_mod.AUTH_ENABLED = False
    return csrf_client(app_mod.app.test_client())


# =========================================================================== #
# 配置一个带 api_key 的 ai_config.json（加密落盘 → 代理时应解密为明文）
# =========================================================================== #
def setup_ai_config(plain_key="sk-proxy-secret-123456"):
    app_mod._save_ai_config({
        "base_url": "http://llm.example/v1",
        "api_key": plain_key,
        "model": "gpt-proxy",
        "api_protocol": "openai",
        "keep_recent_images": 7,
    })
    return plain_key


# =========================================================================== #
# 测试
# =========================================================================== #
def test_run_proxies_with_decrypted_config_and_sse():
    print("== test_run: 代理 /run，body 注入解密明文 api_key + SSE 字节透传 ==")
    fake = install_fake_requests()
    client = make_client()
    plain = setup_ai_config()

    frames = [
        b"id: 1\nevent: slide_opened\ndata: {\"a\":1}\n\n",
        b"id: 2\nevent: delta\ndata: {\"t\":\"hi\"}\n\n",
    ]
    sent_bytes = b"".join(frames)

    def handler(body, query, headers, kwargs):
        # 断言 body 注入了 config 且 api_key 是明文
        check("run body 含 slide", body.get("slide") == "s.svs", "body=%r" % body)
        cfg = body.get("config") or {}
        check("run config.api_key 为明文（解密后）", cfg.get("api_key") == plain,
              "got %r" % cfg.get("api_key"))
        check("run config.base_url 注入", cfg.get("base_url") == "http://llm.example/v1")
        check("run config 含调优字段 keep_recent_images", cfg.get("keep_recent_images") == 7)
        check("run config 含 api_protocol", cfg.get("api_protocol") == "openai")
        check("run body task 透传", body.get("task") == "看全片")
        check("run 带内部 token",
              (headers or {}).get("X-AI-Internal-Token") == app_mod.AI_INTERNAL_TOKEN,
              "headers=%r" % headers)
        return FakeResponse(200, sse_frames=frames,
                            headers={"X-AI-Session-ID": "sess-run-1"})

    fake.register("POST", "/run", handler)
    resp = client.post("/api/ai/run", json={"slide": "s.svs", "task": "看全片"})
    check("run 状态码 200", resp.status_code == 200, "got %d" % resp.status_code)
    check("run X-AI-Session-ID 头透传",
          resp.headers.get("X-AI-Session-ID") == "sess-run-1",
          "got %r" % resp.headers.get("X-AI-Session-ID"))
    check("run Content-Type 为 text/event-stream",
          resp.headers.get("Content-Type", "").startswith("text/event-stream"),
          "got %r" % resp.headers.get("Content-Type"))
    check("run SSE 字节完全一致", resp.data == sent_bytes,
          "got %r" % resp.data)
    # fresh=1 query 透传成 body.fresh=True
    fake.calls.clear()
    fake.register("POST", "/run", lambda b, q, h, k: FakeResponse(200, sse_frames=[]))
    client.post("/api/ai/run?fresh=1", json={"slide": "s.svs"})
    check("run fresh=1 query → body.fresh=True",
          fake.calls[-1]["body"].get("fresh") is True,
          "got %r" % fake.calls[-1]["body"])


def test_continue_and_ask_proxy():
    print("== test_continue / test_ask: 代理 + config 注入 + 状态码透传 ==")
    fake = install_fake_requests()
    client = make_client()
    setup_ai_config()

    fake.register("POST", "/continue",
                  lambda b, q, h, k: FakeResponse(200, sse_frames=[b"x\n\n"],
                   headers={"X-AI-Session-ID": "sess-c"}))
    r = client.post("/api/ai/continue", json={"slide": "s.svs"})
    check("continue 路径转发 /continue",
          fake.calls[-1]["path"] == "/continue")
    check("continue body.config.api_key 明文",
          (fake.calls[-1]["body"].get("config") or {}).get("api_key"))
    check("continue SSE 透传", r.data == b"x\n\n", "got %r" % r.data)
    check("continue X-AI-Session-ID", r.headers.get("X-AI-Session-ID") == "sess-c")

    # ask：410 根标注已删除（错误响应，非 SSE，JSON 透传）
    fake.register("POST", "/ask",
                  lambda b, q, h, k: FakeResponse(410,
                   json.dumps({"error": "该标注已删除"}).encode(),
                   headers={"Content-Type": "application/json"}))
    r2 = client.post("/api/ai/ask",
                     json={"slide": "s.svs", "annotation_id": "ann-1", "question": "?"})
    check("ask 路径转发 /ask", fake.calls[-1]["path"] == "/ask")
    check("ask body.annotation_id 透传",
          fake.calls[-1]["body"].get("annotation_id") == "ann-1")
    check("ask 410 状态码透传", r2.status_code == 410, "got %d" % r2.status_code)
    check("ask 错误 JSON body 透传",
          json.loads(r2.data).get("error") == "该标注已删除")


def test_branch_proxy():
    print("== test_branch: 代理 /branch，body 注入 config + annotation_id 透传 + SSE 透传 + 410 ==")
    fake = install_fake_requests()
    client = make_client()
    setup_ai_config()

    # 1) branch SSE 透传 + config 注入 + annotation_id/question 透传
    frames = [
        b"id: 1\nevent: branch_created\ndata: {\"annotation_id\":\"br-1\"}\n\n",
        b"id: 2\nevent: agent_finished\ndata: {\"summary\":\"ok\"}\n\n",
    ]
    sent_bytes = b"".join(frames)

    def branch_handler(body, query, headers, kwargs):
        check("branch 路径转发 /branch", True)
        check("branch body.slide 透传", body.get("slide") == "s.svs",
              "body=%r" % body)
        check("branch body.annotation_id 透传",
              body.get("annotation_id") == "br-1",
              "body=%r" % body)
        check("branch body.question 透传",
              body.get("question") == "深读这里")
        cfg = body.get("config") or {}
        check("branch config.api_key 明文", cfg.get("api_key"),
              "got %r" % cfg.get("api_key"))
        check("branch config.base_url 注入",
              cfg.get("base_url") == "http://llm.example/v1")
        return FakeResponse(200, sse_frames=frames,
                            headers={"X-AI-Session-ID": "sess-branch-1"})

    fake.register("POST", "/branch", branch_handler)
    r = client.post("/api/ai/branch",
                    json={"slide": "s.svs", "annotation_id": "br-1", "question": "深读这里"})
    check("branch 路径转发 /branch",
          fake.calls[-1]["path"] == "/branch",
          "got %r" % fake.calls[-1]["path"])
    check("branch 状态码 200", r.status_code == 200, "got %d" % r.status_code)
    check("branch X-AI-Session-ID 头透传",
          r.headers.get("X-AI-Session-ID") == "sess-branch-1",
          "got %r" % r.headers.get("X-AI-Session-ID"))
    check("branch Content-Type 为 text/event-stream",
          r.headers.get("Content-Type", "").startswith("text/event-stream"),
          "got %r" % r.headers.get("Content-Type"))
    check("branch SSE 字节完全一致", r.data == sent_bytes, "got %r" % r.data)

    # 2) branch 410 根标注已删除（错误响应，非 SSE，JSON 透传）
    fake.register("POST", "/branch",
                  lambda b, q, h, k: FakeResponse(410,
                   json.dumps({"error": "该标注已删除"}).encode(),
                   headers={"Content-Type": "application/json"}))
    r2 = client.post("/api/ai/branch",
                     json={"slide": "s.svs", "annotation_id": "br-gone"})
    check("branch 410 状态码透传", r2.status_code == 410, "got %d" % r2.status_code)
    check("branch 410 JSON body 透传",
          json.loads(r2.data).get("error") == "该标注已删除")

    # 3) branch 缺 annotation_id → 400（不转发到 sidecar）
    fake.calls.clear()
    r3 = client.post("/api/ai/branch", json={"slide": "s.svs"})
    check("branch 缺 annotation_id 400", r3.status_code == 400,
          "got %d" % r3.status_code)
    check("branch 缺 annotation_id 未转发", len(fake.calls) == 0,
          "calls=%d" % len(fake.calls))


def test_run_conflict_409_non_sse_passthrough():
    print("== test_run 409 冲突（非 SSE JSON 错误透传）==")
    fake = install_fake_requests()
    client = make_client()
    setup_ai_config()
    fake.register("POST", "/run",
                  lambda b, q, h, k: FakeResponse(409,
                   json.dumps({"error": "会话正在运行中"}).encode(),
                   headers={"Content-Type": "application/json"}))
    r = client.post("/api/ai/run", json={"slide": "s.svs"})
    check("409 状态码透传", r.status_code == 409)
    check("409 JSON body 透传", json.loads(r.data).get("error") == "会话正在运行中")
    check("409 非 SSE Content-Type",
          r.headers.get("Content-Type", "").startswith("application/json"))


def test_cancel_proxy():
    print("== test_cancel: 原样转发 body，透传 ok ==")
    fake = install_fake_requests()
    client = make_client()
    setup_ai_config()
    fake.register("POST", "/cancel",
                  lambda b, q, h, k: FakeResponse(200,
                   json.dumps({"ok": True}).encode(),
                   headers={"Content-Type": "application/json"}))
    r = client.post("/api/ai/cancel", json={"session_id": "sess-x"})
    check("cancel 路径转发 /cancel", fake.calls[-1]["path"] == "/cancel")
    check("cancel body.session_id 透传",
          fake.calls[-1]["body"].get("session_id") == "sess-x")
    check("cancel config 不注入（原样转发）",
          "config" not in fake.calls[-1]["body"],
          "body=%r" % fake.calls[-1]["body"])
    check("cancel 200 ok 透传", json.loads(r.data).get("ok") is True)


def test_sessions_and_session_detail_proxy():
    print("== test_sessions / session detail: GET 代理，query 透传 ==")
    fake = install_fake_requests()
    client = make_client()
    setup_ai_config()

    def sessions_handler(body, query, headers, kwargs):
        check("sessions query.slide 透传", query.get("slide") == "s.svs",
              "query=%r" % query)
        check("sessions 无 body（GET）", body is None)
        return FakeResponse(200, json.dumps({"sessions": [{"id": "m1"}]}).encode(),
                            headers={"Content-Type": "application/json"})

    fake.register("GET", "/sessions", sessions_handler)
    r = client.get("/api/ai/sessions?slide=s.svs")
    check("sessions 状态码 200", r.status_code == 200)
    check("sessions body 透传", json.loads(r.data) == {"sessions": [{"id": "m1"}]})

    fake.register("GET", "/session/sess-d",
                  lambda b, q, h, k: FakeResponse(200,
                   json.dumps({"session": {"id": "sess-d"}, "transcript": []}).encode(),
                   headers={"Content-Type": "application/json"}))
    r2 = client.get("/api/ai/session/sess-d")
    check("session detail 路径转发 /session/sess-d",
          fake.calls[-1]["path"] == "/session/sess-d")
    check("session detail 200", r2.status_code == 200)


def test_archive_proxy_paths():
    print("== test_archive/unarchive: 路径分支与 body 透传 ==")
    fake = install_fake_requests()
    client = make_client()
    setup_ai_config()

    fake.register("POST", "/session/sid-1/archive",
                  lambda b, q, h, k: FakeResponse(200,
                   json.dumps({"ok": True, "archived": True}).encode(),
                   headers={"Content-Type": "application/json"}))
    fake.register("POST", "/session/sid-1/unarchive",
                  lambda b, q, h, k: FakeResponse(200,
                   json.dumps({"ok": True, "archived": False}).encode(),
                   headers={"Content-Type": "application/json"}))
    r1 = client.post("/api/ai/session/sid-1/archive", json={})
    check("archive 路径 /session/sid-1/archive",
          fake.calls[-1]["path"] == "/session/sid-1/archive")
    check("archive 返回 archived=True", json.loads(r1.data).get("archived") is True)

    r2 = client.post("/api/ai/session/sid-1/unarchive", json={})
    check("unarchive 路径 /session/sid-1/unarchive",
          fake.calls[-1]["path"] == "/session/sid-1/unarchive")
    check("unarchive 返回 archived=False", json.loads(r2.data).get("archived") is False)


def test_stream_proxy_passes_after_seq_and_last_event_id():
    print("== test_stream: SSE 重挂，after_seq query + Last-Event-ID header 透传 ==")
    fake = install_fake_requests()
    client = make_client()
    setup_ai_config()

    frames = [
        b"id: 5\nevent: delta\ndata: {\"t\":\"a\"}\n\n",
        b"event: session_ended\ndata: {\"status\":\"finished\"}\n\n",
    ]
    expected = b"".join(frames)

    def stream_handler(body, query, headers, kwargs):
        check("stream after_seq query 透传", query.get("after_seq") == "3",
              "query=%r" % query)
        check("stream Last-Event-ID header 透传",
              headers.get("Last-Event-ID") == "2",
              "headers=%r" % headers)
        return FakeResponse(200, sse_frames=frames,
                            headers={"X-AI-Session-ID": "sess-stream"})

    fake.register("GET", "/session/sess-stream/stream", stream_handler)
    r = client.get("/api/ai/session/sess-stream/stream?after_seq=3",
                   headers={"Last-Event-ID": "2"})
    check("stream 状态码 200", r.status_code == 200)
    check("stream SSE 字节完全一致", r.data == expected, "got %r" % r.data)
    check("stream X-AI-Session-ID 透传",
          r.headers.get("X-AI-Session-ID") == "sess-stream")


def test_sidecar_down_returns_503():
    print("== test_sidecar_down: sidecar 不可达 → 503（JSON 与 SSE 端点）==")
    fake = install_fake_requests()
    client = make_client()
    setup_ai_config()
    fake.set_unreachable()

    r1 = client.post("/api/ai/cancel", json={"session_id": "x"})
    check("cancel 503", r1.status_code == 503, "got %d" % r1.status_code)
    check("cancel 503 body", json.loads(r1.data).get("error") == "ai sidecar 不可用")

    r2 = client.post("/api/ai/run", json={"slide": "s.svs"})
    check("run SSE 端点 503", r2.status_code == 503, "got %d" % r2.status_code)
    check("run 503 body", json.loads(r2.data).get("error") == "ai sidecar 不可用")

    r3 = client.get("/api/ai/session/sess-x/stream")
    check("stream SSE 端点 503", r3.status_code == 503)
    fake.clear_unreachable()


def test_auth_still_enforced():
    print("== test_auth: 开启认证时 /api/ai/* 仍返回 401（代理前鉴权）==")
    install_fake_requests()
    client = make_client()
    setup_ai_config()
    app_mod.AUTH_ENABLED = True
    try:
        # 未登录 session
        r = client.post("/api/ai/run", json={"slide": "s.svs"})
        check("开启认证时 /api/ai/run 401", r.status_code == 401,
              "got %d" % r.status_code)
        check("401 body auth_required",
              json.loads(r.data).get("error") == "auth_required")
        r2 = client.get("/api/ai/sessions?slide=s.svs")
        check("开启认证时 /api/ai/sessions 401", r2.status_code == 401)
    finally:
        app_mod.AUTH_ENABLED = False


def test_require_admin_auth_fail_closed(tmp_path):
    """bootstrap 秘密解析（账户系统批次 A docs §5.1，替代旧 _resolve_admin_auth）。

    - REQUIRE_ADMIN_AUTH/owner 状态机的拒启语义在 tests/test_account_auth.py
      覆盖（需要 store/monkeypatch 基建）；本用例聚焦纯函数
      ``_resolve_bootstrap_config``：占位符守护、secret 文件读取与兼容别名。
    - owner 身份经 env/secret 引导进数据库，代码不再内置默认用户名（隐私整改）。
    """
    print("== bootstrap 秘密解析 fail-closed ==")
    login, pw, legacy = app_mod._resolve_bootstrap_config({})
    check("无配置 → 未配置（本地开发态）",
          pw is None and login == "" and legacy is False,
          "login=%r pw=%r legacy=%r" % (login, pw, legacy))
    login, pw, legacy = app_mod._resolve_bootstrap_config(
        {"ADMIN_PASSWORD": "s3cret-password-x"})
    check("兼容 ADMIN_PASSWORD → 读到秘密且标记 legacy",
          pw == "s3cret-password-x" and legacy is True,
          "pw=%r legacy=%r" % (pw, legacy))
    login, pw, legacy = app_mod._resolve_bootstrap_config(
        {"BOOTSTRAP_OWNER_LOGIN_ID": "boss",
         "BOOTSTRAP_OWNER_PASSWORD_FILE": ""})
    check("新变量名不影响无秘密判定",
          pw is None and login == "boss" and legacy is False)
    raised = False
    try:
        app_mod._resolve_bootstrap_config(
            {"BOOTSTRAP_OWNER_PASSWORD_FILE": "/no/such/file-xyz"})
    except SystemExit as e:
        raised = True
        check("secret 文件不存在 SystemExit 文案含路径",
              "/no/such/file-xyz" in str(e), "msg=%r" % (e,))
    check("secret 文件不存在 → SystemExit", raised)
    assert raised
    empty = tmp_path / "empty.secret"
    empty.write_text("   \n", encoding="utf-8")
    raised = False
    try:
        app_mod._resolve_bootstrap_config(
            {"BOOTSTRAP_OWNER_PASSWORD_FILE": str(empty)})
    except SystemExit:
        raised = True
    check("secret 文件为空 → SystemExit", raised)
    assert raised
    real = tmp_path / "real.secret"
    real.write_text("  file-secret-12345  \n", encoding="utf-8")
    login, pw, legacy = app_mod._resolve_bootstrap_config(
        {"BOOTSTRAP_OWNER_LOGIN_ID": "boss",
         "BOOTSTRAP_OWNER_PASSWORD_FILE": str(real)})
    check("secret 文件读取成功（strip）且不算 legacy",
          pw == "file-secret-12345" and login == "boss" and legacy is False,
          "pw=%r" % (pw,))
    sentinel = app_mod.ADMIN_PASSWORD_PLACEHOLDER_SENTINEL
    docs = Path(__file__).resolve().parents[1] / "docs" / "demo-deployment.md"
    # 文档不随仓分发（隐私整改，仅本地保留）：缺失时跳过文档断言组。
    docs_text = docs.read_text(encoding="utf-8") if docs.exists() else sentinel
    check("文档含精确 sentinel", sentinel in docs_text,
          "missing %r in %s" % (sentinel, docs))
    assert sentinel in docs_text
    check("文档 sentinel 被判定为占位符",
          app_mod._is_placeholder_admin_password(sentinel) is True)
    ph = tmp_path / "ph.secret"
    ph.write_text(sentinel, encoding="utf-8")
    _login2, pw2, _legacy2 = app_mod._resolve_bootstrap_config(
        {"BOOTSTRAP_OWNER_PASSWORD_FILE": str(ph)})
    check("secret 文件内容为占位符 → 视为未配置", pw2 is None)
    check("尖括号占位符判定",
          app_mod._is_placeholder_admin_password("<x>") is True)
    check("空串占位符判定",
          app_mod._is_placeholder_admin_password("") is True)
    check("真实密码不是占位符",
          app_mod._is_placeholder_admin_password("not-a-placeholder") is False)


def test_admin_session_cookie_secure_explicit_only():
    """SESSION_COOKIE_SECURE 只认 ADMIN_SESSION_COOKIE_SECURE，不看证书文件。"""
    print("== ADMIN_SESSION_COOKIE_SECURE 显式开关 ==")
    check("缺省 false（SSH 隧道 HTTP）",
          app_mod._resolve_session_cookie_secure({}) is False)
    check("SHARE_TLS_CERT 存在也不开",
          app_mod._resolve_session_cookie_secure({
              "SHARE_TLS_CERT": "/tmp/fullchain.crt",
              "SHARE_TLS_KEY": "/tmp/privkey.key",
          }) is False)
    check("ADMIN_SESSION_COOKIE_SECURE=1 → True",
          app_mod._resolve_session_cookie_secure({
              "ADMIN_SESSION_COOKIE_SECURE": "1",
          }) is True)
    check("ADMIN_SESSION_COOKIE_SECURE=0 → False",
          app_mod._resolve_session_cookie_secure({
              "ADMIN_SESSION_COOKIE_SECURE": "0",
          }) is False)


def test_missing_slide_returns_400():
    print("== test_missing_slide: slide 缺失 400（不转发到 sidecar）==")
    fake = install_fake_requests()
    client = make_client()
    setup_ai_config()
    r = client.post("/api/ai/run", json={})
    check("run 缺 slide 400", r.status_code == 400, "got %d" % r.status_code)
    check("未转发到 sidecar（无调用）", len(fake.calls) == 0,
          "calls=%d" % len(fake.calls))


def test_healthz_reports_sidecar_and_backend():
    """Stage 4-3：/healthz 返回 backend + sidecar 可达性；sidecar 不可达仍 200。"""
    print("== /healthz: backend + sidecar 可达性，不可达不 fail ==")
    fake = install_fake_requests()
    client = make_client()
    # sidecar 可达（mock /healthz 200）
    fake.register("GET", "/healthz",
                  lambda b, q, h, k: FakeResponse(200, b'{"ok":true}',
                   headers={"Content-Type": "application/json"}))
    r = client.get("/healthz")
    check("healthz 200", r.status_code == 200, "got %d" % r.status_code)
    body = json.loads(r.data)
    check("healthz ok=true", body.get("ok") is True)
    check("healthz backend 存在", "backend" in body, "got %r" % body)
    check("healthz sidecar=reachable", body.get("sidecar") == "reachable",
          "got %r" % body.get("sidecar"))
    # sidecar 不可达 → healthz 仍 200，sidecar=unreachable
    fake.set_unreachable()
    r2 = client.get("/healthz")
    check("sidecar 不可达时 healthz 仍 200", r2.status_code == 200,
          "got %d" % r2.status_code)
    check("sidecar=unreachable", json.loads(r2.data).get("sidecar") == "unreachable",
          "got %r" % json.loads(r2.data).get("sidecar"))
    fake.clear_unreachable()


def test_degradation_platform_independent():
    """Stage 4-3 降级验收：sidecar 不可达时 /api/ai/run 503，viewer API 正常。"""
    print("== 降级：sidecar 不可达 → /api/ai/run 503，viewer(/api/slides) 正常 ==")
    fake = install_fake_requests()
    client = make_client()
    setup_ai_config()
    fake.set_unreachable()
    # viewer API 不依赖 sidecar，应正常（非 503）
    r0 = client.get("/api/slides")
    check("sidecar 不可达时 /api/slides 正常（非 503）", r0.status_code != 503,
          "got %d" % r0.status_code)
    # /api/ai/run 503 形状
    r = client.post("/api/ai/run", json={"slide": "s.svs"})
    check("降级时 /api/ai/run 503", r.status_code == 503, "got %d" % r.status_code)
    body = json.loads(r.data)
    check("降级 503 body error 形状", body.get("error") == "ai sidecar 不可用",
          "got %r" % body)
    fake.clear_unreachable()


# =========================================================================== #
# 插件能力网关注入（插件能力层 docs §5.1/§9，P1）
# =========================================================================== #
def test_official_run_injects_extra_tools_and_tool_token():
    """官方 /api/ai/run：注册表有能力时 config 含 extra_tools + tool_token。"""
    print("== 插件能力注入：官方 run config 含 extra_tools 与 tool_token ==")
    fake = install_fake_requests()
    client = make_client()
    setup_ai_config()
    installation, err = app_mod.install_plugin_bundle("sample-tma-score")
    check("示例插件安装成功（登记能力注册表）", err is None and installation is not None,
          "err=%r" % (err,))
    caps = installation.get("capabilities") if installation else []
    check("安装行登记了 slide_summary 能力",
          [c.get("name") for c in caps] == ["slide_summary"], "caps=%r" % caps)

    captured = {}

    def handler(body, query, headers, kwargs):
        captured["config"] = body.get("config") or {}
        captured["security"] = body.get("security")
        return FakeResponse(200, sse_frames=[], headers={"X-AI-Session-ID": "sess-1"})

    fake.register("POST", "/run", handler)
    resp = client.post("/api/ai/run", json={"slide": "s.svs"})
    check("run 200", resp.status_code == 200, "got %d" % resp.status_code)
    cfg = captured.get("config") or {}
    tools = cfg.get("extra_tools")
    check("run config 含 extra_tools", isinstance(tools, list) and len(tools) == 1,
          "tools=%r" % tools)
    tool = (tools or [{}])[0]
    check("工具名 = pluginId 去点下划线连接__能力名",
          tool.get("name") == "dev_sample_tma__slide_summary", "got %r" % tool.get("name"))
    check("endpoint = dispatch 相对路径",
          tool.get("endpoint") == "/api/plugin/v1/dispatch/dev.sample.tma/slide_summary",
          "got %r" % tool.get("endpoint"))
    check("auth = agent-tool-token", tool.get("auth") == "agent-tool-token")
    check("access_mode = read", tool.get("access_mode") == "read")
    check("timeout_ms 注入", tool.get("timeout_ms") == 15000,
          "got %r" % tool.get("timeout_ms"))
    check("description 拼接了不信任后缀",
          "不可信" in (tool.get("description") or "")
          and "slide_summary" not in (tool.get("description") or ""),
          "got %r" % tool.get("description"))
    # parameters 必须与 manifest 声明逐字一致（§9 锁死 extra_tools 形状；
    # manifest 字段演进时本断言随之更新，不接受手写副本漂移）
    mf = json.loads((REPO_ROOT / "plugins" / "sample-tma-score" / "manifest.json")
                    .read_text(encoding="utf-8"))
    mf_params = (mf.get("provides") or [{}])[0].get("parameters")
    check("parameters 来自 manifest（原样透传）",
          tool.get("parameters") == mf_params,
          "params=%r manifest=%r" % (tool.get("parameters"), mf_params))

    # 注入了 extra_tools → 必须随附 standard-v1 + extra-tools:v1 信封
    # （sidecar fail-closed：缺信封/缺 feature 则整个 run 被 4xx 拒绝）
    sec = captured.get("security")
    check("run 随附 security 信封", isinstance(sec, dict), "got %r" % sec)
    check("信封 = AGENT_EXTRA_TOOLS_ENVELOPE",
          sec == dict(app_mod.AGENT_EXTRA_TOOLS_ENVELOPE), "got %r" % sec)
    if isinstance(sec, dict):
        check("信封声明 tool-profile:standard-v1 + extra-tools:v1",
              sec.get("required_features") == ["tool-profile:standard-v1",
                                               "extra-tools:v1"],
              "features=%r" % sec.get("required_features"))
        check("信封 tool_profile=standard-v1（非只读）",
              sec.get("tool_profile") == "standard-v1",
              "got %r" % sec.get("tool_profile"))
        check("信封不带 ephemeral/ttl（官方 run 非临时会话）",
              "session_ttl_seconds" not in sec,
              "keys=%r" % sorted(sec))

    # tool_token claims 完整性（typ/session/slide/能力清单/exp）
    token = cfg.get("tool_token")
    check("run config 含 tool_token", isinstance(token, str) and bool(token),
          "got %r" % token)
    claims, derr = app_mod._agent_tool_token_decode(token or "")
    check("tool_token 可验签", derr is None and isinstance(claims, dict),
          "err=%r" % derr)
    if isinstance(claims, dict):
        check("claims.typ=agent-tool", claims.get("typ") == "agent-tool")
        check("claims.aud=agent-tool", claims.get("aud") == "agent-tool")
        check("claims.slide 绑定本切片", claims.get("slide") == "s.svs",
              "got %r" % claims.get("slide"))
        check("claims.session_id 起跑时未定（空串，与 run grant 同语义）",
              claims.get("session_id") == "", "got %r" % claims.get("session_id"))
        check("claims.capabilities 为全名清单",
              claims.get("capabilities") == ["dev.sample.tma/slide_summary"],
              "got %r" % claims.get("capabilities"))
        check("claims.exp = 签发时刻 + 会话TTL + 10min",
              abs(claims.get("exp", 0) - (time.time()
                   + app_mod._AGENT_TOOL_TOKEN_TTL_SECONDS)) < 120,
              "exp=%r" % claims.get("exp"))
        check("claims 带 user_id/role（owner 归一）",
              claims.get("role") == "owner", "got %r" % claims.get("role"))
    # agent-tool-token 不得混入 plugin v1 端点（aud 域不同）
    pclaims, perr = app_mod._plugin_jwt_decode(token or "")
    check("agent-tool-token 解不开 plugin JWT 域（跨域拒绝）", perr == "invalid_token",
          "err=%r" % perr)
    # 清理登记，避免影响后续用例
    if installation:
        import share_store as _ss
        _ss.set_installation_capabilities(installation["installation_id"], [])


def test_official_run_without_capabilities_has_no_extra_tools():
    """注册表无能力（默认状态）时 config 不带 extra_tools/tool_token 键。"""
    print("== 插件能力注入：无能力时零键注入（老 sidecar 零感知） ==")
    fake = install_fake_requests()
    client = make_client()
    setup_ai_config()
    captured = {}
    fake.register("POST", "/run",
                  lambda b, q, h, k: (captured.__setitem__("config", b.get("config") or {}),
                                      captured.__setitem__("security", b.get("security")),
                                      FakeResponse(200, sse_frames=[]))[2])
    resp = client.post("/api/ai/run", json={"slide": "s.svs"})
    check("run 200", resp.status_code == 200, "got %d" % resp.status_code)
    cfg = captured.get("config") or {}
    check("config 不含 extra_tools", "extra_tools" not in cfg, "keys=%r" % sorted(cfg))
    check("config 不含 tool_token", "tool_token" not in cfg, "keys=%r" % sorted(cfg))
    check("无能力时不随附 security 信封（旧 sidecar 零感知）",
          captured.get("security") is None, "got %r" % captured.get("security"))


def test_demo_path_never_injects_extra_tools():
    """demo 路径零改动：_build_sidecar_config 永不注入；DEMO 信封无 extra-tools。"""
    print("== 插件能力注入：demo 路径零改动 ==")
    setup_ai_config()
    installation, err = app_mod.install_plugin_bundle("sample-tma-score")
    check("示例插件安装成功", err is None, "err=%r" % (err,))
    # /api/demo/ai/run 直接用 _build_sidecar_config 组装（不经 _ai_run_prepare）
    cfg = app_mod._build_sidecar_config(None)
    check("demo 用的 _build_sidecar_config 不注入 extra_tools",
          cfg is not None and "extra_tools" not in cfg, "keys=%r" % sorted(cfg or {}))
    check("demo 用的 _build_sidecar_config 不注入 tool_token",
          cfg is not None and "tool_token" not in cfg, "keys=%r" % sorted(cfg or {}))
    check("DEMO_REQUIRED_FEATURES 不含 extra-tools:v1",
          "extra-tools:v1" not in app_mod.DEMO_REQUIRED_FEATURES,
          "features=%r" % app_mod.DEMO_REQUIRED_FEATURES)
    # 清理登记
    if installation:
        import share_store as _ss
        _ss.set_installation_capabilities(installation["installation_id"], [])


# ============================================================================ #
# PR1：DeepSeek 官方 provider 配置注入（docs/deepseek-files-api-research.md §4）
# ============================================================================ #
HP_UID_RE = re.compile(r"^hp_[0-9a-f]{32}$")


def setup_official_ai_config(plain_key="sk-official-proxy-1234567890"):
    """落一份合法官方配置（canonical base URL / vision-exp / openai / files）。"""
    app_mod._save_ai_config({
        "provider_kind": "deepseek_official",
        "base_url": "https://api.deepseek.com",
        "api_key": plain_key,
        "model": "deepseek-v4-flash-vision-exp",
        "api_protocol": "openai",
        "prompt_cache_mode": "auto",
        "image_transport": "deepseek_files",
        "files_rollout_percent": 100,
        "files_ttl_seconds": 86400,
        "keep_recent_images": 7,
    })
    return plain_key


class _LogCapture(logging.Handler):
    """捕获 app.logger 全部已格式化消息（验证伪名 user_id 不写日志）。"""

    def __init__(self):
        super().__init__(level=logging.DEBUG)
        self.messages = []

    def emit(self, record):
        try:
            self.messages.append(record.getMessage())
        except Exception:
            pass


def test_official_run_injects_provider_fields_and_pseudonym():
    """官方 run：config 注入 provider 字段 + hp_ 伪名 user_id，且不写日志。"""
    print("== 官方 run：provider 字段 + 伪名 user_id 注入，日志零泄露 ==")
    fake = install_fake_requests()
    client = make_client()
    plain = setup_official_ai_config()
    captured = {}

    def handler(body, query, headers, kwargs):
        captured["config"] = body.get("config") or {}
        return FakeResponse(200, sse_frames=[], headers={"X-AI-Session-ID": "sess-off-1"})

    fake.register("POST", "/run", handler)
    cap = _LogCapture()
    app_mod.app.logger.addHandler(cap)
    old_level = app_mod.app.logger.level
    app_mod.app.logger.setLevel(logging.DEBUG)
    try:
        resp = client.post("/api/ai/run", json={"slide": "s.svs", "task": "看全片"})
    finally:
        app_mod.app.logger.removeHandler(cap)
        app_mod.app.logger.setLevel(old_level)
    check("run 200", resp.status_code == 200, "got %d" % resp.status_code)
    cfg = captured.get("config") or {}
    check("config.provider_kind=deepseek_official",
          cfg.get("provider_kind") == "deepseek_official",
          "got %r" % cfg.get("provider_kind"))
    check("config.image_transport=deepseek_files",
          cfg.get("image_transport") == "deepseek_files",
          "got %r" % cfg.get("image_transport"))
    check("config.files_ttl_seconds 注入（24 小时保留）",
          cfg.get("files_ttl_seconds") == 86400,
          "got %r" % cfg.get("files_ttl_seconds"))
    check("config.files_rollout_percent 注入", cfg.get("files_rollout_percent") == 100,
          "got %r" % cfg.get("files_rollout_percent"))
    check("config.prompt_cache_mode=auto 注入",
          cfg.get("prompt_cache_mode") == "auto",
          "got %r" % cfg.get("prompt_cache_mode"))
    check("官方 config.api_key 明文（解密后交 sidecar）",
          cfg.get("api_key") == plain, "got %r" % cfg.get("api_key"))
    uid = cfg.get("user_id")
    check("伪名 user_id 形如 hp_<32hex>", isinstance(uid, str) and bool(HP_UID_RE.match(uid)),
          "got %r" % uid)
    assert isinstance(uid, str) and HP_UID_RE.match(uid)
    # §4.2：伪名 user_id 不写日志（任何已格式化日志消息均不得含该值）
    leaked = [m for m in cap.messages if uid in m]
    check("伪名 user_id 不出现在日志", not leaked, "leaked=%r" % leaked[:3])
    assert not leaked
    # GET 配置回显同样不含伪名 user_id 与 api_key 明文
    j = client.get("/api/ai/config").get_json()
    check("GET 回显 provider_kind", j.get("provider_kind") == "deepseek_official",
          "got %r" % j.get("provider_kind"))
    check("GET 不回显伪名 user_id", "user_id" not in j, "keys=%r" % sorted(j))
    check("GET 不回显 api_key 明文", plain not in json.dumps(j))


def test_pseudonym_scope_isolation_registered_vs_demo():
    """注册 scope=user_id、Demo scope=demo:capability_id，互不相同且稳定。"""
    print("== 伪名 scope：注册 vs 匿名 Demo（每浏览器隔离）==")
    setup_official_ai_config()
    uid_owner = app_mod._build_sidecar_config()["user_id"]
    uid_reg = app_mod._build_sidecar_config({"role": "user", "user_id": "u-100"})["user_id"]
    uid_reg_again = app_mod._build_sidecar_config({"role": "user", "user_id": "u-100"})["user_id"]
    uid_demo_a = app_mod._build_sidecar_config(
        None, demo_capability_id="dcp_aabbccdd11223344")["user_id"]
    uid_demo_b = app_mod._build_sidecar_config(
        None, demo_capability_id="dcp_11223344aabbccdd")["user_id"]
    for name, v in (("owner", uid_owner), ("reg", uid_reg),
                    ("demo_a", uid_demo_a), ("demo_b", uid_demo_b)):
        check("%s user_id 形如 hp_<32hex>" % name, bool(HP_UID_RE.match(v)),
              "got %r" % v)
    check("注册同 user_id 稳定一致", uid_reg == uid_reg_again,
          "got %r vs %r" % (uid_reg, uid_reg_again))
    check("注册 vs owner 不同", uid_reg != uid_owner)
    check("注册 vs 匿名 Demo 不同", uid_reg != uid_demo_a)
    check("不同浏览器（capability_id）Demo 伪名不同", uid_demo_a != uid_demo_b)
    check("伪名不含 scope 源串", "u-100" not in uid_reg and "dcp_aabb" not in uid_demo_a)
    assert uid_reg != uid_demo_a and uid_demo_a != uid_demo_b


def test_generic_config_injection_unchanged():
    """generic（存量 CPA）配置：provider_kind=generic、无伪名，代理行为不变。"""
    print("== generic 配置注入不回归 ==")
    fake = install_fake_requests()
    client = make_client()
    setup_ai_config()  # base_url=http://llm.example/v1（存量 generic 形态）
    captured = {}
    fake.register("POST", "/run",
                  lambda b, q, h, k: (captured.__setitem__("config", b.get("config") or {}),
                                      FakeResponse(200, sse_frames=[]))[1])
    resp = client.post("/api/ai/run", json={"slide": "s.svs"})
    check("run 200", resp.status_code == 200, "got %d" % resp.status_code)
    cfg = captured.get("config") or {}
    check("推断 provider_kind=generic", cfg.get("provider_kind") == "generic",
          "got %r" % cfg.get("provider_kind"))
    check("默认 image_transport=inline", cfg.get("image_transport") == "inline",
          "got %r" % cfg.get("image_transport"))
    check("默认 files_rollout_percent=0", cfg.get("files_rollout_percent") == 0,
          "got %r" % cfg.get("files_rollout_percent"))
    check("默认 files_ttl_seconds=86400", cfg.get("files_ttl_seconds") == 86400,
          "got %r" % cfg.get("files_ttl_seconds"))
    check("generic 不注入伪名 user_id", "user_id" not in cfg,
          "keys=%r" % sorted(cfg))
    check("base_url 原样透传", cfg.get("base_url") == "http://llm.example/v1",
          "got %r" % cfg.get("base_url"))
    check("api_key 明文注入不变", bool(cfg.get("api_key")),
          "got %r" % cfg.get("api_key"))


if __name__ == "__main__":
    test_run_proxies_with_decrypted_config_and_sse()
    test_continue_and_ask_proxy()
    test_branch_proxy()
    test_run_conflict_409_non_sse_passthrough()
    test_cancel_proxy()
    test_sessions_and_session_detail_proxy()
    test_archive_proxy_paths()
    test_stream_proxy_passes_after_seq_and_last_event_id()
    test_sidecar_down_returns_503()
    test_auth_still_enforced()
    test_require_admin_auth_fail_closed()
    test_admin_session_cookie_secure_explicit_only()
    test_missing_slide_returns_400()
    test_healthz_reports_sidecar_and_backend()
    test_degradation_platform_independent()
    test_official_run_injects_extra_tools_and_tool_token()
    test_official_run_without_capabilities_has_no_extra_tools()
    test_demo_path_never_injects_extra_tools()
    test_official_run_injects_provider_fields_and_pseudonym()
    test_pseudonym_scope_isolation_registered_vs_demo()
    test_generic_config_injection_unchanged()
    print("\nPASS=%d FAIL=%d" % (PASS, FAIL))
    sys.exit(1 if FAIL else 0)
