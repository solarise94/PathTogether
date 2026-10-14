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
     实现）读取 ROI：segment_region 目标 ~0.25 µm/px；count_cells（2026-10-14
     方形取景重设计）不接受模型提供的 x/y/w/h——位置只来自平台验签转发的
     ``context.snapshot``（HP 执行阶段冻结的服务端当前快照），插件在快照内
     自动生成不越界 level-0 **正方形**（中心定位、靠边平移、更小缩边），
     在 Cellpose 的实际处理尺度（max(原生, diameter/30) µm/px）上渲染方形
     输入，resolution（512/1024/2048）只是**模型处理边长上限**（含 Cellpose
     内部放大；每档最大视场 = resolution×diameter/30 µm，与切片 MPP 无关）
     ——取不出满足上限的方形在读 ROI 前拒绝；同时取 slide_info 的原生 MPP
     做 µm² 换算（缺失/无效 → null，仅像素口径）；
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
    0.25）；count_cells 的模型处理长边上限为固定枚举（512/1024/2048，缺省
    2048，常量 ``_COUNT_RESOLUTIONS``，无 env）；
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
#: 处理长边**上限**固定枚举（模型处理像素长边；R2 重设计：agent 只在固定
#: 选项里选）。Cellpose 的处理量与渲染分辨率无关（cpsam 内部按
#: rescale=30/diameter_px 把输入缩放到 d/30 µm/px 的处理尺度，处理边 =
#: 30×视场/diameter），因此 resolution 不是「渲染目标」，而是**允许的模型
#: 处理长边上限**：所需处理长边（含 Cellpose 内部放大，原生 MPP 粗于 d/30
#: 时内部 rescale > 1）超过选项 → 读 ROI 前拒绝（不做「粗渲染 + 模型上采
#: 样」——那只是扔掉真像素、算力不变）。cellpose 4.2.1.1 逐轴按
#: ``int(size×rescale)`` 截断取整（core.py run_net / transforms.resize_image），
#: 预读准入与读后硬闸共用同一镜像计算（``_cellpose_processing_dims``）。
#: 仅 mpp 缺失（处理尺度不可判定）时 resolution 才直接决定渲染长边。
#: 每档最大视场与切片 MPP 无关：max FOV = resolution × diameter/30 µm
#: （d=7：512→119.47、1024→238.93、2048→477.87 µm；时间成本约随
#: option² 增长）。2048 为服务输入上限（平台 regions 路由 max_long_edge
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
#: Cellpose 阈值合法域与缺省——**权威定义在 services/cellpose/cellpose_infer.py**
#: （DEFAULT_FLOW_THRESHOLD=0.4 / DEFAULT_CELLPROB_THRESHOLD=0.0）；两处独立
#: 部署，此处常量镜像（插件前置校验 + 显式转发，服务端 _validate_request 仍
#: 按同域复核）。方形取景合同 v1 §5：阈值是仅有的可选模型参数（连同档位与
#: 直径），缺省值不在请求里重复定义第二处语义。
_CELLPOSE_FLOW_THRESHOLD_DEFAULT = 0.4
_CELLPOSE_FLOW_THRESHOLD_MIN = 0.0
_CELLPOSE_FLOW_THRESHOLD_MAX = 1.0
_CELLPOSE_CELLPROB_THRESHOLD_DEFAULT = 0.0
_CELLPOSE_CELLPROB_THRESHOLD_MIN = -6.0
_CELLPOSE_CELLPROB_THRESHOLD_MAX = 6.0
#: 平台委托读凭证的 level-0 ROI 像素上限（w*h）——镜像 app.py
#: _DELEGATED_READ_ROI_MAX_PIXELS 缺省 4096²（env PLUGIN_DELEGATED_ROI_MAX_PIXELS
#: 可收紧）。方形规划用它封顶 level-0 边长，避免注定被平台 403 roi_over_cap
#: 拒绝的读图尝试；平台读端点仍按凭证逐 claim 复核（这里只是前置防线）。
_PLATFORM_ROI_MAX_PIXELS = 4096 * 4096

#: count_cells 模型可见参数白名单（方形取景合同 v1 §5）：位置、尺寸、MPP、
#: 快照/切片身份、渲染上下文、labels、模型名等一律不收——schema
#: additionalProperties:false 之外，后端对任何其他键同样 400（不只靠
#: description 隐藏）。
_COUNT_ALLOWED_ARGS = ("resolution", "diameter_um", "flow_threshold",
                       "cellprob_threshold")

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


def _snapshot_context(body):
    """提取平台 dispatch 转发的当前快照上下文（wire 合同 v1 §4）。

    位置**只**来自 ``body.context.snapshot``（平台网关验签后转发的结构化
    上下文）——绝不从模型 arguments、slide 名或其他来源推断。缺失 →
    400 ``snapshot_required``（模型须先 goto 再调用）；形状非法 → 400
    ``invalid_request``（网关转发形态被破坏，属调用方契约错误）。

    返回 (snapshot_dict, None) 或 (None, PlatformError)。
    """
    ctx = body.get("context") if isinstance(body, dict) else None
    snap = ctx.get("snapshot") if isinstance(ctx, dict) else None
    if snap is None:
        return None, PlatformError(
            400, "snapshot_required",
            "缺少当前快照上下文：请先用 goto 查看要计数的位置，再调用本工具")
    if not isinstance(snap, dict):
        return None, PlatformError(400, "invalid_request",
                                   "context.snapshot 需为对象")
    bbox = snap.get("bbox")
    if not isinstance(bbox, dict):
        return None, PlatformError(400, "invalid_request",
                                   "context.snapshot.bbox 需为对象")
    out_bbox = {}
    for k in ("x", "y", "w", "h"):
        v = bbox.get(k)
        if isinstance(v, bool) or not isinstance(v, int) \
                or (v < 0 if k in ("x", "y") else v <= 0):
            return None, PlatformError(
                400, "invalid_request",
                "context.snapshot.bbox.%s 需为%s整数（level-0 像素）"
                % (k, "非负" if k in ("x", "y") else "正"))
        out_bbox[k] = v
    snap_id = snap.get("snapshot_id")
    if not isinstance(snap_id, str) or not snap_id:
        return None, PlatformError(400, "invalid_request",
                                   "context.snapshot.snapshot_id 需为非空字符串")
    rev = snap.get("slide_revision")
    if rev is not None and not isinstance(rev, str):
        return None, PlatformError(400, "invalid_request",
                                   "context.snapshot.slide_revision 需为字符串或 null")
    fp = snap.get("render_context_fingerprint")
    if fp is not None and not isinstance(fp, str):
        return None, PlatformError(
            400, "invalid_request",
            "context.snapshot.render_context_fingerprint 需为字符串或 null")
    return {"snapshot_id": snap_id, "bbox": out_bbox,
            "slide_revision": rev or None,
            "render_context_fingerprint": fp or None}, None


def _square_processing_dims(side_l0, render_edge, mpp, diameter_um):
    """方形计划的预读处理尺寸镜像（平台 ``_aspect_fit_size`` 取整 + cellpose
    4.2.1.1 逐轴 ``int(size×rescale)`` 截断；与读后硬闸同一镜像计算）。

    level-0 方形（side_l0²）以 ``max_long_edge=render_edge`` 等比渲染：
    方形的 aspect-fit 输出恒为 render_edge²（逐轴 round 恰好回到边长）；
    两轴均值推理 MPP = mpp×side/render_edge；处理尺寸 = 逐轴截断。
    ``mpp`` 缺失 → rescale 1.0、处理尺寸 = 输入尺寸。
    返回 (proc_w, proc_h) 或 None（直径/MPP 非法，调用方拒绝）。
    """
    out = max(1, min(int(render_edge), 4096))
    if mpp is None:
        return out, out
    infer_mpp = _axis_mean_mpp(mpp, side_l0, side_l0, out, out)
    return _cellpose_processing_dims(out, out, infer_mpp, diameter_um)


def _square_render_edge(side_l0, mpp, diameter_um):
    """方形 level-0 边长 → 渲染长边：``mpp_render = max(原生, d/30)``、
    ``edge = ceil(side×原生/mpp_render)``（永不上采样超过原生）。

    原生粗于处理尺度 → edge = side（原生像素）；细于处理尺度 → edge 按
    d/30 µm/px 渲染（模型内部 rescale≈1）。``mpp`` 缺失 → 原生像素
    （edge = side，rescale 1.0）。
    """
    side_l0 = max(1, int(side_l0))
    if mpp is None or mpp <= 0:
        return side_l0
    process_mpp = float(diameter_um) / _CELLPOSE_DIAM_MEAN_PX
    mpp_render = max(float(mpp), process_mpp)
    return max(1, int(math.ceil(side_l0 * float(mpp) / mpp_render)))


def _square_admits(side_l0, mpp, diameter_um, resolution):
    """给定 level-0 方形边长是否满足处理长边与总像素预算（预读镜像判定）。

    用 ``_square_processing_dims`` 的真实取整物理判定（不依赖理想公式）：
    max(proc_w, proc_h) ≤ resolution 且 proc_w×proc_h ≤ 8 Mi px 预算。
    ``mpp`` 缺失按 rescale 1.0（处理尺寸 = 输入）。
    """
    edge = _square_render_edge(side_l0, mpp, diameter_um)
    dims = _square_processing_dims(side_l0, edge, mpp, diameter_um)
    if dims is None:
        return False
    return max(dims) <= resolution and dims[0] * dims[1] <= \
        _CELLPOSE_MAX_SCALED_PIXELS


def _plan_count_square(snap_bbox, slide_w, slide_h, mpp, diameter_um,
                       resolution):
    """count_cells 方形规划器（2026-10-14 方形取景设计 §自动生成不越界的方形）。

    位置固定为当前快照 bbox 的**中心**；统一生成一个 level-0 正方形：
      1. 边长上限 = min(快照短边, 切片短边, 平台 ROI 像素上限开方)；已知
         原生 MPP 时再受 Cellpose 处理边长上限（档位）与 8 Mi px 预算约束
         ——从几何上限逐级递减、用真实取整物理验证（``_square_admits``），
         不依赖理想公式（ceil 渲染取整与逐轴截断的相互作用会让
         ``resolution×d/(30×mpp)`` 差 ±1~2 px）；MPP 缺失时边长直接 ≤ 档位
         （原生像素、rescale 1.0）。
      2. 方形必须完整落在当前快照 bbox **且**切片内：从快照中心整框定位
         （整数除法取左上角），靠边时平移整个方框；快照/切片更小则缩小
         边长（上限已含短边钳制）；**永不**给切片外补边。
      3. 永远是正方形：渲染为方形输入，不做长条拉伸。

    返回 (plan, None) 或 (None, PlatformError)。plan 字段：
    ``x/y/side``（level-0 方形 bbox）、``render_edge``（实际渲染长边）、
    ``reduced``（方形是否小于快照：任一轴被缩）、``snap``（快照上下文）。
    """
    sbx, sby = int(snap_bbox["x"]), int(snap_bbox["y"])
    sbw, sbh = int(snap_bbox["w"]), int(snap_bbox["h"])
    slide_w = int(slide_w or 0)
    slide_h = int(slide_h or 0)
    # 边长上限（几何）：快照短边（方形必须整个在快照内）∩ 切片短边 ∩ 平台
    # ROI 像素上限（side² ≤ ROI cap）。快照 ⊆ 切片，切片短边是防御钳制。
    side = min(sbw, sbh, slide_w if slide_w > 0 else sbw,
               slide_h if slide_h > 0 else sbh,
               int(math.isqrt(_PLATFORM_ROI_MAX_PIXELS)))
    # 处理边长上限（资源）：从几何上限逐级递减，用真实取整物理
    # （``_square_admits``：平台 aspect-fit 取整 + cellpose 逐轴截断）验证——
    # 不依赖理想公式（ceil 渲染取整与截断的相互作用会让理想式差 ±1~2 px，
    # 见 round-2/round-3 review 的边界例）。
    while side >= 1 and not _square_admits(side, mpp, diameter_um, resolution):
        side -= 1
    if side < 1:
        max_um = None
        if mpp is not None:
            budget_edge = math.floor(math.sqrt(_CELLPOSE_MAX_SCALED_PIXELS))
            max_um = min(float(resolution), budget_edge) \
                * float(diameter_um) / _CELLPOSE_DIAM_MEAN_PX
        raise PlatformError(
            400, "resource_limit",
            "当前快照内取不出满足 resolution=%d 处理上限的方形区域（直径 "
            "%.1f µm%s）；请改选更大的 resolution（%s）后重试"
            % (resolution, diameter_um,
               ("，切片原生 %.4f µm/px" % mpp) if mpp is not None else "",
               "/".join(str(r) for r in _COUNT_RESOLUTIONS)),
            extra={"resolution": resolution,
                   "diameter_um": diameter_um,
                   "max_square_um": (round(max_um, 2) if max_um is not None
                                     else None)})
    # 中心定位（整数左上角），平移后仍完整落在快照内（side ≤ 快照短边保证
    # 非空平移空间）；切片界内性是防御断言（快照 ⊆ 切片）。
    x = sbx + (sbw - side) // 2
    y = sby + (sbh - side) // 2
    x = max(sbx, min(x, sbx + sbw - side))
    y = max(sby, min(y, sby + sbh - side))
    if x < 0 or y < 0 or x + side > slide_w or y + side > slide_h:
        raise PlatformError(
            400, "invalid_request",
            "方形规划结果越出切片边界（快照上下文与切片元数据不一致），拒绝读取")
    return {"x": x, "y": y, "side": side,
            "render_edge": _square_render_edge(side, mpp, diameter_um),
            "reduced": side < min(sbw, sbh)}, None


def _count_max_side(native_mpp, diameter_um, resolution):
    """所选 resolution 选项可处理的最大方形 level-0 边长（供 resource_limit
    的 extra 使用）。``native_mpp`` 缺失 → None。

    合同：resolution = **模型处理长边**上限 → 每档最大视场与切片 MPP 无关：
    max side = resolution × diameter/30 µm（处理边 = 30×视场/diameter）；
    另受处理像素预算封顶（处理边 ≤ √预算 → 视场 ≤ √预算×d/30）。
    """
    if native_mpp is None or native_mpp <= 0:
        return None
    process_mpp = float(diameter_um) / _CELLPOSE_DIAM_MEAN_PX
    budget_edge = math.floor(math.sqrt(_CELLPOSE_MAX_SCALED_PIXELS))
    max_um = min(float(resolution), budget_edge) * process_mpp
    return int(math.floor(max_um / float(native_mpp)))


def _axis_mean_mpp(native_mpp, src_w, src_h, out_w, out_h):
    """两轴均值推理 MPP（``_inference_scale`` 同式同浮点序）。

    预读方形规划（``_square_processing_dims``）与读后硬闸共用本函数：同一几何下
    两者逐位一致，是「预准入放行 ⇒ 读后硬闸必过」的前提。
    """
    return (native_mpp * (src_w / float(out_w))
            + native_mpp * (src_h / float(out_h))) / 2.0


def _inference_scale(native_mpp, src_w, src_h, out_w, out_h):
    """实际推理尺度与输入 MPP（R3）：用平台**实际返回**的 src 与输出尺寸，
    不用请求目标值代替。

    输入 MPP = level-0 原生 MPP × 实际缩放（src/输出）。单一 MPP 合同
    （服务只收一个标量），两轴各向差按**平台渲染的取整物理**容忍：region
    渲染（``_aspect_fit_size``）保宽高比、逐轴 round——长轴输出恰为渲染
    长边，短轴 round 误差 ≤0.5 输出像素，因此小 ROI 上两轴 mpp 的相对差
    可达 1/min(out_w, out_h)（16 px 短边即 ≈3%，重试无益）。据此仅在输出
    **不是**「src 按长轴尺度的等比 round」时拒绝（anisotropic_scale）：
    取 k = out_long/src_long，``|out_short − round(src_short×k)| > 1`` 输出
    像素（等价口径：两轴 mpp 相对差 > max(1%, 1/min(out_w, out_h)) + ε 才
    算真各向异常）；否则照常发送两轴均值（直径换算对取整级各向差不敏感）。
    原生 MPP 本身仍用于结果映射（密度/µm²/坐标），保持不变。

    返回 ``(scale_x, scale_y, infer_mpp)``；native_mpp 缺失 → infer_mpp=None
    （服务按模型缺省尺度计数，见 _infer_cellpose）。
    """
    scale_x = src_w / float(out_w)
    scale_y = src_h / float(out_h)
    if native_mpp is None:
        return scale_x, scale_y, None
    mpp_x = native_mpp * scale_x
    mpp_y = native_mpp * scale_y
    if src_w >= src_h:
        k = out_w / float(src_w)
        expected_short = src_h * k
        actual_short = float(out_h)
    else:
        k = out_h / float(src_h)
        expected_short = src_w * k
        actual_short = float(out_w)
    if abs(actual_short - round(expected_short)) > 1.0:
        raise PlatformError(
            400, "anisotropic_scale",
            "region 渲染两轴尺度不一致（%.4f vs %.4f µm/px；输出 %dx%d 不"
            "是 src %dx%d 的等比 round）：单一 MPP 计数服务无法表达，"
            "请重试；持续出现请反馈平台"
            % (mpp_x, mpp_y, out_w, out_h, int(src_w), int(src_h)))
    return scale_x, scale_y, _axis_mean_mpp(native_mpp, src_w, src_h,
                                            out_w, out_h)


def _cellpose_processing_dims(out_w, out_h, infer_mpp, diameter_um):
    """cellpose 4.2.1.1 的**实际模型处理尺寸**（逐轴 int(size×rescale) 截断）。

    cpsam 在给出 diameter（输入图像像素）时按 ``rescale = 30/diameter_px``
    缩放输入（``CellposeModel.eval``，cellpose/models.py:267-269）；缩放后
    尺寸按每轴 ``int(size × rescale)`` **截断**取整（``run_net`` 的
    ``Lyr, Lxr = int(Ly0*rsz[0]), int(Lx0*rsz[1])``，cellpose/core.py:191；
    ``transforms.resize_image`` 同式，transforms.py:747-748）。
    ``infer_mpp`` 为两轴均值（``_axis_mean_mpp``）；缺失（null mpp）→
    rescale 1.0，处理尺寸 = 输入尺寸。直径/MPP 非法或 rescale 非有限 →
    None（调用方拒绝）。
    """
    w = max(1, int(out_w))
    h = max(1, int(out_h))
    if infer_mpp is None or infer_mpp <= 0:
        return w, h
    diameter_px = float(diameter_um) / float(infer_mpp)
    if not math.isfinite(diameter_px) or diameter_px <= 0:
        return None
    rescale = _CELLPOSE_DIAM_MEAN_PX / diameter_px
    if not math.isfinite(rescale) or rescale <= 0:
        return None
    return max(1, int(w * rescale)), max(1, int(h * rescale))


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
    """count_cells 参数校验（方形取景合同 v1 §5）：模型只提供档位与 Cellpose
    参数——``resolution`` 枚举 512/1024/2048（缺省 2048）、``diameter_um``
    4..100（缺省 7）、``flow_threshold`` 0..1（缺省 0.4）、
    ``cellprob_threshold`` -6..6（缺省 0）。

    ``additionalProperties: false`` 不只写在 schema：后端对**任何其他键**
    （x/y/w/h、center、side、bbox、mpp、snapshot_id、slide_id、
    render_context、return_labels、model 等）一律 400 ``invalid_request``——
    位置与身份只来自平台转发的 context.snapshot，绝不接受模型提供的几何。

    缺省值在此层解析为具体数值（与 services/cellpose/cellpose_infer.py 的
    权威定义镜像一致），资源计划与推理调用使用同一份冻结参数。
    返回 (normalized, None) 或 (None, PlatformError 400)。
    """
    if not isinstance(args, dict):
        return None, PlatformError(400, "invalid_request", "arguments 需为对象")
    for k in args:
        if k not in _COUNT_ALLOWED_ARGS:
            return None, PlatformError(
                400, "invalid_request",
                "count_cells 不接受参数 %r（仅允许 %s；位置由系统从当前快照"
                "自动选取，不接受模型提供的坐标/尺寸/身份字段）"
                % (k, "/".join(_COUNT_ALLOWED_ARGS)))
    out = {}
    res = args.get("resolution")
    if res is None:
        out["resolution"] = _COUNT_RESOLUTION_DEFAULT
    else:
        if isinstance(res, bool) or not isinstance(res, int) \
                or res not in _COUNT_RESOLUTIONS:
            return None, PlatformError(
                400, "invalid_request",
                "resolution 需为 %s 之一（模型处理边长上限；缺省 %d）"
                % ("/".join(str(r) for r in _COUNT_RESOLUTIONS),
                   _COUNT_RESOLUTION_DEFAULT))
        out["resolution"] = res
    d = args.get("diameter_um")
    if d is None:
        out["diameter_um"] = _COUNT_DIAMETER_DEFAULT_UM
    else:
        f = _to_float(d)
        if f is None or not (_COUNT_DIAMETER_MIN_UM <= f
                             <= _COUNT_DIAMETER_MAX_UM):
            return None, PlatformError(
                400, "invalid_request",
                "diameter_um 需为 %g..%g 的数值（µm）"
                % (_COUNT_DIAMETER_MIN_UM, _COUNT_DIAMETER_MAX_UM))
        out["diameter_um"] = f
    flow = args.get("flow_threshold")
    if flow is None:
        out["flow_threshold"] = _CELLPOSE_FLOW_THRESHOLD_DEFAULT
    else:
        f = _to_float(flow)
        if f is None or not (_CELLPOSE_FLOW_THRESHOLD_MIN <= f
                             <= _CELLPOSE_FLOW_THRESHOLD_MAX):
            return None, PlatformError(
                400, "invalid_request",
                "flow_threshold 需为 %g..%g 的数值"
                % (_CELLPOSE_FLOW_THRESHOLD_MIN, _CELLPOSE_FLOW_THRESHOLD_MAX))
        out["flow_threshold"] = f
    cellprob = args.get("cellprob_threshold")
    if cellprob is None:
        out["cellprob_threshold"] = _CELLPOSE_CELLPROB_THRESHOLD_DEFAULT
    else:
        f = _to_float(cellprob)
        if f is None or not (_CELLPOSE_CELLPROB_THRESHOLD_MIN <= f
                             <= _CELLPOSE_CELLPROB_THRESHOLD_MAX):
            return None, PlatformError(
                400, "invalid_request",
                "cellprob_threshold 需为 %g..%g 的数值"
                % (_CELLPOSE_CELLPROB_THRESHOLD_MIN,
                   _CELLPOSE_CELLPROB_THRESHOLD_MAX))
        out["cellprob_threshold"] = f
    return out, None


def _infer_cellpose(image_png_bytes, mpp, diameter_um=None,
                    flow_threshold=None, cellprob_threshold=None):
    """POST /v1/count（Cellpose 计数合同）；错误映射与 PSM 同款。

    ``mpp`` 是**推理输入图**的 µm/px（调用方按实际渲染缩放换算，R3）；缺失
    时序列化为 JSON ``null``（R4：真实服务对非空 MPP 要求 >0，发 0.0 会被
    400 拒绝；null → 服务按模型缺省尺度计数并附 warning，直径换算无物理
    意义——此时调用方不发 diameter_um）。阈值显式转发（含解析后的缺省值，
    与插件侧前置校验同域；服务端 _validate_request 仍按同域复核）。
    返回 (body_dict)；实例坐标为输出图像素（由调用方映射回 level-0）。
    """
    payload = {"image_b64": base64.b64encode(image_png_bytes).decode("ascii"),
               "mpp": mpp if (mpp is not None and mpp > 0) else None}
    if diameter_um is not None:
        payload["diameter_um"] = diameter_um
    if flow_threshold is not None:
        payload["flow_threshold"] = flow_threshold
    if cellprob_threshold is not None:
        payload["cellprob_threshold"] = cellprob_threshold
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
    """只读计数能力 count_cells（Cellpose 实例分割；方形取景合同 v1 §5）。

    与 segment_region 共用入口守卫/委托凭证/by-id 读路径；差异（2026-10-14
    方形取景重设计，取代公开 x/y/w/h 自由矩形输入）：
      - 模型参数只有档位与 Cellpose 参数（``_validate_count_args``）；位置
        **只**来自平台验签转发的 ``context.snapshot``（缺失 → 400
        snapshot_required，先 goto 再调用）；
      - 方形规划器（``_plan_count_square``）：以快照中心生成不越界 level-0
        正方形——边长 ≤ 快照短边 ∩ 切片短边 ∩ 平台 ROI 上限，已知 MPP 时再
        受档位处理边长与 8 Mi px 预算约束（理想式起步 + 真实取整物理逐级
        递减验证）；靠边平移整框、更小缩边、永不切片外补边；渲染方形输入
        （``mpp_render = max(原生, d/30)``，永不上采样超过原生）；
      - MPP 缺失：边长 ≤ 档位，原生像素输入、发 ``mpp: null``；直径无物理
        标定意义——不给服务传直径并附提示；只返回像素口径；
      - 读前完成几何/契约/资源准入；读后按实际 src/输出尺寸再核对**方形
        一致性**、MPP 与 Cellpose 处理边长/总像素（与预读镜像同一物理；
        违背几何合同的数据拒绝进入模型）；
      - 结果 ``counted_region``：实际 level-0 方形 bbox + 快照/切片身份与
        显示指纹 + 缩小披露（reduced/note）；另含实际输入/处理边长、档位、
        有效 Cellpose 参数、密度/面积（MPP 有效时）、≤20 示例实例；
        无 label map / base64。
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
    snap, serr = _snapshot_context(ctx["body"])
    if serr is not None:
        return _error(serr.status, serr.code, serr.message)

    timings = {}
    warnings = []
    try:
        t = time.time()
        jwt = _installation_jwt()
        info = _platform_slide_info(slide, jwt, slide_id=slide_id,
                                    delegated=delegated)
        mpp = _normalize_mpp(info.get("mpp"))  # level-0 原生 MPP（可能 None）
        resolution = args["resolution"]
        diameter_um = args["diameter_um"]
        # 方形规划（读前几何/资源准入）：快照中心的不越界正方形。
        plan, perr = _plan_count_square(snap["bbox"], info.get("width"),
                                        info.get("height"), mpp, diameter_um,
                                        resolution)
        if perr is not None:
            raise perr
        read_args = {"x": plan["x"], "y": plan["y"],
                     "w": plan["side"], "h": plan["side"]}
        img_bytes, region_payload = _platform_read_region(
            slide, read_args, plan["render_edge"], delegated, jwt,
            slide_id=slide_id)
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
        # 方形一致性硬闸（读后）：实际读到的 src 与输出都必须是正方形，且
        # 与计划一致（平台夹取不会触发——规划已保证界内；不一致说明底层
        # 违背几何合同，拒绝进入模型）。
        if src_w != src_h or out_w != out_h \
                or int(src_w) != plan["side"] or int(out_w) != plan["render_edge"]:
            raise PlatformError(
                400, "platform_contract",
                "region 返回几何与方形计划不一致（src %dx%d、输出 %dx%d，"
                "计划 level-0 %d 方形、渲染边 %d）——拒绝进入推理"
                % (int(src_w), int(src_h), int(out_w), int(out_h),
                   plan["side"], plan["render_edge"]))
        # R3：推理输入 MPP 从实际 src/输出尺寸算（不是请求目标值）
        scale_x, scale_y, infer_mpp = _inference_scale(
            mpp, src_w, src_h, out_w, out_h)

        # 资源准入硬闸（按实际返回几何 + 实际两轴均值推理 MPP；F2 延续）：
        # 按 cellpose 4.2.1.1 的真实缩放物理（逐轴 int(size×rescale) 截断）
        # 算模型处理尺寸，同时执行处理边长 ≤ 所选 resolution 与总像素预算。
        # 正常路径与预读镜像一致（预准入放行 ⇒ 此处必过），仅作防御。
        proc_dims = _cellpose_processing_dims(out_w, out_h, infer_mpp,
                                              diameter_um)
        proc_px_actual = (None if proc_dims is None
                          else proc_dims[0] * proc_dims[1])
        if proc_dims is None or max(proc_dims) > resolution \
                or proc_px_actual > _CELLPOSE_MAX_SCALED_PIXELS:
            max_side = _count_max_side(mpp, diameter_um, resolution)
            raise PlatformError(
                400, "resource_limit",
                "Cellpose 将处理约 %s 像素（实际输入 %dx%d px、推理分辨率 "
                "%s µm/px、直径 %.1f µm、处理尺寸 %s），超出 resolution=%d "
                "处理边长上限或 %d 像素预算；请改选更大的 resolution（%s）"
                % ("未知" if proc_px_actual is None else "%d" % proc_px_actual,
                   int(out_w), int(out_h),
                   ("%.4f" % infer_mpp) if infer_mpp else "未知",
                   diameter_um,
                   "未知" if proc_dims is None else "%dx%d" % proc_dims,
                   resolution, _CELLPOSE_MAX_SCALED_PIXELS,
                   "/".join(str(r) for r in _COUNT_RESOLUTIONS)),
                extra={"scaled_pixels": proc_px_actual,
                       "processing_dims": (None if proc_dims is None
                                           else list(proc_dims)),
                       "budget_pixels": _CELLPOSE_MAX_SCALED_PIXELS,
                       "resolution": resolution,
                       "diameter_um": diameter_um,
                       "max_square_level0_px": max_side})

        t = time.time()
        # MPP 缺失：直径换算无物理标定意义——不发直径（服务按缺省尺度计数）
        # 并给出提示；MPP 有效：完整转发冻结参数（与资源计划同一份）。
        infer_body = _infer_cellpose(
            img_bytes, infer_mpp,
            diameter_um=(diameter_um if mpp is not None else None),
            flow_threshold=args["flow_threshold"],
            cellprob_threshold=args["cellprob_threshold"])
        timings["infer_ms"] = int((time.time() - t) * 1000)
    except PlatformError as e:
        return _error(e.status, e.code, e.message, retryable=e.retryable,
                      extra=e.extra)

    mpp_x = mpp_y = None
    if mpp is not None and mpp > 0:
        mpp_x = mpp_y = float(mpp)
    else:
        warnings.append("切片 MPP 缺失或无效：不给密度与 µm²，仅像素口径；"
                        "目标直径 %.1f µm 无物理标定意义，已忽略（未参与"
                        "换算与放大）" % diameter_um)

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

    if mpp is not None:
        process_mpp = diameter_um / _CELLPOSE_DIAM_MEAN_PX
        if process_mpp > mpp + 1e-12:
            warnings.append(
                "切片原生分辨率（%.4f µm/px）细于 %.1f µm 直径的处理尺度"
                "（%.4f µm/px）：输入按处理尺度渲染（%d px），模型内部不再"
                "放大" % (mpp, diameter_um, process_mpp, plan["render_edge"]))
        else:
            rescale = _CELLPOSE_DIAM_MEAN_PX * mpp / diameter_um
            if rescale > 1.0 + 1e-9:
                warnings.append(
                    "切片原生分辨率（%.4f µm/px）粗于 %.1f µm 直径的处理尺度"
                    "（%.4f µm/px）：模型内部上采样 %.2f×，不增加信息；结果"
                    "以原生渲染为准" % (mpp, diameter_um, process_mpp,
                                       rescale))
    for w in (infer_body.get("warnings") or []):
        if isinstance(w, str) and w:
            warnings.append("inference: " + w)

    # 范围披露：方形小于快照（任一轴被缩）时如实说明只统计了这方区域，
    # 不能当作整片/整块用户标记的总数（设计 §Cellpose 参数与结果）。
    if plan["reduced"]:
        region_note = (
            "本次统计的是当前视场内的这个 %dx%d 方形区域（系统在快照内自动"
            "取框），不是整张切片或标注的总数；要统计其他位置请先 goto 再"
            "计数" % (plan["side"], plan["side"]))
        warnings.append("统计范围小于当前快照：结果只覆盖 level-0 方形 "
                        "(%d, %d, %d, %d)，不能当作整片总数"
                        % (plan["x"], plan["y"], plan["side"], plan["side"]))
    else:
        region_note = (
            "本次统计覆盖当前视场（系统自动取的 %dx%d level-0 方形），"
            "不是整张切片的总数" % (plan["side"], plan["side"]))

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
        "counted_region": {
            "bbox": {"x": plan["x"], "y": plan["y"],
                     "w": plan["side"], "h": plan["side"]},
            "snapshot_id": snap["snapshot_id"],
            "slide_revision": (region_payload.get("asset_revision")
                               or info.get("asset_revision")),
            "render_context_fingerprint": snap["render_context_fingerprint"],
            "reduced": plan["reduced"],
            "note": region_note,
        },
        "cellpose_params": {
            "resolution": resolution,
            "diameter_um": diameter_um,
            "flow_threshold": args["flow_threshold"],
            "cellprob_threshold": args["cellprob_threshold"],
        },
        "diameter_px_used": _to_float(infer_body.get("diameter_px_used")),
        "instance_count": instance_count,
        "instance_count_note": "Cellpose 实例分割计数（近似值，需人工核对）",
        "density": {
            "per_mm2": density,
            "roi_area_mm2": roi_mm2,
            "basis": ("实际成像方形 ROI（level-0 %dx%d）"
                      % (plan["side"], plan["side"])
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
        "rendered": {
            "src": region_payload.get("src"),
            "width": region_payload.get("width"),
            "height": region_payload.get("height"),
            "read_level": region_payload.get("read_level"),
            "upsampled": region_payload.get("upsampled"),
            "requested_long_edge": plan["render_edge"],
        },
        "processing": {
            "input_edge": int(out_w),
            "processing_dims": (None if proc_dims is None
                                else [int(proc_dims[0]), int(proc_dims[1])]),
            "processing_edge": (None if proc_dims is None
                                else int(max(proc_dims))),
            "inference_mpp": infer_mpp,
        },
        "mpp": {
            "x": mpp_x, "y": mpp_y,
            "source": ("slide_info.mpp（mpp_x；两轴同值）" if mpp_x else None),
        },
        "resolution": resolution,
        "inference_input": {
            "mpp": infer_mpp,
            "scale": {"x": scale_x, "y": scale_y},
            "mpp_render": (None if mpp is None
                           else max(mpp, diameter_um
                                    / _CELLPOSE_DIAM_MEAN_PX)),
            "note": "推理输入 MPP = 原生 MPP × 实际渲染缩放（实际 src/输出"
                    "尺寸，R3）；渲染尺度 = max(原生, diameter/30)（永不上"
                    "采样超过原生）；原生 MPP 仅用于结果映射",
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
