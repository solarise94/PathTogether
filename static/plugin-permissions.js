/* =========================================================================
   PluginPermissions —— 平台 host 端插件权限门（Stage 5-2，docs §7.1/§7.5）

   UMD 双端：
     - 浏览器 <script> → 挂 window.PluginPermissions；
     - node（vitest）   → module.exports = { METHOD_PERMISSIONS, PRIVILEGED_PLUGIN_IDS, checkPermission, gatePermission }。

   host 端 app.js registerHostBridgeHandlers 在**每个被 gate 的 host 方法入口**先查
   env.pluginInstallationId 对应插件声明的 manifest permissions：
     - 声明了所需 permission → 放行（null）；
     - 在权限表内但未声明 → 返回 permission_denied（稳定失败）；
     - method 不在映射表     → null（不 gate 未映射方法）；
     - 身份在 PRIVILEGED_PLUGIN_IDS（histopilot）→ 放行；
     - 未知插件 ID / 缺身份 → permission_denied（fail-closed）。

   同窗口阶段插件仍可直接触达 host 全局对象；本门只挡住自报 ID 绕过权限表。
   真正的安全边界需要 iframe sandbox + postMessage source 绑定。
   ========================================================================= */
(function (root, factory) {
  "use strict";
  if (typeof module === "object" && module && typeof module.exports === "object") {
    module.exports = factory();
  } else {
    root.PluginPermissions = root.PluginPermissions || factory();
  }
})(typeof self !== "undefined" ? self : this, function () {
  "use strict";

  // Plugin→Host request method → 所需 manifest permission（与 docs §7.1 权限枚举对齐）。
  var METHOD_PERMISSIONS = {
    "slide.getCurrent": "slide:metadata:read",
    "selection.getBbox": "slide:metadata:read",
    "viewer.navigate": "viewer:navigate",
    "viewer.highlight": "viewer:navigate",
    "annotation.create": "annotation:write",
    "annotation.read": "annotation:read",
    "annotation.focus": "viewer:navigate",
  };

  // 仅这些 ID 可走特权通道。未知 ID fail-closed，禁止靠「不在表内」冒充 histopilot。
  var PRIVILEGED_PLUGIN_IDS = ["histopilot"];

  // 校验某插件声明的 permissions 是否覆盖调用 method 所需权限。
  //   declaredPerms：插件 manifest.permissions（数组）
  //   method：Plugin→Host request method 名
  // 返回 null（允许）或 {code:"permission_denied", message 含 method 与所需 permission,
  // retryable:false}。method 不在映射表 → null（不 gate 未映射方法）。
  function checkPermission(declaredPerms, method) {
    var need = METHOD_PERMISSIONS[method];
    if (!need) return null; // 未映射方法不 gate
    var declared = Array.isArray(declaredPerms) ? declaredPerms : [];
    if (declared.indexOf(need) !== -1) return null; // 已声明 → 放行
    return {
      code: "permission_denied",
      message: "插件缺少权限 " + need + " 以调用 " + method,
      retryable: false,
    };
  }

  // host 权限门：未知 ID fail-closed；表内走 checkPermission；特权名单显式放行。
  function gatePermission(pluginId, method, table) {
    if (!pluginId) {
      return {
        code: "permission_denied",
        message: "缺少插件身份",
        retryable: false,
      };
    }
    var permsTable = table || {};
    var declared = permsTable[pluginId];
    if (Array.isArray(declared)) {
      return checkPermission(declared, method);
    }
    if (PRIVILEGED_PLUGIN_IDS.indexOf(pluginId) !== -1) return null;
    return {
      code: "permission_denied",
      message: "未知插件身份 " + pluginId,
      retryable: false,
    };
  }

  return {
    METHOD_PERMISSIONS: METHOD_PERMISSIONS,
    PRIVILEGED_PLUGIN_IDS: PRIVILEGED_PLUGIN_IDS,
    checkPermission: checkPermission,
    gatePermission: gatePermission,
  };
});
