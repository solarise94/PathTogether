# -*- coding: utf-8 -*-
"""检查点 A 排空兼容层测试（U4；docs/cos-only-upload-agent-plan-20260928.md §5）。

覆盖：
- full 模式（默认）：V1/V2 照常创建（回归不受影响）；
- drain 模式：V1（无持久任务号）与 V2 一律 410 upload_migration（新任务只
  走 COS）；V1 请求路径仍先跑 committing 惰性恢复扫描；
- 冻结清单资格：清单内任务的状态/续传/提交在 drain 下照常走完；切换后
  新出现（非清单）任务访问 → 410（不接受客户端自述资格）；
- freeze 幂等（重复执行不覆盖既有行）；
- upload_drain 工具：audit（pending 非异常、staging 异常 exit 3）/ report
  （未收口 no-go exit 3；收口后 go exit 0）。
"""

import importlib.util
import io
import json
from pathlib import Path

import pytest

import app as app_mod
import upload_task_store
from _pt_helpers import csrf_client

_REPO = Path(__file__).resolve().parent.parent


@pytest.fixture()
def owner_client(monkeypatch):
    app_mod.app.config["TESTING"] = True

    def mkuser(user_id, role):
        import pg_store
        conn = pg_store.connect()
        try:
            with pg_store.transaction(conn) as c:
                with c.cursor() as cur:
                    cur.execute(
                        "INSERT INTO users (user_id, login_id, display_name, "
                        "role, disabled, created_at, auth_version) VALUES "
                        "(%s, %s, %s, %s, false, now(), 1) "
                        "ON CONFLICT (user_id) DO NOTHING",
                        (user_id, user_id + "@x", user_id, role))
        finally:
            conn.close()

    mkuser("owner-1", "owner")
    client = csrf_client(app_mod.app.test_client())
    with client.session_transaction() as s:
        s["auth_user"] = "owner-1@x"
        s["user_id"] = "owner-1"
        s["role"] = "owner"
        s["auth_version"] = 1
    yield client


def _v2_create(client, name="drain-a.svs", size=64):
    return client.post("/api/uploads", json={
        "filename": name, "declared_size": size})


def _put_chunk(client, upload_id, offset, data):
    import hashlib
    sha = hashlib.sha256(data).hexdigest()
    return client.put(
        "/api/uploads/%s/chunk?offset=%d&sha256=%s" % (upload_id, offset, sha),
        data=data, content_type="application/octet-stream")


def _drain_tool():
    spec = importlib.util.spec_from_file_location(
        "upload_drain_tool", _REPO / "scripts" / "upload_drain.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# --------------------------------------------------------------------------- #
# 模式门禁
# --------------------------------------------------------------------------- #
def test_full_mode_creation_unchanged(owner_client):
    r = _v2_create(owner_client)
    assert r.status_code in (200, 201)
    r2 = owner_client.post(
        "/api/upload", data={"file": (io.BytesIO(b"x"), "a.svs")},
        content_type="multipart/form-data")
    assert r2.status_code != 410  # full：不拦（内容校验按既有路径）


def test_drain_blocks_creation(owner_client, monkeypatch):
    monkeypatch.setenv("PT_UPLOAD_LEGACY_MODE", "drain")
    r = _v2_create(owner_client)
    assert r.status_code == 410
    assert r.get_json()["code"] == "upload_migration"
    r2 = owner_client.post(
        "/api/upload", data={"file": (io.BytesIO(b"x"), "a.svs")},
        content_type="multipart/form-data")
    assert r2.status_code == 410
    assert r2.get_json()["code"] == "upload_migration"


def test_drain_v1_runs_recovery_scan_first(owner_client, monkeypatch):
    """drain 下 V1 请求先跑 committing 惰性恢复扫描再 410（切换前崩溃任务
    的收口不依赖新 V1 请求继续存在）。"""
    calls = {"n": 0}

    def fake_scan(ident=None, *, now=None):
        calls["n"] += 1
        return []

    monkeypatch.setattr(app_mod, "_upload_legacy_recover_stale", fake_scan)
    monkeypatch.setenv("PT_UPLOAD_LEGACY_MODE", "drain")
    owner_client.post(
        "/api/upload", data={"file": (io.BytesIO(b"x"), "a.svs")},
        content_type="multipart/form-data")
    assert calls["n"] == 1


# --------------------------------------------------------------------------- #
# 冻结清单资格
# --------------------------------------------------------------------------- #
def test_drain_frozen_task_completes(owner_client, monkeypatch):
    # full：建任务并传首片
    r = _v2_create(owner_client, size=8)
    upload_id = r.get_json()["upload_id"]
    chunk = b"12345678"
    assert _put_chunk(owner_client, upload_id, 0, chunk).status_code == 200
    # 切换：freeze → drain
    frozen, _already = upload_task_store.freeze_drain_list()
    assert frozen >= 1
    monkeypatch.setenv("PT_UPLOAD_LEGACY_MODE", "drain")
    # 清单内：status / 第二片（无——单片即满）/ commit 照常
    s = owner_client.get("/api/uploads/%s" % upload_id)
    assert s.status_code == 200
    assert s.get_json()["state"] == upload_task_store.STATE_ACTIVE
    c = owner_client.post("/api/uploads/%s/commit" % upload_id)
    # 内容校验失败（dummy 字节非切片）按既有确定性失败收口——不是 410
    assert c.status_code != 410
    task = upload_task_store.get_task(upload_id)
    assert task["state"] in (upload_task_store.STATE_FAILED,
                             upload_task_store.STATE_COMMITTED,
                            upload_task_store.STATE_ACTIVE)


def test_drain_unfrozen_task_410(owner_client, monkeypatch):
    # 切换前任务 A 存在并冻结
    ra = _v2_create(owner_client, name="drain-a.svs")
    upload_a = ra.get_json()["upload_id"]
    upload_task_store.freeze_drain_list()
    monkeypatch.setenv("PT_UPLOAD_LEGACY_MODE", "drain")
    # 切换后「新出现」的任务 B（drain 下 HTTP 无法创建——直插行模拟伪造/
    # 异常路径出现的行）
    rb = upload_task_store.create_task(
        "owner-1", "after-switch.svs", "after-switch.svs", 16,
        upload_task_store.UPLOAD_CHUNK_SIZE)
    upload_b = rb["upload_id"]
    s = owner_client.get("/api/uploads/%s" % upload_b)
    assert s.status_code == 410
    assert s.get_json()["code"] == "upload_migration"
    # 清单内 A 不受影响
    assert owner_client.get("/api/uploads/%s" % upload_a).status_code == 200
    # 清理：B 收尾（取消走 store，绕过 HTTP 门禁）
    upload_task_store.cancel_task(upload_b)


def test_freeze_idempotent(owner_client):
    ra = _v2_create(owner_client, name="idem-a.svs")
    upload_a = ra.get_json()["upload_id"]
    frozen1, already1 = upload_task_store.freeze_drain_list()
    frozen2, already2 = upload_task_store.freeze_drain_list()
    assert frozen1 == 1 and already1 == 0
    assert frozen2 == 0 and already2 == 1  # 重复执行不覆盖既有行
    import pg_store
    conn = pg_store.connect()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT state_at_freeze FROM upload_drain_freeze "
                        "WHERE upload_id=%s", (upload_a,))
            assert cur.fetchone() is not None
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# 排空核验工具（audit / report）
# --------------------------------------------------------------------------- #
def test_report_no_go_then_go(owner_client, monkeypatch, tmp_path):
    tool = _drain_tool()
    r = _v2_create(owner_client, name="report-a.svs")
    upload_id = r.get_json()["upload_id"]

    # audit：在途任务是 pending（非异常）→ exit 0
    assert tool.main(["audit", "--upload-dir", str(tmp_path)]) == 0
    # report：未收口 → no-go
    assert tool.main(["report", "--upload-dir", str(tmp_path)]) == 3
    out_json = tmp_path / "report.json"
    assert tool.main(["report", "--upload-dir", str(tmp_path),
                      "--json", str(out_json)]) == 3
    data = json.loads(out_json.read_text(encoding="utf-8"))
    assert any(k.startswith("old_task_pending") for k in data["pending"]) or \
        data["pending"]

    # 取消收口（清理确认后释放）→ go
    cancel = owner_client.delete("/api/uploads/%s" % upload_id)
    assert cancel.status_code in (200, 202)
    assert tool.main(["report", "--upload-dir", str(tmp_path)]) == 0


def test_report_anomaly_staging_and_ledger(owner_client, tmp_path):
    tool = _drain_tool()
    # 无法解释暂存目录 → audit/report 均 exit 3
    orphan = tmp_path / ".staging" / "upt_orphan0001"
    orphan.mkdir(parents=True)
    (orphan / "data.svs").write_bytes(b"x")
    assert tool.main(["audit", "--upload-dir", str(tmp_path)]) == 3
    assert tool.main(["report", "--upload-dir", str(tmp_path)]) == 3
    orphan.rmtree() if hasattr(orphan, "rmtree") else None
    import shutil
    shutil.rmtree(tmp_path / ".staging", ignore_errors=True)
    assert tool.main(["report", "--upload-dir", str(tmp_path)]) == 0
