# -*- coding: utf-8 -*-
"""百度导入（W5）测试公共装配。

- 统一设置 ``BAIDU_SHARE_SECRET_KEY``（store 加密必需）；
- ``install_fake``：把 :class:`FakeBaiduAdapter` 注入
  ``baidu_import_store.get_adapter``（生产代码只经 ``get_adapter`` 取
  适配器，测试 monkeypatch 该入口，绝不在生产进程设 ``BAIDU_ADAPTER``）；
- 标准分享树（B03/B04/B11 用）与租约过期等 PG 小工具。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import _bootstrap  # noqa: F401,E402

import psycopg  # noqa: E402

import baidu_import_store as store  # noqa: E402
from baidu_adapter import FakeBaiduAdapter  # noqa: E402

TEST_SECRET = "test-baidu-share-secret-key-2026-09-14"

#: 标准分享树（目录隐式派生自文件路径）：
#:   A1/sample.svs .ome.tif .ome.tiff panel.kfbf + panel_kfbf/channel.json
#:   A1/scan.mrxs（无同名伴随目录 → bundle_incomplete）
#:   B1/big.kfb、B1/deep.mrxs + B1/deep/Slidedat.ini（伴随目录存在 →
#:   baidu_bundle_unsupported）、B1/notes.txt（unsupported）
STANDARD_ENTRIES = [
    {"path": "/A1/sample.svs", "size": 1000},
    {"path": "/A1/scan.ome.tif", "size": 2000},
    {"path": "/A1/scan2.ome.tiff", "size": 3000},
    {"path": "/A1/panel.kfbf", "size": 500},
    {"path": "/A1/panel_kfbf/channel.json", "size": 100},
    {"path": "/A1/scan.mrxs", "size": 2048},
    {"path": "/B1/big.kfb", "size": 700},
    {"path": "/B1/deep.mrxs", "size": 4096},
    {"path": "/B1/deep/Slidedat.ini", "size": 10},
    {"path": "/B1/notes.txt", "size": 10},
]


def install_fake(monkeypatch, entries=None, extraction_code=None,
                 page_size=100):
    """装配：secret env + fake 适配器注入 store.get_adapter。"""
    monkeypatch.setenv("BAIDU_SHARE_SECRET_KEY", TEST_SECRET)
    fake = FakeBaiduAdapter(entries=entries or STANDARD_ENTRIES,
                            extraction_code=extraction_code,
                            page_size=page_size)
    monkeypatch.setattr(store, "get_adapter", lambda: fake)
    return fake


def _connect():
    return psycopg.connect(os.environ["DATABASE_URL"], autocommit=True)


def expire_batch_lease(batch_id):
    """把批次租约拨到过去（模拟崩溃后等待租约过期再重领）。"""
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE baidu_import_batches SET lease_expires_at = "
                "now() - interval '1 second' WHERE id = %s", (batch_id,))
    finally:
        conn.close()


def expire_enumeration_lease(enumeration_id):
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE baidu_enumerations SET lease_expires_at = "
                "now() - interval '1 second' WHERE id = %s", (enumeration_id,))
    finally:
        conn.close()


def set_expires_at(enumeration_id, sql_interval):
    """直接拨枚举有效期（如 \"- 1 hour\" 构造过期）。"""
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE baidu_enumerations "
                "SET expires_at = now() + %s::interval WHERE id = %s",
                (sql_interval, enumeration_id))
    finally:
        conn.close()


def set_max_entries(enumeration_id, n):
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE baidu_enumerations SET max_entries=%s WHERE id=%s",
                (int(n), enumeration_id))
    finally:
        conn.close()


def make_ready_enumeration(monkeypatch, owner="u1", entries=None,
                           share_text=None, extraction_code=None):
    """构造已 ready 的枚举，返回 (fake, enum_id, candidates dict by path)。"""
    fake = install_fake(monkeypatch, entries=entries,
                        extraction_code=extraction_code)
    text = share_text or (
        "链接: https://pan.baidu.com/s/1TestShareId99 提取码: ab12"
        if extraction_code else "https://pan.baidu.com/s/1TestShareId99")
    out = store.create_enumeration(owner, text, None)
    result = store.run_enumeration(out["id"], fake)
    assert result["state"] == "ready", result
    cands = store.list_candidates(out["id"], owner, limit=100)
    by_path = {c["relative_path"]: c for c in cands["items"]}
    return fake, out["id"], by_path


def create_batch(monkeypatch, owner="u1", enum_id=None, paths=None,
                 idempotency_key=None, quota_hook=None, entries=None,
                 extraction_code=None):
    """一步构造 queued 批次（枚举 ready + create_import）。"""
    fake, enum_id, by_path = make_ready_enumeration(
        monkeypatch, owner=owner, entries=entries,
        extraction_code=extraction_code) if enum_id is None \
        else (None, enum_id, None)
    if paths is not None:
        cands = store.list_candidates(enum_id, owner, limit=100)
        by_path = {c["relative_path"]: c for c in cands["items"]}
        candidate_ids = [by_path[p]["id"] for p in paths]
    else:
        candidate_ids = None
    batch = store.create_import(
        owner, enum_id, candidate_ids, idempotency_key=idempotency_key,
        quota_hook=quota_hook)
    return fake, enum_id, batch
