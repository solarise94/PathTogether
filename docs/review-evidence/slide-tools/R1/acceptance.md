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

## 4. 已知缺口（不阻塞 R1，已写入报告 §6）

1. ingestion 控制 API 与 COS 分块 PUT 在浏览器 e2e 中由有状态假后端承担；服务端语义由 pytest 覆盖。
2. 项目关联端点在 i/i4/i5 中被假实现（假 slide_id 在真实库中不存在）；断言的是端点、pid、载荷与
   重试语义。真实关联需在生产验收中带真实上传核对。
3. 换账号重新确认用原生 `confirm()`。
4. 工作台一次选多个 KFB 时逐文件交接，没有批量 popup 队列。

## 5. 不在本记录范围

生产迁移、安装与切换（含新发布 env 显式 `CONVERSION_WORKER=0`、`BAIDU_IMPORT_WORKER=0`）仍是单独的
发布动作，须用户批准、晚间执行；C7 四项裁决（旧源计费更正、保留源记录、孤儿清理、插件接管百度副本
清理）尚未实现。
