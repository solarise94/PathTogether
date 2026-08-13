# -*- coding: utf-8 -*-
"""PG 双跑兼容辅助（Stage 3b-2）。

``BACKEND``：当前测试运行后端（'json' | 'postgres'），由 conftest 在 import 期
决定（仅 RUN_PG_TESTS=1 时为 'postgres'）。

``json_only``：json-only 测试标记。凡断言 json 文件格式 / SHARE_FILE patch 语义 /
per-slide seq 具体值 的用例，加 ``@json_only`` 在 PG 跑时跳过（每个都有注释理由，
见各测试文件）。json 默认路径下 BACKEND=='json'，永不跳过。
"""
from conftest import BACKEND  # noqa: F401  （conftest 在 RUN_PG_TESTS=1 时先起 PG）

import pytest  # noqa: E402

json_only = pytest.mark.skipif(
    BACKEND == "postgres",
    reason="json-only 测试：断言 json 文件格式 / 文件 patch / per-slide seq，PG 后端无意义",
)
