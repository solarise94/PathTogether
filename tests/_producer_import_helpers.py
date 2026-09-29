# -*- coding: utf-8 -*-
"""C5 producer 导入测试公共装配（docs/slide-tools/c5-producer-import-contract.md §7）。

- ``ProducerEnv``：user（role=user，配额主体）+ 项目 + 已批准 slide:import 的
  安装行 + 用户导入委托 grant——begin 所需的全部身份素材；
- ``PluginClient``：受控替身插件后端——经**真实** /api/plugin/v1/auth/token
  换发 plugin JWT（secret → token），再调 producer import 各端点；
- ``build_deliverable``：合成金字塔 BigTIFF 交付物（平台 probe 真实通过；
  T1 的 native core 真实转换产物见 test_producer_import_native_chain.py）。
"""
import hashlib
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import _bootstrap  # noqa: F401,E402

import numpy as np  # noqa: E402
import tifffile  # noqa: E402

import producer_import_store as pim  # noqa: E402
import share_store  # noqa: E402
import user_store  # noqa: E402

#: 测试插件 id（与 begin 侧 plugin_id 记账一致；非百度驱动白名单成员）。
TEST_PLUGIN_ID = "dev.pathtogether.producer-test"


class ProducerEnv:
    """begin 所需身份素材（每用例独立构建）。"""

    def __init__(self, tmp_path, *, scratch_bytes=1024, role="user"):
        import app as app_mod
        import share_server
        self.tmp = Path(tmp_path)
        self.upload_dir = Path(os.environ["UPLOAD_DIR"])
        self.user = user_store.create_user(
            "c5-%s@example.com" % os.urandom(4).hex(),
            "pass1234pass1234", role=role)
        self.uid = self.user["user_id"]
        self.project = share_store.create_project(
            "C5P-%s" % os.urandom(3).hex(), owner_user_id=self.uid)
        self.pid = self.project["pid"]
        inst = share_store.create_plugin_installation(
            TEST_PLUGIN_ID, approved_scopes=["slide:import"])
        self.installation = inst
        self.installation_id = inst["installation_id"]
        self.secret = inst["secret"]
        self.grant = pim.create_import_grant(
            self.uid, self.installation_id, TEST_PLUGIN_ID, self.pid)
        self.grant_id = self.grant["grant_id"]
        del app_mod, share_server  # 仅确保模块已按 env 初始化


class PluginClient:
    """受控替身插件后端（真实 token 端点 + producer import API）。"""

    def __init__(self, flask_client, env):
        self.c = flask_client
        self.env = env
        self._token = None

    def token(self, force=False):
        if self._token is None or force:
            r = self.c.post("/api/plugin/v1/auth/token", json={
                "installation_id": self.env.installation_id,
                "secret": self.env.secret})
            assert r.status_code == 200, r.get_json()
            self._token = r.get_json()["access_token"]
        return self._token

    def _h(self, write_token=None, idem=None, extra=None):
        h = {"Authorization": "Bearer " + self.token()}
        if write_token:
            h["X-Import-Token"] = write_token
        if idem:
            h["Idempotency-Key"] = idem
        if extra:
            h.update(extra)
        return h

    def begin(self, payload, idem):
        return self.c.post("/api/plugin/v1/imports/begin",
                           headers=self._h(idem=idem), json=payload)

    def begin_payload(self, filename, data, *, scratch_bytes=1024,
                      project_id=None, grant_id=None, profile=None,
                      format_ext="tif", declared_size=None, extra=None):
        payload = {
            "grant_id": grant_id or self.env.grant_id,
            "project_id": project_id if project_id is not None else self.env.pid,
            "filename": filename,
            "format_ext": format_ext,
            "declared_size": declared_size if declared_size is not None
            else len(data),
            "scratch_bytes": scratch_bytes,
            "profile": profile if profile is not None else
            {"photometric": "brightfield", "channels": 3},
        }
        if extra:
            payload.update(extra)
        return payload

    def write(self, import_id, write_token, offset, chunk):
        return self.c.post(
            "/api/plugin/v1/imports/%s/write" % import_id,
            headers=self._h(write_token=write_token, extra={
                "X-Import-Offset": str(offset),
                "X-Import-Chunk-Sha256": hashlib.sha256(chunk).hexdigest(),
            }), data=chunk, content_type="application/octet-stream")

    def commit(self, import_id, write_token, declared_sha256=None):
        body = {}
        if declared_sha256:
            body["declared_sha256"] = declared_sha256
        return self.c.post("/api/plugin/v1/imports/%s/commit" % import_id,
                           headers=self._h(write_token=write_token),
                           json=body)

    def status(self, import_id):
        return self.c.get("/api/plugin/v1/imports/%s/status" % import_id,
                          headers=self._h())

    def cancel(self, import_id, write_token):
        return self.c.post("/api/plugin/v1/imports/%s/cancel" % import_id,
                           headers=self._h(write_token=write_token), json={})

    def scratch(self, import_id, write_token, *, delta=None, total=None):
        body = {}
        if delta is not None:
            body["delta_bytes"] = delta
        if total is not None:
            body["total_bytes"] = total
        return self.c.post("/api/plugin/v1/imports/%s/scratch" % import_id,
                           headers=self._h(write_token=write_token),
                           json=body)

    def topup(self, import_id, write_token, extra_bytes):
        return self.c.post("/api/plugin/v1/imports/%s/topup" % import_id,
                           headers=self._h(write_token=write_token),
                           json={"extra_bytes": extra_bytes})

    def cleanup_confirm(self, import_id, write_token):
        return self.c.post(
            "/api/plugin/v1/imports/%s/cleanup-confirm" % import_id,
            headers=self._h(write_token=write_token), json={})

    def deliver_all(self, data, *, idem, chunk_size=None, filename="bf.tif",
                    declared_sha256=None, scratch_bytes=1024):
        """begin → 全量分块 write → commit（替身全链路的公共段）。"""
        r = self.begin(self.begin_payload(filename, data,
                                          scratch_bytes=scratch_bytes), idem)
        assert r.status_code == 201, r.get_json()
        b = r.get_json()
        iid, wt = b["import_id"], b["write_token"]
        step = chunk_size or max(1, len(data) // 2)
        off = 0
        while off < len(data):
            chunk = data[off:off + step]
            r = self.write(iid, wt, off, chunk)
            assert r.status_code == 200, (r.status_code, r.get_json())
            off += len(chunk)
        r = self.commit(iid, wt, declared_sha256=declared_sha256)
        return iid, wt, r


def build_deliverable(path, *, size=128, levels=1):
    """合成金字塔 BigTIFF（tiled；openslide 可开、probe 通过）。返回 bytes。"""
    a = (np.arange(size * size * 3).reshape(size, size, 3) % 255) \
        .astype("uint8")
    with tifffile.TiffWriter(str(path), bigtiff=True) as w:
        w.write(a, photometric="rgb", tile=(32, 32))
        half = a[::2, ::2]
        for _ in range(max(0, levels - 1)):
            w.write(half, photometric="rgb", tile=(32, 32))
            half = half[::2, ::2]
    return Path(path).read_bytes()


def sql_one(query, args=()):
    """PG 单行查询小助手（dict 行）。"""
    import pg_store
    import psycopg
    conn = pg_store.connect()
    conn.row_factory = psycopg.rows.dict_row
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                cur.execute(query, args)
                return cur.fetchone()
    finally:
        conn.close()
