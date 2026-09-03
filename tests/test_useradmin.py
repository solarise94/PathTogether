# -*- coding: utf-8 -*-
"""break-glass CLI（python3 -m useradmin）测试（账户系统批次 A，docs §7.3）。

仅当 ``RUN_PG_TESTS=1`` 时真跑（CLI 直连 PostgreSQL；conftest 已起 pgserver、
设 DATABASE_URL 并按用例 TRUNCATE）。json 默认模式整模块 skip。

覆盖：
  - 成功重置：hash 更新 + auth_version+1 + audit 落库（actor NULL、detail 只含
    source/sessions_revoked）+ 输出含 user_id/login_id/「旧 session 已失效」，
    且任何输出不含密码与 hash；
  - --login-id 大小写不敏感 + trim；--password-file 路径；
  - disabled 唯一 owner：无 --enable 拒绝（库不动）；带 --enable 同事务恢复
    （disabled=false + 版本递增 + audit 含 reenabled=true）；enabled 目标配
    --enable 拒绝；
  - 0 个 owner 行（空表 / 只有普通 user）拒绝并输出逃生路径，绝不静默建号；
  - owner 定位以**唯一 enabled owner** 为准（P2 修复，与启动模型/0015 索引
    一致）：1 enabled + 历史 disabled 行 → 正常重置 enabled owner，disabled
    历史行不动；--login-id 命中 disabled 历史行 → 拒绝并说明；0 enabled 且
    >1 全禁用 → 无法唯一定位拒绝；>1 enabled（临时落下 0015 索引构造）→
    不变量破坏拒绝（不选「第一条」）；--login-id 不匹配拒绝；
  - 短密码 / 空 / 全空白拒绝（统一 15..200 策略）；
  - 原子性：audit 写失败（触发器注入异常）→ 密码更新一并回滚；
  - 未设 DATABASE_URL → 清晰报错非 0 退出。

调用 ``useradmin.main(argv)`` 直接驱动（不 shell out），stdin 用 StringIO 注入。
"""
import io
import json
import os
import sys
from pathlib import Path

import pytest

# 缺基建依赖时整模块 skip（不 fail）。
pytest.importorskip("pgserver")
pytest.importorskip("psycopg")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import psycopg  # noqa: E402
import user_store_pg  # noqa: E402

import useradmin  # noqa: E402  （PG 可用后再 import：依赖 psycopg/pg_store）

OLD_PW = "owner-old-pass-12345"
NEW_PW = "owner-new-pass-67890"


# --------------------------------------------------------------------------- #
# 辅助
# --------------------------------------------------------------------------- #
def _bootstrap_owner(login_id="browser_admin", password=OLD_PW):
    owner = user_store_pg.create_bootstrap_owner(login_id, password)
    assert owner and owner["role"] == "owner"
    return owner


def _fetch(pg_uri, sql, params=()):
    c = psycopg.connect(pg_uri)
    c.row_factory = psycopg.rows.dict_row
    try:
        with c.cursor() as cur:
            cur.execute(sql, params)
            return cur.fetchall()
    finally:
        c.close()


def _row(pg_uri, sql, params=()):
    rows = _fetch(pg_uri, sql, params)
    return rows[0] if rows else None


def _run_stdin(monkeypatch, argv, stdin_text):
    monkeypatch.setattr(sys, "stdin", io.StringIO(stdin_text))
    return useradmin.main(argv)


def _last_audit(pg_uri):
    return _row(
        pg_uri,
        "SELECT actor_user_id, action, target_type, target_id, detail "
        "FROM audit_events WHERE action=%s "
        "ORDER BY ts DESC, event_id DESC LIMIT 1",
        (useradmin.AUDIT_ACTION,),
    )


# --------------------------------------------------------------------------- #
# 成功路径
# --------------------------------------------------------------------------- #
def test_reset_success_stdin(pg_uri, capsys, monkeypatch):
    owner = _bootstrap_owner()
    rc = _run_stdin(monkeypatch, [
        "reset-owner-password", "--login-id", "browser_admin",
        "--password-stdin"], NEW_PW + "\n")
    assert rc == 0, capsys.readouterr().err

    # 库内：hash 更新 + auth_version 1→2 + disabled 不变
    row = _row(pg_uri, "SELECT password_hash, auth_version, disabled "
                       "FROM users WHERE user_id=%s", (owner["user_id"],))
    assert row["auth_version"] == 2
    assert row["disabled"] is False
    assert row["password_hash"] != owner["password_hash"]
    # 新密码可登录、旧密码失效
    assert user_store_pg.verify_user("browser_admin", NEW_PW) is not None
    assert user_store_pg.verify_user("browser_admin", OLD_PW) is None

    # audit：同事务落库，actor NULL，detail 无敏感串
    ev = _last_audit(pg_uri)
    assert ev is not None
    assert ev["action"] == useradmin.AUDIT_ACTION
    assert ev["actor_user_id"] is None
    assert ev["target_type"] == "user" and ev["target_id"] == owner["user_id"]
    assert ev["detail"]["source"] == "local_cli"
    assert ev["detail"]["sessions_revoked"] is True
    assert "reenabled" not in ev["detail"]
    detail_text = json.dumps(ev["detail"], ensure_ascii=False)
    assert NEW_PW not in detail_text and "pbkdf2" not in detail_text

    # 输出：user_id/login_id/旧 session 失效；不含密码与 hash
    out, err = capsys.readouterr()
    assert owner["user_id"] in out
    assert "browser_admin" in out
    assert "旧 session 已失效" in out
    assert NEW_PW not in out and NEW_PW not in err
    assert "pbkdf2" not in out and "pbkdf2" not in err
    assert owner["password_hash"] not in out


def test_reset_login_id_case_and_trim_insensitive(pg_uri, capsys, monkeypatch):
    """--login-id 做两侧 trim + 大小写不敏感匹配（lower(login_id)=lower(trim(?))）。"""
    _bootstrap_owner("Browser_Admin")
    rc = _run_stdin(monkeypatch, [
        "reset-owner-password", "--login-id", "  browser_admin  ",
        "--password-stdin"], NEW_PW + "\n")
    assert rc == 0, capsys.readouterr().err
    assert user_store_pg.verify_user("browser_Admin", NEW_PW) is not None


def test_reset_with_password_file(pg_uri, capsys, tmp_path):
    _bootstrap_owner()
    f = tmp_path / "secret.txt"
    f.write_text(NEW_PW + "\n", encoding="utf-8")  # 尾部单个换行被去除
    rc = useradmin.main(["reset-owner-password", "--login-id", "browser_admin",
                         "--password-file", str(f)])
    assert rc == 0, capsys.readouterr().err
    assert user_store_pg.verify_user("browser_admin", NEW_PW) is not None


# --------------------------------------------------------------------------- #
# disabled owner 的 --enable 语义
# --------------------------------------------------------------------------- #
def test_disabled_owner_without_enable_refused(pg_uri, capsys, monkeypatch):
    owner = _bootstrap_owner()
    user_store_pg.set_user_disabled(owner["user_id"], True)  # 版本 1→2
    before = _row(pg_uri, "SELECT password_hash, auth_version, disabled "
                          "FROM users WHERE user_id=%s", (owner["user_id"],))
    rc = _run_stdin(monkeypatch, [
        "reset-owner-password", "--login-id", "browser_admin",
        "--password-stdin"], NEW_PW + "\n")
    assert rc != 0
    out, err = capsys.readouterr()
    assert "--enable" in err
    after = _row(pg_uri, "SELECT password_hash, auth_version, disabled "
                         "FROM users WHERE user_id=%s", (owner["user_id"],))
    assert after == before, "拒绝路径不得改动库（含不写 audit）"
    assert _last_audit(pg_uri) is None


def test_disabled_owner_with_enable_restores(pg_uri, capsys, monkeypatch):
    owner = _bootstrap_owner()
    user_store_pg.set_user_disabled(owner["user_id"], True)  # 版本 1→2
    rc = _run_stdin(monkeypatch, [
        "reset-owner-password", "--login-id", "browser_admin",
        "--password-stdin", "--enable"], NEW_PW + "\n")
    assert rc == 0, capsys.readouterr().err
    row = _row(pg_uri, "SELECT auth_version, disabled FROM users "
                       "WHERE user_id=%s", (owner["user_id"],))
    assert row["disabled"] is False      # 同事务解除禁用
    assert row["auth_version"] == 3      # 同事务递增（2→3）
    ev = _last_audit(pg_uri)
    assert ev is not None and ev["detail"].get("reenabled") is True
    assert user_store_pg.verify_user("browser_admin", NEW_PW) is not None
    out, _ = capsys.readouterr()
    assert "解除禁用" in out and "旧 session 已失效" in out


def test_enabled_owner_with_enable_refused(pg_uri, capsys, monkeypatch):
    owner = _bootstrap_owner()
    rc = _run_stdin(monkeypatch, [
        "reset-owner-password", "--login-id", "browser_admin",
        "--password-stdin", "--enable"], NEW_PW + "\n")
    assert rc != 0
    out, err = capsys.readouterr()
    assert "--enable" in err
    row = _row(pg_uri, "SELECT auth_version FROM users WHERE user_id=%s",
               (owner["user_id"],))
    assert row["auth_version"] == 1, "拒绝路径不得递增版本"
    assert _last_audit(pg_uri) is None


# --------------------------------------------------------------------------- #
# owner 行数不变量与定位拒绝
# --------------------------------------------------------------------------- #
def test_zero_owner_rows_refused_with_escape_hint(pg_uri, capsys, tmp_path):
    """0 个 owner 行：空表与「只有普通 user」都拒绝 + 逃生路径说明，不建号。"""
    f = tmp_path / "secret.txt"
    f.write_text(NEW_PW, encoding="utf-8")
    # 场景 1：完全空表
    rc = useradmin.main(["reset-owner-password", "--login-id", "admin",
                         "--password-file", str(f)])
    assert rc != 0
    out, err = capsys.readouterr()
    assert "owner" in err and ("逃生" in err or "bootstrap" in err)
    # 场景 2：只有普通 user（owner 行被 SQL 删除的锁死状态）
    user_store_pg.create_user("plain@x.com", "password1password1", role="user")
    rc = useradmin.main(["reset-owner-password", "--login-id", "plain@x.com",
                         "--password-file", str(f)])
    assert rc != 0
    out, err = capsys.readouterr()
    assert "逃生" in err
    # 绝不静默建号：仍只有那 1 行普通用户
    rows = _fetch(pg_uri, "SELECT count(*) AS n FROM users")
    assert rows[0]["n"] == 1
    assert _fetch(pg_uri, "SELECT 1 FROM users WHERE role='owner'") == []


def _insert_owner_row(pg_uri, user_id, login_id, disabled):
    """直插一条 owner 行（绕过应用层；hash 用占位值 'x'，不影响断言）。"""
    c = psycopg.connect(pg_uri)
    try:
        with c.cursor() as cur:
            cur.execute(
                "INSERT INTO users (user_id, login_id, display_name, "
                "password_hash, role, created_at, disabled) "
                "VALUES (%s,%s,'','x','owner',to_timestamp(1),%s)",
                (user_id, login_id, disabled))
        c.commit()
    finally:
        c.close()


def test_enabled_plus_disabled_history_owner_resets_enabled(
        pg_uri, capsys, monkeypatch):
    """P2：1 enabled + 1 历史 disabled owner（唯一可经 SQL 构造的正常形态）→
    以唯一 enabled owner 为恢复目标正常重置；disabled 历史行原样不动。"""
    owner = _bootstrap_owner()
    _insert_owner_row(pg_uri, "usr_o2", "o2@x.com", True)
    rc = _run_stdin(monkeypatch, [
        "reset-owner-password", "--login-id", "browser_admin",
        "--password-stdin"], NEW_PW + "\n")
    assert rc == 0, capsys.readouterr().err
    out, err = capsys.readouterr()
    assert owner["user_id"] in out
    # enabled owner：hash 换新 + 版本递增
    assert user_store_pg.verify_user("browser_admin", NEW_PW) is not None
    assert user_store_pg.verify_user("browser_admin", OLD_PW) is None
    row = _row(pg_uri, "SELECT auth_version FROM users WHERE user_id=%s",
               (owner["user_id"],))
    assert row["auth_version"] == 2
    # disabled 历史 owner 行原样保留（不参与定位、不被启用/改密）
    legacy = _row(pg_uri, "SELECT disabled, auth_version FROM users "
                          "WHERE user_id='usr_o2'")
    assert legacy["disabled"] is True and legacy["auth_version"] == 1
    aud = _last_audit(pg_uri)
    assert aud is not None and aud["target_id"] == owner["user_id"]


def test_disabled_history_login_id_refused_when_enabled_exists(
        pg_uri, capsys, monkeypatch):
    """--login-id 命中历史 disabled owner 行（库中另有 enabled owner）→
    拒绝并说明（重新启用会违反单 enabled owner 不变量），带不带 --enable 同拒。"""
    _bootstrap_owner()
    _insert_owner_row(pg_uri, "usr_o2", "legacy@x.com", True)
    for extra in ([], ["--enable"]):
        argv = ["reset-owner-password", "--login-id", "legacy@x.com",
                "--password-stdin"] + extra
        rc = _run_stdin(monkeypatch, argv, NEW_PW + "\n")
        assert rc != 0, "extra=%r 不应放行" % (extra,)
    out, err = capsys.readouterr()
    assert "历史 disabled owner" in err
    # 库未被动：enabled owner 旧密码仍可登录，无 audit
    assert user_store_pg.verify_user("browser_admin", OLD_PW) is not None
    assert _last_audit(pg_uri) is None


def test_all_disabled_multiple_owner_rows_refused(
        pg_uri, capsys, monkeypatch):
    """0 enabled + >1 disabled：无法唯一定位恢复目标 → 拒绝（含 --enable）。"""
    _insert_owner_row(pg_uri, "usr_a", "a@x.com", True)
    _insert_owner_row(pg_uri, "usr_b", "b@x.com", True)
    rc = _run_stdin(monkeypatch, [
        "reset-owner-password", "--login-id", "a@x.com",
        "--password-stdin", "--enable"], NEW_PW + "\n")
    assert rc != 0
    out, err = capsys.readouterr()
    assert "全部被禁用" in err or "无法唯一定位" in err
    assert _last_audit(pg_uri) is None


def test_multiple_enabled_owners_refused(pg_uri, capsys, monkeypatch):
    """>1 enabled owner（临时落下 0015 部分唯一索引构造的不变量破坏态）→
    拒绝执行，不选「第一条」；用后恢复索引。"""
    _bootstrap_owner()
    c = psycopg.connect(pg_uri)
    try:
        with c.cursor() as cur:
            cur.execute("DROP INDEX IF EXISTS users_single_enabled_owner_key")
        c.commit()
    finally:
        c.close()
    try:
        _insert_owner_row(pg_uri, "usr_o2", "second@x.com", False)
        rc = _run_stdin(monkeypatch, [
            "reset-owner-password", "--login-id", "browser_admin",
            "--password-stdin"], NEW_PW + "\n")
        assert rc != 0
        out, err = capsys.readouterr()
        assert "启用的 owner" in err
        assert user_store_pg.verify_user("browser_admin", OLD_PW) is not None
        assert _last_audit(pg_uri) is None
    finally:
        # 恢复 0015 索引（conftest 的 TRUNCATE 不会重建）：先清掉破坏态行
        c = psycopg.connect(pg_uri)
        try:
            with c.cursor() as cur:
                cur.execute("DELETE FROM users WHERE user_id='usr_o2'")
                cur.execute(
                    "CREATE UNIQUE INDEX IF NOT EXISTS "
                    "users_single_enabled_owner_key "
                    "ON users (role) WHERE role = 'owner' AND NOT disabled")
            c.commit()
        finally:
            c.close()


def test_wrong_login_id_refused(pg_uri, capsys, tmp_path):
    _bootstrap_owner()
    f = tmp_path / "secret.txt"
    f.write_text(NEW_PW, encoding="utf-8")
    rc = useradmin.main(["reset-owner-password", "--login-id", "someone_else",
                         "--password-file", str(f)])
    assert rc != 0
    out, err = capsys.readouterr()
    assert "someone_else" in err
    assert _last_audit(pg_uri) is None


# --------------------------------------------------------------------------- #
# 密码输入与策略
# --------------------------------------------------------------------------- #
def test_short_password_refused(pg_uri, capsys, monkeypatch):
    owner = _bootstrap_owner()
    rc = _run_stdin(monkeypatch, [
        "reset-owner-password", "--login-id", "browser_admin",
        "--password-stdin"], "short\n")
    assert rc != 0
    out, err = capsys.readouterr()
    assert "15" in err
    row = _row(pg_uri, "SELECT auth_version FROM users WHERE user_id=%s",
               (owner["user_id"],))
    assert row["auth_version"] == 1
    assert _last_audit(pg_uri) is None


def test_blank_password_refused(pg_uri, capsys, monkeypatch):
    """空 / 全空白（长度可能 ≥15）都拒绝。"""
    _bootstrap_owner()
    for blank in ("", " " * 20 + "\n"):
        rc = _run_stdin(monkeypatch, [
            "reset-owner-password", "--login-id", "browser_admin",
            "--password-stdin"], blank)
        assert rc != 0
        capsys.readouterr()


def test_password_file_missing(pg_uri, capsys):
    _bootstrap_owner()
    rc = useradmin.main(["reset-owner-password", "--login-id", "browser_admin",
                         "--password-file", "/nonexistent/secret.txt"])
    assert rc != 0
    out, err = capsys.readouterr()
    assert "无法读取" in err


def test_password_must_not_be_cli_value(pg_uri):
    """新密码没有命令行参数入口：--password 不存在 → argparse 用法错误（exit 2）。"""
    _bootstrap_owner()
    with pytest.raises(SystemExit) as ei:
        useradmin.main(["reset-owner-password", "--login-id", "browser_admin",
                        "--password", NEW_PW])
    assert ei.value.code == 2


# --------------------------------------------------------------------------- #
# 原子性与连接前置
# --------------------------------------------------------------------------- #
def test_audit_failure_rolls_back_password_update(pg_uri, capsys, monkeypatch):
    """audit 与密码更新同事务：audit 写失败 → UPDATE 一并回滚。"""
    owner = _bootstrap_owner()
    c = psycopg.connect(pg_uri)
    try:
        with c.cursor() as cur:
            cur.execute(
                "CREATE OR REPLACE FUNCTION _uaudit_boom() RETURNS trigger "
                "AS $$ BEGIN RAISE EXCEPTION 'audit down'; END $$ "
                "LANGUAGE plpgsql")
            cur.execute(
                "DROP TRIGGER IF EXISTS trg_uadmin_boom ON audit_events")
            cur.execute(
                "CREATE TRIGGER trg_uadmin_boom BEFORE INSERT ON audit_events "
                "FOR EACH ROW EXECUTE FUNCTION _uaudit_boom()")
        c.commit()
    finally:
        c.close()
    try:
        rc = _run_stdin(monkeypatch, [
            "reset-owner-password", "--login-id", "browser_admin",
            "--password-stdin"], NEW_PW + "\n")
        assert rc != 0
        row = _row(pg_uri, "SELECT password_hash, auth_version FROM users "
                           "WHERE user_id=%s", (owner["user_id"],))
        assert row["password_hash"] == owner["password_hash"], \
            "audit 失败必须连带回滚密码更新"
        assert row["auth_version"] == 1
        assert user_store_pg.verify_user("browser_admin", OLD_PW) is not None
    finally:
        c = psycopg.connect(pg_uri)
        try:
            with c.cursor() as cur:
                cur.execute("DROP TRIGGER IF EXISTS trg_uadmin_boom ON audit_events")
                cur.execute("DROP FUNCTION IF EXISTS _uaudit_boom()")
            c.commit()
        finally:
            c.close()


def test_missing_database_url_refused(capsys, monkeypatch, tmp_path):
    """未设 DATABASE_URL：清晰报错 + 非 0 退出（不依赖 STORAGE_BACKEND）。"""
    monkeypatch.delenv("DATABASE_URL", raising=False)
    for key in ("PGHOST", "PGPORT", "PGUSER", "PGPASSWORD", "PGDATABASE"):
        monkeypatch.delenv(key, raising=False)
    f = tmp_path / "secret.txt"
    f.write_text(NEW_PW, encoding="utf-8")
    rc = useradmin.main(["reset-owner-password", "--login-id", "admin",
                         "--password-file", str(f)])
    assert rc != 0
    out, err = capsys.readouterr()
    assert "DATABASE_URL" in err


def test_connection_failure_refused(capsys, monkeypatch, tmp_path):
    """DATABASE_URL 指向不可达实例：报连接失败 + 非 0 退出。"""
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@127.0.0.1:1/none")
    f = tmp_path / "secret.txt"
    f.write_text(NEW_PW, encoding="utf-8")
    rc = useradmin.main(["reset-owner-password", "--login-id", "admin",
                         "--password-file", str(f)])
    assert rc != 0
    out, err = capsys.readouterr()
    assert "连接失败" in err
