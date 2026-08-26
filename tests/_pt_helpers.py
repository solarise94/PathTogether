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

3. ``isolate_app(monkeypatch, ...)``：通用 per-test 存储隔离（test-review P3-16
   收敛点）——替代各测试文件自带的高度重复的 ``_isolate`` 主体。文件特有的
   额外 monkeypatch（upload_guard 常量复位等）留在各文件的薄 fixture 里，
   先调本函数再加自己的。
"""
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
