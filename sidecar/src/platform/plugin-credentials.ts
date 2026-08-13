/**
 * Plugin installation credential resolution (Stage 4-1b).
 *
 * The sidecar needs `{installation_id, secret}` to build a
 * {@link PathTogatherHttpClient}. These come from either the environment or the
 * secret file that the Flask platform bootstraps:
 *
 *   - env `PLUGIN_INSTALLATION_ID` + `PLUGIN_HISTOPILOT_SECRET` (operator-managed);
 *   - file `SHARE_DATA_DIR/plugin-secret-histopilot.txt`, which the platform
 *     writes as JSON `{"installation_id": "...", "secret": "..."}` (4-1b
 *     format). For backward compatibility with the pre-4-1b single-line plain
 *     secret file, a non-JSON file is accepted as the secret — but without an
 *     installation_id (env must supply it) we cannot build a v1 client, so the
 *     caller falls back to the legacy adapter.
 *
 * This module never logs the resolved secret.
 */
import { promises as fs } from "node:fs";
import { homedir } from "node:os";
import { join } from "node:path";

/** Default sidecar data dir (mirrors flask-client's `defaultDataDir`). */
export function defaultPluginDataDir(): string {
	return process.env.SHARE_DATA_DIR || join(homedir(), "svs-viewer", "share-data");
}

/** The default plugin secret file path for the histopilot installation. */
export function defaultPluginSecretFile(): string {
	return join(defaultPluginDataDir(), "plugin-secret-histopilot.txt");
}

export interface PluginCredentials {
	installationId: string;
	secret: string;
}

export interface ResolvePluginCredentialsOptions {
	/** Override env (tests). Defaults to `process.env`. */
	env?: NodeJS.ProcessEnv;
	/** Absolute path to the plugin secret file. Defaults to the platform file. */
	secretFile?: string;
}

/**
 * Resolve the plugin credentials, or return `null` when absent/incomplete. The
 * caller falls back to the legacy `/internal/ai/*` adapter in that case.
 */
export async function resolvePluginCredentials(
	opts: ResolvePluginCredentialsOptions = {},
): Promise<PluginCredentials | null> {
	const env = opts.env ?? process.env;
	let installationId = env.PLUGIN_INSTALLATION_ID?.trim() || "";
	let secret = env.PLUGIN_HISTOPILOT_SECRET?.trim() || "";

	const filePath = opts.secretFile || defaultPluginSecretFile();
	try {
		const raw = (await fs.readFile(filePath, "utf8")).trim();
		if (raw) {
			try {
				const parsed = JSON.parse(raw) as { installation_id?: unknown; secret?: unknown };
				if (!installationId && typeof parsed.installation_id === "string") {
					installationId = parsed.installation_id.trim();
				}
				if (!secret && typeof parsed.secret === "string") {
					secret = parsed.secret.trim();
				}
			} catch {
				// Legacy plain-secret file: whole content is the secret.
				if (!secret) secret = raw;
			}
		}
	} catch {
		// File missing/unreadable → rely on env only.
	}

	if (installationId && secret) {
		return { installationId, secret };
	}
	return null;
}
