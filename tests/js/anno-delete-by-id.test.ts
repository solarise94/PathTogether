/**
 * 过期 index 寻址缺陷（dogfood BUG-1 2026-10-17）：工作台对单条标注的删除/
 * 审核必须按 annotation_id（稳定 id 路由）寻址，不得用缓存的 token 内序号
 * index。index 是**全体工作台用户共享的同一列表**里的插入序位置：他人增删
 * 标注后，缓存 index 会指向另一条标注——非 owner 403「只能修改自己创建的
 * 标注」，owner 甚至静默删/审错。
 *
 * 本测试加载真实 static/app.js（harness 同 anno-askai-menu.test.ts：最小 DOM
 * + fetch mock + 可变服务端状态），mock 服务端按生产语义实现 index/id 两套
 * 删除与审核（归属校验、live 位置），经生产路径驱动标注面板按钮：
 *   - 删除：缓存 A2@1 后服务端插入他人行挤位（A2 实际移到 0、B1 落到 1）→
 *     点 A2 的删除。旧代码发 DELETE /api/annotation/admin/1 → 命中 B1 → 403
 *     （dogfood 情形）；修复后发 DELETE /api/annotation/id/<aid> → 只删 A2。
 *   - 审核：缓存 AI2@1 后 AI1 删除 + 自己新 AI3 落到 1 → 点 AI2 采纳。旧代码
 *     POST admin/1/review → 误审 AI3（静默审错）；修复后按 id 审 AI2。
 */
import { afterEach, describe, expect, it, vi } from "vitest";
import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const appSrc = readFileSync(resolve(here, "../../static/app.js"), "utf8");

const SLIDE = "stale-index.ndpi";
const SLIDE_ID = "sld_stale0000001";

// ---------- 最小假元素（裁自 anno-askai-menu.test.ts harness） ----------
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

// ---------- 可变服务端状态（生产语义的最小模拟） ----------
interface Row {
  aid: string;
  owner: "A" | "B";
  label: string;
  slide: string;
  type: string;
  ts: number;
  note: string;
  deleted: boolean;
  revision: number;
  source?: string;
  review_status?: string;
  geometry?: Record<string, unknown>;
}

const CURRENT_USER: "A" | "B" = "A";

function makeServer(initial: Row[]) {
  const srv = {
    rows: initial,
    requests: [] as Array<{ method: string; url: string }>,
    live(): Row[] { return srv.rows.filter((r) => !r.deleted); },
    /** token 内 live 位置（get_roi 口径） */
    indexOf(aid: string): number {
      return srv.live().findIndex((r) => r.aid === aid);
    },
    ownRow(row: Row | undefined): boolean {
      return !!row && row.owner === CURRENT_USER;
    },
    deleteByIndex(n: number) {
      const row = srv.live()[n];
      if (!row) return jsonResponse({ error: "标注不存在" }, 404);
      if (!srv.ownRow(row)) return jsonResponse({ error: "只能修改自己创建的标注" }, 403);
      row.deleted = true;
      row.revision += 1;
      return jsonResponse({ ok: true });
    },
    deleteById(aid: string) {
      const row = srv.rows.find((r) => r.aid === aid);
      if (!row || row.deleted) return jsonResponse({ error: "标注不存在" }, 404);
      if (!srv.ownRow(row)) return jsonResponse({ error: "只能修改自己创建的标注" }, 403);
      row.deleted = true;
      row.revision += 1;
      return jsonResponse({ ok: true, annotation_id: aid, revision: row.revision });
    },
    reviewByIndex(n: number, action: string) {
      const row = srv.live()[n];
      if (!row) return jsonResponse({ error: "标注不存在" }, 404);
      if (!srv.ownRow(row)) return jsonResponse({ error: "无权访问" }, 403);
      if (row.source !== "ai") return jsonResponse({ error: "仅 AI 标注可审核" }, 400);
      row.review_status = action === "accept" ? "accepted" : "rejected";
      row.revision += 1;
      return jsonResponse({ ok: true, review_status: row.review_status, revision: row.revision });
    },
    reviewById(aid: string, action: string) {
      const row = srv.rows.find((r) => r.aid === aid);
      if (!row || row.deleted) return jsonResponse({ error: "标注不存在" }, 404);
      if (!srv.ownRow(row)) return jsonResponse({ error: "无权访问" }, 403);
      if (row.source !== "ai") return jsonResponse({ error: "仅 AI 标注可审核" }, 400);
      row.review_status = action === "accept" ? "accepted" : "rejected";
      row.revision += 1;
      return jsonResponse({ ok: true, review_status: row.review_status, revision: row.revision });
    },
    /** /api/annotations?slide_id= 响应（items 带 pre-filter index 与 id） */
    annotationsPayload() {
      const groups = new Map<string, Row[]>();
      for (const r of srv.live()) {
        if (r.owner !== CURRENT_USER) continue; // 他人私有不可见
        const list = groups.get(r.label) || [];
        list.push(r);
        groups.set(r.label, list);
      }
      return {
        slide: SLIDE,
        slide_id: SLIDE_ID,
        annotations: Array.from(groups.entries()).map(([label, items]) => ({
          label,
          count: items.length,
          items: items.map((r) => ({
            index: srv.indexOf(r.aid),
            token: "admin",
            slide: SLIDE,
            slide_id: SLIDE_ID,
            type: r.type,
            annotation_id: r.aid,
            revision: r.revision,
            ts: r.ts,
            note: r.note,
            source: r.source,
            review_status: r.review_status,
            shared: false,
            can_edit: true,
            can_delete: true,
            ...(r.geometry || {}),
          })),
        })),
      };
    },
  };
  return srv;
}

function rowOf(aid: string, owner: "A" | "B", label: string, extra: Partial<Row> = {}): Row {
  return {
    aid, owner, label, slide: SLIDE, type: "rect", ts: 1700000000,
    note: "", deleted: false, revision: 1, ...extra,
  };
}

interface BootResult {
  requests: Array<{ method: string; url: string }>;
  created: FakeEl[];
  doc: { getElementById: (id: string) => FakeEl };
  srv: ReturnType<typeof makeServer>;
}

function bootApp(srv: ReturnType<typeof makeServer>): BootResult {
  const els: Record<string, FakeEl> = {};
  const created: FakeEl[] = [];
  const docListeners: Record<string, Array<() => void>> = {};
  const rafCbs: Array<() => void> = [];

  const container = fakeEl("viewer-container");
  const openHandlers: Record<string, Array<(e?: unknown) => void>> = {};
  const fakeViewer = {
    container,
    canvas: {},
    viewport: {
      getBounds: () => ({ x: 0, y: 0, width: 1, height: 1 }),
      viewportToImageRectangle: () => ({ x: 0, y: 0, width: 1000, height: 800 }),
      imageToViewportRectangle: (x: number, y: number, w: number, h: number) => ({ x, y, width: w, height: h }),
      fitBounds() {},
      imageToViewerElementCoordinates: (p: { x: number; y: number }) => p,
      viewerElementToImageCoordinates: (p: { x: number; y: number }) => p,
    },
    addHandler(type: string, fn: (e?: unknown) => void) {
      (openHandlers[type] ||= []).push(fn);
    },
    removeHandler(type: string, fn: (e?: unknown) => void) {
      const list = openHandlers[type] || [];
      const i = list.indexOf(fn);
      if (i >= 0) list.splice(i, 1);
    },
    open(_url: string) {
      (openHandlers["open"] || []).forEach((fn) => fn({}));
    },
    setMouseNavEnabled() {},
  };

  const fetchImpl = vi.fn((url: string, opts?: { method?: string }) => {
    const u = String(url);
    const method = (opts && opts.method || "GET").toUpperCase();
    srv.requests.push({ method, url: u });
    if (method === "DELETE" && u.startsWith("/api/annotation/id/")) {
      return jsonResponse({ error: "route-missing" }, 404) && srv.deleteById(u.replace("/api/annotation/id/", ""));
    }
    const mIdx = u.match(/^\/api\/annotation\/admin\/(\d+)$/);
    if (method === "DELETE" && mIdx) return srv.deleteByIndex(Number(mIdx[1]));
    const mTok = u.match(/^\/api\/annotation\/([^/]+)\/(\d+)$/);
    if (method === "DELETE" && mTok) return srv.deleteByIndex(Number(mTok[2]));
    const mRevId = u.match(/^\/api\/annotation\/id\/([^/]+)\/review$/);
    if (method === "POST" && mRevId) {
      return srv.reviewById(mRevId[1], "accept");
    }
    const mRevIdx = u.match(/^\/api\/annotation\/admin\/(\d+)\/review$/);
    if (method === "POST" && mRevIdx) return srv.reviewByIndex(Number(mRevIdx[1]), "accept");
    if (u.includes("/api/slides/" + SLIDE_ID + "/info")) {
      return jsonResponse({ name: SLIDE, slide_id: SLIDE_ID, display_name: "", original_filename: SLIDE, width: 1000, height: 800, mpp_x: 0.5, mpp_y: 0.5, mpp_source: "native" });
    }
    if (u.includes("/api/annotations?slide_id=")) return jsonResponse(srv.annotationsPayload());
    if (u.includes("/api/annotations")) return jsonResponse({ by_slide: {} });
    if (u.includes("/api/share/rois")) {
      // resolveAnnoIndex 兜底用：live 行全量（带 index）
      return jsonResponse(srv.live().map((r) => ({
        token: "admin", index: srv.indexOf(r.aid), slide: SLIDE, type: r.type,
        ts: r.ts, annotation_id: r.aid, ...(r.geometry || { x: 0, y: 0, side_px: 100 }),
      })));
    }
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

  new Function("window", "document", "fetch", "location", appSrc)(w, doc, fetchImpl, (w as { location: unknown }).location);
  (docListeners["DOMContentLoaded"] || []).forEach((cb) => cb());
  while (rafCbs.length) (rafCbs.shift() as () => void)();

  return { requests: srv.requests, created, doc, srv };
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

/** 标注面板里第 n 个 .anno-item 行（按创建序 = 分组顺序） */
function annoRow(app: BootResult, n: number): FakeEl {
  const rows = app.created.filter((e) => e.classList.contains("anno-item") && e.parentNode);
  const row = rows[n];
  if (!row) throw new Error("harness: 标注面板行缺失 #" + n);
  return row;
}

function clickBtn(row: FakeEl, cls: string) {
  const btn = (row.children as FakeEl[]).find((b) => b.classList.contains(cls));
  if (!btn) throw new Error("harness: 行内按钮缺失 " + cls);
  btn.dispatch("click", { stopPropagation() {} });
}

afterEach(() => {
  vi.restoreAllMocks();
  delete (globalThis as { window?: unknown }).window;
  delete (globalThis as { document?: unknown }).document;
  delete (globalThis as { fetch?: unknown }).fetch;
  delete (globalThis as { HP_ViewerCore?: unknown }).HP_ViewerCore;
  delete (globalThis as { OpenSeadragon?: unknown }).OpenSeadragon;
});

async function bootOnSlide(srv: ReturnType<typeof makeServer>) {
  const app = bootApp(srv);
  await settle();
  findSlideRow(app.created).dispatch("click");
  await settle();
  // 打开标注面板（渲染行按钮）
  app.doc.getElementById("anno-btn").dispatch("click");
  await settle();
  return app;
}

describe("过期 index 寻址（annotation_moved 回归）", () => {
  it("删除：他人插入挤位后按 annotation_id 删（旧代码按缓存 index → 403 命中他人行）", async () => {
    // 初始服务端：A1@0, A2@1（客户端据此缓存 index）
    const srv = makeServer([rowOf("a-A1", "A", "G1"), rowOf("a-A2", "A", "G2")]);
    const app = await bootOnSlide(srv);
    // 并发变动：他人删除了 A1（列表整体前移），随后 B 插入 B1 落在 index 1
    srv.rows.find((r) => r.aid === "a-A1")!.deleted = true;
    srv.rows.push(rowOf("b-B1", "B", "GB"));
    // 现在 live：A2@0, B1@1 —— A2 的缓存 index=1 指向 B1（dogfood 情形）

    clickBtn(annoRow(app, 1), "ai-del");  // 删除 A2（面板第二组行）
    await settle();

    const dels = app.requests.filter((r) => r.method === "DELETE");
    expect(dels.length).toBeGreaterThan(0);
    // 寻址键必须是 annotation_id（稳定 id 路由），绝不用缓存 index
    expect(dels.some((r) => r.url === "/api/annotation/id/a-A2")).toBe(true);
    expect(dels.some((r) => /\/api\/annotation\/admin\/1$/.test(r.url))).toBe(false);
    // 服务端：删的是 A2，B1（他人）未被动过
    expect(srv.rows.find((r) => r.aid === "a-A2")!.deleted).toBe(true);
    expect(srv.rows.find((r) => r.aid === "b-B1")!.deleted).toBe(false);
  });

  it("审核：自己另一条 AI 挤位后按 annotation_id 审（旧代码按缓存 index → 误审 AI3）", async () => {
    // 初始服务端：AI1@0, AI2@1（待审）
    const srv = makeServer([
      rowOf("a-AI1", "A", "G1", { source: "ai", review_status: "pending" }),
      rowOf("a-AI2", "A", "G2", { source: "ai", review_status: "pending" }),
    ]);
    const app = await bootOnSlide(srv);
    // 并发变动：AI1 被删（如另一端撤销），自己新 AI3 插入落在 index 1
    srv.rows.find((r) => r.aid === "a-AI1")!.deleted = true;
    srv.rows.push(rowOf("a-AI3", "A", "G3", { source: "ai", review_status: "pending" }));
    // 现在 live：AI2@0, AI3@1 —— AI2 的缓存 index=1 指向 AI3

    clickBtn(annoRow(app, 1), "ai-review-accept");  // 采纳 AI2
    await settle();

    const reviews = app.requests.filter((r) => r.method === "POST" && r.url.includes("/review"));
    expect(reviews.length).toBeGreaterThan(0);
    // 寻址键必须是 annotation_id
    expect(reviews.some((r) => r.url === "/api/annotation/id/a-AI2/review")).toBe(true);
    expect(reviews.some((r) => /\/api\/annotation\/admin\/1\/review$/.test(r.url))).toBe(false);
    // 服务端：采纳的是 AI2，AI3 仍是 pending
    expect(srv.rows.find((r) => r.aid === "a-AI2")!.review_status).toBe("accepted");
    expect(srv.rows.find((r) => r.aid === "a-AI3")!.review_status).toBe("pending");
  });
});
