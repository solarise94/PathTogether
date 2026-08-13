#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""SVS 病理图像查看 Web 应用后端（Flask + OpenSlide + Deep Zoom）。

运行：.venv/bin/python app.py   监听 0.0.0.0:8000
"""

import base64
import hmac
import io
import json
import os
import secrets
import shutil
import threading
import time
import zipfile
from collections import OrderedDict
from datetime import timedelta
from pathlib import Path

from flask import (
    Flask,
    Response,
    abort,
    jsonify,
    redirect,
    render_template,
    request,
    send_file,
    send_from_directory,
    session,
)
from werkzeug.utils import secure_filename

from openslide import OpenSlide
from PIL import Image

import requests

import share_store
import slide_cache
import slide_io
import user_store

app = Flask(__name__)

# 上传目录：默认 ~/svs-viewer/uploads，可用环境变量 UPLOAD_DIR 覆盖（容器内挂载）
UPLOAD_DIR = Path(os.environ.get("UPLOAD_DIR") or (Path.home() / "svs-viewer" / "uploads"))
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

# --------------------------------------------------------------------------- #
# 图片派生图确定性规格常量（§6.3）
#
# 坐标刻度 overlay 的渲染版本号：overlay 像素变化时递增，使旧派生图缓存失效。
# _overlay_coord_ticks 的画法带进 region 响应的 encoder 字段，供 sidecar 校验。
# --------------------------------------------------------------------------- #
OVERLAY_VERSION = "v1"
# 派生图编码器标识：与 PIL.Image.LANCZOS resize + JPEG 编码耦合。Pillow 升级可能
# 改变输出字节，故把 PIL.__version__ 一并随响应返回，sidecar 可据此判断是否需要
# 重建缓存（Phase 1 仅记录，不强制失效；Phase 2 接入 content_sha256 修复流程）。
DERIVATIVE_ENCODER_ID = "pillow"
DERIVATIVE_RESIZE_ALGORITHM = "LANCZOS"
DERIVATIVE_JPEG_QUALITY = 85

# 支持的病理图像扩展名
SUPPORTED_EXTS = {
    "svs", "tif", "tiff", "ndpi", "mrxs", "vms", "vmu", "scn", "bif", "svslide",
}
# 归档扩展名：zip 上传后解压（用于 MRXS 等需要伴侣数据目录的格式）
ARCHIVE_EXTS = {"zip"}

# 分享服务基础 URL（外部用户访问入口，生产部署用 env 覆盖，如 https://slides.example.com:18767）
SHARE_BASE_URL = os.environ.get(
    "SHARE_BASE_URL", "http://localhost:38000"
).rstrip("/")

# --------------------------------------------------------------------------- #
# 管理员登录认证（外网门户，可选）
# --------------------------------------------------------------------------- #
# 默认：ADMIN_PASSWORD 非空才启用认证；未设置时与内网一致（免登录）。
# Demo/公网：REQUIRE_ADMIN_AUTH=1 时密码为空或仍为文档占位符则拒绝启动（fail-closed）。
# 与 docs/demo-deployment.md 中 admin.env 示例完全一致；复制未替换即拒绝启动。
ADMIN_PASSWORD_PLACEHOLDER_SENTINEL = "<REPLACE_WITH_STRONG_PASSWORD>"


def _is_placeholder_admin_password(password):
    """空串、文档精确 sentinel、或 <...> 占位符视为未配置真实密码。"""
    s = (password or "").strip()
    if not s:
        return True
    if s == ADMIN_PASSWORD_PLACEHOLDER_SENTINEL:
        return True
    return s.startswith("<") and s.endswith(">")


def _env_truthy(env, name):
    return (env.get(name) or "").strip().lower() in ("1", "true", "yes")


def _resolve_admin_auth(environ=None):
    """解析管理员认证。返回 (username, password, auth_enabled)。

    REQUIRE_ADMIN_AUTH 开启且密码为空/占位符时 SystemExit，避免公网免认证启动。
    """
    env = os.environ if environ is None else environ
    username = env.get("ADMIN_USERNAME") or "admin"
    password = env.get("ADMIN_PASSWORD") or ""
    if _env_truthy(env, "REQUIRE_ADMIN_AUTH") and _is_placeholder_admin_password(password):
        raise SystemExit(
            "REQUIRE_ADMIN_AUTH=1 but ADMIN_PASSWORD is empty or a placeholder; "
            "refusing to start"
        )
    return username, password, bool(password)


ADMIN_USERNAME, ADMIN_PASSWORD, _AUTH_BY_PASSWORD = _resolve_admin_auth()


def _bootstrap_owner(environ=None):
    """owner 引导与迁移（Stage 3a 身份基础）。

    ADMIN_PASSWORD 非空时：若 users.json 无 owner 角色用户，创建 owner 行
    （email 用 ADMIN_USERNAME 值——它可以是名字不要求邮箱格式，display_name 同值）；
    若已存在 owner 且 ADMIN_PASSWORD 与现存 hash 不匹配，更新该 owner 的
    password_hash（env 始终可重置 owner 密码，保住「改密码靠 env」运维习惯）。

    返回 owner 的 user_id；ADMIN_PASSWORD 为空时返回 None。
    """
    env = os.environ if environ is None else environ
    password = env.get("ADMIN_PASSWORD") or ""
    if not password:
        return None
    username = env.get("ADMIN_USERNAME") or "admin"
    try:
        owner = user_store.ensure_owner(username, password)
    except Exception:
        # 引导失败不阻断启动；auth 仍由密码开启，登录时会因无 owner 而无法验证
        return None
    return owner.get("user_id") if owner else None


# 数据归属懒迁移：注入首个 owner 的 user_id（share_store._ensure_owner_refs 据此
# 给现存 projects/slide_meta/rois 补 owner_user_id；本节点只落地字段，不做过滤）。
_OWNER_USER_ID = _bootstrap_owner()
if _OWNER_USER_ID:
    share_store.set_owner_user_id(_OWNER_USER_ID)

# AUTH_ENABLED 语义不变：有 ADMIN_PASSWORD 或存在任何 enabled 用户即开
# （存在 admin 用户账户时即使未设 ADMIN_PASSWORD 也保持认证，防止误开免登录）。
def _resolve_auth_enabled():
    if _AUTH_BY_PASSWORD:
        return True
    try:
        return user_store.has_enabled_users()
    except Exception:
        return False


AUTH_ENABLED = _resolve_auth_enabled()

# session 有效期 7 天
app.permanent_session_lifetime = timedelta(days=7)


def _data_dir_for_secret() -> Path:
    """复用 share_store 的数据目录（SHARE_DATA_DIR）存放持久化 secret 文件。

    保证 Flask secret key 重启不失效；share_store.py 已保证该目录存在。
    """
    return Path(
        os.environ.get("SHARE_DATA_DIR") or (Path.home() / "svs-viewer" / "share-data")
    )


def _load_or_create_secret_key() -> str:
    """优先用 SECRET_KEY env；否则在数据目录下持久化随机 secret（0600）。

    gunicorn 多 worker（-w N、不 preload）时各 worker 独立 import 本模块，
    若不加锁会在「文件不存在」窗口各自生成不同 secret，导致 session 跨 worker
    失效（反复跳登录）。故用 fcntl 排他锁包裹「检查+生成+写」，保证并发首次
    生成时只写一次、其余 worker 读到同一 key。
    """
    env_key = os.environ.get("SECRET_KEY")
    if env_key:
        return env_key
    data_dir = _data_dir_for_secret()
    data_dir.mkdir(parents=True, exist_ok=True)
    secret_file = data_dir / "flask_secret.key"

    def _read_or_create_locked():
        """持排他锁内：双检文件，不存在才生成写入。"""
        if secret_file.is_file():
            try:
                return secret_file.read_text(encoding="utf-8").strip()
            except OSError:
                pass
        key = secrets.token_hex(32)
        secret_file.write_text(key, encoding="utf-8")
        try:
            os.chmod(secret_file, 0o600)
        except OSError:
            pass
        return key

    try:
        import fcntl  # POSIX（Linux/macOS）；gunicorn 多 worker 跨进程互斥

        lock_file = data_dir / "flask_secret.lock"
        with open(lock_file, "a+") as lf:
            fcntl.flock(lf.fileno(), fcntl.LOCK_EX)
            try:
                return _read_or_create_locked()
            finally:
                fcntl.flock(lf.fileno(), fcntl.LOCK_UN)
    except (ImportError, OSError):
        # 极少数无 fcntl 的平台：退回无锁逻辑（单 worker 仍正确）
        return _read_or_create_locked()


def _resolve_session_cookie_secure(environ=None):
    """管理端 session cookie 的 Secure 标志。

    只认显式 ADMIN_SESSION_COOKIE_SECURE（1/true/yes）。TLS 常在 Caddy/nginx
    终止，Flask 本地没有证书文件，不能用 SHARE_TLS_* 推断访问协议。
    SSH 隧道 HTTP 保持关闭（缺省 false）。
    """
    env = os.environ if environ is None else environ
    return _env_truthy(env, "ADMIN_SESSION_COOKIE_SECURE")


app.secret_key = _load_or_create_secret_key()
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["SESSION_COOKIE_SECURE"] = _resolve_session_cookie_secure()


# --------------------------------------------------------------------------- #
# AI sidecar 共享 token（internal 回调端点鉴权，pi 迁移 Step 2）
# --------------------------------------------------------------------------- #
# sidecar（Node）进程与本进程内部回调用同一 token 互信：env AI_INTERNAL_TOKEN
# 优先；缺省则读/生成 SHARE_DATA_DIR/ai_internal.token（0600，32 字节 hex）。
# sidecar 进程用同一规则解析 token（env 优先，否则读同一文件）。
def _load_or_create_ai_internal_token() -> str:
    """优先 env AI_INTERNAL_TOKEN；否则在数据目录下持久化随机 token（0600）。

    与 secret key 同样用 fcntl 排他锁包裹「检查+生成+写」，保证多 worker
    首次生成时只写一次、其余 worker 读到同一 token。
    """
    env_tok = os.environ.get("AI_INTERNAL_TOKEN")
    if env_tok:
        return env_tok
    data_dir = _data_dir_for_secret()
    data_dir.mkdir(parents=True, exist_ok=True)
    tok_file = data_dir / "ai_internal.token"

    def _read_or_create_locked():
        if tok_file.is_file():
            try:
                return tok_file.read_text(encoding="utf-8").strip()
            except OSError:
                pass
        tok = secrets.token_hex(32)
        tok_file.write_text(tok, encoding="utf-8")
        try:
            os.chmod(tok_file, 0o600)
        except OSError:
            pass
        return tok

    try:
        import fcntl

        lock_file = data_dir / "ai_internal.lock"
        with open(lock_file, "a+") as lf:
            fcntl.flock(lf.fileno(), fcntl.LOCK_EX)
            try:
                return _read_or_create_locked()
            finally:
                fcntl.flock(lf.fileno(), fcntl.LOCK_UN)
    except (ImportError, OSError):
        return _read_or_create_locked()


AI_INTERNAL_TOKEN = _load_or_create_ai_internal_token()


def _require_internal():
    """internal 回调端点鉴权：校验 header X-AI-Internal-Token，失败 401。

    internal 端点不走管理员 session 鉴权（_require_auth 放行 /internal/ 前缀）。
    """
    tok = (request.headers.get("X-AI-Internal-Token") or "").strip()
    if not tok or not hmac.compare_digest(tok, AI_INTERNAL_TOKEN):
        return jsonify(error="invalid_internal_token"), 401
    return None


# 防爆破：内存 dict 按 IP 计数 {ip: {"fails": int, "locked_until": float}}
_auth_attempts: dict = {}
_AUTH_FAIL_LIMIT = 5
_AUTH_LOCK_SECONDS = 60


def _is_ip_locked(ip: str):
    """返回该 IP 是否处于锁定期（含到期清理）。"""
    rec = _auth_attempts.get(ip)
    if not rec:
        return False
    if rec.get("locked_until", 0) and time.time() < rec["locked_until"]:
        return True
    # 锁定已过期：清零
    if rec.get("locked_until", 0):
        _auth_attempts.pop(ip, None)
    return False


def _record_auth_fail(ip: str):
    """记录一次失败，达到阈值则锁定 60 秒。"""
    rec = _auth_attempts.setdefault(ip, {"fails": 0, "locked_until": 0.0})
    rec["fails"] += 1
    if rec["fails"] >= _AUTH_FAIL_LIMIT:
        rec["locked_until"] = time.time() + _AUTH_LOCK_SECONDS


def _clear_auth_fails(ip: str):
    """登录成功后清零该 IP 的失败计数。"""
    _auth_attempts.pop(ip, None)


@app.before_request
def _require_auth():
    """启用认证时拦截未登录请求。

    放行 /login、/static/ 下文件；其余请求检查 session：
    /api/ 开头返回 401 jsonify(error="auth_required")，页面 302 到 /login。
    """
    if not AUTH_ENABLED:
        return None
    # 已登录放行
    if session.get("auth_user"):
        return None
    path = request.path
    # 放行登录页与静态资源（含 HistoPilot 插件前端 bundle，与 /static/ 同属非敏感前端资源）
    if path == "/login" or path.startswith("/static/") or path.startswith("/plugins/histopilot/ui/"):
        return None
    # internal 回调端点由 _require_internal 单独鉴权（共享 token），不走管理员 session
    if path.startswith("/internal/"):
        return None
    if path.startswith("/api/"):
        return jsonify(error="auth_required"), 401
    # 页面：跳登录，带 next（防开放跳转在 login 路由内校验）
    return redirect("/login?next=" + path)

# Deep Zoom 参数（512 瓦片降低公网请求数，渐进式 q82 JPEG 降体积并支持模糊→清晰预览）
DZ_TILE_SIZE = 512
DZ_OVERLAP = 1
# JPEG 编码质量，可由环境变量 JPEG_QUALITY 覆盖（默认 82）
JPEG_QUALITY = int(os.environ.get("JPEG_QUALITY") or 82)
# 保留旧名（仅供历史代码引用，实际值与 DZ_* 一致）
TILE_SIZE = DZ_TILE_SIZE
OVERLAP = DZ_OVERLAP

# 切片句柄池与元数据缓存已抽到 slide_cache.py（app.py 与 share_server.py 共享，
# 各自进程独立的池与缓存）

# 瓦片内存缓存（LRU）：大切片瓦片生成是 CPU 密集操作（解压+编码），
# 缓存后平移/缩放往返时秒出，显著减少画面割裂。key=(name, level, x, y)，value=JPEG bytes
TILE_CACHE_MAX = int(os.environ.get("TILE_CACHE_MAX") or 3000)  # ~60KB/片 ≈ 180MB 上限
_tile_cache: "OrderedDict[tuple, bytes]" = OrderedDict()
_tile_cache_lock = threading.Lock()


def _tile_cache_get(key):
    """LRU 命中：取值并移到最新位。"""
    with _tile_cache_lock:
        data = _tile_cache.get(key)
        if data is not None:
            _tile_cache.move_to_end(key)
        return data


def _tile_cache_put(key, data):
    """LRU 写入，超上限淘汰最久未用。"""
    with _tile_cache_lock:
        _tile_cache[key] = data
        _tile_cache.move_to_end(key)
        while len(_tile_cache) > TILE_CACHE_MAX:
            _tile_cache.popitem(last=False)


def _tile_cache_purge(name):
    """切片删除时清掉其全部瓦片缓存。"""
    with _tile_cache_lock:
        stale = [k for k in _tile_cache if k[0] == name]
        for k in stale:
            _tile_cache.pop(k, None)


# --------------------------------------------------------------------------- #
# 辅助函数
# --------------------------------------------------------------------------- #
def _sanitize_name(name: str) -> str:
    """净化文件名：防路径穿越同时保留中文等 Unicode 字符。

    werkzeug 的 secure_filename 会剥离所有非 ASCII 字符（如中文），
    导致纯中文文件名（如"我的切片.svs"）变成仅剩扩展名"svs"。因此：
    - 含非 ASCII 字符时：手动剥离路径分隔符、冒号、控制字符、以及残留的
      点-点（.. 仍可能被解析为父目录引用），保留 Unicode；
    - 纯 ASCII 名：直接用 secure_filename（其路径穿越防护更完整）。
    """
    if not name or "\x00" in name:
        return ""

    has_non_ascii = any(ord(c) > 127 for c in name)

    if not has_non_ascii:
        return secure_filename(name)

    # 含 Unicode：手动清理，保留非 ASCII 字符
    cleaned_chars = []
    for ch in name:
        if ch in "/\\:" or ord(ch) < 32:
            continue
        cleaned_chars.append(ch)
    cleaned = "".join(cleaned_chars).strip().rstrip(".")
    # 防止残留的 ".." 序列被解析为目录跳转（Path() 在无分隔符时不会跳转，
    # 这里做二次保险）
    cleaned = cleaned.replace("..", "")
    return cleaned


def _safe_name(name: str) -> str:
    """校验 name 合法且对应文件存在于 UPLOAD_DIR，防路径穿越。"""
    safe = _sanitize_name(name)
    if not safe or safe != name:
        abort(400, jsonify(error="非法文件名"))
    path = UPLOAD_DIR / safe
    if not path.is_file():
        abort(404, jsonify(error="切片不存在"))
    return safe


def _get_slide(name: str):
    """从缓存获取（或创建）切片的句柄池 entry。

    打开是惰性的，真正的 slide_io.open_slide 在首次 borrow_pair 时发生；
    多路并发读取同一切片由句柄池（SLIDE_HANDLE_POOL）保证并行。
    """
    safe = _safe_name(name)
    return slide_cache.get_slide(safe, UPLOAD_DIR / safe)


def _close_slide(name: str) -> None:
    """关闭并移除缓存中的切片句柄池，同时清掉其瓦片缓存。"""
    slide_cache.evict(name)
    _tile_cache_purge(name)


def _to_float(v):
    """安全转 float。"""
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _mpp_from_tiff_resolution(path: Path):
    """从 TIFF 分辨率标签（真实坐标尺）读取 mpp。

    许多扫描仪/转换软件生成的 TIFF 类切片没有厂商 mpp 元数据，
    但写入了标准的 XResolution/YResolution + ResolutionUnit 标签：
    - ResolutionUnit=2（英寸）：mpp(µm/px) = 25400 / XResolution
    - ResolutionUnit=3（厘米）：mpp(µm/px) = 10000 / XResolution
    读取失败或数值不合理时返回 (None, None)。
    """
    try:
        from PIL import Image
        from PIL.TiffTags import TAGS_V2  # noqa: F401  确保标签表初始化

        with Image.open(str(path)) as img:
            tags = getattr(img, "tag_v2", None)
            if not tags:
                return None, None
            x_res = _to_float(tags.get(282))  # XResolution
            y_res = _to_float(tags.get(283))  # YResolution
            unit = tags.get(296, 2)           # ResolutionUnit，默认英寸
            if not x_res or x_res <= 0:
                return None, None
            factor = 25400.0 if unit == 2 else (10000.0 if unit == 3 else None)
            if factor is None:
                return None, None
            mpp_x = factor / x_res
            mpp_y = factor / y_res if y_res and y_res > 0 else mpp_x
            # 合理性检查：病理切片 mpp 一般在 0.1 ~ 2.0 µm/px 之间
            if 0.05 <= mpp_x <= 3.0:
                return mpp_x, mpp_y
    except Exception:
        pass
    return None, None


def _read_metadata(osr: OpenSlide, path: Path) -> dict:
    """读取尺寸与 mpp 等元数据。

    mpp 取值优先级（均为真实坐标尺，最后一个才是估算）：
    1. 厂商元数据 openslide.mpp-x/y（Aperio/滨松等，最可靠）
    2. TIFF 标准分辨率标签 XResolution + ResolutionUnit
    3. 按扫描倍率估算 mpp = 10 / objective-power（标记为 estimated）
    4. 缺失（missing）
    """
    width, height = osr.dimensions
    props = osr.properties
    objective_f = _to_float(props.get("openslide.objective-power"))

    mpp_x_f = _to_float(props.get("openslide.mpp-x"))
    mpp_y_f = _to_float(props.get("openslide.mpp-y"))

    if mpp_x_f is not None and mpp_y_f is not None:
        mpp_source = "metadata"
    else:
        # TIFF 分辨率标签兜底（真实坐标尺）
        tiff_mpp_x, tiff_mpp_y = _mpp_from_tiff_resolution(path)
        if tiff_mpp_x is not None:
            mpp_x_f = mpp_x_f if mpp_x_f is not None else tiff_mpp_x
            mpp_y_f = mpp_y_f if mpp_y_f is not None else tiff_mpp_y
            mpp_source = "tiff-resolution"
        elif objective_f is not None and objective_f > 0:
            # 估算：mpp = 10 / objective-power
            est = 10.0 / objective_f
            mpp_x_f = mpp_x_f if mpp_x_f is not None else est
            mpp_y_f = mpp_y_f if mpp_y_f is not None else est
            mpp_source = "estimated"
        else:
            mpp_x_f = None
            mpp_y_f = None
            mpp_source = "missing"

    return {
        "width": width,
        "height": height,
        "mpp_x": mpp_x_f,
        "mpp_y": mpp_y_f,
        "objective": objective_f,
        "mpp_source": mpp_source,
    }


def _slide_info_dict(name: str) -> dict:
    """构建单个切片的元数据字典（用于列表与 info 接口）。

    meta 部分（尺寸/mpp，需打开切片读取）走 mtime 感知缓存避免重复打开；
    alias/note（来自 slide_meta，可独立于文件修改）每次现查并合并。
    """
    safe = _safe_name(name)
    path = UPLOAD_DIR / safe
    base = {"name": safe, "size_bytes": path.stat().st_size}

    def _read_meta():
        entry = _get_slide(safe)
        with slide_cache.borrow_pair(entry) as pair:
            return _read_metadata(pair["osr"], path)

    try:
        meta = slide_cache.cached_read_metadata(safe, path, _read_meta)
    except Exception as e:
        base.update(
            {
                "width": None,
                "height": None,
                "mpp_x": None,
                "mpp_y": None,
                "objective": None,
                "mpp_source": "missing",
                "error": str(e),
            }
        )
        sm = share_store.get_slide_meta_full(safe)
        base["alias"] = sm.get("alias", "")
        base["note"] = sm.get("note", "")
        base["public"] = bool(sm.get("public"))
        return base
    base.update(meta)
    sm = share_store.get_slide_meta_full(safe)
    base["alias"] = sm.get("alias", "")
    base["note"] = sm.get("note", "")
    base["public"] = bool(sm.get("public"))
    return base


# --------------------------------------------------------------------------- #
# HistoPilot 插件 UI（Stage 2：同源独立 bundle）
# --------------------------------------------------------------------------- #
# 插件前端资源目录（仅服务 .js/.css，路径穿越交给 send_from_directory 拒绝）。
PLUGINS_UI_DIR = Path(__file__).resolve().parent / "plugins" / "histopilot" / "ui"
_PLUGIN_UI_ALLOWED_EXT = {".js", ".css"}


def histopilot_ui_enabled():
    """HistoPilot 插件 UI feature flag。默认开启；HISTOPILOT_UI_ENABLED=0 时关闭。

    在请求时读取（非 import 时），便于测试与运行时切换。关闭时 index 模板不渲染
    插件 <script> 与 ai-btn / 溢出项，平台前端对 window.HistoPilot 缺失静默降级。
    """
    return os.environ.get("HISTOPILOT_UI_ENABLED", "1") != "0"


# --------------------------------------------------------------------------- #
# 路由
# --------------------------------------------------------------------------- #
@app.route("/")
def index():
    return render_template("index.html", histopilot_ui_enabled=histopilot_ui_enabled())


@app.route("/plugins/histopilot/ui/<path:filename>")
def histopilot_ui_asset(filename):
    """HistoPilot 插件 UI 静态资源（仅 .js/.css）。

    feature flag 关闭时 404；非允许扩展名 403；路径穿越由 send_from_directory 拒绝。
    """
    if not histopilot_ui_enabled():
        abort(404)
    ext = os.path.splitext(filename)[1].lower()
    if ext not in _PLUGIN_UI_ALLOWED_EXT:
        abort(403)
    return send_from_directory(str(PLUGINS_UI_DIR), filename)


@app.route("/login", methods=["GET", "POST"])
def login():
    """登录页。GET 渲染；POST 校验并写 session。

    支持用邮箱或用户名（display_name）登录，密码经 werkzeug 哈希校验
    （user_store.verify_user）。连续失败 5 次锁定 60 秒。
    成功：session.permanent=True，session["auth_user"]=display_name、
    session["user_id"]、session["role"]；跳 next（校验必须以 / 开头且不以 // 开头，
    防开放跳转）或 "/"。
    """
    if not AUTH_ENABLED:
        # 未启用认证：直接回首页
        return redirect("/")

    next_url = request.args.get("next") or "/"

    if request.method == "POST":
        ip = request.remote_addr or ""
        # 锁定期内拒绝
        if _is_ip_locked(ip):
            return render_template("login.html", error="尝试过于频繁，请稍后再试", next_url=next_url), 429

        username = request.form.get("username", "")
        password = request.form.get("password", "")
        # POST 时 next 来自表单隐藏域（GET 渲染时已写入）
        post_next = request.form.get("next") or "/"
        user = user_store.verify_user(username, password)

        if user is not None:
            session.permanent = True
            session["auth_user"] = user.get("display_name") or user.get("email")
            session["user_id"] = user.get("user_id")
            session["role"] = user.get("role")
            _clear_auth_fails(ip)
            # 校验 next：必须以 / 开头且不以 // 开头，防开放跳转
            if not post_next.startswith("/") or post_next.startswith("//"):
                post_next = "/"
            return redirect(post_next)

        # 失败
        _record_auth_fail(ip)
        return render_template("login.html", error="用户名或密码错误", next_url=next_url), 401

    return render_template("login.html", error=None, next_url=next_url)


@app.route("/logout")
def logout():
    """登出：清 session，跳登录页。"""
    session.clear()
    return redirect("/login")


@app.route("/api/auth/info")
def api_auth_info():
    """返回认证状态与当前登录用户信息（含 role 与 user_id）。"""
    return jsonify(
        auth_enabled=AUTH_ENABLED,
        username=session.get("auth_user"),
        role=session.get("role"),
        user_id=session.get("user_id"),
    )


def _require_owner():
    """owner-only 守卫：当前 session 角色非 owner 返回 403 JSON。

    仅 owner（部署者 / superadmin）可管理用户。未登录或 user/guest 一律 403
    （资源级鉴权矩阵是下一节点的事，这里只做身份级 owner 判定）。
    """
    if session.get("role") == user_store.ROLE_OWNER:
        return None
    return jsonify(error="需要 owner 权限"), 403


# =========================================================================== #
# Stage 3a-2a：身份与资源级鉴权矩阵（docs §5.1.1）
#
# 关键不变量：AUTH_ENABLED=False（内网模式）或 session 无 role 时，current_identity
# 返回 role=owner —— 此时所有 can_* 放行、所有过滤返回全量，**完全不影响现状**
# （这是现有 88 个测试在 AUTH_ENABLED=False 下全绿的关键）。
#
# owner：一切。
# user：上传/维护自己的图库；查看 = 自己的 + 公开 + 受邀（认领过 active share）；
#       标注 = 自己的 + 协作切片；删除标注 = 仅本人创建；创建分享 = 仅自己的切片。
# guest：不能上传/维护图库；标注按分享权限走 /s/* 流程（share_server）；不能创建分享。
# =========================================================================== #
def current_identity():
    """返回 {"role","user_id"}。

    session 无 role（AUTH_ENABLED=False 内网模式 / 未登录）→ role=owner 全开。
    AUTH_ENABLED=True 时未登录请求已被 _require_auth 在 before_request 拦截为 401，
    不会走到资源级判定；此处对无 role 的分支保守放行，避免误锁。
    """
    role = session.get("role")
    if role is None:
        role = user_store.ROLE_OWNER
    return {"role": role, "user_id": session.get("user_id")}


def _is_owner():
    return current_identity()["role"] == user_store.ROLE_OWNER


def _current_uid():
    return current_identity()["user_id"]


def _slide_owner(name):
    """切片归属 owner_user_id（来自 slide_meta）；无记录返回 None。"""
    meta = share_store.get_slide_meta_full(name)
    return meta.get("owner_user_id") if meta else None


def _slide_is_public(name):
    meta = share_store.get_slide_meta_full(name)
    return bool(meta.get("public")) if meta else False


def _claimed_slides(uid):
    """user 认领的 active share 中的切片名集合（协作切片）。"""
    if not uid:
        return set()
    return share_store.claimed_active_slides_for_user(uid)


def can_upload():
    """owner/user 可上传；guest 不可。"""
    return current_identity()["role"] in (user_store.ROLE_OWNER, user_store.ROLE_USER)


def can_view_slide(name):
    """owner 全量；user = 自己的 ∪ 公开 ∪ 认领的协作切片。"""
    ident = current_identity()
    if ident["role"] == user_store.ROLE_OWNER:
        return True
    uid = ident["user_id"]
    if _slide_owner(name) == uid:
        return True
    if _slide_is_public(name):
        return True
    if name in _claimed_slides(uid):
        return True
    return False


def can_delete_slide(name):
    """owner 任意；user 仅自己的。"""
    ident = current_identity()
    if ident["role"] == user_store.ROLE_OWNER:
        return True
    return _slide_owner(name) == ident["user_id"]


def can_annotate_slide(name):
    """owner 全量；user = 自己的 ∪ 协作切片（不含纯公开只读）。"""
    ident = current_identity()
    if ident["role"] == user_store.ROLE_OWNER:
        return True
    uid = ident["user_id"]
    if _slide_owner(name) == uid:
        return True
    if name in _claimed_slides(uid):
        return True
    return False


def can_delete_annotation(roi):
    """owner 任意；否则仅本人创建（roi.owner_user_id == 自己）。"""
    ident = current_identity()
    if ident["role"] == user_store.ROLE_OWNER:
        return True
    return roi.get("owner_user_id") == ident["user_id"]


def can_manage_share(slides):
    """创建分享：owner 任意；user 仅当全部切片归自己。"""
    ident = current_identity()
    if ident["role"] == user_store.ROLE_OWNER:
        return True
    uid = ident["user_id"]
    return all(_slide_owner(s) == uid for s in slides)


def _visible_slide_names():
    """当前身份可见的切片文件名集合。owner=全量；user=可见集。"""
    ident = current_identity()
    all_names = {
        child.name for child in UPLOAD_DIR.iterdir()
        if child.is_file() and child.suffix.lower().lstrip(".") in SUPPORTED_EXTS
    }
    if ident["role"] == user_store.ROLE_OWNER:
        return all_names
    uid = ident["user_id"]
    meta_all = share_store.get_all_slide_meta_full()
    claimed = _claimed_slides(uid)
    visible = set()
    for name in all_names:
        m = meta_all.get(name, {})
        if m.get("owner_user_id") == uid or m.get("public") or name in claimed:
            visible.add(name)
    return visible


def _can_access_project(pid):
    """owner 任意；user 仅自己 owner_user_id 的项目（不存在视为无权）。"""
    if _is_owner():
        return True
    proj = share_store.get_project(pid)
    if proj is None:
        return False
    return proj.get("owner_user_id") == _current_uid()


def _denied(msg="无权访问"):
    """统一的资源级 403 响应（不区分 404/403 以免泄露存在性，简单优先）。"""
    return jsonify(error=msg), 403


# --------------------------------------------------------------------------- #
# owner 用户管理（Stage 3a 身份基础；注册默认关闭，docs §19-12）
# --------------------------------------------------------------------------- #
# 开放注册开关默认关闭（"0"）。本节点不做公开注册页——owner 手动添加用户。
REGISTRATION_OPEN = (os.environ.get("REGISTRATION_OPEN") or "0").strip().lower() in (
    "1", "true", "yes")


@app.route("/api/admin/users", methods=["GET"])
def api_admin_users_list():
    """列出全部用户（不含 hash）与开放注册开关。仅 owner。"""
    auth = _require_owner()
    if auth:
        return auth
    return jsonify(users=user_store.list_users(), registration_open=REGISTRATION_OPEN)


@app.route("/api/admin/users", methods=["POST"])
def api_admin_users_create():
    """创建 user 角色账户。仅 owner。JSON: {email, password, display_name?}。

    email 冲突 409；密码 <8 位 400。返回新用户（不含 hash）。初始密码由 owner
    线下告知用户（本节点不做邮件发送）。
    """
    auth = _require_owner()
    if auth:
        return auth
    body = request.get_json(silent=True) or {}
    email = body.get("email")
    password = body.get("password")
    display_name = body.get("display_name")
    if not isinstance(email, str) or not email.strip():
        return jsonify(error="缺少邮箱/用户名"), 400
    if not isinstance(password, str) or not password:
        return jsonify(error="缺少密码"), 400
    if len(password) < 8:
        return jsonify(error="密码长度至少 8 位"), 400
    try:
        user = user_store.create_user(
            email, password, role=user_store.ROLE_USER, display_name=display_name)
    except ValueError as e:
        msg = str(e)
        if "已存在" in msg:
            return jsonify(error=msg), 409
        return jsonify(error=msg), 400
    return jsonify(user)


@app.route("/api/admin/users/<user_id>/disable", methods=["POST"])
def api_admin_users_disable(user_id):
    """禁用用户。仅 owner。不能禁用最后一个 enabled owner（400）。"""
    auth = _require_owner()
    if auth:
        return auth
    target = user_store.get_user(user_id)
    if target is None:
        return jsonify(error="用户不存在"), 404
    # 保护：不能禁用最后一个 enabled owner
    if target.get("role") == user_store.ROLE_OWNER and not target.get("disabled"):
        if user_store.count_owners() <= 1:
            return jsonify(error="不能禁用最后一个启用中的 owner"), 400
    user = user_store.set_user_disabled(user_id, True)
    return jsonify(user)


@app.route("/api/admin/users/<user_id>/enable", methods=["POST"])
def api_admin_users_enable(user_id):
    """启用用户。仅 owner。"""
    auth = _require_owner()
    if auth:
        return auth
    user = user_store.set_user_disabled(user_id, False)
    if user is None:
        return jsonify(error="用户不存在"), 404
    return jsonify(user)


@app.route("/api/admin/users/<user_id>/password", methods=["POST"])
def api_admin_users_password(user_id):
    """重置用户密码。仅 owner。JSON: {password}。

    不能重置最后一个 enabled owner 的密码会致其失联——owner 密码由 env
    ADMIN_PASSWORD 兜底可重置，故仅校验密码长度。
    """
    auth = _require_owner()
    if auth:
        return auth
    body = request.get_json(silent=True) or {}
    new_password = body.get("password")
    if not isinstance(new_password, str) or not new_password:
        return jsonify(error="缺少密码"), 400
    if len(new_password) < 8:
        return jsonify(error="密码长度至少 8 位"), 400
    try:
        user = user_store.set_user_password(user_id, new_password)
    except ValueError as e:
        return jsonify(error=str(e)), 400
    if user is None:
        return jsonify(error="用户不存在"), 404
    return jsonify(user)


@app.route("/api/slides")
def api_slides():
    """列出所有切片的元数据（owner 全量；user 仅可见集，docs §5.1.1）。"""
    visible = _visible_slide_names()
    items = []
    for child in sorted(UPLOAD_DIR.iterdir()):
        if not child.is_file():
            continue
        if child.suffix.lower().lstrip(".") not in SUPPORTED_EXTS:
            continue
        if child.name not in visible:
            continue
        try:
            items.append(_slide_info_dict(child.name))
        except Exception as e:
            # 路径穿越校验等可能抛出 HTTP 异常，这里收集为 error
            sm = share_store.get_slide_meta(child.name)
            items.append(
                {
                    "name": child.name,
                    "size_bytes": child.stat().st_size,
                    "width": None,
                    "height": None,
                    "mpp_x": None,
                    "mpp_y": None,
                    "objective": None,
                    "mpp_source": "missing",
                    "alias": sm.get("alias", ""),
                    "note": sm.get("note", ""),
                    "error": str(getattr(e, "description", e)),
                }
            )
    return jsonify(items)


def _validate_slide_file(path: Path):
    """验证单个切片文件能否被 slide_io 打开（成功返回 True，否则 False）。"""
    try:
        osr = slide_io.open_slide(path)
    except Exception:
        return False
    try:
        osr.close()
    except Exception:
        pass
    return True


def _extract_zip_to_upload(src_zip: Path):
    """把 zip 解压到 UPLOAD_DIR，返回 (主文件名, [解压出的相对路径...])。

    流程：
    1. 解压到 UPLOAD_DIR 下临时目录 .extracting-<随机>；
    2. 防 zip-slip：拒绝绝对路径与含 .. 的 member，跳过 __MACOSX/隐藏文件；
    3. 若临时目录仅含一个子目录（无文件）则剥掉包装层当根；
    4. 把根下内容 move 到 UPLOAD_DIR；任何目标已存在 → 清理并返回 409 错误；
    5. 找出 SUPPORTED_EXTS 切片文件逐个验证；一个都打不开 → 清理并返回 400。

    失败时返回 (error_message, http_status)；成功返回 (main_name_or_None, moved_paths)。
    """
    tmp_dir = UPLOAD_DIR / (".extracting-" + secrets.token_hex(8))
    try:
        tmp_dir.mkdir(parents=True, exist_ok=False)
    except OSError as e:
        return f"创建临时目录失败: {e}", 400

    moved: list = []

    def _cleanup_all():
        # 清理临时目录与已 move 的文件/目录
        shutil.rmtree(tmp_dir, ignore_errors=True)
        for p in moved:
            try:
                p = UPLOAD_DIR / p
                if p.is_dir():
                    shutil.rmtree(p, ignore_errors=True)
                else:
                    p.unlink(missing_ok=True)
            except Exception:
                pass

    try:
        with zipfile.ZipFile(src_zip, "r") as zf:
            for info in zf.infolist():
                raw = info.filename
                if not raw:
                    continue
                # 规范化分隔符
                norm = raw.replace("\\", "/")
                # 跳过 macOS 元数据与隐藏文件
                parts = norm.split("/")
                if any(p == "__MACOSX" or p.startswith(".") for p in parts):
                    continue
                # 防 zip-slip：拒绝绝对路径与含 ..
                if norm.startswith("/") or any(p == ".." for p in parts):
                    _cleanup_all()
                    return "压缩包含非法路径", 400
                # member 路径各组件过 _sanitize_name
                clean_parts = [_sanitize_name(p) for p in parts]
                if any((not p and i < len(clean_parts) - 1) for i, p in enumerate(clean_parts)):
                    # 中间组件净化为空（非法字符）→ 跳过该 member
                    continue
                clean_parts = [p for p in clean_parts if p]
                if not clean_parts:
                    continue
                target = tmp_dir.joinpath(*clean_parts)
                # 二次校验目标在 tmp_dir 内
                try:
                    target.resolve().relative_to(tmp_dir.resolve())
                except ValueError:
                    _cleanup_all()
                    return "压缩包含非法路径", 400
                if info.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                else:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with zf.open(info) as src, open(target, "wb") as dst:
                        shutil.copyfileobj(src, dst)
    except zipfile.BadZipFile as e:
        _cleanup_all()
        return f"无效的 zip 文件: {e}", 400
    except Exception as e:
        _cleanup_all()
        return f"解压失败: {e}", 400

    # 若仅含一个子目录且无文件，剥掉包装层
    children = [p for p in tmp_dir.iterdir()] if tmp_dir.exists() else []
    files_in_root = [p for p in children if p.is_file()]
    dirs_in_root = [p for p in children if p.is_dir()]
    root = tmp_dir
    if not files_in_root and len(dirs_in_root) == 1:
        root = dirs_in_root[0]

    # 收集根下全部「文件」（不含目录，避免先移走父目录导致子文件找不到；
    # 目标父目录按需创建）
    entries = []
    for p in root.rglob("*"):
        if p.is_file():
            entries.append((p, p.relative_to(root)))

    # move 到 UPLOAD_DIR；任何目标已存在 → 409
    for abs_p, rel in entries:
        dest = UPLOAD_DIR / rel
        if dest.exists():
            _cleanup_all()
            return f"文件已存在: {rel.as_posix()}", 409

    for abs_p, rel in entries:
        dest = UPLOAD_DIR / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        try:
            shutil.move(str(abs_p), str(dest))
            moved.append(rel.as_posix())
        except Exception as e:
            _cleanup_all()
            return f"移动文件失败: {e}", 400

    # 找出其中支持的切片文件
    slide_files = []
    for rel in moved:
        ext = rel.rsplit(".", 1)[-1].lower() if "." in rel else ""
        if ext in SUPPORTED_EXTS:
            slide_files.append(rel)

    # 逐个验证能否打开
    valid = []
    for sf in slide_files:
        if _validate_slide_file(UPLOAD_DIR / sf):
            valid.append(sf)

    if not valid:
        _cleanup_all()
        return "压缩包内未找到可打开的有效切片文件", 400

    # 主文件优先 .mrxs，其次第一个
    main = next((v for v in valid if v.lower().endswith(".mrxs")), valid[0])
    # 清理临时目录（已 move 的留下）
    shutil.rmtree(tmp_dir, ignore_errors=True)
    return main, sorted(set(valid))


@app.route("/api/upload", methods=["POST"])
def api_upload():
    """流式上传切片文件，或上传 zip 解压（用于 MRXS 等伴侣数据目录格式）。

    Stage 3a-2a：owner/user 可上传（guest 在 AUTH 下无 session 已 401）；
    上传成功后为每个切片建立归属（slide_meta.owner_user_id = 上传者）。
    """
    if not can_upload():
        return jsonify(error="无上传权限"), 403
    ident = current_identity()
    if "file" not in request.files:
        return jsonify(error="缺少 file 字段"), 400

    file = request.files["file"]
    filename = file.filename or ""
    safe = _sanitize_name(filename)
    if not safe:
        return jsonify(error="非法文件名"), 400

    ext = safe.rsplit(".", 1)[-1].lower() if "." in safe else ""

    # zip 上传：解压分支
    if ext in ARCHIVE_EXTS:
        tmp_zip = UPLOAD_DIR / (".upload-" + secrets.token_hex(8) + ".zip")
        try:
            file.save(tmp_zip)
        except Exception as e:
            tmp_zip.unlink(missing_ok=True)
            return jsonify(error=f"保存失败: {e}"), 400
        result = _extract_zip_to_upload(tmp_zip)
        tmp_zip.unlink(missing_ok=True)
        # _extract_zip_to_upload 失败时返回 (error_msg, status)
        if isinstance(result, tuple) and len(result) == 2 and isinstance(result[1], int):
            msg, status = result
            return jsonify(error=msg), status
        main_name, extracted = result
        # 建立归属（zip 内全部有效切片均为上传者所有）
        for sname in extracted:
            try:
                share_store.set_slide_meta(sname, owner_user_id=ident["user_id"],
                                           requester_role=ident["role"])
            except PermissionError:
                return jsonify(error="无上传权限"), 403
        return jsonify(name=main_name, extracted=extracted)

    if ext not in SUPPORTED_EXTS:
        return jsonify(error="不支持的文件类型"), 400

    dest = UPLOAD_DIR / safe
    if dest.exists():
        return jsonify(error=f"文件已存在: {safe}"), 409

    # 流式保存
    try:
        file.save(dest)
    except Exception as e:
        try:
            dest.unlink(missing_ok=True)
        except Exception:
            pass
        return jsonify(error=f"保存失败: {e}"), 400

    # 验证能否打开（裸 .mrxs 通常缺少数据目录，给出针对性提示）
    if not _validate_slide_file(dest):
        try:
            dest.unlink(missing_ok=True)
        except Exception:
            pass
        hint = "MRXS 需连同数据目录打包为 zip 上传" if safe.lower().endswith(".mrxs") else "无效的切片文件"
        return jsonify(error=hint), 400

    # 建立归属（slide_meta.owner_user_id = 上传者；guest 已在 can_upload 拦截）
    try:
        share_store.set_slide_meta(safe, owner_user_id=ident["user_id"],
                                   requester_role=ident["role"])
    except PermissionError:
        return jsonify(error="无上传权限"), 403
    return jsonify(name=safe)


@app.route("/api/slide/<name>", methods=["DELETE"])
def api_slide_delete(name):
    """关闭句柄并删除切片。

    .mrxs 切片带有同名伴侣数据目录（去扩展名后的目录），一并删除。
    Stage 3a-2a：owner 任意；user 仅自己的切片。
    """
    if not can_delete_slide(name):
        return _denied()
    safe = _safe_name(name)
    _close_slide(safe)
    try:
        (UPLOAD_DIR / safe).unlink()
    except FileNotFoundError:
        pass
    # MRXS：删除伴侣数据目录（先做安全检查确保在 UPLOAD_DIR 内）
    if safe.lower().endswith(".mrxs"):
        stem = safe[: -len(".mrxs")]
        companion = UPLOAD_DIR / stem
        try:
            companion.resolve().relative_to(UPLOAD_DIR.resolve())
        except ValueError:
            pass
        else:
            if companion.is_dir():
                shutil.rmtree(companion, ignore_errors=True)
    return jsonify(ok=True)


@app.route("/api/slide/<name>/info")
def api_slide_info(name):
    """单个切片元数据。Stage 3a-2a：can_view_slide，无权 403。"""
    if not can_view_slide(name):
        return _denied()
    return jsonify(_slide_info_dict(name))


@app.route("/api/slide/<name>.dzi")
def api_slide_dzi(name):
    """手工生成 Deep Zoom XML。Stage 3a-2a：can_view_slide，无权 403。"""
    if not can_view_slide(name):
        return _denied()
    safe = _safe_name(name)
    entry = _get_slide(safe)
    with slide_cache.borrow_pair(entry) as pair:
        dz = pair["dz"]
        # DZI Size 取最高层（level_count-1）尺寸
        width, height = dz.level_dimensions[-1]

    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<Image xmlns="http://schemas.microsoft.com/deepzoom/2008" '
        f'Url="/api/slide/{safe}_files/" Format="jpeg" '
        f'Overlap="{DZ_OVERLAP}" TileSize="{DZ_TILE_SIZE}">'
        f'<Size Width="{width}" Height="{height}"/>'
        "</Image>"
    )
    resp = Response(xml, mimetype="application/xml")
    # DZI 元数据短期可变（重传/换切片后 URL 不变但尺寸会变），用短缓存
    resp.headers["Cache-Control"] = "max-age=60"
    return resp


@app.route("/api/slide/<name>_files/<int:level>/<int:x>_<int:y>.jpeg")
def api_slide_tile(name, level, x, y):
    """返回 Deep Zoom 单张瓦片 JPEG（512×512、baseline、q82，带 LRU 缓存）。

    Stage 3a-2a：can_view_slide，无权 403。
    """
    if not can_view_slide(name):
        return _denied()
    safe = _safe_name(name)

    key = (safe, level, x, y)
    cached = _tile_cache_get(key)
    if cached is not None:
        buf = io.BytesIO(cached)
    else:
        entry = _get_slide(safe)
        with slide_cache.borrow_pair(entry) as pair:
            dz = pair["dz"]
            tile = dz.get_tile(level, (x, y))

        # 含 alpha 通道时先转 RGB（JPEG 不支持透明度）
        if tile.mode != "RGB":
            tile = tile.convert("RGB")
        buf = io.BytesIO()
        # baseline JPEG：省掉 progressive/optimize 的编码开销（快 3–5×）；
        # 模糊→清晰的渐进预览已由切片页 base-thumb 底图层负责，瓦片无需 progressive
        tile.save(
            buf,
            format="JPEG",
            quality=JPEG_QUALITY,
        )
        _tile_cache_put(key, buf.getvalue())
        buf.seek(0)

    resp = send_file(buf, mimetype="image/jpeg")
    # 瓦片内容不变，长期不可变缓存
    resp.headers["Cache-Control"] = "public, max-age=31536000, immutable"
    return resp


@app.route("/api/slide/<name>/crop")
def api_slide_crop(name):
    """裁剪 level-0 原始像素区域的 PNG 图像并下载。Stage 3a-2a：can_view_slide。"""
    if not can_view_slide(name):
        return _denied()
    safe = _safe_name(name)
    entry = _get_slide(safe)

    def _parse_int(key):
        try:
            return int(request.args.get(key, ""))
        except (TypeError, ValueError):
            return None

    x = _parse_int("x")
    y = _parse_int("y")
    size = _parse_int("size")
    if x is None or y is None or size is None:
        return jsonify(error="x/y/size 参数需为整数"), 400
    if x < 0 or y < 0 or size <= 0 or size > 40000:
        return jsonify(error="参数越界（0<=x,y，0<size<=40000）"), 400

    with slide_cache.borrow_pair(entry) as pair:
        osr = pair["osr"]
        width, height = osr.dimensions
        # clamp 到图像边界
        x2 = min(x, max(0, width - 1))
        y2 = min(y, max(0, height - 1))
        max_w = max(0, width - x2)
        max_h = max(0, height - y2)
        size2 = min(size, max_w, max_h)
        if size2 <= 0:
            return jsonify(error="裁剪区域超出图像边界"), 400
        region = osr.read_region((x2, y2), 0, (size2, size2)).convert("RGB")

    buf = io.BytesIO()
    region.save(buf, format="PNG")
    buf.seek(0)

    stem = Path(safe).stem
    download_name = f"{stem}_{x2}_{y2}_{size2}px.png"
    return send_file(
        buf,
        mimetype="image/png",
        as_attachment=True,
        download_name=download_name,
    )


@app.route("/api/slide/<name>/thumbnail")
def api_slide_thumbnail(name):
    """返回缩略图 JPEG。Stage 3a-2a：can_view_slide，无权 403。"""
    if not can_view_slide(name):
        return _denied()
    safe = _safe_name(name)
    entry = _get_slide(safe)
    with slide_cache.borrow_pair(entry) as pair:
        osr = pair["osr"]
        thumb = osr.get_thumbnail((400, 400))
    if thumb.mode != "RGB":
        thumb = thumb.convert("RGB")
    buf = io.BytesIO()
    thumb.save(buf, format="JPEG", quality=90)
    buf.seek(0)
    return send_file(buf, mimetype="image/jpeg")


# --------------------------------------------------------------------------- #
# AI 读片助手相关 API（管理员，走 _require_auth）
# --------------------------------------------------------------------------- #
# AI sidecar 地址（pi 迁移 Step 5：Flask /api/ai/* 代理到此 Node sidecar）。
# 默认 http://127.0.0.1:8055，可用 env AI_SIDECAR_URL 覆盖。代理无状态，gunicorn
# 多 worker 各自转发，与单 sidecar 实例不冲突。
AI_SIDECAR_URL = (os.environ.get("AI_SIDECAR_URL") or "http://127.0.0.1:8055").rstrip("/")
# 普通（非 SSE）代理端点超时（秒）；SSE 长连接另用大超时（不限制读）。
_AI_SIDECAR_TIMEOUT = 30.0
_AI_SIDECAR_SSE_READ_TIMEOUT = 31536000.0  # 一年：等价于不限制 SSE 读

# AI 配置文件：与 flask_secret 同目录（SHARE_DATA_DIR），0600 权限
def _ai_config_path() -> Path:
    return _data_dir_for_secret() / "ai_config.json"


# api_key 加密：磁盘存 Fernet 密文（前缀 "enc:"），读取时解密为明文供调用方使用。
# 密钥单独持久化为 ai_secret.key（0600），与 flask_secret 同目录。明文旧配置
# 自动迁移：读取时检测到明文 api_key 会加密重写落盘。
try:
    from cryptography.fernet import Fernet  # type: ignore
    _HAS_FERNET = True
except Exception:  # pragma: no cover - cryptography 通常已装
    _HAS_FERNET = False


def _ai_secret_path() -> Path:
    return _data_dir_for_secret() / "ai_secret.key"


_FERNET_PREFIX = "enc:"


def _load_or_create_ai_secret() -> "Optional[object]":
    """加载/生成 AI api_key 加密用的 Fernet 密钥（0600），返回 Fernet 或 None。

    cryptography 不可用时返回 None（此时退化为明文存储，与旧行为一致，安全降级）。
    gunicorn 多 worker 下用 fcntl 锁保证并发首次生成只写一次（同 flask_secret）。
    """
    if not _HAS_FERNET:
        return None
    p = _ai_secret_path()
    data_dir = _data_dir_for_secret()
    data_dir.mkdir(parents=True, exist_ok=True)

    def _read_or_create_locked():
        if p.is_file():
            try:
                raw = p.read_bytes().strip()
                if raw:
                    return Fernet(raw)
            except Exception:
                pass  # 损坏 → 重新生成（旧密文将无法解密，调用方会提示重填 key）
        key = Fernet.generate_key()
        p.write_bytes(key)
        try:
            os.chmod(p, 0o600)
        except OSError:
            pass
        return Fernet(key)

    try:
        import fcntl
        lock_file = data_dir / "ai_secret.lock"
        with open(lock_file, "a+") as lf:
            fcntl.flock(lf.fileno(), fcntl.LOCK_EX)
            try:
                return _read_or_create_locked()
            finally:
                fcntl.flock(lf.fileno(), fcntl.LOCK_UN)
    except Exception:
        return _read_or_create_locked()


def _encrypt_api_key(plain: str):
    """加密明文 api_key 为 'enc:' 前缀的密文；Fernet 不可用时退化为明文。"""
    if not plain:
        return ""
    f = _load_or_create_ai_secret()
    if f is None:
        return plain  # 降级明文（cryptography 缺失）
    try:
        return _FERNET_PREFIX + f.encrypt(plain.encode("utf-8")).decode("ascii")
    except Exception:
        return plain  # 加密失败不阻断保存


def _decrypt_api_key(stored):
    """解密磁盘上的 api_key 值（'enc:' 前缀→解密；否则视为明文原样返回）。"""
    if not stored or not isinstance(stored, str):
        return ""
    if not stored.startswith(_FERNET_PREFIX):
        return stored  # 明文（旧配置 / 降级）
    f = _load_or_create_ai_secret()
    if f is None:
        return ""  # 密文但无法解密（密钥丢失/库缺失）
    try:
        return f.decrypt(stored[len(_FERNET_PREFIX):].encode("ascii")).decode("utf-8")
    except Exception:
        return ""  # 密钥不匹配/损坏 → 当作未配置，提示用户重填


def _mask_api_key(key: str) -> str:
    """api_key 掩码：前4 + **** + 后4；过短则全掩。"""
    if not key:
        return ""
    if len(key) <= 8:
        return "*" * len(key)
    return key[:4] + "****" + key[-4:]


# --------------------------------------------------------------------------- #
# AI 会话调优默认参数（内联自 ai_session.py；config 端点 + sidecar config 注入共用）
# --------------------------------------------------------------------------- #
# 默认参数（§8.1；ai_config.json 可覆盖）。base_url/api_key/model/max_tokens/
# api_protocol 是基础字段（不在 DEFAULT_CONFIG，分别由 ai_config.json 显式存），
# 调优参数集中在此。keep_recent_images 为 Step 4 加入的图片淘汰窗口（正整数）。
DEFAULT_CONFIG = {
    "max_steps": 50,
    # §9.2.1：窗口与视觉预算改由 window_tier（默认 balanced=400k/60000）推导；
    # None = 未显式设置，sidecar 按档位推导。显式覆盖仍优先。
    "context_window_tokens": None,
    "reserve_tokens": 16000,
    "safety_margin": 8192,
    "keep_recent_tokens": 20000,
    "keep_recent_images": 6,
    "fork_active_limit": 20,
    "lease_ttl": 150.0,
    "event_buffer": 200,
    "max_tokens": 2048,
    # ---- Phase 1 图片管线降本（§11） ----
    # 视觉工作集上限（不含稳定概览）；keep_recent_images 的后继字段。
    "visual_working_set_max": 4,
    # 每请求视觉 token 硬预算（含固定概览）；None = 按档位推导。
    "visual_context_budget_tokens": None,
    # 自适应分辨率分档（§6.1/§6.2 最长边 px）。
    # None = 未显式设置，由 sidecar 按 window_tier 预设（_WINDOW_TIER_PRESETS）
    # 推导；sidecar 对 null 走 numOr 兜底。显式配置过才透传非 None 值。
    "overview_long_edge": None,
    "working_image_long_edge": None,
    "detail_image_long_edge": None,
    # 确定性派生图编码参数（§6.3）。
    "image_jpeg_quality": 85,
    "image_overlay_version": "v1",
    # region 物化并发上限（替代 sidecar 硬编码 3）。
    "region_materialize_concurrency": 3,
    # 图片派生图 LRU（§6.4）：总字节上限与 TTL。
    "image_derivative_cache_max_mb": 64,
    "image_derivative_cache_ttl": 1800,
    # Prompt Cache 模式：off / auto / explicit（Phase 1 仅透传，Phase 3 启用）。
    "prompt_cache_mode": "auto",
    # Phase 4 §17 风险 2：稳定区概览开关（默认 True）。False 时 Phase 2b
    # assembler 组装稳定区不带概览图（稳定文本块仍保留）。
    "overview_enabled": True,
    # §9.2.1：窗口档位预设。默认 balanced（产品拍板 2026-08-13）。
    "window_tier": "balanced",
}


def _merge_config(cfg) -> dict:
    """把用户配置（ai_config.json 解密后的 dict）合并到 DEFAULT_CONFIG 之上。

    仅合并 DEFAULT_CONFIG 中已知键；None 值不覆盖默认。base_url/api_key/model/
    api_protocol 不在 DEFAULT_CONFIG，由调用方（如 config 组装）单独加。
    """
    out = dict(DEFAULT_CONFIG)
    if isinstance(cfg, dict):
        for k in DEFAULT_CONFIG:
            if k not in cfg:
                continue
            if k == "window_tier":
                # 特例：window_tier 只要键存在于 cfg（即使值是 None）就以 cfg 为准。
                # None = 手动模式（未启用档位），必须保留，否则会回退 DEFAULT_CONFIG
                # 的 "balanced"（Bug 3：用户显式清成 None 后合并被击穿）。
                out[k] = cfg[k]
            elif cfg[k] is not None:
                out[k] = cfg[k]
    return out


def _legacy_reserve_below_min(raw) -> bool:
    """旧版允许落盘的 reserve_tokens（0 / 1–127）在新下限下非法。"""
    if raw is None:
        return False
    try:
        n = int(raw)
    except (TypeError, ValueError):
        return False
    return n < _RESERVE_TOKENS_MIN


def _apply_legacy_reserve_migration(cfg, *, log=True) -> bool:
    """原地把 legacy reserve < 128 换成默认 16000。返回是否改写。

    旧版本允许 0 落盘；升级后 sidecar validateRunConfig 会拒绝 <128，
    导致已有部署无法起跑。加载与 sidecar 构建都必须迁移，不能只拦 PUT。
    """
    if not isinstance(cfg, dict) or not _legacy_reserve_below_min(cfg.get("reserve_tokens")):
        return False
    old = cfg.get("reserve_tokens")
    cfg["reserve_tokens"] = DEFAULT_CONFIG["reserve_tokens"]
    if log:
        app.logger.warning(
            "legacy reserve_tokens=%s (< %s) migrated to %s",
            old, _RESERVE_TOKENS_MIN, cfg["reserve_tokens"],
        )
    return True


def _build_sidecar_config() -> dict:
    """组装 sidecar 请求所需的 config 字段（base_url/api_key 明文 + 全部调优字段）。

    从 ai_config.json 读取（_load_ai_config 已解密 api_key 为明文），合并调优
    默认参数。返回的 dict 直接作为 sidecar body 的 `config` 字段。
    """
    cfg = _load_ai_config()
    out = _merge_config(cfg)
    out["base_url"] = cfg.get("base_url") or ""
    out["api_key"] = cfg.get("api_key") or ""  # 已解密为明文
    out["model"] = cfg.get("model") or ""
    # api_protocol 缺省 openai
    out["api_protocol"] = cfg.get("api_protocol") or "openai"
    # 运行时再守一次：即使加载迁移未持久化，注入 sidecar 的值也不能 <128。
    _apply_legacy_reserve_migration(out)
    return out


def _load_ai_config() -> dict:
    """读取 ai_config.json（0600）；不存在返回空 dict。

    api_key 在磁盘上为加密密文（enc: 前缀），这里解密为明文返回，供调用方（agent、
    校验）直接用。检测到明文 api_key（旧配置）时自动加密重写落盘（无缝迁移）。
    旧版 reserve_tokens < 128 同样在读取时迁移为默认 16000 并落盘，避免升级后
    sidecar run 边界拒绝。其他字段（base_url/model/max_tokens/调优参数/
    api_protocol）原样返回。
    """
    p = _ai_config_path()
    if not p.is_file():
        return {}
    try:
        with open(p, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return {}
    except Exception:
        return {}
    stored = data.get("api_key") or ""
    if stored and not stored.startswith(_FERNET_PREFIX) and _HAS_FERNET:
        # 明文旧配置 → 加密重写（迁移）。失败则保留明文，不阻断读取。
        enc = _encrypt_api_key(stored)
        if enc and enc != stored:
            data["api_key"] = enc
            try:
                _save_ai_config_raw(data)
            except Exception:
                pass
    # 旧版允许 reserve=0/1–127 落盘；升级后 sidecar 会拒绝 <128。
    # 在解密前改写并落盘，避免把明文 api_key 写回磁盘。
    if _apply_legacy_reserve_migration(data):
        try:
            _save_ai_config_raw(data)
        except Exception:
            pass
    data["api_key"] = _decrypt_api_key(stored)
    return data


def _save_ai_config_raw(cfg: dict) -> None:
    """写 ai_config.json（0600），不改动 api_key 字段（已按磁盘格式存好）。"""
    p = _ai_config_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    os.chmod(tmp, 0o600)
    os.replace(tmp, p)


def _save_ai_config(cfg: dict) -> None:
    """写 ai_config.json（0600）。

    cfg["api_key"] 应为明文（调用方约定的内存形态）：非空且非已加密格式时加密后
    落盘；已是 enc: 密文或空则原样写。api_key 不入日志。
    """
    out = dict(cfg)
    key = out.get("api_key")
    if isinstance(key, str) and key and not key.startswith(_FERNET_PREFIX):
        out["api_key"] = _encrypt_api_key(key)
    _save_ai_config_raw(out)


# --------------------------------------------------------------------------- #
# AI 会话调优参数权威校验（PUT /api/ai/config）
# --------------------------------------------------------------------------- #
# 语义依据（读自 sidecar/src + templates/index.html）：
#   context_window_tokens  compaction.ts:92 numOr 要求 n>0（否则回退 272000）；
#                          UI min=10000。→ 正整数。未显式设置时由
#                          _resolve_effective_context_window 按
#                          显式值 > window_tier 预设 > legacy 272k 推导。
#   reserve_tokens         pi 用 floor(0.8 * reserve) 作为压缩摘要 maxTokens。
#                          正整数且 ≥ _RESERVE_TOKENS_MIN（128）：这是产品规定
#                          的最小可用摘要预算，保证 floor(0.8×128)=102 tokens。
#                          缺省 16000。UI min=128。旧版落盘的 <128 在加载时迁
#                          移为 16000（见 _apply_legacy_reserve_migration）。
#   keep_recent_tokens     compaction 接受 >=0。0=不额外保留历史，但仍保留
#                          pi findCutPoint 要求的最小当前回合。缺省 20000。
#                          UI min=0。→ 非负整数（0 允许）。
#   keep_recent_images     transform-context.ts:53-55（>0 才用，否则默认 6）。
#                          无 UI 输入。→ 非负整数（0 允许）。
#   safety_margin          agent-runner.ts:90-91 legacy 字段，sidecar 不再使用；
#                          UI min=0。→ 非负整数（0 允许）。
#   event_buffer           session-store.ts:304 ?? DEFAULT_EVENT_BUFFER(200)；
#                          滚动事件窗口大小，0 会破坏窗口。→ 正整数。
#   fork_active_limit      agent-runner.ts:270/353 Math.max(0,...)；
#                          enforceForkLimit 1402 `if limit<=0 return`（0=不限速）。
#                          任务明确要求拒绝 <=0（-4 即非法）。→ 正整数。
#   lease_ttl              sidecar 不再用（仅 legacy ai_session.py）；UI min=30。
#                          → 正整数。
#   max_tokens             模型输出上限（基础字段，无 UI 输入）。→ 正整数。
#   max_steps              agent-runner.ts:604 Math.max(1,...)；UI min=1 max=500。
#                          → 正整数，且 <= 500（UI 声明上限；防止失控调用/费用）。
# 字段关系：reserve_tokens + keep_recent_tokens 必须 < context_window_tokens
# （压缩：context - reserve 是触发线，keep_recent 是保留尾；重叠即配置矛盾）。
# 允许 0 的字段（keep_recent_tokens / keep_recent_images / safety_margin）：
# keep_recent_tokens=0 表示不额外保留历史，但仍保留算法要求的最小当前回合；
# 图片/safety_margin 的 0 仍为禁用或不生效。reserve_tokens 不允许 0。
# 负数一律拒绝。
# 注意：所有校验失败返回中文明示 error 字符串；调用方负责在落盘前整体校验，
# 任一字段失败都不应部分写入 cfg。
_AI_TUNING_POSITIVE_INT = (
    "context_window_tokens",
    "event_buffer",
    "fork_active_limit",
    "lease_ttl",
    "max_tokens",
    "max_steps",
    "reserve_tokens",
    # Phase 1 新增正整数字段（§11）
    "visual_working_set_max",
    "visual_context_budget_tokens",
    "overview_long_edge",
    "working_image_long_edge",
    "detail_image_long_edge",
    "image_jpeg_quality",
    "region_materialize_concurrency",
    "image_derivative_cache_max_mb",
    "image_derivative_cache_ttl",
)
# 允许 0（非负整数）的字段
_AI_TUNING_NONNEG_INT = (
    "keep_recent_tokens",
    "keep_recent_images",
    "safety_margin",
)
# 布尔字段（Phase 4 §17 风险 2：overview_enabled 产品开关，默认 True）
# 注意：Python 中 isinstance(True, int) 为真，必须先排除 bool 再走整数校验，
# 因此布尔字段单独一类、在整数校验之前处理。
_AI_TUNING_BOOL = (
    "overview_enabled",
)
# 字符串枚举字段（Phase 1 + §9.2.1 window_tier）：一个 dict 统管，避免二次赋值
# 覆盖（曾导致 prompt_cache_mode / image_overlay_version 的 Flask 权威校验失效）。
# allowed 为 tuple → 值必须在其中；allowed 为 None → 任意非空字符串合法。
_AI_TUNING_ENUM = {
    "prompt_cache_mode": ("off", "auto", "explicit"),
    "image_overlay_version": None,  # 任意非空字符串（版本号自由格式）
    "window_tier": ("saving", "balanced", "performance"),
}
# §9.2.1 档位预设（与 sidecar WINDOW_TIER_PRESETS 保持一致）。window_tier
# 选定后，未显式提交的 context_window_tokens / 视觉预算 / 图片档按此推导。
_WINDOW_TIER_PRESETS = {
    "saving": {"context_window_tokens": 200000, "visual_context_budget_tokens": 20000,
               "overview_long_edge": 768, "detail_image_long_edge": 1024, "working_image_long_edge": 640},
    "balanced": {"context_window_tokens": 400000, "visual_context_budget_tokens": 60000,
                 "overview_long_edge": 1024, "detail_image_long_edge": 1280, "working_image_long_edge": 768},
    "performance": {"context_window_tokens": 500000, "visual_context_budget_tokens": 100000,
                    "overview_long_edge": 1024, "detail_image_long_edge": 1536, "working_image_long_edge": 1024},
}
# compaction.ts / pi-model.ts 在窗口未显式设置且无有效档位时的兼容默认。
_LEGACY_CONTEXT_WINDOW_TOKENS = 272000


def _resolve_effective_context_window(cfg):
    """effective context window：显式 context_window_tokens > window_tier 预设 > legacy 272k。

    与 sidecar ``resolveEffectiveContextWindow`` 同一语义。手动模式（tier 无效/
    已清除且未填窗口）不再跳过关系校验，而是按 272k 校验，避免 reserve+keep
    超过运行时实际窗口仍被两端接受。
    """
    if not isinstance(cfg, dict):
        return _LEGACY_CONTEXT_WINDOW_TOKENS
    raw = cfg.get("context_window_tokens")
    if raw is not None:
        try:
            n = int(raw)
            if n > 0:
                return n
        except (TypeError, ValueError):
            pass
    tier = cfg.get("window_tier")
    if tier in _WINDOW_TIER_PRESETS:
        return _WINDOW_TIER_PRESETS[tier]["context_window_tokens"]
    return _LEGACY_CONTEXT_WINDOW_TOKENS
# max_steps 上限：取自 templates/index.html 的 input max="500"（步数上限字段）。
# 防止 max_steps=99999 之类失控（sidecar 运行循环只限下限，费用风险）。
_MAX_STEPS_LIMIT = 500
# reserve_tokens 下限：产品规定的最小可用摘要预算。pi 用 floor(0.8 * reserve)
# 作摘要 maxTokens；128 保证 ≥102 输出 tokens（127 仍有 101，但不是产品下限）。
# 与 sidecar RESERVE_TOKENS_MIN 保持一致。
_RESERVE_TOKENS_MIN = 128
_RESERVE_TOKENS_SUMMARY_MAXTOKENS_MIN = (_RESERVE_TOKENS_MIN * 4) // 5  # 102
# 分辨率分档最长边上限（§6.1，最长边 ≤ 4096 与 region 端点一致）。
_LONG_EDGE_LIMIT = 4096


def _coerce_tuning_int(raw, field):
    """把请求值转换为整数；失败返回 (None, 错误文案)。

    接受 JSON number / 数字串；拒绝 None / 布尔 / 浮点小数（非整数）。
    """
    if isinstance(raw, bool) or raw is None:
        return None, "{} 需为整数".format(field)
    if isinstance(raw, float):
        if not raw.is_integer():
            return None, "{} 需为整数".format(field)
        return int(raw), None
    if isinstance(raw, int):
        return raw, None
    # 字符串：允许数字串，如 "16000"
    try:
        f = float(raw)
    except (TypeError, ValueError):
        return None, "{} 需为整数".format(field)
    if not f.is_integer():
        return None, "{} 需为整数".format(field)
    return int(f), None


def _validate_ai_tuning(body, cfg):
    """权威校验调优参数（PUT /api/ai/config 的数字字段）。

    参数：
        body: 请求 JSON（原始、未转换）。
        cfg:  当前已落盘配置（_load_ai_config 结果）。用于字段关系校验时取
              "本次未提交"字段的既有值（保持向后兼容：未提交字段维持原值）。
    返回：
        (validated, None)  —— validated: {field: int} 本批次校验通过的值；
        (None, err_msg)    —— 校验失败，err_msg 为中文错误说明。

    校验顺序：单字段全部先过（任一失败立即返回），再做关系校验。所有失败均
    在落盘前发生，保证不会产生部分写入。
    """
    validated = {}
    # 1) 单字段校验
    # 1a) 布尔字段（Phase 4 §17 风险 2）：必须在整数校验之前处理，
    #     否则 isinstance(True, int) 会把 True 当成 1 通过。
    for field in _AI_TUNING_BOOL:
        if field not in body:
            continue
        raw = body[field]
        # 接受真正的 bool；拒绝 0/1/"true" 等隐式转换，保持权威层严格。
        if not isinstance(raw, bool):
            return None, "{} 需为布尔值（true/false）".format(field)
        validated[field] = raw
    # 1b) 正整数字段
    for field in _AI_TUNING_POSITIVE_INT:
        if field not in body:
            continue
        iv, err = _coerce_tuning_int(body[field], field)
        if err:
            return None, err
        if iv <= 0:
            return None, "{} 需为正整数（> 0）".format(field)
        validated[field] = iv
    for field in _AI_TUNING_NONNEG_INT:
        if field not in body:
            continue
        iv, err = _coerce_tuning_int(body[field], field)
        if err:
            return None, err
        if iv < 0:
            return None, "{} 不可为负数（>= 0）".format(field)
        validated[field] = iv
    # 1c) 字符串枚举字段（prompt_cache_mode / image_overlay_version / window_tier）
    # 单循环统管三类字符串字段。语义：
    #   - 字段不在 body → 跳过；
    #   - raw is None → validated[field]=None（显式清除 → 回退默认/手动模式）；
    #   - 非字符串 → 中文报错；
    #   - allowed 是 tuple → 用 raw 原值比对（不 strip，保持 window_tier 对带空白
    #     值如 "saving " 拒绝的原有严格语义），不在其中 → 中文报错；
    #   - allowed 为 None（image_overlay_version）→ 保留第 3 节语义：strip 后非空
    #     即合法（任意非空字符串）。
    for field, allowed in _AI_TUNING_ENUM.items():
        if field not in body:
            continue
        raw = body[field]
        if raw is None:
            validated[field] = None  # 显式清除 → 回退默认/手动模式
            continue
        if not isinstance(raw, str):
            return None, "{} 需为字符串".format(field)
        if allowed is not None:
            if raw not in allowed:
                return None, "{} 仅支持 {}".format(field, "/".join(allowed))
            validated[field] = raw
        else:
            val = raw.strip()
            if not val:
                return None, "{} 不可为空".format(field)
            validated[field] = val
    # 4) 分辨率分档上限（§6.1，最长边 ≤ 4096）
    for field in ("overview_long_edge", "working_image_long_edge", "detail_image_long_edge"):
        if field in validated and validated[field] > _LONG_EDGE_LIMIT:
            return None, "{} 不可超过 {}（最长边上限）".format(field, _LONG_EDGE_LIMIT)
    # max_steps 上限（UI 声明 max=500）
    if "max_steps" in validated and validated["max_steps"] > _MAX_STEPS_LIMIT:
        return None, "max_steps 不可超过 {}（步数上限）".format(_MAX_STEPS_LIMIT)
    if "reserve_tokens" in validated and validated["reserve_tokens"] < _RESERVE_TOKENS_MIN:
        return None, (
            "reserve_tokens 不可低于 {}（最小可用摘要预算，保证约 {} 输出 tokens）".format(
                _RESERVE_TOKENS_MIN, _RESERVE_TOKENS_SUMMARY_MAXTOKENS_MIN
            )
        )
    # 2) 字段关系校验：reserve + keep < effective context window
    #    （显式 context_window_tokens > window_tier 预设 > legacy 272k）。
    #    未提交字段取已落盘 cfg 或缺省默认。始终对合并后的完整候选校验，
    #    这样只改 window_tier 也会按新窗口拒绝与 reserve/keep 冲突的配置。
    #    不能 early-return：后面的弃用字段映射仍须执行。
    rel_keys = ("reserve_tokens", "keep_recent_tokens", "context_window_tokens")
    merged = dict(DEFAULT_CONFIG)
    for k in rel_keys:
        cur = cfg.get(k)
        if cur is not None:
            try:
                merged[k] = int(cur)
            except (TypeError, ValueError):
                merged[k] = DEFAULT_CONFIG[k]
        if k in validated:
            merged[k] = validated[k]
    if "window_tier" in cfg:
        merged["window_tier"] = cfg.get("window_tier")
    if "window_tier" in validated:
        merged["window_tier"] = validated["window_tier"]
    reserve = merged["reserve_tokens"]
    keep = merged["keep_recent_tokens"]
    ctx = _resolve_effective_context_window(merged)
    if reserve + keep >= ctx:
        return None, (
            "reserve_tokens + keep_recent_tokens（{}）必须小于 "
            "context_window_tokens（{}）".format(reserve + keep, ctx)
        )
    # 3) 弃用字段映射：keep_recent_images → visual_working_set_max（§11）。
    #    两者同时存在时以新字段为准，并记一次弃用告警；仅旧字段存在时映射过去。
    #    safety_margin 同属弃用字段：接受、不展示、不写回（见 api_ai_config PUT）。
    if "keep_recent_images" in validated and "visual_working_set_max" not in validated:
        validated["visual_working_set_max"] = validated["keep_recent_images"]
    if "keep_recent_images" in validated and "visual_working_set_max" in body:
        app.logger.warning(
            "keep_recent_images 已弃用，与 visual_working_set_max 同时存在时以新字段为准"
        )
    return validated, None


@app.route("/api/slide/<name>/region", methods=["GET"])
def api_slide_region(name):
    """裁剪 level-0 区域为 JPEG base64（非附件下载，供 AI/前端按需取图）。

    参数：x,y,w,h（level-0 整数，必填，x,y>=0，w,h>0）；
         out_w,out_h 可选（默认保持宽高比、最长边 1568，上限各 4096）。
    返回 JSON：{image_base64, mime, width, height, src:{x,y,w,h}, magnification}。
    src 是 clamp 到边界后的实际区域。
    Stage 3a-2a：can_view_slide，无权 403（不泄露存在性差异，统一 403）。
    """
    if not can_view_slide(name):
        return _denied()
    safe = _safe_name(name)
    entry = _get_slide(safe)

    def _parse_int(key):
        try:
            return int(request.args.get(key, ""))
        except (TypeError, ValueError):
            return None

    x = _parse_int("x")
    y = _parse_int("y")
    w = _parse_int("w")
    h = _parse_int("h")
    if x is None or y is None or w is None or h is None:
        return jsonify(error="x/y/w/h 参数需为整数"), 400
    if x < 0 or y < 0 or w <= 0 or h <= 0:
        return jsonify(error="参数越界（0<=x,y，0<w,h）"), 400

    out_w = _parse_int("out_w")
    out_h = _parse_int("out_h")

    with slide_cache.borrow_pair(entry) as pair:
        osr = pair["osr"]
        width, height = osr.dimensions
        # clamp 到图像边界
        x2 = min(x, max(0, width - 1))
        y2 = min(y, max(0, height - 1))
        max_w = max(0, width - x2)
        max_h = max(0, height - y2)
        w2 = min(w, max_w)
        h2 = min(h, max_h)
        if w2 <= 0 or h2 <= 0:
            return jsonify(error="裁剪区域超出图像边界"), 400
        # 选最佳金字塔层（按 downsample）以加速 read_region。
        # read_region 的 location 是 level-0 坐标，但 size 是该层像素尺寸，
        # 故需把 level-0 尺寸 (w2,h2) 除以该层 downsample 得层内尺寸。
        ds = max(w2, h2) / 1568.0 if max(w2, h2) > 1568 else 1.0
        try:
            lvl = osr.get_best_level_for_downsample(ds) if ds > 1 else 0
        except Exception:
            lvl = 0
        try:
            ds_lvl = float(osr.level_downsamples[lvl]) if lvl < len(osr.level_downsamples) else 1.0
        except Exception:
            ds_lvl = 1.0
        rw = max(1, int(round(w2 / ds_lvl)))
        rh = max(1, int(round(h2 / ds_lvl)))
        region = osr.read_region((x2, y2), lvl, (rw, rh))
        if region.mode != "RGB":
            region = region.convert("RGB")

        # 计算输出尺寸：默认保持宽高比、最长边 1568
        if out_w and out_w > 0 and out_h and out_h > 0:
            ow = min(out_w, 4096)
            oh = min(out_h, 4096)
        else:
            longest = max(w2, h2)
            if longest <= 1568:
                ow, oh = w2, h2
            else:
                scale = 1568.0 / longest
                ow = max(1, int(round(w2 * scale)))
                oh = max(1, int(round(h2 * scale)))
        if (ow, oh) != (w2, h2):
            region = region.resize((ow, oh), Image.LANCZOS)

        # 读取 mpp 算放大倍率（供前端展示）
        meta = _read_metadata(osr, UPLOAD_DIR / safe)
        mpp = meta.get("mpp_x")
        mag = None
        if mpp and mpp > 0:
            try:
                level_ds = osr.level_downsamples
                ds_lvl = float(level_ds[lvl]) if lvl < len(level_ds) else 1.0
            except Exception:
                ds_lvl = 1.0
            base = 10.0 / mpp
            mag = base / ds_lvl if ds_lvl > 0 else base

    buf = io.BytesIO()
    region.save(buf, format="JPEG", quality=85)
    img_b64 = base64.b64encode(buf.getvalue()).decode("ascii")
    return jsonify({
        "image_base64": img_b64,
        "mime": "image/jpeg",
        "width": ow,
        "height": oh,
        "src": {"x": x2, "y": y2, "w": w2, "h": h2},
        "magnification": mag,
    })


@app.route("/api/ai/config", methods=["GET", "PUT"])
def api_ai_config():
    """读写 AI 配置（base_url/api_key/model/max_tokens/api_protocol + 会话调优参数）。

    GET：api_key 脱敏为 api_key_set:bool + 掩码（前4后4），不回显明文。
    PUT：空串=清除；与掩码同值=不变；其他=覆盖（明文进 → 加密落盘）。
    api_key 加密存盘（Fernet），旧明文配置自动迁移。api_key 不入日志。
    会话调优参数（§8.1）进 ai_config.json，缺省用文档默认。api_protocol 为
    "openai"|"anthropic"|"gemini"（默认 openai）：anthropic 直连暂未支持，
    选了会在起跑时给出明确报错；gemini 走 pi google-generative-ai provider
    （CPA 网关的 /v1beta gemini 兼容端点）。
    """
    if request.method == "GET":
        cfg = _load_ai_config()
        key = cfg.get("api_key") or ""
        out = {
            "base_url": cfg.get("base_url") or "",
            "api_key_set": bool(key),
            "api_key_mask": _mask_api_key(key),
            "model": cfg.get("model") or "",
            "max_tokens": cfg.get("max_tokens") or 2048,
            "api_protocol": cfg.get("api_protocol") or "openai",
        }
        for k, v in DEFAULT_CONFIG.items():
            out[k] = cfg.get(k, v)
        return jsonify(out)

    body = request.get_json(silent=True) or {}
    cfg = _load_ai_config()
    # ---- 第一阶段：校验（不落盘，保证任一失败都不产生部分写入）----
    # base_url / model：字符串，去空白（无范围限制）
    pending = {}
    if "base_url" in body:
        pending["base_url"] = str(body.get("base_url") or "").strip()
    if "model" in body:
        pending["model"] = str(body.get("model") or "").strip()
    # api_protocol：openai | anthropic | gemini（默认 openai）
    if "api_protocol" in body:
        proto = str(body.get("api_protocol") or "").strip().lower()
        if proto not in ("openai", "anthropic", "gemini"):
            return jsonify(error="api_protocol 仅支持 openai、anthropic 或 gemini"), 400
        pending["api_protocol"] = proto
    # 会话调优参数 + max_tokens：权威校验（正整数 / 非负整数 / 字段关系）
    # max_tokens 与 DEFAULT_CONFIG 调优字段共用同一套校验（均为数值字段）。
    tuning, err = _validate_ai_tuning(body, cfg)
    if err:
        return jsonify(error=err), 400
    pending.update(tuning)
    # 弃用字段处理（§11）：
    #   safety_margin —— 仅读取旧配置时接受（校验已过），但记一次弃用告警，且
    #     不写回、不展示（从 pending 剥离，既不落盘也不回显）。
    #   keep_recent_images —— 已在 _validate_ai_tuning 映射为 visual_working_set_max；
    #     单独存在时也映射过去，落盘走新字段。这里不剥离 keep_recent_images，让它
    #     继续落盘以保持旧 sidecar 兼容（旧 sidecar 仍读 keep_recent_images）。
    if "safety_margin" in pending:
        app.logger.warning("safety_margin 已弃用：接受但不再写回或展示")
        pending.pop("safety_margin", None)
    # api_key：空串=清除；与掩码同值=不变；其他=覆盖（明文 → _save_ai_config 加密）
    # 仅在校验全部通过后解析 key 动作（仍属"校验"阶段，未落盘）。
    key_action = None  # ("set", new_plain) | ("clear",) | None=不动
    if "api_key" in body:
        new_key = body.get("api_key")
        if new_key is None:
            key_action = None  # 不传不动
        else:
            new_key = str(new_key)
            if new_key == "":
                key_action = ("clear",)
            elif new_key == _mask_api_key(cfg.get("api_key") or ""):
                key_action = None  # 与掩码同值，不变
            else:
                key_action = ("set", new_key)
    # ---- 第二阶段：落盘（全部校验通过，原子写入 cfg）----
    cfg.update(pending)
    if key_action is not None:
        if key_action[0] == "clear":
            cfg.pop("api_key", None)
        else:  # "set"
            cfg["api_key"] = key_action[1]
    _save_ai_config(cfg)
    # 回显脱敏
    key = cfg.get("api_key") or ""
    out = {
        "base_url": cfg.get("base_url") or "",
        "api_key_set": bool(key),
        "api_key_mask": _mask_api_key(key),
        "model": cfg.get("model") or "",
        "max_tokens": cfg.get("max_tokens") or 2048,
        "api_protocol": cfg.get("api_protocol") or "openai",
    }
    for k, v in DEFAULT_CONFIG.items():
        out[k] = cfg.get(k, v)
    return jsonify(out)


def _ai_slide_ctx(slide_name: str):
    """构造 AI 读片所需的 slide 上下文 dict + 物化回调（materializer）。

    上下文含 config/info/region/fingerprint：region 调本进程内的 slide_cache
    读图（不走 HTTP）；materializer 把 canonical 的 image_ref 物化为 base64
    image_url（§3.3），带 slide_fingerprint 防伪（§3.3 image_ref 防伪）。
    """
    safe = _safe_name(slide_name)
    entry = _get_slide(safe)
    cfg = _load_ai_config()
    with slide_cache.borrow_pair(entry) as pair:
        osr = pair["osr"]
        width, height = osr.dimensions
        try:
            level_downsamples = tuple(osr.level_downsamples)
        except Exception:
            level_downsamples = (1.0,)
        meta = _read_metadata(osr, UPLOAD_DIR / safe)
        mpp = meta.get("mpp_x")
    fingerprint = _slide_fingerprint(safe)

    def region_fn(x, y, w, h, out_w, out_h):
        return _read_region_b64(entry, int(x), int(y), int(w), int(h),
                                int(out_w), int(out_h), safe, mpp)

    ctx = {
        "config": cfg,
        "info": {
            "width": width, "height": height,
            "level_downsamples": level_downsamples, "mpp": mpp,
        },
        "region": region_fn,
        "fingerprint": fingerprint,
    }

    def materializer(ref):
        """image_ref → image_url（物化，§3.3）。"""
        fp = ref.get("slide_fingerprint") or ""
        if fp and fp != fingerprint:
            return {"type": "text", "text": "该图因切片变更不可用。"}
        src = ref.get("src") or {}
        x = int(src.get("x") or 0)
        y = int(src.get("y") or 0)
        w = int(src.get("w") or 1)
        h = int(src.get("h") or 1)
        try:
            r = _read_region_b64(entry, x, y, w, h, 1568, 1568, safe, mpp)
        except Exception:
            return {"type": "text", "text": "该图因切片变更不可用。"}
        return {
            "type": "image_url",
            "image_url": {"url": "data:image/jpeg;base64," + r["image_base64"]},
        }

    return ctx, materializer


def _slide_fingerprint(safe: str) -> str:
    """切片内容指纹（mtime+size），image_ref 防伪用（§3.3）。"""
    p = UPLOAD_DIR / safe
    try:
        st = p.stat()
        return "{}:{}".format(st.st_mtime_ns, st.st_size)
    except Exception:
        return ""


def _overlay_coord_ticks(img, x0, y0, w0, h0):
    """在 AI 快照图像顶缘/左缘画 level-0 坐标刻度（视觉尺子）。

    只画刻度与数值，不画网格/倍率文字——倍率等结构化信息随文本返回；
    图像刻度的唯一用途是让模型看着图内特征读出它的 level-0 坐标。
    移植自 VirtualMicroscope services/slide-server/reader.py overlay_viewfinder。
    """
    from PIL import ImageDraw, ImageFont

    try:
        iw, ih = img.size
        draw = ImageDraw.Draw(img, "RGBA")
        font_size = max(12, min(iw, ih) // 48)
        try:
            # Pillow>=10.1 支持 size；旧版 TypeError 时回退默认字体
            font = ImageFont.load_default(size=font_size)
        except TypeError:
            font = ImageFont.load_default()
        tick_color = (0, 255, 255, 230)  # 青色
        shadow = (0, 0, 0, 220)
        n_ticks = 5

        # X 轴刻度（顶缘）：竖短线 + level-0 X 值
        for i in range(n_ticks):
            frac = i / (n_ticks - 1)
            px = min(int(iw * frac), iw - 1)  # 最右刻度线防出画布
            l0x = int(x0 + w0 * frac)
            draw.line([(px, 0), (px, 10)], fill=tick_color, width=2)
            label = str(l0x)
            # 最右侧刻度文字防溢出：估算宽度，超界则左移
            try:
                tw = draw.textlength(label, font=font)
            except Exception:  # noqa: BLE001
                tw = 0
            tx = px + 2
            if tx + tw > iw:
                tx = max(0, iw - tw - 2)
            draw.text((tx + 1, 1), label, fill=shadow, font=font)
            draw.text((tx, 0), label, fill=tick_color, font=font)

        # Y 轴刻度（左缘）：横短线 + level-0 Y 值
        for i in range(n_ticks):
            frac = i / (n_ticks - 1)
            py = min(int(ih * frac), ih - 1)  # 最下刻度线防出画布
            l0y = int(y0 + h0 * frac)
            draw.line([(0, py), (10, py)], fill=tick_color, width=2)
            # 底部标签防出界：保证读数完整可见
            ty = min(py + 1, ih - font_size - 2)
            draw.text((2, ty + 1), str(l0y), fill=shadow, font=font)
            draw.text((1, ty), str(l0y), fill=tick_color, font=font)
    except Exception:  # noqa: BLE001
        # 画刻度失败不影响出图
        pass
    return img


def _aspect_fit_size(w_src, h_src, max_long_edge):
    """按原始宽高比计算输出尺寸：最长边 = max_long_edge，限制 [1, 4096]（§6.1）。

    保持宽高比，禁止固定拉伸为正方形。max_long_edge 会被 clamp 到 [1, 4096]。
    """
    longest = max(int(w_src), int(h_src))
    edge = max(1, min(int(max_long_edge), 4096))
    if longest <= 0:
        return edge, edge
    scale = float(edge) / float(longest)
    ow = max(1, min(4096, int(round(w_src * scale))))
    oh = max(1, min(4096, int(round(h_src * scale))))
    return ow, oh


def _read_region_b64(entry, x, y, w, h, out_w, out_h, safe, mpp,
                     max_long_edge=None, jpeg_quality=DERIVATIVE_JPEG_QUALITY):
    """实际读 region → JPEG base64（与 /region 端点逻辑一致，供 AI 进程内调用）。

    输出尺寸规则（§6.1）：
      - max_long_edge 非空时：按 bbox 原始宽高比计算 out_w/out_h，最长边 =
        max_long_edge（保持比例，限制 [1,4096]），忽略显式 out_w/out_h。
      - 否则：用显式 out_w/out_h（仍 clamp 到 [1,4096]），保持旧契约。
    LANCZOS resize 保持不变；JPEG 质量 jpeg_quality 默认 DERIVATIVE_JPEG_QUALITY(85)。
    """
    with slide_cache.borrow_pair(entry) as pair:
        osr = pair["osr"]
        width, height = osr.dimensions
        x2 = max(0, min(x, max(0, width - 1)))
        y2 = max(0, min(y, max(0, height - 1)))
        w2 = max(0, min(w, max(0, width - x2)))
        h2 = max(0, min(h, max(0, height - y2)))
        if w2 <= 0 or h2 <= 0:
            w2, h2 = 1, 1
        ds = max(w2, h2) / 1568.0 if max(w2, h2) > 1568 else 1.0
        try:
            lvl = osr.get_best_level_for_downsample(ds) if ds > 1 else 0
        except Exception:
            lvl = 0
        try:
            ds_lvl = float(osr.level_downsamples[lvl]) if lvl < len(osr.level_downsamples) else 1.0
        except Exception:
            ds_lvl = 1.0
        rw = max(1, int(round(w2 / ds_lvl)))
        rh = max(1, int(round(h2 / ds_lvl)))
        region = osr.read_region((x2, y2), lvl, (rw, rh))
        if region.mode != "RGB":
            region = region.convert("RGB")
        # 输出尺寸：max_long_edge 优先（保宽高比），否则用显式 out_w/out_h。
        if max_long_edge is not None and int(max_long_edge) > 0:
            ow, oh = _aspect_fit_size(w2, h2, max_long_edge)
        else:
            ow = max(1, min(out_w, 4096))
            oh = max(1, min(out_h, 4096))
        if (ow, oh) != (w2, h2):
            region = region.resize((ow, oh), Image.LANCZOS)
        mag = None
        if mpp and mpp > 0:
            try:
                level_ds = osr.level_downsamples
                ds_lvl = float(level_ds[lvl]) if lvl < len(level_ds) else 1.0
            except Exception:
                ds_lvl = 1.0
            base = 10.0 / mpp
            mag = base / ds_lvl if ds_lvl > 0 else base
    # AI 快照图像画 level-0 坐标刻度尺（视觉尺子，失败不影响出图）
    _overlay_coord_ticks(region, x2, y2, w2, h2)
    buf = io.BytesIO()
    region.save(buf, format="JPEG", quality=int(jpeg_quality))
    img_b64 = base64.b64encode(buf.getvalue()).decode("ascii")
    return {
        "image_base64": img_b64,
        "mime": "image/jpeg",
        "width": ow, "height": oh,
        "src": {"x": x2, "y": y2, "w": w2, "h": h2},
        "magnification": mag,
    }


def _derivative_encoder_info(jpeg_quality=DERIVATIVE_JPEG_QUALITY):
    """返回当前编码器环境信息（§6.3），随 region 响应回传供 sidecar 校验/记录。

    jpeg_quality 必须传本次响应实际使用的质量，不能回传常量默认值。
    """
    try:
        pil_version = Image.__version__
    except Exception:  # noqa: BLE001
        pil_version = "unknown"
    return {
        "id": DERIVATIVE_ENCODER_ID,
        "version": pil_version,
        "resize": DERIVATIVE_RESIZE_ALGORITHM,
        "overlay_version": OVERLAY_VERSION,
        "jpeg_quality": int(jpeg_quality),
    }


# --------------------------------------------------------------------------- #
# AI sidecar internal 回调端点（pi 迁移 Step 2）
#
# 这些端点供 Node sidecar 回调本进程读图/落标注/取变更/取切片信息，复用
# 上面的内部函数（_read_region_b64 / share_store.add_roi 等），不复制逻辑。
# 全部用 _require_internal 校验 X-AI-Internal-Token（共享 token 互信），不走
# 管理员 session 鉴权。参数校验缺失/非法 → 400 JSON {error}。
# --------------------------------------------------------------------------- #
@app.route("/internal/ai/region", methods=["POST"])
def internal_ai_region():
    """sidecar 取 level-0 区域图（含青色坐标刻度尺）。

    body: {slide, x, y, w, h, out_w?, out_h?, max_long_edge?,
           jpeg_quality?, expected_fingerprint?}（level-0 整数）。
    expected_fingerprint 为可选字符串：若非空且与当前切片指纹不一致，返回 409。

    输出尺寸（§6.1）：
      - max_long_edge（正整数，[1,4096]）：服务端按 bbox 原始宽高比计算 out_w/out_h
        （最长边 = max_long_edge，保持比例）。与显式 out_w/out_h 同时给出时，以
        max_long_edge 为准（保宽高比，避免固定拉伸）。
      - 仅 out_w/out_h：旧契约，强制到精确尺寸（不保宽高比），保持向后兼容。
    返回 {image_base64, mime, width, height, src, magnification, encoder}（encoder
    含 id/version/resize/overlay_version/jpeg_quality，供 sidecar 校验派生规格 §6.3）。
    """
    auth = _require_internal()
    if auth:
        return auth
    body = request.get_json(silent=True) or {}
    slide = body.get("slide")
    if not isinstance(slide, str) or not slide:
        return jsonify(error="slide 参数缺失"), 400

    def _parse_int(key):
        v = body.get(key)
        try:
            return int(v)
        except (TypeError, ValueError):
            return None

    x = _parse_int("x")
    y = _parse_int("y")
    w = _parse_int("w")
    h = _parse_int("h")
    if x is None or y is None or w is None or h is None:
        return jsonify(error="x/y/w/h 参数需为整数"), 400
    if x < 0 or y < 0 or w <= 0 or h <= 0:
        return jsonify(error="参数越界（0<=x,y，0<w,h）"), 400
    out_w = _parse_int("out_w")
    out_h = _parse_int("out_h")
    max_long_edge = _parse_int("max_long_edge")
    jpeg_quality = _parse_int("jpeg_quality")
    if max_long_edge is not None and (max_long_edge < 1 or max_long_edge > 4096):
        return jsonify(error="max_long_edge 需在 1..4096"), 400
    if jpeg_quality is not None and (jpeg_quality < 1 or jpeg_quality > 100):
        return jsonify(error="jpeg_quality 需在 1..100"), 400

    safe = _safe_name(slide)
    expected_fp = body.get("expected_fingerprint")
    if isinstance(expected_fp, str) and expected_fp:
        fp = _slide_fingerprint(safe)
        if fp != expected_fp:
            return jsonify(error="切片指纹不匹配（文件已变更）"), 409
    entry = _get_slide(safe)
    with slide_cache.borrow_pair(entry) as pair:
        osr = pair["osr"]
        meta = _read_metadata(osr, UPLOAD_DIR / safe)
        mpp = meta.get("mpp_x")
    # 默认输出尺寸（max_long_edge 未给时用旧默认 1568×1568）
    if not out_w or out_w <= 0:
        out_w = 1568
    if not out_h or out_h <= 0:
        out_h = 1568
    q = jpeg_quality if (jpeg_quality and jpeg_quality > 0) else DERIVATIVE_JPEG_QUALITY
    r = _read_region_b64(entry, x, y, w, h, out_w, out_h, safe, mpp,
                         max_long_edge=max_long_edge, jpeg_quality=q)
    return jsonify({
        "image_base64": r["image_base64"],
        "mime": r["mime"],
        "width": r["width"],
        "height": r["height"],
        "src": r["src"],
        "magnification": r["magnification"],
        "encoder": _derivative_encoder_info(q),
    })


@app.route("/internal/ai/annotate", methods=["POST"])
def internal_ai_annotate():
    """sidecar 落矩形标注（写入标注库，管理员可见可编辑）。

    body: {slide, label, x, y, side_px, note, effect_key, session_id}。
    调 share_store.add_roi(ADMIN_TOKEN, ...)（含 _effect_key 幂等、source="ai"）。
    返回 add_roi 的 roi dict（含 annotation_id/index）。
    """
    auth = _require_internal()
    if auth:
        return auth
    body = request.get_json(silent=True) or {}
    slide = body.get("slide")
    label = body.get("label")
    if not isinstance(slide, str) or not slide:
        return jsonify(error="slide 参数缺失"), 400
    if not isinstance(label, str) or not label.strip():
        return jsonify(error="label 参数缺失"), 400

    def _parse_num(key):
        v = body.get(key)
        try:
            return float(v)
        except (TypeError, ValueError):
            return None

    def _parse_int(key):
        v = body.get(key)
        try:
            return int(v)
        except (TypeError, ValueError):
            return None

    x = _parse_num("x")
    y = _parse_num("y")
    side_px = _parse_int("side_px")
    if x is None or y is None or side_px is None:
        return jsonify(error="x/y/side_px 参数需为数值"), 400
    if x < 0 or y < 0:
        return jsonify(error="坐标需 ≥0"), 400
    if side_px < 1 or side_px > 40000:
        return jsonify(error="side_px 需在 1~40000 之间"), 400
    note = body.get("note") or ""
    effect_key = body.get("effect_key") or ""
    session_id = body.get("session_id") or ""
    # slide 文件名合法性（_safe_name 失败会 abort 400/404）
    _safe_name(slide)
    try:
        roi = share_store.add_roi(
            share_store.ADMIN_TOKEN, slide, label, type="rect", note=note,
            x=int(x), y=int(y), side_px=side_px,
            source="ai", created_by_session_id=session_id,
            _effect_key=effect_key or None,
        )
    except ValueError as e:
        return jsonify(error="落标注失败：{}".format(e)), 400
    return jsonify(roi)


@app.route("/internal/ai/spots", methods=["GET"])
def internal_ai_spots():
    """sidecar 增量取切片变更（含 tombstone）。

    query: slide（必填）、after_seq（缺省 0）。
    返回 {changes: [...], current_seq: int}（share_store.list_changes /
    current_change_seq）。
    """
    auth = _require_internal()
    if auth:
        return auth
    slide = request.args.get("slide", "")
    if not slide:
        return jsonify(error="slide 参数缺失"), 400
    try:
        after_seq = float(request.args.get("after_seq", "0") or "0")
    except (TypeError, ValueError):
        after_seq = 0
    changes = share_store.list_changes(slide, after_seq)
    current_seq = share_store.current_change_seq(slide)
    return jsonify({"changes": changes, "current_seq": current_seq})


@app.route("/internal/ai/slide_info", methods=["GET"])
def internal_ai_slide_info():
    """sidecar 取切片尺寸/金字塔/mpp/指纹。

    query: slide（必填）。复用 _ai_slide_ctx 的取数逻辑（width/height/
    level_downsamples/mpp）与 _slide_fingerprint。slide 不存在 → 404。
    """
    auth = _require_internal()
    if auth:
        return auth
    slide = request.args.get("slide", "")
    if not slide:
        return jsonify(error="slide 参数缺失"), 400
    safe = _safe_name(slide)
    entry = _get_slide(safe)
    with slide_cache.borrow_pair(entry) as pair:
        osr = pair["osr"]
        width, height = osr.dimensions
        try:
            level_downsamples = tuple(osr.level_downsamples)
        except Exception:  # noqa: BLE001
            level_downsamples = (1.0,)
        meta = _read_metadata(osr, UPLOAD_DIR / safe)
        mpp = meta.get("mpp_x")
    fingerprint = _slide_fingerprint(safe)
    return jsonify({
        "width": width,
        "height": height,
        "level_downsamples": list(level_downsamples),
        "mpp": mpp,
        "fingerprint": fingerprint,
    })


@app.route("/api/ai/run", methods=["POST"])
def api_ai_run():
    """主 session 起跑（SSE）。body: {slide, task?, fresh?}。

    代理到 sidecar POST /run：注入 config（base_url/api_key 明文/model/
    api_protocol + 全部调优参数）。SSE 字节透传，状态码原样（含 409）。
    """
    body = request.get_json(silent=True) or {}
    slide = body.get("slide")
    if not isinstance(slide, str) or not slide:
        return jsonify(error="缺少 slide"), 400
    payload = {
        "slide": slide,
        "config": _build_sidecar_config(),
    }
    task = body.get("task")
    if isinstance(task, str):
        payload["task"] = task
    # JSON body 与 query 双重兼容（前端历史上把 fresh=1 放在 query）
    if bool(body.get("fresh")) or request.args.get("fresh") == "1":
        payload["fresh"] = True
    return _proxy_sse("/run", payload)


@app.route("/api/ai/continue", methods=["POST"])
def api_ai_continue():
    """主 session 从落库 state+messages 续跑（SSE）。body: {slide}。

    代理到 sidecar POST /continue：注入 config。无 main → 404（sidecar 返回）。
    """
    body = request.get_json(silent=True) or {}
    slide = body.get("slide")
    if not isinstance(slide, str) or not slide:
        return jsonify(error="缺少 slide"), 400
    payload = {
        "slide": slide,
        "config": _build_sidecar_config(),
    }
    return _proxy_sse("/continue", payload)


@app.route("/api/ai/ask", methods=["POST"])
def api_ai_ask():
    """fork 起跑/续聊（批注式对话，SSE）。body: {slide, annotation_id, question?}。

    代理到 sidecar POST /ask：注入 config。根标注已删除 → 410（sidecar 返回）。
    """
    body = request.get_json(silent=True) or {}
    slide = body.get("slide")
    annotation_id = body.get("annotation_id")
    if not isinstance(slide, str) or not slide:
        return jsonify(error="缺少 slide"), 400
    if not isinstance(annotation_id, str) or not annotation_id:
        return jsonify(error="缺少 annotation_id"), 400
    payload = {
        "slide": slide,
        "annotation_id": annotation_id,
        "config": _build_sidecar_config(),
    }
    question = body.get("question")
    if isinstance(question, str):
        payload["question"] = question
    return _proxy_sse("/ask", payload)


@app.route("/api/ai/branch", methods=["POST"])
def api_ai_branch():
    """branch 起跑/续聊（从标注起步的完整会话，全量工具，SSE）。

    body: {slide, annotation_id, question?}。代理到 sidecar POST /branch：注入
    config。根标注已删除 → 410（sidecar 返回）。契约同 /api/ai/ask。
    """
    body = request.get_json(silent=True) or {}
    slide = body.get("slide")
    annotation_id = body.get("annotation_id")
    if not isinstance(slide, str) or not slide:
        return jsonify(error="缺少 slide"), 400
    if not isinstance(annotation_id, str) or not annotation_id:
        return jsonify(error="缺少 annotation_id"), 400
    payload = {
        "slide": slide,
        "annotation_id": annotation_id,
        "config": _build_sidecar_config(),
    }
    question = body.get("question")
    if isinstance(question, str):
        payload["question"] = question
    return _proxy_sse("/branch", payload)


@app.route("/api/ai/cancel", methods=["POST"])
def api_ai_cancel():
    """显式取消。body: {session_id?, slide?}。原样转发到 sidecar POST /cancel。"""
    body = request.get_json(silent=True) or {}
    return _proxy_json("/cancel", body)


@app.route("/api/ai/sessions")
def api_ai_sessions():
    """列出某切片的 main + 活跃 forks。?slide= 必填。代理 sidecar GET /sessions。"""
    slide = request.args.get("slide")
    if not slide:
        return jsonify(error="缺少 slide"), 400
    return _proxy_json("/sessions", None, method="GET", query={"slide": slide})


@app.route("/api/ai/session/<session_id>")
def api_ai_session_detail(session_id):
    """session detail + 脱敏 transcript。代理 sidecar GET /session/<id>。"""
    return _proxy_json("/session/" + session_id, None, method="GET")


@app.route("/api/ai/session/<session_id>/archive", methods=["POST"])
@app.route("/api/ai/session/<session_id>/unarchive", methods=["POST"])
def api_ai_session_archive(session_id):
    """fork 归档/恢复。代理 sidecar POST /session/<id>/archive|unarchive。"""
    sub = "archive" if request.path.endswith("/archive") else "unarchive"
    body = request.get_json(silent=True) or {}
    return _proxy_json("/session/{}/{}".format(session_id, sub), body)


@app.route("/api/ai/session/<session_id>/stream")
def api_ai_session_stream(session_id):
    """SSE 重挂/断线重连。代理 sidecar GET /session/<id>/stream。

    透传 after_seq query 与 Last-Event-ID header；SSE 字节透传。
    """
    return _proxy_sse("/session/{}/stream".format(session_id), None, method="GET")


# --------------------------------------------------------------------------- #
# sidecar 代理辅助
# --------------------------------------------------------------------------- #
def _sidecar_unavailable_response():
    """sidecar 不可达（ConnectionError/超时）→ 503 JSON。"""
    return jsonify(error="ai sidecar 不可用"), 503


def _proxy_json(path, body, method="POST", query=None):
    """代理普通（非 SSE）端点到 sidecar，原样透传响应 body 与状态码。

    body 为 None 时不发 JSON（GET 请求）。query 仅 GET 时拼到 URL。
    """
    url = AI_SIDECAR_URL + path
    try:
        if method == "GET":
            r = requests.get(url, params=query, timeout=_AI_SIDECAR_TIMEOUT)
        else:
            r = requests.post(url, json=body or {}, timeout=_AI_SIDECAR_TIMEOUT)
    except (requests.ConnectionError, requests.Timeout):
        return _sidecar_unavailable_response()
    # 透传 Content-Type（JSON 或其它）与状态码
    ctype = r.headers.get("Content-Type", "application/json")
    return Response(r.content, status=r.status_code, mimetype=ctype.split(";")[0])


def _proxy_sse(path, body, method="POST"):
    """代理 SSE 端点到 sidecar：流式透传字节块，透传响应头与状态码。

    run/continue/ask（POST）注入 body；stream（GET）不注入 body，透传 after_seq
    query 与 Last-Event-ID header。SSE 长连接不设读超时（read timeout=大数）。
    sidecar 不可达 → 503 JSON。错误响应（409/404/410 等非 SSE，JSON）按
    content-type 正确处理：非 text/event-stream 视为普通 JSON 透传。
    """
    url = AI_SIDECAR_URL + path
    headers = {}
    last_event_id = request.headers.get("Last-Event-ID")
    if last_event_id:
        headers["Last-Event-ID"] = last_event_id
    params = None
    if method == "GET":
        # 透传 after_seq query（其它 query 不转，契约上只需 after_seq）
        after_seq = request.args.get("after_seq")
        if after_seq is not None:
            params = {"after_seq": after_seq}

    try:
        if method == "GET":
            upstream = requests.get(
                url, params=params, headers=headers or None,
                stream=True, timeout=(_AI_SIDECAR_TIMEOUT, _AI_SIDECAR_SSE_READ_TIMEOUT),
            )
        else:
            upstream = requests.post(
                url, json=body or {}, stream=True,
                timeout=(_AI_SIDECAR_TIMEOUT, _AI_SIDECAR_SSE_READ_TIMEOUT),
            )
    except (requests.ConnectionError, requests.Timeout):
        return _sidecar_unavailable_response()

    status = upstream.status_code
    ctype = upstream.headers.get("Content-Type", "")

    # 错误响应（非 text/event-stream，sidecar 已在 body 里给出 JSON 错误）：
    # 直接把整段 body 作为 JSON 透传，保留状态码。
    if "text/event-stream" not in ctype:
        content = upstream.content
        upstream.close()
        return Response(content, status=status,
                        mimetype=ctype.split(";")[0] if ctype else "application/json")

    # SSE：流式透传字节块（不缓冲、不修改帧内容）。
    session_id = upstream.headers.get("X-AI-Session-ID", "")

    def generate():
        try:
            for chunk in upstream.iter_content(chunk_size=4096):
                if chunk:
                    yield chunk
        finally:
            upstream.close()

    out_headers = {
        "Cache-Control": "no-cache",
        "X-Accel-Buffering": "no",
    }
    if session_id:
        out_headers["X-AI-Session-ID"] = session_id
    return Response(generate(), status=status, mimetype="text/event-stream",
                    headers=out_headers)
# 分享管理 API（管理员，内网）
# --------------------------------------------------------------------------- #
@app.route("/api/share/create", methods=["POST"])
def api_share_create():
    """创建分享链接。JSON: {slides: [...], expires_hours: number, permissions?}。

    Stage 3a-2a：user 只能分享自己拥有的切片（can_manage_share，403 并说明）；
    permissions 可选（view/annotate/download 子集，缺省 view+annotate 等价旧行为）。
    """
    ident = current_identity()
    body = request.get_json(silent=True) or {}
    slides = body.get("slides")
    expires_hours = body.get("expires_hours")

    if not isinstance(slides, list) or len(slides) == 0:
        return jsonify(error="slides 不能为空"), 400
    if expires_hours is None:
        return jsonify(error="缺少 expires_hours"), 400
    try:
        expires_hours = float(expires_hours)
    except (TypeError, ValueError):
        return jsonify(error="expires_hours 需为数值"), 400
    if expires_hours < 0.1 or expires_hours > 720:
        return jsonify(error="expires_hours 需在 0.1~720 之间"), 400

    # 校验每个文件存在且扩展名合法
    clean = []
    for name in slides:
        if not isinstance(name, str):
            return jsonify(error="slides 含非法文件名"), 400
        safe = _sanitize_name(name)
        if not safe or safe != name:
            return jsonify(error=f"非法文件名: {name}"), 400
        if safe.split(".")[-1].lower() not in SUPPORTED_EXTS:
            return jsonify(error=f"不支持的文件类型: {name}"), 400
        if not (UPLOAD_DIR / safe).is_file():
            return jsonify(error=f"切片不存在: {name}"), 400
        clean.append(safe)

    # 权限矩阵：user 只能分享自己的切片
    if not can_manage_share(clean):
        return jsonify(error="只能分享自己拥有的切片"), 403

    # roi_sizes 可选：未传或 None 用默认；数组则逐元素校验（6/6.5/6.0/6.5）
    roi_sizes = body.get("roi_sizes")
    if roi_sizes is not None:
        if not isinstance(roi_sizes, list):
            return jsonify(error="roi_sizes 需为数组"), 400
        for s in roi_sizes:
            if isinstance(s, bool) or not isinstance(s, (int, float)):
                return jsonify(error="roi_sizes 元素需为 6 或 6.5"), 400
            if float(s) not in share_store.ALLOWED_ROI_SIZES:
                return jsonify(error="roi_sizes 仅允许 6 或 6.5"), 400

    # permissions 可选：view/annotate/download 子集
    permissions = body.get("permissions")
    if permissions is not None:
        if not isinstance(permissions, list):
            return jsonify(error="permissions 需为数组"), 400
        for p in permissions:
            if p not in (share_store.PERMISSION_VIEW, share_store.PERMISSION_ANNOTATE,
                         share_store.PERMISSION_DOWNLOAD):
                return jsonify(error="permissions 仅支持 view/annotate/download"), 400

    try:
        share = share_store.create_share(
            clean, expires_hours, roi_sizes=roi_sizes, permissions=permissions,
            creator_user_id=ident["user_id"], requester_role=ident["role"])
    except (ValueError, PermissionError) as e:
        return jsonify(error=str(e)), 400
    url = SHARE_BASE_URL + "/s/" + share["token"]
    return jsonify(
        token=share["token"],
        url=url,
        expires_at=share["expires_at"],
        roi_sizes=share.get("roi_sizes", list(share_store.DEFAULT_ROI_SIZES)),
        permissions=share.get("permissions", list(share_store.DEFAULT_PERMISSIONS)),
    )


@app.route("/api/share/list")
def api_share_list():
    """列出分享（owner 全量；user 仅自己创建的），附加 url 与 roi_count。"""
    ident = current_identity()
    shares = share_store.list_shares()
    if ident["role"] != user_store.ROLE_OWNER:
        uid = ident["user_id"]
        shares = [s for s in shares if s.get("creator_user_id") == uid]
    roi_counts = share_store.roi_count_by_token()
    for sh in shares:
        sh["url"] = SHARE_BASE_URL + "/s/" + sh["token"]
        sh["roi_count"] = roi_counts.get(sh["token"], 0)
    return jsonify(shares)


@app.route("/api/share/revoke", methods=["POST"])
def api_share_revoke():
    """撤销分享。JSON: {token}。owner 任意；user 仅自己创建的。"""
    ident = current_identity()
    body = request.get_json(silent=True) or {}
    token = body.get("token")
    if not token:
        return jsonify(error="缺少 token"), 400
    # user 只能撤销自己创建的分享：先查 list 找 creator
    if ident["role"] != user_store.ROLE_OWNER:
        mine = [s for s in share_store.list_shares()
                if s.get("token") == token and s.get("creator_user_id") == ident["user_id"]]
        if not mine:
            return _denied("只能撤销自己创建的分享")
    ok = share_store.revoke_share(token)
    if not ok:
        return jsonify(error="分享不存在"), 404
    return jsonify(ok=True)


@app.route("/api/share/rois")
def api_share_rois():
    """列出 ROI（owner 全量；user 仅可见切片的标注）。"""
    rois = share_store.list_rois()
    if _is_owner():
        return jsonify(rois)
    visible = _visible_slide_names()
    return jsonify([r for r in rois if r.get("slide") in visible])


@app.route("/api/share/<token>/claim", methods=["POST"])
def api_share_claim(token):
    """注册 user 认领分享链接（docs §5.4）。

    认领后该 share 的 slides 进入该 user 的「可见切片集」。幂等：重复认领返回
    已有 grant。share 无效/已撤销/已过期 → 404。仅登录 owner/user 可认领。
    """
    ident = current_identity()
    if ident["role"] not in (user_store.ROLE_OWNER, user_store.ROLE_USER):
        return jsonify(error="需要登录用户身份认领"), 403
    share = share_store.get_share(token)
    if share is None:
        return jsonify(error="链接无效或已过期"), 404
    body = request.get_json(silent=True) or {}
    perms = body.get("permissions")
    if perms is not None:
        if not isinstance(perms, list):
            return jsonify(error="permissions 需为数组"), 400
        for p in perms:
            if p not in (share_store.PERMISSION_VIEW, share_store.PERMISSION_ANNOTATE,
                         share_store.PERMISSION_DOWNLOAD):
                return jsonify(error="permissions 仅支持 view/annotate/download"), 400
    try:
        grant = share_store.claim_share(token, ident["user_id"], permissions=perms)
    except ValueError as e:
        return jsonify(error=str(e)), 400
    return jsonify(grant)


# --------------------------------------------------------------------------- #
# 项目管理 API（管理员，内网）
# --------------------------------------------------------------------------- #
def _validate_slide_names(names):
    """校验切片名列表：均需合法、扩展名受支持、文件存在。

    返回 (clean_list, error_str)；成功时 error_str 为 None。
    """
    if not isinstance(names, list):
        return None, "slides 需为数组"
    clean = []
    for name in names:
        if not isinstance(name, str):
            return None, "slides 含非法文件名"
        safe = _sanitize_name(name)
        if not safe or safe != name:
            return None, "非法文件名: " + name
        if safe.split(".")[-1].lower() not in SUPPORTED_EXTS:
            return None, "不支持的文件类型: " + name
        if not (UPLOAD_DIR / safe).is_file():
            return None, "切片不存在: " + name
        clean.append(safe)
    return clean, None


@app.route("/api/project/create", methods=["POST"])
def api_project_create():
    """创建项目。JSON: {name, note?, slides?}。Stage 3a-2a：归属=创建者。"""
    ident = current_identity()
    body = request.get_json(silent=True) or {}
    name = body.get("name", "")
    note = body.get("note", "")
    slides = body.get("slides", [])
    if not isinstance(name, str) or not name.strip():
        return jsonify(error="name 不能为空"), 400
    clean, err = _validate_slide_names(slides if isinstance(slides, list) else [])
    if err:
        return jsonify(error=err), 400
    try:
        proj = share_store.create_project(
            name=name.strip(), note=note or "", slides=clean,
            owner_user_id=ident["user_id"], requester_role=ident["role"])
    except PermissionError:
        return jsonify(error="无权创建项目"), 403
    return jsonify(proj)


@app.route("/api/projects")
def api_projects():
    """列出项目（owner 全量；user 仅自己 owner_user_id 的），附加 roi_count。"""
    ident = current_identity()
    projects = share_store.list_projects()
    if ident["role"] != user_store.ROLE_OWNER:
        uid = ident["user_id"]
        projects = [p for p in projects if p.get("owner_user_id") == uid]
    # 一次性取 annotations_by_slide，按项目 slides 汇总
    by_slide = share_store.annotations_by_slide()

    def _count_for(slides):
        total = 0
        for s in slides:
            for grp in by_slide.get(s, []):
                total += grp.get("count", 0)
        return total

    out = []
    for p in projects:
        item = dict(p)
        item["roi_count"] = _count_for(p.get("slides", []))
        out.append(item)
    return jsonify(out)


@app.route("/api/project/<pid>")
def api_project_detail(pid):
    """单个项目详情，含每张切片的标注摘要。Stage 3a-2a：owner 任意；user 仅自己。"""
    if not _can_access_project(pid):
        return _denied()
    proj = share_store.get_project(pid)
    if proj is None:
        return jsonify(error="项目不存在"), 404
    by_slide = share_store.annotations_by_slide()
    project_slides = set(proj.get("slides", []))
    slide_annotations = [
        {"slide": s, "annotations": by_slide.get(s, [])}
        for s in proj.get("slides", [])
    ]
    return jsonify(project=proj, slide_annotations=slide_annotations)


@app.route("/api/project/<pid>", methods=["PATCH"])
def api_project_update(pid):
    """更新项目字段。JSON: {name?, note?, slides?}。Stage 3a-2a：owner 任意；user 仅自己。"""
    if not _can_access_project(pid):
        return _denied()
    body = request.get_json(silent=True) or {}
    slides = body.get("slides")
    if slides is not None:
        clean, err = _validate_slide_names(slides)
        if err:
            return jsonify(error=err), 400
        slides = clean
    proj = share_store.update_project(
        pid,
        name=body.get("name"),
        note=body.get("note"),
        slides=slides,
    )
    if proj is None:
        return jsonify(error="项目不存在"), 404
    return jsonify(proj)


@app.route("/api/project/<pid>/slides", methods=["POST"])
def api_project_add_slides(pid):
    """向项目追加切片。JSON: {slides: [...]}。Stage 3a-2a：owner 任意；user 仅自己。"""
    if not _can_access_project(pid):
        return _denied()
    body = request.get_json(silent=True) or {}
    slides = body.get("slides")
    clean, err = _validate_slide_names(slides)
    if err:
        return jsonify(error=err), 400
    proj = share_store.add_slides_to_project(pid, clean)
    if proj is None:
        return jsonify(error="项目不存在"), 404
    return jsonify(proj)


@app.route("/api/project/<pid>/slide/<name>", methods=["DELETE"])
def api_project_remove_slide(pid, name):
    """从项目移除某切片（仅解除归属，不删文件）。Stage 3a-2a：owner 任意；user 仅自己。"""
    if not _can_access_project(pid):
        return _denied()
    safe = _sanitize_name(name)
    if not safe or safe != name:
        return jsonify(error="非法文件名"), 400
    proj = share_store.remove_slide_from_project(pid, safe)
    if proj is None:
        return jsonify(error="项目不存在或无该切片"), 404
    return jsonify(proj)


@app.route("/api/project/<pid>", methods=["DELETE"])
def api_project_delete(pid):
    """删除项目（不删切片文件）。Stage 3a-2a：owner 任意；user 仅自己。"""
    if not _can_access_project(pid):
        return _denied()
    ok = share_store.delete_project(pid)
    if not ok:
        return jsonify(error="项目不存在"), 404
    return jsonify(ok=True)


@app.route("/api/annotations")
def api_annotations():
    """返回标注（按 slide 或 project 过滤），供查看器加载某切片的标记。

    查询参数：
      - slide=<name>：只返回该切片的标注分组
      - project=<pid>：只返回该项目内切片的标注
    同时传 slide 与 project 时，slide 优先（且需属于项目）。
    items 已含 type 与全部几何字段（经 store 自动带）。
    Stage 3a-2a：owner 全量；user 仅可见切片的标注，越权 403。
    """
    slide = request.args.get("slide")
    project = request.args.get("project")

    if slide:
        safe = _sanitize_name(slide)
        if not safe or safe != slide:
            return jsonify(error="非法文件名"), 400
        if not can_view_slide(safe):
            return _denied()
        by_slide = share_store.annotations_by_slide()
        return jsonify({"slide": safe, "annotations": by_slide.get(safe, [])})

    if project:
        if not _can_access_project(project):
            return _denied()
        by_slide = share_store.annotations_by_project(project)
        return jsonify({"project": project, "by_slide": by_slide})

    # 默认返回全部（user 按可见切片过滤）
    by_slide = share_store.annotations_by_slide()
    if _is_owner():
        return jsonify({"by_slide": by_slide})
    visible = _visible_slide_names()
    filtered = {s: v for s, v in by_slide.items() if s in visible}
    return jsonify({"by_slide": filtered})


# --------------------------------------------------------------------------- #
# 样本别名/备注 API（管理员）
# --------------------------------------------------------------------------- #
@app.route("/api/slide/<name>/meta", methods=["POST"])
def api_slide_meta(name):
    """设置切片的别名/备注/公开档。JSON: {alias?, note?, public?}（None 不改，空串清除）。

    name 需为已存在的切片文件。
    Stage 3a-2a（docs §5.1.1）：
      - public 仅 owner 可设置（user 尝试 403）；
      - user 可改自己切片的 alias/note（不变），改他人切片 403。
    """
    ident = current_identity()
    safe = _safe_name(name)
    body = request.get_json(silent=True) or {}
    alias = body.get("alias", None)
    note = body.get("note", None)
    public = body.get("public", None)
    # alias/note 仅接受字符串或 None
    if alias is not None and not isinstance(alias, str):
        return jsonify(error="alias 需为字符串"), 400
    if note is not None and not isinstance(note, str):
        return jsonify(error="note 需为字符串"), 400
    if public is not None and not isinstance(public, bool):
        return jsonify(error="public 需为布尔值"), 400
    # public 仅 owner 可改
    if public is not None and ident["role"] != user_store.ROLE_OWNER:
        return jsonify(error="仅 owner 可设置公开状态"), 403
    # user 只能编辑自己切片的 alias/note
    if ident["role"] != user_store.ROLE_OWNER:
        if _slide_owner(safe) != ident["user_id"]:
            return jsonify(error="只能编辑自己切片的元数据"), 403
    try:
        meta = share_store.set_slide_meta(safe, alias=alias, note=note, public=public,
                                          owner_user_id=ident["user_id"],
                                          requester_role=ident["role"])
    except PermissionError:
        return jsonify(error="无权修改元数据"), 403
    return jsonify(ok=True, meta=meta)


# --------------------------------------------------------------------------- #
# 标注 API（管理员直接在切片上做 rect/arrow/freehand 标注）
# --------------------------------------------------------------------------- #
@app.route("/api/annotation", methods=["POST"])
def api_annotation_add():
    """管理员新增标注。JSON: {slide, type?, label?, shared?, ...geometry}。

    token 固定为 "admin"，label 默认 "管理员"。slide 必须存在。
    几何字段随 type 不同：rect(x,y,side_px,size_mm) / arrow(x1,y1,x2,y2) /
    freehand(points)。shared 可选（默认 false），透传给 store 记录公开状态。
    Stage 3a-2a：can_annotate_slide（owner 全量；user 自己的 + 协作切片），无权 403。
    """
    ident = current_identity()
    body = request.get_json(silent=True) or {}
    slide = body.get("slide")
    if not isinstance(slide, str) or not slide:
        return jsonify(error="缺少 slide"), 400
    safe = _sanitize_name(slide)
    if not safe or safe != slide:
        return jsonify(error="非法文件名"), 400
    if not (UPLOAD_DIR / safe).is_file():
        return jsonify(error="切片不存在"), 404
    # 资源级鉴权：能否在该切片落标
    if not can_annotate_slide(safe):
        return _denied()

    typ = body.get("type", "rect")
    if typ not in share_store.ROI_TYPES:
        return jsonify(error="未知标注类型"), 400
    label = body.get("label")
    if label is None:
        label = "管理员"
    if not isinstance(label, str):
        return jsonify(error="label 需为字符串"), 400

    # shared 可选，透传给 store（默认 False）
    shared = bool(body.get("shared", False))
    # note 可选（备注文本），透传给 store 校验/清洗
    note = body.get("note", "")

    # 收集几何字段（透传给 add_roi 校验）
    geom = {}
    for k in ("x", "y", "side_px", "size_mm", "x1", "y1", "x2", "y2", "points"):
        if k in body:
            geom[k] = body[k]
    try:
        roi = share_store.add_roi(
            share_store.ADMIN_TOKEN, safe, label, type=typ, shared=shared, note=note,
            owner_user_id=ident["user_id"], requester_role=ident["role"], **geom
        )
    except (ValueError, PermissionError) as e:
        return jsonify(error=str(e)), 400
    return jsonify(ok=True, index=roi["index"], shared=roi.get("shared", shared))


def _check_annotation_owner(token, index):
    """资源级鉴权（docs §5.1.1）：owner 任意；否则仅本人创建的标注可改/删。

    无权返回 (resp, None)（resp 为 403 JSON）；有权返回 (None, roi_or_None)。
    roi 为 None 表示标注不存在（owner 放行后续 store 调用自行 404）。
    """
    if _is_owner():
        return None, None
    roi = share_store.get_roi(token, index)
    if roi is None or roi.get("owner_user_id") != _current_uid():
        return _denied("只能修改自己创建的标注"), None
    return None, roi


@app.route("/api/annotation/admin/<int:index>", methods=["DELETE"])
def api_annotation_delete_admin(index):
    """管理员删除自己的标注（token="admin" 下第 index 条）。

    Stage 3a-2a：owner 任意；否则仅本人创建（owner_user_id 判定）。
    """
    denied, _roi = _check_annotation_owner(share_store.ADMIN_TOKEN, index)
    if denied:
        return denied
    # delete_roi 返回 (bool, annotation_id|None)；直接当 bool 用会让 404 分支永不触发
    ok, _aid = share_store.delete_roi(share_store.ADMIN_TOKEN, index)
    if not ok:
        return jsonify(error="标注不存在"), 404
    return jsonify(ok=True)


@app.route("/api/annotation/<token>/<int:index>", methods=["DELETE"])
def api_annotation_delete(token, index):
    """管理员删除任意 token 的标注。token 仅允许非空字符串。

    Stage 3a-2a：owner 任意；否则仅本人创建（owner_user_id 判定）。
    """
    if not isinstance(token, str) or not token:
        return jsonify(error="缺少 token"), 400
    denied, _roi = _check_annotation_owner(token, index)
    if denied:
        return denied
    # delete_roi 返回 (bool, annotation_id|None)，需解包
    ok, _aid = share_store.delete_roi(token, index)
    if not ok:
        return jsonify(error="标注不存在"), 404
    return jsonify(ok=True)


@app.route("/api/annotation/<token>/<int:index>", methods=["PATCH"])
def api_annotation_set_shared(token, index):
    """管理员策展/编辑：可切换「公开」状态，或更新几何/备注。

    JSON body 支持任意组合：
      - {"shared": bool}：走 set_roi_shared；
      - {"geom": {...}}：走 update_roi 更新几何（不含 type）；
      - {"note": "..."}：走 update_roi 更新备注。
    两者可同时传（shared 与 geom/note 独立处理）。
    token/index 无效（shared 或 update 侧）返回 404；
    成功返回 {"ok": true, "shared": <更新后值>, "note": <更新后值>}。
    Stage 3a-2a：owner 任意；否则仅本人创建（owner_user_id 判定），无权 403。
    """
    if not isinstance(token, str) or not token:
        return jsonify(error="缺少 token"), 400
    denied, _roi = _check_annotation_owner(token, index)
    if denied:
        return denied
    body = request.get_json(silent=True) or {}

    shared_after = None
    note_after = None

    # shared 部分
    if "shared" in body:
        shared_target = bool(body.get("shared"))
        ok = share_store.set_roi_shared(token, index, shared_target)
        if not ok:
            return jsonify(error="标注不存在"), 404
        shared_after = shared_target

    # geom / note 部分
    if "geom" in body or "note" in body:
        geom = body.get("geom")
        note = body.get("note")
        try:
            updated = share_store.update_roi(token, index, geom=geom, note=note)
        except ValueError as e:
            return jsonify(error=str(e)), 400
        if updated is False:
            return jsonify(error="标注不存在"), 404
        note_after = updated.get("note", "")
        # 若同时没传 shared，回填当前 shared 值便于前端同步
        if shared_after is None:
            shared_after = updated.get("shared")

    # 仅 shared 时回填 note（读当前值）
    if note_after is None:
        rois = share_store.list_rois(token)
        cur = None
        for r in rois:
            if r.get("index") == index:
                cur = r
                break
        note_after = cur.get("note", "") if cur else ""

    return jsonify(ok=True, shared=shared_after, note=note_after)


if __name__ == "__main__":
    # 管理端外网门户由 share_server 合并进程提供（同端口按路径分流），
    # 本进程只保留内网 HTTP 监听；外网走 https://browser.example.invalid/
    app.run(
        host="0.0.0.0",
        port=int(os.environ.get("PORT", 8000)),
        threaded=True,
    )
