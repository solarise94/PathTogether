# -*- coding: utf-8 -*-
"""PR4 用户来源归因测试（docs/admin-billing-plugin-implementation-plan.md
§11/§14.1「来源」「隐私」「降级」行）。

json 模式（无 PG，双跑段）：
  - 纯清理函数：UTM 控制字符/限长、referrer 只留 hostname、slug 边界；
  - IP 前缀 hash：盐缺失不存、盐存在同前缀同 hash/不同前缀不同 hash、
    IPv4 /24 / IPv6 /48 分组、盐轮换换 hash；
  - /r/ 路由：合法 slug 302 allowlist + 签名 cookie（HttpOnly/SameSite）；
    恶意/超长 slug 安全兜底 302 /；?next=evil 与 ?to=evil 被拒；cookie
    篡改/过期换新 visitor_id；json 后端不 500（§16.2 降级）；
  - admin v1 acquisition：owner 门控（匿名 401 / user 403）与 json 后端
    503 pg_backend_required。

PG 模式（RUN_PG_TESTS=1）：
  - 触点行：不可变行粒度、未知 campaign 落 NULL 不报错、active campaign 关联、
    paused 不关联；referrer/UTM/landing/IP hash 落库形态；
  - 归因四路径全覆盖（invite campaign > pt_acq 触点 > referrer/UTM > direct）；
  - first/last touch：同一访客两次不同 campaign 不折叠；
  - 过期触点不参与归因；注册路由全链路（/r/ → cookie → /register POST 事务）；
  - 兑换事务原子性（user_acquisition 写入失败 → 用户不创建，故障注入）；
  - 90 天清理只删未被引用的过期触点；
  - admin API 门控/脱敏（login 掩码、visitor 只给前缀、无完整 IP/query）；
  - 漏斗汇总（访问/注册/首次 AI 计数，首次 AI 直接 SQL 造 ai_usage_events）。

运行：cd 项目根 && python3 -m pytest tests/test_acquisition.py -q
（PG 双跑：RUN_PG_TESTS=1 python3 -m pytest tests/test_acquisition.py -q）
"""
import json
import os
import secrets
import sys
import time
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import _bootstrap  # noqa: E402,F401  # session 目录+openslide stub（conftest 先行）
DATA_DIR = _bootstrap.SHARE_DATA_DIR
UPLOAD_DIR = _bootstrap.UPLOAD_DIR
os.environ["ADMIN_PASSWORD"] = ""
import pytest  # noqa: E402

import app as app_mod  # noqa: E402
import acquisition_store as acq_store  # noqa: E402
import registration_store  # noqa: E402
import user_store  # noqa: E402
from _pt_helpers import csrf_client, isolate_app  # noqa: E402
from pg_compat import BACKEND  # noqa: E402

pg_only = pytest.mark.skipif(
    BACKEND != "postgres",
    reason="acquisition 数据层需 PG（RUN_PG_TESTS=1）",
)


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    """每用例：json 路径隔离 + 清理 acquisition 相关 env/cookie。"""
    isolate_app(monkeypatch, DATA_DIR, clear_stores=True)
    monkeypatch.setattr(app_mod, "_registration_gate_warned", {"flag": False})
    for name in ("PUBLIC_BASE_URL", "ADMIN_SESSION_COOKIE_SECURE",
                 "ACQ_IP_SALT"):
        monkeypatch.delenv(name, raising=False)
    yield


def _raw_client(auth=True):
    app_mod.app.config["TESTING"] = True
    app_mod.AUTH_ENABLED = auth
    return app_mod.app.test_client()


def _client(auth=True):
    return csrf_client(_raw_client(auth))


def _satisfy_preconditions(monkeypatch):
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://path.example.com")
    monkeypatch.setenv("ADMIN_SESSION_COOKIE_SECURE", "1")


def _mk_owner():
    return user_store.create_user("acq-owner@x.com", "ownerpass123456",
                                  role="owner")


def _owner_session(client, owner):
    with client.session_transaction() as s:
        s.update({"auth_user": "o", "user_id": owner["user_id"],
                  "role": "owner", "auth_version": owner.get("auth_version", 1)})


def _cookie_payload_from(client):
    """从 test client 读 pt_acq 并验签 → payload dict|None。"""
    c = client.get_cookie("pt_acq", domain="localhost", path="/")
    raw = c.value if c is not None else None
    if not raw:
        return None
    import itsdangerous
    try:
        data = app_mod._acq_cookie_serializer().loads(
            raw, max_age=app_mod.ACQ_COOKIE_TTL_SECONDS)
    except itsdangerous.BadData:
        return None
    return data if isinstance(data, dict) else None


def _mk_invite(owner_uid, login_id=None, **kw):
    return registration_store.create_invite(owner_uid, login_id=login_id, **kw)


def _redeem(inv, login_id, acq=None):
    return registration_store.redeem_invite(
        inv["token"], login_id, "longpassword123", acq=acq)


# =========================================================================== #
# 1. 纯清理函数（json/PG 双跑）
# =========================================================================== #
def test_sanitize_text_strips_control_chars_and_truncates():
    assert acq_store.sanitize_text("  abc  ") == "abc"
    assert acq_store.sanitize_text("a\x00b\x1fc\x7fd") == "abcd"
    assert acq_store.sanitize_text("x" * 300) == "x" * acq_store.MAX_UTM_LEN
    assert acq_store.sanitize_text(None) == ""
    assert acq_store.sanitize_text(123) == ""


def test_sanitize_referrer_keeps_hostname_only():
    assert acq_store.sanitize_referrer_domain(
        "https://mywebpage.example.com/path?q=secret#frag") == \
        "mywebpage.example.com"
    assert acq_store.sanitize_referrer_domain(
        "http://User:pw@Blog.Example.com:8443/x?a=b") == "blog.example.com"
    # 协议相对/裸串解析不出 hostname → 空（绝不落可能带 query 的原文）
    assert acq_store.sanitize_referrer_domain("not a url ?q=1") == ""
    assert acq_store.sanitize_referrer_domain("") == ""
    assert acq_store.sanitize_referrer_domain(None) == ""


def test_valid_slug_boundaries():
    assert acq_store.valid_slug("mywebpage")
    assert acq_store.valid_slug("a-b_c9")
    assert acq_store.valid_slug("x" * 64)
    for bad in ("", "-abc", "ABC", "a b", "a/b", "x" * 65, None, 123,
                "a.b", "中文"):
        assert not acq_store.valid_slug(bad), bad


def test_ip_prefix_hash_salt_and_prefix_semantics(monkeypatch):
    # 盐缺失 → 空串（不采集，§11.3 实现决策）
    monkeypatch.delenv("ACQ_IP_SALT", raising=False)
    assert acq_store.ip_prefix_hash("203.0.113.7") == ""
    assert acq_store.ip_prefix_hash("") == ""

    monkeypatch.setenv("ACQ_IP_SALT", "salt-1")
    # IPv4 /24：同前缀同 hash，跨前缀不同
    h1 = acq_store.ip_prefix_hash("203.0.113.7")
    h2 = acq_store.ip_prefix_hash("203.0.113.200")
    h3 = acq_store.ip_prefix_hash("203.0.114.7")
    assert h1 and h1 == h2
    assert h1 != h3
    # IPv6 /48 分组
    v6a = acq_store.ip_prefix_hash("2001:db8:1:2::1")
    v6b = acq_store.ip_prefix_hash("2001:db8:1:ffff::9")
    v6c = acq_store.ip_prefix_hash("2001:db8:2::1")
    assert v6a == v6b and v6a != v6c
    # 非法 IP → 空串（不落原文）
    assert acq_store.ip_prefix_hash("not-an-ip") == ""
    # 盐轮换 → 同 IP 不同 hash（旧 hash 自然失效）
    monkeypatch.setenv("ACQ_IP_SALT", "salt-2")
    assert acq_store.ip_prefix_hash("203.0.113.7") != h1


def test_visitor_id_hash_is_salted_domain_separated():
    a = acq_store.visitor_id_hash("visitor-one")
    b = acq_store.visitor_id_hash("visitor-one")
    c = acq_store.visitor_id_hash("visitor-two")
    assert a == b and a != c
    assert len(a) == 64
    vid = acq_store.new_visitor_id()
    assert acq_store.valid_visitor_id(vid)
    assert not acq_store.valid_visitor_id("short")


# =========================================================================== #
# 2. /r/ 路由（json/PG 双跑；json 后端 = §16.2 安全降级）
# =========================================================================== #
def test_r_valid_slug_sets_cookie_and_redirects():
    app_mod.AUTH_ENABLED = True
    client = _client()
    r = client.get("/r/mywebpage?campaign=summer24&utm_medium=cta")
    assert r.status_code == 302
    assert r.headers["Location"] == "/register"  # 缺省 landing
    cookie = client.get_cookie("pt_acq", domain="localhost", path="/")
    assert cookie is not None
    payload = _cookie_payload_from(client)
    assert acq_store.valid_visitor_id(payload["v"])
    assert payload["uc"] == "summer24"
    assert payload["um"] == "cta"


def test_r_landing_allowlist_and_open_redirect_rejected():
    app_mod.AUTH_ENABLED = True
    client = _client()
    # to 只接受 allowlist 精确匹配
    assert client.get("/r/src1?to=/demo").headers["Location"] == "/demo"
    assert client.get("/r/src1?to=/").headers["Location"] == "/"
    # 任意外部/相对 trick 一律回缺省 /register
    for bad in ("https://evil.example.com", "//evil.example.com",
                "/\\evil.example.com", "/admin", "/login?next=/x",
                "/demo/../../admin", "demo"):
        loc = client.get("/r/src1?to=" + bad).headers["Location"]
        assert loc == "/register", (bad, loc)
    # next/redirect 参数从不被读取（忽略，不影响目标）
    assert client.get(
        "/r/src1?next=https://evil.example.com&redirect=/admin"
    ).headers["Location"] == "/register"


@pytest.mark.parametrize("slug", [
    "Bad-Slug", "a b", "a/b", "..", "x" * 65, "%41bc", "中文",
])
def test_r_malicious_slug_safe_fallback(slug):
    app_mod.AUTH_ENABLED = True
    client = _client()
    r = client.get("/r/" + slug)
    # 安全兜底：不 500、不泄露判定细节。带 / 的多段路径按 Flask 404
    # （未匹配路由，无信息量）；其余非法 slug 302 到 /。
    assert r.status_code in (302, 404), (slug, r.status_code)
    if r.status_code == 302:
        assert r.headers["Location"] == "/"


def test_r_reuses_valid_cookie_and_rotates_on_tamper(monkeypatch):
    app_mod.AUTH_ENABLED = True
    client = _client()
    client.get("/r/src1")
    v1 = _cookie_payload_from(client)["v"]
    # 有效 cookie：visitor_id 复用（同一访客多次跳转）
    client.get("/r/src2")
    assert _cookie_payload_from(client)["v"] == v1
    # 篡改 cookie → 验签失败 → 新 visitor_id
    raw = client.get_cookie("pt_acq", domain="localhost", path="/").value
    client.set_cookie(
        "pt_acq",
        raw[:-2] + ("aa" if not raw.endswith("aa") else "bb"),
        domain="localhost")
    client.get("/r/src3")
    assert _cookie_payload_from(client)["v"] != v1


def test_r_expired_cookie_gets_new_visitor(monkeypatch):
    app_mod.AUTH_ENABLED = True
    client = _client()
    client.get("/r/src1")
    v1 = _cookie_payload_from(client)["v"]
    # TTL 视为已过（服务端 max_age 校验）：旧 cookie 无效 → 新 visitor
    monkeypatch.setattr(app_mod, "ACQ_COOKIE_TTL_SECONDS", -1)
    client.get("/r/src2")
    monkeypatch.setattr(app_mod, "ACQ_COOKIE_TTL_SECONDS", 90 * 86400)
    v2 = _cookie_payload_from(client)["v"]
    assert v2 != v1


def test_r_json_backend_never_500s():
    if BACKEND == "postgres":
        pytest.skip("json 后端专用降级用例（PG 模式走触点写入正向路径）")
    app_mod.AUTH_ENABLED = True
    client = _client()
    r = client.get("/r/mywebpage?campaign=c1")
    assert r.status_code == 302
    assert r.headers["Location"] == "/register"
    assert _cookie_payload_from(client) is not None


# =========================================================================== #
# 3. admin v1 acquisition 门控 + json fail-closed
# =========================================================================== #
def _plain_user():
    return user_store.create_user("plain-acq@x.com", "userpass1234567",
                                  role="user")


def test_admin_acquisition_owner_gate():
    owner = _mk_owner()
    app_mod.AUTH_ENABLED = True
    client = _client()
    # 匿名 → 401
    for path in ("/api/admin/v1/acquisition/summary",
                 "/api/admin/v1/acquisition/users"):
        r = client.get(path)
        assert r.status_code == 401, path
    # 非 owner → 403
    u = _plain_user()
    with client.session_transaction() as s:
        s.update({"auth_user": "p", "user_id": u["user_id"], "role": "user",
                  "auth_version": u.get("auth_version", 1)})
    for path in ("/api/admin/v1/acquisition/summary",
                 "/api/admin/v1/acquisition/users"):
        r = client.get(path)
        assert r.status_code == 403, path


def test_admin_acquisition_json_backend_pg_required():
    if BACKEND == "postgres":
        pytest.skip("json 后端专用 fail-closed 用例")
    owner = _mk_owner()
    app_mod.AUTH_ENABLED = True
    client = _client()
    _owner_session(client, owner)
    for path in ("/api/admin/v1/acquisition/summary",
                 "/api/admin/v1/acquisition/users"):
        r = client.get(path)
        assert r.status_code == 503, path
        assert r.get_json()["error"]["code"] == "pg_backend_required"


# =========================================================================== #
# 4. PG：触点写入与归因数据层
# =========================================================================== #
if BACKEND == "postgres":
    import psycopg  # noqa: E402
    import pg_store  # noqa: E402

    def _pg_conn():
        c = pg_store.connect()
        c.row_factory = psycopg.rows.dict_row
        return c

    def _seed_campaign(cid, source, status="active"):
        conn = _pg_conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO acquisition_campaigns "
                    "(campaign_id, source_code, name, status, created_by) "
                    "VALUES (%s,%s,%s,%s,'test') "
                    "ON CONFLICT (campaign_id) DO UPDATE SET status=%s",
                    (cid, source, "camp-" + cid, status, status))
            conn.commit()
        finally:
            conn.close()
        return acq_store.get_campaign(cid)

    def _visits(visitor_id):
        conn = _pg_conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT * FROM acquisition_visits WHERE visitor_id_hash="
                    "%s ORDER BY touched_at, acquisition_id",
                    (acq_store.visitor_id_hash(visitor_id),))
                return [dict(r) for r in cur.fetchall()]
        finally:
            conn.close()

    def _ua(user_id):
        conn = _pg_conn()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT * FROM user_acquisition WHERE user_id=%s",
                            (user_id,))
                row = cur.fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    def _expire_all_visits():
        conn = _pg_conn()
        try:
            with conn.cursor() as cur:
                cur.execute("UPDATE acquisition_visits "
                            "SET expires_at = now() - interval '1 second'")
            conn.commit()
        finally:
            conn.close()

    def _insert_usage_event(user_id, hours_back=1):
        """直接 SQL 造一条 ai_usage_events（unpriced，满足表 CHECK）。"""
        conn = _pg_conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO ai_usage_events "
                    "(event_id, call_id, payload_hash, schema_version, "
                    " session_id, subject_type, subject_id, user_id, provider,"
                    " model, occurred_at, enqueued_at, received_at, status, "
                    " unpriced_reason) "
                    "VALUES (%s,%s,%s,1,%s,'user',%s,%s,'deepseek',"
                    "'deepseek-v4-flash', now() - (%s * interval '1 hour'), "
                    " now(), now(), 'unpriced', 'test')",
                    ("use_" + secrets.token_hex(16),
                     "call_" + secrets.token_hex(16), secrets.token_hex(32),
                     "sess_" + secrets.token_hex(10), user_id, user_id,
                     int(hours_back)))
            conn.commit()
        finally:
            conn.close()


@pg_only
def test_record_visit_row_shape_and_sanitization(monkeypatch):
    monkeypatch.setenv("ACQ_IP_SALT", "test-salt")
    _seed_campaign("camp-active", "mywebpage")
    vid = acq_store.new_visitor_id()
    row = acq_store.record_visit(
        visitor_id=vid, source_code="mywebpage", campaign_id="camp-active",
        referrer_domain="https://mywebpage.example.com/lp?utm=x",
        landing_path="/register",
        utm_source="ctrl\x00chars\x1f", utm_medium="m" * 300,
        utm_campaign="summer\x7f24", ip="203.0.113.55")
    assert row["campaign_id"] == "camp-active"
    assert row["referrer_domain"] == "mywebpage.example.com"  # 只留 hostname
    assert row["utm_source"] == "ctrlchars"
    assert row["utm_medium"] == "m" * 128                     # 限长
    assert row["utm_campaign"] == "summer24"                  # 控制字符清理
    assert row["ip_prefix_hash"] == acq_store.ip_prefix_hash("203.0.113.55")
    assert "203.0.113" not in row["ip_prefix_hash"]
    # 库内无明文 IP / 完整 referrer / visitor 明文
    visits = _visits(vid)
    assert len(visits) == 1
    v = visits[0]
    assert v["visitor_id_hash"] == acq_store.visitor_id_hash(vid)
    assert vid not in json.dumps(v, default=str)
    assert v["expires_at"] > v["touched_at"]


@pg_only
def test_record_visit_unknown_campaign_null_no_error():
    _seed_campaign("camp-paused", "src", status="paused")
    vid = acq_store.new_visitor_id()
    # 未知 slug / 非 active → campaign_id 落 NULL 不报错；slug 保留在 utm_campaign
    for cid in ("no-such-campaign", "camp-paused", "Bad Slug!"):
        row = acq_store.record_visit(
            visitor_id=vid, source_code="s", campaign_id=cid,
            utm_campaign=str(cid))
        assert row["campaign_id"] is None
    # 非法 slug 的 landing → 空串
    row = acq_store.record_visit(visitor_id=vid, source_code="s",
                                 landing_path="/admin")
    assert row["landing_path"] == ""


@pg_only
def test_visit_rows_are_immutable_events_not_upserted():
    vid = acq_store.new_visitor_id()
    acq_store.record_visit(visitor_id=vid, source_code="a")
    acq_store.record_visit(visitor_id=vid, source_code="a")
    acq_store.record_visit(visitor_id=vid, source_code="b")
    visits = _visits(vid)
    assert len(visits) == 3  # 每次跳转一行，不折叠（§11.2 行粒度）
    assert {v["source_code"] for v in visits} == {"a", "b"}


@pg_only
def test_attribution_priority_invite_campaign_first():
    owner = _mk_owner()
    _seed_campaign("camp-inv", "invsrc")
    _seed_campaign("camp-web", "websrc")
    vid = acq_store.new_visitor_id()
    acq_store.record_visit(visitor_id=vid, source_code="websrc",
                           campaign_id="camp-web")
    inv = _mk_invite(owner["user_id"], campaign_id="camp-inv")
    out = _redeem(inv, "prio1@x.com",
                  acq={"visitor_id": vid, "utm_source": "ignored"})
    ua = _ua(out["user"]["user_id"])
    assert ua["attribution_method"] == "invite_campaign"
    assert ua["campaign_id"] == "camp-inv"
    assert ua["source_code"] == "invsrc"
    # 触点仍保留 first/last id（完整路径不丢）
    assert ua["first_acquisition_id"] is not None
    assert ua["last_acquisition_id"] is not None
    assert ua["invite_id"] == inv["invite_id"]


@pg_only
def test_attribution_priority_invite_source_then_visit():
    owner = _mk_owner()
    _seed_campaign("camp-web", "websrc")
    vid = acq_store.new_visitor_id()
    acq_store.record_visit(visitor_id=vid, source_code="websrc",
                           campaign_id="camp-web")
    # 邀请只有 source（无 campaign）→ 仍高于触点
    inv = _mk_invite(owner["user_id"], source_code="invsrc2")
    out = _redeem(inv, "prio2@x.com", acq={"visitor_id": vid})
    ua = _ua(out["user"]["user_id"])
    assert ua["attribution_method"] == "invite_source"
    assert ua["source_code"] == "invsrc2"
    assert ua["campaign_id"] is None
    # 无邀请来源 → 触点路径（last touch 记 source/campaign）
    inv2 = _mk_invite(owner["user_id"])
    out2 = _redeem(inv2, "prio3@x.com", acq={"visitor_id": vid})
    ua2 = _ua(out2["user"]["user_id"])
    assert ua2["attribution_method"] == "visit"
    assert ua2["source_code"] == "websrc"
    assert ua2["campaign_id"] == "camp-web"


@pg_only
def test_attribution_first_last_touch_across_campaigns_not_collapsed():
    owner = _mk_owner()
    _seed_campaign("camp-a", "srca")
    _seed_campaign("camp-b", "srcb")
    vid = acq_store.new_visitor_id()
    v1 = acq_store.record_visit(visitor_id=vid, source_code="srca",
                                campaign_id="camp-a")
    time.sleep(0.01)
    v2 = acq_store.record_visit(visitor_id=vid, source_code="srcb",
                                campaign_id="camp-b")
    inv = _mk_invite(owner["user_id"])
    out = _redeem(inv, "fl@x.com", acq={"visitor_id": vid})
    ua = _ua(out["user"]["user_id"])
    assert ua["first_acquisition_id"] == v1["acquisition_id"]
    assert ua["last_acquisition_id"] == v2["acquisition_id"]
    assert ua["source_code"] == "srcb"  # last touch（转化触点）
    assert ua["campaign_id"] == "camp-b"


@pg_only
def test_attribution_priority_referrer_utm_then_direct():
    owner = _mk_owner()
    _seed_campaign("camp-uc", "ucsrc")
    # utm_campaign 命中已知 active campaign → campaign 归因
    inv = _mk_invite(owner["user_id"])
    out = _redeem(inv, "ru1@x.com", acq={
        "utm_campaign": "camp-uc", "utm_source": "ignored"})
    ua = _ua(out["user"]["user_id"])
    assert ua["attribution_method"] == "referrer_utm"
    assert ua["campaign_id"] == "camp-uc" and ua["source_code"] == "ucsrc"
    # 无 utm_campaign → utm_source slug
    inv2 = _mk_invite(owner["user_id"])
    out2 = _redeem(inv2, "ru2@x.com", acq={"utm_source": "newsletter"})
    ua2 = _ua(out2["user"]["user_id"])
    assert ua2["attribution_method"] == "referrer_utm"
    assert ua2["source_code"] == "newsletter" and ua2["campaign_id"] is None
    # 再无 utm_source → referrer hostname（不是原始 query）
    inv3 = _mk_invite(owner["user_id"])
    out3 = _redeem(inv3, "ru3@x.com", acq={
        "referrer_domain": "https://blog.example.com/lp?x=1"})
    ua3 = _ua(out3["user"]["user_id"])
    assert ua3["attribution_method"] == "referrer_utm"
    assert ua3["source_code"] == "blog.example.com"
    # 无任何信号 → direct/unknown（acq=None 老调用兼容）
    inv4 = _mk_invite(owner["user_id"])
    out4 = _redeem(inv4, "ru4@x.com")
    ua4 = _ua(out4["user"]["user_id"])
    assert ua4["attribution_method"] == "direct"
    assert ua4["source_code"] == "unknown"


@pg_only
def test_expired_visits_not_used_for_attribution():
    owner = _mk_owner()
    vid = acq_store.new_visitor_id()
    acq_store.record_visit(visitor_id=vid, source_code="expired-src")
    _expire_all_visits()
    inv = _mk_invite(owner["user_id"])
    out = _redeem(inv, "exp@x.com", acq={"visitor_id": vid})
    ua = _ua(out["user"]["user_id"])
    assert ua["attribution_method"] == "direct"  # 过期触点不参与
    assert ua["first_acquisition_id"] is None
    assert ua["last_acquisition_id"] is None


@pg_only
def test_cookie_tampered_visitor_not_matched():
    owner = _mk_owner()
    vid = acq_store.new_visitor_id()
    acq_store.record_visit(visitor_id=vid, source_code="s")
    # visitor_id 与 cookie 不匹配（另一访客）→ 不归因到该触点
    other = acq_store.new_visitor_id()
    assert other != vid
    inv = _mk_invite(owner["user_id"])
    out = _redeem(inv, "tamper@x.com", acq={"visitor_id": other})
    ua = _ua(out["user"]["user_id"])
    assert ua["attribution_method"] == "direct"


@pg_only
def test_redeem_atomic_user_acquisition_failure_rolls_back(monkeypatch):
    """故障注入：user_acquisition 写入失败 → 用户不创建、邀请不消费。"""
    owner = _mk_owner()
    inv = _mk_invite(owner["user_id"], login_id="boom@x.com")

    def _boom(*a, **kw):
        raise RuntimeError("injected acquisition failure")

    monkeypatch.setattr(acq_store, "insert_user_acquisition", _boom)
    with pytest.raises(RuntimeError):
        registration_store.redeem_invite(inv["token"], "boom@x.com",
                                         "longpassword123",
                                         acq={"utm_source": "x"})
    assert user_store.get_user_by_login_id("boom@x.com") is None
    row = registration_store.get_invite(inv["invite_id"])
    assert row["use_count"] == 0 and row["consumed_at"] is None
    # 无 user_acquisition 残留
    conn = _pg_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT count(*)::int AS n FROM user_acquisition")
            assert cur.fetchone()["n"] == 0
    finally:
        conn.close()
    # 注入解除后同一邀请可正常兑换（含归因）
    monkeypatch.undo()
    out = _redeem(inv, "boom@x.com", acq={"utm_source": "x"})
    assert _ua(out["user"]["user_id"])["source_code"] == "x"


@pg_only
def test_cleanup_expired_visits_keeps_referenced_rows():
    owner = _mk_owner()
    vid = acq_store.new_visitor_id()
    referenced = acq_store.record_visit(visitor_id=vid, source_code="keep")
    stale = acq_store.record_visit(visitor_id=acq_store.new_visitor_id(),
                                   source_code="drop-me")
    fresh = acq_store.record_visit(visitor_id=acq_store.new_visitor_id(),
                                   source_code="fresh")
    inv = _mk_invite(owner["user_id"])
    _redeem(inv, "clean@x.com", acq={"visitor_id": vid})
    # 只把 stale 与 referenced 标过期
    conn = _pg_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE acquisition_visits SET expires_at="
                "now() - interval '1 second' WHERE acquisition_id IN (%s,%s)",
                (referenced["acquisition_id"], stale["acquisition_id"]))
        conn.commit()
    finally:
        conn.close()
    deleted = acq_store.cleanup_expired_visits()
    assert deleted >= 1
    conn = _pg_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT acquisition_id FROM acquisition_visits")
            left = {r["acquisition_id"] for r in cur.fetchall()}
    finally:
        conn.close()
    # 已归因的过期触点保留（长期保留 campaign/source）；未过期保留
    assert referenced["acquisition_id"] in left
    assert fresh["acquisition_id"] in left
    assert stale["acquisition_id"] not in left


@pg_only
def test_register_route_full_acquisition_flow(monkeypatch):
    """全链路：/r/ 设 cookie → 落触点 → /register POST 事务内归因。"""
    _satisfy_preconditions(monkeypatch)
    _seed_campaign("camp-flow", "mywebpage")
    owner = _mk_owner()
    app_mod.AUTH_ENABLED = True
    client = _client()
    _owner_session(client, owner)
    assert client.put("/api/admin/settings/registration",
                      json={"mode": "invite_only"}).status_code == 200
    inv = client.post("/api/admin/registration-invites",
                      json={"login_id": "flow-acq@x.com"}).get_json()
    anon = _client()
    # 访客从 mywebpage CTA 进入（campaign 已登记 → 触点关联；utm_medium 一并落）
    r = anon.get("/r/mywebpage?campaign=camp-flow&utm_medium=cta&next=evil")
    assert r.status_code == 302 and r.headers["Location"] == "/register"
    anon.get("/register")
    r2 = anon.post("/register", data={
        "invite_token": inv["token"], "login_id": "flow-acq@x.com",
        "password": "longpassword123", "password_confirm": "longpassword123"})
    assert r2.status_code == 302, r2.get_data(as_text=True)
    user = user_store.get_user_by_login_id("flow-acq@x.com")
    ua = _ua(user["user_id"])
    # 邀请无 campaign/source → 走触点路径（last touch = mywebpage/camp-flow）
    assert ua["attribution_method"] == "visit"
    assert ua["source_code"] == "mywebpage"
    assert ua["campaign_id"] == "camp-flow"
    assert ua["invite_id"] == inv["invite_id"]
    # 邀请码绝不进 URL/query（CTA 与注册请求都只有 POST body 携带）
    assert inv["token"] not in r2.get_data(as_text=True)


@pg_only
def test_admin_summary_funnel_counts_with_first_ai():
    owner = _mk_owner()
    _seed_campaign("camp-f1", "srcf1")
    _seed_campaign("camp-f2", "srcf2")
    # 访客 A：两次不同 campaign 跳转（first=c1 last=c2）→ 注册 → 有 AI 事件
    vid_a = acq_store.new_visitor_id()
    acq_store.record_visit(visitor_id=vid_a, source_code="srcf1",
                           campaign_id="camp-f1")
    time.sleep(0.01)
    acq_store.record_visit(visitor_id=vid_a, source_code="srcf2",
                           campaign_id="camp-f2")
    out_a = _redeem(_mk_invite(owner["user_id"]), "funa@x.com",
                    acq={"visitor_id": vid_a})
    _insert_usage_event(out_a["user"]["user_id"], hours_back=2)
    # 访客 B：仅 c1 跳转 → 注册 → 无 AI 事件
    vid_b = acq_store.new_visitor_id()
    acq_store.record_visit(visitor_id=vid_b, source_code="srcf1",
                           campaign_id="camp-f1")
    _redeem(_mk_invite(owner["user_id"]), "funb@x.com",
            acq={"visitor_id": vid_b})
    # 访客 C：仅跳转未注册
    acq_store.record_visit(visitor_id=acq_store.new_visitor_id(),
                           source_code="srcf2", campaign_id="camp-f2")

    summary = acq_store.admin_funnel_summary()
    rows = {(r["source_code"], r["campaign_id"]): r for r in summary["items"]}
    c1 = rows[("srcf1", "camp-f1")]
    c2 = rows[("srcf2", "camp-f2")]
    assert c1["visits"] == 2 and c1["visitors"] == 2
    assert c1["registrations"] == 1        # B 归因到 c1（last touch）
    assert c1["first_ai_count"] == 0
    assert c2["visits"] == 2 and c2["visitors"] == 2
    assert c2["registrations"] == 1        # A 归因到 c2（last touch）
    assert c2["first_ai_count"] == 1
    assert summary["totals"]["visits"] == 4
    assert summary["totals"]["registrations"] == 2
    assert summary["totals"]["first_ai_count"] == 1
    assert {c["campaign_id"] for c in summary["campaigns"]} >= \
        {"camp-f1", "camp-f2"}


@pg_only
def test_admin_users_endpoint_pagination_and_masking(monkeypatch):
    """明细分页 + 脱敏（login 掩码、visitor 前缀、first/last 分列）。"""
    _satisfy_preconditions(monkeypatch)
    owner = _mk_owner()
    app_mod.AUTH_ENABLED = True
    vid = acq_store.new_visitor_id()
    first = acq_store.record_visit(visitor_id=vid, source_code="s1",
                                   referrer_domain="https://a.example.com/x?q=1")
    time.sleep(0.01)
    last = acq_store.record_visit(visitor_id=vid, source_code="s2")
    out = registration_store.redeem_invite(
        _mk_invite(owner["user_id"], login_id="Maskme@x.com")["token"],
        "maskme@x.com", "longpassword123", "Masked User",
        acq={"visitor_id": vid})
    # 第二个归因用户（direct）制造分页
    out2 = _redeem(_mk_invite(owner["user_id"]), "second-acq@x.com")
    client = _client()
    _owner_session(client, owner)

    seen = []
    cursor = None
    pages = 0
    while True:
        url = "/api/admin/v1/acquisition/users?limit=1"
        if cursor:
            url += "&cursor=" + cursor
        r = client.get(url)
        assert r.status_code == 200, r.get_data(as_text=True)
        body = r.get_json()
        pages += 1
        seen.extend(i["user_id"] for i in body["items"])
        cursor = body["next_cursor"]
        if not cursor:
            break
    assert pages == 2 and len(seen) == 2 and len(set(seen)) == 2
    assert {out["user"]["user_id"], out2["user"]["user_id"]} == set(seen)

    body = client.get("/api/admin/v1/acquisition/users?limit=10").get_json()
    item = next(i for i in body["items"]
                if i["user_id"] == out["user"]["user_id"])
    raw = json.dumps(body)
    # 脱敏红线：原始账号不回显；visitor 只给前缀；无 IP；无 referrer query
    assert item["login_id_masked"] == "m***@x.com"
    assert "login_id" not in item
    vhash = acq_store.visitor_id_hash(vid)
    assert vhash not in raw
    assert len(item["first_touch"]["visitor_hash_prefix"]) == 8
    assert item["first_touch"]["referrer_domain"] == "a.example.com"
    assert "ip_prefix" not in raw and "?q=" not in raw
    # first/last 分列
    assert item["first_touch"]["acquisition_id"] == first["acquisition_id"]
    assert item["first_touch"]["source_code"] == "s1"
    assert item["last_touch"]["acquisition_id"] == last["acquisition_id"]
    assert item["last_touch"]["source_code"] == "s2"
    assert item["source_code"] == "s2"
    assert item["attribution_method"] == "visit"
    # 损坏 cursor → 当作第一页（不 500）
    r2 = client.get("/api/admin/v1/acquisition/users?cursor=%% %%bad")
    assert r2.status_code == 200
    # summary 端点同门控（owner 已登录）
    r3 = client.get("/api/admin/v1/acquisition/summary")
    assert r3.status_code == 200
    sbody = r3.get_json()
    assert sbody["registration_mode"] in ("closed", "invite_only")
    assert any(i["source_code"] == "s2" and i["visits"] == 1
               for i in sbody["items"])
    sraw = json.dumps(sbody)
    assert "ip_prefix" not in sraw and "?q=" not in sraw


@pg_only
def test_admin_v1_users_row_fills_campaign_source(monkeypatch):
    _satisfy_preconditions(monkeypatch)
    owner = _mk_owner()
    app_mod.AUTH_ENABLED = True
    out = _redeem(_mk_invite(owner["user_id"], login_id="campfill@x.com",
                             source_code="srcfill"),
                  "campfill@x.com")
    client = _client()
    _owner_session(client, owner)
    r = client.get("/api/admin/v1/users?q=campfill@x.com").get_json()
    assert len(r["items"]) == 1
    item = r["items"][0]
    assert item["source"] == "srcfill"
    assert item["campaign"] is None
    assert item["registration_method"] == "invite"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
