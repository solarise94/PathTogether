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

# --------------------------------------------------------------------------- #
# 升级 C（批次 4a）：通用矩形几何契约（§6.2/§6.3）
# 权威几何 = level-0 像素 x/y/w/h（左上+宽高，轴对齐，不旋转）。
# geometry_version=2 标记 v2 写入（成对 w/h）；v1 旧数据/旧请求只有 side_px，
# 读时归一 w=h=side_px（不批量改存储）。
# --------------------------------------------------------------------------- #
GEOMETRY_VERSION = 2
#: rect 单边像素上限（既有安全界限，§6.2）
RECT_MAX_SIDE_PX = 40000


def _rect_max_pixels():
    """rect 标注的 w*h 像素预算上限（与 crop 单请求硬闸同源，§6.2）。

    延迟 import：crop_guard 在 import 期读 env，收敛到调用期避免模块加载
    顺序耦合；预算值与主站/分享 crop 共用同一实现（防两份漂移）。
    """
    try:
        import crop_guard
        return int(crop_guard.CROP_MAX_PIXELS)
    except Exception:  # pragma: no cover - crop_guard 不可用时退化为同值常量
        return 4096 ** 2


def _int_px(v, name):
    """把 v 校验为有限数值并取整；非法抛 ValueError（bool 不算数值）。"""
    if not _is_finite_num(v):
        raise ValueError("几何参数需为数值（%s=%r 非法）" % (name, v))
    return int(v)

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


def _validate_rect_geometry(geom):
    """升级 C（§6.3）：rect 几何归一化与校验（成对 w/h 权威 + side_px 兼容）。

    规则：
      - v2 写入：x/y/w/h（w/h 必须成对；只给其一直接拒绝）；
        w/h ∈ 1..RECT_MAX_SIDE_PX，且 w*h ≤ rect 像素预算（同 crop 硬闸）；
        geometry_version 记 2。
      - v1 兼容写入：只给 side_px（旧客户端）→ 按旧正方形转换（w=h=side_px
        只在读取时归一，存储保持 v1 形态，不批量改写旧记录语义）。
      - v2 与 side_px 同给：仅当 side_px == w == h（一致的正方形）才接受，
        否则拒绝（防矛盾几何）。
      - size_mm：展示值，非权威；非正方形 v2 一律 0.0（不得用单值冒充宽高）。
    返回归一化 dict；校验失败抛 ValueError。
    """
    x = _int_px(geom.get("x"), "x")
    y = _int_px(geom.get("y"), "y")
    if x < 0 or y < 0:
        raise ValueError("坐标需 ≥0")

    w_raw = geom.get("w")
    h_raw = geom.get("h")
    side_raw = geom.get("side_px")
    has_pair = w_raw is not None or h_raw is not None
    has_side = side_raw is not None

    if has_pair:
        if w_raw is None or h_raw is None:
            raise ValueError("w/h 需成对提供（升级 C 矩形契约）")
        w = _int_px(w_raw, "w")
        h = _int_px(h_raw, "h")
        if w < 1 or h < 1 or w > RECT_MAX_SIDE_PX or h > RECT_MAX_SIDE_PX:
            raise ValueError(
                "w/h 需在 1~%d 之间" % RECT_MAX_SIDE_PX)
        max_px = _rect_max_pixels()
        if w * h > max_px:
            raise ValueError(
                "矩形 %d×%d=%d 像素超过单标注预算 %d" % (w, h, w * h, max_px))
        out = {
            "type": "rect",
            "x": x, "y": y, "w": w, "h": h,
            "geometry_version": GEOMETRY_VERSION,
        }
        if has_side:
            side_check = _int_px(side_raw, "side_px")
            # v2 与 side_px 同给：仅接受一致正方形的冗余兼容字段
            if not (side_check == w and side_check == h):
                raise ValueError("side_px 与 w/h 冲突（矛盾几何）")
            out["side_px"] = side_check  # 正方形保留一致 side_px 兼容字段
        size_mm = geom.get("size_mm")
        if w == h and _is_finite_num(size_mm):
            out["size_mm"] = float(size_mm)
        else:
            out["size_mm"] = 0.0
        return out

    # v1 兼容：只有 side_px（旧请求）→ 旧正方形转换
    if not has_side:
        raise ValueError("rect 需成对 w/h（新接口）或 side_px（旧正方形兼容）")
    side_px = _int_px(side_raw, "side_px")
    if side_px < 1 or side_px > RECT_MAX_SIDE_PX:
        raise ValueError("side_px 需在 1~%d 之间" % RECT_MAX_SIDE_PX)
    size_mm = geom.get("size_mm")
    return {
        "type": "rect",
        "x": x, "y": y,
        "side_px": side_px,
        "size_mm": float(size_mm) if _is_finite_num(size_mm) else 0.0,
    }


def _rect_read_compat(roi):
    """读侧兼容（§6.3-1）：旧 rect 只有 side_px → 归一 w=h=side_px。

    只作用于返回副本（调用方传 dict 副本），绝不改存储；不修改
    annotation_id/revision/change_seq/effect_key/审核状态。v2 非正方形
    不补 side_px（不得用 max/min 冒充旧几何）。非 rect 或 tombstone 原样返回。
    """
    if not isinstance(roi, dict) or roi.get("type") != "rect":
        return roi
    if roi.get("deleted"):
        return roi
    if "w" in roi and "h" in roi:
        return roi
    side = roi.get("side_px")
    if _is_finite_num(side):
        side_i = int(side)
        if side_i > 0:
            roi["w"] = side_i
            roi["h"] = side_i
    return roi


def _rect_is_square(geom):
    """判断归一化后的 rect 几何是否正方形（含 v1 side_px 形态）。"""
    if not isinstance(geom, dict):
        return False
    w = geom.get("w")
    h = geom.get("h")
    if w is None or h is None:
        side = geom.get("side_px")
        return _is_finite_num(side) and int(side) > 0
    return int(w) == int(h)


def _normalize_rect_policy(policy):
    """校验/归一化分享 rect_policy（§6.4）：preset_only | custom。

    None → preset_only（旧分享缺字段一律按 preset_only 解释，语义不放宽）。
    非法值抛 ValueError。
    """
    if policy is None:
        return "preset_only"
    if policy not in ("preset_only", "custom"):
        raise ValueError("rect_policy 仅支持 preset_only 或 custom")
    return policy


def _share_rect_policy(share):
    """读 share 的 rect_policy；缺字段（旧分享）一律 preset_only。"""
    policy = share.get("rect_policy") if isinstance(share, dict) else None
    if policy == "custom":
        return "custom"
    return "preset_only"


def _effective_rect_geometry(existing, patch=None):
    """合并既有 rect 与 PATCH geom，返回供策略/边界校验的生效几何 dict。

    - patch 缺省键回落 existing；w/h 按成对校验（只给其一个抛 ValueError）；
    - 旧客户端只给 side_px 编辑 v2 非正方形 → 抛 ValueError（不兼容写）；
    - v1 正方形记录可只给 side_px（旧编辑路径语义保留）。
    返回 {x,y,w,h,geometry_version}（w/h 恒有值；v1 side_px 正方形归一）。
    """
    ex = existing if isinstance(existing, dict) else {}
    pa = patch if isinstance(patch, dict) else {}
    ex_w = ex.get("w")
    ex_h = ex.get("h")
    if ex_w is None and _is_finite_num(ex.get("side_px")):
        ex_w = int(ex["side_px"])
        ex_h = int(ex["side_px"])
    p_w = pa.get("w")
    p_h = pa.get("h")
    p_side = pa.get("side_px")
    has_pair_patch = p_w is not None or p_h is not None
    if has_pair_patch and (p_w is None or p_h is None):
        raise ValueError("w/h 需成对提供")
    if has_pair_patch:
        w = _int_px(p_w, "w")
        h = _int_px(p_h, "h")
    elif p_side is not None:
        side = _int_px(p_side, "side_px")
        # 旧 side_px 编辑：仅对（归一后）正方形记录兼容
        if not (ex_w is not None and ex_h is not None and ex_w == ex_h):
            raise ValueError(
                "该标注为非正方形矩形，需升级客户端后以成对 w/h 编辑")
        w = side
        h = side
    else:
        w = ex_w
        h = ex_h
    if not (_is_finite_num(w) and _is_finite_num(h)):
        raise ValueError("缺少可用的矩形几何")
    x = pa.get("x", ex.get("x"))
    y = pa.get("y", ex.get("y"))
    out = {
        "x": int(x) if _is_finite_num(x) else 0,
        "y": int(y) if _is_finite_num(y) else 0,
        "w": int(w),
        "h": int(h),
    }
    out["geometry_version"] = (GEOMETRY_VERSION
                               if (ex.get("geometry_version") == GEOMETRY_VERSION
                                   or has_pair_patch) else 1)
    return out


def _validate_rect_bounds(x, y, w, h, slide_w, slide_h):
    """真实切片 level-0 边界校验（§6.2/§6.3-4）。返回 None 或错误 message。

    slide_w/slide_h 为 None（尺寸不可读）时不做包含校验（降级语义与
    app.py 既有 _validate_annotation_rect 一致）；有限性/范围恒定校验。
    """
    if not (_is_finite_num(x) and _is_finite_num(y)
            and _is_finite_num(w) and _is_finite_num(h)):
        return "x/y/w/h 需为有限数值"
    if x < 0 or y < 0:
        return "坐标需 ≥0（x=%s, y=%s）" % (x, y)
    if w < 1 or h < 1 or w > RECT_MAX_SIDE_PX or h > RECT_MAX_SIDE_PX:
        return ("w/h 需在 1~%d 之间（当前 %s×%s）" % (RECT_MAX_SIDE_PX, w, h))
    max_px = _rect_max_pixels()
    if w * h > max_px:
        return "矩形 %d×%d=%d 像素超过单标注预算 %d" % (w, h, w * h, max_px)
    if slide_w is None or slide_h is None:
        return None
    overshoot = {}
    if x + w > slide_w:
        overshoot["right"] = x + w - slide_w
    if y + h > slide_h:
        overshoot["bottom"] = y + h - slide_h
    if not overshoot:
        return None
    parts = []
    if "right" in overshoot:
        parts.append("右边界越界：x + w = %s > 切片宽 %s（超出 %s 像素）"
                     % (x + w, slide_w, overshoot["right"]))
    if "bottom" in overshoot:
        parts.append("下边界越界：y + h = %s > 切片高 %s（超出 %s 像素）"
                     % (y + h, slide_h, overshoot["bottom"]))
    return ("标注矩形越出切片边界，已拒绝（不自动裁剪）：%s。切片 level-0 尺寸 "
            "%s×%s，提交 x=%s, y=%s, w=%s, h=%s。"
            % ("；".join(parts), slide_w, slide_h, x, y, w, h))


def _validate_geom(typ, geom):
    """校验几何字段，返回归一化后的几何 dict（不含 type/label/token/slide/ts）。

    - rect：x, y + 成对 w/h（geometry_version=2）或 side_px（v1 正方形兼容）；
      v2 正方形保留一致 side_px 兼容字段，非正方形不伪造 side_px
    - arrow：x1, y1, x2, y2（两端点距离 > 0）
    - freehand：points: [[x,y],...]（3~500 点，坐标 ≥0 且有限）
    坐标均要求 ≥0 且数值有限。校验失败抛 ValueError。
    （rect 归一化见 _validate_rect_geometry；切片边界校验由路径入口经
    _validate_rect_bounds 承担。）
    """
    if typ == "rect":
        return _validate_rect_geometry(geom)

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