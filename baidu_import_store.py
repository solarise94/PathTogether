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

入库走 :mod:`baidu_ingest`：native 校验后写入 ``UPLOAD_DIR``，KFB/KFBF
同步领取 conversion job 并 ``process_job``；``ingest_token`` 防崩溃重入。
批次级配额预占在全部条目终结后一次 ``consume``（幂等，崩溃重跑不双扣）。
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import tempfile
import threading
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
    {"share_invalid", "source_changed", "not_retryable", "duplicate_filename"})

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


class LeaseLost(BaiduImportError):
    """批次租约已被其他 worker 重领（fence 校验失败）。

    内部异常：``run_claimed_batch`` 捕获后**安静放弃**（不推进剩余条目、
    不 finalize、不清理副本、不覆盖新 owner 状态），绝不向
    ``run_batch`` / ``run_claimed_batch`` 的调用方泄漏（worker 脚本不
    感知）；HTTP 层正常情况下永远不会见到它。
    """
    code = "lease_lost"
    http_status = 409


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


def heartbeat_enumeration(enumeration_id, worker_id, lease_token,
                          lease_seconds=ENUMERATION_LEASE_SECONDS):
    """只有持有当前领取令牌且枚举未过期的 worker 可以续租。"""
    return _progress_enumeration(enumeration_id, worker_id, None,
                                 lease_token, lease_seconds)


def _progress_enumeration(enumeration_id, worker_id, scanned, lease_token,
                          lease_seconds=ENUMERATION_LEASE_SECONDS):
    """每页回写进度并续租；False 表示失去租约，None 表示暂时写库失败。"""
    try:
        conn = _connect()
        try:
            with pg_store.transaction(conn) as c:
                with c.cursor() as cur:
                    cur.execute(
                        "UPDATE baidu_enumerations SET "
                        "scanned_count=COALESCE(%s, scanned_count), "
                        "lease_expires_at="
                        "now() + (%s || ' seconds')::interval, "
                        "updated_at=now() "
                        "WHERE id=%s AND lease_owner=%s AND lease_token=%s "
                        "AND state='enumerating' AND expires_at > now()",
                        (scanned, str(int(lease_seconds)), enumeration_id,
                         worker_id, lease_token))
                    return cur.rowcount == 1
        finally:
            conn.close()
    except Exception:
        # 进度写入可重试；最终提交仍须重新检查令牌，不能绕过 fence。
        return None


def _finalize_enumeration(enumeration_id, *, worker_id, lease_token,
                          state, complete, scanned, error_code=None,
                          incomplete=None, counters=None, files=(), dirs=()):
    """锁定当前领取令牌，原子提交候选与终态；失去租约返回 None。"""
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                cur.execute(
                    "SELECT id FROM baidu_enumerations WHERE id=%s "
                    "AND lease_owner=%s AND lease_token=%s "
                    "AND state='enumerating' AND expires_at > now() "
                    "FOR UPDATE", (enumeration_id, worker_id, lease_token))
                if cur.fetchone() is None:
                    return None
                # 兼容旧版本在候选提交后、终态提交前崩溃遗留的候选。
                cur.execute("DELETE FROM baidu_candidates WHERE enumeration_id=%s",
                            (enumeration_id,))
                count = (_insert_candidates(cur, enumeration_id, files, dirs)
                         if state == "ready" else 0)
                sets = ["state=%s", "complete=%s", "scanned_count=%s",
                        "candidate_count=%s", "error_code=%s",
                        "incomplete_reason=%s", "updated_at=now()",
                        "lease_owner=NULL", "lease_token=NULL",
                        "lease_expires_at=NULL"]
                args = [state, complete, scanned, count, error_code, incomplete]
                if counters:
                    sets += ["transfer_calls=%s", "download_calls=%s",
                             "delete_calls=%s"]
                    args += [counters["transfer"], counters["download"],
                             counters["delete"]]
                args.append(enumeration_id)
                cur.execute(
                    "UPDATE baidu_enumerations SET "
                    + ", ".join(sets) + " WHERE id=%s", tuple(args))
                return count
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
    worker_id = claim_row["lease_owner"]
    lease_token = claim_row["lease_token"]

    def abandon():
        return get_enumeration(enum_id, claim_row["owner_user_id"])

    if heartbeat_enumeration(enum_id, worker_id, lease_token) is False:
        return abandon()

    before = adapter.counters()
    files, dirs = [], set()
    scanned = 0
    error_code = None
    incomplete = None
    queue = [("", 0)]  # 根目录：省略 --source-dir（适配器合同）
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
                    # CLI 路径合同（docs §7.1）：递归 --source-dir 必须用
                    # CLI 返回的 path（/<分享内相对路径>，带前导 /）；
                    # path 缺失时回退拼 relative_path（适配器侧还会归一）
                    child = item.get("path")
                    if not isinstance(child, str) or not child.strip():
                        child = "/" + rel
                    queue.append((child, depth + 1))
                else:
                    files.append(item)
            if error_code is not None:
                break
            # 每页一次：中途回写进度 + 续租约（UI 轮询可见非零计数）
            if _progress_enumeration(enum_id, worker_id, scanned,
                                     lease_token) is False:
                return abandon()
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

    state = "ready" if error_code is None else "failed"
    count = _finalize_enumeration(
        enum_id, worker_id=worker_id, lease_token=lease_token,
        state=state, complete=error_code is None, scanned=scanned,
        error_code=error_code, incomplete=incomplete, counters=counters,
        files=files, dirs=dirs)
    if count is None:
        return abandon()
    if error_code is None:
        return {"id": enum_id, "state": "ready", "complete": True,
                "scanned": scanned, "candidates": count}
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
                names = [found[cid]["name"] for cid in candidate_ids]
                if len(names) != len(set(names)):
                    raise ValidationError(
                        "同一批次不能包含同名文件，请分批导入",
                        code="duplicate_filename")
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
                     "attempt": int(r["attempt"]),
                     "conversion_job_id": r.get("conversion_job_id"),
                     "slide_name": r.get("slide_name"),
                     "project_associate_state": r.get("project_associate_state")
                     or "not_needed"}
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


def heartbeat_batch(batch_id, worker_id, lease_token,
                    lease_seconds=BATCH_LEASE_SECONDS):
    """批次租约续期（对齐 ``heartbeat_enumeration``；额外匹配 lease_token：
    只有仍持有本次 claim 租约的 ``(worker_id, lease_token)`` 能续期）。

    批次被其他 worker 重领（token 更新）或已终结（state != 'running'）
    后返回 False——调用方（心跳线程）据此置标志、主循环安静放弃，
    绝不覆盖新 owner 的状态。
    """
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                cur.execute(
                    "UPDATE baidu_import_batches SET lease_expires_at="
                    "now() + (%s || ' seconds')::interval, updated_at=now() "
                    "WHERE id=%s AND lease_owner=%s AND lease_token=%s "
                    "AND state='running'",
                    (str(int(lease_seconds)), batch_id, worker_id,
                     lease_token))
                return cur.rowcount == 1
    finally:
        conn.close()


def _update_item(item_id, fields, batch_id=None, lease_token=None):
    """短事务更新条目（fields：列名→值 dict）。返回新行。

    带 fence（batch_id + lease_token）时：同一事务内以批次当前
    lease_token 匹配本次 claim 持有的 token（``IS NOT DISTINCT FROM``
    为 NULL 安全比较）——不匹配（租约已被其他 worker 经 claim_batch
    重领）→ 行数 0 → 抛 :class:`LeaseLost`，绝不覆盖新 owner 的条目
    状态。worker 写回路径（_phase_* / _fail_item / 对账）全部带 fence；
    读路径 ``_get_item`` 无需 fence。
    """
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                sets, args = ["updated_at=now()"], []
                for k, v in fields.items():
                    sets.append("%s=%%s" % k)
                    args.append(v)
                if batch_id is None:
                    # 无 fence 兼容路径（无租约上下文的调用方自行负责）
                    args.append(item_id)
                    cur.execute(
                        "UPDATE baidu_import_items SET " + ", ".join(sets) +
                        " WHERE id=%s RETURNING *", tuple(args))
                    return cur.fetchone()
                args.extend([item_id, batch_id, lease_token])
                cur.execute(
                    "UPDATE baidu_import_items SET " + ", ".join(sets) +
                    " WHERE id=%s AND (SELECT lease_token FROM "
                    "baidu_import_batches WHERE id=%s) "
                    "IS NOT DISTINCT FROM %s RETURNING *", tuple(args))
                row = cur.fetchone()
        if row is None:
            raise LeaseLost(
                "批次租约已被重领，条目写回被拒绝（item=%s）" % item_id)
        return row
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


def _fail_item(item_id, error_code, batch_id=None, lease_token=None):
    """条目置 failed（带 fence：租约被夺 → LeaseLost，绝不覆盖新 owner）。"""
    _update_item(item_id, {"stage": "failed", "error_code": error_code},
                 batch_id=batch_id, lease_token=lease_token)


def _phase_transfer(adapter, batch, item, hooks):
    """转存阶段（含崩溃对账：task_id poll → 副本对账 → 才考虑重转存）。"""
    bid, token = batch["id"], batch.get("lease_token")
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
                     "attempt": int(item["attempt"]) + 1}, bid, token)
            try:
                resp = adapter.transfer_selected(
                    batch["id"], batch["_share_url"],
                    batch.get("_extraction_code"), [item["fs_id"]])
            except AdapterError as exc:
                _fail_item(item["id"], exc.code, bid, token)
                return _get_item(item["id"])
            item = _update_item(item["id"],
                                {"transfer_task_id": resp["task_id"]},
                                bid, token)
            poll = adapter.poll_transfer(resp["task_id"])
            if poll.get("state") == "failed":
                _fail_item(item["id"], "transfer_failed", bid, token)
                return _get_item(item["id"])
        hook = hooks.get("on_transfer_persisted")
        if hook:
            hook(_get_item(item["id"]))  # 崩溃注入点 A
    return _get_item(item["id"])


def _phase_download(adapter, batch, item, staging_root, hooks):
    """下载阶段（sha 已记录且文件在盘 → 不重下载）。"""
    bid, token = batch["id"], batch.get("lease_token")
    stage = item["stage"]
    if stage in ("queued", "transferring", "downloading"):
        skip = False
        sp, sha = item["staging_path"], item["source_sha256"]
        if sha and sp and Path(sp).is_file() \
                and Path(sp).stat().st_size == int(item["source_size"]):
            skip = True
        if not skip:
            copies = {c["name"]: c for c in
                      adapter.list_batch_copies(batch["id"])}
            remote = copies.get(item["name"])
            if remote is not None \
                    and int(remote["size"]) != int(item["source_size"]):
                _fail_item(item["id"], "source_changed", bid, token)
                return _get_item(item["id"])
            if stage != "downloading":
                item = _update_item(item["id"], {"stage": "downloading"},
                                    bid, token)
            staging_dir = Path(staging_root) / batch["id"]
            try:
                adapter.download_to(
                    "%s/%s" % (batch["id"], item["name"]), staging_dir)
            except AdapterError as exc:
                _fail_item(item["id"], exc.code, bid, token)
                return _get_item(item["id"])
            spath = staging_dir / item["name"]
            if not spath.is_file():
                _fail_item(item["id"], "download_output_missing", bid, token)
                return _get_item(item["id"])
            digest = _sha256_file(spath)
            if spath.stat().st_size != int(item["source_size"]):
                _fail_item(item["id"], "size_mismatch", bid, token)
                return _get_item(item["id"])
            item = _update_item(
                item["id"], {"stage": "validating",
                             "staging_path": str(spath),
                             "source_sha256": digest}, bid, token)
        hook = hooks.get("on_downloaded")
        if hook:
            hook(_get_item(item["id"]))  # 崩溃注入点 B
    return _get_item(item["id"])


def _phase_convert_placeholder(adapter, batch, item):
    """进入 converting 或 ingesting；真正收口在 _phase_ingest。"""
    if item["stage"] == "validating":
        info = slide_format_registry.lookup(item["name"])
        new_stage = ("converting"
                     if info["capability"] ==
                     slide_format_registry.CAP_CONVERT_REQUIRED
                     else "ingesting")
        item = _update_item(item["id"], {"stage": new_stage},
                            batch["id"], batch.get("lease_token"))
    return item


def _reconcile_ingest(item, batch):
    """入库成功后、ingest_token 落库前崩溃的按标识对账。

    ingest_staging 先完成产物落盘与归属登记，之后才单独写 ingest_token；
    间隙崩溃重跑会因产物已存在 name_unavailable，把已成功任务打成
    failed。此处按标识确认产物已完整落成且归属本批 owner → 直接按成功
    路径落库（不再重跑入库）；不命中返回 None，调用方照旧走入库。
    """
    import baidu_ingest
    owner = batch.get("owner_user_id") or ""
    capability = slide_format_registry.lookup(item["name"])["capability"]
    if capability == slide_format_registry.CAP_CONVERT_REQUIRED:
        # convert：可见名 = canonical；锚点 = conversion job（owner/sha/state）
        import conversion_store
        visible = baidu_ingest.canonical_name_for(item["name"])
        job = conversion_store.get_job_by_canonical(visible)
        if job is None or job.get("state") != "ready" \
                or (job.get("owner_user_id") or "") != owner \
                or (job.get("source_sha256") or "").lower() != \
                (item.get("source_sha256") or "").lower():
            return None
        assoc = job.get("project_associate_state") or "not_needed"
        if assoc == "not_needed" and batch.get("target_project_id"):
            # 关联重放（associate_slide 幂等：已入项目返回 succeeded）
            assoc = baidu_ingest.associate_slide(
                owner, batch["target_project_id"], visible)
            conversion_store.set_project_associate(
                job["id"], batch["target_project_id"], assoc)
        return _update_item(item["id"], {
            "stage": "ready",
            "ingest_token": "cvj:" + job["id"],
            "conversion_job_id": job["id"],
            "slide_name": visible,
            "project_associate_state": assoc,
        }, batch["id"], batch.get("lease_token"))
    # native：可见名 = 条目名（枚举名已是 basename）；锚点 = 产物在盘、
    # 内容 sha 与条目下载摘要一致、share_store 元数据 owner 与批次 owner
    # 一致。首轮入库也会先过对账，故 sha 必须核：owner 既有的同名不同
    # 内容上传不得被认领（应走 ingest → name_unavailable）
    import share_store
    visible = item["name"]
    up_dir = os.environ.get("UPLOAD_DIR")
    if not up_dir or not (Path(up_dir) / visible).is_file():
        return None
    meta = share_store.get_slide_meta_full(visible)
    if (meta.get("owner_user_id") or "") != owner:
        return None
    if not item.get("source_sha256") \
            or _sha256_file(Path(up_dir) / visible) != \
            item["source_sha256"].lower():
        return None
    assoc = "not_needed"
    if batch.get("target_project_id"):
        assoc = baidu_ingest.associate_slide(
            owner, batch["target_project_id"], visible)
    return _update_item(item["id"], {
        "stage": "ready",
        "ingest_token": "slide:" + visible,
        "conversion_job_id": None,
        "slide_name": visible,
        "project_associate_state": assoc,
    }, batch["id"], batch.get("lease_token"))


def _phase_ingest(adapter, batch, item, hooks):
    """入库阶段：真实校验/转换/归属；ingest_token 是崩溃幂等凭证。"""
    bid, token = batch["id"], batch.get("lease_token")
    if item.get("ingest_token"):
        hook = hooks.get("on_ingested")
        if hook:
            hook(_get_item(item["id"]))
        return _get_item(item["id"])
    if item["stage"] not in ("converting", "ingesting", "validating"):
        return _get_item(item["id"])
    import baidu_ingest
    reconciled = _reconcile_ingest(item, batch)
    if reconciled is not None:
        # 间隙崩溃对账命中：产物已落成，按成功路径收口，不重跑入库
        item = reconciled
    else:
        try:
            result = baidu_ingest.ingest_staging(
                owner_user_id=batch.get("owner_user_id") or "",
                original_name=item["name"],
                staging_path=item.get("staging_path"),
                source_sha256=item.get("source_sha256"),
                source_size=item.get("source_size"),
                target_project_id=batch.get("target_project_id"))
        except baidu_ingest.IngestError as exc:
            _fail_item(item["id"], exc.code, bid, token)
            return _get_item(item["id"])
        item = _update_item(item["id"], {
            "stage": "ready",
            "ingest_token": result["ingest_token"],
            "conversion_job_id": result.get("conversion_job_id"),
            "slide_name": result.get("slide_name"),
            "project_associate_state": result.get("project_associate_state")
            or "not_needed",
        }, bid, token)
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
              hooks=None, lease_seconds=BATCH_LEASE_SECONDS,
              heartbeat_interval=None):
    """按 id 领取并推进一个批次到终态（不可领取返回 None）。

    ``hooks``：``on_transfer_persisted`` / ``on_downloaded`` /
    ``on_ingested``（崩溃注入点，异常向上传播 = 模拟进程崩溃；阶段与
    对账凭证已先落库）。取消：queued 条目停止；无 ready 产物时释放
    未消费预占。清理：仅本批 ready 项副本；失败置 cleanup_state=failed，
    不回滚 ready。

    心跳与 fencing：执行期间 daemon 心跳线程每 ``heartbeat_interval``
    秒（缺省 ``max(1, lease_seconds/3)``）调 ``heartbeat_batch`` 续租；
    心跳失败仅置标志。租约被夺后本 worker 的条目写回与收口全部被
    lease_token fence 拒绝 → 安静放弃（返回当前 get_import 视图，批次
    仍 running 由新 owner 推进；不清理副本、不重复配额动作），
    ``LeaseLost`` 不向调用方泄漏。
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

    return run_claimed_batch(claim, adapter, staging_root=staging_root,
                             worker_id=worker_id, hooks=hooks,
                             lease_seconds=lease_seconds,
                             heartbeat_interval=heartbeat_interval)


def _claim_with_secrets(claim):
    """补齐 claim["batch"] 的瞬态 ``_share_url``/``_extraction_code``。

    claim_batch 领取时不解密（保持轻量）；执行前从 baidu_enumerations
    解密补齐，结果只存内存（绝不回写/出线），与 run_batch 领取路径一致。
    """
    batch = claim["batch"]
    if "_share_url" in batch:
        return claim
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                cur.execute(
                    "SELECT share_url_enc, extraction_enc "
                    "FROM baidu_enumerations WHERE id=%s",
                    (batch["enumeration_id"],))
                enc = cur.fetchone()
    finally:
        conn.close()
    batch = dict(batch)
    batch["_share_url"] = decrypt_text(enc["share_url_enc"])
    batch["_extraction_code"] = decrypt_text(enc["extraction_enc"])
    return {"batch": batch, "items": claim["items"]}


def _batch_cancel_requested(batch_id):
    """重读批次 cancel_requested（取消请求与 worker 并发于不同进程）。"""
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                cur.execute(
                    "SELECT cancel_requested FROM baidu_import_batches "
                    "WHERE id=%s", (batch_id,))
                row = cur.fetchone()
                return bool(row and row["cancel_requested"])
    finally:
        conn.close()


def _heartbeat_loop(batch_id, worker_id, lease_token, interval,
                    lease_seconds, stop):
    """daemon 续租循环：每 ``interval`` 秒调 ``heartbeat_batch`` 续期。

    失败（租约被夺 / state 非 running / DB 异常）只置 ``stop`` 标志，
    绝不抛出——主循环在条目边界检查该标志安静放弃；置位后本线程随即
    退出（``finally`` 里主流程也用它停线程）。
    """
    while not stop.wait(interval):
        try:
            ok = heartbeat_batch(batch_id, worker_id, lease_token,
                                 lease_seconds=lease_seconds)
        except Exception:  # noqa: BLE001  心跳绝不干扰主流程
            ok = False
        if not ok:
            stop.set()
            return


def run_claimed_batch(claim, adapter, *, staging_root=None,
                      worker_id="worker", hooks=None, lease_seconds=None,
                      heartbeat_interval=None):
    """执行已领取的批次（claim_batch 返回值），推进到终态。

    与 :func:`run_batch` 只差领取方式：本函数直接消费 claim 持有的新鲜
    租约（两段式 ``claim_batch`` → ``run_claimed_batch``，与枚举侧
    ``claim_enumeration`` → ``run_one_enumeration`` 同款），不按 id 二次
    领取——二次领取因“queued 或租约过期”条件不满足恒返回 None，批次
    会永远停在 running。

    ``hooks``：``on_transfer_persisted`` / ``on_downloaded`` /
    ``on_ingested``（崩溃注入点，异常向上传播 = 模拟进程崩溃；阶段与
    对账凭证已先落库）。取消：queued 条目停止；无 ready 产物时释放
    未消费预占。清理：仅本批 ready 项副本；失败置 cleanup_state=failed，
    不回滚 ready。

    心跳与租约 fencing（P1）：claim 携带 lease_token 时起 daemon 心跳
    线程，每 ``heartbeat_interval`` 秒（缺省 ``max(1, lease_seconds/3)``；
    ``lease_seconds`` 缺省取 ``BATCH_LEASE_SECONDS``）续租一次。条目
    写回、取消收口、批次终态落库全部以本次 claim 的 lease_token 为
    fence：心跳失败（租约被夺）或任一 fence 拒绝（``LeaseLost``）→
    **安静放弃**——停心跳、不推进剩余条目、不 finalize、不清理副本、
    不做配额动作，返回当前 :func:`get_import` 视图（批次仍 running，
    由新 owner 推进到终态）；``LeaseLost`` 绝不向调用方泄漏。
    """
    claim = _claim_with_secrets(claim)
    batch, items = claim["batch"], claim["items"]
    seen_names, duplicate_names = set(), set()
    for item in items:
        if item["name"] in seen_names:
            duplicate_names.add(item["name"])
        seen_names.add(item["name"])
    hooks = hooks or {}
    staging_root = Path(staging_root or STAGING_ROOT)

    if lease_seconds is None:
        lease_seconds = BATCH_LEASE_SECONDS
    stop_hb = threading.Event()  # 置位 = 停心跳（含租约丢失）
    hb = None
    if batch.get("lease_token"):
        interval = (max(1.0, lease_seconds / 3.0)
                    if heartbeat_interval is None
                    else float(heartbeat_interval))
        hb = threading.Thread(
            target=_heartbeat_loop,
            args=(batch["id"], worker_id, batch["lease_token"], interval,
                  int(lease_seconds), stop_hb),
            name="baidu-batch-heartbeat-%s" % batch["id"], daemon=True)
        hb.start()

    def _abandon():
        # 安静放弃：不收口/不清理，返回当前视图（批次仍 running，
        # cancel_requested/终态由赢得租约的新 owner 落地）
        return get_import(batch["id"], batch["owner_user_id"])

    try:
        if batch["cancel_requested"]:
            _apply_cancel(batch)
            return get_import(batch["id"], batch["owner_user_id"])

        for item in items:
            if stop_hb.is_set():
                return _abandon()  # 心跳显示租约已丢：安静放弃
            if item["stage"] in ("ready", "failed", "cancelled"):
                continue
            if _batch_cancel_requested(batch["id"]):
                # 运行中收到取消：停止剩余条目并按取消语义收口（不走 finalize）
                _apply_cancel(batch)
                return get_import(batch["id"], batch["owner_user_id"])
            try:
                # 旧版本已经接受的同名批次同样不能继续复用副本。
                if item["name"] in duplicate_names:
                    _fail_item(item["id"], "duplicate_filename",
                               batch["id"], batch.get("lease_token"))
                    continue
                item = _phase_transfer(adapter, batch, item, hooks)
                if item["stage"] == "failed":
                    continue
                item = _phase_download(adapter, batch, item, staging_root,
                                       hooks)
                if item["stage"] == "failed":
                    continue
                item = _phase_convert_placeholder(adapter, batch, item)
                item = _phase_ingest(adapter, batch, item, hooks)
            except AdapterError as exc:
                # 副本对账也会失败，必须收口为可见错误，不能遗留 running。
                try:
                    _fail_item(item["id"], exc.code,
                               batch["id"], batch.get("lease_token"))
                except LeaseLost:
                    return _abandon()
            except LeaseLost:
                # 租约已被其他 worker 重领：绝不能覆盖新 owner 的条目
                # 状态，也不得 finalize/清理（会误删新 worker 的副本、
                # 重复配额动作）——安静放弃
                return _abandon()

        _finalize_batch(batch, adapter, worker_id)
        return get_import(batch["id"], batch["owner_user_id"])
    finally:
        stop_hb.set()  # 停心跳（wait 立即返回，线程随即退出）
        if hb is not None:
            hb.join(timeout=5)


def _apply_cancel(batch):
    """取消收口：停止未开始条目；有 ready 产物时批次落 partial_failed
    （成功产物不冒充失败也不删除）；无 ready 产物释放未消费预占。

    Fencing：条目取消与批次 terminal 落库均要求本次 claim 的
    lease_token 仍是批次当前 token（事务先 FOR UPDATE 锁批次行并核对，
    与 claim_batch 的 SKIP LOCKED 串行化，杜绝「条目已取消而批次落库
    被拒」的中间态）；不匹配（新 worker 已重领）→ 原样跳过、不动配额
    ——新 owner 会看到 cancel_requested 并自行收口。返回本 worker 是否
    完成收口。
    """
    owner = batch.get("lease_owner")
    token = batch.get("lease_token")
    conn = _connect()
    applied = False
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                cur.execute(
                    "SELECT lease_token FROM baidu_import_batches "
                    "WHERE id=%s FOR UPDATE", (batch["id"],))
                lease_row = cur.fetchone()
                if lease_row is None or lease_row["lease_token"] != token:
                    return False  # 租约已被夺：新 owner 负责取消收口
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
                    "AND stage NOT IN ('ready','failed','cancelled') "
                    "AND (SELECT lease_token FROM baidu_import_batches "
                    "WHERE id=%s) IS NOT DISTINCT FROM %s",
                    (batch["id"], batch["id"], token))
                state = ("partial_failed" if has_ready
                         else ("failed" if has_failed else "cancelled"))
                cur.execute(
                    "UPDATE baidu_import_batches SET state=%s, "
                    "lease_owner=NULL, lease_token=NULL, "
                    "lease_expires_at=NULL, updated_at=now() "
                    "WHERE id=%s AND lease_owner IS NOT DISTINCT FROM %s "
                    "AND lease_token IS NOT DISTINCT FROM %s RETURNING *",
                    (state, batch["id"], owner, token))
                if cur.rowcount != 1:
                    return False  # 新 worker 已接管：跳过配额收口
                applied = True
                reservation_id = batch["quota_reservation_id"]
                cur.execute(
                    "SELECT COALESCE(SUM(source_size),0)::bigint AS bytes "
                    "FROM baidu_import_items WHERE batch_id=%s "
                    "AND stage='ready'", (batch["id"],))
                ready_bytes = int(cur.fetchone()["bytes"])
    finally:
        conn.close()
    if not applied:
        return False
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
    return True


def _finalize_batch(batch, adapter, worker_id):
    """聚合条目终态 → 批次终态；配额一次收口；本批副本清理。

    Fencing：事务先 FOR UPDATE 锁批次行并核对本次 claim 的 lease_token，
    批次 terminal UPDATE 再以 ``lease_owner/lease_token`` 为条件；行数
    0（新 worker 已重领）→ **跳过配额收口与副本清理**——清理删除的是
    「当前批次目录」下的副本，新旧 worker 同批次路径同名，旧 worker
    清理会误删新 worker 的副本。配额 consume/release 只由赢得条件更新
    的那方执行。返回本 worker 是否赢得收口。
    """
    owner = batch.get("lease_owner")
    token = batch.get("lease_token")
    conn = _connect()
    won = False
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                cur.execute(
                    "SELECT lease_token FROM baidu_import_batches "
                    "WHERE id=%s FOR UPDATE", (batch["id"],))
                lease_row = cur.fetchone()
                if lease_row is None or lease_row["lease_token"] != token:
                    return False  # 新 worker 已接管：不收口、不清理
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
                    return False  # 尚有条目在途（本轮未推进完），不改批次态
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
                    "lease_expires_at=NULL, updated_at=now() "
                    "WHERE id=%s AND lease_owner IS NOT DISTINCT FROM %s "
                    "AND lease_token IS NOT DISTINCT FROM %s",
                    (state, batch["id"], owner, token))
                if cur.rowcount != 1:
                    return False  # 条件更新未赢：跳过配额收口与清理
                won = True
                reservation_id = batch["quota_reservation_id"]
                consumed_bytes = ready_bytes
    finally:
        conn.close()
    if not won:
        return False
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
    return True


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
