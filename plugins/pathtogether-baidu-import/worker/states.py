# -*- coding: utf-8 -*-
"""插件任务序（§3.2/§7.3；插件侧状态，不入平台表）。

::

    queued → downloading → transforming → validating → delivering
           → awaiting_receipt → cleanup_pending → done

失败/取消也必须经过可重试清理（cleanup_pending）；``published +
cleanup_failed`` 保留已发布结果与清理状态（不重传、不再 begin）。
"""

from __future__ import annotations

QUEUED = "queued"
DOWNLOADING = "downloading"
TRANSFORMING = "transforming"
VALIDATING = "validating"
DELIVERING = "delivering"
AWAITING_RECEIPT = "awaiting_receipt"
CLEANUP_PENDING = "cleanup_pending"
DONE = "done"
FAILED = "failed"
CANCELLED = "cancelled"

#: 主序（单调推进）
PIPELINE = (QUEUED, DOWNLOADING, TRANSFORMING, VALIDATING, DELIVERING,
            AWAITING_RECEIPT, CLEANUP_PENDING, DONE)

#: 终态
TERMINAL = frozenset({DONE, FAILED, CANCELLED})


def can_advance(cur, nxt):
    """主序只能向前；任意状态都可转入 cleanup_pending（失败/取消清理）。"""
    if cur == nxt:
        return True
    if nxt == CLEANUP_PENDING:
        return True
    if cur in PIPELINE and nxt in PIPELINE:
        return PIPELINE.index(cur) < PIPELINE.index(nxt)
    return False
