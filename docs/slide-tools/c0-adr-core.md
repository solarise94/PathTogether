# C0-② ADR：Rust 核心 spike（KFB 明场解析 + JPEG tile 直拷 BigTIFF writer）

- 日期：2026-09-29。基线 HEAD `6b43656`（与 C0-① 同）；他人未提交改动未触碰、未 stage、未修改任何已跟踪文件。
- 代码：`experiments/slide-tools-c0/core-spike/`（Cargo workspace：`crates/core` 纯逻辑 + IO trait、`crates/cli`（`kfb2tiff`）、`crates/wasm`；`rust-toolchain.toml` 固定 1.98.1）。
- 原始证据：`.gate-tmp/slide-tools-c0/core/`（复跑命令见其 `RERUN.md`）。
- 本报告只陈述实测事实；真实样本以别名 KFB-1（sha256 `17fe1cfde1a6d1f1d8778ea5854db5fd353f657e51003c606b404b60300aab71`，221,189,354 B）指认，样本字节/缩略图不入仓。
- 对照 oracle：`kfb/parser.py`、`kfb/vendor_kfbio.py`、`kfb/converter.py`、`kfb/fixture.py`（HEAD 同版）。

## 0. 摘要

1. **完整 tile 字节级一致**：KFB-1 全部 31,650 个完整 tile（9 层）payload sha256 与 Python oracle **逐一相等**；3 张关联图字节相等；512×512 全完整 tile 夹具**整文件 sha256 相等**（证明 writer 布局、IFD、JSON 描述、RATIONAL 全部逐字节对齐 oracle）。
2. **边缘 tile 像素级近似**：627 个边缘 tile（KFB-1）解码后 max abs diff 32/255（>2 的 366 个、>8 的 75 个）。归因测量：oracle 自身相对源像素 max 45–97，spike 相对源 41–139——两边都是"解码→白底 256×256→复用源量化表重编码"的有损路径，差异来自编码器实现（Pillow/libjpeg vs zune-jpeg+jpeg-encoder），不是策略分歧。**字节级边缘 parity 在换编码器的前提下不可达**，列为开放项。
3. **内存不随文件规模增长**：KFB-1（221 MB）峰值 RSS 11.6 MB；8.9 GB 合成输入峰值 RSS **4.0 MB**（tile 索引分页落盘 + IFD 偏移数组 finish 时从 scratch 流式回放）。oracle 同样本 RSS 274–280 MB（全 tile 对象常驻）。
4. **>4 GiB 证明**：合成 69376×69376（8,905,089,609 B）→ 输出 8,902,313,525 B（8.29 GiB）；level0 73,441 tile 中 26,194 个 offset ≥4 GiB（最大 6.22 GiB），IFD 链位于 8.29 GiB；tifffile 读取并解码 >4 GiB 处 tile 成功且 sha256 与 <4 GiB 处同 payload 一致。
5. **wasm32 构建通过**：完整管线（含双 JPEG 编解码器）bindgen 后 373,557 B `.wasm` + 7,363 B JS 胶水；关掉 `edge-reencode` 特性后 154,849 B。浏览器行为验证属 C0-③。
6. **恶意输入 19 例负向 + 1 例正向全部有界失败**（typed error code 与 oracle 词表一致，无 panic；debug 构建 = 溢出检查开启）。

## 1. 决策：语言与 crate 选型

**语言 Rust**（方案 §3 既定）。工具链：rustc/cargo 1.98.1（用户级 rustup），`rust-toolchain.toml` 锁定并声明 `wasm32-unknown-unknown` target；wasm-bindgen-cli 0.2.129，crate `wasm-bindgen = "=0.2.129"` 精确锁版。

### 1.1 TIFF writer：`tiff` crate 评估结论 —— 不可用，手写

对 `tiff` crate（image-rs 生态，0.9+）encoder 的核查结论：

| 需求 | `tiff` crate 现状 | 结论 |
|---|---|---|
| BigTIFF 写 | 有（`TiffEncoder` + `TiffKindBig`） | 可用 |
| Tiled 写（TileWidth/TileLength/TileOffsets） | encoder 面向 strip（`ImageEncoder` strip-by-strip），无 tile API | 不可用 |
| 预编码 JPEG tile payload 直写（JPEGTables/逐 tile JPEG 原字节） | 无"把这串字节当作该 tile 的压缩段"的入口；`Compression` 枚举只覆盖它自己会压的算法 | 不可用（本项目的核心需求） |
| SubIFD | 无 | 不可用（C1 荧光 OME 需要） |
| 随机访问回填偏移 / 流式（不整文件缓冲） | encoder 面向顺序 `Write`，无 offset 回填 API | 不可用 |

→ **手写 BigTIFF writer**（`crates/core/src/bigtiff.rs`，约 300 行），逐字节复刻 oracle 的 `_BigTiffPyramidWriter` 布局（16 B 头 → 顺序 tile payload → 每层 IFD + 外部数组紧随 → 字节 8 回填首 IFD）。理由：需求组合（tiled + 原样 JPEG 字节 + LONG8 数组 + 64 位回填 + 分页落盘）没有任何维护中的 crate 覆盖；且 C0 的验收基准就是与 oracle 布局对齐，手写是最短路径。512×512 整文件 sha256 相等证明复刻正确。

### 1.2 JPEG 解码/编码：zune-jpeg + jpeg-encoder（均纯 Rust、wasm32 可编译）

- 解码 **zune-jpeg 0.4.21**（依赖 zune-core 0.4.12）：纯 Rust、活跃（image 生态在用）、`JpegDecoder::decode()` 输出 RGB。备选 `jpeg-decoder` crate 维护弱、暴露面小，弃。
- 编码 **jpeg-encoder 0.6.1**：纯 Rust（alloc 级、no_std 友好）。关键能力逐一核实（读 crate 源码）：
  - `QuantizationTableType::Custom(Box<[u16; 64]>)` 接受**自然序** u16 表，且 **`Custom` 路径绕过 quality 缩放**（`quantization.rs::get_user_table` 原值直用，内部 `<<3` 是其 DCT 定标，写回 DQT 时还原）→ 可精确复用源 tile 的 DQT 值；
  - `SamplingFactor::{F_1_1, F_2_1, F_2_2}` 对应 4:4:4/4:2:2/4:2:0，与 oracle 的 `_TIFF_SUBSAMPLING` 支持集一致；
  - `ColorType::Rgb` 输入、`encode(&[u8], w, h, ColorType)`。
- **量化表获取**：zune-jpeg 不暴露 DQT，故手写 `jpeg.rs::parse_dqt`（DQT 标记解析 + zigzag→自然序，8-bit 表；16-bit 表视为不可用走 fallback）——比从解码器内部抠表更稳，也复用同一套 SOF 探测（`scan_jpeg` 为 `kfb/parser.py::scan_jpeg` 的逐行为移植）。

### 1.3 DCT 域无损补边（jpegtran 式）可行性评估

**结论：Rust 生态无现成实现；原理可行、工程上未验证，C0 不做。** jpegtran 的 lossless crop/drop 靠操作熵编码流（MCU 行/列丢弃、restart marker 重写）；"白底扩展"还需要合成全 0/全白 DC 块的熵编码段。所需构件（huffman 解码→系数域操作→再熵编码）在 Rust 里没有维护中的库；移植 jpegtran/mozjpeg（C、IJG 条款）与纯 Rust + wasm 目标冲突；手写熵流编辑器是 C1 级别的工作量与风险。当前策略维持"解码→贴图→复用量化表重编码"，与 oracle 一致；strict 无损模式按方案 §6 拒绝边缘 tile 组合（开放项 §5.3）。

### 1.4 手写部分清单（及理由）

| 手写模块 | 理由（无合适 crate） |
|---|---|
| `kfb/synth.rs`、`kfb/vendor.rs` | 厂商格式解析，仅存在于本仓 Python oracle |
| `bigtiff.rs` | 见 §1.1 |
| `jpeg.rs`（SOF 探测 + DQT 解析） | 解码器 crate 不暴露量化表；探测逻辑须与 oracle 逐行为对齐 |
| `paged_index.rs` + `pagereader.rs` | 分页落盘 tile 索引（方案 §3"大索引分页落盘"），无通用 crate |
| `convert.rs` 中的 py-float/JSON 转义 | 与 `json.dumps(ensure_ascii=True, sort_keys=True)` 输出对齐（整文件 sha256 相等的前提） |
| `synth_gen.rs` | >4 GiB 合成输入生成器（`kfb/fixture.py` 无法扩展到该规模） |

不引入 clap/serde 等（CLI 参数手解析、统计手打印）：spike 范围内收益为负，C1 再定。

## 2. Spike 结构与内存界

- **IO 全部走 trait**：`ByteSource{size, read_at(u64, usize)}`、`RandomAccessSink{write_at, truncate, flush}`、`ScratchFactory`（+可回读 `ScratchSink`）。native 实现落 `io.rs`（File/Mem），wasm/浏览器在 C0-③ 换 OPFS/File.slice 适配器，核心零改动。
- **tile 索引分页落盘**：解析期把校验过的 tile 记录（32 B/条）按 (level,row,col) 散落写进每层一个 scratch 文件；内存只保留占用位图（≤16 层 × ≤782×782 bit ≈ 1.2 MiB 上限，由格式上限推导）与每层计数。转换期按 512 条/页（16 KiB）顺序回放。
- **IFD 偏移/计数数组不驻内存**：逐 tile 写 `(offset u64, count u32)` 12 B 记录到每层 scratch，`finish()` 时按 4096 条/页流式回放成 LONG8 数组写进 IFD 外部区。
- **payload 缓冲**：一次一个 tile ≤8 MiB。
- **实测内存界**（`/usr/bin/time -v`）：KFB-1 11.6 MB；8.9 GB 输入 4.0 MB；生成器 10.8 MB。oracle 同输入 274–280 MB 且随文件近线性（C0-① §0.4 已指出）。

## 3. 与 Python oracle 的差分结果

方法：同一输入分别经 `kfb/converter.convert_kfb` 与 `kfb2tiff convert`，`scripts/diff_oracle.py` 比对页/级结构（尺寸、tile 数、压缩、采样、photometric、ImageDescription）、逐 tile payload sha256、边缘 tile 解码像素差，并以 `--source` 把两边各自与源像素差归因。

### 3.1 合成夹具（`kfb/fixture.py`，Pillow q90 4:2:0）

| 夹具 | 页数 | 完整 tile（相等/总数） | 边缘 tile | 边缘 max diff | 整文件 sha256 |
|---|---|---|---|---|---|
| 580×300 | 3 | 2/2 | 7 | 85 | 不同（有边缘 tile，预期） |
| **512×512** | 2 | 5/5 | 0 | — | **相等** |
| 767×513 | 3 | 5/5 | 7 | 97 | 不同（同上） |
| 1024×768 | 3 | 14/14 | 3 | 85 | 不同（同上） |
| 300×580 | 3 | 2/2 | 7 | 57 | 不同（同上） |

边缘归因（767×513）：oracle-vs-源 max 61–97；spike-vs-源 max 84–139。两路径同为有损重编码；spike 在小尺寸层略差（jpeg-encoder 的 FDCT/Huffman 与 libjpeg 不同），已登记开放项。

### 3.2 真实样本 KFB-1（明场 `kfb_kfbio_jpeg` 变体）

| 指标 | 结果 |
|---|---|
| 层级/几何 | 9 层 34013×46152 → … → 132×180，与 oracle 逐页一致（含 tile 数 24073/6097/1564/391/108/30/9/4/1） |
| 完整 tile | **31,650 / 31,650 sha256 相等** |
| 边缘 tile | 627 个；max abs diff 32；>2 共 366；>8 共 75 |
| 关联图 | overview/label/thumbnail 3/3 字节相等 |
| warning | `warnings=[]`（全部边缘重编码成功复用源量化表，与 C0-① manifest 观测一致，无 q95 回落） |
| Bio-Formats 8.5.0 `showinf -nopix` | spike 与 oracle 输出的 OME `<Pixels>` 块**完全相同**（PhysicalSizeX/Y=0.48410487159960625 µm 保留） |
| tifffile 2024.5.22 | 全部页可开；每层首/中/末 tile 解码 256×256；末页整层 `asarray` 通过其自身 JPEG 管线 |

### 3.3 运行时间 / RSS（同机同盘，`/usr/bin/time -v`）

| 输入 | 实现 | wall | 峰值 RSS |
|---|---|---|---|
| KFB-1 221 MB | Python oracle | 4.88 s | 280,260 kB |
| KFB-1 221 MB | kfb2tiff（release） | **1.62 s** | **11,560 kB** |
| 合成 8.9 GB（gen-synth 生成） | 生成器 | 28.56 s | 10,808 kB |
| 合成 8.9 GB → 8.29 GiB 输出 | kfb2tiff | 44.41 s | **4,012 kB** |

## 4. >4 GiB 证明（64 位偏移）

- 输入：`gen-synth --width 69376 --height 69376 --quality 92 --seed 424242` → 8,905,089,609 B（10 层，98,126 tile；v1 合同，无隐私数据）。
- 输出：8,902,313,525 B = 8.29 GiB；level0 73,441 tile 中 **26,194 个 TileOffset ≥ 4 GiB**（最大 6.22 GiB）；**10 个 IFD 全部位于 ≥8.289 GiB**（首 IFD 偏移回填在字节 8，u64）。
- tifffile 验证：随机取 3 个 >4 GiB tile 解码成功（256×256，sha256 与 <4 GiB 的 tile0 一致——生成器同层复用同 payload，构成内容校验）；末页 (135×135×3) 经 tifffile JPEG 管线整层解码成功。证据：`big/beyond-4gib-proof.txt`。
- 结论：u64 偏移端到端（writer 回填 → 独立 reader）无 32 位截断。

## 5. 开放项与已知分歧

1. **边缘 tile 字节级 parity 不可达**（换编码器的必然结果）：已按任务预案降级为"完整 tile 直拷 + 边缘像素差测量"。C1 若要求更强，需评估 (a) DCT 域无损补边（§1.3），或 (b) 严格无损模式下拒绝边缘 tile。spike 在小尺寸层的边缘质量略逊 oracle（139 vs 97 max，合成噪声图最坏情形），C1 可考虑对 jpeg-encoder 做 FDCT/色度上采样质量调优或换码路径复评。
2. **q95 回落路径的表不同**：源量化表不可解析时，oracle 用 Pillow quality=95（Annex K 基表缩放），spike 用 jpeg-encoder `Default`（mozjpeg 基表）q95。warning 码一致（`edge_reencode_fallback_q95`）。真实样本未触发该路径。
3. **浮点/JSON 序列化边界**：`py_repr_f64` 与 CPython repr 在 [1e-4, 1e16) 外的指数记法不同；`round()` 半值舍入（银行家 vs 远零）仅理论差异。MPP/objective 物理量域内两者一致（512×512 整文件相等已覆盖）。
4. **oracle 的输出自检（tifffile 重开）未在 spike CLI 内复刻**：C0 以外部差分/独立 reader 验证代替；C1 决定是否内建（浏览器端需另行设计轻量结构校验）。
5. **SubIFD 未实现**：荧光 OME（converter_fl）需要；手写 writer 需扩展（open item，C1）。
6. **wasm 浏览器行为未测**（属 C0-③）：本阶段只证明 wasm32-unknown-unknown 构建通过 + 体积；`convert_mem` 是内存版冒烟入口，真实浏览器走 File.slice/OPFS 适配器。
7. **vendor 布局的 `round(log2(objective/scale))`**：Python banker's rounding 与 Rust `f64::round` 仅在精确半值处分歧，实际样本（20/10/5/2.5/1.25…）无影响。
8. **JPEGTables（共享表）路径未实现**：oracle 与 spike 均为逐 tile 独立 JPEG（无 JPEGTables），当前无需求；引入可减输出体积，C1 评估。

## 6. 依赖与许可证清单（Cargo.lock 锁定）

| crate | 版本 | 许可证 | 用途 |
|---|---|---|---|
| zune-jpeg | 0.4.21 | MIT OR Apache-2.0 OR Zlib | JPEG 解码（边缘 tile） |
| zune-core | 0.4.12 | MIT OR Apache-2.0 OR Zlib | zune-jpeg 依赖 |
| jpeg-encoder | 0.6.1 | **(MIT OR Apache-2.0) AND IJG** | JPEG 编码（边缘 tile；**IJG 条款需 C1 法务复核**） |
| wasm-bindgen（family） | 0.2.129 | MIT OR Apache-2.0 | wasm 绑定（含 macro/macro-support/shared） |
| bumpalo | 3.20.3 | MIT OR Apache-2.0 | wasm-bindgen 传递 |
| once_cell | 1.21.4 | MIT OR Apache-2.0 | 传递 |
| cfg-if | 1.0.5 | MIT OR Apache-2.0 | 传递 |
| proc-macro2 / quote / syn / unicode-ident / rustversion | 1.0.107 / 1.0.47 / 3.0.6 / 1.0.26 / 1.0.23 | MIT OR Apache-2.0 | 仅构建期（宏） |

运行时依赖仅 2 个直接 crate（zune-jpeg、jpeg-encoder，均可 feature-gate 关闭）。工具链：rustc/cargo 1.98.1、wasm-bindgen-cli 0.2.129（用户级安装）。

## 7. wasm32 构建与体积

`cargo build --release --target wasm32-unknown-unknown -p slide-transform-wasm-spike`（默认特性，含编解码器，经 `convert_mem` 全链路链接）：

- 原始 `.wasm` 408,326 B；`wasm-bindgen --target web` 后 373,557 B `.wasm` + 7,363 B JS 胶水 + d.ts。
- `--no-default-features`（关 edge-reencode）：154,849 B。
- 注：wasm-opt 未启用（未安装）；C1 产物入库前应加 `wasm-opt -O3/-Oz` 与 size-strip 复测。体积对 §4 预算（节省档 192 MiB 管理内存）无压力。

## 8. 恶意输入测试（`crates/core/tests/malformed.rs`，20 例全过）

截断文件、坏 magic、未知 version、index_offset 越出 EOF、tile_count=0xFFFFFFF0（超上限）、header_bytes=5000、flags 未定义位、非明场、payload_offset=u64::MAX（**checked 加法，无回绕 panic**）、payload 越出 EOF、坐标未对齐网格、reserved≠0、索引 jpeg 尺寸非法、网格 cell 重复、边缘 tile JPEG 中段损坏（SOI/EOI 完好 → 解码期 `jpeg_decode_failed`）、中间层缺 tile、vendor 索引越出文件、vendor 记录 magic 非法、associated 名非法——均以 `ErrorCode`（与 `kfb/errors.py` 词表一致）失败；正例 roundtrip 验证 BigTIFF 头/IFD 回填。debug 构建（算术溢出即 panic）下运行。

## 9. 复跑

见 `experiments/slide-tools-c0/core-spike/README.md`（构建/测试/差分/大文件/阅读器/wasm 全命令）与 `.gate-tmp/slide-tools-c0/core/RERUN.md`（原始证据对照表）。
