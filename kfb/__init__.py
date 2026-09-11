# -*- coding: utf-8 -*-
"""KFB Phase A 离线包：parser / 合成 fixture / 混合重封装 converter。

**不**接入上传入口（app.SUPPORTED_EXTS 与前端 accept 不变），仅提供
离线 CLI（scripts/convert_kfb.py）与测试能力。
"""

from .errors import KfbError, KFB_ERROR_CODES
from .parser import (KfbAssociated, KfbDocument, KfbHeader, KfbLevel,
                     KfbTile, parse_kfb)
from .converter import convert_kfb
from .fixture import build_synthetic_kfb

__all__ = [
    "KfbError", "KFB_ERROR_CODES",
    "KfbAssociated", "KfbDocument", "KfbHeader", "KfbLevel", "KfbTile",
    "parse_kfb", "convert_kfb", "build_synthetic_kfb",
]
