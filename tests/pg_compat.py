# -*- coding: utf-8 -*-
"""PG 测试兼容垫片。BACKEND 恒为 postgres（json 双跑已退役）。

``json_only`` 标记已删除：json 文件格式 / SHARE_FILE patch 语义 / per-slide seq
的用例整体移除，不再需要按后端 skip 的标记。
"""
from conftest import BACKEND  # noqa: F401