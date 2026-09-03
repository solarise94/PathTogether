# -*- coding: utf-8 -*-
"""scripts/repair_pg_user_password_hashes.py 测试（账户系统批次 C，docs §9.2）。

仅 RUN_PG_TESTS=1 下跑（需真实 PG）：验证启动修复 shim 迁出后的主机侧一次性
命令语义：
  - 默认 dry-run：只输出待修复 user_id 计数，不写库，输出绝不含 hash；
  - --apply：仅填充 PG 空 hash，不覆盖非空 hash；幂等（重跑回填 0 行）；
  - json 旧格式（email 键）读侧兼容（口径同 user_store_json._login_id_of）；
  - PG 空 hash 行在 json 无对应权威 hash 时不回填（dry-run 明示人工审计）。
"""
import importlib.util
import json
import sys
from pathlib import Path

import pytest

# 缺基建依赖时整模块 skip（不 fail）。
pytest.importorskip("pgserver")
pytest.importorskip("psycopg")

import psycopg  # noqa: E402

_REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def repair():
    """加载修复脚本模块（独立模块名，不污染应用模块）。"""
    spec = importlib.util.spec_from_file_location(
        "repair_pg_user_password_hashes",
        str(_REPO_ROOT / "scripts" / "repair_pg_user_password_hashes.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _write_users_json(path: Path):
    """两条权威记录：一条旧格式（email 键），一条新格式（login_id 键）。"""
    path.write_text(json.dumps({
        "users": {
            "usr_rpa": {"user_id": "usr_rpa", "email": "old@x.com",
                        "password_hash": "pbkdf2:fake-old",
                        "role": "user", "created_at": 1, "disabled": False},
            "usr_rpb": {"user_id": "usr_rpb", "login_id": "new@x.com",
                        "password_hash": "pbkdf2:fake-new",
                        "role": "user", "created_at": 1, "disabled": False},
        }, "meta": {"schema_version": 1}}), encoding="utf-8")


def _seed_pg_empty_and_nonempty(pg_uri):
    """usr_rpa 空 hash（待回填）、usr_rpb 非空（不得覆盖）。"""
    c = psycopg.connect(pg_uri)
    try:
        with c.cursor() as cur:
            cur.execute(
                "INSERT INTO users (user_id, login_id, password_hash, role) "
                "VALUES ('usr_rpa','old@x.com','','user'),"
                "('usr_rpb','new@x.com','pbkdf2:kept','user')")
        c.commit()
    finally:
        c.close()


def _hash_of(pg_uri, uid):
    c = psycopg.connect(pg_uri)
    try:
        with c.cursor() as cur:
            cur.execute("SELECT password_hash FROM users WHERE user_id=%s",
                        (uid,))
            row = cur.fetchone()
        return row[0] if row else None
    finally:
        c.close()


def test_repair_script_dry_run_default_no_write(repair, pg_uri, tmp_path,
                                                capsys):
    _write_users_json(tmp_path / "users.json")
    _seed_pg_empty_and_nonempty(pg_uri)
    rc = repair.main(["--json-file", str(tmp_path / "users.json")])
    out, err = capsys.readouterr()
    assert rc == 0
    assert "dry-run" in out
    assert "可从 json 回填 1 条" in out
    assert "usr_rpa" in out
    # 输出绝不包含 hash / 密码
    assert "pbkdf2" not in out and "pbkdf2" not in err
    # dry-run 不写库
    assert _hash_of(pg_uri, "usr_rpa") == ""


def test_repair_script_apply_fills_only_empty_idempotent(repair, pg_uri,
                                                         tmp_path, capsys):
    _write_users_json(tmp_path / "users.json")
    _seed_pg_empty_and_nonempty(pg_uri)
    rc = repair.main(["--json-file", str(tmp_path / "users.json"), "--apply"])
    out, err = capsys.readouterr()
    assert rc == 0
    assert "回填 1 行" in out
    assert "pbkdf2" not in out and "pbkdf2" not in err  # 不打印 hash
    # 旧格式 email 键记录同样回填；非空 hash 不被覆盖
    assert _hash_of(pg_uri, "usr_rpa") == "pbkdf2:fake-old"
    assert _hash_of(pg_uri, "usr_rpb") == "pbkdf2:kept"
    # 幂等：重跑无可回填
    rc2 = repair.main(["--json-file", str(tmp_path / "users.json"), "--apply"])
    out2, _ = capsys.readouterr()
    assert rc2 == 0 and "回填 0 行" in out2


def test_repair_script_empty_hash_without_json_authority_flagged(
        repair, pg_uri, tmp_path, capsys):
    """PG 空 hash 行在 json 无权威 hash：dry-run 明示人工审计，apply 不动它。"""
    (tmp_path / "users.json").write_text(json.dumps(
        {"users": {}, "meta": {"schema_version": 1}}), encoding="utf-8")
    c = psycopg.connect(pg_uri)
    try:
        with c.cursor() as cur:
            cur.execute(
                "INSERT INTO users (user_id, login_id, password_hash, role) "
                "VALUES ('usr_orphan','orphan@x.com','','user')")
        c.commit()
    finally:
        c.close()
    rc = repair.main(["--json-file", str(tmp_path / "users.json")])
    out, _ = capsys.readouterr()
    assert rc == 0
    assert "人工审计" in out
    assert _hash_of(pg_uri, "usr_orphan") == ""
    rc2 = repair.main(["--json-file", str(tmp_path / "users.json"), "--apply"])
    capsys.readouterr()
    assert rc2 == 0
    assert _hash_of(pg_uri, "usr_orphan") == ""  # 仍不回填
