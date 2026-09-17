#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""百度分享导入 worker（W5）。

    python scripts/baidu_import_worker.py --once
    python scripts/baidu_import_worker.py --loop

职责：领取并推进枚举（只读）与导入批次（转存/下载/入库/清理），
经 ``baidu_adapter.get_adapter()`` 取适配器（生产 CLI；测试进程可注入
fake）。与 spec §6.3 对齐：

- ``BAIDU_IMPORT_WORKER=false`` 时拒绝启动（部署级总开关；其余值/缺省
  视为允许运行——是否常驻由部署的进程管理决定）。
  注意与 ``docker_entry.sh`` 的口径差异：容器入口只在
  ``BAIDU_IMPORT_WORKER=1/true/yes/on``（缺省 0）时拉起本脚本；而本
  脚本自身把**空值/缺省当允许**，方便运维在容器外手动跑一轮排水
  （``--once``）。两端故意不一致：入口 fail-closed，脚本宽松放行；
  capabilities 暴露的 ``worker_enabled`` 与**入口**口径对齐。
- ``BAIDU_ENUMERATION_ENABLED`` 关闭时**不再领取新枚举**（已接受任务
  继续收口/展示）；导入批次一经接受不受开关回退影响（已接受任务必须
  收口），批次执行的真实外部动作由 ``BAIDU_IMPORT_ENABLED`` 在创建时
  门控；
- 全部外部调用经适配器（参数数组/超时/schema 校验），本脚本不直接
  触发子进程；崩溃恢复依赖 store 内的对账凭证（transfer_task_id /
  source_sha256 / ingest_token），不无条件重转存/重下载/重复入库。
"""

import os
import sys
import time

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import baidu_adapter  # noqa: E402
import baidu_import_store as store  # noqa: E402

POLL_SECONDS = float(os.environ.get("BAIDU_WORKER_POLL_SECONDS") or 15)

_FALSY = frozenset({"false", "0", "no", "off"})


def _enabled(name):
    return (os.environ.get(name) or "").strip().lower() not in _FALSY


def drain_once(worker_id="baidu-worker"):
    """跑一轮：先枚举（只读）后批次；返回 (enumerations, batches)。"""
    adapter = baidu_adapter.get_adapter()
    n_enum = 0
    if _enabled(baidu_adapter.ENV_ENUMERATION_ENABLED):
        while True:
            claim = store.claim_enumeration(worker_id)
            if claim is None:
                break
            store.run_one_enumeration(claim, adapter)
            n_enum += 1
    n_batch = 0
    while True:
        claim = store.claim_batch(worker_id)
        if claim is None:
            break
        # claim 已置 running 并持有新鲜租约：直接执行已领取的批次，
        # 不按 id 二次领取（run_batch 只接受 queued/租约过期 → None，
        # 批次会永远停在 running）
        store.run_claimed_batch(claim, adapter, worker_id=worker_id)
        n_batch += 1
    return n_enum, n_batch


def main(argv):
    if not _enabled("BAIDU_IMPORT_WORKER"):
        sys.stderr.write(
            "BAIDU_IMPORT_WORKER=false：worker 停用（已接受任务保持排队，"
            "可通过重新启用 worker 收口）\n")
        return 0
    if "--loop" in argv:
        while True:
            try:
                drain_once()
            except KeyboardInterrupt:
                return 0
            except Exception:  # noqa: BLE001
                import traceback
                traceback.print_exc()
            time.sleep(POLL_SECONDS)
    n_enum, n_batch = drain_once()
    sys.stdout.write("enumerations=%d batches=%d\n" % (n_enum, n_batch))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
