# -*- coding: utf-8 -*-
"""迁移 0067（slide_asset_identity）验证 —— slide ID 化重构 P1-A。

conftest 起库（pgserver + ensure_schema）时 0067 已应用；本模块验证：
  - schema_migrations 已登记 0067；
  - slides 八列扩展（默认值/CHECK/部分唯一 storage_relpath）；
  - 任务表 slide_id（upload_tasks FK / 部分唯一索引）、upload_task_items
    库层唯一约束；
  - share_slides / slide_view_grants / project_slides 的 ID 化约束；
  - 关系表 slide_id 列与索引、slide_delete_jobs；
  - 迁移幂等：对已应用库直接再执行 0067 文件两遍不报错（pg_store 机制
    要求：单文件单事务、IF NOT EXISTS / DO 判存自保）。

运行：.venv/bin/python -m pytest tests/test_migration_0067.py -q
"""
import psycopg
import pytest

import pg_store

_MIGRATION_FILE = "0067_slide_asset_identity.sql"

_SLIDE_COLUMNS = {
    "original_filename", "storage_layout", "storage_relpath", "format_ext",
    "asset_state", "published_at", "deleted_at", "accounted_bytes",
}

_RELATION_COLUMNS = {
    "rois": {"slide_id"},
    "comments": {"slide_id"},
    "change_log": {"slide_id"},
    "run_grants": {"slide_id"},
    "ai_session_principals": {"slide_id"},
    "annotation_access_events": {"slide_id"},
    "audit_events": {"slide_id"},
    "conversion_job_sources": {"source_slide_id"},
}

# 注：uq_baidu_import_items_slide_id 由 0069 DROP（P4-app 裁决：convert
# 幂等复用允许同 owner 多条目共享同一产物资产；native 一 item 一资产由
# 分配侧保证）——本清单反映全量迁移后的当前 schema。
_EXPECTED_INDEXES = {
    "uq_slides_storage_relpath",
    "idx_slides_owner_user_id",
    "idx_slides_asset_state",
    "uq_ingestion_jobs_slide_id",
    "uq_conversion_jobs_slide_id",
    "uq_slide_view_grants_slide_id_user",
    "uq_project_slides_project_slide_id",
    "idx_rois_slide_id_seq",
    "idx_comments_slide_id",
    "idx_change_log_slide_id_seq",
    "idx_run_grants_slide_id",
    "idx_ai_session_principals_slide_id",
    "idx_annotation_access_events_slide_id_seq",
    "idx_audit_slide_id",
    "idx_share_slides_slide",
}


@pytest.fixture
def conn(pg_uri):
    c = psycopg.connect(pg_uri)
    c.row_factory = psycopg.rows.dict_row
    yield c
    c.close()


@pytest.fixture(autouse=True)
def _clean_unmanaged_tables(conn):
    """conftest TRUNCATE 清单外、无 FK 随 slides CASCADE 的新表（0067）。"""
    with conn.cursor() as cur:
        cur.execute("DELETE FROM upload_task_items")
        cur.execute("DELETE FROM slide_delete_jobs")
        cur.execute("DELETE FROM share_slides")
    conn.commit()
    yield


def _table_columns(cur, table):
    cur.execute(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_name=%s", (table,))
    return {r["column_name"] for r in cur.fetchall()}


def _index_names(cur):
    cur.execute("SELECT indexname FROM pg_indexes")
    return {r["indexname"] for r in cur.fetchall()}


# --------------------------------------------------------------------------- #
# 应用与幂等
# --------------------------------------------------------------------------- #
def test_migration_recorded_and_idempotent(conn):
    sql = (pg_store.migrations_dir() / _MIGRATION_FILE).read_text("utf-8")
    with conn.cursor() as cur:
        cur.execute("SELECT 1 FROM schema_migrations WHERE filename=%s",
                    (_MIGRATION_FILE,))
        assert cur.fetchone() is not None       # conftest ensure_schema 已应用
    # 已应用库直接重放两遍：IF NOT EXISTS / DO 判存自保，无异常
    for _ in range(2):
        conn.execute(sql)
        conn.commit()
    # ensure_schema 扫描亦幂等（按记录去重，不重放）；
    # 用默认 tuple-row 连接（pg_store 内部按 row[0] 取列）
    plain = pg_store.connect()
    try:
        assert _MIGRATION_FILE in pg_store.ensure_schema(plain)
    finally:
        plain.close()


# --------------------------------------------------------------------------- #
# slides 扩展
# --------------------------------------------------------------------------- #
def test_slides_columns_defaults_and_defaults(conn):
    assert _SLIDE_COLUMNS <= _table_columns(conn.cursor(), "slides")
    with conn.cursor() as cur:
        # 旧行默认进入 legacy/legacy（不动既有行为；回填脚本才迁移状态）
        cur.execute("INSERT INTO slides (slide_id, legacy_filename) "
                    "VALUES ('sld_m_old', 'old.svs')")
        cur.execute("SELECT storage_layout, asset_state, original_filename, "
                    "format_ext, storage_relpath, accounted_bytes, "
                    "published_at, deleted_at FROM slides "
                    "WHERE slide_id='sld_m_old'")
        row = cur.fetchone()
        assert row["storage_layout"] == "legacy"
        assert row["asset_state"] == "legacy"
        assert row["original_filename"] is None
        assert row["storage_relpath"] is None
        assert row["accounted_bytes"] is None
        assert row["published_at"] is None and row["deleted_at"] is None
        conn.commit()


def test_slides_check_constraints(conn):
    with conn.cursor() as cur:
        cur.execute("INSERT INTO slides (slide_id) VALUES ('sld_c1')")
        conn.commit()
        for col, bad in (("asset_state", "bogus"),
                         ("storage_layout", "other"),
                         ("accounted_bytes", -1)):
            with pytest.raises(psycopg.errors.CheckViolation):
                cur.execute("UPDATE slides SET %s=%%s WHERE slide_id='sld_c1'"
                            % col, (bad,))
                conn.commit()
            conn.rollback()


def test_slides_storage_relpath_partial_unique(conn):
    with conn.cursor() as cur:
        cur.execute("INSERT INTO slides (slide_id, storage_relpath) "
                    "VALUES ('sld_u1', 'objects/sld_u1/data.svs')")
        conn.commit()
        with pytest.raises(psycopg.errors.UniqueViolation):
            cur.execute("INSERT INTO slides (slide_id, storage_relpath) "
                        "VALUES ('sld_u2', 'objects/sld_u1/data.svs')")
            conn.commit()
        conn.rollback()
        # NULL 不占用唯一键（legacy/staging 未派生路径可多行并存）
        for sid in ("sld_u3", "sld_u4"):
            cur.execute("INSERT INTO slides (slide_id, storage_relpath) "
                        "VALUES (%s, NULL)", (sid,))
        conn.commit()


# --------------------------------------------------------------------------- #
# 索引 / 关系列 / 新表
# --------------------------------------------------------------------------- #
def test_indexes_and_relation_columns_exist(conn):
    assert _EXPECTED_INDEXES <= _index_names(conn.cursor())
    for table, cols in _RELATION_COLUMNS.items():
        assert cols <= _table_columns(conn.cursor(), table), table
    assert {"task_id", "item_key", "slide_id"} <= \
        _table_columns(conn.cursor(), "upload_task_items")
    assert {"job_id", "slide_id", "state", "attempts"} <= \
        _table_columns(conn.cursor(), "slide_delete_jobs")
    assert {"token", "slide_id", "position"} <= \
        _table_columns(conn.cursor(), "share_slides")
    assert {"slide_id"} <= _table_columns(conn.cursor(), "upload_tasks")
    assert {"slide_id"} <= _table_columns(conn.cursor(), "project_slides")


def test_upload_tasks_slide_id_fk(conn):
    with conn.cursor() as cur:
        with pytest.raises(psycopg.errors.ForeignKeyViolation):
            cur.execute(
                "INSERT INTO upload_tasks (upload_id, filename, safe_name, "
                "declared_size, chunk_size, expires_at, slide_id) "
                "VALUES ('upt_m1','a.svs','a.svs',10,5,"
                "now() + interval '1 hour','sld_missing')")
            conn.commit()
        conn.rollback()
        # 指向真实 slides 行 + NULL 两种形态都合法
        cur.execute("INSERT INTO slides (slide_id) VALUES ('sld_upt')")
        cur.execute(
            "INSERT INTO upload_tasks (upload_id, filename, safe_name, "
            "declared_size, chunk_size, expires_at, slide_id) "
            "VALUES ('upt_m2','a.svs','a.svs',10,5,"
            "now() + interval '1 hour','sld_upt')")
        cur.execute(
            "INSERT INTO upload_tasks (upload_id, filename, safe_name, "
            "declared_size, chunk_size, expires_at) "
            "VALUES ('upt_m3','b.svs','b.svs',10,5,"
            "now() + interval '1 hour')")
        conn.commit()


def test_upload_task_items_unique_constraints(conn):
    with conn.cursor() as cur:
        cur.execute("INSERT INTO upload_task_items (task_id, item_key, slide_id) "
                    "VALUES ('upt_z1', 'k1', 'sld_i1')")
        conn.commit()
        # 同 (task,item) 重绑新 ID：PK 冲突（幂等重试必须复用原绑定）
        with pytest.raises(psycopg.errors.UniqueViolation):
            cur.execute("INSERT INTO upload_task_items (task_id, item_key, "
                        "slide_id) VALUES ('upt_z1', 'k1', 'sld_i2')")
            conn.commit()
        conn.rollback()
        # slide_id 全局 UNIQUE：一个资产只属一个任务项
        with pytest.raises(psycopg.errors.UniqueViolation):
            cur.execute("INSERT INTO upload_task_items (task_id, item_key, "
                        "slide_id) VALUES ('upt_z2', 'k9', 'sld_i1')")
            conn.commit()
        conn.rollback()
        cur.execute("INSERT INTO upload_task_items (task_id, item_key, slide_id) "
                    "VALUES ('upt_z2', 'k1', 'sld_i2')")
        conn.commit()


def test_task_tables_partial_unique_slide_id(conn):
    with conn.cursor() as cur:
        # ingestion_jobs：NULL 不受限、非 NULL 全局唯一
        #（owner 各异，避开 0066 的 one_waiting_per_owner 部分唯一）
        for jid, owner in (("inj_m1", "usr_ij1"), ("inj_m2", "usr_ij2")):
            cur.execute(
                "INSERT INTO ingestion_jobs (job_id, owner_user_id, filename, "
                "safe_name, format_ext, declared_size) "
                "VALUES (%s,%s,'a.svs','a.svs','svs',10)",
                (jid, owner))
        cur.execute("UPDATE ingestion_jobs SET slide_id='sld_ig1' "
                    "WHERE job_id='inj_m1'")
        conn.commit()
        with pytest.raises(psycopg.errors.UniqueViolation):
            cur.execute("UPDATE ingestion_jobs SET slide_id='sld_ig1' "
                        "WHERE job_id='inj_m2'")
            conn.commit()
        conn.rollback()
        # conversion_jobs 同款（源哈希唯一键用不同值避开 0046 自身约束）
        for n, cid in enumerate(("cvj_m1", "cvj_m2")):
            cur.execute(
                "INSERT INTO conversion_jobs (id, source_name, source_sha256, "
                "source_format, converter_id, converter_version, slide_id) "
                "VALUES (%s,'a.kfb',%s,'kfb','kfbconv','1',%s)",
                (cid, "sha_%s" % cid, "sld_cv_%d" % n))
        conn.commit()
        with pytest.raises(psycopg.errors.UniqueViolation):
            cur.execute("UPDATE conversion_jobs SET slide_id='sld_cv_0' "
                        "WHERE id='cvj_m2'")
            conn.commit()
        conn.rollback()
        # baidu_import_items：批次外键先建夹具（0051 形态，最小行）
        cur.execute("DELETE FROM baidu_import_items")
        cur.execute("DELETE FROM baidu_import_batches")
        cur.execute("DELETE FROM baidu_enumerations")
        cur.execute(
            "INSERT INTO baidu_enumerations (id, owner_user_id, share_url_enc) "
            "VALUES ('benu_m1','usr_b','enc')")
        cur.execute(
            "INSERT INTO baidu_import_batches (id, owner_user_id, "
            "enumeration_id, idempotency_key, payload_sha256) "
            "VALUES ('bbat_m1','usr_b','benu_m1','idem_m1','sha_m1')")
        for iid in ("bii_m1", "bii_m2"):
            cur.execute(
                "INSERT INTO baidu_import_items (id, batch_id, candidate_id, "
                "fs_id, name, relative_path) "
                "VALUES (%s,'bbat_m1',%s,'1','a.svs','a.svs')",
                (iid, "bc_" + iid))
        cur.execute("UPDATE baidu_import_items SET slide_id='sld_bi1' "
                    "WHERE id='bii_m1'")
        conn.commit()
        with pytest.raises(psycopg.errors.UniqueViolation):
            cur.execute("UPDATE baidu_import_items SET slide_id='sld_bi1' "
                        "WHERE id='bii_m2'")
            conn.commit()
        conn.rollback()


def test_share_slides_and_project_slides_constraints(conn):
    with conn.cursor() as cur:
        cur.execute("INSERT INTO slides (slide_id) VALUES ('sld_rel1')")
        cur.execute("INSERT INTO shares (token) VALUES ('sht_rel')")
        cur.execute("INSERT INTO share_slides (token, slide_id, position) "
                    "VALUES ('sht_rel','sld_rel1',0)")
        conn.commit()
        # PK (token, slide_id)：重复成员拒
        with pytest.raises(psycopg.errors.UniqueViolation):
            cur.execute("INSERT INTO share_slides (token, slide_id) "
                        "VALUES ('sht_rel','sld_rel1')")
            conn.commit()
        conn.rollback()
        # 幻影 slide_id 拒（FK slides）
        with pytest.raises(psycopg.errors.ForeignKeyViolation):
            cur.execute("INSERT INTO share_slides (token, slide_id) "
                        "VALUES ('sht_rel','sld_ghost')")
            conn.commit()
        conn.rollback()
        # share 删除级联清理成员关系
        cur.execute("DELETE FROM shares WHERE token='sht_rel'")
        cur.execute("SELECT count(*) AS n FROM share_slides")
        assert cur.fetchone()["n"] == 0
        conn.commit()

        # slide_view_grants：(slide_id, user_id) 部分唯一（不同 slide_name 也不行）
        cur.execute("INSERT INTO slide_view_grants (slide_name, user_id, "
                    "slide_id, expires_at) VALUES ('n1.svs','usr_v','sld_rel1',"
                    " now() + interval '30 days')")
        conn.commit()
        with pytest.raises(psycopg.errors.UniqueViolation):
            cur.execute("INSERT INTO slide_view_grants (slide_name, user_id, "
                        "slide_id, expires_at) VALUES "
                        "('n2.svs','usr_v','sld_rel1',"
                        " now() + interval '30 days')")
            conn.commit()
        conn.rollback()
        # slide_id 为 NULL 不受限（0034/0035 孤儿授权形态保留）
        cur.execute("INSERT INTO slide_view_grants (slide_name, user_id, "
                    "expires_at) VALUES ('n3.svs','usr_v',"
                    " now() + interval '30 days')")
        cur.execute("INSERT INTO slide_view_grants (slide_name, user_id, "
                    "expires_at) VALUES ('n4.svs','usr_v',"
                    " now() + interval '30 days')")
        conn.commit()

        # project_slides：(project_id, slide_id) 部分唯一；同名不同 ID 可并存
        cur.execute("INSERT INTO projects (project_id, name) "
                    "VALUES ('prj_m1','p1')")
        cur.execute("INSERT INTO slides (slide_id, legacy_filename) "
                    "VALUES ('sld_rel2','same.svs')")
        cur.execute("INSERT INTO project_slides (project_id, slide, slide_id) "
                    "VALUES ('prj_m1','same.svs','sld_rel1')")
        cur.execute("INSERT INTO project_slides (project_id, slide, slide_id) "
                    "VALUES ('prj_m1','same.svs','sld_rel2')")
        conn.commit()
        with pytest.raises(psycopg.errors.UniqueViolation):
            cur.execute("INSERT INTO project_slides (project_id, slide, slide_id) "
                        "VALUES ('prj_m1','same.svs','sld_rel1')")
            conn.commit()
        conn.rollback()
        # NULL slide_id（历史行未回填）不受限
        cur.execute("INSERT INTO project_slides (project_id, slide) "
                    "VALUES ('prj_m1','same.svs')")
        cur.execute("INSERT INTO project_slides (project_id, slide) "
                    "VALUES ('prj_m1','same.svs')")
        conn.commit()


def test_slide_delete_jobs_constraints(conn):
    with conn.cursor() as cur:
        cur.execute("INSERT INTO slide_delete_jobs (job_id, slide_id) "
                    "VALUES ('delj_m1','sld_d1')")
        conn.commit()
        # slide_id UNIQUE：一个资产至多一条删除任务
        with pytest.raises(psycopg.errors.UniqueViolation):
            cur.execute("INSERT INTO slide_delete_jobs (job_id, slide_id) "
                        "VALUES ('delj_m2','sld_d1')")
            conn.commit()
        conn.rollback()
        with pytest.raises(psycopg.errors.CheckViolation):
            cur.execute("UPDATE slide_delete_jobs SET state='bogus' "
                        "WHERE job_id='delj_m1'")
            conn.commit()
        conn.rollback()
        cur.execute("SELECT state, attempts FROM slide_delete_jobs "
                    "WHERE job_id='delj_m1'")
        row = cur.fetchone()
        assert row["state"] == "pending" and row["attempts"] == 0
        conn.commit()
