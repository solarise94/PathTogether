# -*- coding: utf-8 -*-
"""slide_store（PG 资产状态权威）测试 —— slide ID 化重构 P1-A。

对齐 docs/slide-id-refactor-p1-contract-20260925.md §3.1/§4：
  - allocate：staging/id_bundle/objects relpath、显式 owner、不查原名；
    同名 original_filename 是独立资产（legacy_filename 恒 NULL）；
  - resolve_slide_id：缺失不凭 sld_ 前缀当成功；
  - resolve_legacy_alias：仅查冻结映射；
  - authorize_read 全矩阵：ready 且（admin/owner/public/slide_id 级 view
    grant/share_slides⋈grants/demo capability）；非 ready 一律拒；DB 异常按拒；
  - 状态 CAS（expected_state 谓词）与元数据编辑不动 legacy_filename/授权。

运行：.venv/bin/python -m pytest tests/test_slide_store_pg.py -q
（conftest 起内嵌 PG；每用例前 TRUNCATE 业务表）。
"""
import psycopg
import pytest

import slide_store


# --------------------------------------------------------------------------- #
# fixtures
# --------------------------------------------------------------------------- #
@pytest.fixture
def conn(pg_uri):
    c = psycopg.connect(pg_uri)
    c.row_factory = psycopg.rows.dict_row
    yield c
    c.close()


@pytest.fixture(autouse=True)
def _clean_unmanaged_tables(conn):
    """清掉 conftest TRUNCATE 清单外的新表（无 FK 随 slides CASCADE）：

    upload_task_items / slide_delete_jobs（0067 新表，无外键，显式清）；
    share_slides 有 FK→slides，随 CASCADE 兜底，这里再删一次求稳。
    """
    with conn.cursor() as cur:
        cur.execute("DELETE FROM upload_task_items")
        cur.execute("DELETE FROM slide_delete_jobs")
        cur.execute("DELETE FROM share_slides")
    conn.commit()
    yield


def _alloc_ready(owner="usr_owner", name="r.svs", ext="svs", public=False,
                 accounted=1234):
    """快捷夹具：allocate → mark_ready 的 ready 资产 descriptor。"""
    d = slide_store.allocate_slide(owner, name, ext)
    assert slide_store.mark_ready(d.slide_id, accounted_bytes=accounted)
    if public:
        assert slide_store.set_public(d.slide_id, True)
    return slide_store.resolve_slide_id(d.slide_id)


def _insert_legacy_row(cur, slide_id, legacy_filename, state="legacy",
                       owner=None, public=False):
    cur.execute(
        "INSERT INTO slides (slide_id, legacy_filename, owner_user_id, public, "
        "asset_state) VALUES (%s,%s,%s,%s,%s)",
        (slide_id, legacy_filename, owner, public, state))


# --------------------------------------------------------------------------- #
# allocate / resolve
# --------------------------------------------------------------------------- #
def test_allocate_creates_staging_id_bundle(conn):
    d = slide_store.allocate_slide("usr_a", "A.svs", "svs")
    assert d.slide_id.startswith("sld_") and len(d.slide_id) == len("sld_") + 12
    assert d.asset_state == slide_store.SlideState.STAGING
    assert d.storage_layout == slide_store.StorageLayout.ID_BUNDLE
    assert d.legacy_filename is None            # 新资产恒 NULL（R-01）
    assert d.original_filename == "A.svs"
    assert d.display_name == "A.svs"            # display_name 初始 = original
    assert d.format_ext == "svs"
    assert d.owner_user_id == "usr_a"
    assert d.public is False
    assert d.accounted_bytes is None
    assert d.storage_relpath == "objects/%s/data.svs" % d.slide_id
    assert d.published_at is None and d.deleted_at is None
    # 幂等读回：同一 ID 再 resolve 得到同一 descriptor
    again = slide_store.resolve_slide_id(d.slide_id)
    assert again == d


def test_allocate_same_original_name_independent(conn):
    d1 = slide_store.allocate_slide("usr_a", "dup.svs", "svs")
    d2 = slide_store.allocate_slide("usr_b", "dup.svs", "svs")
    assert d1.slide_id != d2.slide_id          # 同名=独立资产，不查原名
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) AS n FROM slides "
                    "WHERE legacy_filename='dup.svs'")
        assert cur.fetchone()["n"] == 0        # 新资产不写冻结别名
    # original_filename 不是键：两行各带展示快照
    assert d1.original_filename == d2.original_filename == "dup.svs"


def test_allocate_requires_explicit_owner(conn):
    for bad in (None, "", "   "):
        with pytest.raises(ValueError):
            slide_store.allocate_slide(bad, "a.svs", "svs")


def test_allocate_input_validation(conn):
    with pytest.raises(ValueError):
        slide_store.allocate_slide("usr_a", "sub/a.svs", "svs")   # 目录语义
    with pytest.raises(ValueError):
        slide_store.allocate_slide("usr_a", "a\\b.svs", "svs")
    with pytest.raises(ValueError):
        slide_store.allocate_slide("usr_a", "a\x00b.svs", "svs")  # 控制字符
    with pytest.raises(ValueError):
        slide_store.allocate_slide("usr_a", "", "svs")
    with pytest.raises(ValueError):
        slide_store.allocate_slide("usr_a", "a.svs", "sv/s")      # 非法扩展名
    with pytest.raises(ValueError):
        slide_store.allocate_slide("usr_a", "a.svs", "")
    # 归一：大写带点扩展名归一为白名单小写
    d = slide_store.allocate_slide("usr_a", "a.svs", ".SVS")
    assert d.format_ext == "svs"


def test_resolve_slide_id_missing_returns_none(conn):
    assert slide_store.resolve_slide_id("sld_" + "a" * 12) is None  # 不凭前缀
    assert slide_store.resolve_slide_id("nope") is None
    assert slide_store.resolve_slide_id("") is None
    assert slide_store.resolve_slide_id(None) is None
    slide_store.allocate_slide("usr_a", "x.svs", "svs")            # 库里有行
    assert slide_store.resolve_slide_id("sld_" + "b" * 12) is None


def test_resolve_legacy_alias(conn):
    with conn.cursor() as cur:
        _insert_legacy_row(cur, "sld_leg001", "old.svs")
        conn.commit()
    d = slide_store.resolve_legacy_alias("old.svs")
    assert d is not None and d.slide_id == "sld_leg001"
    assert d.asset_state == "legacy"           # 0067 默认
    assert slide_store.resolve_legacy_alias("missing.svs") is None
    # 新资产 original_filename 不进冻结映射：按原名解析不到
    nd = slide_store.allocate_slide("usr_a", "fresh.svs", "svs")
    assert slide_store.resolve_legacy_alias("fresh.svs") is None
    # tombstone 保留别名（UNIQUE 阻止重绑新 ID）；解析仍成功、授权拒绝
    with conn.cursor() as cur:
        cur.execute("UPDATE slides SET asset_state='deleted' "
                    "WHERE slide_id='sld_leg001'")
        conn.commit()
    td = slide_store.resolve_legacy_alias("old.svs")
    assert td is not None and td.asset_state == "deleted"


# --------------------------------------------------------------------------- #
# authorize_read 矩阵
# --------------------------------------------------------------------------- #
def test_authorize_owner_public_admin(conn):
    d = _alloc_ready(owner="usr_o", public=False)
    assert slide_store.authorize_read(d, actor_user_id="usr_o")
    assert not slide_store.authorize_read(d, actor_user_id="usr_other")
    # public 开关
    assert slide_store.set_public(d.slide_id, True)
    assert slide_store.authorize_read(d, actor_user_id="usr_other")
    assert slide_store.set_public(d.slide_id, False)
    assert not slide_store.authorize_read(d, actor_user_id="usr_other")
    # admin 角色（平台 owner）优先放行
    assert slide_store.authorize_read(d, actor_role="owner")
    assert slide_store.authorize_read(d, actor_role="owner",
                                      actor_user_id="usr_other")
    # 字符串 slide_id 也走同一门禁
    assert slide_store.authorize_read(d.slide_id, actor_user_id="usr_o")
    assert not slide_store.authorize_read("sld_" + "z" * 12,
                                          actor_user_id="usr_o")


def test_authorize_view_grant_only_by_slide_id(conn):
    target = _alloc_ready(owner="usr_o")
    with conn.cursor() as cur:
        # slide_id 级授权：放行
        cur.execute(
            "INSERT INTO slide_view_grants (slide_name, user_id, slide_id) "
            "VALUES ('legacy-a.svs', 'usr_g', %s)", (target.slide_id,))
        conn.commit()
    assert slide_store.authorize_read(target, actor_user_id="usr_g")
    # 只认 slide_id 不认 slide_name：授权行绑到别的 slide_id 时，
    # 即使 slide_name 碰巧等于本资产的 original_filename 也不放行
    other = _alloc_ready(owner="usr_o2", name="legacy-a.svs")
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO slide_view_grants (slide_name, user_id, slide_id) "
            "VALUES (%s, 'usr_g2', %s)", (other.original_filename,
                                          target.slide_id))
        conn.commit()
    assert not slide_store.authorize_read(other, actor_user_id="usr_g2")


def test_authorize_share_membership(conn):
    d = _alloc_ready(owner="usr_o")
    tok = "sht_ok"
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO shares (token, slides, permissions) "
            "VALUES (%s, '[]'::jsonb, '[\"view\",\"annotate\"]'::jsonb)",
            (tok,))
        cur.execute(
            "INSERT INTO grants (id, token, user_id, permissions) "
            "VALUES ('gr_1', %s, 'usr_m', '[\"view\"]'::jsonb)", (tok,))
        cur.execute(
            "INSERT INTO share_slides (token, slide_id) VALUES (%s,%s)",
            (tok, d.slide_id))
        conn.commit()
    # 已领取未撤销未过期 → 放行
    assert slide_store.authorize_read(d, actor_user_id="usr_m")
    # shares.slides JSONB 快照不参与判定：塞进 JSONB 不建 share_slides 行 → 拒
    d2 = _alloc_ready(owner="usr_o")
    tok2 = "sht_json"
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO shares (token, slides, permissions) "
            "VALUES (%s, %s::jsonb, '[\"view\"]'::jsonb)",
            (tok2, '["%s"]' % d2.slide_id))
        cur.execute(
            "INSERT INTO grants (id, token, user_id, permissions) "
            "VALUES ('gr_2', %s, 'usr_m', '[\"view\"]'::jsonb)", (tok2,))
        conn.commit()
    assert not slide_store.authorize_read(d2, actor_user_id="usr_m")
    # 撤销 / 过期 / 退领 → 拒
    with conn.cursor() as cur:
        cur.execute("UPDATE shares SET revoked=TRUE WHERE token=%s", (tok,))
        conn.commit()
    assert not slide_store.authorize_read(d, actor_user_id="usr_m")
    with conn.cursor() as cur:
        cur.execute("UPDATE shares SET revoked=FALSE, "
                    "expires_at=now() - interval '1 hour' WHERE token=%s",
                    (tok,))
        conn.commit()
    assert not slide_store.authorize_read(d, actor_user_id="usr_m")
    with conn.cursor() as cur:
        cur.execute("UPDATE shares SET expires_at=NULL WHERE token=%s", (tok,))
        cur.execute("UPDATE grants SET active=FALSE WHERE id='gr_1'")
        conn.commit()
    assert not slide_store.authorize_read(d, actor_user_id="usr_m")


def test_authorize_demo_capability(conn):
    d = _alloc_ready(owner="usr_o")
    # 未入目录：capability 也不放行
    assert not slide_store.authorize_read(d, demo_capability=True)
    with conn.cursor() as cur:
        cur.execute("INSERT INTO demo_catalog (slide_id) VALUES (%s)",
                    (d.slide_id,))
        conn.commit()
    assert slide_store.authorize_read(d, demo_capability=True)   # 匿名 capability
    assert not slide_store.authorize_read(d)                     # 不声明 capability 不放行
    assert slide_store.authorize_read(d, actor_user_id="usr_rand",
                                      demo_capability=True)


def test_authorize_non_ready_states_all_rejected(conn):
    # staging：owner/public/admin/demo 全拒
    d = slide_store.allocate_slide("usr_o", "s.svs", "svs")
    slide_store.set_public(d.slide_id, True)
    assert not slide_store.authorize_read(d, actor_user_id="usr_o")
    assert not slide_store.authorize_read(d, actor_user_id="usr_x")
    assert not slide_store.authorize_read(d, actor_role="owner")
    assert not slide_store.authorize_read(d, demo_capability=True)
    with conn.cursor() as cur:
        cur.execute("INSERT INTO demo_catalog (slide_id) VALUES (%s)",
                    (d.slide_id,))
        conn.commit()
    assert not slide_store.authorize_read(d, demo_capability=True)

    def _mk_state(state):
        with conn.cursor() as cur:
            _insert_legacy_row(cur, "sld_st_%s" % state, "st-%s.svs" % state,
                               state=state, owner="usr_o", public=True)
            conn.commit()
        return slide_store.resolve_legacy_alias("st-%s.svs" % state)

    for state in ("legacy", "deleting", "deleted", "failed"):
        desc = _mk_state(state)
        assert desc is not None and desc.asset_state == state
        assert not slide_store.authorize_read(desc, actor_user_id="usr_o")
        assert not slide_store.authorize_read(desc, actor_role="owner")
        assert not slide_store.authorize_read(desc, demo_capability=True)

    # deleting 经正常路径：ready→deleting 后 owner 也立即失去新授权
    r = _alloc_ready(owner="usr_o")
    assert slide_store.request_delete(r.slide_id)
    rd = slide_store.resolve_slide_id(r.slide_id)
    assert not slide_store.authorize_read(rd, actor_user_id="usr_o")


def test_authorize_db_exception_fail_closed(conn, monkeypatch):
    d = _alloc_ready(owner="usr_o")           # 非 owner、非 public 的主体走 DB 查询
    def _boom():
        raise RuntimeError("db down")
    monkeypatch.setattr(slide_store, "_connect", _boom)
    assert slide_store.authorize_read(d, actor_user_id="usr_x") is False


# --------------------------------------------------------------------------- #
# 状态 CAS（expected_state 谓词）
# --------------------------------------------------------------------------- #
def test_state_cas_full_path(conn):
    d = slide_store.allocate_slide("usr_o", "c.svs", "svs")
    # 错误 expected_state：CAS 失败、状态不动
    assert not slide_store.mark_ready(d.slide_id, accounted_bytes=10,
                                      expected_state=slide_store.SlideState.READY)
    assert slide_store.resolve_slide_id(d.slide_id).asset_state == "staging"
    assert not slide_store.request_delete(d.slide_id)          # staging 不可删
    assert not slide_store.mark_deleted(d.slide_id)
    # 正常链：staging→ready→deleting→deleted
    assert slide_store.mark_ready(d.slide_id, accounted_bytes=4096)
    ready = slide_store.resolve_slide_id(d.slide_id)
    assert ready.asset_state == "ready"
    assert ready.accounted_bytes == 4096
    assert ready.published_at is not None
    assert not slide_store.mark_ready(d.slide_id, accounted_bytes=1)  # 重复发布拒
    assert not slide_store.mark_failed(d.slide_id)            # ready→failed 非合同迁移
    assert slide_store.request_delete(d.slide_id)
    assert not slide_store.request_delete(d.slide_id)         # deleting 再删拒（CAS）
    deleting = slide_store.resolve_slide_id(d.slide_id)
    assert deleting.asset_state == "deleting"
    assert not slide_store.mark_ready(deleting.slide_id, accounted_bytes=1)
    assert slide_store.mark_deleted(d.slide_id)
    deleted = slide_store.resolve_slide_id(d.slide_id)
    assert deleted.asset_state == "deleted"
    assert deleted.deleted_at is not None


def test_mark_failed_from_staging_and_legacy(conn):
    d = slide_store.allocate_slide("usr_o", "f.svs", "svs")
    assert slide_store.mark_failed(d.slide_id)
    assert slide_store.resolve_slide_id(d.slide_id).asset_state == "failed"
    with conn.cursor() as cur:
        _insert_legacy_row(cur, "sld_legfail", "legacy-fail.svs")
        conn.commit()
    assert slide_store.mark_failed("sld_legfail",
                                   expected_state=slide_store.SlideState.LEGACY)
    lf = slide_store.resolve_legacy_alias("legacy-fail.svs")
    assert lf.asset_state == "failed"
    # legacy→ready（回填验证通过，layout 保持 legacy）
    with conn.cursor() as cur:
        _insert_legacy_row(cur, "sld_legok", "legacy-ok.svs")
        conn.commit()
    assert slide_store.mark_ready("sld_legok", accounted_bytes=99,
                                  expected_state=slide_store.SlideState.LEGACY)
    ok = slide_store.resolve_legacy_alias("legacy-ok.svs")
    assert ok.asset_state == "ready" and ok.storage_layout == "legacy"
    assert ok.accounted_bytes == 99


def test_unknown_expected_state_rejected():
    with pytest.raises(ValueError):
        slide_store.mark_ready("sld_x", expected_state="bogus")


# --------------------------------------------------------------------------- #
# 元数据编辑：只动元数据，不动 legacy_filename/授权
# --------------------------------------------------------------------------- #
def test_update_display_name_keeps_alias_and_grants(conn):
    with conn.cursor() as cur:
        _insert_legacy_row(cur, "sld_meta1", "keep.svs", state="ready",
                           owner="usr_o")
        cur.execute(
            "INSERT INTO slide_view_grants (slide_name, user_id, slide_id) "
            "VALUES ('keep.svs', 'usr_g', 'sld_meta1')")
        conn.commit()
    assert slide_store.authorize_read(
        slide_store.resolve_legacy_alias("keep.svs"), actor_user_id="usr_g")
    assert slide_store.update_display_name("sld_meta1", "改名后")
    d = slide_store.resolve_slide_id("sld_meta1")
    assert d.display_name == "改名后"
    assert d.legacy_filename == "keep.svs"          # 冻结别名不动
    assert d.asset_state == "ready"                 # 状态不动
    # 授权与别名解析不受改名影响
    assert slide_store.authorize_read(d, actor_user_id="usr_g")
    assert slide_store.resolve_legacy_alias("keep.svs").slide_id == "sld_meta1"
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) AS n FROM slide_view_grants "
                    "WHERE slide_id='sld_meta1'")
        assert cur.fetchone()["n"] == 1
    # 行缺失 / 非法输入
    assert not slide_store.update_display_name("sld_missing", "x")
    with pytest.raises(ValueError):
        slide_store.update_display_name("sld_meta1", "a\nb")
    with pytest.raises(ValueError):
        slide_store.update_display_name("sld_meta1", "x" * 500)


def test_update_note_and_set_public(conn):
    d = slide_store.allocate_slide("usr_o", "n.svs", "svs")
    assert slide_store.update_note(d.slide_id, "备注一")
    assert slide_store.resolve_slide_id(d.slide_id).note == "备注一"
    assert slide_store.set_public(d.slide_id, True)
    nd = slide_store.resolve_slide_id(d.slide_id)
    assert nd.public is True and nd.asset_state == "staging"  # 只动元数据
    assert not slide_store.update_note("sld_missing", "x")
    assert not slide_store.set_public("sld_missing", True)
    with pytest.raises(ValueError):
        slide_store.update_note(d.slide_id, "x" * 1000)
