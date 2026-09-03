# -*- coding: utf-8 -*-
"""站点匿名访问统计（site_visit_events，Batch D2）。

docs review-2026-09-02-upload-user-limits-admin-ui-cleanup.md §3.4 / §4.4 /
§Batch D2 / §7.2。与用户归因（acquisition，已退役）完全解耦：本模块只做
「站长观测」——公开页面访问趋势、外部来源、匿名访客近似数、疑似爬虫；
**没有** user/invite/session/AI usage 关联，不构建 first/last touch 或
转化漏斗，不出现"独立用户数"（日轮换哈希跨日不可识别同一人）。

对外契约（app.py 接线按此调用，一字不差）：

- ``SITE_BOT_UA_RULESET_VERSION``：bot 词表版本常量（词表修改必须同步提版）；
- ``build_event(**kwargs) -> dict | None``：纯函数。非 allowlist 路径 /
  非 2xx-3xx / 非 HTML / 含 query token 或资源 ID → None；返回 dict 键固定
  page_key/occurred_at/dedup_bucket/referrer_domain/utm_source/country_code/
  daily_visitor_hash/visitor_kind/bot_name（即落库最小事件，无原始 IP/UA/
  query/token/资源 ID——原始值在本函数内计算派生后即丢弃）；
- ``enqueue_visit(event) -> bool``：有界进程内队列 put_nowait；队列满 /
  worker 未启动 / 后端不可用 → False（丢该条），**永不抛异常**；
- ``start_worker()``：后台批量写线程，幂等（PostgreSQL 为唯一后端）；
- ``stop_worker()``：幂等；进程退出允许丢队列残余（统计允许少计，绝不阻塞
  页面响应）；
- ``dashboard_stats(*, now=None) -> dict``：owner-only 只读固定聚合，
  无写副作用、不创建事件、不调清理；全部来自 site_visit_events，不联任何
  业务表；
- ``purge_expired(*, now=None) -> int``：显式 retention 清理，只删
  ``expires_at`` 到期的 site events，返回删除行数。

降级矩阵（页面服务永不受影响；统计允许少计）：

==========================================  ==============================
故障                                        行为
==========================================  ==============================
secret 文件未配置/缺失/权限过宽/为空        build_event → None（采集停
                                            止）+ 节流 warning
remote_addr 缺失/不可解析                   build_event → None（无法算
                                            日轮换匿名哈希）
worker 未启动                               enqueue False
队列满（容量 _QUEUE_CAPACITY）              enqueue False（丢该条）
DB 连接失败 / 超时 / 写失败                 丢整批 + 节流 warning，worker
                                            线程存活继续服务
stop_worker / 进程退出                      队列残余丢弃
==========================================  ==============================

隐私不变量（§4.4，违反任一即发布停止条件）：

- 不保存完整 IP、完整 User-Agent、URL query、邀请码、分享 token、资源 ID；
- daily_visitor_hash = HMAC-SHA256(secret, "YYYY-MM-DD" + IPv4 /24 或
  IPv6 /64 网络前缀)：同日同前缀同哈希、跨日或跨前缀不同；**前缀本身
  不落库**（只在内存中拼进 HMAC 输入）；secret 只从
  ``SITE_STATS_HMAC_SECRET_FILE`` 指向的 0600 文件读取，缺失/权限错误时
  停止采集并告警，页面继续服务；**不复用 session secret（SECRET_KEY）**，
  无任何回退链；
- 疑似爬虫只是站长观测标签，不是安全封禁依据（UI 必须写"疑似爬虫"）；
- 队列只持有 build_event 输出的最小事件 dict；原始 IP/UA 绝不进入队列、
  日志或崩溃转储（本模块所有日志只含事件条数与异常类型名）。

bot 词表口径：仿 mywebpage 站长页（显式 UA 标记 + 已知 bot 名称），但规则、
词表与 fixture 归本仓所有，**严禁运行时 import /Users/solarise/mywebpage**。

风格对齐 registration_store：模块级函数 + 中文注释。模块 import 零副作用
（不起线程、不连库、不读 secret），满足镜像 import smoke 门禁。
"""

import hashlib
import hmac
import ipaddress
import logging
import os
import queue
import re
import secrets
import stat as _stat
import threading
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse, parse_qsl

import pg_store

_log = logging.getLogger("svs.site_stats")

# --------------------------------------------------------------------------- #
# 版本化常量
# --------------------------------------------------------------------------- #
#: bot 词表版本（修改 _BOT_UA_NEEDLES / _BOT_UA_GENERIC_MARKERS 必须提版）。
SITE_BOT_UA_RULESET_VERSION = "2026-09-03.v1"

#: 页面 allowlist（path → page_key）：**精确匹配**，不允许前缀/模糊命中未知
#: 路径（权威定义；0030 的 page_key CHECK 只约束形态）。集合口径 §4.4：
#: 明确排除 /api/*、静态资源、health、admin、登录后工作区（形如
#: /workspace/<id>、/project/<id>、/share/<token>、/demo/<slide> 的深路径
#: 天然无法精确命中）。
PAGE_ALLOWLIST = {
    "/": "home",
    "/demo": "demo",
    "/register": "register",
    "/login": "login",
}

#: visitor_kind 词表（与 0030 CHECK 一致）
VISITOR_KINDS = ("anonymous_human", "signed_in_human", "suspected_bot")

#: 去重桶宽度：10 分钟（§4.4）
DEDUP_BUCKET_SECONDS = 600
#: 原始事件保留天数（expires_at = occurred_at + 90 天）
RETENTION_DAYS = 90
#: D2 首发国家码恒量（不调第三方定位；将来离线库另做镜像/许可证 review）
COUNTRY_CODE_FALLBACK = "unknown"

# --------------------------------------------------------------------------- #
# 内部常量（下划线命名，测试可用）
# --------------------------------------------------------------------------- #
#: 有界队列容量（队列满丢新条，不阻塞页面响应）
_QUEUE_CAPACITY = 1000
#: 单批最大写入条数
_BATCH_SIZE = 100
#: 队列空闲时的轮询间隔（有事件到达即尽快刷批）
_FLUSH_INTERVAL_SECONDS = 2.0
#: stop_worker join 超时
_WORKER_JOIN_TIMEOUT = 5.0
#: 单条语句超时（毫秒）——统计写路径宁可丢批也不长等
_DB_STATEMENT_TIMEOUT_MS = 5000
#: 同类告警节流间隔（秒）
_WARN_INTERVAL_SECONDS = 60.0
#: secret 文件 env 名（唯一 secret 来源；绝不回退 SECRET_KEY/session secret）
SECRET_FILE_ENV = "SITE_STATS_HMAC_SECRET_FILE"
_SECRET_MAX_BYTES = 4096

#: 统计日界时区：Asia/Shanghai 无夏令时，固定 UTC+8 等价（避免依赖容器
#: tzdata）；与 spend_store 的业务周期时区口径一致（DB 存 UTC）。
_STATS_TIMEZONE = timezone(timedelta(hours=8))
#: SQL 侧同一偏移（(occurred_at AT TIME ZONE 'UTC' + INTERVAL '8 hours')::date
#: ——必须先剥成 naive UTC 再加偏移，避免 ::date 受会话时区影响）
_STATS_SQL_OFFSET_HOURS = 8

#: utm_source 清洗白名单：清理后仅允许短标签（≤32，字母数字下划线连字符），
#: 其余 UTM/query 一律丢弃
_UTM_SOURCE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,31}$")

#: 危险 query 键：出现即拒绝**整条事件**（可能含凭证或资源标识；§4.4
#: "不保存分享 token / 资源 ID"按最保守口径实现为"带这些键就不记"）
_QUERY_DENY_EXACT = frozenset({
    "id", "ids", "token", "tokens", "key", "keys", "apikey", "code",
    "secret", "password", "passwd", "session", "sid", "invite", "invitee",
    "slide", "slides", "slice", "project", "proj", "roi", "share", "auth",
    "signature", "sig", "credential", "credentials", "file", "filename",
})

# --------------------------------------------------------------------------- #
# bot 词表（仿 mywebpage 口径；规则归本仓所有，运行时不 import 外部仓库）。
# 有序：具体名称优先于泛化标记；needle 均为小写子串。
# --------------------------------------------------------------------------- #
_BOT_UA_NEEDLES = (
    ("googlebot", "Googlebot"),
    ("google-inspectiontool", "GoogleInspectionTool"),
    ("apis-google", "Google-API"),
    ("bingbot", "Bingbot"),
    ("adidxbot", "BingAdsAdIdxBot"),
    ("duckduckbot", "DuckDuckBot"),
    ("baiduspider", "Baiduspider"),
    ("yandexbot", "YandexBot"),
    ("yandeximages", "YandexImages"),
    ("sogou", "SogouSpider"),
    ("applebot", "Applebot"),
    ("petalbot", "PetalBot"),
    ("ahrefsbot", "AhrefsBot"),
    ("semrushbot", "SEMrushBot"),
    ("mj12bot", "MJ12bot"),
    ("dotbot", "DotBot"),
    ("facebookexternalhit", "FacebookExternalHit"),
    ("twitterbot", "Twitterbot"),
    ("linkedinbot", "LinkedInBot"),
    ("slackbot", "Slackbot"),
    ("discordbot", "Discordbot"),
    ("whatsapp", "WhatsApp"),
    ("telegrambot", "TelegramBot"),
    ("headless", "HeadlessBrowser"),
    ("phantomjs", "PhantomJS"),
    ("puppeteer", "Puppeteer"),
    ("playwright", "Playwright"),
    ("selenium", "Selenium"),
    ("lighthouse", "Lighthouse"),
    ("curl", "curl"),
    ("wget", "Wget"),
    ("python-requests", "python-requests"),
    ("python-urllib", "python-urllib"),
    ("httpx", "httpx"),
    ("aiohttp", "aiohttp"),
    ("go-http-client", "GoHTTPClient"),
    ("java/", "JavaHTTPClient"),
    ("okhttp", "OkHttp"),
    ("apache-httpclient", "ApacheHttpClient"),
    ("libwww-perl", "libwww-perl"),
    ("scrapy", "Scrapy"),
    ("httrack", "HTTrack"),
    ("axios", "axios"),
    ("node-fetch", "node-fetch"),
    ("undici", "undici"),
)
#: 泛化标记：未命中具体名称但带这些子串 → 疑似 bot（名称记 generic_bot）
_BOT_UA_GENERIC_MARKERS = ("bot", "crawler", "spider", "slurp", "crawl")

#: 本机/同站判定兜底主机名（PUBLIC_BASE_URL/SERVER_NAME 之外）
_LOCAL_HOSTS = frozenset({
    "localhost", "127.0.0.1", "0.0.0.0", "::1", "testserver",
})

#: 事件 dict 的固定键集合（跨代理契约；多余键一个都不能有）
_EVENT_KEYS = frozenset({
    "page_key", "occurred_at", "dedup_bucket", "referrer_domain",
    "utm_source", "country_code", "daily_visitor_hash", "visitor_kind",
    "bot_name",
})


# --------------------------------------------------------------------------- #
# 节流告警（日志只含条数/异常类型名，绝不含事件内容、IP、UA、query）
# --------------------------------------------------------------------------- #
_warn_state = {}
_warn_lock = threading.Lock()


def _throttled_warn(key, message):
    """同类告警每 _WARN_INTERVAL_SECONDS 至多一条（页面热路径不被日志刷屏）。"""
    now_mono = time.monotonic()
    with _warn_lock:
        last = _warn_state.get(key)
        if last is not None and (now_mono - last) < _WARN_INTERVAL_SECONDS:
            return
        _warn_state[key] = now_mono
    _log.warning("%s", message)


def _reset_warn_state():
    """测试辅助：清空节流状态（下一条告警必然发出）。"""
    with _warn_lock:
        _warn_state.clear()


# --------------------------------------------------------------------------- #
# HMAC secret 加载（唯一来源：SITE_STATS_HMAC_SECRET_FILE 0600 文件；
# 不复用 session secret，无回退链。缺失/权限错误 → 返回 None，调用方停止
# 采集，页面服务不受影响）
# --------------------------------------------------------------------------- #
def _load_secret():
    """读取并校验 secret 文件，返回 bytes 或 None。

    校验：常规文件、权限不含 group/other 位（0600 或更严）、非空、≤4 KiB。
    任何失败：节流 warning（只含异常类型名，不含路径内容）+ None。
    """
    path = (os.environ.get(SECRET_FILE_ENV) or "").strip()
    if not path:
        _throttled_warn(
            "secret_missing",
            "site_stats: %s 未配置，停止站点访问采集（页面服务不受影响）"
            % SECRET_FILE_ENV)
        return None
    try:
        st = os.stat(path)
        if not _stat.S_ISREG(st.st_mode):
            raise ValueError("not a regular file")
        if (st.st_mode & 0o077) != 0:
            raise ValueError("permissions too open: %o" % (st.st_mode & 0o777))
        with open(path, "rb") as fh:
            data = fh.read(_SECRET_MAX_BYTES + 1)
        if not data:
            raise ValueError("empty secret file")
        if len(data) > _SECRET_MAX_BYTES:
            raise ValueError("secret file too large")
    except Exception as exc:  # 缺失/权限/IO 一律降级为"停止采集"
        _throttled_warn(
            "secret_unreadable",
            "site_stats: 读取 %s 失败（%s），停止站点访问采集（页面服务不"
            "受影响）" % (SECRET_FILE_ENV, type(exc).__name__))
        return None
    return data


# --------------------------------------------------------------------------- #
# IP 前缀（IPv4 /24、IPv6 /64；前缀只在内存参与 HMAC，绝不落库）
# --------------------------------------------------------------------------- #
def _visitor_prefix(remote_addr):
    """把 remote_addr 归约到匿名前缀字符串；缺失/不可解析 → None（丢事件）。

    - IPv4 → a.b.c.0/24；
    - IPv6 → 前 64 位网络地址/64；
    - IPv4-mapped IPv6（::ffff:a.b.c.d）→ 按 IPv4 /24 处理（否则同机双栈
      访客会得到两个身份）。
    """
    raw = (remote_addr or "").strip()
    if not raw:
        return None
    try:
        ip = ipaddress.ip_address(raw)
    except ValueError:
        return None
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    plen = 24 if ip.version == 4 else 64
    net = ipaddress.ip_network("%s/%d" % (ip, plen), strict=False)
    return "%s/%d" % (net.network_address.compressed, plen)


def _daily_visitor_hash(secret, day_str, prefix):
    """HMAC-SHA256(secret, "YYYY-MM-DD" + 前缀) 的 hex（口径钉死：日期与前缀
    直接拼接）。日轮换：跨日同一访客产生不同哈希，**跨日不可识别同一人**，
    因此聚合指标不得命名为"独立用户数"。"""
    return hmac.new(
        secret, (day_str + prefix).encode("utf-8"), hashlib.sha256
    ).hexdigest()


# --------------------------------------------------------------------------- #
# referrer / query / utm 清洗
# --------------------------------------------------------------------------- #
def _self_hostnames():
    """视作"同站"的 hostname 集合：PUBLIC_BASE_URL + SERVER_NAME + 本机兜底。"""
    hosts = set(_LOCAL_HOSTS)
    raw = (os.environ.get("PUBLIC_BASE_URL") or "").strip()
    if raw:
        try:
            candidate = raw if "//" in raw else "https://" + raw
            host = urlparse(candidate).hostname
        except ValueError:
            host = None
        if host:
            hosts.add(host.lower().rstrip("."))
    server_name = (os.environ.get("SERVER_NAME") or "").strip().lower()
    if server_name:
        hosts.add(server_name.rstrip("."))
    return hosts


def _referrer_domain(referrer):
    """只取 hostname（小写）；空/解析失败/同站 → 'direct'（同站跳转不当外部
    来源，§2.6 mywebpage 口径）。协议、路径、query 一律丢弃。"""
    raw = (referrer or "").strip()
    if not raw:
        return "direct"
    try:
        host = urlparse(raw).hostname
    except ValueError:
        return "direct"
    if not host:
        return "direct"
    host = host.lower().rstrip(".")
    if host in _self_hostnames():
        return "direct"
    return host


def _parse_query(query_string):
    """query_string → [(name, value)]；None/空 → []。容忍开头 ?/& 与坏编码。"""
    raw = (query_string or "").strip().lstrip("?&")
    if not raw:
        return []
    try:
        return parse_qsl(raw, keep_blank_values=True)
    except ValueError:
        # 无法安全解析的 query 一律视为可疑（丢整条，而不是猜测清洗）
        return None


def _query_has_sensitive_key(query_string):
    """query 含疑似 token/凭证/资源 ID 键 → True（build_event 拒绝整条事件）。"""
    pairs = _parse_query(query_string)
    if pairs is None:
        return True
    for name, _value in pairs:
        n = (name or "").strip().lower()
        if not n:
            continue
        if n in _QUERY_DENY_EXACT:
            return True
        if "token" in n or "secret" in n or "password" in n or "credential" in n:
            return True
        if n.endswith(("_id", "_key", "_sig")) or n.startswith("id_"):
            return True
    return False


def _clean_utm_source(value):
    """utm_source 清洗：仅保留 ^[A-Za-z0-9][A-Za-z0-9_-]{0,31}$ 短标签；
    其余（超长/特殊字符/控制字符）→ None 丢弃。"""
    v = (value or "").strip()
    if not _UTM_SOURCE_RE.match(v):
        return None
    return v


# --------------------------------------------------------------------------- #
# bot 分类（词表版本化于 SITE_BOT_UA_RULESET_VERSION）
# --------------------------------------------------------------------------- #
def _classify_user_agent(user_agent):
    """命中疑似 bot → 规范 bot 名称；否则 None（人类）。只做观测标签，
    不做安全封禁依据（§3.4）。原始 UA 在调用处用后即弃，不落库。"""
    ua = (user_agent or "").strip().lower()
    if not ua:
        return None
    for needle, name in _BOT_UA_NEEDLES:
        if needle in ua:
            return name
    for marker in _BOT_UA_GENERIC_MARKERS:
        if marker in ua:
            return "generic_bot"
    return None


# --------------------------------------------------------------------------- #
# build_event（纯函数；跨代理契约）
# --------------------------------------------------------------------------- #
def build_event(*, path, query_string, referrer, remote_addr, user_agent,
                status_code, content_type, signed_in, now=None):
    """构造最小匿名事件；不符合口径返回 None（调用方静默丢弃）。

    拒绝口径：非 allowlist 精确路径 / 状态码非 2xx-3xx / Content-Type 非
    HTML / query 含 token 或资源 ID 键 / secret 不可用 / remote_addr 不可
    解析。HTTP method（仅 GET 采集）由 after_request 调用点过滤——本函数
    契约无 method 参数。

    返回 dict 键固定（与 0030 列一一对应，无任何多余键）：
    page_key, occurred_at, dedup_bucket, referrer_domain, utm_source,
    country_code, daily_visitor_hash, visitor_kind, bot_name。
    """
    # 1. 状态码：仅 2xx/3xx（要求真实 int，拒绝 "200" 之类的宽松转换）
    if not isinstance(status_code, int) or isinstance(status_code, bool):
        return None
    if status_code < 200 or status_code > 399:
        return None
    # 2. 仅 HTML 响应（容忍 "; charset=..." 后缀）
    ctype = (content_type or "").split(";", 1)[0].strip().lower()
    if ctype != "text/html":
        return None
    # 3. 路径必须精确命中 allowlist（拒绝 /demo/、//demo、/demo/x 等）
    page_key = PAGE_ALLOWLIST.get(path) if isinstance(path, str) else None
    if page_key is None:
        return None
    # 4. query：含 token/资源 ID 键 → 整条拒绝；utm_source 之外的键全部丢弃
    if _query_has_sensitive_key(query_string):
        return None
    utm_source = None
    for name, value in (_parse_query(query_string) or []):
        if (name or "").strip().lower() == "utm_source":
            utm_source = _clean_utm_source(value)
            break
    # 5. secret 不可用 → 停止采集（降级矩阵第一行）
    secret = _load_secret()
    if secret is None:
        return None
    # 6. 前缀不可解析 → 无法算日轮换哈希 → 丢事件
    prefix = _visitor_prefix(remote_addr)
    if prefix is None:
        return None
    # 7. 时间：occurred_at 存 UTC ISO；日界按 Asia/Shanghai（固定 UTC+8）
    moment = now if now is not None else datetime.now(timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    moment_utc = moment.astimezone(timezone.utc)
    day_str = moment_utc.astimezone(_STATS_TIMEZONE).strftime("%Y-%m-%d")
    # 8. 三分类：bot 优先（带会话的爬虫仍是 suspected_bot），其次登录态
    bot_name = _classify_user_agent(user_agent)
    if bot_name is not None:
        visitor_kind = "suspected_bot"
    elif signed_in:
        visitor_kind = "signed_in_human"
    else:
        visitor_kind = "anonymous_human"
    return {
        "page_key": page_key,
        "occurred_at": moment_utc.isoformat(),
        "dedup_bucket": int(moment_utc.timestamp() // DEDUP_BUCKET_SECONDS),
        "referrer_domain": _referrer_domain(referrer),
        "utm_source": utm_source,
        "country_code": COUNTRY_CODE_FALLBACK,
        "daily_visitor_hash": _daily_visitor_hash(secret, day_str, prefix),
        "visitor_kind": visitor_kind,
        "bot_name": bot_name,
    }


# --------------------------------------------------------------------------- #
# 后台 worker（有界队列 + 批量 INSERT ... ON CONFLICT DO NOTHING）
# --------------------------------------------------------------------------- #
_WORKER = None          # {"thread": Thread, "queue": Queue, "stop": Event}
_WORKER_LOCK = threading.Lock()


def start_worker():
    """启动后台批量写线程（幂等）。PostgreSQL 为唯一后端，不再读
    ``STORAGE_BACKEND``——缺键也必须起 worker（Wave 3 后 json/dual 不可达）。"""
    global _WORKER
    with _WORKER_LOCK:
        if _WORKER is not None and _WORKER["thread"].is_alive():
            return
        queue_obj = queue.Queue(maxsize=_QUEUE_CAPACITY)
        stop_event = threading.Event()
        thread = threading.Thread(
            target=_worker_loop, name="site-stats-worker", daemon=True,
            args=(queue_obj, stop_event))
        _WORKER = {"thread": thread, "queue": queue_obj, "stop": stop_event}
        thread.start()


def stop_worker():
    """停线程（幂等）；进程退出允许丢队列残余——不 drain、不等队列清空。"""
    global _WORKER
    with _WORKER_LOCK:
        worker = _WORKER
        _WORKER = None
    if worker is None:
        return
    worker["stop"].set()
    if worker["thread"].is_alive():
        worker["thread"].join(timeout=_WORKER_JOIN_TIMEOUT)


def enqueue_visit(event) -> bool:
    """最小事件入队；队列满 / worker 未启动 / 后端不可用 → False（丢该条）。
    永不抛异常——页面响应路径绝不因统计而等待或报错。"""
    try:
        if not isinstance(event, dict) or not _EVENT_KEYS.issubset(event):
            return False
        with _WORKER_LOCK:
            worker = _WORKER
        if worker is None or not worker["thread"].is_alive():
            return False
        worker["queue"].put_nowait(event)
        return True
    except Exception:
        # queue.Full 及任何意外：丢该条
        return False


def _worker_loop(queue_obj, stop_event):
    """批量消费：取到事件即把队列排空至多 _BATCH_SIZE 条后整批写；
    队列空闲则每隔 _FLUSH_INTERVAL_SECONDS 醒来检查 stop。"""
    while not stop_event.is_set():
        try:
            item = queue_obj.get(timeout=_FLUSH_INTERVAL_SECONDS)
        except queue.Empty:
            continue
        except Exception:  # pragma: no cover - 防御
            continue
        batch = [item] if isinstance(item, dict) else []
        while len(batch) < _BATCH_SIZE:
            try:
                nxt = queue_obj.get_nowait()
            except queue.Empty:
                break
            except Exception:  # pragma: no cover - 防御
                break
            if isinstance(nxt, dict):
                batch.append(nxt)
        if batch:
            _flush_batch(batch)
    # stop：允许丢队列残余（统计允许少计；§Batch D2 实现要求 7）


def _new_event_id():
    return "sve_" + secrets.token_hex(12)


def _flush_batch(events):
    """整批插入；任何 DB 故障（连接/超时/写失败）丢整批 + 节流 warning，
    绝不抛出。去重桶冲突由 UNIQUE + ON CONFLICT DO NOTHING 丢弃。"""
    if not events:
        return
    rows = []
    for ev in events:
        if not isinstance(ev, dict):
            continue  # 队列只应出现 build_event 形状；防御非 dict
        try:
            occurred = datetime.fromisoformat(ev.get("occurred_at"))
        except (TypeError, ValueError):
            continue  # 单条畸形：丢该条（不入批）
        rows.append((
            _new_event_id(),
            occurred,
            occurred + timedelta(days=RETENTION_DAYS),
            ev.get("dedup_bucket"),
            ev.get("page_key"),
            ev.get("referrer_domain"),
            ev.get("utm_source"),
            ev.get("country_code"),
            ev.get("daily_visitor_hash"),
            ev.get("visitor_kind"),
            ev.get("bot_name"),
        ))
    if not rows:
        return
    try:
        conn = pg_store.connect()
    except Exception as exc:
        _throttled_warn(
            "db_connect",
            "site_stats: 统计库连接失败，丢弃 %d 条站点访问事件（%s）"
            % (len(rows), type(exc).__name__))
        return
    try:
        with conn.transaction():
            with conn.cursor() as cur:
                # 宁可丢批也不长等：语句级超时（连接级配置不依赖 DBA 设置）。
                # SET 是 utility statement，不能绑定参数——整型常量安全内插
                cur.execute(
                    "SET LOCAL statement_timeout = %d"
                    % int(_DB_STATEMENT_TIMEOUT_MS))
                cur.executemany(
                    "INSERT INTO site_visit_events ("
                    "  event_id, occurred_at, expires_at, dedup_bucket,"
                    "  page_key, referrer_domain, utm_source, country_code,"
                    "  daily_visitor_hash, visitor_kind, bot_name) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
                    "ON CONFLICT (daily_visitor_hash, page_key, dedup_bucket)"
                    " DO NOTHING",
                    rows)
    except Exception as exc:
        # psycopg conn.transaction 已回滚；连接状态未知，直接弃用
        _throttled_warn(
            "db_write",
            "site_stats: 统计写入失败，丢弃 %d 条站点访问事件（%s）"
            % (len(rows), type(exc).__name__))
    finally:
        try:
            conn.close()
        except Exception:
            pass


# --------------------------------------------------------------------------- #
# dashboard_stats（owner-only 只读固定聚合；无写副作用）
# --------------------------------------------------------------------------- #
#: 聚合窗口（含今日）：today=1 天，d7=近 7 天，d30=近 30 天
_DASHBOARD_WINDOW_KEYS = ("today", "d7", "d30")
_DASHBOARD_DAYS = {"today": 1, "d7": 7, "d30": 30}
_DASHBOARD_DAYS_BACK = {"today": 0, "d7": 6, "d30": 29}


def _day_start_utc(moment_utc, days_back):
    """moment_utc 所在（本地日 - days_back）的本地零点，转回 UTC aware。"""
    local_date = moment_utc.astimezone(_STATS_TIMEZONE).date()
    day = local_date - timedelta(days=days_back)
    local_midnight = datetime(day.year, day.month, day.day,
                              tzinfo=_STATS_TIMEZONE)
    return local_midnight.astimezone(timezone.utc)


def _dict_row():
    import psycopg
    return psycopg.rows.dict_row


def _window_agg(start_utc, end_utc):
    """一个 [start, end) 窗口的三项聚合。unique_visitors 只数人类
    （visitor_kind <> 'suspected_bot'）的日去重哈希——爬虫不进入匿名访客
    近似数（§4.2/§7.2）；哈希按日轮换，跨日去重计数 ≈ 各日去重之和，
    跨日不可识别同一人（故该指标不是"独立用户数"）。"""
    conn = pg_store.connect()
    conn.row_factory = _dict_row()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT count(*) AS visits,"
                " count(DISTINCT daily_visitor_hash)"
                "   FILTER (WHERE visitor_kind <> 'suspected_bot')"
                "   AS unique_visitors,"
                " count(*) FILTER (WHERE visitor_kind = 'suspected_bot')"
                "   AS bots"
                " FROM site_visit_events"
                " WHERE occurred_at >= %s AND occurred_at < %s",
                (start_utc, end_utc))
            row = cur.fetchone()
        return {
            "visits": int(row["visits"] or 0),
            "unique_visitors": int(row["unique_visitors"] or 0),
            "bots": int(row["bots"] or 0),
        }
    finally:
        conn.close()


def _daily_series(today_start, today_end, days):
    """30 天逐日序列（缺日补零）。日界：本地（UTC+8）日，SQL 侧先把
    timestamptz 剥成 naive UTC 再加固定偏移，避免 ::date 受会话时区影响。"""
    sql = (
        "SELECT (occurred_at AT TIME ZONE 'UTC'"
        "         + INTERVAL '%d hours')::date AS day,"
        " count(*) AS visits,"
        " count(DISTINCT daily_visitor_hash)"
        "   FILTER (WHERE visitor_kind <> 'suspected_bot')"
        "   AS unique_visitors,"
        " count(*) FILTER (WHERE visitor_kind = 'suspected_bot')"
        "   AS bots"
        " FROM site_visit_events"
        " WHERE occurred_at >= %%s AND occurred_at < %%s"
        " GROUP BY 1" % _STATS_SQL_OFFSET_HOURS)
    grouped = {}
    conn = pg_store.connect()
    conn.row_factory = _dict_row()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, (today_start, today_end))
            for row in cur.fetchall():
                grouped[row["day"]] = row
    finally:
        conn.close()
    series = []
    for i in range(days - 1, -1, -1):
        day = (today_start.astimezone(_STATS_TIMEZONE).date()
               - timedelta(days=i))
        row = grouped.get(day)
        series.append({
            "date": day.isoformat(),
            "visits": int(row["visits"] or 0) if row else 0,
            "unique_visitors": int(row["unique_visitors"] or 0) if row else 0,
            "bots": int(row["bots"] or 0) if row else 0,
        })
    return series


def _top_list(today_start, today_end, column, output_key, extra_where,
              limit=10):
    """Top-N（top_referrers 排除 direct/空——同站跳转不是外部来源；
    top_countries 排除 unknown——UI 据此隐藏国家块）。column/extra_where
    均为模块内字面常量，不经外部输入。"""
    sql = (
        "SELECT %s AS value, count(*) AS visits"
        " FROM site_visit_events"
        " WHERE occurred_at >= %%s AND occurred_at < %%s %s"
        " GROUP BY 1 ORDER BY count(*) DESC, 1 ASC LIMIT %%s"
        % (column, extra_where))
    conn = pg_store.connect()
    conn.row_factory = _dict_row()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, (today_start, today_end, limit))
            rows = cur.fetchall()
    finally:
        conn.close()
    return [{output_key: r["value"], "visits": int(r["visits"])}
            for r in rows]


def _recent(limit=20):
    conn = pg_store.connect()
    conn.row_factory = _dict_row()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT occurred_at, page_key, referrer_domain,"
                " country_code, visitor_kind, bot_name"
                " FROM site_visit_events"
                " ORDER BY occurred_at DESC, event_id DESC LIMIT %s",
                (limit,))
            rows = cur.fetchall()
    finally:
        conn.close()
    return [{
        "occurred_at": r["occurred_at"].isoformat(),
        "page_key": r["page_key"],
        "referrer_domain": r["referrer_domain"],
        "country_code": r["country_code"],
        "visitor_kind": r["visitor_kind"],
        "bot_name": r["bot_name"],
    } for r in rows]


def _visitor_kind_counts(start_utc, end_utc):
    conn = pg_store.connect()
    conn.row_factory = _dict_row()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT visitor_kind, count(*) AS n FROM site_visit_events"
                " WHERE occurred_at >= %s AND occurred_at < %s GROUP BY 1",
                (start_utc, end_utc))
            grouped = {r["visitor_kind"]: int(r["n"] or 0)
                       for r in cur.fetchall()}
    finally:
        conn.close()
    return {kind: grouped.get(kind, 0) for kind in VISITOR_KINDS}


def dashboard_stats(*, now=None):
    """owner-only 只读固定聚合（契约形状，一字不差）。只读 site_visit_events，
    不联任何业务表、不创建事件、不调清理。

    geo_configured 恒 False（D2 country_code 恒 unknown，未配置离线定位库）。
    """
    moment = now if now is not None else datetime.now(timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    moment_utc = moment.astimezone(timezone.utc)
    today_start = _day_start_utc(moment_utc, 0)
    today_end = today_start + timedelta(days=1)
    d7_start = _day_start_utc(moment_utc, _DASHBOARD_DAYS_BACK["d7"])
    d30_start = _day_start_utc(moment_utc, _DASHBOARD_DAYS_BACK["d30"])
    windows = {}
    for key in _DASHBOARD_WINDOW_KEYS:
        start = d30_start if key == "d30" else (
            d7_start if key == "d7" else today_start)
        windows[key] = _window_agg(start, today_end)
    return {
        "generated_at": moment_utc.isoformat(),
        "today": windows["today"],
        "d7": windows["d7"],
        "d30": windows["d30"],
        "daily": _daily_series(today_start, today_end, 30),
        "top_referrers": _top_list(
            d30_start, today_end, "referrer_domain", "domain",
            "AND referrer_domain IS NOT NULL"
            " AND referrer_domain <> 'direct'"),
        "top_pages": _top_list(
            d30_start, today_end, "page_key", "page_key", ""),
        "top_countries": _top_list(
            d30_start, today_end, "country_code", "country_code",
            "AND country_code <> 'unknown'"),
        "recent": _recent(),
        "visitor_kinds": _visitor_kind_counts(d30_start, today_end),
        "geo_configured": False,
    }


def purge_expired(*, now=None):
    """显式 retention 清理：只删 expires_at 到期的 site_visit_events 行，
    返回删除行数。不碰任何业务表。"""
    moment = now if now is not None else datetime.now(timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    moment_utc = moment.astimezone(timezone.utc)
    conn = pg_store.connect()
    try:
        with conn.transaction():
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM site_visit_events WHERE expires_at <= %s",
                    (moment_utc,))
                deleted = cur.rowcount
        return int(deleted or 0)
    finally:
        conn.close()
