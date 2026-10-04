# 切片工具体验与格式扩展：发布交接（U1–U3、F1、F3）

日期：2026-10-03。分支 `ux-formats`（基于 `release/r1` 的 `7aa4d88`，即已上线的 rc6）。**未推送、未部署。**

> **状态（2026-10-04 更新）：** 独立 review 的缺陷已修复并经独立复核关闭（[复核](ux-formats-independent-recheck-20261004.md)）；真实 COS/XHR 小文件验收在其声明范围内通过（[报告](ux-formats-real-cos-acceptance-20261004.md)：rc6 运行时 + 只读候选源在回环 127.0.0.1:18094，生产摄取 worker 未变更，不构成镜像打包证明）；候选镜像已构建并完成打包验收（[发布计划](ux-formats-release-plan-20261004.md)）。修复与门禁证据见 §4、§7。
>
> 整包仍未发布，待用户完成两项视觉验收：
> - compact 画质盲评；
> - MRXS v2 低倍层（L0 派生金字塔，§7.4）的产品验收。
>
> 视觉评审包已私下备好。Windows、真实低内存设备与真实系统保存对话框仍未验证。在用户接受之前，F3 仍是候选能力。

按 [实施计划](tool-ux-and-format-expansion-implementation-plan-20261003.md) 与 [review](tool-ux-and-upload-progress-review-20261003.md) 实施。F2（SVS / JPEG 2000）只完成 [可行性评估](f2-jpeg2000-feasibility.md)，未实现；赛维尔等待样本，未实施；AI 会话 403 是独立问题，本分支未改动。

## 1. 交付内容

| 阶段 | 用户可见的变化 | 详细报告 | 提交 |
| --- | --- | --- | --- |
| U1 | 工具页与工作台的 COS 上传进度按字节显示；阶段：排队 → 上传至腾讯云 → 工作台接收 → 校验/发布 → 可查看；body 发完未确认时显示「数据已发送，等待确认」 | 本文 §3 | `27f6e85` |
| U2 | 工具页简化：拖放区 + 选择文件、识别后一张配置摘要、画质选择、「更多选项」折叠、「转换并上传到工作台 / 仅转换」、明确「保存到电脑」 | 本文 §3 | `70bf787` |
| U3 | 「更小文件（有损）」= `compact-jpeg-v1`（q80 · 4:2:0 · Annex-K），仅明场；默认仍为「保留画质」 | [u3 报告](u3-compact-encoding-report.md) | `5b07554` |
| F1 | Aperio SVS（JPEG 编码）→ OME-TIFF / 经典 TIFF；tile 原样搬运；JPEG 2000 SVS 复制前拒绝 | [f1 报告](f1-svs-adapter-report.md) | `1d1955d` |
| F3 | 标准 MRXS 完整文件夹（「选择文件夹（MRXS）」或拖入文件夹）→ L0 按真实位置拼接后重编码；低倍层由 L0 逐级 2×2 平均派生 | [f3 报告](f3-mrxs-adapter-report.md) | `042e051`、`b484b0e`、review 修复 `0ec7569`、`07fa71d`、`e708144` |

合并提交：`e636f11`、`a04a5d2`（SVS × compact：SVS 也可选更小文件，实际逐 tile 重编码）、`9325021`（SVS 接入页面）、`af835dc`、`74cb0ec` 与 `a48ac4b`（review 修复）。

## 2. 支持矩阵（浏览器转换）

| 输入 | 保留画质（默认） | 更小文件（有损） | 说明 |
| --- | --- | --- | --- |
| KFB 明场 | 完整 tile 原样搬运，边缘 tile 重编码 | 每 tile 重编码 | 与 rc6 产物逐字节一致 |
| KFBF 荧光 | 原有通道合同 | 不提供 | 荧光不做有损重编码 |
| Aperio SVS（JPEG） | tile 原样搬运，共享 JPEGTables 写入 347 | 每 tile 重编码 | label/macro/缩略图不导出 |
| Aperio SVS（JPEG 2000 33003/33005） | 拒绝（复制前） | 拒绝 | F2 另行评估 |
| 标准 MRXS 明场完整包 | L0 按真实位置拼接 → YCbCr 4:2:2 q96 重编码；低倍层由上一层 2×2 平均派生（`l0-box2`，适配器 v2）；空白区每层只存一份 | 同样的拼接与金字塔，使用 U3 参数 | 不存在字节搬运；输出可能大于源包；内存超出资源档位时返回 `resource_profile_insufficient` |
| 只给 `.mrxs` 或单个 `.dat` | 拒绝并列出缺少的成员 | — | 不开始复制 |
| 荧光/多通道 MRXS、PNG/BMP 数据 | 拒绝 | — | 仅用夹具验证，无真实样本 |

输出布局与 rc6 一致：明场默认 OME-TIFF（适合 QuPath），可选经典 TIFF（兼容 OpenSlide 工具）。

## 3. 关键结果（审查方独立复核过的数值）

**U1 上传进度**

- 节流的单分片上传在完成前出现 17 个不同的百分比值，然后依次显示「数据已发送，等待确认」、接收和校验阶段。
- 新单元测试对旧上传器有 15/34 失败，即确实覆盖了新行为。
- 真实桶的 CORS 预检只读核对通过：三个站点 origin 的 PUT 与 content-type 均允许，ETag 已暴露。
- 真实桶的小文件 XHR 上传已于 2026-10-04 通过（[报告](ux-formats-real-cos-acceptance-20261004.md)）：每次完整工具页上传观测到 190 个进度事件；上传中刷新后续跑沿用原 ingestion，仅覆盖首个分块未完成的情况，不是多分块已确认跳过的测试；取消后服务端任务为 cancelled，重试重新发起并成功发布；小任务未逐一观测每个短暂服务端阶段。

**U3 更小文件**

- KFB-1（真实样本）：输出约为保留画质的 90%（198 374 506 / 220 055 361 B）。
- 致密组织的逐通道平均误差约为 R 3.1 / G 2.2 / B 6.2（满量程 255），最大 68。
- CMU-1 SVS（源为 JPEG q30）：输出为保留画质的 72%，组织区逐通道平均误差 3–8。
- 审查方肉眼并排对比裁片未见差别，但这不能替代人工盲评。
- 页面文案说明节省幅度可能有限，且不适合颜色定量用途。

**F1 SVS**

- CMU-1、CMU-2、CMU-1-Small-Region 的 tile payload 100% 与源相同。
- 经典 TIFF 经 OpenSlide 读取，各层与源 SVS 像素完全一致。
- QuPath 0.6.0-rc5 与 0.7.0 严格门禁均通过。
- 平台 OME 读取路径（tifffile）在 CMU-1 降采样层与 OpenSlide 有解码差异：level 1 组织区平均 6.0。读原始 SVS 时同样存在该差异，因此不是转换造成的。

**F3 MRXS**

- level 0 拼接与 OpenSlide 完全一致：三个样本合计 3 + 3 + 8 个组织 ROI，均值 0.0。
- 低倍层的处理见 §7.4（review 修复）。修复前的做法是用扫描仪自带的低倍图做整数像素对齐，与 OpenSlide 均值相差 8–16；该做法已被替换。
- 保留画质的重编码误差：level 0 组织区逐通道平均 1.2–2.2。
- CMU-1（565 MB 包）保留画质输出（适配器 v2）1 132 364 509 B，3 分 15 秒，在 192 MiB 内存上限下 RSS 约 44 MB。初版为 2.67 GB，经填充 tile 去重与 4:2:2 q96 修订降至 1.08 GB；改用 L0 派生金字塔后增加 5.3%。
- QuPath 两个版本严格门禁通过，12/12 区域与经典输出逐像素一致。

**字节一致性锚点**（native CLI，最终分支复核）

- KFB-1：bf-ome `374c70c8…`、经典 `385a59c6…`、compact `86131cab…` / `037c552d…`。
- C2 smoke：`6e8744f9…`，compact 为 `7e2f4f82…`。
- SVS CMU-1：`9d1ac1e8…` / `00198666…`；CMU-1-Small-Region：`fcb6d171…`，compact 为 `edcaf84c…`。
- MRXS CMU-1-Saved-1_16（适配器 v2）：`62da50da…` / `2b5bcde1…`。
- 页面（C3 `sv` / `mx` 场景）的 SVS 与 MRXS 产物和 native 逐字节相同。

## 4. 最终门禁（review 修复合入后，串行、内存上限）

被测代码为 review 修复合并之后（`a48ac4b`），以及随后的测试修正 `4f0f41b`。审查方于 2026-10-04 运行，所有重型命令都在 `systemd-run --user --scope -p MemoryMax=…` 下串行执行。

| 门禁 | 结果 |
| --- | --- |
| Rust workspace（`--features slide-transform-core/fixtures`） | 153 passed，0 failed |
| vitest `tests/js` | 47 个文件，763 passed |
| pytest 全量（排除 `test_e2e_pg_reap.py`、`tests/e2e`；上限 10 GB） | 2921 passed，10 skipped，4 failed（即 §5 列出的 4 项基线失败） |
| C3 工具页 | 32/32，含 `sv`（真实 SVS，含 JPEG 2000 拒绝）和 `mx`（真实 MRXS 文件夹，v2 哈希） |
| C4 上传 | 17/17；工作台字节进度 PASS |
| R1 一键转换上传 | 31/31 |
| C2 smoke / smoke compact | PASS（`6e8744f9…` / `7e2f4f82…`） |
| C2 故障矩阵 | 43/43（含 4 条 review §2 新行） |
| C2 browser == native | 逐字节相同：KFB-1 `374c70c8…`、SVS `fcb6d171…` / `dcefe860…`、MRXS `62da50da…` / `2b5bcde1…` |
| no-whole-file 静态门禁 | PASS |
| review §1 复现（`scripts/test_mrxs_memory_budget.sh`，MemoryMax=192M） | PASS：probe 与 convert 都返回 `resource_profile_insufficient`，进程未被杀 |
| review §2 复现（审查方脚本，只改端口与路径） | `resumeRefused: true`，`completion: null` |
| review §3 复现（`gt_gate_fails_when_reference_raw_files_are_missing` / `_empty`） | 2 passed：缺失或空材料必然失败 |

`test_slide_transform_core.py` 已随全量 pytest 一起跑过。该文件在 review 之前单独运行（上限 12 GB）的结果为 17 passed、2 skipped；设置样本环境变量后为 23 passed、1 skipped。

## 5. 待办与未验证项

1. **真实 COS 小文件上传**：已完成（2026-10-04，[报告](ux-formats-real-cos-acceptance-20261004.md)）。范围为 rc6 运行时镜像 + 只读候选源（127.0.0.1:18094）、真实平台数据库与未变更的生产摄取 worker、仅合成文件；它不是新镜像的打包验收，打包验收见[发布计划](ux-formats-release-plan-20261004.md)。
2. **更小文件的人工盲评**：仍由用户完成，视觉评审包已私下备好。完成前 compact 只是一个可选项，不推荐用于颜色定量。
3. **大小与时间预期**：MRXS 保留画质的输出通常大于源包（CMU-1 约 2.0 倍）。Mirax2.2-1（2.9 GB）在 native 上转换需要 12 分钟，超过 CLI 默认的 `--timeout 600`；浏览器路径没有这个限制。超过上传上限（9 500 000 000 B）的产物只能本地保存。
4. **未覆盖的输入**：
   - 没有真实的 YCbCr payload SVS、荧光 MRXS、PNG/BMP MRXS 样本，这些路径只经过夹具验证。
   - Windows、真实系统保存对话框、真实 4/8 GB 设备仍未验证。
5. **已知回归基线失败**（与 rc6 相同，非本分支引入）：
   - `test_admin_preview` 3 项：仍调用已删除的 `/api/upload`。
   - `test_ai_budget_wiring::test_ui_budget_card_and_max_steps_sync_present`：断言 admin 0.4.12，实际交付 0.4.14。
6. **主机 OOM 教训**：真实样本测试与浏览器套件并行跑会撑爆 18 GB 主机。门禁必须串行，并放在 `systemd-run … MemoryMax` 下执行。
7. **上线**：JS、WASM 与 manifest 必须一起发布（镜像已验证三者同批且哈希一致）；部署前需用户批准，不限晚间窗口；步骤见[发布计划](ux-formats-release-plan-20261004.md)。
8. **MRXS 几何的产品验收**：低倍层改为由 L0 派生后，像素不再与 OpenSlide 的低倍渲染逐像素相同（见 §7.4）。仍需用户确认，视觉评审包已私下备好。
9. **真实样本测试口径**：MRXS Rust 的真实样本 opt-in 测试在未设置素材环境变量时提前返回，不计为真实样本通过（见[独立复核](ux-formats-independent-recheck-20261004.md)）；GT 门禁仍有显式的高成本 ROI 跳过分支。

## 6. 隐私

提交中只出现 CC0 / 可分发的公开样本（OpenSlide 测试集）名称与哈希。临床 KFB 样本只用别名 KFB-1。样本路径通过环境变量传入，缺失时测试跳过。

## 7. 独立 review 修复记录（2026-10-04）

### 7.1 P1：MRXS 内存保护（`0ec7569`）

**修复**

- 新增 `budget.rs`，按资源档位预算减 24 MiB 预留作为上限。
- CLI 增加 `--memory-budget`，默认取 saver 的 192 MiB。WASM 侧的 probe 与 convert 都接收浏览器资源档位的预算。
- 在以下分配发生之前，先做溢出安全的字节估算：位置表、图像记录、placement 排序副本、CSR 数组、图像缓存的解码像素、画布。
- 超出预算时返回 `resource_profile_insufficient`，页面上已有对应文案。

**复现与回归**

- 审查方的 51 KB large-grid 包：
  - 旧代码：192 MiB 下被 SIGKILL（exit 137），不限内存时 RSS 达 394 MB。
  - 新代码：probe 与 convert 都返回类型化错误（exit 1）。
- 入仓回归：Rust `large_grid_nominal_fallback_refused_within_budget`（计数分配器峰值 < 64 MiB）、`scripts/test_mrxs_memory_budget.sh`。

**真实样本**（MemoryMax=192M）

- 三个样本都能在 saver 档位内完成转换。
- CMU-1 RSS 43.7 MB，产物字节不变；Mirax2.2-1 RSS 52 MB。

### 7.2 P1：续跑绑定原包身份（`e708144`）

**修复**

- 续跑前，worker 从 OPFS 重新读取全部成员，自行重算：成员摘要、规范成员表、成员数、总长、根摘要。
- 重算结果与 job record 和 journal 代次中固定的身份比较，包内 manifest 只当作被检数据，从不作为期望值。
- 不一致时返回 `source_changed_refuse_resume`。

**复现与回归**

- 审查方脚本：旧代码 `refused=false`，状态为 ready；新代码被拒绝，且不产生任何输出。
- C2 新增 4 行负例，旧代码全部失败：
  - 元数据修改并同步更新 manifest；
  - 像素成员替换；
  - 增加成员；
  - 删除成员（旧代码上报的是错误码，不是 source changed）。
- 诚实的中断续跑仍与 native 逐字节一致。

### 7.3 P2：GT 门禁失败即关闭（`0ec7569`）

**修复**

- 比较逻辑抽成 `compare_gt_rois`。
- 以下任一情况都会失败，并在错误中点名对应 ROI：
  - raw 或 mask 长度不精确；
  - 有效覆盖不足 1%；
  - 区域越界；
  - 没有实际比较到 L0 以及至少一个低倍层。

**复现与回归**

- 审查方的缺失、空文件两种材料都已入仓为测试：旧代码判为通过、误差 0；新代码失败。
- 严格门禁在三个真实样本上重跑：L0 均为 0.0000。

### 7.4 MRXS 低倍几何（`07fa71d`）

**方法**

- 采用 review 给出的方向：低倍层 k 是输出层 k−1 的 2×2 面积平均（`l0-box2`），因此每一层都与 L0 坐标一致。
- 上一层从输出 sink 回读，读取量有界，并计入内存预算。扫描仪自带的低倍图不再用作像素来源。
- 层尺寸不变。

**版本化**

- 适配器 v1 → v2，指纹升为 `:v2`。
- v1 的 checkpoint 在续跑时被拒绝，不会把两种金字塔混在一个产物里。

**几何验收**

- 合成测试：跨相机图像接缝的十字特征在每层都位于 L0/2^k ± 0.5 px；梯度跨接缝连续；中断点在 L0 内、L0 边界、低倍层内三种情况下，续跑都与不中断的结果逐字节一致。
- 真实样本，各层与 L0 box 链的自洽度（最差 ROI 均值）：

| 样本 | 修复前 | 修复后 |
| --- | --- | --- |
| CMU-1-Saved-1_16 | 11.5 | 2.6 |
| CMU-1 | 12.5 | 3.9 |
| Mirax2.2-1 | 19.9 | 6.9 |

- 与 OpenSlide 低倍层的配准偏移从 ≤ 0.5 px 降到 ≤ 0.24 px。

**审查方复核**（CMU-1-Saved-1_16，按精确层坐标读取）

- 第 1–4 层相对 L0 box 下采样与相对 OpenSlide 的整数相位相关偏移都是 0。
- 相对 L0 box 下采样的均值 0.3–3.5，属于逐级 JPEG 损失。
- 相对 OpenSlide（屏蔽透明区）的均值 2.7–6.3。

**与 OpenSlide 的剩余差异**

- 剩余差异来自 OpenSlide 使用扫描仪自带的低倍图，以及两者的滤波器不同，不是错位。
- 这正是 §5 第 8 项需要用户验收的内容。

**代价**

- CMU-1 产物增大 5.3%，耗时 2 分 21 秒 → 3 分 15 秒。
- QuPath 0.6.0-rc5 与 0.7.0 严格门禁通过，12/12 区域与经典输出逐像素一致。

## 8. 验收脚本修正（非产品缺陷）

真实 COS 验收沿用的私有 harness 有三处修正（[报告](ux-formats-real-cos-acceptance-20261004.md)）：技术 probe 面板已折叠，改为等待摘要卡；带自动上传意图的任务改用「继续上传」按钮，按稳定属性定位；取消动作的首次即时断言过早——UI 先更新，本地记录写入与服务端通知是异步的，以最终持久状态复核为准。三处均为验收脚本问题，未为此改动任何应用代码。
