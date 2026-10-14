import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

def test_processing_resolution_cap_checks_internal_scaled_long_edge(monkeypatch):
    path = ROOT / 'plugins/pathsegmentor-measure/backend/app.py'
    spec = importlib.util.spec_from_file_location('review_round2_backend', path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    args = {'x': 0, 'y': 0, 'w': 500, 'h': 500, 'resolution': 512, 'diameter_um': 7}
    monkeypatch.setattr(mod, '_capability_ctx', lambda: (None, {
        'slide': 'test.svs', 'slide_id': 'sld_test', 'delegated': 'test-only',
        'body': {'arguments': args},
    }))
    monkeypatch.setattr(mod, '_installation_jwt', lambda: 'test-only')
    monkeypatch.setattr(mod, '_platform_slide_info', lambda *a, **k: {'mpp': 0.5})
    reads = []
    def read(*args, **kwargs):
        reads.append('region')
        return b'image', {'width': 500, 'height': 500,
                          'src': {'x': 0, 'y': 0, 'w': 500, 'h': 500}}
    monkeypatch.setattr(mod, '_platform_read_region', read)
    monkeypatch.setattr(mod, '_infer_cellpose', lambda *a, **k: {'instance_count': 0})
    plan = mod._count_render_plan(500, 500, 0.5, 7, 512)
    assert plan['edge_long'] == 500
    assert plan['scaled_long'] == 1072
    with mod.app.test_client() as client:
        response = client.post('/capabilities/count_cells', json={})
    assert reads == [], '512 processing-cap option must refuse 1072 px model processing before region read'
    assert response.status_code == 400
    assert response.get_json()['error']['code'] == 'resource_limit'
