# -*- coding: utf-8 -*-
"""scripts/backfill_slide_asset_state.py 测试（slide ID 化重构 P1，合同 §6）。

覆盖回填合同逐条：
  - dry-run 默认只读（default_transaction_read_only 硬保证）——不改任何行；
  - 资产翻转三态：文件+owner 明确 → ready（published_at=mtime /
    accounted_bytes 含 MRXS 伴侣目录与转换 sidecar / original_filename /
    R-02 display_name 三分支 / format_ext 白名单归一；storage_layout 与
    legacy_filename 不动）；缺文件 → failed；owner 空/不存在/禁用 → 保持
    legacy + manual_review（不回落认领平台 owner）；
  - format_ext 不在白名单 → 转 manual_review 不翻转；
  - 关系回填：project_slides / rois / comments / change_log / run_grants /
    ai_session_principals / annotation_access_events / audit_events 按
    legacy 名映射，映射不到保持 NULL = unresolved；shares.slides →
    share_slides 逐 token 展开（含 tombstone 成员、position 按数组序、
    未映射名跳过计数）；slide_view_grants 只统计不再按名匹配；
  - 幂等：再跑一遍翻转/回填计数全 0、库内容不变；分批（batch-size=1）
    与 manual_review 行不造成扫描死循环；单资产失败不拖垮批次；
  - 退出码：0=完成（允许 manual_review/unresolved 计数）；1=工具错误；
  - authorize_read（slide_store）：ready 放行、failed/legacy 拒绝；
  - 安全不变量：绝不 INSERT slides、绝不 UPDATE legacy_filename、不改
    storage_layout。

运行：.venv/bin/python -m pytest tests/test_backfill_slide_asset_state.py -q
（conftest 起内嵌 PG 并设 DATABASE_URL；每用例前 TRUNCATE 业务表）。
"""
import json
import sys
from pathlib import Path

import psycopg
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import backfill_slide_asset_state as bf  # noqa: E402

import slide_store  # noqa: E402


# --------------------------------------------------------------------------- #
# 连接与基础夹具
# --------------------------------------------------------------------------- #
@pytest.fixture
def conn(pg_uri):
    c = psycopg.connect(pg_uri)
    c.row_factory = psycopg.rows.dict_row
    yield c
    c.close()


@pytest.fixture
def up(tmp_path):
    """tmp_path 下的 UPLOAD_DIR。"""
    d = tmp_path / "uploads"
    d.mkdir()
    return d


def _exec(conn, sql, params=()):
    with conn.cursor() as cur:
        cur.execute(sql, params)


def seed_user(conn, user_id, disabled=False):
    _exec(conn,
          "INSERT INTO users (user_id, login_id, role, disabled) "
          "VALUES (%s,%s,'user',%s)",
          (user_id, user_id + "@t.example", disabled))


def seed_slide(conn, slide_id, legacy_filename=None, owner="usr_a",
               alias="", display_name="", public=False, asset_state="legacy"):
    _exec(conn,
          "INSERT INTO slides (slide_id, legacy_filename, owner_user_id, "
          "alias, display_name, public, asset_state) "
          "VALUES (%s,%s,%s,%s,%s,%s,%s)",
          (slide_id, legacy_filename, owner, alias, display_name, public,
           asset_state))
    return slide_id


def run_backfill(up, tmp_path, *, apply=False, batch_size=None, name="r"):
    """跑脚本并返回报告 dict；断言退出码 0。"""
    report_path = tmp_path / ("%s-report.json" % name)
    argv = ["--upload-dir", str(up), "--report", str(report_path)]
    if apply:
        argv.append("--apply")
    if batch_size is not None:
        argv += ["--batch-size", str(batch_size)]
    code = bf.main(argv)
    assert code == 0, "期望退出码 0"
    return json.loads(report_path.read_text(encoding="utf-8"))


def slide_row(conn, slide_id):
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM slides WHERE slide_id=%s", (slide_id,))
        return cur.fetchone()


def table_count(conn, table, where="true", params=()):
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) AS n FROM %s WHERE %s"
                    % (table, where), params)
        return cur.fetchone()["n"]


def dump_table(conn, table, order_col):
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM %s ORDER BY %s" % (table, order_col))
        return [dict(r) for r in cur.fetchall()]


ALL_DUMP_TABLES = (
    ("slides", "slide_id"), ("share_slides", "token, slide_id"),
    ("project_slides", "project_id, slide"), ("rois", "id"),
    ("comments", "comment_id"), ("change_log", "seq"),
    ("run_grants", "grant_id"), ("ai_session_principals", "session_id"),
    ("annotation_access_events", "seq"), ("audit_events", "event_id"),
    ("slide_view_grants", "slide_name, user_id"), ("shares", "token"),
)


def dump_world(conn):
    return {t: dump_table(conn, t, o) for t, o in ALL_DUMP_TABLES}


def seed_full_relations(conn, ok="ok.svs", ghost="ghost.svs"):
    """8 张关系表各一行可映射（ok）+ 一行不可映射（ghost）。"""
    _exec(conn, "INSERT INTO projects (project_id, name) "
                "VALUES ('prj_1', 'P')")
    _exec(conn, "INSERT INTO project_slides (project_id, slide, position) "
                "VALUES ('prj_1', %s, 0), ('prj_1', %s, 1)", (ok, ghost))
    _exec(conn,
          "INSERT INTO rois (id, token, slide, annotation_id, type) VALUES "
          "('roi_ok', 'admin', %s, 'ann_ok', 'rect'), "
          "('roi_ghost', 'admin', %s, 'ann_ghost', 'rect')", (ok, ghost))
    _exec(conn, "INSERT INTO comments (comment_id, slide, token) VALUES "
                "('cmt_ok', %s, 'admin'), ('cmt_ghost', %s, 'admin')",
          (ok, ghost))
    _exec(conn, "INSERT INTO change_log (slide, token, op) VALUES "
                "(%s, 'admin', 'add'), (%s, 'admin', 'add')", (ok, ghost))
    _exec(conn,
          "INSERT INTO run_grants (grant_id, installation_id, slide, "
          "expires_at) VALUES "
          "('rgr_ok', 'inst_1', %s, now() + interval '1 hour'), "
          "('rgr_ghost', 'inst_1', %s, now() + interval '1 hour')",
          (ok, ghost))
    _exec(conn,
          "INSERT INTO ai_session_principals (session_id, user_id, slide) "
          "VALUES ('sess_ok', 'usr_b', %s), ('sess_ghost', 'usr_b', %s)",
          (ok, ghost))
    _exec(conn,
          "INSERT INTO annotation_access_events (seq, slide, annotation_id, "
          "op, grantee_kind, grantee_id) VALUES "
          "(1, %s, 'ann_ok', 'grant', 'user', 'usr_b'), "
          "(2, %s, 'ann_ghost', 'grant', 'user', 'usr_b')", (ok, ghost))
    _exec(conn, "INSERT INTO audit_events (event_id, action, slide) VALUES "
                "('aud_ok', 'x', %s), ('aud_ghost', 'x', %s)", (ok, ghost))


def seed_full_world(conn, up):
    """完整夹具：正常/缺文件/owner 空/owner 不存在 + 全量关系引用。"""
    seed_user(conn, "usr_a")
    seed_user(conn, "usr_b")
    seed_slide(conn, "sld_ok", "ok.svs", owner="usr_a")
    seed_slide(conn, "sld_gone", "gone.svs", owner="usr_a")      # 无文件
    seed_slide(conn, "sld_none", "none.svs", owner=None)          # owner 空
    seed_slide(conn, "sld_ghostowner", "ghostowner.svs",
               owner="usr_ghost")                                  # owner 不存在
    (up / "ok.svs").write_bytes(b"A" * 100)
    (up / "none.svs").write_bytes(b"N" * 6)
    (up / "ghostowner.svs").write_bytes(b"G" * 4)
    seed_full_relations(conn)
    _exec(conn, "INSERT INTO shares (token, slides) VALUES "
                "('sht_1', %s::jsonb)",
          (json.dumps(["ok.svs", "ghost.svs"]),))
    _exec(conn, "INSERT INTO slide_view_grants (slide_name, user_id, "
                "slide_id, expires_at) VALUES ('ok.svs', 'usr_b', NULL, "
                " now() + interval '30 days')")
    conn.commit()


# --------------------------------------------------------------------------- #
# dry-run：默认只读，不改任何行
# --------------------------------------------------------------------------- #
def test_dry_run_changes_nothing(conn, up, tmp_path):
    seed_full_world(conn, up)
    before = dump_world(conn)
    report = run_backfill(up, tmp_path)

    assert report["mode"] == "dry-run"
    a = report["assets"]
    assert a["scanned"] == 4
    assert a["flipped_ready"] == 1
    assert a["failed_missing_file"] == 1
    assert a["manual_review"] == 2
    assert a["manual_review_reasons"] == {"owner_missing": 1,
                                          "owner_unknown": 1}
    assert a["asset_errors"] == 0
    kinds = sorted(i["kind"] for i in report["issues"])
    assert kinds == ["manual_review", "manual_review", "missing_file"]
    # 关系：dry-run 输出计划数——unresolved 统计当次仍 NULL 的行（可映射
    # 行 apply 前也是 NULL，故 1 可映射 + 1 不可映射 = 2）
    for table, _col in bf.RELATION_TABLES:
        assert report["relations"][table] == {"backfilled": 1,
                                              "unresolved": 2}
    s = report["share_slides"]
    assert s["tokens_scanned"] == 1
    assert s["names_seen"] == 2
    assert s["names_mapped"] == 1
    assert s["names_unmapped"] == 1
    assert s["members_inserted"] == 1           # dry-run = 计划插入数
    assert report["slide_view_grants"]["unresolved_null_slide_id"] == 1

    assert dump_world(conn) == before           # 一行未动


def test_dry_run_is_session_read_only(conn, up, tmp_path, monkeypatch):
    """dry-run 的只读是会话级硬保证：即便混入写语句也会被 PG 拒绝。"""
    seed_user(conn, "usr_a")
    conn.commit()
    monkeypatch.setattr(bf, "_backfill_assets",
                        lambda *a, **k: a[0].cursor().execute(
                            "UPDATE slides SET note='x'"))
    argv = ["--upload-dir", str(up)]
    assert bf.main(argv) == 1                   # 写语句被拒 → 工具错误


# --------------------------------------------------------------------------- #
# 资产翻转：ready / failed / manual_review
# --------------------------------------------------------------------------- #
def test_apply_flips_ready_asset(conn, up, tmp_path):
    seed_user(conn, "usr_a")
    seed_slide(conn, "sld_ok", "ok.svs", owner="usr_a")
    conn.commit()
    (up / "ok.svs").write_bytes(b"A" * 100)
    mtime = (up / "ok.svs").stat().st_mtime

    report = run_backfill(up, tmp_path, apply=True)
    assert report["mode"] == "apply"
    assert report["assets"]["flipped_ready"] == 1
    row = slide_row(conn, "sld_ok")
    assert row["asset_state"] == "ready"
    assert row["original_filename"] == "ok.svs"
    assert row["format_ext"] == "svs"
    assert row["accounted_bytes"] == 100
    assert row["display_name"] == "ok.svs"      # alias/display 空 → legacy 名
    assert row["legacy_filename"] == "ok.svs"   # 绝不改冻结别名
    assert row["storage_layout"] == "legacy"    # 不改布局（物理迁移是 P6）
    assert row["storage_relpath"] is None       # 不派生包路径
    assert abs(float(row["published_at"].timestamp()) - mtime) < 2.0
    # 【P6 改写】authorize_read 的 layout 门禁：回填后的 ready 行仍是 legacy
    # 布局 = 待迁移——运行时门禁一律拒（owner 亦然，不泄露存在性）；经
    # bind_id_bundle_layout（迁移 bound 步）翻转后 owner 放行、他人拒绝。
    desc = slide_store.resolve_slide_id("sld_ok")
    assert not slide_store.authorize_read(desc, actor_user_id="usr_a")
    assert slide_store.bind_id_bundle_layout(
        "sld_ok", "objects/sld_ok/data.svs", accounted_bytes=100) == "migrated"
    assert slide_store.authorize_read(desc, actor_user_id="usr_a")
    assert not slide_store.authorize_read(desc, actor_user_id="usr_other")


def test_apply_missing_file_to_failed(conn, up, tmp_path):
    seed_user(conn, "usr_a")
    seed_slide(conn, "sld_gone", "gone.svs", owner="usr_a")
    conn.commit()
    report = run_backfill(up, tmp_path, apply=True)
    assert report["assets"]["failed_missing_file"] == 1
    row = slide_row(conn, "sld_gone")
    assert row["asset_state"] == "failed"       # 保留证据
    assert row["legacy_filename"] == "gone.svs"
    assert row["original_filename"] is None     # failed 不回填展示快照
    missing = [i for i in report["issues"] if i["kind"] == "missing_file"]
    assert missing == [{"kind": "missing_file", "slide_id": "sld_gone",
                        "legacy_filename": "gone.svs",
                        "reason": "missing_file"}]
    assert not slide_store.authorize_read(
        slide_store.resolve_slide_id("sld_gone"), actor_user_id="usr_a")


def test_apply_owner_ambiguity_stays_legacy(conn, up, tmp_path):
    seed_user(conn, "usr_a")
    seed_user(conn, "usr_dis", disabled=True)
    seed_slide(conn, "sld_none", "none.svs", owner=None)
    seed_slide(conn, "sld_ghost", "ghostowner.svs", owner="usr_ghost")
    seed_slide(conn, "sld_dis", "disowner.svs", owner="usr_dis")
    conn.commit()
    for name in ("none.svs", "ghostowner.svs", "disowner.svs"):
        (up / name).write_bytes(b"x" * 3)

    report = run_backfill(up, tmp_path, apply=True)
    assert report["assets"]["manual_review"] == 3
    assert report["assets"]["manual_review_reasons"] == {
        "owner_missing": 1, "owner_unknown": 1, "owner_disabled": 1}
    for slide_id in ("sld_none", "sld_ghost", "sld_dis"):
        row = slide_row(conn, slide_id)
        assert row["asset_state"] == "legacy"    # 保持 legacy 不动
        assert row["original_filename"] is None and row["format_ext"] is None
    # 不回落认领平台 owner：owner 原值保持
    assert slide_row(conn, "sld_none")["owner_user_id"] is None
    assert slide_row(conn, "sld_ghost")["owner_user_id"] == "usr_ghost"
    # legacy 状态一律不可读（authorize_read 门禁）
    for slide_id in ("sld_none", "sld_ghost", "sld_dis"):
        assert not slide_store.authorize_read(
            slide_store.resolve_slide_id(slide_id), actor_user_id="usr_a")
    assert not slide_store.authorize_read(
        slide_store.resolve_slide_id("sld_dis"), actor_user_id="usr_dis")


def test_accounted_bytes_mrxs_companion_and_sidecars(conn, up, tmp_path):
    seed_user(conn, "usr_a")
    seed_slide(conn, "sld_m", "m.mrxs", owner="usr_a")
    seed_slide(conn, "sld_c", "c.tif", owner="usr_a")
    seed_slide(conn, "sld_p", "p.svs", owner="usr_a")
    conn.commit()
    (up / "m.mrxs").write_bytes(b"M" * 10)
    comp = up / "m"                              # MRXS 同 stem 伴侣目录
    (comp / "sub").mkdir(parents=True)
    (comp / "sub" / "a.dat").write_bytes(b"a" * 30)
    (comp / "b.dat").write_bytes(b"b" * 5)
    (up / "c.tif").write_bytes(b"C" * 20)        # 转换 canonical sidecar
    (up / "c.tif.manifest.json").write_bytes(b"{}" * 3)   # 6 字节
    assoc = up / "c.tif.associated"
    assoc.mkdir()
    (assoc / "x.bin").write_bytes(b"X" * 11)
    (up / "p.svs").write_bytes(b"P" * 50)
    stray = up / "p"                             # 非 MRXS 的同 stem 目录不计
    stray.mkdir()
    (stray / "huge.bin").write_bytes(b"H" * 999)

    report = run_backfill(up, tmp_path, apply=True)
    assert report["assets"]["flipped_ready"] == 3
    assert slide_row(conn, "sld_m")["accounted_bytes"] == 10 + 30 + 5
    assert slide_row(conn, "sld_c")["accounted_bytes"] == 20 + 6 + 11
    assert slide_row(conn, "sld_p")["accounted_bytes"] == 50
    assert slide_row(conn, "sld_m")["format_ext"] == "mrxs"


def test_display_name_backfill_branches(conn, up, tmp_path):
    seed_user(conn, "usr_a")
    seed_slide(conn, "sld_alias", "one.svs", owner="usr_a",
               alias="Alias One", display_name="D1")
    seed_slide(conn, "sld_disp", "two.svs", owner="usr_a",
               alias="", display_name="Kept Display")
    seed_slide(conn, "sld_blank", "three.svs", owner="usr_a",
               alias="   ", display_name="")
    conn.commit()
    for name in ("one.svs", "two.svs", "three.svs"):
        (up / name).write_bytes(b"z" * 4)

    run_backfill(up, tmp_path, apply=True)
    # R-02：非空 alias → alias；否则现有 display_name；否则 legacy_filename
    assert slide_row(conn, "sld_alias")["display_name"] == "Alias One"
    assert slide_row(conn, "sld_disp")["display_name"] == "Kept Display"
    assert slide_row(conn, "sld_blank")["display_name"] == "three.svs"
    # alias 列本身不改写（停写但不回改历史值）
    assert slide_row(conn, "sld_alias")["alias"] == "Alias One"


def test_format_ext_whitelist(conn, up, tmp_path):
    seed_user(conn, "usr_a")
    seed_slide(conn, "sld_noext", "noext", owner="usr_a")
    seed_slide(conn, "sld_bad", "bad.s+v", owner="usr_a")
    seed_slide(conn, "sld_long", "long." + "a" * 17, owner="usr_a")
    seed_slide(conn, "sld_upper", "UP.SVS", owner="usr_a")
    seed_slide(conn, "sld_max", "max." + "b" * 16, owner="usr_a")
    conn.commit()
    for name in ("noext", "bad.s+v", "long." + "a" * 17, "UP.SVS",
                 "max." + "b" * 16):
        (up / name).write_bytes(b"q" * 2)

    report = run_backfill(up, tmp_path, apply=True)
    # 白名单 ^[a-z0-9]{1,16}$ 之外 → 转 review 不翻转
    for slide_id in ("sld_noext", "sld_bad", "sld_long"):
        assert slide_row(conn, slide_id)["asset_state"] == "legacy"
    assert report["assets"]["manual_review_reasons"]["bad_format_ext"] == 3
    assert report["assets"]["flipped_ready"] == 2
    # 后缀归一：大写带点 → 小写；16 位边界合法
    assert slide_row(conn, "sld_upper")["format_ext"] == "svs"
    assert slide_row(conn, "sld_max")["format_ext"] == "b" * 16


def test_safety_invariants_no_insert_no_rename_no_layout(conn, up, tmp_path):
    seed_user(conn, "usr_a")
    seed_slide(conn, "sld_ok", "ok.svs", owner="usr_a")
    conn.commit()
    (up / "ok.svs").write_bytes(b"A" * 7)
    before_count = table_count(conn, "slides")
    run_backfill(up, tmp_path, apply=True)
    assert table_count(conn, "slides") == before_count   # 绝不 INSERT slides
    row = slide_row(conn, "sld_ok")
    assert row["legacy_filename"] == "ok.svs"            # 绝不 UPDATE 别名
    assert row["storage_layout"] == "legacy"             # 不改布局
    assert row["asset_state"] == "ready"


# --------------------------------------------------------------------------- #
# 关系回填
# --------------------------------------------------------------------------- #
def test_relations_backfill_mixed(conn, up, tmp_path):
    seed_user(conn, "usr_a")
    seed_slide(conn, "sld_ok", "ok.svs", owner="usr_a")
    conn.commit()
    (up / "ok.svs").write_bytes(b"A" * 10)
    seed_full_relations(conn)
    conn.commit()

    report = run_backfill(up, tmp_path, apply=True)
    for table, _col in bf.RELATION_TABLES:
        assert report["relations"][table] == {"backfilled": 1,
                                              "unresolved": 1}
    with conn.cursor() as cur:
        for table in ("project_slides", "rois", "comments", "change_log",
                      "run_grants", "ai_session_principals",
                      "annotation_access_events", "audit_events"):
            cur.execute("SELECT slide_id FROM %s WHERE slide = 'ok.svs'"
                        % table)
            assert cur.fetchone()["slide_id"] == "sld_ok"
            cur.execute("SELECT slide_id FROM %s WHERE slide = 'ghost.svs'"
                        % table)
            assert cur.fetchone()["slide_id"] is None   # unresolved 保持 NULL


def test_share_slides_backfill_with_tombstone(conn, up, tmp_path):
    seed_user(conn, "usr_a")
    seed_slide(conn, "sld_a", "a.svs", owner="usr_a")
    seed_slide(conn, "sld_b", "b.svs", owner="usr_a")
    seed_slide(conn, "sld_del", "del.svs", owner="usr_a",
               asset_state="deleted")          # tombstone 也保留成员映射
    conn.commit()
    for name in ("a.svs", "b.svs"):
        (up / name).write_bytes(b"S" * 5)
    # 名数组含不可映射名 ghost.svs：跳过并计数；position 按数组序
    _exec(conn, "INSERT INTO shares (token, slides) VALUES "
                "('sht_1', %s::jsonb)",
          (json.dumps(["a.svs", "ghost.svs", "b.svs", "del.svs"]),))
    conn.commit()

    report = run_backfill(up, tmp_path, apply=True)
    s = report["share_slides"]
    assert s == {"tokens_scanned": 1, "names_seen": 4, "names_mapped": 3,
                 "names_unmapped": 1, "members_inserted": 3}
    with conn.cursor() as cur:
        cur.execute("SELECT token, slide_id, position FROM share_slides "
                    "ORDER BY position")
        rows = [(r["token"], r["slide_id"], r["position"])
                for r in cur.fetchall()]
    assert rows == [("sht_1", "sld_a", 0), ("sht_1", "sld_b", 2),
                    ("sht_1", "sld_del", 3)]   # ghost 不产生行
    # tombstone 成员：映射保留，但授权门禁按 state 拒绝（owner 也不可读）
    assert not slide_store.authorize_read(
        slide_store.resolve_slide_id("sld_del"), actor_user_id="usr_a")


def test_view_grants_only_counted_no_name_matching(conn, up, tmp_path):
    seed_user(conn, "usr_a")
    seed_user(conn, "usr_b")
    seed_slide(conn, "sld_ok", "ok.svs", owner="usr_a")
    conn.commit()
    (up / "ok.svs").write_bytes(b"A" * 10)
    _exec(conn, "INSERT INTO slide_view_grants (slide_name, user_id, "
                "slide_id, expires_at) VALUES ('ok.svs', 'usr_b', NULL, "
                " now() + interval '30 days'), "
                "('ok.svs', 'usr_c', 'sld_ok', "
                " now() + interval '30 days')")
    conn.commit()

    report = run_backfill(up, tmp_path, apply=True)
    assert report["slide_view_grants"]["unresolved_null_slide_id"] == 1
    with conn.cursor() as cur:
        cur.execute("SELECT user_id, slide_id FROM slide_view_grants "
                    "ORDER BY user_id")
        rows = {r["user_id"]: r["slide_id"] for r in cur.fetchall()}
    # 不再按名匹配：NULL 行保持 NULL（R-06：旧名授权不重绑）
    assert rows == {"usr_b": None, "usr_c": "sld_ok"}


# --------------------------------------------------------------------------- #
# 幂等 / 分批 / 容错 / 退出码
# --------------------------------------------------------------------------- #
def seed_ok_only_relations(conn, ok="ok.svs"):
    """8 张关系表各一行可映射（ok）——幂等用例：二跑 unresolved 必须为 0。"""
    _exec(conn, "INSERT INTO projects (project_id, name) "
                "VALUES ('prj_1', 'P')")
    _exec(conn, "INSERT INTO project_slides (project_id, slide) "
                "VALUES ('prj_1', %s)", (ok,))
    _exec(conn, "INSERT INTO rois (id, token, slide, annotation_id, type) "
                "VALUES ('roi_ok', 'admin', %s, 'ann_ok', 'rect')", (ok,))
    _exec(conn, "INSERT INTO comments (comment_id, slide, token) "
                "VALUES ('cmt_ok', %s, 'admin')", (ok,))
    _exec(conn, "INSERT INTO change_log (slide, token, op) "
                "VALUES (%s, 'admin', 'add')", (ok,))
    _exec(conn, "INSERT INTO run_grants (grant_id, installation_id, slide, "
                "expires_at) VALUES ('rgr_ok', 'inst_1', %s, "
                "now() + interval '1 hour')", (ok,))
    _exec(conn, "INSERT INTO ai_session_principals (session_id, user_id, "
                "slide) VALUES ('sess_ok', 'usr_b', %s)", (ok,))
    _exec(conn, "INSERT INTO annotation_access_events (seq, slide, "
                "annotation_id, op, grantee_kind, grantee_id) VALUES "
                "(1, %s, 'ann_ok', 'grant', 'user', 'usr_b')", (ok,))
    _exec(conn, "INSERT INTO audit_events (event_id, action, slide) "
                "VALUES ('aud_ok', 'x', %s)", (ok,))


def test_idempotent_second_run_all_zero_and_unchanged(conn, up, tmp_path):
    seed_user(conn, "usr_a")
    seed_slide(conn, "sld_ok", "ok.svs", owner="usr_a")
    seed_slide(conn, "sld_gone", "gone.svs", owner="usr_a")   # 缺文件
    conn.commit()
    (up / "ok.svs").write_bytes(b"A" * 10)
    seed_ok_only_relations(conn)
    _exec(conn, "INSERT INTO shares (token, slides) VALUES "
                "('sht_1', %s::jsonb)", (json.dumps(["ok.svs"]),))
    conn.commit()

    first = run_backfill(up, tmp_path, apply=True, name="r1")
    assert first["assets"]["flipped_ready"] == 1
    assert first["assets"]["failed_missing_file"] == 1
    assert all(c["backfilled"] == 1 for c in first["relations"].values())
    assert first["share_slides"]["members_inserted"] == 1
    snapshot = dump_world(conn)

    second = run_backfill(up, tmp_path, apply=True, name="r2")
    a = second["assets"]
    assert a["scanned"] == 0                    # legacy 已清零（checkpoint=DB）
    assert a["flipped_ready"] == 0 and a["failed_missing_file"] == 0
    assert a["manual_review"] == 0 and a["asset_errors"] == 0
    assert all(c["backfilled"] == 0 and c["unresolved"] == 0
               for c in second["relations"].values())
    assert second["share_slides"]["members_inserted"] == 0
    assert dump_world(conn) == snapshot         # 库内容一字不差


def test_batching_small_batches_and_manual_review_no_loop(conn, up,
                                                          tmp_path):
    seed_user(conn, "usr_a")
    for i in range(3):
        seed_slide(conn, "sld_ok%d" % i, "ok%d.svs" % i, owner="usr_a")
        (up / ("ok%d.svs" % i)).write_bytes(b"O" * 4)
    seed_slide(conn, "sld_none", "none.svs", owner=None)     # 粘滞 legacy
    (up / "none.svs").write_bytes(b"N" * 4)
    seed_slide(conn, "sld_ghost", "ghostowner.svs", owner="usr_ghost")
    (up / "ghostowner.svs").write_bytes(b"G" * 4)
    conn.commit()

    # batch-size=1：manual_review 行保持 legacy 不动，键集分页越过它们，
    # 扫描必须收敛（否则本用例永不返回）
    report = run_backfill(up, tmp_path, apply=True, batch_size=1)
    assert report["assets"]["scanned"] == 5
    assert report["assets"]["flipped_ready"] == 3
    assert report["assets"]["manual_review"] == 2
    for i in range(3):
        assert slide_row(conn, "sld_ok%d" % i)["asset_state"] == "ready"
    assert slide_row(conn, "sld_none")["asset_state"] == "legacy"


def test_single_asset_failure_does_not_break_batch(conn, up, tmp_path,
                                                   monkeypatch):
    seed_user(conn, "usr_a")
    seed_slide(conn, "sld_boom", "boom.svs", owner="usr_a")
    seed_slide(conn, "sld_ok", "ok.svs", owner="usr_a")
    conn.commit()
    (up / "boom.svs").write_bytes(b"B" * 8)
    (up / "ok.svs").write_bytes(b"A" * 8)

    real_stat = bf.stat_asset

    def flaky(upload_dir, legacy_filename):
        if legacy_filename == "boom.svs":
            raise OSError("simulated stat failure")
        return real_stat(upload_dir, legacy_filename)

    monkeypatch.setattr(bf, "stat_asset", flaky)
    report = run_backfill(up, tmp_path, apply=True, batch_size=1)
    assert report["assets"]["asset_errors"] == 1
    assert report["assets"]["flipped_ready"] == 1   # 同批其它资产照常翻转
    assert slide_row(conn, "sld_boom")["asset_state"] == "legacy"  # 不动
    assert slide_row(conn, "sld_ok")["asset_state"] == "ready"
    errs = [i for i in report["issues"] if i["kind"] == "asset_error"]
    assert errs and errs[0]["reason"] == "stat_failed"


def test_missing_upload_dir_is_tool_error(conn, up, tmp_path, monkeypatch):
    monkeypatch.delenv("UPLOAD_DIR", raising=False)
    assert bf.main(["--batch-size", "10"]) == 1          # 无 --upload-dir
    assert bf.main(["--upload-dir", str(tmp_path / "nope")]) == 1
    assert bf.main(["--upload-dir", str(up), "--batch-size", "0"]) == 1


# --------------------------------------------------------------------------- #
# 回填后端到端：ready 经 legacy alias 解析 + 授权
# --------------------------------------------------------------------------- #
def test_end_to_end_legacy_alias_ready_readable(conn, up, tmp_path):
    seed_user(conn, "usr_a")
    seed_slide(conn, "sld_leg", "legacy-hist.svs", owner="usr_a",
               alias="历史别名")
    conn.commit()
    (up / "legacy-hist.svs").write_bytes(b"L" * 33)
    run_backfill(up, tmp_path, apply=True)

    desc = slide_store.resolve_legacy_alias("legacy-hist.svs")
    assert desc is not None and desc.asset_state == "ready"
    assert desc.display_name == "历史别名"       # R-02 alias 优先
    assert desc.original_filename == "legacy-hist.svs"
    assert desc.format_ext == "svs" and desc.accounted_bytes == 33
    assert desc.storage_layout == "legacy"
    # 【P6 改写】legacy 布局 = 待迁移：门禁拒；迁移翻转后按 owner 放行
    assert not slide_store.authorize_read(desc, actor_user_id="usr_a")
    assert slide_store.bind_id_bundle_layout(
        desc.slide_id, "objects/%s/data.svs" % desc.slide_id,
        accounted_bytes=33) == "migrated"
    assert slide_store.authorize_read(desc, actor_user_id="usr_a")
    assert not slide_store.authorize_read(desc, actor_user_id="usr_b")
