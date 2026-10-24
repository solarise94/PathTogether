# -*- coding: utf-8 -*-
"""用户独立评审 R6（2026-10-24）回归测试。

来源：/mnt/ssd2/tests/support-review-r6-GscBkG/test_review_r6_regressions.py
——除本头注外**逐字复制，脚手架与断言未做任何改动**。断言评审确定的边界
行为：旧单 A 过期 regenerate 出替换单 B 后，B 已在另一标签页支付并本地落库
（已提交）；不知情的旧标签页携带自身未用的 key 再次 regenerate(A) 时，
绝不能再创建新订单——订单表只剩 A、B 两行，且没有 created/paying 订单
（修复前：A 已 CLOSED → 新建 C，共 3 行且留有可支付单）。

响应形态（点名 B 的 paid 载荷、请求 key 不绑定）不在本文件断言范围，见
tests/test_support_r6_20261024.py。
"""

import test_support_payments as helpers
import support_payments as sp
import pg_store

pytest_plugins = ['test_support_payments']


def test_stale_tab_does_not_generate_another_order_after_replacement_paid(
        _iso, rt, monkeypatch):
    old = helpers._make_order(rt)
    helpers._backdate(old['out_trade_no'], minutes=16)
    client = helpers._own_session(helpers._client(), old['out_trade_no'])
    token = helpers._csrf_token(client)
    calls = []
    def query(runtime, ref):
        calls.append(('query', ref))
        row = sp.get_order(ref)
        if row['status'] == 'paid':
            return {'state': 'PAID', 'total_amount': sp.fen_to_yuan(row['amount_fen']),
                    'trade_no': row['trade_no']}
        return {'state': 'CLOSED' if row['status'] == 'closed' else 'PENDING'}
    monkeypatch.setattr(sp, 'query_trade', query)
    monkeypatch.setattr(sp, 'close_trade', lambda runtime, ref:
                        calls.append(('close', ref)) or True)
    url = '/support/orders/' + old['out_trade_no'] + '/regenerate'
    first = client.post(url, data={'csrf_token': token, 'idempotency_key': '1' * 32})
    assert first.status_code == 201
    replacement = first.get_json()['out_trade_no']
    # Payment in the other tab is already verified and committed locally.
    sp.mark_paid_idempotent(replacement, 'r6-paid-replacement')
    # The original tab has not seen the replacement and uses its own unused next key.
    stale = client.post(url, data={'csrf_token': token, 'idempotency_key': '2' * 32})
    conn = pg_store.connect()
    try:
        rows = conn.execute('SELECT out_trade_no, status FROM support_orders').fetchall()
    finally:
        conn.close()
    assert len(rows) == 2, (stale.status_code, stale.get_json(), calls, rows)
    assert not [r for r in rows if r[1] in ('created', 'paying')]
