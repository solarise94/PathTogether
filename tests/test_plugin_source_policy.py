# -*- coding: utf-8 -*-
"""Plugin source policy and external bundle directory tests."""
import hashlib
import json
import os
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import _bootstrap  # noqa: E402,F401  # session 目录+openslide stub（conftest 先行）
UPLOAD_DIR = _bootstrap.UPLOAD_DIR
from plugins.sdk import manifest as M  # noqa: E402
import app as app_mod  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
SAMPLE_MANIFEST = REPO_ROOT / "plugins" / "sample-annotator" / "manifest.json"
TMA_MANIFEST = REPO_ROOT / "plugins" / "sample-tma-score" / "manifest.json"


def _sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _install_histopilot(root):
    plugin = Path(root) / "histopilot"
    (plugin / "ui").mkdir(parents=True)
    manifest = {
        "manifestSchemaVersion": "1.0.0",
        "id": "com.pathtogether.histopilot",
        "name": "HistoPilot",
        "pluginVersion": "0.1.0",
        "pluginContractVersion": "1.0.0",
        "bridgeProtocolVersion": "1.0.0",
        "ui": {"entry": "/plugins/histopilot/ui/main.js", "slots": ["viewer.right-panel"]},
        "service": {"baseUrl": "/", "health": "/healthz"},
        "permissions": list(M.MANIFEST_PERMISSIONS),
    }
    (plugin / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    (plugin / "ui" / "main.js").write_text("window.HistoPilot = {};\n", encoding="utf-8")
    return plugin


@pytest.fixture(autouse=True)
def _isolation(tmp_path, monkeypatch):
    monkeypatch.setattr(app_mod, "AUTH_ENABLED", False)
    monkeypatch.setattr(app_mod, "PLUGIN_BUNDLES_DIR", tmp_path / "external-plugins")
    monkeypatch.delenv("SAMPLE_PLUGIN_ENABLED", raising=False)
    monkeypatch.delenv("HISTOPILOT_UI_ENABLED", raising=False)
    monkeypatch.delenv("PLUGINS_SOURCE_POLICY_FILE", raising=False)
    app_mod._plugin_source_policy.cache_clear()
    yield
    app_mod._plugin_source_policy.cache_clear()


def _client():
    app_mod.app.config["TESTING"] = True
    return app_mod.app.test_client()


def test_sample_manifest_validates_and_policy_pin_matches():
    data = json.loads(SAMPLE_MANIFEST.read_text(encoding="utf-8"))
    assert M.validate_manifest(data) == []
    policy = json.loads((REPO_ROOT / "plugins" / "source-policy.json").read_text(encoding="utf-8"))
    # 防漂移守卫：两个内置示例插件的 manifest sha256 pin 均须与磁盘一致
    assert set(policy) == {"sample-annotator", "sample-tma-score"}
    assert policy["sample-annotator"] == _sha256(SAMPLE_MANIFEST)
    assert policy["sample-tma-score"] == _sha256(TMA_MANIFEST)
    # sample-tma-score 的 provides 声明须通过校验器（能力注册表登记前置）
    tma = json.loads(TMA_MANIFEST.read_text(encoding="utf-8"))
    assert M.validate_manifest(tma) == []
    assert [c["name"] for c in tma["provides"]] == ["slide_summary"]


def test_histopilot_is_absent_by_default():
    assert app_mod.histopilot_ui_enabled() is False
    assert _client().get("/plugins/histopilot/ui/main.js").status_code == 404
    assert "/plugins/histopilot/ui/main.js" not in _client().get("/").get_data(as_text=True)


def test_external_histopilot_bundle_is_discovered(tmp_path, monkeypatch):
    external = tmp_path / "bundles"
    plugin = _install_histopilot(external)
    monkeypatch.setattr(app_mod, "PLUGIN_BUNDLES_DIR", external)
    policy = tmp_path / "policy.json"
    policy.write_text(json.dumps({"histopilot": _sha256(plugin / "manifest.json")}), encoding="utf-8")
    monkeypatch.setenv("PLUGINS_SOURCE_POLICY_FILE", str(policy))
    app_mod._plugin_source_policy.cache_clear()

    assert app_mod.histopilot_ui_enabled() is True
    assert _client().get("/plugins/histopilot/ui/main.js").status_code == 200
    assert "/plugins/histopilot/ui/main.js" in _client().get("/").get_data(as_text=True)


def test_external_bundle_flag_can_disable(tmp_path, monkeypatch):
    external = tmp_path / "bundles"
    _install_histopilot(external)
    monkeypatch.setattr(app_mod, "PLUGIN_BUNDLES_DIR", external)
    monkeypatch.setenv("HISTOPILOT_UI_ENABLED", "0")
    assert app_mod.histopilot_ui_enabled() is False
    assert _client().get("/plugins/histopilot/ui/main.js").status_code == 404


def test_external_bundle_hash_mismatch_is_rejected(tmp_path, monkeypatch):
    external = tmp_path / "bundles"
    _install_histopilot(external)
    monkeypatch.setattr(app_mod, "PLUGIN_BUNDLES_DIR", external)
    policy = tmp_path / "policy.json"
    policy.write_text(json.dumps({"histopilot": "0" * 64}), encoding="utf-8")
    monkeypatch.setenv("PLUGINS_SOURCE_POLICY_FILE", str(policy))
    app_mod._plugin_source_policy.cache_clear()
    response = _client().get("/plugins/histopilot/ui/main.js")
    assert response.status_code == 403
    assert response.get_json()["reason"] == "source policy mismatch"


def test_external_bundle_precedes_builtin_directory(tmp_path, monkeypatch):
    external = tmp_path / "bundles"
    plugin = _install_histopilot(external)
    monkeypatch.setattr(app_mod, "PLUGIN_BUNDLES_DIR", external)
    assert app_mod._plugin_dir("histopilot") == plugin


def test_unknown_plugin_and_traversal_are_rejected():
    assert _client().get("/plugins/does-not-exist/ui/main.js").status_code == 404
    for plugin_id in ("../static", "..%2fstatic", "a/b"):
        assert _client().get("/plugins/%s/ui/main.js" % plugin_id).status_code == 404


def test_sample_bundle_still_served_and_non_ui_extension_rejected():
    assert _client().get("/plugins/sample-annotator/ui/main.js").status_code == 200
    assert _client().get("/plugins/sample-annotator/ui/README.txt").status_code == 403
