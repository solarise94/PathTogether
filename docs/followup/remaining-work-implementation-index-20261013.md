# 剩余工作实现索引

日期：2026-10-13。用户要求：已完成部分由 Codex review/合入/发布；未完成部分交用户继续实现。本文只定义未实现事项，不把文档等同于交付。

## 优先实施

1. [Viewer 文件夹体验](folder-experience-implementation-20261013.md)：当前目录上传默认值、刷新恢复、账号隔离、深链接与搜索优先级。已有显式目标关联能力要复用。
2. [Aperio JPEG2000 浏览器转换](jpeg2000-browser-conversion-implementation-20261013.md)：F2 decoder/预算/颜色/重编码/任务恢复完整集成。不能把其他扫描仪、HTJ2K 或 strict-lossless 默认纳入。
3. **HP 矩形标注取消**：当前 `src/tools.ts` 的 `create_annotation.execute(toolCallId, params)` 缺少第三个 loopSignal；参考 polygon 路径接入。异步会话读写前后、Flask annotate 前检查取消，annotate 请求传 AbortSignal；如果写入已经成功，不把“用户随后取消”误报成已撤销服务端写入。effect key 与重试身份沿用现有机制。补三个案例：nextBundleSeq 等待中取消 → 零 annotate；annotate 发送前取消 → 零写入；请求中取消 → 客户端终止且不追加成功会话记录，但明确网络取消不能保证服务器绝不提交。保留锁定、权限、待复核和快照身份检查。运行 HP 单测、构建及跨仓合同；本轮不修改模型授权策略。

## 需材料或产品排期

| 项目 | 开工条件与范围 |
|---|---|
| 赛维尔适配 | 先拿到完整真实样本、许可、文件结构和参考读图结果；复用有界 reader/pyramid/writer。未拿到样本前不做“支持”按钮 |
| 插件能力层 P2 | 从 `docs/plugin-capability-layer-design.md` 接续；逐项实施 write capability、跨插件 scope、UI discovery/invoke 与配额。先写权限/幂等/取消合同，不能只放开 manifest 的 read 限制 |
| PathSegmentor / Cellpose 测量 | 保全旧根目录的 `docs/measurement-models-homepc-implementation.md`；明确模型/许可与 ROI 任务资源预算、授权、长任务取消、mask 存储和计量。与 SAM2 轮廓细化分开验收 |
| 转换完成量统计 | 现在线上只统计工具页访问；若要看使用量，定义 started/completed/failed/canceled 及匿名幂等 event ID。只报格式/结果/时长等最小信息，不上传文件名、组织图、路径、像素或用户输入；本地转换本身仍应无需服务器在线。离线队列、去重、bot/Dogfood 口径必须先定 |
| 历史缺失/无主切片 | 先只读重核今日记录、文件和备份；用户逐项决定恢复/归属/保留。按照 manifest 操作，不用 10 月 2 日旧数量执行批量删除 |
| Mac 帧率/真实保存对话框 | 实机 Performance trace + 大切片拖动/选片；区分瓦片解码、上传、画布与动画。记录硬件、Chrome、缓存状态和 AI 状态。Windows 本轮已豁免 |

## 已改为关闭或发布收尾

- 中国 IP 自动从 .com 跳 .cn：用户 2026-10-13 改为两个主页明确提供“中国线路 / 国际线路”链接；不再部署地域跳转方案。语言仍可在账户菜单独立切换。
- fast 纯流式转发：代码已完成，进入真实 COS 验收及上线接线；不要重复从设计开工，不建立 100G 磁盘缓存。凭证由用户提供并放私有目录，禁止提交 Git。
- 主页协作演示比例：已完成，进入本轮发布。
- SAM2 排队复核关闭记录：补回漏合的纯文档提交；产品修复此前已经上线。
- IPv6 直传：作为备选方案归档；本轮以 fast 转发为现行路径，不同时另建一套中转。

开工从最新远端主线建干净 worktree。根目录旧分支有用户未提交修改，先保存补丁与独有文档后再判定归档；不要重置覆盖。所有本机测试严格用 `/mnt/ssd2/tests/<run>/`，按工作区 AGENTS 设置临时与输出路径。每项交付需记录实际 source/image revision、测试范围、已知未验边界及部署状态。
