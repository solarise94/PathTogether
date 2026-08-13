# -*- coding: utf-8 -*-
"""Stage 5-3：插件来源策略（manifest sha256 pin + owner 批准）测试。

覆盖：
  (a) histopilot 与 sample-annotator 的 manifest 都通过 validate_manifest / negotiate；
  (b) source-policy.json 里两个 hash 与磁盘 manifest 实际 sha256 一致（防漂移守卫）；
  (c) 默认（policy 正确）时 /plugins/histopilot/ui/main.js 200、SAMPLE_PLUGIN_ENABLED=1
      时 index 正常注入 sample 块；
  (d) monkeypatch 把 policy 指向 hash 故意错误的临时文件 → sample 资源 403、index 不注入
      sample 块；histopilot hash 错 → 资源 403、index 不加载 bundle；
  (e) policy 文件缺失（指向不存在路径）→ dev 模式放行（200）；
  (f) plugin_source_allowed 各分支 reason：ok / not pinned / explicitly allowed(null) /
      manifest missing / source policy mismatch。

缓存隔离：``_plugin_source_policy`` 为模块级 ``lru_cache``。autouse fixture 在每用例
前后 ``cache_clear()``，避免跨用例 / 跨文件（test_sample_plugin / test_stage2_ui）的
策略缓存漂移；改 ``PLUGINS_SOURCE_POLICY_FILE`` 后显式 ``cache_clear()`` 强制重读。

运行：cd 项目根 && python3 -m pytest tests/test_plugin_source_policy.py -q
"""
import hashlib
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TMP = tempfile.mkdtemp(prefix="svs-srcpol-")
os.environ["SHARE_DATA_DIR"] = os.path.join(TMP, "share-data")
os.makedirs(os.environ["SHARE_DATA_DIR"], exist_ok=True)
os.environ["UPLOAD_DIR"] = os.path.join(TMP, "uploads")
os.makedirs(os.environ["UPLOAD_DIR"], exist_ok=True)
os.environ["AI_SIDECAR_URL"] = "http://127.0.0.1:8055"

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

from plugins.sdk import manifest as M  # noqa: E402
import app as app_mod  # noqa: E402
import pytest  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
PLUGINS_ROOT = REPO_ROOT / "plugins"
HISTOPILOT_MANIFEST = PLUGINS_ROOT / "histopilot" / "manifest.json"
SAMPLE_MANIFEST = PLUGINS_ROOT / "sample-annotator" / "manifest.json"
POLICY_FILE = PLUGINS_ROOT / "source-policy.json"


def _sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _write_policy(tmp_path, overrides=None):
    """写一个临时 source-policy.json：默认两个 hash 取磁盘实际值，再用 overrides 覆盖。

    overrides 形如 ``{"sample-annotator": "0"*64}``（故意错误）或
    ``{"sample-annotator": None}``（显式放行）。返回临时文件绝对路径字符串。
    """
    base = {
        "histopilot": _sha256(HISTOPILOT_MANIFEST),
        "sample-annotator": _sha256(SAMPLE_MANIFEST),
    }
    if overrides:
        base.update(overrides)
    p = Path(tmp_path) / "source-policy.json"
    p.write_text(json.dumps(base), encoding="utf-8")
    return str(p)


@pytest.fixture(autouse=True)
def _isolation(monkeypatch):
    """每用例：关认证、复位三个 env、清来源策略缓存（前后各一次，防跨文件漂移）。

    前清：上一用例（含其它文件）可能遗留缓存 + 已恢复 env → 重读当前 env。
    后清：本用例改的 env 由 monkeypatch 自动还原，缓存手动清以免泄漏给后续文件。
    """
    monkeypatch.setattr(app_mod, "AUTH_ENABLED", False)
    monkeypatch.delenv("SAMPLE_PLUGIN_ENABLED", raising=False)
    monkeypatch.delenv("HISTOPILOT_UI_ENABLED", raising=False)
    monkeypatch.delenv("PLUGINS_SOURCE_POLICY_FILE", raising=False)
    app_mod._plugin_source_policy.cache_clear()
    yield
    app_mod._plugin_source_policy.cache_clear()


def _client():
    app_mod.app.config["TESTING"] = True
    return app_mod.app.test_client()


# =========================================================================== #
# (a) 两个 manifest 都通过校验
# =========================================================================== #
def test_histopilot_manifest_validates_and_negotiates():
    data = json.loads(HISTOPILOT_MANIFEST.read_text(encoding="utf-8"))
    assert M.validate_manifest(data) == [], M.validate_manifest(data)
    res = M.negotiate_versions(data)
    assert res["id"] == "com.pathtogather.histopilot"
    assert res["pluginContractVersion"] == "1.0.0"
    assert res["bridgeProtocolVersion"] == "1.0.0"
    # 全五项权限
    assert set(data["permissions"]) == set(M.MANIFEST_PERMISSIONS)


def test_sample_manifest_validates_and_negotiates():
    data = json.loads(SAMPLE_MANIFEST.read_text(encoding="utf-8"))
    assert M.validate_manifest(data) == [], M.validate_manifest(data)
    res = M.negotiate_versions(data)
    assert res["id"] == "sample-annotator"


# =========================================================================== #
# (b) source-policy.json 防漂移守卫
# =========================================================================== #
def test_source_policy_keys_are_directory_names():
    """来源策略 key = plugins/ 下目录名（与 manifest id 不同）。"""
    policy = json.loads(POLICY_FILE.read_text(encoding="utf-8"))
    assert set(policy.keys()) == {"histopilot", "sample-annotator"}
    for key in policy:
        assert (PLUGINS_ROOT / key).is_dir(), "%s 不是 plugins/ 直下目录" % key


def test_source_policy_hashes_match_disk_manifests():
    """source-policy.json 内 hash 必须与磁盘 manifest 实际 sha256 一致（防漂移）。

    更新 manifest 后忘记同步 pin → 本守卫失败，提示重算 shasum。
    """
    policy = json.loads(POLICY_FILE.read_text(encoding="utf-8"))
    for key in ("histopilot", "sample-annotator"):
        expected = policy[key]
        assert isinstance(expected, str) and len(expected) == 64, \
            "%s pin 需为 64 位 sha256 hex" % key
        actual = _sha256(PLUGINS_ROOT / key / "manifest.json")
        assert actual == expected, (
            "%s manifest hash 漂移：policy=%s 磁盘=%s；"
            "请重算 shasum -a 256 plugins/%s/manifest.json 并同步 source-policy.json"
            % (key, expected, actual, key))


# =========================================================================== #
# (c) 默认（policy 正确）放行
# =========================================================================== #
def test_default_policy_histopilot_route_200():
    r = _client().get("/plugins/histopilot/ui/main.js")
    assert r.status_code == 200
    assert r.data


def test_default_policy_sample_route_200():
    r = _client().get("/plugins/sample-annotator/ui/main.js")
    assert r.status_code == 200
    assert r.data


def test_default_policy_index_renders_histopilot_bundle():
    body = _client().get("/").get_data(as_text=True)
    assert "/plugins/histopilot/ui/main.js" in body
    assert "/static/host-bridge.js" in body


def test_default_policy_sample_injected_when_enabled(monkeypatch):
    monkeypatch.setenv("SAMPLE_PLUGIN_ENABLED", "1")
    body = _client().get("/").get_data(as_text=True)
    assert "/plugins/sample-annotator/ui/main.js" in body
    assert "SVS_PLUGIN_PERMISSIONS" in body
    assert "slide:metadata:read" in body


# =========================================================================== #
# (d) hash 错误 → 来源拒绝
# =========================================================================== #
def test_sample_route_403_when_hash_mismatch(tmp_path, monkeypatch):
    pol = _write_policy(tmp_path, {"sample-annotator": "0" * 64})
    monkeypatch.setenv("PLUGINS_SOURCE_POLICY_FILE", pol)
    app_mod._plugin_source_policy.cache_clear()
    r = _client().get("/plugins/sample-annotator/ui/main.js")
    assert r.status_code == 403
    body = r.get_json()
    assert body["error"] == "forbidden"
    assert body["plugin_id"] == "sample-annotator"
    assert body["reason"] == "source policy mismatch"


def test_index_hides_sample_when_hash_mismatch(tmp_path, monkeypatch):
    pol = _write_policy(tmp_path, {"sample-annotator": "0" * 64})
    monkeypatch.setenv("PLUGINS_SOURCE_POLICY_FILE", pol)
    monkeypatch.setenv("SAMPLE_PLUGIN_ENABLED", "1")
    app_mod._plugin_source_policy.cache_clear()
    body = _client().get("/").get_data(as_text=True)
    assert "/plugins/sample-annotator/ui/main.js" not in body
    assert "SVS_PLUGIN_PERMISSIONS" not in body


def test_histopilot_route_403_when_hash_mismatch(tmp_path, monkeypatch):
    pol = _write_policy(tmp_path, {"histopilot": "0" * 64})
    monkeypatch.setenv("PLUGINS_SOURCE_POLICY_FILE", pol)
    app_mod._plugin_source_policy.cache_clear()
    r = _client().get("/plugins/histopilot/ui/main.js")
    assert r.status_code == 403
    body = r.get_json()
    assert body["plugin_id"] == "histopilot"
    assert body["reason"] == "source policy mismatch"


def test_index_hides_histopilot_when_hash_mismatch(tmp_path, monkeypatch):
    """histopilot index 注入 = flag 与来源策略与逻辑：来源拒绝 → 不加载 bundle。"""
    pol = _write_policy(tmp_path, {"histopilot": "0" * 64})
    monkeypatch.setenv("PLUGINS_SOURCE_POLICY_FILE", pol)
    app_mod._plugin_source_policy.cache_clear()
    body = _client().get("/").get_data(as_text=True)
    assert "/plugins/histopilot/ui/main.js" not in body
    assert "/static/host-bridge.js" not in body
    assert 'id="ai-btn"' not in body


# =========================================================================== #
# (e) dev 模式（policy 文件缺失）全放行
# =========================================================================== #
def test_dev_mode_allows_when_policy_file_missing(monkeypatch):
    monkeypatch.setenv("PLUGINS_SOURCE_POLICY_FILE",
                       os.path.join(TMP, "__definitely_missing__.json"))
    app_mod._plugin_source_policy.cache_clear()
    r = _client().get("/plugins/sample-annotator/ui/main.js")
    assert r.status_code == 200
    r2 = _client().get("/plugins/histopilot/ui/main.js")
    assert r2.status_code == 200


def test_dev_mode_index_renders_histopilot_bundle(monkeypatch):
    monkeypatch.setenv("PLUGINS_SOURCE_POLICY_FILE",
                       os.path.join(TMP, "__definitely_missing2__.json"))
    app_mod._plugin_source_policy.cache_clear()
    body = _client().get("/").get_data(as_text=True)
    assert "/plugins/histopilot/ui/main.js" in body


# =========================================================================== #
# (f) plugin_source_allowed 各分支 reason
# =========================================================================== #
def test_allowed_reason_ok_default():
    allowed, reason = app_mod.plugin_source_allowed("histopilot")
    assert allowed is True
    assert reason == "ok"


def test_allowed_reason_not_pinned(tmp_path, monkeypatch):
    # policy 只 pin histopilot → sample-annotator 未 pin
    p = Path(tmp_path) / "source-policy.json"
    p.write_text(json.dumps({"histopilot": _sha256(HISTOPILOT_MANIFEST)}),
                 encoding="utf-8")
    monkeypatch.setenv("PLUGINS_SOURCE_POLICY_FILE", str(p))
    app_mod._plugin_source_policy.cache_clear()
    allowed, reason = app_mod.plugin_source_allowed("sample-annotator")
    assert allowed is True
    assert reason == "source not pinned"


def test_allowed_reason_explicitly_allowed_null(tmp_path, monkeypatch):
    pol = _write_policy(tmp_path, {"sample-annotator": None})
    monkeypatch.setenv("PLUGINS_SOURCE_POLICY_FILE", pol)
    app_mod._plugin_source_policy.cache_clear()
    allowed, reason = app_mod.plugin_source_allowed("sample-annotator")
    assert allowed is True
    assert reason == "explicitly allowed"


def test_allowed_reason_manifest_missing(tmp_path, monkeypatch):
    """pin 一个目录存在但无 manifest 的 key → manifest missing 拒绝。

    通过临时 plugins 根（含空目录 fakeplugin）+ 指向它的 policy 文件构造：
    PLUGINS_DIR 重定向到临时根（manifest 路径随之），policy 经 env 指向临时文件。
    """
    fake_plugins = Path(tmp_path) / "plugins"
    (fake_plugins / "fakeplugin").mkdir(parents=True)  # 目录存在但无 manifest.json
    pol = Path(tmp_path) / "source-policy.json"
    pol.write_text(json.dumps({"fakeplugin": "deadbeef" * 8}), encoding="utf-8")
    monkeypatch.setenv("PLUGINS_SOURCE_POLICY_FILE", str(pol))
    monkeypatch.setattr(app_mod, "PLUGINS_DIR", fake_plugins)
    app_mod._plugin_source_policy.cache_clear()
    allowed, reason = app_mod.plugin_source_allowed("fakeplugin")
    assert allowed is False
    assert reason == "manifest missing"


def test_allowed_reason_mismatch(tmp_path, monkeypatch):
    pol = _write_policy(tmp_path, {"histopilot": "1" * 64})
    monkeypatch.setenv("PLUGINS_SOURCE_POLICY_FILE", pol)
    app_mod._plugin_source_policy.cache_clear()
    allowed, reason = app_mod.plugin_source_allowed("histopilot")
    assert allowed is False
    assert reason == "source policy mismatch"


def test_allowed_dev_mode_reason(tmp_path, monkeypatch):
    monkeypatch.setenv("PLUGINS_SOURCE_POLICY_FILE",
                       os.path.join(TMP, "__missing3__.json"))
    app_mod._plugin_source_policy.cache_clear()
    allowed, reason = app_mod.plugin_source_allowed("histopilot")
    assert allowed is True
    assert reason == "source policy not configured (dev mode)"


# =========================================================================== #
# (g) 未知目录先 404（不进入来源判定），与非 js/css 仍 403
# =========================================================================== #
def test_unknown_plugin_dir_404_not_403():
    r = _client().get("/plugins/does-not-exist/ui/main.js")
    assert r.status_code == 404


def test_non_js_css_still_403_under_correct_policy():
    # 默认 policy 正确：来源放行，但非 js/css 扩展名仍 403
    r = _client().get("/plugins/sample-annotator/ui/README.txt")
    assert r.status_code == 403


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
