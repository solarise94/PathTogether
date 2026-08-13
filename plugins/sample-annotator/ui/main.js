/* =========================================================================
   Sample Annotator 示例插件（Stage 5-2，docs §7.1/§7.5）
   -------------------------------------------------------------------------
   演示通用 PluginSDK（plugins/sdk/bridge-client.js）：
     - 启动时 negotiate() 握手；version_incompatible → 显示错误条并停止后续逻辑；
     - slide.getCurrent / viewer.navigate / annotation.create 走 bridge.request；
     - 越权演示：调 manifest 未声明权限的 annotation.read，验证稳定失败
       （host 权限门回 permission_denied 信封，插件 UI 展示错误）。
   完全不依赖 HistoPilot 源码：不读 window.HistoPilot 任何字段，UI 用纯 DOM
   （append 一个 fixed 面板），不使用平台私有 selector。
   ========================================================================= */
(function () {
  "use strict";
  if (window.PluginSDK && window.PluginSDK.createPluginBridge) {
    bootstrap();
  } else {
    // SDK 未加载（直开本页但静态路径缺失）→ 兜底提示，不崩
    showErr("PluginSDK 未加载，请经平台正常加载插件脚本");
  }

  function bootstrap() {
    // 面板 DOM 自举：独立 ui/index.html 自带 #sa-panel；平台 index.html 嵌入模式
    // （<script src=".../main.js">）页面没有这些元素——此时自建 fixed 面板 append 到
    // body（纯 DOM，不依赖平台私有 selector）。缺失直接 getElementById 会在
    // addEventListener 处抛 TypeError，嵌入模式整个引导崩掉。
    if (!document.getElementById("sa-panel")) {
      var panel = document.createElement("div");
      panel.id = "sa-panel";
      panel.style.cssText = "position:fixed;top:16px;right:16px;width:280px;max-height:80vh;overflow:auto;"
        + "background:#262e3d;color:#e6e9ef;border:1px solid #3a465c;border-radius:10px;"
        + "padding:14px;z-index:9999;box-shadow:0 8px 24px rgba(0,0,0,.4);"
        + "font:13px/1.5 system-ui,sans-serif";
      panel.innerHTML = '<h3 style="margin:0 0 10px;font-size:15px;color:#7ab6ff">Sample Annotator（示例插件）</h3>'
        + '<button id="sa-read" type="button">读取当前切片</button>'
        + '<button id="sa-navigate" type="button">导航到中心</button>'
        + '<button id="sa-create" type="button">创建测试标注</button>'
        + '<button id="sa-overreach" type="button">越权演示（annotation.read）</button>'
        + '<pre id="sa-out" style="white-space:pre-wrap;font-size:12px;margin:10px 0 0">就绪…</pre>'
        + '<div id="sa-err" style="display:none;margin-top:10px;padding:8px;background:#4a2a2a;'
        + 'border:1px solid #7a3a3a;border-radius:6px;font-size:12px;color:#ffb3b3"></div>';
      document.body.appendChild(panel);
      var btns = panel.querySelectorAll("button");
      for (var i = 0; i < btns.length; i++) {
        btns[i].style.cssText = "display:block;width:100%;margin:6px 0;padding:8px 10px;font-size:13px;"
          + "background:#33415e;color:#e6e9ef;border:1px solid #46566f;border-radius:6px;"
          + "cursor:pointer;text-align:left";
      }
    }
    var bridge = window.PluginSDK.createPluginBridge({ pluginId: "sample-annotator" });
    var out = document.getElementById("sa-out");

    function log(msg) {
      out.textContent = (out.textContent ? out.textContent + "\n" : "") + msg;
    }
    function showErr(msg) {
      var el = document.getElementById("sa-err");
      el.style.display = "block";
      el.textContent = msg;
    }

    // 启动握手：bridge.negotiate。不兼容 → reject {code:"version_incompatible"}，
    // 展示错误条；此后所有 request 也会被 host 以 version_mismatch 稳定拒绝
    // （host 端同 major 强制校验），按钮保留可点用于演示该失败路径。
    bridge.negotiate().then(function () {
      log("桥协议协商成功：protocolVersion=1.0.0");
    }, function (err) {
      showErr("版本协商失败：" + (err && err.code) + " " + (err && err.message || ""));
      return; // 不继续注册后续能力
    });

    document.getElementById("sa-read").addEventListener("click", function () {
      bridge.request("slide.getCurrent").then(function (meta) {
        if (!meta) { log("当前无切片"); return; }
        log("切片: " + (meta.name || "?"));
        log("尺寸: " + meta.width + " x " + meta.height);
        log("MPP: " + (meta.mppX != null ? meta.mppX : "?") + " x " + (meta.mppY != null ? meta.mppY : "?"));
      }, function (err) { log("读取失败: " + (err && err.code) + " " + (err && err.message || "")); });
    });

    document.getElementById("sa-navigate").addEventListener("click", function () {
      // 先取切片元数据计算中心 1/4 区域（level-0 坐标）
      bridge.request("slide.getCurrent").then(function (meta) {
        if (!meta) { log("当前无切片"); return; }
        var w = meta.width, h = meta.height;
        var x = Math.round(w * 0.375), y = Math.round(h * 0.375);
        var rw = Math.round(w * 0.25), rh = Math.round(h * 0.25);
        return bridge.request("viewer.navigate", { x: x, y: y, w: rw, h: rh });
      }).then(function (res) {
        log("已导航到中心 1/4 区域");
      }, function (err) { log("导航失败: " + (err && err.code) + " " + (err && err.message || "")); });
    });

    document.getElementById("sa-create").addEventListener("click", function () {
      bridge.request("slide.getCurrent").then(function (meta) {
        if (!meta) { log("当前无切片"); return; }
        var w = meta.width, h = meta.height;
        var x = Math.round(w * 0.45), y = Math.round(h * 0.45);
        var rw = Math.round(w * 0.1), rh = Math.round(h * 0.1);
        return bridge.request("annotation.create", { text: "SDK 测试标注", x: x, y: y, w: rw, h: rh });
      }).then(function (res) {
        log("标注创建成功: " + (res && res.ok ? "ok" : "?") + " id=" + (res && res.id));
      }, function (err) { log("创建失败: " + (err && err.code) + " " + (err && err.message || "")); });
    });

    // 越权演示：调 manifest 未声明的权限（annotation.read）→ 权限门稳定拒绝
    document.getElementById("sa-overreach").addEventListener("click", function () {
      bridge.request("annotation.read", {}).then(function (res) {
        log("（不应发生）越权调用被放行：" + JSON.stringify(res));
      }, function (err) {
        log("越权调用被拒：" + (err && err.code) + " " + (err && err.message || ""));
      });
    });
  }
})();
