/* =========================================================================
   共享 OpenSeadragon Viewer：正式版与 Demo 使用同一套缩放/旋转/镜像/倍率徽章。
   标注、ROI、底图缩略图仍由 app.js 在官方模式叠加。
   ========================================================================= */
(function (root) {
  "use strict";

  function osdDefaults(element) {
    return {
      element: element,
      showNavigationControl: false,
      imageLoaderLimit: 8,
      placeholderFillStyle: null,
      compositeOperation: "source-over",
      minZoomImageRatio: 0.5,
      maxZoomPixelRatio: 10,
      minPixelRatio: 0.4,
      defaultZoomLevel: 0,
      immediateRender: false,
      preload: false,
      wrapHorizontal: false,
      wrapVertical: false,
      preserveImageSizeOnResize: true,
      pixelsPerWheelLine: 40,
      gestureSettingsMouse: {
        scrollToZoom: true,
        clickToZoom: false,
        dblClickToZoom: true,
      },
      gestureSettingsTouch: {
        pinchToZoom: true,
        flickEnabled: false,
      },
      animationTime: 0.3,
      visibilityRatio: 0.1,
      prefixUrl: "",
    };
  }

  function create(element, extra) {
    extra = extra || {};
    var opts = osdDefaults(element || extra.element);
    Object.keys(extra).forEach(function (k) {
      if (k === "element") return;
      opts[k] = extra[k];
    });
    var viewer = OpenSeadragon(opts);
    if (viewer.container) viewer.container.style.backgroundColor = "#262a30";
    return viewer;
  }

  function formatMag(mag) {
    if (mag >= 1000000) return (mag / 1000000).toFixed(1).replace(/\.0$/, "") + "M×";
    if (mag >= 10000) return Math.round(mag / 1000) + "k×";
    if (mag >= 10) return Math.round(mag) + "×";
    if (mag >= 1) return mag.toFixed(1) + "×";
    return mag.toFixed(2) + "×";
  }

  function zoomText(viewer, mppX) {
    try {
      if (!viewer || !viewer.viewport || !viewer.source) return "—";
      var zoom = viewer.viewport.getZoom(true);
      var containerW = viewer.viewport.getContainerSize().x;
      var imgW = viewer.source.dimensions.x;
      var imageZoom = (zoom * containerW) / imgW;
      var mpp = Number(mppX);
      if (mpp > 0 && imageZoom > 0) {
        return formatMag(imageZoom * (10 / mpp));
      }
      return Math.round(imageZoom * 100) + "%";
    } catch (e) {
      return "—";
    }
  }

  function bindViewTools(viewer, els) {
    els = els || {};
    if (els.zoomIn) {
      els.zoomIn.addEventListener("click", function () {
        if (!viewer || !viewer.viewport) return;
        viewer.viewport.zoomBy(1.4);
        viewer.viewport.applyConstraints();
      });
    }
    if (els.zoomOut) {
      els.zoomOut.addEventListener("click", function () {
        if (!viewer || !viewer.viewport) return;
        viewer.viewport.zoomBy(1 / 1.4);
        viewer.viewport.applyConstraints();
      });
    }
    if (els.rotate) {
      els.rotate.addEventListener("click", function () {
        if (!viewer || !viewer.viewport) return;
        viewer.viewport.setRotation(viewer.viewport.getRotation() + 90);
      });
    }
    if (els.flip) {
      els.flip.addEventListener("click", function () {
        if (!viewer || !viewer.viewport) return;
        if (viewer.viewport.toggleFlip) viewer.viewport.toggleFlip();
        else viewer.viewport.setFlip(!viewer.viewport.getFlip());
      });
    }
    if (els.reset) {
      els.reset.addEventListener("click", function () {
        if (!viewer || !viewer.viewport) return;
        viewer.viewport.setRotation(0);
        if (viewer.viewport.getFlip && viewer.viewport.getFlip()) {
          if (viewer.viewport.toggleFlip) viewer.viewport.toggleFlip();
          else viewer.viewport.setFlip(false);
        }
        viewer.viewport.goHome(true);
      });
    }
  }

  root.HP_ViewerCore = {
    create: create,
    formatMag: formatMag,
    zoomText: zoomText,
    bindViewTools: bindViewTools,
    osdDefaults: osdDefaults,
  };
})(window);
