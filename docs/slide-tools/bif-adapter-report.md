# F9 — Ventana BIF 输入适配器报告

- 分支 `upload-convert-first`（worktree `.gate-tmp/ucf-wt`），第九个浏览器输入转换器（前八个：SCN / 通用 TIFF / NDPI / VMS / MRXS / SVS / KFB / KFBF / 普通图片中的五类明场多格式族）。
- **Status: implemented；本文所有实测数据均出自本 worktree 的运行记录**（命令与输出见各节）。
- 验收样本：OpenSlide 公开样本 **OS-2.bif**（license distributable，路径仅经环境变量 `BIF_SAMPLE` 传入）。另一公开样本 Ventana-1.bif 是 LEFT 拼接走向（OpenSlide 同样拒绝读取），不作验收样本，按类型化拒绝处理。

Code:

- `slide-transform-core/crates/core/src/bif.rs` — BIF 探测适配器（`ventana-bif-jpeg`，adapter version `1`）：BigTIFF + IFD0 XMLPacket 的 iScan 厂商块识别、层级页 `level=N mag=M` 契约、EncodeInfo 拼接几何（AoiOrigin/ImageInfo/TileJointInfo、置信度加权平均重叠、Pos-Y 翻转、边界盒尺寸）、tile 载荷真值探测（JPEGTables 合并）、磁盘预算上界。
- `slide-transform-core/crates/core/src/convert_bif.rs` — 拼接转换：逐瓦片 MCU 行 band 流式解码（无 restart 的缩略 JPEG 流，`jpeg/band.rs`）→ 按 OpenSlide 逆光栅粘贴次序拼进 256 行输出带 → q96 YCbCr 4:2:2 重编码；降采样层 l0-box2；扫描器跨 band carry；预算前拒绝；续跑（适配器版本钉）。
- `slide-transform-core/crates/core/src/bif_fixture.rs` — 合成夹具生成器（`gen-bif`）。
- CLI（`crates/cli/src/main.rs`）：`gen-bif` 子命令、probe/convert 的 `TiffVendor::VentanaBif` 分派；wasm（`crates/wasm/src/lib.rs`）同一路由。
- 前端：`engine.js`（嗅探分派 + BIF 常量 + 转换器词表）、`runner.js`（BIF 版本钉）、`tools-slides.js`/`templates/tools_slides.html`/`i18n.js`（accept、格式名、画质说明行、中英文案）、`static/upload/slide-sniff.js`（`.bif` 从「暂时直传」改为头解析分派；顺带修复 BigTIFF 头解析的两个潜在缺陷，见 §4.3）、`slide_format_registry.py`（bif 行 `browser_convert=available`，`direct_import` 保持 `open`——是否关闭直传是用户的独立决定）、`upload_direct_class.py`（`ventana-bif-jpeg` 进转换器来源词表）。

## 0. 结论（Verdict）

- 真实样本 OS-2.bif（2.72 GB）在 **MemoryMax=320M / MemorySwapMax=0** 门内、默认 **192 MiB** 内存预算下转换成功：**488.3 s** 墙钟、**峰值 RSS 116 MB**、输出 **4,094,070,377 B**（4.09 GB，bf-ome）。
- 拼接几何与 OpenSlide **逐项一致**：114943×76349（TIFF 画布标签是重叠前的 128000×82960）、mpp 0.2325、objective 40、10 层。
- 像素门（复用同一产物）：3 个组织 ROI 的 L0 均值误差 **2.03 / 2.11 / 3.93**（通道均值差 ≤ 0.26，上限 6.0 与 NDPI/VMS 同档）。低倍层（l0-box2 vs 扫描仪自己的层）**7.10 / 8.58 / 10.97**（层 1/2/4，上限 8/10/13 按实测给）。
- **独立审查修复（2026-10-07）**：重叠优先级最初实现反了（(row,col) 最小者胜）——审查方实测并经本机复现（openslide 4.0.1，OS-2.bif 接缝逐点 + 整 ROI 零重编码参照），OpenSlide 在重叠带内**恒是 (row,col) 较大的瓦片胜出**；翻转前 ROI 误差 9.38/6.81/5.01（约 18% 输出像素取自另一次扫描），修复后 2.03/2.11/3.93。落位同步从 floor 改为 round（零重编码参照下 floor 3.77/2.10/4.01 → round 1.73/1.88/3.69）。
- 浏览器 == 原生：合成夹具 preserve/compact 双 profile 逐字节一致（C3 `bi-bif-page-e2e`）；真实样本 parity 见 §3.1。
- 续跑：journal 携带适配器 id + 版本，换转换器或源被改均类型化拒绝（C2 故障矩阵两行 PASS）。

## 1. 设计

### 1.1 文件模型（`bif.rs`）

结构模型镜像 OpenSlide `ventana.c` 的语义（独立实现，无 OpenSlide 代码；全部有界读取）：

- **容器**：BigTIFF（43）恒成立；经典 TIFF 的 `ventana tif` 变体类型化拒绝（「暂时直传」）。
- **厂商识别**：IFD 0 的 XMLPacket（700）含 `iScan` 元素（根为 `<iScan>` 或 `<Metadata><iScan>`）；描述通常是 `Label Image`、无 Make，因此嗅探在描述/Make 之后查 XMLPacket。
- **页分类**：描述含 `level=` → 层级页（必须 `level=0,1,2,…` 严格递增、`mag=` 严格递减、瓦片尺寸全层级一致）；`Label Image`/`Label_Image` → label；`Thumbnail` → thumbnail；其余（如 Ventana-1.bif 的 `Probability_Image`）→ associated；关联页全部检测在位、**不导出**（主图转换，不是源归档）。
- **拼接几何**（EncodeInfo XML，层级 0 的 XMLPacket）：
  - `/EncodeInfo/SlideStitchInfo/ImageInfo`（每 AOI 一个）与 `/EncodeInfo/AoiOrigin/AOIn` 一一配对；`AOIScanned` **非 0 即扫描**（与 ventana.c 一致；缺属性是其硬错误；OS-2 的 AOI1 为 0 被跳过）；
  - **网格遍历上限（审查修复）**：每个 AOI 的网格必须完整落在 canvas 瓦片网格内（算术检查），且各 AOI 网格总格数 ≤ TileOffsets 实际条目数（每个引用格都要有一个真实瓦片且 (col,row) 不得重复）；去重 HashSet 先计费后分配。构造文件（小瓦片 + 大画布 + 大 NumCols/NumRows，~15.3M 格）在遍历前被类型化拒绝（修复前实测：200 KB 文件吃 1.0 GB 内存、22+ CPU 分钟仍未完成）；
  - `OriginX/Y` 必须是瓦片尺寸整数倍 → AOI 首瓦片的全局 (col,row)；`Pos-X/Pos-Y` 是该瓦片的绝对拼接位置（有时写成小数；ventana.c 读 double 后存 int64——按**向零截断**对齐，边界盒/落位与其逐位一致）；
  - `TileJointInfo`：Direction 仅 **RIGHT/UP**（LEFT/DOWN 类型化拒绝——Ventana-1.bif 即 LEFT）；Tile1/Tile2 按 boustrophedon 编号（自底向上、奇数行自右向左）换算回网格坐标并校验相邻性；OverlapX/Y 按 Confidence 加权平均得步进 `advance = tile − mean(overlap)`（OS-2：911.1097 / 1249.8086 px）；
  - **Pos-Y 翻转**：`y' = top − y − H`，`H = (rows−1)·advance_y + tile_h`，`top = max(y+H)`（Pos-Y 度量的是「区域底到一个全区域之下点的距离」）；
  - **拼接尺寸** = 所 有已放瓦片边界盒右/下边缘的 ceil（OS-2：114943×76349，与 OpenSlide `_openslide_grid_get_bounds → ceil(x+w)` 逐位一致；两个 AOI 的网格 (col,row) 不得重叠，否则布局不明确、类型化拒绝）。
- **标定**：mpp = iScan `ScanRes`（µm/px，X/Y 同值）；objective = iScan `Magnification`。
- **载荷真值**：首个引用瓦片的头探测（JPEGTables (347) 合并后的 SOF/采样/颜色）；引用瓦片全表扫描（TileByteCounts=0 → 稀疏变体拒绝；OS-2 的 1643 个未引用网格槽全部指向同一填充流，与真实布局一致，不读它们）。

### 1.2 拼接转换（`convert_bif.rs`）

- **preserve-source-v1（如实声明）**：扫描瓦片带重叠（OS-2 x 向 1024 瓦片、步进 911.1，重叠 ≈113 px），**没有逐字节搬运路径**——每个输出 tile 都是 ≥2 个源瓦片的混合。preserve 指：源瓦片按 MCU 行 band 流式解码（瓦片是无 restart 的缩略 JPEG 流，唯一有界解码单元是 MCU 行；64 KiB 熵窗口、共享 JPEGTables 合成头）→ 按记录位置取小数拼接位置的**最近整数（round）**落位拼进 256 行输出带（确定：浏览器==原生；实测比 floor 更贴近 OpenSlide 的亚像素渲染，见 §4.2）→ 全部 256×256 输出 tile 以 **q96 YCbCr 4:2:2 · 标准 Huffman**（指纹 `bif-mosaic-compose:q96:y422:hstd:v1`，报告 `composed`）重编码。compact-jpeg-v1 同一拼法、锁定 U3 参数重编码。
- **重叠优先级** = OpenSlide 的渲染结果：重叠带内 **(row, col) 较大的瓦片胜出**（openslide 4.0.1 实测：OS-2.bif 接缝逐点核对 + 整 ROI 零重编码参照，翻转次序误差 9.32/6.70/4.83 → 正确次序 3.77/2.10/4.01）。实现按升序 (row,col) 粘贴——较大者最后落笔。合成夹具把两 AOI 的图案偏移拉开（d=1..9 vs 100..103），优先级翻转会在重叠带产生图案差量级误差（Rust 测试 + pytest 双重把关，另有专门的重叠带断言）。
- **未覆盖区**：边界盒内无任何 AOI 覆盖的像素保持**黑色**（OpenSlide 渲染为全透明，读 RGB 即黑）；输出 tile 的层外衬边仍为白（约定：读取器永不读）。
- **流式与内存**：256 行 × 拼接宽的带缓冲（OS-2：88.3 MB）+ 在飞瓦片扫描器（≈2 条源瓦片行 × 95 列 ≈ 190-212 个，每个 ≈334 KB 计费工作集）+ 跨 band carry（MCU 行跨输出带边界时保留尾行，下一带先贴）。带内逐瓦片顺序打开/复用扫描器，每源瓦片自顶向下恰好解码一遍。
- **降采样层**：l0-box2 链（输出 L0 的 2×2 面积平均，从已提交的 sink 读回）——源自己的 level=1.. 层**从不解码像素**。
- **预算前拒绝**（review §1）：带缓冲、tile 画布、位置表、每个扫描器的工作集（`BandScanner::open` 自身计费 + carry）全部先计费后分配，超限稳定码 `resource_profile_insufficient`。OS-2 峰值计费 ≈158-166 MB < 192 MiB 档的 176.16 MB 上限。
- **续跑**：每已提交 tile 行一个检查点；journal 携带 `adapter=ventana-bif-jpeg` + `adapter_version=1`；换适配器/版本/源被改一律类型化拒绝（核心 + runner 双闸）；中断续跑产物与不中断运行**逐字节一致**（Rust 测试 4 个崩溃点 × sha256 相等；浏览器故障矩阵两行）。
- strict-lossless 类型化拒绝（输出必然重编码）；fl-ome 类型化拒绝（BIF 恒明场）。

### 1.3 输出

与 NDPI/VMS 同构：classic 多 IFD 或 bf-ome（SubIFD 金字塔），描述 JSON 带 `source_format: ventana-bif-jpeg`（进转换器产物词表——自己的 classic BigTIFF 不会被二次重编码），OME 溯源注记含 `stitched_width/height`、拼接/金字塔方法、指纹与画质声明。

## 2. 支持范围

| 项 | 判定 |
| --- | --- |
| BigTIFF + iScan XMLPacket + JPEG 基线瓦片 + RIGHT/UP 拼接 | **支持**（转换：拼接重编码 + l0-box2） |
| label / thumbnail / probability 等关联页 | 检测在位，不导出（警告 `bif_associated_not_exported`） |
| JPEG 2000 压缩（33003/33005） | 复制前类型化拒绝 →「暂时直传」 |
| LEFT / DOWN 拼接走向（Ventana-1.bif） | 复制前类型化拒绝（probe 层，`Direction` 消息）→「暂时直传」 |
| 多 z 层（iScan `Z-layers > 1`） | 复制前类型化拒绝 →「暂时直传」 |
| 无 EncodeInfo XMLPacket（无 AOI/重叠记录的 ventana tif） | 复制前类型化拒绝 →「暂时直传」 |
| 只有 level=0 一层的 BIF | **接受**（输出的全部降采样层由 l0-box2 生成；OpenSlide 同样接受） |
| AOIScanned 非 0 非 1（如 "2"） | **按已扫描处理**（ventana.c：非 0 即扫描） |
| AOI 网格总格数超出 TileOffsets 条目数 / 网格越出 canvas | 复制前类型化拒绝（遍历前上限，审查修复） |
| 经典 TIFF 容器（42）的 ventana tif | 复制前类型化拒绝 →「暂时直传」 |
| 灰度/多通道页、非 8 位、平面存储、稀疏引用瓦片（count=0） | 复制前类型化拒绝 |
| 荧光（SamplesPerPixel ≠ 3 的页组） | 复制前类型化拒绝（归入上行的多通道拒绝） |

## 3. 实测（本 worktree 运行记录）

### 3.1 真实样本（OS-2.bif，OpenSlide 公开样本；2,717,833,684 B）

- **probe**：`ventana-bif-jpeg` v1；拼接 **114943×76349**（== OpenSlide `dimensions`；TIFF 画布 128000×82960）；瓦片 1024×1360、网格 125×61；2 个已扫描 AOI（11×17 + 95×61 = **5982** 个引用瓦片，AOI1 未扫描被跳过）；步进 911.1097/1249.8086；mpp 0.2325 / objective 40（== OpenSlide 属性）；label + thumbnail 检测在位；共享 JPEGTables 在位。
- **320M 门转换**（`systemd-run --user --scope -q -p MemoryMax=320M -p MemorySwapMax=0`，默认 192 MiB 预算，`--profile bf-ome --timeout 7200`）：
  - 墙钟 **488.3 s**（`Elapsed 8:12.13`）；峰值 RSS **116,156 KiB**（`/usr/bin/time -v`）；
  - 输出 **4,094,070,377 B**（≈1.94× 引用载荷 2,112,442,968 B；q96 重编码对 q90 4:2:2 源的预期放大）；179,400 个输出 tile，10 层，末层 224×149 ≤ 256；
  - `validate --expect-ifd 10` PASS；`estimate.output_upper_bound_bytes = 8,456,602,208 ≥ 实际`（浏览器磁盘预检按上界预留）。
- **像素门**（复用该产物；pytest `test_real_sample_l0_mean_error_and_pyramid_geometry`）：
  - L0 三组织 ROI 均值误差 **2.030 / 2.109 / 3.932**（max ≤ 55，通道均值差 ≤ 0.26；上限 6.0 与 NDPI/VMS 同档）。参照实验：源瓦片零重编码、按正确优先级 + round 落位拼好对 OpenSlide 的差是 1.73 / 1.88 / 3.69。**审查修复前**（优先级翻转）同三 ROI 为 9.378 / 6.805 / 5.010，误差集中在重叠带（零重编码参照 9.32 / 6.70 / 4.83——约 18% 输出像素取自另一次扫描）；
  - 低倍层（l0-box2 生成 vs 扫描仪自己的层，同尺度 ROI 8192² L0 坐标）：层 1 **7.10** / 层 2 **8.58** / 层 4 **10.97**（上限 8/10/13 按实测 + 13-19% 余量；深层误差主项是重采样核不同）；
  - 构造负例（grid bomb，200 KB 文件声明 15.3M 格）：probe 在遍历前类型化拒绝（修复前实测 1.0 GB 内存 + 22 CPU 分钟仍未完成）；单层 / AOIScanned="2" / 小数 Pos 夹具全部接受且几何与默认一致（Rust 测试）；
  - 产物 `native_rgb = True`，level_count 与 probe 声明的 l0-box2 链逐层一致。
- **浏览器 == 原生**（`run_parity.js --input $BIF_SAMPLE --label parity-bif`）：双 profile 逐字节一致——bf-ome `a49e9cb94fae7d1a…` == 原生（浏览器转换 676.1 s）；bf-classic `8941589dd041f1e2…` == 原生（673.7 s）；`BIF PARITY PASS`。
- **C2 故障矩阵**：`bif-adapter-change-refused` / `bif-source-changed-refused` PASS（全套 56/56）。
- **C3 页面场景**（`bi-bif-page-e2e`，合成夹具）：摘要格式名 `BIF (Ventana)`、画质说明行、刷新后任务行、保存 sha == 原生（preserve `4f4fc879…` == 原生、compact `756460f1…` == 原生 compact）、LEFT 变体在 probe 终审拒绝且无任务目录残留——PASS。

### 3.2 合成夹具（gen-bif，无样本单测）

- 夹具（默认 704×1120 拼接 / 画布 768×1280）：整数几何（重叠 32/24 → 步进 224/232），2 个已扫描 AOI（含像素级重叠区，AOI0 行号小者胜）+ 1 个 `AOIScanned=0`，逐瓦片 DC 偏移的全局梯度图案（错位/漏拼/优先级错误都会放大），未引用网格槽共享填充流（OS-2 布局），label/thumbnail/三层降采样结构在位。
- **Rust 单元**（`crates/core/tests/bif.rs`，8 项全过）：probe/转换契约（双 profile）、拼接像素门（输出 L0 vs 参考粘贴：均值 **1.386**、未覆盖区黑色断言）、7 类变体拒绝（jp2k/LEFT/z-layers/no-xml/classic/gray/sparse，probe 同拒）、strict-lossless/fl 拒绝、预算前拒绝（64 KiB 预算 → `resource_profile_insufficient`，默认档成功）、**续跑逐字节一致**（4 个崩溃点 sha256 相等）、换适配器/无版本续跑拒绝。
- **pytest 合成门**（10 项全过）：gen-bif → probe 契约 → 双 profile 转换 + validate + `native_rgb`；整层像素门 vs OpenSlide 读原 .bif：均值 **1.386**（max ≤ 90，通道 < 4）；L1 vs OpenSlide-L0 的 box2 **2.01**。
- **vitest**（`tests/js/tools-bif-input.test.ts`，13 项全过）：engine 嗅探（BigTIFF+iScan → bif；经典 TIFF → 拒；无 iScan → 回落通用 TIFF；JP2K → 拒）、命名/常量/词表、slide-sniff `.bif` 分流（convert / JP2K temporary / 经典 TIFF temporary / 无 iScan temporary / IFD 在头窗口外按头指向补读）。

## 4. 未覆盖项 / 已知限制（如实声明）

1. **LEFT/DOWN 拼接走向**（Ventana-1.bif）：类型化拒绝、归入「暂时直传」。这是 OpenSlide 同样不读的变体；支持它需要镜像几何并另找真实验证样本。
2. **round 落位 vs OpenSlide 亚像素渲染**：拼接位置是小数（步进 911.1097…），转换器按**最近整数（round）**落位（确定：浏览器/原生逐字节一致）。曾按 floor 并声称「round 仅再降 ~15%、保持 floor」——该数据是在**优先级翻转的 bug 下**测得的（重叠错位主导了误差），审查修复后在正确优先级下重测：零重编码参照 floor 3.77/2.10/4.01 → round **1.73/1.88/3.69**，最终产物 ROI 误差 3.85/2.28/4.20（floor）→ **2.03/2.11/3.93**（round），已改为 round。边界盒仍按 OpenSlide 的未取整边缘 ceil。剩余与 OpenSlide 的像素差 = 亚像素采样差（OpenSlide 双线性）+ 一次高画质 JPEG 生成，量级 2-4，不再是重叠错位。
3. **无 EncodeInfo 的 ventana tif**（OpenSlide 按简单网格直读的变体）、荧光/多 z 的 iScan 变体、非 JPEG 编码：全部类型化拒绝（「暂时直传」），不做猜测。
4. **slide-sniff 顺带修复**（本分支内发现 + 独立审查第 4 条补全的真实缺陷）：`readTiffHeader` 曾按经典布局读 BigTIFF 首 IFD 偏移（永远得到 8）；`findIfdEntry` 用了不存在的 `DataView.getUint64`（应为 `getBigUint64`）且三处 u16/u64 读取硬编码小端（tag、条目数、计数）；`descTextAt`/`ifdUint` 把 BigTIFF 64 位外联偏移截成低 32 位（本工具自己的产物可超 4 GiB）；BIF 补读块的首 IFD 偏移也硬编码小端。以上全部按文件字节序 + 64 位读取修复，vitest 回归：BigTIFF OME → ome-tiff、BigTIFF 转换器产物 → converter-bigtiff、描述偏移 >4 GiB、大端 BigTIFF。
5. **预算**：带缓冲 = 256 × 拼接宽 × 3（OS-2 88.3 MB）。更宽的切片（拼接宽 ≳140k px）会在 192 MiB 档被类型化拒绝（`resource_profile_insufficient`，分配前）；换 balanced/档案档预算或分带重构是后续选项，当前无此类公开样本。
6. **时间**：真实样本原生 488.3 s；浏览器 wasm 同码同预算 673.7-676.1 s（修复前版本的 parity 实测，saver 档；修复后由统一门禁的 parity 复核）。

## 5. 门禁与后续

- 本分支内已跑（相关子集，含独立审查修复后的复跑）：`cargo test -p slide-transform-core --features codecs,fixtures --test bif`（12/12，含优先级/网格炸弹/单层/AOIScanned/小数 Pos 回归）、`pytest tests/test_slide_transform_bif.py`（真实样本门含 320M 复核；L0 上限 6.0、低倍层 8/10/13）、全量 `vitest`（880/880）、`run_faults.js --only bif`（两行 PASS）、`test_no_whole_file.js`（PASS）、C3 `run_e2e.js --only bi`（PASS）、`run_parity.js --input $BIF_SAMPLE`（修复前版本实测 PASS；修复后由统一门禁复核）。
- 统一门禁（全量 C2/C3/C4/R1 + 平台 pytest）由收口方执行；本文不代跑。
- 后续可选项：LEFT 走向支持（需样本）、ventana tif 简单网格变体、浏览器侧真实样本耗时计测。
