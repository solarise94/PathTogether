# -*- coding: utf-8 -*-
"""传输层：退避重试 + Retry-After 遵从（C5-B）。

策略（对平台 §1.1 限流与 §1.8 retryable 语义的客户端对偶）：

- **429 rate_limited**：按响应头 ``Retry-After``（整数秒；缺省看信封
  ``details.retry_after``）睡眠后重试；等待次数有界（``rate_limit_max_waits``）。
- **retryable 信封 / 5xx / 连接错误**：指数退避重试，``max_attempts`` 有界。
- **非 retryable 4xx**：立即抛 :class:`~worker.errors.ContractError`。
- 响应读取失败（连接在响应前断开）→ :class:`TransportError(response_lost=True)`
  ——请求可能已被受理，调用方（commit/write）必须转 status 查询，不盲目重放。

绝不记录请求头/体（含 Bearer / X-Import-Token）。
"""

from __future__ import annotations

import json
import time

import requests

from . import errors


class Response:
    """极薄响应包装（status / headers / body bytes / json）。"""

    def __init__(self, status, headers, body):
        self.status = int(status)
        self.headers = dict(headers or {})
        self.body = body if isinstance(body, (bytes, bytearray)) else b""
        self.text = self.body.decode("utf-8", "replace")

    def json(self):
        try:
            return json.loads(self.body.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return None

    @property
    def ok(self):
        return 200 <= self.status < 300


class Transport:
    """单线程使用（worker 主循环串行；健康端点不经此）。"""

    def __init__(self, base_url, *, max_attempts=5, backoff_base=0.5,
                 rate_limit_max_waits=16, connect_timeout=10.0,
                 request_timeout=30.0, sleep=None, now=None, session=None):
        self.base_url = base_url.rstrip("/")
        self.max_attempts = max(1, int(max_attempts))
        self.backoff_base = max(0.0, float(backoff_base))
        self.rate_limit_max_waits = max(0, int(rate_limit_max_waits))
        self.connect_timeout = float(connect_timeout)
        self.default_timeout = float(request_timeout)
        self._sleep = sleep if sleep is not None else time.sleep
        self._now = now if now is not None else time.monotonic
        self._session = session or requests.Session()

    # -- 内部 -------------------------------------------------------------- #

    def _backoff(self, attempt):
        return self.backoff_base * (2 ** max(0, attempt - 1))

    def _retry_after_seconds(self, resp):
        raw = (resp.headers.get("Retry-After")
               or resp.headers.get("retry-after") or "").strip()
        if raw:
            try:
                return max(1, int(float(raw)))
            except ValueError:
                pass
        payload = resp.json() or {}
        details = ((payload.get("error") or {}).get("details") or {})
        try:
            return max(1, int(details.get("retry_after")))
        except (TypeError, ValueError):
            return 1

    def _raise_contract(self, resp):
        payload = resp.json() or {}
        envelope = payload.get("error") if isinstance(payload, dict) else None
        if isinstance(envelope, dict):
            raise errors.ContractError(
                resp.status, envelope.get("code") or "internal",
                message=envelope.get("message") or "",
                retryable=envelope.get("retryable"),
                details=envelope.get("details") or {})
        # 平台 plugin v1 端点恒为信封；裸响应按内部错误处理（可重试语义
        # 已在上层判断 5xx，这里只兜底 4xx/未知）
        raise errors.ContractError(resp.status, "internal",
                                   message="non-envelope response",
                                   retryable=resp.status >= 500)

    # -- 对外 -------------------------------------------------------------- #

    def request(self, method, path, *, json_body=None, data=None,
                headers=None, timeout=None, response_lost_capable=False):
        """执行一次（带重试策略的）HTTP 请求；成功返回 :class:`Response`。

        失败路径：
        - 非retryable 4xx → ContractError（不重试）；
        - retryable/5xx/连接错误重试耗尽 → ContractError / TransportError；
        - 响应前连接断开且 ``response_lost_capable``（write/commit）→
          TransportError(response_lost=True)（重试仍按策略先做，耗尽才抛）。
        """
        url = self.base_url + path
        hdrs = dict(headers or {})
        attempt = 0
        rate_waits = 0
        timeout = float(timeout if timeout is not None
                        else self.default_timeout)
        while True:
            attempt += 1
            try:
                raw = self._session.request(
                    method, url, json=json_body, data=data, headers=hdrs,
                    timeout=(self.connect_timeout, timeout))
            except requests.exceptions.Timeout as e:
                # 超时：请求可能已到达服务端（受理未知）→ response_lost 语义
                last = errors.TransportError(
                    "request timeout", response_lost=response_lost_capable)
                last.__cause__ = e
            except requests.exceptions.ConnectionError as e:
                last = errors.TransportError(
                    "connection error", response_lost=response_lost_capable)
                last.__cause__ = e
            except requests.exceptions.RequestException as e:
                last = errors.TransportError("request failed")
                last.__cause__ = e
            else:
                resp = Response(raw.status_code, raw.headers, raw.content)
                if resp.ok:
                    return resp
                code = None
                payload = resp.json() or {}
                envelope = (payload.get("error")
                            if isinstance(payload, dict) else None)
                if isinstance(envelope, dict):
                    code = envelope.get("code")
                if resp.status == 429 or code == "rate_limited":
                    if rate_waits >= self.rate_limit_max_waits:
                        self._raise_contract(resp)
                    wait = self._retry_after_seconds(resp)
                    self._sleep(wait)
                    rate_waits += 1
                    attempt -= 1  # 429 等待不吃退避预算
                    continue
                retryable = errors.default_retryable(code) \
                    if code is not None else resp.status >= 500
                if isinstance(envelope, dict) and \
                        envelope.get("retryable") is not None:
                    retryable = bool(envelope.get("retryable"))
                if not retryable or attempt >= self.max_attempts:
                    self._raise_contract(resp)
                last = None  # 触发下方退避
            if attempt >= self.max_attempts:
                if last is not None:
                    raise last
                raise errors.TransportError("retries exhausted")
            self._sleep(self._backoff(attempt))

    def close(self):
        try:
            self._session.close()
        except Exception:  # noqa: BLE001
            pass
