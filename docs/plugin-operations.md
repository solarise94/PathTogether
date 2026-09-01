# 插件运营配置（Stage 5）

面向**平台运维 / owner**的插件子系统配置速查。涵盖四类版本字段、来源策略
（manifest sha256 pin）、配额与限流旋钮、安装凭证管理。设计原则：**最小可用**——
不做 SaaS 级 PKI / 插件商店，来源策略随代码 / 镜像版本化，重启生效。

权威字段定义见 `plugins/manifest.schema.json` 与拆分文档
`docs/pathtogather-histopilot-platform-plugin-upgrade.md` §7。

---

## 1. 四类版本字段与协商

manifest（`${PLUGIN_BUNDLES_DIR}/<id>/manifest.json`；内置示例仍在 `plugins/`）有**四个相互独立**的版本字段，平台按
N/N-1 major 协商：

| 字段 | 含义 | 何时 bump major | 协商行为 |
|------|------|-----------------|----------|
| `manifestSchemaVersion` | manifest 文件自身的字段结构 | 语法不兼容 | major ∈ {当前, 当前-1} 才加载 |
| `pluginContractVersion` | capability API / 领域类型 / 错误码 | 破坏 API/语义 | major ∈ {当前, 当前-1} 协商成功 |
| `bridgeProtocolVersion` | iframe HostBridge 消息协议 | 消息不兼容 | major ∈ {当前, 当前-1} 协商；运行时每条消息强制同 major |
| `pluginVersion` | 产品 / 镜像 / bundle 版本 | 正常产品 SemVer | 不自动改变其它三者，仅回显 |

平台当前声明：`pluginContractVersion=1.0.0`、`bridgeProtocolVersion=1.0.0`
（单一来源 `plugins/sdk/manifest.py`）。运行时协商结果由
`GET /api/plugin/v1/capabilities`（需 plugin JWT）回显：`supportedContractMajors` /
`supportedBridgeMajors` 含当前接受的 major 列表。校验器 `validate_manifest` /
`negotiate_versions` 为纯 stdlib 实现，不依赖 `jsonschema`。

> 注意：manifest 的 `id`（反向域名，如 `com.pathtogether.histopilot`）与 bridge
> 凭证域的 `pluginInstallationId`（目录名简写，如 `histopilot`）**不同**。来源策略
> 与静态路由统一以**安装目录名**为 plugin key（`histopilot` /
> `sample-annotator`），凭证域的 installation_id 是另一套（见第 4 节）。

---

## 2. 来源策略（manifest sha256 pin + owner 批准）

### 2.1 设计

- **不写数据库**，不做 PKI。策略文件 `plugins/source-policy.json` 随仓库 / 镜像分发
  （Containerfile `COPY plugins/ plugins/` 已含），owner 改文件即批准，**重启生效**。
- 每个 plugin key（目录名）pin 其 `manifest.json` 的 sha256。运行时计算磁盘 manifest
  的实际 sha256 与 pin 值比对，不等即拒绝来源（防 manifest 被篡改 / 漂移）。
- 代码域（来源策略）与凭证域（`plugin_installations`）分开：来源策略管"这段 bundle
  能不能加载"，凭证管"这个安装能不能调 API"。

### 2.2 文件格式

`plugins/source-policy.json`（策略文件仍随平台版本发布，bundle 可以位于外部目录）：

```json
{
  "sample-annotator": "<sha256-of-plugins/sample-annotator/manifest.json>"
}
```

- key = 安装目录名；value = 期望的 manifest sha256（hex）。
- value 为 `null` = 显式放行（不校验 hash）；key 缺失 = 未 pin = 放行。

### 2.3 重算 hash（更新 manifest 后必须同步）

```sh
shasum -a 256 "${PLUGIN_BUNDLES_DIR}/histopilot/manifest.json"
shasum -a 256 plugins/sample-annotator/manifest.json
```

把输出的 hex 填回 `plugins/source-policy.json`。**忘记同步会导致来源策略拒绝**——
`tests/test_plugin_source_policy.py` 有防漂移守卫（断言文件内 hash 与磁盘实际一致）。

### 2.4 放行 / 拒绝行为

执行点（全部带明确错误，不带病运行）：

| 执行点 | 来源拒绝时的行为 |
|--------|------------------|
| `GET /plugins/<id>/ui/<filename>` 静态路由 | `403` JSON：`{error:"forbidden", plugin_id, reason}`，reason ∈ `source policy mismatch` / `manifest missing` |
| `index` 渲染 histopilot bundle | `histopilot_ui_enabled()` flag **与** 来源策略与逻辑：来源拒绝 → 不注入 bundle（与 flag=0 同等静默降级） |
| `sample_plugin_context()` | 来源拒绝 → `enabled=False`，index 不注入 sample 脚本与权限表 |
| 管理 API `/api/admin/plugins*` | **不判来源策略**（凭证域，见第 4 节） |

未知 plugin 目录（无 manifest / 无目录）在静态路由先 404，不进入来源判定。

### 2.5 dev 模式

策略文件缺失（或 env `PLUGINS_SOURCE_POLICY_FILE` 指向不存在路径）/ 不可解析 →
返回空策略 = **全放行**，启动日志一行 warning。便于本地开发 / CI 无策略文件时跑通。

env 覆盖（测试 / 临时切策略用）：

```
PLUGINS_SOURCE_POLICY_FILE=/path/to/source-policy.json
```

> 策略在进程内模块级缓存（`functools.lru_cache`），改文件后需**重启进程**生效。

---

## 3. 配额与限流旋钮

四个 env 旋钮（均为**磁盘读取前**的门槛，超限零解码 / 零磁盘 I/O，`slide_cache` 不触碰）：

| env | 默认 | 含义 | 调大影响 | 调小影响 |
|-----|------|------|----------|----------|
| `PLUGIN_REGION_MAX_PIXELS` | `16777216`（4096²） | 单次 region 请求像素上限（入口即拒） | 单请求可取更大区域，内存峰值升 | 大区域被拒，客户端需缩小 |
| `PLUGIN_REGION_PIXEL_BUDGET_PER_MIN` | `268435456`（≈16×4096²/min） | per `installation_id` 滑窗 1 分钟像素预算 | 吞吐升，CPU/内存压力升 | 高频取图更快触 429 |
| `PLUGIN_REGION_MAX_CONCURRENT` | `4` | region 通道进程级并发信号量 | 并发升，解码线程/CPU 升 | 排队更早，429 更频 |
| `PLUGIN_RATE_LIMIT_PER_MIN` | `120` | v1 全能力端点统一速率桶（per `installation_id`） | API 吞吐升 | 调用更早触 429 |

### 3.1 429 语义

所有配额 / 限流超限统一返回 `429` 信封：

```json
{
  "error": {
    "code": "rate_limited",
    "message": "...",
    "retryable": true,
    "details": { "reason": "...", ... }
  }
}
```

并带 HTTP 头 `Retry-After: <秒>`（至少 1）。`details.reason` 取值（按触发点）：

| `reason` | 触发旋钮 | 含义 |
|----------|----------|------|
| `single_request_pixels` | `PLUGIN_REGION_MAX_PIXELS` | 单次 region 像素超上限（入口拒） |
| `concurrency` | `PLUGIN_REGION_MAX_CONCURRENT` | region 并发信号量耗尽 |
| `pixel_budget` | `PLUGIN_REGION_PIXEL_BUDGET_PER_MIN` | 滑窗 1 分钟像素预算耗尽 |
| `rate_limit` | `PLUGIN_RATE_LIMIT_PER_MIN` | 全能力端点统一速率桶耗尽 |

### 3.2 并发信号量行为

`_PLUGIN_REGION_CONCURRENCY_SEM` 是 `threading.BoundedSemaphore`，**非阻塞** `acquire`：
拿不到槽立即返回 429（reason=`concurrency`），不排队等待。仅保护插件 region 通道，
不影响平台自身读片。信号量在 `finally` 中 `release`，异常路径不泄漏槽。

### 3.3 门槛顺序（全部在读盘前）

1. 单请求像素上限（`PLUGIN_REGION_MAX_PIXELS`）→ reason `single_request_pixels`；
2. 内容协商 / 切片指纹冲突（409，非 429）；
3. 并发闸（`PLUGIN_REGION_MAX_CONCURRENT`）→ reason `concurrency`；
4. 滑窗像素预算（`PLUGIN_REGION_PIXEL_BUDGET_PER_MIN`，拿到并发槽后、读盘前计入）
   → reason `pixel_budget`；
5. 之后才 `_get_slide` 读盘解码。

全能力端点的速率桶（`PLUGIN_RATE_LIMIT_PER_MIN`，reason `rate_limit`）在鉴权之后、
端点逻辑之前消费，独立于上述 region 通道。

---

## 4. 安装凭证管理（凭证域）

> 与第 2 节来源策略**分开**：凭证管"这个安装能不能调 `/api/plugin/v1/*`"。

histopilot 安装在启动时由 `_bootstrap_plugin_installations()` 自动创建（若不存在），
明文 secret 以 `0600` 落盘到 `SHARE_DATA_DIR`。管理 API **仅 owner**（`_require_owner`）：

| 方法 / 路径 | 作用 |
|-------------|------|
| `GET /api/admin/plugins` | 列出全部安装（含 sidecar `/healthz` 可达性快照） |
| `POST /api/admin/plugins/<installation_id>/rotate-secret` | 轮换凭证：旧 secret 立即失效，新明文仅本次返回 |
| `POST /api/admin/plugins/<installation_id>/enable` | 启用安装 |
| `POST /api/admin/plugins/<installation_id>/disable` | 停用安装 |

**disable 即时生效**：JWT 校验每请求回查 `installation.enabled`，disable 后该安装全部
在途 JWT 立即失效（下一个 `/api/plugin/v1/*` 请求 401）。安装的创建经启动期 bootstrap
（自动），无独立 POST create 端点。

插件后端换 token：`POST /api/plugin/v1/auth/token`，JSON
`{"installation_id": ..., "secret": ...}` → `access_token`（scoped plugin JWT，
`sub`=installation_id）。

---

## 5. 示例插件（已退役）

`sample-annotator` 已从产品前台退役（2026-09-01）。`/` 不再注入示例插件脚本或
权限表；`SAMPLE_PLUGIN_ENABLED` **被忽略**，设 `1` 也不会打开 Viewer 浮层。
插件目录与 `source-policy.json` pin 仍保留，供 SDK/manifest 契约测试和静态资源
路由使用，不是给终端用户的功能。

histopilot 是内置特权插件，开关为 `HISTOPILOT_UI_ENABLED`（默认 `1` 开启）：

```
HISTOPILOT_UI_ENABLED=0     # 关 histopilot UI（人工读片静默降级）
```
