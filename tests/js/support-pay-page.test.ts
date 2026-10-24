/**
 * R2 视觉/轮询（2026-10-21）+ R4 喂猫互动（同日用户评审）：/support/return
 * 轮询与 /support/pay 返回不重提交的前端逻辑契约。jsdom 不可用（无 DOM
 * 环境），按本仓库既有模式锁定生产源码契约（不执行，只锁行为要素）。
 *
 * 锁定：
 *   - 轮询：周期 3000ms、至多 60 次（约 3 分钟后停止）、轮询会话绑定的
 *     data-status-url、paid/closed 时 location.reload()；
 *   - 支付页返回不重提交：pageshow persisted 与 back_forward 导航都不自动
 *     重提交，改显恢复面板；「继续去支付宝付款」手动重提交；
 *   - 致谢凭条：平滑打印动画类 receipt-live；「再看一次」只重播凭条
 *     （礼物动画舞台已退役——喂猫互动在 /support 猫卡）；
 *   - 回跳页已支付态：领取卡（去喂小猫）替代礼物动画舞台；
 *   - /support 猫卡礼物（服务端渲染、hidden、托盘真按钮）；待机打印机
 *     不再渲染。
 */
import { describe, expect, it } from "vitest";
import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const payJs = readFileSync(
	resolve(here, "../../static/support/support_pay.js"), "utf8");
const supportJs = readFileSync(
	resolve(here, "../../static/support/support.js"), "utf8");
const supportCss = readFileSync(
	resolve(here, "../../static/support/support.css"), "utf8");
const returnHtml = readFileSync(
	resolve(here, "../../templates/support_return.html"), "utf8");
const payHtml = readFileSync(
	resolve(here, "../../templates/support_pay.html"), "utf8");
const supportHtml = readFileSync(
	resolve(here, "../../templates/support.html"), "utf8");

describe("support_pay.js 轮询（R2 B2）", () => {
	it("每 3 秒轮询、至多 60 次（约 3 分钟封顶）", () => {
		expect(payJs).toContain("POLL_INTERVAL_MS = 3000");
		expect(payJs).toContain("POLL_MAX_ATTEMPTS = 60");
		expect(payJs).toContain("attempts > POLL_MAX_ATTEMPTS");
	});

	it("轮询会话绑定的 data-status-url，凭证 same-origin 且不走缓存", () => {
		expect(payJs).toContain("data-status-url");
		expect(payJs).toContain("getAttribute('data-status-url')");
		expect(payJs).toContain("credentials: 'same-origin'");
		expect(payJs).toContain("cache: 'no-store'");
	});

	it("paid/closed → location.reload()；其他状态继续轮询", () => {
		expect(payJs).toContain("data.status === 'paid' || data.status === 'closed'");
		expect(payJs).toContain("window.location.reload()");
	});
});

describe("support_pay.js 返回/前进不重提交（R2 B3）", () => {
	it("pageshow persisted 与 back_forward 导航都不自动重提交", () => {
		expect(payJs).toContain("window.addEventListener('pageshow'");
		expect(payJs).toContain("e.persisted");
		expect(payJs).toContain("'back_forward'");
		expect(payJs).toContain("showPayResumePanel");
	});

	it("恢复面板默认 hidden，显示时隐藏表单宿主；继续按钮手动重提交", () => {
		expect(payHtml).toContain('id="pay-resume" hidden');
		expect(payHtml).toContain('id="pay-resume-continue"');
		expect(payJs).toContain("panel.hidden = false");
		expect(payJs).toContain("host.hidden = true");
		expect(payJs).toContain("continueToAlipay");
		expect(payJs).toContain("form.submit()");
	});

	it("恢复面板的查看结果链接是会话绑定的回跳地址（服务端渲染 ref）", () => {
		expect(payHtml).toContain(
			'href="/support/return?out_trade_no={{ order.out_trade_no }}"');
	});
});

describe("致谢凭条打印（R2 视觉）", () => {
	it("平滑滑出动画：transform 单段缓出，无逐帧 steps", () => {
		expect(supportCss).toContain("@keyframes receipt-slide-out");
		expect(supportCss).toContain(".receipt-live");
		expect(supportCss).not.toContain("steps(");
		expect(supportCss).toContain("prefers-reduced-motion: reduce");
	});

	it("凭条只在回跳页已支付态；展示核实金额/订单号/准确免责声明 + 领取卡", () => {
		expect(returnHtml).toContain('class="receipt receipt-live"');
		expect(returnHtml).toContain("付款记录以支付宝为准");
		expect(returnHtml).toContain("¥{{ amount_yuan }}");
		expect(returnHtml).toContain("receipt-replay");
		// R4：礼物动画舞台退役，改为「领取卡」（礼物本体在 /support 猫卡）
		expect(returnHtml).not.toContain("gift-stage");
		expect(returnHtml).toContain("gift-pickup");
		expect(returnHtml).toContain('href="/support#cat-card"');
		expect(returnHtml).toContain("去喂小猫");
		// 凭条礼物行含自定义金额的小鱼干
		expect(returnHtml).toContain("一条小鱼干");
		// /support 不自动打印：待机打印机与提示不再渲染（弹层已支付窗格的
		// 打印时刻仍复用 .printer 视觉）；猫卡有 id 锚点
		expect(supportHtml).not.toContain("receipt-stage-idle");
		expect(supportHtml).not.toContain("receipt-idle-paper");
		expect(supportHtml).not.toContain("receipt-hint");
		expect(supportHtml).not.toContain("收款机会弹出来");
		expect(supportHtml).not.toContain('id="receipt"');
		expect(supportHtml).toContain('id="cat-card"');
	});

	it("重播：只重播凭条滑出（类切换）；reduced-motion 走链接静态刷新", () => {
		expect(payJs).toContain("classList.remove('receipt-live')");
		expect(payJs).toContain("classList.add('receipt-live')");
		expect(payJs).toContain("prefers-reduced-motion: reduce");
		expect(payJs).toContain("if (reduced()) return;");
		// 礼物舞台重播代码已随舞台一起移除
		expect(payJs).not.toContain("replayGiftStage");
		expect(payJs).not.toContain("gift-paused");
		// 重播按钮常驻可见（不看 JS）：指向回跳 URL 的链接
		expect(returnHtml).toContain('id="receipt-replay"');
		expect(returnHtml).toContain('再看一次');
		expect(returnHtml).not.toContain("hidden>再打一张");
	});

	it("喂猫互动：礼物本体在猫卡舞台（hidden），托盘是真按钮", () => {
		// 服务端渲染（本会话已支付订单）；aria-hidden 本体 + 真实按钮托盘
		expect(supportHtml).toContain('class="cat-gift cat-gift-{{ cat_gift.gift }}"');
		expect(supportHtml).toContain('data-gift="{{ cat_gift.gift }}" aria-hidden="true" hidden');
		expect(supportHtml).toContain('class="btn btn-secondary cat-gift-feed"');
		expect(supportHtml).toContain('id="cat-gift-tray"');
		// 喂食播报是 polite aria-live（不是 role=alert）
		expect(supportHtml).toContain('id="cat-live" aria-live="polite"');
		// 「已喂」指针（纯装饰态；存储异常静默）
		expect(supportJs).toContain("hp.support.gifts.fed.v1");
		expect(supportJs).toContain("function loadFedMap");
		expect(supportJs).toContain("try {");
		expect(supportJs).toContain("function feedGift");
	});

	it("喂食场景：复用节拍关键帧；三种礼物各自舞台类；reduced-motion 静态终态", () => {
		// 滑行终点由 JS 写入的 --feed-dx/--feed-dy 提供（CSSOM，非内联样式）
		expect(supportCss).toContain("@keyframes feed-glide");
		expect(supportCss).toContain("var(--feed-dx, 0px)");
		expect(supportCss).toContain("@keyframes gift-item-drop");
		expect(supportCss).toContain("@keyframes gift-head-eat");
		expect(supportCss).toContain("@keyframes gift-kibble-gone");
		expect(supportCss).toContain("@keyframes gift-crouch-pounce");
		expect(supportCss).toContain("@keyframes gift-paw-swipe");
		expect(supportCss).toContain("@keyframes gift-led-blink");
		// 场景类：吃（猫粮/小鱼干）、玩（老鼠）、装（光猫）
		expect(supportCss).toContain(".cat-stage.is-eating .cat-head");
		expect(supportCss).toContain(".cat-stage.feed-fish_treat.is-eating .gift-fish-bone");
		expect(supportCss).toContain(".cat-stage.is-playing .cat-figure");
		expect(supportCss).toContain(".cat-stage.is-working .gift-hardhat");
		expect(supportCss).toContain(".cat-gift.is-installed .gift-led");
		// 安全帽在头部组内（随头动，见 _cat_svg.html）
		const catSvg = readFileSync(
			resolve(here, "../../templates/_cat_svg.html"), "utf8");
		const headIdx = catSvg.indexOf('<g class="cat-head">');
		expect(headIdx).toBeGreaterThan(-1);
		const hatIdx = catSvg.indexOf('class="gift-hardhat"');
		expect(hatIdx).toBeGreaterThan(headIdx);
		// reduced-motion：不走滑行/进食循环（JS 直接终态 + CSS 兜底静态化）
		const reduceBlock = supportCss.slice(
			supportCss.lastIndexOf("prefers-reduced-motion: reduce"));
		expect(reduceBlock).toContain(".cat-gift.is-gliding");
		expect(reduceBlock).toContain(".cat-stage.is-eating .cat-head");
		// 喂食专属气泡（好吃！）替换默认「喵~」
		expect(supportCss).toContain(".cat-stage.is-yum .cat-bubble-feed");
	});

	it("小鱼干图标：整条/咬过/鱼骨三态，后两态默认不可见", () => {
		const gifts = readFileSync(
			resolve(here, "../../templates/_gift_items.html"), "utf8");
		expect(gifts).toContain("gift-svg-fish");
		for (const cls of ["gift-fish-full", "gift-fish-bitten", "gift-fish-bone"]) {
			expect(gifts).toContain(cls);
		}
		expect(supportCss).toMatch(/\.gift-fish-bitten,\s*\.gift-fish-bone \{ opacity: 0; \}/);
	});
});

describe("/support 陪玩小猫", () => {
	it("提示语承诺的互动由 support.js 实现：摸摸开心、拖毛线球拍爪、打盹", () => {
		expect(supportJs).toContain("function initCatPlay");
		expect(supportJs).toMatch(/initCatPlay\(\);/);
		for (const cls of ["is-happy", "is-batting", "is-sleeping", "is-dragging"]) {
			expect(supportJs).toContain(cls);
		}
		expect(supportJs).toContain("--eye-dx");
		// 只写 CSSOM 变量/transform，不写 style 属性（CSP style-src 'self'）
		expect(supportJs).not.toContain("setAttribute('style'");
	});

	it("档位预览容器本身不隐藏（选中档位时物品可见）", () => {
		const rule = supportCss.match(/\.gift-preview \{[^}]*\}/);
		expect(rule).not.toBeNull();
		expect(rule![0]).not.toContain("display: none");
	});

	it("只有毛线球禁用触摸滚动；猫舞台保留页面滚动", () => {
		const stage = supportCss.match(/\.cat-stage \{[^}]*\}/);
		expect(stage![0]).not.toContain("touch-action: none");
		const toy = supportCss.match(/\.cat-toy \{[^}]*\}/);
		expect(toy![0]).toContain("touch-action: none");
	});
});

describe("support.js 收窄（打印时刻移走）", () => {
	it("凭条函数已从 /support 脚本移除；保留防双击与标题同步", () => {
		expect(supportJs).not.toContain("syncReceiptDate");
		expect(supportJs).not.toContain("receipt-replay");
		expect(supportJs).toContain("initPayPickGuard");
		expect(supportJs).toContain("syncTitle");
	});
});
