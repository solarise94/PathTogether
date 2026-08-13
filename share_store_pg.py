# -*- coding: utf-8 -*-
"""切片分享 —— 共享存储层 PostgreSQL 后端实现（Stage 3b-2）。

逐函数对照 `share_store_json` 语义移植（44 个公共名全实现 + 稳定 slide 身份）。
调用方仍经 `share_store` dispatcher 访问（`STORAGE_BACKEND=postgres` 时 re-export
本模块），app.py / share_server.py / tests 一行不改。

数据模型（对应 migrations/0001_init.sql + 0002_roi_payload.sql）：
  - shares       → shares（slides/permissions/roi_sizes 暂保 JSONB 数组形态）
  - grants       → grants（active 布尔 ⇔ json 的 revoked_at is None）
  - rois         → rois（权威 dict 存 data JSONB，离散列镜像供过滤；insert_seq
                    保证 token 内插入序 = json 文件内数组顺序）
  - change_log   → change_log（bigserial seq 即全局单调序号；json 是 per-slide
                    计数器，两者数值不同——允许的实现差，见模块 docstring）
  - slide_meta   → slides（name=legacy_filename；稳定 slide_id 在此生成/维护）
  - projects     → projects / project_slides

与 JSON 实现的**允许实现差**（测试断言 seq 具体值需归类为 json-only）：
  - json 的 change_seq 是 per-slide 计数器；PG 用 change_log 的全局 bigserial seq。
    `list_changes(slide, after_seq)` / `current_change_seq(slide)` 在 PG 按全局 seq
    过滤/取值，语义（单调、按 slide 过滤）一致，但数值不同。
  - 时间戳在库中为 TIMESTAMPTZ，读出统一转 epoch 浮点，与 json 的浮点形状一致。

稳定 slide 身份（本节点文档验收点，见 docs §Stage 3b）：
  - `set_slide_meta(name, ...)`：name（legacy_filename）首次出现 → 生成稳定
    slide_id（sld_ + 12 位 urlsafe）插入 slides 行；同名已存在 → 仅更新
    alias/note/owner/public，slide_id 不动。
  - `get_slide_id(name)` / `resolve_slide_ref(name)`：name ⇄ slide_id 映射查询。
  - `record_slide_asset(slide_id, legacy_revision)`：记录切片内容资产 revision
    （content_sha256 由 Stage 3b-3 迁移工具填充，本节点先用 legacy_revision 占位）。
"""

import hashlib
import hmac
import secrets
import time
import uuid

import psycopg

import pg_store
from share_store_json import (
    DEFAULT_PERMISSIONS,
    PERMISSION_VIEW,
    PERMISSION_ANNOTATE,
    PERMISSION_DOWNLOAD,
    ROI_TYPES,
    ALLOWED_ROI_SIZES,
    DEFAULT_ROI_SIZES,
    _clean_note,
    _clean_comment_body,
    _grant_out,
    _hash_installation_secret,
    _installation_out,
    _is_active,
    _norm_label,
    _normalize_permissions,
    _normalize_roi_sizes,
    _reject_guest_write,
    _roi_shared_compat,
    _share_permissions,
    _share_roi_sizes,
    _status_of,
    _validate_geom,
)


class RevisionConflict(Exception):
    """CAS 失败：expected_revision 与当前 revision 不符（与 json 同语义）。

    携带 ``current_revision``（int）。pg 后端独立定义一份，保证 postgres 模式下
    ``share_store.RevisionConflict`` 与本模块抛出的类一致（json 的 _check_cas 不能
    直接复用——它引用 json 的 RevisionConflict）。
    """

    def __init__(self, current_revision, message=None):
        self.current_revision = int(current_revision)
        super().__init__(
            message or "revision 冲突：标注已被他人修改，请刷新后重试")

# 文件路径占位：PG 后端不用文件（dispatcher 公共名校验需要这些名字存在）
SHARE_DATA_DIR = None
SHARE_FILE = None
# 兼容测试里 `SHARE_FILE.write_text(...)` 等文件调用：PG 后端这些测试会被标记跳过，
# 故这里保持 None 即可（见 tests/pg_compat.json_only）。

# 常量（re-export 自 json impl，dispatcher 公共名需要）
ROI_TYPES = ROI_TYPES
ALLOWED_ROI_SIZES = ALLOWED_ROI_SIZES
DEFAULT_ROI_SIZES = DEFAULT_ROI_SIZES
ADMIN_TOKEN = "admin"
PERMISSION_VIEW = PERMISSION_VIEW
PERMISSION_ANNOTATE = PERMISSION_ANNOTATE
PERMISSION_DOWNLOAD = PERMISSION_DOWNLOAD
DEFAULT_PERMISSIONS = DEFAULT_PERMISSIONS


def _connect():
    """建连接并设 dict_row（本模块所有查询按列名访问）。"""
    conn = pg_store.connect()
    conn.row_factory = psycopg.rows.dict_row
    return conn


# 数据归属：由 app.py 启动时注入首个 owner 的 user_id（与 json 的 _OWNER_USER_ID
# 语义一致，作为新建 roi/project/slide_meta 的缺省 owner）。
_OWNER_USER_ID = ""


def set_owner_user_id(user_id: str) -> None:
    """注入当前 owner 的 user_id（供数据归属缺省值使用）。"""
    global _OWNER_USER_ID
    _OWNER_USER_ID = user_id or ""


# --------------------------------------------------------------------------- #
# 内部工具
# --------------------------------------------------------------------------- #
# roi 几何字段（存 data JSONB，镜像到 geom 列）
_GEOM_KEYS = ("x", "y", "side_px", "size_mm", "x1", "y1", "x2", "y2", "points")


def _geom_of(roi: dict) -> dict:
    return {k: roi[k] for k in _GEOM_KEYS if k in roi}


def _insert_roi(cur, roi: dict, rid: str):
    """插入一条 roi：data 存权威 dict，离散列镜像，返回 insert_seq。"""
    now = roi.get("updated_at") or roi.get("ts") or time.time()
    cur.execute(
        "INSERT INTO rois "
        "(id, token, slide, annotation_id, label, type, geom, size_mm, shared, "
        " note, deleted, owner_user_id, created_at, updated_at, data) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s, to_timestamp(%s), "
        "to_timestamp(%s), %s) RETURNING insert_seq",
        (
            rid, roi.get("token"), roi.get("slide"), roi.get("annotation_id"),
            roi.get("label", ""), roi.get("type", "rect"),
            psycopg.types.json.Jsonb(_geom_of(roi)),
            roi.get("size_mm", 0.0), bool(roi.get("shared", False)),
            roi.get("note", ""), bool(roi.get("deleted", False)),
            roi.get("owner_user_id"), now, now,
            psycopg.types.json.Jsonb(roi),
        ),
    )
    return cur.fetchone()["insert_seq"]


def _update_roi_row(cur, rid: str, roi: dict):
    """更新一条 roi（data + 离散镜像列）。"""
    now = roi.get("updated_at") or time.time()
    cur.execute(
        "UPDATE rois SET geom=%s, size_mm=%s, shared=%s, note=%s, deleted=%s, "
        "updated_at=to_timestamp(%s), data=%s, annotation_id=%s, label=%s, "
        "type=%s, owner_user_id=%s WHERE id=%s",
        (
            psycopg.types.json.Jsonb(_geom_of(roi)),
            roi.get("size_mm", 0.0), bool(roi.get("shared", False)),
            roi.get("note", ""), bool(roi.get("deleted", False)), now,
            psycopg.types.json.Jsonb(roi), roi.get("annotation_id"),
            roi.get("label", ""), roi.get("type", "rect"),
            roi.get("owner_user_id"), rid,
        ),
    )


def _roi_out(roi: dict, index=None, shared=None) -> dict:
    """ROI 导出副本：统一补 index/shared/note 兼容字段（与 json 一致）。

    tombstone（deleted=true）只保留最小字段；非 tombstone 补 review_status。
    """
    if roi.get("deleted"):
        out = {
            "annotation_id": roi.get("annotation_id"),
            "slide": roi.get("slide"),
            "token": roi.get("token"),
            "revision": int(roi.get("revision") or 1),
            "deleted": True,
            "deleted_at": roi.get("deleted_at"),
            "change_seq": roi.get("change_seq"),
            "type": "annotation",
        }
        if index is not None:
            out["index"] = index
        return out
    out = dict(roi)
    if index is not None:
        out["index"] = index
    if shared is not None:
        out["shared"] = bool(shared)
    out["note"] = roi.get("note", "")
    out.setdefault("review_status", "none")
    # Stage 3c-2：历史 AI 标注（source=ai 但无 provenance）输出 partial 标记
    if roi.get("source") == "ai" and not isinstance(roi.get("provenance"), dict):
        out["provenance"] = {"partial": True}
    return out


def _check_cas(roi, expected_revision):
    """Stage 3c-1 CAS：expected_revision 提供且与当前 revision 不符 → 抛 RevisionConflict。

    引用本模块（pg）的 RevisionConflict，保证 postgres 模式下异常类一致。
    """
    if expected_revision is None:
        return
    cur = int(roi.get("revision") or 1)
    if int(expected_revision) != cur:
        raise RevisionConflict(cur)


def _append_history(roi):
    """Stage 3c-1 修改历史：把当前快照 append 进 roi['history']，上限 20，丢最旧。
    在 update/tombstone 修改**之前**调用。pg 存 data jsonb 内（同 roi dict）。
    """
    snap = {
        "geom": {k: roi[k] for k in _GEOM_KEYS if k in roi},
        "note": roi.get("note", ""),
        "label": roi.get("label", ""),
        "revision": int(roi.get("revision") or 1),
        "ts": roi.get("ts"),
    }
    hist = roi.setdefault("history", [])
    hist.append(snap)
    if len(hist) > 20:
        del hist[: len(hist) - 20]


def _bump_change_seq(cur, slide, token, annotation_id, op):
    """写一条 change_log，返回全局单调 seq（作为该 roi 的 change_seq）。"""
    cur.execute(
        "INSERT INTO change_log (slide, token, annotation_id, op) "
        "VALUES (%s,%s,%s,%s) RETURNING seq",
        (slide, token, annotation_id, op),
    )
    return cur.fetchone()["seq"]


# --------------------------------------------------------------------------- #
# 分享（shares）
# --------------------------------------------------------------------------- #
def create_share(slides, expires_hours, roi_sizes=None, permissions=None,
                 creator_user_id=None, requester_role=None):
    """创建分享：生成 token、写入并返回 share dict（含 token 与 roi_sizes）。"""
    _reject_guest_write(requester_role)
    roi_sizes_norm = _normalize_roi_sizes(roi_sizes)
    perms = _normalize_permissions(permissions)
    creator = creator_user_id or None
    token = secrets.token_urlsafe(18)
    now = time.time()
    expires_at = now + float(expires_hours) * 3600.0

    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                cur.execute(
                    "INSERT INTO shares "
                    "(token, slides, permissions, roi_sizes, expires_at, revoked, "
                    " creator_user_id) "
                    "VALUES (%s,%s,%s,%s, to_timestamp(%s), FALSE, %s)",
                    (token, psycopg.types.json.Jsonb(list(slides)),
                     psycopg.types.json.Jsonb(list(perms)),
                     psycopg.types.json.Jsonb(list(roi_sizes_norm)),
                     expires_at, creator),
                )
        return {
            "slides": list(slides),
            "created_at": now,
            "expires_at": expires_at,
            "revoked": False,
            "token": token,
            "roi_sizes": list(roi_sizes_norm),
            "permissions": list(perms),
            "creator_user_id": creator,
        }
    finally:
        conn.close()


_SHARE_SEL = (
    "token, slides, permissions, roi_sizes, "
    "extract(epoch from expires_at)::float8 AS expires_at, revoked, "
    "creator_user_id, extract(epoch from created_at)::float8 AS created_at"
)


def _fetch_share(cur, token):
    cur.execute("SELECT " + _SHARE_SEL + " FROM shares WHERE token=%s", (token,))
    return cur.fetchone()


def get_share(token):
    """获取有效分享；不存在/已撤销/已过期返回 None。"""
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                share = _fetch_share(cur, token)
        if share is None:
            return None
        if not _is_active(share):
            return None
        out = dict(share)
        out["token"] = token
        out["roi_sizes"] = _share_roi_sizes(share)
        out["permissions"] = _share_permissions(share)
        return out
    finally:
        conn.close()


def list_shares():
    """返回全部分享（含 status 与 roi_sizes 字段），按 created_at 倒序。"""
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                cur.execute("SELECT " + _SHARE_SEL +
                            " FROM shares ORDER BY created_at DESC, token")
                rows = cur.fetchall()
        items = []
        for row in rows:
            sh = dict(row)
            out = dict(sh)
            out["status"] = _status_of(sh)
            out["roi_sizes"] = _share_roi_sizes(sh)
            out["permissions"] = _share_permissions(sh)
            items.append(out)
        return items
    finally:
        conn.close()


def revoke_share(token):
    """撤销分享，返回是否成功。"""
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                cur.execute("UPDATE shares SET revoked=TRUE WHERE token=%s", (token,))
                return cur.rowcount > 0
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# 认领（grants）
# --------------------------------------------------------------------------- #
def claim_share(token, user_id, permissions=None):
    """user 认领分享链接（幂等）。返回 grant dict。"""
    if not isinstance(token, str) or not token:
        raise ValueError("token 不能为空")
    if not isinstance(user_id, str) or not user_id:
        raise ValueError("user_id 不能为空")
    perms = _normalize_permissions(permissions)

    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                # 幂等：同 token + 同 user 且未失效（active ⇔ json revoked_at is None）
                cur.execute(
                    "SELECT id, token, user_id, permissions, "
                    "extract(epoch from claimed_at)::float8 AS claimed_at, active "
                    "FROM grants WHERE token=%s AND user_id=%s AND active",
                    (token, user_id),
                )
                existing = cur.fetchone()
                if existing is not None:
                    g = dict(existing)
                    g["grant_id"] = g["id"]
                    g["share_token"] = g["token"]
                    g["revoked_at"] = None
                    return _grant_out(g)
                gid = "grt_" + secrets.token_urlsafe(8)
                now = time.time()
                cur.execute(
                    "INSERT INTO grants (id, token, user_id, permissions, "
                    "claimed_at, active) VALUES (%s,%s,%s,%s, to_timestamp(%s), TRUE)",
                    (gid, token, user_id, psycopg.types.json.Jsonb(list(perms)), now),
                )
                g = {
                    "grant_id": gid,
                    "user_id": user_id,
                    "share_token": token,
                    "permissions": list(perms),
                    "claimed_at": now,
                    "revoked_at": None,
                }
                return _grant_out(g)
    finally:
        conn.close()


def claimed_active_slides_for_user(user_id):
    """返回该 user 认领过的、且对应 share 仍 active 的切片名集合。"""
    if not user_id:
        return set()
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                cur.execute(
                    "SELECT token FROM grants WHERE user_id=%s AND active", (user_id,)
                )
                grants = cur.fetchall()
                out = set()
                for g in grants:
                    tok = g["token"]
                    share = _fetch_share(cur, tok)
                    if share is None or not _is_active(share):
                        continue
                    for s in share.get("slides") or []:
                        if isinstance(s, str):
                            out.add(s)
                return out
    finally:
        conn.close()


def list_grants_for_user(user_id):
    """返回该 user 的全部 grant（含已失效，附 share_active 标志）。供调试/审计。"""
    if not user_id:
        return []
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                cur.execute(
                    "SELECT id, token, user_id, permissions, "
                    "extract(epoch from claimed_at)::float8 AS claimed_at, active "
                    "FROM grants WHERE user_id=%s ORDER BY claimed_at DESC",
                    (user_id,),
                )
                rows = cur.fetchall()
                out = []
                for row in rows:
                    g = dict(row)
                    g["grant_id"] = g["id"]
                    g["share_token"] = g["token"]
                    g["revoked_at"] = None
                    share = _fetch_share(cur, g["share_token"])
                    g["share_active"] = bool(
                        share is not None and _is_active(share))
                    out.append(_grant_out(g))
        out.sort(key=lambda x: x.get("claimed_at", 0), reverse=True)
        return out
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# 标注（rois）
# --------------------------------------------------------------------------- #
def add_roi(token, slide, label, type="rect", size_mm=0.0, shared=False, note="", visitor=None,
            source=None, created_by_session_id=None, _effect_key=None, owner_user_id=None,
            requester_role=None, provenance=None, **geom):
    """为 token 的 share 添加一条标注；统一入口，支持 rect/arrow/freehand。

    语义与 json 完全一致（含 WAL effect_key 幂等、index 语义、source 推断）。
    """
    _reject_guest_write(requester_role)
    if type not in ROI_TYPES:
        raise ValueError("未知标注类型")
    if not isinstance(label, str):
        raise ValueError("请填写用户名或标签")
    label = label.strip()
    if not label:
        raise ValueError("请填写用户名或标签")
    note_clean = _clean_note(note)

    geom_full = dict(geom)
    geom_full["size_mm"] = size_mm
    norm = _validate_geom(type, geom_full)
    norm["type"] = type

    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                is_admin = (token == ADMIN_TOKEN)
                if not is_admin:
                    share = _fetch_share(cur, token)
                    if share is None or not _is_active(share):
                        raise ValueError("share invalid")
                    if slide not in (share.get("slides") or []):
                        raise ValueError("slide not in share")
                # WAL 幂等：effect_key 已落 → 复用返回
                if _effect_key:
                    cur.execute(
                        "SELECT data FROM rois WHERE NOT deleted "
                        "AND data->>'effect_key'=%s ORDER BY insert_seq",
                        (_effect_key,),
                    )
                    # 需在同 token 内定位 index（含 tombstone，同 json 的 unfiltered）
                    cur.execute(
                        "SELECT data FROM rois WHERE token=%s ORDER BY insert_seq",
                        (token,),
                    )
                    all_rows = cur.fetchall()
                    idx = None
                    hit = None
                    for i, row in enumerate(all_rows):
                        if row["data"].get("effect_key") == _effect_key and \
                                not row["data"].get("deleted"):
                            idx = i
                            hit = row["data"]
                            break
                    if hit is not None:
                        return _roi_out(hit, index=(idx if idx is not None else
                                                     len(all_rows) - 1),
                                        shared=_roi_shared_compat(hit))
                now = time.time()
                src = source if source in ("ai", "human") else (
                    "ai" if (is_admin and not shared) else "human")
                # index = 该 token 全部 roi（含 tombstone）中新增前的数量
                cur.execute("SELECT count(*) FROM rois WHERE token=%s", (token,))
                total = int(cur.fetchone()["count"])
                roi = {
                    "token": token,
                    "slide": slide,
                    "label": label,
                    "ts": now,
                    "shared": bool(shared),
                    "note": note_clean,
                    "visitor": visitor or "",
                    "annotation_id": str(uuid.uuid4()),
                    "source": src,
                    "created_by_session_id": created_by_session_id or "",
                    "revision": 1,
                    "updated_at": now,
                    "deleted": False,
                    "owner_user_id": owner_user_id or _OWNER_USER_ID or None,
                    # Stage 3c-1：AI 新写入默认 pending 待审；人工标注 none
                    "review_status": "pending" if src == "ai" else "none",
                }
                if _effect_key:
                    roi["effect_key"] = _effect_key
                # Stage 3c-2：AI 溯源子对象（仅 AI 写入，且仅当传入非空 dict 才落）
                if src == "ai" and isinstance(provenance, dict) and provenance:
                    roi["provenance"] = dict(provenance)
                roi.update(norm)
                roi["change_seq"] = _bump_change_seq(
                    cur, slide, token, roi["annotation_id"], "add")
                rid = "roi_" + secrets.token_urlsafe(10)
                _insert_roi(cur, roi, rid)
                out = _roi_out(roi)
                out["index"] = total
                out["shared"] = bool(shared)
                return out
    finally:
        conn.close()


def update_roi(token, index, geom=None, note=None, expected_revision=None):
    """更新该 token 下第 index 条 roi 的几何与/或备注。返回更新后的 dict 或 False。

    expected_revision（CAS）：提供且与当前 revision 不符 → 抛 RevisionConflict。
    修改前 append history 快照（上限 20）。
    """
    if geom is not None and not isinstance(geom, dict):
        raise ValueError("geom 需为对象")
    note_clean = "_UNSET_"
    if note is not None:
        note_clean = _clean_note(note)

    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                is_admin = (token == ADMIN_TOKEN)
                if not is_admin:
                    share = _fetch_share(cur, token)
                    if share is None or not _is_active(share):
                        raise ValueError("share invalid")
                cur.execute(
                    "SELECT id, data FROM rois WHERE token=%s AND NOT deleted "
                    "ORDER BY insert_seq",
                    (token,),
                )
                same = cur.fetchall()
                if index < 0 or index >= len(same):
                    return False
                rid = same[index]["id"]
                roi = dict(same[index]["data"])
                _check_cas(roi, expected_revision)  # CAS 在修改前校验
                orig_type = roi.get("type", "rect")
                _append_history(roi)  # 修改历史快照（修改前）
                if geom is not None:
                    geom_full = dict(geom)
                    if orig_type == "rect" and "size_mm" not in geom_full:
                        geom_full["size_mm"] = roi.get("size_mm", 0.0)
                    norm_g = _validate_geom(orig_type, geom_full)
                    norm_g["type"] = orig_type
                    roi.update(norm_g)
                if note_clean != "_UNSET_":
                    roi["note"] = note_clean
                roi["revision"] = int(roi.get("revision") or 1) + 1
                roi["change_seq"] = _bump_change_seq(
                    cur, roi.get("slide"), token, roi.get("annotation_id"), "update")
                roi["updated_at"] = time.time()
                _update_roi_row(cur, rid, roi)
                # index：同 token 非 tombstone 中按插入序
                cur.execute(
                    "SELECT data FROM rois WHERE token=%s AND NOT deleted "
                    "ORDER BY insert_seq", (token,))
                all_rows = cur.fetchall()
                idx = next((i for i, row in enumerate(all_rows)
                            if row["data"].get("annotation_id") ==
                            roi.get("annotation_id")), 0)
                out = _roi_out(roi)
                out["index"] = idx
                out["shared"] = _roi_shared_compat(roi)
                return out
    finally:
        conn.close()


def list_rois(token=None):
    """返回 ROI 列表；可按 token 过滤（跳过 tombstone）。"""
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                if token is not None:
                    cur.execute(
                        "SELECT data FROM rois WHERE token=%s AND NOT deleted "
                        "ORDER BY insert_seq", (token,))
                    rows = cur.fetchall()
                else:
                    cur.execute(
                        "SELECT data FROM rois WHERE NOT deleted ORDER BY insert_seq")
                    rows = cur.fetchall()
        if token is not None:
            out = []
            for i, row in enumerate(rows):
                r = dict(row["data"])
                r["index"] = i
                r["shared"] = _roi_shared_compat(r)
                r["note"] = r.get("note", "")
                out.append(r)
            out.sort(key=lambda x: x.get("ts", 0), reverse=True)
            return out
        from collections import defaultdict
        counters = defaultdict(int)
        out = []
        for row in rows:
            r = dict(row["data"])
            r["index"] = counters[r["token"]]
            counters[r["token"]] += 1
            r["shared"] = _roi_shared_compat(r)
            r["note"] = r.get("note", "")
            out.append(r)
        out.sort(key=lambda x: x.get("ts", 0), reverse=True)
        return out
    finally:
        conn.close()


def get_roi(token, index):
    """返回该 token 下第 index 条 roi 的 dict 副本（跳过 tombstone）；无则 None。"""
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                cur.execute(
                    "SELECT data FROM rois WHERE token=%s AND NOT deleted "
                    "ORDER BY insert_seq", (token,))
                rows = cur.fetchall()
        if index < 0 or index >= len(rows):
            return None
        r = dict(rows[index]["data"])
        r["index"] = index
        r["shared"] = _roi_shared_compat(r)
        r["visitor"] = r.get("visitor", "") or ""
        r["note"] = r.get("note", "")
        return r
    finally:
        conn.close()


def get_roi_by_annotation_id(annotation_id):
    """按稳定 annotation_id 取 ROI 完整 dict（含 tombstone）；不存在返回 None。"""
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                cur.execute("SELECT data FROM rois WHERE annotation_id=%s",
                            (annotation_id,))
                row = cur.fetchone()
        return dict(row["data"]) if row else None
    finally:
        conn.close()


def delete_roi(token, index, expected_revision=None):
    """删除该 token 下第 index 条 ROI（置 tombstone）。返回 (bool, annotation_id|None)。

    expected_revision（CAS）：提供且与当前 revision 不符 → 抛 RevisionConflict。
    tombstone 设 deleted_at + bump revision/change_seq + append history。
    """
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                cur.execute(
                    "SELECT id, data FROM rois WHERE token=%s AND NOT deleted "
                    "ORDER BY insert_seq", (token,))
                same = cur.fetchall()
                if index < 0 or index >= len(same):
                    return False, None
                rid = same[index]["id"]
                roi = dict(same[index]["data"])
                if roi.get("deleted"):
                    return False, None
                _check_cas(roi, expected_revision)
                _append_history(roi)
                roi["deleted"] = True
                roi["deleted_at"] = time.time()
                roi["revision"] = int(roi.get("revision") or 1) + 1
                roi["change_seq"] = _bump_change_seq(
                    cur, roi.get("slide"), token, roi.get("annotation_id"), "delete")
                roi["updated_at"] = roi["deleted_at"]
                _update_roi_row(cur, rid, roi)
                return True, roi.get("annotation_id")
    finally:
        conn.close()


def delete_roi_by_annotation_id(annotation_id, expected_revision=None):
    """按稳定 annotation_id 删除（tombstone 语义同 delete_roi）；返回是否成功。

    expected_revision（CAS）：提供且与当前 revision 不符 → 抛 RevisionConflict。
    """
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                cur.execute(
                    "SELECT id, data FROM rois WHERE annotation_id=%s AND NOT deleted",
                    (annotation_id,))
                row = cur.fetchone()
                if row is None:
                    return False
                rid = row["id"]
                roi = dict(row["data"])
                _check_cas(roi, expected_revision)
                _append_history(roi)
                roi["deleted"] = True
                roi["deleted_at"] = time.time()
                roi["revision"] = int(roi.get("revision") or 1) + 1
                roi["change_seq"] = _bump_change_seq(
                    cur, roi.get("slide"), roi.get("token"),
                    roi.get("annotation_id"), "delete")
                roi["updated_at"] = roi["deleted_at"]
                _update_roi_row(cur, rid, roi)
                return True
    finally:
        conn.close()


def list_changes(slide, after_seq):
    """返回 change_seq > after_seq 的全部变更（含 tombstone）。

    Stage 3c-1：含评论增删（type=comment）与标注变更（type=annotation）；tombstone
    标注走 _roi_out 最小字段输出。
    """
    if not isinstance(after_seq, (int, float)):
        after_seq = 0
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                cur.execute(
                    "SELECT data FROM rois WHERE slide=%s ORDER BY insert_seq",
                    (slide,))
                rows = cur.fetchall()
                cur.execute(
                    "SELECT data FROM comments WHERE slide=%s ORDER BY created_at",
                    (slide,))
                crows = cur.fetchall()
        out = []
        for row in rows:
            r = row["data"]
            cs = r.get("change_seq")
            if cs is None or not isinstance(cs, (int, float)) or cs <= after_seq:
                continue
            rr = _roi_out(r)
            rr.setdefault("type", "annotation")
            out.append(rr)
        for row in crows:
            c = row["data"]
            cs = c.get("change_seq")
            if cs is None or not isinstance(cs, (int, float)) or cs <= after_seq:
                continue
            cc = dict(c)
            cc["type"] = "comment"
            out.append(cc)
        out.sort(key=lambda x: x.get("change_seq", 0))
        return out
    finally:
        conn.close()


def current_change_seq(slide):
    """返回某切片当前的全局 change_seq 水位（无则 0）。"""
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                cur.execute(
                    "SELECT COALESCE(MAX(seq), 0)::int AS s FROM change_log "
                    "WHERE slide=%s", (slide,))
                return int(cur.fetchone()["s"])
    finally:
        conn.close()


def set_roi_shared(token, index, shared, expected_revision=None):
    """设置该 token 下第 index 条 ROI 的 shared 字段（跳过 tombstone）。

    expected_revision（CAS）：提供且与当前 revision 不符 → 抛 RevisionConflict。
    """
    shared_b = bool(shared)
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                cur.execute(
                    "SELECT id, data FROM rois WHERE token=%s AND NOT deleted "
                    "ORDER BY insert_seq", (token,))
                same = cur.fetchall()
                if index < 0 or index >= len(same):
                    return False
                rid = same[index]["id"]
                roi = dict(same[index]["data"])
                _check_cas(roi, expected_revision)
                roi["shared"] = shared_b
                _update_roi_row(cur, rid, roi)
                return True
    finally:
        conn.close()


def review_roi(token, index, action):
    """Stage 3c-1：AI 标注审核（接受/驳回）。仅 source=ai 可审；否则 ValueError。

    成功返回更新后的 roi dict（含 index/review_status/revision）；token/index
    无效返回 False。bump revision + updated_at（不 bump change_seq）。
    """
    if action not in ("accept", "reject"):
        raise ValueError("action 需为 accept 或 reject")
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                cur.execute(
                    "SELECT id, data FROM rois WHERE token=%s AND NOT deleted "
                    "ORDER BY insert_seq", (token,))
                same = cur.fetchall()
                if index < 0 or index >= len(same):
                    return False
                rid = same[index]["id"]
                roi = dict(same[index]["data"])
                if roi.get("source") != "ai":
                    raise ValueError("仅 AI 标注可审核")
                roi["review_status"] = "accepted" if action == "accept" else "rejected"
                roi["revision"] = int(roi.get("revision") or 1) + 1
                roi["updated_at"] = time.time()
                _update_roi_row(cur, rid, roi)
                cur.execute(
                    "SELECT data FROM rois WHERE token=%s AND NOT deleted "
                    "ORDER BY insert_seq", (token,))
                all_rows = cur.fetchall()
                idx = next((i for i, row in enumerate(all_rows)
                            if row["data"].get("annotation_id") ==
                            roi.get("annotation_id")), 0)
                out = _roi_out(roi)
                out["index"] = idx
                out["shared"] = _roi_shared_compat(roi)
                return out
    finally:
        conn.close()


def roi_count_by_token():
    """返回 {token: count} 计数表（跳过 tombstone）。"""
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                cur.execute(
                    "SELECT token, count(*) AS n FROM rois WHERE NOT deleted "
                    "GROUP BY token")
                rows = cur.fetchall()
        return {row["token"]: int(row["n"]) for row in rows}
    finally:
        conn.close()


def list_shared_rois_for_slides(slides):
    """返回 shared 为真且 slide ∈ slides 的标注列表（跳过 tombstone）。"""
    if not slides:
        return []
    slide_set = set(slides)
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                cur.execute(
                    "SELECT data FROM rois WHERE NOT deleted ORDER BY insert_seq")
                rows = cur.fetchall()
        from collections import defaultdict
        counters = defaultdict(int)
        out = []
        for row in rows:
            r = row["data"]
            idx = counters[r["token"]]
            counters[r["token"]] += 1
            if r.get("slide") not in slide_set:
                continue
            if not _roi_shared_compat(r):
                continue
            rr = dict(r)
            rr["index"] = idx
            rr["shared"] = True
            rr.setdefault("type", "rect")
            rr["note"] = r.get("note", "")
            out.append(rr)
        out.sort(key=lambda x: x.get("ts", 0), reverse=True)
        return out
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# 样本元数据 + 稳定 slide 身份（slides 表，name=legacy_filename）
# --------------------------------------------------------------------------- #
def _new_slide_id() -> str:
    return "sld_" + secrets.token_urlsafe(9)  # 12 位 urlsafe


def set_slide_meta(name, alias=None, note=None, owner_user_id=None, public=None,
                   requester_role=None):
    """设置/更新某切片的别名与备注；首次出现 name 时生成稳定 slide_id。

    语义与 json 一致，额外：name（legacy_filename）首次出现 → 新建 slides 行并
    生成稳定 slide_id；同名已存在 → 仅更新 alias/note/owner/public，slide_id 不动。
    """
    _reject_guest_write(requester_role)
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                cur.execute("SELECT slide_id, owner_user_id FROM slides "
                            "WHERE legacy_filename=%s", (name,))
                row = cur.fetchone()
                if row is None:
                    slide_id = _new_slide_id()
                    cur.execute(
                        "INSERT INTO slides (slide_id, legacy_filename) "
                        "VALUES (%s,%s)", (slide_id, name))
                    cur_owner = None
                else:
                    slide_id = row["slide_id"]
                    cur_owner = row["owner_user_id"]
                sets = []
                params = []
                if alias is not None:
                    a = alias.strip() if isinstance(alias, str) else ""
                    sets.append("alias=%s")
                    params.append(a)
                if note is not None:
                    n = note.strip() if isinstance(note, str) else ""
                    sets.append("note=%s")
                    params.append(n)
                if public is not None:
                    sets.append("public=%s")
                    params.append(bool(public))
                if cur_owner is None:
                    sets.append("owner_user_id=%s")
                    params.append(owner_user_id or _OWNER_USER_ID or None)
                if sets:
                    sets.append("updated_at=now()")
                    params.append(slide_id)
                    cur.execute(
                        "UPDATE slides SET " + ", ".join(sets) +
                        " WHERE slide_id=%s", params)
                cur.execute(
                    "SELECT alias, note, owner_user_id, public FROM slides "
                    "WHERE slide_id=%s", (slide_id,))
                r2 = cur.fetchone()
                return {
                    "alias": r2["alias"] or "",
                    "note": r2["note"] or "",
                    "owner_user_id": r2["owner_user_id"],
                    "public": bool(r2["public"]),
                }
    finally:
        conn.close()


def get_slide_meta(name):
    """返回某切片的 {alias, note}（无则空 dict，保证字段存在为空串）。"""
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                cur.execute("SELECT alias, note FROM slides WHERE legacy_filename=%s",
                            (name,))
                row = cur.fetchone()
        if row is None:
            return {"alias": "", "note": ""}
        return {"alias": row["alias"] or "", "note": row["note"] or ""}
    finally:
        conn.close()


def get_slide_meta_full(name):
    """返回某切片的完整 meta（含 owner_user_id / public）。"""
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                cur.execute(
                    "SELECT alias, note, owner_user_id, public FROM slides "
                    "WHERE legacy_filename=%s", (name,))
                row = cur.fetchone()
        if row is None:
            return {"alias": "", "note": "", "owner_user_id": None, "public": False}
        return {
            "alias": row["alias"] or "",
            "note": row["note"] or "",
            "owner_user_id": row["owner_user_id"],
            "public": bool(row["public"]),
        }
    finally:
        conn.close()


def get_all_slide_meta_full():
    """返回全量 {name: {alias, note, owner_user_id, public}}。"""
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                cur.execute(
                    "SELECT legacy_filename, alias, note, owner_user_id, public "
                    "FROM slides WHERE legacy_filename IS NOT NULL ORDER BY "
                    "legacy_filename")
                rows = cur.fetchall()
        out = {}
        for row in rows:
            out[row["legacy_filename"]] = {
                "alias": row["alias"] or "",
                "note": row["note"] or "",
                "owner_user_id": row["owner_user_id"],
                "public": bool(row["public"]),
            }
        return out
    finally:
        conn.close()


def get_all_slide_meta():
    """返回全量 {name: {alias, note}}。"""
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                cur.execute(
                    "SELECT legacy_filename, alias, note FROM slides "
                    "WHERE legacy_filename IS NOT NULL ORDER BY legacy_filename")
                rows = cur.fetchall()
        return {row["legacy_filename"]: {"alias": row["alias"] or "",
                                          "note": row["note"] or ""}
                for row in rows}
    finally:
        conn.close()


def get_slide_id(name):
    """返回某 legacy_filename 对应的稳定 slide_id；无则 None。"""
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                cur.execute("SELECT slide_id FROM slides WHERE legacy_filename=%s",
                            (name,))
                row = cur.fetchone()
        return row["slide_id"] if row else None
    finally:
        conn.close()


def resolve_slide_ref(ref):
    """把 name（或已是稳定 id 的 slide_id）解析为稳定 slide_id；无则 None。"""
    if not ref:
        return None
    if isinstance(ref, str) and ref.startswith("sld_"):
        return ref
    return get_slide_id(ref)


def record_slide_asset(slide_id, legacy_revision):
    """记录切片内容资产 revision。返回 asset_id。

    legacy_revision 封装旧 mtime:size 指纹；content_sha256 由 3b-3 迁移工具填。
    """
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                asset_id = "ast_" + secrets.token_urlsafe(9)
                cur.execute(
                    "INSERT INTO slide_assets (asset_id, slide_id, legacy_revision) "
                    "VALUES (%s,%s,%s) RETURNING asset_id",
                    (asset_id, slide_id, legacy_revision))
                return cur.fetchone()["asset_id"]
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# 项目（projects）
# --------------------------------------------------------------------------- #
def _dedupe(slides):
    seen = set()
    out = []
    for s in slides or []:
        if isinstance(s, str) and s not in seen:
            seen.add(s)
            out.append(s)
    return out


def create_project(name, note="", slides=None, owner_user_id=None, requester_role=None):
    """创建项目。pid=secrets.token_urlsafe(10)。返回新建项目 dict（含 pid）。"""
    _reject_guest_write(requester_role)
    pid = "prj_" + secrets.token_urlsafe(10)
    now = time.time()
    uniq = _dedupe(slides)
    proj = {
        "name": str(name or "").strip() or "未命名项目",
        "note": str(note or ""),
        "slides": uniq,
        "created_at": now,
        "owner_user_id": owner_user_id or _OWNER_USER_ID or None,
        "archived": False,  # Stage 3c-2：归档纯只读开关，默认未归档
    }
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                cur.execute(
                    "INSERT INTO projects (project_id, name, note, owner_user_id, "
                    "created_at) VALUES (%s,%s,%s,%s, to_timestamp(%s))",
                    (pid, proj["name"], proj["note"], proj["owner_user_id"], now))
                for i, s in enumerate(uniq):
                    cur.execute(
                        "INSERT INTO project_slides (project_id, slide, position) "
                        "VALUES (%s,%s,%s)", (pid, s, i))
        out = dict(proj)
        out["pid"] = pid
        return out
    finally:
        conn.close()


_PROJ_SEL = (
    "project_id, name, note, owner_user_id, archived, "
    "extract(epoch from created_at)::float8 AS created_at"
)


def _fetch_project(cur, pid):
    cur.execute("SELECT " + _PROJ_SEL + " FROM projects WHERE project_id=%s", (pid,))
    row = cur.fetchone()
    if row is None:
        return None
    cur.execute("SELECT slide FROM project_slides WHERE project_id=%s "
                "ORDER BY position", (pid,))
    slides = [r["slide"] for r in cur.fetchall()]
    d = dict(row)
    d["pid"] = pid
    d["slides"] = slides
    return d


def list_projects():
    """返回全部项目列表，每项附加 pid、slide_count；按 created_at 倒序。"""
    conn = _connect()
    try:
        items = []
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                cur.execute("SELECT " + _PROJ_SEL +
                            " FROM projects ORDER BY created_at DESC, project_id")
                rows = cur.fetchall()
                for row in rows:
                    d = _fetch_project(cur, row["project_id"])
                    if d is not None:
                        d["slide_count"] = len(d["slides"])
                        items.append(d)
        return items
    finally:
        conn.close()


def get_project(pid):
    """返回单个项目 dict（附加 pid）；不存在返回 None。"""
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                return _fetch_project(cur, pid)
    finally:
        conn.close()


def update_project(pid, *, name=None, note=None, slides=None):
    """更新项目字段（仅更新非 None 字段）。返回更新后的 dict；不存在返回 None。"""
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                cur.execute("SELECT name FROM projects WHERE project_id=%s", (pid,))
                prow = cur.fetchone()
                if prow is None:
                    return None
                if name is not None:
                    cur.execute("UPDATE projects SET name=%s WHERE project_id=%s",
                                (str(name).strip() or prow["name"] or "未命名项目",
                                 pid))
                if note is not None:
                    cur.execute("UPDATE projects SET note=%s WHERE project_id=%s",
                                (str(note), pid))
                if slides is not None:
                    uniq = _dedupe(slides)
                    cur.execute("DELETE FROM project_slides WHERE project_id=%s", (pid,))
                    for i, s in enumerate(uniq):
                        cur.execute(
                            "INSERT INTO project_slides (project_id, slide, position) "
                            "VALUES (%s,%s,%s)", (pid, s, i))
                return _fetch_project(cur, pid)
    finally:
        conn.close()


def add_slides_to_project(pid, slides):
    """向项目追加切片（去重保序）。返回更新后的 dict；不存在返回 None。"""
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                cur.execute("SELECT project_id FROM projects WHERE project_id=%s", (pid,))
                if cur.fetchone() is None:
                    return None
                cur.execute("SELECT slide FROM project_slides WHERE project_id=%s "
                            "ORDER BY position", (pid,))
                existing = [r["slide"] for r in cur.fetchall()]
                seen = set(existing)
                pos = len(existing)
                for s in slides or []:
                    if isinstance(s, str) and s not in seen:
                        seen.add(s)
                        cur.execute(
                            "INSERT INTO project_slides (project_id, slide, position) "
                            "VALUES (%s,%s,%s)", (pid, s, pos))
                        existing.append(s)
                        pos += 1
                return _fetch_project(cur, pid)
    finally:
        conn.close()


def remove_slide_from_project(pid, slide):
    """从项目移除某切片。返回更新后的 dict；不存在或无该切片返回 None。"""
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                cur.execute("SELECT project_id FROM projects WHERE project_id=%s", (pid,))
                if cur.fetchone() is None:
                    return None
                cur.execute(
                    "DELETE FROM project_slides WHERE project_id=%s AND slide=%s "
                    "RETURNING 1", (pid, slide))
                if cur.fetchone() is None:
                    return None
                return _fetch_project(cur, pid)
    finally:
        conn.close()


def delete_project(pid):
    """删除项目（仅删项目记录，不动切片文件）。返回是否删除成功。"""
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                cur.execute("DELETE FROM projects WHERE project_id=%s RETURNING 1", (pid,))
                return cur.fetchone() is not None
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# 标注（annotations）汇总
# --------------------------------------------------------------------------- #
def annotations_by_slide():
    """把全部 rois 按 slide 分组聚合（结构与 json 完全一致）。"""
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                cur.execute(
                    "SELECT data FROM rois WHERE NOT deleted ORDER BY insert_seq")
                rows = cur.fetchall()
        from collections import defaultdict
        counters = defaultdict(int)
        by_slide = {}
        for row in rows:
            r = row["data"]
            slide = r.get("slide")
            lbl = _norm_label(r.get("label"))
            grp_map = by_slide.setdefault(slide, {})
            grp = grp_map.get(lbl)
            if grp is None:
                grp = {"label": lbl, "count": 0, "items": []}
                grp_map[lbl] = grp
            grp["count"] += 1
            tok = r.get("token")
            idx = counters[tok]
            counters[tok] += 1
            item = {
                "index": idx,
                "token": tok,
                "slide": r.get("slide"),
                "type": r.get("type", "rect"),
                "x": r.get("x"),
                "y": r.get("y"),
                "size_mm": r.get("size_mm"),
                "side_px": r.get("side_px"),
                "ts": r.get("ts"),
                "shared": _roi_shared_compat(r),
                "note": r.get("note", ""),
                "annotation_id": r.get("annotation_id"),
                "source": r.get("source", "human"),
                "created_by_session_id": r.get("created_by_session_id", ""),
                "change_seq": r.get("change_seq"),
                "revision": r.get("revision", 1),
                "review_status": r.get("review_status", "none"),
                "visitor": (r.get("visitor") or "")[:8],
            }
            for k in ("x1", "y1", "x2", "y2", "points"):
                if k in r:
                    item[k] = r[k]
            grp["items"].append(item)
        result = {}
        for slide, grp_map in by_slide.items():
            result[slide] = list(grp_map.values())
        return result
    finally:
        conn.close()


def annotations_by_project(pid=None):
    """与 annotations_by_slide 同结构，但可选按项目内的 slides 过滤。"""
    by_slide = annotations_by_slide()
    if pid is None:
        return by_slide
    proj = get_project(pid)
    project_slides = set(proj.get("slides", [])) if proj else set()
    return {
        slide: groups
        for slide, groups in by_slide.items()
        if slide in project_slides
    }


# --------------------------------------------------------------------------- #
# 评论线程（comments）—— Stage 3c-1（docs §5.3）
#
# comments 表存权威 dict 在 data JSONB（同 rois 语义），离散列镜像供过滤/索引。
# 增删 bump change_log（op=comment_add/comment_delete），list_changes 以 type=comment
# 返回。语义与 json 完全一致。
# --------------------------------------------------------------------------- #
def _insert_comment(cur, cmt: dict, cid: str):
    """插入一条 comment：data 存权威 dict，离散列镜像。"""
    now = cmt.get("updated_at") or cmt.get("created_at") or time.time()
    cur.execute(
        "INSERT INTO comments "
        "(comment_id, annotation_id, slide, token, author_user_id, author_label, "
        " body, parent_id, resolved, deleted, created_at, updated_at, data) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s, to_timestamp(%s), to_timestamp(%s), %s)",
        (
            cid, cmt.get("annotation_id", ""), cmt.get("slide", ""),
            cmt.get("token", ""), cmt.get("author_user_id"),
            cmt.get("author_label", "访客"), cmt.get("body", ""),
            cmt.get("parent_id"), bool(cmt.get("resolved", False)),
            bool(cmt.get("deleted", False)), cmt.get("created_at", now), now,
            psycopg.types.json.Jsonb(cmt),
        ),
    )


def _update_comment_row(cur, cid: str, cmt: dict):
    """更新一条 comment（data + 离散镜像列）。"""
    now = cmt.get("updated_at") or time.time()
    cur.execute(
        "UPDATE comments SET resolved=%s, deleted=%s, updated_at=to_timestamp(%s), "
        "data=%s WHERE comment_id=%s",
        (bool(cmt.get("resolved", False)), bool(cmt.get("deleted", False)), now,
         psycopg.types.json.Jsonb(cmt), cid),
    )


def add_comment(annotation_id, slide, token, body, author_user_id=None,
                author_label="", parent_id=None, requester_role=None):
    """新增评论；返回 comment dict（含 comment_id/change_seq）。语义同 json。"""
    body_clean = _clean_comment_body(body)
    if not body_clean:
        raise ValueError("评论正文不能为空")
    now = time.time()
    cid = "cmt_" + uuid.uuid4().hex
    cmt = {
        "comment_id": cid,
        "annotation_id": annotation_id or "",
        "slide": slide or "",
        "token": token or "",
        "author_user_id": author_user_id or None,
        "author_label": (author_label or "").strip()[:80] or "访客",
        "body": body_clean,
        "parent_id": parent_id or None,
        "resolved": False,
        "deleted": False,
        "created_at": now,
        "updated_at": now,
    }
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                cmt["change_seq"] = _bump_change_seq(
                    cur, slide, token, cid, "comment_add")
                _insert_comment(cur, cmt, cid)
                return dict(cmt)
    finally:
        conn.close()


def list_comments(annotation_id=None, slide=None):
    """返回评论列表（跳过软删）。可按 annotation_id / slide 过滤。按 created_at 升序。"""
    clauses = ["NOT deleted"]
    params = []
    if annotation_id is not None:
        clauses.append("annotation_id=%s")
        params.append(annotation_id)
    if slide is not None:
        clauses.append("slide=%s")
        params.append(slide)
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                cur.execute(
                    "SELECT data FROM comments WHERE " + " AND ".join(clauses) +
                    " ORDER BY created_at", params)
                rows = cur.fetchall()
        return [dict(r["data"]) for r in rows]
    finally:
        conn.close()


def resolve_comment(comment_id, resolved=True):
    """设置评论 resolved 状态；返回是否成功。"""
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                cur.execute(
                    "SELECT data FROM comments WHERE comment_id=%s AND NOT deleted",
                    (comment_id,))
                row = cur.fetchone()
                if row is None:
                    return False
                cmt = dict(row["data"])
                cmt["resolved"] = bool(resolved)
                cmt["updated_at"] = time.time()
                _update_comment_row(cur, comment_id, cmt)
                return True
    finally:
        conn.close()


def delete_comment(comment_id):
    """软删评论（deleted=true + bump change_seq）；返回是否成功。"""
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                cur.execute(
                    "SELECT data FROM comments WHERE comment_id=%s AND NOT deleted",
                    (comment_id,))
                row = cur.fetchone()
                if row is None:
                    return False
                cmt = dict(row["data"])
                cmt["deleted"] = True
                cmt["updated_at"] = time.time()
                cmt["change_seq"] = _bump_change_seq(
                    cur, cmt.get("slide"), cmt.get("token"), comment_id,
                    "comment_delete")
                _update_comment_row(cur, comment_id, cmt)
                return True
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# dual 后端 result-replay 镜像（Stage 3b-2）
#
# dual（expand）形态下 json 为权威、pg 为影子副本。凡 json **内部生成身份**的写
# （token/annotation_id/pid/grant_id 等），直接同参调 pg 会让 pg 生成不同身份值，
# 两库从创建起就发散。故这里提供一组带 `_` 前缀的镜像函数（不进 dispatcher 公共
# 名）：接收 json 返回的**权威 dict**（其中含 json 生成的 token/annotation_id/pid/
# grant_id），按这些身份值原样 upsert 进 pg，保证身份逐项一致。_effect_key 幂等只
# 保证重试不重复，跨库身份一致靠这里。
# --------------------------------------------------------------------------- #
def _mirror_share(ret, *a, **k):
    """把 json create_share 返回的权威 share dict upsert 进 pg（按 token）。"""
    share = ret if isinstance(ret, dict) else None
    if not share or not share.get("token"):
        return
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                cur.execute(
                    "INSERT INTO shares "
                    "(token, slides, permissions, roi_sizes, expires_at, revoked, "
                    " creator_user_id) "
                    "VALUES (%s,%s,%s,%s, to_timestamp(%s), %s, %s) "
                    "ON CONFLICT (token) DO UPDATE SET "
                    " slides=EXCLUDED.slides, permissions=EXCLUDED.permissions, "
                    " roi_sizes=EXCLUDED.roi_sizes, expires_at=EXCLUDED.expires_at, "
                    " revoked=EXCLUDED.revoked, creator_user_id=EXCLUDED.creator_user_id",
                    (share["token"],
                     psycopg.types.json.Jsonb(share.get("slides") or []),
                     psycopg.types.json.Jsonb(share.get("permissions") or []),
                     psycopg.types.json.Jsonb(share.get("roi_sizes") or []),
                     share.get("expires_at"), bool(share.get("revoked", False)),
                     share.get("creator_user_id")),
                )
    finally:
        conn.close()


def _mirror_share_revoke(ret, token, *a, **k):
    """revoke_share：token 为调用方入参，按 token 撤销 pg 行。"""
    if not token:
        return
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                cur.execute("UPDATE shares SET revoked=TRUE WHERE token=%s", (token,))
    finally:
        conn.close()


def _mirror_grant(ret, *a, **k):
    """把 json claim_share 返回的权威 grant dict upsert 进 pg（按 grant_id）。"""
    g = ret if isinstance(ret, dict) else None
    if not g or not g.get("grant_id"):
        return
    active = g.get("revoked_at") is None
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                cur.execute(
                    "INSERT INTO grants "
                    "(id, token, user_id, permissions, claimed_at, active) "
                    "VALUES (%s,%s,%s,%s, to_timestamp(%s), %s) "
                    "ON CONFLICT (id) DO UPDATE SET "
                    " token=EXCLUDED.token, user_id=EXCLUDED.user_id, "
                    " permissions=EXCLUDED.permissions, "
                    " claimed_at=EXCLUDED.claimed_at, active=EXCLUDED.active",
                    (g["grant_id"], g.get("share_token"), g.get("user_id"),
                     psycopg.types.json.Jsonb(g.get("permissions") or []),
                     g.get("claimed_at"), active),
                )
    finally:
        conn.close()


def _mirror_roi(ret, *a, **k):
    """把 json add_roi/update_roi 返回的权威 roi dict upsert 进 pg（按 annotation_id）。

    data 存权威负载（剥离 index 兼容字段），离散列同步；insert_seq 按全局插入序
    （max+1）保持与 json 数组顺序一致。
    """
    roi = ret if isinstance(ret, dict) else None
    if not roi or not roi.get("annotation_id"):
        return
    aid = roi["annotation_id"]
    clean = dict(roi)
    clean.pop("index", None)
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                cur.execute("SELECT id FROM rois WHERE annotation_id=%s", (aid,))
                row = cur.fetchone()
                if row is not None:
                    _update_roi_row(cur, row["id"], clean)
                    return
                cur.execute("SELECT COALESCE(MAX(insert_seq),0)+1 AS n FROM rois")
                seq = int(cur.fetchone()["n"])
                rid = "roi_" + secrets.token_urlsafe(10)
                now = clean.get("updated_at") or clean.get("ts") or time.time()
                cur.execute(
                    "INSERT INTO rois "
                    "(id, token, slide, annotation_id, label, type, geom, size_mm, "
                    " shared, note, deleted, owner_user_id, created_at, updated_at, "
                    " data, insert_seq) "
                    "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s, to_timestamp(%s), "
                    "to_timestamp(%s), %s, %s)",
                    (rid, clean.get("token"), clean.get("slide"), aid,
                     clean.get("label", ""), clean.get("type", "rect"),
                     psycopg.types.json.Jsonb(_geom_of(clean)),
                     clean.get("size_mm", 0.0), bool(clean.get("shared", False)),
                     clean.get("note", ""), bool(clean.get("deleted", False)),
                     clean.get("owner_user_id"), now, now,
                     psycopg.types.json.Jsonb(clean), seq),
                )
    finally:
        conn.close()


def _mirror_roi_delete(ret, *a, **k):
    """delete_roi/delete_roi_by_annotation_id：按 annotation_id 置 pg tombstone。

    delete_roi 返回 (bool, annotation_id)；delete_roi_by_annotation_id 的 annotation_id
    在调用入参里。按 id 而非 index 定位，双库 insert_seq 即便有偏差也不受影响。
    """
    aid = None
    if isinstance(ret, tuple) and len(ret) >= 2 and ret[1]:
        aid = ret[1]
    if not aid and a:
        aid = a[0]  # delete_roi_by_annotation_id(annotation_id, ...)
    if not aid:
        return
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                cur.execute(
                    "SELECT id, data FROM rois WHERE annotation_id=%s AND NOT deleted",
                    (aid,))
                row = cur.fetchone()
                if row is None:
                    return
                roi = dict(row["data"])
                roi["deleted"] = True
                roi["deleted_at"] = time.time()
                roi["revision"] = int(roi.get("revision") or 1) + 1
                roi["change_seq"] = _bump_change_seq(
                    cur, roi.get("slide"), roi.get("token"), aid, "delete")
                roi["updated_at"] = roi["deleted_at"]
                _update_roi_row(cur, row["id"], roi)
    finally:
        conn.close()


def _mirror_roi_shared(ret, token, index, shared, *a, **k):
    """set_roi_shared：按 token 内插入序（与 json 一致）定位并设 shared。"""
    shared_b = bool(shared)
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                cur.execute(
                    "SELECT id, data FROM rois WHERE token=%s AND NOT deleted "
                    "ORDER BY insert_seq", (token,))
                same = cur.fetchall()
                if index < 0 or index >= len(same):
                    return
                roi = dict(same[index]["data"])
                roi["shared"] = shared_b
                _update_roi_row(cur, same[index]["id"], roi)
    finally:
        conn.close()


def _mirror_slide_meta(ret, name, *a, **k):
    """set_slide_meta：pg 按 legacy_filename 幂等建/更新 slides 行。

    stable slide_id 仅 pg 侧概念（json 无），join key 是调用方入参 legacy_filename，
    同参重放即保持一致（set_slide_meta 本身按 legacy_filename 幂等）。
    """
    alias = k.get("alias")
    note = k.get("note")
    owner_user_id = k.get("owner_user_id")
    public = k.get("public")
    requester_role = k.get("requester_role")
    set_slide_meta(name, alias=alias, note=note, owner_user_id=owner_user_id,
                   public=public, requester_role=requester_role)


def _mirror_project(ret, *a, **k):
    """把 json create_project/update_project/add_slides_to_project/remove_slide_from_project
    返回的权威 project dict upsert 进 pg（按 pid，整体替换 project_slides）。"""
    proj = ret if isinstance(ret, dict) else None
    if not proj or not proj.get("pid"):
        return
    pid = proj["pid"]
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                cur.execute("SELECT project_id FROM projects WHERE project_id=%s", (pid,))
                if cur.fetchone() is None:
                    cur.execute(
                        "INSERT INTO projects (project_id, name, note, owner_user_id, "
                        "created_at, archived) VALUES (%s,%s,%s,%s, to_timestamp(%s), %s)",
                        (pid, proj.get("name", ""), proj.get("note", ""),
                         proj.get("owner_user_id"), proj.get("created_at"),
                         bool(proj.get("archived", False))))
                else:
                    cur.execute(
                        "UPDATE projects SET name=%s, note=%s, owner_user_id=%s, "
                        "archived=%s WHERE project_id=%s",
                        (proj.get("name", ""), proj.get("note", ""),
                         proj.get("owner_user_id"), bool(proj.get("archived", False)),
                         pid))
                cur.execute("DELETE FROM project_slides WHERE project_id=%s", (pid,))
                for i, s in enumerate(proj.get("slides") or []):
                    cur.execute(
                        "INSERT INTO project_slides (project_id, slide, position) "
                        "VALUES (%s,%s,%s)", (pid, s, i))
    finally:
        conn.close()


def _mirror_project_delete(ret, pid, *a, **k):
    """delete_project：按 pid 删除 pg 项目。"""
    if not pid:
        return
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                cur.execute("DELETE FROM projects WHERE project_id=%s", (pid,))
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# 评论 / 审核 镜像（Stage 3c-1）
# --------------------------------------------------------------------------- #
def _mirror_comment(ret, *a, **k):
    """把 json add_comment 返回的权威 comment dict upsert 进 pg（按 comment_id）。

    data 存权威负载；comment_id 由 json 生成，跨库身份一致靠这里。
    """
    cmt = ret if isinstance(ret, dict) else None
    if not cmt or not cmt.get("comment_id"):
        return
    cid = cmt["comment_id"]
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                cur.execute("SELECT comment_id FROM comments WHERE comment_id=%s", (cid,))
                if cur.fetchone() is not None:
                    _update_comment_row(cur, cid, cmt)
                else:
                    _insert_comment(cur, cmt, cid)
    finally:
        conn.close()


def _mirror_comment_delete(ret, comment_id, *a, **k):
    """delete_comment：comment_id 为调用方入参，同参软删 pg 评论。

    delete_comment 返回 bool；comment_id 在入参首位。pg 侧也 bump change_seq
    （op=comment_delete），与 json 一致。
    """
    if not comment_id or not ret:
        return
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                cur.execute(
                    "SELECT data FROM comments WHERE comment_id=%s AND NOT deleted",
                    (comment_id,))
                row = cur.fetchone()
                if row is None:
                    return
                cmt = dict(row["data"])
                cmt["deleted"] = True
                cmt["updated_at"] = time.time()
                cmt["change_seq"] = _bump_change_seq(
                    cur, cmt.get("slide"), cmt.get("token"), comment_id,
                    "comment_delete")
                _update_comment_row(cur, comment_id, cmt)
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# 审计日志（audit_events）—— Stage 3c-2（docs §5.3/§6.4）
#
# 语义与 json 完全一致：协作操作日志，best-effort（record_audit 内部吞掉写失败），
# 绝不写密钥/明文密码。pg 侧存 audit_events 表。
# --------------------------------------------------------------------------- #
AUDIT_MAX_EVENTS = 5000


def record_audit(action, actor_user_id=None, actor_role=None, target_type=None,
                 target_id=None, slide=None, detail=None, ts=None):
    """best-effort 追加一条审计事件；写失败吞掉返回 False，绝不抛异常。

    与 json 的 record_audit 同签名同语义（dispatcher 在 dual 下同参重放到 pg，
    各自生成独立 event_id，跨库 id 无需一致）。detail 绝不存 api_key/明文密码。
    """
    ev_id = "aud_" + secrets.token_hex(16)
    detail = dict(detail) if isinstance(detail, dict) else {}
    try:
        conn = _connect()
        try:
            with pg_store.transaction(conn) as c:
                with c.cursor() as cur:
                    cur.execute(
                        "INSERT INTO audit_events "
                        "(event_id, ts, actor_user_id, actor_role, action, "
                        " target_type, target_id, slide, detail) "
                        "VALUES (%s, to_timestamp(%s), %s, %s, %s, %s, %s, %s, %s)",
                        (ev_id, ts if ts is not None else time.time(),
                         actor_user_id or None, actor_role or "",
                         str(action or ""), target_type or None, target_id or None,
                         slide or None, psycopg.types.json.Jsonb(detail)),
                    )
            return True
        finally:
            conn.close()
    except Exception:
        return False


def list_audit(limit=50, offset=0, action=None):
    """返回审计事件（最新在前），支持分页与 action 过滤。owner-only 消费（app.py 鉴权）。"""
    limit = max(0, int(limit if limit is not None else 50))
    offset = max(0, int(offset if offset is not None else 0))
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                if action:
                    cur.execute(
                        "SELECT event_id, actor_user_id, actor_role, action, "
                        " target_type, target_id, slide, detail, "
                        " extract(epoch from ts)::float8 AS ts "
                        "FROM audit_events WHERE action=%s "
                        "ORDER BY ts DESC, event_id DESC LIMIT %s OFFSET %s",
                        (action, limit, offset))
                else:
                    cur.execute(
                        "SELECT event_id, actor_user_id, actor_role, action, "
                        " target_type, target_id, slide, detail, "
                        " extract(epoch from ts)::float8 AS ts "
                        "FROM audit_events "
                        "ORDER BY ts DESC, event_id DESC LIMIT %s OFFSET %s",
                        (limit, offset))
                rows = cur.fetchall()
        out = []
        for r in rows:
            ev = {
                "id": r["event_id"],
                "ts": r["ts"],
                "actor_user_id": r["actor_user_id"],
                "actor_role": r["actor_role"],
                "action": r["action"],
                "target_type": r["target_type"],
                "target_id": r["target_id"],
                "slide": r["slide"],
                "detail": r["detail"] or {},
            }
            out.append(ev)
        return out
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# 项目归档只读开关（docs §v1.5）
# --------------------------------------------------------------------------- #
def set_project_archived(pid, archived):
    """设置项目 archived 纯只读开关。返回更新后的项目 dict；不存在返回 None。"""
    archived_b = bool(archived)
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                cur.execute("UPDATE projects SET archived=%s WHERE project_id=%s",
                            (archived_b, pid))
                if cur.rowcount == 0:
                    return None
                return _fetch_project(cur, pid)
    finally:
        conn.close()


def archived_slide_names():
    """返回属于任意 archived 项目的切片名集合（归档只读判定用）。"""
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                cur.execute(
                    "SELECT ps.slide FROM project_slides ps "
                    "JOIN projects p ON p.project_id=ps.project_id "
                    "WHERE p.archived=TRUE")
                return {r["slide"] for r in cur.fetchall()}
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# 插件安装凭证（plugin_installations）—— Stage 4-1a（docs §7.6 / §6.2）
#
# 语义与 json 实现完全一致（secret 只存 sha256 hash；_installation_out 剥离
# hash 的导出形状直接复用 json 侧私有助手，避免两份漂移）。表结构见
# migrations/0005_plugin.sql。
# --------------------------------------------------------------------------- #
def _fetch_installation(cur, installation_id):
    cur.execute(
        "SELECT installation_id, plugin_id, version, enabled, secret_hash, "
        " extract(epoch from created_at)::float8 AS created_at, "
        " extract(epoch from disabled_at)::float8 AS disabled_at "
        "FROM plugin_installations WHERE installation_id=%s",
        (installation_id,))
    return cur.fetchone()


def create_plugin_installation(plugin_id, version="", secret=None):
    """创建插件安装行，返回 {**installation, "secret": 明文}（仅此一次）。"""
    if not isinstance(plugin_id, str) or not plugin_id.strip():
        raise ValueError("plugin_id 不能为空")
    plaintext = secret if isinstance(secret, str) and secret else (
        "pin_" + secrets.token_urlsafe(32))
    installation_id = "pin_" + secrets.token_urlsafe(12)
    now = time.time()
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                cur.execute(
                    "INSERT INTO plugin_installations "
                    "(installation_id, plugin_id, version, enabled, secret_hash, "
                    " created_at) VALUES (%s,%s,%s,TRUE,%s, to_timestamp(%s))",
                    (installation_id, plugin_id.strip(), version or "",
                     _hash_installation_secret(plaintext), now))
                row = _fetch_installation(cur, installation_id)
        out = _installation_out(dict(row))
        out["secret"] = plaintext
        return out
    finally:
        conn.close()


def rotate_installation_secret(installation_id, secret=None):
    """轮换安装凭证：旧 secret 立即失效，返回带新明文的一次性 dict；无则 None。"""
    plaintext = secret if isinstance(secret, str) and secret else (
        "pin_" + secrets.token_urlsafe(32))
    new_hash = _hash_installation_secret(plaintext)
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                cur.execute(
                    "UPDATE plugin_installations SET secret_hash=%s "
                    "WHERE installation_id=%s",
                    (new_hash, installation_id))
                if cur.rowcount == 0:
                    return None
                row = _fetch_installation(cur, installation_id)
        out = _installation_out(dict(row))
        out["secret"] = plaintext
        return out
    finally:
        conn.close()


def get_plugin_installation(installation_id):
    """按 installation_id 取安装行（不含 secret_hash）；无则 None。"""
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                row = _fetch_installation(cur, installation_id)
        return _installation_out(dict(row)) if row else None
    finally:
        conn.close()


def verify_installation_secret(installation_id, secret):
    """校验安装凭证（常数时间比较）；行不存在或 hash 不一致返回 False。"""
    if not isinstance(secret, str) or not secret:
        return False
    candidate = _hash_installation_secret(secret)
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                cur.execute(
                    "SELECT secret_hash FROM plugin_installations "
                    "WHERE installation_id=%s", (installation_id,))
                row = cur.fetchone()
        if row is None:
            return False
        return hmac.compare_digest(str(row["secret_hash"] or ""), candidate)
    finally:
        conn.close()


def set_installation_enabled(installation_id, enabled):
    """启/禁安装（禁用即撤销该安装全部在途 JWT）；不存在返回 None。"""
    enabled_b = bool(enabled)
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                if enabled_b:
                    cur.execute(
                        "UPDATE plugin_installations SET enabled=TRUE, "
                        "disabled_at=NULL WHERE installation_id=%s",
                        (installation_id,))
                else:
                    cur.execute(
                        "UPDATE plugin_installations SET enabled=FALSE, "
                        "disabled_at=now() WHERE installation_id=%s",
                        (installation_id,))
                if cur.rowcount == 0:
                    return None
                row = _fetch_installation(cur, installation_id)
        return _installation_out(dict(row))
    finally:
        conn.close()


def list_plugin_installations():
    """列出全部安装行（不含 hash），按创建时间升序。"""
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                cur.execute(
                    "SELECT installation_id, plugin_id, version, enabled, "
                    " extract(epoch from created_at)::float8 AS created_at, "
                    " extract(epoch from disabled_at)::float8 AS disabled_at "
                    "FROM plugin_installations ORDER BY created_at ASC")
                rows = cur.fetchall()
        return [_installation_out(dict(r)) for r in rows]
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# run grant（run_grants）—— Stage 4-1a（docs §7.6）
# --------------------------------------------------------------------------- #
_GRANT_SEL = (
    "SELECT grant_id, installation_id, slide, session_id, created_by_user_id, "
    " extract(epoch from created_at)::float8 AS created_at, "
    " extract(epoch from expires_at)::float8 AS expires_at, revoked, "
    " extract(epoch from revoked_at)::float8 AS revoked_at "
)


def _fetch_grant(cur, grant_id):
    cur.execute(_GRANT_SEL + "FROM run_grants WHERE grant_id=%s", (grant_id,))
    row = cur.fetchone()
    return dict(row) if row else None


def create_run_grant(installation_id, slide, session_id="",
                     created_by_user_id=None, ttl_seconds=None):
    """发放一条 run grant（默认 2h），返回 grant dict。"""
    if not isinstance(installation_id, str) or not installation_id:
        raise ValueError("installation_id 不能为空")
    if not isinstance(slide, str) or not slide:
        raise ValueError("slide 不能为空")
    try:
        ttl = float(ttl_seconds) if ttl_seconds is not None else 7200.0
    except (TypeError, ValueError):
        ttl = 7200.0
    if ttl <= 0:
        ttl = 7200.0
    now = time.time()
    grant_id = "rgr_" + secrets.token_urlsafe(12)
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                cur.execute(
                    "INSERT INTO run_grants "
                    "(grant_id, installation_id, slide, session_id, "
                    " created_by_user_id, created_at, expires_at) "
                    "VALUES (%s,%s,%s,%s,%s, to_timestamp(%s), to_timestamp(%s))",
                    (grant_id, installation_id, slide, session_id or "",
                     created_by_user_id or None, now, now + ttl))
                return _fetch_grant(cur, grant_id)
    finally:
        conn.close()


def get_run_grant(grant_id):
    """按 grant_id 取 grant dict；无则 None。"""
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                return _fetch_grant(cur, grant_id)
    finally:
        conn.close()


def revoke_run_grant(grant_id):
    """撤销 run grant（幂等）。返回是否找到（已撤销也算 True）。"""
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                cur.execute(
                    "UPDATE run_grants SET revoked=TRUE, revoked_at=now() "
                    "WHERE grant_id=%s", (grant_id,))
                return cur.rowcount > 0
    finally:
        conn.close()


def list_run_grants_for_session(session_id):
    """列出某 session_id 的全部 grant（按创建时间升序）；空 session_id 返回空。"""
    if not session_id:
        return []
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                cur.execute(
                    _GRANT_SEL + "FROM run_grants WHERE session_id=%s "
                    "ORDER BY created_at ASC", (session_id,))
                return [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# dual 后端镜像（Stage 4-1a）：json 为权威，按返回的权威 dict upsert 进 pg。
# --------------------------------------------------------------------------- #
def _mirror_plugin_installation(ret, *a, **k):
    """create/rotate/set_enabled 的 result-replay：按 installation_id 回放进 pg。

    json 侧返回的 dict 已剥离 secret_hash，但 create/rotate 带一次性明文
    "secret"——hash 由明文现算后整行 upsert（明文本身绝不进 pg）；不带
    secret 的回放（set_enabled）走 UPDATE-only：不覆盖 pg 已存 hash（写空串
    会破坏 postgres 单后端下的凭证校验），行不存在则跳过（dual 镜像
    best-effort，身份值以 json 权威为准）。
    """
    row = ret if isinstance(ret, dict) else None
    if not row or not row.get("installation_id"):
        return
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                plaintext = row.get("secret")
                if isinstance(plaintext, str) and plaintext:
                    cur.execute(
                        "INSERT INTO plugin_installations "
                        "(installation_id, plugin_id, version, enabled, "
                        " secret_hash, created_at, disabled_at) "
                        "VALUES (%s,%s,%s,%s,%s, to_timestamp(%s), "
                        " to_timestamp(%s)) "
                        "ON CONFLICT (installation_id) DO UPDATE SET "
                        " plugin_id=EXCLUDED.plugin_id, version=EXCLUDED.version, "
                        " enabled=EXCLUDED.enabled, "
                        " secret_hash=EXCLUDED.secret_hash, "
                        " disabled_at=EXCLUDED.disabled_at",
                        (row["installation_id"], row.get("plugin_id"),
                         row.get("version") or "", bool(row.get("enabled")),
                         _hash_installation_secret(plaintext),
                         row.get("created_at") or time.time(),
                         row.get("disabled_at")))
                else:
                    cur.execute(
                        "UPDATE plugin_installations SET plugin_id=%s, "
                        " version=%s, enabled=%s, disabled_at=to_timestamp(%s) "
                        "WHERE installation_id=%s",
                        (row.get("plugin_id"), row.get("version") or "",
                         bool(row.get("enabled")),
                         row.get("disabled_at"), row["installation_id"]))
    finally:
        conn.close()


def _mirror_run_grant(ret, *a, **k):
    """create_run_grant 的 result-replay：按 grant_id 整行 upsert。"""
    row = ret if isinstance(ret, dict) else None
    if not row or not row.get("grant_id"):
        return
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                cur.execute(
                    "INSERT INTO run_grants "
                    "(grant_id, installation_id, slide, session_id, "
                    " created_by_user_id, created_at, expires_at, revoked, "
                    " revoked_at) "
                    "VALUES (%s,%s,%s,%s,%s, to_timestamp(%s), to_timestamp(%s), "
                    " %s, to_timestamp(%s)) "
                    "ON CONFLICT (grant_id) DO UPDATE SET "
                    " revoked=EXCLUDED.revoked, revoked_at=EXCLUDED.revoked_at, "
                    " expires_at=EXCLUDED.expires_at",
                    (row["grant_id"], row.get("installation_id"),
                     row.get("slide"), row.get("session_id") or "",
                     row.get("created_by_user_id"),
                     row.get("created_at") or time.time(),
                     row.get("expires_at") or (time.time() + 7200),
                     bool(row.get("revoked")), row.get("revoked_at")))
    finally:
        conn.close()


def _mirror_run_grant_revoke(ret, grant_id, *a, **k):
    """revoke_run_grant 的同参镜像：按入参 grant_id 撤销 pg 行。"""
    if not grant_id:
        return
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                cur.execute(
                    "UPDATE run_grants SET revoked=TRUE, revoked_at=now() "
                    "WHERE grant_id=%s", (grant_id,))
    finally:
        conn.close()
