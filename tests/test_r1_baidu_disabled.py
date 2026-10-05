# -*- coding: utf-8 -*-
"""R1 发布：百度导入全程关闭（BAIDU_ENUMERATION_ENABLED=0、BAIDU_IMPORT_ENABLED=0）。

R1 的插件不能枚举分享，关掉旧 worker 后平台没有任何执行者；仅靠
BAIDU_IMPORT_WORKER=0 不能阻止 API 继续受理请求。两个开关为 0 时，经真实
HTTP 路由与生产适配器（连接器探测用本地无副作用二进制，原因只来自开关）：
新建枚举、从既有 ready 枚举建批次、重试失败批次都 503，且不产生任何
待执行行或容量预约；能力端点给出「暂时关闭」的原因码。
"""
import os
import sys
from pathlib import Path

import psycopg
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import _bootstrap  # noqa: F401,E402

import app as app_mod  # noqa: E402
import baidu_import_store as store  # noqa: E402
from _baidu_helpers import (TEST_SECRET, create_batch,  # noqa: E402
                            make_ready_enumeration)
from _pt_helpers import csrf_client, isolate_app  # noqa: E402
from baidu_adapter import ProductionBaiduAdapter  # noqa: E402

USER = "r1-bd-user"


@pytest.fixture()
def _iso(monkeypatch):
    isolate_app(monkeypatch, _bootstrap.SHARE_DATA_DIR, clear_stores=True)
    app_mod.AUTH_ENABLED = True
    monkeypatch.setenv("BAIDU_SHARE_SECRET_KEY", TEST_SECRET)
    yield


def _sql(q, args=()):
    with psycopg.connect(os.environ["DATABASE_URL"], autocommit=True) as c:
        with c.cursor() as cur:
            cur.execute(q, args)
            return cur.fetchall() if cur.description else None


def _mkuser(user_id):
    _sql("INSERT INTO users (user_id, login_id, display_name, role, disabled,"
         " created_at, auth_version) VALUES (%s, %s, %s, 'user', false, now(),"
         " 1) ON CONFLICT (user_id) DO NOTHING",
         (user_id, user_id + "@x", user_id))


def _client(user_id):
    app_mod.app.config["TESTING"] = True
    _mkuser(user_id)
    client = csrf_client(app_mod.app.test_client())
    with client.session_transaction() as s:
        s["auth_user"] = user_id + "@x"
        s["user_id"] = user_id
        s["role"] = "user"
        s["auth_version"] = 1
    return client


def _switch_to_production(monkeypatch, enum_on, import_on):
    """生产适配器 + 可用的本地连接器（/bin/true）——不可用原因只来自开关。"""
    monkeypatch.setenv("BAIDU_ENUMERATION_ENABLED", enum_on)
    monkeypatch.setenv("BAIDU_IMPORT_ENABLED", import_on)
    monkeypatch.setenv("BAIDU_CONNECTOR_BIN", "/bin/true")
    monkeypatch.setattr(store, "get_adapter", ProductionBaiduAdapter)


def _counts():
    return {
        "enumerations": _sql("SELECT count(*) FROM baidu_enumerations")[0][0],
        "batches": _sql("SELECT count(*) FROM baidu_import_batches")[0][0],
        "items": _sql("SELECT count(*) FROM baidu_import_items")[0][0],
        "queued_batches": _sql("SELECT count(*) FROM baidu_import_batches"
                               " WHERE state='queued'")[0][0],
        "queued_enums": _sql("SELECT count(*) FROM baidu_enumerations"
                             " WHERE state='queued'")[0][0],
        "reservations": _sql("SELECT count(*) FROM upload_reservations")[0][0],
    }


def _failed_batch(monkeypatch):
    """一个已终态 failed、条目可重试的批次（生产盘点中的形态）。"""
    _fake, _enum_id, batch = create_batch(
        monkeypatch, owner=USER, paths=["A1/scan.ome.tif"],
        idempotency_key="r1-failed")
    _sql("UPDATE baidu_import_items SET stage='failed',"
         " error_code='download_failed' WHERE batch_id=%s", (batch["id"],))
    _sql("UPDATE baidu_import_batches SET state='failed' WHERE id=%s",
         (batch["id"],))
    return batch["id"]


def test_both_flags_off_rejects_all_new_work_over_http(_iso, monkeypatch):
    client = _client(USER)
    # 切换前（旧部署）留下的：一个 ready 枚举 + 一个 failed 批次
    _fake, enum_id, by_path = make_ready_enumeration(
        monkeypatch, owner=USER)
    failed_id = _failed_batch(monkeypatch)
    failed_items = _sql("SELECT id FROM baidu_import_items WHERE batch_id=%s",
                        (failed_id,))
    _switch_to_production(monkeypatch, "0", "0")
    before = _counts()

    r = client.get("/api/remote-imports/baidu/capabilities")
    assert r.status_code == 200
    caps = r.get_json()
    assert caps["enumeration_available"] is False
    assert caps["import_available"] is False
    assert caps["reason_code"] == "enumeration_disabled"

    r = client.post("/api/remote-imports/baidu/enumerations",
                    json={"share_text": "https://pan.baidu.com/s/1TestShareId99"})
    assert r.status_code == 503, r.get_json()
    assert r.get_json()["code"] == "enumeration_disabled"

    r = client.post("/api/remote-imports/baidu/imports",
                    json={"enumeration_id": enum_id,
                          "candidate_ids": [by_path["A1/scan.ome.tif"]["id"]]},
                    headers={"Idempotency-Key": "r1-new"})
    assert r.status_code == 503, r.get_json()
    assert r.get_json()["code"] == "enumeration_disabled"

    r = client.post("/api/remote-imports/baidu/imports/%s/retry" % failed_id,
                    json={"item_ids": [row[0] for row in failed_items]},
                    headers={"Idempotency-Key": "r1-retry"})
    assert r.status_code == 503, r.get_json()

    assert _counts() == before
    assert _sql("SELECT state FROM baidu_import_batches WHERE id=%s",
                (failed_id,))[0][0] == "failed"
    assert {row[0] for row in _sql(
        "SELECT stage FROM baidu_import_items WHERE batch_id=%s",
        (failed_id,))} == {"failed"}

    # 查询类请求照常可用（用户能看到既有记录）
    assert client.get("/api/remote-imports/baidu/imports/%s"
                      % failed_id).status_code == 200


def test_import_flag_off_alone_rejects_batches(_iso, monkeypatch):
    """只关导入（枚举开）：建批次与重试仍被拒，原因 import_disabled。"""
    client = _client(USER)
    _fake, enum_id, by_path = make_ready_enumeration(
        monkeypatch, owner=USER)
    failed_id = _failed_batch(monkeypatch)
    _switch_to_production(monkeypatch, "1", "0")
    before = _counts()

    r = client.post("/api/remote-imports/baidu/imports",
                    json={"enumeration_id": enum_id,
                          "candidate_ids": [by_path["A1/scan.ome.tif"]["id"]]},
                    headers={"Idempotency-Key": "r1-new2"})
    assert r.status_code == 503
    assert r.get_json()["code"] == "import_disabled"
    item_ids = [row[0] for row in _sql(
        "SELECT id FROM baidu_import_items WHERE batch_id=%s", (failed_id,))]
    r = client.post("/api/remote-imports/baidu/imports/%s/retry" % failed_id,
                    json={"item_ids": item_ids},
                    headers={"Idempotency-Key": "r1-retry2"})
    assert r.status_code == 503
    assert r.get_json()["code"] == "import_disabled"
    assert _counts() == before
