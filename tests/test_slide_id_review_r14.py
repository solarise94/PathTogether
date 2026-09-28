import hashlib
import psycopg
import slide_storage
import slide_store
import slide_publish
import upload_guard as guard
import upload_task_store
from test_reconcile_upload_capacity import recon, _uid, _task


def cli(pg_uri, root, *args):
    return recon.main(['--database-url', pg_uri, '--upload-dir', str(root), *map(str,args)])


def test_terminal_untracked_residue_is_not_go(pg_uri, tmp_path):
    uid = _uid('r14_terminal')
    tid = 'upt_r14_terminal'
    _task(uid, tid, None, state='failed')
    p = slide_storage.staging_dir(tid, 'transfer', root=tmp_path) / 'data.svs'
    p.parent.mkdir(parents=True)
    p.write_bytes(b'x'*100)
    # Known task ID must not make this unaccounted tree disappear from audit.
    assert cli(pg_uri, tmp_path) == 3


def test_quota_aggregate_drift_is_not_go(pg_uri, tmp_path):
    uid = _uid('r14_quota')
    tid = 'upt_r14_quota'
    rid=guard.reserve_upload(uid,100,holder_kind='upload_task',holder_id=tid,purpose='upload')['reservation_id']
    _task(uid,tid,rid)
    with psycopg.connect(pg_uri,autocommit=True) as db:
        db.execute('UPDATE upload_user_quotas SET reserved_bytes=0 WHERE user_id=%s',(uid,))
    assert cli(pg_uri,tmp_path) == 3


def test_commit_intent_cannot_be_demoted_by_reconcile(pg_uri,tmp_path):
    uid=_uid('r14_commit')
    desc=slide_store.allocate_slide(uid,'a.svs','svs')
    sha=hashlib.sha256(b'x'*100).hexdigest()
    manifest=slide_publish.build_manifest('data.svs',100,sha)
    intent=slide_publish.build_intent(desc.slide_id,uid,manifest,sha,100)
    tid, token, task=upload_task_store.begin_legacy_commit(
        uid,'a.svs','a.svs',[{'name':'data.svs','size':100,'sha256':sha,'slide':True}],
        slide_id=desc.slide_id,intent=intent)
    p=slide_storage.staging_dir(tid,'1',root=tmp_path)/'data.svs'
    p.parent.mkdir(parents=True)
    p.write_bytes(b'x'*100)
    plan=tmp_path/'plan.json'
    rc=cli(pg_uri,tmp_path,'--plan-out',plan,'--repair-residuals')
    if rc==0:
        cli(pg_uri,tmp_path,'--apply','--plan',plan,'--repair-residuals')
    with psycopg.connect(pg_uri) as db:
        state, saved_intent=db.execute('SELECT state,commit_intent_json FROM upload_tasks WHERE upload_id=%s',(tid,)).fetchone()
        pending=db.execute('SELECT count(*) FROM upload_cleanup_pending WHERE upload_id=%s',(tid,)).fetchone()[0]
    assert state=='committing' and saved_intent and pending==0,(state,pending)
