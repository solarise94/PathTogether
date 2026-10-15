/**
 * 标记 ⋯ 菜单「✨ 让 AI 解读」（2026-10-15 UX 改版：AI 解读入口在标记上，
 * 不再是 composer 的「+」按钮）。
 *
 * 加载真实 static/app.js（harness 同 conversation-attach-intent.test.ts：最小
 * DOM + fetch mock + HostBridgeHost stub；container 补 addEventListener/appendChild
 * 以绑定 §6.2 捕获路由），经生产路径驱动：init → 开切片 → 显示标注层 → 容器
 * pointerdown/up 选中标注 → 工具条 ⋯ → 点击「✨ 让 AI 解读」。锁定：
 *   - 行为：真实标注的 ⋯ 菜单首项是「让 AI 解读」（aot-askai），点击 →
 *     HostBridge emit conversation.attachIntent，载荷 kind=marker +
 *     intent=interpret + annotation_id/revision/bbox/type/note（与右键
 *     「将此标注加入会话」同一冻结实现 freezeMarkerAttachPayload）；
 *   - 行为：插件未加载（hpReady=false）→ 项禁用，tooltip 为既有「插件未加载」
 *     说明；点击（无监听）不 emit；
 *   - 源契约：draft（临时框）目标不出现解读项——askai 分支位于 openAnnoToolbarMenu
 *     的 draft 早退之后；payload 冻结复用 freezeMarkerAttachPayload，不复制字段；
 *     composer 模板不再有 #ai-task-jump「+」按钮。
 */
import { afterEach, describe, expect, it, vi } from "vitest";
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const appSrc = readFileSync(resolve(here, "../../static/app.js"), "utf8");
const shellSrc = readFileSync(resolve(here, "../../templates/_app_shell.html"), "utf8");
const require = createRequire(import.meta.url);

const SLIDE = "ctx-slide.ndpi";
const SLIDE_ID = "sld_ctx000000001";

// ---------- 最小假元素（裁自 conversation-attach-intent.test.ts harness） ----------
interface FakeEl extends Record<string, unknown> {
  id: string;
  className: string;
  hidden: boolean;
  title: string;
  type: string;
  disabled: boolean;
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
    disabled: false,
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
    remove() {
      const p = el.parentNode;
      if (p) {
        const i = (p.children as FakeEl[]).indexOf(el);
        if (i >= 0) (p.children as FakeEl[]).splice(i, 1);
      }
      el.parentNode = null;
    },
    querySelector: () => null,
    querySelectorAll: () => [],
    closest: () => null,
    getBoundingClientRect: () => ({ width: 800, height: 600, left: 0, top: 0 }),
    // 2D context 桩：任意方法 no-op（Proxy 兜底），只有 measureText 需要真实宽度。
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
  container: FakeEl & { listeners: Record<string, Array<(e?: unknown) => void>>; fire: (type: string, evt?: unknown) => void };
  created: FakeEl[];
  doc: {
    getElementById: (id: string) => FakeEl;
    createElement: () => FakeEl;
    body: FakeEl;
  };
  fakeViewer: { viewport: unknown; open: (url: string) => void };
}

function bootApp(opts: { pluginLoaded?: boolean; aiPending?: boolean } = {}): BootResult {
  const pluginLoaded = opts.pluginLoaded !== false;
  const els: Record<string, FakeEl> = {};
  const created: FakeEl[] = [];
  const docListeners: Record<string, Array<() => void>> = {};
  const rafCbs: Array<() => void> = [];
  const emitted: Array<{ type: string; payload: Record<string, unknown> }> = [];
  const openHandlers: Record<string, Array<(e?: unknown) => void>> = {};

  const containerListeners: Record<string, Array<(e?: unknown) => void>> = {};
  const container = Object.assign(fakeEl("viewer-container"), {
    listeners: containerListeners,
    fire(type: string, evt?: unknown) {
      (containerListeners[type] || []).forEach((fn) => fn(evt));
    },
  }) as FakeEl & { listeners: Record<string, Array<(e?: unknown) => void>>; fire: (type: string, evt?: unknown) => void };
  // container.addEventListener 走 fakeEl 的监听注册（dispatch 由 fire 驱动）
  const realAdd = container.addEventListener;
  container.addEventListener = ((type: string, cb: (e?: unknown) => void) => {
    (containerListeners[type] ||= []).push(cb);
    return realAdd.call(container, type, cb);
  }) as typeof container.addEventListener;
  container.getBoundingClientRect = () => ({ width: 800, height: 600, left: 0, top: 0 });

  const fakeViewer = {
    container,
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
    if (u.includes("/api/slides/" + SLIDE_ID + "/info")) {
      return jsonResponse({ name: SLIDE, slide_id: SLIDE_ID, display_name: "", original_filename: SLIDE, width: 1000, height: 800, mpp_x: 0.5, mpp_y: 0.5, mpp_source: "native" });
    }
    if (u.includes("/api/annotations?slide_id=")) {
      return jsonResponse({ annotations: [{
        label: "病理医生", count: 1,
        items: [{ index: 0, token: "tok1", slide: SLIDE, slide_id: SLIDE_ID, type: "arrow",
                  x1: 100, y1: 100, x2: 300, y2: 200, ts: 1700000000,
                  note: "核异型区域", annotation_id: "anno-7", revision: 2,
                  ...(opts.aiPending ? { source: "ai", review_status: "pending" } : {}) }],
      }] });
    }
    if (u.includes("/api/annotations")) return jsonResponse({ by_slide: {} });
    if (u.includes("/api/slides")) {
      return jsonResponse([{ name: SLIDE, slide_id: SLIDE_ID, display_name: "", width: 1000, height: 800, mpp_x: 0.5, mpp_source: "native" }]);
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
    HP_APP_BOOTSTRAP: { mode: "official", capabilities: { slide_id_api: true } },
    PluginPermissions: {},
    SVS_PLUGIN_PERMISSIONS: { histopilot: [] },
    HostBridgeHost: {
      onRequest() {},
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
  if (pluginLoaded) w.HistoPilot = {};   // hpReady = !!(HostBridgeHost && HistoPilot)

  (globalThis as { document: unknown }).document = doc;
  (globalThis as { window: unknown }).window = w;
  (globalThis as { fetch: typeof fetch }).fetch = fetchImpl;
  (globalThis as { HP_ViewerCore?: unknown }).HP_ViewerCore = { create: () => fakeViewer };
  (globalThis as { OpenSeadragon?: unknown }).OpenSeadragon = {
    Point: function (this: { x: number; y: number }, x: number, y: number) { this.x = x; this.y = y; },
  };

  new Function("window", "document", "fetch", "location", appSrc)(w, doc, fetchImpl, (w as { location: unknown }).location);
  (docListeners["DOMContentLoaded"] || []).forEach((cb) => cb());
  while (rafCbs.length) (rafCbs.shift() as () => void)();

  return { emitted, container, created, doc, fakeViewer };
}

async function settle(times = 20) {
  for (let i = 0; i < times; i++) await Promise.resolve();
}

function findSlideRow(created: FakeEl[]): FakeEl {
  const pick = () => created.filter((e) => e.classList.contains("fb-hit") && e.parentNode).pop();
  let row = pick();
  if (!row) {
    const folder = created.filter((e) => e.classList.contains("fb-folder") && e.parentNode).pop();
    if (!folder) throw new Error("harness: 未渲染出 .fb-hit 卡片与 .fb-folder 文件夹");
    folder.dispatch("click");
    row = pick();
  }
  if (!row) throw new Error("harness: 进入文件夹后仍未渲染出 .fb-hit 卡片");
  return row;
}

/** 容器 pointerdown/up（位移 ≤ slop）选中命中坐标处的标注（§6.2 单击选择路径）。 */
function selectAnnoAt(app: BootResult, x: number, y: number) {
  const ev = {
    button: 0, pointerId: 1, clientX: x, clientY: y,
    target: null, preventDefault() {}, stopPropagation() {},
  };
  app.container.fire("pointerdown", ev);
  app.container.fire("pointerup", ev);
}

/** 最近创建的工具条（#anno-obj-toolbar）里的 ⋯ 按钮。 */
function moreButton(app: BootResult): FakeEl | null {
  const bars = app.created.filter((e) => e.id === "anno-obj-toolbar");
  const bar = bars[bars.length - 1];
  if (!bar) return null;
  return (bar.children as FakeEl[]).find((b) => b.classList.contains("aot-more")) || null;
}

function openAnnoMenu(app: BootResult): FakeEl | null {
  const btn = moreButton(app);
  if (!btn) throw new Error("harness: 工具条无 ⋯ 按钮");
  btn.dispatch("click", { stopPropagation() {} });
  const menus = app.created.filter((e) => e.classList.contains("aot-menu"));
  return menus[menus.length - 1] || null;
}

function clickMenuItem(menu: FakeEl, cls: string) {
  const item = (menu.children as FakeEl[]).find((b) => b.classList.contains(cls));
  if (!item) throw new Error("harness: 菜单项缺失 " + cls);
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

async function bootOnSlide(opts: { pluginLoaded?: boolean; aiPending?: boolean } = {}) {
  const app = bootApp(opts);
  await settle();
  findSlideRow(app.created).dispatch("click");
  await settle();
  app.fakeViewer.viewport = makeMockViewport(
    { x: 0, y: 0, width: 1, height: 1 },
    { x: 0, y: 0, width: 1000, height: 800 },
  );
  // 打开「显示全部标记」→ 箭头 (100,100)-(300,200) 可命中
  app.doc.getElementById("anno-all-btn").dispatch("click");
  return app;
}

describe("标记 ⋯ 菜单「✨ 让 AI 解读」", () => {
  it("选中标注 → ⋯ 菜单首项是解读项；点击 emit attachIntent（kind=marker + intent=interpret + 标注元数据）", async () => {
    const app = await bootOnSlide();
    selectAnnoAt(app, 150, 130);   // 到箭头线段距离 ≈ 4.5px（≤8 容差）→ 命中
    const menu = openAnnoMenu(app);
    expect(menu).toBeTruthy();
    const items = menu!.children as FakeEl[];
    expect(items.length).toBeGreaterThan(1);
    // 首项：✨ 让 AI 解读（✨ 前缀 + 专属类；tooltip 为 tip 键）
    expect(items[0]!.classList.contains("aot-askai")).toBe(true);
    expect(items[0]!.textContent).toBe("✨ anno.menu.askai");
    expect(items[0]!.title).toBe("anno.menu.askai.tip");
    expect(items[0]!.disabled).toBe(false);
    // 其余项里没有第二个解读项；定位项仍在
    expect(items.filter((b) => b.classList.contains("aot-askai"))).toHaveLength(1);
    expect(items.some((b) => b.textContent === "⌖ anno.menu.locate")).toBe(true);

    clickMenuItem(menu!, "aot-askai");
    const evt = app.emitted.find((e) => e.type === "conversation.attachIntent");
    expect(evt).toBeTruthy();
    const p = evt!.payload as Record<string, unknown>;
    expect(p.kind).toBe("marker");
    expect(p.intent).toBe("interpret");
    expect(p.slide_id).toBe(SLIDE_ID);
    expect(p.slide).toBe(SLIDE);
    expect(p.bbox).toEqual({ x: 100, y: 100, w: 200, h: 100 });
    expect(p.annotation_id).toBe("anno-7");
    expect(p.revision).toBe(2);
    expect(p.type).toBe("arrow");
    expect(p.note).toBe("核异型区域");
    expect(typeof p.frozen_at).toBe("number");
  });

  it("AI 待复核标注（source=ai, review_status=pending）→ 解读项仍为首项，与驳回并存，点击正常 emit", async () => {
    const app = await bootOnSlide({ aiPending: true });
    selectAnnoAt(app, 150, 130);
    const menu = openAnnoMenu(app);
    const items = menu!.children as FakeEl[];
    expect(items[0]!.classList.contains("aot-askai")).toBe(true);
    expect(items[0]!.disabled).toBe(false);
    // 真实待复核态：⋯ 菜单出现驳回项（证明夹具确实进入了 AI 待复核分支）
    expect(items.some((b) => b.textContent === "anno.review.reject")).toBe(true);
    clickMenuItem(menu!, "aot-askai");
    const evt = app.emitted.find((e) => e.type === "conversation.attachIntent");
    expect(evt).toBeTruthy();
    const p = evt!.payload as Record<string, unknown>;
    expect(p.kind).toBe("marker");
    expect(p.intent).toBe("interpret");
    expect(p.annotation_id).toBe("anno-7");
    expect(p.revision).toBe(2);
  });

  it("插件未加载（hpReady=false）→ 解读项禁用并给「插件未加载」说明；点击不 emit", async () => {
    const app = await bootOnSlide({ pluginLoaded: false });
    selectAnnoAt(app, 150, 130);
    const menu = openAnnoMenu(app);
    const items = menu!.children as FakeEl[];
    const askai = items.find((b) => b.classList.contains("aot-askai"))!;
    expect(askai).toBeTruthy();
    expect(askai.disabled).toBe(true);
    expect(askai.title).toBe("ctxmenu.attach.plugin.missing");
    askai.dispatch("click", { preventDefault() {}, stopPropagation() {} });
    expect(app.emitted.some((e) => e.type === "conversation.attachIntent")).toBe(false);
  });

  it("源契约：draft 早退后才有解读项；冻结复用 freezeMarkerAttachPayload；模板无「+」按钮", () => {
    const fnStart = appSrc.indexOf("function openAnnoToolbarMenu");
    expect(fnStart).toBeGreaterThan(-1);
    const fnText = appSrc.slice(fnStart);
    const draftReturn = fnText.indexOf('if (target === "draft")');
    const askaiIdx = fnText.indexOf("anno.menu.askai");
    expect(draftReturn).toBeGreaterThan(-1);
    expect(askaiIdx).toBeGreaterThan(draftReturn);
    // 冻结与右键「将此标注加入会话」同一实现（不复制字段逻辑）
    expect(fnText).toContain("freezeMarkerAttachPayload(it, null)");
    expect(fnText).toContain('frozenInterpret.intent = "interpret"');
    expect(fnText).toContain("emitConversationAttachIntent(frozenInterpret)");
    // 插件缺失走既有说明键；不可解读形态禁用说明
    expect(fnText).toContain("ctxmenu.attach.plugin.missing");
    expect(fnText).toContain("anno.menu.askai.unavailable");
    // composer 不再有「+」按钮（入口移到标记上）
    expect(shellSrc).not.toContain("ai-task-jump");
    expect(shellSrc).not.toContain("ai-plus-btn");
  });
});

describe("标记工具条 ⋯ 开合与删除后收起", () => {
  function liveMenus(app: BootResult) {
    return app.created.filter((e) => e.classList.contains("aot-menu") && e.parentNode);
  }

  it("⋯ 再点一次关闭菜单；第三次重新打开", async () => {
    const app = await bootOnSlide();
    selectAnnoAt(app, 150, 130);
    const btn = moreButton(app)!;
    btn.dispatch("click", { stopPropagation() {} });
    expect(liveMenus(app)).toHaveLength(1);
    btn.dispatch("click", { stopPropagation() {} });
    expect(liveMenus(app)).toHaveLength(0);
    btn.dispatch("click", { stopPropagation() {} });
    expect(liveMenus(app)).toHaveLength(1);
  });

  it("菜单内删除成功 → 工具条 hidden，菜单移除", async () => {
    const app = await bootOnSlide({ aiPending: true });
    selectAnnoAt(app, 150, 130);
    // harness 无布局：选中后 updateAnnoToolbarPos 即置 hidden；此处只锁删除路径收起，
    // 「hidden 真能隐藏」由下方样式表断言与真实浏览器验证覆盖。
    const bar = app.created.filter((e) => e.id === "anno-obj-toolbar").pop()!;
    const menu = openAnnoMenu(app)!;
    const del = (menu.children as FakeEl[]).find((b) => b.textContent === "🗑 edit.del")!;
    del.dispatch("click", { preventDefault() {}, stopPropagation() {} });
    await settle();
    expect(bar.hidden).toBe(true);
    expect(liveMenus(app)).toHaveLength(0);
  });

  it("两份样式表的 display:flex 不压过 hidden 属性", () => {
    for (const css of ["../../static/style.css", "../../static/share.css"]) {
      const src = readFileSync(resolve(here, css), "utf8");
      expect(src).toMatch(/#anno-obj-toolbar\[hidden\]\s*\{\s*display:\s*none;?\s*\}/);
    }
  });
});
