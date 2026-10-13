/* PathSegmentor Measure（测量插件 PoC）。
 *
 * 本插件的重点是**服务端测量能力**（manifest.provides 的 segment_region +
 * backend/app.py）；UI 侧无交互逻辑——此文件仅作为 manifest ui.entry 的
 * 最小合法占位（schema 要求 ui 必填），后续测量结果叠加可按
 * plugins/sdk/ui/bridge-client.js 的 HostBridge 用法扩展。
 */
window.PathSegmentorMeasure = { capability: "segment_region", backendOnly: true };
