/**
 * AI reading assistant sidecar — process entry point (Step 3).
 *
 * Resolves env-based configuration, runs boot-time session recovery, and
 * starts the HTTP server. The sidecar does NOT read ai_config.json — the
 * per-run engine config is injected by the caller (Flask proxy, Step 5) in
 * each request body.
 *
 * Env:
 *   AI_SIDECAR_PORT   listen port (default 8055)
 *   AI_SIDECAR_HOST   listen host (default 127.0.0.1). 仅在私有容器网络
 *                     且不发布宿主端口时才设 0.0.0.0；`--network host` 下
 *                     必须保持 127.0.0.1，否则无鉴权接口会绑到宿主网卡。
 *   AI_INTERNAL_TOKEN inbound /run|/sessions 等与 Flask 回调共用；缺省读
 *                     SHARE_DATA_DIR/ai_internal.token。有 token 则除 /healthz
 *                     外要求 X-AI-Internal-Token。非 loopback 绑定且 token 为空
 *                     时拒绝启动（fail-closed）；仅 127.0.0.1/::1/localhost
 *                     或 ALLOW_UNAUTH_SIDECAR=1 允许无 token。
 *   AI_SESSIONS_DIR   session store dir (default ~/.svs-sidecar/sessions;
 *                     同容器由 docker_entry.sh export /data/sidecar-sessions)
 *   AI_FLASK_URL      Flask callback base URL (default http://127.0.0.1:8000)
 */
import { SessionStore } from "./session-store.js";
import { SessionEventBus } from "./events.js";
import { AgentRunner } from "./agent-runner.js";
import { SidecarServer, assertInboundAuthAllowed } from "./server.js";
import { createFlaskClient, resolveAiInternalToken } from "./flask-client.js";
import { LegacyFlaskPlatformAdapter } from "./platform/legacy-flask-adapter.js";
import { PathTogatherHttpClient } from "./platform/http-client.js";
import { resolvePluginCredentials } from "./platform/plugin-credentials.js";
import type { PlatformClient } from "./platform/contract.js";

/**
 * Resolve the platform {@link PlatformClient} for production:
 *
 *   - WITH plugin credentials (env PLUGIN_INSTALLATION_ID + PLUGIN_HISTOPILOT_SECRET,
 *     or the platform's `plugin-secret-histopilot.txt`) → the formal
 *     `/api/plugin/v1` client (Bearer JWT + X-Run-Grant + unified envelope).
 *   - WITHOUT credentials → the legacy `/internal/ai/*` adapter (intranet/dev
 *     unchanged), with a warn log. AI_FLASK_URL remains the base URL source.
 */
async function resolvePlatformClient(baseUrl: string): Promise<PlatformClient> {
	const creds = await resolvePluginCredentials();
	if (creds) {
		return new PathTogatherHttpClient({
			baseUrl,
			installationId: creds.installationId,
			secret: creds.secret,
		});
	}
	console.warn(
		"[sidecar] 未找到插件凭证（PLUGIN_INSTALLATION_ID/PLUGIN_HISTOPILOT_SECRET 或 plugin-secret-histopilot.txt）；回退 legacy /internal/ai/* 适配器",
	);
	const flaskEngine = await createFlaskClient();
	return new LegacyFlaskPlatformAdapter({ flask: flaskEngine });
}

async function main(): Promise<void> {
	const store = new SessionStore();
	await store.ensureDir();

	// Boot recovery (ai_session.py:498-504 generalized): any session left
	// "running" by a crashed process is flipped to "paused", and every
	// session's last_event_seq is reconciled against its events file tail.
	const recovery = await store.recoverOnBoot();
	if (recovery.paused.length || recovery.repaired.length) {
		console.log(
			`[sidecar] boot recovery: ${recovery.paused.length} session(s) paused, ${recovery.repaired.length} seq-repaired`,
		);
	}
	if (recovery.legacy.length) {
		console.warn(
			`[sidecar] boot recovery: ${recovery.legacy.length} legacy session file(s) skipped (see warnings above)`,
		);
	}

	const bus = new SessionEventBus(store);
	// Production wiring (§9.2 / Stage 4-1b): prefer the formal /api/plugin/v1
	// client when plugin credentials are present; fall back to the legacy Flask
	// adapter otherwise. HistoPilot core (AgentRunner / SidecarServer) sees only
	// the PlatformClient surface either way.
	const baseUrl = process.env.AI_FLASK_URL || "http://127.0.0.1:8000";
	const flask = await resolvePlatformClient(baseUrl);
	const runner = new AgentRunner(store, bus, flask);

	const port = parseInt(process.env.AI_SIDECAR_PORT || "", 10) || 8055;
	const host = process.env.AI_SIDECAR_HOST || "127.0.0.1";
	const allowUnauth = envFlag("ALLOW_UNAUTH_SIDECAR");
	let inboundToken = "";
	try {
		inboundToken = await resolveAiInternalToken();
	} catch {
		inboundToken = "";
	}
	if (!inboundToken) {
		assertInboundAuthAllowed(host, inboundToken, allowUnauth);
		console.warn(
			"[sidecar] inbound auth disabled (loopback or ALLOW_UNAUTH_SIDECAR): set AI_INTERNAL_TOKEN for production",
		);
	}
	const server = new SidecarServer({
		host, port, store, bus, flask, runner,
		internalToken: inboundToken, allowUnauth,
	});
	await server.start();
	console.log(`[sidecar] listening on http://${host}:${port}`);
}

function envFlag(name: string): boolean {
	const v = (process.env[name] || "").trim().toLowerCase();
	return v === "1" || v === "true" || v === "yes";
}

main().catch((err) => {
	console.error("[sidecar] fatal:", err);
	process.exit(1);
});
