# Suite release — 2026-09-23

Feature release for the slide-search autofill incident
（docs/slide-search-autofill-bug-2026-09-22.md）：管理员侧栏顶部的常驻
搜索框被浏览器/密码管理器自动填入登录邮箱，按邮箱过滤后全部切片隐藏、
未归类计数仍显示过滤前的数字且无任何提示。

## Deployed versions

| Component | Source revision | Production image / bundle |
| --- | --- | --- |
| PathTogether | `8020c04` | `localhost/pathtogether-demo:suite-20260923`（`d3bfcade93a7`） |
| HistoPilot service | unchanged（`26fd2a9`） | `localhost/histopilot-demo:suite-20260923`（re-tag of `a1c67e34`） |
| HistoPilot browser plugin | unchanged | `releases/histopilot-0.3.4` |
| Admin plugin | unchanged | `releases/pathtogether-admin-0.4.12` |

No database migration（schema stays at 0065；docker_entry 启动期
ensure_schema 幂等空跑）。Remote source：`~/pathtogether-demo`
fast-forwarded `143afbf..8020c04`。Cutover 用既有 staged-container
deploy 脚本（prepare 配置逐项比对通过后切换，prepare/cutover 均零差异）。

## Changes

- 正式工作台搜索框按需创建：默认 DOM 只有「搜索切片」按钮 + 空容器，
  点击才创建 `type=search` 输入框（可见用途标签 + placeholder + aria）；
  关闭（× / Escape）清空过滤、移除输入框、焦点回按钮；Escape 不再连带
  关手机抽屉；「选择切片」只展开侧栏不创建输入框；侧栏收展与列表重渲
  重放查询不丢失。
- 自动填充防护：`name=slide-filter` + `autocomplete=off` + LastPass /
  1Password / Bitwarden / Dashlane 忽略标记；`:-webkit-autofill` 能力
  检测覆盖 input/change 与查询读取双路径，明确标记的自动填充值清空并
  恢复列表；不按「含 @/像邮箱」拒绝合法查询；无定时器清空。
- `applySlideFilter` 重写：文件名 + 别名同时匹配（修 `data-name||` 短路
  导致别名搜不到的旧 bug）；项目名命中整组可见并展开；未归类计数过滤
  时显示 `匹配/总数`；无匹配显式「没有匹配的切片」；项目区全滤掉显示
  「没有匹配的项目」不留空白；未归类/项目分区在查询期间自动展开。
- 分享选项（有效期/矩形尺寸/矩形策略）竖排：修窄侧栏并排时
  「矩形：仅预设 6/6.5mm」等长文案截断。
- Demo 分支常驻搜索框维持原状（demo.js 独立实现，未受影响）。
- 静态资源版本参数 → `?v=20260923`（index/demo/entry/share）。

## Validation

- tests/js：36 文件 539 项通过（slide-row-layout / sidebar-layout /
  project-import-upgrade 等既有套件无回归）。
- Playwright e2e `toolbar-account-upgrade.spec.ts`：22 项通过（含按需
  搜索 9 项最终验收场景：初始无输入框、开关生命周期、别名/文件名/大小写
  /中文/粘贴、autofill 模拟防护、项目场景、重载重放、真实空态、441×975
  手机抽屉 Escape 隔离、三视口遮挡检查 + Console 零错误、基础回归与
  Demo 原状）。纯路由 fixture 用独立临时配置执行，未拉起 e2e 后端。
- 真实环境 dogfood（本机 podman + 内嵌 PostgreSQL + 真实上传 5 张
  OME-TIFF/项目/别名，生产模板与静态资源）：全部场景通过，管理页
  `/admin`（桥接 13 项管理能力）、`/register`（邀请/公开两模式文案、
  注册框中英切换）正常打开，Console 零错误。
- Cutover 后：两容器 suite-20260923 运行中，`/healthz` 绿（sidecar
  reachable）；`schema_migrations` = 65；公网 `histopilot.com`
  首页/`/login`/`/register`/`/demo` 均 200；`app.js?v=20260923`/
  `style.css?v=20260923`/`i18n.js?v=20260923` 已生效（含新选择器与
  文案键）；`/register` 公开注册表单（terms_accepted）渲染正常。

## Known follow-ups

- 真实浏览器 + 密码管理器的 autofill 行为尚未实机复验（e2e 为
  `:-webkit-autofill` 模拟）；主要保障是首屏无搜索输入框与明确的
  过滤反馈，发布后建议管理端实机观察一次。
- 旧中间容器 `*-pre-suite-20260923` 保留在 homepc 供回滚（deploy.py
  rollback 可用）；确认稳定后可清理。
