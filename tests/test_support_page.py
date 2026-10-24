# -*- coding: utf-8 -*-
"""支持项目页（/support）服务端验收。

覆盖：
1. 匿名可访问：AUTH_ENABLED=True 时 GET /support == 200，不 302 /login；
2. 配置缺省（或支付关闭）：说明 + 邮箱联系 + 可陪玩小猫；无任何静态收款
   方式（2026-10-21 评审：静态收款二维码/收款链接已整体退役，赞助只走
   支付宝「AI 网页应用收款」官方下单接口）；
3. 页面级 CSP 与无内联 script/style；图片仅同源；
4. 致谢凭条存在，且不含任何平台无法核实的付款声明（已支付/到账/成功）；
5. 公开发现面：sitemap/robots/site-map/site-stats allowlist 收录 /support；
6. i18n support.*（以及为转换器页脚预留的 tools.support.*）zh/en 成对。
"""
import re
import sys
from pathlib import Path
from xml.etree import ElementTree

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import _bootstrap  # noqa: F401,E402  # noqa: F401
import app as app_mod  # noqa: E402
import public_site  # noqa: E402
import site_stats_store  # noqa: E402
from _pt_helpers import isolate_app  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
SUPPORT_STATIC_DIR = REPO_ROOT / "static" / "support"

EXPECTED_CSP = (
    "default-src 'none'; script-src 'self'; style-src 'self'; "
    "img-src 'self'; font-src 'self'; connect-src 'self'; "
    "base-uri 'none'; object-src 'none'; form-action 'none'; "
    "frame-ancestors 'none'"
)


@pytest.fixture()
def _iso(monkeypatch):
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://histopilot.cn")
    isolate_app(monkeypatch, _bootstrap.SHARE_DATA_DIR, clear_stores=True)
    app_mod.AUTH_ENABLED = True
    yield


def _get(path, **kwargs):
    app_mod.app.config["TESTING"] = True
    client = app_mod.app.test_client()
    return client.get(path, **kwargs)


# --------------------------------------------------------------------------- #
# 1. 路由与公开性
# --------------------------------------------------------------------------- #
def test_support_route_public_no_login(_iso):
    r = _get("/support")
    assert r.status_code == 200, "未登录应直接渲染支持页，而不是 302 /login"
    assert "text/html" in r.headers["Content-Type"]
    assert 'data-page="support"' in r.get_data(as_text=True)


def test_support_head_matches_get(_iso):
    client = app_mod.app.test_client()
    head = client.head("/support")
    assert head.status_code == 200
    assert not head.data


# --------------------------------------------------------------------------- #
# 2. 配置缺省：可陪玩的小猫 + 邮箱联系；无收款区域/付款按钮/「暂未开放」卡
# --------------------------------------------------------------------------- #
def test_support_default_shows_cat_and_contact(_iso):
    body = _get("/support").get_data(as_text=True)
    # 不再出现「线上赞助方式暂未开放」卡片；这个位置是可以陪玩的小猫
    assert "线上赞助方式暂未开放" not in body
    assert 'id="cat-stage"' in body
    assert 'id="cat-toy"' in body
    assert 'data-i18n="support.cat.caption"' in body
    assert 'data-i18n-aria="support.cat.aria"' in body
    # 小猫是可聚焦的按钮式元素（键盘可玩），装饰 SVG 对读屏隐藏
    assert re.search(
        r'<div class="cat-stage" id="cat-stage" role="button" tabindex="0"', body)
    # 使用提示整节在未配置状态隐藏
    assert 'id="tips"' not in body
    assert "收款方式开放后" not in body
    # 无任何收款方式
    assert 'id="pay-qr"' not in body
    assert 'id="alipay-link"' not in body
    assert 'id="payment"' not in body
    # 不渲染任何空二维码 / 损坏图片
    assert '<img src="/static/support/' not in body
    # 邮箱仍在「联系与返回」；仍保留转换器与主页入口
    assert "solarise94@gmail.com" in body
    assert 'href="/tools/slides"' in body
    assert 'href="/"' in body


def test_support_cat_is_inline_svg_without_external_assets(_iso):
    """小猫是手写内联 SVG：无 <img>、无 data:/外链资源、无内联样式。"""
    body = _get("/support").get_data(as_text=True)
    assert "<svg" in body and "class=\"cat-svg\"" in body
    assert "data:image" not in body
    assert "url(http" not in body
    assert not re.search(r"\bstyle\s*=", body)


def test_support_default_security_headers(_iso):
    r = _get("/support")
    assert r.headers.get("Content-Security-Policy") == EXPECTED_CSP
    assert "unsafe-inline" not in r.headers["Content-Security-Policy"]
    assert "no-store" in r.headers.get("Cache-Control", "")
    assert r.headers.get("X-Frame-Options") == "DENY"
    assert r.headers.get("X-Content-Type-Options") == "nosniff"
    assert r.headers.get("Referrer-Policy") == "strict-origin-when-cross-origin"
    # 公开页不设 noindex；canonical 指向主域名
    assert "noindex" not in (r.headers.get("X-Robots-Tag") or "")


# --------------------------------------------------------------------------- #
# 3. 静态收款方式退役（2026-10-21 用户评审）：任何配置残留都不得渲染
# --------------------------------------------------------------------------- #
def test_support_static_collection_removed_even_with_stale_config(
        _iso, monkeypatch):
    """退役回归：即使部署环境残留 URL/QR 配置（属性已不存在，仅以
    raising=False 模拟），页面也绝不渲染二维码/收款链接——赞助只走官方
    下单接口。"""
    monkeypatch.setattr(app_mod, "SUPPORT_ALIPAY_URL",
                        "https://qr.alipay.com/review-placeholder",
                        raising=False)
    monkeypatch.setattr(app_mod, "SUPPORT_ALIPAY_QR_PATH", "/etc/passwd",
                        raising=False)
    body = _get("/support").get_data(as_text=True)
    assert 'id="alipay-link"' not in body
    assert "qr.alipay.com" not in body
    assert 'id="pay-qr"' not in body
    assert 'id="payment"' not in body
    assert 'id="tips"' not in body
    assert '<img src="/static/support/' not in body
    # 页面仍是完整说明页：小猫 + 邮箱联系
    assert 'id="cat-stage"' in body
    assert "solarise94@gmail.com" in body


# --------------------------------------------------------------------------- #
# 6. 无内联 script/style；静态资源仅同源
# --------------------------------------------------------------------------- #
def test_support_no_inline_script_or_style(_iso):
    body = _get("/support").get_data(as_text=True)
    for m in re.finditer(r"<script\b([^>]*)>", body):
        assert "src=" in m.group(1), "支持页不允许内联 <script>"
    assert "<style" not in body
    assert not re.search(r"\bstyle\s*=", body), "支持页不允许内联 style 属性"
    # 同源静态资源存在
    assert 'href="/static/support/support.css' in body
    assert 'src="/static/support/support.js' in body
    # 无第三方脚本/图片/链接
    assert 'src="http' not in body and "src='http" not in body
    assert "https://qr.alipay.com" not in body  # 默认无配置
    for href in re.findall(r'(?:src|href)="(/static/[^"?]+)', body):
        assert (REPO_ROOT / href.lstrip("/")).is_file(), href


# --------------------------------------------------------------------------- #
# 7. 致谢凭条：打印时刻在弹层已支付窗格/回跳页；/support 不渲染待机打印机
# --------------------------------------------------------------------------- #
def test_support_receipt_idle_printer_removed_without_payment_claims(_iso):
    """/support 不渲染待机打印机与静音提示（打印时刻只在桌面弹层的已支付
    窗格/移动端回跳页）；凭条本体与打印动画不在本页。"""
    body = _get("/support").get_data(as_text=True)
    assert "receipt-stage-idle" not in body
    assert "receipt-window-idle" not in body
    assert "receipt-idle-paper" not in body
    assert "receipt-hint" not in body
    assert "完成支持后，收款机会弹出来为你打印一张致谢凭条" not in body
    assert "凭条上的礼物可以拿去喂小猫" not in body
    assert 'id="receipt"' not in body       # 凭条本体不在 /support
    assert "receipt-replay" not in body     # 重播按钮也不在
    # 平台无法核实付款：不得出现到账/已支付/成功状态
    for forbidden in ("我已支付", "已支付", "到账", "付款成功", "赞助名单",
                      "累计金额", "交易号", "订单号"):
        assert forbidden not in body, forbidden


def test_support_receipt_css_smooth_print_and_reduced_motion():
    """凭条动画改为单段缓出 transform 滑出（无 steps——不再一点点卡顿）；
    prefers-reduced-motion 下静态呈现；纹理全部由 CSS 渐变构成。"""
    css = (SUPPORT_STATIC_DIR / "support.css").read_text(encoding="utf-8")
    assert "@keyframes receipt-slide-out" in css
    assert ".receipt-live" in css
    assert "steps(" not in css, "凭条打印不再使用逐帧 steps()"
    assert "prefers-reduced-motion: reduce" in css
    assert "linear-gradient" in css and "repeating-linear-gradient" in css
    assert "url(http" not in css and 'url("http' not in css


def test_support_receipt_date_logic_moved_to_return_page():
    """凭条逻辑随打印时刻一起移到回跳页：support.js 不再包含凭条函数；
    support_pay.js 负责重播（receipt-live 类切换）与本地日期回填。"""
    support_js = (SUPPORT_STATIC_DIR / "support.js").read_text(encoding="utf-8")
    assert "syncReceiptDate" not in support_js
    assert "receipt-replay" not in support_js
    pay_js = (SUPPORT_STATIC_DIR / "support_pay.js").read_text(encoding="utf-8")
    assert "receipt-live" in pay_js
    assert "receipt-replay" in pay_js
    # 本地日期回填：浏览器本地时区的今天（YYYY-MM-DD），不是 toISOString
    assert "querySelector('time')" in pay_js
    for part in ("getFullYear()", "getMonth()", "getDate()"):
        assert part in pay_js, part
    assert "toISOString" not in pay_js
    assert "setAttribute('datetime', iso)" in pay_js
    assert "time.textContent = iso" in pay_js


# --------------------------------------------------------------------------- #
# 8. 公开发现面（sitemap / robots / site-map / 访问统计 allowlist）
# --------------------------------------------------------------------------- #
def test_support_in_public_sitemap_robots_and_site_map(_iso):
    pages = [p.path for p in public_site.public_pages()]
    assert "/support" in pages
    assert site_stats_store.PAGE_ALLOWLIST.get("/support") == "support"

    xml = _get("/sitemap.xml")
    assert xml.status_code == 200
    urls = [n.text for n in ElementTree.fromstring(xml.data)
            .findall("{*}url/{*}loc")]
    assert "https://histopilot.cn/support" in urls

    robots = _get("/robots.txt").text
    assert "Allow: /support$" in robots

    site_map = _get("/site-map").text
    assert 'href="/support"' in site_map


def test_support_canonical_uses_public_origin_regardless_of_host(_iso):
    html = _get("/support", headers={"Host": "histopilot.com"}).get_data(as_text=True)
    assert re.findall(r'<link rel="canonical" href="([^"]*)"', html) == \
        ["https://histopilot.cn/support"]


# --------------------------------------------------------------------------- #
# 9. i18n
# --------------------------------------------------------------------------- #
def _i18n_blocks():
    src = (REPO_ROOT / "static" / "i18n.js").read_text(encoding="utf-8")
    zh = src[src.index("zh: {"):src.index("en: {")]
    en_start = src.index("en: {")
    en_end = src.index("\n  };", en_start)
    return zh, src[en_start:en_end]


def _keys(block):
    return set(re.findall(r'"([A-Za-z0-9_.\-]+)"\s*:', block))


def test_i18n_support_keys_zh_en_parity():
    zh, en = _i18n_blocks()
    zh_keys, en_keys = _keys(zh), _keys(en)
    support_zh = {k for k in zh_keys if k.startswith("support.")}
    support_en = {k for k in en_keys if k.startswith("support.")}
    assert support_zh
    assert support_zh == support_en, (
        "support 键 zh/en 不成对：仅 zh=%s 仅 en=%s"
        % (sorted(support_zh - support_en), sorted(support_en - support_zh)))
    # 为转换器页脚预留的键也必须成对
    tools_support_zh = {k for k in zh_keys if k.startswith("tools.support.")}
    tools_support_en = {k for k in en_keys if k.startswith("tools.support.")}
    assert tools_support_zh == tools_support_en
    assert {
        "tools.support.prompt", "tools.support.link",
        "tools.support.link.aria", "tools.support.privacy",
    } <= tools_support_zh
    # 全表 parity 顺带守住
    assert zh_keys == en_keys


def test_support_template_i18n_keys_defined():
    zh, _en = _i18n_blocks()
    zh_keys = _keys(zh)
    used = set()
    for name in ("support.html", "support_pay.html", "support_return.html"):
        html = (REPO_ROOT / "templates" / name).read_text(encoding="utf-8")
        for attr in ("data-i18n", "data-i18n-aria", "data-i18n-ph",
                     "data-i18n-title"):
            used |= set(re.findall(re.escape(attr) + r'="([^"]+)"', html))
    for name in ("support.js", "support_pay.js"):
        js = (SUPPORT_STATIC_DIR / name).read_text(encoding="utf-8")
        used |= set(re.findall(r"t\('(support[a-z]*\.[A-Za-z0-9_.]+)'", js))
    # R2：data-i18n 可能是 Jinja 动态表达式（如 support.tier.{{ tier }}，
    # 档位名键已逐一定义），字面校验跳过含 {{ 的键。R4：JS 侧同理存在动态
    # 拼接键（如 t('support.cat.fed.' + kind)，四种礼物键已逐一在词典定义），
    # 跳过以「.」结尾的前缀片段。
    missing = {k for k in used
               if k not in zh_keys and not k.startswith("lang.")
               and "{{" not in k and not k.endswith(".")}
    assert not missing, "模板/JS 使用了未定义的 i18n 键: %s" % sorted(missing)
