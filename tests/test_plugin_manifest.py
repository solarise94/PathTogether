# -*- coding: utf-8 -*-
"""Stage 5-1：manifest schema 校验 + 版本协商 + capabilities 端点测试。

覆盖（SDK 单元 + capabilities 端点，自包含，无共享 conftest fixture 依赖）：
  - ``validate_manifest``：合法通过 / 缺字段 / 非法 semver / 未知 permission /
    manifestSchemaVersion 结构错（非字符串）→ 错误列表非空且指明字段；
  - ``parse_semver``：合法三元组 / 非法抛 ValueError；
  - ``check_manifest_schema_supported`` N/N-1：major 1 接受、0 接受（前一 major
    实验期）、2/3 拒绝、非法拒绝；
  - ``negotiate_versions`` N/N-1：contract 1.x 与 0.9 接受、2.0 拒绝
    （field=="pluginContractVersion"）；bridge 同理（field=="bridgeProtocolVersion"）；
    结构非法 manifest 抛 field=="manifest"；
  - ``GET /api/plugin/v1/capabilities``：无 JWT 401/403；有效 plugin JWT 200、
    字段齐全、supportedContractMajors/supportedBridgeMajors 含 1、capabilities
    覆盖 v1 实际能力。

json 后端（RUN_PG_TESTS 未设）。运行：cd 项目根 && python3 -m pytest tests/test_plugin_manifest.py -q
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
from _pt_helpers import isolate_app  # noqa: E402
from _pt_helpers import csrf_client  # noqa: E402
import share_store  # noqa: E402

app_mod.UPLOAD_DIR = Path(os.environ["UPLOAD_DIR"])
app_mod.UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

import pytest  # noqa: E402


# --------------------------------------------------------------------------- #
# manifest 构造助手
# --------------------------------------------------------------------------- #
def _valid_manifest(**overrides):
    base = {
        "manifestSchemaVersion": "1.0.0",
        "id": "com.example.demo",
        "name": "Demo Plugin",
        "pluginVersion": "0.1.0",
        "pluginContractVersion": "1.0.0",
        "bridgeProtocolVersion": "1.0.0",
        "ui": {"entry": "/plugin/index.html", "slots": ["viewer.right-panel"]},
        "service": {"baseUrl": "http://demo:8055", "health": "/healthz"},
        "permissions": ["slide:metadata:read", "annotation:read"],
    }
    base.update(overrides)
    return base


def _valid_capability(**overrides):
    """合法 provides 条目（docs §3.1 schema 草案）。"""
    base = {
        "name": "score_core",
        "version": "1.0.0",
        "description": "计算 TMA 核心区域的着色评分，返回均值与分布摘要。",
        "parameters": {
            "type": "object",
            "properties": {
                "core_count": {
                    "type": "integer",
                    "description": "参与评分的核心数",
                    "minimum": 1,
                    "maximum": 1000,
                },
                "stain": {"type": "string", "enum": ["hek", "ihc"]},
            },
            "required": ["core_count"],
        },
        "accessMode": "read",
        "requiredPermissions": ["slide:metadata:read"],
    }
    base.update(overrides)
    return base


# --------------------------------------------------------------------------- #
# 1. validate_manifest（结构校验）
# --------------------------------------------------------------------------- #
def test_validate_manifest_valid_passes():
    assert M.validate_manifest(_valid_manifest()) == []


def test_validate_manifest_missing_field_reports_field():
    d = _valid_manifest()
    del d["pluginContractVersion"]
    errs = M.validate_manifest(d)
    assert errs, "缺字段应有错误"
    assert any("pluginContractVersion" in e for e in errs), errs


def test_validate_manifest_bad_semver_reports_field():
    d = _valid_manifest(pluginVersion="v1.0")  # 非 semver
    errs = M.validate_manifest(d)
    assert errs and any("pluginVersion" in e for e in errs), errs


def test_validate_manifest_unknown_permission_reports_value():
    d = _valid_manifest(permissions=["slide:metadata:read", "screen:capture"])
    errs = M.validate_manifest(d)
    assert errs and any("permissions" in e and "screen:capture" in e for e in errs), errs


def test_validate_manifest_schema_struct_bad_type_reports_field():
    # manifestSchemaVersion 非字符串（结构错）
    d = _valid_manifest(manifestSchemaVersion=1)
    errs = M.validate_manifest(d)
    assert errs and any("manifestSchemaVersion" in e for e in errs), errs


def test_validate_manifest_ui_slots_must_be_strings():
    d = _valid_manifest()
    d["ui"]["slots"] = ["ok", 123]  # 非字符串元素
    errs = M.validate_manifest(d)
    assert errs and any("slots" in e for e in errs), errs


def test_validate_manifest_service_missing_field():
    d = _valid_manifest(service={"baseUrl": "http://x"})  # 缺 health
    errs = M.validate_manifest(d)
    assert errs and any("service.health" in e for e in errs), errs


def test_validate_manifest_base_url_scheme_must_be_http():
    """声明 provides 时 baseUrl 仅允许 http/https 绝对地址（D1：dispatch 出站）。"""
    for bad in ("file:///etc/passwd", "gopher://x:70", "ftp://x/y",
                "127.0.0.1:8061", "//x/capabilities", "/plugins/x"):
        d = _valid_manifest(service={"baseUrl": bad, "health": "/healthz"})
        d["provides"] = [_valid_capability()]
        errs = M.validate_manifest(d)
        assert errs and any("service.baseUrl" in e for e in errs), (bad, errs)
    for ok in ("http://127.0.0.1:8061", "https://plugins.example.com/api"):
        d = _valid_manifest(service={"baseUrl": ok, "health": "/healthz"})
        d["provides"] = [_valid_capability()]
        assert M.validate_manifest(d) == [], ok


def test_validate_manifest_base_url_loose_without_provides():
    """纯 UI 插件（无 provides）不出站：baseUrl 同源占位（"/"）不受 scheme 约束。"""
    d = _valid_manifest(service={"baseUrl": "/", "health": "/healthz"})
    assert M.validate_manifest(d) == []


def test_validate_manifest_top_level_must_be_object():
    errs = M.validate_manifest(["not", "an", "object"])
    assert errs and any("顶层" in e for e in errs), errs


# --------------------------------------------------------------------------- #
# 2. parse_semver
# --------------------------------------------------------------------------- #
def test_parse_semver_valid_and_invalid():
    assert M.parse_semver("1.2.3") == (1, 2, 3)
    assert M.parse_semver("0.0.0") == (0, 0, 0)
    assert M.parse_semver("10.20.30") == (10, 20, 30)
    for bad in ("1.2", "1", "v1.0.0", "1.0.0-rc1", "abc", "", None, 1, []):
        with pytest.raises(ValueError):
            M.parse_semver(bad)


# --------------------------------------------------------------------------- #
# 3. check_manifest_schema_supported（manifest loader N/N-1）
# --------------------------------------------------------------------------- #
def test_manifest_schema_major_n_n_minus_1():
    # 当前 major：接受
    assert M.check_manifest_schema_supported("1.0.0") is True
    assert M.check_manifest_schema_supported("1.5.7") is True
    # 前一 major（current=1 时 0.x 视为实验期兼容）：接受
    assert M.check_manifest_schema_supported("0.9.0") is True
    # 未来 major：拒绝
    assert M.check_manifest_schema_supported("2.0.0") is False
    assert M.check_manifest_schema_supported("3.1.0") is False
    # 非法版本：拒绝
    assert M.check_manifest_schema_supported("nope") is False
    assert M.check_manifest_schema_supported(None) is False


# --------------------------------------------------------------------------- #
# 4. negotiate_versions（contract / bridge major N/N-1）
# --------------------------------------------------------------------------- #
def test_negotiate_contract_major_n_n_minus_1():
    # 1.x：接受，echo 协商成功的版本
    r = M.negotiate_versions(_valid_manifest(pluginContractVersion="1.5.2"))
    assert r["pluginContractVersion"] == "1.5.2"
    # 0.9（前一 major）：接受
    r = M.negotiate_versions(_valid_manifest(pluginContractVersion="0.9.0"))
    assert r["pluginContractVersion"] == "0.9.0"
    # 2.0：拒绝，field 指明字段 + 插件值 + 平台接受范围
    with pytest.raises(M.PluginVersionError) as ei:
        M.negotiate_versions(_valid_manifest(pluginContractVersion="2.0.0"))
    err = ei.value
    assert err.field == "pluginContractVersion"
    assert err.plugin_version == "2.0.0"
    assert 1 in err.supported and 0 in err.supported  # N/N-1 范围
    assert "pluginContractVersion" in str(err) and "2.0.0" in str(err)


def test_negotiate_bridge_major_n_n_minus_1():
    # 1.x：接受
    M.negotiate_versions(_valid_manifest(bridgeProtocolVersion="1.7.0"))
    # 0.9（前一 major）：接受
    M.negotiate_versions(_valid_manifest(bridgeProtocolVersion="0.9.0"))
    # 2.0：拒绝
    with pytest.raises(M.PluginVersionError) as ei:
        M.negotiate_versions(_valid_manifest(bridgeProtocolVersion="2.0.0"))
    err = ei.value
    assert err.field == "bridgeProtocolVersion"
    assert err.plugin_version == "2.0.0"
    assert 1 in err.supported


def test_negotiate_contract_checked_before_bridge():
    # 两者都不兼容时，先报 contract（实现顺序：contract → bridge）
    with pytest.raises(M.PluginVersionError) as ei:
        M.negotiate_versions(_valid_manifest(
            pluginContractVersion="9.0.0", bridgeProtocolVersion="9.0.0"))
    assert ei.value.field == "pluginContractVersion"


def test_negotiate_malformed_manifest_raises_manifest_field():
    d = _valid_manifest()
    del d["bridgeProtocolVersion"]  # 结构非法
    with pytest.raises(M.PluginVersionError) as ei:
        M.negotiate_versions(d)
    assert ei.value.field == "manifest"


def test_negotiate_returns_negotiated_version_echo():
    # §7.0：plugin_contract_version 记录运行时实际协商成功的版本，非产品版本
    r = M.negotiate_versions(_valid_manifest(
        pluginVersion="0.42.0", pluginContractVersion="1.3.7",
        bridgeProtocolVersion="1.0.4", manifestSchemaVersion="1.0.0"))
    assert r["pluginContractVersion"] == "1.3.7"
    assert r["bridgeProtocolVersion"] == "1.0.4"
    assert r["manifestSchemaVersion"] == "1.0.0"
    # 产品版本不影响协商结果字段，但可一并回显
    assert r["pluginVersion"] == "0.42.0"


# --------------------------------------------------------------------------- #
# 4.5 provides 契约校验（插件能力层 docs §3，P1）
# --------------------------------------------------------------------------- #
def test_provides_absent_is_fine():
    """可选字段：不声明 provides 的老 manifest 完全不受影响（零迁移）。"""
    assert M.validate_manifest(_valid_manifest()) == []


def test_provides_valid_passes():
    d = _valid_manifest(provides=[_valid_capability()])
    assert M.validate_manifest(d) == []
    assert M.validate_provides(d) == []


def test_provides_write_access_mode_rejected():
    """P1 校验层拒绝 write（docs §6.2：不是运行时过滤，是登记前拒绝）。"""
    d = _valid_manifest(provides=[_valid_capability(accessMode="write")])
    errs = M.validate_manifest(d)
    assert errs and any("accessMode" in e and "write" in e for e in errs), errs


def test_provides_unknown_access_mode_rejected():
    d = _valid_manifest(provides=[_valid_capability(accessMode="admin")])
    errs = M.validate_manifest(d)
    assert errs and any("accessMode" in e for e in errs), errs


def test_provides_parameter_superset_rejected():
    """parameters 限 JSON Schema 子集：超集键（如 default/format）拒绝。"""
    bad = _valid_capability()
    bad["parameters"]["properties"]["core_count"]["default"] = 10
    d = _valid_manifest(provides=[bad])
    errs = M.validate_manifest(d)
    assert errs and any("default" in e and "parameters" in e for e in errs), errs


def test_provides_parameter_bad_type_and_required_keys():
    bad = _valid_capability()
    bad["parameters"]["properties"]["core_count"]["type"] = "function"
    bad["parameters"]["required"] = ["not_declared"]
    d = _valid_manifest(provides=[bad])
    errs = M.validate_manifest(d)
    assert errs and any("type=" in e for e in errs), errs
    assert any("not_declared" in e for e in errs), errs


def test_provides_duplicate_names_rejected():
    """同 manifest 内能力重名拒绝（注册表层面 pluginId 内唯一，docs §3.2）。"""
    d = _valid_manifest(provides=[
        _valid_capability(name="score_core"),
        _valid_capability(name="score_core", version="1.1.0"),
    ])
    errs = M.validate_manifest(d)
    assert errs and any("重名" in e for e in errs), errs


def test_provides_name_rule_enforced():
    for bad_name in ("Score", "1score", "score-core", "s", "",
                     "a" * 65, "score core"):
        d = _valid_manifest(provides=[_valid_capability(name=bad_name)])
        errs = M.validate_manifest(d)
        assert errs and any(".name" in e for e in errs), "name=%r 应被拒绝" % bad_name
    # 合法形态：小写开头 + [a-z0-9_]，长度 2~64
    assert M.validate_manifest(
        _valid_manifest(provides=[_valid_capability(name="a1")])) == []
    assert M.validate_manifest(
        _valid_manifest(provides=[_valid_capability(name="a" * 64)])) == []


def test_provides_description_length_bounds():
    for bad_desc in ("太短", "x" * 7, "y" * 501):
        d = _valid_manifest(provides=[_valid_capability(description=bad_desc)])
        errs = M.validate_manifest(d)
        assert errs and any("description" in e for e in errs), "len=%d 应被拒绝" % len(bad_desc)
    # 边界内合法：8 与 500
    assert M.validate_manifest(
        _valid_manifest(provides=[_valid_capability(description="z" * 8)])) == []
    assert M.validate_manifest(
        _valid_manifest(provides=[_valid_capability(description="z" * 500)])) == []


def test_provides_required_permissions_enum_narrowed():
    """requiredPermissions 不含 viewer:navigate（UI 侧权限无服务端消费场景）。"""
    d = _valid_manifest(provides=[
        _valid_capability(requiredPermissions=["viewer:navigate"])])
    errs = M.validate_manifest(d)
    assert errs and any("viewer:navigate" in e for e in errs), errs
    # 4 项服务端权限均合法
    for p in ("slide:metadata:read", "slide:region:read",
              "annotation:read", "annotation:write"):
        assert M.validate_manifest(
            _valid_manifest(provides=[_valid_capability(requiredPermissions=[p])])) == []


def test_provides_missing_required_field_and_unknown_field():
    d = _valid_manifest(provides=[{"name": "score_core"}])  # 缺 version 等
    errs = M.validate_manifest(d)
    assert errs and any("缺少必填字段" in e for e in errs), errs
    d2 = _valid_manifest(provides=[_valid_capability(unsupported=1)])
    errs2 = M.validate_manifest(d2)
    assert errs2 and any("unsupported" in e for e in errs2), errs2


def test_provides_timeout_ms_bounds():
    for bad in (0, -1, 60001, "15000", 1.5, True):
        d = _valid_manifest(provides=[_valid_capability(timeout_ms=bad)])
        errs = M.validate_manifest(d)
        assert errs and any("timeout_ms" in e for e in errs), "timeout_ms=%r 应被拒绝" % bad
    assert M.validate_manifest(
        _valid_manifest(provides=[_valid_capability(timeout_ms=1)])) == []
    assert M.validate_manifest(
        _valid_manifest(provides=[_valid_capability(timeout_ms=60000)])) == []


def test_provides_not_array_rejected():
    errs = M.validate_manifest(_valid_manifest(provides={"name": "x"}))
    assert errs and any("provides" in e for e in errs), errs


def test_capability_tool_name_mangling():
    """注入 agent 的工具名 = {pluginId 去域名点下划线连接}__{name}（§3.2）。"""
    assert M.capability_tool_name("dev.example.tma", "score_core") == \
        "dev_example_tma__score_core"
    assert M.capability_tool_name("histopilot", "slide_summary") == \
        "histopilot__slide_summary"


# --------------------------------------------------------------------------- #
# 5. capabilities 端点（需要 app + plugin JWT）
# --------------------------------------------------------------------------- #
@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    """capabilities 端点用例的独立存储（SDK 单元用例不受影响）。"""
    isolate_app(monkeypatch, tmp_path)
    monkeypatch.setattr(app_mod, "AUTH_ENABLED", False)
    yield


def _bootstrap():
    inst = app_mod._bootstrap_plugin_installations()
    assert inst is not None, "引导应成功"
    app_mod._HISTOPILOT_INSTALLATION = inst
    return inst


def _file_secret():
    f = Path(os.environ["SHARE_DATA_DIR"]) / "plugin-secret-histopilot.txt"
    raw = f.read_text(encoding="utf-8").strip()
    try:
        obj = json.loads(raw)
        if isinstance(obj, dict):
            return str(obj.get("secret") or "")
    except (ValueError, TypeError):
        pass
    return raw


def _client():
    app_mod.app.config["TESTING"] = True
    return csrf_client(app_mod.app.test_client())


def _token_for(inst):
    r = _client().post("/api/plugin/v1/auth/token",
                       json={"installation_id": inst["installation_id"],
                             "secret": _file_secret()})
    assert r.status_code == 200, r.get_json()
    return r.get_json()["access_token"]


def _bearer(token):
    return {"Authorization": "Bearer " + token}


def test_capabilities_requires_jwt():
    _bootstrap()
    r = _client().get("/api/plugin/v1/capabilities")
    assert r.status_code in (401, 403), r.get_json()


def test_capabilities_happy_path_fields_and_majors():
    inst = _bootstrap()
    r = _client().get("/api/plugin/v1/capabilities", headers=_bearer(_token_for(inst)))
    assert r.status_code == 200, r.get_json()
    body = r.get_json()
    # 版本字符串（与 manifest SDK 单一来源一致）
    assert body["pluginContractVersion"] == M.PLUGIN_CONTRACT_VERSION == "1.0.0"
    assert body["bridgeProtocolVersion"] == M.BRIDGE_PROTOCOL_VERSION == "1.0.0"
    # 支持 major 列表含 1（与 SDK 常量一致）
    assert 1 in body["supportedContractMajors"]
    assert 1 in body["supportedBridgeMajors"]
    assert body["supportedContractMajors"] == list(M.SUPPORTED_CONTRACT_MAJORS)
    # capabilities 覆盖 v1 实际能力
    caps = body["capabilities"]
    for needed in ("slide:metadata:read", "slide:region:read", "annotation:read",
                   "annotation:write", "viewer:navigate", "events:read", "audit:write"):
        assert needed in caps, "缺能力 %s" % needed


def test_capabilities_disabled_installation_401():
    inst = _bootstrap()
    client = _client()
    with client.session_transaction() as s:
        s["role"] = "owner"
        s["user_id"] = "usr_owner"
        s["auth_user"] = "owner"
    token = _token_for(inst)
    # disable 后旧 token 立即失效
    assert client.post("/api/admin/plugins/%s/disable" % inst["installation_id"]).status_code == 200
    r = client.get("/api/plugin/v1/capabilities", headers=_bearer(token))
    assert r.status_code == 401


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
