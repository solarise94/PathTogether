# C4 上传接入 — 验收记录

日期：2026-09-29。验收方：主代理。被验收物：[c4-upload-report.md](../../../slide-tools/c4-upload-report.md)；
C4-1 已先行提交（5a572f5：channel.json 随准备记录持久化 + 结果报告显示窗口）；本记录覆盖 C4-2..5：
共享 COS 上传引擎 `static/upload/cos-uploader.js`、工作台适配（`static/app.js`、`templates/index.html`）、
工具页上传控制器 `static/tools/tools-slides-upload.js`、runner 上传记录/删除保护、能力端点
`GET /api/tools/slides/upload-capability`、页级 CSP 放行 COS origin。

> **复审（2026-09-29）**：用户对 f69cc58 复现两处缺陷——[P1] 记录写入失败仍传输（强制写失败下
> 两次尝试建两个服务端任务并发出两份文件）；[P2] 等待期间取消 `done` 挂起、上传锁不释放。修复与
> 回归见 §7；两项产品问题已裁决（§6）。本记录的数字已按复审修复后复跑更新。

结论：**C4 通过（本机可验部分）。** 用户给定的顺序与六类用例全部有实跑证据；验收中修正 3 处缺陷、
补 2 个 e2e 场景与 2 条单测（§3）后全部门禁重跑通过。真实 COS 桶上传、真实桶 CORS 与真实会话超时
时序是外部门禁（§5），不宣称。后端转换退役仍按 C6/C7 排空与验收门禁执行，本阶段不删任何后端路径。

## 1. 用户指定的用例

| 用例 | 场景 | 结果 |
|---|---|---|
| 登录过期 | e2e b（sign 401 注入）、j（清除会话 cookie → 真实能力端点 401 → 真实重新登录） | 不自动跳转、给 `/login?next=/tools/slides` 链接、产物 sha 不变；登录后继续**同一** ingestion（creates=1）→ 发布 |
| 上传中刷新 | e2e c、j | 列表展示上传状态 + 「继续上传」；只补签/补传未确认分块（`[[3,4]]`） |
| 重复点击 | e2e d、k | 上传三连击 + 继续两连击 creates=1；另一标签页继续上传被 Web Lock 拒绝并提示 |
| 完成响应丢失 | e2e e | complete 网络失败 → 限速重发 → 409 `ingestion_state_conflict` 视为已完成 → 发布（completeReqs=2） |
| 超限输出 | e2e f | 按最终文件大小判定；禁用 + 原因；零 ingestion 调用；本地保存仍可用 |
| 不支持查看的格式 | e2e g | 按核心 `result.format` 与服务端表判定（非扩展名）；禁用 + 原因；零 ingestion；保存可用 |

另：a-bf/a-fl 正常发布（文件名 `.tif` / `.ome.tif`，PUT 字节总数 = 产物大小）；h 取消与终态失败保留产物、
显式重试才新建；i 点击前 0 `/api/`、0 跨源，点击后只有能力端点、`/api/ingestions*` 与 COS origin，
非 COS 请求体无产物字节。每个场景断言 OPFS 产物 sha256 全程不变。**C4 e2e 12/12**。

## 2. 顺序项核对

1. channel.json 先于转换持久化 —— 5a572f5（C3 e2e n1/n2）。
2. 共享上传器 —— 审查方把 HEAD 版 app.js/index.html 临时放回工作区独立捕获「重构前」请求序列，
   与重构后逐条一致（14 请求，`results/workbench-seq-{before,after}.json`）；既有 vitest 断言零改动
   （唯一 harness 改动：先加载共享引擎源）。
3. 资格判定 —— 只接 ready/exported；按 `slice()` 分块读 OPFS 产物；上限来自能力端点；可查看性按
   `result.format` 且须有平台读取器证明（pytest：原生 CLI 合成产物经 `slide_io.open_slide` 读区域，
   荧光逐通道；format 常量与核心报告绑定）。审查方另以真实样本复核：KFB-1 输出 OpenSlide 9 层、
   KFBF-A 输出 TiffFileSlide 17 层 6 通道可读（仅别名，未入库）。
4. 上传失败保留本地结果 —— 记录写在 OPFS 任务记录（`record.upload`），同一 ingestion 重试；
   上传中持锁禁删；发布（viewable）后才显示成功。
5. 隐私/CSP —— 点击前零网络（C3 j/k 与 C4 i）；`connect-src 'self'` + 唯一
   `https://<bucket>.cos.<region>.myqcloud.com`（缺配置/非法值不追加，pytest 逐 token）；
   真实 Flask CSP + 假 COS origin 跑 e2e（page.route 晚于 CSP 检查）。

## 3. 验收中修正的缺陷

1. **遗留上传记录永久阻止删除**：发起上传的标签关闭后，记录停在 uploading，删除被拒；解除只能
   「继续→取消」，而继续需要登录——未登录用户永远删不掉本地任务。改为上传期间持有
   `slide-transform:upload:<jobId>` Web Lock：取不到锁即拒删（`lockHeld:true`）；取得锁但记录未收口
   时由用户二次确认「放弃上传」（尽力 POST cancel）后 `discardJob(id,{abandonUpload:true})`。
   同一把锁让跨标签重复上传被拒。e2e k 覆盖。
2. **记录落盘不等待**：引擎创建 ingestion 后调用 `storage.save` 不等待异步（OPFS）写入即开始
   轮询/签名/PUT，此刻刷新会留下无本地记录的服务端任务，再点就建第二个。改为等待 save 的 Promise；
   取消后迟到完成的 PUT 不再写回已移除的续传记录（工作台 localStorage 同样受益）。原单测对
   「save 先于 PUT」是空断言；新增两条单测，已验证在旧实现上失败、新实现通过。
3. **e2e 运行器**：存在旧 creds 文件时不起服务，导致全部场景连接被拒。改为默认自起服务。

另：荧光读取证明原先在读取器缺 `channel_count` 时会跳过通道检查仍记为证明，已改为必须读到通道；
无整文件物化门禁扩展到上传相关 3 个文件。

## 4. 复跑结果（审查方，2026-09-29）

| 门禁 | 结果 |
|---|---|
| pytest（RERUN §1 七个文件） | 112 passed；另 `test_demo_access`/`test_stage2_ui`/`test_slide_format_registry` 同批 205 passed |
| vitest `tests/js` | 37 文件 / 562 passed（基线 36/548） |
| C4 e2e | 15/15（复审后） |
| 工作台请求序列 | 重构前（审查方独立捕获）== 重构后 |
| C3 e2e | 18/18 |
| C2 故障矩阵 / no-whole-file | 26/26 / PASS |

已知无关失败：`tests/test_ai_budget_wiring.py::test_ui_budget_card_and_max_steps_sync_present`
读他人未提交的 admin 插件 manifest 版本（0.4.13 ≠ 0.4.12），不在本阶段文件内。

## 5. 外部门禁（未完成，不宣称）

- 真实 COS 桶上传（真实签名 URL、分块并发、断流重试时序）与桶 CORS 配置——不产生云费用前提下本机不可验。
- 真实会话超时时序与生产 cookie 属性。
- 真实系统保存对话框（C3 遗留）；Firefox/Safari/Edge；真实 4/8 GB 设备（C7）。
- 生产发布：页面 CSP 放行的 COS origin 取自部署环境 `COS_BUCKET`/`COS_REGION`，上线前需在生产响应头复核。

## 6. 产品裁决（用户，2026-09-29）

1. 工具页上传不带项目归属：接受；项目归属沿用工作台既有流程。
2. 遗留未完成上传删除时保留二次确认；文案改为如实说明——删除后无法再继续这次上传，页面会请求
   平台取消，未登录或离线时这个请求可能送不到（不承诺一定取消）。

## 7. 复审修复与回归

| 项 | 修复 | 回归（修复前 f69cc58 上失败 → 修复后通过） |
|---|---|---|
| P1 记录写入失败仍传输 | 写队列如实返回成败；引擎创建后首次落盘失败 = 致命：零状态查询/签名/PUT，立即取消新任务，`{persist, ingestionId, reconciled}`；未确认取消的任务记为待取消（内存 + localStorage），确认取消前同一任务不新建 | 单测 2 条；e2e l：强制 `job.{a,b}.json` 写失败（真实工具页 OPFS 适配器）→ 两次尝试各建 1 个任务且均被取消、0 签名/0 PUT；取消 503 → 下一次点击只重试取消、不新建；取消成功 + 写恢复 → 发布，PUT 字节 = 产物大小，待取消键清空 |
| P2 等待中取消挂起 | 取消以 cancelled 结束所有进行中的等待；`done` 立即收口（race）；取消后控制请求一律拒发 | 单测 3 条（waiting_space 轮询、429 退避、控制请求悬置）；e2e m1/m2：取消 ~0.23 s 收口，A 标签仍开着时 B 标签删除成功（仅一次删除确认），此后 6 s / 4 s 内零控制请求 |

复跑：vitest 37/562；pytest 112；C4 e2e 15/15；工作台请求序列 == 重构前；C3 e2e 18/18；
no-whole-file PASS。runner 本轮未改动（C2 故障矩阵沿用 26/26）。
