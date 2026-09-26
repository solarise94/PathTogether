# slide ID 化重构：部署与验证方案（供用户批准）

日期：2026-09-26。依据[实施任务书](slide-id-storage-refactor-agent-plan-20260925.md) §3.3/P7 与[迁移手册](slide-storage-migration-audit-runbook-20260925.md) §5/§6。

**边界声明**：本文档是方案与证据汇总。**本阶段未执行生产部署、生产数据审计/搬迁、生产停写；COS capability 保持 off**（CSP 三 origin/桶生命周期/Phase 5 池容量演练仍为独立门禁，不因本重构测试通过而豁免）。文中「生产执行」段落均为待批准步骤。

## 1. 两仓提交对应（验收态）

| 仓 | 分支 | 提交区间（基线→终态） | 关键节点 |
|---|---|---|---|
| PathTogether | slide-id-refactor | a2b0ae3 → **4891744** | P0=3a60933 P1-A=e2df401 P1-B1=8a0447d P1-B2=deaece4 P2 后端=70f4893 补丁=e68b398/c1389b5 前端=8a724c8 收口=b972ea5 P3=dd582dc P4-c=9b86b4f P4-b=7c54d00 P4-app=9a79ca1 P5=613cd54 P6-1=66b6e18 P6-2=4891744 |
| HistoPilot | slide-id-refactor | 26fd2a9 → **4598172** | 运行通道=a07850c 双字段+契约=f78f7c3 P3 适配=ed8a30f P6-2 适配=4598172 |

**部署顺序约束（硬）**：PT 与 HP 必须同批发布（HP 4598172 的契约断言依赖 PT P6-2 的 layout 门禁与 revision 统一）；**PT 先行、HP 随后**（HP 旧版对新 PT 的兼容面未验收——HP 会话按 ID 寻址依赖 PT 的 ID 端点族）。两仓镜像摘要与迁移版本号的对应关系在生产发布单上逐行写死，不以「滚动更新即可」代替（手册 §5）。

## 2. Schema 与迁移版本

增量迁移序列（均幂等，IF NOT EXISTS；对已应用库重跑 no-op）：

| 迁移 | 内容 | 引入 |
|---|---|---|
| 0067_slide_asset_identity | slides 八列+CHECK+uq_slides_storage_relpath；任务表 slide_id；upload_task_items/share_slides/slide_delete_jobs；关系表 slide_id 列 | P1-A |
| 0068_upload_publish_intent | upload_tasks.commit_intent_json | P3 |
| 0069_conversion_slide_id_publish | DROP idx_conversion_jobs_canonical_live + uq_baidu_import_items_slide_id（裁决见交付日志 P4 偏差#3）；conversion_jobs.commit_intent_json | P4-app |
| 0070_slide_delete_jobs_lease | slide_delete_jobs lease 列+调度索引 | P5 |

**数据回填（生产执行步骤，先干后开）**：`scripts/backfill_slide_asset_state.py`（dry-run 默认；`--apply` 分批；DB 状态即检查点）——legacy→ready（文件在+owner 明）/→failed（文件缺）/保持 legacy+manual_review（owner 不明）。**在任何运行时切换前完成并复核 manual_review 清单**。

## 3. 上线顺序（手册 §5 硬门禁，逐条对应到本方案步骤）

```text
0. 在线预审（**不停写**，read-only）：scripts/audit_slide_identity.py
   （在线浅审模式）→ 问题清单/隔离决议草案（incomplete=0 才继续）。
1. 停写（用户 API + 全部后台 writer：上传/转换/百度/COS/删除执行器与
   清理 sweep 全部暂停；部署单逐 worker 写明停法与验证命令）+
   一致备份（DB dump + UPLOAD_DIR 快照；备份容量与用户配额分列记账）。
   停写验证：各 writer 无在途任务（active/queued 清零或显式冻结记录）。
2. 部署「兼容维护版本」= 本分支 PT+HP（含 0067–0071 schema、回填脚本、
   迁移工具链）。**维护窗口内不开放读写**：服务可起（只读管理面），但
   writer 保持停用直至第 7 步——本分支运行时只认 id_bundle（P6-2），
   未迁移 legacy 资产在迁移完成前不可见，这是设计要求（不猜测认领）。
3. 数据回填（逻辑状态）：scripts/backfill_slide_asset_state.py
   （dry-run 复核 → --apply 分批；manual_review 清单逐条决议）。
4. 最终冻结审计：audit --mode frozen（停写态下的一致快照；incomplete=0
   且阻断项为零或逐项有隔离决议）→ plan_slide_migration.py --env prod
   （计划头 digest 写入发布单）。
5. 物理迁移：migrate_slide_storage.py --apply --plan-digest <digest>
   --env prod --quiesce-proof <停写证据>（分批；journal 持久；崩溃重跑
   幂等；冻结伴侣逐文件清单比对——R6 审查修复问题 4 的口径）。
6. 独立终验：verify_slide_migration.py（退出码 0 且 incomplete=否；
   配额差额须精确归因或持 --quota-approvals 核准凭据——R6 修复问题 5）+
   手册 §5 的 8 条硬门禁逐项签字（含真实 MRXS 样本试开——演练用的是
   进程内 stub，生产终审必须用真实厂商样本，交付日志 P6-1 偏差#1）。
7. 开放读写（先读端验证：列表/分享/授权/Demo/插件/AI 各至少正负一例；
   旧名重传不继承旧权限抽查；然后逐 writer 解封并回归其健康检查）。
```

**中断恢复口径**：migrate 任一点中断 → 重跑（journal 五态续判）；开放读写后发现的迁移遗留 → 回到维护状态按 journal 处置，不得对有歧义资产恢复旧默认可见（手册 §6）。

## 4. 回滚边界与最低可回滚版本

- **新 writer 开放前**：可回滚到「理解 0067–0070 schema 且仍服务 legacy 布局的上一个生产版本」——即本分支**之前**的生产镜像 + 按需按 journal 恢复旧存储映射；schema 增量列对旧版本透明（additive；DROP 的两个索引在旧版本语义下不再需要——旧版本不使用它们做拒绝判定以外的用途，回滚前评审 0069 对旧版本的兼容：旧版本 canonical 名锁行为依赖 idx_conversion_jobs_canonical_live，**回滚到旧版本前须重建该索引**（DDL 在 0047；写入部署单回滚节）。
- **新 writer 开放后**：只能回滚到理解 slide_id/id_bundle 的版本（即本分支及以后），或停写前滚修复；不得把新资产改回原名布局。
- 恢复点之后的新写入保全：不得整库覆盖回旧 dump（手册 §6）。

## 5. 退役/清理执行清单（生产批准后按序做，均非本阶段已执行）

1. 旧平铺源文件清理：迁移 postverified 全绿且保留期满 → 独立可审查 manifest（逐项文件+sha+迁移 journal 引用）→ 专人执行；**禁止对旧根目录递归删除**（手册 §4）。
2. kfb 派生物（`.manifest.json`/`.associated`）与 `invalidate_by_slide_id(legacy_canonical=)` 兼容参数：随旧转换任务行排空后删除（P6-2 遗留#3）。
3. `shares.slides` JSONB 快照列：全部调用者确认消费 share_slides 后独立 contract migration（先停写快照、审计一致、再 DROP——不得提前 drop 破坏回滚，手册 §6）。
4. `set_slide_meta` 懒建 legacy 行的残留登记通道（demo 目录 PUT / admin visibility 收录）：随 demo 目录强校验收口（P6-2 遗留#1）。
5. `scripts/seed_demo_tcga_catalog.py` 平铺播种：切迁移工具或 objects 播种（P6-2 遗留#2）。
6. `migrate_json_to_pg.py::_det_slide_id` 与 JSON 迁移层整体退役（P4-app 已标注）。

## 6. 本阶段已完成的证据索引

- 验收矩阵执行记录：docs/slide-id-refactor-acceptance-matrix-20260926.md（17 行全覆盖、门禁命令与计数、排除原因）。
- 阶段交付/裁决/遗留总账：docs/slide-id-refactor-delivery-log-20260925.md（P0–P6 全部提交、偏差裁决表、review 修复 F0–F3、门禁轨迹）。
- 迁移演练证据：docs/drill-evidence-20260925/（plan/journal/verification/summary/演练报告；编排方独立重跑 40/40 复现）。
- 只读审计/回填/迁移/核验工具：scripts/audit_slide_identity.py、backfill_slide_asset_state.py、plan|migrate|verify_slide_migration.py（全部 dry-run 默认、确定性、不删源、不动授权）。

## 7. 明确未做（防止状态混报）

- 未执行生产审计/生产迁移/生产停写；工具完成、演练通过、生产迁移完成是三个不同状态——当前处于「工具完成 + 副本演练通过」。
- 未打开 COS capability；未动桶/CSP/费用。
- 未在真实 MRXS 厂商样本上试开（演练 stub）——列入 §3 第 5 步硬门禁。
- 未跑 Playwright 全量 e2e（环境排除，原因见验收矩阵 §1 口径）。
