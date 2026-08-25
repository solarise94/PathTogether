# 账户系统简化与完善修复方案

- 日期：2026-08-25
- 状态：**方案稿，尚未实施**
- 适用基线：PathTogether `e7b5f1d`
- 范围：PathTogether owner 引导、登录标识、密码生命周期、Web 会话、用户管理、
  账户存储收口
- 关联能力：现有邀请注册、登录防爆破、CSRF、用户 AI 权限与预算保持兼容，
  不在本方案中重做

---

## 1. 决策摘要

本轮把账户系统收敛为以下简单模型：

1. **PostgreSQL 是生产账户的唯一事实源**。环境变量只允许在空库时创建首个
   owner，不再参与已有账号的密码对账。
2. 当前产品明确采用**单一 enabled owner**。每次启动从数据库解析 owner 身份，
   与是否执行 bootstrap 完全分离。
3. 每个账号只有一个大小写不敏感的唯一登录标识 `login_id`；`display_name` 只负责
   展示、允许重复、绝不参与登录。
4. owner 和普通 user 都可在已登录状态下凭当前密码修改自己的密码；忘记 owner 密码
   只能由主机侧显式 break-glass CLI 恢复。
5. 密码修改、管理员重置、break-glass、禁用、重新启用或角色变化后，所有旧 Web
   session 必须立即失效。
6. 继续保留 `closed | invite_only | public` 注册模式，但本阶段仍只支持 `closed` 和
   `invite_only`；邀请注册的原子事务、限流、CSRF、AI 权限模板与预算隔离不改语义。
7. JSON/dual 清理与上述安全修复分批进行，避免一次部署同时改变凭据、登录命名空间和
   存储后端。

本方案不建设邮箱验证、邮件找回、短信、MFA、组织/租户、多个管理员角色或公开注册。
这些能力当前没有明确产品需求，引入后会显著增加账户状态和运维依赖。

## 2. 已确认的现状与根因

### 2.1 owner 密码存在双源真相

`app.py:_bootstrap_owner()` 在每次进程启动时读取 `ADMIN_USERNAME` 和
`ADMIN_PASSWORD`，随后调用 `user_store.ensure_owner()`：

- 没有 owner 时创建首个 owner；
- 已有 owner 时忽略传入的用户名，选择最早创建的 owner；
- env 密码与数据库 hash 不匹配时，直接重写数据库密码。

与此同时，`POST /api/admin/users/<user_id>/password` 又允许在数据库中修改任意用户
密码，包括 owner。后者的修改会在下次启动时被 env 值覆盖，且没有用户可见提示。

### 2.2 `ADMIN_USERNAME` 只在首建时有效

数据库已有 owner 后，`ensure_owner()` 按 `role='owner'` 和创建时间取第一条记录，
不再使用 `ADMIN_USERNAME` 定位账号。因此该变量在生产长期被要求提供，却只在第一次
创建时有实际意义。

### 2.3 bootstrap 失败可能留下不可登录服务

`_bootstrap_owner()` 吞掉所有异常并返回 `None`。如果 env 中仍有
`ADMIN_PASSWORD`，`_AUTH_BY_PASSWORD` 会使认证保持开启；最终可能形成应用在线、
认证开启、却没有可验证 owner 的锁死状态。

PG schema 初始化已经采用 fail-fast，账户初始化不应采用更弱的错误策略。

### 2.4 owner bootstrap 还承担资源默认归属注入

`_bootstrap_owner()` 的返回值被传给 `share_store.set_owner_user_id()`。部分旧式
`set_slide_meta`、project、ROI 写路径在调用方没有显式传 `owner_user_id` 时，会使用该
进程级默认值。

因此不能只删除 `ADMIN_PASSWORD` 和 `_bootstrap_owner()`。否则启动后
`_OWNER_USER_ID` 为空，旧路径创建的资源可能静默落成 `owner_user_id=NULL`。

### 2.5 登录标识存在两个不一致的命名空间

`verify_user()` 先按 `lower(email)` 查找，查不到再按 `display_name` 精确匹配。
数据库只有 `lower(email)` 唯一索引，`display_name` 没有唯一约束；PG 查询重复显示名时
也没有稳定排序。

这会产生两类歧义：

- 多个账号使用同一个显示名；
- 账号 A 的 display name 等于账号 B 的 email/用户名，登录解析优先命中 B。

给 `display_name` 增加唯一约束会把展示属性错误地变成身份属性。正确修复是让它退出
登录解析。

### 2.6 密码变化不会废止已有 session

Flask permanent session 有效期为 7 天。每次受保护请求虽然会按 `user_id` 回查数据库，
但只校验用户存在且未 disabled，不校验密码或凭据版本。因此：

- 修改或重置密码后，旧 Cookie 仍可使用；
- 用户被禁用后，如果旧 Cookie 未在禁用期间发起请求，而账号又被重新启用，旧 Cookie
  可能继续有效。

### 2.7 密码入口和审计口径不统一

用户创建、邀请注册和管理员重置均使用 8 位最小长度，但分别在路由、store 和模板中
重复定义。管理员密码重置没有写 audit，且 owner 可经管理端点直接重置自己的密码，
依赖 env 作为兜底。

### 2.8 JSON/PG 双实现已经不再代表完整生产能力

登录防爆破和邀请注册均要求 PostgreSQL；json/dual 下登录写操作会 fail-closed。生产也
已经使用 PostgreSQL。`user_store_json.py`、dual mirror 和每次启动执行的
`repair_empty_password_hashes_from_json()` 因而主要是迁移残留，却仍扩大所有用户逻辑
修改的实现面。

## 3. 目标账户模型

### 3.1 用户字段语义

| 字段 | 语义 | 约束 |
|---|---|---|
| `user_id` | 稳定内部身份 | 主键，不向用户编辑 |
| `login_id` | 唯一登录账号，可为用户名或邮箱形式 | trim + lowercase，大小写不敏感唯一 |
| `display_name` | 界面展示名 | 可空、可重复、不得用于认证 |
| `password_hash` | Werkzeug 生成的密码 hash | 永不输出到 API、日志或 audit |
| `role` | 当前为 `owner | user` | 生产只允许一个 enabled owner |
| `disabled` | 账号禁用状态 | 变化时递增 `auth_version` |
| `auth_version` | Web session 凭据版本 | 正整数，安全相关变化时原子递增 |
| `ai_access` / `ai_config` | 现有 AI 权限与个人配置 | 保持现有语义 |

本阶段不新增 `email` 业务概念。当前没有邮件验证和邮件找回，把任意用户名继续叫 email
只会保留语义债。未来如果引入邮件能力，应单独增加 `verified_email`，不能复用
`display_name` 或未经验证的 `login_id`。

### 3.2 owner 不变量

生产开启认证时必须满足：

1. 数据库中恰好存在一个 `role='owner' AND disabled=false` 的账号；
2. owner 的 `password_hash` 非空且可由当前 Werkzeug 识别；
3. 每个进程启动后都能解析到同一个 owner `user_id`，并注入资源默认归属；
4. 已有 owner 时，任何 bootstrap 环境变量都不得修改其 login ID、display name、密码、
   disabled 或 role；
5. owner 不能通过 Web 管理端点被禁用、删除、降级或重置密码；正常修改密码走本人
   change-password，失联恢复走主机侧 break-glass。

当前不设计多 owner。若以后需要多人管理，应单独引入 `admin` 角色和权限矩阵，而不是
让多个 owner 共享一个隐式的“第一条 owner”规则。

### 3.3 密码策略

统一服务端常量，例如：

```python
PASSWORD_MIN_LENGTH = 15
PASSWORD_MAX_LENGTH = 200
```

规则：

- 新建、邀请兑换、自助修改、管理员重置和 break-glass 使用同一长度校验；
- 不要求大写、小写、数字和符号组合；允许空格和长口令；
- 不做周期性强制改密；
- 存量短于 15 位的 hash 继续允许登录，不强制批量失效；下一次修改或重置时执行新规则；
- 错误响应和 audit 不得包含密码、hash、长度以外的密码特征；
- 当前 UI 最大 200 字符可保留，服务端必须有同样上限，避免异常大输入。

此口径对齐 NIST SP 800-63B 对纯密码认证的当前建议：最小 15 字符、允许至少 64 字符、
不采用组合规则或无风险依据的周期轮换。

## 4. Schema 与兼容迁移

### 4.1 第一批 migration：会话版本

新增 `0015_account_auth_version.sql`：

```sql
ALTER TABLE users
    ADD COLUMN IF NOT EXISTS auth_version BIGINT NOT NULL DEFAULT 1;

ALTER TABLE users
    ADD CONSTRAINT users_auth_version_positive
    CHECK (auth_version >= 1) NOT VALID;

ALTER TABLE users
    VALIDATE CONSTRAINT users_auth_version_positive;

CREATE UNIQUE INDEX IF NOT EXISTS users_single_enabled_owner_key
    ON users (role)
    WHERE role = 'owner' AND NOT disabled;
```

`auth_version` 是内部安全字段：

- `get_user()` / 登录验证需要返回；
- `list_users()` 和公共用户 API 不必暴露；
- session 只存当次登录版本整数；
- 修改密码、disable、enable、role 变化必须和版本递增处于同一数据库事务。

第一批不物理重命名 `users.email`，保证旧镜像可读取新 schema，保留快速回滚能力。
store 内先把它映射为 `login_id`；API 在一个兼容窗口内可以同时返回：

```json
{
  "login_id": "alice",
  "email": "alice"
}
```

其中 `email` 明确标记 deprecated，前端只读 `login_id`。

部分唯一索引把“最多一个 enabled owner”落实到数据库。部署 migration 前必须先执行
§4.3 审计；如果现存 enabled owner 多于一个，应先人工明确主 owner 并处理其余账号，
不得让 migration 靠创建索引失败来替代决策。

### 4.2 第二批 migration：物理字段收口

完成兼容窗口并确认没有旧客户端后再执行物理重命名。可选择 PostgreSQL 事务内直接：

```sql
ALTER TABLE users RENAME COLUMN email TO login_id;
ALTER INDEX users_email_ci_key RENAME TO users_login_id_ci_key;
ALTER TABLE registration_invites
    RENAME COLUMN email_normalized TO login_id_normalized;
```

如果仍要求旧镜像可随时回滚，则采用 expand/contract：先加新列并双写，切读后再删除
旧列。不得在第一批凭据修复中同时完成 contract 删除。

### 4.3 上线前数据审计

部署前必须输出计数和 user_id，不输出 password hash：

```sql
-- enabled owner 必须恰好一条
SELECT user_id, email, disabled
FROM users
WHERE role = 'owner';

-- 重复 display name 允许存在，但需记录迁移影响
SELECT display_name, count(*)
FROM users
WHERE display_name <> ''
GROUP BY display_name
HAVING count(*) > 1;

-- display name 与其他账号 login_id 冲突：改后只影响旧登录习惯
SELECT a.user_id AS display_owner, a.display_name,
       b.user_id AS login_owner, b.email AS login_id
FROM users a
JOIN users b ON lower(a.display_name) = lower(b.email)
WHERE a.user_id <> b.user_id;

-- 密码 hash 空行必须为 0
SELECT count(*) FROM users WHERE password_hash = '';
```

## 5. 启动状态机

### 5.1 新配置语义

推荐变量：

| 配置 | 新语义 |
|---|---|
| `REQUIRE_ADMIN_AUTH=1` | 生产必须启用 PG 账户认证并存在可用 owner |
| `BOOTSTRAP_OWNER_LOGIN_ID` | 仅空库首建 owner 时读取 |
| `BOOTSTRAP_OWNER_PASSWORD_FILE` | 仅空库首建 owner 时读取的 secret 文件 |
| `ADMIN_USERNAME` | 一版兼容别名，仅空库时读取并告警 deprecated |
| `ADMIN_PASSWORD` | 一版兼容别名，仅空库时读取并告警 deprecated；已有 owner 时不得比较密码 |

不建议长期保留 `BOOTSTRAP_OWNER_PASSWORD` 明文 env。容器首建时优先挂载 Podman
secret 文件；建号成功、验证登录后从容器配置移除。

### 5.2 启动算法

启动顺序：

```text
ensure_pg_schema_or_exit()
    ↓
query all enabled owners
    ├─ 1 个：resolve_primary_owner()，忽略 bootstrap 密码
    ├─ 0 个 + 空 users 表 + bootstrap secret：事务创建 owner，再解析
    ├─ 0 个 + 已有非 owner 用户：拒绝启动
    ├─ 0 个 + 无 bootstrap secret：REQUIRE_ADMIN_AUTH=1 时拒绝启动
    └─ >1 个：拒绝启动，禁止选择“第一个”
    ↓
validate owner password_hash 非空
    ↓
share_store.set_owner_user_id(owner.user_id)
    ↓
AUTH_ENABLED = REQUIRE_ADMIN_AUTH 或存在 enabled 用户
```

所有数据库异常均 fail-fast，错误消息只说明阶段与修复动作，不输出 login ID 之外的敏感
内容。不得继续使用“捕获所有异常并返回 None”的路径。

### 5.3 store API 调整

删除含糊的 `ensure_owner()` 和 `first_owner()` 业务语义，改为：

```python
list_enabled_owners() -> list[dict]
create_bootstrap_owner(login_id, password) -> dict
resolve_primary_owner() -> dict
```

`create_bootstrap_owner` 必须在 PG 事务内再次锁定/检查 owner 数量，避免 gunicorn 多
worker 同时首启创建多个 owner。使用**专用 advisory lock key**（不复用 schema 初始化的
`0x53565347`，避免两个启动阶段互相串行耦合），加锁后查询和插入；部分唯一索引
`users_single_enabled_owner_key` 作为数据库层兜底。

`resolve_primary_owner()` 只解析和验证身份，永不接受密码参数、永不写用户行。

## 6. 登录与 Web session

### 6.1 登录只认 `login_id`

`verify_user()` 改为：

```text
normalize(login_id)
    → 按唯一索引查用户
    → 检查 disabled
    → check_password_hash
```

删除 `get_user_by_display_name()` 在认证路径中的 fallback。该读取函数如仍被展示功能使用
可暂留，但不得从 `verify_user()` 调用；若没有其他调用则一并删除。

登录页继续使用统一“账号”文案，不承诺“邮箱或显示名都可以登录”。失败响应仍统一
为“账号或密码错误”，不泄露账号存在性。登录限流主体使用规范化 `login_id` 的摘要。

### 6.2 session 版本校验

登录成功后：

```python
session.clear()
session.permanent = True
session["user_id"] = user["user_id"]
session["auth_version"] = user["auth_version"]
session["role"] = user["role"]
session["auth_user"] = user["display_name"] or user["login_id"]
rotate_csrf_token()
```

每次受保护请求已存在一次 `get_user(user_id)` 回查，本轮只在同一次回查中增加比较：

```text
user 不存在 / disabled / session.auth_version != user.auth_version
    → session.clear()
    → API 返回 401 auth_required；页面进入登录流程
```

不增加新的数据库查询。

以下写操作在同一事务中执行 `auth_version = auth_version + 1`：

- 修改密码；
- 管理员重置普通用户密码；
- break-glass 重置 owner 密码；
- disable；
- enable；
- 未来的 role 变化。

其中 enable 也必须递增，防止账号在禁用期间没有发请求的旧 Cookie 被重新激活。

会话失效范围只覆盖 Web Cookie session。`/api/plugin/*` 的安装级 JWT 不在本轮废止
范围内——token 有自身过期与撤销机制，与密码凭据无派生关系；如未来评估需要联动，
单独立项，不在本方案混入。

## 7. 密码与账户 API

### 7.1 本人修改密码

新增：

```http
POST /api/account/password
Content-Type: application/json

{
  "current_password": "...",
  "new_password": "..."
}
```

规则：

1. 必须是已登录 Cookie session，并通过现有 CSRF；
2. 使用当前数据库 hash 验证 `current_password`；
3. 新密码执行统一长度校验；
4. 新密码不得与当前密码相同；
5. 更新 hash 与递增 `auth_version` 在同一事务；
6. 写 `user.password_change` audit，只含 target user_id 和 `sessions_revoked=true`；
7. 成功后清空当前 session，返回 200，由前端跳转 `/login?password_changed=1`；
8. 当前密码错误统一返回 400 `invalid_current_password`，不写入明文或 hash；
9. `invalid_current_password` 计入现有 `auth_limit_store` 登录失败桶（按规范化
   `login_id` 摘要），命中既有限流阈值后按 429 拒绝。否则持有被窃 Cookie 的攻击者可
   经此端点高速穷举当前密码（尤其存量短密码），把会话窃取升级为完全接管。

owner 和 user 均使用该端点。为了保持实现简单，本轮密码修改后注销全部 session，包括
发起修改的当前会话，不实现“保留本设备”。

### 7.2 owner 重置普通用户密码

保留现有管理端点，但增加：

- target 必须存在且 `role='user'`；owner target 返回 409，并提示使用本人改密或主机侧
  break-glass；
- 使用统一密码策略；
- hash 更新与 `auth_version + 1` 同事务；
- 写 `user.password_reset` audit，包含 actor/target 和 `sessions_revoked=true`；
- 响应不回显密码。

长期建议主要通过邀请注册让用户自行设置密码，管理员创建账号只保留给内网或特殊恢复
场景。本轮不新增邮件找回。

### 7.3 break-glass CLI

新增主机侧命令，例如：

```bash
python3 -m useradmin reset-owner-password \
  --login-id browser_admin \
  --password-stdin
```

CLI 数据库连接复用应用同款配置（`DATABASE_URL` / `pg_store` 连接逻辑），不引入第二套
DSN 来源。

CLI 要求：

- 只能连接 PostgreSQL；json/dual 拒绝执行；
- 必须显式指定唯一 owner login ID，不允许默认“第一条 owner”；
- 新密码只从 stdin 或 `--password-file` 读取，不接受命令行参数值；
- 目标必须恰好是唯一的 `role='owner'` 行。**目标可以是 disabled 的唯一 owner**：
  主机侧 CLI 本身等价于直接改库，若只禁用不破除，会出现「启动状态机要求恰好一个
  enabled owner 拒启 + CLI 要求 enabled 目标拒执」的双向锁死。目标为 disabled 时
  必须显式加 `--enable`，在同一事务内解除禁用、更新 hash 并递增 `auth_version`；
- 若存在 **0 个 owner 行且用户表非空**（owner 行被直接 SQL 删除）：CLI 拒绝并输出
  逃生路径——人工审计后直接 SQL 恢复 owner 行，或清空用户表后走 bootstrap secret。
  CLI 不得在此状态静默建号，避免绕过“已有用户时必须有明确 owner 决策”的不变量；
- 更新 hash 与递增 `auth_version` 同事务；
- 写 `user.password_break_glass_reset` audit，actor 为空或固定 `system`，detail 只含
  `source=local_cli` 与 `sessions_revoked=true`（`--enable` 时追加 `reenabled=true`）；
- 成功输出 user_id、login ID 和“旧 session 已失效”，不输出密码/hash；
- 非 TTY/自动化环境也必须显式提供 stdin/file，不生成不可追踪的随机秘密。

不在 Web UI 提供 owner 忘记密码入口，避免引入邮件 token、恢复码和新的公网攻击面。

## 8. 管理 UI 与邀请注册兼容

### 8.1 用户管理 UI

调整：

- “邮箱 / 用户名”统一改为“登录账号”；
- 显示名继续可选，并注明“不用于登录”；
- 列表展示 `display_name` 和 `login_id`，不再把物理字段名 email 暴露给用户；
- owner 行不显示“重置密码”和“禁用”按钮；
- 普通用户密码重置成功后提示“该用户所有已登录设备均已退出”；
- 增加“修改我的密码”入口，owner/user 均可见。

### 8.2 邀请注册

现有邀请码的高熵 token、HMAC-only 存储、过期/撤销、单次兑换、PG 行锁与原子建号保持
不变。只修改以下账号语义：

- 表单和 API 使用 `login_id`；
- 邀请绑定字段表示“允许兑换的 login ID”，不是已验证邮箱；
- 用户选择的 `display_name` 不参与唯一性或邀请匹配；
- 密码最小长度改用全局常量；
- 兑换成功仍不自动登录，用户返回登录页用新账号登录。

`public` 注册仍返回不支持，不因本轮账户完善而自动开放。

## 9. JSON/dual 收口

### 9.1 第一批只隔离，不删除

凭据修复上线时：

- 生产 `REQUIRE_ADMIN_AUTH=1` 明确要求 `STORAGE_BACKEND=postgres`；
- JSON 只允许本地免认证开发和隔离单元测试；
- dual 不再作为可登录生产形态；
- 保留现有模块文件，避免同批扩大回滚面。

在 JSON adapter 被删除前，它仍需为测试数据补 `auth_version=1`，并在密码、disable、
enable 操作中同步递增；dual mirror 也必须携带该字段。否则同一 dispatcher 在不同后端
会产生不同 session 语义。该兼容实现只服务过渡和测试，不代表继续支持 JSON 生产认证。

### 9.2 将每次启动修复改成显式迁移

删除 app 启动路径上的 `_repair_pg_empty_password_hashes()` 调用。保留一次性命令，例如：

```bash
python3 scripts/repair_pg_user_password_hashes.py --dry-run
python3 scripts/repair_pg_user_password_hashes.py --apply
```

要求：

- 默认 dry-run；输出待修复 user_id 数量，不输出 hash；
- apply 前确认 PG 空 hash 行和 JSON 对应 hash 数量一致；
- 仅填充 PG 空 hash，不覆盖非空 hash；
- 完成后记录迁移执行时间和计数；
- 生产验证空 hash 为 0 后，不再让应用启动读取旧 JSON 用户文件。

### 9.3 最终删除范围

在独立版本中删除：

- `user_store_json.py` 的生产接线；
- `user_store.py` 的 dual mirror；
- `ensure_owner` / `_mirror_user` / 启动修复 shim；
- 只服务于双实现等价性的测试。

仍需要快速无 PG 单测时，可提供内存 fake/repository fixture，而不是保留第二套生产身份
实现。

## 10. 分批实施计划

### 批次 A：凭据与会话闭环

代码：

1. 增加 `auth_version` migration 和 store 字段；
2. 拆分 bootstrap 与 primary owner 解析；
3. 修改 `docker_entry.sh` 和应用启动 fail-fast 规则；
4. 无论是否存在 bootstrap env，都从 DB 注入 owner ID；
5. 登录 session 写入并校验 `auth_version`；
6. 密码/disable/enable 原子递增版本；
7. 新增本人改密与 break-glass CLI；
8. 管理员重置补审计并禁止 target=owner。

兼容：

- 暂时保留 `ADMIN_USERNAME/ADMIN_PASSWORD`；仅数据库无 owner 时读取；
- 数据库已有 owner 时记录一次 deprecated warning，不比较密码；
- `users.email` 物理列保持不动；
- 旧镜像可读取新增列后的 schema。

### 批次 B：登录标识收口

1. store/API 引入 `login_id` 语义别名；
2. `verify_user` 删除 display name fallback；
3. UI/i18n/文档统一“账号/登录账号”；
4. 邀请绑定和表单采用 login ID 语义；
5. 保留旧 `email` JSON 字段一版兼容，记录 deprecated；
6. 上线前执行冲突审计并通知受影响账号：以后必须使用登录账号，不可使用显示名。

### 批次 C：存储与字段 contract

1. 物理重命名 `email → login_id` 和邀请绑定列；
2. 删除旧 API 字段；
3. 移除每次启动的 JSON hash 修复；
4. 完成 PostgreSQL-only 用户 store；
5. 更新部署、恢复和开发文档。

批次 A/B/C 必须分开提交和部署。当前 Files 灰度观察窗结束前不混入批次 A；账户改造
部署后应建立独立观察窗，不复用 Files 指标窗口。

### 预计改动文件

| 文件/模块 | 主要改动 |
|---|---|
| `migrations/0015_account_auth_version.sql` | `auth_version`、正数约束、单 enabled owner 唯一索引 |
| `user_store_pg.py` | login ID 查询、owner 解析、事务式凭据更新与版本递增 |
| `user_store_json.py` | 过渡期 `auth_version` 兼容，删除 display name 登录 fallback |
| `user_store.py` | 新公共 API 与 dual 过渡镜像；批次 C 删除 dual |
| `app.py` | 启动状态机、session 校验、自助改密、管理重置限制与 audit |
| `docker_entry.sh` | 不再要求已有 DB 每次提供 `ADMIN_PASSWORD`；生产强制 PG |
| `useradmin.py` 或 `scripts/useradmin.py` | 主机侧 break-glass CLI |
| `templates/login.html`、`templates/_app_shell.html` | 账号文案、本人改密入口、owner 操作隐藏 |
| `static/app.js`、`static/i18n.js` | login ID 字段、自助改密和会话失效提示 |
| `registration_store.py`、`templates/register.html` | 统一密码策略与 login ID 语义 |
| `tests/test_user_store.py` 等 | 本文 §11 全部回归矩阵 |
| `docs/demo-deployment.md` | bootstrap、secret、break-glass、部署与回滚命令 |

## 11. 测试矩阵

### 11.1 owner bootstrap 与启动

- 空 PG + bootstrap secret：仅创建一个 owner；并发多 worker 也只创建一次；
- 已有 owner + env 密码不同：启动后 DB hash 不变；
- 已有 owner + 无 `ADMIN_PASSWORD`：生产正常启动并可登录；
- 已有 owner + 改动 `ADMIN_USERNAME`：不改 DB login ID，只告警 deprecated；
- 无 owner但已有普通 user：`REQUIRE_ADMIN_AUTH=1` 拒绝启动；
- 两个 enabled owner：拒绝启动，不选择 first owner；
- owner hash 为空或数据库查询异常：拒绝启动；
- 成功启动后 `share_store` 默认 owner ID 正确；旧式 slide/project/ROI 写入不会产生空归属。

### 11.2 登录标识

- login ID 大小写不敏感且唯一；
- 正确 login ID 可登录；
- display name 与 login ID 不同，只用 display name 登录失败；
- 两人同 display name 不影响登录；
- A 的 display name 等于 B 的 login ID 时，只有 B 可用该 login ID 登录；
- 错误账号/密码继续返回统一文案；登录限流仍命中同一规范化主体。

### 11.3 session 失效

- 本人改密后当前 Cookie 和另一浏览器 Cookie 均 401；
- owner 重置普通用户密码后旧 Cookie 立即 401；
- break-glass 后所有 owner 旧 Cookie 立即 401；
- disable 后旧 Cookie 401；
- disable 后未访问、再 enable，旧 Cookie 仍因版本不匹配而 401；
- role 变化时旧 Cookie 失效；
- 部署前签发的旧 Cookie（无 `auth_version` 字段）上线后首次请求即 401，且同一
  Cookie 不会因为回填兼容而复活；
- 普通请求仍只有一次用户 DB 回查。

### 11.4 密码与审计

- 所有入口统一拒绝短于 15 或长于 200 字符的新密码；
- 存量短密码仍可登录，修改时必须满足新规则；
- 当前密码错误不能改密；新密码等于旧密码拒绝；
- 当前密码连续错误计入登录失败桶，达到阈值后改密端点按 429 拒绝；
- Web owner reset 返回 409；CLI owner reset 成功；
- CLI 对 disabled 唯一 owner 带 `--enable` 可恢复（解除禁用 + 重置 + 版本递增同
  事务），不带 `--enable` 拒绝；应用随后可正常启动；
- CLI 对「0 个 owner 行且用户表非空」拒绝并输出逃生路径，不静默建号；
- audit 存在正确 action/actor/target，但不含 password/hash；
- 用户列表、API、日志、异常和 CLI 输出均无 hash/明文。

### 11.5 邀请与既有能力回归

- closed/invite_only/public fail-closed 行为不变；
- 邀请 token 仍一次性、可撤销、可过期且只存 HMAC；
- 邀请兑换、用户创建和审计仍同一 PG 事务；
- display name 重复不影响兑换；login ID 冲突不消费邀请码；
- 登录防爆破、CSRF、AI access、用户预算、匿名 Demo、分享访问和插件鉴权全量回归。

## 12. 部署与验证

### 12.1 部署前

1. 备份 PostgreSQL；记录恢复命令并实际验证备份可读；
2. 执行 §4.3 数据审计，确认恰好一个 enabled owner、空 hash 为 0；
3. 保存当前 owner user_id/login ID 的非敏感记录；
4. 准备 bootstrap/break-glass secret，但不写命令历史或仓库；
5. 在隔离 PG 或容器副本完成“改密→重启→旧密码不恢复”演练；
6. 构建镜像并运行账户、注册、访问控制、审计与迁移测试。

### 12.2 批次 A 部署

第一轮仍保留旧 `ADMIN_*` env，以便旧镜像可回滚；新代码在已有 owner 时必须忽略这些
凭据。

**预期用户影响（须提前周知）**：本次上线会使**所有现存 Web 会话一次性失效**。旧
Cookie 不含 `auth_version`，首次请求即版本不匹配被清理，全体用户（含 owner）需重新
登录一次。这是有意行为：凭据安全版本不做“缺失版本首请求回填”式兼容，避免削弱会话
失效语义。部署通告中写明这一点，避免被当作故障上报。

部署后验证：

1. owner 用当前 DB 密码登录；
2. 修改 owner 密码并确认立即登出；
3. 新密码重新登录；
4. 重启所有 worker；
5. 新密码仍有效、旧密码无效，证明启动不再对账覆盖；
6. 创建一个测试资源并确认 `owner_user_id` 正确；
7. 用部署前 Cookie 请求受保护 API，确认 401；
8. 查看 audit 中 password change 事件，无敏感信息；
9. healthz、匿名 Demo、邀请页、用户管理和 AI 入口正常。

观察稳定并结束旧镜像快速回滚窗口后，才从容器配置删除常驻 `ADMIN_PASSWORD`，同时更新
`docker_entry.sh` 和部署文档，不再要求已有数据库每次提供 bootstrap secret。

### 12.3 监控

账户改造不复用 Files 上传指标。建议观察：

- 启动失败和 owner invariant 告警；
- `/login` 401/429/503 比例；
- `auth_version` mismatch 导致的 session 清理数量；
- 密码修改/重置/break-glass audit 数量；
- 用户管理 API 4xx/5xx；
- `owner_user_id IS NULL` 的新增资源数量必须为 0；
- 邀请兑换成功/失败原因和登录限流是否异常上升。

## 13. 回滚方案

### 13.1 批次 A

`auth_version` 新列对旧代码无害，回滚时不删除。需要注意：旧镜像会恢复“启动按
`ADMIN_PASSWORD` 覆盖 owner 密码”的行为。因此快速回滚窗口内必须保留旧 env，并明确
回滚后 owner 密码会回到该 env 值。

回滚步骤：

1. 切回旧镜像；
2. 保持旧 `ADMIN_USERNAME/ADMIN_PASSWORD` 配置；
3. 重启后使用旧 env 密码验证 owner 登录；
4. 检查 owner ID 注入和资源写入；
5. 记录回滚造成的密码状态变化并通知 owner。

删除 `ADMIN_PASSWORD` 后若仍要回滚旧镜像，必须临时重新注入旧 env，否则
`docker_entry.sh` 会因 `REQUIRE_ADMIN_AUTH=1` 拒绝启动。

### 13.2 批次 B/C

- 批次 B 兼容窗口内 API 同时返回 `login_id`/`email`，可直接回滚代码；
- 物理字段 contract 前必须保留完整 PG 备份和旧字段；
- 一旦删除旧列或 JSON/dual 实现，不再承诺旧镜像原地回滚，应按数据库备份恢复；
- 不执行破坏性 migration down；优先前向修复。

## 14. Go / No-Go 门槛

满足以下条件才可标记“已实施”：

- schema migration、类型检查和全量测试通过；
- owner 密码修改后重启不会回滚；
- 无常驻 `ADMIN_PASSWORD` 时现有 PG owner 可正常启动和登录；
- 所有密码/账号状态变化都能废止旧 session；
- display name 不再参与登录；
- owner Web 重置被禁止，break-glass CLI 实测可恢复（含 disabled owner 经 `--enable`
  的恢复路径）；
- owner ID 始终注入，新增资源空归属为 0；
- password reset/change/break-glass 审计完整且无敏感信息；
- 邀请注册、匿名 Demo、分享、AI、预算和插件能力无回归；
- 部署文档、恢复手册和容器配置已同步；
- 浏览器完成 owner/user 双角色的登录、改密、退出、重登、禁用/启用和邀请兑换
  手工验收。

任一项未满足时，文档状态只能保持“实施中”或“待验收”，不得仅凭单元测试通过标记
完成。

## 15. 明确不采用的方案

1. **已有 owner 时 env 密码不匹配只告警**：仍保留两个密码来源和持续 secret 暴露；
   本方案选择已有 owner 后完全不读取密码。
2. **`ADMIN_PASSWORD_RESET=1` 启动开关**：容器配置残留会重复执行；改用一次性 CLI。
3. **给 `display_name` 加唯一索引**：把展示属性误作身份属性；应从登录路径移除。
4. **立即建设邮箱验证/邮件找回**：当前没有 SMTP、验证状态和投递运维；owner 管理重置
   加本地 break-glass 已覆盖现阶段恢复需求。
5. **凭据修复、login ID 物理改名、删除 JSON/dual 同批上线**：回滚面过大；必须分批。
6. **继续以“第一个 owner”为系统 owner**：多行时结果依赖创建顺序，无法表达明确产品
   所有权；单 owner invariant 不满足时应拒绝启动。

## 16. 参考

- NIST SP 800-63B：<https://pages.nist.gov/800-63-4/sp800-63b.html>
- OWASP Authentication Cheat Sheet：
  <https://cheatsheetseries.owasp.org/cheatsheets/Authentication_Cheat_Sheet.html>
- OWASP Session Management Cheat Sheet：
  <https://cheatsheetseries.owasp.org/cheatsheets/Session_Management_Cheat_Sheet.html>
- OWASP Forgot Password Cheat Sheet：
  <https://cheatsheetseries.owasp.org/cheatsheets/Forgot_Password_Cheat_Sheet.html>
- Podman secret inspect：
  <https://docs.podman.io/en/latest/markdown/podman-secret-inspect.1.html>
- 邀请注册原始方案（其 P0-B 主体已由当前代码实施）：
  `docs/open-registration-security-remediation.md`
- 部署基线：`docs/demo-deployment.md`
