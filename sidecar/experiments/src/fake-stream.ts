/**
 * Phase 4 A/B framework (Wave 2: execution runner) — fake streamFn.
 *
 * A self-contained copy of the minimal scripted streamFn from
 * sidecar/test/helpers.ts `makeFakeStreamFn`, so the experiment runner does NOT
 * import from `test/` (which would couple the experiment data plane to the test
 * tree and trip tsconfig / bundling boundaries). The shape mirrors the
 * ScriptedTurn defined in taskset.ts, so a task's `model_script` feeds straight
 * in for scripted-mode mechanism validation.
 *
 * The fake emits the full pi AssistantMessageEvent protocol
 * (start → text/toolcall deltas → done) so the REAL pi Agent + agent-loop
 * process it correctly — we exercise the real agent loop, not a mock of it.
 *
 * NOTE: experiment data plane only — NOT built into the shipped sidecar bundle.
 */
import { createAssistantMessageEventStream, type AssistantMessage, type AssistantMessageEvent, type AssistantMessageEventStream } from "@earendil-works/pi-ai";

import type { ScriptedTurn } from "./taskset.js";

/**
 * Build a fake streamFn that plays back a script of turns in order, one per
 * model call. The turn is selected by the count of assistant messages already
 * in the context (robust against the agent-loop's internal retries).
 *
 * @returns the streamFn plus a mutable `calls` counter (diagnostic only).
 */
export function makeFakeStreamFn(script: ScriptedTurn[]): {
	fn: (model: unknown, context: unknown, options?: unknown) => AssistantMessageEventStream;
	calls: number;
} {
	let calls = 0;
	const fn = function (_model: unknown, context: unknown): AssistantMessageEventStream {
		const turnIndex = calls;
		calls += 1;
		const stream = createAssistantMessageEventStream();
		void (async () => {
			// Determine the turn index from the context's assistant-message count
			// (more robust than the raw call counter when retries happen).
			const ctx = context as { messages?: Array<{ role?: string; content?: unknown[] }> };
			const assistantCount = (ctx.messages || []).filter((m) => m.role === "assistant").length;

			const turn = script[assistantCount] || script[turnIndex] || { text: "(script exhausted)", stopReason: "stop" as const };
			void turnIndex;
			const content: AssistantMessage["content"] = [];
			// start
			const partial = makeAssistant([], "pending");
			stream.push({ type: "start", partial });
			// text
			if (turn.text) {
				const textStart = makeAssistant(content.slice(), "pending");
				stream.push({ type: "text_start", contentIndex: 0, partial: textStart });
				stream.push({ type: "text_delta", contentIndex: 0, delta: turn.text, partial: textStart });
				content.push({ type: "text", text: turn.text });
				stream.push({ type: "text_end", contentIndex: 0, content: turn.text, partial: makeAssistant(content.slice(), "pending") });
			}
			// tool calls
			for (let i = 0; i < (turn.toolCalls || []).length; i++) {
				const tc = turn.toolCalls![i]!;
				const idx = content.length;
				stream.push({ type: "toolcall_start", contentIndex: idx, partial: makeAssistant(content.slice(), "pending") });
				stream.push({
					type: "toolcall_end",
					contentIndex: idx,
					toolCall: { type: "toolCall", id: tc.id, name: tc.name, arguments: tc.arguments },
					partial: makeAssistant(content.slice(), "pending"),
				});
				content.push({ type: "toolCall", id: tc.id, name: tc.name, arguments: tc.arguments });
			}
			const stopReason = turn.stopReason ?? (turn.toolCalls && turn.toolCalls.length > 0 ? "toolUse" : "stop");
			const finalMsg = makeAssistant(content, stopReason);
			stream.push({ type: "done", reason: stopReason as "stop" | "length" | "toolUse", message: finalMsg });
			stream.end(finalMsg);
		})();
		return stream;
	};
	return { fn, get calls() { return calls; } };
}

function makeAssistant(content: AssistantMessage["content"], stopReason: AssistantMessage["stopReason"], errorMessage?: string): AssistantMessage {
	return {
		role: "assistant",
		content,
		api: "openai-completions",
		provider: "cpa-gateway",
		model: "test-model",
		usage: { input: 10, output: 5, cacheRead: 0, cacheWrite: 0, totalTokens: 15, cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, total: 0 } },
		stopReason,
		errorMessage,
		timestamp: Date.now(),
	} as AssistantMessage;
}

/** Re-exported for type-only convenience. */
export type { AssistantMessageEvent };
