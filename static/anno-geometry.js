/* =========================================================================
   HP_AnnoGeometry —— 标注纯几何 helper（design §5.4 / §6.2，主站 app.js 与
   分享页 share.js 共用；无 DOM / 无 i18n 依赖，经典 script 挂 window）。

   职责边界：只做与坐标系无关的纯几何与「图像坐标 → 画布坐标」回调投影；
   不做命中路由、不发网络请求、不持有标注状态。polygon 与 freehand 共用
   点列算法，但类型字符串由调用方保留（polygon 绝不改写成 freehand 保存）。

   坐标约定：
     - level-0 图像坐标（x 向右、y 向下，整数由调用方 round）；
     - 屏幕/画布坐标由调用方注入 imgToCanvas(ix, iy) 回调投影——OSD 的
       imageToViewerElementCoordinates 自带旋转/镜像，矩形因此按四顶点投影
       （§6.2：旋转/镜像下不能拿两个对角点拼屏幕轴对齐框）。
   ========================================================================= */
(function () {
  "use strict";

  /** 点列外接框 {x,y,w,h}；空/非法点列返回 null。 */
  function pointsBBox(points) {
    if (!points || !points.length) return null;
    var minx = Infinity, miny = Infinity, maxx = -Infinity, maxy = -Infinity;
    for (var i = 0; i < points.length; i++) {
      var x = Number(points[i][0]), y = Number(points[i][1]);
      if (!isFinite(x) || !isFinite(y)) return null;
      if (x < minx) minx = x;
      if (y < miny) miny = y;
      if (x > maxx) maxx = x;
      if (y > maxy) maxy = y;
    }
    return { x: minx, y: miny, w: maxx - minx, h: maxy - miny };
  }

  /** 点到线段距离（任意同坐标系）。 */
  function pointSegDist(px, py, x1, y1, x2, y2) {
    var dx = x2 - x1, dy = y2 - y1;
    var len2 = dx * dx + dy * dy;
    if (len2 <= 0) return Math.hypot(px - x1, py - y1);
    var t = ((px - x1) * dx + (py - y1) * dy) / len2;
    if (t < 0) t = 0; else if (t > 1) t = 1;
    return Math.hypot(px - (x1 + t * dx), py - (y1 + t * dy));
  }

  /** 射线法：点是否在多边形内（闭合多边形，pts 为 [[x,y],...]）。 */
  function pointInPolygon(px, py, pts) {
    var inside = false;
    for (var i = 0, j = pts.length - 1; i < pts.length; j = i++) {
      var xi = pts[i][0], yi = pts[i][1], xj = pts[j][0], yj = pts[j][1];
      var intersect = ((yi > py) !== (yj > py)) &&
        (px < (xj - xi) * (py - yi) / ((yj - yi) || 1e-9) + xi);
      if (intersect) inside = !inside;
    }
    return inside;
  }

  /** 线段严格相交判定（不含共享端点的相邻边；退化长度 0 视为不相交）。 */
  function segmentsIntersect(a1, a2, b1, b2) {
    function cross(ox, oy, ax, ay, bx, by) {
      return (ax - ox) * (by - oy) - (ay - oy) * (bx - ox);
    }
    function onSeg(px, py, qx, qy, rx, ry) {
      return Math.min(px, qx) - 1e-9 <= rx && rx <= Math.max(px, qx) + 1e-9 &&
             Math.min(py, qy) - 1e-9 <= ry && ry <= Math.max(py, qy) + 1e-9 &&
             Math.abs(cross(px, py, qx, qy, rx, ry)) <= 1e-9;
    }
    var d1 = cross(b1[0], b1[1], b2[0], b2[1], a1[0], a1[1]);
    var d2 = cross(b1[0], b1[1], b2[0], b2[1], a2[0], a2[1]);
    var d3 = cross(a1[0], a1[1], a2[0], a2[1], b1[0], b1[1]);
    var d4 = cross(a1[0], a1[1], a2[0], a2[1], b2[0], b2[1]);
    if (((d1 > 0 && d2 < 0) || (d1 < 0 && d2 > 0)) &&
        ((d3 > 0 && d4 < 0) || (d3 < 0 && d4 > 0))) return true;
    if (Math.abs(d1) <= 1e-9 && onSeg(b1[0], b1[1], b2[0], b2[1], a1[0], a1[1])) return true;
    if (Math.abs(d2) <= 1e-9 && onSeg(b1[0], b1[1], b2[0], b2[1], a2[0], a2[1])) return true;
    if (Math.abs(d3) <= 1e-9 && onSeg(a1[0], a1[1], a2[0], a2[1], b1[0], b1[1])) return true;
    if (Math.abs(d4) <= 1e-9 && onSeg(a1[0], a1[1], a2[0], a2[1], b2[0], b2[1])) return true;
    return false;
  }

  /** 简单多边形自交校验（闭合环；相邻边共享端点不计；n<3 恒 false）。
   * 「完成轮廓编辑」时必须校验，服务端同一规则（拒绝自交、不静默修正）。 */
  function polygonSelfIntersects(pts) {
    if (!pts || pts.length < 3) return false;
    var n = pts.length;
    for (var i = 0; i < n; i++) {
      var a1 = pts[i], a2 = pts[(i + 1) % n];
      for (var j = i + 1; j < n; j++) {
        if (j === i) continue;
        // 相邻边（共享端点）跳过
        if ((j + 1) % n === i || (i + 1) % n === j) continue;
        var b1 = pts[j], b2 = pts[(j + 1) % n];
        if (segmentsIntersect(a1, a2, b1, b2)) return true;
      }
    }
    return false;
  }

  /** 整体平移点列（不 clamp；边界约束由调用方处理）。返回新数组。 */
  function translatePoints(points, dx, dy) {
    return (points || []).map(function (p) {
      return [p[0] + dx, p[1] + dy];
    });
  }

  /** 以 (ax, ay) 为锚点等比缩放点列。scale <= 0 返回原列副本。 */
  function scalePointsAbout(points, ax, ay, scale) {
    var s = Number(scale);
    if (!isFinite(s) || s <= 0) return (points || []).map(function (p) { return [p[0], p[1]]; });
    return (points || []).map(function (p) {
      return [ax + (p[0] - ax) * s, ay + (p[1] - ay) * s];
    });
  }

  /** 把点列整体平移回切片内（min corner <0 或超右/下边界时）。
   * 返回 null 表示无需平移；否则返回 {dx, dy}。用于 freehand/polygon
   * 缩放后的越界回移（保持形状，不裁剪点）。 */
  function pointsShiftIntoRect(points, width, height) {
    if (!points || !points.length) return null;
    var bb = pointsBBox(points);
    if (!bb) return null;
    var dx = 0, dy = 0;
    if (bb.x < 0) dx = -bb.x;
    if (bb.y < 0) dy = -bb.y;
    if (width != null && bb.x + bb.w + dx > width) dx = width - bb.x - bb.w;
    if (height != null && bb.y + bb.h + dy > height) dy = height - bb.y - bb.h;
    if (!dx && !dy) return null;
    return { dx: dx, dy: dy };
  }

  /**
   * 矩形四顶点经 imgToCanvas 投影后的屏幕四边形（§6.2 旋转/镜像投影）。
   * imgToCanvas(ix, iy) → {x, y}；返回 [[x,y]×4]（tl, tr, br, bl 顺序）。
   */
  function rectQuad(x, y, w, h, imgToCanvas) {
    return [
      imgToCanvas(x, y),
      imgToCanvas(x + w, y),
      imgToCanvas(x + w, y + h),
      imgToCanvas(x, y + h),
    ].map(function (p) { return [p.x, p.y]; });
  }

  /** 屏幕四边形/多边形命中：内部（射线法）或边线距离 ≤ tol。 */
  function pointInQuad(px, py, quad, tol) {
    if (!quad || quad.length < 3) return false;
    var t = tol == null ? 0 : Number(tol);
    if (t > 0) {
      for (var i = 0; i < quad.length; i++) {
        var a = quad[i], b = quad[(i + 1) % quad.length];
        if (pointSegDist(px, py, a[0], a[1], b[0], b[1]) <= t) return true;
      }
    }
    return pointInPolygon(px, py, quad);
  }

  /** 屏幕多边形外接框（供标签/工具条定位）。 */
  function quadBBox(quad) {
    return pointsBBox(quad);
  }

  /** 深拷贝点列（undo 快照用）。 */
  function clonePoints(points) {
    return (points || []).map(function (p) { return [p[0], p[1]]; });
  }

  var AG = {
    pointsBBox: pointsBBox,
    pointSegDist: pointSegDist,
    pointInPolygon: pointInPolygon,
    segmentsIntersect: segmentsIntersect,
    polygonSelfIntersects: polygonSelfIntersects,
    translatePoints: translatePoints,
    scalePointsAbout: scalePointsAbout,
    pointsShiftIntoRect: pointsShiftIntoRect,
    rectQuad: rectQuad,
    pointInQuad: pointInQuad,
    quadBBox: quadBBox,
    clonePoints: clonePoints,
  };

  if (typeof module !== "undefined" && module.exports) {
    module.exports = AG;  // vitest / Node 侧直接 require
  }
  if (typeof window !== "undefined") {
    window.HP_AnnoGeometry = AG;  // 浏览器经典 script 挂载
  }
})();
