# fix-2026-09-11：供应商余额抓取的可诊断性修复

## 背景

`POST /api/admin/v1/billing/provider-balance/refresh`
（`app.py` `admin_v1_billing_provider_balance_refresh`，约 6804-6903 行）
手动抓取 DeepSeek `GET /user/balance`。2026-09-03 起快照不再更新。
已核实 DeepSeek 官方文档：该端点路径与响应 schema（`balance_infos[]`/
`currency`/`total_balance` 等、金额为十进制字符串）**未变化**，changelog
无相关条目——即失败原因未知，而现有代码让真实原因无法被诊断。

## 问题（本次只修这三点）

1. **节流掩盖错误**：`_provider_balance_refresh_state["last_ok_attempt"]`
   在发请求**之前**盖章，且失败路径不重置——一次失败后 60 秒内重试只得
   `429 refresh_throttled`，真实错误被节流错误覆盖。
2. **错误不进日志**：`_fail()` 只 `logger.warning` 一个类别码；HTTP 状态码
   只出现在前端 message；`except Exception` 兜底（约 6858-6859）完全不记异常
   对象；DeepSeek 的错误响应 body 全部丢弃。
3. （已确认是有意设计，**不改**）：失败不写伪造快照、GET 继续返回最后成功
   快照 + age_seconds、24h 软警告横幅。

## 设计

### 1. 节流区分成功/失败

- `_provider_balance_refresh_state` 增加 `last_fail_attempt`（进程内，与现
  状一致的内存态）。
- 规则：距上次**成功** < 60s（`PROVIDER_BALANCE_REFRESH_MIN_INTERVAL_SECONDS`
  不变）→ 429；距上次**失败** < 10s（新常量
  `PROVIDER_BALANCE_REFRESH_FAILURE_RETRY_SECONDS = 10.0`）→ 429。
- 成功：盖 `last_ok_attempt`、清 `last_fail_attempt`；失败：盖
  `last_fail_attempt`，**不动** `last_ok_attempt`。
- 请求发出前不再预盖章。429 message 里带上还需等待的秒数，文案中文。

### 2. 失败日志可诊断

- `_fail()` 增加可选的 `http_status` 与 `resp_excerpt` 参数：
  `app.logger.warning("provider balance refresh 失败（%s, HTTP %s）：%s", ...)`
  ——body 截断到 300 字符。DeepSeek 错误 body 不含我们的 api_key（key 只在
  请求头），可记；但仍**不得**记录任何请求头。
- `except Exception` 兜底改为 `app.logger.exception(...)`。
- 给前端的 `{error:{code,message}}` 契约不变（code 词表不变，message 可
  附带 HTTP 状态码，现状已如此）。

### 3. 快照/展示语义不变

不写伪造余额、不自动清空旧快照、UI 展示逻辑（KPI 卡 + 24h 横幅 + 按钮旁
状态行）全部不动。

## 不做

- 不加自动定时抓取（保持手动触发）；
- 不改 `parse_balance_to_nano` 的严格性（金额必须是十进制字符串——与官方
  文档一致）；
- 不动 UI 文件。

## 测试与验收

- `tests/` 新增或扩展用例（找现有的 provider-balance 相关测试文件，
  遵循其 conftest/fixture 惯例）：
  - 失败后立即可按 10s 节奏重试，不再被 60s 窗口误伤；
  - 成功后 60s 内重复刷新仍 429；
  - 失败路径产生日志（caplog 断言 warning/exception 被记录，且不含 api_key）。
- 跑相关测试文件全绿；`git diff` 只触及 `app.py` 与测试文件。
