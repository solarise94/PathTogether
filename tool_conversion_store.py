# -*- coding: utf-8 -*-
"""匿名「浏览器内本地转换完成」计数（tool_conversion_daily，0094）。

背景（2026-11-10 产品决定）：切片转换完全在用户浏览器内完成（WASM），
服务端此前不知道任何一次转换已完成。本模块为两个来源提供最小计数：

- ``converter``：公开转换器页 ``/tools/slides`` 的成功转换；
- ``workbench``：工作台上传面板 inline 转换的成功转换（转换完成即计数，
  与之后是否上传无关）。

隐私边界（违反任一即发布停止条件）：

- 落库只有 ``day``（UTC+8 日界，与 site_stats 一致）、``source``、
  归一化输入格式 ``format`` 与 ``count``——**没有**文件名、大小、IP、
  用户 ID、session、UA、完整哈希；
- 防刷用的访客键复用 :mod:`site_stats_store` 的日轮换 HMAC 约定
  （``_visitor_prefix`` + ``_daily_visitor_hash``，secret 只来自
  ``SITE_STATS_HMAC_SECRET_FILE``）；该哈希**只在进程内字典中**用于当天的
  20 次上限判断，不落库、不进日志；
- secret 未配置时使用进程内随机盐（同样不落库、不写日志），计数继续工作
  （与 site_stats 的 fail-closed 采集不同——转换计数不是安全边界）。

降级：``record`` 的 DB 异常由调用方（HTTP beacon）吞掉，页面永不阻塞；
``summary``/``dashboard`` 的 DB 异常向上抛，HTTP 层返回 503，转换器页据此
隐藏统计行。

每进程上限的已知局限：上限字典是**进程内**状态，gunicorn 多 worker 部署下
每个 worker 各自计满 20 次（最坏 20 × worker 数）。这是刻意接受的近似——
计数是非关键指标，宁可少计，也不为它引入跨进程存储或额外数据库写。

风格对齐 site_stats_store：模块级函数 + 中文注释；import 零副作用
（不连库、不起线程、不读 secret）。
"""

import logging
import secrets
import threading
from datetime import date, datetime, timedelta, timezone

import pg_store
import site_stats_store

_log = logging.getLogger("svs.tool_conversions")

# --------------------------------------------------------------------------- #
# 口径常量
# --------------------------------------------------------------------------- #
#: 允许的来源（与 0094 CHECK 一致）
SOURCES = ("converter", "workbench")

#: 归一化输入格式白名单（对外契约）。未知/无法识别 → ``other``。
FORMATS = (
    "kfb", "kfbf", "svs", "scn", "ndpi", "bif", "tmap", "mrxs", "vms",
    "vsi", "czi", "sdpc", "tiff", "ome-tiff", "jpg", "bmp", "png",
)
OTHER_FORMAT = "other"

#: 每访客每日最多计入的成功转换次数（跨来源合计）
MAX_PER_VISITOR_PER_DAY = 20
#: 进程内上限字典的最大键数（防伪造 IP 前缀把内存撑爆；满后新键不再计数）
_CAP_MAX_KEYS = 10000

#: UTC+8 日界（Asia/Shanghai 无夏令时；避免依赖容器 tzdata）
_STATS_TIMEZONE = timezone(timedelta(hours=8))

#: 显式别名（完整格式 id / 常见扩展名 → 归一化 token）。
#: 顺序无关（精确查表）；长前缀匹配见 ``_FORMAT_PREFIXES``。
_FORMAT_ALIASES = {
    "jpeg": "jpg",
    "tif": "tiff",
    "ome.tif": "ome-tiff",
    "ome.tiff": "ome-tiff",
    "tmap06": "tmap",
    "tmap-06": "tmap",
    "kfb_bf_v1": "kfb",
    "kfb_kfbio_jpeg": "kfb",
    "kfbf": "kfbf",
    "mirax": "mrxs",
    "mirax-bundle": "mrxs",
    "aperio-svs": "svs",
    "aperio-svs-jpeg": "svs",
    "aperio-svs-jp2k": "svs",
    "svs-jp2k": "svs",
    "leica-scn": "scn",
    "leica-scn-jpeg": "scn",
    "hamamatsu-ndpi": "ndpi",
    "hamamatsu-ndpi-jpeg": "ndpi",
    "hamamatsu-vms": "vms",
    "hamamatsu-vms-bundle": "vms",
    "ventana-bif": "bif",
    "ventana-bif-jpeg": "bif",
    "unic-tmap06": "tmap",
    "unic-tmap06-jpeg": "tmap",
    "generic-tiled-jpeg-tiff": "tiff",
    "plain-image": OTHER_FORMAT,
    "plain-image-bmp-jpeg": OTHER_FORMAT,
    "unknown": OTHER_FORMAT,
}

#: 前缀匹配：完整的检测器 format id（如 ``aperio-svs-jp2k``）按前缀归约；
#: 长前缀必须先于短前缀（``kfbf`` 先于 ``kfb``）。
_FORMAT_PREFIXES = (
    ("kfbf", "kfbf"),
    ("kfb", "kfb"),
    ("aperio-svs", "svs"),
    ("svs", "svs"),
    ("leica-scn", "scn"),
    ("scn", "scn"),
    ("hamamatsu-ndpi", "ndpi"),
    ("ndpi", "ndpi"),
    ("hamamatsu-vms", "vms"),
    ("vms", "vms"),
    ("ventana-bif", "bif"),
    ("bif", "bif"),
    ("unic-tmap", "tmap"),
    ("tmap", "tmap"),
    ("mirax", "mrxs"),
    ("mrxs", "mrxs"),
    ("generic-tiled", "tiff"),
    ("ome-tiff", "ome-tiff"),
    ("czi", "czi"),
    ("vsi", "vsi"),
    ("sdpc", "sdpc"),
)

#: 单次输入格式字符串长度上限（超长直接归 other，避免异常输入放大）
_FORMAT_MAX_LEN = 64


# --------------------------------------------------------------------------- #
# 日期 / 格式 / 来源归一化
# --------------------------------------------------------------------------- #
def _local_day(moment=None):
    """moment（默认现在）所在的 UTC+8 日期。"""
    if moment is None:
        moment = datetime.now(timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(_STATS_TIMEZONE).date()


def normalize_format(raw):
    """输入格式 → 白名单 token；未知/空/超长 → ``other``。

    接受三类输入：页面已知的检测器 format id（``aperio-svs-jp2k`` 等）、
    文件扩展名（``.svs`` / ``jpeg``）与已归一化 token。大小写不敏感。
    """
    value = str(raw or "").strip().lower()
    if value.startswith("."):
        value = value[1:]
    if not value or len(value) > _FORMAT_MAX_LEN:
        return OTHER_FORMAT
    if value in _FORMAT_ALIASES:
        return _FORMAT_ALIASES[value]
    if value in FORMATS:
        return value
    for prefix, token in _FORMAT_PREFIXES:
        if value.startswith(prefix):
            return token
    # 兜底：``foo-bar-tiff`` / ``foo_bar_svs`` 这类以 token 结尾的 id
    for token in FORMATS:
        if token != "ome-tiff" and (value.endswith("-" + token)
                                    or value.endswith("_" + token)):
            return token
    return OTHER_FORMAT


def normalize_source(raw):
    """来源 → ``converter``/``workbench``；非法返回 None。"""
    value = str(raw or "").strip().lower()
    return value if value in SOURCES else None


# --------------------------------------------------------------------------- #
# 防刷：每访客每日上限（进程内；日轮换 HMAC，不落库）
# --------------------------------------------------------------------------- #
_CAP_LOCK = threading.Lock()
_CAP_DAY = None
_CAP_COUNTS = {}
#: secret 未配置时的进程内随机盐（只用于哈希输入，绝不落库/日志）
_FALLBACK_SECRET = secrets.token_bytes(32)


def _visitor_key(remote_addr, day):
    """当日访客键：HMAC(secret, day + IP 前缀)；无 IP 时归入 unknown 桶。"""
    prefix = site_stats_store._visitor_prefix(remote_addr) or "unknown"
    secret = site_stats_store._load_secret()
    if not secret:
        secret = _FALLBACK_SECRET
    return site_stats_store._daily_visitor_hash(
        secret, day.isoformat(), prefix)


def allow_visitor(remote_addr, *, now=None,
                  limit=MAX_PER_VISITOR_PER_DAY):
    """判定并占用一次当日配额：True = 可以计数，False = 静默忽略。

    字典按本地日轮换；键数达到 ``_CAP_MAX_KEYS`` 后新键一律拒绝（内存有界，
    宁可少计也不让伪造前缀撑爆进程）。
    """
    day = _local_day(now)
    key = _visitor_key(remote_addr, day)
    global _CAP_DAY, _CAP_COUNTS
    with _CAP_LOCK:
        if _CAP_DAY != day:
            _CAP_DAY = day
            _CAP_COUNTS = {}
        current = _CAP_COUNTS.get(key, 0)
        if current >= limit:
            return False
        if key not in _CAP_COUNTS and len(_CAP_COUNTS) >= _CAP_MAX_KEYS:
            return False
        _CAP_COUNTS[key] = current + 1
        return True


def reset_daily_caps():
    """测试辅助：清空进程内当日本地配额。"""
    global _CAP_DAY, _CAP_COUNTS
    with _CAP_LOCK:
        _CAP_DAY = None
        _CAP_COUNTS = {}


def is_bot(user_agent):
    """复用 site_stats 的 UA 词表：命中疑似 bot → True（只作计数忽略依据）。"""
    return site_stats_store._classify_user_agent(user_agent) is not None


# --------------------------------------------------------------------------- #
# DB 读写
# --------------------------------------------------------------------------- #
def _dict_row():
    import psycopg
    return psycopg.rows.dict_row


def record(source, fmt, *, now=None):
    """成功完成一次转换：``(day, source, format)`` 计数 +1。

    非法来源返回 False（不写库）；格式按 ``normalize_format`` 归一化。
    DB 异常向上抛，由 HTTP beacon 捕获并忽略（页面响应永不受影响）。
    """
    source = normalize_source(source)
    if source is None:
        return False
    day = _local_day(now)
    fmt = normalize_format(fmt)
    conn = pg_store.connect()
    try:
        with conn.transaction():
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO tool_conversion_daily"
                    " (day, source, format, count) VALUES (%s, %s, %s, 1)"
                    " ON CONFLICT (day, source, format) DO UPDATE"
                    " SET count = tool_conversion_daily.count + 1",
                    (day, source, fmt))
        return True
    finally:
        conn.close()


def _parse_since(value):
    if value is None:
        return None
    if isinstance(value, datetime):
        value = value.date()
    if isinstance(value, date):
        return value.isoformat()
    return str(value)


def summary(*, now=None):
    """公开汇总：``{"total": int, "since": "YYYY-MM-DD" | None}``。

    ``since`` = 表中最早计入的日期（发布日起算，无历史回填）；无行时为 None。
    DB 异常向上抛（HTTP 层 503；客户端隐藏统计行）。
    """
    conn = pg_store.connect()
    conn.row_factory = _dict_row()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT COALESCE(SUM(count), 0) AS total,"
                " MIN(day) AS since FROM tool_conversion_daily")
            row = cur.fetchone()
        return {
            "total": int(row["total"] or 0),
            "since": _parse_since(row["since"]),
        }
    finally:
        conn.close()


def empty_dashboard():
    """固定空形状（store 不可用/查询失败时 admin 面板的降级值）。"""
    return {
        "today": 0, "d7": 0, "d30": 0, "all": 0,
        "by_source": {source: 0 for source in SOURCES},
        "by_format": [],
    }


def dashboard(*, now=None):
    """admin 只读聚合：今日 / 近 7 天 / 近 30 天 / 累计 + 来源 + 格式。

    - ``today`` 起算点 = 当前 UTC+8 日零点；
    - ``d7`` = 今天与前 6 天（与 site_stats 的 7 天窗口同口径）；
    - ``d30`` = 今天与前 29 天；
    - ``by_source`` 与 ``by_format`` 均限定近 30 天。
    """
    moment = now if now is not None else datetime.now(timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    moment_utc = moment.astimezone(timezone.utc)
    today = moment_utc.astimezone(_STATS_TIMEZONE).date()
    d7_start = today - timedelta(days=6)
    d30_start = today - timedelta(days=29)

    conn = pg_store.connect()
    conn.row_factory = _dict_row()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT"
                " COALESCE(SUM(count) FILTER (WHERE day >= %s), 0) AS today,"
                " COALESCE(SUM(count) FILTER (WHERE day >= %s), 0) AS d7,"
                " COALESCE(SUM(count) FILTER (WHERE day >= %s), 0) AS d30,"
                " COALESCE(SUM(count), 0) AS all"
                " FROM tool_conversion_daily",
                (today, d7_start, d30_start))
            totals = cur.fetchone()

            cur.execute(
                "SELECT source, SUM(count) AS count"
                " FROM tool_conversion_daily WHERE day >= %s"
                " GROUP BY source", (d30_start,))
            by_source = {source: 0 for source in SOURCES}
            for row in cur.fetchall():
                source = str(row["source"] or "")
                if source in by_source:
                    by_source[source] = int(row["count"] or 0)

            cur.execute(
                "SELECT format, SUM(count) AS count"
                " FROM tool_conversion_daily WHERE day >= %s"
                " GROUP BY format ORDER BY count DESC, format ASC",
                (d30_start,))
            by_format = [
                {"format": str(row["format"] or OTHER_FORMAT),
                 "count": int(row["count"] or 0)}
                for row in cur.fetchall()
            ]
    finally:
        conn.close()

    return {
        "today": int(totals["today"] or 0),
        "d7": int(totals["d7"] or 0),
        "d30": int(totals["d30"] or 0),
        "all": int(totals["all"] or 0),
        "by_source": by_source,
        "by_format": by_format,
    }
