# -*- coding: utf-8 -*-
"""二次评审（2026-10-23）P2 回归：状态端点必须显式报告查询置信度。

来源：``/mnt/ssd2/tests/re-review-mac-pay-20261023-SIpZHT/test_followup_repros.py``
——用户的离线复现脚本。复现脚本的 mock **就是契约**：查单网关超时
（``TradeOperationUncertainError`` 被 ``_support_apply_verified_query`` 吞掉）
后端点仍回 HTTP 200 ``{"status": "paying"}``，桌面支付弹层把它当「查询成功」，
QR 截止后展示「已过期 + 重新生成」——已付款买家可能重复支付。脚手架逐字
保留，只把 bug 断言**反转**为修复后的契约：created/paying 响应恒带
``confirmed``/``expired`` 两键，本地 paying + 未确认绝不等于「未支付」。

**相对评审脚本的脚手架增补/改动（逐项列出）**：

- ``test_gateway_timeout_...``（原 ``test_gateway_timeout_returns_http_200_paying``，
  函数名加后缀 ``_unconfirmed``）：``_make_order``/``_own_session``、timeout
  fake（``sp.TradeOperationUncertainError('simulated gateway timeout')``）、
  GET 与 ``assert reply.status_code == 200``（200 仍是契约）逐字保留；
  唯一反转断言 ``reply.get_json() == {'status': 'paying'}`` →
  ``{'status': 'paying', 'confirmed': False, 'expired': False}``；评审经
  ``TEST_RUN_DIR/gateway-timeout-status.json`` 交给 UI 复现的落盘交接，改为
  与跨层契约文件 ``tests/fixtures/support/status_query_failed.json``（JS 回归
  测试加载**同一**文件）断言相等；``print`` 删除；函数内 ``import os`` 随之
  删除（唯一用途是被替换的 TEST_RUN_DIR 落盘），``json`` / ``Path`` 上移到
  模块级（另被契约文件断言共用）。
- 扩展测试（1–9）全部走端点（除纯函数组），复用 ``tests/test_support_payments.py``
  既有基建：``_iso``/``rt`` fixtures、``_make_order``/_own_session/_client/
  _mock_query、``_backdate`` 同款 SQL 直改订单行；唯一新增基建是本模块内
  小助手 ``_set_row``（列名 → SQL 表达式，折叠 ``_backdate`` 的连接样板），
  无其他新代码。

运行：cd 项目根 && python3 -m pytest tests/test_support_rereview_20261023.py -q
"""
import datetime
import json
from pathlib import Path

import pytest

import pg_store
import support_payments as sp
import test_support_payments as helpers

pytest_plugins = ['test_support_payments']


def _set_row(out_trade_no, **columns):
    """SQL 直改订单行（``helpers._backdate`` 同款写法；值为测试内写死的
    SQL 表达式，如 ``"now() - interval '905 seconds'"`` / ``"'status:PENDING'"``）。"""
    conn = pg_store.connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE support_orders SET %s WHERE out_trade_no=%%s"
                % ", ".join("%s = %s" % (name, expr)
                            for name, expr in columns.items()),
                (out_trade_no,))
        conn.commit()
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# 评审原复现（脚手架逐字保留，仅反转 bug 断言）
# --------------------------------------------------------------------------- #
def test_gateway_timeout_returns_http_200_paying_unconfirmed(
        _iso, rt, monkeypatch):
    """原评审名：``test_gateway_timeout_returns_http_200_paying``（复现 bug
    的断言已反转；HTTP 200 保持不变）。查单网关超时 → 响应必须显式「结果
    未知」（confirmed/expired 双 False），且与跨层契约 fixture 相等（JS 回归
    测试加载同一文件）——弹层绝不能把「未知」当「查询成功」推进到
    「已过期 + 重新生成」（买家可能已付款）。"""
    order = helpers._make_order(rt)
    client = helpers._own_session(helpers._client(), order['out_trade_no'])
    def timeout(*args):
        raise sp.TradeOperationUncertainError('simulated gateway timeout')
    monkeypatch.setattr(sp, 'query_trade', timeout)
    reply = client.get('/support/orders/' + order['out_trade_no'] + '/status')
    assert reply.status_code == 200
    # 反转后的安全断言：本地 paying 未被支付宝确认 → 显式置信度键。
    assert reply.get_json() == {'status': 'paying', 'confirmed': False,
                                'expired': False}
    # 跨层契约：与前端回归测试共用同一 fixture（原为 TEST_RUN_DIR 落盘交接）。
    assert reply.get_json() == json.loads(
        (Path(__file__).parent / 'fixtures' / 'support'
         / 'status_query_failed.json').read_text(encoding='utf-8'))


# --------------------------------------------------------------------------- #
# 扩展 1：成功查单 → confirmed=True
# --------------------------------------------------------------------------- #
def test_successful_pending_query_confirms(_iso, rt, monkeypatch):
    """扩展 1a：成功 PENDING 查单 → confirmed=True、expired=False。"""
    order = helpers._make_order(rt)
    c = helpers._own_session(helpers._client(), order['out_trade_no'])
    helpers._mock_query(monkeypatch, state='PENDING', amount='6.66')
    reply = c.get('/support/orders/%s/status' % order['out_trade_no'])
    assert reply.status_code == 200
    assert reply.get_json() == {'status': 'paying', 'confirmed': True,
                                'expired': False}


def test_successful_not_exist_confirms_created_order(_iso, rt, monkeypatch):
    """扩展 1b：``created`` 订单查得 NOT_EXIST（交易从未提交）→ 同样
    confirmed=True（PENDING/NOT_EXIST 都是「未付且支付宝可证明」）。"""
    order = helpers._make_order(rt, status='created')
    c = helpers._own_session(helpers._client(), order['out_trade_no'])
    helpers._mock_query(monkeypatch, state='NOT_EXIST')
    reply = c.get('/support/orders/%s/status' % order['out_trade_no'])
    assert reply.status_code == 200
    assert reply.get_json() == {'status': 'created', 'confirmed': True,
                                'expired': False}


# --------------------------------------------------------------------------- #
# 扩展 2：节流轮询复用窗口内的确认（弹层 0:00 终检通常落在 3s 节流内）
# --------------------------------------------------------------------------- #
def test_throttled_poll_reuses_fresh_confirmation(_iso, rt, monkeypatch):
    """扩展 2：窗口内第二次轮询命中节流（只查一次）→ 复用 stored 成功查单，
    仍 confirmed=True。"""
    order = helpers._make_order(rt)
    c = helpers._own_session(helpers._client(), order['out_trade_no'])
    calls = helpers._mock_query(monkeypatch, state='PENDING', amount='6.66')
    first = c.get('/support/orders/%s/status' % order['out_trade_no'])
    assert first.get_json() == {'status': 'paying', 'confirmed': True,
                                'expired': False}
    second = c.get('/support/orders/%s/status' % order['out_trade_no'])
    assert len(calls) == 1  # 第二次命中 3s 节流，不再查询
    assert second.get_json() == {'status': 'paying', 'confirmed': True,
                                 'expired': False}


# --------------------------------------------------------------------------- #
# 扩展 3：stored 成功已出窗 + 本次查询失败 → 不确认
# --------------------------------------------------------------------------- #
def test_stale_stored_success_with_failing_query_not_confirmed(
        _iso, rt, monkeypatch):
    """扩展 3：stored 成功查单已过置信窗口（10s 前）且本次查询抛网关超时 →
    查询真正发起、失败不落库 → confirmed=False（旧行为会回裸 paying）。"""
    order = helpers._make_order(rt)
    _set_row(order['out_trade_no'],
             last_query_at="now() - interval '10 seconds'",
             last_query_result="'status:PENDING'")
    c = helpers._own_session(helpers._client(), order['out_trade_no'])
    def timeout(*args):
        raise sp.TradeOperationUncertainError('simulated gateway timeout')
    monkeypatch.setattr(sp, 'query_trade', timeout)
    reply = c.get('/support/orders/%s/status' % order['out_trade_no'])
    assert reply.get_json() == {'status': 'paying', 'confirmed': False,
                                'expired': False}


# --------------------------------------------------------------------------- #
# 扩展 4：明确失败 / UNKNOWN / 金额不符的 PAID → 都不确认
# --------------------------------------------------------------------------- #
def test_definite_system_error_not_confirmed(_iso, rt, monkeypatch):
    """扩展 4a：支付宝明确业务失败（ACQ.SYSTEM_ERROR）→ 查询失败不落库 →
    confirmed=False。"""
    order = helpers._make_order(rt)
    c = helpers._own_session(helpers._client(), order['out_trade_no'])
    def system_error(*args):
        exc = sp.TradeOperationError('simulated alipay system error')
        exc.sub_code = 'ACQ.SYSTEM_ERROR'
        raise exc
    monkeypatch.setattr(sp, 'query_trade', system_error)
    reply = c.get('/support/orders/%s/status' % order['out_trade_no'])
    assert reply.get_json() == {'status': 'paying', 'confirmed': False,
                                'expired': False}


def test_unknown_state_not_confirmed(_iso, rt, monkeypatch):
    """扩展 4b：查单成功但 UNKNOWN → 留痕 ``status:UNKNOWN``，STATE 不在
    PENDING/NOT_EXIST → confirmed=False。"""
    order = helpers._make_order(rt)
    c = helpers._own_session(helpers._client(), order['out_trade_no'])
    helpers._mock_query(monkeypatch, state='UNKNOWN')
    reply = c.get('/support/orders/%s/status' % order['out_trade_no'])
    assert reply.get_json() == {'status': 'paying', 'confirmed': False,
                                'expired': False}


def test_paid_with_mismatched_amount_stays_paying_unconfirmed(
        _iso, rt, monkeypatch):
    """扩展 4c：PAID 但金额不符 → 不认账（无 transition）、confirmed=False
    （确证「未确认 ≠ 未支付」正是评审要防的双付路径）。"""
    order = helpers._make_order(rt, amount_fen=666)
    c = helpers._own_session(helpers._client(), order['out_trade_no'])
    helpers._mock_query(monkeypatch, state='PAID', amount='9.99',
                        trade_no='trd-mismatch')
    reply = c.get('/support/orders/%s/status' % order['out_trade_no'])
    assert reply.get_json() == {'status': 'paying', 'confirmed': False,
                                'expired': False}
    assert sp.get_order(order['out_trade_no'])['status'] == 'paying'


# --------------------------------------------------------------------------- #
# 扩展 5：归属不匹配 → 纵有新鲜 stored 成功也不确认，且绝不查单
# --------------------------------------------------------------------------- #
def test_runtime_mismatch_fresh_stored_success_not_confirmed_no_query(
        _iso, rt, monkeypatch):
    """扩展 5：订单/配置归属不匹配 → 即使 stored 成功查单仍在窗口内也
    confirmed=False，且本请求绝不查单（setup 复制
    ``test_status_endpoint_runtime_mismatch_no_query``）。"""
    order = helpers._make_order(rt)
    _set_row(order['out_trade_no'], last_query_at="now()",
             last_query_result="'status:PENDING'")
    prod = sp.RuntimeConfig("production", sp.GATEWAY_PRODUCTION,
                            "2026000000000001", "2088000000000001",
                            rt.app_private_key, rt.alipay_public_key)
    monkeypatch.setattr(helpers.app_mod, "SUPPORT_ALIPAY_PAY_MODE",
                        "production")
    monkeypatch.setattr(sp, "_RUNTIME_CACHE", {"production": prod})
    calls = helpers._mock_query(monkeypatch, state='PENDING', amount='6.66')
    c = helpers._own_session(helpers._client(), order['out_trade_no'])
    reply = c.get('/support/orders/%s/status' % order['out_trade_no'])
    assert reply.status_code == 200
    assert reply.get_json() == {'status': 'paying', 'confirmed': False,
                                'expired': False}
    assert calls == []  # 他方配置订单绝不触发查单


# --------------------------------------------------------------------------- #
# 扩展 6–8：expired 只认「确认查单时刻 vs time_expire + 宽限」（服务端时钟）
# --------------------------------------------------------------------------- #
def test_confirmed_queries_past_grace_line_expired(_iso, rt, monkeypatch):
    """扩展 6：created_at 16 分钟前 + 本次成功 PENDING / NOT_EXIST → 确认
    查单已越过 created + 15m + 10s 宽限线 → confirmed=True、expired=True。"""
    order = helpers._make_order(rt)
    _set_row(order['out_trade_no'],
             created_at="now() - interval '960 seconds'")
    c = helpers._own_session(helpers._client(), order['out_trade_no'])
    helpers._mock_query(monkeypatch, state='PENDING', amount='6.66')
    reply = c.get('/support/orders/%s/status' % order['out_trade_no'])
    assert reply.get_json() == {'status': 'paying', 'confirmed': True,
                                'expired': True}
    order2 = helpers._make_order(rt)
    _set_row(order2['out_trade_no'],
             created_at="now() - interval '960 seconds'")
    c2 = helpers._own_session(helpers._client(), order2['out_trade_no'])
    helpers._mock_query(monkeypatch, state='NOT_EXIST')
    reply2 = c2.get('/support/orders/%s/status' % order2['out_trade_no'])
    assert reply2.get_json() == {'status': 'paying', 'confirmed': True,
                                 'expired': True}


def test_inside_grace_line_not_expired(_iso, rt, monkeypatch):
    """扩展 7：created_at = now - (15m + 5s) + 本次成功 PENDING → 查单时刻
    （now）在宽限线（created + 15m + 10s = now + 5s）之前 → expired=False。"""
    order = helpers._make_order(rt)
    _set_row(order['out_trade_no'],
             created_at="now() - interval '905 seconds'")
    c = helpers._own_session(helpers._client(), order['out_trade_no'])
    helpers._mock_query(monkeypatch, state='PENDING', amount='6.66')
    reply = c.get('/support/orders/%s/status' % order['out_trade_no'])
    assert reply.get_json() == {'status': 'paying', 'confirmed': True,
                                'expired': False}


def test_throttled_confirmation_recorded_before_grace_line(
        _iso, rt, monkeypatch):
    """扩展 8：节流复用的确认是宽限线**之前**落库的（created = now-911s，
    last_query_at = now-2s < created+910s = now-1s）→ 命中节流不查单，
    复用该确认：confirmed=True、expired=False（宽限吸收 time_expire 前一刻
    提交、仍在处理中的支付）。"""
    order = helpers._make_order(rt)
    _set_row(order['out_trade_no'],
             created_at="now() - interval '911 seconds'",
             last_query_at="now() - interval '2 seconds'",
             last_query_result="'status:PENDING'")
    c = helpers._own_session(helpers._client(), order['out_trade_no'])
    calls = helpers._mock_query(monkeypatch, state='PENDING', amount='6.66')
    reply = c.get('/support/orders/%s/status' % order['out_trade_no'])
    assert calls == []  # 命中 3s 节流，本请求不查单
    assert reply.get_json() == {'status': 'paying', 'confirmed': True,
                                'expired': False}


# --------------------------------------------------------------------------- #
# 扩展 9：status_confirmation 纯函数边界（显式 now，无 DB）
# --------------------------------------------------------------------------- #
def test_status_confirmation_pure_boundaries():
    """扩展 9：``sp.status_confirmation`` 纯函数窗口/形态/宽限线边界。"""
    base = datetime.datetime(2026, 10, 23, 12, 0, 0,
                             tzinfo=datetime.timezone.utc)

    def row(status="paying", created_at=base, last_query_at=base,
            last_query_result="status:PENDING"):
        return {"status": status, "created_at": created_at,
                "last_query_at": last_query_at,
                "last_query_result": last_query_result}

    one_second = datetime.timedelta(seconds=1)
    # 恰好 3s 旧 → 仍在窗口内；3.001s → 出窗（不确认）。
    assert sp.status_confirmation(
        row(last_query_at=base + one_second),
        now=base + one_second + datetime.timedelta(seconds=3)) == (True, False)
    assert sp.status_confirmation(
        row(last_query_at=base + one_second),
        now=base + one_second + datetime.timedelta(seconds=3, milliseconds=1)) \
        == (False, False)
    # last_query_at 在未来超过 3s → abs() 防御 → 不确认。
    assert sp.status_confirmation(
        row(last_query_at=base + datetime.timedelta(seconds=4)),
        now=base) == (False, False)
    # last_query_result 缺失/空/非 "<via>:<STATE>" 写入方形态 → (False, False)。
    for bad in (None, "", "   ", "PENDING", "status:"):
        assert sp.status_confirmation(
            row(last_query_result=bad),
            now=base + one_second) == (False, False), bad
    # 终态（paid/closed）不是 created/paying → (False, False)。
    assert sp.status_confirmation(row(status="paid")) == (False, False)
    assert sp.status_confirmation(row(status="closed")) == (False, False)
    # expired 边界：确认查单恰在 created + 15m + 10s（含相等）→ True；
    # 差 1ms 不到线 → False。
    line = base + sp.ORDER_TIMEOUT + datetime.timedelta(
        seconds=sp.STATUS_EXPIRED_GRACE_SECONDS)
    assert sp.status_confirmation(row(last_query_at=line),
                                  now=line + one_second) == (True, True)
    assert sp.status_confirmation(
        row(last_query_at=line - datetime.timedelta(milliseconds=1)),
        now=line + one_second) == (True, False)
