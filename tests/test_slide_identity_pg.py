# -*- coding: utf-8 -*-
"""稳定 slide 身份测试（仅 PG 后端跑，Stage 3b-2）。

验收对齐 docs §Stage 3b：
  - set_slide_meta 首次出现 name → 生成稳定 slide_id（sld_ + 12 位 urlsafe）；
  - 同名已存在 → 复用同一 slide_id（映射唯一），不会出现「同名不同稳定 ID」；
  - 更新别名/note/owner/public 不动 slide_id；
  - SQL 级验证：更新 legacy_filename（重命名）不影响 slide_id 主键；
  - record_slide_asset 记录内容资产 revision（slide_assets 行）。

本模块只在 RUN_PG_TESTS=1（BACKEND=='postgres'）时真正断言；json 后端下跳过。
psycopg 延迟 import（缺依赖的裸解释器在 json 模式也能收集本模块，整模块 skip）。
"""
import pytest

from conftest import BACKEND  # noqa: E402

import share_store  # noqa: E402

if BACKEND == "postgres":
    import psycopg  # noqa: E402
else:
    psycopg = None  # type: ignore


@pytest.fixture
def conn(pg_uri):
    """每用例新连接（autocommit=False，dict_row 便于按列名取）。"""
    c = psycopg.connect(pg_uri)
    c.row_factory = psycopg.rows.dict_row
    yield c
    c.close()


def test_stable_id_generated_on_first_meta(conn):
    sid = share_store.get_slide_id("a.svs")
    assert sid is None  # 尚未建行
    share_store.set_slide_meta("a.svs", alias="切片A", note="note1")
    sid1 = share_store.get_slide_id("a.svs")
    assert sid1 and sid1.startswith("sld_") and len(sid1) == len("sld_") + 12
    with conn.cursor() as cur:
        cur.execute("SELECT legacy_filename FROM slides WHERE slide_id=%s", (sid1,))
        assert cur.fetchone()["legacy_filename"] == "a.svs"


def test_same_name_reuses_same_slide_id(conn):
    share_store.set_slide_meta("b.svs", alias="B")
    sid1 = share_store.get_slide_id("b.svs")
    share_store.set_slide_meta("b.svs", note="more")
    sid2 = share_store.get_slide_id("b.svs")
    assert sid1 == sid2  # 同名不会产生不同稳定 ID


def test_update_meta_keeps_slide_id(conn):
    share_store.set_slide_meta("c.svs", alias="before")
    sid = share_store.get_slide_id("c.svs")
    share_store.set_slide_meta("c.svs", alias="after", note="n", public=True)
    assert share_store.get_slide_id("c.svs") == sid
    full = share_store.get_slide_meta_full("c.svs")
    assert full["alias"] == "after" and full["public"] is True


def test_rename_does_not_change_slide_id(conn):
    """SQL 级验证重命名（更新 legacy_filename）不改变 slide_id 主键。"""
    share_store.set_slide_meta("old.svs")
    sid = share_store.get_slide_id("old.svs")
    assert sid is not None
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE slides SET legacy_filename='new.svs' WHERE slide_id=%s", (sid,))
        conn.commit()
    # 主键 slide_id 不变；映射键由 old → new
    assert share_store.get_slide_id("old.svs") is None
    assert share_store.get_slide_id("new.svs") == sid
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM slides WHERE slide_id=%s", (sid,))
        assert cur.fetchone()["count"] == 1


def test_resolve_slide_ref(conn):
    share_store.set_slide_meta("d.svs")
    sid = share_store.get_slide_id("d.svs")
    assert share_store.resolve_slide_ref("d.svs") == sid
    assert share_store.resolve_slide_ref(sid) == sid  # 已是稳定 id 直返
    assert share_store.resolve_slide_ref("nope.svs") is None


def test_record_slide_asset(conn):
    share_store.set_slide_meta("e.svs")
    sid = share_store.get_slide_id("e.svs")
    asset_id = share_store.record_slide_asset(sid, "100:200")
    assert asset_id and asset_id.startswith("ast_")
    with conn.cursor() as cur:
        cur.execute(
            "SELECT asset_id, slide_id, legacy_revision FROM slide_assets "
            "WHERE asset_id=%s", (asset_id,))
        row = cur.fetchone()
        assert row["slide_id"] == sid
        assert row["legacy_revision"] == "100:200"


def test_legacy_filename_unique(conn):
    """同名不可有两行（UNIQUE 约束）→ 同名不同稳定 ID 不可能。"""
    share_store.set_slide_meta("u.svs")
    with conn.cursor() as cur:
        with pytest.raises(psycopg.errors.UniqueViolation):
            cur.execute(
                "INSERT INTO slides (slide_id, legacy_filename) "
                "VALUES (%s, %s)", ("sld_another", "u.svs"))
            conn.commit()
        conn.rollback()
