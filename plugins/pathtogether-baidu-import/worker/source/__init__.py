# -*- coding: utf-8 -*-
"""百度源适配器接口（C5-B；§6.2 源站逻辑归插件）。

生产实现 :class:`~worker.source.bdpan.BdpanSource` 自平台
``baidu_adapter.py`` 移植（argv 白名单/脱敏/JSON fail-closed 校验/
``<batch_id>/<name>`` 相对路径约束逐条保留），外加插件侧职责：

- **重试**：瞬态码（connector_timeout/connector_failed/…）指数退避重试；
- **限速**：相邻源操作最小间隔（bdpan CLI 自带限速时可设 0）。

测试注入 :class:`~worker.source.fake.FakeSource`（绝不触网；生产环境
变量 ``BAIDU_SOURCE=fake`` 仅测试装配）。

接口（与平台 adapter 同名，便于评审对照移植保真度）::

    capabilities() -> dict
    list_share_page(share, code, path, cursor, limit) -> dict
    transfer_selected(batch_id, share, code, fs_ids) -> {"task_id": str}
    poll_transfer(task_id) -> {"state": str}
    download_to(remote_path, dest_dir) -> None
    list_batch_copies(batch_id) -> [entry]
    cleanup_batch_copies(batch_id, allowed_relpaths) -> None
    counters() -> dict
"""

from __future__ import annotations

from . import fake  # noqa: F401  (re-export for测试装配)
from .bdpan import BdpanSource  # noqa: F401


def get_source(config):
    """按配置装配源适配器（``BAIDU_SOURCE=fake`` → FakeSource）。

    fake 分支支持 ``PT_FAKE_SHARE_TREE_FILE``（JSON 文件路径，entries
    数组形如 ``[{"path": "/share/a.kfb", "fs_id": "1", "size": N,
    "content_b64": "<base64>"}]``）——供**子进程**测试装配分享树（进程内
    测试直接构造 FakeSource；大夹具走文件而不是 env，避开 argv/env 尺寸
    上限）。生产环境绝不设置 BAIDU_SOURCE=fake。
    """
    kind = (config.source_kind or "").strip().lower()
    if kind == "fake":
        import base64
        import json
        import os
        entries = []
        tree_file = (os.environ.get("PT_FAKE_SHARE_TREE_FILE") or "").strip()
        if tree_file:
            with open(tree_file, "r", encoding="utf-8") as fh:
                for e in json.load(fh):
                    entry = dict(e)
                    content = entry.get("content_b64")
                    if content is not None:
                        entry["content"] = base64.b64decode(content)
                    entry.pop("content_b64", None)
                    entries.append(entry)
        return fake.FakeSource(entries)
    return BdpanSource(
        bin_path=config.connector_bin,
        home=config.connector_home,
        list_timeout=config.list_timeout,
        transfer_timeout=config.transfer_timeout,
        download_timeout=config.download_timeout,
        probe_timeout=config.probe_timeout,
        max_attempts=config.source_max_attempts,
        backoff_base=config.source_backoff_base,
        min_interval=config.source_min_interval,
    )
