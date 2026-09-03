# PathTogether

轻量的协作式数字病理读片平台。PathTogether 负责 WSI 查看、项目/切片管理、分享、标注、评论、权限和插件宿主；AI 导航已经拆分到独立的 [HistoPilot](https://github.com/solarise94/HistoPilot) 仓库。

> 本项目面向研究、教学和小组协作，不是临床诊断系统或病人管理系统。

## 功能

- OpenSlide + OpenSeadragon WSI 查看，支持 SVS、TIFF、NDPI、MRXS、SCN 等常见格式
- OME-TIFF / SubIFD 金字塔回退读取与 mpp 解析
- 固定物理尺寸 ROI、矩形/箭头/自由描图标注和全分辨率导出
- 项目分组、限时分享、view / annotate / download 三档权限
- 标注评论、revision CAS、AI 标注人工审核和操作日志
- owner / user / guest / sdk-user 四类身份
- JSON、PostgreSQL 与迁移期 dual 后端
- 版本化 Plugin Contract、HostBridge、scoped JWT 和方法级权限
- 外部插件 bundle 目录；平台镜像不再打包或启动 HistoPilot

## 快速开始

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
mkdir -p uploads share-data plugin-bundles
python app.py
```

打开 <http://localhost:8000>。分享服务可在另一个终端运行：

```bash
python share_server.py
```

### 容器

```bash
podman build -t pathtogether -f Containerfile .
podman run --rm -p 8000:8000 \
  -v "$PWD/uploads:/data/uploads" \
  -v "$PWD/share-data:/data/share" \
  -v "$PWD/plugin-bundles:/data/plugins" \
  pathtogether
```

PathTogether 容器只运行平台。HistoPilot 使用自己的镜像、进程、版本和 session volume。

## 安装 HistoPilot

1. 从 HistoPilot release 下载 `histopilot-pathtogether-plugin-<version>.tar.gz`。
2. 解压成 `${PLUGIN_BUNDLES_DIR}/histopilot/`，其中应包含 `manifest.json` 和 `ui/`。
3. 独立启动 HistoPilot service，并设置其 PathTogether 地址与插件 installation credential。
4. 在 PathTogether 中启用对应 installation。兼容 UI 会在检测到外部 bundle 后出现。

完整步骤见 [HistoPilot 集成说明](docs/histopilot-integration.md)。从旧版 HistoPilot/`svs-viewer` 一体仓迁移见 [拆仓迁移指南](docs/migration-from-monorepo.md)。

## 关键环境变量

| 变量 | 默认值 | 用途 |
|---|---|---|
| `PORT` | `8000` | 管理端端口 |
| `UPLOAD_DIR` | `~/svs-viewer/uploads` | WSI 文件目录 |
| `SHARE_DATA_DIR` | `~/svs-viewer/share-data` | 平台数据目录 |
| `PLUGIN_BUNDLES_DIR` | `${SHARE_DATA_DIR}/plugins` | 独立插件 release bundle 安装目录 |
| `STORAGE_BACKEND` | `json` | `json` / `postgres` / `dual` |
| `DATABASE_URL` | — | PostgreSQL 连接串 |
| `BOOTSTRAP_OWNER_LOGIN_ID` | — | 空库首建 owner 的登录账号 |
| `BOOTSTRAP_OWNER_PASSWORD_FILE` | — | 空库首建 owner 的 secret 文件路径 |
| `HISTOPILOT_URL` | `http://127.0.0.1:8055` | HistoPilot 兼容网关目标；仅安装插件时需要 |
| `HISTOPILOT_INTERNAL_TOKEN` | 自动生成 | PathTogether 与 HistoPilot 的服务间令牌 |

注册模式（closed / invite_only）由 owner 后台的 `platform_settings.registration_mode` 运行时管理，不再有环境变量开关。

## 仓库边界

- PathTogether 拥有：切片、Viewer、标注、评论、分享、用户、权限、审计、Plugin Contract。
- HistoPilot 拥有：导航 Agent、模型接入、prompt、session、SSE、compaction、视觉缓存和实验。
- HistoPilot-DSH 拥有：DSH 工具注册、配置与 HistoPilot 调用适配，不复制导航逻辑。

两边只通过版本化 HTTP Plugin Contract 通信。HistoPilot 不直接读取 WSI 文件或平台数据库；PathTogether 不读取 HistoPilot canonical session。

部署配置请使用上表中的正式名称（HISTOPILOT 系）。

## 开发与测试

```bash
python3 -m pytest tests -q
npm ci
npm run test:js
```

Python tests cover the platform and compatibility gateway. The small Vitest
suite owns the platform-side HostBridge, permission gate, and plugin SDK
contracts that moved out of HistoPilot during the repository split.

插件协议定义位于 `plugins/manifest.schema.json` 和 `plugins/sdk/`。`plugins/sample-annotator/` 是不依赖 HistoPilot 的最小示例插件夹具（2026-09-01 起不再注入产品 Viewer）。

## License

MIT

---

PathTogether is a lightweight collaborative WSI viewer and plugin host. Agentic slide navigation lives in the separately versioned [HistoPilot](https://github.com/solarise94/HistoPilot) project.
