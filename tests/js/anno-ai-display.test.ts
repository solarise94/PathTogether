/**
 * 查看器 AI 判定口径（2026-10-19 存量人工标注显示回归）。
 *
 * 2026-10-19 以前工作台把用户自己画的人工标注落库成 source="ai" /
 * review_status="pending"（add_roi 按 admin token 推断），但没有 AI 会话与
 * provenance。服务端 annotation_access.is_ai_generated 只认「source=ai 且带
 * 会话/溯源」为真 AI，并在列表条目上投影 author_kind；导出已按该口径把这类
 * 存量行导成人工。查看器此前仍按原始 source 判断，把旧人工行画成 AI 橙色、
 * 给出接受/驳回按钮与徽章、工具条「✓ 接受」、⋯「驳回」，且不计入人数。
 *
 * 本测试加载真实 static/app.js（harness 同 anno-delete-by-id.test.ts：最小
 * DOM + fetch mock），让列表接口返回同一切片上的四条标注，断言显示规则：
 *   - 旧人工行（source=ai/pending，author_kind=user）→ 无审核按钮/徽章；
 *   - 旧人工已接受行（source=ai/accepted，author_kind=user）→ 无状态徽章；
 *   - 真 AI 待审行（author_kind=ai）→ 接受 + 驳回按钮；
 *   - 缺 author_kind 的旧形态 AI 待审行 → 回落 source，仍有接受 + 驳回；
 *   - 作者口径（annoBadgeText）：旧人工行按其作者计人，真 AI 行不计人。
 */
import { afterEach, describe, expect, it, vi } from "vitest";
import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const appSrc = readFileSync(resolve(here, "../../static/app.js"), "utf8");

const SLIDE = "anno-ai-display.ndpi";
const SLIDE_ID = "sld_annoaidisplay1";

// ---------- 最小假元素（裁自 anno-delete-by-id.test.ts harness） ----------
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

function jsonResponse(body: unknown, status = 200) {
  return Promise.resolve({
    ok: status >= 200 && status < 300,
    status,
    clone() { return this; },
    json: () => Promise.resolve(body),
  });
}

/** 列表条目：同一切片上的四类行（作者/审核状态各异）。 */
function listingItems() {
  return [
    // 旧工作台人工标注：库里 source=ai/pending，但服务端投影 author_kind=user
    { index: 0, annotation_id: "a-legacy-human", label: "legacy-human-pending",
      source: "ai", review_status: "pending",
      author_key: "user:1", author_kind: "user" },
    // 同上，已被人工接受过
    { index: 1, annotation_id: "a-legacy-human-accepted", label: "legacy-human-accepted",
      source: "ai", review_status: "accepted",
      author_key: "user:2", author_kind: "user" },
    // 真 AI（带会话/溯源 → author_kind=ai），待复核
    { index: 2, annotation_id: "a-real-ai", label: "real-ai-pending",
      source: "ai", review_status: "pending",
      author_key: "ai", author_kind: "ai" },
    // 旧接口形态/本地构造：缺 author_kind → 回落 source 判定
    { index: 3, annotation_id: "a-legacy-ai-no-kind", label: "legacy-ai-no-kind",
      source: "ai", review_status: "pending" },
  ];
}

function annotationsPayload() {
  return {
    slide: SLIDE,
    slide_id: SLIDE_ID,
    annotations: listingItems().map((it) => ({
      label: it.label,
      count: 1,
      items: [{
        ...it,
        token: "admin",
        slide: SLIDE,
        slide_id: SLIDE_ID,
        type: "rect",
        x: 10 + it.index * 30, y: 10, w: 20, h: 20,
        ts: 1700000000,
        note: "",
        shared: true,
        can_edit: true,
        can_delete: true,
      }],
    })),
  };
}

interface BootResult {
  created: FakeEl[];
  doc: { getElementById: (id: string) => FakeEl };
}

function bootApp(): BootResult {
  const els: Record<string, FakeEl> = {};
  const created: FakeEl[] = [];
  const docListeners: Record<string, Array<() => void>> = {};
  const rafCbs: Array<() => void> = [];
  const openHandlers: Record<string, Array<(e?: unknown) => void>> = {};

  const container = fakeEl("viewer-container");
  const fakeViewer = {
    container,
    canvas: {},
    viewport: {
      getBounds: () => ({ x: 0, y: 0, width: 1, height: 1 }),
      viewportToImageRectangle: () => ({ x: 0, y: 0, width: 1000, height: 800 }),
      imageToViewportRectangle: (x: number, y: number, width: number, height: number) => ({ x, y, width, height }),
      fitBounds() {},
      imageToViewerElementCoordinates: (p: { x: number; y: number }) => p,
      viewerElementToImageCoordinates: (p: { x: number; y: number }) => p,
    },
    addHandler(type: string, fn: (e?: unknown) => void) {
      (openHandlers[type] ||= []).push(fn);
    },
    removeHandler() {},
    open(_url: string) {
      (openHandlers["open"] || []).forEach((fn) => fn({}));
    },
    setMouseNavEnabled() {},
  };

  const fetchImpl = vi.fn((url: string) => {
    const u = String(url);
    if (u.includes("/api/slides/" + SLIDE_ID + "/info")) {
      return jsonResponse({ name: SLIDE, slide_id: SLIDE_ID, display_name: "",
        original_filename: SLIDE, width: 1000, height: 800,
        mpp_x: 0.5, mpp_y: 0.5, mpp_source: "native" });
    }
    if (u.includes("/api/annotations?slide_id=")) return jsonResponse(annotationsPayload());
    if (u.includes("/api/annotations")) {
      // 侧栏徽章索引（键 = 切片名快照，后端 annoBadgeText 口径）
      return jsonResponse({ by_slide: { [SLIDE]: annotationsPayload().annotations } });
    }
    if (u.includes("/api/share/rois")) return jsonResponse([]);
    if (u.includes("/api/slides")) {
      return jsonResponse([{ name: SLIDE, slide_id: SLIDE_ID, display_name: "",
        width: 1000, height: 800, mpp_x: 0.5, mpp_source: "native" }]);
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

  // t 按 badge.* 模板做占位符替换（徽章文案「标记 {n} · {m} 人」需要真实数字）
  const templates: Record<string, string> = {
    "badge.marks": "标记 {n} · {m} 人",
    "badge.marks.only": "标记 {n}",
  };
  const t = (k: string, params?: Record<string, unknown>) =>
    String(templates[k] || k).replace(/\{(\w+)\}/g, (m, name: string) =>
      params && name in params ? String(params[name]) : m);
  const w: Record<string, unknown> = {
    HP_I18N: { t, getLang: () => "zh" },
    HP_ViewerCore: { create: () => fakeViewer },
    HP_API: {},
    HP_APP_BOOTSTRAP: { mode: "official", capabilities: { slide_id_api: true } },
    PluginPermissions: {},
    SVS_PLUGIN_PERMISSIONS: { histopilot: [] },
    HostBridgeHost: { onRequest() {}, onEvent() {}, emit() {}, request() { return Promise.reject({}); } },
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
  (globalThis as { OpenSeadragon?: unknown }).OpenSeadragon = {
    Point: function (this: { x: number; y: number }, x: number, y: number) { this.x = x; this.y = y; },
  };

  new Function("window", "document", "fetch", "location", appSrc)(
    w, doc, fetchImpl, (w as { location: unknown }).location);
  (docListeners["DOMContentLoaded"] || []).forEach((cb) => cb());
  while (rafCbs.length) (rafCbs.shift() as () => void)();

  return { created, doc };
}

async function settle(times = 30) {
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

/** 标注面板里第 n 个 .anno-item 行（按创建序 = 分组顺序 = 上面四类行的顺序） */
function annoRow(app: BootResult, n: number): FakeEl {
  const rows = app.created.filter((e) => e.classList.contains("anno-item") && e.parentNode);
  const row = rows[n];
  if (!row) throw new Error("harness: 标注面板行缺失 #" + n);
  return row;
}

function rowHasClass(row: FakeEl, cls: string): boolean {
  return (row.children as FakeEl[]).some((c) => c.classList.contains(cls));
}

afterEach(() => {
  vi.restoreAllMocks();
  delete (globalThis as { window?: unknown }).window;
  delete (globalThis as { document?: unknown }).document;
  delete (globalThis as { fetch?: unknown }).fetch;
  delete (globalThis as { HP_ViewerCore?: unknown }).HP_ViewerCore;
  delete (globalThis as { OpenSeadragon?: unknown }).OpenSeadragon;
});

async function bootOnSlide() {
  const app = bootApp();
  await settle();
  findSlideRow(app.created).dispatch("click");
  await settle();
  app.doc.getElementById("anno-btn").dispatch("click");
  await settle();
  return app;
}

describe("查看器 AI 判定以服务端 author_kind 为准", () => {
  it("旧人工行（author_kind=user）不出审核按钮/徽章；真 AI 与缺 author_kind 的旧形态出行内审核按钮", async () => {
    const app = await bootOnSlide();

    // 0：旧人工待审行（source=ai/pending，author_kind=user）——不显示任何审核 UI
    const legacyHuman = annoRow(app, 0);
    expect(rowHasClass(legacyHuman, "ai-review-accept")).toBe(false);
    expect(rowHasClass(legacyHuman, "ai-review-reject")).toBe(false);
    expect(rowHasClass(legacyHuman, "anno-review-badge")).toBe(false);

    // 1：旧人工已接受行（source=ai/accepted，author_kind=user）——无状态徽章
    const legacyAccepted = annoRow(app, 1);
    expect(rowHasClass(legacyAccepted, "ai-review-accept")).toBe(false);
    expect(rowHasClass(legacyAccepted, "ai-review-reject")).toBe(false);
    expect(rowHasClass(legacyAccepted, "anno-review-badge")).toBe(false);

    // 2：真 AI 待审行（author_kind=ai）——接受 + 驳回按钮
    const realAi = annoRow(app, 2);
    expect(rowHasClass(realAi, "ai-review-accept")).toBe(true);
    expect(rowHasClass(realAi, "ai-review-reject")).toBe(true);
    expect(rowHasClass(realAi, "anno-review-badge")).toBe(false);

    // 3：缺 author_kind 的旧形态 AI 待审行——回落 source，仍有接受 + 驳回
    const legacyAiNoKind = annoRow(app, 3);
    expect(rowHasClass(legacyAiNoKind, "ai-review-accept")).toBe(true);
    expect(rowHasClass(legacyAiNoKind, "ai-review-reject")).toBe(true);
  });

  it("作者口径（标记 N · M 人）：旧人工行计人，真 AI 行不计人", async () => {
    const app = await bootOnSlide();
    const badges = app.created.filter((e) => e.classList.contains("fb-card-badge") && e.parentNode);
    expect(badges).toHaveLength(1);
    // total = 4 条；人 = 2（两条 author_kind=user 各一作者；真 AI 与
    // 缺 author_kind 回落为 AI 的两行都不计人）
    expect(badges[0].textContent).toBe("标记 4 · 2 人");
  });
});
