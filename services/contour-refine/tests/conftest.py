# -*- coding: utf-8 -*-
"""service 测试引导：服务目录置于 sys.path 首位 + 关闭启动即加载真实权重。

本目录（services/contour-refine/tests）用独立 venv 运行（README），不属于
PathTogether 主 pytest 套件（pytest.ini testpaths=tests 已排除 services/）。

必须在 import app 之前：
  1. 把服务目录**无条件**插到 sys.path[0]——pytest 会把 rootdir（仓库根，
     pythonpath=.）与 tests/ 目录插到路径前面，`import app` 会误中仓库根的
     PathTogether app.py（其依赖 requests/PG 等，本 venv 没有）；
  2. 设置 CONTOUR_REFINE_EAGER_LOAD=0——跳过 app.py 启动即加载 SAM2。
"""
import os
import sys

_SERVICE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _SERVICE_DIR)

os.environ.setdefault("CONTOUR_REFINE_EAGER_LOAD", "0")
