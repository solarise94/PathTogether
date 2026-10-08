# -*- coding: utf-8 -*-
"""scripts/audit_slide_identity.py 测试（slide ID 化重构 P0）。

覆盖 runbook §2.3 P0 必查项的工具侧验收：
  - 双向核对：孤儿→quarantine、缺文件→missing_file/retain_history、
    symlink/hardlink 识别、owner 空/不存在→manual_review；
  - MRXS 伴侣目录、转换 sidecar（manifest/associated）、暂存残留、白名单；
  - 引用计数（shares/grants/view grants/demo/run/AI/项目/标注）与悬空授权
    →unresolved；
  - 活跃任务（upload_tasks/ingestion_jobs/conversion_jobs/baidu_import_items）；
  - 容量对账 quota_mismatch 与 --quota-tolerance-bytes；
  - online 非一致快照 / frozen 逐文件 SHA-256 + 一致快照；
  - 输出目录 0700、文件 0600、恰好四个文件；报告无 token 明文；
  - incomplete（扫描错误/读取期间变化）→ 退出码 3；
  - 工具不 import app/share_store_pg 等业务模块（静态契约）。

测试自建 UPLOAD_DIR 夹具（tmp_path）+ pg_uri 直连种数据（SQL 直插，参考
tests/test_slide_identity_pg.py 的 conn fixture 写法；users 表列见
migrations/0001_init.sql）。工具模块经 importlib 独立加载，不污染应用模块。
"""
import ast
import hashlib
import importlib.util
import json
import os
import stat as stat_mod
from pathlib import Path

import pytest

import psycopg  # noqa: E402

_REPO_ROOT = Path(__file__).resolve().parent.parent
_SCRIPT = _REPO_ROOT / "scripts" / "audit_slide_identity.py"

#: 禁止审计工具 import 的业务模块（import app 会起 worker/建表；仓储模块有
#: probe 副作用）。测试静态核对该契约。
_BANNED_MODULES = {
    "app", "share_store", "share_store_pg", "pg_store", "user_store",
    "user_store_pg", "upload_task_store", "upload_guard", "ingestion_store",
    "conversion_store", "baidu_import_store", "baidu_ingest", "demo_store",
    "share_server", "slide_store", "slide_storage",
}


@pytest.fixture
def audit():
    """加载审计脚本模块（独立模块名，不进应用模块命名空间）。"""
    spec = importlib.util.spec_from_file_location(
        "audit_slide_identity", str(_SCRIPT))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def conn(pg_uri):
    """每用例新连接（autocommit=False，dict_row 便于按列名取）。"""
    c = psycopg.connect(pg_uri)
    c.row_factory = psycopg.rows.dict_row
    yield c
    c.close()


# --------------------------------------------------------------------------- #
# 种子与夹具
# --------------------------------------------------------------------------- #

def _exec(conn, sql, params=()):
    with conn.cursor() as cur:
        cur.execute(sql, params)


def seed_users(conn):
    # users 表：0001 建（email NOT NULL），0016 改名 login_id（NOT NULL），
    # 0037 重加可空 email + 激活列（默认值即可）。
    _exec(conn,
          "INSERT INTO users (user_id, login_id, role) VALUES "
          "('usr_a', 'a@t.example', 'user'), "
          "('usr_b', 'b@t.example', 'user')")


def seed_slides(conn):
    """8 行：正常/缺文件/硬链双方/符号链/MRXS/无 owner/owner 不存在。"""
    _exec(conn,
          "INSERT INTO slides (slide_id, legacy_filename, owner_user_id, public)"
          " VALUES "
          "('sld_a', 'a.svs', 'usr_a', true), "
          "('sld_b', 'b.svs', 'usr_a', false), "
          "('sld_h1', 'h1.svs', 'usr_a', false), "
          "('sld_h2', 'h2.svs', 'usr_b', false), "
          "('sld_link', 'link.svs', 'usr_a', false), "
          "('sld_m', 'slide.mrxs', 'usr_a', false), "
          "('sld_none', 'none.svs', NULL, false), "
          "('sld_ghost', 'ghostowner.svs', 'usr_ghost', false)")


def seed_references(conn, token):
    """a.svs 的全量引用 + 一条悬空业务引用（deleted.svs 无 slides 行）。"""
    _exec(conn, "INSERT INTO shares (token, slides) VALUES (%s, %s::jsonb)",
          (token, json.dumps(["a.svs"])))
    _exec(conn,
          "INSERT INTO grants (id, token, user_id, active) "
          "VALUES ('grt_1', %s, 'usr_b', true)", (token,))
    _exec(conn,
          "INSERT INTO slide_view_grants (slide_name, user_id, slide_id, "
                "expires_at) "
          "VALUES ('a.svs', 'usr_b', 'sld_a', "
          " now() + interval '30 days')")
    _exec(conn, "INSERT INTO projects (project_id, name) VALUES ('prj_1', 'P')")
    _exec(conn,
          "INSERT INTO project_slides (project_id, slide) "
          "VALUES ('prj_1', 'a.svs')")
    _exec(conn,
          "INSERT INTO rois (id, token, slide, annotation_id, type) VALUES "
          "('roi_1', 'admin', 'a.svs', 'ann_1', 'rect'), "
          "('roi_2', 'admin', 'deleted.svs', 'ann_2', 'rect')")
    _exec(conn,
          "INSERT INTO comments (comment_id, slide, token) "
          "VALUES ('cmt_1', 'a.svs', 'admin')")
    _exec(conn,
          "INSERT INTO change_log (slide, token, op) "
          "VALUES ('a.svs', 'admin', 'add')")
    _exec(conn,
          "INSERT INTO annotation_access_events (seq, slide, annotation_id, op,"
          " grantee_kind, grantee_id) VALUES "
          "(1, 'a.svs', 'ann_1', 'grant', 'user', 'usr_b')")
    _exec(conn,
          "INSERT INTO run_grants (grant_id, installation_id, slide, expires_at)"
          " VALUES ('rgr_1', 'inst_1', 'a.svs', now() + interval '1 hour')")
    _exec(conn,
          "INSERT INTO ai_session_principals (session_id, user_id, slide) "
          "VALUES ('sess_1', 'usr_b', 'a.svs')")
    _exec(conn,
          "INSERT INTO demo_catalog (slide_id, display_name) "
          "VALUES ('sld_a', 'A')")


def seed_quota(conn):
    # usr_a 名下实际入口字节 = a.svs(10) + h1.svs(8) = 18 ≠ 500 → mismatch。
    _exec(conn,
          "INSERT INTO upload_user_quotas (user_id, quota_bytes, used_bytes,"
          " reserved_bytes) VALUES ('usr_a', 100000, 500, 0)")


def seed_world(conn, token="tok_SECRETTOKEN42"):
    seed_users(conn)
    seed_slides(conn)
    seed_references(conn, token)
    seed_quota(conn)
    conn.commit()


def make_upload_dir(tmp_path):
    """UPLOAD_DIR 夹具：正常/硬链/孤儿/符号链/MRXS 伴侣/暂存残留/sidecar。"""
    up = tmp_path / "uploads"
    up.mkdir()
    (up / "a.svs").write_bytes(b"A" * 10)
    (up / "h1.svs").write_bytes(b"H" * 8)
    os.link(up / "h1.svs", up / "h2.svs")          # nlink=2，双方都是 slides 行
    (up / "ghostowner.svs").write_bytes(b"G" * 4)
    (up / "none.svs").write_bytes(b"N" * 6)
    (up / "orphan.svs").write_bytes(b"O" * 12)     # 无 slides 行 → 孤儿
    # b.svs 故意不创建：slides 行在、文件缺 → missing_file
    target = tmp_path / "outside-target.svs"
    target.write_bytes(b"T" * 5)
    os.symlink(target, up / "link.svs")            # 资产符号链接（不跟随）
    (up / "slide.mrxs").write_bytes(b"M" * 7)
    comp = up / "slide"                            # MRXS 同 stem 伴侣目录
    comp.mkdir()
    (comp / "Slidedata.ini").write_bytes(b"S" * 20)
    (up / ".uploading-abc123.part").write_bytes(b"P" * 3)  # 暂存残留
    (up / "a.svs.manifest.json").write_bytes(b"{}")        # 转换 sidecar（白名单）
    assoc = up / "a.svs.associated"
    assoc.mkdir()
    (assoc / "tile.bin").write_bytes(b"x" * 2)
    return up


# --------------------------------------------------------------------------- #
# 辅助
# --------------------------------------------------------------------------- #

def run_audit(audit_mod, pg_uri, upload_dir, out_dir, mode="online", extra=()):
    return audit_mod.main([
        "--database-url", pg_uri,
        "--upload-dir", str(upload_dir),
        "--out-dir", str(out_dir),
        "--mode", mode,
    ] + list(extra))


def read_jsonl(path):
    return [json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()]


def issues_of(out_dir, type_=None, **kw):
    out = []
    for rec in read_jsonl(out_dir / "issues.jsonl"):
        if type_ is not None and rec["type"] != type_:
            continue
        if all(rec.get(k) == v for k, v in kw.items()):
            out.append(rec)
    return out


def inventory_of(out_dir, **kw):
    out = []
    for rec in read_jsonl(out_dir / "inventory.jsonl"):
        if all(rec.get(k) == v for k, v in kw.items()):
            out.append(rec)
    return out


def find_issue(out_dir, type_, **kw):
    hits = issues_of(out_dir, type_, **kw)
    assert hits, "未找到 issue %s%s" % (type_, kw)
    return hits


def find_record(out_dir, **kw):
    hits = inventory_of(out_dir, **kw)
    assert hits, "未找到 inventory %s" % kw
    return hits


# --------------------------------------------------------------------------- #
# 静态契约：不 import 业务模块
# --------------------------------------------------------------------------- #

def test_no_business_module_imports():
    src = _SCRIPT.read_text(encoding="utf-8")
    found = set()
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.Import):
            found |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            found.add(node.module.split(".")[0])
    assert not (found & _BANNED_MODULES), \
        "审计工具 import 了业务模块：%s" % (found & _BANNED_MODULES)


# --------------------------------------------------------------------------- #
# 核心：双向核对（online）
# --------------------------------------------------------------------------- #

def test_online_core_double_check(audit, conn, pg_uri, tmp_path):
    seed_world(conn)
    up = make_upload_dir(tmp_path)
    before = sorted(os.listdir(up))
    out = tmp_path / "out"
    rc = run_audit(audit, pg_uri, up, out, mode="online")
    assert rc == 0

    # 恰好四个输出文件；权限 0600 / 目录 0700。
    assert sorted(os.listdir(out)) == ["inventory.jsonl", "issues.jsonl",
                                       "summary.md", "verification.json"]
    assert stat_mod.S_IMODE(os.stat(out).st_mode) == 0o700
    for name in ("inventory.jsonl", "issues.jsonl", "summary.md",
                 "verification.json"):
        assert stat_mod.S_IMODE(os.stat(out / name).st_mode) == 0o600

    # 源目录只读：文件集不变。
    assert sorted(os.listdir(up)) == before

    # 缺文件 → missing_file / retain_history / blocker。
    miss = find_issue(out, "missing_file", legacy_filename="b.svs")
    assert miss[0]["severity"] == "blocker"
    assert miss[0]["disposition"] == "retain_history"

    # 孤儿 → orphan_file / quarantine。
    orph = find_issue(out, "orphan_file", legacy_filename="orphan.svs")
    assert orph[0]["disposition"] == "quarantine"

    # 符号链接（资产行）→ 不跟随、blocker。
    lnk = find_issue(out, "symlink", legacy_filename="link.svs")
    assert lnk[0]["severity"] == "blocker"
    rec_link = find_record(out, record_type="slide",
                           legacy_filename="link.svs")[0]
    assert rec_link["is_symlink"] is True
    assert rec_link["sha256"] is None

    # 硬链识别：双方 nlink=2 + hardlink_multi；owner 不同 → owner_ambiguous。
    assert find_issue(out, "hardlink_multi", legacy_filename="h1.svs")
    assert find_issue(out, "hardlink_multi", legacy_filename="h2.svs")
    find_issue(out, "owner_ambiguous")
    for name in ("h1.svs", "h2.svs"):
        rec = find_record(out, record_type="slide", legacy_filename=name)[0]
        assert rec["file_nlink"] == 2

    # owner 空 / 不在 users → manual_review。
    assert find_issue(out, "owner_missing", legacy_filename="none.svs")
    ghost = find_issue(out, "owner_missing", legacy_filename="ghostowner.svs")
    assert ghost[0]["disposition"] == "manual_review"

    # 暂存残留 → info。
    stg = find_issue(out, "staging_leftover")
    assert stg and all(i["severity"] == "info" for i in stg)

    # MRXS 伴侣目录记录 + slide.mrxs 的 has_companion_dir。
    comp = find_record(out, record_type="companion_dir",
                       legacy_filename="slide")[0]
    assert comp["slide_id"] == "sld_m"
    assert comp["file_size"] == 20
    assert comp["companion_file_count"] == 1
    rec_m = find_record(out, record_type="slide",
                        legacy_filename="slide.mrxs")[0]
    assert rec_m["has_companion_dir"] is True

    # a.svs：sidecar 检测 + 引用计数（token 不入报告，只计数）。
    rec_a = find_record(out, record_type="slide", legacy_filename="a.svs")[0]
    assert rec_a["file_exists"] is True
    assert rec_a["file_size"] == 10
    assert rec_a["has_manifest"] is True
    assert rec_a["has_associated_dir"] is True
    refs = rec_a["references"]
    for kind, want in {
        "shares": 1, "share_grants_active": 1, "view_grants_by_name": 1,
        "view_grants_by_id": 1, "demo_catalog": 1, "run_grants": 1,
        "ai_session_principals": 1, "project_slides": 1, "rois": 1,
        "comments": 1, "change_log": 1, "annotation_access_events": 1,
    }.items():
        assert refs.get(kind) == want, (kind, refs)

    # 悬空业务引用 → dangling_reference / retain_history（历史保留）。
    dang = find_issue(out, "dangling_reference", legacy_filename="deleted.svs")
    assert dang[0]["severity"] == "info"
    assert dang[0]["disposition"] == "retain_history"

    # verification：online 明确非一致快照、无 incomplete、计数正确。
    verif = json.loads((out / "verification.json").read_text(encoding="utf-8"))
    assert verif["mode"] == "online"
    assert verif["consistent_snapshot"] is False
    assert verif["incomplete"] is False
    assert verif["started_at"] and verif["finished_at"]
    assert verif["counts"]["slides"] == 8
    assert verif["counts"]["slides_with_file"] == 6
    assert verif["counts"]["slides_missing_file"] == 1
    assert verif["counts"]["orphan_files"] == 1
    assert verif["counts"]["companion_dirs"] == 1
    assert verif["counts"]["staging_leftover"] == 1
    assert verif["counts"]["whitelisted_entries"] == 2  # manifest + associated
    assert verif["counts"]["slide_logical_bytes"] == 43  # 10+8+8+4+6+7
    assert verif["counts"]["companion_bytes"] == 20
    assert verif["counts"]["orphan_bytes"] == 12

    # summary.md：脱敏（无文件名 / 用户 ID / token），有 go/no-go。
    summary = (out / "summary.md").read_text(encoding="utf-8")
    for secret in ("a.svs", "usr_a", "usr_", "tok_", "SECRETTOKEN"):
        assert secret not in summary, secret
    assert "go" in summary or "no-go" in summary


# --------------------------------------------------------------------------- #
# frozen：逐文件 SHA-256 + 一致快照
# --------------------------------------------------------------------------- #

def test_frozen_hashes_and_consistent_snapshot(audit, conn, pg_uri, tmp_path):
    seed_world(conn)
    up = make_upload_dir(tmp_path)
    out = tmp_path / "out"
    rc = run_audit(audit, pg_uri, up, out, mode="frozen")
    assert rc == 0
    rec_a = find_record(out, record_type="slide", legacy_filename="a.svs")[0]
    assert rec_a["sha256"] == hashlib.sha256(b"A" * 10).hexdigest()
    rec_orph = find_record(out, record_type="orphan_file",
                           legacy_filename="orphan.svs")[0]
    assert rec_orph["sha256"] == hashlib.sha256(b"O" * 12).hexdigest()
    verif = json.loads((out / "verification.json").read_text(encoding="utf-8"))
    assert verif["consistent_snapshot"] is True
    assert verif["incomplete"] is False
    assert verif["db"]["isolation"].startswith("REPEATABLE READ")


def test_frozen_changed_during_read_incomplete(audit, conn, pg_uri, tmp_path,
                                               monkeypatch):
    """读取期间文件变化（哈希前后 mtime/size 不一致）→ incomplete + 退出码 3。"""
    seed_world(conn)
    up = make_upload_dir(tmp_path)
    orig = audit._stream_sha256

    def bump_then_hash(path):
        with open(path, "ab") as f:
            f.write(b"!")
        return orig(path)

    monkeypatch.setattr(audit, "_stream_sha256", bump_then_hash)
    out = tmp_path / "out"
    rc = run_audit(audit, pg_uri, up, out, mode="frozen")
    assert rc == 3
    find_issue(out, "changed_during_read")
    verif = json.loads((out / "verification.json").read_text(encoding="utf-8"))
    assert verif["incomplete"] is True
    assert "changed_during_read" in verif["incomplete_reasons"]
    assert verif["consistent_snapshot"] is False


def test_incomplete_scan_error_exit_3(audit, conn, pg_uri, tmp_path,
                                      monkeypatch):
    """不可读文件 → scan_error + incomplete + 退出码 3。

    取舍：chmod 000 在 root 运行下仍可读，故同时 monkeypatch
    _stream_sha256 对该文件抛 PermissionError，保证任何 uid 下都能复现
    「无权限 → incomplete」路径；非 root 环境真实 chmod 即可触发同一分支。
    """
    seed_world(conn)
    up = make_upload_dir(tmp_path)
    unreadable = up / "unreadable.svs"
    unreadable.write_bytes(b"U" * 5)
    os.chmod(unreadable, 0)
    orig = audit._stream_sha256

    def deny(path):
        if str(path).endswith("unreadable.svs"):
            raise PermissionError(13, "Permission denied")
        return orig(path)

    monkeypatch.setattr(audit, "_stream_sha256", deny)
    out = tmp_path / "out"
    try:
        rc = run_audit(audit, pg_uri, up, out, mode="frozen")
    finally:
        os.chmod(unreadable, 0o644)  # 便于 tmp_path 清理
    assert rc == 3
    find_issue(out, "scan_error", legacy_filename="unreadable.svs")
    verif = json.loads((out / "verification.json").read_text(encoding="utf-8"))
    assert verif["incomplete"] is True
    assert "scan_error" in verif["incomplete_reasons"]


# --------------------------------------------------------------------------- #
# 报告不含 token 明文
# --------------------------------------------------------------------------- #

def test_no_share_token_plaintext_in_reports(audit, conn, pg_uri, tmp_path):
    token = "tok_LEAKME_7c31ab"
    seed_world(conn, token=token)
    up = make_upload_dir(tmp_path)
    out = tmp_path / "out"
    assert run_audit(audit, pg_uri, up, out, mode="frozen") == 0
    for name in ("inventory.jsonl", "issues.jsonl", "summary.md",
                 "verification.json"):
        text = (out / name).read_text(encoding="utf-8")
        assert token not in text, name
        assert "LEAKME" not in text, name


# --------------------------------------------------------------------------- #
# 容量对账
# --------------------------------------------------------------------------- #

def test_quota_mismatch_and_tolerance(audit, conn, pg_uri, tmp_path):
    seed_world(conn)
    up = make_upload_dir(tmp_path)

    out1 = tmp_path / "out1"
    assert run_audit(audit, pg_uri, up, out1) == 0
    hits = find_issue(out1, "quota_mismatch")
    assert any("usr_a" in i["detail"] for i in hits)
    assert all(i["severity"] == "review" for i in hits)

    out2 = tmp_path / "out2"
    assert run_audit(audit, pg_uri, up, out2,
                     extra=["--quota-tolerance-bytes", "1000000"]) == 0
    assert issues_of(out2, "quota_mismatch") == []


# --------------------------------------------------------------------------- #
# 活跃任务
# --------------------------------------------------------------------------- #

def seed_active_tasks(conn):
    # a.svs：资产上的活跃转换任务。
    _exec(conn,
          "INSERT INTO conversion_jobs (id, owner_user_id, source_name,"
          " source_sha256, source_format, converter_id, converter_version,"
          " state) VALUES ('cnv_1', 'usr_a', 'a.svs', 'deadbeef', 'kfb',"
          " 'kfb2tif', '1', 'queued')")
    # c.svs：孤儿文件上的活跃 V2 上传任务。
    _exec(conn,
          "INSERT INTO upload_tasks (upload_id, owner_user_id, filename,"
          " safe_name, declared_size, chunk_size, expires_at, state)"
          " VALUES ('upt_1', 'usr_a', 'c.svs', 'c.svs', 4, 2,"
          " now() + interval '1 day', 'active')")
    # d.svs：无行无文件的在途摄取。
    _exec(conn,
          "INSERT INTO ingestion_jobs (job_id, owner_user_id, owner_role,"
          " filename, safe_name, format_ext, declared_size, state)"
          " VALUES ('inj_1', 'usr_a', 'user', 'd.svs', 'd.svs', 'svs', 4,"
          " 'validating')")
    # e.svs：百度导入条目（需先种 enumeration/batch，见 0051/0052）。
    _exec(conn,
          "INSERT INTO baidu_enumerations (id, owner_user_id, share_url_enc)"
          " VALUES ('baid_enum1', 'usr_a', 'enc')")
    _exec(conn,
          "INSERT INTO baidu_import_batches (id, owner_user_id,"
          " enumeration_id, idempotency_key, payload_sha256)"
          " VALUES ('baid_b1', 'usr_a', 'baid_enum1', 'idem-1', 'cafe')")
    _exec(conn,
          "INSERT INTO baidu_import_items (id, batch_id, candidate_id, fs_id,"
          " name, relative_path, stage, slide_name)"
          " VALUES ('baid_i1', 'baid_b1', 'cand_1', 'fs_1', 'e.svs', '/e.svs',"
          " 'ingesting', 'e.svs')")
    conn.commit()


def test_active_tasks_detected(audit, conn, pg_uri, tmp_path):
    seed_world(conn)
    seed_active_tasks(conn)
    up = make_upload_dir(tmp_path)
    (up / "c.svs").write_bytes(b"C" * 4)  # 孤儿文件 + 活跃上传任务
    out = tmp_path / "out"
    assert run_audit(audit, pg_uri, up, out) == 0

    # 资产 a.svs 上的转换任务：references + active_task issue。
    rec_a = find_record(out, record_type="slide", legacy_filename="a.svs")[0]
    assert rec_a["references"]["conversion_jobs_active"] == 1
    assert find_issue(out, "active_task", legacy_filename="a.svs")

    # 孤儿 c.svs 上的上传任务。
    rec_c = find_record(out, record_type="orphan_file",
                        legacy_filename="c.svs")[0]
    assert rec_c["references"]["upload_tasks_active"] == 1
    assert find_issue(out, "active_task", legacy_filename="c.svs")

    # 无行无文件的名字（d.svs / e.svs）：引用悬空路径报 active_task。
    assert find_issue(out, "active_task", legacy_filename="d.svs")
    assert find_issue(out, "active_task", legacy_filename="e.svs")
    # issue 细节包含种类名，便于排空。
    assert any("ingestion_jobs_active" in i["detail"]
               for i in issues_of(out, "active_task"))
    assert any("baidu_import_items_active" in i["detail"]
               for i in issues_of(out, "active_task"))


# --------------------------------------------------------------------------- #
# 悬空授权 → unresolved
# --------------------------------------------------------------------------- #

def test_unresolved_grants(audit, conn, pg_uri, tmp_path):
    seed_world(conn)
    # 悬空 share 成员（无 slides 行）+ 其领取。
    _exec(conn, "INSERT INTO shares (token, slides) VALUES"
                " ('tok_dangle', %s::jsonb)", (json.dumps(["ghostshare.svs"]),))
    _exec(conn,
          "INSERT INTO grants (id, token, user_id, active) "
          "VALUES ('grt_2', 'tok_dangle', 'usr_b', true)")
    # 孤儿 view grant（无 meta 行、NULL id——0035 形态，当前仍生效）。
    _exec(conn,
          "INSERT INTO slide_view_grants (slide_name, user_id, "
          "expires_at) VALUES ('ghostview.svs', 'usr_b', "
          " now() + interval '30 days')")
    # 生代不匹配的 view grant（a.svs 的 slides 行是 sld_a；PK 是
    # (slide_name, user_id)，故用不同 user_id 与正确授权并存）。
    _exec(conn,
          "INSERT INTO slide_view_grants (slide_name, user_id, slide_id, "
                "expires_at) "
          "VALUES ('a.svs', 'usr_a', 'sld_other', "
          " now() + interval '30 days')")
    # demo_catalog 悬空 slide_id。
    _exec(conn,
          "INSERT INTO demo_catalog (slide_id) VALUES ('sld_missing')")
    conn.commit()

    up = make_upload_dir(tmp_path)
    out = tmp_path / "out"
    assert run_audit(audit, pg_uri, up, out) == 0
    hits = issues_of(out, "unresolved_grant")
    assert all(h["disposition"] == "unresolved" for h in hits)
    names = {h.get("legacy_filename") or h.get("slide_id") for h in hits}
    assert {"ghostshare.svs", "ghostview.svs", "a.svs",
            "sld_missing"} <= names


# --------------------------------------------------------------------------- #
# 特殊文件 / 根下未知目录 / 非资产符号链接
# --------------------------------------------------------------------------- #

def test_special_files_and_unknown_dirs(audit, pg_uri, tmp_path):
    up = tmp_path / "uploads"
    up.mkdir()
    os.mkfifo(up / "pipe.fifo")
    os.symlink("/etc/hosts", up / "straylink")
    (up / "junkdir").mkdir()
    (up / "junkdir" / "x.bin").write_bytes(b"J" * 3)
    out = tmp_path / "out"
    assert run_audit(audit, pg_uri, up, out) == 0
    fifo = find_issue(out, "special_file", legacy_filename=None)
    assert any("pipe.fifo" in (i["path"] or "") for i in fifo)
    find_issue(out, "symlink")           # 非资产符号链接 → 隔离报告
    find_issue(out, "orphan_dir")        # 未知目录 → 人工分类
    verif = json.loads((out / "verification.json").read_text(encoding="utf-8"))
    assert verif["counts"]["unknown_dirs"] == 1
    assert verif["counts"]["root_symlinks"] == 1
    assert verif["counts"]["special_files"] == 1


# --------------------------------------------------------------------------- #
# 分页与限速参数
# --------------------------------------------------------------------------- #

def test_pagination_small_page_size(audit, conn, pg_uri, tmp_path):
    seed_users(conn)
    for i in range(5):
        _exec(conn,
              "INSERT INTO slides (slide_id, legacy_filename, owner_user_id)"
              " VALUES (%s, %s, 'usr_a')", ("sld_p%d" % i, "p%d.svs" % i))
    conn.commit()
    up = tmp_path / "uploads"
    up.mkdir()
    for i in range(5):
        (up / ("p%d.svs" % i)).write_bytes(b"p")
    out = tmp_path / "out"
    rc = run_audit(audit, pg_uri, up, out, extra=["--page-size", "1",
                                                  "--rate-limit", "500"])
    assert rc == 0
    verif = json.loads((out / "verification.json").read_text(encoding="utf-8"))
    assert verif["counts"]["slides"] == 5
    assert issues_of(out, "pagination_gap") == []


# --------------------------------------------------------------------------- #
# 工具自身错误 → 退出码 1
# --------------------------------------------------------------------------- #

def test_tool_error_exit_1(audit, pg_uri, tmp_path, capsys):
    out = tmp_path / "out"
    # UPLOAD_DIR 不存在。
    rc = run_audit(audit, pg_uri, tmp_path / "no-such-dir", out)
    assert rc == 1
    # 非法参数。
    assert audit.main(["--out-dir", str(out), "--page-size", "0"]) == 1
    assert audit.main(["--out-dir", str(out), "--rate-limit", "-1"]) == 1
    assert "audit_slide_identity" in capsys.readouterr().err
