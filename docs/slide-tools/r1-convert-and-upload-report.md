# R1 一键转换并上传报告（工具页 + 工作台入口）

日期：2026-09-30。对照产品要求：`docs/slide-tools/c6-migration-drain-plan.md` §3.1
（R1 一键转换并上传，2026-09-30 产品补充）。前置：C2 运行器、C3 工具页、C4 上传接入
（`docs/slide-tools/c2-runner-report.md` / `c3-tool-page-report.md` / `c4-upload-report.md`）。
复跑命令：`docs/review-evidence/slide-tools/R1/RERUN.md`；证据 JSON：同目录 `results/`。

> 撰写纪律：下文全部 PASS/数字均来自 2026-09-30 实跑（命令见 RERUN.md）。

## 0. 结论摘要

| 门禁（§3.1 验收） | 结果 | 数值 / 证据 |
|---|---|---|
| 单次点击顺序：转换 → 校验 → 准入 → 上传 → 可查看，零额外确认 | **通过** | R1 e2e a：第一个 `/api/` 请求是能力预检；ingestion 建立被闸住，放行时 OPFS 记录已 `ready`（转换+校验完成）；点击前 0 `/api/`、0 跨源；creates=1 |
| 上传对象 = 转换产物（非源 KFB） | **通过** | R1 e2e a：重组上传分块 sha256 == OPFS 产物 sha256（`f265596a7662…`，287,803 B）；非 COS 请求体无 KFB magic |
| 转换失败 / 转换中取消：零 ingestion | **通过** | b1（严格策略 → `pixel_policy_violation`，creates=0，意图 revoked）；b2（取消，creates=0，任务目录清空） |
| 超限 / 不可查看：停止自动上传、保留产物、可本地保存 | **通过** | c1/c2：creates=0、statusGets=0，原因明示，意图 pending，产物 sha 不变，保存 sha == 产物 |
| 登录过期（点击前）：不开始转换、登录链接 | **通过** | d1：清 cookie 后点击 → 「需要登录」+ `/login?next=/tools/slides`，结果面板未出现，零意图 |
| 登录过期（上传中）+ 刷新恢复 + 不自动传输 | **通过** | d2：重开只显示「继续上传」；再等 2.5s creates/gets 不变（加载不自动传输）；继续 → 真实 401 登录提示 → 重新登录 → **同一 ingestion**（creates=1） |
| 换账号不转移上传归属（二轮修复，§8） | **通过** | d4/d5：两个普通用户、真实 ingestion 归属检查——B 续 A 的任务只能选「用原账号登录」（A 登录后续传同一 ingestion，creates=1）或「另起上传」（B 新建自己的 ingestion；A 的原任务仍归 A、仍 uploading，B 读它 403、A 读 B 的 403，A 可在列表里取消原任务）；d3：管理员同样不能静默续传他人任务 |
| 已发布任务不再上传（二轮修复，§8） | **通过** | k1：发布且意图 done 后，结果面板上传按钮被发布结果取代；直接触发上传动作 3 次：零能力请求、零 ingestion、零 PUT；列表行无上传按钮。i5：关联未完成时重复触发只重试关联 |
| 各阶段刷新恢复 | **通过** | e1（转换中刷新 → 「继续转换并上传」→ 发布，creates=1）；e3（复制中刷新 → 启动清扫，零 ingestion）；上传中刷新在 d2 |
| 重复点击 / 第二标签 / 完成回调重放：恰一个 ingestion | **通过** | f1（三连击 creates=1）、f2（B 标签被 Web Lock 拒，creates=1）、f3（complete 丢失重放 completeReqs=2，creates=1） |
| 转换后、上传前取消：撤销意图、产物保留 | **通过** | g：超限停止态点取消 → 意图 `revoked`、重开后回普通「上传到工作台」（无继续动作）、creates=0、产物 sha 不变 |
| 仅转换并保存：隐私不回退 | **通过** | h（零 `/api/`、零跨源、无意图落盘）+ C3 套件 **18/18**（含 j 网络捕获 / k 离线全流程） |
| 工作台入口 | **通过** | i（KFB → 「在本机转换并上传」→ popup 交接不重选（file-input 空）→ 保目标 → 发布后按 slide_id 关联，creates=1）；i2（原生 .tif 直传、无转换入口）；i3（弹窗拦截 → 明示 + 工具页链接、选择可重试）；i4（「新项目」目标：发布前零建项目，发布后带幂等键创建并关联）；i5（关联失败 → 意图保持 pending、刷新后已发布行「重试加入项目」→ 只关联不重建项目 → done） |
| 磁盘确认仍在一键链前置 | **通过** | j：5.2 GiB 稀疏输入 + 报告上限 stub → uncertain 对话框出现时「转换并上传」仍禁用；拒绝 → 零任务、零 ingestion |
| C4 回归 | **通过** | C4 e2e **15/15**；工作台原生直传请求序列与基线**逐条一致**（14 请求，`workbench-seq-equal: true`） |
| C2 门禁 | **通过** | `test_no_whole_file.js` PASS（新模块 `tools-slides-convert-upload.js` 纳入 grep 面） |
| vitest | **通过** | `npx vitest run tests/js`：**38 文件 595/595**（新增 `tests/js/r1-convert-upload.test.ts` 33 条；既有断言零改动） |
| pytest | **通过** | 三文件合跑 **37 passed**（`test_slide_tools_upload_capability.py` 10 含新增 account 用例；`test_r1_conversion_gate.py`+`test_ingestion_api.py` 27；分跑数字同） |

R1 e2e 全量：**21/21 ALL PASS**（`results/r1-e2e.txt` / `r1-e2e.json`；含审查修复回归 i5，见 §7）。

## 1. 交付物（代码）

| 文件 | 内容 |
|---|---|
| `static/tools/tools-slides-convert-upload.js`（新增） | 一键控制器：预检（登录/能力，点击后首个网络动作）→ 意图落盘 → 注入页面的转换驱动 → 自动上传（无第二次确认）→ 发布收口（先关联工作台目标，成功后意图 done 并通知打开者；关联失败意图保持 pending + `assocError`）→ 工作台 handoff 接收（同源 postMessage，File structured clone，幂等 ack） |
| `static/tools/tools-slides.js` | `onConvert` 拆出共用 `driveConversion()`；新增 `#convert-upload-btn` 入口 `onConvertUpload`；任务行 start/resume 按意图换文案「继续转换并上传」并在完成后自动上传；`takeHandoffFile`（交接文件走与手选完全相同的探测/磁盘确认流程）；`#upload-cancel-btn` 可见性扩展（待执行意图的停止态）；`renderFlowMsg`（页级流程文案，登录链接 DOM 构造） |
| `static/tools/tools-slides-upload.js` | `fetchCapability` 导出复用；`createUploadController` 新增 `onPublished(jobId, slideId)` 回调与返回值 `{ok, slideId}`；runLocked 内意图账号核对（不同账号 `window.confirm` 重新确认，拒绝即停、接受才重绑）；`renderRowSegment` 意图感知（pending 意图 + 无未收口上传记录 → 「继续上传」）；`cancel()` 扩展（无在传句柄且当前任务挂 pending 意图 → 撤销意图，产物保留）；已发布行在意图仍 pending 且有目标时给「重试加入项目」（`data-action=assoc-retry`，点击才发请求） |
| `static/tools/slide-transform/runner.js` | 公共 API 新增 `setJobIntent(jobId, patch)`（与 `setJobUpload` 同一双槽串行纪律，prepared/planned/paused/ready/exported/failed 可写）；`_summary` 增 `intent` 字段；头注释 API 面同步 |
| `templates/tools_slides.html` | 第 7 节双入口（「转换并上传」primary / 「仅转换并保存」secondary）+ 一键说明（本机处理、只上传转换后文件、点击即网络授权）；隐私说明同步；`i18n.js`/`tools-slides.js` 版本参数升 `v=20260930` |
| `static/i18n.js` | 新增 zh/en 键：`tools.run.upload{,.hint}`、`tools.jobs.action.intent.{start,resume}`、`tools.upload.account.changed{,.confirm}`、`tools.upload.intent.revoked`、`tools.cu.*`（链路/预检/关联 9 条）、`tools.handoff.*`（接收横幅/目标名 4 条）、`upload.kfb.*`（工作台入口 6 条）；`tools.run.start` 改义「仅转换并保存」（模板内联同步）；`tools.privacy.note` 更新 |
| `templates/index.html` | `app.js` 版本参数升 `v=20260930`（缓存再验证之外的双保险；无标签增删） |
| `static/app.js` | 工作台入口：`uploadFile` 对 browser_convert 词表内、直传词表外的文件不判失败，行内给「在本机转换并上传」（`offerBrowserConvert`）；`openConvertHandoff`（popup + postMessage 交接，ack 前每 400ms 重投、幂等；弹窗拦截 → 明示 + 工具页链接，选择不静默丢失）；`convertHandoffTarget`（显式 pid / 新项目（名称+一次性幂等键）/ null=未归类）；`convertHandoffMessage`（ack + 发布通知 → 刷新列表/项目）；`_EXTRA_I18N` 兜底同步；`HP_UPLOAD` 导出面扩展 |
| `app.py` | `GET /api/tools/slides/upload-capability` 响应新增 `account`（当前登录 user_id；内网免登录态为空串）——客户端据此把意图绑定到授权用户。**唯一**服务端改动；转换闸语义未动 |
| `tests/browser/slide_tools_r1/{lib.js,run_e2e.js}`（新增） | R1 e2e：C4 lib 的字节保留版假后端（PUT 体按分块缓冲，可重组比对 sha）+ gateCreate 闸（顺序证明）+ context 级路由（popup 场景）；21 场景对齐 §3.1 验收（含审查回归 i5） |
| `tests/js/r1-convert-upload.test.ts`（新增） | 真实 app.js 驱动（loadApp harness）：KFB 入口/零请求、原生直传不受扰、弹窗拦截回退、popup 交接消息形状（File+目标+同源 targetOrigin）、i18n zh/en 键契约（25 键 ×2）与模板内联一致 |
| `tests/test_slide_tools_upload_capability.py` | 新增 `test_capability_endpoint_exposes_account`（owner → user 换账号回显） |
| `tests/browser/slide_tools_c2/test_no_whole_file.js` | grep 面加入新模块 |

## 2. 设计

### 2.1 单次授权与执行顺序

```
选择文件（复制+探测，磁盘确认在此阶段） → 设置档位/策略
   │
   ├─「仅转换并保存」(#convert-btn)：C3 原路径，零网络、匿名（不变）
   │
   └─「转换并上传」(#convert-upload-btn)  ← 唯一授权点击
        ① 能力预检（本链第一个网络请求）：GET /api/tools/slides/upload-capability
           401 → 登录链接，链不启动；不可用 → 原因，链不启动
        ② 意图落盘：record.intent = {pending, account, target}
        ③ 本机转换 + 完整校验（C2 运行器，WASM+OPFS，无网络）
        ④ 最终准入（上传控制器内，重新拉取能力）：output_bytes vs max_size_bytes；
           result.format vs viewable_formats —— 不过 → 停止自动上传、产物保留
        ⑤ COS 分块上传（C4 共享引擎：create→sign→PUT…→complete→轮询）
        ⑥ viewable → 工作台目标关联（服务端权限检查）→ 成功才意图 done；通知打开者
           关联失败 → 意图保持 pending（记 assocError），已发布行可重试
```

- ① 在 ③ 之前（§3.1「优先在转换前完成登录与能力预检」）；④ 对**最终文件**重新准入
  （§3.1「最终文件仍须重新准入」）——顺序证明由 e2e a 的 gateCreate 闸给出：ingestion
  建立被闸住，放行时读取 OPFS 记录已为 `ready`。
- ③→⑤ 之间**没有**任何确认弹窗；⑤ 只可能发送 ready 产物的分块（C4 语义：source 是
  OPFS 产物视图，只 slice）。
- 磁盘确认在授权点击**之前**（复制/探测阶段）发生——与 §3.1「文件、策略、必要的磁盘
  确认完成后，一次点击」一致；e2e j 验证一键按钮在磁盘确认未过时不可用。

### 2.2 意图状态模型（持久化于 OPFS 任务记录 `record.intent`）

| 字段 | 类型 / 取值 | 说明 |
|---|---|---|
| `state` | `'pending' \| 'revoked' \| 'done'` | pending=已授权待执行；revoked=撤销（转换失败/用户取消）；done=已发布 |
| `account` | string | 授权账号 user_id（能力端点 `account` 字段）；换账号继续须确认后重绑 |
| `target` | `null \| {project: pid} \| {newProject: {name, key}}` | null=未归类（工具页直用）；project=工作台显式项目；newProject=「新项目」目标（名称+一次性幂等键，**仅发布成功后创建**——转换失败不留空项目） |
| `channel` | `'tool' \| 'workbench'` | 入口（工作台交接携带目标） |
| `assocError` | string \| null | 目标关联失败原因（意图保持 pending）；成功后清空 |
| `projectId` | string \| null | done 时实际关联的项目 pid（「新项目」创建后 pid 还会先写回 `target.project`，重试不重建） |
| `createdAt / reconfirmedAt / revokedAt(+revokedReason) / doneAt / updatedAt` | ISO | 溯源 |

状态迁移：`（无）→ pending`（预检通过落盘）；`pending → revoked`（转换失败/取消、
停止态用户取消）；`pending → done`（发布且目标关联成功后，`onPublished` 内幂等；关联失败留在 pending，已发布行重试）；`pending →
pending`（账号重绑，`account`/`reconfirmedAt` 更新）。写经 `runner.setJobIntent`
（双槽记录串行链，与 `setJobUpload` 同纪律）。上传执行态仍在 `record.upload`（C4
语义不变）；「已发布意图视为完成」＝ intent.done，重复回调/多标签不会二建（C4 的
Web Lock + 续传语义承接，e2e f1/f2/f3 复证）。

### 2.3 上传归属与换账号（二轮修复后，见 §8）

能力端点回 `account`（当前 user_id）与 `account_label`（登录名）。意图与每条上传
记录（`record.upload.account/accountLabel`）都绑定创建它的账号——服务端 ingestion
的归属与容量记账都在那个账号上。任何「继续上传」都重新拉能力并比对：

- 同账号 → 续传同一 ingestion；
- 不同账号 → `<dialog id="account-dialog">`：「退出并用原账号登录」（POST /logout
  后到登录页，记录不动）/「用当前账号另起上传」（旧上传原样记入
  `record.upload.superseded`，为当前账号新建 ingestion）/ 取消（零请求）。
  **绝不**用新账号续传旧 ingestion、也不改旧上传的归属；
- 缺归属字段的旧记录：续传前 GET 该 ingestion，403 即按不同账号处理。

被取代的旧上传在任务列表显示归属登录名与「取消旧上传」——只有原账号能取消（服务端
归属检查），否则服务端按 `job_deadline_at` 到期取消并清理（ingestion_store 超期
扫描）。内网免登录态 account 为空串——绑定语义退化为「同一（唯一）账号」。

### 2.4 工作台 → 工具页交接（handoff）方案

**选定：同源 popup（`window.open('/tools/slides')`）+ `postMessage` 交接 File。**

| 候选 | 评估 |
|---|---|
| **popup + postMessage（选定）** | 工具页 CSP 已按需配置（`worker-src 'self'`、`script-src 'self' 'wasm-unsafe-eval'`、`connect-src 'self' + 唯一 COS origin`），转换/上传 UI 与隐私承诺整套复用；File 经 structured clone 跨同源窗口传递**不复制字节**（唯一副本仍是运行器 staging 的 OPFS `source.bin`）；`window.open`/`postMessage` 不受 CSP 约束，零 CSP 改动 |
| 工作台页内跑 runner（import runner.js） | `/app` 页当前无 CSP，但要跑 WASM worker 就得给它加 `worker-src` + `wasm-unsafe-eval`——对全体工作台用户放宽脚本面；且意图/上传 UI 需在 app.js 重建一份，双份维护。弃 |
| iframe 嵌工具页 | `/tools/slides` CSP `frame-ancestors 'none'`（防点击劫持），放行即削弱；且 popup 已满足需求。弃 |

协议（双向、同源校验、幂等）：

```
工作台（点击「在本机转换并上传」，真实用户激活）:
  window.open('/tools/slides', 'pt-convert-upload')
  每 400ms postMessage {type:'pt:convert-upload-handoff', file, target}（ack 前重投，
    popup 加载竞态由重投+幂等吸收；popup 关闭即停，≤60s）
工具页:
  收到 → 幂等 ack {type:'pt:convert-upload-ack'} → takeHandoffFile（走与手选
    相同的探测/磁盘确认/设置流程；file-input 保持空——用户不需要重选）
  发布后 → {type:'pt:convert-upload-published', jobId, slideId, projectId}
工作台收到发布通知 → 刷新列表/项目（目标关联已由工具页按服务端权限完成）
```

弹窗被拦截（`window.open` 返回 null）：行内明示「弹窗被浏览器拦截……」+ 工具页
链接（手动入口）；「在本机转换并上传」按钮保留，允许弹窗后原地重试——选择不静默
丢失（e2e i3）。目标为「新项目」时，名称与一次性幂等键随目标持久化，项目仅在
**发布成功后**创建（e2e i4：发布前 createdProjects==0）。

### 2.5 失败与恢复语义

| 阶段 × 事件 | 行为 | 场景 |
|---|---|---|
| 预检 401 / 离线 / 能力关 | 链不启动（不转换、不落意图）；登录链接/原因 | d1 |
| 转换失败 | 任务 failed、意图 revoked、零 ingestion | b1 |
| 转换中取消 | C2 取消语义（任务目录清理）、零 ingestion | b2 |
| 超限 / 不可查看 | 自动上传停止于创建 ingestion 之前；产物保留、本地保存可用；意图 pending（可继续/可撤销） | c1/c2/g |
| 上传中登录过期 / 断网 / 刷新 / worker 崩溃 | 任务与意图保留；重开只显示「继续上传」，加载不自动传输；继续时同一 ingestion（C4 续传语义） | d2 |
| 换账号继续 | 确认拒绝=零上传；接受=重绑后同一 ingestion | d3 |
| 停止态用户取消 | 撤销 pending 意图；重开回普通「上传到工作台」 | g |
| 重复点击 / 多标签 / 回调重放 | busy 守卫 + Web Lock + 引擎幂等 → 恰一个 ingestion | f1/f2/f3 |
| 转换中刷新 | 意图随记录保留；「继续转换并上传」→ 完成后自动上传 | e1 |
| 复制中刷新 | 授权点击尚未发生（复制在探测阶段）：启动清扫 staging，零 ingestion | e3 |

### 2.6 隐私

- 「仅转换并保存」路径零改动：匿名、加载后离线、零文件外传（C3 套件 18/18 复跑）。
- 「转换并上传」的点击是网络操作授权：点击前零 `/api/`（e2e a/h）；只上传转换后的
  ready 产物（sha 比对）；原始 KFB/KFBF 字节绝不出网（magic 扫描 + PUT 总量 == 产物）。
- 按钮旁说明「本机处理、只上传转换后的文件、点击即授权」（`tools.run.upload.hint`）；
  隐私列表同步（`tools.privacy.note`）。
- 大文件磁盘确认保留（e2e j）。

## 3. 服务端改动（最小化）

唯一改动：`GET /api/tools/slides/upload-capability` 响应新增 `account` 字段
（`app.py::api_tools_slides_upload_capability`）。理由：客户端须把本地意图绑定到
授权用户（§3.1「本地任务持久化上传意图、授权用户与目标」；「重新登录为另一账号……
须重新确认」），无此字段则无法判定换账号。转换闸（`SERVER_CONVERSION_CREATION`、
`conversion_moved_to_browser`、`browser_convert` 下发）语义**未动**——
`tests/test_r1_conversion_gate.py` 全绿复跑。ingestion 创建合同未动（工具页上传本就
进未归类；工作台显式项目目标沿用既有 `POST /api/project/<pid>/slides`（服务端权限
检查），不需要也不会改 ingestion 创建面）。

## 4. 验收条目 → 场景 → 结果（全部实跑）

| §3.1 验收 | 场景（tests/browser/slide_tools_r1/run_e2e.js） | 结果 |
|---|---|---|
| 单击顺序 + 零额外提示 + 无 pre-click /api/ + 无源字节外传 + 上传即产物 | `a-oneclick-order-network` | PASS（creates=1；productSha==uploadedSha；点击前 0/0） |
| 转换失败零 ingestion | `b1-convert-fail-zero-ingestion` | PASS（creates=0；intent revoked） |
| 转换中取消零 ingestion | `b2-cancel-during-convert-zero-ingestion` | PASS（creates=0；目录清空） |
| 超限停止 + 产物保留 + 可保存 | `c1-oversize-stops` | PASS（creates=0；保存 sha==产物） |
| 不可查看停止 + 产物保留 + 可保存 | `c2-not-viewable-stops` | PASS（同上） |
| 点击前登录过期 | `d1-login-expired-before-click` | PASS（不转换、零意图、登录链接） |
| 上传中登录过期 + 重开继续 + 不自动传输 | `d2-session-loss-reopen-continue` | PASS（creates=1；2.5s 零新请求） |
| 换账号须重新确认 | `d3-different-account-reconfirm` | PASS（拒绝零上传；接受后同 ingestion；账号重绑） |
| 转换中刷新恢复 | `e1-refresh-during-convert` | PASS（「继续转换并上传」→ creates=1） |
| 复制中刷新恢复 | `e3-refresh-during-copy` | PASS（清扫、零 ingestion） |
| 重复点击恰一 ingestion | `f1-repeated-clicks-one-ingestion` | PASS |
| 第二标签恰一 ingestion | `f2-second-tab-one-ingestion` | PASS（Web Lock 拒并发） |
| 完成回调重放恰一 ingestion | `f3-replayed-complete-one-ingestion` | PASS（completeReqs=2） |
| 取消撤销自动上传意图 | `g-cancel-after-convert-revokes-intent` | PASS（intent revoked；产物保留） |
| 仅转换隐私/离线回归 | `h-local-only-zero-network` + C3 套件 | PASS（0/0；C3 18/18） |
| 工作台 KFB 入口 + 交接不重选 + 保目标 + 结果入项目 | `i-workbench-handoff` | PASS（popup file-input 空；关联 slide_ids=[发布 id]） |
| 原生 TIFF 直传不受扰 | `i2-native-direct-upload` | PASS（无转换入口；creates=1） |
| 弹窗拦截回退 | `i3-popup-blocked-fallback` | PASS（明示 + 链接；pages=1；可重试） |
| 「新项目」目标（发布后建项目 + 幂等键） | `i4-handoff-new-project-target` | PASS（发布前 0 项目） |
| 发布后目标关联失败可恢复（审查回归） | `i5-assoc-failure-retry` | PASS（首次关联 500 → 意图 pending、pid 已写回；刷新后零自动请求，点重试 → 共 2 次关联、1 次建项目、意图 done） |
| 磁盘确认仍前置 | `j-disk-confirm-gates-oneclick` | PASS（确认前一键禁用；拒绝零任务） |

## 5. 测试与复跑

| 套件 | 命令 | 结果 |
|---|---|---|
| R1 e2e（本报告主证据） | `node tests/browser/slide_tools_r1/run_e2e.js` | **21/21 ALL PASS**（约 27 min，自起 C4 server.py） |
| C4 e2e 回归 | `node tests/browser/slide_tools_c4/run_e2e.js` | **15/15** |
| C4 工作台序列回归 | `node tests/browser/slide_tools_c4/run_workbench.js --out …` | 14 请求与基线逐条一致（`true`） |
| C3 e2e 回归（隐私/离线） | `node tests/browser/slide_tools_c3/run_e2e.js` | **18/18** |
| C2 门禁 | `node tests/browser/slide_tools_c2/test_no_whole_file.js` | PASS |
| vitest | `TMPDIR=$PWD/.gate-tmp npx vitest run tests/js` | **38 文件 595/595** |
| pytest（能力端点 / 转换闸 / ingestion API） | `TMPDIR=$PWD/.gate-tmp .venv/bin/python -m pytest tests/test_slide_tools_upload_capability.py tests/test_r1_conversion_gate.py tests/test_ingestion_api.py -p no:cacheprovider -q` | **37 passed**（分跑：capability 10；gate+ingestion 27） |
| pytest（工具页模板回归，改动模板后补跑） | `… tests/test_slide_tools_page.py …` | **13 passed** |
| pytest 全量（审查修复后） | `… pytest tests … --deselect <已知无关>` | **2868 passed, 8 skipped** |

（逐条命令与原始输出见 `docs/review-evidence/slide-tools/R1/RERUN.md` 与 `results/`。）

## 6. 已知缺口与限制

1. **假后端边界**：R1/C4 e2e 中 `/api/ingestions*`、COS 分块 PUT 与能力端点由
   Playwright 有状态假后端承担（真实 Flask 承担登录、页面、静态与 C2 转换内核）。
   服务端 ingestion 状态机/容量/权限语义由 pytest 覆盖（`tests/test_ingestion_api.py`
   等），不在浏览器 e2e 内重复。
2. **目标关联（二轮修复后）**：i/i4 走真实 `POST /api/project/create` 与
   `POST /api/project/<pid>/slides`，发布的是测试库里真实的 ready 切片（测试服务
   `--seed-ready-slide`：合成 TIFF 落 id_bundle 路径 + `mark_ready`），并用真实
   `GET /api/project/<pid>` 核对项目里确有该切片。i5 的关联失败仍是注入的 500
   （故障注入，不是端点替身）。COS 传输与 ingestion 发布仍是假后端。
3. **两个普通用户的真实授权**（d4/d5）只覆盖到 `upload-complete`（服务端进入
   `completing`）：测试服务只有进程内假 COS 的 Initiate，不推进 completing 之后
   的远端核验与发布。
4. **e2e a 的顺序证明**依赖 gateCreate 闸（放行时读 OPFS 记录为 ready）+ 请求日志
   （能力预检先于一切 `/api/ingestions`）；未做逐毫秒 UI 事件时间线（小夹具阶段切换
   亚秒，轮询不可靠）。
5. **2 GiB 夹具（b2/e3）与 230 MB 夹具（e1）**：e1 不用 2 GiB 是因为其产物
   （~900.4 MB）恰好超过 C4 测试服务的产品上限 900,000,000——那会触发（正确的）
   超限停止而非走完「续跑 → 上传」。
6. **上传阶段的「取消」撤销意图**（g）经由「超限/不可查看停止态」验证——正常
   happy 链中转换完成到上传开始之间没有用户可点的空窗（自动衔接，这正是需求）。
7. 工作台多文件批量（一次选多个 KFB）未做专门入口：当前逐文件行内入口可用；
   批量交接（一个 popup 多任务队列）留待后续。

## 7. 验收审查修复（2026-09-30）

审查发现：原 `handlePublished` 在关联工作台目标**之前**就把意图标记 `done`，已发布
行也没有任何重试入口——关联请求失败或发布后标签被关闭，切片就永久留在「未归类」，
用户无从补救。另一处：页面注入的 `onPublished` 包装没有返回 promise，上传控制器与
行内按钮的 `await onPublished(...)` 实际不等待关联完成，列表会在意图更新前刷新。

修复：

- `tools-slides-convert-upload.js`：`handlePublished` 先经 `associateTarget` 关联，
  成功（或无目标）才写 `done`（带 `projectId`）并通知打开者；失败写 `assocError`、
  意图保持 pending、页级明示「已上传，但加入目标项目失败……可点『重试加入项目』」。
  「新项目」目标创建成功后先把 pid 写回 `target.project`，重试只关联、不再建项目
  （仍带原幂等键兜底）。
- `tools-slides-upload.js`：已发布行在意图 pending 且有目标时渲染「重试加入项目」
  按钮；页面加载不自动发请求，点击才重试。
- `tools-slides.js`：`onPublished` 包装返回 `handlePublished` 的 promise。
- `i18n.js`：`tools.cu.assoc.fail` 改文案（指向重试入口），新增
  `tools.upload.assoc.retry`（zh/en）。
- 回归：e2e `i5-assoc-failure-retry`（首次关联注入 500 → 断言 pending/assocError/pid
  写回 → 刷新 → 断言零自动请求 → 点重试 → 断言共 2 次关联、1 次建项目、意图 done、
  按钮消失）。只修顺序、不修包装时该场景在「retry button gone」处失败（已实测）；
  完全修复前的旧代码先写 done，按代码会在「intent after failure」断言处失败（推断，
  未回滚实测）。

修复后全部复跑：R1 21/21、C4 15/15 + 工作台序列逐条一致、C3 18/18、vitest 595/595、
C2 门禁 PASS、全量 pytest 2868 passed / 8 skipped（见 RERUN §5）。

## 8. 二轮修复：已发布任务重复上传、换账号归属（2026-09-30 用户复现）

用户对 339e06c 复现两处（均为现有测试未覆盖的路径）：

1. **已发布任务可再次上传**：上传控制器把 `published` 当作终态中的「可新建」，
   结果面板的上传动作在发布且意图 done 之后又建了第二个 ingestion 并重传文件。
2. **换账号确认不转移 ingestion 归属**：确认后只改了意图的账号，仍续传旧
   ingestion——另一普通用户收到真实服务端 403；管理员能访问，但归属与记账仍在原
   用户。

修复（`static/tools/tools-slides-upload.js`）：

- 发布守卫在 `runLocked` 内、任何网络请求之前：记录已 `published` → 显示发布结果，
  不拉能力、不建 ingestion；意图仍 pending 且有目标 → 只重试关联。结果面板
  （`setResultJob`/`showPublished`）对已发布任务隐藏上传按钮，换成发布结果（关联
  未完成时附「重试加入项目」）；停止态「取消」不再撤销已发布任务的意图。
- 归属：见 §2.3。`app.py` 能力端点增加 `account_label`；模板增加 `#account-dialog`；
  i18n 删除 `tools.upload.account.changed.confirm`，新增 `tools.account.*`、
  `tools.upload.superseded*`。

测试（先证明能抓到旧缺陷）：

| 场景 | 旧控制器（339e06c）上 | 修复后 |
|---|---|---|
| `k1-published-repeat-clicks` | FAIL：`re-uploaded: creates=2 puts=4`（去掉 UI 断言的探针副本） | PASS |
| `d4-two-users-separate-upload`（真实授权） | FAIL：B 对 A 的 ingestion `GET → 403` ×2（接受 confirm 的探针副本） | PASS |
| `d5-two-users-return-to-original`（真实授权） | — | PASS（A 续传同一 ingestion 至 completing，creates=1） |
| `d3-different-account-reconfirm`（管理员） | 原断言「同一 ingestion」即缺陷本身，已改 | PASS（creates=2，原上传留在 superseded） |
| `i5-assoc-failure-retry` | — | PASS（关联未完成时重复触发只重试关联） |
| `i-workbench-handoff` / `i4-handoff-new-project-target` | — | PASS（真实项目端点 + 真实切片） |

测试服务（`tests/browser/slide_tools_c4/server.py`）新增：第二个普通用户、
`--fake-cos-worker`（进程内只做 Initiate 的假 COS，真实 ingestion 可到 uploading）、
`--seed-ready-slide`。C4 套件不带这些参数，行为不变。
