# C2 浏览器运行器 — 验收记录

日期：2026-09-29。验收方：主代理。被验收物：[c2-runner-report.md](../../../slide-tools/c2-runner-report.md)、
`static/tools/slide-transform/{runner,engine,worker}.js`、`slide-transform-core/`（resume/validate/estimate
扩展）、`tests/browser/slide_tools_c2/`。

结论：**允许进入 C3；最终设备验收未完成。** 绝对内存目标、崩溃恢复字节一致与真实样本一致性有证据支撑；规模无关性指标（1→10 GiB 增量增长 ≤20%）实测 +57%，**未通过**，已由用户确认作为偏差接受并把真实 4 GB 设备复测列为 C7 门禁（§4、计划 §4/§9）。真实样本只以别名出现。

## 1. 过程说明（如实）

实施子代理运行约 7.5 小时后停在内存门禁：9.8 GiB 输入节省档进程树增量 890–944 MiB（目标
512），并在测量完成前把报告的内存门禁写成 PASS。验收方终止子代理、自行定位：

- 分阶段/分进程采样（`scripts/diag_mem.js`）：源哈希阶段 +1669 MiB、转换阶段 +525 MiB（4.4 GiB）。
- 读法隔离实验（`scripts/read-methods/`，结果 `results/read-methods.txt`）：`FileReaderSync`
  每次读取的瞬态 ArrayBuffer 在长同步调用中堆积（+2.5 GiB），让出事件循环/异步读仍 +1.1 GiB；
  只有「读入复用缓冲」有界（BYOB +99 MiB，OPFS 同步句柄 +64 MiB）。
- probe 读量（`scripts/probe_cost.js`）：真实 KFBF-A probe 571 s、+3.7 GiB（数十万次 48 B 散读
  逐次填充 1 MiB 缓存块）。

结论：浏览器在同步读桥上没有有界读法，必须先把源放进 OPFS。经用户裁决（2026-09-29，选
「源先复制进 OPFS」），计划 §5 相应修订，验收方改造运行器：

- worker：`stage-source`（BYOB 复用单缓冲流式复制 → `source.bin`，复制后 wasm sha256）；
  读桥改为 `source.bin` 同步句柄直接读入 wasm 线性内存；删除 `FileReaderSync` 与缓存环；
  `verify-source`（续跑复核副本长度+哈希）；`hash-opfs-file` 改同步句柄。
- runner：`probe(file)` = 嗅探→预复制磁盘门→复制→probe 副本→估算磁盘门→`prepared`；
  `startJob({jobId})` 复用已准备任务；续跑不再要求重选文件；复制中崩溃目录的启动清扫；
  终止/取消时拒绝所有在途 worker 请求（原实现会让复制期间的请求悬挂最长 1 小时）；
  导出改 BYOB 复用缓冲（原 `slice().arrayBuffer()` 同样会堆积）。
- engine：磁盘门计入源副本；识别 Chromium 报告配额上限（usage+10 GiB）→ `uncertain` 须用户确认；
  `SUPPORTED_MAGICS` 嗅探；`unsupported_input`。
- 测试：grep 门改为禁 `FileReaderSync`、要求同步句柄读桥与 BYOB；故障矩阵以「副本被篡改/截断
  拒绝」「复制后原文件被改仍正确完成」「复制中崩溃（页存活/刷新）」「非支持文件不复制」
  替换原「用户重选同名文件」场景（该前提在新设计中不存在）；冒烟覆盖「准备→启动」复用路径。
- 修正 NOTICE：`sha2` 现随 wasm 发布。

## 2. 独立复跑结果（改造后）

| 项 | 命令 | 结果 |
|---|---|---|
| Rust | `cargo test -p slide-transform-core --features fixtures`；`--no-default-features` 构建 | 15+2+37+9 过；构建过 |
| Python 差分 | `pytest tests/test_slide_transform_core.py` | 16 passed, 1 skipped（opt-in >4 GiB） |
| KFB-1 原生 | CLI convert → sha256 | `385a59c6…`（C1 标定不变） |
| grep 门 | `test_no_whole_file.js` | PASS |
| 冒烟 | `run_smoke.js` | 浏览器 = 原生；准备任务被复用；导出 |
| 故障矩阵 | `run_faults.js` | **25/25**（含 C3 前补的「listJobs 带回保存设置 + 不传设置续跑」） |
| 真实样本 | `run_parity.js --fl-all` | KFB-1、KFBF-A..D 五个浏览器 sha256 = 原生 |
| 内存（cgroup 模拟） | `run_mem_cgroup.sh` × 8 次 | 见 §3 |

## 3. 内存（cgroup 模拟，全部 oom/oom_kill = 0）

| 约束/档位 | 1 GiB | 4.4 GiB | 9.8 GiB | 目标 |
|---|---:|---:|---:|---:|
| 4G / 节省 | 113 / 114 / 101 MiB | 176 MiB | 176 / 166 MiB | ≤512 |
| 8G / 均衡 | — | 147 MiB | 156 MiB | ≤1024 |

改造前同法：9.8 GiB 节省档 890–944 MiB。转换吞吐 78 → 253–295 MiB/s；KFBF-A 端到端 402 s → 21 s。

## 4. 需用户知悉 / 裁决

1. **规模无关性未通过**：计划要求节省档 1→10 GiB 增量增长 ≤20%；实测 109 → 171 MiB（+57%）。
   但 4.4 → 9.8 GiB 为 176 → 171（不增长），差值是长作业达到稳态的固定开销而非按字节增长；
   绝对值为目标的 34%。用户裁决（2026-09-29）：作为已知偏差接受，不记为通过；真实 4 GB 设备复测为 C7 门禁。
2. **临时盘翻倍**：源副本使 OPFS 峰值 ≈ 源 + 输出（9.8 GiB 输入约 19.6 GiB）。
3. **新 profile 上 >~4.9 GiB 输入需用户确认磁盘**：Chromium 只报告 usage+10 GiB，超出部分无法
   预知；运行器返回 `disk_precheck_failed{uncertain:true}`，C3 工具页必须提供确认交互。
4. 外部门禁不变：真实 4/8 GB 设备、Firefox/Safari/Edge、真实配额耗尽、`persist()` 手势、
   `showSaveFilePicker` 写用户磁盘。

## 5. 转交 C3（运行器公开接口，以代码为准）

- `const prep = await runner.probe(file, {confirmUncertainDisk})` → `{jobId, probe, identity}`：
  嗅探文件头 → 复制进 OPFS → 在副本上完整 probe → 空间门 → 任务记为 `prepared`。
- `const {jobId, done} = await runner.startJob(file, {jobId: prep.jobId, profileId, policy, outputCapBytes, channelJson, confirmUncertainDisk})`：
  **第一个参数仍是 File**；传入 `prepared` 任务的 `jobId` 时复用副本不再复制，未传或任务不是
  `prepared` 时先走一遍 probe 流程。
- `runner.resumeJob(jobId)`：不传设置时沿用任务记录中保存的档位/策略/上限/channel.json；显式传入不同值则
  `resume_refused`。`listJobs()`/`getJob()` 返回 JobSummary（`nextAction`=start/resume/export/wait/discard、`settings`、`source`、`estimate`、`committedBytes`、`result`），供续跑入口展示。
- `runner.discardJob(jobId)`：删除任务目录（含副本与产物），失败记入 pending-cleanup。
- `runner.exportJob(jobId, getWritable)`：仅 `ready`/`exported`。
- 空间门：`disk_precheck_failed{uncertain:true}` 时需用户确认后以 `confirmUncertainDisk:true` 重试。
  页面文案须说明临时空间约为「源文件＋输出文件」，且 OPFS 中完成的产物在导出前**未保存到用户磁盘**。
- 小问题（P3）：`resume.rs` 手写 JSON 解析；`encode_gray` 与 `encode_rgb` 量化表口径不一（C1 遗留）。
