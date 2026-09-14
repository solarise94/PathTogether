# Agent 一次执行规格：账户状态、导入与项目 UI

版本：2026-09-14。状态：**代码已实施（2026-09-15）**。W1–W6 已落在 `wip/ser8-dev`；真实百度 L01–L04 仍缺外部条件。本文保留为执行规格，完成情况见 [验收报告](acceptance-account-import-project-ui-2026-09-14.md) §0。

工作目录：`/home/solarise/ZCodeProject/histopilot-suite/PathTogether`。

问题证据：[原始 Review](review-upgrade-account-import-project-ui-2026-09-14.md)。原 Review 的建议在本文收敛为执行要求；按本规格完成所有工作包，不在每个工作包结束后停下来询问是否继续。

## 0. 可直接交给 Agent 的任务

> 实施本文 W1–W6，补齐回归测试并运行第 8 节验收。先检查当前工作区及适用 AGENTS.md，保留已有修改和未跟踪文件；使用当前工作区代码，不重置到旧提交。完成账户状态修复、格式申请 PostgreSQL 持久化、项目创建交互、持久转换进度，以及真实百度适配层和完整导入 UI/API/worker。不得把 disabled 按钮、mock-only 后端、TODO 或仅有设计文档当作功能完成。测试失败先定位并修复，交付代码、迁移工具、测试、验收报告和运行说明。常规实现决策按本文执行，无需逐项确认；外部依赖不足时继续完成独立工作，最终如实列出未通过的门槛。

本次“完成”指代码实施与可重复验收；不要求发布生产、修改真实用户额度或为测试发送真实邮件。不得执行旧 cutover 脚本修复待激活用户。真实百度转存/下载/删除的冒烟只使用专门授权的测试分享和连接器账号，未具备时记录外部验收未执行，不操作历史文档中出现的真实分享。

## 1. 固定范围与实现决策

| 项目 | 本次决定 |
|---|---|
| 基线 | 原 Review 基线为 `4d52c25` + dirty working tree；开始时重新记录 HEAD、状态和已有 diff，不丢弃既有 KFB/KFBF、格式申请等实现 |
| 前端 | 延续原生 JS、现有样式、i18n 和 admin iframe/bridge；不引入新 SPA 框架 |
| 数据 | 新增业务权威状态使用 PostgreSQL；JSONL 只作为旧数据导入源 |
| 导入面板 | 桌面抽屉，小屏可用全屏抽屉；“本地文件 / 百度分享”两个页签；“申请新格式支持”为面板内次级入口 |
| 项目创建 | 独立对话框；明确 empty/selection 模式；前端提交锁，并补按用户作用域的服务端幂等键 |
| 格式 | 按当前注册表及真实内容探测；包含已实现的 KFBF 路径，不沿用旧百度调研中“.kfbf 必须不可选”的过期结论；目录/channel.json 不当作切片 |
| 任务恢复 | 服务端按当前身份列出任务；localStorage 只能作缓存，不能成为刷新恢复的唯一来源 |
| 百度 | 实现受限适配层调用经核验的现有连接器；生产不使用 fake，不自行增加网页 Cookie 抓取路线；未知 CLI 参数不得猜造 |
| 开关 | 枚举与正式导入独立启用；默认关闭真实外部动作；关闭后仍能看到已有任务及清晰原因 |
| 不做 | 生产发布、实际账号补款、重新设计计费授权、重写 KFB 转换器、改造 HistoPilot、COS 新架构或所有外部格式的转换实现 |

若后续代码已有等效实现，复用并补测试，不再建第二套。API 路径和状态词按下文执行；确有现存接口冲突时允许统一调整，但必须同步前后端、测试和契约表，在验收报告解释。

## 2. 开始前检查与执行顺序

1. 保存 `git status --short`、HEAD 和既有 diff 清单到本地验收记录；只读检查适用 AGENTS.md，不覆盖用户文件。迁移编号取当前下一个空号，不固定抢占 `0049`。
2. 阅读入口：`app.py`、`spend_store.py`、`registration_store.py`、`format_request_store.py`、`slide_format_registry.py`、`conversion_store.py`、`conversion_worker.py`、`upload_task_store.py`、`static/app.js`、`templates/_app_shell.html`、管理插件及 `static/admin-host.js`。
3. 先运行第 8.1 节已有测试基线，记录预存失败；不要以已有的 51 项通过替代本次基线。测试数据库使用 `tests/conftest.py` 的临时 PostgreSQL。
4. 按 W1 → W2 → W3 → W4 → W5 → W6 推进，可在同一实现周期内迭代，但所有工作包都属于本次交付。遇到外部连接器问题先保留证据，再完成其他工作。
5. 每个已复现 bug 先落可失败的回归测试，再修复；新增流程按行为验收，不写只断言源码字符串存在的替代测试。
6. 最后执行统一验收、检查 diff、填写报告。本文起草时未执行业务修改；coding agent 接到实施任务后应完成上述全部工作，不停在再次输出计划。

## 3. W1：账户激活状态与额度展示

修改位置：`app.py` 管理列表投影、管理插件 `ui/main.js/style.css`；必要时抽取可复用的展示投影函数。**不改变**底层总额度缺行拒绝逻辑。

管理列表 `spend` 新增 `status`，约定：

| 条件 | status | 展示和字段要求 |
|---|---|---|
| 普通用户合法 `pending_activation/email_pending` 且无额度 | `not_provisioned` | “待激活，激活后发放额度”/“待验证”；不带 missing error，不伪造 total/window/金额 0 |
| active 普通用户有额度 | `available` | 继续返回 total，金额仍为十进制字符串 |
| active 普通用户缺额度 | `unavailable` | 保留 `spend_total_allowance_missing`；显示“额度记录异常，请管理员检查”，代码在技术详情 |
| owner 有有效窗口 | `available` | 保持 window 形态及既有金额口径 |
| 读取失败或矛盾数据 | `unavailable` | 明确错误，不当作正常待激活；例如 pending 却已有 allowance，应报告状态不一致，不删除或补写数据 |

必须使用服务端权威激活状态，不由邮箱是否存在或 `ai_access` 推断。合法旧用户缺省激活状态按现有用户模型的规范化规则处理；未知状态不得默认获得额度。禁用状态独立于激活与额度，禁用用户已有金额不能被清零。

桌面和移动端都显示激活标签；“启用”改成明确的账户开关语义或和激活标签并列，避免误认为已开通 AI。总额度缺行的底层异常继续存在；账户余额入口保持现有激活/预览守卫，不能为了返回新状态放宽 pending 访问。若受限身份不能访问余额 API，就验证其原有拒绝行为，而非强行改成 200。

在已存在 owner 窗口等前提下验证管理列表读取无新增 allowance、额度审计或金额变动；不得为满足“只读”测试顺便改掉现有 owner 窗口语义。

## 4. W2：格式申请持久化与用户回执

### 4.1 数据与并发

新增 PG 迁移，至少包含申请与邮件作业（可分两表）：申请 ID、owner、格式、说明、联系邮箱、样本内部引用/大小/hash、业务状态、版本、创建/更新时间；邮件状态、attempt、lease owner/token/expiry、last error。样本路径和内部错误不进入普通用户响应。

业务状态固定 `submitted → reviewing → supported | declined`；拒绝/支持允许管理员附说明。邮件状态与业务状态分开：`queued / sending / sent / failed / uncertain`。管理员状态写入使用 `expected_version` CAS，冲突 409，审计记录同事务。

同用户滚动 24 小时次数检查与新申请插入原子化，保留现有每日限额配置；不能 `count_since` 和提交分开锁定。样本继续流式保存，保留大小/hash校验，数据库提交失败清理本次文件；增加中断后的孤立样本清理策略。样本单文件上限沿用 `FORMAT_REQUEST_MAX_SAMPLE_BYTES`，默认 64 MiB，前端使用服务端配置，不硬编码。

邮件领取使用 `FOR UPDATE SKIP LOCKED` 或等效原子领取，网络发送期间不持长数据库事务；完成回写校验 lease token。取消进程内并行自动 drain 或让所有 drain 共用领取协议。`sent/uncertain` 不自动再次发送。

**崩溃语义必须明确：**发送调用已开始但结果未持久化的过期 `sending` 视为 `uncertain`，不能仅凭租约过期重发。只有能证明未发送的作业才自动恢复为可重试状态；不宣称外部 SMTP exactly-once。fake sender 记录跨进程可观察的尝试，验证该边界。

### 4.2 API、管理端和迁移

保留 `POST /api/format-requests` 的 202 和 `request_id`；增加当前用户 `GET /api/format-requests` 与 `GET /api/format-requests/{id}`，分页限制 1–100，默认 50，返回不透明 next_cursor。越权详情统一 404。

管理端增加 owner-only 列表、详情和状态 PATCH；通过现有 admin bridge 方法/权限映射、manifest/bootstrap/source policy 接入，不能让 sandbox iframe 直接绕过宿主 fetch。样本如需下载，使用鉴权下载接口和 attachment 响应；不能返回服务器路径。

新增 `scripts/migrate_format_requests.py --mode preflight|apply --source <JSONL>`。preflight 无业务写入，报告总数、唯一 ID、坏行、冲突、状态与缺失样本；apply 在维护窗口执行，先备份源文件，按 ID 幂等导入，保留 sent/uncertain，不自动发邮件。坏行或同 ID 不同内容整体拒绝，不静默跳过；缺失样本保留申请并显式标识。重复 apply 无新增或状态回退。运行期不再双写 JSONL。

成功回执显示申请编号、业务状态、可打开的详情；刷新后可查询。未配置邮件也要登记成功，不显示“已发送”。输入有 label、字段错误就地显示，413/429/失败保留用户草稿。

## 5. W3/W4：项目创建、格式能力与转换任务

### W3：项目创建

替换内联新建表单为对话框，name 上限 60、note 上限 200，前后端一致；实际服务端已有更严格合同则沿用并同步 UI。name 非空，非法字段类型返回 400，不能把非 list 的 slides 静默吞为空集合。

普通“新建项目”总是初始化 empty；“含选中”入口将当前选择快照传入 selection，不再依赖隐式全局 fallback。展示实际 N 张，允许删除选择。取消/Esc/重新普通新建清空本次草稿，成功定位服务端返回的 pid；失败保留草稿和选择。

点击、Enter 共用提交锁。开始发送后禁用重复确认，允许关闭视图时也不得产生新的同内容提交。新增可选 `Idempotency-Key` 请求头供旧客户端兼容；新 UI 每份草稿创建键并在网络失败重试时复用，编辑请求内容后生成新键。用户+键唯一，存 payload digest 和创建结果；同键同载荷返回原 pid，同键异载荷 409，跨用户相同键互不影响。项目与幂等结果原子提交，不能出现项目成功但幂等结果丢失。保持同名不同键可创建。

目标切片和目标项目遵守现有所有权/可写权限，不把统一导入面板当作权限放宽入口。

### W4：统一导入与后台任务

增加 `GET /api/slide-formats` 输出清理过的结构化能力：展示名、extensions、capability、canonical 格式、bundle 要求、样本上限及相关限制；不直接把旧 notes 原样当产品文案。`.ome.tif/.ome.tiff` 明确可见，KFB/KFBF 为需转换，MRXS 为完整包。

侧栏两个主按钮“导入切片”“新建项目”；导入抽屉提供本地/百度页签、目标位置（未归类/已有项目/新项目）、次级格式申请入口和任务列表。本地上传仍走原 legacy/V2 通道和原配额收口；任务先持久记录目标项目，文件 ready 后才幂等关联。目标项目被删/权限撤销时显示关联失败，保留可访问的成功产物，不静默移到其他项目。

保留 `GET /api/conversions/{id}`；新增按当前身份隔离的 `GET /api/conversions`，支持 `open/recent` 查询组、分页（默认 50，上限 100）、稳定排序；recent 默认最近 7 天。普通用户及 owner 工作区均只看自己的任务，管理员库存查询另走已有管理权限。新增安全的转换重试接口，只有 failed/cancelled 且源仍可用时才可重排，重试不重复扣配额或创建切片。

后台任务状态为成功/失败权威；观察超过 15 分钟仍显示后台处理中，网络故障显示进度暂不可取；401/403 停止轮询并提示权限，404 显示任务不存在，暂时故障有退避。刷新后通过服务端恢复。成功不强行切换用户正在看的其他切片；提供“打开切片”操作。已入库但关联/清理失败分项显示，不把已成功入库改成上传失败。

## 6. W5：百度适配器、枚举和导入闭环

### 6.1 适配层与配置

实现生产适配器以及仅测试可注入的 fake。生产适配器至少实现：能力检查、分享内分页列表、按选中项转存、异步转存查询、下载到任务暂存路径、查询本批副本、按本批归属清理。优先核验本机连接器的 version/help 与实际 JSON 输出；将经验证版本、调用参数和脱敏输出 fixture 纳入契约测试。缺少支持能力时返回明确 unavailable，不伪造成功。

固定配置名：`BAIDU_ENUMERATION_ENABLED`、`BAIDU_IMPORT_ENABLED`、`BAIDU_CONNECTOR_BIN`、`BAIDU_CONNECTOR_HOME`、`BAIDU_IMPORT_WORKER`。真实开关默认 false，二进制/认证目录仅由部署配置给定；日志不记录完整分享链接、提取码、令牌或未经脱敏的 CLI stdout。现成账号认证文件不复制入库。服务端需要保存的分享秘密使用现有加密机制或独立环境密钥；缺密钥则连接器不可用，不明文落入任务表。

子进程使用参数数组、固定允许的子命令、超时与退出码检查；路径/fs_id 等来自当前分享列表，不由用户提供任意下载地址。对 CLI 输出做 schema 校验；未知结构、缺页、重复游标停止并给明确失败，不能把解析异常视为“空分享”。

### 6.2 API 合同

通用：身份与激活守卫沿用上传权限，写操作 CSRF；请求体禁止客户端伪造 owner/path/下载 URL；本人详情以外 404。所有分页 cursor 不透明，limit 默认 50、上限 100。失败 `{code,error}`；400 输入，403 权限，404 不存在/非本人，409 状态或幂等冲突，429 配额/频控，503 连接器不可用。

| 方法与路径 | 输入/响应 |
|---|---|
| `GET /api/remote-imports/baidu/capabilities` | `enumeration_available, import_available, reason_code, limits`；不返回认证详情 |
| `POST /api/remote-imports/baidu/enumerations` | `{share_text, extraction_code?}`；202 `{id,state}` |
| `GET /api/remote-imports/baidu/enumerations/{id}` | `id,state,complete,scanned_count,candidate_count,error_code,expires_at` |
| `GET /api/remote-imports/baidu/enumerations/{id}/candidates` | 条目 ID、name、relative_path、size_bytes 十进制字符串、format、capability、selectable、reason_code；items/next_cursor |
| `POST /api/remote-imports/baidu/imports` | `{enumeration_id,candidate_ids,target_project_id?}` + `Idempotency-Key`；202 `{id,state}`，重复返回原批次 |
| `GET /api/remote-imports/baidu/imports` | 本人批次列表、分页及 open/recent |
| `GET /api/remote-imports/baidu/imports/{id}` | 逐文件进度、阶段、终态、可恢复错误、项目关联状态、清理状态；不返回内部秘密 |
| `POST /api/remote-imports/baidu/imports/{id}/cancel` | 202 取消请求；已终结返回当前状态，不删除成功产物 |
| `POST /api/remote-imports/baidu/imports/{id}/retry` | 失败条目 ID + 幂等键；只能重试可恢复条目，成功项不重跑 |

输入解析仅接受支持的百度分享域名/格式（初始至少规范 `https://pan.baidu.com/s/<id>`）；从文本和 query 解析提取码，冲突 400 并提示修正。不跳转访问任意分享文本 URL。敏感输入字段不上报分析日志。

### 6.3 状态、持久化和副作用

枚举状态：`queued → enumerating → ready | failed | expired`，仅 ready 且 complete=true、未过期可以导入。默认有效期 24 小时；最大深度 32、最多 10,000 条目、枚举时限 10 分钟，均可配置；达上限明确 incomplete/limit 错误，不能称完整。保留不可选条目及原因。枚举全过程的 transfer/download/delete 调用次数必须是 0。

批次有独立 owner、枚举 ID、幂等键/digest、目标、lease；条目保存源标识、目标暂存路径、配额引用、转换任务引用和收口凭证。条目阶段：`queued → transferring → downloading → validating → converting(可选) → ingesting → ready`，以及 `failed/cancelled`。批次聚合为 `queued/running/succeeded/partial_failed/failed/cancelled`。清理独立 `not_needed/pending/succeeded/failed`，不反向破坏 ready。

只转存所选文件到 `/apps/bdpan/<batch-id>/` 或经核验连接器支持的等效任务专用目录。不得使用自动整份转存的分享下载命令。MRXS 缺包不可选；无法保证完整目录包时显示当前不支持从百度导入该 bundle，保留本地完整包入口，不假装支持。

导入前服务端核验枚举 owner、候选、有效期、可选性、目标可写性，事务化预占配额，再派发。下载写受限暂存区，计算本地 SHA-256 并做真实格式验证；复用已有上传/转换收口原语，必要时抽取服务层，不能靠伪造 HTTP 上传绕过状态机。入库、项目关联与配额消费有幂等凭证；名称冲突沿用现有隔离规则。下载未知/漂移大小及时按现有策略扩容预占或失败，不能无界写盘。

每个外部阶段保存恢复信息。进程在转存成功后、下载结束后、入库后崩溃，恢复都先对账，不能再次无条件转存/扣额度/建切片。支持连接器的断点续传时验证 offset 与来源稳定性；不支持时允许有界重下到新暂存文件，清理旧 partial，不能宣称支持续传。

取消释放未消费预占、停止未开始条目，对正在执行的外部操作先确认结果再收口；成功条目保持 ready，失败/取消不自动删除已导入切片。只有“本批持有 + 本地校验及入库成功”的远端副本能清理；清理失败可重试，失败下载副本不在成功清理范围。开关关闭停止接收新任务，已接受任务继续收口/展示，worker 停用则明确排队暂停。

## 7. W6：界面验收与集成

百度页签从主导入按钮两次操作内可达；未配置显示具体可行动原因，不能呈现无响应按钮。配置后：粘贴 → 读取列表 → 搜索/筛选/分页选择 → 目标项目 → 确认选中 N 项/总大小 → 开始导入 → 持久逐文件状态。先读列表，后显式确认导入。

选择跨页保留，数量和总字节只算选中项；不完整枚举禁用导入并解释原因。格式可选性由服务端候选决定；前端不能改 extension 就绕过。部分失败显示成功/失败数量，重试只针对失败项。

至少在 Chromium 1440×900 和 390×844 验证中英文：无页面水平溢出，主操作在可滚动区域内可达，label 与输入关联，焦点进入面板、Tab 不落到背景、Esc 关闭、焦点返回触发按钮。加载/空/错误/禁用/处理中/部分成功均有可见文案。管理插件新增页面按既有 bridge/CSP 流程工作，不能在新功能中放宽 CSP。

## 8. 测试矩阵：必须达到的通过条件

以下每行是独立验收 ID。将 ID 放入测试名称或报告映射；参数化允许，但每个条件都必须实际执行。新增关键用例不得 skip/xfail，单元 mock 不替代标明的真实 PG/真实浏览器层。

### 8.1 先跑已有基线

在工作目录执行；缺本地依赖先按仓库现有依赖文件安装到 `.venv`/node_modules，不连接生产数据库：

```bash
.venv/bin/python -m pytest tests/test_admin_batch_d.py tests/test_email_verify_activation.py tests/test_account_balance.py tests/test_format_request.py tests/test_slide_format_registry.py tests/test_kfb_upload.py tests/test_kfbf_upload.py tests/test_upload_v2.py tests/test_upload_accounting_recovery.py -q
npm run test:js
```

记录各自 exit code、pass/fail/skip 数和测试环境。新增失败必须修复；发现范围外预存失败要保留最小复现和与本次无关的依据，整体结果不能写“全绿”。

### 8.2 后端与 worker 必测

| ID | 层/场景 | 必须断言 |
|---|---|---|
| A01 | PG+管理 API：真实邮箱验证建 pending | not_provisioned，无 missing error、无伪造金额；额度行仍 0 |
| A02 | PG：active 缺行/有行、owner、禁用 | 缺行仍 unavailable；有效 total/window 精确不变；禁用不清零 |
| A03 | PG：读取/激活 | 预建 owner 窗口后重复 GET 无新增额度审计；邀请码激活一次，重放不再发放 |
| A04 | API：pending/preview、矛盾数据 | 原权限拒绝仍有效；pending 有额度不伪装成正常 not_provisioned |
| F01 | PG：两个独立进程各提交 10 条（不同用户） | 20 次成功对应 20 个唯一 ID、20 条持久记录；barrier 控制重叠，不能用单线程替代 |
| F02 | PG：同用户限额 L-1 后并发两次 | 恰好 1 次成功、1 次 429，最终 L 条；不能靠偶然调度通过 |
| F03 | PG+fake sender：两个 worker 争抢 | 每个作业最多一个有效领取，成功作业只调用 sender 一次，旧 lease 回写被拒绝 |
| F04 | PG：发送前/调用开始后崩溃 | 可证明未发出才重试；不确定发送进入 uncertain，多轮 drain 不再发；sent 保持 sent |
| F05 | PG：提交/排水并发、无发送通道 | 新记录不丢，状态不回退；queued 仍可由用户查询 |
| F06 | 迁移：preflight、两次 apply、坏行/冲突/缺样本 | preflight 无写，apply 幂等，sent/uncertain 保留，坏行拒绝且不部分导入，缺样本可见 |
| F07 | API：样本超限/事务失败/越权/CAS | 413 并清理本次样本；事务失败无孤立新增文件；越权404，CAS冲突409，内部路径不泄露 |
| P01 | API+PG：同用户同键并发两次 | 同 pid，项目/关联仅一份；同键异载荷409，跨用户键独立 |
| P02 | API+PG：无效输入/越权切片/同名不同键 | 非法类型400，越权拒绝；合法同名不同键不误去重 |
| C01 | API+PG：转换列表/重试 | 本人隔离、分页无丢重；源可用的 failed 重试幂等，ready 不重复消费配额 |
| C02 | PG：本地导入目标关联 | native及转换完成后各关联一次；目标删掉/权限撤销明确关联失败，产物保留 |
| B01 | parser/适配器：分享文本、码冲突、恶意参数 | 合法规范化；冲突/非允许域拒绝；参数不经 shell 执行 |
| B02 | adapter contract：固定 CLI 输出 fixture | 分页/目录/file ID/大整数字节解析正确；超时、非零退出、未知 JSON 明确失败 |
| B03 | PG+fake adapter：嵌套目录/分页/重复游标/超限 | 递归只在分享内，稳定候选去重；不完整禁导入；转存/下载/删除调用全部为0 |
| B04 | PG+API：混合格式/候选/有效期/权限 | KFBF按当前能力可选；目录/channel.json/缺包不可选；空选400，过期409，越权404 |
| B05 | PG：创建导入的并发幂等与配额不足 | 同键同批、预占一次；不足零外部副作用；同键异选择409 |
| B06 | PG+真实收口+fake传输：native/KFB/KFBF | fixture真实探测/转换，最终产物可读；仅选中项转存，配额正确，项目关联一次 |
| B07 | PG+worker 故障注入：三处崩溃 | 转存后/下载后/入库后重新启动 worker，均不重复入库/扣费；持久任务可恢复 |
| B08 | PG+worker：断流/来源变化/名称冲突 | partial不当成功；来源漂移拒绝或重核验；不覆盖既有文件、不泄露他人文件名 |
| B09 | PG+worker：部分失败/重试/取消 | 成功项不重跑；取消仅释放未消费预占；无负配额、无重复关联 |
| B10 | adapter+PG：清理与开关 | 只清理本批成功项；非本批路径被拒；清理失败不回滚ready；关开关仍可查已有任务 |
| B11 | API：列表详情与秘密 | owner/user各自隔离；无提取码/令牌/内部路径出线；错误日志也脱敏 |

### 8.3 JS 与真实 Chromium 必测

| ID | 场景 | 必须断言 |
|---|---|---|
| U01 | 管理端桌面/手机 pending 与 active 缺行 | pending 标签真正 visible（computed style），不显示 missing；active 异常仍可识别 |
| U02 | 选A → 含选中新建 → 取消 → 普通新建 | 实际 POST slides=[]，数据库项目无A；空数组不回退旧选择 |
| U03 | 慢响应双击+Enter、响应丢失后重试 | pending只发一次；重试复用键并获得原pid；失败保留草稿，成功定位项目 |
| U04 | 选择列表/关闭/权限错误 | N和内容一致，可移除；Esc清理并归还焦点；403错误就地显示 |
| U05 | 格式说明和申请 | KFB/KFBF/OME-TIFF/MRXS规则正确；label、上限、413/429草稿保留；成功ID刷新可查 |
| U06 | 转换观察时间推进至16分钟、断网、刷新 | fake timer不真实等待16分钟；不能显示失败；恢复网络继续，刷新由服务端重建任务 |
| U07 | 转换401/403/404、后台failed/ready | 按状态处理并停止无意义轮询；ready不抢占正在查看的其他切片 |
| U08 | 百度未配置/可枚举不可导入 | 页签可见、原因明确；正确禁用动作，无空点击或假成功 |
| U09 | 百度真实UI+Flask+PG+fake adapter 全流程 | 粘贴→列表→跨页选中→确认→worker→ready；断言非选中项无转存，刷新后状态保持 |
| U10 | 百度部分失败/超限/过期/重试/取消 | 统计与服务端一致，超限/过期无法导入，失败项重试不影响成功项 |
| U11 | 两种尺寸×中英文，管理和主工作区 | 无水平溢出；主操作可达；Tab/Esc/焦点恢复有效；无未预期console/pageerror/CSP/失败资源 |

UI happy path至少 U02/U03/U05/U09 使用真实 Flask API与临时PG，不能全部 `page.route` mock；允许只在外部百度边界注入 fake。错误状态可用精确的单请求故障注入；每个允许的浏览器错误必须在该用例断言中解释，不设通配忽略。

### 8.4 测试文件、命令与统一入口

复用已有测试文件，新增以下文件作为明确交付；若已存在同功能文件，可合并但必须同步下面命令：

```text
tests/test_format_request_concurrency.py
tests/test_format_request_migration.py
tests/test_project_creation_upgrade.py
tests/test_conversion_task_api.py
tests/test_baidu_adapter.py
tests/test_baidu_imports.py
tests/test_baidu_import_recovery.py
tests/js/project-import-upgrade.test.ts
tests/e2e/import-project-upgrade.spec.ts
```

将 A/F/P/C/B 测试落入上述或现有相关文件；U01 可扩充 `admin-workbench.spec.ts`，U02–U11 落入新 E2E。扩充 `tests/conftest.py` 新表清理和 `tests/e2e/e2e_server.py` 的临时身份/fixture/受控测试 worker；fake注入只能用于测试进程，不暴露公网测试管理接口。

新增 `scripts/verify_import_project_upgrade.sh`，使用 `set -euo pipefail`、切换仓库根、将 `.venv/bin` 前置 PATH，创建独立验收产物目录；按下列逻辑运行并保留 JUnit、Playwright HTML/trace。不能用 `|| true` 吞掉失败，不能只打印待执行命令。测试文件缺失/收集0项应直接失败。

```bash
# 原回归 + 新增后端用例，一次完整运行，不用全局 -k 隐藏收集项
.venv/bin/python -m pytest \
  tests/test_admin_batch_d.py tests/test_email_verify_activation.py \
  tests/test_account_balance.py tests/test_user_creation_spend_target.py \
  tests/test_format_request.py tests/test_slide_format_registry.py \
  tests/test_kfb_upload.py tests/test_kfbf_upload.py \
  tests/test_upload_v2.py tests/test_upload_accounting_recovery.py \
  tests/test_format_request_concurrency.py tests/test_format_request_migration.py \
  tests/test_project_creation_upgrade.py tests/test_conversion_task_api.py \
  tests/test_baidu_adapter.py tests/test_baidu_imports.py \
  tests/test_baidu_import_recovery.py -q

npm run test:js

# 脚本需先设置 PATH，确保 Playwright webServer 的 python3 来自 .venv
CI=1 npx playwright test \
  tests/e2e/admin-workbench.spec.ts \
  tests/e2e/toolbar-account-upgrade.spec.ts \
  tests/e2e/import-project-upgrade.spec.ts \
  --project=chromium --workers=1

git diff --check
```

补充新脚本必要的 `--junitxml`/reporter参数并在运行说明写出实际产物位置。E2E 端口选择空闲本地端口，`CI=1` 不复用未知服务器；缺 Chromium 时安装测试浏览器后再执行。新文件也必须纳入空白/语法检查，因为 `git diff --check` 本身不覆盖未跟踪文件。

**代码验收通过定义：**以上全部命令退出0；所有矩阵ID有实际执行的测试映射并PASS；新增关键用例0 skip/xfail；没有因重试才被掩盖的flaky；有真实浏览器截图和迁移/并发/崩溃恢复证据。原有可选大样本skip必须单列原因，不能覆盖B06必须具备的小型真实格式fixture。不要求人为凑固定测试总数，但必须报告实际数量。

## 9. 真实百度验收与发布门槛

代码验收可以在隔离环境完成，但不能证明真实供应商路径有效。新增 opt-in `tests/test_baidu_live.py` 或等效冒烟脚本，默认不访问远端；仅在专门的 `RUN_BAIDU_LIVE_TESTS=1` 和测试凭据/分享配置齐备时运行。

| ID | 真实环境测试 | 通过条件 |
|---|---|---|
| L01 | 专用测试分享：至少两层目录、有选中/未选中文件 | CLI真实枚举与预期清单一致；只读阶段无转存/下载/删除 |
| L02 | 选中一份native和一份当前支持的需转换样本 | 实际下载、格式探测/转换、入库可打开；目标项目正确；未选中未转存 |
| L03 | 非敏感大文件至少1GiB，中途终止worker并重启 | 有效恢复或明确有界重下，最终SHA一致；产物和配额不重复；记录时长/字节，不承诺会员速度 |
| L04 | 成功副本清理与故障恢复 | 只删专用批次副本；其他预置文件仍在；清理失败重试不影响入库状态 |

真实参数只从测试环境读取，不在日志/报告中输出完整分享或密钥。默认本地任务不执行真实删除；外部动作仅按本文第0节授权条件进行。没有可用连接器或测试分享时，L01–L04 标 `NOT RUN — 缺少具体条件`；已有配置但能力不满足标 `BLOCKED — 具体退出码/脱敏证据`。不得改成PASS，不得用fake替代真实验收。

状态用两栏报告：`代码验收 PASS/FAIL`、`真实百度验收 PASS/NOT RUN/BLOCKED/FAIL`。只允许前者PASS时说“代码实施及隔离验收完成”；仅两者PASS才可说“百度真实链路验收完成，可进入发布评审”。真实门槛未通过时默认真实导入开关保持关闭。若生产适配器本身无法实现完整合同，这也是代码未完成，不能只以外部验收未执行掩盖。

## 10. 最终交付清单与停止条件

必须交付：业务代码/样式/i18n、PG迁移、旧申请迁移工具、百度生产适配器与worker、全部测试、统一验收脚本、运行与回滚说明，以及 `docs/acceptance-account-import-project-ui-2026-09-14.md`。

验收报告格式至少包含：

```text
基线：HEAD、已有工作区修改；实现完成时HEAD/工作区状态
工作包：W1…W6 → 实际文件/功能 → 完成或未完成
测试映射：A01…L04 → 文件::测试名称 → PASS/FAIL/NOT RUN → 证据路径
命令结果：准确命令、退出码、passed/failed/skipped、运行时间
截图：桌面/手机、中英文、成功/空/错误/处理中/部分失败
迁移：preflight、apply、重复apply、异常拒绝结果
真实连接器：脱敏版本/环境、能力核验、L01…L04状态
遗留问题：影响、具体缺少条件；不得只写“后续优化”
最终结论：代码验收状态；真实百度验收状态；默认开关状态
```

满足第8.4节代码门槛并完成上述交付，才算本次代码任务完成；只改文案、只修R1或仅增加百度入口均不满足。缺外部环境时完成所有可做工作并如实标记真实门槛；代码或必须测试仍失败时不得宣称完成。不得以取消测试、调大超时掩盖死锁、删除原断言、全局忽略浏览器错误或把核心用例skip来达成通过。
