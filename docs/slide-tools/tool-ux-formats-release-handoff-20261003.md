# 切片工具体验与格式扩展：发布交接（U1–U3、F1、F3）

日期：2026-10-03。分支 `ux-formats`（基于 `release/r1` 的 `7aa4d88`，即已上线的 rc6）。**未推送、未部署。** 按 [实施计划](tool-ux-and-format-expansion-implementation-plan-20261003.md) 与 [review](tool-ux-and-upload-progress-review-20261003.md) 实施。F2（SVS / JPEG 2000）只完成 [可行性评估](f2-jpeg2000-feasibility.md)，未实现；赛维尔等待样本，未实施；AI 会话 403 是独立问题，本分支未改动。

## 1. 交付内容

| 阶段 | 用户可见的变化 | 详细报告 | 提交 |
| --- | --- | --- | --- |
| U1 | 工具页与工作台的 COS 上传进度按字节显示；阶段：排队 → 上传至腾讯云 → 工作台接收 → 校验/发布 → 可查看；body 发完未确认时显示「数据已发送，等待确认」 | 本文 §3 | `27f6e85` |
| U2 | 工具页简化：拖放区 + 选择文件、识别后一张配置摘要、画质选择、「更多选项」折叠、「转换并上传到工作台 / 仅转换」、明确「保存到电脑」 | 本文 §3 | `70bf787` |
| U3 | 「更小文件（有损）」= `compact-jpeg-v1`（q80 · 4:2:0 · Annex-K），仅明场；默认仍为「保留画质」 | [u3 报告](u3-compact-encoding-report.md) | `5b07554` |
| F1 | Aperio SVS（JPEG 编码）→ OME-TIFF / 经典 TIFF；tile 原样搬运；JPEG 2000 SVS 复制前拒绝 | [f1 报告](f1-svs-adapter-report.md) | `1d1955d` |
| F3 | 标准 MRXS 完整文件夹（「选择文件夹（MRXS）」或拖入文件夹）→ 按真实位置拼接后重编码 | [f3 报告](f3-mrxs-adapter-report.md) | `042e051`、`b484b0e` |

合并提交：`e636f11`、`a04a5d2`（SVS × compact：SVS 也可选更小文件，实际逐 tile 重编码）、`9325021`（SVS 接入页面）、`af835dc`。

## 2. 支持矩阵（浏览器转换）

| 输入 | 保留画质（默认） | 更小文件（有损） | 说明 |
| --- | --- | --- | --- |
| KFB 明场 | 完整 tile 原样搬运，边缘 tile 重编码 | 每 tile 重编码 | 与 rc6 产物逐字节一致 |
| KFBF 荧光 | 原有通道合同 | 不提供 | 荧光不做有损重编码 |
| Aperio SVS（JPEG） | tile 原样搬运，共享 JPEGTables 写入 347 | 每 tile 重编码 | label/macro/缩略图不导出 |
| Aperio SVS（JPEG 2000 33003/33005） | 拒绝（复制前） | 拒绝 | F2 另行评估 |
| 标准 MRXS 明场完整包 | 按真实位置拼接 → YCbCr 4:2:2 q96 重编码；空白区每层只存一份 | 拼接 → U3 参数 | 不存在字节搬运；输出可能大于源包 |
| 只给 `.mrxs` 或单个 `.dat` | 拒绝并列出缺少的成员 | — | 不开始复制 |
| 荧光/多通道 MRXS、PNG/BMP 数据 | 拒绝 | — | 仅用夹具验证，无真实样本 |

输出布局与 rc6 一致：明场默认 OME-TIFF（适合 QuPath），可选经典 TIFF（兼容 OpenSlide 工具）。

## 3. 关键结果（审查方独立复核过的数值）

**U1 上传进度**

- 节流的单分片上传在完成前出现 17 个不同的百分比值，然后依次显示「数据已发送，等待确认」、接收和校验阶段。
- 新单元测试对旧上传器有 15/34 失败，即确实覆盖了新行为。
- 真实桶的 CORS 预检只读核对通过：三个站点 origin 的 PUT 与 content-type 均允许，ETag 已暴露。
- 尚未进行真实桶的小文件上传。

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
- 降采样层采用整数像素对齐，与 OpenSlide 的亚像素渲染有差异：均值 8–16，已记录为容差。
- 保留画质的重编码误差：level 0 组织区逐通道平均 1.2–2.2。
- CMU-1（565 MB 包）保留画质输出 1 075 354 246 B，2 分 22 秒，RSS 43 MiB；初版为 2.67 GB，经填充 tile 去重和 4:2:2 q96 修订后降至此值。
- QuPath 两个版本严格门禁通过，12/12 区域与经典输出逐像素一致。

**字节一致性锚点**（native CLI，最终分支复核）

- KFB-1：bf-ome `374c70c8…`、经典 `385a59c6…`、compact `86131cab…` / `037c552d…`。
- C2 smoke：`6e8744f9…`，compact 为 `7e2f4f82…`。
- SVS CMU-1：`9d1ac1e8…` / `00198666…`；CMU-1-Small-Region：`fcb6d171…`，compact 为 `edcaf84c…`。
- MRXS CMU-1-Saved-1_16：`42f3c650…` / `77d3b1b8…`。
- 页面（C3 `sv` / `mx` 场景）的 SVS 与 MRXS 产物和 native 逐字节相同。

## 4. 最终门禁（`ux-formats` 最终提交，串行、内存上限）

提交 `b484b0e` 的代码，审查方在 2026-10-03 运行；所有重型命令都在 `systemd-run --user --scope -p MemoryMax=…` 下串行执行。

| 门禁 | 结果 |
| --- | --- |
| Rust workspace（`--features slide-transform-core/fixtures`） | 145 passed，0 failed |
| vitest `tests/js` | 45 个文件，744 passed |
| pytest 全量（排除 `test_e2e_pg_reap.py`、`tests/e2e`） | 2921 passed，10 skipped，4 failed（即 §5 列出的 4 项基线失败） |
| `test_slide_transform_core.py` 全文件（单独运行，上限 12 GB） | 17 passed，2 skipped；带 SVS/MRXS 样本环境变量时 23 passed，1 skipped（`C1_BIG` 大文件证明需显式开启） |
| C3 工具页 | 32/32，含 `sv`（真实 SVS，含 JPEG 2000 拒绝）和 `mx`（真实 MRXS 文件夹） |
| C4 上传 | 17/17；工作台字节进度 PASS |
| R1 一键转换上传 | 31/31 |
| C2 smoke / smoke compact | PASS（`6e8744f9…` / `7e2f4f82…`） |
| C2 故障矩阵 | 39/39 |
| C2 browser == native | KFB-1 `374c70c8…`、SVS（两种布局）、MRXS（两种布局）均逐字节相同 |
| no-whole-file 静态门禁 | PASS |

## 5. 待办与未验证项

1. **真实 COS 小文件上传**：在候选环境上传一个小型合成产物，记录方法、域名、状态和字节汇总（隐藏签名），并确认清理完成。U1 把 PUT 从 fetch 改为了 XHR，上线前必须完成这一步。
2. **更小文件的人工盲评**：由用户完成。完成前 compact 只是一个可选项，不推荐用于颜色定量。
3. **大小预期**：MRXS 保留画质的输出通常大于源包（CMU-1 约 1.9 倍）。超过上传上限（9 500 000 000 B）的产物只能本地保存。
4. **未覆盖的输入**：
   - 没有真实的 YCbCr payload SVS、荧光 MRXS、PNG/BMP MRXS 样本，这些路径只经过夹具验证。
   - Windows、真实系统保存对话框、真实 4/8 GB 设备仍未验证。
5. **已知回归基线失败**（与 rc6 相同，非本分支引入）：
   - `test_admin_preview` 3 项：仍调用已删除的 `/api/upload`。
   - `test_ai_budget_wiring::test_ui_budget_card_and_max_steps_sync_present`：断言 admin 0.4.12，实际交付 0.4.14。
6. **主机 OOM 教训**：真实样本测试与浏览器套件并行跑会撑爆 18 GB 主机。门禁必须串行，并放在 `systemd-run … MemoryMax` 下执行。
7. **上线**：JS、WASM 与 manifest 必须一起发布；部署走晚间窗口，部署前需用户批准。

## 6. 隐私

提交中只出现 CC0 / 可分发的公开样本（OpenSlide 测试集）名称与哈希。临床 KFB 样本只用别名 KFB-1。样本路径通过环境变量传入，缺失时测试跳过。
