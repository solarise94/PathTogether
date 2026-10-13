# -*- coding: utf-8 -*-
"""COS fast 纯流式转发下载适配器（``COS_DOWNLOAD_TRANSPORT=fast``）。

线合同：``gokapi-relay/docs/slide-relay-contract.md``（v1）。
设计：``docs/cos-fast-goapi-relay-design-20261013.md``（commit dc5f5b55）。

角色：本模块是 ``cos_ingest_worker`` 下载路径的**薄**适配器。它把腾讯云 fast
上的 goapi 切片转发通道（``GET /internal/histopilot/v1/object``）归一成
worker 已消费的 SDK 响应形状（``.status`` / ``.status_code`` / ``.headers`` /
``.read(n)`` / ``.close()``），使 ``cos_client.get_object`` 可按 transport
分发。**只做传输，不改 worker 的 206/Content-Range/预算/断点校验**。

边界与不变量：

- **绝不自动回退 direct COS**：配置缺失/非法一律 ``CosConfigMissing``，由
  worker 空转告警；回退是显式运维动作（设计 §最小实施面 5）。
- 脱敏：错误消息只含 method/key 形状/HTTP 状态/错误码，不含 token、签名
  URL、query 或完整 key（``cos_client._key_shape``）。
- 有界内存：``stream=True`` + 固定小块读取；不做整对象缓冲。
- 中止语义（合同 §7）：206 头已发出后上游早 EOF/读错误/超时 → ``read``
  返回 ``b""``（worker 视为 early EOF，计入 wire，不 checkpoint），
  不抛异常、不追加 JSON。
- 请求环境隔离：``session.trust_env = False``（不读 ``HTTP(S)_PROXY``/
  ``NO_PROXY``/netrc，防止静默改道）；``allow_redirects=False``；TLS 校验开。
"""

from __future__ import annotations

import json
import threading
import time
import urllib.parse
from typing import Optional

import requests

import cos_client
import cos_config


#: 转发路由（合同固定）。
_RELAY_PATH = "/internal/histopilot/v1/object"

#: 错误体读取上限（错误响应小而固定；多读即弃）。
_ERROR_BODY_MAX = 4096

#: urllib 风格响应读取的默认分块（调用方通常显式传 n，这里只是兜底）。
_READ_CHUNK = 65536

#: http 明文仅允许的回环主机（本机测试）。
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})

_session_lock = threading.Lock()
_session_cache: Optional[requests.Session] = None


def _session() -> requests.Session:
    """模块级复用 Session（与 ``cos_client._client`` 同款加锁惰性创建）。

    ``trust_env=False`` 在此一次性设置：进程环境里的代理/证书覆盖不得改变
    转发目标，也不得把请求静默引到别处。
    """
    global _session_cache
    with _session_lock:
        if _session_cache is None:
            s = requests.Session()
            s.trust_env = False
            _session_cache = s
        return _session_cache


def _base_url() -> str:
    """校验并返回 fast 基地址（scheme/host 硬约束）。"""
    raw = (cos_config.COS_FAST_RELAY_URL or "").strip()
    if not raw:
        raise cos_client.CosConfigMissing(
            "COS_FAST_RELAY_URL 未配置——fast 传输不可用（不自动回退 direct）")
    parsed = urllib.parse.urlsplit(raw)
    scheme = (parsed.scheme or "").lower()
    host = (parsed.hostname or "").lower()
    if scheme not in ("http", "https") or not host:
        raise cos_client.CosConfigMissing(
            "COS_FAST_RELAY_URL 非法（需 http(s)://host）——不自动回退 direct")
    if scheme != "https" and host not in _LOOPBACK_HOSTS:
        raise cos_client.CosConfigMissing(
            "COS_FAST_RELAY_URL 必须为 https://（仅回环主机允许 http）"
            "——不自动回退 direct")
    return raw.rstrip("/") + _RELAY_PATH


def _load_token() -> str:
    """读 token 私有文件首个非空行；缺失/空/不可读 → CosConfigMissing。"""
    path = (cos_config.COS_FAST_RELAY_TOKEN_FILE or "").strip()
    if not path:
        raise cos_client.CosConfigMissing(
            "COS_FAST_RELAY_TOKEN_FILE 未配置——fast 传输不可用")
    try:
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                token = line.strip()
                if token:
                    return token
    except OSError as exc:
        raise cos_client.CosConfigMissing(
            "COS_FAST_RELAY_TOKEN_FILE 无法读取（%s）" % type(exc).__name__) from exc
    raise cos_client.CosConfigMissing(
        "COS_FAST_RELAY_TOKEN_FILE 为空——fast 传输不可用")


def _error_code(resp) -> str:
    """从错误体读取 ``{"error": "<code>"}``（≤4 KiB，失败即空串）。"""
    raw = getattr(resp, "raw", None)
    if raw is None:
        return ""
    try:
        body = raw.read(_ERROR_BODY_MAX)
    except Exception:  # noqa: BLE001  # 错误体读取失败不需要区分原因
        return ""
    try:
        payload = json.loads((body or b"").decode("utf-8", "replace"))
    except (ValueError, TypeError):
        return ""
    if not isinstance(payload, dict):
        return ""
    code = payload.get("error")
    if not isinstance(code, str):
        return ""
    return "".join(ch for ch in code if ch.isalnum() or ch == "_")[:64]


def _deadline_seconds(timeout) -> float:
    try:
        value = float(timeout)
    except (TypeError, ValueError):
        value = 120.0
    return value if value > 0 else 120.0


class _FastStreamWrap:
    """fast 206 响应的 worker 消费形状（对照 ``cos_client._StreamWrap``）。

    ``read`` 契约：**绝不把传输错误抛进 worker**。任何 body 中途错误
    （urllib3/requests ProtocolError、IncompleteRead、ReadTimeout、
    ConnectionError、OSError 等）或超出每请求总时限 → 关闭连接并返回
    ``b""``；worker 据此按 early EOF 计入 wire、不 checkpoint。
    """

    def __init__(self, response, deadline: float):
        self.status = 206
        self.status_code = 206
        src = response.headers
        headers = {str(k): str(v) for k, v in src.items()}
        # requests 的 headers 大小写不敏感；worker 以 canonical 名取用，
        # 这里补 canonical 键，避免上游大小写差异造成误判（合同要求 206 头）。
        for name in ("Content-Range", "Content-Length", "X-HP-Version-Id"):
            if name not in headers:
                value = src.get(name)
                if value is not None:
                    headers[name] = str(value)
        self.headers = headers
        self._response = response
        self._deadline = deadline
        self._closed = False

    def read(self, n=None) -> bytes:
        # n=None 也限定为一块（有界内存；worker 始终显式传 n）。
        want = _READ_CHUNK if n is None else int(n)
        if want <= 0:
            return b""
        if self._closed:
            return b""
        if time.monotonic() >= self._deadline:
            self.close()
            return b""
        try:
            buf = self._response.raw.read(want)
        except Exception:  # noqa: BLE001  # 传输错误一律按 early EOF 处理
            self.close()
            return b""
        if not buf:
            self.close()
            return b""
        return buf

    def close(self):
        """幂等关闭底层连接。"""
        if self._closed:
            return
        self._closed = True
        try:
            self._response.close()
        except Exception:  # noqa: BLE001
            pass


def get_object(key: str, version_id: str, range_header: Optional[str] = None,
               timeout: int = 120, object_size: Optional[int] = None):
    """fast 版本化 Range GET（流式）。仅在 ``COS_DOWNLOAD_TRANSPORT=fast``
    时由 ``cos_client.get_object`` 分发到此。

    - 206 → ``_FastStreamWrap``；
    - 其余状态（含 200 整对象、3xx）→ 读 ≤4 KiB 错误体取 JSON code，关闭，
      抛 ``CosAdapterError``（消息脱敏）；
    - 请求头之前连接错误/超时 → ``CosAdapterError``（worker 回队重试）；
    - 配置问题 → ``CosConfigMissing``（worker 空转告警）。
    """
    url = _base_url()
    token = _load_token()
    if object_size is None:
        raise cos_client.CosAdapterError(
            "fast 传输缺少 object_size——拒绝下载（不静默回退 direct）")
    if not range_header:
        raise cos_client.CosAdapterError(
            "fast 传输缺少 range_header——拒绝下载（不静默回退 direct）")
    try:
        size = int(object_size)
    except (TypeError, ValueError) as exc:
        raise cos_client.CosAdapterError(
            "fast 传输 object_size 非法——拒绝下载") from exc

    headers = {
        "Authorization": "Bearer %s" % token,
        "X-HP-Object-Key": urllib.parse.quote(key, safe="/"),
        "X-HP-Version-Id": str(version_id),
        "X-HP-Object-Size": str(size),
        "Range": str(range_header),
        "Accept-Encoding": "identity",
    }
    connect_timeout = max(1, int(cos_config.COS_FAST_RELAY_CONNECT_TIMEOUT))
    read_timeout = max(1, int(cos_config.COS_FAST_RELAY_READ_TIMEOUT))
    total = _deadline_seconds(timeout)
    # 单次读空闲不得长于每请求总期限：无进展的 body 在 min(空闲, 期限) 内
    # 终止，不会因空闲超时配置过大而拖过总时限。
    socket_read = max(1, int(min(read_timeout, total)))
    deadline = time.monotonic() + total

    try:
        response = _session().get(
            url, headers=headers, stream=True, allow_redirects=False,
            timeout=(connect_timeout, socket_read), verify=True)
    except requests.RequestException as exc:
        raise cos_client.CosAdapterError(
            "COS GET %s 客户端错误（fast）：%s" % (
                cos_client._key_shape(key), str(exc)[:120])) from exc

    status = int(getattr(response, "status_code", 0) or 0)
    if status != 206:
        code = _error_code(response)
        try:
            response.close()
        except Exception:  # noqa: BLE001
            pass
        detail = (" %s" % code) if code else ""
        raise cos_client.CosAdapterError(
            "COS GET %s 失败：HTTP %d%s (fast)" % (
                cos_client._key_shape(key), status, detail))

    return _FastStreamWrap(response, deadline)
