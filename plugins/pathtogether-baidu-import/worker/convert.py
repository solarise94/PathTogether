# -*- coding: utf-8 -*-
"""转换步骤：共享原生核心 slide-transform CLI（C5-B；§7.1）。

- KFB → 经典多 IFD BigTIFF（``.tif``）；KFBF → 多通道 OME-TIFF
  （``.ome.tif``）；**不得**调用旧 Python 转换器（已退役方向）。
- KFBF 可带伴随 ``<名>_kfbf/Annotations/channel.json``——存在时经
  ``--channel-json`` 传入（真实样本 C0 清点 §4：伴随目录形态）。
- native 单文件（svs/tif/…）不转换，产物按原样交付。
- 格式分类镜像平台 ``slide_format_registry``（运行期不 import 平台模块，
  枚举副本在此维护；漂移由平台侧 tests/test_slide_format_registry.py
  与本模块单测共同约束）。
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from . import errors

#: native 单文件扩展（镜像 slide_format_registry CAP_NATIVE_SINGLE_FILE 集）
NATIVE_EXTS = frozenset({
    ".svs", ".tif", ".tiff", ".ndpi", ".vms", ".vmu", ".scn", ".bif",
    ".svslide", ".bmp", ".jpg", ".jpeg",
})

#: 需转换扩展 → 产物扩展（镜像 CAP_CONVERT_REQUIRED）
CONVERT_EXTS = {
    ".kfb": "tif",
    ".kfbf": "ome.tif",
}


class ConvertError(errors.PluginWorkerError):
    """转换失败（code 稳定：unsupported_format/converter_missing/
    converter_timeout/conversion_failed/converter_report_invalid/
    conversion_output_missing/artifact_*）。"""

    def __init__(self, code, message=None):
        super().__init__(message or code)
        self.code = str(code)


def classify(name):
    """文件名 → 分类结果。

    返回 ``{"needs_convert": bool, "format_ext": str, "filename": str}``；
    ``filename`` 是交付产物名（native 原名；kfb/kfbf 换产物扩展）。
    未知/不支持扩展 → ConvertError(unsupported_format)（fail-closed，
    与平台 registry CAP_UNSUPPORTED 同口径；bundle 格式 mrxs 属单文件
    通道不可交付，同样拒绝）。
    """
    base = str(name or "").replace("\\", "/").rsplit("/", 1)[-1].strip()
    if not base or base in (".", "..") or "\x00" in base:
        raise ConvertError("invalid_name")
    stem, dot, ext = base.rpartition(".")
    if not dot or not stem:
        raise ConvertError("unsupported_format")
    ext = "." + ext.lower()
    if ext in NATIVE_EXTS:
        return {"needs_convert": False, "format_ext": ext.lstrip("."),
                "filename": base}
    if ext in CONVERT_EXTS:
        out_ext = CONVERT_EXTS[ext]
        return {"needs_convert": True, "format_ext": out_ext,
                "filename": stem + "." + out_ext}
    raise ConvertError("unsupported_format")


def find_channel_json(source_path):
    """KFBF 伴随 channel.json：``<stem>_kfbf/Annotations/channel.json``。

    仅接受**与源文件同目录**的伴随目录（下载副本旁）；返回 Path 或 None。
    """
    src = Path(source_path)
    companion = src.parent / (src.stem + "_kfbf") / "Annotations" / \
        "channel.json"
    if companion.is_file():
        return companion
    return None


def sniff_artifact(path):
    """交付前的本地魔法嗅探（轻量自检；权威验证在平台 commit §1.4）。

    返回 sniff 名（tiff/jpeg/bmp/…）；无法识别 → ConvertError。
    TIFF 族含经典（``II*\\0``/``MM\\0*``）与 BigTIFF（``II+\\0``/
    ``MM\\0+``——kfb→tif / kfbf→ome.tif 产物即 BigTIFF）。
    """
    with open(path, "rb") as fh:
        head = fh.read(16)
    if head[:4] in (b"II*\x00", b"MM\x00*", b"II+\x00", b"MM\x00+"):
        return "tiff"
    if head.startswith(b"\xff\xd8\xff"):
        return "jpeg"
    if head.startswith(b"BM"):
        return "bmp"
    if head.startswith(b"SVS") or head[8:12] in (b"ISPH",):
        return "svs"
    raise ConvertError("artifact_magic_unknown")


class Converter:
    """slide-transform CLI 封装（convert <in> <out> --overwrite
    [--channel-json f]，stdout 为 JSON 报告）。"""

    def __init__(self, bin_path, *, timeout=7200.0):
        self.bin = str(bin_path)
        self.timeout = float(timeout)

    def convert(self, src, out, channel_json=None):
        src, out = Path(src), Path(out)
        out.parent.mkdir(parents=True, exist_ok=True)
        argv = [self.bin, "convert", str(src), str(out), "--overwrite"]
        if channel_json is not None:
            argv += ["--channel-json", str(channel_json)]
        try:
            proc = subprocess.run(
                argv, capture_output=True, text=True, timeout=self.timeout)
        except FileNotFoundError:
            raise ConvertError("converter_missing") from None
        except subprocess.TimeoutExpired:
            raise ConvertError("converter_timeout") from None
        if proc.returncode != 0:
            # CLI 错误体是 JSON 信封 {error:{code,message}}——code 透传
            code = "conversion_failed"
            try:
                env = (json.loads(proc.stdout or "") or {}).get("error") or {}
                code = env.get("code") or code
            except ValueError:
                pass
            raise ConvertError(code)
        try:
            report = json.loads(proc.stdout or "")
        except ValueError:
            raise ConvertError("converter_report_invalid") from None
        if not isinstance(report, dict):
            raise ConvertError("converter_report_invalid")
        if not out.is_file() or out.stat().st_size <= 0:
            raise ConvertError("conversion_output_missing")
        return report
