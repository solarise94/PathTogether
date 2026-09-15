/* HistoPilot 介绍页交互动画：高倍观察→低倍识别，以及调用分析插件。
   仅挂在 data-page=entry 上；尊重 prefers-reduced-motion。 */
(function () {
  "use strict";

  var NAV = [
    { x: 38, y: 36, z: 3.2, zoom: "20×", scale: "50 µm", status: "entry.principle.status.nav.0" },
    { x: 58, y: 42, z: 3.2, zoom: "20×", scale: "50 µm", status: "entry.principle.status.nav.1" },
    { x: 50, y: 48, z: 1.05, zoom: "2×", scale: "500 µm", status: "entry.principle.status.nav.2" },
    { x: 50, y: 48, z: 1.05, zoom: "2×", scale: "500 µm", status: "entry.principle.status.nav.3" }
  ];
  var PLUGIN = [
    { x: 46, y: 40, z: 2.4, zoom: "10×", scale: "100 µm", status: "entry.principle.status.plugin.0", scan: false, count: false },
    { x: 46, y: 40, z: 2.4, zoom: "10×", scale: "100 µm", status: "entry.principle.status.plugin.1", scan: true, count: false },
    { x: 46, y: 40, z: 2.4, zoom: "10×", scale: "100 µm", status: "entry.principle.status.plugin.2", scan: true, count: true },
    { x: 46, y: 40, z: 2.4, zoom: "10×", scale: "100 µm", status: "entry.principle.status.plugin.3", scan: false, count: true }
  ];
  var STEP_MS = 1600;

  function t(key, fallback) {
    if (window.HP_I18N && typeof window.HP_I18N.t === "function") {
      var s = window.HP_I18N.t(key);
      if (s && s !== key) return s;
    }
    return fallback || key;
  }

  function reduceMotion() {
    return window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  }

  function init(root) {
    var world = root.querySelector("[data-hp-world]");
    var fov = root.querySelector("[data-hp-fov]");
    var scan = root.querySelector("[data-hp-scan]");
    var zoomEl = root.querySelector("[data-hp-zoom]");
    var scaleEl = root.querySelector("[data-hp-scale]");
    var statusEl = root.querySelector("[data-hp-status]");
    var playBtn = root.querySelector("[data-hp-play]");
    var pauseBtn = root.querySelector("[data-hp-pause]");
    var tabs = root.querySelectorAll("[data-hp-tab]");
    var navSteps = root.querySelector("[data-hp-steps-nav]");
    var pluginSteps = root.querySelector("[data-hp-steps-plugin]");
    if (!world || !fov || !playBtn) return;

    var mode = "nav";
    var step = -1;
    var timer = 0;
    var playing = false;

    function frames() { return mode === "plugin" ? PLUGIN : NAV; }

    function apply(i) {
      var f = frames()[i];
      if (!f) return;
      world.style.transform = "scale(" + f.z + ") translate(" + (50 - f.x) + "%, " + (50 - f.y) + "%)";
      fov.style.left = f.x + "%";
      fov.style.top = f.y + "%";
      if (zoomEl) zoomEl.textContent = f.zoom;
      if (scaleEl) scaleEl.textContent = f.scale;
      root.classList.toggle("is-high", f.z >= 2.5);
      root.classList.toggle("is-plugin", mode === "plugin");
      root.classList.toggle("is-scanning", !!f.scan);
      root.classList.toggle("has-count", !!f.count);
      if (scan) scan.style.opacity = f.scan ? "1" : "0";
      if (statusEl) {
        statusEl.setAttribute("data-i18n", f.status);
        statusEl.textContent = t(f.status);
      }
      var list = mode === "plugin" ? pluginSteps : navSteps;
      if (list) {
        list.querySelectorAll("[data-step]").forEach(function (li) {
          var n = Number(li.getAttribute("data-step"));
          li.classList.toggle("is-on", n === i);
          li.classList.toggle("is-done", n < i);
        });
      }
    }

    function idleCopy() {
      return mode === "plugin"
        ? "entry.principle.status.idle.plugin"
        : "entry.principle.status.idle";
    }

    function stop() {
      playing = false;
      if (timer) {
        window.clearInterval(timer);
        timer = 0;
      }
    }

    function showIdle() {
      stop();
      step = -1;
      var f0 = frames()[0];
      apply(0);
      root.classList.remove("is-scanning", "has-count");
      if (scan) scan.style.opacity = "0";
      if (statusEl) {
        statusEl.setAttribute("data-i18n", idleCopy());
        statusEl.textContent = t(idleCopy());
      }
      var list = mode === "plugin" ? pluginSteps : navSteps;
      if (list) {
        list.querySelectorAll("[data-step]").forEach(function (li) {
          li.classList.remove("is-on", "is-done");
        });
      }
      world.style.transform = "scale(" + f0.z + ") translate(" + (50 - f0.x) + "%, " + (50 - f0.y) + "%)";
    }

    function tick() {
      var seq = frames();
      step += 1;
      if (step >= seq.length) {
        stop();
        return;
      }
      apply(step);
    }

    function play() {
      stop();
      step = -1;
      if (reduceMotion()) {
        apply(frames().length - 1);
        return;
      }
      playing = true;
      tick();
      timer = window.setInterval(tick, STEP_MS);
    }

    function setMode(next) {
      if (mode === next) return;
      mode = next;
      tabs.forEach(function (btn) {
        var on = btn.getAttribute("data-hp-tab") === mode;
        btn.classList.toggle("is-on", on);
        btn.setAttribute("aria-pressed", on ? "true" : "false");
      });
      if (navSteps) navSteps.classList.toggle("is-hidden", mode !== "nav");
      if (pluginSteps) pluginSteps.classList.toggle("is-hidden", mode !== "plugin");
      playBtn.setAttribute("data-i18n", mode === "plugin" ? "entry.principle.play.plugin" : "entry.principle.play");
      playBtn.textContent = t(mode === "plugin" ? "entry.principle.play.plugin" : "entry.principle.play");
      showIdle();
    }

    tabs.forEach(function (btn) {
      btn.addEventListener("click", function () {
        setMode(btn.getAttribute("data-hp-tab") === "plugin" ? "plugin" : "nav");
      });
    });
    playBtn.addEventListener("click", play);
    if (pauseBtn) pauseBtn.addEventListener("click", stop);
    document.addEventListener("hp-lang-change", function () {
      playBtn.textContent = t(mode === "plugin" ? "entry.principle.play.plugin" : "entry.principle.play");
      if (pauseBtn) pauseBtn.textContent = t("entry.principle.pause");
      if (statusEl) {
        var key = statusEl.getAttribute("data-i18n");
        if (key) statusEl.textContent = t(key);
      }
    });

    showIdle();
  }

  function boot() {
    if (document.documentElement.getAttribute("data-page") !== "entry") return;
    document.querySelectorAll("[data-hp-stage]").forEach(init);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot);
  } else {
    boot();
  }
})();
