/**
 * R3（2026-10-21 评审后 add-on）+ R4（喂猫互动 add-on）：桌面端「支付状态」
 * 弹层的前端逻辑契约。jsdom 不可用（无 DOM 环境），按本仓库既有模式锁定
 * 生产源码契约（不执行，只锁行为要素）。
 *
 * 锁定：
 *   - support.js 弹层拦截：仅宽视口 + 非触控；JSON 建单（Accept
 *     application/json、复用表单 action 与幂等 key）；iframe 指向同源
 *     embed 页 /support/pay/<ref>/qr；倒计时用服务端 expires_at；轮询既有
 *     状态端点（3s、429 退避）；**paid → 弹层切「致谢凭条打印」第三窗格
 *     （不跳转）**，礼物 kind 取服务端 gift/tier（自定义 → fish_treat），
 *     「收下礼物」（主按钮/×/Esc）关弹层、滚到小猫、经 catWorld.deliver
 *     交付并聚焦喂食按钮；closed/neutral → 「二维码已过期」+「重新生成」
 *     （fresh next_idempotency_key）；Esc 关闭 + 焦点圈闭 + 关闭后焦点
 *     还原；失败回退整页表单提交（既有 pay_error UX）。
 *   - support_pay.js：回跳页在 iframe 内时 break out 到顶层（防循环：顶部
 *     已是回跳页则跳过）。
 *   - 模板：pay-modal 的 aria/hidden 契约、打印窗格与礼物图标来源、embed
 *     模板无内联脚本。
 *   - i18n：support.modal.* / supportpayqr.* zh/en 成对。
 *
 * r4 语义修复（2026-10-23）改动清单（只动编码旧行为的断言，其余原文不动）：
 *   - 「关闭/过期」用例：原断言 `data.status === 'closed' ||
 *     data.status === 'neutral'`（两者都进「已过期」）拆开——neutral 不再
 *     宣判过期，改按会话丢失（noteSessionLost，「暂时无法确认」会话变体）
 *     处置；原断言 `createOrder(state.nextKey)`（客户端直接重建）改为
 *     `regenerateOrder()`（先经服务端复核旧单：paid 出旧单凭条、未付才
 *     关旧建新），幂等 key 仍取服务端下发的 next_idempotency_key。
 */
import { describe, expect, it } from "vitest";
import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const root = resolve(here, "../..");
const supportJs = readFileSync(
	resolve(root, "static/support/support.js"), "utf8");
const payJs = readFileSync(
	resolve(root, "static/support/support_pay.js"), "utf8");
const supportHtml = readFileSync(
	resolve(root, "templates/support.html"), "utf8");
const qrHtml = readFileSync(
	resolve(root, "templates/support_pay_qr.html"), "utf8");
const i18nSrc = readFileSync(resolve(root, "static/i18n.js"), "utf8");

function dictEntries(lang: "zh" | "en"): Map<string, string> {
	const start = lang === "zh"
		? i18nSrc.indexOf("zh: {") : i18nSrc.indexOf("en: {");
	const end = lang === "zh"
		? i18nSrc.indexOf("en: {") : i18nSrc.indexOf("\n  };", start);
	const block = i18nSrc.slice(start, end);
	const out = new Map<string, string>();
	const re = /"([^"\\]+)":\s*"((?:[^"\\]|\\.)*)"/g;
	let m: RegExpExecArray | null;
	while ((m = re.exec(block)) !== null) {
		if (!out.has(m[1])) out.set(m[1], m[2]);
	}
	return out;
}

describe("support.js 桌面端弹层拦截（R3）", () => {
	it("只在宽视口 + 非触控的桌面环境拦截；其余保持整页跳转", () => {
		expect(supportJs).toContain(
			"var DESKTOP_QUERY = '(min-width: 900px)'");
		expect(supportJs).toContain(
			"var TOUCH_QUERY = '(hover: none), (pointer: coarse)'");
		expect(supportJs).toContain("function desktop()");
		expect(supportJs).toContain("if (!desktop()) {");
		expect(supportJs).toContain("ev.preventDefault()");
	});

	it("JSON 建单：Accept application/json、同源凭据、不缓存；失败回退整页", () => {
		expect(supportJs).toContain("headers: { 'Accept': 'application/json' }");
		expect(supportJs).toContain("credentials: 'same-origin'");
		expect(supportJs).toContain("cache: 'no-store'");
		expect(supportJs).toContain("fallbackToFullPage");
		expect(supportJs).toContain("form.submit()");
	});

	it("iframe 指向同源 embed 页 /support/pay/<ref>/qr（未决订单服务端 303 直达签名网关 URL）", () => {
		expect(supportJs).toContain(
			"frame.src = '/support/pay/' + data.out_trade_no + '/qr'");
	});

	it("二维码 iframe 严格 200×200、无滚动（qr_pay_mode=4 裸 canvas 原尺寸）", () => {
		expect(supportJs).toContain("frame.setAttribute('scrolling', 'no')");
		const css = readFileSync(
			resolve(root, "static/support/support.css"), "utf8");
		expect(css).toContain(".pay-modal-frame {\n  width: 200px;\n  height: 200px;\n  border: 0;");
		expect(css).toContain(".pay-modal-qr-tile");
	});

	it("金额大字取服务端权威 amount_yuan；档位行只含图标与档位名", () => {
		expect(supportJs).toContain("amountEl.textContent = data.amount_yuan || ''");
		expect(supportJs).toContain("档位行只展示图标 + 档位名");
	});

	it("QR 与过期两窗格同 grid 单元格叠放（高度取较大者，切换不跳）", () => {
		const css = readFileSync(
			resolve(root, "static/support/support.css"), "utf8");
		expect(css).toContain(".pay-modal-panes {\n  display: grid;\n}");
		expect(css).toContain(".pay-modal-panes > * { grid-area: 1 / 1; }");
		// [hidden] 保持占位（display:flex 覆盖 UA 的 none）+ visibility:hidden
		// ——若 display:none，窗格移出 grid 单元格，切换时弹层高度会跳变
		expect(css).toContain(".pay-modal-panes > [hidden] {");
		expect(css).toMatch(/\.pay-modal-panes > \[hidden\] \{\n  display: flex;[^}]*visibility: hidden;/);
	});

	it("档位图标固定 28×28 盒（压过档卡 56px 规则，不溢出）", () => {
		const css = readFileSync(
			resolve(root, "static/support/support.css"), "utf8");
		expect(css).toContain("#pay-modal-gift {\n  flex: none;\n  width: 28px;\n  height: 28px;");
		expect(css).toContain("#pay-modal-gift .gift-svg {");
	});

	it("倒计时用服务端 expires_at（唯一权威），归零即过期态", () => {
		expect(supportJs).toContain("Date.parse(data.expires_at)");
		expect(supportJs).toContain("state.expiresAtMs - Date.now()");
		expect(supportJs).toContain("showExpired");
	});

	it("轮询既有状态端点：3s 周期、429 退避 6s；paid → 打印窗格（不跳转）", () => {
		expect(supportJs).toContain("var POLL_INTERVAL_MS = 3000");
		expect(supportJs).toContain("var POLL_BACKOFF_MS = 6000");
		expect(supportJs).toContain(
			"'/support/orders/' + state.ref + '/status'");
		expect(supportJs).toContain("data.status === 'paid'");
		expect(supportJs).toContain("showPrintPane(data)");
		expect(supportJs).toContain("schedulePoll(POLL_BACKOFF_MS)");
		// R4：paid 不再导航离站（弹层内打印致谢凭条）
		expect(supportJs).not.toContain("window.location.href");
	});

	it("已支付窗格：标题换「谢谢你的支持！」；礼物/金额/日期/订单号来自服务端", () => {
		expect(supportJs).toContain("function showPrintPane");
		// kind 由服务端 payload 推导，自定义金额（tier=null）→ fish_treat
		expect(supportJs).toContain("data.gift || data.tier || 'fish_treat'");
		expect(supportJs).toContain("printAmountEl.textContent = data.amount_yuan");
		expect(supportJs).toContain("printRefEl.textContent = state.ref");
		expect(supportJs).toContain(
			"'/support/return?out_trade_no=' + state.ref");
		// 凭条滑出：复用回跳页 receipt-slide-out（单段缓出，无 steps）
		expect(supportJs).toContain('classList.add(\'is-printing\')');
		const css = readFileSync(
			resolve(root, "static/support/support.css"), "utf8");
		expect(css).toContain(".pay-print-receipt.is-printing");
		expect(css).toMatch(/\.pay-print-receipt\.is-printing \{[^}]*receipt-slide-out/s);
		expect(supportJs).toContain("prefersReducedMotion()");
	});

	it("收下礼物：主按钮/×/Esc 都关弹层 → 滚到猫卡 → catWorld.deliver 交付", () => {
		expect(supportJs).toContain("deliverToCat(state.ref, state.gift)");
		expect(supportJs).toContain("state.pane === 'print' && state.ref");
		expect(supportJs).toContain("catWorld.deliver");
		expect(supportJs).toContain("getElementById('cat-card')");
		expect(supportJs).toContain("scrollIntoView({");
		expect(supportJs).toContain("block: 'center'");
		// 每单只交付一次（printDone 防重入）
		expect(supportJs).toContain("state.printDone");
		// 模板：打印窗格默认 hidden；礼物图标来源含四种 kind（含小鱼干）
		expect(supportHtml).toContain('id="pay-modal-print" hidden');
		expect(supportHtml).toContain('id="pay-gift-art-source"');
		expect(supportHtml).toContain('data-kind="fish_treat"');
		expect(supportHtml).toContain('id="pay-modal-deliver"');
		expect(supportHtml).toContain('id="pay-modal-fullreceipt"');
	});

	it("关闭/过期：closed → 「二维码已过期」；neutral → 会话丢失（不宣判过期）；重新生成走服务端复核且带 next key", () => {
		expect(supportJs).toContain("data.status === 'closed'");
		expect(supportJs).toContain("data.status === 'neutral'");
		expect(supportJs).toContain("noteSessionLost");
		expect(supportJs).toContain("data.next_idempotency_key");
		expect(supportJs).toContain("regenerateOrder()");
	});

	it("a11y：aria-modal 弹层、Esc 关闭、Tab 焦点圈闭、关闭后焦点还原", () => {
		expect(supportJs).toContain("if (ev.key === 'Escape')");
		expect(supportJs).toContain("document.addEventListener('keydown', onKeydown)");
		expect(supportJs).toContain("if (ev.key !== 'Tab') return;");
		expect(supportJs).toContain("state.lastFocus");
		expect(supportHtml).toContain('id="pay-modal" role="dialog"');
		expect(supportHtml).toContain('aria-modal="true"');
		expect(supportHtml).toContain('aria-labelledby="pay-modal-title"');
		expect(supportHtml).toContain("hidden>");
	});

	it("移动端/无弹层形态保留原防双击（整页跳转）", () => {
		expect(supportJs).toContain("if (button) button.disabled = true;");
		expect(supportJs).toContain("initPayPickGuard");
	});
});

describe("support_pay.js 回跳页 breakout（R3）", () => {
	it("iframe 内的回跳页提升到顶层；顶部已是回跳页（轮询先到）则跳过", () => {
		expect(payJs).toContain("breakoutReturnFromIframe");
		expect(payJs).toContain("if (window.top === window.self) return;");
		expect(payJs).toContain(
			"window.top.location.pathname === window.location.pathname");
		expect(payJs).toContain("window.top.location.replace(window.location.href)");
		expect(payJs).toContain("'support-return'");
	});
});

describe("embed 模板契约（/support/pay/<ref>/qr）", () => {
	it("无内联脚本/内联样式；模板不含任何支付表单（未决订单服务端 303 离站）", () => {
		// 先剥 Jinja 注释（{# … #} 不进渲染产物；注释里的字面 <script>/<style>
		// 不是页面内容），与 pytest 的「渲染后无内联」检查同口径。
		const rendered = qrHtml.replace(/\{#[\s\S]*?#\}/g, "");
		for (const m of rendered.matchAll(/<script\b([^>]*)><\/script>/g)) {
			expect(m[1]).toContain("src=");
		}
		expect(rendered).not.toContain("<style");
		expect(rendered).not.toMatch(/\bstyle\s*=/);
		expect(qrHtml).not.toContain("pay-form-host");
		expect(qrHtml).not.toContain("form_html");
		expect(qrHtml).not.toContain("<form");
	});

	it("中性态分支不渲染订单上下文", () => {
		const neutralBranch = qrHtml.slice(
			qrHtml.indexOf("pay_state == 'neutral'"),
			qrHtml.indexOf("{% else %}"));
		expect(neutralBranch).not.toContain("form_html");
	});
});

describe("弹层 i18n zh/en 成对", () => {
	const zh = dictEntries("zh");
	const en = dictEntries("en");
	const prefixes = ["support.modal.", "supportpayqr."];

	it("新键在 zh 与 en 词典都存在", () => {
		for (const prefix of prefixes) {
			const zhKeys = [...zh.keys()].filter((k) => k.startsWith(prefix));
			expect(zhKeys.length, prefix + " 应有 zh 键").toBeGreaterThan(0);
			for (const key of zhKeys) {
				expect(en.has(key), "en 缺键：" + key).toBe(true);
			}
		}
	});

	it("en 词典没有多余的弹层键", () => {
		for (const prefix of prefixes) {
			for (const key of en.keys()) {
				if (!key.startsWith(prefix)) continue;
				expect(zh.has(key), "zh 缺键：" + key).toBe(true);
			}
		}
	});

	it("关键文案锁定（标题/提示/过期/回退/打印窗格）", () => {
		expect(zh.get("support.modal.title")).toBe("支付状态");
		expect(zh.get("support.modal.qr.hint")).toBe("请使用支付宝扫码付款");
		expect(zh.get("support.modal.expired.title")).toBe("二维码已过期");
		expect(zh.get("support.modal.regenerate")).toBe("重新生成");
		expect(zh.get("support.modal.fallback")).toBe("改用支付宝网页收银台");
		// R4：已支付窗格（致谢凭条打印 + 收下礼物去喂小猫）
		expect(zh.get("support.modal.print.title")).toBe("谢谢你的支持！");
		expect(zh.get("support.modal.print.cta.cat_food")).toBe("收下猫粮，去喂小猫");
		expect(zh.get("support.modal.print.cta.cat_toy")).toBe("收下猫玩具，去陪小猫");
		expect(zh.get("support.modal.print.cta.mystery")).toBe("收下神秘礼物，交给小猫");
		expect(zh.get("support.modal.print.cta.fish_treat")).toBe("收下小鱼干，去喂小猫");
		expect(zh.get("support.modal.print.fullreceipt")).toBe("查看完整凭条");
		// 自定义金额的礼物名（凭条行/打印窗格共用 support.tier.*）
		expect(zh.get("support.tier.fish_treat")).toBe("一条小鱼干");
		expect(en.get("support.tier.fish_treat")).toBe("A dried fish treat");
		expect(en.get("support.modal.title")).toBe("Payment status");
	});
});
