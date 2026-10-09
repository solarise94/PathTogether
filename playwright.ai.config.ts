/** Real installed HistoPilot UI; AI service responses are deterministic fixtures.
 * PLUGIN_BUNDLES_DIR must contain histopilot/ from the supported plugin release.
 * No injected panel markup, paid inference or production credentials. */
import base from './playwright.config';
import { existsSync } from 'node:fs';
import { join } from 'node:path';
if (!process.env.PLUGIN_BUNDLES_DIR || !existsSync(join(process.env.PLUGIN_BUNDLES_DIR, 'histopilot/ui/main.js'))) {
  throw new Error('Set PLUGIN_BUNDLES_DIR to a directory containing the real histopilot bundle.');
}
export default { ...base, testDir: 'tests/ai-e2e' };
