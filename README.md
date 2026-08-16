# HistoPilot · 病理切片智能导航

[English](#english) | [中文](#中文)

**HistoPilot** 是一个**早期实验项目（early-stage demo）**，探索 agent 在大型病理切片（WSI）上的
**自主导航**问题：goto 跳转、渐进变倍、坐标语义理解。平台本体（PathTogether）提供一套简易的
**读片 / 阅片 / 分享 / 协作**功能——WSI 查看（OpenSeadragon）、固定物理尺寸 ROI 框选与导出、
三档权限分享、评论线程、审计日志；AI 读片助手以**插件**形态接入（pi sidecar，
lite/branch 分级会话、批注/深读对话），经 PlatformClient 契约与平台解耦。

> 注：GitHub 仓库已更名为 `HistoPilot`；容器镜像名与内部路径仍沿用 `svs-viewer`（既有部署未改）。

**HistoPilot** is an **early-stage demo** exploring **autonomous agent navigation** over
whole-slide pathology images (WSI): goto jumps, progressive zoom, and coordinate semantics.
The platform (PathTogether) ships a lightweight **reading / review / sharing / collaboration**
layer — WSI viewing (OpenSeadragon), fixed physical-size ROI boxing & export, three-level
permission shares, comment threads, and an audit log. The AI reading assistant plugs in as a
**plugin** (pi sidecar, lite/branch graded sessions, fork chats) and talks to the platform
only through the PlatformClient contract.

> Note: the GitHub repo has been renamed to `HistoPilot`; the container image name and
> internal paths remain `svs-viewer` (existing deployment, unchanged).

![share page](docs/screenshot-share.jpg)

---

## 中文

### 功能

- **WSI 查看**：OpenSlide + OpenSeadragon Deep Zoom，支持 svs / tif / tiff / ndpi / mrxs / vms / vmu / scn / bif / svslide 等格式，滚轮缩放、拖拽平移、旋转、双击放大
- **OME-TIFF 支持**：OpenSlide 打不开的（OME-）TIFF 自动回退到内置 tifffile+zarr 阅读器（`slide_io.py`），识别 SubIFD 金字塔并解析 OME-XML 的 PhysicalSize（mpp）；MRXS 等多文件格式连同数据目录打包 zip 上传，服务端安全解压
- **四层身份**：owner（部署者/超管：管理用户、平台 AI 配置、公开切片与邀请）/ user（注册账户：可上传维护自己的切片、创建分享，可用平台 AI 或自带 API key）/ guest（分享链接访客：按分享权限查看/标注，设备以 HMAC 签名 cookie 标识）/ sdk-user（插件服务身份，代理已授权用户调用平台 API）；注册开关 `REGISTRATION_OPEN` 默认关闭、由 owner 手工添加用户；`ADMIN_PASSWORD` 用于引导首个 owner（session 7 天、IP 防爆破锁定）；与分享端同端口路径分流（`/s/...` 走分享页，其余走管理门户），外网一个 HTTPS 端口即同时提供分享与管理入口
- **项目管理**：切片按项目分组（一个项目 = 一个用户/批次的一组切片），未归类切片单列
- **ROI 选区**：固定物理尺寸 6mm / 6.5mm 方框（边长像素 = mm × 1000 / mpp），随缩放锚定、可拖动；一键导出 level-0 全分辨率 PNG
- **mpp 真实坐标尺**：依次读取厂商元数据 → TIFF 分辨率标签 → 倍率估算 → 手动输入
- **限时分享（三档权限）**：勾选多张切片或整个项目 → 选时效（6h/24h/3d/7d/自定义）与权限（view / annotate / download）→ 生成链接；访客**只能**看到被分享的切片集，无上传/删除
- **署名标注与评论**：访客填标签后保存 ROI 位置；标注可挂**评论线程**（revision CAS 并发保护，冲突 409）；内网按切片/项目查看"被谁标记了几处"，点击跳转定位、或一键叠加全部标注框（按署名人着色）
- **AI 标注审核与审计**：AI 落的标注带 `review_status`（pending / accepted / rejected），人工可采纳/驳回并保留 20 条修改历史；操作审计日志 owner 可查（`/api/admin/audit`，脱敏输出）
- **手机 UI**：分享页响应式 + 触屏捏合缩放
- **性能**：512px 渐进式 JPEG 瓦片、服务端 LRU 瓦片缓存、immutable 浏览器缓存、缩略图底图层（慢网不白屏）、macOS 风格管理界面
- **存储后端可切换**：`STORAGE_BACKEND=json|postgres|dual`（默认 json，行为与拆分前一致）；PG 模式配 `DATABASE_URL`，附 json→PG 迁移 CLI（dry-run / apply / verify / rollback）
- **插件系统**：HistoPilot AI 导航以插件形态接入（`plugins/histopilot/`）——manifest 版本协商（N/N-1 兼容）、sha256 来源 pin、方法级权限门；`plugins/sdk/` 通用 SDK + `plugins/sample-annotator/` 示例插件演示第三方接入（`SAMPLE_PLUGIN_ENABLED` 默认关）

### 架构

```
┌──────────────┐ 内网   ┌─────────────────────┐
│ owner/user   │ ─────→ │ 平台 app.py (:8000)  │ 用户库/切片/分享/标注/评论/审计；AI 凭据解析
│ 浏览器        │        │ STORAGE_BACKEND=     │
└──────────────┘        │ json|postgres|dual   │
                        └──────────┬──────────┘
                                   │ loopback /api/ai/* 代理 ⇄ /internal/ai/* 回调（共享内部 token）
                        ┌──────────┴──────────┐
                        │ AI sidecar (:8055)   │ Node + pi：Agent loop/compaction/会话存储/SSE
                        │  └ HistoPilot 插件    │ 经 PlatformClient 契约回调平台（读图/落标注…）
                        └──────────┬──────────┘
                                   │ ROLE=platform|sidecar 可拆双容器；AI_SESSIONS_DIR 独立
┌──────────────┐ 公网   ┌──────────┴──────────┐
│ 分享访客浏览器 │ ─────→ │share_server(:38000) │ /s/* 分享页（可开 TLS），其余路径分流管理门户
└──────────────┘        └─────────────────────┘
```

进程共享：切片目录 `uploads/`（分享端只读）与 `SHARE_DATA_DIR`（json 模式：`shares.json`
fcntl.flock 互斥；postgres/dual 模式：入 PG）。AI 会话存独立的 `AI_SESSIONS_DIR`（Stage 4-3
起与平台数据分离）。AI 读片助手由 Flask（:8000）+ Node sidecar（:8055，仅 127.0.0.1；绑定
非 loopback 时必须配内部 token，否则拒绝启动）双进程协作，详见下文「AI 读片助手」与
`docs/ai-session-architecture.md`。公网暴露可配合 frp/nginx/caddy 等任意反向代理。

### 快速开始

#### 方式一：pip + venv

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt   # openslide-bin 自带动态库，无需系统安装

mkdir -p uploads share-data
python app.py                     # 管理端 http://localhost:8000（不含 AI sidecar）
python share_server.py            # 分享端 http://localhost:38000（另开终端）

# 可选：切换 PostgreSQL 后端（默认 json；dual 为双写镜像，供迁移/验证）
#   STORAGE_BACKEND=postgres DATABASE_URL=postgres://user:pass@127.0.0.1:5432/db python app.py
#   python scripts/migrate_json_to_pg.py --help    # json→PG 迁移 CLI

# 需要 AI 读片助手时，改用一键脚本（同时起 Flask + Node sidecar）：
#   cd sidecar && npm install --registry=https://registry.npmjs.org && cd ..
#   ./dev_ai.sh                   # 管理端 :8000 + sidecar :8055；详见下文「本地开发」
```

#### 方式二：Podman / Docker（推荐，含开机自启示例）

```bash
podman build -t svs-viewer -f Containerfile .

# 管理端
podman run -d --name svs-viewer -p 8000:8000 \
  -v $PWD/uploads:/data/uploads:Z \
  -v $PWD/share-data:/data/share:Z \
  -e SHARE_BASE_URL=https://slides.example.com:18767 \
  svs-viewer

# 分享端（只读挂载切片；TLS 可选）
podman run -d --name svs-share -p 38000:38000 \
  -v $PWD/uploads:/data/uploads:ro,Z \
  -v $PWD/share-data:/data/share:Z \
  -v $PWD/certs:/data/certs:ro,Z \
  -e SHARE_TLS_CERT=/data/certs/fullchain.crt \
  -e SHARE_TLS_KEY=/data/certs/privkey.key \
  svs-viewer python share_server.py
```

### 环境变量

| 变量 | 默认 | 说明 |
|---|---|---|
| `PORT` | 8000 | 管理端端口 |
| `SHARE_PORT` | 38000 | 分享端端口 |
| `UPLOAD_DIR` | `~/svs-viewer/uploads`（容器内 `/data/uploads`） | 切片目录 |
| `SHARE_DATA_DIR` | `~/svs-viewer/share-data`（容器内 `/data/share`） | 项目/分享/标注数据 |
| `SHARE_BASE_URL` | `http://localhost:38000` | 生成分享链接的外部访问前缀 |
| `SHARE_TLS_CERT` / `SHARE_TLS_KEY` | — | 提供后分享端直接以 HTTPS 运行 |
| `ADMIN_USERNAME` | `admin` | 管理员登录用户名 |
| `ADMIN_PASSWORD` | — | 设置后管理端启用登录认证（内网同样需要） |
| `REQUIRE_ADMIN_AUTH` | — | 设为 `1` 时密码为空、`<...>` 或文档 sentinel `<REPLACE_WITH_STRONG_PASSWORD>` 则拒绝启动（demo/公网 fail-closed） |
| `ADMIN_SESSION_COOKIE_SECURE` | — | 设为 `1` 时管理端 session cookie 带 Secure（外部 HTTPS 终止时开启；SSH 隧道 HTTP 不要设） |
| `STORAGE_BACKEND` | `json` | 存储后端：`json` / `postgres` / `dual`（双写镜像，迁移/验证用）；PG 需配 `DATABASE_URL` |
| `REGISTRATION_OPEN` | `0` | 开放注册开关（owner 也可在用户管理里切换）；默认关闭，由 owner 手工添加用户 |
| `SAMPLE_PLUGIN_ENABLED` | `0` | sample-annotator 示例插件开关（还需通过 manifest sha256 来源 pin） |
| `SECRET_KEY` | 自动生成并持久化到数据目录 | Flask session 密钥 |
| `JPEG_QUALITY` | 82 | 瓦片 JPEG 质量 |
| `TILE_CACHE_MAX` | 3000 | 服务端瓦片缓存片数 |
| `TILE_CACHE_TTL` | 3600 | 分享端瓦片缓存 TTL（秒） |
| `AI_SIDECAR_*` / `AI_INTERNAL_TOKEN` 等 | — | AI 读片助手 sidecar 相关变量，见下文「AI 读片助手」一节 |

### 使用

1. **上传**：管理端左侧"上传切片"或拖拽文件到查看区；MRXS 等多文件格式请把 `.mrxs` 与同名数据目录打包成 zip 上传；也可直接拷入 `uploads/` 刷新即可
2. **建项目**："＋新建项目" → "＋切片"把切片归入项目
3. **分享**：项目行悬停点 ↗（或勾选切片分享）→ 选时效 → 复制链接发给用户
4. **标注回流**：访客打开链接 → 填"标记人/标签" → 框 ROI → 保存选区（可继续跟评论线程）；owner/授权用户在切片行看到"标记 N·M 人"徽章，点"标记"面板跳转定位，或"显示全部标记"叠加全部框

### AI 读片助手（owner / 注册用户，pi sidecar 架构）

工具栏的「✨ AI」按钮打开 AI 读片助手面板，让大模型通过 OpenAI 兼容的 function-calling 接口操控虚拟显微镜：自动从低倍概览扫描、抓取快照、落矩形标注并给出中文总结。

**架构**（双进程，详见 `docs/ai-session-architecture.md`）：

```
浏览器 ──HTTPS──→ Flask app.py (:8000)        ──loopback──→ Node sidecar (:8055)  ──HTTPS──→ cpa 网关
                  鉴权/切片/标注库/配置         ←─loopback──  pi Agent loop/compaction          LLM 模型
                  /api/ai/* 透传代理到 sidecar               会话存储/SSE 事件总线
                  /internal/ai/* 回调端点（sidecar 读图/落标注）
```

- **Flask（`app.py`）**：鉴权、切片 IO、标注库、AI 配置（`ai_config.json`，api_key Fernet 加密）；`/api/ai/*` 字节级透传代理到 sidecar；`/internal/ai/*` 是 sidecar 回调读图/落标注/取变更的内部端点（共享 token 互信）。
- **sidecar（`sidecar/`，Node 22 + pi 0.84.0）**：跑 pi Agent loop、compaction、会话存储与 SSE 事件总线；**不读 `ai_config.json`**，每请求的引擎配置由 Flask 注入 body `config` 字段。
- 两者共享 `SHARE_DATA_DIR/ai_internal.token`（内部 token，可由 env `AI_INTERNAL_TOKEN` 覆盖）。会话文件**不再**共享：sidecar 存 `AI_SESSIONS_DIR`（Stage 4-3 起独立，同容器由 `docker_entry.sh` 指向 `/data/sidecar-sessions`）。

**配置**（凭据分层，§5.1.2）：
- **owner**：在 AI 面板「设置区」配置**平台 AI**（Base URL / API Key / 模型），写入 `SHARE_DATA_DIR/ai_config.json`（0600，**API Key 不入日志**）——所有注册用户默认可用它跑 AI。
- **user**：可勾选「使用平台官方 API」（平台已配置时生效），或在账号下保存**自有凭据**（Fernet 加密存用户库）；面板顶部会显示当前生效来源（平台 AI / 自己的凭据）。
- **Base URL** 须为 OpenAI 兼容端点（如 `https://api.openai.com/v1`，不含 `/chat/completions` 后缀）；**模型**需支持 vision + tool-calling（如 `gpt-4o`、`gpt-4o-mini` 等）。自带 base_url 入库前经 SSRF 校验（DNS 解析后拒绝回环/私网/云元数据/CGNAT 段）。
- 回显一律脱敏（`api_key_set: true` + 掩码「前4 + \*\*\*\* + 后4」），不回明文；保存时空串=清除、与掩码同值=不变。

**使用**：
- 打开任一切片后，在 AI 面板的任务框输入指令（如「客观扫读这张片：先低倍定位，再高倍确认；描述镜下所见，标出值得关注的区域并总结」），点「开始」。任务框留空时也会使用该默认任务。
- 「判读当前选区」快捷钮会把当前 ROI 框或选中标注的 level-0 坐标写进任务前缀，引导 AI 重点看该区域。
- 运行中以 SSE（`text/event-stream`）实时推送轨迹：`slide_opened` / `agent_thinking` / `text_delta` / `tool_started` / `snapshot_captured` / `observation` / `annotation_created` / `snapshot_reviewed` / `agent_paused` / `agent_retrying` / `agent_finished` / `agent_error` / `session_compacted`。`snapshot_captured` 只推 bbox 与放大倍率（不推图像 base64，省带宽），点击该行可跳转到对应区域。
- 单轮步数上限默认 `max_steps=50`（可在 `ai_config.json` 覆盖，到上限自动暂停，可「▶ 继续」）。
- 模型请求若因**上下文超窗**报错（如 `context_length_exceeded`）：自动压缩一次上下文并重试该次调用（轨迹显示 `session_compacted` + "上下文已压缩并继续"），不中断 run。
- AI 的每个视口在画布上以**青色虚线框**叠加（区别于人工标注的金色实线框）；AI 落的标注会写入标注库（label「AI 建议」），出现在现有标注层与「标记」面板，管理员可正常编辑/删除。
- 「开始」可切为「停止」中途中断（AbortController）；同一 session 同时只允许一个 run。断线重连若事件缓冲已滚过断点，服务端发 `event_reset`，前端自动全量刷新对话状态。
- 图片管线：会话落库的图块是 `image_ref`（只存坐标/指纹，不含 base64）；每次发模型前 sidecar 物化为 base64，并只保留最近 `keep_recent_images`（默认 6）张 + 概览首图，更早的替换为占位文本。

**约束**：
- 所有 `/api/ai/*` 与 `/api/slide/<name>/region` 走 `_require_auth`（owner / 注册用户），并校验对目标切片的查看权限；AI 会话按归属隔离（user 只能续看自己的会话，跨用户 acquire 会 409）。
- AI 调用 OpenAI 兼容端点的请求由 sidecar 发出（api_key 由 Flask 解密后经 loopback 注入），不暴露 Key 给前端。
- `/internal/ai/*` 回调与 sidecar `/run` 等入站端点**双向**共用内部 token（`X-AI-Internal-Token`，仅 `/healthz` 豁免）；sidecar 绑定非 loopback 且无 token 时拒绝启动（fail-closed）。

### 本地开发（AI sidecar）

一键脚本 `dev_ai.sh` 同时起 sidecar（:8055）+ Flask（:8000），Ctrl-C 两个都停：

```bash
# 首次：装 sidecar 依赖（pi 包只在官方 registry，必须 --registry）
cd sidecar && npm install --registry=https://registry.npmjs.org && cd ..

# 仓库根装 Python 依赖（建议在 venv 内）
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

./dev_ai.sh             # 默认：自动 tsc 编译 + 起双进程
./dev_ai.sh --rebuild   # 强制重新编译 sidecar
```

也可手动分开跑（两个终端）：
```bash
# 终端 1：sidecar
cd sidecar && npm run build && node dist/index.js

# 终端 2：Flask
python3 app.py
```

容器内同理由 `docker_entry.sh` 编排：先起 sidecar 等 `/healthz` 就绪（最多 30s），再起 `gunicorn app:app -b 0.0.0.0:8000 -w 2 --threads 8`；任一进程退出则容器退出。

**AI 相关环境变量**（容器/本地通用）：

| 变量 | 默认 | 说明 |
|---|---|---|
| `AI_SIDECAR_PORT` | 8055 | sidecar 监听端口 |
| `AI_SIDECAR_HOST` | 127.0.0.1 | sidecar 监听地址。`--network host` 必须保持 loopback；仅私有容器网络且不发布宿主端口时才设 `0.0.0.0`（此时必须有 `AI_INTERNAL_TOKEN`，否则拒绝启动） |
| `AI_FLASK_URL` | `http://127.0.0.1:8000` | sidecar 回调 Flask 的基础 URL |
| `AI_SIDECAR_URL` | `http://127.0.0.1:8055` | Flask 代理 `/api/ai/*` 到 sidecar 的 URL |
| `AI_SESSIONS_DIR` | `~/.svs-sidecar/sessions` | sidecar 会话存储目录（Stage 4-3 起独立，不再与平台 `SHARE_DATA_DIR` 混放；同容器由 `docker_entry.sh` 显式指向 `/data/sidecar-sessions`） |
| `AI_INTERNAL_TOKEN` | （空 → 读文件） | sidecar ↔ Flask 双向内部 token（Flask `/internal/ai/*` 与 sidecar `/run` 等，除 `/healthz`）；空则两边从 `SHARE_DATA_DIR/ai_internal.token`（0600）读取 |
| `ROLE` | `all` | 容器进程拓扑：`all`（双进程，缺省）/ `platform`（只 gunicorn）/ `sidecar`（只 sidecar，见下） |
| `SHARE_DATA_DIR` | `~/svs-viewer/share-data`（容器内 `/data/share`） | 平台数据目录（AI 配置/密钥文件；会话已迁出到 `AI_SESSIONS_DIR`） |

**测试**：
```bash
# Python（Flask 代理层）
python3 -m pytest tests/ -q

# sidecar（vitest）
cd sidecar && npm test
```

### 安全说明

- 分享端所有路由都校验 token（存在/未撤销/未过期）且切片属于该分享，否则一律 404
- 分享端只读：无上传、无删除、无切片列表之外的任何信息；访客设备以 HMAC 签名 cookie 标识，API 不回传原始标识（防复制 cookie 冒用他人标注）
- 标注/评论并发写带 revision CAS（冲突返回 409）；AI 标注可审核/驳回并保留修改历史
- 自带 AI base_url 入库前 SSRF 校验（拒绝私网/云元数据/CGNAT 段）；sidecar 对平台的回调统一经 ssrf-guard
- 用户库损坏/不可读时平台**拒绝启动**，不会静默降级为「无用户 → 关闭认证」
- 分享链接建议经 HTTPS 暴露（`SHARE_TLS_*` 或前置反代），避免明文 token 被窃听
- 管理端暴露到公网时务必设置 `ADMIN_PASSWORD`（登录认证 + IP 连续失败锁定），并经由分享端 TLS 监听（同端口路径分流）以 HTTPS 提供；demo 部署另设 `REQUIRE_ADMIN_AUTH=1`，且不要在纯 HTTP 公网入口登录

---

## English

### Features

- **WSI viewing**: OpenSlide + OpenSeadragon Deep Zoom (svs/tif/tiff/ndpi/mrxs/vms/vmu/scn/bif/svslide), wheel zoom, pan, rotate
- **OME-TIFF support**: (OME-)TIFF files OpenSlide cannot read fall back to a built-in tifffile+zarr reader (`slide_io.py`) with SubIFD pyramid and OME-XML PhysicalSize (mpp) parsing; multi-file formats like MRXS are uploaded as a zip and extracted safely server-side
- **Four identity tiers**: owner (deployer/superadmin: users, platform AI config, invites) / user (registered account: own slide gallery & shares; platform AI or own API key) / guest (share-link visitor: view/annotate per share permission, HMAC-signed device cookie); registration is off by default (`REGISTRATION_OPEN`); `ADMIN_PASSWORD` bootstraps the first owner; the share server path-routes the same port (`/s/...` → share pages, everything else → admin portal)
- **Projects**: organize slides into projects (one project = one client's slide set)
- **Physical ROI**: fixed 6mm / 6.5mm squares anchored to image coordinates; export full-resolution PNG crops
- **Real scale (mpp)**: vendor metadata → TIFF resolution tags → objective-power estimate → manual input
- **Time-limited shares with three permission levels** (view / annotate / download): share selected slides or a whole project; recipients see only the shared set
- **Named annotations & comments**: recipients label and save ROI positions; annotations carry comment threads with revision-CAS concurrency (409 on conflict); jump to each annotation or overlay all boxes (colored by label)
- **AI annotation review & audit log**: AI annotations carry a review_status (pending/accepted/rejected) with edit history; owner-only audit log at `/api/admin/audit`
- **Pluggable AI**: HistoPilot navigation runs as a plugin (`plugins/histopilot/`) behind the PlatformClient contract — manifest version negotiation (N/N-1), sha256 source pinning, method-level permission gating; `plugins/sdk/` + `plugins/sample-annotator/` demo third-party integration
- **Switchable storage**: `STORAGE_BACKEND=json|postgres|dual` (default json); PG needs `DATABASE_URL`, with a json→PG migration CLI (dry-run/apply/verify/rollback)
- **Mobile UI**: responsive share page with pinch zoom
- **Performance**: 512px progressive-JPEG tiles, server-side LRU tile cache, immutable browser caching, thumbnail backdrop (no white flashes), macOS-style admin UI

### Quick start

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
mkdir -p uploads share-data
python app.py           # admin UI  → http://localhost:8000
python share_server.py  # share API → http://localhost:38000
```

Or with Podman/Docker — see the Chinese section above (commands are identical).

Expose the share server publicly behind any reverse proxy (frp/nginx/caddy) and set
`SHARE_BASE_URL` to the public URL so generated links point to it.

### Security

- Every share route validates the token (exists / not revoked / not expired) and slide
  membership; anything else returns 404
- The share server is strictly read-only; visitor devices are identified by HMAC-signed
  cookies and the raw identifier is never echoed back by the API (blocks cookie-copy
  impersonation)
- Annotation/comment writes use revision CAS (409 on conflict); AI annotations are
  reviewable (accept/reject with edit history)
- User-supplied AI base URLs are SSRF-checked before storage (loopback/private/metadata/CGN
  ranges rejected); sidecar→platform callbacks go through an SSRF guard
- A corrupted/unreadable user store **refuses to start** rather than silently degrading to
  "no users → auth off"
- Serve shares over HTTPS to protect tokens in transit
- When exposing the admin UI publicly, always set `ADMIN_PASSWORD` (login + per-IP
  lockout) and serve it via the share server's TLS listener (same port, path-routed).
  Demo deploys should also set `REQUIRE_ADMIN_AUTH=1` and must not log in over
  plaintext public HTTP.

## Documentation (Chinese)

- `docs/pathtogather-histopilot-platform-plugin-upgrade.md` — platform/plugin split design (v1.5, §19 decision table)
- `docs/ai-session-architecture.md` — AI sidecar session & proxy architecture
- `docs/plugin-operations.md` — plugin operations (versioning / source policy / quotas)
- `docs/demo-deployment.md` — demo deployment notes (dual-container topology)

## License

[MIT](LICENSE) © 2026 solarise94

## Acknowledgements

- [OpenSlide](https://openslide.org/) / [openslide-python](https://github.com/openslide/openslide-python)
- [OpenSeadragon](https://openseadragon.github.io/)
- Test slide: OpenSlide public test data (Aperio CMU-1)
