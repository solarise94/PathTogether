# C6 排空审计工具与本地演练 — 验收记录

日期：2026-09-29。验收方：主代理。被验收物：`scripts/conversion_drain.py`（c6.1）、
`tests/test_conversion_drain.py`、[c6-drain-report.md](../../../slide-tools/c6-drain-report.md)；
主代理另写 [c6-migration-drain-plan.md](../../../slide-tools/c6-migration-drain-plan.md)。

结论：**C6 本地可验部分通过。** 工具覆盖用户要求的五类存量（任务、文件、预约、发布 intent、清理
义务），可在生产 0065 旧库上降级运行；排空与恢复演练用真实执行器跑通，前后台账零漂移。
生产只读盘点尚未执行（需授权），所以 A 构建的最终判定按计划 §3 的条件给出，
待盘点数字确认。

## 1. 验收中修正

| # | 问题 | 处理 |
|---|---|---|
| 1 | 盘点以 autocommit 逐条 SELECT：运行中系统上各 section 可能前后矛盾；只读仅靠自律 | 单个 `REPEATABLE READ READ ONLY` 事务；新增用例证明写入被库拒绝 |
| 2 | `UPLOAD_DIR` 指错/不存在时按「无文件」处理 → 残留扫描全空 → 假 GO | exit 2 + 错误提示；新增 CLI 用例 |
| 3 | `compare` 预约变化计数打印缺 `%` 操作数 | 修复；新增输出断言 |
| 4 | 失败任务保留源被归为「预期保留」——退役后无重试入口，实为清理/计费义务 | 决策项 `failed_source_retained`（带字节） |
| 5 | F3 形态下作废产物包（`objects/<slide_id>/`）仍占盘，工具未列出 | 决策项 `failed_product_bundle_present`（带字节） |
| 6 | 旧容器百度下载暂存在容器 /tmp（未挂卷），换容器后不可见会被当作「无残留」 | 有批次而暂存根不存在 → 决策项 `baidu_staging_dir_missing` |
| 7 | **C5 双重计费**（演练审查中发现，属 C5 已提交代码） | 另行修复，见 C5 验收 §7 |

## 2. 关键事实（决定计划形态）

- 0067 不回填 `conversion_jobs.slide_id`；新 worker 对无 `slide_id` 任务 fail-closed ⇒ 旧镜像建的转换
  任务只能在旧镜像排空。A 构建因此**不需要保留转换执行/恢复能力**；是否需要「A」只取决于 C7 前是否
  先发布当前代码（需要一个新建转换关闭闸）。建议先发（计划 R1）。
- 镜像 `CMD` 可覆盖，工具可用新镜像单跑、不触发迁移；工具导入链不调用 `ensure_schema`。
- F1：转换源字节在上传/批次收口计入 `used_bytes`，删除产物只退产物字节——源侧永不退款；工具逐用户
  量化，C7 前需裁决。
- F3：FS 发布后、结算前崩溃时，worker 重领会重转，产物 manifest 含时间戳导致与已发布包不一致 →
  fail-closed。R1 下新镜像不应再有转换任务，非窗口阻断；若 R1 期间仍允许转换则须先修。

## 3. 复跑结果

| 套件 | 结果 |
|---|---|
| C6 新增 | **10 passed** |
| drain/核账/R16/转换/COS 形态 + 百度 + producer + 容量（合并跑） | **234 passed, 4 skipped**（skip = 需真实百度环境） |
| 全量 pytest（串行，TMPDIR 指大盘；deselect 他人在途无关失败 1 条） | **2857 passed, 8 skipped**（`results/pytest-full.txt`） |

## 4. 待用户

1. 授权生产只读盘点（计划 §4 第 1 步；只读事务 + 只读挂载）。
2. 选择 R1（先发当前代码 + 新建转换关闭闸）或 R2（等 C7 直接发 B）。
3. 计划 §6 四项 C7 前裁决（源字节计费、保留源策略、失败任务残留、百度远端副本义务）。
