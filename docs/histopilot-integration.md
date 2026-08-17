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

`AI_SIDECAR_URL` 与 `AI_INTERNAL_TOKEN` 只保留为旧一体仓迁移别名。

## 4. 升级与回滚

- 平台和插件分别使用 SemVer。
- `pluginContractVersion` 与 `bridgeProtocolVersion` 独立协商，不能用产品版本代替。
- 升级 HistoPilot 前备份它自己的 session volume；平台数据无需复制。
- 回滚只需恢复旧 HistoPilot 镜像与 bundle。PathTogether 不跟随回滚。

## 验收

1. 停止 HistoPilot 后，PathTogether Viewer、标注、分享和评论仍正常。
2. 未授权插件无法读取 region 或写入标注。
3. HistoPilot 重启后能够恢复自己的 session。
4. bundle 缺失时平台不显示 AI 入口且首页无 404 资源。
