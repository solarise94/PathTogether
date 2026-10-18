# -*- coding: utf-8 -*-
"""AI 描绘的平台写端复核与来源快照溯源（0082 起按 run grant 技能授权）。

覆盖（PT 侧，本文件专属）：
  0082 写端技能闸门（run_grants.allow_ai_drawing 列；契约
  docs/viewer-drawing-skill-contract-20261009.md §4，裁决 3 无过渡窗口）：
    - grant 列缺省 false / 显式 false（/skills 未置位）→ 伪造 polygon 403
      ai_drawing_disabled（fail closed，不落库）——默认拒绝；
    - grant 列 true（HP 判定成立后经 /skills 端点置位）→ polygon 200 落库；
    - run 终态（grant 撤销/过期）→ 迟到 polygon 403，无新 ROI；
    - freehand 同受闸门约束；矩形路径不受影响；
    - 旧会话镜像（ai_session_drawing_flags）true 不再放行写入（无授权效力）。
  /skills 机器端点（0082 §3.2，裁决 5 事务内创建者复查）：
    installation 不符 / 未 bind / session 不符 / 撤销 / 过期 / 创建者失权 /
    白名单外技能 / 幂等重放。
  旧镜像代理路由 /api/ai/session/<sid>/drawing：保持可写、generation/CAS
    机制不变（契约 §4.3），但其镜像值不参与写入授权。
  fix-2026-09-11 创建参数链路：
    - fresh 首发携带 allow_ai_drawing:true → 严格布尔透传 + run 接受后镜像
      true → 伪造 polygon 不再 403；fresh 不带 flag（含显式 false）→ 不透传
      不镜像，闸门回归仍 403；非 fresh 带 true → 400 invalid_argument；非
      布尔（含显式 null）→ 400。
    - 创建成功后关闭描绘，再重放同一 session 的创建接受路径：镜像保持
      false（仅不存在时初始化，禁止 reserve+CAS 把已关闭闸门写回 true）。
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
                         isolate_app, real_json_response, register_slide_row)


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    """每用例隔离 + 统一注入可信切片元数据（1000×800）。"""
    _, up_dir = isolate_app(monkeypatch, tmp_path, UPLOAD_DIR,
                            clear_stores=True)
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
    register_slide_row(name)  # P3：无行兼容分支已删——夹具先注册行（owner NULL，后续可回填归属）
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


def _mock_plugin_channel(monkeypatch, valid=True, drawing=True):
    """插件 v1 通道 mock（token/grant/installation；与 geom 测试同款口径）。

    0082 起闸门只认 grant.allow_ai_drawing：drawing=True/False 直接控列值；
    drawing=None 时 grant dict 不带该键（模拟未置位的历史行形态）。
    """
    monkeypatch.setattr(app_mod, "_require_plugin_token",
                        lambda scope=None: ({"sub": "inst1"}, None))
    grant = {"grant_id": "g1", "slide": "demo.svs", "installation_id": "inst1",
             "created_by_user_id": "u1", "session_id": "sess1"}
    if drawing is not None:
        grant["allow_ai_drawing"] = drawing
    monkeypatch.setattr(
        app_mod, "_verify_run_grant",
        (lambda gid, slide, inst, expect_session=None, slide_id=None: (True, "")) if valid
        else (lambda gid, slide, inst, expect_session=None, slide_id=None: (False, "expired")))
    monkeypatch.setattr(app_mod.share_store, "get_run_grant", lambda gid: grant)
    monkeypatch.setattr(app_mod, "_archived_slide_names", lambda: [])
    monkeypatch.setattr(app_mod.share_store, "get_plugin_installation",
                        lambda iid: {"plugin_id": "histopilot", "version": "0"})
    monkeypatch.setattr(app_mod, "_audit", lambda *a, **k: None)
    monkeypatch.setattr(app_mod, "_ai_write_revision",
                            lambda gate, safe: "rev0")


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
# 0082：写端技能闸门（run grant 的 allow_ai_drawing 列；默认拒绝）
# =========================================================================== #
def test_gate_fail_closed_when_grant_lacks_skill(monkeypatch):
    """默认拒绝：grant 无 allow_ai_drawing（/skills 未置位）→ 伪造 polygon 403。"""
    _touch()
    _mock_plugin_channel(monkeypatch, valid=True, drawing=None)
    c = app_mod.app.test_client()
    r = _post_polygon(c, **_snap_prov())
    assert r.status_code == 403, r.get_data(as_text=True)
    err = r.get_json()["error"]
    assert err["code"] == "ai_drawing_disabled"
    assert err["retryable"] is False
    assert _no_rois()  # 未落库


def test_gate_blocks_forged_polygon_when_flag_false(monkeypatch):
    """grant.allow_ai_drawing 显式 false → 伪造/重放 polygon 403，无新 ROI。"""
    _touch()
    _mock_plugin_channel(monkeypatch, valid=True, drawing=False)
    c = app_mod.app.test_client()
    r = _post_polygon(c, **_snap_prov())
    assert r.status_code == 403
    assert r.get_json()["error"]["code"] == "ai_drawing_disabled"
    assert _no_rois()


def test_gate_allows_polygon_when_skill_granted(monkeypatch):
    """grant 列 true（HP 判定成立 + /skills 置位）→ polygon 200 落库。"""
    _touch()
    _mock_plugin_channel(monkeypatch, valid=True, drawing=True)
    c = app_mod.app.test_client()
    r = _post_polygon(c, **_snap_prov())
    assert r.status_code == 200, r.get_data(as_text=True)
    roi = share_store.get_roi(share_store.ADMIN_TOKEN, 0)
    assert roi is not None and roi["type"] == "polygon"
    assert roi["review_status"] == "pending"


def test_gate_blocks_late_polygon_after_run_end(monkeypatch):
    """迟到写入：run 终态撤销 grant 后 → 迟到 polygon 403 且无新 ROI
    （旧保护「运行中关闭」迁移为「run 终态撤 grant」——同一默认拒绝语义）。"""
    _touch()
    _mock_plugin_channel(monkeypatch, valid=True, drawing=True)
    c = app_mod.app.test_client()
    r1 = _post_polygon(c, effect_key="ek-gate-open", **_snap_prov())
    assert r1.status_code == 200
    first = share_store.get_roi(share_store.ADMIN_TOKEN, 0)
    assert first is not None
    # run 终态：SSE 结束/cancel → 平台撤销 session 的 grant（真实路径是
    # _revoke_run_grants_for_session；这里以校验失败 mock 等价呈现）。
    _mock_plugin_channel(monkeypatch, valid=False, drawing=True)
    r2 = _post_polygon(c, effect_key="ek-gate-late", **_snap_prov())
    assert r2.status_code == 403
    assert r2.get_json()["error"]["code"] == "run_grant_invalid"
    late = share_store.get_roi(share_store.ADMIN_TOKEN, 1)
    assert late is None  # 迟到写入未落库
    assert share_store.get_roi(share_store.ADMIN_TOKEN, 0)["annotation_id"] \
        == first["annotation_id"]


def test_gate_ignores_stale_session_mirror(monkeypatch):
    """旧镜像 true 不再有授权效力：镜像 true + grant 列 false → 仍 403
    （0082 裁决 3：镜像表保留但不参与写入授权）。"""
    _touch()
    _mock_plugin_channel(monkeypatch, valid=True, drawing=False)
    share_store.upsert_ai_session_drawing_flag("sess1", True)  # 旧开关遗留 true
    c = app_mod.app.test_client()
    r = _post_polygon(c, **_snap_prov())
    assert r.status_code == 403
    assert r.get_json()["error"]["code"] == "ai_drawing_disabled"
    assert _no_rois()


def test_gate_covers_freehand_too(monkeypatch):
    """freehand 同受闸门约束（grant 列 false → 403）。"""
    _touch()
    _mock_plugin_channel(monkeypatch, valid=True, drawing=False)
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
    """矩形路径不受闸门影响：grant 列 false 仍 200（create_annotation 通道不变）。"""
    _touch()
    _mock_plugin_channel(monkeypatch, valid=True, drawing=False)
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
    _mock_plugin_channel(monkeypatch, valid=False, drawing=True)
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
    """二轮 review P1-2 回归③（0082 迁移）：代理关闭路径不变（镜像照写），
    迟到写入的拒绝自 0082 起由 grant 列把守——未置位（false）→ 403
    ai_drawing_disabled（默认拒绝保护迁移，不删除断言）。"""
    _mock_proxy_owner(monkeypatch)
    c = _browser_client(app_mod.app)
    share_store.upsert_ai_session_drawing_flag("sess1", True)

    # 关闭时 HP 不可达（响应丢失）——镜像仍已收紧为 false
    fake = _install_fake_sidecar(monkeypatch, status=200,
                                 body={"ok": True, "allow_ai_drawing": False})
    fake.set_unreachable()
    _proxy_toggle(c, enabled=False)
    assert share_store.get_ai_session_drawing_flag("sess1") is False

    # 迟到写入：grant 列未置位 → 写入口拒绝（与 test_gate_blocks_* 同一口径）
    _touch()
    _mock_plugin_channel(monkeypatch, valid=True, drawing=False)
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


# =========================================================================== #
# 回归（2026-10）：_proxy_json 的 on_response 必须按 requests.Response 解析。
# 历史 bug：对真 Response 调 Flask 的 r.get_json(silent=True) → AttributeError
# 被「回调异常兜底」吞掉 → 生产所有 on_response 回调（drawing 镜像 CAS/稳定
# 错误、detail drawing_mirror 回填、cancel/archive 撤 grant）静默失效；而
# FakeResponse 恰好实现了 get_json，把既有用例全部「测绿」。本节用例统一走
# **真实 requests.Response**（real_json_response，无 get_json 方法）。
# =========================================================================== #
def _install_real_response_sidecar(monkeypatch, status=200, body=None,
                                   ctype="application/json", raw=None,
                                   path="/session/sess1/drawing",
                                   method="POST"):
    fake = FakeRequests()
    fake.register(method, path,
                  lambda b, q, h, k: real_json_response(
                      status, body, ctype=ctype, raw=raw))
    monkeypatch.setattr(app_mod, "requests", fake)
    return fake


def test_proxy_mirror_toggle_with_real_requests_response(monkeypatch):
    """真实 requests.Response：HP 200 + 权威布尔 → 镜像照常 CAS（回调确实跑）。"""
    _mock_proxy_owner(monkeypatch)
    c = _browser_client(app_mod.app)
    _install_real_response_sidecar(
        monkeypatch, 200, {"ok": True, "allow_ai_drawing": True})
    assert _proxy_toggle(c, enabled=True).status_code == 200
    assert share_store.get_ai_session_drawing_flag("sess1") is True

    _install_real_response_sidecar(
        monkeypatch, 200, {"ok": True, "allow_ai_drawing": False})
    assert _proxy_toggle(c, enabled=False).status_code == 200
    assert share_store.get_ai_session_drawing_flag("sess1") is False


def test_proxy_cas_failure_override_with_real_requests_response(monkeypatch):
    """真实 requests.Response：CAS 抛错 → 稳定 503 mirror_write_failed
    （第四轮 P2 覆盖路径；修复前回调被 AttributeError 吞掉 → 透传 HP 200
    假成功，浏览器把镜像未知的操作显示为已开启）。"""
    _mock_proxy_owner(monkeypatch)
    c = _browser_client(app_mod.app)
    share_store.upsert_ai_session_drawing_flag("sess1", False)
    _install_real_response_sidecar(
        monkeypatch, 200, {"ok": True, "allow_ai_drawing": True})

    def _boom(sid, val, max_gen):
        raise RuntimeError("pg down")

    monkeypatch.setattr(app_mod.share_store, "cas_ai_session_drawing_flag",
                        _boom)
    r = _proxy_toggle(c, enabled=True)
    assert r.status_code == 503, r.get_data(as_text=True)
    assert r.get_json()["code"] == "mirror_write_failed"
    # 本地写闸仍关（fail closed）
    assert share_store.get_ai_session_drawing_flag("sess1") is False


def test_session_detail_drawing_mirror_with_real_requests_response(monkeypatch):
    """真实 requests.Response：GET detail 2xx 仍回填 session.drawing_mirror
    （C2；修复前回调被 AttributeError 吞掉 → 响应缺 drawing_mirror 字段，
    UI 无法对照提示「平台同步未完成」）。GET 不回写镜像。"""
    _mock_proxy_owner(monkeypatch)
    _install_real_response_sidecar(
        monkeypatch, 200,
        {"session": {"id": "sess1", "allow_ai_drawing": True},
         "transcript": []},
        path="/session/sess1", method="GET")
    c = _browser_client(app_mod.app)

    r = c.get("/api/ai/session/sess1")
    assert r.status_code == 200, r.get_data(as_text=True)
    assert r.get_json()["session"]["allow_ai_drawing"] is True
    assert r.get_json()["session"]["drawing_mirror"] is False  # 无行
    assert share_store.get_ai_session_drawing_flag("sess1") is None

    share_store.upsert_ai_session_drawing_flag("sess1", True)
    r2 = c.get("/api/ai/session/sess1")
    assert r2.status_code == 200
    assert r2.get_json()["session"]["drawing_mirror"] is True
    assert r2.get_json()["session"]["allow_ai_drawing"] is True


def test_response_json_parsing_contract():
    """_response_json 解析口径：JSON body 解析；坏 JSON / 非 JSON
    content-type → None（不抛）；缺 Content-Type 的 JSON body 仍解析；
    SSE 流不解析。全部构造在真实 requests.Response 上。"""
    assert app_mod._response_json(real_json_response(200, {"a": 1})) == {"a": 1}
    assert app_mod._response_json(
        real_json_response(200, raw=b"not-json")) is None
    assert app_mod._response_json(
        real_json_response(200, raw=b"<html/>", ctype="text/html")) is None
    assert app_mod._response_json(
        real_json_response(200, {"a": 1}, ctype=None)) == {"a": 1}
    assert app_mod._response_json(
        real_json_response(200, raw=b"data: x\n\n",
                           ctype="text/event-stream")) is None


def test_proxy_json_callback_exception_swallowed(monkeypatch):
    """回调自身抛错：记 log 不上抛，仍透传 sidecar 响应（异常语义保持）。"""
    _install_real_response_sidecar(
        monkeypatch, 200, {"ok": True, "allow_ai_drawing": True})
    with app_mod.app.test_request_context():
        resp = app_mod._proxy_json("/session/sess1/drawing",
                                   {"enabled": True},
                                   on_response=lambda s, p: 1 / 0)
    assert resp.status_code == 200
    assert resp.get_json() == {"ok": True, "allow_ai_drawing": True}


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


def test_store_init_drawing_flag_does_not_overwrite():
    """创建参数镜像：仅不存在时插入，已有行（含关闭后 false）不覆盖、不抬代。"""
    assert share_store.init_ai_session_drawing_flag("s-init", True) is True
    assert share_store.get_ai_session_drawing_flag("s-init") is True
    assert share_store.get_ai_session_drawing_generation("s-init") == 1
    # 已存在 true：再次 init 不改
    assert share_store.init_ai_session_drawing_flag("s-init", True) is False
    assert share_store.get_ai_session_drawing_flag("s-init") is True
    assert share_store.get_ai_session_drawing_generation("s-init") == 1
    share_store.upsert_ai_session_drawing_flag("s-init", False)
    gen = share_store.get_ai_session_drawing_generation("s-init")
    assert share_store.get_ai_session_drawing_flag("s-init") is False
    # 关闭后再 init true：不得把闸门写回 true，也不得抬 generation
    assert share_store.init_ai_session_drawing_flag("s-init", True) is False
    assert share_store.get_ai_session_drawing_flag("s-init") is False
    assert share_store.get_ai_session_drawing_generation("s-init") == gen


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
    # 迟到描绘仍被拒（0082：grant 列未置位 → 默认拒绝）
    _touch()
    _mock_plugin_channel(monkeypatch, valid=True, drawing=False)
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
# 0082（裁决 2）：legacy /internal/ai/annotate 通道——按 session+slide 查活跃
# 且 allow_ai_drawing=true 的 run grant；internal token 只完成传输鉴权，不构成
# 描绘授权（真实 store 造 grant，覆盖端到端闸门语义）
# =========================================================================== #
def _mock_internal_channel(monkeypatch):
    monkeypatch.setattr(app_mod, "_require_internal", lambda: None)
    monkeypatch.setattr(app_mod, "_demo_public_mode", lambda: False)
    monkeypatch.setattr(app_mod, "_audit", lambda *a, **k: None)
    monkeypatch.setattr(app_mod, "_ai_write_revision",
                        lambda gate, safe: "rev0")


def _disable_grant_drawing(grant_id):
    """把 grant 的描绘列关掉（新 grant 默认开启；此状态目前只在测试中构造）。"""
    import share_store_pg
    conn = share_store_pg._connect()
    try:
        with conn.cursor() as cur:
            cur.execute("UPDATE run_grants SET allow_ai_drawing = FALSE, "
                        "drawing_bound_at = NULL, drawing_intent = '' "
                        "WHERE grant_id = %s", (grant_id,))
        conn.commit()
    finally:
        conn.close()


def _make_internal_drawing_grant(session_id="sess-int-1", flagged=True):
    """真实 store 造一条绑定 session 的 run grant（flagged=False 时关掉描绘列）。"""
    g = share_store.create_run_grant("inst1", "demo.svs", session_id="",
                                     created_by_user_id=None)
    share_store.bind_run_grant_session(g["grant_id"], session_id)
    if not flagged:
        _disable_grant_drawing(g["grant_id"])
    else:
        ok, reason = share_store.set_run_grant_skill_delineate(
            g["grant_id"], session_id, "explicit",
            creator_allowed=lambda gr: True)
        assert ok, reason
    return g


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
    _make_internal_drawing_grant("sess-int-1", flagged=True)
    c = app_mod.app.test_client()
    r = _post_internal_polygon(c, snapshot_attestation=None)
    assert r.status_code == 400
    assert "snapshot_attestation" in r.get_json()["error"]
    assert _no_rois()


def test_internal_channel_blocked_without_active_grant(monkeypatch):
    """internal 通道：无活跃 grant（未判定/未置位）→ 403 ai_drawing_disabled，
    不落库（internal token 不能绕过——默认拒绝）。"""
    _touch()
    _mock_internal_channel(monkeypatch)
    c = app_mod.app.test_client()
    r = _post_internal_polygon(c)
    assert r.status_code == 403
    assert r.get_json()["code"] == "ai_drawing_disabled"
    assert _no_rois()


def test_internal_channel_allowed_when_grant_flagged(monkeypatch):
    """internal 通道：session 有活跃且置位的 grant → 200 落库。"""
    _touch()
    _mock_internal_channel(monkeypatch)
    _make_internal_drawing_grant("sess-int-1", flagged=True)
    c = app_mod.app.test_client()
    r = _post_internal_polygon(c)
    assert r.status_code == 200, r.get_data(as_text=True)
    assert r.get_json()["type"] == "polygon"


def test_internal_channel_blocks_unflagged_grant(monkeypatch):
    """internal 通道：grant 活跃但 allow_ai_drawing=false → 403（列未置位）。"""
    _touch()
    _mock_internal_channel(monkeypatch)
    _make_internal_drawing_grant("sess-int-1", flagged=False)
    c = app_mod.app.test_client()
    r = _post_internal_polygon(c)
    assert r.status_code == 403
    assert r.get_json()["code"] == "ai_drawing_disabled"
    assert _no_rois()


def test_internal_channel_blocks_late_polygon_after_run_end(monkeypatch):
    """internal 通道：run 终态撤 grant → 迟到 polygon 403（迟到写入保护迁移）。"""
    _touch()
    _mock_internal_channel(monkeypatch)
    g = _make_internal_drawing_grant("sess-int-1", flagged=True)
    c = app_mod.app.test_client()
    r1 = _post_internal_polygon(c, effect_key="ek-int-open")
    assert r1.status_code == 200, r1.get_data(as_text=True)
    share_store.revoke_run_grant(g["grant_id"])  # run 终态：撤销绑定 grant
    r2 = _post_internal_polygon(c, effect_key="ek-int-late")
    assert r2.status_code == 403
    assert r2.get_json()["code"] == "ai_drawing_disabled"
    assert share_store.get_roi(share_store.ADMIN_TOKEN, 1) is None  # 无新 ROI


def test_internal_channel_ignores_stale_mirror(monkeypatch):
    """internal 通道：旧镜像 true 不再有授权效力（无活跃 grant 仍 403）。"""
    _touch()
    _mock_internal_channel(monkeypatch)
    share_store.upsert_ai_session_drawing_flag("sess-int-1", True)
    c = app_mod.app.test_client()
    r = _post_internal_polygon(c)
    assert r.status_code == 403
    assert r.get_json()["code"] == "ai_drawing_disabled"
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


# =========================================================================== #
# fix-2026-09-11：创建参数链路——fresh 首发携带 allow_ai_drawing:true 时透传
# HP 并在 run 被接受后写镜像（HP=true ↔ mirror 一致，写端闸门放行）；不带
# flag 行为回归（无行 → 仍 403）；非 fresh / 非布尔在预算预占前 400。
# =========================================================================== #
def _setup_run_env(monkeypatch, name="demo.svs"):
    """run 端到端用例的公共准备：切片 + 平台 AI 凭据 + 假 sidecar。"""
    _touch(name)
    app_mod._save_ai_config({
        "base_url": "http://llm.example/v1",
        "api_key": "sk-run-secret-123456",
        "model": "m-run",
        "api_protocol": "openai",
    })
    fake = FakeRequests()
    monkeypatch.setattr(app_mod, "requests", fake)
    return fake


def _register_run_sse(fake, session_id):
    """POST /run → 2xx SSE + X-AI-Session-ID（= HistoPilot 已接受执行）。"""
    fake.register("POST", "/run", lambda b, q, h, k: FakeResponse(
        200, sse_frames=[b"id: 1\nevent: slide_opened\ndata: {}\n\n"],
        headers={"X-AI-Session-ID": session_id}))


def test_run_create_param_mirrors_drawing_flag(monkeypatch):
    """fresh + allow_ai_drawing:true：严格布尔透传 + 镜像对齐 + 审计带值。
    0082 起镜像只是对照记录——写闸只认 grant 列：列 false 时即便镜像 true
    仍 403；列 true 放行（见尾部双断言）。"""
    fake = _setup_run_env(monkeypatch)
    _register_run_sse(fake, "sess-fresh-1")
    audits = []
    monkeypatch.setattr(app_mod, "_audit",
                        lambda *a, **k: audits.append(k.get("detail")))
    c = _browser_client(app_mod.app)
    r = c.post("/api/ai/run?fresh=1", json={
        "slide": "demo.svs", "task": "看全片", "allow_ai_drawing": True})
    assert r.status_code == 200, r.get_data(as_text=True)
    # 透传：fresh 与 allow_ai_drawing 严格布尔进 sidecar body
    sent = fake.calls[-1]["body"]
    assert sent["fresh"] is True
    assert sent["allow_ai_drawing"] is True
    # 镜像对齐：HP=true ↔ PT mirror=true（不产生 mirror.stale 假警报）
    assert share_store.get_ai_session_drawing_flag("sess-fresh-1") is True
    # 审计：ai.run detail 带 allow_ai_drawing（true 时）
    run_audits = [d for d in audits
                  if isinstance(d, dict) and d.get("mode") == "run"]
    assert run_audits and run_audits[-1]["allow_ai_drawing"] is True
    # 写端闸门只认 grant 列：镜像 true 但列未置位 → 仍 403（默认拒绝不因
    # 镜像放行）
    _mock_plugin_channel(monkeypatch, valid=True, drawing=False)
    prov = dict(SNAP_PROV, snapshot_attestation=_attest(sid="sess-fresh-1"))
    r2 = _post_polygon(c, session_id="sess-fresh-1",
                       effect_key="ek-create-param", **prov)
    assert r2.status_code == 403
    assert r2.get_json()["error"]["code"] == "ai_drawing_disabled"
    assert _no_rois()
    # grant 列 true（HP 判定 + /skills 置位）→ 放行
    _mock_plugin_channel(monkeypatch, valid=True, drawing=True)
    r3 = _post_polygon(c, session_id="sess-fresh-1",
                       effect_key="ek-create-param-2", **prov)
    assert r3.status_code == 200, r3.get_data(as_text=True)
    roi = share_store.get_roi(share_store.ADMIN_TOKEN, 0)
    assert roi is not None and roi["type"] == "polygon"


def test_run_without_flag_keeps_gate_closed(monkeypatch):
    """回归：fresh 不带 flag（含显式 false）→ 不透传、不镜像（无行）；
    伪造 polygon 仍 403（平台写端闸门不变）。"""
    fake = _setup_run_env(monkeypatch)
    _register_run_sse(fake, "sess-plain-1")
    c = _browser_client(app_mod.app)
    r = c.post("/api/ai/run?fresh=1",
               json={"slide": "demo.svs", "task": "看全片"})
    assert r.status_code == 200
    sent = fake.calls[-1]["body"]
    assert sent["fresh"] is True
    assert "allow_ai_drawing" not in sent  # 只在严格 true 时携带该键
    assert share_store.get_ai_session_drawing_flag("sess-plain-1") is None
    # 显式 false 视同缺省：不透传、不镜像
    r2 = c.post("/api/ai/run?fresh=1",
                json={"slide": "demo.svs", "allow_ai_drawing": False})
    assert r2.status_code == 200
    assert "allow_ai_drawing" not in fake.calls[-1]["body"]
    assert share_store.get_ai_session_drawing_flag("sess-plain-1") is None
    # 闸门回归：grant 列未置位 → 伪造 polygon 403、不落库
    _mock_plugin_channel(monkeypatch, valid=True, drawing=False)
    prov = dict(SNAP_PROV, snapshot_attestation=_attest(sid="sess-plain-1"))
    r3 = _post_polygon(c, session_id="sess-plain-1", **prov)
    assert r3.status_code == 403
    assert r3.get_json()["error"]["code"] == "ai_drawing_disabled"
    assert _no_rois()


def test_run_rejects_flag_without_fresh(monkeypatch):
    """非 fresh 带 allow_ai_drawing:true → 400 invalid_argument（改既有会话
    必须走 /drawing 端点，职责不混）；拒绝在转发前，sidecar 零请求。"""
    fake = _setup_run_env(monkeypatch)
    c = _browser_client(app_mod.app)
    r = c.post("/api/ai/run", json={
        "slide": "demo.svs", "task": "继续", "fresh": False,
        "allow_ai_drawing": True})
    assert r.status_code == 400
    body = r.get_json()
    assert body["code"] == "invalid_argument"
    assert fake.calls == []  # 未转发 sidecar
    assert share_store.get_ai_session_drawing_flag("sess1") is None  # 未镜像


def test_run_rejects_non_boolean_flag(monkeypatch):
    """非布尔非缺省 → 400 invalid_argument（严格布尔，不猜 truthy；显式
    null 同样拒绝）；校验先于转发（零副作用）。"""
    fake = _setup_run_env(monkeypatch)
    c = _browser_client(app_mod.app)
    for bad in ("true", 1, None, [True]):
        r = c.post("/api/ai/run?fresh=1",
                   json={"slide": "demo.svs", "allow_ai_drawing": bad})
        assert r.status_code == 400, "bad=%r" % (bad,)
        assert r.get_json()["code"] == "invalid_argument"
    assert fake.calls == []
    assert share_store.get_ai_session_drawing_flag("sess1") is None


def test_run_create_param_replay_does_not_reopen_closed_mirror(monkeypatch):
    """fresh+allow_ai_drawing=true 创建成功后用户关闭描绘，再重放同一 sid
    的创建接受路径：HP 去重不改开关；PT 不得 reserve+CAS 把镜像写回 true。"""
    fake = _setup_run_env(monkeypatch)
    _register_run_sse(fake, "sess-replay-1")
    _mock_proxy_owner(monkeypatch)
    c = _browser_client(app_mod.app)
    r = c.post("/api/ai/run?fresh=1", json={
        "slide": "demo.svs", "task": "看全片", "allow_ai_drawing": True,
        "request_id": "req-draw-replay-1"})
    assert r.status_code == 200, r.get_data(as_text=True)
    assert share_store.get_ai_session_drawing_flag("sess-replay-1") is True

    fake.register("POST", "/session/sess-replay-1/drawing",
                  lambda b, q, h, k: FakeResponse(
                      200, {"ok": True, "allow_ai_drawing": False}))
    r_off = c.post("/api/ai/session/sess-replay-1/drawing",
                   json={"enabled": False})
    assert r_off.status_code == 200, r_off.get_data(as_text=True)
    assert share_store.get_ai_session_drawing_flag("sess-replay-1") is False
    gen_after_close = share_store.get_ai_session_drawing_generation(
        "sess-replay-1")

    # 迟到/去重重放：sidecar 再次接受同一 session（X-AI-Session-ID 相同）
    _register_run_sse(fake, "sess-replay-1")
    r2 = c.post("/api/ai/run?fresh=1", json={
        "slide": "demo.svs", "task": "看全片", "allow_ai_drawing": True,
        "request_id": "req-draw-replay-1"})
    assert r2.status_code == 200, r2.get_data(as_text=True)
    assert share_store.get_ai_session_drawing_flag("sess-replay-1") is False
    assert share_store.get_ai_session_drawing_generation(
        "sess-replay-1") == gen_after_close

    # 迟到描绘写入仍被闸门拒绝（0082：grant 列未置位 → 默认拒绝）
    _mock_plugin_channel(monkeypatch, valid=True, drawing=False)
    prov = dict(SNAP_PROV, snapshot_attestation=_attest(sid="sess-replay-1"))
    r3 = _post_polygon(c, session_id="sess-replay-1",
                       effect_key="ek-replay-closed", **prov)
    assert r3.status_code == 403
    assert r3.get_json()["error"]["code"] == "ai_drawing_disabled"
    assert _no_rois()


# =========================================================================== #
# 0082：/skills 机器端点（裁决 5：同一事务内创建者复查；只 false→true、幂等）
# =========================================================================== #
def _mock_skills_channel(monkeypatch, sub="inst1"):
    monkeypatch.setattr(app_mod, "_require_plugin_token",
                        lambda scope=None: ({"sub": sub}, None))
    monkeypatch.setattr(app_mod, "_audit", lambda *a, **k: None)


def _post_skills(c, grant_id, session_id="sess1", intent="explicit",
                 skills=None):
    return c.post("/api/plugin/v1/run-grants/%s/skills" % grant_id, json={
        "session_id": session_id,
        "skills": skills if skills is not None else ["delineate"],
        "intent": intent,
    })


def _make_bound_grant(session_id="sess1", installation_id="inst1"):
    """/skills 用例：从描绘关闭的 grant 出发（新 grant 默认开启）。"""
    g = share_store.create_run_grant(installation_id, "demo.svs",
                                     session_id="",
                                     created_by_user_id=None)
    share_store.bind_run_grant_session(g["grant_id"], session_id)
    _disable_grant_drawing(g["grant_id"])
    return g


def test_new_run_grant_allows_drawing_by_default():
    """描绘随 grant 默认发放；是否画由模型决定，写权限由 annotate 路径检查。"""
    _touch()
    g = share_store.create_run_grant("inst1", "demo.svs", session_id="",
                                     created_by_user_id=None)
    stored = share_store.get_run_grant(g["grant_id"])
    assert stored["allow_ai_drawing"] is True
    assert stored["drawing_intent"] == "default"
    assert stored["drawing_bound_at"]


def test_skills_endpoint_sets_flag_idempotently(monkeypatch):
    """置位 happy path：200 + 列 true + drawing_intent；重复调用幂等 200。"""
    _touch()
    _mock_skills_channel(monkeypatch)
    g = _make_bound_grant("sess1")
    c = app_mod.app.test_client()
    r = _post_skills(c, g["grant_id"], intent="natural")
    assert r.status_code == 200, r.get_data(as_text=True)
    body = r.get_json()
    assert body["ok"] is True and body["allow_ai_drawing"] is True
    stored = share_store.get_run_grant(g["grant_id"])
    assert stored["allow_ai_drawing"] is True
    assert stored["drawing_intent"] == "natural"
    assert stored["drawing_bound_at"]
    r2 = _post_skills(c, g["grant_id"], intent="explicit")
    assert r2.status_code == 200  # 幂等
    stored2 = share_store.get_run_grant(g["grant_id"])
    assert stored2["allow_ai_drawing"] is True


def test_skills_endpoint_accepts_model_intent(monkeypatch):
    """模型按本轮消息自行描绘时 HP 以 intent=model 置位；来源只做审计。"""
    _touch()
    _mock_skills_channel(monkeypatch)
    g = _make_bound_grant("sess1")
    c = app_mod.app.test_client()
    r = _post_skills(c, g["grant_id"], intent="model")
    assert r.status_code == 200, r.get_data(as_text=True)
    stored = share_store.get_run_grant(g["grant_id"])
    assert stored["allow_ai_drawing"] is True
    assert stored["drawing_intent"] == "model"


def test_skill_provenance_accepts_model_intent():
    prov, err = app_mod._parse_skill_provenance(
        {"skill": "delineate", "skill_intent": "model"})
    assert err is None
    assert prov == {"skill": "delineate", "skill_intent": "model"}
    _, err = app_mod._parse_skill_provenance(
        {"skill": "delineate", "skill_intent": "vibes"})
    assert err


# =========================================================================== #
# viewer-skills：轮廓精修溯源（contour_refine，仅 polygon/freehand 路径）
# =========================================================================== #
def _contour_refine(**overrides):
    """合法 contour_refine（refined=true 全字段；overrides 直接覆盖键）。"""
    obj = {
        "refined": True,
        "model": "sam2.1",
        "score": 0.92,
        "area_ratio": 0.81,
        "reason": None,
        "original_points": TRIANGLE,
    }
    obj.update(overrides)
    return obj


def test_contour_refine_parser_shapes():
    """解析器直测：合法形状清洗 + 每类畸形（含 iff 双向与 bool 坐标拒绝）。"""
    prov, err = app_mod._parse_contour_refine_provenance(
        {"contour_refine": _contour_refine()})
    assert err is None
    assert prov == {"refined": True, "model": "sam2.1", "score": 0.92,
                    "area_ratio": 0.81, "reason": None,
                    "original_points": TRIANGLE}
    # 缺省（不携带）→ 空 dict；显式 null 值键原样保留（缺省键不持久化）。
    prov, err = app_mod._parse_contour_refine_provenance({})
    assert err is None and prov == {}
    prov, err = app_mod._parse_contour_refine_provenance(
        {"contour_refine": _contour_refine(score=None, area_ratio=None,
                                           model=None, reason=None)})
    assert err is None and prov == {"refined": True, "model": None,
                                    "score": None, "area_ratio": None,
                                    "reason": None,
                                    "original_points": TRIANGLE}
    # 畸形清单（一律 err）。
    bad = [
        {"contour_refine": [1, 2]},                              # 非对象
        {"contour_refine": {"model": "sam2.1"}},                 # 缺 refined
        {"contour_refine": {"refined": "true"}},                 # 非 bool
        {"contour_refine": _contour_refine(extra=1)},            # 未知键
        {"contour_refine": _contour_refine(model="m" * 65)},     # model >64
        {"contour_refine": _contour_refine(model=7)},            # model 非 str
        {"contour_refine": _contour_refine(score=True)},         # score bool
        {"contour_refine": _contour_refine(score="0.9")},        # score str
        {"contour_refine": _contour_refine(area_ratio=float("nan"))},   # NaN
        {"contour_refine": _contour_refine(area_ratio=float("inf"))},   # Inf
        {"contour_refine": _contour_refine(reason="r" * 33)},    # reason >32
        {"contour_refine": {k: v for k, v in _contour_refine().items()
                            if k != "original_points"}},         # refined 缺原点列
        {"contour_refine": _contour_refine(original_points=[[1, 1], [2, 2]])},  # <3 点
        {"contour_refine": _contour_refine(
            original_points=[[1, 1], [2, "x"], [3, 3]])},        # 字符串坐标
        {"contour_refine": _contour_refine(
            original_points=[[1, True], [2, 2], [3, 3]])},       # bool 坐标
        {"contour_refine": {"refined": False,
                            "original_points": TRIANGLE}},       # iff 反向
    ]
    for body in bad:
        _, err = app_mod._parse_contour_refine_provenance(body)
        assert err, body


def test_contour_refine_provenance_persisted(monkeypatch):
    """refined=true：清洗后逐键持久化进 ROI provenance（original_points 对照审计）。"""
    _touch()
    _mock_plugin_channel(monkeypatch, valid=True)
    c = app_mod.app.test_client()
    r = _post_polygon(c, **_snap_prov(), contour_refine=_contour_refine())
    assert r.status_code == 200, r.get_data(as_text=True)
    prov = r.get_json()["provenance"]
    assert prov["contour_refine"] == {
        "refined": True, "model": "sam2.1", "score": 0.92,
        "area_ratio": 0.81, "reason": None, "original_points": TRIANGLE,
    }
    stored = share_store.get_roi(share_store.ADMIN_TOKEN, 0)
    assert stored["provenance"]["contour_refine"]["original_points"] == TRIANGLE


def test_contour_refine_provenance_not_refined_persisted(monkeypatch):
    """refined=false：无 original_points；model/score/reason 仍持久化。"""
    _touch()
    _mock_plugin_channel(monkeypatch, valid=True)
    c = app_mod.app.test_client()
    r = _post_polygon(c, **_snap_prov(), contour_refine={
        "refined": False, "model": "sam2.1", "score": 0.21,
        "area_ratio": None, "reason": "low_score",
    })
    assert r.status_code == 200, r.get_data(as_text=True)
    prov = r.get_json()["provenance"]["contour_refine"]
    assert prov == {"refined": False, "model": "sam2.1", "score": 0.21,
                    "area_ratio": None, "reason": "low_score"}
    assert "original_points" not in prov  # iff：refined=false 不携带原点列


def test_contour_refine_malformed_rejected_400(monkeypatch):
    """端到端：畸形 contour_refine → 400 invalid_request，不落库。"""
    _touch()
    _mock_plugin_channel(monkeypatch, valid=True)
    c = app_mod.app.test_client()
    cases = [
        {"refined": True},                                    # 缺 original_points
        {"refined": True, "original_points": [[1, 1], [2, 2]]},  # 点数不足
        {"refined": "yes"},                                   # 非 bool
        {"refined": False, "original_points": TRIANGLE},      # iff 反向
        {"refined": True, "original_points": TRIANGLE, "extra": 1},  # 未知键
        {"refined": True, "original_points": TRIANGLE, "score": "high"},  # str
    ]
    for i, cr in enumerate(cases):
        r = _post_polygon(c, **_snap_prov(), contour_refine=cr,
                          effect_key="ek-crefine-bad-%d" % i)
        assert r.status_code == 400, (cr, r.get_data(as_text=True))
        assert r.get_json()["error"]["code"] == "invalid_request"
        assert "contour_refine" in r.get_json()["error"]["message"]
    assert _no_rois()


def test_contour_refine_ignored_on_rect_path(monkeypatch):
    """矩形路径与 skill 字段同待遇：contour_refine 不解析、静默忽略（200，
    不持久化该键——矩形描绘不经精修）。"""
    _touch()
    _mock_plugin_channel(monkeypatch, valid=True)
    c = app_mod.app.test_client()
    r = c.post("/api/plugin/v1/slides/demo.svs/annotations", json={
        "label": "AI-rect", "x": 5, "y": 5, "width_px": 220, "height_px": 90,
        "effect_key": "ek-rect-crefine", "session_id": "sess1",
        "contour_refine": _contour_refine(),
    }, headers={"X-Run-Grant": "g1"})
    assert r.status_code == 200, r.get_data(as_text=True)
    assert "contour_refine" not in r.get_json()["provenance"]


def test_internal_channel_contour_refine(monkeypatch):
    """legacy internal 通道同解析器：合法持久化；畸形 400 不落库。"""
    _touch()
    _mock_internal_channel(monkeypatch)
    _make_internal_drawing_grant("sess-int-1", flagged=True)
    c = app_mod.app.test_client()
    r = _post_internal_polygon(c, effect_key="ek-int-crefine-1",
                               contour_refine=_contour_refine())
    assert r.status_code == 200, r.get_data(as_text=True)
    assert r.get_json()["provenance"]["contour_refine"]["refined"] is True
    r2 = _post_internal_polygon(c, effect_key="ek-int-crefine-2",
                                contour_refine={"refined": True})
    assert r2.status_code == 400
    assert "contour_refine" in r2.get_json()["error"]

def test_skills_endpoint_installation_mismatch(monkeypatch):
    """身份隔离：installation 不符 → 403，列保持 false（零副作用）。"""
    _touch()
    _mock_skills_channel(monkeypatch, sub="inst-other")
    g = _make_bound_grant("sess1", installation_id="inst1")
    c = app_mod.app.test_client()
    r = _post_skills(c, g["grant_id"])
    assert r.status_code == 403
    err = r.get_json()["error"]
    assert err["code"] == "run_grant_invalid"
    assert err["details"]["reason"] == "installation_mismatch"
    assert share_store.get_run_grant(g["grant_id"])["allow_ai_drawing"] is False


def test_skills_endpoint_requires_bound_session(monkeypatch):
    """未 bind 的 grant（起跑自查阶段形态）→ 403 grant_unbound。"""
    _touch()
    _mock_skills_channel(monkeypatch)
    g = share_store.create_run_grant("inst1", "demo.svs", session_id="",
                                     created_by_user_id=None)
    c = app_mod.app.test_client()
    r = _post_skills(c, g["grant_id"])
    assert r.status_code == 403
    assert r.get_json()["error"]["details"]["reason"] == "grant_unbound"


def test_skills_endpoint_session_mismatch(monkeypatch):
    """身份隔离：grant 绑定 sess-a、请求 sess-b → 403 session_mismatch。"""
    _touch()
    _mock_skills_channel(monkeypatch)
    g = _make_bound_grant("sess-a")
    c = app_mod.app.test_client()
    r = _post_skills(c, g["grant_id"], session_id="sess-b")
    assert r.status_code == 403
    assert r.get_json()["error"]["details"]["reason"] == "session_mismatch"


def test_skills_endpoint_revoked_grant_rejected(monkeypatch):
    """run 终态撤 grant 后的置位请求 → 403 grant_revoked（迟到置位拒绝）。"""
    _touch()
    _mock_skills_channel(monkeypatch)
    g = _make_bound_grant("sess1")
    share_store.revoke_run_grant(g["grant_id"])
    c = app_mod.app.test_client()
    r = _post_skills(c, g["grant_id"])
    assert r.status_code == 403
    assert r.get_json()["error"]["details"]["reason"] == "grant_revoked"


def test_skills_endpoint_creator_recheck_in_tx(monkeypatch):
    """裁决 5：事务内创建者复查——创建者已失写权 → 403 creator_not_allowed，
    置位零副作用（列保持 false）。"""
    _touch()
    _mock_skills_channel(monkeypatch)
    g = _make_bound_grant("sess1")
    monkeypatch.setattr(app_mod, "_run_grant_creator_allowed",
                        lambda grant: False)
    c = app_mod.app.test_client()
    r = _post_skills(c, g["grant_id"])
    assert r.status_code == 403
    assert r.get_json()["error"]["details"]["reason"] == "creator_not_allowed"
    assert share_store.get_run_grant(g["grant_id"])["allow_ai_drawing"] is False


def test_skills_endpoint_creator_callback_error_fail_closed(monkeypatch):
    """复查回调抛异常 → 按不允许 fail-closed（403），无副作用。"""
    _touch()
    _mock_skills_channel(monkeypatch)
    g = _make_bound_grant("sess1")

    def _boom(grant):
        raise RuntimeError("store down")

    monkeypatch.setattr(app_mod, "_run_grant_creator_allowed", _boom)
    c = app_mod.app.test_client()
    r = _post_skills(c, g["grant_id"])
    assert r.status_code == 403
    assert r.get_json()["error"]["details"]["reason"] == "creator_not_allowed"


def test_skills_endpoint_request_validation(monkeypatch):
    """白名单/形态校验：未知技能、坏 intent、缺 session_id、未知 grant。"""
    _touch()
    _mock_skills_channel(monkeypatch)
    g = _make_bound_grant("sess1")
    c = app_mod.app.test_client()
    r = _post_skills(c, g["grant_id"], skills=["summarize"])
    assert r.status_code == 400
    assert r.get_json()["error"]["code"] == "invalid_request"
    r2 = _post_skills(c, g["grant_id"], intent="vibes")
    assert r2.status_code == 400
    r3 = c.post("/api/plugin/v1/run-grants/%s/skills" % g["grant_id"],
                json={"skills": ["delineate"], "intent": "explicit"})
    assert r3.status_code == 400  # 缺 session_id
    r4 = _post_skills(c, "rgr_nonexistent")
    assert r4.status_code == 404
    assert share_store.get_run_grant(g["grant_id"])["allow_ai_drawing"] is False


def test_store_set_skill_delineate_semantics():
    """store 层置位语义：未绑定拒绝、绑定后 0→1、幂等、坏 intent ValueError。"""
    g0 = share_store.create_run_grant("inst1", "demo.svs", session_id="",
                                      created_by_user_id=None)
    ok, reason = share_store.set_run_grant_skill_delineate(
        g0["grant_id"], "sess-x", "explicit", creator_allowed=None)
    assert ok is False and reason == "grant_unbound"
    share_store.bind_run_grant_session(g0["grant_id"], "sess-x")
    ok2, reason2 = share_store.set_run_grant_skill_delineate(
        g0["grant_id"], "sess-x", "explicit", creator_allowed=lambda gr: True)
    assert ok2 is True and reason2 == ""
    ok3, _ = share_store.set_run_grant_skill_delineate(
        g0["grant_id"], "sess-x", "natural", creator_allowed=lambda gr: True)
    assert ok3 is True  # 幂等：已 true 再置 200
    with pytest.raises(ValueError):
        share_store.set_run_grant_skill_delineate(g0["grant_id"], "sess-x",
                                                  "bogus")


# =========================================================================== #
# 0082：run|continue 的技能请求键校验与白名单透传（E1c）
# =========================================================================== #
def test_run_forwards_skill_request(monkeypatch):
    """@描绘 run：requested_skills/skill_intent/dismissed_skills/target 白名单
    重建后透传 sidecar；审计带 skills_requested。"""
    fake = _setup_run_env(monkeypatch)
    _register_run_sse(fake, "sess-skill-1")
    audits = []
    monkeypatch.setattr(app_mod, "_audit",
                        lambda *a, **k: audits.append(k.get("detail")))
    _touch()
    sid = share_store.get_slide_id("demo.svs")
    c = _browser_client(app_mod.app)
    r = c.post("/api/ai/run?fresh=1", json={
        "slide": "demo.svs", "task": "@描绘 帮我圈出当前视野的腺体",
        "requested_skills": ["delineate"], "skill_intent": "explicit",
        "dismissed_skills": [],
        "target": {"slide_id": sid, "kind": "viewport",
                   "bbox": {"x": 0, "y": 0, "w": 800, "h": 600}},
        "request_id": "req-skill-fwd-1"})
    assert r.status_code == 200, r.get_data(as_text=True)
    sent = fake.calls[-1]["body"]
    assert sent["requested_skills"] == ["delineate"]
    assert sent["skill_intent"] == "explicit"
    assert sent["dismissed_skills"] == []
    assert sent["target"]["slide_id"] == sid
    assert sent["target"]["kind"] == "viewport"
    assert sent["target"]["bbox"] == {"x": 0, "y": 0, "w": 800, "h": 600}
    run_audits = [d for d in audits
                  if isinstance(d, dict) and d.get("mode") == "run"]
    assert run_audits and run_audits[-1]["skills_requested"] == ["delineate"]
    assert run_audits[-1]["skill_intent"] == "explicit"


def test_run_rejects_unknown_skill_and_malformed_target(monkeypatch):
    """白名单/形态校验先于预算预占（零副作用拒绝，sidecar 零请求）。"""
    fake = _setup_run_env(monkeypatch)
    _touch()
    sid = share_store.get_slide_id("demo.svs")
    c = _browser_client(app_mod.app)
    r = c.post("/api/ai/run?fresh=1", json={
        "slide": "demo.svs", "task": "总结",
        "requested_skills": ["summarize"]})
    assert r.status_code == 400
    assert r.get_json()["code"] == "invalid_argument"
    r2 = c.post("/api/ai/run?fresh=1", json={
        "slide": "demo.svs", "task": "总结",
        "skill_intent": "vibes"})
    assert r2.status_code == 400
    r3 = c.post("/api/ai/run?fresh=1", json={
        "slide": "demo.svs", "task": "圈",
        "target": {"slide_id": sid, "kind": "viewport"}})  # 缺 bbox
    assert r3.status_code == 400
    assert fake.calls == []


def test_run_target_slide_mismatch_rejected(monkeypatch):
    """跨切片草稿：target.slide_id 指向另一切片 → 400 slide_ref_conflict。"""
    fake = _setup_run_env(monkeypatch)
    _touch("demo.svs")
    _touch("other.svs")
    other_sid = share_store.get_slide_id("other.svs")
    c = _browser_client(app_mod.app)
    r = c.post("/api/ai/run?fresh=1", json={
        "slide": "demo.svs", "task": "圈出",
        "target": {"slide_id": other_sid, "kind": "viewport",
                   "bbox": {"x": 0, "y": 0, "w": 10, "h": 10}}})
    assert r.status_code == 400
    assert r.get_json()["code"] == "slide_ref_conflict"
    assert fake.calls == []


def test_run_target_stale_revision_conflict(monkeypatch):
    """target.asset_revision 过期（快照后切片被替换）→ 409 slide_revision_conflict。"""
    fake = _setup_run_env(monkeypatch)
    _touch()
    sid = share_store.get_slide_id("demo.svs")
    c = _browser_client(app_mod.app)
    r = c.post("/api/ai/run?fresh=1", json={
        "slide": "demo.svs", "task": "圈出",
        "target": {"slide_id": sid, "kind": "viewport",
                   "asset_revision": "rev-stale",
                   "bbox": {"x": 0, "y": 0, "w": 10, "h": 10}}})
    assert r.status_code == 409
    assert r.get_json()["error"] == "slide_revision_conflict"
    assert fake.calls == []


def test_run_by_slide_id_target_asset_revision(monkeypatch):
    """Viewer 的真实形态：run 携带 slide_id + target.asset_revision。
    过期 → 409；与当前一致 → 200 透传（此前该路径 500 UnboundLocalError）。"""
    fake = _setup_run_env(monkeypatch)
    _touch()
    sid = share_store.get_slide_id("demo.svs")
    cur = app_mod._slide_revision(app_mod.slide_store.resolve_slide_id(sid))
    c = _browser_client(app_mod.app)
    base = {"slide": "demo.svs", "slide_id": sid, "task": "这块是什么"}
    target = {"slide_id": sid, "kind": "roi",
              "bbox": {"x": 0, "y": 0, "w": 10, "h": 10}}
    r = c.post("/api/ai/run?fresh=1", json=dict(
        base, target=dict(target, asset_revision="rev-stale")))
    assert r.status_code == 409, r.get_data(as_text=True)
    assert r.get_json()["error"] == "slide_revision_conflict"
    assert fake.calls == []
    _register_run_sse(fake, "sess-sid-rev")
    r2 = c.post("/api/ai/run?fresh=1", json=dict(
        base, target=dict(target, asset_revision=cur)))
    assert r2.status_code == 200, r2.get_data(as_text=True)
    assert fake.calls[-1]["body"]["target"]["asset_revision"] == cur


def test_run_interpret_marker_attachment_passes_validator(monkeypatch):
    """「✨ 加入 AI 对话」（标记工具条；原「让 AI 解读」）的发送链路：插件按
    applyPendingAttachesToBody + viewer.getSkillTarget 冻结出的精确 body 形态
    ——slide_id run + marker 附件（冻结 bbox、annotation_id/revision/type）+
    viewport=标注包围盒 + target(kind=annotation, asset_revision,
    annotation_revision) → 200 透传 fake sidecar（附件与 target 双校验全过）。
    附件 revision / 资产 revision / 标注 revision 过期 → 409 且零 sidecar 调用。"""
    fake = _setup_run_env(monkeypatch)
    _touch()
    sid = share_store.get_slide_id("demo.svs")
    roi = share_store.add_roi(share_store.ADMIN_TOKEN, "demo.svs", "待解读标记",
                              x=100, y=100, w=200, h=100)
    aid = roi["annotation_id"]
    rev = int(roi["revision"])
    cur_asset_rev = app_mod._slide_revision(
        app_mod.slide_store.resolve_slide_id(sid))
    c = _browser_client(app_mod.app)
    bbox = {"x": 100, "y": 100, "w": 200, "h": 100}
    # HP main.js applyPendingAttachesToBody 构建的附件记录（逐键同形）
    attachment = {"kind": "marker", "bbox": dict(bbox), "slide": sid,
                  "annotation_id": aid, "revision": rev, "type": "rect"}
    # HP main.js normalizeSkillTarget 冻结的 target（发送时该标注仍选中）
    target = {"kind": "annotation", "bbox": dict(bbox), "slide_id": sid,
              "asset_revision": cur_asset_rev,
              "annotation_id": aid, "annotation_revision": rev}

    def _post(att_rev=None, tgt=None):
        att = dict(attachment)
        if att_rev is not None:
            att["revision"] = att_rev
        body = {"slide_id": sid, "task": "这个标记区域是什么？请解读。",
                "fresh": True, "request_id": "req_interpret_1",
                "viewport": dict(bbox), "attachments": [att]}
        if tgt is not None:
            body["target"] = tgt
        return c.post("/api/ai/run?fresh=1", json=body)

    # 过期附件 revision → 409 revision_conflict（零 sidecar 调用）
    r = _post(att_rev=rev + 1)
    assert r.status_code == 409, r.get_data(as_text=True)
    assert r.get_json()["error"] == "revision_conflict"
    # 过期资产 revision 的 target → 409 slide_revision_conflict
    r2 = _post(tgt=dict(target, asset_revision="rev-stale"))
    assert r2.status_code == 409, r2.get_data(as_text=True)
    assert r2.get_json()["error"] == "slide_revision_conflict"
    # 过期标注 revision 的 target → 409 revision_conflict
    r3 = _post(tgt=dict(target, annotation_revision=rev + 3))
    assert r3.status_code == 409, r3.get_data(as_text=True)
    assert fake.calls == []

    # 全新冻结 → 200；附件与 target 白名单重建后原样透传
    _register_run_sse(fake, "sess-interpret-1")
    r4 = _post(tgt=dict(target))
    assert r4.status_code == 200, r4.get_data(as_text=True)
    sent = fake.calls[-1]["body"]
    atts = sent["attachments"]
    assert len(atts) == 1
    assert atts[0]["kind"] == "marker"
    assert atts[0]["annotation_id"] == aid
    assert atts[0]["revision"] == rev
    assert atts[0]["bbox"] == bbox
    assert sent["viewport"] == bbox
    assert sent["target"]["annotation_id"] == aid
    assert sent["target"]["annotation_revision"] == rev
    assert sent["target"]["asset_revision"] == cur_asset_rev
    assert sent["task"] == "这个标记区域是什么？请解读。"


def test_run_viewport_attachment_clean_record(monkeypatch):
    """回归：viewport 附件（composer「添加视野」路径）此前在
    _validate_ai_attachments 里因 aid/roi 未定义 500 NameError——现在干净
    重建（无 marker 专属键）并 200 透传。"""
    fake = _setup_run_env(monkeypatch)
    _touch()
    c = _browser_client(app_mod.app)
    _register_run_sse(fake, "sess-att-vp")
    bbox = {"x": 10, "y": 20, "w": 500, "h": 400}
    r = c.post("/api/ai/run?fresh=1", json={
        "slide": "demo.svs", "task": "看这个视野",
        "attachments": [{"kind": "viewport", "bbox": dict(bbox),
                         "slide": "demo.svs"}]})
    assert r.status_code == 200, r.get_data(as_text=True)
    atts = fake.calls[-1]["body"]["attachments"]
    assert atts == [{"kind": "viewport", "bbox": bbox, "slide": "demo.svs"}]
    # AI 入口统一（2026-10-18）：附件与视野分列——网关不再把附件 bbox 冒充
    # viewport（真实视野由 UI 实时查询独立携带；不带即不透传）
    assert "viewport" not in fake.calls[-1]["body"]


def test_run_target_annotation_kind_validated(monkeypatch):
    """kind=annotation：存在/归属/revision 匹配；不符 409，合法则白名单透传。"""
    fake = _setup_run_env(monkeypatch)
    _touch()
    sid = share_store.get_slide_id("demo.svs")
    roi = share_store.add_roi(share_store.ADMIN_TOKEN, "demo.svs", "目标区",
                              x=10, y=10, w=50, h=50)
    c = _browser_client(app_mod.app)
    base = {"slide": "demo.svs", "task": "圈出这个标记附近的范围"}
    r = c.post("/api/ai/run?fresh=1", json=dict(base, target={
        "slide_id": sid, "kind": "annotation",
        "annotation_id": roi["annotation_id"],
        "annotation_revision": int(roi["revision"]) + 5}))
    assert r.status_code == 409
    assert r.get_json()["error"] == "revision_conflict"
    r2 = c.post("/api/ai/run?fresh=1", json=dict(base, target={
        "slide_id": sid, "kind": "annotation",
        "annotation_id": "ann_missing", "annotation_revision": 1}))
    assert r2.status_code == 404
    assert fake.calls == []
    _register_run_sse(fake, "sess-skill-2")
    r3 = c.post("/api/ai/run?fresh=1", json=dict(base, target={
        "slide_id": sid, "kind": "annotation",
        "annotation_id": roi["annotation_id"],
        "annotation_revision": int(roi["revision"])}))
    assert r3.status_code == 200, r3.get_data(as_text=True)
    sent = fake.calls[-1]["body"]["target"]
    assert sent["annotation_id"] == roi["annotation_id"]
    assert sent["annotation_revision"] == int(roi["revision"])


def test_continue_forwards_skill_request(monkeypatch):
    """continue 的新 grant 重新判定（裁决 6）：技能键同样校验并透传。"""
    fake = _setup_run_env(monkeypatch)
    _mock_proxy_owner(monkeypatch)
    fake.register("POST", "/continue", lambda b, q, h, k: FakeResponse(
        200, sse_frames=[b"id: 1\nevent: slide_opened\ndata: {}\n\n"],
        headers={"X-AI-Session-ID": "sess-cont-1"}))
    _touch()
    sid = share_store.get_slide_id("demo.svs")
    c = _browser_client(app_mod.app)
    r = c.post("/api/ai/continue", json={
        "slide": "demo.svs", "session_id": "sess-cont-1",
        "task": "帮我把选框里的坏死区域圈出来",
        "requested_skills": ["delineate"], "skill_intent": "natural",
        "target": {"slide_id": sid, "kind": "roi",
                   "bbox": {"x": 100, "y": 200, "w": 800, "h": 600}}})
    assert r.status_code == 200, r.get_data(as_text=True)
    sent = fake.calls[-1]["body"]
    assert sent["requested_skills"] == ["delineate"]
    assert sent["skill_intent"] == "natural"
    assert sent["target"]["kind"] == "roi"


# =========================================================================== #
# 主代理 review 修复4/5：target 校验顺序（不泄露外部 slide_id 存在性）与
# annotation 归属的 slide_id 优先比较（ID-only 资产名可能为空/别名不同）
# =========================================================================== #
def test_target_unknown_slide_id_no_existence_leak(monkeypatch):
    """修复4：run 已知 slide_id 时先比较——target.slide_id 指向不存在资产
    也返回 400 slide_ref_conflict（不解析、不泄露存在性 404）。"""
    fake = _setup_run_env(monkeypatch)
    _touch()
    sid = share_store.get_slide_id("demo.svs")
    c = _browser_client(app_mod.app)
    r = c.post("/api/ai/run?fresh=1", json={
        "slide_id": sid, "task": "圈出",
        "target": {"slide_id": "sld_does_not_exist", "kind": "viewport",
                   "bbox": {"x": 0, "y": 0, "w": 10, "h": 10}}})
    assert r.status_code == 400
    assert r.get_json()["code"] == "slide_ref_conflict"
    assert fake.calls == []


def test_target_other_slide_id_conflict_not_404(monkeypatch):
    """修复4：target.slide_id 指向另一切片（存在）→ 400 slide_ref_conflict
    （与 404 存在性泄露区分；两路径同码，存在性不再可区分）。"""
    fake = _setup_run_env(monkeypatch)
    _touch("demo.svs")
    _touch("other.svs")
    demo_sid = share_store.get_slide_id("demo.svs")
    other_sid = share_store.get_slide_id("other.svs")
    c = _browser_client(app_mod.app)
    r = c.post("/api/ai/run?fresh=1", json={
        "slide_id": demo_sid, "task": "圈出",
        "target": {"slide_id": other_sid, "kind": "viewport",
                   "bbox": {"x": 0, "y": 0, "w": 10, "h": 10}}})
    assert r.status_code == 400
    assert r.get_json()["code"] == "slide_ref_conflict"
    assert fake.calls == []


def test_target_annotation_id_only_asset_by_slide_id(monkeypatch):
    """修复5：roi.slide_id 与本 run slide_id 一致、slide 名为旧别名/空 →
    通过（ID-only/迁移资产不因名不同被误拒）。"""
    fake = _setup_run_env(monkeypatch)
    _touch()
    sid = share_store.get_slide_id("demo.svs")
    roi_dict = {"annotation_id": "ann_idonly", "slide": "legacy-alias.svs",
                "slide_id": sid, "revision": 2, "deleted": False}
    monkeypatch.setattr(app_mod.share_store, "get_roi_by_annotation_id",
                        lambda aid: dict(roi_dict) if aid == "ann_idonly" else None)
    monkeypatch.setattr(app_mod.annotation_access, "can_read_annotation",
                        lambda subject, roi: True)
    c = _browser_client(app_mod.app)
    _register_run_sse(fake, "sess-fix5-ok")
    r = c.post("/api/ai/run?fresh=1", json={
        "slide_id": sid, "task": "圈出这个标记附近",
        "target": {"slide_id": sid, "kind": "annotation",
                   "annotation_id": "ann_idonly", "annotation_revision": 2}})
    assert r.status_code == 200, r.get_data(as_text=True)
    sent = fake.calls[-1]["body"]["target"]
    assert sent["annotation_id"] == "ann_idonly"


def test_target_annotation_slide_id_mismatch_rejected(monkeypatch):
    """修复5 反例：roi.slide_id 与 run slide_id 不符 → 403（ID 优先判定，
    即便 roi.slide 名恰好等于 run 名也不放行）。"""
    fake = _setup_run_env(monkeypatch)
    _touch()
    sid = share_store.get_slide_id("demo.svs")
    other_sid = share_store.get_slide_id("demo.svs") + "x"
    roi_dict = {"annotation_id": "ann_other", "slide": "demo.svs",
                "slide_id": other_sid, "revision": 1, "deleted": False}
    monkeypatch.setattr(app_mod.share_store, "get_roi_by_annotation_id",
                        lambda aid: dict(roi_dict) if aid == "ann_other" else None)
    c = _browser_client(app_mod.app)
    r = c.post("/api/ai/run?fresh=1", json={
        "slide_id": sid, "task": "圈出这个",
        "target": {"slide_id": sid, "kind": "annotation",
                   "annotation_id": "ann_other", "annotation_revision": 1}})
    assert r.status_code == 403
    assert fake.calls == []
