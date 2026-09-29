# PathTogether Baidu Import（C5-B 插件包）

百度网盘 → PathTogether 的**机器生产者导入插件**（C5 合同
`docs/slide-tools/c5-producer-import-contract.md` §6.2/§6.3）：百度凭证/
下载/重试限速、转换执行（共享原生核心 `slide-transform`）、源副本清理
迁入本插件；平台只保留用户面 API（`/api/remote-imports/baidu/*`）与统一
发布（producer import channel）。

- **独立后端进程**（`worker/`）：只用 HTTP 与平台通信
  （`/api/plugin/v1/*`，Bearer scoped JWT），运行期**不 import 任何平台
  模块**（app.py / baidu_* / slide_* 一概不依赖）。
- **受管任务根**（合同 §5，平台派生）：一切任务文件只写在
  `SHARE_DATA_DIR/plugin-work/<installation_id>/imports/<import_id>/`；
  清理确认前停写；本地任务日志在
  `<root>/<installation_id>/journal/`（0600，存 write_token，终态抹除）。
- 插件任务序：`queued → downloading → transforming → validating →
  delivering → awaiting_receipt → cleanup_pending → done`（失败/取消也过
  可重试清理；`published + cleanup_failed` 保留已发布结果不重传）。

## 结构

```
manifest.json        # permissions: ["slide:import"]（需平台侧枚举支持，见下）
ui/main.js           # grant 引导面板（纯 DOM，最小实现）
worker/              # 后端进程（python3 -m worker）
  config.py          # env 配置
  errors.py          # §1.8 错误码 retryable 表
  http_client.py     # 退避重试 + Retry-After 遵从
  platform_client.py # token 交换 + imports.* + 百度桥
  journal.py         # 每任务持久日志（T12 重启续跑）
  source/bdpan.py    # bdpan CLI 适配器（自 baidu_adapter.py 移植）+重试限速
  source/fake.py     # 测试 fake 源（绝不触网）
  convert.py         # slide-transform CLI 封装（KFB/KFBF + channel.json）
  cleanup.py         # 受管根清理 + cleanup-confirm 退避
  item_task.py       # 单条目编排（状态机）
  batch_driver.py    # 桥 claim/heartbeat/report 循环
tests/               # stub 平台（进程内 HTTP）+ 全链路测试
```

## 安装（owner）

1. 平台侧 C5 平台面（migration 0077、`/api/plugin/v1/imports/*`、grant
   端点、`slide:import` scope、百度桥）已由 C5 平面子代理落地。本包
   manifest 申请 `slide:import`（`dev.pathtogether.baidu-import`——平台
   `_BAIDU_IMPORT_PLUGIN_IDS` 桥白名单即按该 id 匹配，manifest id 不得改动）。
2. **来源策略 pin**（`plugins/source-policy.json`，键 = 目录名）：管理员
   把下面一行加进该 JSON（本包不改动该文件——source-policy.json 属他人
   in-progress 文件）：

   ```json
   "pathtogether-baidu-import": "7c6ea101584b09670b95c8096369df7021b34812fd5fbc888d15f25d5167fa20"
   ```

   （= `sha256sum plugins/pathtogether-baidu-import/manifest.json`；
   manifest 改动后需重算并同步 pin，hash 不匹配 → 安装被来源策略拒绝。）

3. 安装（owner）：`POST /api/admin/plugins/install` body
   `{"plugin": "pathtogether-baidu-import"}`；admin 批准时确认
   `approved_scopes` 含 `slide:import`（申请不建立信任——未批准的 scope
   在安装时被拒，合同 §2.1）。
4. 记下安装行 `installation_id` 与一次性 `secret`（轮换入口
   `/api/admin/plugins/<installation_id>/rotate-secret`）。

## 用户授权（grant 引导，合同 §2.2）

用户（项目 owner）在平台用户面创建导入委托：

```sh
curl -X POST /api/plugin/import-grants \
  -H 'Cookie: <用户会话>' -H 'X-CSRF-Token: <token>' \
  -H 'Content-Type: application/json' \
  -d '{"plugin_id": "dev.pathtogether.baidu-import",
       "project_id": "prj_…", "ttl_seconds": 86400}'
# → {"grant_id": "pig_…"}   # 一次性展示；默认 TTL 24h
```

把 grant_id 交给插件（插件 UI 面板可生成登记行）：

- 写 `<SHARE_DATA_DIR>/plugin-work/<installation_id>/grants.json`（0600）：
  `{"<project_id>": "pig_…"}`；或
- env 种子 `PT_IMPORT_GRANTS=prj_…:pig_…`。

grant 撤销/过期后：begin/新 write 块被拒（403 `import_grant_invalid`）；
在途清理仍可用（write_token 与任务同寿命，合同 §2.4）。

## 运行 worker（与平台同机）

```sh
PYTHONPATH=plugins/pathtogether-baidu-import \
PT_PLATFORM_URL=http://127.0.0.1:8000 \
PT_INSTALLATION_ID=<安装行 id> \
PT_INSTALLATION_SECRET=<安装凭证明文> \
SHARE_DATA_DIR=<平台共享数据目录> \
BAIDU_CONNECTOR_HOME=<bdpan 认证目录（BDPAN_CONFIG_DIR）> \
SLIDE_TRANSFORM_BIN=slide-transform-core/target/release/slide-transform \
python3 -m worker
```

- 健康端点：`http://127.0.0.1:8062/healthz`（`PT_HEALTH_PORT` 可调，
  与 manifest `service.baseUrl` 一致）。
- 转换用共享原生核心（`scripts/build_slide_transform.sh` 构建）；KFB→
  `.tif`、KFBF→`.ome.tif`（伴随 `<名>_kfbf/Annotations/channel.json` 存在
  时经 `--channel-json` 传入）；native 单文件不转换按原样交付。
- 主要 env（全部可调）：`PT_BAIDU_LEASE_SECONDS`（桥租约，缺省 1800）、
  `PT_BAIDU_CLAIM_POLL_SECONDS`、`PT_CLEANUP_MAX_ATTEMPTS`、
  `BAIDU_SOURCE_MAX_ATTEMPTS` / `BAIDU_SOURCE_BACKOFF_BASE` /
  `BAIDU_SOURCE_MIN_INTERVAL`（源侧重试/限速）、`PT_HTTP_*`（传输层）。
- 测试装配：`BAIDU_SOURCE=fake`（内存源，绝不触网；生产环境绝不设置）。

## 容量/交付策略（合同 §4）

- begin 在下载前（受管根按 import_id 派生，scratch 初值 = 下载副本字节）；
  转换前补占峰值（≈2×源）；交付前按实际产物大小 final 补占
  （`POST …/topup`；转换产物大小不可预知，declared_size 从 1 起按 §4.3
  只增不减——topup 是合同机制，无 shrink）。
- 任一 413（配额/容量拒绝）即停：不再发任何 write 块，转失败清理路径。
- 清理：可靠回执 → 停写 → 删受管根 → `cleanup-confirm`（幂等，退避重试；
  `cleanup_not_verified` → 重删重确认）。百度网盘侧只删本条目副本，绝不
  触碰云端分享源文件或用户本机原件。

## 桥契约（§6.3；与平台 as-built 对齐）

对齐对象：`baidu_import_store.plugin_claim_batch` /
`plugin_heartbeat_batch` / `plugin_report_item`。

- `POST /api/plugin/v1/baidu/batches/claim`
  req `{"lease_seconds"}`（worker_id 平台侧从 JWT 派生）→
  `{"claimed": false}`（200）或 `{"claimed": true, "batch": {id, state,
  target_project_id, cancel_requested, lease: {worker_id, lease_token,
  lease_expires_at}, share_url, extraction_code}, "items": [{id, batch_id,
  name, stage, error_code, slide_id, source_size(str), ...}]}`。
  客户端把该视图归一为 `batch_id`/`lease_token`/整型 `source_size`
  （`platform_client._normalize_claim`；stub 平台同走该归一）。
- `POST /api/plugin/v1/baidu/batches/heartbeat`
  req `{"batch_id", "lease_token", "lease_seconds"?}` → `{"ok": bool}`
  （租约被夺/批终态 → ok=false，客户端安静放弃）。
- `POST /api/plugin/v1/baidu/items/<item_id>/report`
  req `{"batch_id", "lease_token", "fields": {stage, error_code?}}`——
  字段白名单 stage/error_code/ingest_token/source_sha256/slide_name/
  project_associate_state/transfer_task_id/staging_path；stage 白名单为
  平台条目枚举（queued/transferring/downloading/validating/converting/
  ingesting/ready/failed/cancelled）。插件任务序经
  `batch_driver._PLATFORM_STAGE_MAP` 映射回写；import↔item 的 slide 绑定经
  begin 的 `baidu_item_id`（§6.3 条目发布不另开端点）。

已知缺口（待平台补齐，见 C5-B 报告）：桥条目视图未下发 `fs_id`——无既有
副本时 worker 以 `fs_id_missing` 显式失败；KFBF 伴随 channel.json 的
`companion_fs_id` 下发同为缺口（客户端已支持该字段，平台下发即生效）。

## 测试

```sh
TMPDIR=$PWD/.gate-tmp .venv/bin/python -m pytest \
  plugins/pathtogether-baidu-import/tests -q
```

stub 平台（进程内 HTTP）忠实实现合同端点的可测面：幂等、offset、
write_token、§1.8 错误码、清理核验、限流（429+Retry-After）、grant
撤销/停用注入、回执丢失注入。复跑指南与证据见
`docs/review-evidence/slide-tools/C5/plugin/RERUN.md`。
