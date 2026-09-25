# -*- coding: utf-8 -*-
"""review 第五轮（8b565ad）问题回归测试（复现用例入仓，2026-09-25）。

覆盖：失败上传提升到正式路径的内容经另一用户陈旧元数据暴露（列表可见
+可读）→ 元数据归属强制跟随实际文件（撤销旧 view 授权 + CAS 转移给
上传者，仅当 dest 仍为本任务提升的那份时）。复现脚本由审查方提供
（/tmp/cos-review-740e823/），此处原样入仓仅加本头注释。
"""
import app as app_mod
import test_cos_ingest_worker as h
from test_cos_ingest_worker import _env
import cos_ingest_worker as w
import ingestion_store as s
import share_store
import slide_io


def test_failed_upload_not_visible_under_other_users_metadata(monkeypatch, tmp_path):
    fake, st = h.FakeCos(), {}
    job, payload = h._drive_to_validating(fake, st, owner='uploader', role='user')
    # Name was free when upload started; another user's metadata now owns it,
    # with no file present (same scenario as the third/fourth review tests).
    share_store.set_slide_meta('a.svs', owner_user_id='other-user', requester_role='owner')
    monkeypatch.setattr(slide_io, 'open_slide', h._ok_open_slide)
    w.process_validating(cos=fake, state=st)
    assert s.get_job(job['job_id'])['state'] == s.FAILED
    assert (tmp_path / 'a.svs').read_bytes() == payload
    monkeypatch.setattr(app_mod, 'UPLOAD_DIR', tmp_path)
    monkeypatch.setattr(app_mod, 'current_identity', lambda: {'role': 'user', 'user_id': 'other-user'})
    with app_mod.app.test_request_context('/'):
        observed = ('a.svs' in app_mod._visible_slide_names(), app_mod.can_view_slide('a.svs'))
        assert observed == (False, False)
