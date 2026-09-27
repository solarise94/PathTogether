# -*- coding: utf-8 -*-
"""R9 复核（2026-09-27）复现用例——入仓为回归。

审查记录：/tmp/slide-id-review-r9/REVIEW.md。两个反例同一根因：登记在
取得配额行锁前读了预约状态、取锁后不重读，重激活 UPDATE 无
expected-state CAS——①锁前读到 reserved、锁内已被回收→漏账；②两个
登记方都锁前读到 released→重复补账。

0072 生命周期改写（plan §D 注记——**被替代的状态机合同**）：R9 时代的
「重激活 + 补账」已随绑定模型拆除——任务持有的容量绑定（holder）后
**从不被 TTL 回收**，清理失败登记（record_cleanup_pending）只管理重试、
不动账本。故本文件的并发不变量改写为「责任从未释放」目标：
  ① 清理失败登记 × 同用户新准入（任意串行序/注入交错）：残留预约保持
     reserved，账本恒 150，不漏账；
  ② 两个登记方并发登记同一预约：账本恒定（登记零财务副作用），不重复
     记账，attempts 累计。
原「重激活补记 100」的旧断言已随其实现的拆除而失效，不保留。
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import _bootstrap  # noqa: E402,F401

import psycopg
import upload_task_store
import upload_guard
import user_store

def test_reclaim_after_snapshot_before_lock_is_accounted(pg_uri):
    """① 清理失败登记 × 新准入：绑定责任从未被回收（原反例的「漏账」
    目标在新模型下的表达——账本恒 150，登记零财务副作用）。"""
    uid = user_store.create_user('r9-reclaim@example.com','pass1234pass1234',role='user')['user_id']
    rid = upload_guard.reserve_upload(
        uid, 100, holder_kind='upload_task', holder_id='upt-r9-reclaim',
        purpose='upload')['reservation_id']
    with psycopg.connect(pg_uri, autocommit=True) as conn:
        conn.execute("UPDATE upload_reservations SET expires_at=now()-interval '1 second' WHERE reservation_id=%s", (rid,))
    # 两种串行序（登记先/准入先）都成立——绑定预约不参加回收
    upload_guard.reserve_upload(uid,50)
    upload_task_store.record_cleanup_pending('upt-r9-reclaim',rid,error='IO')
    assert upload_task_store.get_cleanup_pending('upt-r9-reclaim') is not None
    assert upload_guard.get_quota_row(uid)['reserved_bytes'] == 150
    assert upload_guard.get_reservation(rid)['state'] == 'reserved'

def test_registration_first_then_admission_keeps_duty(pg_uri):
    """① 的另一串行胜者：登记先、准入后——同一不变量（原 R8 亦覆盖）。"""
    uid = user_store.create_user('r9-reg1@example.com','pass1234pass1234',role='user')['user_id']
    rid = upload_guard.reserve_upload(
        uid, 100, holder_kind='upload_task', holder_id='upt-r9-reg1',
        purpose='upload')['reservation_id']
    with psycopg.connect(pg_uri, autocommit=True) as conn:
        conn.execute("UPDATE upload_reservations SET expires_at=now()-interval '1 second' WHERE reservation_id=%s", (rid,))
    upload_task_store.record_cleanup_pending('upt-r9-reg1',rid,error='IO')
    upload_guard.reserve_upload(uid,50)
    assert upload_guard.get_quota_row(uid)['reserved_bytes'] == 150
    assert upload_guard.get_reservation(rid)['state'] == 'reserved' 

def test_two_pending_registrations_do_not_double_charge():
    """② 两个登记方并发登记同一（绑定）预约：登记零财务副作用——账本
    恒 100，不重复记账；attempts 累计。原「released 后重激活补记」路径
    已拆除：绑定预约经持有者清理确认收口释放，登记方不复活不补账。"""
    uid = user_store.create_user('r9-double@example.com','pass1234pass1234',role='user')['user_id']
    rid = upload_guard.reserve_upload(
        uid, 100, holder_kind='upload_task', holder_id='upt-r9-double',
        purpose='upload')['reservation_id']
    upload_task_store.record_cleanup_pending('upt-r9-double',rid,error='IO worker B')
    upload_task_store.record_cleanup_pending('upt-r9-double',rid,error='IO worker A')
    assert upload_task_store.get_cleanup_pending('upt-r9-double') is not None
    assert upload_task_store.get_cleanup_pending('upt-r9-double')['attempts'] == 2
    assert upload_guard.get_quota_row(uid)['reserved_bytes'] == 100
    assert upload_guard.get_reservation(rid)['state'] == 'reserved' 
