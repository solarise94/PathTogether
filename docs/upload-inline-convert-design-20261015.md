# 工作台上传面板内转换 —— 设计（2026-10-15）

状态：设计（本文件只描述方案，不含实现）。分支 `upload-inline-convert`，基线
`upload-liveness`（`dca42ac2`）。

## 0. 目标与不变量

产品目标（用户已确认）：工作台选文件/拖入后**全部在上传面板内完成**——
浏览器识别 → 需要转换的在面板里排队、本机转换、结构校验通过才上传；不需要
转换的直接上传；转换失败或内容与扩展名不符，就在上传行说明原因并给
「申请支持该格式」按钮。不再跳转或弹出 `/tools/slides`（该页面保留可单独
使用）。

三个必须守住的不变量（都来自现有实现，本设计不得放宽）：

1. **词表权威在服务端**。前端不做第二份格式词表：受理集/上限来自
   `bootstrap.capabilities.cos_upload`（`app.py:3181 _cos_upload_capability_payload`）；
   `direct_class` 声明与直传关闭闸在服务端 `app.py:3122 _validate_direct_class`；
   内容级复核在 worker（`upload_direct_class.enforcement_failure`，
   `upload_direct_class.py:328`）。
2. **ingestion 只在真正开始上传时创建**。现有 `uploadFileCos`
   （`static/app.js:10655`）经共享引擎 `createUpload`（`static/upload/cos-uploader.js:343`）
   在 `createNewJob`（`cos-uploader.js:1033`）里 POST `/api/ingestions`——转换
   阶段一个任务都不建（R1 合同，`static/tools/tools-slides-upload.js:667` 注释）。
3. **转换与上传都在浏览器内**，无服务端转换（`app.py:3045
   SERVER_CONVERSION_CREATION = False`）。

## 1. 现状（读码结论，供对照）

| 位置 | 现状 |
| --- | --- |
| `static/app.js:10104` `uploadFile` | 冻结目标快照 → `HP_SLIDE_SNIFF.classifyFile` → `uploadFileClassified` |
| `static/app.js:10128` `uploadFileClassified` | `cls==='convert'` → `offerBrowserConvert`；`unsupported` → 报错；否则 `uploadFileCos` |
| `static/app.js:10326` `offerBrowserConvert` | 行内 + 抽屉「在本机转换并上传」按钮，点击 → `openConvertHandoff` |
| `static/app.js:10247` `openConvertHandoff` | `window.open('/tools/slides')` + `postMessage('pt:convert-upload-handoff')` 交接 File/bundle |
| `static/app.js:11123` `handoffBundleFolder` | MRXS/VMS 文件夹整包交接 |
| `static/app.js:11041` `handleWorkbenchDrop` / `:11066` `importDroppedDirectory` | 拖入散文件逐个 `uploadFile`；含 `.mrxs/.dat` 成员走 `handoffBundleFolder` |
| `static/app.js:10655` `uploadFileCos` | 行 UI + `HP_COS_UPLOAD.createUpload`（`createBody.direct_class`、内容锁、心跳不跳转） |
| `static/app.js:10875` `restoreCosJobs` | 刷新后只读恢复**上传**任务行（`pt.cos.jobs`） |
| `static/upload/slide-sniff.js:776` `classifyFile` | 有界嗅探 → `ome-tiff / converter-bigtiff / convert / temporary / unsupported` + `directClass` |
| `static/upload/slide-sniff.js:610` `verifyUploadHeader` | 创建前 16 字节魔数核对（PNG 改名 `.jpg` 等零网络拒绝） |
| `static/tools/slide-transform/engine.js:578/811` | `sniffTiffSlideCapability` / `sniffRasterCapability`：复制前的**引擎权威**可转换判定 |
| `static/tools/slide-transform/runner.js:418/444/794/807` | `probe` / `prepareBundle` / `startJob` / `resumeJob`，都持 Web Lock `slide-transform:heavy`（`runner.js:76`） |
| `runner.js:634` `setJobIntent` | `record.intent`（上传意图；prepared 起可写）——R1 用它做刷新后「继续转换并上传」 |
| `runner.js:1386` `artifactView` | ready/exported 产物 → 可 `slice()` 的 File 视图（绝不整体物化） |
| `static/tools/tools-slides-upload.js:667` `runLocked` | 能力 → 账号 → 大小上限（按**最终产物**）→ `viewable_formats` → 复用/新建 ingestion → `artifactView` → `createUpload` |
| `app.py:3626` `/api/tools/slides/upload-capability` | `cos_upload` + `viewable_formats` + `account` / `account_label` |
| `app.py:12092` `POST /api/format-requests` | `format_request_http.handle_submit`（`:118`）：`format_ext` ≤40、`message` ≤2000、`contact` 邮箱、样本 ≤64 MiB、429 限流 |

**关键缺口**：工作台 bootstrap 的 `cos_upload` 载荷里**没有** `viewable_formats`
（实测：`python3 -c "app._cos_upload_capability_payload(False).keys()"` →
`['available','browser_convert','direct_upload','formats','manual_only']`）。
上传两道门禁的第二道（产物平台可查看）目前只有工具页拿得到，本设计必须补进
工作台载荷。

### 1.1 `/app` 的 CSP 实测（本次指令要求核对）

用真实 Flask app + test client 实测（命令见 §10「本次已跑的核对」）：

```
/app         200 | CSP: <none> | XFO: <none>
/tools/slides 200 | CSP: default-src 'none'; script-src 'self' 'wasm-unsafe-eval';
                    worker-src 'self'; connect-src 'self'; style-src 'self'; …
```

即：**`/app` 当前不下发任何 CSP**（`app.py:3555 _workbench_response` 只设
`Cache-Control: no-cache`；`_slide_tools_csp()`（`app.py:3448`）只挂在
`/tools/slides`）。所以面板内转换**不存在既有 CSP 阻挡**。

但这不等于可以什么都不做：

- `/app` 用的是**内联 `window.HP_APP_BOOTSTRAP`**（`templates/index.html:15`）
  与多处内联样式，将来若给 `/app` 加 CSP 必须带 `'unsafe-inline'` 或 nonce；
- 一旦给 `/app` 加 CSP，必须同时包含 `script-src 'wasm-unsafe-eval'` 与
  `worker-src 'self'`，否则 WASM 编译与 module worker 会被拦。
  本设计把这条写成**回归断言**（§9 pytest）：`/app` 若存在 CSP 头，必须包含
  `'wasm-unsafe-eval'` 与 `worker-src 'self'`。
- 公网响应头（用户提供的 302 列表）只有 nginx/edge 的 HSTS 与 `x-histopilot-*`，
  没有 CSP；仓库内 `deploy/domain-routing/*.conf` 也只在 `location` 级做转发，
  未加 CSP（`grep add_header` 仅命中 HSTS）。
- 静态资源类型已就绪（实测）：`slide_transform_bg.wasm` →
  `application/wasm`（`app.py:205 mimetypes.add_type`），`worker.js` /
  `runner.js` → `text/javascript`，均 `Cache-Control: no-cache`。
- **动态 import 可用性已实测**：在真实 `/app` 页面（`AUTH_ENABLED=False` 的工作台
  渲染，同一 `index.html`）里执行
  `await import('/static/tools/slide-transform/runner.js')` →
  `SlideToolsRunner.create()` 成功、`coreVersion=0.1.0`、`listJobs()` 返回 `[]`，
  且页面零 `pageerror` / 零 `console.error`（见 §10）。classic script（app.js）
  里做动态 import 是可行的。

## 2. 模块划分与文件改动清单

### 2.1 新增

| 文件 | 职责 | 形态 |
| --- | --- | --- |
| `static/upload/inline-convert.js` | **面板内转换编排器**（`window.HP_INLINE_CONVERT`）：队列状态机、每类格式的判定表（§4）、转换/上传调度、锁与槽、失败 → 申请支持的桥。无 DOM 依赖细节、无网络请求（`fetch`/跑 runner 全部注入），可 vitest 直测 | classic script（与 `cos-uploader.js` / `slide-sniff.js` 同风格，先于 `app.js` 加载） |

放新文件的理由：判定表 + 状态机是本次改动里唯一需要**穷举测试**的部分；放进
`app.js`（14036 行、classic IIFE）无法独立驱动，而 `app.js` 已有
`window.HP_*` + `__PT_TEST_HOOKS` 的成熟先例。

### 2.2 修改

| 文件 | 改动 |
| --- | --- |
| `static/app.js` | ① `uploadFile`（:10104）→ 交 `HP_INLINE_CONVERT.enqueue`；② `uploadFileClassified`（:10128）→ 判定表驱动（直传 / 转换 / 当场拒绝）；③ `offerBrowserConvert`（:10326）/`openConvertHandoff`（:10247）/`handoffBundleFolder`（:11123）保留但**仅在 `inline_convert.enabled=false` 时可达**（回滚路径，零删除）；④ 新增面板内行的渲染与**按钮组**——按钮全集以 **§6.3 为唯一权威**（含「删除本地产物」这一唯一的 OPFS 清理入口）；⑤ 新增 runner 动态 import 与生命周期（`beforeunload`）；⑥ 新增刷新恢复：`listJobs()` 里带 `intent.channel==='workbench-inline'` 的未完成任务 → 行 + 「继续」；⑦ `HP_UPLOAD` 导出面补新入口（测试驱动）；⑧ `submitFormatRequest`（:11530）抽出可复用内核 `submitFormatRequestCore({ext,message,contact,sampleFile})`；⑨ `importDroppedDirectory`（:11105-11120）的 bundle 成员正则补 `.vms`（`.mrxs|dat|vms`，**不加 `.vmu`**——理由见 §4.1.1 note）；⑩ `handleWorkbenchDrop`（:11041）的散文件路径把 `cls.bundle===true` 交给新的「需完整包」分支（§4.1.1）而不是 `uploadFile`；**⑩b 开关开时 bundle 一律进面板队列，绝不再弹 `/tools/slides`**（第四轮复审 A3 修正）：`importDroppedDirectory` 命中 bundle 时的 `handoffBundleFolder(rows, dirEntry.name)`（`:11112-11114`）与 `importPickFolder`（`:11137-11151`，**原改动清单从未提及它**）里的 `handoffBundleFolder(rows, first)` 都要改为交给 `HP_INLINE_CONVERT.enqueueBundle(rows, folderName)`——它们**无条件**调用交接（不看开关），不改就会在开关默认开时仍弹窗，与 §0 目标及 §4.1.1 动作表冲突；开关关闭时才走 `handoffBundleFolder`（回退路径保留）；⑪ **目标关联链路：一处保留 + 一处必须修**（§3.5，第四轮 A1）：保留——`enqueue` 之前仍先取 `importTargetSnapshot()`（`app.js:10109`，源码级断言 `tests/js/viewer-folders.test.ts:1095` 不许失效）、完成时 `loadAll()` → `importAssociateUploaded(…, entry.target, row)` → `openSlide(...)`（`:10862-10865`）、`created` → `fbUpAssocSave(ingestionId, target)`（`:10789`）；**必须修**——`uploadFileCos(file, row, {…})` 的实参字面量（`:10164-10170`）要**补上 `assocTarget`**（否则冻结快照从未被消费：`:10789` 被守卫吞掉、`:10864` 落到当前文件夹，属既有缺陷）；⑫ 磁盘门确认后的重试**带原始输入重新起**（`probe(entry.file,…)` / `prepareBundle(entry.bundle.rows,…)`，§6.2.1）——因此 `entry.file`/`entry.bundle.rows` 必须在内存保留到收口 |
| `templates/index.html` | 在 `cos-uploader.js` / `slide-sniff.js` 之后、`app.js` 之前加载 `/static/upload/inline-convert.js?v=20261015` |
| `static/i18n.js` | 新增上传面板内转换文案（zh/en 同步）；**改写**两条指向 `/tools/slides` 的旧文案（`upload.cos.err.convert_in_browser` @:1025/2686、`upload.err.unsupported` @:1026/2687）——见 §7 |
| `static/app.js` 的 `_EXTRA_I18N`（:30 起） | 同步兜底键（`cos-upload.test.ts:964` 那条断言会检查 `_EXTRA_I18N` 与 stage 映射同步） |
| `app.py` | ① `_cos_upload_capability_payload`（:3181）新增 `inline_convert`（默认开，见 §8）；② 同载荷补 `viewable_formats`（唯一来源 `SLIDE_TOOLS_VIEWABLE_OUTPUT_FORMATS`，:3619）；③ `/app` 加 CSP 断言用的注释（若加头，必须含 `'wasm-unsafe-eval'` + `worker-src 'self'`） |
| `static/style.css` | 复用 `.upload-item*` / `.upload-item-btn`（:827-866）与 `.import-convert-offer*`（:4210-4221）；只补「行内转换进度条 + 按钮组换行」的最小规则（缺口见 §11 风险） |

**不改**：`slide_format_registry.py`（目录行语义不变）、`upload_direct_class.py`
（词表与核验不变）、`format_request_http.py`、`static/tools/slide-transform/*`
（引擎/运行器/worker 一行不动——面板只使用它们的公开面）、
`static/tools/tools-slides*.js`（工具页保留可用；面板不复用它的 DOM 层）。

### 2.3 复用清单（不重写）

- `SlideToolsRunner`（`runner.js:123`）：`probe` / `prepareBundle` / `startJob` /
  `resumeJob` / `cancelJob` / `listJobs` / `getJob` / `artifactView` /
  `setJobIntent` / `setJobUpload` / `discardJob`。
- `HP_COS_UPLOAD`（`cos-uploader.js:95/343/1203/1215`）：`resolveConfig` /
  `createUpload` / `contentLockName` / `acquireContentLock`。
- `HP_SLIDE_SNIFF`（`slide-sniff.js:776/610`）：`classifyFile` /
  `verifyUploadHeader` / `detectBytesKind` / `extOf`。
- `engine.js`：`sniffTiffSlideCapability` / `sniffRasterCapability`（可转换终审）、
  `defaultOutputProfile` / `defaultEncodingProfile` / `isOmeProfile` /
  `outputFileName`（产物命名）、`getProfile` / `defaultProfileId`（资源档位）、
  `errCode` / `errText`（类型化错误）、`checkDiskBudget` 的 `uncertain` 语义。
- 上传控制器语义照搬 `tools-slides-upload.js:667 runLocked` 的**四道门禁**与
  `opfsStorage`（:330）的 `setJobUpload` 适配器写法；**生产代码不 import 该模块**
  （它是 ES module + 工具页 DOM 依赖），而是把同一套判定抽进
  `inline-convert.js` 的纯函数。**门禁的作用域按条目类型不同（评审 R2 修正）**，
  见 §5.1；判定等价性由 §9.2 第 12 项在**测试里** import 该模块做对照（生产与
  测试的加载面不同，这不是矛盾）。对照的**可测边界**见 §9.2 第 12 项。

## 3. 队列状态机

### 3.1 条目（entry）

```js
// 面板内单个上传条目（一行 = 一个 entry）
{
  id,                 // 本页唯一（uuid）
  file,               // File（bundle 形态为 null）
  bundle: null | { rows: [{file, relPath}], folderName },   // MRXS/VMS 整包
  name, size,         // 展示（bundle = folderName / 成员字节和）
  cls,                // HP_SLIDE_SNIFF 分类（§4 判定表）
  directClass,        // 直传声明（ome-tiff / converter-bigtiff / legacy-direct /
                      //   unconverted-variant:svs-jp2k / null）
  needsConvert,       // 布尔：引擎嗅探终审后的结论（§4）
  jobId,              // 转换任务（runner），直传条目恒 null
  slideId,            // 服务端发布产物 ID（wheel 完成时回填；见 §3.5）
  uploadJobId,        // 服务端 ingestion id（uploadFileCos 的 created 事件回填）
  target,             // 上传开始时冻结的目标快照（importTargetSnapshot，app.js:11213）
  state,              // 见 3.2
  stage: { key, frac, note, indeterminate },   // 行文案（复用 makeUploadRow.setStage）
  error: null | { code, detail, detectedType, engineReason, retryable },
}
```

### 3.2 状态

```
           ┌──────────── unsupported / magic_mismatch ──► rejected ──(申请支持)──┐
enqueue ──► sniffing ──┬── needsConvert=false ─────────► upload_queued ──────────┤
                       └── needsConvert=true ──────────► convert_queued         │
                                                             │                  │
                       (heavy lock + 转换槽有空)               ▼                 │
                                            converting ──┬──► converted ──► upload_queued
                                                         ├──► convert_failed ──(重试 / 申请支持)
                                                         ├──► need_disk_confirm ──(确认)──► convert_queued
                                                         └──► cancelled
upload_queued ──(上传槽 = pt:upload-slot:<acct>)──► uploading ──┬──► done
                                                              ├──► upload_failed ──(重试 / 申请支持)
                                                              ├──► requeue_convert ──► convert_queued   (服务端 convert_in_browser)
                                                              └──► cancelled
```

补充约束：

- `sniffing` 是**本页同步/微任务级**步骤（有界读 ≤ 128 KiB + 引擎嗅探 ≤ 256 KiB），
  不做用户可见的排队；但仍是状态（防止重复入队）。
- `converted` 不是终态：立刻转 `upload_queued`（**自动上传**——见 §3.4）。
- `done` / `rejected` / `convert_failed` / `upload_failed` / `cancelled` 为终态；
  **终态行的按钮表在 §6.3**（含「删除本地产物」，本设计必须提供的 OPFS 清理
  入口——否则源副本与产物永久残留）。
- `need_disk_confirm` 是**非终态**且**源副本已在 OPFS**：`beforeunload` 提醒
  必须覆盖它（§6.2 第 7 点，评审 J3）。
- 「1 个转换 + 1 个上传并行」：`converting` 与 `uploading` 各至多 1 个 entry
  （§5）。其余条目停在 `convert_queued` / `upload_queued`，行上显示排队位置。

### 3.3 转换子阶段（行文案）

直接复用 `runner.on('state'|'progress'|'phase')` 的既有信息，映射到引擎的
阶段词表（`tools-slides.js:1207 PHASE_LABEL_KEY` 同一套键）：

| runner state / progress | 行文案键 | 百分比 |
| --- | --- | --- |
| `probing` | `tools.phase.probing`（复制与探测中） | `progress.unit==='stage'` → `done/total`（`worker.js:139`） |
| `planned` | `tools.phase.planned` | 不定态 |
| `running` | `tools.phase.running` | `progress.unit==='level'` → `done/total`（层/瓦片行） |
| `finalizing` | `tools.phase.finalizing` | 不定态 |
| `validating` | `tools.phase.validating` | 不定态 |
| `ready` | —（立即转上传阶段） | — |

bundle 任务另接 `progress.unit==='stage-bundle'`（`worker.js:994`）显示
「第 m/n 个成员」。

### 3.4 「转换完成自动上传，无第二次确认」

沿用 R1 的授权语义（`tools-slides-convert-upload.js:98-138`）：用户**一次
拖入/选择**即授权整条链（转换 → 校验 → 准入 → 上传），中间不再询问。因此
`converted` 直接转 `upload_queued`。若最终产物超过 `max_size_bytes` 或格式不在
`viewable_formats`，则**停在 `convert_failed`**（保留本地产物 + 行内说明 +
「保存到本机」提示，不建 ingestion）。

### 3.5 目标关联与打开（第三轮评审 K2 修正：原设计缺这条，会导致静默未归类）

既有链路的**完整形态**（`app.js`）：

| 时点 | 现有代码 | 面板必须保持的语义 |
| --- | --- | --- |
| `uploadFile` 入口 | `app.js:10109` `opts.assocTarget = importTargetSnapshot()` | **入队瞬间**冻结目标快照（不是上传时、更不是完成时）——folder-experience §1.3：侧栏后续切换/重开抽屉不影响本次归属 |
| ingestion 建成（`created` 事件） | `app.js:10789` `fbUpAssocSave(jobId, opts.assocTarget)` | 按**任务 ID** 落盘（`pt.fb.upassoc.v1:<uid>`，key 见 `app.js:4028`），刷新恢复行完成时仍按原目标关联 |
| 完成（可查看） | `app.js:10862-10865`：`loadAll()` → `importAssociateUploaded(target.id, target.name, assocTarget, row)` → `openSlide(target.id \|\| target.name)` | 刷新列表 → 按冻结快照关联（`importAssociateUploaded`，`app.js:11282`）→ 打开新切片；关联失败进入待办 + 行内重试（`fbAssocPendingSave`/`fbAssocPendingClear`） |
| 刷新后的恢复行 | `app.js` 的 `pollRestoredRow` viewable 分支：`fbIdentityReady()` → `fbUpAssocTake(job.job_id)` → `importAssociateUploaded(...)`（一次性消费，"恰好一次"，见 R5 复核注释） | 同上，且**身份就绪后先 Take 再关联**（`fbIdentityReady`，`app.js:3829`） |
| 终态/取消 | 恢复行终态分支 `fbIdentityReady().then(fbUpAssocTake)` | 快照清理，不留残余 |

**面板的接线要求**（设计新增，落进 §2.2 与测试）：

1. `enqueue`（`uploadFile` 的第一件事）冻结 `entry.target = importTargetSnapshot()`
   ——**必须在 `HP_INLINE_CONVERT.enqueue` 之前取**，保持 `app.js:10109` 的
   「上传开始即冻结」语义与源码级断言（`tests/js/viewer-folders.test.ts:1095`
   `expect(appSrc).toContain("opts.assocTarget = importTargetSnapshot()")`）。
2. 走直传的 entry：把 `entry.target` 作为 `assocTarget` **显式转发**给
   `uploadFileCos(file, row, {assocTarget: entry.target, …})`，`created` 事件仍调
   `fbUpAssocSave`。**这是一处必须改的既有缺陷，不是「已做对」**（第四轮复审
   A1 修正）：`app.js:10164-10170` 的实参是新对象字面量
   `{resumeJobId, skipConfirm, directClass}`，**没有 `assocTarget`**——全仓
   `assocTarget` 只有 5 处（`:10109` 写入、`:10789`/`:10798` 读、`:10843`/`:10864`
   传参）。后果：
   - `fbUpAssocSave(jobId, opts.assocTarget)`（`:10789`）收到 `undefined`，被
     守卫 `if (!jobId || !target || (!target.pid && !target.newProjectName))
     return;`（`:4029`）吞掉 → **刷新恢复拿不到快照**；
   - 完成时 `cosUploadSucceeded(row, r.body, file, opts.assocTarget)`（`:10798`）
     → `importAssociateUploaded(target.id, target.name, assocTarget || null, row)`
     （`:10864`）的 `snap` 为 null → 落到 `var st = snap || importTargetState;`
     （`:11284`）即**完成时刻的当前文件夹**——正是本条第 5 点禁止的漂移。
   `git show 766214ad` 与 `3cd08792` 显示该字面量在历史版本里同样不含该键：
   属既有缺陷，但**面板必须在同一批接线里补上**（否则「冻结快照」只生成、
   从未被消费）。§2.2⑪ 与 §9.2 第 19 项据此改写。
3. 走转换的 entry：**转换前的 `setJobIntent` 也要带目标**
   （`record.intent.target`，`runner.js:634`），这样刷新后 intent 里也有归属；
   上传阶段的 `fbUpAssocSave` 照旧按 **ingestion id** 落盘（恢复轮询靠它）。
4. 完成时：`loadAll()` → `importAssociateUploaded(slideId, name, entry.target, row)`
   → `openSlide(slideId)`；关联失败由 `importAssociateUploaded` 自身的
   `catch` 处理（待办 + 行内重试按钮），面板不重复实现。
5. **绝不**用「完成时刻的当前文件夹/项目」做关联——那正是 folder-experience
   §1.3 明确要避免的漂移。

> 覆盖要求（`§9.1`/`§9.2`）：把 `tests/js/viewer-folders.test.ts` 列入受影响
> 清单（它对 `app.js` 做源码级断言），并在 §9.2 增加「冻结快照 → 完成时按快照
> 关联」的行为断言（用 `HP_PROJECT_UI.importDrawer.snapshot` /
> `associate` 的既有测试驱动面，`app.js:13922` 与 `:13925`；`snapshot`
> 即 `importTargetSnapshot`，`app.js:11213`）。

## 4. 每类格式的判定表

### 4.1 判定顺序（三步，次序固定）

**步 1（路由，不与引擎竞争）**：`HP_SLIDE_SNIFF.classifyFile(file)`
（`slide-sniff.js:776`）→ `cls` / `directClass` / `ext` / `bundle` / `svsJp2k`。

**步 2（当场拒绝闸，零网络）**：`HP_SLIDE_SNIFF.verifyUploadHeader(name, source)`
（`slide-sniff.js:610`，16 字节魔数）。返回非空 → `rejected`
（`detectedType` = `detectBytesKind` 结果，`slide-sniff.js:549`）。**注意**：该闸
认经典 TIFF(42) 与 BigTIFF(43)（`slide-sniff.js:563-566`），所以本机转换产物
不会被误拒（回归：`upload-liveness` `dca42ac2`）。

**步 3（可转换终审，面板不得比引擎更严）**：
`cls==='convert'` 时用引擎嗅探确认，**按 `cls.bundle` 先分派**：

- **`bundle === true`**（`.mrxs` / `.dat` / `.vms`，`slide-sniff.js:722-729`）
  → 不进单文件魔数分派，而是判「手上是不是一个完整包」（见 §4.1.1）；
- TIFF 容器（`engine.isTiffHeader`，`engine.js:400`）→ `await sniffTiffSlideCapability(file)`
  （`engine.js:578`）；
- 普通图片（`engine.isRasterHeader`，`engine.js:384`）→ `await sniffRasterCapability(file)`
  （`engine.js:811`）；
- KFB/KFBF → `engine.magicSupported`（`engine.js:405`，8 字节魔数）。

`supported===true` → `needsConvert=true`（并且把引擎给出的 `modality` 用于
输出格式默认：明场 `bf-ome`、荧光 `fl-ome`，见 §4.4）。
`supported===false` → **不转换**，按 §4.2 的「引擎拒绝分支」处理
（说出 `cap.reason`，`direct_import` 开放的重放直传，关闭的不上传 + 申请支持）。

#### 4.1.1 bundle 类条目（第一轮评审 R6 修正 + 第二轮 J2 补强）

原设计只定义了 TIFF/普通图片/KFB 三类魔数分派，而 `classifyFile` 对
`.mrxs`/`.dat`/`.vms` 返回 `{cls:'convert', bundle:true}` —— **不取决于它是不是
来自文件夹选择**（实测：`classifyExt('a.vms') = {cls:'convert', ext:'.vms',
bundle:true}`、`a.mrxs` / `x/a.dat` 同形）。同时原设计§2.2③ 又把散文件入口
（`uploadFile` → `handoffBundleFolder`）限定为开关关闭才可达，于是**开关默认开
时用户直接拖入一个 `.mrxs` 或单个 `.vms` 没有任何定义好的结局**——评审 R6 成立。

修正后的 bundle 判定与动作（**开关开、关两种形态都有定义**）：

先修一处**既有**的文件夹分流缺口（本轮评审 J2）：`importDroppedDirectory`
（`app.js:11109-11111`）判 bundle 用的正则是 `/\.(mrxs|dat)$/i`，**不含 `.vms`**——
所以「纯 VMS 文件夹」（`.vms` 入口 + 同目录 tile JPEG，无 `.mrxs/.dat`）会落到
`rows.forEach(uploadFile)`，逐成员进上传队列。而工具页的同一判定是
`tools-slides-bundle.js:16` 的 `looksLikeBundleMember` =
`/\.(mrxs|dat|vms|vmu)$/i`（`bundleRoute` 见 `:26-31`）。本设计要求把 `app.js`
的成员正则**对齐到 `.mrxs|dat|vms`**（单一判定，别再维护第二份窄正则）。

> **刻意不对齐 `.vmu`**（与 `looksLikeBundleMember` 的表面差异）：`.vmu` 今天在
> **直传受理集**里 —— 实测 `_cos_upload_capability_payload(False)` 的
> `"vmu" in direct_upload.formats` = **True**、`"vmu" in
> browser_convert.formats` = **False**、`slide_format_registry.lookup('a.vmu')
> ["capability"] = 'native-single-file'`（命令见 §10.13）。若把 `.vmu` 也划入
> 文件夹 bundle 分支，**含 `.vmu` 的文件夹会从「可用」变成「planner 专门拒绝」**
> （`planBundle` 首分支即抛 `unsupported_input`，`engine.js:1358-1362`）——
> 那是收紧既有能力，本设计不做。`tools-slides-bundle.js` 送 `.vmu` 进 planner 是
> **工具页**的语义选择，面板不复制。将来若要两处统一，必须同时改注册表与
> capability 词表，属独立变更。

| 输入形态 | 判定 | 动作 |
| --- | --- | --- |
| 拖入/选择**文件夹**（`webkitGetAsEntry` 目录遍历，`app.js:11066`；`webkitdirectory` 选择同路径）含 `.mrxs`/`.dat`/`.vms` 成员 | 成员行 `[{name, relPath, file}]` **原样交给** `runner.prepareBundle`，由 `engine.planBundle`（`engine.js:1353`）判定：`sniffMrxBundle`（`:1096`）/ `sniffVmsBundle`（`:1257`） | **整包转换**（`stage-bundle`，`progress.unit==='stage-bundle'`）。"缺哪个成员"的类型化信息（`unsupported_input` + `missing`）**由 planner 产出**——面板不预判（见下）。**开关开：进面板队列**（由 `enqueueBundle` 接；**两条既有入口都要改**：`importDroppedDirectory` 的 `handoffBundleFolder`（`app.js:11112-11114`）与 `importPickFolder`（`:11137-11151`）——**两者目前都无条件交接、不看开关**，第四轮复审 A3）；开关关：仍走 `handoffBundleFolder` 交接 |
| **散文件**（单文件拖入/选择）`.mrxs` / `.dat` | `classifyFile` 给 `bundle:true`，但**手上没有伴随成员**，且浏览器不允许读取同目录其他文件 | 行内**类型化说明**：「该格式需要完整包（主文件 + 同名目录及其全部数据文件）；请用『选择文件夹』或拖入整个文件夹」。**面板不列出具体缺哪个成员**——那要跑 `planBundle`，而它需要 `File[]`（跨目录取文件），与 §6.1「不新增本地句柄」的隐私口径相悖。文案列举「需要什么」用**静态**文案（`sniffMrxBundle` 的必需成员名规则：`{stem}.mrxs` + `{stem}/Slidedat.ini` + `{stem}/<INDEXFILE>` + 每个 `FILE_i`，`engine.js:1178` + `:1181-1188`），**不**预取实际文件列表 |
| **散文件** `.vms` | 同上（VMS 需入口 + 同目录全部 tile JPEG；tile 名只在 `.vms` 的 INI 里，见 `parseVmsMembers` 的 `ImageFile(c,r)`，`engine.js:1234-1246`） | 同上（静态文案：「`.vms` 入口 + 同目录全部 tile JPEG」）。注意 `sniffVmsBundle` 本身只对「缺 `.vms` 主入口 / 多个入口」给 `bad`，**不报缺 tile**（实测代码 `:1269-1280`）；真正逐 tile 核对的是 `planVmsBundle`（`engine.js:1340-1345` 的 `missing`）——这正是「判定归 planner」的理由 |
| `.vmu`（单文件或文件夹成员） | `classifyFile` 给 `cls:'temporary'` + `directClass:'legacy-direct'`（**不是** bundle） | 照 §4.2 **直传**（VMU 是暂时直传的 native 单文件，今天就能上传）；面板**不**因它进 planner 而收紧（理由见上方 note） |

**判定职责的唯一归属（评审 J2 的实质要求）**：「是不是一个完整包 / 缺哪个成员」
**只由运行器的 planner 判**（`prepareBundle` → `planBundle` → `sniffMrxBundle` /
`sniffVmsBundle` / `planVmsBundle`）。面板侧只做两件事：把文件夹的成员行原样
转交；在**拿不到文件夹**（散文件）时给静态说明 + 「选择文件夹」按钮
（复用 `#import-folder-input` 的 click，`app.js:12995-12998`）。面板**不得**自己
读目录、不得自己维护必需的 tile/成员清单——否则就是第二份判定，会与 planner 漂移
（今天 `app.js:11109` 的窄正则就是这个漂移的实例）。

**散文件的诚实结论**：**散落的 `.mrxs`/`.vms` 无法在面板内转换**——不可能在缺少
伴随文件的情况下完成转换。工具页今天的行为也是「类型化拒绝」（`planBundle` 在
复制前抛 `unsupported_input` + `missing`）。

**bundle 条目的声明**：转换产物同样是 `.tif`/`.ome.tif`，声明按 §4.3 从产物
字节嗅探（MRXS 明场 → `bf-*` → `ome-tiff` 或 `converter-bigtiff`；VMS 同）。

> 「面板不得比引擎更严」的具体含义：步 1/2 只决定**路由与当场拒绝**，
> 「能不能转换」的唯一裁决是步 3 的引擎嗅探（与 `runner._prepare`
> `runner.js:705-726` 用的是同一对函数）。历史上曾因面板窗口比核心窄而把可转换
> JPEG 判死（`engine.js:542 RASTER_SNIFF_WINDOW_BYTES` 的注释与
> `tests/js/tools-raster-input.test.ts` 就是这条回归）；本设计把它写成断言。

**引擎拒绝分支的后果（逐类）**：

- `direct_import` **开放**（`.tif/.tiff/.ndpi/.vms/.scn/.bif/.bmp/.jpg/.jpeg`）：
  不转换，按原样直传（`legacy-direct`），服务端 `open_slide` 终审。若终态是
  `convert_in_browser`（内容其实是 Aperio JPEG SVS 等），按 §7.2 回队转换一次；
  引擎既已判不支持，转换大概率也失败 → 停在 `convert_failed` + 申请支持
  （同 entry 只回队一次，见 §7.2）。
- `direct_import` **关闭**（`.svs`/`.kfb`/`.kfbf`/`.mrxs`）：**不上传**
  （服务端必 422 `convert_in_browser`，`app.py:3167`），行内直接说明
  `cap.reason` + 申请支持。与现状一致（今天服务端本来就拒），区别只是把拒绝
  从「创建任务时」提前到「本地、零网络」。

**步 3 与 `runner.probe` 的关系**：步 3 只决定**走不走转换**，它不产生任务、
不复制字节。真正的转换前提仍由 `probe`（`runner.js:418`）/`prepareBundle`
（`:444`）在复制前后各做一次（`_prepare` 的 `sniffTiffSlideCapability` /
`sniffRasterCapability` 与前置磁盘门）。两者是同一对函数，因此步 3 判 true 而
probe 判 false 只可能因为「同一文件的字节在读取之间变化」（浏览器里不可能），
不构成双重判定风险。

### 4.2 判定表（与引擎/服务端对应）

| 扩展名 / 变体 | `classifyFile` | 引擎步 3 | 面板动作 | `direct_class` | 服务端对应 |
| --- | --- | --- | --- | --- | --- |
| `.kfb` | `convert` | `magicSupported` 真 | **转换**（明场 → `bf-ome`/`bf-classic`） | 见 §4.3（产物侧） | 创建闸 `conversion` 分支 422 `convert_in_browser`（`app.py:12440`）；转换产物按**产物字节**声明（§4.3） |
| `.kfbf` | `convert` | 真 | **转换**（荧光 → `fl-ome`，保留画质） | 见 §4.3（产物侧） | 同上 |
| `.svs`（IFD0 压缩 7） | `convert` | `sniffTiffSlideCapability` 真 → `aperio-svs-jpeg` | **转换** | — | 直传关闭（`slide_format_registry.py:207`） |
| `.svs`（33003/33005） | `svsJp2k`（`temporary`） | （不适用） | **直接上传原文件** | `unconverted-variant:svs-jp2k` | 词表 ↔ 扩展名匹配 + 第 0 层压缩核验（`upload_direct_class.declaration_matches`，`.py:348`） |
| `.tif/.tiff`（OME-XML 描述） | `ome-tiff` | （不适用） | **直接上传** | `ome-tiff` | 描述必须含 OME-XML（`.py:371`） |
| `.tif/.tiff`（转换器来源标记） | `converter-bigtiff` | （不适用） | **直接上传** | `converter-bigtiff` | 描述 JSON `source_format ∈ CONVERTER_SOURCE_FORMATS`（`.py:238/_converter_marked`） |
| `.tif/.tiff`（通用瓦片 JPEG） | `convert` | `sniffTiffSlideCapability` 真 → `generic-tiled-jpeg-tiff` | **转换** | — | 直传仍开放（`direct_import=open`） |
| `.tif/.tiff`（条带/LZW/deflate/非 8 位/多通道） | `temporary` | （不适用） | **直接上传** | `legacy-direct` | `legacy-direct` 只对 `direct_upload` 集（`app.py:3174`） |
| `.tif/.tiff`（+ 引擎判不支持） | `convert` 但引擎拒 | `supported:false` | 不转换；`legacy-direct` 直传（服务端 open_slide 终审） | `legacy-direct` | worker：`enforcement_failure` 若内容是 Aperio JPEG SVS → `convert_in_browser` → §7.2 回队 |
| `.ndpi`（Make=Hamamatsu + 整层单条带 JPEG） | `convert` | `sniffTiffSlideCapability` 真 → `hamamatsu-ndpi-jpeg` | **转换** | — | 直传仍开放 |
| `.ndpi`（JP2K/分块/多通道） | `temporary` | （不适用） | **直接上传** | `legacy-direct` | — |
| `.scn`（JPEG + 明场） | `convert` | 真 → `leica-scn-jpeg` | **转换** | — | 直传仍开放 |
| `.scn`（荧光/非 JPEG） | `temporary` | 不适用 | **直接上传** | `legacy-direct` | — |
| `.bif`（BigTIFF + XMLPacket iScan + 压缩 7） | `convert` | 真 → `ventana-bif-jpeg` | **转换** | — | 直传仍开放 |
| `.bif`（JP2K/经典 TIFF/无 iScan） | `temporary` | 不适用 | **直接上传** | `legacy-direct` | — |
| `.vms`（入口 + 同目录 tile JPEG） | `convert`（`bundle:true`） | `prepareBundle` → `planBundle`（`engine.js:1353`） | **整包转换**（`stage-bundle`） | — | 直传仍开放（裸 `.vms` 也可直传，但面板选整包更稳） |
| `.mrxs` / `.dat`（整包） | `convert`（`bundle:true`） | `planBundle` → `mirax-bundle` | **整包转换** | — | 裸 `.mrxs` 不直传（`app.py:3115` 422 提示打包） |
| `.bmp`（未压缩 24/32 位） | `convert` | `sniffRasterCapability` 真 → `plain-image-bmp-jpeg` | **转换** | — | 直传仍开放 |
| `.bmp`（RLE/位域/调色板） | `temporary` | 不适用 | **直接上传** | `legacy-direct` | — |
| `.jpg/.jpeg`（三分量基线） | `convert` | 真 → `plain-image-bmp-jpeg` | **转换** | — | 直传仍开放 |
| `.jpg/.jpeg`（渐进/灰度） | `temporary` | 不适用 | **直接上传** | `legacy-direct` | — |
| `.vmu` / `.svslide` | `temporary` | 不适用 | **直接上传** | `legacy-direct` | 暂时直传 |
| `.zip` | `temporary` | 不适用 | **直接上传**（不携带声明） | `null` | 显式 `legacy-direct` 会被 422 `invalid_direct_class`（`tests/test_ingestion_api.py:230`） |
| 未登记扩展名 | `unsupported` | — | **当场拒绝** + 申请支持 | — | §7 |

`direct_class` 取值与扩展名的匹配由服务端 `_validate_direct_class`
（`app.py:3122`）权威裁定；面板只发它算出来的值，不自造。

**两点实测澄清**（§10.8 的判定表核对）：

- `classifyFile` 对 `cls==='convert'` 的条目一律回填 `directClass:'legacy-direct'`
  （`slide-sniff.js:821-824` 的收口分支）。**转换条目绝不使用该值**；表里的
  「—」就是这个意思。若把 `legacy-direct` 发给 `.svs`，服务端会 422
  `convert_in_browser`（`app.py:3167`）。转换条目的声明按 §4.3 从**产物字节**
  重新嗅探得出。
- `.svs`（JPEG）在 `classifyExt` 走 tiff 路由、最终由 `decideTiff`
  （`slide-sniff.js:264`）判 `convert`；`.svs`（33003/33005）判 `temporary`
  且 `directClass='unconverted-variant:svs-jp2k'`（`slide-sniff.js:256`）——
  与表一致。

### 4.3 转换后上传的 `direct_class`（第一轮评审 R1 修正，原设计有误）

**结论**：转换产物的声明**必须从产物字节嗅探得出**，不能假定为
`converter-bigtiff`；在 §4.4 选定的 `bf-ome`/`fl-ome` 默认下，正确值是
`ome-tiff`。

**证据链**（产物侧代码 + 服务端核验 + 实测）：

1. `bf-ome`/`fl-ome` 产物 IFD0 的 ImageDescription 是 **OME-XML**
   （`convert_bf.rs:487` 的 `ome_xml: Some(ome_rgb_description(...))`；
   `ome.rs:204` 写的 `<OME xmlns="http://www.openmicroscopy.org/Schemas/OME/2016-06" …>`；
   荧光 `convert_fl.rs:155` 同构），且未降采样层带该描述（`convert_gtiff.rs:96`
   `let desc = if m.reduced { None } else { ome_xml.take() }`，明场在
   `convert_bf.rs:57` 的 `BfWriter::Ome` 上同一写法）。
   `bf-classic` 产物才是 JSON 来源标记
   （`convert_bf.rs:487` `description_bytes(source_format, ...)`）。
2. 服务端核验是**按声明字符串分派**的（`upload_direct_class.declaration_matches`，
   `upload_direct_class.py:348`）：`ome-tiff` 要求描述含 OME-XML
   （`_looks_like_ome_xml`，`.py:255`）；`converter-bigtiff` 要求描述是带来源
   标记的 JSON（`_converter_marked`，`.py:238`）。worker 在 `open_slide`
   **之前**用它裁定，不符即 `_fail("convert_in_browser")`
   （`cos_ingest_worker.py:1054-1063`）。注意：`open_slide` 本身两者都能读，
   被拒的**唯一**原因是声明错配。
3. **实测**（命令见 §10.11）：用 `upload_direct_class` 对两枚构造夹具核对 ——

   | 产物描述 | `sniff_tiff_class` | `declaration_matches('ome-tiff')` | `declaration_matches('converter-bigtiff')` |
   | --- | --- | --- | --- |
   | OME-XML（`bf-ome`/`fl-ome`） | `ome-tiff` | **True** | **False** |
   | JSON 来源标记（`bf-classic`） | `converter-bigtiff` | False | **True** |

**设计修正（面板的产物声明算法）**：

```
// 转换完成、拿到 artifactView(jobId) 之后、创建 ingestion 之前：
const artifact = await runner.artifactView(jobId);        // runner.js:1386
const name = E.outputFileName(source.name, job);          // engine.js:1571
const cls  = await HP_SLIDE_SNIFF.classifyFile(artifact); // 同一份共享嗅探
// → bf-ome/fl-ome 得 'ome-tiff'；bf-classic 得 'converter-bigtiff'
const directClass = (cls.cls === 'ome-tiff') ? 'ome-tiff'
                  : (cls.cls === 'converter-bigtiff') ? 'converter-bigtiff'
                  : null;   // 其他（理论不可达）→ 不带声明，交给 open_slide + enforcement
```

- 声明只与 `.tif/.tiff` 名匹配（`app.py:3147/3154`）——`bf-ome`/`fl-ome` 是
  `.ome.tif`，服务端按末段 `tif` 判（`app.py:3140`），合法。
- 兜底：嗅探读失败/结果既非 OME 也非转换器标记 → **不带 `direct_class`**
  （`None`）：`declaration_matches(p, None) = True`、`enforcement_failure` 对
  无声明 + `.tif` 名只做 Aperio-SVS 检查（实测 `_converter_marked` 与
  OME-XML 描述都不命中），字节合法性仍由 `open_slide` 终审（§10.11 第 3 组）。
  **绝不**在嗅探不确定时硬发 `converter-bigtiff`。
- 若 §4.4 的默认改成 `bf-classic`（评审 R7），本算法自动得出
  `converter-bigtiff`——**这正是把它写成「从产物嗅探」而不是写死常量的原因**。

> 评审指出「照原设计实现则转换后上传主链路（含 §9.4 用例 A）必然失败」——
> **成立**，已按上述修正。原文档 §4.2 的 `.kfb` 行把产物声明写成
> `converter-bigtiff` 是错的。

### 4.4 输出格式 / 画质默认（不做成可选项）

面板内**不提供**输出格式与画质选择（避免在 300px 侧栏里塞单选组）；
固定为：

- 明场 → `outputProfile = E.OUTPUT_PROFILES.BF_OME`（`bf-ome`，`.ome.tif`）；
- 荧光（KFBF） → `FL_OME`（`fl-ome`，`.ome.tif`）；
- `encodingProfile = E.ENCODING_PROFILES.PRESERVE`（`preserve-source-v1`，无损）。

理由：与工具页默认一致（`templates/tools_slides.html:161` 的 `bf-ome` 选中），
且 `fl-ome`/`preserve` 是荧光唯一合法组合（`engine.js:1549/1523`）；产物名用
`E.outputFileName(source.name, job)`（`engine.js:1571`）。

> 与用户描述「preserve-source-v1 无损重封装，明场 bf-classic、荧光 fl-ome」的
> 差异：用户给的明场默认是 `bf-classic`。二者都能被平台读取（`viewable_formats`
> 三个格式全在内，`app.py:3619`）。本设计**取 `bf-ome`**，与工具页默认一致、
> 且 OME 带 mpp 元数据；如果 owner 要 `bf-classic`，改一个常量即可（记为待确认项）。

## 5. 并发与锁

| 资源 | 机制 | 现状/新增 |
| --- | --- | --- |
| 重型转换（WASM + OPFS） | Web Lock `slide-transform:heavy`（`runner.js:76`，`_acquireHeavyLock` `:380`） | **沿用**：`probe`/`prepareBundle`/`startJob`/`resumeJob` 内部自持锁（`:419/:445/:795/:808`）。面板因此天然与 `/tools/slides` 页互斥；撞锁时运行器抛 `job_locked_other_tab`（`engine.js:18`） |
| 面板内转换并发 | 面板自己的 FIFO：至多 1 个 entry 处于 `converting` | **新增**（避免同页多次撞锁、避免排队期无限重试） |
| 跨标签上传并发 | Web Lock `pt:upload-slot:<account>` | **新增**（用户确认的口径） |
| 同文件内容身份 | `HP_COS_UPLOAD.contentLockName(account,{name,size,lastModified})` + `acquireContentLock`（`cos-uploader.js:1203/1215`） | **沿用**（双标签同一文件各建 ingestion 的既有防护） |
| 单任务产物读取 | `engine.uploadLockName(jobId)`（`engine.js:38`） | **必须由面板上传期间持有**（评审 R3 修正，见下） |

**`engine.uploadLockName(jobId)` 必须被面板的上传阶段持有**（评审 R3 修正）：

现状的互斥链是：工具页 `startOrContinue` 在上传前后持
`E.uploadLockName(jobId)`（`tools-slides-upload.js:647-649` 的
`navigator.locks.request(E.uploadLockName(jobId), {ifAvailable:true}, …)`），
而 `runner.discardJob`（`runner.js:659-673`）**只在拿不到同一把锁、或 upload
记录活跃且未 `abandonUpload` 时才拒删**——它保护的是 `uploadLockName`，
**不认识**新加的 `pt:upload-slot:<account>`。

面板创建的任务会出现在工具页任务列表里且可被操作：`listJobs()`
（`runner.js:1355-1372`）枚举 OPFS `slide-jobs/` 全部目录，`renderJobs`
（`tools-slides.js:1533`）**不按 `intent.channel` 过滤**，工具页「删除」按钮
直接走 `discardJob`（`tools-slides.js:1654-1667`）。因此若面板只依赖新槽锁：

- 用户在另一标签的工具页点「删除」→ 若面板的 `record.upload.state` 还没写成
  活跃态（例如刚进 `uploading` 但记录尚未落盘），`discardJob` 会在**无锁**下
  删掉 `slide-jobs/<jobId>/`（含唯一源副本与产物），面板上传随即失败且产物
  不可恢复；
- 或用户在工具页对同一任务点「上传」（列表里的 `nextAction==='export'` 行）
  → 工具页**另建一个 ingestion** 重复上传同一产物，容量记账与
  `slide-jobs` 记录互相覆盖（工具页与面板各写 `record.upload`）。

**设计修正**：面板在**整个上传阶段**（`upload_queued` → 终态）持两把锁：

1. `pt:upload-slot:<account>`（跨标签「1 个上传」的调度槽，本设计新增）；
2. `E.uploadLockName(jobId)`（同任务产物的读写互斥，既有语义）——
   `navigator.locks.request(engine.uploadLockName(jobId), {ifAvailable:true}, …)`，
   拿不到即在行内说明「该任务正在另一处上传/被操作」，**不并发**。

这样：工具页 `discardJob` 会因拿不到锁而拒删（`runner.js:665` 的
`!lock` 分支 → `upload_active` + `lockHeld:false`，工具页还会再确认一次
`abandonUpload`，`tools-slides.js:1661-1664`）；工具页的
`startOrContinue` 也会因拿不到锁回 `tools.upload.other.tab`
（`tools-slides-upload.js:649-653`）。上传收口后两把锁都释放。

> 注：这条也意味着面板与工具页可**同时存在**于两个标签（用户偏好），互相
> 串行而不是互相破坏——本设计不去「删掉」工具页的入口。

`pt:upload-slot:<account>` 语义：

- `navigator.locks.request('pt:upload-slot:' + account, { ifAvailable: true }, …)`
  （与 `engine.uploadLockName` 同一原语）；拿不到 → 行显示「另一标签页正在上传」
  （`upload.cos.other.tab` 与工具页 `tools.upload.other.tab` 同义），**排队等待**
  而非失败（用 `ifAvailable:false` + `AbortController` 取消，写法照
  `acquireContentLock`）；
- 上传收口（成功/失败/取消）立即释放，下一 entry 顶上；
- `navigator.locks` 不可用（旧浏览器/非安全上下文）→ `held:false` 照常执行
  （与既有 `acquireContentLock` 的退化语义一致，绝不阻塞）。

**`<account>` 的取值来源（第四轮复审 A6 修正）**：工作台 bootstrap 的
`cos_upload` 载荷**没有** `account` 字段（实测键集 = `available` /
`browser_convert` / `direct_upload` / `formats` / `manual_only`；带 `account`
的是工具页端点 `/api/tools/slides/upload-capability`，它不走 bootstrap），而本设计
又承诺加载期零网络请求，所以**唯一可用来源是页面既有的 `currentUserId`**
（`app.js:275`，由 `/api/auth/info` 填充；既有代码已在同一语义下使用它——
`uploadFileCos` 的 `storage.account` 与 `contentLockName`，`app.js:10757`）。
口径统一为：

```
const acct = String(window.currentUserId || "");   // 免认证/未就绪 = 空串
lockName = "pt:upload-slot:" + acct;
```

即与 `cos-uploader.js:1203 contentLockName(account, …)` 同一取值、同一退化语义
（空串 = 「同一（唯一）账号」）。**不得**为此新增网络请求或读 bootstrap 的
`account`（不存在）；`currentUserId` 未就绪时按空串处理（此时也还没有任何
entry 能进入 `uploading`——上传必然晚于 `/api/auth/info`，且即便抢先，
`/api/ingestions` 会以 401/403 暴露授权问题）。

**ingestion 创建时机**：只有拿到上传槽 + 本条目适用的门禁全部通过后才
POST `/api/ingestions`（`cos-uploader.js:1033`）。转换期间（含刷新、崩溃）
服务端零任务。

### 5.1 门禁作用域（第一轮评审 R2 修正）

`viewable_formats` 是**按核心 `result.format` 字符串键控**的门禁
（`tools-slides-upload.js:783-789`），它的语义是「**本机转换器的这个产物**平台
能不能查看」。直传条目没有 `result.format`（没有本机产物），把它套上去会把
**全部直传条目**判成「平台暂不支持查看」而拒绝——与 §4.2 逐行要求的
「直接上传」直接矛盾，而且比现有工作台直传更严（`cosUploadEligible`，
`app.py`/`app.js:10404` 只看词表 + 上限）。

| 条目类型 | 能力（`cos_upload.available`） | 账号 | 大小上限 | `viewable_formats` |
| --- | --- | --- | --- | --- |
| **转换产物**（kfb/kfbf/svs-jpeg/ndpi/scn/bif/vms 整包/mrxs 整包/gtiff/bmp/jpg） | 要求 | 要求 | 按**最终产物** `result.outputBytes` ↔ `max_size_bytes`（`tools-slides-upload.js:778`） | **要求**（`job.result.format` ∈ 载荷的 `viewable_formats`） |
| **直传**（OME-TIFF / 转换器 BigTIFF / SVS-JP2K / 普通 TIFF / NDPI 变体 / SCN 变体 / BIF 变体 / VMS 原样 / VMU / SVSlide / BMP-JPEG 变体 / zip） | 要求 | 要求 | 按**源文件大小** ↔ `max_size_bytes`（与现状 `cosUploadEligible`、`app.js:10411` 同口径） | **不适用**——不参与判定；平台可查看性由服务端 `open_slide` 终审（失败则终态 `validation_failed`，按 §7 给出原因） |

即：直传条目的门禁 = 现状（能力 + 账号 + 源大小 + 词表），**一行都不加严**；
只有本机转换出来的产物才吃 `viewable_formats`，因为只有它存在「产物格式」
这个新维度。审计线索：`viewable_formats` 现仅出现在
`tools-slides-upload.js:783` 一处，`tools-slides-direct-upload.js` 的直传
路径（:320/:332）只看 `max_size_bytes` 与 `cfg.formats`——本设计与之对齐。

## 6. 刷新 / 续转

### 6.1 持久化面

（表中 `record.upload` 与「继续上传」的续传语义见 §6.4。）

| 数据 | 存放 | 依据 |
| --- | --- | --- |
| 转换任务（源副本、日志、产物） | OPFS `slide-jobs/<jobId>/` | 运行器既有 |
| 上传意图（授权账号 + 目标 + 渠道） | OPFS `record.intent`（`runner.setJobIntent`，`runner.js:634`） | R1 既有；本设计写 `channel: 'workbench-inline'` |
| 上传记录（ingestion id / 已确认分块 / 分片摘要 / 账号 / slide_id） | OPFS `record.upload`（`runner.setJobUpload`，`runner.js:611`） | 既有；见 §6.4 |
| 直传任务的续传记录 | localStorage `pt.cos.jobs`（`app.js:10383`） | 既有 |
| 目标快照 | localStorage `pt.fb.upassoc.v1:<uid>`（`app.js:4028`） | 既有 |

**不新增任何 localStorage 键**用于转换恢复：恢复的唯一权威是 OPFS 任务记录
（含 `intent` / `upload`），这与「绝不凭名称/大小把浏览器文件当同一份」的隐私
约定一致（`app.js:10404` 附近注释）。

### 6.2 刷新后的行为（沿用现有隐私约定：不自动开始）

1. 页面加载：`HP_INLINE_CONVERT` **不做任何面板发起的新网络请求、不自动开始
   转换或上传**（口径见 §9.2 第 9 项：页面自身的 5 个 GET 不计）。
2. `runner.listJobs()`（`runner.js:1355`）→ 取 `intent.channel ===
   'workbench-inline'` 的**全部**记录（第四轮复审 A2 修正：**不过滤掉已收口
   记录**），分成两组渲染：
   - **待继续组**：`state ∈ {prepared, planned, paused, failed}`，或
     `state ∈ {ready, exported} && upload 未收口` → 行 + 恢复动作（第 3 步）；
   - **待清理组**：其余（含 `record.upload.state === 'published'`，即已完成 /
     已发布 / 终态 / 已取消）→ 行 + 「删除本地产物」（§6.3）。
   原设计把过滤条件写成「已收口的不进恢复行」，与 §6.3 要求在同一批
   `listJobs()` 结果上给这些记录提供删除按钮**直接矛盾**——那个按钮在刷新后
   唯一可达的 OPFS 清理入口会变成不可达，源副本与产物永久残留（且
   `_sweepIncompleteStaging` 显式 `if (rec && rec.upload) continue;`，
   `runner.js:361-366`，不会兜底）。
3. 待继续组的行显示「未完成的转换：<名> · 已提交 X / 共 Y」+ 按钮（§6.3 的
   完整按钮表）：
   - `nextAction==='resume'`（有 journal）→ 「继续转换」（`resumeJob`，不重选文件）；
   - `nextAction==='start'`（prepared，无 journal）→ 「继续转换」
     （`startJob(null, {jobId})`——源副本已在 OPFS，无需 File）；
   - `ready/exported` → 「继续上传」（跳过转换，直接进 `upload_queued`）。
   映射取自 `runner.js:1310-1317` 的 `nextAction` 计算。
4. `resumeJob` 的断点续转不需要重选文件：源副本是 OPFS 里的 `source.bin`，
   运行器 `_startOrResume` 会 `verify-source`（`runner.js:952`）——源变了就
   `source_changed_refuse_resume`。bundle 走 `verify-bundle`
   （`engine.expectedBundleIdentity`，`engine.js:1425`）。
5. 版本/设置不符的续跑拒绝（`resume_refused` 的 `kind`：`core-version` /
   `profile` / `policy` / `output-profile` / `encoding-profile` /
   `channel-json` / `source-adapter`，`runner.js:886-1027`）→ 行内说明 +
   **「重新开始转换（需重新选择文件）」**，**绝不静默改设置续跑**（理由见 §6.3）。
6. `job_dir_missing`（目录被浏览器驱逐，`engine.js:19`）→ 行内说明 +
   「重新选择文件」（与第 5 点同一处置：**丢弃 + 重选**）。
7. `beforeunload`：只要有 entry 处于**「未收口且本页持有用户的选择」**的状态，
   注册 `preventDefault()` 提醒（照 `tools-slides.js:1772` 的写法）。集合
   （**按语义判定，不按状态名枚举**）：`converting`、**`need_disk_confirm`**、
   `convert_queued`、`upload_queued`、`uploading`、`requeue_convert`。写成语义
   判据而不是枚举状态名，正是为了避免再犯评审 J3 的按状态名枚举漏项。

   > `need_disk_confirm` 为何要在内（**理由已按本轮评审 J1 修正**）：它既不属于
   > `converting` 也不属于 `uploading`，按状态名枚举会漏；但原稿写的理由是
   > **错的**——原稿说「源副本已经写进 OPFS 且是唯一一份」，而运行器在任一
   > `disk_precheck_failed` 抛出**之前**就已把 job 目录删掉了：
   > - 前置门（`runner.js:732`，`_prepare`；`:463`，`prepareBundle`）在
   >   `_writeJobRecord(…, {state:'staging'})`（`:734`/`:465`）**之前**——
   >   此刻 `slide-jobs/<jobId>/` 根本还没创建（目录由 `_writeJobRecordNow`
   >   → `_jobDir(jobId, true)` 建，`runner.js:285-288`/`:250-253`）；
   > - 后置门（`:755`/`:496`）在 `try` 内，其 `catch` 立即
   >   `await this._discardNow(jobId)` 再 `throw`（`:759`/`:534`），而
   >   `_discardNow`（`runner.js:674-686`）就是 `E.removeEntryRecursive(jobs, jobId)`（`:682`）。
   >
   > 所以此刻 **OPFS 里没有可丢的副本**。提醒的正确理由是：**用户的选择只在
   > 本页内存里**（`entry.file` / `entry.bundle.rows`），刷新后没有 File 句柄
   > （§6.1 不新增本地句柄），要重新选择并**重新复制**（大切片是分钟级 I/O，
   > 见 §6.3 的 5 GiB 例）；对已经在等的用户来说这是实打实的返工。

### 6.2.1 磁盘门失败后的重试语义（第三轮评审 K1 修正）

`disk_precheck_failed`（含 `uncertain:true`）**不是**「就地确认继续」：

- 运行器已丢弃 job（见上），**必须带着原始输入重新起一次**
  `probe(file, {confirmUncertainDisk:true, …})` /
  `prepareBundle(rows, {confirmUncertainDisk:true, …})`——这会分配**新 jobId、
  重新复制**（`_prepare` 的 `opts.jobId || E.newJobId()`，`runner.js:728-729`）。
  这与工具页的既有正确形态逐条一致（`tools-slides.js:298-322` 的
  `probeWithDiskFlow` 明确把 `file` 再传一次；`:564-578` 的
  `prepareBundleWithDiskFlow` 把 `rows` 再传一次）。
- 因此面板**必须在内存里保留** `entry.file` / `entry.bundle.rows` 直到收口
  （这不是持久化，不违反 §6.1：刷新后它们就是没了，所以才要
  `beforeunload`）。
- **不得**走 `startJob(null, {jobId})` / 复用旧 job 目录的路线——那会复现上一轮
  评审 R4 的失败模式（`io_recoverable: 缺少输入文件`，`runner.js:867`）。

### 6.3 终态 / 恢复态按钮表与清理（第一轮评审 R4 + R5 修正）

刷新后**没有原始 `File`**（§6.1 明确不新增本地句柄，隐私约定不自动开始），
这一点决定了哪些按钮可实现。运行器侧的两条硬约束：

- 已有 journal 的非 prepared 记录**只能用 `resumeJob`**：`startJob(null,{jobId})`
  会抛 `resume_refused ... kind='use-resume'`（`runner.js:854-858`）；
- 「重新 `probe`」需要 `file`：`_startOrResume` 在 `!reusable` 且非 bundle 时
  `if (!file) throw io_recoverable: 缺少输入文件`（`runner.js:861-869`）。
  bundle 记录是例外（`reusable = true`，源就是 OPFS 里的成员）。

因此**原设计 §6.2 第 5 点「丢弃旧任务 + 重新 probe」在刷新路径上不可实现**
（评审 R4 成立）：丢弃后没有 File，再调运行器只会再抛一次
`io_recoverable: 缺少输入文件`，把用户引向第二次失败。修正后的按钮表：

| entry 状态 | 行按钮 | 动作 | 可实现性依据 |
| --- | --- | --- | --- |
| `planned/paused` + 有 journal | **继续转换** | `resumeJob(jobId)` | `runner.js:807` |
| `prepared`（无 journal） | **继续转换** | `startJob(null, {jobId})`（`reusable` 为真，不需要 File） | `runner.js:851-853`、`:874-880` |
| `planned/paused` 无 journal | **继续转换** | 同上（`startJob(null,{jobId})`） | 同上 |
| `ready/exported`（upload 未收口） | **继续上传** | 直接进 `upload_queued`（§6.4） | `runner.js:1386` |
| bundle 记录（任意非终态） | **继续转换** | `startJob(null,{jobId})` 或 `resumeJob`（成员已在 OPFS） | `runner.js:862-864` |
| `resume_refused`（版本/设置不符） | **重新开始转换** + **重新选择文件** | 提示「请重新选择同一文件」→ 用户重选后走全新 `probe`；旧任务由清理入口删除 | `runner.js:861-869` 的 `!file` 分支 |
| `job_dir_missing` | **重新选择文件** | 同上 | `engine.js:19` |
| 任一终态（`done`/`convert_failed`/`upload_failed`/`cancelled`/`rejected`） | **删除本地产物**（+ 按 §7 的申请支持） | `discardJob(jobId)`，失败按 `upload_active` 的 `lockHeld:false` 分支再确认 `abandonUpload` | `runner.js:659`；工具页同一处置 `tools-slides.js:1654-1667` |

**本地产物清理（评审 R5 修正）**：原设计缺这条，会导致 OPFS 里的
`source.bin` + `output.tif` 在收口后**永远残留**——`_sweepIncompleteStaging`
只收 `!rec || rec.state === 'staging'` 且显式跳过 `rec.upload` 存在的记录
（`runner.js:361-366`），ready/published 任务永不被自动清理；工作台又没有工具页
那样的任务列表，大切片（设计自述 5 GiB）转几次就撞
`quota_exceeded_recoverable`。修正：

- **每个终态行都提供「删除本地产物」**（`discardJob`），并显示它释放的字节数
  （`getJob` 的 `source.size` + `result.outputBytes`）；
- `done`（已发布）行的删除**只删本地副本**，不影响已发布切片——文案必须说清
  「已上传到平台的切片不受影响」，避免用户以为会删库里的；
- 面板启动时（§6.2 第 2 步的同一批 `listJobs()` 结果）对 `intent.channel ===
  'workbench-inline'` 的**已收口**记录（第四轮复审 A2 修正的「待清理组」）
  **不自动删**（删除是不可逆动作，且 `record.upload` 是到服务端 ingestion 的
  唯一线索），只在行上给按钮——**这批记录必须有行**，否则清理入口在刷新后
  不可达（这正是 A2 指出的矛盾）；
- 待清理组的行是**最小形态**：无进度条、无可继续动作，只有
  「<名> · 已上传/已失败/已取消 · 本地产物 <释放字节数>」+ **删除本地产物**；
- 行被用户忽略/浏览器关闭后残留的，由既有 `_processPendingCleanups` /
  `_sweepIncompleteStaging`（运行器启动期）与工作台下次续转/删除入口兜底——
  本设计不新增自动删除策略。

### 6.4 「继续上传」的续传语义（第一轮评审 R7 修正）

原设计 §6.1 的持久化表只列了 `intent`，没说清「继续上传」是**续传同一个
ingestion** 还是**重新创建**。修正：

| 数据 | 存放 | 依据 |
| --- | --- | --- |
| 转换任务（源副本、日志、产物） | OPFS `slide-jobs/<jobId>/` | 运行器既有 |
| 上传意图（授权账号 + 目标 + 渠道） | OPFS `record.intent`（`runner.setJobIntent`，`runner.js:634`） | R1 既有；本设计写 `channel: 'workbench-inline'` |
| **上传记录**（ingestion id / 已确认分块 / 分片摘要 / 账号 / slide_id / 终态） | OPFS `record.upload`（`runner.setJobUpload`，`runner.js:611`） | 既有；面板照 `tools-slides-upload.js:330-370 opfsStorage` 的适配器写法（`save`/`complete`/`remove`/`readConfirmed`/`readRecord`） |
| 直传任务的续传记录 | localStorage `pt.cos.jobs`（`app.js:10383`） | 既有 |
| 目标快照 | localStorage `pt.fb.upassoc.v1:<uid>`（`app.js:4028`） | 既有 |

「继续上传」（`ready/exported` 行）的语义与工具页**逐条一致**
（`tools-slides-upload.js:667 runLocked` 的 ④）：

- 读 `record.upload`：有 `ingestionId` 且 `state ∉ UPLOAD_ACTIVE_STATES 的终态集`
  （`engine.js:31`，终态 = `published`/`failed`/`cancelled`）→
  以 `resumeJobId = ingestionId` 传 `createUpload`：引擎走 `prepareResume`
  （`cos-uploader.js:1075`），读回已有确认分块 + 分片摘要，**逐片核验字节**后
  跳过已传分块（`verifyResumeDigests`，`cos-uploader.js:591`）；
- 记录是终态 / 不存在 → **不自动重建**：行内给「重新上传」按钮，点击才建新
  ingestion（否则刷新一次就多一个服务端任务，违反 §0 不变量 2，也重复占容量）；
- 无 `record.upload` 且服务端可能仍有孤儿任务（`save` 落盘失败的情形）→
  照工具页的 `settleOrphan` 语义先确认取消再新建（`tools-slides-upload.js:472`）。

`§9.2` 第 8 项据此补强：断言必须区分「续传同一 ingestion」（`readRecord` 非空
→ `resumeJobId` 等于记录里的 id、且**零** `POST /api/ingestions`）与
「记录为空 → 显示按钮但不请求」。

## 7. 失败与「申请支持」

### 7.1 行内说明 + 申请支持按钮的触发条件

| 触发 | `format_ext`（预填） | `message`（预填，含机器码） |
| --- | --- | --- |
| `unsupported`（未登记扩展名） | `extOf(name)` 去点 | `detected=unsupported; ext=<ext>; code=unsupported_ext` |
| `magic_mismatch`（内容与扩展名不符） | 实际检测类型的建议扩展？ → **仍填原扩展名** | `detected=<png/webp/gif/heic/…>; declared=<.jpg>; code=content_ext_mismatch` |
| 引擎 `supported:false` 且 `direct_import` 关闭（`.svs`/`.kfb`/`.kfbf`/`.mrxs`） | 原扩展名 | `engine_reason=<cap.reason>; kind=<cap.kind>; code=engine_unsupported_variant` |
| 服务端终态 `convert_in_browser`（识别漏判，§7.2） | 原扩展名 | `fail_code=convert_in_browser; fail_detail=<…>` |
| 任一终态 `validation_failed`（含 `raster_format_mismatch`） | 原扩展名 | `fail_code=validation_failed; fail_detail=<…>` |

按钮复用 `POST /api/format-requests`（`app.py:12092` →
`format_request_http.handle_submit`，`format_request_http.py:118`）：

- `format_ext` ≤ 40 字符、`message` ≤ 2000 字符、`contact` 可选（邮箱正则
  `format_request_http.py:37`）、样本可选且 ≤ `FORMAT_REQUEST_MAX_SAMPLE_BYTES`
  （默认 64 MiB，`app.py:12063`）。
  前端上限的取值链有一个**现状缺口**：`app.js:11450` 会读
  `body.max_sample_bytes`，但 `GET /api/slide-formats`（`app.py:12126`）返回的是
  `public_catalog()` 的**裸列表**，没有该字段——所以
  `importDrawerState.maxSampleBytes` 实际一直是模板初值 64 MiB
  （`app.js:11179` 附近的默认值）。本设计**不改**这条链路（申请支持用与抽屉
  相同的默认值），只把它记为既有事实，避免把「服务端会告诉我上限」写成前提；
- 202 → 在行内显示申请编号 + 业务状态（沿用
  `renderFormatRequestResult`，`app.js:11478` 的语义：**只说已登记，不说已兼容**）；
- 413 → 「样本超过上限」（`fr.sample.too.large`）就地提示，草稿保留；
- 429 → 「今日已达上限」（`format_request_http.py:159`，默认每日 10 条
  `app.py:12066`）——按用户确认的文案处理；
- 401/403 → 「无上传权限」提示，不跳转（工作台 `apiFetch` 的 401 语义见
  `app.js:205`：`auth_required` 会跳登录；申请支持走 `apiFetchNoRedirect`
  `app.js:241` 以免在换语言/上传中途把页面导航走）。

样本文件：默认**不带**（面板已经握有原文件，用户可自行勾选「附带小样本」）；
勾选时若样本 > 上限，前端先行拒绝并提示（照 `app.js:11535`）。

### 7.2 服务端返回 `convert_in_browser` → 自动回队

两类场景，**处置不同**（都由 §10.9 的实测确认）：

**(a) 改名但引擎能转**：`.tif` 名的 Aperio JPEG SVS 且 **tiled** 布局。
`classifyFile` → `convert`（`.tif` 走 tiff 路由，`decideTiff` 的
`compression===7 && genericTiledConvertible` 命中），引擎
`sniffTiffSlideCapability` → `supported:true, adapter='aperio-svs-jpeg'`。
**面板直接转换**，根本不进直传——这是「面板比旧路径更早识别」的实例。

**(b) 改名且引擎不能转**：`.tif` 名的 **条带（strip）** Aperio JPEG SVS。
实测：`classifyFile` → `temporary`（非 tiled，不满足可转换判定）→ 直传
（`legacy-direct`）→ worker `enforcement_failure` 判
`convert_in_browser`（实测：`sniff_tiff_class=tiff-other`、
`is_aperio_svs_jpeg=True`、`enforcement_failure(legacy-direct,'tif')='convert_in_browser'`）。
此时：entry `state → requeue_convert` → 重新走 §4 步 3 → 引擎仍
`supported:false`（`主图不是分块（tiled）存储：该 TIFF 变体不在支持集`）→
**停在 `convert_failed`** + 申请支持（带 `engine_reason`）。**同一 entry 最多
自动回队 1 次**，防「直传 → 拒绝 → 转换 → 再直传」的死循环。

两条路径的检测点：

- 实时路径：引擎在 `createNewJob` 对非 2xx 抛 `{status:422, data:{code, tools_url}}`
  （`cos-uploader.js:1061`）；面板据此判 `data.code === 'convert_in_browser'`。
- worker 路径：状态轮询到 `terminal` + `fail_code==='convert_in_browser'`
  （`app.js:10537 cosErrorMessage` 已映射该码）。
- 回队时清掉 ingestion 记录（`cosJobRemove`，`app.js:10472` 附近）与那条
  `direct_class` 判定，重新走 §4 步 3。

### 7.3 转换失败的其它分支

| 错误码（`engine.ERROR_CODES`，`engine.js:11`） | 行内文案 | 可用动作 |
| --- | --- | --- |
| `disk_precheck_failed` + `uncertain:true` | 「浏览器只报告了 ~X 可用，本任务预计需要 ~Y」 | **「继续（我已确认）」/「取消」**——确认后**带原始输入重新起**（`probe(entry.file, {confirmUncertainDisk:true})` / `prepareBundle(entry.bundle.rows, {confirmUncertainDisk:true})`，会分配新 jobId 并**重新复制**；`runner.js:688-699` 的门语义 + §6.2.1）。**不得**复用旧 job 目录（它已被运行器删掉） |
| `disk_precheck_failed` + `uncertain:false` | 「空间不足」 | 取消（换更小文件） |
| `quota_exceeded_recoverable` | 「浏览器存储配额不足」 | 「改用转换器页转换并本地保存」链接 + 取消（用户确认口径） |
| `unsupported_input` | 引擎 `cap.reason` 原文 | 申请支持（`direct_import` 开放时先提示「已按原样上传」可选） |
| `resource_profile_insufficient` | 「当前资源档位内存不足」 | 自动以更低档位重试一次（`saver`），仍失败 → 取消 |
| `output_too_large` / `resume_refused` / `io_recoverable` / `job_locked_other_tab` / `job_dir_missing` | 各自文案 | 重试 / 重新开始 / 重新选文件 |

### 7.4 上传失败的既有映射（不改）

`upload.cos.err.*` 一族（`app.js:10537 cosErrorMessage`、`:10578 cosFailStageKey`）
原样复用，含 `client_gone` / `client_idle_timeout` / `raster_format_mismatch` /
`upload_too_large` / `cos_pool_below_product_limit` / `cos_waiting_limit` /
`cos_sign_rate_limited`。实时与刷新恢复**共用同一映射**（既有回归
`tests/js/cos-upload-liveness.test.ts`）。

## 8. 开关与回退

`bootstrap.capabilities.cos_upload.inline_convert`：

```jsonc
"inline_convert": {
  "enabled": true,                          // 默认开
  "url": "/tools/slides",                   // 关闭时的回退目的地（与 browser_convert.url 同值）
  "viewable_formats": [                     // 新增：唯一来源 SLIDE_TOOLS_VIEWABLE_OUTPUT_FORMATS（app.py:3619）
    "classic-bigtiff-jpeg-pyramid",
    "ome-bigtiff-subifd-rgb-jpeg-pyramid",
    "ome-bigtiff-subifd-multichannel-jpeg-passthrough"
  ]
}
```

- 默认开：`_cos_upload_capability_payload`（`app.py:3181`）**无论 `available`
  与否**都下发该键（与 `browser_convert`/`direct_upload` 同一惯例——
  `available:false` 时前端也要能显示明确原因）。
- 关闭（`inline_convert.enabled=false`）→ `uploadFileClassified` 走**现有**
  `offerBrowserConvert`（`app.js:10326`）→ `openConvertHandoff`（`:10247`）
  弹窗交接；bundle 走 `handoffBundleFolder`（§4.1.1/§2.2⑩b）。**控制流逐条
  保留**（零删除）。

  > **第四轮复审 A5 修正——「逐字节不变」的措辞收紧为「控制流不变」**：本设计
  > **确实**要改两条文案，而它们恰恰只在回退/服务端拒绝路径上显示：
  > `upload.cos.err.convert_in_browser`（`i18n.js:1025`/`2686`）的唯一使用点是
  > `app.js:10545` 的 `cosErrorMessage`（服务端 422 `convert_in_browser` 时），
  > `upload.err.unsupported`（`i18n.js:1026`/`2687`）的唯一使用点是
  > `app.js:10148` 的 `unsupported` 分支——**开关关闭时也是这一分支**。
  > 所以「零改写」是同一条设计里自相矛盾的承诺（原 §2.2/§8 的 R8 自己要求改写）。
  > 收紧后的承诺：**控制流与行为不变，文案改写见下**。
  >
  > **改写后的文案必须对两种开关状态都成立**：关闭时用户**没有**面板内转换，
  > 文案要指向可操作的入口（工具页）；开启时（服务端仍可能因漏判返回 422）
  > 面板已自动处理，文案不该再让用户去别处。因此：
  > - `upload.err.unsupported`（纯本地、`cls==='unsupported'`，与开关无关）：
  >   改写为**说明 + 「申请支持该格式」引导**（§7.1 的按钮已在同一行），
  >   **不再点名 `/tools/slides`**——这个分支开启时也不走工具页。
  > - `upload.cos.err.convert_in_browser`（服务端码，开启时面板自动回队，
  >   关闭时必须引导用户）：改写为**中性事实句 + 条件性入口**，即
  >   「该格式需先在本机转换后上传」+（仅 `enabled===false` 时）由前端在文案
  >   后追加 `/tools/slides` 链接（`tt()` 支持形参；链接按 `inline_convert.url`
  >   构造）。**开启时不给链接**（面板正在自动处理，给链接反而误导）。
  > 这条要求进 §9.2 第 14 项（i18n 断言按开关两态分别校验）。
- 回退判定的唯一读点：`HP_INLINE_CONVERT.enabled(caps)`（纯函数）——不许在
  其它地方再读一次，避免半开半关。
- `viewable_formats` 缺失（旧后端/缓存 JS）→ 与工具页同一 fail-closed 语义：
  禁用自动上传，行内提示「平台暂不支持查看该产物」+「本地保存」指引
  （`tools-slides-upload.js:783` 同义）。**绝不**因为字段缺失就当通过。

## 9. 对旧测试的影响与新测试清单

### 9.1 对旧测试的影响（逐条核实过）

| 测试 | 影响 | 处置 |
| --- | --- | --- |
| `tests/js/workbench-direct-class.test.ts` | 「KFB → 零请求 + 行内给『在本机转换并上传』入口」（:210-221）在开关**默认开**时不再成立（KFB 直接进转换队列） | 改：开关注入 `inline_convert.enabled=false` 时保持原断言（回退路径）；新增 enabled=true 时「KFB → 转换队列 + 零 `/api/ingestions`」 |
| 同上，MRXS bundle 交接测试（:245-275） | 同上（仅开关关闭时 reachable） | 同上处理 |
| `tests/js/cos-upload.test.ts:421` | 「mrxs 行是『待本机转换』提示」——默认开时 mrxs 进转换队列（仍是「待本机转换」语义，文案键变） | 断言改到新文案键；或在用例里注入 enabled=false 保持原路径 |
| `tests/js/r1-convert-upload.test.ts`（48 项，工具页 + 工作台入口） | 工具页侧全部不受影响；工作台入口侧（:293 `href === '/tools/slides'`）仅在开关关闭时成立 | 给工作台入口用例注入 enabled=false；保留 i18n 静态扫描（:333-370）并扩展到新键 |
| `tests/js/viewer-folders.test.ts`（第三轮评审 K2 补入；第四轮 A1 加码） | 对 `app.js` 做**源码级断言** `expect(appSrc).toContain("opts.assocTarget = importTargetSnapshot()")`（`:1095`），并驱动 `importDrawer.associate` / `snapshot` 的行为面 | **必须保持**：§2.2① 的 `uploadFile` 改写要在**调用 `enqueue` 之前**仍先取快照，断言不变；**新增一条「`uploadFileCos` 调用字面量含 `assocTarget`」的源码级断言**（`:10164-10170`，第四轮 A1——现有断言只钉住快照被**取**，没钉住被**传**）；再加「转换条目完成时按 `entry.target` 关联」的行为断言（§9.2 第 19 项） |
| `tests/js/cos-upload-liveness.test.ts` | 魔数 fail-fast（PNG 改名 .jpg）在面板内**同样**生效且零网络——语义不变 | 无需改；新增面板内该路径的行文案断言 |
| `tests/js/raster-image-compat.test.ts` | 只测 `acceptFromCatalog` / fallback 目录；不涉上传分流 | 不影响 |
| `tests/js/tools-u2-page.test.ts`（`bundleRoute` / `looksLikeBundleMember` 的用例） | §4.1.1 要求把 `app.js` 的成员正则对齐到 `.mrxs|dat|vms`（**刻意不含 `.vmu`**）——只改 `app.js` 的**调用侧**，`tools-slides-bundle.js` 里的纯函数与其既有断言不变 | 不受影响；新增一条源码级断言：`app.js` 的成员正则**覆盖** `.mrxs|dat|vms`（刻意**不含** `.vmu`，与 `looksLikeBundleMember` 的表面差异已说明理由），防再次漂移 |
| `tests/browser/slide_tools_r1/run_e2e.js` i/i2/i3/i4/l2/l3 | i 系列依赖**工作台弹窗交接**（`context.waitForEvent('page')`） | 「工作台入口」场景需在开关关闭下跑（或重写为面板内）；工具页场景不受影响。这不是 pytest/vitest，按 §9.4（Playwright 侧）处理 |
| `tests/browser/slide_tools_c4/run_workbench.js` | 原生 `.tif` 直传、请求序列逐条比对 | **已实测失效（非本设计引入）**：该脚本的 64 B `wb-regression.tif`（字节 `0..63`，`run_workbench.js:51-55`）经 `f8f130c2` 的 fail-fast 魔数闸判为 `{ext:'.tif', expected:'tiff', detected:'unknown'}`；在真实 app.js + 真实引擎上实测：**零 `/api/ingestions`、零 COS 请求，行停在「上传失败」**（命令与输出见 §10.7）。`dca42ac2` 只修了 `slide_tools_c4/run_e2e.js` 的夹具，未动本脚本 | 换真 TIFF 头夹具（属既有测试维护）；本设计的面板路径同样受该闸约束，故 §9.4 的新 spec 一律用真 TIFF/BigTIFF 头 |
| `tests/test_ingestion_api.py:483` / `test_r1_conversion_gate.py:77` | 断言 `_cos_upload_capability_payload` 的字段/词表 | 只新增键，现有断言（含 `browser_convert` 集合 `:93`、`direct_upload` 集合 `:98`）不变；**无**测试断言精确键集合（已核） |
| `tests/test_slide_tools_upload_capability.py:235/276/315` | 读 `body["cos_upload"]`，无键集合断言 | 不变 |
| `tests/test_slide_tools_page.py:72-113` | `/tools/slides` 的 CSP 逐 token 精确匹配 | **不受影响**（本设计不改 `_slide_tools_csp`） |
| `tests/e2e/*`（现有 **16** 个 spec——实测计数见 §10.15） | 都不断言工作台上传行为；`e2e_server.py` 不配置 COS（capability off） | 不受影响；新增 spec 需要 COS 配置，见 §9.4 |

### 9.2 vitest（新增 `tests/js/inline-convert.test.ts`）

**两种加载面，同一文件但分开描述**（评审 J4）：`app.js` 与 `inline-convert.js`
是 classic script，用 `new Function('window','document',…)` 加载（形态照
`workbench-direct-class.test.ts:100-152`）；第 12 项的对照对象
`tools-slides-upload.js` 是 **ES module**，只能用顶层 `import`（照
`tools-upload-limit.test.ts:19`、`tools-upload-feedback.test.ts:23` 的既有写法）——
vitest 单文件内两者**可以并存**（`import` 提升到模块顶，`new Function` 在
用例里执行），本设计明确要求这样拆，不许把 `tools-slides-upload.js` 塞进
`new Function`（它 `import * as E from './slide-transform/engine.js'`，见该文件
头部，且依赖工具页 DOM 与注入的 `t`/`runner`）。

形态照 `workbench-direct-class.test.ts`：`new Function` 加载 `cos-uploader.js`
+ `slide-sniff.js` + `inline-convert.js` + `app.js`，注入假 fetch / 假
`HP_INLINE_CONVERT.runner`。

1. 判定表穷举：§4.2 每一行（真装 TIFF 头夹具：`classicTiff()` 同款 +
   BigTIFF 夹具）→ 断言 `needsConvert` / `directClass` / 最终动作
   （convert | upload | reject），且与 `engine.sniffTiffSlideCapability` /
   `sniffRasterCapability` 的真实结果一致（**「不得比引擎更严」**：对一批
   夹具断言 `panelNeedsConvert === engineSupported`）。
2. 未登记扩展名 → `rejected`，零 fetch。
3. PNG 改名 `.jpg`（真实 PNG 头）→ `rejected`，`detectedType==='png'`，
   **零 fetch / 零 XHR**（照 `cos-upload-liveness.test.ts` 的 FakeXHR 计数）。
4. 队列：3 个转换类条目 → 任一时刻至多 1 个 `converting`；转换 A 完成前
   B/C 停 `convert_queued`。
5. **门禁作用域**（§5.1）：同一组假记录下 —— 转换产物条目在
   `job.result.format ∉ viewable_formats` 时**不建 ingestion**（断言
   `/api/ingestions` 调用数 0）；直传条目**没有** `result.format` 时照常建
   ingestion（断言 `viewable_formats` 不参与判定，与
   `tools-slides-direct-upload.js:320/332` 的直传口径一致）。两条断言一起
   才排除了「直传被 viewable_formats 误杀」。
6. **产物声明（§4.3）**：转换完成后，`createUpload` 的 `createBody.direct_class`
   必须等于对**产物视图**`classifyFile` 的结果 —— OME-XML 描述产物 →
   `'ome-tiff'`；JSON 来源标记产物 → `'converter-bigtiff'`；嗅探失败 →
   不带字段（`undefined`）。**反向断言**：绝不出现「OME 产物 + converter-bigtiff」
   （该组合服务端 `declaration_matches=False` → worker `convert_in_browser`）。
7. `convert_in_browser`（`createNewJob` 422）→ 自动回队**恰一次**；第二次 → 终态 + 申请支持按钮。
8. 上传槽与产物锁：假 `navigator.locks` 记录**锁名**，断言上传阶段同时持有
   `pt:upload-slot:<acct>` 与 `engine.uploadLockName(jobId)`
   （后者是 `discardJob` 的保护对象，§5）；同号第二次请求排队（不并发两个
   `createUpload`）。
9. 刷新恢复**不自动开始**：`listJobs()` 返回带 `intent.channel==='workbench-inline'`
   的 `planned+journal` / `ready` / 已收口（待清理组）记录 → 各得一行
   （未完成的给「继续」、已收口的给「删除本地产物」）；**面板不自动开始任何
   转换或上传**。

   断言口径（第一轮 R8a + 第四轮 A7 修正，**必须写成「无面板发起的新请求」**）：
   真实页面加载本身就会发 5 个 GET（`/api/auth/info`、`/api/slides`、
   `/api/projects`、`/api/share/list`、`/api/annotations`——本文件 §10.7 自己
   记录了这一点），所以**字面的「零 `/api/` 请求」在真页面上不可能成立**。
   正确写法：以「reload 完成后」为基准，断言
   - **零新增** `/api/ingestions*`（含 `POST /api/ingestions` 与各 `/parts/sign`）、
     零跨源 PUT；
   - 零 `probe` / `prepareBundle` / `startJob` / `resumeJob` / `createUpload`
     / `discardJob` 调用（在面板模块的 fake runner 上计数；`SlideToolsRunner
     .create()` 内部的 `_processPendingCleanups` / `_sweepIncompleteStaging` /
     `_spawnWorker`（`runner.js:146-152`）是**本地 OPFS + worker 启动**，
     不是「开始转换或上传」——R8a）；
   - 点击「继续」/「删除本地产物」**之后**才出现对应请求（正例）。
   `tests/e2e` 用例 C 采用同一口径（§9.4）。
10. **续传语义**（§6.4）：`record.upload` 有未收口 `ingestionId` → 「继续上传」
    传 `resumeJobId` 等于该 id 且 **零** `POST /api/ingestions`；记录为空/终态
    → 只显示按钮、**零请求**；点击后才建新任务。
11. **终态清理**（§6.3）：终态行有「删除本地产物」→ 调用 `discardJob`；
    `upload_active` + `lockHeld:false` → 二次确认后 `abandonUpload:true`。
12. **门禁等价性对照**（§2.3 承诺；评审 R8b + 本轮 J4 修正）：**对照面不是
    `runLocked` 全序列**——它含人工交互分支（`chooseAccountAction` 的
    dialog、`signOutForOriginal` 的登录跳转、`:553/:580`）与孤儿结算
    （`settleOrphan`、`:494`），无法逐例自动比对（本轮评审成立）。改为对照两点
    可自动化的观察面：

    - **观察面 A（判定结论）**：真 `import { createUploadController } from
      '../../static/tools/tools-slides-upload.js'`（既有先例：
      `tools-upload-limit.test.ts:19`、`tools-upload-feedback.test.ts:23` 已这样
      import 并驱动它），用同一组假 `runner`/假 capability/假 `job` 记录注入
      `createUploadController({runner, t, …})`，对每条输入同时读
      **`ctl.isDisabled(jobId)`**（`:1137-1141` 的返回面；既有测试正是这样断言
      `tools-upload-limit.test.ts:169`、`tools-upload-feedback.test.ts:270`）与
      面板 `decide()` 的结论（allow/deny + 原因键），断言**等价**。覆盖输入：
      产物 > `max_size_bytes`（→ 双方 refuse，`tools.upload.too.large`）、
      `result.format ∉ viewable_formats`（→ 双方 refuse，
      `tools.upload.format.unsupported`）、产物格式可查看且大小合规（→ 双方
      allow）；
    - **观察面 B（副作用）**：同一组输入下断言双方**都没有**
      `POST /api/ingestions`（refuse 路径）；allow 路径的对照只在面板侧断言
      `createUpload` 被调用且 `createBody` 形状符合 §4.3（工具页侧的
      ingestion 创建已由既有 `tools-upload-*` 测试覆盖，不重复）。

    **明确不做**：账号 mismatch / 孤儿 / 已发布重放（`runLocked` ⓪①b④ 的分支）
    不纳入本对照——它们是工具页特有的交互语义，面板按 §7 自己的处置走；
    两侧在这些分支上**允许**不同。这条边界写进测试注释，避免后来者误解
    「等价」的范围。
13. 申请支持：按钮 → `POST /api/format-requests`，body 含预填 `format_ext` 与
    带机器码的 `message`；202 显示编号；429 → 今日上限文案；413 → 样本超限。
14. i18n：新键 zh/en 成对存在（静态扫描 `i18n.js`），`_EXTRA_I18N` 同步；
    **反向断言**：`upload.err.unsupported` 的 zh/en 文案里不再出现
    `/tools/slides`（§8 的改写）；`upload.cos.err.convert_in_browser` 的**基础
    文案**也不再直接写死路径，路径只经 `inline_convert.url` 形参在两态下
    有条件追加（第四轮 A5，见 §8 与第 22 项）。
15. 回退：`inline_convert.enabled=false` → 走 `offerBrowserConvert`，
    断言 `window.open` 收到 `/tools/slides`（与 `r1-convert-upload.test.ts`
    现有断言同形）。
16. bundle：`classifyFile` 给 `bundle:true` 的**散文件**（单 `.mrxs` / 单
    `.vms`）→ 类型化说明 + 「选择文件夹」按钮，**零 fetch / 零转换**
    （且说明文本是**静态**的、不依赖读目录）；文件夹形态 → `prepareBundle`
    被调用、`progress.unit==='stage-bundle'` 映射到行文案（§4.1.1）。
    另断言：**含 `.vmu` 的文件夹仍走逐成员直传**（不被划进 bundle 分支，
    保护今天可用的 VMU 直传）。
17. `beforeunload`（§6.2 第 7 点）：对 `converting` / **`need_disk_confirm`** /
    `upload_queued` / `uploading` / `requeue_convert` 五个状态逐一断言注册了
    提醒；对 `done` / `rejected` 等终态断言**未**注册（第二轮评审 J3 —— 防按
    状态名枚举再漏项）。
18. `need_disk_confirm`（§7.3 + §6.2.1，**第三轮评审 K1 修正**）：
    `disk_precheck_failed` + `uncertain:true` → 行内确认按钮；确认后断言
    **带着原始输入重新起**：`probe`/`prepareBundle` 被再次调用且**第一个实参
    是同一个 `entry.file` / `entry.bundle.rows`**，`confirmUncertainDisk:true`
    出现在选项里；**反向断言**绝不出现 `startJob(null, {jobId})` 或任何
    「复用旧 jobId」的调用（旧目录已被运行器自己删掉——`runner.js:759`/`:534`
    的 `_discardNow`，`:682` 的 `removeEntryRecursive`）。原稿「重试同一
    `probe`（不重建任务）」的说法**已删除**。
19. **目标关联（§3.5，第三轮 K2 + 第四轮 A1）**：`uploadFile`/`enqueue` 入口冻结
    快照（源码级断言 `opts.assocTarget = importTargetSnapshot()` 仍成立）；
    **源码级断言 `uploadFileCos` 的调用字面量含 `assocTarget`**（`:10164-10170`
    ——现有断言只钉住「被取」，第四轮 A1 实测它没钉住「被传」）；
    转换条目完成后 `importAssociateUploaded(slideId, name, entry.target, row)`
    被调用且**第三参是入队时冻结的 target**（用假 `snapshot` 值 + 中途改
    `importTargetState` 驱动，断言关联用冻结值而非当前文件夹）；随后
    `loadAll()` 与 `openSlide(slideId)` 被调用；直传条目走 `uploadFileCos`
    的同一路径且 `created` 事件调 `fbUpAssocSave(ingestionId, <冻结 target>)`
    （断言第二参非 `undefined`）。
20. **恢复行两组渲染（§6.2 第 2 步，第四轮 A2）**：`listJobs()` 返回三条记录
    ——`planned+journal`、`ready`（upload 未收口）、`ready`（upload
    `state:'published'`）→ 断言**三条各得一行**；前两条给「继续转换」/
    「继续上传」，第三条给「**删除本地产物**」且**不应有**继续动作
    （反向断言：已收口记录不得被过滤掉）。
21. **bundle 两个入口（§4.1.1/§2.2⑩b，第四轮 A3）**：开关开时
    `importDroppedDirectory`（目录拖入含 `.vms`）与 `importPickFolder`
    （`#import-folder-input` change）**都**调 `enqueueBundle` 且断言
    **`window.open` 零调用**、`handoffBundleFolder` 零调用；开关关时两者都调
    `handoffBundleFolder`（回退行为）。
22. **回退文案两态（§8，第四轮 A5）**：`enabled===false` 时
    `tt('upload.cos.err.convert_in_browser')` 的结果**含** `/tools/slides`
    链接（取自 `inline_convert.url`）；`enabled===true` 时**不含**（面板自行
    处理）。`upload.err.unsupported` 两态都**不含** `/tools/slides`，且同行的
    申请支持按钮存在。

### 9.3 pytest（新增 `tests/test_upload_inline_convert_capability.py`）

1. `_cos_upload_capability_payload(False)["inline_convert"]` 存在、`enabled is True`、
   `viewable_formats == list(app.SLIDE_TOOLS_VIEWABLE_OUTPUT_FORMATS)`；
   `available:false`（capability off）时该键**仍然下发**。
2. `viewable_formats` 与 `/api/tools/slides/upload-capability` 的同名字段
   **同源**（两处相等），且 `tests/test_slide_tools_upload_capability.py` 的
   「读取器证明」约束一并适用（三个格式都由 `slide_io.open_slide` 真开过）。
3. `/app` CSP 断言：若响应存在 `Content-Security-Policy`，必须包含
   `'wasm-unsafe-eval'` 与 `worker-src 'self'`（本次实测当前**无** CSP 头，
   所以断言写成条件式；再加一条「现在没有 CSP 头」的显式记录，防止将来静默
   加头挡掉 WASM）。
4. 关闭开关（monkeypatch 常量/env）→ `enabled is False`，其余字段不变。
5. `/app` 渲染：`inline-convert.js` 在
   `cos-uploader.js` / `slide-sniff.js` 之后、`app.js` 之前加载（源码级断言，
   照 `tests/test_slide_tools_page.py:115` 的「共享上传器」写法）。
6. `tests/test_upload_direct_class.py` 不动（31 项本次实测全绿，见 §10）。

### 9.4 Playwright e2e（新增 `tests/e2e/upload-inline-convert.spec.ts`）

硬要求（本次指令）：**真实 WASM + 真实 Chromium**，覆盖三条：

| # | 用例 | 验收点 |
| --- | --- | --- |
| A | 「转换后上传且上传字节能被平台读取器 `slide_io.open_slide` 打开」 | 真 KFB 夹具（`kfb.fixture.build_synthetic_kfb`，或 CLI `gen-kfb`）→ 工作台拖入 → 面板内转换（真 wasm）→ 上传 → 行到「入库完成」→ 服务端用 `slide_io.open_slide` 打开**已发布**资产并读到层级/尺寸/区域像素。**另断言创建体里的 `direct_class` 与产物字节一致**（`bf-ome` 默认 → `'ome-tiff'`；写死 `converter-bigtiff` 会让 worker `declaration_matches=False` → 终态 `convert_in_browser`，用例 A 必红——这正是评审 R1 的缺陷） |
| B | 「PNG 改名 `.jpg` 当场被拒且申请支持提交成功」 | 真 PNG 字节 named `x.jpg` → 行内出现检测类型 + 申请支持按钮 → 点击 → `POST /api/format-requests` 202 → 行内出现申请编号；**全程零 `/api/ingestions`、零跨源 PUT** |
| C | 「刷新后点继续能把转换做完」 | 转换进行中 `page.reload()` → 行显示「继续转换」且**不自动开始**（第四轮 A7：断言口径是「无**面板发起的新**请求」——reload 后零新增 `/api/ingestions*`、零跨源 PUT、零 `probe`/`startJob`/`resumeJob`/`createUpload`/`discardJob`；页面自身的 5 个 GET 与 `SlideToolsRunner.create()` 的本地 OPFS cleanup/sweep 及 worker 启动都不算）→ 点「继续」→ 转换完成 → 产物 sha256 与 OPFS 产物一致 → 上传完成 |

为让 A 的「上传字节被平台读取器打开」真成立，需要（**本设计的关键新增测试基建**；
接线方式已按第四轮复审 A4 实测修正）：

- **服务端指向假 COS：只能靠测试侧 monkeypatch `cos_client._client`，`Endpoint`
  是错的旋钮**（实测，见 §10.16）：
  - 生产构造在 `cos_client.py:60-64` 写死
    `CosConfig(Region=…, SecretId=…, SecretKey=…, Scheme="https")`——**没有**
    `Endpoint`，也没有任何 env 读取（`grep -rn "COS_ENDPOINT" --include=*.py .`
    = 0 命中），所以「服务端指向本地假 COS」**在生产代码里没有落点**。本设计
    **不改 `cos_client.py`**（生产路径零改动），由 e2e 进程在 import 后
    `monkeypatch.setattr(cos_client, "_client", fake_factory)` 并清
    `_client_cache`；已实测该替换同时影响 `presign_upload_part` 与控制 API
    （两者都走 `_client()`），且签出的 URL 主机随替换变化；
  - **`Endpoint=<host:port>` 会产出坏主机**：实测
    `CosConfig(Endpoint="127.0.0.1:9443")` 的预签名 URL 是
    `https://probe-1250000000.127.0.0.1:9443/…`（把 endpoint 当成域名后缀拼在
    bucket 后），`Endpoint="http://127.0.0.1:9443"` 更退化成
    `probe-1250000000.http:`。**正确旋钮是 `Domain="127.0.0.1:<port>"`**
    （path-style：`https://127.0.0.1:9443/incoming/<job>/data.tif`，
    `Content-Length` 仍在签名 query 里）配 `VerifySSL=False`；
  - 因此**浏览器侧不需要、也不该用 `--host-resolver-rules`**：预签名 URL 已经是
    `https://127.0.0.1:<port>/…`，直接命中本地 HTTPS 假 COS 即可（自签证书 →
    仍需 `--ignore-certificate-errors`）。原稿写「MAP
    `<bucket>.cos.<region>.myqcloud.com 127.0.0.1`」是**基于错误的
    `Endpoint` 假设**；那条映射只在 C4 的 virtual-hosted 形态下成立
    （`slide_tools_c4/lib.js:295-296` 的证书 SAN 也是那个域名）；
  - 备选（若坚持 virtual-hosted 形态以便复用 C4 的自签证书与映射）：
    monkeypatch 仍用 `Domain=<bucket>.cos.<region>.myqcloud.com`，并用
    `--host-resolver-rules=MAP <那个域名>:443 127.0.0.1:<port>` 把它指到本地
    HTTPS 假 COS（C4 已验证过的手法）。两条路都**不在生产代码里加注入点**；
- `tests/e2e/e2e_server.py` 增加 COS 支持（默认关，`E2E_INLINE_CONVERT=1` 时开）：
  - 设 `COS_BUCKET`/`COS_REGION`/`COS_SECRET_ID`/`COS_SECRET_KEY`、
    `COS_UPLOAD_CAPABILITY=on`、池容量 > 产品上限（照
    `tests/browser/slide_tools_c4/server.py:110-118` 的假 COS 配置与
    `app_mod.UPLOAD_PRODUCT_MAX_BYTES = 900_000_000`）；
  - **进程内假 COS**：一个真实本地 HTTPS 端点实现 `InitiateMultipartUpload` /
    `UploadPart` / `CompleteMultipartUpload` / `ListParts` / `GET(Range)`，
    对象落临时目录（这两条既有实现可直接照抄 c4 `lib.js:285-345` 的
    `startLocalCos`，只需扩出 multipart 与 GET）；
  - 新增测试专用只读端点 `GET /__test/slide-probe?slide_id=<id>` 返回
    `{reader, levels, width, height, mpp}`（内部 `slide_io.open_slide` 真读）。
- `playwright.config.ts`：新增 project `chromium-cos`（`testMatch:
  'upload-inline-convert.spec.ts'`、带上述 launch args），并在默认 project 上
  `testIgnore` 该文件，避免其余 **16** 个 spec 被 launch args 影响或重复跑。
- `e2e_server.py` 的假 COS 与 HTTP 端点只在该 env 打开时装配，默认路径
  （CI 现有 spec）行为不变。

替代方案（若上面成本过高，作为降级备案）：page.route 拦 COS PUT 收字节 +
测试专用端点把字节写回服务端 + 用 `tests/test_cos_ingest_worker.py` 的
`FakeCos` 推进 worker 状态（即 `tests/test_bf_ome_platform_chain.py` 的
pytest 手法搬进 e2e）。**但这会让「浏览器真的把字节 PUT 出去」这一环退化**，
与「真实 WASM + 真实 Chromium」的意图不符，故只作备案。

## 10. 本次已跑的核对（供复核，命令与输出）

环境（所有命令前设置，符合仓库约定）：

```bash
mountpoint -q /mnt/ssd2 || exit 97
export TEST_RUN_DIR=/mnt/ssd2/tests/run-inline-convert-Mwpad1 \
  TMPDIR=$TEST_RUN_DIR/tmp TMP=$TEST_RUN_DIR/tmp TEMP=$TEST_RUN_DIR/tmp \
  npm_config_cache=$TEST_RUN_DIR/cache/npm \
  COVERAGE_FILE=$TEST_RUN_DIR/.coverage \
  PATH=/home/solarise/ZCodeProject/histopilot-suite/PathTogether/.venv/bin:$HOME/.cargo/bin:$PATH
```

1. **`/app` 是否有阻 WASM 的 CSP**（指令要求）：Flask `test_client` 直取
   `/app`、`/tools/slides`、`/login?next=/app` → 结果：
   `/app 200 | CSP: <none>`、`/login?next=/app 302 | CSP: <none>`、
   `/tools/slides 200` 带 `script-src 'self' 'wasm-unsafe-eval'; worker-src 'self'`。
   再用真实 Chromium（Playwright）取 `/`（`AUTH_ENABLED=False` 的工作台渲染）
   → 同样 `CSP: <none>`。
2. **classic script 动态 import + module worker + WASM 可行**：真实 Chromium 打开
   真实工作台页面，`await import('/static/tools/slide-transform/runner.js')` →
   `SlideToolsRunner.create()` 成功、`coreVersion=0.1.0`、`listJobs()===[]`；
   `engine.isTiffHeader(II*\0)` 为真；**零 pageerror / 零 console.error**。
3. **静态资源 MIME**：`slide_transform_bg.wasm` → `application/wasm`；
   `worker.js`/`runner.js`/`slide-sniff.js` → `text/javascript`；均 `no-cache`。
4. **capability 载荷缺口**：`_cos_upload_capability_payload(False).keys()` =
   `['available','browser_convert','direct_upload','formats','manual_only']`
   ——**无** `viewable_formats`；`/api/tools/slides/upload-capability` 顶层为
   `['account','account_label','cos_upload','viewable_formats']`。
5. 基线测试（未改动仓库）：
   - `python3 -m pytest tests/test_upload_direct_class.py -q -p no:cacheprovider
     --basetemp=$TEST_RUN_DIR/pytest-directclass` → **31 passed**；
   - `python3 -m pytest tests/test_r1_conversion_gate.py -q …` → **8 passed**；
   - `python3 -m pytest tests/test_ingestion_api.py::test_direct_class_svs_closed_unless_declared
     tests/test_ingestion_api.py::test_direct_class_zip_container_has_no_class -q …`
     → **2 passed**（这两条是「`.svs` 无声明 422 `convert_in_browser`」与
     「zip 不携带 `direct_class`」的权威断言，§4.2 表里对应行的依据）；
   - `npx vitest run tests/js/{workbench-direct-class,cos-upload,cos-upload-liveness,
     r1-convert-upload,slide-sniff}.test.ts` → **5 files / 112 passed, 2 skipped**。
   - **评审后复跑**（本文件修改后、未改仓库代码）：
     `python3 -m pytest tests/test_upload_direct_class.py tests/test_r1_conversion_gate.py
     -q -p no:cacheprovider --basetemp=$TEST_RUN_DIR/pytest-r3` →
     **39 passed in 14.98s**；
     `npx vitest run tests/js/{workbench-direct-class,cos-upload,cos-upload-liveness,
     r1-convert-upload,slide-sniff,tools-u2-page,tools-upload-limit,tools-upload-feedback}.test.ts`
     → **8 files / 179 passed, 2 skipped**（后三个文件即 §9.2 第 12 项对照测试的
     既有先例与 `bundleRoute` 纯函数用例）；
     **第四轮复审后复跑**（`pytest-a4`，仓库代码仍未动）：
     `python3 -m pytest tests/test_upload_direct_class.py
     tests/test_r1_conversion_gate.py -q -p no:cacheprovider
     --basetemp=$TEST_RUN_DIR/pytest-a4` → **39 passed in 15.09s**；
     同一 9 个 vitest 文件 → **9 files / 222 passed, 2 skipped**（1.19s）。
     **第三轮评审后复跑**（`pytest-k1`，并加入 `viewer-folders`）：
     `python3 -m pytest tests/test_upload_direct_class.py
     tests/test_r1_conversion_gate.py -q -p no:cacheprovider
     --basetemp=$TEST_RUN_DIR/pytest-k1` → **39 passed in 15.10s**；
     `npx vitest run tests/js/viewer-folders.test.ts
     tests/js/workbench-direct-class.test.ts tests/js/cos-upload.test.ts
     tests/js/cos-upload-liveness.test.ts tests/js/r1-convert-upload.test.ts
     tests/js/slide-sniff.test.ts tests/js/tools-u2-page.test.ts
     tests/js/tools-upload-limit.test.ts tests/js/tools-upload-feedback.test.ts`
     → **9 files / 222 passed, 2 skipped**（含 §3.5 依赖的
     `viewer-folders.test.ts` 全绿——它的源码级断言在当前代码上成立）。
6. `CosConfig` 签名含 `Endpoint` / `VerifySSL`（假 COS 的可行性依据）。
7. **`run_workbench.js` 的 64 B 假 `.tif` 在真实工作台里被 fail-fast 拒**（既有
   回归，非本设计引入）：照 `tests/browser/slide_tools_c4/server.py` 的假 COS env
   起真实 Flask（`COS_UPLOAD_CAPABILITY=on`、池容量 10 GB−0.1 GB、
   `UPLOAD_PRODUCT_MAX_BYTES=900_000_000` → `cos_upload.available=true`），
   真实 Chromium 打开工作台，执行 `run_workbench.js:51-55` 的同一段：
   64 B `wb-regression.tif` → 实测结果：
   - `cosAvailable: true`；
   - `apiCalls` 只有页面加载的 5 个 GET（`/api/auth/info`、`/api/slides`、
     `/api/projects`、`/api/share/list`、`/api/annotations`）——**无
     `/api/ingestions`**；`cosCalls: []`；
   - 行文本 `wb-regression.tif · 64 B上传失败取消`（失败态）。
   纯函数侧同一结论：`headerMismatch('wb-regression.tif', bytes0..63)` →
   `{"ext":".tif","expected":"tiff","detected":"unknown"}`。
8. **判定表（§4.2）逐行核对**：用真实的 `slide-sniff.js`（classic script 注入
   到 Node 里跑）+ 真实的 `engine.js`（动态 import）跑 15 个夹具，覆盖
   `kfb / kfbf / svs-jpeg / svs-jp2k / ome / converter-bigtiff / gtiff / plain-tif /
   zip / vmu / 未知扩展名 / jpg / bmp / png-改名-jpg`。结果与 §4.2 一致，例如：

   | 夹具 | `classifyFile.cls` | `directClass` | `headerMismatch` | `engine.supported` |
   | --- | --- | --- | --- | --- |
   | KFB / KFBF（真魔数） | `convert` | `null` | `null` | `true` |
   | `.svs` + Aperio 描述 + 压缩 7 | `convert` | `legacy-direct` ⚠ | `null` | `true` |
   | `.svs` + 压缩 33003 | `temporary` | `unconverted-variant:svs-jp2k` | `null` | 不适用 |
   | `.ome.tif` + OME-XML 描述 | `ome-tiff` | `ome-tiff` | `null` | 不适用 |
   | `.tif` + 转换器来源标记 | `converter-bigtiff` | `converter-bigtiff` | `null` | 不适用 |
   | 通用瓦片 JPEG TIFF | `convert` | `legacy-direct` ⚠ | `null` | `true` |
   | 普通 TIFF（无厂商描述） | `temporary` | `legacy-direct` | `null` | 不适用 |
   | `.zip` | `temporary` | `null` | `null` | 不适用 |
   | `.vmu` / 未登记扩展名 | `temporary` / `unsupported` | `legacy-direct` / `null` | `null` | 不适用 |
   | PNG 改名 `.jpg` | `temporary` | `legacy-direct` | `{expected:'jpeg', detected:'png'}` | 不适用 |

   ⚠ = `classifyFile` 的收口分支回填的 `legacy-direct`，**转换条目不得使用**
   （见 §4.2 末的澄清）；`.svs` 用它会被服务端 422 `convert_in_browser`。
9. **普通图片的可转换判定与引擎一致**：真 PIL 生成的三张 JPEG 上
   `classifyFile` 与 `sniffRasterCapability` 结论一致 ——
   基线 RGB `convert` / `supported:true`；渐进 RGB `temporary` /
   `supported:false`（`渐进/分层/无损 JPEG 不受支持（SOF FFc2）`）；
   基线灰度 `temporary` / `supported:false`（`JPEG 是单分量（灰度）`）。
   BMP：未压缩 24 位与 32 位 → `supported:true`；8 位调色板 →
   `supported:false`（`BMP 位深 8 不在支持集`）；24 位 RLE8 →
   `supported:false`（`只支持未压缩 BI_RGB`）。
10. **改名 SVS 的两条分支**（§7.2 的依据）：同一份 Aperio JPEG 描述，
    tiled 布局 → `classifyFile=convert` + 引擎 `supported:true`
    （`adapter=aperio-svs-jpeg`）；strip 布局 → `classifyFile=temporary`
    + 引擎 `supported:false`（`主图不是分块（tiled）存储：该 TIFF 变体不在支持集`）。
    strip 那份再经服务端纯函数：
    `upload_direct_class.sniff_tiff_class = tiff-other`、
    `is_aperio_svs_jpeg = True`、
    `enforcement_failure(path,'legacy-direct','tif') = 'convert_in_browser'`
    （而 `enforcement_failure(path,'ome-tiff','tif') = None`，
    `declaration_matches(path,'legacy-direct') = True`）——
    即 §7.2(a)/(b) 两条分支都按设计成立。
11. **转换产物的 `direct_class`（§4.3 的依据）**：构造两枚 `.tif` 夹具，用
    `upload_direct_class` 直接裁定 ——

    ```
    bf-ome/fl-ome 产物（OME-XML 描述）
       sniff_tiff_class                        = ome-tiff
       declaration_matches('ome-tiff')          = True
       declaration_matches('converter-bigtiff') = False
    bf-classic 产物（JSON 来源标记）
       sniff_tiff_class                        = converter-bigtiff
       declaration_matches('ome-tiff')          = False
       declaration_matches('converter-bigtiff') = True
    ```

    另对无声明路径实测：OME-XML 与 JSON 两种产物在
    `declared ∈ {None,'','legacy-direct'}` 下 `declaration_matches=True` 且
    `enforcement_failure=None`（即 §4.3 的「嗅探不确定 → 不带声明」兜底安全）。
    这直接证明评审 R1 成立：在 §4.3 的 `bf-ome` 默认下声明
    `converter-bigtiff` 会被 worker 以 `convert_in_browser` 拒绝。
12. **工具页上传不发 `direct_class`**（对照面）：`grep -c createBody
    static/tools/tools-slides-upload.js` = **0**（该路径靠**无声明** +
    `open_slide` 终审）；只有 `tools-slides-direct-upload.js:401` 的直传路径发
    `createBody: { direct_class: directClass }`。这解释了为什么既有工具页转换
    上传不受 R1 影响，而面板一旦「主动声明」就会踩到。
13. **VMU 今天的受理状态（§4.1.1 的 `.vmu` 决策依据）**：`python3` +
    `app._cos_upload_capability_payload(False)` 与
    `slide_format_registry.lookup` 实测 ——

    ```
    vmu 在 direct_upload 受理集: True
    vmu 在 browser_convert 集:   False
    vmu 注册表 capability:       native-single-file
    ```

    即 VMU 是「今天就能直传的 native 单文件」，不是转换输入。把 `.vmu` 划进
    文件夹 bundle 分支会把含 `.vmu` 的文件夹变成 planner 的 `unsupported_input`
    拒绝（`engine.js:1358-1362`）——那是收紧能力，故 §4.1.1 明确不对齐 `.vmu`。
14. **本轮四条评审的复核动作**：J1 用 `grep -c 删除本地产物` 分别对
    工作树 / `d2b56235` / `897c7830` 的第 107 行计数（结果 1 / 1 / 0）；
    J2 读 `app.js:11109-11111` 的正则与 `tools-slides-bundle.js:16`、
    `engine.js:1269-1280`（`sniffVmsBundle` 不给 tile 级 `missing`）与
    `engine.js:1340-1345`（`planVmsBundle` 才给）；J3 读
    `runner.js:734/772/867` 与 §6.2 第 7 点的原文；J4 读
    `tools-slides-upload.js:1137-1141`（返回面）、`:553/:580`（交互分支）与
    `tools-upload-limit.test.ts:19/169`、`tools-upload-feedback.test.ts:23/270`
    （既有 import 与 `isDisabled` 断言先例），并**实跑**
    `npx vitest run tests/js/tools-upload-limit.test.ts
    tests/js/tools-upload-feedback.test.ts` → **16 passed**（证明该模块可在
    vitest 里被 import 驱动）。
15. **第三轮三条评审的复核动作**：
    - J1（磁盘门后重试）：读 `runner.js:732`（前置门）与 `:734`
      （`_writeJobRecord(state:'staging')`）的相对次序、`:755`（后置门）与
      `:759`（catch 里的 `_discardNow`）、`:674-686`（`_discardNow` =
      `removeEntryRecursive`，`:682`）、`:285-288`（`_jobDir(jobId, true)` 建目录）、
      `:867`（`缺少输入文件`）与 `:729`（新 jobId —— 即重试会**重新复制**）；
      并对照工具页的既有正确形态 `tools-slides.js:298-322`（`probeWithDiskFlow`
      把 `file` 再传一次）/ `:564-578`（`prepareBundleWithDiskFlow` 把 `rows`
      再传一次）。原稿「副本已在 OPFS」的理由**被推翻**，已按实测改写
      （§6.2 第 7 点 + 新 §6.2.1）。
    - J2（目标关联）：读 `app.js:10109`（`opts.assocTarget =
      importTargetSnapshot()`）、`:10789`（`created` → `fbUpAssocSave`）、
      `:10859-10865`（`loadAll()` → `importAssociateUploaded` → `openSlide`）、
      `:4028`（`fbUpAssocSave`）、`:11282`（`importAssociateUploaded`）、
      `:3829`（`fbIdentityReady`）、`pollRestoredRow` 的 viewable 分支
      （`fbIdentityReady` → `fbUpAssocTake` → `importAssociateUploaded`），
      以及源码级断言 `tests/js/viewer-folders.test.ts:1095`；并核对文档
      `grep -c "关联" = 0`、`grep -c
      "importAssociateUploaded\|loadAll\|openSlide\|fbUpAssocSave\|fbAssocPendingSave" = 0`
      —— 证实原设计确实缺这条链路。已新增 §3.5。
    - J3（spec 计数）：`ls tests/e2e/*.spec.ts | wc -l` → **16**（本 worktree
      HEAD `ec9ee180`）；文档原写 15，已改（§9.1 与 §9.4）。
16. **第四轮六条复审的实测证据**（命令与输出，均在本 worktree `ec9ee180` 基线上跑）：
    - **A1（`assocTarget` 未转发）**：`grep -n assocTarget static/app.js` →
      **5 处**（`10109` 写入、`10789`/`10798` 读、`10843`/`10864` 传参）；
      `sed -n '10164,10170p' static/app.js` 的实参字面量是
      `{resumeJobId, skipConfirm, directClass}`——**无 `assocTarget`**。
      历史对照：`git show 766214ad:static/app.js`（:9798-9804）与
      `git show 3cd08792:static/app.js`（:9866-9872）的同一字面量同样不含该键
      → 属既有缺陷。守卫在 `app.js:4029`，回落点在 `:11284`（`snap || importTargetState`）。
    - **A2（清理入口被过滤掉）**：doc §6.2 第 2 步原文的过滤条件与 §6.3
      「已收口记录也只在行上给按钮」矛盾；旁证
      `static/tools/tools-slides-upload.js:350`（`complete` 写
      `state:'published'`）与 `runner.js:361-366`（`_sweepIncompleteStaging`
      显式 `if (rec && rec.upload) continue;`，不兜底）。已改为两组渲染。
    - **A3（文件夹两个入口未进清单）**：`grep -c importPickFolder
      docs/…md` = **0**；`static/app.js:11137-11151` 的 `importPickFolder`
      无条件 `handoffBundleFolder(rows, first)`；`:11109-11114` 的目录拖入命中
      `/\.(mrxs|dat)$/i` 时无条件 `handoffBundleFolder`。已在 §2.2⑩b/§4.1.1 补上。
    - **A4（假 COS 接线）**：`grep -rn "COS_ENDPOINT" --include=*.py .` = **0**；
      `cos_client.py:60-64` 的 `CosConfig(...)` 无 `Endpoint`/`VerifySSL`。
      用 `qcloud_cos` 实测 `get_presigned_url` 的主机：
      ```
      无 Endpoint                              -> probe-1250000000.cos.ap-probe.myqcloud.com
      Endpoint="127.0.0.1:9443"                -> probe-1250000000.127.0.0.1:9443   ← 坏主机
      Endpoint="http://127.0.0.1:9443"         -> probe-1250000000.http:             ← 更坏
      Endpoint + IP=127.0.0.1                  -> 127.0.0.1（域名丢失）
      Domain="127.0.0.1:9443"                  -> 127.0.0.1:9443, path=/incoming/…  ← 正确
      Domain + VerifySSL=False（测试 monkeypatch `_client`）-> 127.0.0.1:9443
      ```
      即 **`Endpoint` 是错的旋钮**（评审建议的注入项会产生坏主机），正确做法是
      测试侧 `monkeypatch.setattr(cos_client, "_client", …)` +
      `CosConfig(Domain="127.0.0.1:<port>", VerifySSL=False)`；
      因预签名 URL 已指向 `127.0.0.1`，**不需要 `--host-resolver-rules`**。
    - **A5（回退路径文案自相矛盾）**：`i18n.js:1025`/`1026`（zh）与
      `:2686`/`:2687`（en）四处现文都含 `/tools/slides`；唯一使用点分别是
      `app.js:10545`（`cosErrorMessage`）与 `:10148`（`unsupported` 分支）。
      已把 §8 的承诺收紧为「控制流不变」并规定按开关两态的两套文案。
    - **A6（上传槽 account 来源未定义）**：`_cos_upload_capability_payload(False)
      .keys()` = `['available','browser_convert','direct_upload','formats',
      'manual_only']`（**无** account；带 account 的是工具页端点）；已定为页面
      既有的 `currentUserId`（`app.js:275`，与 `:10757` 的 `contentLockName`
      同一取值）。
    - **A7（零请求口径）**：doc §10.7 自己记录真工作台加载发 5 个 GET
      （`/api/auth/info`、`/api/slides`、`/api/projects`、`/api/share/list`、
      `/api/annotations`）→ 字面「零 `/api/` 请求」不可能；已改为
      「零面板发起的新请求」（§9.2 第 9 项 + §9.4 用例 C）。

## 11. 风险

| # | 风险 | 影响 | 缓解 |
| --- | --- | --- | --- |
| R1 | **侧栏空间不足**：现有行是 `makeUploadRow`（`app.js:9894`）的 4 段（名/进度条/状态/字节）+ 按钮，面板内要容纳「转换阶段 + 「请保持页面打开」 + 取消 + 驾驶舱级说明」 | 窄屏溢出（`viewer-folder-experience` 类回归） | 复用不定态进度条；说明文字折行 + `title`；Playwright 加 390×900 视口断言（照 `slide-tools-save.spec.ts:95` 的手机不溢出写法） |
| R2 | **`viewable_formats` 新增到 bootstrap 载荷**属产品级能力面变更，`/app` 的 HTML 会变大且含格式 id 列表 | 极小；但缓存 JS 与新载荷的时序（旧 JS 不读新键） | 新键纯增量；前端 fail-closed（缺字段 → 不自动上传） |
| R3 | **动态 import 的 URL**：`import('/static/tools/slide-transform/runner.js')` 是硬编码路径；部署前缀变化即失效 | 面板整体不可用 | 路径写成单一常量并在 pytest 里断言文件存在；import 失败 → 行内说明 + 回退到手交（开关关闭路径） |
| R4 | **e2e 假 COS 基建**（§9.4）是新增量：本地 HTTPS + DNS 映射 + `cos_client` 注入 | 若做不成，A 用例只能降级到 pytest 证明（`test_bf_ome_platform_chain.py` 已是既有证据） | 先做 pytest 侧证明（转换产物 → 上传字节 → `open_slide`），e2e 用力争；做不成时如实上报，不伪造 |
| R5 | **自动回队死循环**：`convert_in_browser` ↔ `temporary` 互推 | 无限循环 + 网络噪声 | 同 entry 最多回队 1 次（§7.2） |
| R6 | **磁盘/配额**：面板内转换把源副本 + 产物都放 OPFS，大文件（5 GiB KFB）在浏览器的 `uncertain` 报告下需要用户确认 | 用户看到额外弹层（与工具页一致） | 行内确认而非模态弹层；文案给出 need/available 两个数（`runner.js:697`）；**每个终态行给「删除本地产物」**（§6.3 的评审 R5 修正） |
| R7 | **两处默认输出格式分歧**（用户口径 `bf-classic` vs 工具页默认 `bf-ome`） | 产物扩展名 `.tif` vs `.ome.tif`、mpp 元数据有无；**并连带决定产物声明是 `converter-bigtiff` 还是 `ome-tiff`** | 记为待确认项（§4.4）；§4.3 的声明算法从产物嗅探，切换默认**不需要**同步改声明 |
| R8 | 旧文案改写（去掉 `/tools/slides` 指引）会同时改掉工具页可见文案 | 工具页用户看不到「用转换工具」提示（该页不需要） | 改写为「在本机转换（本页面板内自动完成）」；工具页有自己的输入提示文案，不受影响 |
| R9 | `tests/browser/slide_tools_r1/run_e2e.js` 的 i/i3/i4/l2/l3 依赖弹窗交接 | 这些浏览器回归在开关默认开后失效 | 明确这些场景只在 `inline_convert.enabled=false` 下有意义，脚本内显式注入关闭；新面板行为由 §9.4 新 spec 覆盖 |
| R10 | `run_workbench.js` 的 64 B 假 `.tif` 与步 2 魔数闸冲突 | **已实测失效**（§10.7）：零 ingestion、零 COS、行停在「上传失败」 | 换真 TIFF 头夹具（既有测试维护，非本设计引入）；面板路径同样受该闸约束，故新 spec 一律用真头夹具 |
| R11（本轮评审新增） | **产物声明错配会静默打穿主链路**：`ome-tiff` 产物 + `converter-bigtiff` 声明 → worker `convert_in_browser`，且只在 worker 阶段才暴露（创建闸不看内容） | 「转换后上传」全链路失败（工具页因不发声明而从未暴露此坑） | §4.3 的**产物嗅探**算法 + §9.2 第 6 项的反向断言 + §9.4 用例 A 的创建体断言，三道锁 |
| R12（本轮评审新增） | **门禁过严把直传杀光**：把 `viewable_formats` 套到直传条目 | 所有直传格式不可上传（与 §4.2 矛盾） | §5.1 的作用域表 + §9.2 第 5 项两条断言（转换产物受限、直传不受限） |
| R13（本轮评审新增） | **面板与工具页双标签互相破坏**：面板不持 `uploadLockName` 时，工具页可删掉正在上传的唯一本地产物 | 产物不可恢复 | §5 的「上传阶段持两把锁」（`pt:upload-slot:<acct>` + `engine.uploadLockName(jobId)`） |
| R14（上轮评审新增） | **散落 bundle 无归宿**（单 `.mrxs`/`.vms`） | 开关默认开时用户拖入后无定义行为 | §4.1.1 的类型化说明 + 「选择文件夹」按钮 |
| R15（本轮评审新增） | **纯 VMS 文件夹被逐成员上传**：`app.js:11109` 的成员正则不含 `.vms`（既有缺口） | `.vms` 入口被当普通文件上传 → 服务端按内容拒，且丢掉整包转换机会；用户看到逐文件失败而不是「选文件夹」 | §4.1.1：`app.js` 成员正则补 `.vms`（**不加 `.vmu`**——那会把今天可用的 VMU 文件夹变成 planner 拒绝）+ §9.1 的源码级防漂移断言 |
| R16（本轮评审新增） | **`need_disk_confirm` 不在 beforeunload 覆盖内**：该状态源副本已在 OPFS 且不可重建 | 用户在这唯一需要他确认的状态下刷新/关闭 → 丢掉唯一副本（大文件重来） | §6.2 第 7 点改为按语义判据（「产物或源副本只在 OPFS 且未收口」）并显式点名 `need_disk_confirm`；§3.2 补注 |
| R17（本轮评审新增） | **对照测试范围被高估**：`runLocked` 含交互分支，无法逐例比对 | 测试写不出来，或写成假的「等价」断言 | §9.2 第 12 项把对照面限定为 `isDisabled` 结论 + 零创建副作用，并明确列出**不纳入**的分支 |
| R18（本轮评审新增） | **按钮清单在 §2.2 与 §6.3 两处维护会漂移** | 实施者只读改动清单会漏掉唯一的 OPFS 清理入口（本轮 J1 的实例） | §2.2④ 改为「以 §6.3 为唯一权威」的指针，不再复制清单 |
| R19（第三轮评审新增） | **磁盘门确认后误走「复用旧任务」路线**：原稿把 `need_disk_confirm` 的理由写成「副本已在 OPFS」，据此会实现成 `startJob(null,{jobId})` 或复用旧目录 | 复现 `io_recoverable: 缺少输入文件`（`runner.js:867`），确认按钮永远失败 | §6.2 纠正理由 + 新 §6.2.1（带原始输入重新起、会重新复制）+ §7.3 表 + §9.2 第 18 项反向断言 |
| R20（第三轮评审新增） | **目标关联链路在改写中丢失**：`uploadFile` → `enqueue` 时若不再先取快照，或完成时不按冻结快照关联 | 静默未归类，或落到完成时刻的当前文件夹（正是 folder-experience §1.3 要避免的漂移），且既有源码级断言 `viewer-folders.test.ts:1095` 会红 | 新 §3.5（全链路表 + 5 条接线要求）+ §2.2⑪ + §9.1 新增 `viewer-folders` 行 + §9.2 第 19 项 |
| R21（第三轮评审新增） | **磁盘门提醒覆盖不足**：`need_disk_confirm` 若漏注册 `beforeunload` | 用户的选择只在本页内存（`entry.file`/`bundle.rows`），刷新后要重新选择并重新复制（大切片分钟级） | §6.2 第 7 点的语义判据（含 `need_disk_confirm`）+ §9.2 第 17 项（五态逐一断言） |
| R22（第四轮复审新增） | **冻结快照从未被消费**：`uploadFileCos` 调用字面量不含 `assocTarget`（既有缺陷） | 直传条目静默关联到完成时刻的当前文件夹；刷新恢复拿不到快照（`app.js:10789` 被守卫吞、`:10864` 回落 `:11284`） | §3.5 第 2 点改写 + §2.2⑪ + §9.1 新增源码级断言（钉「被传」不只钉「被取」）（第四轮 A1） |
| R23（第四轮复审新增） | **终态行的清理入口不可达**：恢复过滤排除已收口记录，而清理按钮只在那些行上 | OPFS 源副本 + 产物永久残留 → 撞 `quota_exceeded_recoverable`（`_sweepIncompleteStaging` 不兜底） | §6.2 第 2 步改为「取全部、分两组渲染」，§6.3 定义待清理组的最小行（第四轮 A2） |
| R24（第四轮复审新增） | **文件夹两个入口仍弹工具页**：`importPickFolder`（清单从未提及）与目录拖入都无条件 `handoffBundleFolder` | 开关默认开时选/拖文件夹仍弹 `/tools/slides`，与 §0 目标冲突 | §2.2⑩b + §4.1.1 动作表（两处都改为 `enqueueBundle`，开关关闭才交接）（第四轮 A3） |
| R25（第四轮复审新增） | **假 COS 接线写错旋钮**：`Endpoint=<host:port>` 产出坏主机（`probe-…127.0.0.1:9443`），且 `cos_client` 无注入点 | e2e 用例 A 的测试基建做不出来（或做出一个连不上的假 COS） | §9.4 改为测试侧 monkeypatch `cos_client._client` + `CosConfig(Domain=127.0.0.1:<port>, VerifySSL=False)`，并说明不再需要 host-resolver 映射；生产代码零改动（第四轮 A4，实测见 §10.16） |
| R26（第四轮复审新增） | **回退承诺自相矛盾**：「逐字节不变（零改写）」vs 同一设计要改写回退路径用的两条文案，且改后文案在两态下都不成立 | 开关关闭时用户看到「本页面板内自动完成」这类不成立的说明，且无入口 | §8 收紧为「控制流不变」+ 按开关两态的两套文案（`unsupported` 去点名工具页、`convert_in_browser` 仅关闭时追加链接）（第四轮 A5） |
| R27（第四轮复审新增） | **上传槽 account 来源未定义**（bootstrap 无该字段，面板又承诺零网络） | 两个实现分叉（空串 / currentUserId / 自行请求） | §5 明确 `String(window.currentUserId)` 加空串兜底，与 `contentLockName`/`storage.account` 同一取值与退化语义（第四轮 A6） |
| R28（第四轮复审新增） | **「零 /api/ 请求」字面不可满足**（页面自身 5 个 GET） | 新 spec 天生为红 | §9.2 第 9 项与 §9.4 用例 C 改为「零面板发起的新请求」（第四轮 A7） |

## 12. 实施顺序建议

1. `static/upload/inline-convert.js` 判定表 + 状态机（纯逻辑，vitest 先行）——
   含 §4.3 的产物声明算法（先于任何上传接线，否则主链路必红）与 §5.1 的
   门禁作用域。
2. `app.py` capability 两个新字段 + `tests/js`/pytest（§9.2/§9.3）——此时开关
   仍可 close。
3. `app.js` 接线：面板行渲染 + 按钮表（§6.3）、单文件路径、两把锁（§5）、
   刷新恢复（§6.2）、`beforeunload`（§6.2 第 7 点）、终态清理、**目标关联链路（§3.5，五项逐条对）**；回退路径保留不动。
   同一批要落 **§6.2.1 的磁盘门重试语义**（带原始输入重新起）。
4. bundle（MRXS/VMS）路径 + 拖入文件夹（§4.1.1 两种形态）。
5. 失败/申请支持/回队（§7）。
6. i18n（zh/en 同步 + 旧文案改写）。
7. e2e 基建（假 COS + project + spec）。
8. 旧测试按 §9.1 调整。

## 13. 评审处置一览

| 评审项 | 严重度 | 处置 |
| --- | --- | --- |
| 1 产物 `direct_class` 应为 `ome-tiff` | high | **接受**——新增 §4.3（产物嗅探算法）+ 实测证据 §10.11/§10.12 + §9.2 第 6 项反向断言 + §9.4 用例 A 断言；§4.2 `.kfb/.kfbf` 行改为指向 §4.3 |
| 2 `viewable_formats` 误杀直传 | medium | **接受**——新增 §5.1 作用域表 + §9.2 第 5 项两条断言；§2.3 由「四道门禁次序」改为「四道门禁（作用域见 §5.1）」 |
| 3 缺 `uploadLockName` 持有 | medium | **接受**——§5 锁表改写 + 锁定理由（`discardJob`/工具页 `renderJobs`/`onJobAction` 的实际调用链）+ §9.2 第 8 项 |
| 4 刷新路径「重新 probe」不可实现 | medium | **接受**——§6.2 第 5/6 点改为「丢弃 + 重新选择文件」，新增 §6.3 按钮表并逐行标注可实现性依据（`runner.js:854-869`） |
| 5 终态无清理入口 | medium | **接受**——新增 §6.3 的「删除本地产物」行与清理策略说明（含 `_sweepIncompleteStaging` 不清理 ready/upload 记录的依据） |
| 6 散落 bundle 无归宿 | medium | **接受**——新增 §4.1.1（bundle 判定与两种形态的动作表；明确散落 `.mrxs`/`.vms` 在浏览器内**不可能**补齐伴随文件，故给类型化说明 + 选择文件夹） |
| 7 「继续上传」续传语义缺 | low | **接受**——新增 §6.4（`record.upload` 持久化 + `resumeJobId` 续传而非重建 + 孤儿确认）+ §9.2 第 10 项 |
| 8a 「零 runner 调用」表述矛盾 | low | **接受**——§9.2 第 9 项与 §9.4 用例 C 的断言口径改为「零 `/api/` + 零 probe/startJob/resumeJob/createUpload」，并注明 `_init()` 的 OPFS cleanup/sweep 与 worker 启动不算「开始」 |
| 8b 「对照测试」悬空承诺 | low | **接受**——§9.2 新增第 12 项（真 import `tools-slides-upload.js` 的 `runLocked` 判定序列与面板 `decide()` 逐例比对） |

### 13.1 第一轮九条（R1–R8b）

无不同意项；九条全部按评审意见修正（8b 是「补上缺失的测试项」，不是改结论）。

### 13.2 第二轮四条（J1–J4）

| 评审项 | 严重度 | 处置 |
| --- | --- | --- |
| J1 §2.2④ 按钮清单漏「删除本地产物」，与 §3.2/§6.3 不一致 | medium | **接受**——§2.2④ 改为「按钮全集以 §6.3 为唯一权威（含「删除本地产物」）」的**指针**，不再复制清单（避免两处维护漂移，新增 R18）；**并说明一处与评审描述的差异**：评审称第 107 行「不含『删除本地产物』」，实测在**当前工作树与 HEAD（`d2b56235`）**该行**已含**该项（`grep -c 删除本地产物` = 1）——缺项存在于**上一切（`897c7830`）**，即上轮评审读的是旧版。因此本条的**实质要求在上一轮已被满足**，本轮只做「单一权威指针」的加固，不留双份清单 |
| J2 §4.1.1 文件夹分流 vs 散文件判定职责不清（纯 VMS 目录被逐成员上传；.vms 缺成员只有 planner 知道） | low | **接受**——§4.1.1 重写：明确「完整包/缺成员的判定**只归 planner**」（`prepareBundle`→`planBundle`→`sniffMrxBundle`/`sniffVmsBundle`/`planVmsBundle`；`sniffVmsBundle` 实测**不报缺 tile**，逐 tile 核对在 `planVmsBundle:1340-1345`）；面板侧散文件只给**静态**说明（不预取实际缺成员，因那要 `File[]`，与 §6.1 隐私口径冲突）；同时**修既有缺口**：`app.js:11109` 的成员正则补 `.vms`（**不加 `.vmu`**，避免把今天可直传的 VMU 文件夹变成 planner 拒绝——实测 VMU 在 `direct_upload` 受理集内），并加源码级防漂移断言（新增 R15） |
| J3 `need_disk_confirm` 不在 beforeunload 覆盖内 | low | **接受**——§6.2 第 7 点改为**按语义判据**（「产物或源副本只在 OPFS 且未收口」）并显式列出 `need_disk_confirm`；补理由链（`runner.js:734` 写 `staging` → `:772` 写 `prepared` → `:867` 无 File 即 `io_recoverable: 缺少输入文件`）+ §3.2 补注（新增 R16） |
| J4 §9.2 第 12 项「`new Function` 加载 app.js + `import` ES module」形态未说明拆分，且 `runLocked` 含交互分支无法逐例比对 | low | **接受**——§9.2 前言新增「两种加载面」段落（`app.js`/`inline-convert.js` 走 `new Function`；`tools-slides-upload.js` 走顶层 `import`，vitest 单文件内可并存，禁止把 ES module 塞进 `new Function`）；第 12 项改为**限定对照面**：用真 `createUploadController` 注入假依赖，只比对 `ctl.isDisabled(jobId)` 的判定结论与「零创建」副作用（可自动化、且既有 `tools-upload-*` 测试正是这样驱动它），并明确列出**不纳入**的分支（账号 mismatch / 孤儿 / 已发布重放）（新增 R17） |

### 13.3 第三轮三条（K1–K3）

| 评审项 | 严重度 | 处置 |
| --- | --- | --- |
| K1 §6.2 第 7 点「need_disk_confirm 时副本已在 OPFS」的理由与 §9.2 第 18 项「重试不重建任务」 | medium | **接受**——理由**确实错了**（实测 `runner.js:732` 前置门在 `:734` 写 `staging` 之前、`:755` 后置门的 catch 在 `:759` 立即 `_discardNow`，而 `_discardNow` = `:682 removeEntryRecursive`；`:285-288` 的 `_jobDir(jobId,true)` 说明前置门时目录尚未创建）→ 任一磁盘门失败时 **OPFS 里没有可丢的副本**。§6.2 理由改为「用户的选择只在本页内存」，新增 §6.2.1（重试**带原始输入重新起**、分配新 jobId、**重新复制**，与 `tools-slides.js:298-322`/`:564-578` 同形），§7.3 表同步，§9.2 第 18 项改为「重传同一 `entry.file`/`bundle.rows` + 反向断言不得 `startJob(null,{jobId})`」（新增 R19） |
| K2 §2.2① 改 `uploadFile` → `enqueue` 后目标关联链路无落脚点；§9.1 未列 `viewer-folders.test.ts` | medium | **接受**——实测文档 `grep -c "关联" = 0`、同文 grep（关联/importAssociateUploaded/loadAll/openSlide/fbUpAssocSave/fbAssocPendingSave 任一关键词）= 0，而源码链路完整（`app.js:10109`/`:10789`/`:10859-10865`/`:11282`/`:4028`/`:3829`）。新增 §3.5（全链路时点表 + 5 条接线要求：入队前冻结快照、直传沿用 `uploadFileCos`、转换前把目标写进 `record.intent.target`、完成时按冻结快照 `importAssociateUploaded` + `openSlide`、绝不用完成时刻的当前文件夹）；§2.2⑪；§9.1 新增 `viewer-folders.test.ts` 行（`app.js` 源码级断言 `:1095` 不许失效）；§9.2 第 19 项行为断言（新增 R20） |
| K3 §9.1/§9.4 写「现有 15 个 spec」，实际 16 | low | **接受**——`ls tests/e2e/*.spec.ts` 计数为 16（本 worktree HEAD `ec9ee180`，命令见 §10.15），已改两处 |

无不同意项；三条全部按评审意见修正。K1 的理由确实与我上一轮的论证相反——上一轮
我把「`staging` 记录先写、复制随后」的顺序当成了「副本已在」，漏看了前置门在写记录
**之前**、后置门的 catch 立即 `_discardNow`，故本轮改正（这是实质错误，不是表述问题）。

### 13.4 第四轮六条（A1–A7）

| 评审项 | 严重度 | 处置 |
| --- | --- | --- |
| A1 `assocTarget` 未转发给 `uploadFileCos` | medium | **接受，并升级为「必须修」**——原 §3.5 第 2 点把它写成「已做对、不重写」，实测 `app.js:10164-10170` 的字面量无该键（全仓仅 5 处 `assocTarget`），冻结快照被 `:4029` 守卫吞掉、完成时回落 `:11284` 的当前文件夹。§3.5 第 2 点改写为实测说明 + 「面板必须在同一批接线里补上」，§2.2⑪ 标为「一处保留 + 一处必须修」，§9.1 的 `viewer-folders` 行加码为「钉『被传』不只钉『被取』」（新增 R22） |
| A2 恢复过滤与清理入口矛盾 | medium | **接受**——§6.2 第 2 步改为「取全部、分两组渲染」（待继续组 / 待清理组），§6.3 定义待清理组的最小行形态；引 `runner.js:361-366` 说明不会兜底（新增 R23） |
| A3 `importPickFolder` / 目录拖入仍无条件交接 | medium | **接受**——`grep -c importPickFolder` 在原文为 0；两处都改为 `enqueueBundle`（开关关闭才 `handoffBundleFolder`）。§2.2⑩b + §4.1.1 补齐；连带 §9.2 第 16 项的可测性（新增 R24） |
| A4 假 COS 的 `Endpoint` 注入 | low | **接受，但纠正评审的建议项**——`Endpoint=<host:port>` 实测产出坏主机（`probe-1250000000.127.0.0.1:9443`；带 scheme 更退化为 `probe-1250000000.http:`），**正确旋钮是 `Domain`**（path-style，签名与 `Content-Length` 均正常）；注入点只能靠测试侧 monkeypatch `cos_client._client`（生产 `cos_client.py:60-64` 无该参数、无 env，`grep COS_ENDPOINT` = 0），故**生产代码零改动**；且预签名 URL 已指向 `127.0.0.1`，不需要 `--host-resolver-rules`。§9.4 已按此重写（新增 R25，实测见 §10.16） |
| A5 回退承诺与文案改写自相矛盾 | low | **接受**——§8 的「逐字节不变（零删除、零改写）」收紧为「控制流不变」；并规定两条文案按开关两态分写：`upload.err.unsupported`（`app.js:10148`）去点名工具页 + 走申请支持；`upload.cos.err.convert_in_browser`（`app.js:10545`）用中性事实句，仅 `enabled===false` 时追加 `/tools/slides` 链接（新增 R26） |
| A6 上传槽 account 来源未定义 | low | **接受**——§5 明确 `String(window.currentUserId)` 加空串兜底（`app.js:275`），与既有 `contentLockName`（`:10757`）同一取值与退化语义；明确不得为此新增网络请求或读 bootstrap 的 `account`（不存在）（新增 R27） |
| A7 「零 `/api/` 请求」字面不可满足 | low | **接受**——§9.2 第 9 项与 §9.4 用例 C 改为「零**面板发起的**新请求」（页面自身 5 个 GET 与 `SlideToolsRunner.create()` 的本地 OPFS/worker 启动不计），并加「点击后才出现对应请求」的正例（新增 R28） |

无不同意项；六条全部按评审意见修正。A4 是一处**与评审建议不同的技术结论**：
评审建议用 `Endpoint` 注入，实测它产出坏主机（把 endpoint 当域名后缀拼在 bucket 后），
真正可用的旋钮是 `Domain`（path-style）；这一条我按实测写，并给出了备选的 virtual-hosted 方案。
