# -*- coding: utf-8 -*-
"""viewer 显示资源身份（image-transport-upgrade §5.1/§5.2）。

三种身份严格分离：
- 既有 ``asset_revision`` / render fingerprint：既有文件与显示通道语义契约，
  本模块不解释、不改变；
- ``display_asset_revision``：网页图像资源的文件身份——由
  ``slide_cache.FileSignature(dev, ino, size, mtime_ns)`` 的确定性摘要派生，
  不泄露路径；同名替换、原地改写、换 inode 都会变化（§5.1）；
- ``encoding_fingerprint`` / ``display_version``：viewer 编码与资源身份。
  ``display_version = SHA256(canonical(dar, fp, enc_fp, purpose, geometry,
  pipeline))``，摘要确定性；不授予任何访问权限。

依赖既有文件写入纪律（read_stable / FileSignature）。跨节点**非共享 inode**
拓扑下 dev/ino 不稳定：部署前必须按 B0 流程验证所有服务同一路由的 replica
对同一底层文件得到相同摘要，否则不得上线本方案（§5.1 硬约束）。

本模块不 import Flask；参数解析只依赖 MultiDict 形状（get/getlist）。
"""
from __future__ import annotations

import hashlib
import json
import re

import slide_cache
import slide_render

#: dv 摘要十六进制长度（sha256 全长）；客户端回传必须完全一致
_DV_RE = re.compile(r"^[0-9a-f]{64}$")

#: tile 几何身份（§5.1 geometry：tile size/overlap/裁边策略；层级坐标不在内
#: ——dv 是资源集版本，不随单张瓦片坐标变化）
DZ_BOUNDS_POLICY = "limit_bounds_v1"
#: thumbnail 几何身份（固定 400×400 上限 + LANCZOS，与既有实现一致）
THUMBNAIL_MAX_EDGE = 400
THUMBNAIL_RESAMPLE_POLICY = "lanczos-v1"


class DisplayParamError(Exception):
    """profile/dv 查询参数非法（缺配对、未知 profile、非法格式、重复冲突）。

    ``code`` 恒 ``invalid_display_profile``（§5.2：统一 400 码表）。
    """

    code = "invalid_display_profile"

    def __init__(self, message):
        super().__init__(message)
        self.status = 400


class DisplayVersionConflict(Exception):
    """合法但已过时的 display_version（§5.2：409 display_version_conflict）。"""

    code = "display_version_conflict"

    def __init__(self, message="display_version 与服务端当前身份不一致"):
        super().__init__(message)
        self.status = 409


def display_asset_revision(entry) -> str:
    """由切片当前 FileSignature 派生确定性摘要（"dar1-<32hex>"）。

    只吃 stat 四元组，不泄露路径/挂载拓扑细节。stat 失败（文件消失）由
    调用方按既有 SlideFileChanged 语义处理（本函数抛 OSError 原样上抛）。
    """
    sig = slide_cache.signature_of(entry["path"])
    if sig is None:
        raise slide_cache.SlideFileChanged(
            "切片文件不可判读（stat 失败）：%s" % entry["name"])
    raw = "sig-v1:%d:%d:%d:%d" % (sig.st_dev, sig.st_ino, sig.st_size,
                                  sig.st_mtime_ns)
    return "dar1-" + hashlib.sha256(raw.encode("ascii")).hexdigest()[:32]


def tile_geometry() -> dict:
    return {
        "tile_size": int(slide_cache.DZ_TILE_SIZE),
        "overlap": int(slide_cache.DZ_OVERLAP),
        "bounds": DZ_BOUNDS_POLICY,
    }


def thumbnail_geometry() -> dict:
    return {
        "max_edge": THUMBNAIL_MAX_EDGE,
        "resample": THUMBNAIL_RESAMPLE_POLICY,
    }


def display_version(*, display_asset_revision, render_fingerprint,
                    encoding_fingerprint, purpose, geometry,
                    pipeline_version=None):
    """确定性 display_version（§5.1 公式；不授予访问权限）。"""
    payload = {
        "pipeline": pipeline_version or slide_render.VIEWER_PIPELINE_VERSION,
        "dar": str(display_asset_revision),
        "fp": str(render_fingerprint),
        "enc": str(encoding_fingerprint),
        "purpose": purpose,
        "geometry": geometry,
    }
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"),
                     ensure_ascii=True).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def compute_display_version(entry, render_fingerprint, spec, geometry=None):
    """便捷入口：当前 entry 代 + fp + spec → display_version。"""
    return display_version(
        display_asset_revision=display_asset_revision(entry),
        render_fingerprint=str(render_fingerprint or ""),
        encoding_fingerprint=slide_render.viewer_encoding_fingerprint(spec),
        purpose=spec.purpose,
        geometry=geometry or (thumbnail_geometry() if spec.purpose
                              == "thumbnail" else tile_geometry()))


def parse_display_params(args, purpose):
    """解析并校验 tile/thumbnail 查询参数（§5.2；白名单内校验前置）。

    返回 ``(profile_id, dv)``；两者都缺 → ``(None, None)``（旧 URL 语义）。
    违反配对/白名单/格式/重复 → :class:`DisplayParamError`（400）。
    ``purpose``：``"tile"`` | ``"thumbnail"``（决定 profile 白名单）。
    """
    profiles = args.getlist("profile") if hasattr(args, "getlist") \
        else ([args.get("profile")] if args.get("profile") is not None else [])
    dvs = args.getlist("dv") if hasattr(args, "getlist") \
        else ([args.get("dv")] if args.get("dv") is not None else [])
    if len(profiles) > 1 or len(dvs) > 1:
        raise DisplayParamError("profile/dv 重复冲突")
    profile = profiles[0] if profiles else None
    dv = dvs[0] if dvs else None
    if profile is None and dv is None:
        return None, None
    if profile is None or dv is None:
        raise DisplayParamError("profile 与 dv 必须成对提供")
    whitelist = slide_render.VIEWER_TILE_PROFILES if purpose == "tile" \
        else slide_render.VIEWER_THUMBNAIL_PROFILES
    if profile not in whitelist:
        raise DisplayParamError("未知显示 profile %r" % (profile,))
    if not isinstance(dv, str) or not _DV_RE.match(dv):
        raise DisplayParamError("dv 格式非法")
    return profile, dv


def build_display_info(entry, render_fields, *, include_thumbnail=True,
                       quality_override=None):
    """info 响应的 additive ``display`` 对象（§5.2）。

    ``render_fields``：同一 read_stable 里 ``build_render_info`` 的返回值
    （取 image_mode 与 default_render_context.fingerprint——display 的 dv
    按默认 context fp 计算；自定义 context 的 dv 由 render-context 响应的
    ``display_versions`` 提供）。字段 additive，不覆盖既有能力声明。
    """
    mode = render_fields.get("image_mode") or "native_rgb"
    default_ctx = render_fields.get("default_render_context") or {}
    # 默认 context 的 fp：flag 关时 build_render_info 不带 default_render_context，
    # 而瓦片路由对 ctx=None 恒用 NATIVE_RGB_FINGERPRINT——info 的 dv 必须与
    # 路由同源（否则每个版本化请求都会 409）。
    fp = default_ctx.get("fingerprint") \
        or slide_render.NATIVE_RGB_FINGERPRINT
    dar = display_asset_revision(entry)

    def _profiles(purpose):
        purpose_geometry = tile_geometry() if purpose == "tile" \
            else thumbnail_geometry()
        out = []
        for item in slide_render.display_encoding_info(
                mode, purpose, quality_override=quality_override):
            spec = slide_render.resolve_viewer_encoding(
                mode, purpose, item["profile_id"],
                quality_override=quality_override)
            item["display_version"] = display_version(
                display_asset_revision=dar, render_fingerprint=fp,
                encoding_fingerprint=item["encoding_fingerprint"],
                purpose=purpose, geometry=purpose_geometry)
            out.append(item)
        return out

    info = {
        "image_mode": mode,
        "display_asset_revision": dar,
        "default_profile": slide_render.VIEWER_PROFILE_NATIVE_STANDARD
        if mode == "native_rgb" else slide_render.VIEWER_PROFILE_FLUORESCENCE,
        "profiles": _profiles("tile"),
    }
    if include_thumbnail:
        info["thumbnail"] = {
            "max_edge": THUMBNAIL_MAX_EDGE,
            "profiles": _profiles("thumbnail"),
        }
    return info


def _profile_ids_for(mode, purpose):
    """按 mode/purpose 过滤白名单（荧光只出 preserve 档，§3.2）。"""
    if mode == "multichannel":
        return (slide_render.VIEWER_PROFILE_FLUORESCENCE,) \
            if purpose == "tile" \
            else (slide_render.VIEWER_PROFILE_FLUORESCENCE_THUMB,)
    if purpose == "tile":
        return (slide_render.VIEWER_PROFILE_NATIVE_STANDARD,
                slide_render.VIEWER_PROFILE_NATIVE_DETAIL)
    return (slide_render.VIEWER_PROFILE_NATIVE_THUMB,)


def display_versions_for_fingerprint(entry, render_fingerprint, mode, *,
                                     quality_override=None):
    """render-context 响应 ``display_versions``：自定义 context 的每档 dv。

    键为 profile_id（tile 与 thumbnail 的 profile 词表不相交，客户端按
    当前用途查自己的 profile）；mode 不匹配的档直接不出（荧光不列 RGB 档）。
    """
    dar = display_asset_revision(entry)
    out = {}
    for purpose, geometry in (("tile", tile_geometry()),
                              ("thumbnail", thumbnail_geometry())):
        for pid in _profile_ids_for(mode, purpose):
            spec = slide_render.resolve_viewer_encoding(
                mode, purpose, pid, quality_override=quality_override)
            out[spec.profile_id] = display_version(
                display_asset_revision=dar,
                render_fingerprint=str(render_fingerprint or ""),
                encoding_fingerprint=slide_render.viewer_encoding_fingerprint(
                    spec),
                purpose=purpose, geometry=geometry)
    return out
