#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""主页升级（H1-media）：工作台预览截图的独立本地被测环境。

与 tests/e2e/e2e_server.py 同款隔离模式（临时目录 + 内嵌 PostgreSQL +
仓库代码），但按 docs/homepage-onboarding-upgrade-agent-plan-20260928.md
§5「优先：真实界面截图」裁剪：

- 一个普通角色用户（预览用户；凭据仅写进程内存 + 临时凭据文件，绝不进
  日志/工件）——与默认用户角色界面一致，不含 admin 控制台；
- 两张**合成**示例切片（确定性伪随机伪 H&E 图案，无真实医疗数据）经
  离线受管理通道（slide_publish.publish_standalone）发布为 ready；
- AUTH 开启（REQUIRE_ADMIN_AUTH=1）→ 截图脚本走真实登录表单。

用法：python3 docs/review-evidence/homepage-upgrade/boot_preview_app.py \
        [--port 8917]
截图侧见同目录 shot_workbench.mjs。素材来源/取景/复跑命令记录在
docs/review-evidence/homepage-upgrade/RECORD.md。
"""

import argparse
import atexit
import json
import os
import secrets
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "tests"))

REVIEW_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(REPO_ROOT / "tests" / "e2e"))  # pg_reap（进程收割）


def synth_slide_tif(path, base=4096, seed=20260928):
    """确定性伪 H&E 合成切片（单层 tiled TIFF；真实 openslide 可读）。

    仅为展示用合成图案：浅粉间质 + 深品红腺体环 + 紫色核点；不含任何
    真实组织/患者数据，也不模仿任何具体病例。"""
    import numpy as np
    import tifffile

    rng = np.random.default_rng(seed)
    yy, xx = np.mgrid[0:base, 0:base]
    img = np.full((base, base, 3), (238, 214, 224), np.uint8)
    for _ in range(base // 96):
        cx, cy = int(rng.integers(0, base)), int(rng.integers(0, base))
        r = int(rng.integers(base // 26, base // 14))
        d = np.hypot(xx - cx, yy - cy)
        ring = (d < r) & (d > r * 0.62)
        img[ring] = (196, 118, 156)
        core = d <= r * 0.6
        img[core] = (244, 232, 238)
        nuc = (d < r * 0.95) & (d > r * 0.72) & (((xx + yy) % 7) == 0)
        img[nuc] = (122, 52, 108)
    img[((xx * yy) % 11 == 0)] = (176, 128, 168)
    with tifffile.TiffWriter(str(path)) as tw:
        tw.write(img, tile=(512, 512), photometric="rgb", compression="deflate")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8917)
    args = parser.parse_args()

    tmp = tempfile.mkdtemp(prefix="pt-preview-app-")
    os.environ["SHARE_DATA_DIR"] = os.path.join(tmp, "share-data")
    os.environ["UPLOAD_DIR"] = os.path.join(tmp, "uploads")
    os.makedirs(os.environ["SHARE_DATA_DIR"], exist_ok=True)
    os.makedirs(os.environ["UPLOAD_DIR"], exist_ok=True)
    os.environ.setdefault("UPLOAD_RESERVED_FREE_BYTES", str(16 * 1024 ** 2))

    import pgserver
    import psycopg

    pgdata = os.path.join(tmp, "pgdata")
    srv = pgserver.get_server(pgdata)
    os.environ["DATABASE_URL"] = srv.get_uri()
    os.environ["STORAGE_BACKEND"] = "postgres"

    import pg_reap
    marker = pg_reap.marker_path_for(args.port)
    pg_reap.write_marker(marker, pgdata=pgdata, tmp=tmp)
    cleanup = pg_reap.make_cleanup(pgdata, tmp, marker, lambda: srv)
    atexit.register(cleanup)
    pg_reap.install_signal_handlers()

    conn = psycopg.connect(os.environ["DATABASE_URL"])
    try:
        import pg_store
        pg_store.ensure_schema(conn)
    finally:
        conn.close()

    user_pw = secrets.token_urlsafe(24)
    owner_pw = secrets.token_urlsafe(24)
    pw_file = Path(tmp) / "bootstrap-owner-pw"
    pw_file.write_text(owner_pw, encoding="utf-8")
    pw_file.chmod(0o600)
    os.environ["BOOTSTRAP_OWNER_LOGIN_ID"] = "preview-owner@pt.test"
    os.environ["BOOTSTRAP_OWNER_PASSWORD_FILE"] = str(pw_file)
    os.environ["REQUIRE_ADMIN_AUTH"] = "1"
    os.environ["PUBLIC_BASE_URL"] = "http://127.0.0.1:%d" % args.port
    os.environ["SHARE_BASE_URL"] = "http://127.0.0.1:%d" % (args.port + 1)

    # 启动引导先建 owner（app import 期执行；用户库须为空），再种普通
    # 预览用户与合成示例切片（顺序与 tests/e2e/e2e_server.py 一致）。
    import _bootstrap  # noqa: F401  # openslide 真库存在时不注册 stub
    import app as app_mod

    import user_store
    created = user_store.create_user(
        "preview@pt.test", user_pw, role="user", display_name="预览用户")
    uid = (created or {}).get("user_id") or ""
    assert uid, "预览用户建号失败"

    # 合成示例切片 → 离线受管理通道发布（与测试 publish_test_slide 同款）
    import hashlib

    import slide_publish
    import slide_storage
    import slide_store

    for idx, (name, seed) in enumerate(
            [("示例切片 A.tif", 20260928), ("示例切片 B.tif", 99031102)]):
        data_path = Path(tmp) / ("synth-%d.tif" % idx)
        synth_slide_tif(data_path, seed=seed)
        data = data_path.read_bytes()
        c2 = psycopg.connect(os.environ["DATABASE_URL"])
        c2.row_factory = psycopg.rows.dict_row
        try:
            with c2.transaction():
                desc = slide_store.allocate_slide(
                    uid, original_filename=name, format_ext="tif", conn=c2)
        finally:
            c2.close()
        staging = slide_storage.staging_dir(
            "preview-" + desc.slide_id, "1", root=os.environ["UPLOAD_DIR"])
        staging.mkdir(parents=True, exist_ok=True)
        (staging / "data.tif").write_bytes(data)
        sha = hashlib.sha256(data).hexdigest()
        manifest = slide_publish.build_manifest("data.tif", len(data), sha)
        slide_publish.publish_standalone(
            desc.slide_id, manifest, staging, sha256=sha,
            accounted_bytes=len(data), upload_root=os.environ["UPLOAD_DIR"])

    creds = Path(os.environ.get("PREVIEW_CREDS_FILE")
                 or (Path(tempfile.gettempdir())
                     / ("pt-preview-creds-%d.json" % args.port)))
    creds.write_text(json.dumps({
        "baseUrl": "http://127.0.0.1:%d" % args.port,
        "login": "preview@pt.test",
        "password": user_pw,
    }), encoding="utf-8")
    creds.chmod(0o600)
    print("PREVIEW_READY %s" % creds, flush=True)

    import share_server
    import threading
    threading.Thread(
        target=lambda: share_server.app.run(
            host="127.0.0.1", port=args.port + 1, threaded=True),
        name="preview-share-server", daemon=True).start()

    app_mod.app.run(host="127.0.0.1", port=args.port, threaded=True)


if __name__ == "__main__":
    main()
