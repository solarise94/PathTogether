# -*- coding: utf-8 -*-
"""计价与时段判定（admin-billing 方案 §4/§5，PR2）。

纯计算模块 + 接受 cursor 的价格查询：

- 时段判定：Asia/Shanghai，工作日 09:00–12:00、14:00–18:00（左闭右开）为
  peak，其余（清晨/午休/晚间/周末全天）为 off_peak。判定依据是事件
  ``occurred_at``，**不得**静默改用 received_at（那会在时段边界静默改账单）；
- 计价：三分项 ``ceil(tokens * rate_nano_per_million / 1_000_000)``，纯整数
  运算（等值整数式，禁 float），分项分别向上取整后求和，避免大量小请求被
  系统性舍零；
- 价格版本固定：事件入库时写死 price_book_id 与两种金额
  （billing_store.ingest_usage_event），后续调价不重算历史（重放返回原行）；
- 余额十进制字符串 → nano-CNY：Python ``Decimal`` 精确换算（最多 9 位小数，
  禁止先转 float 再乘比例），供 provider_balance_snapshots 写入复用。

price book 激活的事务串行化（§6.3：固定 key pg_advisory_xact_lock + active
区间重叠拒绝，不用 btree_gist）在 ``billing_store.activate_price_book``（需要
连接与事务，属存储层）。
"""

import re
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo

#: 计价时区（DeepSeek 高峰按北京时间）
PRICING_TIMEZONE = ZoneInfo("Asia/Shanghai")

#: 高峰窗口（左闭右开，秒数）：[09:00,12:00) ∪ [14:00,18:00)，仅工作日
PEAK_WINDOWS_SECONDS = ((9 * 3600, 12 * 3600), (14 * 3600, 18 * 3600))

#: nano-CNY 分母：价格单位是 nano-CNY / 百万 tokens
NANO_PER_MILLION_DENOMINATOR = 1_000_000

#: 价格 book kind（provider_cost 平台成本 / customer_charge 用户扣费）
PRICE_BOOK_KINDS = ("provider_cost", "customer_charge")

TIME_BANDS = ("peak", "off_peak")

#: 余额字符串上限小数位（nano = 1e-9 CNY，再小无法精确表示）
_BALANCE_MAX_DECIMALS = 9

_BALANCE_RE = re.compile(r"^[+-]?[0-9]+(\.[0-9]{1,%d})?$" % _BALANCE_MAX_DECIMALS)


def time_band_for(occurred_at) -> str:
    """occurred_at（aware datetime 或 RFC3339 字符串）→ 'peak' | 'off_peak'。

    规则（tests/fixtures/billing/time_band_cases.json rule_text）：北京时间
    工作日 09:00–12:00、14:00–18:00 为 peak（左闭右开），其余均为 off_peak；
    周六/周日全天 off_peak。任意偏移输入先转到 Asia/Shanghai 再套规则。
    """
    if isinstance(occurred_at, str):
        occurred_at = parse_rfc3339(occurred_at)
    if not isinstance(occurred_at, datetime) or occurred_at.tzinfo is None:
        raise ValueError("occurred_at 需为带时区的 datetime 或 RFC3339 字符串")
    local = occurred_at.astimezone(PRICING_TIMEZONE)
    if local.weekday() >= 5:  # 周六=5 / 周日=7→全天 off_peak
        return "off_peak"
    secs = local.hour * 3600 + local.minute * 60 + local.second
    for start, end in PEAK_WINDOWS_SECONDS:
        if start <= secs < end:
            return "peak"
    return "off_peak"


def parse_rfc3339(value: str) -> datetime:
    """RFC3339/ISO-8601（必须带时区）→ aware datetime（UTC）。

    ``Z``/``z``/``±HH:MM`` 偏移统一接受；小数位超出微秒精度按截断处理
    （canonical hash 规则只约定到微秒，PR0 README 明确更高精度未定义）。
    """
    if not isinstance(value, str):
        raise ValueError("时间需为字符串")
    s = value.strip()
    if s and s[-1] in "zZ":
        s = s[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(s).astimezone(timezone.utc)
    except (ValueError, TypeError) as exc:
        raise ValueError("非法 RFC3339 时间：%r" % (value,)) from exc


def ceil_div(numerator: int, denominator: int = NANO_PER_MILLION_DENOMINATOR) -> int:
    """非负整数向上取整除法（等值整数式，禁 float）。"""
    numerator = int(numerator)
    denominator = int(denominator)
    if numerator < 0 or denominator <= 0:
        raise ValueError("ceil_div 需要非负分子与正分母")
    return (numerator + denominator - 1) // denominator


def price_component_nano(tokens, rate_nano_per_million: int) -> int:
    """单分项费用：ceil(tokens × rate / 1_000_000) nano-CNY。

    tokens 为 null（中断无 usage）时返回 0——本函数只在具备完整 token 的
    priced 路径被调用，此防御分支不产生账单（调用方先判 unpriced）。
    """
    if tokens is None:
        return 0
    tokens = int(tokens)
    if tokens < 0:
        raise ValueError("token 计数不能为负")
    rate = int(rate_nano_per_million)
    if rate < 0:
        raise ValueError("单价不能为负")
    return ceil_div(tokens * rate)


def price_tokens_nano(hit_tokens, miss_tokens, output_tokens, rates: dict) -> int:
    """三分项计价求和（§5）：每个分项独立向上取整后相加。

    ``rates`` 为 billing_rates 行 dict（或含
    cache_hit_nano_per_million / cache_miss_nano_per_million /
    output_nano_per_million 三键的任意 dict）。
    """
    total = 0
    total += price_component_nano(
        hit_tokens, rates["cache_hit_nano_per_million"])
    total += price_component_nano(
        miss_tokens, rates["cache_miss_nano_per_million"])
    total += price_component_nano(
        output_tokens, rates["output_nano_per_million"])
    return total


# --------------------------------------------------------------------------- #
# 价格查询（接受 cursor，供 ingest 单事务复用；独立连接版在 billing_store）
# --------------------------------------------------------------------------- #
_ACTIVE_BOOK_SQL = (
    "SELECT b.price_book_id, b.kind, b.effective_from, b.effective_to, "
    "       r.provider, r.model, r.time_band, "
    "       r.cache_hit_nano_per_million, r.cache_miss_nano_per_million, "
    "       r.output_nano_per_million, r.timezone "
    "FROM billing_price_books b "
    "JOIN billing_rates r ON r.price_book_id = b.price_book_id "
    "WHERE b.kind = %s AND b.status = 'active' "
    "  AND r.provider = %s AND r.model = %s "
    "  AND r.time_band = %s "
    "  AND b.effective_from <= %s "
    "  AND (b.effective_to IS NULL OR b.effective_to > %s) "
    "ORDER BY b.effective_from DESC LIMIT 1"
)


def find_active_rate(cur, kind, provider, model, occurred_at) -> dict | None:
    """按 occurred_at 查 (kind, provider, model) 的 active 价格行。

    有效区间语义：effective_from <= occurred_at 且（effective_to IS NULL 或
    occurred_at < effective_to）。时段（peak/off_peak）由调用方先经
    time_band_for 判定。激活时的区间重叠拒绝保证同维度至多一本 active book；
    即便历史数据出现重叠，也取 effective_from 最新的一本（确定性）。

    返回 rate 行 dict（含 price_book_id）或 None（未知模型/无有效价格）。
    occurred_at 传 datetime（timestamptz 参数）。
    """
    if kind not in PRICE_BOOK_KINDS:
        raise ValueError("未知 price book kind：%r" % (kind,))
    band = time_band_for(occurred_at)
    cur.execute(_ACTIVE_BOOK_SQL,
                (kind, provider, model, band, occurred_at, occurred_at))
    row = cur.fetchone()
    if row is None:
        return None
    out = dict(row)
    out["time_band"] = band
    return out


def parse_balance_to_nano(value, currency: str = "CNY") -> int:
    """DeepSeek /user/balance 十进制金额字符串 → 精确 nano-CNY 整数。

    规则（§6.6）：只接受字符串（响应本身就是十进制字符串；JSON number 先经
    float 会失真，调用方须传原始字符串）；币种必须 CNY；至多 9 位小数
    （nano = 1e-9 CNY）；全程 ``Decimal`` 精确运算，禁止 float 中转。
    无法精确解析抛 ``ValueError``——调用方只记无敏感信息的错误类别并落
    抓取失败指标，**不写伪造的零余额**。
    """
    if not isinstance(value, str):
        raise ValueError("余额须为十进制字符串（禁止 number/float 中转）")
    s = value.strip()
    if not _BALANCE_RE.match(s):
        raise ValueError("余额格式非法（最多 %d 位小数）" % _BALANCE_MAX_DECIMALS)
    if (currency or "CNY").upper() != "CNY":
        raise ValueError("不支持的余额币种：%r" % (currency,))
    try:
        nano = Decimal(s) * Decimal(1_000_000_000)
    except InvalidOperation as exc:  # pragma: no cover - 正则已保证形态
        raise ValueError("余额换算失败") from exc
    if nano != nano.to_integral_value():
        raise ValueError("余额超出 nano 精度（>%d 位小数）"
                         % _BALANCE_MAX_DECIMALS)  # pragma: no cover
    return int(nano)
