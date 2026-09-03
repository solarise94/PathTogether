# -*- coding: utf-8 -*-
"""切片分享层纯函数/常量（无文件 IO）；json 与 pg 两实现共用。"""

import hashlib
import time


# 支持的标注类型
ROI_TYPES = ("rect", "arrow", "freehand")

# 分享可选的 ROI 矩形标记尺寸（mm），以 float 存储为子集
ALLOWED_ROI_SIZES = (6.0, 6.5)
# 默认标记尺寸子集（未指定时）
DEFAULT_ROI_SIZES = [6.0, 6.5]

# 管理员标注使用的固定 token
ADMIN_TOKEN = "admin"


# --------------------------------------------------------------------------- #
# Stage 3a-2a：分享权限档位与认领（docs §5.4）
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


def _is_active(share):
    """判断 share dict 是否仍有效（未撤销且未过期）。"""
    if share.get("revoked"):
        return False
    exp = share.get("expires_at")
    if exp is not None and exp < time.time():
        return False
    return True


def _status_of(share):
    if share.get("revoked"):
        return "revoked"
    exp = share.get("expires_at")
    if exp is not None and exp < time.time():
        return "expired"
    return "active"


def _grant_out(g):
    """导出 grant 副本（确保字段齐全）。"""
    out = dict(g)
    out.setdefault("permissions", list(DEFAULT_PERMISSIONS))
    out.setdefault("revoked_at", None)
    return out


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


def _norm_label(label):
    """读旧 roi 缺 label 时视为「未署名」。"""
    if isinstance(label, str) and label.strip():
        return label.strip()
    return "未署名"


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