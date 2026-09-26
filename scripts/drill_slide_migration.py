#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""drill_slide_migration —— P6 合成副本迁移演练驱动（合同 §2 六项全覆盖）。

在**合成副本环境**（内嵌 pgserver + 临时 UPLOAD_DIR）完整走一遍生产迁移管线：

    种子世界 → backfill --apply（逻辑状态回填）→ P0 冻结审计（frozen）→
    plan（确定性计划）→ migrate --apply（含三处崩溃注入的中断恢复）→
    verify（独立核验）→ 断言 §2 六项 → 证据落 docs/drill-evidence-<date>/

本脚本不操作生产、不部署、不打开 COS capability（合同范围边界）。二进制
大文件不入仓——证据目录只落文本（计划头摘要、journal、verification.json、
summary.md、演练报告）。

§2 六项与本脚本的对应：
  1. 全受支持格式代表样本：svs/tif 单文件、mrxs+伴侣目录、kfb→tif 转换
     产物形态（含 .manifest.json/.associated 派生物留置）、kfbf→ome.tif。
  2. 历史关系全谱：share_slides 成员（含领取）、显式 view grant、项目成员、
     rois/comments/change_log 标注、demo 目录、run grants、AI principals。
  3. 中断恢复：copied 后杀进程重跑不重复复制；after_publish（FS 已发布、
     DB 未绑定）重跑经 journal+manifest 幂等复用；bound 后重跑只补 postverify。
  4. 隔离类：missing_file→retain_history(failed)；owner 空/symlink/活跃
     任务→quarantine（不可读、不进 ready 列表、报告披露）。
  5. 同 legacy 名 tombstone + 新同名 id_bundle 资产并存：迁移不碰
     tombstone；旧分享领取人对新资产 authorize_read=False（不复活）。
  6. 证据：drill-report.md + 计划/journal/verification 副本落证据目录。

用法（演练，独立进程运行；不进 pytest 套件）::

    .venv/bin/python scripts/drill_slide_migration.py \\
        [--work-root /tmp/drill] [--evidence-dir docs/drill-evidence-20260925]

测试复用：``build_world()`` / ``run_pipeline_once()`` 被
tests/test_slide_migration_tools.py import 作夹具（同一种子、更细粒度断言）。
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))
sys.path.insert(0, str(_REPO_ROOT / "scripts"))

import psycopg  # noqa: E402

import pg_store  # noqa: E402
import pgserver  # noqa: E402
import slide_storage  # noqa: E402
import slide_store  # noqa: E402

import backfill_slide_asset_state as backfill  # noqa: E402
import migrate_slide_storage as migrator  # noqa: E402
import plan_slide_migration as planner  # noqa: E402
import verify_slide_migration as verifier  # noqa: E402
import slide_io  # noqa: E402

sys.path.insert(0, str(_REPO_ROOT / "tests"))
import _tiff_fixtures  # noqa: E402  合成 TIFF/OME-TIFF 字节的仓内唯一实现

DRILL_TAG = "20260925"

# --------------------------------------------------------------------------- #
# 合成世界（tests/ 演练共用夹具）
# --------------------------------------------------------------------------- #

ALICE = "usr_alice"
BOB = "usr_bob"
TOK_SHARE = "tok_drill_share_1"        # 旧分享（specimen.svs，成员=BOB）
TOK_TOMB = "tok_drill_tomb_1"          # tombstone 分享（reborn.svs，成员=BOB）

#: migrate 人群（期望全部 postverified）
MIGRATE_IDS = ("sld_drill_kfbp01", "sld_drill_mrxs01", "sld_drill_ome01",
               "sld_drill_svs01", "sld_drill_tif01")
#: 隔离/保留人群
RETAIN_IDS = ("sld_drill_miss01",)
QUARANTINE_IDS = ("sld_drill_inflight01", "sld_drill_link01",
                  "sld_drill_noown01")
TOMB_ID = "sld_drill_tomb01"
REBORN_ID = None  # allocate_slide 随机生成；build_world 返回


def _exec(conn, sql, params=()):
    with conn.cursor() as cur:
        cur.execute(sql, params)


def _seed_users(conn):
    _exec(conn,
          "INSERT INTO users (user_id, login_id, role) VALUES "
          "(%s,'alice@drill.example','user'), (%s,'bob@drill.example','user')",
          (ALICE, BOB))


def _seed_slides(conn):
    """legacy 平铺行（0067 默认态）+ tombstone + 新同名 id_bundle 资产。"""
    _exec(conn,
          "INSERT INTO slides (slide_id, legacy_filename, owner_user_id, "
          "public, asset_state) VALUES "
          "('sld_drill_svs01','specimen.svs',%s,false,'legacy'), "
          "('sld_drill_tif01','scan.tif',%s,false,'legacy'), "
          "('sld_drill_mrxs01','panel.mrxs',%s,false,'legacy'), "
          "('sld_drill_kfbp01','kfb-converted.tif',%s,false,'legacy'), "
          "('sld_drill_ome01','kfbf-out.ome.tif',%s,false,'legacy'), "
          "('sld_drill_miss01','gone.svs',%s,false,'legacy'), "
          "('sld_drill_noown01','no-owner.svs',NULL,false,'legacy'), "
          "('sld_drill_link01','linked.svs',%s,false,'legacy'), "
          "('sld_drill_inflight01','inflight.tif',%s,false,'legacy')",
          (ALICE, ALICE, ALICE, BOB, BOB, ALICE, ALICE, ALICE))
    # tombstone：已删除但保留冻结别名（UNIQUE 阻止旧别名重绑新 ID）
    _exec(conn,
          "INSERT INTO slides (slide_id, legacy_filename, owner_user_id, "
          "public, asset_state, accounted_bytes, deleted_at) VALUES "
          "(%s,'reborn.svs',%s,false,'deleted',4096,now())",
          (TOMB_ID, BOB))


def _seed_relations(conn):
    """specimen.ssv/svs 的历史关系全谱（名基列——由 backfill 回填 slide_id）。"""
    _exec(conn, "INSERT INTO shares (token, slides) VALUES "
                "(%s, %s::jsonb), (%s, %s::jsonb)",
          (TOK_SHARE, json.dumps(["specimen.svs"]),
           TOK_TOMB, json.dumps(["reborn.svs"])))
    _exec(conn,
          "INSERT INTO grants (id, token, user_id, active) VALUES "
          "('grt_drill_1', %s, %s, true), ('grt_drill_2', %s, %s, true)",
          (TOK_SHARE, BOB, TOK_TOMB, BOB))
    _exec(conn,
          "INSERT INTO slide_view_grants (slide_name, user_id, slide_id) "
          "VALUES ('specimen.svs', %s, 'sld_drill_svs01')", (BOB,))
    _exec(conn, "INSERT INTO projects (project_id, name) "
                "VALUES ('prj_drill_1', '迁移演练项目')")
    _exec(conn,
          "INSERT INTO project_slides (project_id, slide) "
          "VALUES ('prj_drill_1', 'specimen.svs')")
    _exec(conn,
          "INSERT INTO rois (id, token, slide, annotation_id, type) VALUES "
          "('roi_drill_1', 'admin', 'specimen.svs', 'ann_d1', 'rect')")
    _exec(conn,
          "INSERT INTO comments (comment_id, slide, token) "
          "VALUES ('cmt_drill_1', 'specimen.svs', 'admin')")
    _exec(conn,
          "INSERT INTO change_log (slide, token, op) "
          "VALUES ('specimen.svs', 'admin', 'add')")
    _exec(conn,
          "INSERT INTO run_grants (grant_id, installation_id, slide, "
          "expires_at) VALUES ('rgr_drill_1', 'inst_drill_1', "
          "'specimen.svs', now() + interval '1 day')")
    _exec(conn,
          "INSERT INTO ai_session_principals (session_id, user_id, slide) "
          "VALUES ('sess_drill_1', %s, 'specimen.svs')", (BOB,))
    _exec(conn,
          "INSERT INTO annotation_access_events (seq, slide, annotation_id, "
          "op, grantee_kind, grantee_id) VALUES "
          "(1, 'specimen.svs', 'ann_d1', 'grant', 'user', %s)", (BOB,))
    _exec(conn,
          "INSERT INTO demo_catalog (slide_id, display_name) "
          "VALUES ('sld_drill_svs01', '迁移演示切片')")
    # 活跃任务（inflight.tif）→ 审计 active_task → 计划隔离（排空义务披露）
    _exec(conn,
          "INSERT INTO upload_tasks (upload_id, owner_user_id, filename, "
          "safe_name, declared_size, chunk_size, expires_at, state) "
          "VALUES ('upt_drill_1', %s, 'inflight.tif', 'inflight.tif', 16, 4, "
          "now() + interval '1 day', 'active')", (ALICE,))


def _seed_files(upload_dir: Path, work_root: Path):
    """合成物理世界（真实可开 TIFF 族 + MRXS 伴侣结构 + kfb 派生物 + 孤儿）。

    tiff 族（svs/tif/ome.tif）用 tests/_tiff_fixtures 的真实字节——真
    openslide（本机已装）generic-tiff/TiffFileSlide 可真实试开；MRXS 是厂商
    私有格式，合成环境只构造入口+伴侣目录结构，试开经 _install_mrxs_open_
    stub（披露：生产终审须以真实 MRXS 样本试开，runbook §5.8）。
    """
    up = upload_dir
    (up / "specimen.svs").write_bytes(_tiff_fixtures.make_tiff_bytes())
    (up / "scan.tif").write_bytes(_tiff_fixtures.make_tiff_bytes())
    (up / "panel.mrxs").write_bytes(b"MRXS-HEADER-SYNTH-" + b"z" * 44)  # 64B
    comp = up / "panel"
    comp.mkdir()
    (comp / "Slidedata.ini").write_bytes(b"[GENERAL]\nVersion=2\n" + b"a" * 8)
    (comp / "Index.dat").write_bytes(b"I" * 32)
    # kfb 转换产物形态：入口 + 可重建派生物（.manifest.json/.associated/，
    # P0 白名单口径——计划披露留置，不入迁移包）
    (up / "kfb-converted.tif").write_bytes(
        _tiff_fixtures.make_tiff_bytes())
    (up / "kfb-converted.tif.manifest.json").write_bytes(b'{"v":1}')
    assoc = up / "kfb-converted.tif.associated"
    assoc.mkdir()
    (assoc / "tile.bin").write_bytes(b"t" * 16)
    # kfbf→ome 产物形态（真实 OME-TIFF 字节——TiffFileSlide 可开）
    (up / "kfbf-out.ome.tif").write_bytes(
        _tiff_fixtures.make_ome_tiff_bytes())
    # retain_history：行在、文件缺（gone.svs 不创建）
    # quarantine：owner 空的行有文件
    (up / "no-owner.svs").write_bytes(b"N" * 24)
    # quarantine：symlink 入口（backfill 的 is_file() 跟随链接会翻 ready——
    # 计划按审计 blocker 隔离；migrator 对 ready 隔离项 force_fail 收口）
    target = work_root / "linked-target.bin"
    target.write_bytes(b"L" * 32)
    os.symlink(target, up / "linked.svs")
    (up / "inflight.tif").write_bytes(b"W" * 16)
    # 孤儿文件（无行）→ quarantine
    (up / "orphan-slide.svs").write_bytes(b"O" * 48)


def _seed_reborn(conn, upload_dir: Path) -> str:
    """新同名 id_bundle 资产（original_filename=reborn.svs；走真实发布路径）。"""
    desc = slide_store.allocate_slide(
        ALICE, "reborn.svs", "svs", conn=None)
    staging = upload_dir / ".staging" / ("seed-" + desc.slide_id)
    staging.mkdir(parents=True, exist_ok=True)
    payload = _tiff_fixtures.make_tiff_bytes()  # 真实可开（试开门禁）
    (staging / "data.svs").write_bytes(payload)
    manifest = {"entry": "data.svs",
                "files": [{"path": "data.svs", "size": len(payload),
                           "sha256": hashlib.sha256(payload).hexdigest()}]}
    slide_storage.publish_bundle_no_clobber(
        staging, desc.slide_id, manifest, root=upload_dir)
    with pg_store.transaction(conn):
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE slides SET asset_state='ready', published_at=now(), "
                "accounted_bytes=%s WHERE slide_id=%s AND asset_state=%s",
                (len(payload), desc.slide_id, "staging"))
    return desc.slide_id


def _seed_quotas(conn, upload_dir):
    """backfill 后校准：ALICE 精确对齐；BOB 带 tombstone 不退款差（披露演练）。"""
    _exec(conn,
          "INSERT INTO upload_user_quotas (user_id, quota_bytes, used_bytes, "
          "reserved_bytes) VALUES (%s, 100000000, 0, 0), (%s, 100000000, 0, 0)",
          (ALICE, BOB))
    conn.commit()
    backfill.run_backfill(upload_dir=str(upload_dir), apply=True)
    for owner, extra in ((ALICE, 0), (BOB, 4096)):  # BOB: deleted 不退款差
        _exec(conn,
              "UPDATE upload_user_quotas SET used_bytes = "
              "(SELECT COALESCE(sum(accounted_bytes),0) FROM slides "
              " WHERE owner_user_id=%s AND asset_state IN ('ready','deleting')"
              ") + %s WHERE user_id=%s",
              (owner, extra, owner))
    conn.commit()


def build_world(conn, upload_dir: Path, work_root: Path) -> dict:
    """种子合成世界；返回世界清单 dict（reborn slide_id 等）。"""
    _seed_users(conn)
    _seed_slides(conn)
    _seed_relations(conn)
    _seed_files(upload_dir, work_root)
    reborn_id = _seed_reborn(conn, upload_dir)
    conn.commit()
    _seed_quotas(conn, upload_dir)
    return {"reborn_id": reborn_id, "upload_dir": str(upload_dir)}


# --------------------------------------------------------------------------- #
# 管线
# --------------------------------------------------------------------------- #
def _install_mrxs_open_stub() -> None:
    """MRXS 代表性试开的合成 stub（进程内；其余格式走真 slide_io）。

    真 openslide 的 mirax 驱动需要真实厂商数据集（Slidedata 索引/层级），
    合成演练无法构造；与 tests/_bootstrap.py 对 openslide 的 stub 先例同
    理——但这里只对 .mrxs 命中生效，svs/tif/ome 仍走真实打开。披露见
    drill-report；生产终审按 runbook §5.8 用真实样本试开。
    """
    if getattr(slide_io, "_mrxs_stub_installed", False):
        return  # 幂等（tests 复用时防嵌套包装）
    real_open = slide_io.open_slide

    def wrapped(path, *, format_hint=None):
        hint = str(format_hint or path)
        if hint.lower().endswith(".mrxs"):
            return object()
        return real_open(path, format_hint=format_hint)

    wrapped._mrxs_stub_installed = True  # type: ignore[attr-defined]
    slide_io.open_slide = wrapped
    slide_io._mrxs_stub_installed = True  # type: ignore[attr-defined]


def load_audit_tool():
    spec = importlib.util.spec_from_file_location(
        "audit_slide_identity_drill",
        str(_REPO_ROOT / "scripts" / "audit_slide_identity.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def run_pipeline_once(work_root: Path, *, with_crashes: bool = True) -> dict:
    """完整跑一遍管线（含崩溃注入）；返回 {evidence paths + 断言用数据}。

    独立 pgserver + 独立 UPLOAD_DIR（测试与演练共用；测试传 tmp_path）。
    """
    work = work_root
    work.mkdir(parents=True, exist_ok=True)
    upload_dir = work / "uploads"
    upload_dir.mkdir(exist_ok=True)
    audit_out = work / "audit-out"
    plan_path = work / "migration-plan.jsonl"
    verify_out = work / "verify-out"
    evidence = {}

    pg_data = tempfile.mkdtemp(prefix="drill-pg-")
    server = pgserver.get_server(pg_data, cleanup_mode="delete")
    uri = server.get_uri()
    os.environ["DATABASE_URL"] = uri
    os.environ["UPLOAD_DIR"] = str(upload_dir)
    boot = psycopg.connect(uri)
    try:
        pg_store.ensure_schema(boot)
    finally:
        boot.close()

    _install_mrxs_open_stub()

    conn = psycopg.connect(uri)
    conn.row_factory = psycopg.rows.dict_row
    try:
        world = build_world(conn, upload_dir, work)

        # —— P0 冻结审计（frozen：逐文件 SHA + 一致快照）——
        audit = load_audit_tool()
        rc = audit.main(["--database-url", uri, "--upload-dir",
                         str(upload_dir), "--out-dir", str(audit_out),
                         "--mode", "frozen"])
        assert rc == 0, "P0 冻结审计未通过（rc=%s）" % rc

        # —— 计划 ——
        rc = planner.main(["--inventory", str(audit_out / "inventory.jsonl"),
                           "--issues", str(audit_out / "issues.jsonl"),
                           "--env", "drill-local", "--out", str(plan_path)])
        assert rc == 0, "计划生成失败"
        digest = hashlib.sha256(plan_path.read_bytes()).hexdigest()

        # —— 迁移前基线（授权/配额快照，断言零变更）——
        pre_auth = _snapshot_auth(conn)
        pre_quota = _snapshot_quota(conn)

        # —— 迁移（含中断恢复注入）——
        kw = dict(plan_path=str(plan_path), apply=True, plan_digest=digest,
                  env="drill-local",
                  quiesce_proof="drill: writers stopped 2026-09-25 "
                                "(pgserver isolated + UPLOAD_DIR private)",
                  upload_dir=str(upload_dir), database_url=uri)
        if with_crashes:
            for item, point in (("sld_drill_mrxs01", "copied"),
                                ("sld_drill_svs01", "after_publish"),
                                ("sld_drill_tif01", "bound")):
                try:
                    migrator.run_migrate(crash_after=(item, point), **kw)
                    raise AssertionError("崩溃注入未生效：%s@%s" % (item, point))
                except SystemExit as exc:
                    assert exc.code == 130, "非演练崩溃信号：%r" % exc
        summary = migrator.run_migrate(**kw)
        assert not summary["failures"], "迁移存在失败项：%s" % summary["failures"]
        assert summary["outcomes"].get("postverified") >= len(MIGRATE_IDS), \
            "migrate 人群未全部 postverified：%s" % summary["outcomes"]

        # —— 独立核验 ——
        verif = verifier.run_verify(
            upload_dir=str(upload_dir), out_dir=str(verify_out),
            plan_path=str(plan_path),
            journal_path=str(work / "migration-journal.jsonl"),
            database_url=uri)

        # —— 授权/配额零变更断言 ——
        post_auth = _snapshot_auth(conn)
        post_quota = _snapshot_quota(conn)
        assert pre_auth == post_auth, "授权映射被迁移改动（违规）"
        assert pre_quota == post_quota, "配额账本被迁移改动（R-12 违规）"

        evidence.update({
            "world": world, "uri": uri, "upload_dir": str(upload_dir),
            "audit_out": str(audit_out), "plan": str(plan_path),
            "digest": digest, "verify_out": str(verify_out),
            "verify": verif,
            "journal": str(work / "migration-journal.jsonl"),
            "summary": summary, "conn": conn, "server": server,
        })
        return evidence
    except BaseException:
        conn.close()
        try:
            server.cleanup()
        except Exception:  # noqa: BLE001
            pass
        shutil.rmtree(pg_data, ignore_errors=True)
        raise


def _snapshot_auth(conn) -> dict:
    """授权相关表的确定性快照（迁移前后 diff 用）。"""
    snap = {}
    for table, order in (
            ("share_slides", "token, slide_id"),
            ("slide_view_grants", "slide_name, user_id"),
            ("grants", "id"), ("shares", "token"),
            ("project_slides", "project_id, slide"),
            ("run_grants", "grant_id"), ("demo_catalog", "slide_id")):
        with conn.cursor() as cur:
            cur.execute("SELECT to_jsonb(x) AS j FROM %s x ORDER BY %s"
                        % (table, order))
            snap[table] = [r["j"] for r in cur.fetchall()]
    return snap


def _snapshot_quota(conn) -> dict:
    with conn.cursor() as cur:
        cur.execute("SELECT user_id, used_bytes, reserved_bytes "
                    "FROM upload_user_quotas ORDER BY user_id")
        return {r["user_id"]: (int(r["used_bytes"]), int(r["reserved_bytes"]))
                for r in cur.fetchall()}


# --------------------------------------------------------------------------- #
# §2 六项断言
# --------------------------------------------------------------------------- #
def assert_drill(ev: dict, report_lines: list) -> None:
    conn = ev["conn"]
    up = Path(ev["upload_dir"])
    out = lambda ok, msg: report_lines.append(
        ("%s  %s" % ("ok  " if ok else "FAIL", msg)))

    # ---- 1. 全受支持格式代表样本 ----
    for sid in MIGRATE_IDS:
        row = _row(conn, sid)
        ok = (row["storage_layout"] == "id_bundle"
              and row["asset_state"] == "ready"
              and (up / row["storage_relpath"]).is_file())
        out(ok, "§2-1 格式样本 %s → id_bundle 且入口可读（%s）"
            % (sid, row["legacy_filename"]))
    mrxs_bundle = up / "objects" / "sld_drill_mrxs01"
    ok = (mrxs_bundle / "panel.mrxs").is_file() and \
        (mrxs_bundle / "panel" / "Slidedata.ini").is_file() and \
        (mrxs_bundle / "manifest.json").is_file()
    out(ok, "§2-1 MRXS 伴侣目录全成员入包（保名入口 + manifest）")
    kfbp_bundle = up / "objects" / "sld_drill_kfbp01"
    ok = (kfbp_bundle / "data.tif").is_file() and \
        (up / "kfb-converted.tif.manifest.json").is_file() and \
        (up / "kfb-converted.tif.associated").is_dir()
    out(ok, "§2-1 kfb 转换产物形态：入口入包，派生物留置原位（计划披露）")
    # 源保留（不删源）
    for name in ("specimen.svs", "panel.mrxs", "panel", "scan.tif",
                 "kfb-converted.tif", "kfbf-out.ome.tif"):
        out((up / name).exists(), "§不删源：%s 保留原位" % name)

    # ---- 2. 历史关系全谱（迁移后逐条落点 + 正向访问） ----
    desc = slide_store.resolve_slide_id("sld_drill_svs01")
    out(slide_store.authorize_read(desc, actor_user_id=ALICE),
        "§2-2 owner 读自己资产")
    out(slide_store.authorize_read("sld_drill_svs01", actor_user_id=BOB),
        "§2-2 分享成员（share_slides+grants 领取）读旧资产")
    out(slide_store.authorize_read("sld_drill_svs01", actor_user_id=BOB,
                                   allow_share=False),
        "§2-2 显式 view grant 通道（关 share 仍可读）")
    out(slide_store.authorize_read("sld_drill_svs01",
                                   demo_capability=True),
        "§2-2 demo 目录 capability 通道")
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) AS n FROM project_slides "
                    "WHERE slide_id = 'sld_drill_svs01'")
        out(cur.fetchone()["n"] == 1, "§2-2 项目成员关系落点")
        cur.execute("SELECT count(*) AS n FROM rois WHERE "
                    "slide_id='sld_drill_svs01' AND NOT deleted")
        out(cur.fetchone()["n"] == 1, "§2-2 标注（rois）关系落点")
        cur.execute("SELECT count(*) AS n FROM run_grants WHERE "
                    "slide_id='sld_drill_svs01'")
        out(cur.fetchone()["n"] == 1, "§2-2 run grants 落点")
        cur.execute("SELECT count(*) AS n FROM ai_session_principals "
                    "WHERE slide_id='sld_drill_svs01'")
        out(cur.fetchone()["n"] == 1, "§2-2 AI principals 落点")

    # ---- 3. 中断恢复（journal 事件计数证明不重复） ----
    events = _journal_events(ev["journal"])
    for sid in MIGRATE_IDS:
        copied = [e for e in events.get(sid, [])
                  if e["phase"] == "copied" and e["result"] == "ok"
                  and not e.get("resumed")]
        bound = [e for e in events.get(sid, []) if e["phase"] == "bound"
                 and e["result"] == "ok"]
        post = [e for e in events.get(sid, [])
                if e["phase"] == "postverified" and e["result"] == "ok"]
        out(len(copied) == 1 and len(bound) == 1 and len(post) == 1,
            "§2-3 %s：copied/bound/postverified 各恰一次（重跑不重复）"
            % sid)
    mrxs_events = events.get("sld_drill_mrxs01", [])
    out(any(e["phase"] == "copied" and e["result"] == "ok"
            for e in mrxs_events),
        "§2-3 copied 后杀进程：journal 留证，重跑幂等复用 staging")

    # ---- 4. 隔离类：不可读、不进列表、披露 ----
    ready_ids = {d.slide_id for d in slide_store.list_ready_descriptors()}
    for sid in QUARANTINE_IDS + RETAIN_IDS:
        row = _row(conn, sid)
        readable = slide_store.authorize_read(sid, actor_user_id=ALICE) or \
            slide_store.authorize_read(sid, actor_user_id=BOB)
        out(row["asset_state"] in ("failed", "legacy")
            and sid not in ready_ids and not readable,
            "§2-4 %s（%s）不可读且不在 ready 列表（state=%s）"
            % (sid, row["legacy_filename"], row["asset_state"]))
    out(_row(conn, "sld_drill_miss01")["asset_state"] == "failed",
        "§2-4 retain_history（缺文件）→ failed+reason")
    orphan_row = slide_store.resolve_legacy_alias("orphan-slide.svs")
    out(orphan_row is None, "§2-4 孤儿文件不建行不进列表（只隔离报告）")

    # ---- 5. tombstone 不复活 ----
    reborn = ev["world"]["reborn_id"]
    out(slide_store.authorize_read(reborn, actor_user_id=BOB) is False,
        "§2-5 旧分享领取人（%s）不能读同展示名新资产 %s" % (BOB, reborn))
    out(slide_store.authorize_read(reborn, actor_user_id=ALICE),
        "§2-5 新资产 owner 正常可读（不受 tombstone 影响）")
    tomb = _row(conn, TOMB_ID)
    out(tomb["asset_state"] == "deleted"
        and tomb["legacy_filename"] == "reborn.svs",
        "§2-5 迁移不碰 tombstone（deleted 行保留冻结别名）")

    # ---- 6. 独立核验结论 ----
    verif = ev["verify"]
    out(verif["go_no_go"] == "go" and not verif["incomplete"]
        and not verif["violations"],
        "§2-6 verify：go（无违规/incomplete）")
    quota = verif["quota"]
    # ALICE：used 以迁移前 ready 合计播种；迁移后隔离项翻 failed —— delta
    # 须被「failed accounted」原因完整披露（隔离收口属合法不等于）。
    failed_acct = quota[ALICE]["accounted_by_state"].get("failed", 0)
    out(quota[ALICE]["delta_used_minus_responsible"] == failed_acct
        and any("failed" in r for r in quota[ALICE]["reasons"]),
        "§2-6 ALICE 配额「不等于」被 failed 隔离原因完整披露（delta=%d）"
        % quota[ALICE]["delta_used_minus_responsible"])
    # BOB：tombstone 不退款差 + 派生物留置的校准差，两原因都须披露。
    bob_delta = quota[BOB]["delta_used_minus_responsible"]
    out(bob_delta > 4096
        and any("deleted" in r for r in quota[BOB]["reasons"])
        and any("派生物" in r for r in quota[BOB]["reasons"]),
        "§2-6 BOB 配额「不等于」的合法原因披露（deleted 不退款 + 派生物"
        "校准；delta=%d）" % bob_delta)
    out(verif["tombstones"]["crossread_checks"] >= 1
        and not verif["tombstones"]["crossread_violations"],
        "§2-6 tombstone×重生交叉验证无复活")


def _row(conn, slide_id):
    with conn.cursor() as cur:
        cur.execute("SELECT slide_id, legacy_filename, owner_user_id, "
                    "asset_state, storage_layout, storage_relpath, "
                    "accounted_bytes FROM slides WHERE slide_id=%s",
                    (slide_id,))
        return cur.fetchone()


def _journal_events(path):
    events = {}
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        rec = json.loads(line)
        if rec.get("record") == "apply_header":
            continue
        events.setdefault(rec["item_id"], []).append(rec)
    return events


# --------------------------------------------------------------------------- #
# 证据落盘 + main
# --------------------------------------------------------------------------- #
def write_evidence(ev: dict, evidence_dir: Path, report_lines: list) -> None:
    evidence_dir.mkdir(parents=True, exist_ok=True)
    _copy_private(Path(ev["plan"]), evidence_dir / "migration-plan.jsonl")
    _copy_private(Path(ev["journal"]), evidence_dir / "migration-journal.jsonl")
    _copy_private(Path(ev["verify_out"]) / "verification.json",
                  evidence_dir / "verification.json")
    _copy_private(Path(ev["verify_out"]) / "summary.md",
                  evidence_dir / "summary.md")
    _copy_private(Path(ev["audit_out"]) / "verification.json",
                  evidence_dir / "audit-frozen-verification.json")
    header = json.loads(Path(ev["plan"]).read_text(
        encoding="utf-8").splitlines()[0])
    lines = []
    lines.append("# P6 迁移演练报告（合成副本，%s）" % DRILL_TAG)
    lines.append("")
    lines.append("## 环境构造")
    lines.append("")
    lines.append("- 内嵌 pgserver（临时数据目录，退出即删）+ 临时 UPLOAD_DIR；"
                 "DATABASE_URL/UPLOAD_DIR 全程指向该副本。")
    lines.append("- 管线：种子世界 → backfill --apply → P0 冻结审计（frozen）"
                 "→ plan → migrate --apply（含三处崩溃注入的中断恢复）→ "
                 "verify 独立核验。")
    lines.append("- 崩溃注入点：sld_drill_mrxs01@copied / "
                 "sld_drill_svs01@after_publish / sld_drill_tif01@bound"
                 "（SystemExit(130) 模拟 kill；journal 逐事件 fsync）。")
    lines.append("")
    lines.append("## 计划摘要")
    lines.append("")
    lines.append("```json")
    lines.append(json.dumps(header, ensure_ascii=False, indent=2))
    lines.append("```")
    lines.append("")
    lines.append("## §2 六项验证结果")
    lines.append("")
    lines.append("```")
    lines.extend(report_lines)
    lines.append("```")
    lines.append("")
    failed = [l for l in report_lines if l.startswith("FAIL")]
    lines.append("## 结论")
    lines.append("")
    lines.append("- 断言合计 %d，失败 %d。%s"
                 % (len(report_lines), len(failed),
                    "演练通过。" if not failed else "**演练失败。**"))
    lines.append("")
    _write_private(evidence_dir / "drill-report.md", "\n".join(lines) + "\n")


def _copy_private(src: Path, dst: Path):
    _write_private(dst, src.read_text(encoding="utf-8"))


def _write_private(path: Path, text: str):
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.chmod(str(path), 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
    except BaseException:
        os.close(fd)
        raise


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="P6 合成副本迁移演练（§2 六项）")
    p.add_argument("--work-root", default=None,
                   help="工作根（默认临时目录；含 pgserver 数据与 UPLOAD_DIR）")
    p.add_argument("--evidence-dir", default=None,
                   help="证据目录（默认 docs/drill-evidence-%s）" % DRILL_TAG)
    p.add_argument("--keep-work", action="store_true",
                   help="保留工作根（默认演练后删除）")
    args = p.parse_args(argv)

    work_root = Path(args.work_root) if args.work_root else \
        Path(tempfile.mkdtemp(prefix="drill-slide-migration-"))
    evidence_dir = Path(args.evidence_dir) if args.evidence_dir else \
        _REPO_ROOT / "docs" / ("drill-evidence-%s" % DRILL_TAG)
    report_lines = []
    try:
        ev = run_pipeline_once(work_root)
        try:
            assert_drill(ev, report_lines)
        finally:
            write_evidence(ev, evidence_dir, report_lines)
            conn = ev.pop("conn")
            conn.close()
            server = ev.pop("server")
            server.cleanup()
        failed = [l for l in report_lines if l.startswith("FAIL")]
        for line in report_lines:
            print(line)
        print("drill_slide_migration: %d 断言，失败 %d；证据 → %s"
              % (len(report_lines), len(failed), evidence_dir))
        return 1 if failed else 0
    finally:
        if not args.keep_work:
            shutil.rmtree(work_root, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
