# -*- coding: utf-8 -*-
"""CSP_EXTRA_CONNECT_SRC：entry 页 connect-src 受控放行（COS 直传 Phase 0
门禁，docs/evidence/cos-20260924.md §9.5-1）。

覆盖：

  - 解析：只接受精确 ``https://host[:port]``（空格/逗号分隔、按序去重）；
    拒绝通配、路径、query、http 等放大授权面的表达式；
  - 默认零变化：不设置变量时 /、/login 的 CSP 与历史值逐字节一致
    （connect-src 仍为 'self'）；
  - 追加语义：设置后仅 connect-src 段插入受控源，其余指令不变；
  - fail-fast：非法值在 import 期 SystemExit，不静默忽略。
"""

import os
import subprocess
import sys
from pathlib import Path

import pytest

import app as app_mod  # noqa: E402


_LANDING_CSP_DEFAULT = (
    "default-src 'none'; script-src 'self'; style-src 'self'; "
    "img-src 'self' data:; font-src 'self'; connect-src 'self'; "
    "base-uri 'none'; form-action 'self'; frame-ancestors 'none'"
)

_BUCKET = "https://histopilot-1255456712.cos.ap-shanghai.myqcloud.com"


# --------------------------------------------------------------------------- #
# 解析
# --------------------------------------------------------------------------- #
def test_parse_empty_and_none():
    assert app_mod._parse_csp_extra_connect_sources(None) == ()
    assert app_mod._parse_csp_extra_connect_sources("") == ()
    assert app_mod._parse_csp_extra_connect_sources("   ") == ()


def test_parse_accepts_exact_https_sources():
    assert app_mod._parse_csp_extra_connect_sources(_BUCKET) == (_BUCKET,)
    # 空格/逗号/混合分隔 + 端口 + 按序去重
    assert app_mod._parse_csp_extra_connect_sources(
        "https://a.example.com,  https://b.example.com:8443 https://a.example.com"
    ) == ("https://a.example.com", "https://b.example.com:8443")


@pytest.mark.parametrize("bad", [
    "https://*.cos.ap-shanghai.myqcloud.com",  # 通配子域
    "https://*.myqcloud.com",
    "*",                                       # 全放行
    "http://insecure.example.com",             # 非 https
    "https://bucket.example.com/poc/",         # 带路径
    "https://bucket.example.com?poc=1",
    "https://bucket.example.com#f",
    "bucket.example.com",                      # 裸 host
    "https://",                                # 空 host
    "https://bucket example.com",              # 空格进 host（拆词后仍非法）
    "HTTPS://UPPER.EXAMPLE.COM",               # 非小写 scheme（要求规范化输入）
    "https://host:0",
    "https://host:99999",
])
def test_parse_rejects_broader_or_malformed(bad):
    with pytest.raises(ValueError, match="CSP_EXTRA_CONNECT_SRC"):
        app_mod._parse_csp_extra_connect_sources(bad)


# --------------------------------------------------------------------------- #
# 响应头
# --------------------------------------------------------------------------- #
def test_landing_csp_byte_identical_by_default(monkeypatch):
    """不设置变量时 CSP 逐字节不变（默认零行为变化的回归守卫）。"""
    monkeypatch.setattr(app_mod, "AUTH_ENABLED", True)
    client = app_mod.app.test_client()
    for path in ("/", "/login"):
        r = client.get(path)
        assert r.status_code == 200
        assert r.headers["Content-Security-Policy"] == _LANDING_CSP_DEFAULT


def test_landing_csp_appends_only_connect_src(monkeypatch):
    monkeypatch.setattr(app_mod, "AUTH_ENABLED", True)
    monkeypatch.setattr(
        app_mod, "CSP_EXTRA_CONNECT_SOURCES",
        ("https://a.cos.ap-shanghai.myqcloud.com",
         "https://b.cos.ap-shanghai.myqcloud.com:8443"))
    client = app_mod.app.test_client()
    r = client.get("/")
    csp = r.headers["Content-Security-Policy"]
    assert ("connect-src 'self' https://a.cos.ap-shanghai.myqcloud.com "
            "https://b.cos.ap-shanghai.myqcloud.com:8443;") in csp
    # 其余指令保持不变（拼接不得破坏指令边界）
    assert csp.startswith("default-src 'none'; script-src 'self'; ")
    assert "base-uri 'none'; form-action 'self'; frame-ancestors 'none'" in csp
    assert "connect-src 'self';" not in csp  # 'self' 后必须紧跟追加源而非分号


# --------------------------------------------------------------------------- #
# import 期 fail-fast
# --------------------------------------------------------------------------- #
def test_invalid_env_fails_at_import():
    """非法值必须在 import 期拒启，不能静默忽略后误以为已放行。"""
    env = dict(os.environ)
    env["CSP_EXTRA_CONNECT_SRC"] = "https://*.cos.ap-shanghai.myqcloud.com"
    proc = subprocess.run(
        [sys.executable, "-c", "import app"],
        cwd=str(Path(__file__).resolve().parent.parent),
        env=env, capture_output=True, text=True, timeout=120)
    assert proc.returncode != 0
    assert "CSP_EXTRA_CONNECT_SRC" in (proc.stderr + proc.stdout)
