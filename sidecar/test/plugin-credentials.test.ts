/**
 * Plugin credential resolution tests (Stage 4-1b).
 */
import { describe, expect, it } from "vitest";
import { promises as fs } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { resolvePluginCredentials } from "../src/platform/plugin-credentials.js";

async function writeFile(dir: string, name: string, content: string): Promise<string> {
	const p = join(dir, name);
	await fs.mkdir(dir, { recursive: true });
	await fs.writeFile(p, content);
	return p;
}

describe("resolvePluginCredentials", () => {
	it("returns creds from env (installation_id + secret), ignoring a missing file", async () => {
		const dir = await fs.mkdtemp(join(tmpdir(), "svs-creds-"));
		const r = await resolvePluginCredentials({
			env: { PLUGIN_INSTALLATION_ID: "pin_env", PLUGIN_HISTOPILOT_SECRET: "env-secret" } as NodeJS.ProcessEnv,
			secretFile: join(dir, "missing.txt"),
		});
		expect(r).toEqual({ installationId: "pin_env", secret: "env-secret" });
	});

	it("reads the JSON file when env is absent (4-1b format)", async () => {
		const dir = await fs.mkdtemp(join(tmpdir(), "svs-creds-"));
		const file = await writeFile(dir, "plugin-secret-histopilot.txt", JSON.stringify({ installation_id: "pin_file", secret: "file-secret" }));
		const r = await resolvePluginCredentials({ env: {} as NodeJS.ProcessEnv, secretFile: file });
		expect(r).toEqual({ installationId: "pin_file", secret: "file-secret" });
	});

	it("reads a legacy plain-secret file as the secret (no installation_id → null)", async () => {
		const dir = await fs.mkdtemp(join(tmpdir(), "svs-creds-"));
		const file = await writeFile(dir, "plugin-secret-histopilot.txt", "legacy-plain-secret\n");
		const r = await resolvePluginCredentials({ env: {} as NodeJS.ProcessEnv, secretFile: file });
		// secret parsed but no installation_id → cannot build a v1 client
		expect(r).toBeNull();
	});

	it("env installation_id can complete a legacy plain-secret file", async () => {
		const dir = await fs.mkdtemp(join(tmpdir(), "svs-creds-"));
		const file = await writeFile(dir, "plugin-secret-histopilot.txt", "legacy-plain-secret\n");
		const r = await resolvePluginCredentials({
			env: { PLUGIN_INSTALLATION_ID: "pin_env" } as NodeJS.ProcessEnv,
			secretFile: file,
		});
		expect(r).toEqual({ installationId: "pin_env", secret: "legacy-plain-secret" });
	});

	it("returns null when neither env nor file provides complete credentials", async () => {
		const dir = await fs.mkdtemp(join(tmpdir(), "svs-creds-"));
		const r = await resolvePluginCredentials({ env: {} as NodeJS.ProcessEnv, secretFile: join(dir, "missing.txt") });
		expect(r).toBeNull();
	});
});
