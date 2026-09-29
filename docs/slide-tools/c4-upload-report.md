# C4 上传接入报告（/tools/slides → COS 直传）

日期：2026-09-29。对照计划：`docs/browser-slide-tools-and-baidu-plugin-agent-plan-20260929.md`
§1（导出能力 ≠ 查看能力）、§3/§5（产物 File 交给既有分块上传器、不整体物化）、
§6（CSP 最小面）、§9 C4（原生直传不回退；登录过期不丢本地产物；限额按最终文件）。
前置：C3 工具页（`docs/slide-tools/c3-tool-page-report.md`）。
复跑命令：`docs/review-evidence/slide-tools/C4/RERUN.md`；证据 JSON：同目录 `results/`。

> 撰写纪律：下文全部 PASS/数字均来自 2026-09-29 实跑（命令见 RERUN.md）。

> **验收修订（2026-09-29，审查方）**：以下改动与数字已由审查方复跑确认，正文相应更新。
> 1. 遗留上传记录（发起上传的标签已关闭）原先会永久阻止删除——未登录用户无法经
>    「继续→取消」解开。现改为：上传期间持有 Web Lock `slide-transform:upload:<jobId>`；
>    `discardJob` 取不到锁（任一标签在传）一律拒绝，取得锁但记录未收口时须用户二次确认
>    「放弃上传」（尽力 POST cancel）后以 `{abandonUpload:true}` 删除。同一把锁也让
>    另一标签页的「继续上传」直接提示「正在另一个标签页上传」，不会并发创建/续传。
> 2. 引擎创建后「先落记录再发任何请求」原先不等待异步（OPFS）写入；现 `storage.save`
>    返回的 Promise 被等待。取消后迟到完成的 PUT 不再把已移除的续传记录写回。原单测对
>    「save 先于 PUT」的断言是空断言，已补两条会在旧实现上失败的单测。
> 3. 新增 e2e j（真实会话丢失：清 cookie + 刷新 → 真实能力端点 401 → 真实重新登录 →
>    同一 ingestion 发布）与 k（跨标签锁 + 放弃遗留上传）；C4 e2e 12/12。
> 4. 读取证明加严：荧光证明必须读到 `channel_count`；format 常量与核心转换报告的
>    `format` 值绑定（新用例）。审查方另以真实样本复核平台读取：KFB-1 输出 OpenSlide
>    9 层、KFBF-A 输出 TiffFileSlide 17 层 6 通道均可读区域（样本仅用别名，未入库）。
> 5. 工作台请求序列「before」由审查方把 HEAD 版 app.js/index.html 临时放回独立重捕获，
>    与重构后逐条一致（14 请求）。e2e 运行器原先在存在旧 creds 文件时不启动服务（全部
>    场景连不上），现默认自起服务，`--reuse-server` 才复用。
> 6. 无整文件物化门禁扩展到 `static/upload/cos-uploader.js`、`tools-slides-upload.js`、
>    `tools-slides.js`。静态资源由 Flask 3.1 以 `Cache-Control: no-cache` 下发（每次
>    重新验证），模块/wasm 无版本号不会造成新旧混用。

## 0. 结论摘要

| 门禁 | 结果 | 数值 / 证据 |
|---|---|---|
| 共享上传器抽出（Item 2） | **通过** | 工作台请求序列重构前后**逐条一致**（14 请求：create→GET→sign[1–8]→8×PUT→complete→GET→GET；`results/workbench-seq-{before,after}.json` diff 为空）；既有 vitest **548/548 零断言改动**通过 |
| 能力端点（Item 3） | **通过** | `GET /api/tools/slides/upload-capability`：未登录 401 auth_required；登录后 200（cos_upload 同权威载荷 + viewable_formats）；demo 恒 False |
| 查看格式准入 = 读取器证明 | **通过** | pytest 用原生 CLI 合成产物经 `slide_io.open_slide` 实开：明场 OpenSlide（4 层）+ 读区域；荧光 TiffFileSlide + 读区域 + 逐通道 plane；表与证明集合精确相等 |
| 点击前零网络（C3 j/k 不回退） | **通过** | C3 套件 **18/18**（j-network-capture 0 `/api/`、k-offline 全流程 sha=原生）；C4 场景 i 点击前 0 `/api/`、0 跨源 |
| 登录过期不丢产物 | **通过** | 场景 b：sign 401 → 「登录已过期」+ `/login?next=/tools/slides` 链接、不自动跳转、产物 sha 不变 → 恢复后**同一 ingestion**（creates=1）→ published |
| 刷新续传 | **通过** | 场景 c：刷新后列表展示上传状态 + 继续按钮 → 同 ingestion、只补签/补传未确认分块（resume signed `[[3,4]]`、PUT 只 3/4） |
| 重复点击恰一创建 | **通过** | 场景 d：上传三连击 + 继续两连击 → creates=1 |
| 完成响应丢失 | **通过** | 场景 e：complete 第 1 次网络失败 → 限速重发 → 409 `ingestion_state_conflict` 视为已完成 → 轮询 published（completeReqs=2） |
| 限额按最终文件 | **通过** | 场景 f：capability `max_size_bytes` < 输出 → 按钮禁用 + 原因、**零 ingestion 调用**、保存仍可用（保存 sha=原生） |
| 不支持查看的格式 | **通过** | 场景 g：format 不在服务端表 → 禁用 + 原因、零 ingestion、保存仍可用 |
| 取消/终态失败保产物 | **通过** | 场景 h：上传中删除禁用（runner `upload_active` 拒绝 + 按钮 disabled）；取消 → 产物/ready 保留、记录 cancelled、删除解禁；终态失败 → creates=2（显式重试才新建）；删除最终清空目录 |
| CSP（Item 5） | **通过** | 无 COS 配置 = C3 原串逐 token；配置后恰多一个 `https://<bucket>.cos.<region>.myqcloud.com`（无通配/无 http/单 origin）；非法值 fail-closed 不追加；pytest 逐 token 精确断言 |
| C2 回归 | **通过** | `run_faults.js` **26/26**；`run_smoke.js` PASS；`test_no_whole_file.js` PASS |
| vitest | **通过** | 37 文件 **557/557**（基线 36/548 + 共享引擎单测 9；既有断言零改动） |

## 1. 交付物（代码）

| 文件 | 内容 |
|---|---|
| `static/upload/cos-uploader.js` | **共享 COS 上传引擎**（classic script，`window.HP_COS_UPLOAD`；工作台与工具页同一份）。依赖全部注入：`apiFetch`（认证/CSRF）、`config`（resolveConfig 规范化后的 capability）、`storage`（save/complete/remove/findResumable/readConfirmed 持久化适配器）、`source`（`{name,size,slice}`——只按分块 slice，绝不整体物化）、`onEvent`（status/progress/created）。状态机（drive/sign/put/complete）从 app.js **原样迁出** |
| `static/app.js` | COS 段瘦身为工作台适配层：`uploadFileCos` 只做行 UI（阶段文案/失败映射/成功跳转）+ 注入 localStorage `pt.cos.jobs` 适配器；`resolveCosConfig` 委托共享引擎；`cosUploadSucceeded` 拆出（原 succeed 的 UI 部分）。`_EXTRA_I18N`、阶段/错误码文案、`HP_UPLOAD` 导出面全部不变 |
| `templates/index.html` | `cos-uploader.js` 在 app.js 之前加载（1 个新增 script；无脚本计数断言受影响） |
| `app.py` | `GET /api/tools/slides/upload-capability`（登录后；cos_upload 同权威载荷 + `viewable_formats`）；`SLIDE_TOOLS_VIEWABLE_OUTPUT_FORMATS` 常量（按核心 result.format 键控）；`_SLIDE_TOOLS_CSP` 常量 → `_slide_tools_csp()` 函数（桶/区域齐全且合法才追加唯一 COS origin） |
| `static/tools/slide-transform/engine.js` | `ERROR_CODES.UPLOAD_ACTIVE`（`upload_active`）+ `UPLOAD_ACTIVE_STATES` |
| `static/tools/slide-transform/runner.js` | 公共 API 新增 `setJobUpload(jobId, patch)`（经 `_updateJobRecord` 串行合并 `record.upload`）、`artifactView(jobId)`（ready 产物 File 视图，仅 slice 用途）；`discardJob` 上传活跃期拒绝（typed `upload_active`）；启动清扫**跳过带 upload 记录的任务**；`_summary` 增 `result.format` 与 `upload` 字段；头注释 API 面同步 |
| `static/tools/tools-slides-upload.js` | 工具页上传控制器：点击后能力判定（登录→可用→限额→格式）→ 引擎装配（OPFS 产物视图、pageApiFetch：CSRF 每次重读 cookie、**401 不重定向**）→ 记录写回 `record.upload`（串行队列 + 收口冲刷后刷新列表）→ UI（阶段文案/进度/登录链接/工作台链接/行内继续上传/删除禁用） |
| `static/tools/tools-slides.js` | 结果面板上传块接线；任务行渲染上传段（actions 挂行**之后**，段内 insertBefore 以其为锚）；语言切换重渲 |
| `templates/tools_slides.html` | 结果区上传块（`#upload-btn`/`#upload-cancel-btn`/`#upload-status`）；`cos-uploader.js` classic script；隐私说明更新为「点击上传后才连接平台与对象存储」 |
| `static/tools/tools-slides.css` | `.upload-block` / `.job-upload-state`（文字为主、色调辅助） |
| `static/i18n.js` | 新增 `tools.upload.*`（27 键）+ `tools.err.upload_active`，zh/en 成对；`tools.privacy.note` 文案更新 |
| `tests/test_slide_tools_upload_capability.py` | 读取器证明（BF/FL 实开+读区域+通道）、证明集合 ⇄ 常量精确相等、端点 401/载荷字段/off 仍 200/demo 恒 False、`.tif`/`.ome.tif` 注册表受理 |
| `tests/test_slide_tools_page.py` | CSP 基线夹具显式清桶/区域（确定性）+ 新增 COS origin 精确断言、非法值 fail-closed 矩阵、共享引擎资源存在 |
| `tests/js/cos-upload.test.ts` | **仅改 harness 加载**：同 realm 先执行 `cos-uploader.js` 再 `app.js`（断言零改动） |
| `tests/js/cos-uploader-shared.test.ts` | 共享引擎单元测试（注入假 apiFetch/storage/source）：slice-only、save-先于-PUT、完成丢失重发+409、续传跳过已确认块+/resume、401 reject（引擎无 location）、cancel、terminal |
| `tests/browser/slide_tools_c4/` | `server.py`（真实 Flask + 假 COS env + 一次性 owner/user 凭据文件 + 产品上限压到池准入之下）、`lib.js`（登录、page.route 有状态假 ingestion/COS、OPFS 产物 sha）、`run_e2e.js`（场景 a–i）、`run_workbench.js`（工作台请求序列回归） |

## 2. 设计要点与裁决

### 2.1 共享引擎依赖接口（Item 2）

```text
window.HP_COS_UPLOAD.resolveConfig(payload) -> 配置 | null   # available!==true、缺字段、非法值一律 null
window.HP_COS_UPLOAD.createUpload({
  source:   { name, size, slice(start,end) },   # File 或 OPFS 产物视图；引擎只按计划分块 slice
  apiFetch: (url, opts) => Promise<Response>,   # 认证/CSRF/401 策略由调用方注入
  config:   resolveConfig(...) 结果,
  storage:  { save({job_id,filename,size,confirmed,slide_id?}),
              complete(id, {succeeded,slide_id?} | {terminal,fail_code?}),
              remove(id), findResumable(source)?, readConfirmed(id) },
  resumeJobId, skipConfirm, confirmResume(),
  retryCompleteOnNetworkError,   # 工具页 true：完成响应丢失限速重发（≤4 次，1.5s）
  useResumeEndpoint,             # 工具页 true：续传时已离开 uploading → POST /resume
  onEvent(ev),                   # {type:'status',body} | {type:'progress',frac} | {type:'created',jobId,body}
}) -> { cancel(), done }         # viewable→resolve{ok,body}；取消→resolve{cancelled}；失败→reject 分类错误
```

- **classic script 形态**：工作台（index.html 在 app.js 前加载）、工具页（CSP `script-src 'self'` 允许 classic `<script src>`）、vitest harness（同 realm 先引擎后 app.js）三处共用一份源码。
- **工作台行为不变**：适配层保持同请求、同阶段文案、同 localStorage 记录形态；`retryCompleteOnNetworkError=false`（完成网络失败仍走行级失败，与抽出前一致）；`useResumeEndpoint=false`。工具页两项开 true 是 C4 新语义（显式记录，不算工作台回归）。
- 引擎自身无 DOM/i18n/location——401 只 reject `{status,data}`，**跳转与否完全是调用方策略**（工作台 apiFetch 跳登录；工具页 pageApiFetch 不跳）。

### 2.2 能力端点契约（Item 3）

`GET /api/tools/slides/upload-capability`（需登录；未登录 401 `{code:"auth_required"}`）：

```json
{ "cos_upload": { …与工作台 bootstrap 同一 _cos_upload_capability_payload… },
  "viewable_formats": ["classic-bigtiff-jpeg-pyramid",
                        "ome-bigtiff-subifd-multichannel-jpeg-passthrough"] }
```

- `viewable_formats` 按**核心 result.format 字符串**键控（非扩展名）；准入唯一门禁是
  `tests/test_slide_tools_upload_capability.py` 的读取器证明——证明集合与常量精确相等，
  哪个格式实开失败就必须从表中移除（页面随之显示「平台暂不支持查看」并禁用上传）。
- capability off 仍 200（页面要展示明确原因）；`_cos_upload_capability_payload` 恒以
  demo=False 调用（pytest spy 断言）。
- 页面判定顺序（全部发生在**点击后**）：登录（401→登录提示）→ 能力可用 →
  `result.output_bytes ≤ max_size_bytes`（最终文件，不是源文件）→ format ∈ viewable_formats；
  任一「否」给明确原因、按钮对该结果保持禁用、本地保存不受影响。
- 上传文件名：明场 `<base>.tif`、荧光 `<base>.ome.tif`（注册表要求完整复合后缀；
  `_cos_ingestion_kind_for` 对两者均 native 受理，pytest 断言）。

### 2.3 上传记录与不丢产物（Item 4）

- `record.upload = {ingestionId, filename, size, state, confirmedParts, slideId, updatedAt, error}`，
  一律经 `runner.setJobUpload`（`_updateJobRecord` 串行双槽写）。引擎创建 ingestion 后
  **等待** `storage.save` 落盘，才查状态/签名/PUT（vitest 单测断言顺序）。
- 复用规则：记录非终态（非 published/failed/cancelled）→ 续传同一 ingestion（engine
  `resumeJobId` + `readConfirmed` 跳过已确认块）；终态后的新建只来自显式点击（场景 h：
  取消后重试 creates=2）。本标签 busy 守卫防并发（场景 d）。
- 401：pageApiFetch 不重定向；错误分类为登录过期 → 消息 + `/login?next=/tools/slides`
  链接；记录保持非终态 → 登录后继续同一 ingestion（场景 b）。CSRF 每次调用重读 cookie。
- 刷新：列表按 `job.upload` 展示状态并给「继续上传」；继续走 GET 状态 +
  （已离开 uploading 时）`POST /resume`，再签/再传**未确认**分块（场景 c：只 [3,4]）。
- 完成响应丢失：`retryCompleteOnNetworkError` 限速重发；409 `ingestion_state_conflict`
  视为已完成 → 轮询收口（场景 e；引擎 post/send 分层防递归双判）。
- 取消/失败：产物与 ready/exported 状态保留；上传期间持有 Web Lock
  `slide-transform:upload:<jobId>`，`discardJob` 取不到锁即抛 `upload_active`（typed，
  `lockHeld:true`），本标签在传时删除按钮禁用；遗留未收口记录（`lockHeld:false`）经
  二次确认放弃（尽力 POST cancel）后 `discardJob(id,{abandonUpload:true})`（场景 k）；启动清扫跳过带 upload 记录的任务
  （防御——upload 只能来自 ready，正常清扫本就不会命中）。
- published：记录 `state='published'` + `slideId`（状态响应回填），显示到工作台链接
  （`/app`）；本地产物保留至用户删除（场景 a/h 验证 sha 全程不变）。

### 2.4 CSP（Item 5）

`_slide_tools_csp()`：`connect-src 'self'` +（桶/区域都配置且均匹配
`^[a-z0-9][a-z0-9-]{0,61}[a-z0-9]$` 时）唯一 `https://<bucket>.cos.<region>.myqcloud.com`。
缺一半、含空格/协议/路径/通配 → 不追加（fail-closed）。其余指令与 C3 逐字相同。
读取点为 `cos_config` 模块属性（pytest 可 monkeypatch）。

## 3. 测量与证据（全部实跑）

### 3.1 pytest（`results/pytest.txt`）

    TMPDIR=$PWD/.gate-tmp .venv/bin/python -m pytest \
      tests/test_slide_tools_page.py tests/test_slide_tools_upload_capability.py \
      tests/test_ingestion_api.py tests/test_cos_ingestion_kinds.py \
      tests/test_cos_review_fixes.py tests/test_phase1_auth_ui.py \
      tests/test_slide_id_review_r16_homepage.py -q
    → 111 passed

读取器证明细节：明场 `bf.tif` → OpenSlide（levels=4），read_region(0,0,64×64) 正常；
荧光 `fl.ome.tif` → TiffFileSlide（levels=3），read_region + `read_region_channels`
两通道 plane 形状正确；两格式均列入 viewable_formats（证明集合 == 常量）。

另跑加载 index.html/app.js 的既有套件：`test_demo_access.py`、`test_stage2_ui.py`、
`test_slide_format_registry.py` 全绿；`test_ai_budget_wiring.py` 有 **1 个与 C4 无关的
预存失败**——`plugins/pathtogether-admin/manifest.json` 他人未提交改动把
`pluginVersion` 从 0.4.12 升到 0.4.13，而该测试断言 0.4.12（该文件属他人，本次不动；
失败断言只读 manifest，不涉及本次任何改动文件）。

### 3.2 vitest（`results/vitest.txt`；基线 `results/vitest-baseline.txt`）

    基线（改动前）：Test Files 36 passed (36) / Tests 548 passed (548)
    之后：        Test Files 37 passed (37) / Tests 555 passed (555)
    既有断言零改动；唯一 harness 改动 = cos-upload.test.ts 的 loadApp 同 realm
    先执行 static/upload/cos-uploader.js（加载方式，非断言）。

### 3.3 浏览器 e2e（真实 Flask + 登录会话 + page.route 假后端；`results/c4-e2e.json`）

    node tests/browser/slide_tools_c4/run_e2e.js → E2E ALL PASS（10/10 场景）

| 场景 | 关键断言（结果） |
|---|---|
| a-bf | filename `bf-580x300.tif`；PUT 字节合计 = 产物 287,803 B；published + `/app` 链接；行 `data-upload-state=published`；sha `f265596a7662…`（= C3/原生标定）前后不变 |
| a-fl | filename `fl-600x400.ome.tif`；134,114 B；sha `910d170e…`（= C3/原生）不变 |
| b-401 | 「登录已过期」+ `/login?next=/tools/slides`；URL 未跳转；creates=1；恢复后同 ingestion → published |
| c-refresh | 续传只签 `[[3,4]]`、只 PUT 3/4；creates=1；sha 不变 |
| d-repeat | 三连击 + 继续两连击 → creates=1 |
| e-lost | completeReqs=2（丢失→重发→409=已完成）→ published |
| f-oversize | 「超过平台上限」；creates=0、statusGets=0；按钮禁用；保存 sha=原生 |
| g-format | 「平台暂不支持查看」；creates=0；保存 sha=原生 |
| h-cancel | 上传中删除禁用 → 取消（cancels=1、行 cancelled、删除解禁、sha 不变）→ 终态失败（行 failed、creates=2=显式重试）→ 删除清空目录 |
| i-network | 点击前 0 `/api/`、0 跨源；点击后仅 capability + `/api/ingestions*` + COS origin；非 COS 请求体 ≤4 KiB 且不含 sha 片段 |

### 3.4 工作台原生直传回归（`results/workbench-seq-{before,after}.json`）

    重构前捕获：14 请求（POST /api/ingestions → GET → sign[1–8] → 8×PUT →
                 complete → GET → GET；localStorage pt.cos.jobs 收口为 []）
    重构后捕获：序列逐条一致（JSON 深比较相等）

### 3.5 C3/C2 回归

    node tests/browser/slide_tools_c3/run_e2e.js   → 18/18（含 n1/n2；j 0 /api/、k 离线）
    node tests/browser/slide_tools_c2/run_faults.js → 26/26
    node tests/browser/slide_tools_c2/run_smoke.js  → SMOKE PASS
    node tests/browser/slide_tools_c2/test_no_whole_file.js → PASS

## 4. 登录方式（e2e）

C4 `server.py` 仿 `tests/e2e/e2e_server.py`：`BOOTSTRAP_OWNER_LOGIN_ID` +
`BOOTSTRAP_OWNER_PASSWORD_FILE`（owner 启动期自动首建）、`user_store_pg.
create_user_with_total_allowance` 种普通用户；一次性随机密码只写 `--creds` JSON
（进程内存 + 文件，不进日志）。Playwright 侧走**真实登录表单**（GET `/login?next=…`
→ 填 `#login-dialog-username/password` → 提交；CSRF 隐藏域由页面自带）。

## 5. 偏差与已知事项

1. **假 COS 是唯一被执行的上传通道**（外部门禁）：真实桶上传、真实桶 CORS、真实会话
   过期时序不在本机验证——page.route 假后端在 CSP 检查之后运行，CSP 写错仍会失败，
   但真实 CORS 头配置只能上真桶验证。
2. `retryCompleteOnNetworkError` / `useResumeEndpoint` 为工具页语义，工作台适配层
   显式关掉（保持抽出前行为）；这是新能力不是工作台行为变化（请求序列回归证明）。
3. 引擎 cancel 只 abort 在途分块 PUT；控制 API 悬置时 `done` 可能长时间 pending——
   工具页取消即收尾 UI（与工作台行取消同语义），迟到的 done 结果被忽略。
4. 荧光格式目前列入 viewable_formats（读取器证明通过：TiffFileSlide + 通道读取）。
   若平台读取器将来打不开（如 OME 结构变化），读取证明 pytest 会失败——届时必须从
   表中移除该格式，页面自动回到「不支持查看」分支。
5. 合成 KFBF 部分小尺寸（如 320×240）会被核心校验拒绝（IFD 布局错位）——读取证明
   与 e2e 用 600×400（C2/C3 同参）。
6. 工具页上传不自动关联项目（无 import 目标概念）；发布后的 slide 经工作台列表可见，
   关联由工作台既有交互完成。
7. 任务列表行的上传按钮对 `ready/exported` 任务恒出现；未登录/能力 off 时点击给出
   原因（不预先探测——保住点击前零网络承诺）。

## 6. 外部门禁（本机无法完成，未宣称通过）

- 真实 COS 桶直传（含真实签名 URL、真实分块并发、断流重试时序）与桶 CORS 配置。
- 真实登录过期时序（session TTL、cookie Secure 属性差异）——e2e b 用 401 注入、j 用清除会话 cookie 后的真实 401 与真实重新登录，均非真实超时。
- Firefox/Safari/Edge；真实 4/8 GB 整机（C7）。
- 生产 nginx 链路上的 CSP 头透传复核（C3 §5 结论：全量 proxy_pass，无改写风险）。
