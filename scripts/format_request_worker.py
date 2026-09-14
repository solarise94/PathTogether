#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""格式兼容请求邮件排水 worker（PG 权威发送方，W2）。

    python scripts/format_request_worker.py --once
    python scripts/format_request_worker.py --loop

每轮先 :func:`format_request_store.reap_expired_sending` 回收租约过期的
sending 作业（崩溃语义：send_started_at 空 → queued 重试；非空 →
uncertain 封存，绝不自动重发），再 claim（FOR UPDATE SKIP LOCKED，发送
不占 DB 事务）。uncertain/sent 绝不重发；发送通道未配置 → 作业保留
queued。web 进程的 drain_async 默认关闭（FORMAT_REQUEST_INLINE_DRAIN），
本 worker 是唯一权威发送方。
"""

import os
import sys
import time

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import format_request_store  # noqa: E402

POLL_SECONDS = float(os.environ.get("FORMAT_REQUEST_POLL_SECONDS") or 60)


def run_once():
    """单轮排水：崩溃回收 + 领取发送；返回发送成功条数。"""
    reaped = format_request_store.reap_expired_sending()
    if reaped["requeued"] or reaped["uncertain"]:
        sys.stderr.write(
            "reap: requeued=%d uncertain=%d\n"
            % (reaped["requeued"], reaped["uncertain"]))
    return format_request_store.drain_once()


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    loop = "--loop" in argv
    if not loop and "--once" not in argv:
        sys.stderr.write("需要 --once 或 --loop 之一\n")
        return 2
    if not loop:
        sys.stdout.write("sent=%d\n" % run_once())
        return 0
    while True:
        try:
            run_once()
        except KeyboardInterrupt:
            return 0
        except Exception:  # noqa: BLE001
            import traceback
            traceback.print_exc()
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    sys.exit(main())
