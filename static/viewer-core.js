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

  function uiLang() {
    try {
      if (root.HP_I18N && typeof root.HP_I18N.getLang === "function") {
        return root.HP_I18N.getLang();
      }
    } catch (e) { /* i18n 未加载：中文默认 */ }
    return "zh";
  }

  function zoomText(viewer, mppX) {
    try {
      if (!viewer || !viewer.viewport || !viewer.source) return "—";
      var zoom = viewer.viewport.getZoom(true);
      var containerW = viewer.viewport.getContainerSize().x;
      var imgW = viewer.source.dimensions.x;
      var imageZoom = (zoom * containerW) / imgW;
      // 倍率诚实（F3）：imageZoom>1 即 level-0 像素被拉伸显示（数字放大），
      // 超出光学等效的 portion 如实标注，不冒充更高物镜
      var suffix = imageZoom > 1.02 ? (uiLang() === "en" ? " digital" : " 数字放大") : "";
      var mpp = Number(mppX);
      if (mpp > 0 && imageZoom > 0) {
        return formatMag(imageZoom * (10 / mpp)) + suffix;
      }
      return Math.round(imageZoom * 100) + "%" + suffix;
    } catch (e) {
      return "—";
    }
  }

  // 1:1（F3）：imageZoom=1（1 CSS 像素 / 1 level-0 像素），保留视野中心。
  // 不动 maxZoomPixelRatio/minPixelRatio；越界由 applyConstraints 收敛。
  function zoomToNative(viewer) {
    try {
      if (!viewer || !viewer.viewport || !viewer.source) return false;
      var vp = viewer.viewport;
      var containerW = vp.getContainerSize().x;
      var imgW = viewer.source.dimensions.x;
      if (!(containerW > 0) || !(imgW > 0)) return false;
      // imageZoom = zoom × containerW / imgW → imageZoom=1 ⇔ zoom = imgW/containerW
      vp.zoomTo(imgW / containerW, vp.getCenter());
      vp.applyConstraints();
      return true;
    } catch (e) {
      return false;
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
    if (els.zoomNative) {
      els.zoomNative.addEventListener("click", function () {
        zoomToNative(viewer);
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
    zoomToNative: zoomToNative,
    bindViewTools: bindViewTools,
    osdDefaults: osdDefaults,
  };
})(window);
