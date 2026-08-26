# -*- coding: utf-8 -*-
"""Stage 5-2：sample-annotator 示例插件 manifest 校验 + 平台加载/路由测试。

覆盖（对应任务书第 4 节）：
  (a) plugins/sample-annotator/manifest.json 通过 validate_manifest + negotiate_versions
      不抛（service.baseUrl 填 "/" 占位以满足非空校验，README 已说明）；
  (b) SAMPLE_PLUGIN_ENABLED 未设时：index 不含 sample-annotator 脚本；
      /plugins/sample-annotator/ui/main.js 仍可服务（静态文件始终可服务，仅 index.html
      注入受 flag 控制——推荐方案）；
  (c) SAMPLE_PLUGIN_ENABLED=1 且 manifest 存在：index 注入 SVS_PLUGIN_PERMISSIONS
      与 main.js 脚本标签；
  (d) 路径穿越：/plugins/../app.py 等 → 非 200、不泄露源码；
  (e) 通用插件路由：非 .js/.css 扩展名 403、不存在 .js 404。

运行：cd 项目根 && python3 -m pytest tests/test_sample_plugin.py -q
"""
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import _bootstrap  # noqa: E402,F401  # session 目录+openslide stub（conftest 先行）
UPLOAD_DIR = _bootstrap.UPLOAD_DIR
from plugins.sdk import manifest as M  # noqa: E402
import app as app_mod  # noqa: E402
import pytest  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
SAMPLE_MANIFEST = REPO_ROOT / "plugins" / "sample-annotator" / "manifest.json"


@pytest.fixture(autouse=True)
def _isolation(monkeypatch):
    """每用例前：关闭认证、清空 SAMPLE_PLUGIN_ENABLED / HISTOPILOT_UI_ENABLED。"""
    monkeypatch.setattr(app_mod, "AUTH_ENABLED", False)
    monkeypatch.delenv("SAMPLE_PLUGIN_ENABLED", raising=False)
    monkeypatch.delenv("HISTOPILOT_UI_ENABLED", raising=False)
    yield


def _client():
    app_mod.app.config["TESTING"] = True
    return app_mod.app.test_client()


# =========================================================================== #
# (a) manifest 校验 / 版本协商
# =========================================================================== #
def test_sample_manifest_validates():
    data = json.loads(SAMPLE_MANIFEST.read_text(encoding="utf-8"))
    assert M.validate_manifest(data) == [], M.validate_manifest(data)


def test_sample_manifest_negotiates():
    data = json.loads(SAMPLE_MANIFEST.read_text(encoding="utf-8"))
    res = M.negotiate_versions(data)
    assert res["id"] == "sample-annotator"
    assert res["pluginContractVersion"] == "1.0.0"
    assert res["bridgeProtocolVersion"] == "1.0.0"


def test_sample_manifest_permissions_declared():
    """三套工作按钮所需权限已声明；annotation:read 未声明（供越权演示）。"""
    data = json.loads(SAMPLE_MANIFEST.read_text(encoding="utf-8"))
    perms = data["permissions"]
    for needed in ("slide:metadata:read", "viewer:navigate", "annotation:write"):
        assert needed in perms, "缺权限 %s" % needed
    assert "annotation:read" not in perms  # 越权演示依据


# =========================================================================== #
# (b) 默认关闭：index 不含插件脚本；静态文件仍可服务
# =========================================================================== #
def test_index_hides_sample_when_flag_off():
    r = _client().get("/")
    assert r.status_code == 200
    body = r.get_data(as_text=True)
    assert "/plugins/sample-annotator/ui/main.js" not in body
    assert "SVS_PLUGIN_PERMISSIONS" not in body


def test_sample_asset_served_even_when_flag_off():
    """静态文件始终可服务，仅 index.html 注入受 SAMPLE_PLUGIN_ENABLED 控制。"""
    r = _client().get("/plugins/sample-annotator/ui/main.js")
    assert r.status_code == 200
    assert r.data


# =========================================================================== #
# (c) 开启：注入权限表 + 脚本
# =========================================================================== #
def test_index_injects_sample_when_enabled(monkeypatch):
    monkeypatch.setenv("SAMPLE_PLUGIN_ENABLED", "1")
    r = _client().get("/")
    assert r.status_code == 200
    body = r.get_data(as_text=True)
    assert "/plugins/sample-annotator/ui/main.js" in body
    assert "/plugins/sdk/ui/bridge-client.js" in body
    # 权限表：sample-annotator 键存在且含声明权限
    assert "SVS_PLUGIN_PERMISSIONS" in body
    assert "sample-annotator" in body
    assert "slide:metadata:read" in body
    # histopilot 不在权限表内（内置特权插件，host 端全放行）
    assert "SVS_PLUGIN_PERMISSIONS = {\"sample-annotator\"" in body


def test_sample_context_flag_and_permissions(monkeypatch):
    assert app_mod.sample_plugin_context()["enabled"] is False
    monkeypatch.setenv("SAMPLE_PLUGIN_ENABLED", "1")
    ctx = app_mod.sample_plugin_context()
    assert ctx["enabled"] is True
    assert "annotation:write" in ctx["permissions"]


# =========================================================================== #
# (d) 路径穿越
# =========================================================================== #
def test_plugin_route_rejects_path_traversal():
    for path in [
        "/plugins/../app.py",
        "/plugins/%2e%2e/app.py",
        "/plugins/histopilot/ui/../../../../etc/passwd",
        "/plugins/sample-annotator/ui/../../../../etc/passwd",
        "/plugins/..%2fsample-annotator/ui/main.js",
    ]:
        r = _client().get(path)
        assert r.status_code != 200, path
        body = r.get_data(as_text=True)
        assert "def index()" not in body  # 不泄露 app.py
        assert "root:" not in body        # 不泄露 /etc/passwd


# =========================================================================== #
# (e) 通用插件路由行为
# =========================================================================== #
def test_plugin_asset_rejects_non_js_css():
    r = _client().get("/plugins/sample-annotator/ui/README.txt")
    assert r.status_code == 403


# =========================================================================== #
# (f) SDK 静态资产与示例插件独立页（demo 烟雾发现的 404/403 回归守卫）
# =========================================================================== #
def test_sdk_bridge_client_served():
    """SDK 浏览器端必须经通用路由可达：plugins/sdk/ui/bridge-client.js。

    Stage 5-2 初版把它放 plugins/sdk/bridge-client.js（无 ui/ 层），通用路由
    /plugins/<id>/ui/<file> 匹配不到 → demo 实测 404，示例插件面板引导崩。"""
    r = _client().get("/plugins/sdk/ui/bridge-client.js")
    assert r.status_code == 200
    assert b"createPluginBridge" in r.data


def test_sample_standalone_page_served():
    """示例插件独立页（manifest ui.entry）必须可服务（.html 在允许扩展名内）。

    Stage 5-2 初版扩展名白名单仅 .js/.css → 独立页 403。"""
    r = _client().get("/plugins/sample-annotator/ui/index.html")
    assert r.status_code == 200
    assert b"sa-panel" in r.data


def test_plugin_asset_rejects_nonexistent_js():
    r = _client().get("/plugins/sample-annotator/ui/nope.js")
    assert r.status_code == 404


def test_plugin_asset_rejects_unknown_plugin_dir():
    r = _client().get("/plugins/does-not-exist/ui/main.js")
    assert r.status_code == 404


def test_plugin_route_rejects_plugin_id_traversal():
    # plugin_id 段含 ".." 或路径分隔符 → 404（_plugin_ui_dir 拒绝，不落入 send_from_directory）
    for pid in ("../static", "..%2fstatic", "a/b"):
        r = _client().get("/plugins/%s/ui/main.js" % pid)
        assert r.status_code == 404, pid


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
