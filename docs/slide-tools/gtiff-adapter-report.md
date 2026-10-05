# F5 通用瓦片 JPEG TIFF/BigTIFF 输入适配器报告

日期：2026-10-05。分支 `upload-convert-first`（工作树 `.gate-tmp/ucf-wt`）。
对应提交：核心 `5561bfb`、路由 `b60083b`、前端 `a35fbf9`、pytest/浏览器套件与
本报告同批提交。样本只以名称引用（公开 OpenSlide 样本，
https://openslide.cs.cmu.edu/download/openslide-testdata/，Generic-TIFF 目录
`CMU-1.tiff`，CC0-1.0，sha256 见同目录 `Generic-TIFF-index.yaml`）；测试里的
样本路径只经环境变量 `GTIFF_SAMPLE` 传入。

## 1. 支持范围

输入：**无已知厂商描述**的 classic TIFF / BigTIFF（双字节序）明场金字塔，
即 OpenSlide `generic-tiff` 家族（vips/tiffcp 转换产物等）。判定链：

1. 魔数路由：TIFF/BigTIFF 容器 → 有界 `sniff_tiff_vendor`；
2. 厂商分派（IFD 0 描述）：Aperio → SVS 适配器、Leica SCN XML → SCN 适配器、
   OME-TIFF / 本转换器自产 BigTIFF（描述 JSON 带 `CONVERTER_SOURCE_FORMATS`
   来源标记，词表含本适配器 `generic-tiled-jpeg-tiff`）→ **不是转换输入**，
   在 staging/复制前类型化拒绝；未知厂商 → 本适配器；
3. 本适配器复制前终审（`gtiff.rs probe_gtiff_with_budget`）：主链每个 IFD
   必须是 tiled、基线 JPEG（259=7）、photometric 2/6、3×8 bit、chunky
   （284=1）、无 ExtraSamples；各层严格递减、单步 1.5–16× 且 x/y 比；
   全金字塔共享一个 tile 几何（16–8192）。IFD 0 描述再次过厂商分类器
   （纵深防御）。

输出：与 SVS/SCN 相同的两个明场 profile——`bf-classic`（经典多 IFD JPEG
BigTIFF）与 `bf-ome`（RGB OME-BigTIFF，SubIFD 金字塔）；画质档
preserve（默认，源 tile 原样搬运）/ compact（锁定 compact-jpeg-v1 参数
q80/4:2:0/标准 Huffman，全量重编码，报告 lossy）。

元数据：MPP 取自 XResolution/YResolution + ResolutionUnit（inch/cm），双方
同时在场、有限、为正且相差 ≤1% 才写入；其余一律 unknown（不发明）。
IFD 0 的 ICC（34675）带入输出 level 0，且此时**不再**报
`color_management_not_applied`（无 ICC 时才报，SVS 同款条件式——独立审查
2026-10-05 修复，带 Rust/pytest 回归）。JPEG tile 真彩色空间按流内标记判定
（与 SVS/SCN 同规则：TIFF photometric 只在歧义时参与）；共享 `JPEGTables`
（347）顺位搬运。tile 允许非方形（tile_w ≠ tile_h）：l0-box2 合成画布按
(2·tile_w)×(2·tile_h) 计（独立审查 2026-10-05 修复——此前合成按正方形
假设，tile_h > tile_w 时越界 panic，probe 判定与转换崩溃自相矛盾；现
probe/convert 一致，Rust/pytest 双回归）。

## 2. 关键行为

### 2.1 tile 原样搬运（preserve）

源层每个 tile 逐字节复制（`tiles_raw_copied`），边界残缺 tile 直通并记
`edge_regions`（警告 `gtiff_cropped_edge_tile_passthrough`），内部残缺 tile
类型化拒绝。实测公开样本 CMU-1.tiff：31,098 tile 全部原样搬运、零重编码。

### 2.2 缺失降采样层：l0-box2 生成

源金字塔末层双边 >256 px 时（典型：单 IFD 转换产物），转换按 **l0-box2**
追加生成层——每个生成 tile 是上一**输出**层已提交 tile 的 2×2 面积均值链
（自「最后一个在场源层」续链；单层源即严格 L0 派生金字塔）。floor 尺寸
（min 1），生成到双边 ≤256 px 为止（与 CMU-1.tiff 自身 9 层到 179×128 的
惯例一致）；参数锁定 q96 / 4:2:2 / 标准 Annex-K Huffman
（`gtiff-l0-box2:q96:y422:hstd:v1`），compact 档换用锁定紧凑参数。画布
越界区域为白（明场 padding 惯例），边缘均值包含该 padding（构造性、确定
性，与 MRXS 适配器同一性质）。生成层数计入警告
`gtiff_missing_levels_generated:<n>`，probe 文档里生成层带 `"generated":true`
（probe `levels` ≙ 输出 IFD 数——runner 据此预开 scratch，同 MRXS 不变量）。

注意：**中段缺失不补**（如 4× 跳层保持在 1.5–16× 比范围内原样保留）；
生成只追加尾部。

### 2.3 内存预算与有界读取

probe/convert 的工作集在分配前收费（`MemBudget`，超额稳定拒绝
`resource_profile_insufficient`）：IFD 链结构预留、合成画布/输出缓冲
（边长 = 2×tile，按 tile 上界计）。l0-box2 合成自已提交输出回读解码，
工作集 O(画布)，与层尺寸无关。全部读取经 `ByteSource` 定长有界读
（浏览器宿主 ≤1 MiB/次），`tests/browser/slide_tools_c2/test_no_whole_file.js`
通过（本批未引入任何整文件读取模式）。

### 2.4 续跑

逐 tile 行 checkpoint；journal/checkpoint 携带
`adapter=generic-tiled-jpeg-tiff` + `adapter_version="1"`。换转换器
（record.sourceAdapter 篡改 / checkpoint 适配器不符）与源文件被改
（sha256 复核）都拒绝续跑；核心层对 checkpoint 缺失/不一致的
adapter_version 一律拒绝。Rust 项 `resume_from_a_crash_inside_a_generated_level_is_byte_identical`
把崩溃点刻意钉在生成层中段，续跑产物与不间断运行逐字节一致。
修复（随本批）：l0-box2 回读合成必须合并上一层的共享 `JPEGTables`
（源层缩略 tile 流不能独立解码）；convert_scn 的 resume_done 分支原会
传空描述丢 IFD tag（潜伏差异），已改为重写真实描述。

## 3. 拒绝的变体（复制前类型化拒绝，归「暂时直传」）

| 变体 | 拒绝点 | 稳定码 |
| --- | --- | --- |
| 条带存储（273/279，无 322/323） | probe 结构检查 | `unsupported_kfb_variant` |
| LZW（259=5）/ deflate（259=8/32946）/ 其他非 7 | 同上 | 同上 |
| JPEG 2000（33003/33005） | 同上 | 同上 |
| 非 8 位（BitsPerSample ≠ [8,8,8]） | 同上 | 同上 |
| 灰度/多通道（SamplesPerPixel ≠ 3） | 同上 | 同上 |
| 平面存储（PlanarConfiguration=2） | 同上 | 同上 |
| 各层 tile 几何不一致 | probe 层序检查 | 同上 |
| (0,0) tile 记录（稀疏网格惯例不适用于本家族） | TileCursor | `tile_payload_out_of_bounds` |
| Aperio / SCN XML 描述 | 路由分派至各自适配器（通用适配器纵深防御同样拒绝） | — |
| OME-TIFF / 本转换器 BigTIFF | **不是转换输入**：嗅探与核心双层拒绝，提示直接上传 | 同上 |

浏览器嗅探（`engine.js sniffTiffSlideCapability`）在 staging 前做同契约
快判：结构门槛全过的无厂商 TIFF → 需要转换；上述变体维持「暂时直传」
（`slide-sniff.js` classify 同步：tiled+压缩 7+3 采样+photo 2/6 → convert）。

## 4. 实测（公开样本 CMU-1.tiff，46000×32914，9 层，204,117,846 B）

环境：本机 18 GB，release CLI（0.1.0，rustc 1.98.1），默认预算 192 MiB，
`systemd-run --user --scope -p MemoryMax=320M -p MemorySwapMax=0` 下复核。

| 指标 | 值 |
| --- | --- |
| probe | 9 层全链、tile 256、生成层 0、mpp=1000 µm（样本分辨率标签 10 px/cm 占位值，OpenSlide 同读） |
| bf-ome（preserve） | 204,368,901 B，31,098 tile 全部原样搬运，核心耗时 ~0.21 s（进程峰值 RSS 5.1 MB） |
| bf-classic（preserve） | 204,369,481 B，同上 |
| bf-ome（compact） | 169,485,439 B，31,098 tile 重编码，指纹 cj1:q80:420:hstd:v1 |
| L0 像素 | 3 个组织 ROI（512²/384²）与 OpenSlide 读源 **max diff 0** |
| 低倍层几何 | 9 层逐层尺寸与源一致；level 2 ROI 均值差 <1（读器相位 + 源缺 tag 530 的色度上采样语义差，≤±3，产物写 SOF 真值 (2,1) 更正确） |
| 平台读取器 | `slide_io.open_slide` 打开产物：TiffFileSlide、`is_native_rgb=True`、9 层 |
| 浏览器↔原生 parity | `run_parity.js --input` 两种输出 profile × {preserve, compact} 全部逐字节一致（bf-ome preserve 3a62f980…、bf-classic preserve 589ea437…、compact 两 profile 亦相等） |

生成尾实测（合成单层源 4100×2600/tile 256）：生成 4 层（2050/1025/512/256），
54+15+4+1 个生成 tile，16.6 MB 输出，核心耗时 ~0.68 s。

## 5. 测试矩阵

- Rust `crates/core/tests/gtiff.rs`：26 项（探测/生成尾几何、逐字节搬运、
  box2 逐 tile 像素（含非方形 tile 回归）、OME profile 校验、变体/路由
  拒绝、零值 tile、预算、估算、续跑一致、大端/BigTIFF、厂商分类词表、
  ICC 携带与条件警告回归）。
- pytest `tests/test_slide_transform_gtiff.py`：18 项（夹具双 profile、
  生成层像素门、变体拒绝、厂商路由分派、预算、env 未设置 skip 回归、
  ICC/--tile-h 审查回归；真实样本门 + 320M scope + L0 硬门）。
- vitest `tests/js/tools-gtiff-input.test.ts`（13 项）+ `slide-sniff.test.ts`
  /`tools-scn-input.test.ts` 跟进。
- 浏览器 C2：`run_faults.js` 新增 `gtiff-adapter-change-refused` /
  `gtiff-source-changed-refused` 两行（均 PASS）；`run_parity.js --input`
  四种组合 PASS；`test_no_whole_file.js` PASS。C3：`run_e2e.js` 新增
  `gt` 页面场景（GTIFF_SAMPLE 门控）。

## 6. 未覆盖项 / 已知边界

1. **中段缺失层不补**：非 ÷2 链（如 4× 跳层）按声明比范围原样保留，只在
   尾部生成；「任意位置补齐缺层」未实现（也未观察到真实文件需要）。
2. **生成层边缘均值含白 padding**：与 MRXS l0-box2 同一构造性近似；未做
   边缘复制/加权等变体。
3. **associated 图**：通用 TIFF 家族无厂商词表区分 label/macro，主链非
   level IFD 一律拒绝——带缩略图 IFD 的文件会被拒（fail-closed），未做
   「检测并排除」。
4. **tile 几何必须全塔一致**：vips 极少数输出各层不同 tile 尺寸的变体被
   拒绝（暂时直传）。
5. **YCbCr 源且缺 tag 530 的读端语义差**：产物写 SOF 真值采样，比源更
   正确，但与「按缺省 4:2:0 解源」的读器在低倍层有 ≤±3 的差异（L0 为
   零，OpenSlide/libjpeg 不受影响）。
6. **64 KiB/生成 tile 的磁盘估算代理**：远高于实测中位数（~8–20 KiB），
   仅影响空间预检的保守性，不影响产物。
7. **大端 classic TIFF 的真实样本未测**（公开目录无此类 generic-tiff），
   仅合成夹具覆盖。
8. **JS 侧无 ICC/tile 形状行为**：engine.js 嗅探只做厂商分派 + IFD 0 结构
   快判，不读 ICC 也不检查 tile 比例——两项审查修复的行为都在 wasm 核心
   （probe/convert），JS 无对应断言面（故 vitest 未加用例，非遗漏）。
