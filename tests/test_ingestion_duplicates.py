# -*- coding: utf-8 -*-
"""重上传提示与断点续传清单（upload-resilience 2026-11-10）。

覆盖：
  - POST /api/ingestions/duplicates：登录/上传权限、owner 隔离、90 天窗口、
    completed 需 ready 且未删除、unfinished 仅限可续传状态（waiting/
    preparing/uploading）与块数（COS ListParts 权威；不可得时
    uploaded_parts=null）、processing（已传完、服务端处理中）单列且绝不
    混入 unfinished、JSON body 契约（非 JSON/超大 → 400）；
  - GET /api/ingestions/<id>/parts：owner 隔离、状态闸、冻结计划 + ListParts
    清单（编号/长度/ETag）、ListParts 失败降级 502 且不泄露对象键；
  - Web 进程 ListParts 一律传受限 timeout/retry（不钉住 gunicorn 线程）。

COS 一律 monkeypatch ``cos_client.list_parts``（与既有 ingestion 测试同一
方式：worker 之外不连真实 COS）。
"""

import json
import time

import pytest

import app as app_mod
import cos_client
import ingestion_store as ist
import pg_store
from _pt_helpers import csrf_client


def _mkuser(user_id, role="user"):
    conn = pg_store.connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                cur.execute(
                    "INSERT INTO users (user_id, login_id, display_name, role, "
                    "disabled, created_at, auth_version) VALUES (%s, %s, %s, "
                    "%s, false, now(), 1) ON CONFLICT (user_id) DO NOTHING",
                    (user_id, user_id + "@x", user_id, role))
    finally:
        conn.close()


def _client(uid, role="user"):
    app_mod.app.config["TESTING"] = True
    _mkuser(uid, role)
    client = csrf_client(app_mod.app.test_client())
    with client.session_transaction() as s:
        s["auth_user"] = uid + "@x"
        s["user_id"] = uid
        s["role"] = role
        s["auth_version"] = 1
    return client


@pytest.fixture()
def user_client():
    return _client("dup-user-1")


@pytest.fixture()
def other_client():
    return _client("dup-user-2")


def _sql(fn):
    conn = pg_store.connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                fn(cur)
    finally:
        conn.close()


def _mk_slide(slide_id, owner, original, display=None, *, state="ready",
              deleted=False):
    def op(cur):
        cur.execute(
            "INSERT INTO slides (slide_id, display_name, original_filename, "
            "owner_user_id, asset_state, published_at, deleted_at) "
            "VALUES (%s, %s, %s, %s, %s, now(), "
            + ("now()" if deleted else "NULL") + ")",
            (slide_id, display if display is not None else original,
             original, owner, state))
    _sql(op)


def _mk_job(job_id, owner, filename, size, *, state, slide_id=None,
            source_size=None, terminal_sql=None, upload_id=None,
            object_key=None, plan=None, created_sql="now()"):
    def op(cur):
        cur.execute(
            "INSERT INTO ingestion_jobs (job_id, owner_user_id, owner_role, "
            "filename, safe_name, format_ext, declared_size, state, "
            "transport, policy_version, route_reason, slide_id, kind, "
            "source_size_bytes, terminal_at, upload_id, object_key, "
            "part_plan_json, waiting_expires_at, job_deadline_at, created_at) "
            "VALUES (%s, %s, 'user', %s, %s, %s, %s, %s, 'presign_parts', "
            "'v1-manual', 'manual_cos', %s, 'native', %s, "
            + (terminal_sql or "NULL") + ", %s, %s, %s, "
            "now() + interval '1800 seconds', "
            "now() + interval '3 days', " + created_sql + ")",
            (job_id, owner, filename, filename, filename.rsplit(".", 1)[-1],
             size, state, slide_id, source_size, upload_id, object_key,
             None if plan is None else __import__("json").dumps(plan)))
    _sql(op)


def _query(client, filename="scan.tif", size=1000):
    return client.post("/api/ingestions/duplicates",
                       json={"filename": filename, "size": size})


# --------------------------------------------------------------------------- #
# auth / 参数
# --------------------------------------------------------------------------- #
def test_duplicates_requires_upload_permission(monkeypatch):
    app_mod.app.config["TESTING"] = True
    # 认证关闭时本地态即唯一 owner（既有语义）；本用例显式开认证验证登录闸
    monkeypatch.setattr(app_mod, "AUTH_ENABLED", True)
    anon = csrf_client(app_mod.app.test_client())
    r = _query(anon)
    # 未登录：全局认证闸 401；登录但无上传权限：路由闸 403
    assert r.status_code in (401, 403)


def test_duplicates_is_post_only(user_client):
    """文件名不得走查询串（访问日志/代理）：GET 不再提供该端点。"""
    r = user_client.get("/api/ingestions/duplicates",
                        query_string={"filename": "scan.tif", "size": 1000})
    assert r.status_code == 405


def test_duplicates_validates_filename_and_size(user_client):
    r = user_client.post("/api/ingestions/duplicates")
    assert r.status_code == 400
    assert r.get_json()["code"] == "invalid_body"
    r = user_client.post("/api/ingestions/duplicates",
                         json={"filename": "a.tif", "size": 0})
    assert r.status_code == 400
    assert r.get_json()["code"] == "invalid_size"
    r = user_client.post("/api/ingestions/duplicates",
                         json={"filename": "  ", "size": 10})
    assert r.status_code == 400
    assert r.get_json()["code"] == "invalid_filename"
    r = user_client.post("/api/ingestions/duplicates",
                         json={"filename": 123, "size": 10})
    assert r.status_code == 400
    assert r.get_json()["code"] == "invalid_filename"
    r = user_client.post("/api/ingestions/duplicates",
                         json={"filename": "a.tif", "size": True})
    assert r.status_code == 400
    assert r.get_json()["code"] == "invalid_size"
    # 非 JSON / 坏 JSON / 超大 body 一律 400（不进入业务查询）
    r = user_client.post("/api/ingestions/duplicates",
                         data="filename=scan.tif&size=1000",
                         content_type="text/plain")
    assert r.status_code == 400
    assert r.get_json()["code"] == "invalid_body"
    r = user_client.post("/api/ingestions/duplicates", data="{bad json",
                         content_type="application/json")
    assert r.status_code == 400
    assert r.get_json()["code"] == "invalid_body"
    big = json.dumps({"filename": "a.tif", "size": 10,
                      "pad": "x" * 5000})
    r = user_client.post("/api/ingestions/duplicates", data=big,
                         content_type="application/json")
    assert r.status_code == 400
    assert r.get_json()["code"] == "body_too_large"


def test_duplicates_empty_when_no_match(user_client):
    r = _query(user_client)
    assert r.status_code == 200
    assert r.get_json() == {"completed": None, "unfinished": None,
                            "processing": None}
    assert r.headers["Cache-Control"] == "no-store"


# --------------------------------------------------------------------------- #
# completed 匹配：owner 隔离 / ready / 90 天
# --------------------------------------------------------------------------- #
def test_completed_match_and_owner_scoping(user_client, other_client):
    _mk_slide("sld_own", "dup-user-1", "scan.tif", "我的切片")
    _mk_job("inj_own", "dup-user-1", "scan.tif", 1000,
            state=ist.COMPLETED, slide_id="sld_own", source_size=1000,
            terminal_sql="now()")
    # 同一时刻另有同 owner 的**旧**匹配：取最近完成的一条
    _mk_slide("sld_old", "dup-user-1", "scan.tif", "旧切片")
    _mk_job("inj_old", "dup-user-1", "scan.tif", 1000,
            state=ist.COMPLETED, slide_id="sld_old", source_size=1000,
            terminal_sql="now() - interval '10 days'")
    # 他人同名同大小：绝不出现在本人查询里
    _mk_slide("sld_other", "dup-user-2", "scan.tif", "别人的切片")
    _mk_job("inj_other", "dup-user-2", "scan.tif", 1000,
            state=ist.COMPLETED, slide_id="sld_other", source_size=1000,
            terminal_sql="now()")

    body = _query(user_client).get_json()
    assert body["completed"] is not None
    assert body["completed"]["slide_id"] == "sld_own"
    assert body["completed"]["display_name"] == "我的切片"
    assert isinstance(body["completed"]["completed_at"], (int, float))
    # 他人查询只看到他自己的
    assert _query(other_client).get_json()["completed"]["slide_id"] == "sld_other"


def test_completed_requires_ready_and_recent(user_client):
    _mk_slide("sld_gone", "dup-user-1", "scan.tif", state="deleting")
    _mk_job("inj_gone", "dup-user-1", "scan.tif", 1000,
            state=ist.COMPLETED, slide_id="sld_gone", source_size=1000,
            terminal_sql="now()")
    assert _query(user_client).get_json()["completed"] is None

    _mk_slide("sld_deleted", "dup-user-1", "scan.tif", state="ready",
              deleted=True)
    _mk_job("inj_deleted", "dup-user-1", "scan.tif", 1000,
            state=ist.COMPLETED, slide_id="sld_deleted", source_size=1000,
            terminal_sql="now()")
    assert _query(user_client).get_json()["completed"] is None

    # 90 天窗口：91 天前完成 → 不算
    _mk_slide("sld_windy", "dup-user-1", "windy.tif", state="ready")
    _mk_job("inj_windy", "dup-user-1", "windy.tif", 1000,
            state=ist.COMPLETED, slide_id="sld_windy", source_size=1000,
            terminal_sql="now() - interval '91 days'")
    assert _query(user_client, "windy.tif", 1000).get_json()["completed"] is None
    # 89 天前 → 仍算
    _mk_slide("sld_windy2", "dup-user-1", "windy.tif", state="ready")
    _mk_job("inj_windy2", "dup-user-1", "windy.tif", 1000,
            state=ist.COMPLETED, slide_id="sld_windy2", source_size=1000,
            terminal_sql="now() - interval '89 days'")
    assert _query(user_client, "windy.tif", 1000).get_json()[
        "completed"]["slide_id"] == "sld_windy2"


def test_completed_size_uses_source_size_when_pinned(user_client):
    _mk_slide("sld_size", "dup-user-1", "scan.tif")
    # 声明大小与钉源大小不同：以 source_size_bytes 为准（完成后权威值）
    _mk_job("inj_size", "dup-user-1", "scan.tif", 1000,
            state=ist.COMPLETED, slide_id="sld_size", source_size=1001,
            terminal_sql="now()")
    assert _query(user_client, "scan.tif", 1000).get_json()["completed"] is None
    assert _query(user_client, "scan.tif", 1001).get_json()[
        "completed"]["slide_id"] == "sld_size"


# --------------------------------------------------------------------------- #
# unfinished：状态集合 + 权威块数
# --------------------------------------------------------------------------- #
def _list_parts_recorder(result):
    """替换 cos_client.list_parts：记录调用（含受限 timeout/retry）。"""
    calls = []

    def fake(key, upload_id, *, timeout=None, retry=None):
        calls.append({"key": key, "upload_id": upload_id,
                      "timeout": timeout, "retry": retry})
        return result

    return fake, calls


def test_unfinished_reports_state_and_authoritative_parts(
        user_client, monkeypatch):
    plan = [{"part_number": n, "offset": (n - 1) * 500, "length": 500}
            for n in (1, 2)]
    _mk_job("inj_live", "dup-user-1", "scan.tif", 1000, state=ist.UPLOADING,
            upload_id="up-1", object_key="incoming/u/inj_live/x", plan=plan)
    fake, calls = _list_parts_recorder([
        {"part_number": 1, "etag": '"abc"', "size": 500,
         "last_modified": time.time()}])
    monkeypatch.setattr(cos_client, "list_parts", fake)
    body = _query(user_client).get_json()
    assert body["unfinished"] == {
        "job_id": "inj_live", "state": "uploading",
        "uploaded_parts": 1, "total_parts": 2,
    }
    # web 线程受限调用：浏览器 3 秒放弃，SDK 默认重试（4 次尝试）不可接受
    assert calls == [{"key": "incoming/u/inj_live/x", "upload_id": "up-1",
                      "timeout": 3, "retry": 0}]


def test_unfinished_parts_unknown_when_list_parts_fails(
        user_client, monkeypatch):
    plan = [{"part_number": 1, "offset": 0, "length": 1000}]
    _mk_job("inj_live", "dup-user-1", "scan.tif", 1000, state=ist.UPLOADING,
            upload_id="up-1", object_key="incoming/u/inj_live/x", plan=plan)

    def boom(key, upload_id, *, timeout=None, retry=None):
        raise cos_client.CosClientError("HTTP 500 InternalError")

    monkeypatch.setattr(cos_client, "list_parts", boom)
    body = _query(user_client).get_json()
    assert body["unfinished"]["uploaded_parts"] is None
    assert body["unfinished"]["total_parts"] == 1


@pytest.mark.parametrize("state", [ist.WAITING, ist.PREPARING])
def test_unfinished_waiting_or_preparing_has_no_parts(user_client, state):
    _mk_job("inj_pending", "dup-user-1", "scan.tif", 1000, state=state)
    body = _query(user_client).get_json()
    assert body["unfinished"] == {
        "job_id": "inj_pending", "state": state,
        "uploaded_parts": 0, "total_parts": 0,
    }
    assert body["processing"] is None


@pytest.mark.parametrize("state", ist.DUPLICATE_PROCESSING_STATES)
def test_processing_states_are_not_unfinished(user_client, state):
    """分块已全部上传：只报 processing，绝不报「未完成 0/N 块」。"""
    plan = [{"part_number": n, "offset": (n - 1) * 500, "length": 500}
            for n in (1, 2)]
    _mk_job("inj_proc", "dup-user-1", "scan.tif", 1000, state=state,
            upload_id="up-1", object_key="incoming/u/inj_proc/x", plan=plan)
    body = _query(user_client).get_json()
    assert body["unfinished"] is None
    assert body["processing"] == {"job_id": "inj_proc", "state": state}


def test_processing_is_owner_scoped_and_most_recent(user_client,
                                                    other_client):
    # 同一 owner 的活动任务唯一索引不允许两条 ACTIVE_UPLOAD_STATES 并存：
    # 旧任务用 ready（不属于活动集，但仍算「服务端处理中」）。
    _mk_job("inj_proc_old", "dup-user-1", "scan.tif", 1000,
            state=ist.READY, created_sql="now() - interval '5 minutes'")
    _mk_job("inj_proc_new", "dup-user-1", "scan.tif", 1000,
            state=ist.QUEUED)
    _mk_job("inj_proc_other", "dup-user-2", "scan.tif", 1000,
            state=ist.DOWNLOADING)
    assert _query(user_client).get_json()["processing"] == {
        "job_id": "inj_proc_new", "state": ist.QUEUED}
    assert _query(other_client).get_json()["processing"] == {
        "job_id": "inj_proc_other", "state": ist.DOWNLOADING}


def test_unfinished_and_processing_are_reported_independently(
        user_client, monkeypatch):
    """旧任务可续传、新任务处理中：两个键都在；客户端按 unfinished 优先，
    且只取消 unfinished（processing 绝不取消）。"""
    plan = [{"part_number": 1, "offset": 0, "length": 1000}]
    _mk_job("inj_old", "dup-user-1", "scan.tif", 1000, state=ist.UPLOADING,
            upload_id="up-old", object_key="incoming/u/inj_old/x", plan=plan,
            created_sql="now() - interval '5 minutes'")
    # ready 不在活动任务唯一索引内（同 owner 可与 uploading 并存）
    _mk_job("inj_new", "dup-user-1", "scan.tif", 1000, state=ist.READY)
    fake, _calls = _list_parts_recorder([])
    monkeypatch.setattr(cos_client, "list_parts", fake)
    body = _query(user_client).get_json()
    assert body["unfinished"]["job_id"] == "inj_old"
    assert body["processing"] == {"job_id": "inj_new",
                                  "state": ist.READY}


def test_terminal_job_is_not_unfinished(user_client):
    _mk_slide("sld_failed", "dup-user-1", "scan.tif", state="failed")
    _mk_job("inj_failed", "dup-user-1", "scan.tif", 1000,
            state=ist.FAILED, slide_id="sld_failed", source_size=1000,
            terminal_sql="now()")
    body = _query(user_client).get_json()
    assert body["unfinished"] is None
    assert body["processing"] is None
    assert body["completed"] is None


# --------------------------------------------------------------------------- #
# GET /api/ingestions/<id>/parts
# --------------------------------------------------------------------------- #
def test_parts_endpoint_returns_plan_and_listed_parts(
        user_client, monkeypatch):
    plan = [{"part_number": n, "offset": (n - 1) * 400, "length": 400}
            for n in (1, 2, 3)]
    _mk_job("inj_live", "dup-user-1", "scan.tif", 1200, state=ist.UPLOADING,
            upload_id="up-1", object_key="incoming/u/inj_live/x", plan=plan)
    fake, calls = _list_parts_recorder([
        {"part_number": 1, "etag": '"aa"', "size": 400,
         "last_modified": None},
        {"part_number": 3, "etag": '"cc"', "size": 400,
         "last_modified": None},
    ])
    monkeypatch.setattr(cos_client, "list_parts", fake)
    r = user_client.get("/api/ingestions/inj_live/parts")
    assert r.status_code == 200
    body = r.get_json()
    assert body["job_id"] == "inj_live"
    assert body["state"] == "uploading"
    assert body["total_parts"] == 3
    assert body["uploaded_parts"] == 2
    assert body["plan"] == [{"part_number": n, "length": 400}
                            for n in (1, 2, 3)]
    assert body["parts"] == [
        {"part_number": 1, "size": 400, "etag": '"aa"'},
        {"part_number": 3, "size": 400, "etag": '"cc"'},
    ]
    assert r.headers["Cache-Control"] == "no-store"
    # 浏览器 8 秒左右放弃：受限 timeout/retry，避免钉住 web 线程
    assert calls[0]["timeout"] == 8
    assert calls[0]["retry"] == 1
    # 对象键绝不返回浏览器
    assert "object_key" not in body
    assert "up-1" not in r.get_data(as_text=True)


def test_parts_endpoint_owner_scoping(user_client, other_client, monkeypatch):
    plan = [{"part_number": 1, "offset": 0, "length": 1000}]
    _mk_job("inj_other", "dup-user-2", "scan.tif", 1000,
            state=ist.UPLOADING, upload_id="up-1",
            object_key="incoming/u/inj_other/x", plan=plan)
    monkeypatch.setattr(cos_client, "list_parts",
                        lambda key, upload_id, **kw: [])
    assert other_client.get("/api/ingestions/inj_other/parts").status_code == 200
    assert user_client.get("/api/ingestions/inj_other/parts").status_code == 403


def test_parts_endpoint_rejects_non_resumable_state(user_client):
    _mk_job("inj_wait", "dup-user-1", "scan.tif", 1000, state=ist.WAITING)
    r = user_client.get("/api/ingestions/inj_wait/parts")
    assert r.status_code == 409
    assert r.get_json()["code"] == "ingestion_state_conflict"


def test_parts_endpoint_plan_not_ready(user_client):
    _mk_job("inj_live", "dup-user-1", "scan.tif", 1000, state=ist.UPLOADING,
            upload_id="up-1", object_key="incoming/u/inj_live/x", plan=None)
    r = user_client.get("/api/ingestions/inj_live/parts")
    assert r.status_code == 409
    assert r.get_json()["code"] == "plan_not_ready"


def test_parts_endpoint_list_parts_failure_is_502(user_client, monkeypatch):
    plan = [{"part_number": 1, "offset": 0, "length": 1000}]
    _mk_job("inj_live", "dup-user-1", "scan.tif", 1000, state=ist.UPLOADING,
            upload_id="up-1", object_key="incoming/u/inj_live/x", plan=plan)

    def boom(key, upload_id, **kw):
        raise cos_client.CosClientError("HTTP 404 NoSuchUpload")

    monkeypatch.setattr(cos_client, "list_parts", boom)
    r = user_client.get("/api/ingestions/inj_live/parts")
    assert r.status_code == 502
    assert r.get_json()["code"] == "list_parts_failed"
    assert "NoSuchUpload" not in r.get_data(as_text=True)
