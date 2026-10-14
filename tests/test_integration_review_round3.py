# -*- coding: utf-8 -*-
"""round-3 复核 G1 的方向对称回归（方形取景快照形态，2026-10-14）。

G1（round-3 review）：竖向 ROI 的预准入把宽度当长边，导致同一物理视场
转 90° 后「先读图、后被拒」——横向在读前拒绝、竖向白白消耗一次 region
读取才被读后硬闸拒。原交付补丁以 x/y/w/h 输入复现（3982×30 vs 30×3982）。

2026-10-14 合同变更（用户决定）：count_cells 取消模型矩形输入，位置由
系统从当前快照自动取**不越界方形**——长条 ROI 输入不复存在，G1 的缺口
由本回归以快照形态闭环：互为转置的快照（3982×30 与 30×3982，原生
0.12 µm/px、d=7、resolution 2048）必须产生
  - 相同的方形边长（= 快照短边 30）与渲染边（ceil(30×0.12×30/7)=16），
  - 相同的行为（两者都在读图前完成全部准入、都成功计数；不存在
    「一个读前拒绝、另一个先读后拒」的方向差），
  - 对称的 bbox（中心定位互为转置）。

安全口径与原补丁一致：不调用 GPU/真实平台——平台回调与推理全部 stub。
"""
import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
BACKEND = ROOT / 'plugins/pathsegmentor-measure/backend/app.py'


def _load_backend(name):
    spec = importlib.util.spec_from_file_location(name, BACKEND)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _run(monkeypatch, mod, width, height):
    """快照 (0,0,w,h) @0.12、d=7、resolution 2048 的完整能力调用（stub）。"""
    monkeypatch.setattr(mod, '_capability_ctx', lambda: (None, {
        'slide': 'x.svs', 'slide_id': 'sld_x', 'delegated': 'test-only',
        'body': {
            'slide': 'x.svs', 'slide_id': 'sld_x',
            'arguments': {'resolution': 2048, 'diameter_um': 7},
            'context': {'snapshot': {
                'snapshot_id': 'snap-1',
                'bbox': {'x': 0, 'y': 0, 'w': width, 'h': height},
                'slide_revision': 'sha256:abcd1234',
                'render_context_fingerprint': None,
            }},
        },
    }))
    monkeypatch.setattr(mod, '_installation_jwt', lambda: 'test-only')
    monkeypatch.setattr(mod, '_platform_slide_info', lambda *a, **k: {
        'width': 20000, 'height': 20000, 'mpp': 0.12,
        'asset_revision': 'sha256:abcd1234'})
    reads = []
    inference = []

    def read(slide, args, edge, delegated, jwt, slide_id=None):
        reads.append((args['x'], args['y'], args['w'], args['h'], edge))
        out = max(1, min(int(edge), 4096))
        return b'image', {
            'src': {'x': args['x'], 'y': args['y'],
                    'w': args['w'], 'h': args['h']},
            'width': out, 'height': out,
        }
    monkeypatch.setattr(mod, '_platform_read_region', read)

    def infer(*a, **k):
        inference.append('model')
        return {'instance_count': 0}
    monkeypatch.setattr(mod, '_infer_cellpose', infer)
    with mod.app.test_client() as client:
        response = client.post('/capabilities/count_cells', json={})
    return response, reads, inference


@pytest.mark.parametrize('width,height', [(3982, 30), (30, 3982)])
def test_orientation_symmetric_square_plan_and_single_read(monkeypatch,
                                                           width, height):
    mod = _load_backend('portrait_review_backend_%dx%d' % (width, height))
    response, reads, inference = _run(monkeypatch, mod, width, height)
    # 两个方向行为一致：都在读图前完成准入并成功计数（一次 region 读取、
    # 一次推理；不存在「读后硬闸才发现超限」的方向差——G1 的核心物理）
    assert response.status_code == 200, response.get_json()
    assert len(reads) == 1
    assert inference == ['model']
    bx, by, bw, bh, edge = reads[0]
    # 不越界方形：短边封顶、中心定位
    assert bw == bh == 30, 'square side = snapshot short side in both orientations'
    assert 0 <= bx and bx + bw <= 20000 and 0 <= by and by + bh <= 20000
    assert edge == 16, 'ceil(30x0.12x30/7)'
    if width > height:
        assert (bx, by) == ((3982 - 30) // 2, 0)
    else:
        assert (bx, by) == (0, (3982 - 30) // 2)
    # 处理边长在档内（预读镜像与读后硬闸同一物理）
    proc = mod._cellpose_processing_dims(
        edge, edge, mod._axis_mean_mpp(0.12, bw, bh, edge, edge), 7.0)
    assert max(proc) <= 2048
    result = response.get_json()['result']
    assert result['counted_region']['bbox']['w'] == 30
    assert result['counted_region']['bbox']['h'] == 30


def test_transposed_snapshots_produce_identical_plan(monkeypatch):
    """互为转置的快照给出逐字段一致的规划（除 bbox 转置对称外）。"""
    plans = {}
    for width, height in ((3982, 30), (30, 3982)):
        mod = _load_backend('portrait_review_plan_%dx%d' % (width, height))
        response, reads, _ = _run(monkeypatch, mod, width, height)
        assert response.status_code == 200
        bx, by, bw, bh, edge = reads[0]
        plans[(width, height)] = (bw, bh, edge)
    assert plans[(3982, 30)] == plans[(30, 3982)], \
        'rotating the same field of view must not change the square plan'
