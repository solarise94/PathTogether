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
  - 90 天保留（PR5 §11.3）：过期未引用行删除、过期已归因行脱敏
    （ip/referrer/UTM/landing/visitor hash 置空，保留 source/campaign）、
    未到期不动、幂等与计数；retention 入口 json fail-closed；daemon 开关；
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
    if BACKEND == "postgres":
        # review R2-F2：PG 上注册兑换/建号统一走「维护闸 + 开通锁」组合
        # 原语，闸 fail-closed（platform_settings 缺 ai_dispatch_maintenance
        # 即拒绝）。conftest TRUNCATE 清掉 0029 种子，每用例幂等重放
        # （target=window + 闸=false）。
        import _billing_helpers as bh
        bh.seed_spend_settings()
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


def _cookie_state(client):
    """读 test client 的 pt_acq cookie 原始形态 → (存在, value, attrs)。

    Batch D1 16 起 /r/ 不再签发 cookie，只输出**过期清除**指令——断言用
    Set-Cookie 的属性（path/HttpOnly/SameSite/Secure/Expires）而非 payload。
    """
    c = client.get_cookie("pt_acq", domain="localhost", path="/")
    if c is None:
        return False, None, None
    return True, c.value, c


def _mk_invite(owner_uid, login_id=None, **kw):
    return registration_store.create_invite(owner_uid, login_id=login_id, **kw)


def _redeem(inv, login_id):
    return registration_store.redeem_invite(
        inv["token"], login_id, "longpassword123")


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
# 2. /r/ 路由（json/PG 双跑；Batch D1 16：兼容 302、零记录、清 cookie）
# =========================================================================== #
def test_r_valid_slug_sets_cookie_and_redirects():
    """Batch D1 16：合法 slug 安全 302 到 allowlist landing；响应按原属性
    （path=/、HttpOnly、SameSite=Lax、Secure 随配置）+ 过期时间**清除**
    pt_acq，不设置任何替代 cookie；不落任何触点记录。"""
    app_mod.AUTH_ENABLED = True
    client = _client()
    r = client.get("/r/mywebpage?campaign=summer24&utm_medium=cta")
    assert r.status_code == 302
    assert r.headers["Location"] == "/register"  # 缺省 landing
    # 清除指令：value 为空 + Expires 过去（epoch 0）+ 与原写入属性完全匹配
    set_cookie = r.headers.get("Set-Cookie", "")
    assert 'pt_acq="";' in set_cookie or "pt_acq=;" in set_cookie
    assert "Expires=Thu, 01 Jan 1970" in set_cookie
    assert "Path=/" in set_cookie
    assert "HttpOnly" in set_cookie and "SameSite=Lax" in set_cookie
    # 浏览器语义：清除指令后 cookie 从 jar 消失（或至多残留空值）——
    # 绝不再携带可验签 payload
    exists, value, _c = _cookie_state(client)
    assert (not exists) or value == ""
    # 不设置任何替代访客 cookie（响应只有该清除指令一条 Set-Cookie 相关行）
    assert "visitor" not in set_cookie.lower()


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
    # （未匹配路由，无信息量）；其余非法 slug 302 到 /
    assert r.status_code in (302, 404), (slug, r.status_code)
    if r.status_code == 302:
        assert r.headers["Location"] == "/"


def test_r_never_sets_new_cookie_and_never_records(monkeypatch):
    """Batch D1 17（§4.4）：/r/ 与触点写路径零耦合——全新 client 访问 /r/
    不会得到任何访客 cookie（只有清除指令）；record_visit 注入失败也不影响
    跳转（根本不再调用）。"""
    app_mod.AUTH_ENABLED = True
    client = _client()
    r = client.get("/r/src1")
    assert r.status_code == 302
    exists, value, _c = _cookie_state(client)
    assert (not exists) or value == ""  # 仅清除指令，不是新签发

    def _boom(*a, **kw):
        raise RuntimeError("record_visit must not be called from /r/")
    monkeypatch.setattr(acq_store, "record_visit", _boom)
    r2 = client.get("/r/src2?to=/demo")
    assert r2.status_code == 302
    assert r2.headers["Location"] == "/demo"


def test_r_json_backend_never_500s():
    app_mod.AUTH_ENABLED = True
    client = _client()
    r = client.get("/r/mywebpage?campaign=c1")
    assert r.status_code == 302
    assert r.headers["Location"] == "/register"
    exists, value, _c = _cookie_state(client)
    assert (not exists) or value == ""  # 旧 cookie 清除（不签发新 payload）


# =========================================================================== #
# 3. admin v1 acquisition 端点已随 R3 wave1 物理删除（原 410 退役面）
# =========================================================================== #


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

    def _acq_total():
        conn = _pg_conn()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT count(*)::int AS n FROM user_acquisition")
                return int(cur.fetchone()["n"])
        finally:
            conn.close()

    def _visits_total():
        """acquisition_visits 全表行数（Batch D1 16：/r/ 不再新增触点行）。"""
        conn = _pg_conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT count(*)::int AS n FROM acquisition_visits")
                return int(cur.fetchone()["n"])
        finally:
            conn.close()

    def _count_override_rows():
        """user_override 月额度策略行数（cutover 契约：window 过渡期显式
        额度会建过渡 override；total 模式不建）。"""
        conn = _pg_conn()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT count(*)::int AS n FROM ai_spend_policies "
                            "WHERE scope_type='user_override'")
                return int(cur.fetchone()["n"])
        finally:
            conn.close()

    def _insert_historical_attribution(user_id, visit_id, campaign=None):
        """直接 SQL 造一条 user_acquisition（模拟冻结前的**历史**归因行——
        Batch B 起写路径已冻结，仅历史数据读取/清理语义仍需覆盖）。"""
        conn = _pg_conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO user_acquisition (user_id, "
                    "first_acquisition_id, last_acquisition_id, invite_id, "
                    "source_code, campaign_id, attributed_at, "
                    "attribution_method) VALUES (%s,%s,%s,NULL,%s,%s,now(),"
                    "'visit')",
                    (user_id, visit_id["acquisition_id"],
                     visit_id["acquisition_id"], visit_id["source_code"],
                     campaign))
            conn.commit()
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
def test_redeem_writes_no_user_acquisition_but_total_allowance():
    """Batch B §4.4/§Batch B + R3 单轨：兑换与归因解耦——无论触点/UTM/邀请
    来源如何，兑换成功但 user_acquisition **零新增**；额度面恒为一次性总额度：
    带初始面值邀请同事务建行（source=invite），无面值邀请按 defaults 基线
    建行；user_override 过渡策略已随单轨删除（恒零新增）。"""
    owner = _mk_owner()
    _seed_campaign("camp-web", "websrc")
    _seed_campaign("camp-inv", "invsrc")
    vid = acq_store.new_visitor_id()
    acq_store.record_visit(visitor_id=vid, source_code="websrc",
                           campaign_id="camp-web")
    before = _acq_total()
    inv = _mk_invite(owner["user_id"], campaign_id="camp-inv",
                     total_limit_nano_cny=12 * 10 ** 9)
    out = _redeem(inv, "prio1@x.com")
    # 单轨：注册成功、归因零新增；同事务建 allowance（source=invite）
    assert user_store.get_user_by_login_id("prio1@x.com") is not None
    assert out["total_allowance"]["limit_nano_cny"] == 12 * 10 ** 9
    assert out["total_allowance"]["source"] == "invite"
    assert _acq_total() == before
    assert _ua(out["user"]["user_id"]) is None
    assert _count_override_rows() == 0
    # 无初始面值的邀请：按 defaults 基线（20 CNY）建行
    inv2 = _mk_invite(owner["user_id"])
    out2 = _redeem(inv2, "prio2@x.com")
    assert out2["total_allowance"]["limit_nano_cny"] == 20 * 10 ** 9
    assert _count_override_rows() == 0
    assert _acq_total() == before


@pg_only
def test_expired_visits_and_tampered_visitor_are_ignored_frozen():
    """Batch B：触点过期/visitor 不匹配语义随写路径冻结一并退役——兑换根本
    不读取触点；本用例锁定「兑换后归因行仍为零」。"""
    owner = _mk_owner()
    vid = acq_store.new_visitor_id()
    acq_store.record_visit(visitor_id=vid, source_code="expired-src")
    _expire_all_visits()
    before = _acq_total()
    inv = _mk_invite(owner["user_id"])
    out = _redeem(inv, "exp@x.com")
    assert _acq_total() == before
    # visitor 不匹配（另一访客）同样无关紧要——不读取
    other = acq_store.new_visitor_id()
    assert other != vid
    out2 = _redeem(_mk_invite(owner["user_id"]), "tamper@x.com")
    assert _acq_total() == before


@pg_only
def test_redeem_succeeds_even_if_acquisition_store_broken(monkeypatch):
    """Batch B 红线：站点统计故障绝不能阻断注册——归因已不在兑换事务内，
    insert_user_acquisition 注入失败不再影响兑换（用户创建、邀请消费）。"""
    owner = _mk_owner()
    inv = _mk_invite(owner["user_id"], login_id="boom@x.com")

    def _boom(*a, **kw):
        raise RuntimeError("injected acquisition failure")

    monkeypatch.setattr(acq_store, "insert_user_acquisition", _boom)
    out = registration_store.redeem_invite(inv["token"], "boom@x.com",
                                           "longpassword123")
    assert user_store.get_user_by_login_id("boom@x.com") is not None
    row = registration_store.get_invite(inv["invite_id"])
    assert row["use_count"] == 1 and row["consumed_at"] is not None
    assert _acq_total() == 0  # 全程零归因写入


@pg_only
def test_visit_retention_deletes_scrubs_and_is_idempotent(monkeypatch):
    """§11.3 PR5：过期引用行脱敏、未引用行删除、未到期不动、计数正确。

    Batch B 起兑换不再写归因——「被引用」行用直接 SQL 造的**历史**归因行
    （冻结前数据），引用/清理语义照旧覆盖。"""
    monkeypatch.setenv("ACQ_IP_SALT", "salt-ret")   # 让 ip_prefix_hash 非空
    owner = _mk_owner()
    _seed_campaign("keep-camp", "keep")
    vid = acq_store.new_visitor_id()
    referenced = acq_store.record_visit(
        visitor_id=vid, source_code="keep", campaign_id="keep-camp",
        referrer_domain="https://ref.example.com/x?q=1", landing_path="/demo",
        utm_source="us", utm_medium="um", utm_campaign="uc", ip="203.0.113.7")
    stale = acq_store.record_visit(
        visitor_id=acq_store.new_visitor_id(), source_code="drop-me",
        utm_source="bye")
    fresh = acq_store.record_visit(
        visitor_id=acq_store.new_visitor_id(), source_code="fresh",
        utm_source="stay", ip="198.51.100.9")
    # 历史归因行（冻结写路径前的形态）：引用 referenced 触点
    attr_user = user_store.create_user("retention-hist@x.com",
                                       "userpass1234567")
    _insert_historical_attribution(attr_user["user_id"], referenced,
                                   campaign="keep-camp")
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
    deleted, scrubbed = acq_store.run_visit_retention()
    assert deleted >= 1 and scrubbed == 1

    def _row(acq_id):
        conn = _pg_conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT * FROM acquisition_visits WHERE acquisition_id=%s",
                    (acq_id,))
                r = cur.fetchone()
                return dict(r) if r is not None else None
        finally:
            conn.close()

    # 已归因的过期触点：骨架保留，但脱敏字段全部清空（§11.3 长期只留
    # campaign/source：ip/referrer/UTM/landing/visitor hash 到期即清）
    ref_row = _row(referenced["acquisition_id"])
    for f in acq_store._SCRUB_FIELDS:
        assert ref_row[f] == "", f
    assert ref_row["source_code"] == "keep"
    assert ref_row["campaign_id"] == "keep-camp"
    assert ref_row["touched_at"] is not None
    # 未引用过期行删除；未到期行原样不动
    assert _row(stale["acquisition_id"]) is None
    fresh_row = _row(fresh["acquisition_id"])
    assert fresh_row["utm_source"] == "stay"
    assert fresh_row["ip_prefix_hash"]
    assert fresh_row["visitor_id_hash"]
    # 幂等：再跑一轮两类计数均为 0（脱敏 UPDATE 带 <>'' 条件）
    assert acq_store.run_visit_retention() == (0, 0)


def test_visit_retention_requires_pg_backend():
    """json/dual 后端：retention 入口 fail-closed（不静默跳过）。"""
    if BACKEND == "postgres":
        pytest.skip("json 后端专用反向用例（PG 模式跑正向路径）")
    import platform_features
    with pytest.raises(platform_features.PgFeatureUnavailable):
        acq_store.run_visit_retention()
    # 兼容包装同源 fail-closed
    with pytest.raises(platform_features.PgFeatureUnavailable):
        acq_store.cleanup_expired_visits()


def test_acquisition_retention_daemon_switch(monkeypatch):
    """retention daemon：非 PG 不启动；env ≤0 关闭；PG + 正间隔才起线程。"""
    if BACKEND != "postgres":
        # json/dual：导入期即不启动（fail-closed）
        assert app_mod._ACQUISITION_RETENTION_THREAD is None
    # 开关：0 / 负数 → None（不建线程）
    monkeypatch.setenv("ACQ_RETENTION_INTERVAL_SECONDS", "0")
    assert app_mod._start_acquisition_retention_thread() is None
    monkeypatch.setenv("ACQ_RETENTION_INTERVAL_SECONDS", "-5")
    assert app_mod._start_acquisition_retention_thread() is None
    if BACKEND == "postgres":
        monkeypatch.setenv("ACQ_RETENTION_INTERVAL_SECONDS", "3600")
        th = app_mod._start_acquisition_retention_thread()
        assert th is not None and th.name == "acquisition-retention" \
            and th.daemon is True


@pg_only
def test_register_route_full_acquisition_flow(monkeypatch):
    """Batch D1 16/17 全链路：/r/ 只做安全 302（零触点行、清 cookie）、注册
    成功且 user_acquisition **零新增**（注册与归因彻底解耦）。"""
    _satisfy_preconditions(monkeypatch)
    _seed_campaign("camp-flow", "mywebpage")
    owner = _mk_owner()
    app_mod.AUTH_ENABLED = True
    client = _client()
    _owner_session(client, owner)
    assert client.put("/api/admin/v1/settings/registration",
                      json={"mode": "invite_only"}).status_code == 200
    inv = client.post("/api/admin/v1/invites",
                      json={"login_id": "flow-acq@x.com"}).get_json()["invite"]
    anon = _client()
    before = _acq_total()
    # 访客从旧 mywebpage CTA 进入：安全 302，触点表零新增、旧 cookie 被清除
    r = anon.get("/r/mywebpage?campaign=camp-flow&utm_medium=cta&next=evil")
    assert r.status_code == 302 and r.headers["Location"] == "/register"
    assert "Expires=Thu, 01 Jan 1970" in r.headers.get("Set-Cookie", "")
    assert _visits_total() == 0  # record_visit 不再被 /r/ 调用
    anon.get("/register")
    r2 = anon.post("/register", data={
        "invite_token": inv["token"], "login_id": "flow-acq@x.com",
        "password": "longpassword123", "password_confirm": "longpassword123"})
    assert r2.status_code == 302, r2.get_data(as_text=True)
    user = user_store.get_user_by_login_id("flow-acq@x.com")
    assert user is not None  # 注册成功（不再被归因写路径阻断）
    assert _ua(user["user_id"]) is None and _acq_total() == before
    # 兑换 audit 不携带来源/归因字段
    conn = _pg_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT detail::text AS d FROM audit_events WHERE "
                        "action='registration.user_created'")
            rows = cur.fetchall()
    finally:
        conn.close()
    assert rows  # app 层审计在（字段本就不含来源）
    # 邀请码绝不进 URL/query
    assert inv["token"] not in r2.get_data(as_text=True)


@pg_only
def test_admin_summary_funnel_reads_frozen_history_only():
    """Batch B：漏斗汇总继续可读**历史**行；新注册不再进入漏斗
    （registrations 不因兑换增长）。"""
    owner = _mk_owner()
    _seed_campaign("camp-f1", "srcf1")
    vid = acq_store.new_visitor_id()
    acq_store.record_visit(visitor_id=vid, source_code="srcf1",
                           campaign_id="camp-f1")
    v = _visits(vid)[0]
    attr_user = user_store.create_user("funnel-hist@x.com",
                                       "userpass1234567")
    _insert_historical_attribution(attr_user["user_id"], v,
                                   campaign="camp-f1")
    _insert_usage_event(attr_user["user_id"], hours_back=2)
    # 兑换不再产生归因：新用户不进漏斗
    _redeem(_mk_invite(owner["user_id"]), "funnew@x.com")
    summary = acq_store.admin_funnel_summary()
    rows = {(r["source_code"], r["campaign_id"]): r for r in summary["items"]}
    c1 = rows[("srcf1", "camp-f1")]
    assert c1["visits"] == 1 and c1["visitors"] == 1
    assert c1["registrations"] == 1       # 仅历史归因行
    assert c1["first_ai_count"] == 1      # 历史 user 的 AI 事件可读
    assert summary["totals"]["registrations"] == 1


@pg_only
def test_admin_users_endpoint_no_new_attribution_but_masking_kept(monkeypatch):
    """Batch D1 15/17 + R3 wave1：acquisition/users 明细端点物理删除（404；
    410 stub 不留）。写路径冻结用数据库行数断言——新注册零归因行；历史归因
    行（SQL 造的冻结前形态）只能经审计工具/SQL 读取（脱敏红线由表结构保证，
    不再有 API 出口）。"""
    _satisfy_preconditions(monkeypatch)
    owner = _mk_owner()
    app_mod.AUTH_ENABLED = True
    client = _client()
    _owner_session(client, owner)
    vid = acq_store.new_visitor_id()
    first = acq_store.record_visit(visitor_id=vid, source_code="s1",
                                   referrer_domain="https://a.example.com/x?q=1")
    time.sleep(0.01)
    last = acq_store.record_visit(visitor_id=vid, source_code="s2")
    out = registration_store.redeem_invite(
        _mk_invite(owner["user_id"], login_id="Maskme@x.com")["token"],
        "maskme@x.com", "longpassword123", "Masked User")
    # 明细端点物理删除：路由不存在（404）即无任何来源明细出口
    r = client.get("/api/admin/v1/acquisition/users?limit=10")
    assert r.status_code == 404
    # 新兑换零归因：直接 SQL 证明明细数据不存在
    assert _ua(out["user"]["user_id"]) is None
    # 历史归因行（冻结前形态）仍可写历史读（表冻结 ≠ 数据消失）
    _insert_historical_attribution(out["user"]["user_id"], first)
    conn = _pg_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT first_acquisition_id, last_acquisition_id "
                        "FROM user_acquisition WHERE user_id=%s",
                        (out["user"]["user_id"],))
            row = cur.fetchone()
    finally:
        conn.close()
    assert row is not None
    assert row["first_acquisition_id"] == first["acquisition_id"]
    assert row["last_acquisition_id"] == first["acquisition_id"]


@pg_only
def test_admin_v1_users_row_attribution_frozen_to_null(monkeypatch):
    """Batch D1 14（§4.4）：users 列表行**整键删除** campaign/source（不再
    查询归因表）；registration_method 仍为 invite。"""
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
    # 归因键整键删除（不是留位 null）——历史行存在与否都不再回显
    assert "source" not in item
    assert "campaign" not in item
    assert item["registration_method"] == "invite"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
