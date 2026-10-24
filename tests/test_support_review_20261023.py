# -*- coding: utf-8 -*-
"""用户独立评审（2026-10-23）发现问题的回归测试。

来源：``/mnt/ssd2/tests/review-mac-pay-20261023-sw5H5x/test_review_refund_crash.py``
——用户的离线复现脚本。复现脚本的 mock **就是契约**，脚手架（fixture、fake、
monkeypatch）逐字保留，只把断言**反转**为修复后的安全行为：

1. [P1] ``failed`` 退款行的显式重试必须在调用支付宝**之前**把行推进到
   ``requested``（预留额度）并提交——进程在 ``update_refund_status`` 前死掉
   时，本地账本也必须是「未决退款」而不是「已释放」；随后（a）同号新 POST
   经退款查询确认成功 → 返回**原行**（绝不生成新请求号）；（b）查询失败 →
   409 ``refund_unresolved`` 阻断且绝不重复受理；（c）明确业务拒绝 → 行回
   ``failed`` 释放预留。
2. [P2] 支付宝 ``return_url`` 不得固定用 canonical host（PUBLIC_BASE_URL）：
   会话 Cookie 是 host-only，从 histopilot.com 发起的支付必须回到
   histopilot.com；return_url origin 只允许来自请求 Host 经固定入口白名单
   （``registration_antibot.entry_site_for_host``）的映射，未知 Host 一律
   回退 canonical，绝不回显。

**相对评审脚本的脚手架增补（逐项列出）**：

- Test A（原 ``test_failed_retry_crash_leaves_external_refund_unreserved``）：
  脚手架逐字保留到 ``assert external == [(request_no, 300)]``；增补
  ``monkeypatch.setattr(sp, 'query_refund', ... REFUND_SUCCESS ...)``——
  修复后的代码在崩溃后的新 POST 里会先走同号退款查询解析未决行，真实
  ``query_refund`` 会出网；评审设定「支付宝在崩溃前已受理该重试」，故应答
  REFUND_SUCCESS。
- Test B（原 ``test_return_url_changes_host_and_loses_order_session``）：
  脚手架**逐字内联**、保持评审脚本原顺序（含 ``import app as app_mod``、
  setenv 在建单之前）；唯一改动是那一条反转断言；末尾增补同 host（.com）+
  同会话的回跳 GET，用 ``helpers._mock_query(state="PENDING")`` 打桩查单——
  验证订单经会话找到、渲染待确认态（评审脚本没有这一跟进，因为旧代码下
  该 GET 根本到不了查单）。
- 扩展 A2/A3/A4/B2/B3 为同一契约的补充场景：A2 查询失败阻断、A3 明确拒绝
  释放预留、A4 预留 CAS 不中（并发状态变化）→ 409 refund_state_changed 且
  绝不发支付宝、B2 QR 内嵌页、B3 白名单/未知 Host；脚手架全部来自
  ``tests/test_support_payments.py`` / R2 既有用法，无新增基建；B3/未知
  Host 经 ``_pay_page_capturing_return_url`` 折叠同一段脚手架（仅扩展用）。

运行：cd 项目根 && python3 -m pytest tests/test_support_review_20261023.py -q
"""
import pytest
import support_payments as sp
import test_support_payments as helpers
import pg_store

pytest_plugins = ['test_support_payments']

class SimulatedWorkerDeath(BaseException):
    pass


def _refund_count(out_trade_no):
    conn = pg_store.connect()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM support_refunds"
                        " WHERE out_trade_no=%s", (out_trade_no,))
            return cur.fetchone()[0]
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# Finding 1 [P1]：failed 行显式重试——先预留（requested + commit）再碰支付宝
# --------------------------------------------------------------------------- #
def test_failed_retry_crash_leaves_refund_reserved(_iso, rt, monkeypatch):
    """原评审名：``test_failed_retry_crash_leaves_external_refund_unreserved``
    （复现 bug 的断言已反转）。崩溃前支付宝已受理 → 行必须留在 ``requested``
    （额度预留、未决），同号新 POST 经查询确认后返回原行，绝不产生新请求号。"""
    order = helpers._paid_order(rt)
    client, _owner = helpers._admin_client()
    failed = sp.create_refund_row(order['out_trade_no'], 300, 'review')
    request_no = failed['out_request_no']
    sp.update_refund_status(request_no, 'failed')
    external = []
    def accepted_then_crashed(runtime, current_order, amount, number, reason=None):
        external.append((number, amount))
        raise SimulatedWorkerDeath()
    monkeypatch.setattr(sp, 'refund_trade', accepted_then_crashed)
    endpoint = '/api/admin/v1/support-payments/' + order['out_trade_no'] + '/refund'
    with pytest.raises(SimulatedWorkerDeath):
        client.post(endpoint, json={'amount_yuan': '3.00', 'out_request_no': request_no})
    assert external == [(request_no, 300)]
    # 反转后的安全断言：重试在触碰支付宝前已把行推进 requested 并提交——
    # 崩溃后本地账本是「未决/预留」而不是「已释放」。
    assert sp.get_refund(request_no)['status'] == 'requested'
    assert sp.sum_refunded_fen(order['out_trade_no']) == 300
    assert [r['out_request_no']
            for r in sp.list_pending_refunds(order['out_trade_no'])] == [request_no]
    monkeypatch.setattr(sp, 'refund_trade', lambda *args: {'fund_change': 'Y', 'trade_no': 'review'})
    # 脚手架增补：崩溃后的新 POST 先走同号退款查询（评审设定：崩溃前支付宝
    # 已受理）；真实 query_refund 会出网，必须打桩。
    monkeypatch.setattr(sp, 'query_refund', lambda *args: {'refund_status': 'REFUND_SUCCESS', 'refund_amount': '3.00', 'trade_no': 'review'})
    result = client.post(endpoint, json={'amount_yuan': '3.00'})
    assert result.status_code == 200
    assert result.get_json()['refund']['out_request_no'] == request_no
    assert result.get_json()['refund']['status'] == 'refunded'
    assert sp.sum_refunded_fen(order['out_trade_no']) == 300
    assert _refund_count(order['out_trade_no']) == 1


def test_failed_retry_crash_then_query_failure_blocks_new_refund(_iso, rt, monkeypatch):
    """扩展（A2）：同样的崩溃后，退款查询失败 → 新退款 POST 409
    ``refund_unresolved``；``refund_trade` 绝不被再次调用；不产生新行。"""
    order = helpers._paid_order(rt)
    client, _owner = helpers._admin_client()
    failed = sp.create_refund_row(order['out_trade_no'], 300, 'review')
    request_no = failed['out_request_no']
    sp.update_refund_status(request_no, 'failed')
    calls = []
    def accepted_then_crashed(runtime, current_order, amount, number, reason=None):
        calls.append(number)
        raise SimulatedWorkerDeath()
    monkeypatch.setattr(sp, 'refund_trade', accepted_then_crashed)
    endpoint = '/api/admin/v1/support-payments/' + order['out_trade_no'] + '/refund'
    with pytest.raises(SimulatedWorkerDeath):
        client.post(endpoint, json={'amount_yuan': '3.00', 'out_request_no': request_no})
    assert sp.get_refund(request_no)['status'] == 'requested'
    monkeypatch.setattr(sp, 'refund_trade',
                        lambda runtime, order, amount, number, reason=None:
                        calls.append(number)
                        or {'fund_change': 'Y', 'trade_no': 'review'})
    monkeypatch.setattr(sp, 'query_refund', lambda *args: (_ for _ in ()).throw(
        RuntimeError('simulated refund query failure')))
    result = client.post(endpoint, json={'amount_yuan': '3.00'})
    assert result.status_code == 409
    assert result.get_json()['error']['code'] == 'refund_unresolved'
    assert calls == [request_no]  # 崩溃那一次之外，绝不再受理
    assert _refund_count(order['out_trade_no']) == 1


def test_failed_retry_definite_rejection_releases_reservation(_iso, rt, monkeypatch):
    """扩展（A3）：failed 行显式重试遇到支付宝**明确业务拒绝**（白名单
    sub_code）→ 行回 ``failed``（释放预留额度），502 ``refund_failed``。"""
    order = helpers._paid_order(rt)
    client, _owner = helpers._admin_client()
    failed = sp.create_refund_row(order['out_trade_no'], 300, 'review')
    request_no = failed['out_request_no']
    sp.update_refund_status(request_no, 'failed')
    def definite_rejection(runtime, current_order, amount, number, reason=None):
        exc = sp.TradeOperationError('simulated definite business rejection')
        exc.sub_code = 'ACQ.TRADE_STATUS_ERROR'  # REFUND_DEFINITE_REJECT_SUB_CODES
        raise exc
    monkeypatch.setattr(sp, 'refund_trade', definite_rejection)
    endpoint = '/api/admin/v1/support-payments/' + order['out_trade_no'] + '/refund'
    result = client.post(endpoint,
                         json={'amount_yuan': '3.00', 'out_request_no': request_no})
    assert result.status_code == 502
    assert result.get_json()['error']['code'] == 'refund_failed'
    assert sp.get_refund(request_no)['status'] == 'failed'
    assert sp.sum_refunded_fen(order['out_trade_no']) == 0


def test_failed_retry_reservation_cas_conflict_blocks_alipay_call(_iso, rt, monkeypatch):
    """扩展（A4）：预算检查之后、支付宝调用之前发生并发状态变化——
    ``reserve_failed_refund_for_retry`` 的 CAS 不中（返回 False）→ 409
    ``refund_state_changed``，绝不调用 ``refund_trade``，行/账本保持原样
    （failed、额度仍释放，等待操作员刷新后重试）。"""
    order = helpers._paid_order(rt)
    client, _owner = helpers._admin_client()
    failed = sp.create_refund_row(order['out_trade_no'], 300, 'review')
    request_no = failed['out_request_no']
    sp.update_refund_status(request_no, 'failed')
    calls = []
    monkeypatch.setattr(sp, 'reserve_failed_refund_for_retry',
                        lambda number: False)  # 并发路径已改变该行状态
    monkeypatch.setattr(sp, 'refund_trade',
                        lambda runtime, order, amount, number, reason=None:
                        calls.append(number)
                        or {'fund_change': 'Y', 'trade_no': 'review'})
    endpoint = '/api/admin/v1/support-payments/' + order['out_trade_no'] + '/refund'
    result = client.post(endpoint,
                         json={'amount_yuan': '3.00', 'out_request_no': request_no})
    assert result.status_code == 409
    assert result.get_json()['error']['code'] == 'refund_state_changed'
    assert calls == []  # CAS 不中 → 绝不发支付宝
    assert sp.get_refund(request_no)['status'] == 'failed'  # 行未被本请求改变
    assert sp.sum_refunded_fen(order['out_trade_no']) == 0  # 额度仍释放


# --------------------------------------------------------------------------- #
# Finding 2 [P2]：return_url 用发起入口 host（白名单映射），不用固定 canonical
# --------------------------------------------------------------------------- #
def _pay_page_capturing_return_url(order, base_url, monkeypatch, headers=None):
    """会话里放进订单 ref 后 GET 支付页，捕获传给建表单的 return_url。

    仅供**扩展测试**（B3 参数化 / 未知 Host 回退）折叠评审脚手架用——主
    Test B 的脚手架必须逐字内联（见其函数体），不走这里。
    """
    monkeypatch.setenv('PUBLIC_BASE_URL', 'https://histopilot.cn')
    client = helpers._client()
    with client.session_transaction(base_url=base_url) as sess:
        sess['support_order_refs'] = [order['out_trade_no']]
    captured = []
    def form(runtime, current_order, return_url, **kwargs):
        captured.append(return_url)
        return '<form class="pay-form"></form>'
    monkeypatch.setattr(sp, 'build_page_pay_form', form)
    page = client.get('/support/pay/' + order['out_trade_no'], base_url=base_url,
                      headers=headers)
    assert page.status_code == 200
    return client, captured


def test_return_url_changes_host_and_loses_order_session(_iso, rt, monkeypatch):
    """原评审名：``test_return_url_changes_host_and_loses_order_session``
    （复现 bug 的断言已反转 + 增补同 host 回跳跟进）。

    脚手架**逐字内联**、保持评审脚本原顺序（``import app as app_mod`` →
    setenv → 建单 → client → session → captured/form fake → 打桩建表单 →
    GET 支付页）；唯一改动是那一条反转断言，其后是评审的 .cn 中性块原样，
    最后追加同 host 回跳跟进（本文件新增的脚手架）。
    """
    import app as app_mod
    monkeypatch.setenv('PUBLIC_BASE_URL', 'https://histopilot.cn')
    order = helpers._make_order(rt)
    client = helpers._client()
    with client.session_transaction(base_url='https://histopilot.com') as sess:
        sess['support_order_refs'] = [order['out_trade_no']]
    captured = []
    def form(runtime, current_order, return_url, **kwargs):
        captured.append(return_url)
        return '<form class="pay-form"></form>'
    monkeypatch.setattr(sp, 'build_page_pay_form', form)
    page = client.get('/support/pay/' + order['out_trade_no'], base_url='https://histopilot.com')
    assert page.status_code == 200
    assert captured == ['https://histopilot.com/support/return']
    monkeypatch.setattr(sp, 'query_trade', lambda *args: pytest.fail('unowned order should not be queried'))
    returned = client.get('/support/return?out_trade_no=' + order['out_trade_no'], base_url='https://histopilot.cn')
    body = returned.get_data(as_text=True)
    assert returned.status_code == 200
    assert 'supportreturn.neutral.title' in body
    # 跟进（增补，本文件新脚手架）：同一 host（.com）+ 同一会话 → 订单找到
    # 并推进（PENDING 查单），渲染待确认态而不是中性页。
    helpers._mock_query(monkeypatch, state='PENDING')
    returned_own = client.get(
        '/support/return?out_trade_no=' + order['out_trade_no'],
        base_url='https://histopilot.com')
    assert returned_own.status_code == 200
    assert 'supportreturn.neutral.title' not in returned_own.get_data(as_text=True)


def test_return_url_qr_embed_uses_entry_host(_iso, rt, monkeypatch):
    """扩展（B2）：QR 内嵌页 /support/pay/<ref>/qr 同样用入口 host 构造
    return_url（fake 经 **kwargs 接收 qr_embed=True）。"""
    order = helpers._make_order(rt)
    monkeypatch.setenv('PUBLIC_BASE_URL', 'https://histopilot.cn')
    client = helpers._client()
    with client.session_transaction(base_url='https://histopilot.com') as sess:
        sess['support_order_refs'] = [order['out_trade_no']]
    captured = []
    def form(runtime, current_order, return_url, **kwargs):
        captured.append(return_url)
        return '<form class="pay-form"></form>'
    monkeypatch.setattr(sp, 'build_page_pay_form', form)
    page = client.get('/support/pay/%s/qr' % order['out_trade_no'],
                      base_url='https://histopilot.com')
    assert page.status_code == 200
    assert captured == ['https://histopilot.com/support/return']


@pytest.mark.parametrize('base_url,expected', [
    ('https://histopilot.cn', 'https://histopilot.cn/support/return'),
    ('https://pt.solarise94.fun', 'https://histopilot.cn/support/return'),
])
def test_return_url_whitelisted_hosts_map_by_whitelist(
        base_url, expected, _iso, rt, monkeypatch):
    """扩展（B3a/b）：白名单 host 经 entry_site_for_host 映射——.cn 原地；
    旧入口 pt.solarise94.fun 落 .cn（与验证邮件链接同一张映射表）。"""
    order = helpers._make_order(rt)
    _client_used, captured = _pay_page_capturing_return_url(
        order, base_url, monkeypatch)
    assert captured == [expected]


def test_return_url_unknown_host_falls_back_to_canonical(_iso, rt, monkeypatch):
    """扩展（B3c）：未知 Host 绝不回显（带 X-Forwarded-Host 也不行）——
    回退 canonical（PUBLIC_BASE_URL）。"""
    order = helpers._make_order(rt)
    _client_used, captured = _pay_page_capturing_return_url(
        order, 'https://evil.example', monkeypatch,
        headers={'X-Forwarded-Host': 'histopilot.com'})
    assert captured == ['https://histopilot.cn/support/return']
