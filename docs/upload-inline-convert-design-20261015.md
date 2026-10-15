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
| `static/app.js` | ① `uploadFile`（:10104）→ 交 `HP_INLINE_CONVERT.enqueue`；② `uploadFileClassified`（:10128）→ 判定表驱动（直传 / 转换 / 当场拒绝）；③ `offerBrowserConvert`（:10326）/`openConvertHandoff`（:10247）/`handoffBundleFolder`（:11123）保留但**仅在 `inline_convert.enabled=false` 时可达**（回滚路径，零删除）；④ 新增面板内行的渲染与按钮（转换进度/取消/继续/申请支持）；⑤ 新增 runner 动态 import 与生命周期（`beforeunload`）；⑥ 新增刷新恢复：`listJobs()` 里带 `intent.channel==='workbench-inline'` 的未完成任务 → 行 + 「继续」；⑦ `HP_UPLOAD` 导出面补新入口（测试驱动）；⑧ `submitFormatRequest`（:11530）抽出可复用内核 `submitFormatRequestCore({ext,message,contact,sampleFile})` |
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
- 上传控制器语义照搬 `tools-slides-upload.js:667 runLocked` 的**四道门禁次序**
  （能力 → 账号 → 最终产物大小 `max_size_bytes` → `viewable_formats`）与
  `opfsStorage`（:330）的 `setJobUpload` 适配器写法；面板不 import 该模块
  （它是 ES module + 工具页 DOM），而是把同一套判定抽进
  `inline-convert.js` 的纯函数，等价性由 §9 的对照测试锁定。

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
- `converted` 不是终态：立刻转 `upload_queued`（不自动上传？**自动上传**——
  见 §3.4）。
- `done` / `rejected` / `convert_failed` / `upload_failed` / `cancelled` 为终态；
  终态行的按钮按 §6。
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
`cls==='convert'` 时用引擎嗅探确认：

- TIFF 容器（`engine.isTiffHeader`，`engine.js:400`）→ `await sniffTiffSlideCapability(file)`
  （`engine.js:578`）；
- 普通图片（`engine.isRasterHeader`，`engine.js:384`）→ `await sniffRasterCapability(file)`
  （`engine.js:811`）；
- KFB/KFBF → `engine.magicSupported`（`engine.js:405`，8 字节魔数）。

`supported===true` → `needsConvert=true`（并且把引擎给出的 `modality` 用于
输出格式默认：明场 `bf-ome`、荧光 `fl-ome`，见 §4.3）。
`supported===false` → **不转换**，按 §4.2 的「引擎拒绝分支」处理
（说出 `cap.reason`，`direct_import` 开放的重放直传，关闭的不上传 + 申请支持）。

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
| `.kfb` | `convert` | `magicSupported` 真 | **转换**（明场 → `bf-ome`/`bf-classic`） | — | 创建闸 `conversion` 分支 422 `convert_in_browser`（`app.py:12440`）；转换产物经 `converter-bigtiff` 声明放行 |
| `.kfbf` | `convert` | 真 | **转换**（荧光 → `fl-ome`，保留画质） | — | 同上 |
| `.svs`（IFD0 压缩 7） | `convert` | `sniffTiffSlideCapability` 真 → `aperio-svs-jpeg` | **转换** | — | 直传关闭（`slide_format_registry.py:207`） |
| `.svs`（33003/33005） | `svsJp2k`（`temporary`） | （不适用） | **直接上传原文件** | `unconverted-variant:svs-jp2k` | 词表 ↔ 扩展名匹配 + 第 0 层压缩核验（`upload_direct_class.declaration_matches`，`.py:348`） |
| `.tif/.tiff`（OME-XML 描述） | `ome-tiff` | （不适用） | **直接上传** | `ome-tiff` | 描述必须含 OME-XML（`.py:371`） |
| `.tif/.tiff`（转换器来源标记） | `converter-bigtiff` | （不适用） | **直接上传** | `converter-bigtiff` | 描述 JSON `source_format ∈ CONVERTER_SOURCE_FORMATS`（`.py:238/_converter_marked`） |
| `.tif/.tiff`（通用瓦片 JPEG） | `convert` | `sniffTiffSlideCapability` 真 → `generic-tiled-jpeg-tiff` | **转换** | — | 直传仍开放（`direct_import=open`） |
| `.tif/.tiff`（条带/LZW/deflate/非 8 位/多通道） | `temporary` | （不适用） | **直接上传** | `legacy-direct` | `legacy-direct` 只对 `direct_upload` 集（`app.py:3174`） |
| `.tif/.tiff`（+ 引擎判不支持） | `convert` 但引擎拒 | `supported:false` | 不转换；`legacy-direct` 直传（服务端 open_slide 终审） | `legacy-direct` | worker：`enforcement_failure` 若内容是 Aperio JPEG SVS → `convert_in_browser` → §6 回队 |
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
  （`slide-sniff.js:821-824` 的收口分支）。**转换条目绝不使用该值**——转换后
  上传的声明由嗅探产物字节得出（`converter-bigtiff`）；表里的「—」就是这个
  意思。若把 `legacy-direct` 发给 `.svs`，服务端会 422
  `convert_in_browser`（`app.py:3167`）。
- `.svs`（JPEG）在 `classifyExt` 走 tiff 路由、最终由 `decideTiff`
  （`slide-sniff.js:264`）判 `convert`；`.svs`（33003/33005）判 `temporary`
  且 `directClass='unconverted-variant:svs-jp2k'`（`slide-sniff.js:256`）——
  与表一致。

### 4.3 输出格式 / 画质默认（不做成可选项）

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
| 单任务产物读取 | `engine.uploadLockName(jobId)`（`engine.js:38`）由 `discardJob`（`runner.js:659`）自持 | **沿用**（上传中禁止 discard） |

`pt:upload-slot:<account>` 语义：

- `navigator.locks.request('pt:upload-slot:' + account, { ifAvailable: true }, …)`
  （与 `engine.uploadLockName` 同一原语）；拿不到 → 行显示「另一标签页正在上传」
  （`upload.cos.other.tab` 与工具页 `tools.upload.other.tab` 同义），**排队等待**
  而非失败（用 `ifAvailable:false` + `AbortController` 取消，写法照
  `acquireContentLock`）；
- 上传收口（成功/失败/取消）立即释放，下一 entry 顶上；
- `navigator.locks` 不可用（旧浏览器/非安全上下文）→ `held:false` 照常执行
  （与既有 `acquireContentLock` 的退化语义一致，绝不阻塞）。

**ingestion 创建时机**：只有拿到上传槽 + 四道门禁通过后才 POST `/api/ingestions`
（`cos-uploader.js:1033`）。转换期间（含刷新、崩溃）服务端零任务。

## 6. 刷新 / 续转

### 6.1 持久化面

| 数据 | 存放 | 依据 |
| --- | --- | --- |
| 转换任务（源副本、日志、产物） | OPFS `slide-jobs/<jobId>/` | 运行器既有 |
| 上传意图（授权账号 + 目标 + 渠道） | OPFS `record.intent`（`runner.setJobIntent`，`runner.js:634`） | R1 既有；本设计写 `channel: 'workbench-inline'` |
| 直传任务的续传记录 | localStorage `pt.cos.jobs`（`app.js:10383`） | 既有 |
| 目标快照 | localStorage `pt.fb.upassoc.v1:<uid>`（`app.js:4028`） | 既有 |

**不新增任何 localStorage 键**用于转换恢复：恢复的唯一权威是 OPFS 任务记录
（含 `intent`），这与「绝不凭名称/大小把浏览器文件当同一份」的隐私约定一致
（`app.js:10404` 附近注释）。

### 6.2 刷新后的行为（沿用现有隐私约定：不自动开始）

1. 页面加载：`HP_INLINE_CONVERT` **不做任何网络请求、不自动开始转换或上传**。
2. `runner.listJobs()`（`runner.js:1355`）→ 过滤 `intent.channel === 'workbench-inline'`
   且 `state ∈ {prepared, planned, paused, failed}` 或
   `state ∈ {ready, exported} && upload 未收口`的记录。
3. 每个命中 → 行显示「未完成的转换：<名> · 已提交 X / 共 Y」+ 按钮：
   - `nextAction==='resume'`（有 journal）→ 「继续转换」（`resumeJob`，不重选文件）；
   - `nextAction==='start'`（prepared/planned 无 journal）→ 「继续转换」（`startJob(null,{jobId})`）；
   - `ready/exported` → 「继续上传」（跳过转换，直接进 `upload_queued`）。
   映射取自 `runner.js:1310-1317` 的 `nextAction` 计算。
4. `resumeJob` 的断点续转不需要重选文件：源副本是 OPFS 里的 `source.bin`，
   运行器 `_startOrResume` 会 `verify-source`（`runner.js:952`）——源变了就
   `source_changed_refuse_resume`。bundle 走 `verify-bundle`
   （`engine.expectedBundleIdentity`，`engine.js:1425`）。
5. 版本/设置不符的续跑拒绝（`resume_refused` 的 `kind`：`core-version` /
   `profile` / `policy` / `output-profile` / `encoding-profile` /
   `channel-json` / `source-adapter`，`runner.js:886-1027`）→ 行内说明 +
   「重新开始转换」（丢弃旧任务 + 重新 `probe`），**绝不静默改设置续跑**。
6. `job_dir_missing`（目录被浏览器驱逐，`engine.js:19`）→ 行内说明 +
   「重新选择文件」。
7. `beforeunload`：只要有 entry 处于 `converting` 或 `uploading`，注册
   `preventDefault()` 提醒（照 `tools-slides.js:1772` 的写法）。

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
| `disk_precheck_failed` + `uncertain:true` | 「浏览器只报告了 ~X 可用，本任务预计需要 ~Y」 | **「继续（我已确认）」/「取消」**（`confirmUncertainDisk:true` 重试；`runner.js:692-699` 语义）。用户确认后重启 `probe` |
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
  弹窗交接，即 upload-liveness 的行为**逐字节不变**（零删除、零改写）。
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
| `tests/js/cos-upload-liveness.test.ts` | 魔数 fail-fast（PNG 改名 .jpg）在面板内**同样**生效且零网络——语义不变 | 无需改；新增面板内该路径的行文案断言 |
| `tests/js/raster-image-compat.test.ts` | 只测 `acceptFromCatalog` / fallback 目录；不涉上传分流 | 不影响 |
| `tests/browser/slide_tools_r1/run_e2e.js` i/i2/i3/i4/l2/l3 | i 系列依赖**工作台弹窗交接**（`context.waitForEvent('page')`） | 「工作台入口」场景需在开关关闭下跑（或重写为面板内）；工具页场景不受影响。这不是 pytest/vitest，按 §9.4（Playwright 侧）处理 |
| `tests/browser/slide_tools_c4/run_workbench.js` | 原生 `.tif` 直传、请求序列逐条比对 | **已实测失效（非本设计引入）**：该脚本的 64 B `wb-regression.tif`（字节 `0..63`，`run_workbench.js:51-55`）经 `f8f130c2` 的 fail-fast 魔数闸判为 `{ext:'.tif', expected:'tiff', detected:'unknown'}`；在真实 app.js + 真实引擎上实测：**零 `/api/ingestions`、零 COS 请求，行停在「上传失败」**（命令与输出见 §10.7）。`dca42ac2` 只修了 `slide_tools_c4/run_e2e.js` 的夹具，未动本脚本 | 换真 TIFF 头夹具（属既有测试维护）；本设计的面板路径同样受该闸约束，故 §9.4 的新 spec 一律用真 TIFF/BigTIFF 头 |
| `tests/test_ingestion_api.py:483` / `test_r1_conversion_gate.py:77` | 断言 `_cos_upload_capability_payload` 的字段/词表 | 只新增键，现有断言（含 `browser_convert` 集合 `:93`、`direct_upload` 集合 `:98`）不变；**无**测试断言精确键集合（已核） |
| `tests/test_slide_tools_upload_capability.py:235/276/315` | 读 `body["cos_upload"]`，无键集合断言 | 不变 |
| `tests/test_slide_tools_page.py:72-113` | `/tools/slides` 的 CSP 逐 token 精确匹配 | **不受影响**（本设计不改 `_slide_tools_csp`） |
| `tests/e2e/*`（现有 15 个 spec） | 都不断言工作台上传行为；`e2e_server.py` 不配置 COS（capability off） | 不受影响；新增 spec 需要 COS 配置，见 §9.4 |

### 9.2 vitest（新增 `tests/js/inline-convert.test.ts`）

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
5. 转换完成自动上传、无第二次确认；大小/格式门禁失败 → 不建 ingestion
   （断言 `/api/ingestions` 调用数 0）。
6. `convert_in_browser`（`createNewJob` 422）→ 自动回队**恰一次**；第二次 → 终态 + 申请支持按钮。
7. 上传槽：假 `navigator.locks` 记录名字，断言 `pt:upload-slot:<acct>`
   被获取/释放；同号第二次请求排队（不并发两个 `createUpload`）。
8. 刷新恢复：`listJobs()` 返回带 `intent.channel==='workbench-inline'` 的
   `planned+journal` / `ready` 记录 → 行 + 「继续」按钮；**加载期间零
   fetch / 零 runner 调用**（不自动开始）。
9. 申请支持：按钮 → `POST /api/format-requests`，body 含预填 `format_ext` 与
   带机器码的 `message`；202 显示编号；429 → 今日上限文案；413 → 样本超限。
10. i18n：新键 zh/en 成对存在（静态扫描 `i18n.js`），`_EXTRA_I18N` 同步；
    **反向断言**：`upload.cos.err.convert_in_browser` / `upload.err.unsupported`
    的 zh/en 文案里不再出现 `/tools/slides`（§7 的改写）。
11. 回退：`inline_convert.enabled=false` → 走 `offerBrowserConvert`，
    断言 `window.open` 收到 `/tools/slides`（与 `r1-convert-upload.test.ts`
    现有断言同形）。

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
| A | 「转换后上传且上传字节能被平台读取器 `slide_io.open_slide` 打开」 | 真 KFB 夹具（`kfb.fixture.build_synthetic_kfb`，或 CLI `gen-kfb`）→ 工作台拖入 → 面板内转换（真 wasm）→ 上传 → 行到「入库完成」→ 服务端用 `slide_io.open_slide` 打开**已发布**资产并读到层级/尺寸/区域像素 |
| B | 「PNG 改名 `.jpg` 当场被拒且申请支持提交成功」 | 真 PNG 字节 named `x.jpg` → 行内出现检测类型 + 申请支持按钮 → 点击 → `POST /api/format-requests` 202 → 行内出现申请编号；**全程零 `/api/ingestions`、零跨源 PUT** |
| C | 「刷新后点继续能把转换做完」 | 转换进行中 `page.reload()` → 行显示「继续转换」且**不自动开始**（断言 reload 后若干秒内零 `/api/` 与零 runner 进度）→ 点「继续」→ 转换完成 → 产物 sha256 与 OPFS 产物一致 → 上传完成 |

为让 A 的「上传字节被平台读取器打开」真成立，需要（**本设计的关键新增测试基建**）：

- `tests/e2e/e2e_server.py` 增加 COS 支持（默认关，`E2E_INLINE_CONVERT=1` 时开）：
  - 设 `COS_BUCKET`/`COS_REGION`/`COS_SECRET_ID`/`COS_SECRET_KEY`、
    `COS_UPLOAD_CAPABILITY=on`、池容量 > 产品上限（照
    `tests/browser/slide_tools_c4/server.py:110-118` 的假 COS 配置与
    `app_mod.UPLOAD_PRODUCT_MAX_BYTES = 900_000_000`）；
  - **进程内假 COS**：一个真实本地 HTTPS 端点实现 `InitiateMultipartUpload` /
    `UploadPart` / `CompleteMultipartUpload` / `ListParts` / `GET(Range)`，
    对象落临时目录；服务端 `cos_client._client()` 的 `CosConfig` 以
    `Endpoint=127.0.0.1:<port>` + `VerifySSL=False` 指向它（`CosConfig` 接受
    这两个参数，已核实签名含 `Endpoint`/`VerifySSL`）；
  - 浏览器侧用 Chromium `--host-resolver-rules=MAP <bucket>.cos.<region>.myqcloud.com 127.0.0.1`
    + `--ignore-certificate-errors`（**已在 C4 套件验证过的手法**，
    `tests/browser/slide_tools_c4/lib.js:285` 与 `run_workbench.js:129-131`），
    于是分块 PUT 走真实 socket、真实字节，服务端 worker 从同一个假 COS
    下载后 `open_slide` 校验——**不用 `page.route` 造字节**；
  - 新增测试专用只读端点 `GET /__test/slide-probe?slide_id=<id>` 返回
    `{reader, levels, width, height, mpp}`（内部 `slide_io.open_slide` 真读）。
- `playwright.config.ts`：新增 project `chromium-cos`（`testMatch:
  'upload-inline-convert.spec.ts'`、带上述 launch args），并在默认 project 上
  `testIgnore` 该文件，避免其余 15 个 spec 被 launch args 影响或重复跑。
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

## 11. 风险

| # | 风险 | 影响 | 缓解 |
| --- | --- | --- | --- |
| R1 | **侧栏空间不足**：现有行是 `makeUploadRow`（`app.js:9894`）的 4 段（名/进度条/状态/字节）+ 按钮，面板内要容纳「转换阶段 + 「请保持页面打开」 + 取消 + 驾驶舱级说明」 | 窄屏溢出（`viewer-folder-experience` 类回归） | 复用不定态进度条；说明文字折行 + `title`；Playwright 加 390×900 视口断言（照 `slide-tools-save.spec.ts:95` 的手机不溢出写法） |
| R2 | **`viewable_formats` 新增到 bootstrap 载荷**属产品级能力面变更，`/app` 的 HTML 会变大且含格式 id 列表 | 极小；但缓存 JS 与新载荷的时序（旧 JS 不读新键） | 新键纯增量；前端 fail-closed（缺字段 → 不自动上传） |
| R3 | **动态 import 的 URL**：`import('/static/tools/slide-transform/runner.js')` 是硬编码路径；部署前缀变化即失效 | 面板整体不可用 | 路径写成单一常量并在 pytest 里断言文件存在；import 失败 → 行内说明 + 回退到手交（开关关闭路径） |
| R4 | **e2e 假 COS 基建**（§9.4）是新增量：本地 HTTPS + DNS 映射 + `cos_client` 注入 | 若做不成，A 用例只能降级到 pytest 证明（`test_bf_ome_platform_chain.py` 已是既有证据） | 先做 pytest 侧证明（转换产物 → 上传字节 → `open_slide`），e2e 用力争；做不成时如实上报，不伪造 |
| R5 | **自动回队死循环**：`convert_in_browser` ↔ `temporary` 互推 | 无限循环 + 网络噪声 | 同 entry 最多回队 1 次（§7.2） |
| R6 | **磁盘/配额**：面板内转换把源副本 + 产物都放 OPFS，大文件（5 GiB KFB）在浏览器的 `uncertain` 报告下需要用户确认 | 用户看到额外弹层（与工具页一致） | 行内确认而非模态弹层；文案给出 need/available 两个数（`runner.js:697`） |
| R7 | **两处默认输出格式分歧**（用户口径 `bf-classic` vs 工具页默认 `bf-ome`） | 产物扩展名 `.tif` vs `.ome.tif`、mpp 元数据有无 | 记为待确认项（§4.3）；改一个常量 |
| R8 | 旧文案改写（去掉 `/tools/slides` 指引）会同时改掉工具页可见文案 | 工具页用户看不到「用转换工具」提示（该页不需要） | 改写为「在本机转换（本页面板内自动完成）」；工具页有自己的输入提示文案，不受影响 |
| R9 | `tests/browser/slide_tools_r1/run_e2e.js` 的 i/i3/i4/l2/l3 依赖弹窗交接 | 这些浏览器回归在开关默认开后失效 | 明确这些场景只在 `inline_convert.enabled=false` 下有意义，脚本内显式注入关闭；新面板行为由 §9.4 新 spec 覆盖 |
| R10 | `run_workbench.js` 的 64 B 假 `.tif` 与步 2 魔数闸冲突 | **已实测失效**（§10.7）：零 ingestion、零 COS、行停在「上传失败」 | 换真 TIFF 头夹具（既有测试维护，非本设计引入）；面板路径同样受该闸约束，故新 spec 一律用真头夹具 |

## 12. 实施顺序建议

1. `static/upload/inline-convert.js` 判定表 + 状态机（纯逻辑，vitest 先行）。
2. `app.py` capability 两个新字段 + `tests/js`/pytest（§9.2/§9.3）——此时开关
   仍可 close。
3. `app.js` 接线：面板行渲染、单文件路径、刷新恢复、`beforeunload`；回退路径
   保留不动。
4. bundle（MRXS/VMS）路径 + 拖入文件夹。
5. 失败/申请支持/回队。
6. i18n（zh/en 同步 + 旧文案改写）。
7. e2e 基建（假 COS + project + spec）。
8. 旧测试按 §9.1 调整。
