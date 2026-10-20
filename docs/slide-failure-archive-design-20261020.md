# 失败切片「归档」设计（把已物理清理的失败墓碑移出失败列表）

日期：2026-10-20
来源：用户评审 §四「用户切片 filter 与清理」（`review-for-opus.txt` §四）
状态：**设计，未实现**（本轮只做显示过滤/失败历史归档的显示侧；无数据维护、
无迁移、无 DELETE、无归档列）。执行前须另开可审阅的数据维护变更。

## 1. 问题

管理台切片清单（`GET /api/admin/v1/slides/inventory`）默认视图按
`asset_state` 呈现。2026-10-20 生产只读快照（`production-evidence.json`）：

| asset_state | storage_layout | n |
| --- | --- | --- |
| ready | id_bundle | 119 |
| deleted | id_bundle | 101 |
| failed | id_bundle | 13 |
| failed | legacy | 16 |
| staging | id_bundle | 1 |

失败档 29 行里有两类完全不同的东西：

1. **16 条 legacy**：入口文件全不存在，无当前任务链证据（只读核对见
   `cleanup-candidates.json`）；后端按现状推断 `missing_file` + `inferred=true`
   （UI 文案「历史文件缺失（推断）」，不声称已查明最初消失原因）。其中 8 条有
   rois、2 条有 share 成员（去重共 9 条有关联），**不能当空行批量硬删**。
2. **13 条 id_bundle**：入口与 bundle 均不存在，`accounted_bytes` 为空，关联
   ingestion 全为终态，本地/远端 `cleanup_status` 均为 cleaned，无 rois / 无
   share 成员。失败码：`validation_failed` 7、`cancelled_by_user` 3、
   `convert_in_browser` 1、`abandoned_no_client` 1、`client_idle_timeout` 1。
   这批**已完成物理清理**，现在主要是「管理清单仍显示失败墓碑」。

用户结论（§四 第一步）：**只做显示过滤/失败历史归档**。这批失败没有入口文件
可释放，再删数据库行不会回收切片磁盘空间；要「移出失败列表」必须用**显式归档
/筛选元数据**，不能把 failed 伪装成用户主动 deleted，也不得用删除用户/改分类
来隐藏记录。

本轮已落地的部分（无迁移）：服务端状态筛选 + 计数（`status=not_deleted /
viewable / processing / failed / deleted / all`）与前端筛选控件——失败档仍然
显示全部 29 行。**把 13 条移出失败档需要下面这份设计。**

## 2. 为什么需要一个显式归档标记

- `asset_state` 是**生命周期状态机**（`staging → ready → deleting → deleted`，
  `failed` 是终态证据位，0067 的 CHECK 固定词表）。把 13 条改成 `deleted`
  等于宣称「用户删除了它」——归因错误：`deleted` 承载用户删除语义
  （tombstone、别名不可重绑、配额结算、审计、旧分享/别名约束），而这 13 条
  的失败码里有 7 条 `validation_failed`、3 条 `cancelled_by_user`，与「用户
  主动删除」不是一回事。
- 反过来把它们从库里 DELETE 也不行：会丢掉失败原因、来源任务与用户关联
  （审计/回溯价值），且不释放任何磁盘空间（文件早已清理）。
- 因此需要第三种事实：**「这条失败记录已经了结、归档留存」**——它与
  `deleted` 正交，可逆、可审计、只影响管理清单的默认呈现。

## 3. 方案（推荐）：独立的归档记录表 + 清单过滤元数据

### 3.1 迁移（additive，不改既有列/词表）

```sql
-- 0085_slide_archive_records.sql（示意；编号以落地时的最新迁移为准）
CREATE TABLE IF NOT EXISTS slide_archive_records (
    slide_id      text PRIMARY KEY REFERENCES slides(slide_id) ON DELETE RESTRICT,
    archived_at   timestamptz NOT NULL DEFAULT now(),
    archived_by   text,              -- 操作者 user_id（审计同源）
    archive_reason text NOT NULL,    -- 词表见下（CHECK）
    archive_note  text,              -- 逐条理由（如「物理清理已完成，无入口文件」）
    evidence      jsonb NOT NULL DEFAULT '{}'::jsonb,
    -- 归档当时的只读证据快照：asset_state/storage_layout/入口与 bundle 存在性/
    -- cleanup_status/失败码与来源任务引用/关联计数（rois/share/annotations…）
    CHECK (archive_reason IN ('cleaned_no_entry','legacy_missing_entry',
                              'superseded','other')),
    CHECK (jsonb_typeof(evidence) = 'object')
);
CREATE INDEX IF NOT EXISTS slide_archive_records_reason_idx
    ON slide_archive_records (archive_reason, archived_at DESC);
```

- `ON DELETE RESTRICT`：归档记录不会因为删切片行被悄悄带走（本设计也不删行）。
- 不新增 `slides` 列、不改 `asset_state` 词表 → 现有读门禁
  （`authorize_read` 只认 ready+id_bundle）、删除编排、配额结算、
  别名冻结与审计语义**零改动**。
- 为什么不用 `slides.archive_state` 列：列会被迫进状态机与 `_DESCRIPTOR_SQL`
  投影，任何 future 的 CAS/回填都要同时改两处；而归档是**清单策展事实**，
  与资产生命周期正交。代价是查询要多一个 LEFT JOIN（清单规模 <1e4，可忽略；
  且服务端本来就把全量 descriptor 读进内存）。

### 3.2 清单语义（`/api/admin/v1/slides/inventory`）

| 视图（status） | 归档行是否出现 |
| --- | --- |
| `not_deleted`（默认） | **不出现**（归档 = 已了结的历史失败，不再占默认版面） |
| `viewable` / `processing` / `deleted` | 不出现（与归档正交，状态不符） |
| `failed` | **默认不出现**；带 `archived=include` 时才出现（并标注「已归档」） |
| 新增 `archived_failed` | 只出现归档的失败行（历史留档视图） |
| `all` | 出现（并在行上标注 `archived=true`、`archived_at`、`archive_reason`） |

- `status_counts` 新增 `archived_failed` 键；`failed` 计数**只计未归档**失败
  （与列表同口径，避免「计数 29 / 列表 16」的错位）。
- 行上新增只读字段：`archived`（bool）、`archived_at`、`archive_reason`
  （白名单渲染，textContent）。
- 未注册文件、deleted 墓碑、servable 判定、temporary_view 状态机不变。

### 3.3 语义边界（红线）

- **归档 ≠ 用户删除**：不改 `asset_state`，不动 `deleted_at`，不触发任何配额
  结算；用户侧可见性/旧分享/别名约束/删除幂等完全不变（这些行本来就不可读）。
- **保留全部关联**：失败原因（`fail_code`/`fail_detail` 原值）、来源任务引用
  （ingestion/conversion/baidu/upload 行）、用户关联（`owner_user_id`）、
  审计行一律不动；归档记录里的 `evidence` 只是**快照**，不是新的事实来源。
- **可逆**：撤销归档 = 删除对应 `slide_archive_records` 行（写审计），行即回到
  失败档；不涉及数据搬迁。
- **不清理**：本设计不含任何文件删除、配额回收、`rm -rf`、`DELETE FROM slides`。

## 4. 落地步骤（另一个可审阅的数据维护变更）

1. **逐 ID dry-run 清单**（离线脚本，只读）：每条列当前状态、来源任务与终态、
   物理路径/大小/`cleanup_status`、配额结算状态、标注/分享/会话/研究等引用、
   计划动作（`archive` / `keep` / `needs_human`）。**必须先完成缺失关联盘点**
   （生产只读核对已指出 legacy 16 条里有 9 条有关联 → 这批先 `keep`）。
2. **锁内复核**（每条一个事务）：`asset_state` 仍为 `failed`、无活跃任务/租约/
   未 commit 的迁移、无恢复价值（入口与 bundle 均不存在）、关联计数与 dry-run
   一致；不一致即跳过并报告。
3. **写归档记录**：`INSERT INTO slide_archive_records ...`（`ON CONFLICT DO
   NOTHING`，幂等）+ 同事务写 `audit_events`
   （action=`admin.slide.archive`，target=slide_id，detail=reason/evidence 摘要）。
4. **验收**：`failed` 档计数从 29 → 16（仅 legacy），`archived_failed` = 13；
   `not_deleted` 计数不变（归档行本来就在 failed 档内）；deleted 墓碑与别名
   约束不变；temporary-view/删除端点对归档行仍按 `failed` 语义 409。
5. **回滚**：删除归档记录（写审计）；不重建任何文件。

## 5. 未决问题（留待评审）

- legacy 16 条的处置：其中 9 条有关联（rois/share），是否也归档？建议**先
  保留在失败档**（关联盘点未完成），或单独用 `legacy_missing_entry` 归档但
  先完成关联迁移决策。
- 归档是否需要「到期自动清理」？本设计不做（不删除任何行）。
- 是否需要用户可见的说明？不需要——这些行本来就不可读，且不属于用户删除。

## 6. 本轮实现范围（对照）

| 项 | 本轮 | 本设计 |
| --- | --- | --- |
| 服务端状态筛选 + 计数（分页前） | ✅ 已实现 | 复用 |
| 未注册文件独立展示 | ✅ 已实现 | 复用 |
| 「历史文件缺失（推断）」文案 | ✅ 已实现 | 复用 |
| 归档记录表 + 迁移 | ❌ 未做 | 本文 §3.1 |
| 把 13 条移出失败档 | ❌ 未做（仍显示） | 本文 §4 |
| 删除/清理任何数据 | ❌ 未做（且不在本设计内） | — |
