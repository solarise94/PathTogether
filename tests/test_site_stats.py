# -*- coding: utf-8 -*-
"""Batch D2：站点匿名访问统计测试（site_stats_store + 0030，docs
review-2026-09-02-upload-user-limits-admin-ui-cleanup.md §3.4 / §4.4 /
§Batch D2 / §7.2 / §7.3 / §8.1-9）。

store 级（默认全跑；PG-only 用例 RUN_PG_TESTS=1）：
  - allowlist：/、/demo、/register、/login 精确命中；/api/*、/static/*、
    /admin、/healthz、工作区/深路径（含分享 token、切片名、项目 ID）不落；
  - 口径门：非 2xx-3xx 不落；非 HTML 不落；query 含 token/资源 ID 键整条
    拒绝；utm_source 白名单外丢弃；其余 query 全丢弃；
  - referrer 只取 hostname；同站（PUBLIC_BASE_URL/本机）归 direct；
  - bot：Googlebot/curl/headless → suspected_bot + bot_name；正常浏览器
    登录 → signed_in_human；匿名 → anonymous_human；bot 优先于登录态；
  - 去重与哈希：IPv4 同 /24 同哈希、跨 /24 不同；IPv6 /64 同理；
    IPv4-mapped IPv6 等价；跨日哈希改变（含 Asia/Shanghai 日界回归）；
    同 hash+page+10 分钟桶唯一约束生效、跨桶落新行（真实 PG）；
  - 落库形态：0030 列固定 11 列；行值无完整 IP/UA/query/token/资源 ID；
  - secret 缺失/权限错误：build_event/enqueue 降级不落事件且不抛
    （公开页面仍成功由 app.py 接线代理测；此处只证 store 不抛）；
    secret 绝不复用 session secret（SECRET_KEY 无回退）；
  - worker：未启动 enqueue False；队列满 False 不抛；DB 故障注入丢批不抛；
    start/stop 幂等；非 postgres 后端 start_worker no-op；
  - dashboard_stats：形状与钉死契约逐键一致；visits/unique_visitors/bots
    三分类互不混入（爬虫不进入匿名访客近似数）；只读——调用前后业务表与
    site 表行数不变、不触发清理；90 天窗口外事件不计入；
  - purge_expired 只删 expires_at 到期行；
  - F4/R2-F5 每日保留任务接线（双跑纯单元，monkeypatch 注入）：R2-F5 拆分
    后 acquisition 段（_run_daily_retention_once）与 site stats 段
    （_run_site_stats_retention_once）为独立函数/线程/开关——各自独立
    try/except 互不拖垮；store 缺失（None）时 site stats 段静默跳过；
    ACQ 间隔=0（归因退役）不影响 site stats 段；

app.py 接线契约测试（``site_stats_app_wiring`` 标记，**默认启用、不 skip**；
app.py 接线由并行代理实施，接线落地前这些用例红属预期）：
  - GET /api/admin/v1/site-stats owner-only：匿名 401 / user 403 / owner 200
    且形状与钉死契约逐键一致；（PG）调用前后 site_visit_events 行数不变；
  - after_request 采集：公开 HTML GET / 落 home 事件；/healthz（JSON）不落。

运行：
  cd PathTogether && python3 -m pytest tests/test_site_stats.py -q
  RUN_PG_TESTS=1 python3 -m pytest tests/test_site_stats.py -q
"""
import json
import logging
import os
import sys
import threading
import time
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import _bootstrap  # noqa: E402,F401  # session 目录 + openslide stub（conftest 先行）
UPLOAD_DIR = _bootstrap.UPLOAD_DIR
import pytest  # noqa: E402

import app as app_mod  # noqa: E402
import site_stats_store as sss  # noqa: E402
import user_store  # noqa: E402
from _pt_helpers import csrf_client, isolate_app  # noqa: E402
from pg_compat import BACKEND  # noqa: E402

pg_only = pytest.mark.skipif(
    BACKEND != "postgres",
    reason="site_visit_events 数据层需真实 PG（RUN_PG_TESTS=1）")

# app.py 接线由并行代理实施；依赖它的用例打此标记但**默认启用**（不 skip），
# 接线未就绪时红属预期，不得删除。
site_stats_app_wiring = pytest.mark.site_stats_app_wiring

if BACKEND == "postgres":
    import psycopg  # noqa: E402

UTC = timezone.utc
BASE_TIME = datetime(2026, 9, 3, 2, 0, 0, tzinfo=UTC)
BASE_DAY = "2026-09-03"          # BASE_TIME 的 Asia/Shanghai 日期
_SECRET_BYTES = b"unit-test-secret-0123456789abcdef-site-stats"
_BROWSER_UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X) Safari/605.1.15"

DASHBOARD_KEYS = {
    "generated_at", "today", "d7", "d30", "daily", "top_referrers",
    "top_pages", "top_countries", "recent", "visitor_kinds",
    "geo_configured",
}
WINDOW_KEYS = {"visits", "unique_visitors", "bots"}
DAILY_KEYS = {"date", "visits", "unique_visitors", "bots"}
RECENT_KEYS = {"occurred_at", "page_key", "referrer_domain", "country_code",
               "visitor_kind", "bot_name"}
SITE_COLUMNS = [
    "event_id", "occurred_at", "dedup_bucket", "page_key",
    "referrer_domain", "utm_source", "country_code", "daily_visitor_hash",
    "visitor_kind", "bot_name", "expires_at",
]


# --------------------------------------------------------------------------- #
# 公共基建
# --------------------------------------------------------------------------- #
@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    """每用例：存储隔离 + 采集 env 清理 + worker/告警节流状态复位。

    worker 跨用例必须停掉——上一个用例的 monkeypatch（如 STORAGE_BACKEND、
    _flush_batch 桩）在 teardown 已还原，残留线程会带着旧状态写库。
    """
    isolate_app(monkeypatch, tmp_path, clear_stores=True)
    for name in ("SITE_STATS_HMAC_SECRET_FILE", "PUBLIC_BASE_URL",
                 "SERVER_NAME"):
        monkeypatch.delenv(name, raising=False)
    sss._reset_warn_state()
    sss.stop_worker()          # 先停 worker（app import 可能已自起；见 app.py）
    if BACKEND == "postgres":
        # conftest truncate 与 in-flight flush 之间可能落下前用例残余——
        # 本文件大量精确计数断言，显式清一次 site_visit_events 兜底
        conn = _conn()
        try:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM site_visit_events")
            conn.commit()
        finally:
            conn.close()
        # review R2-F2：app 接线用例会经 HTTP 建 owner/user（PG 上 role=user
        # 建号走「维护闸 + 开通锁」组合原语，闸 fail-closed）——conftest
        # TRUNCATE 清掉 0029 种子，每用例幂等重放（target=window + 闸=false）
        import _billing_helpers as bh
        bh.seed_spend_settings()
    yield
    sss.stop_worker()


@pytest.fixture
def secret(tmp_path, monkeypatch):
    """0600 secret 文件 + env 指向（采集启用的前提）。"""
    return _make_secret(tmp_path, monkeypatch)


def _make_secret(tmp_path, monkeypatch, mode=0o600,
                 content=_SECRET_BYTES, name="site_stats.secret"):
    p = tmp_path / name
    p.write_bytes(content)
    os.chmod(p, mode)
    monkeypatch.setenv("SITE_STATS_HMAC_SECRET_FILE", str(p))
    return p


def _ev(**over):
    """build_event 固定入参（公开页成功 HTML GET、匿名、IPv4）。"""
    kwargs = dict(
        path="/", query_string="", referrer="", remote_addr="203.0.113.45",
        user_agent=_BROWSER_UA, status_code=200,
        content_type="text/html; charset=utf-8", signed_in=False,
        now=BASE_TIME)
    kwargs.update(over)
    return sss.build_event(**kwargs)


def _fake_event(**over):
    """手工最小事件（绕开 secret，供队列/worker 语义测试）。"""
    ev = {
        "page_key": "home",
        "occurred_at": BASE_TIME.isoformat(),
        "dedup_bucket": int(BASE_TIME.timestamp() // sss.DEDUP_BUCKET_SECONDS),
        "referrer_domain": "direct",
        "utm_source": None,
        "country_code": "unknown",
        "daily_visitor_hash": "a" * 64,
        "visitor_kind": "anonymous_human",
        "bot_name": None,
    }
    ev.update(over)
    return ev


def _conn():
    import pg_store
    c = pg_store.connect()
    c.row_factory = psycopg.rows.dict_row
    return c


def _sql_all(sql, params=()):
    conn = _conn()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            return cur.fetchall()
    finally:
        conn.close()


def _sql_one(sql, params=()):
    rows = _sql_all(sql, params)
    return rows[0] if rows else None


def _site_count():
    return _sql_one("SELECT count(*) AS n FROM site_visit_events")["n"]


def _business_counts():
    """业务表 + site 表行数（只读断言用；全部应为 0 或调用前后不变）。"""
    counts = {}
    for table in ("site_visit_events", "users", "ai_usage_events",
                  "billing_ledger_entries", "billing_holds",
                  "registration_invites", "platform_settings",
                  "ai_spend_windows"):
        counts[table] = _sql_one(
            "SELECT count(*) AS n FROM %s" % table)["n"]
    return counts


def _wait_until(pred, timeout=8.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(0.05)
    return bool(pred())


def _flush(events):
    """直接驱动批量写（PG 测试不依赖 worker 线程时序）。"""
    sss._flush_batch(events)


# --------------------------------------------------------------------------- #
# 1. allowlist 与口径门（build_event 纯函数；双跑）
# --------------------------------------------------------------------------- #
def test_allowlist_pages_accepted_exact_match(secret):
    for path, page_key in sss.PAGE_ALLOWLIST.items():
        ev = _ev(path=path)
        assert ev is not None, path
        assert ev["page_key"] == page_key


def test_non_allowlist_paths_rejected(secret):
    # /api/*、静态资源、health、admin、登录后工作区、分享 token/切片名/
    # 项目 ID 深路径、尾斜杠与大小写变形——一律精确匹配失败
    for path in ("/api/demo/config", "/api/admin/v1/overview",
                 "/static/app.js", "/templates/base.html", "/healthz",
                 "/admin", "/admin/registration", "/workspace",
                 "/workspace/abc123", "/project/p-1", "/demo/slide-01",
                 "/share/tok123", "/r/campaign01", "/demo/", "/demo/x",
                 "//demo", "/DEMO", "/login/", "/register/step2", "",
                 "/?utm_source=x", None):
        assert _ev(path=path) is None, repr(path)


def test_status_and_content_type_gates(secret):
    for status in (100, 199, 404, 410, 500, 503, None, "200"):
        assert _ev(status_code=status) is None, status
    assert _ev(status_code=302) is not None          # 3xx 允许
    assert _ev(status_code=204, content_type=None) is None   # 非 HTML
    for ctype in ("application/json", "text/plain", "image/png",
                  "", None, "text/htmlscript"):
        assert _ev(content_type=ctype) is None, ctype
    assert _ev(content_type="Text/HTML; charset=utf-8") is not None


def test_sensitive_query_rejects_whole_event(secret):
    # query 含 token / 资源 ID / 凭证键 → 整条不落（最保守口径）
    for qs in ("?share_token=abc123", "?token=1", "?access_token=x",
               "?project_id=9", "?slide=slide-01", "?roi=42", "?id=7",
               "?api_key=SECRET", "?invite=cd9", "?session=sid",
               "?next_token=t", "?file=patient.tif"):
        assert _ev(query_string=qs) is None, qs


def test_query_otherwise_dropped_utm_whitelisted(secret):
    # 其余 query 全丢弃（事件仍落，但 utm 为空）；utm_source 仅白名单短标签
    ev = _ev(query_string="?foo=bar&utm_medium=cpc")
    assert ev is not None and ev["utm_source"] is None
    ev = _ev(query_string="?utm_source=newsletter")
    assert ev["utm_source"] == "newsletter"
    ev = _ev(query_string="?UTM_SOURCE=News")
    assert ev["utm_source"] == "News"
    # 超长 / 特殊字符 / 控制字符 / 空白 → 清洗为 None（事件保留）
    for bad in ("?utm_source=" + "x" * 40, "?utm_source=%3Cscript%3E",
                "?utm_source=a%00b", "?utm_source=a b"):
        ev = _ev(query_string=bad)
        assert ev is not None
        assert ev["utm_source"] is None, bad


def test_event_keys_are_exactly_contract_and_minimized(secret):
    ev = _ev(query_string="?utm_source=n", referrer="https://news.example.com")
    assert set(ev.keys()) == {
        "page_key", "occurred_at", "dedup_bucket", "referrer_domain",
        "utm_source", "country_code", "daily_visitor_hash", "visitor_kind",
        "bot_name"}
    blob = json.dumps(ev, ensure_ascii=False)
    # 原始 IP / UA / query / 资源标识一个都不能出现在事件里
    for raw in ("203.0.113.45", _BROWSER_UA, "utm_medium", "Safari/605",
                "share_token"):
        assert raw not in blob, raw


def test_referrer_hostname_only_and_same_site_direct(secret, monkeypatch):
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://histo.example.com")
    assert _ev(referrer="")["referrer_domain"] == "direct"
    assert _ev(referrer=None)["referrer_domain"] == "direct"
    assert _ev(referrer="garbage not a url")["referrer_domain"] == "direct"
    assert _ev(referrer="::::")["referrer_domain"] == "direct"
    ev = _ev(referrer="https://news.example.com/a/b?x=1#frag")
    assert ev["referrer_domain"] == "news.example.com"
    ev = _ev(referrer="HTTP://News.Example.COM:8443/x")
    assert ev["referrer_domain"] == "news.example.com"
    # 同站 hostname → direct（不当外部来源）
    assert _ev(referrer="https://histo.example.com/whatever")[
        "referrer_domain"] == "direct"
    assert _ev(referrer="https://histo.example.com")[
        "referrer_domain"] == "direct"


# --------------------------------------------------------------------------- #
# 2. 三分类与 bot 词表
# --------------------------------------------------------------------------- #
def test_ruleset_version_constant_shape():
    assert isinstance(sss.SITE_BOT_UA_RULESET_VERSION, str)
    assert sss.SITE_BOT_UA_RULESET_VERSION
    assert sss.SITE_BOT_UA_RULESET_VERSION == "2026-09-03.v1"


def test_bot_ua_classified_with_name(secret):
    cases = {
        "Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)":
            "Googlebot",
        "Mozilla/5.0 (compatible; bingbot/2.0)": "Bingbot",
        "curl/8.4.0": "curl",
        "Wget/1.21": "Wget",
        "python-requests/2.31": "python-requests",
        "Mozilla/5.0 HeadlessChrome/120.0": "HeadlessBrowser",
        "Mozilla/5.0 (X11; Linux) AppleWebKit/537.36 PhantomJS/2.1":
            "PhantomJS",
        "Mozilla/5.0 (compatible; SomeCompanyCrawler/1.0)": "generic_bot",
        "facebookexternalhit/1.1": "FacebookExternalHit",
    }
    for ua, expected in cases.items():
        ev = _ev(user_agent=ua)
        assert ev is not None, ua
        assert ev["visitor_kind"] == "suspected_bot", ua
        assert ev["bot_name"] == expected, ua


def test_human_kinds_and_bot_priority(secret):
    ev = _ev(signed_in=False)
    assert ev["visitor_kind"] == "anonymous_human"
    assert ev["bot_name"] is None
    ev = _ev(signed_in=True)
    assert ev["visitor_kind"] == "signed_in_human"
    assert ev["bot_name"] is None
    # 带会话的爬虫仍归 suspected_bot（bot 判定优先）
    ev = _ev(user_agent="curl/8.4.0", signed_in=True)
    assert ev["visitor_kind"] == "suspected_bot"
    assert ev["bot_name"] == "curl"


# --------------------------------------------------------------------------- #
# 3. 日轮换哈希与 IP 前缀
# --------------------------------------------------------------------------- #
def test_ipv4_same_slash24_same_hash(secret):
    a = _ev(remote_addr="203.0.113.1")
    b = _ev(remote_addr="203.0.113.99")
    c = _ev(remote_addr="203.0.114.1")
    assert a["daily_visitor_hash"] == b["daily_visitor_hash"]
    assert a["daily_visitor_hash"] != c["daily_visitor_hash"]


def test_ipv6_same_slash64_same_hash(secret):
    a = _ev(remote_addr="2001:db8:1:2:3:4:5:6")
    b = _ev(remote_addr="2001:db8:1:2::9")
    c = _ev(remote_addr="2001:db8:1:3::1")
    assert a["daily_visitor_hash"] == b["daily_visitor_hash"]
    assert a["daily_visitor_hash"] != c["daily_visitor_hash"]


def test_ipv4_mapped_ipv6_equivalent(secret):
    a = _ev(remote_addr="::ffff:203.0.113.7")
    b = _ev(remote_addr="203.0.113.9")
    assert a["daily_visitor_hash"] == b["daily_visitor_hash"]


def test_day_rotation_and_shanghai_day_boundary(secret):
    day1 = _ev(remote_addr="203.0.113.1", now=BASE_TIME)
    day2 = _ev(remote_addr="203.0.113.1",
               now=BASE_TIME + timedelta(days=1))
    assert day1["daily_visitor_hash"] != day2["daily_visitor_hash"]
    # Asia/Shanghai 日界 = 16:00 UTC：15:59:59 与次日 00:00（本地）分属两日
    before = _ev(remote_addr="203.0.113.1",
                 now=datetime(2026, 9, 3, 15, 59, 59, tzinfo=UTC))
    after = _ev(remote_addr="203.0.113.1",
                now=datetime(2026, 9, 3, 16, 0, 0, tzinfo=UTC))
    assert before["daily_visitor_hash"] != after["daily_visitor_hash"]
    assert _ev(now=datetime(2026, 9, 3, 15, 59, 59, tzinfo=UTC))[
        "daily_visitor_hash"] == day1["daily_visitor_hash"]


def test_missing_or_unparseable_ip_drops_event(secret):
    for addr in (None, "", "   ", "not-an-ip", "999.1.1.1", "203.0.113"):
        assert _ev(remote_addr=addr) is None, repr(addr)


def test_secret_not_reused_from_session_secret(monkeypatch, tmp_path):
    # 明确设置 SECRET_KEY（session secret），但未配置采集 secret 文件 →
    # 必须停止采集（绝不能回退复用 session secret）
    monkeypatch.setenv("SECRET_KEY", "session-secret-do-not-reuse")
    assert _ev() is None
    # 采集 secret 文件存在但权限过宽同样拒绝
    _make_secret(tmp_path, monkeypatch, mode=0o644)
    assert _ev() is None
    os.chmod(str(tmp_path / "site_stats.secret"), 0o600)
    assert _ev() is not None


# --------------------------------------------------------------------------- #
# 4. secret 降级：不落事件、不抛、有告警
# --------------------------------------------------------------------------- #
def test_secret_missing_degrades_without_raise(monkeypatch, caplog):
    monkeypatch.delenv("SITE_STATS_HMAC_SECRET_FILE", raising=False)
    with caplog.at_level(logging.WARNING, logger="svs.site_stats"):
        assert _ev() is None                       # build_event 降级
        assert sss.enqueue_visit(_fake_event()) is False
        assert sss.enqueue_visit(None) is False    # 永不抛
    assert "site_stats" in caplog.text


def test_secret_bad_permissions_or_missing_file_degrades(
        tmp_path, monkeypatch):
    sss._reset_warn_state()
    # 0644 / 0640 / 0666：权限过宽 → 拒绝
    for mode in (0o644, 0o640, 0o666):
        _make_secret(tmp_path, monkeypatch, mode=mode)
        assert _ev() is None, oct(mode)
    # 文件缺失 / 空文件 / 目录路径
    monkeypatch.setenv("SITE_STATS_HMAC_SECRET_FILE",
                       str(tmp_path / "no-such.secret"))
    assert _ev() is None
    _make_secret(tmp_path, monkeypatch, content=b"")
    assert _ev() is None
    monkeypatch.setenv("SITE_STATS_HMAC_SECRET_FILE", str(tmp_path))
    assert _ev() is None


def test_secret_readable_event_built(secret):
    ev = _ev()
    assert ev is not None
    assert len(ev["daily_visitor_hash"]) == 64


# --------------------------------------------------------------------------- #
# 5. 迁移 0030（真实 PG）
# --------------------------------------------------------------------------- #
@pg_only
def test_migration_0030_applied_idempotent_and_minimal():
    import pg_store
    # ensure_schema 按 tuple 行访问（row[0]），必须用默认 row_factory 连接
    plain = pg_store.connect()
    try:
        # ensure_schema 幂等：再跑两遍不报错且 0030 已记录
        files = pg_store.ensure_schema(plain)
        assert "0030_site_visit_events.sql" in files
        files2 = pg_store.ensure_schema(plain)
        assert files == files2
    finally:
        plain.close()
    cols = _sql_all(
        "SELECT column_name FROM information_schema.columns"
        " WHERE table_name='site_visit_events'"
        " ORDER BY ordinal_position")
    col_names = [r["column_name"] for r in cols]
    assert col_names == SITE_COLUMNS
    # 禁止出现原始 IP/UA/query/token/资源 ID/用户外键形态的列
    forbidden = ("ip", "agent", "query", "token", "invite", "session",
                 "user_id", "url", "raw")
    for name in col_names:
        for word in forbidden:
            assert word not in name, name
    unique = _sql_all(
        "SELECT conname FROM pg_constraint WHERE conrelid ="
        " 'site_visit_events'::regclass AND contype = 'u'")
    assert {r["conname"] for r in unique} == {
        "site_visit_events_dedup_unique"}
    check = _sql_one(
        "SELECT count(*) AS n FROM pg_constraint WHERE conrelid ="
        " 'site_visit_events'::regclass AND contype = 'c'"
        " AND conname = 'site_visit_events_kind_check'")
    assert check["n"] == 1
    # 迁移文件直接重放幂等（IF NOT EXISTS / ON CONFLICT DO NOTHING）
    sql = (pg_store.migrations_dir()
           / "0030_site_visit_events.sql").read_text(encoding="utf-8")
    conn = _conn()
    try:
        with conn.cursor() as cur:
            cur.execute(sql)
        conn.commit()
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# 6. 去重桶与落库形态（真实 PG）
# --------------------------------------------------------------------------- #
@pg_only
def test_dedup_bucket_unique_and_cross_bucket_new_row(secret):
    ev = _ev()
    _flush([ev, _ev()])                      # 同 hash+page+桶 → 冲突丢弃
    assert _site_count() == 1
    _flush([_ev(now=BASE_TIME + timedelta(seconds=601))])   # 跨桶 → 新行
    assert _site_count() == 2
    _flush([_ev(path="/demo")])              # 同 hash 同桶不同 page → 新行
    assert _site_count() == 3


@pg_only
def test_persisted_row_minimized_shape(secret):
    ev = _ev(query_string="?utm_source=n", referrer="https://news.example.com",
             user_agent="curl/8.4.0", signed_in=True)
    _flush([ev])
    row = _sql_one("SELECT * FROM site_visit_events")
    assert row is not None
    assert row["event_id"].startswith("sve_")
    assert len(row["event_id"]) == 4 + 24
    assert row["page_key"] == "home"
    assert row["referrer_domain"] == "news.example.com"
    assert row["utm_source"] == "n"
    assert row["country_code"] == "unknown"
    assert row["daily_visitor_hash"] == ev["daily_visitor_hash"]
    assert row["visitor_kind"] == "suspected_bot"
    assert row["bot_name"] == "curl"
    assert row["occurred_at"] == BASE_TIME
    assert row["expires_at"] == BASE_TIME + timedelta(
        days=sss.RETENTION_DAYS)
    assert row["dedup_bucket"] == ev["dedup_bucket"]
    # 行内任何值都不得携带原始 IP / UA / query / token 字样
    for value in row.values():
        text = str(value)
        for raw in ("203.0.113.45", "curl/8.4.0", "utm_source",
                    "share_token", "news.example.com/a"):
            assert raw not in text, (raw, text)


# --------------------------------------------------------------------------- #
# 7. retention 清理（真实 PG）
# --------------------------------------------------------------------------- #
@pg_only
def test_purge_expired_deletes_only_expired_rows(secret):
    old = _ev(now=BASE_TIME - timedelta(days=100))     # 到期 10 天前
    fresh = _ev()
    _flush([old, fresh])
    assert _site_count() == 2
    deleted = sss.purge_expired(now=BASE_TIME)
    assert deleted == 1
    assert _site_count() == 1
    row = _sql_one("SELECT occurred_at FROM site_visit_events")
    assert row["occurred_at"] == BASE_TIME
    # 幂等：再跑删 0 行；边界（expires_at == now）也删
    assert sss.purge_expired(now=BASE_TIME) == 0
    boundary = _ev(now=BASE_TIME - timedelta(days=sss.RETENTION_DAYS))
    _flush([boundary])
    assert sss.purge_expired(now=BASE_TIME) == 1


# --------------------------------------------------------------------------- #
# 8. dashboard_stats：形状 / 三分类互斥 / 只读（真实 PG + json 空形状）
# --------------------------------------------------------------------------- #
def test_dashboard_stats_contract_shape_without_postgres(monkeypatch):
    monkeypatch.setenv("STORAGE_BACKEND", "json")
    stats = sss.dashboard_stats(now=BASE_TIME)
    assert set(stats.keys()) == DASHBOARD_KEYS
    for key in ("today", "d7", "d30"):
        assert set(stats[key].keys()) == WINDOW_KEYS
        assert stats[key] == {"visits": 0, "unique_visitors": 0, "bots": 0}
    assert len(stats["daily"]) == 30
    for row in stats["daily"]:
        assert set(row.keys()) == DAILY_KEYS
        assert row == {"date": row["date"], "visits": 0,
                       "unique_visitors": 0, "bots": 0}
    assert stats["daily"][-1]["date"] == BASE_DAY
    assert stats["top_referrers"] == []
    assert stats["top_pages"] == []
    assert stats["top_countries"] == []
    assert stats["recent"] == []
    assert stats["visitor_kinds"] == {
        "anonymous_human": 0, "signed_in_human": 0, "suspected_bot": 0}
    assert stats["geo_configured"] is False
    assert sss.purge_expired(now=BASE_TIME) == 0


@pg_only
def test_dashboard_stats_counts_and_readonly(secret):
    anon = _ev(remote_addr="203.0.113.1")
    # 同 /24 同日 → 同哈希；不同 page 落新行，但去重计数仍只算 1 个访客
    anon_same_prefix = _ev(remote_addr="203.0.113.2", path="/register")
    signed = _ev(remote_addr="198.51.100.5", path="/demo", signed_in=True,
                 referrer="https://news.example.com/a")
    bot = _ev(remote_addr="216.239.32.10",
              user_agent="Mozilla/5.0 (compatible; Googlebot/2.1)",
              referrer="https://news.example.com/b")
    _flush([anon, anon_same_prefix, signed, bot])
    before = _business_counts()
    assert before["site_visit_events"] == 4

    stats = sss.dashboard_stats(now=BASE_TIME)

    # 形状逐键一致（钉死契约）
    assert set(stats.keys()) == DASHBOARD_KEYS
    for key in ("today", "d7", "d30"):
        assert set(stats[key].keys()) == WINDOW_KEYS, key
    assert set(stats["visitor_kinds"].keys()) == set(sss.VISITOR_KINDS)
    assert len(stats["daily"]) == 30
    for row in stats["daily"]:
        assert set(row.keys()) == DAILY_KEYS
    assert len(stats["recent"]) == 4
    for row in stats["recent"]:
        assert set(row.keys()) == RECENT_KEYS
    for row in stats["top_referrers"]:
        assert set(row.keys()) == {"domain", "visits"}
    for row in stats["top_pages"]:
        assert set(row.keys()) == {"page_key", "visits"}
    for row in stats["top_countries"]:
        assert set(row.keys()) == {"country_code", "visits"}

    # 三分类互不混入：visits=事件数；bots 单列不进入 unique_visitors；
    # unique_visitors=人类日去重哈希数（同 /24 同日的两个 IP 只算 1 个）
    assert stats["today"] == {"visits": 4, "unique_visitors": 2, "bots": 1}
    assert stats["d7"] == stats["today"]
    assert stats["d30"] == stats["today"]
    assert stats["visitor_kinds"] == {
        "anonymous_human": 2, "signed_in_human": 1, "suspected_bot": 1}
    assert stats["daily"][-1] == {
        "date": BASE_DAY, "visits": 4, "unique_visitors": 2, "bots": 1}
    # top：外部来源排除 direct；页 Top；国家全 unknown → 空列表
    assert stats["top_referrers"] == [
        {"domain": "news.example.com", "visits": 2}]
    assert stats["top_pages"] == [
        {"page_key": "home", "visits": 2},
        {"page_key": "demo", "visits": 1},
        {"page_key": "register", "visits": 1}]
    assert stats["top_countries"] == []
    assert stats["geo_configured"] is False
    # recent 按时间倒序
    occurred = [r["occurred_at"] for r in stats["recent"]]
    assert occurred == sorted(occurred, reverse=True)

    # 只读：业务表与 site 表行数都不变；也不触发清理
    assert _business_counts() == before


@pg_only
def test_dashboard_stats_excludes_expired_window_and_never_purges(secret):
    outside = _ev(now=BASE_TIME - timedelta(days=40))
    fresh = _ev()
    _flush([outside, fresh])
    stats = sss.dashboard_stats(now=BASE_TIME)
    assert stats["today"]["visits"] == 1
    assert stats["d30"]["visits"] == 1
    assert stats["d30"]["unique_visitors"] == 1
    # 30 天 daily 里最早一天也要晚于 40 天前的事件（无任何一天计入它）
    assert all(row["visits"] <= 1 for row in stats["daily"])
    assert sum(row["visits"] for row in stats["daily"]) == 1
    # 只读聚合不得顺带清理到期行（outside 行 expires_at 未到本例 now，但
    # 用一个已到期行专门验证「不清理」）
    expired = _ev(now=BASE_TIME - timedelta(days=100))
    _flush([expired])
    stats2 = sss.dashboard_stats(now=BASE_TIME)
    assert _site_count() == 3            # 旧行仍在：stats 不调清理
    assert stats2["today"]["visits"] == 1
    assert sss.purge_expired(now=BASE_TIME) == 1   # 显式清理才删


# --------------------------------------------------------------------------- #
# 9. worker / 队列降级矩阵
# --------------------------------------------------------------------------- #
def test_enqueue_without_worker_returns_false():
    assert sss.enqueue_visit(_fake_event()) is False
    assert sss.enqueue_visit(None) is False


def test_start_worker_noop_without_postgres(monkeypatch):
    monkeypatch.setenv("STORAGE_BACKEND", "json")
    sss.start_worker()
    assert sss._WORKER is None
    assert sss.enqueue_visit(_fake_event()) is False
    sss.stop_worker()


def test_start_stop_worker_idempotent(monkeypatch):
    monkeypatch.setenv("STORAGE_BACKEND", "postgres")
    monkeypatch.setattr(sss, "_flush_batch", lambda batch: None)
    sss.start_worker()
    thread = sss._WORKER["thread"]
    sss.start_worker()                       # 幂等：不重复起线程
    assert sss._WORKER["thread"] is thread
    sss.stop_worker()
    assert sss._WORKER is None
    sss.stop_worker()                        # 幂等：重复 stop 不抛
    sss.start_worker()                       # 可重启
    assert sss._WORKER is not None
    sss.stop_worker()


def test_enqueue_rejects_non_minimized_payloads(monkeypatch):
    monkeypatch.setenv("STORAGE_BACKEND", "postgres")
    monkeypatch.setattr(sss, "_flush_batch", lambda batch: None)
    sss.start_worker()
    try:
        # 队列只收 build_event 形状的最小事件——带原始 IP/UA 的 dict 形状
        # 不满足固定键集合，直接 False
        assert sss.enqueue_visit({"page_key": "home"}) is False
        assert sss.enqueue_visit({"remote_addr": "203.0.113.45",
                                  "user_agent": "x"}) is False
        assert sss.enqueue_visit(["not", "a", "dict"]) is False
    finally:
        sss.stop_worker()


def test_queue_full_returns_false_without_raise(monkeypatch):
    monkeypatch.setenv("STORAGE_BACKEND", "postgres")
    monkeypatch.setattr(sss, "_QUEUE_CAPACITY", 2)
    monkeypatch.setattr(sss, "_BATCH_SIZE", 1)
    release = threading.Event()
    started = threading.Event()

    def _blocked_flush(batch):
        started.set()
        release.wait(timeout=5)

    monkeypatch.setattr(sss, "_flush_batch", _blocked_flush)
    sss.start_worker()
    try:
        assert sss.enqueue_visit(_fake_event()) is True
        assert started.wait(timeout=5)       # 第一条已被 worker 取走（阻塞在 flush）
        assert sss.enqueue_visit(_fake_event()) is True
        assert sss.enqueue_visit(_fake_event()) is True
        assert sss.enqueue_visit(_fake_event()) is False   # 队列满：丢该条
        assert sss.enqueue_visit(None) is False
    finally:
        release.set()
        sss.stop_worker()


def test_db_failure_drops_batch_without_raise(monkeypatch, caplog):
    monkeypatch.setenv("STORAGE_BACKEND", "postgres")
    monkeypatch.setattr(sss, "_WARN_INTERVAL_SECONDS", 0.0)
    attempts = []

    def _boom():
        attempts.append(1)
        raise RuntimeError("db down")

    monkeypatch.setattr(sss.pg_store, "connect", _boom)
    sss.start_worker()
    try:
        with caplog.at_level(logging.WARNING, logger="svs.site_stats"):
            assert sss.enqueue_visit(_fake_event()) is True
            assert _wait_until(lambda: bool(attempts))
            assert sss.enqueue_visit(_fake_event()) is True
            assert _wait_until(lambda: len(attempts) >= 2)
        # 两次 flush 全部失败但 worker 未崩、异常未外泄、日志无事件内容
        assert sss._WORKER["thread"].is_alive()
        assert "daily_visitor_hash" not in caplog.text
        assert "203.0.113" not in caplog.text
    finally:
        sss.stop_worker()


# --------------------------------------------------------------------------- #
# 10. app.py 接线契约（并行代理实施中；默认启用，未接线时红属预期）
# --------------------------------------------------------------------------- #
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


@site_stats_app_wiring
def test_app_admin_site_stats_owner_only(monkeypatch):
    monkeypatch.setattr(app_mod, "AUTH_ENABLED", True)
    owner = user_store.create_user("owner@x.test", "ownerpass1234567",
                                   role="owner", display_name="Owner")
    user = user_store.create_user("user@x.test", "userpass12345678",
                                  role="user", display_name="User")
    client = _client()

    # 匿名 401
    resp = client.get("/api/admin/v1/site-stats")
    assert resp.status_code == 401
    # user 403
    _login(client, user)
    resp = client.get("/api/admin/v1/site-stats")
    assert resp.status_code == 403
    # owner 200 且形状与钉死契约逐键一致
    _login(client, owner)
    resp = client.get("/api/admin/v1/site-stats")
    assert resp.status_code == 200
    payload = resp.get_json()
    assert set(payload.keys()) == DASHBOARD_KEYS
    for key in ("today", "d7", "d30"):
        assert set(payload[key].keys()) == WINDOW_KEYS
    assert set(payload["visitor_kinds"].keys()) == set(sss.VISITOR_KINDS)
    assert len(payload["daily"]) == 30
    for row in payload["daily"]:
        assert set(row.keys()) == DAILY_KEYS
    assert payload["geo_configured"] is False
    if BACKEND == "postgres":
        # 无写副作用：调用前后 site 表行数不变（不创建事件、不清理）
        n_before = _site_count()
        resp = client.get("/api/admin/v1/site-stats")
        assert resp.status_code == 200
        assert _site_count() == n_before


@site_stats_app_wiring
@pg_only
def test_app_after_request_records_public_html_get(secret, monkeypatch):
    monkeypatch.setattr(app_mod, "AUTH_ENABLED", True)
    sss.start_worker()                       # 幂等；若 app 启动已起则 no-op
    try:
        client = _client()
        resp = client.get("/")               # 公开 HTML GET → 应落 home 事件
        assert resp.status_code == 200
        assert _wait_until(lambda: _site_count() >= 1)
        row = _sql_one("SELECT page_key, visitor_kind, country_code,"
                       " referrer_domain FROM site_visit_events")
        assert row["page_key"] == "home"
        assert row["visitor_kind"] in sss.VISITOR_KINDS
        assert row["country_code"] == "unknown"
        assert row["referrer_domain"] == "direct"
        # 非 HTML JSON 端点不落事件
        n = _site_count()
        assert client.get("/healthz").status_code == 200
        time.sleep(1.0)
        assert _site_count() == n
    finally:
        sss.stop_worker()


# --------------------------------------------------------------------------- #
# 11. F4 + review R2-F5：每日保留任务接线（双跑纯单元）。
# R2-F5 拆分后 acquisition 段（_run_daily_retention_once）与 site stats 段
# （_run_site_stats_retention_once）各自独立函数/线程/开关，互不牵连
# --------------------------------------------------------------------------- #
def test_run_daily_retention_once_runs_acquisition_segment(monkeypatch):
    """acquisition 段：_run_daily_retention_once 只做来源触点清理
    （R2-F5 拆分后不再连带 site stats）。"""
    calls = []
    monkeypatch.setattr(app_mod.acquisition_store, "run_visit_retention",
                        lambda: calls.append("acq") or (3, 2))

    def _unexpected():
        raise AssertionError("R2-F5 拆分后 site stats 段不应被 "
                             "_run_daily_retention_once 调用")

    monkeypatch.setattr(sss, "purge_expired", _unexpected)
    monkeypatch.setattr(app_mod, "site_stats_store", sss)
    app_mod._run_daily_retention_once()
    assert calls == ["acq"]


def test_run_site_stats_retention_once_runs_purge(monkeypatch):
    """site stats 段：_run_site_stats_retention_once 只跑 purge_expired
    （store 非 None），不做 acquisition 清理。"""
    calls = []
    monkeypatch.setattr(sss, "purge_expired",
                        lambda: calls.append("purge") or 5)
    monkeypatch.setattr(app_mod, "site_stats_store", sss)

    def _unexpected():
        raise AssertionError("R2-F5 拆分后 acquisition 段不应被 "
                             "_run_site_stats_retention_once 调用")

    monkeypatch.setattr(app_mod.acquisition_store, "run_visit_retention",
                        _unexpected)
    app_mod._run_site_stats_retention_once()
    assert calls == ["purge"]


def test_retention_segments_do_not_take_each_other_down(monkeypatch):
    """互不拖垮：任一段异常不外泄、另一段照常执行（各段独立 try/except）。"""
    calls = []

    def _acq_boom():
        raise RuntimeError("acq down")

    monkeypatch.setattr(app_mod.acquisition_store, "run_visit_retention",
                        _acq_boom)
    monkeypatch.setattr(sss, "purge_expired",
                        lambda: calls.append("purge") or 4)
    monkeypatch.setattr(app_mod, "site_stats_store", sss)
    app_mod._run_daily_retention_once()          # acquisition 异常被吞
    app_mod._run_site_stats_retention_once()
    assert calls == ["purge"]
    # 反向：site stats 段异常，acquisition 段照常执行
    calls.clear()

    def _purge_boom():
        raise RuntimeError("stats down")

    monkeypatch.setattr(app_mod.acquisition_store, "run_visit_retention",
                        lambda: calls.append("acq") or (1, 0))
    monkeypatch.setattr(sss, "purge_expired", _purge_boom)
    app_mod._run_daily_retention_once()
    app_mod._run_site_stats_retention_once()    # purge 异常被吞
    assert calls == ["acq"]


def test_run_site_stats_retention_once_skips_missing_store(monkeypatch):
    """store 未随镜像发布（import 容错 None）：site stats 段静默跳过——
    与 _start_site_stats_worker 同口径。"""

    def _unexpected():
        raise AssertionError("store 为 None 时 purge_expired 不应被调用")

    monkeypatch.setattr(sss, "purge_expired", _unexpected)
    monkeypatch.setattr(app_mod, "site_stats_store", None)
    app_mod._run_site_stats_retention_once()    # 不抛即通过


def test_acq_interval_zero_does_not_affect_site_stats_segment(monkeypatch):
    """review R2-F5 核心契约：运维把 ACQ 间隔设 0（归因已退役的合理操作）
    只关掉 acquisition 调度线程，site stats 段开关/执行完全独立（R2-F5 拆分
    理由）。函数级验证：monkeypatch 后直接调 _run_site_stats_retention_once，
    不依赖 import 期线程。"""
    calls = []

    def _no_acq():
        raise AssertionError("site stats 段不应做 acquisition 清理")

    monkeypatch.setenv("ACQ_RETENTION_INTERVAL_SECONDS", "0")
    # ACQ 间隔=0 → acquisition 调度线程不启动（budget 特性不可用时本就 None）
    if app_mod.platform_features.budget_features_available():
        assert app_mod._start_acquisition_retention_thread() is None
    # 同一 env 前提下，site stats 单轮清理照常执行（独立函数，不触 acquisition）
    monkeypatch.setattr(app_mod.acquisition_store, "run_visit_retention",
                        _no_acq)
    monkeypatch.setattr(sss, "purge_expired",
                        lambda: calls.append("purge") or 7)
    monkeypatch.setattr(app_mod, "site_stats_store", sss)
    app_mod._run_site_stats_retention_once()
    assert calls == ["purge"]
    # site stats 自己的开关独立生效：间隔=0 关闭；正间隔以契约线程名启动
    monkeypatch.setenv("SITE_STATS_RETENTION_INTERVAL_SECONDS", "0")
    assert app_mod._start_site_stats_retention_thread() is None
    monkeypatch.setenv("SITE_STATS_RETENTION_INTERVAL_SECONDS", "86400")
    th = app_mod._start_site_stats_retention_thread()
    assert th is not None and th.name == "site-stats-retention" and th.daemon
