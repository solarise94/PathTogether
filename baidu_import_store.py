# -*- coding: utf-8 -*-
"""百度分享导入 PG 权威存储 + worker 状态机（W5，spec §6.2/§6.3）。

分层：

- 本模块：枚举/候选/批次/条目的持久化与状态机（migrations/0051）；
- ``baidu_adapter``：外部连接器边界（生产 CLI / 测试 fake）；
- ``baidu_import_http``：HTTP 装配（错误 → 状态码映射）；本模块不 import
  Flask/app，可被 worker 与测试独立驱动。

安全/幂等要点（spec §6.3）：

- 分享 URL 与提取码用 ``BAIDU_SHARE_SECRET_KEY`` 派生 Fernet 加密落库
  （sha256 前缀 ``baidu-share-v1:``，与 registration_mail_worker 同款）；
  缺密钥拒绝持久化；公开视图绝不含密文/明文秘密、staging 路径；
- 枚举只在分享内递归（路径来自当前页返回值），fs_id 去重；深度/条目/
  时限超限 → ``state=failed, error_code=incomplete_limit``（不称完整），
  仅 ``ready`` 且 ``complete`` 且未过期可导入；枚举阶段适配器
  transfer/download/delete 调用计数必须为 0（写入行内审计列）；
- 创建导入不触发转存（worker 才做）；同 owner+幂等键唯一，同键同载荷
  重放返回原批次，同键异载荷 409；配额预占失败零外部副作用；
- 条目崩溃恢复对账：``transfer_task_id``（poll unknown → 先
  ``list_batch_copies`` 对账，不无条件重转存）、``source_sha256`` +
  暂存文件在盘（不重下载）、``ingest_token``（不重复入库）；
- 清理只针对 ``/apps/bdpan/<batch-id>/`` 本批副本；清理失败置
  ``cleanup_state=failed``，绝不回滚 ready。

简化声明（app 集成层接线，见 spec W5/B06）：入库默认钩子只落幂等
凭证 ``ingest_token``（真实转换/入库沿用既有 upload/conversion 收口，
由 ``hooks`` 注入）；批次级配额预占在全部条目终结后一次 ``consume``
（``consume_reservation`` 幂等，崩溃重跑不双扣）。
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import tempfile
import time
from pathlib import Path

import pg_store
import slide_format_registry
import upload_guard
from baidu_adapter import AdapterError, get_adapter  # noqa: F401  (可注入)

# --------------------------------------------------------------------------- #
# 常量（env 可调；测试 monkeypatch 模块属性）
# --------------------------------------------------------------------------- #

def _env_int(name, default):
    try:
        return int((os.environ.get(name) or "").strip() or default)
    except ValueError:
        return default


DEFAULT_MAX_DEPTH = _env_int("BAIDU_ENUM_MAX_DEPTH", 32)
DEFAULT_MAX_ENTRIES = _env_int("BAIDU_ENUM_MAX_ENTRIES", 10000)
ENUMERATION_TIMEOUT_SECONDS = _env_int("BAIDU_ENUM_TIMEOUT_SECONDS", 600)
SHARE_TTL_HOURS = _env_int("BAIDU_SHARE_TTL_HOURS", 24)
PAGE_SIZE = min(100, _env_int("BAIDU_ENUM_PAGE_SIZE", 100))
ENUMERATION_LEASE_SECONDS = _env_int("BAIDU_ENUM_LEASE_SECONDS", 600)
BATCH_LEASE_SECONDS = _env_int("BAIDU_BATCH_LEASE_SECONDS", 1800)
DEFAULT_LIMIT = 50
MAX_LIMIT = 100
STAGING_ROOT = os.environ.get("BAIDU_IMPORT_STAGING_DIR") or str(
    Path(tempfile.gettempdir()) / "baidu-import-staging")

#: 不可重试的条目错误码（其余 failed 条目均可重试）
NON_RETRYABLE_ERROR_CODES = frozenset(
    {"share_invalid", "source_changed", "not_retryable"})

#: 份额秘密加密 env（同 baidu_adapter.ENV_SHARE_SECRET_KEY）
_ENV_SECRET = "BAIDU_SHARE_SECRET_KEY"

#: 取消/失败时释放未消费预占（测试可 monkeypatch）
_release_reservation = upload_guard.release_reservation
_consume_reservation = upload_guard.consume_reservation


# --------------------------------------------------------------------------- #
# 业务异常（http 层按 http_status 映射）
# --------------------------------------------------------------------------- #

class BaiduImportError(Exception):
    code = "baidu_import_error"
    http_status = 400

    def __init__(self, message="", code=None):
        super().__init__(message or self.code)
        if code:
            self.code = code


class ValidationError(BaiduImportError):
    code = "invalid_input"
    http_status = 400


class PermissionDeniedError(BaiduImportError):
    code = "forbidden"
    http_status = 403


class NotFoundError(BaiduImportError):
    code = "not_found"
    http_status = 404


class ConflictError(BaiduImportError):
    code = "state_conflict"
    http_status = 409


class QuotaError(BaiduImportError):
    code = "quota_exceeded"
    http_status = 429


class UnavailableError(BaiduImportError):
    code = "connector_unavailable"
    http_status = 503


# --------------------------------------------------------------------------- #
# Fernet 加密（BAIDU_SHARE_SECRET_KEY 派生；缺密钥拒绝持久化）
# --------------------------------------------------------------------------- #

def _share_secret() -> str:
    v = (os.environ.get(_ENV_SECRET) or "").strip()
    if not v:
        raise UnavailableError(
            "分享秘密加密密钥未配置（secret_unconfigured）",
            code="secret_unconfigured")
    return v


def _fernet():
    from cryptography.fernet import Fernet
    digest = hashlib.sha256(
        ("baidu-share-v1:" + _share_secret()).encode("utf-8")).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def encrypt_text(plain: str) -> str:
    if plain is None:
        return None
    return _fernet().encrypt(str(plain).encode("utf-8")).decode("ascii")


def decrypt_text(enc: str) -> str:
    if not enc:
        return None
    try:
        return _fernet().decrypt(str(enc).encode("ascii")).decode("utf-8")
    except Exception as exc:
        raise UnavailableError(
            "分享秘密解密失败（secret_mismatch）",
            code="secret_mismatch") from exc


def secret_configured() -> bool:
    return bool((os.environ.get(_ENV_SECRET) or "").strip())


# --------------------------------------------------------------------------- #
# 连接与行工具
# --------------------------------------------------------------------------- #

def _connect():
    import psycopg
    conn = pg_store.connect()
    conn.row_factory = psycopg.rows.dict_row
    return conn


def _new_id(prefix):
    return prefix + "_" + secrets.token_hex(12)


def _iso(v):
    return v.isoformat() if hasattr(v, "isoformat") else v


def _dec_str(v):
    return str(int(v or 0))


# --------------------------------------------------------------------------- #
# 能力
# --------------------------------------------------------------------------- #

def capabilities():
    """透传适配器能力（HTTP 层直接使用；不含认证详情）。"""
    return get_adapter().capabilities()


# --------------------------------------------------------------------------- #
# 枚举：创建 / 查询 / 候选
# --------------------------------------------------------------------------- #

def create_enumeration(owner_user_id, share_text, extraction_code=None):
    """登记一次分享枚举（202 queued）。解析失败 400；开关/密钥/连接器
    不可用 503（带 reason_code）。不触发任何外部动作。"""
    import baidu_share_parser as parser

    if not owner_user_id:
        raise PermissionDeniedError("缺少 owner")
    try:
        parsed = parser.parse_share_text(share_text, extraction_code)
    except parser.ShareParseError as exc:
        raise ValidationError(
            "分享文本解析失败（%s）" % exc.code, code=exc.code) from exc

    caps = get_adapter().capabilities()
    if not caps.get("enumeration_available"):
        raise UnavailableError(
            "百度枚举不可用（%s）" % (caps.get("reason_code") or "unknown"),
            code=caps.get("reason_code") or "connector_unavailable")

    enum_id = _new_id("be")
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                cur.execute(
                    "INSERT INTO baidu_enumerations "
                    "(id, owner_user_id, share_url_enc, extraction_enc, "
                    " state, max_depth, max_entries, expires_at) "
                    "VALUES (%s,%s,%s,%s,'queued',%s,%s,"
                    " now() + make_interval(secs => %s))",
                    (enum_id, owner_user_id,
                     encrypt_text(parsed["share_url"]),
                     encrypt_text(parsed["extraction_code"])
                     if parsed["extraction_code"] else None,
                     DEFAULT_MAX_DEPTH, DEFAULT_MAX_ENTRIES,
                     SHARE_TTL_HOURS * 3600))
    finally:
        conn.close()
    return {"id": enum_id, "state": "queued"}


def _expire_if_due(cur, enum_id):
    """惰性过期：queued/enumerating/ready 且 expires_at < now() → expired。"""
    cur.execute(
        "UPDATE baidu_enumerations SET state='expired', updated_at=now() "
        "WHERE id=%s AND state IN ('queued','enumerating','ready') "
        "AND expires_at < now() RETURNING id", (enum_id,))
    return cur.fetchone() is not None


def _get_enum_row(cur, enum_id, owner_user_id):
    cur.execute("SELECT * FROM baidu_enumerations WHERE id=%s", (enum_id,))
    row = cur.fetchone()
    if row is None:
        raise NotFoundError("枚举不存在")
    if owner_user_id is not None and row["owner_user_id"] != owner_user_id:
        # 越权统一 404（不泄露存在性）
        raise NotFoundError("枚举不存在")
    return row


def get_enumeration(enumeration_id, owner_user_id):
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                _expire_if_due(cur, enumeration_id)
                row = _get_enum_row(cur, enumeration_id, owner_user_id)
    finally:
        conn.close()
    return enumeration_public_view(row)


def enumeration_public_view(row):
    """spec §6.2 响应形态；无秘密。"""
    return {
        "id": row["id"],
        "state": row["state"],
        "complete": bool(row["complete"]),
        "scanned_count": int(row["scanned_count"]),
        "candidate_count": int(row["candidate_count"]),
        "error_code": row["error_code"],
        "incomplete_reason": row["incomplete_reason"],
        "expires_at": _iso(row["expires_at"]),
    }


def candidate_public_view(row):
    return {
        "id": row["id"],
        "name": row["name"],
        "relative_path": row["relative_path"],
        "size_bytes": _dec_str(row["size_bytes"]),
        "format": row["format"],
        "capability": row["capability"],
        "selectable": bool(row["selectable"]),
        "reason_code": row["reason_code"],
    }


def _encode_cursor(offset):
    blob = json.dumps({"o": int(offset)}, separators=(",", ":"))
    return "bc_" + base64.urlsafe_b64encode(blob.encode()).decode().rstrip("=")


def _decode_cursor(cursor):
    if cursor in (None, ""):
        return 0
    try:
        raw = str(cursor)
        if not raw.startswith("bc_"):
            raise ValueError
        pad = "=" * (-len(raw) % 4 if (len(raw) - 3) % 4 else 0)
        blob = base64.urlsafe_b64decode(raw[3:] + pad)
        out = json.loads(blob.decode("utf-8"))
        off = int(out["o"])
        if off < 0:
            raise ValueError
        return off
    except Exception:
        raise ValidationError("游标非法", code="invalid_cursor") from None


def list_candidates(enumeration_id, owner_user_id, cursor=None, limit=None):
    """候选分页（cursor 不透明；limit 1-100 默认 50）。"""
    offset = _decode_cursor(cursor)
    limit = DEFAULT_LIMIT if limit is None else int(limit)
    limit = max(1, min(MAX_LIMIT, limit))
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                _expire_if_due(cur, enumeration_id)
                _get_enum_row(cur, enumeration_id, owner_user_id)
                cur.execute(
                    "SELECT * FROM baidu_candidates "
                    "WHERE enumeration_id=%s "
                    "ORDER BY relative_path, id LIMIT %s OFFSET %s",
                    (enumeration_id, limit + 1, offset))
                rows = cur.fetchall()
    finally:
        conn.close()
    has_more = len(rows) > limit
    rows = rows[:limit]
    return {
        "items": [candidate_public_view(r) for r in rows],
        "next_cursor": _encode_cursor(offset + limit) if has_more else None,
    }


# --------------------------------------------------------------------------- #
# 枚举 worker：领取 / 心跳 / 递归遍历
# --------------------------------------------------------------------------- #

def claim_enumeration(worker_id="worker", lease_seconds=ENUMERATION_LEASE_SECONDS):
    """领取一条 queued 或租约过期的枚举（FOR UPDATE SKIP LOCKED）。"""
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                cur.execute(
                    "SELECT * FROM baidu_enumerations "
                    "WHERE state IN ('queued', 'enumerating') "
                    "AND (lease_expires_at IS NULL OR lease_expires_at < now()) "
                    "ORDER BY created_at FOR UPDATE SKIP LOCKED LIMIT 1")
                row = cur.fetchone()
                if row is None:
                    return None
                token = secrets.token_hex(8)
                cur.execute(
                    "UPDATE baidu_enumerations SET state='enumerating', "
                    "lease_owner=%s, lease_token=%s, lease_expires_at="
                    "now() + (%s || ' seconds')::interval, updated_at=now() "
                    "WHERE id=%s RETURNING *",
                    (worker_id, token, str(int(lease_seconds)), row["id"]))
                return cur.fetchone()
    finally:
        conn.close()


def heartbeat_enumeration(enumeration_id, worker_id="worker",
                          lease_seconds=ENUMERATION_LEASE_SECONDS):
    """枚举租约续期（活性由 lease_expires_at 表达；匹配 lease_owner）。"""
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                cur.execute(
                    "UPDATE baidu_enumerations SET lease_expires_at="
                    "now() + (%s || ' seconds')::interval, updated_at=now() "
                    "WHERE id=%s AND lease_owner=%s AND state='enumerating'",
                    (str(int(lease_seconds)), enumeration_id, worker_id))
                return cur.rowcount == 1
    finally:
        conn.close()


def _finalize_enumeration(enumeration_id, *, state, complete, scanned,
                          candidates, error_code=None, incomplete=None,
                          counters=None):
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                sets = ["state=%s", "complete=%s", "scanned_count=%s",
                        "candidate_count=%s", "error_code=%s",
                        "incomplete_reason=%s", "updated_at=now()",
                        "lease_owner=NULL", "lease_token=NULL",
                        "lease_expires_at=NULL"]
                args = [state, complete, scanned, candidates,
                        error_code, incomplete]
                if counters:
                    sets += ["transfer_calls=%s", "download_calls=%s",
                             "delete_calls=%s"]
                    args += [counters["transfer"], counters["download"],
                             counters["delete"]]
                args.append(enumeration_id)
                cur.execute(
                    "UPDATE baidu_enumerations SET "
                    + ", ".join(sets) + " WHERE id=%s", tuple(args))
    finally:
        conn.close()


def _format_suffix(name):
    """最长后缀匹配（.ome.tif/.ome.tiff 先于最后一点），小写。"""
    base = str(name).replace("\\", "/").rsplit("/", 1)[-1].lower()
    for suffix in (".ome.tiff", ".ome.tif"):
        if base.endswith(suffix):
            return suffix
    if "." in base.lstrip("."):
        return "." + base.rsplit(".", 1)[-1]
    return ""


def _classify_files(files, dirs):
    """按注册表 + W5 规则分类文件条目 → candidate 行字段列表。"""
    out = []
    for f in files:
        rel = f["relative_path"]
        name = f["name"]
        suffix = _format_suffix(name)
        if suffix in (".json",):
            out.append((f, suffix, "", False, "not_a_slide"))
            continue
        info = slide_format_registry.lookup(name)
        cap = info["capability"]
        if cap == slide_format_registry.CAP_NATIVE_BUNDLE:
            # MRXS：本地完整包入口保留；百度侧无法保证整目录包时明确
            # “当前不支持从百度导入该 bundle”，不假装支持
            parent = rel.rsplit("/", 1)[0] if "/" in rel else ""
            stem = name[:-len(suffix)] if suffix else name
            sibling = (parent + "/" + stem) if parent else stem
            reason = ("baidu_bundle_unsupported" if sibling in dirs
                      else "bundle_incomplete")
            out.append((f, suffix, cap, False, reason))
        elif cap in (slide_format_registry.CAP_NATIVE_SINGLE_FILE,
                     slide_format_registry.CAP_CONVERT_REQUIRED):
            # native 单文件 + 已实现转换（KFB/KFBF）可选
            out.append((f, suffix, cap, True, None))
        else:
            out.append((f, suffix, cap, False, "unsupported_format"))
    return out


def _insert_candidates(cur, enumeration_id, files, dirs):
    """文件 + 目录候选落库（fs_id 去重；目录/不可选保留原因）。"""
    rows = []
    seen_fsids = set()
    for f in files:
        if f["fs_id"] in seen_fsids:
            continue
        seen_fsids.add(f["fs_id"])
        rows.append(f)
    classified = _classify_files(rows, dirs)
    count = 0
    for f, suffix, cap, selectable, reason in classified:
        cur.execute(
            "INSERT INTO baidu_candidates "
            "(id, enumeration_id, fs_id, name, relative_path, size_bytes, "
            " format, capability, selectable, reason_code) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
            "ON CONFLICT (enumeration_id, fs_id) DO NOTHING",
            (_new_id("bcand"), enumeration_id, f["fs_id"], f["name"],
             f["relative_path"], int(f["size"]), suffix, cap, selectable,
             reason))
        count += 1
    for d in sorted(dirs):
        cur.execute(
            "INSERT INTO baidu_candidates "
            "(id, enumeration_id, fs_id, name, relative_path, size_bytes, "
            " format, capability, selectable, reason_code) "
            "VALUES (%s,%s,%s,%s,%s,%s,'','',FALSE,'not_a_slide') "
            "ON CONFLICT (enumeration_id, fs_id) DO NOTHING",
            (_new_id("bcand"), enumeration_id, "dir:" + d,
             d.rsplit("/", 1)[-1], d, 0))
        count += 1
    return count


def run_one_enumeration(claim_row, adapter):
    """执行已领取的枚举：分享内递归分页遍历 → 分类落库 → 终态。

    失败模式（全部 state=failed、complete=false，不冒充完整/空分享）：
    重复游标 ``cursor_loop``、超限 ``incomplete_limit``、超时
    ``enumeration_timeout``、适配器错误透传其 code、枚举期间出现
    transfer/download/delete 副作用 ``enumeration_side_effect``。
    """
    enum_id = claim_row["id"]
    share_url = decrypt_text(claim_row["share_url_enc"])
    code = decrypt_text(claim_row.get("extraction_enc"))
    max_depth = int(claim_row["max_depth"] or DEFAULT_MAX_DEPTH)
    max_entries = int(claim_row["max_entries"] or DEFAULT_MAX_ENTRIES)
    deadline = time.monotonic() + ENUMERATION_TIMEOUT_SECONDS

    before = adapter.counters()
    files, dirs = [], set()
    scanned = 0
    error_code = None
    incomplete = None
    queue = [("", 0)]
    visited = set()  # (dir_path, cursor)

    while queue and error_code is None:
        dir_path, depth = queue.pop(0)
        cursor = None
        while True:
            if time.monotonic() > deadline:
                error_code, incomplete = "enumeration_timeout", "timeout"
                break
            key = (dir_path, cursor or "1")
            if key in visited:
                error_code, incomplete = "cursor_loop", "duplicate_cursor"
                break
            visited.add(key)
            try:
                resp = adapter.list_share_page(
                    share_url, code, dir_path, cursor, PAGE_SIZE)
            except AdapterError as exc:
                error_code = exc.code
                break
            for item in resp.get("items", []):
                scanned += 1
                if scanned > max_entries:
                    error_code = "incomplete_limit"
                    incomplete = "max_entries"
                    break
                rel = item["relative_path"]
                if item["is_dir"]:
                    dirs.add(rel)
                    if depth + 1 > max_depth:
                        error_code = "incomplete_limit"
                        incomplete = "max_depth"
                        break
                    queue.append((rel, depth + 1))
                else:
                    files.append(item)
            if error_code is not None:
                break
            if resp.get("has_more") and resp.get("next_cursor"):
                cursor = resp["next_cursor"]
                continue
            break

    after = adapter.counters()
    counters = {
        "transfer": after.get("transfer", 0) - before.get("transfer", 0),
        "download": after.get("download", 0) - before.get("download", 0),
        "delete": after.get("delete", 0) - before.get("delete", 0),
    }
    if error_code is None and any(counters.values()):
        # 枚举阶段出现外部副作用 → 明确失败（spec：转存/下载/删除恒 0）
        error_code = "enumeration_side_effect"
        incomplete = "side_effect_during_enumeration"

    if error_code is None:
        conn = _connect()
        try:
            with pg_store.transaction(conn) as c:
                with c.cursor() as cur:
                    count = _insert_candidates(cur, enum_id, files, dirs)
        finally:
            conn.close()
        _finalize_enumeration(
            enum_id, state="ready", complete=True, scanned=scanned,
            candidates=count, counters=counters)
        return {"id": enum_id, "state": "ready", "complete": True,
                "scanned": scanned, "candidates": count}

    _finalize_enumeration(
        enum_id, state="failed", complete=False, scanned=scanned,
        candidates=0, error_code=error_code, incomplete=incomplete,
        counters=counters)
    return {"id": enum_id, "state": "failed", "complete": False,
            "error_code": error_code, "scanned": scanned}


def run_enumeration(enumeration_id, adapter, worker_id="worker",
                    lease_seconds=ENUMERATION_LEASE_SECONDS):
    """按 id 领取并执行一次枚举（不可领取返回 None）。"""
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                token = secrets.token_hex(8)
                cur.execute(
                    "UPDATE baidu_enumerations SET state='enumerating', "
                    "lease_owner=%s, lease_token=%s, lease_expires_at="
                    "now() + (%s || ' seconds')::interval, updated_at=now() "
                    "WHERE id=%s AND (state='queued' OR "
                    "(state='enumerating' AND (lease_expires_at IS NULL OR "
                    "lease_expires_at < now()))) RETURNING *",
                    (worker_id, token, str(int(lease_seconds)),
                     enumeration_id))
                row = cur.fetchone()
    finally:
        conn.close()
    if row is None:
        return None
    return run_one_enumeration(row, adapter)


# --------------------------------------------------------------------------- #
# 导入批次：创建 / 查询 / 取消 / 重试
# --------------------------------------------------------------------------- #

def _batch_public_view(row, item_counts=None):
    return {
        "id": row["id"],
        "state": row["state"],
        "enumeration_id": row["enumeration_id"],
        "target_project_id": row["target_project_id"],
        "total_bytes": _dec_str(row["total_bytes"]),
        "cleanup_state": row["cleanup_state"],
        "cancel_requested": bool(row["cancel_requested"]),
        "error_code": row["error_code"],
        "created_at": _iso(row["created_at"]),
        "updated_at": _iso(row["updated_at"]),
        "item_counts": item_counts,
    }


def _payload_digest(enumeration_id, candidate_ids, target_project_id):
    blob = json.dumps(
        {"enumeration_id": enumeration_id,
         "candidate_ids": sorted(candidate_ids),
         "target_project_id": target_project_id or None},
        sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _load_batch(cur, batch_id, owner_user_id):
    cur.execute("SELECT * FROM baidu_import_batches WHERE id=%s", (batch_id,))
    row = cur.fetchone()
    if row is None:
        raise NotFoundError("批次不存在")
    if owner_user_id is not None and row["owner_user_id"] != owner_user_id:
        raise NotFoundError("批次不存在")
    return row


def create_import(owner_user_id, enumeration_id, candidate_ids,
                  target_project_id=None, idempotency_key=None,
                  quota_hook=None):
    """创建导入批次（202 queued；不触发转存——worker 才做）。

    校验：枚举 owner/state=ready/complete/未过期；候选属于该枚举且可选；
    空选择 400。幂等：同 owner+key 同 digest 重放返回原批次；异 digest 409。
    配额：``quota_hook(user_id, nbytes) -> reservation_id``（不足抛
    upload_guard.QuotaExceeded → QuotaError 429），失败零外部副作用。
    """
    if not owner_user_id:
        raise PermissionDeniedError("缺少 owner")
    if not isinstance(enumeration_id, str) or not enumeration_id:
        raise ValidationError("enumeration_id 缺失")
    if not isinstance(candidate_ids, list) or not candidate_ids \
            or not all(isinstance(x, str) for x in candidate_ids):
        raise ValidationError("candidate_ids 不能为空", code="empty_selection")
    # 去重保序
    candidate_ids = list(dict.fromkeys(candidate_ids))

    conn = _connect()
    extra_reservation = None
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                _expire_if_due(cur, enumeration_id)
                enum_row = _get_enum_row(cur, enumeration_id, owner_user_id)
                if enum_row["state"] != "ready":
                    raise ConflictError(
                        "枚举状态不可导入（%s）" % enum_row["state"],
                        code="enumeration_%s" % enum_row["state"]
                        if enum_row["state"] == "expired"
                        else "enumeration_not_ready")
                if not enum_row["complete"]:
                    raise ConflictError(
                        "枚举不完整（%s），不能导入"
                        % (enum_row["error_code"] or "incomplete"),
                        code="enumeration_incomplete")
                cur.execute(
                    "SELECT * FROM baidu_candidates WHERE enumeration_id=%s "
                    "AND id = ANY(%s)", (enumeration_id, candidate_ids))
                found = {r["id"]: r for r in cur.fetchall()}
                missing = [cid for cid in candidate_ids if cid not in found]
                if missing:
                    raise ValidationError(
                        "候选不存在或不属于该枚举", code="invalid_candidate")
                not_selectable = [cid for cid in candidate_ids
                                  if not found[cid]["selectable"]]
                if not_selectable:
                    raise ValidationError(
                        "存在不可选候选", code="candidate_not_selectable")
                total_bytes = sum(int(found[cid]["size_bytes"])
                                  for cid in candidate_ids)
                digest = _payload_digest(enumeration_id, candidate_ids,
                                         target_project_id)
                key = idempotency_key or ("auto_" + secrets.token_hex(16))

                # 幂等重放检查（同 owner+key）
                cur.execute(
                    "SELECT * FROM baidu_import_batches "
                    "WHERE owner_user_id=%s AND idempotency_key=%s",
                    (owner_user_id, key))
                existing = cur.fetchone()
                if existing is not None:
                    if existing["payload_sha256"] == digest:
                        return _batch_view_with_counts(cur, existing)
                    raise ConflictError(
                        "幂等键已用于不同载荷", code="idempotency_conflict")

                # 配额预占（同事务内尽量靠近插入；不足则整体失败零副作用）
                if quota_hook is not None and total_bytes > 0:
                    reservation_id = quota_hook(owner_user_id, total_bytes)
                    extra_reservation = reservation_id
                else:
                    reservation_id = None

                batch_id = _new_id("bib")
                cur.execute(
                    "INSERT INTO baidu_import_batches "
                    "(id, owner_user_id, enumeration_id, idempotency_key, "
                    " payload_sha256, target_project_id, state, "
                    " quota_reservation_id, total_bytes) "
                    "VALUES (%s,%s,%s,%s,%s,%s,'queued',%s,%s) "
                    "ON CONFLICT (owner_user_id, idempotency_key) "
                    "DO NOTHING RETURNING *",
                    (batch_id, owner_user_id, enumeration_id, key, digest,
                     target_project_id, reservation_id, total_bytes))
                row = cur.fetchone()
                if row is None:
                    # 并发同键：重读裁决（同 digest 原批次 / 异 digest 409）
                    cur.execute(
                        "SELECT * FROM baidu_import_batches "
                        "WHERE owner_user_id=%s AND idempotency_key=%s",
                        (owner_user_id, key))
                    winner = cur.fetchone()
                    if winner is not None \
                            and winner["payload_sha256"] == digest:
                        if extra_reservation:
                            _safe_release(extra_reservation)
                            extra_reservation = None
                        return _batch_view_with_counts(cur, winner)
                    raise ConflictError(
                        "幂等键已用于不同载荷", code="idempotency_conflict")
                for cid in candidate_ids:
                    cand = found[cid]
                    cur.execute(
                        "INSERT INTO baidu_import_items "
                        "(id, batch_id, candidate_id, fs_id, name, "
                        " relative_path, stage, source_size, cleanup_state) "
                        "VALUES (%s,%s,%s,%s,%s,%s,'queued',%s,'not_needed')",
                        (_new_id("bitem"), batch_id, cid, cand["fs_id"],
                         cand["name"], cand["relative_path"],
                         int(cand["size_bytes"])))
                out = _batch_view_with_counts(cur, row)
                extra_reservation = None  # 已归属批次，不再释放
                return out
    except upload_guard.QuotaExceeded as exc:
        raise QuotaError(
            "存储配额不足：%s" % exc.code, code="quota_exceeded") from exc
    except upload_guard.InflightLimitExceeded as exc:
        raise QuotaError("在途任务过多", code="inflight_limit") from exc
    except upload_guard.RateLimitExceeded as exc:
        raise QuotaError("请求频率超限", code="rate_limited") from exc
    finally:
        if extra_reservation:
            _safe_release(extra_reservation)
        conn.close()


def _safe_release(reservation_id):
    try:
        _release_reservation(reservation_id)
    except Exception:
        pass


def _batch_view_with_counts(cur, row):
    cur.execute(
        "SELECT stage, COUNT(*)::int AS n FROM baidu_import_items "
        "WHERE batch_id=%s GROUP BY stage", (row["id"],))
    counts = {r["stage"]: int(r["n"]) for r in cur.fetchall()}
    return _batch_public_view(row, item_counts=counts)


def get_import(batch_id, owner_user_id):
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                row = _load_batch(cur, batch_id, owner_user_id)
                view = _batch_view_with_counts(cur, row)
                cur.execute(
                    "SELECT * FROM baidu_import_items WHERE batch_id=%s "
                    "ORDER BY created_at, id", (batch_id,))
                view["items"] = [
                    {"id": r["id"], "fs_id": r["fs_id"], "name": r["name"],
                     "relative_path": r["relative_path"], "stage": r["stage"],
                     "error_code": r["error_code"],
                     "cleanup_state": r["cleanup_state"],
                     "source_size": _dec_str(r["source_size"]),
                     "attempt": int(r["attempt"])}
                    for r in cur.fetchall()]
        return view
    finally:
        conn.close()


def list_imports(owner_user_id, cursor=None, limit=None, group=None):
    offset = _decode_cursor(cursor)
    limit = DEFAULT_LIMIT if limit is None else int(limit)
    limit = max(1, min(MAX_LIMIT, limit))
    where = "owner_user_id=%s"
    args = [owner_user_id]
    if group == "open":
        where += " AND state IN ('queued','running')"
    elif group == "recent":
        where += " AND created_at > now() - interval '7 days'"
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                cur.execute(
                    "SELECT * FROM baidu_import_batches WHERE " + where +
                    " ORDER BY created_at DESC, id LIMIT %s OFFSET %s",
                    args + [limit + 1, offset])
                rows = cur.fetchall()
                views = []
                for row in rows[:limit]:
                    views.append(_batch_view_with_counts(cur, row))
    finally:
        conn.close()
    has_more = len(rows) > limit
    return {
        "items": views,
        "next_cursor": _encode_cursor(offset + limit) if has_more else None,
    }


def request_cancel(batch_id, owner_user_id):
    """取消请求（202）；已终结批次原样返回，不删除成功产物。"""
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                row = _load_batch(cur, batch_id, owner_user_id)
                if row["state"] in ("succeeded", "partial_failed", "failed",
                                    "cancelled"):
                    return _batch_view_with_counts(cur, row)
                cur.execute(
                    "UPDATE baidu_import_batches SET cancel_requested=TRUE, "
                    "updated_at=now() WHERE id=%s RETURNING *", (batch_id,))
                return _batch_view_with_counts(cur, cur.fetchone())
    finally:
        conn.close()


def retry_items(batch_id, owner_user_id, item_ids, idempotency_key=None):
    """重试失败条目：仅 failed 且可恢复；成功项不重跑。"""
    if not isinstance(item_ids, list) or not item_ids \
            or not all(isinstance(x, str) for x in item_ids):
        raise ValidationError("item_ids 不能为空", code="empty_selection")
    item_ids = list(dict.fromkeys(item_ids))
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                row = _load_batch(cur, batch_id, owner_user_id)
                if row["state"] in ("queued", "running"):
                    raise ConflictError("批次仍在进行", code="batch_running")
                cur.execute(
                    "SELECT * FROM baidu_import_items WHERE batch_id=%s "
                    "AND id = ANY(%s)", (batch_id, item_ids))
                found = {r["id"]: r for r in cur.fetchall()}
                missing = [i for i in item_ids if i not in found]
                if missing:
                    raise ValidationError("条目不存在",
                                          code="invalid_item")
                bad = []
                for iid in item_ids:
                    it = found[iid]
                    if it["stage"] != "failed":
                        bad.append((iid, "stage_%s" % it["stage"]))
                    elif it["error_code"] in NON_RETRYABLE_ERROR_CODES:
                        bad.append((iid, "not_retryable"))
                if bad:
                    raise ConflictError(
                        "存在不可重试条目", code="item_not_retryable")
                for iid in item_ids:
                    cur.execute(
                        "UPDATE baidu_import_items SET stage='queued', "
                        "error_code=NULL, cleanup_state='not_needed', "
                        "updated_at=now() WHERE id=%s", (iid,))
                cur.execute(
                    "UPDATE baidu_import_batches SET state='queued', "
                    "cancel_requested=FALSE, error_code=NULL, "
                    "updated_at=now() WHERE id=%s RETURNING *", (batch_id,))
                return _batch_view_with_counts(cur, cur.fetchone())
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# 批次 worker：领取 / 逐条目流水线 / 崩溃恢复对账 / 清理
# --------------------------------------------------------------------------- #

def claim_batch(worker_id="worker", lease_seconds=BATCH_LEASE_SECONDS):
    """领取一条 queued 或租约过期的 running 批次（含条目）。"""
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                cur.execute(
                    "SELECT * FROM baidu_import_batches "
                    "WHERE state IN ('queued', 'running') "
                    "AND (lease_expires_at IS NULL OR lease_expires_at < now()) "
                    "ORDER BY created_at FOR UPDATE SKIP LOCKED LIMIT 1")
                row = cur.fetchone()
                if row is None:
                    return None
                token = secrets.token_hex(8)
                cur.execute(
                    "UPDATE baidu_import_batches SET state='running', "
                    "lease_owner=%s, lease_token=%s, lease_expires_at="
                    "now() + (%s || ' seconds')::interval, updated_at=now() "
                    "WHERE id=%s RETURNING *",
                    (worker_id, token, str(int(lease_seconds)), row["id"]))
                batch = cur.fetchone()
                cur.execute(
                    "SELECT * FROM baidu_import_items WHERE batch_id=%s "
                    "ORDER BY created_at, id", (batch["id"],))
                items = cur.fetchall()
                return {"batch": batch, "items": items}
    finally:
        conn.close()


def _update_item(item_id, fields):
    """短事务更新条目（fields：列名→值 dict）。返回新行。"""
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                sets, args = ["updated_at=now()"], []
                for k, v in fields.items():
                    sets.append("%s=%%s" % k)
                    args.append(v)
                args.append(item_id)
                cur.execute(
                    "UPDATE baidu_import_items SET " + ", ".join(sets) +
                    " WHERE id=%s RETURNING *", tuple(args))
                return cur.fetchone()
    finally:
        conn.close()


def _get_item(item_id):
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                cur.execute(
                    "SELECT * FROM baidu_import_items WHERE id=%s",
                    (item_id,))
                return cur.fetchone()
    finally:
        conn.close()


def _fail_item(item_id, error_code):
    _update_item(item_id, {"stage": "failed", "error_code": error_code})


def _phase_transfer(adapter, batch, item, hooks):
    """转存阶段（含崩溃对账：task_id poll → 副本对账 → 才考虑重转存）。"""
    stage = item["stage"]
    if stage in ("queued", "transferring"):
        need = True
        if stage == "transferring" and item["transfer_task_id"]:
            poll = adapter.poll_transfer(item["transfer_task_id"])
            if poll.get("state") == "succeeded":
                need = False
        if need:
            copies = {c["name"]: c for c in
                      adapter.list_batch_copies(batch["id"])}
            c = copies.get(item["name"])
            if c is not None and int(c["size"]) == int(item["source_size"]):
                need = False  # 本批副本已存在（大小一致）→ 不重转存
        if need:
            if stage == "queued":
                item = _update_item(
                    item["id"],
                    {"stage": "transferring",
                     "attempt": int(item["attempt"]) + 1})
            try:
                resp = adapter.transfer_selected(
                    batch["id"], batch["_share_url"],
                    batch.get("_extraction_code"), [item["fs_id"]])
            except AdapterError as exc:
                _fail_item(item["id"], exc.code)
                return _get_item(item["id"])
            item = _update_item(item["id"],
                                {"transfer_task_id": resp["task_id"]})
            poll = adapter.poll_transfer(resp["task_id"])
            if poll.get("state") == "failed":
                _fail_item(item["id"], "transfer_failed")
                return _get_item(item["id"])
        hook = hooks.get("on_transfer_persisted")
        if hook:
            hook(_get_item(item["id"]))  # 崩溃注入点 A
    return _get_item(item["id"])


def _phase_download(adapter, batch, item, staging_root, hooks):
    """下载阶段（sha 已记录且文件在盘 → 不重下载）。"""
    stage = item["stage"]
    if stage in ("queued", "transferring", "downloading"):
        skip = False
        sp, sha = item["staging_path"], item["source_sha256"]
        if sha and sp and Path(sp).is_file() \
                and Path(sp).stat().st_size == int(item["source_size"]):
            skip = True
        if not skip:
            if stage != "downloading":
                item = _update_item(item["id"], {"stage": "downloading"})
            staging_dir = Path(staging_root) / batch["id"]
            try:
                adapter.download_to(
                    "%s/%s" % (batch["id"], item["name"]), staging_dir)
            except AdapterError as exc:
                _fail_item(item["id"], exc.code)
                return _get_item(item["id"])
            spath = staging_dir / item["name"]
            if not spath.is_file():
                _fail_item(item["id"], "download_output_missing")
                return _get_item(item["id"])
            digest = _sha256_file(spath)
            if spath.stat().st_size != int(item["source_size"]):
                _fail_item(item["id"], "size_mismatch")
                return _get_item(item["id"])
            item = _update_item(
                item["id"], {"stage": "validating",
                             "staging_path": str(spath),
                             "source_sha256": digest})
        hook = hooks.get("on_downloaded")
        if hook:
            hook(_get_item(item["id"]))  # 崩溃注入点 B
    return _get_item(item["id"])


def _phase_convert_placeholder(adapter, batch, item):
    """转换阶段占位：真实转换在 app 集成层经 hooks 接线（B06 范围）；
    此处保留阶段转移与 conversion_job_id 字段。"""
    if item["stage"] == "validating":
        info = slide_format_registry.lookup(item["name"])
        new_stage = ("converting"
                     if info["capability"] ==
                     slide_format_registry.CAP_CONVERT_REQUIRED
                     else "ingesting")
        item = _update_item(item["id"], {"stage": new_stage})
    if item["stage"] == "converting":
        item = _update_item(item["id"], {"stage": "ingesting"})
    return item


def _phase_ingest(adapter, batch, item, hooks):
    """入库阶段：ingest_token 为幂等凭证，崩溃重跑不重复入库。"""
    if item["stage"] == "ingesting" and not item["ingest_token"]:
        token = "bing_" + secrets.token_hex(12)
        item = _update_item(item["id"],
                            {"stage": "ready", "ingest_token": token})
    hook = hooks.get("on_ingested")
    if hook:
        hook(_get_item(item["id"]))  # 崩溃注入点 C
    return _get_item(item["id"])


def _sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


_TERMINAL_BATCH_STATES = ("succeeded", "partial_failed", "failed",
                          "cancelled")


def run_batch(batch_id, adapter, *, staging_root=None, worker_id="worker",
              hooks=None, lease_seconds=BATCH_LEASE_SECONDS):
    """按 id 领取并推进一个批次到终态（不可领取返回 None）。

    ``hooks``：``on_transfer_persisted`` / ``on_downloaded`` /
    ``on_ingested``（崩溃注入点，异常向上传播 = 模拟进程崩溃；阶段与
    对账凭证已先落库）。取消：queued 条目停止；无 ready 产物时释放
    未消费预占。清理：仅本批 ready 项副本；失败置 cleanup_state=failed，
    不回滚 ready。
    """
    claim = None
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                token = secrets.token_hex(8)
                cur.execute(
                    "UPDATE baidu_import_batches SET state='running', "
                    "lease_owner=%s, lease_token=%s, lease_expires_at="
                    "now() + (%s || ' seconds')::interval, updated_at=now() "
                    "WHERE id=%s AND (state='queued' OR "
                    "(state='running' AND (lease_expires_at IS NULL OR "
                    "lease_expires_at < now()))) RETURNING *",
                    (worker_id, token, str(int(lease_seconds)), batch_id))
                row = cur.fetchone()
                if row is None:
                    return None
                # 枚举行解密为瞬态字段（绝不回写/出线）
                cur.execute(
                    "SELECT share_url_enc, extraction_enc "
                    "FROM baidu_enumerations WHERE id=%s",
                    (row["enumeration_id"],))
                enc = cur.fetchone()
                row = dict(row)
                row["_share_url"] = decrypt_text(enc["share_url_enc"])
                row["_extraction_code"] = decrypt_text(
                    enc["extraction_enc"])
                cur.execute(
                    "SELECT * FROM baidu_import_items WHERE batch_id=%s "
                    "ORDER BY created_at, id", (batch_id,))
                claim = {"batch": row, "items": cur.fetchall()}
    finally:
        conn.close()

    batch, items = claim["batch"], claim["items"]
    hooks = hooks or {}
    staging_root = Path(staging_root or STAGING_ROOT)

    if batch["cancel_requested"]:
        _apply_cancel(batch)
        return get_import(batch_id, batch["owner_user_id"])

    for item in items:
        if item["stage"] in ("ready", "failed", "cancelled"):
            continue
        item = _phase_transfer(adapter, batch, item, hooks)
        if item["stage"] == "failed":
            continue
        item = _phase_download(adapter, batch, item, staging_root, hooks)
        if item["stage"] == "failed":
            continue
        item = _phase_convert_placeholder(adapter, batch, item)
        item = _phase_ingest(adapter, batch, item, hooks)

    _finalize_batch(batch, adapter, worker_id)
    return get_import(batch_id, batch["owner_user_id"])


def _apply_cancel(batch):
    """取消收口：停止未开始条目；有 ready 产物时批次落 partial_failed
    （成功产物不冒充失败也不删除）；无 ready 产物释放未消费预占。"""
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                cur.execute(
                    "SELECT stage FROM baidu_import_items "
                    "WHERE batch_id=%s AND stage='ready'", (batch["id"],))
                has_ready = cur.fetchone() is not None
                cur.execute(
                    "SELECT stage FROM baidu_import_items "
                    "WHERE batch_id=%s AND stage IN ('failed')",
                    (batch["id"],))
                has_failed = cur.fetchone() is not None
                cur.execute(
                    "UPDATE baidu_import_items SET stage='cancelled', "
                    "updated_at=now() WHERE batch_id=%s "
                    "AND stage NOT IN ('ready','failed','cancelled')",
                    (batch["id"],))
                state = ("partial_failed" if has_ready
                         else ("failed" if has_failed else "cancelled"))
                cur.execute(
                    "UPDATE baidu_import_batches SET state=%s, "
                    "lease_owner=NULL, lease_token=NULL, "
                    "lease_expires_at=NULL, updated_at=now() "
                    "WHERE id=%s RETURNING *", (state, batch["id"]))
                reservation_id = batch["quota_reservation_id"]
                cur.execute(
                    "SELECT COALESCE(SUM(source_size),0)::bigint AS bytes "
                    "FROM baidu_import_items WHERE batch_id=%s "
                    "AND stage='ready'", (batch["id"],))
                ready_bytes = int(cur.fetchone()["bytes"])
    finally:
        conn.close()
    if reservation_id:
        if has_ready and ready_bytes > 0:
            # 已有 ready 产物：按实际字节幂等收口（consume 幂等，重跑不双扣）
            try:
                _consume_reservation(reservation_id, ready_bytes)
            except Exception:
                pass
        elif not has_ready:
            # 仅有未消费预占（无任何 ready 产物）→ 释放
            _safe_release(reservation_id)


def _finalize_batch(batch, adapter, worker_id):
    """聚合条目终态 → 批次终态；配额一次收口；本批副本清理。"""
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                cur.execute(
                    "SELECT stage, COUNT(*)::int AS n, "
                    "COALESCE(SUM(source_size),0)::bigint AS bytes "
                    "FROM baidu_import_items WHERE batch_id=%s "
                    "GROUP BY stage", (batch["id"],))
                stats = {r["stage"]: (int(r["n"]), int(r["bytes"]))
                         for r in cur.fetchall()}
                ready_n, ready_bytes = stats.get("ready", (0, 0))
                failed_n = stats.get("failed", (0, 0))[0]
                cancelled_n = stats.get("cancelled", (0, 0))[0]
                pending = sum(n for s, (n, _) in stats.items()
                              if s not in ("ready", "failed", "cancelled"))
                if pending:
                    return  # 尚有条目在途（本轮未推进完），不改批次态
                if ready_n and (failed_n or cancelled_n):
                    state = "partial_failed"
                elif ready_n:
                    state = "succeeded"
                elif failed_n:
                    state = "failed"
                else:
                    state = "cancelled"
                cur.execute(
                    "UPDATE baidu_import_batches SET state=%s, "
                    "lease_owner=NULL, lease_token=NULL, "
                    "lease_expires_at=NULL, updated_at=now() WHERE id=%s",
                    (state, batch["id"]))
                reservation_id = batch["quota_reservation_id"]
                consumed_bytes = ready_bytes
    finally:
        conn.close()
    # 配额收口：ready 产物字节数一次 consume（consume_reservation 幂等，
    # 崩溃重跑不双扣；无 ready（全失败/取消）→ 释放未消费预占
    if reservation_id:
        if consumed_bytes > 0:
            try:
                _consume_reservation(reservation_id, consumed_bytes)
            except Exception:
                pass  # 预占过期等：不阻塞批次终态，审计由配额表自身记录
        else:
            _safe_release(reservation_id)
    _cleanup_copies(batch, adapter, state)


def _cleanup_copies(batch, adapter, batch_state):
    """仅清理本批 ready 条目持有的远端副本；失败不回滚 ready。"""
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                cur.execute(
                    "SELECT name FROM baidu_import_items "
                    "WHERE batch_id=%s AND stage='ready'", (batch["id"],))
                ready_names = [r["name"] for r in cur.fetchall()]
                if not ready_names:
                    if batch_state in ("succeeded", "partial_failed"):
                        cur.execute(
                            "UPDATE baidu_import_batches "
                            "SET cleanup_state='not_needed' WHERE id=%s",
                            (batch["id"],))
                    return
                cur.execute(
                    "UPDATE baidu_import_batches SET cleanup_state='pending', "
                    "updated_at=now() WHERE id=%s", (batch["id"],))
                cur.execute(
                    "UPDATE baidu_import_items SET cleanup_state='pending' "
                    "WHERE batch_id=%s AND stage='ready'", (batch["id"],))
    finally:
        conn.close()
    try:
        copies = adapter.list_batch_copies(batch["id"])
    except AdapterError:
        _set_cleanup_state(batch["id"], "failed")
        return
    present = {c["name"] for c in copies}
    allowed = ["%s/%s" % (batch["id"], n) for n in ready_names
               if n in present]
    if not allowed:
        _set_cleanup_state(batch["id"], "succeeded")  # 无本批副本可清理
        return
    try:
        adapter.cleanup_batch_copies(batch["id"], allowed)
        _set_cleanup_state(batch["id"], "succeeded")
    except AdapterError:
        # 清理失败可重试；绝不回滚 ready / 批次终态
        _set_cleanup_state(batch["id"], "failed")


def _set_cleanup_state(batch_id, state):
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                cur.execute(
                    "UPDATE baidu_import_batches SET cleanup_state=%s, "
                    "updated_at=now() WHERE id=%s", (state, batch_id))
                if state == "succeeded":
                    cur.execute(
                        "UPDATE baidu_import_items "
                        "SET cleanup_state='succeeded' "
                        "WHERE batch_id=%s AND stage='ready'", (batch_id,))
                elif state == "failed":
                    cur.execute(
                        "UPDATE baidu_import_items SET cleanup_state='failed' "
                        "WHERE batch_id=%s AND stage='ready'", (batch_id,))
    finally:
        conn.close()


def retry_cleanup(batch_id, adapter=None):
    """清理失败后的显式重试（spec §6.3：清理失败可重试，不动 ready）。"""
    adapter = adapter or get_adapter()
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                cur.execute(
                    "SELECT * FROM baidu_import_batches WHERE id=%s",
                    (batch_id,))
                batch = cur.fetchone()
                if batch is None:
                    raise NotFoundError("批次不存在")
                if batch["cleanup_state"] != "failed":
                    return get_import(batch_id, batch["owner_user_id"])
    finally:
        conn.close()
    _cleanup_copies(dict(batch), adapter, batch["state"])
    return get_import(batch_id, batch["owner_user_id"])
