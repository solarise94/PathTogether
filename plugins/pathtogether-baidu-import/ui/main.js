/* =========================================================================
   PathTogether Baidu Import 插件 UI（C5-B：grant 引导面板）

   只做一件事：引导用户把「用户导入委托 grant」交给插件后端——
     1. 用户在平台用户面创建 grant：POST /api/plugin/import-grants
        （Cookie session + CSRF；body {plugin_id, project_id, ttl_seconds?}，
        返回一次性展示的 grant_id，pig_ 前缀）；
     2. 把 grant_id 交给插件（本面板生成 grants 文件行/env 种子，管理员
        写入 <SHARE_DATA_DIR>/plugin-work/<installation_id>/grants.json）。

   刻意保持最小（现有插件 UI 约定：纯 DOM fixed 面板、不依赖平台私有
   selector、不读取 window 平台字段）。grant_id 是凭证：本 UI 只在本机
   生成粘贴文本，不发送任何网络请求。
   ========================================================================= */
(function () {
  "use strict";

  var PANEL_ID = "pt-baidu-import-panel";

  function boot() {
    if (document.getElementById(PANEL_ID)) return;

    var panel = document.createElement("div");
    panel.id = PANEL_ID;
    panel.setAttribute("style", [
      "position:fixed", "right:16px", "bottom:16px", "z-index:2147483000",
      "width:360px", "max-width:calc(100vw - 32px)",
      "background:#111827", "color:#e5e7eb",
      "border:1px solid #374151", "border-radius:10px",
      "font:13px/1.5 system-ui,sans-serif", "padding:14px",
      "box-shadow:0 8px 30px rgba(0,0,0,.35)"
    ].join(";"));

    var title = document.createElement("div");
    title.textContent = "百度网盘导入插件";
    title.setAttribute("style", "font-weight:600;margin-bottom:8px");
    panel.appendChild(title);

    var steps = document.createElement("ol");
    steps.setAttribute("style",
      "padding-left:18px;margin:0 0 10px;color:#9ca3af");
    [
      "在平台「导入委托」面板选择目标项目创建 grant（POST /api/plugin/import-grants），获得一次性 grant_id（pig_ 前缀）。",
      "在下方填入项目 ID 与 grant_id，生成登记行。",
      "把登记行交给插件后端运维（写入 grants.json 或 PT_IMPORT_GRANTS env），插件即可在该项目下执行导入。"
    ].forEach(function (t) {
      var li = document.createElement("li");
      li.textContent = t;
      li.setAttribute("style", "margin-bottom:4px");
      steps.appendChild(li);
    });
    panel.appendChild(steps);

    function field(labelText, placeholder) {
      var wrap = document.createElement("label");
      wrap.setAttribute("style", "display:block;margin-bottom:6px");
      var label = document.createElement("span");
      label.textContent = labelText;
      label.setAttribute("style", "display:block;color:#9ca3af");
      var input = document.createElement("input");
      input.type = "text";
      input.placeholder = placeholder;
      input.setAttribute("style", [
        "width:100%", "box-sizing:border-box", "margin-top:2px",
        "background:#1f2937", "color:#e5e7eb",
        "border:1px solid #374151", "border-radius:6px", "padding:4px 6px"
      ].join(";"));
      wrap.appendChild(label);
      wrap.appendChild(input);
      panel.appendChild(wrap);
      return input;
    }

    var project = field("项目 ID", "prj_…");
    var grant = field("grant_id（pig_…）", "pig_…");

    var btn = document.createElement("button");
    btn.type = "button";
    btn.textContent = "生成登记行";
    btn.setAttribute("style", [
      "background:#2563eb", "color:#fff", "border:0", "border-radius:6px",
      "padding:6px 10px", "cursor:pointer", "margin-top:2px"
    ].join(";"));
    panel.appendChild(btn);

    var out = document.createElement("textarea");
    out.readOnly = true;
    out.setAttribute("style", [
      "display:none", "width:100%", "box-sizing:border-box",
      "margin-top:8px", "background:#1f2937", "color:#d1d5db",
      "border:1px solid #374151", "border-radius:6px",
      "padding:4px 6px", "min-height:48px", "font:12px/1.45 monospace"
    ].join(";"));
    panel.appendChild(out);

    btn.addEventListener("click", function () {
      var p = (project.value || "").trim();
      var g = (grant.value || "").trim();
      if (!/^pig_[A-Za-z0-9_-]{4,128}$/.test(g)) {
        out.style.display = "block";
        out.value = "grant_id 形态应为 pig_ 前缀（pig_xxxxxxxx…）";
        return;
      }
      if (!p) {
        out.style.display = "block";
        out.value = "请填写目标项目 ID";
        return;
      }
      out.style.display = "block";
      out.value =
        '# grants.json（<SHARE_DATA_DIR>/plugin-work/<installation_id>/grants.json，0600）\n' +
        JSON.stringify(JSON.parse('{"' + p + '":"' + g + '"}'), null, 2) +
        '\n# 或 env 种子：PT_IMPORT_GRANTS=' + p + ':' + g;
      out.select();
      try { document.execCommand("copy"); } catch (e) { /* 剪贴板失败不影响展示 */ }
    });

    document.body.appendChild(panel);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot);
  } else {
    boot();
  }
})();
