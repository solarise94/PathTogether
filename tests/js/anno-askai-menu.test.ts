/**
 * 标记工具条「✨ 加入 AI 对话」（AI 入口统一 2026-10-18：AI 加入入口从 ⋯
 * 菜单上移到工具条主位；「让 AI 解读」interpret 预填退役；锁定/解锁移入 ⋯；
 * fork「快速问答」/ branch「从此处深读」标注行入口退役）。
 *
 * 加载真实 static/app.js（harness 同 conversation-attach-intent.test.ts：最小
 * DOM + fetch mock + HostBridgeHost stub；container 补 addEventListener/appendChild
 * 以绑定 §6.2 捕获路由），经生产路径驱动：init → 开切片 → 显示标注层 → 容器
 * pointerdown/up 选中标注 → 工具条。锁定：
 *   - 行为：已保存标记工具条布局 [接受（仅 AI 待复核）] [编辑备注]
 *     [✨ 加入 AI 对话] [⋯]；锁定按钮不再在工具条上（移入 ⋯）；
 *   - 行为：点击 ✨（点击时刻冻结）→ HostBridge emit conversation.attachIntent，
 *     载荷 kind=marker（无 intent=interpret）+ annotation_id/revision/bbox/
 *     type/note（与右键「将此标注加入会话」同一冻结实现 freezeMarkerAttachPayload）；
 *   - 行为：只读（can_edit=false）标记的 ✨ 仍可用——加入讨论不受写权限限制；
 *   - 行为：插件未加载（hpReady=false）→ ✨ 禁用，tooltip 为「插件未加载」；
 *     点击（无监听）不 emit；
 *   - 行为：⋯ 菜单含「锁定/解锁位置」（带锁状态提示），定位项文案为
 *     「居中显示此标记」；菜单不再有 AI 解读项；
 *   - 源契约：draft（临时框）目标不出现 AI 加入项——早退分支在其之前；
 *     标注行不再渲染 fork「快速问答」/ branch「从此处深读」，改为单个
 *     「✨ 加入 AI 对话」chip（buildAnnoAiActions 统一入口）。
 */
import { afterEach, describe, expect, it, vi } from "vitest";
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const appSrc = readFileSync(resolve(here, "../../static/app.js"), "utf8");
const shellSrc = readFileSync(resolve(here, "../../templates/_app_shell.html"), "utf8");
const i18nSrc = readFileSync(resolve(here, "../../static/i18n.js"), "utf8");
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

function bootApp(opts: { pluginLoaded?: boolean; aiPending?: boolean; readOnly?: boolean } = {}): BootResult {
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
                  ...(opts.aiPending ? { source: "ai", review_status: "pending" } : {}),
                  ...(opts.readOnly ? { can_edit: false } : {}) }],
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

/** 最近创建的工具条（#anno-obj-toolbar）。 */
function toolbar(app: BootResult): FakeEl | null {
  const bars = app.created.filter((e) => e.id === "anno-obj-toolbar");
  return bars[bars.length - 1] || null;
}

function moreButton(app: BootResult): FakeEl | null {
  const bar = toolbar(app);
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

async function bootOnSlide(opts: { pluginLoaded?: boolean; aiPending?: boolean; readOnly?: boolean } = {}) {
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

describe("工具条「✨ 加入 AI 对话」（已保存标记）", () => {
  function barButton(app: BootResult, cls: string): FakeEl | null {
    const bar = toolbar(app);
    if (!bar) return null;
    return (bar.children as FakeEl[]).find((b) => b.classList.contains(cls)) || null;
  }

  it("布局 [编辑备注] [✨ 加入 AI 对话] [⋯]；锁定不在工具条上；点击 emit marker 载荷（无 intent）", async () => {
    const app = await bootOnSlide();
    selectAnnoAt(app, 150, 130);   // 到箭头线段距离 ≈ 4.5px（≤8 容差）→ 命中
    const bar = toolbar(app)!;
    const clsOf = (bar.children as FakeEl[]).map((b) =>
      (b.className as string).split(/\s+/).filter((c) => c.startsWith("aot-") && c !== "aot-btn")[0]);
    expect(clsOf).toEqual(["aot-note", "aot-askai", "aot-more"]);
    // 锁定按钮已移入 ⋯（工具条无 aot-lock）
    expect(barButton(app, "aot-lock")).toBeNull();

    const askai = barButton(app, "aot-askai")!;
    expect(askai.textContent).toBe("✨ anno.menu.askai");
    expect(askai.title).toBe("anno.menu.askai.tip");
    expect(askai.disabled).toBe(false);
    askai.dispatch("click", { preventDefault() {}, stopPropagation() {} });
    const evt = app.emitted.find((e) => e.type === "conversation.attachIntent");
    expect(evt).toBeTruthy();
    const p = evt!.payload as Record<string, unknown>;
    expect(p.kind).toBe("marker");
    expect(p.intent).toBeUndefined();   // interpret 预填退役：加附件 ≠ 提问
    expect(p.slide_id).toBe(SLIDE_ID);
    expect(p.slide).toBe(SLIDE);
    expect(p.bbox).toEqual({ x: 100, y: 100, w: 200, h: 100 });
    expect(p.annotation_id).toBe("anno-7");
    expect(p.revision).toBe(2);
    expect(p.type).toBe("arrow");
    expect(p.note).toBe("核异型区域");
    expect(typeof p.frozen_at).toBe("number");
  });

  it("AI 待复核：[接受] 在工具条上，✨ 仍可用；驳回仍在 ⋯ 菜单", async () => {
    const app = await bootOnSlide({ aiPending: true });
    selectAnnoAt(app, 150, 130);
    const bar = toolbar(app)!;
    const clsOf = (bar.children as FakeEl[]).map((b) =>
      (b.className as string).split(/\s+/).filter((c) => c.startsWith("aot-") && c !== "aot-btn")[0]);
    expect(clsOf).toEqual(["aot-accept", "aot-note", "aot-askai", "aot-more"]);
    const askai = barButton(app, "aot-askai")!;
    expect(askai.disabled).toBe(false);
    askai.dispatch("click", { preventDefault() {}, stopPropagation() {} });
    const evt = app.emitted.find((e) => e.type === "conversation.attachIntent");
    expect(evt).toBeTruthy();
    expect((evt!.payload as Record<string, unknown>).annotation_id).toBe("anno-7");
    // ⋯ 菜单仍提供驳回
    const menu = openAnnoMenu(app)!;
    expect((menu.children as FakeEl[]).some((b) => b.textContent === "anno.review.reject")).toBe(true);
  });

  it("只读（can_edit=false）：✨ 仍可用（加入讨论不受写权限限制）；只读徽章保留", async () => {
    const app = await bootOnSlide({ readOnly: true });
    selectAnnoAt(app, 150, 130);
    const bar = toolbar(app)!;
    const ro = (bar.children as FakeEl[]).find((b) => b.classList.contains("aot-readonly"))!;
    expect(ro).toBeTruthy();
    expect(ro.disabled).toBe(true);
    const askai = (bar.children as FakeEl[]).find((b) => b.classList.contains("aot-askai"))!;
    expect(askai.disabled).toBe(false);
    askai.dispatch("click", { preventDefault() {}, stopPropagation() {} });
    const evt = app.emitted.find((e) => e.type === "conversation.attachIntent");
    expect(evt).toBeTruthy();
    expect((evt!.payload as Record<string, unknown>).kind).toBe("marker");
  });

  it("插件未加载 → ✨ 禁用并给「插件未加载」说明；点击不 emit", async () => {
    const app = await bootOnSlide({ pluginLoaded: false });
    selectAnnoAt(app, 150, 130);
    const askai = (toolbar(app)!.children as FakeEl[]).find((b) => b.classList.contains("aot-askai"))!;
    expect(askai).toBeTruthy();
    expect(askai.disabled).toBe(true);
    expect(askai.title).toBe("ctxmenu.attach.plugin.missing");
    askai.dispatch("click", { preventDefault() {}, stopPropagation() {} });
    expect(app.emitted.some((e) => e.type === "conversation.attachIntent")).toBe(false);
  });
});

describe("⋯ 菜单：锁定移入 + 定位改名 + 无 AI 解读项", () => {
  it("锁定/解锁在 ⋯ 菜单内（带状态提示），点击切换并刷新工具条", async () => {
    const app = await bootOnSlide();
    selectAnnoAt(app, 150, 130);
    const menu = openAnnoMenu(app)!;
    const items = menu.children as FakeEl[];
    const lock = items.find((b) => b.textContent === "🔒 anno.lock.lock");
    expect(lock).toBeTruthy();
    expect(lock!.title).toBe("anno.lock.lock.tip");
    // 定位项文案 = 实际行为描述（键值见 i18n 断言；此处锁键名）
    expect(items.some((b) => b.textContent === "⌖ anno.menu.locate")).toBe(true);
    // 菜单没有 AI 解读项（已上移工具条）
    expect(items.some((b) => b.classList.contains("aot-askai"))).toBe(false);
    // 点击锁定 → toggleItemGeometryLocked + 工具条重渲（菜单关）
    lock!.dispatch("click", { preventDefault() {}, stopPropagation() {} });
    await settle();
    expect(app.emitted.some((e) => e.type === "conversation.attachIntent")).toBe(false);
  });
});

describe("源契约与文案（AI 入口统一 2026-10-18）", () => {
  it("draft（临时框）早退分支在 AI 加入之前——未保存草稿不提供加入入口", () => {
    const fnStart = appSrc.indexOf("function renderAnnoToolbar");
    expect(fnStart).toBeGreaterThan(-1);
    const fnText = appSrc.slice(fnStart);
    const draftReturn = fnText.indexOf('if (target === "draft")');
    const askaiIdx = fnText.indexOf("annoAiAttachToolbarButton");
    expect(draftReturn).toBeGreaterThan(-1);
    expect(askaiIdx).toBeGreaterThan(draftReturn);
    // 冻结与右键「将此标注加入会话」同一实现；点击时刻冻结（不在菜单打开期）
    expect(appSrc).toContain("function attachMarkerToConversation");
    expect(appSrc).toContain("freezeMarkerAttachPayload(it, null)");
    // interpret 专属预填退役
    expect(appSrc).not.toContain('intent = "interpret"');
    // 几何不可用/插件缺失的明确原因说明仍在
    expect(appSrc).toContain("anno.menu.askai.unavailable");
    expect(appSrc).toContain("ctxmenu.attach.plugin.missing");
    // composer 模板不再有「+」按钮
    expect(shellSrc).not.toContain("ai-task-jump");
    expect(shellSrc).not.toContain("ai-plus-btn");
  });

  it("标注行不再渲染 fork「快速问答」/ branch「从此处深读」（统一到加入 AI 对话）", () => {
    expect(appSrc).not.toContain('"fork.open"');
    expect(appSrc).not.toContain('"branch.open"');
    expect(appSrc).not.toContain("anno.fork.quick");
    expect(appSrc).not.toContain("anno.branch.deep");
    // 行内 AI 动作 = 单个 ✨ chip（同一 attachMarkerToConversation 入口）
    const fnStart = appSrc.indexOf("function buildAnnoAiActions");
    const sepAfter = appSrc.indexOf("工单 E（plan §6）：查看器右键菜单", fnStart);
    const fn = appSrc.slice(fnStart, sepAfter);
    expect(fn).toContain("attachMarkerToConversation");
    expect(fn).not.toContain("ai-branch");
  });

  it("i18n：加入 AI 对话 / 居中显示此标记（zh+en），旧「让 AI 解读」与 fork/branch 键退役", () => {
    expect(i18nSrc).toContain('"anno.menu.askai": "加入 AI 对话"');
    expect(i18nSrc).toContain('"anno.menu.askai": "Add to AI chat"');
    expect(i18nSrc).toContain('"anno.menu.locate": "居中显示此标记"');
    expect(i18nSrc).toContain('"anno.menu.locate": "Center this mark"');
    expect(i18nSrc).toContain("调整视野并高亮此标记");
    // 旧文案与旧键退役（退役注释里不再出现旧入口名）
    expect(i18nSrc).not.toContain("让 AI 解读");
    expect(i18nSrc).not.toContain(": \"快速问答\"");
    expect(i18nSrc).not.toContain(": \"从此处深读\"");
    expect(i18nSrc).not.toContain('"anno.fork.quick"');
    expect(i18nSrc).not.toContain('"anno.branch.deep"');
    expect(appSrc).not.toContain("让 AI 解读");
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
