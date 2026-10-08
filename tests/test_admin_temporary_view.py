# -*- coding: utf-8 -*-
"""管理员临时查看：P0 验收矩阵（docs/admin-viewer-simplified-20261008.md §3/§8-1/§8-2）。

覆盖：
  - 开启后 1 小时内 info/tile/thumbnail/crop（ID 与 legacy 名端点）可读；
  - 把测试授权 expires_at 拨到过去 → 同一批请求全部 403；带 If-None-Match
    （先前拿到的 ETag）**不返回 304**（门禁在 ETag 之前）；
  - 重复开启不续期；结束立即拒绝且幂等；
  - 同名不同 ID 的切片互不影响；
  - 不可读资产 409 / 本人切片 409 own_slide；
  - 迁移后旧永久授权（expires_at=迁移时刻=过去）不再放行；
  - 旧 visibility 端点 410；
  - AI 运行授权到期不晚于临时查看到期（§3.3）；主动结束撤销运行授权。
"""
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import psycopg  # noqa: E402
import pytest  # noqa: E402

import _bootstrap  # noqa: E402,F401  # session 目录+openslide stub（conftest 先行）
UPLOAD_DIR = _bootstrap.UPLOAD_DIR
from _pt_helpers import csrf_client, isolate_app, publish_test_slide, \
    register_slide_row  # noqa: E402
from _tiff_fixtures import make_tiff_bytes  # noqa: E402

import app as app_mod  # noqa: E402
import share_store  # noqa: E402
import user_store  # noqa: E402


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    _, up_dir = isolate_app(monkeypatch, tmp_path, UPLOAD_DIR)
    monkeypatch.setattr(app_mod, "AUTH_ENABLED", True)
    for child in up_dir.iterdir():
        if child.is_file():
            child.unlink()
    yield


def _client(user):
    app_mod.app.config["TESTING"] = True
    c = csrf_client(app_mod.app.test_client())
    with c.session_transaction() as s:
        s["auth_user"] = user.get("login_id") or user["user_id"]
        s["user_id"] = user["user_id"]
        s["role"] = user.get("role") or "user"
        s["auth_version"] = user.get("auth_version", 1)
    return c


def _setup():
    owner = user_store.create_user("owner@x.com", "ownerpass123456",
                                   role="owner")
    usera = user_store.create_user("a@x.com", "userApass123456", role="user")
    share_store.set_owner_user_id(owner["user_id"])
    return owner, usera


def _sql(query, params=(), fetch=False):
    conn = psycopg.connect(os.environ["DATABASE_URL"])
    try:
        with conn.cursor() as cur:
            cur.execute(query, params)
            out = cur.fetchall() if fetch else None
        conn.commit()
        return out
    finally:
        conn.close()


def _backdate(slide_id, seconds=3600):
    """把该切片的授权 expires_at 拨到过去（模拟自然到期/迁移时刻）。"""
    _sql("UPDATE slide_view_grants SET expires_at = now() - "
         "(%s * interval '1 second') WHERE slide_id=%s", (seconds, slide_id))


def _read_urls_by_id(sid):
    return [
        "/api/slides/%s/info" % sid,
        "/api/slides/%s/tiles/0/0_0.jpeg" % sid,
        "/api/slides/%s/thumbnail" % sid,
        "/api/slides/%s/crop?x=0&y=0&size=8" % sid,
        "/api/slides/%s/region?x=0&y=0&w=8&h=8" % sid,
        "/api/slides/%s/dzi" % sid,
    ]


def _read_urls_by_name(name):
    return [
        "/api/slide/%s/info" % name,
        "/api/slide/%s_files/0/0_0.jpeg" % name,
        "/api/slide/%s/thumbnail" % name,
        "/api/slide/%s/crop?x=0&y=0&size=8" % name,
    ]


def test_temporary_view_read_matrix_and_expiry():
    """P0-1：开启 → ID/legacy 名读端点全通；拨过期 → 全部 403；If-None-Match
    不给 304。"""
    owner, usera = _setup()
    tiff = make_tiff_bytes()
    sid = publish_test_slide("temp.tif", tiff, owner_user_id=usera["user_id"])
    oc = _client(owner)

    # 开启前：全部 403
    for url in _read_urls_by_id(sid):
        assert oc.get(url).status_code == 403, url
    r = oc.post("/api/admin/v1/slides/%s/temporary-view" % sid)
    assert r.status_code == 200, r.get_data(as_text=True)
    tv = r.get_json()["temporary_view"]
    assert 3000 < tv["expires_at"] - r.get_json()["server_now"] <= 3600

    # 开启后：ID 端点全通（stub openslide 真渲染）
    for url in _read_urls_by_id(sid):
        resp = oc.get(url)
        assert resp.status_code == 200, (url, resp.status_code)
    etag = oc.get("/api/slides/%s/thumbnail" % sid).headers.get("ETag")
    assert etag

    # 未过期前 If-None-Match → 304（鉴权通过才有 304）
    resp304 = oc.get("/api/slides/%s/thumbnail" % sid,
                     headers={"If-None-Match": etag})
    assert resp304.status_code == 304

    # 拨过期（模拟 1 小时到期）→ 同一批请求全部 403
    _backdate(sid)
    for url in _read_urls_by_id(sid):
        assert oc.get(url).status_code == 403, url
    # 带 If-None-Match 的请求不得 304（门禁在 ETag 之前）
    resp = oc.get("/api/slides/%s/thumbnail" % sid,
                  headers={"If-None-Match": etag})
    assert resp.status_code == 403
    # 列表不再包含该切片
    assert sid not in {i.get("slide_id")
                       for i in oc.get("/api/slides").get_json()}


def test_temporary_view_legacy_name_endpoints():
    """P0-1（legacy URL）：带冻结 legacy 名的资产，旧按名端点同一门禁。"""
    owner, usera = _setup()
    name = "legacy-name.tif"
    p = Path(UPLOAD_DIR) / name
    p.write_bytes(make_tiff_bytes())
    sid = register_slide_row(name)  # id_bundle ready 行 + legacy_filename
    _sql("UPDATE slides SET owner_user_id=%s WHERE slide_id=%s",
         (usera["user_id"], sid))
    oc = _client(owner)
    assert oc.post("/api/admin/v1/slides/%s/temporary-view" % sid) \
        .status_code == 200
    for url in _read_urls_by_name(name):
        resp = oc.get(url)
        assert resp.status_code != 403, url
    # 拨过期 → 旧名端点同样 403
    _backdate(sid)
    for url in _read_urls_by_name(name):
        assert oc.get(url).status_code == 403, url


def test_repeat_start_does_not_extend_and_end_is_immediate_idempotent():
    """P0-1：重复开启返回原到期不续期；结束立即拒绝；再次结束 none。"""
    owner, usera = _setup()
    sid = publish_test_slide("rewind2.tif", make_tiff_bytes(),
                             owner_user_id=usera["user_id"])
    oc = _client(owner)
    tv1 = oc.post("/api/admin/v1/slides/%s/temporary-view" % sid) \
        .get_json()["temporary_view"]
    tv2 = oc.post("/api/admin/v1/slides/%s/temporary-view" % sid) \
        .get_json()["temporary_view"]
    assert tv2["expires_at"] == tv1["expires_at"]
    assert tv2["granted_at"] == tv1["granted_at"]

    r = oc.delete("/api/admin/v1/slides/%s/temporary-view" % sid)
    assert r.status_code == 200
    assert r.get_json()["temporary_view"]["status"] == "ended"
    # 立即拒绝
    assert oc.get("/api/slides/%s/info" % sid).status_code == 403
    # 幂等
    r2 = oc.delete("/api/admin/v1/slides/%s/temporary-view" % sid)
    assert r2.status_code == 200
    assert r2.get_json()["temporary_view"]["status"] == "none"


def test_same_display_name_different_slide_id_isolated():
    """P0-1：同名（original_filename/display_name 相同）不同 slide_id 互不影响。"""
    owner, usera = _setup()
    tiff = make_tiff_bytes()
    sid1 = publish_test_slide("dup-name.tif", tiff,
                              owner_user_id=usera["user_id"])
    sid2 = publish_test_slide("dup-name.tif", tiff,
                              owner_user_id=usera["user_id"])
    assert sid1 != sid2
    oc = _client(owner)
    assert oc.post("/api/admin/v1/slides/%s/temporary-view" % sid1) \
        .status_code == 200
    # 仅 sid1 可见；sid2（同名不同 ID）不可见
    visible = {i.get("slide_id") for i in oc.get("/api/slides").get_json()}
    assert sid1 in visible and sid2 not in visible
    assert oc.get("/api/slides/%s/info" % sid1).status_code == 200
    assert oc.get("/api/slides/%s/info" % sid2).status_code == 403
    # 结束 sid1 不影响（也不波及）其它行
    oc.delete("/api/admin/v1/slides/%s/temporary-view" % sid1)
    assert oc.get("/api/slides/%s/info" % sid2).status_code == 403


def test_not_servable_409_and_own_slide_409_and_migrated_grant_denied():
    """P0-1：不可读 409；本人切片 409 own_slide；旧永久授权（迁移时刻=过去）
    不再放行。"""
    owner, usera = _setup()
    # 不可服务：legacy 布局行
    name = "unmigrated.svs"
    (Path(UPLOAD_DIR) / name).write_bytes(b"svs-stub")
    legacy_sid = register_slide_row(name)
    _sql("UPDATE slides SET asset_state='legacy', storage_layout='legacy', "
         "storage_relpath=NULL, owner_user_id=%s WHERE legacy_filename=%s",
         (usera["user_id"], name))
    oc = _client(owner)
    r = oc.post("/api/admin/v1/slides/%s/temporary-view" % legacy_sid)
    assert r.status_code == 409
    assert r.get_json()["error"]["code"] == "slide_not_servable"

    # 本人切片
    mine = publish_test_slide("mine2.tif", make_tiff_bytes(),
                              owner_user_id=owner["user_id"])
    r = oc.post("/api/admin/v1/slides/%s/temporary-view" % mine)
    assert r.status_code == 409
    assert r.get_json()["error"]["code"] == "own_slide"

    # 迁移形态的旧永久授权：0080 把存量 expires_at 设为迁移时刻（过去）——
    # 直接构造（expires_at=过去）验证读门禁只认未到期行
    old = publish_test_slide("migrated-old.tif", make_tiff_bytes(),
                             owner_user_id=usera["user_id"])
    share_store.grant_slide_view(owner["user_id"], old, granted_by=None,
                                 slide_id=old, ttl_seconds=3600)
    _backdate(old, seconds=86400)  # 迁移时刻：昨天
    assert oc.get("/api/slides/%s/info" % old).status_code == 403
    assert old not in {i.get("slide_id")
                       for i in oc.get("/api/slides").get_json()}
    # 重新开启 = 新窗口（不继承旧到期）
    r = oc.post("/api/admin/v1/slides/%s/temporary-view" % old)
    assert r.status_code == 200
    assert r.get_json()["temporary_view"]["expires_at"] > time.time()
    assert oc.get("/api/slides/%s/info" % old).status_code == 200


def test_grant_slide_view_requires_explicit_ttl():
    """round-2 收紧：grant_slide_view 的 ttl_seconds 必填（无缺省）——
    任何调用方都不可能因漏传而造出「永久」授权。"""
    owner, usera = _setup()
    sid = publish_test_slide("ttl-guard.tif", make_tiff_bytes(),
                             owner_user_id=usera["user_id"])
    # 漏传 → TypeError（必填参数）
    with pytest.raises(TypeError):
        share_store.grant_slide_view(owner["user_id"], "ttl-guard.tif",
                                     slide_id=sid)
    # 非正数 → ValueError
    with pytest.raises(ValueError):
        share_store.grant_slide_view(owner["user_id"], "ttl-guard.tif", 0,
                                     slide_id=sid)
    with pytest.raises(ValueError):
        share_store.grant_slide_view(owner["user_id"], "ttl-guard.tif", -5,
                                     slide_id=sid)
    assert _sql("SELECT count(*) FROM slide_view_grants WHERE slide_id=%s",
                (sid,), fetch=True)[0][0] == 0


def test_old_visibility_route_410():
    """P0-1：旧 POST /visibility → 410 endpoint_retired（不建任何授权）。"""
    owner, usera = _setup()
    sid = publish_test_slide("retired.tif", make_tiff_bytes(),
                             owner_user_id=usera["user_id"])
    oc = _client(owner)
    r = oc.post("/api/admin/v1/slides/%s/visibility" % sid,
                json={"granted": True})
    assert r.status_code == 410
    assert r.get_json()["error"]["code"] == "endpoint_retired"
    assert _sql("SELECT count(*) FROM slide_view_grants WHERE slide_id=%s",
                (sid,), fetch=True)[0][0] == 0


# --------------------------------------------------------------------------- #
# §3.3：AI 运行授权到期不晚于临时查看到期；主动结束撤销运行授权
# --------------------------------------------------------------------------- #
def _bootstrap_plugin():
    inst = app_mod._bootstrap_plugin_installations()
    assert inst is not None
    app_mod._HISTOPILOT_INSTALLATION = inst
    return inst


def test_run_grant_ttl_capped_to_temporary_view_expiry():
    inst = _bootstrap_plugin()
    owner, usera = _setup()
    sid = publish_test_slide("cap.tif", make_tiff_bytes(),
                             owner_user_id=usera["user_id"])
    oc = _client(owner)
    # 临时查看剩余 5 分钟：直接插入一个 5 分钟窗口
    share_store.start_slide_view_grant_timed(
        owner["user_id"], sid, granted_by=owner["user_id"], slide_id=sid,
        ttl_seconds=300)
    config = {"sidecar": True}
    ctx = {"user_id": owner["user_id"], "role": user_store.ROLE_OWNER}
    assert app_mod._issue_run_grant("cap.tif", ctx, config, slide_id=sid)
    grant = share_store.get_run_grant(config["run_grant"]["grant_id"])
    remaining = share_store.active_slide_view_grants_for_user(
        owner["user_id"])[sid] - time.time()
    granted_ttl = grant["expires_at"] - time.time()
    # 运行授权到期 <= 临时查看到期（且明显低于常规 TTL）
    assert granted_ttl <= remaining + 1.0
    assert granted_ttl < app_mod._RUN_GRANT_TTL_SECONDS

    # 无临时授权（本人切片）→ 不钳制
    mine = publish_test_slide("mine3.tif", make_tiff_bytes(),
                              owner_user_id=owner["user_id"])
    config2 = {"sidecar": True}
    assert app_mod._issue_run_grant("mine3.tif", ctx, config2, slide_id=mine)
    g2 = share_store.get_run_grant(config2["run_grant"]["grant_id"])
    assert g2["expires_at"] - time.time() > 300


def test_expired_temporary_view_blocks_ai_run_at_gate():
    """临时查看到期后 AI run 起跑在可见性闸即 403（不进入 grant/预算段）。

    用 legacy 名资产（annotate = view 收录集口径）；id_bundle 资产的
    annotate 闸本就不随 view 授权放行（既有语义，非本变更引入）。"""
    owner, usera = _setup()
    name = "expired-cap.tif"
    (Path(UPLOAD_DIR) / name).write_bytes(make_tiff_bytes())
    sid = register_slide_row(name)
    _sql("UPDATE slides SET owner_user_id=%s WHERE slide_id=%s",
         (usera["user_id"], sid))
    oc = _client(owner)
    share_store.grant_slide_view(owner["user_id"], sid, slide_id=sid,
                                 ttl_seconds=3600)
    _backdate(sid)
    r = oc.post("/api/ai/run", json={"slide_id": sid})
    assert r.status_code == 403, r.get_data(as_text=True)
    # 未到期窗口内：权限闸放行（无凭据 → 400/503 配置层，非 403）
    assert oc.post("/api/admin/v1/slides/%s/temporary-view" % sid) \
        .status_code == 200
    r2 = oc.post("/api/ai/run", json={"slide_id": sid})
    assert r2.status_code in (400, 503), r2.get_data(as_text=True)


@pytest.mark.parametrize("preserve_uploader", [
    False,
    pytest.param(True, marks=pytest.mark.xfail(
        strict=True, raises=AssertionError,
        reason="KNOWN: ending admin view revokes uploader AI grant too; Opus owns fix")),
])
def test_end_temporary_view_revokes_run_grants(preserve_uploader):
    inst = _bootstrap_plugin()
    owner, usera = _setup()
    sid = publish_test_slide("revoke.tif", make_tiff_bytes(),
                             owner_user_id=usera["user_id"])
    oc = _client(owner)
    assert oc.post("/api/admin/v1/slides/%s/temporary-view" % sid) \
        .status_code == 200
    config = {"sidecar": True}
    ctx = {"user_id": owner["user_id"], "role": user_store.ROLE_OWNER}
    assert app_mod._issue_run_grant("revoke.tif", ctx, config, slide_id=sid)
    grant_id = config["run_grant"]["grant_id"]
    assert share_store.get_run_grant(grant_id)["revoked"] is False
    if preserve_uploader:
        uploader_config = {"sidecar": True}
        uploader_ctx = {"user_id": usera["user_id"], "role": user_store.ROLE_USER}
        assert app_mod._issue_run_grant(
            "revoke.tif", uploader_ctx, uploader_config, slide_id=sid)
        uploader_grant = uploader_config["run_grant"]["grant_id"]
        assert share_store.get_run_grant(uploader_grant)["revoked"] is False
    # 主动结束 → 运行授权撤销
    assert oc.delete("/api/admin/v1/slides/%s/temporary-view" % sid) \
        .status_code == 200
    assert share_store.get_run_grant(grant_id)["revoked"] is True
    if preserve_uploader:
        assert share_store.get_run_grant(uploader_grant)["revoked"] is False
