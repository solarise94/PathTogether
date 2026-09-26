# -*- coding: utf-8 -*-
"""review 第三轮（af4ac46）回归测试（P4-b 断言换新，2026-09-26）。

原复现一：平台 owner 回落被无条件视为任务归属（实名任务要求精确归属，
平台 owner 等价仅限匿名任务）——归属判定改在创建时一次性解析（asset_owner_
for_job：实名=job owner，匿名=配置 owner 回落），发布只做一致性复核。
场景保留改写：**实名任务发布自己的 ID，绝不落到平台 owner 名下；匿名任务
按配置 owner 归属**。

原复现二：归属拒绝时按路径删除 dest 可能误删并发替换后的他人文件——
按路径删除已不可能（目标恒为 objects/<slide_id>/，无按名 dest）。场景
（并发替换他人文件）保留改写：**legacy 名下文件被并发替换对本任务发布
零影响，替换后的他人文件原样保留**。
"""
import os

import pytest

import test_cos_ingest_worker as h
from test_cos_ingest_worker import _env  # noqa: F401  # fixture 注册
from test_cos_ingest_worker import _env  # noqa: F401  # fixture 注册
import cos_ingest_worker as w
import ingestion_store as s
import share_store
import share_store_pg
import slide_store


@pytest.fixture(autouse=True)
def _worker_env(_env):
    """复用 worker 套件的池/分块/水位/UPLOAD_DIR 环境。"""
    yield _env


def test_platform_owner_equivalence_only_for_anonymous(monkeypatch):
    """平台 owner 回落等价仅限匿名任务（场景保留，断言换新——P4-b）。

    旧断言：实名任务撞平台 owner 同名文件 → FAILED。
    新断言：实名任务发布自己的 slide_id（owner=实名用户，不落平台 owner）；
    匿名任务（owner=""）的资产按配置 owner 归属。"""
    monkeypatch.setattr(share_store_pg, '_OWNER_USER_ID', 'platform-owner')
    fake, st = h.FakeCos(), {}
    # 实名任务：平台 owner 名下已有同名行/文件（陈旧名称预约）
    victim_path = os.path.join(h._env_dir(), 'a.svs')
    with open(victim_path, 'wb') as fh:
        fh.write(b'platform-owned')
    share_store.set_slide_meta('a.svs', owner_user_id='platform-owner',
                               requester_role='owner')
    monkeypatch.setattr(h.slide_io, 'open_slide', h._ok_open_slide)
    try:
        job, payload = h._drive_to_validating(
            fake, st, owner='ordinary-user', role='user')
        assert w.process_validating(cos=fake, state=st) == job['job_id']
        out = s.get_job(job['job_id'])
        assert out['state'] == s.READY  # 不因平台 owner 同名而失败
        desc = slide_store.resolve_slide_id(out['slide_id'])
        assert desc.owner_user_id == 'ordinary-user'  # 精确归属，非平台 owner
        with open(victim_path, 'rb') as fh:  # 平台 owner 的文件不动
            assert fh.read() == b'platform-owned'
    finally:
        try:
            os.unlink(victim_path)
        except OSError:
            pass

    # 匿名任务：配置 owner 回落（匿名≠无主资产）
    fake2, st2 = h.FakeCos(), {}
    anon = h._mkjob(owner='', role='', size=150, name='b.svs')
    h._admit(anon['job_id'])
    desc2 = slide_store.resolve_slide_id(anon['slide_id'])
    assert desc2 is not None and desc2.owner_user_id == 'platform-owner'


def test_concurrent_replacement_of_foreign_file_irrelevant(monkeypatch):
    """legacy 名下文件被并发替换不影响本任务（场景保留，断言换新——P4-b）。

    旧断言：归属拒绝分支不删被并发替换的 dest（inode 守卫）。按路径 dest
    已不存在；新断言：替换发生在本任务校验/发布全程，他人文件原样保留、
    本任务包完整落地（目标 objects/<slide_id>/ 与按名路径零关联）。"""
    fake, st = h.FakeCos(), {}
    job, payload = h._drive_to_validating(
        fake, st, owner='ordinary-user', role='user')
    victim_path = os.path.join(h._env_dir(), 'a.svs')
    with open(victim_path, 'wb') as fh:
        fh.write(b'V' * len(payload))
    share_store.set_slide_meta('a.svs', owner_user_id='victim',
                               requester_role='owner')
    monkeypatch.setattr(h.slide_io, 'open_slide', h._ok_open_slide)
    # 屏障：intent 持久化后、FS 发布前，另一 actor 并发替换同名文件
    real_persist = s.worker_persist_commit_intent

    def replace_then_persist(job_id, generation, intent):
        out = real_persist(job_id, generation, intent)
        with open(victim_path, 'wb') as fh:
            fh.write(b'REPLACED')
        return out

    monkeypatch.setattr(s, 'worker_persist_commit_intent',
                        replace_then_persist)
    try:
        assert w.process_validating(cos=fake, state=st) == job['job_id']
        out = s.get_job(job['job_id'])
        assert out['state'] == s.READY  # 本任务发布不受任何影响
        with open(victim_path, 'rb') as fh:
            assert fh.read() == b'REPLACED'  # 替换后的他人文件原样保留
        entry = h._bundle_data(out['slide_id'])
        with open(entry, 'rb') as fh:
            assert fh.read() == payload  # 本任务包完整
    finally:
        try:
            os.unlink(victim_path)
        except OSError:
            pass
