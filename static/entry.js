/* 主页读片演示：2× / 10× / 20× / 40× 同心实图。
 * 同一视野连续放大；只在倍率正好翻倍、两张图 FOV 重合时换图（无淡入淡出）。
 * 高倍到位后在图上画蓝框，框边逐字流出解读。自动循环，无播放按钮。 */
(function () {
  "use strict";
  var MEDIA = "/static/entry-media/";
  var LOOP_HOLD_MS = 3000;
  var BOX_DELAY_MS = 280;
  var CHAR_MS = 26;
  var TICK_MS = 32;
  var SETTLE_MS = 420;

  var LEFT = [
    { src: "tcga-left-2.jpg", mag: 2.5 },
    { src: "tcga-left-10.jpg", mag: 10 },
    { src: "tcga-left-20.jpg", mag: 20 },
    { src: "tcga-left-40.jpg", mag: 40 }
  ];
  var UPPER = [
    { src: "tcga-upper-2.jpg", mag: 2.5 },
    { src: "tcga-upper-10.jpg", mag: 10 },
    { src: "tcga-upper-20.jpg", mag: 20 },
    { src: "tcga-upper-40.jpg", mag: 40 }
  ];
  var RIGHT = [
    { src: "tcga-right-2.jpg", mag: 2.5 },
    { src: "tcga-right-10.jpg", mag: 10 },
    { src: "tcga-right-20.jpg", mag: 20 },
    { src: "tcga-right-40.jpg", mag: 40 }
  ];
  var SCENES = [
    { kind: "wide", src: "tcga-session-base.jpg", mag: 0.5, d: 1600, stage: 0, status: 0 },
    { kind: "zoom", stack: LEFT, zoom: 2600, hold: 3400, stage: 1, box: "a", statusLow: 1, statusHigh: 2 },
    { kind: "zoom", stack: UPPER, zoom: 2200, hold: 1400, stage: 2, statusLow: 3, statusHigh: 4 },
    { kind: "zoom", stack: RIGHT, zoom: 2600, hold: 3600, stage: 3, box: "b", statusLow: 5, statusHigh: 6 },
    { kind: "wide", src: "tcga-session-base.jpg", mag: 1, scale: 1.35, ox: 58, oy: 57, d: 3800, stage: 4, both: true, status: 7 }
  ];

  function t(key) { return window.HP_I18N ? window.HP_I18N.t(key) : key; }
  function clamp(v, lo, hi) { return Math.max(lo, Math.min(hi, v)); }
  function ease(k) { return k * k * (3 - 2 * k); }

  function pickLayer(stack, mag) {
    var i = 0;
    while (i + 1 < stack.length && mag >= stack[i + 1].mag - 1e-4) i += 1;
    return { src: stack[i].src, mag: stack[i].mag, scale: mag / stack[i].mag, idx: i };
  }

  function init(root) {
    var fromImg = root.querySelector("[data-hp-img-from]");
    var toImg = root.querySelector("[data-hp-img-to]");
    var bufs = [fromImg, toImg];
    var cur = 0;
    var zoom = root.querySelector("[data-hp-zoom]");
    var scale = root.querySelector("[data-hp-scale]");
    var scaleBar = root.querySelector("[data-hp-scale-bar]");
    var status = root.querySelector("[data-hp-status]");
    var phase = root.querySelector("[data-hp-phase]");
    var zoomIn = root.querySelector("[data-hp-zoom-in]");
    var zoomOut = root.querySelector("[data-hp-zoom-out]");
    var boxes = {};
    var streams = {};
    Array.prototype.forEach.call(root.querySelectorAll("[data-hp-box]"), function (el) {
      boxes[el.getAttribute("data-hp-box")] = el;
      streams[el.getAttribute("data-hp-box")] = el.querySelector("[data-hp-stream]");
    });
    var inView = false, autoResume = false, started = false, ready = false;
    var loading = false;
    var imageWidth;
    function mediaUrl(src) {
      return MEDIA + src.replace(/\.jpg$/, "-" + imageWidth + ".webp");
    }
    var motion = window.matchMedia("(prefers-reduced-motion: reduce)");
    var step = 0, elapsed = 0, last = 0, raf = 0, streamTimer = 0;
    var playing = false, manual = false;
    var shownBox = null;
    var statusIdx = 0;

    function copy(el, key) {
      el.setAttribute("data-i18n", key);
      el.textContent = t(key);
    }
    function magText(mag) {
      if (mag < 1) return "≈0.5×";
      if (mag < 4) return "≈2×";
      if (mag < 15) return "≈10×";
      if (mag < 30) return "≈20×";
      return "≈40×";
    }
    function scaleFor(mag) {
      scale.textContent = mag >= 30 ? "50 µm" : mag >= 15 ? "100 µm" : mag >= 4 ? "200 µm" : mag >= 1 ? "500 µm" : "2 mm";
      scaleBar.style.width = mag >= 30 ? "42px" : mag >= 15 ? "48px" : mag >= 4 ? "56px" : "72px";
    }
    function pose(el, spec) {
      el.classList.toggle("is-wide", !!spec.wide);
      el.style.transformOrigin = (spec.ox == null ? 50 : spec.ox) + "% " + (spec.oy == null ? 50 : spec.oy) + "%";
      el.style.transform = "translateZ(0) scale(" + (spec.scale || 1) + ")";
    }
    function showFrame(spec) {
      var a = bufs[cur];
      var b = bufs[1 - cur];
      if (a.getAttribute("data-key") !== spec.src) {
        if (b.getAttribute("data-key") !== spec.src) {
          b.src = mediaUrl(spec.src);
          b.setAttribute("data-key", spec.src);
        }
        pose(b, spec);
        b.style.opacity = "1";
        a.style.opacity = "0";
        cur = 1 - cur;
      } else {
        pose(a, spec);
        a.style.opacity = "1";
        b.style.opacity = "0";
      }
    }
    function prefetch(src) {
      var b = bufs[1 - cur];
      if (b.getAttribute("data-key") === src) return;
      b.src = mediaUrl(src);
      b.setAttribute("data-key", src);
    }
    function stopStream() {
      if (streamTimer) { window.clearInterval(streamTimer); streamTimer = 0; }
    }
    function streamInto(kind) {
      stopStream();
      var el = streams[kind];
      var full = t("entry.principle.review." + kind);
      if (motion.matches) { el.textContent = full; return; }
      el.textContent = "";
      var i = 0;
      streamTimer = window.setInterval(function () {
        i += 1;
        el.textContent = full.slice(0, i);
        if (i >= full.length) stopStream();
      }, CHAR_MS);
    }
    function setBox(kind, on, wide) {
      var el = boxes[kind];
      el.classList.toggle("is-on", on);
      el.classList.toggle("is-wide", !!wide);
      if (!on) {
        streams[kind].textContent = "";
        if (shownBox === kind) shownBox = null;
      }
    }
    function showInterpret(kind) {
      if (shownBox === kind) return;
      shownBox = kind;
      setBox(kind, true, false);
      streamInto(kind);
    }
    function setStatus(idx) {
      if (statusIdx === idx && status.getAttribute("data-i18n") === "entry.principle.status.nav." + idx) return;
      statusIdx = idx;
      copy(status, "entry.principle.status.nav." + idx);
    }
    function applySceneChrome(n, mag) {
      step = n;
      var scene = SCENES[n];
      copy(phase, "entry.principle.nav.s" + (scene.stage + 1));
      zoom.textContent = magText(mag);
      scaleFor(mag);
      root.dataset.phase = String(scene.stage);
      root.querySelectorAll("[data-hp-steps-nav] [data-step]").forEach(function (li) {
        var s = Number(li.dataset.step);
        li.classList.toggle("is-on", s === scene.stage);
        li.classList.toggle("is-done", s < scene.stage);
      });
      zoomIn.disabled = !ready || n >= SCENES.length - 1;
      zoomOut.disabled = !ready || n <= 0;
      if (scene.kind === "zoom") {
        setStatus(mag >= 20 ? scene.statusHigh : scene.statusLow);
      } else {
        setStatus(scene.status);
      }
      if (scene.both) {
        stopStream();
        shownBox = null;
        setBox("a", true, true);
        setBox("b", true, true);
        streams.a.textContent = t("entry.principle.pin.a");
        streams.b.textContent = t("entry.principle.pin.b");
      } else if (!scene.box || mag < 35) {
        setBox("a", false);
        setBox("b", false);
        shownBox = null;
        stopStream();
      } else if (scene.box) {
        setBox(scene.box === "a" ? "b" : "a", false);
      }
    }
    function frameOf(n, elapsedMs) {
      var scene = SCENES[n];
      if (scene.kind === "wide") {
        return {
          src: scene.src,
          wide: true,
          scale: scene.scale || 1,
          ox: scene.ox,
          oy: scene.oy,
          mag: scene.mag,
          atEnd: elapsedMs >= scene.d
        };
      }
      var stack = scene.stack;
      var mag0 = stack[0].mag;
      var mag1 = stack[stack.length - 1].mag;
      var mag = mag0;
      if (elapsedMs > SETTLE_MS) {
        var k = clamp((elapsedMs - SETTLE_MS) / scene.zoom, 0, 1);
        mag = mag0 * Math.pow(mag1 / mag0, ease(k));
      }
      var layer = pickLayer(stack, mag);
      return {
        src: layer.src,
        wide: false,
        scale: layer.scale,
        mag: mag,
        atEnd: elapsedMs >= SETTLE_MS + scene.zoom,
        holdEnd: elapsedMs >= SETTLE_MS + scene.zoom + scene.hold
      };
    }
    function paintScene(n, elapsedMs) {
      var fr = frameOf(n, elapsedMs);
      showFrame(fr);
      // Prepare the next layer only after the current frame has been installed.
      var stack = SCENES[n].stack;
      if (stack) {
        var next = stack[pickLayer(stack, fr.mag).idx + 1];
        if (next) prefetch(next.src);
      }
      applySceneChrome(n, fr.mag);
      var scene = SCENES[n];
      if (scene.box && fr.atEnd && elapsedMs >= SETTLE_MS + scene.zoom + BOX_DELAY_MS) {
        showInterpret(scene.box);
      }
      return fr;
    }
    function sceneDuration(n) {
      var scene = SCENES[n];
      if (scene.kind === "wide") return scene.d + (n === SCENES.length - 1 ? LOOP_HOLD_MS : 0);
      return SETTLE_MS + scene.zoom + scene.hold;
    }
    function stopLoop() {
      playing = false;
      if (raf) { window.clearTimeout(raf); raf = 0; }
      last = 0;
    }
    function tick() {
      if (!playing) return;
      var now = Date.now();
      if (last) elapsed += now - last;
      last = now;
      paintScene(step, elapsed);
      if (elapsed >= sceneDuration(step)) {
        elapsed = 0;
        if (step === SCENES.length - 1) {
          setBox("a", false);
          setBox("b", false);
          paintScene(0, 0);
        } else {
          paintScene(step + 1, 0);
        }
      }
      raf = window.setTimeout(tick, TICK_MS);
    }
    function start() {
      if (playing || !ready || manual) return;
      if (motion.matches) {
        applyReducedMotion();
        return;
      }
      started = true;
      playing = true;
      last = Date.now();
      raf = window.setTimeout(tick, TICK_MS);
    }
    function applyReducedMotion() {
      stopLoop();
      started = true;
      paintScene(SCENES.length - 1, SCENES[SCENES.length - 1].d);
    }
    function manualZoom(dir) {
      if (!ready) return;
      autoResume = false;
      started = true;
      stopLoop();
      manual = true;
      stopStream();
      var n = clamp(step + dir, 0, SCENES.length - 1);
      var scene = SCENES[n];
      var elaps = scene.kind === "zoom" ? SETTLE_MS + scene.zoom + BOX_DELAY_MS + 40 : scene.d;
      paintScene(n, elaps);
      copy(status, "entry.principle.manual");
    }
    zoomIn.addEventListener("click", function () { manualZoom(1); });
    zoomOut.addEventListener("click", function () { manualZoom(-1); });
    function visibility() {
      if (inView && !document.hidden && !ready && !loading) loadMedia();
      if (!inView || document.hidden) {
        if (playing) { autoResume = true; stopLoop(); }
        stopStream();
        if (shownBox) streams[shownBox].textContent = t("entry.principle.review." + shownBox);
      } else if (autoResume && !manual) {
        autoResume = false;
        start();
      } else if (!started) {
        start();
      }
    }
    document.addEventListener("visibilitychange", visibility);
    var stageEl = root.querySelector(".slide-stage");
    function stageVisible() {
      var r = stageEl.getBoundingClientRect();
      var vis = Math.max(0, Math.min(r.bottom, window.innerHeight) - Math.max(r.top, 0));
      return r.height > 0 && vis / r.height >= 0.4;
    }
    if ("IntersectionObserver" in window) {
      var observer = new IntersectionObserver(function (entries) {
        inView = entries[0].isIntersecting && entries[0].intersectionRatio >= 0.4;
        visibility();
      }, { threshold: [0, 0.4] });
      observer.observe(stageEl);
    }
    if (!("IntersectionObserver" in window)) {
      window.addEventListener("scroll", function () { inView = stageVisible(); visibility(); }, { passive: true });
      window.addEventListener("resize", function () { inView = stageVisible(); visibility(); });
    }
    if (stageVisible()) inView = true;
    document.addEventListener("hp-lang-change", function () {
      if (manual) copy(status, "entry.principle.manual");
      else if (ready && started) {
        copy(status, "entry.principle.status.nav." + statusIdx);
        copy(phase, "entry.principle.nav.s" + (SCENES[step].stage + 1));
      } else copy(status, ready ? "entry.principle.status.idle" : "entry.principle.loading");
      stopStream();
      if (shownBox) streams[shownBox].textContent = t("entry.principle.review." + shownBox);
      if (SCENES[step] && SCENES[step].both) {
        streams.a.textContent = t("entry.principle.pin.a");
        streams.b.textContent = t("entry.principle.pin.b");
      }
    });

    function loadMedia() {
      loading = true;
      // Lock one resolution for this visit so every layer has the same pixel grid.
      // Data Saver uses the compact stack; all layers decode before playback.
      var compact = navigator.connection && navigator.connection.saveData;
      imageWidth = compact || stageEl.clientWidth * Math.min(window.devicePixelRatio || 1, 2) <= 640 ? 640 : 1024;
      copy(status, "entry.principle.loading");
      var urls = ["tcga-session-base.jpg"];
      [LEFT, UPPER, RIGHT].forEach(function (stack) {
        stack.forEach(function (s) { urls.push(s.src); });
      });
      function loadImage(name) {
        return new Promise(function (resolve, reject) {
          var im = new Image();
          im.onload = function () {
            var decoded = im.decode ? im.decode() : Promise.resolve();
            decoded.then(function () {
              resolve();
            }, reject);
          };
          im.onerror = reject;
          im.src = mediaUrl(name);
        });
      }
      // Bound decode concurrency to keep image preparation responsive on small devices.
      var pending = 0;
      function loadNext() {
        if (pending >= urls.length) return Promise.resolve();
        return loadImage(urls[pending++]).then(loadNext);
      }
      Promise.all([loadNext(), loadNext()]).then(function () {
        ready = true;
        paintScene(0, 0);
        copy(status, "entry.principle.status.idle");
        visibility();
      }).catch(function () { copy(status, "entry.principle.load.error"); });
    }
    copy(status, "entry.principle.loading");
    visibility();
  }

  function boot() {
    if (document.documentElement.dataset.page === "entry") {
      document.querySelectorAll("[data-hp-stage]").forEach(init);
    }
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", boot);
  else boot();
})();
