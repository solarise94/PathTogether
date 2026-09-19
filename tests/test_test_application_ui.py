# -*- coding: utf-8 -*-
"""测试申请（test_applications）注册/激活 UI 接线轻量测试（ser8，wip/ser8-dev）。

产品要求：新用户验证邮箱后，在设置密码的同一流程完成「申请测试」：
- verify_email.html（state=valid）：research_direction 四选项（radio）+
  share_research_data 勾选框（默认不勾）+ fieldset/legend a11y；
  前端缺方向先拦截；fetch JSON body 恒含两字段（checkbox 未勾时 false）；
- activate.html：双区块「申请测试（默认）/ 邀请码激活」；
  申请状态机 none/pending/rejected/approved/activated_by_invite +
  POST /api/account/test-application 带 X-CSRF-Token；等待页「刷新状态」
  按钮 + 焦点/可见恢复刷新 + 有界轮询（R7 2026-09-19）；401 引导重新登录
  （不解释为审批通过）、503/网络错误保留重试状态；邀请码逻辑保留；
- _login_dialog.html（R2：注册并入介绍主页弹窗，register.html 已删）：
  流程说明「验证邮箱并提交申请，管理员审核通过后即可使用」；
- static/i18n.js：verify.* / activate.* 命名空间新键 zh/en 双语成对。

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

DIRECTION_VALUES = ("model_plant", "model_animal",
                    "clinical_pathology", "other")
DIRECTION_KEYS = tuple("verify.apply.direction.%s" % v for v in DIRECTION_VALUES)

VERIFY_KEYS = (
    "verify.badge", "verify.valid.title", "verify.valid.desc",
    "verify.password", "verify.password.ph", "verify.password_confirm",
    "verify.err.pw.mismatch", "verify.err.pw.length",
    "verify.err.generic", "verify.err.network", "verify.footer",
) + DIRECTION_KEYS + (
    "verify.apply.direction.legend", "verify.apply.direction.required",
    "verify.apply.share.label", "verify.apply.share.hint",
    "verify.apply.submit", "verify.apply.submitting",
    "verify.apply.err.generic", "verify.apply.err.network",
)
ACTIVATE_KEYS = (
    "activate.badge", "activate.title", "activate.desc",
    "activate.tab.apply", "activate.tab.invite",
    "activate.state.checking", "activate.state.load.fail",
    "activate.state.none.desc", "activate.state.pending.desc",
    "activate.state.rejected.desc", "activate.state.approved.desc",
    "activate.state.approved.login", "activate.state.refresh",
    "activate.state.invite_activated.desc", "activate.state.auth.desc",
    "activate.state.auth.login", "activate.reapply",
    "activate.invite.hint", "activate.invite.code", "activate.invite.code.ph",
    "activate.invite.identity.hint", "activate.invite.submit",
    "activate.invite.activating", "activate.invite.need_code",
    "activate.invite.err.auth", "activate.invite.err.generic",
    "activate.err.network", "activate.logout", "activate.footer",
)


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    """每用例：临时目录夺回（与 test_phase1_auth_ui 同口径）。"""
    isolate_app(monkeypatch, _bootstrap.SHARE_DATA_DIR, clear_stores=True)
    yield


def _render(template, **ctx):
    with app_mod.app.test_request_context():
        return render_template(template, **ctx)


def _verify_email_html():
    return _render("verify_email.html", state="valid",
                   email_masked="a***@x.com", token="tok-1", csrf_token="c-1")


def _activate_html():
    return _render("activate.html", email_masked="a***@x.com", csrf_token="c-1")


# =========================================================================== #
# 1. verify_email.html：设置密码同流程申请测试
# =========================================================================== #
def test_verify_email_has_direction_radios_and_share_checkbox():
    html = _verify_email_html()
    # 研究方向四选一（radio，同 name；JS 选择器另计，故统计 radio 元素）
    assert html.count('name="research_direction" type="radio"') == 4
    assert html.count('name="research_direction"') == 5  # 4 输入 + 1 JS 选择器
    for value in DIRECTION_VALUES:
        assert 'value="%s"' % value in html
    # 数据分享勾选框：checkbox、默认不勾、value=1
    assert 'id="share_research_data"' in html
    assert 'name="share_research_data"' in html
    assert 'type="checkbox"' in html
    assert "checked" not in html.split('id="share_research_data"')[1][:200]
    # 自愿说明文案（zh 默认）
    assert "此项为自愿选择，不影响测试申请审批" in html


def test_verify_email_a11y_fieldset_legend_and_labels():
    html = _verify_email_html()
    # radio 组用 fieldset + legend
    assert "<fieldset" in html and "<legend" in html
    # label for 与 radio id 一一对应
    ids = ('vd-model_plant', 'vd-model_animal',
           'vd-clinical_pathology', 'vd-other')
    for i in ids:
        assert 'for="%s"' % i in html
        assert 'id="%s"' % i in html
    assert 'for="share_research_data"' in html
    # 错误提示 role=alert
    assert 'role="alert"' in html


def test_verify_email_js_intercepts_missing_direction_and_always_submits_fields():
    html = _verify_email_html()
    # 缺 research_direction 时前端先校验拦截
    assert 'input[name="research_direction"]:checked' in html
    # JSON body 恒含两字段（checkbox 未勾时提交 false）
    assert "research_direction: direction.value" in html
    assert "share_research_data: form.share_research_data.checked" in html
    # 仍提交到 /api/registration/verify + CSRF 头
    assert 'fetch("/api/registration/verify"' in html
    assert '"X-CSRF-Token"' in html


def test_verify_email_submit_button_is_apply_copy():
    html = _verify_email_html()
    assert 'data-i18n="verify.apply.submit"' in html
    assert ">申请测试</button>" in html
    assert "完成验证并创建账号" not in html


def test_verify_email_copy_mentions_submit_creates_account_and_application():
    html = _verify_email_html()
    assert "提交后将创建账号并提交测试申请" in html
    assert "管理员审核通过后即可使用" in html


def test_verify_email_non_valid_states_render_without_form():
    for state in ("expired", "consumed", "unknown"):
        html = _render("verify_email.html", state=state,
                       email_masked=None, token="", csrf_token="c-1")
        assert 'id="verify-form"' not in html
        assert 'name="research_direction"' not in html


# =========================================================================== #
# 2. activate.html：申请测试（默认）/ 邀请码激活 双区块
# =========================================================================== #
def test_activate_has_apply_and_invite_panels():
    html = _activate_html()
    # 双区块 + 标签（申请测试为默认显示）
    assert 'id="panel-apply"' in html and 'id="panel-invite"' in html
    assert 'data-i18n="activate.tab.apply"' in html
    assert 'data-i18n="activate.tab.invite"' in html
    # 默认面板：panel-apply 不带 hidden，panel-invite 折叠
    assert 'id="panel-apply" role="tabpanel"' in html
    assert 'id="panel-invite" role="tabpanel" aria-labelledby="tab-invite" hidden' in html
    # 顶部说明两种方式
    assert "管理员审核" in html and "邀请码" in html


def test_activate_apply_panel_form_fields_and_state_machine():
    html = _activate_html()
    # 申请表单（兜底）：同 verify 的 radio + checkbox
    assert html.count('name="research_direction" type="radio"') == 4
    for value in DIRECTION_VALUES:
        assert 'value="%s"' % value in html
    assert 'name="share_research_data"' in html
    assert "<fieldset" in html and "<legend" in html
    assert 'role="alert"' in html
    # 状态机文案：pending / rejected（重新申请）/ approved /
    # activated_by_invite（邀请码收口）
    assert "申请已提交，请等待管理员审核" in html
    assert "重新申请" in html
    assert 'id="apply-retry"' in html
    assert 'id="apply-invite-activated"' in html
    assert "已通过邀请码激活" in html
    # JS 状态机：GET 状态 + 全部分流（含 activated_by_invite）
    assert 'fetch("/api/account/test-application"' in html
    for state in ('"none"', '"pending"', '"rejected"', '"approved"',
                  '"activated_by_invite"'):
        assert state in html


def test_activate_pending_block_has_refresh_and_stale_guard():
    """R7：等待页可刷新——pending 区块「刷新状态」按钮 + 焦点/可见恢复刷新 +
    有界轮询 + 离开页面停止 + seq 守卫防旧响应覆盖新状态。

    2026-09-19 修复（轮询上限失效）：轮询预算/定时器控制迁至
    static/activate-waiting.js（预算只在进入等待态武装一次、耗尽保持停止），
    模板断言同步指向新接线。"""
    html = _activate_html()
    assert 'id="apply-refresh"' in html
    assert "刷新状态" in html
    assert 'id="apply-load-retry"' in html  # 读取失败态同样提供重试按钮
    # 焦点/可见恢复刷新（节流）
    assert 'window.addEventListener("focus", autoRefresh)' in html
    assert 'document.addEventListener("visibilitychange"' in html
    assert "APPLY_REFRESH_THROTTLE_MS" in html
    # 有界轮询常量仍由模板接线（预算控制在 activate-waiting.js）
    assert "APPLY_POLL_INTERVAL_MS" in html
    assert "APPLY_POLL_MAX_REQUESTS" in html
    # 隐藏页暂停（isHidden 注入）+ 离开页面停止（pagehide/beforeunload）
    assert "isHidden: function () { return document.hidden; }" in html
    assert 'window.addEventListener("pagehide", function () { waiting.stop(); })' in html
    assert 'window.addEventListener("beforeunload", function () { waiting.stop(); })' in html
    # seq 守卫：旧响应一律丢弃
    assert "loadSeq" in html
    assert "if (seq !== loadSeq) return;" in html


def test_activate_wires_waiting_controller():
    """2026-09-19 修复：模板接线存在——activate.html 引用带版本号的
    activate-waiting.js 并经 HPActivateWaiting.createWaitingController 装配；
    进入 pending 用 waiting.enterPending()（幂等，不重置预算），离开
    pending / 离开页面用 waiting.stop()；控制器内隐藏页不发请求。"""
    html = _activate_html()
    waiting_js = (REPO_ROOT / "static" / "activate-waiting.js").read_text(
        encoding="utf-8")
    # 引用新 JS 文件（带版本号 query，与 entry.html 的 ?v= 习惯一致）
    assert '<script src="/static/activate-waiting.js?v=' in html
    assert "HPActivateWaiting" in html and "createWaitingController" in html
    assert "intervalMs: APPLY_POLL_INTERVAL_MS" in html
    assert "maxRequests: APPLY_POLL_MAX_REQUESTS" in html
    # pending 区块切换经控制器：enterPending（幂等）+ 非 pending/卸载 stop
    assert "waiting.enterPending();" in html
    assert 'if (block !== "pending") waiting.stop();' in html
    assert "waiting.stop();" in html
    # 旧内联轮询控制已迁出（不再有 startPolling/pollRequestsLeft 内联实现）
    assert "startPolling" not in html
    assert "pollRequestsLeft" not in html
    # 控制器行为锚点：隐藏页不发请求（预算不动）+ 耗尽保持停止（drained）
    assert "if (isHidden()) return;" in waiting_js
    assert "drained" in waiting_js


def test_activate_401_shows_relogin_not_approval():
    """R7：401 显示「登录状态已更新或失效，请重新登录查看」+ 登录按钮；
    绝不把 401 解释为审批通过（auth 区块与 approved 区块分离）。"""
    html = _activate_html()
    assert 'id="apply-auth"' in html
    assert "登录状态已更新或失效，请重新登录查看" in html
    assert 'data-i18n="activate.state.auth.login"' in html
    # 401 分支只进入 auth 区块，绝不进入 approved/invite-activated
    assert 'resp.status === 401' in html
    auth_fix = html.split('resp.status === 401')[1][:400]
    assert 'showApplyBlock("auth")' in auth_fix
    assert 'showApplyBlock("approved")' not in auth_fix
    assert 'href="/login"' in html.split('id="apply-auth"')[1][:600]


def test_activate_load_failure_keeps_retry_state():
    """R7：503/网络错误保留明确重试状态（专用区块 + 重试按钮），不退回
    申请表单、不误标待审、不重复提交。"""
    html = _activate_html()
    assert 'id="apply-load-error"' in html
    assert "暂时无法获取申请状态" in html
    # 失败分支只进入 load-error，不回退表单、不标 pending
    fail_fix = html.split("if (!resp.ok) {")[1][:300]
    assert 'showApplyBlock("load-error")' in fail_fix
    assert 'showApplyBlock("form")' not in fail_fix
    # loadState 自己的 catch（第二处 seq 守卫之后）：网络异常同样进 load-error
    seq_anchor = "if (seq !== loadSeq) return;  // 旧响应：丢弃"
    assert html.count(seq_anchor) == 2
    catch_fix = html.split(seq_anchor)[2][:200]
    assert 'showApplyBlock("load-error")' in catch_fix
    assert 'showApplyBlock("form")' not in catch_fix


def test_activate_apply_post_carries_csrf_and_fields():
    html = _activate_html()
    assert 'method: "POST"' in html
    assert '"X-CSRF-Token"' in html
    assert "research_direction: direction.value" in html
    assert "share_research_data: applyForm.share_research_data.checked" in html
    # 缺方向时前端先校验拦截
    assert 'input[name="research_direction"]:checked' in html
    # 提交防重复点击
    assert "applyBtn.disabled = true" in html


def test_activate_keeps_invite_code_flow():
    html = _activate_html()
    # 原邀请码输入与激活接口保留
    assert 'id="invite_code"' in html and 'name="invite_code"' in html
    assert 'fetch("/api/account/activate"' in html
    assert "已有邀请码？直接激活" in html
    # 退出会话入口保留
    assert 'action="/logout"' in html


# =========================================================================== #
# 3. 注册弹窗（R2 2026-09-19）：注册流程说明
# =========================================================================== #
def test_register_copy_describes_new_flow():
    """R2：注册并入介绍主页弹窗（register.html 已删），弹窗文案即流程说明。"""
    assert not (REPO_ROOT / "templates" / "register.html").exists()
    text = (REPO_ROOT / "templates" / "_login_dialog.html").read_text(encoding="utf-8")
    # 首屏核心文案：验证邮箱并提交申请 → 管理员审核通过后即可使用
    assert "验证邮箱并提交申请，管理员审核通过后即可使用。" in text
    # 发送后统一文案
    assert "验证邮件已发送，请查收。" in text
    # 实现型说明一律删除（模板/邮件/i18n 同步，行为层守卫另有用例）
    assert "验证邮箱本身不授予" not in text
    assert "不授予任何工作区" not in text
    # 注册表单复用既有 POST /register API
    assert 'action="/register"' in text
    assert 'id="register-dialog-form"' in text
    # 渲染 email_verify 模式确认文案真的展示（经介绍主页渲染链）
    html = _landing_register_html(mode="email_verify")
    assert "验证邮箱并提交申请，管理员审核通过后即可使用。" in html
    assert 'id="register-dialog-form"' in html
    assert 'name="email"' in html


def _landing_register_html(mode):
    """经 _register_landing_page 的模板链渲染（entry.html + _login_dialog.html）。"""
    with app_mod.app.test_request_context("/"):
        return render_template(
            "entry.html",
            signed_in=False, csrf_token="c-1",
            login_open=False, login_error=None, login_error_code=None,
            login_next_url="/app", login_retry_after=0,
            login_password_changed=False,
            register_open=True, registration_mode=mode,
            register_error=None, register_error_code=None,
            register_done=False, register_retry_after=0)


# =========================================================================== #
# 4. i18n.js：verify.* / activate.* 新键 zh/en 双语成对
# =========================================================================== #
def test_i18n_verify_activate_keys_bilingual():
    i18n = (REPO_ROOT / "static" / "i18n.js").read_text(encoding="utf-8")
    zh_block = i18n[i18n.index("zh: {"):i18n.index("en: {")]
    en_block = i18n[i18n.index("en: {"):]
    for key in VERIFY_KEYS + ACTIVATE_KEYS:
        assert '"%s"' % key in zh_block, "i18n.js zh 缺键：%r" % key
        assert '"%s"' % key in en_block, "i18n.js en 缺键：%r" % key
    # 数据分享中英文案（产品指定原文）
    assert "我愿意向研究团队分享我的切片、分析结果及使用行为数据" in zh_block
    assert ("I agree to share my slide images, analysis results, and usage "
            "behavior data with the research team") in en_block
    assert "Optional. Does not affect application review." in en_block


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
