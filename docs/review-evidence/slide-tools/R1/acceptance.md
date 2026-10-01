# R1 一键转换并上传 — 验收记录

日期：2026-09-30。验收方：主代理。被验收物：[r1-convert-and-upload-report.md](../../../slide-tools/r1-convert-and-upload-report.md)；
需求：`docs/slide-tools/c6-migration-drain-plan.md` §3.1（用户撰写的 R1 产品补充）。服务端转换
创建/重试闸与 worker 默认关已先行提交（c08fef7），本记录覆盖浏览器侧「转换并上传」与工作台入口。

结论：**R1 一键转换并上传通过（本机可验部分）。** 验收中发现 1 处实质缺陷（目标项目关联失败后
不可恢复）并连带 1 处 promise 未返回，已修复并补 e2e 回归 i5；修复后全部门禁复跑通过。真实 COS
桶、真实项目关联端点的端到端（带真实 slide_id）属生产验收，不宣称。

## 1. 独立复跑（修复前，确认子代理结论）

R1 e2e 20/20、vitest 595/595、pytest（能力端点 / 转换闸 / ingestion / 工具页模板）50 passed、
C2 门禁 PASS——与报告一致。

## 2. 审查发现与修复

| 级别 | 发现 | 修复 |
|---|---|---|
| P1 | `handlePublished` 在关联工作台目标**之前**把意图标记 `done`；已发布行无重试入口。关联请求失败或发布后标签被关闭 → 切片永久留在「未归类」，用户无法补救；「新项目」目标在重试时还可能再建一次 | 先关联、成功才 `done`（记 `projectId`）；失败记 `assocError`、意图保持 pending；新项目 pid 创建后先写回 `target.project`；已发布行在 pending+有目标时给「重试加入项目」（点击才发请求） |
| P2 | 页面 `onPublished` 包装未返回 promise，`await onPublished(...)` 不等待关联——列表在意图更新前刷新（i5 首跑即以「retry button gone」超时暴露） | 包装返回 `handlePublished` 的 promise |

回归：`i5-assoc-failure-retry`——首次关联注入 500 → 断言意图 pending、`assocError` 已记、pid 已写回；
刷新 → 断言零自动请求且重试按钮在；点击 → 断言共 2 次关联（同一 pid）、1 次建项目、1 个 ingestion、
意图 done、按钮消失。

## 3. 修复后门禁（全部实跑）

| 套件 | 结果 | 证据 |
|---|---|---|
| R1 e2e | **21/21 ALL PASS** | `results/r1-e2e.{txt,json}` |
| C4 e2e | **15/15** | `results/c4-e2e.txt` |
| C4 工作台请求序列 vs 基线 | 14 请求逐条一致（`true`） | `results/workbench-seq-*` |
| C3 e2e（仅转换隐私/离线） | **18/18** | `results/c3-e2e.{txt,json}` |
| vitest `tests/js` | **38 文件 595/595**（含 i18n zh/en 键契约，新键已覆盖） | `results/vitest.txt` |
| C2 无整文件门禁 | PASS | `results/c2-no-whole-file.txt` |
| pytest 全量（串行，已知无关 deselect 1） | **2868 passed, 8 skipped** | `results/pytest-full.txt` |

## 3b. 二轮：用户复现的两处缺陷（2026-09-30）

用户在 339e06c 上复现：已发布任务的上传动作又建第二个 ingestion 并重传；换账号确认只改
意图账号却续传旧 ingestion（另一普通用户 403，管理员可访问但归属/记账仍在原用户）。修复与
测试见报告 §8。关键证据：

| 证据 | 结果 |
|---|---|
| k1 / d4 在旧控制器上（探针副本去掉 UI 断言） | k1 `creates=2 puts=4`；d4 B 对 A 的任务 `GET → 403` ×2 —— 两处缺陷均被抓到 |
| 修复后 R1 e2e | **24/24**（新增 d4/d5 真实授权、k1；d3 改为不转移归属；i/i4 真实项目端点；i5 加重复触发） |
| C4 15/15、工作台序列一致、C3 18/18、C2 PASS、vitest 607/607 | 全绿 |
| pytest 全量 | 见 `results/pytest-full.txt` |

## 3c. r1-rc2：R1 全程关闭百度导入（2026-10-01 用户裁决）

r1-rc1 的插件不能枚举分享，关掉旧 worker 后平台没有枚举执行者；而 `BAIDU_IMPORT_WORKER=0` 不阻止
API 受理请求。核实 r1-rc1：新建枚举已受 `BAIDU_ENUMERATION_ENABLED` 门控，但**从既有 ready 枚举建
批次、重试失败批次都不检查 `BAIDU_IMPORT_ENABLED`**——生产盘点中的 failed 批次一经重试就会变回
queued，没有执行者处理。

修复（`release/r1`，标签 `r1-rc2`）：
- `baidu_import_store._require_import_available()`：`create_import` 与 `retry_items` 在任何写入/
  容量预约之前检查能力，不可用即 503（原因码沿用能力端点：`enumeration_disabled` /
  `import_disabled` / 连接器原因）。已在关闭前受理的批次照常收口（b10 改为此语义）。
- 页面：原因文案改为「功能暂时关闭（维护中）」，不再显示配置名；失败批次的「重试失败项」按钮在
  导入不可用时禁用并说明原因，点击不发请求。

| 证据 | 结果 |
|---|---|
| `tests/test_r1_baidu_disabled.py`（真实 HTTP 路由 + 生产适配器，原因只来自开关） | 两开关 0：能力端点 `enumeration_disabled`；新建枚举、既有枚举建批次、重试 failed 批次全部 503；枚举/批次/条目/queued 行与容量预约计数不变；failed 批次与条目原样；只关导入同样拒绝（`import_disabled`） |
| 同测试在 r1-rc1 的 `baidu_import_store.py` 上 | 失败：建批次返回 **202**（新建 queued 批次） |
| vitest 新用例（重试按钮禁用、点击零请求）在去掉门控的 app.js 上 | 失败；修复后 606/606 |
| 百度相关 pytest（含 b10） | 125 passed, 7 skipped |
| pytest 全量 | 见 `results/pytest-full-rc2.txt` |

发布 env 必须显式 `BAIDU_ENUMERATION_ENABLED=0`、`BAIDU_IMPORT_ENABLED=0`（与两个 worker 开关同为
G1 门禁项），上线后按 runbook §A4 实测拒绝且零新增待执行行。R1 窗口不安装百度插件。

## 4. 已知缺口（不阻塞 R1，已写入报告 §6）

1. ingestion 控制 API 与 COS 分块 PUT 在浏览器 e2e 中由有状态假后端承担；服务端语义由 pytest 覆盖。
2. 项目关联：i/i4 已走真实端点与真实 ready 切片；i5 的失败是注入 500。真实 COS 上传后的端到端
   关联留给生产验收。
3. 两个普通用户的真实授权只覆盖到 `upload-complete`（测试服务不推进 completing 之后）。
4. 工作台一次选多个 KFB 时逐文件交接，没有批量 popup 队列。

## 5. 不在本记录范围

生产迁移、安装与切换（含新发布 env 显式 `CONVERSION_WORKER=0`、`BAIDU_IMPORT_WORKER=0`）仍是单独的
发布动作，须用户批准、晚间执行；C7 四项裁决（旧源计费更正、保留源记录、孤儿清理、插件接管百度副本
清理）尚未实现。
