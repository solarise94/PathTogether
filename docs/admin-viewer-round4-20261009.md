# 改版第四轮：交互修正、后台精简与用户反馈

日期：2026-10-09。在 `admin-viewer` 上继续，未部署。依据站长 dogfood 反馈 6 条。

## 1. Viewer

1. **切片抽出预览浮在主视觉之前。** 悬停抽出的卡片目前被画布遮住（侧栏层级低于画布/侧栏裁切）。抽出层必须显示在画布之上、不被侧栏宽度裁切，同时不改变侧栏布局与命中区。
2. **顶栏图标按钮。** 搜索、分享、矩形、箭头、描图改为大号矢量图标按钮（图标约 20px、按钮约 34px 方形，同一线宽风格），不显示文字；`aria-label` 与悬停 `title` 保留中英文名称。选中/激活态与现有工具一致。折叠逻辑与优先级不变（搜索/分享最后折叠）。
3. **「⋯」菜单不被 AI 面板压住。** 顶栏所有下拉/浮层的层级高于 AI 面板。
4. **AI 导航助手可移动、可调整大小。** 拖动面板标题栏移动，拖右下角调整大小；位置和大小限制在切片视框内（窗口缩放后自动收回框内）；最小 280×240；按用户在 localStorage 记住；双击标题栏恢复默认位置。标题栏上的按钮仍可正常点击。≤768px 维持现有布局，不可拖动。只改 PathTogether 侧（`#ai-panel` 外框），不改 HistoPilot 插件代码。

## 2. 后台

1. **概览「用户总数」只统计正式用户**（`account_kind='real'`），副行的启用/禁用也只计正式用户；「AI access 用户」同口径，避免出现 AI 用户数大于用户总数。
2. **用户表恢复余额，并增加研究数据授权列。** 列：用户、加入时间、最近登录、余额（剩余额度，沿用原表的显示方式）、研究数据（已授权 / 未授权 / 已撤回）、分类。
3. **删除「测试申请」。** 后台页面、桥方法、服务端路由全部下线；`test_applications` 表保留为历史（研究授权视图仍读取其历史标记）。

### 接口变更

- `GET /api/admin/v1/overview`：`users.total / active / disabled / ai_access` 改为只计 `account_kind='real'`；新增 `users.dogfood`（Dogfood 账号数，界面可不展示）。
- `GET /api/admin/v1/users` 每项新增 `research: {"state": "granted"|"withdrawn"|null, "granted": bool}`（批量查询，不逐行 N+1）。余额沿用已有的 `billing`/`spend` 字段。
- 下线（410 `endpoint_retired`）：`/api/admin/v1/test-applications*`、`/api/account/test-application`（GET/POST）；`/admin/test-applications` 宿主页重定向 `/admin`。桥方法 `admin.testApplications.*` 删除（`unknown_method`）。

## 3. 用户反馈

登录用户在「账户」弹层和侧栏底部链接区看到「反馈问题」。点击打开对话框：问题描述（必填，10–4000 字）、「查看将附带的信息」可展开预览、发送按钮。发送后提示已收到。Demo 与未登录不显示。

### 客户端记录（始终在内存中运行，不落盘、不上传，只在用户主动发送时附带）

环形缓冲：最近 300 条或 15 分钟内的事件，每条 `{t: epoch_ms, kind, ...}`：

| kind | 内容 |
| --- | --- |
| `nav` | 页面路径（不含查询串）、打开/关闭切片的 slide_id |
| `action` | 点击的控件标识（id / data-action / data-i18n 键 / role），不含输入内容 |
| `api` | 方法、路径（去掉查询串，`/s/<token>` 记为 `/s/***`）、状态码、耗时 ms、响应 JSON 里的 `code` 字段（若有） |
| `error` | `window.onerror` / `unhandledrejection` 的消息与脚本位置（截断 500 字） |
| `console` | `console.error` / `console.warn` 的前 500 字 |

绝不记录：输入框内容、密码、请求/响应正文、Cookie、查询串、图像数据。

发送时附带：`{captured_at, url_path, lang, viewport:{w,h}, user_agent, current_slide_id, events:[...]}`。

### 服务端

- `POST /api/feedback`，登录用户 + CSRF。body `{"description": str, "client": object}`。
  - 202 `{"feedback_id", "mailed": bool}`；401 未登录；400 描述长度不符或 client 不是对象；413 序列化后超过 256 KB；429 频率超限（每用户每小时 5 次、每天 20 次），带 `retry_after`。
- 迁移 `0081_user_feedback.sql`：`user_feedback(feedback_id, user_id, created_at, description, client JSONB, server JSONB, mail_job_id)`，并为 `registration_mail_jobs` 的 purpose 约束加入 `user_feedback`。
- 服务端附带（`server` 字段）：应用版本（镜像 revision 环境变量，没有则空）、用户 id/邮箱/角色/AI 权限/剩余额度、该用户最近 24 小时的审计事件（≤100 条）、最近的上传/转换任务及失败原因（≤20 条）。
- 邮件：复用现有注册邮件队列、worker 与发送器；收件人为现有管理员通知邮箱配置（`REGISTRATION_ADMIN_EMAIL` / `TEST_APPLICATION_ADMIN_EMAIL`）。正文：用户、时间、描述、当前切片、最近的错误与失败请求摘要，随后附完整 JSON（客户端 + 服务端）；正文超过 300 KB 时从最旧事件开始截断并注明。未配置管理员邮箱时仍保存记录，`mailed=false`。
- 反馈记录保存在数据库，便于邮件丢失时查询；本轮不做后台反馈页面。

## 4. 分工

| 线 | 范围 |
| --- | --- |
| 后端 glm-coder（`av-backend-wt`） | §2 接口变更、§3 服务端与迁移、相关 pytest |
| 后台 UI（`av-admin-ui-wt`） | §2 界面：概览卡口径、用户表两列、删除测试申请页与桥方法；插件仍为 0.4.16（未发布），更新 fileHashes 与 pin |
| Viewer（`av-viewer-wt`） | §1 全部、§3 客户端（记录器、入口、对话框、i18n） |
