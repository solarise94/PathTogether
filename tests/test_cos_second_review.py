# -*- coding: utf-8 -*-
"""review 第二轮（8e1e2db）回归测试（P4-b 断言换新，2026-09-26）。

原复现：Complete 响应丢失后恢复路径首次 HEAD 瞬态超时被误判永久失败
（_RecoveryTransient 保持 completing 重试）——完成阶段与 P4-b 无关，原样保留；
同内容（同 sha）他人文件仍被提交恢复认领（set_slide_meta 返回 owner 终检）
——归属终检族已拆除（P4 合同 §5.1），场景意义保留改写为：**恢复只按任务
绑定的 slide_id 判定，盘上同名同内容文件不是归属证据**——他人文件/行
分毫不动，本任务发布进自己的 objects/<slide_id>/。
"""
import os

import pytest

import test_cos_ingest_worker as h
from test_cos_ingest_worker import _env  # noqa: F401  # fixture 注册
from test_cos_ingest_worker import _env  # noqa: F401  # fixture 注册
import cos_client
import cos_ingest_worker as w
import ingestion_store as s
import share_store
import slide_store


@pytest.fixture(autouse=True)
def _worker_env(_env):
    """复用 worker 套件的池/分块/水位/UPLOAD_DIR 环境。"""
    yield _env


def test_complete_response_lost_then_head_transient_recovers(monkeypatch):
    fake, st = h.FakeCos(), {}
    job, payload = h._prepare(fake, st)
    h._put_plan_parts(fake, job, payload)
    s.request_upload_complete(job['job_id'])
    original = fake.complete_multipart

    def completes_but_response_lost(*args):
        original(*args)
        raise cos_client.CosClientError('response lost')

    monkeypatch.setattr(fake, 'complete_multipart', completes_but_response_lost)
    w.process_completing(cos=fake, state=st)
    monkeypatch.setattr(fake, 'complete_multipart', original)

    def no_such_upload(*args):
        raise cos_client.CosClientError('HTTP 404 NoSuchUpload')

    monkeypatch.setattr(fake, 'list_parts', no_such_upload)
    saved_head = fake.head_object
    calls = [0]

    def temporary_head(*args):
        calls[0] += 1
        if calls[0] == 1:
            raise cos_client.CosClientError('HEAD timeout')
        return saved_head(*args)

    monkeypatch.setattr(fake, 'head_object', temporary_head)
    w.process_completing(cos=fake, state=st)
    assert s.get_job(job['job_id'])['state'] == s.COMPLETING
    w.process_completing(cos=fake, state=st)
    assert s.get_job(job['job_id'])['state'] == s.QUEUED


def test_same_content_other_owner_not_claimed(monkeypatch):
    """同内容（同 sha）他人同名文件不被认领（场景保留，断言换新——P4-b）。

    旧断言：归属终检失败 → job FAILED（name_unavailable）。
    新断言：本任务发布进自己的 slide_id（owner=上传者），victim 的同名
    文件/行原样保留——隔离不猜归属、无 owner 修正。"""
    fake, st = h.FakeCos(), {}
    job, payload = h._drive_to_validating(
        fake, st, owner='uploader', role='user')
    victim_path = os.path.join(h._env_dir(), 'a.svs')
    with open(victim_path, 'wb') as fh:  # 他人**同内容**文件（同 sha）
        fh.write(payload)
    share_store.set_slide_meta('a.svs', owner_user_id='victim',
                               requester_role='owner')
    monkeypatch.setattr(h.slide_io, 'open_slide', h._ok_open_slide)
    try:
        assert w.process_validating(cos=fake, state=st) == job['job_id']
        out = s.get_job(job['job_id'])
        assert out['state'] == s.READY  # 同名同内容不再构成冲突
        desc = slide_store.resolve_slide_id(out['slide_id'])
        assert desc is not None
        assert desc.owner_user_id == 'uploader'  # 归属=上传者，绝不认领 victim
        assert desc.legacy_filename is None  # 新资产无按名别名
        with open(victim_path, 'rb') as fh:  # victim 文件分毫未动
            assert fh.read() == payload

        def victim_owner(cur):
            cur.execute("SELECT owner_user_id FROM slides WHERE "
                        "legacy_filename='a.svs'")
            return cur.fetchone()['owner_user_id']

        assert h._sql(victim_owner) == 'victim'  # victim 行归属不修正
    finally:
        try:
            os.unlink(victim_path)
        except OSError:
            pass
