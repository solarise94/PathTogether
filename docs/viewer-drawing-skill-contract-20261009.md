# AI 描绘改按需技能：跨仓服务端契约（提案）

日期：2026-10-09。状态：**契约提案，待主代理批准；未实施**。
上游设计：`docs/viewer-interaction-skills-review-design-20261009.md` §5 / R6 / §7-E、F。
基线：PathTogether `5328fefc`（vi-skill）；HistoPilot `23e89fa`（release/hp-0.3.5）。
行号对应该两基线，实施后以函数名定位。

---

## 0. 现状梳理（证据基线）

### 0.1 PathTogether

**run 启动入口**（均经 `_ai_run_prepare`，app.py:15216）：
- `POST /api/ai/run` app.py:20885：`_ai_run_slide_ref`（app.py:15190，view+annotate
  双复核）→ `allow_ai_drawing` 仅创建参数（严格布尔、fresh-only，app.py:20932-20941）
  → viewport/attachments 校验（app.py:20943-20958）→ `_ai_run_prepare`
  → 显式白名单 payload 转发 sidecar（app.py:20963-20989，未知 body 键不透传）。
- `POST /api/ai/continue` app.py:21034、`/api/ai/ask` app.py:21113（fork 无写工具，
  `need_grant=False`）。
- `_ai_run_prepare` 顺序：维护闸 → request_id 校验/生成（`_parse_client_request_id`
  app.py:14619，`^[A-Za-z0-9_-]{1,128}$`）→ `_build_sidecar_config`
  （app.py:16432；凭据只来自平台 `_resolve_ai_credentials` app.py:16028，**PT 从不
  直接调模型**）→ `need_grant` 时 `_issue_run_grant`（app.py:14525）→
  extra_tools 注入（app.py:15113，附 `AGENT_EXTRA_TOOLS_ENVELOPE` app.py:6269）
  → 预算预占（拒绝即撤 grant，app.py:15276）。
- grant 生命周期回调：`on_rejected` → 撤销（app.py:15301）；
  `on_finished` → `_revoke_run_grants_for_session(session, "run_finished")`
  （app.py:15305-15307）。`_proxy_sse`（app.py:21732）明确：**上游 SSE 正常结束**
  才回调 on_finished；客户端提前断开不撤——sidecar run 后台续跑、grant 保持有效。
- `POST /api/ai/cancel` app.py:21241：sidecar 2xx 后按回显 session 撤销全部绑定
  grant（app.py:21288）。
- 去重三层：PT request_id（预算幂等）+ HP request_id 预占
  （`withIdempotentRequest` agent-runner.ts:1184，dedup/retry 两模式）+
  annotate `effect_key`（add_roi `_effect_key` 幂等）。

**run_grants 表**：migrations/0005_plugin.sql:31
（grant_id/installation_id/slide/session_id 缺省空串/created_by_user_id/
created_at/expires_at/revoked/revoked_at）；0067_slide_asset_identity.sql:174 加
`slide_id`。TTL 缺省 1800s（`_RUN_GRANT_TTL_SECONDS` app.py:13599），临时查看
主体再取 min(剩余期)。share_store_pg.py：`create_run_grant` :3559、`get_run_grant`
:3598、`revoke_run_grant` :3609（幂等）、`list_run_grants_for_session` :3623、
`bind_run_grant_session` :3639（CAS 空→session）、`list_run_grants` :3671。
校验 `_verify_run_grant` app.py:18477：存在/未撤/未过期/slide_id 匹配/
installation 匹配/创建者复查（`_run_grant_creator_allowed` app.py:18427）/
`expect_session` 绑定匹配（grant_unbound、session_mismatch 稳定码）。
机器端点：bind app.py:19332、verify app.py:19369（稳定 reason 码）、DELETE 撤销
app.py:19306；人类撤销 `DELETE /api/ai/run-grants/<id>` app.py:21293
（owner 或创建者）。权限撤销联动：`_revoke_stale_run_grants` app.py:18574、
`_cancel_sidecar_runs_for_owners` app.py:18594。

**ai_session_drawing_flags**（0039 建表 + 0041 加单调 generation）：HP 权威开关的
PT 本地镜像。share_store_pg.py：读 :3719（无行返回 None，调用方 fail-closed）、
upsert :3740、init-if-absent :3771、reserve generation :3815、CAS :3845。
代理端点 `POST /api/ai/session/<sid>/drawing` app.py:21430：关闭先预写 false 再
转发；开启先原子占代；`drawing_generation` 随请求传 HP（HP 侧 server.ts:888
lock 内只应用更高代）；回程按本请求占到的代 CAS。session detail 回填
`drawing_mirror`（app.py:21345-21376）。

**AI 两条写路径的开关检查**：
- `/internal/ai/annotate` app.py:17941（`X-AI-Internal-Token`，app.py:847）：
  `type=polygon|freehand` 时查镜像开关，无行/false → 403 `ai_drawing_disabled`
  （app.py:18007-18017，P1-4：internal token 不再单独放行）；多边形必带
  snapshot_id（`_parse_snapshot_provenance` app.py:2532）+ HP attestation
  （`_check_snapshot_attestation` app.py:2708 / `_verify_snapshot_attestation`
  app.py:2616：HMAC(internal token, domain `hp-snapshot-attest-v1`)、exp、
  sid 归属、四字段逐项一致、rev 不符 409）。**该路径无 run grant**。
- `/api/plugin/v1/slides/<slide>/annotations` app.py:19096（by-id :19296）：
  Bearer plugin JWT `annotation:write` → `_verify_run_grant`（含
  expect_session，app.py:19129）→ polygon/freehand 同样查镜像（app.py:19149-19156）
  → 同套 snapshot/attestation/几何校验 → `created_by_user_id` 从 grant 来
  （app.py:19253）。矩形路径不受描绘闸影响（app.py:19205-19222）。

**矩形 create_annotation 与 polygon 的区别**：矩形走 x/y + width_px/height_px（v2）
或 side_px（v1）parser（`_parse_rect_request_geom`），不检查描绘开关、不要求
snapshot/attestation；polygon/freehand 走点列（3~500 点、简单多边形校验
`_validate_annotation_points` app.py:2492），要求开关 + snapshot 溯源。两者落库均
`source="ai"`、`review_status` 缺省 pending、`_effect_key` 幂等。

**迟到写入现有处理**：run 终态（SSE 上游结束 on_finished）、cancel、TTL 到期、
权限撤销钩子均撤 grant → plugin v1 写路径 403 `run_grant_invalid`；HP 侧工具
execute 重读 session 开关（tools.ts:1943-1948）拒绝运行中关闭后的描绘。镜像关闭
预写（app.py:21461）防「HP 已关、PT 残留 true」的迟到写入。

### 0.2 HistoPilot

- **server.ts**：`/run` 处理器 :341——`allow_ai_drawing` 严格布尔边界校验
  （:379-382）→ `runMain`（:400）。`POST /session/:id/drawing` `handleDrawingToggle`
  :866：`drawing_generation` 必填正整数（:879-886），lock 内只应用更高代（:888-899）。
  session detail 透出 `allow_ai_drawing`（:807）。
- **agent-runner.ts**：`RunConfig.allow_ai_drawing` :284-290（仅服务端写入，请求体
  config 同名字段不采信）。`runMain` :923：`enforceWriteRunGrant("main")` :950
  （session 创建前 pre-verify，GrantError fail-closed，实现 :1609）→ request_id
  预占 :1184 → `bindRunGrantToSession` :1146（→PT bind 端点，实现 :1653）→
  fresh 创建参数写入 session（**早于 driveMain kick**，:1153-1181，结构性
  happens-before）→ `driveMain`。工具上下文 `toolCtx.cfg.allow_ai_drawing =
  sessionForKind?.allow_ai_drawing === true`（:2874，session 持久化字段为权威）。
- **tools.ts**：`draw_suspicious_region` :1884-2109。第一层 `ctx.cfg.allow_ai_drawing
  !== true` 拒绝（:1920）；只读 profile 拒绝（:1925）；branch 写入上限（:1936）；
  第二层 execute 期重读 session 字段（:1943-1948）；必须绑定 pending 快照（:1951）；
  `effect_key = sessionId:bundleSeq:toolCallId`（:2004）；annotate 携带
  `runGrant` + snapshot 溯源 + `signSnapshotAttestation`（:2019-2057）；
  `ai_drawing_disabled` 软拒绝（:2062）；致命权限错误停 Agent（:2071）。
  装配：仅 `cfg.allow_ai_drawing === true` 追加该工具（:2219）；只读 profile
  恒不装配（:2206）；fork 无工具。矩形 `create_annotation` 为独立工具，不受开关影响。
- **session-store.ts**：`SessionData.allow_ai_drawing` :410-414、
  `drawing_generation` :416。
- **UI**（integrations/pathtogether/ui/）：sessions.js checkbox
  `#hp-ai-drawing-toggle`（:585-603）、草稿待定选择（:669-672）、
  `POST /api/ai/session/:id/drawing`（:682）、per-session epoch（:679）、
  mirror.stale 提示（:53-59）；main.js `startAiRun` :254——草稿选择为开时首发 body
  带 `allow_ai_drawing: true`（:286）。
- **能力/协议协商**：security envelope（security-envelope.ts）：`security_contract_version
  "1.0"` + `required_features` 闭合集（demo-readonly-v1 / ephemeral / ttl /
  **extra-tools:v1** / **tool-profile:standard-v1**），未知 feature → 4xx fail-closed。
  manifest.json：`pluginVersion 0.3.3`、`pluginContractVersion 1.0.0`、
  `bridgeProtocolVersion 1.0.0`。
- **模型路由**：HP 无独立路由层——单一模型来自 PT 注入的 config
  （pi-model.ts `buildModel`）；PT 不调模型。故「复用现有模型路由步骤」实际指
  复用 HP 已有的模型引擎/请求栈做一次结构化判定调用，而不是既有现成路由组件。

### 0.3 现有测试与保护

- PT `tests/test_ai_drawing_gate.py`（约 45 例）：默认拒绝（无镜像行 fail-closed
  :131）、伪造 polygon 拒绝 :144、开启放行 :156、**运行中关闭后迟到写入拒绝**
  :169、freehand 同闸 :190、矩形路径不受影响 :205、**仍要求 run grant** :218、
  代理镜像语义/CAS/代序 :260-676、snapshot 溯源与 attestation 伪造/跨会话/过期/
  篡改/错钥拒绝 :678-930、internal 通道同闸 :930/:942。
- PT `tests/test_ai_drawing_geom.py`：polygon/freehand 几何校验、pending、
  effect_key、矩形回归。
- PT `tests/test_plugin_api.py`：grant verify 端点 :515、**annotate 必须有效
  grant** :539、provenance 从 grant 来（不采信请求体）:563、撤销/过期 :606、
  撤销权限 :642。
- HP `test/ai-drawing.test.ts`：默认关不装配 + execute 纵深拒绝 :145/:153、
  只读+true 不升级 :168、**运行中关闭迟到拒绝** :185、合法 polygon :211、
  无 pending 快照 :240、软拒绝 :268/:289、几何拒绝 :302-370。
- HP `test/ai-drawing-provenance.test.ts`（溯源字段）、
  `test/ui-drawing-toggle.test.ts`（开关启用条件/草稿/首发 body/running 禁用/
  C2 镜像对照）。
- 跨仓 `test/ai-drawing-contract.integration.test.ts`：**真实 PT Flask + 内嵌
  PostgreSQL + 真实插件 JWT 通道** 对真实 HP 客户端栈；断言镜像闸门 + provenance
  逐键落库；`npm run test:contract`，`PATHTOGETHER_REPO` 指向 PT checkout，缺
  checkout 显式 fail 不 skip。

---

## 1. 目标与非目标

目标：把描绘授权从「会话级常驻开关」收敛为「按需技能 × 一次逻辑 run」，贯通
浏览器→PT→HP→写入口；不破坏现有默认拒绝、迟到写入拒绝、身份隔离三保护。

非目标：不删 `ai_session_drawing_flags` 表与 `/drawing` 端点（保留历史与过渡兼容）；
不改矩形 `create_annotation` 授权语义；不做技能市场/多技能框架；不放开既有
数量、预算、时长限制。

---

## 2. 请求契约（浏览器 → PT → HP）

### 2.1 浏览器 → PT（POST /api/ai/run | /api/ai/continue body 新增键）

```json
{
  "task": "帮我把选框里的腺体圈出来",
  "requested_skills": ["delineate"],
  "skill_intent": "explicit",
  "dismissed_skills": [],
  "target": {
    "slide_id": "sld_example",
    "asset_revision": "mtime:size",
    "kind": "viewport | roi | annotation | slide",
    "bbox": { "x": 100, "y": 200, "w": 800, "h": 600 },
    "annotation_id": "ann_xxx",
    "annotation_revision": 3
  }
}
```

- `requested_skills`：候选仅 `delineate`（白名单；未知值 400 invalid_request，
  不静默忽略）。`skill_intent`：`explicit`（@ 标签）| `natural`（命中自然语言
  预估）| 缺省。二者与 `dismissed_skills` **全部只是提示（claims），不是凭证**。
- `dismissed_skills: ["delineate"]`：用户删除了「描绘 · 本次」标签——本条消息
  显式关闭，判定层必须尊重（否定信号），不得解析后又加回。
- `target`：发送时冻结的目标范围。默认范围顺序：用户明确范围 > 当前选框/选中
  标记 > 当前视野；`kind=slide` 为明确全片（沿用既有分步观察/预算机制）。
  `annotation_id+annotation_revision` 仅 `kind=annotation` 时必填（引用既有标记
  的范围，**不授予编辑该标记的权限**——结果始终新建 pending 标记）。

### 2.2 PT 对 body 的处理（新增校验，先于预算预占，零副作用拒绝）

1. `requested_skills` 白名单校验；`target` 形态校验（kind 枚举、bbox 有限数值
   四键，同 `_validate_ai_viewport` 口径）。
2. `target.slide_id` 必须与本次 run 的 slide 指向同一资产（不符 400
   `slide_ref_conflict`，复用 `_ai_run_slide_ref` 语义）——**跨切片草稿不得携带
   旧几何**。
3. `target.asset_revision` 与当前权威 revision 比对：不符 → 409
   `slide_revision_conflict`（同 `expected_asset_revision` 语义，不静默写）。
4. `kind=annotation`：annotation 存在、属于该切片、当前用户可见，revision 匹配
   （不符 409 `revision_conflict`）。
5. 写权前置（既有，不新增）：`/api/ai/run|continue` 已做 view+annotate 双复核
   （app.py:20910-20913）；**只读分享、只读 fork、无切片 → 本就不允许起跑写
   run**，技能不改变这一点。
6. 白名单透传：`requested_skills` / `skill_intent` / `dismissed_skills` /
   `target` 加入 run|continue 的转发 payload（app.py:20963 起）。PT 不做意图判定。

### 2.3 唯一权威意图判定位置：**HistoPilot，run 接受时（runMain/continueMain
preamble），判定结果经新机器端点写回 PT run_grants**

推荐 HP 单点判定，理由：

1. **技能是 HP 的产品能力定义**（设计 §5.3：能力定义文件——名称、描述、意图
   示例、范围规则——归属 HP）。判定规则与技能定义同仓同源，天然不漂移。
2. **HP 是唯一有模型引擎的组件**；PT 刻意不直连模型（凭据只在
   `_build_sidecar_config` 组装）。在 PT 判定需要新建第二条模型调用栈（凭据、
   超时、重试、SSRF 面），且意图规则会落在 PT、技能定义落在 HP——两套规则分居
   两仓正是要避免的漂移源。
3. HP 已在 run 接受处做权威决策（security envelope 校验、`enforceWriteRunGrant`
   pre-verify），fail-closed preamble 是现成挂点；判定在 dedup 命中之后、session
   创建/driveMain 之前。
4. 判定与执法分离：HP 判定 → PT 用 grant 列执法（写入口只认 grant），浏览器
   提示全程不参与授权。

判定输入（**只读本次 run 的真实用户指令与合法上下文**）：
- 本次 task 文本（run 的新消息 / continue 追加消息）；**不含**历史轮模型回复、
  组织图片中的文字、工具结果、其他会话。
- 显式通道：`requested_skills 含 delineate` 且 **task 文本中真实出现对应标记**
  （锚定校验：`@描绘`/`@delineate` 字面在消息里）——浏览器自报 explicit 而文本
  无锚 → 按自然语言通道处理或拒绝，不直接采信。
- 自然语言通道：仅当 `requested_skills` 显式给出、或轻量预筛（关键词存在性，
  纯本地、零模型成本）命中时，发**一次**结构化模型调用（复用现有引擎；输出
  JSON `{skill, verdict: yes|no|ambiguous}`）。否定句、引用文本（「报告里写
  “帮我圈出来”」）、查询类（「这个框是谁画的」）必须判 no；ambiguous → 不授权、
  回复中追问范围。**不每条消息都调模型；预筛不中零模型成本。**
- 尊重 `dismissed_skills`：显式删除标签 = 本条消息否定信号，verdict 强制 no。
- 判定失败/模型不可达 → fail-closed：不授权（run 照常进行，无描绘工具），
  UI 显示「描绘暂不可用」。

判定成立后的动作（HP → PT）：调用新机器端点（见 §3.2）把 grant 的
`allow_ai_drawing` 置 true；失败 → 本 run 不装配描绘工具 + UI「描绘暂不可用」
（不重试循环、不回退 session 开关）。

---

## 3. run_grants 扩展（迁移 0082）

### 3.1 DDL（migrations/0082_run_grant_drawing_skill.sql）

```sql
ALTER TABLE run_grants
    ADD COLUMN IF NOT EXISTS allow_ai_drawing BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE run_grants
    ADD COLUMN IF NOT EXISTS drawing_bound_at TIMESTAMPTZ;
ALTER TABLE run_grants
    ADD COLUMN IF NOT EXISTS drawing_intent  TEXT NOT NULL DEFAULT '';  -- explicit|natural（审计）
COMMENT ON COLUMN run_grants.allow_ai_drawing IS
    '本次 run 的描绘技能授权（技能判定成立后由 HP 经机器端点置 true；缺省 false，fail closed）';
```

幂等 ADD COLUMN；不建并行授权表；`_GRANT_SEL`（share_store_pg.py:3544）补三列。

### 3.2 发放/置位规则与新机器端点

**发放**：`create_run_grant` 不变（allow_ai_drawing 恒缺省 false）。一次逻辑 run
一次 grant 的既有语义保留：run|continue 各签发新 grant（互不继承）；断线重连走
`GET /session/:id/stream`，不新建 grant，同 run 恢复；run 终态/cancel/TTL/权限
撤销均撤 grant（迟到写入 403 `run_grant_invalid`，既有机制直接复用）。

**置位**：新端点 `POST /api/plugin/v1/run-grants/<grant_id>/skills`
（Bearer plugin JWT，scope `annotation:write`）：

- body：`{"session_id": "...", "skills": ["delineate"], "intent": "explicit|natural"}`。
- 校验：installation 匹配 grant（同 bind 端点 app.py:19332 口径）；grant 存在、
  未撤、未过期；**已绑定到同一 session**（bind 先于 skills 置位——与既有顺序
  一致：runMain preamble 中 bindRunGrantToSession :1146 在前）；skills 白名单。
- 语义：只允许 false→true，幂等（重复置 true 返回 200 + 当前值）；**不提供
  false 通道**——收回靠既有 revoke/expiry（避免「判定层撤权」与「平台撤权」
  双写竞争）。置位同时写 `drawing_bound_at` / `drawing_intent`，并审计
  `run_grant.skill_grant`。
- 旧 PT 不存在该端点 → HP 得 404 → 本 run 无描绘（§5 兼容矩阵）。

### 3.3 绑定与不继承

- grant 维度绑定既有：installation + slide_id + session（CAS bind）。技能授权
  随 grant 行走，天然绑定 run/session/slide/user（created_by_user_id）+ expiry。
- 新 run 不继承：新 grant 默认 false，须重新判定（「继续上次描绘」= 用户再发
  明确指令 → 新 run 重新判定，符合设计 §5.3「继续若是新 run，只在明确继续上次
  描绘任务时重新校验并发能力」）。
- 历史会话 `allow_ai_drawing=true`（HP session 字段/PT 镜像）对新 run **无效力**
  ——新装配只看本 run grant 列。

---

## 4. 执行链：HP 装配/复查 × PT 写入口复查

### 4.1 HP 工具装配与执行复查

- 装配（tools.ts:2206-2222）：`draw_suspicious_region` 的条件由
  `ctx.cfg.allow_ai_drawing === true`（session 权威注入，agent-runner.ts:2874）
  改为**本 run 判定结果 + grant 置位成功**的 run 级字段（RunConfig 新键
  `skill_delineate: boolean`，由 runMain preamble 判定后注入；session 持久化
  字段不再作为新 run 的装配依据）。只读 profile 恒不装配（不变）。
- execute 第一层检查改为 run 级字段；第二层（执行期复查）改为**调 PT
  `/api/plugin/v1/run-grants/verify`**（现成端点，扩展返回
  `allow_ai_drawing`）或直接依赖写端 403 软拒绝（tools.ts:2062 现状已软拒）。
  推荐：保留本地 run 级检查（廉价）+ 写端权威 403（已有），verify 扩展为可选
  诊断，不在 execute 热路径增加一跳。
- 目标一致性：run 携带冻结 `target`；draw 工具的 pending 快照 bbox 与 target
  显著偏离（无交集）时拒绝并说明（快照-attestation 链仍保证几何锚定在模型
  实际看到的图，target 是第二道范围闸）。
- 停止/终态：run 终态即撤 grant（既有）；HP 侧 fatal/cancel 语义不变。

### 4.2 PT 两条写路径改为 run grant 复查

- **plugin v1 annotate**（app.py:19096）：polygon/freehand 的检查由
  `get_ai_session_drawing_flag(session_id)`（app.py:19149-19156）改为：
  `_verify_run_grant(...)` 通过后，取 grant 行的 `allow_ai_drawing`，false →
  403 `ai_drawing_disabled`（错误码沿用，文案改为「本次对话未调用描绘技能/
  未获授权」）。矩形路径不变。
- **legacy internal annotate**（app.py:17941）：polygon/freehand 由镜像检查
  （app.py:18012-18017）改为：按 `session_id + slide_id` 查活跃 grant
  （`list_run_grants_for_session` + slide 匹配 + 未撤/未过期 +
  `allow_ai_drawing=true`），无 → 403 `ai_drawing_disabled`。**internal token
  只完成传输鉴权，不构成描绘授权**；无 Plugin Contract 凭据的 legacy 部署
  （无 grant）从此不可描绘——与「正式写入只走 Plugin Contract」的 PT-4 方向
  一致。矩形 internal 路径不变。
- 快照 attestation、几何校验、`expected_asset_revision`、effect_key 幂等、
  `source="ai"` + pending、数量上限全部沿用，技能不改写这些规则。
- 迟到写入：grant 撤销/过期/TTL（run 终态、cancel、权限钩子）→ 两路径 403。
  幂等重放：同 effect_key 重放返回既有结果（add_roi 现状），授权失效后新写入
  拒绝但重放读不破坏。

### 4.3 过渡兼容（ai_session_drawing_flags）

- 表、`/api/ai/session/<sid>/drawing` 端点、HP session 字段全部保留（过渡 +
  历史）；不删数据。
- 写入口过渡窗口（PT 新版上线起 ≤ 一个 grant TTL=1800s + 缓冲，随下次 PT
  release 移除）：polygon/freehand 闸门 = `grant.allow_ai_drawing`
  **OR** `镜像 flag == true（且 session 绑定的 grant 为旧版签发，即
  drawing_bound_at IS NULL）`。OR 两支缺省均 false，默认拒绝不变；窗口只让
  **部署切换时已在途**的旧 run 按旧语义收尾（设计 §7「旧进行中 run 按既有
  有效 grant/停止策略收尾」）。
- 窗口结束后：写入口只认 grant 列；镜像表转纯历史（新 UI 不再写）。

---

## 5. 能力/协议版本协商

- **版本标识**：HP manifest.json `pluginContractVersion` 1.0.0 → **1.1.0**
  （新增 skills 语义：run body 键、/skills 端点、run 级装配）。PT 侧按既有
  manifest 校验/安装链感知（`tests/test_plugin_manifest.py` 口径）。
- **HP 感知 PT 支持**：功能探测——HP 调 `POST .../run-grants/<id>/skills`，
  404（旧 PT 无端点）= 不支持 → 本 run 无描绘 + UI「描绘 · 暂不可用」；不因
  任何探测失败回退到读 session 开关（**绝不静默永久开启**）。
- **PT 感知 HP 支持**：旧 HP 忽略 body 新键（白名单透传对旧 HP 是未知字段，
  现有 server 边界对未知 body 键宽容）→ 无判定 → grant 列恒 false → 闸门关。
  PT 不需要主动探测；写入口 fail-closed 即正确行为。
- **兼容矩阵**：

| 组合 | 行为 |
| --- | --- |
| 新 PT + 新 HP | 完整技能链：@ / 自然语言 → 判定 → grant 置位 → 装配 → 写入口放行 |
| 旧 HP + 新 PT | HP 不判定不置位 → grant 列 false → 描绘不可用（过渡窗口内旧 session 开关仍收尾在途 run）；矩形标注照常 |
| 新 HP + 旧 PT | HP 收不到技能键（旧 PT 不透传）或 /skills 404 → 不装配、不判定回退；旧 PT 镜像闸门仍在，但新 HP 不再走 /drawing，不产生新 true |
| 旧 PT + 旧 HP | 现状不变（checkbox + 镜像开关） |
| 回滚 HP（新→旧） | 旧 HP 走镜像开关；过渡窗口内 OR 逻辑使其可用，窗口后仅不可用——任何组合都**不会**永久开启 |
| 回滚 PT（新→旧） | 旧 PT 无 grant 列逻辑 → 回到镜像闸门；新 HP 判定端点 404 → 不装配；行为 = 旧 PT + 新 HP 行 |

- **部署顺序**（设计 §7 兼容顺序的具体化）：
  1. PT 发布：0082 迁移 + /skills 端点 + 写入口 OR 过渡闸门 + run|continue
     透传新键（对旧 HP 全部 no-op，可先上线）。
  2. HP 发布 0.4.0：技能定义 + 判定 + run 级装配 + 新 UI（移除 checkbox）。
  3. 观察一个 TTL 窗口后，PT 下一版移除镜像 OR 支路（保留表与端点）。
  回滚按上表；任何一步回滚不需要数据修复（grant 列缺省 false 安全）。

---

## 6. 来源标注

- 技能创建的结果：两条写路径的 provenance 增加 `skill: "delineate"` 与
  `skill_intent: "explicit"|"natural"`（additive，旧端忽略；随 effect_key 幂等
  一致）。UI 标记详情「来源：AI · 本次描绘」，人工修改保留原始来源快照与
  source=ai（设计 §5.4）。
- AI 矩形 `create_annotation`：**保持现状**（无 skill 字段、无新闸门）。是否
  禁止普通 AI 自动落矩形属后续产品决定，本契约不引入。

---

## 7. HP UI 改动清单（随 HP release，不能只发 PT CSS）

移除：
- checkbox `#hp-ai-drawing-toggle` 及 switch（sessions.js:585-603）、
  `S.aiDrawingFlags`/`S.aiDrawingEpochs`/`S.aiDrawingDraft`、
  `POST /api/ai/session/:id/drawing` 调用（sessions.js:682）、
  mirror.stale 对照提示（sessions.js:53-59）、main.js 首发
  `allow_ai_drawing`（main.js:286）及草稿 draw 字段。

新增（UI→后端字段即 §2.1）：
- `@` 候选菜单：输入 `@` 弹出「描绘——圈出指定组织区域，生成待复核标记」；
  选中变可删除小标签；直接发送 `@描绘` 文本也合法（锚定在文本里）。
- 发送前命中明确意图显示「描绘 · 本次」小标签，可删除；删除 → 本条消息
  `dismissed_skills=["delineate"]`（显式关闭记录，判定层强制 no）。
- 状态文案：技能判定成立且装配后显示「正在描绘……」；完成显示
  「已生成 N 个待复核标记」+ 定位入口（复用 `annotation_created` 事件的
  annotation_id/geometry，tools.ts:2084）。
- 不可用态：兼容矩阵任一不支持组合 → 候选菜单条目标注「暂不可用」并说明，
  不出现永久开启路径。
- 模糊指代（多个框时「圈出这个」）：判定 ambiguous → 对话内追问目标范围，
  不弹「是否允许描绘」。

---

## 8. 测试迁移与保护保留

原则（设计 §8）：旧 toggle 测试删改时，**默认拒绝、迟到写入、身份隔离**三类
断言必须迁移到 run 范围测试，不得直接删除。

| 旧保护（现状测试） | 迁移目标（新测试） |
| --- | --- |
| 镜像无行/false fail-closed（gate :131/:144） | grant 列缺省 false → 两写路径 403 `ai_drawing_disabled`；/skills 未置位不可写 |
| 运行中关闭迟到写入（gate :169；HP ai-drawing :185） | run 终态（SSE 结束）撤 grant → 迟到 polygon 403；cancel → 403；TTL 到期 → 403；execute 期 PT 403 → 软拒绝不落库 |
| 仍要求 run grant（gate :218） | plugin v1 不变；internal polygon 无活跃 grant → 403（internal token 不绕过） |
| 身份隔离（grant session CAS、creator 复查、attestation sid 归属 gate :788） | /skills 端点：installation 不符 403；未 bind session 403；跨 session attestation 拒绝（沿用）；grant.created_by_user_id 复查沿用 |
| 首发创建参数/开关 UI（ui-drawing-toggle 全部） | 替换为 composer 测试：@ 候选、标签删除 → dismissed_skills、状态文案、暂不可用态；首发 body 断言改为 requested_skills/target/skill_intent 形态 |
| HP 装配/纵深（ai-drawing :145-:200） | 装配键改为 run 级判定字段；session 旧 true 不再装配新 run（反例：历史会话开关不继承） |
| 跨仓 contract（ai-drawing-contract.integration） | 扩展：真实 PT 起 Flask+pg，/skills 置位 → 真实 HP 客户端 polygon 落库 + provenance.skill；未置位 403；判定模块以注入桩替代模型（不调付费 AI），断言 wire 契约与闸门 |

意图判定单测（HP，纯函数 + 桩模型）：设计 §5.2 全表正反例——@显式、自然语言
肯定、否定句、引用文本、查询类、下一轮不继承、删除标签、无权限/只读、跨片
target、过期 asset_revision 409、模糊追问。

---

## 9. 实施步骤切分与每步测试

| 步骤 | 仓 | 内容 | 测试 |
| --- | --- | --- | --- |
| E1a | PT | 0082 迁移 + `_GRANT_SEL` 三列 + store 读写（get/create 带 allow_ai_drawing） | `python -m pytest tests/test_plugin_api.py tests/test_ai_integrity.py -q`（迁移由 conftest pgserver 全量跑） |
| E1b | PT | `/skills` 机器端点（校验链 + 幂等 + 审计） | `tests/test_plugin_api.py` 新增：installation 不符/未 bind/过期/撤销/白名单/幂等重放 |
| E1c | PT | run\|continue 校验并透传 requested_skills/skill_intent/dismissed_skills/target（含 target.asset_revision 409、annotation kind 校验） | `tests/test_ai_proxy.py` + `tests/test_ai_fixes.py` 扩展；`tests/test_annotation_bounds.py` 回归 |
| E1d | PT | 两写路径闸门切 grant 列 + internal 按 session 查活跃 grant + 过渡 OR 支路 | `tests/test_ai_drawing_gate.py` 改造迁移（§8 表）；`tests/test_ai_drawing_geom.py`/`tests/test_rect_annotations.py` 回归 |
| E2a | HP | 技能定义文件（delineate：描述/意图示例/范围规则）+ 判定模块（锚定 + 预筛 + 一次结构化调用 + dismissed 强制 no + fail-closed） | `npx vitest run --project unit test/skill-intent.test.ts`（§5.2 表全例，桩模型） |
| E2b | HP | runMain/continueMain preamble 判定 → /skills 置位 → RunConfig.skill_delineate 注入；装配与 execute 改 run 级；移除 session 字段对新 run 的装配效力 | `npx vitest run --project unit test/ai-drawing.test.ts test/agent-runner.test.ts`（改造）+ `test/continue-session-binding.test.ts` 回归 |
| E2c | HP | provenance.skill/skill_intent 下发（additive） | `test/ai-drawing-provenance.test.ts` 扩展 |
| E3 | HP | UI：移除 checkbox 全链 + @ 候选 + 标签 + 状态文案 + 暂不可用 | 替换 `test/ui-drawing-toggle.test.ts` → `test/ui-skill-composer.test.ts`；`tests/js/ai-panel-dock.test.ts`（PT 侧）不受影响 |
| F1 | 跨仓 | contract 集成扩展（§8 末行）；manifest 1.1.0 | PT 仓：`PATHTOGETHER_REPO=<vi-skill-wt> npx vitest run --project contract`（在 hp-skill-wt 内，指向 PT checkout）；PT 侧 `python -m pytest tests/test_ai_drawing_gate.py tests/test_plugin_manifest.py -q` |
| F2 | 两仓 | 发布：PT 先（E1 全量）→ HP 0.4.0（E2/E3）→ TTL 窗口后 PT 移除 OR 支路 | 全量：PT `python -m pytest tests/ -q`（或按门禁子集）；HP `npm test` + `npm run test:contract`；真实 bundle 验收（设计 §8「真实插件」行，非 mock 面板） |

PT pytest 子集（每步最小回归）：
`tests/test_ai_drawing_gate.py tests/test_ai_drawing_geom.py tests/test_plugin_api.py
tests/test_ai_proxy.py tests/test_ai_session_owner.py tests/test_annotation_isolation.py
tests/test_rect_annotations.py tests/test_plugin_manifest.py -q`（conftest 自动起
pgserver 内嵌 PG）。

HP 命令：`npm test`（= `vitest run --project '!contract'`）；
契约 `PATHTOGETHER_REPO=/path/to/vi-skill-wt npm run test:contract`。

---

## 10. 待主代理拍板的决策点

1. **判定位置**（§2.3）：推荐 HP（理由如上）。备选 PT 判定（原子发放但需在 PT
   新建模型调用栈 + 规则与技能定义分居两仓）。
2. **internal 通道**（§4.2）：推荐按 session 查活跃 grant（legacy 无 grant 部署
   不再可描绘）。备选：要求 internal 也带 X-Run-Grant 头（wire 改动更大）。
3. **过渡 OR 窗口**（§4.3）：推荐有窗口（一个 TTL）保在途 run 收尾。备选：
   切换即只认 grant 列（在途描绘 run 立即中断，更干净但影响少量在线用户）。
4. **execute 期复查**（§4.1）：推荐本地 run 级字段 + 写端 403 软拒绝（零新增
   网络跳）。备选：execute 前调 verify 端点（多一跳、更早反馈）。
5. **/skills 端点形态**（§3.2）：推荐独立 POST 端点、只 false→true。备选：
   扩展 bind body 一次带 skills（少一跳，但把「绑定」与「授权」耦合，回滚语义
   复杂）。
