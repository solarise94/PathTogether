# F8 — 普通图片（BMP/JPEG）Input Adapter Report

Phase F8（先转换后上传 / 格式扩展）：浏览器切片转换工具新增普通图片
（单张 BMP / 基线 JPEG）输入转换器。普通图片没有源 tile 可搬运，输出瓦
片全部是「按图 own 的有界解码单元解码 → 拼成 256 瓦片 → 重编码」的产物
——BMP 逐行读取（行长由头精确给出）；基线 JPEG 带 restart marker 走
restart 分段（NDPI 同款 `SegmentReader`），无 restart marker（真实相机
输出常态）走**流式 MCU 行 band 解码**（新增 `jpeg/band.rs`，64 KiB 有界
熵窗口 + 提前解码一行 + 上一行 carry，与整帧解码逐字节一致）。金字塔用
l0-box2；**BMP/JPEG 不携带物理标尺，OME 输出刻意不写 PhysicalSize**。
分支 `upload-convert-first`，worktree `.gate-tmp/ucf-wt`。
**Status: implemented；本文所有实测数据均出自本 worktree 的运行记录**
（命令与输出见各节）。

Code:

- `slide-transform-core/crates/core/src/raster.rs` — 探测适配器
  （`plain-image-bmp-jpeg`，adapter version `1`）：BM / FF D8 FF 魔数、
  BMP DIB 头解析（BITMAPCOREHEADER 12 / InfoHeader 40 / V4 108 / V5 124、
  压缩码与位深判定、bottom-up/top-down、行区间界检查）、JPEG 标记走查
  （复用 `ndpi::scan_strip_head`，SOF/DRI/SOS 定位 + 渐进/算术拒绝）、
  多段 APP2 ICC 提取、像素上限（每边 ≤ 10⁶、总数 ≤ 2³²）、estimate
  （像素基准上界）
- `slide-transform-core/crates/core/src/jpeg/band.rs` — 无 restart 扫描的
  流式 MCU 行 band 解码器：`StreamBits`（BitReader 语义：FF00 解塞、
  marker 停止、末尾零填充，但字节来自 `ByteSource` 的 64 KiB 有界窗
  口）、共享 `decode_block`/`idct_islow_block` 内核、滑窗上采样（当前
  MCU 行 + 提前解码的下一行 + 上一行 carry；h2v1/h2v2 fancy/int 整数扩
  展三路，帧边 clamp 与整帧 `upsample_all` 同规则）；`skip_to` 解码跳过
  支持续跑
- `slide-transform-core/crates/core/src/convert_raster.rs` — 转换：三路
  有界喂入（BMP 行 / JPEG 分段 / JPEG band）→ 256 瓦片 → q96 YCbCr
  4:2:2 保留画质重编码（`raster-compose:q96:y422:hstd:v1`）或锁定 U3
  compact；l0-box2 生成尾；classic 描述 JSON 与 OME provenance；续跑
  版本钉；内存预算
- `slide-transform-core/crates/core/src/segment.rs` —
  `paste_segment_rects` 泛化共享（`convert_ndpi` 委托同一实现，行为不变）
- `slide-transform-core/crates/core/src/raster_fixture.rs` — 合成夹具生成
  器（纯代码，`fixtures` feature；BMP 24/32/top-down/core/RLE8/4 位/
  截断；JPEG 带/无 restart/渐进/灰度；`--pattern gradient|noise`）
- `slide-transform-core/crates/core/tests/raster.rs` — 14 项 Rust 集成测
  试（band==整帧 8 组采样逐字节、skip 一致、双 profile 契约、变体拒绝、
  预算拒绝、三种喂入的续跑逐字节一致、estimate 上界盖实测）
- `slide-transform-core/crates/cli/src/main.rs` — `probe`/`convert` 魔数
  路由 + `gen-raster` 合成夹具子命令
- `slide-transform-core/crates/wasm/src/lib.rs` — wasm probe/convert/resume
  路由；`InputKind::Raster` 携带 adapter id/版本进 journal 与 checkpoint
- `static/tools/slide-transform/engine.js` — 魔数表 + `sniffRasterCapability`
  （staging 前有界嗅探：变体/像素上限类型化拒绝）；`RASTER_SOURCE_ADAPTER`
  等常量；转换器产物词表
- `static/tools/slide-transform/runner.js` — `_prepare` raster 分支（嗅探
  失败不进 OPFS）
- `static/tools/tools-slides.js`、`templates/tools_slides.html`、
  `static/i18n.js` — 工具页 accept/文案/格式族名「普通图片 (BMP/JPEG)」/
  画质说明行（中英）
- `static/upload/slide-sniff.js` — 工作台直传分流：.bmp/.jpg/.jpeg →
  需要转换（`classifyRasterHead` 头解析分派）
- `slide_format_registry.py` — 目录行级 `browser_convert=available`
  （`direct_import` 保持 `open`）；`upload_direct_class.py` 转换器产物
  词表补 `plain-image-bmp-jpeg`
- 测试：`tests/test_slide_transform_raster.py`（pytest）、
  `tests/js/tools-raster-input.test.ts`、`tests/js/slide-sniff.test.ts`、
  `tests/js/raster-image-compat.test.ts`、`tests/js/tools-u2-page.test.ts`、
  `tests/browser/slide_tools_c2/run_parity.js`（既有 `--input` 直接覆盖）、
  `tests/browser/slide_tools_c2/run_faults.js`（两个 raster 场景）、
  `tests/browser/slide_tools_c3/run_e2e.js`（`ra-raster-page-e2e`）

## 0. 结论（Verdict）

| 要求 | 状态 |
|---|---|
| 有界读取（绝不整文件进内存） | **PASS** — BMP 逐行 `read_at`（行长精确）；JPEG 带 DRI 按段读取（RST 扫描 64 KiB 块），无 DRI 走 64 KiB 熵窗口的流式解码；`test_no_whole_file.js` PASS（本次运行） |
| JPEG 基线按 restart 区间**或**按 MCU 行有界解码 | **PASS** — DRI>0：`SegmentReader` 分段（NDPI 同款）；DRI=0：`jpeg/band.rs` 按流式 MCU 行解码；`band_matches_whole_frame_decode` 8 组采样/尺寸组合下 band 拼装 == `jpeg::decode` 整帧**逐字节**一致；真实样本（无 restart marker）走 band 路径（probe `decode_unit=mcu-row-bands`） |
| BMP 支持未压缩 24/32 位，其他变体类型化拒绝 | **PASS** — 24/32 位 BI_RGB（InfoHeader 40/V4/V5 + OS/2 core header 12、top-down）全支持；RLE8/4（"RLE 行程编码"）、BITFIELDS、JPEG/PNG-in-BMP、ALPHA 位域、1/4/8/16 位深（"位深 … 不在支持集"）、未知 DIB 头尺寸、planes≠1、像素区间截断（oob）全部类型化拒绝且在写出之前（pytest 断言产物不存在） |
| 切块后编码为保留画质（q96 4:2:2）或 compact | **PASS** — 256 瓦片按 `raster-compose:q96:y422:hstd:v1` 指纹重编码；compact 用锁定 U3 参数；报告 `composed` 摘要 + `lossy_reencode`（仅 compact）；strict-lossless 类型化拒绝（`pixel_policy_violation`） |
| 金字塔用 l0-box2 | **PASS** — 输出 = 重编码 L0 + `generated_tail`（÷2 box 链，末层 ≤ 256）；`pyramid=l0-box2` 进报告/描述 JSON/OME-XML；真实样本 6 层至 187×125 |
| 无物理标尺，OME 不写 PhysicalSize | **PASS** — mpp 恒为 unknown（`mpp_source="none (plain image: no physical scale in BMP/JPEG)"`）；OME XML 无 `PhysicalSizeX/Y`（pytest 对输出字节断言 `b"PhysicalSize" not in raw`）；描述 JSON `mpp_x/mpp_y/objective` 恒 null；告警 `raster_no_physical_size` |
| 输入像素数设合理上限并在复制前拒绝超限 | **PASS** — 核心 `MAX_SIDE=10⁶`、`MAX_PIXELS=2³²`（probe 拒绝）；engine `sniffRasterCapability` 用同一数值在 staging 前拒绝（pytest 合成超限头 + vitest 100000×100000 头均拒绝，无产物） |
| 内存在分配前按预算拒绝 | **PASS** — 行带/条带缓冲、瓦片画布、单段解码峰值、band 扫描工作集（平面×2 + 上采样带 + 窗口）、ICC 载荷全部 `charge` 在分配前，超限 → `resource_profile_insufficient`（pytest：64 KiB 预算拒绝且无产物，192 MiB 默认成功；BMP/JPEG-band/JPEG-seg 三路都测） |
| source_format + `ADAPTER_VERSION="1"`；续跑 journal 携带转换器 id/版本 | **PASS** — 报告/provenance/journal/checkpoint 携带 `plain-image-bmp-jpeg`/`1`；换版本（或字段缺失）的 checkpoint 在核心入口拒绝（Rust 断言）；wasm `InputKind::Raster` 的 adapter id/版本进 `HostCheckpoint` |
| 换转换器或源文件被改拒绝续跑 | **PASS** — `run_faults.js --only raster`：`raster-adapter-change-refused`（resume_refused / source-adapter）与 `raster-source-changed-refused`（source_changed_refuse_resume）实测 PASS；诚实续跑 == 原生字节（Rust resume 测试三种喂入逐字节一致；浏览器 `raster-adapter-change-refused` sha == 原生） |
| CLI 路由 + `gen-raster` 子命令 | **PASS** — probe/convert 按魔数在 TIFF 嗅探之前路由；`gen-raster` 无样本单测夹具（kind/bpp/topdown/core/compression/bits/truncated/no-restart/progressive/gray/noise 旋钮） |
| wasm probe/convert/resume 路由 | **PASS** — probe 文档带 `decode_unit`（rows / restart-segments / mcu-row-bands）；convert/resume 分支按资源档位预算传入（与 gtiff/ndpi 同款）；路由单元测试锁定魔数分派与 adapter 身份 |
| 浏览器产物 == 原生 CLI（逐字节） | **PASS** — `run_parity.js --input`（既有通用参数直接覆盖）：JPEG preserve bf-ome/bf-classic 与 compact 两 profile、BMP preserve + compact 共 6 组 `equal=true`（本次运行） |
| 工具页 accept、格式名称、i18n 中英 | **PASS** — accept 加 `.bmp,.jpg,.jpeg`；`普通图片 (BMP/JPEG)` 格式族名；raster 画质说明行（`tools.quality.raster.note` 中英）；`ra-raster-page-e2e` 识别断言（实测 PASS） |
| slide-sniff 改「需要转换」；格式注册表 `browser_convert=available`（direct_import 保持 open） | **PASS** — `.bmp/.jpg/.jpeg` → `route=raster` 头解析分派（24/32 未压缩与三分量基线 → convert；变体 → temporary 暂时直传）；`raster-image` 行 `browser_convert=available`、`import_mode=convert`、`direct_import=open`；`upload_direct_class.CONVERTER_SOURCE_FORMATS` 补 `plain-image-bmp-jpeg`（pytest 参数化锁定） |

## 1. 支持范围

| 容器 | 支持变体 | 有界解码单元 |
|---|---|---|
| BMP（`BM`） | BITMAPCOREHEADER（12，OS/2，行 2 字节对齐）与 BITMAPINFOHEADER/V4/V5（40/52/56/108/124，行 4 字节对齐）；未压缩 BI_RGB；24 位（BGR→RGB）与 32 位（BGRA→RGB，BI_RGB 的 alpha 未定义、直接丢弃）；bottom-up（默认）与 top-down（负高度） | 单行（行长 = `width×bpp/8` 对齐，由头精确给出） |
| JPEG（`FF D8 FF`） | 基线 SOF0/SOF1、8 位、三分量（JFIF / Adobe APP14 / 无标记，色彩判定与 SVS 同规则 `tiff_jpeg_color`）；DRI>0 与 DRI=0；多段 APP2 ICC（≤1 MiB，随输出携带） | DRI>0：restart 分段（每段独立 MCU run，DC 复位）；DRI=0：MCU 行 band（流式） |

两种容器共用同一输出管线：L0 → 256×256 瓦片（画布贴源、边缘 tile 满幅
重编码）→ `l0-box2` 生成尾 → classic 多 IFD JPEG BigTIFF 或 RGB OME-BigTIFF。

## 2. 拒绝的变体（全部复制/写出之前，类型化）

| 变体 | 稳定码 | 消息片段 |
|---|---|---|
| BMP RLE8/RLE4（compression 1/2） | `unsupported_kfb_variant` | 「RLE 行程编码…不在支持集」 |
| BMP BITFIELDS / ALPHA 位域（3/6） | `unsupported_kfb_variant` | 「BITFIELDS 位域掩码…」 |
| JPEG/PNG-in-BMP（4/5） | `unsupported_kfb_variant` | 「JPEG-in-BMP / PNG-in-BMP」 |
| BMP 位深 ∉ {24,32}（1/4/8/16/调色板） | `unsupported_kfb_variant` | 「位深 … 不在支持集」 |
| 未知 DIB 头尺寸 / planes≠1 / 像素区间截断 / 数据偏移落头内 | `unsupported_kfb_variant` / `tile_payload_out_of_bounds` | 「未知 DIB 头尺寸…」「截断」 |
| 渐进/分层/无损 JPEG（SOF2/3/5-7/9-B/D-F） | `jpeg_decode_failed` | 「不支持渐进/分层/无损 JPEG（SOF FF…）」 |
| 算术编码 / JPG 扩展 | `jpeg_decode_failed` | 「算术编码不受支持」「JPG 扩展不受支持」 |
| 灰度（单分量）JPEG | `unsupported_kfb_variant` | 「单分量（灰度）…不在支持集」 |
| restart interval > 2²⁴ | `unsupported_kfb_variant` | 「restart interval … 越界」 |
| 非 BMP/JPEG 魔数（含 TIFF/KFB 伪装扩展名） | `unsupported_kfb_variant` | 「不是 BMP/JPEG（魔数不符）」 |
| 尺寸超上限（每边 >10⁶ 或 >2³² 像素） | `unsupported_kfb_variant` | 「超出普通图片支持上限…复制前拒绝」 |
| 预算 < 工作集 | `resource_profile_insufficient` | 「内存预算不足…在其分配前拒绝」 |
| strict-lossless | `pixel_policy_violation` | 「…无逐字节搬运路径」 |
| 荧光 OME profile（fl-ome） | `unsupported_kfb_variant` | 「荧光 OME profile 不适用于明场普通图片输入」 |

工作台分流（slide-sniff.js）：`.bmp/.jpg/.jpeg` 满足头判定 → `convert`
（本机转换后上传）；不满足（RLE/位域/未知 DIB、渐进/灰度 JPEG）→
`temporary`（暂时直传，服务端终审）。

## 3. 实测（本 worktree 运行记录）

样本：公开 OpenSlide 样本（CC0，`.testdata/openslide/ucf-samples.env`）

- **CMU-1-region.jpg**（6000×4000，基线 JPEG 4:2:2，**无 restart
  marker** → band 路径；源 9,619,181 B）：
  - preserve（bf-ome）：**21,773,721 B**（0.91 B/px；≈2.26× 源），1.69 s
    墙钟（原生 CLI，release）；compact：**9,862,358 B**，1.29 s。
  - 磁盘预检上界：preserve 73,089,568 B（3× 像素上界）≥ 实测
    （bound/actual ≈ 3.4）；compact 37,089,568 B ≥ 实测（≈3.8）。
  - L0 组织 ROI 均值误差（vs 平台读取器读源，`int16` L1）：ROI(1000,800)
    **1.157**、ROI(3000,2000) **1.076**（上限 6；量级 = 源一次 JPEG 有损
    + q96 4:2:2 再生成）。
  - 低倍层几何：6 层 (6000×4000) → (187×125)，末层 ≤ 256；level 1/3 与
    Pillow LANCZOS 缩略均值误差 < 24（重采样核不同）。
- **CMU-1-region-small.bmp**（1500×1000×24 位未压缩；源 4,500,054 B）：
  - preserve（bf-ome）：**1,809,870 B**（0.4 B/px 输出小于无损源——
    q96 4:2:2 重编码），0.10 s 墙钟。
  - L0 ROI 均值误差：**3.157 / 3.520**（上限 6；无损源完整承担一次
    q96 4:2:2 生成损失，色度下采样是主导项）。
- **浏览器 parity**（`run_parity.js --input`，Playwright + 生产 runner）：
  JPEG preserve bf-ome `63fb3c27…` / bf-classic `ce538c6f…`、compact
  bf-ome `aa49cefe…` / bf-classic `ff23ad17…`；BMP preserve bf-ome
  `b07662a3…` / bf-classic `b0321417…`、compact bf-classic `2466d934…` —
  与原生 CLI **逐字节一致（6 组 equal=true）**。
- **故障矩阵**（`run_faults.js --only raster`）：换转换器拒绝续跑
  （`resume_refused`/`source-adapter`，诚实续跑 sha == 原生）与源文件被
  改拒绝续跑（`source_changed_refuse_resume`、输出不增长）2/2 PASS。
- **C3 页面场景**（`run_e2e.js --only ra`）：页面识别「普通图片
  (BMP/JPEG)」、画质说明行可见、preserve/compact 保存 sha == 原生、渐进
  夹具复制前被拒且无任务目录 — PASS。
- 内存：pytest 以 `--memory-budget 65536` 验证分配前拒绝；真实样本在
  `systemd-run --user --scope -p MemoryMax=320M` 下转换成功（默认 192 MiB
  预算）。

## 4. 未覆盖项 / 已知限制

1. **EXIF 方向不旋转**：带 EXIF Orientation 的 JPEG 按存储像素序转换
   （平台侧 RasterSlide 同样不二次 EXIF 校正——raster-image-compat 契约
   「无二次 EXIF」两侧一致）。需要旋转的图片请先在图片工具里落盘旋转。
2. **BMP V4/V5 内嵌 ICC profile 不提取**（`icc_profile` 告警
   `color_management_not_applied`）；JPEG 的 APP2 ICC 提取并携带。
3. **16 位 BMP、灰度 JPEG、CMYK（4 分量）JPEG**：类型化拒绝，未提供
   自动转换路径。
4. **灰度输出需求**：输出恒为 RGB 明场（YCbCr 4:2:2 tile），不提供灰度
   或 16 位输出。
5. **OS/2 v2 系（DIB 头 16/64）与 BITMAPV2/3（52/56）的 RLE 变体**：52/56
   头按压缩=0 判定支持，但其 RLE 变体同样按压缩码拒绝（未见真实样本）。
6. **像素上限固定值**（10⁶/边、2³² 像素）是工程上限而非格式边界；更大
   的单图（整切片拼接导出）应走切片容器格式。
7. **浏览器单文件转换的内存预算**：wasm 单文件路径与既有 gtiff/ndpi
   一致使用 saver 默认 192 MiB（bundle 路径才传资源档位预算）；预算拒绝
   的契约由核心与 CLI/presets 覆盖（`--memory-budget`）。
8. **ICC 色彩管理**：携带的 ICC 原样随输出（TIFF 34675），不做转换或
   校色（与 NDPI/SVS 适配器同语义）。
