# -*- coding: utf-8 -*-
"""worker env 配置（C5-B）。

全部经环境变量注入（插件后端与平台分进程部署；安装凭证只在此处读取，
绝不进日志/journal/报告）。缺省值面向同机部署：受管根复用平台
``SHARE_DATA_DIR/plugin-work/<installation_id>/imports/<import_id>/``（合同 §5
平台派生路径，插件不得自造任意路径）。
"""

from __future__ import annotations

import os
from pathlib import Path


def _env(name, default=""):
    v = (os.environ.get(name) or "").strip()
    return v or default


def _env_float(env, name, default):
    try:
        return float((env.get(name) or "").strip() or default)
    except ValueError:
        return float(default)


def _env_int(env, name, default):
    try:
        return int((env.get(name) or "").strip() or default)
    except ValueError:
        return int(default)


class ConfigError(Exception):
    pass


class Config:
    """全部可调项集中于此；测试用小退避/小超时避免慢用例。"""

    def __init__(self, environ=None):
        env = dict(os.environ if environ is None else environ)
        get = lambda k, d="": (env.get(k) or "").strip() or d  # noqa: E731

        # ---- 平台连接与安装凭证（必填） ---------------------------------- #
        self.platform_url = get("PT_PLATFORM_URL").rstrip("/")
        self.installation_id = get("PT_INSTALLATION_ID")
        self.installation_secret = get("PT_INSTALLATION_SECRET")
        if not (self.platform_url and self.installation_id
                and self.installation_secret):
            raise ConfigError(
                "需要 PT_PLATFORM_URL / PT_INSTALLATION_ID / "
                "PT_INSTALLATION_SECRET")

        # ---- 受管根（平台派生；合同 §5） --------------------------------- #
        # 缺省 = $SHARE_DATA_DIR/plugin-work（与平台同机同盘）；测试可用
        # PT_PLUGIN_WORK_ROOT 指到临时目录。任务根 = <root>/<inst>/imports/<import_id>/
        work_root = get("PT_PLUGIN_WORK_ROOT")
        if not work_root:
            share_data = get("SHARE_DATA_DIR")
            if not share_data:
                raise ConfigError(
                    "需要 SHARE_DATA_DIR 或 PT_PLUGIN_WORK_ROOT（受管根基座）")
            work_root = str(Path(share_data) / "plugin-work")
        self.work_root = Path(work_root)
        self.installation_root = self.work_root / self.installation_id
        self.imports_root = self.installation_root / "imports"
        self.journal_dir = self.installation_root / "journal"
        self.grants_path = self.installation_root / "grants.json"

        # ---- 源适配器 ----------------------------------------------------- #
        # BAIDU_SOURCE=fake 仅测试装配（生产绝不设置；fake 源永不触网）。
        self.source_kind = get("BAIDU_SOURCE", "bdpan")
        self.connector_bin = get("BAIDU_CONNECTOR_BIN", "bdpan")
        self.connector_home = get("BAIDU_CONNECTOR_HOME") or None
        self.list_timeout = _env_float(env, "BAIDU_LIST_TIMEOUT_SECONDS", 60)
        self.transfer_timeout = _env_float(env, "BAIDU_TRANSFER_TIMEOUT_SECONDS", 600)
        self.download_timeout = _env_float(env, "BAIDU_DOWNLOAD_TIMEOUT_SECONDS", 86400)
        self.probe_timeout = _env_float(env, "BAIDU_PROBE_TIMEOUT_SECONDS", 30)
        # 源操作重试（下载/转存瞬态故障退避重试；§6.2 重试限速归插件）
        self.source_max_attempts = _env_int(env, "BAIDU_SOURCE_MAX_ATTEMPTS", 4)
        self.source_backoff_base = _env_float(env, "BAIDU_SOURCE_BACKOFF_BASE", 2.0)
        # 客户端最小操作间隔（限速；0=不限制——bdpan CLI 自带限速时置 0）
        self.source_min_interval = _env_float(env, "BAIDU_SOURCE_MIN_INTERVAL", 0.0)

        # ---- 转换 CLI（共享原生核心，§7.1） ------------------------------- #
        self.slide_transform_bin = get("SLIDE_TRANSFORM_BIN", "slide-transform")
        self.convert_timeout = _env_float(env, "SLIDE_TRANSFORM_TIMEOUT_SECONDS", 7200)

        # ---- 传输层退避 --------------------------------------------------- #
        self.max_attempts = _env_int(env, "PT_HTTP_MAX_ATTEMPTS", 5)
        self.backoff_base = _env_float(env, "PT_HTTP_BACKOFF_BASE", 0.5)
        self.rate_limit_max_waits = _env_int(env, "PT_HTTP_RATE_LIMIT_MAX_WAITS", 16)
        self.connect_timeout = _env_float(env, "PT_HTTP_CONNECT_TIMEOUT", 10)
        self.control_timeout = _env_float(env, "PT_HTTP_CONTROL_TIMEOUT", 30)
        self.write_timeout = _env_float(env, "PT_HTTP_WRITE_TIMEOUT", 600)
        self.commit_timeout = _env_float(env, "PT_HTTP_COMMIT_TIMEOUT", 1800)

        # ---- 桥租约 / 主循环 --------------------------------------------- #
        self.lease_seconds = _env_int(env, "PT_BAIDU_LEASE_SECONDS", 1800)
        self.heartbeat_interval = _env_float(
            env, "PT_BAIDU_HEARTBEAT_INTERVAL",
            max(1.0, self.lease_seconds / 3.0))
        self.claim_poll_seconds = _env_float(env, "PT_BAIDU_CLAIM_POLL_SECONDS", 5.0)
        self.worker_id = get("PT_WORKER_ID", "baidu-import-plugin")

        # ---- 清理退避 ----------------------------------------------------- #
        self.cleanup_max_attempts = _env_int(env, "PT_CLEANUP_MAX_ATTEMPTS", 8)
        self.cleanup_backoff_base = _env_float(env, "PT_CLEANUP_BACKOFF_BASE", 1.0)

        # ---- 回执轮询（commit 响应丢失 / awaiting_receipt） --------------- #
        self.receipt_poll_seconds = _env_float(env, "PT_RECEIPT_POLL_SECONDS", 2.0)
        self.receipt_poll_max = _env_int(env, "PT_RECEIPT_POLL_MAX", 60)

        # ---- 健康端点（manifest.service.health 指向） --------------------- #
        self.health_host = get("PT_HEALTH_HOST", "127.0.0.1")
        self.health_port = _env_int(env, "PT_HEALTH_PORT", 8062)

        # ---- grant 种子（可选；正式入口是 grants.json / 插件 UI）---------- #
        # 形如 "proj_a:pig_xxx,proj_b:pig_yyy"
        self.grant_seed = get("PT_IMPORT_GRANTS")

    def managed_root(self, import_id):
        """受管任务根（平台派生；合同 §5）：插件的一切任务文件只写这里。"""
        return self.imports_root / str(import_id)

    def describe(self):
        """脱敏配置摘要（日志用；绝不含 secret/grant_id）。"""
        return {
            "platform_url": self.platform_url,
            "installation_id": self.installation_id,
            "work_root": str(self.work_root),
            "source_kind": self.source_kind,
            "slide_transform_bin": self.slide_transform_bin,
            "worker_id": self.worker_id,
        }
