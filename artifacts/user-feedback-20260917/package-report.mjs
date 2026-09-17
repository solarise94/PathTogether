// Use the canonical renderer and verifier; apply one document-local fix for its
// 100vw header overflowing by half the system scrollbar width. No plugin edit.
import {deliverPortableArtifact} from '/home/solarise/.codex/plugins/cache/openai-curated-remote/data-analytics/0.2.10-13ceeea1f599/skills/build-report/scripts/deliver_portable_artifact.mjs';
import {buildPortableArtifact} from '/home/solarise/.codex/plugins/cache/openai-curated-remote/data-analytics/0.2.10-13ceeea1f599/skills/build-report/scripts/build_portable_artifact.mjs';
import {fileURLToPath} from 'node:url';
import fs from 'node:fs';
const root=fileURLToPath(new URL('.',import.meta.url));
const result=await deliverPortableArtifact({inputPath:root+'artifact.json',outputPath:root+'report.html'},{build:(input,opts)=>buildPortableArtifact(input,opts).replace('</head>','<style>.dashboard-shell .analytics-top-bar{width:100%;margin-left:0;margin-right:0}</style></head>')});
fs.writeFileSync(root+'delivery-receipt.json',JSON.stringify(result,null,2));
console.log(JSON.stringify(result));
if(!result.ok)process.exitCode=1;
