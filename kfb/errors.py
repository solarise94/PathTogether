# -*- coding: utf-8 -*-
"""KFB 稳定错误契约（Phase A）。

KFB parser/converter 面向非可信二进制：所有失败以 :class:`KfbError`
（携带稳定机器码 ``.code``）抛出，客户端只见码不见内部细节
（docs/kfb-ingestion-converter-review.md §9）。
"""

from __future__ import annotations

#: 稳定错误码固定词表（§0.4 / §9）
KFB_ERROR_CODES = frozenset((
    "unsupported_kfb_variant",
    "invalid_kfb_header",
    "invalid_tile_index",
    "tile_payload_out_of_bounds",
    "jpeg_decode_failed",
    "metadata_missing_required",
    "conversion_timeout",
    "conversion_output_too_large",
    "conversion_disk_low",
    "conversion_validation_failed",
))


class KfbError(ValueError):
    """KFB 解析/转换失败（携带稳定机器码）。

    code 固定为 :data:`KFB_ERROR_CODES` 之一：
      - ``unsupported_kfb_variant``：未知 magic/version、非明场 flags 等
        变体问题——不猜测，fail-closed；
      - ``invalid_kfb_header``：header 字段越界/非法（尺寸、上限、flags、
        scanner/associated 名等）；
      - ``invalid_tile_index``：tile/associated 索引区结构非法（level 越界、
        网格未对齐、重叠、尺寸与层不符、payload 长度非法）；
      - ``tile_payload_out_of_bounds``：payload offset+length 越出文件边界；
      - ``jpeg_decode_failed``：payload 不是合法 JPEG（SOI/EOI 缺失）或
        解码失败；
      - ``metadata_missing_required``：转换必需元数据（MPP 等）缺失；
      - ``conversion_timeout`` / ``conversion_output_too_large`` /
        ``conversion_disk_low``：worker 资源护栏；
      - ``conversion_validation_failed``：tile 网格覆盖不全、层级采样
        不一致、输出重校验不过等。

    兼容历史：继承 ValueError（脚本型调用方按 ValueError 捕获仍成立）。
    """

    def __init__(self, code, message=None):
        if code not in KFB_ERROR_CODES:
            code = "invalid_kfb_header"
        super().__init__(message or code)
        self.code = code
