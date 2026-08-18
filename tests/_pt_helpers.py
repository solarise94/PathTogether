# -*- coding: utf-8 -*-
"""Phase 1 认证加固（CSRF / 跨 worker 登录锁定）测试公共基建。

提供两个工具：

1. ``csrf_client(base)``：包装 Flask test client，对非安全方法自动附加
   ``X-CSRF-Token``（先 GET /login 惰性取得 token，与真实前端行为一致）。
   生产 CSRF 校验对测试不放宽——旧行为的测试统一走这个包装。

2. ``install_json_login_limits(monkeypatch, ...)``：json 后端（RUN_PG_TESTS 未开）
   下的登录防爆破内存 mock（两桶独立计数，与 auth_limit_store 语义对齐）。
   PG 后端不安装，走真实 auth_rate_limits（conftest 每用例 TRUNCATE 保证隔离）。
   背景：app.py 已删除 per-worker 内存字典（docs §11.1-7），json 后端生产路径
   POST /login 一律 503 fail-closed；需要真实登录流程的旧测试在 json 模式显式装
   本 mock（等价「改为 mock store」的验收路径）。
"""
import math
import time

from pg_compat import BACKEND


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


def install_json_login_limits(monkeypatch, account_limit=10, ip_limit=5,
                             lock_seconds=60):
    """json 后端：安装两桶内存 mock；postgres 后端 no-op（用真实 store）。"""
    if BACKEND == "postgres":
        return
    import app as app_mod

    state = {}  # subject_hash -> [failed_count, locked_until]

    def check(account_hash, ip_prefix_hash):
        now = time.time()
        retry = 0
        for h in (account_hash, ip_prefix_hash):
            rec = state.get(h)
            if rec and rec[1] > now:
                retry = max(retry, int(math.ceil(rec[1] - now)))
        return retry

    def record(account_hash, ip_prefix_hash):
        now = time.time()
        retry = 0
        for h, limit in ((account_hash, account_limit), (ip_prefix_hash, ip_limit)):
            if not h:
                continue
            rec = state.get(h, [0, 0.0])
            rec[0] += 1
            if rec[0] >= limit:
                rec[1] = max(rec[1], now + lock_seconds)
            state[h] = rec
            if rec[1] > now:
                retry = max(retry, int(math.ceil(rec[1] - now)))
        return retry

    def clear(account_hash, ip_prefix_hash):
        state.pop(account_hash, None)
        state.pop(ip_prefix_hash, None)

    monkeypatch.setattr(app_mod, "_login_limits_available", lambda: True)
    monkeypatch.setattr(app_mod, "_check_login_locked", check)
    monkeypatch.setattr(app_mod, "_record_login_failure", record)
    monkeypatch.setattr(app_mod, "_clear_login_failures", clear)
