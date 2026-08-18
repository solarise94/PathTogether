#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把 Demo 目录换成 4 张已在 UPLOAD_DIR 的 TCGA 公开诊断切片。

幂等：切片已入库则复用 slide_id；已在目录则更新展示名/排序。
合成切片（synth-*.tiff / uitest-synth.tiff）移出 Demo allowlist，文件保留。
TCGA DX 切片为 GDC 公开、已脱敏诊断切片，仅用于研究/教学/软件演示。

运行（平台容器内，需 STORAGE_BACKEND=postgres）：

    python3 scripts/seed_demo_tcga_catalog.py
"""
from pathlib import Path
import os
import sys

# 容器 WORKDIR=/app；本地也可从仓库根运行。stdin / python - 无 __file__。
_ROOT = Path("/app") if not globals().get("__file__") else Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

import demo_store  # noqa: E402
import share_store  # noqa: E402

UPLOAD_DIR = Path(os.environ.get("UPLOAD_DIR") or (Path.home() / "svs-viewer" / "uploads"))

# filename, display_name, description, sort_order, is_default
TCGA_SLIDES = (
    (
        "TCGA-49-AAR4-01Z-00-DX1.EDB32358-AF23-4F81-A99F-15574A2DE28E.svs",
        "肺腺癌 TCGA-49-AAR4",
        "TCGA-LUAD 公开诊断切片（已脱敏）。仅用于研究与软件演示，不用于临床诊断。",
        0,
        True,
    ),
    (
        "TCGA-86-8668-01Z-00-DX1.d720d486-02c7-4f98-8feb-e0e50a12c158.svs",
        "肺腺癌 TCGA-86-8668",
        "TCGA-LUAD 公开诊断切片（已脱敏）。仅用于研究与软件演示，不用于临床诊断。",
        1,
        False,
    ),
    (
        "TCGA-BC-A10Q-01Z-00-DX1.A2D1E6CD-73DA-49FF-B291-5A4FDB32808A.svs",
        "肝细胞癌 TCGA-BC-A10Q",
        "TCGA-LIHC 公开诊断切片（已脱敏）。仅用于研究与软件演示，不用于临床诊断。",
        2,
        False,
    ),
    (
        "TCGA-FV-A3R2-01Z-00-DX1.B9E286ED-B4A3-44E7-B11F-F2B763083FBC.svs",
        "胆管癌 TCGA-FV-A3R2",
        "TCGA-CHOL 公开诊断切片（已脱敏）。仅用于研究与软件演示，不用于临床诊断。",
        3,
        False,
    ),
)

REMOVE_FROM_CATALOG = (
    "synth-sparse.tiff",
    "synth-dense.tiff",
    "synth-heterogeneous.tiff",
    "uitest-synth.tiff",
)


def main():
    missing = [name for name, *_ in TCGA_SLIDES if not (UPLOAD_DIR / name).is_file()]
    if missing:
        raise SystemExit("UPLOAD_DIR 缺少切片：\n  " + "\n  ".join(missing))

    for name, display, desc, order, is_default in TCGA_SLIDES:
        share_store.set_slide_meta(name)
        slide_id = share_store.get_slide_id(name)
        if not slide_id:
            raise SystemExit("无法解析 slide_id：%s" % name)
        demo_store.catalog_add(
            slide_id,
            display_name=display,
            description=desc,
            sort_order=order,
            added_by="owner",
        )
        if is_default:
            demo_store.catalog_set_default(slide_id)
        print("catalog+ %s  %s  default=%s" % (slide_id, name, is_default))

    for name in REMOVE_FROM_CATALOG:
        slide_id = share_store.get_slide_id(name)
        if not slide_id:
            print("skip- 未入库 %s" % name)
            continue
        result = demo_store.catalog_remove(slide_id)
        print("catalog- %s  %s  %s" % (
            slide_id, name, "removed" if result else "not-in-catalog"))

    print("--- Demo 目录 ---")
    for entry in demo_store.catalog_list_ordered():
        filename = demo_store.resolve_slide_filename(entry["slide_id"])
        print("  %s  default=%s  %s  %s" % (
            entry["sort_order"], entry["is_default"],
            entry.get("display_name") or "", filename))


if __name__ == "__main__":
    main()
