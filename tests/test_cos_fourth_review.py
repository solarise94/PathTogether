# -*- coding: utf-8 -*-
"""review 第四轮（a2e17c3）回归测试（P4-b 断言换新，2026-09-26）。

原复现：inode 守卫（_stat_ident）的 stat 与 unlink 两步之间被并发替换时
仍会误删新文件 → 归属拒绝分支彻底不按路径删除 dest。P4-b 拆除后**本任务
根本没有按路径 dest**（目标恒为 objects/<slide_id>/，与按名文件零关联）
——场景（检查点与删除点之间被并发替换）保留改写为：**校验/发布全程
legacy 名下文件被替换，本任务不读、不删、不受影响**；_stat_ident 随
still_ours/adopted 族一并消失。
"""
import os

import pytest

import test_cos_ingest_worker as h
from test_cos_ingest_worker import _env  # noqa: F401  # fixture 注册
from test_cos_ingest_worker import _env  # noqa: F401  # fixture 注册
import cos_ingest_worker as w
import ingestion_store as s
import share_store


@pytest.fixture(autouse=True)
def _worker_env(_env):
    """复用 worker 套件的池/分块/水位/UPLOAD_DIR 环境。"""
    yield _env


def test_replacement_during_validating_is_never_touched(monkeypatch):
    fake, st = h.FakeCos(), {}
    job, payload = h._drive_to_validating(
        fake, st, owner='ordinary-user', role='user')
    dest = os.path.join(h._env_dir(), 'a.svs')
    with open(dest, 'wb') as fh:
        fh.write(b'V' * len(payload))
    share_store.set_slide_meta('a.svs', owner_user_id='victim',
                               requester_role='owner')
    monkeypatch.setattr(h.slide_io, 'open_slide', h._ok_open_slide)
    replacement = b'R' * len(payload)
    # 屏障：open_slide 校验刚过、intent 尚未持久化——victim 文件此刻被换
    real_open = h.slide_io.open_slide  # 已是 _ok_open_slide 替身
    seen = [0]

    def swap_after_check(path, format_hint=None):
        slide = real_open(path, format_hint=format_hint)
        seen[0] += 1
        with open(dest, 'wb') as fh:
            fh.write(replacement)  # 并发 actor 替换同名文件
        return slide

    monkeypatch.setattr(h.slide_io, 'open_slide', swap_after_check)
    try:
        assert w.process_validating(cos=fake, state=st) == job['job_id']
        assert seen[0] == 1  # 校验确实发生且替换注入成功
        out = s.get_job(job['job_id'])
        assert out['state'] == s.READY  # 本任务照常发布（与按名文件无关）
        with open(dest, 'rb') as fh:
            assert fh.read() == replacement  # 被替换的他人文件绝不被删/改
        with open(h._bundle_data(out['slide_id']), 'rb') as fh:
            assert fh.read() == payload  # 本任务包完整
        assert not hasattr(w, '_stat_ident')  # inode 守卫族已拆除
    finally:
        try:
            os.unlink(dest)
        except OSError:
            pass
