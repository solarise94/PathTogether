# -*- coding: utf-8 -*-
"""升级 B 批次 3：owner 工作区收录关系、权限收口与授权资产生命周期。

覆盖（review 2026-09-05 升级 B §5 / R5 / R6 / R7）：
  - AI 会话守卫（R6）：owner 旁路删除——他人会话即使已收录切片也 403；
    owner 自己会话（真实 principal）200；空 owner 历史会话按「owner 角色 +
    切片收录」过渡口径；收录撤销后属主自己的会话读取也立即 403；
    stream/path 同口径；sidecar 不可达保守 403。
  - run grant 创建者复查（R6）：owner 创建者的 grant 在收录移除后失效。
  - 撤销联动（R6d）：管理端 revoke → 活跃 grant 撤销 + 运行中 run 走既有
    /cancel（可控断言 sidecar 收到取消请求，不 mock 掉取消机制）。
  - 资产生命周期（R7）：添加 → 删除文件（授权清理，无孤儿行）→ 同名重传
    （slide_id 复用）→ 旧授权不自动生效；资产生代失配的授权不匹配；
    迁移 0035 backfill + 可重复执行。
  - 插件 region 闸（R6）：活跃 grant 复核通过放行；撤销后 fail-closed 403；
    demo 目录切片与本地免认证态无 grant 放行。
  - 空 principal 盘点接口（R6e/§5.4）：owner-only 只读报告 + 审计。
  - session_owner 注入（R6e）：认证 owner 起跑也注入真实 principal。

Fake sidecar 用共享 FakeRequests（不 mock 平台取消机制本身）；真实 PG 由
conftest 自举。运行：python -m pytest tests/test_owner_workspace_upgrade.py -q
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import _bootstrap  # noqa: E402,F401  # session 目录+openslide stub（conftest 先行）
DATA_DIR = _bootstrap.SHARE_DATA_DIR
UPLOAD_DIR = _bootstrap.UPLOAD_DIR
import psycopg  # noqa: E402
import pytest  # noqa: E402

import app as app_mod  # noqa: E402
import demo_store  # noqa: E402
import share_store  # noqa: E402
import user_store  # noqa: E402
from _pt_helpers import csrf_client, isolate_app, FakeRequests  # noqa: E402


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    _, up_dir = isolate_app(monkeypatch, tmp_path, UPLOAD_DIR,
                            login_limits=True)
    monkeypatch.setattr(app_mod, "AUTH_ENABLED", True)
    for child in up_dir.iterdir():
        if child.is_file():
            child.unlink()
    yield


@pytest.fixture()
def fake_sidecar(monkeypatch):
    fake = FakeRequests()
    monkeypatch.setattr(app_mod, "requests", fake)
    return fake


def _client():
    app_mod.app.config["TESTING"] = True
    app_mod.AUTH_ENABLED = True
    return csrf_client(app_mod.app.test_client())


def _login(client, user):
    with client.session_transaction() as s:
        s["auth_user"] = user.get("login_id") or user.get("user_id")
        s["user_id"] = user["user_id"]
        s["role"] = user.get("role") or "user"
        s["auth_version"] = user.get("auth_version", 1)
    return client


def _touch(name):
    p = Path(UPLOAD_DIR) / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"svs-stub")
    return name


def _setup_two_owners():
    """owner + userA（站点单 enabled owner 是 0015 DB 不变量；「O2」仅以
    uid 形态用于 store 层隔离断言，无法建第二个 enabled 账号）。"""
    owner = user_store.create_user("owner@x.com", "ownerpass123456", role="owner")
    usera = user_store.create_user("a@x.com", "userApass123456", role="user")
    share_store.set_owner_user_id(owner["user_id"])
    return owner, usera


def _grant(slide, who):
    """以管理端语义给 who 加入工作区（走服务端接口，保证 slide_id 绑定）。"""
    c = _client()
    _login(c, who)
    r = c.post("/api/admin/v1/slides/%s/visibility" % slide,
               json={"granted": True})
    assert r.status_code == 200, r.get_data(as_text=True)
    return r


def _install_grant(slide, creator, installation_id="inst-test", session_id=""):
    return share_store.create_run_grant(
        installation_id=installation_id, slide=slide, session_id=session_id,
        created_by_user_id=creator["user_id"], ttl_seconds=1800)


def test_grant_isolated_per_account_uid():
    """矩阵「O1 添加不影响 O2」：收录按 user_id 严格隔离（单 enabled owner
    是 0015 DB 不变量，O2 以 uid 形态表达——真实部署不存在并行 enabled
    owner；此处锁死收录集合绝不跨账号共享）。"""
    owner, usera = _setup_two_owners()
    slide = _touch("a.svs")
    share_store.set_slide_meta(slide, owner_user_id=usera["user_id"])
    _grant(slide, owner)
    other_owner_uid = "usr_synthetic_other_owner"
    assert slide in share_store.slide_view_grants_for_user(owner["user_id"])
    assert slide not in share_store.slide_view_grants_for_user(other_owner_uid)
    assert slide not in share_store.slide_view_grants_for_user(usera["user_id"])


# =========================================================================== #
# R6：AI 会话守卫（_require_ai_session_owner 收紧）
# =========================================================================== #
def test_owner_cannot_read_user_session_even_granted(fake_sidecar):
    """owner 已收录切片也不能读 user 会话（添加≠取得对方聊天记录）。"""
    owner, usera = _setup_two_owners()
    slide = _touch("a.svs")
    share_store.set_slide_meta(slide, owner_user_id=usera["user_id"])
    _grant(slide, owner)
    fake = fake_sidecar
    fake.register_json("GET", "/session/sess-a",
                       body={"session": {"owner": usera["user_id"],
                                         "slide": slide},
                             "transcript": []})
    co = _login(_client(), owner)
    assert co.get("/api/ai/session/sess-a").status_code == 403
    # stream 同口径：无 stream 连接建立
    assert co.get("/api/ai/session/sess-a/stream").status_code == 403
    assert not any(c["path"].endswith("/stream") for c in fake.calls)


def test_owner_reads_own_session_with_real_principal(fake_sidecar):
    """owner 自己的会话（R6e 起真实 principal）→ 200（自己的切片天然在集合
    内）；user 读该 owner 会话 403。"""
    owner, usera = _setup_two_owners()
    slide = _touch("a.svs")
    share_store.set_slide_meta(slide, owner_user_id=owner["user_id"])
    fake = fake_sidecar
    fake.register_json("GET", "/session/sess-o",
                       body={"session": {"owner": owner["user_id"],
                                         "slide": slide},
                             "transcript": []})
    co = _login(_client(), owner)
    assert co.get("/api/ai/session/sess-o").status_code == 200
    ca = _login(_client(), usera)
    assert ca.get("/api/ai/session/sess-o").status_code == 403


def test_owner_granted_session_denied_after_revoke(fake_sidecar):
    """空 owner 历史会话：收录时 owner 可读（过渡口径）；移除后立即 403。"""
    owner, usera = _setup_two_owners()
    slide = _touch("a.svs")
    share_store.set_slide_meta(slide, owner_user_id=usera["user_id"])
    fake = fake_sidecar
    fake.register_json("GET", "/session/sess-legacy",
                       body={"session": {"owner": "", "slide": slide}})
    fake.register_json("GET", "/session/sess-legacy/path",
                       body={"waypoints": [], "next_after_seq": 0})
    co = _login(_client(), owner)
    # 未收录 → 403
    assert co.get("/api/ai/session/sess-legacy").status_code == 403
    # 收录 → 200（detail 与 path 同口径）
    _grant(slide, owner)
    assert co.get("/api/ai/session/sess-legacy").status_code == 200
    assert co.get("/api/ai/session/sess-legacy/path").status_code == 200
    # 移除 → 立即 403
    assert co.post("/api/admin/v1/slides/%s/visibility" % slide,
                   json={"granted": False}).status_code == 200
    assert co.get("/api/ai/session/sess-legacy").status_code == 403
    assert co.get("/api/ai/session/sess-legacy/path").status_code == 403


def test_user_cannot_read_legacy_unowned_session(fake_sidecar):
    """user 对空 owner 历史会话一律 403（例外仅 owner 角色）。"""
    owner, usera = _setup_two_owners()
    slide = _touch("a.svs")
    share_store.set_slide_meta(slide, owner_user_id=usera["user_id"])
    fake = fake_sidecar
    fake.register_json("GET", "/session/sess-legacy",
                       body={"session": {"owner": "", "slide": slide}})
    ca = _login(_client(), usera)
    assert ca.get("/api/ai/session/sess-legacy").status_code == 403


def test_session_guard_sidecar_unavailable_403(fake_sidecar):
    """sidecar 不可达 → 保守 403（不因 sidecar 宕机 fail-open）。"""
    owner, usera = _setup_two_owners()
    fake = fake_sidecar
    fake.set_unreachable()
    co = _login(_client(), owner)
    assert co.get("/api/ai/session/sess-x").status_code == 403


def test_ai_sessions_list_scoped_for_authenticated_owner(fake_sidecar):
    """认证 owner 的会话目录同样按 owner=<uid> 过滤（不开放他人会话列表）。"""
    owner, usera = _setup_two_owners()
    slide = _touch("a.svs")
    share_store.set_slide_meta(slide, owner_user_id=owner["user_id"])
    fake = fake_sidecar
    fake.register_json("GET", "/sessions",
                       body={"sessions": [], "conversations": []})
    co = _login(_client(), owner)
    assert co.get("/api/ai/sessions?slide=%s" % slide).status_code == 200
    call = [c for c in fake.calls if c["path"] == "/sessions"][-1]
    assert call["query"] == {"slide": slide, "owner": owner["user_id"]}


# =========================================================================== #
# R6d：撤销联动（run grant 失效 + 运行中 run 取消）
# =========================================================================== #
def test_revoke_revokes_stale_run_grants_and_cancels_running(fake_sidecar):
    """revoke 后：创建者 run grant 失效；运行中 run 收到既有 /cancel 请求。

    取消断言走真实管理端点 + fake sidecar 的调用记录（可控地断言取消被
    触发，不 mock 掉取消机制本身）；费用 hold 不被触碰（无 billing 写）。
    """
    owner, usera = _setup_two_owners()
    slide = _touch("a.svs")
    share_store.set_slide_meta(slide, owner_user_id=usera["user_id"])
    _grant(slide, owner)
    grant = _install_grant(slide, owner)
    assert share_store.get_run_grant(grant["grant_id"])["revoked"] is False

    fake = fake_sidecar
    fake.register_json("GET", "/sessions", body={"sessions": [
        {"id": "sess-run-1", "owner": owner["user_id"], "status": "running"},
        {"id": "sess-done", "owner": owner["user_id"], "status": "finished"},
        {"id": "sess-other", "owner": usera["user_id"], "status": "running"},
    ]})
    fake.register_json("POST", "/cancel", body={"ok": True})

    co = _client()
    _login(co, owner)
    r = co.post("/api/admin/v1/slides/%s/visibility" % slide,
                json={"granted": False})
    assert r.status_code == 200, r.get_data(as_text=True)
    # 只对「running 且属被撤主体」的 run 发起取消；finished / 他人 run 不动
    assert r.get_json()["runs_cancelled"] == ["sess-run-1"]
    cancels = [c for c in fake.calls
               if c["method"] == "POST" and c["path"] == "/cancel"]
    assert [c["body"]["session_id"] for c in cancels] == ["sess-run-1"]
    # run grant 已失效（下一次工具派发 fail-closed）
    assert share_store.get_run_grant(grant["grant_id"])["revoked"] is True


def test_slide_delete_clears_view_grants_no_orphans():
    """R7 生命周期：添加 → 删除文件 → 授权清理（无孤儿行）→ 同名重传（
    slide_id 复用）→ 旧授权不自动生效。"""
    owner, usera = _setup_two_owners()
    slide = _touch("a.svs")
    share_store.set_slide_meta(slide, owner_user_id=usera["user_id"])
    old_slide_id = share_store.get_slide_id(slide)
    _grant(slide, owner)
    names = {i["name"] for i in _login(_client(), owner)
             .get("/api/slides").get_json()}
    assert slide in names

    # 删除文件（owner 可删任意）→ 授权同事务清理
    co = _login(_client(), owner)
    assert co.delete("/api/slide/%s" % slide).status_code == 200
    with psycopg.connect(os.environ["DATABASE_URL"]) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM slide_view_grants")
            assert cur.fetchone()[0] == 0  # 无孤儿授权行
    names = {i["name"] for i in co.get("/api/slides").get_json()}
    assert slide not in names

    # 同名重传（no-clobber 上传必经删除；slides 行保留 → slide_id 复用）
    _touch(slide)
    share_store.set_slide_meta(slide, owner_user_id=usera["user_id"])
    assert share_store.get_slide_id(slide) == old_slide_id  # 同名复用
    names = {i["name"] for i in co.get("/api/slides").get_json()}
    assert slide not in names  # 旧授权不自动生效（失效语义）
    assert co.get("/api/slide/%s/info" % slide).status_code == 403

    # 显式重新添加才恢复
    _grant(slide, owner)
    names = {i["name"] for i in co.get("/api/slides").get_json()}
    assert slide in names


def test_grant_requires_asset_generation_match():
    """R7：资产生代失配的授权行不匹配（按名残留的防御断言）。"""
    owner, usera = _setup_two_owners()
    slide = _touch("a.svs")
    share_store.set_slide_meta(slide, owner_user_id=usera["user_id"])
    slide_id = share_store.get_slide_id(slide)
    # 直插一行 slide_id 失配的授权（模拟旧代残留/迁移前形态）
    with psycopg.connect(os.environ["DATABASE_URL"]) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO slide_view_grants (slide_name, user_id, "
                "granted_by, slide_id) VALUES (%s,%s,%s,%s)",
                (slide, owner["user_id"], owner["user_id"], "sld_stale"))
        conn.commit()
    # 失配 → 不计入收录集合
    assert slide not in share_store.slide_view_grants_for_user(owner["user_id"])
    co = _login(_client(), owner)
    assert co.get("/api/slide/%s/info" % slide).status_code == 403
    # inventory 的 included 状态同样不把失配行算进去
    items = co.get("/api/admin/v1/slides/inventory").get_json()["items"]
    by_name = {i["name"]: i for i in items}
    assert by_name[slide]["granted_to_owner"] is False
    # 正确绑定资产生代后生效（管理端点行为）
    _grant(slide, owner)
    assert slide in share_store.slide_view_grants_for_user(owner["user_id"])
    assert share_store.get_slide_id(slide) == slide_id


def test_migration_0035_backfill_and_replayable(pg_uri):
    """迁移 0035：backfill 既有行的当前同名 slide_id；重放幂等。"""
    owner, usera = _setup_two_owners()
    slide = _touch("a.svs")
    share_store.set_slide_meta(slide, owner_user_id=usera["user_id"])
    slide_id = share_store.get_slide_id(slide)
    migrations_sql = (Path(__file__).resolve().parent.parent / "migrations"
                      / "0035_slide_view_grants_asset_generation.sql")
    sql = migrations_sql.read_text(encoding="utf-8")
    with psycopg.connect(pg_uri) as conn:
        with conn.cursor() as cur:
            # 模拟 0034 形态（无 slide_id 列值）的既有授权行
            cur.execute(
                "INSERT INTO slide_view_grants (slide_name, user_id, "
                "granted_by, slide_id) VALUES (%s,%s,%s,NULL)",
                (slide, owner["user_id"], owner["user_id"]))
            cur.execute("SELECT slide_id FROM slide_view_grants "
                        "WHERE slide_name=%s", (slide,))
            assert cur.fetchone()[0] is None
            # 无 meta 行的孤儿授权保持 NULL（不伪造资产生代）
            cur.execute(
                "INSERT INTO slide_view_grants (slide_name, user_id) "
                "VALUES ('ghost.svs', %s)", (owner["user_id"],))
        conn.commit()
        with conn.cursor() as cur:
            cur.execute(sql)
        conn.commit()
        with conn.cursor() as cur:
            cur.execute("SELECT slide_id FROM slide_view_grants "
                        "WHERE slide_name=%s", (slide,))
            assert cur.fetchone()[0] == slide_id  # backfill 按当前元数据
            cur.execute("SELECT slide_id FROM slide_view_grants "
                        "WHERE slide_name='ghost.svs'")
            assert cur.fetchone()[0] is None  # 孤儿授权语义不变
        # 重放：幂等（不报错、不改变已回填值）
        with conn.cursor() as cur:
            cur.execute(sql)
            cur.execute(sql)
        conn.commit()
        with conn.cursor() as cur:
            cur.execute("SELECT slide_id FROM slide_view_grants "
                        "WHERE slide_name=%s", (slide,))
            assert cur.fetchone()[0] == slide_id


# =========================================================================== #
# R6：插件 region 闸
# =========================================================================== #
def test_plugin_region_gate_active_grant_pass_and_revoke_failclosed():
    """有活跃 grant 且创建者收录有效 → 放行；撤销收录后 fail-closed 403。"""
    owner, usera = _setup_two_owners()
    slide = _touch("a.svs")
    share_store.set_slide_meta(slide, owner_user_id=usera["user_id"])
    _grant(slide, owner)
    grant = _install_grant(slide, owner)
    gate = app_mod._plugin_slide_run_grant_gate

    # 活跃 grant + 收录有效 → 放行
    with app_mod.app.test_request_context("/x", method="POST"):
        assert gate(slide, {"sub": "inst-test"}) is None

    # 移除收录 → 联动撤销 grant → 无活跃 grant（认证态非 demo）→ 403
    co = _login(_client(), owner)
    assert co.post("/api/admin/v1/slides/%s/visibility" % slide,
                   json={"granted": False}).status_code == 200
    with app_mod.app.test_request_context("/x", method="POST"):
        err = gate(slide, {"sub": "inst-test"})
    assert err is not None and err.status_code == 403


def test_plugin_region_gate_demo_and_local_mode_without_grant(monkeypatch):
    """无活跃 grant：demo 目录切片与本地免认证态放行；认证态其余 fail-closed。"""
    owner, usera = _setup_two_owners()
    slide = _touch("a.svs")
    share_store.set_slide_meta(slide, owner_user_id=usera["user_id"])
    demo_store.catalog_add(share_store.get_slide_id(slide), added_by=owner["user_id"])
    gate = app_mod._plugin_slide_run_grant_gate

    # demo 目录切片：无 grant 放行
    with app_mod.app.test_request_context("/x", method="POST"):
        assert gate(slide, {"sub": "inst-test"}) is None

    # 认证态、非 demo 切片、无 grant → 403
    other = _touch("b.svs")
    share_store.set_slide_meta(other, owner_user_id=usera["user_id"])
    with app_mod.app.test_request_context("/x", method="POST"):
        err = gate(other, {"sub": "inst-test"})
    assert err is not None and err.status_code == 403

    # 本地免认证态（AUTH_ENABLED=False）：无 grant 放行（内网行为）
    monkeypatch.setattr(app_mod, "AUTH_ENABLED", False)
    with app_mod.app.test_request_context("/x", method="POST"):
        assert gate(other, {"sub": "inst-test"}) is None


def test_plugin_region_gate_with_grant_header(monkeypatch):
    """带 X-Run-Grant 的请求按 grant 校验：无效 grant 403。"""
    owner, usera = _setup_two_owners()
    slide = _touch("a.svs")
    share_store.set_slide_meta(slide, owner_user_id=usera["user_id"])
    _grant(slide, owner)
    grant = _install_grant(slide, owner)
    gate = app_mod._plugin_slide_run_grant_gate

    with app_mod.app.test_request_context(
            "/x", method="POST", headers={"X-Run-Grant": grant["grant_id"]}):
        assert gate(slide, {"sub": "inst-test"}) is None

    # slide 失配 → 403
    other = _touch("b.svs")
    share_store.set_slide_meta(other, owner_user_id=usera["user_id"])
    with app_mod.app.test_request_context(
            "/x", method="POST", headers={"X-Run-Grant": grant["grant_id"]}):
        err = gate(other, {"sub": "inst-test"})
    assert err is not None and err.status_code == 403


# =========================================================================== #
# R6e：空 principal 盘点接口 + session_owner 注入
# =========================================================================== #
def test_unowned_sessions_inventory_owner_only(fake_sidecar):
    """盘点接口：owner-only 只读报告；仅返回空 owner 会话；写审计。"""
    owner, usera = _setup_two_owners()
    slide = _touch("a.svs")
    share_store.set_slide_meta(slide, owner_user_id=owner["user_id"])
    fake = fake_sidecar
    fake.register_json("GET", "/sessions", body={"sessions": [
        {"id": "sess-legacy-1", "owner": "", "title": "全片读片",
         "status": "finished", "created_at": 100, "updated_at": 200},
        {"id": "sess-o", "owner": owner["user_id"], "status": "finished"},
        {"id": "sess-a", "owner": usera["user_id"], "status": "finished"},
    ]})
    co = _login(_client(), owner)
    r = co.get("/api/admin/v1/ai/unowned-sessions?slide=%s" % slide)
    assert r.status_code == 200, r.get_data(as_text=True)
    body = r.get_json()
    assert body["unowned_count"] == 1
    assert body["unowned_sessions"][0]["id"] == "sess-legacy-1"
    # 审计（只记数量，不含会话内容）
    actions = [e["action"] for e in share_store.list_audit(limit=50)]
    assert "admin.ai_unowned_sessions.inventory" in actions
    # user → 403
    ca = _login(_client(), usera)
    assert ca.get(
        "/api/admin/v1/ai/unowned-sessions?slide=%s" % slide
    ).status_code == 403


def test_session_owner_injected_for_all_authenticated_roles(monkeypatch):
    """R6e：认证 owner 起跑同样注入真实 principal；本地模式不注入。"""
    owner, usera = _setup_two_owners()
    slide = _touch("a.svs")
    share_store.set_slide_meta(slide, owner_user_id=owner["user_id"])
    monkeypatch.setattr(app_mod, "_build_sidecar_config",
                        lambda ctx, demo_capability_id=None: {"base_url": "x"})
    monkeypatch.setattr(app_mod, "_inject_agent_extra_tools",
                        lambda ctx, slide, config: None)
    monkeypatch.setattr(app_mod, "_issue_run_grant",
                        lambda slide, ctx, config: True)
    monkeypatch.setattr(app_mod, "_ai_reserve_run_budget",
                        lambda ctx, rid: ({}, None, None))
    with app_mod.app.test_request_context("/api/ai/run", method="POST",
                                          json={"slide": slide}):
        from flask import session as flask_session
        flask_session["auth_user"] = owner["login_id"]
        flask_session["user_id"] = owner["user_id"]
        flask_session["role"] = "owner"
        prep = app_mod._ai_run_prepare(app_mod.current_identity(),
                                       {"slide": slide}, slide,
                                       need_grant=False)
    assert isinstance(prep, dict)
    assert prep["config"]["session_owner"] == owner["user_id"]
