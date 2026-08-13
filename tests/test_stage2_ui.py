# -*- coding: utf-8 -*-
"""HistoPilot UI 模块化（Stage 2 第一阶段：同源独立 bundle）验收测试。

覆盖（对应设计文档 §10 Stage 2 验收 + 任务书第 4 节）：
  (a) HISTOPILOT_UI_ENABLED 默认开启：GET / 200 且含 /plugins/histopilot/ui/ 脚本标签、
      host-bridge.js、ai-btn、#ai-panel；
  (b) HISTOPILOT_UI_ENABLED=0：GET / 200，不含插件脚本、不含 host-bridge.js、
      不含 ai-btn / #ai-panel（平台前端静默降级，人工读片可用）；
  (c) 插件资源路由 GET /plugins/histopilot/ui/<filename>：.js/.css 200、非 js/css 403、
      路径穿越被拒（非 200，不泄露 /etc/passwd）、不存在 .js 404；
  (d) 静态守护：plugins/histopilot/ui/*.js 不得出现平台私有标识符（OpenSeadragon /
      window.viewer / state.slide / annoPanelList / getElementById("anno 等）；app.js 不再
      含已搬走的 pumpAiSse / handleAiEvent / loadAiConfig 等函数定义。

隔离方式参考现有测试：SHARE_DATA_DIR/UPLOAD_DIR 临时目录、openslide stub、
monkeypatch 关闭 AUTH_ENABLED。

运行：cd 项目根 && python3 -m pytest tests/test_stage2_ui.py -q
"""
import os
import re
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TMP = tempfile.mkdtemp(prefix="svs-stage2-")
os.environ["SHARE_DATA_DIR"] = os.path.join(TMP, "share-data")
os.makedirs(os.environ["SHARE_DATA_DIR"], exist_ok=True)
os.environ["UPLOAD_DIR"] = os.path.join(TMP, "uploads")
os.makedirs(os.environ["UPLOAD_DIR"], exist_ok=True)

# openslide 未安装时 stub（本测试不需要真 OpenSlide）
try:
    import openslide  # noqa: F401
except ImportError:
    import types as _types
    _os = _types.ModuleType("openslide")
    _os.OpenSlide = object
    sys.modules["openslide"] = _os
    _dz = _types.ModuleType("openslide.deepzoom")
    _dz.DeepZoomGenerator = object
    sys.modules["openslide.deepzoom"] = _dz

import app as app_mod  # noqa: E402
import pytest  # noqa: E402

from pathlib import Path  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
PLUGIN_UI_DIR = REPO_ROOT / "plugins" / "histopilot" / "ui"
APP_JS = REPO_ROOT / "static" / "app.js"


@pytest.fixture(autouse=True)
def _isolation(monkeypatch):
    """每个用例前：关闭管理员认证、把 HISTOPILOT_UI_ENABLED 复位为默认开启。"""
    monkeypatch.setattr(app_mod, "AUTH_ENABLED", False)
    monkeypatch.delenv("HISTOPILOT_UI_ENABLED", raising=False)
    yield


def _client():
    app_mod.app.config["TESTING"] = True
    return app_mod.app.test_client()


# =========================================================================== #
# (a) 默认开启
# =========================================================================== #
def test_index_renders_plugin_bundle_when_enabled():
    r = _client().get("/")
    assert r.status_code == 200
    body = r.get_data(as_text=True)
    # 插件 bundle 脚本标签
    assert "/plugins/histopilot/ui/bridge-client.js" in body
    assert "/plugins/histopilot/ui/main.js" in body
    # host 端
    assert "/static/host-bridge.js" in body
    # AI 触发点与面板 DOM 渲染
    assert 'id="ai-btn"' in body
    assert 'id="ai-panel"' in body
    assert 'id="tbb-more-ai"' in body


# =========================================================================== #
# (b) flag 关闭：静默降级
# =========================================================================== #
def test_index_hides_plugin_when_disabled(monkeypatch):
    monkeypatch.setenv("HISTOPILOT_UI_ENABLED", "0")
    r = _client().get("/")
    assert r.status_code == 200
    body = r.get_data(as_text=True)
    assert "/plugins/histopilot/ui/" not in body
    assert "/static/host-bridge.js" not in body
    assert 'id="ai-btn"' not in body
    assert 'id="ai-panel"' not in body
    assert 'id="tbb-more-ai"' not in body


def test_flag_helper_reads_env(monkeypatch):
    assert app_mod.histopilot_ui_enabled() is True
    monkeypatch.setenv("HISTOPILOT_UI_ENABLED", "0")
    assert app_mod.histopilot_ui_enabled() is False
    monkeypatch.setenv("HISTOPILOT_UI_ENABLED", "1")
    assert app_mod.histopilot_ui_enabled() is True


# =========================================================================== #
# (c) 插件资源路由
# =========================================================================== #
def test_plugin_asset_serves_js():
    r = _client().get("/plugins/histopilot/ui/main.js")
    assert r.status_code == 200
    assert "javascript" in (r.headers.get("Content-Type") or "").lower() or r.data


def test_plugin_asset_serves_css_allowed():
    # 没有 .css 时仅校验扩展名放行（不要求文件存在）：放行后 404 也算"非 403"
    r = _client().get("/plugins/histopilot/ui/does-not-exist.css")
    assert r.status_code in (200, 404)


def test_plugin_asset_rejects_non_js_css():
    r = _client().get("/plugins/histopilot/ui/README.txt")
    assert r.status_code == 403


def test_plugin_asset_rejects_nonexistent_js():
    r = _client().get("/plugins/histopilot/ui/nope.js")
    assert r.status_code == 404


def test_plugin_asset_rejects_path_traversal():
    # send_from_directory/safe_join 拒绝 .. ；不得返回 200 或泄露系统文件
    for path in [
        "/plugins/histopilot/ui/../../../../etc/passwd",
        "/plugins/histopilot/ui/%2e%2e%2f%2e%2e%2fetc%2fpasswd",
    ]:
        r = _client().get(path)
        assert r.status_code != 200
        body = r.get_data(as_text=True)
        assert "root:" not in body  # 绝不泄露 /etc/passwd


def test_plugin_asset_disabled_when_flag_off(monkeypatch):
    monkeypatch.setenv("HISTOPILOT_UI_ENABLED", "0")
    r = _client().get("/plugins/histopilot/ui/main.js")
    assert r.status_code == 404


# =========================================================================== #
# (d) 静态守护：插件代码不读取平台私有 state/viewer/DOM selector
# =========================================================================== #
# 禁止模式清单 —— 依据 Stage 2 验收"插件代码不读取平台全局 state/viewer/DOM selector"。
# 插件应通过 HostBridge（slide.getCurrent/selection.getBbox/viewer.navigate/
# viewer.highlight）获取这些能力，而非直接读平台全局。
_FORBIDDEN_IN_PLUGIN = [
    ("OpenSeadragon", "不得直接使用平台查看器库（viewer 经 HostBridge 暴露）"),
    ("window.viewer", "不得持有平台 OpenSeadragon 实例"),
    ("viewer.viewport", "不得操作平台 viewport（改发 viewer.navigate 请求）"),
    ("state.slide", "不得读平台全局 state（改用 HostBridge slide.getCurrent/slide.opened）"),
    ("state.roi", "不得读平台选区 state（改用 selection.getBbox 请求）"),
    ("editItem", "不得读平台编辑态（选区经 selection.getBbox 提供）"),
    ("annoPanelList", "不得触碰平台标注面板 DOM（fork 经 fork.open anchorEl 提供）"),
    ('getElementById("anno', "不得用平台私有 anno-* 选择器（仅可查 ai-*/fork-chat-*）"),
    ("getElementById('anno", "不得用平台私有 anno-* 选择器（单引号变体）"),
    ("redrawAnnoCanvas", "不得直接触发平台画布重绘（改发 viewer.highlight 请求）"),
]


def _all_plugin_js_text():
    files = sorted(PLUGIN_UI_DIR.glob("*.js"))
    assert files, "plugins/histopilot/ui/*.js 不存在"
    return "\n".join(f.read_text(encoding="utf-8") for f in files)


def _strip_js_comments(text):
    r"""剥离 JS 注释后再做禁止模式匹配：文档性注释（说明"不再做 X"）允许保留，
    只检测真正的代码引用。移除块注释 /*...*/ 与整行 // 注释；保留代码与行尾注释
    中的代码部分（禁止模式出现在代码里仍会被命中）。"""
    text = re.sub(r"/\*[\s\S]*?\*/", "", text)
    out = []
    for line in text.split("\n"):
        out.append("" if re.match(r"^\s*//", line) else line)
    return "\n".join(out)


def test_plugin_js_has_no_forbidden_platform_symbols():
    text = _strip_js_comments(_all_plugin_js_text())
    leaks = []
    for pat, reason in _FORBIDDEN_IN_PLUGIN:
        if pat in text:
            leaks.append((pat, reason))
    assert not leaks, "插件 bundle 命中禁止模式（应只经 HostBridge 访问平台能力）：%r" % leaks


def test_plugin_js_uses_hostbridge_for_platform_capabilities():
    text = _all_plugin_js_text()
    # 正向：插件确实通过 HostBridge 与平台通信
    assert "viewer.highlight" in text
    assert "HostBridgeHost" in text
    assert "slide.opened" in text
    assert "selection.getBbox" in text


def test_plugin_js_exposes_single_namespace():
    text = _all_plugin_js_text()
    # 对外只暴露 window.HistoPilot（bridge-client.js 建命名空间）
    assert "window.HistoPilot" in text


def test_appjs_no_longer_contains_moved_ai_functions():
    text = APP_JS.read_text(encoding="utf-8")
    # app.js 收缩为平台 viewer + HostBridge host 适配层：以下函数定义应已迁入插件 bundle
    moved = ["pumpAiSse", "handleAiEvent", "handleSseFrame", "loadAiConfig",
             "startAiRun", "renderAiTranscript", "resetAiForSlide", "restoreAiSession",
             "openForkChat", "handleForkEvent", "renderAiConfigState"]
    leaked = [fn for fn in moved if ("function " + fn) in text]
    assert not leaked, "app.js 仍含已搬走的 AI 函数定义：%r" % leaked


def test_appjs_registers_hostbridge_handlers():
    text = APP_JS.read_text(encoding="utf-8")
    # 平台侧实现了 host 端能力
    assert "registerHostBridgeHandlers" in text
    assert 'onRequest("viewer.highlight"' in text
    assert 'onRequest("selection.getBbox"' in text
    assert 'onEvent("annotation.changed"' in text


def test_containerfile_ships_plugin_bundle():
    """Containerfile 必须 COPY plugins/，否则 demo/生产镜像缺插件 bundle（路由 404）。"""
    cf = REPO_ROOT / "Containerfile"
    text = cf.read_text(encoding="utf-8")
    assert "COPY plugins/ plugins/" in text, "Containerfile 未 COPY plugins/ 目录"


def test_containerfile_ships_app_modules():
    """app.py import 的仓库内模块必须全部进镜像（否则 gunicorn worker 起不来）。

    Stage 3a-1 曾漏 COPY user_store.py → demo 重建后 ModuleNotFoundError。
    静态扫描 app.py 顶层 import，凡 repo 根有同名 .py 且非 tests/ 的都必须出现
    在 Containerfile 的 COPY 行里。
    """
    cf = (REPO_ROOT / "Containerfile").read_text(encoding="utf-8")
    src = (REPO_ROOT / "app.py").read_text(encoding="utf-8")
    mods = set(re.findall(r"^(?:import|from)\s+([a-zA-Z_][\w]*)", src, re.M))
    missing = []
    for m in sorted(mods):
        if (REPO_ROOT / (m + ".py")).is_file() and m != "app":
            if not re.search(r"^COPY .*\b{}\.py\b".format(re.escape(m)), cf, re.M):
                missing.append(m)
    assert not missing, "Containerfile 未 COPY 这些 app.py 依赖模块：%r" % missing


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
