/**
 * 支付弹层 r5 修复回归（2026-10-23 评审）：幂等 key 跟随展示订单。
 *
 * 评审复现（真实 Chromium）：A 过期 → 重新生成 → B → 关弹层 → 再次提交，
 * 表单仍携带 A 的原始 key → 服务端按「同 key 同额」原样返回 A（已关闭）
 * → 弹层展示 A → 又要点一次「重新生成」才回到可付状态（B/C 同时可付）。
 * 修复的两条行为：
 *   1. key 跟单：建单/重建成功应用订单负载（applyOrder）时，把该请求实际
 *      使用的幂等 key 写回表单隐藏输入——之后同额重开提交，服务端按
 *      「同 key 同额」原样返回当前订单（重开 A→B 后展示的是 B）。
 *      重建 {status:'paid'}（旧单凭条）与一切非订单结论、迟到/作废响应
 *      （关弹层/换代）都不碰隐藏输入。
 *   2. 换额换 key：弹层已有当前订单且本次提交金额（服务端口径：非空
 *      trimmed amount_custom 优先，其次选中的 amount 单选，再退非单选
 *      amount 输入；^\d+(\.\d{1,2})?$ 按字符串分段折整分，其余未知）与
 *      当前订单 amount_yuan 不同 → 提交改用未绑定的 nextKey：先写隐藏
 *      输入再建 FormData（整页回退同一 key），请求体再 fd.set 双保险。
 *      同额/金额未知/无当前订单 → 维持表单原 key 的既有行为。
 *
 * 脚手架来源：tests/js/support-pay-modal-r4-20261023.test.ts 的 harness()
 * 逐字拷贝（含 r2 的 intervals 返回、fetch (url, opts) 双参捕获 reject、
 * FormData 桩记录 set() 调用）。逐字保留之外的脚手架改动（均为纯记录能
 * 力或字段桩，不改变被测代码在旧桩下的任何可见行为——旧桩 querySelector
 * 一律返回 null，被测代码按「缺失即跳过」处理）：
 *   1. form.querySelector 按选择器返回可配置字段桩：idempotency_key 隐藏
 *      输入、amount_custom 文本输入、选中的 amount 单选（:checked）、非
 *      单选 amount 输入——供被测代码按真实表单读写；
 *   2. FormData 桩构造时以 idempotency_key 字段桩当前值预置 fields（镜像
 *      真实浏览器 FormData(form) 序列化隐藏输入），set() 语义改为同名替换；
 *   3. 元素桩 setAttribute 记录到 attrs、appendChild 记录到 children、
 *      innerHTML 赋值清空 children——断言 iframe 的 src（/support/pay/
 *      <ref>/qr）；
 *   4. form.submit 记录每次整页提交时刻的隐藏 key 值（submits 数组）；
 *   5. 新增辅助：frameSrc()（读最新挂载 iframe 的 src）、setCheckedRadio()
 *      （切换选中档位；单选桩带 closest → null，selectedTier() 无档卡可寻，
 *      档位行/预览自然为空）。
 */
import { describe, expect, it } from "vitest";
import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import vm from "node:vm";
import assert from "node:assert/strict";

const here = dirname(fileURLToPath(import.meta.url));
const root = resolve(here, "../..");
const source = readFileSync(resolve(root, "static/support/support.js"), "utf8");
const fn = source.slice(
  source.indexOf("  function initDesktopQrModal("),
  source.indexOf("  // R2 add-on：档位预览"));
const tick = () => new Promise((resolve) => setImmediate(resolve));

// 服务端渲染的初始隐藏 key（每页渲染随机的 32-hex）
const INIT_KEY = "a".repeat(32);

// ---- 脚手架（r4 harness() 原文 + 文件头改动清单第 1–5 项）----
function harness() {
  let seq = 0;
  const timeouts = new Map();
  const intervals = new Map();
  const events = {};
  function el(id) {
    return { id, hidden: id === 'pay-modal', textContent: '', dataset: {},
      attrs: {} as Record<string, string>, children: [] as any[],
      get innerHTML() { return ''; },
      set innerHTML(_) { this.children.length = 0; },
      addEventListener(k, f) { events[id + ':' + k] = f; },
      setAttribute(k, v) { this.attrs[k] = v; },
      removeAttribute() {}, focus() {},
      appendChild(child) { this.children.push(child); },
      querySelector() { return null; }, querySelectorAll() { return []; },
      getBoundingClientRect() { return {}; },
      classList: {add() {}, remove() {}},
    };
  }
  const nodes = new Map();
  const document = {getElementById(id) { if (!nodes.has(id)) nodes.set(id, el(id)); return nodes.get(id); },
    addEventListener() {}, removeEventListener() {}, activeElement: null,
    createElement() {return el('iframe');} };
  // 字段桩（改动清单第 1 项）：idempotency_key / amount_custom / 单选 / 隐藏 amount
  const keyInput = { value: INIT_KEY };
  const customInput = { value: '' };
  const fixedAmount = { value: '' };
  let checkedRadio: { value: string } | null = null;
  const requests = [];
  const submits: { key: string }[] = [];
  const ctx = {document, t: () => '', prefersReducedMotion: () => true, localDateIso: () => '2026-10-23',
    POLL_INTERVAL_MS: 3000, POLL_BACKOFF_MS: 6000, DESKTOP_QUERY: 'desktop', TOUCH_QUERY: 'touch',
    catWorld: null, Date,
    // FormData 桩（改动清单第 2 项）：镜像真实浏览器对隐藏输入的序列化
    FormData: class {
      fields: [string, string][];
      constructor() { this.fields = [['idempotency_key', keyInput.value]]; }
      set(k: string, v: string) {
        this.fields = this.fields.filter((pair) => pair[0] !== k);
        this.fields.push([k, v]);
      }
    },
    fetch(url, opts) {return new Promise((resolve, reject) => requests.push({url, opts, resolve, reject}));},
    window: {matchMedia(q) {return {matches: q === 'desktop'};},
      setTimeout(f, delay) {const id=++seq; timeouts.set(id,{f,delay}); return id;},
      clearTimeout(id) {timeouts.delete(id);},
      setInterval(f, delay) {const id=++seq; intervals.set(id,{f,delay}); return id;},
      clearInterval(id) {intervals.delete(id);}},
  };
  vm.createContext(ctx);
  vm.runInContext(fn + '\nthis.start = initDesktopQrModal;', ctx);
  const form = document.getElementById('form'); form.action = '/support/orders';
  form.querySelector = (sel) => {
    if (sel === 'input[name="idempotency_key"]') return keyInput;
    if (sel === 'input[name="amount_custom"]') return customInput;
    if (sel === 'input[name="amount"]:checked') return checkedRadio;
    if (sel === 'input[name="amount"]') return fixedAmount;
    return null;
  };
  // form.submit 记录（改动清单第 4 项）：整页提交时刻的隐藏 key 值
  form.submit = () => { submits.push({ key: keyInput.value }); };
  ctx.start(form, document.getElementById('pay-modal'));
  function submit() {events['form:submit']({preventDefault(){}});}
  function respond(index, payload, status=200) {requests[index].resolve({status, ok: status>=200 && status<300, json:()=>Promise.resolve(payload)});}
  function poll() {const pair=[...timeouts].find(([,v])=>v.delay===3000); assert.ok(pair); timeouts.delete(pair[0]); pair[1].f();}
  function order(ref) {return {out_trade_no: ref, amount_yuan:'6.00', pay_url:'/support/pay/'+ref,
    expires_at: new Date(Date.now()+900000).toISOString(), next_idempotency_key:'key-'+ref};}
  return {nodes, timeouts, intervals, requests, events, submits, keyInput,
    customInput, fixedAmount,
    // closest 桩：selectedTier() 会对选中单选取 .pay-tier（无档卡 → 无预览）
    setCheckedRadio(value: string) {
      checkedRadio = { value, closest: () => null };
    },
    submit, respond, poll, order};
}

/** 扩展辅助（改动清单第 5 项）：最新挂载 iframe 的 src（生产代码以
 *  frame.src 属性赋值，title/scrolling 才走 setAttribute）。 */
function frameSrc(h: ReturnType<typeof harness>): string {
  const host = h.nodes.get("pay-modal-frame-host");
  assert.ok(host && host.children.length, "iframe 应已挂载");
  const frame = host.children[host.children.length - 1];
  return frame.src || frame.attrs.src;
}

describe("支付弹层 r5：key 跟随展示订单（2026-10-23）", () => {
  it("重建 A→B：应用 B 的负载后隐藏输入立即等于重建请求的 key；重开提交（同额）原样取回 B 的 iframe", async () => {
    const h = harness();
    h.setCheckedRadio("6.00");
    h.submit();
    assert.equal(h.requests.length, 1);
    assert.deepEqual(h.requests[0].opts.body.fields,
      [["idempotency_key", INIT_KEY]]);
    h.respond(0, h.order("orderA"), 201); await tick();
    assert.equal(h.keyInput.value, INIT_KEY);      // 建单成功：key 本就同值
    h.poll();
    h.respond(1, { status: "closed" }); await tick();
    assert.equal(h.nodes.get("pay-modal-expired").hidden, false);
    // 重建：请求带 nextKey；B 的负载一应用，隐藏输入立即换成该请求的 key
    h.events["pay-modal-regenerate:click"]();
    assert.deepEqual(h.requests[2].opts.body.fields,
      [["idempotency_key", "key-orderA"]]);
    h.respond(2, h.order("orderB"), 201); await tick();
    assert.equal(h.keyInput.value, "key-orderA");
    assert.equal(frameSrc(h), "/support/pay/orderB/qr");
    // 关弹层后同额重开：表单当前 key（= B 的 key）随请求发送，
    // 服务端「同 key 同额」原样返回 B → 弹层直接展示 B
    h.events["pay-modal-close:click"]();
    h.submit();
    assert.equal(h.requests.length, 4);
    assert.equal(h.requests[3].url, "/support/orders");
    assert.deepEqual(h.requests[3].opts.body.fields,
      [["idempotency_key", "key-orderA"]]);
    h.respond(3, h.order("orderB"), 201); await tick();
    assert.equal(frameSrc(h), "/support/pay/orderB/qr");
  });

  it("首提（无当前订单）：key 原样随请求发送，建单成功后不变", async () => {
    const h = harness();
    h.setCheckedRadio("6.00");
    h.submit();
    assert.deepEqual(h.requests[0].opts.body.fields,
      [["idempotency_key", INIT_KEY]]);
    h.respond(0, h.order("orderA"), 201); await tick();
    assert.equal(h.keyInput.value, INIT_KEY);
    assert.equal(h.submits.length, 0);             // 成功路径不整页提交
  });
});

describe("支付弹层 r5：换额提交换用未绑定 nextKey（2026-10-23）", () => {
  /** 前奏：6.00 建单 A 并应用 → 关弹层，返回待换额提交的 harness。 */
  async function withCurrentOrder() {
    const h = harness();
    h.setCheckedRadio("6.00");
    h.submit();
    h.respond(0, h.order("orderA"), 201); await tick();
    h.events["pay-modal-close:click"]();
    return h;
  }

  it("预设档换额：隐藏输入在建单请求前已更新，请求显式携带 nextKey", async () => {
    const h = await withCurrentOrder();
    h.setCheckedRadio("18.00");
    h.submit();
    // 先写隐藏输入、再建 FormData：请求体与输入此刻一致，均为 nextKey
    assert.equal(h.requests.length, 2);
    assert.equal(h.keyInput.value, "key-orderA");
    assert.deepEqual(h.requests[1].opts.body.fields,
      [["idempotency_key", "key-orderA"]]);
  });

  it("自定义金额换额（非空 trimmed amount_custom 优先）：同样换用 nextKey", async () => {
    const h = await withCurrentOrder();
    h.setCheckedRadio("68.00");                    // 自定义非空时优先于单选
    h.customInput.value = "  9.50 ";
    h.submit();
    assert.equal(h.requests.length, 2);
    assert.equal(h.keyInput.value, "key-orderA");
    assert.deepEqual(h.requests[1].opts.body.fields,
      [["idempotency_key", "key-orderA"]]);
  });

  it("换额建单非 2xx：整页回退 form.submit()，提交的仍是刚写入的 nextKey", async () => {
    const h = await withCurrentOrder();
    h.setCheckedRadio("18.00");
    h.submit();
    h.respond(1, null, 500); await tick();
    assert.equal(h.submits.length, 1);
    assert.equal(h.submits[0].key, "key-orderA");
  });

  it("自定义 6 / 6.0 / 带空白 与当前 6.00 同额 → 维持表单原 key（既有行为）", async () => {
    for (const v of ["6", "6.0", "  6.00  "]) {
      const h = await withCurrentOrder();
      h.customInput.value = v;
      h.submit();
      assert.deepEqual(h.requests[1].opts.body.fields,
        [["idempotency_key", INIT_KEY]], "custom=" + v);
      assert.equal(h.keyInput.value, INIT_KEY, "custom=" + v);
    }
  });

  it("金额未知（abc / 6.001 / 6.）→ 不换 key，维持今天的表单 key 行为", async () => {
    for (const v of ["abc", "6.001", "6."]) {
      const h = await withCurrentOrder();
      h.customInput.value = v;
      h.submit();
      assert.deepEqual(h.requests[1].opts.body.fields,
        [["idempotency_key", INIT_KEY]], "custom=" + v);
      assert.equal(h.keyInput.value, INIT_KEY, "custom=" + v);
    }
  });

  it("金额取不到（无任何 amount 输入可选）→ 同样维持表单原 key", async () => {
    const h = harness();
    h.fixedAmount.value = "6.00";                  // 退路：非单选 amount 输入
    h.submit();
    h.respond(0, h.order("orderA"), 201); await tick();
    h.events["pay-modal-close:click"]();
    h.fixedAmount.value = "";                      // 取不到金额 → 未知
    h.submit();
    assert.deepEqual(h.requests[1].opts.body.fields,
      [["idempotency_key", INIT_KEY]]);
    assert.equal(h.keyInput.value, INIT_KEY);
  });
});

describe("支付弹层 r5：非订单结论与作废响应不碰隐藏 key（2026-10-23）", () => {
  /** 前奏：建单 A → 查询 closed → 过期窗格。 */
  async function expiredPane() {
    const h = harness();
    h.setCheckedRadio("6.00");
    h.submit();
    h.respond(0, h.order("orderA"), 201); await tick();
    h.poll();
    h.respond(1, { status: "closed" }); await tick();
    assert.equal(h.nodes.get("pay-modal-expired").hidden, false);
    return h;
  }

  it("重建 200 {status:'paid'}：出示旧单凭条，隐藏 key 原样（旧单仍绑定原 key）", async () => {
    const h = await expiredPane();
    h.events["pay-modal-regenerate:click"]();
    h.respond(2, { status: "paid", amount_yuan: "6.00", gift: "cat_food" });
    await tick();
    assert.equal(h.nodes.get("pay-modal-print").hidden, false);
    assert.equal(h.keyInput.value, INIT_KEY);
  });

  it("重建 200 {status:'unconfirmed'}：状态未知，隐藏 key 原样", async () => {
    const h = await expiredPane();
    h.events["pay-modal-regenerate:click"]();
    h.respond(2, { status: "unconfirmed" }); await tick();
    assert.equal(h.nodes.get("pay-modal-unconfirmed").hidden, false);
    assert.equal(h.keyInput.value, INIT_KEY);
  });

  it("重建建单负载迟到（在途关弹层）：整体丢弃，隐藏 key 原样", async () => {
    const h = await expiredPane();
    h.events["pay-modal-regenerate:click"]();
    h.events["pay-modal-close:click"]();
    h.respond(2, h.order("orderB"), 201); await tick();
    assert.equal(h.nodes.get("pay-modal").hidden, true);
    assert.equal(h.keyInput.value, INIT_KEY);
    assert.equal(h.submits.length, 0);
  });

  it("建单负载迟到（在途关弹层换代）：不应用、不同步，隐藏 key 原样", async () => {
    // A 已在展示（applyOrder 已换代），再次建单在途时关弹层 → 代次失效
    const h = harness();
    h.setCheckedRadio("6.00");
    h.submit();
    h.respond(0, h.order("orderA"), 201); await tick();
    h.submit();
    h.events["pay-modal-close:click"]();
    h.respond(1, h.order("orderB"), 201); await tick();
    assert.equal(h.nodes.get("pay-modal").hidden, true);
    assert.equal(h.keyInput.value, INIT_KEY);
  });
});

describe("支付弹层 r5：资产版本与既有口径（2026-10-23）", () => {
  const html = readFileSync(resolve(root, "templates/support.html"), "utf8");

  it("支付脚本与文案缓存随金额档位更新；css 保持既有版本", () => {
    expect(html).toContain("/static/support/support.js?v=20261026a");
    expect(html).not.toContain("/static/support/support.js?v=20261023a");
    expect(html).toContain("/static/support/support.css?v=20261023a");
    expect(html).toContain("/static/i18n.js?v=20261026a");
  });
});
