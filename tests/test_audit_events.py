# -*- coding: utf-8 -*-
"""Stage 3c-2 测试（docs §5.3/§6.4/§v1.5）。

覆盖：
  1. 审计日志：record/list 分页/action 过滤/owner-only；脱敏检查（detail 无
     api_key / 明文密码字段）。
  2. 正式事件 cursor：/api/annotations/changes 的 cursor 前进 / tombstone /
     reset_required 触发路径。
  3. AI provenance：完整写入 + 历史 AI partial 标记 + effect_key 重试幂等
     （返回原 annotation_id 且 provenance 不变）。
  4. archived 项目只读：can_annotate/can_upload 对归档切片 False；写 403；
     解档恢复；旧数据无字段默认 false 兼容。
  5. 分享访问日志：/s/<token> 记 share.access，5 分钟窗口去重。

json/pg 双跑（pg 由 RUN_PG_TESTS=1 conftest 起库 + autouse TRUNCATE 隔离）。
"""
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402

TMP = tempfile.mkdtemp(prefix="svs-audit-")
DATA_DIR = os.path.join(TMP, "share-data")
UPLOAD_DIR = os.path.join(TMP, "uploads")
os.environ["SHARE_DATA_DIR"] = DATA_DIR
os.environ["UPLOAD_DIR"] = UPLOAD_DIR
os.environ["AI_INTERNAL_TOKEN"] = "test-internal-token"
os.makedirs(DATA_DIR, exist_ok=True)
os.makedirs(UPLOAD_DIR, exist_ok=True)
os.environ["ADMIN_PASSWORD"] = ""  # 默认无 owner 引导；各用例手动建用户

# openslide 未安装时 stub（多数 API 测试无需真 OpenSlide）
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

import share_store  # noqa: E402
import user_store  # noqa: E402
import app as app_mod  # noqa: E402
from _pt_helpers import csrf_client, install_json_login_limits  # noqa: E402
import share_server as share_srv  # noqa: E402

app_mod.UPLOAD_DIR = Path(UPLOAD_DIR)
app_mod.UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
share_srv.UPLOAD_DIR = Path(UPLOAD_DIR)


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    """每用例前把常量 / env 指回本模块临时目录，清空 users.json / shares.json。"""
    data_dir = Path(DATA_DIR)
    up_dir = Path(UPLOAD_DIR)
    monkeypatch.setenv("SHARE_DATA_DIR", DATA_DIR)
    monkeypatch.setenv("AI_INTERNAL_TOKEN", "test-internal-token")
    monkeypatch.setattr(user_store, "SHARE_DATA_DIR", data_dir)
    monkeypatch.setattr(user_store, "USER_FILE", data_dir / "users.json")
    monkeypatch.setattr(share_store, "SHARE_DATA_DIR", data_dir)
    monkeypatch.setattr(share_store, "SHARE_FILE", data_dir / "shares.json")
    monkeypatch.setattr(app_mod, "UPLOAD_DIR", up_dir)
    monkeypatch.setattr(share_srv, "UPLOAD_DIR", up_dir)
    monkeypatch.setattr(app_mod, "AI_INTERNAL_TOKEN", "test-internal-token")
    share_store.set_owner_user_id("")
    install_json_login_limits(monkeypatch)
    # 重置分享访问日志去重窗口
    share_srv._share_access_last.clear()
    for name in ("users.json", "shares.json", "users.json.bak", "shares.json.bak"):
        p = data_dir / name
        if p.exists():
            p.unlink()
    for child in up_dir.iterdir():
        if child.is_file():
            child.unlink()
    yield


# --------------------------------------------------------------------------- #
# 辅助
# --------------------------------------------------------------------------- #
def _client():
    app_mod.app.config["TESTING"] = True
    app_mod.AUTH_ENABLED = True
    return csrf_client(app_mod.app.test_client())


def _client_noauth():
    app_mod.app.config["TESTING"] = True
    app_mod.AUTH_ENABLED = False
    return csrf_client(app_mod.app.test_client())


def _login(client, login_id, password):
    return client.post("/login", data={"username": login_id, "password": password})


def _touch(name="demo.svs"):
    p = Path(UPLOAD_DIR) / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"svs-stub")
    return name


def _setup_users():
    owner = user_store.create_user("owner@x.com", "ownerpass123456", role="owner")
    userA = user_store.create_user("a@x.com", "userApass123456", role="user")
    share_store.set_owner_user_id(owner["user_id"])
    return owner, userA


def _int_token():
    return {"X-AI-Internal-Token": "test-internal-token"}


# =========================================================================== #
# 1. 审计日志
# =========================================================================== #
def test_audit_record_list_filter_owner_only():
    owner, userA = _setup_users()
    _touch()
    c = _client()
    _login(c, "owner@x.com", "ownerpass123456")
    # 触发一些审计事件
    share_store.record_audit("share.create", actor_user_id=owner["user_id"],
                             actor_role="owner", target_type="share",
                             target_id="tok1", detail={"slide_count": 1})
    share_store.record_audit("user.disable", actor_user_id=owner["user_id"],
                             actor_role="owner", target_type="user",
                             target_id="usr_x", detail={})
    # owner 可读
    r = c.get("/api/admin/audit")
    assert r.status_code == 200
    body = r.get_json()
    assert body["limit"] == 50
    assert len(body["events"]) == 2
    assert body["events"][0]["action"] == "user.disable"  # 最新在前
    # action 过滤
    r = c.get("/api/admin/audit?action=share.create")
    evs = r.get_json()["events"]
    assert len(evs) == 1 and evs[0]["action"] == "share.create"
    # 分页
    r = c.get("/api/admin/audit?limit=1&offset=1")
    assert len(r.get_json()["events"]) == 1
    # 非 owner 403
    ca = _client()
    _login(ca, "a@x.com", "userApass123456")
    assert ca.get("/api/admin/audit").status_code == 403


def test_audit_redaction_no_secrets():
    owner, _ = _setup_users()
    _touch()
    c = _client()
    _login(c, "owner@x.com", "ownerpass123456")
    # AI 起跑 + 分享（经 AI 标注不落 key 到 audit，只用 detail 空）
    share_store.record_audit("ai.run", actor_role="owner", slide="demo.svs",
                             detail={"mode": "run"})
    share_store.record_audit("share.create", actor_role="owner", target_type="share",
                             target_id="tok_x", detail={"slide_count": 1})
    # 全量扫 detail，断言不含敏感串
    r = c.get("/api/admin/audit?limit=500")
    for ev in r.get_json()["events"]:
        d = ev.get("detail") or {}
        for k, v in d.items():
            assert "api_key" not in str(k).lower()
            assert "secret" not in str(k).lower()
            assert "password" not in str(k).lower()
            assert "token" not in str(k).lower()
            if isinstance(v, str):
                assert "api_key" not in v.lower()
                assert "password" not in v.lower()


# =========================================================================== #
# 2. 正式事件 cursor
# =========================================================================== #
def test_changes_cursor_advance_and_tombstone():
    owner, _ = _setup_users()
    _touch()
    c = _client()
    _login(c, "owner@x.com", "ownerpass123456")
    # 添加两条标注
    r1 = share_store.add_roi(share_store.ADMIN_TOKEN, "demo.svs", "L",
                             type="rect", x=0, y=0, side_px=10, size_mm=6.0)
    a1 = r1["annotation_id"]
    r2 = share_store.add_roi(share_store.ADMIN_TOKEN, "demo.svs", "L",
                             type="rect", x=1, y=1, side_px=10, size_mm=6.0)
    # cursor 前进
    resp = c.get("/api/annotations/changes?slide=demo.svs&after=0")
    assert resp.status_code == 200
    b = resp.get_json()
    assert b["cursor"] >= 2
    assert len(b["changes"]) == 2
    assert b["reset_required"] is False
    after = b["cursor"]
    # 删除第一条 → tombstone 出现在流里
    share_store.delete_roi(share_store.ADMIN_TOKEN, 0)
    resp = c.get("/api/annotations/changes?slide=demo.svs&after=%s" % after)
    b = resp.get_json()
    assert len(b["changes"]) == 1
    assert b["changes"][0]["deleted"] is True
    assert b["changes"][0]["annotation_id"] == a1
    assert b["cursor"] >= after


def test_changes_reset_required_after_ahead():
    owner, _ = _setup_users()
    _touch()
    c = _client()
    _login(c, "owner@x.com", "ownerpass123456")
    share_store.add_roi(share_store.ADMIN_TOKEN, "demo.svs", "L",
                        type="rect", x=0, y=0, side_px=10, size_mm=6.0)
    # after 远超当前水位 → reset_required=True
    resp = c.get("/api/annotations/changes?slide=demo.svs&after=999999")
    b = resp.get_json()
    assert b["reset_required"] is True
    assert b["changes"] == []
    # 合法 after → 不 reset
    resp = c.get("/api/annotations/changes?slide=demo.svs&after=0")
    assert resp.get_json()["reset_required"] is False
    # 无权限用户 → 403
    ca = _client()
    _login(ca, "a@x.com", "userApass123456")
    share_store.set_slide_meta("demo.svs", owner_user_id="someone-else")
    assert ca.get("/api/annotations/changes?slide=demo.svs&after=0").status_code == 403


# =========================================================================== #
# 3. AI provenance
# =========================================================================== #
def test_ai_provenance_written_and_partial_marker():
    owner, _ = _setup_users()
    _touch()
    c = _client()
    _login(c, "owner@x.com", "ownerpass123456")
    # 历史 AI 标注（无 provenance）→ 变更流(_roi_out)输出 partial 标记
    old = share_store.add_roi(share_store.ADMIN_TOKEN, "demo.svs", "AI",
                              type="rect", x=0, y=0, side_px=10, size_mm=6.0,
                              source="ai")
    changes = share_store.list_changes("demo.svs", 0)
    partial = next(r for r in changes if r["annotation_id"] == old["annotation_id"])
    assert partial["provenance"] == {"partial": True}
    # 通过 internal annotate 写入带 provenance
    r = c.post("/internal/ai/annotate", json={
        "slide": "demo.svs", "label": "AI", "x": 5, "y": 6, "side_px": 10,
        "effect_key": "ek-1", "session_id": "sess-1",
        "plugin_id": "histopilot", "plugin_version": "0.9.0",
        "run_id": "run-1", "model": "gpt-4o", "base_url": "https://api.openai.com/v1",
        "created_by_user_id": owner["user_id"],
    }, headers=_int_token())
    assert r.status_code == 200
    roi = r.get_json()
    prov = roi["provenance"]
    assert prov["plugin_id"] == "histopilot"
    assert prov["plugin_version"] == "0.9.0"
    assert prov["run_id"] == "run-1"
    assert prov["model"] == "gpt-4o"
    assert prov["provider"] == "api.openai.com"  # 只记 host，不记全 URL/key
    assert prov["created_by_user_id"] == owner["user_id"]
    assert prov["idempotency_key"] == "ek-1"
    assert prov["slide_asset_revision"]  # 非空
    # 再读一条确认持久化
    fetched = share_store.get_roi_by_annotation_id(roi["annotation_id"])
    assert fetched["provenance"]["model"] == "gpt-4o"


def test_ai_provenance_effect_key_idempotent():
    owner, _ = _setup_users()
    _touch()
    c = _client()
    _login(c, "owner@x.com", "ownerpass123456")
    body = {
        "slide": "demo.svs", "label": "AI", "x": 5, "y": 6, "side_px": 10,
        "effect_key": "ek-same", "session_id": "sess-1",
        "plugin_id": "histopilot", "plugin_version": "0.9.0",
        "run_id": "run-1", "model": "gpt-4o",
    }
    r1 = c.post("/internal/ai/annotate", json=body, headers=_int_token()).get_json()
    r2 = c.post("/internal/ai/annotate", json=body, headers=_int_token()).get_json()
    # 同 effect_key 重试 → 返回原 annotation_id 且 provenance 不变
    assert r1["annotation_id"] == r2["annotation_id"]
    assert r1["provenance"]["idempotency_key"] == r2["provenance"]["idempotency_key"]
    assert r1["provenance"]["model"] == r2["provenance"]["model"]
    # 未重复写：只有一条
    rois = share_store.list_rois(share_store.ADMIN_TOKEN)
    assert len(rois) == 1


def test_ai_annotate_revision_conflict_409():
    owner, _ = _setup_users()
    _touch()
    c = _client()
    _login(c, "owner@x.com", "ownerpass123456")
    body = {
        "slide": "demo.svs", "label": "AI", "x": 5, "y": 6, "side_px": 10,
        "effect_key": "ek-rev", "expected_asset_revision": "999:123",
    }
    r = c.post("/internal/ai/annotate", json=body, headers=_int_token())
    assert r.status_code == 409
    assert r.get_json()["error"] == "slide_revision_conflict"
    # 不带 expected_asset_revision → 兼容不强制
    body.pop("expected_asset_revision")
    r2 = c.post("/internal/ai/annotate", json=body, headers=_int_token())
    assert r2.status_code == 200


# =========================================================================== #
# 4. archived 只读
# =========================================================================== #
def test_archived_project_write_protected():
    owner, userA = _setup_users()
    _touch()
    c = _client()
    _login(c, "owner@x.com", "ownerpass123456")
    proj = share_store.create_project("P", slides=["demo.svs"], owner_user_id=owner["user_id"])
    pid = proj["pid"]
    # 归档
    r = c.post("/api/project/%s/archive" % pid)
    assert r.status_code == 200
    assert r.get_json()["archived"] is True
    # 标注被拒（owner 在归档项目内也不可写）
    r = c.post("/api/annotation", json={
        "slide": "demo.svs", "type": "rect", "label": "L", "x": 0, "y": 0,
        "side_px": 100, "size_mm": 6.0,
    })
    assert r.status_code == 403
    # can_annotate_slide / can_upload 对归档切片 False（在请求上下文内判定）
    with app_mod.app.test_request_context():
        assert app_mod.can_annotate_slide("demo.svs") is False
        assert app_mod.can_upload("demo.svs") is False
    # 解档恢复
    r = c.post("/api/project/%s/unarchive" % pid)
    assert r.status_code == 200
    assert r.get_json()["archived"] is False
    with app_mod.app.test_request_context():
        assert app_mod.can_annotate_slide("demo.svs") is True
    r = c.post("/api/annotation", json={
        "slide": "demo.svs", "type": "rect", "label": "L", "x": 0, "y": 0,
        "side_px": 100, "size_mm": 6.0,
    })
    assert r.status_code == 200


def test_archived_default_false_compat():
    # 旧项目数据无 archived 字段 → 默认 false（未归档）
    _setup_users()
    _touch()
    proj = share_store.create_project("Old", slides=["demo.svs"])
    # 直接构造一个无 archived 字段的旧项目并落盘（模拟旧数据）
    p = share_store.get_project(proj["pid"])
    p.pop("archived", None)
    share_store.record_audit("x", detail={})  # 触发一次落盘载入迁移
    fetched = share_store.get_project(proj["pid"])
    assert fetched["archived"] is False


# =========================================================================== #
# 5. 分享访问日志
# =========================================================================== #
def test_share_access_log_dedup():
    owner, _ = _setup_users()
    _touch()
    c = _client()
    _login(c, "owner@x.com", "ownerpass123456")
    # 建分享
    r = c.post("/api/share/create", json={"slides": ["demo.svs"], "expires_hours": 1})
    token = r.get_json()["token"]
    # 访问分享页（share_server 的 client）
    sc = share_srv.app.test_client()
    sc.set_cookie("svs_visitor", share_srv._sign_visitor("visitor-1"),
                  domain="localhost", path="/s")
    resp = sc.get("/s/%s" % token)
    assert resp.status_code == 200
    # 连续两次同 token+visitor → 5 分钟窗口内去重只记一条
    sc.get("/s/%s" % token)
    evs = c.get("/api/admin/audit?action=share.access").get_json()["events"]
    assert len(evs) == 1
    d = evs[0]["detail"]
    assert d["visitor"]  # 有 visitor 前段
    assert "*" in d["ip"]  # IP 已脱敏
