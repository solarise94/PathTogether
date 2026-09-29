# -*- coding: utf-8 -*-
"""C5-B 测试用 stub 平台：进程内 HTTP 服务器，忠实实现合同可测面。

实现范围（对照 docs/slide-tools/c5-producer-import-contract.md）：

- §1.1 认证：``POST /api/plugin/v1/auth/token``（installation secret →
  opaque JWT 替身，带过期；installation 每请求回查 enabled → 401）。
- §1.2 begin：grant 校验链（§2.3）、幂等域 (installation, key)——同键同载
  荷重放**不重发 write_token**（终态回执/原状态）、同键异载荷 409
  ``idempotency_conflict``、declared_size 漂移拒绝。
- §1.3 write：JWT+scope+write_token 三层（不符 403）、state 门
  （409 import_state_invalid）、offset==confirmed_offset（409
  offset_conflict + details.expected_offset）、写前容量闸（413
  size_exceeded）、逐块 sha256（409 checksum_mismatch）、流式落盘。
- §4.3 topup：final 补占（declared_size 只增）；配额不足 413
  ``upload_quota_exceeded``。
- §1.4 commit：完整性（409 incomplete_write）、平台自算 sha256（声明不符
  422 declared_checksum_mismatch，无 intent 仍 writing）、魔法嗅探（422
  format_unsupported → failed）、稳定回执幂等（重发/读 status 同回执，
  不再建资产）、**响应丢失注入**（受理后断连）。
- §1.5 status / §1.6 cancel（committing → 409 commit_in_progress；终态
  幂等）/ §1.7 scratch（total/delta、重复同值幂等、413 配额）与
  cleanup-confirm（受管根非空 → 409 cleanup_not_verified + residual_bytes；
  通过 → scratch 释放**恰一次**、plugin_cleanup_status=cleaned；重复幂等）。
- §1.1/§1.8 限流与错误信封：可配置 per-路径窗口 → 429 + Retry-After。
- §6.3 桥：claim（SKIP-LOCKED 语义近似：一次一个租约）、heartbeat（
  lease_token fence）、items/<id>/report（fence；条目字段回写）。

计数器（``counters``）供断言：begin/write/commit/status/cancel/scratch/
topup/cleanup_confirm/release_scratch/publish 等。绝不打印请求体/凭证。
"""

from __future__ import annotations

import hashlib
import json
import re
import secrets
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

_IDEMPOTENCY_KEY_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
_SLIDE_PREFIX = "sld_"
_IMPORT_PREFIX = "pim_"
_GRANT_PREFIX = "pig_"

_MAGIC_SNIFFS = (
    (b"II*\x00", "tiff"), (b"MM\x00*", "tiff"),
    (b"II+\x00", "tiff"), (b"MM\x00+", "tiff"),  # BigTIFF（KFB/KFBF 产物）
    (b"\xff\xd8\xff", "jpeg"), (b"BM", "bmp"),
)

TERMINAL_STATES = ("published", "done", "failed", "cancelled")


def _err(status, code, message="", retryable=None, details=None):
    body = {"error": {"code": code, "message": message or code,
                      "retryable": bool(retryable)
                      if retryable is not None else False}}
    if details is not None:
        body["error"]["details"] = details
    return status, body


def _ok(body=None, status=200):
    return status, (body if body is not None else {})


class StubPlatformState:
    """全部服务端状态（线程锁保护；测试直接注入故障/读计数）。"""

    def __init__(self, *, share_data_dir, installation_id="inst_test",
                 secret="test-secret", scopes=("slide:import",),
                 chunk_max_bytes=65536):
        self.lock = threading.RLock()
        self.share_data_dir = Path(share_data_dir)
        self.installation_id = installation_id
        self.secret = secret
        self.scopes = list(scopes)
        self.enabled = True
        self.chunk_max_bytes = int(chunk_max_bytes)

        self.tokens = {}          # token -> expires_at (monotonic+wall)
        self.grants = {}          # grant_id -> {...}
        self.imports = {}         # import_id -> row dict
        self.idempotency = {}     # (installation, key) -> import_id
        self.batches = {}         # batch_id -> {...}
        self.items = {}           # item_id -> {...}

        # 故障/策略注入
        self.quota_bytes = None           # None=不限；scratch+final 合计口径
        self.rate_limits = {}             # path-prefix -> {"n","window","retry_after"}
        self.lose_commit_response = 0     # 剩余次数（受理后断连）
        self.cleanup_confirm_hook = None  # callable(row)——确认前注入残迹
        self.grant_check_hook = None      # callable(grant) -> None|err
        self.write_hook = None            # callable(row, offset) -> None|err
        self.begin_hook = None            # callable(body) -> None|err

        self.counters = {}
        self._rate_hits = {}

    # -- 计数 ------------------------------------------------------------ #

    def bump(self, name, n=1):
        self.counters[name] = self.counters.get(name, 0) + n

    # -- grant ----------------------------------------------------------- #

    def add_grant(self, grant_id, *, project_id, user_id="usr_owner",
                  ttl_seconds=86400, installation_id=None):
        self.grants[grant_id] = {
            "grant_id": grant_id,
            "installation_id": installation_id or self.installation_id,
            "user_id": user_id,
            "project_id": project_id,
            "expires_at": time.time() + ttl_seconds,
            "revoked": False,
        }

    def revoke_grant(self, grant_id):
        g = self.grants.get(grant_id)
        if g:
            g["revoked"] = True

    def check_grant(self, grant_id, *, installation_id, project_id):
        g = self.grants.get(grant_id)
        if g is None:
            return _err(403, "import_grant_invalid", "grant_not_found",
                        details={"reason": "grant_not_found"})
        if g["revoked"]:
            return _err(403, "import_grant_invalid", "grant_revoked",
                        details={"reason": "grant_revoked"})
        if g["expires_at"] < time.time():
            return _err(403, "import_grant_invalid", "grant_expired",
                        details={"reason": "grant_expired"})
        if g["installation_id"] != installation_id:
            return _err(403, "import_grant_invalid", "installation_mismatch",
                        details={"reason": "installation_mismatch"})
        if project_id is not None and g["project_id"] != project_id:
            return _err(403, "import_grant_invalid", "project_mismatch",
                        details={"reason": "project_mismatch"})
        return None

    # -- 桥数据 ---------------------------------------------------------- #

    def add_batch(self, batch_id, *, share_url, extraction_code, items,
                  target_project_id="prj_test", owner_user_id="usr_owner"):
        self.batches[batch_id] = {
            "batch_id": batch_id, "state": "queued",
            "share_url": share_url, "extraction_code": extraction_code,
            "target_project_id": target_project_id,
            "owner_user_id": owner_user_id,
            "cancel_requested": False,
            "lease_owner": None, "lease_token": None,
            "lease_expires_at": 0.0,
        }
        for item in items:
            self.items[item["item_id"]] = {
                "item_id": item["item_id"], "batch_id": batch_id,
                "name": item["name"], "fs_id": item["fs_id"],
                "source_size": int(item["source_size"]),
                "stage": item.get("stage", "queued"),
                "error_code": None, "import_id": None, "slide_id": None,
                "companion_fs_id": item.get("companion_fs_id"),
                "companion_name": item.get("companion_name"),
            }

    # -- 容量 ------------------------------------------------------------ #

    def _charged_bytes(self, exclude_import=None):
        total = 0
        for row in self.imports.values():
            if exclude_import is not None and \
                    row["import_id"] == exclude_import:
                continue
            if row["scratch_released"]:
                total += int(row["declared_size"])
            else:
                total += int(row["declared_size"]) + \
                    int(row["scratch_confirmed_bytes"])
        return total

    def _quota_ok(self, extra_bytes, exclude_import=None):
        if self.quota_bytes is None:
            return True
        return self._charged_bytes(exclude_import) + int(extra_bytes) \
            <= self.quota_bytes

    # -- 受管根（§5 平台派生路径） --------------------------------------- #

    def managed_root(self, import_id):
        return self.share_data_dir / "plugin-work" / self.installation_id \
            / "imports" / import_id

    def staging_file(self, import_id):
        d = self.share_data_dir / "staging" / import_id
        d.mkdir(parents=True, exist_ok=True)
        return d / "data"


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "StubC5/1.0"

    # -- 基础 ------------------------------------------------------------ #

    @property
    def state(self):
        return self.server.state

    def log_message(self, *args):
        return  # 静默：绝不打印含凭证的请求行

    def _send(self, status, body, *, extra_headers=None):
        if status == 204:
            self.send_response(204)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        payload = json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        for k, v in (extra_headers or {}).items():
            self.send_header(k, str(v))
        self.end_headers()
        self.wfile.write(payload)

    def _drop(self):
        """受理后断连（响应丢失注入）。"""
        try:
            self.close_connection = True
            self.connection.close()
        except OSError:
            pass

    def _body_json(self):
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n) if n else b""
        if not raw:
            return {}
        try:
            out = json.loads(raw.decode("utf-8"))
            return out if isinstance(out, dict) else {}
        except ValueError:
            return {}

    def _read_body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(n) if n else b""

    # -- 鉴权 ------------------------------------------------------------ #

    def _auth(self, required_scope=None):
        """返回 (claims, None) 或 (None, err)。opaque token + enabled 回查。"""
        authz = self.headers.get("Authorization") or ""
        if not authz.startswith("Bearer "):
            return None, _err(401, "unauthorized", "缺少 Bearer token")
        token = authz[len("Bearer "):].strip()
        with self.state.lock:
            exp = self.state.tokens.get(token)
            if exp is None:
                return None, _err(401, "unauthorized", "token 无效")
            if exp < time.time():
                return None, _err(401, "token_expired", "token 已过期")
            if not self.state.enabled:
                return None, _err(401, "unauthorized",
                                  "插件安装不存在或已停用")
            scopes = list(self.state.scopes)
        if required_scope and required_scope not in scopes:
            return None, _err(403, "forbidden", "scope 不足")
        return {"sub": self.state.installation_id,
                "scope": " ".join(scopes)}, None

    def _rate_gate(self, path):
        for prefix, cfg in self.state.rate_limits.items():
            hit = path.endswith(prefix) if cfg.get("suffix") \
                else path.startswith(prefix)
            if hit:
                now = time.monotonic()
                bucket = self.state._rate_hits.setdefault(prefix, [])
                with self.state.lock:
                    bucket[:] = [t for t in bucket
                                 if now - t < cfg["window"]]
                    if len(bucket) >= cfg["n"]:
                        retry_after = int(cfg.get("retry_after", 1))
                        return retry_after
                    bucket.append(now)
        return None

    # -- 路由 ------------------------------------------------------------ #

    def do_POST(self):
        self._route("POST")

    def do_GET(self):
        self._route("GET")

    def _route(self, method):
        try:
            self._dispatch(method)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:  # noqa: BLE001 — stub 兜底不静默
            try:
                self._send(500, {"error": {"code": "internal",
                                           "message": type(e).__name__,
                                           "retryable": True}})
            except OSError:
                pass

    def _dispatch(self, method):
        path = self.path.split("?", 1)[0]
        wait = self._rate_gate(path)
        if wait is not None:
            self.state.bump("rate_limited")
            self._send(429, {"error": {"code": "rate_limited",
                                       "message": "限流",
                                       "retryable": True,
                                       "details": {"retry_after": wait}}},
                       extra_headers={"Retry-After": wait})
            return

        if path == "/api/plugin/v1/auth/token" and method == "POST":
            return self._auth_token()
        m = re.fullmatch(r"/api/plugin/v1/imports/([A-Za-z0-9_-]+)/(write|"
                         r"commit|cancel|scratch|topup|cleanup-confirm)",
                         path)
        if m and method == "POST":
            return self._import_op(m.group(1), m.group(2))
        m = re.fullmatch(r"/api/plugin/v1/imports/([A-Za-z0-9_-]+)/status",
                         path)
        if m and method == "GET":
            return self._import_status(m.group(1))
        if path == "/api/plugin/v1/imports/begin" and method == "POST":
            return self._import_begin()
        if path == "/api/plugin/v1/baidu/batches/claim" and method == "POST":
            return self._baidu_claim()
        if path == "/api/plugin/v1/baidu/batches/heartbeat" \
                and method == "POST":
            return self._baidu_heartbeat()
        m = re.fullmatch(
            r"/api/plugin/v1/baidu/items/([A-Za-z0-9_-]+)/report", path)
        if m and method == "POST":
            return self._baidu_report(m.group(1))
        self._send(*_err(404, "not_found", "路由不存在"))

    # -- /auth/token ------------------------------------------------------ #

    def _auth_token(self):
        body = self._body_json()
        with self.state.lock:
            if body.get("installation_id") != self.state.installation_id \
                    or body.get("secret") != self.state.secret:
                return self._send(*_err(401, "unauthorized", "安装凭证无效"))
            if not self.state.enabled:
                return self._send(*_err(401, "unauthorized",
                                        "插件安装不存在或已停用"))
            token = "tok_" + secrets.token_hex(16)
            self.state.tokens[token] = time.time() + 900
        self.state.bump("auth_token")
        self._send(200, {"access_token": token, "expires_in": 900,
                         "token_type": "bearer"})

    # -- begin ------------------------------------------------------------- #

    def _import_begin(self):
        claims, err = self._auth("slide:import")
        if err:
            return self._send(*err)
        body = self._body_json()
        key = (self.headers.get("Idempotency-Key") or "").strip()
        if not _IDEMPOTENCY_KEY_RE.match(key):
            return self._send(*_err(400, "invalid_request",
                                    "Idempotency-Key 缺失/非法"))
        if body.get("idempotency_key") not in (None, key):
            return self._send(*_err(400, "invalid_request",
                                    "idempotency_key 与头不一致"))
        with self.state.lock:
            self.state.bump("begin")
        grant_id = body.get("grant_id")
        project_id = body.get("project_id")
        gerr = self.state.check_grant(
            grant_id, installation_id=claims["sub"], project_id=project_id)
        if gerr:
            self.state.bump("begin_grant_refused")
            return self._send(*gerr)
        if self.state.grant_check_hook:
            hook_err = self.state.grant_check_hook(grant_id)
            if hook_err:
                return self._send(*hook_err)
        if self.state.begin_hook:
            hook_err = self.state.begin_hook(body)
            if hook_err:
                return self._send(*hook_err)
        with self.state.lock:
            existing = self.state.idempotency.get((claims["sub"], key))
            if existing is not None:
                row = self.state.imports[existing]
                digest = row["payload_sha256"]
                if digest != hashlib.sha256(json.dumps(
                        body, sort_keys=True).encode()).hexdigest():
                    return self._send(*_err(409, "idempotency_conflict",
                                            "同键异载荷"))
                # 同键同载荷重放：write_token 不重发（§1.2）
                if row["state"] in TERMINAL_STATES:
                    return self._send(200, self._receipt(row))
                return self._send(200, {
                    "import_id": row["import_id"],
                    "slide_id": row["slide_id"], "state": row["state"],
                    "chunk_max_bytes": self.state.chunk_max_bytes,
                    "confirmed_offset": row["confirmed_offset"],
                    "idempotency_key": key})
            declared = int(body.get("declared_size") or 0)
            if declared <= 0:
                return self._send(*_err(400, "invalid_request",
                                        "declared_size 非法"))
            scratch = int(body.get("scratch_bytes") or 0)
            if not self.state._quota_ok(declared + scratch):
                return self._send(*_err(413, "upload_quota_exceeded",
                                        "配额不足"))
            import_id = _IMPORT_PREFIX + secrets.token_hex(8)
            slide_id = _SLIDE_PREFIX + secrets.token_hex(8)
            write_token = "wt_" + secrets.token_hex(16)
            row = {
                "import_id": import_id, "slide_id": slide_id,
                "installation_id": claims["sub"], "grant_id": grant_id,
                "project_id": project_id, "idempotency_key": key,
                "payload_sha256": hashlib.sha256(json.dumps(
                    body, sort_keys=True).encode()).hexdigest(),
                "filename": body.get("filename"),
                "format_ext": body.get("format_ext"),
                "declared_size": declared, "confirmed_offset": 0,
                "received_bytes": 0, "state": "created",
                "write_token": write_token,
                "write_token_issued": True,
                "sha256_actual": None, "fail_code": None,
                "scratch_confirmed_bytes": scratch,
                "scratch_released": False,
                "plugin_cleanup_status": "none",
                "cleanup_confirm_calls": 0,
                "cancel_calls": 0, "commit_calls": 0,
                "receipt": None, "terminal_at": None,
                "write_token_validated": 0,
            }
            self.state.imports[import_id] = row
            self.state.idempotency[(claims["sub"], key)] = import_id
            # §6.3：条目发布不另开端点——begin 携带 baidu_item_id 时把
            # 预分配 slide_id 绑定到条目（平台 _allocate_item_slide 同义）
            baidu_item_id = str(body.get("baidu_item_id") or "")
            if baidu_item_id and baidu_item_id in self.state.items:
                self.state.items[baidu_item_id]["slide_id"] = slide_id
        return self._send(201, {
            "import_id": import_id, "slide_id": slide_id,
            "state": "created", "write_token": write_token,
            "chunk_max_bytes": self.state.chunk_max_bytes,
            "confirmed_offset": 0, "idempotency_key": key})

    # -- write/commit/cancel/scratch/topup/cleanup-confirm ---------------- #

    def _import_op(self, import_id, op):
        claims, err = self._auth("slide:import")
        if err:
            return self._send(*err)
        with self.state.lock:
            row = self.state.imports.get(import_id)
        if row is None or row["installation_id"] != claims["sub"]:
            return self._send(*_err(404, "import_not_found"))
        token = self.headers.get("X-Import-Token") or ""
        if not secrets.compare_digest(token, row["write_token"]):
            self.state.bump("write_token_rejected")
            return self._send(*_err(403, "forbidden", "任务凭证不匹配"))

        if op == "write":
            return self._write(row)
        if op == "topup":
            return self._topup(row)
        if op == "scratch":
            return self._scratch(row)
        if op == "cancel":
            return self._cancel(row)
        if op == "commit":
            return self._commit(row)
        if op == "cleanup-confirm":
            return self._cleanup_confirm(row)
        return self._send(*_err(404, "not_found"))

    def _write(self, row):
        self.state.bump("write")
        with self.state.lock:
            if row["state"] not in ("created", "writing"):
                return self._send(*_err(409, "import_state_invalid",
                                        "state=%s" % row["state"],
                                        details={"state": row["state"]}))
            try:
                offset = int(self.headers.get("X-Import-Offset", ""))
            except ValueError:
                return self._send(*_err(400, "invalid_request",
                                        "offset 头非法"))
            chunk_sha = (self.headers.get("X-Import-Chunk-Sha256")
                         or "").lower()
            if not re.fullmatch(r"[0-9a-f]{64}", chunk_sha):
                return self._send(*_err(400, "invalid_request",
                                        "块摘要头非法"))
        body = self._read_body()
        if not body:
            return self._send(*_err(400, "invalid_request", "空块"))
        if len(body) > self.state.chunk_max_bytes:
            return self._send(*_err(413, "size_exceeded", "块超上限"))
        if self.state.write_hook:
            hook_err = self.state.write_hook(row, offset)
            if hook_err:
                return self._send(*hook_err)
        with self.state.lock:
            if row["state"] not in ("created", "writing"):
                return self._send(*_err(409, "import_state_invalid",
                                        "state=%s" % row["state"],
                                        details={"state": row["state"]}))
            if offset != row["confirmed_offset"]:
                return self._send(*_err(
                    409, "offset_conflict", "offset 不等于 confirmed_offset",
                    details={"expected_offset": row["confirmed_offset"]}))
            if row["confirmed_offset"] + len(body) > row["declared_size"]:
                return self._send(*_err(413, "size_exceeded",
                                        "越 declared_size（先 topup）"))
            actual = hashlib.sha256(body).hexdigest()
            if actual != chunk_sha:
                return self._send(*_err(409, "checksum_mismatch",
                                        "块 sha256 不符"))
            staging = self.state.staging_file(row["import_id"])
            with open(staging, "ab") as fh:
                fh.write(body)
                fh.flush()
            row["confirmed_offset"] += len(body)
            row["received_bytes"] = row["confirmed_offset"]
            row["state"] = "writing"
        return self._send(200, {
            "confirmed_offset": row["confirmed_offset"],
            "remaining_final_bytes":
                row["declared_size"] - row["confirmed_offset"]})

    def _topup(self, row):
        self.state.bump("topup")
        body = self._body_json()
        extra = int(body.get("extra_bytes") or 0)
        if extra <= 0:
            return self._send(*_err(400, "invalid_request",
                                    "extra_bytes 非法"))
        with self.state.lock:
            # 补占是加量：既有 charged（含本任务 declared+scratch）+extra
            if not self.state._quota_ok(extra):
                return self._send(*_err(413, "upload_quota_exceeded",
                                        "配额不足"))
            row["declared_size"] += extra
        return self._send(200, {"declared_size": row["declared_size"]})

    def _scratch(self, row):
        self.state.bump("scratch")
        body = self._body_json()
        total = body.get("total_bytes")
        delta = body.get("delta_bytes")
        if (total is None) == (delta is None):
            return self._send(*_err(400, "invalid_request",
                                    "total/delta 恰一"))
        with self.state.lock:
            new_total = int(total) if total is not None else \
                row["scratch_confirmed_bytes"] + int(delta)
            if new_total < row["scratch_confirmed_bytes"]:
                new_total = row["scratch_confirmed_bytes"]  # 补占只增
            extra = new_total - row["scratch_confirmed_bytes"]
            if extra and not self.state._quota_ok(extra):
                return self._send(*_err(413, "upload_quota_exceeded",
                                        "配额不足"))
            row["scratch_confirmed_bytes"] = new_total
        return self._send(200, {"scratch_confirmed_bytes":
                                row["scratch_confirmed_bytes"]})

    def _cancel(self, row):
        self.state.bump("cancel")
        with self.state.lock:
            if row["state"] == "committing":
                return self._send(*_err(409, "commit_in_progress",
                                        "intent 后取消被拒"))
            if row["state"] in TERMINAL_STATES:
                return self._send(200, self._receipt(row))
            row["state"] = "cancelled"
            row["cancel_calls"] += 1
            row["terminal_at"] = time.time()
        return self._send(200, {"import_id": row["import_id"],
                                "state": "cancelled",
                                "slide_id": row["slide_id"],
                                "cleanup_status":
                                    row["plugin_cleanup_status"]})

    def _commit(self, row):
        self.state.bump("commit")
        body = self._body_json()
        declared_sha = (body.get("declared_sha256") or "").lower()
        with self.state.lock:
            if row["state"] == "committing":
                return self._send(*_err(409, "import_state_invalid",
                                        "committing"))
            if row["state"] in ("published", "done"):
                # 回执幂等（§1.4 第 7 步）：重发 commit 同一回执
                return self._send(200, self._receipt(row))
            if row["state"] in ("failed", "cancelled"):
                return self._send(*_err(409, "import_state_invalid",
                                        "state=%s" % row["state"]))
            if row["confirmed_offset"] != row["declared_size"]:
                return self._send(*_err(409, "incomplete_write",
                                        "字节不齐"))
            staging = self.state.staging_file(row["import_id"])
            data = staging.read_bytes()
            actual_sha = hashlib.sha256(data).hexdigest()
            if declared_sha and declared_sha != actual_sha:
                # §8 裁决 6：422；无 intent、保持 writing
                self.state.bump("declared_checksum_mismatch")
                return self._send(*_err(422, "declared_checksum_mismatch",
                                        "声明摘要与平台自算不符"))
            head = data[:4]
            if not any(head.startswith(magic) for magic, _
                       in _MAGIC_SNIFFS):
                row["state"] = "failed"
                row["fail_code"] = "format_unsupported"
                row["terminal_at"] = time.time()
                return self._send(*_err(422, "format_unsupported",
                                        "格式/查看能力不过"))
            row["commit_calls"] += 1
            row["state"] = "committing"   # intent 落库（同步端点立即收口）
            row["sha256_actual"] = actual_sha
            # —— 结算（发布）——
            row["state"] = "published"
            row["terminal_at"] = time.time()
            row["receipt"] = {
                "import_id": row["import_id"], "state": "published",
                "slide_id": row["slide_id"], "revision": 1,
                "sha256": actual_sha, "accounted_bytes": len(data),
                "cleanup_status": row["plugin_cleanup_status"]}
            self.state.bump("publish")
            if self.state.lose_commit_response > 0:
                self.state.lose_commit_response -= 1
                self._drop()   # 受理后断连：回执只能经 status 读回
                return
        return self._send(200, self._receipt(row))

    def _cleanup_confirm(self, row):
        self.state.bump("cleanup_confirm")
        with self.state.lock:
            row["cleanup_confirm_calls"] += 1
            root = self.state.managed_root(row["import_id"])
            if self.state.cleanup_confirm_hook is not None:
                self.state.cleanup_confirm_hook(row)
            residual = 0
            if root.exists():
                for p in root.rglob("*"):
                    if p.is_file():
                        try:
                            residual += p.stat().st_size
                        except OSError:
                            residual += 1
            if residual > 0:
                self.state.bump("cleanup_not_verified")
                return self._send(*_err(
                    409, "cleanup_not_verified", "受管根非空",
                    details={"residual_bytes": residual}))
            if not row["scratch_released"]:
                row["scratch_released"] = True
                self.state.bump("release_scratch")
            row["plugin_cleanup_status"] = "cleaned"
        return self._send(200, {"import_id": row["import_id"],
                                "cleanup_status": "cleaned"})

    # -- status ------------------------------------------------------------ #

    @staticmethod
    def _receipt(row):
        if row["receipt"] is not None:
            return dict(row["receipt"])
        return {"import_id": row["import_id"], "state": row["state"],
                "slide_id": row["slide_id"],
                "cleanup_status": row["plugin_cleanup_status"]}

    def _import_status(self, import_id):
        claims, err = self._auth("slide:import")
        if err:
            return self._send(*err)
        with self.state.lock:
            row = self.state.imports.get(import_id)
        if row is None or row["installation_id"] != claims["sub"]:
            return self._send(*_err(404, "import_not_found"))
        self.state.bump("status")
        body = {
            "import_id": row["import_id"], "state": row["state"],
            "slide_id": row["slide_id"],
            "confirmed_offset": row["confirmed_offset"],
            "declared_size": row["declared_size"],
            "remaining_final_bytes":
                max(0, row["declared_size"] - row["confirmed_offset"]),
            "scratch_confirmed_bytes": row["scratch_confirmed_bytes"],
            "scratch_released": row["scratch_released"],
            "cleanup_status": row["plugin_cleanup_status"],
            "fail_code": row["fail_code"],
            "terminal_at": row["terminal_at"],
        }
        if row["state"] in TERMINAL_STATES:
            body["receipt"] = self._receipt(row)
            if row["state"] == "published":
                body["revision"] = 1
                body["sha256"] = row["sha256_actual"]
                body["accounted_bytes"] = row["received_bytes"]
        self._send(200, body)

    # -- 百度桥（§6.3） ---------------------------------------------------- #

    def _baidu_claim(self):
        """平台 as-built：{claimed:false}（200）或 {claimed:true,...}；
        batch 用 ``id`` + 嵌套 ``lease``；条目用 ``id`` + 字符串 size。"""
        claims, err = self._auth("slide:import")
        if err:
            return self._send(*err)
        body = self._body_json()
        worker_id = "plugin:%s" % claims["sub"]
        lease_seconds = int(body.get("lease_seconds") or 1800)
        lease_seconds = max(60, min(4 * 3600, lease_seconds))
        with self.state.lock:
            self.state.bump("claim")
            now = time.time()
            for batch in self.state.batches.values():
                if batch["state"] in ("queued", "running") and \
                        (batch["lease_token"] is None
                         or batch["lease_expires_at"] < now):
                    batch["state"] = "running"
                    batch["lease_owner"] = worker_id
                    batch["lease_token"] = "lst_" + secrets.token_hex(8)
                    batch["lease_expires_at"] = now + lease_seconds
                    items = [dict(i) for i in self.state.items.values()
                             if i["batch_id"] == batch["batch_id"]]
                    for it in items:
                        it["id"] = it.pop("item_id")
                        it["source_size"] = str(it["source_size"])
                    return self._send(200, {
                        "claimed": True,
                        "batch": {
                            "id": batch["batch_id"],
                            "state": batch["state"],
                            "target_project_id":
                                batch["target_project_id"],
                            "cancel_requested":
                                batch["cancel_requested"],
                            "lease": {
                                "worker_id": worker_id,
                                "lease_token": batch["lease_token"],
                                "lease_expires_at":
                                    batch["lease_expires_at"],
                            },
                            "share_url": batch["share_url"],
                            "extraction_code": batch["extraction_code"],
                        },
                        "items": items})
        self._send(200, {"claimed": False})

    def _baidu_heartbeat(self):
        claims, err = self._auth("slide:import")
        if err:
            return self._send(*err)
        body = self._body_json()
        with self.state.lock:
            self.state.bump("heartbeat")
            batch = self.state.batches.get(body.get("batch_id"))
            if batch is None or batch["state"] != "running" or \
                    batch["lease_token"] != body.get("lease_token"):
                return self._send(200, {"ok": False})
            batch["lease_expires_at"] = time.time() + 1800
            return self._send(200, {"ok": True,
                                    "cancel_requested":
                                        batch["cancel_requested"]})

    _REPORT_FIELDS = frozenset({
        "stage", "error_code", "ingest_token", "source_sha256",
        "slide_name", "project_associate_state", "transfer_task_id",
        "staging_path",
    })
    _REPORT_STAGES = frozenset({
        "queued", "transferring", "downloading", "validating",
        "converting", "ingesting", "ready", "failed", "cancelled",
    })

    def _baidu_report(self, item_id):
        claims, err = self._auth("slide:import")
        if err:
            return self._send(*err)
        body = self._body_json()
        fields = body.get("fields") or {}
        if not isinstance(fields, dict):
            return self._send(*_err(400, "invalid_request",
                                    "fields 需为 JSON object"))
        for key in fields:
            if key not in self._REPORT_FIELDS:
                return self._send(*_err(400, "invalid_request",
                                        "字段 %r 不在白名单" % key))
        if "stage" in fields and fields["stage"] not in self._REPORT_STAGES:
            return self._send(*_err(400, "invalid_request",
                                    "stage 非法：%r" % fields["stage"]))
        if not fields:
            return self._send(*_err(400, "invalid_request",
                                    "report 载荷为空"))
        with self.state.lock:
            self.state.bump("report")
            item = self.state.items.get(item_id)
            batch = self.state.batches.get(body.get("batch_id"))
            if item is None or batch is None or \
                    batch["lease_token"] != body.get("lease_token"):
                return self._send(*_err(409, "conflict",
                                        "lease lost（fencing）"))
            for key, value in fields.items():
                item[key] = value
        self._send(200, {"item": {"id": item_id}})


def serve(state, host="127.0.0.1", port=0):
    """启动 stub（线程化 HTTP）；返回 (server, base_url)。"""
    server = ThreadingHTTPServer((host, port), _Handler)
    server.state = state
    server.daemon_threads = True
    t = threading.Thread(target=server.serve_forever, daemon=True,
                         name="stub-c5-platform")
    t.start()
    return server, "http://%s:%d" % server.server_address
