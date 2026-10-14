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

def backend(monkeypatch):
    mod = load_module(ROOT / 'plugins/pathsegmentor-measure/backend/app.py', 'review_backend')
    monkeypatch.setattr(mod, '_capability_ctx', lambda: (None, {
        'slide': 'x.svs', 'slide_id': 'sld_x', 'delegated': 'test-only',
        'body': {'arguments': {'x': 0, 'y': 0, 'w': 1900, 'h': 1900}},
    }))
    monkeypatch.setattr(mod, '_installation_jwt', lambda: 'test-only')
    monkeypatch.setattr(mod, '_platform_slide_info', lambda *a, **k: {'mpp': 0.25})
    monkeypatch.setattr(mod, '_platform_read_region', lambda *a, **k: (b'image', {
        'src': {'x': 0, 'y': 0, 'w': 1900, 'h': 1900}, 'width': 950, 'height': 950,
    }))
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
    assert response.status_code == 200
    assert seen == [0.5], '2048 L0 px @0.25 rendered to 1024 px is 0.5 um/px'

def test_original_2048_fixture_now_refused_by_processing_cap(monkeypatch):
    mod = load_module(ROOT / 'plugins/pathsegmentor-measure/backend/app.py', 'review_backend_orig')
    monkeypatch.setattr(mod, '_capability_ctx', lambda: (None, {
        'slide': 'x.svs', 'slide_id': 'sld_x', 'delegated': 'test-only',
        'body': {'arguments': {'x': 0, 'y': 0, 'w': 2048, 'h': 2048}},
    }))
    monkeypatch.setattr(mod, '_installation_jwt', lambda: 'test-only')
    monkeypatch.setattr(mod, '_platform_slide_info', lambda *a, **k: {'mpp': 0.25})
    reads = []
    def read(*args, **kwargs):
        reads.append('region')
        return b'image', {'src': {'x': 0, 'y': 0, 'w': 2048, 'h': 2048},
                          'width': 1024, 'height': 1024}
    monkeypatch.setattr(mod, '_platform_read_region', read)
    monkeypatch.setattr(mod, '_infer_cellpose', lambda *a, **k: {'instance_count': 0})
    with mod.app.test_client() as client:
        response = client.post('/capabilities/count_cells', json={})
    assert reads == []
    assert response.status_code == 400
    assert response.get_json()['error']['code'] == 'resource_limit'

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
