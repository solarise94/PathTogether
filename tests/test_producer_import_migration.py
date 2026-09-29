# -*- coding: utf-8 -*-
"""C5 迁移 0077（producer_imports/producer_import_events/plugin_import_grants
+ plugin_installations.approved_scopes）。

fresh 库（conftest ensure_schema 已应用 0077）+ 升级库（原始 SQL 重跑两次
幂等 no-op）+ CHECK/唯一约束/缺省值冒烟——tests/test_migration.py 的 0055
同款模式。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import _bootstrap  # noqa: E402,F401

import pg_store  # noqa: E402
import psycopg  # noqa: E402
import pytest  # noqa: E402

_MIGRATION_0077 = "0077_producer_imports.sql"


def test_migration_0077_applied_and_raw_sql_idempotent(pg_uri):
    """conftest ensure_schema 已应用 0077；原始 SQL 重跑两次 no-op。"""
    c = psycopg.connect(pg_uri)
    try:
        with c.cursor() as cur:
            cur.execute("SELECT 1 FROM schema_migrations WHERE filename=%s",
                        (_MIGRATION_0077,))
            assert cur.fetchone() is not None, "0077 应已被 ensure_schema 应用"
    finally:
        c.close()
    sql = (pg_store.migrations_dir() / _MIGRATION_0077).read_text(
        encoding="utf-8")
    c = psycopg.connect(pg_uri)
    try:
        with c.cursor() as cur:
            for _ in range(2):
                cur.execute(sql)
        c.commit()
    finally:
        c.close()


def test_migration_0077_constraints_and_defaults(pg_uri):
    c = psycopg.connect(pg_uri)
    try:
        with c.cursor() as cur:
            # 缺省：approved_scopes={}（存量安装行不自动获得 slide:import）
            cur.execute(
                "INSERT INTO plugin_installations (installation_id, plugin_id,"
                " secret_hash) VALUES ('pin_m77a','dev.m77','x') RETURNING"
                " approved_scopes")
            assert list(cur.fetchone()[0]) == []
            # 状态机 CHECK 拒绝未知 state
            violated = False
            try:
                cur.execute(
                    "INSERT INTO producer_imports (import_id, installation_id,"
                    " grant_id, owner_user_id, slide_id, filename, format_ext,"
                    " declared_size, commit_token, write_token_hash, deadline_at)"
                    " VALUES ('pim_m77a','pin_m77a','pig_x','usr_x','sld_x',"
                    "'a.tif','tif',10,'tok','hash', now())")
                cur.execute("UPDATE producer_imports SET state='weird' "
                            "WHERE import_id='pim_m77a'")
                c.commit()
            except psycopg.errors.CheckViolation:
                violated = True
                c.rollback()
            assert violated, "未知 state 应被 producer_imports_state_check 拒绝"
            # 幂等域唯一：同 (installation_id, idempotency_key) 第二行被拒
            def _insert(key):
                cur.execute(
                    "INSERT INTO producer_imports (import_id, installation_id,"
                    " grant_id, owner_user_id, slide_id, filename, format_ext,"
                    " declared_size, commit_token, write_token_hash,"
                    " idempotency_key, deadline_at) VALUES (%s,'pin_m77a',"
                    "'pig_x','usr_x','sld_x','a.tif','tif',10,'tok','hash',"
                    "%s, now())", ("pim_m77b", key))
            _insert("m77-key")
            c.commit()
            dup = False
            try:
                _insert("m77-key")
                c.commit()
            except psycopg.errors.UniqueViolation:
                dup = True
                c.rollback()
            assert dup, "同 (installation_id, idempotency_key) 应被唯一索引拒绝"
            # 事件表可写（detail 脱敏由应用层保证；迁移只建表）
            cur.execute(
                "INSERT INTO producer_import_events (import_id, kind) "
                "VALUES ('pim_m77b','created')")
            c.commit()
            # grant 表：撤销幂等（revoked_at 置位后再次撤销保持首个时间戳）
            cur.execute(
                "INSERT INTO plugin_import_grants (grant_id, installation_id,"
                " user_id, expires_at) VALUES ('pig_m77','pin_m77a','usr_x',"
                " now() + interval '1 hour')")
            cur.execute("UPDATE plugin_import_grants SET revoked_at=now() "
                        "WHERE grant_id='pig_m77' AND revoked_at IS NULL "
                        "RETURNING revoked_at")
            first = cur.fetchone()[0]
            c.commit()
            cur.execute("UPDATE plugin_import_grants SET revoked_at=now() "
                        "WHERE grant_id='pig_m77' AND revoked_at IS NULL")
            assert cur.rowcount == 0
            cur.execute("SELECT revoked_at FROM plugin_import_grants "
                        "WHERE grant_id='pig_m77'")
            assert cur.fetchone()[0] == first
            c.commit()
    finally:
        c.close()
