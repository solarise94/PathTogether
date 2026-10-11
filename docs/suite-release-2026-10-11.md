# Suite release — 2026-10-11

已推送并部署：PathTogether Viewer/skills、HistoPilot 0.4.1、0082 迁移及 SAM2 GPU 细化。
用户已明确授权推送、真实测试及部署。生产部署使用单独的
`release/viewer-skills-20261011` 分支；不改动原工作目录中的未提交内容。

## 构建版本

| 组件 | 源码 | 镜像或插件 |
| --- | --- | --- |
| PathTogether | `89f517f504d5b47521dae3df65c510929b3fbb1c` | `localhost/pathtogether-demo:suite-20261011-viewer-skills`，image `c345307c923d` |
| HistoPilot 服务与浏览器插件 | `66b905b78be423ae49976fa01524a0ae41fcab0e` | 0.4.1，`localhost/histopilot-demo:hp-20261011-session-id`，image `1bc27d8ea992` |
| SAM2 细化服务 | `4bb9aa90` | `localhost/contour-refine:20261011-4bb9aa90`，image `b081c96ce2e1` |
| Admin 插件 | 本次不变 | 0.4.17 |

平台镜像的 `APP_REVISION` 指向实际包含的运行时代码 `89f517f5`。
随后提交的 `4bb9aa90` 只改独立细化服务的构建及文档，`15735016` 只修测试临时路径；均未重新标记平台镜像。
发布记录的提交同样不改变已部署镜像的源码身份。

## 实测发现并修复的问题

### 切片版本校验不一致

先单独上线 HP 0.3.6（`f3616ce`），解决旧插件未使用 by-ID 读图路由的问题。
随后对生产 ID-only 切片发起真实 AI 观察，模型虽然结束了任务，却没有成功
获取快照：插件 info 接口提供内容 SHA revision，regions/refine 接口却与
文件 mtime/size 指纹比较，导致 409 `slide_revision_conflict`。

`89f517f5` 将两个插件读图入口统一为 info 所公布的内容版本；保留旧 internal
接口使用 mtime/size 的兼容语义。新增四项回归直接取真实 info 结果回传给
regions/refine，覆盖当前版本成功和过期版本拒绝。修复前 2 失败、2 通过；
修复后相关 70 项通过，旧 internal 兼容路径另 3 项通过。

首次 0.3.6 AI 实测**不能算作读图成功**。本次验收要求出现真实
`snapshot_captured`，不能仅凭 `agent_finished` 判断成功。

### GPU 镜像无法安装 SAM2

原 Containerfile 首次真实构建失败：CUDA runtime 镜像没有 Git，而 requirements
通过 `git+https` 安装 SAM2。`4bb9aa90` 改为固定源码归档
`2b90b9f5ceec907a1c18123530e92e794ad901a4`，使用正确的分发名 `SAM-2`，
并关闭 pip 构建隔离，复用基础镜像已有的 CUDA PyTorch。

最终镜像：PyTorch 2.6.0+cu124、CUDA 12.4、OpenCV 5.0.0、Gunicorn 23.0.0；
基础镜像 image `40472de84a38`。权重 SHA-256：
`a2345aede8715ab1d5d31b4a509fb160c5a4af1970f199d9054ccfb746c004c5`。
权重以只读方式挂载，不放入镜像或 Git。

### 会话详情漏掉稳定切片 ID

清理线上 dogfood 会话时发现 `/api/ai/session/<id>` 和归档都返回 403。
HistoPilot 已将 `slide_id` 持久化在会话文件里，却没有在真实 HTTP 详情响应中
返回；PathTogether 的会话权限复核无法解析没有旧文件名别名的新切片。
会话仍能首次运行，因此单测只模拟带 ID 的 sidecar 响应、或只测试首次运行会漏掉此问题。

HistoPilot `66b905b` 在详情响应中增加已存储的 `slide_id`；旧会话缺该字段时
保持不返回，不按文件名猜测身份、不放宽平台授权。新增真实 SidecarServer HTTP
回归，修复前 ID 用例失败、legacy 用例通过；修复后全套 1545 项及构建通过。
由于 0.4.0 已上线，另发 0.4.1 包并保留 0.4.0 回滚目录。
最终线上 4 个本次测试会话（包括 0.3.6 创建的会话）均能正确读取 ID、重开和归档。

## GPU 验收

- 设备：RTX 2080 Ti，驱动 580.159.03；实际 CDI 容器透传验证通过。
- 正式服务重启后 `/healthz` 返回 `ok=true`、`device=cuda`，模型在 worker 加载。
- 真实组织 crop 请求：首次 0.3994 秒、随后 0.1182 秒，接受 24 点轮廓，score 0.8207。
- 8 个并发请求：1 个 200、7 个 503 busy，最长 0.1089 秒；未授权请求 401。
  这不是广泛性能基准，只是当前设备、此输入的部署冒烟结果。
- 在最终镜像的临时测试容器中：20 passed、1 skipped（5.25 秒）。跳过项依赖
  本地绝对路径的研究样本；相同组织 crop 已在上面的真实 GPU HTTP 请求中验收。
  第一次测试因构建下载代理残留，使本地 Gunicorn 健康探测走错代理而失败；
  清除测试进程的代理后原断言全部通过，未改测试或服务行为。
- 正式服务仅监听 `127.0.0.1:8970`，配置访问令牌；`contour-refine.service`
  已启用开机启动，并实际重启后重新验收。生产平台通过本机地址调用。

## 部署与回滚准备

- 保留旧 PT/HP 容器、插件版本目录和数据备份；切换时先停止旧写入进程，
  再做最终 PostgreSQL/session 备份，统一切换平台与 0.4.0 插件。
- 新平台镜像 348 个运行时文件与归档源码一致，97 个静态资源有预期哈希。
- 新镜像已在生产 schema 副本完成 0082 迁移及重复运行；验收容器、数据库
  与生产写入隔离，测试后移除。当前生产基线为 0081，Admin 0.4.17 已存在。
- 切换脚本检查生产基线、候选镜像、配置指纹、队列空闲状态、GPU 实测结果
  及全量测试完成标记；异常时恢复旧 PT/HP 与插件链接。
- 远端操作证据目录：`~/releases/suite-20261011-viewer-skills/`。
  其中配置、备份、会话、图像与完整模型输出属于私有运维证据，不提交仓库。

## 最终上线验收

- 平台与 HP 0.4.0 同步切换：2026-10-11 04:01:09 UTC（北京时间 12:01:09）。
  会话缺 ID 修复后，HP 服务/插件更新为 0.4.1：04:10:05 UTC（北京时间 12:10:05）；
  平台随后重启刷新插件缓存，GPU 服务不变。
- 生产迁移台账最新为 `0082_run_grant_drawing_skill.sql`；已有 0080、0081 保留。
- `histopilot.cn`、`histopilot.com` 均通过 health、share health、CSP 检查；
  每个域名 97 个静态资源哈希与发布归档相符。
- 真实模型观察 ID-only 切片：9.83 秒，4 次成功快照，任务完成，无读图 409。
- 两次自然语言请求描绘：46.00 秒、55.64 秒，各写入 1 个 polygon。
  实际数据库记录为 `source=ai`、`skill_intent=model`、`review_status=pending`。
  两次都真实调用了 GPU SAM2，但得分 0.7306、0.5638 低于 0.75 闸门，
  均记录 `refined=false, reason=low_score` 并保留模型原轮廓。
  **没有把这两次描绘称为细化采用成功**；采用成功由上述独立组织样本 HTTP 验收覆盖。
- 0.4.1 上：4 个测试会话详情 HTTP 200，均返回匹配的 `slide_id`；
  对已结束会话续聊约 4.26 秒完成，收到 `session_resumed`。SSE 包含历史重放，
  续聊日志中的旧快照不作为新增看图次数统计。
- ID-only 切片的标记操作：创建、移动/缩放、锁定、锁定后几何/删除拒绝、
  仅改备注、解锁、删除、恢复均通过；只清理本次创建的标记。
- 超时回退：独立进程加载已部署的 PT 代码并真实读取自有切片 crop，
  将**仅该进程**的目标指向延迟 HTTP 服务；实际 8 秒读超时后，8.219 秒返回
  HTTP 200、`refined=false, reason=service_error`。没有修改生产 worker 配置，
  也不把该实验称为强制取消了正在执行的 CUDA 推理。
- 真实插件的桌面浏览器验收（1440×900）：标题与新建/更多/关闭三个按钮
  均可起拖，拖完不会触发按钮；正常点击仍可打开会话菜单。手机独立视口
  390×844 从底栏更多菜单打开 AI，面板在屏幕内，页面没有横向溢出。
  最终浏览器 JS 错误 0、AI 接口 4xx/5xx 0。
  初次脚本在窄屏仍尝试点击隐藏的桌面按钮，另一次在动画未结束时读布局；
  按手机实际入口、等待有限动画结束后通过，未为测试修改产品 UI。
- 清理：手工操作验收创建的 1 个标记已删除；模型创建的 2 个标记已删除；
  本次 4 个测试会话已归档。未删除用户原有标记或会话。

### 本轮测试口径

| 检查 | 实际结果 |
| --- | --- |
| PathTogether 全量 pytest（未忽略文件） | 3036 passed、1 failed、113 skipped，27 分 56 秒 |
| 全量中的唯一失败 | 测试写死仓库 `.gate-tmp/cmp-b.json`，新工作目录不存在该目录；非运行时代码问题 |
| 修复及复测 | `15735016` 改用 `tmp_path`，原断言不变；`test_conversion_drain.py` 全文件 12 passed |
| HistoPilot 0.4.1 全套 npm test | 80 文件、1545 passed；类型检查/构建通过 |
| 最终 GPU 镜像内服务测试 | 20 passed、1 本地样本路径用例 skipped；真实 GPU 样本另行通过 |

全量初次结果与失败项复测分开记录，**没有声称一次 3037 全绿**。
只修了测试夹具路径后未再重跑整套约半小时的 PT 测试；运行时代码未变化。
本轮未重跑此前已经通过的全部 vitest/Playwright 或跨仓契约套件；本轮新增修复
分别用 PT 回归、HP 真实 HTTP 回归及生产端到端操作验证。

HP 0.4.1 的运维证据位于 `~/releases/hp-20261011-session-id/`。
两仓均推送到 `release/viewer-skills-20261011`；原 `viewer-skills`、
`hp-viewer-skills` 与 0.3.6 hotfix 分支也已按授权推送。
后续开发需包含发布分支新增的 `89f517f5`、`4bb9aa90`、`15735016` 和 HP `66b905b`。

脱敏机器验收摘要见 [suite-release-2026-10-11-validation.json](suite-release-2026-10-11-validation.json)。

## 验收边界

- Linux 无头 Chromium 的桌面/手机视口测试不等于 Mac 实机帧率验收。
- SAM2 分割的质量闸门仍是启发式，细化不成功应保留模型原轮廓。
- 矩形标注工具的会话文件读写阶段仍未贯穿取消信号，这是既有已记录限制。
