/**
 * P3 人工读片行为采集模块测试（docs/agent-plan-20260921-registration-
 * consent-research.md §7.1/§7.2/§7.3 + §10 P3 验收）。
 *
 * 加载真实 static/research-viewer-telemetry.js（假 window/document + 可编程
 * OpenSeadragon viewport 桩 + fetch 桩 + vitest 假时钟），按生产路径驱动：
 * attach → startSlide（建会话）→ 底层输入事件 → animation-finish 归并 →
 * observe_pause → 批次发送。锁定：
 *   - 装配边界：demo/公开分享模板不加载采集脚本；viewer-core.js 无采集代码；
 *   - 滚轮连续手势 250ms 静默归并成一条 zoom（中心变化只置 changed_center，
 *     不重复产出 pan）；按钮/快捷键、拖拽 pan 各自归并；
 *   - 程序回放/resize/无手势 animation-finish 不算人工动作；
 *   - observe_pause 稳定 2 秒只记一次（1 分钟仍一次）；离开标签页/失焦/
 *     绘制中取消，下一次有效人工交互才开启新周期；payload 无时长字段；
 *   - 批次 ≤50 条 / 64KiB、缓冲 200 上限丢最旧；403/409 停采清缓冲；
 *     切切片清旧缓冲（旧数据不贴到新授权）；
 *   - 标注事件只带匿名局部标注 ID（业务标注 ID 绝不上传）；无会话不采集。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const moduleSrc = readFileSync(
  resolve(here, "../../static/research-viewer-telemetry.js"), "utf8");
const viewerCoreSrc = readFileSync(
  resolve(here, "../../static/viewer-core.js"), "utf8");
const demoHtml = readFileSync(
  resolve(here, "../../templates/demo.html"), "utf8");
const shareHtml = readFileSync(
  resolve(here, "../../templates/share.html"), "utf8");
const indexHtml = readFileSync(
  resolve(here, "../../templates/index.html"), "utf8");

const SLIDE = "p3-slide.ndpi";
const DIMS = { x: 1000, y: 800 };
const SESSION_ID = "rvs_testsession001";
const SESSION_EPOCH = 3;

// ---------- 假 OpenSeadragon viewer（可编程视野） ----------
interface Handlers {
  [type: string]: Array<(e?: unknown) => void>;
}

function makeFakeViewer() {
  const handlers: Handlers = {};
  // 视野状态：imageRect 为 level-0 像素矩形（viewportToImageRectangle 的
  // 返回），ratio 为 image zoom ratio（getZoom 的值恒等于 ratio——containerW
  // 与 dims.x 相等），center 为视口中心
  const state = {
    imageRect: { x: 0, y: 0, width: 1000, height: 800 },
    ratio: 1,
    center: { x: 0.5, y: 0.4 },
  };
  const viewport = {
    getBounds: () => ({ x: 0, y: 0, width: 1, height: 0.8 }),
    viewportToImageRectangle: () => ({ ...state.imageRect }),
    getZoom: () => state.ratio,
    getContainerSize: () => ({ x: DIMS.x, y: 600 }),
    getCenter: () => ({ ...state.center }),
  };
  const viewer = {
    container: { style: {} as Record<string, string> },
    source: { dimensions: { ...DIMS } },
    viewport,
    addHandler(type: string, fn: (e?: unknown) => void) {
      (handlers[type] ||= []).push(fn);
    },
  };
  const dispatch = (type: string, evt?: unknown) => {
    (handlers[type] || []).forEach((fn) => fn(evt));
  };
  return { viewer, state, dispatch, handlers };
}

// ---------- 假 window/document + fetch ----------
interface FetchCall {
  url: string;
  init: { method?: string; headers?: Record<string, string>; body?: string };
}

interface Harness {
  rt: {
    attach: (v: unknown) => void;
    startSlide: (spec: unknown) => void;
    endSlide: () => void;
    notifyToolInteraction: (d: unknown) => void;
    notifyAnnotation: (d: unknown) => void;
    setBusy: (k: string, on: boolean) => void;
    stop: () => void;
    flush: () => void;
    isCollecting: () => boolean;
    _state: () => Record<string, unknown>;
  };
  fv: ReturnType<typeof makeFakeViewer>;
  fetchCalls: FetchCall[];
  setFetchResult: (fn: (url: string) => { ok: boolean; status: number; json?: () => Promise<unknown> } | null) => void;
  doc: Record<string, unknown>;
  win: Record<string, unknown>;
}

function bootModule(): Harness {
  const fv = makeFakeViewer();
  const fetchCalls: FetchCall[] = [];
  const listeners: Record<string, Array<() => void>> = {};
  const doc = {
    cookie: "csrf_token=tok123",
    hidden: false,
    visibilityState: "visible",
    hasFocus: () => true,
    addEventListener(type: string, cb: () => void) {
      (listeners[type] ||= []).push(cb);
    },
  };
  const win = {
    setTimeout: (cb: () => void, ms?: number) => setTimeout(cb, ms),
    clearTimeout: (id: unknown) => clearTimeout(id as ReturnType<typeof setTimeout>),
    addEventListener(type: string, cb: () => void) {
      (listeners["win:" + type] ||= []).push(cb);
    },
    dispatch(type: string) {
      (listeners["win:" + type] || []).forEach((cb) => cb());
    },
    document: doc,
  };
  let resultFn: ((url: string) => { ok: boolean; status: number; json?: () => Promise<unknown> } | null) = (url) => {
    if (url.includes("/api/research/viewing-sessions")) {
      return { ok: true, status: 200, json: async () => ({
        ok: true,
        viewing_session_id: SESSION_ID,
        consent_epoch: SESSION_EPOCH,
        schema_version: "research-viewer-events-v1",
      }) };
    }
    return { ok: true, status: 200, json: async () => ({
      ok: true, accepted: 1, replayed: 0 }) };
  };
  const fetchImpl = vi.fn((url: string, init?: { method?: string; headers?: Record<string, string>; body?: string }) => {
    fetchCalls.push({ url: String(url), init: init || {} });
    const out = resultFn(String(url));
    return Promise.resolve(out);
  }) as unknown as typeof fetch;

  // 模块尾部 `typeof window !== "undefined" ? window : this`：new Function
  // 参数名 window 使其绑定到我们传入的假 window
  const factory = new Function("window", moduleSrc) as (w: unknown) => void;
  factory(win);
  const HP = (win as { HP_ResearchTelemetry?: { create: (o: unknown) => Harness["rt"] } })
    .HP_ResearchTelemetry;
  if (!HP) throw new Error("模块未挂载 HP_ResearchTelemetry");
  const rt = HP.create({
    fetchImpl,
    document: doc,
    window: win,
    randomId: (() => {
      let n = 0;
      return () => String(++n).padStart(4, "0") + "rnd";
    })(),
  });
  return {
    rt, fv, fetchCalls,
    setFetchResult(fn) { resultFn = fn; },
    doc, win,
  };
}

async function settle(times = 12) {
  for (let i = 0; i < times; i++) await Promise.resolve();
}

/** 打开切片：attach → startSlide → viewer open（ready）。 */
async function openSlide(h: Harness) {
  h.rt.attach(h.fv.viewer);
  h.rt.startSlide({ slide: SLIDE, width: DIMS.x, height: DIMS.y });
  await settle();
  h.fv.dispatch("open");
  return h;
}

/** 触发一次滚轮缩放手势（含动画完成）。 */
function wheelZoom(h: Harness, ratioTo: number) {
  h.fv.dispatch("canvas-scroll");
  h.fv.dispatch("animation-start");
  h.fv.state.ratio = ratioTo;
  vi.advanceTimersByTime(251); // 超过 250ms 静默窗口
  h.fv.dispatch("animation-finish");
}

/** 触发一次拖拽 pan 手势。 */
function dragPan(h: Harness, centerTo: { x: number; y: number }) {
  h.fv.dispatch("canvas-press");
  h.fv.dispatch("canvas-drag");
  h.fv.state.center = { ...centerTo };
  h.fv.dispatch("canvas-release");
  h.fv.dispatch("animation-finish");
}

function eventBodies(h: Harness) {
  return h.fetchCalls
    .filter((c) => c.url.includes("/api/research/viewer-events"))
    .map((c) => JSON.parse(String(c.init.body)));
}

function allEvents(h: Harness) {
  return eventBodies(h).flatMap((b: { events?: unknown[] }) => b.events as unknown[]);
}

beforeEach(() => {
  vi.useFakeTimers();
});

afterEach(() => {
  vi.useRealTimers();
});

describe("装配边界（§7.1：采集只进独立模块）", () => {
  it("demo.html / share.html 不加载 research-viewer-telemetry.js；index.html 加载", () => {
    expect(demoHtml).not.toContain("research-viewer-telemetry");
    expect(shareHtml).not.toContain("research-viewer-telemetry");
    expect(indexHtml).toContain("research-viewer-telemetry.js");
  });

  it("viewer-core.js 不内置采集（无 research 端点/无 HP_ResearchTelemetry）", () => {
    expect(viewerCoreSrc).not.toContain("/api/research/");
    expect(viewerCoreSrc).not.toContain("HP_ResearchTelemetry");
    expect(viewerCoreSrc).not.toContain("observe_pause");
  });
});

describe("会话与批次传输（§7.3）", () => {
  it("startSlide 建会话（CSRF 头 + 业务切片名）；无会话时不发事件请求", async () => {
    const h = bootModule();
    h.rt.attach(h.fv.viewer);
    h.rt.startSlide({ slide: SLIDE, width: DIMS.x, height: DIMS.y });
    await settle();
    expect(h.fetchCalls.length).toBe(1);
    const call = h.fetchCalls[0];
    expect(call.url).toContain("/api/research/viewing-sessions");
    expect(call.init.headers?.["X-CSRF-Token"]).toBe("tok123");
    expect(JSON.parse(String(call.init.body))).toEqual({ slide: SLIDE });
    expect(h.rt.isCollecting()).toBe(true);
  });

  it("会话创建 403：本页停采，之后零研究网络请求", async () => {
    const h = bootModule();
    h.setFetchResult(() => ({ ok: false, status: 403 }));
    await openSlide(h);
    wheelZoom(h, 2);
    vi.advanceTimersByTime(2000);
    await settle();
    expect(h.fetchCalls.length).toBe(1); // 只有会话创建一次
    expect(h.rt.isCollecting()).toBe(false);
  });

  it("批次结构：{viewing_session_id, consent_epoch, events}；事件恰为五字段；无身份字段", async () => {
    const h = bootModule();
    await openSlide(h);
    wheelZoom(h, 2);
    h.rt.flush();
    await settle();
    const bodies = eventBodies(h);
    expect(bodies.length).toBe(1);
    const body = bodies[0] as Record<string, unknown>;
    expect(body.viewing_session_id).toBe(SESSION_ID);
    expect(body.consent_epoch).toBe(SESSION_EPOCH);
    const ev = (body.events as Array<Record<string, unknown>>)[0];
    expect(Object.keys(ev).sort()).toEqual(
      ["action", "event_id", "payload", "schema_version", "seq"]);
    expect(ev.schema_version).toBe("research-viewer-events-v1");
    const raw = JSON.stringify(body);
    expect(raw).not.toContain("user_id");
    expect(raw).not.toContain("email");
  });

  it("达到 50 条阈值自动发送；周期 15s 也发送", async () => {
    const h = bootModule();
    await openSlide(h);
    // 每次迭代比例都变化（含首次：初始 ratio=1）：50 次手势 = 50 条事件
    for (let i = 0; i < 50; i++) wheelZoom(h, 1 + ((i % 3) + 1) * 0.1);
    await settle();
    const batches = eventBodies(h);
    expect(batches.length).toBeGreaterThanOrEqual(1);
    const counts = batches.map(
      (b: { events: unknown[] }) => b.events.length);
    expect(Math.max(...counts)).toBeLessThanOrEqual(50);
    expect(h.fetchCalls.filter((c) => c.url.includes("viewer-events")).length)
      .toBeGreaterThanOrEqual(1);
    // 周期发送：再攒 1 条，15s 定时器触发
    const before = eventBodies(h).length;
    wheelZoom(h, 2);
    vi.advanceTimersByTime(15000);
    await settle();
    expect(eventBodies(h).length).toBeGreaterThan(before);
  });

  it("事件端点 403/409：停采并清缓冲（旧数据不贴到新授权）", async () => {
    let status = 200;
    const h = bootModule();
    h.setFetchResult((url) => {
      if (url.includes("viewing-sessions")) {
        return { ok: true, status: 200, json: async () => ({
          viewing_session_id: SESSION_ID, consent_epoch: SESSION_EPOCH }) };
      }
      return { ok: status === 200, status, json: async () => ({}) };
    });
    await openSlide(h);
    wheelZoom(h, 2);
    h.rt.flush();
    await settle();
    expect(eventBodies(h).length).toBe(1);
    status = 409;
    wheelZoom(h, 3);
    h.rt.flush();
    await settle();
    // 409 那批已发出并被拒（共 2 次请求）；响应后停采清缓冲
    expect(eventBodies(h).length).toBe(2);
    expect(h.rt.isCollecting()).toBe(false);
    expect((h.rt._state() as { buffered: number }).buffered).toBe(0);
    // 再交互：零新请求（旧数据不贴到失效授权上）
    wheelZoom(h, 4);
    vi.advanceTimersByTime(5000);
    h.rt.flush();
    await settle();
    expect(eventBodies(h).length).toBe(2);
  });

  it("切切片：旧缓冲清空，事件不会发到新会话", async () => {
    const h = bootModule();
    await openSlide(h);
    wheelZoom(h, 2); // 旧切片上攒 1 条（未达阈值/未到周期）
    h.rt.startSlide({ slide: "another.ndpi", width: DIMS.x, height: DIMS.y });
    await settle();  // 等新会话创建完成（创建期间的事件本就允许缺失）
    h.fv.dispatch("open");
    wheelZoom(h, 3); // ratio 2 → 3（新切片上的新手势）
    h.rt.flush();
    await settle();
    const bodies = eventBodies(h);
    expect(bodies.length).toBe(1);
    const evs = bodies[0].events as Array<{ payload: Record<string, unknown> }>;
    // 只剩新切片会话里的事件（seq 从 1 重新计数）
    expect(evs.length).toBe(1);
    expect(evs[0].payload).toBeTruthy();
    // 会话创建请求发了两次（两次切片）
    expect(h.fetchCalls.filter((c) => c.url.includes("viewing-sessions")).length)
      .toBe(2);
  });

  it("stop()（撤回/登出）：停采集、清缓冲、后续输入零请求", async () => {
    const h = bootModule();
    await openSlide(h);
    wheelZoom(h, 2);
    h.rt.stop();
    wheelZoom(h, 3);
    vi.advanceTimersByTime(2000);
    h.rt.flush();
    await settle();
    expect(h.fetchCalls.filter((c) => c.url.includes("viewer-events")).length)
      .toBe(0);
    expect(h.rt.isCollecting()).toBe(false);
  });
});

describe("手势归并（§7.1）", () => {
  it("连续滚轮 + 静默 250ms + animation-finish = 一条 zoom（中心变化不重复 pan）", async () => {
    const h = bootModule();
    await openSlide(h);
    h.fv.dispatch("canvas-scroll");
    h.fv.dispatch("animation-start");
    h.fv.state.ratio = 1.5;
    h.fv.state.center = { x: 0.4, y: 0.3 }; // 缩放同时中心变了
    h.fv.dispatch("animation-finish"); // 静默未到：不归并
    h.fv.dispatch("canvas-scroll");
    h.fv.state.ratio = 2;
    vi.advanceTimersByTime(251);
    h.fv.dispatch("animation-finish");
    vi.advanceTimersByTime(2000);
    h.rt.flush();
    await settle();
    const evs = allEvents(h) as Array<{
      action: string; payload: Record<string, unknown> }>;
    const zooms = evs.filter((e) => e.action === "zoom_in");
    const pans = evs.filter((e) => e.action === "pan");
    expect(zooms.length).toBe(1);
    expect(pans.length).toBe(0);
    expect(zooms[0].payload.input_kind).toBe("wheel");
    expect(zooms[0].payload.changed_center).toBe(true);
    expect(zooms[0].payload.image_zoom_ratio).toBe(2);
    expect(zooms[0].payload.bbox_before).toEqual([0, 0, 1, 1]);
    expect(zooms[0].payload.bbox_after).toEqual([0, 0, 1, 1]);
  });

  it("缩小 → zoom_out；按钮/快捷键输入 input_kind=button", async () => {
    const h = bootModule();
    await openSlide(h);
    h.rt.notifyToolInteraction({ inputKind: "button" });
    h.fv.dispatch("animation-start");
    h.fv.state.ratio = 0.5;
    h.fv.dispatch("animation-finish");
    h.rt.flush();
    await settle();
    const evs = allEvents(h) as Array<{ action: string; payload: Record<string, unknown> }>;
    expect(evs.filter((e) => e.action === "zoom_out").length).toBe(1);
    expect(evs[0].payload.input_kind).toBe("button");
  });

  it("拖拽（press→drag→release→animation-finish）= 一条 pan", async () => {
    const h = bootModule();
    await openSlide(h);
    dragPan(h, { x: 0.3, y: 0.2 });
    h.rt.flush();
    await settle();
    const evs = allEvents(h) as Array<{ action: string; payload: Record<string, unknown> }>;
    expect(evs.filter((e) => e.action === "pan").length).toBe(1);
    expect(evs[0].payload.input_kind).toBe("drag");
  });

  it("无手势的 animation-finish / resize / 程序回放：零事件", async () => {
    const h = bootModule();
    await openSlide(h);
    h.fv.dispatch("animation-finish");
    h.fv.state.center = { x: 0.1, y: 0.1 };
    h.fv.dispatch("animation-start");
    h.fv.dispatch("animation-finish"); // 程序性移动（无输入）：不算人工
    h.fv.dispatch("resize");
    vi.advanceTimersByTime(2000);
    h.rt.flush();
    await settle();
    expect(allEvents(h).length).toBe(0);
  });

  it("点击未拖动（无视野变化）：不产出事件", async () => {
    const h = bootModule();
    await openSlide(h);
    h.fv.dispatch("canvas-press");
    h.fv.dispatch("canvas-release");
    h.fv.dispatch("animation-finish");
    h.rt.flush();
    await settle();
    expect(allEvents(h).length).toBe(0);
  });

  it("坐标归一化：level-0 像素 → [0,1] 四位小数、越界裁剪", async () => {
    const h = bootModule();
    await openSlide(h);
    h.fv.state.imageRect = { x: -50, y: -40, width: 2000, height: 1600 };
    h.rt.notifyToolInteraction({ inputKind: "button" });
    h.fv.dispatch("animation-start");
    h.fv.state.ratio = 2;
    h.fv.dispatch("animation-finish");
    h.rt.flush();
    await settle();
    const evs = allEvents(h) as Array<{ payload: { bbox_after: number[] } }>;
    expect(evs[0].payload.bbox_after).toEqual([0, 0, 1, 1]);
  });

  it("非有限视野值：事件被丢弃，不抛错", async () => {
    const h = bootModule();
    await openSlide(h);
    h.fv.state.imageRect = { x: NaN, y: 0, width: 100, height: 100 };
    h.rt.notifyToolInteraction({ inputKind: "button" });
    h.fv.dispatch("animation-start");
    h.fv.state.ratio = 2;
    h.fv.dispatch("animation-finish");
    h.rt.flush();
    await settle();
    expect(allEvents(h).length).toBe(0);
  });
});

describe("observe_pause（§7.2：动作不是时长）", () => {
  it("手势完成后稳定 2 秒记一次；保持静止 1 分钟仍只有一次", async () => {
    const h = bootModule();
    await openSlide(h);
    wheelZoom(h, 2);
    vi.advanceTimersByTime(2000);
    vi.advanceTimersByTime(60000);
    h.rt.flush();
    await settle();
    const pauses = (allEvents(h) as Array<{
      action: string; payload: Record<string, unknown> }>)
      .filter((e) => e.action === "observe_pause");
    expect(pauses.length).toBe(1);
    expect(Object.keys(pauses[0].payload).sort()).toEqual(
      ["bbox", "evidence", "image_zoom_ratio"]);
    expect(pauses[0].payload.evidence).toBe("inferred_stable_view");
    const raw = JSON.stringify(pauses[0].payload);
    expect(raw).not.toMatch(/duration|dwell|started|ended|_ms/);
  });

  it("离开标签页取消；回来不自动开新周期，下一次人工交互才重新检测", async () => {
    const h = bootModule();
    await openSlide(h);
    wheelZoom(h, 2);
    (h.doc as { hidden: boolean }).hidden = true;
    vi.advanceTimersByTime(2000);
    (h.doc as { hidden: boolean }).hidden = false;
    vi.advanceTimersByTime(5000); // 回来静止：不记
    wheelZoom(h, 3);              // 下一次有效人工交互
    vi.advanceTimersByTime(2000);
    h.rt.flush();
    await settle();
    const pauses = (allEvents(h) as Array<{ action: string }>)
      .filter((e) => e.action === "observe_pause");
    expect(pauses.length).toBe(1);
  });

  it("失焦取消（window blur）", async () => {
    const h = bootModule();
    await openSlide(h);
    wheelZoom(h, 2);
    (h.win as { dispatch: (t: string) => void }).dispatch("blur");
    vi.advanceTimersByTime(3000);
    h.rt.flush();
    await settle();
    expect((allEvents(h) as Array<{ action: string }>)
      .filter((e) => e.action === "observe_pause").length).toBe(0);
  });

  it("绘制中/弹窗打开（setBusy）取消检测", async () => {
    const h = bootModule();
    await openSlide(h);
    h.rt.setBusy("drawing", true);
    wheelZoom(h, 2);
    vi.advanceTimersByTime(3000);
    h.rt.setBusy("drawing", false);
    vi.advanceTimersByTime(3000); // busy 解除不自动开新周期
    h.rt.flush();
    await settle();
    expect((allEvents(h) as Array<{ action: string }>)
      .filter((e) => e.action === "observe_pause").length).toBe(0);
  });

  it("切片加载失败（open-failed）取消且不开新周期", async () => {
    const h = bootModule();
    await openSlide(h);
    h.fv.dispatch("open-failed");
    wheelZoom(h, 2);
    // open-failed 后 ready=false：手势 finalize 后 armObserve 不成立
    vi.advanceTimersByTime(3000);
    h.rt.flush();
    await settle();
    expect((allEvents(h) as Array<{ action: string }>)
      .filter((e) => e.action === "observe_pause").length).toBe(0);
  });

  it("新周期由下一次手势开启：两次手势两次稳定期各记一次", async () => {
    const h = bootModule();
    await openSlide(h);
    wheelZoom(h, 2);
    vi.advanceTimersByTime(2000);
    dragPan(h, { x: 0.2, y: 0.2 });
    vi.advanceTimersByTime(2000);
    h.rt.flush();
    await settle();
    expect((allEvents(h) as Array<{ action: string }>)
      .filter((e) => e.action === "observe_pause").length).toBe(2);
  });
});

describe("标注事件（§7.1/§7.4）", () => {
  it("create/update/delete/accept：匿名局部 ID 稳定、业务标注 ID 绝不上传", async () => {
    const h = bootModule();
    await openSlide(h);
    h.rt.notifyAnnotation({
      action: "annotation_create", shapeType: "rect",
      geom: { x: 100, y: 80, w: 200, h: 160 },
      annotationId: "anno-business-123",
    });
    h.rt.notifyAnnotation({
      action: "annotation_update", shapeType: "rect",
      geom: { x: 120, y: 90, w: 200, h: 160 },
      annotationId: "anno-business-123",
    });
    h.rt.notifyAnnotation({
      action: "annotation_delete", annotationId: "anno-business-123",
    });
    h.rt.notifyAnnotation({
      action: "annotation_accept", annotationId: "anno-business-123",
    });
    h.rt.flush();
    await settle();
    const evs = allEvents(h) as Array<{
      action: string; payload: Record<string, unknown> }>;
    expect(evs.map((e) => e.action)).toEqual([
      "annotation_create", "annotation_update", "annotation_delete",
      "annotation_accept",
    ]);
    const raw = JSON.stringify(evs);
    expect(raw).not.toContain("anno-business-123"); // 业务 ID 不上传
    const ids = new Set(evs.map((e) => e.payload.annotation_local_id));
    expect(ids.size).toBe(1); // 同一标注 → 同一匿名局部 ID
    expect(evs[0].payload.origin).toBe("human");
    expect(evs[3].payload.origin).toBe("human_review");
    expect(evs[0].payload.bbox).toEqual([0.1, 0.1, 0.2, 0.2]);
    expect(evs[2].payload.bbox).toBeUndefined(); // delete 无几何
  });

  it("非法 shapeType / 几何不可归一化：事件丢弃，不伪造", async () => {
    const h = bootModule();
    await openSlide(h);
    h.rt.notifyAnnotation({
      action: "annotation_create", shapeType: "fancy-tool",
      geom: { x: 1, y: 1, w: 10, h: 10 }, annotationId: "a1",
    });
    h.rt.notifyAnnotation({
      action: "annotation_create", shapeType: "rect",
      geom: null, annotationId: "a2",
    });
    h.rt.flush();
    await settle();
    expect(allEvents(h).length).toBe(0);
  });
});

describe("缓冲上限（§7.3：观测事件可丢）", () => {
  it("内存缓冲 200 条封顶：丢最旧，不崩、不阻塞", async () => {
    const h = bootModule();
    await openSlide(h);
    // 发送端点挂起（不 resolve 之前 flush 占位）：事件只进缓冲
    let release: (() => void) | null = null;
    h.setFetchResult((url) => {
      if (url.includes("viewing-sessions")) {
        return { ok: true, status: 200, json: async () => ({
          viewing_session_id: SESSION_ID, consent_epoch: SESSION_EPOCH }) };
      }
      return new Promise((res) => { release = () => res({
        ok: true, status: 200, json: async () => ({ ok: true }) }); }) as never;
    });
    void release;
    for (let i = 0; i < 260; i++) wheelZoom(h, 1 + (i % 5) * 0.1);
    const state = h.rt._state() as { buffered: number };
    expect(state.buffered).toBeLessThanOrEqual(200);
    expect(state.buffered).toBeGreaterThan(0);
  });
});
