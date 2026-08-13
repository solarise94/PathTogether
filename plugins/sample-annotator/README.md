# Sample Annotator（示例插件，Stage 5-2）

一个**完全不依赖 HistoPilot 源码**的最小示例插件，演示通用插件 SDK
`plugins/sdk/bridge-client.js`（`window.PluginSDK.createPluginBridge`）与平台 bridge
权限门（`static/plugin-permissions.js`）。

## 结构

- `manifest.json`：插件清单（docs §7.1），四个版本字段相互独立。
- `ui/index.html`：独立可开的 UI 入口（引 `/static/bridge-version.js` +
  `/plugins/sdk/bridge-client.js` + `ui/main.js`），平台 iframe 或直开均可。
- `ui/main.js`：用 SDK 读取当前切片 metadata、导航 Viewer、创建测试标注、演示越权失败。

## service.baseUrl 说明

示例插件**没有自己的后端服务**，`manifest.json` 的
`service.baseUrl` 填 `"/"`、`health` 填 `"/healthz"`（即指向平台自身）。

原因：`plugins/sdk/manifest.py` 的 `validate_manifest` 要求 `service.baseUrl` 与
`service.health` 为**非空字符串**（任务书要求不改校验器）。JSON 不支持注释，故在此说明：
该值仅用于占位以满足 schema，示例插件的全部能力都通过前端 HostBridge 与平台交互，
并不真正调用该 baseUrl。

## 来源策略（Stage 5-3，manifest sha256 pin）

本插件目录名 `sample-annotator` 是**来源策略的 plugin key**。平台在
`plugins/source-policy.json` 中 pin 了本 manifest 的 sha256；改动 `manifest.json` 后
需重算并同步该 pin：

```sh
shasum -a 256 plugins/sample-annotator/manifest.json
```

hash 不匹配 → 静态路由 403（`reason: source policy mismatch`）、index 不注入示例脚本。
策略文件缺失 = dev 模式全放行。详见 `docs/plugin-operations.md` 第 2 节。

## 权限门演示

`manifest.json` 声明 `slide:metadata:read`、`viewer:navigate`、`annotation:write`。

- 「读取当前切片」→ `slide.getCurrent`（需 `slide:metadata:read`，已声明 → 放行）；
- 「导航到中心」→ `viewer.navigate`（需 `viewer:navigate`，已声明 → 放行）；
- 「创建测试标注」→ `annotation.create`（需 `annotation:write`，已声明 → 放行）；
- 「越权演示」→ `annotation.read`（需 `annotation:read`，**未声明** → 权限门稳定返回
  `permission_denied`，UI 展示错误）。
