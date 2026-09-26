# -*- coding: utf-8 -*-
"""scripts/{plan,migrate,verify}_slide_migration.py 测试（P6 合同 §4）。

覆盖合同 §4 全列：
  - 计划确定性（同输入同输出同 sha256；头部 version/env/输入摘要）；
  - 三件套缺任一拒绝 apply（plan-digest/env/quiesce-proof；防测试计划打
    生产）；
  - 五态推进 / 中断重跑幂等（copied 后杀进程不重复复制；after_publish 重跑
    经 journal+manifest 幂等复用；bound 后重跑只补 postverify）；
  - no-clobber 冲突中止（无 journal 证据的先占目标 / manifest 不吻合的
    已存在目标都拒绝，不依内容相同认领）；
  - 隔离口径（quarantine/retain_history 不可读不列表，报告披露）；
  - verify 独立性（篡改 journal 的 success 字段仍被独立核验抓出；bundle
    字节损坏被抓出；权限故障→incomplete）；
  - 不计上传配额（used_bytes 迁移前后零变更，R-12）；
  - 授权零变更（shares/grants/share_slides/view_grants 等迁移前后逐行一致）；
  - 复制不硬链接（源与包 inode 隔离）、不删源、空间不足阻塞、源漂移中止。

夹具复用 scripts/drill_slide_migration.py 的 build_world（§2 同一合成世界：
svs/tif/mrxs+伴侣/kfb 产物+派生物/ome、关系全谱、tombstone+同名新资产、
隔离项、孤儿）。conftest 起 session PG（每用例 TRUNCATE）；UPLOAD_DIR 用
tmp_path。MRXS 代表性试开经 drill._install_mrxs_open_stub（真 openslide 的
mirax 驱动需真实厂商数据集；其余格式仍走真实打开——见 drill 模块披露）。
"""
import ast
import hashlib
import importlib.util
import json
import os
import shutil
import sys
from pathlib import Path
from types import SimpleNamespace

import psycopg
import pytest

_REPO = Path(__file__).resolve().parents[1]
for _p in (str(_REPO / "scripts"), str(_REPO / "tests")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import slide_store  # noqa: E402

import backfill_slide_asset_state as backfill  # noqa: E402
import drill_slide_migration as drill  # noqa: E402
import migrate_slide_storage as migrator  # noqa: E402
import plan_slide_migration as planner  # noqa: E402
import verify_slide_migration as verifier  # noqa: E402

_PLAN_ENV = "pytest-local"


def _load_audit():
    spec = importlib.util.spec_from_file_location(
        "audit_slide_identity_for_migration_tests",
        str(_REPO / "scripts" / "audit_slide_identity.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


AUDIT = _load_audit()


# --------------------------------------------------------------------------- #
# 夹具：合成世界 + 冻结审计 + 计划
# --------------------------------------------------------------------------- #
@pytest.fixture
def conn(pg_uri):
    c = psycopg.connect(pg_uri)
    c.row_factory = psycopg.rows.dict_row
    yield c
    c.close()


@pytest.fixture
def world(pg_uri, tmp_path, conn):
    """种子世界 → backfill → P0 冻结审计 → 确定性计划。

    返回 {conn, uri, up(UPLOAD_DIR), plan, digest, journal, audit_out,
    world(drill 世界清单)}。
    """
    drill._install_mrxs_open_stub()
    up = tmp_path / "uploads"
    up.mkdir()
    w = drill.build_world(conn, up, tmp_path)

    audit_out = tmp_path / "audit-out"
    rc = AUDIT.main(["--database-url", pg_uri, "--upload-dir", str(up),
                     "--out-dir", str(audit_out), "--mode", "frozen"])
    assert rc == 0, "P0 冻结审计失败（rc=%s）" % rc

    plan = tmp_path / "migration-plan.jsonl"
    rc = planner.main(["--inventory", str(audit_out / "inventory.jsonl"),
                       "--issues", str(audit_out / "issues.jsonl"),
                       "--env", _PLAN_ENV, "--out", str(plan)])
    assert rc == 0
    digest = hashlib.sha256(plan.read_bytes()).hexdigest()
    yield {"conn": conn, "uri": pg_uri, "up": up, "plan": plan,
           "digest": digest, "audit_out": audit_out,
           "journal": tmp_path / "journal.jsonl", "world": w}


def apply_migrate(world, *, crash_after=None):
    return migrator.run_migrate(
        plan_path=str(world["plan"]), apply=True,
        plan_digest=world["digest"], env=_PLAN_ENV,
        quiesce_proof="pytest quiesce proof 2026-09-25",
        upload_dir=str(world["up"]), journal_path=str(world["journal"]),
        database_url=world["uri"], crash_after=crash_after)


def full_apply(world):
    summary = apply_migrate(world)
    assert not summary["failures"], summary["failures"]
    return summary


def journal_events(path):
    events = {}
    if not Path(path).exists():
        return events
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        rec = json.loads(line)
        if rec.get("record") == "apply_header":
            continue
        events.setdefault(rec["item_id"], []).append(rec)
    return events


def phase_events(events, item_id, phase, result="ok"):
    return [e for e in events.get(item_id, [])
            if e["phase"] == phase and e.get("result") == result]


def row_of(conn, slide_id):
    with conn.cursor() as cur:
        cur.execute("SELECT slide_id, legacy_filename, owner_user_id, "
                    "asset_state, storage_layout, storage_relpath, "
                    "accounted_bytes FROM slides WHERE slide_id=%s",
                    (slide_id,))
        return cur.fetchone()


def run_verify(world, out_name="verify-out", monkeypatch=None,
               quota_approvals=None):
    """quota_approvals：[{user_id, delta, reason}]（R6 审查修复问题 5 的
    核准凭据形态）——写临时 JSON 传给 run_verify；缺省无核准（任何差额
    阻断）。"""
    out = world["plan"].parent / out_name
    approvals_path = None
    if quota_approvals is not None:
        approvals_path = str(world["plan"].parent / (
            "%s-quota-approvals.json" % out_name))
        import json as _json
        io_path = approvals_path
        with open(io_path, "w", encoding="utf-8") as f:
            _json.dump(quota_approvals, f, ensure_ascii=False, indent=1)
    return verifier.run_verify(
        upload_dir=str(world["up"]), out_dir=str(out),
        plan_path=str(world["plan"]), journal_path=str(world["journal"]),
        database_url=world["uri"], quota_approvals_path=approvals_path)


def derive_quota_approvals(verif):
    """从首遍 verify 的配额报告推导核准条目（演练/测试用的两遍法：首遍
    报告差额 → 按报告事实签发核准 → 复跑放行）。生产流程中这步是人工
    审批，不是自动推导。"""
    out = []
    for owner, rep in sorted((verif.get("quota") or {}).items()):
        delta = int(rep.get("delta_used_minus_responsible") or 0)
        if delta != 0:
            out.append({
                "user_id": owner, "delta": delta,
                "reason": "测试/演练核准：" + "; ".join(rep.get("reasons") or [])})
    return out


# --------------------------------------------------------------------------- #
# 静态契约：plan 不 import 业务模块；migrate/verify 不 import app
# --------------------------------------------------------------------------- #
def test_static_import_contracts():
    def imports_of(path):
        src = Path(path).read_text(encoding="utf-8")
        found = set()
        for node in ast.walk(ast.parse(src)):
            if isinstance(node, ast.Import):
                found |= {a.name.split(".")[0] for a in node.names}
            elif isinstance(node, ast.ImportFrom) and node.level == 0 \
                    and node.module:
                found.add(node.module.split(".")[0])
        return found

    plan_imports = imports_of(_REPO / "scripts" / "plan_slide_migration.py")
    assert not plan_imports & {
        "app", "pg_store", "slide_store", "slide_storage", "slide_io",
        "share_store", "share_store_pg"}, plan_imports
    for name in ("migrate_slide_storage", "verify_slide_migration"):
        imps = imports_of(_REPO / "scripts" / ("%s.py" % name))
        assert "app" not in imps
        assert imps <= {
            "__future__", "argparse", "hashlib", "json", "os", "re", "shutil",
            "stat", "sys", "datetime", "pathlib", "psycopg", "types",
            "pg_store", "slide_store", "slide_storage", "slide_io"}, imps


# --------------------------------------------------------------------------- #
# slide_store.bind_id_bundle_layout 原语（P6 补的原语+测试义务）
# --------------------------------------------------------------------------- #
def test_bind_id_bundle_layout_primitive(conn):
    with conn.cursor() as cur:
        cur.execute("INSERT INTO users (user_id, login_id, role) "
                    "VALUES ('u1','u1@t.example','user')")
        cur.execute("INSERT INTO slides (slide_id, legacy_filename, "
                    "owner_user_id, asset_state, storage_layout, "
                    "accounted_bytes) VALUES "
                    "('sld_b1','b.svs','u1','ready','legacy',999)")
        cur.execute("INSERT INTO upload_user_quotas (user_id, quota_bytes, "
                    "used_bytes, reserved_bytes) VALUES "
                    "('u1', 10000, 500, 0)")
    conn.commit()

    # 迁移：CAS 命中；accounted_bytes 校准；asset_state 不动
    assert slide_store.bind_id_bundle_layout(
        "sld_b1", "objects/sld_b1/data.svs", accounted_bytes=100,
        conn=None) == "migrated"
    r = row_of(conn, "sld_b1")
    assert r["storage_layout"] == "id_bundle"
    assert r["storage_relpath"] == "objects/sld_b1/data.svs"
    assert int(r["accounted_bytes"]) == 100
    assert r["asset_state"] == "ready"

    # 幂等重入：同参 already
    assert slide_store.bind_id_bundle_layout(
        "sld_b1", "objects/sld_b1/data.svs", accounted_bytes=100,
        conn=None) == "already"
    # accounted=None=保持现值的幂等
    assert slide_store.bind_id_bundle_layout(
        "sld_b1", "objects/sld_b1/data.svs") == "already"

    # 异参冲突：不猜不绑
    with pytest.raises(slide_store.LayoutBindConflict):
        slide_store.bind_id_bundle_layout(
            "sld_b1", "objects/sld_b1/data.tif", accounted_bytes=100)
    with pytest.raises(slide_store.LayoutBindConflict):
        slide_store.bind_id_bundle_layout(
            "sld_b1", "objects/sld_b1/data.svs", accounted_bytes=200)

    # relpath 必须位于 objects/<本 slide_id>/ 之下
    with pytest.raises(ValueError):
        slide_store.bind_id_bundle_layout(
            "sld_b1", "objects/sld_other/data.svs")
    with pytest.raises(ValueError):
        slide_store.bind_id_bundle_layout("sld_b1", "elsewhere/data.svs")

    # 状态谓词：expected_state 不匹配 → 冲突
    with conn.cursor() as cur:
        cur.execute("INSERT INTO slides (slide_id, legacy_filename, "
                    "owner_user_id, asset_state, storage_layout) VALUES "
                    "('sld_b2','b2.svs','u1','legacy','legacy')")
    conn.commit()
    with pytest.raises(slide_store.LayoutBindConflict):
        slide_store.bind_id_bundle_layout(
            "sld_b2", "objects/sld_b2/data.svs",
            expected_state=slide_store.SlideState.STAGING)

    # 不动配额（R-12）：used_bytes 保持 500
    with conn.cursor() as cur:
        cur.execute("SELECT used_bytes FROM upload_user_quotas "
                    "WHERE user_id='u1'")
        assert int(cur.fetchone()["used_bytes"]) == 500


# --------------------------------------------------------------------------- #
# 计划：确定性 + 分类口径（合同 §1.1 / §4）
# --------------------------------------------------------------------------- #
def test_plan_deterministic_and_classification(world, tmp_path):
    plan2 = tmp_path / "plan2.jsonl"
    rc = planner.main(["--inventory",
                       str(world["audit_out"] / "inventory.jsonl"),
                       "--issues", str(world["audit_out"] / "issues.jsonl"),
                       "--env", _PLAN_ENV, "--out", str(plan2)])
    assert rc == 0
    # 同输入同输出：逐字节一致（排序稳定、无时间戳/随机参与内容）
    assert plan2.read_bytes() == world["plan"].read_bytes()
    assert hashlib.sha256(plan2.read_bytes()).hexdigest() == world["digest"]

    lines = [json.loads(l) for l in
             world["plan"].read_text(encoding="utf-8").splitlines() if l.strip()]
    header, items = lines[0], lines[1:]
    assert header["record_type"] == "plan_header"
    assert header["env"] == _PLAN_ENV
    assert header["tool_version"].startswith("1.0.0-p6")
    assert len(header["inputs"]["inventory"]["sha256"]) == 64
    assert len(header["inputs"]["issues"]["sha256"]) == 64
    assert "ts" not in header and "ts" not in items[0]  # 无时间戳

    counts = header["counts"]
    assert counts["migrate"] == len(drill.MIGRATE_IDS)
    assert counts["retain_history"] == len(drill.RETAIN_IDS) + 1  # +tombstone
    assert counts["quarantine"] == len(drill.QUARANTINE_IDS) + 1  # +孤儿
    assert counts["no_alias_rows_out_of_scope"] == 1              # reborn

    by_id = {it["item_id"]: it for it in items}
    # 绝不重新分配 slide_id：全部沿用审计冻结的既有 ID
    for it in items:
        if it["kind"] == "slide":
            assert it["slide_id"].startswith("sld_drill_")
    # 目标布局派生：单文件 data.<ext>；MRXS 保名入口（伴侣 stem 耦合）
    svs = by_id["sld_drill_svs01"]
    assert svs["target"]["entry"] == "data.svs"
    assert svs["target"]["storage_relpath"] == \
        "objects/sld_drill_svs01/data.svs"
    assert svs["format_ext"] == "svs"
    mrxs = by_id["sld_drill_mrxs01"]
    assert mrxs["target"]["entry"] == "panel.mrxs"
    assert mrxs["target"]["storage_relpath"] == \
        "objects/sld_drill_mrxs01/panel.mrxs"
    assert mrxs["source"]["companion_dir"] == "panel"
    # kfb 产物：派生物留置披露
    kfbp = by_id["sld_drill_kfbp01"]
    assert kfbp["source"]["derivatives_in_place"] == {
        "manifest_json": True, "associated_dir": True}
    # 授权映射摘要只有计数（无 token/秘密）
    auth = svs["authorization_summary"]
    assert auth["shares"] == 1 and auth["view_grants_by_id"] == 1 \
        and auth["project_slides"] == 1 and auth["demo_catalog"] == 1
    plan_text = world["plan"].read_text(encoding="utf-8")
    assert drill.TOK_SHARE not in plan_text
    # 动作裁决：缺文件 retain；owner 空/symlink/活跃任务/孤儿 quarantine
    assert by_id["sld_drill_miss01"]["action"] == "retain_history"
    assert by_id["sld_drill_miss01"]["reason"] == "missing_file"
    assert by_id["sld_drill_noown01"]["action"] == "quarantine"
    assert by_id["sld_drill_noown01"]["reason"] == "owner_unresolvable"
    assert by_id["sld_drill_link01"]["reason"] == "symlink_entry"
    assert by_id["sld_drill_inflight01"]["reason"] == "active_task_in_flight"
    assert by_id["orphan:orphan-slide.svs"]["action"] == "quarantine"
    # 回滚定位：源保留位
    assert svs["rollback"]["source_retained"] is True
    assert svs["rollback"]["source_relpath"] == "specimen.svs"
    # 冻结源证据：入口 sha256 在案（frozen 审计）
    assert svs["source"]["entry_sha256"] == hashlib.sha256(
        (world["up"] / "specimen.svs").read_bytes()).hexdigest()


# --------------------------------------------------------------------------- #
# apply 三件套（合同 §1.2 / §4）
# --------------------------------------------------------------------------- #
def test_apply_trio_required(world):
    base = ["--plan", str(world["plan"]), "--apply",
            "--upload-dir", str(world["up"]),
            "--journal", str(world["journal"]),
            "--database-url", world["uri"]]
    ok_digest = world["digest"]
    cases = [
        base,                                                   # 全缺
        base + ["--plan-digest", ok_digest, "--env", _PLAN_ENV],  # 缺 proof
        base + ["--plan-digest", "dead" * 16, "--env", _PLAN_ENV,
                "--quiesce-proof", "x"],                        # 摘要不符
        base + ["--plan-digest", ok_digest, "--env", "prod-oops",
                "--quiesce-proof", "x"],                        # env 不符
    ]
    for argv in cases:
        assert migrator.main(argv) == 1, argv
        assert not world["journal"].exists(), "拒绝态不得产生 journal"
    # 库与盘零变化（objects/ 只有种子期 reborn 资产，无迁移产物）
    for sid in drill.MIGRATE_IDS:
        r = row_of(world["conn"], sid)
        assert r["storage_layout"] == "legacy"
        assert not (world["up"] / "objects" / sid).exists()
    assert not (world["up"] / ".staging" /
                ("migrate-%s" % world["digest"][:16])).exists()


def test_dry_run_default_no_writes(world):
    summary = migrator.run_migrate(
        plan_path=str(world["plan"]), upload_dir=str(world["up"]),
        journal_path=str(world["journal"]), database_url=world["uri"])
    assert summary["mode"] == "dry-run"
    assert summary["outcomes"]["dry"] >= len(drill.MIGRATE_IDS)
    assert not world["journal"].exists()
    for sid in drill.MIGRATE_IDS:
        assert row_of(world["conn"], sid)["storage_layout"] == "legacy"
        assert not (world["up"] / "objects" / sid).exists()
    assert not (world["up"] / ".staging" /
                ("migrate-%s" % world["digest"][:16])).exists()


# --------------------------------------------------------------------------- #
# 五态推进 / 中断重跑幂等（合同 §1.2 / §4）
# --------------------------------------------------------------------------- #
def test_interrupt_after_copied_reuse(world):
    with pytest.raises(SystemExit) as ei:
        apply_migrate(world, crash_after=("sld_drill_mrxs01", "copied"))
    assert ei.value.code == 130
    ev = journal_events(world["journal"])
    assert phase_events(ev, "sld_drill_mrxs01", "copied")
    # staging 已就位（副本已在，不重复复制由 copied 事件唯一性证明）
    staging = world["up"] / ".staging" / \
        ("migrate-%s" % world["digest"][:16]) / "sld_drill_mrxs01"
    assert (staging / "panel.mrxs").is_file()

    full_apply(world)
    ev = journal_events(world["journal"])
    for sid in drill.MIGRATE_IDS:
        assert len(phase_events(ev, sid, "copied")) == 1, sid
        assert len(phase_events(ev, sid, "bound")) == 1, sid
        assert len(phase_events(ev, sid, "postverified")) == 1, sid
        assert len(phase_events(ev, sid, "verified")) == 1, sid


def test_interrupt_after_publish_idempotent_reuse(world):
    with pytest.raises(SystemExit):
        apply_migrate(world, crash_after=("sld_drill_svs01", "after_publish"))
    # FS 已发布、DB 未绑定：bundle 在、行仍 legacy
    bundle = world["up"] / "objects" / "sld_drill_svs01"
    assert (bundle / "data.svs").is_file()
    assert row_of(world["conn"], "sld_drill_svs01")["storage_layout"] == \
        "legacy"
    ev = journal_events(world["journal"])
    assert not phase_events(ev, "sld_drill_svs01", "bound")

    full_apply(world)   # journal+manifest 证明同一迁移项 → 幂等复用不重发
    ev = journal_events(world["journal"])
    assert len(phase_events(ev, "sld_drill_svs01", "copied")) == 1
    assert len(phase_events(ev, "sld_drill_svs01", "bound")) == 1
    r = row_of(world["conn"], "sld_drill_svs01")
    assert r["storage_layout"] == "id_bundle"


def test_interrupt_after_bound_only_postverify(world):
    with pytest.raises(SystemExit):
        apply_migrate(world, crash_after=("sld_drill_tif01", "bound"))
    r = row_of(world["conn"], "sld_drill_tif01")
    assert r["storage_layout"] == "id_bundle"   # DB 已绑定
    ev = journal_events(world["journal"])
    assert phase_events(ev, "sld_drill_tif01", "bound")
    assert not phase_events(ev, "sld_drill_tif01", "postverified")

    full_apply(world)   # 只补 postverified
    ev = journal_events(world["journal"])
    assert len(phase_events(ev, "sld_drill_tif01", "bound")) == 1
    assert len(phase_events(ev, "sld_drill_tif01", "postverified")) == 1


def test_journal_records_quiesce_proof(world):
    full_apply(world)
    header = None
    for line in world["journal"].read_text(encoding="utf-8").splitlines():
        rec = json.loads(line)
        if rec.get("record") == "apply_header":
            header = rec
    assert header and header["quiesce_proof"] == "pytest quiesce proof 2026-09-25"
    assert header["env"] == _PLAN_ENV
    assert header["plan_sha256"] == world["digest"]


def test_rerun_after_completion_adds_no_events(world):
    full_apply(world)
    before = journal_events(world["journal"])
    full_apply(world)
    after = journal_events(world["journal"])
    for sid in drill.MIGRATE_IDS:
        for phase in ("planned", "copied", "verified", "bound",
                      "postverified"):
            assert len(phase_events(before, sid, phase)) == \
                len(phase_events(after, sid, phase)) == 1, (sid, phase)


# --------------------------------------------------------------------------- #
# no-clobber 冲突中止（合同 §1.2 / §4）
# --------------------------------------------------------------------------- #
def test_no_clobber_preexisting_target_aborts(world):
    bundle = world["up"] / "objects" / "sld_drill_svs01"
    bundle.mkdir(parents=True)
    (bundle / "intruder.bin").write_bytes(b"not-ours")
    summary = apply_migrate(world)
    fails = {f["item_id"]: f["reason"] for f in summary["failures"]}
    assert fails.get("sld_drill_svs01") == "target_exists_no_journal_proof"
    # 行未绑定、入侵文件未被覆盖/删除（不依内容相同认领）
    r = row_of(world["conn"], "sld_drill_svs01")
    assert r["storage_layout"] == "legacy"
    assert (bundle / "intruder.bin").read_bytes() == b"not-ours"
    # 其余项不受株连
    for sid in drill.MIGRATE_IDS:
        if sid != "sld_drill_svs01":
            assert row_of(world["conn"], sid)["storage_layout"] == \
                "id_bundle"


def test_no_clobber_mismatched_existing_bundle_aborts(world):
    with pytest.raises(SystemExit):
        apply_migrate(world, crash_after=("sld_drill_svs01", "after_publish"))
    entry = world["up"] / "objects" / "sld_drill_svs01" / "data.svs"
    entry.write_bytes(entry.read_bytes() + b"TAMPER")   # 内容漂移
    summary = apply_migrate(world)
    fails = {f["item_id"]: f["reason"] for f in summary["failures"]}
    assert fails.get("sld_drill_svs01") == "target_conflict_manifest_mismatch"
    assert row_of(world["conn"], "sld_drill_svs01")["storage_layout"] == \
        "legacy"


# --------------------------------------------------------------------------- #
# 隔离口径（合同 §1.1/§1.2 / §4）
# --------------------------------------------------------------------------- #
def test_quarantine_and_retain_history_semantics(world):
    full_apply(world)
    conn = world["conn"]
    ready_ids = {d.slide_id for d in slide_store.list_ready_descriptors()}
    for sid in drill.QUARANTINE_IDS + drill.RETAIN_IDS:
        assert sid not in ready_ids
        assert slide_store.authorize_read(sid, actor_user_id=drill.ALICE) \
            is False
    assert row_of(conn, "sld_drill_miss01")["asset_state"] == "failed"
    assert row_of(conn, "sld_drill_link01")["asset_state"] == "failed"
    assert row_of(conn, "sld_drill_inflight01")["asset_state"] == "failed"
    # owner 空的 quarantine 行保持 legacy（人工决议通道：backfill 重扫依赖）
    assert row_of(conn, "sld_drill_noown01")["asset_state"] == "legacy"
    # 孤儿文件不建行
    assert slide_store.resolve_legacy_alias("orphan-slide.svs") is None
    # journal 披露
    ev = journal_events(world["journal"])
    noown = [e for e in ev.get("sld_drill_noown01", [])
             if e["phase"] == "skipped"]
    assert noown and noown[0]["detail"]["reason"] == \
        "quarantine_manual_review_pending"
    tomb = [e for e in ev.get("sld_drill_tomb01", [])]
    assert tomb  # tombstone 的 retain_history 处置有留痕


# --------------------------------------------------------------------------- #
# verify 独立性（合同 §1.3 / §4：不信 journal 的 success 字段）
# --------------------------------------------------------------------------- #
def test_verify_catches_tampered_journal_success(world):
    full_apply(world)
    conn = world["conn"]
    # 伪造：行回退成未迁移（模拟「journal 声称成功但绑定从未发生/被回滚」）
    with conn.cursor() as cur:
        cur.execute("UPDATE slides SET storage_layout='legacy', "
                    "storage_relpath=NULL WHERE slide_id='sld_drill_svs01'")
    conn.commit()
    shutil.rmtree(world["up"] / "objects" / "sld_drill_svs01")
    with world["journal"].open("a", encoding="utf-8") as f:
        f.write(json.dumps({
            "ts": "2099-01-01T00:00:00Z", "plan_sha256": world["digest"],
            "item_id": "sld_drill_svs01", "phase": "postverified",
            "result": "ok", "resumed": False, "detail": {}}) + "\n")
    verif = run_verify(world)
    checks = {v["check"] for v in verif["violations"]}
    assert "plan_item_not_migrated" in checks
    assert verif["go_no_go"] == "no-go"


def test_verify_catches_bundle_bytes_corruption(world):
    full_apply(world)
    entry = world["up"] / "objects" / "sld_drill_svs01" / "data.svs"
    entry.write_bytes(entry.read_bytes()[:-1] + b"X")
    verif = run_verify(world, out_name="verify-corrupt")
    checks = {v["check"] for v in verif["violations"]}
    assert "bundle_integrity" in checks
    assert any(v["check"] == "bundle_integrity"
               and any(f["error"] == "sha_mismatch"
                       for f in v["detail"]["bad_files"])
               for v in verif["violations"])
    assert verif["go_no_go"] == "no-go"


def test_verify_incomplete_on_permission_failure(world, monkeypatch):
    full_apply(world)
    target = str(world["up"] / "objects" / "sld_drill_svs01" / "data.svs")
    real = verifier._sha256_file

    def deny(path):
        if str(path) == target:
            raise PermissionError(13, "Permission denied")
        return real(path)

    monkeypatch.setattr(verifier, "_sha256_file", deny)
    verif = run_verify(world, out_name="verify-perm")
    assert verif["incomplete"] is True
    assert "scan_error" in verif["incomplete_reasons"]
    assert verif["go_no_go"].startswith("no-go")


# --------------------------------------------------------------------------- #
# 配额与授权零变更（R-12 / 合同 §4）
# --------------------------------------------------------------------------- #
def test_quota_ledger_untouched(world):
    conn = world["conn"]
    with conn.cursor() as cur:
        cur.execute("SELECT user_id, used_bytes, reserved_bytes "
                    "FROM upload_user_quotas ORDER BY user_id")
        before = cur.fetchall()
    full_apply(world)
    with conn.cursor() as cur:
        cur.execute("SELECT user_id, used_bytes, reserved_bytes "
                    "FROM upload_user_quotas ORDER BY user_id")
        after = cur.fetchall()
    assert before == after


def test_authorization_rows_unchanged(world):
    before = drill._snapshot_auth(world["conn"])
    full_apply(world)
    after = drill._snapshot_auth(world["conn"])
    assert before == after


# --------------------------------------------------------------------------- #
# 物理纪律：复制不硬链接 / 不删源 / 空间阻塞 / 源漂移中止
# --------------------------------------------------------------------------- #
def test_copy_not_hardlink_and_source_retained(world):
    full_apply(world)
    up = world["up"]
    pairs = [("specimen.svs", "sld_drill_svs01", "data.svs"),
             ("scan.tif", "sld_drill_tif01", "data.tif"),
             ("kfb-converted.tif", "sld_drill_kfbp01", "data.tif"),
             ("kfbf-out.ome.tif", "sld_drill_ome01", "data.ome.tif"),
             ("panel.mrxs", "sld_drill_mrxs01", "panel.mrxs")]
    for src_name, sid, entry in pairs:
        src = up / src_name
        dst = up / "objects" / sid / entry
        assert src.is_file(), "不删源：%s" % src_name
        assert dst.is_file()
        assert src.stat().st_ino != dst.stat().st_ino, \
            "复制不得共享 inode：%s" % src_name
    # MRXS 伴侣目录也复制隔离
    comp_src = up / "panel" / "Slidedata.ini"
    comp_dst = up / "objects" / "sld_drill_mrxs01" / "panel" / "Slidedata.ini"
    assert comp_src.stat().st_ino != comp_dst.stat().st_ino


def test_insufficient_space_blocks_item(world, monkeypatch):
    monkeypatch.setattr(
        migrator.shutil, "disk_usage",
        lambda p: SimpleNamespace(free=10, total=100, used=90))
    summary = apply_migrate(world)
    reasons = {f["reason"] for f in summary["failures"]}
    assert reasons == {"insufficient_space"}
    for sid in drill.MIGRATE_IDS:
        assert row_of(world["conn"], sid)["storage_layout"] == "legacy"
        assert not (world["up"] / "objects" / sid).exists()


def test_source_drift_aborts_item(world):
    # 同尺寸翻转末字节（size 不变 → 走 frozen sha 比对路径）
    data = bytearray((world["up"] / "specimen.svs").read_bytes())
    data[-1] ^= 0xFF
    (world["up"] / "specimen.svs").write_bytes(bytes(data))
    summary = apply_migrate(world)
    fails = {f["item_id"]: f["reason"] for f in summary["failures"]}
    assert fails.get("sld_drill_svs01") == "source_sha_changed"
    assert row_of(world["conn"], "sld_drill_svs01")["storage_layout"] == \
        "legacy"
    assert not (world["up"] / "objects" / "sld_drill_svs01").exists()


# --------------------------------------------------------------------------- #
# verify 常规通过路径（端到端）+ tombstone 不复活（§4/§2-5）
# --------------------------------------------------------------------------- #
def test_verify_pass_end_to_end_and_tombstone(world):
    full_apply(world)
    # R6 审查修复（问题 5）两遍法：首遍报告差额（本世界有 failed 隔离/
    # deleted 不退款/派生物校准的合法差额）→ 无核准时阻断（非 go）→
    # 按报告签发核准 → 复跑放行。生产流程的签发是人工审批。
    first = run_verify(world, out_name="verify-first-pass")
    if first["go_no_go"] != "go":
        # 有未归因差额：无核准时必须阻断（R6 审查修复问题 5 的反例语义）
        assert any(v.get("check") == "quota_delta_unapproved"
                   for v in first["violations"]), first["violations"]
    approvals = derive_quota_approvals(first)
    verif = run_verify(world, quota_approvals=approvals)
    assert verif["incomplete"] is False
    assert verif["violations"] == []
    assert verif["counts"]["ready_id_bundle_bad"] == 0
    assert verif["go_no_go"] == "go"
    assert verif["plan_migration_coverage"]["not_migrated"] == []
    assert verif["quarantine_discipline"]["readable"] == []
    assert verif["authorization_diff"]["diffs"] == []
    # tombstone 不复活：旧分享领取人读不到同展示名新资产
    reborn = world["world"]["reborn_id"]
    assert slide_store.authorize_read(reborn, actor_user_id=drill.BOB) is False
    assert slide_store.authorize_read(
        "sld_drill_svs01", actor_user_id=drill.BOB) is True  # 旧分享照常
    assert verif["tombstones"]["crossread_violations"] == []
