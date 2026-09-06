# B0 基线核验（2026-09-06T02:09Z 起执行）

## 仓库状态
- PathTogether HEAD: 75837363f11a216c2832f78605f26f4be4915ee8, branch wip/ser8-dev, git status --short 干净
- HistoPilot HEAD: b9d780596059547cc70e4cf639f430ff280fd343, branch wip/ser8-dev, git status --short 干净
- 注意：PathTogether/.worktrees/ 下存在多个历史 worktree 副本（pt-sidebar/pt-rect/pt-viewer-bridge/pt-owner-workspace），
  本次全部忽略，仅在主 checkout 工作。

## 与任务书 §2 的偏差（以代码为权威）
1. 任务书说 PT/.venv-image-review 已装依赖 —— 实际不存在。按 §8.4 用本机 python3.12(conda 3.12.2) 重建：
   python -m venv .venv-image-review && pip install -r requirements.txt -r requirements-dev.txt
2. 依赖版本（实测，与文档编写时 Pillow 11.3.0 不同）：
   - Python 3.12.2；Pillow 12.3.0（jpeg lib 6.2）；openslide-python 1.4.6（openslide-bin 4.0.1.2）
   - tifffile 2024.5.22；Flask 3.1.3；numpy 2.5.2；psycopg 3.3.5；pytest 9.1.1；pytest-cov 7.1.0
   - Node v23.11.0（HP engines >=22.19.0 满足；PT >=20 满足）
   - 基准/测试全部使用此环境；不做生产依赖替换。

## 编码调用点审计（主 checkout，全部核对）
- slide_render.py:1303-1356：TILE_ENCODER_VERSION="display-jpeg-v2"；native q82/4:2:0；multichannel q95/4:4:4；
  display_jpeg_params(image_mode, quality=None)；encode_display_jpeg(img,*,image_mode,quality=None)→(bytes,params)
  ——img.save(..., quality=q, subsampling=s)，无 optimize/progressive（=False 默认）。
- app.py:1198 JPEG_QUALITY=int(env or 82)；app.py:1284-1297 _tile_fp_key ← display_jpeg_params(image_mode)（默认常量82，
  不读 env）＝任务书所记"键 q82 字节 q<env>"缺陷确认；app.py:1300-1311 _tile_cache_lookup 双模式试键；
  app.py:1313-1318 _encode_tile_jpeg ← JPEG_QUALITY(env)。
- app.py:12023 region endpoint(api_slide_region) encode_display_jpeg(quality=85) — AI/网页 region 路径，保持不变。
- app.py:12608 派生图(_derivative系) encode_display_jpeg(quality=DERIVATIVE_JPEG_QUALITY=85) — AI 导数路径，保持不变。
- app.py:8906 api_slide_thumbnail：thumb.save(JPEG, quality=90)（隐含采样）。
- app.py:3514 demo tile route；app.py:8717 主站 tile route —— 均 public,max-age=31536000,immutable。
- share_server.py:61 JPEG_QUALITY(env)；321-338 与主站同形缺陷；1055 share tile route immutable；
  1239 share_slide_thumbnail save(q90)。
- tiles 缓存：app.py 内存 LRU（TILE_CACHE_MAX=3000 数量上限，无 bytes 预算）；share_server 独立 LRU+TTL（同形状键）。
  slide_cache.FileSignature(dev,ino,size,mtime_ns) + read_stable + generation 已具备（§5.1 复用基础）。
- HTML 入口（/、/demo、/s/<token>）无显式 Cache-Control（浏览器启发式缓存）→ 需修。
- JS 资源以 ?v=YYYYMMDDx 版本化；本次需 bump 并新增 viewer-encoding.js。
- minPixelRatio=0.4 出现在 viewer-core.js:17（集中默认）、app.js:669、demo.js:89、share.js:213（页内兜底副本）。

## 前端架构（B4 依托）
- 三入口共用 static/channel-controls.js 的 createChannelController：inline custom TileSource
  （createDeepZoomTileSource：width/height/tileSize/tileOverlap/min/maxLevel + getTileUrl=adapter.tileUrl(...)）。
  epoch 纪律（ctrl.epoch++，晚到响应丢弃）；409 slide_revision_conflict → refreshInfo 一次重建；
  multichannel_disabled → 隐藏面板回退 DZI。
- adapter=static/app-mode.js（official/demo）：tileUrl(id,level,x,y,renderToken)；demo thumbnailUrl 恒 ""。
  share.js 自建同接口 adapter。
- RGB/flag关 → plan.kind="legacy"（DZI URL）；multichannel → plan.kind="render"（inline TileSource + token）。
  B4 画质档对 RGB 需要 inline TileSource 路径（DZI XML 不携带 query），能力存在时 RGB 也走 inline TileSource。
- app.js 近期改动（不得回退）：emitSlideOpened（name|revision 去重，channelReopening 轻量路径补发）、
  state.slide.revision=info.asset_revision。已有 JS 测试 slide-opened-*.test.ts 保护。
- app.py:15018 已有 "Cache-Control": "no-cache" 用例（诊断/代理响应），tile 响应未涉及。

## AI 契约现状（不可破坏）
- region 端点：choose_read_level → read_region → LANCZOS → encode_display_jpeg(q85, mode 采样) → base64。
  read_level/upsampled/magnification/render_context_fingerprint 回显。HP http-client 优先二进制（Accept: application/octet-stream），
  JSON/base64 兼容（src/platform/http-client.ts:364-446）。gen2-444 导数/检查点键不变。

## 真实样本盘点
- 本机 PathTogether/uploads：空；~/svs-viewer/uploads：仅 4-8 字节测试 stub（不算真实样本）。
- homePC:~/svs-viewer-demo-data/uploads（ssh 只读）：
  - 真实 RGB HE SVS（可授权样本，共 20+ 张）：1-NC肝(1) 244MB、1-NC肠(1) 24.7MB、2-MC肠(1) 22.8MB、
    3-PC肠(1) 25.7MB、4-LD肠(1) 24.6MB、6-HD肠 26.2MB、2-MC肝(1) 473.7MB 等
  - 真实多色荧光 OME-TIF（6 通道，CYX）：E04260806007-LZM-Control.ome.tif 1.675GB (6×47031×47104)、
    E04260806007-WKL-CDStricture.ome.tif 1.418GB (6×45312×45298)
  - TCGA SVS 4 张 + smokebot/aitester 副本（smokebot-TCGA-49-AAR4.svs 497.5MB）
  - smoke_cyx4.ome.tiff / smoke_named.ome.tiff（4ch 96×64）＝合成测试 stub，不算真实样本
- 结论：RGB 真实样本 ≥3 满足；真实荧光 2 张 < 任务书 §7.1 要求的 3 张 → G7 荧光样本量不足将如实标 blocked
  （第 3 张不得用合成冒充）。基准样本已拷贝至 套件根 bench-samples/（不在任何 git 仓内）。
