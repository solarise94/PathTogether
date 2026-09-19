# -*- coding: utf-8 -*-
"""R7 历史修复工具测试：scripts/repair_invite_activated_applications.py。

修复前的 activate_registered_user 不更新 test_applications，留下
「active + activation_source=invite + application 仍 pending」滞留行。应用层
修复只覆盖新激活；本文件验证显式修复工具的契约：

  - dry-run：只报告（目标行数、user_id、掩码账号、其他 active+pending 单独
    报告），不写任何数据、不写审计；
  - apply：单事务把目标行收口为 activated_by_invite（reviewed_by/reviewed_at
    保持 NULL），逐行记真实修复审计；不补发额度、不重发邮件；
  - 重复 apply 幂等：第二遍 0 行改动、不重复审计；
  - 其他 active+pending（来源非 invite）只报告不改写；
  - rejected/approved 历史决定与 pending 用户一律不动。

  - schema fail-closed（2026-09-19 修复：dry-run 曾因隐式 ensure_schema
    实际写库建表）：未迁移空库 dry-run 报错退出且库保持全空；部分迁移库
    （缺 0055）dry-run 绝不偷偷补迁移；全迁移库 dry-run 不动
    schema_migrations / audit_events；monkeypatch 探针断言两条路径都不
    调用 pg_store.ensure_schema。

场景全部用真实 store 链路构造（verify 建号 / 真实 review / 真实邀请激活），
唯一例外是「滞留行」本身——修复后正常激活已会即时收口，故用一条显式
UPDATE 把状态回拨为 pending 来模拟修复前的历史数据（仅测试装置，不掩饰
工具行为）。

运行：cd 项目根 && .venv/bin/python -m pytest tests/test_repair_invite_activated_applications.py -q
"""
import contextlib
import importlib.util
import io
import os
import sys
import uuid
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import _bootstrap  # noqa: E402,F401
SHARE_DATA_DIR = _bootstrap.SHARE_DATA_DIR
import psycopg  # noqa: E402
import pytest  # noqa: E402

import app as app_mod  # noqa: E402
import pg_store  # noqa: E402
import registration_mail_worker  # noqa: E402
import registration_store  # noqa: E402
import settings_store  # noqa: E402
import test_application_store  # noqa: E402
import user_store  # noqa: E402
from _pt_helpers import csrf_client, isolate_app  # noqa: E402

_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / \
    "repair_invite_activated_applications.py"
REPAIR_ACTION = "test_application.repair_invite_activated"


def _load_script():
    spec = importlib.util.spec_from_file_location(
        "repair_invite_activated_applications", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    """每用例隔离（与 test_test_application_api 同口径）。"""
    from _pt_helpers import isolate_app
    import _billing_helpers as bh
    isolate_app(monkeypatch, SHARE_DATA_DIR, clear_stores=True)
    monkeypatch.setattr(app_mod, "_registration_gate_warned", {"flag": False})
    bh.seed_spend_settings()
    for name in ("REGISTRATION_MAIL_SENDER", "REGISTRATION_AGENT_MAIL_CLI",
                 "REGISTRATION_AGENT_MAIL_FROM", "PUBLIC_BASE_URL",
                 "TEST_APPLICATION_ADMIN_EMAIL",
                 "FORMAT_REQUEST_ADMIN_EMAIL"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("REGISTRATION_MAIL_SENDER", "fake")
    monkeypatch.setenv("TEST_APPLICATION_ADMIN_EMAIL",
                       "repair-admin@x.com")
    monkeypatch.setattr(app_mod.registration_mail_worker, "drain_async",
                        lambda: None)
    fake = registration_mail_worker.install_fake_sender()
    fake.clear()
    yield
    fake.clear()


def _pg():
    conn = pg_store.connect()
    conn.row_factory = psycopg.rows.dict_row
    return conn


def _pending_user(email, direction="other", share=False):
    out = registration_store.enqueue_email_verification(
        email, base_url="https://repair.example.com")
    registration_store.verify_email_create_user(out["token"],
                                                "longpassword123")
    user = user_store.get_user_by_login_id(email.lower())
    assert user is not None
    test_application_store.submit(user["user_id"], direction, share)
    return user


def _owner():
    return user_store.create_user("repair-owner@x.com", "ownerpass12345678",
                                  role="owner")


def _rewind_to_pending(user_id):
    """测试装置：把已收口/已审批的申请回拨为 pending（模拟修复前滞留数据）。"""
    conn = _pg()
    try:
        with conn.cursor() as cur:
            cur.execute("UPDATE test_applications SET status='pending', "
                        "reviewed_at=NULL, reviewed_by=NULL "
                        "WHERE user_id=%s", (user_id,))
        conn.commit()
    finally:
        conn.close()


def _app_status(user_id):
    return test_application_store.get(user_id)["status"]


def _counts():
    """全局快照：各终态行数 + 额度行数 + 审计数 + 结果邮件数（核对不变量）。"""
    conn = _pg()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT status, count(*)::int AS n FROM "
                        "test_applications GROUP BY status")
            by_status = {r["status"]: r["n"] for r in cur.fetchall()}
            cur.execute("SELECT count(*)::int AS n FROM "
                        "ai_spend_total_allowances")
            allowances = cur.fetchone()["n"]
            cur.execute("SELECT count(*)::int AS n FROM audit_events "
                        "WHERE action=%s", (REPAIR_ACTION,))
            audits = cur.fetchone()["n"]
            cur.execute("SELECT count(*)::int AS n FROM registration_mail_jobs "
                        "WHERE purpose='test_decision'")
            mails = cur.fetchone()["n"]
    finally:
        conn.close()
    return {"by_status": by_status, "allowances": allowances,
            "audits": audits, "mails": mails}


def _build_scenarios():
    """四类行：
    A/B：active + invite + pending（修复目标，模拟修复前滞留）；
    C：active + admin + approved（正常审批，不动）；
    D：pending 用户 + rejected（拒绝历史，不动）；
    E：active + admin + pending（其他 active+pending：只报告不改写）。
    """
    owner = _owner()
    user_a = _pending_user("repair-a@x.com")
    user_b = _pending_user("repair-b@x.com")
    user_c = _pending_user("repair-c@x.com")
    user_d = _pending_user("repair-d@x.com")
    user_e = _pending_user("repair-e@x.com")
    inv_a = registration_store.create_invite(owner["user_id"])
    inv_b = registration_store.create_invite(owner["user_id"])
    registration_store.activate_registered_user(user_a["user_id"],
                                                inv_a["token"])
    registration_store.activate_registered_user(user_b["user_id"],
                                                inv_b["token"])
    # 修复后正常激活已即时收口 → 回拨模拟历史滞留
    _rewind_to_pending(user_a["user_id"])
    _rewind_to_pending(user_b["user_id"])
    client = csrf_client(app_mod.app.test_client())
    app_mod.app.config["TESTING"] = True
    app_mod.AUTH_ENABLED = True
    with client.session_transaction() as s:
        s.update({"auth_user": "o", "user_id": owner["user_id"],
                  "role": "owner",
                  "auth_version": owner.get("auth_version", 1)})
    r = client.post("/api/admin/v1/test-applications/%s/review"
                    % user_c["user_id"], json={"decision": "approved"})
    assert r.status_code == 200, r.get_data(as_text=True)
    r2 = client.post("/api/admin/v1/test-applications/%s/review"
                     % user_d["user_id"], json={"decision": "rejected"})
    assert r2.status_code == 200, r2.get_data(as_text=True)
    # E：审批通过（active+admin+approved）后回拨为 pending——「其他
    # active+pending」报告桶（来源非 invite，工具绝不改写）
    r3 = client.post("/api/admin/v1/test-applications/%s/review"
                     % user_e["user_id"], json={"decision": "approved"})
    assert r3.status_code == 200, r3.get_data(as_text=True)
    assert user_store.get_user(user_e["user_id"])[
        "activation_state"] == "active"
    _rewind_to_pending(user_e["user_id"])
    return {"owner": owner, "A": user_a, "B": user_b, "C": user_c,
            "D": user_d, "E": user_e}


def test_repair_dry_run_reports_without_writing():
    users = _build_scenarios()
    before = _counts()
    mod = _load_script()
    import io
    import contextlib
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = mod.main([])
    out = buf.getvalue()
    assert rc == 0
    assert "模式：dry-run" in out
    # 两个目标行 + E 单独报告
    assert "修复目标" in out and "2 行" in out
    assert users["A"]["user_id"] in out and users["B"]["user_id"] in out
    assert "仅报告" in out and users["E"]["user_id"] in out
    # 不写任何数据：快照逐项不变
    assert _counts() == before
    assert _app_status(users["A"]["user_id"]) == "pending"
    assert _app_status(users["B"]["user_id"]) == "pending"


def test_repair_apply_closes_targets_writes_audit_and_is_idempotent():
    users = _build_scenarios()
    before = _counts()
    mod = _load_script()
    import io
    import contextlib
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = mod.main(["--apply"])
    out = buf.getvalue()
    assert rc == 0
    assert "收口 2 行" in out
    after = _counts()
    # 只收口目标行；C approved / D rejected / E pending 原样
    assert _app_status(users["A"]["user_id"]) == "activated_by_invite"
    assert _app_status(users["B"]["user_id"]) == "activated_by_invite"
    assert _app_status(users["C"]["user_id"]) == "approved"
    assert _app_status(users["D"]["user_id"]) == "rejected"
    assert _app_status(users["E"]["user_id"]) == "pending"
    conn = _pg()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT reviewed_by, reviewed_at FROM "
                        "test_applications WHERE user_id=%s",
                        (users["A"]["user_id"],))
            row = cur.fetchone()
            assert row["reviewed_by"] is None and row["reviewed_at"] is None
    finally:
        conn.close()
    # 真实修复审计：恰好每个目标行一条，actor=NULL（系统修复）
    assert after["audits"] == before["audits"] + 2
    conn = _pg()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT actor_user_id, target_id, detail FROM "
                        "audit_events WHERE action=%s ORDER BY target_id",
                        (REPAIR_ACTION,))
            rows = cur.fetchall()
    finally:
        conn.close()
    assert {r["target_id"] for r in rows} == {
        users["A"]["user_id"], users["B"]["user_id"]}
    assert all(r["actor_user_id"] is None for r in rows)
    # 不补发额度、不重发邮件：快照不变
    assert after["allowances"] == before["allowances"]
    assert after["mails"] == before["mails"]
    # 重复 apply 幂等：0 行、审计不再增加
    buf2 = io.StringIO()
    with contextlib.redirect_stdout(buf2):
        rc2 = mod.main(["--apply"])
    assert rc2 == 0
    assert "收口 0 行" in buf2.getvalue()
    assert _counts() == after


def test_repair_never_touches_unrelated_states():
    """守卫断言：pending 用户/rejected 历史/正常审批行即使混在目标库中也
    不被 dry-run 或 apply 触碰（数量核对口径之外的语义兜底）。"""
    users = _build_scenarios()
    mod = _load_script()
    with contextlib.redirect_stdout(io.StringIO()):
        mod.main([])
        mod.main(["--apply"])
    assert user_store.get_user(users["D"]["user_id"])[
        "activation_state"] == "pending_activation"
    assert _app_status(users["D"]["user_id"]) == "rejected"
    assert _app_status(users["C"]["user_id"]) == "approved"
    assert user_store.get_user(users["E"]["user_id"])[
        "activation_state"] == "active"
    assert _app_status(users["E"]["user_id"]) == "pending"


# =========================================================================== #
# schema fail-closed（2026-09-19 修复）：dry-run 绝不隐式迁移 / 绝不写库
# =========================================================================== #
def _admin_conn(base_params):
    return psycopg.connect(
        psycopg.conninfo.make_conninfo(
            **{**base_params, "dbname": "postgres"}),
        autocommit=True)


@contextlib.contextmanager
def _isolated_db():
    """在 conftest 的同一嵌入式 PG 实例上开一个全新空库（复用 session
    pgserver；用后 FORCE DROP，不影响主测试库）。"""
    base = psycopg.conninfo.conninfo_to_dict(os.environ["DATABASE_URL"])
    name = "repair_iso_%s" % uuid.uuid4().hex[:10]
    admin = _admin_conn(base)
    try:
        admin.execute("CREATE DATABASE %s" % name)
    finally:
        admin.close()
    try:
        yield psycopg.conninfo.make_conninfo(**{**base, "dbname": name})
    finally:
        admin = _admin_conn(base)
        try:
            admin.execute("DROP DATABASE %s WITH (FORCE)" % name)
        except psycopg.Error:
            admin.rollback()
            admin.execute("DROP DATABASE IF EXISTS %s" % name)
        finally:
            admin.close()


def _run_script(mod, argv):
    """跑脚本并回收 (rc, stdout, stderr)。"""
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        rc = mod.main(argv)
    return rc, out.getvalue(), err.getvalue()


def _apply_migrations_except(uri, skip_filenames):
    """在指定库上按文件名序应用迁移（跳过 skip_filenames）——模拟「部分
    迁移」库。与 pg_store.ensure_schema 同口径，但受控跳过指定文件。"""
    conn = psycopg.connect(uri)
    try:
        with conn.cursor() as cur:
            cur.execute(pg_store._SCHEMA_MIGRATIONS_DDL)
        conn.commit()
        mdir = pg_store.migrations_dir()
        applied = []
        for fname in sorted(p.name for p in mdir.glob("*.sql")):
            if fname in skip_filenames:
                continue
            with conn.cursor() as cur:
                cur.execute((mdir / fname).read_text(encoding="utf-8"))
                cur.execute("INSERT INTO schema_migrations (filename) "
                            "VALUES (%s) ON CONFLICT (filename) DO NOTHING",
                            (fname,))
            conn.commit()
            applied.append(fname)
        return applied
    finally:
        conn.close()


def _schema_migrations(uri):
    """schema_migrations 全量内容（有序元组，供前后比对）。"""
    conn = psycopg.connect(uri)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT filename FROM schema_migrations "
                        "ORDER BY filename")
            return tuple(r[0] for r in cur.fetchall())
    finally:
        conn.close()


_SKIP_0055 = ("0055_test_application_invite_terminal.sql",)


def test_repair_dry_run_on_unmigrated_db_fails_closed_and_writes_nothing(
        monkeypatch):
    """未迁移的全新空库上 dry-run：明确报错退出（非零码、指向缺 schema /
    先迁移），且**不得创建任何表或迁移记录**（此前 _connect 无条件
    ensure_schema，dry-run 实际建表 + 写 schema_migrations——隐式写库）。"""
    with _isolated_db() as uri:
        monkeypatch.setenv("DATABASE_URL", uri)
        mod = _load_script()
        rc, out, err = _run_script(mod, [])
        assert rc != 0
        assert rc == 2
        # 中文错误信息明确指向缺 schema 与「先应用迁移」
        assert "schema" in err and "迁移" in err
        assert "test_applications" in err and "users" in err
        assert "audit_events" in err
        # 空库保持全空：没有任何表（含 schema_migrations）被隐式创建
        conn = psycopg.connect(uri)
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT count(*)::int FROM information_schema."
                            "tables WHERE table_schema = 'public'")
                assert cur.fetchone()[0] == 0
                cur.execute("SELECT to_regclass('schema_migrations')")
                assert cur.fetchone()[0] is None
        finally:
            conn.close()


def test_repair_dry_run_on_partially_migrated_db_never_applies_missing_migration(
        monkeypatch):
    """部分迁移库（缺 0055）上 dry-run：schema_migrations 内容前后完全一致
    ——绝不偷偷应用 0055；apply 则因缺 activated_by_invite 终态约束明确
    拒绝开工（fail-closed）。"""
    with _isolated_db() as uri:
        applied = _apply_migrations_except(uri, _SKIP_0055)
        assert _SKIP_0055[0] not in applied
        before = _schema_migrations(uri)
        monkeypatch.setenv("DATABASE_URL", uri)
        mod = _load_script()
        # dry-run：只读可跑（表/列齐备），报告 0 行目标
        rc, out, err = _run_script(mod, [])
        assert rc == 0
        assert "修复目标" in out and "0 行" in out
        assert _schema_migrations(uri) == before
        assert _SKIP_0055[0] not in _schema_migrations(uri)
        # apply：缺 0055 的 status CHECK → 显式报错退出，同样不补迁移
        rc2, out2, err2 = _run_script(mod, ["--apply"])
        assert rc2 != 0
        assert "activated_by_invite" in err2 and "迁移" in err2
        assert _schema_migrations(uri) == before


def test_repair_dry_run_on_fully_migrated_db_leaves_schema_and_audit_alone():
    """正常全迁移库上 dry-run：schema_migrations 与 audit_events 均不变
    （不新增审计、不重放/补写迁移记录）；目标行保持 pending。"""
    users = _build_scenarios()
    before_mig = _schema_migrations(os.environ["DATABASE_URL"])
    before = _counts()
    mod = _load_script()
    rc, out, _err = _run_script(mod, [])
    assert rc == 0
    assert "修复目标" in out and "2 行" in out
    assert _schema_migrations(os.environ["DATABASE_URL"]) == before_mig
    after = _counts()
    assert after == before            # audit_events 不新增（audits 含在快照内）
    assert _app_status(users["A"]["user_id"]) == "pending"


def test_repair_script_never_calls_ensure_schema(monkeypatch):
    """防回归探针：dry-run 与 --apply（含幂等重放）都不得调用
    pg_store.ensure_schema 或任何隐式迁移入口。"""
    users = _build_scenarios()

    def _probe(*_args, **_kwargs):
        raise AssertionError(
            "repair 脚本不得调用 pg_store.ensure_schema（隐式迁移）")

    monkeypatch.setattr(pg_store, "ensure_schema", _probe)
    mod = _load_script()
    with contextlib.redirect_stdout(io.StringIO()):
        assert mod.main([]) == 0            # dry-run
        assert mod.main(["--apply"]) == 0   # apply
        assert mod.main(["--apply"]) == 0   # 幂等重放
    assert _app_status(users["A"]["user_id"]) == "activated_by_invite"
    assert _app_status(users["B"]["user_id"]) == "activated_by_invite"
