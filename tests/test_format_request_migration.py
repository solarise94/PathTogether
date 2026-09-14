# -*- coding: utf-8 -*-
"""W2 JSONL → PG 迁移工具测试（F06，scripts/migrate_format_requests.py）。

合同：
  - preflight 零业务写入：只读源文件与库内 ID，输出统计/坏行/冲突/缺样本；
  - apply 整文件原子：任一坏行或同 ID 异内容 → 拒绝整个导入（exit 1，
    零部分导入）；
  - apply 幂等：重复 apply 零新行、零状态回退（已存在行一律跳过）；
  - 邮件语义：sent/uncertain 原样保留（绝不重发）；queued/failed 导入为
    failed + attempts 顶格（不排队发送）；
  - 样本缺失：请求照常导入，sample_missing=true；
  - apply 前备份源文件。
"""
import importlib.util
import io
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import _bootstrap  # noqa: E402,F401

import psycopg  # noqa: E402
import pytest  # noqa: E402

import format_request_store as frs  # noqa: E402
import pg_store  # noqa: E402
import registration_mail_worker as rmw  # noqa: E402

_REPO = Path(__file__).resolve().parent.parent


def _load_migration_module():
    spec = importlib.util.spec_from_file_location(
        "migrate_format_requests",
        str(_REPO / "scripts" / "migrate_format_requests.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(autouse=True)
def _setup(tmp_path, monkeypatch):
    monkeypatch.setenv("FORMAT_REQUEST_DIR",
                       str(tmp_path / "format_requests"))
    monkeypatch.setattr(frs, "_RETRY_BACKOFF_BASE_SECONDS", 0)
    # session PG 在 0049 落盘前已启动：幂等补应用迁移
    conn = psycopg.connect(os.environ["DATABASE_URL"])
    try:
        pg_store.ensure_schema(conn)
    finally:
        conn.close()
    rmw.install_fake_sender().clear()
    monkeypatch.setenv("REGISTRATION_MAIL_SENDER", "fake")
    yield


def _mkrec(rid, *, user_id="u1", ext=".mrx", mail_status="queued",
           attempts=0, sample=None, last_error=""):
    rec = {
        "id": rid,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "user_id": user_id,
        "format_ext": ext,
        "message": "msg-" + rid,
        "contact": "u@example.com",
        "sample": sample,
        "mail": {"status": mail_status, "attempts": attempts,
                 "last_error": last_error, "updated_at":
                     datetime.now(timezone.utc).isoformat()},
    }
    return rec


def _write_jsonl(path, records):
    with open(path, "w", encoding="utf-8") as f:
        for rec in records:
            f.write(json.dumps(rec, ensure_ascii=False, sort_keys=True))
            f.write("\n")


def _rows():
    conn = psycopg.connect(os.environ["DATABASE_URL"])
    conn.row_factory = psycopg.rows.dict_row
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT r.id, r.sample_missing, j.mail_status, j.attempts "
                "FROM format_requests r LEFT JOIN format_request_mail_jobs j "
                "ON j.request_id = r.id ORDER BY r.id")
            return {r["id"]: dict(r) for r in cur.fetchall()}
    finally:
        conn.close()


def _preflight(mod, path):
    out = io.StringIO()
    code = mod.preflight(path, out=out)
    return code, out.getvalue()


def _apply(mod, path):
    out = io.StringIO()
    code = mod.apply_import(path, out=out)
    return code, out.getvalue()


def test_f06_preflight_no_writes(tmp_path):
    mod = _load_migration_module()
    # 库内已有 1 条业务提交（preflight 只读不写）
    existing = frs.submit_request(user_id="db_user", format_ext=".exist")
    src = tmp_path / "requests.jsonl"
    present = tmp_path / "present.bin"
    present.write_bytes(b"pk")
    _write_jsonl(src, [
        _mkrec("fr_aa01", mail_status="sent", attempts=1,
               sample={"name": "a.bin", "path": str(present),
                       "size": 2, "sha256": "x" * 64}),
        _mkrec("fr_bb02", mail_status="queued"),
        _mkrec("fr_cc03", mail_status="failed", attempts=2,
               sample={"name": "gone.bin", "path":
                       str(tmp_path / "gone.bin"), "size": 5,
                       "sha256": "y" * 64}),
    ])
    before = _rows()
    code, text = _preflight(mod, src)
    assert code == 0
    assert "唯一 ID: 3" in text
    assert "坏行: 0" in text
    assert "同 ID 异内容冲突: 0" in text
    assert "已在库 ID: 0" in text
    assert "样本缺失: 1" in text
    assert "fr_cc03" in text
    # 零业务写入：库内仍只有既有 1 行
    after = _rows()
    assert after == before
    assert set(after) == {existing["id"]}


def test_f06_apply_idempotent_preserves_status_no_enqueue(tmp_path):
    mod = _load_migration_module()
    src = tmp_path / "requests.jsonl"
    present = tmp_path / "s.bin"
    present.write_bytes(b"content")
    _write_jsonl(src, [
        _mkrec("fr_sent1", mail_status="sent", attempts=1),
        _mkrec("fr_unc2", mail_status="uncertain", attempts=1),
        _mkrec("fr_q3", mail_status="queued", attempts=0),
        _mkrec("fr_fail4", mail_status="failed", attempts=2),
        _mkrec("fr_miss5", sample={"name": "m.bin", "path":
                                   str(tmp_path / "m.bin"), "size": 3,
                                   "sha256": "z" * 64}),
    ])
    code, text = _apply(mod, src)
    assert code == 0, text
    rows = _rows()
    assert len(rows) == 5
    assert rows["fr_sent1"]["mail_status"] == "sent"  # 已发送保留
    assert rows["fr_sent1"]["attempts"] == 1
    assert rows["fr_unc2"]["mail_status"] == "uncertain"  # 不确定保留
    assert rows["fr_q3"]["mail_status"] == "failed"  # 不排队发送
    assert rows["fr_q3"]["attempts"] == frs.MAX_SEND_ATTEMPTS
    assert rows["fr_fail4"]["attempts"] == frs.MAX_SEND_ATTEMPTS
    assert rows["fr_miss5"]["sample_missing"] is True  # 缺样本照常导入
    assert rows["fr_sent1"]["sample_missing"] is False
    # 备份已生成
    backups = list(tmp_path.glob("requests.jsonl.bak.*"))
    assert len(backups) == 1

    # 绝不自动发送（attempts 顶格 → 领取范围排除）
    assert frs.drain_once(sender=rmw.install_fake_sender()) == 0
    assert rmw.install_fake_sender().sent == []
    fresh = _rows()
    assert fresh["fr_sent1"]["mail_status"] == "sent"
    assert fresh["fr_q3"]["mail_status"] == "failed"

    # 第二次 apply：零新行、零状态回退
    code2, text2 = _apply(mod, src)
    assert code2 == 0, text2
    assert "新导入 0" in text2
    assert _rows() == fresh


def test_f06_bad_line_or_conflict_rejects_whole_file(tmp_path):
    mod = _load_migration_module()
    src = tmp_path / "requests.jsonl"
    good = [_mkrec("fr_ok1"), _mkrec("fr_ok2")]
    _write_jsonl(src, good)
    with open(src, "a", encoding="utf-8") as f:
        f.write("{not json at all\n")
    code, text = _apply(mod, src)
    assert code == 1
    assert "拒绝导入" in text
    assert _rows() == {}  # 零部分导入

    # 同 ID 异内容 → 拒绝
    src2 = tmp_path / "requests2.jsonl"
    _write_jsonl(src2, good)
    conflict = _mkrec("fr_ok1", ext=".different")
    with open(src2, "a", encoding="utf-8") as f:
        f.write(json.dumps(conflict, ensure_ascii=False,
                           sort_keys=True) + "\n")
    code, text = _apply(mod, src2)
    assert code == 1
    assert _rows() == {}

    # preflight 对坏文件返回不可导入（exit 1）
    code, text = _preflight(mod, src)
    assert code == 1
    assert "坏行: 1" in text
    code, text = _preflight(mod, src2)
    assert code == 1
    assert "同 ID 异内容冲突: 1" in text
    # 好文件修掉坏行后 apply 成功
    src3 = tmp_path / "requests3.jsonl"
    _write_jsonl(src3, good)
    code, text = _apply(mod, src3)
    assert code == 0, text
    assert set(_rows()) == {"fr_ok1", "fr_ok2"}


def test_worker_cli_once_roundtrip(tmp_path):
    """worker CLI --once 在无 sender 时安全返回（退出码 0）。"""
    import subprocess
    env = dict(os.environ)
    env.pop("REGISTRATION_MAIL_SENDER", None)
    proc = subprocess.run(
        [sys.executable, str(_REPO / "scripts" / "format_request_worker.py"),
         "--once"],
        capture_output=True, text=True, env=env, timeout=60)
    assert proc.returncode == 0, proc.stderr
    assert "sent=" in proc.stdout
