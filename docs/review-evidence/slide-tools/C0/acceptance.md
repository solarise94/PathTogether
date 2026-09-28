# C0 验收记录（主代理审查，2026-09-29）

方案：[browser-slide-tools-and-baidu-plugin-agent-plan-20260929.md](../../../browser-slide-tools-and-baidu-plugin-agent-plan-20260929.md) §9.1。
子代理产物：[c0-inventory](../../../slide-tools/c0-inventory.md)、[c0-capability-tables.json](../../../slide-tools/c0-capability-tables.json)、[c0-adr-core](../../../slide-tools/c0-adr-core.md)、[c0-adr-browser](../../../slide-tools/c0-adr-browser.md)；spike 代码 `experiments/slide-tools-c0/`。

## 主代理独立复核

| 项 | 复核方式 | 结果 |
|---|---|---|
| ① 隐私 | grep docs/slide-tools 原始文件名/人员缩写 | 无泄漏（仅别名 + sha256） |
| ① 变体事实 | 自写脚本调用现有 parse_kfb/parse_kfbf | KFB-1 9 级、MPP 0.4841；KFBF-A 17 级 × 6 通道 |
| ① oracle 回落 | 统计 10 份 manifest 中 `edge_reencode_fallback_q95` | 0 次 |
| ② 单元测试 | `cargo test`（release 构建） | 20 passed |
| ② 真实样本差分 | 自写 tifffile 脚本逐 tile 比较 payload sha256（不用子代理脚本） | 9/9 页结构一致；31,650 完整 tile 全等；627 不等 = 边缘 tile 数；3 张关联图字节相等 |
| ③ >4 GiB 正确性 | cgroup 模拟 MemoryMax=4G 下重跑 4.5 GiB 作业（Chromium 151） | ok；420 MiB/s；2^32+12345 标记、回填、截断金丝雀均通过；391 次读 / 209 块写越过 4 GiB；未被杀 |
| ③ 内存 | 同上 | 进程树增量 558 MiB（节省档目标 512 MiB **未达**，与子代理结论一致） |

## 结论

C0 通过，进入 C1/C2，附带以下硬性要求：

1. **边缘 tile 质量不得劣于现有转换器**：合成夹具上 Rust 编码器边缘 tile 相对源的最大偏差 84–139，Pillow 为 61–97。C1 门禁：逐边缘 tile 相对源的 PSNR 不低于 oracle 同 tile PSNR − 0.5 dB，且全体最大绝对偏差不超过 oracle 最大值。
2. **jpeg-encoder 许可**含 IJG 条款，C1 须完成许可审查或替换。
3. **KFBF 输出**：`ExposureTime` 须按 OME 模型放在 Plane 上（Bio-Formats 重序列化会丢 Channel 上的值）；明场经典金字塔在 Bio-Formats 中不作为分辨率层暴露，C1 评估改为 SubIFD 金字塔或记录为互操作限制。
4. **channel.json**：作为可选伴随输入吸收显示窗口，本体优先。
5. **C2 内存**：必须做缓冲池，节省档进程树增量 ≤512 MiB 才算通过。
6. 外部门禁不变：真实 4/8 GB 设备、Firefox/Safari、真实 >4 GiB/MRXS/OME 样本、配额超限真实触发、`persist()`/`showSaveFilePicker` 手工验证。
