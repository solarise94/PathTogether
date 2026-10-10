# 导航合并与轮廓细化：Codex 复核

日期：2026-10-10。复核基线：PathTogether `ce5b17a3`，HistoPilot `906e35f`；独立 hotfix `64a0396`。

结论：合并导航的调用量改善有原始数据支持；本轮发现 **2 项 P1、3 项 P2**。GPU 细化服务不宜按当前镜像命令直接启用。生产读片 hotfix 可独立处理，不应等待 SAM2 升级。

本次仅 review、只读生产核查和本地最小复现，没有改业务实现、调用付费模型、构建/运行 GPU 镜像或部署。
四条失败回归作为 patch 交给 Opus，没有把已知失败测试加入默认门禁。

## R1 · P1：Gunicorn preload 在 fork 前初始化 CUDA

位置：PT `services/contour-refine/Containerfile:19–22`；`services/contour-refine/app.py:89–99,221–224`。

镜像命令启用 `--preload`，导入 app 时默认 `_MODEL.ensure()`；GPU 可用时 `build_sam2(..., device="cuda")` 在 Gunicorn master 中完成。随后 worker fork 并使用继承的 predictor。此顺序违反 CUDA 的 fork 使用要求，CPU 直跑 Flask 的冒烟无法覆盖。

依据：Gunicorn 的 [preload_app 定义](https://gunicorn.org/reference/settings/#preload_app) 明确在 fork worker 前加载应用；PyTorch 的 [Poison fork / CUDA 多进程说明](https://docs.pytorch.org/docs/2.14/notes/multiprocessing.html#poison-fork-in-multiprocessing) 要求避免 fork 前初始化 accelerator。

这是代码路径与官方运行约束确认的启动缺陷；**本次没有 GPU 实机复现**。不能把静态确认写成已跑 GPU。
另外 `/healthz` 只检查已加载的 predictor，继承的 holder 可以让健康检查成功，不能证明 worker 的 GPU 推理能工作。

最小修复：去掉 `--preload`，在 worker 进程中加载模型（或改为明确的 post-fork 初始化）。保留单 worker，不必为此扩展服务架构。用最终镜像在 GPU 上完成一次真实 `/v1/refine`，同时检查 GPU 设备、返回结果与超时回退；只看 `/healthz` 不足。

## R2 · P1：1024 输出上限不限制原始解码量

位置：PT `app.py:19520–19523` 调 `_read_region_pil`，后者在 `app.py:18011–18019` 先按选定层 `read_region`，之后才 resize。

`contours/refine` 没复用 `/regions` 的解码像素估算、预算和并发闸。对于没有足够低分辨率金字塔层的切片，即使最终只输出 1024×1024，仍会先读取很大的原始区域。这发生在 PT 进程中，在 SAM2 推理锁和 HTTP 超时之前。

最小复现：单层 80,000×80,000 图，轮廓接近全图，使用只记录读取尺寸的假 reader（不分配大图）：

```text
HTTP 200
read_region(location=(0,0), level=0, size=(80000,80000))
attempted decoded pixels = 6,400,000,000
existing regions budget = 16,777,216
```

这意味着真实 reader 可能尝试分配数十 GB 数据；“输出小、SAM2 串行”不能限制这个峰值。

修复：在 `_read_region_pil` 前复用 region 路径的解码量估算和非阻塞并发准入，超过预算直接拒绝/回退；推理服务也应限制等待队列，避免客户端超时后后台仍排队计算。回归应断言预算拒绝发生在 `read_region` 之前，而不只是断言最终 PNG 边长。

复现 patch：`docs/review-evidence/navigation-refine-20261010/pathtogether-regression.patch`。

## R3 · P2：同位置 goto 复用旧快照时不检查显示身份

位置：HP `src/tools.ts:1473–1490`。

快照现在跨轮保留。同位置、同层 goto 的 shortcut 仅比较 bbox，未比较来源快照的 render context / asset revision。若用户下一轮改变通道、颜色、Z/T plane 等显示配置，新的 run 已绑定新 context，但同位置 goto 仍告诉模型“当前快照就是此视野”，不产生新图。

已复现：先在 cyan 通道 context 下 goto；换成 red context，再调用相同位置同层的 goto。第二次不返回图像，pending 仍属于旧 context。测试期望新图失败：`expected false to be true`。

修复：复用必须同时满足切片身份、资产 revision、render fingerprint 与 bbox 一致。不同则重新取景；不要关闭正常的跨轮同图复用。描绘入口也应确认来源快照仍适用于本次请求，避免没有再次 goto 时绕过检查。

复现 patch：HP patch 中 `current snapshot identity across runs`。

## R4 · P2：SAM2 crop 丢失来源快照的显示上下文

位置：HP `src/tools.ts:2035–2040`、`src/platform/http-client.ts:724–729`；PT `app.py:19520–19523`。

HP 只发送 points 和 expectedFingerprint；端点调用 `_read_region_pil` 时没有传 render_context。该 helper 支持 context，但这里实际总走默认读图。于是模型观察的是选定通道/plane/配色后的图，SAM2 接收到的可能是原始默认图。

后续校验只验证几何、切片和快照范围，无法发现边界来自另一种显示。provenance 仍记模型来源快照的 fingerprint，使审计也无法表达细化图的差异。

已用当前 harness 复现：建立 multichannel 快照后，refine 确实被调用，但请求没有来源 context。验收可选择：支持并传递来源 context；或在暂不支持的 context 下明确跳过细化，保留模型点列。静默改用默认图不应通过。

修复时使用 **pending 快照自身**的 context，而非盲目取当前全局 context，并由 PT 校验 revision 和规范化 fingerprint。补到真实 platform 契约测试。

复现 patch：HP patch 中 `refinement must use the snapshot rendering`。

## R5 · P2：取消检查只放在 refine 抛异常分支

位置：HP `src/tools.ts:2041–2069,2078–2092`。

代码在 catch 中检查 `loopSignal.aborted`；如果 refinement 已正常 resolve、signal 此时已经 abort，仍继续申请 bundle 序号和调用 annotate。后面的 session 文件 I/O 也给取消留下了窗口，annotate 调用没有传递该取消信号。

已复现：fake refine 在返回 `refined:false` 前 abort，然后正常 resolve；`annotateRequests.length` 实际为 1，预期为 0。现有测试让 refine 同时抛 AbortError，只覆盖了 catch 路径。

平台若已撤销 grant，写入会被后端拒绝；本次确认的是 **取消后仍发送写请求**，并未在生产复现取消后落库。若撤销与该请求竞速，不能靠本地现有代码保证“不写”。

修复：refine 返回后、任何实际写调用前检查 signal；将取消贯穿可取消的请求链，并保留平台撤权。分别覆盖 resolve 后取消、bundle I/O 期间取消、正常服务回退三种路径。

复现 patch：HP patch 中 `cancellation after refine resolves`。

## 生产 hotfix 核查

只读检查 `homepc` 得到：

- PT APP_REVISION 仍是 `5b87fb9036ba661b570c633118f3302d88c8e66e`。
- 运行的 HistoPilot package 是 **0.3.5**，镜像 `localhost/histopilot-demo:suite-20261002`。
- 持久化事件中存在 4 条“切片不存在” agent_error：2026-10-06 21:04:12、21:05:29、21:06:22，2026-10-09 14:32:43（+08:00）。
- hotfix 的 `enc()` 将 slide-id 改成 `/slides/by-id/<id>`，保留 legacy 文件名路径；另修正不被 DeepSeek 接受的 tuple `items` schema。
- 在 hotfix 分支实际运行 `test/http-client.test.ts` 和 `test/ai-drawing.test.ts`：**74 passed**。

生产错误与分支修复指向一致；这不是一次新的真实模型线上试用。本次所查事件支持已发生故障，不足以证明“从 10-01 起每一笔 AI 请求都失败”的完整时间范围。
建议把 0.3.6 作为独立 hotfix 验收发布，不与新默认描绘策略、goto 协议和 SAM2 绑定。此建议不代表本次已经部署。

## 评测数字与验收口径

从用户提供的 `drawing-eval/out-r3`、`out-a1`、`out-b1/results.json` 直接重算 **14 个 case**：

| 指标 | r3 | a1 | b1 |
| --- | ---: | ---: | ---: |
| 原 evaluator 的 pass | 14/14 | 13/14 | 14/14 |
| model_calls 合计 | 465 | 196 | 172 |
| 每 case model_calls 中位数 | 34 | 10.5 | 9.5 |
| transcript 工具调用合计 | 471 | 222 | 190 |
| 每 case 工具调用中位数 | 34 | 12 | 10.5 |
| token 合计 | 2,313,431 | 1,925,991 | 1,694,583 |
| case 结束未 finished | 4 | 0 | 0 |
| 每 case 耗时中位数（秒） | 108 | 69 | 78.5 |

报告中的 465 / 196 / 172 是 **模型请求数**，不是工具调用总数；38 / 11 / 11 与这三份原始文件按 case 重算不一致。若来自不同筛选或另一份 run，需说明分母与来源，不能混列。原始摘要保存在同目录 `eval-recount.json`，不含病例图像或文本。

改善方向成立，token 下降约 16.7% / 26.7%。不过：

- `experiments/drawing-eval.ts:247–254` 的 pass 检查末轮是否调用描绘/矩形，以及应画时是否至少写入一次；没有要求 finished、回答内容完整、目标召回率或轮廓质量达标。
- r3 的 4 个 paused case 仍算 pass，因此 14/14 不是“14 例任务完整完成”。b1 本次所有 case 确实 finished，能报告该样本的结果，不能据此保证今后不会跑满步数。
- 一张切片的 14 个语义 case、一次随机运行，不能证明分割模型在其他组织/染色/尺度上的质量；SAM2 置信分和面积比也不等于病理轮廓准确率。
- 细化服务本次没有做 GPU 实测，报告的 0.2 秒只是估计。

建议分开记录：触发行为、任务完成、定位、轮廓质量、耗时/用量。可继续使用这 14 个语义用例；额外加入 R2–R5 的确定性回归和实际 GPU 镜像冒烟。

## 复现补丁与执行范围

证据目录：`docs/review-evidence/navigation-refine-20261010/`。

| 文件 | 内容 |
| --- | --- |
| `histopilot-regressions.patch` | HP 三条失败回归：快照 context、细化 context、取消竞态 |
| `pathtogether-regression.patch` | PT 一条失败回归：原始解码预算 |
| `hp-snapshot-cancel.txt` | 两条回归实际失败输出 |
| `hp-refine-context.txt` | context 丢失回归实际失败输出 |
| `pt-decode-budget.txt` | 6.4G 像素请求记录；无巨大内存分配 |
| `hotfix-tests.txt` | hotfix 74 passed |
| `eval-recount.json` | 三轮 eval 的确定性重算结果 |

使用 `git apply --unidiff-zero <补丁路径>` 将各 patch 应用到对应基线即可复现；修完后再纳入正式门禁。命令：

```sh
# HistoPilot 906e35f：应用 histopilot-regressions.patch 后
npx vitest run test/tools.test.ts test/ai-drawing-refine.test.ts -t 'Codex review'

# PathTogether ce5b17a3：应用 pathtogether-regression.patch 后
python -m pytest tests/test_plugin_contour_refine.py -k test_review -q

# 独立 hotfix 64a0396（不应用补丁）
npx vitest run test/http-client.test.ts test/ai-drawing.test.ts
```

本次没有重跑全部 3020/1529 项，也没有新增模型评测开销。现有全量通过与这些新反例并不矛盾：门禁此前没有覆盖这些边界。
