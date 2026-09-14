# -*- coding: utf-8 -*-
"""W3：项目创建升级——对话框契约 + 按用户 Idempotency-Key（存储层）。

钉死的契约（project_idempotency_store.py + migrations/0050）：

- P01 并发幂等：同用户同键同负载两线程（Barrier）→ 同一 pid、单项目行、
  单幂等行；同键不同负载 → IdempotencyConflict(409)；他用户同键独立成功；
- P02 形状契约：slides 非 list（含 dict/str/None 不许静默吞成空）、元素非
  str、name 空/缺失/非 str/超 60、note 超 200/非 str → PayloadInvalid(400)；
  同名不同键 → 两个项目；无键（旧客户端）→ 同负载两次也是两个项目；
- 原子性：幂等行插入被注入失败 → projects 行随事务回滚，键仍可重试；
- guest 两条路径（无键/有键）均 PermissionError(403 语义)。

运行：python -m pytest tests/test_project_creation_upgrade.py -q
"""

import os
import sys
import threading

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import _bootstrap  # noqa: E402,F401  # session 目录+openslide stub（conftest 先行）
import psycopg  # noqa: E402
import pytest  # noqa: E402

import pg_store  # noqa: E402
import project_idempotency_store as pis  # noqa: E402


@pytest.fixture(autouse=True)
def _ensure_schema_0050():
    """对已启动的 PG session 幂等应用迁移（0050 可在 session 起来后才落盘）。"""
    conn = pg_store.connect()
    try:
        pg_store.ensure_schema(conn)
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# 辅助
# --------------------------------------------------------------------------- #
def _run_concurrently(callables):
    """Barrier 同放多个 callable（各自独立连接），返回 (results, errors)。"""
    n = len(callables)
    barrier = threading.Barrier(n)
    results = [None] * n
    errors = [None] * n

    def _wrap(i, fn):
        try:
            barrier.wait(timeout=30)
        except BaseException as exc:  # pragma: no cover - 仅屏障异常
            errors[i] = exc
            return
        try:
            results[i] = fn()
        except BaseException as exc:
            errors[i] = exc

    threads = [threading.Thread(target=_wrap, args=(i, fn))
               for i, fn in enumerate(callables)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    assert not any(t.is_alive() for t in threads), "并发线程未在时限内完成"
    return results, errors


def _scalar(pg_uri, sql, params=()):
    conn = psycopg.connect(pg_uri)
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            return cur.fetchone()[0]
    finally:
        conn.close()


def _projects_of(pg_uri, owner):
    return _scalar(pg_uri, "SELECT count(*) FROM projects WHERE owner_user_id=%s",
                   (owner,))


# --------------------------------------------------------------------------- #
# P01：同用户同键并发 → 单项目；冲突 409；跨用户独立
# --------------------------------------------------------------------------- #
def test_p01_same_user_same_key_concurrent_one_project(pg_uri):
    owner = "usr_p01_a"
    other = "usr_p01_b"
    key = "p01-draft-key-1"

    def _create():
        return pis.create_project_idempotent(
            name="P01 会诊项目", note="首批", slides=["a.tif", "b.tif"],
            owner_user_id=owner, requester_role="user", idempotency_key=key)

    results, errors = _run_concurrently([_create, _create])
    assert errors == [None, None], errors
    pids = {r["pid"] for r in results}
    assert len(pids) == 1, "并发同键同负载必须返回同一 pid"
    # 恰好一个是首次创建、一个是重放
    assert sorted(bool(r.get("duplicate")) for r in results) == [False, True]

    assert _projects_of(pg_uri, owner) == 1
    assert _scalar(pg_uri, "SELECT count(*) FROM project_create_idempotency") == 1

    # 同键不同负载 → 409 语义
    with pytest.raises(pis.IdempotencyConflict):
        pis.create_project_idempotent(
            name="P01 另一份草稿", note="不同负载", slides=["a.tif"],
            owner_user_id=owner, requester_role="user", idempotency_key=key)

    # 他用户同键同负载 → 独立成功，互不影响
    proj_other = pis.create_project_idempotent(
        name="P01 会诊项目", note="首批", slides=["a.tif", "b.tif"],
        owner_user_id=other, requester_role="user", idempotency_key=key)
    assert proj_other["pid"] not in pids
    assert _projects_of(pg_uri, other) == 1
    assert _projects_of(pg_uri, owner) == 1  # 原用户不受影响
    assert _scalar(pg_uri, "SELECT count(*) FROM project_create_idempotency") == 2

    # 串行重放同键同负载 → 仍是原 pid，且响应形状与首次创建一致
    replay = pis.create_project_idempotent(
        name="P01 会诊项目", note="首批", slides=["a.tif", "b.tif"],
        owner_user_id=owner, requester_role="user", idempotency_key=key)
    first = next(r for r in results if not r.get("duplicate"))
    assert replay["pid"] == first["pid"]
    assert replay["duplicate"] is True
    for field in ("name", "note", "slides", "owner_user_id", "archived"):
        assert replay[field] == first[field]
    assert abs(replay["created_at"] - first["created_at"]) < 1e-6
    assert _projects_of(pg_uri, owner) == 1


# --------------------------------------------------------------------------- #
# P02：非法输入 400 语义 + 同名不同键/无键不去重
# --------------------------------------------------------------------------- #
def test_p02_invalid_input_and_same_name_different_keys(pg_uri):
    # —— 形状校验：一律 PayloadInvalid（HTTP 400），绝不静默吞成默认值 ——
    bad_bodies = [
        {"name": "x", "slides": "not-a-list"},        # slides 非 list
        {"name": "x", "slides": [1, 2]},              # 元素非 str
        {"name": "x", "slides": {"a": 1}},            # dict 不是 list
        {"name": "x", "slides": None},                # None 不是 list
        {"name": "x", "slides": ["a/b.tif"]},         # 路径分隔符
        {"name": "x", "slides": ["a\\b.tif"]},
        {"name": "x", "slides": ["a\x00b.tif"]},      # NUL
        {"name": "x", "slides": ["   "]},             # strip 后为空
        {"name": ""},                                  # 空名
        {},                                            # 缺 name
        {"name": 123},                                 # name 非 str
        {"name": "n" * 61},                            # name 超 60
        {"name": "x", "note": "n" * 201},              # note 超 200
        {"name": "x", "note": 5},                      # note 非 str
    ]
    for body in bad_bodies:
        with pytest.raises(pis.PayloadInvalid):
            pis.validate_create_payload(body)

    with pytest.raises(pis.PayloadInvalid):
        pis.validate_create_payload(["not", "a", "dict"])  # body 非 dict

    # —— 边界合法：60/200 恰好达标；slides 缺省 [] ——
    name, note, slides = pis.validate_create_payload(
        {"name": "n" * 60, "note": "x" * 200})
    assert name == "n" * 60
    assert note == "x" * 200
    assert slides == []
    assert pis.validate_create_payload({"name": "  ok  "}) == ("ok", "", [])

    # —— 同名不同键：允许两个项目（不按名去重） ——
    owner = "usr_p02_a"
    r1 = pis.create_project_idempotent(
        name="同名项目", note="", slides=["a.tif"], owner_user_id=owner,
        requester_role="user", idempotency_key="p02-key-1")
    r2 = pis.create_project_idempotent(
        name="同名项目", note="", slides=["a.tif"], owner_user_id=owner,
        requester_role="user", idempotency_key="p02-key-2")
    assert r1["pid"] != r2["pid"]
    assert _projects_of(pg_uri, owner) == 2
    assert _scalar(pg_uri, "SELECT count(*) FROM project_create_idempotency") == 2

    # —— 无键（旧客户端）：同负载两次 → 两个项目，且不写幂等行 ——
    r3 = pis.create_project_idempotent(
        name="无键项目", note="", slides=["a.tif"], owner_user_id=owner,
        requester_role="user")
    r4 = pis.create_project_idempotent(
        name="无键项目", note="", slides=["a.tif"], owner_user_id=owner,
        requester_role="user")
    assert r3["pid"] != r4["pid"]
    assert "duplicate" not in r3 and "duplicate" not in r4
    assert _projects_of(pg_uri, owner) == 4
    assert _scalar(pg_uri, "SELECT count(*) FROM project_create_idempotency") == 2


# --------------------------------------------------------------------------- #
# 原子性：项目行与幂等行同事务，绝不单边落地
# --------------------------------------------------------------------------- #
def test_project_and_idempotency_inserts_atomic(pg_uri, monkeypatch):
    owner = "usr_atom_a"
    key = "atom-key-1"

    def _boom(cur, owner_user_id, idempotency_key, payload_sha256, project_id):
        raise RuntimeError("injected idempotency insert failure")

    monkeypatch.setattr(pis, "_insert_idempotency_row", _boom)
    with pytest.raises(RuntimeError):
        pis.create_project_idempotent(
            name="原子性项目", note="", slides=["a.tif"], owner_user_id=owner,
            requester_role="user", idempotency_key=key)

    assert _projects_of(pg_uri, owner) == 0, "幂等行失败时项目行必须回滚"
    assert _scalar(
        pg_uri, "SELECT count(*) FROM project_create_idempotency") == 0

    # 回滚后同键可正常重试成功（前次未留下任何半成品）
    monkeypatch.undo()
    proj = pis.create_project_idempotent(
        name="原子性项目", note="", slides=["a.tif"], owner_user_id=owner,
        requester_role="user", idempotency_key=key)
    assert _projects_of(pg_uri, owner) == 1
    assert _scalar(
        pg_uri, "SELECT count(*) FROM project_create_idempotency") == 1
    assert _scalar(pg_uri,
                   "SELECT payload_sha256 FROM project_create_idempotency "
                   "WHERE owner_user_id=%s AND idempotency_key=%s",
                   (owner, key)) == pis.canonical_payload_digest(
        "原子性项目", "", ["a.tif"])


# --------------------------------------------------------------------------- #
# 摘要：slides 保序（顺序是负载语义）
# --------------------------------------------------------------------------- #
def test_digest_preserves_slide_order():
    a = pis.canonical_payload_digest("n", "", ["a.tif", "b.tif"])
    b = pis.canonical_payload_digest("n", "", ["b.tif", "a.tif"])
    assert a != b
    assert a == pis.canonical_payload_digest("n", "", ["a.tif", "b.tif"])


# --------------------------------------------------------------------------- #
# guest：两条路径（无键/有键）均仓储边界拒绝
# --------------------------------------------------------------------------- #
def test_guest_write_rejected_on_both_paths():
    with pytest.raises(PermissionError):
        pis.create_project_idempotent(
            name="guest 无键", owner_user_id="usr_guest",
            requester_role="guest")
    with pytest.raises(PermissionError):
        pis.create_project_idempotent(
            name="guest 有键", owner_user_id="usr_guest",
            requester_role="guest", idempotency_key="g-key")


# --------------------------------------------------------------------------- #
# HTTP 装配助手：400/409/403/200 状态码映射（供协调者接线 app.py）
# --------------------------------------------------------------------------- #
def test_http_helper_status_mapping():
    import project_create_http as pch

    ident = {"user_id": "usr_http_a", "role": "user"}
    key = "http-key-1"

    body, status = pch.handle_create(ident, {"name": "x", "slides": "oops"}, key)
    assert status == 400 and "error" in body

    body, status = pch.handle_create({"user_id": "u", "role": "guest"},
                                     {"name": "x"}, key)
    assert status == 403

    body, status = pch.handle_create(ident, {"name": "HTTP 项目"}, key)
    assert status == 200 and body["pid"].startswith("prj_")
    assert "duplicate" not in body
    pid = body["pid"]

    # 同键同负载重放：仍 200 同 pid，且对外形状与首次一致（无 duplicate 标记）
    body2, status2 = pch.handle_create(ident, {"name": "HTTP 项目"}, key)
    assert status2 == 200 and body2["pid"] == pid
    assert "duplicate" not in body2

    # 同键不同负载 → 409
    body3, status3 = pch.handle_create(ident, {"name": "换了负载"}, key)
    assert status3 == 409 and body3.get("code") == "idempotency_key_conflict"
