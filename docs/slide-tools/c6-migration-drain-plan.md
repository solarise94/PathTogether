# C6 转换链路存量排空与迁移计划（含回滚）

日期：2026-09-29。依据：`scripts/conversion_drain.py`（c6.1）、本地演练
（`tests/test_conversion_drain.py`）、[c6-drain-report.md](c6-drain-report.md) 的发现、代码核查。
本文**不授权任何生产动作**：生产只读盘点、停写窗口、迁移与部署均需用户另行批准（部署一律晚间）。

## 1. 起点（已核实）

- 生产运行 `suite-20260923`（8020c04），迁移停在 **0065**；0066–0077 共 12 个迁移待执行。
  **容器启动即自动迁移**（`docker_entry.sh` → `pg_store.ensure_schema`），所以「启动新镜像」本身就是
  迁移动作，必须落在已批准的晚间迁移窗口内。
- 旧镜像常驻进程：`conversion_worker --loop`（`CONVERSION_WORKER` 缺省开）；百度进程内执行器缺省关
  （`BAIDU_IMPORT_WORKER` 缺省 0，生产实际取值待盘点确认）。旧镜像里 KFB/KFBF 经旧上传通道即建
  转换任务。
- 既有 `deploy.py cutover` 在存在 queued 的百度/转换任务时中止——生产历次切换都在「无排队」时进行。
- **0067 只加 `conversion_jobs.slide_id` 列、不回填**。新代码的转换 worker 对无 `slide_id` 的任务
  fail-closed（「升级窗口旧任务，请重新上传」，`conversion_worker.process_job` 开头）。
  ⇒ **旧镜像建的转换任务必须在旧镜像里跑完**；带到新镜像只会被判失败。
- 容器未设 `BAIDU_IMPORT_STAGING_DIR`：旧百度执行器的本地下载在**容器 /tmp**（不在卷上），换容器后
  不可见；盘点须在旧容器内看，或视为随旧容器一并消失（不占卷容量）。

## 2. 盘点清单（工具输出 → 窗口判定）

| 类别 | 工具 section / 码 | 窗口前必须为零 | 说明 |
|---|---|---|---|
| 在途转换任务 | `conversion_job_open`（held/queued/converting/validating） | 是 | 旧镜像跑完；新镜像会判失败 |
| 未结算发布 intent | `intent_unresolved` / `ingest_intent_unresolved` | 是 | 0065 库无 intent 列（not_applicable），以在途任务为准 |
| 百度在途批次 | `baidu_in_process_live_lease` / `baidu_handoff_pending`；queued 批次列为插件工作 | 进程内租约与交接为零；queued 批次见 §4 | |
| 文件残留 | `residue_attempt_dir` / `residue_cancelled_job` / `residue_flat_source` / `unknown_staging_dir` / `baidu_staging_*` | 是（清理或逐项裁决） | 取消任务树、平铺源**无运行时清理入口**（F2），需一次性运维清理 |
| 预约 | `baidu_reservation_terminal_bound`、核账阻断（新库） | 是 | 0065 库无持有者绑定，按批次预约连接核对 |
| 决策项 | `failed_source_retained`、`failed_product_bundle_present`、`source_missing_*`、`association_*`、`baidu_remote_cleanup_*` | 否（C7 前裁决） | 退役后无重试入口：失败任务的保留源是清理/计费义务 |
| C7 输入 | `retained_sources`、`source_charges` | 否 | 源字节计费永不退款（F1），需裁决 |

## 3. 是否需要独立兼容构建（A）

**结论：不需要保留转换执行能力的 A 构建。** 排空发生在旧生产镜像里（§1 第 4 点决定了只能如此），
新镜像不承担任何旧任务的执行或恢复。剩下的只有一个问题：**C7 删除版（B）完成之前，是否要先发布
当前代码**（浏览器工具、插件、COS 上传、slide ID 等全部未上线）。

| 路线 | 适用 | 新镜像需要的改动 | 代价 |
|---|---|---|---|
| **R1：先发当前代码，再发 B** | 希望 C0–C5 功能早上线；把 12 个迁移与「删除」拆成两次风险 | 一个**新建转换关闭闸**：`POST /api/ingestions` 拒绝 KFB/KFBF conversion 形态（引导到 `/tools/slides`，零 COS 对象/零预约）；`POST /api/conversions/<id>/retry` 关闭；百度进程内执行器保持关闭（插件执行）。转换 worker 可保持运行作兜底（应无任务） | 小改动 + 测试；闸本身是 B 的子集，不白做 |
| **R2：等 C7，直接发 B** | 不急于上线；接受一次性大窗口 | 无（B 已删除创建路径） | 12 个迁移 + 删除同窗；回滚面更大 |

建议 **R1**：迁移窗口只承担 schema 升级与新功能，删除另起一次窗口；闸的改动量小且 B 必然包含。
F3（FS 发布后、结算前崩溃时重转 fail-closed）只影响在新镜像里执行的转换；R1 下新镜像不应再有
转换任务，F3 不是窗口阻断，随 C7 删除一并消失（若 R1 期间仍允许转换，则必须先修 F3）。

## 4. 窗口步骤（R1 与 R2 共用，差别只在第 7 步的镜像）

窗口前（白天，只读，需授权）：

1. **生产只读盘点**：用新镜像单跑工具，不经入口脚本（`CMD` 可覆盖，不会触发迁移）：
   `podman run --rm --network host -e DATABASE_URL=… -v <uploads 卷>:/data/uploads:ro
   -v <输出目录>:/out <新镜像> python3 scripts/conversion_drain.py inventory
   --upload-dir /data/uploads --json /out/inv.json`
   （库连接由工具设为 `READ ONLY` 事务；上传卷只读挂载）。另在旧容器内 `ls /tmp/baidu-import-staging`。
   按 §2 得出计数；据此确认 R1/R2 与窗口时长。
2. **迁移演练**：按既有 recipe 取生产 schema-only dump + `schema_migrations`，本地套 0066–0077，
   断言新对象与重跑幂等。

窗口内：

3. **停新建**：边缘对上传/百度建批/转换重试返回维护提示（其余只读访问可保留）。
4. **排空（旧镜像）**：等旧转换 worker 把 queued/converting 跑完；百度：进程内执行器若开着，等
   在途批次终态，否则对 queued 批次二选一——留给插件执行（新世界 claim）或经用户面取消。
   每 5 分钟跑一次 `report`（旧库形态），直到阻断只剩 §2「是否必须为零 = 否」的项。
   超过预定时长仍不收口 → **no-go，结束窗口、恢复服务**（未做任何写，零部分状态）。
5. **一次性残留清理**（F2 无运行时入口的项）：按 `report --json` 的 id 清单逐项删除取消任务树/
   平铺源，删前核对状态仍为 cancelled；记录删除清单。
6. **快照 BEFORE**：`inventory --json before.json`；停旧容器；**备份**（pg_dump 全库 + 上传卷元数据
   清单 + 旧容器 env/镜像 tag）。
7. 启动新镜像（自动迁移 0066–0077）→ 健康检查 → 插件安装 `approvePermissions:["slide:import"]` →
   `report`（新库形态）与 `reconcile_upload_capacity` → **快照 AFTER** → `compare before.json
   after.json` 必须零漂移。
8. 通过则开放流量；任一步失败走 §5。

## 5. 回滚

- **开放流量前**（第 7 步任意失败）：停新容器 → 用第 6 步 pg_dump 恢复库（新迁移随之撤销）→
  启动旧容器（`deploy.py rollback`：恢复旧容器与插件链接）。新镜像在窗口内写入的文件只可能是
  迁移产物/空目录，按清单删除。旧任务已在第 4 步排空，回滚不会让旧 worker 重新领取任何东西。
- **开放流量后**：不做库回退（会丢用户新写入）。优先前滚修复；若必须回退，只能用「理解已迁移
  责任的兼容版本」，不可回到会再次领取插件已接管批次的旧执行器（计划 §11）。
- R1 → B 的第二次窗口：回滚目标是 R1 镜像（同 schema），不涉及迁移回退。

## 6. C7 前必须由用户裁决的事项

1. **源字节计费（F1）**：转换源在上传/批次收口时计入 `used_bytes`，删除产物只退产物字节，源侧
   永不退款。选项：接受既成计费；或 C7 引入源侧退款（旧库无 slides 记账列时只能按预约
   `settled_bytes`）。工具给出逐用户 `live / charged_never_refundable / undetermined`。
2. **保留源策略**：ready 任务的源长期保留（产物删除时连带删除）。C7 删除转换代码前，要么把
   「产物 → 源文件」关系迁到通用来源/清理记录，要么一次性清理保留源（与第 1 项一起定）。
3. **失败任务的保留源与作废产物包**（`failed_source_retained` / `failed_product_bundle_present`）：
   退役后无重试入口，是否一次性清理。
4. **百度终态批次的远端副本义务**（`baidu_remote_cleanup_pending|failed`）：进程内执行器退役后
   由插件承担，还是接受残留在用户网盘（不占平台容量）。

## 7. 本地演练覆盖（证据见 c6-drain-report §3–§4）

- 排空：真实 COS 转换形态（held → 激活 → 结算）、queued 真实转换、validating+intent 崩溃后重领
  恢复、百度过期进程内租约由插件接管收口、取消任务树清理 → report GO、compare 零漂移、产物字节
  恰结算一次。
- 恢复：FS 发布后结算前崩溃 → `intent_unresolved` → intent 权威重放 → 恰一次结算；worker 重领路径
  的 fail-closed 行为（F3）如实记录。
- 只读：库转储与目录清单前后一致；盘点连接写入被数据库拒绝；`UPLOAD_DIR` 不存在时 exit 2（不以
  「看不到」冒充「没有」）。
- 旧库：≤0065 迁移建库 + 旧形态种子，工具降级运行并给出裁决。
