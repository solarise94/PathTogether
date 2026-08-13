/* =========================================================================
   PluginPermissions —— 平台 host 端插件权限门（Stage 5-2，docs §7.1/§7.5）

   UMD 双端：
     - 浏览器 <script> → 挂 window.PluginPermissions；
     - node（vitest）   → module.exports = { METHOD_PERMISSIONS, checkPermission }。

   host 端 app.js registerHostBridgeHandlers 在**每个被 gate 的 host 方法入口**先查
   env.pluginInstallationId 对应插件声明的 manifest permissions：
     - 声明了所需 permission → 放行（null）；
     - 在权限表内但未声明 → 返回 permission_denied（稳定失败）；
     - method 不在映射表     → null（不 gate 未映射方法）；
     - 插件不在 SVS_PLUGIN_PERMISSIONS 表内（如 histopilot 内置特权插件）→ 放行
       （该判定在 app.js 的 gatePluginPermission 完成，本模块只管 method→permission）。

   注：annotation.read → annotation:read 是为「越权演示」新增的映射——通用 SDK 插件
   若未声明 annotation:read，调 annotation.read 即稳定 permission_denied（三套工作按钮
   只依赖 slide:metadata:read / viewer:navigate / annotation:write，不受影响）。
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
  };

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

  return { METHOD_PERMISSIONS: METHOD_PERMISSIONS, checkPermission: checkPermission };
});
