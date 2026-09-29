# C5-B 百度导入插件（pathtogether-baidu-import）实现报告

- 日期：2026-09-29。基线：slide-id-refactor @ `963ddf1` + C5 平台子代理在途
  改动（producer import API / 桥 / 0077）。合同：
  `docs/slide-tools/c5-producer-import-contract.md`（冻结稿 v1，§8 裁决未重开）。
- 范围：**仅** `plugins/pathtogether-baidu-import/` 与其测试（本地 stub 套件
  + `tests/test_baidu_import_plugin_integration.py`）。未触碰任何平台文件
  （app.py / 迁移 / manifest.schema.json / sdk / source-policy.json 等均为
  平台子代理或用户在途改动）。

## 1. 交付物

```
plugins/pathtogether-baidu-import/
├── manifest.json          # id=dev.pathtogether.baidu-import；permissions=["slide:import"]
├── README.md              # 安装/grant 引导/运行/桥契约（as-built 对齐）/容量策略
├── ui/main.js             # grant 引导面板（纯 DOM 最小实现；无网络请求）
├── worker/                # 后端进程（python3 -m worker；运行期零平台 import）
│   ├── __main__.py        # 入口：健康端点线程 + 主循环 + SIGTERM 优雅停机
│   ├── config.py          # env 配置（含受管根派生）
│   ├── errors.py          # §1.8 retryable 表 + 三层错误类型
│   ├── http_client.py     # 退避重试 + Retry-After 遵从 + response_lost 语义
│   ├── platform_client.py # token 交换 + imports.* + 桥（claim 归一化）
│   ├── journal.py         # 每任务持久日志（0600；write_token 终态抹除）
│   ├── states.py          # 插件任务序
│   ├── source/bdpan.py    # bdpan CLI 适配器（自 baidu_adapter.py 移植）+重试限速
│   ├── source/fake.py     # fake 源（绝不触网；PT_ENV=production 恒不可用）
│   ├── convert.py         # slide-transform 封装 + 格式分类 + channel.json + 嗅探
│   ├── cleanup.py         # 受管根清理 + cleanup-confirm 退避
│   ├── grants.py          # grant 登记（project→pig_id；0600）
│   ├── item_task.py       # 单条目编排状态机（journal 驱动续跑）
│   └── batch_driver.py    # claim/heartbeat/report + 租约 fencing + 取消
└── tests/                 # stub 平台 + 77 用例（见 §4）
tests/test_baidu_import_plugin_integration.py   # 真实 app 集成（2 用例）
```

## 2. worker 架构

**进程形态**：单进程主循环（claim → 逐条目串行推进）+ daemon 心跳线程
（lease/3 续租；失败只置停止标志，绝不抛出）+ 健康端点线程
（`PT_HEALTH_PORT`，manifest.service.health 指向 `/healthz`）。安装凭证只经
env 进内存；write_token 只进 journal（0600，终态抹除，绝不进日志/报告——
异常 `repr` 只含 code）。

**journal 格式**：`<work>/<installation_id>/journal/<import_id>.json`，原子写
（tmp+fsync+os.replace，0600），字段含 item/batch/grant/idempotency_key/
stage/source_*/artifact_*/delivered_offset/write_token/receipt/cleanup_status/
terminal。**不在**受管任务根内（清理确认要求该根为空；journal 是安装级簿记）。
begin 前先落 `pending-<item_id>` 记录（收窄「begin 响应已到、token 未落盘」
的崩溃窗口：重启重发同键 begin——首次受理返回新 token；幂等重放不重发
token → 条目失败 `write_token_lost`（本地清理照做，平台侧由超期 sweep
收口），绝不凭空再 begin 第二个任务）。

**状态机**（`queued→downloading→transforming→validating→delivering→
awaiting_receipt→cleanup_pending→done`；失败/取消也过 cleanup_pending；
`published+cleanup_failed` 保留发布结果不重传，write_token 保留供清理重试）：

- **queued**：grant 查找（缺 → `grant_missing` 不 begin）→ 格式分类 →
  begin（带 `baidu_item_id` 绑定条目；Idempotency-Key 头与体一致）。
- **downloading**：scratch 补占（= 源大小；413 即停）→ 转存（副本对账：
  已在且大小一致不重转存）→ 下载入受管根 `source/` → KFBF 伴随
  channel.json 落 `source/<stem>_kfbf/Annotations/`。
- **transforming**：scratch 补占到 ≈2× 源（峰值）→ 共享原生核心转换
  （kfb→.tif / kfbf→.ome.tif + `--channel-json`；native 不转换）。
- **validating**：sha256 + size + 本地魔法嗅探（TIFF 族含 BigTIFF）。
- **delivering**：实际产物 > declared 时先 final topup（§4.3 只增；413 即停
  零传输）→ 逐块写（块 ≤ chunk_max_bytes、逐块 sha256、offset 断线续传：
  409 offset_conflict → status 取权威 confirmed_offset 续传）→ commit
  （declared_sha256 交叉核对）。
- **awaiting_receipt**：commit 响应丢失（TransportError.response_lost）→
  status 轮询读回终态回执。
- **cleanup_pending**：停写 → 删受管根 → cleanup-confirm（幂等；非空 409 →
  退避重删重确认；有界耗尽 → cleanup_failed）→ ready 项网盘副本清理
  （仅本条目；云端分享源/用户本机原件不动）→ journal 终态 + token 抹除
  （cleaned 时）。

**容量策略**（§4）：begin 在下载前（受管根按 import_id 派生）；native 条目
declared_size = 枚举精确值；convert 条目产物大小不可预知 → declared 从 1
起、交付前按实际 topup（topup 是合同机制、只增不减——避免为 shrink 重做
下载+转换）。scratch：begin 报下载副本字节，转换前补占到 2× 源（保守峰值，
§8 裁决 7 允许：无倍率硬上限，对账兜底）。

**桥客户端（§6.3，与平台 as-built 对齐）**：claim 响应（`{claimed,
batch:{id, lease:{lease_token}}, items:[{id,...}]}`）归一化为驱动器形态
（`_normalize_claim`——stub 平台同走该归一，两侧同源）；heartbeat
`{ok:false}` → LeaseLost 安静放弃；report 走 `/api/plugin/v1/baidu/items/
<id>/report` 的 `fields` 嵌套 + 白名单，插件任务序经 `_PLATFORM_STAGE_MAP`
映射为平台条目枚举（done→ready 等）。

## 3. 测试矩阵覆盖（全部实跑）

| 合同行 | 用例（文件::用例） | 结果 |
|---|---|---|
| T1（插件侧全链路） | test_pipeline.py::test_t1_happy_path_kfb_and_kfbf（KFB+KFBF+channel.json→native 转换→交付→scratch 释放恰一次/受管根空/journal 终态） | PASS |
| T1 集成行 | tests/test_baidu_import_plugin_integration.py::test_worker_full_chain_against_real_app（真实 PG：slide ready、项目关联、final consumed/scratch released SQL 断言） | PASS |
| T12（重启续跑） | test_pipeline.py::test_t12_restart_mid_delivery_resumes_without_duplicate_begin（begin 恰 1 次；权威 offset 续传无重复字节） | PASS |
| T12 窗口 | test_t12_crash_before_begin_restarts_cleanly / test_t12_crash_after_begin_response_documented_window（token 丢失窗口 → write_token_lost，不二次 begin 建任务） | PASS |
| T14/T16（进程停/桥） | test_worker_main.py::test_worker_main_healthz_and_graceful_shutdown（子进程全链路+SIGTERM rc=0）；集成::…lease_fence（真实 claim_batch 重领 → report 被 fence → 安静放弃） | PASS |
| T3（回执幂等） | test_t3_lost_commit_response_receipt_via_status（commit 断连 → status 读回；publish 恰 1）；test_t3_commit_replayed_returns_same_receipt | PASS |
| T5（offset 恢复） | test_t5_offset_conflict_recovery（权威 offset=100 续传；staging == 产物逐字节） | PASS |
| T6（校验和） | test_t6_declared_checksum_mismatch_fails_closed（422 无 intent 仍 writing→插件取消收口；publish=0）；test_platform_client.py::test_write_chunk_sha_mismatch_raw_http（块 409 可重发） | PASS |
| T7/T17（容量） | test_t17_quota_refusal_on_scratch_stops_before_transfer / …on_final_topup…（413 → write=0 零传输）；test_platform_client.py::test_write_requires_exact_offset_and_topup（413 size_exceeded → topup 后可续） | PASS |
| T18（限流） | test_t18_rate_limited_write_waits_and_succeeds（真 HTTP 429+Retry-After=1s，计时 ≥0.9s）；test_platform_client.py::test_transport_rate_limit_honours_retry_after / test_429_real_http_honoured | PASS |
| T9（撤销/停用） | test_t9_grant_revoked_before_begin_no_begin（begin 被拒那次之后再无动作、write=0、无任务）；test_t9_plugin_disabled_midflight_stops（401 → 条目停非终态可续跑；重启用后完成，begin 仍 1）；test_t9_grant_missing_no_begin_at_all | PASS |
| T13（清理） | test_t13_cleanup_not_verified_retries_until_empty（残迹注入 → 409×2 → 重删重确认 → cleaned）；test_t13_published_cleanup_failure_keeps_result_no_reupload（cleanup_failed：begin/commit/publish 恰 1、scratch 不释放、token 保留）；test_cleanup_confirm_idempotent_repeat（释放恰一次） | PASS |
| T2（幂等域） | test_platform_client.py::test_begin_idempotent_replay_no_second_write_token（同键同载荷不重发 token；异载荷 409） | PASS |
| 源适配器 | test_source_and_convert.py（bdpan 纯逻辑/重试/限速/白名单；fake 源；真 CLI 转换） | PASS |
| journal/manifest | test_journal_manifest.py（0600/0700、原子写、pending→attach、strip_secret、manifest 结构 + states 转移） | PASS |
| 驱动器 | test_pipeline.py::test_driver_*（取消不 begin、租约被夺安静放弃、无批次 None、阶段回写） | PASS |

计数（2026-09-29 实跑，证据 `docs/review-evidence/slide-tools/C5/plugin/results/`）：

- 插件本地套件（stub 平台）：**77 passed**（`pytest-plugin.txt`）。
- 真实 app 集成：**2 passed**（`pytest-integration.txt`）。
- 合计 **79 passed，0 failed，0 skipped**（本机原生 CLI 已构建；未构建时
  依赖 CLI 的用例按既有仓库惯例 skip）。

## 4. 测试基建

- **stub 平台**（`tests/stub_platform.py`，进程内 ThreadingHTTPServer）忠实
  实现合同可测面：幂等域（同键重放不重发 write_token/异载荷 409）、offset
  门 + expected_offset、写前容量闸、逐块 sha、topup 只增、commit 平台自算
  sha + 魔法嗅探 + 回执幂等 + **响应丢失注入**（受理后断连）、cancel 互斥、
  scratch total/delta 幂等、cleanup-confirm 真实列受管根（非空 409 +
  residual_bytes + 近期残迹注入钩子）、限流（429+Retry-After，suffix 匹配
  只限 write）、grant 撤销/过期/项目不匹配 reason、installation 停用（401）、
  桥（claim SKIP-LOCKED 近似 + lease fence + report 白名单——**与平台
  as-built 同形态**）。
- 合成夹具：`gen-kfb`（580×300）/`gen-kfbf`（600×400，与既有用例同参）+
  合成 channel.json；**绝不使用真实百度账号**（fake 源永不触网；
  `PT_ENV=production` 下 fake capabilities 恒 unavailable）；私有样本未使用。

## 5. 平台侧缺口（我需要但不能改的）

> **验收（2026-09-29）**：1、3 已由验收方在平台侧补上（claim 视图含 `fs_id`；heartbeat 返回
> `cancel_requested`）；4 记为合同勘误。验收另修 6 处（批次收口、重试复用绑定资产、重试幂等键、
> 已发布条目重跑重放、心跳瞬时错误与租约丢失区分、取消不重报已完成条目），见
> `docs/review-evidence/slide-tools/C5/acceptance.md`。

1. **桥条目视图缺 `fs_id`**（`baidu_import_store._plugin_item_view`）——无
   既有副本时插件无法转存，worker 以 `fs_id_missing` 显式失败。集成测试以
   「副本已转存」对账路径覆盖。建议：`_plugin_item_view` 加
   `"fs_id": row["fs_id"]`。
2. **KFBF 伴随 channel.json 无下发通道**——插件客户端已支持条目字段
   `companion_fs_id`/`companion_name`（枚举发现伴随文件时下发即生效）；
   平台侧枚举/条目目前不带该字段。过渡期伴随文件可作为分享内独立候选
   入批（平台语义内可解）。
3. **heartbeat 不回 `cancel_requested`**——插件仅在 claim 时看到
   cancel_requested；运行中取消要等重领/下轮 claim。客户端已兼容读取该
   字段（stub 亦有该行为测试）。
4. 合同 §6.3 写的是 `items/<id>/report`（在 batches 下）；平台实现为
   `/api/plugin/v1/baidu/items/<id>/report`。插件按平台 as-built 对齐
   （README「桥契约」记录）；建议合同勘误对齐。

## 6. 未做 / 已知限制

- **cleanup_failed 的后续重试**（验收后：同一条目下次被推进时会先补做未确认清理；无条目推进时
  仍不主动重扫）未完全自动化（主循环不重扫终态 cleanup_failed
  记录；journal 保留 write_token 供将来 sweep——列为后续 reconcile 工作）。
- bdpan 生产适配器只做了纯逻辑/重试封装的单测（monkeypatch 子进程执行，
  不起真 CLI 不触网）；真实 CLI 行为 = 外部门禁（替身成功不宣称真实可用，
  §9.1）。
- UI 是纯引导面板（生成 grants.json 行/env 种子），不接 HostBridge 之外的
  平台回调；「用户在插件 UI 内一步完成授权」需 HostBridge 扩展（合同 §2.2
  明示 C5 不强制桥协议改动）。
- 插件 UI 无 vitest 用例（面板无逻辑分支超出正则校验；js 侧测试归 C5 验收
  全量 vitest 跑——本包不新增 js 文件到 tests/js 域）。

## 7. 来源策略 pin（管理员需加入 plugins/source-policy.json）

```json
"pathtogether-baidu-import": "7c6ea101584b09670b95c8096369df7021b34812fd5fbc888d15f25d5167fa20"
```

（= `sha256sum plugins/pathtogether-baidu-import/manifest.json`；
`plugins/source-policy.json` 属他人在途文件，本包不改。）
