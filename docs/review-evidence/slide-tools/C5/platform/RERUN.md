# C5 复跑指南（producer 导入 API——平台侧）

日期：2026-09-29。全部命令在仓库根 `PathTogether/` 执行；`TMPDIR=$PWD/.gate-tmp`
（/tmp 是小 tmpfs）。证据输出在本目录 `results/`。报告：
`docs/slide-tools/c5-platform-report.md`。合同：
`docs/slide-tools/c5-producer-import-contract.md`。

前置：`.venv` 可用（psycopg/pgserver）；原生 CLI 已构建（缺失时
`PATH=$HOME/.cargo/bin:$PATH bash scripts/build_slide_transform.sh`——T1 用）。
各命令独立起内嵌 PG（conftest），**不要并行**。

已知无关失败（不在下列任何命令中）：`tests/test_ai_budget_wiring.py::
test_ui_budget_card_and_max_steps_sync_present`（读他人未提交的 admin manifest
版本）。工作区另有他人未提交改动（admin 插件/注册邮件/模板等），与本通道无关。

## 1. C5 新增测试（T1-T19 平台侧）

    TMPDIR=$PWD/.gate-tmp .venv/bin/python -m pytest \
      tests/test_producer_imports.py tests/test_producer_import_auth.py \
      tests/test_producer_import_baidu_bridge.py \
      tests/test_producer_import_migration.py \
      tests/test_producer_import_native_chain.py -q

预期（2026-09-29 验收修复后实跑，`results/pytest-c5-new.txt`）：**60 passed**（原 57 + 桥 3）。

- T1 在 `test_producer_import_native_chain.py`（native CLI `gen-kfb 580x300` →
  `convert` → 真实 BigTIFF 交付；CLI 缺失时该文件 skip，构建后必过）。
- T17 时间旅行/水位、T11 崩溃注入均在 `test_producer_imports.py` 内
  （DB 拨表 + monkeypatch 故障注入，模式同 `tests/test_capacity_lifecycle_*`）。

## 2. 回归批次（改动面触及的既有套件）

    # 批次 1：guard/任务锁/publish/ingestion/COS（T14 的核心原生链路回归）
    TMPDIR=$PWD/.gate-tmp .venv/bin/python -m pytest \
      tests/test_upload_guard.py tests/test_r12_lifecycle_acceptance.py \
      tests/test_slide_id_review_r13.py tests/test_slide_publish_pg.py \
      tests/test_ingestion_store_phase1.py tests/test_ingestion_api.py \
      tests/test_cos_ingest_worker.py tests/test_cos_ingestion_kinds.py \
      tests/test_capacity_lifecycle_model.py tests/test_capacity_lifecycle_cos.py \
      tests/test_capacity_lifecycle_channels.py tests/test_cos_fifth_review.py \
      tests/test_cos_fourth_review.py tests/test_cos_review_fixes.py \
      tests/test_cos_second_review.py tests/test_cos_third_review.py -q

预期（`results/pytest-regression-1-guard-publish-ingest-cos.txt`）：
**153 passed, 1 skipped**。

    # 批次 2：baidu（store/ingest/adapter/live）
    TMPDIR=$PWD/.gate-tmp .venv/bin/python -m pytest \
      tests/test_baidu_adapter.py tests/test_baidu_import_recovery.py \
      tests/test_baidu_imports.py tests/test_baidu_ingest.py \
      tests/test_baidu_review_regressions.py tests/test_baidu_slide_id_pg.py \
      tests/test_baidu_live.py -q

预期（`results/pytest-regression-2-baidu.txt`）：**102 passed, 4 skipped**。
注：摘要行之后可能打印一段 `OperationalError ... database system is shutting
down`——pytest 会话结束后残留线程重连被停库拒绝的 teardown 噪音（exit 0，计数
不受影响；与 C5 改动无关，单文件/组合均全绿）。

    # 批次 3：plugin 鉴权/manifest/dispatch/usage
    TMPDIR=$PWD/.gate-tmp .venv/bin/python -m pytest \
      tests/test_plugin_api.py tests/test_plugin_manifest.py \
      tests/test_plugin_dispatch.py tests/test_plugin_source_policy.py \
      tests/test_plugin_v1_transport.py tests/test_plugin_region_pixel_gate.py \
      tests/test_plugin_agent_optout.py tests/test_sample_plugin.py \
      tests/test_usage_ingest.py -q

预期（`results/pytest-regression-3-plugin-usage.txt`）：**170 passed**。

    # 批次 4：核账/排空工具 + 迁移 + slide 核心（读门/删除/存储）
    TMPDIR=$PWD/.gate-tmp .venv/bin/python -m pytest \
      tests/test_reconcile_upload_capacity.py tests/test_upload_drain.py \
      tests/test_pg_infra.py tests/test_migration.py \
      tests/test_slide_read_gate_pg.py tests/test_slide_delete_pg.py \
      tests/test_slide_store_pg.py tests/test_slide_storage.py -q

预期（`results/pytest-regression-4-tools-migrations.txt`）：**108 passed**。

    # 批次 5：CSRF urlmap/访问控制/工具页/账号/dispatch/admin 插件（含他人
    #        编辑中的文件——只跑不改）
    TMPDIR=$PWD/.gate-tmp .venv/bin/python -m pytest \
      tests/test_csrf_urlmap.py tests/test_access_control.py \
      tests/test_slide_tools_page.py tests/test_slide_tools_upload_capability.py \
      tests/test_account_auth.py tests/test_backend_dispatch.py \
      tests/test_admin_plugin.py tests/test_admin_preview.py -q

预期（`results/pytest-regression-5-auth-csrf-admin.txt`）：**287 passed**。

    # 批次 6：native core oracle（差分/真实样本门禁）
    TMPDIR=$PWD/.gate-tmp .venv/bin/python -m pytest \
      tests/test_slide_transform_core.py -q

预期（`results/pytest-regression-6-slide-transform-core.txt`）：
**16 passed, 1 skipped**（真实样本缺席时的既有 skip；并行重负载下偶发瞬时
失败，串行复跑即绿）。

## 3. 工具 CLI 证据（T19）

`results/tools-cli-demo.txt` 的生成脚本（同文件首行注释）：内嵌 PG 上造一个
在途 producer 导入（writing、staging 512B、final+scratch 双预约），然后：

    .venv/bin/python scripts/reconcile_upload_capacity.py --upload-dir <UPLOAD_DIR>
    .venv/bin/python scripts/upload_drain.py audit --upload-dir <UPLOAD_DIR>

预期：reconcile dry-run **exit 0**（在途 producer 预约 ok 不阻断、无 dangling/
未知目录/quota drift）；drain audit **exit 0** 且输出
`producer 导入暂存证据（新通道，逐成员扫描，未裁决）：1`（producer 残留不进
旧链路异常）。逐成员扫描与核账同一实现（`recon.scan_task_manifest`）。

## 4. 迁移

conftest `ensure_schema` 自动应用 0077（文件名序）。清单断言：
`tests/test_pg_infra.py::test_schema_migrations_recorded`（批次 4 内）；幂等/
约束冒烟：`tests/test_producer_import_migration.py`（批次 1 的 C5 新增命令内）。

## 5. 结果文件

- `results/pytest-c5-new.txt` —— 60 passed
- `results/pytest-regression-1-guard-publish-ingest-cos.txt` —— 153 passed, 1 skipped
- `results/pytest-regression-2-baidu.txt` —— 102 passed, 4 skipped
- `results/pytest-regression-3-plugin-usage.txt` —— 170 passed
- `results/pytest-regression-4-tools-migrations.txt` —— 108 passed
- `results/pytest-regression-5-auth-csrf-admin.txt` —— 287 passed
- `results/pytest-regression-6-slide-transform-core.txt` —— 16 passed, 1 skipped
- `results/tools-cli-demo.txt` —— reconcile/drain CLI 证据
