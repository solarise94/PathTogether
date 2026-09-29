# -*- coding: utf-8 -*-
"""受管根清理 + cleanup-confirm（C5-B；合同 §5/§7.3）。

顺序（§7.3「顺序为可靠发布回执 → 关闭全部源/输出写者 → 清理插件源/
中间件/产物副本 → 幂等清理确认」）：调用方保证进入本模块前已停止对
受管根的一切写入（写者已关闭）。

- 受管根 = 平台派生 ``…/plugin-work/<installation_id>/imports/<import_id>/``
  ——只删这一棵树，绝不接受/构造任意其它路径；
- 本地先删再验（存在且非空 → 继续重试）；
- ``cleanup-confirm`` 非空 → ``409 cleanup_not_verified``（带残余字节）→
  退避重试（本地再删 → 再确认），有界；
- 耗尽仍失败 → ``cleanup_failed``：**保留已发布结果**（不重传、不再
  begin——§7.3「published + cleanup_failed 保留已发布结果与清理状态」）；
  scratch 未释放（平台侧），责任进对账。
"""

from __future__ import annotations

import shutil
import time
from pathlib import Path

from . import errors


def tree_is_empty(root):
    """受管根不存在（整体删除也算清理）或存在且无任何成员。"""
    root = Path(root)
    if not root.exists():
        return True
    return next(root.iterdir(), None) is None


def remove_tree(root):
    """删除受管根整树（不存在 → no-op）。删除是幂等重试的。"""
    root = Path(root)
    if not root.exists():
        return
    # 先尽力删子项再删根（rmtree 对只读残件抛错时调用方按退避重试）
    shutil.rmtree(root)


def residual_bytes(root):
    root = Path(root)
    if not root.exists():
        return 0
    total = 0
    for p in root.rglob("*"):
        if p.is_file():
            try:
                total += p.stat().st_size
            except OSError:
                total += 1
    return total


def cleanup_managed_root(platform, import_id, write_token, managed_root, *,
                          max_attempts=8, backoff_base=1.0, sleep=None,
                          on_attempt=None):
    """清理 + 确认循环。返回 ``"cleaned"`` 或 ``"cleanup_failed"``。

    ``on_attempt(attempt, phase, detail)``：测试观测钩子（不打日志的替代）。
    """
    sleep = sleep if sleep is not None else time.sleep
    attempts = 0
    while True:
        attempts += 1
        try:
            remove_tree(managed_root)
        except OSError:
            pass  # 本地删除失败也走确认让平台报残余（同一判定来源）
        empty = tree_is_empty(managed_root)
        if on_attempt:
            on_attempt(attempts, "removed", {"empty": empty})
        if empty:
            try:
                platform.import_cleanup_confirm(import_id, write_token)
                return "cleaned"
            except errors.ContractError as e:
                if e.code != "cleanup_not_verified":
                    raise
                if on_attempt:
                    on_attempt(attempts, "not_verified",
                               {"residual_bytes":
                                e.details.get("residual_bytes")})
        if attempts >= max_attempts:
            return "cleanup_failed"
        sleep(backoff_base * (2 ** (attempts - 1)))
