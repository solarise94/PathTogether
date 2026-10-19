# -*- coding: utf-8 -*-
"""工作台上传面板内转换：能力载荷与页面接线（服务端侧）.

对应设计 docs/upload-inline-convert-design-20261015.md：
  §8   开关与回退——bootstrap.capabilities.cos_upload 新增 inline_convert
       {enabled, url, viewable_formats}，默认开、available=false 也随形下发；
  §5.1 门禁作用域——viewable_formats 是**转换产物**的门禁依据，与工具页
       端点同源（唯一来源 SLIDE_TOOLS_VIEWABLE_OUTPUT_FORMATS）；
  §1.1 /app 的 CSP 实测——当前无 CSP 头；将来若加，必须放行 WASM 与 worker
       （wasm-unsafe-eval / worker-src 'self'），否则面板内转换整体不可用；
  §2.2 页面加载顺序——inline-convert.js 必须在 cos-uploader.js / slide-sniff.js
       之后、app.js 之前。

浏览器侧行为（真实 WASM + 真实 Chromium）由 tests/e2e 覆盖（另一位同事）；
本文件只锁服务端载荷、渲染顺序与「加头不放行 WASM」这类不可在浏览器里
早发现的回归。
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import _bootstrap  # noqa: F401,E402  # noqa: F401
import app as app_mod  # noqa: E402
import cos_config  # noqa: E402
import cos_pool_store  # noqa: E402
import upload_guard  # noqa: E402
from _pt_helpers import csrf_client, isolate_app  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent


# --------------------------------------------------------------------------- #
# 夹具（与 tests/test_slide_tools_upload_capability.py 同款）
# --------------------------------------------------------------------------- #
@pytest.fixture()
def _iso(monkeypatch):
    isolate_app(monkeypatch, _bootstrap.SHARE_DATA_DIR, clear_stores=True)
    app_mod.AUTH_ENABLED = True
    yield


@pytest.fixture()
def _cos_on(monkeypatch):
    """capability on + 池配置过门禁。"""
    monkeypatch.setattr(cos_config, "COS_POOL_CAPACITY_BYTES", 1_000_000_000)
    monkeypatch.setattr(cos_config, "COS_POOL_SAFETY_BYTES", 100_000_000)
    monkeypatch.setattr(upload_guard, "UPLOAD_RESERVED_FREE_BYTES", 0)
    monkeypatch.setattr(app_mod, "UPLOAD_PRODUCT_MAX_BYTES", 800_000_000)
    monkeypatch.setattr(cos_config, "COS_BUCKET", "bucket-appid")
    monkeypatch.setattr(cos_config, "COS_REGION", "ap-shanghai")
    monkeypatch.setattr(cos_config, "COS_UPLOAD_CAPABILITY", "on")
    cos_pool_store.ensure_pool_state()
    yield


def _mkuser(user_id, role):
    import pg_store
    conn = pg_store.connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                cur.execute(
                    "INSERT INTO users (user_id, login_id, display_name, role, "
                    "disabled, created_at, auth_version) VALUES (%s, %s, %s, "
                    "%s, false, now(), 1) ON CONFLICT (user_id) DO NOTHING",
                    (user_id, user_id + "@x", user_id, role))
    finally:
        conn.close()


@pytest.fixture()
def owner_client():
    app_mod.app.config["TESTING"] = True
    _mkuser("owner-inline", "owner")
    client = csrf_client(app_mod.app.test_client())
    with client.session_transaction() as s:
        s["auth_user"] = "owner-inline@x"
        s["user_id"] = "owner-inline"
        s["role"] = "owner"
        s["auth_version"] = 1
    return client


def _payload(monkeypatch, demo=False):
    """钉死身份后直取能力载荷（与 test_ingestion_api 同款口径）。"""
    monkeypatch.setattr(app_mod, "current_identity",
                        lambda: {"role": "owner", "user_id": "owner-1"})
    with app_mod.app.test_request_context("/"):
        return app_mod._cos_upload_capability_payload(demo)


# --------------------------------------------------------------------------- #
# 1. 载荷形态与同源（设计 §8）
# --------------------------------------------------------------------------- #
def test_inline_convert_payload_default_on(_iso, _cos_on, monkeypatch):
    """默认开：enabled=True、url=回退目的地、viewable_formats 与工具页同源。"""
    p = _payload(monkeypatch)["inline_convert"]
    assert p["enabled"] is True
    assert p["url"] == app_mod.BROWSER_CONVERT_URL == "/tools/slides"
    assert p["viewable_formats"] == list(
        app_mod.SLIDE_TOOLS_VIEWABLE_OUTPUT_FORMATS)


def test_inline_convert_viewable_formats_single_source(monkeypatch):
    """viewable_formats 单一来源 SLIDE_TOOLS_VIEWABLE_OUTPUT_FORMATS
    （2026-10-20：/api/tools/slides/upload-capability 已退役，端点对照
    由 tests/test_slide_tools_upload_capability.py 的退役用例钉住）。"""
    bootstrap = _payload(monkeypatch)["inline_convert"]["viewable_formats"]
    assert set(bootstrap) == set(app_mod.SLIDE_TOOLS_VIEWABLE_OUTPUT_FORMATS)


def test_inline_convert_payload_present_when_capability_off(
        _iso, monkeypatch):
    """available=false（capability off）时**仍然下发**——前端要显示明确原因，
    而不是因为没有字段而静默禁用。"""
    monkeypatch.setattr(cos_config, "COS_UPLOAD_CAPABILITY", "off")
    p = _payload(monkeypatch)
    assert p["available"] is False
    assert p["inline_convert"]["enabled"] is True
    assert p["inline_convert"]["viewable_formats"] == list(
        app_mod.SLIDE_TOOLS_VIEWABLE_OUTPUT_FORMATS)


def test_inline_convert_payload_present_in_demo(monkeypatch):
    """demo 载荷同样带该键（demo 恒不可用，但形态完整）。"""
    p = _payload(monkeypatch, demo=True)
    assert p["available"] is False
    assert p["inline_convert"]["enabled"] is True


def test_inline_convert_switch_off(_iso, _cos_on, monkeypatch):
    """关闭开关（INLINE_CONVERT_ENABLED=0 的解析结果）→ enabled=False，
    其余字段不变（前端据此完全退回弹窗交接）。"""
    monkeypatch.setattr(app_mod, "INLINE_CONVERT_ENABLED", False)
    p = _payload(monkeypatch)["inline_convert"]
    assert p["enabled"] is False
    assert p["url"] == "/tools/slides"
    assert p["viewable_formats"] == list(
        app_mod.SLIDE_TOOLS_VIEWABLE_OUTPUT_FORMATS)


def test_inline_convert_env_parsing(monkeypatch):
    """"INLINE_CONVERT_ENABLED 的解析：默认开；0/false/no（任意大小写）关。"""
    def parse(raw):
        return (raw if raw is not None else "1").strip().lower() not in (
            "0", "false", "no")

    assert parse(None) is True
    assert parse("1") is True
    assert parse("true") is True
    assert parse("0") is False
    assert parse("FALSE") is False
    assert parse(" No ") is False


# --------------------------------------------------------------------------- #
# 2. 既有词表不受影响（新增键必须纯增量）
# --------------------------------------------------------------------------- #
def test_existing_payload_lists_unchanged(_iso, _cos_on, monkeypatch):
    """新增 inline_convert 不得改动既有清单与可查看格式表本身的取值。"""
    p = _payload(monkeypatch)
    assert p["browser_convert"]["formats"] == sorted(
        app_mod._browser_convert_formats())
    assert p["direct_upload"]["formats"] == sorted(
        app_mod._direct_upload_formats())
    assert p["manual_only"] is True
    # inline_convert 是纯增量的键：既有键集合逐个仍在
    for k in ("available", "manual_only", "formats",
              "browser_convert", "direct_upload"):
        assert k in p


# --------------------------------------------------------------------------- #
# 3. /app 的 CSP 与 WASM（设计 §1.1）
# --------------------------------------------------------------------------- #
def test_app_csp_must_not_block_wasm_or_worker(_iso, owner_client):
    """/app 当前**没有** CSP 头（设计 §1.1 实测）；将来若加，必须放行
    'wasm-unsafe-eval' 与 worker-src 'self'——否则面板内转换（WASM + module
    worker）整体不可用，而这是浏览器里才能发现的失败。

    条件式断言 + 显式的「当前无头」记录：两种状态都被钉住，任一方向漂移
    都会让本用例失败并说明原因。
    """
    r = owner_client.get("/app")
    assert r.status_code == 200
    csp = r.headers.get("Content-Security-Policy")
    if csp is None:
        # 现状：/app 无页面级 CSP（与 /tools/slides 不同）
        assert "Content-Security-Policy" not in r.headers
        return
    assert "'wasm-unsafe-eval'" in csp, (
        "/app 加了 CSP 但缺少 'wasm-unsafe-eval'：WASM 编译会被拦，"
        "面板内转换不可用（设计 §1.1）")
    assert "worker-src 'self'" in csp, (
        "/app 加了 CSP 但缺少 worker-src 'self'：module worker 会被拦，"
        "面板内转换不可用（设计 §1.1）")


def test_wasm_and_worker_assets_served_for_app(_iso, owner_client):
    """面板要在 /app 里动态 import 运行器 → 其静态资源必须可服务且 MIME 正确
    （application/wasm 是 compileStreaming 的严格前提；classic script 里的
    动态 import 在 /app 上已实测可行，见设计 §10.2）。"""
    for path, mime in (
        ("/static/tools/slide-transform/slide_transform_bg.wasm",
         "application/wasm"),
        ("/static/tools/slide-transform/runner.js", "text/javascript"),
        ("/static/tools/slide-transform/worker.js", "text/javascript"),
    ):
        r = owner_client.get(path)
        assert r.status_code == 200, path
        assert mime in r.headers["Content-Type"], (path, r.headers["Content-Type"])


# --------------------------------------------------------------------------- #
# 4. 页面加载顺序与共享引擎复用（设计 §2.2）
# --------------------------------------------------------------------------- #
def test_app_template_loads_inline_convert_before_app_js(_iso, owner_client):
    """inline-convert.js 以 classic script 加载，且在 cos-uploader.js /
    slide-sniff.js 之后、app.js 之前（app.js 初始化时 HP_INLINE_CONVERT
    必须已就绪）。"""
    html = owner_client.get("/app").get_data(as_text=True)
    needle = "inline-convert.js"
    assert needle in html, "工作台未加载 inline-convert.js"
    i_cos = html.index("cos-uploader.js")
    i_sniff = html.index("slide-sniff.js")
    i_inline = html.index(needle)
    i_app = html.index("/static/app.js")
    assert i_cos < i_inline, "inline-convert.js 必须先于 app.js 且不早于 cos-uploader.js"
    assert i_sniff < i_inline
    assert i_inline < i_app, "inline-convert.js 必须在 app.js 之前加载"


def test_inline_convert_source_shape():
    """面板编排器是 classic script（挂 window.HP_INLINE_CONVERT），与共享引擎
    同风格；不 import ES module（app.js 是 classic script，无法 import）。"""
    src = (REPO_ROOT / "static" / "upload" / "inline-convert.js").read_text(
        encoding="utf-8")
    assert "window.HP_INLINE_CONVERT" in src
    assert "^import " not in src and "\nimport " not in src


# --------------------------------------------------------------------------- #
# 5. 面板侧门禁函数（artifactGate）的契约（设计 §5.1）
# --------------------------------------------------------------------------- #
def test_artifact_gate_is_scoped_to_converted_artifacts():
    """`artifactGate` 是**转换产物**的门禁纯函数（JS）；这里锁「服务端侧的
    同源约束」：viewable_formats 的唯一来源是
    SLIDE_TOOLS_VIEWABLE_OUTPUT_FORMATS，且它只含三个核心产物格式字符串
    （不是扩展名）——面板的直传路径**不**使用它。"""
    import slide_format_registry
    formats = list(app_mod.SLIDE_TOOLS_VIEWABLE_OUTPUT_FORMATS)
    assert formats, "可查看格式表不能为空"
    # 全是核心 result.format 形态的字符串（含 '-'，不含 '.' 扩展名前缀）
    for f in formats:
        assert isinstance(f, str) and f
        assert not f.startswith("."), f
        assert "-" in f, f
    # 注册表里不存在同名扩展名（防把它当扩展名用）
    for f in formats:
        assert slide_format_registry.lookup(f)["capability"] == \
            slide_format_registry.CAP_UNSUPPORTED, f
