# -*- coding: utf-8 -*-
"""PR6 模拟软扣费测试（admin-billing 方案 §12.2 Phase B / §19 v0.4）。

json 模式（无 PG 也跑）：
  - ``BILLING_SIMULATED_DEBIT`` 开关解析：缺省启用；``0/false/off``（大小写
    不敏感、允许首尾空白）关闭。ingest 本身 PG-only，json/dual 无行为变化
    （pg_backend_required，见 test_usage_ingest.py / test_billing_store.py）。

PG 模式（RUN_PG_TESTS=1；conftest 每用例 TRUNCATE billing 表）：
  - priced user 事件 → 同事务自动开户 + 一条 usage_debit（负的 customer_charge、
    幂等键 ``usage:<event_id>``、metadata.simulated=true、actor NULL）+ 余额
    为负；
  - 同 event 重放 → duplicate、仍只一条 debit、audit 只一条；
  - owner 主体同样开户扣费；demo 主体永不开户无 debit（§14.1 红线回归）；
  - unpriced（未知 model）→ skipped=unpriced；开关关闭 → skipped=disabled；
    users 无行 → skipped=user_missing；charge=0 → skipped=zero_charge；
    账户 suspended → skipped=account_suspended；
  - 扣费段强制抛错（_SIM_DEBIT_HOOK）→ ingest 仍成功、事件入库、无 debit、
    audit skipped=failed、失败计数 +1（best-effort 纪律）；
  - ingest audit detail 经 admin v1 出口函数（_admin_v1_audit_event_out =
    sanitize + nano_out）金额为十进制字符串（嵌套 simulated_debit.amount_nano_cny
    命中既有白名单键）；
  - 两线程并发投递同 event → 一条事件一条 debit（唯一索引/幂等键兜底）。

运行：cd 项目根 && python3 -m pytest tests/test_billing_sim_debit.py -q
"""
import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import _bootstrap  # noqa: E402,F401  # session 目录+openslide stub（conftest 先行）

import pytest  # noqa: E402
import billing_store  # noqa: E402

from pg_compat import BACKEND  # noqa: E402

if BACKEND == "postgres":
    import _billing_helpers as bh  # noqa: E402
    import user_store  # noqa: E402


# =========================================================================== #
# 开关解析（json 模式也跑；env 每次现读，monkeypatch 即时生效）
# =========================================================================== #
def test_flag_default_and_enabled_values(monkeypatch):
    monkeypatch.delenv("BILLING_SIMULATED_DEBIT", raising=False)
    assert billing_store.simulated_debit_enabled() is True  # 缺省启用
    for raw in ("1", "true", "yes", "on", "TRUE", " enabled "):
        monkeypatch.setenv("BILLING_SIMULATED_DEBIT", raw)
        assert billing_store.simulated_debit_enabled() is True, raw


def test_flag_disabled_values(monkeypatch):
    for raw in ("0", "false", "off", "FALSE", " Off ", " 0 "):
        monkeypatch.setenv("BILLING_SIMULATED_DEBIT", raw)
        assert billing_store.simulated_debit_enabled() is False, raw


# =========================================================================== #
# PG：数据路径
# =========================================================================== #
def _fresh(event, hours_back=1):
    """夹具时间平移到相对当前时刻（test_billing_store 同款；只关心状态语义）。"""
    out = dict(event)
    occurred = datetime.now(timezone.utc) - timedelta(hours=hours_back)
    out["occurred_at"] = occurred.isoformat().replace("+00:00", "Z")
    out["enqueued_at"] = (occurred + timedelta(seconds=1)
                          ).isoformat().replace("+00:00", "Z")
    return out


def _user_event(name, user, *, role=None):
    """装一条主体为真实注册用户的 priced 事件（reservation 权威绑定）。"""
    event = _fresh(bh.load_event(name))
    subject_type = role or event["subject_type"]
    event["subject_type"] = subject_type
    event["subject_id"] = user["user_id"]
    event["user_id"] = user["user_id"]
    bh.bind_reservation(event["request_id"], event["session_id"],
                        subject_type, user["user_id"])
    return event


def _ingest(event, installation="pin_sim", **kwargs):
    return billing_store.ingest_usage_event(
        event, installation_id=installation, **kwargs)


def _ingest_audit_detail(event_id):
    conn = bh.connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT detail FROM audit_events WHERE target_id=%s "
                "AND action=%s",
                (event_id, billing_store.USAGE_INGEST_AUDIT_ACTION))
            rows = [r["detail"] for r in cur.fetchall()]
    finally:
        conn.close()
    assert len(rows) == 1, "ingest audit 应恰好一条（duplicate 不重复写）"
    return rows[0]


def _debit_row(event_id):
    conn = bh.connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT entry_id, account_id, kind, amount_nano_cny, "
                "idempotency_key, reason, actor_user_id, metadata "
                "FROM billing_ledger_entries WHERE event_id=%s", (event_id,))
            rows = [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()
    return rows


def _count(table):
    conn = bh.connect()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) AS n FROM %s" % table)
            return int(cur.fetchone()["n"])
    finally:
        conn.close()


def test_priced_user_auto_account_and_debit():
    """priced user 事件：自动开户 + 一条负金额 usage_debit + 余额为负。"""
    bh.seed_price_books_with_history()
    user = user_store.create_user("simdeb-u1@x.com", "pass123456789012")
    event = _user_event("02_user_priced_pro_offpeak_reasoning.json", user)
    result = _ingest(event)
    assert result["status"] == "priced" and result["duplicate"] is False
    charge = result["row"]["charge_nano_cny"]
    assert charge > 0

    acct = billing_store.get_billing_account_by_user(user["user_id"])
    assert acct is not None, "priced user 应自动开户"
    assert acct["currency"] == "CNY" and acct["status"] == "active"
    assert acct["account_id"].startswith("bac_") \
        and len(acct["account_id"]) == len("bac_") + 24

    rows = _debit_row(event["event_id"])
    assert len(rows) == 1
    row = rows[0]
    assert row["account_id"] == acct["account_id"]
    assert row["kind"] == "usage_debit"
    assert row["amount_nano_cny"] == -charge  # 负的 customer_charge
    assert row["idempotency_key"] == "usage:%s" % event["event_id"]
    assert row["actor_user_id"] is None  # 系统行为，非人工操作
    assert row["reason"]  # §6.5 reason 必填非空
    meta = row["metadata"]
    assert meta["simulated"] is True
    assert meta["model"] == event["model"]
    assert meta["total_tokens"] == event["total_tokens"]
    assert meta["session_id"] == event["session_id"]
    assert meta["charge_price_book_id"] == result["row"]["charge_price_book_id"]

    # 余额 = ledger 合计（无余额列）；模拟期为负正是要观察的数据
    assert billing_store.account_balance_nano(acct["account_id"]) == -charge

    # audit detail 并入扣费结果（同一事务）
    detail = _ingest_audit_detail(event["event_id"])
    assert detail["simulated_debit"] == {
        "entry_id": row["entry_id"],
        "amount_nano_cny": -charge,
        "duplicate": False,
    }

    # admin ledger 页输出 metadata（插件 UI「模拟」徽标的数据源）
    page = billing_store.admin_ledger_page(limit=10)
    item = next(i for i in page["items"] if i["entry_id"] == row["entry_id"])
    assert item["metadata"]["simulated"] is True
    assert item["amount_nano_cny"] == -charge


def test_replay_same_event_single_debit():
    bh.seed_price_books_with_history()
    user = user_store.create_user("simdeb-u2@x.com", "pass123456789012")
    event = _user_event("03_user_priced_vision_exp_peak.json", user)
    first = _ingest(event)
    assert first["duplicate"] is False
    # 重放不重新解析主体/计价/扣费（dedup 提前返回原行）
    replay = _ingest(event)
    assert replay["duplicate"] is True
    assert replay["status"] == first["status"]
    assert len(_debit_row(event["event_id"])) == 1, "重放不得重复扣"
    conn = bh.connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) AS n FROM billing_ledger_entries "
                "WHERE account_id=(SELECT account_id FROM billing_accounts "
                "WHERE user_id=%s)", (user["user_id"],))
            assert int(cur.fetchone()["n"]) == 1
    finally:
        conn.close()
    # audit 只一条（duplicate 路径不重复写 audit）
    _ingest_audit_detail(event["event_id"])


def test_owner_subject_debited():
    bh.seed_price_books_with_history()
    owner = user_store.create_user("simdeb-owner@x.com", "pass123456789012",
                                   role="owner")
    event = _user_event("01_owner_priced_flash_peak.json", owner, role="owner")
    result = _ingest(event)
    assert result["status"] == "priced"
    acct = billing_store.get_billing_account_by_user(owner["user_id"])
    assert acct is not None, "owner 主体同样开户扣费"
    rows = _debit_row(event["event_id"])
    assert len(rows) == 1
    assert rows[0]["amount_nano_cny"] == -result["row"]["charge_nano_cny"]
    assert billing_store.account_balance_nano(acct["account_id"]) \
        == -result["row"]["charge_nano_cny"]


def test_demo_subject_never_debited():
    """§14.1 红线回归：demo 主体 priced 也不开户、不写 ledger。"""
    bh.seed_price_books_with_history()
    event = _fresh(bh.load_event("05_demo_subject_offpeak.json"))
    bh.bind_demo_session(event["session_id"], event["subject_id"])
    result = _ingest(event)
    assert result["status"] == "priced"  # 只计量
    assert _count("billing_accounts") == 0, "demo 主体永不开户"
    assert _count("billing_ledger_entries") == 0, "demo 主体永不扣账"
    detail = _ingest_audit_detail(event["event_id"])
    assert detail["simulated_debit_skipped"] == "demo_subject"


def test_unpriced_event_no_debit():
    bh.seed_price_books_with_history()
    user = user_store.create_user("simdeb-u3@x.com", "pass123456789012")
    event = _user_event("02_user_priced_pro_offpeak_reasoning.json", user)
    event["model"] = "deepseek-v9-unknown"  # 无价格行 → unpriced
    result = _ingest(event)
    assert result["status"] == "unpriced"
    assert result["row"]["unpriced_reason"] == "no_active_price_book"
    assert _count("billing_ledger_entries") == 0
    assert _count("billing_accounts") == 0
    detail = _ingest_audit_detail(event["event_id"])
    assert detail["simulated_debit_skipped"] == "unpriced"


def test_disabled_flag_skips_debit(monkeypatch):
    monkeypatch.setenv("BILLING_SIMULATED_DEBIT", "0")
    bh.seed_price_books_with_history()
    user = user_store.create_user("simdeb-u4@x.com", "pass123456789012")
    event = _user_event("02_user_priced_pro_offpeak_reasoning.json", user)
    result = _ingest(event)
    assert result["status"] == "priced"  # 计量照常，只是不扣
    assert _count("billing_ledger_entries") == 0
    assert _count("billing_accounts") == 0
    detail = _ingest_audit_detail(event["event_id"])
    assert detail["simulated_debit_skipped"] == "disabled"


def test_user_row_missing_skips_debit():
    """owner/user 主体在 users 无行：不伪造用户行也不开户（FK 保护）。"""
    bh.seed_price_books_with_history()
    event = _fresh(bh.load_event("06_user_priced_flash_no_provider_request_id.json"))
    synthetic = "usr_synthetic00ff00ff00ff00ff"
    event["subject_id"] = synthetic
    event["user_id"] = synthetic
    bh.bind_reservation(event["request_id"], event["session_id"],
                        "user", synthetic)
    result = _ingest(event)
    assert result["status"] == "priced"
    assert result["row"]["user_id"] is None  # 镜像列 NULL（users 无行）
    assert _count("billing_accounts") == 0
    assert _count("billing_ledger_entries") == 0
    detail = _ingest_audit_detail(event["event_id"])
    assert detail["simulated_debit_skipped"] == "user_missing"


def test_zero_charge_skips_debit():
    """全 0 token 的 priced 事件 charge=0：usage_debit 符号 CHECK 要求严格
    负数，0 元不入账（≠ 0 元冒充扣费）。"""
    bh.seed_price_books_with_history()
    user = user_store.create_user("simdeb-u5@x.com", "pass123456789012")
    event = _user_event("02_user_priced_pro_offpeak_reasoning.json", user)
    for key in ("cache_hit_input_tokens", "cache_miss_input_tokens",
                "output_tokens", "reasoning_tokens", "total_tokens"):
        event[key] = 0
    result = _ingest(event)
    assert result["status"] == "priced"
    assert result["row"]["charge_nano_cny"] == 0
    assert _count("billing_ledger_entries") == 0
    detail = _ingest_audit_detail(event["event_id"])
    assert detail["simulated_debit_skipped"] == "zero_charge"


def test_suspended_account_skips_debit():
    bh.seed_price_books_with_history()
    user = user_store.create_user("simdeb-u6@x.com", "pass123456789012")
    acct = billing_store.create_billing_account(user["user_id"])
    conn = bh.connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE billing_accounts SET status='suspended' "
                "WHERE account_id=%s", (acct["account_id"],))
        conn.commit()
    finally:
        conn.close()
    event = _user_event("02_user_priced_pro_offpeak_reasoning.json", user)
    result = _ingest(event)
    assert result["status"] == "priced"
    assert _count("billing_ledger_entries") == 0
    detail = _ingest_audit_detail(event["event_id"])
    assert detail["simulated_debit_skipped"] == "account_suspended"


def test_debit_failure_does_not_block_ingest(monkeypatch, caplog):
    """best-effort 纪律：扣费段抛错 → SAVEPOINT 回滚，事件照常入库。"""
    bh.seed_price_books_with_history()
    user = user_store.create_user("simdeb-u7@x.com", "pass123456789012")
    event = _user_event("02_user_priced_pro_offpeak_reasoning.json", user)

    def _boom(cur):  # noqa: U100
        raise RuntimeError("sim-debit injected failure")
    monkeypatch.setattr(billing_store, "_SIM_DEBIT_HOOK", _boom)

    before = billing_store._SIM_DEBIT_FAILED_TOTAL
    with caplog.at_level("WARNING", logger="billing_store"):
        result = _ingest(event)  # 不得抛错（路由层即 200）
    assert result["status"] == "priced" and result["duplicate"] is False
    assert billing_store.get_usage_event(event["event_id"]) is not None
    assert _count("billing_ledger_entries") == 0, "失败段已回滚"
    assert _count("billing_accounts") == 0, "开户同在 savepoint 内一并回滚"
    detail = _ingest_audit_detail(event["event_id"])
    assert detail["simulated_debit_skipped"] == "failed"
    assert billing_store._SIM_DEBIT_FAILED_TOTAL == before + 1
    # 无敏感 warning（只含 event_id/错误类别）+ 指标日志行（仿 HP outbox 风格）
    msgs = [r.getMessage() for r in caplog.records]
    warn = next(m for m in msgs if "模拟扣费失败" in m)
    assert event["event_id"] in warn and "RuntimeError" in warn
    assert "sim-debit injected failure" not in warn, "异常消息不落日志"
    metric = next(m for m in msgs if '"metric"' in m)
    assert ('[billing-sim-debit] {"metric":"billing_sim_debit_failed_total",'
            '"value":%d}' % (before + 1)) == metric


def test_ingest_audit_wire_amount_decimal_string():
    """admin v1 出口：simulated_debit.amount_nano_cny 为十进制字符串。

    经 app 层出口函数 _admin_v1_audit_event_out（sanitize_audit_detail +
    nano_out）验证嵌套对象命中既有白名单键 amount_nano_cny（§5 v0.3 P2）。
    """
    import app as app_mod
    import share_store
    bh.seed_price_books_with_history()
    user = user_store.create_user("simdeb-u8@x.com", "pass123456789012")
    event = _user_event("02_user_priced_pro_offpeak_reasoning.json", user)
    result = _ingest(event)
    charge = result["row"]["charge_nano_cny"]

    events = share_store.list_audit(
        action=billing_store.USAGE_INGEST_AUDIT_ACTION)
    assert events, "应写入 ingest audit"
    out = app_mod._admin_v1_audit_event_out(events[0])
    frag = out["detail"]["simulated_debit"]
    assert isinstance(frag["amount_nano_cny"], str), "wire 禁 JSON number"
    assert frag["amount_nano_cny"] == str(-charge)
    assert isinstance(frag["duplicate"], bool)
    assert isinstance(frag["entry_id"], str)
    # 余额出口同口径：负余额十进制字符串（balance_nano 在既有白名单内）
    acct = billing_store.get_billing_account_by_user(user["user_id"])
    wired = app_mod._admin_v1_nano_out(
        {"balance_nano": billing_store.account_balance_nano(
            acct["account_id"])})
    assert wired["balance_nano"] == str(-charge)
    assert wired["balance_nano"].startswith("-")


def test_concurrent_delivery_single_debit():
    """两线程并发投递同 event：一条事件一条 debit（唯一索引/幂等键兜底）。"""
    from concurrent.futures import ThreadPoolExecutor
    import threading
    bh.seed_price_books_with_history()
    user = user_store.create_user("simdeb-u9@x.com", "pass123456789012")
    event = _user_event("02_user_priced_pro_offpeak_reasoning.json", user)
    barrier = threading.Barrier(2)

    def _deliver():
        barrier.wait()
        return _ingest(event)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: _deliver(), range(2)))
    statuses = sorted(r["duplicate"] for r in results)
    assert statuses == [False, True], "恰好一次入库一次重放：%r" % results
    assert all(r["status"] == "priced" for r in results)
    assert _count("ai_usage_events") == 1
    rows = _debit_row(event["event_id"])
    assert len(rows) == 1, "并发投递不得重复扣"
    acct = billing_store.get_billing_account_by_user(user["user_id"])
    assert billing_store.account_balance_nano(acct["account_id"]) \
        == rows[0]["amount_nano_cny"] < 0


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
