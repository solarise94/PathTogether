# -*- coding: utf-8 -*-
"""S5 路径回放代理 + `_require_ai_session_owner` 四端点越权补齐测试。

覆盖（test-review-and-optimization P1-7 + session-isolation-fix-plan S5）：
  - GET /api/ai/session/<id>/path 代理：属主 200、透传 after_seq/limit、
    user B 读 A 的 path → 403（sidecar 只收到归属查询，无 path 转发）；
    archived session 属主仍可读（S5 契约：owner 归属判定不看 archived 位）；
  - `_require_ai_session_owner` 挂载的 session 详情 / stream / archive /
    unarchive 四处补 user B→A 403（此前只有 cancel 有越权用例）。

Fake sidecar（FakeRequests）替换 app.requests，无需真 HistoPilot 服务；
path 投影端点字段宽松透传（`{waypoints, next_after_seq}` 形态即可）。
运行：cd 项目根 && python3 -m pytest tests/test_ai_session_owner.py -q
"""
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import _bootstrap  # noqa: E402,F401  # session 目录+openslide stub（conftest 先行）
DATA_DIR = _bootstrap.SHARE_DATA_DIR
UPLOAD_DIR = _bootstrap.UPLOAD_DIR
import pytest  # noqa: E402

import app as app_mod  # noqa: E402
import share_store  # noqa: E402
import user_store  # noqa: E402
from _pt_helpers import csrf_client, install_json_login_limits, isolate_app, FakeRequests # noqa: E402


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    _, up_dir = isolate_app(monkeypatch, tmp_path, UPLOAD_DIR,
                            login_limits=True)
    monkeypatch.setattr(app_mod, "AUTH_ENABLED", True)
    for child in up_dir.iterdir():
        if child.is_file():
            child.unlink()
    yield


# --------------------------------------------------------------------------- #
# Fake sidecar：requests 兼容（普通 JSON + SSE 两种形态）
# --------------------------------------------------------------------------- #


@pytest.fixture()
def fake_sidecar(monkeypatch):
    fake = FakeRequests()
    monkeypatch.setattr(app_mod, "requests", fake)
    return fake


def _client():
    app_mod.app.config["TESTING"] = True
    return csrf_client(app_mod.app.test_client())


def _login(client, user):
    with client.session_transaction() as s:
        s["auth_user"] = user.get("login_id") or user.get("user_id")
        s["user_id"] = user["user_id"]
        s["role"] = user.get("role") or "user"
        s["auth_version"] = user.get("auth_version", 1)
    return client


def _setup():
    owner = user_store.create_user("owner@x.com", "ownerpass123456", role="owner")
    usera = user_store.create_user("a@x.com", "userApass123456", role="user")
    userb = user_store.create_user("b@x.com", "userBpass123456", role="user")
    share_store.set_owner_user_id(owner["user_id"])
    slide = "coop.svs"
    p = Path(UPLOAD_DIR) / slide
    p.write_bytes(b"svs-stub")
    share_store.set_slide_meta(slide, owner_user_id=usera["user_id"])
    # A 建分享（view+annotate），B 认领 → B 有该切片 annotate 权限但不是会话属主
    share = share_store.create_share([slide], 24,
                                     permissions=["view", "annotate"],
                                     creator_user_id=usera["user_id"])
    share_store.claim_share(share["token"], userb["user_id"])
    return owner, usera, userb, slide


WAYPOINTS = {
    "waypoints": [
        {"seq": 3, "x": 100, "y": 200, "reason": "inspect",
         "bbox_level0": [90, 190, 20, 20], "level": 2,
         "magnification": "10x", "captured_at": 1750000000},
        {"seq": 7, "x": 400, "y": 500, "reason": "goto",
         "bbox_level0": [390, 490, 20, 20], "level": 1,
         "magnification": "20x", "captured_at": 1750000100},
    ],
    "next_after_seq": 7,
}


# --------------------------------------------------------------------------- #
# S5：GET /api/ai/session/<id>/path 代理
# --------------------------------------------------------------------------- #
def test_path_owner_200_and_query_passthrough(fake_sidecar):
    """属主 200：waypoints 透传 + after_seq/limit 原样转发到 sidecar。"""
    owner, usera, _b, slide = _setup()
    fake = fake_sidecar
    fake.register_json("GET", "/session/sess-a", body={
        "session": {"owner": usera["user_id"], "slide": slide,
                    "archived": True}})
    fake.register_json("GET", "/session/sess-a/path", body=WAYPOINTS)
    ca = _login(_client(), usera)
    r = ca.get("/api/ai/session/sess-a/path?after_seq=3&limit=50")
    assert r.status_code == 200, r.get_data(as_text=True)
    body = r.get_json()
    assert body["waypoints"] == WAYPOINTS["waypoints"]
    assert body["next_after_seq"] == 7
    path_call = [c for c in fake.calls if c["path"] == "/session/sess-a/path"][-1]
    assert path_call["query"] == {"after_seq": "3", "limit": "50"}


def test_path_owner_archived_session_readable(fake_sidecar):
    """archived session 属主仍可读 path（S5 契约；归属判定不看 archived）。"""
    owner, usera, _b, slide = _setup()
    fake = fake_sidecar
    fake.register_json("GET", "/session/sess-arch", body={
        "session": {"owner": usera["user_id"], "slide": slide,
                    "archived": True}})
    fake.register_json("GET", "/session/sess-arch/path", body=WAYPOINTS)
    ca = _login(_client(), usera)
    r = ca.get("/api/ai/session/sess-arch/path")
    assert r.status_code == 200
    assert r.get_json()["waypoints"]


def test_path_user_b_reads_a_gets_403_no_proxy(fake_sidecar):
    """user B 读 A 的 path → 403；sidecar 只收到归属查询，无 path 转发。"""
    owner, usera, userb, slide = _setup()
    fake = fake_sidecar
    fake.register_json("GET", "/session/sess-a",
                       body={"session": {"owner": usera["user_id"],
                                         "slide": slide}})
    cb = _login(_client(), userb)
    r = cb.get("/api/ai/session/sess-a/path?after_seq=0&limit=100")
    assert r.status_code == 403, r.get_data(as_text=True)
    assert not any(c["path"] == "/session/sess-a/path" for c in fake.calls)
    # 归属查询确实发生过（403 来自归属判定，而非路由缺失）
    assert any(c["path"] == "/session/sess-a" for c in fake.calls)


def test_path_owner_other_session_denied(fake_sidecar):
    """升级 B R6：owner 读他人（userA）会话 → 403（删除 owner 任意放行旁路；
    添加切片不等于取得对方聊天记录或续跑权）。"""
    owner, usera, _b, slide = _setup()
    fake = fake_sidecar
    fake.register_json("GET", "/session/sess-x",
                       body={"session": {"owner": usera["user_id"],
                                         "slide": slide}})
    co = _login(_client(), owner)
    r = co.get("/api/ai/session/sess-x/path")
    assert r.status_code == 403
    assert not any(c["path"] == "/session/sess-x/path" for c in fake.calls)


# --------------------------------------------------------------------------- #
# P1-7：_require_ai_session_owner 四端点越权（B→A 403）
# --------------------------------------------------------------------------- #
def test_session_detail_foreign_user_403(fake_sidecar):
    """GET session 详情：B 读 A → 403；A 读自己 → 200 且转发。"""
    owner, usera, userb, slide = _setup()
    fake = fake_sidecar
    fake.register_json("GET", "/session/sess-a",
                       body={"session": {"owner": usera["user_id"],
                                         "slide": slide},
                             "transcript": []})
    cb = _login(_client(), userb)
    r = cb.get("/api/ai/session/sess-a")
    assert r.status_code == 403
    # A 读自己 → 200 且转发
    ca = _login(_client(), usera)
    r = ca.get("/api/ai/session/sess-a")
    assert r.status_code == 200
    assert r.get_json()["session"]["owner"] == usera["user_id"]


def test_session_stream_foreign_user_403(fake_sidecar):
    """GET stream：B 挂 A → 403（sidecar 无 stream 连接）；A → 200 SSE 透传。"""
    owner, usera, userb, slide = _setup()
    fake = fake_sidecar
    fake.register_json("GET", "/session/sess-a",
                       body={"session": {"owner": usera["user_id"],
                                         "slide": slide}})
    fake.register_sse("GET", "/session/sess-a/stream",
                      [b"id: 1\nevent: delta\ndata: {\"t\":\"hi\"}\n\n"])
    cb = _login(_client(), userb)
    r = cb.get("/api/ai/session/sess-a/stream?after_seq=0")
    assert r.status_code == 403
    assert not any(c["path"] == "/session/sess-a/stream" for c in fake.calls)
    ca = _login(_client(), usera)
    r = ca.get("/api/ai/session/sess-a/stream?after_seq=0")
    assert r.status_code == 200
    assert b"delta" in r.data


def test_session_archive_unarchive_foreign_user_403(fake_sidecar):
    """POST archive/unarchive：B 对 A 的会话 → 403；A → 200 转发。"""
    owner, usera, userb, slide = _setup()
    fake = fake_sidecar
    fake.register_json("GET", "/session/sess-a",
                       body={"session": {"owner": usera["user_id"],
                                         "slide": slide}})
    fake.register_json("POST", "/session/sess-a/archive",
                       body={"ok": True, "archived": True})
    fake.register_json("POST", "/session/sess-a/unarchive",
                       body={"ok": True, "archived": False})
    cb = _login(_client(), userb)
    assert cb.post("/api/ai/session/sess-a/archive").status_code == 403
    assert cb.post("/api/ai/session/sess-a/unarchive").status_code == 403
    assert not any(c["method"] == "POST" for c in fake.calls)
    ca = _login(_client(), usera)
    assert ca.post("/api/ai/session/sess-a/archive").status_code == 200
    assert ca.post("/api/ai/session/sess-a/unarchive").status_code == 200


def test_session_path_csrf_exempt_get_only(fake_sidecar):
    """path 是 GET（安全方法）：无 CSRF 要求，但未登录（AUTH_ENABLED=True）→ 401。"""
    owner, _a, _b, _slide = _setup()
    c = _client()
    r = c.get("/api/ai/session/sess-a/path")
    assert r.status_code == 401
    assert r.get_json()["error"] == "auth_required"
