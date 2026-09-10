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
# P1-3 起 polygon 必带 snapshot_attestation（按需用 _snap_prov 覆盖字段）。
SNAP_PROV = {
    "snapshot_id": "snap-20260907-1",
    "snapshot_bbox": {"x": 0, "y": 0, "w": 1024, "h": 1024},
    "render_context_fingerprint": "rcfp-abc123",
    "slide_revision": "rev0",
}


def _attest(sid="sess1", snap="snap-20260907-1",
            bbox=(0, 0, 1024, 1024), rev="rev0", fp="rcfp-abc123",
            exp_delta=600, key=None):
    from _pt_helpers import make_snapshot_attestation
    return make_snapshot_attestation(
        key or app_mod.AI_INTERNAL_TOKEN, sid=sid, snap=snap, bbox=bbox,
        rev=rev, fp=fp, exp_delta=exp_delta)


def _snap_prov(**overrides):
    """合法快照溯源（含匹配 attestation）；overrides 直接覆盖最终 dict。"""
    prov = dict(SNAP_PROV, snapshot_attestation=_attest())
    prov.update(overrides)
    return prov


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
    r = _post_polygon(c, **_snap_prov())
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
    r = _post_polygon(c, **_snap_prov())
    assert r.status_code == 403
    assert r.get_json()["error"]["code"] == "ai_drawing_disabled"
    assert _no_rois()


def test_gate_allows_polygon_when_on(monkeypatch):
    """开关开启（镜像 true）→ polygon 200 落库（run grant 通道不变）。"""
    _touch()
    _mock_plugin_channel(monkeypatch, valid=True)
    share_store.upsert_ai_session_drawing_flag("sess1", True)
    c = app_mod.app.test_client()
    r = _post_polygon(c, **_snap_prov())
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
    r1 = _post_polygon(c, effect_key="ek-gate-open", **_snap_prov())
    assert r1.status_code == 200
    first = share_store.get_roi(share_store.ADMIN_TOKEN, 0)
    assert first is not None
    # 运行中用户关闭（经代理路由镜像写回 false——这里直写镜像等价）。
    share_store.upsert_ai_session_drawing_flag("sess1", False)
    r2 = _post_polygon(c, effect_key="ek-gate-late", **_snap_prov())
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
    r = _post_polygon(c, **_snap_prov())
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
    """HP 5xx / 404 / 畸形成功体 → 镜像不因**开启**失败而放大（不推定开关）。

    二轮 review P1-2 后语义按方向区分：关闭（enabled=false）在请求 HP 前
    已预写 false（见 test_proxy_disable_* 用例）；本用例锁**开启方向**失败
    不镜像——旧值保留（无行/非 true 写入口 fail closed，不放大权限）。
    """
    _mock_proxy_owner(monkeypatch)
    c = _browser_client(app_mod.app)
    share_store.upsert_ai_session_drawing_flag("sess1", True)

    _install_fake_sidecar(monkeypatch, status=500, body={"error": "boom"})
    assert _proxy_toggle(c, enabled=True).status_code == 500  # 上游错误码透传
    assert share_store.get_ai_session_drawing_flag("sess1") is True

    _install_fake_sidecar(monkeypatch, status=404, body={"error": "no session"})
    _proxy_toggle(c, enabled=True)
    assert share_store.get_ai_session_drawing_flag("sess1") is True

    # 200 但响应体缺权威布尔（旧 sidecar 形态）——开启方向不镜像。
    _install_fake_sidecar(monkeypatch, status=200, body={"ok": True})
    _proxy_toggle(c, enabled=True)
    assert share_store.get_ai_session_drawing_flag("sess1") is True

    # 开启前的失败路径：镜像仍无行（写入口 fail closed）。
    share_store.upsert_ai_session_drawing_flag("sess1", False)
    _install_fake_sidecar(monkeypatch, status=500, body={"error": "boom"})
    _proxy_toggle(c, enabled=True)
    assert share_store.get_ai_session_drawing_flag("sess1") is False


def test_proxy_disable_tightens_mirror_before_hp(monkeypatch):
    """二轮 review P1-2 回归①：关闭在 HP 前预写 false——HP 已应用关闭但
    响应途中丢失（连接失败 503）也绝不残留旧 true。"""
    _mock_proxy_owner(monkeypatch)
    c = _browser_client(app_mod.app)
    share_store.upsert_ai_session_drawing_flag("sess1", True)

    _install_fake_sidecar(monkeypatch, status=500, body={"error": "boom"})
    assert _proxy_toggle(c, enabled=False).status_code == 500
    assert share_store.get_ai_session_drawing_flag("sess1") is False

    # HP 完全不可达（连接失败 → _proxy_json 503）：镜像同样已收紧。
    fake = _install_fake_sidecar(monkeypatch, status=200,
                                 body={"ok": True, "allow_ai_drawing": False})
    fake.set_unreachable()
    assert _proxy_toggle(c, enabled=False).status_code == 503
    assert share_store.get_ai_session_drawing_flag("sess1") is False


def test_proxy_disable_local_write_failure_returns_503(monkeypatch):
    """二轮 review P1-2 回归②：关闭预写失败 → 503，且绝不向 HP 发出请求。"""
    _mock_proxy_owner(monkeypatch)
    c = _browser_client(app_mod.app)
    share_store.upsert_ai_session_drawing_flag("sess1", True)
    fake = _install_fake_sidecar(monkeypatch, status=200,
                                 body={"ok": True, "allow_ai_drawing": False})

    def _fail_upsert(session_id, value):
        raise RuntimeError("pg down")

    monkeypatch.setattr(app_mod.share_store, "upsert_ai_session_drawing_flag",
                        _fail_upsert)
    r = _proxy_toggle(c, enabled=False)
    assert r.status_code == 503
    assert r.get_json()["code"] == "mirror_write_failed"
    # 预写先于转发：HP 一个请求都没收到
    assert fake.calls == []
    # 镜像保持旧 true（本次关闭失败，用户可重试；闸门状态未被破坏）
    assert share_store.get_ai_session_drawing_flag("sess1") is True


def test_proxy_disable_then_late_write_blocked(monkeypatch):
    """二轮 review P1-2 回归③：关闭（即便 HP 响应丢失）之后，迟到的
    plugin v1 polygon 写入立即被写端闸门拒绝（镜像已是 false）。"""
    _mock_proxy_owner(monkeypatch)
    c = _browser_client(app_mod.app)
    share_store.upsert_ai_session_drawing_flag("sess1", True)

    # 关闭时 HP 不可达（响应丢失）——镜像仍已收紧为 false
    fake = _install_fake_sidecar(monkeypatch, status=200,
                                 body={"ok": True, "allow_ai_drawing": False})
    fake.set_unreachable()
    _proxy_toggle(c, enabled=False)
    assert share_store.get_ai_session_drawing_flag("sess1") is False

    # 迟到写入：镜像 false → 写入口拒绝（与 test_gate_blocks_* 同一口径）
    _touch()
    _mock_plugin_channel(monkeypatch, valid=True)
    r = _post_polygon(c, effect_key="ek-late-after-off", **_snap_prov())
    assert r.status_code == 403
    assert r.get_json()["error"]["code"] == "ai_drawing_disabled"
    assert _no_rois()


# =========================================================================== #
# 第四轮 review：跨服务 generation + 稳定错误
# =========================================================================== #
def test_proxy_forwards_drawing_generation(monkeypatch):
    """第四轮 P1：意图代数随请求传 HP（关闭=预写代；开启=基线+1）——
    客户端伪造的同名字段被覆盖（不采信浏览器值）。"""
    _mock_proxy_owner(monkeypatch)
    c = _browser_client(app_mod.app)
    share_store.upsert_ai_session_drawing_flag("sess1", True)  # gen=1

    fake = _install_fake_sidecar(monkeypatch, status=200,
                                 body={"ok": True, "allow_ai_drawing": False})
    r = c.post("/api/ai/session/sess1/drawing",
               json={"enabled": False, "drawing_generation": 999})
    assert r.status_code == 200
    # 关闭：预写自增到 gen=2 并随请求转发（伪造 999 被覆盖）
    assert fake.calls[0]["body"]["drawing_generation"] == 2
    assert share_store.get_ai_session_drawing_flag("sess1") is False

    # 关闭回程重申 CAS 生效后 gen=3；开启意图代 = 3+1 = 4（伪造 1 被覆盖）
    base_before_open = share_store.get_ai_session_drawing_generation("sess1")
    fake2 = _install_fake_sidecar(monkeypatch, status=200,
                                  body={"ok": True, "allow_ai_drawing": True})
    r2 = c.post("/api/ai/session/sess1/drawing",
                json={"enabled": True, "drawing_generation": 1})
    assert r2.status_code == 200
    assert fake2.calls[0]["body"]["drawing_generation"] == base_before_open + 1
    assert share_store.get_ai_session_drawing_flag("sess1") is True


def test_proxy_open_cas_failure_returns_503(monkeypatch):
    """第四轮 P2：开启获 HP 200/true 但本地 CAS 抛错 → 覆盖为 503
    mirror_write_failed（绝不让浏览器以为已开启）。"""
    _mock_proxy_owner(monkeypatch)
    c = _browser_client(app_mod.app)
    share_store.upsert_ai_session_drawing_flag("sess1", False)
    _install_fake_sidecar(monkeypatch, status=200,
                          body={"ok": True, "allow_ai_drawing": True})

    def _boom(sid, val, max_gen):
        raise RuntimeError("pg down")

    monkeypatch.setattr(app_mod.share_store, "cas_ai_session_drawing_flag",
                        _boom)
    r = _proxy_toggle(c, enabled=True)
    assert r.status_code == 503
    assert r.get_json()["code"] == "mirror_write_failed"
    # 本地写闸仍关（fail closed）
    assert share_store.get_ai_session_drawing_flag("sess1") is False


def test_proxy_superseded_toggle_returns_409(monkeypatch):
    """第四轮 P2：CAS 意外 no-op（行已被更晚请求写入）→ 409 stale_generation
    + 当前镜像值（稳定错误而非透传成功）。"""
    _mock_proxy_owner(monkeypatch)
    c = _browser_client(app_mod.app)
    share_store.upsert_ai_session_drawing_flag("sess1", True)  # gen=1

    def _delayed_open(body, q, h, k):
        # 开启响应回程前，更晚的关闭已完成预写（gen 超越开启基线）
        share_store.upsert_ai_session_drawing_flag("sess1", False)  # gen=2
        return FakeResponse(200, {"ok": True, "allow_ai_drawing": True})

    fake = FakeRequests()
    fake.register("POST", "/session/sess1/drawing", _delayed_open)
    monkeypatch.setattr(app_mod, "requests", fake)
    r = _proxy_toggle(c, enabled=True)
    assert r.status_code == 409
    body = r.get_json()
    assert body["code"] == "stale_generation"
    assert body["allow_ai_drawing"] is False  # 当前权威镜像值
    assert share_store.get_ai_session_drawing_flag("sess1") is False


def test_proxy_close_cas_failure_returns_503(monkeypatch):
    """五轮 P2：关闭重申 CAS 抛错 → 一律 503（镜像状态未知不得报成功；
    此前「预写已收紧即透传」属残余洞——预写后可能已有并发写）。"""
    _mock_proxy_owner(monkeypatch)
    c = _browser_client(app_mod.app)
    share_store.upsert_ai_session_drawing_flag("sess1", True)
    _install_fake_sidecar(monkeypatch, status=200,
                          body={"ok": True, "allow_ai_drawing": False})

    def _boom(sid, val, max_gen):
        raise RuntimeError("pg down")

    monkeypatch.setattr(app_mod.share_store, "cas_ai_session_drawing_flag",
                        _boom)
    r = _proxy_toggle(c, enabled=False)
    assert r.status_code == 503
    assert r.get_json()["code"] == "mirror_write_failed"
    # 预写仍已生效（本地收紧不受影响），用户刷新/重试即可对齐
    assert share_store.get_ai_session_drawing_flag("sess1") is False


def test_store_mirror_generation_cas(monkeypatch):
    """三轮 review P1（0041）：generation 单调 + CAS 语义。

    - 无条件 upsert 每次自增 generation 并返回；
    - cas 到「未超越基线」生效（gen+1）、到「已被超越基线」no-op；
    - cas 对无行 session 直接生效（首代）。
    """
    assert share_store.get_ai_session_drawing_generation("s-cas") == 0
    g1 = share_store.upsert_ai_session_drawing_flag("s-cas", True)
    assert g1 == 1
    g2 = share_store.upsert_ai_session_drawing_flag("s-cas", False)
    assert g2 == 2 and share_store.get_ai_session_drawing_flag("s-cas") is False
    # 基线=2（未被超越）→ 生效
    assert share_store.cas_ai_session_drawing_flag("s-cas", True, 2) is True
    assert share_store.get_ai_session_drawing_flag("s-cas") is True
    assert share_store.get_ai_session_drawing_generation("s-cas") == 3
    # 基线=1（已被 gen=3 超越）→ no-op，值不变
    assert share_store.cas_ai_session_drawing_flag("s-cas", False, 1) is False
    assert share_store.get_ai_session_drawing_flag("s-cas") is True
    assert share_store.get_ai_session_drawing_generation("s-cas") == 3
    # 无行：直接生效（首代），基线 0
    assert share_store.cas_ai_session_drawing_flag("s-cas-new", True, 0) is True
    assert share_store.get_ai_session_drawing_flag("s-cas-new") is True
    with pytest.raises(ValueError):
        share_store.cas_ai_session_drawing_flag("s-cas", True, -1)


def test_proxy_stale_open_response_cannot_override_later_close(monkeypatch):
    """三轮 review P1 复现用例：开启请求在 HP 排队期间用户完成关闭（预写
    false），晚到的开启成功响应（allow=true）必须被 CAS 作废——镜像保持
    false，迟到 polygon 仍被写闸拒绝。"""
    _mock_proxy_owner(monkeypatch)
    c = _browser_client(app_mod.app)
    share_store.upsert_ai_session_drawing_flag("sess1", True)  # gen=1

    def _delayed_open(body, q, h, k):
        # 开启响应回程前，关闭请求已完成预写+确认（generation 超越开启
        # 占代：开启 reserve 占 2，关闭预写占 3 并重申生效至 4）
        close_gen = share_store.upsert_ai_session_drawing_flag("sess1", False)
        assert close_gen == 3
        assert share_store.cas_ai_session_drawing_flag("sess1", False, 3)
        return FakeResponse(200, {"ok": True, "allow_ai_drawing": True})

    fake = FakeRequests()
    fake.register("POST", "/session/sess1/drawing", _delayed_open)
    monkeypatch.setattr(app_mod, "requests", fake)
    r = _proxy_toggle(c, enabled=True)
    # 第四轮起被取代的请求得稳定 409（而非透传 HP 200），镜像断言不变
    assert r.status_code == 409
    assert r.get_json()["code"] == "stale_generation"
    # 关键：镜像未被旧开启响应写回 true
    assert share_store.get_ai_session_drawing_flag("sess1") is False
    # 迟到描绘仍被拒
    _touch()
    _mock_plugin_channel(monkeypatch, valid=True)
    r2 = _post_polygon(c, effect_key="ek-late-stale-open", **_snap_prov())
    assert r2.status_code == 403
    assert r2.get_json()["error"]["code"] == "ai_drawing_disabled"
    assert _no_rois()


def test_proxy_stale_close_response_cannot_override_later_open(monkeypatch):
    """对称反例：关闭请求响应回程前，更晚的开启已把镜像写 true——旧关闭
    响应重申 false 也只能 CAS 到自己预写代，不得覆盖其后的 true。"""
    _mock_proxy_owner(monkeypatch)
    c = _browser_client(app_mod.app)
    share_store.upsert_ai_session_drawing_flag("sess1", True)   # gen=1

    def _delayed_close(body, q, h, k):
        # 关闭预写已完成（路由层 gen=2）；其后的开启请求 CAS 到基线 2 → true
        assert share_store.cas_ai_session_drawing_flag("sess1", True, 2) is True
        return FakeResponse(200, {"ok": True, "allow_ai_drawing": False})

    fake = FakeRequests()
    fake.register("POST", "/session/sess1/drawing", _delayed_close)
    monkeypatch.setattr(app_mod, "requests", fake)
    r = _proxy_toggle(c, enabled=False)
    # 旧关闭响应被其后的开启取代：409 + 镜像保持 true
    assert r.status_code == 409
    assert share_store.get_ai_session_drawing_flag("sess1") is True


def test_proxy_open_then_close_generations_strictly_increase(monkeypatch):
    """五轮 P1 复现回归：开启与紧随的关闭必须拿到**不同**代（开启原子
    占代）。旧行为 get+1 不落库——close 预写与 open 同代，HP 把后到的
    关闭当同代旧请求丢弃 → PT=false / HP=true 分叉。"""
    _mock_proxy_owner(monkeypatch)
    c = _browser_client(app_mod.app)
    share_store.upsert_ai_session_drawing_flag("sess1", True)  # gen=1

    fake = FakeRequests()

    def _open_handler(body, q, h, k):
        return FakeResponse(200, {"ok": True, "allow_ai_drawing": True})

    def _close_handler(body, q, h, k):
        return FakeResponse(200, {"ok": True, "allow_ai_drawing": False})

    fake.register("POST", "/session/sess1/drawing", _open_handler)
    monkeypatch.setattr(app_mod, "requests", fake)
    assert _proxy_toggle(c, enabled=True).status_code == 200
    open_gen = fake.calls[0]["body"]["drawing_generation"]

    fake2 = FakeRequests()
    fake2.register("POST", "/session/sess1/drawing", _close_handler)
    monkeypatch.setattr(app_mod, "requests", fake2)
    assert _proxy_toggle(c, enabled=False).status_code == 200
    close_gen = fake2.calls[0]["body"]["drawing_generation"]

    assert close_gen > open_gen  # 严格递增（旧代码 get+1 下两者相等 → 同代分叉）
    assert share_store.get_ai_session_drawing_flag("sess1") is False


def test_proxy_stale_current_read_failure_returns_503(monkeypatch):
    """五轮 P2：CAS no-op（被取代）后读当前镜像值失败 → 503，绝不能被
    代理层吞掉后透传 HP 200。"""
    _mock_proxy_owner(monkeypatch)
    c = _browser_client(app_mod.app)
    share_store.upsert_ai_session_drawing_flag("sess1", True)  # gen=1

    def _delayed_open(body, q, h, k):
        share_store.upsert_ai_session_drawing_flag("sess1", False)  # gen=2
        return FakeResponse(200, {"ok": True, "allow_ai_drawing": True})

    fake = FakeRequests()
    fake.register("POST", "/session/sess1/drawing", _delayed_open)
    monkeypatch.setattr(app_mod, "requests", fake)

    real_cas = share_store.cas_ai_session_drawing_flag

    def _cas_ok(sid, val, max_gen):
        return False if real_cas(sid, val, max_gen) else False

    monkeypatch.setattr(app_mod.share_store,
                        "get_ai_session_drawing_flag",
                        lambda sid: (_ for _ in ()).throw(
                            RuntimeError("pg down")))
    r = _proxy_toggle(c, enabled=True)
    assert r.status_code == 503
    assert r.get_json()["code"] == "mirror_read_failed"


def test_store_reserve_generation_semantics(monkeypatch):
    """reserve：原子占代、严格递增、不改 allow；无行时插 false（fail-closed）；
    与 upsert 交错的代数全局唯一。"""
    assert share_store.get_ai_session_drawing_flag("s-res") is None
    g1 = share_store.reserve_ai_session_drawing_generation("s-res")
    assert g1 == 1
    assert share_store.get_ai_session_drawing_flag("s-res") is False  # 不放大
    g2 = share_store.upsert_ai_session_drawing_flag("s-res", True)   # 占代 2
    g3 = share_store.reserve_ai_session_drawing_generation("s-res")  # 占代 3
    assert (g1, g2, g3) == (1, 2, 3)
    assert share_store.get_ai_session_drawing_flag("s-res") is True  # upsert 值
    # 连续 reserve 仍严格递增
    g4 = share_store.reserve_ai_session_drawing_generation("s-res")
    assert g4 == 4 and share_store.get_ai_session_drawing_flag("s-res") is True


def test_proxy_open_reservation_failure_returns_503(monkeypatch):
    """开启请求原子占代失败 → 503 且不转发 HP（不放大权限路径同样
    fail-closed；五轮 P1 起开启经 reserve 原子占代）。"""
    _mock_proxy_owner(monkeypatch)
    c = _browser_client(app_mod.app)
    fake = _install_fake_sidecar(monkeypatch, status=200,
                                 body={"ok": True, "allow_ai_drawing": True})

    def _boom(sid):
        raise RuntimeError("pg down")

    monkeypatch.setattr(app_mod.share_store,
                        "reserve_ai_session_drawing_generation", _boom)
    r = _proxy_toggle(c, enabled=True)
    assert r.status_code == 503
    assert r.get_json()["code"] == "mirror_write_failed"
    assert fake.calls == []


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
    r = _post_polygon(c, **_snap_prov())
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
    """只带 snapshot_id（无 bbox/指纹）也放行：可选字段缺省不伪造——
    attestation 载荷相应字段为 null（双方同缺即一致）。"""
    _touch()
    _mock_plugin_channel(monkeypatch, valid=True)
    share_store.upsert_ai_session_drawing_flag("sess1", True)
    c = app_mod.app.test_client()
    r = _post_polygon(c, snapshot_id="snap-only",
                      snapshot_attestation=_attest(snap="snap-only",
                                                   bbox=None, rev=None,
                                                   fp=None))
    assert r.status_code == 200, r.get_data(as_text=True)
    prov = r.get_json()["provenance"]
    assert prov["snapshot_id"] == "snap-only"
    assert "snapshot_bbox" not in prov
    assert "render_context_fingerprint" not in prov
    assert "slide_revision" not in prov


# =========================================================================== #
# P1-3（二轮 review）：快照 provenance 权威归属（HP 服务端 attestation）
# =========================================================================== #
def test_polygon_without_attestation_rejected(monkeypatch):
    """带 snapshot_id 但无 attestation → 400（形状合法≠归属可信：有效
    plugin token + run grant 的调用者仍可自报任意 snapshot 字段）。"""
    _touch()
    _mock_plugin_channel(monkeypatch, valid=True)
    share_store.upsert_ai_session_drawing_flag("sess1", True)
    c = app_mod.app.test_client()
    prov = {k: v for k, v in _snap_prov().items()
            if k != "snapshot_attestation"}
    r = _post_polygon(c, **prov)
    assert r.status_code == 400
    assert r.get_json()["error"]["code"] == "invalid_request"
    assert "snapshot_attestation" in r.get_json()["error"]["message"]
    assert _no_rois()


def test_polygon_attestation_forged_bbox_rejected(monkeypatch):
    """请求 bbox 改写（attestation 覆盖服务端真值 1024×1024）→ 400。"""
    _touch()
    _mock_plugin_channel(monkeypatch, valid=True)
    share_store.upsert_ai_session_drawing_flag("sess1", True)
    c = app_mod.app.test_client()
    r = _post_polygon(c, **_snap_prov(
        snapshot_bbox={"x": 0, "y": 0, "w": 512, "h": 512}))
    assert r.status_code == 400
    assert _no_rois()


def test_polygon_attestation_cross_session_rejected(monkeypatch):
    """跨 session 快照（attestation.sid=其他会话）→ 400。"""
    _touch()
    _mock_plugin_channel(monkeypatch, valid=True)
    share_store.upsert_ai_session_drawing_flag("sess1", True)
    c = app_mod.app.test_client()
    r = _post_polygon(c, **_snap_prov(
        snapshot_attestation=_attest(sid="sess-other")))
    assert r.status_code == 400
    assert "会话" in r.get_json()["error"]["message"]
    assert _no_rois()


def test_polygon_attestation_expired_rejected(monkeypatch):
    """过期 attestation → 400（短 TTL 缩小信令重放窗口）。"""
    _touch()
    _mock_plugin_channel(monkeypatch, valid=True)
    share_store.upsert_ai_session_drawing_flag("sess1", True)
    c = app_mod.app.test_client()
    r = _post_polygon(c, **_snap_prov(
        snapshot_attestation=_attest(exp_delta=-30)))
    assert r.status_code == 400
    assert "过期" in r.get_json()["error"]["message"]
    assert _no_rois()


def test_polygon_attestation_tampered_payload_rejected(monkeypatch):
    """payload 字节被改（改 bbox 后不重算 mac）→ 签名失配 400。"""
    import base64
    import json as _json
    _touch()
    _mock_plugin_channel(monkeypatch, valid=True)
    share_store.upsert_ai_session_drawing_flag("sess1", True)
    c = app_mod.app.test_client()
    v1, seg, mac = _attest().split(".")
    pad = "=" * (-len(seg) % 4)
    payload = _json.loads(base64.urlsafe_b64decode(seg + pad))
    payload["bbox"] = [0, 0, 512, 512]
    forged = base64.urlsafe_b64encode(
        _json.dumps(payload, separators=(",", ":")).encode()
    ).decode().rstrip("=")
    r = _post_polygon(c, **_snap_prov(
        snapshot_attestation="v1.%s.%s" % (forged, mac)))
    assert r.status_code == 400
    assert _no_rois()


def test_polygon_attestation_wrong_key_rejected(monkeypatch):
    """非共享密钥签发（密钥不匹配）→ 签名失配 400。"""
    _touch()
    _mock_plugin_channel(monkeypatch, valid=True)
    share_store.upsert_ai_session_drawing_flag("sess1", True)
    c = app_mod.app.test_client()
    r = _post_polygon(c, **_snap_prov(
        snapshot_attestation=_attest(key="not-the-internal-token")))
    assert r.status_code == 400
    assert _no_rois()


def test_polygon_attestation_stale_asset_revision_conflict(monkeypatch):
    """attestation rev=旧资产 revision（快照后切片被替换）→ 409
    slide_revision_conflict（几何坐标系可能漂移，fail-closed）。"""
    _touch()
    _mock_plugin_channel(monkeypatch, valid=True)
    share_store.upsert_ai_session_drawing_flag("sess1", True)
    c = app_mod.app.test_client()
    r = _post_polygon(c, **_snap_prov(
        slide_revision="rev-old",
        snapshot_attestation=_attest(rev="rev-old")))
    assert r.status_code == 409
    assert r.get_json()["error"]["code"] == "slide_revision_conflict"
    assert _no_rois()


def test_polygon_attestation_valid_not_persisted(monkeypatch):
    """合法 attestation 通过且不进 provenance（一次性凭据，验证即弃）。"""
    _touch()
    _mock_plugin_channel(monkeypatch, valid=True)
    share_store.upsert_ai_session_drawing_flag("sess1", True)
    c = app_mod.app.test_client()
    r = _post_polygon(c, **_snap_prov())
    assert r.status_code == 200
    assert "snapshot_attestation" not in r.get_json()["provenance"]
    stored = share_store.get_roi(share_store.ADMIN_TOKEN, 0)
    assert "snapshot_attestation" not in stored["provenance"]


def test_freehand_with_snapshot_id_requires_attestation(monkeypatch):
    """freehand 带 snapshot_id → 同样必须带 attestation；不带 snapshot_id
    的 freehand 维持「溯源可选」既有语义。"""
    _touch()
    _mock_plugin_channel(monkeypatch, valid=True)
    share_store.upsert_ai_session_drawing_flag("sess1", True)
    c = app_mod.app.test_client()
    fh = {"label": "AI 描图", "type": "freehand",
          "points": [[10, 10], [60, 40], [90, 90], [40, 60]],
          "effect_key": "ek-fh-attest", "session_id": "sess1"}
    # 带 snapshot_id 无 attestation → 400
    r1 = c.post("/api/plugin/v1/slides/demo.svs/annotations",
                json=dict(fh, snapshot_id="snap-fh-1"),
                headers={"X-Run-Grant": "g1"})
    assert r1.status_code == 400
    # 合法 attestation → 200
    r2 = c.post("/api/plugin/v1/slides/demo.svs/annotations",
                json=dict(fh, snapshot_id="snap-fh-1",
                          snapshot_attestation=_attest(snap="snap-fh-1",
                                                       bbox=None, rev=None,
                                                       fp=None)),
                headers={"X-Run-Grant": "g1"})
    assert r2.status_code == 200, r2.get_data(as_text=True)
    # 完全不带 snapshot_id 的 freehand：可选语义不变
    r3 = c.post("/api/plugin/v1/slides/demo.svs/annotations", json=fh,
                headers={"X-Run-Grant": "g1"})
    assert r3.status_code == 200


# =========================================================================== #
# P1-4 收口：legacy /internal/ai/annotate 通道同闸（internal token 不再单独
# 放行描绘写入；几何路径回归见 test_ai_drawing_geom）
# =========================================================================== #
def _mock_internal_channel(monkeypatch):
    monkeypatch.setattr(app_mod, "_require_internal", lambda: None)
    monkeypatch.setattr(app_mod, "_demo_public_mode", lambda: False)
    monkeypatch.setattr(app_mod, "_audit", lambda *a, **k: None)
    monkeypatch.setattr(app_mod, "_legacy_slide_revision", lambda safe: "rev0")


def _post_internal_polygon(c, session_id="sess-int-1", **overrides):
    body = {
        "slide": "demo.svs", "label": "AI 描绘", "type": "polygon",
        "points": TRIANGLE, "effect_key": "ek-int-gate",
        "session_id": session_id,
        # P1-3：legacy 通道与 plugin v1 同一溯源要求
        "snapshot_id": "snap-int-1",
        "snapshot_attestation": _attest(sid=session_id, snap="snap-int-1",
                                        bbox=None, rev=None, fp=None),
    }
    body.update(overrides)
    return c.post("/internal/ai/annotate", json=body)


def test_internal_channel_requires_attestation(monkeypatch):
    """legacy 通道：polygon 无 attestation → 400（与 plugin v1 同一强制）。"""
    _touch()
    _mock_internal_channel(monkeypatch)
    share_store.upsert_ai_session_drawing_flag("sess-int-1", True)
    c = app_mod.app.test_client()
    r = _post_internal_polygon(c, snapshot_attestation=None)
    assert r.status_code == 400
    assert "snapshot_attestation" in r.get_json()["error"]
    assert _no_rois()


def test_internal_channel_blocked_without_mirror_row(monkeypatch):
    """internal 通道：镜像无行 → 403 ai_drawing_disabled，不落库。"""
    _touch()
    _mock_internal_channel(monkeypatch)
    c = app_mod.app.test_client()
    r = _post_internal_polygon(c)
    assert r.status_code == 403
    assert r.get_json()["code"] == "ai_drawing_disabled"
    assert _no_rois()


def test_internal_channel_allowed_when_mirror_on(monkeypatch):
    """internal 通道：镜像 true → 200 落库（与 plugin v1 同闸语义）。"""
    _touch()
    _mock_internal_channel(monkeypatch)
    share_store.upsert_ai_session_drawing_flag("sess-int-1", True)
    c = app_mod.app.test_client()
    r = _post_internal_polygon(c)
    assert r.status_code == 200, r.get_data(as_text=True)
    assert r.get_json()["type"] == "polygon"


def test_internal_channel_blocks_late_polygon_after_midrun_off(monkeypatch):
    """internal 通道：运行中关闭（true→false）→ 迟到 polygon 403。"""
    _touch()
    _mock_internal_channel(monkeypatch)
    share_store.upsert_ai_session_drawing_flag("sess-int-1", False)
    c = app_mod.app.test_client()
    r = _post_internal_polygon(c)
    assert r.status_code == 403
    assert _no_rois()


# =========================================================================== #
# C2（2026-09-10）：session detail 带回 drawing_mirror，不在 GET 路径回写镜像
# =========================================================================== #
def test_session_detail_includes_drawing_mirror(monkeypatch):
    """GET /api/ai/session/<id> 2xx 附带 session.drawing_mirror。

    无行 → false；镜像 true → true。HP allow_ai_drawing 仍透传。GET 不把
    HP=true 回写成镜像 true（写入口继续对无行 fail-closed）。
    """
    _mock_proxy_owner(monkeypatch)
    fake = FakeRequests()
    fake.register("GET", "/session/sess1",
                  lambda b, q, h, k: FakeResponse(
                      200, {"session": {"id": "sess1",
                                        "allow_ai_drawing": True},
                            "transcript": []}))
    monkeypatch.setattr(app_mod, "requests", fake)
    c = _browser_client(app_mod.app)

    r = c.get("/api/ai/session/sess1")
    assert r.status_code == 200, r.get_data(as_text=True)
    body = r.get_json()
    assert body["session"]["allow_ai_drawing"] is True
    assert body["session"]["drawing_mirror"] is False  # 无行
    assert share_store.get_ai_session_drawing_flag("sess1") is None

    share_store.upsert_ai_session_drawing_flag("sess1", True)
    r2 = c.get("/api/ai/session/sess1")
    assert r2.status_code == 200
    assert r2.get_json()["session"]["drawing_mirror"] is True
    assert r2.get_json()["session"]["allow_ai_drawing"] is True
