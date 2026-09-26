#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把 Demo 目录换成 4 张已在 UPLOAD_DIR 的 TCGA 公开诊断切片。

P4-app（合同 §6.2）回填式登记：幂等——切片已有**可复用** slides 行
（既有 legacy 布局 ready 行）则复用其 slide_id；否则 allocate → 复制进
受管理 staging → ``slide_publish.publish_standalone``（objects/<slide_id>/
独占包 + ready 收口——不再经 set_slide_meta 按名建行）。已在目录则更新
展示名/排序。合成切片（synth-*.tiff / uitest-synth.tiff）移出 Demo
allowlist，文件保留。TCGA DX 切片为 GDC 公开、已脱敏诊断切片，仅用于
研究/教学/软件演示。

owner 解析：``share_store.get_owner_user_id()``（部署注入的配置 owner）；
未配置则报错退出（allocate_slide 不允许空 owner 自动认领）。

运行（平台容器内，需 STORAGE_BACKEND=postgres）：

    python3 scripts/seed_demo_tcga_catalog.py
"""
from pathlib import Path
import hashlib
import os
import sys

# 容器 WORKDIR=/app；本地也可从仓库根运行。stdin / python - 无 __file__。
_ROOT = Path("/app") if not globals().get("__file__") else Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

import demo_store  # noqa: E402
import share_store  # noqa: E402
import slide_publish  # noqa: E402
import slide_storage  # noqa: E402
import slide_store  # noqa: E402

UPLOAD_DIR = Path(os.environ.get("UPLOAD_DIR") or (Path.home() / "svs-viewer" / "uploads"))
STAGING_TASK = "seed-demo-tcga"

# filename, display_name(zh), description(zh), display_name(en), description(en),
# sort_order, is_default
TCGA_SLIDES = (
    (
        "TCGA-49-AAR4-01Z-00-DX1.EDB32358-AF23-4F81-A99F-15574A2DE28E.svs",
        "肺腺癌 TCGA-49-AAR4",
        "TCGA-LUAD 公开诊断切片（已脱敏）。仅用于研究与软件演示，不用于临床诊断。",
        "Lung adenocarcinoma TCGA-49-AAR4",
        "TCGA-LUAD public diagnostic slide (de-identified). For research and "
        "software demonstration only; not for clinical diagnosis.",
        0,
        True,
    ),
    (
        "TCGA-86-8668-01Z-00-DX1.d720d486-02c7-4f98-8feb-e0e50a12c158.svs",
        "肺腺癌 TCGA-86-8668",
        "TCGA-LUAD 公开诊断切片（已脱敏）。仅用于研究与软件演示，不用于临床诊断。",
        "Lung adenocarcinoma TCGA-86-8668",
        "TCGA-LUAD public diagnostic slide (de-identified). For research and "
        "software demonstration only; not for clinical diagnosis.",
        1,
        False,
    ),
    (
        "TCGA-BC-A10Q-01Z-00-DX1.A2D1E6CD-73DA-49FF-B291-5A4FDB32808A.svs",
        "肝细胞癌 TCGA-BC-A10Q",
        "TCGA-LIHC 公开诊断切片（已脱敏）。仅用于研究与软件演示，不用于临床诊断。",
        "Hepatocellular carcinoma TCGA-BC-A10Q",
        "TCGA-LIHC public diagnostic slide (de-identified). For research and "
        "software demonstration only; not for clinical diagnosis.",
        2,
        False,
    ),
    (
        "TCGA-FV-A3R2-01Z-00-DX1.B9E286ED-B4A3-44E7-B11F-F2B763083FBC.svs",
        "胆管癌 TCGA-FV-A3R2",
        "TCGA-CHOL 公开诊断切片（已脱敏）。仅用于研究与软件演示，不用于临床诊断。",
        "Cholangiocarcinoma TCGA-FV-A3R2",
        "TCGA-CHOL public diagnostic slide (de-identified). For research and "
        "software demonstration only; not for clinical diagnosis.",
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


def _sha256_file(path: Path, chunk=1 << 20):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for buf in iter(lambda: fh.read(chunk), b""):
            h.update(buf)
    return h.hexdigest()


def register_slide(name: str, owner_user_id: str):
    """回填式登记单张切片（P4-app 合同 §6.2）。

    - 已有可复用行（legacy_filename 命中且 asset_state='ready'——既有 demo
      环境的 legacy 布局资产）→ 复用其 slide_id（幂等，不重复建资产）；
    - 否则 allocate → 复制进 ``.staging/seed-demo-tcga/<n>/`` →
      publish_standalone（objects/<slide_id>/ 独占包；源平铺文件保留——
      P6 历史资产物理迁移统一搬运，不在种子脚本删源）。
    """
    existing = slide_store.resolve_legacy_alias(name)
    if existing is not None and existing.asset_state == "ready":
        return existing.slide_id, "reused"
    src = UPLOAD_DIR / name
    ext = name.rsplit(".", 1)[-1].lower()
    staging_dir = slide_storage.staging_dir(
        STAGING_TASK, "item-%s" % hashlib.sha256(
            name.encode("utf-8")).hexdigest()[:8], root=UPLOAD_DIR)
    staged = staging_dir / ("data." + ext)
    import shutil
    try:
        staging_dir.mkdir(parents=True, exist_ok=False)
        shutil.copyfile(src, staged)
        size = staged.stat().st_size
        sha = _sha256_file(staged)
        import psycopg.rows
        import pg_store
        conn = pg_store.connect()
        conn.row_factory = psycopg.rows.dict_row
        try:
            with pg_store.transaction(conn):
                desc = slide_store.allocate_slide(
                    owner_user_id, original_filename=name, format_ext=ext,
                    conn=conn)
        finally:
            conn.close()
        manifest = slide_publish.build_manifest(
            "data." + ext, size, sha)
        slide_publish.publish_standalone(
            desc.slide_id, manifest, staging_dir, sha256=sha,
            accounted_bytes=size, upload_root=UPLOAD_DIR)
        slide_storage.remove_staging_tree(STAGING_TASK, root=UPLOAD_DIR)
    except Exception:
        shutil.rmtree(staging_dir, ignore_errors=True)
        raise
    return desc.slide_id, "published"


def main():
    missing = [name for name, *_ in TCGA_SLIDES if not (UPLOAD_DIR / name).is_file()]
    if missing:
        raise SystemExit("UPLOAD_DIR 缺少切片：\n  " + "\n  ".join(missing))

    owner = (share_store.get_owner_user_id() or "").strip()
    if not owner:
        raise SystemExit(
            "未配置部署 owner（share_store.get_owner_user_id 为空）——"
            "allocate_slide 不允许空 owner 自动认领")

    for name, display, desc, display_en, desc_en, order, is_default in TCGA_SLIDES:
        slide_id, how = register_slide(name, owner)
        demo_store.catalog_add(
            slide_id,
            display_name=display,
            description=desc,
            sort_order=order,
            added_by="owner",
            display_name_en=display_en,
            description_en=desc_en,
        )
        if is_default:
            demo_store.catalog_set_default(slide_id)
        print("catalog+ %s  %s  default=%s  (%s)" % (
            slide_id, name, is_default, how))

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
