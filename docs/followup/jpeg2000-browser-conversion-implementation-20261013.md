# Aperio JPEG2000 浏览器转换实现交接（F2）

日期：2026-10-13。状态：已实施（分支 `jpeg2000-browser`；实现报告与验收数字见 [F2 实现报告](../slide-tools/f2-jpeg2000-adapter-report.md)）。[可行性研究](../slide-tools/f2-jpeg2000-feasibility.md) 的结论已按其 §8 条件落地：解码器钉在 hayro-jpeg2000 0.4.0 标量配置，颜色判定来自 codestream header，输出合同（重编码、strict-lossless 拒绝、provenance）如实标注。

## 交付范围

让符合支持合同的 Aperio SVS compression 33003 / 33005 在现有 `/tools/slides` Web Worker 中转换为 OME-TIFF / 金字塔 TIFF。保留已有 JPEG SVS、KFB、MRXS 等行为。首轮只支持三分量、8-bit、明场、独立 tile codestream、已验证的 Aperio 变体；HTJ2K、16-bit、荧光、任意 JP2、其他扫描仪 JP2K 不自动纳入。

用户选择文件后，应看见“JPEG2000 源，输出需要重新编码”的说明；不能显示为原样搬运或无损转换。转换在浏览器本地完成，支持取消、刷新恢复、本地保存及现有转换并上传；不因解码器添加服务器转换或新上传状态机。

## 代码入口与改造边界

| 入口 | 需要改变的内容 |
|---|---|
| `slide-transform-core/crates/core/src/svs.rs` | 当前明确拒绝 33003/33005；增加带 codec、颜色与预算信息的 level 合同，检测全部真实金字塔层 |
| `crates/core/src/convert_svs.rs` | 现有 JPEG probe/raw-copy/compact 重编码是 JPEG 专用；加入有界 JP2K 解码 → RGB → 当前 JPEG writer 路径 |
| `crates/core/Cargo.toml`、Cargo.lock、许可清单 | 固定实际验证过的解码器版本及 feature set |
| `crates/wasm/src/lib.rs` | 沿用流式 source/sink、进度、预算和取消接口；更新错误与探测结果，避免额外整文件复制 |
| `static/tools/slide-transform/{engine.js,worker.js}` | 能力、plan、估算和恢复身份接线；更新真实 WASM 构建产物及版本/哈希 |
| `static/tools/tools-slides.js`、i18n、指南/LLM 文档 | 展示变体范围、重编码提示、policy 冲突和具体拒绝原因 |
| 服务端 format/direct-class 合同 | 对照现有 `unconverted-variant:svs-jp2k`，确认何种浏览器能力声明成立后推荐转换。未覆盖变体继续保留既有合法退路 |

不要只删除 compression 拒绝分支后把 JP2K 数据交给 JPEG decoder，或直接写进 JPEG TIFF；两者都违反输出合同。

## 解码器与颜色

研究首选 `hayro-jpeg2000` 的纯 Rust scalar 配置。实施时先复核 upstream API、许可证和安全修订，选择并锁定确切版本；不能把 `=0.4.x` 当作可执行 Cargo 配置。native/wasm 使用相同 feature set，后续 SIMD 升级需单独证明一致性。

- 33003 已验证样本为 YCbCr、MCT=0、4:2:2：按 codestream 分量信息解码，再用经参考验证的 YCbCr→RGB；不能把 TIFF 中可能不准确的 subsampling 标签当作唯一依据。
- 33005 已验证样本为 MCT=1，decoder 逆变换后已经是 RGB，不能再次做 YCbCr 转换。
- 读取每个 tile 的 codestream header 后核对 dimensions、component count、bit depth、sampling、MCT 与 level 合同；不认识的组合 typed reject，不“猜颜色”。
- 边界 tile 解码后的有效区域与 level 实际宽高一致；输出的白色 padding、downsample 和坐标不能移位。缩略图/label/macro 与真实金字塔层的区分沿用 SVS 解析规则。

## 资源与损坏输入

先解析有界 SIZ/COD 等必要 header，再决定是否进入 decoder。使用 checked arithmetic，限制 compressed tile bytes、解码 pixels、分量数、decomposition levels 与本次可用 heap；恶意 4096²、超大原点/参考网格、异常分量或截断输入必须在大分配前拒绝。

既有 saver/normal 预算是上限，不能为 F2 静默提高。一次只保留必要 tile 解码与编码缓冲，结果及时写 sink。各层不加载整幅图、不收集全部 RGB tiles。内存增长需实测源读取、解码、RGB、编码和 writer 同时存活的峰值，而非只测 decoder。

保留逐 tile 检查取消和预算；如单 tile 解码不可抢占，限制其尺寸/工作量并记录最坏取消延迟，不宣称即时取消。decoder panic/trap、损坏 codestream 不能留下“已成功”的任务；现有临时产物与 checkpoint 清理必须继续成立。

## 输出、质量和恢复

1. 当前输出 JPEG-in-TIFF 必须重新编码 JP2K。strict-lossless 明确拒绝，在开始转换/上传前给出理由；默认模式与 compact 的参数分别复用其已锁定策略，并记录源 codec 与重新编码次数。
2. 保留像素尺寸、标定和金字塔关系；输出用 tifffile、OpenSlide/Bio-Formats 实际打开。不得声称保留源 JPEG2000 codestream 或原始质量。
3. 计划/任务 manifest 加入 decoder/codec/feature-set 与编码配置身份。旧 JPEG 任务仍能恢复；跨引擎版本的 JP2K checkpoint 不兼容时解释需重开任务，不把旧 checkpoint 当作新输出。
4. capability、UI 词表、服务端路由与实际 WASM 支持集同步；浏览器有能力但该变体不支持时显示具体退路，不切断原有查看路径。

## 实施顺序与验收

1. 集成有界 header probe 与 decoder 模块，native fixture 正负例先过。
2. 集成 SVS level/转换 plan、颜色、policy 和 output writer；保持原 JPEG 测试不变。
3. 接入 WASM/Web Worker、任务恢复与取消，补界面说明及 capability。
4. 对真实公开样本做独立参考解码、完整转换、保存/恢复、内存和质量验收后再开放。

| 案例 | 验收要求 |
|---|---|
| 33003 / 33005 内部、边界、低倍 tiles | native 与同配置 WASM 一致；与 OpenSlide 参考差异有预先记录的阈值，并有人复核颜色和结构 |
| 33005 被错当 YCbCr、33003 被错当 RGB | 负对照必须失败，证明颜色判定有效 |
| strict-lossless | 拒绝 JP2K，零输出发布、零上传 |
| 重编码输出 | 支持的两类 TIFF 实际可读，尺寸/标定/层级无误；记录质量和大小，不承诺一定更小 |
| 损坏/超大 header、truncated tile、未知 MCT/bit depth | 有界 typed error，无大分配、无 trap、无成功残留 |
| 中途取消、刷新恢复、版本不兼容 | 不重复提交输出或使用错 checkpoint；既有 JPEG 恢复仍通过 |
| Saver / 4GB、8GB 限内存与 Chromium | 记录实际 heap/RSS、耗时、取消延迟；Safari/Firefox 若未测，明确边界 |

测试位置：`crates/core/tests/svs.rs` 及新增 decoder 回归、现有 WASM parity/worker/runner 测试、少量浏览器整条链路。F2 研究中的第三份 33003 样本仅部分下载，需补完整文件 SHA 与参考验证；不能沿用半文件结论。仓库夹具遵守样本许可证，研究指出 33003 非 CC0，不直接提交整片；优先生成合成负例和许可明确的最小 fixture。

所有本机测试下载、Rust target、浏览器数据、参考图、日志放 `/mnt/ssd2/tests/<run>/`，设置 TMPDIR/TMP/TEMP、CARGO_TARGET_DIR 与必要缓存。发布前更新构建 manifest、WASM/hash、许可说明、格式指南和回滚记录；不只提交 Rust 源码。
