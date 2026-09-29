# -*- coding: utf-8 -*-
"""C3 本地切片工具页（/tools/slides）服务端验收。

对应计划 docs/browser-slide-tools-and-baidu-plugin-agent-plan-20260929.md §6
（隐私/CSP）、§9 C3（独立工具）；浏览器侧 Playwright 证据在
tests/browser/slide_tools_c3/（见 docs/review-evidence/slide-tools/C3/RERUN.md）。

本文件只做 Flask 层可静态断言的部分：

1. 路由：AUTH_ENABLED=True 且未登录 GET /tools/slides 200（不 302 /login）；
2. 页面级 CSP（ADR 模式 C + worker/wasm 最小面）逐 token 精确匹配；
3. 无内联 script/style（CSP 不放 'unsafe-inline'，也不需要）；
4. i18n：新增 tools.* / entry.nav.slides 键 zh/en 成对；
5. 主页入口：entry.html 有 /tools/slides 链接且带 data-i18n 键；
6. wasm 静态资源以 application/wasm 提供（compileStreaming 前提）。
"""
import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import _bootstrap  # noqa: F401,E402  # noqa: F401
import app as app_mod  # noqa: E402
from _pt_helpers import isolate_app  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent

EXPECTED_CSP = (
    "default-src 'none'; script-src 'self' 'wasm-unsafe-eval'; "
    "worker-src 'self'; connect-src 'self'; style-src 'self'; "
    "img-src 'self' data:; base-uri 'none'; object-src 'none'; "
    "form-action 'none'; frame-ancestors 'none'"
)


@pytest.fixture()
def _iso(monkeypatch):
    isolate_app(monkeypatch, _bootstrap.SHARE_DATA_DIR, clear_stores=True)
    app_mod.AUTH_ENABLED = True
    yield


def _get(path):
    app_mod.app.config["TESTING"] = True
    client = app_mod.app.test_client()
    return client.get(path)


def test_tools_slides_route_public_no_login(_iso):
    """AUTH_ENABLED=True 下未登录 GET /tools/slides == 200（不跳登录）。"""
    r = _get("/tools/slides")
    assert r.status_code == 200, "未登录应直接渲染工具页，而不是 302 /login"
    assert "text/html" in r.headers["Content-Type"]


def test_tools_slides_csp_exact(_iso):
    """页面级 CSP 逐 token 精确匹配（模式 C + wasm/worker），无 unsafe-inline。"""
    r = _get("/tools/slides")
    csp = r.headers.get("Content-Security-Policy")
    assert csp == EXPECTED_CSP
    assert "unsafe-inline" not in csp
    assert "unsafe-eval'" not in csp.replace("'wasm-unsafe-eval'", "")
    # 不放行外部 connect（工具页隐私承诺：除自身静态代码外零网络请求）
    assert "http" not in csp


def test_tools_slides_no_coop_coep_page_headers(_iso):
    """不加 COOP/COEP（worker 非共享内存型；不改全站隔离头）。"""
    r = _get("/tools/slides")
    assert "Cross-Origin-Opener-Policy" not in r.headers
    assert "Cross-Origin-Embedder-Policy" not in r.headers
    # 页面禁止中间缓存/嵌入（与公开介绍页同口径）
    assert "no-store" in r.headers.get("Cache-Control", "")
    assert r.headers.get("X-Frame-Options") == "DENY"
    assert r.headers.get("X-Content-Type-Options") == "nosniff"


def test_tools_slides_no_inline_script_or_style(_iso):
    """无内联 script/style：script 全部带 src，无 <style>、无 style= 属性。"""
    body = _get("/tools/slides").get_data(as_text=True)
    for m in re.finditer(r"<script\b([^>]*)>", body):
        attrs = m.group(1)
        assert "src=" in attrs, "工具页不允许内联 <script>（CSP script-src 'self'）"
    assert "<style" not in body
    assert not re.search(r"\bstyle\s*=", body), "工具页不允许内联 style 属性"
    # 静态资源全部同源
    assert 'src="/static/' in body or 'href="/static/' in body
    assert 'src="http' not in body and "src='http" not in body


def test_tools_slides_assets_exist():
    """页面引用的静态资源存在（模板 → CSS/JS；引擎产物）。"""
    html = (REPO_ROOT / "templates" / "tools_slides.html").read_text(encoding="utf-8")
    for href in re.findall(r'(?:src|href)="(/static/[^"?]+)', html):
        p = REPO_ROOT / href.lstrip("/")
        assert p.is_file(), f"模板引用的静态资源缺失: {href}"
    for rel in ("tools-slides.js", "tools-slides.css"):
        assert (REPO_ROOT / "static" / "tools" / rel).is_file()


def test_wasm_served_as_application_wasm(_iso):
    """slide_transform_bg.wasm 必须以 application/wasm 提供。"""
    r = _get("/static/tools/slide-transform/slide_transform_bg.wasm")
    assert r.status_code == 200
    assert r.headers["Content-Type"] == "application/wasm"


def _i18n_blocks():
    src = (REPO_ROOT / "static" / "i18n.js").read_text(encoding="utf-8")
    zh = src[src.index("zh: {"):src.index("en: {")]
    en_start = src.index("en: {")
    # en 块截止于 DICT 结束（其后是运行时代码，含 'zh'/'zh-CN' 等字面量）
    en_end = src.index("\n  };", en_start)
    en = src[en_start:en_end]
    return zh, en


def _keys(block):
    out = set()
    for m in re.finditer(r'"([A-Za-z0-9_.\-]+)"\s*:', block):
        out.add(m.group(1))
    return out


def test_i18n_tools_keys_zh_en_parity():
    """所有 tools.* 与 entry.nav.slides 新键 zh/en 成对出现。"""
    zh, en = _i18n_blocks()
    zh_keys = _keys(zh)
    en_keys = _keys(en)
    tools_zh = {k for k in zh_keys if k.startswith("tools.") or k == "entry.nav.slides"}
    tools_en = {k for k in en_keys if k.startswith("tools.") or k == "entry.nav.slides"}
    assert tools_zh, "zh 未找到 tools.* 键"
    assert tools_zh == tools_en, (
        f"tools 键 zh/en 不成对：仅 zh={sorted(tools_zh - tools_en)} "
        f"仅 en={sorted(tools_en - tools_zh)}")
    # 全表 parity 顺带守住（本页新键不得破坏既有约定）
    assert zh_keys == en_keys, (
        f"i18n 全表 zh/en 不成对：仅 zh={sorted(zh_keys - en_keys)[:8]} "
        f"仅 en={sorted(en_keys - zh_keys)[:8]}")


def test_i18n_template_keys_all_defined():
    """模板里出现的 data-i18n* 键都在字典中（zh 侧必在）。"""
    zh, _en = _i18n_blocks()
    zh_keys = _keys(zh)
    html = (REPO_ROOT / "templates" / "tools_slides.html").read_text(encoding="utf-8")
    used = set()
    for attr in ("data-i18n", "data-i18n-aria", "data-i18n-ph", "data-i18n-title"):
        used |= set(re.findall(re.escape(attr) + r'="([^"]+)"', html))
    # 页面 JS 动态使用的键（字面量）也必须存在
    js = (REPO_ROOT / "static" / "tools" / "tools-slides.js").read_text(encoding="utf-8")
    used |= set(re.findall(r"t\('(tools\.[A-Za-z0-9_.]+)'", js))
    # 动态拼接的错误码文案键（tools.err.<code>）单独点名校验
    for code in ("unsupported_input", "disk_precheck_failed",
                 "resource_profile_insufficient", "quota_exceeded_recoverable",
                 "io_recoverable", "conversion_output_too_large", "resume_refused",
                 "job_locked_other_tab", "job_dir_missing", "not_ready_not_exportable",
                 "source_changed_refuse_resume", "cancelled"):
        assert f"tools.err.{code}" in zh_keys, f"缺少错误码文案键 tools.err.{code}"
    assert "tools.err.pixel_policy_violation" in zh_keys
    missing = {k for k in used if k not in zh_keys and not k.startswith("lang.") and k != "tools.doc.title"}
    # tools.doc.title 由 JS 设置（键在字典中即可）
    assert not missing, f"模板/JS 使用了未定义的 i18n 键: {sorted(missing)}"


def test_homepage_links_to_tools():
    """主页导航提供「本地切片工具」入口（/tools/slides，带 i18n 键）。"""
    html = (REPO_ROOT / "templates" / "entry.html").read_text(encoding="utf-8")
    assert 'href="/tools/slides"' in html
    assert 'data-i18n="entry.nav.slides"' in html
    # 本地切片工具链接文本存在（zh 默认）
    assert "本地切片工具" in html


def test_homepage_entry_renders_link(_iso):
    """渲染后的主页（AUTH_ENABLED=True 未登录）包含工具入口链接。"""
    r = _get("/")
    assert r.status_code == 200
    body = r.get_data(as_text=True)
    assert 'href="/tools/slides"' in body
