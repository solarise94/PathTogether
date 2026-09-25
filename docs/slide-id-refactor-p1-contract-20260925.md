# P1 设计合同：schema 0067、统一 resolver 与状态门禁

日期：2026-09-25。配套[实施任务书](slide-id-storage-refactor-agent-plan-20260925.md) §2、§3 与 [P0 盘点](slide-id-refactor-p0-inventory-20260925.md)。
本文是 P1 阶段的实现合同：实现 Agent 必须将本文状态机、锁顺序、接口语义镜像为模块 docstring（代码旁合同），偏差需在 review 中显式裁决。

## 1. 身份与 ID

- slide_id 继续用现行生成器 `"sld_" + secrets.token_urlsafe(9)`（`share_store_pg.py:1834`），服务端随机、DB 主键兜底唯一；不从原名/owner/内容哈希推导；不重置旧 ID。
- 一次新的逻辑切片上传 = 一个新 slide_id；幂等重试复用原任务及其 ID；删除后重传用新 ID。
- 文件名（original_filename/display_name）只是展示；`legacy_filename` 是冻结别名：新资产恒 NULL；已删除资产的 tombstone 行**保留** legacy_filename（UNIQUE 约束天然阻止旧别名重绑新 ID）。

## 2. 迁移 0067（`migrations/0067_slide_asset_identity.sql`）

遵守 pg_store 机制约定：单文件单事务、幂等（IF NOT EXISTS / DO 判存）、`NNNN_描述.sql`。

### 2.1 slides 扩展

| 新列 | 类型/约束 | 语义 |
|---|---|---|
| `original_filename` | TEXT 可空 | 原始 basename 展示快照（新资产 NOT NULL 由应用层保证；旧行待回填） |
| `storage_layout` | TEXT NOT NULL DEFAULT 'legacy'，CHECK IN ('legacy','id_bundle') | 只有 resolver 理解两种布局 |
| `storage_relpath` | TEXT 可空 + 部分唯一索引 `WHERE NOT NULL` | 服务端生成的包入口相对路径；唯一且不可变；客户端不可设置 |
| `format_ext` | TEXT 可空 | 实际逻辑格式（白名单小写扩展名），不从展示名推导 |
| `asset_state` | TEXT NOT NULL DEFAULT 'legacy'，CHECK IN ('staging','legacy','ready','deleting','deleted','failed') | 见 §4 状态机；`legacy`=待验证历史资产 |
| `published_at` / `deleted_at` | TIMESTAMPTZ 可空 | 发布/删除审计时间 |
| `accounted_bytes` | BIGINT 可空 CHECK >=0 | 本地已计费物理字节稳定值；与 ready 发布同事务设置；删除结算用它 |

索引：`idx_slides_owner_user_id (owner_user_id)`、`idx_slides_asset_state (asset_state)`。

### 2.2 任务表显式 slide_id

- `upload_tasks ADD COLUMN slide_id TEXT`（可空，引用 slides(slide_id)）；单切片任务直接绑定。
- 批量任务新表 `upload_task_items (task_id TEXT NOT NULL, item_key TEXT NOT NULL, slide_id TEXT NOT NULL UNIQUE, PRIMARY KEY (task_id, item_key))`——"同一任务、同一逻辑 item"的库层唯一约束；V1 ZIP 每个逻辑切片一行。slide_id 全局 UNIQUE 保证一个资产只属一个任务项。
- `ingestion_jobs ADD COLUMN slide_id TEXT` + 部分唯一索引 `WHERE NOT NULL`。
- `conversion_jobs ADD COLUMN slide_id TEXT` + 部分唯一索引 `WHERE NOT NULL`（产物预分配 ID）；`conversion_job_sources ADD COLUMN source_slide_id TEXT`（源切片 ID 关联，P4 用）。
- `baidu_import_items ADD COLUMN slide_id TEXT` + 部分唯一索引 `WHERE NOT NULL`。

### 2.3 关系表 ID 化

- 新建 `share_slides (token TEXT NOT NULL REFERENCES shares(token) ON DELETE CASCADE, slide_id TEXT NOT NULL REFERENCES slides(slide_id), position INTEGER NOT NULL DEFAULT 0, PRIMARY KEY (token, slide_id))`；`shares.slides` JSONB 保留为兼容快照，不再参与授权判定。
- `slide_view_grants`：已有 slide_id 列；加部分唯一索引 `(slide_id, user_id) WHERE slide_id IS NOT NULL`；slide_name 保留为历史快照。
- `project_slides ADD COLUMN slide_id TEXT` + 部分唯一索引 `(project_id, slide_id) WHERE slide_id IS NOT NULL`；旧 `slide` 文本列保留为快照。
- `rois / comments / change_log / run_grants / ai_session_principals / annotation_access_events / audit_events` 各 `ADD COLUMN slide_id TEXT`（可空）+ 相应索引（rois: `(slide_id, insert_seq)`；change_log: `(slide_id, seq)`；其余按现有查询形态）。历史行回填不到可信 ID 的保持 NULL = unresolved。

### 2.4 删除任务表（P5 用，schema 先行）

`slide_delete_jobs (job_id TEXT PRIMARY KEY, slide_id TEXT NOT NULL UNIQUE, requested_by TEXT, state TEXT NOT NULL DEFAULT 'pending' CHECK IN ('pending','cleaning','done','failed'), attempts INTEGER NOT NULL DEFAULT 0, last_error TEXT, created_at TIMESTAMPTZ NOT NULL DEFAULT now(), updated_at TIMESTAMPTZ NOT NULL DEFAULT now())`。

### 2.5 不做的事

不在 schema migration 中搬动文件、不调用云服务、不回填业务数据（回填是单独可重跑脚本，见 §6）。

## 3. 统一接口（新模块）

新增三个模块，worker 可 import（不得 import app）：

### 3.1 `slide_store.py`（PG 资产状态，权威）

```python
class SlideState:  # 'staging' | 'legacy' | 'ready' | 'deleting' | 'deleted' | 'failed'
    ...

@dataclass(frozen=True)
class SlideDescriptor:
    slide_id: str
    owner_user_id: str | None
    original_filename: str | None
    display_name: str
    legacy_filename: str | None      # 冻结别名，仅兼容解析用
    format_ext: str | None
    asset_state: str
    storage_layout: str              # 'legacy' | 'id_bundle'
    storage_relpath: str | None      # 仅内部使用，绝不返回浏览器
    accounted_bytes: int | None
    public: bool
    note: str
    published_at: ... | None
    deleted_at: ... | None
    revision: str | None             # 内容 revision（render token/快照 attestation 用）
```

- `allocate_slide(owner_user_id, original_filename, format_ext, *, conn/txn)` → 创建 `asset_state='staging'`、`storage_layout='id_bundle'`、`storage_relpath='objects/<slide_id>/...'` 的行；**不查原名是否已存在**；owner 必须显式（无 UID 的本地模式由调用方先解析配置 owner，不允许空 owner 自动认领）。
- `resolve_slide_id(slide_id)` → Descriptor | None：**校验存在性**，缺失不凭 `sld_` 前缀当成功。
- `resolve_legacy_alias(alias)` → Descriptor | None：仅查 `legacy_filename` 冻结映射，再进入同一 resolver 语义；无文件系统猜测。
- `authorize_read(desc, actor/capability)` → bool/raise：统一门禁 = `asset_state == 'ready'` **且**（owner / public / slide_id 级 view grant / share_slides 成员经已领取 grant / demo capability / admin）。staging/legacy/deleting/deleted/failed 一律不可读；DB 异常按拒绝处理，不回退目录扫描。
- `publish_ready(...)` / `request_delete(...)` / `mark_deleted(...)` / `mark_failed(...)` 等状态迁移原语，全部带 `expected_state` 谓词的真实 SQL CAS。
- 名称编辑：`update_display_name(slide_id, display_name)` / `update_note(...)` / `set_public(...)`——只动元数据，不动文件、不动 legacy_filename、不动授权。

### 3.2 `slide_storage.py`（安全路径/包操作）

- `staging_dir(task_id, generation)` → `UPLOAD_DIR/.staging/<task_id>/<generation>/`（不提供静态访问）。
- `bundle_dir(slide_id)` → `UPLOAD_DIR/objects/<slide_id>/`；`entry_relpath(slide_id, format_ext)` 等路径派生全部服务端侧，无用户可控片段。
- `resolve_descriptor_path(desc)` → 绝对路径：id_bundle → objects 下 entry；legacy → `UPLOAD_DIR / legacy_filename`（**仅过渡**，注释标注退役条件）。containment 校验：解析后必须位于 UPLOAD_DIR 内，拒绝 `..`/绝对路径/符号链接逃逸。
- `publish_bundle_no_clobber(staging, slide_id, manifest)`：完整包 no-clobber 发布（目标已存在即 FileExistsError，绝不覆盖）；同卷优先原子 rename；跨卷先目标卷私有暂存+完整复制校验再发布；按耐久合同 fsync 文件与相关目录。
- `remove_bundle(slide_id)`：仅清理该 ID 的独占目录；绝不按显示名扫描删除。
- manifest 内相对路径同样做 containment 验证。

### 3.3 `slide_publish.py`（薄编排，P3 起接 writer）

`publish_slide(task_ref, generation, slide_id, manifest, …)`：验证文件/任务/owner → intent → 锁 → 发布 → 短事务 CAS+结算。P1 只落模块骨架与合同注释，真实接线在 P3/P4。

## 4. 状态机与可见性

```text
allocate_slide
    │
    ▼
 staging ──publish 成功──▶ ready ──request_delete──▶ deleting ──清理+结算完成──▶ deleted
    │                      │                            │
    ├─任务失败/取消─▶ failed│                            └─清理失败：停留 deleting 重试（slide_delete_jobs）
    │   （staging 清理后行可删或留 failed 证据） │
legacy（0067 默认）──盘点/验证通过──▶ ready（layout 仍 'legacy'，P6 再迁 id_bundle）
legacy ──验证失败（缺文件/归属歧义）──▶ failed（保留证据，不可读）
```

- **DB `asset_state='ready'` 是唯一可见性开关**；所有读取入口（含旧端点经 legacy alias 解析后）同受此门禁。
- `deleting` 立即拒绝新读取授权；已取得的受控文件句柄可完成当次读取。
- 不允许同一 slide_id 覆盖内容；换内容 = 新 slide_id。

## 5. 锁顺序（全仓统一，写模块注释）

既有口径（0066）：`ingestion_jobs 行 → upload_reservations 行 → upload_user_quotas 行 → cos_pool_state 行`。
本重构扩展为：

```text
pg_advisory_xact_lock(hashtext('slide:' || slide_id))   ← 所有 publish/delete/recovery 的第一把锁（跨进程仲裁点；'slide:' 前缀与既有 hashtext(job_id) 键空间隔离）
  → 任务行锁（upload_tasks / ingestion_jobs / conversion_jobs 行，SELECT ... FOR UPDATE）
  → slides 行锁
  → upload_reservations 行
  → upload_user_quotas 行
  → cos_pool_state 行
```

已审计无环：既有 `_pg_finish_commit`/`worker_settle_ready` 均为 任务行→reservation→quota(→pool)，slides 行锁插入在任务行之后不引入反向边；delete 路径只持 advisory+slides 行，不回头取任务锁。任何新代码取多把锁必须按此顺序；禁止"stat 相同就 unlink/转 owner"。

## 6. 旧数据回填（P1 内，独立于 schema migration）

`scripts/backfill_slide_asset_state.py`（可重跑、带 checkpoint）：
1. 对 `asset_state='legacy'` 的行：文件存在 + owner 明确 → `ready`（layout 保持 legacy，published_at 回填为文件 mtime 或 now）；文件缺失 → `failed` + issue 记录；owner 空/矛盾 → 保持 legacy + manual_review issue（不回落认领平台 owner）。
2. 回填 `original_filename = legacy_filename`、`display_name`（规则：非空 alias → display_name，否则现有 display_name，否则 legacy_filename）、`format_ext`（白名单后缀）、`accounted_bytes`（按文件实际字节；多文件包含伴侣）。
3. 关系回填（可信部分）：`project_slides.slide_id` / `rois.slide_id` / `comments.slide_id` / `change_log.slide_id` / `run_grants.slide_id` / `ai_session_principals.slide_id` 按当前 legacy 名→slide_id 映射唯一确定的行；`slide_view_grants.slide_id` 既有列沿用；`shares.slides` → `share_slides` 逐 token 映射（仅映射到已确认旧 ID）。
4. 全部回填幂等、分批（--batch-size）、记录 checkpoint（落 DB 表或 checkpoint 文件），中断可续。
5. 该脚本只读文件系统 stat/哈希，不移动文件（物理迁移是 P6）。

## 7. P1 API 面

- 新路由挂 `/api/slides/<slide_id>/...`（实现前检查既有 Flask 路由冲突——`/api/slide/<name>/...` 单数旧路由群已存在，注意区分）。
- `GET /api/slides` 集合返回独立字段 `slide_id / original_filename / display_name / format_ext`（P2 起前端切 ID；P1 阶段旧 `/api/slides` 行为不变，ID 字段先以附加字段出现）。
- 旧端点（`/api/slide/<name>/...` 全部读取路由）在 P1 改为：`_safe_name` → `resolve_legacy_alias` → `authorize_read` → descriptor 路径读取；行为对可信迁移夹具保持不变。
- descriptor 的路径字段绝不序列化进 HTTP 响应。

## 8. 兼容与退出条件

- `set_slide_meta(name)` 自动建行、`resolve_slide_ref` 前缀直返：P1 起仅迁移兼容层使用，加 deprecation 注释；正常新读写走 ID。
- `alias` 列停写；过渡 API 输出的 `alias` 仅从 display_name 派生；旧 PATCH alias 入参映射到 display_name。
- `slide_assets.content_sha256` 保留列不动；新发布的原文件哈希写新列 `file_sha256`（若需要——P3 裁决，需要时随 P3 迁移号新增）。
