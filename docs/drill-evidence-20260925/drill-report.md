# P6 迁移演练报告（合成副本，20260925）

## 环境构造

- 内嵌 pgserver（临时数据目录，退出即删）+ 临时 UPLOAD_DIR；DATABASE_URL/UPLOAD_DIR 全程指向该副本。
- 管线：种子世界 → backfill --apply → P0 冻结审计（frozen）→ plan → migrate --apply（含三处崩溃注入的中断恢复）→ verify 独立核验。
- 崩溃注入点：sld_drill_mrxs01@copied / sld_drill_svs01@after_publish / sld_drill_tif01@bound（SystemExit(130) 模拟 kill；journal 逐事件 fsync）。

## 计划摘要

```json
{
  "counts": {
    "items": 11,
    "migrate": 5,
    "no_alias_rows_out_of_scope": 1,
    "quarantine": 4,
    "retain_history": 2
  },
  "env": "drill-local",
  "inputs": {
    "inventory": {
      "records": 13,
      "sha256": "6efd78c9d8d9bd44822edcf6150796c9f5475d56883c49789305fe6549f7fa7d"
    },
    "issues": {
      "by_disposition": {
        "manual_review": 6,
        "quarantine": 1,
        "retain_history": 2
      },
      "records": 9,
      "sha256": "64bf3118b9f0742cf3655d062bd6b99538b81640e0a620ab32bbf000e370e391"
    }
  },
  "plan_version": 1,
  "record_type": "plan_header",
  "tool_version": "1.0.0-p6"
}
```

## §2 六项验证结果

```
ok    §2-1 格式样本 sld_drill_kfbp01 → id_bundle 且入口可读（kfb-converted.tif）
ok    §2-1 格式样本 sld_drill_mrxs01 → id_bundle 且入口可读（panel.mrxs）
ok    §2-1 格式样本 sld_drill_ome01 → id_bundle 且入口可读（kfbf-out.ome.tif）
ok    §2-1 格式样本 sld_drill_svs01 → id_bundle 且入口可读（specimen.svs）
ok    §2-1 格式样本 sld_drill_tif01 → id_bundle 且入口可读（scan.tif）
ok    §2-1 MRXS 伴侣目录全成员入包（保名入口 + manifest）
ok    §2-1 kfb 转换产物形态：入口入包，派生物留置原位（计划披露）
ok    §不删源：specimen.svs 保留原位
ok    §不删源：panel.mrxs 保留原位
ok    §不删源：panel 保留原位
ok    §不删源：scan.tif 保留原位
ok    §不删源：kfb-converted.tif 保留原位
ok    §不删源：kfbf-out.ome.tif 保留原位
ok    §2-2 owner 读自己资产
ok    §2-2 分享成员（share_slides+grants 领取）读旧资产
ok    §2-2 显式 view grant 通道（关 share 仍可读）
ok    §2-2 demo 目录 capability 通道
ok    §2-2 项目成员关系落点
ok    §2-2 标注（rois）关系落点
ok    §2-2 run grants 落点
ok    §2-2 AI principals 落点
ok    §2-3 sld_drill_kfbp01：copied/bound/postverified 各恰一次（重跑不重复）
ok    §2-3 sld_drill_mrxs01：copied/bound/postverified 各恰一次（重跑不重复）
ok    §2-3 sld_drill_ome01：copied/bound/postverified 各恰一次（重跑不重复）
ok    §2-3 sld_drill_svs01：copied/bound/postverified 各恰一次（重跑不重复）
ok    §2-3 sld_drill_tif01：copied/bound/postverified 各恰一次（重跑不重复）
ok    §2-3 copied 后杀进程：journal 留证，重跑幂等复用 staging
ok    §2-4 sld_drill_inflight01（inflight.tif）不可读且不在 ready 列表（state=failed）
ok    §2-4 sld_drill_link01（linked.svs）不可读且不在 ready 列表（state=failed）
ok    §2-4 sld_drill_noown01（no-owner.svs）不可读且不在 ready 列表（state=legacy）
ok    §2-4 sld_drill_miss01（gone.svs）不可读且不在 ready 列表（state=failed）
ok    §2-4 retain_history（缺文件）→ failed+reason
ok    §2-4 孤儿文件不建行不进列表（只隔离报告）
ok    §2-5 旧分享领取人（usr_bob）不能读同展示名新资产 sld_zkBaSFMOhVrA
ok    §2-5 新资产 owner 正常可读（不受 tombstone 影响）
ok    §2-5 迁移不碰 tombstone（deleted 行保留冻结别名）
ok    §2-6 verify：go（无违规/incomplete）
ok    §2-6 ALICE 配额「不等于」被 failed 隔离原因完整披露（delta=48）
ok    §2-6 BOB 配额差额=deleted 不退款桶（精确归因披露；delta=4096）
ok    §2-6 tombstone×重生交叉验证无复活
```

## 结论

- 断言合计 40，失败 0。演练通过。

