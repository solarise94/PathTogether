# -*- coding: utf-8 -*-
"""R16 第 6 项反例（/tmp/slide-id-review-r16/test_homepage_r16.py 原样入仓）：
closed 注册模式下「如何开始」不得承诺可申请注册。"""
import test_phase1_auth_ui as h
from test_phase1_auth_ui import _isolate

def test_closed_mode_does_not_instruct_registration(monkeypatch):
    h.app_mod.AUTH_ENABLED = True
    h._setup_owner_and_user()
    monkeypatch.setattr(h.app_mod, '_registration_dialog_mode', lambda: 'closed')
    response = h._client().get('/')
    assert response.status_code == 200
    body = response.get_data(as_text=True)
    guide = body.split('id="get-started"', 1)[1].split('</section>', 1)[0]
    assert '验证邮箱并提交申请，管理员审核通过后即可使用。' not in guide
