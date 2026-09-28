# 主页使用引导升级交付报告（H0–H5）

日期：2026-09-28。执行方案：[homepage-onboarding-upgrade-agent-plan-20260928.md](homepage-onboarding-upgrade-agent-plan-20260928.md)（随本次执行入仓）。
范围：PathTogether 主页（entry）与工作台首用衔接；不改注册审核策略、角色/额度、
上传链路；不启用 COS；无生产部署（未获授权，全部为本地可审阅结果）。

## 1. 结果概览

| 计划项 | 结果 |
|---|---|
| H0 基线 | 四注册模式（public/email_verify/invite_only/closed）、登录态、默认 next=/app、上传能力 off（capability fail-closed）核对完毕 |
| H1 素材与布局 | 真实工作台截图（本地隔离环境 + 合成切片 + 真实登录/标注流程拍摄）；Hero 双入口 + 注册文字入口 + 四步「如何开始」+ 顶栏简化 |
| H2 状态与认证 | 四模式状态矩阵（服务端权威 `_registration_dialog_mode` → 模板渲染 + 回归测试）；头像 disclosure（WAI APG）；中英双语；登录弹窗口径「登录工作台」 |
| H3 工作台衔接 | 空态「上传你的第一张切片」复用既有导入抽屉 +「查看示例」；不新增上传实现 |
| H4 验收 | pytest/vitest/Playwright 全量门禁（见 §6）；修复 4 个自查缺陷（见 §5） |
| H5 交付 | 前后对比截图、素材来源记录（RECORD.md）、状态矩阵与测试证据入仓 |

## 2. 修改文件

| 文件 | 变更 |
|---|---|
| `templates/entry.html` | Hero 双入口（登录工作台主按钮 `/login?next=/app` + Demo 次按钮）、按模式注册文字入口、`.workbench-preview`（真实截图 `<picture>` WebP/PNG + 固有尺寸 1440×900 + 「工作台预览 · 示例切片」标注 + 3 个 HTML 图外标注）、`#get-started` 四步、顶栏简化为语言切换 + `#account-btn` 头像（aria 合同）+ `#account-panel`（登录态：身份摘要/进入工作台/POST 退出+CSRF；未登录：登录工作台/按模式注册/Demo） |
| `static/entry.css` | account/hero-register/workbench-preview/steps 新样式；头像 44×44 目标尺寸；删除旧 Hero 纯 CSS 组织示意等 57 条死规则 |
| `static/entry.js` | 新增 `initAccountPanel()`（disclosure：Enter/Space 开、Escape 关回触发器、点外关闭、打开认证弹窗前收面板并把焦点交还账户按钮——捕获阶段先于 entry-auth 的拦截器）；删除旧 `#hero-tissue` 初始化（下方 `#tissue` 独立演示保留） |
| `static/i18n.js` | 新增 account/register/start/workbench-preview/step 词条（中英成对）；`entry.hero.lead`、`entry.demo.hint`、`entry.signed.hint`、`login.title`/`login.subtitle` 更新口径；删除 40 个死键（entry.mock.*/cta.*/workbench/login/nav.login/principle.nav|status|review|pin/tagline/zoom 等，zh/en 各 80 行） |
| `templates/_login_dialog.html` | 登录视图标题「登录工作台」+ 说明「使用已有账号登录，上传并管理你的切片」（与首屏同一口径；仅提交本任务 hunks） |
| `app.py` | `_entry_signed_in_context` 增加 `account_name`（display_name 快照 → 登录号本地部分回落；不把完整邮箱进头像） |
| `templates/_app_shell.html` | 空态按钮组：正式版「上传你的第一张切片」（`#viewer-empty-upload`）+「查看示例」（/demo）+ 保留「选择切片」；Demo 模式不变；修正上传管线陈旧注释（legacy /api/upload → COS /api/ingestions） |
| `static/app.js` | `#viewer-empty-upload` → `openImportDrawer()`（复用既有上传入口与能力判定） |
| `static/style.css` | `.viewer-empty-actions` 竖排按钮组 |
| `static/entry-media/` | `workbench-preview.webp`（1440×900，82KB）、`workbench-preview-720.webp`（720×450，23KB）、`workbench-preview.png`（量化回退，211KB） |
| 测试 | `tests/test_phase1_auth_ui.py`（旧断言更新至新结构 + 新增 `test_entry_register_entry_state_matrix` 四模式矩阵）、`tests/test_demo_access.py`（空态渲染 + 复用导入抽屉断言）、`tests/e2e/entry-media.spec.ts`（Hero 断言更新：一张预算内预览图）、`tests/e2e/homepage-onboarding.spec.ts`（新增 5 用例，见 §4） |
| 证据 | `docs/review-evidence/homepage-upgrade/`（boot_preview_app.py、shot_workbench.mjs、RECORD.md、artifacts 四图） |

## 3. 状态矩阵结果（计划 §4.1）

服务端权威 = `_registration_dialog_mode()`；矩阵由
`tests/test_phase1_auth_ui.py::test_entry_register_entry_state_matrix` 逐模式锁定：

| 模式 | Hero 主入口 | 注册入口/说明 | 账户面板 |
|---|---|---|---|
| public | 登录工作台 → `/login?next=/app` | 没有账号？注册账号（Hero+面板）+「验证邮箱并设置密码即可使用」 | 登录工作台 + 注册 + Demo |
| email_verify | 同上 | 没有账号？注册账号 +「验证邮箱并提交申请，审核通过后即可使用」 | 同上 |
| invite_only | 同上 | 有邀请码？注册账号 +「注册需管理员发放的邀请码」；不宣传开放注册 | 登录 + 邀请码注册 + Demo |
| closed | 同上 | 「已有账号可登录；暂未开放注册」（Hero+面板同文案；无注册链接） | 登录 + closed 说明 + Demo |
| 已登录 | 进入工作台 → `/app`（Hero+面板） | 无注册/登录引导；「上传切片，继续查看、标注与协作。」 | 身份摘要 + 进入工作台 + POST /logout（CSRF） |

异步审核不被隐藏为「注册即用」：email_verify 模式的四步第一步与 Hero 说明均写明
「管理员审核通过后即可使用」。上传能力状态由导入抽屉按服务端权威展示（capability
off → 抽屉内真实原因），空态不做第二套判断（计划 §7.5）。

## 4. 浏览器验收（Playwright，真实 Flask + 内嵌 PG，无 mock）

`tests/e2e/homepage-onboarding.spec.ts`（5 用例）：

1. **首访**：Hero 双入口、closed 注册说明、四步流程、预览图（WebP 命中 + 1440×900
   固有尺寸 + 标注）；头像 disclosure 键盘全程（Enter 展开 → Tab 入面板 → Escape
   关回触发器 → 点外关闭 → Space 开关 + aria 同步），全程无 JS 错误。
2. **认证衔接**：Hero 按钮与账户面板链接打开同一 dialog（登录视图「登录工作台」）；
   Esc 关闭后焦点分别回到 Hero 按钮 / 账户按钮（面板内链接不落焦点到隐藏元素）；
   登录/注册视图互切。
3. **已登录**：进入工作台主按钮、无注册/登录引导、面板身份摘要 + POST 退出表单
   （CSRF hidden 有值）、不渲染登录弹窗。
4. **H3 空态**：登录后 `/app` 空态「上传你的第一张切片」点击打开既有导入抽屉
   （`#import-drawer`），「查看示例」指向 /demo。
5. **响应式**：1280×720、820×1180、390×844、320×800 无横溢出；窄屏入口在预览图
   之前；720×450@2x（≈200% 缩放）可用。

`tests/e2e/entry-media.spec.ts`（更新）：Hero 为唯一栅格（`workbench-preview.webp`），
旧 `#hero-tissue` 断言改为「不存在」；下方 `#tissue` 八场景演示与几何不变性断言保留。

注册→上传闭环的分步证据：矩阵测试（注册入口四模式）+ 既有
`tests/test_email_verify_activation.py` / `tests/test_public_registration.py`
（受控测试邮箱的真实注册/验证/审核流程，随全量回归通过）+ 上述登录/上传 e2e。
未使用真实外部邮箱（未获授权）；未录制录屏，以可复跑命令 + 截图为证。

## 5. 自查发现并修复的缺陷

1. **entry.css 括号缺失（H1 引入）**：`@media (max-width: 720px)` 少一个 `}`，
   导致其后所有响应式规则被嵌套吞噬——`≤1079px` 单列布局失效，820 宽度下 Hero
   仍双列（左列被压到 184px）。由新增响应式 e2e 用例捕获，修复 + 顺带删除 57 条
   死 CSS 规则（旧 Hero 纯 CSS 组织示意/agent 面板/cta-section/hero-slide）。
2. **`entry.hero.lead` i18n 未随模板更新**：服务端渲染新文案、客户端 i18n 又把旧
   文案写回（截图复核发现）。已同步 zh/en，并加了模板↔i18n 全量比对（当前 0 不一致）。
3. **头像面板焦点陷阱**：面板内登录链接打开弹窗时，entry-auth（链接 target 阶段）
   先于面板收起（冒泡阶段）执行，弹窗关闭后焦点落到已隐藏链接。改为捕获阶段先收
   面板并聚焦账户按钮；e2e 用例 2 锁定。
4. **截图脚本标注可见性**：`state.showAnno=false` 时已保存标注不画在画布层，需点
   「显示全部标记」；脚本现于截图前在页内统计 `#anno-canvas` 非透明像素（2451），
   为 0 即失败（防「面板开了但标注没画」的静默坏图）。详见 RECORD.md「复现注意」。
5. **pg_reap 双失败（pytest 导入链 COLUMNS 污染）**：pytest 导入链会 C-level
   `setenv(COLUMNS=80/LINES=24)`（不进 `os.environ`，`/proc/self/environ` 也不
   可见），子进程继承后 procps `ps -o command=` 截到 80 列——postgres 二进制
   路径长于 80，`pid_is_our_postgres` 认不出真实 postmaster，`stop_postmaster`
   静默不杀 → `test_e2e_pg_reap` 两用例失败、内嵌 PG 泄漏。修复：
   `tests/e2e/pg_reap.py` 与 `tests/e2e/stop-embedded-postgres.ts` 的 ps 调用
   加 `-ww`（不受 COLUMNS 限制）。修后 3/3 通过且 4.5s 完成（原 48s 等待重试）。
6. **e2e fixture 陈旧（P2 合同遗留，非本任务回归）**：`toolbar-account-upgrade.spec.ts`
   的 mock 只发 `alias` 旧字段，而 P2 合同后侧栏显示名/搜索过滤读 `display_name`
   （真实 /api/slides 双字段下发）→ 切片搜索 9 用例 0/4 失败。经 HEAD 静态文件
   复跑确认为主页改动之前已存在（U0–U6 门禁未含 PT e2e 的覆盖缺口）。修复：
   fixture 补 `display_name`（与真实 DTO 一致），22/22 通过。

## 6. 门禁与测试命令

| 门禁 | 命令 | 结果 |
|---|---|---|
| Python 全量 | `.venv/bin/python -m pytest -q`（TMPDIR 指大盘） | **2727 passed / 1 failed（允许的既有失败）/ 11 skipped**，24:23 |
| JS 单元 | `npm run test:js` | **548 passed**（36 files） |
| Playwright | `npx playwright test`（`PATH=.venv/bin:$PATH`，webServer=e2e_server.py） | **56 passed**；8 既有失败 + 20 连带未跑（见下，均非本任务回归） |

**e2e 既有失败（非本任务回归，均已核实归因）**：全量 84 用例中 8 failed /
56 passed / 20 did not run（did not run 为失败用例 60s 超时拖垮同 worker 后
续调度的连带效应）。8 个失败全部在本任务改动之前即存在：

- `raster-image-compat` BMP 上传（×1）：显式失败原因「上传暂不可用（云直传
  能力未开放）」——U0–U6 统一 COS 上传后 capability 按设计 fail-closed，
  旧 e2e 仍按「上传恒可用」断言（COS 任务门禁未含 PT e2e 的覆盖缺口）。
- `import-project-upgrade` U02/U03/U05/U08（×6）：同批真实服务用例，部分
  依赖已删除的旧上传/转换路径（spec 头注明「未接线 → 清晰失败」的 fail-closed
  设计），需 COS 后续按新合同重写。
- `admin-workbench` 10a（×1）：设置页注册模式保存用例，与本任务无关（工作区
  另有他人未提交的注册/管理台改动）。

本任务范围的 e2e 全绿：homepage-onboarding 5/5、entry-media 3/3、
toolbar-account-upgrade 22/22（fixture 修复后）。上述 8 例修复属 COS/
注册任务后续，不在本次主页交付内擅自改动（涉及上传能力语义与用户未提交改动）。

当日全量结果（本机，slide-id-refactor 工作树）：见提交信息与
`docs/review-evidence/homepage-upgrade/` 证据目录。允许的既有失败仅
`tests/test_ai_budget_wiring.py::test_ui_budget_card_and_max_steps_sync_present`。

环境备注：全量 pytest 需将临时目录放到大盘（如 `TMPDIR=<root-disk-dir>`）——
本机 `/tmp` 为 9.5G tmpfs，嵌入式 PG 集群 + pytest 临时数据会写满（本次排查到的
DiskFull 假失败即此因，非代码缺陷）。另两条环境敏感修复见 §5 第 5/6 条。

## 7. 素材尺寸与来源

- 首屏主图预算（计划 §5）：桌面 ≤250KB → **82KB**（webp 1440×900）；手机 ≤120KB →
  **23KB**（webp 720×450）；PNG 量化回退 211KB（仅无 WebP 浏览器加载）。
- 来源：本地隔离环境（`boot_preview_app.py`：临时目录 + 内嵌 PostgreSQL + 合成
  伪 H&E 切片 + 一次性测试账号）真实 UI 拍摄；登录 → 打开切片 → 命名「观察标记」
  箭头标注（真实 POST /api/annotation 保存）→ 打开标注面板 → 截图。无真实邮箱/
  患者/研究者资料；未伪造分析结果。取景、代码版本、复跑命令与踩坑记录：
  [RECORD.md](review-evidence/homepage-upgrade/RECORD.md)。
- 前后对比：`artifacts/entry-before.png`（HEAD acb1e8e worktree 拍摄）vs
  `entry-after.png` / `entry-after-en.png`（中/英）；`workbench.png` 为预览原图。

## 8. 未做 / 边界

- 未部署生产（部署需晚间低峰 + 用户确认钟点）。
- 未做真人可用性测试（计划 §9 允许仅报告启发式检查；未编造转化率）。
- Demo 模式空态不加「上传」入口（Demo 无上传能力，避免不可用按钮）。
- 上传能力开关/权限的实际文案由导入抽屉现有逻辑承载，本任务未改。
