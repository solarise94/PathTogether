# ux-formats 生产发布 — 2026-10-05

## 授权与最终版本

用户接受 compact 有损模式与 MRXS v2 视觉效果后明确要求“发布吧”，并要求官网将“本地切片工具”改为“切片格式转换工具”、提高入口位置。

- 应用提交：`3fcd388b9a5689535267030205d922b43085428c`，分支 `ux-formats`，基于已验收 d861dd1 加官网入口及中英文名称调整。
- 镜像：`localhost/pathtogether-demo:suite-20261005`。
- 镜像 ID：`6344df71f79b8660d47a2615f560eb63017c4bd66da2e1c98cb705777d9a0604`。
- 摘要：`sha256:512dc66e3077eea9956878f2f44182ad803bed54047898c717f94678b6722e2d`。
- 生产容器启动时间：2026-10-05 01:16:00 +08:00。
- Git 未推送；部署使用本地提交的 git archive，在 homePC 以仓库 Containerfile 构建。

## 官网调整

首屏操作区下方新增醒目的转换工具卡片，说明支持格式、浏览器本地转换、无需登录及可选上传。导航、工具页标题、品牌文字和工作台跳转提示同步更名，提供中英文文案。桌面、390px 手机和英文版均经过真实 Chromium 检查：入口正确、无横向溢出、无页面脚本异常。截图保存在私有发布证据目录。

## 发布前检查

- 镜像 /app 的 334 个应用文件与提交源码逐字节一致；转换器 WASM/JS/runner/engine/worker 符合构建清单。
- 无服务端代码或迁移变更，仍为 77 个迁移；转换引擎与已验收候选一致。
- 验收容器七个后台 worker 全部关闭；健康、CSP、90 个静态文件通过。
- 正式 staged 容器与旧生产配置一致：容量 10,000,000,000 B、安全余量 500,000,000 B、单次上限 9,500,000,000 B。
- 候选页面小型 KFB 转换并保存（系统保存对话框使用 OPFS 桩）与原生输出逐字节一致。
- 备份位于 homePC `~/releases/suite-20261005/precutover.dump`：3,312,210 B，pg_restore 列表校验 632 行；本次未再次恢复备份。
- 停机前和停机后七类进行中任务计数均为 0。

## 切换及公网验收

执行更新镜像身份和目录后的既有 deploy helper，保留此前修复的部分改名状态回滚逻辑。旧容器停止后再次确认无进行中任务，然后切换。新服务健康，sidecar 可达。

- `https://histopilot.cn` 与 `https://pt.solarise94.fun`：TLS 验证开启，健康通过，90 个静态文件均与最终镜像一致。
- 公网 Chromium：中文桌面、中文手机、英文首页及跳转通过；KFB 本地转换并保存与原生逐字节一致。
- 普通测试账号在真实公网完成“转换并上传”：产物 9,537,416 B，真实 COS PUT 200，4 个 XHR 字节进度事件；发布完成后信息接口及低倍、全分辨率图块均 200。
- 测试产物服务端 SHA-256 与浏览器验证结果一致。
- 测试切片已删除；该任务 completed / viewer_ready，远端清理 cleaned，中转预留 0、对象版本及删除标记 0、未完成分片上传 0。测试账号最终 used_bytes=6,359,395、reserved_bytes=0。

本次测试不是完整重复所有故障场景；之前的候选验收仍适用于未改动的转换和上传实现。

## 回滚与证据

- 回滚目标：`pathtogether-demo-pre-suite-20261005`（原 suite-20261003）。
- 命令：在 homePC `~/releases/suite-20261005/` 执行 `python3 deploy.py rollback`。
- 无数据库迁移，不需数据库回滚。旧版不支持的新格式／编码任务不要在回滚后的页面续跑，应保留 OPFS。
- 本地私有原始证据：worktree `.gate-tmp/release-20261005/`；远端发布文件：上述 homePC 目录。原始记录含测试账号标识，不提交。
- 验收边界：Windows 按用户决定不测；Linux Docker 限制内存已通过；真实 OS 保存对话框仍未验证。视觉验收是用户接受，不代表像素相等。

相关：[有损视觉验收](ux-formats-compact-visual-acceptance-20261005.md)、[MRXS 视觉验收](ux-formats-mrxs-visual-acceptance-20261005.md)、[Linux 内存验收](ux-formats-linux-memory-20261005.md)、[独立发布复核](ux-formats-release-recheck-20261004.md)。

## 官网更新说明补发（2026-10-05 11:15 CST）

用户随后授权推送 Git，并在官网补充更新内容。

- 应用提交 `e46d37e9e146f1301879a7df0301f5ce75ad6a99` 已推送 `origin/ux-formats`；未合并 main。
- 首页“更新内容”新增 2026.10.05 中英文五条：转换入口、格式范围、有损模式、本地保存／一键上传、上传阶段及字节进度。
- 镜像 `localhost/pathtogether-demo:suite-20261005-notes`，ID `39bc8dcbb57bb666c09f6c43df15bfd3a1c9c9a0cb6c705d0bcb7da478b23e3f`，digest `sha256:682e42d1ef378ee0c5507aaff0d94c0100a943bf406ce32b531f4a4158f55a76`。
- 与上一生产镜像逐文件比较，仅 `static/releases.json` 不同。现有更新内容单元测试 4 个通过（vitest 同时发现 scratch 副本另跑 4 个，不计作额外覆盖）。
- 按原流程完成候选验收、备份、停机前后零进行中任务检查、切换及两域名 90 个静态文件校验；CSP、预算不变。
- 公网 Chromium 确认 histopilot.cn 中文与 pt.solarise94.fun 英文均将本次版本展示在最前、各五条。
- 当前回滚 helper：homePC `~/releases/suite-20261005-notes/deploy.py rollback`，恢复 `pathtogether-demo-pre-suite-20261005-notes`（保留全部转换功能的 suite-20261005）。
- 本次备份 3,317,346 B，632 行 restore TOC；私有证据位于本地 `.gate-tmp/release-20261005-notes/` 及 homePC 同名发布目录。
