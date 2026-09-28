# C0 browser spike: WASM 管线 + OPFS >4 GiB 随机写 + 内存/约束测量

阶段 C0 ③ 的可复跑 spike。测量结论见 `PathTogether/docs/slide-tools/c0-adr-browser.md`；
原始报告 JSON 在 `PathTogether/.gate-tmp/slide-tools-c0/browser/logs/`（复跑说明见该目录上级 `RERUN.md`）。

## 布局

```
browser-spike/
├── build.sh              # cargo build (wasm32) + wasm-bindgen --target web -> site/
├── rust/                 # Cargo crate（wasm-bindgen =0.2.129; rust-toolchain.toml 固定 1.98.1）
│   └── src/lib.rs        # ChunkProcessor::process_into —— checksum + 两次内存复制的分块处理
├── site/                 # 本地静态服务内容（127.0.0.1，OPFS 需安全上下文）
│   ├── index.html        # spike 页（CSP 友好：无内联脚本/样式）
│   ├── blank.html        # RSS 基线空白页
│   ├── common.js         # 字节合同（生成器/验证器/Rust 三方共享）+ FNV-1a64 + xorshift32
│   ├── io-worker.js      # File.slice 有界读 -> transferable ArrayBuffer
│   ├── compute-worker.js # WASM 实例化 + OPFS 同步句柄随机写 + 重开校验 + 导出代理
│   ├── engine.js         # 页面编排：worker 生命周期、terminate 探针、reload 后检查
│   ├── slide_chunk_spike.js / slide_chunk_spike_bg.wasm   # 构建产物
│   └── spike.css
├── server.js             # 静态服务 + CSP/COOP 矩阵（A/B/C/D/E）
├── make-input.js         # >4 GiB 确定性输入生成器（自检含 >2^32 点位）
├── rss.js                # /proc 进程树 RSS 采样
└── run-spike.js          # Playwright runner（persistent context，phase: blank/full/terminate/reload）
```

## 复跑

前置：`~/.cargo/bin`（rustc 1.98.1 + wasm32-unknown-unknown + wasm-bindgen-cli 0.2.129）、
node 22、`PathTogether/node_modules` 的 playwright 1.62.1、可选 `google-chrome`（channel chrome）。
GATE=`PathTogether/.gate-tmp/slide-tools-c0/browser`（磁盘，勿放 /tmp——tmpfs 仅 9.5 GB 且占内存）。

```bash
SPIKE=PathTogether/experiments/slide-tools-c0/browser-spike
GATE=PathTogether/.gate-tmp/slide-tools-c0/browser

# 1) 构建 WASM
$SPIKE/build.sh

# 2) 生成 >4 GiB 输入（约 17 s；spotOk 必须 true）
node $SPIKE/make-input.js 4800000000 $GATE/input.bin

# 3) 完整 4.5 GiB 作业（基线 + 写 + 校验 + 导出代理；窗口=引擎在途预算）
node $SPIKE/run-spike.js --label full-192 --phase full --browser chromium \
  --out-gib 4.5 --window-mb 192 --port 8931
node $SPIKE/run-spike.js --label full-384 ... --window-mb 384 ...
node $SPIKE/run-spike.js --label chrome-192 --browser chrome ...          # Chrome 153 无头
node $SPIKE/run-spike.js --label chrome-headed-192 --browser chrome --headed ...

# 4) cgroup 模拟（结果一律标注「cgroup 模拟」）
CGROUP_DESC="cgroup 模拟 MemoryMax=4G MemorySwapMax=0" systemd-run --user --scope \
  -p MemoryMax=4G -p MemorySwapMax=0 --unit slide-c0-<name> \
  node $SPIKE/run-spike.js --label <name> --phase full --browser chromium \
  --out-gib 4.5 --window-mb 192 --port 8931
# oom 计数：scope 退出即清理；运行中另开 shell 采样
# /sys/fs/cgroup/user.slice/user-1000.slice/user@1000.service/app.slice/<unit>.scope/memory.events

# 5) 韧性探针
node $SPIKE/run-spike.js --label terminate --phase terminate --kill-at-gib 1.5 --no-export ...
node $SPIKE/run-spike.js --label reload --phase reload --reload-at-gib 1.5 --no-export ...

# 6) CSP 矩阵（小作业即可）
for M in A B C D E; do node $SPIKE/run-spike.js --label csp$M --phase full \
  --out-gib 0.25 --window-mb 32 --csp $M --no-export --port 89xx ...; done
node $SPIKE/run-spike.js --label cspB-coop --csp B --coop ...   # COOP/COEP 附带验证

# 7) 磁盘清理（必做：每次 full 运行的 profile 含约 9 GB OPFS 数据）
rm -rf $GATE/profiles $GATE/input.bin
```

判定要点：`logs/<label>.json` 的 `result.ok`、`result.verify.failures` 为空、
`highMarkerOk/backpatchOk/truncationCanaryClean` 全 true；内存看
`job.deltaPeakVsBaselineMean`；终止/刷新看 `result.state.killReport` / `result.persisted`。

## 已知坑（本机实测）

- Playwright 1.62 给 `launchPersistentContext` 传 `env`（即使原样拷贝）会让 Chrome stable
  启动即优雅自关——runner 已不传 env。
- Chromium 根进程 argv 为空格连接（proctitle 重写），找 pid 需按原始 cmdline 子串匹配
  `--user-data-dir=`（见 run-spike.js `findBrowserPid`）。
- `systemd-run --scope` 不支持 `--remain-after-exit`；transient service 的
  `--wait --remain-after-exit` 组合会阻塞到手动 stop。
- reload 后立刻读 OPFS 可能 `NotReadableError`（句柄释放竞态）：恢复路径必须重试。
