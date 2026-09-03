# -*- coding: utf-8 -*-
"""Phase 1 认证加固（CSRF / 跨 worker 登录锁定）测试公共基建。

提供两个工具：

1. ``csrf_client(base)``：包装 Flask test client，对非安全方法自动附加
   ``X-CSRF-Token``（先 GET /login 惰性取得 token，与真实前端行为一致）。
   生产 CSRF 校验对测试不放宽——旧行为的测试统一走这个包装。

2. ``install_json_login_limits(monkeypatch, ...)``：json 双跑已退役，本函数恒
   no-op（BACKEND 恒为 postgres，走真实 auth_rate_limits；conftest 每用例
   TRUNCATE 保证隔离）。保留调用点兼容。

3. ``isolate_app(monkeypatch, ...)``：通用 per-test 存储隔离（test-review P3-16
   收敛点）——替代各测试文件自带的高度重复的 ``_isolate`` 主体。文件特有的
   额外 monkeypatch（upload_guard 常量复位等）留在各文件的薄 fixture 里，
   先调本函数再加自己的。

4. ``FakeResponse`` / ``FakeRequests``：fake HistoPilot sidecar 的统一实现
   （test-review P3-17 合一）。此前 test_ai_proxy / test_ai_integrity /
   test_ai_credentials / test_ai_budget_wiring / test_admin_preview /
   test_ai_session_owner / test_demo_access 各持一份逐渐分叉的副本；现在
   以最完整的一份（SSE 帧 + 按 method+path 注册 + calls 记录 + 不可达开关）
   为基础合一，各文件 import 使用。允许子类扩展，禁止再复制粘贴新副本。
"""
import json
import math
import os
import sys
import time
from pathlib import Path

from pg_compat import BACKEND

# --------------------------------------------------------------------------- #
# 计数式断言（统一收敛实现，替代各脚本式测试文件自带的 check()）
# --------------------------------------------------------------------------- #
def check(name, cond, detail=""):
    """计数式断言：在**调用方模块**的 PASS/FAIL 上计数 + print。

    - 脚本直跑（python tests/test_xxx.py）：各文件的汇总计数彼此独立，
      收尾 print / sys.exit 逻辑保持原样（读各自模块的 PASS/FAIL）。
    - pytest 收集运行（PYTEST_CURRENT_TEST 由 pytest 在用例执行期间设置）：
      失败必须 raise——否则计数式断言下每个 test_* 都会「绿」，失败静默漏网。
    """
    g = sys._getframe(1).f_globals
    if cond:
        g["PASS"] = g.get("PASS", 0) + 1
        print("  ok  %s" % name)
        return
    g["FAIL"] = g.get("FAIL", 0) + 1
    print("FAIL  %s  %s" % (name, detail))
    if "PYTEST_CURRENT_TEST" in os.environ:
        raise AssertionError("FAIL %s %s" % (name, detail))


class CsrfClient:
    """werkzeug test client 包装：写方法自动附带 X-CSRF-Token。"""

    def __init__(self, base):
        self._base = base

    # -- token 获取：与前端一致（读非 HttpOnly csrf_token cookie） --
    def _cookie_token(self):
        c = self._base.get_cookie("csrf_token", domain="localhost", path="/")
        return c.value if c is not None else None

    def _token(self):
        tok = self._cookie_token()
        if tok:
            return tok
        # 惰性触发 token 下发（/login 是公开路径；安全方法只下发不校验）
        self._base.get("/login")
        return self._cookie_token()

    def open(self, *args, **kwargs):
        method = (kwargs.get("method") or "GET").upper()
        if method not in ("GET", "HEAD", "OPTIONS"):
            tok = self._token()
            if tok:
                headers = kwargs.pop("headers", None)
                if headers is None:
                    kwargs["headers"] = {"X-CSRF-Token": tok}
                elif isinstance(headers, dict):
                    merged = dict(headers)
                    merged.setdefault("X-CSRF-Token", tok)
                    kwargs["headers"] = merged
                else:  # list / Headers
                    kwargs["headers"] = list(headers) + [("X-CSRF-Token", tok)]
        return self._base.open(*args, **kwargs)

    # 常用快捷方法（与 werkzeug client 对齐）
    def get(self, *a, **kw):
        return self.open(*a, method="GET", **kw)

    def post(self, *a, **kw):
        return self.open(*a, method="POST", **kw)

    def put(self, *a, **kw):
        return self.open(*a, method="PUT", **kw)

    def patch(self, *a, **kw):
        return self.open(*a, method="PATCH", **kw)

    def delete(self, *a, **kw):
        return self.open(*a, method="DELETE", **kw)

    def head(self, *a, **kw):
        return self.open(*a, method="HEAD", **kw)

    def options(self, *a, **kw):
        return self.open(*a, method="OPTIONS", **kw)

    def __getattr__(self, name):
        # set_cookie / get_cookie / session_transaction 等直接委托底层 client
        return getattr(self._base, name)


def csrf_client(base):
    return CsrfClient(base)


# --------------------------------------------------------------------------- #
# 通用 per-test 存储隔离（test-review P3-16 收敛点）
# --------------------------------------------------------------------------- #
def isolate_app(monkeypatch, data_dir, upload_dir=None, login_limits=False,
                clear_stores=False):
    """把存储相关 env/常量指到本用例私有目录，并登记防泄漏还原护栏。

    做的事（PG 后端下这些 json 常量不被读，等价 no-op）：
      - ``SHARE_DATA_DIR`` env + ``user_store.SHARE_DATA_DIR/USER_FILE`` +
        ``share_store.SHARE_DATA_DIR/SHARE_FILE`` → ``data_dir``；
      - ``app.UPLOAD_DIR`` 与 ``share_server.UPLOAD_DIR`` → ``upload_dir``
        （缺省 ``data_dir/uploads``，自动创建）；
      - ``share_store.set_owner_user_id("")`` 清归属注入；
      - ``login_limits=True``：json 后端装登录防爆破内存 mock（默认不装——
        部分用例专门断言 json 生产行为的 503 fail-closed，装了会掩盖）；
      - ``clear_stores=True``：删 ``data_dir`` 下 users/shares json（含 .bak）；
      - 防泄漏护栏：登记 ``app.AUTH_ENABLED`` / ``app.requests`` 的当前值，
        用例内（``_client()`` 辅助函数等）对它们的**裸赋值**在 teardown 一律
        自动还原——「模块级直接赋值不还原」类串扰从根上堵住。

    返回 ``(data_dir, upload_dir)``（均为 ``Path``）。
    """
    import app as app_mod
    import share_server as share_srv
    import share_store
    import user_store

    data_dir = Path(data_dir)
    if upload_dir is None:
        upload_dir = data_dir / "uploads"
    upload_dir = Path(upload_dir)
    upload_dir.mkdir(parents=True, exist_ok=True)

    monkeypatch.setenv("SHARE_DATA_DIR", str(data_dir))
    monkeypatch.setattr(user_store, "SHARE_DATA_DIR", data_dir)
    monkeypatch.setattr(user_store, "USER_FILE", data_dir / "users.json")
    monkeypatch.setattr(share_store, "SHARE_DATA_DIR", data_dir)
    monkeypatch.setattr(share_store, "SHARE_FILE", data_dir / "shares.json")
    # UPLOAD_DIR：app 与 share_server 各有一份模块级常量
    monkeypatch.setattr(app_mod, "UPLOAD_DIR", upload_dir)
    monkeypatch.setattr(share_srv, "UPLOAD_DIR", upload_dir)
    share_store.set_owner_user_id("")
    if login_limits:
        install_json_login_limits(monkeypatch)
    if clear_stores:
        for name in ("users.json", "shares.json",
                     "users.json.bak", "shares.json.bak"):
            p = data_dir / name
            if p.exists():
                p.unlink()
    # 防泄漏还原护栏（值不变、只登记还原）：见 docstring
    monkeypatch.setattr(app_mod, "AUTH_ENABLED", app_mod.AUTH_ENABLED)
    monkeypatch.setattr(app_mod, "requests", app_mod.requests)
    return data_dir, upload_dir


def clear_upload_dir(upload_dir):
    """清空上传目录里的测试切片文件（子目录一并 rmtree）。

    供沿用**模块级** UPLOAD_DIR（跨用例复用、非 tmp_path）的薄 fixture 作
    文件内额外清理。
    """
    import shutil

    for child in Path(upload_dir).iterdir():
        if child.is_file():
            child.unlink()
        else:
            shutil.rmtree(child, ignore_errors=True)


def install_json_login_limits(monkeypatch, account_limit=10, ip_limit=5,
                             lock_seconds=60):
    """postgres 后端恒 no-op：json 双跑已退役，登录防爆破走真实 auth_rate_limits。"""
    return


# --------------------------------------------------------------------------- #
# Fake HistoPilot sidecar（test-review P3-17 合一）
# --------------------------------------------------------------------------- #
class FakeResponse:
    """模拟 requests.Response：普通 JSON / SSE 两种形态。

    - ``content``：bytes / str / dict（dict 自动 json 序列化）；None 缺省 b"{}"
    - ``sse_frames``：给定则按 SSE 形态（content 为帧拼接，iter_content 逐帧吐）
    - ``headers``：显式给定则以其为准；否则按形态补默认 Content-Type
    - ``.json()`` / ``.get_json(silent=)`` / ``.iter_content()`` / ``.close()``
    """

    def __init__(self, status_code=200, content=None, headers=None,
                 sse_frames=None, ctype=None):
        self.status_code = status_code
        if sse_frames is not None:
            self._sse_frames = list(sse_frames)
            self.content = b"".join(self._sse_frames)
            self.headers = {"Content-Type": ctype or "text/event-stream"}
            if headers:
                self.headers.update(headers)
        else:
            self._sse_frames = None
            if content is None:
                content = b"{}"
            elif isinstance(content, dict):
                content = json.dumps(content)
            if isinstance(content, str):
                content = content.encode("utf-8")
            self.content = content
            self.headers = dict(headers or {})
            self.headers.setdefault("Content-Type", ctype or "application/json")
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

    def get_json(self, silent=False):
        try:
            return json.loads(self.content.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            if silent:
                return None
            raise

    def json(self):
        data = self.get_json(silent=True)
        if data is None:
            raise ValueError("No JSON object could be decoded")
        return data


class FakeRequests:
    """替换 app.requests 的 fake HistoPilot（统一实现，见模块 docstring #4）。

    - ``register(method, path, handler)``：handler 形如
      ``lambda body, query, headers, kwargs: FakeResponse(...)``
    - ``register_json`` / ``register_sse``：数据式便捷注册
    - ``calls``：每次分发记录 {method, path, body, query, params, headers}
      （query 与 params 同值，兼容两种历史读取口径）
    - ``unreachable`` / ``set_unreachable()`` / ``clear_unreachable()``：
      模拟 sidecar 宕机（ConnectionError）
    """

    ConnectionError = __import__("requests").ConnectionError
    Timeout = __import__("requests").Timeout

    def __init__(self):
        self._routes = {}  # (METHOD, path) -> handler(body, query, headers, kwargs)
        self.calls = []
        self.unreachable = False

    def register(self, method, path, handler):
        self._routes[(method.upper(), path)] = handler

    def register_json(self, method, path, status=200, body=None, headers=None):
        payload = json.dumps(body if body is not None else {"ok": True}).encode("utf-8")
        self.register(method, path,
                      lambda b, q, h, k: FakeResponse(status, payload,
                                                      headers=headers))

    def register_sse(self, method, path, frames, status=200, headers=None):
        self.register(method, path,
                      lambda b, q, h, k: FakeResponse(status, sse_frames=frames,
                                                      headers=headers))

    def set_unreachable(self):
        self.unreachable = True

    def clear_unreachable(self):
        self.unreachable = False
        self.calls.clear()

    @staticmethod
    def _url_to_path(url):
        """绝对/相对 URL → path（去 scheme/host/query）。"""
        raw = url.split("?")[0]
        for prefix in ("http://", "https://"):
            if raw.startswith(prefix):
                raw = raw[len(prefix):]
        slash = raw.find("/")
        return raw[slash:] if slash >= 0 else "/"

    def _dispatch(self, method, url, **kwargs):
        path = self._url_to_path(url)
        self.calls.append({
            "method": method.upper(), "path": path,
            "body": kwargs.get("json"),
            "query": kwargs.get("params"), "params": kwargs.get("params"),
            "headers": kwargs.get("headers"),
        })
        if self.unreachable:
            raise FakeRequests.ConnectionError("sidecar down (test)")
        handler = self._routes.get((method.upper(), path))
        if handler is None:
            return FakeResponse(404,
                                json.dumps({"error": "no route"}).encode("utf-8"))
        return handler(kwargs.get("json"), kwargs.get("params"),
                       kwargs.get("headers") or {}, kwargs)

    def get(self, url, **kwargs):
        return self._dispatch("GET", url, **kwargs)

    def post(self, url, **kwargs):
        return self._dispatch("POST", url, **kwargs)
