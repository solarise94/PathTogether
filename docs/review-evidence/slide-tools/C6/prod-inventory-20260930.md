# 生产只读盘点记录（2026-09-30 00:13 +08:00）

授权：用户 2026-09-30 批准的范围化只读盘点（仅观察）。本记录只含聚合数字；原始 JSON 含用户 id/文件名，
留在生产主机 `~/c6-audit/20260930-001324/out/`（0600）与本机未入库目录，不入仓。

## 1. 执行方式

- 代码：提交 `c920942` 的 6 个文件（`scripts/conversion_drain.py` 及其导入闭包：
  `scripts/reconcile_upload_capacity.py`、`pg_store.py`、`platform_features.py`、`slide_storage.py`、
  `upload_guard.py`），`git archive` 送到独立目录 `~/c6-audit/<ts>/src`，sha256 记于 `src-sha256.txt`。
  闭包内模块无导入期副作用，不调用 `ensure_schema`。
- 运行：旧生产镜像 `pathtogether-demo:suite-20260923`，`--entrypoint python3` 绕过 `docker_entry.sh`
  （不迁移、不起 worker）；`--read-only` 根文件系统；代码与上传卷 `/data/uploads` 均 `:ro` 挂载；
  唯一可写的是独立输出目录 `/out`。库连接为工具的 `REPEATABLE READ READ ONLY` 事务；连接串经 0600
  临时 env 文件传入，运行后删除。未使用 `--probe-locks`（生产版无任务锁，且探测会短暂取锁）。
- 旧容器内观察：`podman exec … find /tmp/baidu-import-staging`（百度暂存默认位置——生产未设
  `BAIDU_IMPORT_STAGING_DIR`，挂载的 `/data/import-staging` 属管理员批量导入脚本）、`/proc` 进程清单。
- 补充观察：`svs-pg` 内 `BEGIN TRANSACTION READ ONLY … ROLLBACK` 查询一条 reserved 预约与百度批次细节。

## 2. 结果

| 项 | 结果 |
|---|---|
| schema | `0065`（65 个迁移）；无 slide_id / intent / held / slides 记账 / 持有者绑定 / ingestion_jobs / producer_imports |
| `conversion_jobs` | **0 行**（生产从未产生后端转换任务）；无转换暂存树、无平铺源、无 unknown `.staging` 目录 |
| 百度批次 | 1 个，`failed`（1 条目 `failed:connector_failed`），`cleanup_state=not_needed`，无批次预约；无在途批次 |
| 旧容器百度暂存 | `/tmp/baidu-import-staging` **不存在**；`/tmp` 为空 |
| 文件扫描 | 完整（`scan_errors` 0） |
| report | **GO**（exit 0，无阻断）；唯一决策项 `baidu_staging_dir_missing`（审计容器看不到旧容器 /tmp，已由上一行直接观察补足） |
| inventory | exit 0 |
| 台账 | 12 个用户；预约 consumed 60 / released 7 / reserved 1 |
| 源计费 / 保留源 | 无事件、无保留源（无转换任务） |
| 旧容器常驻进程 | gunicorn、registration_mail_worker、format_request_worker、research_deletion_worker、**conversion_worker**、**baidu_import_worker**（均在跑，无任务可做） |
| 旧容器 env | `BAIDU_IMPORT_WORKER=1`（显式开）；`CONVERSION_WORKER` 未设（缺省开） |

## 3. 窗口相关的发现

1. **转换侧无需排空**：零任务、零文件。R1 窗口的转换排空只需停写后复跑确认仍为零。
2. **`BAIDU_IMPORT_WORKER=1` 是显式设置**：`deploy.py prepare` 会把旧容器 env 带进新 release——新部署
   必须显式写 `BAIDU_IMPORT_WORKER=0`、`CONVERSION_WORKER=0`，不能依赖镜像缺省。
3. **上传侧遗留（R16 范围，非转换）**：1 个 `active` 旧上传任务（2026-09-18 起无更新），其预约
   `reserved` 1,287,867,278 B、2026-09-18 已过期仍占用户在途额度。窗口内由上传排空/核账工具
   （`scripts/upload_drain.py` / `reconcile_upload_capacity.py`）收口，需计入窗口步骤。
