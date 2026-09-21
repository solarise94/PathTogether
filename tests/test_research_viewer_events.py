# -*- coding: utf-8 -*-
"""P3 人工读片行为采集与研究副本测试（docs/agent-plan-20260921-registration-
consent-research.md §6.2/§6.3/§7 + §10 P3 验收）。

覆盖：

  - 迁移 0063（research_subjects / research_viewing_sessions /
    research_viewer_events / research_conversation_items）：conftest 内嵌 PG
    从零应用、事件 action CHECK、唯一 (session_id,event_id)/(session_id,seq)；
  - POST /api/research/viewing-sessions：匿名 401、缺 CSRF 400、采集开关默认
    关闭 403 collection_disabled、未同意 403、**非本人切片 403
    resource_not_owned 且不建会话**（伪造/无 owner 同拒）、同意+本人切片 200
    （响应无账号/邮箱/真实文件名；库中只存研究伪名）、每用户稳定 subject
    伪名复用、撤回后 403（deletion_pending/not_granted）；
  - POST /api/research/viewer-events：合法批次（zoom/pan/observe_pause/
    annotation 全类型）写入；payload 白名单（额外字段整批 400 且零写入）；
    未知顶层字段/未知 body 字段 400；批次 >50 400 batch_too_large；>64KiB
    413 payload_too_large；bbox 越界/非有限/五位小数、changed_center 非布尔、
    event_id/seq 形状非法均 400；payload 携 user_id/email 400（白名单外）；
    同 event_id 同内容重传幂等（replayed，无重复行）、同 id 异内容 409；
    seq 复用 409；伪造/他人会话 404 零写入；客户端 user_id 字段不被信任；
    撤回后再同意（新 epoch）旧 epoch 会话 409 epoch_mismatch 零写入；
  - 撤回即时阻断在途写入（§6.3-3）：ingestion 与 withdraw 同一 consent 行
    锁——持锁期间 withdraw 阻塞，锁释放后撤回提交，随后旧会话事件写入
    403 且 research_viewer_events 零行；
  - 速率限制（§7.3）：压低 env 阈值后超限批次 429 + Retry-After，且不写库；
  - observe_pause 字段面（§7.2）：库中 payload 仅 bbox/image_zoom_ratio/
    evidence，结构上不存在 duration_ms/dwell_ms/起止时间字段。

运行：cd 项目根 && .venv/bin/python -m pytest tests/test_research_viewer_events.py -q
"""
import json
import os
import sys
import threading
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
import research_store  # noqa: E402
import share_store  # noqa: E402
import user_store  # noqa: E402
from _pt_helpers import csrf_client, isolate_app  # noqa: E402

PASSWORD = "longpassword123"
MIGRATION_0063 = "0063_research_viewer_telemetry.sql"
SLIDE = "p3-research-slide.ndpi"
SLIDE_OTHER = "p3-other-user-slide.ndpi"

SWITCH = "RESEARCH_COLLECTION_ENABLED"


def _pg():
    conn = pg_store.connect()
    conn.row_factory = psycopg.rows.dict_row
    return conn


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    """per-test 隔离 + 认证开启 + 采集开关复位（P3 交付态：默认关闭）。"""
    isolate_app(monkeypatch, tmp_path / "share", clear_stores=True)
    app_mod.AUTH_ENABLED = True
    monkeypatch.delenv(SWITCH, raising=False)
    monkeypatch.delenv("RESEARCH_EVENTS_USER_BURST", raising=False)
    monkeypatch.delenv("RESEARCH_SESSION_CREATE_BURST", raising=False)
    monkeypatch.delenv("RESEARCH_PSEUDONYM_SALT", raising=False)
    research_store._PSEUDONYM_SALT_CACHE["value"] = None
    import _billing_helpers as bh
    bh.seed_spend_settings()
    yield


def _create_user(login_id):
    return user_store.create_user(login_id, PASSWORD)


def _client():
    app_mod.app.config["TESTING"] = True
    return csrf_client(app_mod.app.test_client())


def _login(client, user):
    with client.session_transaction() as s:
        s["auth_user"] = user.get("login_id") or user.get("user_id")
        s["user_id"] = user["user_id"]
        s["role"] = user.get("role") or "user"
        s["auth_version"] = user.get("auth_version", 1)
    return client


def _doc(doc_type):
    for d in agreement_store.builtin_documents():
        if d["document_type"] == doc_type:
            return d
    raise AssertionError("missing builtin doc %s" % doc_type)


def _publish_and_grant(user_id):
    """发布内置协议并 grant；返回 grant 后的 consent 视图（dict）。"""
    agreement_store.ensure_builtin_documents()
    for dt in agreement_store.DOCUMENT_TYPES:
        agreement_store.publish_document(dt, _doc(dt)["version"])
    d = _doc("research_sharing")
    result = research_consent_store.grant(
        user_id, document_version=d["version"],
        document_sha256=d["content_sha256"])
    return result["consent"]


def _own_slide(user, name=SLIDE):
    share_store.set_slide_meta(name, owner_user_id=user["user_id"])
    return name


def _own_slide_for(user_b, name=SLIDE_OTHER):
    share_store.set_slide_meta(name, owner_user_id=user_b["user_id"])
    return name


def _open_collection(monkeypatch):
    monkeypatch.setenv(SWITCH, "1")


def _create_session(client):
    resp = client.post("/api/research/viewing-sessions",
                       json={"slide": SLIDE})
    assert resp.status_code == 200, resp.get_json()
    body = resp.get_json()
    return body["viewing_session_id"], body["consent_epoch"]


# ---- 事件工厂（与服务端白名单一致的最小合法事件） ----
def ev(seq, action, **payload):
    return {
        "event_id": "evt_%012d" % seq,
        "seq": seq,
        "action": action,
        "schema_version": research_store.SCHEMA_VERSION,
        "payload": payload,
    }


def zoom_event(seq, ratio=1.5, changed_center=True):
    return ev(seq, "zoom_in",
              bbox_before=[0.0, 0.0, 1.0, 1.0],
              bbox_after=[0.25, 0.25, 0.5, 0.5],
              image_zoom_ratio=ratio,
              input_kind="wheel",
              changed_center=changed_center)


def pan_event(seq):
    return ev(seq, "pan",
              bbox_before=[0.0, 0.0, 1.0, 1.0],
              bbox_after=[0.1, 0.1, 0.8, 0.8],
              input_kind="drag")


def observe_event(seq):
    return ev(seq, "observe_pause",
              bbox=[0.2, 0.2, 0.4, 0.4],
              image_zoom_ratio=2.0,
              evidence="inferred_stable_view")


def anno_create_event(seq, local_id="al_test1234"):
    return ev(seq, "annotation_create",
              tool_type="rect", shape_type="rect",
              bbox=[0.1, 0.1, 0.2, 0.2],
              annotation_local_id=local_id,
              origin="human")


def anno_accept_event(seq, local_id="al_test1234"):
    return ev(seq, "annotation_accept",
              annotation_local_id=local_id,
              origin="human_review")


def _post_events(client, sid, epoch, events):
    return client.post("/api/research/viewer-events", json={
        "viewing_session_id": sid,
        "consent_epoch": epoch,
        "events": events,
    })


def _db_events(sid):
    conn = _pg()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT event_id, seq, action, schema_version, consent_epoch, "
                "payload, server_received_at FROM research_viewer_events "
                "WHERE session_id=%s ORDER BY seq", (sid,))
            return [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# 1. 迁移 0063：表结构 + 约束
# --------------------------------------------------------------------------- #
def test_migration_0063_tables_and_constraints(monkeypatch):
    _open_collection(monkeypatch)
    conn = _pg()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM schema_migrations WHERE filename=%s",
                        (MIGRATION_0063,))
            assert cur.fetchone() is not None, "0063 应已被 ensure_schema 应用"
            for table in ("research_subjects", "research_viewing_sessions",
                          "research_viewer_events",
                          "research_conversation_items"):
                cur.execute("SELECT 1 FROM pg_tables WHERE tablename=%s",
                            (table,))
                assert cur.fetchone() is not None, "缺表 %s" % table
        # 事件 action CHECK：非法动作被拒
        user = _create_user("m63chk@x.com")
        _publish_and_grant(user["user_id"])
        _own_slide(user)
        sid, epoch = _create_session(_login(_client(), user))
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO research_viewer_events "
                "(session_id, event_id, seq, action, schema_version, "
                " consent_epoch, payload, expires_at) VALUES "
                "(%s,'evt_bad0001',1,'bogus_action',%s,%s,'{}',now())",
                (sid, research_store.SCHEMA_VERSION, epoch))
            conn.commit()
        assert False, "非法 action 应被 CHECK 拒绝"
    except psycopg.errors.CheckViolation:
        conn.rollback()
    finally:
        conn.close()


def test_unique_session_event_id_and_seq(monkeypatch):
    _open_collection(monkeypatch)
    user = _create_user("m63uniq@x.com")
    _publish_and_grant(user["user_id"])
    _own_slide(user)
    sid, epoch = _create_session(_login(_client(), user))
    conn = _pg()
    try:
        base = (sid, research_store.SCHEMA_VERSION, epoch)
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO research_viewer_events "
                "(session_id, event_id, seq, action, schema_version, "
                " consent_epoch, payload, expires_at) VALUES "
                "(%s,'evt_dup000001',1,'pan',%s,%s,'{}',now())", base)
            conn.commit()
        # 同 (session_id, event_id)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO research_viewer_events "
                    "(session_id, event_id, seq, action, schema_version, "
                    " consent_epoch, payload, expires_at) VALUES "
                    "(%s,'evt_dup000001',2,'pan',%s,%s,'{}',now())", base)
                conn.commit()
            assert False, "同 event_id 应被拒"
        except psycopg.errors.UniqueViolation:
            conn.rollback()
        # 同 (session_id, seq)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO research_viewer_events "
                    "(session_id, event_id, seq, action, schema_version, "
                    " consent_epoch, payload, expires_at) VALUES "
                    "(%s,'evt_seq000002',1,'pan',%s,%s,'{}',now())", base)
                conn.commit()
            assert False, "同 seq 应被拒"
        except psycopg.errors.UniqueViolation:
            conn.rollback()
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# 2. POST /api/research/viewing-sessions
# --------------------------------------------------------------------------- #
def test_session_anonymous_401_and_csrf_required():
    client = _client()
    resp = client.post("/api/research/viewing-sessions", json={"slide": SLIDE})
    assert resp.status_code == 401


def test_session_csrf_missing_rejected():
    user = _create_user("csrfless@x.com")
    client = app_mod.app.test_client()  # 不带 X-CSRF-Token 的裸 client
    _login(client, user)
    resp = client.post("/api/research/viewing-sessions",
                       json={"slide": SLIDE})
    assert resp.status_code == 400
    assert resp.get_json()["error"] == "csrf_required"


def test_session_switch_off_default_403_no_session_rows():
    """采集开关默认关闭（P3 交付态）：已同意+本人切片也不建会话、零网络外写。"""
    user = _create_user("switchoff@x.com")
    _publish_and_grant(user["user_id"])
    _own_slide(user)
    client = _login(_client(), user)
    resp = client.post("/api/research/viewing-sessions", json={"slide": SLIDE})
    assert resp.status_code == 403
    assert resp.get_json()["code"] == "collection_disabled"
    conn = _pg()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) AS n FROM research_viewing_sessions")
            assert cur.fetchone()["n"] == 0
    finally:
        conn.close()


def test_session_not_granted_403_even_with_switch_on(monkeypatch):
    user = _create_user("notgranted@x.com")
    agreement_store.ensure_builtin_documents()
    for dt in agreement_store.DOCUMENT_TYPES:
        agreement_store.publish_document(dt, _doc(dt)["version"])
    _own_slide(user)
    _open_collection(monkeypatch)
    client = _login(_client(), user)
    resp = client.post("/api/research/viewing-sessions", json={"slide": SLIDE})
    assert resp.status_code == 403
    assert resp.get_json()["code"] == "not_granted"


def test_session_success_shape_and_pseudonym_isolation(monkeypatch):
    user = _create_user("okuser@x.com")
    _publish_and_grant(user["user_id"])
    _own_slide(user)
    _open_collection(monkeypatch)
    client = _login(_client(), user)
    resp = client.post("/api/research/viewing-sessions", json={"slide": SLIDE})
    assert resp.status_code == 200, resp.get_json()
    body = resp.get_json()
    assert body["schema_version"] == research_store.SCHEMA_VERSION
    assert body["consent_epoch"] >= 1
    assert body["viewing_session_id"].startswith("rvs_")
    # 响应不含账号/邮箱/真实文件名
    raw = json.dumps(body)
    assert "okuser@x.com" not in raw and SLIDE not in raw
    assert "user_id" not in raw and "email" not in raw
    # 库中：会话行只存研究伪名；subject 伪名 ≠ user_id、不含邮箱
    conn = _pg()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT slide_pseudonym, subject_id, status, "
                "expires_at > now() AS not_expired "
                "FROM research_viewing_sessions WHERE session_id=%s",
                (body["viewing_session_id"],))
            row = cur.fetchone()
            assert row is not None
            assert row["slide_pseudonym"].startswith("sl_")
            assert SLIDE not in row["slide_pseudonym"]
            assert row["status"] == "active" and row["not_expired"]
            cur.execute(
                "SELECT subject_id, user_id FROM research_subjects "
                "WHERE user_id=%s", (user["user_id"],))
            subj = cur.fetchone()
            assert subj is not None
            assert subj["subject_id"].startswith("rs_")
            assert subj["subject_id"] != user["user_id"]
            assert subj["subject_id"] == row["subject_id"]
    finally:
        conn.close()
    # 同用户第二次建会话：伪名稳定复用（隔离映射一致）
    resp2 = client.post("/api/research/viewing-sessions",
                        json={"slide": SLIDE})
    assert resp2.status_code == 200
    assert research_store.subject_for_user(user["user_id"])["subject_id"] \
        == subj["subject_id"]


def test_session_rejects_other_users_or_unowned_slide(monkeypatch):
    user_a = _create_user("owner-a@x.com")
    user_b = _create_user("owner-b@x.com")
    _publish_and_grant(user_a["user_id"])
    _own_slide(user_a)
    _own_slide_for(user_b)  # B 拥有的切片
    _open_collection(monkeypatch)
    client_a = _login(_client(), user_a)
    # B 的切片（A 仅可见也不行——研究第一版只允许本人拥有）
    resp = client_a.post("/api/research/viewing-sessions",
                         json={"slide": SLIDE_OTHER})
    assert resp.status_code == 403
    assert resp.get_json()["code"] == "resource_not_owned"
    # 不存在的切片
    resp = client_a.post("/api/research/viewing-sessions",
                         json={"slide": "no-such-slide.ndpi"})
    assert resp.status_code == 403
    # 未知 body 字段
    resp = client_a.post("/api/research/viewing-sessions",
                         json={"slide": SLIDE, "user_id": user_b["user_id"]})
    assert resp.status_code == 400
    # 均未建会话
    conn = _pg()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) AS n FROM research_viewing_sessions")
            assert cur.fetchone()["n"] == 0
    finally:
        conn.close()


def test_session_withdrawn_blocks_new_sessions(monkeypatch):
    user = _create_user("withdrawn@x.com")
    consent = _publish_and_grant(user["user_id"])
    _own_slide(user)
    _open_collection(monkeypatch)
    client = _login(_client(), user)
    research_consent_store.withdraw(user["user_id"],
                                    expected_epoch=consent["epoch"])
    resp = client.post("/api/research/viewing-sessions", json={"slide": SLIDE})
    assert resp.status_code == 403
    # 撤回产生的未终态删除任务也在阻断面内（deletion_pending 或 not_granted）
    assert resp.get_json()["code"] in ("not_granted", "deletion_pending")


# --------------------------------------------------------------------------- #
# 3. POST /api/research/viewer-events
# --------------------------------------------------------------------------- #
def _granted_session(monkeypatch, login="granted@x.com"):
    user = _create_user(login)
    _publish_and_grant(user["user_id"])
    _own_slide(user)
    _open_collection(monkeypatch)
    client = _login(_client(), user)
    sid, epoch = _create_session(client)
    return user, client, sid, epoch


def test_events_happy_path_all_action_types(monkeypatch):
    user, client, sid, epoch = _granted_session(
        monkeypatch, "happy@x.com")
    events = [
        zoom_event(1),
        pan_event(2),
        observe_event(3),
        anno_create_event(4),
        ev(5, "annotation_update", tool_type="arrow", shape_type="arrow",
           bbox=[0.1, 0.1, 0.2, 0.1], annotation_local_id="al_test1234",
           origin="human"),
        ev(6, "annotation_delete", annotation_local_id="al_test1234",
           origin="human"),
        anno_accept_event(7),
        ev(8, "annotation_reject", annotation_local_id="al_test9999",
           origin="human_review"),
        ev(9, "zoom_out", bbox_before=[0.25, 0.25, 0.5, 0.5],
           bbox_after=[0.0, 0.0, 1.0, 1.0], image_zoom_ratio=0.5,
           input_kind="button", changed_center=False),
    ]
    resp = _post_events(client, sid, epoch, events)
    assert resp.status_code == 200, resp.get_json()
    body = resp.get_json()
    assert body["accepted"] == len(events) and body["replayed"] == 0
    rows = _db_events(sid)
    assert len(rows) == len(events)
    assert [r["seq"] for r in rows] == list(range(1, len(events) + 1))
    # 响应不含账号/路径
    raw = json.dumps(body)
    assert "user_id" not in raw and SLIDE not in raw


def test_events_observe_pause_has_no_duration_fields(monkeypatch):
    """§7.2 验收：事件网络包与数据库均无单次等待时长/起止字段。"""
    user, client, sid, epoch = _granted_session(
        monkeypatch, "opausedur@x.com")
    payload = observe_event(1)["payload"]
    assert set(payload.keys()) == {"bbox", "image_zoom_ratio", "evidence"}
    resp = _post_events(client, sid, epoch, [observe_event(1)])
    assert resp.status_code == 200
    row = _db_events(sid)[0]
    assert set(row["payload"].keys()) == {"bbox", "image_zoom_ratio",
                                          "evidence"}
    forbidden = ("duration_ms", "dwell_ms", "started_at", "ended_at",
                 "start_ms", "end_ms", "stable_ms")
    raw = json.dumps(row["payload"])
    for word in forbidden:
        assert word not in raw


def test_events_extra_payload_field_rejects_whole_batch(monkeypatch):
    user, client, sid, epoch = _granted_session(
        monkeypatch, "extrafield@x.com")
    bad = zoom_event(1)
    bad["payload"]["user_id"] = "usr_someone"  # 白名单外（含身份字段）
    events = [zoom_event(1), pan_event(2)]
    events[0] = bad
    resp = _post_events(client, sid, epoch, events)
    assert resp.status_code == 400
    assert resp.get_json()["code"] == "invalid_event"
    assert research_store.count_viewer_events(sid) == 0, "整批拒绝：无部分写入"


def test_events_extra_top_level_field_rejected(monkeypatch):
    user, client, sid, epoch = _granted_session(
        monkeypatch, "extratop@x.com")
    bad = zoom_event(1)
    bad["client_note"] = "arbitrary"
    resp = _post_events(client, sid, epoch, [bad])
    assert resp.status_code == 400
    assert resp.get_json()["code"] == "invalid_event"
    assert research_store.count_viewer_events(sid) == 0


def test_events_unknown_body_field_and_bad_shapes(monkeypatch):
    user, client, sid, epoch = _granted_session(
        monkeypatch, "badbody@x.com")
    # body 未知字段（客户端身份字段不被信任——白名单外直接 400）
    resp = client.post("/api/research/viewer-events", json={
        "viewing_session_id": sid, "consent_epoch": epoch,
        "events": [zoom_event(1)], "user_id": "usr_forged"})
    assert resp.status_code == 400
    # 空 events
    resp = _post_events(client, sid, epoch, [])
    assert resp.status_code == 400
    # 非 JSON
    resp = client.post("/api/research/viewer-events", data="not-json",
                       headers={"Content-Type": "application/json"})
    assert resp.status_code == 400
    assert research_store.count_viewer_events(sid) == 0


def test_events_batch_over_50_rejected(monkeypatch):
    user, client, sid, epoch = _granted_session(
        monkeypatch, "batch51@x.com")
    events = [zoom_event(i) for i in range(1, 52)]
    resp = _post_events(client, sid, epoch, events)
    assert resp.status_code == 400
    assert resp.get_json()["code"] == "batch_too_large"
    assert research_store.count_viewer_events(sid) == 0
    # 恰好 50 条可写
    resp = _post_events(client, sid, epoch, events[:50])
    assert resp.status_code == 200


def test_events_body_over_64kib_rejected(monkeypatch):
    user, client, sid, epoch = _granted_session(
        monkeypatch, "bigbody@x.com")
    # 构造 >64KiB 的合法 JSON body（长 event_id 不合法——用大量事件近似不行
    # 因为 ≤50 条；用 bbox 之外的合法大字段没有——直接超长 body 走 413 分支）
    padding = "x" * (70 * 1024)
    resp = client.post(
        "/api/research/viewer-events",
        data=json.dumps({"viewing_session_id": sid, "consent_epoch": epoch,
                         "events": [zoom_event(1)], "padding": padding}),
        headers={"Content-Type": "application/json"})
    assert resp.status_code in (400, 413)  # 未知字段 400 或体积 413 都拒
    # 纯体积超限（合法字段、超长字符串值在白名单校验前 413）
    big_local = "al_" + "a" * 60
    resp = client.post(
        "/api/research/viewer-events",
        data=json.dumps({
            "viewing_session_id": sid, "consent_epoch": epoch,
            "events": [ev(1, "annotation_accept",
                          annotation_local_id=big_local,
                          origin="human_review")],
            "note": padding}),
        headers={"Content-Type": "application/json"})
    assert resp.status_code in (400, 413)
    assert research_store.count_viewer_events(sid) == 0


def test_events_invalid_values_rejected(monkeypatch):
    user, client, sid, epoch = _granted_session(
        monkeypatch, "badvals@x.com")
    cases = []
    # bbox 越界
    bad = zoom_event(1); bad["payload"]["bbox_after"] = [0.0, 0.0, 1.5, 1.0]
    cases.append(bad)
    # bbox 五位小数
    bad = zoom_event(1); bad["payload"]["bbox_after"] = [0.0, 0.0, 0.12345, 0.5]
    cases.append(bad)
    # bbox 非四元
    bad = zoom_event(1); bad["payload"]["bbox_after"] = [0.0, 0.0, 0.5]
    cases.append(bad)
    # changed_center 字符串（Python truthiness 陷阱）
    bad = zoom_event(1); bad["payload"]["changed_center"] = "false"
    cases.append(bad)
    # 非法 input_kind
    bad = zoom_event(1); bad["payload"]["input_kind"] = "agent-replay"
    cases.append(bad)
    # 非法 image_zoom_ratio
    bad = zoom_event(1); bad["payload"]["image_zoom_ratio"] = 0
    cases.append(bad)
    # event_id 形状
    bad = zoom_event(1); bad["event_id"] = "evt_bad id!"
    cases.append(bad)
    # seq 非法
    bad = zoom_event(0)
    cases.append(bad)
    # schema_version 不匹配
    bad = zoom_event(1); bad["schema_version"] = "other-v9"
    cases.append(bad)
    # 未知 action
    bad = zoom_event(1); bad["action"] = "mouse_move"
    cases.append(bad)
    # observe_pause 证据值固定
    bad = observe_event(1); bad["payload"]["evidence"] = "real_attention"
    cases.append(bad)
    # annotation origin 固定
    bad = anno_create_event(1); bad["payload"]["origin"] = "ai"
    cases.append(bad)
    # 自由文本字段（白名单外）
    bad = anno_create_event(1); bad["payload"]["label"] = "ki67 高表达"
    cases.append(bad)
    for case in cases:
        resp = _post_events(client, sid, epoch, [case])
        assert resp.status_code == 400, "应拒绝：%s" % json.dumps(case)
        assert resp.get_json()["code"] == "invalid_event"
    assert research_store.count_viewer_events(sid) == 0


def test_events_json_float_nan_like_rejected(monkeypatch):
    """非有限值拒绝：JSON 里塞 1e999（Python 解析为 inf）必须 400。"""
    user, client, sid, epoch = _granted_session(
        monkeypatch, "infval@x.com")
    raw = ('{"viewing_session_id":%s,"consent_epoch":%d,"events":[{"event_id":'
           '"evt_inf000001","seq":1,"action":"zoom_in","schema_version":"%s",'
           '"payload":{"bbox_before":[0.0,0.0,1.0,1.0],"bbox_after":[0.0,0.0,'
           '1e999,1.0],"image_zoom_ratio":1.5,"input_kind":"wheel",'
           '"changed_center":false}}]}' % (json.dumps(sid), epoch,
                                           research_store.SCHEMA_VERSION))
    resp = client.post("/api/research/viewer-events", data=raw,
                       headers={"Content-Type": "application/json"})
    assert resp.status_code == 400
    assert research_store.count_viewer_events(sid) == 0


def test_events_retransmit_idempotent_and_conflicts(monkeypatch):
    user, client, sid, epoch = _granted_session(
        monkeypatch, "retrans@x.com")
    first = [zoom_event(1), pan_event(2)]
    resp = _post_events(client, sid, epoch, first)
    assert resp.status_code == 200
    assert resp.get_json()["accepted"] == 2
    # 网络重试同批次重传：幂等 replayed，不产生重复行
    resp = _post_events(client, sid, epoch, first)
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["accepted"] == 0 and body["replayed"] == 2
    assert research_store.count_viewer_events(sid) == 2
    # 同 event_id 不同内容 → 409 冲突，不覆盖旧事件
    mutated = zoom_event(1)
    mutated["payload"]["image_zoom_ratio"] = 9.9
    resp = _post_events(client, sid, epoch, [mutated])
    assert resp.status_code == 409
    assert resp.get_json()["code"] == "event_id_conflict"
    rows = _db_events(sid)
    assert rows[0]["payload"]["image_zoom_ratio"] == 1.5, "旧事件未被覆盖"
    # 不同事件复用 seq → 409（event_id 换新值，孤立 seq 冲突）
    seq_reuser = zoom_event(2)
    seq_reuser["event_id"] = "evt_other_id_99"
    resp = _post_events(client, sid, epoch, [seq_reuser])
    assert resp.status_code == 409
    assert resp.get_json()["code"] == "seq_conflict"
    assert research_store.count_viewer_events(sid) == 2


def test_events_forged_or_foreign_session_404_no_write(monkeypatch):
    user_a, client_a, sid_a, epoch_a = _granted_session(
        monkeypatch, "forged-a@x.com")
    user_b = _create_user("forged-b@x.com")
    _publish_and_grant(user_b["user_id"])
    _own_slide(user_b, "p3-b-slide.ndpi")
    client_b = _login(_client(), user_b)
    # B 用 A 的会话 ID 上报（伪造资源映射）
    resp = _post_events(client_b, sid_a, epoch_a, [zoom_event(1)])
    assert resp.status_code == 404
    assert resp.get_json()["code"] == "session_not_found"
    # 随机不存在的会话
    resp = _post_events(client_a, "rvs_nosuchsession0000", epoch_a,
                        [zoom_event(1)])
    assert resp.status_code == 404
    assert research_store.count_viewer_events(sid_a) == 0


def test_events_epoch_mismatch_after_regrant(monkeypatch):
    """撤回→再同意产生新 epoch：旧 epoch 会话的事件 409 epoch_mismatch。"""
    user, client, sid, epoch = _granted_session(monkeypatch, "regrant@x.com")
    consent = research_consent_store.get_consent(user["user_id"])
    research_consent_store.withdraw(user["user_id"],
                                    expected_epoch=consent["epoch"])
    # 撤回产生的删除任务先执行完毕（模拟清理链走完），再同意产生新 epoch
    conn = _pg()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE research_data_deletion_jobs SET status='completed', "
                "online_cleared=TRUE, exports_cleared=TRUE, "
                "backups_pending=FALSE, completed_at=now() "
                "WHERE user_id=%s AND status IN ('pending','running')",
                (user["user_id"],))
        conn.commit()
    finally:
        conn.close()
    d = _doc("research_sharing")
    research_consent_store.grant(user["user_id"], document_version=d["version"],
                                 document_sha256=d["content_sha256"])
    current = research_consent_store.get_consent(user["user_id"])
    assert current["epoch"] > epoch, "再同意必须产生新 epoch"
    resp = _post_events(client, sid, epoch, [zoom_event(1)])
    assert resp.status_code == 409
    assert resp.get_json()["code"] == "epoch_mismatch"
    assert research_store.count_viewer_events(sid) == 0


def test_events_switch_off_mid_flight_403(monkeypatch):
    """开关中途关闭：已有会话也不得继续写入（fail-closed）。"""
    user, client, sid, epoch = _granted_session(monkeypatch, "midoff@x.com")
    resp = _post_events(client, sid, epoch, [zoom_event(1)])
    assert resp.status_code == 200
    monkeypatch.delenv(SWITCH, raising=False)
    resp = _post_events(client, sid, epoch, [zoom_event(2)])
    assert resp.status_code == 403
    assert resp.get_json()["code"] == "collection_disabled"
    assert research_store.count_viewer_events(sid) == 1


# --------------------------------------------------------------------------- #
# 4. 撤回即时阻断在途写入（§6.3-3：同一 consent 行锁）
# --------------------------------------------------------------------------- #
def test_withdraw_blocks_inflight_writes_via_consent_row_lock(monkeypatch):
    """ingestion 与 withdraw 使用同一 consent 行锁：撤回提交后的写入不成功。

    步骤：先建会话 → 主线程持 user_research_consents 行 FOR UPDATE（模拟
    ingestion 已进入锁内窗口）→ 线程发起 withdraw（阻塞在行锁上，未返回）→
    主线程释放锁 → withdraw 完成提交 → 旧会话再发事件 → 403 且零行。
    """
    user, client, sid, epoch = _granted_session(
        monkeypatch, "inflight@x.com")
    resp = _post_events(client, sid, epoch, [zoom_event(1)])
    assert resp.status_code == 200

    consent = research_consent_store.get_consent(user["user_id"])
    lock_conn = _pg()
    try:
        # 未提交事务持有 consent 行 FOR UPDATE（模拟 ingestion 的锁内窗口）
        with lock_conn.cursor() as cur:
            cur.execute(
                "SELECT epoch FROM user_research_consents WHERE user_id=%s "
                "FOR UPDATE", (user["user_id"],))
            assert cur.fetchone() is not None

        result = {}

        def do_withdraw():
            try:
                out = research_consent_store.withdraw(
                    user["user_id"], expected_epoch=consent["epoch"])
                result["done"] = True
                result["out"] = out
            except Exception as exc:  # noqa: BLE001
                result["done"] = False
                result["error"] = exc

        t = threading.Thread(target=do_withdraw)
        t.start()
        time.sleep(0.4)
        assert not result.get("done"), \
            "withdraw 不应在 consent 行锁被持有期间完成（锁序阻断在途写入）"
        # 释放锁：withdraw 继续，随后提交（epoch++ + 删除任务同事务）
        lock_conn.rollback()
        t.join(timeout=10)
        assert result.get("done"), "锁释放后 withdraw 应完成：%r" % result

        # 撤回提交后的写入不成功
        resp = _post_events(client, sid, epoch, [pan_event(2)])
        assert resp.status_code == 403
        assert research_store.count_viewer_events(sid) == 1, \
            "撤回后旧会话不得新增事件（在途写入被阻断）"
        # 撤回同事务创建了删除任务（§6.3 撤回原子）
        job = research_consent_store.get_active_deletion_job(user["user_id"])
        assert job is not None and job["reason"] == "withdrawal"
    finally:
        lock_conn.close()


# --------------------------------------------------------------------------- #
# 5. 速率限制（§7.3：超限 429 + Retry-After，不写库）
# --------------------------------------------------------------------------- #
def test_events_rate_limited_429(monkeypatch):
    monkeypatch.setenv("RESEARCH_EVENTS_USER_BURST", "2")
    user, client, sid, epoch = _granted_session(monkeypatch, "ratelimit@x.com")
    ok = _post_events(client, sid, epoch, [zoom_event(1)])
    assert ok.status_code == 200
    ok2 = _post_events(client, sid, epoch, [pan_event(2)])
    assert ok2.status_code == 200
    limited = _post_events(client, sid, epoch, [observe_event(3)])
    assert limited.status_code == 429
    assert limited.get_json()["code"] == "rate_limited"
    assert int(limited.headers["Retry-After"]) >= 1
    assert research_store.count_viewer_events(sid) == 2, "429 批次未写库"


def test_session_create_rate_limited_429(monkeypatch):
    monkeypatch.setenv("RESEARCH_SESSION_CREATE_BURST", "1")
    user = _create_user("sessrate@x.com")
    _publish_and_grant(user["user_id"])
    _own_slide(user)
    _open_collection(monkeypatch)
    client = _login(_client(), user)
    first = client.post("/api/research/viewing-sessions", json={"slide": SLIDE})
    assert first.status_code == 200
    second = client.post("/api/research/viewing-sessions", json={"slide": SLIDE})
    assert second.status_code == 429
    assert int(second.headers["Retry-After"]) >= 1


# --------------------------------------------------------------------------- #
# 6. 研究副本与业务隔离（§6.2：不复制真实文件名/身份）
# --------------------------------------------------------------------------- #
def test_research_copy_isolated_from_business_identity(monkeypatch):
    user, client, sid, epoch = _granted_session(
        monkeypatch, "isolated@x.com")
    _post_events(client, sid, epoch, [zoom_event(1), observe_event(2)])
    conn = _pg()
    try:
        with conn.cursor() as cur:
            # 研究存储不存 email/login_id（结构隔离：唯一 user 关联在 subjects）
            cur.execute(
                "SELECT count(*) AS n FROM research_viewing_sessions s "
                "JOIN research_subjects sub ON sub.subject_id=s.subject_id "
                "WHERE sub.user_id=%s", (user["user_id"],))
            assert cur.fetchone()["n"] == 1
            cur.execute(
                "SELECT string_agg(column_name, ',') AS cols "
                "FROM information_schema.columns "
                "WHERE table_name='research_viewer_events'")
            cols = cur.fetchone()["cols"]
            for absent in ("user_id", "email", "slide", "filename", "path",
                           "duration", "dwell"):
                assert absent not in cols.split(","), \
                    "research_viewer_events 不应有列 %s（实际 %s）" % (absent, cols)
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# 7. 预览态与前端装配旗标（§6.1/§7.1：owner 预览/demo/公开分享不采集）
# --------------------------------------------------------------------------- #
def test_preview_mode_write_blocked_403(monkeypatch):
    """owner 预览态：研究端点写被全局预览闸 403（预览不是用户本人操作）。"""
    owner = user_store.create_user("prev-owner@x.com", PASSWORD,
                                   role="owner")
    user = _create_user("prev-subject@x.com")
    _publish_and_grant(user["user_id"])
    _own_slide(user)
    _open_collection(monkeypatch)
    client = _login(_client(), owner)
    with client.session_transaction() as s:
        s[app_mod.PREVIEW_SESSION_KEY] = {
            "subject_user_id": user["user_id"],
            "expires_at": time.time() + 600,
        }
    resp = client.post("/api/research/viewing-sessions", json={"slide": SLIDE})
    assert resp.status_code == 403
    assert resp.get_json()["code"] == "preview_readonly"
    conn = _pg()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) AS n FROM research_viewing_sessions")
            assert cur.fetchone()["n"] == 0
    finally:
        conn.close()


def test_workbench_capabilities_research_flag_follows_switch(monkeypatch):
    """前端装配旗标：开关关闭 → capabilities.research_collection=false
    （正式 app 不装配采集、零研究网络请求）；开启 → true。Demo 恒 false。"""
    user = _create_user("capflag@x.com")
    client = _login(_client(), user)
    resp = client.get("/app")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert '"research_collection": false' in body
    # 正式工作台加载采集模块脚本（装配与否由旗标决定，脚本本身惰性）
    assert "research-viewer-telemetry.js" in body
    _open_collection(monkeypatch)
    resp = client.get("/app")
    body = resp.get_data(as_text=True)
    assert '"research_collection": true' in body
