/* =========================================================================
   HistoPilot UI bundle —— HTTP 封装（JSON + SSE reader + 401 跳 /login）

   自己实现 apiFetch，不调用平台 apiFetch（Stage 2 验收：插件不依赖平台私有 JS）。
   SSE 字节泵（fetch + ReadableStream + 手动 \n\n 分帧）在 sse.js；本文件只负责普通
   JSON 请求与错误体解析。
   ========================================================================= */
(function () {
  "use strict";
  var HP = window.HistoPilot;
  var t = HP.t;

  // fetch 包装：响应 401 且 body 含 auth_required 时跳登录页（与平台 apiFetch 行为一致）。
  function apiFetch(url, opts) {
    return fetch(url, opts).then(function (resp) {
      if (resp.status === 401) {
        return resp.clone().json().then(
          function (body) {
            if (body && body.error === "auth_required") {
              location.href = "/login?next=" + encodeURIComponent(location.pathname);
            }
            return resp;
          },
          function () { return resp; }
        );
      }
      return resp;
    });
  }

  // 不把代理/Flask 返回的整页 HTML 错误塞进聊天气泡。
  function aiResponseError(resp) {
    return resp.text().then(function (raw) {
      var text = (raw || "").trim();
      try {
        var body = JSON.parse(text);
        if (body && body.error) return String(body.error);
      } catch (e) {}
      var title = text.match(/<title[^>]*>([^<]+)<\/title>/i);
      if (title && title[1]) return "HTTP " + resp.status + " " + title[1].replace(/^\d+\s*/, "");
      if (/<(?:!doctype|html|body)\b/i.test(text)) return t("ai.http.error", { s: resp.status });
      return text || ("HTTP " + resp.status);
    });
  }

  HP.api = apiFetch;
  HP.aiResponseError = aiResponseError;
})();
