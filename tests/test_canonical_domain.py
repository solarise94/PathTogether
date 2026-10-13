# -*- coding: utf-8 -*-
"""Canonical 域名声明回归（2026-10-13）：histopilot.cn 唯一 canonical。

域名事实：histopilot.cn 为 canonical 主域名（``PUBLIC_BASE_URL`` 严格解析）；
histopilot.com 是同一服务与账号的**国际优化线路**，不是独立站点——以 .com
Host 到达的请求，canonical 仍必须指向 .cn，且不得出现与 canonical 冲突的
hreflang 备选；pt.solarise94.fun 已弃用（nginx 308 保留路径跳 .cn），只作
事实声明出现。

回归面：
- public_pages 口径的可索引公开页（/, /tools/slides, /demo, /legal/<slug>/<ver>）
  恰好输出一条 canonical、等于 origin+path、不带 query；
- entry.html 的登录/注册渲染（私有页与错误态）不输出 canonical；
- /llms.txt、/llms-full.txt 与 /site-map 含 Domains 声明。
"""
import re

import pytest

import app as app_mod
import public_site

CANONICAL_HREF_RE = re.compile(r'<link rel="canonical" href="([^"]*)"')


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(app_mod, "AUTH_ENABLED", True)
    monkeypatch.setitem(app_mod.app.config, "TESTING", True)
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://histopilot.cn")
    return app_mod.app.test_client()


def _com(client, path):
    """以 histopilot.com Host（国际线路）请求。"""
    return client.get(path, headers={"Host": "histopilot.com"})


def _legal_paths():
    return [p.path for p in public_site.public_pages()
            if p.path.startswith("/legal/")]


@pytest.mark.parametrize("path", ["/", "/tools/slides", "/demo"] + _legal_paths())
def test_public_pages_canonical_points_at_cn_regardless_of_host(client, path):
    """可索引公开页：.com Host 请求也恰好一条 canonical，指向 .cn 页面路径。"""
    resp = _com(client, path)
    assert resp.status_code == 200, path
    assert CANONICAL_HREF_RE.findall(resp.text) == \
        ["https://histopilot.cn" + path], path


@pytest.mark.parametrize("path,query", [
    ("/", "?utm_source=spam"),
    ("/tools/slides", "?from=entry"),
    ("/demo", "?lang=en"),
])
def test_canonical_excludes_query_string(client, path, query):
    """canonical 是页面路径本身：统计/分享用 query 参数不进入 canonical。"""
    resp = _com(client, path + query)
    assert resp.status_code == 200
    assert CANONICAL_HREF_RE.findall(resp.text) == \
        ["https://histopilot.cn" + path]


def test_unversioned_legal_canonicalizes_to_versioned_permalink(client):
    """/legal/<slug>（当前版本页）canonical 指向版本化不可变链接。"""
    slug_version = _legal_paths()[0]
    resp = _com(client, slug_version.rsplit("/", 1)[0])
    assert resp.status_code == 200
    assert CANONICAL_HREF_RE.findall(resp.text) == \
        ["https://histopilot.cn" + slug_version]


def test_download_attachment_has_no_canonical(client):
    """?download=1 是 Markdown 附件而非 HTML 页面，不存在 canonical 标签。"""
    slug_version = _legal_paths()[0]
    resp = _com(client, slug_version + "?download=1")
    assert "text/markdown" in resp.headers["Content-Type"]
    assert CANONICAL_HREF_RE.search(resp.text) is None


@pytest.mark.parametrize("path", ["/", "/tools/slides", "/demo", "/site-map"])
def test_no_hreflang_alternates_conflicting_with_canonical(client, path):
    """.com 是线路不是语言备选：不得出现指向 .com 的 hreflang 与 canonical 冲突。"""
    html = _com(client, path).text
    assert "hreflang" not in html, path


@pytest.mark.parametrize("path", ["/login", "/register"])
def test_private_entry_renders_have_no_canonical(client, path):
    """entry.html 的登录/注册渲染（私有页）不输出 canonical。"""
    resp = _com(client, path)
    assert resp.status_code == 200, path
    assert CANONICAL_HREF_RE.search(resp.text) is None, path


def test_login_error_render_has_no_canonical(client):
    """登录失败（CSRF 缺失/口令错误）重渲染 entry.html 错误态：无 canonical。"""
    resp = client.post("/login", data={"login_id": "nobody@example.com",
                                       "password": "wrong-pass"},
                       headers={"Host": "histopilot.com"})
    assert CANONICAL_HREF_RE.search(resp.text) is None


@pytest.mark.parametrize("path", ["/llms.txt", "/llms-full.txt"])
def test_llms_files_declare_domains(client, path):
    """LLM 发现文件声明：canonical 主域名 + 国际线路 + 弃用域名跳转。"""
    body = _com(client, path).text
    assert "https://histopilot.cn" in body, path
    assert "histopilot.com" in body, path
    assert "pt.solarise94.fun" in body, path
    assert "redirect" in body.lower(), path


def test_site_map_declares_domains(client):
    """/site-map 页面含 Domains 声明（canonical 来自 origin()，非硬编码）。"""
    html = _com(client, "/site-map").text
    assert "histopilot.com" in html
    assert "pt.solarise94.fun" in html
    assert "https://histopilot.cn" in html
