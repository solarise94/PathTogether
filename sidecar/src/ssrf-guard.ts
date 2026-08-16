/**
 * 用户自带 AI base_url 的连接层 SSRF 守卫。
 *
 * Flask 在保存/注入时已拒绝私网解析结果，但真正的 TCP 连接由 sidecar/Node 再次
 * DNS。攻击者可在两次解析间 rebinding，或让公网端点 30x 跳到内网。
 *
 * 本模块在 `ssrf_guard` 运行上下文中：
 *   1. 连接前校验协议/主机；dns.lookup 只返回已验证的公网 IP（固定解析结果）；
 *   2. fetch 使用 redirect:'error'，禁止跟随 30x。
 *
 * Flask 回调不得在整个 agent run 中按 hostname 豁免：仅 FlaskClient /
 * PathTogatherHttpClient 在 `withTrustedCallback` 内、且请求精确 origin 匹配
 * `AI_FLASK_URL` 时放行。owner/平台 URL 不置 ssrf_guard，可继续使用 loopback CPA。
 */
import { AsyncLocalStorage } from "node:async_hooks";
import dns from "node:dns";
import { isIP, BlockList } from "node:net";

const ssrfAls = new AsyncLocalStorage<{ guard: true; trusted?: boolean }>();

const BLOCKED_HOSTS = new Set([
	"localhost",
	"localhost.localdomain",
	"metadata.google.internal",
	"metadata.goog",
	"metadata",
	"kubernetes.default",
	"kubernetes.default.svc",
]);

const blockList = new BlockList();
blockList.addSubnet("0.0.0.0", 8, "ipv4");
blockList.addSubnet("10.0.0.0", 8, "ipv4");
blockList.addSubnet("100.64.0.0", 10, "ipv4");
blockList.addSubnet("127.0.0.0", 8, "ipv4");
blockList.addSubnet("169.254.0.0", 16, "ipv4");
blockList.addSubnet("172.16.0.0", 12, "ipv4");
blockList.addSubnet("192.168.0.0", 16, "ipv4");
blockList.addSubnet("198.18.0.0", 15, "ipv4");
blockList.addSubnet("224.0.0.0", 4, "ipv4");
blockList.addSubnet("240.0.0.0", 4, "ipv4");
blockList.addAddress("255.255.255.255", "ipv4");
blockList.addAddress("::1", "ipv6");
blockList.addAddress("::", "ipv6");
blockList.addSubnet("fc00::", 7, "ipv6");
blockList.addSubnet("fe80::", 10, "ipv6");
blockList.addSubnet("ff00::", 8, "ipv6");

function unwrapIpLiteral(ip: string): string {
	let raw = ip.trim().toLowerCase();
	if (raw.startsWith("[") && raw.endsWith("]")) {
		raw = raw.slice(1, -1);
	}
	return raw;
}

function mappedIpv4(rest: string): string | null {
	if (isIP(rest) === 4) return rest;
	const parts = rest.split(":");
	if (parts.length !== 2 || !parts.every((p) => /^[0-9a-f]{1,4}$/.test(p))) {
		return null;
	}
	const hi = parseInt(parts[0] ?? "0", 16);
	const lo = parseInt(parts[1] ?? "0", 16);
	return `${(hi >> 8) & 255}.${hi & 255}.${(lo >> 8) & 255}.${lo & 255}`;
}

export function isBlockedIp(ip: string): boolean {
	const raw = unwrapIpLiteral(ip);
	if (raw.startsWith("::ffff:")) {
		const mapped = mappedIpv4(raw.slice("::ffff:".length));
		if (mapped) return isBlockedIp(mapped);
		return true;
	}
	const ver = isIP(raw);
	if (ver === 4) return blockList.check(raw, "ipv4");
	if (ver === 6) return blockList.check(raw, "ipv6");
	return true;
}

export function isBlockedHostname(hostname: string): boolean {
	const host = unwrapIpLiteral(hostname).replace(/\.$/, "");
	if (!host) return true;
	if (BLOCKED_HOSTS.has(host) || host.endsWith(".localhost")) return true;
	if (isIP(host) && isBlockedIp(host)) return true;
	return false;
}

function flaskBaseUrl(): string {
	return (process.env.AI_FLASK_URL || "http://127.0.0.1:8000").replace(/\/+$/, "");
}

function trustedOrigins(): string[] {
	try {
		return [new URL(flaskBaseUrl()).origin];
	} catch {
		return ["http://127.0.0.1:8000"];
	}
}

function trustedFlaskHost(): string {
	try {
		return new URL(flaskBaseUrl()).hostname.trim().toLowerCase().replace(/\.$/, "");
	} catch {
		return "127.0.0.1";
	}
}

function toUrl(input: Parameters<typeof fetch>[0] | URL): URL {
	if (typeof input === "string") return new URL(input);
	if (input instanceof URL) return input;
	return new URL(input.url);
}

export class SsrfError extends Error {
	constructor(message: string) {
		super(message);
		this.name = "SsrfError";
	}
}

function assertSafeUrl(url: URL): void {
	if (url.protocol !== "http:" && url.protocol !== "https:") {
		throw new SsrfError("base_url 仅支持 http 或 https");
	}
	if (url.username || url.password) {
		throw new SsrfError("base_url 不得包含用户名或密码");
	}
	const host = unwrapIpLiteral(url.hostname || "").replace(/\.$/, "");
	if (!host) {
		throw new SsrfError("base_url 缺少主机名");
	}
	if (isBlockedHostname(host)) {
		throw new SsrfError("base_url 不得指向内网、回环或云元数据地址");
	}
}

const origLookup = dns.lookup.bind(dns);

function ssrfLookup(
	hostname: string,
	optionsOrCb: dns.LookupOptions | ((err: NodeJS.ErrnoException | null, address: string, family: number) => void),
	cb?: (err: NodeJS.ErrnoException | null, address: string | dns.LookupAddress[], family?: number) => void,
): void {
	const callback = typeof optionsOrCb === "function" ? optionsOrCb : cb;
	const options = typeof optionsOrCb === "function" ? {} : optionsOrCb || {};
	if (!callback) return;
	const store = ssrfAls.getStore();
	const host = unwrapIpLiteral(hostname).replace(/\.$/, "");
	if (store?.trusted && host && host === trustedFlaskHost()) {
		if (typeof optionsOrCb === "function") {
			origLookup(hostname, optionsOrCb as never);
			return;
		}
		origLookup(hostname, optionsOrCb as never, cb as never);
		return;
	}
	if (isBlockedHostname(hostname)) {
		const e = Object.assign(new SsrfError("base_url 不得指向内网、回环或云元数据地址"), {
			code: "ENOTFOUND",
		}) as NodeJS.ErrnoException;
		callback(e, "", 4);
		return;
	}
	origLookup(hostname, { all: true, verbatim: true }, (err, addresses) => {
		if (err) {
			callback(err, "", 4);
			return;
		}
		const list = Array.isArray(addresses) ? addresses : [];
		const allowed = list.filter((a) => a?.address && !isBlockedIp(String(a.address)));
		const first = allowed[0];
		if (!first) {
			const e = Object.assign(new SsrfError("base_url 不得指向内网、回环或云元数据地址"), {
				code: "ENOTFOUND",
			}) as NodeJS.ErrnoException;
			callback(e, "", 4);
			return;
		}
		if ((options as dns.LookupAllOptions).all) {
			(callback as unknown as (err: NodeJS.ErrnoException | null, address: dns.LookupAddress[]) => void)(
				null,
				allowed,
			);
			return;
		}
		(callback as (err: NodeJS.ErrnoException | null, address: string, family: number) => void)(
			null,
			first.address,
			first.family,
		);
	});
}

function installDnsLookupPatch(): void {
	const current = dns.lookup as typeof dns.lookup & { __ssrfPatched?: boolean };
	if (current.__ssrfPatched) return;
	const patched = ((hostname: string, optionsOrCb?: unknown, cb?: unknown) => {
		if (!ssrfAls.getStore()?.guard) {
			if (typeof optionsOrCb === "function") {
				return origLookup(hostname, optionsOrCb as never);
			}
			return origLookup(hostname, optionsOrCb as never, cb as never);
		}
		return ssrfLookup(hostname, optionsOrCb as never, cb as never);
	}) as typeof dns.lookup & { __ssrfPatched?: boolean };
	patched.__ssrfPatched = true;
	dns.lookup = patched;
}

const origFetch = globalThis.fetch.bind(globalThis);

export async function ssrfGuardedFetch(
	input: Parameters<typeof fetch>[0] | URL,
	init?: RequestInit,
): Promise<Response> {
	const url = toUrl(input);
	if (ssrfAls.getStore()?.trusted && trustedOrigins().includes(url.origin)) {
		return origFetch(input as Parameters<typeof fetch>[0], { ...init, redirect: "error" });
	}
	assertSafeUrl(url);
	return origFetch(url, { ...init, redirect: "error" });
}

export function withSsrfGuard<T>(fn: () => T): T {
	return ssrfAls.run({ guard: true }, fn);
}

/** Flask callback：仅在守卫已激活时嵌套可信上下文，精确 origin 才豁免私网。 */
export function withTrustedCallback<T>(fn: () => T): T {
	const parent = ssrfAls.getStore();
	if (!parent?.guard) {
		return fn();
	}
	return ssrfAls.run({ guard: true, trusted: true }, fn);
}

export function ssrfGuardActive(): boolean {
	return ssrfAls.getStore()?.guard === true;
}

export function installSsrfFetchGuard(): void {
	installDnsLookupPatch();
	const current = globalThis.fetch as typeof fetch & { __ssrfPatched?: boolean };
	if (current.__ssrfPatched) return;
	const wrapped = ((input: Parameters<typeof fetch>[0], init?: RequestInit) => {
		if (!ssrfAls.getStore()?.guard) {
			return origFetch(input, init);
		}
		return ssrfGuardedFetch(input, init);
	}) as typeof fetch & { __ssrfPatched?: boolean };
	wrapped.__ssrfPatched = true;
	globalThis.fetch = wrapped;
}

installSsrfFetchGuard();
