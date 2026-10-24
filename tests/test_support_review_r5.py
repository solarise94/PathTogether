# -*- coding: utf-8 -*-
"""用户独立评审 R5（2026-10-23）回归测试。

来源：/mnt/ssd2/tests/support-review-r5-91f3ed/test_review_r5_regressions.py
——除本头注与下方列出的脚手架增补外**逐字复制，断言未做任何改动**。断言的
是评审确定的两条「同一会话留下两张可支付订单」修复面：

  1) 刷新 /support 后携带**新表单 key** 的同金额下单，在旧二维码仍可支付
     （11 分钟旧、剩 4 分钟）时必须先核实并关掉旧单再建新单——普通下单
     入口与 regenerate 端点同样不得绕过关单（服务端规则 A）；
  2) 同一弹层「A 过期 → 重建 B → 关弹层 → 再提交（表单仍带 A 的 key）→
     弹层显示 A → 再次重新生成」绝不能再建第三张可支付订单——regenerate
     一个已被替换的旧单必须交回替换单（服务端规则 B），且重开弹层必须展示
     当前订单（客户端：表单 key 跟随所示订单，static/support/support.js）
     ——测试 2 走真实 Chromium，同时覆盖服务端与客户端两半。

**相对评审原文件的脚手架增补（逐项列出；断言与 mock 契约逐字未动）**：

- 测试 2 的评审原文件把运行产物写死到评审运行目录
  ``/mnt/ssd2/tests/support-review-r5-91f3ed``（只读证据，禁止写入）；本
  文件新增 ``tmp_path`` fixture 参数，``REVIEW_RUN`` 改指 ``tmp_path``
  （``real-browser.log`` / ``real-browser-result.json`` 随之落入 pytest 的
  basetemp——本机约定：测试运行数据在 SSD2 的运行目录下）；
- ``reopen-real-app.cjs`` 复制到 ``tests/browser/support_review_r5/``，
  **唯一改动**是 playwright require：评审机器的绝对 worktree 路径 →
  ``require('playwright')``（按仓库既有浏览器驱动
  ``tests/browser/slide_tools_c3/lib.js`` 的方式从仓库根 node_modules 解析，
  与 worktree 位置无关）；测试改为运行该仓库内脚本；
- 测试 2 加最小 skip 守卫：node 不在 PATH 或仓库根缺 node_modules/playwright
  时跳过（仓库无既有 pytest 级浏览器守卫可复用）；
- 评审原文件的 import 行保持原顺序，守卫所需的 ``shutil`` / ``pytest`` 追加
  在其后。
"""

import test_support_payments as helpers
import support_payments as sp
import pg_store
import app as app_mod
import json
import os
from pathlib import Path
import subprocess
import threading
import shutil

import pytest

pytest_plugins = ["test_support_payments"]

#: 测试 2 运行的仓库内浏览器脚本（见头注脚手架增补第三条）
_REOPEN_SCRIPT = Path(__file__).resolve().parent / "browser" \
    / "support_review_r5" / "reopen-real-app.cjs"


def _node_and_playwright_available():
    """最小 skip 守卫：真实 Chromium 链路需要 node 与仓库根 playwright。"""
    if shutil.which("node") is None:
        return False
    root = Path(__file__).resolve().parent.parent
    return (root / "node_modules" / "playwright").is_dir()


def active_refs():
    conn = pg_store.connect()
    try:
        return [r[0] for r in conn.execute(
            "SELECT out_trade_no FROM support_orders "
            "WHERE status IN ('created','paying') ORDER BY out_trade_no").fetchall()]
    finally:
        conn.close()


def test_fresh_form_does_not_bypass_close_before_new_order(_iso, monkeypatch):
    client = helpers._client()
    token = helpers._csrf_token(client)
    first = client.post('/support/orders', data={
        'amount': '6.66', 'csrf_token': token, 'idempotency_key': '1' * 32})
    old_ref = first.headers['Location'].rsplit('/', 1)[-1]
    sp.mark_paying(old_ref)
    helpers._backdate(old_ref, minutes=11)
    operations = []
    monkeypatch.setattr(sp, 'query_trade', lambda *args: operations.append('query') or {
        'state': 'PENDING', 'trade_status': 'WAIT_BUYER_PAY'})
    monkeypatch.setattr(sp, 'close_trade', lambda *args: operations.append('close') or True)
    # A refreshed /support page has a new form key while the old QR is still payable.
    client.get('/support')
    fresh = client.post('/support/orders', data={
        'amount': '6.66', 'csrf_token': token, 'idempotency_key': '2' * 32})
    refs = active_refs()
    assert len(refs) <= 1, (fresh.status_code, operations, refs)


@pytest.mark.skipif(not _node_and_playwright_available(),
                    reason="node/playwright 不可用（真实 Chromium 浏览器链路）")
def test_same_tab_reopening_after_regenerate_does_not_create_two_live_orders(
        _iso, monkeypatch, tmp_path):
    from werkzeug.serving import make_server
    run = tmp_path
    operations = []
    create_order = sp.create_order
    created = []
    def create(*args, **kwargs):
        row = create_order(*args, **kwargs)
        if row:
            created.append(row['out_trade_no'])
            if len(created) == 1:
                # Only the first order is expired; all replacements retain the real 15m duration.
                helpers._backdate(row['out_trade_no'], minutes=16)
                return sp.get_order(row['out_trade_no'])
        return row
    monkeypatch.setattr(sp, 'create_order', create)
    def query(runtime, ref):
        operations.append(('query', ref))
        closed = sp.get_order(ref)['status'] == 'closed'
        return {'state': 'CLOSED' if closed else 'PENDING'}
    monkeypatch.setattr(sp, 'query_trade', query)
    monkeypatch.setattr(sp, 'close_trade', lambda runtime, ref:
                        operations.append(('close', ref)) or True)
    server = make_server('127.0.0.1', 0, app_mod.app, threaded=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    env = dict(os.environ, REVIEW_RUN=str(run),
               REVIEW_BASE_URL='http://127.0.0.1:%d' % server.server_port)
    try:
        with (run / 'real-browser.log').open('w') as log:
            result = subprocess.run(['node', str(_REOPEN_SCRIPT)],
                                    env=env, stdout=log, stderr=subprocess.STDOUT,
                                    timeout=30)
        assert result.returncode == 0, (run / 'real-browser.log').read_text()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
    browser = json.loads((run / 'real-browser-result.json').read_text())
    refs = active_refs()
    assert refs == [browser['replacement']], (browser, operations, refs)
    assert browser['replacement'] in browser['reopenedFrame']
