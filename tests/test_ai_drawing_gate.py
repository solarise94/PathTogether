# -*- coding: utf-8 -*-
"""P1-4 / P1-5（review-2026-09-07）：AI 描绘的平台写端复核与来源快照溯源。

覆盖（PT 侧，本文件专属）：
  P1-4 写端镜像闸门（ai_session_drawing_flags，0039）：
    - 镜像从未见过 session（无行）→ 伪造 polygon 403 ai_drawing_disabled
      （fail closed，不落库）；
    - 镜像 false（用户已关闭）→ 伪造 polygon 403，无新 ROI；
    - 开启（镜像 true）→ polygon 200 落库；
    - 运行中关闭（true → false）→ 迟到 polygon 403，无新 ROI；
    - freehand 同受闸门约束；矩形路径不受影响（无镜像行仍 200）；
    - 代理路由 /api/ai/session/<sid>/drawing：HP 200 且带权威布尔 → 镜像
      upsert；HP 200 畸形体 / HP 5xx → 镜像不变（不推定开关状态）。
  P1-5 来源快照溯源：
    - polygon 缺 snapshot_id → 400 invalid_request，不落库；
    - snapshot_bbox 畸形 → 400（不把垃圾持久化）；
    - 合法溯源字段（snapshot_id/snapshot_bbox/render_context_fingerprint/
      slide_revision）逐键持久化进 ROI provenance；session/用户归属仍以
      grant 为准（请求体不采信）。
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import _bootstrap  # noqa: E402,F401  # session 目录+openslide stub（conftest 先行）
UPLOAD_DIR = _bootstrap.UPLOAD_DIR
import pytest  # noqa: E402

import share_store  # noqa: E402
import app as app_mod  # noqa: E402
from _pt_helpers import (FakeRequests, FakeResponse, csrf_client,  # noqa: E402
                         isolate_app)


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    """每用例隔离 + 统一注入可信切片元数据（1000×800）。"""
    _, up_dir = isolate_app(monkeypatch, tmp_path, UPLOAD_DIR,
                            login_limits=True, clear_stores=True)
    for child in up_dir.iterdir():
        if child.is_file():
            child.unlink()
    monkeypatch.setattr(app_mod, "_annotation_slide_bounds",
                        lambda safe: (1000.0, 800.0))
    yield


def _touch(name="demo.svs"):
    p = Path(UPLOAD_DIR) / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"svs-stub")
    return name


# 合法三角形（level-0 整数坐标）。
TRIANGLE = [[100, 100], [400, 100], [250, 400]]

# P1-5 溯源样例（与 HP draw_suspicious_region 发送侧同一套字段口径）。
SNAP_PROV = {
    "snapshot_id": "snap-20260907-1",
    "snapshot_bbox": {"x": 0, "y": 0, "w": 1024, "h": 1024},
    "render_context_fingerprint": "rcfp-abc123",
    "slide_revision": "1700000000:4096",
}


def _mock_plugin_channel(monkeypatch, valid=True):
    """插件 v1 通道 mock（token/grant/installation；与 geom 测试同款口径）。"""
    monkeypatch.setattr(app_mod, "_require_plugin_token",
                        lambda scope=None: ({"sub": "inst1"}, None))
    grant = {"grant_id": "g1", "slide": "demo.svs", "installation_id": "inst1",
             "created_by_user_id": "u1", "session_id": "sess1"}
    monkeypatch.setattr(
        app_mod, "_verify_run_grant",
        (lambda gid, slide, inst, expect_session=None: (True, "")) if valid
        else (lambda gid, slide, inst, expect_session=None: (False, "expired")))
    monkeypatch.setattr(app_mod.share_store, "get_run_grant", lambda gid: grant)
    monkeypatch.setattr(app_mod, "_archived_slide_names", lambda: [])
    monkeypatch.setattr(app_mod.share_store, "get_plugin_installation",
                        lambda iid: {"plugin_id": "histopilot", "version": "0"})
    monkeypatch.setattr(app_mod, "_audit", lambda *a, **k: None)
    monkeypatch.setattr(app_mod, "_legacy_slide_revision", lambda safe: "rev0")


def _post_polygon(c, **overrides):
    body = {
        "label": "AI 描绘", "type": "polygon", "points": TRIANGLE,
        "note": "n", "effect_key": "ek-gate-1", "session_id": "sess1",
    }
    body.update(overrides)
    return c.post("/api/plugin/v1/slides/demo.svs/annotations", json=body,
                  headers={"X-Run-Grant": "g1"})


def _no_rois():
    """ADMIN token 下无任何活 ROI。"""
    return share_store.get_roi(share_store.ADMIN_TOKEN, 0) is None


# =========================================================================== #
# P1-4：写端镜像闸门
# =========================================================================== #
def test_gate_fail_closed_when_mirror_never_seen(monkeypatch):
    """镜像无行（平台从未见过该 session 的开启记录）→ 伪造 polygon 403。"""
    _touch()
    _mock_plugin_channel(monkeypatch, valid=True)
    c = app_mod.app.test_client()
    r = _post_polygon(c, **SNAP_PROV)
    assert r.status_code == 403, r.get_data(as_text=True)
    err = r.get_json()["error"]
    assert err["code"] == "ai_drawing_disabled"
    assert err["retryable"] is False
    assert _no_rois()  # 未落库


def test_gate_blocks_forged_polygon_when_off(monkeypatch):
    """开关关闭（镜像 false）→ 伪造/重放 polygon 403，无新 ROI。"""
    _touch()
    _mock_plugin_channel(monkeypatch, valid=True)
    share_store.upsert_ai_session_drawing_flag("sess1", False)
    c = app_mod.app.test_client()
    r = _post_polygon(c, **SNAP_PROV)
    assert r.status_code == 403
    assert r.get_json()["error"]["code"] == "ai_drawing_disabled"
    assert _no_rois()


def test_gate_allows_polygon_when_on(monkeypatch):
    """开关开启（镜像 true）→ polygon 200 落库（run grant 通道不变）。"""
    _touch()
    _mock_plugin_channel(monkeypatch, valid=True)
    share_store.upsert_ai_session_drawing_flag("sess1", True)
    c = app_mod.app.test_client()
    r = _post_polygon(c, **SNAP_PROV)
    assert r.status_code == 200, r.get_data(as_text=True)
    roi = share_store.get_roi(share_store.ADMIN_TOKEN, 0)
    assert roi is not None and roi["type"] == "polygon"
    assert roi["review_status"] == "pending"


def test_gate_blocks_late_polygon_after_midrun_off(monkeypatch):
    """运行中关闭：先开→写成功，再关→迟到 polygon 403 且无新 ROI。"""
    _touch()
    _mock_plugin_channel(monkeypatch, valid=True)
    share_store.upsert_ai_session_drawing_flag("sess1", True)
    c = app_mod.app.test_client()
    r1 = _post_polygon(c, effect_key="ek-gate-open", **SNAP_PROV)
    assert r1.status_code == 200
    first = share_store.get_roi(share_store.ADMIN_TOKEN, 0)
    assert first is not None
    # 运行中用户关闭（经代理路由镜像写回 false——这里直写镜像等价）。
    share_store.upsert_ai_session_drawing_flag("sess1", False)
    r2 = _post_polygon(c, effect_key="ek-gate-late", **SNAP_PROV)
    assert r2.status_code == 403
    assert r2.get_json()["error"]["code"] == "ai_drawing_disabled"
    late = share_store.get_roi(share_store.ADMIN_TOKEN, 1)
    assert late is None  # 迟到写入未落库
    assert share_store.get_roi(share_store.ADMIN_TOKEN, 0)["annotation_id"] \
        == first["annotation_id"]


def test_gate_covers_freehand_too(monkeypatch):
    """freehand 同受闸门约束（镜像无行 → 403）。"""
    _touch()
    _mock_plugin_channel(monkeypatch, valid=True)
    c = app_mod.app.test_client()
    r = c.post("/api/plugin/v1/slides/demo.svs/annotations", json={
        "label": "AI 描图", "type": "freehand",
        "points": [[10, 10], [60, 40], [90, 90], [40, 60]],
        "effect_key": "ek-gate-fh", "session_id": "sess1",
    }, headers={"X-Run-Grant": "g1"})
    assert r.status_code == 403
    assert r.get_json()["error"]["code"] == "ai_drawing_disabled"
    assert _no_rois()


def test_gate_does_not_touch_rect_path(monkeypatch):
    """矩形路径不受闸门影响：镜像无行仍 200（create_annotation 通道不变）。"""
    _touch()
    _mock_plugin_channel(monkeypatch, valid=True)
    c = app_mod.app.test_client()
    r = c.post("/api/plugin/v1/slides/demo.svs/annotations", json={
        "label": "AI-rect", "x": 5, "y": 5, "width_px": 220, "height_px": 90,
        "effect_key": "ek-gate-rect", "session_id": "sess1",
    }, headers={"X-Run-Grant": "g1"})
    assert r.status_code == 200, r.get_data(as_text=True)
    assert r.get_json()["type"] == "rect"


def test_gate_still_requires_run_grant(monkeypatch):
    """闸门在 run grant 之后：grant 无效仍是 403 run_grant_invalid（优先级不变）。"""
    _touch()
    _mock_plugin_channel(monkeypatch, valid=False)
    share_store.upsert_ai_session_drawing_flag("sess1", True)
    c = app_mod.app.test_client()
    r = _post_polygon(c, **SNAP_PROV)
    assert r.status_code == 403
    assert r.get_json()["error"]["code"] == "run_grant_invalid"
    assert _no_rois()


# =========================================================================== #
# P1-4：代理路由的镜像 upsert（HP 成功才镜像；失败不改变镜像）
# =========================================================================== #
def _mock_proxy_owner(monkeypatch):
    """AI 代理鉴权放行（owner 语义在本文件不测，见 test_ai_session_owner）。"""
    monkeypatch.setattr(app_mod, "_require_ai_session_owner", lambda sid: None)


def _proxy_toggle(c, enabled=True):
    return c.post("/api/ai/session/sess1/drawing", json={"enabled": enabled})


def _browser_client(app):
    """带 CSRF token 的浏览器客户端（/api/ai/* 是浏览器路由，POST 强制 CSRF）。"""
    return csrf_client(app.test_client())


def _install_fake_sidecar(monkeypatch, status=200, body=None):
    fake = FakeRequests()
    payload = body if body is not None else {"ok": True, "allow_ai_drawing": True}
    fake.register("POST", "/session/sess1/drawing",
                  lambda b, q, h, k: FakeResponse(status, payload))
    monkeypatch.setattr(app_mod, "requests", fake)
    return fake


def _proxy_toggle(c, enabled=True):
    return c.post("/api/ai/session/sess1/drawing", json={"enabled": enabled})


def test_proxy_mirrors_hp_authoritative_value(monkeypatch):
    """HP 200 {ok, allow_ai_drawing:true} → 镜像 true；再关 → 镜像 false。"""
    _install_fake_sidecar(monkeypatch, status=200,
                          body={"ok": True, "allow_ai_drawing": True})
    _mock_proxy_owner(monkeypatch)
    c = _browser_client(app_mod.app)
    r = _proxy_toggle(c, enabled=True)
    assert r.status_code == 200
    assert share_store.get_ai_session_drawing_flag("sess1") is True
    # 同一 session 再切换为关：镜像跟随权威值。
    _install_fake_sidecar(monkeypatch, status=200,
                          body={"ok": True, "allow_ai_drawing": False})
    r2 = _proxy_toggle(c, enabled=False)
    assert r2.status_code == 200
    assert share_store.get_ai_session_drawing_flag("sess1") is False


def test_proxy_failure_keeps_mirror_unchanged(monkeypatch):
    """HP 5xx / 404 / 畸形成功体 → 镜像不变（不推定开关状态）。"""
    _mock_proxy_owner(monkeypatch)
    c = _browser_client(app_mod.app)
    share_store.upsert_ai_session_drawing_flag("sess1", True)

    _install_fake_sidecar(monkeypatch, status=500, body={"error": "boom"})
    assert _proxy_toggle(c).status_code == 500  # 上游错误码透传
    assert share_store.get_ai_session_drawing_flag("sess1") is True

    _install_fake_sidecar(monkeypatch, status=404, body={"error": "no session"})
    _proxy_toggle(c)
    assert share_store.get_ai_session_drawing_flag("sess1") is True

    # 200 但响应体缺权威布尔（旧 sidecar 形态）——不镜像。
    _install_fake_sidecar(monkeypatch, status=200, body={"ok": True})
    _proxy_toggle(c, enabled=False)
    assert share_store.get_ai_session_drawing_flag("sess1") is True

    # 开启前的失败路径：镜像仍无行（写入口 fail closed）。
    share_store.upsert_ai_session_drawing_flag("sess1", False)
    _install_fake_sidecar(monkeypatch, status=500, body={"error": "boom"})
    _proxy_toggle(c, enabled=True)
    assert share_store.get_ai_session_drawing_flag("sess1") is False


def test_store_mirror_semantics(monkeypatch):
    """镜像存储语义：无行 None / false / true 可区分；upsert 幂等且可覆盖。"""
    assert share_store.get_ai_session_drawing_flag("nope") is None
    assert share_store.get_ai_session_drawing_flag("") is None
    share_store.upsert_ai_session_drawing_flag("s-a", False)
    assert share_store.get_ai_session_drawing_flag("s-a") is False
    share_store.upsert_ai_session_drawing_flag("s-a", True)
    share_store.upsert_ai_session_drawing_flag("s-a", True)  # 幂等
    assert share_store.get_ai_session_drawing_flag("s-a") is True
    with pytest.raises(ValueError):
        share_store.upsert_ai_session_drawing_flag("s-b", "true")  # 非布尔拒绝


# =========================================================================== #
# P1-5：来源快照溯源
# =========================================================================== #
def test_polygon_without_snapshot_id_rejected(monkeypatch):
    """polygon 缺 snapshot_id → 400 invalid_request，不落库（不给静默降级）。"""
    _touch()
    _mock_plugin_channel(monkeypatch, valid=True)
    share_store.upsert_ai_session_drawing_flag("sess1", True)
    c = app_mod.app.test_client()
    r = _post_polygon(c)  # 无任何溯源字段
    assert r.status_code == 400
    assert r.get_json()["error"]["code"] == "invalid_request"
    assert "snapshot_id" in r.get_json()["error"]["message"]
    assert _no_rois()
    # 空串同缺省。
    r2 = _post_polygon(c, snapshot_id="   ")
    assert r2.status_code == 400


def test_polygon_with_malformed_snapshot_fields_rejected(monkeypatch):
    """溯源字段给出即校验：畸形 snapshot_bbox / 非字符串指纹 → 400 不落库。"""
    _touch()
    _mock_plugin_channel(monkeypatch, valid=True)
    share_store.upsert_ai_session_drawing_flag("sess1", True)
    c = app_mod.app.test_client()
    bad_bbox = dict(SNAP_PROV, snapshot_bbox={"x": 0, "y": 0, "w": "wide", "h": 10})
    r = _post_polygon(c, **bad_bbox)
    assert r.status_code == 400
    assert r.get_json()["error"]["code"] == "invalid_request"
    nonfinite = dict(SNAP_PROV, snapshot_bbox={"x": 0, "y": 0, "w": 1e999, "h": 10})
    r2 = _post_polygon(c, **nonfinite)
    assert r2.status_code == 400
    bad_fp = dict(SNAP_PROV, render_context_fingerprint=12345)
    r3 = _post_polygon(c, **bad_fp)
    assert r3.status_code == 400
    assert _no_rois()


def test_polygon_provenance_persisted(monkeypatch):
    """合法溯源字段逐键持久化进 ROI provenance（session/用户仍从 grant）。"""
    _touch()
    _mock_plugin_channel(monkeypatch, valid=True)
    share_store.upsert_ai_session_drawing_flag("sess1", True)
    c = app_mod.app.test_client()
    r = _post_polygon(c, **SNAP_PROV)
    assert r.status_code == 200, r.get_data(as_text=True)
    prov = r.get_json()["provenance"]
    assert prov["snapshot_id"] == SNAP_PROV["snapshot_id"]
    assert prov["snapshot_bbox"] == SNAP_PROV["snapshot_bbox"]
    assert prov["render_context_fingerprint"] == SNAP_PROV["render_context_fingerprint"]
    assert prov["slide_revision"] == SNAP_PROV["slide_revision"]
    # grant 绑定的会话与用户归属不被请求体改写（P1 通用约束）。
    assert prov["session_id"] == "sess1"
    assert prov["created_by_user_id"] == "u1"
    assert prov["slide_asset_revision"] == "rev0"
    # 读路径 roundtrip（get_roi 输出同键）。
    stored = share_store.get_roi(share_store.ADMIN_TOKEN, 0)
    assert stored["provenance"]["snapshot_id"] == SNAP_PROV["snapshot_id"]
    assert stored["provenance"]["snapshot_bbox"] == SNAP_PROV["snapshot_bbox"]
    assert stored["provenance"]["render_context_fingerprint"] \
        == SNAP_PROV["render_context_fingerprint"]


def test_polygon_provenance_partial_persisted(monkeypatch):
    """只带 snapshot_id（无 bbox/指纹）也放行：可选字段缺省不伪造。"""
    _touch()
    _mock_plugin_channel(monkeypatch, valid=True)
    share_store.upsert_ai_session_drawing_flag("sess1", True)
    c = app_mod.app.test_client()
    r = _post_polygon(c, snapshot_id="snap-only")
    assert r.status_code == 200
    prov = r.get_json()["provenance"]
    assert prov["snapshot_id"] == "snap-only"
    assert "snapshot_bbox" not in prov
    assert "render_context_fingerprint" not in prov
    assert "slide_revision" not in prov
