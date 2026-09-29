# C3 独立工具页 — 验收记录

日期：2026-09-29。验收方：主代理。被验收物：[c3-tool-page-report.md](../../../slide-tools/c3-tool-page-report.md)、
`/tools/slides`（`app.py` 路由/放行/页级 CSP/wasm MIME、`templates/tools_slides.html`、
`static/tools/tools-slides.{js,css}`、`static/i18n.js` 新键、`templates/entry.html` 入口）。

结论：**C3 通过，允许进入 C4。** 按用户要求先验本地保存与隐私，两项均有独立复跑证据；
验收中修正 5 处缺陷（§3）后全部门禁重跑通过。外部门禁未完成（§5），不宣称。

## 1. 本地保存（优先项）

| 检查 | 方法 | 结果 |
|---|---|---|
| 保存字节 = 原生转换 | e2e a/b/c/k/m + 真实样本 + 9.8 GiB 页面全流程 | 全部 sha256 相等；KFB-1 `385a59c6…`；9.8 GiB `2084a333…` |
| OPFS ≠ 已保存到磁盘 | 代码 + 截图 | 结果区显式警示「还不是已保存到磁盘，可能被驱逐」；列表状态「已完成（未保存到磁盘）」 |
| 临时空间说明 | 截图 + 对话框文案 | 「源文件副本 + 输出文件」分项与合计；uncertain 对话框给出报告值与需求值 |
| uncertain 确认 | e2e d1/d2 + 验收方 9.8 GiB 新 profile | 对话框（10.00 GiB 报告 / 19.62 GiB 需求）→ 取消零残留 / 确认后全量完成 |
| 保存不可用 | e2e i | 按钮禁用 + 说明；0 次 createObjectURL、无 `a[download]`（无整文件下载兜底） |
| 保存失败不删产物 | C2 矩阵 export 场景（runner 同一路径） | 产物保留、可重试 |
| 刷新续跑 / 废弃清理 | e2e c/f + C2 矩阵 26/26 | 无需重选文件；删除后任务目录消失 |

## 2. 隐私（优先项）

- 网络捕获（浏览器 context 级，含 worker 请求）：全流程 10 个请求 = 页面 + 9 个同源 `/static/`
  GET；0 POST/PUT、0 `/api/`、URL 中无文件名/哈希；无下载。
- 离线：页面与引擎加载后 `setOffline(true)`，转换+保存成功，离线期间网络尝试 0。页面文案只承诺
  「加载完成后可离线使用」，与测试范围一致（无 service worker，离线刷新不可用）。
- 代码审查：`tools-slides.js` 无 fetch/XHR/beacon/WebSocket/createObjectURL/存储外传；
  channel.json 以 ≤1 MiB 有界 slice 本地读取；模板仅加载同源静态资源，无 inline 脚本/样式。
- 页级 CSP 与规格逐 token 一致（真实 Flask 响应），不追加全站 `CSP_EXTRA_CONNECT_SOURCES`；
  无 COOP/COEP；全站头未改。服务端匿名访问计数只看到页面 GET（与主页同口径）。
- 部署：生产 nginx 全量反代到 Flask，不覆盖 MIME/CSP（只读核对 `deploy/*.conf`）。

## 3. 验收中修正

1. 完成后「取消」按钮仍可见：`.btn{display:inline-block}` 压过 `hidden` → `[hidden]{display:none!important}`。
2. 复制完成后进度条停在最后一次事件（小文件 0%）→ 完成时置 100%。
3. runner：`done` 在任务记录更新、释放锁前即 resolve，导致完成/失败后列表仍显示「已排程/进行中」
   → 记录写入并释锁后再 resolve（C2 矩阵、C3 e2e 全部重跑通过）。
4. runner：列表对「planned 无 journal」任务给出「开始」，但 `startJob(null,{jobId})` 会报缺少文件
   → 允许从副本全新开始；已有 journal 时要求续跑（新增矩阵场景，26/26）。
5. 列表导出荧光产物建议名 `.tif`、无类型过滤 → `.ome.tif` + TIFF 过滤；完成任务不再显示「已提交 0 B」。
6. 撤回子代理对 zh `activate.footer`（「和→与」）的越界修改——会与 `activate.html` 不一致。

e2e 场景 a 新增完成态断言（取消不可见、复制进度 100%、列表 nextAction=export），防止回归。

## 4. 独立复跑

| 项 | 结果 |
|---|---|
| `pytest tests/test_slide_tools_page.py tests/test_phase1_auth_ui.py tests/test_slide_id_review_r16_homepage.py` | 65 passed |
| 路由/鉴权/CSP 相关 18 个测试文件（排除他人在改的 4 个文件） | 550 passed |
| `run_e2e.js` | E2E ALL PASS（16 记录） |
| `run_real_sample.js`（KFB-1） | REAL SAMPLE PASS |
| `run_large_page.js`（9.8 GiB） | LARGE PAGE PASS |
| C2 `run_smoke.js` / `run_faults.js` | SMOKE PASS / 26/26 |
| 视觉审查（8 张状态截图，验收方目检） | 修正上述 1/2/3/5 后通过 |

## 5. 未完成 / 外部门禁

- 真实 `showSaveFilePicker` 写用户磁盘（e2e 用 OPFS 句柄替身，字节路径同一 `exportJob`）。
- `persist()` 真实手势；Firefox/Safari/Edge；真实 4/8 GB 设备（C7）；>4.9 GiB 真实厂商样本。
- 已知小项：刷新后从列表开始一个未启动的荧光 `prepared` 任务，不会带回刷新前选的 channel.json。

## 6. 转交 C4

- 上传只接受 `ready`/`exported` 产物；产物 File 取自 OPFS `output.tif`，交给现有 COS 分块上传器，
  不得 `arrayBuffer()` 整体物化；登录过期不得丢本地产物；限额按最终文件大小。
- 工具页与平台支持能力分离：工具可产出超过平台上传上限的文件，此时上传入口禁用并说明。
