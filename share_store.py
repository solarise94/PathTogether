# -*- coding: utf-8 -*-
"""切片分享存储层（PostgreSQL 唯一后端）。

调用方一律 ``import share_store``。``STORAGE_BACKEND`` 仅接受 ``postgres``
（缺省即 postgres）。json/dual 实现已删除。
"""
import os as _os

_BACKEND_ENV = "STORAGE_BACKEND"
STORAGE_BACKEND = (_os.environ.get(_BACKEND_ENV) or "postgres").strip()
if STORAGE_BACKEND != "postgres":
    raise ValueError(
        "STORAGE_BACKEND 仅支持 postgres，当前值为 %r" % STORAGE_BACKEND
    )

_PUBLIC_NAMES = (
    # —— 常量 ——
    "SHARE_DATA_DIR",
    "SHARE_FILE",
    "ROI_TYPES",
    "ALLOWED_ROI_SIZES",
    "DEFAULT_ROI_SIZES",
    "ADMIN_TOKEN",
    "PERMISSION_VIEW",
    "PERMISSION_ANNOTATE",
    "PERMISSION_DOWNLOAD",
    "DEFAULT_PERMISSIONS",
    "AUDIT_MAX_EVENTS",  # 审计日志封顶条数
    # —— 异常类（CAS 与 review G1 fail-closed：损坏 / IO 不可用分流）——
    "RevisionConflict",
    "ShareStoreCorrupt",
    "ShareStoreUnavailable",
    # —— 函数 ——
    "set_owner_user_id",
    "probe_readable",
    "create_share",
    "get_share",
    "list_shares",
    "revoke_share",
    "claim_share",
    "claimed_active_slides_for_user",
    "list_grants_for_user",
    "grant_slide_view",
    "revoke_slide_view",
    "revoke_slide_view_grants_for_slide",
    "slide_view_grants_for_user",
    "list_slide_view_grants",
    "add_roi",
    "update_roi",
    "list_rois",
    "get_roi",
    "get_roi_by_annotation_id",
    "rehash_plaintext_visitors",
    "delete_roi",
    "delete_roi_by_annotation_id",
    "list_changes",
    "current_change_seq",
    "set_roi_shared",
    "roi_count_by_token",
    "list_shared_rois_for_slides",
    "review_roi",
    "add_comment",
    "list_comments",
    "resolve_comment",
    "delete_comment",
    "set_slide_meta",
    "get_slide_meta",
    "get_slide_meta_full",
    "get_all_slide_meta_full",
    "get_all_slide_meta",
    "get_slide_id",
    "resolve_slide_ref",
    "record_slide_asset",
    "create_project",
    "list_projects",
    "get_project",
    "update_project",
    "add_slides_to_project",
    "remove_slide_from_project",
    "delete_project",
    "annotations_by_slide",
    "annotations_by_project",
    # —— 审计日志 / 归档只读 ——
    "record_audit",
    "list_audit",
    "set_project_archived",
    "archived_slide_names",
    # —— 插件安装凭证 + run grant ——
    "create_plugin_installation",
    "rotate_installation_secret",
    "get_plugin_installation",
    "verify_installation_secret",
    "set_installation_enabled",
    "list_plugin_installations",
    # —— 插件能力层：能力注册表登记 ——
    "set_installation_capabilities",
    "create_run_grant",
    "get_run_grant",
    "revoke_run_grant",
    "list_run_grants_for_session",
    "bind_run_grant_session",
    "list_run_grants",
)

import share_store_pg as _pg

_missing = [n for n in _PUBLIC_NAMES if not hasattr(_pg, n)]
if _missing:
    raise RuntimeError("share_store_pg 缺少公共名 %s" % _missing)
_g = globals()
for _name in _PUBLIC_NAMES:
    _g[_name] = getattr(_pg, _name)


def __getattr__(name):
    raise AttributeError("module %r has no attribute %r" % (__name__, name))