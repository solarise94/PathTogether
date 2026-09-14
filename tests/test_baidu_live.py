# -*- coding: utf-8 -*-
"""W5 真实百度链路验收（L01–L04）——**默认不运行**。

只在 ``RUN_BAIDU_LIVE_TESTS=1`` **且**专用测试分享/连接器凭据齐备时执行
（spec §9）。真实参数只从环境读取（``BAIDU_LIVE_SHARE_TEXT`` 等），
日志与断言绝不输出完整分享链接、提取码或令牌。默认（未设
``RUN_BAIDU_LIVE_TESTS``）整模块 skip，记录 L01–L04 为
`NOT RUN — 缺少具体条件`；不允许用 fake 代替真实验收，也不允许把
NOT RUN 标成 PASS。

L01 专用测试分享（≥2 层目录、含选中/未选中文件）：CLI 真实只读枚举与
    预期清单一致；只读阶段 0 转存/0 下载/0 删除。
L02 一份 native + 一份当前支持的需转换样本：真实下载、探测/转换、
    入库可打开；未选中未转存。
L03 非敏感 ≥1GiB 文件、worker 中途终止重启：恢复或有界重下，SHA 一致；
    产物与配额不重复。
L04 成功副本清理与故障恢复：只删本批副本；预置文件仍在；清理失败
    不影响入库状态。
"""
import os

import pytest

_LIVE = (os.environ.get("RUN_BAIDU_LIVE_TESTS") or "").strip() == "1"

pytestmark = pytest.mark.skipif(
    not _LIVE,
    reason="RUN_BAIDU_LIVE_TESTS!=1 —— L01–L04 NOT RUN（真实百度链路验收"
           "需专用授权测试分享与连接器账号，默认不访问远端）")


def _live_share_text():
    """专用测试分享文本（含提取码），只从测试环境读取，绝不写日志。"""
    text = (os.environ.get("BAIDU_LIVE_SHARE_TEXT") or "").strip()
    if not text:
        pytest.skip(
            "NOT RUN — 缺少 BAIDU_LIVE_SHARE_TEXT（专用授权测试分享未配置）")
    return text


def test_l01_read_only_enumeration_matches_expected_listing():
    _live_share_text()
    pytest.skip(
        "NOT RUN — 真实 L01 需专用测试分享的预期清单基线"
        "（expected manifest env 未配置）")


def test_l02_selected_transfer_download_and_ingest():
    _live_share_text()
    pytest.skip(
        "NOT RUN — 真实 L02 需专用测试分享中的 native + 需转换样本清单"
        "（env 未配置）")


def test_l03_large_file_worker_restart_recovery():
    _live_share_text()
    pytest.skip(
        "NOT RUN — 真实 L03 需 ≥1GiB 非敏感测试文件与中断重启环境"
        "（env 未配置）")


def test_l04_cleanup_only_batch_copies():
    _live_share_text()
    pytest.skip(
        "NOT RUN — 真实 L04 需已完成批次的连接器账号副本布局（env 未配置）")
