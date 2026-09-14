# -*- coding: utf-8 -*-
"""转换任务 HTTP 处理器（W4）：列表 / 单查 / 重试。

**不含 Flask 路由**——app.py 负责路由注册、can_upload / CSRF 等前置守卫，
本模块只做 ident → conversion_store 的参数映射与错误码翻译，保持纯函数
可单测（不 import app，避免模块环，与 slide_format_registry 同约定）。

可见性合同（W4）：普通用户与 owner 工作区**均只看自己的任务**
（按 ident.user_id 等值过滤；他人任务一律 404 conversion_not_found，
不泄露存在性）。
"""

from __future__ import annotations

import conversion_store

#: GET 列表合法 group（与 conversion_store.list_jobs 对齐）
LIST_GROUPS = ("open", "recent")


def _owner_of(ident):
    """ident → owner 工作区 id（本地免登录归一为空串，与上传侧同口径）。"""
    return str((ident or {}).get("user_id") or "")


def _error(message, code, status, **extra):
    body = {"error": message, "code": code}
    body.update(extra)
    return body, status


def handle_list(ident, args):
    """GET 任务列表：group=open|recent（默认 open）、limit、cursor。"""
    args = args or {}
    group = str(args.get("group") or "open")
    if group not in LIST_GROUPS:
        return _error("group 需为 open|recent", "invalid_argument", 400)
    raw_limit = args.get("limit")
    limit = conversion_store.LIST_LIMIT_DEFAULT
    if raw_limit not in (None, ""):
        try:
            limit = int(raw_limit)
        except (TypeError, ValueError):
            return _error("limit 需为整数", "invalid_argument", 400)
    cursor = args.get("cursor") or None
    try:
        page = conversion_store.list_jobs(
            owner_user_id=_owner_of(ident), group=group, limit=limit,
            cursor=cursor)
    except ValueError as e:
        return _error(str(e) or "请求参数非法", "invalid_argument", 400)
    return {
        "items": page["items"],
        "next_cursor": page["next_cursor"],
        "group": group,
    }, 200


def handle_get(ident, job_id):
    """GET 单个任务：仅任务归属者可见；缺失/他人 → 404 conversion_not_found。"""
    job = conversion_store.get_job(job_id)
    if not job or (job.get("owner_user_id") or "") != _owner_of(ident):
        return _error("转换任务不存在",
                      conversion_store.JobNotFound.code, 404)
    return conversion_store.public_view(job), 200


def handle_retry(ident, job_id, *, source_available=True):
    """POST 重试 failed/cancelled 任务（同 id 重新入队）。

    ``source_available`` 由调用方（app.py 路由）判定源文件是否仍在
    UPLOAD_DIR 后传入——本模块不触碰文件系统。
    """
    try:
        view = conversion_store.retry_job(
            job_id, owner_user_id=_owner_of(ident),
            source_available=source_available)
    except conversion_store.JobNotFound:
        return _error("转换任务不存在",
                      conversion_store.JobNotFound.code, 404)
    except conversion_store.StateConflict as e:
        if "source_unavailable" in str(e):
            message = "源文件已不存在，请重新上传后再试"
        else:
            message = str(e) or "任务当前状态不可重试"
        body = {"error": message, "code": e.code}
        job = getattr(e, "job", None)
        if job:
            body["state"] = job.get("state")
        return body, 409
    return view, 200
