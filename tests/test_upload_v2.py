# -*- coding: utf-8 -*-
"""Upload V2 分片续传后端测试（docs/upload-resumable-fix-plan.md §3/§6 U2）。

覆盖（json 默认 + RUN_PG_TESTS=1 双跑）：
  - 创建任务：预校验（文件名/类型/ZIP-MRXS 引导旧接口/上限/名称冲突）+
    初始化即预占（PG：reserved_bytes 在任何 PUT 之前即等于 declared_size）；
  - PUT 分片：串行 offset 409（offset_mismatch 带当前进度）、重复 PUT 幂等
    （§3.2.1 三分支）、每片哈希校验失败回退、单片越界 413、pwrite 单次落盘；
  - 并发：多线程抢同一 upload_id 的 PUT（行锁/文件锁串行化，一个推进其余
    幂等或 409）；
  - commit 三段式（§3.2.5）：受理→事务外哈希+验证+提升→收口；**验证在提升
    之前**（§2.3 纠正）；整文件复算 + sha256_expected 比对 + 大小校验；
    确定性失败 → failed；未传完 commit → 400（active 保留）；
  - DELETE：active/failed → cancelled（清临时文件 + 释放预占）；committing
    中 → 409（与 commit 竞态）；
  - TTL：PUT 刷新任务 expires_at（§3.2.4）；过期惰性清理临时文件并释放预占；
  - CSRF：裸 client 无 token 的 POST/PUT/DELETE → 400 csrf_required；
  - 权限：他人任务 403（不泄露存在性）；AUTH_ENABLED=False 身份归一不破；
  - PG 专属：reservation 续租（PUT 后 expires_at 后移）、commit 转实占、
    json 后端 role=user 创建 503 fail-closed；
  - 旧 POST /api/upload 并存不受影响。

运行：cd 项目根 && python3 -m pytest tests/test_upload_v2.py -q
（PG 双跑：RUN_PG_TESTS=1 python3 -m pytest tests/test_upload_v2.py -q）
"""
import hashlib
import io
import json as _json
import os
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import _bootstrap  # noqa: E402,F401  # session 目录+openslide stub（conftest 先行）
DATA_DIR = _bootstrap.SHARE_DATA_DIR
UPLOAD_DIR = _bootstrap.UPLOAD_DIR
os.environ["ADMIN_PASSWORD"] = ""
import pytest  # noqa: E402

import share_store  # noqa: E402
import upload_guard  # noqa: E402
import upload_task_store  # noqa: E402
import user_store  # noqa: E402
import app as app_mod  # noqa: E402
from pg_compat import BACKEND  # noqa: E402
from _pt_helpers import csrf_client, install_json_login_limits, isolate_app, clear_upload_dir # noqa: E402


pg_only = pytest.mark.skipif(
    BACKEND != "postgres", reason="配额/续租需 PG（RUN_PG_TESTS=1）")
json_only = pytest.mark.skipif(
    BACKEND != "json", reason="json 后端 fail-closed 行为仅在 json 模式断言")


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    """每用例：独立存储 + 无登录限制 mock + 防护参数复位 + 清空 uploads。"""
    isolate_app(monkeypatch, tmp_path, UPLOAD_DIR, login_limits=True)
    monkeypatch.setattr(upload_task_store, "UPLOAD_TASK_FILE",
                        tmp_path / "upload_tasks.json")
    # 防护/TTL 参数复位（防其它用例污染；水印 0 使本机磁盘不干扰）
    monkeypatch.setattr(upload_guard, "UPLOAD_MAX_REQUEST_BYTES", 10 * 1024 ** 3)
    monkeypatch.setattr(upload_guard, "UPLOAD_RESERVED_FREE_BYTES", 0)
    monkeypatch.setattr(upload_task_store, "UPLOAD_TASK_TTL_SECONDS", 24 * 3600)
    monkeypatch.setattr(upload_task_store, "UPLOAD_CHUNK_MAX_BYTES", 64 * 1024 ** 2)
    monkeypatch.setitem(app_mod.app.config, "MAX_CONTENT_LENGTH", None)
    clear_upload_dir(UPLOAD_DIR)
    yield


def _client(auth=False):
    app_mod.app.config["TESTING"] = True
    app_mod.AUTH_ENABLED = auth
    return csrf_client(app_mod.app.test_client())


def _user_session(client, role="user", login="v2@x.com"):
    u = user_store.create_user(login, "pass1234pass1234", role=role)
    with client.session_transaction() as sess:
        sess["auth_user"] = True
        sess["user_id"] = u["user_id"]
        sess["role"] = role
        sess["auth_version"] = (user_store.get_user(u["user_id"]) or {}).get(
            "auth_version", 1)
    return u["user_id"]


def _create(client, name="a.svs", size=1000, sha=None, **extra):
    body = {"filename": name, "declared_size": size}
    if sha:
        body["sha256_expected"] = sha
    body.update(extra)
    return client.post("/api/uploads", json=body)


def _put(client, upload_id, offset, data, sha=None):
    """PUT 一个分片（sha 缺省按数据现算——模拟正确客户端）。"""
    if sha is None:
        sha = hashlib.sha256(data).hexdigest()
    return client.put("/api/uploads/%s/chunk?offset=%d&sha256=%s"
                      % (upload_id, offset, sha),
                      data=data, content_type="application/octet-stream")


def _part(upload_id):
    return Path(UPLOAD_DIR) / (".uploading-%s.part" % upload_id)


def _upload_full(client, upload_id, data, chunk=64):
    """按 chunk 顺序传完全部数据，返回最后一个响应。"""
    r = None
    for off in range(0, len(data), chunk):
        r = _put(client, upload_id, off, data[off:off + chunk])
        assert r.status_code == 200, r.get_data(as_text=True)
    return r


if BACKEND == "postgres":
    import psycopg

    def _quota_row(uid):
        with psycopg.connect(os.environ["DATABASE_URL"],
                             autocommit=True) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT used_bytes, reserved_bytes FROM "
                            "upload_user_quotas WHERE user_id=%s", (uid,))
                row = cur.fetchone()
        assert row is not None
        return int(row[0]), int(row[1])

    def _set_quota(uid, quota_bytes):
        with psycopg.connect(os.environ["DATABASE_URL"],
                             autocommit=True) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO upload_user_quotas (user_id, quota_bytes) "
                    "VALUES (%s, %s) ON CONFLICT (user_id) DO UPDATE "
                    "SET quota_bytes = EXCLUDED.quota_bytes",
                    (uid, quota_bytes))

    def _reservation_row(rid):
        with psycopg.connect(os.environ["DATABASE_URL"],
                             autocommit=True) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT state, extract(epoch from expires_at) "
                            "FROM upload_reservations WHERE reservation_id=%s",
                            (rid,))
                row = cur.fetchone()
        return (row[0], float(row[1])) if row else None
else:
    def _quota_row(uid):  # pragma: no cover
        raise RuntimeError("PG only")

    def _set_quota(uid, quota_bytes):  # pragma: no cover
        raise RuntimeError("PG only")

    def _reservation_row(rid):  # pragma: no cover
        raise RuntimeError("PG only")


# =========================================================================== #
# 1. 创建任务（预校验 + 初始化即预占）
# =========================================================================== #
def test_create_returns_initial_state():
    c = _client()
    r = _create(c, name="a.svs", size=1000)
    assert r.status_code == 200, r.get_data(as_text=True)
    j = r.get_json()
    assert j["upload_id"].startswith("upt_")
    assert j["state"] == "active"
    assert j["confirmed_offset"] == 0
    assert j["chunk_size"] == upload_task_store.UPLOAD_CHUNK_SIZE
    assert j["expires_at"] > 0


def test_create_zip_and_mrxs_guided_to_legacy():
    c = _client()
    r = _create(c, name="bundle.zip", size=100)
    assert r.status_code == 400
    assert r.get_json()["code"] == "use_legacy_upload"
    r = _create(c, name="slide.mrxs", size=100)
    assert r.status_code == 400
    assert r.get_json()["code"] == "use_legacy_upload"


def test_create_unsupported_ext_and_bad_args():
    c = _client()
    assert _create(c, name="a.txt", size=10).status_code == 400
    assert _create(c, name="a.svs", size=0).status_code == 400
    assert _create(c, name="a.svs", size="x").status_code == 400
    r = _create(c, name="a.svs", size=10, sha="not-a-hash")
    assert r.status_code == 400


def test_create_over_limit_413(monkeypatch):
    monkeypatch.setattr(upload_guard, "UPLOAD_MAX_REQUEST_BYTES", 1000)
    r = _create(_client(), name="big.svs", size=2000)
    assert r.status_code == 413
    assert r.get_json()["code"] == "upload_too_large"


def test_create_name_conflict_409():
    (Path(UPLOAD_DIR) / "dup.svs").write_bytes(b"existing")
    r = _create(_client(), name="dup.svs", size=10)
    assert r.status_code == 409
    assert r.get_json()["code"] == "name_unavailable"


# =========================================================================== #
# 2. PUT 分片：串行 offset / 幂等 / 哈希 / 单次落盘
# =========================================================================== #
def test_put_chunk_roundtrip_and_get_progress():
    c = _client()
    uid = _create(c, name="a.svs", size=200).get_json()["upload_id"]
    data = os.urandom(120)
    r = _put(c, uid, 0, data)
    assert r.status_code == 200, r.get_data(as_text=True)
    assert r.get_json()["confirmed_offset"] == 120
    assert r.get_json()["action"] == "advanced"
    # 单次落盘：pwrite 直接写 .part（无 multipart 暂存副本）
    assert _part(uid).read_bytes() == data
    g = c.get("/api/uploads/%s" % uid)
    assert g.status_code == 200
    j = g.get_json()
    assert j["state"] == "active" and j["confirmed_offset"] == 120
    assert j["chunk_size"] == upload_task_store.UPLOAD_CHUNK_SIZE
    assert j["expires_at"] > 0


def test_serial_offset_gap_409():
    c = _client()
    uid = _create(c, name="a.svs", size=300).get_json()["upload_id"]
    r = _put(c, uid, 50, b"x" * 50)
    assert r.status_code == 409
    j = r.get_json()
    assert j["code"] == "offset_mismatch"
    assert j["confirmed_offset"] == 0  # 带当前进度供客户端对齐
    assert not _part(uid).exists() or _part(uid).stat().st_size == 0


def test_put_chunk_hash_mismatch_rolls_back(monkeypatch):
    c = _client()
    uid = _create(c, name="a.svs", size=300).get_json()["upload_id"]
    good = os.urandom(80)
    assert _put(c, uid, 0, good).status_code == 200
    r = _put(c, uid, 80, b"y" * 40, sha="0" * 64)
    assert r.status_code == 400
    assert r.get_json()["code"] == "hash_mismatch"
    # 整段回退：.part 截回 confirmed_offset，任务仍 active 可重试
    assert _part(uid).stat().st_size == 80
    t = upload_task_store.get_task(uid)
    assert t["state"] == "active" and t["confirmed_offset"] == 80
    # 正确重试成功
    assert _put(c, uid, 80, b"y" * 40).status_code == 200
    assert _part(uid).stat().st_size == 120


def test_duplicate_put_idempotent():
    c = _client()
    uid = _create(c, name="a.svs", size=200).get_json()["upload_id"]
    data = os.urandom(90)
    r1 = _put(c, uid, 0, data)
    assert r1.get_json()["action"] == "advanced"
    before = upload_task_store.get_task(uid)
    r2 = _put(c, uid, 0, data)  # 同 (offset,length,sha) 重放
    assert r2.status_code == 200
    j = r2.get_json()
    assert j["action"] == "idempotent"
    assert j["confirmed_offset"] == 90  # 返回当前进度，不重复写
    after = upload_task_store.get_task(uid)
    assert after["confirmed_offset"] == 90
    assert after["expires_at"] >= before["expires_at"]  # TTL 刷新（§3.2.4）
    assert _part(uid).read_bytes() == data


def test_duplicate_put_mismatch_409():
    c = _client()
    uid = _create(c, name="a.svs", size=200).get_json()["upload_id"]
    assert _put(c, uid, 0, b"a" * 50).status_code == 200
    r = _put(c, uid, 0, b"b" * 50)  # 同 offset 不同内容
    assert r.status_code == 409
    assert r.get_json()["code"] == "chunk_conflict"


def test_earlier_chunk_replay_returns_progress():
    c = _client()
    uid = _create(c, name="a.svs", size=200).get_json()["upload_id"]
    first, second = b"a" * 50, b"b" * 50
    assert _put(c, uid, 0, first).status_code == 200
    assert _put(c, uid, 50, second).status_code == 200
    r = _put(c, uid, 0, first)  # 更早分片（非最后一片）
    assert r.status_code == 200
    j = r.get_json()
    assert j["action"] == "progressed"
    assert j["confirmed_offset"] == 100  # 只回当前进度，不声称哈希比对
    assert _part(uid).stat().st_size == 100


def test_chunk_beyond_declared_413():
    c = _client()
    uid = _create(c, name="a.svs", size=100).get_json()["upload_id"]
    r = _put(c, uid, 0, b"x" * 101)
    assert r.status_code == 413
    assert r.get_json()["code"] == "chunk_too_large"
    assert not _part(uid).exists() or _part(uid).stat().st_size == 0


# =========================================================================== #
# 3. 并发（行锁/文件锁串行化）
# =========================================================================== #
def test_concurrent_identical_put_single_advance():
    """多线程抢同一 upload_id 的同一分片：一个推进，其余幂等，最终一致。"""
    c = _client()
    c.get("/login")  # 预热 CSRF cookie（避免线程内惰性取 token 竞争）
    uid = _create(c, name="a.svs", size=200).get_json()["upload_id"]
    data = os.urandom(64)
    results, lock = [], threading.Lock()

    def worker():
        r = _put(c, uid, 0, data)
        with lock:
            results.append((r.status_code, r.get_json().get("action")))

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert all(s == 200 for s, _ in results), results
    actions = [a for _, a in results]
    assert actions.count("advanced") >= 1
    t = upload_task_store.get_task(uid)
    assert t["confirmed_offset"] == len(data)  # 只推进一次，无重复累计
    assert _part(uid).read_bytes() == data


def test_concurrent_distinct_put_one_wins():
    """同 offset 不同内容并发：恰好一个 advanced，其余 409 chunk_conflict。"""
    c = _client()
    c.get("/login")
    uid = _create(c, name="a.svs", size=200).get_json()["upload_id"]
    payloads = [bytes([65 + i]) * 64 for i in range(3)]
    results, lock = [], threading.Lock()

    def worker(data):
        r = _put(c, uid, 0, data)
        with lock:
            results.append(r.status_code)

    threads = [threading.Thread(target=worker, args=(d,)) for d in payloads]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert results.count(200) == 1, results
    assert results.count(409) == len(payloads) - 1
    t = upload_task_store.get_task(uid)
    assert t["confirmed_offset"] == 64
    part = _part(uid).read_bytes()
    assert hashlib.sha256(part).hexdigest() == t["last_chunk_sha256"]
    assert part in payloads


# =========================================================================== #
# 4. commit 三段式（§3.2.5）
# =========================================================================== #
def test_commit_happy_path(monkeypatch):
    monkeypatch.setattr(app_mod, "_validate_slide_file", lambda p: True)
    c = _client()
    uid = _create(c, name="ok.svs", size=200).get_json()["upload_id"]
    data = os.urandom(200)
    _upload_full(c, uid, data, chunk=64)
    r = c.post("/api/uploads/%s/commit" % uid)
    assert r.status_code == 200, r.get_data(as_text=True)
    j = r.get_json()
    assert j["state"] == "committed"
    assert j["sha256"] == hashlib.sha256(data).hexdigest()  # 服务端复算权威值
    dest = Path(UPLOAD_DIR) / "ok.svs"
    assert dest.read_bytes() == data
    assert not _part(uid).exists()  # 临时文件已清
    assert upload_task_store.get_task(uid)["state"] == "committed"


def test_recover_commit_does_not_commit_when_settle_fails(monkeypatch):
    """崩溃恢复：入账失败不得留下 committed 文件。"""
    monkeypatch.setattr(app_mod, "_validate_slide_file", lambda p: True)
    c = _client()
    uid = _create(c, name="rc.svs", size=100).get_json()["upload_id"]
    data = b"r" * 100
    _upload_full(c, uid, data, chunk=50)
    _token, task = upload_task_store.begin_commit(uid)
    dest = Path(UPLOAD_DIR) / "rc.svs"
    dest.write_bytes(data)

    def boom(*_a, **_k):
        raise upload_guard.ReservationInvalid("expired")

    monkeypatch.setattr(upload_task_store, "finish_commit", boom)
    out = app_mod._upload_v2_recover_commit(task)
    assert out["state"] == "failed"
    assert not dest.exists()


def test_commit_validates_before_promotion(monkeypatch):
    """§2.3 纠正：_validate_slide_file 必须在原子提升**之前**调用。"""
    calls = []

    def fake_validate(path):
        calls.append((str(path), (Path(UPLOAD_DIR) / "vbp.svs").exists()))
        return True

    monkeypatch.setattr(app_mod, "_validate_slide_file", fake_validate)
    c = _client()
    uid = _create(c, name="vbp.svs", size=100).get_json()["upload_id"]
    _upload_full(c, uid, b"z" * 100, chunk=64)
    r = c.post("/api/uploads/%s/commit" % uid)
    assert r.status_code == 200, r.get_data(as_text=True)
    assert len(calls) == 1
    assert calls[0][1] is False, "验证发生时目标文件不应已提升"
    assert (Path(UPLOAD_DIR) / "vbp.svs").exists()


def test_commit_hash_mismatch_deterministic_failure(monkeypatch):
    monkeypatch.setattr(app_mod, "_validate_slide_file", lambda p: True)
    c = _client()
    uid = _create(c, name="hm.svs", size=100,
                  sha=hashlib.sha256(b"other").hexdigest()).get_json()["upload_id"]
    _upload_full(c, uid, b"d" * 100, chunk=64)
    r = c.post("/api/uploads/%s/commit" % uid)
    assert r.status_code == 409
    j = r.get_json()
    assert j["code"] == "hash_mismatch" and j["state"] == "failed"
    t = upload_task_store.get_task(uid)
    assert t["state"] == "failed"
    assert t["sha256_actual"] == hashlib.sha256(b"d" * 100).hexdigest()
    assert not (Path(UPLOAD_DIR) / "hm.svs").exists()
    assert _part(uid).exists()  # failed 保留临时文件（证据），DELETE 时清理
    # failed 后不可续写/重 commit，DELETE → cancelled 清理
    assert _put(c, uid, 100, b"e").status_code == 409
    assert c.post("/api/uploads/%s/commit" % uid).status_code == 409
    r = c.delete("/api/uploads/%s" % uid)
    assert r.status_code == 200 and r.get_json()["state"] == "cancelled"
    assert not _part(uid).exists()


def test_commit_size_mismatch_400_keeps_active(monkeypatch):
    monkeypatch.setattr(app_mod, "_validate_slide_file", lambda p: True)
    c = _client()
    uid = _create(c, name="sm.svs", size=200).get_json()["upload_id"]
    _upload_full(c, uid, b"x" * 100, chunk=64)  # 只传一半
    r = c.post("/api/uploads/%s/commit" % uid)
    assert r.status_code == 400
    j = r.get_json()
    assert j["code"] == "size_mismatch"
    assert j["confirmed_offset"] == 100 and j["declared_size"] == 200
    assert upload_task_store.get_task(uid)["state"] == "active"
    # 传完剩余部分后可再 commit
    r = _put(c, uid, 100, b"x" * 100)
    assert r.status_code == 200
    assert c.post("/api/uploads/%s/commit" % uid).status_code == 200


def test_commit_invalid_slide_failed(monkeypatch):
    monkeypatch.setattr(app_mod, "_validate_slide_file", lambda p: False)
    c = _client()
    uid = _create(c, name="bad.svs", size=100).get_json()["upload_id"]
    _upload_full(c, uid, b"q" * 100, chunk=64)
    r = c.post("/api/uploads/%s/commit" % uid)
    assert r.status_code == 409
    j = r.get_json()
    assert j["code"] == "invalid_slide" and j["state"] == "failed"
    assert not (Path(UPLOAD_DIR) / "bad.svs").exists()
    assert upload_task_store.get_task(uid)["state"] == "failed"


def test_commit_name_taken_failed(monkeypatch):
    monkeypatch.setattr(app_mod, "_validate_slide_file", lambda p: True)
    (Path(UPLOAD_DIR) / "nt.svs").write_bytes(b"taken")
    c = _client()
    uid = _create(c, name="nt2.svs", size=100).get_json()["upload_id"]
    # 任务创建后目标名被占（模拟并发抢占）
    (Path(UPLOAD_DIR) / "nt2.svs").write_bytes(b"taken")
    _upload_full(c, uid, b"w" * 100, chunk=64)
    r = c.post("/api/uploads/%s/commit" % uid)
    assert r.status_code == 409
    assert r.get_json()["code"] == "name_unavailable"
    assert upload_task_store.get_task(uid)["state"] == "failed"
    assert (Path(UPLOAD_DIR) / "nt2.svs").read_bytes() == b"taken"  # 未覆盖


def test_commit_idempotent_replay_returns_committed(monkeypatch):
    monkeypatch.setattr(app_mod, "_validate_slide_file", lambda p: True)
    c = _client()
    uid = _create(c, name="idem.svs", size=100).get_json()["upload_id"]
    _upload_full(c, uid, b"i" * 100, chunk=64)
    assert c.post("/api/uploads/%s/commit" % uid).status_code == 200
    r = c.post("/api/uploads/%s/commit" % uid)  # 重放
    assert r.status_code == 200
    assert r.get_json()["state"] == "committed"


def test_cancel_during_commit_returns_409(monkeypatch):
    """commit 与 cancel 竞态：验证阶段（无行锁）收到 DELETE → 409 不等待。"""
    started, release = threading.Event(), threading.Event()

    def fake_validate(path):
        started.set()
        release.wait(5)
        return True

    monkeypatch.setattr(app_mod, "_validate_slide_file", fake_validate)
    c = _client()
    uid = _create(c, name="race.svs", size=100).get_json()["upload_id"]
    _upload_full(c, uid, b"r" * 100, chunk=64)
    out = {}

    def do_commit():
        out["r"] = c.post("/api/uploads/%s/commit" % uid)

    th = threading.Thread(target=do_commit)
    th.start()
    try:
        assert started.wait(5), "commit 应进入验证阶段"
        assert upload_task_store.get_task(uid)["state"] == "committing"
        r = c.delete("/api/uploads/%s" % uid)
        assert r.status_code == 409
        assert r.get_json()["code"] == "upload_state_conflict"
    finally:
        release.set()
        th.join(5)
    assert out["r"].status_code == 200
    assert upload_task_store.get_task(uid)["state"] == "committed"


# =========================================================================== #
# 5. DELETE / TTL / 过期
# =========================================================================== #
def test_cancel_active_cleans_part_and_state():
    c = _client()
    uid = _create(c, name="ca.svs", size=200).get_json()["upload_id"]
    _put(c, uid, 0, b"c" * 100)
    assert _part(uid).exists()
    r = c.delete("/api/uploads/%s" % uid)
    assert r.status_code == 200
    assert r.get_json()["state"] == "cancelled"
    assert not _part(uid).exists()
    g = c.get("/api/uploads/%s" % uid)
    assert g.status_code == 200 and g.get_json()["state"] == "cancelled"
    assert c.delete("/api/uploads/%s" % uid).status_code == 200  # 幂等


def test_put_refreshes_task_ttl(monkeypatch):
    c = _client()
    uid = _create(c, name="ttl.svs", size=200).get_json()["upload_id"]
    e0 = upload_task_store.get_task(uid)["expires_at"]
    monkeypatch.setattr(upload_task_store, "UPLOAD_TASK_TTL_SECONDS", 24 * 3600 + 500)
    assert _put(c, uid, 0, b"t" * 10).status_code == 200
    e1 = upload_task_store.get_task(uid)["expires_at"]
    assert e1 > e0  # PUT 刷新 expires_at（§3.2.4）


def test_expiry_lazy_cleanup(monkeypatch):
    """TTL 到期：下次访问惰性转 expired，清临时文件并释放预占。"""
    monkeypatch.setattr(upload_task_store, "UPLOAD_TASK_TTL_SECONDS", 1)
    c = _client()
    uid = _create(c, name="exp.svs", size=200).get_json()["upload_id"]
    assert _put(c, uid, 0, b"e" * 100).status_code == 200
    assert _part(uid).exists()
    time.sleep(1.3)
    g = c.get("/api/uploads/%s" % uid)
    assert g.status_code == 200
    assert g.get_json()["state"] == "expired"
    assert not _part(uid).exists()
    # 过期后 PUT → 409（任务不可写入）
    r = _put(c, uid, 100, b"f" * 100)
    assert r.status_code == 409


# =========================================================================== #
# 6. CSRF / 权限 / 身份
# =========================================================================== #
def test_csrf_required_for_writes_bare_client():
    """裸 client（不包 CsrfClient）无 X-CSRF-Token：POST/PUT/DELETE → 400。"""
    app_mod.app.config["TESTING"] = True
    app_mod.AUTH_ENABLED = False
    bare = app_mod.app.test_client()
    r = bare.post("/api/uploads", json={"filename": "x.svs", "declared_size": 10})
    assert r.status_code == 400
    assert r.get_json()["error"] == "csrf_required"
    r = bare.put("/api/uploads/upt_x/chunk?offset=0&sha256=" + "0" * 64,
                 data=b"zz", content_type="application/octet-stream")
    assert r.status_code == 400
    assert r.get_json()["error"] == "csrf_required"
    r = bare.delete("/api/uploads/upt_x")
    assert r.status_code == 400
    assert r.get_json()["error"] == "csrf_required"
    # 任务未被创建（CSRF 在 body 消费/落库之前拒绝）
    assert upload_task_store.get_task("upt_x") is None


def test_cross_user_task_binding_403():
    """他人任务与不存在任务统一 403（不泄露存在性）。owner（运维）可见。"""
    c_owner = _client(auth=True)
    owner_uid = _user_session(c_owner, role="owner", login="own@x.com")
    uid = _create(c_owner, name="bind.svs", size=100).get_json()["upload_id"]
    task = upload_task_store.get_task(uid)
    assert task["owner_user_id"] == owner_uid

    c_user = _client(auth=True)
    _user_session(c_user, role="user", login="other@x.com")
    assert c_user.get("/api/uploads/%s" % uid).status_code == 403
    assert c_user.put("/api/uploads/%s/chunk?offset=0&sha256=%s"
                      % (uid, "0" * 64), data=b"x",
                      content_type="application/octet-stream").status_code == 403
    assert c_user.delete("/api/uploads/%s" % uid).status_code == 403
    # 不存在任务同款 403（无 404 差异）
    assert c_user.get("/api/uploads/upt_missing").status_code == 403
    # 本人（owner）照常
    assert c_owner.get("/api/uploads/%s" % uid).status_code == 200


def test_noauth_identity_owner_semantics_unchanged():
    """AUTH_ENABLED=False：current_identity 归一 owner，V2 创建/写入不破。"""
    c = _client(auth=False)
    uid = _create(c, name="na.svs", size=50).get_json()["upload_id"]
    assert upload_task_store.get_task(uid)["owner_user_id"] == ""
    assert _put(c, uid, 0, b"n" * 50).status_code == 200


# =========================================================================== #
# 7. 旧接口并存
# =========================================================================== #
def test_legacy_upload_still_works(monkeypatch):
    monkeypatch.setattr(app_mod, "_validate_slide_file", lambda p: True)
    c = _client()
    r = c.post("/api/upload",
               data={"file": (io.BytesIO(b"legacy"), "old.svs")},
               content_type="multipart/form-data")
    assert r.status_code == 200, r.get_data(as_text=True)
    assert r.get_json()["name"] == "old.svs"


# =========================================================================== #
# 8. PG 权威：初始化预占 / 续租 / 转实占 / 释放（RUN_PG_TESTS=1）
# =========================================================================== #
@pg_only
def test_create_reserves_quota_before_any_put(monkeypatch):
    """初始化即预占（§3.3）：任务创建时 reserved_bytes == declared_size。"""
    monkeypatch.setattr(app_mod, "_validate_slide_file", lambda p: True)
    c = _client(auth=True)
    uid_user = _user_session(c, role="user", login="p1@x.com")
    _set_quota(uid_user, 10 ** 9)
    r = _create(c, name="pq.svs", size=5000)
    assert r.status_code == 200, r.get_data(as_text=True)
    used, reserved = _quota_row(uid_user)
    assert used == 0 and reserved == 5000  # 任何 PUT 之前即预占
    task = upload_task_store.get_task(r.get_json()["upload_id"])
    st, _ = _reservation_row(task["reservation_id"])
    assert st == "reserved"


@pg_only
def test_put_renews_reservation():
    """续租（§3.2.4）：PUT 成功后 reservation 的 expires_at 后移。"""
    c = _client(auth=True)
    uid_user = _user_session(c, role="user", login="p2@x.com")
    _set_quota(uid_user, 10 ** 9)
    uid = _create(c, name="pr.svs", size=200).get_json()["upload_id"]
    rid = upload_task_store.get_task(uid)["reservation_id"]
    _, t0 = _reservation_row(rid)
    assert _put(c, uid, 0, b"p" * 100).status_code == 200
    _, t1 = _reservation_row(rid)
    assert t1 > t0


@pg_only
def test_commit_consumes_reservation(monkeypatch):
    monkeypatch.setattr(app_mod, "_validate_slide_file", lambda p: True)
    c = _client(auth=True)
    uid_user = _user_session(c, role="user", login="p3@x.com")
    _set_quota(uid_user, 10 ** 9)
    uid = _create(c, name="pc.svs", size=300).get_json()["upload_id"]
    rid = upload_task_store.get_task(uid)["reservation_id"]
    _upload_full(c, uid, b"c" * 300, chunk=128)
    assert c.post("/api/uploads/%s/commit" % uid).status_code == 200
    used, reserved = _quota_row(uid_user)
    assert used == 300 and reserved == 0
    assert _reservation_row(rid)[0] == "consumed"


@pg_only
def test_cancel_and_expiry_release_reservation(monkeypatch):
    c = _client(auth=True)
    uid_user = _user_session(c, role="user", login="p4@x.com")
    _set_quota(uid_user, 10 ** 9)
    uid = _create(c, name="pl.svs", size=200).get_json()["upload_id"]
    rid = upload_task_store.get_task(uid)["reservation_id"]
    assert c.delete("/api/uploads/%s" % uid).status_code == 200
    used, reserved = _quota_row(uid_user)
    assert reserved == 0
    assert _reservation_row(rid)[0] == "released"

    monkeypatch.setattr(upload_task_store, "UPLOAD_TASK_TTL_SECONDS", 1)
    uid2 = _create(c, name="pe.svs", size=200).get_json()["upload_id"]
    rid2 = upload_task_store.get_task(uid2)["reservation_id"]
    time.sleep(1.3)
    g = c.get("/api/uploads/%s" % uid2)
    assert g.get_json()["state"] == "expired"
    assert _quota_row(uid_user)[1] == 0
    assert _reservation_row(rid2)[0] == "released"


@pg_only
def test_commit_hash_mismatch_releases_reservation():
    c = _client(auth=True)
    uid_user = _user_session(c, role="user", login="p5@x.com")
    _set_quota(uid_user, 10 ** 9)
    uid = _create(c, name="ph.svs", size=100,
                  sha=hashlib.sha256(b"nope").hexdigest()).get_json()["upload_id"]
    _upload_full(c, uid, b"h" * 100, chunk=64)
    r = c.post("/api/uploads/%s/commit" % uid)
    assert r.status_code == 409
    assert _quota_row(uid_user)[1] == 0  # 确定性失败即释放（不占额度）


@pg_only
def test_expired_reservation_reclaimed_rejects_old_put_and_commit(monkeypatch):
    """过期预占被新任务回收后，旧任务 PUT/commit fail-closed，不得无记账提交。"""
    monkeypatch.setattr(app_mod, "_validate_slide_file", lambda p: True)
    c = _client(auth=True)
    uid_user = _user_session(c, role="user", login="p6@x.com")
    _set_quota(uid_user, 5000)
    uid = _create(c, name="old.svs", size=5000).get_json()["upload_id"]
    rid = upload_task_store.get_task(uid)["reservation_id"]
    assert _put(c, uid, 0, b"a" * 100).status_code == 200
    with psycopg.connect(os.environ["DATABASE_URL"], autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE upload_reservations SET expires_at = now() - interval '1 second' "
                "WHERE reservation_id=%s", (rid,))
    uid2 = _create(c, name="new.svs", size=5000)
    assert uid2.status_code == 200, uid2.get_data(as_text=True)
    r = _put(c, uid, 100, b"b" * 100)
    assert r.status_code == 409
    assert r.get_json().get("code") == "reservation_expired"
    used, reserved = _quota_row(uid_user)
    assert used == 0
    assert reserved == 5000  # 仅新任务预占
    dest = Path(UPLOAD_DIR) / "old.svs"
    assert not dest.exists()


@json_only
def test_json_backend_user_create_fail_closed():
    """json 后端 + role=user：配额不可用 → 503（与旧 /api/upload 同哲学）。"""
    c = _client(auth=True)
    _user_session(c, role="user", login="j1@x.com")
    r = _create(c, name="j.svs", size=100)
    assert r.status_code == 503
    assert r.get_json()["code"] == "upload_guard_unavailable"


@json_only
def test_json_backend_store_equivalent_records():
    """json 后端等价文件记录：原子写盘结构可读、字段与 PG 模型同名。"""
    c = _client()
    uid = _create(c, name="js.svs", size=100).get_json()["upload_id"]
    assert _put(c, uid, 0, b"j" * 60).status_code == 200
    path = upload_task_store._path()
    assert os.path.exists(path)
    with open(path, encoding="utf-8") as f:
        data = _json.load(f)
    rec = data["tasks"][uid]
    for key in ("upload_id", "owner_user_id", "safe_name", "declared_size",
                "chunk_size", "confirmed_offset", "last_chunk_offset",
                "last_chunk_length", "last_chunk_sha256", "sha256_expected",
                "sha256_actual", "reservation_id", "state", "commit_token",
                "expires_at"):
        assert key in rec, "等价文件记录缺字段 %s" % key
    assert rec["confirmed_offset"] == 60
    assert rec["last_chunk_sha256"] == hashlib.sha256(b"j" * 60).hexdigest()


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
