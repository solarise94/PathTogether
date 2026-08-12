/**
 * Phase 4 A/B framework (Wave 2: execution runner) — Flask subprocess lifecycle.
 *
 * Spawns the repo-root Flask app (app.py) on an ephemeral port with the slides
 * directory as UPLOAD_DIR and a known internal token, polls its health endpoint
 * until ready, and tears it down cleanly. The health check targets
 * `GET /api/slides` (a public JSON listing endpoint — no auth needed; the
 * runner spawns Flask WITHOUT ADMIN_PASSWORD so `AUTH_ENABLED` is False and the
 * endpoint is reachable without a login session).
 *
 * NOTE: experiment data plane only — NOT built into the shipped sidecar bundle.
 */
import { spawn, type ChildProcess } from "node:child_process";
import { randomBytes } from "node:crypto";

export interface FlaskHandle {
	/** Base URL (e.g. http://127.0.0.1:38271) of the running Flask. */
	url: string;
	/** The internal token shared with the sidecar FlaskClient. */
	token: string;
	/** The underlying child process (for --keep-flask debugging). */
	child: ChildProcess;
	/** Stop the Flask process (SIGTERM, then SIGKILL after a grace period). */
	stop: () => Promise<void>;
}

export interface SpawnFlaskOptions {
	/** Absolute path to the repo root (where app.py lives). */
	repoRoot: string;
	/** Absolute path to the directory holding the slide files (UPLOAD_DIR). */
	uploadDir: string;
	/** Path to the python interpreter (defaults to repo-root .venv/bin/python). */
	pythonBin?: string;
	/** Explicit port; when omitted an ephemeral free port is chosen. */
	port?: number;
	/** AbortSignal to cancel a pending health-check wait. */
	signal?: AbortSignal;
}

/** Pick a free ephemeral TCP port by binding to :0 and reading the assigned port. */
export async function pickFreePort(): Promise<number> {
	const net = await import("node:net");
	return new Promise((resolve, reject) => {
		const srv = net.createServer();
		srv.unref();
		srv.on("error", reject);
		srv.listen(0, "127.0.0.1", () => {
			const addr = srv.address();
			if (addr && typeof addr === "object") {
				const port = addr.port;
				srv.close(() => resolve(port));
			} else {
				srv.close();
				reject(new Error("could not pick a free port"));
			}
		});
	});
}

/**
 * Poll a Flask URL until its health endpoint responds, or until `timeoutMs`
 * elapses. Health = any HTTP response from `GET /api/slides` (200 when
 * AUTH_ENABLED is False; a connection refused means the server is not up yet).
 *
 * Captures the child's stderr so a startup failure has an actionable message.
 */
export async function waitForFlask(url: string, timeoutMs = 30_000, signal?: AbortSignal): Promise<void> {
	const deadline = Date.now() + timeoutMs;
	const healthUrl = `${url}/api/slides`;
	let lastErr = "";
	while (Date.now() < deadline) {
		if (signal?.aborted) throw new Error("flask health check aborted");
		const controller = new AbortController();
		const timer = setTimeout(() => controller.abort(), 3000);
		try {
			const res = await fetch(healthUrl, { signal: controller.signal });
			clearTimeout(timer);
			// Any HTTP response (200/302/401) means the server is listening.
			// 200 = ready (AUTH_ENABLED False). 401/302 = auth on but up.
			if (res.status < 500) return;
			lastErr = `HTTP ${res.status}`;
		} catch (e) {
			clearTimeout(timer);
			lastErr = (e as Error).message || String(e);
		}
		await new Promise((r) => setTimeout(r, 300));
	}
	throw new Error(`Flask did not become healthy within ${timeoutMs}ms (last error: ${lastErr}). See captured stderr.`);
}

/**
 * Spawn the repo-root Flask app (app.py) on an ephemeral port with the slides
 * directory as UPLOAD_DIR, wait for health, and return a handle.
 *
 * Environment knobs (aligned with app.py):
 *   - PORT: the ephemeral port (default 8000 in app.py; we override).
 *   - UPLOAD_DIR: where Flask reads slide files.
 *   - AI_INTERNAL_TOKEN: shared token for /internal/* callbacks. We set a fresh
 *     random token so the runner's FlaskClient can authenticate; the same value
 *     is exported via the env so generate.py --pin can send it as
 *     X-AI-Internal-Token.
 *   - ADMIN_PASSWORD is deliberately NOT set → AUTH_ENABLED is False → /api/slides
 *     is public (used for the health check) and no login session is needed.
 */
export async function spawnFlask(opts: SpawnFlaskOptions): Promise<FlaskHandle> {
	const pythonBin = opts.pythonBin ?? `${opts.repoRoot}/.venv/bin/python`;
	const port = opts.port ?? (await pickFreePort());
	const token = randomBytes(16).toString("hex");

	const env: NodeJS.ProcessEnv = {
		...process.env,
		PORT: String(port),
		UPLOAD_DIR: opts.uploadDir,
		AI_INTERNAL_TOKEN: token,
		// Ensure auth is off so the health endpoint (and /api/slides) is public.
		ADMIN_PASSWORD: "",
	};
	const child = spawn(pythonBin, ["app.py"], {
		cwd: opts.repoRoot,
		env,
		stdio: ["ignore", "pipe", "pipe"],
	});

	const stderrChunks: string[] = [];
	child.stderr?.on("data", (chunk: Buffer) => {
		const s = chunk.toString("utf8");
		stderrChunks.push(s);
		// Surface Flask startup logs live for debugging.
		process.stderr.write(`[flask] ${s}`);
	});

	const url = `http://127.0.0.1:${port}`;

	const stop = async (): Promise<void> => {
		if (child.exitCode !== null || child.killed) return;
		await new Promise<void>((resolve) => {
			const onExit = (): void => resolve();
			child.once("exit", onExit);
			child.kill("SIGTERM");
			// SIGKILL after a 5s grace period if it hasn't exited.
			setTimeout(() => {
				if (child.exitCode === null && !child.killed) {
					child.kill("SIGKILL");
				}
			}, 5000);
			// Resolve regardless after 6s (defensive).
			setTimeout(resolve, 6000);
		}, );
	};

	try {
		await waitForFlask(url, 30_000, opts.signal);
	} catch (e) {
		await stop();
		const tail = stderrChunks.join("").slice(-4000);
		throw new Error(`${(e as Error).message}\n--- Flask stderr (tail) ---\n${tail}`);
	}

	return { url, token, child, stop };
}
