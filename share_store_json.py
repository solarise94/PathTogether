# -*- coding: utf-8 -*-
"""切片分享 —— 共享存储层。

被两个进程并发使用：
- app.py（主应用 :8000，管理员写入）
- share_server.py（分享服务 :38000，读为主，外部用户写入 ROI）

数据文件为单个 JSON，所有读写通过 fcntl.flock 互斥访问，保证一致性。
"""

import json
import os
import secrets
import shutil
import time
import uuid
import hashlib
import hmac
from pathlib import Path

import fcntl

# 数据目录与文件路径
SHARE_DATA_DIR = Path(
    os.environ.get("SHARE_DATA_DIR") or (Path.home() / "svs-viewer" / "share-data")
)
SHARE_DATA_DIR.mkdir(parents=True, exist_ok=True)
SHARE_FILE = SHARE_DATA_DIR / "shares.json"

# 空结构骨架（change_seq_by_slide 为切片级全局单调变更序号计数器，见 docs 4.2）
# grants：Stage 3a-2a 认领关系（user 认领分享链接 → 可见受邀切片），见 docs §5.4。
_EMPTY = {
    "shares": {},
    "rois": [],
    "projects": {},
    "slide_meta": {},
    "change_seq_by_slide": {},
    "grants": [],
    "comments": [],
    # Stage 3c-2：协作操作审计日志（顶层数组，封顶 AUDIT_MAX_EVENTS 条丢最旧）
    "audit": [],
    # Stage 4-1a：插件安装（安装凭证只存 hash，明文仅创建/轮换时返回一次）
    "plugin_installations": [],
    # Stage 4-1a：run grant（起跑授权，slide 级、默认 2h、可撤销；无 org，docs §7.6）
    "run_grants": [],
}

# 支持的标注类型
ROI_TYPES = ("rect", "arrow", "freehand")

# 分享可选的 ROI 矩形标记尺寸（mm），以 float 存储为子集
ALLOWED_ROI_SIZES = (6.0, 6.5)
# 默认标记尺寸子集（未指定时）
DEFAULT_ROI_SIZES = [6.0, 6.5]

# 管理员标注使用的固定 token
ADMIN_TOKEN = "admin"


# --------------------------------------------------------------------------- #
# Stage 3c-1：revision CAS（并发编辑不静默覆盖）
#
# update_roi/delete_roi/set_roi_shared 接受可选 expected_revision：提供且与当前
# revision 不符 → 抛 RevisionConflict（携带 current_revision）。不提供则维持旧行为
# （旧客户端兼容，不强制 CAS）。两 impl 各定义一份同语义类；dispatcher 导出 json
# 侧为准（dual 下写路径权威在 json，抛出的也是 json 的类）。
# --------------------------------------------------------------------------- #
class RevisionConflict(Exception):
    """CAS 失败：expected_revision 与当前 revision 不符。

    携带 ``current_revision``（int）供 app.py 映射 409 {error:"revision_conflict"}。
    """

    def __init__(self, current_revision, message=None):
        self.current_revision = int(current_revision)
        super().__init__(
            message or "revision 冲突：标注已被他人修改，请刷新后重试")


# --------------------------------------------------------------------------- #
# Stage 3a-2a：分享权限档位与认领（docs §5.4）
#
# 分享权限三档：view / annotate / download。旧分享无 permissions 字段时一律视为
# ["view","annotate"]（严格等价旧行为：分享页本来就能看能标，不能下载切片文件）。
# download 控制未来文件下载端点；本节点只落地字段 + 在标注写入端点判定 annotate。
# --------------------------------------------------------------------------- #
PERMISSION_VIEW = "view"
PERMISSION_ANNOTATE = "annotate"
PERMISSION_DOWNLOAD = "download"
_PERMISSION_ALL = (PERMISSION_VIEW, PERMISSION_ANNOTATE, PERMISSION_DOWNLOAD)
# 旧分享 / 未指定时的默认权限（等价拆分前的"能看就能标"）
DEFAULT_PERMISSIONS = [PERMISSION_VIEW, PERMISSION_ANNOTATE]


def _normalize_permissions(perms):
    """归一化分享权限档位：返回去重保序的合法子集 list。

    None 或空 → DEFAULT_PERMISSIONS（["view","annotate"]，等价旧行为）。
    非数组 / 含非法值抛 ValueError。
    """
    if perms is None:
        return list(DEFAULT_PERMISSIONS)
    if not isinstance(perms, list):
        raise ValueError("permissions 需为数组")
    if not perms:
        return list(DEFAULT_PERMISSIONS)
    out = []
    for p in perms:
        if p not in _PERMISSION_ALL:
            raise ValueError("permissions 仅支持 view/annotate/download")
        if p not in out:
            out.append(p)
    return out


def _share_permissions(share):
    """从 share dict 读取归一化权限；旧分享无该字段时返回默认 view+annotate。"""
    perms = share.get("permissions") if isinstance(share, dict) else None
    if not isinstance(perms, list) or not perms:
        return list(DEFAULT_PERMISSIONS)
    # 兜底过滤脏数据（未知值剔除后为空则回默认）
    clean = [p for p in perms if p in _PERMISSION_ALL]
    return clean if clean else list(DEFAULT_PERMISSIONS)


def _grant_permissions_of(g):
    """grant.permissions；旧 grant 无该字段时等同 DEFAULT（view+annotate）。"""
    perms = g.get("permissions") if isinstance(g, dict) else None
    if not isinstance(perms, list) or not perms:
        return list(DEFAULT_PERMISSIONS)
    clean = [p for p in perms if p in _PERMISSION_ALL]
    return clean if clean else list(DEFAULT_PERMISSIONS)


def _cap_claim_permissions(requested, allowed):
    """认领权限必须是分享权限的子集。None/空 → 使用分享权限（而非全局 DEFAULT）。"""
    allowed = list(allowed)
    if requested is None or requested == []:
        return allowed
    if not isinstance(requested, list):
        raise ValueError("permissions 需为数组")
    out = []
    for p in requested:
        if p not in _PERMISSION_ALL:
            raise ValueError("permissions 仅支持 view/annotate/download")
        if p not in allowed:
            raise ValueError("permissions 超出分享权限")
        if p not in out:
            out.append(p)
    return out if out else allowed


def _reject_guest_write(requester_role):
    """仓储边界（docs §5.1.1）：显式传 guest 角色时拒绝图库写操作。

    requester_role 默认 None = 内部调用（如 share_server 的 /s/* 标注流程、
    internal AI 回调）不限制；显式传 "guest"（== user_store.ROLE_GUEST）时 raise
    PermissionError。app.py 调用处传入当前 role，作为应用层之外的 defense-in-depth。
    """
    if requester_role == "guest":
        raise PermissionError("guest 无权进行图库写操作")

# 数据归属（Stage 3a 身份基础）：懒迁移用「首个 owner 的 user_id」。
# 由 app.py 在启动（owner 引导）后调用 set_owner_user_id() 注入；share_server.py
# 不注入（保持其读路径无归属迁移）。_ensure_owner_refs 在 _load_locked 中对
# projects/slide_meta/rois 补 owner_user_id 字段（本节点只落地字段，不做按字段
# 过滤——那是下一个节点「资源级鉴权矩阵」的事）。
_OWNER_USER_ID: str = ""


def set_owner_user_id(user_id: str) -> None:
    """注入当前 owner 的 user_id（供数据归属懒迁移使用）。"""
    global _OWNER_USER_ID
    _OWNER_USER_ID = user_id or ""


def _ensure_owner_refs(data):
    """把现存 projects/slide_meta/rois 懒迁移补 owner_user_id=首个 owner。

    幂等：已有 owner_user_id 的不改。owner user_id 未注入（为空串）时跳过，
    保持 share_server 等不关心归属的读路径零改动。返回是否发生过迁移。
    """
    if not _OWNER_USER_ID:
        return False
    migrated = False
    for proj in data["projects"].values():
        if isinstance(proj, dict) and proj.get("owner_user_id") is None:
            proj["owner_user_id"] = _OWNER_USER_ID
            migrated = True
    for meta in data["slide_meta"].values():
        if isinstance(meta, dict) and meta.get("owner_user_id") is None:
            meta["owner_user_id"] = _OWNER_USER_ID
            migrated = True
    for roi in data["rois"]:
        if isinstance(roi, dict) and roi.get("owner_user_id") is None:
            roi["owner_user_id"] = _OWNER_USER_ID
            migrated = True
    return migrated


def _roi_shared_compat(roi):
    """读取 roi 的 shared 字段并做旧数据兼容。

    缺失 "shared" 字段时：
      - token == "admin" 视为 True（管理员标注此前对分享用户全可见，保持不突变）
      - 其他用户 token 视为 False（此前对其他用户本就不可见）
    存在但非布尔时按真值判断；最终统一返回 bool。
    """
    if not isinstance(roi, dict):
        return False
    if "shared" in roi:
        return bool(roi.get("shared"))
    # 旧数据兼容
    return roi.get("token") == ADMIN_TOKEN


def _normalize_roi_sizes(roi_sizes):
    """校验并归一化 roi_sizes：统一转 float，去重保序，且必须是
    ALLOWED_ROI_SIZES 的子集。返回 list[float]；非法抛 ValueError。
    None 时返回 DEFAULT_ROI_SIZES 的副本。
    """
    if roi_sizes is None:
        return list(DEFAULT_ROI_SIZES)
    if not isinstance(roi_sizes, (list, tuple)):
        raise ValueError("roi_sizes 需为数组")
    allowed = set(ALLOWED_ROI_SIZES)
    out = []
    seen = set()
    for s in roi_sizes:
        if isinstance(s, bool) or not isinstance(s, (int, float)):
            raise ValueError("roi_sizes 元素需为数值")
        import math
        if not math.isfinite(float(s)):
            raise ValueError("roi_sizes 元素需为有限数值")
        v = float(s)
        if v not in allowed:
            raise ValueError("roi_sizes 仅允许 6 或 6.5")
        if v not in seen:
            seen.add(v)
            out.append(v)
    if not out:
        raise ValueError("roi_sizes 不能为空")
    return out


def _share_roi_sizes(share):
    """从 share dict 读取归一化的 roi_sizes；旧分享无该字段时返回默认。"""
    rs = share.get("roi_sizes") if isinstance(share, dict) else None
    if not isinstance(rs, list) or not rs:
        return list(DEFAULT_ROI_SIZES)
    # 兜底过滤：脏数据（非数字/越界）统一回默认
    try:
        return _normalize_roi_sizes(rs)
    except ValueError:
        return list(DEFAULT_ROI_SIZES)


def _is_finite_num(v):
    """判断 v 是否为有限数值（int/float，非 NaN/Inf）。"""
    if isinstance(v, bool):
        return False
    if not isinstance(v, (int, float)):
        return False
    import math
    return math.isfinite(v)


def _load_locked(f):
    """在已锁定的文件对象上读取并解析 JSON；损坏则备份重建。

    会做存量 ROI 迁移（_ensure_roi_identity）。若迁移改动数据，本次临界区的
    迁移后 dict 会缓存到 f._svs_migrated_data；_with_lock 在 fn 结束后若发现
    fn 自身未保存（缓存未被消费）且 fn 未显式写盘，则补一次落盘，保证读路径
    返回的迁移值（annotation_id/source 等）下次落盘不丢。迁移本身幂等。
    """
    f.seek(0)
    raw = f.read()
    if not raw:
        return _copy_empty()
    try:
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError("top-level not object")
        data.setdefault("shares", {})
        data.setdefault("rois", [])
        # 向后兼容：旧文件无 projects 时补 {}
        data.setdefault("projects", {})
        # 向后兼容：旧文件无 slide_meta 时补 {}
        data.setdefault("slide_meta", {})
        # 向后兼容：旧文件无 change_seq_by_slide 时补 {}
        data.setdefault("change_seq_by_slide", {})
        # 向后兼容：旧文件无 grants 时补 []（Stage 3a-2a 认领关系）
        data.setdefault("grants", [])
        # 向后兼容：旧文件无 comments 时补 []（Stage 3c-1 评论线程）
        data.setdefault("comments", [])
        if not isinstance(data["shares"], dict):
            data["shares"] = {}
        if not isinstance(data["rois"], list):
            data["rois"] = []
        if not isinstance(data["projects"], dict):
            data["projects"] = {}
        if not isinstance(data["slide_meta"], dict):
            data["slide_meta"] = {}
        if not isinstance(data["change_seq_by_slide"], dict):
            data["change_seq_by_slide"] = {}
        if not isinstance(data["grants"], list):
            data["grants"] = []
        if not isinstance(data["comments"], list):
            data["comments"] = []
        # 向后兼容：旧文件无 audit 时补 []（Stage 3c-2 审计日志）
        data.setdefault("audit", [])
        if not isinstance(data["audit"], list):
            data["audit"] = []
        # 向后兼容：旧文件无 plugin_installations / run_grants 时补 []（Stage 4-1a）
        data.setdefault("plugin_installations", [])
        if not isinstance(data["plugin_installations"], list):
            data["plugin_installations"] = []
        data.setdefault("run_grants", [])
        if not isinstance(data["run_grants"], list):
            data["run_grants"] = []
        # 向后兼容：旧项目无 archived 字段 → 默认 false（未归档，纯只读开关，docs §v1.5）
        for _proj in data.get("projects", {}).values():
            if isinstance(_proj, dict) and _proj.get("archived") is None:
                _proj["archived"] = False
        # 存量 ROI 一次性迁移（补 annotation_id/change_seq/revision/source/deleted）
        # 迁移若改动数据，缓存给 _with_lock 用于补落盘
        changed = _ensure_roi_identity(data)
        # 数据归属懒迁移（补 owner_user_id；owner 已注入时才生效）
        if _ensure_owner_refs(data):
            changed = True
        if changed:
            f._svs_migrated_data = data
        return data
    except (json.JSONDecodeError, ValueError):
        # 损坏：备份后重建
        f.seek(0)
        bak = SHARE_FILE.with_suffix(".json.bak")
        try:
            with open(bak, "w", encoding="utf-8") as bf:
                bf.write(raw)
        except Exception:
            pass
        return _copy_empty()


def _copy_empty():
    """返回一个新的空结构（避免共享引用）。"""
    return {"shares": {}, "rois": [], "projects": {}, "slide_meta": {},
            "change_seq_by_slide": {}, "grants": [], "comments": [], "audit": [],
            "plugin_installations": [], "run_grants": []}


# --------------------------------------------------------------------------- #
# ROI 迁移 / 稳定 ID / 变更序号（docs §4.2 v3 P0）
#
# 现有 ROI 只按「token 内插入序 index」定位，delete 用 pop() 会位移，不能作
# fork 根键。一次性迁移补齐：annotation_id(UUID)、change_seq（切片级全局单调，
# 按现有顺序赋递增初值）、revision=1、source（旧数据安全默认 human）、
# deleted=false；并初始化 change_seq_by_slide 全局计数器。
#
# source 不再用启发式判据猜（#4 后续）：AI 标注自本版起在落标时显式写
# source="ai"（add_roi 的 source 参数），新数据天然带正确来源；旧数据一律
# human，不做"看起来像 AI"的猜测修正。
# --------------------------------------------------------------------------- #



def _ensure_roi_identity(data):
    """把 data（已锁定）中的存量 ROI 一次性补齐稳定 ID / 变更字段。

    幂等：已有 annotation_id 的跳过；change_seq_by_slide 已初始化的不重置
    （后续每次新建/编辑/删除在锁内继续递增）。返回是否发生过迁移。
    """
    migrated = False
    seq_map = data.get("change_seq_by_slide")
    if not isinstance(seq_map, dict):
        seq_map = {}
        data["change_seq_by_slide"] = seq_map
    next_seq = {}
    for slide, cur in seq_map.items():
        if isinstance(cur, (int, float)):
            next_seq[str(slide)] = int(cur)
    for roi in data["rois"]:
        if not isinstance(roi, dict):
            continue
        slide = roi.get("slide")
        if not roi.get("annotation_id"):
            roi["annotation_id"] = str(uuid.uuid4())
            migrated = True
        if roi.get("revision") is None:
            roi["revision"] = 1
            migrated = True
        if roi.get("source") is None:
            # 缺省：旧标注一律 human（不再用启发式判据猜 AI 来源——AI 标注
            # 自本版起在落标时显式写 source="ai"，新数据天然带正确来源）。
            roi["source"] = "human"
            migrated = True
        if roi.get("deleted") is None:
            roi["deleted"] = False
            migrated = True
        if roi.get("review_status") is None:
            # Stage 3c-1：旧标注缺审核状态 → 一律 none（兼容；新 AI 标注写入
            # 时显式写 pending，新人工标注写 none）
            roi["review_status"] = "none"
            migrated = True
        if roi.get("change_seq") is None and slide is not None:
            # 按现有顺序赋递增初值；同一张切片的计数器共享
            nxt = next_seq.get(slide, 0) + 1
            next_seq[slide] = nxt
            roi["change_seq"] = nxt
            migrated = True
        if roi.get("updated_at") is None:
            roi["updated_at"] = roi.get("ts") or time.time()
            migrated = True
    for slide, nxt in next_seq.items():
        seq_map[slide] = nxt
    return migrated


def _roi_index_map(data, token):
    """返回 {roi对象id: token 内插入序 index}（与 list_rois 的 counters 一致）。"""
    counters = {}
    out = {}
    for r in data["rois"]:
        if r.get("token") == token:
            out[id(r)] = counters.get(token, 0)
            counters[token] = counters.get(token, 0) + 1
    return out


# roi 几何字段集合（history 快照与镜像用）
_GEOM_KEYS = ("x", "y", "side_px", "size_mm", "x1", "y1", "x2", "y2", "points")


def _roi_out(roi, index=None, shared=None):
    """ROI 导出副本：统一补 index/shared/note 兼容字段。

    tombstone（deleted=true）只保留最小字段（annotation_id/slide/revision/
    deleted_at/change_seq/deleted），避免泄露已删标注的几何/备注。
    非 tombstone 补 review_status 字段（缺省 none）。
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
    # Stage 3c-2：历史 AI 标注（source=ai 但无 provenance）输出 partial 标记，
    # 供前端/审计识别「早期无溯源」的 AI 标注（docs §6.4）。
    if roi.get("source") == "ai" and not isinstance(roi.get("provenance"), dict):
        out["provenance"] = {"partial": True}
    return out


def _check_cas(roi, expected_revision):
    """Stage 3c-1 CAS：expected_revision 提供且与当前 revision 不符 → 抛 RevisionConflict。

    expected_revision 为 None 时跳过（旧行为兼容，不强制 CAS）。
    """
    if expected_revision is None:
        return
    cur = int(roi.get("revision") or 1)
    if int(expected_revision) != cur:
        raise RevisionConflict(cur)


def _append_history(roi):
    """Stage 3c-1 修改历史：把当前快照（geom/note/label/revision/ts）append 进
    roi['history']，上限 20 条，超出丢最旧。在 update/tombstone 修改**之前**调用。
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


def _bump_change_seq(data, slide):
    """在锁内为某切片递增全局 change_seq 计数器，返回新值。"""
    seq_map = data.setdefault("change_seq_by_slide", {})
    cur = seq_map.get(slide)
    if not isinstance(cur, (int, float)):
        cur = 0
    nxt = int(cur) + 1
    seq_map[slide] = nxt
    return nxt


def _visible(data):
    """过滤出非 tombstone 的 roi（list/get/update 默认过滤，docs §4.2）。"""
    return [r for r in data["rois"] if not r.get("deleted")]


def _save_locked(f, data):
    """在已锁定的文件对象上写入 JSON（先截断）。

    fn 自身已写盘时，清掉 _with_lock 的迁移补落盘缓存（避免用过期快照覆盖）。
    """
    f.seek(0)
    f.truncate()
    json.dump(data, f, ensure_ascii=False, indent=2)
    f.flush()
    os.fsync(f.fileno())
    # fn 已落盘 → 取消 _with_lock 末尾的迁移补落盘
    if hasattr(f, "_svs_migrated_data"):
        f._svs_migrated_data = None


def _with_lock(mode, fn):
    """以指定模式打开 SHARE_FILE，加排他锁后执行 fn(file_obj)。

    mode 为 'r+'（读写，要求文件已存在；不存在则先创建）或 'w+'。
    返回 fn 的返回值。

    若本次 _load_locked 触发了存量 ROI 迁移，而 fn 自身未显式写盘（只读路径如
    list_rois / annotations_by_slide），这里补一次保存，让迁移结果落盘——读路径
    返回的 annotation_id/source 立即可被前端使用，下次任何写自然带上迁移后格式。
    迁移本身幂等（已补字段的 ROI 不再改），重复读不会重复落盘。
    """
    # 确保文件存在
    if not SHARE_FILE.exists():
        SHARE_FILE.touch()
    with open(SHARE_FILE, mode, encoding="utf-8") as f:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        migrated_data = None
        try:
            ret = fn(f)
            # 若迁移发生且 fn 未消费（未清缓存），补落盘
            migrated_data = getattr(f, "_svs_migrated_data", None)
            if migrated_data is not None:
                _save_locked(f, migrated_data)
                f._svs_migrated_data = None
            return ret
        finally:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)


# --------------------------------------------------------------------------- #
# 公共 API
# --------------------------------------------------------------------------- #
def create_share(slides, expires_hours, roi_sizes=None, permissions=None,
                 creator_user_id=None, requester_role=None):
    """创建分享：生成 token、写入并返回 share dict（含 token 与 roi_sizes）。

    roi_sizes：矩形标记可选尺寸子集（元素 6/6.5），None 时默认两者皆可。
    非法（非数组、含越界值、空）抛 ValueError。
    permissions：权限档位子集（view/annotate/download），None 时默认 view+annotate
    （等价旧行为）。creator_user_id：创建者归属（Stage 3a-2a 分享列表按此过滤）。
    requester_role：显式传 "guest" 时拒绝（仓储边界 defense-in-depth）。
    """
    _reject_guest_write(requester_role)
    roi_sizes_norm = _normalize_roi_sizes(roi_sizes)
    perms = _normalize_permissions(permissions)
    creator = creator_user_id or None
    token = secrets.token_urlsafe(18)
    now = time.time()
    expires_at = now + float(expires_hours) * 3600.0
    share = {
        "slides": list(slides),
        "created_at": now,
        "expires_at": expires_at,
        "revoked": False,
        "token": token,
        "roi_sizes": list(roi_sizes_norm),
        "permissions": list(perms),
        "creator_user_id": creator,
    }

    def _do(f):
        data = _load_locked(f)
        data["shares"][token] = {
            "slides": list(slides),
            "created_at": now,
            "expires_at": expires_at,
            "revoked": False,
            "roi_sizes": list(roi_sizes_norm),
            "permissions": list(perms),
            "creator_user_id": creator,
        }
        _save_locked(f, data)
        return share

    return _with_lock("r+", _do)


def _is_active(share):
    """判断 share dict 是否仍有效（未撤销且未过期）。"""
    if share.get("revoked"):
        return False
    exp = share.get("expires_at")
    if exp is not None and exp < time.time():
        return False
    return True


def get_share(token):
    """获取有效分享；不存在/已撤销/已过期返回 None。

    返回 dict 含 token 与归一化的 roi_sizes（旧分享无该字段默认两者皆可）。
    """
    def _do(f):
        data = _load_locked(f)
        share = data["shares"].get(token)
        if share is None:
            return None
        if not _is_active(share):
            return None
        out = dict(share)
        out["token"] = token
        out["roi_sizes"] = _share_roi_sizes(share)
        out["permissions"] = _share_permissions(share)
        return out

    return _with_lock("r+", _do)


def _status_of(share):
    if share.get("revoked"):
        return "revoked"
    exp = share.get("expires_at")
    if exp is not None and exp < time.time():
        return "expired"
    return "active"


def list_shares():
    """返回全部分享（含 status 与 roi_sizes 字段），按 created_at 倒序。"""
    def _do(f):
        data = _load_locked(f)
        items = []
        for tok, sh in data["shares"].items():
            out = dict(sh)
            out["token"] = tok
            out["status"] = _status_of(sh)
            out["roi_sizes"] = _share_roi_sizes(sh)
            out["permissions"] = _share_permissions(sh)
            items.append(out)
        items.sort(key=lambda x: x.get("created_at", 0), reverse=True)
        return items

    return _with_lock("r+", _do)


def revoke_share(token):
    """撤销分享，返回是否成功。"""
    def _do(f):
        data = _load_locked(f)
        share = data["shares"].get(token)
        if share is None:
            return False
        share["revoked"] = True
        _save_locked(f, data)
        return True

    return _with_lock("r+", _do)


# --------------------------------------------------------------------------- #
# 认领（grants）—— Stage 3a-2a 协作授权（docs §5.4）
#
# 注册 user 认领一个分享链接后，该 share 的 slides 进入其「可见切片集」
# （{owner_user_id==自己} ∪ {public} ∪ {认领的 active share 的 slides}）。
# grant 记录 {grant_id, user_id, share_token, permissions, claimed_at, revoked_at}。
# share 被撤销/过期后 grant 自动失效（判定时检查 share active，见
# claimed_active_slides_for_user）。
# --------------------------------------------------------------------------- #
def _grant_out(g):
    """导出 grant 副本（确保字段齐全）。"""
    out = dict(g)
    out.setdefault("permissions", list(DEFAULT_PERMISSIONS))
    out.setdefault("revoked_at", None)
    return out


def claim_share(token, user_id, permissions=None):
    """user 认领分享链接（幂等）。

    重复认领（同 token + 同 user，且未 revoke）返回已有 grant；权限被夹到当前
    分享权限子集（修复旧的越权 grant）。缺省 permissions 使用分享权限，而不是
    全局 DEFAULT（避免 view-only 分享被认领成 annotate）。
    返回 grant dict。不校验 share 是否 active —— 由调用方（app.py）先用 get_share
    判定；本函数只负责记录认领关系。
    """
    if not isinstance(token, str) or not token:
        raise ValueError("token 不能为空")
    if not isinstance(user_id, str) or not user_id:
        raise ValueError("user_id 不能为空")

    def _do(f):
        data = _load_locked(f)
        shares = data.get("shares") or {}
        share = shares.get(token) if isinstance(shares, dict) else None
        allowed = _share_permissions(share) if share is not None else list(
            DEFAULT_PERMISSIONS)
        grants = data.setdefault("grants", [])
        if not isinstance(grants, list):
            grants = []
            data["grants"] = grants
        for g in grants:
            if (g.get("share_token") == token and g.get("user_id") == user_id
                    and g.get("revoked_at") is None):
                if permissions is not None:
                    perms = _cap_claim_permissions(permissions, allowed)
                else:
                    perms = [p for p in _grant_permissions_of(g) if p in allowed]
                    if not perms:
                        perms = list(allowed)
                if list(g.get("permissions") or []) != list(perms):
                    g["permissions"] = list(perms)
                    _save_locked(f, data)
                return _grant_out(g)
        perms = _cap_claim_permissions(permissions, allowed)
        g = {
            "grant_id": "grt_" + secrets.token_urlsafe(8),
            "user_id": user_id,
            "share_token": token,
            "permissions": list(perms),
            "claimed_at": time.time(),
            "revoked_at": None,
        }
        grants.append(g)
        _save_locked(f, data)
        return _grant_out(g)

    return _with_lock("r+", _do)


def claimed_active_slides_for_user(user_id, permission=None):
    """返回该 user 认领过的、且对应 share 仍 active 的切片名集合。

    grant.revoked_at 非 None 或 share 不存在/已撤销/已过期均不计入
    （share 撤销/过期后 grant 自动失效）。
    permission 若给出（view/annotate/download），只计入 grant 含该权限的切片。
    """
    if not user_id:
        return set()
    if permission is not None and permission not in _PERMISSION_ALL:
        return set()

    def _do(f):
        data = _load_locked(f)
        grants = data.get("grants") or []
        shares = data.get("shares") or {}
        out = set()
        for g in grants:
            if g.get("user_id") != user_id:
                continue
            if g.get("revoked_at") is not None:
                continue
            if permission is not None and permission not in _grant_permissions_of(g):
                continue
            tok = g.get("share_token")
            share = shares.get(tok) if isinstance(tok, str) else None
            if share is None or not _is_active(share):
                continue
            for s in share.get("slides", []):
                if isinstance(s, str):
                    out.add(s)
        return out

    return _with_lock("r+", _do)


def list_grants_for_user(user_id):
    """返回该 user 的全部 grant（含已失效，附 share_active 标志）。供调试/审计。"""
    if not user_id:
        return []

    def _do(f):
        data = _load_locked(f)
        grants = data.get("grants") or []
        shares = data.get("shares") or {}
        out = []
        for g in grants:
            if g.get("user_id") != user_id:
                continue
            tok = g.get("share_token")
            share = shares.get(tok) if isinstance(tok, str) else None
            gg = _grant_out(g)
            gg["share_active"] = bool(share is not None and _is_active(share))
            out.append(gg)
        out.sort(key=lambda x: x.get("claimed_at", 0), reverse=True)
        return out

    return _with_lock("r+", _do)


def _validate_geom(typ, geom):
    """校验几何字段，返回归一化后的几何 dict（不含 type/label/token/slide/ts）。

    - rect：x, y, side_px, size_mm（side_px 1~40000）
    - arrow：x1, y1, x2, y2（两端点距离 > 0）
    - freehand：points: [[x,y],...]（3~500 点，坐标 ≥0 且有限）
    坐标均要求 ≥0 且数值有限；x/y/side_px 等兼容字段据此计算。
    校验失败抛 ValueError。
    """
    if typ == "rect":
        x = geom.get("x")
        y = geom.get("y")
        side_px = geom.get("side_px")
        size_mm = geom.get("size_mm")
        if not (_is_finite_num(x) and _is_finite_num(y) and _is_finite_num(side_px)):
            raise ValueError("几何参数需为数值")
        x = int(x); y = int(y)
        side_px = int(side_px)
        if x < 0 or y < 0:
            raise ValueError("坐标需 ≥0")
        if side_px < 1 or side_px > 40000:
            raise ValueError("side_px 需在 1~40000 之间")
        size_mm_v = float(size_mm) if _is_finite_num(size_mm) else 0.0
        return {
            "type": "rect",
            "x": x, "y": y,
            "side_px": side_px,
            "size_mm": size_mm_v,
        }

    if typ == "arrow":
        x1 = geom.get("x1"); y1 = geom.get("y1")
        x2 = geom.get("x2"); y2 = geom.get("y2")
        if not all(_is_finite_num(v) for v in (x1, y1, x2, y2)):
            raise ValueError("几何参数需为数值")
        x1 = int(x1); y1 = int(y1); x2 = int(x2); y2 = int(y2)
        if any(v < 0 for v in (x1, y1, x2, y2)):
            raise ValueError("坐标需 ≥0")
        dist2 = (x1 - x2) ** 2 + (y1 - y2) ** 2
        if dist2 <= 0:
            raise ValueError("箭头两端点不能重合")
        # 中点存 x/y，side_px 留 0（兼容旧查询，无意义）
        cx = (x1 + x2) // 2
        cy = (y1 + y2) // 2
        return {
            "type": "arrow",
            "x1": x1, "y1": y1, "x2": x2, "y2": y2,
            "x": cx, "y": cy,
            "side_px": 0,
            "size_mm": 0.0,
        }

    if typ == "freehand":
        pts = geom.get("points")
        if not isinstance(pts, list) or len(pts) < 3 or len(pts) > 500:
            raise ValueError("描图需 3~500 个点")
        clean = []
        for p in pts:
            if (not isinstance(p, (list, tuple))) or len(p) != 2:
                raise ValueError("points 元素需为 [x,y]")
            px, py = p
            if not (_is_finite_num(px) and _is_finite_num(py)):
                raise ValueError("坐标需为数值")
            px = int(px); py = int(py)
            if px < 0 or py < 0:
                raise ValueError("坐标需 ≥0")
            clean.append([px, py])
        xs = [p[0] for p in clean]
        ys = [p[1] for p in clean]
        minx = min(xs); miny = min(ys)
        side = max(max(xs) - minx, max(ys) - miny)
        return {
            "type": "freehand",
            "points": clean,
            "x": minx, "y": miny,
            "side_px": int(side),
            "size_mm": 0.0,
        }

    raise ValueError("未知标注类型")


def _clean_note(note):
    """归一化备注文本：非 str 视为空串；strip 后长度 ≤ 500，否则抛 ValueError。

    None → ""；非字符串 → ""。返回清洗后的字符串。
    """
    if note is None:
        return ""
    if not isinstance(note, str):
        return ""
    n = note.strip()
    if len(n) > 500:
        raise ValueError("备注过长")
    return n


def add_roi(token, slide, label, type="rect", size_mm=0.0, shared=False, note="", visitor=None,
            source=None, created_by_session_id=None, _effect_key=None, owner_user_id=None,
            requester_role=None, provenance=None, **geom):
    """为 token 的 share 添加一条标注；统一入口，支持 rect/arrow/freehand。

    管理员标注使用 token="admin"（此时 share 校验放宽：不要求 token 命中 shares，
    但仍要求 slide 文件名合法）。普通用户标注校验 share 存在、有效、且 slide 属于它。

    label（标记人/标签）为必填，去空白后非空，否则抛 ValueError。
    type 必须是 ROI_TYPES 之一（缺省 rect，向后兼容）。
    shared 为布尔，记录该标注是否对全部分享用户公开展示（缺省 False）。
    note 为备注文本（可选，缺省空串；strip 后 ≤ 500 字符，超出抛 ValueError）。
    visitor 为创建设备的访客标识（可选，None 存空串）——分享端按设备归属校验用。

    source：ROI 来源（"ai" | "human"），缺省按 token 推断（admin 且非公开 → "ai"，
    其余 → "human"）。created_by_session_id：AI 落标时所属 session（可选）。
    _effect_key：WAL 幂等键（docs §5.4）；同一键已落标时复用返回（不重复写入），
    幂等检查与写入在同一 share_store 锁临界区内完成。
    provenance（Stage 3c-2，docs §6.4）：AI 写回时的溯源子对象
    {plugin_id, plugin_version, run_id, model, provider, created_by_user_id,
     slide_asset_revision, idempotency_key}。人工标注不传。

    返回新增的 roi dict（含该 token 下的 index，从 0 起按时间顺序，以及 shared/note、
    annotation_id/change_seq 等）。若校验失败抛出 ValueError。
    requester_role：显式传 "guest" 时拒绝（仓储边界 defense-in-depth）。
    """
    _reject_guest_write(requester_role)
    # type 合法性
    if type not in ROI_TYPES:
        raise ValueError("未知标注类型")
    # label 非空校验（在锁外做即可）
    if not isinstance(label, str):
        raise ValueError("请填写用户名或标签")
    label = label.strip()
    if not label:
        raise ValueError("请填写用户名或标签")
    # note 清洗（在锁外做即可）
    note_clean = _clean_note(note)

    # 几何校验（合并 size_mm，rect 会覆盖）
    geom_full = dict(geom)
    geom_full["size_mm"] = size_mm
    norm = _validate_geom(type, geom_full)
    norm["type"] = type

    def _do(f):
        data = _load_locked(f)
        is_admin = (token == ADMIN_TOKEN)
        if is_admin:
            # 管理员标注：不要求 token 命中 shares，slide 文件名合法性由调用方保证
            pass
        else:
            share = data["shares"].get(token)
            if share is None or not _is_active(share):
                raise ValueError("share invalid")
            if slide not in share.get("slides", []):
                raise ValueError("slide not in share")
        # WAL 幂等：effect_key 已落 → 直接复用返回（不重复写、不重复递增 change_seq）
        if _effect_key:
            for r in data["rois"]:
                if r.get("effect_key") == _effect_key and not r.get("deleted"):
                    same = [x for x in data["rois"] if x.get("token") == token]
                    idx = [i for i, x in enumerate(same) if x is r]
                    return _roi_out(r, index=(idx[0] if idx else len(same) - 1))
        now = time.time()
        src = source if source in ("ai", "human") else (
            "ai" if (is_admin and not shared) else "human")
        roi = {
            "token": token,
            "slide": slide,
            "label": label,
            "ts": now,
            "shared": bool(shared),
            "note": note_clean,
            "visitor": visitor or "",
            # docs §4.2 新增字段：稳定 ID / 来源 / 变更追踪 / tombstone
            "annotation_id": str(uuid.uuid4()),
            "source": src,
            "created_by_session_id": created_by_session_id or "",
            "revision": 1,
            "change_seq": _bump_change_seq(data, slide),
            "updated_at": now,
            "deleted": False,
            "owner_user_id": owner_user_id or _OWNER_USER_ID or None,
            # Stage 3c-1：AI 新写入默认 pending 待审；人工标注 none（兼容现状）
            "review_status": "pending" if src == "ai" else "none",
        }
        if _effect_key:
            roi["effect_key"] = _effect_key
        # Stage 3c-2：AI 溯源子对象（仅 AI 写入，且仅当传入非空 dict 才落）
        if src == "ai" and isinstance(provenance, dict) and provenance:
            roi["provenance"] = dict(provenance)
        roi.update(norm)
        data["rois"].append(roi)
        _save_locked(f, data)
        # index 为该 token 下按插入顺序的序号
        same_token = [r for r in data["rois"] if r["token"] == token]
        roi_out = dict(roi)
        roi_out["index"] = len(same_token) - 1
        roi_out["shared"] = bool(shared)
        return roi_out

    return _with_lock("r+", _do)


def update_roi(token, index, geom=None, note=None, expected_revision=None):
    """更新该 token 下第 index 条 roi 的几何与/或备注。

    - 锁定内按 token 内序号定位（逻辑同 delete_roi：same 列表）。
      index 越界返回 False（与 set_roi_shared 风格一致，不抛异常）。
    - 非 admin token 时校验 share 有效（同 add_roi 的 _is_active 逻辑），
      无效抛 ValueError("share invalid")。admin token 直接放行。
    - geom（dict，不含 type）经 _validate_geom(原type, geom) 归一化后 merge 进 roi
      （type 保持原值，ts 不动 → index 语义稳定）；geom 为 None/缺省时不改几何。
    - note 为 None 时不改备注，否则按 _clean_note 规则清洗并写入。
    - expected_revision（Stage 3c-1 CAS）：提供且与当前 revision 不符 → 抛
      RevisionConflict（携带 current_revision）；不提供则不校验（旧行为兼容）。
    - 修改前把旧快照 append 进 roi['history']（上限 20，丢最旧）。
    - 返回更新后的 roi dict（含 index，按 token 内序号计算，同 list_rois 逻辑）。
    """
    # 几何基本校验（真正按原 type 归一化在锁内做，因需读取 roi 原始 type）
    if geom is not None and not isinstance(geom, dict):
        raise ValueError("geom 需为对象")

    # note 清洗（note 非 None 时在锁外校验长度，失败早抛）
    note_clean = "_UNSET_"  # 哨兵：不修改
    if note is not None:
        note_clean = _clean_note(note)

    def _do(f):
        data = _load_locked(f)
        # 非 admin token 校验 share 有效
        is_admin = (token == ADMIN_TOKEN)
        if not is_admin:
            share = data["shares"].get(token)
            if share is None or not _is_active(share):
                raise ValueError("share invalid")
        # 定位该 token 下第 index 条 roi（跳过 tombstone，docs §4.2）
        same = [i for i, r in enumerate(data["rois"])
                if r["token"] == token and not r.get("deleted")]
        if index < 0 or index >= len(same):
            return False
        real_i = same[index]
        roi = data["rois"][real_i]
        _check_cas(roi, expected_revision)  # CAS 在修改前校验
        orig_type = roi.get("type", "rect")

        # 修改历史快照（修改前）
        _append_history(roi)

        # 几何更新：用原 type 归一化后 merge（type 保持原值，ts 不动）
        if geom is not None:
            geom_full = dict(geom)
            # 补齐 size_mm（rect 需要，缺失时用原值）
            if orig_type == "rect" and "size_mm" not in geom_full:
                geom_full["size_mm"] = roi.get("size_mm", 0.0)
            norm_g = _validate_geom(orig_type, geom_full)
            norm_g["type"] = orig_type
            roi.update(norm_g)

        # 备注更新
        if note_clean != "_UNSET_":
            roi["note"] = note_clean

        # docs §4.2：任何编辑都递增 revision + 切片级 change_seq（锁内分配）
        roi["revision"] = int(roi.get("revision") or 1) + 1
        roi["change_seq"] = _bump_change_seq(data, roi.get("slide"))
        roi["updated_at"] = time.time()

        _save_locked(f, data)

        # 返回更新后的 roi dict（含 index）
        out = _roi_out(roi)
        # index 按 token 内序号计算（同 list_rois 逻辑）
        all_same = [r for r in data["rois"] if r["token"] == token and not r.get("deleted")]
        out["index"] = all_same.index(roi)
        out["shared"] = _roi_shared_compat(roi)
        return out

    return _with_lock("r+", _do)


def list_rois(token=None):
    """返回 ROI 列表；可按 token 过滤（跳过 tombstone，docs §4.2）。

    每项含 index（该 token 下的序号）与 shared（按兼容规则归一为 bool）。
    """
    def _do(f):
        data = _load_locked(f)
        rois = _visible(data)
        if token is not None:
            rois = [r for r in rois if r["token"] == token]
            # 计算 index：原列表中同 token 的顺序序号
            all_same = [r for r in _visible(data) if r["token"] == token]
            idx_map = {}
            for i, r in enumerate(all_same):
                idx_map[id(r)] = i
            out = []
            for r in rois:
                rr = dict(r)
                rr["index"] = idx_map.get(id(r), 0)
                rr["shared"] = _roi_shared_compat(r)
                rr["note"] = r.get("note", "")
                out.append(rr)
            out.sort(key=lambda x: x.get("ts", 0), reverse=True)
            return out
        # 全部：按 token 分组计算 index
        from collections import defaultdict
        counters = defaultdict(int)
        out = []
        for r in rois:
            rr = dict(r)
            rr["index"] = counters[r["token"]]
            counters[r["token"]] += 1
            rr["shared"] = _roi_shared_compat(r)
            rr["note"] = r.get("note", "")
            out.append(rr)
        out.sort(key=lambda x: x.get("ts", 0), reverse=True)
        return out

    return _with_lock("r+", _do)


def get_roi(token, index):
    """返回该 token 下第 index 条 roi 的 dict 副本（跳过 tombstone）；不存在返回 None。

    供分享端做设备归属校验（visitor 字段）使用。index 语义与 list_rois 的
    counters 完全一致（该 token 下按文件插入顺序的序号，tombstone 不计入）。
    返回副本含 visitor 字段（旧数据缺省空串），含 index。
    """
    def _do(f):
        data = _load_locked(f)
        same = [i for i, r in enumerate(data["rois"])
                if r["token"] == token and not r.get("deleted")]
        if index < 0 or index >= len(same):
            return None
        roi = data["rois"][same[index]]
        out = dict(roi)
        out["index"] = index
        out["shared"] = _roi_shared_compat(roi)
        out["visitor"] = roi.get("visitor", "") or ""
        out["note"] = roi.get("note", "")
        return out

    return _with_lock("r+", _do)


def get_roi_by_annotation_id(annotation_id):
    """按稳定 annotation_id 取 ROI 完整 dict（含 tombstone）；不存在返回 None。

    fork 根标注定位 / 变更追踪用（docs §4.2）。旧数据（已删）找不到返回 None。
    """
    def _do(f):
        data = _load_locked(f)
        for r in data["rois"]:
            if r.get("annotation_id") == annotation_id:
                return dict(r)
        return None

    return _with_lock("r+", _do)


def rehash_plaintext_visitors(token, plaintext_vid, hashed_vid):
    """当前 token 的 share 仍 active 且确有匹配明文时，原子迁移所有 token 的相同 visitor。

    必须在本文件锁内先验证 share 存在、未撤销、未过期，再改写 ROI：
    `_bind_visitor` 早于路由 `_require_share`，只在 Flask 层 get_share 会留下
    撤销并发窗口。签名 HMAC 不绑定 token，故证明通过后仍全局迁移（含 tombstone）。
    不 bump revision。返回当前 token 下活 ROI 迁移条数；未通过则 0 且不改写。
    """
    if not token or not plaintext_vid or not hashed_vid:
        return 0
    if not isinstance(plaintext_vid, str) or not isinstance(hashed_vid, str):
        return 0
    if plaintext_vid.startswith("h1.") or hashed_vid == plaintext_vid:
        return 0

    def _do(f):
        data = _load_locked(f)
        share = (data.get("shares") or {}).get(token)
        if share is None or not _is_active(share):
            return 0
        matched = []
        current_live = 0
        for r in data.get("rois") or []:
            if (r.get("visitor") or "") != plaintext_vid:
                continue
            matched.append(r)
            if r.get("token") == token and not r.get("deleted"):
                current_live += 1
        if current_live == 0:
            return 0
        for r in matched:
            r["visitor"] = hashed_vid
        _save_locked(f, data)
        return current_live

    return _with_lock("r+", _do)


def delete_roi(token, index, expected_revision=None):
    """删除该 token 下第 index 条 ROI；返回是否删除成功。

    docs §4.2【v3】：物理删除改为置 tombstone（deleted=true + deleted_at + 递增
    revision/change_seq，产生 spot_deleted 事件）。重复删除（已 deleted=true 的）
    是 no-op，不再递增。返回 (bool, annotation_id|None)。
    expected_revision（Stage 3c-1 CAS）：提供且与当前 revision 不符 → 抛
    RevisionConflict；不提供则不校验（旧行为兼容）。
    """
    def _do(f):
        data = _load_locked(f)
        same = [i for i, r in enumerate(data["rois"])
                if r["token"] == token and not r.get("deleted")]
        if index < 0 or index >= len(same):
            return False, None
        real_i = same[index]
        roi = data["rois"][real_i]
        if roi.get("deleted"):
            return False, None
        _check_cas(roi, expected_revision)  # CAS 在删除前校验
        _append_history(roi)  # 修改历史快照（删除前）
        roi["deleted"] = True
        roi["deleted_at"] = time.time()
        roi["revision"] = int(roi.get("revision") or 1) + 1
        roi["change_seq"] = _bump_change_seq(data, roi.get("slide"))
        roi["updated_at"] = roi["deleted_at"]
        _save_locked(f, data)
        return True, roi.get("annotation_id")

    return _with_lock("r+", _do)


def delete_roi_by_annotation_id(annotation_id, expected_revision=None):
    """按稳定 annotation_id 删除（tombstone 语义同 delete_roi）；返回是否成功。

    expected_revision（Stage 3c-1 CAS）：提供且与当前 revision 不符 → 抛
    RevisionConflict；不提供则不校验。
    """
    def _do(f):
        data = _load_locked(f)
        for r in data["rois"]:
            if r.get("annotation_id") == annotation_id and not r.get("deleted"):
                _check_cas(r, expected_revision)
                _append_history(r)
                r["deleted"] = True
                r["deleted_at"] = time.time()
                r["revision"] = int(r.get("revision") or 1) + 1
                r["change_seq"] = _bump_change_seq(data, r.get("slide"))
                r["updated_at"] = r["deleted_at"]
                _save_locked(f, data)
                return True
        return False

    return _with_lock("r+", _do)


def list_changes(slide, after_seq):
    """返回 change_seq > after_seq 的全部变更（含 tombstone，docs §4.2）。

    内部接口，供 session 做 spot 增量注入（§8.4）；不进入 UI 标注层。
    Stage 3c-1：含评论增删（type=comment）与标注变更（type=annotation）；tombstone
    标注走 _roi_out 最小字段输出。
    """
    if not isinstance(after_seq, (int, float)):
        after_seq = 0

    def _do(f):
        data = _load_locked(f)
        out = []
        for r in data["rois"]:
            if r.get("slide") != slide:
                continue
            cs = r.get("change_seq")
            if cs is None or not isinstance(cs, (int, float)) or cs <= after_seq:
                continue
            rr = _roi_out(r)
            rr.setdefault("type", "annotation")
            out.append(rr)
        # 评论增删事件（type=comment）
        for c in data.get("comments", []):
            if c.get("slide") != slide:
                continue
            cs = c.get("change_seq")
            if cs is None or not isinstance(cs, (int, float)) or cs <= after_seq:
                continue
            cc = dict(c)
            cc["type"] = "comment"
            out.append(cc)
        out.sort(key=lambda x: x.get("change_seq", 0))
        return out

    return _with_lock("r+", _do)


def current_change_seq(slide):
    """返回某切片当前的全局 change_seq 水位（无则 0）。"""
    def _do(f):
        data = _load_locked(f)
        seq_map = data.get("change_seq_by_slide") or {}
        cur = seq_map.get(slide)
        return int(cur) if isinstance(cur, (int, float)) else 0

    return _with_lock("r+", _do)


def set_roi_shared(token, index, shared, expected_revision=None):
    """设置该 token 下第 index 条 ROI 的 shared 字段（跳过 tombstone）。

    返回是否设置成功（token/index 无效时返回 False，不抛异常）。
    shared 会被归一为 bool 并持久化。
    expected_revision（Stage 3c-1 CAS）：提供且与当前 revision 不符 → 抛
    RevisionConflict；不提供则不校验（旧行为兼容）。
    """
    shared_b = bool(shared)

    def _do(f):
        data = _load_locked(f)
        same = [i for i, r in enumerate(data["rois"])
                if r["token"] == token and not r.get("deleted")]
        if index < 0 or index >= len(same):
            return False
        roi = data["rois"][same[index]]
        _check_cas(roi, expected_revision)
        roi["shared"] = shared_b
        _save_locked(f, data)
        return True

    return _with_lock("r+", _do)


def roi_count_by_token():
    """返回 {token: count} 计数表（跳过 tombstone）。"""
    def _do(f):
        data = _load_locked(f)
        counts = {}
        for r in data["rois"]:
            if r.get("deleted"):
                continue
            counts[r["token"]] = counts.get(r["token"], 0) + 1
        return counts

    return _with_lock("r+", _do)


def review_roi(token, index, action):
    """Stage 3c-1：AI 标注审核（接受/驳回）。

    action ∈ {accept, reject} → review_status 迁移为 accepted/rejected。
    仅 source=ai 的标注可审（人工标注抛 ValueError）；token/index 无效返回 False。
    成功返回更新后的 roi dict（含 index/review_status/revision），不 bump
    change_seq（审核不进标注变更流；但 bump revision + updated_at 便于 CAS）。
    """
    if action not in ("accept", "reject"):
        raise ValueError("action 需为 accept 或 reject")

    def _do(f):
        data = _load_locked(f)
        same = [i for i, r in enumerate(data["rois"])
                if r["token"] == token and not r.get("deleted")]
        if index < 0 or index >= len(same):
            return False
        roi = data["rois"][same[index]]
        if roi.get("source") != "ai":
            raise ValueError("仅 AI 标注可审核")
        roi["review_status"] = "accepted" if action == "accept" else "rejected"
        roi["revision"] = int(roi.get("revision") or 1) + 1
        roi["updated_at"] = time.time()
        _save_locked(f, data)
        out = _roi_out(roi)
        all_same = [r for r in data["rois"] if r["token"] == token and not r.get("deleted")]
        out["index"] = all_same.index(roi)
        out["shared"] = _roi_shared_compat(roi)
        return out

    return _with_lock("r+", _do)


def list_shared_rois_for_slides(slides):
    """返回 shared 为真（按兼容规则判定）且 slide ∈ slides 的标注列表（跳过 tombstone）。

    供分享端展示「公开标注」使用：包含管理员公开标注与其他用户被管理员公开的标注。
    每项带 index/token/label/type/几何/ts/shared=True，按 token 归组计算 index
    （与 list_rois 的 index 语义一致，便于按 token+index 定位）。
    不传 slides 或空列表时返回空列表。
    """
    if not slides:
        return []
    slide_set = set(slides)

    def _do(f):
        data = _load_locked(f)
        from collections import defaultdict
        counters = defaultdict(int)  # token -> 下一个 index
        out = []
        for r in data["rois"]:
            if r.get("deleted"):
                continue
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

    return _with_lock("r+", _do)


# --------------------------------------------------------------------------- #
# 评论线程（comments）—— Stage 3c-1（docs §5.3）
#
# 标注级评论：挂在某条 annotation 下（annotation_id），一层回复（parent_id 不嵌套）。
# 字段：comment_id(cmt_)、annotation_id、slide、token、author_user_id(可空=guest)、
# author_label(展示名快照)、body(≤2000)、parent_id(可空)、resolved/deleted bool、
# created_at/updated_at、change_seq（变更流用）。
# 增删 bump change_seq（per-slide 计数器，同 rois），list_changes 以 type=comment 返回。
# --------------------------------------------------------------------------- #
def _clean_comment_body(body):
    """归一化评论正文：非 str → ""；strip 后 ≤2000，否则抛 ValueError。"""
    if body is None:
        return ""
    if not isinstance(body, str):
        return ""
    b = body.strip()
    if len(b) > 2000:
        raise ValueError("评论正文过长（≤2000 字）")
    return b


def add_comment(annotation_id, slide, token, body, author_user_id=None,
                author_label="", parent_id=None, requester_role=None):
    """新增评论；返回 comment dict（含 comment_id/change_seq）。

    annotation_id 为挂靠的标注稳定 id；token 为归属上下文（admin 伪 token 或分享
    token）；author_user_id 为空表示 guest；author_label 为展示名快照。
    parent_id 为回复目标评论 id（一层回复，不嵌套）。
    requester_role：显式传 "guest" 不拒绝（评论是 guest 合法操作；仓储边界只挡
    图库写），保持 None 即可。
    """
    body_clean = _clean_comment_body(body)
    if not body_clean:
        raise ValueError("评论正文不能为空")
    now = time.time()
    cmt = {
        "comment_id": "cmt_" + uuid.uuid4().hex,
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

    def _do(f):
        data = _load_locked(f)
        cmt["change_seq"] = _bump_change_seq(data, slide)
        data["comments"].append(cmt)
        _save_locked(f, data)
        return dict(cmt)

    return _with_lock("r+", _do)


def list_comments(annotation_id=None, slide=None):
    """返回评论列表（跳过软删 deleted）。

    可按 annotation_id 或 slide 过滤（两者可组合：同切片下某标注的评论）。
    不传任何过滤 → 全部。按 created_at 升序（线程阅读顺序）。
    """
    def _do(f):
        data = _load_locked(f)
        out = []
        for c in data.get("comments", []):
            if c.get("deleted"):
                continue
            if annotation_id is not None and c.get("annotation_id") != annotation_id:
                continue
            if slide is not None and c.get("slide") != slide:
                continue
            out.append(dict(c))
        out.sort(key=lambda x: x.get("created_at", 0))
        return out

    return _with_lock("r+", _do)


def resolve_comment(comment_id, resolved=True):
    """设置评论 resolved 状态；返回是否成功（评论不存在/已软删 → False）。"""
    def _do(f):
        data = _load_locked(f)
        for c in data.get("comments", []):
            if c.get("comment_id") == comment_id and not c.get("deleted"):
                c["resolved"] = bool(resolved)
                c["updated_at"] = time.time()
                _save_locked(f, data)
                return True
        return False

    return _with_lock("r+", _do)


def delete_comment(comment_id):
    """软删评论（deleted=true + bump change_seq）；返回是否成功。

    重复软删（已 deleted）是 no-op，不再递增 change_seq。
    """
    def _do(f):
        data = _load_locked(f)
        for c in data.get("comments", []):
            if c.get("comment_id") == comment_id and not c.get("deleted"):
                c["deleted"] = True
                c["updated_at"] = time.time()
                c["change_seq"] = _bump_change_seq(data, c.get("slide"))
                _save_locked(f, data)
                return True
        return False

    return _with_lock("r+", _do)


# --------------------------------------------------------------------------- #
# 样本元数据（别名/备注）—— shares.json 顶层 slide_meta
# --------------------------------------------------------------------------- #
def set_slide_meta(name, alias=None, note=None, owner_user_id=None, public=None,
                   requester_role=None):
    """设置/更新某切片的别名与备注。

    alias/note 为 None 表示不改该项；空串表示清除该项。
    name 不存在时仍写入（便于先建别名后传文件）；返回更新后的 meta dict。
    owner_user_id 为创建者归属（可选；缺省用已注入的 owner id）。
    public：None 不改；True/False 设置公开档位（仅 owner 可改，由 app.py 强制）。
    requester_role：显式传 "guest" 时拒绝（仓储边界 defense-in-depth）。
    """
    _reject_guest_write(requester_role)

    def _do(f):
        data = _load_locked(f)
        meta_map = data.setdefault("slide_meta", {})
        if not isinstance(meta_map, dict):
            meta_map = {}
            data["slide_meta"] = meta_map
        cur = meta_map.get(name)
        if not isinstance(cur, dict):
            cur = {}
        if alias is not None:
            a = alias.strip() if isinstance(alias, str) else ""
            if a:
                cur["alias"] = a
            else:
                cur.pop("alias", None)
        if note is not None:
            n = note.strip() if isinstance(note, str) else ""
            if n:
                cur["note"] = n
            else:
                cur.pop("note", None)
        if public is not None:
            cur["public"] = bool(public)
        if cur.get("owner_user_id") is None:
            cur["owner_user_id"] = owner_user_id or _OWNER_USER_ID or None
        if cur:
            meta_map[name] = cur
        else:
            meta_map.pop(name, None)
        _save_locked(f, data)
        return dict(cur)

    return _with_lock("r+", _do)


def get_slide_meta(name):
    """返回某切片的 {alias, note}（无则空 dict，保证字段存在为空串）。"""
    def _do(f):
        data = _load_locked(f)
        meta_map = data.get("slide_meta", {})
        cur = meta_map.get(name) if isinstance(meta_map, dict) else None
        if not isinstance(cur, dict):
            return {"alias": "", "note": ""}
        return {"alias": cur.get("alias", ""), "note": cur.get("note", "")}

    return _with_lock("r+", _do)


def get_slide_meta_full(name):
    """返回某切片的完整 meta（含 owner_user_id / public），供鉴权矩阵判定。

    无记录返回 {alias:"", note:"", owner_user_id:None, public:False}。
    """
    def _do(f):
        data = _load_locked(f)
        meta_map = data.get("slide_meta", {})
        cur = meta_map.get(name) if isinstance(meta_map, dict) else None
        if not isinstance(cur, dict):
            return {"alias": "", "note": "", "owner_user_id": None, "public": False}
        return {
            "alias": cur.get("alias", ""),
            "note": cur.get("note", ""),
            "owner_user_id": cur.get("owner_user_id"),
            "public": bool(cur.get("public")),
        }

    return _with_lock("r+", _do)


def get_all_slide_meta_full():
    """返回全量 {name: {alias, note, owner_user_id, public}}（鉴权矩阵批量判定用）。"""
    def _do(f):
        data = _load_locked(f)
        meta_map = data.get("slide_meta", {})
        if not isinstance(meta_map, dict):
            return {}
        out = {}
        for k, v in meta_map.items():
            if not isinstance(v, dict):
                continue
            out[k] = {
                "alias": v.get("alias", ""),
                "note": v.get("note", ""),
                "owner_user_id": v.get("owner_user_id"),
                "public": bool(v.get("public")),
            }
        return out

    return _with_lock("r+", _do)


def get_all_slide_meta():
    """返回全量 {name: {alias, note}}。"""
    def _do(f):
        data = _load_locked(f)
        meta_map = data.get("slide_meta", {})
        if not isinstance(meta_map, dict):
            return {}
        out = {}
        for k, v in meta_map.items():
            if not isinstance(v, dict):
                continue
            out[k] = {"alias": v.get("alias", ""), "note": v.get("note", "")}
        return out

    return _with_lock("r+", _do)


# --------------------------------------------------------------------------- #
# 项目（projects）—— 仅维护切片归属关系，不移动/删除切片文件
# --------------------------------------------------------------------------- #
def create_project(name, note="", slides=None, owner_user_id=None, requester_role=None):
    """创建项目。pid=secrets.token_urlsafe(10)。

    slides 在此只做去重，是否为已存在切片由调用方保证。
    owner_user_id 为创建者归属（可选；缺省用已注入的 owner id，否则留空）。
    返回新建项目 dict（含 pid）。
    requester_role：显式传 "guest" 时拒绝（仓储边界 defense-in-depth）。
    """
    _reject_guest_write(requester_role)
    pid = secrets.token_urlsafe(10)
    now = time.time()
    # 去重（保序）
    seen = set()
    uniq = []
    for s in slides or []:
        if isinstance(s, str) and s not in seen:
            seen.add(s)
            uniq.append(s)
    project = {
        "name": str(name or "").strip() or "未命名项目",
        "note": str(note or ""),
        "slides": uniq,
        "created_at": now,
        "owner_user_id": owner_user_id or _OWNER_USER_ID or None,
        "archived": False,  # Stage 3c-2：归档纯只读开关，默认未归档
    }

    def _do(f):
        data = _load_locked(f)
        data["projects"][pid] = dict(project)
        _save_locked(f, data)
        out = dict(project)
        out["pid"] = pid
        return out

    return _with_lock("r+", _do)


def list_projects():
    """返回全部项目列表，每项附加 pid、slide_count；按 created_at 倒序。"""
    def _do(f):
        data = _load_locked(f)
        items = []
        for pid, proj in data["projects"].items():
            out = dict(proj)
            out["pid"] = pid
            out["slide_count"] = len(out.get("slides", []))
            items.append(out)
        items.sort(key=lambda x: x.get("created_at", 0), reverse=True)
        return items

    return _with_lock("r+", _do)


def get_project(pid):
    """返回单个项目 dict（附加 pid）；不存在返回 None。"""
    def _do(f):
        data = _load_locked(f)
        proj = data["projects"].get(pid)
        if proj is None:
            return None
        out = dict(proj)
        out["pid"] = pid
        return out

    return _with_lock("r+", _do)


def update_project(pid, *, name=None, note=None, slides=None):
    """更新项目字段（仅更新非 None 字段）。slides 传入时去重替换。
    返回更新后的项目 dict；不存在返回 None。
    """
    def _do(f):
        data = _load_locked(f)
        proj = data["projects"].get(pid)
        if proj is None:
            return None
        if name is not None:
            proj["name"] = str(name).strip() or proj.get("name", "未命名项目")
        if note is not None:
            proj["note"] = str(note)
        if slides is not None:
            seen = set()
            uniq = []
            for s in slides:
                if isinstance(s, str) and s not in seen:
                    seen.add(s)
                    uniq.append(s)
            proj["slides"] = uniq
        _save_locked(f, data)
        out = dict(proj)
        out["pid"] = pid
        return out

    return _with_lock("r+", _do)


def add_slides_to_project(pid, slides):
    """向项目追加切片（去重保序）。返回更新后的项目 dict；不存在返回 None。"""
    def _do(f):
        data = _load_locked(f)
        proj = data["projects"].get(pid)
        if proj is None:
            return None
        existing = proj.get("slides", [])
        seen = set(existing)
        for s in slides or []:
            if isinstance(s, str) and s not in seen:
                seen.add(s)
                existing.append(s)
        proj["slides"] = existing
        _save_locked(f, data)
        out = dict(proj)
        out["pid"] = pid
        return out

    return _with_lock("r+", _do)


def remove_slide_from_project(pid, slide):
    """从项目移除某切片。返回更新后的项目 dict；不存在或无该切片返回 None。"""
    def _do(f):
        data = _load_locked(f)
        proj = data["projects"].get(pid)
        if proj is None:
            return None
        slides = proj.get("slides", [])
        if slide not in slides:
            return None
        proj["slides"] = [s for s in slides if s != slide]
        _save_locked(f, data)
        out = dict(proj)
        out["pid"] = pid
        return out

    return _with_lock("r+", _do)


def delete_project(pid):
    """删除项目（仅删项目记录，不动切片文件）。返回是否删除成功。"""
    def _do(f):
        data = _load_locked(f)
        if pid not in data["projects"]:
            return False
        del data["projects"][pid]
        _save_locked(f, data)
        return True

    return _with_lock("r+", _do)


def set_project_archived(pid, archived):
    """Stage 3c-2（docs §v1.5）：设置项目 archived 纯只读开关。

    archived=True → 该项目切片对所有身份（含 owner）只读（解除归档才可写）。
    返回更新后的项目 dict；不存在返回 None。
    """
    archived_b = bool(archived)
    def _do(f):
        data = _load_locked(f)
        proj = data["projects"].get(pid)
        if proj is None:
            return None
        proj["archived"] = archived_b
        _save_locked(f, data)
        out = dict(proj)
        out["pid"] = pid
        return out

    return _with_lock("r+", _do)


def archived_slide_names():
    """返回属于任意 archived 项目的切片名集合（归档只读判定用）。

    某切片只要出现在任一归档项目内即视为只读（不区分来源项目）。
    """
    def _do(f):
        data = _load_locked(f)
        out = set()
        for proj in data.get("projects", {}).values():
            if isinstance(proj, dict) and proj.get("archived"):
                for s in proj.get("slides", []):
                    if isinstance(s, str):
                        out.add(s)
        return out

    return _with_lock("r+", _do)


# --------------------------------------------------------------------------- #
# 标注（annotations）汇总 —— 把 rois 按 slide/label 聚合，供管理员查看
# --------------------------------------------------------------------------- #
def _norm_label(label):
    """读旧 roi 缺 label 时视为「未署名」。"""
    if isinstance(label, str) and label.strip():
        return label.strip()
    return "未署名"


def annotations_by_slide():
    """把全部 rois 按 slide 分组聚合。

    返回 {slide: {label: {"label","count","items":[...]}, ...}}，items 含
    index/token/slide/x/y/size_mm/side_px/ts/type 及 arrow/freehand 的几何字段。
    index 为该 token 的 rois 按文件插入顺序的序号（同 list_rois 的 counters
    逻辑），与 delete_roi / set_roi_shared / update_roi 的 index 语义完全一致，
    前端可直接用于 DELETE/PATCH /api/annotation/<token>/<index>。
    结构是嵌套：slide -> label -> group。为方便前端，外层每个 slide 的值是按
    label 的分组列表。
    """
    def _do(f):
        data = _load_locked(f)
        from collections import defaultdict
        counters = defaultdict(int)  # token -> 下一个 index（按文件内出现顺序）
        # slide -> label -> {label, count, items}
        by_slide = {}
        for r in data["rois"]:
            if r.get("deleted"):
                continue  # tombstone 不进 UI 标注层 / spot 索引（docs §4.2）
            slide = r.get("slide")
            lbl = _norm_label(r.get("label"))
            grp_map = by_slide.setdefault(slide, {})
            grp = grp_map.get(lbl)
            if grp is None:
                grp = {"label": lbl, "count": 0, "items": []}
                grp_map[lbl] = grp
            grp["count"] += 1
            # index：该 token 下按文件插入顺序的序号（同 list_rois 的 counters 逻辑）
            tok = r.get("token")
            idx = counters[tok]
            counters[tok] += 1
            item = {
                "index": idx,
                "token": tok,
                # slide 字段也带上：前端兜底反推 index（旧缓存无 index 时）需要
                "slide": r.get("slide"),
                "type": r.get("type", "rect"),  # 旧数据无 type 视为 rect
                "x": r.get("x"),
                "y": r.get("y"),
                "size_mm": r.get("size_mm"),
                "side_px": r.get("side_px"),
                "ts": r.get("ts"),
                "shared": _roi_shared_compat(r),
                "note": r.get("note", ""),
                # docs §4.2：稳定 ID / 来源，供 fork 批注挂 💬（旧数据兼容默认值）
                "annotation_id": r.get("annotation_id"),
                "source": r.get("source", "human"),
                "created_by_session_id": r.get("created_by_session_id", ""),
                "change_seq": r.get("change_seq"),
                "revision": r.get("revision", 1),
                # Stage 3c-1：AI 审核状态（none|pending|accepted|rejected；旧数据缺省 none）
                "review_status": r.get("review_status", "none"),
                # 设备标识短码：同链接不同设备在管理端可区分（旧数据缺省空）
                "visitor": (r.get("visitor") or "")[:8],
            }
            # 带上 arrow / freehand 专属几何字段（存在则透传）
            for k in ("x1", "y1", "x2", "y2", "points"):
                if k in r:
                    item[k] = r[k]
            grp["items"].append(item)
        # 转为 slide -> list[group]（label 按出现顺序）
        result = {}
        for slide, grp_map in by_slide.items():
            result[slide] = list(grp_map.values())
        return result

    return _with_lock("r+", _do)


def annotations_by_project(pid=None):
    """与 annotations_by_slide 同结构，但可选按项目内的 slides 过滤。

    pid=None 时等同于 annotations_by_slide()。
    pid 存在但项目不存在则按空 slides 过滤（返回空）。
    """
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
# 稳定 slide 身份 —— JSON 兼容 shim（Stage 3b-2）
#
# JSON 后端没有稳定 slide_id（legacy filename 就是身份，可重名/可重命名），只提供
# 同名 shim 保持 dispatcher 公共名一致（纯兼容形状）。真正的稳定 id 由
# `share_store_pg`（PostgreSQL 后端）实现，见 docs §Stage 3b。
# --------------------------------------------------------------------------- #
def get_slide_id(name):
    """JSON 后端无稳定 slide id：返回 None（纯兼容形状）。"""
    return None


def resolve_slide_ref(name):
    """JSON 后端无稳定 slide id：返回原 name（纯兼容形状，调用方按字符串用）。"""
    return name


def record_slide_asset(slide_id, legacy_revision):
    """JSON 后端无 slide_assets 表：返回 None（纯兼容形状）。"""
    return None


# --------------------------------------------------------------------------- #
# 审计日志（audit_events）—— Stage 3c-2（docs §5.3/§6.4）
#
# 协作操作日志（非医疗审计）：分享建/撤/claim、标注增删改/审核、评论增删、
# 用户建/禁/启、AI 起跑（ai.run）、分享访问（share.access）。绝不在 detail 里写
# 密钥/明文密码（脱敏见测试 test_audit_events.py）。
#
# record_audit 为 best-effort：**本函数内部**吞掉一切写失败（返回 False），调用方
# 无需用 try 包裹即可安全调用、不阻断主流程。理由：审计是辅助记录，不应因日志失败
# 破坏业务主链路；且 app.py / share_server.py 是在各自端点**写完业务后**再独立调用
# 本函数（不在业务锁内嵌套调用），从根上规避死锁，见各调用处注释。
# --------------------------------------------------------------------------- #
AUDIT_MAX_EVENTS = 5000


def record_audit(action, actor_user_id=None, actor_role=None, target_type=None,
                 target_id=None, slide=None, detail=None, ts=None):
    """best-effort 追加一条审计事件；写失败吞掉返回 False，绝不抛异常。

    action 为枚举字符串（见 docs：share.create/share.revoke/share.claim/
    share.access/annotation.add/annotation.update/annotation.delete/review/
    comment.add/comment.delete/user.create/user.disable/user.enable/ai.run）。
    detail 为少量上下文 dict（默认 {}）；**绝不在此放 api_key / 明文密码**。
    """
    ev = {
        "id": "aud_" + uuid.uuid4().hex,
        "ts": ts if ts is not None else time.time(),
        "actor_user_id": actor_user_id or None,
        "actor_role": actor_role or "",
        "action": str(action or ""),
        "target_type": target_type or None,
        "target_id": target_id or None,
        "slide": slide or None,
        "detail": dict(detail) if isinstance(detail, dict) else {},
    }

    def _do(f):
        data = _load_locked(f)
        events = data.setdefault("audit", [])
        if not isinstance(events, list):
            events = []
            data["audit"] = events
        events.append(ev)
        if len(events) > AUDIT_MAX_EVENTS:
            del events[: len(events) - AUDIT_MAX_EVENTS]
        _save_locked(f, data)
        return True

    try:
        return _with_lock("r+", _do)
    except Exception:
        return False


def list_audit(limit=50, offset=0, action=None):
    """返回审计事件（最新在前），支持分页与 action 过滤。owner-only 消费（app.py 鉴权）。

    limit 默认 50，offset 默认 0；action 传字符串时精确过滤。
    """
    limit = max(0, int(limit if limit is not None else 50))
    offset = max(0, int(offset if offset is not None else 0))

    def _do(f):
        data = _load_locked(f)
        events = data.get("audit") or []
        if not isinstance(events, list):
            events = []
        if action:
            events = [e for e in events if e.get("action") == action]
        # 最新在前（按 ts 倒序，同 ts 按插入倒序）
        events = list(reversed(events))
        return events[offset:offset + limit]

    return _with_lock("r+", _do)


# --------------------------------------------------------------------------- #
# 插件安装凭证（plugin_installations）—— Stage 4-1a（docs §7.6 / §6.2）
#
# 安装凭证（installation secret）是插件后端调 /api/plugin/v1/auth/token 换
# scoped JWT 的长期凭证：只存 sha256 hash（secret_hash），明文仅在创建与
# 轮换时随返回值出现一次，绝不落盘、绝不入日志。禁用（enabled=false）后
# 其签发的 token 立即不可用（app.py 每次校验回查 enabled）。
# 本实体是平台运行时状态，但 json 仍是默认后端（AUTH_ENABLED=False 内网零
# 依赖红线），故与 pg 双实现（见 share_store_pg / migrations/0005_plugin.sql）。
# --------------------------------------------------------------------------- #
def _hash_installation_secret(secret: str) -> str:
    """安装凭证明文 → sha256 hex（存储形态）。"""
    return hashlib.sha256((secret or "").encode("utf-8")).hexdigest()


def _installation_out(row: dict) -> dict:
    """installation 导出副本：剥离 secret_hash（hash 不出存储层）。

    capabilities（插件能力注册表，docs §4.1）缺省补 []——旧安装行没有该字段，
    读侧一律拿到 list（兼容 0011 迁移前的旧行）。
    """
    out = dict(row)
    out.pop("secret_hash", None)
    out["enabled"] = bool(row.get("enabled"))
    caps = out.get("capabilities")
    out["capabilities"] = [c for c in caps if isinstance(c, dict)] if isinstance(caps, list) else []
    return out


def create_plugin_installation(plugin_id, version="", secret=None,
                               capabilities=None):
    """创建插件安装行，返回 {**installation, "secret": 明文}（仅此一次）。

    plugin_id 必填（如 "histopilot"）；version 缺省空串；secret 可显式传入
    （env 引导用），否则生成 "pin_" + 32 字节 urlsafe；capabilities 为可选的
    能力注册表登记项（安装时解析 manifest.provides 而来，docs §4.1；缺省 []）。
    installation_id 形如 pin_<12 字节 urlsafe>。同 plugin_id 允许多行（不做
    唯一约束，引导逻辑由 app.py 保证单实例 demo 只有一行）。
    """
    if not isinstance(plugin_id, str) or not plugin_id.strip():
        raise ValueError("plugin_id 不能为空")
    plaintext = secret if isinstance(secret, str) and secret else (
        "pin_" + secrets.token_urlsafe(32))
    installation_id = "pin_" + secrets.token_urlsafe(12)
    now = time.time()
    row = {
        "installation_id": installation_id,
        "plugin_id": plugin_id.strip(),
        "version": version or "",
        "enabled": True,
        "secret_hash": _hash_installation_secret(plaintext),
        "created_at": now,
        "disabled_at": None,
        "capabilities": [dict(c) for c in capabilities]
        if isinstance(capabilities, list) else [],
    }

    def _do(f):
        data = _load_locked(f)
        data["plugin_installations"].append(row)
        _save_locked(f, data)
        out = _installation_out(row)
        out["secret"] = plaintext
        return out

    return _with_lock("r+", _do)


def rotate_installation_secret(installation_id, secret=None):
    """轮换安装凭证：旧 secret 立即失效，返回带新明文的一次性 dict。

    不存在返回 None。secret 缺省时生成新随机值。
    """
    plaintext = secret if isinstance(secret, str) and secret else (
        "pin_" + secrets.token_urlsafe(32))
    new_hash = _hash_installation_secret(plaintext)

    def _do(f):
        data = _load_locked(f)
        for row in data["plugin_installations"]:
            if row.get("installation_id") == installation_id:
                row["secret_hash"] = new_hash
                _save_locked(f, data)
                out = _installation_out(row)
                out["secret"] = plaintext
                return out
        return None

    return _with_lock("r+", _do)


def get_plugin_installation(installation_id):
    """按 installation_id 取安装行（不含 secret_hash）；无则 None。"""
    def _do(f):
        data = _load_locked(f)
        for row in data["plugin_installations"]:
            if row.get("installation_id") == installation_id:
                return _installation_out(row)
        return None

    return _with_lock("r+", _do)


def verify_installation_secret(installation_id, secret):
    """校验安装凭证：行存在且 hash 一致才 True（常数时间比较）。

    安装已禁用时凭证仍可校验通过（hash 本身没错），否决发生在 app.py 的
    enabled 回查——这里保持纯凭证语义，便于引导/诊断复用。
    """
    if not isinstance(secret, str) or not secret:
        return False
    candidate = _hash_installation_secret(secret)

    def _do(f):
        data = _load_locked(f)
        for row in data["plugin_installations"]:
            if row.get("installation_id") == installation_id:
                return hmac.compare_digest(
                    str(row.get("secret_hash") or ""), candidate)
        return False

    return _with_lock("r+", _do)


def set_installation_enabled(installation_id, enabled):
    """启/禁安装。返回更新后的安装行（不含 hash）；不存在返回 None。

    禁用时记 disabled_at；重新启用清空 disabled_at。禁用即撤销该安装全部
    在途 JWT（app.py 每次校验回查 enabled）。
    """
    enabled_b = bool(enabled)

    def _do(f):
        data = _load_locked(f)
        for row in data["plugin_installations"]:
            if row.get("installation_id") == installation_id:
                row["enabled"] = enabled_b
                row["disabled_at"] = None if enabled_b else time.time()
                _save_locked(f, data)
                return _installation_out(row)
        return None

    return _with_lock("r+", _do)


def list_plugin_installations():
    """列出全部安装行（不含 hash），按创建时间升序。"""
    def _do(f):
        data = _load_locked(f)
        rows = [r for r in data["plugin_installations"] if isinstance(r, dict)]
        rows.sort(key=lambda r: r.get("created_at") or 0)
        return [_installation_out(r) for r in rows]

    return _with_lock("r+", _do)


def set_installation_capabilities(installation_id, capabilities):
    """整体替换安装行的能力注册表（docs §4.1：安装/启用时解析 provides 登记）。

    capabilities 为能力登记项数组（空数组 = 清空登记，如 manifest 不再声明
    provides）。返回更新后的安装行（不含 hash）；不存在返回 None。
    """
    caps = [dict(c) for c in capabilities] if isinstance(capabilities, list) else []

    def _do(f):
        data = _load_locked(f)
        for row in data["plugin_installations"]:
            if row.get("installation_id") == installation_id:
                row["capabilities"] = caps
                _save_locked(f, data)
                return _installation_out(row)
        return None

    return _with_lock("r+", _do)


# --------------------------------------------------------------------------- #
# run grant（run_grants）—— Stage 4-1a（docs §7.6）
#
# 用户起跑时平台发放的短期授权：绑定 installation + slide（+ 将来确定后的
# session_id）、创建人与过期时间（默认 2h），可撤销。无 org（demo 单实例，
# docs §7.6）。plugin v1 的 annotate 端点强制 X-Run-Grant（有效 + slide 匹配
# + 未过期未撤销），provenance 的 created_by_user_id 取自 grant。
# --------------------------------------------------------------------------- #
def create_run_grant(installation_id, slide, session_id="",
                     created_by_user_id=None, ttl_seconds=None):
    """发放一条 run grant，返回 grant dict。

    ttl_seconds 缺省 7200（2h，docs §7.6「run grant 默认最长 1 小时」的
    demo 宽松值；本节点不缩紧，sidecar 4-1b 消费时按 expires_at 判定）。
    """
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
    row = {
        "grant_id": "rgr_" + secrets.token_urlsafe(12),
        "installation_id": installation_id,
        "slide": slide,
        "session_id": session_id or "",
        "created_by_user_id": created_by_user_id or None,
        "created_at": now,
        "expires_at": now + ttl,
        "revoked": False,
        "revoked_at": None,
    }

    def _do(f):
        data = _load_locked(f)
        data["run_grants"].append(row)
        _save_locked(f, data)
        return dict(row)

    return _with_lock("r+", _do)


def get_run_grant(grant_id):
    """按 grant_id 取 grant dict；无则 None。"""
    def _do(f):
        data = _load_locked(f)
        for row in data["run_grants"]:
            if row.get("grant_id") == grant_id:
                return dict(row)
        return None

    return _with_lock("r+", _do)


def revoke_run_grant(grant_id):
    """撤销 run grant（幂等）。返回是否找到并撤销（已撤销也算 True）。"""
    def _do(f):
        data = _load_locked(f)
        for row in data["run_grants"]:
            if row.get("grant_id") == grant_id:
                if not row.get("revoked"):
                    row["revoked"] = True
                    row["revoked_at"] = time.time()
                    _save_locked(f, data)
                return True
        return False

    return _with_lock("r+", _do)


def list_run_grants_for_session(session_id):
    """列出某 session_id 的全部 grant（含已撤销/过期，按创建时间升序）。

    session_id 为空串时返回空列表（slide 级 grant 不归属任何 session）。
    """
    if not session_id:
        return []

    def _do(f):
        data = _load_locked(f)
        rows = [dict(r) for r in data["run_grants"]
                if isinstance(r, dict) and r.get("session_id") == session_id]
        rows.sort(key=lambda r: r.get("created_at") or 0)
        return rows

    return _with_lock("r+", _do)
