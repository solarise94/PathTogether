# C0 ADR：浏览器 IO / 内存 spike（>4 GiB OPFS 随机写 + WASM 管线）

日期：2026-09-29。阶段：C0 ③（浏览器 spike）。对照计划：`docs/browser-slide-tools-and-baidu-plugin-agent-plan-20260929.md` §1.2、§1.3、§3、§4、§5、§6 CSP 条目、§10.2、§10.3。
代码：`experiments/slide-tools-c0/browser-spike/`（可复跑，见其 README）。原始日志：`.gate-tmp/slide-tools-c0/browser/logs/`（复跑命令见同目录上级 `RERUN.md`）。
基线：PT HEAD `6b43656`；本阶段只新增文件，未改动任何已跟踪文件、未提交。

## 0. 结论摘要

1. **>4 GiB 随机写正确性成立**：OPFS `FileSystemSyncAccessHandle` 以 1–4 MiB 有界块写出 4.5 GiB（及 12 GiB）文件，含 2^32+12345 高位回写与 4096 低位 backpatch；重开句柄后按字节比对抽样全部一致，WASM FNV-1a64 与独立 JS 实现交叉核对一致，截断金丝雀证明全链路无 32 位截断。
2. **吞吐**：4.5 GiB 写入 447 MiB/s（无约束 Chromium）；cgroup 模拟 4 GB 下 245–412 MiB/s（4 次运行区间）；导出代理（OPFS→FileSystemWritableFileStream 流式复制）198–370 MiB/s。
3. **内存（关键发现）**：进程树 RSS 增量 ≈ 在途窗口的 1.9–3.1 倍（192 MiB 窗口 → 增量 530–605 MiB）。spike 的朴素逐块 `new Uint8Array` + 转移缓冲策略**不满足** §4 节省档「进程树增量 ≤512 MiB」目标；C2 必须引入缓冲池/显式回收。作业规模无关性满足（4.5 GiB 与 12 GiB 增量 605 vs 601 MiB，§4 「≤20% 增长」远超满足）。
4. **cgroup 模拟可行**：`systemd-run --user --scope -p MemoryMax=4G -p MemorySwapMax=0` 在本机真实生效（200 MiB 上限内申请 600 MiB 的进程被 SIGKILL，exit 137）。4 GB 下作业全部完成，live 采样 `oom=0 oom_kill=0`；`memory.current` 峰值贴到上限（页缓存占满、内核回收而非 OOM）。所有此类结果**仅标注「cgroup 模拟」**，不等价于 4 GB 整机验收（§1.2/§10.2 外部门禁维持）。
5. **最小 CSP**：`default-src 'self'; script-src 'self' 'wasm-unsafe-eval'` 即可全量通过（Worker 加载 + WASM 编译 + wasm fetch）。实测 `worker-src` 在 `script-src 'self'` 存在时可省（回退链生效），但建议显式声明。**COOP/COEP 不需要**（全部无隔离头运行通过，`crossOriginIsolated=false`；非多线程设计不依赖 SharedArrayBuffer）。
6. **配额**：`storage.estimate()` 初始 quota=10 GiB，随用量动态上调（用 9 GiB → quota 19 GiB；用 12 GiB → 22 GiB；磁盘 757 GB 空闲）。本机**无法**触发 quota 超限（除非灌满磁盘），列为外部门禁；写入/读取错误路径已在代码中就位。`persist()` 在无用户手势的自动化环境一律返回 `false`——真实交互下的授权率需人工验证。
7. **导出**：OPFS→OPFS 第二文件的 `FileSystemWritableFileStream` 流式复制（4 MiB 单缓冲）验证通过，无整文件 ArrayBuffer/Blob 物化。`showSaveFilePicker` 到用户磁盘的最后一跳无法无头验证，列为人工项。
8. **终止/刷新**：`worker.terminate()` 与页面 reload 后 OPFS 文件保留，已写内容可验证（chunk0 逐字节一致）；观察到一次 reload 后立即 `getFile()` 读返回 `NotReadableError`（垂死 worker 的同步句柄锁未及时释放），恢复路径必须带退避重试。journal/checkpoint 必须记录的量见 §7。

## 1. 被测系统

- Rust→WASM（wasm-bindgen `=0.2.129`，rustc 1.98.1，`rust-toolchain.toml` 固定 1.98.1，无线程/无 SAB）：`ChunkProcessor::process_into(src, dst) -> u64`。数据路径复制两次经 WASM 线性内存（wasm-bindgen 胶水拷入 + 拷出）+ WASM 内 staging→out 变换（rotl8 3 ^ 0x5A）与 FNV-1a64 校验。WASM 堆固定 2×chunkMax=8 MiB。
- 两个 module Worker：`io-worker`（持有 File，`File.slice().arrayBuffer()` 有界读，**transferable ArrayBuffer** 发往 compute）+ `compute-worker`（WASM 实例化、OPFS 同步句柄随机写）。字节预算的**在途窗口**（192/384 MiB）限制未完成读请求总量（背压）。
- 输入：4,800,000,000 字节（4.47 GiB）确定性合成文件（word 级 hash 模式，生成器/验证器/Rust 三方共享同一字节合同），Playwright `setInputFiles` 注入。
- 输出：4.5 GiB（4,831,838,208 B）/ 12 GiB 补充运行；输出块源偏移一半偏置到文件顶部三分之一（每次运行 391/1855 块源偏移 >2^32；chunk 7 固定从 2^32+2048 读）。
- 内存采样：node 侧每 400 ms 遍历 `/proc/<pid>/task/*/children` 递归收集浏览器进程树，求 `statm` resident 之和（RSS，跨进程共享页重复计入，偏保守）。基线 = 同浏览器空白页 5 s 均值。

## 2. 浏览器/版本矩阵

| 浏览器 | 版本 | 形态 | 结果 |
|---|---|---|---|
| Playwright Chromium（chromium-1234 headless shell） | 151.0.7922.34 | 无头 | 全部通过（正确性/内存/cgroup/CSP） |
| Google Chrome（channel "chrome"） | 153.0.8010.47 | 无头 | 全部通过 |
| Google Chrome | 153.0.8010.47 | 有头（DISPLAY=:0） | 全部通过 |
| Firefox | — | — | **未验证**（本机 snap 权限故障，见 §1.2 计划约束） |
| Safari | — | — | **未验证**（无 macOS 设备） |
| Edge | — | — | 未验证（本机未装，外部门禁） |

注：Playwright 1.62.1。给 `launchPersistentContext` 传 `env`（即使是 process.env 原样拷贝）会使 Chrome stable 启动即自关（"Target page … closed"，优雅关闭日志）——已从 runner 移除 env 覆盖并记录；Chromium headless shell 不受影响。

## 3. 吞吐（4.5 GiB 顺序写 + 采样校验 + 4.5 GiB 导出代理）

| 运行 | 浏览器/约束 | 窗口 | 写入 MiB/s | 导出 MiB/s |
|---|---|---|---:|---:|
| full-192 | Chromium 无约束 | 192 MiB | 447.7 | 335.4 |
| full-384 | Chromium 无约束 | 384 MiB | 436.7 | 263.0 |
| chrome-192 | Chrome 153 无头 | 192 MiB | 434.8 | 261.5 |
| chrome-headed-192 | Chrome 153 有头 | 192 MiB | 237.0 | 225.5 |
| cg4g-192 / cg4gb / cg4gc / cg4gd | Chromium **cgroup 模拟 4G**（MemoryMax=4G, SwapMax=0） | 192 MiB | 245.2 / 285.1 / 411.5 / 386.8 | 197.9 / 203.6 / 370.3 / 351.2 |
| cg8g-384 | Chromium **cgroup 模拟 8G** | 384 MiB | 268.4 | 256.0 |
| quota-chase-12g | Chromium 无约束（12 GiB 输出） | 192 MiB | 433.6 | — |

- 写入路径含 2 次跨 WASM 内存复制 + JS→Worker 转移；仍达 435–450 MiB/s（NVMe、页缓存命中输入）。
- 12 GiB 与 4.5 GiB 吞吐持平（433.6 vs 447.7）：规模无关。
- cgroup 4G 下方差大（245–412）：`memory.current` 顶到上限，内核回收页缓存产生周期性停顿；无 OOM。
- 校验阶段（约 23×~2.5 MiB 读 + 字节比对 + 1 次 4 MiB BigInt FNV）≈1 s。
- WASM 准备（fetch+instantiate）<100 ms（phase 记录 `wasm-ready`）。

## 4. 进程树内存（RSS 峰值相对空白页基线的增量）

| 运行 | 基线均值 | 作业峰值 | **增量** | 窗口 | 增量/窗口 |
|---|---:|---:|---:|---:|---:|
| full-192 | 433 MiB | 1038 MiB | **605 MiB** | 192 | 3.1× |
| full-384 | 433 MiB | 1178 MiB | **745 MiB** | 384 | 1.9× |
| chrome-192 | 944 MiB | 1525 MiB | **581 MiB** | 192 | 3.0× |
| chrome-headed-192 | 1065 MiB | 1657 MiB | **592 MiB** | 192 | 3.1× |
| cg4g-192（4 次区间） | 430–435 | 965–1035 | **530–605** | 192 | 2.8–3.1× |
| cg8g-384 | 433 MiB | 1205 MiB | **772 MiB** | 384 | 2.0× |
| quota-chase-12g | 431 MiB | 1031 MiB | **601 MiB** | 192 | 3.1× |
| smoke（256 MiB 作业） | 434 MiB | 544 MiB | 110 MiB | 32 | 3.4× |

- 峰值增量集中在页面所在 renderer（103→671 MiB）：io/compute worker 均驻留该进程。
- **判定**：非窗口固定开销 ≈ 380–410 MiB（逐块 `new Uint8Array` 分配 + V8 ArrayBuffer 外部内存池 + OPFS 写缓冲）。朴素实现下 192 MiB 窗口总增量 605 MiB，**超出 §4 节省档验收线 512 MiB**；384 MiB 窗口 745 MiB，在均衡档 ≤1 GiB 线内但余量小。C2 需要：按尺寸分级复用缓冲池、写后显式置空引用、（必要时）缩小窗口并分代 GC。spike 证明的是测量方法与量级，不是最终预算。
- 基线本身：Chromium 无头 6 进程 433 MiB；Chrome 153 无头 9 进程 944 MiB；有头 1065 MiB。引擎外开销显著，验收必须以「同浏览器空白工具页」为基线（§4 已如此规定）。

## 5. >4 GiB 正确性证据（每个 full 运行均含，seed=42 可复现）

- 最终尺寸精确等于目标（4,831,838,208 / 12,884,901,888）；重开句柄 `getSize()` 一致。
- 抽样 23 块（含首块、末块、max 源偏移块、max 目标偏移块、等距 20 点）**逐字节**等于按确定性合同重算的期望输出；1 块额外做 JS BigInt FNV-1a64 == WASM 返回值交叉核对。
- 高位标记：主写完成后在 **2^32+12345** 写 4096 B 标记 → 重开后在该偏移读回一致；同时在**绝对偏移 12345** 读 16 B 确认**不含**该标记（若任何路径发生 32 位截断，高位写会落到 12345）——金丝雀干净（`truncationCanaryClean=true`）。
- 低位 backpatch：4096 偏移重写 4096 B，重开后覆盖内容正确。
- 偏移全部以 Number 安全整数（<2^53）传递并在边界 `assertSafeOffset` 断言；`File.slice`、`write({at})`、`read({at})` 三处一致。源读偏移 >2^32 共 391 块、目标写 >2^32 共 209 块（4.5 GiB 运行）。
- 超出 EOF 的写在 OPFS 中稀疏扩展（smoke 运行 256 MiB 作业因高位标记扩展到 4 GiB+，行为符合预期）。

## 6. cgroup 结论（标注：cgroup 模拟）

- 可用机制：`systemd-run --user --scope -p MemoryMax=4G -p MemorySwapMax=0 <cmd>`（user slice 已委托 memory 控制器）。**强制力验证**：MemoryMax=200M 下分配 600 MiB 的 python 进程被 SIGKILL（exit 137）。
- 4 GB：4 次完整 4.5 GiB 作业（含导出）全部 `ok=true`、runner exit 0、进程树存活；live 采样 `memory.events` 全程 `oom 0 oom_kill 0`；`memory.current` 峰值 4,294,922,240 ≈ 上限（差 44 KiB），即靠页缓存回收贴限运行而非 OOM。RSS 树峰值 965–1035 MiB。
- 8 GB（384 MiB 窗口）：完成，systemd 记录 Memory peak 6.1G（含页缓存）、swap 0。
- `--scope` 退出即清理 cgroup：需要 `oom_kill` 证据时用 transient **service**（`systemd-run --user --wait --remain-after-exit`，注意 `--wait`+`RemainAfterExit` 会永久阻塞，读完计数后手动 stop）或运行中采样（本次采用后者，日志 `cg4gd-cgroup-samples.log`）。
- 局限（如实声明）：node runner 与浏览器同 scope（runner RSS ~150 MiB 计入）；这是进程树级模拟，不是整机 4 GB 验收。

## 7. 终止 / 刷新 / 恢复语义（C2 checkpoint 设计输入）

- `worker.terminate()`（1.5 GiB 处杀两个 worker）：OPFS 文件保留，`getFile().size` = 1,611,331,910 B，恰好等于最后一条已收到记录的写边界；chunk0（固定 s=0/t=0/4 MiB）逐字节可验证。→ **terminate 后文件长度反映已发出的 write()，而非「已提交」语义**；journal 必须自行记录已提交长度。
- 页面 reload（1.5 GiB 处）：文件保留（1,728,053,640 B ≥ 阈值），chunk0 一致；28 ms 后即可读。但**首次尝试时** reload 后立即 `getFile().slice().arrayBuffer()` 抛 `NotReadableError`（垂死 worker 的同步句柄释放竞态）——恢复路径必须带退避重试（已实现：20×500 ms）。
- 一次 terminate 尝试中 renderer 目标直接关闭（页失联）未复现——C2 故障注入矩阵需包含重复 terminate/杀进程混合场景。
- checkpoint/journal 必须记录（C2 落地）：已提交字节数（独立于文件长度）、逐块 (目标偏移, 长度, FNV-64)、写入代次/序号、core/profile 版本、输入身份；恢复时以记录为准截断/校验，不信任 size。

## 8. storage.estimate / persist / 配额

| 时点 | quota | usage |
|---|---:|---:|
| 新 profile（Chromium/Chrome 一致） | 10.0 GiB | 0 |
| 写入 9 GiB（4.5 输出+4.5 导出）后 | 19.0 GiB | 9.0 GiB |
| 写入 12 GiB 后 | 22.0 GiB | 12.0 GiB |

- quota 随用量/空闲磁盘动态上调；本机（757 GB 空闲）**触发不了超限**。超限错误路径（读/写捕获→fatal 上报）已就位但未被真实触发——列为外部门禁（灌满磁盘或低空间设备）。
- `navigator.storage.persist()` 自动化环境（无用户手势）一律 `false`（Chromium 与 Chrome 153 一致）。真实手势下能否授予、以及「持久化被拒时的恢复边界文案」需人工验证（§5 计划要求）。

## 9. 导出路径

- 代理实现：源 OPFS 同步句柄 4 MiB 复用缓冲读 → `createWritable()` 的 `FileSystemWritableFileStream.write({type:'write', position, data})` 顺序流写 → `close()`。与用户磁盘保存共用同一 WritableStream 接口；无整文件 ArrayBuffer/Blob 物化（最大常驻缓冲 4 MiB）。
- 结果：尺寸精确、首尾抽样一致，198–370 MiB/s。
- **人工项**：`showSaveFilePicker` 拾取真实用户文件、写满磁盘/可移动盘的中途失败语义、下载栏替代路径。不支持该 API 的浏览器上大文件导出必须禁用（§5），不得回落整文件下载。

## 10. CSP 最小集（实测）

服务端对**所有响应**（HTML、worker JS、wasm）下发同一 CSP；worker 自身响应头即其环境策略。

| 模式 | 策略 | 结果 |
|---|---|---|
| A | `default-src 'self'` | Worker 加载 OK；**WASM 编译被拒**（CompileError，页面与 worker 皆然） |
| B | `default-src 'self'; script-src 'self' 'wasm-unsafe-eval'` | **全量通过**（写/校验/导出） |
| C | `default-src 'none'; script-src 'self' 'wasm-unsafe-eval'; worker-src 'self'; connect-src 'self'; style-src 'self'; img-src 'self'; base-uri 'none'; object-src 'none'; frame-ancestors 'none'` | 全量通过（推荐基线：显式、最小面） |
| D | C 去掉 `worker-src` | **通过**（worker-src 回退到 script-src；显式声明仍建议保留作硬ening 与未来变更保险） |
| E | `default-src 'none'; script-src 'self' 'wasm-unsafe-eval'` | WASM **fetch 被拒**（connect-src 回退 default-src 'none'） |

- **最小必需**：`script-src` 含 `'wasm-unsafe-eval'`，且 connect（或 default）允许同源 wasm fetch。工具页推荐 C。
- **COOP/COEP 不需要**：全部主矩阵在无隔离头下通过（`crossOriginIsolated=false`），非多线程设计不依赖 SAB；附带 `--coop` 变体同样通过（隔离生效），仅作记录。若未来引入 wasm-vips 多线程需重测（§3/§6 计划约束）。

## 11. 未验证 / 外部门禁清单

- Firefox（本机 snap 故障）、Safari、Edge：未验证。
- 真实/整机 4 GB 与 8 GB 设备（cgroup 模拟不替代）。
- quota 超限真实触发；`persist()` 带用户手势行为。
- `showSaveFilePicker` 真实用户文件导出（含失败语义）。
- 真实 >4 GiB 厂商样本（本次全部合成文件；格式门禁仍需真实样本，§10.1）。
- 移动端浏览器。

## 12. 对 C2 的建议

1. 缓冲池化（复用固定尺寸 ArrayBuffer、避免逐块分配）是满足 §4 512 MiB/1 GiB 进程树增量的首要工程项；spike 数据（3.1× 窗口）是基线对照。
2. IO/compute Worker 分离拓扑可行（MessageChannel + transferable），窗口字节预算背压有效，直接沿用。
3. checkpoint 按 Sizes §7 记录；恢复路径读 OPFS 需带退避重试（NotReadableError 竞态实测存在）。
4. 工具页 CSP 采用模式 C；不需要 COOP/COEP（除非引入多线程 WASM）。
5. cgroup 复跑脚本沿用本 spike 的 systemd-run 用法；报告标注「cgroup 模拟」。
