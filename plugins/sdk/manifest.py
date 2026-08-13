# -*- coding: utf-8 -*-
"""Plugin manifest schema 校验器与版本协商（Stage 5-1，docs §7.0/§7.1）。

纯 stdlib 实现（**不引入 jsonschema 依赖**）：手写最小校验覆盖必填/类型/
semver pattern/permissions 枚举/slots 字符串。供平台 app.py（capabilities
端点 + 加载期协商）与示例插件共用，权威字段定义见 ``plugins/manifest.schema.json``。

版本模型（§7.0）：四个版本字段相互独立，禁止共用一个模糊 ``version`` 字段——
  - ``manifestSchemaVersion``：manifest 文件自身的字段结构（语法不兼容 bump major）；
  - ``pluginContractVersion``：capability API / 领域类型 / 错误码（破坏 API/语义 bump major）；
  - ``bridgeProtocolVersion``：iframe HostBridge 消息协议（消息不兼容 bump major）；
  - ``pluginVersion``：产品/镜像/bundle 版本（正常产品 SemVer，不自动改变其它三者）。

N/N-1 兼容策略：平台接受当前 major 与前一 major（current=1 时 0.x 视为实验期兼容）。
平台在加载/启动前完成版本协商，不兼容则拒绝启动（``PluginVersionError``）。
"""
import re

# ---------------------------------------------------------------------------
# 版本常量（capabilities 端点的单一来源；app.py 复用）
# ---------------------------------------------------------------------------
# 当前 manifest schema major（与 manifestSchemaVersion 的 major 对齐）。
MANIFEST_SCHEMA_MAJOR = 1

# 平台当前支持的 contract / bridge major（§7.2 capabilities 端点 advertised）。
# negotiate_versions 在此基础上额外接受前一 major（N/N-1）。
SUPPORTED_CONTRACT_MAJORS = (1,)
SUPPORTED_BRIDGE_MAJORS = (1,)

# 平台对外声明的完整版本字符串（与 SUPPORTED_*_MAJORS 的 major 一致）。
PLUGIN_CONTRACT_VERSION = "1.0.0"
BRIDGE_PROTOCOL_VERSION = "1.0.0"

# manifest permissions 枚举（§7.1；与 capabilities 列表对齐，未知值校验失败）。
MANIFEST_PERMISSIONS = (
    "slide:metadata:read",
    "slide:region:read",
    "annotation:read",
    "annotation:write",
    "viewer:navigate",
)

# semver core：major.minor.patch（不带 prerelease/build，保持 parse 简单；与
# manifest.schema.json 的 pattern 一致）。
_SEMVER_RE = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")


class PluginVersionError(Exception):
    """manifest 版本协商不兼容错误。

    属性：
      - ``field``：不兼容的版本字段名（``pluginContractVersion`` /
        ``bridgeProtocolVersion``），或 ``"manifest"`` 表示整体结构非法；
      - ``plugin_version``：插件声明的版本字符串（结构非法时为占位值）；
      - ``supported``：平台接受的 major 列表（N/N-1 范围）。
    """

    def __init__(self, field, plugin_version, supported, message=None):
        self.field = field
        self.plugin_version = plugin_version
        self.supported = list(supported)
        if message is None:
            message = "插件 %s 版本不兼容：插件=%r 平台接受的 major=%r" % (
                field, plugin_version, self.supported)
        super().__init__(message)


def parse_semver(v):
    """解析 semver 字符串 → ``(major, minor, patch)`` 整数三元组。

    非法（非字符串、不匹配 ``major.minor.patch``）抛 ``ValueError``。
    """
    if not isinstance(v, str):
        raise ValueError("版本号需为字符串，got %r" % (v,))
    m = _SEMVER_RE.match(v)
    if not m:
        raise ValueError("非法 semver：%r" % (v,))
    return (int(m.group(1)), int(m.group(2)), int(m.group(3)))


def _accepted_majors(supported_majors):
    """N/N-1 接受范围：当前 major 与前一 major。

    current=1 时返回 ``[1, 0]``——0.x 视为实验期兼容（前一 major）。注意与
    运行时消息协议（JS BridgeVersion，默认强制同 major）不同：manifest 加载期
    协商更宽松，接受前一 major；运行时每条消息强制同 major。
    """
    current = max(supported_majors)
    return [current, current - 1]


def check_manifest_schema_supported(mv):
    """manifest schema major 是否落在 N/N-1 支持范围。

    ``manifestSchemaVersion.major ∈ {MANIFEST_SCHEMA_MAJOR, MANIFEST_SCHEMA_MAJOR-1}``。
    非法/不可解析的版本号返回 False（调用方应先用 validate_manifest 拦截结构错误）。
    """
    try:
        major, _, _ = parse_semver(mv)
    except (ValueError, TypeError):
        return False
    return major == MANIFEST_SCHEMA_MAJOR or major == MANIFEST_SCHEMA_MAJOR - 1


def validate_manifest(d):
    """校验 manifest 结构/类型/semver/枚举，返回错误信息列表（**空 = 通过**）。

    纯结构校验，不含版本 major 范围策略（N/N-1 由 check_manifest_schema_supported /
    negotiate_versions 负责）。每条错误信息指明出问题的字段。
    """
    errors = []
    if not isinstance(d, dict):
        return ["manifest 顶层需为对象，got %s" % type(d).__name__]

    # ---- 顶层必填字符串字段 ----
    _STR_FIELDS = ("manifestSchemaVersion", "id", "name", "pluginVersion",
                   "pluginContractVersion", "bridgeProtocolVersion")
    for key in _STR_FIELDS:
        v = d.get(key)
        if v is None or v == "":
            errors.append("缺少必填字段：%s" % key)
        elif not isinstance(v, str):
            errors.append("%s 需为字符串，got %s" % (key, type(v).__name__))

    # ---- 四个版本字段 semver 格式（§7.0，相互独立）----
    for key in ("manifestSchemaVersion", "pluginVersion",
                "pluginContractVersion", "bridgeProtocolVersion"):
        v = d.get(key)
        if isinstance(v, str):
            try:
                parse_semver(v)
            except ValueError as e:
                errors.append("%s：%s" % (key, e))

    # ---- manifestSchemaVersion 结构：必须可解析为 semver（上面已校验） ----

    # ---- ui ----
    ui = d.get("ui")
    if ui is None:
        errors.append("缺少必填字段：ui")
    elif not isinstance(ui, dict):
        errors.append("ui 需为对象，got %s" % type(ui).__name__)
    else:
        entry = ui.get("entry")
        if not isinstance(entry, str) or not entry:
            errors.append("ui.entry 需为非空字符串")
        slots = ui.get("slots")
        if slots is None:
            errors.append("缺少必填字段：ui.slots")
        elif not isinstance(slots, list):
            errors.append("ui.slots 需为数组，got %s" % type(slots).__name__)
        elif not slots:
            errors.append("ui.slots 不能为空")
        else:
            for i, s in enumerate(slots):
                if not isinstance(s, str) or not s:
                    errors.append("ui.slots[%d] 需为非空字符串" % i)

    # ---- service ----
    svc = d.get("service")
    if svc is None:
        errors.append("缺少必填字段：service")
    elif not isinstance(svc, dict):
        errors.append("service 需为对象，got %s" % type(svc).__name__)
    else:
        base = svc.get("baseUrl")
        if not isinstance(base, str) or not base:
            errors.append("service.baseUrl 需为非空字符串")
        health = svc.get("health")
        if not isinstance(health, str) or not health:
            errors.append("service.health 需为非空字符串")

    # ---- permissions 枚举 ----
    perms = d.get("permissions")
    if perms is None:
        errors.append("缺少必填字段：permissions")
    elif not isinstance(perms, list):
        errors.append("permissions 需为数组，got %s" % type(perms).__name__)
    else:
        for i, p in enumerate(perms):
            if not isinstance(p, str):
                errors.append("permissions[%d] 需为字符串，got %s" % (i, type(p).__name__))
            elif p not in MANIFEST_PERMISSIONS:
                errors.append("permissions[%d]=%r 不在允许枚举中（允许：%s）"
                              % (i, p, ", ".join(MANIFEST_PERMISSIONS)))

    return errors


def negotiate_versions(manifest):
    """contract/bridge major N/N-1 协商。

    先 ``validate_manifest``（结构非法 → 抛 ``PluginVersionError(field="manifest")``），
    再检查 ``pluginContractVersion.major`` 与 ``bridgeProtocolVersion.major`` 落在
    N/N-1 接受范围；不兼容抛 ``PluginVersionError``（field 指明哪个版本字段，
    message 含插件值与平台接受范围）。

    成功返回协商结果 dict：echo 插件实际声明并协商成功的版本（§7.0 要求
    ``plugin_contract_version`` 记录运行时实际协商成功的值，不记产品版本）。
    """
    errors = validate_manifest(manifest)
    if errors:
        raise PluginVersionError("manifest", "(malformed)", [],
                                 message="manifest 校验失败：" + "; ".join(errors))
    contract = manifest["pluginContractVersion"]
    bridge = manifest["bridgeProtocolVersion"]
    cmaj = parse_semver(contract)[0]
    bmaj = parse_semver(bridge)[0]

    contract_accept = _accepted_majors(SUPPORTED_CONTRACT_MAJORS)
    bridge_accept = _accepted_majors(SUPPORTED_BRIDGE_MAJORS)
    if cmaj not in contract_accept:
        raise PluginVersionError("pluginContractVersion", contract, contract_accept)
    if bmaj not in bridge_accept:
        raise PluginVersionError("bridgeProtocolVersion", bridge, bridge_accept)

    return {
        "manifestSchemaVersion": manifest["manifestSchemaVersion"],
        "id": manifest["id"],
        "pluginVersion": manifest["pluginVersion"],
        "pluginContractVersion": contract,
        "bridgeProtocolVersion": bridge,
    }
