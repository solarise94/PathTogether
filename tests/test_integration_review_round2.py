import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

def test_processing_resolution_cap_checks_internal_scaled_long_edge(monkeypatch):
    """round-2 回归（方形取景迁移，2026-10-14 合同变更：模型不再提供矩形）。

    原断言：512 档对 1072 px 模型处理长边在读 region **之前**拒绝
    （500² @0.5、d=7 → 处理边 1071 > 512 → 400 resource_limit、零读图）。

    迁移后语义（用户决定：系统在快照内自动取不越界方形，资源限制自动
    缩小边长并披露——设计 §自动生成不越界的方形第 3 步）：同一物理场景
    （快照 500² @0.5、d=7、512 档）不再拒绝，而是把方形缩到该档真实取整
    物理的最大可处理边长（239；int(239×30×0.5/7)=512 恰在档内、240→514
    越档）后读图。保留的物理断言：
      - 模型实际处理边长永不越档（读后镜像复核 ≤512）；
      - region 读取就是缩边后的方形（次数 1、边长相等、快照中心）；
      - 缩小范围经 counted_region.reduced + note 披露。
    「无可行方形 → 读图前拒绝」的物理由 extreme-coarse 校准用例
    （test_pathsegmentor_backend.py）与读后硬闸延续。"""
    path = ROOT / 'plugins/pathsegmentor-measure/backend/app.py'
    spec = importlib.util.spec_from_file_location('review_round2_backend', path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    monkeypatch.setattr(mod, '_capability_ctx', lambda: (None, {
        'slide': 'test.svs', 'slide_id': 'sld_test', 'delegated': 'test-only',
        'body': {
            'slide': 'test.svs', 'slide_id': 'sld_test',
            'arguments': {'resolution': 512, 'diameter_um': 7},
            'context': {'snapshot': {
                'snapshot_id': 'snap-1',
                'bbox': {'x': 0, 'y': 0, 'w': 500, 'h': 500},
                'slide_revision': 'sha256:abcd1234',
                'render_context_fingerprint': None,
            }},
        },
    }))
    monkeypatch.setattr(mod, '_installation_jwt', lambda: 'test-only')
    monkeypatch.setattr(mod, '_platform_slide_info', lambda *a, **k: {
        'width': 20000, 'height': 20000, 'mpp': 0.5,
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
    monkeypatch.setattr(mod, '_infer_cellpose', lambda *a, **k: {'instance_count': 0})
    # 镜像物理：500² 需 1071 px 处理边（越档）；239² 恰 512（档内最大）
    assert mod._square_processing_dims(500, 500, 0.5, 7.0) == (1071, 1071)
    assert int(239 * 30 * 0.5 / 7.0) == 512
    assert not mod._square_admits(500, 0.5, 7.0, 512)
    assert mod._square_admits(239, 0.5, 7.0, 512)
    with mod.app.test_client() as client:
        response = client.post('/capabilities/count_cells', json={})
    assert response.status_code == 200, response.get_json()
    assert len(reads) == 1, 'exactly one region read, for the shrunk square'
    bx, by, bw, bh, edge = reads[0]
    assert (bw, bh) == (239, 239), 'the region read is for the shrunk square'
    assert (bx, by) == ((500 - 239) // 2, (500 - 239) // 2)  # 快照中心
    proc = mod._cellpose_processing_dims(
        edge, edge, mod._axis_mean_mpp(0.5, bw, bh, edge, edge), 7.0)
    assert max(proc) <= 512, 'actual processing edge stays within the 512 option'
    result = response.get_json()['result']
    assert result['counted_region']['reduced'] is True
    assert '方形' in result['counted_region']['note']
