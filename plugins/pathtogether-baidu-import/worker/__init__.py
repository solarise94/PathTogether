# -*- coding: utf-8 -*-
"""pathtogether-baidu-import 插件后端 worker 包（C5-B）。

独立进程：只用 HTTP 与平台通信（/api/plugin/v1/*，Bearer scoped JWT），
**绝不 import 平台模块**（app.py / baidu_* / slide_* 一概不依赖）。
模块布局：

- ``config``：env 配置（平台地址/安装凭证/源适配器/转换 CLI/路径）；
- ``errors``：合同 §1.8 错误码 retryable 表 + 插件侧错误类型；
- ``http_client``：传输层（退避重试、Retry-After 遵从、连接错误重试）；
- ``platform_client``：合同端点客户端（token 交换 / imports.* / baidu 桥）；
- ``journal``：每任务持久日志（0600；write_token 只存活跃任务，绝不打日志）；
- ``states``：插件任务序 queued→downloading→transforming→validating→
  delivering→awaiting_receipt→cleanup_pending→done（失败/取消也过清理）；
- ``source``：百度源适配器接口 + bdpan 生产实现（自平台 baidu_adapter.py
  移植）+ fake 测试源（绝不触网）；
- ``convert``：slide-transform 原生 CLI 封装（KFB→tif / KFBF→ome.tif，
  伴随 channel.json；native 单文件不转换）；
- ``cleanup``：受管根清理 + cleanup-confirm 退避重试；
- ``item_task``：单条目编排（下载→转换→校验→交付→回执→清理）；
- ``batch_driver``：桥 claim/heartbeat/report 驱动循环；
- ``__main__``：进程入口（健康端点 + 主循环）。
"""

__all__ = [
    "batch_driver", "cleanup", "config", "convert", "errors",
    "http_client", "item_task", "journal", "platform_client", "source",
    "states",
]
