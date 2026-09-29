# -*- coding: utf-8 -*-
"""插件后端进程入口（C5-B）。

运行（与平台同机；受管根复用平台 SHARE_DATA_DIR）::

    PYTHONPATH=plugins/pathtogether-baidu-import \\
    PT_PLATFORM_URL=http://127.0.0.1:8000 \\
    PT_INSTALLATION_ID=<安装行 id> \\
    PT_INSTALLATION_SECRET=<安装凭证明文> \\
    SHARE_DATA_DIR=<平台共享数据目录> \\
    BAIDU_CONNECTOR_HOME=<bdpan 认证目录> \\
    python3 -m worker

进程形态：单进程主循环（claim → 逐条目串行推进）+ daemon 心跳线程 +
健康端点线程（manifest.service.health 指向 ``/healthz``）。安装凭证只经
env 进入内存，绝不写日志/journal/报告。
"""

from __future__ import annotations

import json
import signal
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import errors
from .batch_driver import BatchDriver
from .config import Config, ConfigError
from .convert import Converter
from .grants import GrantRegistry
from .item_task import ItemContext
from .journal import Journal
from .platform_client import PlatformClient
from .source import get_source


class _HealthHandler(BaseHTTPRequestHandler):
    """manifest.service.health 端点（无状态；不含任何凭证）。"""

    state = {"ok": False, "info": {}}

    def do_GET(self):
        if self.path != "/healthz":
            self.send_response(404)
            self.end_headers()
            return
        body = json.dumps({
            "ok": bool(_HealthHandler.state["ok"]),
            "plugin": "dev.pathtogether.baidu-import",
            "info": _HealthHandler.state["info"],
        }).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):  # 静默（健康轮询不打日志）
        return


def start_health_server(config):
    server = ThreadingHTTPServer(
        (config.health_host, int(config.health_port)), _HealthHandler)
    t = threading.Thread(target=server.serve_forever, daemon=True,
                         name="baidu-plugin-health")
    t.start()
    return server


def main(argv=None):
    try:
        config = Config()
    except ConfigError as e:
        print("config error: %s" % e, file=sys.stderr)
        return 2
    _HealthHandler.state["info"] = config.describe()

    platform = PlatformClient(config)
    source = get_source(config)
    converter = Converter(config.slide_transform_bin,
                          timeout=config.convert_timeout)
    journal = Journal(config.journal_dir)
    grants = GrantRegistry(config.grants_path, seed=config.grant_seed)
    ctx = ItemContext(config, platform, source, converter, journal, grants)
    driver = BatchDriver(ctx)

    health = start_health_server(config)
    stop = threading.Event()

    def _sig(_sig, _frm):
        stop.set()

    signal.signal(signal.SIGTERM, _sig)
    signal.signal(signal.SIGINT, _sig)

    _HealthHandler.state["ok"] = True
    print("baidu-import worker started %s" % json.dumps(
        config.describe(), ensure_ascii=False))
    try:
        while not stop.is_set():
            try:
                summary = driver.run_once()
            except errors.ContractError as e:
                if e.code == "unauthorized":
                    # 安装被停用：退避重试（token 交换会持续 401 直到
                    # 重新启用）——绝不让凭证类错误刷屏/崩溃
                    stop.wait(min(30.0, config.claim_poll_seconds * 2))
                    continue
                print("worker error: %r" % (e,), file=sys.stderr)
                stop.wait(config.claim_poll_seconds)
                continue
            except Exception as e:  # noqa: BLE001
                print("worker crash-safed: %s" % type(e).__name__,
                      file=sys.stderr)
                stop.wait(config.claim_poll_seconds)
                continue
            if summary is None:
                stop.wait(config.claim_poll_seconds)
                continue
            if summary.get("stopped"):
                # plugin_disabled / stop_requested：安静退避
                stop.wait(min(30.0, config.claim_poll_seconds * 2))
    finally:
        _HealthHandler.state["ok"] = False
        health.shutdown()
        platform.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
