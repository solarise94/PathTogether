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

### 1.1 生产只读盘点（2026-09-30 00:13，记录：`docs/review-evidence/slide-tools/C6/prod-inventory-20260930.md`）

- `conversion_jobs` **0 行**：生产从未产生后端转换任务；无转换暂存树、平铺源、unknown `.staging` 目录；
  文件扫描完整。report **GO**。
- 百度：1 个 `failed` 批次（无预约、`cleanup_state=not_needed`），无在途批次；旧容器
  `/tmp/baidu-import-staging` 不存在。
- 旧容器实际在跑 `conversion_worker` 与 `baidu_import_worker`；env 显式 `BAIDU_IMPORT_WORKER=1`，
  `CONVERSION_WORKER` 未设（缺省开）。`deploy.py prepare` 会把旧 env 带进新 release。
- 上传侧（R16 范围）：1 个 2026-09-18 起停滞的 `active` 旧上传任务，预约 `reserved` 1,287,867,278 B 且已
  过期——窗口内由上传排空/核账收口。

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
| **R1：先发当前代码，再发 B**（**2026-09-30 用户选定**） | 希望 C0–C5 功能早上线；把 12 个迁移与「删除」拆成两次风险 | **新建转换关闭闸**（已实现 c08fef7）：`POST /api/ingestions` 对 KFB/KFBF 返回 422 `conversion_moved_to_browser` + 工具页地址（不建行/不占预约/不发凭证）；`POST /api/conversions/<id>/retry` 410；上传 capability 把这些格式列在 `browser_convert`。闸是模块常量，不读 env。**新部署必须 `CONVERSION_WORKER=0`、`BAIDU_IMPORT_WORKER=0`**（镜像缺省已关，但 release env 须显式写 0——旧 env 带 `BAIDU_IMPORT_WORKER=1`）；百度由插件执行。加一键「转换并上传」（§3.1） | 小改动 + 测试；闸本身是 B 的子集，不白做 |
| **R2：等 C7，直接发 B** | 不急于上线；接受一次性大窗口 | 无（B 已删除创建路径） | 12 个迁移 + 删除同窗；回滚面更大 |

**R1 已选定**：迁移窗口只承担 schema 升级与新功能，删除另起一次窗口。F3（FS 发布后、结算前崩溃时
重转 fail-closed）只影响在新镜像里执行的转换；R1 下新镜像既不建转换任务也不运行转换 worker，所以 F3
不是窗口阻断，随 C7 删除一并消失。转换 worker 若在新部署里开着，这个前提就不成立——这是
`CONVERSION_WORKER=0` 必须写死在 release env 的原因。

## 4. 窗口步骤（R1）

原则：
- **写者围栏**：迁移与核账期间，任何后台写者都不运行。旧镜像的写者随旧容器停止；新镜像的写者不只
  是入口脚本拉起的 worker，还有 gunicorn 进程内的 daemon（producer 导入 sweep、删除执行器、AI
  预算回收/绑定重试、保留期清理）——所以**迁移、盘点、核账都用一次性容器**（覆盖启动命令，不经
  `docker_entry.sh`），核账通过之前不启动正式容器。
- **可恢复备份先于任何破坏性动作**（删除残留、核账修复、迁移）。
- **库快照不让在线文件扫描原子化**：写者全停后再跑最终盘点，以它为准。

窗口前（白天，只读，已授权部分已完成——见 §1.1）：

1. 生产只读盘点：一次性容器 `--entrypoint python3`、`--read-only`、代码与上传卷 `:ro`、独立输出目录；
   工具自身 `READ ONLY` 事务。旧容器内另行列出 `/tmp/baidu-import-staging` 与进程。
2. 迁移演练：生产 schema-only dump + `schema_migrations` 本地套 0066–0077，断言新对象与重跑幂等。
3. 准备 release：`deploy.py` 的 release env 显式 `CONVERSION_WORKER=0`、`BAIDU_IMPORT_WORKER=0`
   （`prepare` 从旧容器 env 生成 .env——两项须进 EXTRA_ENV 白名单并覆盖旧值，shape 对比同步）；
   插件 bundle 与来源策略 pin 就位。

窗口内：

4. **停新建**：边缘对上传、百度建批、转换重试返回维护提示；只读访问可保留。
5. **排空（旧镜像，写者仍在）**：旧转换 worker/百度执行器跑完在途任务（按 §1.1 预期为零）；每 5 分钟
   `report`，直到阻断只剩「是否必须为零 = 否」的项。超时不收口 → no-go，恢复服务（此前零写入）。
6. **停旧容器**（全部旧写者随之停止）；确认无残留进程。
7. **最终盘点（权威）**：一次性容器跑 `conversion_drain.py inventory --json before.json` 与 `report`
   （旧库形态；`upload_drain.py` 依赖 0071 之后的表，放到步骤 11）。写者已停，文件扫描不再与写入
   竞争；`inventory` 非零（含扫描不完整）即 no-go。
8. **备份**：pg_dump 全库；上传卷、`share`、`plugins` 等卷的元数据清单（路径、大小、mtime）；将要删除
   或修复的对象（步骤 9 的清单）按原路径复制到备份目录；旧容器 env 与镜像 tag。**校验备份可读后**
   才进入下一步。
9. **破坏性收口**（按步骤 7 的 id 清单，逐项核对状态后执行并记录）：F2 无运行时入口的残留（转换侧
   按 §1.1 预期为零）。旧上传任务的停滞预约**不在旧库上修**——迁移后由核账工具处理（步骤 11）。
10. **迁移（一次性容器，无写者）**：`--entrypoint python3` 执行与 `docker_entry.sh` 相同的
    `pg_store.ensure_schema` 片段；随后重跑一次确认无新迁移（幂等）。
11. **迁移后核账（一次性容器，无写者）**：`conversion_drain.py report` 与 `inventory --json after.json`、
    `compare before.json after.json`（零漂移）、`reconcile_upload_capacity`（按 R16 的 stop/repair 处理
    停滞旧上传任务，每个动作留回执）、`upload_drain.py report` 必须 GO。任一失败 → §5「开放流量前」回滚。
12. **启动正式容器**（release env，入口脚本重跑 `ensure_schema` 为无操作）→ 健康检查 → 确认容器内
    无 `conversion_worker` / `baidu_import_worker` 进程 → 插件安装 `approvePermissions:["slide:import"]`
    → 再跑一次 `report`。
13. 开放流量。

## 5. 回滚

- **开放流量前**（步骤 10–12 任一失败）：停新容器 → 用步骤 8 的 pg_dump 恢复库（新迁移随之撤销）→
  按步骤 8 的副本恢复步骤 9 删除/修改过的对象 → 启动旧容器（`deploy.py rollback`：恢复旧容器与插件
  链接）。新镜像在窗口内写入的文件只可能是迁移产物/空目录，按清单删除。旧任务已在步骤 5 排空，
  回滚不会让旧 worker 重新领取任何东西。
- **开放流量后**：不做库回退（会丢用户新写入）。优先前滚修复；若必须回退，只能用「理解已迁移
  责任的兼容版本」，不可回到会再次领取插件已接管批次的旧执行器（计划 §11）。
- R1 → B 的第二次窗口：回滚目标是 R1 镜像（同 schema），不涉及迁移回退。

## 6. C7 前的裁决（2026-09-30 用户已定）与由此产生的工作

1. **纠正过时的源字节计费**：对「已证明计费、且文件已删除」的源恢复配额。每笔调整有可审计、幂等的
   回执；不得按聚合残差或不确定分类（`undetermined`）计算额度。
2. **保留源进入通用来源记录**：独立于转换运行时保存 owner、产物关系、位置、计费与清理状态；删除源时
   恰好一次释放其已证明的计费。
3. **清理已确认的孤儿产物与过时临时文件**。失败任务的源文件在通用记录下保持可恢复，直到被显式放弃
   或移交；「任务失败」本身不足以证明其输入可丢弃。
4. **插件负责未完成的百度副本清理**：义务转入持久、可重试的记录，失败回报平台；清理只针对记录在案的
   应用创建副本，不触碰用户原始文件。

这些是 C7 删除前的实现工作（新增通用来源/清理记录、回执化的计费纠正、插件侧清理义务）。按 §1.1，
生产当前没有转换源、保留源或百度副本义务，因此它们不阻断 R1 窗口；R1 期间也不会产生新的转换源
（服务端不再建转换任务）。C7 仍须对全部历史计费逐项证明后再调整。

## 7. 本地演练覆盖（证据见 c6-drain-report §3–§4）

- 排空：真实 COS 转换形态（held → 激活 → 结算）、queued 真实转换、validating+intent 崩溃后重领
  恢复、百度过期进程内租约由插件接管收口、取消任务树清理 → report GO、compare 零漂移、产物字节
  恰结算一次。
- 恢复：FS 发布后结算前崩溃 → `intent_unresolved` → intent 权威重放 → 恰一次结算；worker 重领路径
  的 fail-closed 行为（F3）如实记录。
- 只读：库转储与目录清单前后一致；盘点连接写入被数据库拒绝；`UPLOAD_DIR` 不存在时 exit 2（不以
  「看不到」冒充「没有」）。
- 旧库：≤0065 迁移建库 + 旧形态种子，工具降级运行并给出裁决。
