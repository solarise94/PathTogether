# ux-formats 发布与回滚计划（待批准）

日期：2026-10-04。候选为分支 `ux-formats` 的 `d861dd1`。

> **未部署、未切流、未推送。整包在 compact 盲评与 MRXS v2 低倍层两项视觉验收完成前不发布。本文是待批准计划，不是发布记录。**

关联：[发布交接](tool-ux-formats-release-handoff-20261003.md)、[独立复核](ux-formats-independent-recheck-20261004.md)、[真实 COS 验收](ux-formats-real-cos-acceptance-20261004.md)、[rc6 发布记录](../review-evidence/slide-tools/R1/rc6-production-20261003.md)。

## 1. 候选

| 项 | 值 |
| --- | --- |
| Git | 本地 annotated tag `ux-formats-rc1`（未推送）→ `d861dd19c41c1f3899263bd1ebf4bd583d2c65be` |
| 镜像 | `localhost/pathtogether-demo:suite-20261004` |
| 镜像 ID | `c6a44cb6b758e10b311b6485f1866820cb8c6ac206f9cbe9e241a592c058e811` |
| manifest 摘要 | `sha256:634bdead8fc8413e0262417de9763031b00ad86f5e6d7b637aa67fc901989ae2` |
| 标签 | revision = `d861dd19c41c1f3899263bd1ebf4bd583d2c65be`，version = `ux-formats-rc1` |
| 创建时间 | 2026-10-04 11:44 UTC |

构建方式：在 homePC 上用仓库 `Containerfile` 从 `git archive ux-formats-rc1` 导出的源码构建（`podman build --network host`，pip 镜像配置以只读方式挂载）。

## 2. 当前生产

| 项 | 值 |
| --- | --- |
| 容器 | `pathtogether-demo` |
| 镜像 | `localhost/pathtogether-demo:suite-20261003`（r1-rc6，revision `8127ecb`） |
| 镜像 ID | `56e705cae15e…` |
| manifest 摘要 | `sha256:2d2b5e112ee9965d61fdbd197b677879437a2eb7a3fc9b9cf7dddb6fe3899bf5` |
| 运行起点 | 2026-10-03 10:42 CST |

更早的 rc5 容器仍保留为 `pathtogether-demo-pre-suite-20261003`。HistoPilot 容器与各插件发布不属于本次范围。

## 3. 与生产镜像的差异

方法：对两个镜像 /app 下 templates、static、plugins、migrations、scripts、kfb、legal_docs 的全部文件，以及顶层 `*.py`、`*.sh`，逐一计算 SHA-256 后比对。结果：16 个文件改动、2 个新增、0 个删除（候选侧受检文件 333 个，生产侧 331 个）。

- 改动（16）：`static/app.js`、`static/i18n.js`、`static/style.css`、`static/tools/slide-transform/build-manifest.json`、`static/tools/slide-transform/engine.js`、`static/tools/slide-transform/runner.js`、`static/tools/slide-transform/slide_transform_bg.wasm`、`static/tools/slide-transform/slide_transform_bg.wasm.d.ts`、`static/tools/slide-transform/slide_transform.d.ts`、`static/tools/slide-transform/slide_transform.js`、`static/tools/slide-transform/worker.js`、`static/tools/tools-slides.css`、`static/tools/tools-slides.js`、`static/tools/tools-slides-upload.js`、`static/upload/cos-uploader.js`、`templates/tools_slides.html`。
- 新增（2）：`static/tools/tools-slides-bundle.js`、`scripts/test_mrxs_memory_budget.sh`。

Python、requirements、Containerfile、entrypoint、插件与迁移均无变化。

迁移核对：镜像内迁移文件 77 个；生产 `schema_migrations` 77 行；两边文件名集合完全一致（排序后列表的 SHA-256 相同），本次发布不会应用任何迁移。

## 4. 镜像验收证据（2026-10-04 完成，未写生产）

- §3 范围内的 333 个交付文件与 `git archive ux-formats-rc1` 逐字节一致。
- 转换器构建清单：WASM、生成 JS、runner、engine、worker 的 SHA-256 全部与清单一致；native CLI 在清单中列出但未随镜像交付，其哈希已在独立复核中核对一致。
- 隔离启动：一次性 Postgres 16 与候选镜像组成仅绑定 127.0.0.1 的 podman pod；数据目录与 bootstrap 账号均为一次性；无 COS 凭据；7 个后台 worker 全部置 0（entrypoint 日志逐项确认跳过）。正常 entrypoint 只接触一次性数据库：77 个迁移全部应用；`/healthz` ok（隔离环境 sidecar 不可达属预期）；`/tools/slides` 200；`/app` 重定向到登录；日志无 error/traceback。事后 pod 与数据已删除。
- 工具页 CSP 与公网 rc6 响应头一致，仅缺 COS origin——隔离 pod 没有桶配置所致；app.py 中 CSP 代码未变。
- 90 个静态文件经 HTTP 提供且逐字节一致；WASM 以 `application/wasm` 提供。
- 浏览器 smoke（Chromium 经 SSH 隧道连隔离镜像，OPFS 保存桩）6/6 通过：合成 KFB 保留画质 `6e8744f9`、compact `7e2f4f82`；CMU-1-Small-Region SVS `fcb6d171`；JPEG 2000 SVS 在复制前被拒绝（0 个 job 目录）；MRXS 完整文件夹 CMU-1-Saved-1_16 `62da50da`——全部与 native CLI 逐字节相同；转换期间 0 次 `/api` 请求、0 次跨源请求；登录后工作台 `/app` 以随镜像 app.js 正常启动，无页面错误。
- 真实 COS/XHR：直接沿用[真实 COS 验收](ux-formats-real-cos-acceptance-20261004.md)。理由：该轮运行的是 d861dd1 源码；本镜像文件与 d861dd1 逐字节一致；服务端上传、签名、摄取与 CSP 代码相对 rc6 未变。保留其范围声明：rc6 运行时 + 只读候选源（127.0.0.1:18094）、生产摄取 worker 未变更、仅合成文件；它不是镜像打包证明，本节才是打包证明。因此无需重跑 COS 验收。

## 5. 配置对比

staged 容器按运行中 PT 原样克隆 env、挂载、ulimits、network、restart 与 command（helper 校验形状一致，且 env 差异为空）。验收容器（端口 18090，restart=no）只有两处不同：PORT，以及把七个后台 worker 开关全部强制为 0——`SAMPLE_TMA_BACKEND`、`REGISTRATION_MAIL_WORKER`、`FORMAT_REQUEST_WORKER`、`RESEARCH_DELETION_WORKER`、`CONVERSION_WORKER`、`COS_INGEST_WORKER`、`BAIDU_IMPORT_WORKER`——因此它不会运行第二套摄取/对账/邮件循环。

预算在容器内断言：容量 10,000,000,000 B，安全余量 500,000,000 B，单次上传上限 9,500,000,000 B；生产已于 2026-10-04 按这些值核实。凭据、CORS、桶生命周期、百度/后台转换开关与各插件发布均不变。

注意：验收容器的 entrypoint 会对生产数据库运行 `ensure_schema`（幂等，无待应用迁移）——这属于部署流程自身的预期行为（按本计划批准后执行），不是打包测试。

## 6. 发布步骤（仅在用户批准后执行，不限晚间窗口）

helper 位于 homePC 的 `~/releases/suite-20261004/deploy.py`（操作员目录，不在仓库）。以下命令按顺序在 homePC 该目录执行：

```bash
python3 deploy.py verify-baseline   # 只读；2026-10-04 已通过（同日只读 quiesce-check 各计数均为 0）
python3 deploy.py prepare
python3 deploy.py accept-check      # 在 18090 启动验收容器：健康、worker 跳过、预算、CSP 与生产一致、90 个静态文件；随后停止
python3 deploy.py backup            # pg_dump -Fc + pg_restore -l 校验；不做恢复
python3 deploy.py quiesce-check     # 任何进行中的上传/摄取/删除/转换/百度/producer/staging 工作非零则 exit 2；等待后重试
podman stop -t 30 pathtogether-demo
python3 deploy.py quiesce-check     # 停机后复查仍须为 0
python3 deploy.py cutover-pt        # 旧容器改名 pathtogether-demo-pre-suite-20261004；staged 改名 pathtogether-demo 并启动；等待 /healthz ok 且 18080 sidecar 可达
python3 deploy.py post-check        # pt.solarise94.fun 与 histopilot.cn 公网健康（sidecar 可达）；两个公网主机的 90 个静态文件逐字节相等；开启 TLS 校验
```

之后是可选的公网浏览器检查：工具页加载并本地转换一个小型合成文件；如用户同意，再做一次真实小文件上传并清理（上传路径已被此前的真实 COS 验收覆盖）。

## 7. 回滚

首选 `python3 deploy.py rollback`：先确认运行中的 PT 是候选镜像，否则拒绝；停止它并改名 `pathtogether-demo-failed-suite-20261004`；把 `pathtogether-demo-pre-suite-20261004` 改回 `pathtogether-demo` 并启动；健康检查（含 sidecar）；最后断言运行的是 rc6 镜像。手工等价的 podman 命令为 stop、rename、rename、start，再 curl `/healthz`。

无数据库回滚需求（无迁移）；备份仅用于灾难场景。回滚限制（如实记录，未测试）：

- 浏览器任务保存在本地（OPFS）。回滚后 rc6 页面不认识 SVS/MRXS/compact 任务与 MRXS 适配器 v2——用户不得在 rc6 下继续此类任务；回滚时须保留 OPFS 数据。
- 已上传的结果由未变更的服务端代码提供，回滚后仍可读取。
- 未演练「新 → rc6 → 新」的浏览器任务完整往返。
- 切流前已打开的旧标签页在重载前仍运行旧 JS。

## 8. 发布前未决事项（阻断项，由用户决定）

- compact 画质盲评：待 owner。
- MRXS v2 低倍层接受：待 owner。
- Windows、真实低内存设备、真实系统保存对话框仍未验证；是否阻断由 owner 决定。
- 升级续跑未测：rc6 页面创建、在切流时尚未完成的浏览器任务，由新页面续跑的行为本轮没有实测（rc6 发布时做过 rc5 → rc6 的同类检查）。建议在批准发布前补一次：同源先用 rc6 中断一个 KFB 任务，再换候选镜像续跑，并与 native 对比字节。
- AI 会话列表 403 是独立的既有问题，不属于本次发布。

部分发布仅在用户日后选择时才考虑：需要覆盖所有入口（工具页文件选择、文件夹选择、拖放、工作台交接、续跑）的真实能力开关；既有本地结果必须保持可读、可导出；不得删除任务。现在不做决定，当前没有任何功能被移除或禁用。

## 9. 私有证据

原始证据（哈希清单、smoke JSON、deploy helper 副本）保存在 worktree 内被忽略的 `.gate-tmp/release-20261004/` 目录及 homePC。视觉评审包是仓库外的私有本地 HTML 包（含临床 KFB-1 裁片），不提交。
