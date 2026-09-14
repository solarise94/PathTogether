#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""离线 KFBF（荧光）→ 多通道 OME-TIFF 转换 CLI。

用法：
    python scripts/convert_kfbf.py SRC.kfbf OUT.ome.tif

成功：写 OUT.ome.tif + OUT.ome.tif.manifest.json + OUT.ome.tif.associated/
*.jpg，stdout 打印一行 JSON 摘要，退出码 0。
失败：stderr 打印稳定错误码（KfbError.code），退出码 1。
"""

import json
import os
import sys

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from kfb import KfbError, convert_kfbf  # noqa: E402


def main(argv):
    if len(argv) != 3:
        sys.stderr.write(
            "用法: %s SRC.kfbf OUT.ome.tif\n" % os.path.basename(argv[0]))
        return 2
    src, dst = argv[1], argv[2]
    try:
        manifest = convert_kfbf(src, dst)
    except KfbError as e:
        sys.stderr.write("%s\n" % e.code)
        return 1
    sys.stdout.write(json.dumps({
        "output": dst,
        "manifest": dst + ".manifest.json",
        "levels": len(manifest["levels"]),
        "dimensions": manifest["dimensions"],
        "channels": [c["name"] for c in manifest["channels"]],
        "warnings": manifest["warnings"],
    }, ensure_ascii=False))
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
