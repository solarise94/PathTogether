# 分支与近两周文档收尾审计（2026-10-13）

> 后续执行：用户已授权 review、合入和发布完成部分；当前收口分支 `release/closeout-20261013` 已合入主页修复、COS relay 及 `fd0f7832` 关闭记录。剩余实施任务见 [实现索引](followup/remaining-work-implementation-index-20261013.md)。地域跳转已按用户新决定改为主页线路选择。下文保留初次盘点事实，部署与归档结果见 [发布记录](closeout-release-20261013.md)。

## 补充核对：COS fast 转发已实现（同日更新）

用户补充了转发任务的交付记录。本轮另核对了新分支和本地仓库：PT `cos-fast-relay@d49adaf4` 已有 `cos_fast_relay.py` 及 `cos_client.get_object` 的 transport 分发；新仓库 `histopilot-suite/gokapi-relay@7926715` 已有 Go 转发实现和 E2E 验收脚本。因此，初次盘点的“fast 仅设计”已过时，现改为 **两端实现完成、本机专项验收通过，尚未推送、部署或连接真实 COS 验收**。

验收依据在 `cos-fast-relay` 分支的 `docs/cos-fast-relay-acceptance-20261013.md`：274 项 COS 测试通过、模拟 COS 的真实镜像 E2E 25/25 通过；全量测试仍有 127 项依赖环境不匹配导致的失败，基线同样复现，不能表述成全量门禁全绿。本次只核对记录和代码存在性，没有独立重跑这些验收。

剩余收尾：Go 仓库远端托管、只读 COS 身份与版本读取权限、真实 COS 的 Range/VersionId 响应合同、fast 内网解析与 Nginx 配置、PathTogether 发布环境变量/凭证挂载接线，以及上线后的真实链路验收。前文用户误贴的 `feat/v0.4-agent-gateway@04cd867` settings 写锁交付属于另一项任务，不作为本转发部署完成的证据。

下文分支数量保留初次盘点快照；本次新增发现的 PT `cos-fast-relay` 和第三个 Go 仓库应另计。

## 结论与范围

大多数旧分支已经交付；真正的遗留包括一个未发布的 UI 修复、少量功能缺口、设计储备，以及漏合入的复核关闭记录。不能根据历史文档中的“未部署”判断现在仍未部署。

本轮核对两仓远端引用、全部本地分支及 worktree 状态，扫描 2026-09-30 至 2026-10-13 Git 历史中变动的 Markdown：PathTogether 105 个路径，HistoPilot 3 个路径；另检查未提交文档。对重点条目追查后续提交、当前代码和发布证据，并只读核对 homepc 容器版本。没有重跑功能测试、调用模型、修产品代码、合并、删除或部署。

快照：PT `origin/main@169454b5`、本地 release `dc5f5b55`；HP master、origin/master 与 release 均为 `c1db40e`。两仓 GitHub 均无开放 PR。盘点产物：`/mnt/ssd2/tests/branch-audit-20261013/`，包含 inventory.json、inventory.txt、doc-pending.txt。

## 一、明确需要收尾的事项

| 项目 | 当前状态与证据 | 下一步 |
|---|---|---|
| 首页协作演示 UI | `b723ab31` 已本地提交，尚未推送/上线；本地 release 比远端 release 多三个提交，另两个都是 COS 设计文档 | 单独安排发布；不要误认为已随 Logo 上线 |
| SAM2 排队问题的关闭记录 | `codex/navigation-refine-review` 最后提交 `fd0f7832` 未进主线/发布分支；此前两笔复核提交已有等价提交 | 合入这个纯文档提交，使 `navigation-refine-review-20261010.md` 和 closure 证据同步。产品问题已经修好，不能重复报为未修 |
| 矩形标注取消 | HP `src/tools.ts` 的 `create_annotation.execute(toolCallId, params)` 没有接收取消信号，标注写入也未传 signal；polygon 路径已有 | 补矩形路径取消传播及会话 I/O 后、写入前的回归测试；这是代码审查确认的缺口，本轮未做运行时复现 |
| 文件夹浏览体验 | `static/app.js:3538` 明确页码仅在内存，刷新不保留；`importTargetState` 与 `fbState.folder` 分离 | 可补当前目录/页码恢复、从当前文件夹上传时默认选中该目录。现有上传已经支持显式选目标，不能写成“完全不能上传到文件夹” |
| 浏览器 JPEG2000 转换 | [F2 可行性](slide-tools/f2-jpeg2000-feasibility.md) 之后没有集成；`slide-transform-core/crates/core/src/svs.rs` 仍拒绝 33003/33005，说明需独立解码器 | 若继续格式扩展，单独实施 F2。部分变体可服务器直传查看，不等于网站完全不能读 JPEG2000 |

## 二、设计储备，不属于漏交付

| 项目 | 判定 |
|---|---|
| fast COS 转发 | **已移出设计储备：两端实现完成，待真实环境验收与发布，见页首补充。** [最新设计](cos-fast-goapi-relay-design-20261013.md) 最终方案为 COS → fast → HistoPilot 流式转发，fast 不落盘、不建立缓存；采用用户确认的两段流量不计费前提。前一版 100G 缓存方案已被取代 |
| 插件能力层 P2 | [设计](plugin-capability-layer-design.md) 的后续写能力等仍是规划；当前 `plugins/sdk/manifest.py` 明确只放行 read。P1 已有实现，不应把整篇文档当成未实施 |
| 赛维尔格式 | [格式交接](slide-tools/tool-ux-formats-release-handoff-20261003.md) 记录等待样本，未发现后续适配交付；需真实样本才能推进 |
| IPv6 直传 | 根 worktree 未提交的 `docs/ipv6-direct-upload-feasibility-20261001.md` 是备选方案。建议与 fast 转发设计一起标明优先级，不同时启动两套中转改造 |
| PathSegmentor / Cellpose 测量 | 根 worktree 未提交 `docs/measurement-models-homepc-implementation.md`，日期 9 月 28 日，属时间窗外补充发现；仍是实施前设计。已上线的 SAM2 轮廓细化不能算作它的实现 |

## 三、证据尚未闭环，不能直接断言线上有故障

- **中国 IP 访问 .com 自动转 .cn**：[域名状态](slide-tools/domain-routing-status-20261008.md) 明确写尚未上线，未找到后续地域分流验收。两域名能访问、语言不同不证明地域跳转已完成。需要在当前真实入口做 CN/非 CN 与可信代理头验收。
- **历史迁移数据处置**：[旧切片迁移记录](review-evidence/slide-tools/R1/legacy-migration-remediation-20261002.md) 保留无主样本隔离、缺失文件不恢复、旧源保留等决定。未找到逐项处置闭环；本轮未查生产数据，不能把 10 月 2 日数量当作今日数量，更不能直接清理文件或代替用户认领样本。
- **真实设备/系统对话框验收**：Mac 实机帧率、真实系统保存选择器等缺完整闭环。已有无头浏览器或 Linux 限内存验证不等价。Windows 已被用户明确豁免，不应重新作为发布阻断项，见 [发布计划](slide-tools/ux-formats-release-plan-20261004.md)。
- **转换完成量统计**：最新[发布记录](site-branding-stats-release-20261013.md) 明确只有工具页访问统计，本地转换完成次数未上报。这是测量范围限制，不是访问统计修复未上线，也不能由页面访问数推断转换人数。

## 四、分支盘点

PT 33 个本地分支，其中 29 个已是 `origin/main` 祖先；HP 10 个，其中 9 个已是主线祖先。未命中祖先关系的分支逐项核对如下：

| 仓库/分支 | 判定 |
|---|---|
| PT `codex/navigation-refine-review` | 前两笔复核已等价合入；最后 `fd0f7832` 关闭记录遗漏，见第一节 |
| PT `codex/public-site-zarr3` | 经 PR #11 squash 合入，已完成；不能因 `--merged` 查不到就认为未合并 |
| PT `release/viewer-skills-20261011` | 当前工作分支；比远端 release 多 `b723ab31`、`a80d1966`、`dc5f5b55`。与 main 的历史差异含 squash，不能全算未发布代码 |
| PT `slide-id-refactor` | 旧分叉且根工作树有未提交修改；后续主线已有大量替代实现。不能整支合并或直接删除，需先保全修改再按文件判定 |
| HP `hotfix/hp-0.3.6-slide-routes` | stable-ID 路由补丁已等价合入；DeepSeek schema 修复及回归已在现主线，0.3.6 版本号已被 0.4.2 取代。不是待上线 hotfix |

`admin-viewer`、`viewer-skills`、`vi-*`、`av-*`、`upload-convert-first`、`ux-*`、格式适配及 CI 修复等分支已合入。可以视为归档候选，但删除前仍需单独保护 worktree 内未提交文件，本轮没有删除任何分支。

本地 PT `main@8955b7ec` 落后 `origin/main` 478 个提交，只是旧本地指针；根 HP 也停留在旧 `slide-id-refactor`。后续工作应使用已同步的 release 或从远端主线新建工作树，避免从旧根目录继续开发。

## 五、未提交文件与文档治理

PT 根工作树有 **17 个 tracked 文件修改**，另有未跟踪文档、设计图、部署文件。包含旧注册/品牌修改及后台插件版本修改，不能假设都无用；也不能把老版本清单直接覆盖现有版本。

其中四份根目录未提交文档与 release 内容相同：`admin-viewer-registration-acceptance-20261008.md`、`admin-viewer-registration-design-20261008.md`、`opus-admin-viewer-handoff-20261008.md`、`registration-antibot-and-author-help-design-20261008.md`。其他根目录方案和原型需要归档归属。部分旧 worktree 的“脏”状态来自虚拟环境、依赖链接或测试产物，不代表业务改动未提交。

建议先保存根工作树补丁与未跟踪文件清单，再做逐项比对；不要自动 stash、reset、批量清理 worktree。历史报告保留当时事实，在页首添加“当前状态见……”链接即可，不要把旧验收改写成当时已经通过。

## 六、已完成但容易被旧文档误导的项目

| 历史措辞 | 后续闭环 |
|---|---|
| admin/Viewer 未部署、需要 0081/插件 0.4.17 | 已由后续发布完成，见 [suite 发布](suite-release-2026-10-11.md) |
| GPU 镜像未构建、SAM2 未实测 | 同上，有 CUDA/真实推理/忙拒绝/超时回退验收；本轮只读确认 contour-refine 容器运行且 PT 已配置服务地址 |
| AI 读图不存在、刷新会话失败 | [Dogfood](dogfood-user-read-20261011.md) 与 [0.4.2 发布](session-restore-collaboration-release-20261011.md) 已记录修复 |
| Zarr3、注册测试稳定化、Dependabot 尚待合并 | [交付记录](public-site-zarr3-delivery-20261013.md)；#11/#12 已合并，#1/#6 已关闭，两仓无开放 PR |
| 6-worker 尚为本地改动、HP 主线契约漂移 | HP `docs/build-test-memory-review-20261012.md` 后续记录、`docs/cross-repo-ci-drift-20261013.md` 及 master/release 同为 `c1db40e` |
| Logo、统计、转换保存修复未上线 | [10 月 13 日发布](site-branding-stats-release-20261013.md) 已完成；本轮只读检查生产 PT 的 APP_REVISION 为 `c8697a495112fedbba0041d05cb3062ad30c51b2`，与记录一致 |

建议收尾顺序：补复核关闭文档 → 发布已完成的主页修复 → 补矩形取消 → 整理根工作树与旧文档状态。fast 转发进入真实环境验收和发布准备，不重复启动实现；JPEG2000、测量插件和地域跳转按需求优先级排期。
