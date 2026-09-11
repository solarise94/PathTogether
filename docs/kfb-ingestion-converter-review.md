# KFB 上传支持与低开销切片转换器 Review

- 文档状态：可行性评审 + Phase A/B/C 上线合同（2026-09-11）
- 日期：2026-09-11
- 适用范围：PathTogether 上传、切片存储与读取链路
- 本轮实施：Phase A 离线转换器 + 厂商 KFB 校准 + Phase B 后台 job/worker + Phase C 上传 UI。**不**宣称通用 KFB/KFBF；**不**把 `.kfb` 加入 `SUPPORTED_EXTS`（源文件不对 Viewer 列出）。

## 0. 2026-09-11 核实与 Phase A 合同

### 0.1 对照当前代码（仍成立）

- `app.py` `SUPPORTED_EXTS` 仍是 svs/tif/tiff/ndpi/mrxs/vms/vmu/scn/bif/svslide；无 `.kfb`。
- `slide_io.py` `LOGICAL_EXTS` 与上同集，OpenSlide 无 KFB backend。
- `templates/_app_shell.html` 的 `accept` 与 `static/app.js` 上传成功后打开 `file.name` 的行为未变。
- 仅加后缀会在 `slide_io.open_slide` 失败。V2 commit 仍同步校验，不适合承载转换。

### 0.2 样本与布局约束

评审用真实样本（`切片文件夹/26-005817_2026-08-25_14_12_12.kfb`，SHA-256 `17fe1cfde1a6d1f1d8778ea5854db5fd353f657e51003c606b404b60300aab71`）**不在本仓库**。Phase A 必须：

1. 用仓库内合成 fixture（无患者数据）驱动 CI。
2. 若环境变量 `PT_KFB_SAMPLE_PATH` 指向可读文件，额外跑真实样本测试；缺失则 skip，不得伪绿。
3. 未知 magic/version → `unsupported_kfb_variant`，禁止猜测。

合成 fixture 使用下文 **kfb_bf_v1** 布局（与评审样本同一 magic `f1 01 ee ee 4b 46 42 00` 和「JPEG tiled 金字塔」语义）。该布局是 PathTogether 生产 parser 的 **v1 合同**；真实厂商变体在校准前一律 fail-closed。

### 0.3 kfb_bf_v1 磁盘布局（little-endian）

```text
0x00  8s     magic = f1 01 ee ee 4b 46 42 00
0x08  u32    version = 1
0x0C  u32    header_bytes（含 magic，>= 96，<= 4096）
0x10  u32    width_px          # level 0
0x14  u32    height_px
0x18  u32    tile_w            # 必须 256
0x1C  u32    tile_h            # 必须 256
0x20  u32    level_count       # 1..16
0x24  u32    tile_count        # 1..2_000_000
0x28  f64    mpp_x
0x30  f64    mpp_y
0x38  f32    objective         # 例如 20.0
0x3C  16s    scanner_id ASCII NUL 填充（评审样本为 KFPBL40000110032）
0x4C  u32    associated_count  # 0..8
0x50  u64    index_offset
0x58  u32    flags             # bit0=brightfield；其它位必须 0
0x5C  零填充至 header_bytes

index_offset 起连续 tile_count 条，每条 32 字节：
  u32 level
  u32 x_px, y_px            # 该层左上角
  u16 jpeg_w, jpeg_h        # 实际 JPEG 解码尺寸；完整 tile 为 256x256
  u64 payload_offset
  u32 payload_length        # 1..8 MiB
  u32 reserved=0

随后 associated_count 条，每条 48 字节：
  16s name（label/overview/thumbnail）
  u64 payload_offset
  u32 payload_length
  u16 width, height
  u32 reserved=0

JPEG payload：SOI..EOI，offset+length 必须落在文件内、互不要求连续。
```

硬上限：宽/高 <= 200_000；层最短边或最长边 < tile 后不再写入更小层；拒绝负值、重叠异常、越界、残缺 JPEG。

### 0.4 Phase A 交付（离线，不接上传）

| 路径 | 职责 |
| --- | --- |
| `slide_format_registry.py` | 单一格式注册表：`native-single-file` / `native-bundle` / `convert-required` / `unsupported`。KFB=`convert-required`，KFBF=`unsupported`。**不**改 `SUPPORTED_EXTS`。 |
| `kfb/errors.py` | 稳定错误码：`unsupported_kfb_variant` `invalid_kfb_header` `invalid_tile_index` `tile_payload_out_of_bounds` `jpeg_decode_failed` `metadata_missing_required` `conversion_timeout` `conversion_output_too_large` `conversion_disk_low` `conversion_validation_failed` |
| `kfb/parser.py` | mmap/只读解析；checked 算术；返回 header/levels/tiles/associated |
| `kfb/fixture.py` | 合成 kfb_bf_v1（含完整 tile + 边缘残缺 tile + label/overview/thumbnail） |
| `kfb/converter.py` | 混合重封装：256×256 JPEG **原样写入** TIFF tile；不足尺寸的边缘 tile 白底补边后用相容量化表重编码；经典多 IFD 金字塔 BigTIFF（`.tif`）；MPP 写 resolution tags；objective 进 manifest 与 ImageDescription JSON |
| `kfb/manifest.py` | JSON sidecar：source hash、converter 版本、dimensions、levels、mpp、objective、codec、tile size、associated 引用、警告 |
| `scripts/convert_kfb.py` | CLI：`python scripts/convert_kfb.py SRC.kfb OUT.tif`，先写 `OUT.tif.part` 再原子改名；同写 `OUT.tif.manifest.json` |
| `tests/test_slide_format_registry.py` | 注册表与现有扩展名能力一致 |
| `tests/test_kfb_parser.py` | 合成样本解析；损坏 magic/截断/越界/非 JPEG → 稳定码 |
| `tests/test_kfb_converter.py` | 完整 tile 字节一致；边缘 MAE/PSNR 记录；`slide_io.open_slide` 能读全部层与四角；不把整幅 RGB 展开进内存 |

完成标准：离线 CLI + 测试通过。禁止：把 `.kfb` 加入上传入口、Gunicorn 内同步转换、统一转 SVS、删除源文件、KFBF、宣称生产 SLA。

### 0.5 转换算法（不可降级）

1. 解析并校验 index。
2. 每层按 tile 网格写出；缺 tile → `conversion_validation_failed`。
3. `jpeg_w==tile_w && jpeg_h==tile_h`：TIFF 压缩段 = 源 JPEG 字节（禁止二次有损）。
4. 否则：解码 → 贴到白色 256×256 左上角 → 用源量化表/色度采样重编码。
5. 停止条件：最短边或最长边已 < tile size 后不再写更小层。
6. 输出 BigTIFF classic multi-IFD（不是 SubIFD OME），供 OpenSlide generic-tiff 认全层。
7. manifest + associated 字节写入 sidecar 目录或主文件旁 `.kfb-meta/`（Phase A 用 `OUT.associated/` + manifest 引用即可）。

---


## 1. 结论

PathTogether 可以以较低 CPU、内存和磁盘放大成本支持本次提供的明场 KFB 样本，但不应采用“所有格式统一重编码为 SVS”的方案。

推荐策略是：

1. OpenSlide 或现有 TIFF reader 已能直接读取的格式保持原样，不转换。
2. 明场 KFB 转换为经典多 IFD、JPEG tiled、金字塔 BigTIFF，扩展名使用 `.tif`。
3. 完整的 256×256 JPEG tile 原样复制；只对尺寸不足的边缘 tile 解码、白色补边并重新编码。
4. KFBF 等荧光、多通道格式单独转换为 OME-TIFF，保留通道名、显示颜色、曝光和物理像素信息。
5. 转换在独立、限流的后台 worker 中执行，不占用 Gunicorn 请求 worker。

对已提供的真实明场 KFB 样本，混合重封装原型约 1.08 秒生成 9 层 BigTIFF，输出约 219.93 MB；31,650 个完整 tile 与源文件逐字节一致，仅 627 个边缘 tile 需要重编码。OpenSlide 与 PathTogether 均能读取全部 9 层和边缘区域。

因此，可以进入生产转换器的实现 spike，但在补测其他 KFB 版本和 KFBF 前，不能将当前结论宣称为通用 KFB/KFBF 支持。

## 2. 用户目标与评审边界

目标是让上传链路支持 KFB，并为 KFB、OME-TIFF、MRXS 等格式选择低性能消耗的后端处理方式。

本轮完成：

- 当前上传、校验、读取、删除和容器运行方式的源码 review。
- OpenSlide、OME-TIFF、MRXS 与 KFB 的能力边界调查。
- 对一份真实 KFB 文件进行只读头部、索引、层级、tile 和关联图像解析。
- 两类 TIFF 重封装原型及性能、兼容性、像素差异验证。
- 生产转换 worker、任务状态、存储生命周期和验收门槛设计。

本轮未完成：

- 未向 PathTogether 增加 KFB 上传入口。
- 未实现生产级 KFB parser 或 converter。
- 未修改数据库 schema、前端状态机、配额或删除逻辑。
- 未提交、推送、运行 CI、构建镜像或部署。
- 未验证其他扫描仪版本的 KFB 或荧光 KFBF。

## 3. 当前系统审查结果

### 3.1 格式入口分散

当前格式能力由多处分别维护：

- `app.py` 的 `SUPPORTED_EXTS` 控制后端接受的扩展名。
- `slide_io.py` 有独立的逻辑扩展名集合和 OpenSlide/TIFF 分流。
- `templates/_app_shell.html` 维护文件选择器的 `accept`。
- `static/app.js` 决定 V1/V2 上传和成功后打开哪个文件名。

当前允许的主要格式包括 SVS、TIFF、NDPI、MRXS、VMS、VMU、SCN、BIF 和 SVSlide；KFB 不在入口白名单中，OpenSlide 也没有 KFB backend。仅添加 `.kfb` 后缀会在上传后的 `slide_io.open_slide` 校验阶段失败。

生产实现应建立单一的 format registry，由服务端向前端暴露每种格式的能力：

- `native-single-file`
- `native-bundle`
- `convert-required`
- `unsupported`

### 3.2 上传请求不适合直接承担转换

V1 上传在请求内完成临时落盘、切片验证、哈希和正式入库。V2 虽然支持分片上传，但 commit 仍会同步计算哈希并调用切片读取器校验。

分钟级或不可预期的格式转换如果放入 commit 请求，会造成：

- Gunicorn worker 长时间占用。
- 请求超时后客户端状态与后台文件状态分叉。
- 多个转换并发抢占 viewer、AI 和上传所需的 CPU、内存及磁盘 I/O。
- Web 容器重启时无法可靠恢复任务。

因此，V2 commit 对需要转换的文件应返回 HTTP 202 和独立的 `conversion_job_id`，而不是等待转换完成。

### 3.3 当前任务与删除模型不足

现有 upload task 主要表达上传生命周期，不能准确表示：

```text
uploaded
  -> probing
  -> queued
  -> converting
  -> validating
  -> ready
  |-> failed
  |-> cancelled
```

应新增 `conversion_jobs`，而不是把转换状态硬塞进现有 upload task。

当前删除路径主要删除正式主文件，并对 MRXS 特判同名伴随目录。引入转换后，一个资产可能同时包含：

- 原始上传文件。
- 转换中的 `.part` 文件。
- 正式 canonical 文件。
- label、overview、thumbnail。
- 转换日志和元数据 manifest。

删除、配额结算和失败清理必须依据资产 manifest，不能继续只按一个文件名处理。

## 4. 格式处理矩阵

| 输入格式 | 默认处理 | canonical 结果 | 说明 |
| --- | --- | --- | --- |
| SVS | 原样保留 | 原文件 | OpenSlide 原生支持，避免再次 JPEG 压缩 |
| NDPI、SCN、BIF、SVSlide 等 | 原样保留 | 原文件 | 先以真实样本验证后加入 registry |
| MRXS | bundle 原样保留 | `.mrxs` 加同名目录 | 不能当作单文件转换入口 |
| OME-TIFF | 原样保留 | 原文件 | 当前 `TiffFileSlide` 已支持金字塔和多通道 |
| 明场 KFB | 后台转换 | 经典多 IFD pyramidal BigTIFF | 标准 tile 原样搬运，边缘 tile 补边重编码 |
| 荧光 KFBF | 后台转换 | 多通道 OME-TIFF | 必须保存通道名、颜色、曝光等语义 |
| 未知格式 | 拒绝或进入显式 adapter | 不自动猜测 | 不承诺“任意格式万能转换” |

SVS 是 Aperio 的 TIFF 方言。把非 Aperio 文件仅改扩展名或伪造少量 description 并不能形成可靠的 SVS，也容易丢失元数据。PathTogether 自用的明场 canonical 格式没有必要伪装成 `.svs`。

## 5. 真实 KFB 样本证据

### 5.1 样本身份

样本路径：

```text
切片文件夹/26-005817_2026-08-25_14_12_12.kfb
```

只读解析结果：

| 字段 | 值 |
| --- | --- |
| 文件大小 | 221,189,354 bytes |
| SHA-256 | `17fe1cfde1a6d1f1d8778ea5854db5fd353f657e51003c606b404b60300aab71` |
| 文件类型 | KFB brightfield |
| 头部 magic | `f1 01 ee ee 4b 46 42 00` |
| scanner/header 标识 | `KFPBL40000110032` |
| 全分辨率 | 34,013 × 46,152 |
| 扫描倍率 | 20× |
| MPP | 约 0.4841049 µm/px |
| nominal tile | 256 × 256 |
| 声明 tile 数 | 32,285 |
| 实际索引 tile 数 | 32,285 |
| codec | JPEG |

文件还包含 label、overview 和小缩略图。生产转换不能只保存主金字塔而丢弃这些关联图像。

### 5.2 完整性与解码抽样

- 32,285 个 tile 的偏移和长度均落在文件边界内。
- 32,285 个 tile 均具有完整 JPEG SOI/EOI。
- 跨层级、中心、四角和边缘抽样 111 个 tile，全部解码成功。
- 抽样 tile 的解码尺寸与 KFB 索引记录一致。
- 抽样 JPEG 使用一致的色彩采样结构和量化表集合。
- JPEG tile payload 总计约占文件 99.02%。

这些结果说明该样本无需将整张 34,013×46,152 RGB 图展开到内存，可以按索引顺序流式重封装。

### 5.3 有效金字塔

文件内带有从 20× 向下连续减半的层级。对 viewer 有意义的前 9 层为：

| Level | 尺寸 |
| ---: | ---: |
| 0 | 34,013 × 46,152 |
| 1 | 17,006 × 23,076 |
| 2 | 8,503 × 11,538 |
| 3 | 4,251 × 5,769 |
| 4 | 2,125 × 2,884 |
| 5 | 1,062 × 1,442 |
| 6 | 531 × 721 |
| 7 | 265 × 360 |
| 8 | 132 × 180 |

文件还携带更小的冗余单 tile 层级。生产 writer 可以在最短边或最长边已小于 tile size 后停止写入，避免无意义的 1×1 重复层。

## 6. 转换原型结果

所有原型输出只生成在本机 `/private/tmp`，不属于仓库和发布产物。

### 6.1 OME-TIFF SubIFD 原样重封装

结果：

- 输出：9 层 OME-TIFF。
- 大小：219,411,736 bytes。
- wall time：约 0.78 秒。
- 32,277 个保留层级 JPEG segment 全部与源 KFB 逐字节一致。
- PathTogether `TiffFileSlide` 可读取全部 9 层。
- OpenSlide 4.0.1 只暴露首层。

该结果证明原样重封装可行，但 SubIFD 不是明场 KFB 默认输出的最佳选择，因为它缩小了通用 OpenSlide 兼容面。

### 6.2 经典多 IFD 全量原样重封装

OpenSlide 能识别全部 9 层，但在读取原始尺寸不足 256×256 的边缘 JPEG 时失败：

```text
Dimensional mismatch reading JPEG, expected 256x256, got ...
```

TIFF 的 tiled image 使用固定 TileWidth/TileLength；即使图像处于右侧或底部边缘，OpenSlide 仍期望压缩 segment 解码为完整 tile。因此不能无条件复制所有 KFB JPEG。

### 6.3 推荐的混合重封装

算法：

1. mmap 或顺序读取 KFB header 和 tile index。
2. 校验每个 tile 的坐标、尺寸、offset、length 和 JPEG 边界。
3. 对 256×256 tile 原样写入 TIFF segment。
4. 对不足 256×256 的 tile 解码到小图。
5. 将小图贴到白色 256×256 canvas 左上角。
6. 使用与源 JPEG 相容的量化表和色度采样重新编码。
7. 写入经典多 IFD 金字塔及分辨率元数据。

真实样本结果：

| 指标 | 结果 |
| --- | ---: |
| 输出大小 | 219,925,816 bytes |
| wall time | 约 1.08 秒 |
| 最大 RSS | 约 278 MB，包含 mmap 文件页 |
| 完整 tile 原样复制 | 31,650 |
| 完整 tile 字节不一致 | 0 |
| 边缘 tile 重编码 | 627 |
| 重编码比例 | 1.94% |
| 边缘原图区域 MAE | 约 0.073/255 |
| 边缘原图区域 PSNR | 约 55.18 dB |

验证结果：

- OpenSlide 识别 9 层。
- PathTogether `slide_io.open_slide` 走 OpenSlide 路径并识别 9 层。
- 每层左上、右上、左下、右下读取成功。
- MPP 通过 TIFF resolution tags 暴露为约 0.484104 µm/px。
- 没有把整张约 47 亿 RGB 字节的图像展开到内存。

### 6.4 原型仍缺少的内容

- 20× objective 尚未作为可靠属性暴露给 OpenSlide。
- label、overview、thumbnail 尚未写入最终资产模型。
- 只有一份明场 KFB 样本。
- 当前 Python 原型用于证明格式和性能路线，不应直接复制为生产 parser。
- 尚未做 worker 中断、磁盘不足、重复任务、取消、删除或恢复测试。

## 7. 生产转换器设计

### 7.1 数据流

```text
Upload V2 source staging
        |
        v
format probe + source hash
        |
        +---- native readable ----> validate ----> atomic promote
        |
        +---- convert required ---> conversion_jobs
                                      |
                                      v
                              dedicated converter worker
                                      |
                                      v
                              canonical.tif.part
                                      |
                                      v
                          metadata + pixel validation
                                      |
                                      v
                    atomic promote + ownership + quota settle
```

### 7.2 `conversion_jobs` 最小字段

建议字段：

- `id`
- `owner_id`
- `upload_id`
- `source_asset_id`
- `source_sha256`
- `source_format`
- `target_format`
- `converter_id`
- `converter_version`
- `converter_options_json`
- `state`
- `attempt`
- `lease_owner`
- `lease_expires_at`
- `heartbeat_at`
- `created_at`
- `started_at`
- `finished_at`
- `error_code`
- `error_detail_internal`
- `canonical_asset_id`

幂等键建议限定在同一 owner 内：

```text
(owner_id, source_sha256, converter_id, converter_version, converter_options_hash)
```

不能直接跨租户复用结果或暴露 hash 命中，以免形成数据存在性侧信道。

### 7.3 worker 约束

首版不需要 Redis/Celery，可以使用 PostgreSQL lease 和：

```sql
SELECT ...
FROM conversion_jobs
WHERE state = 'queued'
ORDER BY created_at
FOR UPDATE SKIP LOCKED
LIMIT 1;
```

converter 应运行在独立容器中：

- 并发 1。
- CPU 初始限制 1–2 cores。
- 内存初始限制 1–2 GiB，按更多样本 benchmark 调整。
- 禁止外网。
- 只挂载当前任务的输入和输出目录。
- read-only root filesystem。
- 限制 pids、wall timeout、输出字节数和临时空间。
- 定时续租；进程退出或租约过期后任务可安全重试。
- 输出始终先写 `.part`，完整验证后原子改名。

### 7.4 上传 API 和前端状态

需要转换时，V2 commit 建议返回：

```json
{
  "status": "conversion_pending",
  "conversion_job_id": "...",
  "source_name": "original.kfb"
}
```

HTTP 状态使用 202。前端通过轮询或 SSE 获取：

- queued
- converting
- validating
- ready
- failed
- cancelled

ready 响应必须返回 `canonical_name` 或 `canonical_asset_id`。当前前端在上传成功后直接打开原始 `file.name`，需要改为打开服务端返回的 canonical 资产。

转换未完成前：

- 不应出现在可打开切片列表中，或必须明确显示“转换中”。
- 不允许创建分享链接。
- 不允许启动 AI session、region crop 或 annotation 工作流。

### 7.5 存储、配额和清理

开始转换前，必须为以下内容预留空间：

```text
source bytes + worst-case output bytes + temporary overhead
```

真实样本的重封装输出略小于源文件，但不能把这个比例外推到所有格式。对需要全量重编码的格式，输出可能明显放大。

初期建议保留原始 KFB，至少直到：

- canonical 输出完全写入。
- 索引、元数据、所有层级和边缘 ROI 校验通过。
- 正式资产、ownership 和 quota 已在同一提交边界完成。

资产 manifest 至少记录：

- 原始文件名、大小和 SHA-256。
- canonical 文件名、大小和 SHA-256。
- 转换器和版本。
- 转换参数。
- dimensions、levels、downsamples。
- MPP、objective、orientation。
- codec、tile size。
- channel metadata。
- label、overview、thumbnail 的引用。
- 创建时间和失败/警告状态。

删除正式资产时，应以 manifest 为准清理 source、canonical、associated images 和残留 temp；不能只 unlink 当前显示文件。

## 8. 元数据要求

明场 KFB 至少保留：

- 原始文件名与 hash。
- scanner/header 标识。
- 扫描时间。
- full-resolution width/height。
- level dimensions 和 downsamples。
- MPP X/Y。
- objective magnification。
- orientation。
- label、overview、thumbnail。

对于本样本，TIFF resolution tags 能让 OpenSlide恢复 MPP，但 20× objective 不会自动出现。生产实现应通过 canonical asset metadata 和 PathTogether reader 一并暴露，而不是只把倍率写在自由文本 comment 中。

KFBF 还必须保留：

- channel count。
- channel name。
- display color。
- exposure time。
- 每通道位深和有效位深。
- 通道顺序和缺失 tile 语义。

KFBF 的结论必须由真实样本验证，不能从本次 RGB brightfield 样本推导。

## 9. 安全与失败处理

KFB parser 面向用户上传的非可信二进制文件。最低要求：

- 固定 magic 和版本/variant 探测。
- 所有加法、乘法、offset 和 length 使用 checked arithmetic。
- tile count、dimensions、levels 和关联图像数量设置硬上限。
- 拒绝负长度、重叠异常、越界 payload 和不完整 JPEG。
- 在正式写入前验证预期 tile 数和网格覆盖。
- 控制解码器输入大小，避免 decompression bomb。
- 内部错误写入受保护日志；客户端只返回稳定的错误码。
- 转换器进程崩溃不能影响 Web 服务。
- 源文件在失败时保留或进入 quarantine；半成品可安全清理。

建议错误码：

- `unsupported_kfb_variant`
- `invalid_kfb_header`
- `invalid_tile_index`
- `tile_payload_out_of_bounds`
- `jpeg_decode_failed`
- `metadata_missing_required`
- `conversion_timeout`
- `conversion_output_too_large`
- `conversion_disk_low`
- `conversion_validation_failed`

## 10. 测试与验收门槛

### 10.1 样本集

生产前至少需要：

- 两份来自不同扫描日期、扫描仪或软件版本的明场 KFB。
- 一份超大、接近真实上限的明场 KFB。
- 一份带特殊边缘、空 tile、label 和 overview 的 KFB。
- 如果要支持荧光，至少一份多通道 KFBF。
- 一份损坏 header、损坏索引和截断 payload 的负向样本。

真实病理文件需要脱敏，并明确测试数据的访问和保存范围。

### 10.2 结构验收

- dimensions 完全一致。
- level 数和每层尺寸符合策略。
- tile 网格无错位、无重复、无未解释空洞。
- MPP、objective 和 orientation 正确。
- label、overview、thumbnail 可访问。
- KFBF 通道名称、颜色、曝光和顺序一致。

### 10.3 像素验收

- 对原样复制 tile 做压缩 payload 字节一致性校验。
- 每层抽取中心、四角、组织边界和背景 ROI。
- 所有边缘重编码区域记录 MAE、RMSE、PSNR，并做人工病理复核。
- 不允许对完整 JPEG tile 进行第二次有损压缩。
- 不以“肉眼看起来差不多”替代数值和病理复核。

### 10.4 应用验收

- 主 viewer 打开、缩放和漫游。
- Demo viewer。
- 分享页面。
- thumbnail、crop、tile endpoints。
- AI region capture 和 checkpoint。
- annotation 坐标与缩放。
- 浏览器 Chromium E2E。
- HTTPS 和缓存行为。

### 10.5 故障与并发验收

- worker 在写到一半时被 kill。
- worker/容器重启后租约恢复。
- 同一上传重复 commit。
- 同一 hash 重试。
- 任务取消。
- 上传后立即删除。
- 转换中磁盘达到 watermark。
- 输出超过预留大小。
- 多用户排队但 converter 并发保持 1。
- 转换进行时 viewer 和 AI 请求的延迟没有不可接受回退。

### 10.6 性能报告

每个真实样本记录：

- 输入和输出字节数。
- wall time。
- CPU seconds。
- peak RSS/cgroup memory。
- 读取和写入字节数。
- 原样复制 tile 比例。
- 边缘或 fallback 重编码 tile 比例。
- viewer p50/p95 tile latency 基线和转换期间数值。

本次约 1.08 秒结果只能作为该样本、本机和临时 Python writer 的可行性证据，不能直接作为生产 SLA。

## 11. 分阶段实施建议

### Phase A：生产 parser 与 converter spike

- 实现只读 KFB header/index parser。
- 固化真实样本和损坏样本测试。
- 生成经典多 IFD BigTIFF。
- 完整 tile 原样复制，边缘 tile 补白重编码。
- 保存 manifest 和 associated images。
- 与当前 `slide_io.open_slide` 做结构及 ROI 对照。

完成标准：转换器离线运行通过，不接上传入口。

### Phase B：后台任务与资产生命周期

- 增加 `conversion_jobs` migration。
- 增加独立 worker 和 lease/heartbeat。
- 实现 quota reservation、atomic promote、retry 和 cleanup。
- 实现 202 job API。

完成标准：故障注入、重启、取消和磁盘门槛测试通过。

### Phase C：上传 UI 与 viewer 集成

- format registry。
- 文件选择器支持 KFB。
- 上传进度后进入转换状态。
- ready 后打开 canonical asset。
- failed 显示稳定错误码和可重试动作。

完成标准：主 viewer、Demo、分享、crop、thumbnail、AI ROI 和 Chromium E2E 通过。

### Phase D：格式扩展

- 先补测更多 KFB variant。
- 获得真实 KFBF 后单独实现 OME-TIFF 通道路径。
- 对其他格式逐一决定 native 或 adapter，不做无边界万能转换器。

## 12. Go/No-Go

### 当前可以 Go

- 明场 KFB 生产 converter spike。
- 经典多 IFD BigTIFF 作为本次明场 canonical 候选。
- 独立、并发 1 的 PostgreSQL 驱动 worker 设计。

### 当前仍然 No-Go

- 仅把 `.kfb` 加入扩展名白名单后上线。
- 在 Gunicorn commit 请求内同步转换。
- 把所有格式统一重编码为 SVS。
- 删除原始 KFB 后仅保留未完整验证的转换输出。
- 宣称支持所有 KFB/KFBF variant。
- 未经真实荧光样本验证就上线 KFBF。
- 把本机原型性能描述为生产性能保证。

## 13. 交付状态

| 层级 | 状态 |
| --- | --- |
| 源码 review | 完成 |
| 单份真实 KFB 解析与转换 | **已校准**（34013×46152、9 层、0.82s、OpenSlide 可读） |
| Phase A 离线 parser/converter | 已落地 |
| Phase B conversion_jobs + worker | **已落地**（0046 + conversion_worker.py） |
| Phase C 上传 UI | **已落地**（accept `.kfb`，202 轮询，打开 canonical `.tif`） |
| 多样本/KFBF | 未完成 |
| 线上部署验证 | 需在目标主机跑 migration 并确认 worker 进程 |

合成 fixture 走 **kfb_bf_v1**。真实江丰样本走 **kfb_kfbio_jpeg**。未知变体 fail-closed。**不得**把 `.kfb` 加入 `SUPPORTED_EXTS`。

