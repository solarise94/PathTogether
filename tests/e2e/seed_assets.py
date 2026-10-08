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
    return {key: publish_asset(owner_id, 'e2e-raster-' + key + '.bmp', buf.getvalue())
            for key in ('workbench', 'share')}
