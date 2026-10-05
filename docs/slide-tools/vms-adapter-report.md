# F7 — Hamamatsu VMS Input Adapter Report

Phase F7（先转换后上传 / 格式扩展）：浏览器切片转换工具新增 Hamamatsu
VMS（`.vms` INI 入口 + 同目录巨型 tile JPEG + 可选 map/opt/macro）输入
转换器，按 MRXS 的「文件夹包 + BundleFs + 成员身份绑定 + 拼接后重编码 +
l0-box2 降采样」模式实现。VMS 的 L0 是**精确拼接（无重叠）的马赛克**：
每块 tile 是一个 ≈64K px 的基线 JPEG，没有任何整块可搬运路径，输出瓦片
全部是「逐 tile 按 restart 区间有界分段解码 → 按真实位置拼接 → 重编码」
的产物。VMU（未压缩原始数据）类型化拒绝，不在本次范围。分支
`upload-convert-first`，worktree `.gate-tmp/ucf-wt`。
**Status: implemented；本文所有实测数据均出自本 worktree 的运行记录**
（命令与输出见各节）。

Code:

- `slide-transform-core/crates/core/src/vms.rs` — VMS 探测适配器
  （`hamamatsu-vms-bundle`，adapter version `1`）：`[Virtual Microscope
  Specimen]` INI 解析（复用 mirax 的有界 INI 解析器）、`NoLayers=1` 门、
  逐 tile JPEG 头扫描（复用 ndpi 的 `scan_strip_head`）、DRI 必需、
  列宽/行高一致性、OpenSlide 同款精确拼接几何（前缀和定位）、
  `mpp = PhysicalWidth/(1000·宽)`、缺成员聚合列出、VMU 类型化拒绝、
  map/opt 仅探测在位
- `slide-transform-core/crates/core/src/convert_vms.rs` — 拼接转换：
  256 行拼接带 × 每 tile 前向分段解码（跨带段 carry）、马赛克粘贴
  （源 tile 真实范围与带窗双重裁剪）、q96 YCbCr 4:2:2 保留画质重编码、
  l0-box2 生成尾、checkpoint/resume（每 tile `skip_to` 只扫标记）、
  内存预算
- `slide-transform-core/crates/core/src/segment.rs` — 从 `convert_ndpi`
  提取的共享 restart 分段读取器（`StripGeom`/`SegmentReader`；NDPI 与
  VMS 消费同一实现，NDPI 全量测试通过确认行为不变）
- `slide-transform-core/crates/core/src/bundle.rs` — `DirBundle::open_flat`
  （VMS 平铺布局：入口 + 同目录兄弟文件；MRXS 的 `open` 保持原样）
- `slide-transform-core/crates/core/src/vms_fixture.rs` — 合成 VMS 夹具
  生成器（纯代码，`fixtures` feature；行对齐 restart 段拼装 tile、
  忠实 `.opt`（40 字节记录 × Σmcus_y−1，含「末行缺失」怪癖）、
  macro/map 带 restart marker、变体旋钮）
- `slide-transform-core/crates/core/tests/vms.rs` — 9 项 Rust 集成测试
- `slide-transform-core/crates/cli/src/main.rs` — `probe`/`convert`
  路由（`.vms`/`.vmu` 入口）+ `gen-vms` 合成夹具子命令
- `slide-transform-core/crates/wasm/src/lib.rs` — `probeBundle`/
  `convertProfileEncodedBundle`/`convertResumeProfileEncodedBundle`
  按入口分派（`.mrxs` → MRXS、`.vms` → VMS）；adapter id/版本随 journal
  与 checkpoint
- `static/tools/slide-transform/engine.js` — `parseVmsMembers`/
  `sniffVmsBundle`/`planVmsBundle`/`planBundle`（复制前嗅探与计划）；
  `VMS_SOURCE_ADAPTER`/`VMS_ADAPTER_VERSION`/`VMS_PYRAMID_METHOD`/
  `VMS_PRESERVE_COMPOSE_FINGERPRINT`
- `static/tools/slide-transform/runner.js`、`worker.js` — `prepareBundle`
  按入口类型分派；bundle manifest 记录 VMS 适配器；`prepareScratch` 对
  VMS 跳过 index spill（拼接类与 MRXS 同款）；续跑时适配器代际不符拒绝
- `static/tools/tools-slides.js`、`tools-slides-bundle.js`、
  `templates/tools_slides.html`、`static/i18n.js` — 工具页 accept/文案/
  格式族名 `VMS (Hamamatsu)`/画质说明行（中英）
- `static/upload/slide-sniff.js` — 工作台直传分流：.vms → 需要转换
  （bundle 标记）；.vmu 维持暂时直传
- `slide_format_registry.py` — vms 目录行 `browser_convert=available`、
  `import_mode=convert`、`direct_import` 保持 open
- `upload_direct_class.py` — `hamamatsu-vms-bundle` 入「转换器产物」词表
- 测试：`tests/test_slide_transform_vms.py`（pytest）、
  `tests/js/tools-vms-input.test.ts`、`tests/js/slide-sniff.test.ts`、
  `tests/js/tools-u2-page.test.ts`、
  `tests/browser/slide_tools_c2/run_parity.js`（`--bundle` 扩展 VMS
  平铺文件夹）、`tests/browser/slide_tools_c2/run_faults.js`
  （三个 VMS 场景）、`tests/browser/slide_tools_c3/run_e2e.js`
  （`vm-vms-folder-e2e`）

## 0. 结论（Verdict）

| 要求 | 状态 |
|---|---|
| 有界读取（绝不整文件/整成员进内存） | **PASS** — tile JPEG 只按段读取（64 KiB 扫描 + 单段读取），全部经 `BundleFs::read_member_at` / `ByteSource::read_at`；`tests/browser/slide_tools_c2/test_no_whole_file.js` PASS（本次运行） |
| 按 MRXS 方式作文件夹包处理（BundleFs、成员绑定、缺成员列出、续跑重算身份） | **PASS** — 平铺布局成员名 = 入口目录相对名；`planVmsBundle` 缺成员在**任何复制之前**一条错误列出全部（pytest 断言）；续跑从 staged OPFS 字节重算整包身份（复用 F3 review §2 机制），`vms-source-changed-refused` PASS |
| 逐 tile restart 有界分段解码 + 按真实位置拼接重编码 | **PASS** — 每 tile 独立前向段游标（共享 `segment.rs`），跨带段 carry；马赛克粘贴按前缀和位置、双重裁剪；合成夹具像素门：拼接产物 vs OpenSlide 读原 `.vms` L0 均值差 0.457（max 10，纯重编码噪声）；真实样本 L0 三组织 ROI 均值误差 0.033–0.045 |
| 沿用 MRXS 保留画质参数（q96 YCbCr 4:2:2 + compact） | **PASS** — 指纹 `vms-mosaic-compose:q96:y422:hstd:v1`、`composed.mode=mosaic-compose-reencode`；compact 用锁定 U3 参数（q80 4:2:0），报告 `lossy_reencode`（仅 compact） |
| 降采样层用 l0-box2 | **PASS** — 输出金字塔 = 重编码 L0 + `generated_tail`（÷2 box 链，末层 ≤ 256；真实样本 10 层至 200×149）；`pyramid=l0-box2` 进报告/描述 JSON/OME-XML |
| 排除 macro；map/opt 不用于像素 | **PASS** — macro 检测并排除（真实样本 1191×408）；map（12800×9536）仅在位探测、从不解码（警告 `vms_map_not_used`）；.opt 仅探测在位（OpenSlide 自身也只当 hint，转换器逐字节解码无需它） |
| 内存在分配前按预算拒绝 | **PASS** — 拼接带（真实样本 75 MiB）、瓦片画布、单段解码峰值（6 B/px）全部 `charge` 在分配前，超限 → 稳定码 `resource_profile_insufficient`（pytest：64 KiB 预算拒绝且无产物，192 MiB 默认成功） |
| 变体类型化拒绝 | **PASS** — 无 restart marker（DRI=0）→ `unsupported_kfb_variant`；渐进 SOF → `jpeg_decode_failed`；缺成员 → 列表拒绝；VMU（组或入口）→ 专门拒绝；NoLayers≠1（多焦面，OpenSlide 同样只接受 1）→ 拒绝；路径穿越名 → 拒绝；全部发生在写出之前（pytest 断言产物不存在） |
| source_format + `ADAPTER_VERSION="1"`；续跑 journal 携带转换器 id/版本 | **PASS** — 报告/provenance/journal/checkpoint 携带 `hamamatsu-vms-bundle`/`1`；换版本（或字段缺失）的 checkpoint 在核心入口拒绝（Rust resume 测试） |
| 换转换器或源文件被改拒绝续跑 | **PASS** — `run_faults.js --only vms`：`vms-adapter-change-refused`（resume_refused / source-adapter）与 `vms-source-changed-refused`（source_changed_refuse_resume，翻转 staged 成员字节）PASS；诚实续跑 == 原生字节（Rust 三种切点逐字节一致） |
| CLI 路由 + `gen-vms` 子命令 | **PASS** — probe/convert 按入口扩展分派（.vms/.vmu）；`gen-vms` 无样本单测夹具（变体旋钮 ×6） |
| wasm probe/convert/resume 路由 | **PASS** — `probeBundle`/`convert*Bundle` 按入口分派；VMS 探测文档含逐 tile 几何与 estimate |
| 工具页文件夹入口识别 VMS 包；accept、格式名称、i18n 中英 | **PASS** — `.vms/.vmu` 进束包路由（`bundleRoute`/`looksLikeBundleMember`）与 accept；文件夹选择/目录 drop → 同一 `prepareBundleSource`；`VMS (Hamamatsu)` 格式族名；VMS 画质说明行（`tools.quality.vms.note` 中英） |
| slide-sniff 从「暂时直传」改「需要转换」；注册表 `browser_convert=available`；`direct_import` 保持 open | **PASS** — .vms → convert（bundle 标记）；注册表 vms 行改 available/convert，`direct_import` 保持 open（是否关闭直传由用户看本报告后决定，未动）；.vmu 维持 temporary |
| 原生 CLI 在默认 192 MiB 预算下转换真实样本成功（MemoryMax=320M 实跑） | **PASS** — `systemd-run -p MemoryMax=320M` + 默认预算转换成功（输出 1,761,618,512 B，149,850 瓦片，validate 通过；compact 276 s 实跑见 §3） |
| 浏览器 == 原生逐字节一致（保留画质与 compact 各一次） | **PASS** — 合成 VMS 包（C2 `run_parity.js --bundle`）bf-ome `96369f59…` / bf-classic `5104e3de…` 与原生 equal=true；真实样本页面场景（C3 `vm-vms-folder-e2e`，本次实跑 PASS）：页面文件夹选择识别 `VMS (Hamamatsu)`，preserve sha `cf9ca6c5…` 与 compact sha `b851932e…`（与独立 CLI compact 实跑逐位一致）均等于原生，no-restart 变体复制前拒绝且无任务目录 |

## 1. 设计

### 1.1 文件模型（`vms.rs`）

真实布局对照公开 CC0 样本（CMU-1 VMS，`CMU-1-40x - 2010-01-12
13.24.05.vms` 711 B + 4 个 tile JPEG 共 633,593,176 B + .opt 762,840 B +
macro 44,802 B + map 12,448,223 B）逐字节确认：

```text
<dir>/<stem>.vms            INI 入口（[Virtual Microscope Specimen]）
<dir>/<stem>.jpg            ImageFile      = tile (0,0)
<dir>/<stem>(1,0).jpg       ImageFile(1,0) = tile (1,0)   … 平铺兄弟文件
<dir>/<stem>(0,1).jpg / (1,1).jpg
<dir>/<stem>.opt            OptimisationFile（可选；restart 偏移 hint）
<dir>/<stem>_macro.jpg      MacroImage（可选；检测并排除）
<dir>/<stem>_map2.jpg       MapFile（可选；仅探测在位）
```

- **入口契约**：`NoLayers=1`（多焦面拒绝，OpenSlide 同样只接受 1）、
  `NoJpegColumns/NoJpegRows` ∈ 1..=4096、`ImageFile`/`ImageFile(x,y)`
  全网格在场；可选 `MapFile`/`OptimisationFile`/`MacroImage` 只探测在位
  与安全名。**缺成员聚合列出**后一次拒绝（对比 MRXS 的首错即停——本
  适配器按任务要求列出全部缺失）。
- **tile 契约**：基线 JPEG（SOF0/1）、3 分量、**DRI > 0 强制**（≈64K px
  的 tile 整块解码 ≈ 11 GB RGB，分段是唯一有界解码单元）；真实样本
  DRI=512、MCU 8×8（4:4:4）→ 段 = 4096×8 px。
- **拼接几何（OpenSlide 同款）**：tile 精确拼接、**无重叠**。L0 宽 =
  第 0 行各列宽之和、高 = 第 0 列各行高之和；tile (c,r) 位于列宽/行高
  前缀和。真实样本：列宽 [61440, 40960]、行高 [61440, 14848] →
  102400×76288。同列宽/同行高必须一致（否则不是可拼接矩形，类型化拒绝）。
- **标定**：`mpp = PhysicalWidth/(1000·L0 宽)`（PhysicalHeight 同式，
  nm 单位）——实测 0.22819824/0.22753125 μm，与 OpenSlide 逐位一致；
  objective = `SourceLens`（实测 40）。
- **.opt**：40 字节记录（int64-LE，每 MCU 行的熵起点），tile 按行主序
  打包，实测 19,071 = Σmcus_y − 1（「整个文件最后一行缺失」的已知怪癖）。
  适配器只报告其在位——OpenSlide 自己也只把它当 hint（校验失败回退全
  扫描），转换器逐字节解码时它没有用途。

### 1.2 拼接转换（`convert_vms.rs`）

每 tile 一个前向段游标（共享 `segment.rs`，与 NDPI 同一扫描/解码规则；
成员经 `MemberSource` 适配为 `ByteSource`，strip 区间 = 整个成员）：

1. **256 行拼接带**：带缓冲 = 256 行 × 拼接全宽 × 3 B（真实样本
   75 MiB——saver 168 MiB 可用预算下的主项，分配前 charge）；
2. **带内逐 tile 解码**：与带相交的每个 tile 从其游标继续解码段、按
   马赛克位置粘贴（`paste_segment_mosaic`：源 tile 真实像素范围 × 带窗
   双重裁剪，兼容边缘 tile 的非整 MCU 宽）；跨带底的段解码一次携带
   （carry）到下一带；每个 tile 的段只解码一次；
3. **续跑**：checkpoint 逐输出行（与 NDPI/MRXS 同一 `ResumePoint`）；
   恢复时每个 tile 的游标 `skip_to` 到所在带的第一个段（只扫标记不解
   码），续跑输出与不间断运行逐字节一致（Rust 三切点测试：L0 中段 /
   L0 末行 / 生成层内）；
4. **重编码**：带的每个 256 瓦片从带裁剪（层外白填充）、按当前编码
   profile 重编码、顺序写 tile 游标；每行发 progress + checkpoint。

### 1.3 输出

- 保留画质（`preserve-source-v1`）：每个输出瓦片按
  **YCbCr 4:2:2 · q96 · 标准 Annex-K Huffman** 重编码，指纹
  `vms-mosaic-compose:q96:y422:hstd:v1`，`composed.mode =
  mosaic-compose-reencode`。本格式没有逐字节搬运路径（输出 256 网格
  与源 tile 网格不对齐），报告如实声明。
- 更小文件（`compact-jpeg-v1`）：同一路径、锁定 U3 参数（q80 4:2:0）。
- 降采样层：`l0-box2`（`gtiff::generated_tail`：÷2 box 链，末层两边
  ≤ 256；已提交前层瓦片经 `tile_record`/`read_output_at` 回读构成，
  与 MRXS v2/NDPI/通用 TIFF 适配器同方法）。**map 图像从不用于像素**。
  strict-lossless 与本适配器互斥（类型化 `pixel_policy_violation`）；
  荧光 profile 拒绝（VMS 恒为明场）。

## 2. 支持范围

| 输入 | 判定 |
|---|---|
| Hamamatsu VMS（`[Virtual Microscope Specimen]` 组、NoLayers=1、全部 tile 基线 JPEG 带 restart marker、列宽/行高一致、成员齐全） | **转换**（本适配器；浏览器端整文件夹交接） |
| VMU（`[Uncompressed Virtual Microscope Specimen]` 组或 `.vmu` 入口） | **类型化拒绝**（未压缩原始数据不在本次范围；slide-sniff 维持暂时直传） |
| tile 无 restart marker（DRI=0） | 类型化拒绝（无有界解码单元） |
| 渐进/算术编码 tile | 类型化拒绝（SOF/算术码） |
| 多焦面（NoLayers≠1） | 类型化拒绝 |
| 缺成员（tile/map/opt/macro 引用不存在） | 一条错误**列出全部缺失**，复制/转换前拒绝（浏览器端复制前同一行为） |
| 路径穿越名（`ImageFile(1,0)=../evil.jpg`） | 类型化拒绝 |
| 列宽/行高不一致（非可拼接矩形） | 类型化拒绝 |
| macro / map / .opt | 检测在位：macro 排除（报告 `associated`）；map/opt 不用于像素（警告 `vms_map_not_used`） |

## 3. 实测（本 worktree 运行记录）

### 3.1 真实样本（CMU-1 VMS，公开 CC0 样本；tile 载荷 633,593,176 B）

- **probe**：102400×76288、网格 2×2（列宽 61440/40960、行高
  61440/14848）、objective 40、mpp 0.22819824/0.22753125（与 OpenSlide
  一致）、每 tile DRI=512、段数 115200/115200/38400/38400、macro
  （1191×408）被排除、map/opt 在位、输出金字塔 10 层至 200×149。
- **转换（保留画质）**：`systemd-run --user --scope -p MemoryMax=320M`
  + release CLI、默认 192 MiB 预算：**成功**，输出 1,761,618,512 B
  （≈2.78× 源载荷），149,850 瓦片（L0 119,200 + 9 个生成层），
  `validate` 通过。gated pytest 会话（两次完整转换 + probe/validate +
  像素比对）共 678 s。
- **转换（compact）**：同 320M 上限独立实跑 **276 s**，输出
  693,552,459 B（≈1.10× 载荷），sha `b851932e…`。
- **估算上界**：preserve `output_upper_bound_bytes` = 2,540,552,064
  （4× 载荷 + 瓦片记录；实测 1.44 倍覆盖）；compact
  `compact_upper_bound_bytes` = 956,569,124（实测 1.38 倍覆盖）。
- **像素门**（pytest `test_real_sample_l0_mean_error_and_pyramid_geometry`，
  slide_io/openslide 读产物 vs OpenSlide 读原包）：
  - L0 三组织 ROI 均值绝对误差 **0.045 / 0.033 / 0.035**（p99 ≤ 2，
    max ≤ 14），逐通道均值差 < 4；
  - 低倍层几何 = probe 声明的 l0-box2 链逐层一致（10 层）、末层
    200×149 ≤ 256；产物 `native_rgb` = True；
  - 输出 box2 层 vs 扫描仪自己的降采样层（同尺寸 ÷4 / ÷16）：
    均值误差 0.669 / 0.849（重采样核不同，组织内容紧贴）。

### 3.2 合成夹具（gen-vms，无样本单测）

- OpenSlide 完整识别并读取夹具包（含 .opt 的逐记录校验——夹具生成
  忠实偏移；macro/map 同样带 restart marker）；平台读取器
  `slide_io.open_slide` 打开转换产物 `native_rgb` = True。
- Rust 9 项（probe 契约与拼接几何、缺成员聚合列出、VMU/多焦面/穿越/
  无 restart/渐进拒绝、预算拒绝、估算上界覆盖、双 profile 契约 +
  L0 全局图案逐像素、L1 == L0 box2、续跑三切点逐字节一致 + 版本钉扎）
  全绿：`cargo test -p slide-transform-core --features fixtures --test
  vms` → 9 passed；提取共享 `segment.rs` 后 NDPI/mirax/gtiff/scn/svs
  全量 Rust 测试同步复跑全绿。
- pytest 10 项合成 + 2 项真实样本全绿（真实样本组 678 s）。
- 浏览器 parity（`run_parity.js --bundle <gen-vms 夹具>`）：
  bf-ome `96369f59…` / bf-classic `5104e3de…` 与原生 equal=true。
- 故障矩阵（`run_faults.js --only vms`）：3/3 PASS。

## 4. 未覆盖项 / 已知限制（如实声明）

1. **VMU 不在范围**：未压缩原始数据需要独立合同（组名/入口识别已有，
   但没有像素路径）；slide-sniff 维持暂时直传，注册表 vms 行不动
   `direct_import`。
2. **多焦面 VMS（NoLayers>1）拒绝**：与 OpenSlide 的接受集一致（只接受
   1），没有实现多焦面堆栈的合成。
3. **tile JPEG 的变体拒绝依赖流头**：CMYK/4 分量 tile 由「3 分量」
   契约拒绝；嵌入式 ICC（VMS 格式本身无 ICC 载体）不存在的告警
   `color_management_not_applied` 与其他明场适配器一致。
4. **列宽不一致的「锯齿」网格拒绝**：OpenSlide 对 VMS 的读取模型同样
   假定列内等宽/行内等高（其一致性断言在 JPEG 内部 tile 尺寸上）；
   本适配器在网格几何层直接拒绝，未见真实反例。
5. **非整除行宽的 restart 段**（DRI 不整除每行 MCU 数）：粘贴是索引式
   （与 NDPI 共享代码），合成夹具的段都是行对齐矩形；真实样本
   （DRI=512，每行 7680 MCU，整除）恰好整除——一个 DRI 不整除的真实
   样本未覆盖。
6. **.opt 仅作在位探测**：适配器不读取其内容（转换逐字节解码，不需要
   偏移 hint）；夹具生成忠实偏移供 OpenSlide 互操作。若未来做「随机
   访问加速」可复用 NDPI 的 `unreliable_mcu_starts` 思路。
7. **浏览器端真实样本端到端**：C2 parity 与故障矩阵用合成包（逐字节
   断言）；真实样本的页面场景（C3 `vm-vms-folder-e2e`）由
   VMS_SAMPLE_DIR 门控——本次实跑 PASS（两次浏览器内完整转换约
   25 分钟），适合门禁按需运行而非每次全量。

## 5. 门禁与后续

- `direct_import` 对 .vms **保持 open**：转换器落地与直传关闭是两个
  独立决定——是否关闭直传由用户看完本报告后决定（注册表行级字段未动，
  与 SVS/SCN/NDPI 的处理一致）。
- 浏览器产物词表四层同步（Rust `CONVERTER_SOURCE_FORMATS` 消费侧、
  engine.js、slide-sniff.js、upload_direct_class.py）：本适配器自己的
  classic BigTIFF 产物在 staging 前即被识别为「转换器输出」，不会被
  当普通输入做第二次有损重编码。
