# -*- coding: utf-8 -*-
"""错误模型（C5-B 插件侧）。

三层错误：

1. :class:`TransportError`——连接层故障（超时/连接拒绝/响应读取失败）。
   **响应丢失≠操作失败**：commit 的回执可能已持久化（合同 §1.4 回执幂等），
   调用方必须转 status 查询，绝不盲目重放有副作用语义的请求。
2. :class:`ContractError`——平台统一错误信封
   ``{"error": {code, message, retryable, details?}}``。``retryable`` 以
   §1.8 表为权威（本模块的表与之逐行对应；信封自带值优先）。
3. :class:`SourceError`——百度源适配器业务失败（自平台 baidu_adapter 的
   AdapterError 语义移植）；code 稳定，retryable 由 SOURCE_RETRYABLE_CODES 判定。

任何层都不得携带 write_token / 安装凭证 / 分享 URL / 提取码进 message
（repr 只含 code 与脱敏摘要）。
"""

from __future__ import annotations

# --------------------------------------------------------------------------- #
# 合同 §1.8 新增错误码表（HTTP / retryable）——与冻结稿逐行对应；平台信封
# 自带 retryable 时以信封为准（表是缺省）。基础码（unauthorized/
# token_expired/rate_limited/internal/unavailable…）沿用平台既有语义。
# --------------------------------------------------------------------------- #
CONTRACT_ERROR_TABLE = {
    # code: (http_status, retryable)
    "import_not_found": (404, False),
    "import_state_invalid": (409, False),
    "idempotency_conflict": (409, False),
    "offset_conflict": (409, False),
    "checksum_mismatch": (409, False),
    "incomplete_write": (409, False),
    "commit_in_progress": (409, False),
    "size_exceeded": (413, False),
    "upload_quota_exceeded": (413, False),
    "disk_watermark_exceeded": (507, True),
    "format_unsupported": (422, False),
    "import_grant_invalid": (403, False),
    "cleanup_not_verified": (409, False),
}

#: 平台既有基础码的 retryable 缺省（§7.7；与 app._PLUGIN_ERROR_RETRYABLE 同口径）
BASE_ERROR_TABLE = {
    "unauthorized": (401, False),
    "token_expired": (401, True),
    "forbidden": (403, False),
    "not_found": (404, False),
    "invalid_request": (400, False),
    "rate_limited": (429, True),
    "internal": (500, True),
    "unavailable": (503, True),
}


def default_retryable(code):
    """按 §1.8/基础码表给出 code 的缺省 retryable（未知 code → False）。"""
    if code in CONTRACT_ERROR_TABLE:
        return CONTRACT_ERROR_TABLE[code][1]
    return BASE_ERROR_TABLE.get(code, (None, False))[1]


class PluginWorkerError(Exception):
    """插件 worker 错误基类（message 必须已脱敏）。"""

    code = "internal"
    retryable = False

    def __repr__(self):  # 绝不打消息体（防泄漏）；只打 code
        return "%s(code=%r)" % (type(self).__name__, self.code)


class TransportError(PluginWorkerError):
    """连接层故障（重试耗尽后仍失败）。response_lost=True 表示请求可能已
    被服务端受理但响应未读到（commit 回执丢失场景：转 status，不重放）。"""

    code = "transport_error"

    def __init__(self, message="", *, response_lost=False):
        super().__init__(message or self.code)
        self.response_lost = bool(response_lost)


class ContractError(PluginWorkerError):
    """平台统一错误信封。details 透传（expected_offset / reason 等）。"""

    def __init__(self, status, code, message="", retryable=None, details=None):
        super().__init__(message or code)
        self.status = int(status)
        self.code = str(code or "internal")
        self.retryable = (default_retryable(self.code)
                          if retryable is None else bool(retryable))
        self.details = dict(details or {})

    @property
    def reason(self):
        """import_grant_invalid 的 reason 细分（§2.3：grant_not_found/
        grant_revoked/grant_expired/installation_mismatch/project_mismatch/
        user_not_allowed）。"""
        return (self.details.get("reason")
                or (self.details.get("details") or {}).get("reason"))


class SourceError(PluginWorkerError):
    """百度源适配器业务失败（code 稳定，message 已脱敏）。"""

    def __init__(self, code, message="", detail_internal=None):
        super().__init__(message or code)
        self.code = str(code or "source_error")
        # 内部细节不进普通日志/响应（与平台 AdapterError 同约定）
        self.detail_internal = detail_internal

    @property
    def retryable(self):
        return self.code in SOURCE_RETRYABLE_CODES


#: 源适配器可重试码（连接器瞬态故障）。业务拒绝不重试：未登录
#: （connector_unusable——凭证问题）、share_invalid/share_password_error/
#: invalid_*（确定性拒绝）。
SOURCE_RETRYABLE_CODES = frozenset({
    "connector_timeout",
    "connector_failed",
    "download_failed",
    "transfer_failed",
})


class LeaseLostError(PluginWorkerError):
    """桥租约被夺（heartbeat/report 被 fence 拒绝）：安静放弃本批。"""

    code = "lease_lost"


class StopRequested(PluginWorkerError):
    """进程收到停止信号（SIGTERM/SIGINT）：当前条目边界退出。"""

    code = "stop_requested"
