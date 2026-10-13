# 网站 Logo（2026-10-13）

使用用户提供的“眨眼的组织学女仆助手”透明 PNG。来源及原文件 SHA-256
记录在 `branding-logo-source.json`；仅缩放、压缩和生成图标，没有重新绘制。

- 首页和公开指南显示 40px 图标；工作台、Demo 与转换页使用 32px 图标。
- 常规页眉资源为 96px WebP（7,580 bytes），高清资源为 192px WebP
  （22,140 bytes）；512px 资源为后续用途保留，不在页眉加载。
- ICO 包含 16/32/48px，另有 48px PNG 与 180px Apple touch icon。
- 所有完整 HTML 模板复用图标片段，品牌文字与图片分离，语言切换不会清掉图片。
- 图片来自本站 `/static/brand/`；网页运行时不依赖提供原图的 CDN。
- `/favicon.ico` 为匿名可读的静态图标别名，robots 精确放行该路径。

验证产物在 `/mnt/ssd2/tests/logo-ZDrdCg`：

- 相关公开页面/工具页 Python：30 通过。
- 已有首页 Playwright：3 通过；图片请求允许列表增加品牌资源目录，
  首页内容图片的原有检查保留。
- 浏览器人工走查首页、指南、转换页与登录后的工作台，1440px / 390px
  无横向溢出、图片正常加载、无 JavaScript 异常。
- 三个图标入口均返回 200；手机工作台收起侧栏后 Logo 在页眉正常显示。

预览：`home-1440.png`、`home-390.png`、`workbench-1440.png`、
`workbench-mobile-closed.png`。浏览器检查明细为 `browser-check.json`。

已与统计修复一同发布，0083 已执行。上线时间为 2026-10-13 10:49:18
（UTC+8），见 `site-branding-stats-release-20261013.md`。
