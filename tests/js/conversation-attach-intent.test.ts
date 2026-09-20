/**
 * 工单 E（plan §6）：查看器右键「加入会话草稿」——conversation.attachIntent
 * 桥事件契约测试。
 *
 * 加载真实 static/app.js（harness 同 viewer-get-viewport.test.ts：最小 DOM +
 * fetch mock + HostBridgeHost stub），经生产路径驱动：init → 切片行 click →
 * openSlide → viewer viewport 就绪 → #anno-canvas 上派发 contextmenu → 点击
 * 菜单项。锁定：
 *   - 源契约：app.js 注册 conversation.attachIntent 发射、anno-canvas 绑定
 *     contextmenu 处理器、onAnnoPointerDown 在进入绘制分支前拦截右键
 *     （button=2 不启动绘制/编辑/选中）；
 *   - 行为：右键 → 弹 #viewer-ctx-menu；点「将当前视野加入会话」→ HostBridge
 *     emit conversation.attachIntent，载荷含 level-0 bbox、与视野中心分列的
 *     click_point（右键点位绝不冒充中心）、slide、frozen_at；
 *   - 行为：显示标注层 + 右键命中箭头标注 → 菜单含标注项；点「将此标注加入
 *     会话」→ kind=marker，bbox=标注包围盒，annotation_id/revision/type/
 *     geometry/note 原样冻结；
 *   - 无命中且无选中：菜单不出现可点击的标注项。
 */
import { afterEach, describe, expect, it, vi } from "vitest";
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const appSrc = readFileSync(resolve(here, "../../static/app.js"), "utf8");
const require = createRequire(import.meta.url);

const SLIDE = "ctx-slide.ndpi";

// ---------- 最小假元素（裁自 viewer-get-viewport.test.ts harness） ----------
interface FakeEl extends Record<string, unknown> {
  id: string;
  className: string;
  hidden: boolean;
  title: string;
  type: string;
  checked: boolean;
  style: Record<string, string>;
  dataset: Record<string, string>;
  textContent: string;
  innerHTML: string;
  value: string;
  children: FakeEl[];
  parentNode: FakeEl | null;
  classList: {
    add: (...names: string[]) => void;
    remove: (...names: string[]) => void;
    contains: (n: string) => boolean;
    toggle: (n: string, force?: boolean) => boolean;
  };
  setAttribute: (k: string, v: string) => void;
  getAttribute: (k: string) => string | null;
  addEventListener: (type: string, cb: (e?: unknown) => void) => void;
  dispatch: (type: string, evt?: unknown) => void;
  appendChild: (c: unknown) => unknown;
  insertBefore: (c: unknown, ref: unknown) => unknown;
  remove: () => void;
  querySelector: () => null;
  querySelectorAll: () => FakeEl[];
  closest: () => null;
  getBoundingClientRect: () => { width: number; height: number; left: number; top: number };
  getContext: () => Record<string, unknown>;
}

function fakeEl(id = ""): FakeEl {
  const listeners: Record<string, Array<(e?: unknown) => void>> = {};
  const children: FakeEl[] = [];
  const classes = new Set<string>();
  const attrs = new Map<string, string>();
  const el: FakeEl = {
    id,
    hidden: false,
    title: "",
    type: "",
    checked: false,
    style: {},
    dataset: {},
    textContent: "",
    innerHTML: "",
    value: "",
    children,
    parentNode: null,
    classList: {
      add: (...names) => names.forEach((n) => classes.add(n)),
      remove: (...names) => names.forEach((n) => classes.delete(n)),
      contains: (n) => classes.has(n),
      toggle: (n, force) => {
        const on = force === undefined ? !classes.has(n) : !!force;
        if (on) classes.add(n);
        else classes.delete(n);
        return on;
      },
    },
    setAttribute: (k, v) => void attrs.set(k, String(v)),
    getAttribute: (k) => (attrs.has(k) ? attrs.get(k) as string : null),
    addEventListener: (type, cb) => void (listeners[type] ||= []).push(cb),
    dispatch: (type, evt) => (listeners[type] || []).forEach((cb) => cb(evt)),
    appendChild: (c) => {
      if (c && typeof c === "object" && "dispatch" in (c as object)) {
        (c as FakeEl).parentNode = el;
        children.push(c as FakeEl);
      }
      return c;
    },
    insertBefore: (c) => {
      if (c && typeof c === "object" && "dispatch" in (c as object)) {
        (c as FakeEl).parentNode = el;
        children.push(c as FakeEl);
      }
      return c;
    },
    remove() {},
    querySelector: () => null,
    querySelectorAll: () => [],
    closest: () => null,
    getBoundingClientRect: () => ({ width: 800, height: 600, left: 0, top: 0 }),
    // 2D context 桩：任意方法 no-op（Proxy 兜底，绘制工单新增 API 也不缺），
    // 只有 measureText 需要真实宽度（文本布局分支）。
    getContext: () => new Proxy({}, {
      get: (_t, prop) => {
        if (prop === "measureText") return (t: string) => ({ width: String(t).length * 6 });
        return () => undefined;
      },
    }) as unknown as Record<string, unknown>,
  };
  Object.defineProperty(el, "className", {
    get: () => Array.from(classes).join(" "),
    set: (v: string) => {
      classes.clear();
      String(v).split(/\s+/).filter(Boolean).forEach((n) => classes.add(n));
    },
    configurable: true,
  });
  return el;
}

function jsonResponse(body: unknown) {
  return Promise.resolve({
    ok: true,
    status: 200,
    clone() { return this; },
    json: () => Promise.resolve(body),
  });
}

interface ImageRect { x: number; y: number; width: number; height: number }

/** OpenSeadragon viewport 桩：可编程视野 + 屏幕↔图像恒等映射（canvas px = image px）。 */
function makeMockViewport(viewportRect: ImageRect, imageRect: ImageRect) {
  return {
    getBounds: (_immediate?: boolean) => ({ ...viewportRect }),
    viewportToImageRectangle: (_b: unknown) => ({ ...imageRect }),
    imageToViewportRectangle: (x: number, y: number, w: number, h: number) => ({ x, y, width: w, height: h }),
    fitBounds() {},
    imageToViewerElementCoordinates: (p: { x: number; y: number }) => ({ x: p.x, y: p.y }),
    viewerElementToImageCoordinates: (p: { x: number; y: number }) => ({ x: p.x, y: p.y }),
  };
}

interface BootResult {
  emitted: Array<{ type: string; payload: Record<string, unknown> }>;
  bridgeHandlers: Record<string, (payload: unknown, env?: { pluginInstallationId?: string }) => unknown>;
  created: FakeEl[];
  doc: {
    getElementById: (id: string) => FakeEl;
    createElement: () => FakeEl;
    body: FakeEl;
  };
  fakeViewer: { viewport: unknown; open: (url: string) => void };
}

function bootApp(): BootResult {
  const els: Record<string, FakeEl> = {};
  const created: FakeEl[] = [];
  const docListeners: Record<string, Array<() => void>> = {};
  const rafCbs: Array<() => void> = [];
  const emitted: Array<{ type: string; payload: Record<string, unknown> }> = [];
  const bridgeHandlers: Record<string, (payload: unknown, env?: { pluginInstallationId?: string }) => unknown> = {};
  const openHandlers: Record<string, Array<(e?: unknown) => void>> = {};

  const fakeViewer = {
    container: {
      style: {} as Record<string, string>,
      getBoundingClientRect: () => ({ width: 800, height: 600, left: 0, top: 0 }),
      insertBefore() {},
    },
    canvas: {},
    viewport: null as unknown,
    addHandler(type: string, fn: (e?: unknown) => void) {
      (openHandlers[type] ||= []).push(fn);
    },
    open(_url: string) {
      (openHandlers["open"] || []).forEach((fn) => fn({}));
    },
    setMouseNavEnabled() {},
  };

  const fetchImpl = vi.fn((url: string) => {
    const u = String(url);
    if (u.includes("/api/slide/" + SLIDE + "/info")) {
      return jsonResponse({ name: SLIDE, width: 1000, height: 800, mpp_x: 0.5, mpp_y: 0.5, mpp_source: "native" });
    }
    if (u.includes("/api/annotations?slide=")) {
      return jsonResponse({ annotations: [{
        label: "病理医生", count: 1,
        items: [{ index: 0, token: "tok1", slide: SLIDE, type: "arrow",
                  x1: 100, y1: 100, x2: 300, y2: 200, ts: 1700000000,
                  note: "核异型区域", annotation_id: "anno-7", revision: 2 }],
      }] });
    }
    if (u.includes("/api/annotations")) return jsonResponse({ by_slide: {} });
    if (u.includes("/api/slides")) {
      return jsonResponse([{ name: SLIDE, width: 1000, height: 800, mpp_x: 0.5, mpp_source: "native" }]);
    }
    if (u.includes("/api/projects")) {
      return jsonResponse([{ pid: "p1", name: "P1", slides: [SLIDE], slide_count: 1, roi_count: 0 }]);
    }
    if (u.includes("/api/share/list")) return jsonResponse({ shares: [] });
    return jsonResponse({});
  }) as unknown as typeof fetch;

  const doc = {
    readyState: "loading",
    cookie: "",
    getElementById(id: string) {
      if (!els[id]) els[id] = fakeEl(id);
      return els[id];
    },
    createElement: () => {
      const el = fakeEl();
      created.push(el);
      return el;
    },
    addEventListener(type: string, cb: () => void) {
      (docListeners[type] ||= []).push(cb);
    },
    querySelector: () => null,
    querySelectorAll: () => [] as FakeEl[],
    body: fakeEl("body"),
  };

  const w: Record<string, unknown> = {
    HP_I18N: { t: (k: string) => k, getLang: () => "zh" },
    HP_ViewerCore: { create: () => fakeViewer },
    HP_API: {},
    HistoPilot: {},   // 插件已加载（hpReady=true，emit 走 stub）
    PluginPermissions: {},
    SVS_PLUGIN_PERMISSIONS: { histopilot: [] },
    HostBridgeHost: {
      onRequest(method: string, fn: (payload: unknown, env?: { pluginInstallationId?: string }) => unknown) {
        bridgeHandlers[method] = fn;
      },
      onEvent() {},
      emit(type: string, payload: Record<string, unknown>) {
        emitted.push({ type, payload });
      },
      request() {
        return Promise.reject({ code: "plugin_disabled" });
      },
    },
    fetch: fetchImpl,
    location: { href: "http://local/", pathname: "/" },
    matchMedia: () => ({ matches: false, addEventListener() {}, addListener() {} }),
    requestAnimationFrame: (cb: () => void) => {
      rafCbs.push(cb);
      return rafCbs.length;
    },
    addEventListener() {},
    localStorage: null,
  };

  (globalThis as { document: unknown }).document = doc;
  (globalThis as { window: unknown }).window = w;
  (globalThis as { fetch: typeof fetch }).fetch = fetchImpl;
  (globalThis as { HP_ViewerCore?: unknown }).HP_ViewerCore = { create: () => fakeViewer };
  // screenToImg / imgToCanvas 需要的全局 OpenSeadragon.Point 桩
  (globalThis as { OpenSeadragon?: unknown }).OpenSeadragon = {
    Point: function (this: { x: number; y: number }, x: number, y: number) { this.x = x; this.y = y; },
  };

  new Function("window", "document", "fetch", "location", appSrc)(w, doc, fetchImpl, (w as { location: unknown }).location);
  (docListeners["DOMContentLoaded"] || []).forEach((cb) => cb());
  while (rafCbs.length) (rafCbs.shift() as () => void)();

  return { emitted, bridgeHandlers, created, doc, fakeViewer };
}

async function settle(times = 20) {
  for (let i = 0; i < times; i++) await Promise.resolve();
}

function findSlideRow(created: FakeEl[]): FakeEl {
  const row = created.find((e) => e.classList.contains("slide-row"));
  if (!row) throw new Error("harness: 未渲染出 .slide-row");
  return row;
}

function stopEvent() {
  return { preventDefault() {}, stopPropagation() {}, button: 2 };
}

/** 在 #anno-canvas 上派发右键并返回菜单元素（未弹菜单 → null）。 */
function openCtxMenu(app: BootResult, clientX: number, clientY: number): FakeEl | null {
  const canvas = app.doc.getElementById("anno-canvas");
  canvas.dispatch("contextmenu", Object.assign({ clientX, clientY }, stopEvent()));
  const menu = app.created.find((e) => e.id === "viewer-ctx-menu" && e.style.display === "block");
  return menu || null;
}

function clickMenuItem(menu: FakeEl, kind: string) {
  const item = (menu.children as FakeEl[]).find((b) => b.dataset.attachKind === kind);
  if (!item) throw new Error("harness: 菜单项缺失 " + kind);
  item.dispatch("click", { preventDefault() {}, stopPropagation() {} });
}

afterEach(() => {
  vi.restoreAllMocks();
  delete (globalThis as { window?: unknown }).window;
  delete (globalThis as { document?: unknown }).document;
  delete (globalThis as { fetch?: unknown }).fetch;
  delete (globalThis as { HP_ViewerCore?: unknown }).HP_ViewerCore;
  delete (globalThis as { OpenSeadragon?: unknown }).OpenSeadragon;
});

describe("工单 E：右键加入会话（conversation.attachIntent）", () => {
  it("源契约：注册 attachIntent 发射 + anno-canvas contextmenu 处理器 + 右键不进绘制分支", () => {
    expect(appSrc).toContain("conversation.attachIntent");
    expect(appSrc).toContain('c.addEventListener("contextmenu", onViewerContextMenu)');
    // onAnnoPointerDown：右键守卫必须出现在绘制/矩形分支之前
    const fnStart = appSrc.indexOf("function onAnnoPointerDown");
    expect(fnStart).toBeGreaterThan(-1);
    const fnText = appSrc.slice(fnStart);
    const drawBranch = fnText.indexOf("if (state.drawMode)");
    expect(drawBranch).toBeGreaterThan(-1);
    const head = fnText.slice(0, drawBranch);
    expect(head).toContain("e.button === 2");
    // 载荷契约字段：bbox 与 click_point/center 分列
    expect(appSrc).toContain("click_point: clickPt || null");
    expect(appSrc).toContain('hpEmit("conversation.attachIntent", payload)');
  });

  it("右键空白（无命中无选中）→ 仅视野项；点击 → emit viewport 载荷（bbox + 独立 click_point）", async () => {
    const app = bootApp();
    await settle();
    findSlideRow(app.created).dispatch("click");
    await settle();
    // 视野：图像像素 (10,20,500,400)（钳界不触发）
    app.fakeViewer.viewport = makeMockViewport(
      { x: 0.01, y: 0.025, width: 0.5, height: 0.5 },
      { x: 10, y: 20, width: 500, height: 400 },
    );
    const menu = openCtxMenu(app, 150, 130);
    expect(menu).toBeTruthy();
    // 未显示标注层且无选中 → 不出现可点击的标注项
    const kinds = (menu!.children as FakeEl[]).map((b) => b.dataset.attachKind);
    expect(kinds).toEqual(["viewport"]);
    clickMenuItem(menu!, "viewport");
    const evt = app.emitted.find((e) => e.type === "conversation.attachIntent");
    expect(evt).toBeTruthy();
    const p = evt!.payload as Record<string, unknown>;
    expect(p.kind).toBe("viewport");
    expect(p.slide).toBe(SLIDE);
    // bbox：与 viewer.getViewport 同一实现的 level-0 取整
    expect(p.bbox).toEqual({ x: 10, y: 20, w: 500, h: 400 });
    // click_point 是右键点位（canvas px = image px 的桩映射），与中心分列
    expect(p.click_point).toEqual({ x: 150, y: 130 });
    expect(p.center).toEqual({ x: 260, y: 220 });
    expect(p.center).not.toEqual(p.click_point);
    expect(typeof p.frozen_at).toBe("number");
    expect(p.annotation_id).toBeNull();
  });

  it("显示标注层 + 右键命中箭头 → 标注项；点击 → emit marker 载荷（标注元数据冻结）", async () => {
    const app = bootApp();
    await settle();
    findSlideRow(app.created).dispatch("click");
    await settle();
    app.fakeViewer.viewport = makeMockViewport(
      { x: 0, y: 0, width: 1, height: 1 },
      { x: 0, y: 0, width: 1000, height: 800 },
    );
    // 打开「显示全部标记」→ state.showAnno（箭头 (100,100)-(300,200) 可命中）
    app.doc.getElementById("anno-all-btn").dispatch("click");
    // 右键点 (150,130)：到箭头线段距离 ≈ 4.5px（≤8 容差）→ 命中
    const menu = openCtxMenu(app, 150, 130);
    expect(menu).toBeTruthy();
    const kinds = (menu!.children as FakeEl[]).map((b) => b.dataset.attachKind);
    expect(kinds).toContain("viewport");
    expect(kinds).toContain("marker");
    clickMenuItem(menu!, "marker");
    const evt = app.emitted.find((e) => e.type === "conversation.attachIntent");
    expect(evt).toBeTruthy();
    const p = evt!.payload as Record<string, unknown>;
    expect(p.kind).toBe("marker");
    // bbox = 标注包围盒（发送时作为冻结 viewport，不重查实时视野）
    expect(p.bbox).toEqual({ x: 100, y: 100, w: 200, h: 100 });
    expect(p.annotation_id).toBe("anno-7");
    expect(p.revision).toBe(2);
    expect(p.type).toBe("arrow");
    expect(p.geometry).toEqual({ x1: 100, y1: 100, x2: 300, y2: 200 });
    expect(p.note).toBe("核异型区域");
    expect(p.click_point).toEqual({ x: 150, y: 130 });
    expect(p.center).toEqual({ x: 200, y: 150 });   // 中心由 bbox 推导，非右键点位
  });

  it("viewer 未就绪 → 不弹菜单（无可附内容不开死菜单）", async () => {
    const app = bootApp();
    await settle();
    findSlideRow(app.created).dispatch("click");
    await settle();
    // viewport 保持 null：level0ViewportBbox → null → 无菜单项 → 不开菜单
    expect(app.fakeViewer.viewport).toBeNull();
    const canvas = app.doc.getElementById("anno-canvas");
    canvas.dispatch("contextmenu", Object.assign({ clientX: 100, clientY: 100 }, stopEvent()));
    const menu = app.created.find((e) => e.id === "viewer-ctx-menu" && e.style.display === "block");
    expect(menu).toBeFalsy();
    expect(app.emitted.some((e) => e.type === "conversation.attachIntent")).toBe(false);
  });
});
