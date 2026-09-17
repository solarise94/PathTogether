# -*- coding: utf-8 -*-
"""能力级 agent 退出（manifest.provides[].agent_exposed，2026-09-17 P1 修复）。

背景：样例插件 dev.sample.tma 的 slide_summary 被注入官方读片工具集后每会话
被必调，而插件后端无平台回调时恒返回降级占位（source=degraded、尺寸/mpp 为
空）——7 次调用全部空结果，只增冲突与无效步骤（切片尺寸/mpp 主上下文本就有）。

修复：provides 条目新增 additive 可选布尔 ``agent_exposed``（缺省/缺字段 =
暴露，向后兼容历史安装行）；``_list_agent_capabilities`` 过滤声明退出的能力，
``_inject_agent_extra_tools`` 不再为其签发工具与 token；dispatch 端点与插件
后端零改动（仍可被持有 token 的调用方使用）。

覆盖：
  - SDK 校验：agent_exposed 布尔通过 / 非布尔拒绝 / 缺省不受影响；
  - 登记透传：_parse_provides_registry 把标记归一落盘（缺省=True）；
  - 注入过滤：样例插件安装后不再进 _list_agent_capabilities / extra_tools
    （登记行仍保留该能力，dispatch 不受影响）；
  - 混合安装行：未标记能力照常注入（工具名 + token claims 清单）；
  - 历史兼容：缺 agent_exposed 字段的历史安装行照常注入。

运行：cd 项目根 && python3 -m pytest tests/test_plugin_agent_optout.py -q
"""
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import _bootstrap  # noqa: E402,F401  # session 目录+openslide stub（conftest 先行）
UPLOAD_DIR = _bootstrap.UPLOAD_DIR

import app as app_mod  # noqa: E402
import share_store  # noqa: E402
from _pt_helpers import isolate_app  # noqa: E402
from plugins.sdk import manifest as M  # noqa: E402

app_mod.UPLOAD_DIR = Path(os.environ["UPLOAD_DIR"])
app_mod.UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

import pytest  # noqa: E402

SLIDE = "demo.svs"


# --------------------------------------------------------------------------- #
# manifest 构造助手（与 test_plugin_manifest.py 同风格的最小形状）
# --------------------------------------------------------------------------- #
def _manifest(provides):
    return {
        "manifestSchemaVersion": "1.0.0",
        "id": "dev.optout.test",
        "name": "Optout Test Plugin",
        "pluginVersion": "0.1.0",
        "pluginContractVersion": "1.0.0",
        "bridgeProtocolVersion": "1.0.0",
        "ui": {"entry": "/plugin/index.html", "slots": ["viewer.right-panel"]},
        "service": {"baseUrl": "http://127.0.0.1:8061", "health": "/healthz"},
        "permissions": ["slide:metadata:read"],
        "provides": provides,
    }


def _capability(name, **overrides):
    base = {
        "name": name,
        "version": "1.0.0",
        "description": "只读能力占位描述，用于 agent 退出注入过滤测试。",
        "parameters": {"type": "object", "properties": {}, "required": []},
        "accessMode": "read",
        "requiredPermissions": ["slide:metadata:read"],
        "timeout_ms": 15000,
    }
    base.update(overrides)
    return base


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    """每用例独立存储 + AUTH_ENABLED=False（owner 无 uid → 全量权限映射）。"""
    isolate_app(monkeypatch, tmp_path)
    monkeypatch.setattr(app_mod, "AUTH_ENABLED", False)
    yield


# --------------------------------------------------------------------------- #
# 1. SDK 校验层（plugins/sdk/manifest.py validate_provides 白名单 + 类型）
# --------------------------------------------------------------------------- #
def test_validate_agent_exposed_bool_accepted():
    """additive 可选字段：true/false 均通过；缺省不受影响（向后兼容）。"""
    assert M.validate_manifest(
        _manifest([_capability("cap_a", agent_exposed=False)])) == []
    assert M.validate_manifest(
        _manifest([_capability("cap_a", agent_exposed=True)])) == []
    assert M.validate_manifest(_manifest([_capability("cap_a")])) == []


def test_validate_agent_exposed_non_bool_rejected():
    """非布尔（字符串/数字/容器）拒绝，错误指明字段。"""
    for bad in ("false", 0, 1, [], {}):
        d = _manifest([_capability("cap_a", agent_exposed=bad)])
        errs = M.validate_manifest(d)
        assert errs and any("agent_exposed" in e for e in errs), \
            "agent_exposed=%r 应被拒绝" % (bad,)


def test_validate_agent_exposed_null_treated_as_absent():
    """JSON null 与其它可选字段同口径：视为未声明（= 暴露），不拒绝。"""
    assert M.validate_manifest(
        _manifest([_capability("cap_a", agent_exposed=None)])) == []


# --------------------------------------------------------------------------- #
# 2. 登记层（app.py _parse_provides_registry：归一落盘）
# --------------------------------------------------------------------------- #
def test_parse_provides_registry_normalizes_agent_exposed():
    caps = app_mod._parse_provides_registry(_manifest([
        _capability("opted_out", agent_exposed=False),
        _capability("opted_in", agent_exposed=True),
        _capability("legacy_absent"),
    ]))
    assert [c["agent_exposed"] for c in caps] == [False, True, True]


# --------------------------------------------------------------------------- #
# 3. 注入过滤（_list_agent_capabilities / _inject_agent_extra_tools）
# --------------------------------------------------------------------------- #
def _install_optout_test_plugin():
    """按登记形状创建混合安装行：opted_out（false）+ legacy_absent（缺字段）。"""
    caps = app_mod._parse_provides_registry(_manifest([
        _capability("opted_out", agent_exposed=False),
        _capability("legacy_absent"),
        _capability("opted_in", agent_exposed=True),
    ]))
    created = share_store.create_plugin_installation(
        "dev.optout.test", capabilities=caps)
    return created, caps


def test_opted_out_capability_not_listed_or_injected():
    created, _ = _install_optout_test_plugin()
    listed = app_mod._list_agent_capabilities({}, SLIDE)
    names = [c["name"] for _, c in listed]
    # 声明退出的能力不出现；未标记（缺字段历史行）与显式 true 的照常出现
    assert "opted_out" not in names, names
    assert "legacy_absent" in names and "opted_in" in names, names
    # 注入：extra_tools / token claims 均不含退出能力（config 需非空——
    # 生产里由 _build_sidecar_config 组装，空 config 会被提前拒绝）
    config = {"base_url": "x"}
    assert app_mod._inject_agent_extra_tools({}, SLIDE, config) is True
    tool_names = [t["name"] for t in config["extra_tools"]]
    assert app_mod.capability_tool_name("dev.optout.test", "opted_out") \
        not in tool_names, tool_names
    assert app_mod.capability_tool_name("dev.optout.test", "legacy_absent") \
        in tool_names
    assert app_mod.capability_tool_name("dev.optout.test", "opted_in") \
        in tool_names
    claims, err = app_mod._agent_tool_token_decode(config["tool_token"])
    assert err is None, err
    assert "dev.optout.test/opted_out" not in (claims.get("capabilities") or [])
    assert sorted(claims.get("capabilities") or []) == [
        "dev.optout.test/legacy_absent", "dev.optout.test/opted_in"]
    assert created["installation_id"]  # 安装行确已建立（防手误空跑）


def test_all_capabilities_opted_out_injects_nothing():
    """全部能力声明退出 → 等价无可用能力：不写 extra_tools/tool_token。"""
    caps = app_mod._parse_provides_registry(
        _manifest([_capability("only_cap", agent_exposed=False)]))
    share_store.create_plugin_installation("dev.optout.test", capabilities=caps)
    assert app_mod._list_agent_capabilities({}, SLIDE) == []
    config = {"base_url": "x"}
    assert app_mod._inject_agent_extra_tools({}, SLIDE, config) is False
    assert "extra_tools" not in config and "tool_token" not in config


def test_sample_tma_optout_not_injected_but_registry_and_dispatch_intact():
    """样例插件（manifest 已标记 slide_summary 退出）：
    不进 agent 工具集；登记行仍登记该能力且字段透传（dispatch 侧不受影响，
    dispatch 端到端由 test_plugin_dispatch.py 覆盖）。"""
    installation, err = app_mod.install_plugin_bundle("sample-tma-score")
    assert err is None, err
    caps = installation.get("capabilities") or []
    assert [c["name"] for c in caps] == ["slide_summary"]
    assert caps[0]["agent_exposed"] is False
    assert app_mod._list_agent_capabilities({}, SLIDE) == []
    config = {"base_url": "x"}
    assert app_mod._inject_agent_extra_tools({}, SLIDE, config) is False
    assert "extra_tools" not in config and "tool_token" not in config
    # 登记行本身仍完整（可经 dispatch 调用，仅不进 AI 工具集）
    row = share_store.get_plugin_installation(installation["installation_id"])
    row_caps = row.get("capabilities") or []
    assert [c["name"] for c in row_caps] == ["slide_summary"]
    assert row_caps[0]["access_mode"] == "read"


def test_sample_tma_history_row_without_field_still_injected():
    """向后兼容：把样例登记行改回「缺字段」的历史形状 → 照常注入
    （存量安装行在重新安装前保持旧行为）。"""
    installation, err = app_mod.install_plugin_bundle("sample-tma-score")
    assert err is None, err
    legacy = [dict(c) for c in installation["capabilities"]]
    for c in legacy:
        c.pop("agent_exposed", None)
    share_store.set_installation_capabilities(
        installation["installation_id"], legacy)
    listed = app_mod._list_agent_capabilities({}, SLIDE)
    assert [c["name"] for _, c in listed] == ["slide_summary"]
    config = {"base_url": "x"}
    assert app_mod._inject_agent_extra_tools({}, SLIDE, config) is True
    assert [t["name"] for t in config["extra_tools"]] == \
        ["dev_sample_tma__slide_summary"]


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
