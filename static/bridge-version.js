/* =========================================================================
   BridgeVersion —— 桥协议版本共享工具（Stage 5-1，docs §7.0/§7.5）

   host-bridge.js（平台 host 端）与 plugins/histopilot/ui/bridge-client.js
   （插件端）共用本模块的 compat / 常量，避免两份兼容逻辑漂移。UMD 风格：
     - 浏览器 <script> → 挂 window.BridgeVersion；
     - node（vitest）   → module.exports = { ... }。

   两种版本语义（与 Python plugins/sdk/manifest.py 的 manifest 加载期协商区分）：
     - compat(v, local)：运行时每条消息的协议主版本校验——**强制同 major**；
     - negotiate(remote, {supportedMajors})：握手期版本协商——remote 的 major
       落在 supportedMajors 即兼容（缺省 SUPPORTED_MAJORS=[1]）。negotiate **不
       抛异常**，返回 {ok:true,protocolVersion} 或 {ok:false,error:{code,...}}。

   N/N-1 说明：bridge 运行时默认只接受同 major（SUPPORTED_MAJORS=[1]，即 major 1），
   这比 manifest 加载期的 N/N-1（接受当前与前一 major）更严格——消息格式一旦不兼容
   无法降级。N/N-1 兼容逻辑通过可选 {supportedMajors} 参数注入测试（如 [2,1]）。
   ========================================================================= */
(function (root, factory) {
  "use strict";
  if (typeof module === "object" && module && typeof module.exports === "object") {
    module.exports = factory();
  } else {
    // 浏览器：挂到 window（root）。同名全局已存在则不覆盖。
    root.BridgeVersion = root.BridgeVersion || factory();
  }
})(typeof self !== "undefined" ? self : this, function () {
  "use strict";

  var PROTOCOL_VERSION = "1.0.0";
  // 运行时消息协议接受的 major（默认仅当前 major；N/N-1 经 negotiate 第二参注入测试）。
  var SUPPORTED_MAJORS = [1];

  // 解析版本字符串的 major；非法/缺失返回 null（不抛）。
  function parseMajor(v) {
    var s = String(v == null ? "" : v).trim();
    if (!s) return null;
    var head = s.split(".")[0];
    // 仅当前缀是纯数字时才认可（避免 "v1.0.0" / "1a" 这类被 parseInt 宽松吃掉）
    if (!/^\d+$/.test(head)) return null;
    var n = parseInt(head, 10);
    return isFinite(n) ? n : null;
  }

  // 运行时同 major 兼容校验（替代旧版内联 compat）。local 缺省取 PROTOCOL_VERSION。
  function compat(v, local) {
    var a = parseMajor(v);
    var b = parseMajor(local == null ? PROTOCOL_VERSION : local);
    if (a === null || b === null) return false;
    return a === b;
  }

  // 握手期版本协商：remote major ∈ supportedMajors 即兼容。negotiate 不抛异常。
  function negotiate(remoteVersion, opts) {
    var supported = (opts && opts.supportedMajors) ? opts.supportedMajors : SUPPORTED_MAJORS;
    var major = parseMajor(remoteVersion);
    var ok = major !== null && supported.indexOf(major) !== -1;
    if (ok) {
      return { ok: true, protocolVersion: PROTOCOL_VERSION };
    }
    return {
      ok: false,
      error: {
        code: "version_incompatible",
        message: "bridge 协议版本不兼容：插件="
          + (remoteVersion == null ? "(missing)" : remoteVersion)
          + " 平台支持的 major=[" + supported.join(",") + "]",
        retryable: false,
      },
    };
  }

  return {
    PROTOCOL_VERSION: PROTOCOL_VERSION,
    SUPPORTED_MAJORS: SUPPORTED_MAJORS,
    parseMajor: parseMajor,
    compat: compat,
    negotiate: negotiate,
  };
});
