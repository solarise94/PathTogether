# p3fix 修复计划：UI 统一 review + 平台模型露出

- 日期：2026-08-19
- 范围：基于当前未提交的"demo/readonly 统一外壳"工作区（约 +3880 行）之上的两轮 review 结论。
- 基线：pytest 364 passed / 117 skipped（skip 均为 `RUN_PG_TESTS=1` 门控）；vitest 43 passed。
- 核查：2026-08-19 已对两仓逐条核验，文中行号/键名/测试文件均确认到位（app.py 实际约 7705 行，引用行号为锚点而非文件长度）。
- 批次命名沿用 p2fix 惯例。A 批先修（信息暴露/语义错误），B 批 UI 改善，C 批低风险清理。
- 涉及两仓：PathTogether（本文档所在仓）与 HistoPilot（`integrations/pathtogether/ui/` 插件 bundle）。

---

## 批次 A：语义正确性 / 信息暴露（先修）

### A1 平台模型名对普通用户泄露 〔跨两仓〕

**问题**：GET `/api/ai/config` 的 user 分支把平台真实模型名下发给普通用户，前端来源提示
直接渲染出来。模型名属于平台运营信息，只应 owner 可见（owner 侧折叠摘要
`平台 AI · model @ base_url` 是 admin 看自己，符合预期，不动）。

**位置**：
- `app.py:5349` — user GET 分支返回 `"platform_model": platform_cfg.get("model")`
- `HistoPilot/integrations/pathtogether/ui/config-panel.js:79-82` —
  `t("ai.config.using.platform", { s: aiConfig.platform_model || ... })` 拼出模型名
- `static/i18n.js:566`（zh）/ `:1140`（en）— `"ai.config.using.platform": "当前生效：平台 AI（{s}）"`
- `tests/test_ai_credentials.py:255-256` — 断言了 `platform_model == "gpt-p"` 回显

**修法**：
1. `app.py` user GET 分支删除 `platform_model` 字段（owner 分支的 `model` 回显保留）。
2. `config-panel.js` user 分支来源提示改为不拼模型名：新增 i18n key
   `ai.config.using.platform.plain` = zh「当前生效：平台 AI（由管理员配置）」/
   en「Active: platform AI (configured by admin)」。`ai.config.using.own` 保留 `{s}`
   （那是用户自己的模型名，无泄露）。
3. `tests/test_ai_credentials.py` 改为断言 user 响应**不含** `platform_model` 键。
4. 同步部署：HistoPilot 仓改完 `integrations/pathtogether/ui/config-panel.js` 后，
   拷贝到部署机 `PLUGIN_BUNDLES_DIR/histopilot/ui/`（PathTogether 内置 `plugins/`
   无 histopilot 目录，运行时只读外部 bundle 目录；`app.py:1064-1083`）。

**验证**：pytest `test_ai_credentials.py`；手工以 user 角色打开正式版 AI 面板，
来源提示不含模型名；owner 视角折叠摘要仍显示 model @ base_url。

### A2 移动端 Demo 只读徽章被隐藏

**问题**：`@media (max-width: 760px)` 里 `.demo-badge { display: none; }`
（`static/style.css:3039`）。Demo 对外仅有的三个标记（只读徽章 / 额度 / 登录入口）
中徽章在手机上整个消失，页面与正式版无法区分，违背"Demo 必须明确标注只读"。

**修法**：
- 保留徽章，改为紧凑形态：移动端缩小 padding/字号（如 `font-size: 10px; padding: 0 4px`），
  文案可缩短为「Demo」。若 360px 窄屏标题栏仍溢出，把徽章移入侧栏 `sidebar-top`
  与额度 chip 同行显示（`templates/_app_shell.html:104-106`），标题栏不承载。
- `.header-register-link` 移动端隐藏可保留（`static/style.css:3038`；空间权衡，
  注册入口侧栏底部还有「登录后继续使用」）。

**验证**：375px / 360px 视口下打开 `/demo`，只读标识可见且标题栏不换行溢出；
`tests/test_stage2_ui.py` 外壳相关断言不受影响。

### A3 额度文案硬编码「1 次」+ finishRun 状态回跳

**问题**：`per_browser_limit` 管理员可调（默认 1，`budget_store.py:52`；上限
`_BUDGET_LIMIT_MAX` = 1,000,000，`app.py:2497`，经 `PUT /api/admin/settings/ai-budget`
调整），但三处文案写死「1 次」；
`finishRun()` 无条件置 `used`（含"已用完+登录"提示）再靠 `loadConfig` 纠正，
limit>1 时用户每轮 run 结束都先看到错误的"已用完"再跳回"还剩 N 次"。

**位置**：
- `templates/_app_shell.html:105` — quota chip 种子文案 `demo.quota.idle`「可体验 1 次 AI 导航」
- `templates/_app_shell.html:350` — run 按钮 title「体验 AI 导航（1 次）」
- `templates/entry.html:118` — 入口页提示「体验 1 次 AI 导航」
- `static/demo.js:710-715` — `finishRun()` 先 `setAiButton("used")` 再 `loadConfig`
- `static/demo.js:313-314` — `usedN/limN` 用 `|| 1` 兜底，0 会被显示成 1（顺手修）

**修法**：
1. 种子文案改中性：`demo.quota.idle` = zh「AI 导航额度加载中…」/ en「Loading AI quota…」；
   按钮 title 初始为「AI 导航」占位，均由 `applyConfig()` 用
   `demo.ai.run.available.n`（已有键）回填真实剩余次数。
2. `entry.html` 提示改为不含具体次数的表述（如「可查看示例切片并体验 AI 导航」），
   或由 `/` 入口路由注入当前 limit——取前者，避免入口页依赖 PG。
3. `finishRun()` 改为：保持按钮禁用的过渡态（复用 `running` 文案或新 key
   `demo.ai.refreshing`「正在刷新额度…」），只调 `loadConfig({restore:false})`，
   由 `applyConfig()` 落最终状态；网络失败时兜底 `used`。
   （已核实可行：`loadConfig(opts)` 仅读 `opts.restore`，`demo.js:391-412`；
   `applyConfig()` 是其唯一下游，`demo.js:359-389`。）
4. `usedN/limN` 兜底改 `!= null ? Number(x) : 默认值` 写法。

**验证**：`tests/js/demo-ai.test.ts` 若断言了 `used` 即时态需同步；手工把
per-browser limit 调成 3，跑一轮 AI，结束后应直接显示「还剩 2 次」而非闪"已用完"。

---

## 批次 B：UI 改善

### B1 「平台 AI / 自定义 API」两卡式切换 〔跨两仓〕〔已回退：自定义 API 通道于 2026-08 下线，user 恒走平台 AI〕

**问题**：现状切换入口是藏在 Base URL 输入上方的小 checkbox（`_app_shell.html:389-392`，
`ai-use-platform-wrap`，user 角色才显示）。视觉权重低；勾选平台后自定义三字段仅
disabled 仍占位；`max_steps` 语义随模式变化（平台模式=注入平台值，自带模式=自有步数，
`static/app.js:537-575` 已有联动）但 UI 无说明；平台未配置时置绿原因只在 hover title。

**方案**：把 checkbox 升级为配置区顶部的两卡 radio group（复用外壳 `seg-control`
分段控件风格，新 `.ai-source-cards` 样式）：

- **卡 A「平台 AI（推荐）」**：副文案「由管理员统一提供，开箱即用」；下方一行
  「单次最多 N 步」（N 取自 `/api/ai/config` 已下发的注入值）。平台未配置 → 整卡
  置灰，卡内直接写「暂未开放」（替代 hover title）。
- **卡 B「自定义 API」**：副文案「使用自己的 Base URL / API Key / 模型」；选中才
  展开 Base URL / API Key / 模型三字段 + `max_steps`（仅此模式可编辑）。
- 选中卡 A 时**收起**（不是灰掉）自定义字段，只留一行来源提示（A1 改造后的
  无模型名文案）；选中卡 B 时显示「当前生效：我自己的凭据（…）」。

**边界**：
- PUT 协议不变：仍提交 `use_platform: true/false`，后端零改动。
- owner 侧面板不动（owner 分支本就是平台配置编辑器，折叠摘要 + 重新配置够用）。
- 调优区（user 只读）保持现状，仅在卡 A 选中时隐藏整块高级调优（user 不可改）。

**落点**：
- `templates/_app_shell.html` official 分支 `ai-config-wrap` 结构重排
- `HistoPilot/integrations/pathtogether/ui/config-panel.js`：
  `renderAiConfigState()` / `applyOwnCredsDisabled()` 改驱动两卡态；`saveAiConfig()`
  payload 组装不变（`use_platform` 取卡 A 选中态）
- `static/style.css` 两卡样式 + 移动端适配
- `static/i18n.js` 新 key（卡标题/副文案/未开放/步数行）
- 部署同步：`config-panel.js` 属外部 bundle，改完须按 A1 第 4 步同流程拷贝到
  部署机 `PLUGIN_BUNDLES_DIR/histopilot/ui/`

**验证**：user 视角三种态（平台可用 / 平台未配 / 自带凭据已存）截图比对；
PUT 回显正确；`tests/test_ai_credentials.py` / `test_ai_config_validation.py` 不回归。

### B2 /demo 对已登录用户仍显示「登录 PathTogether」

**问题**：`demo_landing()`（`app.py:1626`）不查 `session["auth_user"]`，已登录的
测试用户访问 `/demo` 会看到语义不通的登录/注册 CTA（`_app_shell.html:21-24`）。

**修法**：`demo_landing` 读取登录态传 `logged_in` 给模板；shell 的
`{% if C.login_cta %}` 分支按 `logged_in` 切换为「打开完整版 → `/`」（i18n 新 key
`demo.open.full`）。demo 的 API 面与 capability 签发逻辑完全不动（/demo 不读
identity 的安全设计保持）。

**验证**：登录态访问 `/demo` 显示「打开完整版」；匿名访问不变；
`tests/test_demo_access.py` 匿名路径不回归。

---

## 批次 C：低风险清理

### C1 app-mode.js camelCase 死字典

`static/app-mode.js:14-66` 的 `DEMO_CAPS`/`OFFICIAL_CAPS` 全仓无消费者
（`HP_can`/`HP_CAPABILITIES` 无人读，app.js 只用 `HP_API`）；键名与服务端 snake_case
不一致（`saveImage` vs `save_image`），且 `OFFICIAL_CAPS` 静态写死 `annotate: true`
对分享访客是错的——将来谁信它就埋雷。**删掉两个字典**，`caps` 只来自
`Object.assign({}, boot.capabilities || {})`。

### C2 demo.js 兜底 adapter 重复

`static/demo.js:14-49` 的 `demoApi()` 兜底（兜底对象在 16-48 行，7 个方法）是
app-mode.js demoAdapter（`app-mode.js:94-120`，9 个方法）的过时副本——**已实际
漂移**：缺 `slideInfoUrl`/`thumbnailUrl`，留着只会继续腐化。`demo.html` 恒定先加载
app-mode.js，兜底基本不可达。改为硬依赖：`window.HP_API` 缺失时 `console.error`
并直接 return，删兜底实现。

### C3 zoom 徽章兜底口径不一致

`static/demo.js:138-155` 兜底显示 µm/px，`viewer-core.js` 的 `zoomText` 显示倍率 ×。
兜底仅在 viewer-core 加载失败时可见。统一为只走 `HP_ViewerCore.zoomText`，
兜底分支仅显示百分比。

---

## 建议提交切分

1. `fix(security): 平台模型名不再下发普通用户`（PathTogether + HistoPilot 各一提交，
   A1，含测试与 bundle 同步）
2. `fix(ui): demo 只读徽章移动端可见 + 额度文案去硬编码 + finishRun 过渡态`（A2+A3）
3. `feat(ui): AI 来源两卡式切换（use_platform UI 重构）`（B1，跨两仓）
4. `fix(ui): /demo 已登录用户 CTA 切换`（B2）
5. `chore(ui): 清理 app-mode 死字典与 demo 兜底重复`（C1-C3）

## 回归清单（每批跑）

- `python3 -m pytest tests/ -q`（364 passed / 117 skipped 为基线）
- `npm run test:js`（43 passed 为基线）
- 手工：375px 视口 /demo 徽章；limit=3 的 run 结束态；user 来源提示无模型名；
  owner 改 model 后下一次 run 生效

## 明确不在本轮修（review 已确认现状正确）

- 服务端 capabilities 门控与 `/api/demo/*` 只读边界（fail-closed、IP 限流、
  cookie HMAC 均正确）
- i18n demo 侧键中英齐全；i18n.js 自绑 `.lang-toggle`，demo 语言切换可用
- 旧黑色 Admin 用量栏零残留；owner 诊断在正式版侧栏「AI 预算」（含重置用量）
- `obs-canvas` 改名安全（CSS 仅按 `.anno-canvas` 类选择）
- `/login` 的 `next` 走 `_safe_next_path` 安全校验
