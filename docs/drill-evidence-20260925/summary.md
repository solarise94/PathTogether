# 切片存储迁移独立核验摘要（P6）

- 工具版本：`1.0.0-p6`
- 核验起止：2026-09-26T07:00:25+00:00 → 2026-09-26T07:00:25+00:00
- incomplete：**否**
- 违规项：0；披露项：0

## 资产计数（脱敏，仅计数）

| 项 | 数量 |
|---|---|
| slides 行 | 11 |
| ready 资产 | 6 |
| ready + id_bundle 核验通过 | 6 |
| ready + id_bundle 核验失败 | 0 |
| ready + legacy 未迁移（披露） | 0 |
| 非 ready（隔离/tombstone/在途） | 5 |

## 引用落点

| 表 | 带 slide_id 行 | 悬空 |
|---|---|---|
| ai_session_principals | 1 | 0 |
| annotation_access_events | 1 | 0 |
| change_log | 1 | 0 |
| comments | 1 | 0 |
| demo_catalog | 1 | 0 |
| project_slides | 1 | 0 |
| rois | 1 | 0 |
| run_grants | 1 | 0 |
| share_slides | 2 | 0 |
| slide_view_grants | 1 | 0 |

## tombstone 不复活

- tombstone（deleted 且保留冻结别名）：1 个；别名唯一：**是**
- 同名重生×旧分享领取人交叉验证：1 次；违规 0 次

## 配额对账（只报告不改账；「不等于」须有合法原因）

| owner | used | ready+deleting 合计 | 差值 | 原因 |
|---|---|---|---|---|
| usr_alice | 56426 | 56378 | +48 | failed 资产 accounted=48（撤回/验证失败不退款或从未入账——历史责任项，须单独核准） |
| usr_bob | 42202 | 38083 | +4119 | deleted tombstone accounted=4096（0013 口径删除不回退 used——合法不等于）；迁移 accounted 校准以包内字节为准（.manifest.json/.associated 派生物留置原位不计——计划 derivatives_in_place 披露） |

## 授权差异（对照计划；双向）

- 对照迁移项 5 个；差异 0 处（意外增加与意外缩小均计）

## 隔离口径

- 计划隔离项检查 5 个；仍可读 0 个

## go / no-go 建议

- **go（核验通过）**：独立重读 DB/磁盘全项通过。

> 本摘要为脱敏计数；逐项证据见同目录 verification.json（0600）。
