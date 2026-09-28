# -*- coding: utf-8 -*-
"""独立审查（2026-09-26 R6）复现用例——原样入仓为回归（断言不动）。

审查记录：/tmp/slide-id-review-20260926/REVIEW.md（审查方环境）；修复
对应的交付日志节：P6 后 R6 审查修复。六个反例覆盖：
  1. 冻结审计未绑定 MRXS 伴侣逐文件内容（等长改一字节须被拒）；
  2. 迁移终验漏检分享成员删除（授权集合比对）；
  3. 迁移终验漏检 owner 转移；
  4. 未核准配额差额不得放行 go；
  5. 清理失败不得释放容量预约；
  6. 新 ID 资产的 AI 会话按名鉴权拒绝合法属主（slide_id 优先）。
"""
import os
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
for _p in (str(_REPO / "scripts"), str(_REPO / "tests"), str(_REPO)):
    if _p not in sys.path:
        sys.path.insert(0, _p)


import json
from pathlib import Path
import pytest
from test_slide_migration_tools import world, conn, full_apply
from test_slide_migration_tools import migrator, verifier, drill
from test_slide_publish_pg import _isolate, _client, _user_session, TIFF
import app as app_mod
from _pt_helpers import publish_test_slide

def verify(w, suffix):
    return verifier.run_verify(upload_dir=str(w['up']), out_dir=str(w['up'].parent / suffix),
        plan_path=str(w['plan']), journal_path=str(w['journal']), database_url=w['uri'])

def test_frozen_companion_same_size_mutation_rejected(world):
    item = next(json.loads(line) for line in world['plan'].read_text().splitlines()
                if json.loads(line).get('slide_id') == 'sld_drill_mrxs01')
    directory = world['up'] / item['source']['companion_dir']
    member = next(p for p in directory.rglob('*') if p.is_file() and p.stat().st_size)
    data = member.read_bytes()
    member.write_bytes(bytes([data[0] ^ 1]) + data[1:])
    with pytest.raises(migrator.ItemFailure):
        migrator.validate_source_frozen(world['up'], item)

def test_lost_share_membership_blocks_go(world):
    full_apply(world)
    assert verify(world, 'baseline')['go_no_go'] == 'go'
    with world['conn'].cursor() as cur:
        cur.execute("DELETE FROM share_slides WHERE slide_id='sld_drill_svs01'")
        assert cur.rowcount > 0
    world['conn'].commit()
    result = verify(world, 'lost-share')
    assert result['go_no_go'] != 'go', result['authorization_diff']

def test_owner_transfer_blocks_go(world):
    full_apply(world)
    with world['conn'].cursor() as cur:
        cur.execute("UPDATE slides SET owner_user_id=%s WHERE slide_id='sld_drill_svs01'", (drill.BOB,))
    world['conn'].commit()
    result = verify(world, 'wrong-owner')
    assert result['go_no_go'] != 'go', result['authorization_diff']

def test_zero_used_bytes_blocks_go(world):
    full_apply(world)
    with world['conn'].cursor() as cur:
        cur.execute('UPDATE upload_user_quotas SET used_bytes=0')
    world['conn'].commit()
    result = verify(world, 'no-accounting')
    assert result['go_no_go'] != 'go', result['quota']

# U5（检查点 B）：旧上传端点删除，本场景已由 COS 统一链路覆盖（tests/test_cos_ingestion_kinds.py / test_ingestion_api.py / test_cos_ingest_worker.py）。
# （原 test_cleanup_failure_preserves_capacity：经旧 DELETE 取消端点注入
#   清理失败后断言预约保留——取消侧清理编排随端点删除；存活通道的
#   「清理失败不释放容量」由 test_capacity_lifecycle_cos.py::
#   test_cancel_keeps_reservation_until_cleanup_confirmed 覆盖。）

def test_new_id_session_owner_can_access(monkeypatch):
    c = _client()
    uid = _user_session(c, login='session-review@example.com')
    # U5（检查点 B）：旧上传端点删除——资产改由服务级发布夹具生成
    sid = publish_test_slide('brand-new.tif', TIFF, owner_user_id=uid,
                             upload_dir=app_mod.UPLOAD_DIR)
    assert c.get('/api/slides/' + sid + '/info').status_code == 200
    monkeypatch.setattr(app_mod, '_ai_session_record', lambda session_id: {
        'id': session_id, 'owner': uid, 'slide': 'brand-new.tif',
        'slide_id': sid, 'status': 'paused'})
    monkeypatch.setattr(app_mod, 'current_identity', lambda: {'role':'user', 'user_id':uid})
    with app_mod.app.test_request_context():
        result = app_mod._require_ai_session_owner('review-session')
    assert result is None, result
