/**
 * SSRF 连接层守卫：固定公网 IP、禁止重定向、拒绝 loopback/私网。
 */
import dns from "node:dns";
import http from "node:http";
import { afterAll, describe, expect, it } from "vitest";

import {
	isBlockedHostname,
	isBlockedIp,
	ssrfGuardedFetch,
	SsrfError,
	withSsrfGuard,
	withTrustedCallback,
} from "../src/ssrf-guard.js";

async function listen(
	handler: http.RequestListener,
): Promise<{ url: string; close: () => Promise<void> }> {
	const server = http.createServer(handler);
	await new Promise<void>((resolve) => {
		server.listen(0, "127.0.0.1", () => resolve());
	});
	const addr = server.address();
	if (!addr || typeof addr === "string") throw new Error("no addr");
	return {
		url: `http://127.0.0.1:${addr.port}`,
		close: () =>
			new Promise((resolve, reject) => server.close((err) => (err ? reject(err) : resolve()))),
	};
}

describe("isBlockedIp / hostname", () => {
	it("blocks loopback, private, link-local, metadata", () => {
		expect(isBlockedIp("127.0.0.1")).toBe(true);
		expect(isBlockedIp("10.0.0.1")).toBe(true);
		expect(isBlockedIp("192.168.1.1")).toBe(true);
		expect(isBlockedIp("169.254.169.254")).toBe(true);
		expect(isBlockedIp("100.64.0.1")).toBe(true);
		expect(isBlockedIp("172.16.0.1")).toBe(true);
		expect(isBlockedIp("::1")).toBe(true);
		expect(isBlockedIp("[::1]")).toBe(true);
		expect(isBlockedIp("::ffff:127.0.0.1")).toBe(true);
		expect(isBlockedIp("[::ffff:127.0.0.1]")).toBe(true);
		expect(isBlockedIp("[::ffff:7f00:1]")).toBe(true);
		expect(isBlockedIp("8.8.8.8")).toBe(false);
		expect(isBlockedIp("1.1.1.1")).toBe(false);
	});

	it("blocks localhost and metadata hostnames", () => {
		expect(isBlockedHostname("localhost")).toBe(true);
		expect(isBlockedHostname("metadata.google.internal")).toBe(true);
		expect(isBlockedHostname("127.0.0.1")).toBe(true);
		expect(isBlockedHostname("[::1]")).toBe(true);
		expect(isBlockedHostname("::1")).toBe(true);
		expect(isBlockedHostname("[::ffff:127.0.0.1]")).toBe(true);
		expect(isBlockedHostname("example.com")).toBe(false);
	});
});

describe("ssrfGuardedFetch", () => {
	const servers: Array<() => Promise<void>> = [];
	const origFlask = process.env.AI_FLASK_URL;
	afterAll(async () => {
		if (origFlask === undefined) delete process.env.AI_FLASK_URL;
		else process.env.AI_FLASK_URL = origFlask;
		for (const close of servers) await close();
	});

	it("rejects loopback URLs", async () => {
		await expect(ssrfGuardedFetch("http://127.0.0.1:9/v1")).rejects.toBeInstanceOf(SsrfError);
	});

	it("rejects private IP literals before connect", async () => {
		await expect(ssrfGuardedFetch("http://169.254.169.254/latest/meta-data/")).rejects.toBeInstanceOf(
			SsrfError,
		);
		await expect(ssrfGuardedFetch("http://10.1.2.3/v1")).rejects.toBeInstanceOf(SsrfError);
	});

	it("rejects IPv6 loopback literals before connect", async () => {
		await expect(ssrfGuardedFetch("http://[::1]:9/v1")).rejects.toBeInstanceOf(SsrfError);
		await expect(ssrfGuardedFetch("http://[::ffff:127.0.0.1]:9/v1")).rejects.toBeInstanceOf(
			SsrfError,
		);
	});

	it("does not exempt the Flask origin outside the trusted callback context", async () => {
		const srv = await listen((_req, res) => {
			res.end("ok");
		});
		servers.push(srv.close);
		process.env.AI_FLASK_URL = srv.url;
		await expect(ssrfGuardedFetch(srv.url + "/internal")).rejects.toBeInstanceOf(SsrfError);
		await expect(withSsrfGuard(() => fetch(srv.url + "/internal"))).rejects.toBeInstanceOf(SsrfError);
	});

	it("allows the Flask callback origin only inside withTrustedCallback", async () => {
		const srv = await listen((_req, res) => {
			res.end("ok");
		});
		servers.push(srv.close);
		process.env.AI_FLASK_URL = srv.url;
		const res = await withSsrfGuard(() =>
			withTrustedCallback(() => ssrfGuardedFetch(srv.url + "/internal")),
		);
		expect(res.status).toBe(200);
		expect(await res.text()).toBe("ok");
	});

	it("does not follow 302 even from the Flask origin", async () => {
		const srv = await listen((_req, res) => {
			res.writeHead(302, { Location: "http://169.254.169.254/latest/meta-data/" });
			res.end();
		});
		servers.push(srv.close);
		process.env.AI_FLASK_URL = srv.url;
		await expect(
			withSsrfGuard(() => withTrustedCallback(() => ssrfGuardedFetch(srv.url + "/redir"))),
		).rejects.toThrow();
	});

	it("withSsrfGuard patches global fetch for the async context only", async () => {
		await expect(withSsrfGuard(() => fetch("http://127.0.0.1:9/v1"))).rejects.toBeInstanceOf(SsrfError);
	});
});

describe("dns.lookup under SSRF guard", () => {
	it("rejects localhost resolution inside the guard (rebinding window)", async () => {
		const err = await new Promise<NodeJS.ErrnoException | null>((resolve) => {
			withSsrfGuard(() => {
				dns.lookup("localhost", (e) => resolve(e));
			});
		});
		expect(err).toBeTruthy();
		expect(err?.message || "").toMatch(/内网|回环|元数据/);
	});

	it("still resolves localhost outside the guard (owner CPA / Flask)", async () => {
		const result = await new Promise<{ err: NodeJS.ErrnoException | null; address?: string }>(
			(resolve) => {
				dns.lookup("localhost", (err, address) => resolve({ err, address }));
			},
		);
		expect(result.err).toBeNull();
		expect(result.address).toBeTruthy();
	});

	it("does not exempt Flask hostname without trusted callback (same-host other port)", async () => {
		const prev = process.env.AI_FLASK_URL;
		process.env.AI_FLASK_URL = "http://localhost:8000";
		try {
			const err = await new Promise<NodeJS.ErrnoException | null>((resolve) => {
				withSsrfGuard(() => {
					dns.lookup("localhost", (e) => resolve(e));
				});
			});
			expect(err).toBeTruthy();
			expect(err?.message || "").toMatch(/内网|回环|元数据/);
		} finally {
			if (prev === undefined) delete process.env.AI_FLASK_URL;
			else process.env.AI_FLASK_URL = prev;
		}
	});

	it("allows the Flask callback hostname only inside withTrustedCallback", async () => {
		const prev = process.env.AI_FLASK_URL;
		process.env.AI_FLASK_URL = "http://localhost:8000";
		try {
			const result = await new Promise<{ err: NodeJS.ErrnoException | null; address?: string }>(
				(resolve) => {
					withSsrfGuard(() => {
						withTrustedCallback(() => {
							dns.lookup("localhost", (err, address) => resolve({ err, address }));
						});
					});
				},
			);
			expect(result.err).toBeNull();
			expect(result.address).toBeTruthy();
		} finally {
			if (prev === undefined) delete process.env.AI_FLASK_URL;
			else process.env.AI_FLASK_URL = prev;
		}
	});
});
