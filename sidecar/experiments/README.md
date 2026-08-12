# Phase 4 A/B 实验（Wave 1 数据面 + Wave 2 执行 runner）

> 状态：**准备工作 GO，正式采数 NO-GO**。Wave 1 数据面（fixture / 任务集 / arm 定义 / rubric / 报表 / 门禁）与 Wave 2 **执行 runner**（`src/run.ts` + `run-ab.ts`，scripted 模式可用）均已交付。real-model 采数仍被 `gate.ts` 门禁拦截，直到 `prompt_cache_key` 在真实 CPA 网关上验证通过。
>
> 设计依据：`docs/ai-context-cache-visual-workspace-upgrade.md` §14 Phase 4、§15.3、§15.4、§16。

## 为什么现在只做数据面

§14 Phase 3 要求显式 Prompt Cache 验证 `prompt_cache_key`/breakpoint/usage 透传。在真实 CPA 网关验证之前，缓存命中率数字不作为正式结论（CPA-UNVERIFIED）。因此本 Wave 只交付：

- 可被 runner 消费的**数据格式**（taskset / arms / rubric transcript / metrics JSONL）；
- **格式校验**与**报表聚合**；
- 一个**硬门禁**（`gate.ts`），real-model 模式必须 `PHASE4_CPA_VERIFIED=1` 才放行。

执行 runner（Wave 2）会用 `makeFakeStreamFn`（scripted 模式）跑通整套管线，并在门禁解除后切到真实模型。

## 目录结构

```
sidecar/experiments/
├── README.md                     本文档
├── fixtures/
│   ├── generate.py               合成去标识化 pyramidal TIFF 生成器 + --pin manifest
│   ├── manifest.schema.json      manifest JSON Schema
│   ├── manifest.example.json     形状示例（占位指纹，未 pin）
│   └── slides/                   生成的切片（gitignored，不提交）
├── tasksets/
│   ├── reading-v1.json           §15.3 七类质量回归任务集（7 任务）
│   └── taskset.schema.json       taskset JSON Schema
├── arms/
│   ├── step1-overview-none.json  Step 1：稳定区无概览对照组（§17 风险 2）
│   ├── step1-overview-768.json   Step 1：768px 概览
│   ├── step1-overview-1024.json  Step 1：1024px 概览（产品默认）
│   ├── step2-window-272k.json    Step 2：context window 272k（产品默认）
│   ├── step2-window-400k.json    Step 2：400k
│   └── step2-window-512k.json    Step 2：512k
└── src/
    ├── taskset.ts                taskset 加载 + 校验（无 ajv 运行时依赖）
    ├── arms.ts                   arm 加载 + 矩阵生成 + Step-2 image-arm 解析
    ├── rubric.ts                 rubric 检查器（纯函数）
    ├── report.ts                 metrics 聚合 + 确定性 markdown 报表
    ├── gate.ts                   NO-GO 门禁（CPA-UNVERIFIED）
    ├── fake-stream.ts            scripted 模式 streamFn（回放 model_script；自包含副本，不依赖 test/）
    ├── manifest.ts               pinned manifest 加载 + 校验 + ground-truth 区域索引
    ├── flask-process.ts          spawn Flask（ephemeral port + 内部 token）+ 健康探测 + 清退
    └── run.ts                    执行 runner 库（矩阵驱动 + 每 cell 真实 AgentRunner + 输出）
├── run-ab.ts                     CLI 入口（parseArgs；调用 run.ts；接真实 fixture→Flask→pin 管线）
```

测试在 `sidecar/test/experiments-*.test.ts`（vitest，含 `experiments-runner.test.ts`）。

> **不进发布包**：`tsconfig.build.json` 的 `rootDir`/`include` 仅含 `src`，`experiments/` 只参与 `tsc --noEmit` 类型检查，不进 `dist/`。

## 两步 A/B 设计（§14 Phase 4）

**Step 1（固定 context window，对比图像策略）**：三组 `step1-overview-{none,768,1024}`。仅改概览开关 + `overview_long_edge`；working/detail 档位、`visual_context_budget_tokens`、`context_window_tokens`（272k）保持一致。

**Step 2（固定图像策略，对比 context window）**：三组 `step2-window-{272k,400k,512k}`。仅改 `context_window_tokens`；图像策略由 `--image-arm` 指向 **Step 1 胜出组**（arm 里的 `image_strategy: "${step1_winner}"` 占位符由 `arms.ts` 解析为显式值）。

## `overview_enabled` 产品开关（§17 风险 2，已接线）

`step1-overview-none` 用 `overview_enabled: false` 表达“稳定区不携带概览图”。Wave 2 已把它接到实际抑制逻辑（不再是占位字段）：

- `transform-context.ts` `TransformContextConfig.overview_enabled` / `TransformContextSettings.overviewEnabled`（默认 true），`resolveTransformSettings` 解析。
- **Phase 2b assembler**（`request-assembler.ts` `makeRequestAssembler`）：当 `settings.overviewEnabled === false` 时，跳过 `materializeStableOverview`（不发起 overview 的 region fetch），稳定区**只保留文本块**（summary + annotation_index），`overview_image_bytes_sent` 为 0、`overview_status` 报 `no-overview`。不新增 metric 字段——arm 身份已记录该开关。
- **Phase 1 transformOnce 兼容路径忽略该开关**（Phase 1 无稳定区，概览是 recency 位置之一，不在本开关语义内）。
- `app.py` `_validate_ai_tuning` 白名单 `overview_enabled`（布尔，默认 true），并在 `DEFAULT_CONFIG` 中声明，经 `_merge_config` 透传到 sidecar `RunConfig` → `resolveTransformSettings`。
- 端到端流向：`app.py _validate_ai_tuning(overview_enabled:bool) → ai_config.json → _build_sidecar_config → RunConfig.overview_enabled → resolveTransformSettings → TransformContextSettings.overviewEnabled → makeRequestAssembler`。

## 生成 fixture 与 pin manifest

工作解释器是仓库根 `.venv`（含 openslide / PIL / tifffile / numpy）：

```bash
# 仅生成切片（写入 slides/，已 gitignore）
.venv/bin/python sidecar/experiments/fixtures/generate.py

# 生成 + pin manifest（需要一个可访问、且已部署生成切片的 Flask）
.venv/bin/python sidecar/experiments/fixtures/generate.py --pin \
    --flask-url http://127.0.0.1:5000 \
    --manifest sidecar/experiments/fixtures/manifest.json
```

`--pin` 必须带 `--flask-url`，否则报错；Flask 不可达也立即失败。指纹来自 Flask `/internal/ai/slide_info`（`_slide_fingerprint` = `mtime_ns:size`）。在 Wave 2 smoke run pin 成功前，仓库只提交 `manifest.example.json`（占位指纹）。

合成切片是 openslide 可读的页式金字塔（`NewSubfileType=1` 降采样页，tiled + LZW；已验证 openslide 1.4.6 读出 3 层、downsamples 1.0/2.0/4.0）。坐标是 level-0 像素，与 `tasksets/reading-v1.json` 的 `bbox_revisit` 断言一致（单一真源在 `generate.py` 的 `FIXTURE_SPECS`）。

## 执行 runner（Wave 2，scripted 模式可用）

`run-ab.ts` 是 CLI 入口，`src/run.ts` 是可注入依赖的库（vitest 用 `RunnerDeps.acquireEnv` 注入内存 FlaskClient mock，不 spawn 真 Flask）。

**Step 1（固定 context window，对比图像策略）**：

```bash
cd sidecar
npx tsx experiments/run-ab.ts --step 1
```

**Step 2（固定图像策略，对比 context window；`--image-arm` 指向 Step 1 胜出组）**：

```bash
cd sidecar
npx tsx experiments/run-ab.ts --step 2 --image-arm step1-overview-1024
```

CLI 参数（`parseArgs`）：`--step 1|2`、`--image-arm <id>`（step 2 必填）、`--mode scripted|real-model`（默认 scripted）、`--taskset <path>`（默认 `experiments/tasksets/reading-v1.json`）、`--arms-dir`、`--fixtures-dir`、`--out experiments/results/<run-id>`（run-id 默认 `step{N}-{mode}-{utc时间戳}`）、`--keep-flask`（调试：run 后不清退 Flask）、`--help`。

### 单次 run 的执行管线（scripted 模式）

1. `gate.ts` `assertDataCollectionAllowed(mode)` **最先**调用；real-model 除非 `PHASE4_CPA_VERIFIED=1` 否则抛错。real-model 即使放行也在 Wave 2 抛“未实现（provider 接线随 CPA 验证落地）”。
2. 参数校验：step 2 必须带 `--image-arm`；加载 taskset + arms 目录，用 `arms.ts buildStepMatrix` 解析 `image_strategy` 占位符。
3. `acquireEnv`：`ensureSlides`（slides/ 为空才 spawn `generate.py --out-dir`）→ `spawnFlask`（ephemeral port，slides 作 UPLOAD_DIR，随机内部 token；不设 ADMIN_PASSWORD 故 AUTH_ENABLED=False，`/api/slides` 公开做健康探测）→ `pinManifest`（`generate.py --pin`，token 经 env 透传，满足 `/internal/ai/slide_info` 鉴权）→ `loadManifest` → 真 `FlaskClient`（HTTP，非内存 mock）。
4. taskset 的 `fixture_id` 与 manifest 交叉校验。
5. 对每个 (arm, task)（arm-major，task 按 taskset 顺序）：**fresh SessionStore（独立 temp 目录）+ SessionEventBus + 共享 FlaskClient + AgentRunner**（`streamFn=makeFakeStreamFn(task.model_script)`、`metricsSink=采集 sink`、`compactionModels=scripted 假摘要`）；用 `runMain` 驱动 `user_turns[0]`，后续 turn append 用户消息后 `continueMain`；每请求收 `RequestMetrics`。
6. transcript 扁平成 `RubricTranscriptEntry[]`（snapshot 的 viewport bbox 取自 `toolResult.details.src`），`checkRubric(rubric, transcript, manifestRegions)`。
7. 输出（run 目录）：`metrics.jsonl`（每行一个 `ReportRow`）、`rubric.json`（每 task+arm 含每断言详情）、`transcripts.json`、`report.md`（`aggregateReport`+`renderReport`，确定性、**无时间戳**）、`run.json`（run 元数据，**可带时间戳**）。
8. 清退：`handle.stop()`（除非 `--keep-flask`），删除每 cell 的 temp session 目录；slides/ 留存复用（gitignored）。

> **RunConfig/transform 覆盖接线点**：`run.ts buildRunConfig(arm)` 把 arm 的 `resolvedOverrides`（snake_case）**直接展开**到 `RunConfig` 上。因为 `resolveTransformSettings(config)`（`agent-runner.ts runAgentLoop`）从 config 对象上直接读这些 snake_case 字段，所以 `overview_enabled` / `overview_long_edge` / `visual_*` / `image_*` / `context_window_tokens` 无需中间适配器就到达 assembler + 引擎；`prompt_cache_mode` 来自 arm 的独立字段。

> **`metricsSink` 接线**：`AgentRunner` 构造接受 `overrides.metricsSink`（默认 `defaultMetricsSink`，即 `console.info` 一行 JSON）。runner 注入一个**采集 sink**把每请求 `RequestMetrics` 收进数组（无需改 `agent-runner.ts`），再附 `task_id`/`arm_id`/`step`/`wall_ms` 落 JSONL。

### 输出布局

```
experiments/results/<run-id>/
├── metrics.jsonl      每 (task, arm, request) 一行 ReportRow
├── rubric.json        每 (task, arm) rubric 结果 + 每断言详情
├── transcripts.json   扁平 transcript（调试 / 复跑 rubric）
├── report.md          确定性 markdown 对比报表（无时间戳）
└── run.json           run 元数据（step/mode/arm ids/taskset id/manifest sha/时间戳）
```

### scripted 模式已知 caveats

- **wall_ms 无意义**：度量的是 `makeFakeStreamFn` 回放，不是真实模型；仅在 real-model 模式下才有意义。
- **cat5 compaction 不会真正 compact**：scripted 模式下没有真实模型摘要能力；runner 注入假 `compactionModels`（返回固定摘要），且 272k window 下短 script 几乎不触发压缩。cat5 的长会话语义需 real-model 模式验证。
- **overview 命中依赖稳定区 checkpoint**：scripted run 起步时无 checkpoint，稳定概览在首请求后才可能 back-fill；概览字节数在 scripted 下偏低是预期的。
- **real-model 未实现**：gate 通过后仍抛“Wave 2 未实现”，provider 接线随 CPA 验证落地。

## NO-GO 门禁与如何解除

`src/gate.ts`：

- `scripted` 模式：始终放行（用 fake streamFn，不产生真实缓存声明）。
- `real-model` 模式：仅当 `env.PHASE4_CPA_VERIFIED === "1"` 放行；否则抛 `DataCollectionGateError`，消息含 `CPA-UNVERIFIED`、`prompt_cache_key`、并引用 §14 Phase 3。

解除条件：在真实 CPA 网关上验证 `prompt_cache_key`/breakpoint/usage 透传（§14 Phase 3），随后 `export PHASE4_CPA_VERIFIED=1`。报表（`report.ts`）在存在 `prompt_cache_mode != "off"` 数据时也会在顶部打出 NO-GO 横幅。

## CPA gemini 冒烟（一次性验证脚本）

`smoke-gemini.ts`：真实 Flask + 真实 AgentRunner + CPA 网关 gemini 兼容端点
（`gemini-3.6-flash-high`）的端到端验证——goto/snapshot/标注审阅/finish 工具链、
快照图片入请求（working_set_image_bytes_sent>0）、usage 指标落地。
运行：`npx tsx experiments/smoke-gemini.ts`（需 CPA base_url/key 写在脚本顶部常量，
或自行改为 env 读取）。验证实录见设计文档 §14 Phase 4 批注（2026-08-12）。
