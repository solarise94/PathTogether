import base64
import importlib.util
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

def load_module(path, name):
    sys.path.insert(0, str(path.parent))
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod

@pytest.mark.parametrize('service,env', [('cellpose', 'CELLPOSE_EAGER_LOAD'), ('pathsegmentor', 'PATHSEG_EAGER_LOAD')])
def test_oversized_image_rejected_before_pixel_decode(monkeypatch, service, env):
    monkeypatch.setenv(env, '0')
    mod = load_module(ROOT / 'services' / service / 'app.py', 'review_' + service)
    calls = []
    class HeaderOnlyImage:
        size = (4096, 4096)
        def load(self):
            calls.append('decoded_pixels')
    monkeypatch.setattr(mod.Image, 'open', lambda _: HeaderOnlyImage())
    err, _ = mod._decode_image(base64.b64encode(b'header').decode())
    assert err[0] == 'image_too_large'
    assert calls == [], 'oversized input must never reach pixel decoding'

# ---------------------------------------------------------------------------
# 方形取景合同 v1（2026-10-14 用户决定：模型不再提供矩形；位置只来自平台
# 验签转发的 context.snapshot，后端在快照内自动取不越界方形）。以下夹具与
# 用例自 x/y/w/h 输入形态迁移；物理断言逐条保留（见各 docstring）。
# ---------------------------------------------------------------------------
def backend(monkeypatch):
    """count_cells 后端夹具（快照形态）：快照 1900² @原生 0.25、显式
    diameter_um=15（处理尺度 0.5 µm/px = 原生的 2 倍粗 → 系统把 1900²
    level-0 方形按**恰好一半尺度**渲染成 950² 输入——保持原夹具的
    「rendered at half scale」物理）。"""
    mod = load_module(ROOT / 'plugins/pathsegmentor-measure/backend/app.py', 'review_backend')
    monkeypatch.setattr(mod, '_capability_ctx', lambda: (None, {
        'slide': 'x.svs', 'slide_id': 'sld_x', 'delegated': 'test-only',
        'body': {
            'slide': 'x.svs', 'slide_id': 'sld_x',
            'arguments': {'diameter_um': 15, 'resolution': 2048},
            'context': {'snapshot': {
                'snapshot_id': 'snap-1',
                'bbox': {'x': 0, 'y': 0, 'w': 1900, 'h': 1900},
                'slide_revision': 'sha256:abcd1234',
                'render_context_fingerprint': None,
            }},
        },
    }))
    monkeypatch.setattr(mod, '_installation_jwt', lambda: 'test-only')
    monkeypatch.setattr(mod, '_platform_slide_info', lambda *a, **k: {
        'width': 20000, 'height': 20000, 'mpp': 0.25,
        'asset_revision': 'sha256:abcd1234'})
    reads = []

    def read(slide, args, edge, delegated, jwt, slide_id=None):
        reads.append((args['x'], args['y'], args['w'], args['h'], edge))
        # 平台 `_aspect_fit_size` 镜像：界内方形 → 恰好 edge² 输出
        out = max(1, min(int(edge), 4096))
        return b'image', {
            'src': {'x': args['x'], 'y': args['y'], 'w': args['w'], 'h': args['h']},
            'width': out, 'height': out,
        }
    monkeypatch.setattr(mod, '_platform_read_region', read)
    mod._review_reads = reads
    return mod

def test_cellpose_receives_rendered_image_mpp(monkeypatch):
    mod = backend(monkeypatch)
    seen = []
    def infer(image, mpp, **kwargs):
        seen.append(mpp)
        return {'instance_count': 0}
    monkeypatch.setattr(mod, '_infer_cellpose', infer)
    with mod.app.test_client() as client:
        response = client.post('/capabilities/count_cells', json={})
    assert response.status_code == 200, response.get_json()
    assert seen == [0.5], '1900 L0 px @0.25 rendered to 950 px is 0.5 um/px'
    # 半尺度渲染来自规划器（d=15 → 处理尺度 0.5 = 2×原生）：读的就是
    # 1900² 方形、渲染边 950
    assert mod._review_reads == [(0, 0, 1900, 1900, 950)]

def test_original_2048_fixture_processing_cap_now_shrinks_square(monkeypatch):
    """原 2048² 请求在 2048 档下不再「读前拒绝」而是**缩边**（2026-10-14
    用户决定：系统自动在快照内取不越界方形，资源限制自动缩小边长并披露；
    设计 §自动生成不越界的方形第 3 步）。保留的物理断言：模型实际处理
    边长永不越档（≤2048），读取的就是缩边后的方形。读前拒绝语义由
    test_count_cells_extreme_coarse_calibration_rejected_before_read
    （无可行方形）与读后硬闸延续。"""
    mod = load_module(ROOT / 'plugins/pathsegmentor-measure/backend/app.py', 'review_backend_orig')
    monkeypatch.setattr(mod, '_capability_ctx', lambda: (None, {
        'slide': 'x.svs', 'slide_id': 'sld_x', 'delegated': 'test-only',
        'body': {
            'slide': 'x.svs', 'slide_id': 'sld_x',
            'arguments': {},
            'context': {'snapshot': {
                'snapshot_id': 'snap-1',
                'bbox': {'x': 0, 'y': 0, 'w': 2048, 'h': 2048},
                'slide_revision': 'sha256:abcd1234',
                'render_context_fingerprint': None,
            }},
        },
    }))
    monkeypatch.setattr(mod, '_installation_jwt', lambda: 'test-only')
    monkeypatch.setattr(mod, '_platform_slide_info', lambda *a, **k: {
        'width': 20000, 'height': 20000, 'mpp': 0.25,
        'asset_revision': 'sha256:abcd1234'})
    reads = []

    def read(slide, args, edge, delegated, jwt, slide_id=None):
        reads.append((args['x'], args['y'], args['w'], args['h'], edge))
        out = max(1, min(int(edge), 4096))
        return b'image', {
            'src': {'x': args['x'], 'y': args['y'], 'w': args['w'], 'h': args['h']},
            'width': out, 'height': out,
        }
    monkeypatch.setattr(mod, '_platform_read_region', read)
    infer_calls = []

    def infer(image, mpp, **kwargs):
        infer_calls.append(mpp)
        return {'instance_count': 0}
    monkeypatch.setattr(mod, '_infer_cellpose', infer)
    with mod.app.test_client() as client:
        response = client.post('/capabilities/count_cells', json={})
    assert response.status_code == 200, response.get_json()
    assert len(reads) == 1
    bx, by, bw, bh, edge = reads[0]
    assert bw == bh, 'the read region is a square'
    # 2048 档真实物理最大方形（int(s×30×0.25/7) ≤ 2048 → s = 1912）
    assert int(1912 * 30 * 0.25 / 7.0) == 2048
    assert (bw, bh, edge) == (1912, 1912, 1912)
    assert (bx, by) == ((2048 - 1912) // 2, (2048 - 1912) // 2)
    proc = mod._cellpose_processing_dims(
        edge, edge, mod._axis_mean_mpp(0.25, bw, bh, edge, edge), 7.0)
    assert max(proc) <= 2048, 'model processing edge stays within the option'
    assert infer_calls and len(infer_calls) == 1
    result = response.get_json()['result']
    assert result['counted_region']['reduced'] is True
    assert '不是整张切片' in result['counted_region']['note']

def test_missing_mpp_is_forwarded_as_null(monkeypatch):
    mod = backend(monkeypatch)
    seen = []
    class Response:
        status_code = 200
        def json(self):
            return {'instance_count': 0}
    def post(url, json, **kwargs):
        seen.append(json['mpp'])
        return Response()
    monkeypatch.setattr(mod.requests, 'post', post)
    mod._infer_cellpose(b'image', None)
    assert seen == [None], 'the real service accepts missing mpp as null, not 0'

def test_cellpose_rejects_explosive_rescaling(monkeypatch):
    monkeypatch.setenv('CELLPOSE_EAGER_LOAD', '0')
    mod = load_module(ROOT / 'services/cellpose/app.py', 'review_cellpose_budget')
    from PIL import Image
    import numpy as np
    monkeypatch.setattr(mod, '_decode_image', lambda _: (None, Image.new('RGB', (1024, 1024))))
    calls = []
    def count(img, **kwargs):
        calls.append(kwargs['diameter_px_'])
        return np.zeros((1024, 1024), dtype=np.int32)
    mod._MODEL.inject(count)
    with mod.app.test_client() as client:
        response = client.post('/v1/count', json={'image_b64': 'stub', 'mpp': 0.5, 'diameter_um': 1.0})
    assert calls == [], 'valid 1um at 0.5mpp causes 15x scaling: 15360x15360, must refuse before inference'
    assert response.status_code == 400
