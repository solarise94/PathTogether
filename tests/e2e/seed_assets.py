"""Offline E2E assets: real bytes through the production publication path.

Only imported by the isolated E2E server. No test conftest imports, upload
credentials, external storage, or production HTTP fixture routes.
"""
import hashlib
import io
import secrets

import numpy as np
import pg_store
import psycopg.rows
import slide_publish
import slide_storage
import slide_store
import tifffile
from PIL import Image


def publish_asset(owner_id, name, data):
    ext = name.rsplit('.', 1)[1]
    with pg_store.connect() as conn:
        conn.row_factory = psycopg.rows.dict_row
        with pg_store.transaction(conn):
            desc = slide_store.allocate_slide(
                owner_user_id=owner_id, original_filename=name,
                format_ext=ext, conn=conn)
    staging = slide_storage.staging_dir('tst-' + secrets.token_hex(8), '1')
    staging.mkdir(parents=True, exist_ok=True)
    basename = 'data.' + ext
    (staging / basename).write_bytes(data)
    sha = hashlib.sha256(data).hexdigest()
    manifest = slide_publish.build_manifest(basename, len(data), sha)
    slide_publish.publish_standalone(
        desc.slide_id, manifest, staging, sha256=sha, accounted_bytes=len(data))
    return {'slide_id': desc.slide_id, 'name': name}


def seed_admin_inventory_rows(owner_id):
    """管理台切片清单状态筛选 E2E 的墓碑/失败行（2026-10-20 评审 §四）。

    只建**不可读**行（deleted 墓碑 + legacy failed 且入口文件缺失），
    因此普通 /api/slides（只认 ready）与 Viewer 各 spec 的可见集完全不变：

      - ``e2e-admin-deleted.bmp``：deleted 墓碑（默认「未删除」档必须排除它）；
      - ``e2e-admin-missing.svs``：legacy 布局 failed、入口文件缺失 →
        后端按现状推断 missing_file + inferred=True（「历史文件缺失（推断）」
        文案的真实来源）。

    返回 name → slide_id（spec 断言用）。
    """

    def _allocate(name, ext):
        with pg_store.connect() as conn:
            conn.row_factory = psycopg.rows.dict_row
            with pg_store.transaction(conn):
                return slide_store.allocate_slide(
                    owner_user_id=owner_id, original_filename=name,
                    format_ext=ext, conn=conn)

    def _rewrite(slide_id, sql, params):
        with pg_store.connect() as conn:
            with pg_store.transaction(conn):
                with conn.cursor() as cur:
                    cur.execute(sql, params)

    out = {}
    deleted = _allocate('e2e-admin-deleted.bmp', 'bmp')
    _rewrite(deleted.slide_id,
             "UPDATE slides SET asset_state='deleted', "
             "legacy_filename='e2e-admin-deleted.bmp' WHERE slide_id=%s",
             (deleted.slide_id,))
    out['deleted'] = {'slide_id': deleted.slide_id,
                      'name': 'e2e-admin-deleted.bmp'}

    legacy = _allocate('e2e-admin-missing.svs', 'svs')
    _rewrite(legacy.slide_id,
             "UPDATE slides SET asset_state='failed', "
             "storage_layout='legacy', storage_relpath=NULL, "
             "legacy_filename='e2e-admin-missing.svs' WHERE slide_id=%s",
             (legacy.slide_id,))
    out['legacy_failed'] = {'slide_id': legacy.slide_id,
                            'name': 'e2e-admin-missing.svs'}
    return out


def seed_viewer_assets(owner_id):
    img = (np.indices((64, 96)).sum(axis=0) % 256).astype(np.uint8)
    rgb = np.stack([img, img[::-1], img[:, ::-1]], axis=-1)
    buf = io.BytesIO()
    tifffile.imwrite(buf, rgb, photometric='rgb', tile=(32, 32))
    publish_asset(owner_id, 'e2e-temp-view.tif', buf.getvalue())
    y, x = np.indices((240, 320))
    raster = np.stack([(x * 3 + y * 11) % 256, (y * 5) % 256,
                       (x * 7) % 256], axis=-1).astype(np.uint8)
    buf = io.BytesIO()
    Image.fromarray(raster).save(buf, format='BMP')
    # Each test has its own asset: failure/order of one cannot block the other.
    assets = {key: publish_asset(owner_id, 'e2e-raster-' + key + '.bmp', buf.getvalue())
              for key in ('workbench', 'share')}
    # 框选指针手势回归（docs/rect-draft-ai-context-opus-handoff-20261018.md §3.3）：
    # 真实 JPG（无物理标尺）与既有 BMP 各跑同一状态机场景。
    yj, xj = np.indices((480, 640))
    jpg_img = np.stack([(xj * 2 + yj) % 256, (yj * 3) % 256,
                        (xj * 5 + yj * 7) % 256], axis=-1).astype(np.uint8)
    jbuf = io.BytesIO()
    Image.fromarray(jpg_img).save(jbuf, format='JPEG', quality=92)
    assets['rectDraftJpg'] = publish_asset(
        owner_id, 'e2e-rect-draft.jpg', jbuf.getvalue())
    # folder-experience E2E：10 张未归类小图——保证根目录堆叠 ≥2 页（每叠 ≤8）。
    assets['folderSlides'] = [
        publish_asset(owner_id, 'e2e-fb-%02d.bmp' % i, buf.getvalue())
        for i in range(1, 11)]
    return assets
