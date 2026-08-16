/* =========================================================================
   Sample Annotator 示例插件（Stage 5-2，docs §7.1/§7.5）
   -------------------------------------------------------------------------
   演示通用 PluginSDK（plugins/sdk/ui/bridge-client.js）：
     - 启动时 negotiate() 握手；version_incompatible → 显示错误条并停止后续逻辑；
     - slide.getCurrent / viewer.navigate / annotation.create 走 bridge.request；
     - 越权演示：调 manifest 未声明权限的 annotation.read，验证稳定失败
       （host 权限门回 permission_denied 信封，插件 UI 展示错误）。
   完全不依赖 HistoPilot 源码：不读 window.HistoPilot 任何字段，UI 用纯 DOM
   （append 一个 fixed 面板），不使用平台私有 selector。

   面板窗口控制（2026-08-16）：嵌入模式自建面板带标题栏 [–] 收起 / [×] 关闭；
   关闭后右下角出现重开胶囊。状态记忆在 localStorage（不可用时静默降级为无记忆）。
   独立 ui/index.html 自带 #sa-panel（页面 CSS 提供样式），只接线按钮、不加窗口控制。
   ========================================================================= */
(function () {
  "use strict";
  if (window.PluginSDK && window.PluginSDK.createPluginBridge) {
    bootstrap();
  } else {
    // SDK 未加载（直开本页但静态路径缺失）→ 兜底提示，不崩
    showErr("PluginSDK 未加载，请经平台正常加载插件脚本");
  }

  var CLOSE_KEY = "sa-panel-closed";
  var COLLAPSE_KEY = "sa-panel-collapsed";
  function getFlag(key) {
    try { return window.localStorage.getItem(key) === "1"; } catch (e) { return false; }
  }
  function setFlag(key, on) {
    try { on ? window.localStorage.setItem(key, "1") : window.localStorage.removeItem(key); } catch (e) {}
  }

  function bootstrap() {
    var bridge = window.PluginSDK.createPluginBridge({ pluginId: "sample-annotator" });
    var out = null; // 指向 #sa-out；面板未构建（用户已关闭）时为 null，日志静默丢弃

    function log(msg) {
      if (out) out.textContent = (out.textContent ? out.textContent + "\n" : "") + msg;
    }
    function showErr(msg) {
      var el = document.getElementById("sa-err");
      if (el) { el.style.display = "block"; el.textContent = msg; }
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

    function wireButtons() {
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

    // 标题栏 [–]/[×]：只作用于嵌入模式自建面板（attachChrome 在 buildPanel 内调用）
    function attachChrome(panel) {
      var body = panel.querySelector("#sa-body");
      var collapseBtn = panel.querySelector("#sa-collapse");
      var closeBtn = panel.querySelector("#sa-close");
      if (!body || !collapseBtn || !closeBtn) return;
      var collapsed = getFlag(COLLAPSE_KEY);
      function applyCollapsed() {
        body.style.display = collapsed ? "none" : "";
        collapseBtn.textContent = collapsed ? "+" : "–";
        collapseBtn.title = collapsed ? "展开面板" : "收起面板";
      }
      applyCollapsed();
      collapseBtn.addEventListener("click", function () {
        collapsed = !collapsed;
        setFlag(COLLAPSE_KEY, collapsed);
        applyCollapsed();
      });
      closeBtn.addEventListener("click", function () {
        setFlag(CLOSE_KEY, true);
        panel.remove();
        out = null;
        showPill();
      });
    }

    function buildPanel() {
      // 独立 ui/index.html：面板已在页面 HTML 里，只接线按钮（样式由页面 CSS 提供）
      if (!document.getElementById("sa-panel")) {
        var panel = document.createElement("div");
        panel.id = "sa-panel";
        panel.style.cssText = "position:fixed;top:16px;right:16px;width:280px;max-height:80vh;overflow:auto;"
          + "background:#262e3d;color:#e6e9ef;border:1px solid #3a465c;border-radius:10px;"
          + "padding:14px;z-index:9999;box-shadow:0 8px 24px rgba(0,0,0,.4);"
          + "font:13px/1.5 system-ui,sans-serif";
        var chromeBtnStyle = "flex:none;width:26px;height:24px;padding:0;font-size:14px;line-height:1;"
          + "background:#33415e;color:#e6e9ef;border:1px solid #46566f;border-radius:6px;cursor:pointer";
        panel.innerHTML = '<div style="display:flex;align-items:center;gap:8px;margin:0 0 10px">'
          + '<h3 style="margin:0;flex:1;font-size:15px;color:#7ab6ff">Sample Annotator（示例插件）</h3>'
          + '<button id="sa-collapse" type="button" title="收起面板" style="' + chromeBtnStyle + '">–</button>'
          + '<button id="sa-close" type="button" title="关闭（右下角可重新打开）" style="' + chromeBtnStyle + '">×</button>'
          + '</div>'
          + '<div id="sa-body">'
          + '<button id="sa-read" type="button">读取当前切片</button>'
          + '<button id="sa-navigate" type="button">导航到中心</button>'
          + '<button id="sa-create" type="button">创建测试标注</button>'
          + '<button id="sa-overreach" type="button">越权演示（annotation.read）</button>'
          + '<pre id="sa-out" style="white-space:pre-wrap;font-size:12px;margin:10px 0 0">就绪…</pre>'
          + '<div id="sa-err" style="display:none;margin-top:10px;padding:8px;background:#4a2a2a;'
          + 'border:1px solid #7a3a3a;border-radius:6px;font-size:12px;color:#ffb3b3"></div>'
          + '</div>';
        document.body.appendChild(panel);
        var btns = panel.querySelectorAll("#sa-body > button");
        for (var i = 0; i < btns.length; i++) {
          btns[i].style.cssText = "display:block;width:100%;margin:6px 0;padding:8px 10px;font-size:13px;"
            + "background:#33415e;color:#e6e9ef;border:1px solid #46566f;border-radius:6px;"
            + "cursor:pointer;text-align:left";
        }
        attachChrome(panel);
      }
      wireButtons();
      out = document.getElementById("sa-out");
    }

    function showPill() {
      if (document.getElementById("sa-reopen")) return;
      var pill = document.createElement("button");
      pill.id = "sa-reopen";
      pill.type = "button";
      pill.textContent = "◈ 打开示例插件";
      pill.style.cssText = "position:fixed;right:16px;bottom:16px;z-index:9998;padding:8px 14px;"
        + "background:#33415e;color:#e6e9ef;border:1px solid #46566f;border-radius:999px;"
        + "font:13px system-ui,sans-serif;cursor:pointer;box-shadow:0 4px 12px rgba(0,0,0,.35)";
      pill.addEventListener("click", function () {
        setFlag(CLOSE_KEY, false);
        pill.remove();
        buildPanel();
      });
      document.body.appendChild(pill);
    }

    if (document.getElementById("sa-panel") || !getFlag(CLOSE_KEY)) {
      buildPanel();
    } else {
      showPill();
    }
  }
})();
