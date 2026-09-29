# C2 复跑命令（reviewer 独立复现）

代码根：`PathTogether/`（branch `slide-id-refactor`）。所有命令在仓库根执行。
浏览器：Playwright Chromium 1234（`node_modules/playwright` 1.62.1，无新下载）。
真实样本：`/home/solarise/ZCodeProject/histopilot-suite/切片文件夹/`（仅别名 KFB-1 / KFBF-A..D 出现在任何输出里）。
大文件全部走 `TMPDIR=$PWD/.gate-tmp`（/tmp 是 tmpfs，勿用）。cargo 需 `PATH=$HOME/.cargo/bin:$PATH`。

## 0. 构建

    PATH=$HOME/.cargo/bin:$PATH bash scripts/build_slide_transform.sh

产出 `static/tools/slide-transform/`（wasm + 胶水 + 手写 runner/engine/worker + manifest）。

## 1. Rust 核心回归（resume 之后必须仍绿）

    cd slide-transform-core && PATH=$HOME/.cargo/bin:$PATH cargo test -p slide-transform-core --features fixtures
    PATH=$HOME/.cargo/bin:$PATH cargo test -p slide-transform-cli
    cd ..

期望：15 + 2 + 37（C1 原有）+ 9（新增 `tests/resume.rs`：BF/FL 各档 checkpoint 恢复字节一致、
finalize 阶段崩溃恢复、wire JSON 往返、撒谎 journal 拒绝、validator 拒绝截断/环引用）全部 ok。

## 2. Python 差分（oracle 对照不回退）

    TMPDIR=$PWD/.gate-tmp .venv/bin/python -m pytest tests/test_slide_transform_core.py

期望：16 passed, 1 skipped（>4 GiB 为 opt-in，同 C1）。

## 3. KFB-1 整文件哈希（不变性）

    ./slide-transform-core/target/release/slide-transform convert \
      "$(ls /home/solarise/ZCodeProject/histopilot-suite/切片文件夹/*.kfb | sort | head -1)" \
      .gate-tmp/slide-tools-c2/nativ/kfb1-native.tif --overwrite
    sha256sum .gate-tmp/slide-tools-c2/nativ/kfb1-native.tif

期望：`385a59c6c69478c26fcac9f4232d065137864f5be47aa398f6222c6e818657fd`（与 C1 一致）。

## 4. 冒烟（浏览器管线 + 与原生 CLI 字节一致 + 导出）

    node tests/browser/slide_tools_c2/run_smoke.js

期望末行 `SMOKE PASS`；结果 `.gate-tmp/slide-tools-c2/browser/smoke/result.json`
（browserSha256 == nativeSha256）。

## 5. 故障矩阵（§10.3，26 项）

    node tests/browser/slide_tools_c2/run_faults.js            # 全部
    node tests/browser/slide_tools_c2/run_faults.js --only kill-worker   # 单项

期望：`FAULT MATRIX: 26/26 passed`；明细 `.gate-tmp/slide-tools-c2/browser/faults/results.json`
（每项含 sha 与原生对照）。每个场景独立新开页面；确定性崩溃点来自 worker 的
testMode fault 注入（`init{testMode:true}` 才生效，生产路径不经过）。

## 6. 无整文件物化 grep 门

    node tests/browser/slide_tools_c2/test_no_whole_file.js

期望 `no-whole-file gate: PASS`（同时断言：代码中无 `FileReaderSync`；读桥为源副本同步句柄直读 wasm 内存；复制/导出为 BYOB 复用缓冲）。

## 7. 真实样本浏览器↔原生一致（KFB-1 + KFBF-A..D）

    node tests/browser/slide_tools_c2/run_parity.js --samples \
      /home/solarise/ZCodeProject/histopilot-suite/切片文件夹 --fl-all

期望末行 `PARITY PASS`；结果 `.gate-tmp/slide-tools-c2/browser/parity/result.json`
（KFB-1 浏览器 sha256 == 原生 == 385a59c6…；KFBF-A..D 各自 == 原生）。报告/日志只含别名与哈希。

## 8. 内存矩阵（cgroup 模拟）

    # 生成输入（一次性；~15.5 GiB 磁盘）
    CLI=slide-transform-core/target/release/slide-transform
    $CLI gen-kfb .gate-tmp/slide-tools-c2/browser/memfix/bf-1g.kfb  --width 25600 --height 25600
    $CLI gen-kfb .gate-tmp/slide-tools-c2/browser/memfix/bf-4g.kfb  --width 54500 --height 54500
    $CLI gen-kfb .gate-tmp/slide-tools-c2/browser/memfix/bf-10g.kfb --width 81000 --height 81000

    # cgroup（每条一个 user scope；samples 记录 memory.events 的 oom/oom_kill）
    # 每条之间清空 profiles：rm -rf .gate-tmp/slide-tools-c2/browser/profiles/*
    bash tests/browser/slide_tools_c2/run_mem_cgroup.sh cg4g-1g   4G 1g  saver     # 另跑 -r2/-r3 看方差
    bash tests/browser/slide_tools_c2/run_mem_cgroup.sh cg4g-4g   4G 4g  saver
    bash tests/browser/slide_tools_c2/run_mem_cgroup.sh cg4g-10g  4G 10g saver     # 另跑 -r2
    bash tests/browser/slide_tools_c2/run_mem_cgroup.sh cg8g-4g   8G 4g  balanced
    bash tests/browser/slide_tools_c2/run_mem_cgroup.sh cg8g-10g  8G 10g balanced

每个作业的 OPFS 峰值 ≈ 源副本 + 输出（10 GiB 输入约 20 GiB），profile 放在 .gate-tmp。
结果 JSON：`.gate-tmp/slide-tools-c2/browser/mem/<label>.json`（基线=同浏览器空白页；
`deltaPeakVsBaselineMean` 为验收数）。所有结果标注「cgroup 模拟」。

## 9. CSP（模式 C，全部响应）

    node tests/browser/slide_tools_c2/server.js --port 8999 &
    curl -s -D- -o /dev/null http://127.0.0.1:8999/tools/worker.js | grep -i content-security
    # 存档：results/csp-headers.txt

## 9b. 读法隔离实验（根因证据，§4.3）

    # 脚本会在自身目录建浏览器 profile（含 4.4 GiB OPFS 副本），必须拷到 .gate-tmp 再跑
    mkdir -p .gate-tmp/c2-frs && cp docs/review-evidence/slide-tools/C2/scripts/read-methods/* .gate-tmp/c2-frs/
    node .gate-tmp/c2-frs/run.js .gate-tmp/slide-tools-c2/browser/memfix/bf-4g.kfb

    # 分阶段/分进程内存与 probe 读量诊断（验收方工具）
    node docs/review-evidence/slide-tools/C2/scripts/diag_mem.js .gate-tmp/slide-tools-c2/browser/memfix/bf-4g.kfb d4g saver
    node docs/review-evidence/slide-tools/C2/scripts/probe_cost.js "BF10G=.gate-tmp/slide-tools-c2/browser/memfix/bf-10g.kfb"

## 10. 汇总证据

- `results/fault-matrix.json`（24 项 pass/fail）
- `results/parity.json`（真实样本别名+哈希）
- `results/memory-matrix.json`（各内存运行摘要）
- `results/smoke.json`
- `results/csp-headers.txt`
- 大文件原始输出在 `.gate-tmp/slide-tools-c2/`（勿提交；复跑后可 `rm -rf` 清理）
