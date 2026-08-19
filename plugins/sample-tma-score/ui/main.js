/* Sample TMA Score（插件能力层 P1 示例插件）。
 *
 * 本示例的重点是**服务端能力**（manifest.provides + backend/app.py 的
 * /capabilities/slide_summary），UI 侧暂无交互逻辑——此文件仅作为 manifest
 * ui.entry 的占位（schema 要求 ui 必填），后续可按 sample-annotator 的
 * HostBridge 用法扩展（plugins/sdk/ui/bridge-client.js）。
 */
window.SampleTmaScore = { capability: "slide_summary", backendOnly: true };
