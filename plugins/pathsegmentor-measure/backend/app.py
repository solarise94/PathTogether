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
     实现）读取 ROI：目标 ~0.5 µm/px 或更细、输出长边 ≤1024（且 ≤ 平台
     ROI 像素上限——超限由平台 403 roi_over_cap 拒绝，本服务透传为工具级
     错误）；同时取 slide_info 的 MPP 做 µm² 换算；
  4. 调推理服务 ``POST /v1/segment``（busy / not_ready / 不可达 / 契约不符
     映射为明确的错误信封）；
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
  - ``PT_PSM_TARGET_MPP``：目标输出分辨率 µm/px（缺省 0.5）；
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
_CAPABILITY = "segment_region"

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
        "mpp": _to_float(body.get("mpp")),
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


def _validate_args(args):
    """dispatch arguments 校验（跨字段规则平台 schema 不覆盖，这里收口）。

    返回 (normalized, None) 或 (None, PlatformError 400)。
    """
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


def _target_long_edge(bbox_w, bbox_h, mpp):
    """选输出长边：~target µm/px 或更细，夹到 [1, min(_MAX_LONG_EDGE, 长边)]。

    MPP 未知时用上限（1024）并在调用方附 warning——分辨率不可判定。
    ``≤ 长边``钳制保证**永不上采样超过切片原生分辨率**（输出像素 ≤ level-0
    源像素 ⇒ 输出分辨率 ≥ 原生 mpp，原生即最细）。
    """
    cap = max(1, _MAX_LONG_EDGE)
    long_side = max(int(bbox_w), int(bbox_h))
    cap = min(cap, max(1, long_side))
    if not mpp or mpp <= 0 or _TARGET_MPP <= 0:
        return cap, True
    want = int(round(long_side * mpp / _TARGET_MPP))
    if want < 1:
        want = 1
    if want <= cap:
        return want, False
    return cap, True  # 受 1024 合同上限或原生分辨率钳制（变粗/不放大）


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
                   inference_url=_INFER_URL)


@app.post("/capabilities/segment_region")
def capability_segment_region():
    """只读测量能力 segment_region（manifest.provides 声明的服务端端点）。"""
    t0 = time.time()
    principal = _dispatch_principal()
    if not principal or principal.get("type") != "agent":
        return _error(401, "unauthorized",
                      "缺少或非法 X-Dispatch-Principal（只接受平台 dispatch 转发）")
    delegated = _delegated_token()
    if not delegated:
        return _error(
            401, "unauthorized",
            "缺少 X-Delegated-Read-Token（须由平台 dispatch 签发转发；"
            "直连调用不受理）")
    if not (_INSTALLATION_ID and _INSTALLATION_SECRET):
        return _error(503, "backend_not_configured",
                      "插件后端未配置平台安装凭证（PT_INSTALLATION_ID/"
                      "PT_INSTALLATION_SECRET）", retryable=True)
    body = request.get_json(silent=True) or {}
    slide = body.get("slide") or ""
    if not isinstance(slide, str) or not slide:
        return _error(400, "invalid_request", "body.slide 必填")
    # P2 双字段（合同 §6.3）：dispatch 转发的 slide_id（签名 claim 权威值）。
    # 给出 → 平台回调一律走 by-id 路由（id_bundle 行 legacy_filename=NULL，
    # 按名回调恒 404）；仅名（兼容形态）走 by-name。
    slide_id = body.get("slide_id")
    if slide_id is not None:
        if not isinstance(slide_id, str) or not slide_id.strip():
            return _error(400, "invalid_request", "body.slide_id 需为非空字符串")
        slide_id = slide_id.strip()
    args, aerr = _validate_args(body.get("arguments"))
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
        "capability": _CAPABILITY,
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


if __name__ == "__main__":
    port = int(os.environ.get("PT_PATHSEGMENTOR_MEASURE_PORT") or 8062)
    # use_reloader=False：容器内由 docker_entry.sh 托管，禁止 Flask debug
    # reloader 再 fork 一份（否则 PID 关系乱、端口冲突）。
    app.run(host="127.0.0.1", port=port, use_reloader=False, threaded=True)
