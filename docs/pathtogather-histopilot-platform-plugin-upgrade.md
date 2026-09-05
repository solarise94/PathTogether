# PathTogether 协作式病理读片平台与 HistoPilot AI 插件

## 功能调整与升级方案

> 文档版本：v1.5
> 状态：历史拆分设计。Stage 1–5 已在原一体仓实施；2026-08-17 起 PathTogether、HistoPilot 与 HistoPilot-DSH 改为独立仓库和独立发布。当前操作说明以各仓 README 为准。
> 编写日期：2026-08-12
> 设计基线：仓库 `HEAD 6dbab64` 及其之前已经提交的功能
> 并行工作：`EXP-VISCTX-v1`（原“Phase 4”分辨率/上下文窗口 A/B）正在工作区中开发，不计入本文“已实现”状态

---

## 1. 执行摘要

现有项目已经包含两类价值不同的能力：

1. 病理切片查看、项目管理、分享和标注；
2. 基于大模型的自主导航、快照观察、上下文管理和 AI 标注。

下一阶段建议将其明确调整为两个产品：

- **PathTogether**：协作式病理读片平台，负责用户、组织、病例、切片、Viewer、标注、讨论、分享、权限、审计和插件运行环境。
- **HistoPilot**：可安装的 AI 读片插件，负责 Agent、模型接入、导航策略、提示词、AI 会话、图片预算、上下文压缩、Prompt Cache 和 AI 读片交互。

本次升级的核心不是简单地把 Node sidecar 移到另一个仓库，而是建立稳定的产品边界：

- 平台拥有病理业务数据；
- 插件拥有 AI 运行状态；
- 双方只通过版本化 Plugin Contract 通信；
- 插件不能直接访问平台数据库、共享数据目录或 Viewer 全局变量；
- 未安装或停用 HistoPilot 时，PathTogether 的人工读片和协作功能仍完整可用。

### 1.1 建议决策

| 决策项 | 建议 |
|---|---|
| 初期代码组织 | 先保持 monorepo，完成逻辑和部署边界后再评估拆仓库 |
| HistoPilot 前端隔离 | 独立 bundle；第一阶段用 HostBridge，最终使用 sandbox iframe |
| 服务端通信 | 版本化 HTTP API + SSE；平台签发短期 scoped token |
| 数据库 | 协作数据迁移到 PostgreSQL；开发/单机版可保留 SQLite 适配器 |
| 切片身份 | 使用稳定 `slide_id + asset_revision`，文件名仅作为展示/导入属性 |
| 图片传输 | 服务端间优先 JPEG 二进制流，兼容期保留现有 base64 JSON 适配器 |
| 插件会话 | 由 HistoPilot 自己持久化，平台只保存安装关系和必要的审计引用 |
| AI 标注 | 写入 PathTogether 标注库，并记录插件、run、模型和创建者来源 |
| `EXP-VISCTX-v1` | 与拆分并行；先冻结实验契约，不在边界迁移中修改实验算法 |

### 1.2 命名待确认

本文沿用用户提出的 **PathTogether** 拼写。进入包名、域名、数据库 schema 和公开 API 固化前，需要确认该拼写是否为有意品牌命名；若目标名称是 `PathTogether`，应在 Stage 0 完成统一，避免后续兼容成本。

---

## 2. 现状基线

### 2.1 当前进程结构

```text
Browser
  └─ Flask app.py
       ├─ WSI Viewer / 上传 / 项目 / 分享 / 标注
       ├─ AI 配置与 API 代理
       ├─ /internal/ai/* 私有回调
       └─ Node sidecar
            ├─ Agent loop / tools
            ├─ session / SSE
            ├─ context compaction
            ├─ image materialization
            └─ prompt cache
```

当前 sidecar 已经形成独立进程，是 HistoPilot 服务的良好起点；但它仍属于“平台内部实现组件”，还不是可以独立安装、升级和授权的插件。

### 2.2 主要耦合点

| 耦合 | 当前表现 | 升级影响 |
|---|---|---|
| 私有 Flask 客户端 | sidecar 硬编码 `/internal/ai/region`、`annotate`、`spots`、`slide_info` | 无法接入其他平台实现，也无法独立版本演进 |
| 共享文件系统 | AI token、会话和平台数据共同依赖 `SHARE_DATA_DIR` | 插件无法真正独立部署或水平扩容 |
| 配置归属混乱 | 模型 API Key 由 Flask 保存、解密后逐请求注入 sidecar | 平台与插件安全责任混在一起 |
| 前端直接嵌入 | AI HTML、CSS、状态和事件逻辑位于平台模板及全局 `app.js` | 无法按插件安装/卸载，容易发生 DOM/CSS 冲突 |
| 文件名充当身份 | 大量接口以 `slide` 文件名定位切片 | 重命名、迁移、多租户和同名切片难以处理 |
| 单 JSON 协作存储 | 项目、分享、ROI 和变更序号集中在 `shares.json` | 多用户并发、查询、审计和迁移能力不足 |
| 单容器双进程 | Flask 和 sidecar 被同一个入口脚本强绑定 | HistoPilot 故障会影响平台部署生命周期 |
| 平台领域不足 | 当前“项目”主要是切片分组，缺少组织、病例、成员和讨论模型 | 尚不能支撑真正的协作式读片 |

### 2.3 可直接复用的现有能力

以下能力不应重写，应通过适配器迁移：

- OpenSlide / tifffile WSI 读取；
- Deep Zoom 瓦片、缩略图和 region 输出；
- mpp、level-0 坐标和多级金字塔语义；
- annotation 稳定 ID、revision、change sequence 和 tombstone；
- HistoPilot Agent loop、tools、会话、SSE、compaction；
- 图片指纹校验、派生图规格、LRU、AbortSignal、in-flight 合并；
- PreparedRequest、稳定区/工作区分离和 Prompt Cache；
- `EXP-VISCTX-v1` 的分辨率/上下文窗口实验框架。

---

## 3. 产品目标与非目标

### 3.0 产品定位（2026-08-13 拍板，全文最高优先级约束）

> **这不是 SaaS，也不是病人管理软件。PathTogether 是一个简单的协作读片软件 + HistoPilot agent 导航插件。别搞复杂。**
>
> 部署模型：一个部署实例 + 多用户协作——部署者邀请注册用户与游客共同查看切片（不是单机单人，也不是多租户 SaaS）。
>
> 技术债与选型约束：尽量一次做好不留技术债；能用开源方案的不自己造轮子（PostgreSQL、现有 WSI/Agent 栈均复用，不写双仓储适配器）。
>
> 任何 SaaS 级机制——机构管理、多租户隔离、实名认证、医疗审计、插件商店、签名策略、运维配额——除非另行明确要求，一律不建设。Stages 的设计以"小组协作 + 可插拔 AI"为上限，逐项裁剪过度设计。

### 3.1 PathTogether 产品目标

1. 支持小团队围绕病例协作：上传、查看、标注、评论；协作者分两类加入——邮箱注册的正式用户（可用平台 API），或受邀链接直接进入的游客（AI 需自带 key）。
2. 提供稳定的 WSI 查看、导航、标注与评论能力。
3. 让 AI 读片以插件形式安装、授权、停用和升级。
4. 保证人工产生的业务数据不依赖任一 AI 插件而存在。
5. 对人工与 AI 操作提供轻量操作日志（谁何时改了什么），不引入医疗审计语义。
6. 为未来其他算法插件预留受控接口，但第一版不建设插件市场。

### 3.2 HistoPilot 产品目标

1. 在获得授权的切片上自主低倍扫描、高倍确认和记录观察。
2. 提供主会话、分支会话、标注追问和历史恢复。
3. 在视觉质量、上下文成本、延迟和缓存命中之间做可配置权衡。
4. 支持不同模型/provider，并对不支持的 Prompt Cache 能力安全降级。
5. 通过稳定插件协议接入 PathTogether，而不依赖其内部语言、框架和存储结构。

### 3.3 本轮非目标

- 不做机构（organization）管理、角色权限矩阵和多租户隔离——身份模型止于 owner/guest/sdk-user；
- 不建设公开插件市场、签名/来源策略与运营配额；
- 不引入实名认证体系，邮箱注册封顶，匿名链接保留；
- 不建设医疗审计档案——audit 语义止于协作操作日志；
- 不立即拆成多个 Git 仓库；
- 不为了拆分同步重写 Flask、原生 JavaScript 或 WSI 读取栈；
- 不在接口抽取阶段修改 `EXP-VISCTX-v1` 的实验算法；
- 不将 HistoPilot 输出定义为独立临床诊断结论；
- 不做实时多人光标/同时编辑，只做变更同步。

---

## 4. 目标架构

```text
┌──────────────────────────────────────────────────────────────┐
│ PathTogether Web                                             │
│  Viewer / Cases / Annotations / Discussion / Plugin Host     │
│                                   ┌──────────────────────┐   │
│                                   │ HistoPilot Plugin UI │   │
│                                   │ sandbox iframe       │   │
│                                   └──────────┬───────────┘   │
└───────────────────────────┬──────────────────┼───────────────┘
                            │                  │ HostBridge
                            ▼                  ▼
┌──────────────────────────────────────────────────────────────┐
│ PathTogether API Gateway                                     │
│ Auth / Tenant / Case / Slide / Annotation / Event / Plugin   │
└───────────────┬──────────────────────────────┬───────────────┘
                │                              │ scoped service token
                ▼                              ▼
┌───────────────────────────┐       ┌──────────────────────────┐
│ Slide Service             │       │ HistoPilot Agent Service │
│ metadata/tile/region      │       │ agent/session/cache/SSE  │
└───────────────────────────┘       └─────────────┬────────────┘
                                                 ▼
                                      ┌────────────────────────┐
                                      │ LLM / CPA Gateway      │
                                      └────────────────────────┘
```

### 4.1 平台拥有的数据

- user（owner/注册用户）与 invite token（guest 链接）；
- case / project；
- slide / slide asset / asset revision；
- annotation / comment / thread；
- share link / access grant；
- plugin installation / permission grant；
- activity log / audit event；
- AI 写回平台后的正式标注及其 provenance。

### 4.2 HistoPilot 拥有的数据

- AI engine/provider 配置和密钥；
- session、message、event、observation；
- checkpoint、compaction summary、working set；
- 派生图缓存和 request metrics；
- `EXP-VISCTX-v1` experiment/run 记录；
- provider capability 探测结果；
- 与平台对象关联的外键引用，但不复制平台业务实体。

### 4.3 禁止跨边界共享的数据

- 双方不共享数据库表；
- 双方不通过同一 JSON 文件协作；
- 双方不依赖同一个本地 token 文件；
- HistoPilot 不直接访问 WSI 文件路径；
- PathTogether 不读取或改写 HistoPilot canonical messages；
- 插件 UI 不直接访问平台 DOM、全局变量或 OpenSeadragon 实例。

### 4.4 切片身份与资产版本

`slide_id` 与 `asset_revision` 是两个不同层次的标识：

| 标识 | 语义 | 生命周期 |
|---|---|---|
| `slide_id` | 平台中的逻辑切片身份 | 创建切片记录时由 PathTogether 分配，永久稳定，不因重命名或替换底层文件而变化 |
| `asset_revision` | 当前 WSI 文件内容版本 | 每次底层内容发生变化时生成新值；只改别名、note、病例归属时不变化 |
| `Content-SHA256` | 某个 region 派生输出的字节哈希 | 由 bbox、asset revision、输出规格和编码器共同决定，不能替代 asset revision |

目标 `slide_id` 使用平台生成的不透明 ID，建议格式为 `sld_<uuidv7>`。客户端只能把它当字符串，不能依赖 UUID 格式或从中推导租户信息。

分配流程：

1. 上传开始时创建 pending slide record 并分配 `slide_id`；
2. 文件完整写入并校验后计算内容 SHA-256；
3. 生成不可变 `slide_asset` 记录，`asset_revision = ar_sha256_<digest>`；
4. 原子更新 slide 的 current asset revision；
5. 同名文件不会复用已有 `slide_id`，除非用户明确执行“替换当前切片资产”；
6. 替换只创建新的 asset revision，旧 revision 按保留策略进入只读/待回收状态。

MRXS 等多文件格式的 asset revision 使用确定性的文件清单哈希：对安全归一化后的相对路径、文件长度和每个文件 SHA-256 排序后再计算总哈希。

迁移期使用显式联合类型，禁止把裸文件名伪装成稳定 ID：

```ts
type SlideRef =
  | { kind: "legacy-filename"; filename: string }
  | { kind: "slide-id"; slideId: string };
```

- Stage 1–2 的 `LegacyFlaskPlatformAdapter` 接受 `legacy-filename`；
- Stage 3b 导入旧数据时建立 `legacy filename → slide_id` 映射；
- 新 PathTogether API 只接受 `slide-id`；
- Stage 4 完成后，HistoPilot canonical session 不再创建新的 legacy ref；
- 旧 session 恢复时先经映射服务解析，无法唯一映射则暂停并要求人工选择，禁止按同名猜测。

---

## 5. PathTogether 功能调整

### 5.1 身份与访问（PathTogether 底层查看/标记逻辑）

新增：

- 用户账户（邮箱注册）；
- 受邀游客（链接进入，无注册）；
- 部署者 owner（superadmin，管理一切）；
- 插件访问身份 sdk-user（非自然人，代理某 user 调用）。

无 organization、无角色矩阵、无病例状态机（v0.3 的五角色矩阵与 draft/in_review/finalized/archived 状态机按 §3.0 简化定位裁剪；归档需求仅保留"归档开关"级别的只读保护）。

#### 5.1.1 数据权限（查看 / 标注 / 图库）

> **2026-09 更新注记（review P0 读隔离）**：owner 的**读**不再全量——与 user
> 同一可见模型：自己的 ∪ 公开 ∪ 受邀协作 ∪ **显式授权**。owner 的「看全部」
> 唯一出口是管理台「切片可见性」页（GET /api/admin/v1/slides/inventory 全量
> 清点 + POST /api/admin/v1/slides/<name>/visibility 幂等授权/收回，均
> owner-only + CSRF + audit；无主切片在 inventory 可见并可授权恢复）。
> **写路径不变**：删除切片/标注、项目管理、分享撤销等 owner 语义保持原样。

| 动作 | owner | user | guest |
|---|---:|---:|---:|
| 查看他人切片/标注 | 显式授权后可见（管理台授权；写路径 owner 语义不变） | — | — |
| 上传切片、维护自己的图库 | ✓ | ✓（仅自己的图库） | — |
| 查看他人图库切片 | 显式授权的切片（+ 自己的 + 公开） | 被邀请协作的切片 + 公开切片 | 被邀请协作的切片 + 公开切片 |
| 创建/编辑标注与评论 | ✓ | 自己的切片 + 协作切片 | 协作切片（按分享权限） |
| 删除标注 | ✓ | 本人创建的标注 | 本人创建的标注 |
| 创建协作分享（邀请 user/guest） | ✓ | 自己的切片 | — |
| 管理用户与游客（停用/移除） | ✓ | — | — |
| 配置平台 API key、公开切片集 | ✓ | — | — |

后端做资源级鉴权，不能只依赖前端隐藏按钮。sdk-user 的权限 = 被代理 user 的权限，且可被 user 显式收窄。

#### 5.1.2 AI 凭据权限（agent 查看逻辑）

| 动作 | owner | user | guest |
|---|---|---|---|
| 使用平台配置的官方托管 API（如 luna） | ✓ | ✓ | — |
| 自带 API key 运行 AI | ✓ | ✓ | ✓（必须自带） |

平台配置的官方 API 就是注册用户（owner/user）的常规 AI 来源之一，与自带 key 并列——不存在额外的"兜底"机制：平台未配置官方 API 时，注册用户同样需要自带 key；guest 则始终必须自带 key，无 key 时 AI 不可用、人工读片不受影响。

> **2026-08 更新注记**：user 的自带 API key 通道已下线（AI 服务统一由平台提供）。上表中「自带 API key 运行 AI」一列现仅适用于 owner（owner 平台配置不变）；user 恒走平台凭据，平台未配置时 AI 不可用并提示联系管理员。详见 demo-access-auth-ui-design.md §9.2 的更新注记。

AI 会话与标注历史：user 持久记录在名下；guest 的 AI 会话不持久（会话结束即弃），人工标注若允许则按分享权限记录。

### 5.2 项目升级为病例工作区

现有 `project` 兼容迁移为轻量 `case`（切片集合 + 讨论协作单元，无状态机）：

```text
Case
  id
  title
  note
  owner_user_id           # 创建者（user 或 owner）
  visibility: private | shared | public
  slides[]
  created_at / updated_at
  archived_at?            # 归档开关（只读保护），无 draft/in_review/finalized 状态机
```

`case_grants` 是协作授权的唯一权威来源：`case_id`、`grantee_type`（user/guest）、`grantee_user_id?`、`invite_token_hash`（guest 链接）、`permissions`（view/annotate/comment）、`granted_at`、`revoked_at`。查看权限判定只认这张表 + `visibility`。

兼容策略：

- 旧项目生成新的 `case_id`；
- 原项目名称和 note 原样迁移；
- 未归类切片进入"待整理"系统 case 或保持图库级资产；
- 旧 API 在迁移期返回 deprecation header。

### 5.3 协作式标注

标注统一模型：

```text
Annotation
  id
  case_id
  slide_id
  geometry
  coordinate_space: level0
  label
  note
  author_type: human | plugin
  author_user_id?
  plugin_id?
  plugin_run_id?
  revision
  change_seq
  status: active | resolved
  deleted_at?
  created_at / updated_at
```

新增功能：

- 标注下评论线程；
- `@成员` 与待办状态（仅协作 case 内）；
- 标注接受/驳回 AI 建议；
- 标注修改历史；
- 按作者、来源、标签和状态过滤；
- 人工标注与 AI 建议使用可区分但一致的交互样式。

删除采用同行 tombstone：原 annotation 行保留稳定 ID、最后 revision 和最小日志字段，并设置 `deleted_at`；默认列表不返回，增量 change stream 返回 `annotation.deleted`。不另建"墓碑表"，也不使用 `status=deleted`，避免业务状态与同步状态混淆。

### 5.4 分享与外部会诊

- 分享 = 对 case/单张切片生成受邀链接（guest 进入）或直接邀请注册用户；
- 权限三档：查看 / 标注评论 / 下载开关；
- 支持到期、撤销、访问日志；
- 旧匿名分享链接保留兼容读取，禁止自动扩大权限；
- 不做实名认证体系，邮箱注册封顶。

### 5.5 插件管理

平台新增：

- 插件安装、启用、禁用和升级（owner 操作）；
- manifest 校验；
- 插件入口加载；
- 插件健康状态；
- 每次运行的用户授权上下文（sdk-user 代理）；
- 插件操作日志（轻量操作日志，非医疗审计）。

## 6. HistoPilot 功能调整

### 6.1 服务端重命名与抽象

将当前：

```text
FlaskClient → Flask internal endpoints
```

调整为：

```text
PlatformClient interface
  ├─ LegacyFlaskPlatformAdapter（迁移期）
  └─ PathTogetherHttpClient（目标实现）
```

Agent、tools、request assembler 和 transform context 只能依赖 `PlatformClient` 接口，不能导入 Flask 语义。

建议能力接口：

```ts
interface PlatformClient {
  getSlide(slide: SlideRef): Promise<SlideDescriptor>;
  readRegion(request: RegionRequest, signal?: AbortSignal): Promise<RegionResult>;
  listAnnotationChanges(slide: SlideRef, afterCursor?: string): Promise<ChangePage>;
  createAnnotation(request: CreateAnnotationRequest): Promise<Annotation>;
  updateAnnotation(request: UpdateAnnotationRequest): Promise<Annotation>;
  deleteAnnotation(request: DeleteAnnotationRequest): Promise<AnnotationTombstone>;
  openEventStream(request: EventStreamRequest, signal?: AbortSignal): AsyncIterable<PlatformEvent>;
  appendAuditEvent(request: PluginAuditEvent): Promise<void>;
}

interface RegionResult {
  bytes: Uint8Array;
  mimeType: "image/jpeg";
  width: number;
  height: number;
  src: { x: number; y: number; w: number; h: number };
  magnification: number | null;
  contentSha256: string;
  assetRevision: string;
  encoder: {
    id: string;
    version: string;
    resize: string;
    overlayVersion: string;
    jpegQuality: number;
  };
}

interface AnnotationTombstone {
  annotationId: string;
  slideId: string;
  revision: number;
  deletedAt: string;
}
```

`RegionResult` 从 Stage 1 起就是二进制原生类型。`LegacyFlaskPlatformAdapter` 负责把现有 Flask JSON 中的 `image_base64` 解码为 `Uint8Array`，并把 snake_case 字段归一化；Agent、tools、assembler 和缓存层不得看到 legacy base64 transport。目标 `PathTogetherHttpClient` 直接读取 `image/jpeg` 响应，因此替换 adapter 时下游类型不变。

迁移期 adapter 还负责补齐目标契约：`contentSha256` 由解码后的实际 JPEG bytes 计算；当前 `mtime:size` fingerprint 被封装为不透明的 `legacyAssetRevision`，只能用于 legacy CAS，不能写成目标 `ar_sha256_*`。Stage 3b 导入时生成真正的内容型 asset revision，并保留旧值到新值的恢复映射。

`legacyAssetRevision` 保留当前 `mtime:size` 的保守语义：仅 `touch` 文件也会使 revision 改变并触发 409，即使文件内容未变。这是迁移期允许的安全误失效，不应在 adapter 中通过猜测内容相同而绕过；Stage 3b 切换到内容型 asset revision 后自然消除。

Stage 1 的 `SlideRef` 仍可能是 `legacy-filename`；Stage 3b 完成映射后新请求使用稳定 `slide-id`。接口参数不能在迁移期写成含义模糊的裸 `string`。

### 6.2 插件自己的配置

HistoPilot 负责：

- provider base URL；
- credential resolver 与最终解析出的 provider credential；
- model / protocol；
- compaction 与视觉预算参数；
- prompt cache mode；
- `EXP-VISCTX-v1` 实验档位。

平台只负责：

- 哪个组织安装了插件；
- 哪些用户可以运行；
- 插件能访问哪些病例/切片能力；
- 按组织授权的 secret reference（若最终产品策略允许）。

禁止平台在普通业务请求中反复把明文 API Key 注入插件。

凭据归属按 §19 决策 3：`CredentialResolver` 支持 `user / platform-managed` 两种来源——注册用户（owner/user）可用平台配置的官方 API 或自带 key；guest 必须自带 key。Agent 只接收 resolver 的结果及不含密钥的 `credential_source` 标识，不得把某一来源硬编码为永久优先级。

### 6.3 会话关联

HistoPilot session 至少保存：

```text
platform_instance_id?   # 单实例部署可省
case_id
slide_id
slide_asset_revision
initiated_by_user_id
plugin_installation_id
engine_version
experiment_profile?
```

平台可以保存 `plugin_session_id` 引用用于打开插件历史，但不复制完整 transcript。

### 6.4 AI 标注 provenance

每个 AI 写回操作必须携带：

- `Idempotency-Key`；
- `plugin_id=histopilot`；
- `plugin_version`；
- `run_id/session_id`；
- `model/provider` 的审计标识，不记录密钥；
- 创建该 run 的平台用户；
- 当前 `slide_asset_revision`。

切片 revision 不匹配时返回 `409 slide_revision_conflict`，插件不得静默写入旧坐标。

`Idempotency-Key` 规范：

- 服务端命名空间为 `(plugin_installation_id, idempotency_key)`；
- HistoPilot 推荐生成 `${session_id}:${tool_call_id}:${effect_seq}`，与当前 `effect_key` 语义对应；
- 同 key、同 canonical payload 重试时返回第一次写入的对象和原状态码语义；
- 同 key、不同 payload 返回 `409 idempotency_key_reused`；
- 记录保留时间不得短于对应 session 的可恢复期，默认至少 30 天；
- Idempotency record 与 annotation 写入在同一数据库事务提交。

---

## 7. Plugin Contract v0.1

### 7.0 命名与版本模型

命名规则：

- 数据库列和文档中的持久化领域模型使用 `snake_case`，例如 `deleted_at`、`asset_revision`；
- REST JSON、SSE data、HostBridge payload 和 TypeScript 公共接口使用 `camelCase`，例如 `deletedAt`、`assetRevision`；
- HTTP header 使用标准或 `X-...` 的连字符形式；
- adapter 是唯一允许执行命名转换的位置；同一 wire contract 内禁止混用两种命名；
- URL path 中的资源参数沿用 `snake_case`（例如 `{slide_id}`），query 参数 v0.1 也使用 `snake_case`。

四类版本相互独立，禁止共用一个模糊的 `version` 字段：

| 字段 | 控制范围 | bump 规则 |
|---|---|---|
| `manifestSchemaVersion` | manifest 文件自身的字段结构 | manifest 语法不兼容时 bump major |
| `pluginContractVersion` | 服务端 capability API、领域类型和错误码 | 按 SemVer；破坏 API/语义时 bump major |
| `bridgeProtocolVersion` | iframe HostBridge 消息协议 | 按 SemVer；消息不兼容时 bump major |
| `pluginVersion` | HistoPilot 产品/镜像/bundle 版本 | 正常产品 SemVer，不自动改变其他三个版本 |

`plugin_contract_version` 实验字段必须记录运行时实际协商成功的 `pluginContractVersion`，不能记录 plugin 产品版本。Stage 5 的 N/N-1 兼容要求分别针对 `pluginContractVersion` 和 `bridgeProtocolVersion` 的 major；manifest loader 至少支持当前与前一 major。平台在加载/启动前完成版本协商，不兼容则拒绝启动并返回明确错误，不能带病运行。

### 7.1 Manifest

```json
{
  "manifestSchemaVersion": "1.0.0",
  "id": "com.pathtogather.histopilot",
  "name": "HistoPilot",
  "pluginVersion": "0.1.0",
  "pluginContractVersion": "1.0.0",
  "bridgeProtocolVersion": "1.0.0",
  "ui": {
    "entry": "/plugin/index.html",
    "slots": ["viewer.right-panel", "annotation.actions"]
  },
  "service": {
    "baseUrl": "http://histopilot:8055",
    "health": "/healthz"
  },
  "permissions": [
    "slide:metadata:read",
    "slide:region:read",
    "annotation:read",
    "annotation:write",
    "viewer:navigate"
  ]
}
```

### 7.2 平台能力 API

建议 namespace：`/api/plugin/v1`。

| 方法 | 路径 | 用途 |
|---|---|---|
| GET | `/slides/{slide_id}` | 尺寸、mpp、levels、asset revision |
| POST | `/slides/{slide_id}/regions` | 按 level-0 bbox 读取派生图 |
| GET | `/slides/{slide_id}/annotations?after=` | 全量或增量读取标注 |
| POST | `/slides/{slide_id}/annotations` | 幂等创建标注 |
| PATCH | `/annotations/{annotation_id}` | 带 revision 的 CAS 更新 |
| DELETE | `/annotations/{annotation_id}` | 带 revision 的 tombstone 删除 |
| GET | `/events/stream?slide_id=` | 标注/切片变化事件 |
| POST | `/audit/plugin-events` | 上报插件关键操作摘要 |
| POST | `/usage-events` | HistoPilot durable usage outbox 投递模型调用量事件（§7.8） |

`PlatformClient` 必须覆盖本表全部能力；尚未使用的能力可以由 adapter 返回显式 `capability_not_supported`，不能从类型中消失。manifest 的 permissions、HTTP endpoint 和 client 方法使用同一份 contract schema 生成或校验，避免三份定义漂移。

### 7.3 RegionRequest

```json
{
  "bbox": {"x": 0, "y": 0, "w": 10000, "h": 5000},
  "coordinateSpace": "level0",
  "maxLongEdge": 1024,
  "format": "jpeg",
  "quality": 85,
  "overlay": "coordinate-ticks-v1",
  "expectedAssetRevision": "ar_sha256_..."
}
```

要求：

- 输出保持宽高比；
- 返回真实输出宽高；
- 返回 `Content-SHA256`、asset revision 和 encoder descriptor；
- `AbortSignal` 断开后服务端应中止排队或尚未完成的工作；
- 指纹/revision 不匹配返回 409；
- 新客户端使用 `image/jpeg` 二进制响应；
- `LegacyFlaskPlatformAdapter` 把当前 Flask 的 base64 JSON 解码为统一 `RegionResult.bytes`；
- `overlay` 是可选枚举，v0.1 允许 `none` 和 `coordinate-ticks-v1`，缺省 `none`；HistoPilot snapshot 明确请求 `coordinate-ticks-v1`；
- 未知 overlay 返回 `400 invalid_overlay`，overlay 像素算法变化必须引入新枚举值，不能原地修改旧版本。

目标二进制响应通过 HTTP headers 携带元数据：

```text
Content-Type: image/jpeg
Content-SHA256: <hex>
ETag: "<content-sha256>"
X-Asset-Revision: ar_sha256_...
X-Region-Width: 1024
X-Region-Height: 512
X-Region-Src: x=0,y=0,w=10000,h=5000
X-Region-Magnification: 2.5
X-Encoder-Id: pillow
X-Encoder-Version: ...
X-Encoder-Resize: LANCZOS
X-Overlay-Version: coordinate-ticks-v1
X-JPEG-Quality: 85
```

header 缺失、非法或与 bytes/hash 不一致时，`PathTogetherHttpClient` 将响应视为 `invalid_region_response`，不得把不完整结果放入 derivative cache。

### 7.4 事件流

平台事件流使用 SSE：

```text
id: <cursor>
event: <event-type>
data: {"cursor":"...","occurredAt":"...","resourceRevision":"...","payload":{...}}

```

v0.1 事件类型：

```text
annotation.created
annotation.updated
annotation.deleted
slide.asset-replaced
case.status-changed
permission.revoked
stream.reset-required
```

- SSE `id` 是权威恢复 cursor，data 中重复 cursor 便于非 SSE adapter 使用；
- annotation 事件 payload 使用与 REST 相同的 schema；deleted payload 至少含 annotation ID、slide ID、revision 和 `deletedAt`；
- `slide.asset-replaced` 携带旧/新 asset revision，HistoPilot 收到后停止旧 revision 的写操作并使 region/checkpoint 缓存失效；
- cursor 过期时服务端发送一次 `stream.reset-required` 后关闭，插件重新拉取完整 annotation snapshot；
- 心跳使用 SSE comment，不推进 cursor；
- 未知事件必须忽略并记录指标，不能导致整个流失败。

### 7.5 前端 HostBridge

插件 UI 不直接持有 OpenSeadragon 对象，而通过消息协议调用：

```text
Host → Plugin
  auth.bootstrap
  context.changed
  slide.opened
  viewport.changed
  annotation.selected
  annotation.changed
  theme.changed

Plugin → Host
  auth.refresh
  viewer.navigate
  viewer.highlight
  viewer.flushViewport
  annotation.select
  panel.resize
  notification.show
```

消息分为需要结果的 `request/response` 和单向通知的 `event`，不能混用：

```ts
type BridgeRequest = {
  kind: "request";
  protocolVersion: string;
  requestId: string;
  pluginInstallationId: string;
  method: string;
  payload: unknown;
};

type BridgeResponse = {
  kind: "response";
  protocolVersion: string;
  requestId: string;
  ok: boolean;
  result?: unknown;
  error?: ContractError;
};

type BridgeEvent = {
  kind: "event";
  protocolVersion: string;
  eventId: string;
  pluginInstallationId: string;
  type: string;
  payload: unknown;
};
```

`auth.refresh`、`viewer.navigate`、`viewer.highlight`、`viewer.flushViewport`、`annotation.select` 和 `panel.resize` 是 request，必须返回 response；`auth.bootstrap`、`slide.opened`、`annotation.changed`、`theme.changed` 是 Host→Plugin event；`notification.show` 是 Plugin→Host event，单向提交展示请求，不等待 ack，也不参与 10 秒 response timeout。需要知道通知是否实际显示的未来能力应新增独立 request，不能改变 `notification.show` 的既有语义。

`viewport.changed` 高频事件由 Host 在 `requestAnimationFrame` 边界合并，最多发送 30 Hz，只保留最新 viewport；交互结束、程序化导航完成或插件发送 `viewer.flushViewport` request 时，Host 立即发送一条 `viewport.changed {final:true}`。插件不得假设收到每个中间帧。

Host 必须校验 `event.origin`、iframe window、plugin installation、协议版本和对应 permission。response 超时默认 10 秒；超时返回本地 `bridge_timeout`，不能无限挂起 UI。

### 7.6 鉴权与续期

目标方案：

1. 安装 HistoPilot 时，平台为 installation 创建可撤销、可轮换的 service credential，只存于双方 secret store，不下发浏览器；
2. 用户启动 run 时，平台创建有最大生命周期的 `run_grant`，记录 user、installation、case/slide scope 和 permissions；
3. 平台签发短期 access JWT，默认 10 分钟，包含 `run_grant_id` 和上述授权快照；
4. HistoPilot backend 使用 access JWT 调平台能力 API；浏览器插件 UI 只通过同源网关/BFF，不持有 installation service credential；
5. JWT 即将过期时，HistoPilot backend 用 installation service credential + `run_grant_id` 调 `/api/plugin/v1/token/exchange` 换取新 access JWT；
6. 即使用户离线，只要 run grant 未过期且权限未撤销，长任务仍可续期；run grant 默认最长 1 小时；
7. 成员、病例或插件权限撤销会使 exchange 失败，并通过 `permission.revoked` 通知在途任务停止；
8. 每个能力 API 仍根据当前数据库状态做资源级鉴权，不能只相信旧 JWT 快照；
9. service credential 定期轮换，泄露或卸载插件时立即撤销。

迁移完成后删除共享 `AI_INTERNAL_TOKEN` 文件方案。

#### 7.6.1 插件 UI 到 HistoPilot backend

浏览器身份使用独立的 UI session 流程，不把 installation credential 或平台能力 access JWT 暴露给 iframe：

1. 已登录用户点击打开插件时，PathTogether backend 根据平台 session、CSRF、病例权限和 installation 状态创建一次性 `plugin_launch_ticket`；
2. ticket 绑定 user、installation、case/slide、目标 iframe origin 和随机 nonce，60 秒过期且只能交换一次；
3. Host 通过受校验的 HostBridge bootstrap event 把 ticket 交给 HistoPilot UI；不放进长期 URL、localStorage 或日志；
4. UI 向同源插件网关/BFF 的 `/plugin/session/exchange` 交换一个短期 `plugin_ui_token`；该 token 只允许调用 HistoPilot 的 UI/session API，不能调用 PathTogether capability API；
5. `plugin_ui_token` 默认 5 分钟，保存在内存；续期时 UI 通过 HostBridge `auth.refresh` request 请求新的单次 ticket，再次交换；
6. BFF/HistoPilot backend 根据 UI token 中的用户上下文创建 run；随后由 backend 按 §7.6 获取 run grant/access JWT 调用平台；
7. iframe 被关闭、用户登出、病例权限撤销或插件停用时，UI token 和对应 launch/run grant 均失效；
8. HostBridge context payload 只用于界面状态，不能单独作为服务端身份凭证。

目标 sandbox iframe 可以是独立 origin；网关需配置严格 CORS allowlist 和 CSP。此流程不依赖第三方 cookie，因此不受浏览器禁用 third-party cookies 的影响。

### 7.7 统一错误信封

所有 JSON 错误和二进制 endpoint 在发送正文前发生的错误统一为：

```json
{
  "error": {
    "code": "slide_revision_conflict",
    "message": "切片资产已更新",
    "retryable": false,
    "requestId": "req_...",
    "details": {"expected": "...", "actual": "..."}
  }
}
```

基础映射：

| HTTP | code 示例 | retryable |
|---:|---|---:|
| 400 | `invalid_request`, `invalid_overlay` | false |
| 401 | `token_expired`, `invalid_token`, `launch_ticket_expired`, `launch_ticket_reused` | 仅 `token_expired` 可按对应流程续期后重试 |
| 403 | `permission_denied`, `plugin_disabled` | false |
| 404 | `slide_not_found`, `annotation_not_found` | false |
| 409 | `slide_revision_conflict`, `revision_conflict`, `idempotency_key_reused` | false |
| 410 | `cursor_expired` | false，需 full reset |
| 429 | `rate_limited`, `region_budget_exceeded` | true，遵守 `Retry-After` |
| 5xx | `service_unavailable`, `region_failed` | 按 code 决定 |

SSE 已建立后不能改 HTTP 状态，使用 `event: error`，data 为同一 error 对象；是否重连由 `retryable` 决定。错误 `message` 用于展示，程序分支只能依赖稳定 `code`。

### 7.8 Usage ingestion（用量事件投递）

> 登记于 2026-08-28，实施批次 PR0（契约先行）/ PR2（PathTogether 服务端）。业务背景、
> 计价与时钟规则见 `docs/admin-billing-plugin-implementation-plan.md` §4–§7。

HistoPilot 为每次真实 provider 调用生成一个 usage event，经 durable outbox
at-least-once 投递：

```http
POST /api/plugin/v1/usage-events
Authorization: Bearer <plugin JWT>
Content-Type: application/json
Idempotency-Key: <event_id>
```

要求：

- **鉴权**：沿用 §7.6 的插件 JWT（`iss=pathtogether`，`aud=plugin`），且对应
  installation 必须处于 enabled 状态。不得使用浏览器 owner session 调用本端点；
- **schema 版本**：body 携带 `schema_version`，v1 的 request body 契约固定为
  `tests/fixtures/usage_events/schema_v1.json`（draft 2020-12，
  `additionalProperties:false`；样例事件与 canonical payload_hash 规范见同目录
  `README.md`）。不兼容变更必须 bump 版本并协商，不得原地改语义；
- **幂等语义**：`Idempotency-Key` header 必须与 body `event_id` 一致。服务端在
  schema 校验与字段规范化后自行计算 canonical `payload_hash`（规则以
  `tests/fixtures/usage_events/README.md` 为唯一依据），不信任客户端提交值。
  同一 `event_id` 重放且 hash 相同：返回原行并标记 `duplicate`，不重复计价或入账；
  hash 不同：返回 409 `usage_event_conflict` 并告警，绝不能以新 payload 覆盖旧账单。
  `call_id` 服务端唯一；
- **主体绑定**：body 中的 `subject_type`/`subject_id`/`user_id` 只是 assertion。
  服务端按权威顺序解析计费主体：`request_id` → `ai_budget_reservations`（要求
  `state='consumed'` 且 session 一致）→ demo capability（`demo_sessions` 的
  `histopilot_session_id`）→ run grant 交叉校验，最后与 assertion 比对。不一致返回
  确定性 409 `usage_subject_conflict`（P0 告警）；绑定行尚未提交返回可重试 409
  `usage_subject_not_ready`。Demo 主体只计量，不开户、不写 ledger；
- **影子阶段**：PR2 只做入库与计价，不写 `usage_debit` ledger entry；硬计费另按
  admin-billing 方案 §12/§14 门槛显式开启。

成功响应（§7.7 错误信封之外的正常信封）：

```json
{ "ok": true, "event_id": "use_...", "duplicate": false, "status": "priced", "priced": true }
```

`status` 取 `priced`/`unpriced`/`void`：未知模型、找不到有效价格、缺最终 usage、
算术或时钟校验失败时写 `unpriced`，不猜测 token、不自动扣费。

---

## 8. 数据与存储升级

### 8.1 PathTogether 数据库

建议 PostgreSQL 表域：

```text
identity: users, invite_tokens
cases: cases, case_grants, case_slides
slides: slides, slide_assets, slide_metadata
annotations: annotations, annotation_revisions, comments
sharing: access_grants, share_links, share_visits
plugins: plugin_catalog, plugin_installations, plugin_grants
audit: audit_events, outbox_events
```

### 8.2 HistoPilot 数据库

建议表域：

```text
sessions
session_messages
session_events
observations
checkpoints
experiment_runs
provider_capabilities
```

派生图片正文不进入关系数据库，可进入内存/磁盘 LRU 或对象存储；数据库只存 content hash、规格和生命周期信息。

### 8.3 变更流

保留当前 `change_seq` 的单调语义，升级为正式 cursor：

- annotation 写入与 outbox event 在同一事务提交；
- 插件按 cursor 拉取或订阅；
- 删除使用 annotation 同行 tombstone（设置 `deleted_at` 并保留最小同步/审计字段），不创建独立墓碑行；
- 重连时 cursor 过期，返回 `reset_required` 并重新取全量快照；
- 插件内部 `spot_cursor` 与平台 cursor 分开命名。

### 8.4 旧数据迁移

| 旧数据 | 新数据 |
|---|---|
| `projects` | `cases` |
| `slide_meta` | `slides + slide_metadata` |
| `rois` | `annotations + annotation_revisions` |
| share token | `share_links + access_grants` |
| `ai_sessions/*.json` | HistoPilot session storage |
| `ai_config.json` | HistoPilot encrypted config/secret store |

迁移工具必须支持 dry-run、数量核对、幂等重跑和回滚备份。

历史 `source="ai"` ROI 只保证存在 `created_by_session_id`，无法可靠补出 `plugin_id`、`plugin_version` 或 model。迁移时：

- `author_type=plugin`；
- `plugin_id=histopilot-legacy`；
- `plugin_version=null`、`model=null`；
- 保留原 `created_by_session_id`；
- provenance 增加 `migration_quality="partial"`；
- 禁止根据 label/note 猜测模型或版本。

这些 null 是允许的历史兼容状态；新 API 写入的 AI 标注不得缺少 provenance 必填字段。

---

## 9. 代码与部署结构

### 9.1 建议目录

```text
apps/
  pathtogather-web/

services/
  pathtogather-api/
  slide-service/

plugins/
  histopilot/
    manifest.json
    ui/
    service/

packages/
  plugin-contract/
  plugin-sdk-web/
  plugin-sdk-server/
  pathology-domain/

legacy/
  flask-adapter/          # 只在迁移期存在
```

### 9.2 第一阶段文件映射

| 当前文件 | 第一阶段目标 |
|---|---|
| `sidecar/src/flask-client.ts` | `PlatformClient` 接口 + `LegacyFlaskPlatformAdapter` |
| `sidecar/src/agent-runner.ts` | `plugins/histopilot/service`，保持核心逻辑 |
| `sidecar/src/tools.ts` | 改为只依赖 `PlatformClient` |
| `static/app.js` AI 段 | `plugins/histopilot/ui` |
| `templates/index.html` AI 面板 | 插件 UI entry |
| `app.py /internal/ai/*` | Legacy adapter，最终由 `/api/plugin/v1/*` 取代 |
| `app.py /api/ai/*` | 插件网关路由或直接指向 HistoPilot service |
| `share_store.py` | 仓储接口，随后迁移关系数据库 |
| `docker_entry.sh` | 平台与插件分别启动，开发环境用 compose 编排 |

### 9.3 部署单元

目标部署至少包含：

```text
pathtogather-web
pathtogather-api
slide-service        # 初期可仍合并在 API 内
histopilot-service   # 可选
postgres
object-storage       # 可选，切片/派生资产
```

要求：

- HistoPilot 停机不影响人工 Viewer；
- 平台健康检查不依赖插件健康；
- 插件独立滚动升级；
- 插件无权限读取未授权组织/病例；
- 不通过宿主路径挂载共享会话目录；
- 平台先独立启动，插件健康只影响插件入口，不再由 `docker_entry.sh` 阻塞 gunicorn 启动；
- HistoPilot 不可用时，插件面板显示“服务暂不可用”并允许重试，平台 `/api/ai/*` 兼容路由返回统一 `503 service_unavailable`，不能使 Viewer 或平台 healthz 失败；
- 已建立的 AI SSE 在插件故障时发送/转译为 retryable error 并关闭，前端不得无限显示 running。

Stage 4 即使尚未建设完整配额产品，也必须启用基础保护：

- 按 plugin installation 和 user 限制并发 run；
- region endpoint 限制请求速率、并发解码数、`maxLongEdge` 和单位时间像素预算；
- 超限返回 `429`、稳定错误码和 `Retry-After`；
- cancel/断连释放排队配额；
- 限流参数可配置并进入结构化指标。

---

## 10. 分阶段实施计划

### Stage 0：冻结基线与命名

目标：避免 `EXP-VISCTX-v1`、产品重命名和插件拆分互相污染。

工作项：

- 确认 `PathTogether` 最终拼写；
- 为 `EXP-VISCTX-v1` 记录 engine/schema/contract version；
- 冻结现有 `/internal/ai/*` 请求响应样本；
- 建立拆分 ADR 和兼容原则；
- 给当前未提交实验工作单独形成 commit。

验收：

- 能从 commit 和实验记录复现当前行为；
- 拆分提交不混入实验算法改动。

### Stage 1：后端边界抽取

目标：HistoPilot 核心不再依赖 Flask 名称、base64 transport 和实现细节。

工作项：

- 引入 `PlatformClient`、`SlideRef` 和二进制原生 `RegionResult`；
- 保留 `LegacyFlaskPlatformAdapter`，由其执行 base64 → bytes 解码；
- 将 slide、region、annotation、events、audit 和错误类型移入 contract package；
- 用 contract tests 锁住现有行为；
- filename 使用显式 `LegacySlideRef`，不伪装成 `slide_id`。

验收：

- Agent/tools 中无 `/internal/ai` 字符串和 Flask 类型；
- Agent/tools/assembler 中无 legacy `image_base64` transport 类型；
- 替换 mock adapter 后所有 sidecar 测试可运行；
- 拆分前已有测试无回退，新增 adapter contract tests 全部通过；
- 生产行为与拆分前一致。

回滚：删除新 adapter 装配，恢复原 FlaskClient 实现；不涉及数据迁移。

### Stage 2：HistoPilot UI 模块化

目标：从平台全局 JS/DOM 中抽离 AI 面板。

工作项：

- 提取 AI 状态、API client、SSE parser 和 renderer；
- 实现 request/response/event 三类 HostBridge 消息；
- 平台 Viewer 用 adapter 暴露当前 slide/viewport/annotation；
- 第一阶段同源加载独立 bundle；
- 第二阶段切换 sandbox iframe。

验收：

- 删除/禁用插件 bundle 后平台人工读片功能正常；
- 插件代码不读取平台全局 `state`、`viewer` 或 DOM selector；
- 导航、选区判读、标注跳转和断线重连保持一致；
- viewport 高频事件满足节流与 final flush 契约；
- 主题、移动端和无障碍交互无明显回退。

### Stage 3a：身份与访问边界

目标：建立四级身份（owner/user/guest/sdk-user）与切片/病例级授权，替代现有的隐式单用户模型。

工作项：

- 引入 user 账户（邮箱注册）、invite token（guest 链接）与 sdk-user 代理身份；
- 为现存数据归属 owner（部署者）账户；
- 给 case、slide、annotation、share 引用回填 owner_user_id；
- 后端接入 §5.1.1 数据权限矩阵（owner/user/guest）与 §5.1.2 AI 凭据规则；
- 增加越权拒绝测试（guest 上传切片、跨用户图库读取等）。

验收：

- 所有业务对象均可归属到 user 或 owner；
- guest 上传/维护图库在应用/仓储边界被拒绝；
- 越权 ID 枚举、region 读取和标注写入均被拒绝；
- 仍使用旧存储时也保持原功能兼容。

### Stage 3b：切片/病例模型与 PostgreSQL

目标：建立稳定 slide identity，并迁移核心业务存储。

工作项：

- 创建 slide、slide_asset 和 asset revision 模型；
- 导入旧文件并建立 legacy filename 映射；
- project → case；
- `shares.json` 的 project/slide metadata/ROI 主数据迁移 PostgreSQL；
- 采用 expand-and-contract 双读核对后切主读；
- 提供 dry-run、校验和回滚备份。

验收：

- 同名切片具有不同稳定 ID；
- 重命名不改变 `slide_id`，替换内容生成新 asset revision；
- 旧数据迁移数量、ID、几何和 mpp 一致；
- legacy session 可通过映射恢复，歧义映射会暂停而非猜测；
- 数据库切换可按 feature flag 回退读路径。

### Stage 3c：协作、审核与事件

目标：补全协作式读片能力，并让插件获得可靠变更流。

工作项：

- 标注评论、审核和 revision 历史；
- 实现同行 tombstone；
- 分享升级为 access grant；
- 引入 audit/outbox 和正式 event cursor；
- AI provenance 与历史 partial provenance 迁移；
- 落实病例状态对角色权限的覆盖规则。

验收：

- 并发编辑使用 revision CAS，不发生静默覆盖；
- finalized/archived 的写保护符合权限矩阵；
- 旧分享链接按兼容策略工作；
- 新 AI 标注 provenance 完整，历史 AI 标注明确标记 partial；
- cursor 重连、tombstone 和 reset-required 有端到端测试。

### Stage 4：独立插件服务

目标：HistoPilot 可以独立部署、启停和升级。

工作项：

- HistoPilot 独立 session 数据库；
- 模型配置迁出平台并接入 CredentialResolver；
- scoped access JWT + run grant + installation credential 替代共享 token；
- 正式 `/api/plugin/v1`、统一错误信封和 SSE contract；
- 切换二进制 region transport；
- 平台插件安装和权限 UI；
- 分离容器、启动顺序和健康检查；
- 上线基础并发、region 像素预算和速率限制。

验收：

- 平台和插件不共享 volume/database；
- 停止 HistoPilot 后平台可独立启动，Viewer、标注和协作正常；
- 插件面板和兼容路由按约定返回可恢复的 service unavailable 状态；
- 未授权插件请求返回 403；
- token 到期续期、run grant 到期、撤销和组织切换均有测试；
- region 超限返回 429 且不会压垮 slide service；
- 插件升级不要求平台同时发布。

### Stage 5：通用插件 SDK

目标：协议不再只是 HistoPilot 的专用桥接层。

工作项：

- 发布 manifest schema 和三类版本规范；
- SDK 示例插件；
- contract/bridge 版本协商和兼容矩阵；
- 安装包签名/来源策略；
- 扩展资源配额管理和运营配置。

验收：

- 一个不依赖 HistoPilot 源码的最小示例插件可读取当前切片 metadata、导航 Viewer 并创建测试标注；
- 权限越界请求稳定失败；
- plugin contract 和 bridge protocol 的 N/N-1 major 兼容策略有自动化测试。

---

## 11. 与 `EXP-VISCTX-v1` 的并行策略

产品迁移只使用 `Stage` 编号；视觉上下文实验统一使用以下 ID，后续正文、代码、指标和提交信息不再使用无定语的“Phase 4”：

```text
EXP-VISCTX-v1 = HistoPilot Visual Context Experiment v1
```

### 11.1 可以并行的工作

- `EXP-VISCTX-v1` 真实读片任务集和评分标准；
- 分辨率、视觉预算和窗口参数 A/B；
- provider payload 和 Prompt Cache 验证；
- `PlatformClient` 接口抽取；
- contract test 和 UI HostBridge 设计。

### 11.2 不应同时修改的内容

- 不在接口抽取提交里改视觉 token 公式；
- 不在 UI 拆分提交里改任务提示词或 Agent tool schema；
- 不在数据库迁移时改 observation/annotation 语义；
- 不把接口迁移前后的 cache hit 直接当作同一实验样本；
- 不在 CPA `prompt_cache_key` 未验证前给出正式缓存收益结论。

### 11.3 实验记录新增字段

```text
platform_version
plugin_version
plugin_contract_version
engine_version
request_schema_version
slide_id
slide_asset_revision
provider
model
prompt_cache_mode
prompt_cache_capability_verified
resolution_profile
context_window_tokens
visual_context_budget_tokens
```

---

## 12. 测试与质量门禁

### 12.1 Contract tests

- slide metadata 字段、单位和空值语义；
- level-0 bbox 边界裁剪；
- region 宽高比和 deterministic hash；
- legacy base64 → `RegionResult.bytes` 解码及字段归一化；
- binary region headers、hash 校验和缺失 header 拒绝；
- asset revision 不匹配 409；
- annotation 同 key/同 payload 幂等返回、同 key/异 payload 409；
- annotation revision CAS；
- change cursor、同行 tombstone、SSE envelope 和 reset-required；
- AbortSignal 与服务端取消；
- JWT scope 和组织隔离；
- access JWT 到期 exchange、run grant 到期和 permission revoke；
- launch ticket 过期/重放、UI token scope 和 HostBridge `auth.refresh`；
- 统一 error envelope、retryable 和 HTTP status 映射；
- manifest/contract/bridge 版本协商。

### 12.2 HistoPilot 回归

- 主会话、continue、lite fork、branch；
- pending snapshot 守卫；
- overview/detail/working 档位；
- request-local visual overflow；
- 稳定区与临时工作区；
- PreparedRequest retry 复用；
- prompt cache 降级；
- compaction 与 checkpoint CAS；
- 平台 annotation 409 后不错误 backfill。

### 12.3 PathTogether 回归

- 多租户权限；
- 病例成员与外部分享；
- 同一标注并发编辑；
- 人工/AI 标注筛选与审计；
- 插件安装、停用和权限撤销；
- 插件故障时 Viewer 降级；
- 旧项目、标注和分享数据迁移；
- owner/user/guest/sdk-user 权限矩阵；
- `case_grants` 权威授权与权限判定一致性；
- HostBridge request/response/event、超时、origin 校验和 viewport final flush。

### 12.4 端到端烟雾测试

至少覆盖：

1. 用户登录 PathTogether；
2. 打开病例和切片；
3. 启动 HistoPilot；
4. AI 获取概览并导航；
5. AI 创建标注；
6. 另一用户看到标注与 provenance；
7. 人工接受/驳回并评论；
8. 插件断线重连；
9. 切片 revision 变化时旧会话安全降级；
10. 停用插件后人工工作流继续可用。

---

## 13. 可观测性

### 13.1 统一关联键

所有平台与插件日志/指标统一携带：

```text
trace_id
case_id
slide_id
asset_revision
plugin_installation_id
plugin_session_id
plugin_run_id
user_id
```

不得记录 API Key、完整病理图 base64 或不必要的病例敏感文本。

### 13.2 平台指标

- Viewer/region 请求延迟和错误率；
- 活跃协作者、标注和评论量；
- revision conflict 次数；
- 插件安装/启动/失败率；
- 插件权限拒绝和 token 失败；
- outbox/cursor 延迟。

### 13.3 HistoPilot 指标

沿用并扩展当前结构化指标：

- request duration；
- region fetch/encode duration；
- derivative LRU hit/miss/bytes；
- visual tokens 与 overflow；
- context tokens 与 compaction；
- prompt cache mode/key/capability；
- provider cache usage（可获取时）；
- Agent steps、retry、cancel 和任务成功率；
- 标注创建、冲突与人工接受率。

---

## 14. 安全、隐私与审计

1. 插件默认最小权限，安装时显式授权。
2. 插件 token 短期有效，并绑定 user 授权和 installation。
3. region 能力必须检查用户和病例权限，不能因为是服务端调用而跳过资源鉴权。
4. 模型密钥使用 secret store 或 envelope encryption，不放入平台普通配置 JSON。
5. 对发送给模型的图片和文本建立数据出境/provider 策略。
6. 审计记录谁在何时用哪个插件和模型读取了哪张切片、创建了什么标注。
7. AI 生成内容在 UI 中明确标识，并支持人工接受、修改和驳回。
8. 删除用户/病例/插件时分别定义业务数据、AI 会话和缓存的保留周期。
9. 插件 UI 使用 CSP、sandbox iframe、严格 origin 校验和受限 postMessage 协议。
10. 第三方插件不得获得宿主 session cookie、数据库凭据或切片文件系统路径。

---

## 15. 风险与缓解措施

| 风险 | 影响 | 缓解 |
|---|---|---|
| 同时拆前后端、数据库和产品模型 | 回归范围失控 | 按 Stage 拆提交，每阶段保持兼容适配器 |
| `EXP-VISCTX-v1` 数据被架构迁移污染 | A/B 结论不可比较 | 固定 engine/contract/schema version，迁移前后分层统计 |
| 插件协议过度围绕 HistoPilot | 后续插件无法复用 | 用 capability 命名，增加非 AI 示例插件验收 |
| iframe 导致交互延迟或复杂度增加 | Viewer 体验下降 | 先同源 HostBridge，再切 sandbox；消息批处理 viewport 事件 |
| region 跨服务带宽增加 | 延迟和成本上升 | 二进制响应、就近部署、缓存、分档和 AbortSignal |
| 多租户引入越权风险 | 严重安全问题 | 资源级鉴权、scope token、跨租户自动化测试 |
| 旧 JSON 数据迁移失败 | 标注/分享丢失 | dry-run、校验报告、只读备份、幂等迁移和双读窗口 |
| 插件数据库与平台对象漂移 | 孤儿 session/错误标注 | 稳定 ID、asset revision、删除事件和定期 reconciliation |

---

## 16. 发布与回滚策略

### 16.1 Feature flags

建议新增：

```text
PLUGIN_HOST_ENABLED
HISTOPILOT_PLUGIN_UI_ENABLED
PLUGIN_API_V1_ENABLED
HISTOPILOT_EXTERNAL_SERVICE_ENABLED
COLLAB_DB_V2_ENABLED
```

### 16.2 双轨兼容

迁移期间：

- 旧 `/api/ai/*` 映射到新 HistoPilot 路由；
- 旧 `/internal/ai/*` 由 Legacy adapter 使用；
- 新插件 API 通过 feature flag 开启；
- 数据迁移先双读核对，再切主读；
- 切换后保留有限时间只读旧数据和回滚工具。

### 16.3 回滚原则

- 代码回滚不依赖反向数据迁移；
- schema 变更优先采用 expand-and-contract；
- 插件服务回滚不回退平台业务 schema；
- HistoPilot 新版本不能读取旧 session 时，应暂停旧 session 并提示迁移，禁止静默丢弃；
- 每个阶段单独发布，不把 `EXP-VISCTX-v1` 参数调整混入架构切换版本。

---

## 17. 里程碑与完成定义

### M1：边界清晰

- `PlatformClient` 落地；
- HistoPilot 核心无 Flask 私有语义；
- contract tests 通过；
- 现有功能无行为回退。

### M2：UI 可插拔

- AI UI 独立 bundle；
- 只通过 HostBridge 操作 Viewer；
- 插件可禁用且平台正常。

### M3：平台可协作

- 用户、组织、病例、评论和审计上线；
- PostgreSQL 成为业务数据主存储；
- 多用户并发和权限测试通过。

### M4：服务可独立

- HistoPilot 独立配置、数据库和部署；
- scoped token 与 `/api/plugin/v1` 生效；
- 无共享文件系统依赖。

### M5：协议可复用

- 发布 Plugin Contract/SDK；
- 非 HistoPilot 示例插件完成端到端验证；
- N/N-1 兼容策略自动化。

---

## 18. 推荐的首个实施批次

首批只做后端边界抽取，建议控制为一个可独立 review 的提交系列：

1. 新增 `PlatformClient` 类型和 contract fixtures；
2. 将现有 `FlaskClient` 包装为 `LegacyFlaskPlatformAdapter`；
3. tools、assembler、checkpoint 依赖改用接口类型；
4. 保持现有 Flask wire endpoint/payload、浏览器 SSE 和 session 文件不变；仅 adapter 内部转换为新 contract 类型；
5. 增加 adapter contract tests；
6. 全量测试与真实 Flask + sidecar 烟雾测试；
7. 文档状态从“设计提案”更新为“Stage 1 已实施”。

这个批次不做目录大搬迁、不改数据库、不改 UI，也不调整 `EXP-VISCTX-v1` 算法。完成后，后续拆分可以沿稳定接口逐步推进，而不会继续扩大当前耦合。

---

## 19. 已确认决策（2026-08-13 产品拍板）

以下 10 项已由产品负责人确认，取代此前的"待确认"状态。**与本表冲突的正文章节（§5.1/§5.4、Stage 3a/3c、Stage 2、Stage 4 凭据、§7.0 版本模型）以本表为准，待下一修订版本逐章同步。**

| # | 决策项 | 结论 | 对文档的影响 |
|---|---|---|---|
| 1 | 品牌拼写 | **PathTogether**（标准英文） | 全文已替换；包名/域名/schema 用 path-together 风格 |
| 2 | 身份与部署模型 | **一个部署实例 + 多用户协作，四类身份**：① owner = 部署者（管理平台 API 配置、公开切片、邀请）；② **user = 平台注册用户**（邮箱注册的正式账户，有持久会话/标注历史，可用平台配置的 API，也可自带 key）；③ guest = 游客（受邀链接直接进入，无注册，AI 必须自带 key，标注/评论按分享权限）；④ sdk-user = 插件访问身份（非自然人，插件经某 user 授权后以其身份调用 PlatformClient） | Stage 3a "身份与访问边界"：扁平四级模型（owner/user/guest/sdk-user），无 organization、无 roles 矩阵、无租户隔离 |
| 3 | 模型凭据 | **注册用户可用平台配置的官方 API；游客必须自带 key**。平台配置的官方托管 API（如 luna，便宜）是注册用户的常规 AI 来源之一（与自带 key 并列），不是"兜底"——平台未配置时注册用户也需自带 key | Stage 4 CredentialResolver：registered → PlatformKey 或 UserKey；guest → 必须 UserKey，无 key 则 AI 不可用但人工读片不受影响 |
| 4 | 病理图外发 | **允许，保持现状**（经 CPA 网关等外部 provider） | §4.3/§15 维持现状表述；无需阻断或默认关闭机制 |
| 5 | AI 审计 | **本产品不是病人管理软件，没有"病理审计档案"概念** | Stage 3c 的 audit 重新定义为**协作操作日志**（谁何时改了什么，服务于冲突解决与调试），不引入医疗审计语义；AI transcript 不进入任何正式档案，平台最多保存标注来源引用 |
| 6 | 会诊身份 | **不搞实名认证体系，邮箱注册封顶** | §5.4 分享：匿名链接继续支持 + 邮箱注册用户；不做实名/KYC |
| 7 | 实时协作 | **首版只做变更同步**（不做多人光标/同时编辑） | Stage 3c 范围收缩 |
| 8 | 数据库 | **PostgreSQL 唯一存储**（开源、单实例多用户并发写稳妥）。砍掉 SQLite 兼容层：双仓储实现是技术债，demo/正式部署统一走 PostgreSQL（Docker compose 一键起）；现有 shares.json 迁移 Postgres 而非保留双轨 | Stage 3b：只做 expand-and-contract 迁 PostgreSQL 单一实现，无 SQLite 适配器 |
| 9 | 插件 UI 隔离 | **直接 sandbox iframe**（跳过同源 bundle 过渡期） | Stage 2 改为直接 iframe；HostBridge 协议降级为 iframe 内 postMessage 通道，request/response/event 三类消息语义不变 |
| 10 | 版本策略 | **已被 2026-08-17 拆仓 ADR 取代：独立仓库、独立 SemVer 发布** | Plugin Contract 与 Bridge Protocol 继续独立版本化；PathTogether、HistoPilot、HistoPilot-DSH 各自发布，并维护 N/N-1 兼容矩阵 |

| 11 | Demo 模式 | **公开切片集 + 平台官方 API**：部署者提供公开 demo 切片；注册用户用平台官方 API 零配置体验读片；游客看 demo 切片仍需自带 key | 数据模型中需支持 slice visibility=public 与平台配置的官方 API key |
| 12 | 注册策略 | **开放注册开关归 owner**：默认（demo 场景）不开放自助注册，用户由 owner 手动添加（邮箱创建）；owner 可开启开放注册 | user 模型带 `registration_open` 平台级开关，默认 false；owner 手动创建用户流程（生成初始密码/邀请注册链接） |

上述决策已全部落地为本表约束，Stage 2+ 设计不再有产品层阻塞项。
