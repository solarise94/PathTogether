# -*- coding: utf-8 -*-
"""平台客户端：token 交换 + producer import 端点 + 百度桥（C5-B）。

覆盖合同 §1（作为每个端点的客户端）、§6.3（claim/heartbeat/report）：

- ``POST /api/plugin/v1/auth/token``：安装凭证 → 短期 scoped JWT（缓存，
  过期前 60s 主动刷新；401 token_expired → 强刷一次重试）。
- ``POST /api/plugin/v1/imports/begin|topup|write|commit|cancel|scratch|
  cleanup-confirm`` + ``GET …/status``：§1.2–§1.7。
- ``POST /api/plugin/v1/baidu/batches/claim|heartbeat|items/<id>/report``。

桥载荷形态（§6.3 只冻结端点名；本客户端的请求/响应形态如下，平台侧
实现需对齐——见 README「桥契约」）：

- claim 请求 ``{"worker_id", "lease_seconds"}``；200 响应
  ``{"batch": {"batch_id", "owner_user_id", "target_project_id", "share_url",
  "extraction_code", "cancel_requested", "lease_token",
  "lease_expires_at"}, "items": [{"item_id", "name", "fs_id",
  "source_size", "stage"}]}``；无批次 204。
- heartbeat 请求 ``{"batch_id", "worker_id", "lease_token"}``；响应
  ``{"ok": true, "cancel_requested": bool}``；租约被夺 → 409
  ``import_state_invalid``（本客户端转 LeaseLostError）。
- report 请求 ``{"batch_id", "lease_token", "stage", "error_code"?,
  "import_id"?, "slide_id"?}``；被 fence 拒绝 → 409（同上转 LeaseLost）。
"""

from __future__ import annotations

import time

from . import errors
from .http_client import Transport

_TOKEN_PATH = "/api/plugin/v1/auth/token"
_TOKEN_REFRESH_MARGIN = 60.0


class PlatformClient:
    """线程不安全（主循环串行调用；心跳线程只用 ``baidu_heartbeat``——
    其只读共享的 JWT 缓存，必要时先 ``ensure_token``）。"""

    def __init__(self, config, *, transport=None, sleep=None, now=None):
        self._cfg = config
        self._transport = transport or Transport(
            config.platform_url,
            max_attempts=config.max_attempts,
            backoff_base=config.backoff_base,
            rate_limit_max_waits=config.rate_limit_max_waits,
            connect_timeout=config.connect_timeout,
            request_timeout=config.control_timeout,
            sleep=sleep, now=now)
        self._sleep = sleep if sleep is not None else time.sleep
        self._now = now if now is not None else time.monotonic
        self._token = ""
        self._token_exp = 0.0

    # ------------------------------------------------------------------ #
    # token 交换（§1.1 认证）
    # ------------------------------------------------------------------ #

    def ensure_token(self, force=False):
        """返回有效 JWT（缓存；临期/force 时重换）。失败抛 ContractError。"""
        if (not force and self._token
                and self._now() < self._token_exp - _TOKEN_REFRESH_MARGIN):
            return self._token
        resp = self._transport.request(
            "POST", _TOKEN_PATH,
            json_body={"installation_id": self._cfg.installation_id,
                       "secret": self._cfg.installation_secret},
            timeout=self._cfg.control_timeout)
        payload = resp.json() or {}
        token = payload.get("access_token")
        expires_in = float(payload.get("expires_in") or 900)
        if not isinstance(token, str) or not token:
            raise errors.ContractError(500, "internal", "token 响应非法")
        self._token = token
        self._token_exp = self._now() + max(1.0, expires_in)
        return token

    def _headers(self, write_token=None, idempotency_key=None):
        h = {"Authorization": "Bearer " + self.ensure_token()}
        if write_token:
            h["X-Import-Token"] = write_token
        if idempotency_key:
            h["Idempotency-Key"] = idempotency_key
        return h

    def _call(self, method, path, *, json_body=None, data=None,
              write_token=None, idempotency_key=None, timeout=None,
              response_lost_capable=False):
        """带一次 token_expired 恢复的调用（强刷 JWT 后重试一次）。"""
        hdrs = self._headers(write_token, idempotency_key)
        try:
            return self._transport.request(
                method, path, json_body=json_body, data=data, headers=hdrs,
                timeout=timeout, response_lost_capable=response_lost_capable)
        except errors.ContractError as e:
            if e.code == "token_expired":
                self.ensure_token(force=True)
                hdrs = self._headers(write_token, idempotency_key)
                return self._transport.request(
                    method, path, json_body=json_body, data=data, headers=hdrs,
                    timeout=timeout, response_lost_capable=response_lost_capable)
            raise

    @staticmethod
    def _json(resp):
        payload = resp.json()
        return payload if isinstance(payload, dict) else {}

    # ------------------------------------------------------------------ #
    # producer import 端点（§1.2–§1.7）
    # ------------------------------------------------------------------ #

    def import_begin(self, *, grant_id, project_id, filename, format_ext,
                     declared_size, scratch_bytes, profile=None,
                     idempotency_key, baidu_item_id=None):
        key = str(idempotency_key)
        if not key or len(key) > 128:
            raise errors.ContractError(400, "invalid_request",
                                       "idempotency_key 非法")
        body = {
            "grant_id": grant_id,
            "project_id": project_id,
            "filename": filename,
            "format_ext": format_ext,
            "declared_size": int(declared_size),
            "scratch_bytes": int(scratch_bytes),
            "profile": profile or {},
            "idempotency_key": key,   # §1.2：与头必须相等
        }
        if baidu_item_id:
            # §6.3：条目发布不另开端点——begin 携带 baidu_item_id 绑定条目
            body["baidu_item_id"] = str(baidu_item_id)
        resp = self._call(
            "POST", "/api/plugin/v1/imports/begin", json_body=body,
            idempotency_key=key, timeout=self._cfg.control_timeout)
        return self._json(resp)

    def import_topup(self, import_id, write_token, extra_bytes):
        """§4.3 final 补占（写块越 declared_size 界前）。"""
        resp = self._call(
            "POST", "/api/plugin/v1/imports/%s/topup" % import_id,
            json_body={"extra_bytes": int(extra_bytes)},
            write_token=write_token, timeout=self._cfg.control_timeout)
        return self._json(resp)

    def import_write_chunk(self, import_id, write_token, offset, chunk):
        """§1.3：一块字节流（offset + 逐块 sha256）。"""
        import hashlib
        digest = hashlib.sha256(chunk).hexdigest()

        def _hdrs():
            h = self._headers(write_token)
            h["Content-Type"] = "application/octet-stream"
            h["X-Import-Offset"] = str(int(offset))
            h["X-Import-Chunk-Sha256"] = digest
            return h

        try:
            resp = self._transport.request(
                "POST", "/api/plugin/v1/imports/%s/write" % import_id,
                data=bytes(chunk), headers=_hdrs(),
                timeout=self._cfg.write_timeout, response_lost_capable=True)
        except errors.ContractError as e:
            if e.code == "token_expired":
                self.ensure_token(force=True)
                resp = self._transport.request(
                    "POST", "/api/plugin/v1/imports/%s/write" % import_id,
                    data=bytes(chunk), headers=_hdrs(),
                    timeout=self._cfg.write_timeout,
                    response_lost_capable=True)
            else:
                raise
        return self._json(resp)

    def import_commit(self, import_id, write_token, declared_sha256):
        """§1.4：同步 commit；回执丢失由调用方转 status（§1.5）。"""
        resp = self._call(
            "POST", "/api/plugin/v1/imports/%s/commit" % import_id,
            json_body={"declared_sha256": str(declared_sha256).lower()},
            write_token=write_token, timeout=self._cfg.commit_timeout,
            response_lost_capable=True)
        return self._json(resp)

    def import_status(self, import_id, write_token=None):
        """§1.5：只读幂等；终态含稳定回执。"""
        resp = self._call(
            "GET", "/api/plugin/v1/imports/%s/status" % import_id,
            write_token=write_token, timeout=self._cfg.control_timeout)
        return self._json(resp)

    def import_cancel(self, import_id, write_token):
        """§1.6：created/writing → cancelled；终态幂等。"""
        resp = self._call(
            "POST", "/api/plugin/v1/imports/%s/cancel" % import_id,
            json_body={}, write_token=write_token,
            timeout=self._cfg.control_timeout)
        return self._json(resp)

    def import_scratch(self, import_id, write_token, *, total_bytes=None,
                       delta_bytes=None):
        """§1.7 scratch 补占（total/delta 二选一；重复同值幂等）。"""
        if (total_bytes is None) == (delta_bytes is None):
            raise errors.ContractError(400, "invalid_request",
                                       "scratch 需要 total/delta 恰一")
        body = ({"total_bytes": int(total_bytes)}
                if total_bytes is not None
                else {"delta_bytes": int(delta_bytes)})
        resp = self._call(
            "POST", "/api/plugin/v1/imports/%s/scratch" % import_id,
            json_body=body, write_token=write_token,
            timeout=self._cfg.control_timeout)
        return self._json(resp)

    def import_cleanup_confirm(self, import_id, write_token):
        """§1.7/§5：受管根清理确认（非空 → 409 cleanup_not_verified）。"""
        resp = self._call(
            "POST", "/api/plugin/v1/imports/%s/cleanup-confirm" % import_id,
            json_body={}, write_token=write_token,
            timeout=self._cfg.control_timeout)
        return self._json(resp)

    # ------------------------------------------------------------------ #
    # 百度驱动桥（§6.3；平台 as-built：claim/heartbeat 在 /baidu/batches/*，
    # report 在 /baidu/items/<id>/report 且 fields 嵌套 + 白名单）
    # ------------------------------------------------------------------ #

    @staticmethod
    def _normalize_claim(payload):
        """平台桥 claim 视图 → 驱动器消费的规范形态。

        平台 as-built（baidu_import_store.plugin_claim_batch）：batch 是
        public_view（``id``）+ 顶层 share 凭证 + ``lease`` 嵌套；items 用
        ``id``/字符串 source_size。此处归一为 batch_id/lease_token/整型
        source_size（stub 平台同走本归一，两侧同源）。
        """
        batch = payload.get("batch") or {}
        lease = batch.get("lease") or {}
        items = []
        for i in (payload.get("items") or []):
            items.append({
                "item_id": str(i.get("id") or i.get("item_id") or ""),
                "batch_id": str(i.get("batch_id")
                                or batch.get("id") or ""),
                "name": str(i.get("name") or ""),
                "fs_id": (str(i["fs_id"]) if i.get("fs_id") else None),
                "source_size": int(i.get("source_size") or 0),
                "stage": str(i.get("stage") or "queued"),
                "error_code": i.get("error_code"),
                "slide_id": i.get("slide_id"),
                "companion_fs_id": (str(i["companion_fs_id"])
                                    if i.get("companion_fs_id") else None),
                "companion_name": i.get("companion_name"),
            })
        return {
            "batch": {
                "batch_id": str(batch.get("id") or batch.get("batch_id")
                                or ""),
                "state": batch.get("state"),
                "target_project_id": batch.get("target_project_id"),
                "owner_user_id": batch.get("owner_user_id"),
                "share_url": batch.get("share_url") or "",
                "extraction_code": batch.get("extraction_code") or "",
                "cancel_requested": bool(batch.get("cancel_requested")),
                "lease_token": str(lease.get("lease_token") or ""),
                "lease_expires_at": lease.get("lease_expires_at"),
            },
            "items": items,
        }

    def baidu_claim(self):
        """领取一条批次（无 → None）。worker_id 平台侧从 JWT 派生。"""
        resp = self._call(
            "POST", "/api/plugin/v1/baidu/batches/claim",
            json_body={"lease_seconds": int(self._cfg.lease_seconds)},
            timeout=self._cfg.control_timeout)
        payload = self._json(resp)
        if payload.get("claimed") is False:
            return None
        if not payload.get("batch"):
            return None
        return self._normalize_claim(payload)

    def baidu_heartbeat(self, batch_id, lease_token):
        """续租（平台 as-built：ok=False 即租约被夺）；
        返回 (ok, cancel_requested)。"""
        try:
            resp = self._call(
                "POST", "/api/plugin/v1/baidu/batches/heartbeat",
                json_body={"batch_id": batch_id,
                           "lease_token": lease_token,
                           "lease_seconds": int(self._cfg.lease_seconds)},
                timeout=self._cfg.control_timeout)
        except errors.ContractError as e:
            if e.code in ("import_state_invalid", "not_found",
                          "conflict", "forbidden"):
                raise errors.LeaseLostError() from None
            raise
        payload = self._json(resp)
        if not payload.get("ok"):
            raise errors.LeaseLostError()
        return True, bool(payload.get("cancel_requested"))

    def baidu_report_item(self, item_id, batch_id, lease_token, fields):
        """条目状态回写（fields 嵌套 + 平台白名单；fence 拒绝 →
        LeaseLostError）。"""
        body = {"batch_id": batch_id, "lease_token": lease_token,
                "fields": dict(fields or {})}
        try:
            self._call(
                "POST", "/api/plugin/v1/baidu/items/%s/report" % item_id,
                json_body=body, timeout=self._cfg.control_timeout)
        except errors.ContractError as e:
            if e.code in ("import_state_invalid", "not_found", "conflict",
                          "forbidden"):
                raise errors.LeaseLostError() from None
            raise
        return True

    def close(self):
        self._transport.close()
