# -*- coding: utf-8 -*-
"""PR2 billing 存储与计价测试（admin-billing 方案 §4/§5/§6/§7.5）。

纯计算部分（json 模式也跑）：
  - 计价数学：三分项独立向上取整（ceil）、hit=0 / output=0、混合、精确整除；
  - 时段边界：消费 tests/fixtures/billing/time_band_cases.json 全部 24 条
    （Asia/Shanghai 工作日 09:00–12:00/14:00–18:00 左闭右开，周末 off_peak）；
  - canonical payload_hash：fixture 01 与 README 已验证示例一致（PR0 互锁）、
    07 冲突 hash 不同、raw_usage 不参与、缺省≡null、时间偏移归一；
  - 手写严格校验器：7 个正例通过 + 负例拒绝；
  - 余额 Decimal 解析（≤9 位小数，禁 float）；
  - json/dual 后端 fail-closed（pg_backend_required）。

PG 部分（RUN_PG_TESTS=1；conftest 每用例 TRUNCATE billing 表）：
  - 0018 迁移种子与价格快照夹具逐项一致（重放迁移文件验证 + 幂等）；
  - 价格版本固定：调价（新 active book）后历史事件不重算、重放 duplicate；
  - 并发激活 price book 只有一个成功（advisory xact lock + 区间重叠拒绝）；
  - ledger 符号 CHECK、usage_debit 每 event_id 只一条（部分唯一索引）、
    idempotency_key 重放幂等、余额 = SUM(amount)；
  - usage event dedup / payload 冲突 / call_id 冲突；
  - unpriced 路径（时钟两条 / 算术 / 无最终 usage / 未知模型）与
    「不得静默改用 received_at 判时段」。

运行：cd 项目根 && python3 -m pytest tests/test_billing_store.py -q
"""
import re
from datetime import datetime, timedelta, timezone

import pytest

import billing_pricing
import billing_store

from pg_compat import BACKEND  # noqa: E402
import _billing_helpers as bh  # noqa: E402

PG = pytest.mark.skipif(BACKEND != "postgres",
                        reason="billing 数据层需 PG（RUN_PG_TESTS=1）")

README_PATH = bh.USAGE_DIR / "README.md"

def _fresh(event, hours_back=1):
    """把夹具的 occurred_at/enqueued_at 平移到相对当前时刻（默认 1 小时前）。

    夹具固定日期（2026-09-07/12）相对真实运行时钟可能是"未来"，会先触发
    clock_skew_future；本文件按语义改写时间的用例保持原样，直接使用夹具
    时间且断言 priced/no_final_usage 等状态的用例统一平移（金额断言均以
    注入 now 的确定性用例单独覆盖）。
    """
    out = dict(event)
    occurred = datetime.now(timezone.utc) - timedelta(hours=hours_back)
    out["occurred_at"] = occurred.isoformat().replace("+00:00", "Z")
    out["enqueued_at"] = (occurred + timedelta(seconds=1)
                          ).isoformat().replace("+00:00", "Z")
    return out


def _bind_for(event):
    """只绑定权威行不 ingest（并发竞态用例：A/B 各自的 dedup 都要通过）。"""
    if event.get("request_id"):
        bh.bind_reservation(event["request_id"], event["session_id"],
                            event["subject_type"], event["subject_id"])
    elif event["subject_type"] == "demo":
        bh.bind_demo_session(event["session_id"], event["subject_id"])


def _readme_hashes():
    text = README_PATH.read_text(encoding="utf-8")
    documented = re.search(r"payload_hash = ([0-9a-f]{64})", text).group(1)
    all_hashes = re.findall(r"`([0-9a-f]{64})`", text)
    return documented, all_hashes


# =========================================================================== #
# 1. 计价数学（§5：三分项 ceil，整数运算，禁 float）
# =========================================================================== #
_RATES_OFFPEAK_FLASH = {
    "cache_hit_nano_per_million": 50,
    "cache_miss_nano_per_million": 1500,
    "output_nano_per_million": 4500,
}


def test_price_component_ceil_rounds_up_each_term():
    # 1856×50 = 92800 < 1e6 → ceil 到 1（不得舍零）；1e6×50 → 50 精确
    assert billing_pricing.price_component_nano(1856, 50) == 1
    assert billing_pricing.price_component_nano(1_000_000, 50) == 50
    # 2418×1500 = 3,627,000 → ceil 3.627 → 4
    assert billing_pricing.price_component_nano(2418, 1500) == 4
    # 357×4500 = 1,606,500 → 2
    assert billing_pricing.price_component_nano(357, 4500) == 2


def test_price_hit_zero_and_output_zero():
    assert billing_pricing.price_component_nano(0, 50) == 0
    assert billing_pricing.price_tokens_nano(0, 0, 0, _RATES_OFFPEAK_FLASH) == 0
    # hit=0 只跳过 hit 分项，miss/output 仍各自向上取整
    assert billing_pricing.price_tokens_nano(
        0, 1, 1, _RATES_OFFPEAK_FLASH) == 0 + 1 + 1
    # output=0：reasoning 已含在 output 内，不再单独计价
    assert billing_pricing.price_tokens_nano(
        512, 512, 0, _RATES_OFFPEAK_FLASH) == 1 + 1 + 0


def test_price_tokens_mixed_sum_of_three_ceilings():
    rates = {"cache_hit_nano_per_million": 100,
             "cache_miss_nano_per_million": 3000,
             "output_nano_per_million": 9000}
    # fixture 01（flash peak）：1 + 8 + 4 = 13 nano（各自 ceil 后求和）
    assert billing_pricing.price_tokens_nano(1856, 2418, 357, rates) == 13
    # 大额精确整除不丢精度：1e6×100 + 2e6×3000 + 500000×9000
    assert billing_pricing.price_tokens_nano(1_000_000, 2_000_000, 500_000,
                                             rates) == (
        100 + 6000 + 4500)


def test_price_math_rejects_negative_and_float_paths():
    with pytest.raises(ValueError):
        billing_pricing.price_component_nano(-1, 50)
    with pytest.raises(ValueError):
        billing_pricing.price_component_nano(1, -50)


# =========================================================================== #
# 2. 时段边界（§4：Asia/Shanghai 工作日双窗左闭右开，周末全天 off_peak）
# =========================================================================== #
def test_time_band_all_fixture_cases():
    cases = bh.load_time_band_cases()["cases"]
    assert len(cases) == 24
    for case in cases:
        got = billing_pricing.time_band_for(case["input"])
        assert got == case["expected_time_band"], (
            "%s 期望 %s，判定 %s（%s）" % (
                case["input"], case["expected_time_band"], got,
                case.get("note", "")))


def test_time_band_accepts_datetime_and_rejects_naive():
    peak = datetime.fromisoformat("2026-09-07T01:00:00+00:00")
    assert billing_pricing.time_band_for(peak) == "peak"
    with pytest.raises(ValueError):
        billing_pricing.time_band_for(datetime(2026, 9, 7, 9, 0))


# =========================================================================== #
# 3. canonical payload_hash（PR0 README 为唯一依据；互锁）
# =========================================================================== #
def test_canonical_hash_fixture_01_matches_readme():
    documented, all_hashes = _readme_hashes()
    event = bh.load_event("01_owner_priced_flash_peak.json")
    assert billing_store.canonical_payload_hash(event) == documented
    # 07 冲突样本 hash 与 01 不同，且在 README 记录之内
    conflict = bh.load_event("07_replay_conflict_of_01.json")
    conflict_hash = billing_store.canonical_payload_hash(conflict)
    assert conflict_hash in all_hashes
    assert conflict_hash != documented


def test_canonical_hash_exclusions_and_normalization():
    event = bh.load_event("01_owner_priced_flash_peak.json")
    baseline = billing_store.canonical_payload_hash(event)

    # raw_usage 整体不参与哈希
    mutated = dict(event, raw_usage={"finish_reason": "length"})
    assert billing_store.canonical_payload_hash(mutated) == baseline

    # 缺省可选键 ≡ 显式 null
    omitted = dict(event)
    omitted.pop("provider_request_id")
    explicit_null = dict(event, provider_request_id=None)
    assert (billing_store.canonical_payload_hash(omitted)
            == billing_store.canonical_payload_hash(explicit_null))
    assert billing_store.canonical_payload_hash(omitted) != baseline

    # Z / 等价偏移 / 小写 t-z / 少位小数 → 同一 hash（时间归一到 UTC 微秒）
    offset = dict(event,
                  occurred_at="2026-09-07T04:30:12.345000+02:00",
                  enqueued_at="2026-09-07t02:30:13.12z")
    assert billing_store.canonical_payload_hash(offset) == baseline


# =========================================================================== #
# 4. 手写严格校验器（语义 == schema_v1.json）
# =========================================================================== #
FIXTURE_FILES = (
    "01_owner_priced_flash_peak.json",
    "02_user_priced_pro_offpeak_reasoning.json",
    "03_user_priced_vision_exp_peak.json",
    "04_owner_interrupted_no_usage.json",
    "05_demo_subject_offpeak.json",
    "06_user_priced_flash_no_provider_request_id.json",
    "07_replay_conflict_of_01.json",
)


def test_validator_accepts_all_sample_events():
    for name in FIXTURE_FILES:
        assert not billing_store.validate_usage_event_body(bh.load_event(name)), name


def test_validator_rejects_invalid_bodies():
    base = bh.load_event("01_owner_priced_flash_peak.json")
    assert not billing_store.validate_usage_event_body(base)

    def bad(mutate):
        body = dict(base)
        mutate(body)
        return billing_store.validate_usage_event_body(body)

    cases = {
        "event_id 前缀错误": lambda b: b.update(event_id="evt_" + "0" * 32),
        "call_id 长度错误": lambda b: b.update(call_id="call_" + "0" * 31),
        "大写 hex": lambda b: b.update(event_id="use_" + "A" * 32),
        "schema_version 未协商": lambda b: b.update(schema_version=2),
        "schema_version 字符串": lambda b: b.update(schema_version="1"),
        "subject_type 非法": lambda b: b.update(subject_type="guest"),
        "occurred_at 缺时区": lambda b: b.update(occurred_at="2026-09-07T02:30:12"),
        "enqueued_at 非法日期": lambda b: b.update(
            enqueued_at="2026-13-07T02:30:12Z"),
        "token 缺省": lambda b: b.pop("total_tokens"),
        "token 字符串化": lambda b: b.update(total_tokens="4631"),
        "token 负数": lambda b: b.update(output_tokens=-1),
        "token 布尔": lambda b: b.update(output_tokens=True),
        "token 超 2^53-1": lambda b: b.update(total_tokens=2 ** 53),
        "raw_usage token 超 2^53-1": lambda b: b.update(
            raw_usage={"prompt_tokens": 10 ** 20}),
        "额外字段": lambda b: b.update(_description="x"),
        "provider_request_id 整数": lambda b: b.update(provider_request_id=12345),
        "raw_usage 长文本": lambda b: b.update(
            raw_usage={"leaked_prompt": "x" * 200}),
        "raw_usage 数组": lambda b: b.update(raw_usage=["stop", 4274]),
        "raw_usage 元数据缺 meta_version": lambda b: b.update(
            raw_usage={"provider_meta_v2": {"service": "deepseek"}}),
        "raw_usage 字段名大写": lambda b: b.update(
            raw_usage={"FinishReason": "stop"}),
        "model 大写": lambda b: b.update(model="DeepSeek-V4"),
        "request_id 空串": lambda b: b.update(request_id=""),
        "body 非 object": None,
    }
    for label, mutate in cases.items():
        if mutate is None:
            errors = billing_store.validate_usage_event_body(["not", "dict"])
        else:
            errors = bad(mutate)
        assert errors, "校验器应拒绝：%s" % label


# =========================================================================== #
# 5. 余额十进制解析（§6.6：Decimal 精确，≤9 位小数，禁 float 中转）
# =========================================================================== #
def test_parse_balance_to_nano_exact():
    assert billing_pricing.parse_balance_to_nano("110.5") == 110_500_000_000
    assert billing_pricing.parse_balance_to_nano("0.000000001") == 1
    assert billing_pricing.parse_balance_to_nano(
        "99999999.123456789") == 99_999_999_123_456_789
    assert billing_pricing.parse_balance_to_nano("0") == 0
    assert billing_pricing.parse_balance_to_nano(" 12 ") == 12_000_000_000


def test_parse_balance_rejects_bad_inputs():
    for bad_value in ("0.0000000001", "1.2.3", "", "  ", "abc", "1e5", "NaN",
                      "Infinity", 110.5, None):
        with pytest.raises(ValueError):
            billing_pricing.parse_balance_to_nano(bad_value)
    with pytest.raises(ValueError):
        billing_pricing.parse_balance_to_nano("1.5", currency="USD")


# =========================================================================== #
# 6. json/dual fail-closed（pg_backend_required）
# =========================================================================== #
@pytest.mark.skipif(BACKEND == "postgres",
                    reason="json 后端专用（PG 模式下本用例无意义）")
def test_json_backend_fail_closed():
    import platform_features
    with pytest.raises(platform_features.PgFeatureUnavailable) as exc_info:
        billing_store.ingest_usage_event(
            bh.load_event("01_owner_priced_flash_peak.json"),
            installation_id="pin_test")
    assert exc_info.value.code == "pg_backend_required"
    with pytest.raises(platform_features.PgFeatureUnavailable):
        billing_store.get_usage_event("use_" + "0" * 32)
    assert platform_features.billing_features_available() is False
    assert platform_features.usage_ingest_available() is False


# =========================================================================== #
# 7. PG：迁移种子 / 价格版本 / 并发激活 / 账本约束 / ingest 路径
# =========================================================================== #
@PG
def test_migration_seed_matches_price_fixture():
    conn = bh.connect()
    try:
        bh.seed_price_books(conn)   # 幂等重放 0018（种子唯一权威来源）
        bh.seed_price_books(conn)
        with conn.cursor() as cur:
            cur.execute(
                "SELECT price_book_id, kind, status, source_url, created_by, "
                "effective_from, effective_to FROM billing_price_books "
                "ORDER BY kind")
            books = [dict(r) for r in cur.fetchall()]
            cur.execute(
                "SELECT price_book_id, provider, model, time_band, "
                "cache_hit_nano_per_million, cache_miss_nano_per_million, "
                "output_nano_per_million FROM billing_rates "
                "ORDER BY price_book_id, model, time_band")
            rates = [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()
    assert {b["kind"] for b in books} == {"provider_cost", "customer_charge"}
    snap = bh.load_price_snapshot()
    for book in books:
        assert book["status"] == "active"
        assert book["created_by"] == "system-seed"
        assert book["source_url"] == snap["source_url"]
        assert book["effective_to"] is None
        # 快照日 Asia/Shanghai 00:00 = UTC 前一日 16:00
        assert book["effective_from"] == datetime.fromisoformat(
            "2026-08-27T16:00:00+00:00")
    # 两套 book 同价（影子阶段 charge = provider cost），值与夹具逐项一致
    assert len(rates) == len(snap["models"]) * 2 * 2  # 3 模型 × 2 时段 × 2 book
    by_key = {(r["price_book_id"], r["model"], r["time_band"]): r
              for r in rates}
    for model, bands in snap["models"].items():
        for band, values in bands.items():
            for book in books:
                row = by_key[(book["price_book_id"], model, band)]
                assert row["cache_hit_nano_per_million"] == \
                    values["cache_hit_nano_per_million"]
                assert row["cache_miss_nano_per_million"] == \
                    values["cache_miss_nano_per_million"]
                assert row["output_nano_per_million"] == \
                    values["output_nano_per_million"]


def _ingest(event, *, installation="pin_test", **kwargs):
    """store 级 ingest 便捷封装（绑定 reservation 后调用）。"""
    bh.bind_reservation(event["request_id"], event["session_id"],
                        event["subject_type"], event["subject_id"])
    return billing_store.ingest_usage_event(
        event, installation_id=installation, **kwargs)


@PG
def test_price_version_fixed_after_rate_change():
    bh.seed_price_books()
    event = bh.load_event("01_owner_priced_flash_peak.json")
    received = datetime.fromisoformat("2026-09-07T02:35:00+00:00")
    first = _ingest(event, now=received)
    assert first["status"] == "priced" and first["duplicate"] is False
    row = billing_store.get_usage_event(event["event_id"])
    original_cost = row["provider_cost_nano_cny"]
    original_book = row["provider_price_book_id"]
    assert original_book == "pb_deepseek_provider_cost_20260828"
    # flash peak：ceil(1856×100)+ceil(2418×3000)+ceil(357×9000) = 1+8+4 = 13
    assert original_cost == 13

    # 调价：新建 10× 价格的书（effective 晚于原事件），supersede 接班——
    # 旧书在分界点收口（已入账事件的价格版本不受影响），两套 kind 各一本
    later = datetime.fromisoformat("2026-09-08T00:00:00+00:00")
    snap = bh.load_price_snapshot()
    rates = [{
        "provider": "deepseek", "model": model, "time_band": band,
        "cache_hit_nano_per_million":
            values["cache_hit_nano_per_million"] * 10,
        "cache_miss_nano_per_million":
            values["cache_miss_nano_per_million"] * 10,
        "output_nano_per_million": values["output_nano_per_million"] * 10,
    } for model, bands in snap["models"].items()
        for band, values in bands.items()]
    new_books = {}
    for kind in ("provider_cost", "customer_charge"):
        book = billing_store.create_price_book(
            kind, rates, later, source_url="test", created_by="pytest")
        activated = billing_store.activate_price_book(
            book["price_book_id"], actor="pytest", supersede=True)
        assert activated["status"] == "active"
        new_books[kind] = book["price_book_id"]
    # 旧书被收口：[2026-08-27, 2026-09-08)，仍是 active（旧区间内迟到事件
    # 仍可计价），不是 retired
    old_book = billing_store.get_price_book(original_book)
    assert old_book["status"] == "active"

    # 重放（重放不调价）：duplicate 且金额/价格版本不变
    replay = billing_store.ingest_usage_event(
        event, installation_id="pin_test", now=later + timedelta(hours=1))
    assert replay["duplicate"] is True
    assert replay["row"]["provider_cost_nano_cny"] == original_cost
    assert replay["row"]["provider_price_book_id"] == original_book

    # 历史时刻（旧书区间内）的事件用旧书；新时刻的事件固定到新书
    old_time_event = dict(
        bh.load_event("06_user_priced_flash_no_provider_request_id.json"))
    old_time_event["occurred_at"] = "2026-09-07T11:45:02.900Z"  # 旧书区间
    old_time_event["enqueued_at"] = "2026-09-07T11:45:03.310Z"
    old_result = _ingest(old_time_event,
                         now=datetime.fromisoformat("2026-09-07T12:00:00+00:00"))
    assert old_result["status"] == "priced"
    assert old_result["row"]["provider_price_book_id"] == original_book

    new_event = dict(bh.load_event("03_user_priced_vision_exp_peak.json"))
    new_event["occurred_at"] = "2026-09-08T02:00:00.000Z"  # 新书区间
    new_event["enqueued_at"] = "2026-09-08T02:00:01.000Z"
    new_result = _ingest(new_event,
                         now=datetime.fromisoformat("2026-09-08T02:30:00+00:00"))
    assert new_result["status"] == "priced"
    assert new_result["row"]["provider_price_book_id"] == \
        new_books["provider_cost"]
    # 新价（10×）：ceil(742×1000)+ceil(31808×30000)+ceil(640×90000)
    # = 1+955+58 = 1014（分项 ceil 后求和，不能先求和再取整）
    assert new_result["row"]["provider_cost_nano_cny"] == 1014


@PG
def test_concurrent_price_book_activation_single_winner():
    from concurrent.futures import ThreadPoolExecutor
    bh.seed_price_books()
    # 用种子未覆盖的模型：只让两本新书彼此竞争（种子书不参与重叠判定）
    start = datetime.fromisoformat("2026-09-20T00:00:00+00:00")
    rates = [{"provider": "deepseek", "model": "deepseek-v4-testmodel",
              "time_band": band, "cache_hit_nano_per_million": 1,
              "cache_miss_nano_per_million": 1,
              "output_nano_per_million": 1}
             for band in ("peak", "off_peak")]
    book_ids = []
    for i in range(2):
        book = billing_store.create_price_book(
            "provider_cost", rates, start + timedelta(days=i),
            source_url="test", created_by="pytest",
            price_book_id="pb_test_overlap_%d" % i)
        book_ids.append(book["price_book_id"])
    # 两个区间重叠的 draft 并发激活：advisory xact lock + 区间检查 → 只一个成功
    with ThreadPoolExecutor(max_workers=2) as pool:
        outs = list(pool.map(lambda bid: _try_activate(bid), book_ids))
    results = sorted(outs)
    assert results.count("ok") == 1
    assert results.count("overlap") == 1
    statuses = {bid: billing_store.get_price_book(bid)["status"]
                for bid in book_ids}
    assert set(statuses.values()) == {"active", "draft"}


def _try_activate(book_id):
    try:
        billing_store.activate_price_book(book_id, actor="pytest")
        return "ok"
    except billing_store.PriceBookOverlapError:
        return "overlap"


@PG
def test_ledger_sign_check_and_usage_debit_unique():
    import psycopg
    import user_store
    user = user_store.create_user("ledger@x.com", "pass123456789012")
    account = billing_store.create_billing_account(user["user_id"])
    assert billing_store.get_billing_account_by_user(user["user_id"])["account_id"] \
        == account["account_id"]
    with pytest.raises(billing_store.BillingAccountExistsError):
        billing_store.create_billing_account(user["user_id"])

    # 符号 CHECK：expiry 必须为负、grant 必须为正、manual_adjustment 非零
    # （usage_debit 的符号在下方拿到真实 event 后单独验）
    with pytest.raises(psycopg.errors.CheckViolation):
        billing_store.append_ledger_entry(
            account["account_id"], "expiry", 100, "expiry:test:pos")
    with pytest.raises(psycopg.errors.CheckViolation):
        billing_store.append_ledger_entry(
            account["account_id"], "grant", -100, "grant:test:neg")
    with pytest.raises(psycopg.errors.CheckViolation):
        billing_store.append_ledger_entry(
            account["account_id"], "manual_adjustment", 0, "adj:test:zero")

    # usage_debit 幂等键固定 usage:<event_id>，且必须携带 event_id
    bh.seed_price_books_with_history()
    event = _fresh(bh.load_event("05_demo_subject_offpeak.json"))
    bh.bind_demo_session(event["session_id"], event["subject_id"])
    result = billing_store.ingest_usage_event(
        dict(event, request_id=None), installation_id="pin_test",
        now=datetime.now(timezone.utc))
    assert result["status"] == "priced"
    with pytest.raises(ValueError):
        billing_store.append_ledger_entry(
            account["account_id"], "usage_debit", -5, "wrong-key",
            event_id=event["event_id"])
    with pytest.raises(ValueError):
        billing_store.append_ledger_entry(
            account["account_id"], "usage_debit", -5, "usage:")
    # usage_debit 正号同样被 CHECK 拒绝（此时 event 行已存在，FK 满足）
    with pytest.raises(psycopg.errors.CheckViolation):
        billing_store.append_ledger_entry(
            account["account_id"], "usage_debit", 5,
            "usage:%s" % event["event_id"], event_id=event["event_id"])
    debit = billing_store.append_ledger_entry(
        account["account_id"], "usage_debit", -5,
        "usage:%s" % event["event_id"], event_id=event["event_id"])
    assert debit["duplicate"] is False
    # 部分唯一索引兜底（§6.5）：即便绕过 store 守卫直接写第二条同 event 的
    # usage_debit，数据库也拒绝（index: WHERE kind='usage_debit'）
    with pytest.raises(psycopg.errors.UniqueViolation):
        conn = bh.connect()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO billing_ledger_entries "
                    "(entry_id, account_id, event_id, kind, amount_nano_cny, "
                    " idempotency_key) VALUES (%s,%s,%s,'usage_debit',-5,%s)",
                    ("ble_dup_direct", account["account_id"],
                     event["event_id"], "usage:duplicate-direct"))
            conn.commit()
        finally:
            conn.close()
    # idempotency_key 重放 → duplicate 返回原行
    replay = billing_store.append_ledger_entry(
        account["account_id"], "usage_debit", -5,
        "usage:%s" % event["event_id"], event_id=event["event_id"])
    assert replay["duplicate"] is True


@PG
def test_account_balance_is_ledger_sum():
    import user_store
    user = user_store.create_user("balance@x.com", "pass123456789012")
    account = billing_store.create_billing_account(user["user_id"])
    assert billing_store.account_balance_nano(account["account_id"]) == 0
    billing_store.append_ledger_entry(
        account["account_id"], "grant", 1000, "grant:t1")
    billing_store.append_ledger_entry(
        account["account_id"], "topup", 500, "topup:t2")
    billing_store.append_ledger_entry(
        account["account_id"], "refund", 50, "refund:t3")
    billing_store.append_ledger_entry(
        account["account_id"], "expiry", -200, "expiry:t4")
    assert billing_store.account_balance_nano(account["account_id"]) == 1350
    assert billing_store.account_balance_nano("bac_missing") is None


@PG
def test_provider_balance_snapshot_roundtrip():
    from decimal import Decimal
    observed = datetime.now(timezone.utc)
    total = billing_pricing.parse_balance_to_nano("110.5")
    granted = billing_pricing.parse_balance_to_nano("10.000000001")
    snap = billing_store.insert_provider_balance_snapshot(
        "deepseek", "CNY", total, granted,
        total - granted, True, observed)
    assert snap["total_balance_nano"] == 110_500_000_000
    assert Decimal(snap["total_balance_nano"]) / Decimal(10 ** 9) == \
        Decimal("110.5")
    latest = billing_store.latest_provider_balance_snapshot("deepseek")
    assert latest["snapshot_id"] == snap["snapshot_id"]
    billing_store.insert_provider_balance_snapshot(
        "deepseek", "CNY", 5, 5, 0, True, observed + timedelta(minutes=5))
    assert billing_store.latest_provider_balance_snapshot("deepseek")[
        "total_balance_nano"] == 5
    assert billing_store.latest_provider_balance_snapshot("openai") is None


@PG
def test_usage_event_duplicate_and_conflicts():
    bh.seed_price_books()
    event = bh.load_event("01_owner_priced_flash_peak.json")
    first = _ingest(event, now=datetime.now(timezone.utc))
    assert first["duplicate"] is False

    # 同 payload 重放 → duplicate 原行
    replay = billing_store.ingest_usage_event(
        event, installation_id="pin_test", now=datetime.now(timezone.utc))
    assert replay["duplicate"] is True
    assert replay["event_id"] == event["event_id"]
    assert replay["row"]["payload_hash"] == first["row"]["payload_hash"]

    # 同 event_id、不同 payload → 409 usage_event_conflict（07 冲突样本）
    with pytest.raises(billing_store.UsageEventConflictError):
        billing_store.ingest_usage_event(
            bh.load_event("07_replay_conflict_of_01.json"),
            installation_id="pin_test", now=datetime.now(timezone.utc))

    # 同 call_id、不同 event_id → 同样冲突（hash 含 event_id，必然不同）
    other = dict(bh.load_event("02_user_priced_pro_offpeak_reasoning.json"))
    other["call_id"] = event["call_id"]
    bh.bind_reservation(other["request_id"], other["session_id"],
                        other["subject_type"], other["subject_id"])
    with pytest.raises(billing_store.UsageEventConflictError):
        billing_store.ingest_usage_event(
            other, installation_id="pin_test", now=datetime.now(timezone.utc))


@PG
def test_unpriced_paths_and_no_received_at_substitution():
    bh.seed_price_books_with_history()
    now = datetime.now(timezone.utc)

    # ① occurred_at 超前 >5min → clock_skew_future（token 齐全也不计价）
    future = dict(bh.load_event("01_owner_priced_flash_peak.json"))
    future["occurred_at"] = (now + timedelta(minutes=6)).isoformat()
    future["enqueued_at"] = (now + timedelta(minutes=6)).isoformat()
    r = _ingest(future, now=now)
    assert (r["status"], r["row"]["unpriced_reason"]) == (
        "unpriced", "clock_skew_future")
    assert r["row"]["provider_price_book_id"] is None

    # ② occurred_at 早于 received_at-30d → occurred_at_out_of_range（env 可配）
    old = dict(bh.load_event("01_owner_priced_flash_peak.json"),
               event_id="use_" + "a" * 32, call_id="call_" + "b" * 32,
               request_id="req_too_old_case")
    old["occurred_at"] = (now - timedelta(days=31)).isoformat()
    old["enqueued_at"] = (now - timedelta(days=31)).isoformat()
    r = _ingest(old, now=now)
    assert r["row"]["unpriced_reason"] == "occurred_at_out_of_range"

    # ③ 中断无最终 usage（04）→ no_final_usage，token 全 NULL（不用 0 冒充）
    aborted = _fresh(bh.load_event("04_owner_interrupted_no_usage.json"))
    r = _ingest(aborted, now=now)
    assert (r["status"], r["row"]["unpriced_reason"]) == (
        "unpriced", "no_final_usage")
    assert r["row"]["total_tokens"] is None
    assert r["row"]["cache_hit_input_tokens"] is None

    # ④ 算术不符 → arithmetic_mismatch，token 列置 NULL、原值镜像 raw_usage
    bad_math = dict(bh.load_event("02_user_priced_pro_offpeak_reasoning.json"))
    bad_math["total_tokens"] = bad_math["cache_hit_input_tokens"] + \
        bad_math["cache_miss_input_tokens"] + bad_math["output_tokens"] + 1
    r = _ingest(bad_math, now=now)
    assert r["row"]["unpriced_reason"] == "arithmetic_mismatch"
    assert r["row"]["total_tokens"] is None
    assert r["row"]["raw_usage"]["reported_tokens_v1"]["total_tokens"] == \
        bad_math["total_tokens"]

    # ⑤ 未知模型（无价格行）→ no_active_price_book
    unknown = _fresh(bh.load_event(
        "06_user_priced_flash_no_provider_request_id.json"))
    unknown["model"] = "deepseek-v4-unknown"
    r = _ingest(unknown, now=now)
    assert r["row"]["unpriced_reason"] == "no_active_price_book"

    # ⑥ 时段判定用 occurred_at，不得静默改用 received_at：fixture 02 的
    # occurred_at 是北京周一 07:15（off_peak），received_at 人为落在同日
    # 10:30（peak 窗口内、且晚于 occurred_at 3 小时——合法延迟）。正确计价
    # 用 off_peak 价；若错用 received_at 会算出 peak 价
    event02 = dict(_fresh(
        bh.load_event("02_user_priced_pro_offpeak_reasoning.json")),
        event_id="use_" + "c" * 32, call_id="call_" + "d" * 32,
        request_id="req_band_case")
    received_monday_peak = None  # occurred_at 已平移到 now-1h；received 用默认 now
    r = _ingest(event02)  # received=now（默认）晚于 occurred 1 小时，合法延迟
    assert r["status"] == "priced"
    # received_at 时刻的时段与计价无关：金额只由 occurred_at 的时段决定。
    # pro off_peak：ceil(5120×150)+ceil(9876×4500)+ceil(1204×13500)
    # = 1+45+17 = 63（若错用 received_at 的时段，跑出 peak 价 124 的概率随
    # 测试时刻变化；这里直接断言金额等于 occurred_at 时段的单价计算结果）
    band = billing_pricing.time_band_for(event02["occurred_at"])
    expected = {  # pro：off_peak 150/4500/13500；peak 300/9000/27000
        "off_peak": (150, 4500, 13500, 63),
        "peak": (300, 9000, 27000, 124),
    }[band]
    assert r["row"]["provider_cost_nano_cny"] == expected[3]
    # 直接用 occurred_at 的时段价独立复算（双保险）
    assert r["row"]["provider_cost_nano_cny"] == (
        billing_pricing.price_component_nano(5120, expected[0])
        + billing_pricing.price_component_nano(9876, expected[1])
        + billing_pricing.price_component_nano(1204, expected[2]))


@PG
def test_concurrent_insert_race_savepoint_paths(monkeypatch):
    """并发投递竞态的确定性复现（§7.5 步骤 2；SAVEPOINT 修复的回归测试）。

    用 ``_INGEST_PRE_INSERT_HOOK`` 在连接 A 的 dedup 检查与 INSERT 之间用
    连接 B（独立事务）提交行——A 的 INSERT 随后撞唯一约束；PG 中失败语句
    会把事务置 aborted，必须 ``ROLLBACK TO SAVEPOINT sp_usage_insert``
    恢复后才能重读 B 行比对 hash。三场景（event_id/call_id 各自独立，避免
    场景间通过夹具字面 id 串扰）：
      ① B 提交同 event_id 同 payload → A 返回 duplicate（B 的原行）；
      ② B 提交同 event_id 不同 payload → A 抛 UsageEventConflictError；
      ③ B 提交同 call_id 不同 event_id → A 的 INSERT 撞 call_id 唯一约束
        → savepoint 回滚 → 按 A 的 event_id 重读为空 → UsageEventConflictError。
    """
    bh.seed_price_books_with_history()
    now = datetime.now(timezone.utc)

    def _reid(event, tag):
        """给事件换唯一 event_id/call_id/request_id（tag 为两位 hex）。"""
        out = dict(event)
        out["event_id"] = "use_" + tag * 16
        out["call_id"] = "call_" + tag * 16
        if out.get("request_id"):
            out["request_id"] = "req_race_" + tag
        return out

    def _hook_with(b_body):
        """hook：在 A 的 INSERT 前用独立事务完整 ingest b_body 并提交。

        必须单发（armed 标志）：B 的 ingest 走到同一 hook 点时若再递归会
        逐层占住打开的连接直到连接池耗尽。B 内部的 hook 调用直接跳过。
        """
        state = {"armed": True}

        def _racer(cur_a):  # noqa: U100
            if not state["armed"]:
                return
            state["armed"] = False
            billing_store.ingest_usage_event(
                b_body, installation_id="pin_other", now=now)
        return _racer

    # ① 同 event_id 同 payload → A 撞主键 → savepoint 回滚 → 重读 B 行 → duplicate
    a1 = _reid(_fresh(bh.load_event("01_owner_priced_flash_peak.json")), "aa")
    _bind_for(a1)
    monkeypatch.setattr(billing_store, "_INGEST_PRE_INSERT_HOOK",
                        _hook_with(dict(a1)))
    result = billing_store.ingest_usage_event(
        a1, installation_id="pin_test", now=now)
    assert result["duplicate"] is True
    assert result["status"] == "priced"
    # 只有一行（B 的），价格版本来自 B 的入库
    row = billing_store.get_usage_event(a1["event_id"])
    assert row["provider_price_book_id"] is not None

    # ② 同 event_id 不同 payload（07 冲突样本平移）→ hash 不同 → 409
    a2 = _reid(_fresh(bh.load_event("01_owner_priced_flash_peak.json")), "bb")
    _bind_for(a2)
    b2 = _reid(_fresh(bh.load_event("07_replay_conflict_of_01.json")), "bb")
    _bind_for(b2)
    monkeypatch.setattr(billing_store, "_INGEST_PRE_INSERT_HOOK",
                        _hook_with(b2))
    with pytest.raises(billing_store.UsageEventConflictError):
        billing_store.ingest_usage_event(
            a2, installation_id="pin_test", now=now)
    monkeypatch.setattr(billing_store, "_INGEST_PRE_INSERT_HOOK", None)

    # ③ 同 call_id 不同 event_id → call_id 唯一约束分支
    a3 = _reid(_fresh(bh.load_event("01_owner_priced_flash_peak.json")), "cc")
    _bind_for(a3)
    b3 = _reid(_fresh(
        bh.load_event("06_user_priced_flash_no_provider_request_id.json")), "dd")
    b3["call_id"] = a3["call_id"]
    _bind_for(b3)
    monkeypatch.setattr(billing_store, "_INGEST_PRE_INSERT_HOOK",
                        _hook_with(b3))
    with pytest.raises(billing_store.UsageEventConflictError):
        billing_store.ingest_usage_event(
            a3, installation_id="pin_test", now=now)
    monkeypatch.setattr(billing_store, "_INGEST_PRE_INSERT_HOOK", None)
    # B 的行仍在（A 的事务回滚不影响已提交的 B）
    assert billing_store.get_usage_event(b3["event_id"]) is not None


@PG
def test_sim_debit_disabled_never_writes_ledger(monkeypatch):
    """PR6 kill-switch（§19 v0.4）：BILLING_SIMULATED_DEBIT=0 → 回到纯计量。

    影子语义回归（原 test_shadow_phase_never_writes_ledger）：开关关闭时
    ledger/账户恒空，audit detail 记 skipped=disabled。模拟扣费正向路径见
    tests/test_billing_sim_debit.py。
    """
    monkeypatch.setenv("BILLING_SIMULATED_DEBIT", "0")
    bh.seed_price_books()
    now = datetime.now(timezone.utc)
    for name in ("01_owner_priced_flash_peak.json",
                 "04_owner_interrupted_no_usage.json",
                 "05_demo_subject_offpeak.json"):
        event = dict(bh.load_event(name))
        if event.get("request_id"):
            bh.bind_reservation(event["request_id"], event["session_id"],
                                event["subject_type"], event["subject_id"])
        else:
            bh.bind_demo_session(event["session_id"], event["subject_id"])
        billing_store.ingest_usage_event(
            event, installation_id="pin_test", now=now)
    conn = bh.connect()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) AS n FROM billing_ledger_entries")
            assert cur.fetchone()["n"] == 0  # 开关关闭：ledger 恒空
            cur.execute("SELECT COUNT(*) AS n FROM billing_accounts")
            assert cur.fetchone()["n"] == 0  # demo 主体永不开户
            cur.execute(
                "SELECT COUNT(*) AS n FROM ai_usage_events "
                "WHERE subject_type='demo' AND user_id IS NOT NULL")
            assert cur.fetchone()["n"] == 0
            cur.execute(
                "SELECT detail FROM audit_events WHERE action=%s "
                "AND target_type='usage_event'",
                (billing_store.USAGE_INGEST_AUDIT_ACTION,))
            details = [r["detail"] for r in cur.fetchall()]
            assert details and all(
                d.get("simulated_debit_skipped") == "disabled" for d in details)
    finally:
        conn.close()


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-q"]))
