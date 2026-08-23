# -*- coding: utf-8 -*-
"""批次 C 第 3 项：正式标注写入端切片右/下边界校验。

docs/ai-viewport-observation-annotation-fix-plan.md §2.5 / §4.3 / §6.1 / §10.3。
两条正式标注写入路径（Plugin Contract v1 与 legacy internal）共用
app._validate_annotation_rect 同一套几何规则：

  - 右边界越界（x + side_px > slide_width）→ 400；
  - 下边界越界（y + side_px > slide_height）→ 400；
  - 合法贴边标注（x + side_px == slide_width 且 y + side_px == slide_height）通过；
  - x/y 非有限（NaN/Infinity）/负值、side_px 越出 1..40000 → 400；
  - 失败请求不写数据库（ROI 数与 change seq 不变）、不产生 annotation.add
    成功审计事件；
  - 切片尺寸读不到（桩文件、无元数据 mock）时保持旧降级语义：不做包含校验。

json / pg 双后端通用（RUN_PG_TESTS=1 时 conftest 已切 postgres 并逐用例
TRUNCATE）。运行：cd 项目根 && python3 -m pytest tests/test_annotation_bounds.py -q
"""
import json
import os
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TMP = tempfile.mkdtemp(prefix="svs-bounds-")
os.environ["SHARE_DATA_DIR"] = os.path.join(TMP, "share-data")
os.makedirs(os.environ["SHARE_DATA_DIR"], exist_ok=True)
os.environ["UPLOAD_DIR"] = os.path.join(TMP, "uploads")
os.makedirs(os.environ["UPLOAD_DIR"], exist_ok=True)
os.environ["AI_SIDECAR_URL"] = "http://127.0.0.1:8055"
os.environ["AI_INTERNAL_TOKEN"] = "test-internal-token"

# openslide 未安装时 stub（本测试通过 mock borrow_pair/_read_metadata 提供
# 尺寸，不需要真 OpenSlide）
try:
    import openslide  # noqa: F401
except ImportError:
    import types as _types
    _os = _types.ModuleType("openslide")
    _os.OpenSlide = object
    sys.modules["openslide"] = _os
    _dz = _types.ModuleType("openslide.deepzoom")
    _dz.DeepZoomGenerator = object
    sys.modules["openslide.deepzoom"] = _dz

import app as app_mod  # noqa: E402
import share_store  # noqa: E402
from _pt_helpers import csrf_client  # noqa: E402

app_mod.UPLOAD_DIR = Path(os.environ["UPLOAD_DIR"])
app_mod.UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

import pytest  # noqa: E402


# mock 切片 level-0 尺寸（width > height，便于区分右/下边界用例）
SLIDE_W, SLIDE_H = 2000, 1000

_FAKE_ENTRY = {"pool": None, "sem": None}


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    """每用例独立存储 + 数据目录（json/pg 双后端通用）。"""
    monkeypatch.setenv("SHARE_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(app_mod, "AUTH_ENABLED", False)
    monkeypatch.setattr(app_mod, "AI_INTERNAL_TOKEN", "test-internal-token")
    monkeypatch.setattr(share_store, "SHARE_DATA_DIR", tmp_path)
    monkeypatch.setattr(share_store, "SHARE_FILE", tmp_path / "shares.json")
    # 元数据缓存按 (name, mtime) 命中；跨用例彻底清空，避免桩文件 mtime 碰撞
    with app_mod.slide_cache._info_cache_lock:
        app_mod.slide_cache._info_cache.clear()
    yield


def _client():
    app_mod.app.config["TESTING"] = True
    return csrf_client(app_mod.app.test_client())


def _touch_slide(name="bounds.svs"):
    path = app_mod.UPLOAD_DIR / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"svs-stub")
    return name


@contextmanager
def _borrow_pair_ctx(pair):
    yield pair


@contextmanager
def _slide_dims(width=SLIDE_W, height=SLIDE_H, mpp=0.5):
    """mock 切片读路径：level-0 dimensions + _read_metadata（返回形状与真实
    实现一致，含 width/height）。"""
    pair = {"osr": mock.Mock(dimensions=(width, height))}
    meta = {"width": width, "height": height, "mpp_x": mpp, "mpp_y": mpp,
            "objective": None, "mpp_source": "metadata"}
    with mock.patch.object(app_mod, "_get_slide", return_value=_FAKE_ENTRY), \
            mock.patch.object(app_mod.slide_cache, "borrow_pair",
                              side_effect=lambda _e: _borrow_pair_ctx(pair)), \
            mock.patch.object(app_mod, "_read_metadata", return_value=meta):
        yield


def _int_headers():
    return {"X-AI-Internal-Token": "test-internal-token"}


def _annotate_internal(client, slide, x, y, side_px):
    return client.post("/internal/ai/annotate", json={
        "slide": slide, "label": "AI 病灶", "x": x, "y": y, "side_px": side_px,
    }, headers=_int_headers())


def _plugin_token():
    """引导 histopilot 安装并换 scoped JWT（annotation:write）。"""
    inst = app_mod._bootstrap_plugin_installations()
    assert inst is not None, "引导应成功"
    app_mod._HISTOPILOT_INSTALLATION = inst
    r = _client().post("/api/plugin/v1/auth/token",
                       json={"installation_id": inst["installation_id"],
                             "secret": _file_secret()})
    assert r.status_code == 200, r.get_json()
    return r.get_json()["access_token"]


def _file_secret():
    raw = (Path(os.environ["SHARE_DATA_DIR"]) / "plugin-secret-histopilot.txt"
           ).read_text(encoding="utf-8").strip()
    try:
        obj = json.loads(raw)
        if isinstance(obj, dict):
            return str(obj.get("secret") or "")
    except (ValueError, TypeError):
        pass
    return raw


def _annotate_plugin(client, slide, x, y, side_px, token, grant_id):
    return client.post(
        "/api/plugin/v1/slides/%s/annotations" % slide,
        headers={"Authorization": "Bearer " + token, "X-Run-Grant": grant_id},
        json={"label": "AI 病灶", "x": x, "y": y, "side_px": side_px})


def _make_grant(slide):
    inst = app_mod._HISTOPILOT_INSTALLATION
    return share_store.create_run_grant(
        installation_id=inst["installation_id"], slide=slide,
        created_by_user_id=None)


def _db_state(slide):
    """副作用快照：ROI 数、该切片 change seq、annotation.add 审计数。"""
    rois = [r for r in share_store.list_rois(share_store.ADMIN_TOKEN)
            if r.get("slide") == slide]
    return (len(rois),
            share_store.current_change_seq(slide),
            len(share_store.list_audit(limit=500, action="annotation.add")))


# =========================================================================== #
# legacy internal 路径（/internal/ai/annotate）
# =========================================================================== #
def test_internal_right_edge_overflow_rejected():
    slide = _touch_slide("right.svs")
    client = _client()
    before = _db_state(slide)
    with _slide_dims():
        r = _annotate_internal(client, slide, x=SLIDE_W - 50, y=0, side_px=100)
    assert r.status_code == 400, r.get_json()
    err = r.get_json()["error"]
    # 错误信息指明哪条边越界 + 切片尺寸 + 不静默裁剪
    assert "右边界" in err and str(SLIDE_W) in err
    assert "不自动裁剪" in err
    assert _db_state(slide) == before, "失败请求不得写库/记成功审计"


def test_internal_bottom_edge_overflow_rejected():
    slide = _touch_slide("bottom.svs")
    client = _client()
    before = _db_state(slide)
    with _slide_dims():
        r = _annotate_internal(client, slide, x=0, y=SLIDE_H - 50, side_px=100)
    assert r.status_code == 400, r.get_json()
    err = r.get_json()["error"]
    assert "下边界" in err and str(SLIDE_H) in err
    assert _db_state(slide) == before


def test_internal_both_edges_overflow_names_both():
    slide = _touch_slide("both.svs")
    client = _client()
    with _slide_dims():
        r = _annotate_internal(client, slide,
                               x=SLIDE_W - 50, y=SLIDE_H - 50, side_px=100)
    assert r.status_code == 400
    err = r.get_json()["error"]
    assert "右边界" in err and "下边界" in err


def test_internal_flush_edge_annotation_passes():
    """合法贴边：x + side_px == slide_width 且 y + side_px == slide_height。"""
    slide = _touch_slide("flush.svs")
    client = _client()
    with _slide_dims():
        r = _annotate_internal(client, slide,
                               x=SLIDE_W - 100, y=SLIDE_H - 100, side_px=100)
    assert r.status_code == 200, r.get_json()
    roi = r.get_json()
    assert roi["source"] == "ai" and roi["slide"] == slide
    # 成功写入有 DB 变化 + annotation.add 审计（对照失败用例的零副作用断言）
    after = _db_state(slide)
    assert after[0] == 1 and after[2] == 1


def test_internal_nonfinite_and_range_rejected():
    slide = _touch_slide("range.svs")
    client = _client()
    before = _db_state(slide)
    with _slide_dims():
        # NaN / Infinity（字符串形式，float() 可解析）
        r = _annotate_internal(client, slide, x="NaN", y=0, side_px=10)
        assert r.status_code == 400 and "有限" in r.get_json()["error"]
        r = _annotate_internal(client, slide, x=0, y="Infinity", side_px=10)
        assert r.status_code == 400
        # 负坐标 / side_px 越出 1..40000
        r = _annotate_internal(client, slide, x=-1, y=0, side_px=10)
        assert r.status_code == 400 and "≥0" in r.get_json()["error"]
        r = _annotate_internal(client, slide, x=0, y=0, side_px=0)
        assert r.status_code == 400
        r = _annotate_internal(client, slide, x=0, y=0, side_px=40001)
        assert r.status_code == 400
    assert _db_state(slide) == before


def test_internal_dims_unavailable_degrades_without_containment():
    """切片尺寸读不到（桩文件、无 mock）→ 保持旧降级语义：不做包含校验。

    与 _rect_size_mm 读不到 mpp 返回 0 的降级口径一致（测试桩/损坏文件场景，
    生产真实切片元数据可读，恒走 §6.1 包含校验）。
    """
    slide = _touch_slide("nodims.svs")
    client = _client()
    r = _annotate_internal(client, slide, x=SLIDE_W - 50, y=0, side_px=100)
    assert r.status_code == 200, r.get_json()
    assert share_store.list_rois(share_store.ADMIN_TOKEN)


# =========================================================================== #
# Plugin Contract v1 路径（/api/plugin/v1/slides/<slide>/annotations）
# =========================================================================== #
def test_plugin_v1_right_and_bottom_overflow_rejected():
    slide = _touch_slide("pv1.svs")
    token = _plugin_token()
    grant = _make_grant(slide)
    client = _client()
    before = _db_state(slide)
    with _slide_dims():
        r = _annotate_plugin(client, slide, x=SLIDE_W - 50, y=0, side_px=100,
                             token=token, grant_id=grant["grant_id"])
    assert r.status_code == 400, r.get_json()
    err = (r.get_json() or {}).get("error") or {}
    assert err.get("code") == "invalid_request"
    assert "右边界" in err.get("message", "") and str(SLIDE_W) in err.get("message", "")
    # 统一信封附带结构化 details：越界边 + 提交矩形 + 切片 level-0 尺寸
    details = err.get("details") or {}
    assert details.get("edges") == ["right"]
    assert details.get("slide_level0", {}).get("width") == SLIDE_W
    assert details.get("submitted", {}).get("side_px") == 100
    assert _db_state(slide) == before, "失败请求不得写库/记成功审计"

    with _slide_dims():
        r = _annotate_plugin(client, slide, x=0, y=SLIDE_H - 50, side_px=100,
                             token=token, grant_id=grant["grant_id"])
    assert r.status_code == 400
    err = (r.get_json() or {}).get("error") or {}
    assert "下边界" in err.get("message", "")
    assert err.get("details", {}).get("edges") == ["bottom"]
    assert _db_state(slide) == before


def test_plugin_v1_flush_edge_annotation_passes():
    slide = _touch_slide("pv1flush.svs")
    token = _plugin_token()
    grant = _make_grant(slide)
    client = _client()
    with _slide_dims():
        r = _annotate_plugin(client, slide,
                             x=SLIDE_W - 100, y=SLIDE_H - 100, side_px=100,
                             token=token, grant_id=grant["grant_id"])
    assert r.status_code == 200, r.get_json()
    roi = r.get_json()
    assert roi["source"] == "ai" and roi["review_status"] == "pending"
    after = _db_state(slide)
    assert after[0] == 1 and after[2] == 1


def test_plugin_v1_dims_unavailable_degrades_without_containment():
    slide = _touch_slide("pv1nodims.svs")
    token = _plugin_token()
    grant = _make_grant(slide)
    client = _client()
    r = _annotate_plugin(client, slide, x=SLIDE_W - 50, y=0, side_px=100,
                         token=token, grant_id=grant["grant_id"])
    assert r.status_code == 200, r.get_json()


# =========================================================================== #
# 两条路径同一几何规则（同一组几何 → 同一通过/拒绝结论）
# =========================================================================== #
@pytest.mark.parametrize("x,y,side_px,expect_ok", [
    (0, 0, 100, True),                       # 内部
    (SLIDE_W - 100, SLIDE_H - 100, 100, True),  # 双边贴边
    (SLIDE_W - 99, 0, 100, False),           # 右边界超 1px
    (0, SLIDE_H - 99, 100, False),           # 下边界超 1px
    (SLIDE_W, SLIDE_H, 1, False),            # 原点即出界
    (SLIDE_W // 2, SLIDE_H // 2, 40001, False),  # side_px 超上限（未触包含校验）
])
def test_both_paths_share_geometry_rules(x, y, side_px, expect_ok):
    inst_slide = _touch_slide("shared_internal.svs")
    plug_slide = _touch_slide("shared_plugin.svs")
    token = _plugin_token()
    grant = _make_grant(plug_slide)
    client = _client()
    with _slide_dims():
        r_internal = _annotate_internal(client, inst_slide, x, y, side_px)
        r_plugin = _annotate_plugin(client, plug_slide, x, y, side_px,
                                    token=token, grant_id=grant["grant_id"])
    expected = 200 if expect_ok else 400
    assert r_internal.status_code == expected, r_internal.get_json()
    assert r_plugin.status_code == expected, r_plugin.get_json()
