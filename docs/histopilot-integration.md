# HistoPilot 集成

PathTogether 与 HistoPilot 是两个独立发布、独立升级的服务。平台可在完全没有 HistoPilot 的情况下运行全部人工读片、分享和协作功能。

## 1. 安装 UI bundle

下载 HistoPilot release 中的 PathTogether 插件包并解压：

```bash
mkdir -p plugin-bundles/histopilot
tar -xzf histopilot-pathtogether-plugin-0.1.0.tar.gz \
  -C plugin-bundles/histopilot --strip-components=1
```

目录结构应为：

```text
plugin-bundles/histopilot/
├── manifest.json
└── ui/
    ├── api.js
    ├── bridge-client.js
    ├── config-panel.js
    ├── main.js
    ├── renderer.js
    ├── sessions.js
    └── sse.js
```

将 `PLUGIN_BUNDLES_DIR` 指向 `plugin-bundles`。外部目录优先于仓库内置示例插件。

## 2. 建立 installation credential

PathTogether 首次启动会建立 HistoPilot installation。owner 可在“插件管理”中轮换凭证。把返回的 installation id 和 secret 仅配置到 HistoPilot 服务端：

```bash
PLUGIN_INSTALLATION_ID=<installation-id>
PLUGIN_HISTOPILOT_SECRET=<secret>
PATHTOGETHER_URL=http://pathtogether:8000
```

secret 不得进入浏览器、URL、日志或 Git。

## 3. 启动 HistoPilot

参照 HistoPilot 仓库的容器说明启动独立服务。PathTogether 兼容网关通过 `HISTOPILOT_URL` 指向它：

```bash
HISTOPILOT_URL=http://histopilot:8055
HISTOPILOT_INTERNAL_TOKEN=<shared-random-token>
```

HistoPilot 侧设置同一个 `HISTOPILOT_INTERNAL_TOKEN`。生产部署应把两个服务放进私有容器网络，不直接发布 HistoPilot 的内部端口。

## 4. 升级与回滚

- 平台和插件分别使用 SemVer。
- `pluginContractVersion` 与 `bridgeProtocolVersion` 独立协商，不能用产品版本代替。
- 升级 HistoPilot 前备份它自己的 session volume；平台数据无需复制。
- 回滚只需恢复旧 HistoPilot 镜像与 bundle。PathTogether 不跟随回滚。

## 5. AI 引擎配置：DeepSeek 官方直连与 Files（owner）

平台是 AI 配置的唯一所有者：owner 在「插件管理 / AI 配置」写入平台配置，
普通 user 只读并统一使用平台配置。方案全文与灰度/回滚门槛见
[deepseek-files-api-research.md](deepseek-files-api-research.md)。

目标配置（官方直连 + Files 全量）：

```json
{
  "provider_kind": "deepseek_official",
  "base_url": "https://api.deepseek.com",
  "model": "deepseek-v4-flash-vision-exp",
  "api_protocol": "openai",
  "api_key": "<DeepSeek API key>",
  "image_transport": "deepseek_files",
  "files_rollout_percent": 100,
  "files_ttl_seconds": 86400,
  "prompt_cache_mode": "auto"
}
```

字段说明：

| 字段 | 约束 |
|---|---|
| `provider_kind` | `deepseek_official`（官方直连）/ `generic`（CPA 兼容，仅人工回滚） |
| `base_url` | 官方模式锁定 `https://api.deepseek.com`（canonical，不带 `/v1`） |
| `model` | 官方模式锁定 `deepseek-v4-flash-vision-exp` |
| `api_protocol` | 官方模式锁定 `openai` |
| `image_transport` | `inline`（缺省）/ `deepseek_files`（仅官方 + OpenAI + vision-exp） |
| `files_rollout_percent` | 0–100 整数（缺省 0）；按 session id 稳定分桶灰度 |
| `files_ttl_seconds` | 3600–2592000（缺省 86400 = 24 小时）；永不省略 |
| `prompt_cache_mode` | 官方模式必须 `auto` |

上线分两步（都只访问官方）：先 `image_transport=inline` 验证官方聊天无回归，
再切 `deepseek_files` 并按 10 → 50 → 100 放量 `files_rollout_percent`。

**安全门禁（保存时校验，不满足拒绝保存 API key）**：

- `cryptography`/Fernet 必须可用——官方模式**不降级明文落盘**；
- 数据卷中 `ai_config.json` 与 `ai_secret.key` 权限必须为 **0600**（服务端先
  尝试自动修正，失败即拒绝）；key 以 `enc:` 密文保存。

**计费语义**：Files API 的文件**存储/上传免费**；模型推理（含图片）**仍按
token 计费**（每张图片缩放后最多 384 token）。两者不得混同。

**回滚开关**（首选，只改配置、不改 key/不重启会话）：

```json
{"image_transport": "inline", "files_rollout_percent": 0}
```

这会停止新上传并继续使用官方聊天；进程内 file cache 等待 TTL/LRU 清理。
只有 owner 明确决定恢复旧供应链时才人工恢复备份 CPA 配置
（`provider_kind=generic`）；401/402/数据合规问题应停服修复，不是切回 CPA
的理由。owner 配置面板（外部 bundle `ui/config-panel.js`）已提供
「DeepSeek 官方」选项：官方模式锁定 canonical 三项（base URL / 模型 / 协议）
为只读展示，Files 与 TTL 说明常驻。

## 验收

1. 停止 HistoPilot 后，PathTogether Viewer、标注、分享和评论仍正常。
2. 未授权插件无法读取 region 或写入标注。
3. HistoPilot 重启后能够恢复自己的 session。
4. bundle 缺失时平台不显示 AI 入口且首页无 404 资源。
