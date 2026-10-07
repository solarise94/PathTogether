# 8428f7f0 修复复核 — 2026-10-07

结论：原有核心复现已转正，但两项直传控制器问题仍需收口。未修改产品代码、未推送、未部署、未接触真实 COS。

## 仍需修复

### [P2] 双标签并发可重复创建同一文件的上传

位置：`static/tools/tools-slides-direct-upload.js:151–154,293–315`。

控制器 busy 只在单页内生效。读取 localStorage 回执和创建 ingestion 之间没有跨页互斥；回执只能防已完成之后的重复点击，不能防两个页面并发首次上传。

独立复现：同一 Chromium context 中两个真实 page，同一账号、同一 File，各自实际点击上传按钮。用路由屏障模拟两个创建请求同时在途，再放行到有状态 API/COS 替身；两个页面各创建一次，合计 2 个 ingestion。没有使用两个独立浏览器存储空间。COS 与 ingestion 是替身，未产生线上重复任务。

修复建议：账号＋内容身份的 Web Lock（或等价可靠互斥），拿锁后重新读取上传记录／回执。另一页显示正在上传或已完成，并复用结果。用两个真实 page 同时点击的测试验证，不用检查 localStorage 字段存在代替并发验证。

### [P2] 项目关联重试没有绑定当前回执，重试了另一份切片

位置：`static/tools/tools-slides-direct-upload.js:208–215`、`static/tools/tools-slides.js:439–444`。

`retryAssociation()` 不接收 job/receipt ID，也不检查当前账号，直接取所有本地回执中第一条 pending。

独立页面复现：localStorage 留有 A、B 两条待关联回执；重新选择 B 后页面显示“切片 B 已发布，但加入项目失败”。点击实际“重试加入项目”按钮，却请求 `/api/project/project-A/slides`，body 为 `slide_ids:[slide-A]`，之后页面显示 A 成功。B 仍未完成。另用不同 account 的回执验证，选择代码同样拿第一条；实际服务端权限仍会阻止不合法的跨账号关联，未声称绕过权限。

修复建议：按钮绑定当前 receipt/job ID；重试前重新核对登录账号与回执账号；只修改被点选的回执。覆盖同账号两条 pending，以及退出重登另一账号两个场景。

## 已独立确认转正

复跑原审查脚本后：

- 原生转换产物 OME-TIFF 显示直传面板，无错误。
- 单标签连续点击：创建数保持 1。
- 无内容凭证的同名同大小旧记录不再跳过分片，分片 1、2 全部上传。
- 项目关联失败返回 ok=false、不回调完整成功（原脚本只读旧上传记录 key；不以它证明新回执内容）。
- 小端、大端、经典大端、首 IFD 位于 512 KiB 后、两份实际转换产物均分类 ome-tiff。

定向测试：

- vitest `tools-direct-upload`、`cos-uploader-shared`、`slide-sniff`：80 passed、2 skipped。
- pytest `test_admin_plugin.py`、`test_upload_direct_class.py`：89 passed。
- diff 核对 `8d89b2ea..8428f7f0`：只有文档、插件清单/source-policy 与测试，无转换器／嗅探实现变化，因此没有为这轮复核重跑六个大型样本转换。

本轮未重新执行全量门禁、真实 COS、Windows/macOS。用户提供的全量门禁数字与实际复跑范围分开记录。

## 发布建议

- 六种新增格式暂时保留直传；不在本轮修复中顺便关闭。关闭应先明确未覆盖变体的退路。
- 修完上述两项再进行候选真实 COS 验收、迁移 0078 演练及发布。
- 管理插件 0.4.15 的校验通过不等于生产安装已经切换，仍需独立发布并确认现有未提交改动的版本归属。
- localhost 属安全上下文，本轮浏览器测试可用 WebCrypto；不能简单把所有 HTTP 都归为不可用，应以 crypto.subtle 实际能力判定。
- 新摘要实现改变上传内存路径，现有转换内存报告不能代替上传时 32 MB × 3 通道的实测。

私有证据：`.gate-tmp/recheck-8428f7f0/` 中 `two-tabs.cjs`、`two-tabs.json`、`dogfood.json`、`sniff.txt`、`units.log`、`backend.log`。所有凭据来自一次性本地测试服务。
