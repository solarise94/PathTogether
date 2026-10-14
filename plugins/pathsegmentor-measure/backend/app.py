# -*- coding: utf-8 -*-
"""PathSegmentor Measure 插件后端（测量插件 PoC）。

契约（docs/measurement-models-homepc-implementation.md §2-§3）：

    HistoPilot agent → PathTogether /api/plugin/v1/dispatch/dev.pathsegmentor.measure/segment_region
        → 本服务 POST /capabilities/segment_region
            → PathTogether region 端点（**委托读凭证**读 ROI）
            → PathSegmentor 推理服务（PATHSEGMENTOR_URL，GPU 留在 homePC）
            → 小型 JSON 计量结果（无 mask / 无 base64 图像）

平台 dispatch 在 agent token / 用户实时权限 / 插件启用全部通过后，于转发请求
头 ``X-Delegated-Read-Token`` 下发**短时只读委托凭证**（绑定 plugin
installation / user / slide_id / revision / ROI 上限，TTL ≤120s）。本服务：

  1. 强制校验 ``X-Dispatch-Principal``（平台附加）与委托凭证头——二者缺一
     即拒绝（直连调用不受理，与平台「D1 唯一分发点」口径一致）；
  2. 用安装凭证换 scoped JWT（/api/plugin/v1/auth/token，带缓存）；
  3. 经 ``POST /api/plugin/v1/slides/{slide}/regions``（by-name 与 by-id 共一
     实现）读取 ROI：segment_region 目标 ~0.25 µm/px；count_cells 在
     Cellpose 的实际处理尺度（max(原生, diameter/30) µm/px）上渲染，
     resolution（512/1024/2048）只是处理长边上限——所需长边/处理预算超限
     在读 ROI 前拒绝（≤ 平台 ROI 像素上限——超限由平台 403 roi_over_cap
     拒绝，本服务透传为工具级错误）；同时取 slide_info 的原生 MPP 做 µm²
     换算（缺失/无效 → null，仅像素口径）；
  4. 调推理服务（segment_region → ``POST /v1/segment``；count_cells →
     Cellpose ``POST /v1/count``，输入 MPP = 原生 MPP × 实际渲染缩放，
     调用前镜像服务端缩放预算做资源准入；busy / not_ready / 不可达 /
     契约不符 / resource_limit 映射为明确的错误信封）；
  5. 纯后处理（无 CV 依赖）：mask 像素映射回 level-0、面积/占比/µm²
     （MPP 按轴有效才给 µm²）、小目标过滤后的连通域计数（语义分割 → 计数
     为**近似值** approx_object_count）、尺寸分布摘要、最大 20 个连通域的
     level-0 bbox/质心。

响应体恒 ≪ 64 KiB（平台 dispatch 截断阈值），绝不含 mask / base64 图像 /
委托凭证本身。

运行（开发）::

    python3 plugins/pathsegmentor-measure/backend/app.py   # 监听 127.0.0.1:8062

环境变量：
  - ``PT_PATHSEGMENTOR_MEASURE_PORT``：监听端口（缺省 8062，须与 manifest
    ``service.baseUrl`` 一致）；
  - ``PT_PLATFORM_URL``：PathTogether 基地址（缺省 http://127.0.0.1:8000）；
  - ``PT_INSTALLATION_ID`` / ``PT_INSTALLATION_SECRET``：本插件安装凭证
    （owner 安装 API 创建安装行后取回/轮换获得）；
  - ``PATHSEGMENTOR_URL``：推理服务基地址（缺省 http://127.0.0.1:8971）；
  - ``PT_PSM_TARGET_MPP``：segment_region 目标输出分辨率 µm/px（缺省
    0.25）；count_cells 的处理长边上限为固定枚举（512/1024/2048，缺省
    1024，常量 ``_COUNT_RESOLUTIONS``，无 env）；
  - ``PT_PSM_MAX_LONG_EDGE``：ROI 输出图长边上限（缺省 1024，≤2048² 合同）；
  - ``PT_PSM_MIN_COMPONENT_FRACTION``：连通域小目标过滤阈值（占 ROI 输出
    像素比例，缺省 1e-4，下限 4 像素）；
  - ``PT_PSM_MAX_COMPONENTS``：返回的最大连通域个数（缺省 20）。
"""
import base64
import io
import json
import math
import os
import secrets
import time

import requests
from flask import Flask, jsonify, request

app = Flask(__name__)

_PLATFORM_URL = (os.environ.get("PT_PLATFORM_URL") or
                 "http://127.0.0.1:8000").rstrip("/")
_INSTALLATION_ID = os.environ.get("PT_INSTALLATION_ID") or ""
_INSTALLATION_SECRET = os.environ.get("PT_INSTALLATION_SECRET") or ""
_INFER_URL = (os.environ.get("PATHSEGMENTOR_URL") or
              "http://127.0.0.1:8971").rstrip("/")
#: 目标输出分辨率 µm/px：0.25（PathSegmentor 以 40x 1024² patch 训练，实测
#: 0.25 下核mask明显优于 0.5）。输出永不上采样超过切片原生 mpp
#: （_target_long_edge 钳制输出像素 ≤ level-0 源像素）。
_TARGET_MPP = float(os.environ.get("PT_PSM_TARGET_MPP") or 0.25)
_MAX_LONG_EDGE = min(int(os.environ.get("PT_PSM_MAX_LONG_EDGE") or 1024), 2048)
_MIN_COMPONENT_FRACTION = float(
    os.environ.get("PT_PSM_MIN_COMPONENT_FRACTION") or 0.0001)
_MAX_COMPONENTS = min(int(os.environ.get("PT_PSM_MAX_COMPONENTS") or 20), 50)
#: 计数拆分（近似）：期望单目标直径 µm（核 ~6–8，缺省 7）→ 最小间距
#: min_distance = 直径/2（输出像素）；estimated_count_split 仅在
#: objects_resolved ≥ 阈值时给出（否则 mask 覆盖的是组织区域/细胞带，
#: 拆分会发明伪目标）
_EXPECTED_DIAMETER_UM = float(
    os.environ.get("PT_PSM_EXPECTED_DIAMETER_UM") or 7.0)
_OBJECTS_RESOLVED_MIN = float(
    os.environ.get("PT_PSM_OBJECTS_RESOLVED_MIN") or 0.6)
#: 单目标面积判定系数：连通域面积 ≤ 3×期望单目标面积 视为「已分辨」
_RESOLVED_AREA_FACTOR = 3.0

#: 平台回调超时（region / slide-info / token 换取）；总和须 < capability
#: timeout_ms（manifest 55000）− 推理预算
_REGION_TIMEOUT_S = 20.0
_INFO_TIMEOUT_S = 10.0
_TOKEN_TIMEOUT_S = 5.0
#: 推理服务超时（≤2048²、≤8MB 图像单次推理预算）
_INFER_TIMEOUT_S = 32.0

_PLUGIN_ID = "dev.pathsegmentor.measure"
_CAPABILITY_SEGMENT = "segment_region"
_CAPABILITY_COUNT = "count_cells"

# ---------------------------------------------------------------------------
# Cellpose 计数服务（count_cells 能力；homePC 第二推理服务）
# ---------------------------------------------------------------------------
#: 处理长边**上限**固定枚举（输出像素；R2 重设计：agent 只在固定选项里选）。
#: Cellpose 的处理量与渲染分辨率无关（cpsam 内部按 rescale=30/diameter_px
#: 把输入缩放到 d/30 µm/px 的处理尺度，处理边 = 30×视场/diameter），因此
#: resolution 不再是「渲染目标」，而是**允许的处理长边上限**：所需处理长边
#: 超过选项 → 读 ROI 前拒绝（不做「粗渲染 + 模型上采样」——那只是扔掉真
#: 像素、算力不变）。仅 mpp 缺失（处理尺度不可判定）时 resolution 才直接
#: 决定渲染长边。2048 为服务输入上限（平台 regions 路由 max_long_edge
#: 1..4096、单请求像素上限 4096²，2048² 输出在内）。
_COUNT_RESOLUTIONS = (512, 1024, 2048)
#: 缺省取上限 2048：Cellpose 的处理量与渲染分辨率无关，缺省给最大视场
#: （d=7、原生 ≤0.2333 µm/px → 约 478 µm）最少误拒；最坏缺省算力
#: 2048²×1.07² ≈ 4.8M px（原生 0.25），在 8 Mi px 预算与插件 32s 超时内。
#: 512/1024 是更快的显式选项。
_COUNT_RESOLUTION_DEFAULT = 2048
_CELLPOSE_URL = (os.environ.get("CELLPOSE_URL") or
                 "http://127.0.0.1:8972").rstrip("/")
#: 与 services/cellpose/cellpose_infer.MAX_SCALED_PIXELS / DIAM_MEAN_PX 同源
#: （两处独立部署，常量镜像；服务端准入仍在，此处是前置防线）。cpsam 在给出
#: diameter（像素）时按 rescale=30/diameter_px 内部放大输入，合法参数组合在
#: 粗分辨率切片上可放大许多倍（极端例：diameter_um=1 @ 4 µm/px 输入 →
#: diameter_px=0.25 → 120×），必须在调用前按实际几何拒绝。
_CELLPOSE_MAX_SCALED_PIXELS = 8 * 1024 * 1024
_CELLPOSE_DIAM_MEAN_PX = 30.0
#: diameter_um 合法域（µm）：核 ~6–8、整细胞 15–25；缺省 7。下限 4：更小
#: 的「直径」在推理分辨率粗于 0.25 µm/px 的切片上会把 diameter_px 压到个位
#: 数、触发内部放大爆炸（预算闸拒绝）；上限 100 覆盖成片大目标。
_COUNT_DIAMETER_DEFAULT_UM = 7.0
_COUNT_DIAMETER_MIN_UM = 4.0
_COUNT_DIAMETER_MAX_UM = 100.0

#: 安装 JWT 缓存（demo 单进程；到期前 60s 主动换新）
_JWT_CACHE = {"token": "", "exp": 0.0}


# --------------------------------------------------------------------------- #
# 通用
# --------------------------------------------------------------------------- #
def _error(status, code, message, retryable=False, extra=None):
    """统一错误信封（docs §7.7；dispatch 对 4xx 原样透传、5xx 归一重试语义）。"""
    err = {"code": code, "message": message, "retryable": bool(retryable)}
    if extra:
        err["extra"] = extra
    resp = jsonify(error=err)
    resp.status_code = status
    return resp


def _dispatch_principal():
    """解析平台附加的 X-Dispatch-Principal（唯一可信主体头；缺失/非法 → None）。"""
    raw = request.headers.get("X-Dispatch-Principal") or ""
    if not raw:
        return None
    try:
        obj = json.loads(raw)
    except ValueError:
        return None
    return obj if isinstance(obj, dict) else None


def _delegated_token():
    """平台 dispatch 下发的委托读凭证（原样转发给平台 region 端点）。

    绝不写入任何返回结果或日志。缺失 → None（调用方拒绝本次测量）。
    """
    tok = (request.headers.get("X-Delegated-Read-Token") or "").strip()
    return tok or None


def _installation_jwt():
    """安装凭证 → 短期 scoped JWT（缓存至到期前 60s）。"""
    now = time.time()
    if _JWT_CACHE["token"] and _JWT_CACHE["exp"] - 60 > now:
        return _JWT_CACHE["token"]
    r = requests.post(
        _PLATFORM_URL + "/api/plugin/v1/auth/token",
        json={"installation_id": _INSTALLATION_ID,
              "secret": _INSTALLATION_SECRET},
        timeout=_TOKEN_TIMEOUT_S)
    if r.status_code != 200:
        raise PlatformError(503, "platform_token_unavailable",
                            "换取平台 scoped JWT 失败（HTTP %d）" % r.status_code,
                            retryable=True)
    body = r.json() or {}
    token = body.get("access_token") or ""
    if not token:
        raise PlatformError(503, "platform_token_unavailable",
                            "平台 token 响应缺少 access_token", retryable=True)
    _JWT_CACHE["token"] = token
    _JWT_CACHE["exp"] = now + float(body.get("expires_in") or 900)
    return token


class PlatformError(Exception):
    """平台回调 / 推理调用的可映射错误（status/code/message/retryable）。"""

    def __init__(self, status, code, message, retryable=False, extra=None):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.retryable = retryable
        self.extra = extra


def _platform_error_from_response(r, what):
    """平台非 2xx → 归一错误信封（4xx 保码便于 agent 理解；5xx 归一）。

    平台 404（切片不存在/不可读——id_bundle 行无 legacy 别名，按名回调必
    404）→ 归一为 ``slide_unavailable``（不透传平台的 not_found：那会在
    HP 侧渲染成「插件已停用」，且与 PT 保留码语义冲突）。
    """
    if r.status_code == 404:
        return PlatformError(404, "slide_unavailable",
                             "%s 失败：切片不存在或当前不可读" % what,
                             retryable=False)
    code, message = "platform_error", "%s 失败（HTTP %d）" % (what, r.status_code)
    retryable = r.status_code >= 500
    try:
        body = r.json()
        if isinstance(body, dict) and isinstance(body.get("error"), dict):
            code = str(body["error"].get("code") or code)
            message = str(body["error"].get("message") or message)
            retryable = bool(body["error"].get("retryable", retryable))
    except Exception:
        pass
    return PlatformError(r.status_code if r.status_code < 500 else 503,
                         code, message, retryable=retryable)


def _platform_slide_info(slide, jwt, slide_id=None, delegated=None):
    """slide_info（尺寸/MPP/revision 元数据；scope slide:read）。

    slide_id 给出（dispatch 转发，P2 合同）→ 走 by-id 路由（id_bundle 行
    legacy_filename=NULL，按名回调恒 404）；并附委托凭证头（读时逐 claim
    复核）。否则按名（legacy 冻结别名行）。
    """
    if slide_id:
        url = "%s/api/plugin/v1/slides/by-id/%s" % (_PLATFORM_URL, slide_id)
    else:
        url = "%s/api/plugin/v1/slides/%s" % (_PLATFORM_URL, slide)
    headers = {"Authorization": "Bearer " + jwt}
    if slide_id and delegated:
        headers["X-Delegated-Read-Token"] = delegated
    r = requests.get(url, headers=headers, timeout=_INFO_TIMEOUT_S)
    if r.status_code != 200:
        raise _platform_error_from_response(r, "slide_info")
    body = r.json() or {}
    return {
        "width": body.get("width"),
        "height": body.get("height"),
        "mpp": _normalize_mpp(body.get("mpp")),
        "asset_revision": body.get("asset_revision"),
    }


def _platform_read_region(slide, args, target_edge, delegated, jwt,
                          slide_id=None):
    """经平台 region 端点读 ROI（JSON 路径；返回 (image_bytes, region_payload)）。

    slide_id 给出 → by-id 路由（平台按稳定 ID 解析后逐项复核委托凭证：
    installation / 用户实时权限 / revision / ROI 上限 / 像素闸）；否则按名
    （与 by-id 共用同一实现）。
    """
    body = {"x": args["x"], "y": args["y"], "w": args["w"], "h": args["h"],
            "max_long_edge": target_edge}
    if slide_id:
        url = "%s/api/plugin/v1/slides/by-id/%s/regions" % (_PLATFORM_URL,
                                                            slide_id)
    else:
        url = "%s/api/plugin/v1/slides/%s/regions" % (_PLATFORM_URL, slide)
    r = requests.post(
        url,
        json=body,
        headers={"Authorization": "Bearer " + jwt,
                 "X-Delegated-Read-Token": delegated},
        timeout=_REGION_TIMEOUT_S)
    if r.status_code != 200:
        raise _platform_error_from_response(r, "region 读取")
    payload = r.json() or {}
    try:
        img_bytes = base64.b64decode(payload["image_base64"])
    except Exception:
        raise PlatformError(503, "platform_contract",
                            "region 响应缺少可解码的 image_base64", retryable=True)
    return img_bytes, payload


def _to_float(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _validate_bbox_args(args):
    """两个能力共用的 level-0 bbox 校验。返回 (dict, None) 或 (None, err)。"""
    if not isinstance(args, dict):
        return None, PlatformError(400, "invalid_request", "arguments 需为对象")
    out = {}
    for k in ("x", "y", "w", "h"):
        v = args.get(k)
        if isinstance(v, bool) or not isinstance(v, int):
            # 平台 sidecar 的 TypeBox integer 校验在前；此处兜底（含 float-int）
            if isinstance(v, float) and v.is_integer():
                v = int(v)
            else:
                return None, PlatformError(
                    400, "invalid_request",
                    "%s 需为整数（level-0 像素坐标）" % k)
        out[k] = v
    if out["x"] < 0 or out["y"] < 0 or out["w"] <= 0 or out["h"] <= 0:
        return None, PlatformError(400, "invalid_request",
                                   "参数越界（0<=x,y，0<w,h）")
    return out, None


def _validate_args(args):
    """segment_region 参数校验（bbox + 受控文本提示/阈值）。

    返回 (normalized, None) 或 (None, PlatformError 400)。
    """
    out, aerr = _validate_bbox_args(args)
    if aerr is not None:
        return None, aerr
    obj = args.get("object")
    if not isinstance(obj, str) or not obj.strip():
        return None, PlatformError(400, "invalid_request",
                                   "object 必填（要分割的目标名称）")
    out["object"] = obj.strip()
    region = args.get("region")
    if region is not None:
        if not isinstance(region, str) or not region.strip():
            return None, PlatformError(400, "invalid_request",
                                       "region 需为非空字符串或省略")
        out["region"] = region.strip()
    thr = args.get("threshold")
    if thr is not None:
        f = _to_float(thr)
        if f is None or not (0.0 <= f <= 1.0):
            return None, PlatformError(400, "invalid_request",
                                       "threshold 需为 0..1 的数值或省略")
        out["threshold"] = f
    return out, None


# --------------------------------------------------------------------------- #
# mask 后处理（纯 Python，无 CV 依赖；单测覆盖）
# --------------------------------------------------------------------------- #
def mask_to_grid(png_bytes):
    """PNG mask bytes → 二值 grid（list[list[0/1]]，255=前景）。

    推理合同：mask 与输入同尺寸、单通道语义掩码。解码失败/空图抛
    PlatformError(503, inference_contract)。
    """
    try:
        from PIL import Image
    except ImportError:
        raise PlatformError(503, "backend_missing_dependency",
                            "插件后端缺少 Pillow（mask 解码不可用）")
    try:
        img = Image.open(io.BytesIO(png_bytes)).convert("L")
    except Exception:
        raise PlatformError(503, "inference_contract",
                            "推理服务返回的 mask_png_b64 不可解码", retryable=True)
    w, h = img.size
    if w <= 0 or h <= 0:
        raise PlatformError(503, "inference_contract",
                            "推理服务返回空 mask", retryable=True)
    raw = img.tobytes()
    return [[1 if raw[y * w + x] >= 128 else 0 for x in range(w)]
            for y in range(h)], w, h


def _target_long_edge(bbox_w, bbox_h, mpp, target_mpp=None):
    """选输出长边：~target µm/px 或更细，夹到 [1, min(_MAX_LONG_EDGE, 长边)]。

    ``target_mpp`` 缺省用 PathSegmentor 目标（0.25）；Cellpose 计数传
    ``_CELLPOSE_TARGET_MPP``（0.5，直径驱动通常足够）。MPP 未知时用上限
    （1024）并在调用方附 warning——分辨率不可判定。
    ``≤ 长边``钳制保证**永不上采样超过切片原生分辨率**（输出像素 ≤ level-0
    源像素 ⇒ 输出分辨率 ≥ 原生 mpp，原生即最细）。
    """
    tgt = float(target_mpp) if target_mpp else _TARGET_MPP
    cap = max(1, _MAX_LONG_EDGE)
    long_side = max(int(bbox_w), int(bbox_h))
    cap = min(cap, max(1, long_side))
    if not mpp or mpp <= 0 or tgt <= 0:
        return cap, True
    want = int(round(long_side * mpp / tgt))
    if want < 1:
        want = 1
    if want <= cap:
        return want, False
    return cap, True  # 受 1024 合同上限或原生分辨率钳制（变粗/不放大）


def _normalize_mpp(mpp):
    """切片原生 MPP 规范化（R4）：非有限/≤0 一律按缺失（None）处理。

    此前缺失/无效的 MPP 会被发成 0.0，真实计数服务 ``_validate_request``
    对非空 MPP 要求 >0 → 直接 400，降级分支不可达。
    """
    f = _to_float(mpp)
    return f if (f is not None and f > 0) else None


def _render_long_edge(bbox_w, bbox_h, requested):
    """mpp 缺失时 count_cells 的渲染长边：所选 resolution 选项，钳到源长边。

    仅 null-MPP 降级路径使用（处理尺度不可判定，resolution 直接决定渲染，
    rescale 1.0 → 算力 = edge²）。原生 MPP 已知时走 ``_count_render_plan``。
    返回 ``(edge, coarse)``——coarse=True 表示被源长边钳制。
    """
    cap = max(1, int(requested))
    long_side = max(int(bbox_w), int(bbox_h))
    edge = min(cap, max(1, long_side))
    return edge, edge < cap


def _count_render_plan(bbox_w, bbox_h, native_mpp, diameter_um, resolution):
    """count_cells 渲染计划（R2 重设计）：在 Cellpose 的实际处理尺度上渲染。

    cpsam 内部按 ``rescale = 30/diameter_px`` 把输入缩放到「每 diameter_um
    30 px」的处理分辨率，即处理尺度 = diameter_um/30 µm/px（d=7 → 0.2333）：
    处理边 = 30 × 视场/diameter，**与渲染分辨率无关**。渲染粗于处理尺度只
    是扔掉真像素再让模型上采样（算力不变、信息变差）；渲染细于它的部分会被
    模型自己降采样。因此渲染尺度取 ``mpp_render = max(native, d/30)``（永不
    上采样超过原生），所需处理长边 = ``ceil(L_long_level0 × native /
    mpp_render)``，resolution 只是处理长边**上限**选项。

    返回 dict：mpp_render / edge_long / edge_short_req / rescale /
    scaled_long / scaled_short / scaled_px（预估处理像素量，与
    ``_cellpose_scaled_pixels`` 同式）/ native_clamped（原生粗于处理尺度，
    按原生渲染）。native 缺失（null mpp）→ rescale 1.0、按所选 resolution
    长边渲染（钳到源长边；算力 = edge²，恒 ≤ 2048² < 预算）。
    """
    long_l0 = max(int(bbox_w), int(bbox_h))
    short_l0 = min(int(bbox_w), int(bbox_h))
    if native_mpp is None:
        edge = min(max(1, int(resolution)), max(1, long_l0))
        return {"mpp_render": None, "edge_long": edge,
                "edge_short_req": None, "rescale": 1.0,
                "scaled_long": edge, "scaled_short": None,
                "scaled_px": float(edge) * float(edge),
                "native_clamped": None}
    process_mpp = float(diameter_um) / _CELLPOSE_DIAM_MEAN_PX
    mpp_render = max(float(native_mpp), process_mpp)
    edge_long = max(1, int(math.ceil(long_l0 * native_mpp / mpp_render)))
    edge_short = max(1, int(math.ceil(short_l0 * native_mpp / mpp_render)))
    rescale = _CELLPOSE_DIAM_MEAN_PX * mpp_render / float(diameter_um)
    scaled_long = int(math.ceil(edge_long * rescale))
    scaled_short = int(math.ceil(edge_short * rescale))
    return {"mpp_render": mpp_render, "edge_long": edge_long,
            "edge_short_req": edge_short, "rescale": rescale,
            "scaled_long": scaled_long, "scaled_short": scaled_short,
            "scaled_px": scaled_long * scaled_short,
            "native_clamped": mpp_render > process_mpp}


def _count_max_roi(native_mpp, diameter_um, resolution):
    """所选 resolution 选项可处理的最大 ROI（方形等价；供 resource_limit
    的 extra 使用）。返回 ``(max_um, max_level0_px)``。

    两个上限取小者：resolution 选项（= resolution × mpp_render µm）与
    处理预算（处理边 ≤ √预算 px → 视场 ≤ √预算 × d/30 ≈ 96.54×d µm，
    与 resolution 无关）。
    """
    process_mpp = float(diameter_um) / _CELLPOSE_DIAM_MEAN_PX
    mpp_render = max(float(native_mpp), process_mpp)
    budget_edge = math.floor(math.sqrt(_CELLPOSE_MAX_SCALED_PIXELS))
    max_um = min(float(resolution) * mpp_render,
                 budget_edge * float(diameter_um) / _CELLPOSE_DIAM_MEAN_PX)
    max_l0 = int(math.floor(max_um / float(native_mpp)))
    return max_um, max_l0


def _inference_scale(native_mpp, src_w, src_h, out_w, out_h):
    """实际推理尺度与输入 MPP（R3）：用平台**实际返回**的 src 与输出尺寸，
    不用请求目标值代替。

    输入 MPP = level-0 原生 MPP × 实际缩放（src/输出）。单一 MPP 合同
    （服务只收一个标量）：两轴 mpp 相对差 >1% 视为平台异常——region 渲染
    保宽高比（``_aspect_fit_size`` 逐轴 round），正常只引入 ≤1 输出像素的
    取整差（1024 长边下 ≈0.1%）——拒绝（anisotropic_scale）而非猜测；
    否则取两轴均值（直径换算对 ≤1% 各向差不敏感）。原生 MPP 本身仍用于
    结果映射（密度/µm²/坐标），保持不变。

    返回 ``(scale_x, scale_y, infer_mpp)``；native_mpp 缺失 → infer_mpp=None
    （服务按模型缺省尺度计数，见 _infer_cellpose）。
    """
    scale_x = src_w / float(out_w)
    scale_y = src_h / float(out_h)
    if native_mpp is None:
        return scale_x, scale_y, None
    mpp_x = native_mpp * scale_x
    mpp_y = native_mpp * scale_y
    lo = min(mpp_x, mpp_y)
    if lo > 0 and abs(mpp_x - mpp_y) > 0.01 * lo:
        raise PlatformError(
            400, "anisotropic_scale",
            "region 渲染两轴尺度不一致（%.4f vs %.4f µm/px，>1%%）："
            "单一 MPP 计数服务无法表达，请重试；持续出现请反馈平台"
            % (mpp_x, mpp_y))
    return scale_x, scale_y, (mpp_x + mpp_y) / 2.0


def _cellpose_scaled_pixels(out_w, out_h, infer_mpp, diameter_um):
    """镜像 cellpose 服务准入（R2 前置防线）：内部缩放后的像素量估计。

    cpsam 按 ``rescale = 30 / diameter_px`` 放大输入（diameter_px =
    diameter_um / 推理 MPP）；逐轴 ceil 保守估计。infer_mpp 缺失（null
    mpp）→ 模型缺省尺度（rescale 1.0），工作集即输入像素。直径/MPP 非法
    或 rescale 非有限 → None（调用方拒绝）。
    """
    if infer_mpp is None or infer_mpp <= 0:
        return float(out_w) * float(out_h)
    diameter_px = float(diameter_um) / float(infer_mpp)
    if not math.isfinite(diameter_px) or diameter_px <= 0:
        return None
    rescale = _CELLPOSE_DIAM_MEAN_PX / diameter_px
    if not math.isfinite(rescale) or rescale <= 0:
        return None
    scaled_w = math.ceil(out_w * rescale)
    scaled_h = math.ceil(out_h * rescale)
    if scaled_w <= 0 or scaled_h <= 0:
        return None
    return scaled_w * scaled_h


def connected_components(grid, return_labels=False):
    """4-连通域标记（迭代 flood fill；返回 [(area_px, bbox, centroid), ...]）。

    bbox/centroid 为输出图像素坐标 (x, y, w, h) / (cx, cy)（float）。
    ``return_labels=True`` 时另返回标签网格（0=背景，1..N=连通域序号）——
    计数拆分（watershed）只在保留的连通域内进行。
    """
    h = len(grid)
    w = len(grid[0]) if h else 0
    seen = [[False] * w for _ in range(h)]
    labels = [[0] * w for _ in range(h)] if return_labels else None
    out = []
    for sy in range(h):
        row = grid[sy]
        for sx in range(w):
            if not row[sx] or seen[sy][sx]:
                continue
            stack = [(sx, sy)]
            seen[sy][sx] = True
            area = 0
            minx = maxx = sx
            miny = maxy = sy
            sumx = sumy = 0
            while stack:
                x, y = stack.pop()
                area += 1
                sumx += x
                sumy += y
                if x < minx:
                    minx = x
                if x > maxx:
                    maxx = x
                if y < miny:
                    miny = y
                if y > maxy:
                    maxy = y
                if x > 0 and grid[y][x - 1] and not seen[y][x - 1]:
                    seen[y][x - 1] = True
                    stack.append((x - 1, y))
                if x + 1 < w and grid[y][x + 1] and not seen[y][x + 1]:
                    seen[y][x + 1] = True
                    stack.append((x + 1, y))
                if y > 0 and grid[y - 1][x] and not seen[y - 1][x]:
                    seen[y - 1][x] = True
                    stack.append((x, y - 1))
                if y + 1 < h and grid[y + 1][x] and not seen[y + 1][x]:
                    seen[y + 1][x] = True
                    stack.append((x, y + 1))
            out.append({
                "area_px": area,
                "bbox": [minx, miny, maxx - minx + 1, maxy - miny + 1],
                "centroid": (sumx / float(area), sumy / float(area)),
            })
            if labels is not None:
                lab_idx = len(out)
                # 重走该域（第二次 flood 代价可接受：仅 PoC 规模）
                stack = [(sx, sy)]
                labels[sy][sx] = lab_idx
                while stack:
                    x, y = stack.pop()
                    if x > 0 and grid[y][x - 1] and labels[y][x - 1] == 0:
                        labels[y][x - 1] = lab_idx
                        stack.append((x - 1, y))
                    if x + 1 < w and grid[y][x + 1] and labels[y][x + 1] == 0:
                        labels[y][x + 1] = lab_idx
                        stack.append((x + 1, y))
                    if y > 0 and grid[y - 1][x] and labels[y - 1][x] == 0:
                        labels[y - 1][x] = lab_idx
                        stack.append((x, y - 1))
                    if y + 1 < h and grid[y + 1][x] and labels[y + 1][x] == 0:
                        labels[y + 1][x] = lab_idx
                        stack.append((x, y + 1))
    if return_labels:
        return out, labels
    return out


def _edt_chamfer(bin_grid):
    """二值网格的前景距离变换（两遍 3-4 chamfer，≈欧氏距离；纯 stdlib）。

    前景像素值 = 到最近背景像素的距离（输出像素单位）；背景为 0。
    scipy 不可用（PT venv 无 scipy/skimage）——PoC 用两遍 chamfer 近似，
    峰值定位足够。
    """
    h = len(bin_grid)
    w = len(bin_grid[0]) if h else 0
    inf = float("inf")
    d = [[0.0] * w for _ in range(h)]
    for y in range(h):
        row = bin_grid[y]
        drow = d[y]
        for x in range(w):
            if not row[x]:
                continue
            v = inf
            if y > 0:
                v = min(v, d[y - 1][x] + 1.0)
                if x > 0:
                    v = min(v, d[y - 1][x - 1] + 1.4142)
                if x + 1 < w:
                    v = min(v, d[y - 1][x + 1] + 1.4142)
            if x > 0:
                v = min(v, drow[x - 1] + 1.0)
            drow[x] = v if v < inf else (w + h)
    for y in range(h - 1, -1, -1):
        drow = d[y]
        for x in range(w - 1, -1, -1):
            if not bin_grid[y][x]:
                continue
            v = drow[x]
            if y + 1 < h:
                v = min(v, d[y + 1][x] + 1.0)
                if x > 0:
                    v = min(v, d[y + 1][x - 1] + 1.4142)
                if x + 1 < w:
                    v = min(v, d[y + 1][x + 1] + 1.4142)
            if x + 1 < w:
                v = min(v, drow[x + 1] + 1.0)
            drow[x] = v
    return d


def _watershed_split_count(bin_grid, min_distance_px, min_area_px):
    """距离变换局部极大值作种子 + 优先级泛洪分水岭（纯 stdlib）。

    - 种子：前景像素且距离 ≥ 8 邻域最大（局部极大）、距离 ≥ min_distance/2；
      按 (-dist, y, x) 贪心接受，与已接受种子 Chebyshev 距离 < min_distance_px
      的弃掉（min_distance = 期望直径/2，输出像素）；
    - 泛洪：heapq 按 -dist 出队（从高距离向低处扩展），4 连通；
    - 返回：盆地面积 ≥ min_area_px 的数量（拆分后的近似目标数）。
    """
    h = len(bin_grid)
    w = len(bin_grid[0]) if h else 0
    dist = _edt_chamfer(bin_grid)
    seed_min = max(1.0, min_distance_px / 2.0)
    cands = []
    for y in range(h):
        row = bin_grid[y]
        drow = dist[y]
        for x in range(w):
            dv = drow[x]
            if not row[x] or dv < seed_min:
                continue
            peak = True
            for ny in (y - 1, y, y + 1):
                if ny < 0 or ny >= h:
                    continue
                nrow = dist[ny]
                for nx in (x - 1, x, x + 1):
                    if nx < 0 or nx >= w or (nx == x and ny == y):
                        continue
                    if nrow[nx] > dv:
                        peak = False
                        break
                if not peak:
                    break
            if peak:
                cands.append((-dv, y, x))
    cands.sort()
    seeds = []
    for _nd, y, x in cands:
        ok = True
        for _ad, ay, ax in seeds:
            if max(abs(ay - y), abs(ax - x)) < min_distance_px:
                ok = False
                break
        if ok:
            seeds.append((-dist[y][x], y, x))
    if not seeds:
        return 0
    import heapq
    lab = [[0] * w for _ in range(h)]
    heap = []
    for i, (_dv, y, x) in enumerate(seeds, start=1):
        lab[y][x] = i
        heap.append((-dist[y][x], i, y, x))
    heapq.heapify(heap)
    while heap:
        _dv, li, y, x = heapq.heappop(heap)
        for nx, ny in ((x - 1, y), (x + 1, y), (x, y - 1), (x, y + 1)):
            if 0 <= nx < w and 0 <= ny < h and bin_grid[ny][nx] \
                    and lab[ny][nx] == 0:
                lab[ny][nx] = li
                heapq.heappush(heap, (-dist[ny][nx], li, ny, nx))
    areas = {}
    for y in range(h):
        lrow = lab[y]
        for x in range(w):
            li = lrow[x]
            if li:
                areas[li] = areas.get(li, 0) + 1
    return sum(1 for a in areas.values() if a >= min_area_px)


def _distribution(values):
    """面积分布摘要（min/p50/p90/max；空列表返回 None）。"""
    if not values:
        return None
    vs = sorted(values)

    def pct(p):
        if len(vs) == 1:
            return vs[0]
        idx = min(len(vs) - 1, max(0, int(round(p * (len(vs) - 1)))))
        return vs[idx]
    return {"min": vs[0], "p50": pct(0.5), "p90": pct(0.9), "max": vs[-1]}


def analyze_mask(grid, mask_w, mask_h, region_payload, mpp, *, request_id=None):
    """mask + region 元数据 → 计量结果 dict（不含 mask / base64 / 凭证）。

    - mask 像素先映射回 level-0（src 为平台夹取后的实际 level-0 bbox），
      面积 = 正像素数 × (src.w/out_w) × (src.h/out_h)；
    - µm² 仅在 MPP 有效（>0）时给出（平台 slide_info 暴露单一 mpp，按同值
      用于两轴并如实标注来源）；缺失 → 仅像素单位 + warning；
    - 连通域计数经小目标过滤（占比阈值，下限 4 px），语义分割 → 计数为
      近似值（approx_object_count）。
    """
    warnings = []
    src = region_payload.get("src") or {}
    out_w = int(region_payload.get("width") or mask_w)
    out_h = int(region_payload.get("height") or mask_h)
    src_w = _to_float(src.get("w"))
    src_h = _to_float(src.get("h"))
    if not src_w or not src_h or src_w <= 0 or src_h <= 0:
        # 防御态：src 缺失时按等比 1:1 处理并告警（面积失真）
        src_w, src_h = float(out_w), float(out_h)
        warnings.append("region 响应缺少 level-0 src，面积按输出像素 1:1 估算")
    if (mask_w, mask_h) != (out_w, out_h):
        raise PlatformError(
            503, "inference_contract",
            "mask 尺寸 (%dx%d) 与输入图像 (%dx%d) 不一致"
            % (mask_w, mask_h, out_w, out_h), retryable=True)
    scale_x = src_w / float(out_w)
    scale_y = src_h / float(out_h)
    scale_um2 = scale_x * scale_y

    positive = sum(sum(row) for row in grid)
    area_l0_px = positive * scale_um2
    roi_out_px = out_w * out_h
    area_fraction = (positive / float(roi_out_px)) if roi_out_px > 0 else 0.0

    mpp_x = mpp_y = None
    if mpp is not None and mpp > 0:
        mpp_x = mpp_y = float(mpp)
    else:
        warnings.append("切片 MPP 缺失或无效：面积仅给 level-0 像素单位，"
                        "不给 µm²")

    area_um2 = None
    dist_um2 = None
    if mpp_x and mpp_y:
        area_um2 = area_l0_px * mpp_x * mpp_y

    min_component_px = max(4, int(round(roi_out_px * _MIN_COMPONENT_FRACTION)))
    comps, labels = connected_components(grid, return_labels=True)
    kept = [c for c in comps if c["area_px"] >= min_component_px]
    filtered_out = len(comps) - len(kept)
    if filtered_out:
        warnings.append(
            "小目标过滤：%d 个 <%d 输出像素的连通域未计入 approx_object_count"
            % (filtered_out, min_component_px))

    # ---- 计数拆分（近似；guard 见下）---- #
    # 语义分割常把密集目标带糊成一片（真实 E2E：整条上皮细胞带一块 mask），
    # 纯连通域计数严重偏低。加距离变换 + 分水岭拆分，但**带 guard**：
    # objects_resolved = 前景中「面积 ≤ 3×期望单目标面积」的连通域占比；
    # 大多数前景已分辨（≥0.6）才给 estimated_count_split，否则 null +
    # mask_is_region_not_instances（拆一整条细胞带只会发明伪目标）。
    objects_resolved = None
    split_count = None
    split_method = None
    um_per_out_px = None
    if mpp_x and mpp_y:
        um_per_out_px = mpp_x * scale_x  # 每输出像素的 µm（x 轴；y 近似同）
        diameter_out_px = _EXPECTED_DIAMETER_UM / um_per_out_px
        expected_area_out = math.pi * (diameter_out_px / 2.0) ** 2
        kept_fg = sum(c["area_px"] for c in kept)
        if kept_fg > 0:
            resolved_px = sum(c["area_px"] for c in kept
                              if c["area_px"] <=
                              _RESOLVED_AREA_FACTOR * expected_area_out)
            objects_resolved = resolved_px / float(kept_fg)
        if kept and objects_resolved is not None \
                and objects_resolved >= _OBJECTS_RESOLVED_MIN:
            min_distance_px = max(1.0, diameter_out_px / 2.0)
            kept_labels = {i + 1 for i, c in enumerate(comps)
                           if c["area_px"] >= min_component_px}
            split_grid = [[1 if labels[y][x] in kept_labels else 0
                           for x in range(mask_w)] for y in range(mask_h)]
            split_count = _watershed_split_count(
                split_grid, min_distance_px=min_distance_px,
                min_area_px=min_component_px)
            split_method = ("distance_transform(chamfer 3-4) + priority-flood "
                            "watershed; min_distance=期望直径/2（输出像素）")
        elif kept:
            warnings.append(
                "mask_is_region_not_instances：前景主要是成片区域（"
                "objects_resolved=%.2f < %.2f，覆盖组织区域/细胞带），"
                "不给出实例计数拆分，请以面积/占比为准"
                % (objects_resolved if objects_resolved is not None else 0.0,
                   _OBJECTS_RESOLVED_MIN))
    elif kept:
        warnings.append("MPP 缺失：无法换算期望目标尺寸，跳过实例计数拆分")

    areas_l0 = [c["area_px"] * scale_um2 for c in kept]
    if mpp_x and mpp_y:
        dist_um2 = _distribution([a * mpp_x * mpp_y for a in areas_l0])

    kept_sorted = sorted(kept, key=lambda c: c["area_px"], reverse=True)
    top = []
    for c in kept_sorted[:_MAX_COMPONENTS]:
        bx, by, bw, bh = c["bbox"]
        cx, cy = c["centroid"]
        area_px_l0 = c["area_px"] * scale_um2
        item = {
            "bbox_l0": [int(round(src.get("x", 0) + bx * scale_x)),
                        int(round(src.get("y", 0) + by * scale_y)),
                        int(max(1, round(bw * scale_x))),
                        int(max(1, round(bh * scale_y)))],
            "centroid_l0": [int(round(src.get("x", 0) + cx * scale_x)),
                            int(round(src.get("y", 0) + cy * scale_y))],
            "area_px_l0": area_px_l0,
        }
        if mpp_x and mpp_y:
            item["area_um2"] = area_px_l0 * mpp_x * mpp_y
        top.append(item)

    return {
        "foreground": {
            "positive_pixels_out": positive,
            "area_px_l0": area_l0_px,
            "area_fraction_of_roi": area_fraction,
            "area_um2": area_um2,
        },
        "mpp": {
            "x": mpp_x, "y": mpp_y,
            "source": ("slide_info.mpp（mpp_x；两轴同值）" if mpp_x else None),
        },
        "approx_object_count": {
            "connected_components": len(kept),
            "estimated_count_split": split_count,
            "objects_resolved": (round(objects_resolved, 3)
                                 if objects_resolved is not None else None),
            "split_method": split_method,
            "expected_diameter_um": (_EXPECTED_DIAMETER_UM
                                     if um_per_out_px else None),
            "min_component_px_out": min_component_px,
            "counting_note": "PathSegmentor 是语义分割（给目标的区域/面积），"
                             "不是实例计数器：connected_components 为连通域"
                             "近似值；estimated_count_split 为距离变换+分水岭"
                             "拆分估计（objects_resolved≥%.2f 才给出），"
                             "两者皆需人工核对" % _OBJECTS_RESOLVED_MIN,
        },
        "size_distribution": {
            "components": len(kept),
            "area_px_l0": _distribution(areas_l0),
            "area_um2": dist_um2,
        },
        "components_top": top,
        "warnings": warnings,
    }


# --------------------------------------------------------------------------- #
# 推理服务
# --------------------------------------------------------------------------- #
def _infer(image_png_bytes, obj, region, threshold):
    """POST /v1/segment；busy/not_ready/不可达/契约不符 → PlatformError。"""
    b64 = base64.b64encode(image_png_bytes).decode("ascii")
    payload = {"image_b64": b64, "object": obj}
    if region:
        payload["region"] = region
    if threshold is not None:
        payload["threshold"] = threshold
    try:
        r = requests.post(_INFER_URL + "/v1/segment", json=payload,
                          timeout=_INFER_TIMEOUT_S)
    except (requests.ConnectionError, requests.Timeout):
        raise PlatformError(503, "inference_unavailable",
                            "推理服务不可达或超时（%s），可稍后重试" % _INFER_URL,
                            retryable=True)
    if r.status_code == 400:
        try:
            body = r.json() or {}
        except Exception:
            body = {}
        raise PlatformError(400, "invalid_argument",
                            "推理服务拒绝输入：%s" % (body.get("message")
                                                    or body.get("error") or r.text[:200]))
    if r.status_code == 503:
        try:
            body = r.json() or {}
        except Exception:
            body = {}
        reason = str(body.get("error") or "busy")
        raise PlatformError(503, "inference_" + reason,
                            "推理服务暂不可用（%s），可稍后重试" % reason,
                            retryable=True)
    if r.status_code != 200:
        raise PlatformError(503, "inference_error",
                            "推理服务返回 HTTP %d" % r.status_code, retryable=True)
    try:
        body = r.json() or {}
        png_bytes = base64.b64decode(body["mask_png_b64"])
    except Exception:
        raise PlatformError(503, "inference_contract",
                            "推理服务响应缺少 mask_png_b64", retryable=True)
    return body, png_bytes


# --------------------------------------------------------------------------- #
# HTTP 端点
# --------------------------------------------------------------------------- #
@app.get("/healthz")
def healthz():
    """健康检查（manifest.service.health 指向这里）。"""
    return jsonify(ok=True, plugin=_PLUGIN_ID, version="0.1.0",
                   inference_url=_INFER_URL, cellpose_url=_CELLPOSE_URL,
                   capabilities=[_CAPABILITY_SEGMENT, _CAPABILITY_COUNT])


def _capability_ctx():
    """两个能力端点共用的入口守卫与请求上下文。

    校验 X-Dispatch-Principal / 委托凭证 / 安装凭证配置 / slide+slide_id
    双字段。返回 (None, ctx) 或 (error_response, None)；ctx 含
    {slide, slide_id, delegated, body}。
    """
    principal = _dispatch_principal()
    if not principal or principal.get("type") != "agent":
        return _error(401, "unauthorized",
                      "缺少或非法 X-Dispatch-Principal（只接受平台 dispatch 转发）"), None
    delegated = _delegated_token()
    if not delegated:
        return _error(
            401, "unauthorized",
            "缺少 X-Delegated-Read-Token（须由平台 dispatch 签发转发；"
            "直连调用不受理）"), None
    if not (_INSTALLATION_ID and _INSTALLATION_SECRET):
        return _error(503, "backend_not_configured",
                      "插件后端未配置平台安装凭证（PT_INSTALLATION_ID/"
                      "PT_INSTALLATION_SECRET）", retryable=True), None
    body = request.get_json(silent=True) or {}
    slide = body.get("slide") or ""
    if not isinstance(slide, str) or not slide:
        return _error(400, "invalid_request", "body.slide 必填"), None
    # P2 双字段（合同 §6.3）：dispatch 转发的 slide_id（签名 claim 权威值）。
    # 给出 → 平台回调一律走 by-id 路由（id_bundle 行 legacy_filename=NULL，
    # 按名回调恒 404）；仅名（兼容形态）走 by-name。
    slide_id = body.get("slide_id")
    if slide_id is not None:
        if not isinstance(slide_id, str) or not slide_id.strip():
            return _error(400, "invalid_request",
                          "body.slide_id 需为非空字符串"), None
        slide_id = slide_id.strip()
    return None, {"slide": slide, "slide_id": slide_id,
                  "delegated": delegated, "body": body}


@app.post("/capabilities/segment_region")
def capability_segment_region():
    """只读测量能力 segment_region（manifest.provides 声明的服务端端点）。"""
    t0 = time.time()
    cerr, ctx = _capability_ctx()
    if cerr is not None:
        return cerr
    slide = ctx["slide"]
    slide_id = ctx["slide_id"]
    delegated = ctx["delegated"]
    args, aerr = _validate_args(ctx["body"].get("arguments"))
    if aerr is not None:
        return _error(aerr.status, aerr.code, aerr.message)

    timings = {}
    try:
        t = time.time()
        jwt = _installation_jwt()
        info = _platform_slide_info(slide, jwt, slide_id=slide_id,
                                    delegated=delegated)
        target_edge, coarse = _target_long_edge(args["w"], args["h"],
                                                info.get("mpp"))
        img_bytes, region_payload = _platform_read_region(
            slide, args, target_edge, delegated, jwt, slide_id=slide_id)
        timings["region_read_ms"] = int((time.time() - t) * 1000)

        t = time.time()
        infer_body, mask_png = _infer(img_bytes, args["object"],
                                      args.get("region"), args.get("threshold"))
        timings["infer_ms"] = int((time.time() - t) * 1000)
    except PlatformError as e:
        return _error(e.status, e.code, e.message, retryable=e.retryable,
                      extra=e.extra)

    t = time.time()
    grid, mask_w, mask_h = mask_to_grid(mask_png)
    analysis = analyze_mask(grid, mask_w, mask_h, region_payload,
                            info.get("mpp"))
    timings["postprocess_ms"] = int((time.time() - t) * 1000)
    timings["total_ms"] = int((time.time() - t0) * 1000)

    warnings = list(analysis.get("warnings") or [])
    if coarse and info.get("mpp"):
        warnings.append(
            "ROI 大于 %.0f 输出像素上限：输出分辨率粗于目标 %.2f µm/px"
            % (_MAX_LONG_EDGE, _TARGET_MPP))
    # 推理服务的 warnings 一并透出（不透传内部细节之外的字段）
    for w in (infer_body.get("warnings") or []):
        if isinstance(w, str) and w:
            warnings.append("inference: " + w)

    result = {
        "analysis_id": "psm-" + secrets.token_hex(8),
        "plugin": _PLUGIN_ID,
        "capability": _CAPABILITY_SEGMENT,
        "model": {
            "model_id": infer_body.get("model_id"),
            "model_sha256": infer_body.get("model_sha256"),
            "code_rev": infer_body.get("code_rev"),
        },
        "prompt_used": infer_body.get("prompt_used"),
        "object": args["object"],
        "region_hint": args.get("region"),
        "threshold": infer_body.get("threshold")
        if infer_body.get("threshold") is not None else args.get("threshold"),
        "slide": slide,
        "slide_id": slide_id,
        "slide_revision": region_payload.get("asset_revision")
        or info.get("asset_revision"),
        "source_bbox": {"x": args["x"], "y": args["y"],
                        "w": args["w"], "h": args["h"]},
        "rendered": {
            "src": region_payload.get("src"),
            "width": region_payload.get("width"),
            "height": region_payload.get("height"),
            "read_level": region_payload.get("read_level"),
            "upsampled": region_payload.get("upsampled"),
        },
        "timings_ms": timings,
        "warnings": warnings,
    }
    result.update(analysis)
    return jsonify(result=result)


# --------------------------------------------------------------------------- #
# count_cells（Cellpose 实例分割计数）
# --------------------------------------------------------------------------- #
def _validate_count_args(args):
    """count_cells 参数校验：bbox（同 segment_region）+ resolution +
    diameter_um/model。

    resolution 是**固定枚举**（处理长边上限，输出像素；agent 只在
    固定选项里选）——与 diameter_um、切片实际 MPP 一起决定模型内部缩放，
    任意组合超资源预算在调用前被拒绝（见 capability_count_cells）。
    返回 (normalized, None) 或 (None, PlatformError 400)。
    """
    out, aerr = _validate_bbox_args(args)
    if aerr is not None:
        return None, aerr
    res = args.get("resolution")
    if res is None:
        out["resolution"] = _COUNT_RESOLUTION_DEFAULT
    else:
        if isinstance(res, bool) or not isinstance(res, int) \
                or res not in _COUNT_RESOLUTIONS:
            return None, PlatformError(
                400, "invalid_request",
                "resolution 需为 %s 之一（处理长边上限，输出像素；缺省 %d）"
                % ("/".join(str(r) for r in _COUNT_RESOLUTIONS),
                   _COUNT_RESOLUTION_DEFAULT))
        out["resolution"] = res
    d = args.get("diameter_um")
    if d is not None:
        f = _to_float(d)
        if f is None or not (_COUNT_DIAMETER_MIN_UM <= f
                             <= _COUNT_DIAMETER_MAX_UM):
            return None, PlatformError(
                400, "invalid_request",
                "diameter_um 需为 %g..%g 的数值（µm）"
                % (_COUNT_DIAMETER_MIN_UM, _COUNT_DIAMETER_MAX_UM))
        out["diameter_um"] = f
    model = args.get("model")
    if model is not None:
        if not isinstance(model, str) or not model.strip():
            return None, PlatformError(400, "invalid_request",
                                       "model 需为非空字符串或省略")
        out["model"] = model.strip()[:64]
    return out, None


def _infer_cellpose(image_png_bytes, mpp, diameter_um=None, model=None):
    """POST /v1/count（Cellpose 计数合同）；错误映射与 PSM 同款。

    ``mpp`` 是**推理输入图**的 µm/px（调用方按实际渲染缩放换算，R3）；缺失
    时序列化为 JSON ``null``（R4：真实服务对非空 MPP 要求 >0，发 0.0 会被
    400 拒绝；null → 服务按模型缺省尺度计数并附 warning）。
    返回 (body_dict)；实例坐标为输出图像素（由调用方映射回 level-0）。
    """
    payload = {"image_b64": base64.b64encode(image_png_bytes).decode("ascii"),
               "mpp": mpp if (mpp is not None and mpp > 0) else None}
    if diameter_um is not None:
        payload["diameter_um"] = diameter_um
    if model:
        payload["model"] = model
    try:
        r = requests.post(_CELLPOSE_URL + "/v1/count", json=payload,
                          timeout=_INFER_TIMEOUT_S)
    except (requests.ConnectionError, requests.Timeout):
        raise PlatformError(503, "inference_unavailable",
                            "Cellpose 计数服务不可达或超时（%s），可稍后重试"
                            % _CELLPOSE_URL, retryable=True)
    if r.status_code == 400:
        try:
            body = r.json() or {}
        except Exception:
            body = {}
        # 服务端资源准入（与插件侧同式，双保险）透传专用码，便于 agent
        # 理解「改 ROI/参数后重试」而不是当成坏参数
        code = ("resource_limit"
                if str(body.get("error") or "") == "resource_limit"
                else "invalid_argument")
        raise PlatformError(400, code,
                            "计数服务拒绝输入：%s"
                            % (body.get("message") or body.get("error")
                               or r.text[:200]))
    if r.status_code == 503:
        try:
            body = r.json() or {}
        except Exception:
            body = {}
        reason = str(body.get("error") or "busy")
        raise PlatformError(503, "inference_" + reason,
                            "计数服务暂不可用（%s），可稍后重试" % reason,
                            retryable=True)
    if r.status_code != 200:
        raise PlatformError(503, "inference_error",
                            "计数服务返回 HTTP %d" % r.status_code,
                            retryable=True)
    body = r.json() or {}
    if not isinstance(body.get("instance_count"), int) \
            or isinstance(body.get("instance_count"), bool) \
            or body.get("instance_count") < 0:
        raise PlatformError(503, "inference_contract",
                            "计数服务响应缺少 instance_count", retryable=True)
    return body


def _map_instance(item, src, scale_x, scale_y, mpp_x, mpp_y):
    """服务端实例（输出像素 bbox/centroid/area）→ level-0 + µm²。"""
    bbox = item.get("bbox") or [0, 0, 0, 0]
    centroid = item.get("centroid") or [0.0, 0.0]
    ox, oy = float(src.get("x", 0)), float(src.get("y", 0))
    area_px_l0 = _to_float(item.get("area_px"))
    if area_px_l0 is not None:
        area_px_l0 *= scale_x * scale_y
    out = {
        "bbox_l0": [int(round(ox + float(bbox[0]) * scale_x)),
                    int(round(oy + float(bbox[1]) * scale_y)),
                    int(max(1, round(float(bbox[2]) * scale_x))),
                    int(max(1, round(float(bbox[3]) * scale_y)))],
        "centroid_l0": [int(round(ox + float(centroid[0]) * scale_x)),
                        int(round(oy + float(centroid[1]) * scale_y))],
        "area_px_l0": area_px_l0,
        "area_um2": (area_px_l0 * mpp_x * mpp_y
                     if (mpp_x and mpp_y and area_px_l0 is not None) else None),
    }
    return out


@app.post("/capabilities/count_cells")
def capability_count_cells():
    """只读计数能力 count_cells（Cellpose 实例分割；manifest.provides）。

    与 segment_region 共用入口守卫/委托凭证/by-id 读路径；差异：
      - 渲染在 Cellpose 的实际处理尺度上：``mpp_render = max(原生, d/30)``、
        所需长边 = ceil(L_level0 × 原生 / mpp_render)（R2 重设计）；
        ``resolution``（512/1024/2048，缺省 2048）只是**处理长边上限**选项，
        所需长边或处理预算超限在读 ROI **之前**拒绝（400 resource_limit，
        不做粗渲染 + 模型上采样）；mpp 缺失时按所选选项长边渲染（算力 =
        edge²）；
      - 推理输入 MPP = 原生 MPP × **实际渲染缩放**（由实际返回的 src 与
        输出尺寸计算，R3），缺失/无效的原生 MPP 规范为 JSON null（R4）；
      - 调 CELLPOSE_URL POST /v1/count 前按实际返回几何再过一次缩放预算
        （防御；服务端闸仍在）；错误引导按真实物理（缩小 ROI / 增大
        diameter，resolution 不改变处理量）；
      - 返回 instance_count / 密度 / 面积分布 µm² / ≤20 示例实例
        （映射回 level-0），无 label map / base64。
    """
    t0 = time.time()
    cerr, ctx = _capability_ctx()
    if cerr is not None:
        return cerr
    slide = ctx["slide"]
    slide_id = ctx["slide_id"]
    delegated = ctx["delegated"]
    args, aerr = _validate_count_args(ctx["body"].get("arguments"))
    if aerr is not None:
        return _error(aerr.status, aerr.code, aerr.message)

    timings = {}
    warnings = []
    try:
        t = time.time()
        jwt = _installation_jwt()
        info = _platform_slide_info(slide, jwt, slide_id=slide_id,
                                    delegated=delegated)
        mpp = _normalize_mpp(info.get("mpp"))  # level-0 原生 MPP（可能 None）
        resolution = args["resolution"]
        diameter_um = args.get("diameter_um") or _COUNT_DIAMETER_DEFAULT_UM
        # R2 重设计：渲染在 Cellpose 的实际处理尺度（d/30 µm/px 或原生）上，
        # resolution 只是**处理长边上限**选项；处理量 = (30×视场/diameter)²
        # 与渲染分辨率无关。超预算/超选项在读 ROI 之前拒绝——不做「粗渲染
        # + 模型上采样」。
        plan = _count_render_plan(args["w"], args["h"], mpp, diameter_um,
                                  resolution)
        if mpp is not None:
            if plan["scaled_px"] > _CELLPOSE_MAX_SCALED_PIXELS:
                fov_um = max(args["w"], args["h"]) * mpp
                raise PlatformError(
                    400, "resource_limit",
                    "该 ROI（level-0 %dx%d px ≈ %.0f µm 视场）在 Cellpose "
                    "处理分辨率（%.4f µm/px）下约 %dx%d px（%d 像素），超出 "
                    "%d 像素推理预算。处理量只取决于视场与直径（处理边 = "
                    "30×视场/diameter），调整 resolution 无法改变；请缩小 "
                    "ROI（拆分为多个 tile）或增大 diameter_um 后重试"
                    % (args["w"], args["h"], fov_um, plan["mpp_render"],
                       plan["scaled_long"], plan["scaled_short"],
                       plan["scaled_px"], _CELLPOSE_MAX_SCALED_PIXELS),
                    extra={"scaled_pixels": int(plan["scaled_px"]),
                           "budget_pixels": _CELLPOSE_MAX_SCALED_PIXELS,
                           "resolution": resolution,
                           "diameter_um": diameter_um})
            if plan["edge_long"] > resolution:
                max_um, max_l0 = _count_max_roi(mpp, diameter_um, resolution)
                raise PlatformError(
                    400, "resource_limit",
                    "该 ROI 需要约 %d px 处理长边（level-0 %dx%d px @ "
                    "%.4f µm/px，按 %.1f µm 直径以 %.4f µm/px 渲染），超出 "
                    "resolution=%d 选项上限。请改选更大的 resolution（%s），"
                    "或将 ROI 拆分为多个 tile 分别计数"
                    % (plan["edge_long"], args["w"], args["h"], mpp,
                       diameter_um, plan["mpp_render"], resolution,
                       "/".join(str(r) for r in _COUNT_RESOLUTIONS)),
                    extra={"max_roi_um": [round(max_um, 1), round(max_um, 1)],
                           "max_roi_level0_px": [max_l0, max_l0],
                           "resolution": resolution,
                           "diameter_um": diameter_um})
        img_bytes, region_payload = _platform_read_region(
            slide, args, plan["edge_long"], delegated, jwt, slide_id=slide_id)
        timings["region_read_ms"] = int((time.time() - t) * 1000)

        src = region_payload.get("src") or {}
        out_w = _to_float(region_payload.get("width"))
        out_h = _to_float(region_payload.get("height"))
        src_w = _to_float(src.get("w"))
        src_h = _to_float(src.get("h"))
        if not out_w or not out_h or not src_w or not src_h \
                or out_w <= 0 or out_h <= 0 or src_w <= 0 or src_h <= 0:
            raise PlatformError(503, "platform_contract",
                                "region 响应缺少可用的尺寸元数据",
                                retryable=True)
        # R3：推理输入 MPP 从实际 src/输出尺寸算（不是请求目标值）
        scale_x, scale_y, infer_mpp = _inference_scale(
            mpp, src_w, src_h, out_w, out_h)

        # 资源准入防御（按实际返回几何；正常路径与预准入一致，不触发）。
        scaled_px = _cellpose_scaled_pixels(out_w, out_h, infer_mpp,
                                            diameter_um)
        if scaled_px is None or scaled_px > _CELLPOSE_MAX_SCALED_PIXELS:
            raise PlatformError(
                400, "resource_limit",
                "Cellpose 将处理约 %s 像素（实际输入 %dx%d px、推理分辨率 "
                "%s µm/px、直径 %.1f µm），超出 %d 像素预算；处理量只取决"
                "于视场与直径，请缩小 ROI（拆分 tile）或增大 diameter_um"
                % ("未知" if scaled_px is None else "%d" % int(scaled_px),
                   int(out_w), int(out_h),
                   ("%.4f" % infer_mpp) if infer_mpp else "未知",
                   diameter_um, _CELLPOSE_MAX_SCALED_PIXELS),
                extra={"scaled_pixels": (None if scaled_px is None
                                         else int(scaled_px)),
                       "budget_pixels": _CELLPOSE_MAX_SCALED_PIXELS,
                       "resolution": resolution,
                       "diameter_um": diameter_um})

        t = time.time()
        infer_body = _infer_cellpose(img_bytes, infer_mpp,
                                     diameter_um=args.get("diameter_um"),
                                     model=args.get("model"))
        timings["infer_ms"] = int((time.time() - t) * 1000)
    except PlatformError as e:
        return _error(e.status, e.code, e.message, retryable=e.retryable,
                      extra=e.extra)

    mpp_x = mpp_y = None
    if mpp is not None and mpp > 0:
        mpp_x = mpp_y = float(mpp)
    else:
        warnings.append("切片 MPP 缺失或无效：不给密度与 µm²，仅像素口径")

    instance_count = int(infer_body["instance_count"])
    roi_mm2 = None
    density = None
    if mpp_x and mpp_y:
        roi_mm2 = (src_w * mpp_x / 1000.0) * (src_h * mpp_y / 1000.0)
        if roi_mm2 > 0:
            density = instance_count / roi_mm2
    areas_px = infer_body.get("areas_px") or {}
    area_stats_um2 = None
    if mpp_x and mpp_y and isinstance(areas_px, dict):
        um2_per_out_px = (scale_x * scale_y) * mpp_x * mpp_y
        area_stats_um2 = {k: _to_float(v) * um2_per_out_px
                          for k, v in areas_px.items()
                          if _to_float(v) is not None}

    raw_top = infer_body.get("instances_top") or []
    if not isinstance(raw_top, list):
        raw_top = []
        warnings.append("计数服务 instances_top 形状异常，已忽略")
    if len(raw_top) > _MAX_COMPONENTS:
        raw_top = raw_top[:_MAX_COMPONENTS]
    instances = []
    for item in raw_top:
        if not isinstance(item, dict):
            continue
        try:
            instances.append(_map_instance(item, src, scale_x, scale_y,
                                           mpp_x, mpp_y))
        except (TypeError, ValueError, IndexError):
            warnings.append("一个实例条目形状异常，已跳过")
    timings["postprocess_ms"] = int((time.time() - t) * 1000)
    timings["total_ms"] = int((time.time() - t0) * 1000)

    if mpp is not None and plan["rescale"] > 1.0 + 1e-9:
        warnings.append(
            "切片原生分辨率（%.4f µm/px）粗于 %.1f µm 直径的处理尺度（%.4f "
            "µm/px）：模型内部上采样 %.2f×，不增加信息；结果以原生渲染为准"
            % (mpp, diameter_um, diameter_um / _CELLPOSE_DIAM_MEAN_PX,
               plan["rescale"]))
    for w in (infer_body.get("warnings") or []):
        if isinstance(w, str) and w:
            warnings.append("inference: " + w)

    result = {
        "analysis_id": "cpose-" + secrets.token_hex(8),
        "plugin": _PLUGIN_ID,
        "capability": _CAPABILITY_COUNT,
        "model": {
            "model_id": infer_body.get("model_id"),
            "model_name": infer_body.get("model_name"),
            "model_sha256": infer_body.get("model_sha256"),
            "code_version": infer_body.get("code_version"),
        },
        "diameter_um": args.get("diameter_um") or _COUNT_DIAMETER_DEFAULT_UM,
        "diameter_px_used": _to_float(infer_body.get("diameter_px_used")),
        "instance_count": instance_count,
        "instance_count_note": "Cellpose 实例分割计数（近似值，需人工核对）",
        "density": {
            "per_mm2": density,
            "roi_area_mm2": roi_mm2,
            "basis": ("实际成像 ROI（level-0 bbox 经边界夹取后的 src）"
                      if roi_mm2 is not None else None),
        },
        "area_stats_px": areas_px if isinstance(areas_px, dict) else {},
        "area_stats_um2": area_stats_um2,
        "instances_top": instances,
        "instances_top_note": "≤20 个实例的 level-0 坐标示例（非全量）",
        "slide": slide,
        "slide_id": slide_id,
        "slide_revision": region_payload.get("asset_revision")
        or info.get("asset_revision"),
        "source_bbox": {"x": args["x"], "y": args["y"],
                        "w": args["w"], "h": args["h"]},
        "rendered": {
            "src": region_payload.get("src"),
            "width": region_payload.get("width"),
            "height": region_payload.get("height"),
            "read_level": region_payload.get("read_level"),
            "upsampled": region_payload.get("upsampled"),
            "requested_long_edge": plan["edge_long"],
        },
        "mpp": {
            "x": mpp_x, "y": mpp_y,
            "source": ("slide_info.mpp（mpp_x；两轴同值）" if mpp_x else None),
        },
        "resolution": resolution,
        "inference_input": {
            "mpp": infer_mpp,
            "scale": {"x": scale_x, "y": scale_y},
            "mpp_render": plan["mpp_render"],
            "note": "推理输入 MPP = 原生 MPP × 实际渲染缩放（实际 src/输出"
                    "尺寸，R3）；渲染尺度 = max(原生, diameter/30)（R2 重设"
                    "计）；原生 MPP 仅用于结果映射",
        },
        "timings_ms": timings,
        "warnings": warnings,
    }
    return jsonify(result=result)


if __name__ == "__main__":
    port = int(os.environ.get("PT_PATHSEGMENTOR_MEASURE_PORT") or 8062)
    # use_reloader=False：容器内由 docker_entry.sh 托管，禁止 Flask debug
    # reloader 再 fork 一份（否则 PID 关系乱、端口冲突）。
    app.run(host="127.0.0.1", port=port, use_reloader=False, threaded=True)
