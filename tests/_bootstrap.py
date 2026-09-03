# -*- coding: utf-8 -*-
"""测试自举（test-review P3-16 收敛点）：session 级数据目录 + openslide stub。

此前约 38 个测试模块在各自 import 期抢写 ``os.environ["SHARE_DATA_DIR"]`` /
``UPLOAD_DIR`` 并各自 try/except 注册 openslide stub——pytest 收集时全部 import，
而 app 只初始化一次，只有第一个模块的 env 生效，其余靠 per-test ``_isolate``
补偿。本模块把这两件事收敛为唯一一份实现：

- **pytest 路径**：conftest.py 在最顶部 ``import _bootstrap``——pytest 先加载
  conftest 再加载各测试模块，故早于任何 ``import app`` / ``import share_store``；
- **脚本直跑路径**（``python3 tests/test_xxx.py``，无 conftest）：各测试文件在
  ``import app`` 前 ``import _bootstrap``。模块幂等：谁先执行谁生效。

提供：
  - ``SHARE_DATA_DIR`` / ``UPLOAD_DIR``：session 级临时目录（mkdtemp），并写入
    对应 env（app.py / share_server.py / share_store.py / user_store.py
    在 import 期读取这些 env）；per-test 隔离由 ``_pt_helpers.isolate_app`` 负责。
  - openslide 不可 import 时注册一次 stub（``OpenSlide = object`` +
    ``openslide.deepzoom.DeepZoomGenerator = object``）。生产代码只用到这两个
    名字；测试切片文件均为字节 stub，无需真 openslide。
"""
import os
import sys
import tempfile

# --------------------------------------------------------------------------- #
# 1) openslide stub（幂等：真 openslide 存在则什么都不做）
# --------------------------------------------------------------------------- #
try:
    import openslide  # noqa: F401
except ImportError:
    import types as _types

    _os_mod = _types.ModuleType("openslide")
    _os_mod.OpenSlide = object
    _dz_mod = _types.ModuleType("openslide.deepzoom")
    _dz_mod.DeepZoomGenerator = object
    # 显式挂为子模块属性（`from openslide.deepzoom import ...` 双保险）
    _os_mod.deepzoom = _dz_mod
    sys.modules.setdefault("openslide", _os_mod)
    sys.modules.setdefault("openslide.deepzoom", _dz_mod)

# --------------------------------------------------------------------------- #
# 2) session 级数据目录（幂等：本进程已设则沿用，不覆盖）
# --------------------------------------------------------------------------- #
# 固定 sidecar 地址（AI 相关测试用假 requests，地址不会真的被访问；统一在此
# 固定可消除「先 import app 的模块决定 AI_SIDECAR_URL」的收集顺序依赖）。
os.environ.setdefault("AI_SIDECAR_URL", "http://127.0.0.1:8055")

if "SHARE_DATA_DIR" in os.environ and "UPLOAD_DIR" in os.environ:
    SHARE_DATA_DIR = os.environ["SHARE_DATA_DIR"]
    UPLOAD_DIR = os.environ["UPLOAD_DIR"]
else:
    _tmp_root = tempfile.mkdtemp(prefix="svs-pt-tests-")
    SHARE_DATA_DIR = os.environ.setdefault(
        "SHARE_DATA_DIR", os.path.join(_tmp_root, "share-data"))
    UPLOAD_DIR = os.environ.setdefault(
        "UPLOAD_DIR", os.path.join(_tmp_root, "uploads"))
os.makedirs(SHARE_DATA_DIR, exist_ok=True)
os.makedirs(UPLOAD_DIR, exist_ok=True)

#: session 临时根（个别文件「路径必须落在临时树内」防御断言用）
SESSION_TMP = os.path.commonpath((SHARE_DATA_DIR, UPLOAD_DIR))
