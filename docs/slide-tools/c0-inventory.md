# C0-① 盘点报告：真实样本变体、数据语义、基线 oracle 与调用者清单

- 日期：2026-09-29。执行分支基线：PathTogether HEAD `6b436560228fa6131e1fdff33900b434034bbf92`（`git rev-parse HEAD` 复核）。最高迁移号 `migrations/0076_conversion_jobs_held.sql`（下一个可用 0077）。
- 工作区 dirty 文件（admin 插件、注册邮件、登录弹窗等）属他人未提交改动，本次未触碰、未 stage、未修改任何已跟踪文件；新文件只落在 `docs/slide-tools/` 与 `.gate-tmp/slide-tools-c0/`。
- 本报告是**事实盘点**，不是支持声明。样本一律用别名 + sha256 指认；原始文件名、缩略图、标签图、样本字节不入仓（别名映射表仅存 `.gate-tmp/slide-tools-c0/inventory/alias-map.txt`）。
- 复跑命令：`.gate-tmp/slide-tools-c0/inventory/RERUN.md`。

## 0. 摘要（关键结论）

1. **真实样本只有两个厂商变体**：明场 `kfb_kfbio_jpeg`（KFB-1，1 份）与荧光 `kfbf_kfbio_jpeg`（KFBF-A..D，同一扫描仪 KFFL02000113023、同一 6 通道 panel）。没有 >4 GiB、MRXS、OME、SVS 真实样本——这些格式 v1 门禁未过，只能登记 "no real sample — not supportable in v1"。
2. **两种变体的 tile codec 高度统一**：baseline（非渐进）JPEG；明场 3 分量 4:2:2（2 张量化表），荧光单通道灰度（1 张量化表）；全部样本/层/通道抽样到的量化表完全相同（luma 4..48 / chroma 7..40）。这缩小了 C1 编码器面，但**不能**推及未知厂商变体。
3. **channel.json 裁决：可选伴随输入（optional companion）**。4 份样本中它与 KFBF 本体的通道名/颜色/gamma 完全一致、零冲突；它**独有**显示窗口（lower/upper）、show 标志、offsetX/offsetY，本体独有曝光/扫描仪/通道数。缺它转换不受影响；浏览器输入应支持「KFBF + 可选伴随目录」并在有伴随时吸收显示窗口。注意 upper 可为 258/260（>255，显示缩放概念，非数据范围）。
4. **基线 oracle 全绿**：5 份样本现有 Python converter 全部成功，wall 0.97–3.34 s，峰值 RSS 274 MiB–1.02 GiB（RSS 随源文件近线性——mmap 读全量 tile 的代价，浏览器端不可照搬，必须流式）。完整 tile 原字节搬运 31650/32277（KFB-1）；边缘重编码 627（KFB-1）、每样本 1260–1980 ×6 通道（KFBF）；稀疏黑填充 810–1776 ×6 通道。**全部边缘重编码复用了源量化表，无一例 quality=95 回落**（manifest warnings 无 `edge_reencode_fallback_q95`）。
5. **独立 reader 双开通过，附一个互操作注意点**：tifffile 2024.5.22 与 Bio-Formats 8.5.0（`showinf -nopix`）都能打开全部 5 个产物；明场经典多 IFD 金字塔在 Bio-Formats 显示为单 series（仅全分辨率层）；OME 产物 17 series × 6 plane 全读，通道名/颜色/PhysicalSize 保留，但 **Bio-Formats 再序列化丢弃 Channel@ExposureTime**（OME 模型中该属性属 Plane）——C1 metadata 映射要修正挂载位置或接受丢失。
6. **调用者清单**（§8 退役面）跨 12 个主模块 + docker 入口 + 镜像 COPY + 前端轮询/重试/i18n + 26 个测试文件（约 360 个测试函数）；HistoPilot 跨仓确认无依赖。

## 1. 真实样本登记（别名制）

| 别名 | 类型 | 大小 (bytes) | sha256（全文件） |
|---|---|---:|---|
| KFB-1 | KFB 明场 | 221,189,354 | `17fe1cfde1a6d1f1d8778ea5854db5fd353f657e51003c606b404b60300aab71` |
| KFBF-A | KFBF 荧光 | 946,972,112 | `1d10e48fa6e7a7da96dfe5ada5d4c55199fd77ac29560b537fa9e31a888c78f6` |
| KFBF-B | KFBF 荧光 | 530,063,959 | `0ad51d3dce5d6fcdb5db5a4ea220dd452c9c2d220d590a6cde4d76d83e0e2c94` |
| KFBF-C | KFBF 荧光 | 481,661,346 | `8fe031ad72534a82325ffc56100bbe7bf275ae9808250f9f523ea0ce63a58963` |
| KFBF-D | KFBF 荧光 | 806,456,145 | `4aa66d2fda0d7a9a9f5509063f695a8b9c53b59dcfa9f723d0819b8329f03013` |

别名按文件名排序指认（KFBF-A< B < C < D）；映射表在 `.gate-tmp`（私有）。每份 KFBF 均带伴随目录 `<名>_kfbf/Annotations/channel.json`（1228–1230 bytes）。证据命令：`sha256sum`（§9 RERUN）。

## 2. 变体盘点（现有 parser 输出 + 原始头取证）

探针脚本 `.gate-tmp/slide-tools-c0/inventory/probe_samples.py` 只调用现有 `kfb.parse_kfb` / `kfb.parse_kfbf`（kfb/parser.py:265、kfb/vendor_kfbf.py:255），不改代码；原始 JSON 逐字段输出在 `.gate-tmp/slide-tools-c0/inventory/<别名>.inventory.json`。

### 2.1 KFB-1（明场厂商变体 kfb_kfbio_jpeg）

分发路径：`parse_kfb` → `looks_like_vendor`（kfb/vendor_kfbio.py:58，version≠1 且 codec=JPEG、tile=256）→ `parse_vendor`（kfb/vendor_kfbio.py:70）。

| 字段 | 值（KFB-1） | 代码依据 |
|---|---|---|
| magic | `f1 01 ee ee 4b 46 42 00` | kfb/parser.py:46 MAGIC |
| version 字段（0x08 u32） | 0（非合同 v1） | vendor_kfbio.py:62-67 |
| 尺寸（0x14 高 / 0x18 宽，注意高在前） | 46152 × 34013 | vendor_kfbio.py:72 |
| objective（0x1C） | 20（整数倍率） | vendor_kfbio.py:77 |
| codec（0x20 4s） | `JPEG` | vendor_kfbio.py:79 |
| 索引偏移（0x44 u64）/ 条目 | 219,119,902；32,277 条 × 64B | vendor_kfbio.py:87 |
| MPP（0x4C f32） | 0.48410487174987793 µm/px（x=y） | vendor_kfbio.py:84 |
| tile 边长（0x58） | 256 | vendor_kfbio.py:81-83 |
| scanner_id | tagged 段 ff01eeee tag29 → `KFPBL40000110032` | vendor_kfbio.py:165-187 |
| 关联图 | f102/f103 记录扫描，按面积映射 overview/label/thumbnail | vendor_kfbio.py:190-229 |

层级几何：`_pyramid_levels`（vendor_kfbio.py:152-162）floor 减半，止于首个 1×1 网格层——KFB-1 得 9 层（34013×46152 → 132×180），与 §0.4 评审样本同族。层级明细（层，宽×高，网格，tile 数，完整/边缘）：

| L | 尺寸 | 网格 | tile | 完整 256² | 边缘（重编码） |
|--:|---|--:|--:|--:|--:|
| 0 | 34013×46152 | 133×181 | 24073 | 23760 | 313 |
| 1 | 17006×23076 | 67×91 | 6097 | 5940 | 157 |
| 2 | 8503×11538 | 34×46 | 1564 | 1485 | 79 |
| 3 | 4251×5769 | 17×23 | 391 | 352 | 39 |
| 4 | 2125×2884 | 9×12 | 108 | 88 | 20 |
| 5 | 1062×1442 | 5×6 | 30 | 20 | 10 |
| 6 | 531×721 | 3×3 | 9 | 4 | 5 |
| 7 | 265×360 | 2×2 | 4 | 1 | 3 |
| 8 | 132×180 | 1×1 | 1 | 0 | 1 |

网格覆盖完整（0 缺失；`_level_grid` 缺 tile 即 `conversion_validation_failed`，kfb/converter.py:250-262）。边缘 jpeg 尺寸含右/底几何裁剪（如 221×72、256×7）。关联图：overview 1632×696（59,094 B）、label 804×808（34,363 B）、thumbnail 136×184（3,160 B），均 RGB JPEG。

### 2.2 KFBF-A..D（荧光厂商变体 kfbf_kfbio_jpeg）

同一 parser 合同（kfb/vendor_kfbf.py docstring 布局即由此 4 份样本校准）。公共头字段：

| 字段 | A | B | C | D | 代码依据 |
|---|---|--:|--:|--:|---|
| magic / version / 格式版本 | `f101eeee4b464246` / 0 / 2.1 (f32@0x0C) | 同 | 同 | 同 | vendor_kfbf.py:73,76,297-308 |
| L0 尺寸（H×W） | 55608×48856 | 47382×43029 | 53370×31023 | 51528×48920 | vendor_kfbf.py:309 |
| objective / tile | 40 / 256 | 40 / 256 | 40 / 256 | 40 / 256 | vendor_kfbf.py:314-325 |
| MPP (f32@0x4C) | 0.2506265640258789 | 同 | 同 | 同 | vendor_kfbf.py:320-322 |
| tile_count / index_offset | 55353 / 943429468 | 41798 / 527388835 | 34002 / 479485166 | 51480 / 803161373 | vendor_kfbf.py:309-329 |
| scanned_at (unix, 0x2C) | 1789135116 | 1789136831 | 1789138045 | 1789139188 | vendor_kfbf.py:318 |
| scanner_id (tag29) | KFFL02000113023 | 同 | 同 | 同 | vendor_kfbf.py:339-341 |

通道元数据（tag77 名 / tag79 色 / tag84 曝光 / tag87 gamma，`_parse_channels` vendor_kfbf.py:384-419）——4 份样本名/色完全一致，曝光随样本：

| 通道 | 颜色 | 曝光 A / B / C / D（原始标定值） | gamma |
|---|---|---|---|
| DAPI | #0000E5 | 8.0 / 7.0 / 6.0 / 8.0 | 1.0 |
| 480 | #80FFFF | 3.0 / 3.0 / 3.0 / 3.0 | 1.0 |
| 520 | #00FF00 | 2.0 / 2.0 / 2.0 / 2.5 | 1.0 |
| 570 | #FF0000 | 2.5 / 2.0 / 2.5 / 2.0 | 1.0 |
| 620 | #FF8000 | 0.8 / 1.0 / 1.0 / 1.0 | 1.0 |
| 690 | #FFFF80 | 15.0 / 15.0 / 15.0 / 15.0 | 1.0 |

曝光单位是现有代码的**假定**：`converter_fl.py:556` manifest 写 `"exposure_unit": "ms(assumed)"`、OME 写 `ExposureTimeUnit="ms"`（converter_fl.py:177）。tag84 原始值仅是 1..15 的标定数，无单位字段——该假定不得升级为“已确认”（方案 §1.1 明令）。

层级几何（`level_dimensions`，vendor_kfbf.py:220-238）：4 份样本全部 17 层（L0=header；L1..L3 floor 减半；L4..L8 = ceil(L0/256)·2^(8-L)；L9+ floor 减半至 1×1）。以 KFBF-A 为例：48856×55608 → 24428×27804 → 12214×13902 → 6107×6951 → 3056×3488 → … → 191×218（L8，= ceil(48856/256)×ceil(55608/256)）→ 95×109 … → 1×1（L16）。L≥1 的层网格 extent 必须与公式精确一致，否则 `invalid_tile_index`（vendor_kfbf.py:507-515）。

每层网格统计（cells_present = 索引实有；missing = 稀疏黑填充候选；edge = jpeg 尺寸≠cell 尺寸即重编码候选；数值为每通道，全部 6 通道同网格）：

| 样本 | L0 网格 | L0 present/missing/edge | L1 | L2 | L3 | L4..L16 |
|---|---|--:|--:|--:|--:|---|
| KFBF-A | 191×218=41638 | 41406 / 232 / 176 | 10428/36/88 | 2622/18/44 | 662/10/21 | 全满、无边缘 |
| KFBF-B | 169×186=31434 | 31210 / 224 / 152 | 7873/32/80 | 2005/16/40 | 520/8/20 | 全满、无边缘 |
| KFBF-C | 122×209=25498 | 25426 / 72 / 112 | 6369/36/56 | 1625/18/28 | 423/9/14 | 全满、无边缘 |
| KFBF-D | 192×202=38784 | 38552 / 232 / 176 | 9660/36/88 | 2430/18/44 | 615/9/22 | 全满、无边缘 |

边缘形态实证：L0 底部数行整行 jpeg 高度被厂商裁短（如 A 的 row215 全行 256×62，而 cell 是 256×256）——即“L0 底行内容可能被裁剪，缺失网格按黑填充”的 docstring 语义（vendor_kfbf.py:43）。稀疏缺失只出现在 L0–L3；L4 起网格完整。

关联图：overview 1676×872、label 988×892（两尺寸 4 份一致，RGB JPEG，header 指针 0x34/0x38，vendor_kfbf.py:522-558）；thumbnail 经文件末尾 52B f102 记录 + `*p1` 二级间接（灰度，尺寸随样本 124×212~196×204，vendor_kfbf.py:561-583）。

### 2.3 tile codec 证据（SOF / 采样 / 量化表）

抽样法：每层取首/中/末 tile（KFBF 另 ×6 通道），`scan_jpeg`（kfb/parser.py:208-259）读 SOF，Pillow 读模式/采样/量化表。结果高度统一：

| 变体 | SOF 分量 | 采样 | progressive | 量化表 | 表值范围 | 首张表前 8 值 |
|---|---|--:|---|---:|---|---|
| KFB-1（全 9 层，25 抽样） | 3 (YCbCr) | (2,1,1,1,1,1)=4:2:2 | 否 | 2（luma+chroma） | luma 4..48；chroma 7..40 | [6,4,4,6,10,16,20,24] |
| KFBF-A..D（17 层 ×6 通道，各 32-33 抽样） | 1（灰度） | n/a | 否 | 1 | 4..48 | [6,4,4,6,10,16,20,24] |

所有抽样 tile 量化表完全相同（同一厂商编码器固定质量）。关联图 JPEG 为 RGB 4:2:2（明场采样同款）。含义：C1 共享核心只需实现 baseline JPEG 解码 + 量化表复用重编码即可覆盖已知变体；但 parser 对未知 magic/version/codec 一律 fail-closed（`unsupported_kfb_variant`，errors.py:12-23），支持矩阵不得外推。

### 2.4 现有转换器对字段的取舍（honored / ignored / assumed）

- **荣耀（写入产物/manifest）**：MPP→TIFF 分辨率 tags（converter.py:145-156、converter_fl.py:151-160）；objective/scanner_id→明场 ImageDescription JSON（converter.py:327-334）或 OME NominalMagnification/Name（converter_fl.py:166-202）；通道名/色/曝光/gamma→OME Channel + manifest（converter_fl.py:551-558）；关联图→`.associated/*.jpg` + manifest sha256（converter.py:424-426、manifest.py:44-53）。
- **忽略（读出但未用于转换）**：KFBF `scanned_at`（仅进 manifest source，converter_fl.py:535）；KFB tile 记录 scale 字段（仅用于推导 level，vendor_kfbio.py:112）；KFBF 指针块后 6 条冗余回链（vendor_kfbf.py:39 docstring「不使用」）；明场 1×1 网格层之后的冗余单 tile 层（vendor_kfbio.py:113-114 丢弃）。
- **假定（无本体证据）**：曝光单位 ms（见 §2.2）；KFBF L0 底行裁剪区显示语义=黑（荧光背景即黑，converter_fl.py:10-11）；明场边缘 tile 白底（converter.py:207，明场载体白底惯例）。
- **不读取**：伴随 `channel.json`（全部通道信息取自 KFBF 本体 tags）。

## 3. channel.json 比对与裁决

结构（4 份一致）：顶层 JSON 数组，6 个对象；字段 `channelName, channelIndex(1-based), channelColor(#RRGGBB), lower, upper, offsetX, offsetY, gamma, show`。逐字段与本体（tag77/79/87）比对（探针 `channel_json_comparison` 字段，4 份原始 JSON 见 `.gate-tmp`）：

| 信息 | KFBF 本体 | channel.json | 判定 |
|---|---|---|---|
| 通道名（DAPI/480/520/570/620/690） | 有（tag77） | 有，**顺序与值完全一致**（names_match_order=true ×4） | 冗余一致 |
| 通道颜色 | 有（tag79，值相同 ×4） | 有，`colors_match=true` ×4 | 冗余一致 |
| gamma | 有（tag87，全 1.0） | 有，全 1 | 冗余一致 |
| 通道数 | tag75（=6） | 隐含（数组长度） | 一致 |
| **显示窗口 lower/upper** | **无** | 有（逐样本不同，如 A=[0..178]、B 690 upper=258、D DAPI upper=260） | **仅 channel.json** |
| **show（初始显示开关）** | **无** | 有（逐样本不同） | **仅 channel.json** |
| **offsetX/offsetY（配准偏移）** | **无** | 有（全部 0） | **仅 channel.json**（非零时是显示层配准信息） |
| 曝光 | 有（tag84） | **无** | 仅本体 |
| channelIndex | 0-based（tag77 序） | 1-based | 约定差异，映射需 -1 |

**冲突：4 份样本零冲突**（名/色/gamma 全一致）。**裁决：可选伴随输入（optional companion）**——
1. 现有转换器不读它且转换语义完备 → 不是必需输入；缺失不能报错，只能提示“显示窗口不可用”。
2. 它承载本体没有的**显示层**信息（lower/upper、show、offset）→ 按 §0/§6「显示 LUT 与数据分离」原则，浏览器工具在有伴随时应吸收为默认显示窗口并写入版本化 manifest 的 display 段；无伴随时显示窗口标记 unknown，不从数据猜。
3. 若出现名/色冲突：以本体为准并在 warnings 记录（本体是像素出处）；**禁止**用 channel.json 反过来裁决本体。
4. 输入形态：浏览器/插件须支持「KFBF + 可选伴随目录 `<名>_kfbf/Annotations/`」（方案 §1.1 预判成立）。

## 4. 基线 oracle（现有 Python converter，未改源码）

驱动：`run_oracle.sh` → `/usr/bin/time -v .venv/bin/python run_oracle_one.py <kind> <src> <dst>`，TMPDIR=`.gate-tmp`。产物/manifest/计时原始文件在 `.gate-tmp/slide-tools-c0/inventory/oracle/`。

| 样本 | converter | wall | 峰值 RSS | 输出大小 | 输出 sha256 |
|---|---|---:|---:|---:|---|
| KFB-1 | convert_kfb → KFB-1.tif | 0.97 s | 280,196 KB | 220,055,311 | `385a59c6c69478c26fcac9f4232d065137864f5be47aa398f6222c6e818657fd` |
| KFBF-A | convert_kfbf → KFBF-A.ome.tif | 3.34 s | 1,070,292 KB | 944,993,116 | `b096e9d14f996e347147cc0c2d20aa792f2e63db823663cc404ec9d8c1c22781` |
| KFBF-B | 同 | 2.29 s | 639,352 KB | 528,681,298 | `75cf65b1a79db11680f1a8b57e0b80e70bbeae2d860cef860a111210d43917ad` |
| KFBF-C | 同 | 1.93 s | 578,264 KB | 480,045,158 | `e0a120eb20a5acd692977eca22230fae99f21e79bdad77f77539448813fd15d5` |
| KFBF-D | 同 | 3.07 s | 925,880 KB | 804,751,889 | `da6e9cabc67d4fcdefdf6869da0935a17d9a842ca8b6a83357c63d2490613214` |

RSS 观察是与源大小的近线性关系（mmap 顺序读全量 tile，页缓存计入 RSS）——这是 Python oracle 的实现属性，**不是**问题下界；浏览器端 §4 预算要求有界读缓存，C1/C2 差分时须按 tile 流式而非按 mmap 复制该行为。

tile 处置统计（manifest sidecar 逐层聚合；未改 converter，warning 即仪表）：

| 样本 | 完整 tile 原样搬运 | 边缘重编码 | 稀疏黑填充 | warnings |
|---|--:|--:|--:|---|
| KFB-1（单层金字塔） | 31,650 | 627 | n/a | `[]` |
| KFBF-A（×6 通道合计） | 330,144 | 1,974 | 1,776 | `["sparse_fill_black"]` |
| KFBF-B | 249,036 | 1,752 | 1,680 | 同 |
| KFBF-C | 202,752 | 1,260 | 810 | 同 |
| KFBF-D | 306,900 | 1,980 | 1,770 | 同 |

**量化表复用 vs q95 回落**：5 份产物 warnings 均无 `edge_reencode_fallback_q95`（converter.py:53、converter_fl.py:45）→ 全部边缘重编码走了 `_reencode_*` 的源量化表复用分支，无 quality=95 兜底。黑填充块为 quality=90 合成（converter_fl.py:259-269，按尺寸缓存复用同一压缩段）。与 §2.5 探针预判逐层一致（如 KFBF-A L0：176 重编码/232 填充 ↔ manifest L0 per-ch re=176 black=232）。

## 5. 独立 reader 验证

**tifffile 2024.5.22**（`validate_oracle.py`，原始输出 `tifffile-validation.txt`）：
- KFB-1.tif：BigTIFF，9 页/1 series，axes YXS shape (46152, 34013, 3) uint8，9 levels 形状逐层正确；page0：JPEG、photometric=6(YCbCr)、tile 256×256、24,073 tiles、spp=3、bps=8。
- KFBF-*.ome.tif：BigTIFF+OME，顶层 6 页（=6 通道），series CYX (6, H, W) uint8，17 levels；page0：JPEG、photometric=1(BlackIsZero)、spp=1；L1+ 经 SubIFD 挂接（levels_detail 中 `+subifd`）。OME-XML ~1.6 KB 含 NominalMagnification=40、PhysicalSizeX=0.2506…、6 通道名。
- 附带发现：tifffile 先访问 series 会把页降级为无 tags 的 TiffFrame——converter 自校验已处理（converter_fl.py:598-604），任何新验证脚本同样必须先物化 `list(tf.pages)`。

**Bio-Formats 8.5.0**（`bftools showinf -nopix -novalid`，JDK 21.0.12.1-jre，日志 `*.showinf.log`）：
- KFB-1.tif：识别 "Tagged Image File Format"（TiffDelegateReader）；Series=1，Image count=1，RGB=true(3)，34013×46152，uint8，tile 256×256，YCbCrSubSampling 半色度。**注意点：经典多 IFD 缩减层不作为 resolution 暴露**——BF 视角只有全分辨率层。
- KFBF-A.ome.tif：OMETiffReader；**Series=17**（每金字塔层一 series），每 series Image count=6、SizeC=6、XYZCT、uint8、tile 256×256。`-omexml` 再序列化：通道名 DAPI/480/…、Color 保留为有符号 int32（如 DAPI -16776987 = 0xFF0000E5）、PhysicalSizeX=0.2506265640258789 保留；**ExposureTime 属性消失**（grep 计 0）——OME 模型把 ExposureTime 定义在 Plane，我们写在 Channel 上，BF 再序列化即丢弃。C1 修正项。

## 6. 调用者清单（§8 退役面，全部 file:line 锚点，HEAD 6b43656）

### 6.1 后端转换链（核心拆除对象）

| 模块 | 锚点 | 职责 |
|---|---|---|
| `conversion_store.py` | :170 create_job、:263 activate_held_locked、:282 void_held_for_upload_locked、:305 claim_job、:331 set_project_associate、:417 get_job_by_slide_id、:433 invalidate_by_slide_id、:499 claim_one、:584 persist_commit_intent、:616 worker_settle_ready | 任务表/租约/恢复/作废（含 R16 held） |
| `conversion_http.py` | :32 handle_list、:59 handle_get、:68 handle_retry | /api/conversions 处理器 |
| `conversion_worker.py` | :45 导入 kfb convert_kfb/convert_kfbf、:55 按 KFBF_MAGIC 分派、:168 产物 manifest 校验、:221 process_job、:359 claim_one 循环、:380 main(--loop/--once) | 转换执行体（独立进程） |
| `app.py` | :11696 GET /api/conversions、:11706 GET /api/conversions/<job_id>、:11719 POST /api/conversions/<job_id>/retry（retry 前 :11727-11731 查 job+resolve_source） | HTTP 路由 |
| `app.py` | :272-273 import needs_conversion/conversion_store、:277 `_cleanup_conversion_sidecars`（invalidate_by_slide_id + 源副本 staging 删除 + legacy `.manifest.json`/`.associated`）、:11910-11946 删除编排第 5/6 步调用 | 删除联动 |
| `app.py` | :10389-10425 孤儿清理保留集含 conversion/baidu id 集 | 孤儿目录核账 |
| `app.py` | :2787 `_cos_ingestion_kind_for`（zip/native/conversion 分派，裸 .mrxs 422） | 上传受理分类 |
| `ingestion_store.py` | :126 KIND_CONVERSION、:295 conversion 特判、:2122 `worker_accept_conversion`（held 子任务创建/复用）、:2214 conversion_view | COS 侧任务接 conversion |
| `cos_ingest_worker.py` | :845/:1489 kind==KIND_CONVERSION 分派、:1245 `_conversion_critical_section`（probe→held 子任务→commit intent→源搬 staging→结算）、:1326 probe_kfb_or_fail、:1439 代表性 tile 探针 | 上传转换交接与结算 |
| `upload_content.py` | :44 import parse_kfb、:107 needs_conversion、:831 `probe_kfb_or_fail`（parse_kfbf/parse_kfb 探测格式）、:870 源副本搬入 conversion staging（跨类锁序） | 受理探测与暂存 |
| `slide_publish.py` | :352 publish_with_channel（conversion 通道复用唯一发布实现）、:506 publish_slide | 发布编排（**保留**，仅拆通道接入） |

### 6.2 百度链（迁插件对象）

| 模块 | 锚点 | 职责 |
|---|---|---|
| `baidu_ingest.py` | :146 `_probe_convert`、:248 `_ingest_convert`（:284 conversion_store.create_job → :300 claim_job → :304 **conversion_worker.process_job 同步执行在 gunicorn 进程内**）、:436 _ingest_native、:517 ingest_staging | 下载后转换入库（Web 进程内执行——迁出首要收益） |
| `baidu_import_store.py`（1988 行） | 批次/枚举/item 状态机、transfer/ingest 凭证对账（`_baidu_helpers.py` 配套） | 源站逻辑 + 平台关联混合 |
| `baidu_import_http.py` | :50 capabilities、:104 create_import、:158 retry_import 等 | HTTP 处理器 |
| `app.py` | :11210 `_baidu_ident_or_403`、:11216-11306 `/api/remote-imports/baidu/*` 路由族（capabilities/enumerations/candidates/imports/retry） | 路由 |
| `app.py` | :11951-11954 删除时 `baidu_import_store.invalidate_items_for_slide` | 删除联动 |
| `baidu_adapter.py`（841 行）/`baidu_share_parser.py` | 适配器 CLI/超时/schema 校验 | 源站访问 |
| `scripts/baidu_import_worker.py` | 常驻 worker（枚举只读 + 批次收口），docker_entry.sh:200-216 以 `BAIDU_IMPORT_WORKER=1` 拉起 | 进程入口 |

### 6.3 进程/镜像

- `docker_entry.sh:156-168`：`CONVERSION_WORKER` 环境开关 + `conversion_worker.py --loop` 重启循环；:200-216 `BAIDU_IMPORT_WORKER`。
- `Containerfile:16-18`：COPY conversion_store/conversion_worker/conversion_http、baidu 五件；:23 `COPY kfb/ kfb/`；:5-6 pip 安装 requirements.txt（:2-8 pillow、openslide-python、openslide-bin、tifffile==2024.5.22、imagecodecs——**查看/验证所需库不随 B 拆除**）。

### 6.4 前端与注册表

- `static/app.js`：:7335 「KFB / KFBF」格式项、:7346 accept 回退串含 `.kfb,.kfbf`、:7541-7563 后台任务列表轮询 `/api/conversions?group=open|recent`、:7633 转换 retry、:6444 任务详情、:6502/:6734 失败/状态文案。
- `templates/_app_shell.html`：:244 文件选择 accept（含 .kfb/.kfbf/.mrxs/.zip）、:711 后台任务列表入口。
- `static/i18n.js`：:789/:1910 `upload.cos.stage.processing`、:803/:1924 `upload.cos.conv_state`、:975/:2097 `imp.formats.mode.convert`（中/英两份）。
- `slide_format_registry.py`：:101 `.kfb`、:108 `.kfbf` convert-required 登记；:307/:318 catalog 展示 id `kfb`/`kfbf`；:118 ARCHIVE_EXTS=.zip（ZIP 独立任务，不在本方案内）。

### 6.5 脚本与运维工具

| 脚本 | 锚点 | 用途 |
|---|---|---|
| `scripts/convert_kfb.py` / `convert_kfbf.py` | :21/:30、:21/:31 | kfb 离线 CLI（依赖 kfb/） |
| `scripts/upload_drain.py` | :99-105 conversion_jobs×upload_tasks 交接核账 | 排空核账 |
| `scripts/backfill_slide_asset_state.py` | :105 转换 sidecar 命名同源注释 | 回填 |
| `scripts/audit_slide_identity.py` | :102/:140/:564 `conversion_jobs_active`/`baidu_import_items_active` 计数 | 孤儿审计 |
| `scripts/drill_slide_migration.py` | :16-17/:111-112 kfb→tif、kfbf→ome.tif 派生物演练 | 迁移演练 |
| `scripts/verify_import_project_upgrade.sh` | :66-69 引用 test_kfb_upload/test_kfbf_upload（**已于 ec06f84 随 V1/V2 链路删除，脚本引用过期**——C7 需同步更新） | 回归入口 |

### 6.6 测试（引用 conversion/kfb/baidu 的 27 个文件，函数计数）

`test_kfb_parser.py`(17)、`test_kfb_converter.py`(19)、`test_kfbf_parser.py`(15)、`test_kfbf_converter.py`(7)、`test_kfbf_real_samples.py`(1，参数化 ×4，指向 `../切片文件夹/ref`)、`test_conversion_task_api.py`(15)、`test_conversion_slide_id_pg.py`(5)、`test_baidu_ingest.py`(14)、`test_baidu_imports.py`(19)、`test_baidu_import_recovery.py`(22)、`test_baidu_adapter.py`(32)、`test_baidu_slide_id_pg.py`(4)、`test_baidu_review_regressions.py`(5)、`_baidu_helpers.py`、`test_cos_ingestion_kinds.py`(11，含 :486/:521/:535/:563/:586 conversion 交接用例)、`test_cos_ingest_worker.py`(29)、`test_ingestion_store_phase1.py`(30)、`test_ingestion_api.py`(19)、`test_usage_ingest.py`(22)、`test_capacity_lifecycle_channels.py`(3)、`test_audit_slide_identity.py`(12)、`test_slide_delete_pg.py`(10)、`test_p6_legacy_runtime_retirement.py`(6)、`test_r16_handoff.py`(10)、`test_migration_0067.py`(10)、`test_slide_id_relations_pg.py`(19)、`test_slide_id_review_r16.py`(4)、`test_upgrade_http_wiring.py`(4)、`conftest.py`、e2e `tests/e2e/import-project-upgrade.spec.ts` + `tests/js/project-import-upgrade.test.ts`。

### 6.7 HistoPilot 跨仓（HEAD `2423dfef92ea8519fb422a8d0ba4f5cc6f52f0ce`）

`grep -rn "api/conversions|conversion_job|kfb" src/ tests/`（排除 node_modules）零命中；`conversion` 仅出现于 `src/prepared-request.ts`（模型 provider 请求载荷的前后转换注释，与切片无关）；无 `api/remote-imports`/`api/ingest` 引用。**确认无依赖**，与方案 §1.1 一致；跨仓只需既有 contract 回归。

## 7. v0 能力表

机器可读版：`docs/slide-tools/c0-capability-tables.json`（schema `slide-tools.capability-tables` version 0，三表：`input_reader_capability` / `output_profile_capability` / `platform_publish_view_capability`）。要点：

- **输入 reader v0**：`kfb-brightfield-vendor-jpeg`（KFB-1 证据）、`kfbf-fluorescence-vendor-jpeg`（A–D 证据）为 supported-v0-evidenced；`kfb-bf-v1` 仅 fixture（不进用户矩阵）；`mrxs-directory`、`generic-tiff-input`、`ome-tiff-input`、`svs-input` 一律 "no real sample — not supportable in v1"。
- **输出 profile v0**：`brightfield-classic-bigtiff-pyramid`、`fluorescence-ome-bigtiff-subifd-multichannel` 有 oracle+双 reader 证据；`pixel-lossless`（边缘重编码在，不可宣称无损）、`lossy-recompression`、`svs-export` 均未具备。
- **平台 publish/view v0**：现状三条通道（COS native 直传、COS conversion kind、百度 import）+ 目标态「工具产物上传查看」仅部分成立（产物可发可读；producer import 属 C5；OME 多通道显示受 `slide_io.py:83 render_channel_limit` 约束，未逐项验证）。

## 8. 阻塞与风险（如实登记）

1. **>4 GiB / 10 GiB 无真实样本**：本机全部样本 ≤0.95 GB；大文件只能合成验收（性能门禁可用合成，格式门禁不可，方案 §10.1）。
2. **MRXS/OME/SVS 输入门禁未过**：无真实样本，v1 矩阵只能拒；MRXS 拆 ZIP 的前置能力（目录→单文件）尚无实现。
3. **厂商变体单一性**：4 份 KFBF 同扫描仪同 panel；不能宣称支持“所有 KFBF/KFB”（方案 §6）。
4. **曝光单位假定**：ms(assumed) 无本体证据；且 OME Channel@ExposureTime 被 Bio-Formats 丢弃——C1 须改挂 Plane 或接受丢失（两条都要记 manifest）。
5. **边缘重编码非无损**：qtable 复用 ≠ 无损；strict 无损需 DCT 域补边（未验证）或真无损编码或明确拒绝。
6. **经典金字塔的 Bio-Forms 分辨率可见性**：明场产物在 BF 只见全分辨率层；若 QuPath/BF 工作流需要 resolution 语义，C1 可考虑 OME 双写或 SubIFD 化明场——需用户裁决，v0 不改。
7. **python oracle RSS 随源线性**：不可作为浏览器内存预期；C2 预算另行实测。
8. `scripts/verify_import_project_upgrade.sh:66` 引用已删除的测试文件（ec06f84），C7 拆除时要修。

## 9. 复跑

见 `.gate-tmp/slide-tools-c0/inventory/RERUN.md`（探针、oracle、tifffile、bftools 四组命令与期望输出文件清单）。原始数据：`<别名>.inventory.json`、`oracle/`（产物、manifest、time.txt、showinf.log、OME dump）、`tifffile-validation.txt`、`alias-map.txt`（私有，勿外传）。
