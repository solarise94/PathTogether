# -*- coding: utf-8 -*-
"""用户来源归因存储层（admin-billing 方案 §11，PR4）。

PG-only：全部公共入口经 ``platform_features.require_pg_backend("acquisition")``
fail-closed（json/dual 稳定 ``pg_backend_required``）。**例外**是纯计算辅助
（slug/UTM/referrer/IP 前缀 hash 清理函数）——它们不接库，供路由层在任意
后端复用（/r/ 在 json 后端也要能做安全 302 与 cookie 清理，§16.2）。

内容：
  - 触点写入 ``record_visit``：每次 /r/<source_code> 跳转一个**不可变事件行**
    （不按 visitor upsert，不折叠 first/last_seen，§11.2 行粒度红线）；
  - 注册归因 ``insert_user_acquisition``：在 ``registration_store.redeem_invite``
    的**同一事务 cursor** 内调用（兑换 + 建号 + invite 消费 + 归因原子）；
    优先级严格按 §11.2：邀请码显式 campaign > 有效 pt_acq 触点（未过期，按
    touched_at/acquisition_id 稳定选 first/last）> sanitized referrer/UTM >
    direct/unknown；``attribution_method`` 记录走了哪条路径；
  - 匿名触点 90 天清理 ``cleanup_expired_visits``（只删未被 user_acquisition
    引用的过期行；已归因行由 FK 兜底永不删）；
  - admin 汇总：campaign/source → 访问 → 注册 → 首次 AI 漏斗
    （首次 AI = ai_usage_events 每用户最早 occurred_at）与用户来源明细分页。

隐私（§11.3 / §9 红线）：
  - 不保存完整 IP：只存 IP 前缀（IPv4 /24、IPv6 /48）规范化文本 + 盐的
    HMAC-SHA-256。盐来自 env ``ACQ_IP_SALT``；**未配置（空串）时一律存空串
    = 不采集 IP hash**（宁可少数据也不落可关联明文前缀）；盐可轮换（只影响
    新行，旧行 hash 自然失效）；
  - visitor_id（高熵随机明文只在 cookie，本模块只见 hash）：HMAC-SHA-256，
    盐链 ACQ_VISITOR_HASH_SALT → AUTH_SUBJECT_HASH_SALT → SECRET_KEY →
    固定域常量（与 registration_store._invite_hash_salt 同风格）；
  - referrer 只保留 hostname（query/路径/端口剥除，限长）；
  - UTM 每字段限长（128）并清理控制字符；
  - admin 导出无完整 IP、无 referrer query、visitor 只给 hash 前缀（8 hex）。
"""

import hashlib
import hmac
import ipaddress
import logging
import os
import re
import secrets
from urllib.parse import urlparse

import psycopg

import pg_store
import platform_features

_log = logging.getLogger("svs.acquisition")

#: source/campaign slug：只允许 [a-z0-9_-]，长度 1..64，首字符须为字母/数字
#: （§11.1「只允许 [a-z0-9_-] 和长度上限」的保守收紧：拒绝 -abc/_abc 形态，
#: 与 billing_store 的 provider/model slug 首字符口径一致）
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
#: UTM 每字段最大长度（§11.3「UTM 每字段限制长度」）
MAX_UTM_LEN = 128
#: referrer hostname 最大长度
MAX_REFERRER_LEN = 128
#: 匿名触点保留期（天）：可 env ACQ_VISIT_TTL_DAYS 覆盖（§11.3 默认 90）
DEFAULT_VISIT_TTL_DAYS = 90
#: /r/ landing target 固定 allowlist（§11.1 第 5 条；路由层与本层双重校验）
LANDING_ALLOWLIST = ("/demo", "/register", "/")
#: visitor_id 明文形态（secrets.token_urlsafe(24)）：[A-Za-z0-9_-]{32}
VISITOR_ID_RE = re.compile(r"^[A-Za-z0-9_-]{16,64}$")

#: 归因路径稳定标识（user_acquisition.attribution_method 取值；导出白名单）
ATTRIBUTION_METHODS = (
    "invite_campaign",  # 邀请码显式 campaign（最高优先级）
    "invite_source",    # 邀请码显式 source_code（无 campaign）
    "visit",            # 有效 pt_acq first-party 触点
    "referrer_utm",     # sanitized referrer/UTM（注册请求携带）
    "direct",           # 无任何来源信号
)


def _connect():
    """建连接并设 dict_row（本模块所有查询按列名访问）。"""
    conn = pg_store.connect()
    conn.row_factory = psycopg.rows.dict_row
    return conn


def _visit_ttl_days() -> int:
    """匿名触点保留天数：ACQ_VISIT_TTL_DAYS（正整数；非法回退默认 90）。"""
    raw = (os.environ.get("ACQ_VISIT_TTL_DAYS") or "").strip()
    try:
        val = int(raw)
    except ValueError:
        return DEFAULT_VISIT_TTL_DAYS
    return val if val > 0 else DEFAULT_VISIT_TTL_DAYS


def visit_ttl_seconds() -> int:
    return _visit_ttl_days() * 86400


# --------------------------------------------------------------------------- #
# 纯计算辅助（不接库；任意后端可用）
# --------------------------------------------------------------------------- #
def valid_slug(value) -> bool:
    """source/campaign slug 校验：首字符 [a-z0-9]，整体 [a-z0-9_-]{1,64}。"""
    return isinstance(value, str) and bool(SLUG_RE.match(value))


def sanitize_text(value, max_len=MAX_UTM_LEN) -> str:
    """UTM 类自由文本清理：去控制字符（<0x20 及 DEL）→ strip → 截断。"""
    if not isinstance(value, str):
        return ""
    cleaned = "".join(
        ch for ch in value if ord(ch) >= 0x20 and ord(ch) != 0x7f)
    return cleaned.strip()[:max_len]


def sanitize_referrer_domain(value) -> str:
    """referrer 只保留 hostname：去 query/路径/端口/fragment，小写，截断。

    解析失败（裸字符串等）按无域名处理（不落可能携带 query 的原文）。
    """
    if not isinstance(value, str) or not value.strip():
        return ""
    try:
        host = urlparse(value.strip()).hostname
    except ValueError:
        return ""
    if not host:
        return ""
    return host.strip().lower()[:MAX_REFERRER_LEN]


def sanitize_landing_path(value) -> str:
    """landing target：固定 allowlist 之外一律回空（路由层再回缺省 /register）。"""
    if isinstance(value, str) and value in LANDING_ALLOWLIST:
        return value
    return ""


def ip_salt() -> str:
    """IP 前缀 hash 盐：env ACQ_IP_SALT；**空 = 不采集 IP hash**（§11.3）。

    独立盐（不复用登录桶盐）：acquisition hash 只作反滥用/粗粒度统计，轮换
    不影响登录防爆破主体；缺省不配置时宁可少数据也不落可关联前缀。
    """
    return (os.environ.get("ACQ_IP_SALT") or "").strip()


def ip_prefix_hash(ip) -> str:
    """IP 前缀 hash：IPv4 取 /24、IPv6 取 /48 的规范化网络地址文本 + 盐
    HMAC-SHA-256。盐未配置 → 返回空串（调用方存 ""，即不采集）。

    注意与登录防爆破（app.py _ip_prefix）口径不同：那边 v6 取 /64（更细的
    暴力维度），本模块按归因任务约定取 /48（更粗、更不易重识别）。
    """
    salt = ip_salt()
    if not salt:
        return ""
    raw = (ip or "").strip()
    if not raw:
        return ""
    try:
        addr = ipaddress.ip_address(raw)
    except ValueError:
        return ""
    bits = 24 if addr.version == 4 else 48
    prefix = str(ipaddress.ip_network(
        "%s/%d" % (addr, bits), strict=False).network_address)
    return hmac.new(("acqip:" + salt).encode("utf-8"),
                    prefix.encode("utf-8"), hashlib.sha256).hexdigest()


def _hash_salt() -> str:
    """visitor_id hash 盐链（域分离；与 registration_store 同风格）。"""
    for name in ("ACQ_VISITOR_HASH_SALT", "AUTH_SUBJECT_HASH_SALT",
                 "SECRET_KEY"):
        v = (os.environ.get(name) or "").strip()
        if v:
            return v
    return "pt-acquisition-v1"


def visitor_id_hash(visitor_id) -> str:
    """visitor_id 明文 → 带域分离盐的 HMAC-SHA-256（库内只存 hash）。"""
    return hmac.new(("acqvis:" + _hash_salt()).encode("utf-8"),
                    str(visitor_id or "").encode("utf-8"),
                    hashlib.sha256).hexdigest()


def new_visitor_id() -> str:
    """高熵匿名 visitor_id（secrets.token_urlsafe(24)，192-bit）。"""
    return secrets.token_urlsafe(24)


def valid_visitor_id(value) -> bool:
    return isinstance(value, str) and bool(VISITOR_ID_RE.match(value))


# --------------------------------------------------------------------------- #
# campaign 字典
# --------------------------------------------------------------------------- #
_CAMPAIGN_SEL = ("campaign_id, source_code, name, status, "
                 "extract(epoch from created_at)::float8 AS created_at, "
                 "created_by")


def _campaign_out(row) -> dict:
    return dict(row) if row is not None else None


def _get_campaign(cur, campaign_id, active_only=False):
    """cursor 内取 campaign 行；不存在（或已 paused/archived 且要求 active）
    返回 None。"""
    sql = "SELECT " + _CAMPAIGN_SEL + " FROM acquisition_campaigns " \
          "WHERE campaign_id=%s"
    if active_only:
        sql += " AND status='active'"
    cur.execute(sql + " LIMIT 1", (campaign_id,))
    return cur.fetchone()


def create_campaign(campaign_id, source_code, name, created_by=None):
    """种入 campaign（幂等冲突抛错由调用方处理）。

    本批无 owner CRUD API（§13 PR4 范围外；§10.3 的创建/撤销按钮属 PR5）：
    行由 owner SQL 或测试/种子脚本经本函数写入。
    """
    platform_features.require_pg_backend("acquisition")
    if not valid_slug(campaign_id):
        raise ValueError("campaign_id 需匹配 [a-z0-9][a-z0-9_-]{0,63}")
    if not valid_slug(source_code):
        raise ValueError("source_code 需匹配 [a-z0-9][a-z0-9_-]{0,63}")
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                cur.execute(
                    "INSERT INTO acquisition_campaigns "
                    "(campaign_id, source_code, name, status, created_by) "
                    "VALUES (%s,%s,%s,'active',%s) RETURNING " + _CAMPAIGN_SEL,
                    (campaign_id, source_code,
                     sanitize_text(name, 128) or campaign_id,
                     created_by or None))
                row = cur.fetchone()
        return _campaign_out(row)
    finally:
        conn.close()


def get_campaign(campaign_id):
    """按 campaign_id 取行；不存在返回 None（owner 校验用）。"""
    platform_features.require_pg_backend("acquisition")
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                row = _get_campaign(cur, campaign_id)
        return _campaign_out(row)
    finally:
        conn.close()


def list_campaigns(limit=200):
    """campaign 列表（owner 视图；无敏感字段）。"""
    platform_features.require_pg_backend("acquisition")
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                cur.execute("SELECT " + _CAMPAIGN_SEL +
                            " FROM acquisition_campaigns "
                            "ORDER BY created_at DESC, campaign_id LIMIT %s",
                            (max(1, min(int(limit), 1000)),))
                rows = cur.fetchall()
        return [_campaign_out(r) for r in rows]
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# 触点写入（/r/<source_code>）
# --------------------------------------------------------------------------- #
def record_visit(*, visitor_id, source_code, campaign_id=None,
                 referrer_domain="", landing_path="", utm_source="",
                 utm_medium="", utm_campaign="", ip="", now=None):
    """落一个不可变触点事件行。返回行 dict（不含 visitor_id 明文）。

    - source_code 非法 slug → ValueError（路由层已校验，防御层）；
    - campaign_id：未知 / 非 active 的 slug 一律落 NULL，**不报错**（§11.2
      行为约定：未登记 campaign 不阻断跳转记录，slug 本身保留在 utm_campaign）；
    - referrer/UTM/IP 全部经清理函数（hostname-only、限长、控制字符、
      前缀 hash 或空串）；
    - landing_path 只接受 allowlist（/demo、/register、/），否则空串。
    """
    platform_features.require_pg_backend("acquisition")
    if not valid_visitor_id(visitor_id):
        raise ValueError("visitor_id 非法")
    if not valid_slug(source_code):
        raise ValueError("source_code 需匹配 [a-z0-9][a-z0-9_-]{0,63}")
    vhash = visitor_id_hash(visitor_id)
    landing = sanitize_landing_path(landing_path)
    referrer = sanitize_referrer_domain(referrer_domain)
    utm_s = sanitize_text(utm_source)
    utm_m = sanitize_text(utm_medium)
    utm_c = sanitize_text(utm_campaign)
    ip_h = ip_prefix_hash(ip)
    campaign = None
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                if campaign_id and valid_slug(campaign_id):
                    # 未知/非 active → NULL 不报错（slug 保留在 utm_campaign）
                    row = _get_campaign(cur, campaign_id, active_only=True)
                    campaign = row["campaign_id"] if row else None
                touched_at = now if now is not None else _now_epoch(cur)
                cur.execute(
                    "INSERT INTO acquisition_visits "
                    "(acquisition_id, visitor_id_hash, source_code, "
                    " campaign_id, referrer_domain, landing_path, utm_source, "
                    " utm_medium, utm_campaign, ip_prefix_hash, touched_at, "
                    " expires_at) "
                    "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s, "
                    " to_timestamp(%s), to_timestamp(%s) + (%s * interval "
                    "'1 second')) "
                    "RETURNING acquisition_id, source_code, campaign_id, "
                    " referrer_domain, landing_path, utm_source, utm_medium, "
                    " utm_campaign, ip_prefix_hash, "
                    " extract(epoch from touched_at)::float8 AS touched_at, "
                    " extract(epoch from expires_at)::float8 AS expires_at",
                    ("acq_" + secrets.token_urlsafe(12), vhash, source_code,
                     campaign, referrer, landing, utm_s, utm_m, utm_c, ip_h,
                     float(touched_at), float(touched_at),
                     visit_ttl_seconds()))
                row = cur.fetchone()
        return dict(row)
    finally:
        conn.close()


def _now_epoch(cur):
    """取数据库 now()（epoch float；触点时间以库时钟为准）。"""
    cur.execute("SELECT extract(epoch from now())::float8 AS t")
    return float(cur.fetchone()["t"])


# --------------------------------------------------------------------------- #
# 注册归因（redeem_invite 同一事务内调用；cursor 注入，不自己开连接）
# --------------------------------------------------------------------------- #
def insert_user_acquisition(cur, *, user_id, invite_id=None,
                            invite_campaign_id=None, invite_source_code="",
                            acq=None):
    """在调用方事务 cursor 内解析优先级并写 user_acquisition（不提交）。

    必须与「兑换 + users 插入 + invite 消费」同事务（§11.2 原子性）：本函数
    抛错时调用方整体回滚（用户也不创建）。**绝不**事后按 IP/login ID/邮箱
    模糊匹配——visitor 联结只经 pt_acq cookie 的 visitor_id hash。

    优先级（§11.2）：
      1. 邀请码显式 campaign（invite_campaign；行缺失时退 2 中的 invite_source）
         — 邀请显式 source_code（无 campaign）同属邀请级归因（invite_source）；
      2. 有效 pt_acq 触点：visitor_id hash 命中且 expires_at > now() 的行，
         按 (touched_at, acquisition_id) 稳定排序取 first/last（跨
         source/campaign 不折叠）；source/campaign 记 **last touch**（转化
         触点），first/last acquisition_id 并存保留完整路径；
      3. sanitized referrer/UTM（注册请求携带）：utm_campaign 命中已知
         active campaign > utm_source（合法 slug）> referrer hostname；
      4. direct/unknown。

    ``acq`` dict（路由层从已验签 cookie 与请求头组装，值已 sanitize）：
    ``{visitor_id, utm_source, utm_medium, utm_campaign, referrer_domain}``。
    返回写入行 dict（attribution_method 标明路径）。
    """
    acq = acq or {}
    campaign_id = None
    source_code = "unknown"
    method = "direct"
    invite_source_code = sanitize_text(invite_source_code, 64)

    # 1. 邀请码显式 campaign / source（§11.2 最高优先级）
    if invite_campaign_id and valid_slug(invite_campaign_id):
        row = _get_campaign(cur, invite_campaign_id, active_only=False)
        if row is not None:
            campaign_id = row["campaign_id"]
            source_code = row["source_code"] or invite_source_code \
                or "invite"
            method = "invite_campaign"
    if method == "direct" and invite_source_code and \
            valid_slug(invite_source_code):
        source_code = invite_source_code
        method = "invite_source"

    # 2. 有效 pt_acq first-party 触点（first/last 双 id；last 记 source）
    visitor_id = acq.get("visitor_id")
    first_visit = last_visit = None
    if valid_visitor_id(visitor_id):
        vhash = visitor_id_hash(visitor_id)
        cur.execute(
            "SELECT acquisition_id, source_code, campaign_id FROM "
            "acquisition_visits WHERE visitor_id_hash=%s AND expires_at>now() "
            "ORDER BY touched_at ASC, acquisition_id ASC LIMIT 1", (vhash,))
        first_visit = cur.fetchone()
        if first_visit is not None:
            cur.execute(
                "SELECT acquisition_id, source_code, campaign_id FROM "
                "acquisition_visits WHERE visitor_id_hash=%s AND "
                "expires_at>now() ORDER BY touched_at DESC, acquisition_id "
                "DESC LIMIT 1", (vhash,))
            last_visit = cur.fetchone()

    if method == "direct" and last_visit is not None:
        source_code = last_visit["source_code"] or "unknown"
        campaign_id = last_visit["campaign_id"]
        method = "visit"

    # 3. sanitized referrer/UTM（注册请求携带；不落完整 referrer，只可能归到
    #    campaign/source slug 或 hostname）
    if method == "direct":
        uc = sanitize_text(acq.get("utm_campaign"), 64)
        if uc and valid_slug(uc):
            row = _get_campaign(cur, uc, active_only=True)
            if row is not None:
                campaign_id = row["campaign_id"]
                source_code = row["source_code"] or uc
                method = "referrer_utm"
        if method == "direct":
            us = sanitize_text(acq.get("utm_source"), 64)
            if us and valid_slug(us):
                source_code = us
                method = "referrer_utm"
        if method == "direct":
            rd = sanitize_referrer_domain(acq.get("referrer_domain"))
            if rd:
                source_code = rd
                method = "referrer_utm"

    cur.execute(
        "INSERT INTO user_acquisition "
        "(user_id, first_acquisition_id, last_acquisition_id, invite_id, "
        " source_code, campaign_id, attributed_at, attribution_method) "
        "VALUES (%s,%s,%s,%s,%s,%s, now(), %s) "
        "RETURNING user_id, first_acquisition_id, last_acquisition_id, "
        " invite_id, source_code, campaign_id, "
        " extract(epoch from attributed_at)::float8 AS attributed_at, "
        " attribution_method",
        (user_id,
         first_visit["acquisition_id"] if first_visit else None,
         last_visit["acquisition_id"] if last_visit else None,
         invite_id or None, source_code, campaign_id, method))
    return dict(cur.fetchone())


def user_acquisition_by_ids(user_ids):
    """一批用户的归因摘要 ``{user_id: {source_code, campaign_id,
    attribution_method}}``（admin v1 users 行填充；无行用户不在 dict）。"""
    platform_features.require_pg_backend("acquisition")
    ids = [str(u) for u in (user_ids or []) if u]
    if not ids:
        return {}
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                cur.execute(
                    "SELECT user_id, source_code, campaign_id, "
                    "attribution_method FROM user_acquisition "
                    "WHERE user_id = ANY(%s)", (ids,))
                rows = [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()
    return {r["user_id"]: {"source_code": r["source_code"],
                           "campaign_id": r["campaign_id"],
                           "attribution_method": r["attribution_method"]}
            for r in rows}


# --------------------------------------------------------------------------- #
# 匿名触点 90 天清理
# --------------------------------------------------------------------------- #
def cleanup_expired_visits() -> int:
    """删除过期且未被 user_acquisition 引用的触点行，返回删除数。

    已归因行（first/last_acquisition_id 引用）由 WHERE NOT EXISTS 排除，且
    FK 兜底——用户归因长期保留 campaign/source，被引用触点不随匿名清理消失。
    """
    platform_features.require_pg_backend("acquisition")
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                cur.execute(
                    "DELETE FROM acquisition_visits v WHERE v.expires_at<now() "
                    "AND NOT EXISTS ("
                    " SELECT 1 FROM user_acquisition ua WHERE "
                    " ua.first_acquisition_id=v.acquisition_id OR "
                    " ua.last_acquisition_id=v.acquisition_id)")
                return int(cur.rowcount or 0)
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# admin 汇总（§10.3：campaign → 访问 → 注册 → 首次 AI 漏斗；§9 脱敏红线）
# --------------------------------------------------------------------------- #
def admin_funnel_summary():
    """每 (source_code, campaign_id) 的漏斗行列表。

    - ``visits`` / ``visitors``：acquisition_visits 计数 / 去重 visitor hash 数；
    - ``registrations``：user_acquisition 归因到该组的用户数；
    - ``first_ai_count``：其中至少有一条 ai_usage_events 的用户数（首次 AI =
      每用户最早 occurred_at 是否存在，不重复计数）；
    - campaign_name 来自字典表（无 campaign 行时 NULL）。
    FULL OUTER 语义在 Python 侧合并：只有注册无触点（invite/referrer 归因）
    与只有触点无注册的组都各自成行。
    """
    platform_features.require_pg_backend("acquisition")
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                cur.execute(
                    "SELECT source_code, campaign_id, count(*)::int AS visits, "
                    "count(DISTINCT visitor_id_hash)::int AS visitors "
                    "FROM acquisition_visits GROUP BY source_code, campaign_id")
                visit_rows = [dict(r) for r in cur.fetchall()]
                cur.execute(
                    "SELECT ua.source_code, ua.campaign_id, "
                    "count(*)::int AS registrations, "
                    "count(f.user_id)::int AS first_ai_count "
                    "FROM user_acquisition ua LEFT JOIN ("
                    " SELECT user_id, min(occurred_at) AS first_at "
                    " FROM ai_usage_events WHERE user_id IS NOT NULL "
                    " GROUP BY user_id) f ON f.user_id=ua.user_id "
                    "GROUP BY ua.source_code, ua.campaign_id")
                user_rows = [dict(r) for r in cur.fetchall()]
                cur.execute("SELECT campaign_id, source_code, name, status "
                            "FROM acquisition_campaigns")
                campaigns = {r["campaign_id"]: dict(r)
                             for r in cur.fetchall()}
    finally:
        conn.close()
    merged = {}
    for r in visit_rows:
        key = (r["source_code"], r["campaign_id"])
        merged.setdefault(key, {"visits": 0, "visitors": 0,
                                "registrations": 0, "first_ai_count": 0})
        merged[key]["visits"] = int(r["visits"])
        merged[key]["visitors"] = int(r["visitors"])
    for r in user_rows:
        key = (r["source_code"], r["campaign_id"])
        merged.setdefault(key, {"visits": 0, "visitors": 0,
                                "registrations": 0, "first_ai_count": 0})
        merged[key]["registrations"] = int(r["registrations"])
        merged[key]["first_ai_count"] = int(r["first_ai_count"])
    items = []
    for (source, campaign), v in merged.items():
        row = campaigns.get(campaign) or {}
        items.append({
            "source_code": source,
            "campaign_id": campaign,
            "campaign_name": row.get("name"),
            "campaign_status": row.get("status"),
            "visits": v["visits"],
            "visitors": v["visitors"],
            "registrations": v["registrations"],
            "first_ai_count": v["first_ai_count"],
        })
    items.sort(key=lambda i: (-(i["visits"] + i["registrations"]),
                              i["source_code"], i["campaign_id"] or ""))
    totals = {
        "visits": sum(i["visits"] for i in items),
        "registrations": sum(i["registrations"] for i in items),
        "first_ai_count": sum(i["first_ai_count"] for i in items),
    }
    return {"items": items, "totals": totals,
            "campaigns": [campaigns[k] for k in sorted(campaigns)]}


def admin_user_acquisition_page(*, cursor=None, limit=50):
    """用户来源明细分页（first/last touch 分列；§9 脱敏红线）。

    keyset 游标：(attributed_at epoch, user_id) 降序。导出红线：无完整 IP
    （触点行本就只有前缀 hash，不导出）、无 referrer query（只 hostname）、
    visitor 只给 hash 前缀 8 hex（够 owner 肉眼对齐，不足以复原/碰撞查询）。
    """
    platform_features.require_pg_backend("acquisition")
    limit = max(1, min(int(limit or 50), 200))
    where, params = "", []
    if cursor is not None:
        try:
            attributed, user_id = cursor
            attributed = float(attributed)
            user_id = str(user_id)
        except (TypeError, ValueError) as exc:
            raise ValueError("cursor 非法") from exc
        where = (" WHERE (extract(epoch from ua.attributed_at) < %s OR "
                 "(extract(epoch from ua.attributed_at) = %s "
                 " AND ua.user_id < %s))")
        params = [attributed, attributed, user_id]
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                cur.execute(
                    "SELECT ua.user_id, ua.source_code, ua.campaign_id, "
                    "ua.invite_id, ua.attribution_method, "
                    "extract(epoch from ua.attributed_at)::float8 "
                    " AS attributed_at, "
                    "u.display_name, u.login_id, u.role, "
                    "f.acquisition_id AS first_id, f.source_code "
                    " AS first_source, f.campaign_id AS first_campaign, "
                    "extract(epoch from f.touched_at)::float8 "
                    " AS first_touched_at, f.referrer_domain "
                    " AS first_referrer_domain, f.landing_path "
                    " AS first_landing_path, "
                    "left(f.visitor_id_hash, 8) AS first_visitor_prefix, "
                    "l.acquisition_id AS last_id, l.source_code AS last_source, "
                    "l.campaign_id AS last_campaign, "
                    "extract(epoch from l.touched_at)::float8 "
                    " AS last_touched_at, l.referrer_domain "
                    " AS last_referrer_domain, l.landing_path "
                    " AS last_landing_path, "
                    "left(l.visitor_id_hash, 8) AS last_visitor_prefix "
                    "FROM user_acquisition ua "
                    "JOIN users u ON u.user_id = ua.user_id "
                    "LEFT JOIN acquisition_visits f "
                    " ON f.acquisition_id = ua.first_acquisition_id "
                    "LEFT JOIN acquisition_visits l "
                    " ON l.acquisition_id = ua.last_acquisition_id"
                    + where +
                    " ORDER BY ua.attributed_at DESC, ua.user_id DESC "
                    "LIMIT %s", params + [limit + 1])
                rows = [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()
    has_more = len(rows) > limit
    rows = rows[:limit]

    def _touch(row, p):
        if row.get(p + "_id") is None:
            return None
        return {
            "acquisition_id": row[p + "_id"],
            "source_code": row[p + "_source"],
            "campaign_id": row[p + "_campaign"],
            "touched_at": row[p + "_touched_at"],
            "referrer_domain": row[p + "_referrer_domain"],
            "landing_path": row[p + "_landing_path"],
            "visitor_hash_prefix": row[p + "_visitor_prefix"],
        }

    items = []
    for r in rows:
        items.append({
            "user_id": r["user_id"],
            "display_name": r["display_name"],
            "login_id": r["login_id"],  # 路由层负责掩码（§9 红线）
            "role": r["role"],
            "source_code": r["source_code"],
            "campaign_id": r["campaign_id"],
            "attribution_method": r["attribution_method"],
            "invite_id": r["invite_id"],
            "attributed_at": r["attributed_at"],
            "first_touch": _touch(r, "first"),
            "last_touch": _touch(r, "last"),
        })
    next_cursor = None
    if has_more and rows:
        next_cursor = (rows[-1]["attributed_at"], rows[-1]["user_id"])
    return {"items": items, "next_cursor": next_cursor}
