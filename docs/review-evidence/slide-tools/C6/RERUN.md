# C6 复跑指南（转换/百度排空审计工具 + 本地排空/恢复演练）

日期：2026-09-29。全部命令在仓库根 `PathTogether/` 执行；`TMPDIR=$PWD/.gate-tmp`
（/tmp 是小 tmpfs；先 `mkdir -p .gate-tmp`）。证据输出在本目录 `results/`。
报告：`docs/slide-tools/c6-drain-report.md`（工具契约 / 阻断码推导 /
findings F1-F5）。

前置：`.venv` 可用（psycopg/pgserver）。各命令独立起内嵌 PG（conftest），
不要并行。

## 1. C6 新增测试（分类矩阵 / 排空演练 / 恢复×2 / 只读证明 / 旧 schema / CLI）

    TMPDIR=$PWD/.gate-tmp .venv/bin/python -m pytest \
      tests/test_conversion_drain.py -q -p no:cacheprovider

预期（2026-09-29 验收修订后实跑，`results/pytest-c6-new.txt`）：**10 passed**（~14 s）。

- `test_classification_matrix`：真实 COS conversion 形态（`_drive_to_validating`
  kind=conversion + 结算被拒）构造 held 子任务；queued/converting 存活与过期
  租约/validating+intent/ready 保留源/ready 产物已删（源计费永不退款 300 B）
  /failed 有源/failed 无源/cancelled 残留+平铺源/终态代次残留/未知 .staging；
  百度 queued 无人领取 / in-process 活租约 / in-process 过期+非终态条目 /
  plugin 租约 / 终态暂存残留 / 终态+reserved 预约（SQL 种子）/ 远端清理义务。
  断言全部 inventory 分类 + report 阻断码**精确集合**（13 个码）。
- `test_drain_rehearsal`：NO-GO → BEFORE 快照 → 真实执行器排空
  （claim_one/process_job 循环 + process_ready + plugin_claim_batch/
  plugin_report_item）→ AFTER：report exit 0、compare exit 0、产物字节恰
  结算一次（used == used_before + Σ accounted + 18752）。
- `test_recovery_rehearsal`：intent+FS 发布后、结算前崩溃 → intent 权威重放
  （`publish_with_channel(manifest=None)`）→ 恰一次结算 → compare 0。
- `test_recovery_worker_reclaim_path_hits_manifest_conflict`：同窗口走 worker
  重领重转 → `publish_conflict` fail-closed（finding F3）。
- `test_readonly_proof`：10 张表正则化转储 + UPLOAD_DIR/百度暂存递归清单
  （path,size,mtime_ns）在 inventory/report/compare 前后逐位一致；
  `--probe-locks` 他进程持锁报 `live_lock_holder` 且零写入。
- `test_legacy_schema_inventory`：内嵌 PG 另建库、按 `pg_store.ensure_schema`
  同款机制应用 ≤0065 迁移、纯 SQL 种子；inventory 成功 + 特性缺失报告 +
  计数正确；report exit 3（conversion_job_open + source_missing_open +
  baidu_in_process_live_lease）；结束 DROP DATABASE。
- `test_cli_subprocess`：子进程直跑脚本——report exit 3 + 阻断码 + `--json`
  可解析；收口后 exit 0（`report: go`）。

## 2. 回归批次（改动面触及的既有套件）

    # 批次 1：排空/核账/R16 交接 + 转换链（工具复用其原语；测试复用其夹具）
    TMPDIR=$PWD/.gate-tmp .venv/bin/python -m pytest \
      tests/test_upload_drain.py tests/test_reconcile_upload_capacity.py \
      tests/test_r16_handoff.py tests/test_conversion_task_api.py \
      tests/test_conversion_slide_id_pg.py tests/test_cos_ingestion_kinds.py \
      -q -p no:cacheprovider

预期（`results/pytest-regression-1-drain-conversion.txt`）：**73 passed**。

    # 批次 2：百度（store/恢复/ingest/adapter/插件桥/slide_id/live）
    TMPDIR=$PWD/.gate-tmp .venv/bin/python -m pytest \
      tests/test_baidu_adapter.py tests/test_baidu_import_recovery.py \
      tests/test_baidu_imports.py tests/test_baidu_ingest.py \
      tests/test_baidu_review_regressions.py tests/test_baidu_slide_id_pg.py \
      tests/test_baidu_import_plugin_integration.py tests/test_baidu_live.py \
      -q -p no:cacheprovider

预期（`results/pytest-regression-2-baidu.txt`）：**105 passed, 4 skipped**。
注：摘要行之后可能打印 `OperationalError ... database system is shutting
down`——pytest 会话结束后残留线程重连被停库拒绝的 teardown 噪音（exit 0，
计数不受影响；与 C5 RERUN 记录的已知现象一致）。

    # 批次 3：容量生命周期（模型/COS/通道——ledger/compare 不变量相关）
    TMPDIR=$PWD/.gate-tmp .venv/bin/python -m pytest \
      tests/test_capacity_lifecycle_model.py \
      tests/test_capacity_lifecycle_cos.py \
      tests/test_capacity_lifecycle_channels.py -q -p no:cacheprovider

预期（`results/pytest-regression-3-capacity.txt`）：**29 passed**。

## 3. 手工冒烟（可选；对任意环境只读）

    # 全量 schema（开发库）
    TMPDIR=$PWD/.gate-tmp .venv/bin/python scripts/conversion_drain.py \
      inventory --upload-dir ./uploads --json /tmp/c6-inv.json
    TMPDIR=$PWD/.gate-tmp .venv/bin/python scripts/conversion_drain.py \
      report --upload-dir ./uploads            # exit 0=GO / 3=NO-GO

    # 生产旧库（0065；DATABASE_URL 指向旧镜像的只读副本）
    TMPDIR=$PWD/.gate-tmp .venv/bin/python scripts/conversion_drain.py \
      inventory --database-url "$LEGACY_DB_URL" --upload-dir "$UPLOAD_DIR" \
      --json /tmp/c6-legacy-inv.json
    # 期望：成功；meta.schema.applied_max=0065_*；缺失特性 section 报
    # not_applicable_schema（slide_id/commit_intent_json/held/slides 记账/
    # holder 绑定/ingestion_jobs/producer_imports）

compare 演练门禁（排空前后各取一份 inventory JSON）：

    TMPDIR=$PWD/.gate-tmp .venv/bin/python scripts/conversion_drain.py \
      compare BEFORE.json AFTER.json                  # exit 0=零未解释漂移
