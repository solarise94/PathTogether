# -*- coding: utf-8 -*-
"""匿名「浏览器内本地转换完成」计数（0094）——store / HTTP / 迁移。

覆盖：
  - store：upsert +1、UTC+8 日界、格式归一化、summary/dashboard 窗口、
    非法来源不写库；
  - HTTP：POST 204（正常/忽略都 204）、CSRF 豁免仅精确路径、匿名放行、
    bot 忽略、同源要求、每访客每日 20 次上限、非法 source 400、body 1 KB
    上限、无 Set-Cookie；GET summary 形状/缓存头/失败 503；
  - 迁移：表/列/CHECK/PK + 文件重放幂等。

运行：cd PathTogether && python3 -m pytest tests/test_tool_conversions.py -q
"""
import json
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import _bootstrap  # noqa: E402,F401  # session 目录 + openslide stub（conftest 先行）
UPLOAD_DIR = _bootstrap.UPLOAD_DIR
import pytest  # noqa: E402

import app as app_mod  # noqa: E402
import pg_store  # noqa: E402
import site_stats_store as sss  # noqa: E402
import tool_conversion_store as tcs  # noqa: E402
from _pt_helpers import (  # noqa: E402
    isolate_app,
    pg_connection as _conn,
    make_client as _client,
)

UTC = timezone.utc
FIXED_NOW = datetime(2026, 11, 10, 2, 0, 0, tzinfo=UTC)   # 本地 11-10 10:00
BROWSER_UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X) Safari/605.1.15"
_SECRET_BYTES = b"unit-test-secret-0123456789abcdef-tool-conversions"


# --------------------------------------------------------------------------- #
# 基建
# --------------------------------------------------------------------------- #
@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    isolate_app(monkeypatch, tmp_path, clear_stores=True)
    sss._reset_warn_state()
    tcs.reset_daily_caps()
    monkeypatch.delenv("SITE_STATS_HMAC_SECRET_FILE", raising=False)
    yield
    tcs.reset_daily_caps()


@pytest.fixture
def secret(tmp_path, monkeypatch):
    path = tmp_path / "site_stats.secret"
    path.write_bytes(_SECRET_BYTES)
    os.chmod(path, 0o600)
    monkeypatch.setenv("SITE_STATS_HMAC_SECRET_FILE", str(path))
    return path


def _rows():
    conn = _conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT day, source, format, count FROM tool_conversion_daily"
                " ORDER BY day, source, format")
            return cur.fetchall()
    finally:
        conn.close()


def _beacon(client, payload, *, host="localhost", ua=BROWSER_UA, remote=None,
            origin=None, referer=None, raw=None):
    headers = {"User-Agent": ua}
    if origin is not None:
        headers["Origin"] = origin
    else:
        headers["Origin"] = "http://" + host
    if referer is not None:
        headers["Referer"] = referer
    headers["Content-Type"] = "application/json"
    env = {"REMOTE_ADDR": remote} if remote else {}
    body = raw if raw is not None else json.dumps(payload)
    return client.post(app_mod.TOOL_CONVERSIONS_PATH, data=body,
                       headers=headers, environ_base=env)


def _raw_client():
    app_mod.app.config["TESTING"] = True
    return app_mod.app.test_client()


# --------------------------------------------------------------------------- #
# 1. store：upsert / 日界 / 归一化
# --------------------------------------------------------------------------- #
def test_record_upserts_same_day_source_format():
    assert tcs.record("converter", "svs", now=FIXED_NOW)
    assert tcs.record("converter", ".SVS", now=FIXED_NOW)
    assert tcs.record("workbench", "aperio-svs-jp2k", now=FIXED_NOW)
    rows = _rows()
    assert len(rows) == 2
    by_key = {(r["source"], r["format"]): r for r in rows}
    assert by_key[("converter", "svs")]["count"] == 2
    assert by_key[("workbench", "svs")]["count"] == 1
    assert all(str(r["day"]) == "2026-11-10" for r in rows)


def test_record_utc8_day_boundary():
    # 本地 23:59:59 与次日 00:00:00 分属两天
    before = datetime(2026, 11, 10, 15, 59, 59, tzinfo=UTC)
    after = datetime(2026, 11, 10, 16, 0, 0, tzinfo=UTC)
    assert tcs.record("converter", "kfb", now=before)
    assert tcs.record("converter", "kfb", now=after)
    rows = _rows()
    assert [(str(r["day"]), r["count"]) for r in rows] == [
        ("2026-11-10", 1), ("2026-11-11", 1)]


@pytest.mark.parametrize("raw,expected", [
    ("svs", "svs"), (".SVS", "svs"), ("aperio-svs-jp2k", "svs"),
    ("jpeg", "jpg"), (".jpg", "jpg"), ("tif", "tiff"),
    ("ome.tiff", "ome-tiff"), ("mirax-bundle", "mrxs"),
    ("unic-tmap06-jpeg", "tmap"), ("plain-image-bmp-jpeg", "other"),
    ("kfbf", "kfbf"), ("kfb_bf_v1", "kfb"), ("vms", "vms"),
    ("czi", "czi"), ("vsi", "vsi"), ("sdpc", "sdpc"), ("png", "png"),
    ("something-weird", "other"), ("", "other"), (None, "other"),
    ("x" * 100, "other"),
])
def test_normalize_format(raw, expected):
    assert tcs.normalize_format(raw) == expected


def test_record_rejects_invalid_source():
    assert tcs.record("bogus", "svs", now=FIXED_NOW) is False
    assert tcs.record(None, "svs", now=FIXED_NOW) is False
    assert _rows() == []


# --------------------------------------------------------------------------- #
# 2. store：summary / dashboard
# --------------------------------------------------------------------------- #
def test_summary_empty_and_since():
    assert tcs.summary() == {"total": 0, "since": None}
    tcs.record("converter", "svs", now=datetime(2026, 11, 9, 2, tzinfo=UTC))
    tcs.record("workbench", "kfb", now=FIXED_NOW)
    assert tcs.summary() == {"total": 2, "since": "2026-11-09"}


def test_dashboard_windows_sources_formats():
    tcs.record("converter", "svs", now=datetime(2026, 11, 10, 1, tzinfo=UTC))
    tcs.record("converter", "svs", now=datetime(2026, 11, 10, 2, tzinfo=UTC))
    tcs.record("workbench", "kfb", now=datetime(2026, 11, 10, 3, tzinfo=UTC))
    tcs.record("converter", "kfb", now=datetime(2026, 11, 9, 2, tzinfo=UTC))
    tcs.record("workbench", "kfb", now=datetime(2026, 11, 9, 3, tzinfo=UTC))
    tcs.record("converter", "ndpi", now=datetime(2026, 11, 5, 2, tzinfo=UTC))
    tcs.record("converter", "tiff", now=datetime(2026, 10, 20, 2, tzinfo=UTC))
    tcs.record("converter", "ndpi", now=datetime(2026, 10, 1, 2, tzinfo=UTC))

    stats = tcs.dashboard(now=FIXED_NOW)
    assert stats["today"] == 3
    assert stats["d7"] == 6            # 11-04..11-10
    assert stats["d30"] == 7           # 10-12..11-10
    assert stats["all"] == 8
    assert stats["by_source"] == {"converter": 5, "workbench": 2}
    assert stats["by_format"] == [
        {"format": "kfb", "count": 3},
        {"format": "svs", "count": 2},
        {"format": "ndpi", "count": 1},
        {"format": "tiff", "count": 1},
    ]


def test_dashboard_empty_shape():
    stats = tcs.dashboard(now=FIXED_NOW)
    assert stats == {
        "today": 0, "d7": 0, "d30": 0, "all": 0,
        "by_source": {"converter": 0, "workbench": 0},
        "by_format": [],
    }


# --------------------------------------------------------------------------- #
# 3. 迁移：列 / CHECK / PK / 幂等
# --------------------------------------------------------------------------- #
def test_migration_0094_schema_and_replay():
    conn = _conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT column_name, data_type FROM information_schema.columns"
                " WHERE table_name = 'tool_conversion_daily'"
                " ORDER BY ordinal_position")
            cols = cur.fetchall()
            assert [(r["column_name"], r["data_type"]) for r in cols] == [
                ("day", "date"), ("source", "text"), ("format", "text"),
                ("count", "bigint"),
            ]
            # PK (day, source, format)：重复行被拒绝
            cur.execute(
                "INSERT INTO tool_conversion_daily VALUES"
                " ('2026-11-10', 'converter', 'svs', 1)")
            with pytest.raises(Exception):
                cur.execute(
                    "INSERT INTO tool_conversion_daily VALUES"
                    " ('2026-11-10', 'converter', 'svs', 1)")
        conn.rollback()
        with conn.cursor() as cur:
            # source CHECK 词表（非法值拒绝）
            with pytest.raises(Exception):
                cur.execute(
                    "INSERT INTO tool_conversion_daily VALUES"
                    " ('2026-11-10', 'other', 'svs', 1)")
        conn.rollback()
        with conn.cursor() as cur:
            # 迁移文件重放幂等（CREATE IF NOT EXISTS + 固定 audit id）
            sql = (pg_store.migrations_dir()
                   / "0094_tool_conversion_counts.sql").read_text("utf-8")
            cur.execute(sql)
            cur.execute(sql)
        conn.commit()
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# 4. HTTP：POST beacon
# --------------------------------------------------------------------------- #
def test_post_counts_and_returns_204_without_cookie(secret):
    client = _raw_client()
    client.get("/login")     # 先建立 session/csrf cookie，验证 beacon 不依赖它
    r = _beacon(client, {"source": "converter", "format": "aperio-svs-jpeg"})
    assert r.status_code == 204
    assert "Set-Cookie" not in r.headers
    assert r.get_data() == b""
    rows = _rows()
    assert len(rows) == 1
    assert rows[0]["source"] == "converter"
    assert rows[0]["format"] == "svs"
    assert rows[0]["count"] == 1


def test_post_anonymous_allowed_when_auth_enabled(monkeypatch, secret):
    monkeypatch.setattr(app_mod, "AUTH_ENABLED", True)
    client = _raw_client()
    assert client.get("/tools/slides").status_code == 200
    r = _beacon(client, {"source": "workbench", "format": "kfb"})
    assert r.status_code == 204
    s = client.get(app_mod.TOOL_CONVERSIONS_SUMMARY_PATH)
    assert s.status_code == 200
    assert s.get_json()["total"] == 1
    assert "Set-Cookie" not in s.headers


def test_csrf_exemption_scoped_to_exact_paths():
    assert app_mod._csrf_exempt_path(app_mod.TOOL_CONVERSIONS_PATH) is True
    assert app_mod._csrf_exempt_path(
        app_mod.TOOL_CONVERSIONS_SUMMARY_PATH) is True
    assert app_mod._csrf_exempt_path(
        app_mod.TOOL_CONVERSIONS_PATH + "/extra") is False
    assert app_mod._csrf_exempt_path("/api/tools/conversions2") is False
    assert app_mod._csrf_exempt_path("/api/tools/other") is False


def test_post_requires_same_origin(secret):
    client = _raw_client()
    # 无 Origin 且无 Referer → 静默 204 不计数
    headers = {"User-Agent": BROWSER_UA, "Content-Type": "application/json"}
    r = client.post(app_mod.TOOL_CONVERSIONS_PATH,
                    data=json.dumps({"source": "converter", "format": "svs"}),
                    headers=headers)
    assert r.status_code == 204 and _rows() == []
    # 跨源 Origin 被忽略
    r = _beacon(client, {"source": "converter", "format": "svs"},
                origin="https://evil.example")
    assert r.status_code == 204 and _rows() == []
    # Referer 回退：同源可用
    r = _beacon(client, {"source": "converter", "format": "svs"},
                origin="", referer="http://localhost/tools/slides")
    assert r.status_code == 204
    assert len(_rows()) == 1


def test_post_bot_is_ignored(secret):
    client = _raw_client()
    r = _beacon(client, {"source": "converter", "format": "svs"},
                ua="Googlebot/2.1 (+http://www.google.com/bot.html)")
    assert r.status_code == 204
    assert _rows() == []


def test_post_invalid_source_is_400(secret):
    client = _raw_client()
    r = _beacon(client, {"source": "bogus", "format": "svs"})
    assert r.status_code == 400
    assert r.get_json() == {"error": "invalid_request"}
    assert _rows() == []


def test_post_body_limit_and_malformed_json(secret):
    client = _raw_client()
    big = json.dumps({"source": "converter", "format": "x" * 2000})
    r = _beacon(client, None, raw=big)
    assert r.status_code == 400
    for raw in ("", "not-json", "[]", '"svs"'):
        assert _beacon(client, None, raw=raw).status_code == 400
    assert _rows() == []


def test_post_per_visitor_daily_cap(secret):
    client = _raw_client()
    for _ in range(tcs.MAX_PER_VISITOR_PER_DAY + 3):
        assert _beacon(client, {"source": "converter", "format": "svs"},
                       remote="203.0.113.10").status_code == 204
    rows = _rows()
    assert len(rows) == 1
    assert rows[0]["count"] == tcs.MAX_PER_VISITOR_PER_DAY
    # 另一个 /24 前缀独立计数（日轮换哈希按 IPv4 /24 归约）
    assert _beacon(client, {"source": "converter", "format": "svs"},
                   remote="203.0.114.11").status_code == 204
    assert _rows()[0]["count"] == tcs.MAX_PER_VISITOR_PER_DAY + 1


def test_post_record_failure_still_204(monkeypatch, secret):
    client = _raw_client()

    def boom(*_a, **_kw):
        raise RuntimeError("db down")

    monkeypatch.setattr(tcs, "record", boom)
    r = _beacon(client, {"source": "converter", "format": "svs"})
    assert r.status_code == 204


def test_post_store_missing_is_noop(monkeypatch, secret):
    client = _raw_client()
    monkeypatch.setattr(app_mod, "tool_conversion_store", None)
    assert _beacon(client, {"source": "converter", "format": "svs"}).status_code == 204
    assert client.get(app_mod.TOOL_CONVERSIONS_SUMMARY_PATH).status_code == 503


# --------------------------------------------------------------------------- #
# 5. HTTP：GET summary
# --------------------------------------------------------------------------- #
def test_summary_shape_cache_and_no_cookie(secret):
    client = _raw_client()
    client.get("/login")
    _beacon(client, {"source": "converter", "format": "kfb"})
    _beacon(client, {"source": "workbench", "format": "svs"})
    r = client.get(app_mod.TOOL_CONVERSIONS_SUMMARY_PATH)
    assert r.status_code == 200
    today = tcs._local_day().isoformat()
    assert r.get_json() == {"total": 2, "since": today}
    assert r.headers["Cache-Control"] == "public, max-age=60"
    assert "Set-Cookie" not in r.headers


def test_summary_db_failure_is_503(monkeypatch, secret):
    client = _raw_client()

    def boom(**_kw):
        raise RuntimeError("db down")

    monkeypatch.setattr(tcs, "summary", boom)
    r = client.get(app_mod.TOOL_CONVERSIONS_SUMMARY_PATH)
    assert r.status_code == 503
    assert r.get_json() == {"error": "unavailable"}
