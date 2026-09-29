# C2 浏览器运行器报告

日期：2026-09-29。对照计划：`docs/browser-slide-tools-and-baidu-plugin-agent-plan-20260929.md`
§3（接口）、§4（资源预算）、§5（浏览器 IO/磁盘/恢复，含 2026-09-29 源复制裁决）、§6 CSP、§10.2、§10.3。
前置：C0 ADR（`docs/slide-tools/c0-adr-browser.md`）、C1 报告（`docs/slide-tools/c1-core-report.md`）。
复跑命令：`docs/review-evidence/slide-tools/C2/RERUN.md`；验收记录：`docs/review-evidence/slide-tools/C2/acceptance.md`。

> 修订说明：本报告初稿由实施子代理撰写，内存门禁在测量完成前即标 PASS，且当时的读桥
> （`FileReaderSync` + 块缓存环）在 10 GiB 输入上实测进程树增量 890–944 MiB、KFBF 真实样本
> probe 9.5 分钟 / +3.7 GiB。验收方定位根因并按用户裁决（源先复制进 OPFS）改造后重测；
> 下文数字全部来自改造后的复跑。

代码：

- `static/tools/slide-transform/{runner,engine,worker}.js` — 生产运行器（ES modules，零第三方依赖）
- `slide-transform-core/` — Rust 核心（C2 扩展见 §2）
- `scripts/build_slide_transform.sh` — 构建（manifest 纳入三个 JS）
- `tests/browser/slide_tools_c2/` — harness 与驱动（Playwright 1.62.1，Chromium headless shell 1234）
- 证据：`docs/review-evidence/slide-tools/C2/results/`；原始输出 `.gate-tmp/slide-tools-c2/`（不入库）

## 0. 结论摘要

| 门禁 | 结果 | 数值 / 证据 |
|---|---|---|
| 每个崩溃点恢复后与不间断运行字节一致 | **通过** | 故障矩阵 24/24，恢复类场景 sha256 = 原生 CLI（`results/fault-matrix.json`） |
| 核心回归 | **通过** | cargo 15+2+37+9；pytest 16 passed + 1 opt-in skip；KFB-1 整文件仍 `385a59c6…` |
| 真实样本浏览器 = 原生 | **通过** | KFB-1 + KFBF-A/B/C/D 五个样本 sha256 全等（`results/parity.json`） |
| 节省档 ≤512 MiB（cgroup 模拟 4G） | **通过** | 1 GiB 101–114、4.4 GiB 176、9.8 GiB 166–176 MiB；oom/oom_kill 全 0 |
| 均衡档 ≤1 GiB（cgroup 模拟 8G） | **通过** | 4.4 GiB 147、9.8 GiB 156 MiB |
| 1→10 GiB 增量增长 ≤20%（节省档） | **字面未过** | 1 GiB 均值 109 → 9.8 GiB 均值 171 MiB（+57%）；但 4.4→9.8 GiB 为 176→171（持平），见 §5 |
| 无整文件物化 | **通过** | `test_no_whole_file.js`：禁 `FileReaderSync`、要求读桥为同步句柄直读 wasm 内存、流式复制为 BYOB |
| 取消反馈 ≤250 ms | **通过** | 状态即时翻转（终止式取消，§4.6） |
| CSP 模式 C 全响应 | **通过** | `results/csp-headers.txt` |

外部门禁（未完成，如实标注）：真实/整机 4 GB 与 8 GB 设备；Firefox/Safari/Edge；真实配额耗尽
（本机磁盘空闲大，只能注入）；`persist()` 真实手势；`showSaveFilePicker` 写用户磁盘。

## 1. 架构（生产代码）

```
页面 (runner.js)
  ├─ 状态机 selected→probing→planned→running↔paused→finalizing→validating→ready→exported
  │   (+ failed / cancelled / cleanup_pending，双槽 job.json 持久化)
  ├─ Web Locks 'slide-transform:heavy'：跨标签页一次一个重任务（复制也在锁内）
  ├─ probe(file) = 准备：文件头嗅探（8 B，仅 KFB/KFBF）→ 预复制磁盘门 → 源复制 → probe 副本
  │   → 按估算的磁盘门 → `prepared` 记录；startJob({jobId}) 直接转换已准备任务
  ├─ 恢复裁决：副本长度+sha256 复核、core/plan/policy/profile/cap/channelJson 逐项比对
  ├─ 启动清扫：记录缺失或仍为 `staging` 的任务目录（复制中崩溃）→ 删除；被垂死 worker
  │   的句柄锁住时写 pending-cleanup，下次进入重试
  └─ 导出：ready → 产物 BYOB 读入单一复用缓冲 → FileSystemWritableFileStream
        （失败绝不删除 OPFS 产物；状态保持 ready 可重试）

worker.js（专用 module worker）
  ├─ stage-source：File.stream() BYOB 读入单一 4 MiB 缓冲 → source.bin 同步句柄写；
  │   复制完成后 wasm sha256 走副本
  ├─ 同步读桥：source.bin 的 FileSystemSyncAccessHandle.read() 直接读入 wasm 线性内存
  │   （每次调用零 JS 分配）
  ├─ 输出/scratch：OPFS 同步句柄随机写；偏移 Number 安全整数断言
  ├─ journal：append-only 校验和 JSON 行，顺序 = 数据写+flush → journal 提交+flush
  └─ 故障注入钩子（init{testMode:true} 才激活；生产路径 inert）
```

IO 合同（wasm 宿主回调 v2，兼容 C1 宿主：返回 null/undefined=成功）：
`stHostReadInto(off,len,ptr)`、`stHostWrite/Truncate/Flush`（可返回错误串→类型化 CoreError）、
`stHostScratchOpen(name,preserve)`、`stHostOutReadInto`、`stHostCheckpoint`（§4.4）。

## 2. Rust 核心改动（全部在 `slide-transform-core/`）

| 文件 | 改动 | 理由 |
|---|---|---|
| `crates/core/src/resume.rs`（新） | `ResumePoint`（level/channel/cell/committed_output/ifd_tiles）+ wire JSON 解析 + 内部一致性校验 | C2 续跑入口的数据合同 |
| `crates/core/src/job.rs` | `CheckpointState`/`CheckpointCallback`、`JobControl::{with_checkpoint,checkpoint_enabled,emit_checkpoint,with_cancel}` | 转换器在已提交边界发快照；测试从 checkpoint 内取消模拟崩溃 |
| `crates/core/src/bigtiff.rs` | `resume_new`（采用已提交游标不重写头）、`begin_level_resume`（preserve-open + 采用已提交 tile 数）、`ifd_tile_counts` | BF writer 状态重建 |
| `crates/core/src/ome_writer.rs` | 同上三件（`begin_ifd_resume`） | FL writer 状态重建 |
| `crates/core/src/convert_bf.rs` | `convert_kfb_to_bigtiff_resume`：跳过已提交 cell（报告侧效应从索引重建：stats/edge_regions/warnings；边缘 tile 仅读 payload 取 qtable 复用标志）、已完成层走快速采样（单 tile 探测；宿主已做内容哈希验证）、emit checkpoint | 恢复后最终输出字节一致（原生 `tests/resume.rs` 9 项 + 浏览器故障矩阵验证） |
| `crates/core/src/convert_fl.rs` | `convert_kfbf_to_ome_resume`：level-major/channel-minor 顺序的 IFD 重建 + cell 快进（黑填充计数、裁剪 cell 重建） | 同上（OME/SubIFD 路径） |
| `crates/core/src/validate.rs`（新） | 流式 sha256（≤1 MiB 块）+ 有界 BigTIFF 结构走查（头/IFD 链/环引用/条目上限/TileOffsets-Counts 边界） | `ready` 前的 finalize 校验；原生+wasm 共用 |
| `crates/core/src/estimate.rs`（新） | 输出上界/载荷和/缺失 cell/边缘 tile 计数 | 宿主磁盘预检输入 |
| `crates/core/src/io.rs` | `ScratchFactory::create_preserve` + `FileSink::open_preserve` | 续跑时 offcnt 流保字节打开 |
| `crates/core/src/lib.rs` | 注册新模块 | — |
| `crates/core/Cargo.toml` | `sha2` 由可选改为必选（validator/身份哈希，wasm 也需要） | — |
| `crates/wasm/src/lib.rs` | 宿主 v2（§1 列表）、`convertResume`、`finalizeValidate`（0=跳过 IFD 数断言：FL 顶层链 vs 全 IFD 口径不同）、`sha256Source`、`enableSourceHash/sourceSha256`（后者运行器已不再使用）、`configure/enableCheckpoint`、`coreVersion`；convert 结果 JSON 增加 `ifd_count`（附加字段） | 浏览器合同 |
| `crates/cli/src/main.rs` | `validate` 子命令；probe JSON 附加 `estimate`（附加字段） | 原生证据对齐 |
| `crates/core/tests/resume.rs`（新，9 测试） | BF/FL 在 checkpoint 1/2/…/末尾恢复字节一致；finalize 阶段崩溃恢复；wire JSON 往返+撒谎 journal 拒绝；validator 拒截断/环 | 原生门禁 |

**非续跑路径输出不变**：KFB-1 整文件 sha256 仍 `385a59c6…`；pytest 差分 16 passed；
浏览器↔原生 parity（§7）。

## 3. 资源档位（§4）

| 档位 | 总预算 | wasm 堆软上限 | 复制缓冲 | journal 间隔 | 默认触发 |
|---|---:|---:|---:|---:|---|
| 节省 | 192 MiB | 160 MiB | 4 MiB | 16 MiB / 4 s | deviceMemory 未知/≤4 |
| 均衡 | 384 MiB | 288 MiB | 4 MiB | 32 MiB / 4 s | deviceMemory ≤8 |
| 较快 | 768 MiB | 576 MiB | 4 MiB | 64 MiB / 4 s | 显式选择 |

- `assertProfileFeasible` 在任何分配前做算术判定（最小工作集 12 MiB），不满足即
  `resource_profile_insufficient`；不以申请大数组探测 RAM。
- wasm 堆软上限在每个 checkpoint 检查；实测峰值 2.2–3.2 MiB（核心有界流式）。
- 单计算 worker（核心为顺序流水）；档位差异体现在预算与 journal 间隔。

## 4. 关键设计与偏差

### 4.1 journal / checkpoint

代次制 append-only JSON 行（`journal.jsonl`）：`gen` 头（身份/版本/策略/档位/上限/续跑基点）+
`c` 提交记录（seq、`(level,channel,cell,out,ifds[])`、区间内逐块 `[len, fnv2x32]`、记录校验和）。
读取时校验每行校验和与 seq 连续；撕裂尾行丢弃。恢复时以 journal 为准截断输出与 offcnt 尾部。

### 4.2 恢复裁决（拒绝而非盲续）

- 任务记录仍为 `staging`（复制中断）→ `resume_refused`（且启动清扫会删除该目录）。
- 源副本长度或 sha256 与复制完成时记录不符 → `source_changed_refuse_resume`（矩阵：翻转字节、截断）。
- core 版本 / plan / policy / 档位 / 输出上限 / channelJson 任一改变 → `resume_refused`。
- 任务目录被删/驱逐 → `job_dir_missing`；输出短于 journal 已提交长度 → 拒。
- 续跑不再需要用户重新选择原文件；原文件在复制后被修改不影响结果（矩阵场景验证）。

### 4.3 源复制（根因与裁决）

C2 初版的同步读桥是 `FileReaderSync.readAsArrayBuffer(File.slice())`。验收方隔离实验（4.4 GiB，
4 MiB 块，无转换，仅读）：

| 读法 | 进程树峰值增量 |
|---|---:|
| FileReaderSync 4 MiB 块（初版） | +2.4 GiB |
| FileReaderSync 1 MiB 块 | +2.5 GiB |
| FileReaderSync + 每 64 MiB 让出事件循环 | +1.1 GiB |
| 异步 `slice().arrayBuffer()` | +1.2 GiB |
| `File.stream()` BYOB 读入复用缓冲 | +99 MiB |
| OPFS 同步句柄读入复用缓冲 | +66 MiB |

每次读取产生的瞬态 ArrayBuffer 在长同步 wasm 调用期间不能及时回收；块缓存环只能减少次数，
KFBF 的数十万次 48 B 散读仍逐次整块填充（真实 KFBF-A probe 571 s、+3.7 GiB）。唯一有界的
同步读法是 OPFS 同步句柄读入既有缓冲，因此按用户裁决（计划 §5 变更）：转换前以 BYOB 流式
复制源到任务目录，之后一律读副本。代价：临时盘多一份源大小（磁盘门已计入）；收益：内存有界、
吞吐提高（转换 78→255 MiB/s，KFBF-A 402→21 s），续跑不再需要重选文件。

### 4.4 checkpoint 粒度

核心在每个 tile 行边界发 `stHostCheckpoint`；worker 按档位间隔（字节/时间）决定是否
flush+journal。测试用 `faults.journalIntervalBytes` 压低间隔以获得确定性崩溃点。

### 4.5 finalize + validate

convert 成功 → flush+close → 重开输出 → `finalizeValidate`（wasm 流式 sha256 + 有界结构走查）
→ `ready`。只有 `ready`/`exported` 可导出（`not_ready_not_exportable`）。

### 4.6 取消

同步 wasm 期间 worker 消息循环被阻塞，协作取消无法送达，因此取消 = 终止隔离 worker（计划
§10.2 认可的机制），页面随即删除任务目录；失败写 `pending-cleanup.json`，下次进入重试。终止时
所有在途 worker 请求立即以类型化错误拒绝（复制/probe 期间取消不会悬挂）。

### 4.7 磁盘预检（Chromium 配额报告的限制）

Chromium 报告的 quota = usage + min(实际可用, 10 GiB)（C0 实测 0→10、9→19、12→22 GiB）。因此：
报告余量低于 10 GiB 时是真实限制，不足即拒；余量恰为 10 GiB 上限而需求更大时判为 `uncertain`，
运行器返回 `disk_precheck_failed{uncertain:true}`，需调用方取得用户确认（`confirmUncertainDisk`）
后继续，逐块配额错误仍为可恢复错误。含源副本后，约 >4.9 GiB 的输入在新 profile 上都会走到确认，
**C3 工具页必须设计该确认交互**。

## 5. 内存测量（全部「cgroup 模拟」；基线 = 同浏览器空白 harness 页均值）

| 运行 | 约束 | 档位 | 输入 | 增量 | 临时盘峰值 | 转换吞吐 | oom/oom_kill |
|---|---|---|---:|---:|---:|---:|---|
| cg4g-1g ×3 | 4G / 无 swap | 节省 | 0.98 GiB | 113 / 114 / 101 MiB | 2.0 GiB | 436–440 MiB/s | 0/0 |
| cg4g-4g | 4G / 无 swap | 节省 | 4.43 GiB | 176 MiB | 8.9 GiB | 255 MiB/s | 0/0 |
| cg4g-10g ×2 | 4G / 无 swap | 节省 | 9.79 GiB | 176 / 166 MiB | 19.6 GiB | 253–259 MiB/s | 0/0 |
| cg8g-4g | 8G / 无 swap | 均衡 | 4.43 GiB | 147 MiB | 8.9 GiB | 295 MiB/s | 0/0 |
| cg8g-10g | 8G / 无 swap | 均衡 | 9.79 GiB | 156 MiB | 19.6 GiB | 263 MiB/s | 0/0 |

- 各档绝对上限均满足（节省档最高 176 MiB = 512 目标的 34%）。
- **规模无关性（≤20%）字面未过**：1 GiB 三次均值 109 MiB → 9.8 GiB 两次均值 171 MiB（+57%）。
  但 4.4 GiB → 9.8 GiB 为 176 → 171 MiB（不增长）：差值是较长作业达到稳态的固定开销，而不是
  随输入字节增长的泄漏。是否据此接受由用户裁决（验收记录已列出）。
- 对比改造前（同机同法）：9.8 GiB 节省档 890–944 MiB。
- 新 profile 下 9.8 GiB 作业首次启动按 §4.7 返回 `uncertain`，测量脚本以确认参数重启（结果中
  `precheckRefusedFirst=true`）。
- 输出 sha256 与改造前一致（4.4 GiB `e7998b95…`、9.8 GiB `2084a333…`）。

## 6. 故障矩阵（§10.3，24/24）

| # | 场景 | 判定 |
|---|---|---|
| 1–2 | payload 写入中杀 worker / 刷新页面 | 恢复后 sha = 原生 |
| 3 | 数据 flush 后、journal 提交前崩溃 | 同上 |
| 4 | checkpoint 提交后立即崩溃 | 同上 |
| 5 | IFD/偏移回填（finalize）中崩溃 | 同上 |
| 6 | 验证中崩溃 | 同上 |
| 7–8 | 导出中注入失败 / 导出中刷新 | 产物保留、状态 ready、重试成功、sha = 原生 |
| 9 | 注入配额错误 | `quota_exceeded_recoverable`，恢复后完成 |
| 10 | 输出句柄丢失 | 类型化 io 错误，恢复后完成 |
| 11 | 源副本同长翻转字节 / 截断 | 两者均 `source_changed_refuse_resume` |
| 12 | 复制完成后用户原文件被改，续跑 | 完成且 sha = 原文件的原生转换 |
| 13 | 复制中崩溃：worker 死（页存活）/ 页面刷新 | 前者任务立即丢弃；后者目录保留为 `staging`，首次清扫遇垂死 worker 锁→pending 记录，下次进入删除，续跑被拒 |
| 14 | 非 KFB/KFBF 文件 | `unsupported_input`，不产生任何任务目录 |
| 15 | 续跑时改档位/改策略 | `resume_refused`；原设置续跑成功 |
| 16 | core 版本漂移 | `resume_refused` |
| 17 | 双标签同时启动 | 第二页 `job_locked_other_tab` |
| 18 | 任务目录被删/驱逐 | `job_dir_missing` |
| 19–20 | 取消+清理失败 / 基本取消 | pending 记录→下次进入重试→目录消失；目录删除 |
| 21 | 反复 terminate+reload 混合（3 次崩溃后完成） | sha = 原生 |
| 22–24 | 荧光（稀疏+裁剪夹具）：中途崩溃 / finalize 崩溃 / reload | sha = 原生 |

崩溃点为确定性注入（worker testMode 钩子），页面侧 terminate/reload 为真实杀死。

## 7. 真实样本（别名 + 哈希）

| 别名 | 模态 | 浏览器 sha256 = 原生 | 转换 | 端到端（复制+probe+转换+校验） |
|---|---|---|---:|---:|
| KFB-1 | 明场 | `385a59c6…`（= C1 标定） | 2.1 s | — |
| KFBF-A | 荧光 | `6f8a1e3b…` | 9.4 s | 20.7 s |
| KFBF-B | 荧光 | `40cb12fa…` | 6.7 s | 13.9 s |
| KFBF-C | 荧光 | `5d6b1b21…` | 5.4 s | 11.7 s |
| KFBF-D | 荧光 | `872fe3b6…` | 8.5 s | 18.5 s |

导出往返 220 055 311 B（KFB-1）。报告与日志只含别名与哈希。

## 8. 门禁复现

见 `docs/review-evidence/slide-tools/C2/RERUN.md`。

## 9. 偏差与已知事项

1. **取消 = 终止 worker**（§4.6）。
2. **源复制占盘**：临时盘峰值 ≈ 源 + 输出（9.8 GiB 输入 19.6 GiB）；导出到 OPFS 另计一份。
3. **配额报告上限**：>~4.9 GiB 输入在新 profile 需用户确认（§4.7），C3 负责交互。
4. **FL finalizeValidate 不断言 IFD 数**：结构走查只走顶层链（SubIFD 挂 330 标签），sha256 与结构仍全量校验。
5. **续跑快进的采样检查**：已完成层只探测首个 full tile（副本已整体哈希复核）。
6. **resume wire JSON 为手写解析**（`resume.rs`，数据来自本机带校验和的 journal；为不给 wasm 引入 serde）。
7. **测试钩子**：故障注入仅在 `init{testMode:true}` 激活。

## 10. 外部门禁（未验证，如实标注）

- 真实/整机 4 GB、8 GB 设备（cgroup 模拟不替代）。
- Firefox（snap 故障）、Safari、Edge（OPFS 同步句柄、BYOB 流、导出能力需分别实测）。
- 真实配额耗尽；`persist()` 真实手势；`showSaveFilePicker` 写入用户磁盘。
- 移动端浏览器。
