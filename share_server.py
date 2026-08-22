#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""切片分享 —— 独立 Flask 服务（view-only，对外）。

监听 0.0.0.0:38000（可用 SHARE_PORT 覆盖）。
所有 /s/<token>/... 路由先校验 token 有效，再校验 slide 归属于该分享。
与主应用通过共享 JSON 文件（share_store）+ 共享上传目录（UPLOAD_DIR）交换数据。
"""

import hashlib
import hmac
import io
import os
import secrets
import threading
import time
from collections import OrderedDict
from pathlib import Path

from flask import (
    Flask,
    Response,
    abort,
    g,
    jsonify,
    render_template,
    request,
    send_file,
    send_from_directory,
)
from werkzeug.utils import secure_filename

from openslide import OpenSlide

import share_store
import slide_cache
import slide_io
# P0-A §3.5：与主站 app.py 共用的 crop 像素闸（同一实现，防两份逻辑漂移）
import crop_guard

app = Flask(__name__)

# 上传目录与主应用共享（容器内挂载同一卷）
UPLOAD_DIR = Path(os.environ.get("UPLOAD_DIR") or (Path.home() / "svs-viewer" / "uploads"))
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

SUPPORTED_EXTS = {
    "svs", "tif", "tiff", "ndpi", "mrxs", "vms", "vmu", "scn", "bif", "svslide",
}

# Deep Zoom 参数（512 瓦片降低公网请求数，渐进式 q82 JPEG 降体积并支持模糊→清晰预览）
DZ_TILE_SIZE = 512
DZ_OVERLAP = 1
# JPEG 编码质量，可由环境变量 JPEG_QUALITY 覆盖（默认 82）
JPEG_QUALITY = int(os.environ.get("JPEG_QUALITY") or 82)
# 保留旧名（与主应用一致，实际值与 DZ_* 一致）
TILE_SIZE = DZ_TILE_SIZE
OVERLAP = DZ_OVERLAP

# 切片句柄池与元数据缓存（与主应用共享 slide_cache 抽象，进程独立）
# 瓦片内存缓存（LRU + TTL）：key=(name, level, x, y)，value=(ts, JPEG bytes)
# 分享端只读，但切片可能被管理端删除后同名重传，加 TTL 兜底避免长期服务旧图
TILE_CACHE_MAX = int(os.environ.get("TILE_CACHE_MAX") or 3000)
TILE_CACHE_TTL = float(os.environ.get("TILE_CACHE_TTL") or 3600)  # 秒
_tile_cache: "OrderedDict[tuple, tuple]" = OrderedDict()
_tile_cache_lock = threading.Lock()


def _tile_cache_get(key):
    """LRU+TTL 命中：未过期才返回，过期剔除。"""
    with _tile_cache_lock:
        item = _tile_cache.get(key)
        if item is None:
            return None
        ts, data = item
        if time.time() - ts > TILE_CACHE_TTL:
            _tile_cache.pop(key, None)
            return None
        _tile_cache.move_to_end(key)
        return data


def _tile_cache_put(key, data):
    """LRU 写入，超上限淘汰最久未用。"""
    with _tile_cache_lock:
        _tile_cache[key] = (time.time(), data)
        _tile_cache.move_to_end(key)
        while len(_tile_cache) > TILE_CACHE_MAX:
            _tile_cache.popitem(last=False)


# --------------------------------------------------------------------------- #
# 辅助函数（从 app.py 复制，保持一致）
# --------------------------------------------------------------------------- #
def _sanitize_name(name: str) -> str:
    """净化文件名：防路径穿越同时保留中文等 Unicode 字符。"""
    if not name or "\x00" in name:
        return ""
    has_non_ascii = any(ord(c) > 127 for c in name)
    if not has_non_ascii:
        return secure_filename(name)
    cleaned_chars = []
    for ch in name:
        if ch in "/\\:" or ord(ch) < 32:
            continue
        cleaned_chars.append(ch)
    cleaned = "".join(cleaned_chars).strip().rstrip(".")
    cleaned = cleaned.replace("..", "")
    return cleaned


def _get_slide(name: str):
    """从缓存获取（或创建）切片的句柄池 entry（惰性打开，见 slide_cache）。"""
    path = UPLOAD_DIR / name
    if not path.is_file():
        abort(404, "切片不存在")
    return slide_cache.get_slide(name, path)


def _to_float(v):
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _mpp_from_tiff_resolution(path: Path):
    """从 TIFF 分辨率标签读取 mpp（与主应用逻辑相同）。"""
    try:
        from PIL import Image
        from PIL.TiffTags import TAGS_V2  # noqa: F401

        with Image.open(str(path)) as img:
            tags = getattr(img, "tag_v2", None)
            if not tags:
                return None, None
            x_res = _to_float(tags.get(282))
            y_res = _to_float(tags.get(283))
            unit = tags.get(296, 2)
            if not x_res or x_res <= 0:
                return None, None
            factor = 25400.0 if unit == 2 else (10000.0 if unit == 3 else None)
            if factor is None:
                return None, None
            mpp_x = factor / x_res
            mpp_y = factor / y_res if y_res and y_res > 0 else mpp_x
            if 0.05 <= mpp_x <= 3.0:
                return mpp_x, mpp_y
    except Exception:
        pass
    return None, None


def _read_metadata(osr: OpenSlide, path: Path) -> dict:
    """读取尺寸与 mpp 元数据（与主应用逻辑相同）。"""
    width, height = osr.dimensions
    props = osr.properties
    objective_f = _to_float(props.get("openslide.objective-power"))

    mpp_x_f = _to_float(props.get("openslide.mpp-x"))
    mpp_y_f = _to_float(props.get("openslide.mpp-y"))

    if mpp_x_f is not None and mpp_y_f is not None:
        mpp_source = "metadata"
    else:
        tiff_mpp_x, tiff_mpp_y = _mpp_from_tiff_resolution(path)
        if tiff_mpp_x is not None:
            mpp_x_f = mpp_x_f if mpp_x_f is not None else tiff_mpp_x
            mpp_y_f = mpp_y_f if mpp_y_f is not None else tiff_mpp_y
            mpp_source = "tiff-resolution"
        elif objective_f is not None and objective_f > 0:
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


# --------------------------------------------------------------------------- #
# 安全核心：token 与 slide 校验
# --------------------------------------------------------------------------- #
# 设备级访客身份：为每个访问 /s/* 的设备静默分配签名 cookie（svs_visitor）。
# 该设备创建的标注会记录 visitor 的 HMAC 哈希，仅允许同一访客编辑/删除自己新建
# 的标记。API 响应不回传原始 visitor，避免复制 cookie 冒用。
VISITOR_COOKIE = "svs_visitor"
VISITOR_MIG_COOKIE = "svs_visitor_mig"
_VISITOR_COOKIE_PREFIX = "v2."
_VISITOR_STORED_PREFIX = "h1."
# (path, secret_bytes)；路径变化（测试隔离）时自动失效
_visitor_secret_cache = None


def _is_secure():
    """按 SHARE_TLS_CERT 是否配置判定当前部署是否走 HTTPS（cookie secure 用）。"""
    cert = os.environ.get("SHARE_TLS_CERT")
    return bool(cert) and os.path.exists(cert)


def _visitor_data_dir() -> Path:
    d = getattr(share_store, "SHARE_DATA_DIR", None)
    if d:
        return Path(d)
    return Path(os.environ.get("SHARE_DATA_DIR") or (Path.home() / "svs-viewer" / "share-data"))


def _visitor_hmac_secret() -> bytes:
    """HMAC 密钥：SHARE_DATA_DIR/visitor_hmac.key（0600），跨进程复用。

    gunicorn 多 worker 首启与 Flask secret 相同：fcntl 排他锁 + 双检 + 临时文件
    原子替换；进程内按路径缓存，避免每次请求读盘。
    """
    global _visitor_secret_cache
    data_dir = _visitor_data_dir()
    data_dir.mkdir(parents=True, exist_ok=True)
    secret_file = data_dir / "visitor_hmac.key"
    path_key = str(secret_file)
    cached = _visitor_secret_cache
    if cached and cached[0] == path_key:
        return cached[1]

    def _read_or_create_locked():
        if secret_file.is_file():
            try:
                raw = secret_file.read_text(encoding="utf-8").strip()
                if raw:
                    return raw.encode("utf-8")
            except OSError:
                pass
        key = secrets.token_hex(32)
        tmp = secret_file.with_name(secret_file.name + ".tmp")
        tmp.write_text(key, encoding="utf-8")
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
        os.replace(tmp, secret_file)
        try:
            os.chmod(secret_file, 0o600)
        except OSError:
            pass
        return key.encode("utf-8")

    try:
        import fcntl
        lock_file = data_dir / "visitor_hmac.lock"
        with open(lock_file, "a+") as lf:
            fcntl.flock(lf.fileno(), fcntl.LOCK_EX)
            try:
                key_bytes = _read_or_create_locked()
            finally:
                fcntl.flock(lf.fileno(), fcntl.LOCK_UN)
    except (ImportError, OSError):
        key_bytes = _read_or_create_locked()

    _visitor_secret_cache = (path_key, key_bytes)
    return key_bytes


def _sign_visitor(vid: str) -> str:
    sig = hmac.new(_visitor_hmac_secret(), vid.encode("utf-8"), hashlib.sha256).hexdigest()[:32]
    return "%s%s.%s" % (_VISITOR_COOKIE_PREFIX, vid, sig)


def _parse_visitor_cookie(raw):
    """校验签名 cookie，返回 visitor id；非法 cookie 返回 None。

    旧 unsigned cookie 不在这里接受；由 _bind_visitor 结合 svs_visitor_mig
    走「当前 token 有明文证明 → 全局迁移」认领。
    """
    if not raw or not isinstance(raw, str):
        return None
    if not raw.startswith(_VISITOR_COOKIE_PREFIX):
        return None
    rest = raw[len(_VISITOR_COOKIE_PREFIX):]
    vid, sep, sig = rest.partition(".")
    if not sep or not vid or not sig:
        return None
    expected = hmac.new(
        _visitor_hmac_secret(), vid.encode("utf-8"), hashlib.sha256
    ).hexdigest()[:32]
    if not hmac.compare_digest(sig, expected):
        return None
    return vid


def _visitor_stored(vid):
    """落库用的 visitor 哈希（不含原始 id）。"""
    if not vid:
        return ""
    digest = hmac.new(
        _visitor_hmac_secret(), vid.encode("utf-8"), hashlib.sha256
    ).hexdigest()
    return _VISITOR_STORED_PREFIX + digest


def _visitor_id():
    """当前请求的访客 id（before_request 绑定）；无则 None。"""
    return getattr(g, "visitor_id", None)


def _share_token_from_path(path):
    """从 /s/<token>/... 抽出 share token；无则 None。"""
    if not path:
        return None
    parts = path.split("/")
    if len(parts) >= 3 and parts[1] == "s" and parts[2]:
        return parts[2]
    return None


def _legacy_plaintext(raw):
    """从 cookie 取出升级前的 unsigned visitor id；签名/空值返回 None。"""
    if not raw or not isinstance(raw, str):
        return None
    if raw.startswith(_VISITOR_COOKIE_PREFIX) or raw.startswith(_VISITOR_STORED_PREFIX):
        return None
    return raw


def _reclaim_legacy_visitor(token, plaintext_vid, canonical_vid=None):
    """升级路径：当前 token 的分享仍有效且确有明文匹配时，原子全局迁移。

    canonical_vid：已有合法签名身份时，明文 ROI 迁到该身份的哈希，避免切换
    cookie 丢掉迁移期间新建 ROI 的所有权。缺省则迁到 plaintext_vid 自身。
    share 有效性在存储层与 ROI 改写同一锁/事务内检查（撤销/过期/缺失均失败，
    不会当场迁移）。失败不销毁 legacy 凭据：调用方写入 svs_visitor_mig，之后
    在仍 active 的链接上先到先得认领。
    返回是否认领成功。
    """
    if not token or not plaintext_vid or plaintext_vid.startswith(_VISITOR_COOKIE_PREFIX):
        return False
    target = canonical_vid or plaintext_vid
    if not target or target.startswith(_VISITOR_COOKIE_PREFIX):
        return False
    hashed = _visitor_stored(target)
    if not hashed or hashed == plaintext_vid:
        return False
    try:
        n = share_store.rehash_plaintext_visitors(token, plaintext_vid, hashed)
    except Exception:
        return False
    return n > 0


def _set_visitor_cookie(resp, name, value):
    resp.set_cookie(
        name,
        value,
        path="/s",
        httponly=True,
        samesite="Lax",
        secure=_is_secure(),
    )


@app.before_request
def _bind_visitor():
    """/s/* 绑定访客：签名 cookie 复用；legacy 凭据可在仍有效的 token 上认领。

    已有合法 v2 时它是 canonical：明文 ROI 迁到该身份，不切换 cookie。
    无 v2 才签发 legacy 签名。当场认领失败（含 share 撤销/过期/缺失、/s、
    无匹配 ROI）时签发随机身份，并保留 svs_visitor_mig：无效链接不得立即
    全局迁移，但同一浏览器之后访问仍 active 的链接可先到先得认领。
    """
    path = request.path
    if path != "/s" and not path.startswith("/s/"):
        return
    raw = request.cookies.get(VISITOR_COOKIE)
    mig = request.cookies.get(VISITOR_MIG_COOKIE)
    vid = _parse_visitor_cookie(raw)
    legacy = _legacy_plaintext(raw) or _legacy_plaintext(mig)
    token = _share_token_from_path(path)
    g.visitor_issue_cookie = False
    g.visitor_mig_cookie = None
    g.visitor_mig_clear = False

    if legacy and token and _reclaim_legacy_visitor(token, legacy, canonical_vid=vid):
        # 已有合法 v2：保留该身份，明文 ROI 已迁到其哈希。无 v2 才签发 legacy。
        g.visitor_id = vid or legacy
        g.visitor_issue_cookie = not bool(vid)
        g.visitor_mig_clear = True
        return
    if vid:
        g.visitor_id = vid
        return
    g.visitor_id = secrets.token_urlsafe(16)
    g.visitor_issue_cookie = True
    if legacy:
        g.visitor_mig_cookie = legacy


@app.after_request
def _ensure_visitor_cookie(resp):
    """补发签名访客 cookie。当场认领失败则写入/保留 mig；认领成功才清除。"""
    if getattr(g, "visitor_issue_cookie", False) and getattr(g, "visitor_id", None):
        _set_visitor_cookie(resp, VISITOR_COOKIE, _sign_visitor(g.visitor_id))
    if getattr(g, "visitor_mig_clear", False):
        resp.delete_cookie(VISITOR_MIG_COOKIE, path="/s")
    elif getattr(g, "visitor_mig_cookie", None):
        _set_visitor_cookie(resp, VISITOR_MIG_COOKIE, g.visitor_mig_cookie)
    return resp


def _roi_owned_by(r, visitor):
    """判断某 roi 是否归当前访客所有（用于编辑/删除的归属校验）。

    无 visitor 字段 = 旧数据：按链接级共享，任意访客可编辑（兼容历史行为）。
    新数据存 HMAC 哈希，与当前签名 cookie 的 id 哈希比对。
    """
    v = r.get("visitor") or ""
    if not v:
        return True
    if not visitor:
        return False
    if v.startswith(_VISITOR_STORED_PREFIX):
        return hmac.compare_digest(v, _visitor_stored(visitor))
    # 旧明文 visitor：仅当签名 cookie 的 id 与明文一致（无法用复制的明文伪造签名）
    return hmac.compare_digest(v, visitor)


def _public_roi(r):
    """分享端响应：去掉原始/哈希 visitor，避免身份被复制冒用。"""
    out = dict(r)
    out.pop("visitor", None)
    return out


def _require_share(token):
    """校验 token 有效，返回 share dict；无效则 404（不泄露信息）。"""
    share = share_store.get_share(token)
    if share is None:
        abort(404, "链接无效或已过期")
    return share


def _fmt_mm(v):
    """把 mm 数值格式化为整数优先、否则一位小数（6 → "6"，6.5 → "6.5"）。"""
    f = float(v)
    if f == int(f):
        return str(int(f))
    # 6.5 这类保留一位
    return ("%.1f" % f).rstrip("0").rstrip(".")


def _require_slide(share, name):
    """校验 name 属于该 share 且通过文件名校验；否则 403/404。"""
    safe = _sanitize_name(name)
    if not safe or safe != name:
        abort(403, "无权访问")
    if safe not in share.get("slides", []):
        abort(403, "无权访问")
    return safe


def _share_has_annotate(share):
    """判断分享是否含 annotate 权限（docs §5.4 权限三档）。

    旧分享无 permissions 字段 → 默认 view+annotate（严格等价旧行为）。
    """
    perms = share.get("permissions")
    if not isinstance(perms, list) or not perms:
        return True  # 旧链接默认可标注
    return share_store.PERMISSION_ANNOTATE in perms


# --------------------------------------------------------------------------- #
# Stage 3c-2：分享访问日志 + 归档只读（docs §5.4/§v1.5）
# --------------------------------------------------------------------------- #
# 分享访问日志：/s/<token> 页面加载与 /s/<token>/api/* 关键调用记 audit
# （action=share.access，detail 带 visitor 与 IP 后两段脱敏）。
# 为防每请求一条太密，按「同 token + 同 visitor」做 5 分钟窗口去重：内存 dict
# {token+"|"+visitor: last_ts}，窗口内同键不再重复记。简单可靠，无需持久化状态。
_SHARE_ACCESS_WINDOW = 300.0
_share_access_last: dict = {}
_share_access_lock = threading.Lock()


def _mask_ip(ip: str) -> str:
    """IP 脱敏：保留前两段（IPv4）或前 6 字符（IPv6），后段用 *。"""
    if not ip:
        return ""
    if ":" in ip:
        return ip[:6] + "*"
    parts = ip.split(".")
    if len(parts) == 4:
        return ".".join(parts[:2]) + ".*.*"
    return "*"


def _log_share_access(token, detail=None):
    """best-effort 记一条 share.access 审计事件（5 分钟窗口去重）。

    调用方无需 try（record_audit 自吞失败）。去重键 = token + visitor。
    """
    visitor = _visitor_id() or ""
    key = token + "|" + visitor
    now = time.time()
    with _share_access_lock:
        last = _share_access_last.get(key)
        if last is not None and (now - last) < _SHARE_ACCESS_WINDOW:
            return
        _share_access_last[key] = now
    d = dict(detail or {})
    d["visitor"] = visitor[:8] if visitor else ""
    d["ip"] = _mask_ip(request.remote_addr or "")
    share_store.record_audit(
        action="share.access", target_type="share", target_id=token,
        detail=d,
    )


def _archived_slide_names():
    """属于归档项目的切片名集合（归档只读判定）。"""
    return share_store.archived_slide_names()


def _reject_archived_slide(share, name):
    """归档项目内切片只读：guest 亦不可在归档项目内标注/评论。命中 → True。"""
    return name in _archived_slide_names()


# --------------------------------------------------------------------------- #
# 路由
# --------------------------------------------------------------------------- #
@app.errorhandler(404)
def _handle_404(e):
    return "链接无效或已过期", 404


@app.errorhandler(403)
def _handle_403(e):
    return "无权访问", 403


@app.route("/")
def index():
    return "链接无效或已过期", 404


@app.route("/s/")
@app.route("/s")
def s_root():
    return "链接无效或已过期", 404


@app.route("/s/<token>")
def share_page(token):
    share = _require_share(token)
    _log_share_access(token, detail={"via": "page"})
    return render_template("share.html", token=token)


@app.route("/s/<token>/api/slides")
def share_slides(token):
    share = _require_share(token)
    # 一次性取 slide_meta，减少锁竞争
    all_meta = share_store.get_all_slide_meta()
    items = []
    for name in share["slides"]:
        safe = _sanitize_name(name)
        path = UPLOAD_DIR / safe
        info = {"name": safe, "exists": path.is_file()}
        sm = all_meta.get(safe, {})
        info["alias"] = sm.get("alias", "")
        info["note"] = sm.get("note", "")
        if path.is_file():
            def _read_meta():
                entry = _get_slide(safe)
                with slide_cache.borrow_pair(entry) as pair:
                    return _read_metadata(pair["osr"], path)
            try:
                meta = slide_cache.cached_read_metadata(safe, path, _read_meta)
                info.update(meta)
            except Exception as e:
                info.update({
                    "width": None, "height": None,
                    "mpp_x": None, "mpp_y": None,
                    "mpp_source": "missing",
                    "error": str(getattr(e, "description", e)),
                })
        else:
            info.update({
                "width": None, "height": None,
                "mpp_x": None, "mpp_y": None,
                "mpp_source": "missing",
                "error": "文件不存在",
            })
        items.append(info)
    return jsonify(items)


@app.route("/s/<token>/api/config")
def share_config(token):
    """返回本次分享的配置（矩形标记允许的尺寸子集）。

    先 _require_share：无效 token → 404，不泄露信息。
    旧分享无 roi_sizes 字段时默认两者皆可。
    """
    share = _require_share(token)
    return jsonify({"roi_sizes": share.get("roi_sizes") or list(share_store.DEFAULT_ROI_SIZES)})


@app.route("/s/<token>/api/slide/<name>.dzi")
def share_slide_dzi(token, name):
    share = _require_share(token)
    safe = _require_slide(share, name)
    entry = _get_slide(safe)
    with slide_cache.borrow_pair(entry) as pair:
        dz = pair["dz"]
        width, height = dz.level_dimensions[-1]

    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<Image xmlns="http://schemas.microsoft.com/deepzoom/2008" '
        f'Url="/s/{token}/api/slide/{safe}_files/" Format="jpeg" '
        f'Overlap="{DZ_OVERLAP}" TileSize="{DZ_TILE_SIZE}">'
        f'<Size Width="{width}" Height="{height}"/>'
        "</Image>"
    )
    resp = Response(xml, mimetype="application/xml")
    # DZI 元数据短期可变（重传/换切片后尺寸会变），用短缓存
    resp.headers["Cache-Control"] = "max-age=60"
    return resp


@app.route("/s/<token>/api/slide/<name>_files/<int:level>/<int:x>_<int:y>.jpeg")
def share_slide_tile(token, name, level, x, y):
    """返回 Deep Zoom 单张瓦片 JPEG（512×512、baseline、q82，带 LRU+TTL 缓存）。"""
    share = _require_share(token)
    safe = _require_slide(share, name)

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
        # 模糊→清晰的渐进预览已由查看器 base-thumb 底图层负责，瓦片无需 progressive
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


@app.route("/s/<token>/api/slide/<name>/crop")
def share_slide_crop(token, name):
    """crop PNG 导出（分享端）。

    P0-A §3.5：与主站 /api/slide/<name>/crop 共用 crop_guard（同一实现，
    防两份逻辑漂移）。read_region 之前按 clamp 后实际 size2² 过三道闸：
    像素硬闸 413 / 每分钟像素预算 429（按 share capability 即 token 计）/
    并发闸 429；任何解码前拒绝。分享端无需登录，闸主体是 token。
    """
    share = _require_share(token)
    safe = _require_slide(share, name)
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
        x2 = min(x, max(0, width - 1))
        y2 = min(y, max(0, height - 1))
        max_w = max(0, width - x2)
        max_h = max(0, height - y2)
        size2 = min(size, max_w, max_h)
        if size2 <= 0:
            return jsonify(error="裁剪区域超出图像边界"), 400
        # 像素硬闸：clamp 后实际值，任何解码前拒绝（docs §3.5）
        try:
            crop_guard.check_pixel_limit(size2, size2)
        except crop_guard.CropTooLargeError as e:
            return jsonify(error=str(e), code=e.code,
                           max_pixels=crop_guard.CROP_MAX_PIXELS), 413
        # 每分钟像素预算（按 share capability）+ 并发闸：read_region 前
        allowed, retry_after = crop_guard.admit_pixels(token, size2 * size2)
        if not allowed:
            resp = jsonify(error="crop 请求过于频繁，请稍后重试",
                           code="crop_rate_limited", retry_after=retry_after)
            resp.headers["Retry-After"] = str(int(max(1, retry_after)))
            return resp, 429
        slot = crop_guard.acquire_slot()
        if slot is None:
            return jsonify(error="crop 并发已达上限，请稍后重试",
                           code="crop_busy"), 429
        try:
            region = osr.read_region((x2, y2), 0, (size2, size2)).convert("RGB")
        finally:
            crop_guard.release_slot(slot)

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


@app.route("/s/<token>/api/slide/<name>/thumbnail")
def share_slide_thumbnail(token, name):
    """返回缩略图 JPEG（用作查看器底图预览，慢网下避免瓦片未到区域变白）。"""
    share = _require_share(token)
    safe = _require_slide(share, name)
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


@app.route("/s/<token>/api/roi", methods=["POST"])
def share_roi_add(token):
    share = _require_share(token)
    _log_share_access(token, detail={"via": "api_roi_add"})
    # 权限三档（docs §5.4）：无 annotate 权限的 token 写标注 403。
    # 旧链接无 permissions 字段 → 默认含 annotate（_share_has_annotate 兼容）。
    if not _share_has_annotate(share):
        return jsonify(error="该链接不允许标注"), 403
    body = request.get_json(silent=True) or {}
    slide = body.get("slide")
    label = body.get("label")
    typ = body.get("type", "rect")

    # label 必填：去空白后非空
    if not isinstance(label, str) or not label.strip():
        return jsonify(error="请填写用户名或标签"), 400

    if typ not in share_store.ROI_TYPES:
        return jsonify(error="未知标注类型"), 400

    if not slide:
        return jsonify(error="缺少 slide"), 400
    safe = _sanitize_name(slide)
    if not safe or safe != slide or safe not in share.get("slides", []):
        return jsonify(error="slide 不属于该分享"), 403
    # Stage 3c-2（docs §v1.5）：归档项目内切片只读，guest 亦不可标注
    if _reject_archived_slide(share, safe):
        return jsonify(error="切片已归档只读"), 403

    # 收集几何字段透传给 store 校验
    geom = {}
    for k in ("x", "y", "side_px", "size_mm", "x1", "y1", "x2", "y2", "points"):
        if k in body:
            geom[k] = body[k]

    # rect（含未指定默认 rect）需校验 size_mm ∈ 本次分享允许的尺寸子集；
    # arrow / freehand 不受限。
    if typ == "rect":
        allowed = share.get("roi_sizes") or list(share_store.DEFAULT_ROI_SIZES)
        size_mm_v = geom.get("size_mm")
        try:
            smm = float(size_mm_v)
        except (TypeError, ValueError):
            smm = None
        if smm is None or smm not in allowed:
            # 允许值拼接到友好的提示（如 "6 / 6.5"）
            label_str = " / ".join(_fmt_mm(v) for v in allowed)
            return jsonify(error="本次分享仅允许 " + label_str + " mm 标记"), 403

    # note 可选（备注文本），透传给 store 校验/清洗
    note = body.get("note", "")

    try:
        roi = share_store.add_roi(
            token, safe, label, type=typ, note=note,
            visitor=_visitor_stored(_visitor_id()), **geom
        )
    except ValueError as e:
        return jsonify(error=str(e)), 400
    return jsonify(ok=True, index=roi["index"])


@app.route("/s/<token>/api/roi/<int:index>", methods=["PATCH"])
def share_roi_update(token, index):
    """编辑本 token 的标注几何与/或备注。

    JSON body: {"geom": {...}, "note": "..."}（两者均可缺省）。
    可选 {"expected_revision": int}（Stage 3c-1 CAS），不符 → 409 revision_conflict。
    设备归属校验：只有创建该标注的访客可编辑（_roi_owned_by）；旧数据
    （无 visitor）按链接级共享允许编辑。越权返回 403。
    调 update_roi；update 返回 False 时 404；成功返回更新后的 roi dict（含 index）。
    编辑允许自由调大小，不做 6/6.5mm 限制。
    权限三档（docs §5.4）：无 annotate 权限的 token 写标注 403（旧链接默认含 annotate）。
    """
    share = _require_share(token)
    _log_share_access(token, detail={"via": "api_roi_update"})
    if not _share_has_annotate(share):
        return jsonify(error="该链接不允许标注"), 403
    body = request.get_json(silent=True) or {}
    geom = body.get("geom")
    note = body.get("note")
    expected = body.get("expected_revision")
    if geom is None and note is None:
        return jsonify(error="缺少 geom 或 note"), 400
    r = share_store.get_roi(token, index)
    if r is None:
        return jsonify(error="选区不存在"), 404
    if not _roi_owned_by(r, _visitor_id()):
        return jsonify(error="只能编辑自己创建的标记"), 403
    if _reject_archived_slide(share, r.get("slide")):
        return jsonify(error="切片已归档只读"), 403
    try:
        updated = share_store.update_roi(
            token, index, geom=geom, note=note, expected_revision=expected)
    except share_store.RevisionConflict as e:
        return jsonify(error="revision_conflict",
                       current_revision=e.current_revision), 409
    except ValueError as e:
        return jsonify(error=str(e)), 400
    if updated is False:
        return jsonify(error="选区不存在"), 404
    return jsonify(_public_roi(updated))


@app.route("/s/<token>/api/rois")
def share_roi_list(token):
    """返回本 token 可见的全部标注（仅本分享切片内）。

    组装三类来源：
      - source="me"：本 token 且归当前访客所有的标注（本设备新建，可编辑）
      - source="admin"：管理员(admin)被公开的标注
      - source="shared"：其他用户被管理员公开的标注（含本 token 其他设备
        被公开的标注，排除已归当前访客的"me"项）
    后两类来自 list_shared_rois_for_slides(本分享切片)；本 token 其他设备未
    公开的私有标注对当前设备不可见（既不在 me 也不在 shared）。
    admin（token==ADMIN_TOKEN）为管理端特权视角，返回本 token 全部标注。
    每项的 index 沿用 list_rois 的 token+index 语义（按 token 归组）。
    """
    share = _require_share(token)
    share_slides = share.get("slides", [])
    visitor = _visitor_id()

    # 1) 本 token 且归当前访客所有的标注（含未公开，source=me）
    mine = share_store.list_rois(token)
    out = []
    for r in mine:
        if token == share_store.ADMIN_TOKEN or _roi_owned_by(r, visitor):
            rr = _public_roi(r)
            rr["source"] = "me"
            out.append(rr)

    # 2) 管理员策展公开的他人/admin 标注（排除已是 me 的条目）
    shared_all = share_store.list_shared_rois_for_slides(share_slides)
    for r in shared_all:
        if r.get("token") == token:
            # 本 token 的公开标注：仅当非 me（其他设备的 shared 标注）才作为
            # shared 只读显示；me 已在上面列出，不重复
            if _roi_owned_by(r, visitor):
                continue
        rr = _public_roi(r)
        rr["source"] = "admin" if r.get("token") == share_store.ADMIN_TOKEN else "shared"
        out.append(rr)

    # 按时间倒序
    out.sort(key=lambda x: x.get("ts", 0), reverse=True)
    return jsonify(out)


@app.route("/s/<token>/api/roi/<int:index>", methods=["DELETE"])
def share_roi_delete(token, index):
    """删除本 token 的标注；管理员标注不可由分享端删除。

    设备归属校验：只有创建该标注的访客可删除（_roi_owned_by）；旧数据
    （无 visitor）按链接级共享允许删除。越权返回 403。
    可选 body {"expected_revision": int}（Stage 3c-1 CAS），不符 → 409。
    权限三档（docs §5.4）：无 annotate 权限的 token 写标注 403（旧链接默认含 annotate）。
    """
    share = _require_share(token)
    _log_share_access(token, detail={"via": "api_roi_delete"})
    if not _share_has_annotate(share):
        return jsonify(error="该链接不允许标注"), 403
    body = request.get_json(silent=True) or {}
    expected = body.get("expected_revision")
    r = share_store.get_roi(token, index)
    if r is None:
        return jsonify(error="选区不存在"), 404
    if not _roi_owned_by(r, _visitor_id()):
        return jsonify(error="只能编辑自己创建的标记"), 403
    if _reject_archived_slide(share, r.get("slide")):
        return jsonify(error="切片已归档只读"), 403
    try:
        ok, _aid = share_store.delete_roi(token, index, expected_revision=expected)
    except share_store.RevisionConflict as e:
        return jsonify(error="revision_conflict",
                       current_revision=e.current_revision), 409
    if not ok:
        return jsonify(error="选区不存在"), 404
    return jsonify(ok=True)


# --------------------------------------------------------------------------- #
# 评论线程（guest）—— Stage 3c-1（docs §5.3）
#
# guest 经 /s/* 评论，按 annotation_id 定位（支持评论本分享内任意可见标注，含
# 管理员策展公开的标注）。校验：标注所在 slide ∈ 本次分享；评论需 share 含
# annotate 权限。author_user_id 留空（guest），author_label 取 body.name 或"访客"。
# --------------------------------------------------------------------------- #
def _resolve_anno_in_share(share, annotation_id):
    """按 annotation_id 取标注并校验其 slide 属于该 share；否则 (None, error_resp)。"""
    roi = share_store.get_roi_by_annotation_id(annotation_id)
    if roi is None:
        return None, (jsonify(error="标注不存在"), 404)
    slide = roi.get("slide")
    if not slide or slide not in share.get("slides", []):
        return None, (jsonify(error="无权访问"), 403)
    return roi, None


@app.route("/s/<token>/api/comments")
def share_comments_list(token):
    """列出某标注的评论（guest 视角，query: annotation_id）。share 有效即可查看。"""
    share = _require_share(token)
    annotation_id = request.args.get("annotation_id")
    if not annotation_id:
        return jsonify(error="缺少 annotation_id"), 400
    _roi, err = _resolve_anno_in_share(share, annotation_id)
    if err:
        return err
    return jsonify({"comments": share_store.list_comments(annotation_id=annotation_id)})


@app.route("/s/<token>/api/comments", methods=["POST"])
def share_comment_add(token):
    """在某标注下新增评论（guest）。需 share 含 annotate 权限。

    JSON: {annotation_id, body, parent_id?, name?}。
    """
    share = _require_share(token)
    _log_share_access(token, detail={"via": "api_comment_add"})
    if not _share_has_annotate(share):
        return jsonify(error="该链接不允许评论"), 403
    body = request.get_json(silent=True) or {}
    annotation_id = body.get("annotation_id")
    text = body.get("body")
    parent_id = body.get("parent_id")
    label = (body.get("name") or "").strip() or None
    if not annotation_id:
        return jsonify(error="缺少 annotation_id"), 400
    roi, err = _resolve_anno_in_share(share, annotation_id)
    if err:
        return err
    # Stage 3c-2（docs §v1.5）：归档项目内切片只读，guest 亦不可评论
    if _reject_archived_slide(share, roi.get("slide")):
        return jsonify(error="切片已归档只读"), 403
    try:
        cmt = share_store.add_comment(
            annotation_id, roi.get("slide"), token, text,
            author_user_id=None, author_label=label, parent_id=parent_id)
    except ValueError as e:
        return jsonify(error=str(e)), 400
    return jsonify(ok=True, comment=cmt)


@app.route("/static/<path:filename>")
def share_static(filename):
    return send_from_directory("static", filename)


# --------------------------------------------------------------------------- #
# 合并 WSGI 应用（模块级，供 gunicorn 直接引用 share_server:combined_app）
# --------------------------------------------------------------------------- #
# 同一端口按路径分流：
# /s/...（含 /s/<token>/ 全部分享路由）→ 分享应用；
# 其余（/、/login、/api/*、/static/*）→ 管理端应用（开启 ADMIN_PASSWORD
# 后需登录），实现"同入口不同页面"：访问 / 是管理员登录
# 门户，进 /s/<token> 是正常分享页。这样 frp 不需要为管理端另开隧道，
# 复用既有分享隧道即可。
# import app 会触发 app.py 模块级代码，可接受——两者本就要在 gunicorn
# 线程 worker 下共存（app 也作为 WSGI 对象被 gunicorn 引用）。
import app as admin_app


def combined_app(environ, start_response):
    path = environ.get("PATH_INFO") or ""
    if path == "/s" or path.startswith("/s/"):
        return app(environ, start_response)  # 分享应用
    return admin_app.app(environ, start_response)  # 管理端应用


if __name__ == "__main__":
    # 本地开发 fallback（生产由 Containerfile 直接调 gunicorn，不走 __main__）：
    # HTTPS：提供 SHARE_TLS_CERT / SHARE_TLS_KEY 时直接以 TLS 运行
    # （frp TCP 隧道只是转发，TLS 需在本服务终止，避免被备案系统按 HTTP 拦截）
    tls_cert = os.environ.get("SHARE_TLS_CERT")
    tls_key = os.environ.get("SHARE_TLS_KEY")
    ssl_context = None
    if tls_cert and tls_key and os.path.exists(tls_cert) and os.path.exists(tls_key):
        ssl_context = (tls_cert, tls_key)
        print(f"[share_server] HTTPS enabled: {tls_cert}")
    else:
        print("[share_server] WARNING: 未找到 TLS 证书，以 HTTP 运行")

    from werkzeug.serving import run_simple

    run_simple(
        "0.0.0.0",
        int(os.environ.get("SHARE_PORT", 38000)),
        combined_app,
        threaded=True,
        ssl_context=ssl_context,
    )
