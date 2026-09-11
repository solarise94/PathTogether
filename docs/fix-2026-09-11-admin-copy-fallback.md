# fix-2026-09-11：管理插件「复制」按钮静默失效

## 问题

`plugins/pathtogether-admin/ui/main.js` 的 `copyToClipboard()`（约 483-492 行）：

1. 降级路径是**注释、零代码**——`navigator.clipboard` 不可用（插件 UI 运行在
   `<iframe sandbox="allow-scripts">` 的 opaque origin 中，见
   `templates/admin_host.html`，clipboard API 必然缺失或 reject）时按钮完全无反应；
2. `writeText` 的 rejection 被 `.catch(function () { /* ignore */ })` 吞掉，无任何用户反馈。

受影响：全部 3 个复制按钮（邀请码明文 `adm-invite-token-copy`、插件密钥、
rawValuesDetails 行复制），共用同一 helper。

对照：宿主页 `static/app.js:458-470` 的 `copyText` 有真正的
textarea + `execCommand("copy")` 降级，所以宿主页复制是好的。

## 设计

### 1. `copyToClipboard` 改造（plugins/pathtogether-admin/ui/main.js）

- 保持签名兼容但**返回成功/失败**（可改为返回 Promise<boolean> 或同步 boolean，
  以调用方能简单拿到结果为准；现有 3 个调用点全部在点击处理器里，可同步适配）。
- 路径一：`navigator.clipboard.writeText` 可用 → 调用，rejection 进入降级路径而不是吞掉。
- 路径二（降级）：textarea 离屏挂载 + `select()` + `document.execCommand("copy")`，
  返回其结果（参考 `static/app.js` 的实现写法，风格对齐本文件）。
- 路径三（兜底）：降级也失败时，用 `window.getSelection()` 选中目标文本节点
  方便用户手动 Ctrl+C，并返回失败。

### 2. 调用点加用户反馈

3 个复制按钮的点击处理器：成功 → 现有 UI 反馈惯例（查本文件的 status/toast
helper，例如邀请码区域的状态行）显示「已复制」；失败 → 显示
「复制失败，文本已选中，请手动复制」。文案走本文件既有中文文案风格。

### 3. iframe 授权（templates/admin_host.html）

给 `admin-plugin-frame` 加 `allow="clipboard-write"`。注意这只是让 secure
context（https/localhost）下的主路径可用；plain HTTP 部署仍靠路径二/三，
所以 2/3 不可省。sandbox 属性**不动**（不加 allow-same-origin）。

## 不做

- 不改 iframe 的 sandbox 边界与 postMessage 协议；
- 不引入新依赖、不做全局复制 helper 的统一重构（static/app.js 的 copyText 保持原样）；
- 不改变「明文邀请码只显示一次」的语义。

## 测试与验收

- 若本仓库插件 UI 有 vitest/单测惯例则补 `copyToClipboard` 的单测：
  clipboard 可用并 resolve → true；clipboard reject → 走 textarea 降级；
  clipboard 不存在 → 走 textarea 降级；降级也失败 → false。
  没有惯例则在文档级说明手工验证步骤，不要为一个函数新建测试基建。
- `git diff` 只应触及：`plugins/pathtogether-admin/ui/main.js`、
  `templates/admin_host.html`、（可选）对应测试文件。
