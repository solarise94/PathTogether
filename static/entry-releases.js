/* 首页「更新内容」：从受版本控制的 releases.json 渲染已发布条目。 */
(function () {
  "use strict";

  function t(key, fallback) {
    var i18n = window.HP_I18N;
    if (i18n && typeof i18n.t === "function") {
      var v = i18n.t(key);
      if (v && v !== key) return v;
    }
    return fallback || key;
  }

  function lang() {
    var i18n = window.HP_I18N;
    if (i18n && typeof i18n.getLang === "function") return i18n.getLang();
    return "zh";
  }

  function publishedReleases(data) {
    var list = (data && data.releases) || [];
    return list.filter(function (r) { return r && r.published === true; })
      .slice()
      .sort(function (a, b) {
        return String(b.date || "").localeCompare(String(a.date || ""));
      });
  }

  function itemsFor(release, lng) {
    var items = (release && release.items) || {};
    var chosen = items[lng] || items.zh || items.en || [];
    return Array.isArray(chosen) ? chosen : [];
  }

  function el(tag, className, text) {
    var node = document.createElement(tag);
    if (className) node.className = className;
    if (text != null) node.textContent = text;
    return node;
  }

  function renderRelease(release, expanded) {
    var lng = lang();
    var wrap = expanded ? el("article", "whats-new-release is-latest") : document.createElement("details");
    if (!expanded) wrap.className = "whats-new-release";
    var heading = expanded ? el("div", "whats-new-head") : el("summary", "whats-new-head");
    var ver = el("span", "whats-new-version", String(release.version || ""));
    var date = el("time", "whats-new-date", String(release.date || ""));
    if (release.date) date.setAttribute("datetime", release.date);
    heading.appendChild(ver);
    heading.appendChild(date);
    wrap.appendChild(heading);
    var ul = el("ul", "whats-new-items");
    itemsFor(release, lng).forEach(function (line) {
      ul.appendChild(el("li", "", line));
    });
    wrap.appendChild(ul);
    return wrap;
  }

  function render(data, mount) {
    mount.replaceChildren();
    var list = publishedReleases(data);
    if (!list.length) {
      mount.appendChild(el("p", "whats-new-empty", t("entry.whatsnew.empty", "暂无已发布的更新。")));
      return;
    }
    list.forEach(function (release, i) {
      mount.appendChild(renderRelease(release, i === 0));
    });
  }

  function boot() {
    if (typeof document === "undefined") return;
    if (document.documentElement.getAttribute("data-page") !== "entry") return;
    var mount = document.getElementById("whats-new-list");
    if (!mount) return;
    var cache = null;
    function paint() { if (cache) render(cache, mount); }
    document.addEventListener("hp-lang-change", paint);
    fetch("/static/releases.json", { credentials: "same-origin" })
      .then(function (r) { if (!r.ok) throw new Error("releases " + r.status); return r.json(); })
      .then(function (data) { cache = data; paint(); })
      .catch(function () {
        mount.replaceChildren(el("p", "whats-new-empty", t("entry.whatsnew.empty", "暂无已发布的更新。")));
      });
  }

  window.HP_EntryReleases = {
    publishedReleases: publishedReleases,
    itemsFor: itemsFor,
    render: render,
  };

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", boot);
  else boot();
})();
