# -*- coding: utf-8 -*-
"""C5-B 插件测试装配。

- stub 平台（进程内 HTTP，忠实合同可测面）；
- 合成夹具：原生 CLI（gen-kfb/gen-kfbf）生成真实 KFB/KFBF（不使用任何
  私有样本；真实百度账号属外部门禁，fake 源成功不宣称真实可用）；
- 快速退避/短超时 env 覆盖（避免慢用例）。

TMPDIR=$PWD/.gate-tmp（/tmp 是小 tmpfs）——大块 scratch 一律走
``.gate-tmp/``。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
PLUGIN_ROOT = HERE.parent
REPO_ROOT = PLUGIN_ROOT.parents[1]

sys.path.insert(0, str(PLUGIN_ROOT))

from stub_platform import StubPlatformState, serve  # noqa: E402

#: 原生 CLI（slide-transform-core/target/release/slide-transform）
CLI = REPO_ROOT / "slide-transform-core" / "target" / "release" \
    / "slide-transform"

FAST_ENV = {
    "PT_HTTP_MAX_ATTEMPTS": "3",
    "PT_HTTP_BACKOFF_BASE": "0.05",
    "PT_HTTP_RATE_LIMIT_MAX_WAITS": "6",
    "PT_HTTP_CONNECT_TIMEOUT": "5",
    "PT_HTTP_CONTROL_TIMEOUT": "10",
    "PT_HTTP_WRITE_TIMEOUT": "30",
    "PT_HTTP_COMMIT_TIMEOUT": "30",
    "PT_BAIDU_LEASE_SECONDS": "120",
    "PT_BAIDU_HEARTBEAT_INTERVAL": "0.2",
    "PT_BAIDU_CLAIM_POLL_SECONDS": "0.05",
    "PT_CLEANUP_MAX_ATTEMPTS": "5",
    "PT_CLEANUP_BACKOFF_BASE": "0.02",
    "PT_RECEIPT_POLL_SECONDS": "0.05",
    "PT_RECEIPT_POLL_MAX": "40",
    "BAIDU_SOURCE_MAX_ATTEMPTS": "3",
    "BAIDU_SOURCE_BACKOFF_BASE": "0.05",
}


def pytest_configure(config):
    config.addinivalue_line(
        "markers", "c5b: C5-B baidu import plugin tests")


@pytest.fixture(scope="session")
def native_cli():
    if not CLI.is_file():
        pytest.skip(
            "native CLI missing: %s（build: PATH=$HOME/.cargo/bin:$PATH "
            "bash scripts/build_slide_transform.sh）" % CLI)
    return CLI


@pytest.fixture(scope="session")
def fixtures_dir(tmp_path_factory, native_cli):
    """合成 KFB / KFBF / channel.json（会话级，一次生成多次复用）。"""
    d = tmp_path_factory.mktemp("c5b-fixtures")
    bf = d / "sample-bf.kfb"
    fl = d / "sample-fl.kfbf"
    # 尺寸与既有浏览器/平台用例同参（部分小尺寸合成 KFBF 会触发核心
    # 校验拒绝 IFD 布局错位——沿用已验证参数）
    subprocess.run([str(native_cli), "gen-kfb", str(bf),
                    "--width", "580", "--height", "300"], check=True)
    subprocess.run([str(native_cli), "gen-kfbf", str(fl),
                    "--width", "600", "--height", "400"], check=True)
    # KFBF 伴随 channel.json（<名>_kfbf/Annotations/；通道名/颜色为合成值）
    channel = {
        "channels": [
            {"name": "DAPI", "color": "#0000ff", "exposure_us": 100},
            {"name": "FITC", "color": "#00ff00", "exposure_us": 120},
            {"name": "Cy5", "color": "#ff0000", "exposure_us": 140},
        ]
    }
    comp_dir = d / "sample-fl_kfbf" / "Annotations"
    comp_dir.mkdir(parents=True, exist_ok=True)
    (comp_dir / "channel.json").write_text(
        json.dumps(channel, ensure_ascii=False), encoding="utf-8")
    return {"dir": d, "kfb": bf, "kfbf": fl,
            "channel_json": comp_dir / "channel.json",
            "kfb_bytes": bf.read_bytes(), "kfbf_bytes": fl.read_bytes()}


class _Sandbox:
    """一次用例的完整环境：stub 平台 + worker 组件（可重建模拟重启）。"""

    def __init__(self, tmp_path, fixtures, *, chunk_max_bytes=8192):
        self.tmp = Path(tmp_path)
        self.fixtures = fixtures
        self.share_data = self.tmp / "share-data"
        self.share_data.mkdir(parents=True, exist_ok=True)
        self.state = StubPlatformState(
            share_data_dir=self.share_data,
            installation_id="inst_c5b",
            secret="c5b-secret",
            chunk_max_bytes=chunk_max_bytes)
        self.server, self.base_url = serve(self.state)
        self.state.add_grant(
            "pig_c5b_main", project_id="prj_test",
            user_id="usr_owner", ttl_seconds=3600)

    def env(self, **extra):
        out = dict(FAST_ENV)
        out.update({
            "PT_PLATFORM_URL": self.base_url,
            "PT_INSTALLATION_ID": self.state.installation_id,
            "PT_INSTALLATION_SECRET": self.state.secret,
            "PT_PLUGIN_WORK_ROOT": str(self.share_data / "plugin-work"),
            "PT_IMPORT_GRANTS": "prj_test:pig_c5b_main",
            "BAIDU_SOURCE": "fake",
            "SLIDE_TRANSFORM_BIN": str(CLI),
            "SLIDE_TRANSFORM_TIMEOUT_SECONDS": "300",
            "PT_HEALTH_PORT": "0",
        })
        out.update(extra)
        return out

    def fake_source_with(self, entries, **kw):
        from worker.source.fake import FakeSource
        src = FakeSource(entries, **kw)
        return src

    def build_ctx(self, *, source=None, hooks=None, env=None,
                  grants_seed="prj_test:pig_c5b_main"):
        """构造全新组件组（模拟新进程/重启；journal 从磁盘加载）。"""
        from worker import config as config_mod
        from worker.convert import Converter
        from worker.grants import GrantRegistry
        from worker.item_task import ItemContext
        from worker.journal import Journal
        from worker.platform_client import PlatformClient
        cfg = config_mod.Config({**self.env(**(env or {}))})
        platform = PlatformClient(cfg)
        if source is None:
            source = self.default_source()
        converter = Converter(cfg.slide_transform_bin,
                              timeout=cfg.convert_timeout)
        journal = Journal(cfg.journal_dir)
        grants = GrantRegistry(cfg.grants_path, seed=grants_seed)
        return ItemContext(cfg, platform, source, converter, journal,
                           grants, hooks=hooks)

    def default_source(self):
        return self.fake_source_with([
            {"path": "/share/sample-bf.kfb", "fs_id": "9100001",
             "size": len(self.fixtures["kfb_bytes"]),
             "content": self.fixtures["kfb_bytes"]},
            {"path": "/share/sample-fl.kfbf", "fs_id": "9100002",
             "size": len(self.fixtures["kfbf_bytes"]),
             "content": self.fixtures["kfbf_bytes"]},
            # KFBF 伴随 channel.json（分享内独立条目；companion_fs_id 引用）
            {"path": "/share/sample-fl_kfbf/Annotations/channel.json",
             "fs_id": "9100003",
             "size": len(self.fixtures["channel_json"].read_bytes()),
             "content": self.fixtures["channel_json"].read_bytes()},
        ])

    def enqueue_batch(self, batch_id="bch_c5b", *, names=("sample-bf.kfb",
                                                          "sample-fl.kfbf")):
        fs_map = {"sample-bf.kfb": "9100001",
                  "sample-fl.kfbf": "9100002"}
        items = []
        for i, name in enumerate(names):
            size = (len(self.fixtures["kfb_bytes"])
                    if name.endswith(".kfb")
                    else len(self.fixtures["kfbf_bytes"]))
            item = {"item_id": "itm_%s_%d" % (batch_id, i),
                    "name": name, "fs_id": fs_map[name],
                    "source_size": size}
            if name.endswith(".kfbf"):
                item["companion_fs_id"] = "9100003"
                item["companion_name"] = "channel.json"
            items.append(item)
        self.state.add_batch(
            batch_id, share_url="https://pan.baidu.com/s/fakeShareUrl",
            extraction_code=None, items=items)
        return {"batch_id": batch_id, "items": items}

    def import_rows(self):
        return {k: dict(v) for k, v in self.state.imports.items()}

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def sandbox(tmp_path, fixtures_dir, native_cli):
    sbx = _Sandbox(tmp_path, fixtures_dir)
    yield sbx
    sbx.close()
