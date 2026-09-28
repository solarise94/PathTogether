# C1 共享转换核心 — 验收记录

日期：2026-09-29。验收方：主代理（review + 独立复跑）。被验收物：
[c1-core-report.md](../../../slide-tools/c1-core-report.md)、`slide-transform-core/`、
`scripts/build_slide_transform.sh`、`static/tools/slide-transform/`、
`tests/test_slide_transform_core.py`、`scripts/c1_biginput.py`。

结论：**C1 通过**（含验收中修正的 3 项，见 §3）。真实样本只以别名出现。

## 1. 独立复跑（不依赖实现方证据）

| 项 | 方法 | 结果 |
|---|---|---|
| Rust 单测 + 畸形输入 | `cargo test -p slide-transform-core --features fixtures` | 15 + 2（新增）+ 37 过；`--no-default-features` 构建过 |
| 差分 harness | `pytest tests/test_slide_transform_core.py` | 16 passed, 1 skipped（>4 GiB 为 opt-in） |
| >4 GiB 64 位偏移 | `C1_BIG=1 … ::test_over_4gib_both_writers` | 1 passed（229 s） |
| BF 整文件 | CLI 转 KFB-1 → sha256 | `385a59c6…` = C0 Python oracle 输出 |
| **wasm on 真实样本** | 自写 node 宿主（`scripts/wasm_kfb1.mjs`）以 committed wasm 转 KFB-1 | sha256 `385a59c6…`，与 oracle / 原生 CLI 整文件一致（实现方只在 300×300 合成样本上比过） |
| **FL 全 tile** | 自写 tifffile 对比（`scripts/fl_tile_parity.py`），逐 series/level/page/tile | KFBF-A..D 共 1 101 834 tiles **全部字节相等（含边缘 tile）**；102/102 页结构、axes=CYX、shape 一致。比报告宣称（仅 full tiles）更强 |
| wasm 可复现 | 修构建脚本后重建 | wasm sha256 前后不变（`4b5a5091…`） |

## 2. 手写 JPEG 编解码器的安全审查

背景：边缘 tile 的解码/重编码需与 Pillow 字节一致（C0 质量缺口的根治），
实现方手写了基线 JPEG 编解码器（decoder 1146 行、encoder 739 行），它解析
来自不可信切片文件的字节。

- `unsafe`：无。
- 变异 fuzz（`scripts/fuzz_seeds.rs`，种子 = 90 个真实 tile 的 JPEG 流，
  位翻转/截断/插标记/FF 填充等 6 类变异）：release 600 000 次 + debug
  （溢出检查开启）60 000 次：**panic 0、>0.5 s 0**，最慢 14 ms，峰值 RSS
  11 MB。
- 尺寸门：SOF 65535×65535 / 4096×4096 在分配前以类型化错误拒绝（µs 级）；
  0 尺寸拒绝。
- 已入仓回归：`crates/core/tests/jpeg_mutation.rs`（编码器自生成种子，
  debug 4 000 / release 40 000 次确定性变异 + 像素预算用例）。

## 3. 验收中修正的问题

1. **许可证陈述错误（已修）**：报告 §6 称 libjpeg-turbo 源码为「MIT-style
   IJG-free」且「未复制代码」，与 §4「verbatim port of jidctint.c…」自相
   矛盾；这些文件实为 IJG 许可。已改为按 IJG 派生代码处理：新增
   `slide-transform-core/NOTICE`（含 IJG 致谢语），构建时复制到
   `static/tools/slide-transform/NOTICE`；报告 §1/§6 与 manifest notes 更正。
   移除 jpeg-encoder 的真实理由是字节一致性，而不是规避 IJG。
2. **原生二进制进入 web 目录（已修）**：构建脚本把 1 MB Linux CLI 复制到
   `static/tools/slide-transform/`（会被公开服务并入仓）。已删除并改为不复制；
   CLI 只留在 `target/`（测试本就从那里取）。
3. **JPEG 无 fuzz 回归（已补）**：见 §2。

## 4. 转交 C2 的已知事项（不阻塞 C1）

- `runner.js` 是 C1 占位：同步读桥（`_syncRead`）未实现，大文件须由 C2 的
  OPFS `FileSystemSyncAccessHandle` 变体替换；C2 必须重写并接故障矩阵。
- C0 复跑在 4G cgroup 下进程树增量 558 MiB，超 512 MiB 目标——C2 需缓冲池化，
  并在浏览器内复测（本次 node 宿主 RSS 490 MB 主要是整体载入的 220 MB 源，
  不代表浏览器形态）。
- `encode_gray` 取 zigzag 量化表，而 `EncoderCfg`/`encode_rgb` 取自然序——
  API 口径不一，C2 触及时统一（P3）。
- 手写 codec 与「优先成熟组件」偏好的取舍需用户知悉：备选是把
  libjpeg-turbo 以 emscripten 编译进 wasm（字节一致天然成立，但引入 C 工具链、
  wasm32-unknown-unknown 目标不适用、构建复杂度显著上升）。验收方建议保留
  手写 codec：已有 960/960 双向对拍、全样本字节一致与上述 fuzz 证据。

## 5. 复跑命令

见 `.gate-tmp/slide-tools-c1/RERUN.md`（实现方）与本目录 `scripts/`：

    PATH=$HOME/.cargo/bin:$PATH node docs/review-evidence/slide-tools/C1/scripts/wasm_kfb1.mjs <样本目录> /tmp/k.tif
    TMPDIR=$PWD/.gate-tmp .venv/bin/python docs/review-evidence/slide-tools/C1/scripts/fl_tile_parity.py <输出目录>
    # fuzz：把 fuzz_seeds.rs 放进 crate 副本的 crates/core/examples/，
    # cargo run --release --example fuzz_seeds -- <种子目录> 600000
