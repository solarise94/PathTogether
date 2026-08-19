# -*- coding: utf-8 -*-
"""Sample TMA Score 插件后端（插件能力层 P1 示例插件）。

演示插件能力层的服务端契约（docs §4.2 第 6/7 步）：
  - 平台 dispatch 端点会向 ``service.baseUrl`` 转发
    ``POST /capabilities/{name}``，body 为 ``{"slide": ..., "arguments": {...}}``；
  - 请求头 ``X-Dispatch-Principal`` 是**唯一**可信的主体信息（主体类型/id/
    session，由平台附加）；插件不得信任其余自定义头；
  - 2xx 返回 ``{"result": <json>}``（≤64KB，超限平台会截断并附 truncated）；
    4xx/5xx 返回 ``{"error": {code, message, retryable}}`` 统一信封。

运行（开发）::

    python3 plugins/sample-tma-score/backend/app.py   # 监听 127.0.0.1:8061

环境变量：
  - ``PT_TMA_SCORE_PORT``：监听端口（缺省 8061，须与 manifest 的
    ``service.baseUrl`` 一致）；
  - ``PT_PLATFORM_URL`` + ``PT_INSTALLATION_ID`` + ``PT_INSTALLATION_SECRET``：
    可选的平台回调配置——配置后用安装凭证换 scoped JWT 调
    ``/api/plugin/v1/slides/{slide}``（尺寸/mpp）与 ``/changes``（标注数），
    汇总真实数据；未配置时降级返回占位摘要（source="degraded"）。
"""
import json
import os

import requests
from flask import Flask, jsonify, request

app = Flask(__name__)

_PLATFORM_URL = os.environ.get("PT_PLATFORM_URL") or ""
_INSTALLATION_ID = os.environ.get("PT_INSTALLATION_ID") or ""
_INSTALLATION_SECRET = os.environ.get("PT_INSTALLATION_SECRET") or ""


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


def _error(status, code, message, retryable=False):
    """统一错误信封（docs §7.7；平台 dispatch 对 4xx 原样透传语义）。"""
    resp = jsonify(error={"code": code, "message": message,
                          "retryable": retryable})
    resp.status_code = status
    return resp


def _platform_token():
    """安装凭证 → 短期 scoped JWT（平台 /api/plugin/v1/auth/token）。"""
    r = requests.post(
        _PLATFORM_URL.rstrip("/") + "/api/plugin/v1/auth/token",
        json={"installation_id": _INSTALLATION_ID,
              "secret": _INSTALLATION_SECRET},
        timeout=5)
    r.raise_for_status()
    return r.json()["access_token"]


def _platform_slide_meta(slide):
    """平台回调取切片元数据与标注数；未配置回调或失败返回 None（降级）。"""
    if not (_PLATFORM_URL and _INSTALLATION_ID and _INSTALLATION_SECRET):
        return None
    try:
        headers = {"Authorization": "Bearer " + _platform_token()}
        info = requests.get(
            _PLATFORM_URL.rstrip("/") + "/api/plugin/v1/slides/" + slide,
            headers=headers, timeout=10).json()
        changes = requests.get(
            _PLATFORM_URL.rstrip("/") + "/api/plugin/v1/slides/%s/changes"
            % slide, headers=headers, timeout=10).json()
        annotation_count = sum(
            1 for c in (changes.get("changes") or [])
            if c.get("type") == "annotation" and not c.get("deleted"))
        return {
            "width": info.get("width"),
            "height": info.get("height"),
            "mpp": info.get("mpp"),
            "annotation_count": annotation_count,
        }
    except Exception:
        return None


@app.get("/healthz")
def healthz():
    """健康检查（manifest.service.health 指向这里）。"""
    return jsonify(ok=True, plugin="dev.sample.tma", version="0.1.0")


@app.post("/capabilities/slide_summary")
def capability_slide_summary():
    """只读能力 slide_summary（manifest.provides 声明的服务端端点）。"""
    principal = _dispatch_principal()
    if not principal or principal.get("type") != "agent":
        return _error(401, "unauthorized",
                      "缺少或非法 X-Dispatch-Principal（只接受平台 dispatch 转发）")
    body = request.get_json(silent=True) or {}
    slide = body.get("slide") or ""
    args = body.get("arguments") or {}
    include_mpp = bool(args.get("include_mpp", True))
    meta = _platform_slide_meta(slide)
    summary = {
        "slide": slide,
        "plugin": "dev.sample.tma",
        "capability": "slide_summary",
        "source": "platform" if meta else "degraded",
        "requested_by_session": principal.get("session_id") or "",
    }
    if meta:
        summary.update({
            "width": meta["width"],
            "height": meta["height"],
            "annotation_count": meta["annotation_count"],
        })
        if include_mpp:
            summary["mpp"] = meta["mpp"]
        else:
            summary["mpp"] = None
    else:
        # 未配置平台回调：占位摘要（真实插件应配置 PT_* 三元组取真实数据）
        summary.update({
            "width": None, "height": None, "mpp": None,
            "annotation_count": None,
            "note": "插件未配置平台回调（PT_PLATFORM_URL 等环境变量），"
                    "返回降级占位摘要",
        })
    return jsonify(result=summary)


if __name__ == "__main__":
    port = int(os.environ.get("PT_TMA_SCORE_PORT") or 8061)
    app.run(host="127.0.0.1", port=port)
