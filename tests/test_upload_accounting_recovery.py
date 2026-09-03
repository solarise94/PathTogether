# -*- coding: utf-8 -*-
"""V1（旧单请求 /api/upload）上传配额持久补偿测试（review-2026-08-29 §10.4 G7）。

覆盖（json 默认 + RUN_PG_TESTS=1 双跑，PG 段 pg_only）：
  - 单文件 / ZIP 成功路径接入 upload_task_store 收口状态机：task 落
    committing→committed，manifest（name/size/sha256/slide）在提升之前持久化；
  - 「task intent 前不得提升」：begin_legacy_commit 失败 → 无任何文件提升、
    无 .uploading-/.extracting- 残留、预占释放；
  - PG 故障点与幂等恢复：finish_commit / consume 失败 → 任务保持
    committing、文件已提升 → committing 扫描幂等补账，used_bytes 只增加一次
    （重复恢复不双扣）；
  - 提升前崩溃（全不存在）→ rollback + 取消 + 释放预占；
  - 部分提升 / 证据冲突（大小或哈希不符）→ fail-closed 保持 committing，
    **绝不按过期时间盲 release**；
  - owner / 免认证不回归（无配额主体时状态机照常工作）；
  - admin overview uploads 段：committing/backlog 计数与最老年龄，不暴露
    路径 / 原文件名 / 用户标识；
  - `_upload_consume_quietly` 符号已删除（端点源码零引用，模块无该属性
    源码零调用）。

运行：cd 项目根 && python3 -m pytest tests/test_upload_accounting_recovery.py -q
（PG 双跑：RUN_PG_TESTS=1 python3 -m pytest tests/test_upload_accounting_recovery.py -q）
"""
import hashlib
import inspect
import io
import os
import sys
import zipfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import _bootstrap  # noqa: E402,F401  # session 目录+openslide stub（conftest 先行）
UPLOAD_DIR = _bootstrap.UPLOAD_DIR
import pytest  # noqa: E402

import share_store  # noqa: E402
import upload_guard  # noqa: E402
import upload_task_store  # noqa: E402
import user_store  # noqa: E402
import app as app_mod  # noqa: E402
from pg_compat import BACKEND  # noqa: E402
from _pt_helpers import csrf_client, isolate_app, clear_upload_dir  # noqa: E402


pg_only = pytest.mark.skipif(
    BACKEND != "postgres", reason="配额 consume/恢复需 PG（RUN_PG_TESTS=1）")


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    """每用例：独立存储 + 上传防护复位 + _validate_slide_file 放行 + 清空。"""
    isolate_app(monkeypatch, tmp_path, UPLOAD_DIR, login_limits=True)
    monkeypatch.setattr(upload_task_store, "UPLOAD_TASK_FILE",
                        tmp_path / "upload_tasks.json")
    monkeypatch.setattr(upload_guard, "UPLOAD_MAX_REQUEST_BYTES", 10 * 1024 ** 3)
    monkeypatch.setattr(upload_guard, "UPLOAD_RESERVED_FREE_BYTES", 0)
    monkeypatch.setattr(upload_task_store, "UPLOAD_TASK_TTL_SECONDS", 24 * 3600)
    monkeypatch.setattr(upload_task_store, "UPLOAD_COMMIT_TIMEOUT_SECONDS", 600)
    monkeypatch.setattr(app_mod, "ZIP_MAX_MEMBERS", 4096)
    monkeypatch.setattr(app_mod, "ZIP_MAX_PATH_DEPTH", 8)
    monkeypatch.setattr(app_mod, "ZIP_MAX_MEMBER_BYTES",
                        upload_guard.UPLOAD_MAX_REQUEST_BYTES)
    monkeypatch.setattr(app_mod, "ZIP_MAX_TOTAL_BYTES",
                        2 * upload_guard.UPLOAD_MAX_REQUEST_BYTES)
    monkeypatch.setattr(app_mod, "ZIP_MAX_COMPRESSION_RATIO", 100.0)
    monkeypatch.setattr(app_mod, "_validate_slide_file",
                        lambda p, **_: None)  # A0 异常契约
    monkeypatch.setitem(app_mod.app.config, "MAX_CONTENT_LENGTH", None)
    # G8 secret 节流状态复位（防跨用例吞掉本用例想断言的告警）
    monkeypatch.setattr(app_mod, "_secret_warn_last", {})
    clear_upload_dir(UPLOAD_DIR)
    yield


if BACKEND == "postgres":
    import psycopg

    def _set_quota(user_id, quota_bytes):
        with psycopg.connect(os.environ["DATABASE_URL"], autocommit=True) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO upload_user_quotas (user_id, quota_bytes) "
                    "VALUES (%s, %s) ON CONFLICT (user_id) DO UPDATE "
                    "SET quota_bytes = EXCLUDED.quota_bytes",
                    (user_id, quota_bytes))
else:
    def _set_quota(user_id, quota_bytes):  # pragma: no cover
        raise RuntimeError("PG only")


def _client(auth=False):
    app_mod.app.config["TESTING"] = True
    app_mod.AUTH_ENABLED = auth
    return csrf_client(app_mod.app.test_client())


def _user_session(client, role="user", login="rec@x.com"):
    u = user_store.create_user(login, "pass1234pass1234", role=role)
    with client.session_transaction() as sess:
        sess["auth_user"] = True
        sess["user_id"] = u["user_id"]
        sess["role"] = role
        sess["auth_version"] = (user_store.get_user(u["user_id"]) or {}).get(
            "auth_version", 1)
    return u["user_id"]


def _upload(client, name="a.svs", size=100, content=None):
    data = content if content is not None else os.urandom(size)
    return client.post("/api/upload",
                       data={"file": (io.BytesIO(data), name)},
                       content_type="multipart/form-data")


def _upload_zip(client, members, name="bundle.zip"):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for fname, data in members:
            zf.writestr(fname, data)
    return client.post("/api/upload",
                       data={"file": (io.BytesIO(buf.getvalue()), name)},
                       content_type="multipart/form-data")


def _residue():
    return [p.name for p in Path(UPLOAD_DIR).iterdir()
            if p.name.startswith(".uploading-")
            or p.name.startswith(".extracting-")]


def _tasks(**kw):
    return upload_task_store.list_tasks(**kw)


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _age_committing(monkeypatch):
    """把 commit 超时压到 0：所有 committing 任务立即进入恢复窗口。"""
    monkeypatch.setattr(upload_task_store, "UPLOAD_COMMIT_TIMEOUT_SECONDS", 0)


# =========================================================================== #
# 1. 成功路径：V1/ZIP 接入 upload_task_store 收口状态机
# =========================================================================== #
def test_v1_single_file_success_uses_task_machine():
    """免登录 owner：成功上传 → 唯一任务 committed，manifest 与文件吻合。"""
    data = b"single-slide-bytes"
    r = _upload(_client(), name="s1.svs", content=data)
    assert r.status_code == 200, r.get_data(as_text=True)
    assert r.get_json() == {"name": "s1.svs"}
    tasks = _tasks()
    assert len(tasks) == 1
    t = tasks[0]
    assert t["state"] == upload_task_store.STATE_COMMITTED
    assert t["safe_name"] == "s1.svs"
    assert t["declared_size"] == len(data)
    arts = t["v1_artifacts"]
    assert isinstance(arts, list) and len(arts) == 1
    assert arts[0]["name"] == "s1.svs"
    assert arts[0]["size"] == len(data)
    assert arts[0]["sha256"] == _sha(data)
    assert arts[0]["slide"] is True
    assert (Path(UPLOAD_DIR) / "s1.svs").read_bytes() == data
    assert _residue() == []


def test_v1_zip_success_records_manifest_and_ownership():
    """zip（多切片 + 无关扩展名伴侣文件不参与）：manifest 记全部提升文件，
    只有有效切片入归属；响应契约（name/extracted）与旧行为一致。"""
    a, b = b"slide-a-bytes", b"slide-b-bytes"
    r = _upload_zip(_client(), [("a.svs", a), ("b.tif", b)])
    assert r.status_code == 200, r.get_data(as_text=True)
    body = r.get_json()
    assert body["name"] == "a.svs"
    assert body["extracted"] == ["a.svs", "b.tif"]
    tasks = _tasks()
    assert len(tasks) == 1
    t = tasks[0]
    assert t["state"] == upload_task_store.STATE_COMMITTED
    assert t["declared_size"] == len(a) + len(b)
    names = {x["name"]: x for x in t["v1_artifacts"]}
    assert set(names) == {"a.svs", "b.tif"}
    assert all(x["slide"] for x in names.values())
    assert names["a.svs"]["sha256"] == _sha(a)
    # 任务 committed 前归属已入库
    meta = share_store.get_slide_meta_full("a.svs") or {}
    assert meta.get("owner_user_id") in (None, "")
    assert _residue() == []


@pg_only
def test_v1_zip_companion_files_in_manifest_not_slides():
    """MRXS bundle：manifest 含伴侣目录文件（slide=False，只提升不入归属），
    settle_bytes = 全部提升文件字节总和。"""
    uid = user_store.create_user("z1@x.com", "pass1234pass1234", role="user")["user_id"]
    _set_quota(uid, 10 ** 7)
    c = _client(auth=True)
    with c.session_transaction() as sess:
        sess["auth_user"] = True
        sess["user_id"] = uid
        sess["role"] = "user"
        sess["auth_version"] = 1
    main, comp = b"mrxs-main", b"companion-data"
    r = _upload_zip(c, [("S.mrxs", main), ("S/", b""), ("S/d.dat", comp)])
    assert r.status_code == 200, r.get_data(as_text=True)
    assert r.get_json() == {"name": "S.mrxs", "extracted": ["S.mrxs"]}
    t = _tasks()[0]
    arts = {x["name"]: x for x in t["v1_artifacts"]}
    assert set(arts) == {"S.mrxs", "S/d.dat"}
    assert arts["S.mrxs"]["slide"] is True
    assert arts["S/d.dat"]["slide"] is False
    assert t["declared_size"] == len(main) + len(comp)
    # 成功即同事务转实占（全部提升字节）
    row = upload_guard.get_quota_row(uid)
    assert row["used_bytes"] == len(main) + len(comp)
    assert row["reserved_bytes"] == 0


@pg_only
def test_v1_single_file_consumes_exact_bytes():
    """user 成功上传：consume 与 committed 同事务（used=实际字节，reserved=0）。"""
    uid = user_store.create_user("q1@x.com", "pass1234pass1234", role="user")["user_id"]
    _set_quota(uid, 10 ** 7)
    c = _client(auth=True)
    with c.session_transaction() as sess:
        sess["auth_user"] = True
        sess["user_id"] = uid
        sess["role"] = "user"
        sess["auth_version"] = 1
    r = _upload(c, name="ok.svs", content=b"x" * 777)
    assert r.status_code == 200, r.get_data(as_text=True)
    row = upload_guard.get_quota_row(uid)
    assert row["used_bytes"] == 777
    assert row["reserved_bytes"] == 0
    assert _tasks()[0]["state"] == upload_task_store.STATE_COMMITTED


def test_owner_upload_no_quota_task_machine_still_works(monkeypatch):
    """owner（AUTH_ENABLED=True）：无配额主体，但状态机照常收口（不回归）。"""
    monkeypatch.setattr(app_mod, "_validate_slide_file",
                        lambda p, **_: None)  # A0 异常契约
    c = _client(auth=True)
    _user_session(c, role="owner", login="own@x.com")
    r = _upload(c, name="own.svs", content=b"owner-bytes")
    assert r.status_code == 200
    t = _tasks()[0]
    assert t["state"] == upload_task_store.STATE_COMMITTED
    assert t["reservation_id"] is None
    assert t["owner_user_id"] != ""


# =========================================================================== #
# 2. task intent 前不得提升
# =========================================================================== 
def test_no_promotion_before_task_intent_single_file(monkeypatch):
    """begin_legacy_commit 失败 → 无提升、无残留、任务不落库。"""
    def _boom(*a, **kw):
        raise upload_task_store.UploadTaskError("受理失败（测试注入）")

    monkeypatch.setattr(upload_task_store, "begin_legacy_commit", _boom)
    r = _upload(_client(), name="np.svs", content=b"never-promoted")
    assert r.status_code == 500
    assert r.get_json()["code"] == "upload_task_error"
    assert not (Path(UPLOAD_DIR) / "np.svs").exists()
    assert _residue() == []
    assert _tasks() == []


def test_no_promotion_before_task_intent_zip(monkeypatch):
    def _boom(*a, **kw):
        raise upload_task_store.UploadTaskError("受理失败（测试注入）")

    monkeypatch.setattr(upload_task_store, "begin_legacy_commit", _boom)
    r = _upload_zip(_client(), [("a.svs", b"aa"), ("b.svs", b"bb")])
    assert r.status_code == 500
    assert not (Path(UPLOAD_DIR) / "a.svs").exists()
    assert not (Path(UPLOAD_DIR) / "b.svs").exists()
    assert _residue() == []
    assert _tasks() == []


@pg_only
def test_intent_failure_releases_reservation(monkeypatch):
    uid = user_store.create_user("q2@x.com", "pass1234pass1234", role="user")["user_id"]
    _set_quota(uid, 10 ** 7)
    c = _client(auth=True)
    with c.session_transaction() as sess:
        sess["auth_user"] = True
        sess["user_id"] = uid
        sess["role"] = "user"
        sess["auth_version"] = 1

    def _boom(*a, **kw):
        raise upload_task_store.UploadTaskError("受理失败（测试注入）")

    monkeypatch.setattr(upload_task_store, "begin_legacy_commit", _boom)
    r = _upload(c, name="np2.svs", content=b"xyz")
    assert r.status_code == 500
    assert upload_guard.get_quota_row(uid)["reserved_bytes"] == 0


# =========================================================================== #
# 3. PG 故障点与幂等恢复（G7 核心）
# =========================================================================== #
@pg_only
def test_finish_crash_then_scan_settles_once(monkeypatch):
    """finish_commit 崩溃（consume 未发生）→ 文件已提升、任务 committing；
    committing 扫描幂等补账，重复恢复 used_bytes 只增加一次。"""
    uid = user_store.create_user("q3@x.com", "pass1234pass1234", role="user")["user_id"]
    _set_quota(uid, 10 ** 7)
    c = _client(auth=True)
    with c.session_transaction() as sess:
        sess["auth_user"] = True
        sess["user_id"] = uid
        sess["role"] = "user"
        sess["auth_version"] = 1
    data = b"z" * 500
    real_finish = upload_task_store.finish_commit
    monkeypatch.setattr(upload_task_store, "finish_commit",
                        lambda *a, **kw: (_ for _ in ()).throw(
                            RuntimeError("PG 抖动（测试注入）")))
    r = _upload(c, name="crash.svs", content=data)
    # 文件已持久提升：请求按成功返回，收口由恢复扫描补账
    assert r.status_code == 200
    assert (Path(UPLOAD_DIR) / "crash.svs").read_bytes() == data
    t = _tasks()[0]
    assert t["state"] == upload_task_store.STATE_COMMITTING
    row = upload_guard.get_quota_row(uid)
    assert row["used_bytes"] == 0
    assert row["reserved_bytes"] >= len(data)
    monkeypatch.setattr(upload_task_store, "finish_commit", real_finish)

    _age_committing(monkeypatch)
    for _ in range(3):  # 重复恢复：幂等，只补一次账
        app_mod._upload_legacy_recover_stale({"role": "owner"})
    t = _tasks()[0]
    assert t["state"] == upload_task_store.STATE_COMMITTED
    row = upload_guard.get_quota_row(uid)
    assert row["used_bytes"] == len(data)
    assert row["reserved_bytes"] == 0


@pg_only
def test_consume_crash_same_transaction_no_partial_settle(monkeypatch):
    """consume 在 finish 事务内失败 → 整体回滚（任务仍 committing、used 不动），
    恢复后一次性补账。"""
    uid = user_store.create_user("q4@x.com", "pass1234pass1234", role="user")["user_id"]
    _set_quota(uid, 10 ** 7)
    c = _client(auth=True)
    with c.session_transaction() as sess:
        sess["auth_user"] = True
        sess["user_id"] = uid
        sess["role"] = "user"
        sess["auth_version"] = 1
    real_consume = upload_guard.consume_reservation_locked

    def _boom(cur, rid, n):
        raise RuntimeError("consume 崩溃（测试注入）")

    monkeypatch.setattr(upload_guard, "consume_reservation_locked", _boom)
    r = _upload(c, name="cc.svs", content=b"c" * 300)
    assert r.status_code == 200
    t = _tasks()[0]
    assert t["state"] == upload_task_store.STATE_COMMITTING
    assert upload_guard.get_quota_row(uid)["used_bytes"] == 0
    monkeypatch.setattr(upload_guard, "consume_reservation_locked", real_consume)

    _age_committing(monkeypatch)
    app_mod._upload_legacy_recover_stale({"role": "owner"})
    assert _tasks()[0]["state"] == upload_task_store.STATE_COMMITTED
    assert upload_guard.get_quota_row(uid)["used_bytes"] == 300


def test_promote_crash_before_promotion_rolls_back_and_releases(monkeypatch):
    """提升 IO 失败（intent 后、提升中）→ 临时失败收尾：任务取消、预占释放、
    目标不落盘。"""
    from unittest import mock

    with mock.patch.object(app_mod, "_promote_no_clobber") as pm:
        pm.side_effect = OSError("EIO（测试注入）")
        r = _upload(_client(), name="pf.svs", content=b"promote-fail")
    assert r.status_code == 400
    assert not (Path(UPLOAD_DIR) / "pf.svs").exists()
    assert _residue() == []
    tasks = _tasks()
    assert len(tasks) == 1
    assert tasks[0]["state"] == upload_task_store.STATE_CANCELLED


def test_name_conflict_after_intent_fails_permanently(monkeypatch):
    """受理后提升撞名（TOCTOU 竞态，no-clobber 兜底）→ 确定性失败：任务
    failed，不占目标名。（入口的 dest.exists() 早退 409 不建任务，另有
    test_upload_guard 覆盖。）"""
    from unittest import mock

    with mock.patch.object(app_mod, "_promote_no_clobber") as pm:
        pm.side_effect = FileExistsError()
        r = _upload(_client(), name="race.svs", content=b"challenger")
    assert r.status_code == 409
    assert r.get_json()["code"] == "name_unavailable"
    assert not (Path(UPLOAD_DIR) / "race.svs").exists()
    tasks = _tasks()
    assert len(tasks) == 1
    assert tasks[0]["state"] == upload_task_store.STATE_FAILED
    assert _residue() == []


# =========================================================================== #
# 4. 恢复的三态证据判定（部分提升 fail-closed；全不存在回滚释放）
# =========================================================================== #
def _mk_committing_task(artifacts, *, owner="", reservation_id=None):
    return upload_task_store.begin_legacy_commit(
        owner_user_id=owner, filename="f.zip", safe_name=artifacts[0]["name"],
        artifacts=artifacts, reservation_id=reservation_id)


def _write_artifact(name, data):
    p = Path(UPLOAD_DIR) / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)
    return {"name": name, "size": len(data), "sha256": _sha(data),
            "slide": True}


@pg_only
def test_recovery_absent_promotes_nothing_releases(monkeypatch):
    """全不存在 → rollback + 取消 + 释放预占（安全回退，无配额泄漏）。"""
    uid = user_store.create_user("q5@x.com", "pass1234pass1234", role="user")["user_id"]
    _set_quota(uid, 10 ** 7)
    r = upload_guard.reserve_upload(uid, 1000, inflight_limit=10, hourly_limit=10)
    a1 = {"name": "gone1.svs", "size": 10, "sha256": _sha(b"0123456789"),
          "slide": True}
    a2 = {"name": "gone2.svs", "size": 20, "sha256": _sha(b"a" * 20),
          "slide": True}
    _mk_committing_task([a1, a2], owner=uid,
                        reservation_id=r["reservation_id"])
    _age_committing(monkeypatch)
    app_mod._upload_legacy_recover_stale({"role": "owner"})
    t = _tasks()[0]
    assert t["state"] == upload_task_store.STATE_CANCELLED
    row = upload_guard.get_quota_row(uid)
    assert row["reserved_bytes"] == 0
    assert row["used_bytes"] == 0


@pg_only
def test_recovery_absent_respects_commit_timeout_window(monkeypatch):
    """未超 commit 超时的 committing 不被扫描动（恢复必须凭超时窗口，不凭
    TTL 盲动）。"""
    uid = user_store.create_user("q6@x.com", "pass1234pass1234", role="user")["user_id"]
    _set_quota(uid, 10 ** 7)
    r = upload_guard.reserve_upload(uid, 1000, inflight_limit=10, hourly_limit=10)
    a1 = {"name": "wait.svs", "size": 10, "sha256": _sha(b"0123456789"),
          "slide": True}
    _mk_committing_task([a1], owner=uid, reservation_id=r["reservation_id"])
    app_mod._upload_legacy_recover_stale({"role": "owner"})  # 未老化：不动
    assert _tasks()[0]["state"] == upload_task_store.STATE_COMMITTING
    assert upload_guard.get_quota_row(uid)["reserved_bytes"] == 1000


@pg_only
def test_recovery_partial_promote_fail_closed_no_blind_release(monkeypatch):
    """部分提升 → 保持 committing + 预占不释放（绝不按过期时间盲 release）。"""
    uid = user_store.create_user("q7@x.com", "pass1234pass1234", role="user")["user_id"]
    _set_quota(uid, 10 ** 7)
    r = upload_guard.reserve_upload(uid, 1000, inflight_limit=10, hourly_limit=10)
    _write_artifact("part1.svs", b"0123456789")
    a2 = {"name": "part2.svs", "size": 20, "sha256": _sha(b"b" * 20),
          "slide": True}
    a1 = {"name": "part1.svs", "size": 10, "sha256": _sha(b"0123456789"),
          "slide": True}
    _mk_committing_task([a1, a2], owner=uid,
                        reservation_id=r["reservation_id"])
    _age_committing(monkeypatch)
    for _ in range(2):
        app_mod._upload_legacy_recover_stale({"role": "owner"})
    t = _tasks()[0]
    assert t["state"] == upload_task_store.STATE_COMMITTING
    row = upload_guard.get_quota_row(uid)
    assert row["reserved_bytes"] == 1000  # 不盲 release
    assert row["used_bytes"] == 0


@pg_only
def test_recovery_size_mismatch_conflict_fail_closed(monkeypatch):
    """目标名被同名不同内容文件占用（大小不符）→ 证据冲突，保持 committing。"""
    uid = user_store.create_user("q8@x.com", "pass1234pass1234", role="user")["user_id"]
    _set_quota(uid, 10 ** 7)
    r = upload_guard.reserve_upload(uid, 1000, inflight_limit=10, hourly_limit=10)
    (Path(UPLOAD_DIR) / "squatter.svs").write_bytes(b"foreign-content!!")
    a1 = {"name": "squatter.svs", "size": 5, "sha256": _sha(b"12345"),
          "slide": True}
    _mk_committing_task([a1], owner=uid, reservation_id=r["reservation_id"])
    _age_committing(monkeypatch)
    app_mod._upload_legacy_recover_stale({"role": "owner"})
    assert _tasks()[0]["state"] == upload_task_store.STATE_COMMITTING
    assert upload_guard.get_quota_row(uid)["reserved_bytes"] == 1000


@pg_only
def test_recovery_promoted_finishes_idempotently_zip(monkeypatch):
    """全已提升（含伴侣目录文件）→ 幂等 finish：used 增加恰好一次，ownership
    补齐；再次扫描不双扣。"""
    uid = user_store.create_user("q9@x.com", "pass1234pass1234", role="user")["user_id"]
    _set_quota(uid, 10 ** 7)
    r = upload_guard.reserve_upload(uid, 1000, inflight_limit=10, hourly_limit=10)
    main, comp = b"mrxs-main-data", b"companion"
    a1 = _write_artifact("R.mrxs", main)
    a1["slide"] = True
    a2 = _write_artifact("R/d.dat", comp)
    a2["slide"] = False
    _mk_committing_task([a1, a2], owner=uid,
                        reservation_id=r["reservation_id"])
    _age_committing(monkeypatch)
    for _ in range(2):
        app_mod._upload_legacy_recover_stale({"role": "owner"})
    t = _tasks()[0]
    assert t["state"] == upload_task_store.STATE_COMMITTED
    row = upload_guard.get_quota_row(uid)
    assert row["used_bytes"] == len(main) + len(comp)
    assert row["reserved_bytes"] == 0
    # ownership 在 finish 之前补齐
    meta = share_store.get_slide_meta_full("R.mrxs") or {}
    assert meta.get("owner_user_id") == uid


# =========================================================================== #
# 5. admin overview：committing / backlog 观测（不泄露隐私）
# =========================================================================== 
def test_admin_overview_uploads_section(monkeypatch):
    owner = user_store.create_user("ov@x.com", "ownerpass12345678", role="owner")
    # 一个 committed（成功上传）+ 一个卡住的 committing（证据冲突形态）
    r0 = _upload(_client(), name="done.svs", content=b"done")
    assert r0.status_code == 200
    _write_artifact("half1.svs", b"0123456789")
    a2 = {"name": "half2.svs", "size": 20, "sha256": _sha(b"c" * 20),
          "slide": True}
    a1 = {"name": "half1.svs", "size": 10, "sha256": _sha(b"0123456789"),
          "slide": True}
    _mk_committing_task([a1, a2], owner=owner["user_id"])

    c = _client(auth=True)
    with c.session_transaction() as sess:
        sess["auth_user"] = True
        sess["user_id"] = owner["user_id"]
        sess["role"] = "owner"
        sess["auth_version"] = 1
    _age_committing(monkeypatch)
    resp = c.get("/api/admin/v1/overview")
    assert resp.status_code == 200, resp.get_data(as_text=True)
    seg = resp.get_json()["uploads"]
    assert seg["available"] is True
    assert seg["committing"] == 1
    assert seg["committing_backlog"] == 1
    assert seg["committing_oldest_age_seconds"] >= 0.0
    # 隐私红线：不暴露路径 / 文件名 / 用户标识
    seg_text = repr(seg)
    for forbidden in ("half1.svs", "half2.svs", "done.svs", owner["user_id"]):
        assert forbidden not in seg_text
    assert set(seg) == {"available", "committing", "committing_backlog",
                        "committing_oldest_age_seconds"}


def test_admin_overview_uploads_section_empty():
    owner = user_store.create_user("ov2@x.com", "ownerpass12345678", role="owner")
    c = _client(auth=True)
    with c.session_transaction() as sess:
        sess["auth_user"] = True
        sess["user_id"] = owner["user_id"]
        sess["role"] = "owner"
        sess["auth_version"] = 1
    resp = c.get("/api/admin/v1/overview")
    seg = resp.get_json()["uploads"]
    assert seg == {"available": True, "committing": 0,
                   "committing_backlog": 0, "committing_oldest_age_seconds": 0.0}


# =========================================================================== #
# 6. quiet consume 成功路径已删除（锁死）
# =========================================================================== #
def test_quiet_consume_removed_from_success_path():
    """_upload_consume_quietly 必须彻底删除，不得留墓碑桩（review 10.5.1）。"""
    src = inspect.getsource(app_mod.api_upload) + inspect.getsource(
        app_mod._api_upload_zip)
    assert "_upload_consume_quietly" not in src
    assert not hasattr(app_mod, "_upload_consume_quietly")


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
