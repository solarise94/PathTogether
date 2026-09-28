import psycopg
import user_store
import upload_task_store
import slide_storage
import app as app_mod
from _pt_helpers import csrf_client
from test_reconcile_upload_capacity import recon, _uid, _task
from test_capacity_lifecycle_channels import _isolate


def _create(client, name, size):
    # U5（检查点 B）：原 HTTP V2 创建端点已删除——store 级等价夹具
    # （owner 任务：无预约，按身份合同属豁免；断言不变）。
    return upload_task_store.create_task(
        "", name, name, size, upload_task_store.UPLOAD_CHUNK_SIZE)["upload_id"]


def cli(pg_uri, root, *args):
    return recon.main(['--database-url', pg_uri, '--upload-dir', str(root), *map(str, args)])


def test_terminal_repair_creates_cleanup_work(pg_uri, tmp_path):
    uid = _uid('r15_terminal')
    tid = 'upt_r15_terminal'
    _task(uid, tid, None, state='failed')
    p = slide_storage.staging_dir(tid, 'transfer', root=tmp_path) / 'data.svs'
    p.parent.mkdir(parents=True)
    p.write_bytes(b'x'*100)
    plan = tmp_path/'plan.json'
    assert cli(pg_uri,tmp_path,'--plan-out',plan,'--repair-residuals') == 0
    assert cli(pg_uri,tmp_path,'--apply','--plan',plan,'--repair-residuals') == 0
    task = upload_task_store.get_task(tid)
    assert task['reservation_id']
    pending = upload_task_store.get_cleanup_pending(tid)
    assert pending and pending['reservation_id']==task['reservation_id'], (task['state'],pending)


def test_owner_upload_survives_reconcile(pg_uri, tmp_path):
    user = user_store.create_user('r15-owner@example.com','localownerpass12345',role='owner')
    app_mod.app.config['TESTING'] = True
    app_mod.AUTH_ENABLED = True
    client = csrf_client(app_mod.app.test_client())
    with client.session_transaction() as s:
        s['auth_user'] = True
        s['user_id'] = user['user_id']
        s['role'] = 'owner'
        s['auth_version'] = user.get('auth_version',1)
    tid = _create(client,'owner.svs',100)
    before = upload_task_store.get_task(tid)
    assert before['state']=='active' and before['reservation_id'] is None
    plan = tmp_path/'owner-plan.json'
    assert cli(pg_uri,app_mod.UPLOAD_DIR,'--plan-out',plan) == 0
    assert cli(pg_uri,app_mod.UPLOAD_DIR,'--apply','--plan',plan) == 0
    after = upload_task_store.get_task(tid)
    assert after['state']=='active' and upload_task_store.get_cleanup_pending(tid) is None, after['state']


# 入仓适配（断言与用例原样，见 docs/review-evidence/r15/）：审查方在独立
# 目录单独收集；本仓全量运行时共享会话 UPLOAD_DIR 可能有其他用例残留的
# 暂存目录，补 autouse 清理保证「unknown_staging_dir」不误报。
import pytest as _pytest


@_pytest.fixture(autouse=True)
def _clean_shared_upload_dir():
    import os as _os
    import shutil as _shutil
    for sub in (".staging", ".task-locks"):
        base = _os.path.join(str(app_mod.UPLOAD_DIR), sub)
        if _os.path.isdir(base):
            _shutil.rmtree(base, ignore_errors=True)
    yield
