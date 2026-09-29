# C3 独立工具页报告（/tools/slides）

日期：2026-09-29。对照计划：`docs/browser-slide-tools-and-baidu-plugin-agent-plan-20260929.md`
§2（产品流程）、§4（资源档位）、§5（浏览器 IO，含 2026-09-29 源复制裁决）、§6（隐私/CSP）、
§9 C3、§10.2–10.3。前置：C2 运行器（`docs/slide-tools/c2-runner-report.md`）。
复跑命令：`docs/review-evidence/slide-tools/C3/RERUN.md`；证据 JSON：同目录 `results/`。

> 撰写纪律：下文全部 PASS/数字均来自 2026-09-29 实跑（命令见 RERUN.md），
> 截图与原始 JSON 在 `.gate-tmp/slide-tools-c3/`（不入库，复核后清理）。

> **验收修订（2026-09-29，验收方）**：审查截图与代码后修正 5 处并重跑全部门禁——
> ① `.btn{display:inline-block}` 压过 `hidden` 属性，完成后「取消」仍显示（加 `[hidden]{display:none!important}`）；
> ② 复制完成后进度条停在最后一次 256 MiB 事件处（小文件 0%）→ 完成时置 100%；
> ③ runner `done` 在任务记录更新与释放锁**之前**就 resolve，完成后列表仍显示「已排程/进行中」→ 改为记录写入并释锁后再 resolve；
> ④ 列表对「planned 但无 journal」的任务给出「开始」，但 `startJob(null,{jobId})` 只接受 prepared → runner 允许从副本全新开始（已有 journal 则要求续跑）；
> ⑤ 列表导出荧光产物时建议名为 `.tif` 且无类型过滤 → `.ome.tif` + TIFF 过滤；已完成任务不再显示无意义的「已提交 0 B」。
> 另：撤回一处越界修改（zh `activate.footer` 的「和→与」会使 `activate.html` 与 i18n 不一致）。
> e2e 场景 a 新增完成态断言（取消按钮不可见、复制进度 100%、列表行 nextAction=export）；
> C2 矩阵新增「planned 无 journal 从列表开始」场景（26/26）；验收方另跑 9.8 GiB 全量页面流程（§4.5）。

## 0. 结论摘要

| 门禁 | 结果 | 数值 / 证据 |
|---|---|---|
| 路由无登录可达 | **通过** | AUTH_ENABLED=True 未登录 GET `/tools/slides` = 200（同会话 `/app` = 302 /login）；pytest 10/10 |
| 页面级 CSP（模式 C + wasm/worker） | **通过** | 逐 token == 规格；真实 Flask 响应头核对（e2e `csp-real-app` + curl）；无 COOP/COEP |
| wasm MIME = application/wasm | **通过** | 真实 Flask 静态路由实测；`app.py` 防御性 `mimetypes.add_type` |
| worker + wasm 在该 CSP 下加载并转换 | **通过** | 全部 e2e 场景在真实 Flask 页面内完成转换；CSP violation 0（console 无 Refused/*） |
| 主页入口 | **通过** | `entry.html` 顶栏「本地切片工具 / Local slide tools」；既有主页/i18n 测试 55/55 |
| 明场小夹具 → ready → 保存 | **通过** | 页内保存字节 sha256 = 原生 CLI（`f265596a…`，287 803 B） |
| 荧光 KFBF（含可选 channel.json） | **通过** | `910d170e…`（134 114 B）= 原生（`--channel-json` 同参） |
| 转换中刷新 → 列表续跑 | **通过** | 2 GiB 输入：刷新 → `nextAction=resume` → 完成，sha256 = 原生 `cefec34e…`；beforeunload 实际触发 |
| uncertain 磁盘确认 | **通过** | 对话框（数字 + 焦点入/还原）→ 取消零任务目录；确认后开始复制；真 quota（fresh profile）同样 uncertain；硬不足无对话框直接数字拒绝 |
| 取消反馈 ≤250 ms | **通过** | 同步 0.2–0.4 ms / 下一渲染帧 ≤9 ms；任务目录随后删除 |
| 严格无损拒绝可见 | **通过** | 预警（边缘 tile 数）+ 核心拒绝 `pixel_policy_violation` 明文展示 |
| 不支持文件复制前拒绝 | **通过** | `unsupported_input`，0 任务目录 |
| 保存不可用不兜底下载 | **通过** | 按钮禁用 + 说明；`URL.createObjectURL` 0 次、无 `a[download]` |
| 网络捕获（不外传） | **通过** | 全流程仅同源 GET：页面 + 9 个 `/static/…`；0 POST/PUT、0 `/api/`、URL 不含文件名/哈希 |
| 离线全流程 | **通过** | `setOffline(true)` 后完整转换+保存成功，sha = 原生；离线期间网络尝试 0 |
| 中英覆盖 | **通过** | EN 态无 CJK 残留（除按设计显示对端语言的切换按钮）、无未翻译键；切回 zh 正常 |
| 纯键盘主流程 + 对话框焦点 | **通过** | Tab/Enter/方向键完成 选文件→转换→保存；`<dialog>` 焦点入/还原、Esc 取消 |
| 真实样本 KFB-1 | **通过** | 页内保存 sha256 = 原生 CLI = `385a59c6…`（C1/C2 标定不变），220 055 311 B |
| C2 运行器回归 | **通过** | `run_smoke.js` PASS；`run_faults.js` **26/26**（验收修订改动 runner 两处，见上） |
| >4.9 GiB 全量页面流程 | **通过** | 9.8 GiB 合成 KFB、新 profile：uncertain 对话框（报告 10.00 GiB / 需要 19.62 GiB）→ 确认 → 复制+probe 53 s → 转换 68 s → 保存；保存 sha256 = 原生 `2084a333…` |

外部门禁（未完成，如实标注）：真实系统保存选择器写用户磁盘、`persist()` 真实手势、
Firefox/Safari/Edge、真实 4/8 GB 整机设备（C7）。

## 1. 交付物（代码）

| 文件 | 内容 |
|---|---|
| `app.py` | `/tools/slides` 路由 + `_apply_slide_tools_security_headers`（页面级 CSP）；`_require_auth` 白名单加 `/tools/slides`（无登录）；防御性 `mimetypes.add_type("application/wasm", ".wasm")`。未改任何全站头 |
| `templates/tools_slides.html` | 工具页骨架（无 inline script/style；全部可见文案走 `data-i18n*`） |
| `static/tools/tools-slides.js` | 页面模块（唯一运行器 API 面 = runner.js 头注释；engine.js 仅取 PROFILES/默认档位/ERROR_CODES/diskNeedBytes 常量） |
| `static/tools/tools-slides.css` | 深色样式、`:focus-visible`、`prefers-reduced-motion`、状态文字+色调双通道 |
| `templates/entry.html` | 顶栏新增「本地切片工具」入口（1 行；不改脚本数量/既有结构） |
| `static/i18n.js` | 新增 `tools.*`（138 键）+ `entry.nav.slides`，zh/en 成对（pytest 全表 parity 1177/1177） |
| `tests/test_slide_tools_page.py` | pytest 门禁（10 用例，见 §4） |
| `tests/browser/slide_tools_c3/` | e2e：`server.py`（真实 Flask + 内嵌 PG，仿 `tests/e2e/e2e_server.py`）、`lib.js`、`run_e2e.js`（a–m）、`run_real_sample.js` |
| `static/entry.css` | 未改动（nav 链接沿用既有样式） |
| runner/engine/worker | **零改动**（C2 回归 25/25 + smoke 证明） |

## 2. 页面流程（对应任务书）

1. 选择文件（KFB/KFBF `accept` + 嗅探门在前）；KFBF 可选伴随 `channel.json`
   （本地 `slice(0, 1 MiB)` 有界读，超限/读失败仅提示并忽略）。
2. 复制进度：`progress.unit==='stage'` → 字节 + 百分比（`role=progressbar` + `aria-valuenow`）。
3. 探测摘要：格式 / 模态（明场|荧光）/ 尺寸 / 层级数 / 通道（FL 列通道名）/ MPP
   （缺失显示「未知（不从物镜倍率猜测）」）/ 边缘 tile 数（将重编码，注明有损）/
   缺失网格单元（FL，>0 时）/ 源 sha256（本地计算）。
4. 临时空间：文案明确「源文件副本 + 输出文件」，数字取自 `engine.diskNeedBytes(probe.estimate,
   {sourceBytes})`（源副本 / 输出上界 / 索引+日志 / 合计）。
5. 档位：节省/均衡/较快（预算数字入文案）；默认 = `defaultProfileId(navigator.deviceMemory)`，
   文案明说这是「近似、非所有浏览器都有、仅供参考」的建议，可改。
6. 策略：保留编码（默认）/ 像素严格无损；有边缘 tile 时在严格选项下实时预警
   「核心将拒绝（pixel_policy_violation）」，实际被拒后错误面板展示核心原话 + 码。
7. 转换：分阶段文案（转换中/收尾/校验）+ 层级进度 + 已提交字节；取消即时生效；
   ready 面板明示「OPFS 内 ≠ 已保存到磁盘，可能被驱逐」，`保存到磁盘` 走
   `showSaveFilePicker → exportJob`；`persist()` 仅按钮手势触发并如实回报授予/拒绝。

## 3. 关键设计点

- **uncertain 磁盘确认（C2 §4.7 交接）**：`probe()` 抛 `disk_precheck_failed{uncertain:true}`
  时弹原生 `<dialog>`：标题 + 需求/可用数字 + 「临时空间约为源副本+输出」说明 + 显式
  「仍要继续/取消」；确认即以 `confirmUncertainDisk:true` 重试；Esc/取消 = 终止且零任务目录。
  非 uncertain（报告余量真实小于需求）不弹确认框，直接错误面板 + 数字硬停。
  印证：≈5.2 GiB 输入在新 profile 上无论 stub（10 GiB 上限）还是真实 quota 都走 uncertain。
- **保存语义**：`showSaveFilePicker` 在 click 手势内调用；不可用时按钮禁用 + 推荐受支持
  浏览器，绝不构造 Blob/`URL.createObjectURL`/`<a download>` 兜底（e2e 断言三者皆零）。
  保存失败保留 OPFS 产物可重试（runner 语义，页面透传）。
- **任务列表 / 刷新续跑**：`listJobs()` 渲染（源名+大小、状态、保存设置、已提交字节、
  结果大小），动作取 `nextAction`（开始/继续转换/保存/删除；本标签运行中 = wait）。
  续跑/开始均不要求重选文件（prepared 复用副本 / resume 校验副本）。删除走
  `discardJob`（带确认）。复制/转换期间 `beforeunload` 拦截（e2e 实测触发）。
- **可访问性**：真实 `<label for>`；`role=progressbar`（min/max/now）+ `aria-live` 状态；
  对话框 `showModal` 原生焦点圈 + 打开者焦点还原 + Esc=取消；`:focus-visible` 可见焦点；
  状态一律文字（含色彩仅辅助）；键盘全程可操作（e2e 场景 m）。
- **i18n**：静态文案 `data-i18n*`；动态文案统一经 `HP_I18N.t()` 且记录键，`hp-lang-change`
  时全部按键重渲染（进度/状态/任务列表/探测摘要/错误），无旧语言残留。

## 4. 测量与证据（全部实跑）

### 4.1 pytest（`results/pytest.txt`）

    TMPDIR=$PWD/.gate-tmp .venv/bin/python -m pytest \
      tests/test_slide_tools_page.py tests/test_phase1_auth_ui.py tests/test_slide_id_review_r16_homepage.py -q
    → 65 passed（新 10 + 既有主页/i18n 55）

新用例：无登录 200；CSP 逐 token；无 COOP/COEP；无 inline script/style；静态资源存在；
wasm MIME；`tools.*` zh/en 成对 + 全表 parity；模板/JS 引用键全定义；主页源码与渲染
入口各一。

### 4.2 浏览器 e2e（真实 Flask app；`results/e2e.json`，16/16 PASS）

    node tests/browser/slide_tools_c3/run_e2e.js → E2E ALL PASS

| 场景 | 关键断言（结果） |
|---|---|
| a 明场 | 保存字节 sha256 = 原生 `f265596a…`；「未保存到磁盘」警示在；persist 如实回报（本次=未授予） |
| b 荧光 | `910d170e…` = 原生（含 channel.json 同参）；探测摘要含通道名 |
| c 刷新续跑 | 2 GiB：转换中 reload → 列表 resume → 完成 sha = 原生 `cefec34e…`；beforeunload 触发 |
| d1 uncertain（stub 上限） | 对话框数字；焦点入框/还原；取消 0 目录；确认后复制开始；复制中 reload → 启动清扫删 staging |
| d2 uncertain（真 quota） | fresh profile 同样触发（本机空闲 > 10 GiB → 报告封顶） |
| d3 硬不足 | 无对话框；错误面板含「需要 ~4.02 GiB / 可用 1.00 GiB」；0 任务目录 |
| e 取消 | 同步 0.2 ms / 渲染帧 8.9 ms ≤ 250；目录最终删除 |
| f 列表 | prepared 跨刷新「开始」→ sha = 原生；删除（确认）→ 列表空 + 0 目录 |
| g 严格无损 | 预警 + `pixel_policy_violation` 明文（核心原话保留） |
| h 不支持 | `unsupported_input`，0 目录（复制前拒绝） |
| i 保存不可用 | 禁用 + 说明；createObjectURL 0、`a[download]` 0 |
| j 网络 | 全流程 10 请求，全部同源 GET（页面 + 9 个 `/static/…`）；0 POST/PUT、0 `/api/`、URL 无文件名/哈希 |
| k 离线 | `setOffline(true)` 后转换+保存成功，sha = 原生；离线后网络尝试 0 |
| l 中英 | EN 无 CJK（切换按钮按设计除外）、无 `tools.*` 原键；回 zh 正常 |
| m 键盘 | Tab/Enter/方向键全流程；radio 方向键实际改变档位；保存经键盘完成 |

截图（`.gate-tmp/slide-tools-c3/screens/`，PNG fullPage）：`empty / copying /
probe-summary / uncertain-dialog / converting / ready-not-saved / job-list-resume /
en-locale`。

### 4.3 真实样本（`results/real-sample.json`）

    node tests/browser/slide_tools_c3/run_real_sample.js --samples <样本目录>
    → REAL SAMPLE PASS：KFB-1 页内保存 sha256 = 原生 CLI = 385a59c6…（220 055 311 B）

仓内文件只含别名与哈希；样本路径运行时解析（排序第一个 `*.kfb`）。

### 4.5 >4.9 GiB 全量页面流程（验收方，`results/large-page.json`）

    node tests/browser/slide_tools_c3/run_large_page.js \
      --input .gate-tmp/slide-tools-c2/browser/memfix/bf-10g.kfb --native-sha <原生输出 sha256>

新 profile → uncertain 对话框文案含真实数字 → 确认 → 全流程完成，保存字节 = 原生（10 509 701 385 B）。

### 4.4 C2 回归（`results/c2-regression.txt`）

    node tests/browser/slide_tools_c2/run_smoke.js   → SMOKE PASS
    node tests/browser/slide_tools_c2/run_faults.js  → FAULT MATRIX: 25/25 passed

## 5. 部署 / nginx 发现（只读核对 `deploy/*.conf`，未改动）

- 所有生产 vhost 均 **proxy_pass 全量回源**（gunicorn/Flask），**没有任何 conf 直接从磁盘
  serve `/static/`**，也没有自定义 `types{}`/MIME 覆盖 → `application/wasm` 与页面级 CSP
  都由 Flask 决定并原样透传，不会被 nginx 改写。
  - `deploy/histopilot-cn/nginx-https.conf`：单 `location /` → 127.0.0.1:37184；带
    `sub_filter`（只匹配主页页脚 `<p class="footer-copy" …>© 2026 HistoPilot</p>` 注入 ICP
    备案号）——工具页无该串，不受影响；`Accept-Encoding ""` 仅为此关闭上游压缩。
  - `deploy/pt-edge-international.inc.conf`（18444）：`location /` → 18080；另有
    `/admin/plugin-assets/` 长超时特例、`/internal/`+`/api/plugin/` 404；server 级
    `add_header Strict-Transport-Security always`（不与 Flask 头冲突）。
  - `deploy/kuaikuaiyun-00-default.conf`：80 catch-all 444；ni-biolab holding 443 404。
- 无任何 COOP/COEP 注入 → 与页面决策一致（worker 非共享内存型）。
- 结论：**生产链路无需为工具页改 nginx**；MIME 防线在 Flask（已加防御性注册）。

## 6. 隐私说明（计划 §6）

- 页面除加载自身静态代码外零网络请求（e2e j/k 两路证明：全捕获断言 + 离线全流程）；
  无任何分析脚本（模板只引 `i18n.js` + `tools-slides.js`）。
- 文件字节/文件名/哈希/缩略图不出浏览器（请求 URL 断言不含文件名与 sha；转换走 OPFS 副本）。
- 不创建平台任务：0 次 `/api/` 调用。
- 服务端可见面：仅匿名页面访问计数（`after_request` → `_collect_site_visit`，Batch D2），
  只记录 GET HTML 200/3xx 的 `/tools/slides` 页面事件（路径、hostname、IP 前缀哈希等派生值，
  无文件数据）；`/static/…` 资源非 HTML 不产生事件。这与主页/登录页同口径。

## 7. 偏差与已知事项

1. `showSaveFilePicker` 在 e2e 中以「写 OPFS 文件句柄」的替身验证（真实选择器是外部门禁）；
   导出字节路径（`exportJob` → `FileSystemWritableFileStream` 分块写）与真实一致。
2. `persist()` 在无头环境返回拒绝属正常；页面两种结果都如实展示（本次实测=未授予文案）。
3. 页内 sha256 校验（e2e 对保存副本）为纯 JS 流式实现（4 MiB 分块，不整文件物化），
   已用 node crypto 在 0/1/55/56/63/64/65/120/1000/4 MiB+100 边界对照通过，并在场景 a
   与原生 CLI 互验。
4. 任务列表读取 `committedBytes` 需解析 journal（runner `_summary` 行为）。原「完成/失败瞬间列表
   滞后」问题已在验收修订中从根上修复（`done` 在记录写入后才 resolve）。
7. 未启动的 `prepared` 荧光任务在刷新后从列表开始时，不会带回刷新前选择的 channel.json
   （prepared 记录不保存伴随文件）；需要伴随文件时请重新选择源文件。
5. d1 场景的「确认继续」在中断复制时依靠启动清扫删除 staging 目录（runner 语义），
   垂死 worker 句柄偶发锁目录时经 pending-cleanup 在下次进入重试（驱动最多重进一次验证）。
6. 语言切换按钮文案按全站约定显示对端语言（zh 界面显示 EN / en 界面显示「中」），
   e2e 的「无 CJK 残留」断言据此排除该按钮。

## 8. 外部门禁（本机无法完成，未宣称通过）

- 真实 `showSaveFilePicker` 写用户磁盘（含中断/覆盖/权限拒绝路径）。
- `persist()` 真实用户手势与浏览器策略差异。
- Firefox（snap 故障）/ Safari / Edge（OPFS 同步句柄、BYOB、导出能力分别实测）。
- 真实/整机 4 GB 与 8 GB 设备（C2 内存指标真机复测 = C7 门禁）。
- >4.9 GiB 真实厂商样本（本机只有合成大文件；合成 9.8 GiB 的全量页面流程已由验收方跑通，§4.5）。
