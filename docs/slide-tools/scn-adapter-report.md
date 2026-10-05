# F4 — Leica SCN Input Adapter Report

Phase F4（先转换后上传 / 格式扩展）：浏览器切片转换工具新增 Leica SCN
（BigTIFF + SCN XML 描述，JPEG tile）输入转换器，按 SVS 的 tile 原样搬运
模式实现。分支 `upload-convert-first`，worktree `.gate-tmp/ucf-wt`。
**Status: implemented；本文所有实测数据均出自本 worktree 的运行记录**
（命令与输出见各节）。

Code:

- `slide-transform-core/crates/core/src/tiff_read.rs` — 有界 TIFF/BigTIFF
  读取器；本次 `MAX_IFDS` 64 → 256（多 ROI SCN 全部层挂在一条主链上，
  可超 64，仍有界 + 指针环检测）；`TileCursor::next_pair_allow_zero`
  支持 SCN 稀疏网格的 `(0,0)` 缺失 tile 语义
- `slide-transform-core/crates/core/src/scn.rs` — SCN 探测适配器
  （`leica-scn-jpeg`，adapter version `1`）+ TIFF 厂商嗅探
  `sniff_tiff_vendor`/`classify_description`（Aperio / Leica SCN /
  OME-TIFF / 转换器 BigTIFF / 未知）
- `slide-transform-core/crates/core/src/convert_scn.rs` — 双明场输出
  profile 转换（tile 搬运 + 稀疏填充 + resume + 内存预算）
- `slide-transform-core/crates/core/src/scn_fixture.rs` — 合成 SCN 夹具
  生成器（纯代码，`fixtures` feature；`--sparse/--fluoro/--non-jpeg/
  --desc` 旋钮）
- `slide-transform-core/crates/core/tests/scn.rs` — 12 项 Rust 集成测试
- `slide-transform-core/crates/cli/src/main.rs` — `probe`/`convert` 厂商
  分派路由 + `gen-scn` 合成夹具子命令
- `slide-transform-core/crates/wasm/src/lib.rs` — wasm probe/convert/resume
  路由；`InputKind::Scn` 携带 adapter id/版本进 journal 与 checkpoint
- `static/tools/slide-transform/engine.js` — TIFF 嗅探按厂商分派；
  `SCN_SOURCE_ADAPTER`/`SCN_ADAPTER_VERSION`
- `static/tools/slide-transform/runner.js` — resume 的 SCN 适配器版本钉扎
- `static/tools/tools-slides.js`、`templates/tools_slides.html`、
  `static/i18n.js` — 工具页 accept/文案/格式族名（中英）
- `static/upload/slide-sniff.js` — 工作台直传分流：.scn → 需要转换
- `slide_format_registry.py` / `upload_direct_class.py` — 目录行级与
  服务端直传类词表
- 测试：`tests/test_slide_transform_scn.py`（pytest）、
  `tests/js/tools-scn-input.test.ts`、`tests/js/slide-sniff.test.ts`、
  `tests/browser/slide_tools_c2/run_parity.js`（`--input`/`--bundle`）、
  `tests/browser/slide_tools_c2/run_faults.js`（两个 SCN 场景）、
  `tests/browser/slide_tools_c3/run_e2e.js`（`sc-scn-page-e2e`）

## 0. 结论（Verdict）

| 要求 | 状态 |
|---|---|
| 有界读取（绝不整文件进内存） | **PASS** — 全部经 `ByteSource::read_at`；`tests/browser/slide_tools_c2/test_no_whole_file.js` PASS（本次运行） |
| 多 collection/image：选出明场主金字塔，排除 macro/label/preview | **PASS** — 主图 = `<pixels>` 面积最大者；其余记为 associated（label/macro）不导出；真实样本 probe 主图 36832×38432，label 1616×4668 被排除 |
| 瓦片 JPEG 按 SVS 方式原样搬运 | **PASS** — Rust 逐 cell 字节相等测试 + 真实样本 OpenSlide L0 三组织 ROI 逐像素零误差 |
| 空缺 tile 用填充块 | **PASS** — `(0,0)` 条目 → 每几何一份共享白色填充 tile（header 后固定偏移），`tiles_filled` 计数 + `scn_missing_tiles_filled` 告警；resume 逐字节一致 |
| 荧光/非 JPEG 编码在复制前类型化拒绝；前端归「暂时直传」变体 | **PASS** — `unsupported_kfb_variant`（拒绝发生在任何输出写出之前，pytest 断言产物不存在）；slide-sniff 将其归 temporary（legacy-direct） |
| 必要时放宽 tiff_read 的 IFD 上限并保持有界 | **PASS** — `MAX_IFDS` 64→256，保持条目数/环检测/边界检查 |
| source_format + `ADAPTER_VERSION="1"`；内存预算分配前收费 | **PASS** — 报告/provenance/journal 携带 `leica-scn-jpeg`/`1`；`MemBudget` 在 IFD 链/XML/夹具工作集分配前 `charge`，超限 → 稳定码 `resource_profile_insufficient`（pytest：64 KiB 预算拒绝，192 MiB 默认成功） |
| 续跑 journal 携带转换器 id/版本；换转换器或源文件被改拒绝续跑 | **PASS** — wasm checkpoint 携带 adapter/adapter_version（并修复了此前所有适配器都记 MRXS 版本的问题）；runner 钉扎；run_faults 两场景 PASS |
| CLI 路由 + `gen-scn` 子命令 | **PASS** — probe/convert 按厂商分派；`gen-scn` 无样本单测夹具 |
| wasm probe/convert/resume 路由；engine.js 嗅探分派；OME-TIFF 与转换器 BigTIFF 不是转换输入 | **PASS** — 两类容器在 staging 前类型化拒绝（engine.js 与 Rust 同一判定词表） |
| 工具页 accept、格式名称、i18n 中英 | **PASS** — accept 加 `.scn`；SCN (Leica) 格式族名；`sc-scn-page-e2e` 识别断言 |
| slide-sniff 从「暂时直传」改「需要转换」；注册表 `browser_convert=available`；`direct_import` 保持 open | **PASS** — JPEG 明场 SCN → convert；注册表 scn 行改 available/convert，直传是否关闭由用户看本报告后决定（未动） |
| 原生 CLI 在默认 192 MiB 预算下转换真实样本成功（MemoryMax=320M 实跑） | **PASS** — `systemd-run -p MemoryMax=320M` 下转换成功（pytest 内） |
| 浏览器 == 原生逐字节一致（保留画质与 compact 各一次） | **PASS** — 真实样本 + 合成稀疏夹具，bf-ome/bf-classic × preserve/compact 全部 equal=true |

## 1. 设计

### 1.1 文件布局与主图选择（`scn.rs`）

2010/10/01 schema 的 SCN 文件是一条 BigTIFF 主链：IFD 0 是 label/preview
扫描（其 ImageDescription 是 SCN XML），其后是 label 的缩减层和主图金字塔
的每一层。XML 形如：

```xml
<scn xmlns="http://www.leica-microsystems.com/scn/2010/10/01">
  <collection …>
    <barcode>…</barcode>
    <image …>
      <pixels sizeX="1616" sizeY="4668">
        <dimension sizeX="1616" sizeY="4668" r="0" ifd="0" /> …
      </pixels>
      <view sizeX="26564529" sizeY="76734666" offsetX="0" … />
      <scanSettings>
        <objectiveSettings><objective>0.60833</objective></objectiveSettings>
        <illuminationSettings>…<illuminationSource>brightfield</illuminationSource>…</illuminationSettings>
      </scanSettings>
    </image>
    <image …>（主图，5 层，ifd 3..7）</image>
  </collection>
</scn>
```

探测规则（有界；XML ≤ 4 MiB 读取上限，解析纯手写、零依赖）：

1. IFD 0 描述必须是 `<scn>` 且命名空间含 `leica-microsystems.com/scn`；
2. 主图 = 带 `<pixels>/<dimension>` 的 image 中面积最大者（与 OpenSlide
   的选择一致）；其余 image 记为 associated（第一个非主图命名 `label`，
   其余 `macro`），不导出（`scn_associated_not_exported` 告警）；
3. 主图 `<illuminationSource>` 存在且非 `brightfield` → **复制前**
   `unsupported_kfb_variant`（荧光 SCN；Leica-Fluorescence-1 类文件）；
4. 层 = `<dimension r ifd>`，r 连续、严格递减、层间 1.5–16×；每层 IFD 的
   256/257 必须与 XML `sizeX/sizeY` 完全一致；
5. 每层 IFD：tiled、压缩 7（33003/33005 → JPEG 2000 拒绝；其他 → 非 JPEG
   拒绝）、chunky、3×8 bit、photometric 2/6；JPEG 真彩空间从 SOF/JFIF/
   Adobe 判定（与 SVS 同一规则）；可选 tag 347 原样带入输出；
6. MPP = `<view sizeX/sizeY>`（纳米）÷ `<pixels sizeX>` ÷ 1000；
   objective 取 `<objective>`；都缺失则保持 unknown（不编造）。

### 1.2 稀疏网格与填充块（`convert_scn.rs`）

SCN400 允许 tile 网格有空缺：`TileOffsets` 数恒等于网格大小，缺失 tile
表达为 `(0,0)` 条目。转换器把每个「实际有缺失的 tile 几何」的白色填充块
（q90 4:4:4，自包含 JPEG）写在 16 字节 BigTIFF 头之后的固定偏移、每几何
只写一份，所有填充 cell 的 TileOffsets 条目引用它（MRXS 同款 scheme）——
fresh 与 resume 两条路径引用相同偏移，**续跑输出逐字节一致**。没有缺失
的文件不写任何填充字节。填充计入 `tiles_filled` 并告警一次
（`scn_missing_tiles_filled`）；半零条目（`(0,c)/(o,0)`）按损坏类型化拒绝。

### 1.3 内存预算（分配前收费）

`probe_scn_with_budget(src, budget_bytes)` 在任何大分配之前向
`MemBudget`（reserve 24 MiB）收费：IFD 链结构预留
（`MAX_IFDS × MAX_IFD_ENTRIES × 32 B ≈ 4 MiB`）、XML 描述实际长度、
image 记录（96 B/个）、层记录；转换期再收填充画布
（`max(tile_w×tile_h×3)×2`）。超限是稳定码
`resource_profile_insufficient`，发生在分配前。CLI `--memory-budget`
（默认 192 MiB saver 档）与 wasm 的资源档位共用该路径。

### 1.4 路由与「不是转换输入」

`sniff_tiff_vendor`（Rust，有界读 IFD 0 描述）与 engine.js 嗅探同一词表：

| IFD 0 描述 | 判定 | 行为 |
|---|---|---|
| OME-XML | `ome-tiff` | 不是转换输入，staging 前类型化拒绝（平台可直接读） |
| JSON 且 `source_format ∈ CONVERTER_SOURCE_FORMATS`（含新增 `leica-scn-jpeg`） | `converter-bigtiff` | 不是转换输入（这是本工具自己的产物） |
| `<scn>` + Leica 命名空间 | `leica-scn-jpeg` | 走本适配器（结构门槛同 SVS；荧光拒绝） |
| 含 `Aperio` | `aperio-svs-jpeg` | SVS 适配器（行为不变） |
| 其他 | 未知 | 「未标识 Aperio / Leica SCN：不猜」类型化拒绝 |

工作台 `slide-sniff.js`：`.scn` 从扩展名快路径（temporary）改为走 TIFF
头解析——SCN XML + 明场 + 压缩 7 → `convert`（需要转换）；荧光/非 JPEG/
非 SCN XML 描述 → `temporary`（暂时直传变体，legacy-direct）。

### 1.5 Resume

checkpoint 携带 `adapter` + `adapter_version`（本次修复：此前所有适配器
的 checkpoint 都硬编码 MRXS 的版本号）。适配器版本钉扎在**核心层**强制
（`convert_scn_to_bigtiff_resume` 拒绝版本不符或字段缺失的 checkpoint；
`convert_svs_to_bigtiff_resume` 拒绝点名其他版本的 checkpoint，无版本
字段的 legacy journal 保持可续跑并在文档中写明），与 MRXS 同一契约；
runner 在 resume 时再做一次 SCN 版本钉扎。换适配器（任务记录被改成其他
adapter 或副本 probe 出不同 adapter）与源副本被改（staged sha256 不符）
都是类型化拒绝（`resume_refused/source-adapter`、
`source_changed_refuse_resume`）。

## 2. 实测（真实样本：公开 OpenSlide `Leica-1.scn`，CC0）

样本：`SCN_SAMPLE` 环境变量传入（291,812,870 B；sha256
`63a3c00f…` 见 `Leica-index.yaml`）。以下为本次运行记录
（`/usr/bin/time`，默认 `--memory-budget` 192 MiB，bf-ome）：

| 项 | 值 |
|---|---|
| probe | 主图 36832×38432@512×512，5 层（4× 递减至 144×150），label 1616×4668 被排除，mpp 0.5 µm，objective 20，明场 |
| 输出 | 290,410,095 B（OME-BigTIFF，SubIFD 金字塔，ifd_count=5） |
| tiles | 5,844 全部原样搬运（raw_copied=5,844，reencoded=0，filled=0——该样本网格完整） |
| 耗时 / RSS | wall 0.36 s；max RSS 5,012 KiB（≈4.9 MiB，远低于 192 MiB 预算） |
| 320M 门 | pytest 内 `systemd-run -p MemoryMax=320M -p MemorySwapMax=0` 实跑转换成功 |
| 像素门 | OpenSlide 读原文件（bounds-x/y=10778/35096 的 collection 画布）对齐后，三个组织 ROI 的 **L0 逐像素零误差**；level 1/2 几何 = XML 声明、均值误差 < 2.0（4× 画布重采样相位差，见 pytest 注释） |
| native_rgb | `slide_io.open_slide` 打开产物 `is_native_rgb` 为真 |
| 浏览器 == 原生 | bf-ome 与 bf-classic、preserve 与 compact 全部 sha256 相等（C2 run_parity `--input`；C3 `sc-scn-page-e2e`） |

合成稀疏夹具（`gen-scn --sparse`，580,514 B）：L0 网格 5×3 缺 2 cell →
`tiles_filled=2`，输出 560,278 B，填充块为共享白色 tile（Rust 测试解码
校验全 255）；浏览器 bf-ome/bf-classic parity 与原生逐字节一致，
compact 再各一次亦一致。

变体拒绝实测（pytest，全部发生在写出之前、probe 同拒绝）：

| 变体 | 稳定码 | 信息 |
|---|---|---|
| `illuminationSource=fluorescence` | `unsupported_kfb_variant` | 荧光 SCN 不在明场转换支持集（复制前拒绝） |
| 层压缩 8（deflate 标记） | `unsupported_kfb_variant` | 不是基线 JPEG（259=7），无法按原样搬运 |
| 层压缩 33003/33005 | `unsupported_kfb_variant` | JPEG 2000 不在支持集 |
| 描述伪装 OME | `unsupported_kfb_variant` | OME-TIFF 不是转换输入 |
| 描述伪装转换器 JSON | `unsupported_kfb_variant` | 本工具导出的 BigTIFF 不是转换输入 |
| 描述缺失/外来厂商 | `unsupported_kfb_variant` | 未标识 Aperio / Leica SCN：不猜 |
| `--memory-budget 65536` | `resource_profile_insufficient` | 内存预算不足（分配前） |

## 3. 未覆盖项 / 已知边界

1. **多 ROI SCN**：转换器只导出面积最大的主 image（与 OpenSlide 的主图
   一致）；其余 ROI 作为 associated 报告尺寸、不导出。OpenSlide 的呈现
   是「collection 画布 + 偏移贴图」，本转换器输出 ROI 本体——平台查看器
   以产物自身为准（几何门已断言与 XML 声明一致）。
2. **`--desc` 伪装 / 多 image 荧光混合**：只审主图的 illuminationSource；
   非主 image 的荧光声明不影响判定（它本来就不导出）。
3. **稀疏网格的语义**：`(0,0)` = 缺失（填充）；TileOffsets 数 ≠ 网格数 =
   结构错误。未见「TileOffsets 短于网格」的真实样本；若有则是新变体，
   按不猜处理。
4. **Z 轴/拼接**：`<view spacingZ>`、多 Z/多 S 不在支持集（明场单层）；
   XML 里出现也不解析（层数仍由 dimension 决定）。
5. **低倍层像素等值**：OpenSlide 画布金字塔与 ROI 本地金字塔存在 4×
   重采样相位差，低倍层对比给均值上限（<2.0）而非零误差；L0 是零误差
   硬门。
6. **直传关闭**：`direct_import` 对 scn 保持 `open`（本阶段决定：是否关闭
   由用户看完本报告后另行处理）；工作台分流已把 JPEG 明场 SCN 归
   「需要转换」。
7. **Ventana/Hamamatsu 等其他 TIFF 厂商**：不在本次范围，维持「不猜」
   拒绝。

## 4. 证据索引

- Rust：`cargo test -p slide-transform-core --features fixtures` 全绿
  （含 `tests/scn.rs` 15 项；审查 2026-10-05 回归后：单边上限、
  vendor 路由、核心层适配器版本钉扎各带回归测试）
- pytest：`tests/test_slide_transform_scn.py` 11 项全绿
  （SCN_SAMPLE 设置时含真实样本像素门；缺失自动跳过）
- vitest：`tools-scn-input.test.ts` 11 项、`slide-sniff.test.ts`
  （含 SCN 四例）、`tools-svs-input.test.ts` 回归全绿
- C2：`run_parity.js --input`（合成稀疏 + 真实样本，preserve/compact）
  PASS；`run_faults.js --only scn` 2/2 PASS；`test_no_whole_file.js` PASS
- C3：`run_e2e.js --only sc` PASS（真实样本页面全流程 + 荧光变体拒绝）
