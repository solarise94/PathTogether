# -*- coding: utf-8 -*-
"""review 第五轮（8b565ad）回归测试（P4-b 断言换新，2026-09-26）。

原复现：失败上传提升到正式路径的内容经另一用户陈旧元数据暴露 → 元数据
归属强制跟随实际文件（force_slide_owner_follow_file）。P4-b 拆除后该场景
结构性消失——**本任务内容只进自己的 objects/<slide_id>/，他人同名元数据
是另一个独立资产**。场景（上传内容不得暴露给同名元数据的旧主人）保留
改写为：发布前（staging/已发布未结算）对所有人不可读；结算后只有资产
owner 可读——旧主人经其同名行读到的永远是它自己的资产，与本任务零关联。
"""
import pytest

import test_cos_ingest_worker as h
from test_cos_ingest_worker import _env  # noqa: F401  # fixture 注册
from test_cos_ingest_worker import _env  # noqa: F401  # fixture 注册
import cos_ingest_worker as w
import ingestion_store as s
import share_store
import slide_store


@pytest.fixture(autouse=True)
def _worker_env(_env):
    """复用 worker 套件的池/分块/水位/UPLOAD_DIR 环境。"""
    yield _env


def test_upload_never_visible_under_other_users_metadata(monkeypatch):
    fake, st = h.FakeCos(), {}
    job, payload = h._drive_to_validating(
        fake, st, owner='uploader', role='user')
    # 上传开始时名称空闲；另一用户随后占用了同名行（其文件不存在）
    share_store.set_slide_meta('a.svs', owner_user_id='other-user',
                               requester_role='owner')
    monkeypatch.setattr(h.slide_io, 'open_slide', h._ok_open_slide)

    # 发布前（staging）：任何身份（含 owner/旧名主人/admin）都不可读
    sid = job['slide_id']
    for actor, role in (('uploader', None), ('other-user', None),
                        (None, slide_store.ROLE_ADMIN)):
        assert not slide_store.authorize_read(sid, actor_user_id=actor,
                                              actor_role=role)

    assert w.process_validating(cos=fake, state=st) == job['job_id']
    out = s.get_job(job['job_id'])
    assert out['state'] == s.READY
    # 结算后：仅资产 owner（与 admin）可读；同名行主人读不到本任务内容
    assert slide_store.authorize_read(sid, actor_user_id='uploader')
    assert slide_store.authorize_read(sid, actor_role=slide_store.ROLE_ADMIN)
    assert not slide_store.authorize_read(sid, actor_user_id='other-user')
    # 同名行是另一个独立资产：归属不修正、内容互不串
    def rows(cur):
        cur.execute("SELECT slide_id, owner_user_id, asset_state FROM slides "
                    "WHERE legacy_filename='a.svs'")
        return cur.fetchone()

    victim = h._sql(rows)
    assert victim['owner_user_id'] == 'other-user'
    assert victim['slide_id'] != sid
    with open(h._bundle_data(sid), 'rb') as fh:
        assert fh.read() == payload
