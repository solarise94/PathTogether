/**
 * Phase 2a tests: per-session lock (§12.1) and session message sequence (§10).
 *
 * Covers:
 *   - Per-session lock: different sessions run concurrently; same session
 *     serializes (§12.1).
 *   - session_message_seq: monotonic, never reused, lazy migration 1..N,
 *     compaction retained-tail seq preservation, _context_meta stripping at
 *     Provider/UI boundaries.
 */
import { afterAll, beforeAll, describe, expect, it } from "vitest";
import { promises as fs } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import {
	SessionStore,
	appendMessages,
	assignMessageSeqs,
	assignSeqToMessage,
	replaceMessagesPreservingSeq,
	stripContextMeta,
	stripContextMetaMessage,
	type PersistedAgentMessage,
	type SessionData,
} from "../src/session-store.js";
import { buildTranscript } from "../src/transcript.js";

// Per-test temp directory tree.
let rootTmp = "";
beforeAll(async () => {
	rootTmp = await fs.mkdtemp(join(tmpdir(), "svs-msgseq-"));
});
afterAll(async () => {
	await fs.rm(rootTmp, { recursive: true, force: true });
});

let dirCounter = 0;
async function newStoreDir(): Promise<string> {
	const p = join(rootTmp, `d${dirCounter++}`);
	await fs.mkdir(p, { recursive: true });
	return p;
}

const SLIDE = "slide-msgseq.svs";

function userMsg(text: string): PersistedAgentMessage {
	return { role: "user", content: text, timestamp: Date.now() } as PersistedAgentMessage;
}

// =========================================================================== //
// Per-session lock (§12.1)
// =========================================================================== //
describe("Per-session lock (§12.1)", () => {
	it("different sessions enter withLock concurrently (not mutually exclusive)", async () => {
		const store = new SessionStore({ sessionsDir: await newStoreDir() });
		const s1 = await store.createSession({ slide: SLIDE, kind: "main" });
		const s2 = await store.createSession({ slide: SLIDE, kind: "main" });

		// Gate: each withLock holds until we release its resolve.
		let resolve1!: () => void;
		let resolve2!: () => void;
		const gate1 = new Promise<void>((r) => {
			resolve1 = r;
		});
		const gate2 = new Promise<void>((r) => {
			resolve2 = r;
		});

		let entered1 = false;
		let entered2 = false;
		let concurrent = false;

		const p1 = store.withLock(s1.id, async () => {
			entered1 = true;
			// Wait until released; if p2 also entered, concurrency is proven.
			await gate1;
			if (entered2) concurrent = true;
			return 1;
		});
		const p2 = store.withLock(s2.id, async () => {
			entered2 = true;
			await gate2;
			if (entered1) concurrent = true;
			return 2;
		});

		// Give both a chance to enter (they should, concurrently). Do NOT await
		// p1/p2 here — both are gated. Just wait a tick for the microtasks.
		await new Promise((r) => setTimeout(r, 20));
		expect(entered1).toBe(true);
		expect(entered2).toBe(true);

		// Release both and await completion.
		resolve1();
		resolve2();
		await Promise.all([p1, p2]);
		expect(concurrent).toBe(true);
	});

	it("same session serializes withLock (mutually exclusive)", async () => {
		const store = new SessionStore({ sessionsDir: await newStoreDir() });
		const s = await store.createSession({ slide: SLIDE, kind: "main" });

		let firstEntered = false;
		let secondSawFirstInside = false;
		let firstDone = false;

		let releaseFirst!: () => void;
		const firstGate = new Promise<void>((r) => {
			releaseFirst = r;
		});

		const p1 = store.withLock(s.id, async () => {
			firstEntered = true;
			await firstGate;
			firstDone = true;
			return 1;
		});
		// Ensure p1 entered first.
		await new Promise((r) => setTimeout(r, 10));
		expect(firstEntered).toBe(true);

		const p2 = store.withLock(s.id, async () => {
			// If serialization holds, firstDone must be true by the time we run.
			if (!firstDone) secondSawFirstInside = true;
			return 2;
		});

		// p2 should be blocked while p1 holds the lock.
		await new Promise((r) => setTimeout(r, 10));
		// Release p1; now p2 can proceed.
		releaseFirst();
		await Promise.all([p1, p2]);
		// p2 did NOT run concurrently with p1.
		expect(secondSawFirstInside).toBe(false);
	});

	it("releaseLock drops the per-session mutex entry", async () => {
		const store = new SessionStore({ sessionsDir: await newStoreDir() });
		const s = await store.createSession({ slide: SLIDE, kind: "main" });
		await store.withLock(s.id, async () => 1);
		// The lock map is private; we verify releaseLock is a no-throw no-op and
		// a subsequent withLock still works (recreates lazily).
		expect(() => store.releaseLock(s.id)).not.toThrow();
		const out = await store.withLock(s.id, async () => 42);
		expect(out).toBe(42);
	});
});

// =========================================================================== //
// Session message sequence (§10)
// =========================================================================== //
describe("session_message_seq assignment (§10)", () => {
	it("appendMessages assigns monotonic, never-reused seqs from 1", async () => {
		const store = new SessionStore({ sessionsDir: await newStoreDir() });
		const s = await store.createSession({ slide: SLIDE, kind: "main" });
		await store.appendMessages(s.id, [userMsg("a"), userMsg("b")]);
		await store.appendMessages(s.id, [userMsg("c")]);

		const back = await store.readSession(s.id);
		const seqs = (back?.messages || []).map(
			(m) => (m as PersistedAgentMessage & { _context_meta?: { session_message_seq?: number } })?._context_meta?.session_message_seq,
		);
		expect(seqs).toEqual([1, 2, 3]);
		expect(back?.next_message_seq).toBe(4);
	});

	it("seqs keep growing across multiple appends (never reused)", async () => {
		const store = new SessionStore({ sessionsDir: await newStoreDir() });
		const s = await store.createSession({ slide: SLIDE, kind: "main" });
		for (let i = 0; i < 5; i++) {
			await store.appendMessages(s.id, [userMsg(`m${i}`)]);
		}
		const back = await store.readSession(s.id);
		const seqs = (back?.messages || []).map(
			(m) => (m as PersistedAgentMessage & { _context_meta?: { session_message_seq?: number } })?._context_meta?.session_message_seq,
		);
		expect(seqs).toEqual([1, 2, 3, 4, 5]);
		expect(back?.next_message_seq).toBe(6);
	});

	it("lazy migration assigns 1..N to legacy messages on read", async () => {
		const store = new SessionStore({ sessionsDir: await newStoreDir() });
		const s = await store.createSession({ slide: SLIDE, kind: "main" });
		// Manually write a legacy session: messages present, no next_message_seq,
		// no _context_meta.
		const raw: SessionData = store.emptySession(SLIDE, "main", s.id);
		raw.messages = [userMsg("legacy1"), userMsg("legacy2"), userMsg("legacy3")];
		delete (raw as SessionData & { next_message_seq?: number }).next_message_seq;
		await store.writeSession(s.id, raw);

		// Reading triggers migration.
		const back = await store.readSession(s.id);
		expect(back?.next_message_seq).toBe(4);
		const seqs = (back?.messages || []).map(
			(m) => (m as PersistedAgentMessage & { _context_meta?: { session_message_seq?: number } })?._context_meta?.session_message_seq,
		);
		expect(seqs).toEqual([1, 2, 3]);

		// Migration is idempotent: another read produces the same seqs.
		const back2 = await store.readSession(s.id);
		const seqs2 = (back2?.messages || []).map(
			(m) => (m as PersistedAgentMessage & { _context_meta?: { session_message_seq?: number } })?._context_meta?.session_message_seq,
		);
		expect(seqs2).toEqual([1, 2, 3]);
	});

	it("assignMessageSeqs is a no-op once next_message_seq is set", () => {
		const store = new SessionStore({ sessionsDir: "/tmp/unused" });
		const d = store.emptySession(SLIDE, "main", "sess_x");
		d.next_message_seq = 42;
		const before = JSON.stringify(d);
		assignMessageSeqs(d);
		expect(JSON.stringify(d)).toBe(before);
	});

	it("assignSeqToMessage stamps and advances next_message_seq", () => {
		const store = new SessionStore({ sessionsDir: "/tmp/unused" });
		const d = store.emptySession(SLIDE, "main", "sess_x");
		const m1 = userMsg("a");
		const m2 = userMsg("b");
		expect(assignSeqToMessage(d, m1)).toBe(1);
		expect(assignSeqToMessage(d, m2)).toBe(2);
		expect(d.next_message_seq).toBe(3);
		expect((m1 as { _context_meta?: { session_message_seq?: number } })._context_meta?.session_message_seq).toBe(1);
		expect((m2 as { _context_meta?: { session_message_seq?: number } })._context_meta?.session_message_seq).toBe(2);
	});
});

describe("replaceMessagesPreservingSeq (§10 compaction)", () => {
	it("preserves existing retained-tail seqs and assigns fresh seqs to new messages", () => {
		const store = new SessionStore({ sessionsDir: "/tmp/unused" });
		const d = store.emptySession(SLIDE, "main", "sess_x");
		// Two retained messages already carrying seqs (simulating a retained tail).
		const retained1 = { ...userMsg("retained1"), _context_meta: { session_message_seq: 5 } } as PersistedAgentMessage;
		const retained2 = { ...userMsg("retained2"), _context_meta: { session_message_seq: 6 } } as PersistedAgentMessage;
		d.next_message_seq = 7;
		// New summary message (no seq) + retained tail.
		const summary = userMsg("summary");
		const replacement: PersistedAgentMessage[] = [summary, retained1, retained2];
		replaceMessagesPreservingSeq(d, replacement);
		// Summary got a fresh seq (7); retained kept 5 and 6.
		const seqs = d.messages.map(
			(m) => (m as PersistedAgentMessage & { _context_meta?: { session_message_seq?: number } })?._context_meta?.session_message_seq,
		);
		expect(seqs).toEqual([7, 5, 6]);
		expect(d.next_message_seq).toBe(8);
	});

	it("compaction does not renumber retained messages", () => {
		const store = new SessionStore({ sessionsDir: "/tmp/unused" });
		const d = store.emptySession(SLIDE, "main", "sess_x");
		const retained = { ...userMsg("kept"), _context_meta: { session_message_seq: 100 } } as PersistedAgentMessage;
		d.next_message_seq = 101;
		// Replace with [newSummary, retained]; retained's seq 100 must survive.
		replaceMessagesPreservingSeq(d, [userMsg("new"), retained]);
		expect(
			(d.messages[1] as PersistedAgentMessage & { _context_meta?: { session_message_seq?: number } })?._context_meta?.session_message_seq,
		).toBe(100);
		// New message got 101.
		expect(
			(d.messages[0] as PersistedAgentMessage & { _context_meta?: { session_message_seq?: number } })?._context_meta?.session_message_seq,
		).toBe(101);
	});
});

// =========================================================================== //
// _context_meta stripping at boundaries (§10)
// =========================================================================== //
describe("stripContextMeta (§10 Provider/UI boundary)", () => {
	it("stripContextMetaMessage removes _context_meta, keeps other fields", () => {
		const m = {
			role: "user",
			content: "hi",
			timestamp: 123,
			_context_meta: { session_message_seq: 7 },
		} as PersistedAgentMessage;
		const out = stripContextMetaMessage(m);
		expect("_context_meta" in out).toBe(false);
		expect((out as { role: string }).role).toBe("user");
		expect((out as { content: string }).content).toBe("hi");
	});

	it("stripContextMetaMessage passes through messages without meta", () => {
		const m = { role: "user", content: "hi", timestamp: 1 } as PersistedAgentMessage;
		const out = stripContextMetaMessage(m);
		expect(out).toBe(m); // same reference
	});

	it("stripContextMeta strips all messages in an array", () => {
		const msgs = [
			{ role: "user", content: "a", _context_meta: { session_message_seq: 1 } },
			{ role: "user", content: "b" },
			{ role: "user", content: "c", _context_meta: { session_message_seq: 3 } },
		] as PersistedAgentMessage[];
		const out = stripContextMeta(msgs);
		for (const m of out) {
			expect("_context_meta" in m).toBe(false);
		}
		expect(out.length).toBe(3);
		expect((out[0] as { content: string }).content).toBe("a");
	});

	it("buildTranscript output contains no _context_meta", () => {
		const store = new SessionStore({ sessionsDir: "/tmp/unused" });
		const d = store.emptySession(SLIDE, "main", "sess_x");
		appendMessages(d, [userMsg("a"), userMsg("b")]);
		const transcript = buildTranscript(d);
		expect(transcript.length).toBe(2);
		for (const m of transcript) {
			expect("_context_meta" in m).toBe(false);
		}
	});
});
