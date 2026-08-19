# Sample TMA Score（插件能力层 P1 示例插件）

演示插件契约的**双向能力**：本插件在 manifest 中用 `provides` 声明一个只读
服务端能力 `slide_summary`，由独立后端（`backend/app.py`，Flask）实现
`POST /capabilities/slide_summary`。平台安装时把它登记进能力注册表，
HistoPilot agent 起跑时经 `/api/ai/run` 网关注入为额外工具，调用统一走平台
dispatch 端点（`/api/plugin/v1/dispatch/dev.sample.tma/slide_summary`），
**插件后端地址不暴露给任何消费方**（docs/plugin-capability-layer-design.md D1/D2）。

## 结构

- `manifest.json`：`manifestSchemaVersion=1.1.0`（provides 属可选新字段，minor
  bump 不破坏老平台）；`provides[0]` 声明 `slide_summary`（read-only，
  requiredPermissions 收窄枚举，参数 JSON Schema 子集）。
- `backend/app.py`：插件后端。健康检查 `GET /healthz`；能力端点
  `POST /capabilities/slide_summary`，**强制校验 `X-Dispatch-Principal`**
  （平台附加的唯一可信主体头），2xx 返回 `{"result": <json>}`。
- `ui/main.js`：占位（本示例重点是服务端能力；UI 侧可后续按
  `plugins/sdk/ui/bridge-client.js` 的 HostBridge 用法扩展）。

## 运行

```sh
# 1. 起插件后端（监听 127.0.0.1:8061，须与 manifest service.baseUrl 一致）
python3 plugins/sample-tma-score/backend/app.py

# 2. 平台侧安装（owner）——解析 provides 并登记能力注册表（fail-closed：
#    manifest 校验失败即安装失败）
curl -X POST /api/admin/plugins/install -H 'Content-Type: application/json' \
     -d '{"plugin": "sample-tma-score"}'
```

可选平台回调（让 slide_summary 汇总真实尺寸/mpp/标注数，而非降级占位）：

```sh
PT_PLATFORM_URL=http://127.0.0.1:8000 \
PT_INSTALLATION_ID=<安装行 installation_id> \
PT_INSTALLATION_SECRET=<安装凭证明文> \
python3 plugins/sample-tma-score/backend/app.py
```

## 来源策略（manifest sha256 pin）

本插件目录名 `sample-tma-score` 是来源策略的 plugin key，平台在
`plugins/source-policy.json` 中 pin 了本 manifest 的 sha256；改动
`manifest.json` 后需重算并同步该 pin：

```sh
shasum -a 256 plugins/sample-tma-score/manifest.json
```

hash 不匹配 → 安装被来源策略拒绝（403 `source policy mismatch`）。
