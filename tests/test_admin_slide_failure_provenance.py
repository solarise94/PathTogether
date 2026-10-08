# -*- coding: utf-8 -*-
"""管理清单 failed 资产的失败证据溯源字段（inventory ``failure``）。

生产现象：admin UI 对 asset_state=failed 一律显示「处理失败/资产处理失败，
不可读取，不能加入」，而生产 failed 大头是历史回填 missing_file（多数对应
已删除切片）与一条 cancelled_by_user——通用文案误导排障。本文件冻结
``/api/admin/v1/slides/inventory`` 对 failed 行附带的 ``failure`` 证据字段：
  - code：任务表存储的稳定失败码（ingestion fail_code / conversion 与
    baidu 的 error_code）；无任务绑定的 legacy 回填行按「文件缺失」推断
    missing_file（inferred=True），不冒充存储证据；
  - source / source_state / source_ref：来源任务族与终态
    （ingestion_item / ingestion / conversion / baidu_import / upload_task /
    upload_task_item / backfill / unknown）；
  - occurred_at：失败时间（任务终态时间，兜底 slides.updated_at）；
  - 只读、零 schema 变更；不暴露 error_detail_internal / 路径 / 对象键。
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import _bootstrap  # noqa: E402,F401  # session 目录+openslide stub（conftest 先行）
UPLOAD_DIR = _bootstrap.UPLOAD_DIR
import psycopg  # noqa: E402
import pytest  # noqa: E402

import app as app_mod  # noqa: E402
import share_store  # noqa: E402
import slide_store  # noqa: E402
import user_store  # noqa: E402
from _pt_helpers import csrf_client, isolate_app  # noqa: E402
from _tiff_fixtures import make_tiff_bytes  # noqa: E402
from _pt_helpers import publish_test_slide  # noqa: E402


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    _, up_dir = isolate_app(monkeypatch, tmp_path, UPLOAD_DIR)
    monkeypatch.setattr(app_mod, "AUTH_ENABLED", True)
    for child in up_dir.iterdir():
        if child.is_file():
            child.unlink()
    yield


def _sql(query, params=(), fetch=False):
    conn = psycopg.connect(os.environ["DATABASE_URL"])
    try:
        with conn.cursor() as cur:
            cur.execute(query, params)
            out = cur.fetchall() if fetch else None
        conn.commit()
        return out
    finally:
        conn.close()


def _owner_client():
    owner = user_store.create_user("owner@x.com", "ownerpass123456",
                                   role="owner")
    share_store.set_owner_user_id(owner["user_id"])
    app_mod.app.config["TESTING"] = True
    c = csrf_client(app_mod.app.test_client())
    with c.session_transaction() as s:
        s["auth_user"] = owner.get("login_id") or owner.get("user_id")
        s["user_id"] = owner["user_id"]
        s["role"] = "owner"
        s["auth_version"] = owner.get("auth_version", 1)
    return c, owner


def _failed_staging_slide(owner_uid, name="a.tif"):
    """走真实原语：allocate（staging）→ mark_failed，返回 slide_id。"""
    desc = slide_store.allocate_slide(owner_uid, original_filename=name,
                                      format_ext="tif")
    assert slide_store.mark_failed(desc.slide_id)
    return desc.slide_id


def _inventory_item(c, slide_id):
    r = c.get("/api/admin/v1/slides/inventory?limit=200")
    assert r.status_code == 200, r.get_data(as_text=True)
    items = [it for it in r.get_json()["items"]
             if it.get("slide_id") == slide_id]
    assert len(items) == 1, "failed 资产必须留在管理清单（证据不删行）"
    return items[0]


def _insert_ingestion_job(job_id, slide_id, state, fail_code, owner_uid):
    _sql(
        "INSERT INTO ingestion_jobs (job_id, owner_user_id, owner_role, "
        "filename, safe_name, format_ext, declared_size, state, kind, "
        "fail_code, slide_id, terminal_at) "
        "VALUES (%s,%s,'user',%s,%s,'tif',10,%s,'native',%s,%s, now())",
        (job_id, owner_uid, "a.tif", "a.tif", state, fail_code, slide_id))


def test_ingestion_native_failure_code_surfaces():
    c, owner = _owner_client()
    sid = _failed_staging_slide(owner["user_id"])
    _insert_ingestion_job("inj_prov1", sid, "failed", "source_size_mismatch",
                          owner["user_id"])
    item = _inventory_item(c, sid)
    f = item["failure"]
    assert f["code"] == "source_size_mismatch"
    assert f["source"] == "ingestion"
    assert f["source_state"] == "failed"
    assert f["source_ref"] == "inj_prov1"
    assert f["inferred"] is False
    assert f["occurred_at"] and f["occurred_at"] > 0
    assert set(f.keys()) == {"code", "inferred", "source", "source_state",
                             "source_ref", "occurred_at"}


def test_ingestion_cancelled_by_user_surfaces():
    c, owner = _owner_client()
    sid = _failed_staging_slide(owner["user_id"], "cancel.tif")
    _insert_ingestion_job("inj_prov2", sid, "cancelled", "cancelled_by_user",
                          owner["user_id"])
    item = _inventory_item(c, sid)
    f = item["failure"]
    assert f["code"] == "cancelled_by_user"
    assert f["source"] == "ingestion"
    assert f["source_state"] == "cancelled"


def test_ingestion_zip_item_failure_most_specific():
    c, owner = _owner_client()
    sid = _failed_staging_slide(owner["user_id"], "z.tif")
    _sql("INSERT INTO ingestion_jobs (job_id, owner_user_id, owner_role, "
         "filename, safe_name, format_ext, declared_size, state, kind) "
         "VALUES ('inj_zip1', %s, 'user', 'z.zip', 'z.zip', 'zip', 10, "
         "'completed', 'zip')", (owner["user_id"],))
    _sql("INSERT INTO ingestion_job_items (job_id, item_key, slide_id, "
         "state, fail_code) VALUES ('inj_zip1', 'z.tif', %s, 'failed', "
         "'item_source_missing')", (sid,))
    item = _inventory_item(c, sid)
    f = item["failure"]
    assert f["code"] == "item_source_missing"
    assert f["source"] == "ingestion_item"
    assert f["source_ref"] == "inj_zip1"


def test_conversion_failure_and_baidu_attribution():
    c, owner = _owner_client()
    # 独立转换任务（KFB→BigTIFF）失败
    sid1 = _failed_staging_slide(owner["user_id"], "k1.kfb")
    _sql("INSERT INTO conversion_jobs (id, owner_user_id, source_name, "
         "source_sha256, source_format, converter_id, converter_version, "
         "state, error_code, slide_id, finished_at) "
         "VALUES ('cvj_p1', %s, 'k1.kfb', %s, 'kfb', 'cv', '1', 'failed', "
         "'kfb_bad', %s, now())",
         (owner["user_id"], "a" * 64, sid1))
    # 百度导入发起的转换失败：来源归并 baidu_import，不冒充独立转换
    sid2 = _failed_staging_slide(owner["user_id"], "k2.kfb")
    _sql("INSERT INTO conversion_jobs (id, owner_user_id, source_name, "
         "source_sha256, source_format, converter_id, converter_version, "
         "state, error_code, slide_id, finished_at) "
         "VALUES ('cvj_p2', %s, 'k2.kfb', %s, 'kfb', 'cv', '1', 'failed', "
         "'invalid_kfb_header', %s, now())",
         (owner["user_id"], "b" * 64, sid2))
    _sql("INSERT INTO baidu_enumerations (id, owner_user_id, share_url_enc) "
         "VALUES ('enum_p1', %s, 'x')", (owner["user_id"],))
    _sql("INSERT INTO baidu_import_batches (id, owner_user_id, "
         "enumeration_id, idempotency_key, payload_sha256) "
         "VALUES ('bat_p1', %s, 'enum_p1', 'ik1', %s)",
         (owner["user_id"], "c" * 64))
    _sql("INSERT INTO baidu_import_items (id, batch_id, candidate_id, fs_id, "
         "name, relative_path, stage, conversion_job_id) "
         "VALUES ('bii_p1', 'bat_p1', 'cand1', 'fs1', 'k2.kfb', 'k2.kfb', "
         "'failed', 'cvj_p2')")

    f1 = _inventory_item(c, sid1)["failure"]
    assert f1["code"] == "kfb_bad"
    assert f1["source"] == "conversion"
    assert f1["source_state"] == "failed"
    assert f1["source_ref"] == "cvj_p1"
    f2 = _inventory_item(c, sid2)["failure"]
    assert f2["code"] == "invalid_kfb_header"
    assert f2["source"] == "baidu_import"
    assert f2["source_ref"] == "cvj_p2"


def test_baidu_native_item_failure():
    c, owner = _owner_client()
    sid = _failed_staging_slide(owner["user_id"], "b1.svs")
    _sql("INSERT INTO baidu_enumerations (id, owner_user_id, share_url_enc) "
         "VALUES ('enum_p2', %s, 'x')", (owner["user_id"],))
    _sql("INSERT INTO baidu_import_batches (id, owner_user_id, "
         "enumeration_id, idempotency_key, payload_sha256) "
         "VALUES ('bat_p2', %s, 'enum_p2', 'ik2', %s)",
         (owner["user_id"], "d" * 64))
    _sql("INSERT INTO baidu_import_items (id, batch_id, candidate_id, fs_id, "
         "name, relative_path, stage, error_code, slide_id) "
         "VALUES ('bii_p2', 'bat_p2', 'cand2', 'fs2', 'b1.svs', 'b1.svs', "
         "'failed', 'transfer_failed', %s)", (sid,))
    f = _inventory_item(c, sid)["failure"]
    assert f["code"] == "transfer_failed"
    assert f["source"] == "baidu_import"
    assert f["source_state"] == "failed"
    assert f["source_ref"] == "bii_p2"


def test_backfill_missing_file_inferred_and_unknown_fallback():
    c, owner = _owner_client()
    # 历史回填形态：legacy 布局 failed 行、盘上无文件、无任何任务绑定
    _sql("INSERT INTO slides (slide_id, legacy_filename, storage_layout, "
         "asset_state, updated_at) "
         "VALUES ('sld_bf1', 'gone.svs', 'legacy', 'failed', now())")
    ts = _sql("SELECT extract(epoch from updated_at)::float8 FROM slides "
              "WHERE slide_id='sld_bf1'", fetch=True)[0][0]
    f = _inventory_item(c, "sld_bf1")["failure"]
    assert f["code"] == "missing_file"
    assert f["inferred"] is True
    assert f["source"] == "backfill"
    assert f["occurred_at"] == pytest.approx(ts, abs=5)
    # id_bundle 失败行且无任务绑定：不猜原因，档 unknown（code=None）
    sid = _failed_staging_slide(owner["user_id"], "u.tif")
    f2 = _inventory_item(c, sid)["failure"]
    assert f2["code"] is None
    assert f2["source"] == "unknown"
    assert f2["inferred"] is False


def test_non_failed_rows_carry_no_failure_field():
    c, owner = _owner_client()
    sid = publish_test_slide("ok.tif", make_tiff_bytes(),
                             owner_user_id=owner["user_id"])
    item = _inventory_item(c, sid)
    assert item["asset_state"] == "ready"
    assert "failure" not in item
