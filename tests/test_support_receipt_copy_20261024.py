# -*- coding: utf-8 -*-
"""支持页凭条文案收敛（2026-10-24 用户评审）回归。

用户原话：「“本凭条为致谢balabala”，这个可以写“您的支持将用于histopilot
服务器维护什么什么的”说法。“谢谢你，凭条上的礼物已经打包好”这个也不用吧，
可以克制一点」。

契约：
1. 弹层已支付窗格凭条（``#pay-print-receipt``）只保留一行 ``support.receipt.use``
   中性用途说明——不再有「凭条上的礼物已打包好」与「本凭条为致谢卡片」；
2. /support/return 已支付态完整凭条同样换成 ``support.receipt.use``，保留
   「—— HistoPilot 开发者」署名与条码，顺序为 分隔线 → receipt-use →
   receipt-sign → 条码；「本凭条为致谢卡片」删除；
3. i18n zh/en 都有 ``support.receipt.use`` 且文案精确；三个旧键
   （``support.modal.print.thanks`` / ``supportreturn.receipt.thanks`` /
   ``supportreturn.receipt.disclaimer``）在字典里不存在。

运行：cd 项目根 && python3 -m pytest tests/test_support_receipt_copy_20261024.py -q
"""
from pathlib import Path

import pytest

import test_support_payments as helpers

pytest_plugins = ["test_support_payments"]

REPO_ROOT = Path(__file__).resolve().parent.parent
I18N_JS = REPO_ROOT / "static" / "i18n.js"

USE_ZH = "你的支持将用于 HistoPilot 的服务器与日常维护。"
USE_EN = "Your support goes toward HistoPilot's servers and day-to-day maintenance."

OLD_KEYS = (
    "support.modal.print.thanks",
    "supportreturn.receipt.thanks",
    "supportreturn.receipt.disclaimer",
)
OLD_COPY = ("打包好", "本凭条", "致谢卡片")


def _assert_no_old_copy(body, where):
    for text in OLD_COPY:
        assert text not in body, "%s 仍含旧文案：%s" % (where, text)
    for key in OLD_KEYS:
        assert key not in body, "%s 仍含旧键：%s" % (where, key)


# --------------------------------------------------------------------------- #
# 1. 弹层凭条（/support 开启支付时渲染的已支付窗格）
# --------------------------------------------------------------------------- #
def test_support_modal_receipt_has_single_use_line(_iso):
    body = helpers._client().get("/support").get_data(as_text=True)
    assert 'id="pay-print-receipt"' in body
    assert 'class="receipt-use"' in body
    assert 'data-i18n="support.receipt.use"' in body
    assert USE_ZH in body
    _assert_no_old_copy(body, "/support")


# --------------------------------------------------------------------------- #
# 2. /support/return 已支付态完整凭条
# --------------------------------------------------------------------------- #
def test_return_receipt_use_line_keeps_signature(_iso, rt, monkeypatch):
    order = helpers._make_order(rt)
    client = helpers._own_session(helpers._client(), order["out_trade_no"])
    helpers._mock_query(monkeypatch, state="PAID", amount="6.66",
                        trade_no="20261024copy")
    body = client.get(
        "/support/return?out_trade_no=%s" % order["out_trade_no"]
    ).get_data(as_text=True)
    assert 'id="receipt"' in body
    assert 'class="receipt-use"' in body
    assert 'data-i18n="support.receipt.use"' in body
    assert USE_ZH in body
    assert "—— HistoPilot 开发者" in body
    _assert_no_old_copy(body, "/support/return")
    # 顺序：分隔线 → receipt-use → receipt-sign → 条码
    use_at = body.index('class="receipt-use"')
    sign_at = body.index('class="receipt-sign"')
    barcode_at = body.index('class="receipt-barcode"')
    assert use_at < sign_at < barcode_at


# --------------------------------------------------------------------------- #
# 3. i18n 字典：新键 zh/en 精确成对，三个旧键清除
# --------------------------------------------------------------------------- #
def test_i18n_receipt_use_zh_en_and_old_keys_removed():
    source = I18N_JS.read_text(encoding="utf-8")
    assert source.count('"support.receipt.use": "%s"' % USE_ZH) == 1
    assert source.count('"support.receipt.use": "%s"' % USE_EN) == 1
    for key in OLD_KEYS:
        assert '"%s"' % key not in source, "i18n.js 仍含旧键：%s" % key
    for text in OLD_COPY:
        assert text not in source, "i18n.js 仍含旧文案：%s" % text
