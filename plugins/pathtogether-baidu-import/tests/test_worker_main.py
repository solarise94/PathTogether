# -*- coding: utf-8 -*-
"""进程入口冒烟：python3 -m worker（stub 平台；健康端点 + 优雅停机 + 全链路）。"""

import json
import os
import signal
import socket
import subprocess
import sys
import time
import urllib.request

import pytest

from stub_platform import StubPlatformState, serve

pytestmark = pytest.mark.c5b

PLUGIN_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPO_ROOT = os.path.dirname(os.path.dirname(PLUGIN_ROOT))
CLI = os.path.join(REPO_ROOT, "slide-transform-core", "target", "release",
                   "slide-transform")


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.fixture(scope="module")
def fixtures(tmp_path_factory):
    """合成 KFB（供子进程全链路用；原生 CLI 生成）。"""
    if not os.path.isfile(CLI):
        pytest.skip("native CLI missing: %s" % CLI)
    d = tmp_path_factory.mktemp("c5b-main")
    kfb = d / "smoke.kfb"
    subprocess.run([CLI, "gen-kfb", str(kfb), "--width", "580",
                    "--height", "300"], check=True)
    return kfb


def test_worker_main_healthz_and_graceful_shutdown(tmp_path, fixtures):
    state = StubPlatformState(share_data_dir=tmp_path / "share-data",
                              installation_id="inst_smoke",
                              secret="smoke-secret",
                              chunk_max_bytes=16384)
    state.add_grant("pig_smoke", project_id="prj_smoke",
                    user_id="usr_owner", ttl_seconds=3600)
    state.add_batch(
        "bch_smoke", share_url="https://pan.baidu.com/s/fakeSmoke",
        extraction_code=None, target_project_id="prj_smoke",
        items=[{"item_id": "itm_smoke", "name": "smoke.kfb",
                "fs_id": "1", "source_size": fixtures.stat().st_size}])
    server, base_url = serve(state)
    health_port = _free_port()
    env = dict(os.environ)
    env.update({
        "PT_PLATFORM_URL": base_url,
        "PT_INSTALLATION_ID": "inst_smoke",
        "PT_INSTALLATION_SECRET": "smoke-secret",
        "PT_PLUGIN_WORK_ROOT": str(tmp_path / "plugin-work"),
        "PT_IMPORT_GRANTS": "prj_smoke:pig_smoke",
        "BAIDU_SOURCE": "fake",
        # fake 源的分享树由 FAKE_SHARE_TREE 以路径注入（见下）
        "PT_HEALTH_PORT": str(health_port),
        "PT_BAIDU_CLAIM_POLL_SECONDS": "0.1",
        "PT_BAIDU_HEARTBEAT_INTERVAL": "0.2",
        "PT_BAIDU_LEASE_SECONDS": "60",
        "PT_HTTP_MAX_ATTEMPTS": "3",
        "PT_HTTP_BACKOFF_BASE": "0.05",
        "PT_CLEANUP_MAX_ATTEMPTS": "5",
        "PT_CLEANUP_BACKOFF_BASE": "0.02",
        "PT_RECEIPT_POLL_SECONDS": "0.05",
        "PT_RECEIPT_POLL_MAX": "60",
        "SLIDE_TRANSFORM_BIN": CLI,
        "SLIDE_TRANSFORM_TIMEOUT_SECONDS": "300",
        "PYTHONPATH": PLUGIN_ROOT,
        "TMPDIR": os.environ.get("TMPDIR", "/tmp"),
    })
    # fake 源分享树经文件注入（大夹具走文件，避开 env 尺寸上限）
    import base64
    tree_file = tmp_path / "fake-share-tree.json"
    tree_file.write_text(json.dumps([{
        "path": "/share/smoke.kfb", "fs_id": "1",
        "size": fixtures.stat().st_size,
        "content_b64":
            base64.b64encode(fixtures.read_bytes()).decode("ascii"),
    }]))
    env["PT_FAKE_SHARE_TREE_FILE"] = str(tree_file)

    proc = subprocess.Popen(
        [sys.executable, "-m", "worker"], cwd=PLUGIN_ROOT, env=env,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    try:
        # 健康端点就绪
        deadline = time.time() + 20
        health = None
        while time.time() < deadline:
            try:
                with urllib.request.urlopen(
                        "http://127.0.0.1:%d/healthz" % health_port,
                        timeout=2) as r:
                    health = json.loads(r.read().decode("utf-8"))
                break
            except OSError:
                if proc.poll() is not None:
                    out = proc.stdout.read()
                    pytest.fail("worker 早退：%s" % out[-2000:])
                time.sleep(0.1)
        assert health and health["ok"] is True
        assert health["plugin"] == "dev.pathtogether.baidu-import"
        # 全链路完成（published + 清理 + 桥回写 ready——回写在 publish
        # 之后，轮询条目终态避免读到中间态）
        deadline = time.time() + 90
        while time.time() < deadline:
            if state.items["itm_smoke"]["stage"] == "ready" and \
                    state.counters.get("release_scratch", 0) >= 1:
                break
            time.sleep(0.2)
        assert state.counters.get("publish", 0) == 1
        assert state.counters.get("release_scratch", 0) == 1
        (row,) = state.imports.values()
        assert row["state"] == "published"
        assert row["plugin_cleanup_status"] == "cleaned"
        assert state.items["itm_smoke"]["stage"] == "ready"
        # 优雅停机（SIGTERM；退出码 0）
        proc.send_signal(signal.SIGTERM)
        rc = proc.wait(timeout=20)
        out = proc.stdout.read()
        assert rc == 0, "SIGTERM 应优雅退出（rc=%s）：%s" % (rc, out[-2000:])
    finally:
        if proc.poll() is None:
            proc.kill()
        server.shutdown()
        server.server_close()
