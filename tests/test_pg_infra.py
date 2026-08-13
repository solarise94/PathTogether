# -*- coding: utf-8 -*-
"""Stage 3b-1 PostgreSQL 基建冒烟测试（pg_store + migrations/0001_init.sql）。

用 pgserver 在临时目录拉起**真实** PostgreSQL（本机无 postgres/docker 时的基建）。
缺 pgserver/psycopg 时整模块 skip（不 fail）。

覆盖：
  - ensure_schema 幂等：连跑多遍无异常，目标表存在；
  - users 插入/查询 + email 大小写不敏感唯一（citext 等价：lower(email) 唯一索引）；
  - rois 基本冒烟（含 ADMIN_TOKEN='admin' 伪 token，无 shares 行——印证 rois.token 不设
    外键的迁移期决策）；
  - 外键冒烟：slide_assets.slide_id → slides（含违规插入失败 + ON DELETE CASCADE）；
  - change_log.bigserial 单调自增。
"""
import pytest

# 缺基建依赖时整模块 skip（不 fail）
pgserver = pytest.importorskip("pgserver")
psycopg = pytest.importorskip("psycopg")
from psycopg.types.json import Jsonb  # noqa: E402

import pg_store  # noqa: E402

_REPO_ROOT = pg_store.migrations_dir().parent


# --------------------------------------------------------------------------- #
# fixtures
# --------------------------------------------------------------------------- #
@pytest.fixture(scope="session")
def pg_server(tmp_path_factory):
    """session 级：起一个真实 PG（数据目录在 pytest tmp 下）。"""
    data_dir = tmp_path_factory.mktemp("pgdata")
    srv = pgserver.get_server(str(data_dir))
    yield srv
    srv.cleanup()


@pytest.fixture(scope="session", autouse=True)
def _apply_schema_once(pg_server):
    """session 级：在所有用例前应用一次 schema，保证表已就绪。"""
    conn = psycopg.connect(pg_server.get_uri())
    try:
        pg_store.ensure_schema(conn)
        yield
    finally:
        conn.close()


@pytest.fixture
def conn(pg_server):
    """每用例新连接（autocommit=False），避免上一用例事务 abort 泄漏。"""
    c = psycopg.connect(pg_server.get_uri())
    yield c
    c.close()


# --------------------------------------------------------------------------- #
# 1. ensure_schema 幂等（_apply_schema_once 已跑过一次，这里再连跑两遍）
# --------------------------------------------------------------------------- #
def test_ensure_schema_idempotent(conn):
    # 再跑两遍不应报错
    files1 = pg_store.ensure_schema(conn)
    files2 = pg_store.ensure_schema(conn)
    assert files1 == files2
    assert "0001_init.sql" in files1
    # 目标表全部存在
    with conn.cursor() as cur:
        cur.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema='public'"
        )
        tables = {r[0] for r in cur.fetchall()}
    expected = {
        "schema_migrations", "users", "slides", "slide_assets",
        "projects", "project_slides", "shares", "grants", "rois",
        "change_log",
    }
    assert expected <= tables, "缺失表: %s" % (expected - tables)


def test_schema_migrations_recorded(conn):
    with conn.cursor() as cur:
        cur.execute("SELECT filename FROM schema_migrations ORDER BY filename")
        rows = [r[0] for r in cur.fetchall()]
    # 3b-1 只应有 0001；3b-2 追加 0002_roi_payload.sql（ROI 全量负载 + 插入序）；
    # 3c-1 追加 0003_comments.sql（评论线程）；3c-2 追加 0004_audit.sql（审计+归档）；
    # 4-1a 追加 0005_plugin.sql（插件安装凭证 + run grant）
    assert rows == [
        "0001_init.sql", "0002_roi_payload.sql", "0003_comments.sql",
        "0004_audit.sql", "0005_plugin.sql",
    ]


# --------------------------------------------------------------------------- #
# 2. users 插入/查询 + email 大小写不敏感唯一
# --------------------------------------------------------------------------- #
def test_users_insert_and_query(conn):
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO users (user_id, email, display_name, role) "
            "VALUES (%s,%s,%s,%s) RETURNING user_id, email, role",
            ("usr_smoke", "smoke@example.com", "Smoke", "user"),
        )
        uid, email, role = cur.fetchone()
        conn.commit()
    assert uid == "usr_smoke" and email == "smoke@example.com" and role == "user"

    with conn.cursor() as cur:
        cur.execute("SELECT email FROM users WHERE user_id=%s", ("usr_smoke",))
        assert cur.fetchone()[0] == "smoke@example.com"


def test_users_email_case_insensitive_unique(conn):
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO users (user_id, email, role) VALUES (%s,%s,%s)",
            ("usr_ci1", "Case@Example.com", "user"),
        )
        conn.commit()
    # 仅大小写不同的 email 应被 lower(email) 唯一索引拒绝
    with conn.cursor() as cur:
        with pytest.raises(psycopg.errors.UniqueViolation):
            cur.execute(
                "INSERT INTO users (user_id, email, role) VALUES (%s,%s,%s)",
                ("usr_ci2", "case@example.COM", "user"),
            )
            conn.commit()
        conn.rollback()


# --------------------------------------------------------------------------- #
# 3. rois 冒烟（ADMIN_TOKEN 伪 token 无 shares 行 → 印证 rois.token 无外键）
# --------------------------------------------------------------------------- #
def test_rois_admin_token_no_share_row(conn):
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO rois (id, token, slide, type, geom, size_mm, shared) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING id, token",
            ("roi_1", "admin", "demo.svs", "rect", Jsonb({"x": 1}), 6.0, True),
        )
        rid, token = cur.fetchone()
        conn.commit()
    assert rid == "roi_1" and token == "admin"


# --------------------------------------------------------------------------- #
# 4. 外键冒烟：slide_assets → slides（违规失败 + ON DELETE CASCADE）
# --------------------------------------------------------------------------- #
def test_slide_asset_fk_enforced(conn):
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO slides (slide_id, legacy_filename) VALUES (%s,%s)",
            ("sld_fk", "fk.svs"),
        )
        cur.execute(
            "INSERT INTO slide_assets (asset_id, slide_id, legacy_revision) "
            "VALUES (%s,%s,%s)",
            ("ast_ok", "sld_fk", "111:222"),
        )
        conn.commit()
    # 指向不存在的 slide → 外键违反
    with conn.cursor() as cur:
        with pytest.raises(psycopg.errors.ForeignKeyViolation):
            cur.execute(
                "INSERT INTO slide_assets (asset_id, slide_id) VALUES (%s,%s)",
                ("ast_bad", "sld_nope"),
            )
            conn.commit()
        conn.rollback()


def test_slide_asset_cascade_delete(conn):
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO slides (slide_id, legacy_filename) VALUES (%s,%s)",
            ("sld_casc", "casc.svs"),
        )
        cur.execute(
            "INSERT INTO slide_assets (asset_id, slide_id) VALUES (%s,%s)",
            ("ast_casc", "sld_casc"),
        )
        conn.commit()
        cur.execute("DELETE FROM slides WHERE slide_id=%s", ("sld_casc",))
        conn.commit()
        cur.execute("SELECT 1 FROM slide_assets WHERE asset_id=%s", ("ast_casc",))
        assert cur.fetchone() is None  # 随 slide 级联删除


# --------------------------------------------------------------------------- #
# 5. change_log bigserial 单调自增
# --------------------------------------------------------------------------- #
def test_change_log_bigserial(conn):
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO change_log (slide, token, op) VALUES (%s,%s,%s) RETURNING seq",
            ("seq.svs", "admin", "add"),
        )
        s1 = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO change_log (slide, token, op) VALUES (%s,%s,%s) RETURNING seq",
            ("seq.svs", "admin", "update"),
        )
        s2 = cur.fetchone()[0]
        conn.commit()
    assert isinstance(s1, int) and isinstance(s2, int)
    assert s2 > s1 > 0


# --------------------------------------------------------------------------- #
# 6. transaction() 上下文：commit/rollback 语义 + 不关闭连接（psycopg3 陷阱）
# --------------------------------------------------------------------------- #
def test_transaction_commits_and_keeps_connection_open(conn):
    assert conn.closed is False
    with pg_store.transaction(conn) as c:
        with c.cursor() as cur:
            cur.execute(
                "INSERT INTO users (user_id, email, role) VALUES (%s,%s,%s)",
                ("usr_tx_ok", "tx-ok@example.com", "user"),
            )
    # 正常退出 → commit；连接仍可用（不被关闭）
    assert conn.closed is False
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM users WHERE user_id='usr_tx_ok'")
        assert cur.fetchone()[0] == 1


def test_transaction_rolls_back_on_exception(conn):
    assert conn.closed is False
    with pytest.raises(RuntimeError):
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                cur.execute(
                    "INSERT INTO users (user_id, email, role) VALUES (%s,%s,%s)",
                    ("usr_tx_rollback", "tx-rb@example.com", "user"),
                )
            raise RuntimeError("boom")
    # 异常 → rollback；连接仍可用
    assert conn.closed is False
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM users WHERE user_id='usr_tx_rollback'")
        assert cur.fetchone()[0] == 0
