# P5 设计合同：统一删除、回收与资源账本

日期：2026-09-26。配套[实施任务书](slide-id-storage-refactor-agent-plan-20260925.md) §3.3/P5 与 [迁移手册](slide-storage-migration-audit-runbook-20260925.md) §2.3（容量项）。
前置：P3（id_bundle 删除最小版）+ P4（全部 writer 切统一发布）。

## 1. 范围

1. **删除任务持久化**：`slide_delete_jobs`（0067 已建）成为删除的唯一执行载体。`request_delete`（ready→deleting）与任务落库同一事务；物理清理由执行器完成；清理成功后一次性结算（mark_deleted + refund_used_bytes_locked 同事务，幂等键=CAS 本身）。
2. **执行器**：app.py 后台 daemon 线程（参照现有 `ai-binding-attach-retry` 模式）周期性领取 pending/failed 任务（lease + 退避）；DELETE 端点在落库后可同步尝试执行一次（用户感知不变），失败/中断由 daemon 兜底重试。**重复执行/worker 重启不得重复减账、不得重复清理别人**。
3. **两种布局的物理清理**：
   - id_bundle：`slide_storage.remove_bundle`（已就绪）。
   - legacy：`UPLOAD_DIR/<legacy_filename>` unlink + MRXS 伴侣目录 + `<name>.manifest.json`/`<name>.associated/` sidecar + 转换联动（`_cleanup_conversion_sidecars` 语义保留——作废关联 conversion 任务+清源 KFB 别名）；新 writer 永不重用 legacy 名路径（tombstone 保留 legacy_filename，授权映射随之冻结）。
4. **取消路径统一**：上传取消（V1/V2）、COS 取消、转换取消、过期 sweep 的 staging 清理全部经 slide_storage/remove_staging_tree；staging 资产行 → failed；reservation 在清理确认后释放。
5. **孤儿扫描只隔离报告**：admin inventory 的 orphan_files 已只报告（P1-B2）；新增对 `objects/` 的孤儿扫描（无 slides 行或非 deleting/deleted 状态的目录 → 报告，不按名猜 owner、不自动删）；`.staging/` 残留（无活任务）→ 报告+可清理（任务键可判）。
6. **删除失效覆盖面**（计划 P5 完成标准）：分享（share_slides 成员行）、显式授权（slide_view_grants）、Demo（catalog 撤销+预算释放）、插件/run grants、AI session、缓存（句柄/tile/render 统计按 ID 失效）、研究（伪名不动，研究删除按 user 维度已有）、HistoPilot 会话（旧会话/回放映射不存在或资产删除 → 明确失效，不绑定新同名内容——HP 侧已有冻结语义，核对即可）。
7. **legacy 资产删除的账本口径**：维持 R-12 过渡裁决——**不回退 used_bytes**（迁移演练后统一；refund 只对 id_bundle 资产）。deleted_at/tombstone 同样落。
8. **删除端点归一**：`DELETE /api/slides/<slide_id>`（权威）与 `DELETE /api/slide/<name>`（legacy alias 解析进同一编排）共用同一 request_delete + 任务落库 + 执行器路径——P3 删除端点的同步内联执行改为「落库 + 同步尝试 + daemon 兜底」。

## 2. 状态机与幂等（把 P3 最小版升级为持久化）

```text
ready ──request_delete(+job 落库，同事务)──▶ deleting ──执行器：清理成功──▶（结算事务：mark_deleted CAS + refund）──▶ deleted
                                                   │
                          清理失败/崩溃 → 停留 deleting，job 退避重试；
                          重复 DELETE：deleted → 幂等 200；deleting → 触发/等待执行器
```

- 结算幂等键 = deleting→deleted CAS（P3 已证）。job 表记录 attempts/last_error/lease；`(slide_id)` UNIQUE 防重复任务。
- 授权联动清理（view grants/share_slides/demo/run grants/缓存 evict）在 request_delete 同事务或执行器清理前完成——**可重入**（重复执行无副作用）。
- 用户配额口径：used_bytes 只经 refund_used_bytes_locked（GREATEST 防负）；COS 池预约不经本路径（池释放在远端清理确认后——保持 0066 语义）。

## 3. 测试（计划 P5 完成标准 + §8 矩阵删除行）

- 同名重传新 ID 与旧删除互不影响（删除 A 中 B 传，B 不受 A 的清理影响）。
- 删除重放/执行器重启/重复调用：物理清理与配额释放各一次（DB deleting 门禁持续生效）。
- 保留文件始终有账本责任（failed 任务的 staging 有 reservation 或实占记录；孤儿 objects 目录被报告）。
- legacy 资产删除：文件+伴侣+sidecar 清理、tombstone 保留别名、授权冻结、不减账（R-12）。
- 删除失效覆盖七类入口的正反用例（含 HP 会话失效的 contract 侧核对）。
- daemon 领取并发两实例不重复执行同一 job（lease/UNIQUE 约束）。

## 4. 门禁

全量 pytest（先清 /tmp 残渣）仅允许已知无关失败；test:js 全绿；HP contract 复跑全绿（删除失效语义跨仓验证）。
