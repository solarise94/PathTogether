# -*- coding: utf-8 -*-
"""review 第四轮（a2e17c3）问题回归测试（复现用例入仓，2026-09-25）。

覆盖：inode 守卫的 stat 与 unlink 两步之间被并发替换时仍会误删新文件
→ 归属拒绝分支彻底不按路径删除 dest（无法保证同名互斥时避免路径删除，
残局交人工，日志含 still_ours 诊断）。复现脚本由审查方提供
（/tmp/cos-review-740e823/），此处原样入仓仅加本头注释。
"""
import test_cos_ingest_worker as h
from test_cos_ingest_worker import _env
import cos_ingest_worker as w
import ingestion_store as s
import share_store
import slide_io


def test_replacement_after_identity_check_is_not_deleted(monkeypatch, tmp_path):
    fake, st = h.FakeCos(), {}
    job, payload = h._drive_to_validating(fake, st, owner='ordinary-user', role='user')
    share_store.set_slide_meta('a.svs', owner_user_id='victim', requester_role='owner')
    monkeypatch.setattr(slide_io, 'open_slide', h._ok_open_slide)
    dest = tmp_path / 'a.svs'
    replacement = b'V' * len(payload)
    original_stat_ident = w._stat_ident
    seen = [0]
    def swap_after_check(path):
        ident = original_stat_ident(path)
        seen[0] += 1
        if seen[0] == 2:
            dest.unlink()
            dest.write_bytes(replacement)
        return ident
    monkeypatch.setattr(w, '_stat_ident', swap_after_check)
    w.process_validating(cos=fake, state=st)
    assert seen[0] == 2
    assert dest.read_bytes() == replacement
    assert s.get_job(job['job_id'])['state'] == s.FAILED
