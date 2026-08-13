# -*- coding: utf-8 -*-
"""dual 后端 result-replay 身份一致性测试（仅 RUN_PG_TESTS=1 下跑，Stage 3b-2）。

验证：dual（expand）形态下 json 为权威、pg 为影子副本，凡 json 内部生成身份
（token/annotation_id/grant_id/pid/user_id）的写，pg 镜像按 json 返回的权威 dict
原样 upsert → 两库身份逐项一致，不会因 pg 自生成不同身份值而发散。

覆盖：
  - create_user：json 返回的 user_id 在 pg users 中存在且相同；
  - create_share：token / slides / permissions 与 json 权威一致；
  - add_roi：annotation_id / geom / shared / token 一致，insert_seq 与 json index 对齐；
  - claim_share：grant_id / permissions 一致；
  - create_project / add_slides_to_project：pid / slides 一致；
  - json 抛错（非法入参）时 pg 不写；
  - pg 镜像失败不阻断 json 返回（log 即可）。

conftest 在 RUN_PG_TESTS=1 时已起 pgserver 并设 DATABASE_URL + STORAGE_BACKEND=postgres；
本模块用独立模块名重新加载一份 STORAGE_BACKEND=dual 的 dispatcher（读 DATABASE_URL），
json 侧用独立 SHARE_DATA_DIR。pg 表由 conftest 每用例 TRUNCATE。
"""
import importlib.util
import os
import sys
import tempfile
from pathlib import Path

import pytest

import conftest
import pg_store

pytestmark = pytest.mark.skipif(
    conftest.BACKEND != "postgres",
    reason="dual 后端身份一致性需 PG（RUN_PG_TESTS=1）",
)

_REPO_ROOT = Path(__file__).resolve().parent.parent


def _load_fresh(source_path, mod_name):
    """以独立模块名加载一份全新的 dispatcher（读当前 os.environ）。"""
    spec = importlib.util.spec_from_file_location(mod_name, str(source_path))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = mod
    try:
        spec.loader.exec_module(mod)
    except Exception:
        sys.modules.pop(mod_name, None)
        raise
    sys.modules.pop(mod_name, None)
    return mod


@pytest.fixture
def dual(monkeypatch, tmp_path):
    """加载一套 dual dispatcher（share_store + user_store），json 侧用临时目录。

    json 实现是共享模块，其 SHARE_FILE/USER_FILE 是模块级常量；这里把它们指到
    每用例独立的临时目录，保证 json 权威侧各用例隔离（pg 侧由 conftest TRUNCATE）。
    """
    import share_store_json
    import user_store_json

    monkeypatch.setenv("STORAGE_BACKEND", "dual")
    data_dir = tmp_path / "share-data"
    data_dir.mkdir()
    monkeypatch.setenv("SHARE_DATA_DIR", str(data_dir))
    # 让 json 实现写/读到本用例目录（模块函数体裸全局读这些常量）
    monkeypatch.setattr(share_store_json, "SHARE_DATA_DIR", data_dir)
    monkeypatch.setattr(share_store_json, "SHARE_FILE", data_dir / "shares.json")
    monkeypatch.setattr(user_store_json, "SHARE_DATA_DIR", data_dir)
    monkeypatch.setattr(user_store_json, "USER_FILE", data_dir / "users.json")
    ss = _load_fresh(_REPO_ROOT / "share_store.py", "dual_share")
    us = _load_fresh(_REPO_ROOT / "user_store.py", "dual_user")
    assert ss.STORAGE_BACKEND == "dual"
    assert us.STORAGE_BACKEND == "dual"
    return ss, us


@pytest.fixture
def pg_conn(pg_uri):
    """直连 pg（dict_row）核对影子副本。"""
    import psycopg
    c = psycopg.connect(pg_uri)
    c.row_factory = psycopg.rows.dict_row
    yield c
    c.close()


def test_dual_create_user_identity(dual, pg_conn):
    ss, us = dual
    u = us.create_user("dual@x.com", "password1", role="user", display_name="Dual")
    assert u and u["user_id"].startswith("usr_")
    with pg_conn.cursor() as cur:
        cur.execute("SELECT user_id, email, display_name FROM users WHERE user_id=%s",
                    (u["user_id"],))
        row = cur.fetchone()
    assert row is not None
    assert row["user_id"] == u["user_id"]  # 身份一致
    assert row["email"] == "dual@x.com"


def test_dual_create_share_identity(dual, pg_conn):
    ss, _us = dual
    s = ss.create_share(["a.svs", "b.svs"], 24, roi_sizes=[6.0],
                        permissions=["view", "annotate"])
    assert s and s["token"]
    with pg_conn.cursor() as cur:
        cur.execute("SELECT token, slides, permissions FROM shares WHERE token=%s",
                    (s["token"],))
        row = cur.fetchone()
    assert row is not None
    assert row["token"] == s["token"]  # token 一致
    assert row["slides"] == ["a.svs", "b.svs"]
    assert row["permissions"] == ["view", "annotate"]


def test_dual_add_roi_identity(dual, pg_conn):
    ss, _us = dual
    ss.create_share(["a.svs"], 24, roi_sizes=[6.0])
    roi = ss.add_roi("admin", "a.svs", "医师A", type="rect", x=10, y=20,
                     side_px=100, size_mm=6.0, shared=True, note="n")
    aid = roi["annotation_id"]
    with pg_conn.cursor() as cur:
        cur.execute("SELECT annotation_id, token, slide, shared, geom, insert_seq "
                    "FROM rois WHERE annotation_id=%s", (aid,))
        row = cur.fetchone()
    assert row is not None
    assert row["annotation_id"] == aid  # annotation_id 一致
    assert row["token"] == "admin"
    assert row["slide"] == "a.svs"
    assert bool(row["shared"]) is True
    assert row["geom"]["x"] == 10 and row["geom"]["side_px"] == 100
    assert row["insert_seq"] == 1  # json 第一个 roi → pg insert_seq=1
    # json 权威侧 index（从 json 读）应为 0
    json_rois = ss.list_rois("admin")
    assert json_rois and json_rois[0]["index"] == 0


def test_dual_claim_share_grant_identity(dual, pg_conn):
    ss, us = dual
    s = ss.create_share(["a.svs"], 24)
    u = us.create_user("g@x.com", "password1", role="user")
    grant = ss.claim_share(s["token"], u["user_id"])
    assert grant and grant["grant_id"]
    with pg_conn.cursor() as cur:
        cur.execute("SELECT id, token, user_id FROM grants WHERE id=%s",
                    (grant["grant_id"],))
        row = cur.fetchone()
    assert row is not None
    assert row["id"] == grant["grant_id"]  # grant_id 一致
    assert row["token"] == s["token"]
    assert row["user_id"] == u["user_id"]


def test_dual_create_project_identity(dual, pg_conn):
    ss, _us = dual
    p = ss.create_project("病例1", note="n", slides=["a.svs", "b.svs"])
    assert p and p["pid"]
    with pg_conn.cursor() as cur:
        cur.execute("SELECT project_id, name FROM projects WHERE project_id=%s",
                    (p["pid"],))
        row = cur.fetchone()
        cur.execute("SELECT slide FROM project_slides WHERE project_id=%s "
                    "ORDER BY position", (p["pid"],))
        slides = [r["slide"] for r in cur.fetchall()]
    assert row is not None
    assert row["project_id"] == p["pid"]  # pid 一致
    assert slides == ["a.svs", "b.svs"]


def test_dual_add_slides_to_project_identity(dual, pg_conn):
    ss, _us = dual
    p = ss.create_project("P", slides=["a.svs"])
    updated = ss.add_slides_to_project(p["pid"], ["b.svs", "a.svs"])  # a 去重
    with pg_conn.cursor() as cur:
        cur.execute("SELECT slide FROM project_slides WHERE project_id=%s "
                    "ORDER BY position", (p["pid"],))
        slides = [r["slide"] for r in cur.fetchall()]
    assert updated["slides"] == ["a.svs", "b.svs"]
    assert slides == ["a.svs", "b.svs"]  # 追加保序一致


def test_dual_json_error_no_pg_write(dual, pg_conn):
    ss, _us = dual
    with pytest.raises(ValueError):
        ss.create_share(["a.svs"], 1, roi_sizes=[99.0])  # 非法 roi_sizes
    with pg_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM shares")
        assert cur.fetchone()["count"] == 0


def test_dual_pg_mirror_failure_does_not_block_json(dual, pg_conn, monkeypatch):
    ss, _us = dual
    # 让 pg 镜像失败（connection 抛异常），json 权威写入仍须成功返回
    import share_store_pg
    monkeypatch.setattr(share_store_pg, "_connect",
                        lambda: (_ for _ in ()).throw(RuntimeError("pg down")))
    s = ss.create_share(["a.svs"], 24)  # 不应抛错
    assert s and s["token"]
    with pg_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM shares")
        assert cur.fetchone()["count"] == 0  # pg 影子未写（失败被吞）
    # json 权威仍在（读 json 路径）
    assert len(ss.list_shares()) == 1


# --------------------------------------------------------------------------- #
# Stage 4-1a：插件安装 / run grant 的 dual 身份一致性
# --------------------------------------------------------------------------- #
def test_dual_plugin_installation_identity(dual, pg_conn):
    ss, _us = dual
    created = ss.create_plugin_installation("histopilot", version="0.1.0")
    iid = created["installation_id"]
    assert iid.startswith("pin_")
    assert "secret" in created and "secret_hash" not in created
    with pg_conn.cursor() as cur:
        cur.execute(
            "SELECT plugin_id, version, enabled, secret_hash "
            "FROM plugin_installations WHERE installation_id=%s", (iid,))
        row = cur.fetchone()
    assert row is not None
    assert row["plugin_id"] == "histopilot" and row["version"] == "0.1.0"
    assert row["enabled"] is True
    # 镜像按明文现算 hash（明文绝不进 pg），与 json 权威可互相验证
    assert row["secret_hash"] and row["secret_hash"] != created["secret"]
    import share_store_json
    assert row["secret_hash"] == share_store_json._hash_installation_secret(created["secret"])
    # rotate：旧 secret 失效，pg 行 hash 同步翻转（result-replay 复用同一镜像）
    rotated = ss.rotate_installation_secret(iid)
    with pg_conn.cursor() as cur:
        cur.execute("SELECT secret_hash FROM plugin_installations WHERE installation_id=%s", (iid,))
        assert cur.fetchone()["secret_hash"] == share_store_json._hash_installation_secret(rotated["secret"])
    # disable：enabled 镜像翻转且不破坏 pg 已存 hash（postgres 单后端语义依赖它）
    ss.set_installation_enabled(iid, False)
    with pg_conn.cursor() as cur:
        cur.execute("SELECT enabled, secret_hash FROM plugin_installations WHERE installation_id=%s", (iid,))
        row = cur.fetchone()
    assert row["enabled"] is False
    assert row["secret_hash"] == share_store_json._hash_installation_secret(rotated["secret"])


def test_dual_run_grant_identity(dual, pg_conn):
    ss, _us = dual
    created = ss.create_plugin_installation("histopilot")
    grant = ss.create_run_grant(created["installation_id"], "a.svs",
                                 session_id="sess_d", created_by_user_id="usr_d")
    gid = grant["grant_id"]
    assert gid.startswith("rgr_")
    with pg_conn.cursor() as cur:
        cur.execute(
            "SELECT installation_id, slide, session_id, created_by_user_id, revoked "
            "FROM run_grants WHERE grant_id=%s", (gid,))
        row = cur.fetchone()
    assert row is not None
    assert row["installation_id"] == created["installation_id"]
    assert row["slide"] == "a.svs" and row["session_id"] == "sess_d"
    assert row["created_by_user_id"] == "usr_d" and row["revoked"] is False
    # 撤销镜像
    assert ss.revoke_run_grant(gid) is True
    with pg_conn.cursor() as cur:
        cur.execute("SELECT revoked, revoked_at FROM run_grants WHERE grant_id=%s", (gid,))
        row = cur.fetchone()
    assert row["revoked"] is True and row["revoked_at"] is not None
    # session 维度列表（读 json 权威）
    assert [g["grant_id"] for g in ss.list_run_grants_for_session("sess_d")] == [gid]
