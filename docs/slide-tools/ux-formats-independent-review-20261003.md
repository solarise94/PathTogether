# ux-formats 独立 review

日期：2026-10-03。审查提交：`8cd6427`，比较基线 `7aa4d88`（线上 rc6）。

**结论：当前整包暂不建议发布。** 本轮确认两项 MRXS 核心缺陷和一项验收门禁缺陷。修复后需复跑对应负例及恢复链路；MRXS 低倍整数拼接的精度限制也不能仅以已写报告作为接受。真实 COS/XHR 验收、compact 盲评继续待验。

本轮只检查代码、运行本地小型合成负例并新增此报告；没有修改应用代码，没有推送、合并或部署，也没有创建生产上传任务。未重跑大型样本或全量 pytest，不将交接报告的计数写成自己的复测结果。

## 1. P1：MRXS 探测/索引分配不受资源档位约束

位置：`slide-transform-core/crates/core/src/mirax.rs:754`、`:781`；同类风险在 `convert_mirax.rs:216` 的完整画布 tile 数组和后续 clone。

`IMAGENUMBER_X/Y`、基准尺寸及总 placement 的固定数量上限，并不限制实际内存字节。没有位置表时，探测直接分配 `npositions` 个 `(i64,i64)`；这发生在 tile 转换/checkpoint 之前。浏览器资源检查只在 checkpoint 检查 wasm heap，probe-bundle 没有等价的提前预算判断。

### 复现（已执行）

1. 候选交付 CLI 生成 8×6、两层的合成 MRXS，五个成员合计约 51 KB。
2. 复制到独立 scratch，INI 改为 5000×5000、CameraImageDivisionsPerSide=1，并移除可识别的 VIMSLIDE_POSITION_BUFFER 声明以走已支持的 nominal fallback。图像成员和索引不扩大。
3. 这些数值均在当前 parser 明确允许的上限以内；2500 万位置需要约 400 MB 的元组存储，尚不含其他结构。
4. 用只限制子进程的 128 MiB cgroup 跑 `probe`：signal 9 / exit 137。
5. 再用 192 MiB（saver 的整体预算）独立 service 验证：`result: oom-kill`、`status=9/KILL`、`Memory peak: 192M`，约 127 ms 被终止；未返回资源不足的类型化错误。

运行示例（scratch 中的输入已生成）：

```bash
systemd-run --user --wait --pipe \
  -p MemoryMax=192M -p MemorySwapMax=0 \
  slide-transform-core/target/release/slide-transform probe \
  "$PWD/.gate-tmp/independent-ux-review/large-grid/synthetic.mrxs"
```

这是共享核心的 **native 探测负例**，不是在真实 4 GB 设备上观察的浏览器 OOM。源码表明同一分配也在 WASM probe 路径执行；本轮没有用它把浏览器进程推到 OOM。

### 必须修复

probe/convert 传入实际资源预算；在位置、图像记录、placement 排序临时副本、CSR counts/starts/fill/items 和 JPEG 解码之前检查 checked 字节估算。超预算时分页/溢写或明确拒绝，不能等分配成功及第一 checkpoint 后再查。大单张 JPEG 也要按解码像素预算拒绝。新负例必须在 saver 预算内返回稳定错误，而非进程终止。

## 2. P1：包 manifest 没有绑定原任务身份，换源仍可续跑成功

位置：`static/tools/slide-transform/runner.js:902`；`worker.js:975`。

单文件路径比较实际长度/sha256 与 `record.identity`。MRXS 路径只调用 verify-bundle；worker 用当前 manifest 中的 size/hash 检查当前成员，返回 verified 数量。没有重新计算根摘要并与原 `record.identity.sha256` / 保存的 bundleManifest / journal identity 比较。

因此替换包目录及其自洽 manifest 可以绕过身份检查；旧的已提交输出和新的源输入不再属于同一源。成员检查本身正确，也无法防止这个问题。

### 复现（真实 runner/worker/WASM，已执行）

- 准备合成包，`crashAtWrite:4` 中断产生 journal，终止 worker。
- 修改 OPFS `Slidedat.ini` 的 objective 20 → 40（合法元数据、几何不变），更新包内 manifest 的成员摘要及 rootDigest。
- **不修改 job record 或 journal**。确认新的根摘要与原任务摘要不同。
- 请求续跑：`refused=false`。
- 等待本次新产生的 done-summary，返回 `ok=true, phase=validated`，持久状态 **ready**，输出 50,810 B。
- 完成产物 sha256：`e28444d7bd995f1b35ac436e310062f0904c3f9261a4d694d18fb5b8306c8c3e`。

该复现证明源身份不一致仍能通过，未声称这个特定元数据负例已经造成像素拼接损坏。替换实际像素成员会使旧 checkpoint 继续读取另一份源，是需要防止的恢复风险。

复现脚本：`.gate-tmp/independent-ux-review/check-bundle-identity.cjs`。只使用合成输入与本地 C2 server，结果记录在同目录 `bundle-identity-result.json`。

### 必须修复

verify-bundle 接收原任务期望身份，重新计算/验证规范清单、成员数量、总长度和根摘要，与 job 和 journal 固定身份一致后才能续跑；不能把被验证 manifest 的自报摘要当作期望值。保持旧任务兼容规则及 source_changed 错误合同。新增“包与 manifest 一起改变，但 job/journal 未改变”的拒绝负例；正确源的中断恢复仍须与 uninterrupted 字节一致。

## 3. P2：MRXS ground-truth 门禁会把缺失或空 ROI 判为零误差通过

位置：`slide-transform-core/crates/core/tests/mirax.rs:772`、`:792`、`:808`。

- 参考 raw 文件不存在时直接 continue。
- 无 mask 时使用 zip，只比较较短输入，不检查 `w*h*3`。
- 全零/空 mask 跳过；没有要求实际比较过 L0、低倍或每一目标层。
- 两个 worst 初始为 0，最终只有误差阈值断言。

### 两种负例（均已执行）

显式设置 MRXS_GT、MRXS_SAMPLES 和 MRXS_GT_STEM，提供可打开的合成 source，rois.json 列出一个 L0 和一个 L1 的 16×16 ROI：

1. **两份 raw 都不存在**：PASS，日志 `worst L0 mean 0.0000, worst L1+ mean 0.0000`，exit 0。
2. **两份 raw 都是 0 bytes**：PASS，exit 0。

对应测试名 `real_sample_composition_vs_openslide_ground_truth`。未设置 opt-in 样本时主动 skip 是另一件事；这里材料路径已明确配置，却仍把不完整验收视为通过。

这不证明交接报告已有的真实 ROI 有误，只证明当前 gate 无法拒绝坏材料。需检查所有参考/输出字节数、mask 长度和有效组织覆盖、区域边界及目标层覆盖，缺材料必须非零失败。补负例后，按严格 gate 重新跑已报告的真实 ROI。

## 4. MRXS 几何精度：待解决/接受的设计限制

`mirax.rs:970` 将真实 fractional destination 和 source offset 取整；`convert_mirax.rs` 以整数矩形覆盖。交接报告记录低倍层与 OpenSlide 的平均差异 8–16；本轮确认代码确实执行这种舍入，没有重新测该公开样本数值。

MIRAX 的降采样数据可能包含非整数宽度的拼接重叠，属于几何处理而非单纯 JPEG 色差。[OpenSlide MIRAX 格式说明](https://openslide.org/formats/mirax/)

“L0 参考 ROI 对齐”与“所有层保留几何/跨层一致性”不能互相替代。当前宽松均值阈值也无法证明没有局部错位或接缝。建议实现 fractional composition，或重新设计保持 L0 坐标的预览金字塔；参数/算法需要版本化。若保留当前算法，先明确验证局部接缝、跨层点位和误差合同，并获得该精度限制的产品接受；在此之前不把 F3 描述为已完全验收。

## 5. 本轮独立通过的检查

- 5 个相关 vitest 文件，**122/122**：共享上传器、工具上传反馈、画质记录、MRXS 输入识别、简化页面。串行执行，1 GiB cgroup 限制。
- build-manifest 六个产物摘要全部匹配：WASM、生成 JS、runner、engine、worker、native CLI。负例使用的 native CLI 与候选 manifest 对应。
- bundle 身份负例通过 C2 真 runner/worker/WASM 完成；并非只 mock 验证函数。
- 以上通过不能代替真实 COS XHR 请求、真实设备或用户盲评。本轮未再次跑全量 pytest/Rust/browser、临床样本或真实系统保存对话框。

## 6. 收口建议

1. 先修 §1/§2/§3，并保存各负例旧代码失败、新代码通过的证据；§3 修复后重测真实 MRXS ROI。
2. MRXS 的低倍几何合同解决/接受前，保持为候选能力；可以单独准备 U1/U2/U3/F1 发布，但需真实隔离或功能禁用，不能只把 MRXS 按钮藏起来而其他入口仍可使用。
3. 真实 COS 小文件候选验收应覆盖 XHR 请求、响应成功判定、进度、取消/续传和最终 remote cleanup；只有预检不算通过。
4. compact 继续非默认，盲评尚未完成不提高推荐等级；“更小”文案不保证每种输入都会缩小。
5. Windows、OS 保存对话框、真实低内存设备按交接清单保留未验证；AI session 403 另行修复。
6. 更新交接报告状态为“实现已提交，独立 review 有待修项，发布 gate 待验”。本轮没有授权或执行部署。
