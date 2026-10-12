# CI runner 修复与依赖升级 review — 2026-10-12

## 结论

`10aab061` 与 `20528f28` 没有发现阻止合入的问题。支持先合入两个 CI 修复，再逐个重跑依赖 PR 的 CI；不把修复分支绿灯作为所有依赖升级已经通过的证明。

初次依赖升级审查发现 **1 项 P2：#10 升级 Vitest 5 后没有同步项目的 Node engines 声明**，后续已由 `6efe77d6` 修复并复核关闭。#1/#6 的读片不兼容是另一个已知阻碍，用户计划将两者合成适配改动、最后处理，这个安排正确。新的 CI 复核另发现 #6 含一项与 Zarr 无关的时间边界测试失败，见文末。

本次只进行 review、临时隔离验证和记录，没有合并、推送、rebase PR 或部署。

## 核对到的远端状态

首次核查时 GitHub `main` 仍为 `f38b96578fb63837b5b285a7048b37c8cf10b092`，七个 Dependabot PR（#1、#2、#4、#5、#6、#7、#10）均为 OPEN；#3、#8、#9 是 CLOSED，不是 MERGED。没有查到本轮七项升级已经合入 main 的记录。此后 main 已更新至 `20528f28`，见文末复核。

`ci/fix-runner-env` 为 `20528f28ddc880a3c743250ee2cc6af00db0ed2c`，确实是 main 的快进后继。HistoPilot 默认 6-worker 的修改属于另一仓库，不在这两个 PathTogether 提交内。

[run 38210814767](https://github.com/solarise94/PathTogether/actions/runs/38210814767) 的实际 head SHA 是 `20528f28`，四个 job 全部成功：

| Job | 日志实际结果 |
| --- | --- |
| python-pg | 3041 passed、115 skipped，覆盖率门槛通过 |
| python-pg-canary | 3 passed，真实执行 |
| host-bridge-unit | 1115 passed、2 skipped |
| admin-browser-e2e | 117 passed |

这些是实际 CI 日志的统计，不把用户本地的 3043 passed 当成 CI 统计。

## R1 — [P2] #10 的 Node 支持声明与新依赖要求矛盾

对象：[PR #10](https://github.com/solarise94/PathTogether/pull/10)，审查 head `4cb3d8ae`。位置：`package.json:11`（升级）及 `package.json:15`（现有 engines）。

新依赖 `vitest@5.0.3` 自己声明：

```json
{"node":"^22.12.0 || ^24.0.0 || >=26.0.0"}
```

项目仍声明 `node >=20`，因此允许开发者使用不在依赖支持范围内的 Node 20。CI 当前使用 Node 22，会掩盖这项兼容范围矛盾。

实证：隔离安装目录中使用 Node 20.20.2、npm 10.9.8，运行实际 PR lockfile 的 `npm ci --dry-run --engine-strict --ignore-scripts --offline`，返回 `EBADENGINE`，明确指出 `vitest@5.0.3` 不支持当前 Node。没有改动任何共享依赖目录。

另一方面，Node 20 下选取的 15 项面板单测可以执行通过，所以不能夸大为“Node 20 上所有测试都无法运行”。问题是项目明确承诺的支持范围与依赖实际要求冲突，在严格安装的环境中会直接失败，其他情况也处于未受支持的组合。

建议 #10 同步更新 `package.json` 和 lockfile 根包 engines，采用与依赖相容的 Node 范围，并同步开发说明。保持 CI Node 22。官方依据：[Vitest 5 迁移指南](https://main.vitest.dev/guide/migration/)，最终判断同时核对了实际 lockfile 和 npm 包元数据。

## 两个 CI 修复的代码审查

### `10aab061`：日志首条记录

六处节流状态统一改为 `None`，只有已经记录过时才比较 monotonic 时间差。原有 300 秒窗口、对外返回值和日志内容没有改变；测试辅助复位函数也同步更新。审查没有发现新的认证、余额或审计行为变化。

独立针对性运行六个受影响测试文件，包含 fresh-boot 新回归以及已有节流/错误处理用例：**11 passed，243 deselected**。新回归与既有窗口内/窗口外用例一起验证，未修改断言。

### `20528f28`：e2e 临时占位插件

桩只在 e2e server 的临时目录内安装；外部 `PLUGIN_BUNDLES_DIR` 通过 resolve 和路径父子关系检查后跳过写入；已有 histopilot 目录由 `_plugin_dir` 识别后也跳过。不会把桩放进正式镜像或仓库插件目录。

HostBridge 的 panel.toggle 和 panel.stateChanged 传输使用真实宿主入口，面板拖动、限制、位置记忆及标题弹层控制依然由宿主代码执行；没有修改或取消对应 Playwright 断言。用桩验证宿主行为的边界是合理的，但不代表真实 HistoPilot 会话管理、模型调用和宿主/真实插件的全部交互已由这套 CI 验证。

独立读取实际 CI 日志确认 117/117 通过。本机补充浏览器测试没有取得有效通过结果：默认 Chromium headless compositor 多次发生 native trap / 页面崩溃，换完整 Chromium 后仍在登录等早期阶段出现 crash。本机内核日志有相应 Compositor trap 记录；不能把这些运行算作补丁通过，也没有足够证据把 native crash 归因到这两行修复或桩。临时诊断配置的中途加载错误同样不作为产品缺陷。

## 依赖 PR 的失败原因不是全部相同

逐项下载实际失败 job 的日志，结果如下：

| PR | Python 历史失败数 | 核对到的原因 |
| --- | ---: | --- |
| #2 pytest-cov | 2 | role 缺失告警、hash 校验异常告警首条被节流 |
| #4 pytest | 2 | 同上 |
| #5 psycopg | 2 | 同上 |
| #7 Playwright | 2 | 同上；其浏览器 job 的 AI 面板 7 项缺插件失败 |
| #10 Vitest | 3 | 上述两项，另有审计告警首条被节流 |
| #1 tifffile | 129 | 上述两项，另有大量 TIFF/Zarr 打开、渲染、上传与 display 协议失败 |
| #6 zarr | 129 | 上述两项，另有大量 TIFF/Zarr 不兼容导致的读片失败 |

#1 的日志明确包含 `ValueError: zarr 2.18.7 < 3 is not supported`。该 PR 单独把 tifffile 改为 2026.9.20，保留 zarr<3；#6 单独改 zarr>=3.4.1，保留 tifffile 2024.5.22，两者都不能直接作为安全的单项运行期升级。

这并不否定用户已有的“#1/#6 合成 zarr 3 适配改动，最后处理”计划，但要更正“七个 PR 全部是同一个 CI 环境原因”的说法。修复分支的绿灯不会消除这些不兼容。官方版本背景：[tifffile 修订记录](https://github.com/cgohlke/tifffile/blob/master/CHANGES.rst)。

独立补充把新 tifffile/zarr 安装到私有 target 目录的尝试未成功：pip 临时安装受到 `/tmp` 用户磁盘配额阻碍，随后 PostgreSQL 的 WAL 写入也返回 `Disk quota exceeded`。因此该轮 71 passed / 60 setup errors **不是联合升级的有效验收结果**，本报告没有声称两者同时升级已经通过。共享 venv 未修改；本次临时 Vitest 依赖目录已清理。

## 对升级与合入计划的判断

- 两个 CI 修复可先合入；合入 main 后每个 PR 都应基于新 main 重跑。
- #7、#2、#5、#4 的现有差异和失败日志没有显示额外回归，仍以重跑后的完整 CI 为合入依据。
- #10 在 Node 22.23.2 下独立 JS 全量验证：**70 文件、1115 passed、2 skipped**；补 R1 后再合入。
- #1/#6 继续作为单独适配任务，一起验证 TIFF 打开、金字塔、RGB/多通道 plane、缩略图、区域读取、上传验证、显示指纹及分享权限链路后最后处理。
- release 分支目前与 main 同在 `f38b9657`；main 未来加入修复后，继续 release 开发时同步合入是必要的。当前没有理由改写历史。

## 证据位置

suite 根目录 `.gate-tmp/ci-upgrade-review-20261012/`：

- `pr-status.json`：PR head、检查和日志链接。
- `ci-failure-comparison.json`、`pr-*-ci-failures.log`：各 PR 的实际失败明细。
- `fix-ci-green.log`：成功 run 的完整日志。
- `throttle-tests.log`：独立日志回归测试。
- `vitest5-tests.log`、`vitest5-node20.log`、`vitest5-node20-engine-strict.log`：Vitest 5 与 Node 兼容验证。
- `stub-drag-tests.log`、`stub-drag-full-chromium.log`：本机浏览器限制及其诊断记录。
- `zarr3-install.log`、`zarr3-paired-tests.log`：被本机配额阻碍的无效联合验收尝试。

## 后续复核：main 已合入 CI 修复，五个升级 PR 可以依次合并

再次 fetch 并核对 GitHub：main 已快进到 `20528f28`；五个 PR 当前仍为 OPEN / MERGEABLE / CLEAN，base 均为这个 main。以下是当前 head 上的实际 CI，四项检查全部为 completed / success：

| PR | 审查 head | 当前成功 run |
| --- | --- | --- |
| #7 | `7f8bc2a0` | [38217000690](https://github.com/solarise94/PathTogether/actions/runs/38217000690) |
| #2 | `9ecd6edb` | [38217013835](https://github.com/solarise94/PathTogether/actions/runs/38217013835) |
| #5 | `b8921ed8` | [38217000950](https://github.com/solarise94/PathTogether/actions/runs/38217000950) |
| #4 | `7a2376b4` | [38216996483](https://github.com/solarise94/PathTogether/actions/runs/38216996483) |
| #10 | `6efe77d6` | [38217091007](https://github.com/solarise94/PathTogether/actions/runs/38217091007) |

`6efe77d6` 仅修改 package.json 与 lockfile 根条目的 Node engines，均为 `^22.12.0 || ^24.0.0 || >=26.0.0`。独立使用 npm 的 semver.subset 检查 lockfile 中全部 50 个声明 Node engines 的依赖：新的项目范围都在其支持范围内。R1 关闭。

确认可以按 **#7 → #2 → #5 → #4 → #10** 逐个合并。每次 main 变化后，以新 main 的 CI 与后续 PR 更新后的检查为准；若 #10 手工 rebase，旧 head 的绿灯不能作为 rebase 后 head 的验收。#1/#6 保持开放、单独处理。没有执行任何远端合并、rebase 或推送。

## R2 — [P2，独立测试不稳定] #6 多出的第 128 项不是 TIFF/Zarr 问题

最新实际失败日志：#1 为 **127 failed**，#6 为 **128 failed**。按测试名比较，#6 比 #1 多出的唯一用例是：

```text
tests/test_registration_antibot.py::test_known_and_unknown_email_identical_response_structure
```

[对应 run 38217021080](https://github.com/solarise94/PathTogether/actions/runs/38217021080) 的 diff 显示两个响应只在时间戳秒数上不同：`1791790784` 与 `1791790785`。两次响应均为 200 / submitted；不是账号存在性文案不同，也不是 Zarr 解码失败。

`tests/test_registration_antibot.py:1234–1260` 比较两个连续注册响应的完整 HTML，但 `_normalize` 只去掉随机 submission_id，没有控制或归一化模板中的 `data-resend-at` 时间。两个请求跨过整数秒边界时，绝对重发时间不同是正常的，完整字符串比较就会偶发失败。

因此应把剩余问题描述为“#1 的 127 项读片兼容失败；#6 的同样 127 项，再加 1 项注册测试时间边界失败”，而不是全部 128 项都属于 Zarr。建议固定测试中产生注册重发时间的时钟，保留 HTML 结构/文案和真实入队断言；或有针对性地归一化绝对时间并单独验证相同冷却时长。该独立测试问题不阻碍已经全绿的另外五个 PR 按上述顺序合并。

复核数据：`pr-recheck-status.json`、`pr-zarr-recheck-failures.json`、`pr-1-recheck-failures.log`、`pr-6-recheck-failures.log`，位于同一 review 证据目录。文档仍是本地未提交文件。

## 最终复核：五项升级已经合入 main

远端 main 最终为 `db5918c38d49d4bef8ba22540094970375daa3d1`；#7、#2、#5、#4、#10 均为 MERGED，#1/#6 仍为 OPEN。实际线性合并顺序与用户记录一致。

独立核对每个 squash 提交的 main CI，不仅核对合并前 PR 的 CI：

| main 提交 | 对应 PR | 成功 run | 四个 job |
| --- | --- | --- | --- |
| `405c99cd` | #7 | [38226157459](https://github.com/solarise94/PathTogether/actions/runs/38226157459) | 4/4 success |
| `a4b6476c` | #2 | [38228482248](https://github.com/solarise94/PathTogether/actions/runs/38228482248) | 4/4 success |
| `b42e9bfc` | #5 | [38230283482](https://github.com/solarise94/PathTogether/actions/runs/38230283482) | 4/4 success |
| `81866d60` | #4 | [38232422135](https://github.com/solarise94/PathTogether/actions/runs/38232422135) | 4/4 success |
| `db5918c3` | #10 | [38237301279](https://github.com/solarise94/PathTogether/actions/runs/38237301279) | 4/4 success |

合并后的 lockfile 与已审查的 `6efe77d6` 逐项比较：排除 Playwright 自身及其嵌套依赖后，全部 60 个原有包的 version、resolved、integrity 均保持一致，没有意外新增依赖。Playwright 包及依赖条目与最后一次合并前的 main `81866d60` 完全一致，版本为 1.64.0；旧的 `playwright/node_modules/fsevents` 消失属于这次 Playwright 升级的既有变化。Vitest 为 5.0.3，Node engines 根条目一致，最终范围仍覆盖于各锁定依赖的支持范围内。没有发现 lockfile 重建引入的新问题。

证据：`merged-main-ci.json`、`merged-lockfile-recheck.json`，存放于原 review 证据目录。最终复核没有重复安装共享依赖、没有修改产品或测试代码，也没有执行部署。

`release/viewer-skills-20261011` 相比最终 main 落后 7 个提交。后续 release 开发/发布前需要同步 main；独立注册测试稳定化和 tifffile/zarr 3 适配仍待处理。本 review 文档已记录最终状态，仍为本地未提交文件。
