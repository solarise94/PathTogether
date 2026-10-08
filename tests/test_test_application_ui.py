# -*- coding: utf-8 -*-
"""注册/测试申请 UI 模板轻量测试（2026-10-08 §4 邀请码激活退役后）。

保留面：
- verify_email.html：旧 email_verify 链接（无 intent）渲染「注册流程已更新」
  引导重走公开注册；expired/consumed/unknown 状态页无表单；
- _login_dialog.html：注册弹窗只剩 public 表单 + closed 关闭态说明（无
  邀请码输入）；public 流程表单断言见 test_public_registration；
- activate.html 已删除（激活页退役，GET /activate 302 /login——行为层由
  test_email_verify_activation.test_activation_endpoints_retired 锁定）；
- static/i18n.js：verify.* / activate.* 键 zh/en 双语成对（i18n.js 由其它
  线维护，键不随模板删除而移除）。

写法跟随 tests/test_phase1_auth_ui.py：Jinja 直接渲染模板 + 源码子串守卫，
不起 PG 事务（纯模板/静态资源层，任何后端可跑）。
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402

from flask import render_template  # noqa: E402

import _bootstrap  # noqa: E402,F401  # session 目录 + openslide stub（conftest 先行）
import app as app_mod  # noqa: E402
from _pt_helpers import isolate_app  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent

VERIFY_KEYS = (
    "verify.badge", "verify.valid.title", "verify.valid.desc",
    "verify.password", "verify.password.ph", "verify.password_confirm",
    "verify.err.pw.mismatch", "verify.err.pw.length",
    "verify.err.generic", "verify.err.network", "verify.footer",
)
# 2026-10-08 §4：activate.* 与 verify.apply.*（申请测试表单/激活页）键
# 已随模板删除从 i18n.js 移除（zh/en 同删；见 static/i18n.js）
RETIRED_KEY_FAMILIES = ("activate.", "verify.apply.")


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    """每用例：临时目录夺回（与 test_phase1_auth_ui 同口径）。"""
    isolate_app(monkeypatch, _bootstrap.SHARE_DATA_DIR, clear_stores=True)
    yield


def _render(template, **ctx):
    with app_mod.app.test_request_context():
        return render_template(template, **ctx)


def _verify_email_html():
    """legacy（无 intent）token 的 valid 渲染（2026-10-08 §4：退役文案）。"""
    return _render("verify_email.html", state="valid",
                   email_masked="a***@x.com", token="tok-1", csrf_token="c-1",
                   flow="legacy", public_ctx=None)


# =========================================================================== #
# 1. verify_email.html：legacy 链接退役文案 + 状态页无表单
# =========================================================================== #
def test_verify_email_legacy_link_renders_retirement_notice():
    html = _verify_email_html()
    assert "注册流程已更新" in html
    # 不再渲染旧「申请测试」表单/邀请码语义
    assert 'id="verify-form"' not in html
    assert 'name="research_direction"' not in html
    assert 'name="share_research_data"' not in html
    assert "申请测试" not in html
    # 引导出口
    assert 'href="/register"' in html
    assert 'href="/login"' in html


def test_verify_email_non_valid_states_render_without_form():
    for state in ("expired", "consumed", "unknown"):
        html = _render("verify_email.html", state=state,
                       email_masked=None, token="", csrf_token="c-1")
        assert 'id="verify-form"' not in html
        assert 'name="research_direction"' not in html


# =========================================================================== #
# 2. activate.html 已删除（2026-10-08 §4；激活面退役）
# =========================================================================== #
def test_activate_template_removed():
    assert not (REPO_ROOT / "templates" / "activate.html").exists()
# =========================================================================== #
# 3. 注册弹窗（R2 2026-09-19）：注册流程说明
# =========================================================================== #
def test_register_copy_closed_and_public_only():
    """2026-10-08 §4：注册弹窗只剩 public 表单 + closed 关闭态；邀请码输入
    与「等候邀请」文案删除。"""
    assert not (REPO_ROOT / "templates" / "register.html").exists()
    text = (REPO_ROOT / "templates" / "_login_dialog.html").read_text(encoding="utf-8")
    # 邀请码输入与等候邀请文案已删（_login_dialog 渲染产物内无邀请语义；
    # entry.html 介绍主页的入口文案归其它线维护，不在本断言范围）
    assert 'name="invite_token"' not in text
    assert "管理员审核通过后即可使用" not in text
    assert "测试账号由管理员创建" not in text
    # public 首屏文案 + 注册表单复用既有 POST /register API
    assert "验证邮箱并设置密码，即可开始使用。" in text
    assert 'action="/register"' in text
    assert 'id="register-dialog-form"' in text
    # closed 关闭态说明（无表单语义由渲染分支给出）
    html = _landing_register_html(mode="closed")
    assert "当前未开放注册" in html
    assert 'id="register-dialog-form"' not in html
    # public 模式渲染表单（协议复选框）
    html_pub = _landing_register_html(mode="public")
    assert 'id="register-dialog-form"' in html_pub
    assert 'name="email"' in html_pub
    assert 'name="terms_accepted"' in html_pub


def _landing_register_html(mode):
    """经 _register_landing_page 的模板链渲染（entry.html + _login_dialog.html）。

    public 模式带双协议占位（文稿发布检查在行为层，模板层只需键存在）。"""
    extra = {}
    if mode == "public":
        extra = {
            "register_terms": {"version": "v1", "content_sha256": "h1",
                               "url": "/legal/user-agreement/v1"},
            "register_research": {"version": "v1", "content_sha256": "h1",
                                  "url": "/legal/research-sharing/v1"},
        }
    with app_mod.app.test_request_context("/"):
        return render_template(
            "entry.html",
            signed_in=False, csrf_token="c-1",
            login_open=False, login_error=None, login_error_code=None,
            login_next_url="/app", login_retry_after=0,
            login_password_changed=False,
            register_open=True, registration_mode=mode,
            register_error=None, register_error_code=None,
            register_done=False, register_retry_after=0, **extra)


# =========================================================================== #
# 4. i18n.js：verify.* / activate.* 新键 zh/en 双语成对
# =========================================================================== #
def test_i18n_verify_activate_keys_bilingual():
    i18n = (REPO_ROOT / "static" / "i18n.js").read_text(encoding="utf-8")
    zh_block = i18n[i18n.index("zh: {"):i18n.index("en: {")]
    en_block = i18n[i18n.index("en: {"):]
    for key in VERIFY_KEYS:
        assert '"%s"' % key in zh_block, "i18n.js zh 缺键：%r" % key
        assert '"%s"' % key in en_block, "i18n.js en 缺键：%r" % key
    # 退役键族不得残留（zh/en 都删）
    import re as _re
    for prefix in RETIRED_KEY_FAMILIES:
        assert not _re.search(r'"%s[A-Za-z0-9_.]*"' % _re.escape(prefix),
                              zh_block), "zh 残留退役键族 %r" % prefix
        assert not _re.search(r'"%s[A-Za-z0-9_.]*"' % _re.escape(prefix),
                              en_block), "en 残留退役键族 %r" % prefix
        assert "我愿意向研究团队分享" not in zh_block
        assert "Does not affect application review" not in en_block


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
