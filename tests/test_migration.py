# -*- coding: utf-8 -*-
"""JSON → PostgreSQL 迁移工具测试（Stage 3b-3）。

仅当 ``RUN_PG_TESTS=1`` 时真跑（与 test_pg_infra / test_dual_backend 同惯例）：
conftest 已起 pgserver、设 DATABASE_URL + STORAGE_BACKEND=postgres，并提供
``pg_uri`` fixture 与 autouse TRUNCATE。缺 pgserver/psycopg 时整模块 skip。

覆盖：
  - dry-run 计数正确 + 问题清单；
  - apply → verify 0 差异 → 重跑 apply 幂等（计数不翻倍）→ rollback 还原 json；
  - 同名归属冲突切片被跳过且 exit 非零；
  - verify 检出人为制造的 pg 差异（exit 2）。
"""
import importlib.util
import json
import secrets
import time
import uuid
from pathlib import Path

import pytest

# 缺基建依赖时整模块 skip（不 fail）。
pytest.importorskip("pgserver")
pytest.importorskip("psycopg")

import conftest  # noqa: E402
import psycopg  # noqa: E402

pytestmark = pytest.mark.skipif(
    conftest.BACKEND != "postgres",
    reason="迁移工具需 PG（RUN_PG_TESTS=1）",
)

_REPO_ROOT = Path(__file__).resolve().parent.parent


# --------------------------------------------------------------------------- #
# 加载迁移工具模块（scripts/migrate_json_to_pg.py，独立模块名避免与 app 冲突）
# --------------------------------------------------------------------------- #
@pytest.fixture(scope="module")
def mig():
    spec = importlib.util.spec_from_file_location(
        "mig_json_to_pg", str(_REPO_ROOT / "scripts" / "migrate_json_to_pg.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def args_ns():
    """构造一个可属性赋值的 args 命名空间。"""
    class _NS:
        pass
    return _NS()


# --------------------------------------------------------------------------- #
# fixture：带各种边界的 json（owner+user、grant、tombstone roi、public slide_meta、
# project、旧版无 permissions 的 share）+ 真实切片文件（供 content_sha256）
# --------------------------------------------------------------------------- #
@pytest.fixture
def fixture_dir(tmp_path):
    sdd = tmp_path / "share"
    sdd.mkdir()
    upload = tmp_path / "uploads"
    upload.mkdir()

    owner_uid = "usr_owner1"
    user_uid = "usr_user1"
    s1, s2 = "slide_one.svs", "slide_two.svs"
    now = time.time()
    token = "tok_abc123"
    token_old = "tok_old_noperm"

    shares = {
        "shares": {
            token: {
                "slides": [s1, s2], "created_at": now,
                "expires_at": now + 3600, "revoked": False,
                "roi_sizes": [6, 6.5], "permissions": ["view", "annotate"],
                "creator_user_id": owner_uid,
            },
            # 旧版 share：无 permissions / roi_sizes（迁移侧默认处理）
            token_old: {
                "slides": [s1], "created_at": now,
                "expires_at": now + 7200, "revoked": False,
                "creator_user_id": owner_uid,
            },
        },
        "rois": [
            {"token": token, "slide": s1, "label": "L1", "type": "rect",
             "x": 1, "y": 2, "side_px": 10, "size_mm": 6.0, "ts": now,
             "shared": True, "annotation_id": "aid_live", "source": "human",
             "revision": 1, "deleted": False, "owner_user_id": owner_uid,
             "updated_at": now},
            {"token": token, "slide": s2, "label": "L2", "type": "rect",
             "x": 5, "y": 6, "side_px": 20, "size_mm": 6.5, "ts": now,
             "shared": False, "annotation_id": "aid_tomb", "source": "human",
             "revision": 1, "deleted": True, "owner_user_id": owner_uid,
             "updated_at": now},
            {"token": "admin", "slide": s1, "label": "AI", "type": "rect",
             "x": 0, "y": 0, "side_px": 5, "size_mm": 6.0, "ts": now,
             "shared": True, "annotation_id": "aid_admin", "source": "ai",
             "revision": 1, "deleted": False, "updated_at": now},
        ],
        "projects": {
            "prj_1": {"name": "P1", "note": "n1", "slides": [s1, s2],
                      "created_at": now, "owner_user_id": owner_uid},
        },
        "slide_meta": {
            s1: {"alias": "S1", "note": "note1", "owner_user_id": owner_uid,
                 "public": True},
        },
        "change_seq_by_slide": {s1: 2, s2: 1},
        "grants": [
            {"grant_id": "grt_1", "user_id": user_uid, "share_token": token,
             "permissions": ["view"], "claimed_at": now, "revoked_at": None},
        ],
    }
    users = {
        "users": {
            owner_uid: {"user_id": owner_uid, "email": "owner@x.co",
                        "display_name": "owner", "password_hash": "hash_o",
                        "role": "owner", "created_at": now, "disabled": False},
            user_uid: {"user_id": user_uid, "email": "user@x.co",
                       "display_name": "user", "password_hash": "hash_u",
                       "role": "user", "created_at": now, "disabled": False},
        },
        "meta": {"schema_version": 1},
    }
    (sdd / "shares.json").write_text(json.dumps(shares), encoding="utf-8")
    (sdd / "users.json").write_text(json.dumps(users), encoding="utf-8")
    # 真实切片文件（让 content_sha256 有值）
    (upload / s1).write_bytes(b"\x00slice-one-bytes")
    (upload / s2).write_bytes(b"\x00slice-two-bytes")
    return {"sdd": sdd, "upload": upload, "shares": shares, "users": users}


def _run(mig, args_ns, sdd, upload=None, **kw):
    """填充 args 并调用指定 cmd。"""
    for k, v in kw.items():
        setattr(args_ns, k, v)
    args_ns.share_data_dir = str(sdd)
    args_ns.upload_dir = str(upload) if upload else None
    return args_ns


# --------------------------------------------------------------------------- #
# 1. dry-run：计数 + 问题清单
# --------------------------------------------------------------------------- #
def test_dry_run_counts(mig, args_ns, fixture_dir, capsys):
    _run(mig, args_ns, fixture_dir["sdd"], fixture_dir["upload"])
    rc = mig.cmd_dry_run(args_ns)
    assert rc == 0
    out, err = capsys.readouterr()
    assert "users        2" in out
    assert "shares       2" in out
    assert "grants       1" in out
    assert "rois         3" in out
    assert "slide_meta   1" in out
    assert "projects     1" in out
    assert "change_log   3" in out
    # 切片文件都在，无缺失问题
    assert "切片文件缺失" not in out


# --------------------------------------------------------------------------- #
# 2. apply → verify → 幂等 → rollback
# --------------------------------------------------------------------------- #
def test_apply_verify_idempotent_rollback(mig, args_ns, fixture_dir, pg_uri,
                                          tmp_path, capsys):
    sdd, upload = fixture_dir["sdd"], fixture_dir["upload"]
    backup = tmp_path / "bk"

    # apply
    _run(mig, args_ns, sdd, upload, backup_dir=str(backup))
    rc = mig.cmd_apply(args_ns)
    out, err = capsys.readouterr()
    assert rc == 0, "apply 应成功：%s\n%s" % (out, err)

    # verify：0 差异
    _run(mig, args_ns, sdd, upload)
    rc = mig.cmd_verify(args_ns)
    out, err = capsys.readouterr()
    assert rc == 0, "verify 应 0 差异：%s\n%s" % (out, err)
    assert "0 差异" in out

    # 幂等：再 apply 一次，PG 计数不翻倍
    def _pg_counts():
        c = psycopg.connect(pg_uri)
        c.row_factory = psycopg.rows.dict_row
        try:
            with c.cursor() as cur:
                cur.execute(
                    "SELECT (SELECT count(*) FROM users) AS u, "
                    "(SELECT count(*) FROM shares) AS s, "
                    "(SELECT count(*) FROM rois) AS r, "
                    "(SELECT count(*) FROM grants) AS g, "
                    "(SELECT count(*) FROM change_log) AS cl, "
                    "(SELECT count(*) FROM projects) AS p, "
                    "(SELECT count(*) FROM slide_assets) AS sa")
                return dict(cur.fetchone())
        finally:
            c.close()

    after1 = _pg_counts()
    _run(mig, args_ns, sdd, upload, backup_dir=str(tmp_path / "bk2"))
    mig.cmd_apply(args_ns)
    capsys.readouterr()
    after2 = _pg_counts()
    assert after1 == after2, "重跑 apply 应幂等（计数不翻倍）：%s vs %s" % (after1, after2)
    assert after2["u"] == 2 and after2["s"] == 2 and after2["r"] == 3
    assert after2["g"] == 1 and after2["cl"] == 3 and after2["p"] == 1
    # content_sha256 已填（切片文件存在）
    assert after2["sa"] == 2  # s1, s2 两个有 slide_id 的切片

    # rollback：先破坏源 json，再还原，断言还原后 == 原始
    orig_shares = (sdd / "shares.json").read_text(encoding="utf-8")
    (sdd / "shares.json").write_text("{}", encoding="utf-8")  # 破坏
    _run(mig, args_ns, sdd, upload, backup_dir=str(backup), yes=True)
    rc = mig.cmd_rollback(args_ns)
    out, err = capsys.readouterr()
    assert rc == 0, "rollback 应成功：%s" % err
    assert (sdd / "shares.json").read_text(encoding="utf-8") == orig_shares
    assert "STORAGE_BACKEND=json" in out


# --------------------------------------------------------------------------- #
# 3. 同名归属冲突：切片被跳过且 exit 非零
# --------------------------------------------------------------------------- #
def test_conflict_slide_skipped(mig, args_ns, fixture_dir, pg_uri, tmp_path,
                                capsys):
    sdd, upload = fixture_dir["sdd"], fixture_dir["upload"]
    # 预置一个不同归属的 slides 行（owner=usr_seed ≠ json 的 usr_owner1）
    c = psycopg.connect(pg_uri)
    try:
        with c.cursor() as cur:
            cur.execute(
                "INSERT INTO slides (slide_id, legacy_filename, owner_user_id, "
                "public) VALUES (%s, %s, %s, false)",
                ("sld_seed", "slide_one.svs", "usr_seed"))
        c.commit()
    finally:
        c.close()

    _run(mig, args_ns, sdd, upload, backup_dir=str(tmp_path / "bk_conf"))
    rc = mig.cmd_apply(args_ns)
    out, err = capsys.readouterr()
    # 冲突 → exit 非零，且冲突切片被列入清单
    assert rc != 0, "同名归属冲突应 exit 非零"
    assert "同名归属冲突" in out
    assert "slide_one.svs" in out
    assert "usr_seed" in out
    # 其余实体仍正常导入（不因单切片冲突整体失败）
    c = psycopg.connect(pg_uri)
    c.row_factory = psycopg.rows.dict_row
    try:
        with c.cursor() as cur:
            cur.execute("SELECT count(*) AS n FROM users")
            assert int(cur.fetchone()["n"]) == 2  # users 仍导入
    finally:
        c.close()


# --------------------------------------------------------------------------- #
# 4. verify 检出人为差异（改 pg 一行后 exit 2）
# --------------------------------------------------------------------------- #
def test_verify_detects_drift(mig, args_ns, fixture_dir, pg_uri, tmp_path,
                              capsys):
    sdd, upload = fixture_dir["sdd"], fixture_dir["upload"]
    _run(mig, args_ns, sdd, upload, backup_dir=str(tmp_path / "bk_drift"))
    mig.cmd_apply(args_ns)
    capsys.readouterr()

    # 人为改 pg：把一条 roi 的 data.shared 翻转（verify 读 data JSONB 权威）
    c = psycopg.connect(pg_uri)
    try:
        with c.cursor() as cur:
            cur.execute(
                "UPDATE rois SET data = jsonb_set(data, '{shared}', "
                "to_jsonb(NOT (data->>'shared')::boolean)) "
                "WHERE annotation_id=%s",
                ("aid_live",))
        c.commit()
    finally:
        c.close()

    _run(mig, args_ns, sdd, upload)
    rc = mig.cmd_verify(args_ns)
    out, err = capsys.readouterr()
    assert rc == 2, "有差异时 verify 应 exit 2"
    assert "aid_live" in err  # 差异报告列在前 20 条
