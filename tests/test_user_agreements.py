# -*- coding: utf-8 -*-
"""P0 协议与迁移底座测试（docs/agent-plan-20260921-registration-consent-research.md
§3 + §10 P0）。

覆盖：

  - 迁移 0060（agreement_consent_registry）：
      * 空库：conftest 内嵌 PG 从零应用全部迁移，0060 记录在案、四表存在、
        CHECK 约束生效；
      * 升级：模拟「已有 0001..0059 与业务数据、但无 0060 表」的存量库
        （DROP 0060 四表 + 删 schema_migrations 记录），重跑 ensure_schema
        重建并回填记录，存量 users 数据不受影响；
      * 重跑：0060 原始 SQL 直接执行两次幂等 no-op，ensure_schema 不重复记录；
  - 协议文档注册表（agreement_store）：内置文稿登记幂等、hash 不一致拒绝
    （同 version 不换内容）、发布状态机（draft→published→retired、同类型
    同语言至多一条 published、retired 不得复活）；
  - 必选协议接受凭据（research_consent_store.record_terms_acceptance）：
    条款缺失（无 published）拒绝、版本/hash 不匹配拒绝、追加不覆盖；
  - 研究授权 grant/withdraw：版本必填、版本/hash 校验、epoch CAS、不可变
    历史、幂等键重放、管理者不能代用户 grant、文稿下架后仍可撤回、
    撤回幂等；
  - 旧 test_applications 选项兼容层：旧 true **不自动 granted**、旧
    false/缺行保持拒绝、后台视图单独标「历史版本」；
  - 研究采集开关默认关闭：granted 用户 ingest_allowed 仍为 False；
  - /legal/* 公开只读页面：匿名可访问（AUTH_ENABLED=True 下不 302 登录）、
    enrollment 受限会话可访问、版本化不可变链接、下载附件字节与规范文件
    一致、Referrer-Policy、GET 零副作用（不建用户、不记录同意）。

运行：cd 项目根 && .venv/bin/python -m pytest tests/test_user_agreements.py -q
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import _bootstrap  # noqa: E402,F401  # session 目录 + openslide stub
SHARE_DATA_DIR = _bootstrap.SHARE_DATA_DIR

import psycopg  # noqa: E402
import pytest  # noqa: E402

import agreement_store  # noqa: E402
import app as app_mod  # noqa: E402
import pg_store  # noqa: E402
import research_consent_store  # noqa: E402
import user_store  # noqa: E402
from _pt_helpers import isolate_app  # noqa: E402

PASSWORD = "longpassword123"
MIGRATION_0060 = "0060_agreement_consent_registry.sql"
TABLES_0060 = ("agreement_documents", "user_agreement_acceptances",
               "user_research_consents", "user_research_consent_history")


def _pg():
    conn = pg_store.connect()
    conn.row_factory = psycopg.rows.dict_row
    return conn


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    """per-test 隔离 + 认证开启（白名单断言需要真实认证闸）+ 采集开关复位。"""
    isolate_app(monkeypatch, tmp_path / "share", clear_stores=True)
    app_mod.AUTH_ENABLED = True
    monkeypatch.delenv("RESEARCH_COLLECTION_ENABLED", raising=False)
    yield


def _create_user(login_id="p0user@x.com"):
    return user_store.create_user(login_id, PASSWORD)


def _doc(doc_type):
    for d in agreement_store.builtin_documents():
        if d["document_type"] == doc_type:
            return d
    raise AssertionError("missing builtin doc %s" % doc_type)


def _register_and_publish(*doc_types):
    agreement_store.ensure_builtin_documents()
    for dt in doc_types:
        d = _doc(dt)
        agreement_store.publish_document(dt, d["version"])


# --------------------------------------------------------------------------- #
# 1. 迁移 0060：空库应用（conftest ensure_schema 已在全新 PG 上跑过全部迁移）
# --------------------------------------------------------------------------- #
def test_migration_0060_fresh_database_applied():
    conn = _pg()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM schema_migrations WHERE filename=%s",
                        (MIGRATION_0060,))
            assert cur.fetchone() is not None, "0060 应已被 ensure_schema 应用"
            for table in TABLES_0060:
                cur.execute(
                    "SELECT 1 FROM pg_tables WHERE tablename=%s", (table,))
                assert cur.fetchone() is not None, "%s 表应存在" % table
            # 部分唯一索引：同类型同语言至多一条 published
            cur.execute(
                "SELECT 1 FROM pg_indexes "
                "WHERE indexname='agreement_documents_single_published'")
            assert cur.fetchone() is not None
    finally:
        conn.close()

    # CHECK 约束：坏 state / epoch<1 / 坏 hash 全部被拒
    user = _create_user("m60fresh@x.com")
    conn = _pg()
    try:
        for sql, params in (
            ("INSERT INTO user_research_consents (user_id, state, epoch) "
             "VALUES (%s,'bogus',1)", (user["user_id"],)),
            ("INSERT INTO user_research_consents (user_id, state, epoch) "
             "VALUES (%s,'granted',0)", (user["user_id"],)),
            ("INSERT INTO agreement_documents "
             "(document_type, version, title, content_sha256, content_path) "
             "VALUES ('user_agreement','vX','t','nothex','p')", ()),
        ):
            violated = False
            try:
                with conn.cursor() as cur:
                    cur.execute(sql, params)
                conn.commit()
            except psycopg.errors.CheckViolation:
                violated = True
                conn.rollback()
            assert violated, "约束应拒绝：%s" % sql
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# 2. 迁移 0060：存量升级路径（有 0001..0059 与业务数据、无 0060 表）
# --------------------------------------------------------------------------- #
def test_migration_0060_upgrade_path(pg_uri):
    user = _create_user("m60upgrade@x.com")
    conn = _pg()
    try:
        with conn.cursor() as cur:
            # 模拟升级前状态：删 0060 的记录并 DROP 其四张表（子表先删）
            cur.execute("DELETE FROM schema_migrations WHERE filename=%s",
                        (MIGRATION_0060,))
            cur.execute(
                "DROP TABLE user_research_consent_history, "
                "user_research_consents, user_agreement_acceptances, "
                "agreement_documents")
        conn.commit()
        with conn.cursor() as cur:
            for table in TABLES_0060:
                cur.execute(
                    "SELECT 1 FROM pg_tables WHERE tablename=%s", (table,))
                assert cur.fetchone() is None
    finally:
        conn.close()
    # 升级：重跑 ensure_schema → 只补 0060（ensure_schema 按位置取列，
    # 需要默认 tuple_row 连接）
    plain = pg_store.connect()
    try:
        applied = pg_store.ensure_schema(plain)
    finally:
        plain.close()
    assert MIGRATION_0060 in applied
    conn = _pg()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM schema_migrations WHERE filename=%s",
                        (MIGRATION_0060,))
            assert cur.fetchone() is not None, "升级后 0060 应重新登记"
            for table in TABLES_0060:
                cur.execute(
                    "SELECT 1 FROM pg_tables WHERE tablename=%s", (table,))
                assert cur.fetchone() is not None
            # 存量业务数据不受影响
            cur.execute("SELECT user_id FROM users WHERE user_id=%s",
                        (user["user_id"],))
            assert cur.fetchone() is not None, "升级不得影响存量 users 数据"
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# 3. 迁移 0060：重跑幂等（原始 SQL 直跑两次 + ensure_schema 不重复记录）
# --------------------------------------------------------------------------- #
def test_migration_0060_rerun_idempotent():
    sql = (pg_store.migrations_dir() / MIGRATION_0060).read_text(
        encoding="utf-8")
    conn = _pg()
    try:
        with conn.cursor() as cur:
            for _ in range(2):
                cur.execute(sql)  # 已应用过的 DDL 重跑不抛错
        conn.commit()
    finally:
        conn.close()
    # ensure_schema 需要默认 tuple_row 连接
    plain = pg_store.connect()
    try:
        pg_store.ensure_schema(plain)
    finally:
        plain.close()
    conn = _pg()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT count(*) AS n FROM schema_migrations WHERE filename=%s",
                (MIGRATION_0060,))
            assert cur.fetchone()["n"] == 1, "ensure_schema 不得重复记录"
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# 4. 文档注册表：登记幂等 + 同 version 内容不可变
# --------------------------------------------------------------------------- #
def test_ensure_builtin_documents_idempotent_and_immutable():
    agreement_store.ensure_builtin_documents()
    docs = agreement_store.list_documents()
    assert len(docs) == 3
    by_type = {d["document_type"]: d for d in docs}
    for doc_type in agreement_store.DOCUMENT_TYPES:
        row = by_type[doc_type]
        builtin = _doc(doc_type)
        assert row["version"] == agreement_store.BUILTIN_VERSION
        assert row["status"] == "draft", "P0 内置文稿按草稿登记"
        assert row["content_sha256"] == builtin["content_sha256"]
        assert row["content_path"] == builtin["content_path"]
        assert row["locale"] == "zh-CN"
    # 幂等：重复登记不产生新行
    agreement_store.ensure_builtin_documents()
    assert len(agreement_store.list_documents()) == 3
    # 不可变：人为篡改注册表 hash 后再次登记 → 拒绝（同 version 不换内容）
    conn = _pg()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE agreement_documents SET content_sha256=%s "
                "WHERE document_type='user_agreement'",
                ("0" * 64,))
        conn.commit()
    finally:
        conn.close()
    with pytest.raises(agreement_store.DocumentConflictError):
        agreement_store.ensure_builtin_documents()


# --------------------------------------------------------------------------- #
# 5. 发布状态机：draft→published→retired；当前发布唯一；retired 不复活
# --------------------------------------------------------------------------- #
def test_publish_transitions_and_single_published():
    agreement_store.ensure_builtin_documents()
    # 未发布前：无当前发布文稿
    assert agreement_store.current_published("user_agreement") is None
    d = _doc("user_agreement")
    published = agreement_store.publish_document("user_agreement", d["version"])
    assert published["status"] == "published"
    assert published["published_at"] is not None
    current = agreement_store.current_published("user_agreement")
    assert current["version"] == d["version"]
    # 重复发布同版本幂等
    again = agreement_store.publish_document("user_agreement", d["version"])
    assert again["status"] == "published"
    # 手工插入同类型同语言的第二个 published → 部分唯一索引拒绝
    conn = _pg()
    try:
        blocked = False
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO agreement_documents "
                    "(document_type, version, title, content_sha256, "
                    " content_path, status) "
                    "VALUES ('user_agreement','v-other','t',%s,'p','published')",
                    ("a" * 64,))
            conn.commit()
        except psycopg.errors.UniqueViolation:
            blocked = True
            conn.rollback()
        assert blocked, "同类型同语言至多一条 published"
        # 把当前 published retire 后不得复活
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE agreement_documents SET status='retired' "
                "WHERE document_type='user_agreement' AND version=%s",
                (d["version"],))
        conn.commit()
    finally:
        conn.close()
    with pytest.raises(agreement_store.AgreementStoreError):
        agreement_store.publish_document("user_agreement", d["version"])
    # 发布未登记版本 → document_not_found
    with pytest.raises(agreement_store.DocumentNotFoundError):
        agreement_store.publish_document("user_agreement", "2099-01-01-v9")


# --------------------------------------------------------------------------- #
# 6. 必选协议接受凭据：条款缺失/版本不匹配/hash 不匹配拒绝；追加不覆盖
# --------------------------------------------------------------------------- #
def test_terms_acceptance_validation_and_append_only():
    user = _create_user("accept@x.com")
    d = _doc("user_agreement")
    # 未发布（条款缺失）→ 拒绝
    agreement_store.ensure_builtin_documents()
    with pytest.raises(agreement_store.DocumentNotPublishedError):
        research_consent_store.record_terms_acceptance(
            user["user_id"], "user_agreement", d["version"],
            d["content_sha256"], source="register")
    agreement_store.publish_document("user_agreement", d["version"])
    # hash 不匹配 → 拒绝
    with pytest.raises(agreement_store.DocumentNotPublishedError):
        research_consent_store.record_terms_acceptance(
            user["user_id"], "user_agreement", d["version"],
            "f" * 64, source="register")
    # 版本不匹配 → 拒绝
    with pytest.raises(agreement_store.DocumentNotPublishedError):
        research_consent_store.record_terms_acceptance(
            user["user_id"], "user_agreement", "1999-01-01-v0",
            d["content_sha256"], source="register")
    # 非法来源 → 拒绝
    with pytest.raises(research_consent_store.ConsentError):
        research_consent_store.record_terms_acceptance(
            user["user_id"], "user_agreement", d["version"],
            d["content_sha256"], source="stealth")
    # 正常接受（register 与 account_reaccept 各一次）→ 追加两条，不覆盖
    r1 = research_consent_store.record_terms_acceptance(
        user["user_id"], "user_agreement", d["version"],
        d["content_sha256"], source="register")
    r2 = research_consent_store.record_terms_acceptance(
        user["user_id"], "user_agreement", d["version"],
        d["content_sha256"], source="account_reaccept")
    assert r1["acceptance_id"] != r2["acceptance_id"]
    rows = research_consent_store.list_acceptances(user["user_id"])
    assert len(rows) == 2
    assert {r["source"] for r in rows} == {"register", "account_reaccept"}
    assert all(r["version"] == d["version"] for r in rows)


# --------------------------------------------------------------------------- #
# 7. grant：版本必填 + 当前 published 版本/hash 校验
# --------------------------------------------------------------------------- #
def test_grant_requires_published_and_matching_document():
    user = _create_user("grant@x.com")
    d = _doc("research_sharing")
    agreement_store.ensure_builtin_documents()
    # 无 published → 拒绝（缺当前发布文稿不能授权）
    with pytest.raises(agreement_store.DocumentNotPublishedError):
        research_consent_store.grant(
            user["user_id"], document_version=d["version"],
            document_sha256=d["content_sha256"])
    agreement_store.publish_document("research_sharing", d["version"])
    # 版本必填
    with pytest.raises(research_consent_store.DocumentVersionRequiredError):
        research_consent_store.grant(user["user_id"])
    # 版本不匹配 → 拒绝
    with pytest.raises(agreement_store.DocumentNotPublishedError):
        research_consent_store.grant(
            user["user_id"], document_version="1999-01-01-v0",
            document_sha256=d["content_sha256"])
    # hash 不匹配 → 拒绝
    with pytest.raises(agreement_store.DocumentNotPublishedError):
        research_consent_store.grant(
            user["user_id"], document_version=d["version"],
            document_sha256="0" * 64)
    # 版本+hash 匹配 → 成功
    result = research_consent_store.grant(
        user["user_id"], document_version=d["version"],
        document_sha256=d["content_sha256"])
    assert result["changed"] is True
    assert result["consent"]["state"] == "granted"
    assert result["consent"]["epoch"] == 1


# --------------------------------------------------------------------------- #
# 8. grant/withdraw：epoch CAS + 不可变历史 + 撤回幂等 + 再授权新 epoch
# --------------------------------------------------------------------------- #
def test_grant_withdraw_epoch_cas_and_history():
    user = _create_user("cas@x.com")
    d = _doc("research_sharing")
    _register_and_publish("research_sharing")

    r = research_consent_store.grant(
        user["user_id"], d["version"], d["content_sha256"], expected_epoch=0)
    assert r["consent"]["epoch"] == 1 and r["consent"]["state"] == "granted"
    # 无行时 expected_epoch 非 0 → CAS 失败已由上面的 expected_epoch=0 成功
    # 路径覆盖；这里直接验证行内 stale epoch 冲突
    with pytest.raises(research_consent_store.EpochConflictError):
        research_consent_store.grant(
            user["user_id"], d["version"], d["content_sha256"],
            expected_epoch=99)
    # 撤回：正确 epoch → epoch+1
    w = research_consent_store.withdraw(user["user_id"], expected_epoch=1)
    assert w["changed"] is True
    assert w["consent"]["state"] == "withdrawn"
    assert w["consent"]["epoch"] == 2
    assert w["consent"]["withdrawn_at"] is not None
    # 重复撤回幂等
    w2 = research_consent_store.withdraw(user["user_id"])
    assert w2["changed"] is False
    # 撤回期间的 stale epoch 仍冲突
    with pytest.raises(research_consent_store.EpochConflictError):
        research_consent_store.withdraw(user["user_id"], expected_epoch=1)
    # 再同意：新 epoch，不复活旧语义
    r2 = research_consent_store.grant(
        user["user_id"], d["version"], d["content_sha256"], expected_epoch=2)
    assert r2["consent"]["epoch"] == 3
    assert r2["consent"]["state"] == "granted"
    # 不可变历史：grant→withdraw→grant 三条，含前后 state 与文档 hash
    history = research_consent_store.list_history(user["user_id"])
    assert len(history) == 3
    assert [(h["from_state"], h["to_state"], h["epoch"]) for h in history] == [
        ("withdrawn", "granted", 3), ("granted", "withdrawn", 2),
        (None, "granted", 1)]
    assert all(h["document_sha256"] == d["content_sha256"] for h in history)
    assert all(h["actor_user_id"] == user["user_id"] for h in history)


def test_new_user_grant_with_nonzero_expected_epoch_rejected():
    user = _create_user("freshcas@x.com")
    d = _doc("research_sharing")
    _register_and_publish("research_sharing")
    with pytest.raises(research_consent_store.EpochConflictError):
        research_consent_store.grant(
            user["user_id"], d["version"], d["content_sha256"],
            expected_epoch=5)


# --------------------------------------------------------------------------- #
# 9. 操作者必须为本人（管理者不能代用户 grant/withdraw）
# --------------------------------------------------------------------------- #
def test_actor_must_be_self():
    user = _create_user("self@x.com")
    other = _create_user("other@x.com")
    d = _doc("research_sharing")
    _register_and_publish("research_sharing")
    with pytest.raises(research_consent_store.ActorForbiddenError):
        research_consent_store.grant(
            user["user_id"], d["version"], d["content_sha256"],
            actor_user_id=other["user_id"])
    with pytest.raises(research_consent_store.ActorForbiddenError):
        research_consent_store.withdraw(
            user["user_id"], actor_user_id=other["user_id"])
    assert research_consent_store.get_consent(user["user_id"]) is None


# --------------------------------------------------------------------------- #
# 10. 请求幂等键：重放不重复写历史
# --------------------------------------------------------------------------- #
def test_idempotency_key_replay():
    user = _create_user("idem@x.com")
    d = _doc("research_sharing")
    _register_and_publish("research_sharing")
    key = research_consent_store.new_idempotency_key()
    r1 = research_consent_store.grant(
        user["user_id"], d["version"], d["content_sha256"],
        idempotency_key=key)
    r2 = research_consent_store.grant(
        user["user_id"], d["version"], d["content_sha256"],
        idempotency_key=key)
    assert r1["changed"] is True and r2["replayed"] is True
    assert r2["consent"]["epoch"] == 1, "重放不得推进 epoch"
    assert len(research_consent_store.list_history(user["user_id"])) == 1
    wkey = research_consent_store.new_idempotency_key()
    research_consent_store.withdraw(user["user_id"], idempotency_key=wkey)
    w2 = research_consent_store.withdraw(user["user_id"], idempotency_key=wkey)
    assert w2["replayed"] is True
    assert len(research_consent_store.list_history(user["user_id"])) == 2


# --------------------------------------------------------------------------- #
# 11. 无行撤回幂等成功；文稿下架后仍可撤回
# --------------------------------------------------------------------------- #
def test_withdraw_without_row_and_after_document_retired():
    user = _create_user("wd@x.com")
    # 无任何 consent 行：撤回是幂等成功（未授权语义不变）
    result = research_consent_store.withdraw(user["user_id"])
    assert result["changed"] is False and result["consent"] is None

    d = _doc("research_sharing")
    _register_and_publish("research_sharing")
    research_consent_store.grant(
        user["user_id"], d["version"], d["content_sha256"])
    # 文稿下架（retire）后撤回仍必须可用
    conn = _pg()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE agreement_documents SET status='retired' "
                "WHERE document_type='research_sharing' AND version=%s",
                (d["version"],))
        conn.commit()
    finally:
        conn.close()
    w = research_consent_store.withdraw(user["user_id"])
    assert w["changed"] is True and w["consent"]["state"] == "withdrawn"


# --------------------------------------------------------------------------- #
# 12. 旧 test_applications 选项兼容层：旧 true 不自动 granted
# --------------------------------------------------------------------------- #
def _insert_legacy_application(user_id, share):
    conn = _pg()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO test_applications "
                "(user_id, research_direction, share_research_data, "
                " consent_version) VALUES (%s,'other',%s,%s)",
                (user_id, share, "research-data-20260916-v1"))
        conn.commit()
    finally:
        conn.close()


def test_legacy_true_not_auto_granted():
    user = _create_user("legacy-true@x.com")
    _insert_legacy_application(user["user_id"], True)
    # 旧 true 只是历史证明：当前未授权
    assert research_consent_store.is_granted(user["user_id"]) is False
    assert research_consent_store.ingest_allowed(user["user_id"]) is False
    view = research_consent_store.research_authorization_view(user["user_id"])
    assert view["granted"] is False
    assert view["state"] is None
    legacy = view["legacy_test_application"]
    assert legacy is not None
    assert legacy["historical_only"] is True
    assert legacy["share_research_data"] is True
    assert legacy["historical_consent_version"] == "research-data-20260916-v1"


def test_legacy_false_and_missing_row_stay_refused():
    user_false = _create_user("legacy-false@x.com")
    _insert_legacy_application(user_false["user_id"], False)
    user_none = _create_user("legacy-none@x.com")
    for u in (user_false, user_none):
        assert research_consent_store.is_granted(u["user_id"]) is False
        assert research_consent_store.ingest_allowed(u["user_id"]) is False
    view_false = research_consent_store.research_authorization_view(
        user_false["user_id"])
    assert view_false["legacy_test_application"]["share_research_data"] is False
    view_none = research_consent_store.research_authorization_view(
        user_none["user_id"])
    assert view_none["legacy_test_application"] is None
    assert view_none["granted"] is False


def test_legacy_true_still_not_granted_after_collection_switch_on(monkeypatch):
    """即使采集开关被人为打开，旧 true 也不能成为授权（唯一权威是新表）。"""
    user = _create_user("legacy-sw@x.com")
    _insert_legacy_application(user["user_id"], True)
    monkeypatch.setenv("RESEARCH_COLLECTION_ENABLED", "1")
    assert research_consent_store.collection_enabled() is True
    assert research_consent_store.is_granted(user["user_id"]) is False
    assert research_consent_store.ingest_allowed(user["user_id"]) is False


# --------------------------------------------------------------------------- #
# 13. 研究采集开关默认关闭：granted 也不允许采集
# --------------------------------------------------------------------------- #
def test_collection_switch_default_off_blocks_ingest():
    user = _create_user("switch@x.com")
    d = _doc("research_sharing")
    _register_and_publish("research_sharing")
    research_consent_store.grant(
        user["user_id"], d["version"], d["content_sha256"])
    assert research_consent_store.collection_enabled() is False, \
        "研究采集开关必须默认关闭"
    assert research_consent_store.is_granted(user["user_id"]) is True
    assert research_consent_store.ingest_allowed(user["user_id"]) is False
    view = research_consent_store.research_authorization_view(user["user_id"])
    assert view["collection_enabled"] is False
    assert view["ingest_allowed"] is False
    assert view["granted"] is True


# --------------------------------------------------------------------------- #
# 14. /legal/* 公开只读页面：匿名/enrollment 可访问、版本化链接、下载、
#     Referrer-Policy、零副作用
# --------------------------------------------------------------------------- #
def _table_counts():
    conn = _pg()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT (SELECT count(*) FROM users) AS users, "
                "(SELECT count(*) FROM user_agreement_acceptances) AS acc, "
                "(SELECT count(*) FROM user_research_consents) AS consents, "
                "(SELECT count(*) FROM user_research_consent_history) AS hist")
            return dict(cur.fetchone())
    finally:
        conn.close()


def test_legal_pages_anonymous_accessible_without_login_redirect():
    client = app_mod.app.test_client()
    for slug, marker in (
            ("user-agreement", "用户协议与数据处理说明"),
            ("research-sharing", "数据共享与软件改进协议"),
            ("model-providers", "模型服务商披露")):
        resp = client.get("/legal/%s" % slug)
        assert resp.status_code == 200, "/legal/%s 应匿名可读" % slug
        body = resp.get_data(as_text=True)
        assert marker in body
        assert agreement_store.BUILTIN_VERSION in body
        assert "草稿" in body, "P0 页面须明确标注草稿状态"
        assert resp.headers.get("Referrer-Policy") == "no-referrer"
    # 白名单没有扩大：未登录访问工作台仍跳登录
    resp = client.get("/app")
    assert resp.status_code == 302 and resp.headers["Location"].startswith(
        "/login"), "白名单不得放宽其他受保护页面"
    # 未知 slug / 未知版本 → 404（不重定向登录）
    assert client.get("/legal/nonexistent").status_code == 404
    assert client.get(
        "/legal/user-agreement/1999-01-01-v0").status_code == 404


def test_legal_pages_content_and_versioned_links():
    client = app_mod.app.test_client()
    d = _doc("user_agreement")
    # 当前版本页与版本化链接内容一致
    current = client.get("/legal/user-agreement")
    versioned = client.get("/legal/user-agreement/%s" % d["version"])
    assert current.status_code == versioned.status_code == 200
    assert d["content_sha256"] in current.get_data(as_text=True)
    assert "immutable" in versioned.headers.get("Cache-Control", "")
    assert "no-cache" in current.headers.get("Cache-Control", "")
    # 页面正文与文稿一致（抽查各节标题）
    body = versioned.get_data(as_text=True)
    for heading in ("一、适用范围", "五、AI 读片与模型服务商", "八、协议变更和联系"):
        assert heading in body
    # 研究共享协议正文抽查
    rs = client.get("/legal/research-sharing").get_data(as_text=True)
    assert "自愿参与与研究目的" in rs
    assert "撤回" in rs
    # 模型服务商披露：列出真实服务商，且不臆造未核实的保存/训练承诺
    mp = client.get("/legal/model-providers").get_data(as_text=True)
    assert "DeepSeek" in mp and "api.deepseek.com" in mp
    assert "未独立核实" in mp


def test_legal_pages_download_matches_canonical_bytes():
    client = app_mod.app.test_client()
    for slug in ("user-agreement", "research-sharing", "model-providers"):
        doc = agreement_store.get_builtin_document(slug)
        resp = client.get("/legal/%s/%s?download=1" % (slug, doc["version"]))
        assert resp.status_code == 200
        assert "attachment" in resp.headers.get("Content-Disposition", "")
        assert resp.get_data() == doc["content"].encode("utf-8"), \
            "下载内容必须与规范文件字节一致"
        # 不带 download 参数的当前页也可下载
        resp2 = client.get("/legal/%s?download=1" % slug)
        assert resp2.get_data() == doc["content"].encode("utf-8")


def test_legal_pages_get_has_no_side_effects():
    before = _table_counts()
    client = app_mod.app.test_client()
    for slug in ("user-agreement", "research-sharing", "model-providers"):
        assert client.get("/legal/%s" % slug).status_code == 200
        doc = agreement_store.get_builtin_document(slug)
        assert client.get(
            "/legal/%s/%s" % (slug, doc["version"])).status_code == 200
    after = _table_counts()
    assert after == before, "GET 协议页不得创建用户、记录同意或授权历史"


def test_legal_pages_accessible_for_enrollment_session():
    """pending_activation 用户（enrollment 受限会话）也能阅读协议。"""
    user = _create_user("pending@x.com")
    conn = _pg()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE users SET activation_state='pending_activation' "
                "WHERE user_id=%s", (user["user_id"],))
        conn.commit()
    finally:
        conn.close()
    client = app_mod.app.test_client()
    with client.session_transaction() as sess:
        sess[app_mod.ENROLLMENT_SESSION_KEY] = {
            "user_id": user["user_id"],
            "email": "pending@x.com",
            "purpose": "activation",
            "issued_at": time.time(),
            "auth_version": user.get("auth_version"),
        }
    resp = client.get("/legal/user-agreement")
    assert resp.status_code == 200, "enrollment 会话应能阅读协议"
    # 但 enrollment 会话仍不能触达业务面（对照）
    assert client.get("/app").status_code in (302, 401, 403)
