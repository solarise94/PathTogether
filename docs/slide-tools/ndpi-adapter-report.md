# F6 — Hamamatsu NDPI Input Adapter Report

Phase F6（先转换后上传 / 格式扩展）：浏览器切片转换工具新增 Hamamatsu
NDPI（经典 TIFF + 厂商标签、整层单条带 JPEG、>4 GiB 非标准偏移）输入
转换器，按 MRXS 的「拼接后重编码 + l0-box2 降采样」模式实现——NDPI 的层
是**整层单条带 JPEG**，没有源 tile 可搬运，因此输出瓦片全部是「restart
区间有界分段解码 → 拼成 256 瓦片 → 重编码」的产物。分支
`upload-convert-first`，worktree `.gate-tmp/ucf-wt`。
**Status: implemented；本文所有实测数据均出自本 worktree 的运行记录**
（命令与输出见各节）。

Code:

- `slide-transform-core/crates/core/src/ndpi.rs` — NDPI 探测适配器
  （`hamamatsu-ndpi-jpeg`，adapter version `1`）：Make（271）厂商识别、
  SourceLens（65421）层级/关联图分类、>4 GiB 非标准偏移（每条目 4 字节
  扩展字 + 64 位首 IFD 偏移回退）、整层单条带契约、restart marker 段几何
- `slide-transform-core/crates/core/src/convert_ndpi.rs` — 分段解码转换：
  前向 RST 扫描、去 DRI 子 JPEG（SOF 尺寸改写）、MCU 索引拼接、行带
  （band）缓冲、跨行带段携带（carry）、q96 YCbCr 4:2:2 保留画质重编码、
  l0-box2 生成尾、checkpoint/resume、内存预算
- `slide-transform-core/crates/core/src/ndpi_fixture.rs` — 合成 NDPI 夹具
  生成器（纯代码，`fixtures` feature；行对齐 restart 段拼装整层条带、
  macro/focusmap 页、扩展字区、变体旋钮）
- `slide-transform-core/crates/core/tests/ndpi.rs` — 18 项 Rust 集成测试
- `slide-transform-core/crates/core/src/scn.rs` — 厂商嗅探加
  `HamamatsuNdpi`（描述未命中时按 Make 兜底）；`gtiff.rs` 守卫同步
- `slide-transform-core/crates/cli/src/main.rs` — `probe`/`convert` 厂商
  分派路由 + `gen-ndpi` 合成夹具子命令
- `slide-transform-core/crates/wasm/src/lib.rs` — wasm probe/convert/resume
  路由；`InputKind::Ndpi` 携带 adapter id/版本进 journal 与 checkpoint
- `static/tools/slide-transform/engine.js` — TIFF 嗅探按厂商分派（NDPI 走
  条带门槛 + BigTIFF 拒绝）；`NDPI_SOURCE_ADAPTER`/`NDPI_ADAPTER_VERSION`
- `static/tools/tools-slides.js`、`templates/tools_slides.html`、
  `static/i18n.js` — 工具页 accept/文案/格式族名/画质说明行（中英）
- `static/upload/slide-sniff.js` — 工作台直传分流：.ndpi → 需要转换
- `slide_format_registry.py` — 目录行级 `browser_convert=available`
- 测试：`tests/test_slide_transform_ndpi.py`（pytest）、
  `tests/js/tools-ndpi-input.test.ts`、`tests/js/slide-sniff.test.ts`、
  `tests/js/tools-u2-page.test.ts`、
  `tests/browser/slide_tools_c2/run_parity.js`（复用既有 `--input`）、
  `tests/browser/slide_tools_c2/run_faults.js`（两个 NDPI 场景）、
  `tests/browser/slide_tools_c3/run_e2e.js`（`nd-ndpi-page-e2e`）

## 0. 结论（Verdict）

| 要求 | 状态 |
|---|---|
| 有界读取（绝不整文件进内存） | **PASS** — 条带只按段读取（SCAN_CHUNK 64 KiB 扫描 + 单段读取），全部经 `ByteSource::read_at`；`tests/browser/slide_tools_c2/test_no_whole_file.js` PASS（本次运行） |
| 整层单条带 JPEG 按 restart 区间有界分段解码 | **PASS** — 去掉 DRI 的子 JPEG（SOF 尺寸改写为段自身网格）+ EOI，DC 预测器每段复位故逐段独立可解码；合成夹具像素门证明分段解码 == 整层条带解码（Rust `l0_tiles_match_whole_strip_decode`）；真实样本 L0 三组织 ROI 均值误差 0.040–0.055（max 5） |
| 拼成瓦片后重编码，沿用 MRXS 保留画质参数（q96 YCbCr 4:2:2 + compact） | **PASS** — 256 瓦片按 `ndpi-segment-compose:q96:y422:hstd:v1` 指纹重编码（与 MRXS compose/gtiff 生成层同参数族）；compact 用锁定 U3 参数（q80 4:2:0）；报告 `composed` 摘要 + `lossy_reencode`（仅 compact） |
| 降采样层用 l0-box2 | **PASS** — 输出金字塔 = 重编码 L0 + `generated_tail`（÷2 box 链，末层 ≤ 256；真实样本 9 层至 200×149）；`pyramid=l0-box2` 进报告/描述 JSON/OME-XML |
| 排除 macro/focus map | **PASS** — SourceLens −1 → macro、−2 → focusmap、其他 ≤0 → associated、z-stack（65424 ≠ 0）→ focalplane：全部检测并排除，真实样本 macro（1191×408）被排除 |
| 内存在分配前按预算拒绝 | **PASS** — 行带缓冲、瓦片画布、单段解码峰值（读取 + 解码器临时 + RGB，6 B/px）全部 `charge` 在分配前，超限 → 稳定码 `resource_profile_insufficient`（pytest：64 KiB 预算拒绝且无产物，192 MiB 默认成功） |
| JPEG2000 等变体类型化拒绝 | **PASS** — 压缩 33003/33005 → `unsupported_kfb_variant`（"JPEG 2000"）；渐进/算术 → `jpeg_decode_failed`（"SOF"）；无 restart marker（DRI=0，仅对被解码的 L0 强制——真实样本最高缩减少层无 DRI）→ `unsupported_kfb_variant`；非 Hamamatsu Make → 通用 TIFF 适配器的条带变体拒绝；ExtraSamples/多通道/平面存储/BigTIFF 容器 → 类型化拒绝；全部发生在写出之前（pytest 断言产物不存在） |
| source_format + `ADAPTER_VERSION="1"`；续跑 journal 携带转换器 id/版本 | **PASS** — 报告/provenance/journal/checkpoint 携带 `hamamatsu-ndpi-jpeg`/`1`；换版本（或字段缺失）的 checkpoint 在核心入口拒绝 |
| 换转换器或源文件被改拒绝续跑 | **PASS** — `run_faults.js --only ndpi`：`ndpi-adapter-change-refused`（resume_refused / source-adapter）与 `ndpi-source-changed-refused`（source_changed_refuse_resume）两行 PASS；诚实续跑 == 原生字节（Rust resume 测试逐字节一致） |
| CLI 路由 + `gen-ndpi` 子命令 | **PASS** — probe/convert 按厂商分派；`gen-ndpi` 无样本单测夹具 |
| wasm probe/convert/resume 路由；engine.js 嗅探按厂商分派；OME-TIFF 与转换器 BigTIFF 不是转换输入 | **PASS** — 嗅探先 OME/转换器标记、再厂商（Aperio/Leica/Hamamatsu-Make/未知），OME-TIFF 与转换器 BigTIFF 在 staging 前类型化拒绝（行为与词表不变，本次未改其语义） |
| 工具页 accept、格式名称、i18n 中英 | **PASS** — accept 加 `.ndpi`；`NDPI (Hamamatsu)` 格式族名；NDPI 画质说明行（`tools.quality.ndpi.note` 中英）；`nd-ndpi-page-e2e` 识别断言 |
| slide-sniff 从「暂时直传」改「需要转换」；注册表 `browser_convert=available`；`direct_import` 保持 open | **PASS** — Make 标识 Hamamatsu + 整层单条带 JPEG 明场 → convert（JP2K/多通道变体仍 temporary）；注册表 ndpi 行改 available/convert；直传是否关闭由用户看本报告后决定（未动） |
| 原生 CLI 在默认 192 MiB 预算下转换真实样本成功（MemoryMax=320M 实跑） | **PASS** — `systemd-run -p MemoryMax=320M` + 默认预算转换成功（release CLI 1 m27 s，输出 502,582,927 B，39,857 瓦片；pytest 内复跑 3 m05 s 含两组转换与像素比对） |
| 浏览器 == 原生逐字节一致（保留画质与 compact 各一次） | **PASS** — 合成夹具 bf-ome/bf-classic × preserve/compact 全部 equal=true（本次运行）；真实样本见 §5 |

## 1. 设计

### 1.1 文件模型（`ndpi.rs`）

真实布局对照公开 CC0 样本（CMU-1.ndpi，198,030,965 B）逐字节确认：

```text
header（classic II*\0；首 IFD 偏移按 64 位写，≤4 GiB 文件高 32 位为 0）
16 B   … 条带载荷（每层一条整层 JPEG；层内 restart 标记分段）
…      … macro / focus-map 载荷
文件尾 IFD 链：每 IFD = count + 条目（12 B）+ next(4 B) + 4 B 保留
       + 每条目 4 B 值扩展字（NDPI 模式）+ 外联值
```

- **厂商识别**：IFD 0 的 `Make`（271）= Hamamatsu（该样本 IFD 0 没有
  ImageDescription，描述嗅探未命中时按 Make 兜底，与 OpenSlide/tifffile
  一致）；`TiffVendor::HamamatsuNdpi` 进 CLI/wasm 路由。
- **NDPI 模式门**：IFD 0 存在 65420（NDPI_FORMAT_FLAG）才信任每条目扩展
  字（OpenSlide tifflike 的同一判定）；≤4 GiB 文件扩展字全 0（实测样本
  104 B 全 0）。
- **>4 GiB 非标准偏移**：条目值 = 低 32 位（条目值域）| 扩展字 << 32
  （仅内联 LONG 生效）；首 IFD 偏移非法时回退读头部 64 位字段。
- **层级/关联图分类**：`SourceLens`（65421，实测 FLOAT）> 0 → 层级
  （20/5/1.25/0.3125…即物镜倍率），−1 → macro，−2 → focusmap，其余 ≤0 →
  associated；`focal plane`（65424，实测 SLONG）≠ 0 → z-stack 页排除。
  层级序列须严格递减、步长 1.5–8×（实测 4×）。
- **层契约**：整层单条带（RowsPerStrip = 层高、StripOffsets/ByteCounts
  各 1 条）、基线 JPEG（SOF0/1）、3 分量 8 位、chunky、photo 2/6、
  TIFF 尺寸 == SOF 尺寸。**restart marker 仅对被解码的 L0 强制**——实测
  样本缩减少层 DRI 逐层缩小（64、16）且最高层无 DRI，而转换器从不解码
  它们（降采样层是 l0-box2）。
- **MPP**：厂商标签 65441/65442 在场才填（DOUBLE/FLOAT）；实测 2009 年
  样本没有这两个标签 → MPP 保持未知，不从物镜倍率猜测。objective 取
  L0 的 SourceLens（实测 20.0）。

### 1.2 分段解码（`convert_ndpi.rs`）

层条带的 DRI 给出唯一的有界解码单元：restart 段内 DC 预测器复位、熵编
码字节对齐，因此每段可独立解码。转换器维护一个前向段游标：

1. **段头**：探测期把条带头 `[SOI…SOS]` 存下（≤ 256 KiB 上限），去掉
   DRI 段（解码器 restart_interval=0 → 不做 RST 校验）；
2. **段界扫描**：从上一段终点按 64 KiB 有界分块扫描下一个 `FF D0–D7`/
   `FF D9` 标记，校验 RST 序号（段 k 以 RST(k mod 8) 结束、末段以 EOI
   结束——与 OpenSlide 的 Hamamatsu restart 校验同族）；
3. **子 JPEG**：段头（SOF 尺寸改写为段自身网格：`grid_w = min(段 MCU
   数, 每行 MCU 数, 256)`）+ 段熵数据 + EOI，交给核心 JPEG 解码器；
4. **拼接**：段内第 i 个 MCU 贴到全局 MCU 序号 `k·R + i` 的网格位置
   （索引式粘贴，天然兼容不整除行宽的段）——行带缓冲为
   `⌈256/mcu_h⌉` MCU 行 × padded 宽 × 3 B，跨行带的段解码一次携带
   （carry）到下一行带，每段只解码一次；
5. **重编码**：每行带的 256 瓦片从行带裁剪（层外白填充）、按当前编码
   profile 重编码、顺序写 tile 游标；每行带发 progress + checkpoint。

内存（review §1 同款）：行带缓冲 + 瓦片画布 + 单段解码峰值（段读取 +
解码器临时 + RGB，6 B/px）全部在分配前 `charge`，段解码后 `release`；
超预算 → `resource_profile_insufficient`。实测 192 MiB 预算下
51200 px 宽的行带（37.5 MiB）+ 段峰值（实测 DRI=256、4:4:4 → 段
2048×8 px ≈ 0.1 MiB）余量充足。

### 1.3 输出

- 保留画质（`preserve-source-v1`）：每个输出瓦片按
  **YCbCr 4:2:2 · q96 · 标准 Annex-K Huffman** 重编码，指纹
  `ndpi-segment-compose:q96:y422:hstd:v1`，`composed.mode =
  segment-compose-reencode`。本格式没有逐字节搬运路径，报告如实声明。
- 更小文件（`compact-jpeg-v1`）：同一路径、锁定 U3 参数（q80 4:2:0）。
- 降采样层：`l0-box2`（`gtiff::generated_tail`：÷2 box 链，末层两边
  ≤ 256；与前一层已提交瓦片经 `tile_record`/`read_output_at` 回读构成，
  与 MRXS v2/通用 TIFF 适配器同方法）。strict-lossless 与本适配器互斥
  （类型化 `pixel_policy_violation`）。

## 2. 支持范围

| 输入 | 判定 |
|---|---|
| Hamamatsu NDPI（Make 标识、经典 TIFF、整层单条带基线 JPEG 明场、L0 带 restart marker） | **转换**（本适配器） |
| NDPI 的 JPEG 2000 变体（压缩 33003/33005） | 类型化拒绝（复制前；slide-sniff 归 temporary 暂时直传） |
| NDPI 的渐进/算术编码层 | 类型化拒绝（SOF/算术码） |
| L0 无 restart marker（DRI=0） | 类型化拒绝（无有界解码单元） |
| 非 Hamamatsu 的 Make | 不路由进本适配器（按既有路由：通用 TIFF 适配器给条带变体拒绝） |
| 荧光/多通道/ExtraSamples、平面存储、BigTIFF 容器 | 类型化拒绝 |
| macro/focus map/z-stack 页 | 检测并排除（不导出；报告 `associated`） |

## 3. 实测（本 worktree 运行记录）

### 3.1 真实样本（CMU-1.ndpi，198,030,965 B，公开 CC0 样本）

- **probe**：51200×38144、objective 20、MPP 无（标签缺席）、L0
  DRI=256、119,200 段、macro 被排除、输出金字塔 9 层至 200×149。
- **转换（保留画质）**：`systemd-run --user --scope -p MemoryMax=320M`
  + release CLI、默认 192 MiB 预算：**成功**，1 m27 s，
  输出 502,582,927 B（≈2.5×源，q96 4:2:2 对 4:4:4 源的预期膨胀），
  39,857 瓦片（L0 29,800 + 生成层），`validate` 通过。
- **像素门**（pytest `test_real_sample_l0_mean_error_and_pyramid_geometry`，
  slide_io/openslide 读产物 vs OpenSlide 读原文件）：
  - L0 三组织 ROI 均值绝对误差 **0.040 / 0.055 / 0.044**（max ≤ 5），
    逐通道均值差 < 4；
  - 低倍层几何 = probe 声明的 l0-box2 链逐层一致、末层 ≤ 256；
  - 输出 box2 层 vs 扫描仪自己的缩减少层（同尺寸）：均值误差 1.297
    （重采样核不同，组织内容一致）。

### 3.2 合成夹具（gen-ndpi，无样本单测）

- OpenSlide 识别 `vendor=hamamatsu` 并打开；tifffile 可读（NDPI 布局
  三要素：64 位首 IFD 偏移、65420 格式旗标、+8 扩展字区）。
- Rust 18 项（probe 契约、双 profile、分段解码 == 整层解码像素门、
  跨行带 carry 路径、变体拒绝 ×6、扩展字加宽、预算拒绝、续跑逐字节
  一致 + 版本钉扎）全绿：`cargo test -p slide-transform-core --features
  fixtures --test ndpi` → 18 passed。
- pytest 7 项合成 + 2 项真实样本全绿（真实样本组 3 m05 s）。
- 浏览器 parity（`run_parity.js --input <合成夹具>`）：bf-ome/bf-classic
  × preserve/compact 四组 `equal=true`。

## 4. 未覆盖项 / 已知限制（如实声明）

1. **>4 GiB 真实布局无端到端样本**：扩展字加宽、64 位首 IFD 偏移的
   组合算术有 Rust 单元测试（`value_extensions_widen_inline_longs`、
   头部回退逻辑），但 CI 无法用 <4 GiB 合成夹具构造真 >4 GiB 文件；
   布局按 OpenSlide tifflike（扩展字区位置）与 tifffile NDPI_LE（头部
   8 字节偏移）的读取模型实现，并与公开 198 MB 样本的实际字节核对
   （扩展字区位置在实测文件上为全零区，两种候选位置不可区分——已按
   OpenSlide 源码的确切 `fseek(12·n+8, SEEK_CUR)` 定位）。
2. **跨行宽的「非对齐」restart 段**（DRI 不整除每行 MCU 数）在粘贴上
   是索引式的（`g = k·R + i`），合成夹具的段都是行对齐矩形（行对齐
   编码器的构造限制），真实样本 L0（DRI=256，6400/256=25）恰好整除。
   该路径与对齐路径共享同一粘贴代码，但有真实样本的整除性巧合——
   一个 DRI 不整除的真实样本未覆盖。OpenSlide 对 NDPI 的 JPEG 校验
   只要求 DRI ≤ 每行 MCU 数，非整除文件理论上存在。
3. **MPP 缺失保持未知**：实测样本无厂商 MPP 标签，输出的
   PhysicalSize 为空、`mpp_source=unknown`；不从物镜倍率或 XResolution
   猜测（tifffile 的 NDPI 单位约定存在版本分歧，不猜）。
4. **缩减少层的 DRI 差异**：真实样本缩减少层 DRI 逐层不同（64、16）
   且最高层无 DRI——适配器不解码这些层（l0-box2），探针也只对 L0
   强制 restart；若未来需要「用源缩减少层像素」的省时模式，需要单独
   的分段契约（当前不做）。
5. **性能**：2 GPx 整层条带在纯 Rust/wasm 解码器下原生 ~1.5 分钟
   （release）。debug 构建超过默认 600 s 墙钟，真实样本的 pytest/
   e2e 都显式 `--timeout 3600`（浏览器 wasm 路径无墙钟超时，不受影响）。

## 5. 浏览器逐字节一致

- 合成夹具：`node tests/browser/slide_tools_c2/run_parity.js --input
  <gen-ndpi 夹具>` 与 `--compact`（端口 8949/8950）→ bf-ome/bf-classic
  × preserve/compact **全部 equal=true**（本次运行记录）。
- 真实样本（本次运行记录）：
  - 保留画质：`run_parity.js --input <CMU-1.ndpi> --label ndpi-real`
    → bf-ome `6afd6cff…` / bf-classic `41753084…` 均 **equal=true**
    （浏览器内转换 ~113 s/次；bf-classic 哈希与 §3.1 的 320M 原生转换
    完全一致）；
  - compact：`--compact --label ndpi-real-compact` → bf-ome
    `d300012b…` / bf-classic `9124b616…` 均 **equal=true**（~92 s/次）。
- C2 faults：`run_faults.js --only ndpi` → `ndpi-adapter-change-refused`
  （PASS，2.9 s）与 `ndpi-source-changed-refused`（PASS，2.8 s）。
- C3 页面场景：`run_e2e.js --only nd` → **PASS** `nd-ndpi-page-e2e`
  （385 s；识别 `NDPI (Hamamatsu)` → NDPI 画质说明行显示 → 保留画质
  保存 sha `6afd6cff…` == 原生、compact 保存 sha `d300012b…` == 原生
  → 无 restart marker 变体在复制前被拒、无任务目录）。
