# 用户后台、注册与 Viewer 改版 —— 简化版实施设计

日期：2026-10-08。本文取代 [原设计稿](admin-viewer-registration-design-20261008.md) 中的实现部分，作为本轮实现的唯一依据；原稿第 1 节「已确认的产品结论」和两份原型（[Viewer](design-assets/admin-viewer-20261008/viewer-folders.html)、[后台](design-assets/admin-viewer-20261008/admin-temporary-view.html)）继续有效。[原验收清单](admin-viewer-registration-acceptance-20261008.md) 中与本文冲突的案例以本文第 8 节为准。

基线：分支 `admin-viewer`，从生产线 `registration-antibot` @ 66cfa29c 切出（原稿核查的 `slide-id-refactor` @ 19b6438e 已落后生产 160 个提交，迁移编号也不同）。下一个迁移号 **0080**。

## 0. 相对原稿的简化与理由

| 原稿 | 本文 | 理由 |
| --- | --- | --- |
| 新建 `admin_slide_view_grants` 表 + lease_id + Idempotency-Key + expected_lease_id CAS | 给现有 `slide_view_grants` 加 `expires_at`；开启=写入 1 小时到期，结束=把到期改为 now() | 核查确认 `slide_view_grants` 只由后台自授权端点写入，本身就是「管理员查看」专用表；对同一管理员同一切片，重复开启返回原到期即天然幂等，不需要租约与 CAS |
| 临时查看只授予读图，单独拒绝 AI/标注/下载/分享 | 保留旧自授权已有的能力集合，只增加时限 | 平台 owner 就是站长本人，额外裁剪是新权限模型；时限才是用户要的。AI 运行授权的到期不晚于临时查看到期（见 3.3） |
| 旧永久授权冻结为历史 + 读路径分支忽略 | 迁移把存量行的 `expires_at` 设为迁移时刻 | 一条 UPDATE：旧授权立即失效，后台显示「已结束」，可重新开启 |
| 后台清单改为 ID 驱动 | 不需要 | 生产线的清单已按 `legacy_filename or slide_id` 组织，无旧名资产不会被遗漏；只需改排序和授权列 |
| pending 用户迁移脚本 + dry-run + 状态统计 | 登录时惰性激活：已验证邮箱、未禁用的 `pending_activation` 用户登录成功即转 active 并按公开注册同口径初始化额度 | 不写一次性数据脚本；按状态判断，天然幂等；生产当前已是 `public` |
| 注册模式迁移决策表、两阶段删表、旧邮件任务终态 | 模式只剩 `closed/public`；旧存储值按 closed 处理（fail-closed，生产已是 public 不受影响）；邀请码表保留不删 | 邮件任务里没有邀请码专用 purpose；表留作历史，不再有任何读写入口 |
| 测试申请模块拆分 | 不动 | 它只服务 pending 用户；惰性激活后自然无新申请，历史列表仍可在后台查看 |
| 缩略图后台预生成任务、持久幂等键、补偿扫描、多级缓存 key | 直接用现有 `/api/slides/<id>/thumbnail`（鉴权后 ETag/304），前端只加载当前叠 | 现有端点已按 ID、鉴权后出图；400px 缩略图走最低层级，按需生成足够 |
| 服务端搜索 API、分页、防抖取消 | 前端在已鉴权的 `/api/slides` 全量结果上本地过滤 | `/api/slides` 已一次返回当前账号全部可读切片，且已有前端过滤逻辑 |
| 目录树 owner 级锁、删除非空目录拒绝 | 移动时在事务内锁 owner 的项目行做环检测；删除文件夹时子文件夹回到根（FK `ON DELETE SET NULL`），切片引用随项目删除（与现状一致，切片本身不删） | 数据不丢、无需「先清空」流程 |
| 用户列表 cursor 绑定筛选/排序 | 前端切换筛选/排序时清空 cursor；服务端在内存全量排序后分页 | 用户量小，现有实现就是内存 offset 分页 |
| 刷新恢复目录状态、上传到当前目录且处理途中删除目录 | 不做 | 页码记忆只在内存；上传现状不带目标项目，保持现状 |
| 「仅自己/共享」摘要 | 不做 | 分享面板原样迁入顶栏按钮浮层 |

## 1. 数据库迁移 `migrations/0080_admin_viewer.sql`

```sql
ALTER TABLE users ADD COLUMN IF NOT EXISTS account_kind TEXT NOT NULL DEFAULT 'real';
-- CHECK (account_kind IN ('real','dogfood'))，用 DO 块按约束名幂等添加
ALTER TABLE users ADD COLUMN IF NOT EXISTS last_login_at TIMESTAMPTZ;

ALTER TABLE slide_view_grants ADD COLUMN IF NOT EXISTS expires_at TIMESTAMPTZ;
UPDATE slide_view_grants SET expires_at = now() WHERE expires_at IS NULL;   -- 旧永久授权立即结束
ALTER TABLE slide_view_grants ALTER COLUMN expires_at SET NOT NULL;

ALTER TABLE projects ADD COLUMN IF NOT EXISTS parent_project_id TEXT
    REFERENCES projects(project_id) ON DELETE SET NULL;
CREATE INDEX IF NOT EXISTS projects_parent_idx ON projects(parent_project_id);
```

旧用户 `last_login_at` 保持 NULL，不回填。存量用户默认 `real`；Dogfood 名单由站长在后台逐个标记，本轮不批量标记。

## 2. 用户管理

- 登录成功（`app.py: login()` 写入正常 session 的路径）执行 `UPDATE users SET last_login_at = GREATEST(COALESCE(last_login_at, '-infinity'), now())`。登录失败、刷新、AI 请求、注册完成（不自动登录）都不更新。
- `GET /api/admin/v1/users` 新增参数 `kind=real|dogfood|all`（默认 `real`）与 `sort=joined_desc|joined_asc|last_login_desc`（默认 `joined_desc`）。排序在分页前对全量完成，`user_id` 作稳定次键，`last_login_at IS NULL` 恒排末尾。每项新增 `account_kind`、`last_login_at`（格式与 `created_at` 相同）。
- `POST /api/admin/v1/users/<user_id>/account-kind`，body `{"account_kind":"dogfood"|"real"}`，与同组 enable/ai-access 端点同样的 owner 权限与 CSRF；值未变则不写审计，变化时写 `_audit("admin.user.account_kind", ...)`，detail 记录前后值。不改 session、额度、角色、启用状态。
- 后台用户页主表四列：用户（显示名+邮箱）、加入时间、最近登录、分类。上方：分类筛选（正式用户/Dogfood/全部，默认正式）与排序下拉。启停、AI 权限、重置密码、额度等现有操作保留在行内「更多」或详情中。未登录过显示「暂无记录」。不加活跃度指标、不加统计卡。

## 3. 后台切片：上传清单与管理员临时查看

### 3.1 API

- `POST /api/admin/v1/slides/<slide_id>/temporary-view`：owner 权限 + CSRF。受益人固定为当前登录 actor；不接受时长参数（常量 `TEMPORARY_VIEW_SECONDS = 3600`）。
  - 切片不存在 → 404；不可读（非 ready / 非 id_bundle / 文件缺失，沿用 `_admin_v1_desc_servable`）→ 409 `slide_not_servable`；切片属于 actor 本人 → 409 `own_slide`。
  - 已有 `expires_at > now()` 的授权 → 原样返回，不续期。否则 upsert：`granted_at=now(), expires_at=now()+interval '1 hour', granted_by=actor`，写审计 `admin.slide_temporary_view.start`。
  - 响应：`{"slide_id", "temporary_view": {"status":"active","granted_at","expires_at"}, "server_now"}`。
- `DELETE /api/admin/v1/slides/<slide_id>/temporary-view`：把 actor 对该切片未到期的授权 `expires_at` 设为 now()，调用现有 `_revoke_run_grants_for_slide_id` 取消派生 AI 运行授权，写审计 `admin.slide_temporary_view.end`。已结束/不存在时也返回 200（幂等），响应 status `ended` 或 `none`。
- 旧 `POST /api/admin/v1/slides/<name>/visibility` 改为 `_admin_v1_retired(...)`（410），不再建立任何授权。`share_store.grant_slide_view` 必须带 `expires_at`（列 NOT NULL），无其它写入方。
- `GET /api/admin/v1/slides/inventory`：已登记切片按 `slides.created_at` 新→旧排序（`slide_id` 次键）；每项新增 `created_at`（首次登记时间，直接取 `slides.created_at`）和 `temporary_view: {"status": "own"|"none"|"active"|"ended"|"unavailable", "expires_at": ...|null}`（`unavailable` = 不可读，同时给出现有的原因字段）；顶层新增 `server_now`。删除 `granted_to_owner/granted_at/grant_recorded`。孤儿文件/未登记文件维持现有独立区段，不并入正常行。

### 3.2 读权限

`slide_store._has_slide_view_grant` 和 `visible_ready_slide_ids` 中查询 `slide_view_grants` 的 SQL 各加 `AND expires_at > now()`。所有会话通道的读取（列表、info、tile、DZI、crop、region、thumbnail，新旧 URL）都已经过这两个函数，因此到期立即对新请求生效，不依赖清理任务或前端倒计时。ETag/304 已在鉴权之后，无需改动。

`/api/slides` 对「仅凭有效临时授权可见、非本人所有」的切片每项附加 `temporary_view_expires_at`，供 Viewer 归入「临时查看」虚拟文件夹并在到期时清屏。

### 3.3 AI 运行授权

签发 AI/插件运行授权（`_issue_run_grant`）时，若主体对该切片的读取仅来自临时查看（非 owner），运行授权到期时间取 `min(现有 TTL, 临时查看 expires_at)`。主动结束已有撤销钩子。

### 3.4 后台切片页

列：切片/上传者、加入时间、管理员临时查看、操作。

| status | 显示 | 操作 |
| --- | --- | --- |
| none / ended | 未开启 / 已结束 | 开启 1 小时 |
| active | 可查看 · 剩余 N 分钟（按 `expires_at - server_now` 计算并向上取整，每分钟刷新） | 查看、结束查看 |
| own | 本人切片 | 查看 |
| unavailable | 不可查看（原因） | 无 |

「查看」在新标签页打开 `/?slide=<slide_id>`（Viewer 已支持该深链）。页脚说明：临时查看 1 小时后自动结束，可提前结束，不改变用户自己的分享设置。

## 4. 注册：邀请码退役

- `settings_store.REGISTRATION_MODES = ("closed", "public")`。读取到旧值 `invite_only` / `email_verify_invite_activation` 时按 `closed` 处理（现有读取已 fail-closed，保持即可）；后台设置 PUT 只接受这两个值。
- `/register`：删除 invite_only 与 email_verify_invite_activation 分支，只保留 closed 与 public。登录框/注册模板中的邀请码输入、等候邀请文案全部删除。
- 登录：`pending_activation` 用户若 `email_verified_at` 非空且未禁用，在登录成功时惰性激活（`activation_state='active'`、`activation_source='public'` 或沿用现有可接受值、`activation_updated_at=now()`），并调用公开注册同口径的额度/AI 初始化（必须幂等：已初始化不重复）；然后走正常登录。不再签发受限 enrollment session。未验证邮箱的账号无法登录（现状），重新走公开注册流程。禁用账号保持禁用。
- 下线（返回 `_admin_v1_retired` 410 或同等的稳定退役响应，不回显 token）：`/api/admin/v1/invites*`、`/api/account/activate`、`/api/account/enrollment`。`GET /activate` 重定向到 `/login`。
- `registration_store` 中仅被上述入口使用的函数（`create_invite/list_invites/revoke_invite/redeem_invite/activate_registered_user/verify_email_create_user` 等）删除；与 public 流程共用的邮件、令牌、限流、协议证明代码不动。删除前用 grep 确认无其它调用方。
- `registration_invites` 表保留（历史），不新建删表迁移。测试申请模块不动。
- 后台：删除「邀请」页和 `admin.invites.*` 桥方法；注册设置只显示 closed/public。

## 5. Viewer

### 5.1 布局（参照 viewer-folders 原型）

- 顶栏保持单层：在现有工具栏上新增「搜索」与「分享」两个按钮（图标+文字，窄屏只留图标），现有缩放、旋转、ROI、标注、保存、AI、语言、账户等全部保留，原有「⋯」折叠机制继续工作。
- 左栏改为文件夹/切片浏览器（宽约 160px，桌面可折叠，移动端仍是抽屉）：
  - 头部：当前位置（根目录显示「我的切片」；子目录显示「‹ 上级名」返回 + 当前文件夹名）、「＋」菜单（导入切片、新建文件夹）、当前文件夹「⋯」菜单（重命名、移动到…、分享此文件夹、删除文件夹）。
  - 内容：先子文件夹卡片（名称 + 切片数），后切片卡片堆叠；底部固定「‹ 1/3 ›」翻叠按钮。
  - 根目录 = 顶层文件夹（`parent_project_id` 为空）+ 「临时查看」虚拟文件夹（仅当存在 `temporary_view_expires_at` 切片）+ 未归类切片（不在任何项目中、非临时查看）。
  - 底部账户区（/admin、修改密码/邮箱、数据共享、退出）保留。
- 分享管理：原侧栏「分享管理」整块移入顶栏「分享」按钮的浮层，功能不变（有效期、ROI、策略、权限、创建、复制、链接列表、撤销）。浮层默认分享对象为当前打开的切片；「选择切片…」复用现有切片选择器（`openSlidePicker`）多选；文件夹「⋯ → 分享此文件夹」复用 `shareProject`。
- 当前切片名放画面左上角紧凑标签，不新增第二行。

### 5.2 切片堆叠

- 每叠最多 8 张，按左栏实际可用高度减少；文件夹卡片占用同一高度预算。翻叠只换列表，不动当前 Viewer。
- 卡片纵向重叠堆放，每张露出名称条和一小段缩略图；鼠标进入某卡片时该卡片向右滑出（约 180ms），完整显示缩略图与名称，浮在 Viewer 上方，不改变布局；离开收回。命中区以卡片原位置为准，滑出层也可点击，避免抖动。
- 悬停只预览：不打开切片、不改变倍率、AI 和标注。点击或 Enter 才调用现有 `openSlide(slide_id)`；为 `openSlide` 加请求序号，晚到的旧响应丢弃（A→B 快速点击只保留 B）。打开成功后卡片显示选中标记。
- 缩略图 `<img src="/api/slides/<id>/thumbnail">` 只为当前叠创建；加载失败显示占位，不影响打开切片。
- 触屏点击直接打开；键盘上下键在卡片间移动、Enter 打开、Esc 关闭浮层；`prefers-reduced-motion` 下关闭位移动画，仍显示预览。
- 每张切片原有的单片操作（重命名/备注、加入文件夹、从当前文件夹移出、删除等现有能力）放在卡片「⋯」菜单中，一项不少。

### 5.3 文件夹

- 文件夹 = 现有项目（project_id、成员、分享、归档语义不变），UI 文案叫「文件夹」。
- `/api/projects` 返回 `parent_project_id`；`/api/project/create` 接受可选 `parent_project_id`；`PATCH /api/project/<pid>` 接受 `parent_project_id`（null = 移到根）。服务端校验：父项目存在且同 owner、未归档、不能是自己或自己的子孙、层级不超过 5；在事务中 `SELECT ... FOR UPDATE` 锁住该 owner 的项目行后再做环检测。错误码 400/403/409 带可读原因。
- 一张切片可在多个文件夹（现有多对多）。「从此文件夹移出」只删关联；失去全部关联的切片回到根目录「未归类」。
- 删除文件夹：子文件夹回到根（FK），切片不删。确认框说明这一点。
- 返回上级时恢复该目录上次所在的叠（内存记录，刷新不保留）。当前文件夹被删除后回到根目录。

### 5.4 搜索

- 顶栏「搜索」打开浮层，输入框自动获焦；Esc 关闭并把焦点还给按钮。
- 在 `/api/slides` 已鉴权的全部切片上本地匹配显示名、别名和原文件名（不区分大小写），不受当前文件夹限制。结果行：缩略图、名称、所在位置（「教学切片 / 复核」；多个文件夹时显示第一个并标注「+N」；未归类显示「未归类」）。同名结果靠位置与 slide_id 末 6 位区分。
- 点击结果：左栏切到该切片的首个所在文件夹（或根目录）并翻到其所在叠 → `openSlide(slide_id)` → 关闭浮层。

### 5.5 临时查看到期

Viewer 打开的切片若带 `temporary_view_expires_at`，按其到期时间设定计时器；到期（或任何读取请求返回 403/404）时关闭画面，移除该切片卡片与缩略图，提示「临时查看已结束，可在后台重新开启」。服务端门禁是唯一权限依据，计时器只负责清屏。

## 6. 后台插件发布约束

修改 `plugins/pathtogether-admin/ui/*` 必须同步：`manifest.json` 的 `pluginVersion` 升到 0.4.16、`ui.fileHashes`，以及 `plugins/source-policy.json` 中该 manifest 的 sha256 pin（`tests/test_plugin_source_policy.py` 校验）。桥方法改动在 `static/admin-host.js` 的三张表（权限、参数 schema、后端映射）同步：

| 方法 | 变更 |
| --- | --- |
| `admin.users.list` | 新增参数 `kind`、`sort` |
| `admin.users.setAccountKind` | 新增，`{user_id, account_kind}` → `POST /api/admin/v1/users/<id>/account-kind`，权限 `admin:users:write` |
| `admin.slides.startTemporaryView` / `admin.slides.endTemporaryView` | 新增，`{slide_id}` → POST / DELETE `/api/admin/v1/slides/<id>/temporary-view`，权限 `admin:slides:write` |
| `admin.slides.setVisibility`、`admin.invites.*` | 删除（调用返回 `unknown_method`） |
| `admin.settings.update` 的 `registration_mode` | 枚举改为 `closed/public` |

iframe 内不得直接 fetch；「查看」若需宿主打开新标签，在 admin-host 增加只读宿主方法（如 `admin.viewer.open {slide_id}` → `window.open('/?slide=' + encodeURIComponent(id), '_blank', 'noopener')`），不经 HTTP。

## 7. 分工与顺序

三条线并行，各自独立 worktree/分支，从 `admin-viewer` 文档提交切出，最后由主代理合并：

| 线 | 负责文件 | 不得修改 |
| --- | --- | --- |
| 后端 `av-backend` | `migrations/0080_*`、`app.py`、`*_store*.py`、`slide_store.py`、注册/登录模板（`templates/_login_dialog.html`、`register*`、`activate*`）、`tests/test_*.py`、`tests/conftest.py`、`Containerfile`（如有新模块） | `static/*`、`plugins/*`、`templates/_app_shell.html` |
| 后台 UI `av-admin-ui` | `plugins/pathtogether-admin/**`、`plugins/source-policy.json`、`static/admin-host.js`、对应 `tests/js/admin-*`、`tests/test_admin_plugin.py`、`tests/test_plugin_source_policy.py` 的期望值 | `app.py` 等后端文件 |
| Viewer `av-viewer` | `templates/_app_shell.html`、`static/app.js`、`static/style.css`、`static/i18n.js`、viewer 相关 `tests/js/*`、`tests/e2e/*` | 后端与后台插件文件 |

接口以本文为合同；某条线发现合同不可行时停下报告，不自行改合同。

## 8. 验收（取代原验收清单中的冲突部分）

P0（阻止发布）：

1. 临时查看：开启后 1 小时内 info/tile/thumbnail/crop 可读；把测试授权 `expires_at` 改到过去后同一批请求全部 403（含带 If-None-Match 的请求不返回 304）；重复开启不续期；结束后立即拒绝；同名不同 ID 的切片互不影响；不可读资产 409；迁移后旧永久授权不再放行；旧 visibility 端点 410。
2. AI 运行授权到期不晚于临时查看到期；主动结束撤销运行授权。
3. 注册：公开注册全流程不变；closed 拒绝新注册；旧 invite 端点/桥方法退役；已验证未禁用的 pending 用户登录后成为 active 且额度只初始化一次（连续登录两次验证）；禁用 pending 用户仍不能登录。
4. 文件夹：建子文件夹、移动、环检测（A→B 后 B→A 被拒、移到自己子孙被拒）、跨 owner 被拒、删除父文件夹后子文件夹回根且切片仍在；原项目成员和分享不受影响。
5. 后台插件哈希与 pin 校验通过，iframe 内无新增直接 fetch。

P1（本轮功能验收）：

6. 用户页：筛选/排序/分页无重复无遗漏，NULL 最近登录排末尾；登录成功才更新 last_login_at；分类可切换并留审计；普通用户调用分类接口被拒。
7. Viewer：单层顶栏在 1440/1024/768/390px 不换行且全部原有功能可达；悬停预览不切片；点击打开；A→B 快速点击只停在 B；文件夹进入/返回/页码记忆；搜索跨文件夹定位同名切片；分享浮层功能完整；36 张切片翻叠高度稳定；键盘与触屏可用；临时查看到期清屏。
8. 原有测试套件（pytest 全量、vitest、相关 Playwright）通过；因合同变化而改写的旧测试在提交说明中逐项列出。

交付物：变更说明、迁移在生产 schema 副本上的演练结果（含重复执行）、测试结果、桌面与窄屏截图（Viewer 根目录/文件夹内悬停/搜索同名/分享浮层；后台用户页、切片页三态）。不部署。
