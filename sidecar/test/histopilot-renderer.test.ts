/**
 * plugins/histopilot/ui/renderer.js 气泡渲染回归（2026-08-16）。
 *
 * 背景：流式 text_delta 空文本也会走 setBubbleContent → renderMarkdown 兜底包一层
 * <p></p>，叠加 .ai-chat-bubble 的 padding/背景形成一串灰色空白气泡。本文件加载真实
 * renderer.js（最小 window + 迷你 DOM shim，仿 host-bridge-native.test.ts 的
 * new Function 注入法），锁定：
 *   - appendChatBubble 对 "" / 纯空白不建 DOM；
 *   - appendChatBubble 正常文本出一个 assistant 气泡且内容可见；
 *   - setBubbleContent 空白输入不产出 <p>（含从有内容清回空）；
 *   - setBubbleContent 正常输入保留文本。
 * main.js 的 appendTextBubble 是闭包不强行单测：它只经由 renderer 的
 * setBubbleContent 写 DOM，以上行为即锁住回归面。
 */
import { describe, expect, it } from "vitest";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, resolve } from "node:path";

const here = dirname(fileURLToPath(import.meta.url));
const rendererSrc = readFileSync(resolve(here, "../../plugins/histopilot/ui/renderer.js"), "utf8");

// ---- 迷你 DOM：只覆盖 renderer.js 顶层与气泡路径用到的 API ----
class FakeElement {
	tagName: string;
	className = "";
	innerHTML = "";
	textContent = "";
	children: FakeElement[] = [];
	parentNode: FakeElement | null = null;
	scrollTop = 0;
	scrollHeight = 0;
	classList: {
		add: (...cls: string[]) => void;
		remove: (...cls: string[]) => void;
		contains: (cls: string) => boolean;
	};

	constructor(tagName: string) {
		this.tagName = tagName;
		const set = new Set<string>();
		this.classList = {
			add: (...cls) => cls.forEach((c) => set.add(c)),
			remove: (...cls) => cls.forEach((c) => set.delete(c)),
			contains: (c) => set.has(c),
		};
	}

	get lastElementChild(): FakeElement | null {
		return this.children.length ? this.children[this.children.length - 1]! : null;
	}

	get nextSibling(): FakeElement | null {
		if (!this.parentNode) return null;
		const sibs = this.parentNode.children;
		const i = sibs.indexOf(this);
		return i >= 0 && i + 1 < sibs.length ? sibs[i + 1]! : null;
	}

	appendChild(el: FakeElement): FakeElement {
		if (el.parentNode) el.parentNode.removeChild(el);
		el.parentNode = this;
		this.children.push(el);
		return el;
	}

	insertBefore(el: FakeElement, ref: FakeElement | null): FakeElement {
		if (!ref) return this.appendChild(el);
		if (el.parentNode) el.parentNode.removeChild(el);
		el.parentNode = this;
		const i = this.children.indexOf(ref);
		if (i >= 0) this.children.splice(i, 0, el);
		else this.children.push(el);
		return el;
	}

	removeChild(el: FakeElement): FakeElement {
		const i = this.children.indexOf(el);
		if (i >= 0) this.children.splice(i, 1);
		el.parentNode = null;
		return el;
	}

	remove(): void {
		if (this.parentNode) this.parentNode.removeChild(this);
	}

	// 测试路径不涉及选择器命中（无 .ai-trace-empty / 未读 user 气泡），返回空即可
	querySelector(_sel: string): FakeElement | null {
		return null;
	}

	querySelectorAll(_sel: string): FakeElement[] {
		return [];
	}
}

// renderer.js 只用 document.createElement（气泡路径）
const documentShim = { createElement: (tag: string): FakeElement => new FakeElement(tag) };

type Renderer = Record<string, (...args: unknown[]) => unknown>;

function loadRenderer(): Renderer {
	const w: Record<string, unknown> = {
		HistoPilot: {
			s: { els: {} },
			t: (k: string) => k,
			esc: String,
			fmtAiMag: String,
			fmtNum: String,
			truncateStr: (s: string) => s,
			fmtMsgTs: () => "",
		},
	};
	new Function("window", "document", rendererSrc)(w, documentShim);
	return w.HistoPilot as Renderer;
}

describe("histopilot renderer — 空白文本不产生空气泡", () => {
	it("appendChatBubble 跳过空字符串：不加子节点、返回 null", () => {
		const HP = loadRenderer();
		const c = new FakeElement("div");
		expect(HP.appendChatBubble?.(c, "assistant", "")).toBeNull();
		expect(c.children).toHaveLength(0);
	});

	it("appendChatBubble 跳过纯空白（空格/换行）：不加子节点", () => {
		const HP = loadRenderer();
		const c = new FakeElement("div");
		expect(HP.appendChatBubble?.(c, "assistant", "   \n")).toBeNull();
		expect(c.children).toHaveLength(0);
	});

	it("appendChatBubble 正常文本：一个 assistant 气泡，内容含 hello", () => {
		const HP = loadRenderer();
		const c = new FakeElement("div");
		const row = HP.appendChatBubble?.(c, "assistant", "hello") as FakeElement;
		expect(c.children).toHaveLength(1);
		expect(c.children[0]).toBe(row);
		expect(row.className).toContain("ai-chat-bubble");
		expect(row.className).toContain("assistant");
		expect(row.innerHTML).toContain("hello");
		expect(row.innerHTML).not.toBe("<p></p>");
	});

	it("setBubbleContent 空白输入：innerHTML 为空、无 <p>（含从有内容清回空）", () => {
		const HP = loadRenderer();
		const el = new FakeElement("div");
		HP.setBubbleContent?.(el, "assistant", "");
		expect(el.innerHTML).toBe("");
		expect(el.innerHTML.includes("<p")).toBe(false);

		HP.setBubbleContent?.(el, "assistant", "  \n ");
		expect(el.innerHTML).toBe("");

		HP.setBubbleContent?.(el, "assistant", "abc");
		expect(el.innerHTML).toContain("abc");
		HP.setBubbleContent?.(el, "assistant", "");
		expect(el.innerHTML).toBe("");
		expect(el.innerHTML.includes("<p")).toBe(false);
	});

	it("setBubbleContent 正常输入：保留文本并包 <p>", () => {
		const HP = loadRenderer();
		const el = new FakeElement("div");
		HP.setBubbleContent?.(el, "assistant", "abc");
		expect(el.innerHTML).toContain("<p");
		expect(el.innerHTML).toContain("abc");
	});

	it("appendChatBubble user 侧空白同样跳过", () => {
		const HP = loadRenderer();
		const c = new FakeElement("div");
		expect(HP.appendChatBubble?.(c, "user", " \n\t ")).toBeNull();
		expect(c.children).toHaveLength(0);
	});
});
