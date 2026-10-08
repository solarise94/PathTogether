# -*- coding: utf-8 -*-
"""用户页分类/排序/最近登录验收（docs/admin-viewer-simplified-20261008.md §2 / §8-6）。

覆盖：
  - kind 筛选（real 默认 / dogfood / all）+ 切换词表校验；
  - joined_desc（默认）/ joined_asc / last_login_desc（NULL 恒排末尾）排序；
  - 分页无重复无遗漏（排序全量完成后 offset 切片）；
  - account_kind / last_login_at 字段（格式与 created_at 相同）；
  - POST /api/admin/v1/users/<id>/account-kind：owner 权限（普通用户 403）、
    值校验、审计（含前后值）、未变不写审计、不改 session/额度/角色/启用；
  - last_login_at 只在登录成功更新（行为层细节见 test_registration_retirement）。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import psycopg  # noqa: E402
import pytest  # noqa: E402

import _bootstrap  # noqa: E402,F401  # session 目录+openslide stub（conftest 先行）
from _pt_helpers import csrf_client, isolate_app  # noqa: E402

import app as app_mod  # noqa: E402
import share_store  # noqa: E402
import user_store  # noqa: E402


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    isolate_app(monkeypatch, _bootstrap.SHARE_DATA_DIR, clear_stores=True)
    monkeypatch.setattr(app_mod, "AUTH_ENABLED", True)
    yield


def _client():
    app_mod.app.config["TESTING"] = True
    return csrf_client(app_mod.app.test_client())


def _login(client, user):
    with client.session_transaction() as s:
        s.update({"auth_user": user.get("login_id") or "u",
                  "user_id": user["user_id"],
                  "role": user.get("role") or "user",
                  "auth_version": user.get("auth_version", 1)})
    return client


def _mk(users_spec):
    """建号：[(login, role, delay)]——delay 控制创建时间先后。"""
    import time
    out = []
    for login, role in users_spec:
        out.append(user_store.create_user(login, "userpass12345678",
                                          role=role))
        import time as _t
        _t.sleep(0.01)
    return out


def _sql(query, params=(), fetch=False):
    conn = psycopg.connect(os.environ["DATABASE_URL"])
    conn.row_factory = psycopg.rows.dict_row
    try:
        with conn.cursor() as cur:
            cur.execute(query, params)
            rows = cur.fetchall() if fetch else None
        conn.commit()
        return rows
    finally:
        conn.close()


def _users(client, qs=""):
    r = client.get("/api/admin/v1/users?limit=100" + qs)
    assert r.status_code == 200, r.get_data(as_text=True)
    return r.get_json()["items"]


def test_kind_filter_and_word_table():
    owner = user_store.create_user("owner@x.com", "ownerpass12345678",
                                   role="owner")
    real1 = user_store.create_user("r1@x.com", "userpass12345678")
    real2 = user_store.create_user("r2@x.com", "userpass12345678")
    dog = user_store.create_user("dog@x.com", "userpass12345678")
    user_store.set_user_account_kind(dog["user_id"], "dogfood")
    c = _login(_client(), owner)

    # 默认 kind=real：owner 也属 real
    ids = {i["user_id"] for i in _users(c)}
    assert dog["user_id"] not in ids
    assert real1["user_id"] in ids and real2["user_id"] in ids
    assert owner["user_id"] in ids
    assert all(i["account_kind"] == "real" for i in _users(c))

    # kind=dogfood
    ids = {i["user_id"] for i in _users(c, "&kind=dogfood")}
    assert ids == {dog["user_id"]}
    assert _users(c, "&kind=dogfood")[0]["account_kind"] == "dogfood"

    # kind=all
    ids = {i["user_id"] for i in _users(c, "&kind=all")}
    assert ids == {owner["user_id"], real1["user_id"], real2["user_id"],
                   dog["user_id"]}

    # 非法值 400
    r = c.get("/api/admin/v1/users?kind=bogus")
    assert r.status_code == 400
    assert r.get_json()["error"]["code"] == "invalid_request"
    r = c.get("/api/admin/v1/users?sort=bogus")
    assert r.status_code == 400


def test_sort_orders_and_null_last():
    owner = user_store.create_user("owner@x.com", "ownerpass12345678",
                                   role="owner")
    u1 = user_store.create_user("u1@x.com", "userpass12345678")
    u2 = user_store.create_user("u2@x.com", "userpass12345678")
    u3 = user_store.create_user("u3@x.com", "userpass12345678")
    # u1/u2 登录（u1 先）；u3 从未登录
    c = _client()
    assert c.post("/login", data={"username": "u1@x.com",
                                  "password": "userpass12345678"}) \
        .status_code == 302
    import time
    time.sleep(0.05)
    assert c.post("/login", data={"username": "u2@x.com",
                                  "password": "userpass12345678"}) \
        .status_code == 302
    oc = _login(_client(), owner)

    # 默认 joined_desc：新→旧（owner 最先建 → 排最后；user_id 稳定次键）
    items = _users(oc)
    created = [i["created_at"] for i in items]
    assert created == sorted(created, reverse=True)

    # joined_asc
    items = _users(oc, "&sort=joined_asc")
    created = [i["created_at"] for i in items]
    assert created == sorted(created)

    # last_login_desc：u2（最近）→ u1 → NULL（owner + u3）恒排末尾
    items = _users(oc, "&sort=last_login_desc")
    logins = [(i["user_id"], i["last_login_at"]) for i in items]
    non_null = [x for x in logins if x[1] is not None]
    nulls = [x for x in logins if x[1] is None]
    assert [x[0] for x in non_null] == [u2["user_id"], u1["user_id"]]
    assert {x[0] for x in nulls} == {owner["user_id"], u3["user_id"]}
    assert len(non_null) + len(nulls) == 4
    # last_login_at 与 created_at 同格式（epoch float）
    assert isinstance(non_null[0][1], float)


def test_pagination_no_dup_no_gap_across_sorts():
    owner = user_store.create_user("owner@x.com", "ownerpass12345678",
                                   role="owner")
    for i in range(7):
        user_store.create_user("p%d@x.com" % i, "userpass12345678")
    c = _login(_client(), owner)
    for sort in ("joined_desc", "joined_asc", "last_login_desc"):
        for kind in ("real", "all"):
            seen = []
            cursor = None
            while True:
                url = "/api/admin/v1/users?limit=3&sort=%s&kind=%s" \
                    % (sort, kind)
                if cursor:
                    url += "&cursor=" + cursor
                r = c.get(url)
                assert r.status_code == 200
                body = r.get_json()
                seen.extend(i["user_id"] for i in body["items"])
                cursor = body["next_cursor"]
                if not cursor:
                    break
            expected = 8  # owner（real）+ 7 个 real 用户
            assert len(seen) == expected, (sort, kind, seen)
            assert len(set(seen)) == expected, (sort, kind)


def test_account_kind_endpoint_permission_audit_and_semantics():
    owner = user_store.create_user("owner@x.com", "ownerpass12345678",
                                   role="owner")
    target = user_store.create_user("target@x.com", "userpass12345678")
    c = _login(_client(), owner)
    url = "/api/admin/v1/users/%s/account-kind" % target["user_id"]

    # 值校验
    r = c.post(url, json={"account_kind": "bogus"})
    assert r.status_code == 400
    assert r.get_json()["error"]["code"] == "invalid_request"

    # 变化：写审计（前后值）
    r = c.post(url, json={"account_kind": "dogfood"})
    assert r.status_code == 200, r.get_data(as_text=True)
    body = r.get_json()
    assert body == {"user_id": target["user_id"], "account_kind": "dogfood",
                    "changed": True}
    ev = _sql("SELECT detail FROM audit_events WHERE "
              "action='admin.user.account_kind' AND target_id=%s "
              "ORDER BY ts DESC LIMIT 1", (target["user_id"],),
              fetch=True)[0]["detail"]
    assert ev["from"] == "real" and ev["to"] == "dogfood"

    # 未变：不写审计
    n_before = _sql("SELECT count(*)::int AS n FROM audit_events WHERE "
                    "action='admin.user.account_kind'",
                    fetch=True)[0]["n"]
    r2 = c.post(url, json={"account_kind": "dogfood"})
    assert r2.status_code == 200
    assert r2.get_json()["changed"] is False
    n_after = _sql("SELECT count(*)::int AS n FROM audit_events WHERE "
                   "action='admin.user.account_kind'",
                   fetch=True)[0]["n"]
    assert n_after == n_before

    # 不改 session/额度/角色/启用状态
    after = user_store.get_user(target["user_id"])
    before = user_store.get_user(target["user_id"])
    assert after["disabled"] is False
    assert after["role"] == "user"
    assert after["auth_version"] == target["auth_version"]
    assert after["ai_access"] == target["ai_access"]

    # 改回 real：再写一条审计
    r3 = c.post(url, json={"account_kind": "real"})
    assert r3.status_code == 200
    assert r3.get_json()["changed"] is True
    ev2 = _sql("SELECT detail FROM audit_events WHERE "
               "action='admin.user.account_kind' AND target_id=%s "
               "ORDER BY ts DESC LIMIT 1", (target["user_id"],),
               fetch=True)[0]["detail"]
    assert ev2["from"] == "dogfood" and ev2["to"] == "real"

    # 普通用户调用被拒（owner 门控 403）
    pleb = user_store.create_user("pleb@x.com", "userpass12345678")
    pc = _login(_client(), pleb)
    r = pc.post("/api/admin/v1/users/%s/account-kind" % target["user_id"],
                json={"account_kind": "dogfood"})
    assert r.status_code == 403
    # 匿名 401
    assert _client().post(url, json={"account_kind": "dogfood"}) \
        .status_code == 401
    # 未知用户 404
    r = c.post("/api/admin/v1/users/usr_nope/account-kind",
               json={"account_kind": "real"})
    assert r.status_code == 404
